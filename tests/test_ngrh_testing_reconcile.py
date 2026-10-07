"""Step 7: reconcile and delete-tests (design 5.9 and 5.11), against the in-memory fake CLI layer.

Reconcile must create a test only when the service has none for the template, update it only where it
differs, make the sources match without ever duplicating, stop on duplicates and on anything that doesn't
resolve before writing anything, and leave a matching test alone on a second run.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from ngrh_scenario import (  # noqa: E402
    COMPOSITES, DEPLOYMENT, DESIRED, DESIRED_SOURCES, HOPS, NAME, OBSERVABILITY, SPEC_FILE, SUCCESS, TEMPLATE,
    deployed_fake, environment, spec_tests,
)
from ngrh_fake_aws import (  # noqa: E402
    ACCOUNT, BROKER_HOST, BROKER_ID, ENV, PRIMARY, STANDBY, FakeAws, alarm_arn, service_arn, template_arn,
)

from ngrh_testing import cli, context, reconcile, spec  # noqa: E402


def run_reconcile(fake, write=True):
    return reconcile.reconcile(fake, environment(fake), spec_tests(), write=write)


def write_operations(fake):
    return [c[1] for c in fake.writes()]


# --- creating ---------------------------------------------------------------------------------

class TestCreate:

    def test_an_absent_test_is_created_with_the_specs_settings_and_sources(self):
        fake = deployed_fake()
        plans, lines = run_reconcile(fake)
        assert fake.calls_of("create-test") == [DESIRED]
        (test_id,) = fake.tests
        assert sorted(fake.sources[test_id]) == DESIRED_SOURCES
        assert write_operations(fake) == ["create-test", "put-test-sources"]
        assert lines == [f"{NAME}: created test {test_id}; sources +{len(SUCCESS) + len(OBSERVABILITY)} -0"]
        assert plans[0].action == "create"

    def test_the_sources_are_written_in_the_shape_the_api_takes(self):
        fake = deployed_fake()
        run_reconcile(fake)
        (put,) = fake.calls_of("put-test-sources")
        assert {"successCriteriaAlarm": {"alarmArn": alarm_arn(SUCCESS[0])}} in put["test_sources"]
        assert {"observabilityAlarm": {"alarmArn": alarm_arn(OBSERVABILITY[-1])}} in put["test_sources"]
        assert len(put["test_sources"]) == len(SUCCESS) + len(OBSERVABILITY)

    def test_the_broker_host_comes_from_the_brokers_own_endpoint(self):
        fake = deployed_fake()
        fake.brokers[BROKER_ID] = [
            f"amqps://{BROKER_HOST}:5671",
            f"amqps://{BROKER_HOST}:5671",  # listed twice: one host
        ]
        run_reconcile(fake)
        assert fake.calls_of("create-test")[0]["parameters"]["dependencies"] == [BROKER_HOST]


# --- idempotence and updates --------------------------------------------------------------------

class TestReconcileAgain:

    def test_a_second_run_writes_nothing(self):
        fake = deployed_fake()
        run_reconcile(fake)
        before = len(fake.writes())
        plans, lines = run_reconcile(fake)
        assert len(fake.writes()) == before
        assert plans[0].in_sync and "unchanged" in lines[0]
        assert len(fake.tests) == 1                               # never a second test

    def test_only_what_differs_is_updated_and_the_sources_are_left_alone(self):
        fake = deployed_fake()
        run_reconcile(fake)
        (test_id,) = fake.tests
        fake.tests[test_id]["parameters"]["duration"] = ["30"]
        fake.tests[test_id]["roleName"] = "someone-elses-role"
        fake.calls.clear()
        plans, lines = run_reconcile(fake)
        assert write_operations(fake) == ["update-test"]
        wanted = {k: v for k, v in DESIRED.items() if k != "test_template_arn"}      # UpdateTest takes no template
        assert fake.calls_of("update-test") == [{**wanted, "test_id": test_id}]
        assert lines == [f"{NAME}: updated test {test_id} (parameters, role)"]
        assert fake.tests[test_id]["parameters"]["duration"] == ["15"]

    def test_defaults_the_service_adds_to_logging_are_not_drift(self):
        fake = deployed_fake()
        run_reconcile(fake)
        (test_id,) = fake.tests
        fake.tests[test_id]["loggingConfiguration"]["logSchemaVersion"] = "2"
        plans, _ = run_reconcile(fake)
        assert plans[0].in_sync

    def test_parameter_values_in_a_different_order_are_not_drift(self):
        fake = deployed_fake()
        fake.brokers[BROKER_ID] = ["amqps://a.example.com:5671", "amqps://z.example.com:5671"]
        run_reconcile(fake)
        (test_id,) = fake.tests
        fake.tests[test_id]["parameters"]["dependencies"].reverse()
        plans, _ = run_reconcile(fake)
        assert plans[0].in_sync

    def test_a_changed_logging_configuration_is_updated(self):
        fake = deployed_fake()
        run_reconcile(fake)
        (test_id,) = fake.tests
        fake.tests[test_id]["loggingConfiguration"] = {"cloudWatchLogGroupArn": f"arn:aws:logs:{STANDBY}:{ACCOUNT}:log-group:/aws/fis/ngrh-tests{ENV}"}
        plans, _ = run_reconcile(fake)
        assert plans[0].differences == ["logging"]
        assert fake.tests[test_id]["loggingConfiguration"] == DESIRED["logging_configuration"]

    def test_a_changed_stop_condition_is_updated(self):
        fake = deployed_fake()
        run_reconcile(fake)
        (test_id,) = fake.tests
        fake.tests[test_id]["stopConditions"] = [{"source": "none", "value": "none"}]
        plans, lines = run_reconcile(fake)
        assert plans[0].differences == ["stop conditions"]
        assert fake.tests[test_id]["stopConditions"] == DESIRED["stop_conditions"]

    def test_sources_are_made_to_match_removals_first_and_each_kind_is_right(self):
        fake = deployed_fake()
        run_reconcile(fake)
        (test_id,) = fake.tests
        extra = ("OBSERVABILITY", alarm_arn(f"hop-ui-slow-{PRIMARY}{ENV}"))
        wrong_kind = ("OBSERVABILITY", alarm_arn(SUCCESS[1]))            # a success alarm filed as observability
        fake.sources[test_id].remove(("SUCCESS_CRITERIA", alarm_arn(SUCCESS[1])))
        fake.sources[test_id].remove(("OBSERVABILITY", alarm_arn(OBSERVABILITY[0])))
        fake.sources[test_id] += [extra, wrong_kind]
        fake.calls.clear()
        plans, lines = run_reconcile(fake)
        assert write_operations(fake) == ["delete-test-sources", "put-test-sources"]      # removals free a slot first
        assert sorted(fake.sources[test_id]) == DESIRED_SOURCES
        assert lines == [f"{NAME}: test {test_id} unchanged; sources +2 -2"]

    def test_an_active_run_blocking_the_update_says_so(self):
        fake = deployed_fake()
        run_reconcile(fake)
        (test_id,) = fake.tests
        fake.tests[test_id]["parameters"]["duration"] = ["30"]
        fake.add_run("orders", test_id, "RUNNING")
        with pytest.raises(reconcile.ReconcileError, match="a run is active on this service"):
            run_reconcile(fake)

    def test_a_failure_after_the_create_says_what_was_already_done(self):
        fake = deployed_fake()
        fake.fail_on("resiliencehubv2", "put-test-sources", "An error occurred (ServiceQuotaExceededException): at most five sources")
        with pytest.raises(reconcile.ReconcileError) as e:
            run_reconcile(fake)
        assert "ServiceQuotaExceededException" in e.value.problems[0]
        assert "already done: created test" in e.value.problems[1]


# --- stopping before anything is written -----------------------------------------------------------

class TestNothingIsWrittenWhenSomethingIsWrong:

    def test_two_tests_for_one_service_and_template_stop_the_run_with_both_ids(self):
        fake = deployed_fake()
        first = fake.add_test("orders", TEMPLATE)
        second = fake.add_test("orders", TEMPLATE)
        with pytest.raises(reconcile.ReconcileError) as e:
            run_reconcile(fake)
        assert first in e.value.problems[0] and second in e.value.problems[0]
        assert "nothing is deleted automatically" in e.value.problems[0]
        assert fake.writes() == [] and set(fake.tests) == {first, second}

    def test_a_lookup_that_finds_nothing_names_itself(self):
        fake = deployed_fake()
        fake.brokers[BROKER_ID] = []
        with pytest.raises(context.ContextError, match="lookup mq-broker-host in us-east-1 returned nothing"):
            run_reconcile(fake)
        assert fake.writes() == []

    def test_a_lookup_whose_call_fails_names_the_lookup_and_the_error(self):
        fake = deployed_fake()
        fake.fail_on("mq", "describe-broker", "An error occurred (ForbiddenException): denied")
        with pytest.raises(context.ContextError, match="lookup mq-broker-host in us-east-1 failed: .*ForbiddenException"):
            run_reconcile(fake)
        assert fake.writes() == []

    def test_an_alarm_that_does_not_exist_is_named_with_its_region(self):
        fake = deployed_fake()
        del fake.alarms[(STANDBY, f"region-degraded-{STANDBY}{ENV}")]
        del fake.alarms[(PRIMARY, OBSERVABILITY[-1])]
        with pytest.raises(context.ContextError) as e:
            run_reconcile(fake)
        assert sorted(e.value.problems) == [
            f"alarm {OBSERVABILITY[-1]} does not exist in {PRIMARY}",
            f"alarm region-degraded-{STANDBY}{ENV} does not exist in {STANDBY}",
        ]
        assert fake.writes() == []

    def test_an_alarm_of_the_wrong_kind_still_counts_as_existing(self):
        # Composite alarms are found too: describe-alarms lists metric alarms only unless asked.
        fake = deployed_fake()
        run_reconcile(fake)
        assert any(c[1] == "describe-alarms" and c[3]["alarm_types"] == ["MetricAlarm", "CompositeAlarm"] for c in fake.calls)

    def test_a_template_the_region_does_not_offer_stops_the_run(self):
        fake = deployed_fake()
        fake.templates.discard(template_arn(TEMPLATE))
        with pytest.raises(context.ContextError, match="template aws-dependency-validation:rtdep001 is not offered"):
            run_reconcile(fake)
        assert fake.writes() == []

    def test_an_ngrh_stack_without_the_per_service_outputs_says_to_deploy_it(self):
        fake = deployed_fake()
        del fake.outputs["OrdersServiceArn"]
        with pytest.raises(context.ContextError, match="has no output OrdersServiceArn; deploy the current ngrh.yaml"):
            run_reconcile(fake)

    def test_a_missing_ngrh_stack_says_to_deploy_it(self):
        fake = deployed_fake()
        fake.outputs = None
        with pytest.raises(context.ContextError, match=f"stack ngrh{ENV} does not exist in {PRIMARY}; deploy it first"):
            environment(fake)

    def test_the_dry_run_reads_everything_and_writes_nothing(self):
        fake = deployed_fake()
        plans, lines = run_reconcile(fake, write=False)
        assert fake.writes() == []
        assert plans[0].action == "create" and lines == [f"{NAME}: to create; sources +{len(SUCCESS) + len(OBSERVABILITY)} -0"]


# --- delete-tests ---------------------------------------------------------------------------------

class TestDeleteTests:

    def test_every_test_on_the_stacks_services_is_deleted(self):
        fake = deployed_fake()
        orders = fake.add_test("orders", TEMPLATE)
        ui = fake.add_test("ui", "aws-multi-region-isolation:rtmr001")
        lines = reconcile.delete_tests(fake, PRIMARY, ENV)
        assert fake.tests == {}
        assert sorted(fake.calls_of("delete-test"), key=lambda c: c["test_id"]) == [
            {"service_arn": service_arn("orders"), "test_id": orders},
            {"service_arn": service_arn("ui"), "test_id": ui},
        ]
        assert len(lines) == 2 and all(line.startswith("deleted test") for line in lines)

    def test_a_run_that_has_not_ended_blocks_the_whole_delete(self):
        fake = deployed_fake()
        orders = fake.add_test("orders", TEMPLATE)
        fake.add_test("ui", "aws-multi-region-isolation:rtmr001")
        run = fake.add_run("orders", orders, "RUNNING")
        with pytest.raises(reconcile.ReconcileError, match=f"test run {run} RUNNING; stop it first"):
            reconcile.delete_tests(fake, PRIMARY, ENV)
        assert fake.calls_of("delete-test") == [] and len(fake.tests) == 2

    @pytest.mark.parametrize("status", ["INITIALIZING", "STOPPING"])
    def test_every_active_status_blocks(self, status):
        fake = deployed_fake()
        fake.add_run("orders", fake.add_test("orders", TEMPLATE), status)
        with pytest.raises(reconcile.ReconcileError):
            reconcile.delete_tests(fake, PRIMARY, ENV)

    @pytest.mark.parametrize("status", ["PASSED", "FAILED", "STOPPED", "ERROR"])
    def test_a_finished_run_does_not_block(self, status):
        fake = deployed_fake()
        fake.add_run("orders", fake.add_test("orders", TEMPLATE), status)
        reconcile.delete_tests(fake, PRIMARY, ENV)
        assert fake.tests == {}

    def test_an_absent_stack_has_no_tests_and_nothing_is_called_but_the_lookup(self):
        fake = deployed_fake()
        fake.outputs = None
        assert reconcile.delete_tests(fake, PRIMARY, ENV) == [f"stack ngrh{ENV} does not exist in {PRIMARY}: no tests to delete"]
        assert [c[1] for c in fake.calls] == ["describe-stacks"]

    def test_nothing_to_delete_is_said_plainly(self):
        assert reconcile.delete_tests(deployed_fake(), PRIMARY, ENV) == ["no tests to delete"]

    def test_a_service_that_is_already_gone_has_no_tests(self):
        fake = deployed_fake()
        fake.fail_on("resiliencehubv2", "list-test-runs", "An error occurred (ResourceNotFoundException): Service not found", times=6)
        fake.fail_on("resiliencehubv2", "list-tests", "An error occurred (ResourceNotFoundException): Service not found", times=12)
        assert reconcile.delete_tests(fake, PRIMARY, ENV) == ["no tests to delete"]

    def test_any_other_error_is_not_swallowed(self):
        fake = deployed_fake()
        fake.fail_on("resiliencehubv2", "list-test-runs", "An error occurred (AccessDeniedException): no")
        with pytest.raises(Exception, match="AccessDeniedException"):
            reconcile.delete_tests(fake, PRIMARY, ENV)

    def test_a_test_that_survives_its_delete_is_an_error(self):
        fake = deployed_fake()
        stubborn = fake.add_test("orders", TEMPLATE)
        fake._handlers[("resiliencehubv2", "delete-test")] = lambda region, **params: {"testId": params["test_id"]}   # says yes, keeps it
        with pytest.raises(reconcile.ReconcileError, match=f"test {stubborn} of .* is still there"):
            reconcile.delete_tests(fake, PRIMARY, ENV)


# --- the command line -----------------------------------------------------------------------------------

ARGS = ["--primary-region", PRIMARY, "--standby-region", STANDBY, f"--env={ENV}"]


class TestCommandLine:

    def test_reconcile_prints_a_line_per_test_and_exits_clean(self, capsys):
        fake = deployed_fake()
        assert cli.main(["reconcile", *ARGS], aws=fake) == 0
        assert capsys.readouterr().out.startswith(f"{NAME}: created test ")

    def test_check_exits_3_when_the_tests_have_drifted_and_writes_nothing(self, capsys):
        fake = deployed_fake()
        assert cli.main(["reconcile", *ARGS, "--check"], aws=fake) == cli.EXIT_FOUND
        assert "have drifted from the spec: run make ngrh-tests" in capsys.readouterr().out
        assert fake.writes() == []

    def test_check_exits_0_when_the_tests_match(self, capsys):
        fake = deployed_fake()
        cli.main(["reconcile", *ARGS], aws=fake)
        before = len(fake.writes())
        assert cli.main(["reconcile", *ARGS, "--check"], aws=fake) == 0
        assert len(fake.writes()) == before
        assert "drifted" not in capsys.readouterr().out

    def test_a_named_test_that_is_not_in_the_spec_is_an_error(self, capsys):
        assert cli.main(["reconcile", *ARGS, "--test", "nope"], aws=deployed_fake()) == cli.EXIT_ERROR
        assert "no test named 'nope'" in capsys.readouterr().err

    def test_every_problem_is_printed_to_stderr(self, capsys):
        fake = deployed_fake()
        del fake.alarms[(PRIMARY, OBSERVABILITY[-1])]
        del fake.alarms[(PRIMARY, SUCCESS[0])]
        assert cli.main(["reconcile", *ARGS], aws=fake) == cli.EXIT_ERROR
        err = capsys.readouterr().err
        assert SUCCESS[0] in err and OBSERVABILITY[-1] in err

    def test_an_api_error_is_a_message_not_a_traceback(self, capsys):
        fake = deployed_fake()
        fake.fail_on("resiliencehubv2", "list-tests", "An error occurred (AccessDeniedException): no identity-based policy allows resiliencehub:ListTests")
        assert cli.main(["reconcile", *ARGS], aws=fake) == cli.EXIT_ERROR
        assert "AccessDeniedException" in capsys.readouterr().err

    def test_delete_tests_prints_what_it_deleted(self, capsys):
        fake = deployed_fake()
        fake.add_test("orders", TEMPLATE)
        assert cli.main(["delete-tests", *ARGS], aws=fake) == 0
        assert capsys.readouterr().out.startswith("deleted test ")

    def test_the_default_spec_is_the_one_beside_the_package(self):
        assert Path(cli.DEFAULT_SPEC) == SPEC_FILE


# --- the Makefile ----------------------------------------------------------------------------------------

REAL_MAKE = shutil.which("make")


@pytest.mark.skipif(REAL_MAKE is None, reason="make not installed")
class TestMakefile:

    def _dry_run(self, tmp_path, target, *variables):
        # The Makefile shells out to aws while parsing; a stub that answers nothing is enough for -n.
        stub = tmp_path / "aws"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
        r = subprocess.run([REAL_MAKE, "-C", str(DEPLOYMENT), "-n", target, f"ENV={ENV}", *variables], env=env,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout.splitlines()

    def _tool_line(self, tmp_path, target, *variables):
        (line,) = [l for l in self._dry_run(tmp_path, target, *variables) if "ngrh_testing" in l]
        return line

    def test_preflight_checks_everything_live_unless_told_otherwise(self, tmp_path):
        line = self._tool_line(tmp_path, "ngrh-test-preflight")
        assert line.startswith("python3 -m ngrh_testing preflight --primary-region ")
        assert line.endswith('--test "all" --mode "live"')
        assert self._tool_line(tmp_path, "ngrh-test-preflight", "TEST=orders-broker-dependency", "MODE=static").endswith(
            '--test "orders-broker-dependency" --mode "static"')

    def test_run_stop_and_report_name_the_test_and_the_env(self, tmp_path):
        for target, command in (("ngrh-test", "run"), ("ngrh-test-stop", "stop"), ("ngrh-test-report", "report")):
            line = self._tool_line(tmp_path, target, "TEST=orders-broker-dependency")
            assert line.startswith(f"python3 -m ngrh_testing {command} --primary-region ")
            assert f'--env="{ENV}"' in line and '--test "orders-broker-dependency"' in line

    def test_a_run_id_reaches_the_report_only_when_given(self, tmp_path):
        assert '--run "run-0042"' in self._tool_line(tmp_path, "ngrh-test-report", "TEST=x", "RUN=run-0042")
        assert "--run" not in self._tool_line(tmp_path, "ngrh-test-report", "TEST=x")

    def test_running_without_naming_a_test_is_refused_by_the_tool_not_run_for_all(self, tmp_path):
        assert '--test ""' in self._tool_line(tmp_path, "ngrh-test")

    def test_a_run_waits_for_no_alarms_unless_asked(self, tmp_path):
        assert self._tool_line(tmp_path, "ngrh-test", "TEST=x").endswith('--alarm-wait-minutes "0"')
        assert self._tool_line(tmp_path, "ngrh-test", "TEST=x", "ALARM_WAIT=15").endswith('--alarm-wait-minutes "15"')

    def test_stop_waits_for_the_run_to_end_only_when_asked(self, tmp_path):
        assert self._tool_line(tmp_path, "ngrh-test-stop", "TEST=x").endswith('--wait-minutes "0"')
        assert self._tool_line(tmp_path, "ngrh-test-stop", "TEST=x", "STOP_WAIT=10").endswith('--wait-minutes "10"')

    def test_destroy_ngrh_deletes_the_tests_before_the_stack(self, tmp_path):
        lines = self._dry_run(tmp_path, "destroy-ngrh")
        tests = next(i for i, line in enumerate(lines) if "ngrh_testing delete-tests" in line)
        stack = next(i for i, line in enumerate(lines) if "delete-stack --stack-name ngrh" in line)
        assert tests < stack
        assert f'--env="{ENV}"' in lines[tests]                 # ENV starts with a dash, so it goes after an =

    def test_ngrh_tests_reconciles_with_the_makefiles_regions_and_env(self, tmp_path):
        (line,) = [l for l in self._dry_run(tmp_path, "ngrh-tests") if "ngrh_testing" in l]
        assert line.startswith("python3 -m ngrh_testing reconcile --primary-region ")
        assert "--standby-region" in line and f'--env="{ENV}"' in line

    def test_destroy_all_still_starts_with_destroy_ngrh(self):
        rule = next(l for l in (DEPLOYMENT / "Makefile").read_text().splitlines() if l.startswith("destroy-all:"))
        assert rule.split(":", 1)[1].split()[0] == "destroy-ngrh"
