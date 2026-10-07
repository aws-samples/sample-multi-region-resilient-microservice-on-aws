"""Step 7: preflight (design 5.9). Every refusal has a test that breaks exactly that one thing in an
otherwise ready deployment, so a check that stops looking fails here; static mode must skip the checks about
what is happening right now and only those.
"""

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from ngrh_scenario import (  # noqa: E402
    OBSERVABILITY, PEER_DEGRADED, STOP, SUCCESS, TEMPLATE, environment, reconciled_fake, spec_tests,
)
from ngrh_fake_aws import CLUSTER, ENV, PLAN_ARN, PRIMARY, STANDBY, alarm_arn, service_arn  # noqa: E402

from ngrh_testing import cli, preflight  # noqa: E402

LIVE, STATIC = preflight.LIVE, preflight.STATIC
INVOKER = f"ngrh-invoker{ENV}"
EXPERIMENT = f"ngrh-test-experiment{ENV}"


def check(fake, mode=LIVE):
    return preflight.run_checks(fake, environment(fake), spec_tests(), mode)


def reasons(result, number):
    return [r.reason for r in result.refusals if r.check == number]


def numbers(result):
    return sorted({r.check for r in result.refusals})


def execution(action, region, state="completed", at="2026-10-06T14:00:00+00:00", eid="exec-1"):
    return {"planArn": PLAN_ARN, "executionId": eid, "startTime": at, "mode": "graceful", "executionState": state,
            "executionAction": action, "executionRegion": region}


def foreign_run(fake, status="RUNNING", service="tester-service"):
    """A run on a Resilience Hub service that is not ours, as another tester's would be."""
    fake.runs["run-foreign"] = {"testRunId": "run-foreign", "testId": "test-foreign", "status": status, "serviceArn": service_arn(service),
                                "startedAt": "2026-10-06T15:00:00+00:00", "testTemplateArn": "x", "script": [status], "polls": 0}
    return "run-foreign"


# --- a ready deployment ---------------------------------------------------------------------------

class TestReady:

    @pytest.mark.parametrize("mode", [LIVE, STATIC])
    def test_everything_in_place_passes(self, mode):
        result = check(reconciled_fake(), mode)
        assert result.passed and result.refusals == [] and result.notes == []

    def test_static_mode_leaves_out_the_checks_about_what_is_happening_now(self):
        fake = reconciled_fake()
        check(fake, STATIC)
        asked = {c[1] for c in fake.calls}
        assert not asked & {"list-services", "list-test-runs", "list-experiments", "list-plan-executions"}
        fake.calls.clear()
        check(fake, LIVE)
        assert {"list-services", "list-test-runs", "list-experiments", "list-plan-executions"} <= {c[1] for c in fake.calls}

    def test_a_check_makes_no_write(self):
        fake = reconciled_fake()
        check(fake)
        assert fake.writes() == []

    def test_an_unknown_mode_is_rejected(self):
        with pytest.raises(ValueError, match="mode must be one of live, static"):
            check(reconciled_fake(), "sometimes")

    def test_render_lists_each_reason_with_its_check_number(self):
        fake = reconciled_fake()
        fake.roles.discard(EXPERIMENT)
        text = preflight.render(check(fake), LIVE, ["orders-broker-dependency"])
        assert text.splitlines()[0] == "Preflight (live) for orders-broker-dependency: refused, 1 reason(s)"
        assert text.splitlines()[1].startswith("  [3] the experiment role ")
        assert preflight.render(check(reconciled_fake()), STATIC, ["a", "b"]) == "Preflight (static) for a, b: all checks passed\n"

    def test_several_problems_are_all_reported_together(self):
        fake = reconciled_fake()
        fake.roles.discard(EXPERIMENT)
        fake.alarms[(PRIMARY, SUCCESS[0])]["state"] = "ALARM"
        fake.add_ecs_service("orders", sidecar=False)
        foreign_run(fake)
        assert numbers(check(fake)) == [3, 4, 5, 9]


# --- 1: the credential sentinel -----------------------------------------------------------------------

ARGS = ["--primary-region", PRIMARY, "--standby-region", STANDBY, f"--env={ENV}"]


class TestCommand:

    def test_a_ready_deployment_exits_0_with_the_summary(self, capsys):
        assert cli.main(["preflight", *ARGS], aws=reconciled_fake()) == cli.EXIT_OK
        assert capsys.readouterr().out == "Preflight (live) for orders-broker-dependency: all checks passed\n"

    def test_a_refusal_exits_2_and_lists_the_reasons(self, capsys):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, SUCCESS[0])]["state"] = "ALARM"
        assert cli.main(["preflight", *ARGS, "--test", "orders-broker-dependency"], aws=fake) == cli.EXIT_REFUSED
        assert f"[4] orders-broker-dependency: success alarm {SUCCESS[0]} is ALARM, not OK" in capsys.readouterr().out

    def test_static_mode_is_chosen_with_mode(self, capsys):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, SUCCESS[0])]["state"] = "ALARM"
        assert cli.main(["preflight", *ARGS, "--mode", "static"], aws=fake) == cli.EXIT_OK
        assert capsys.readouterr().out.startswith("Preflight (static) for")

    def test_a_test_that_is_not_in_the_spec_is_an_error_not_a_pass(self, capsys):
        assert cli.main(["preflight", *ARGS, "--test", "nope"], aws=reconciled_fake()) == cli.EXIT_ERROR
        assert "no test named 'nope'" in capsys.readouterr().err


class TestCredentials:

    def test_credentials_that_do_not_work_refuse_at_once(self, capsys):
        fake = reconciled_fake()
        fake.fail_on("sts", "get-caller-identity", "An error occurred (ExpiredToken): The security token is expired")
        args = ["preflight", "--primary-region", PRIMARY, "--standby-region", STANDBY, f"--env={ENV}", "--test", "all"]
        assert cli.main(args, aws=fake) == cli.EXIT_REFUSED
        out = capsys.readouterr().out
        assert "[1] the credentials do not work" in out and "ExpiredToken" in out
        assert [c[1] for c in fake.calls] == ["get-caller-identity"]          # nothing else was asked


# --- 2 and 10: the tests match the spec, and every lookup returned a value ----------------------------------------

class TestSpecMatch:

    def test_a_test_that_does_not_exist_yet(self):
        from ngrh_scenario import deployed_fake
        result = check(deployed_fake())
        assert reasons(result, 2) == ["the test does not exist yet; run make ngrh-tests"]
        assert result.refusals[0].test == "orders-broker-dependency"

    def test_drifted_settings(self):
        fake = reconciled_fake()
        (test_id,) = fake.tests
        fake.tests[test_id]["parameters"]["duration"] = ["30"]
        (reason,) = reasons(check(fake), 2)
        assert reason == f"test {test_id} differs from the spec in settings (parameters); run make ngrh-tests"

    def test_drifted_sources(self):
        fake = reconciled_fake()
        (test_id,) = fake.tests
        fake.sources[test_id].pop()
        fake.sources[test_id].append(("OBSERVABILITY", "arn:aws:cloudwatch:us-east-1:111111111111:alarm:other"))
        (reason,) = reasons(check(fake), 2)
        assert "sources (+1 -1)" in reason and "run make ngrh-tests" in reason

    def test_two_tests_for_one_service_and_template(self):
        fake = reconciled_fake()
        extra = fake.add_test("orders", TEMPLATE)
        (test_id,) = [t for t in fake.tests if t != extra]
        (reason,) = reasons(check(fake), 2)
        assert extra in reason and test_id in reason and "nothing is deleted automatically" in reason

    def test_a_template_the_region_does_not_offer(self):
        from ngrh_fake_aws import template_arn
        fake = reconciled_fake()
        fake.templates.discard(template_arn(TEMPLATE))
        assert any("is not offered" in r for r in reasons(check(fake), 2))

    def test_a_lookup_that_returns_nothing_is_check_10_and_stops_the_checks_that_need_it(self):
        fake = reconciled_fake()
        fake.brokers = {next(iter(fake.brokers)): []}
        result = check(fake)
        assert reasons(result, 10) == ["orders-broker-dependency: lookup mq-broker-host in us-east-1 returned nothing"]
        assert numbers(result) == [10]                                   # drift, alarms and ECS were not judged on a guess

    def test_a_lookup_whose_call_fails_is_check_10(self):
        fake = reconciled_fake()
        fake.fail_on("mq", "describe-broker", "An error occurred (ForbiddenException): denied")
        (reason,) = reasons(check(fake), 10)
        assert "lookup mq-broker-host in us-east-1 failed" in reason and "ForbiddenException" in reason

    def test_an_error_reading_the_test_is_a_refusal_not_a_crash(self):
        fake = reconciled_fake()
        fake.fail_on("resiliencehubv2", "list-tests", "An error occurred (AccessDeniedException): no", times=2)
        assert any("could not read the test" in r for r in reasons(check(fake), 2))


# --- 3: the roles ------------------------------------------------------------------------------------

class TestRoles:

    def test_an_invoker_role_without_the_testing_policy(self):
        fake = reconciled_fake()
        fake.role_policies[INVOKER] = ["AWSResilienceHubV2AssessmentExecutionPolicy"]
        (reason,) = reasons(check(fake), 3)
        assert "AWSResilienceHubResilienceTestingPolicy attached" in reason and "make ngrh" in reason

    def test_an_experiment_role_that_does_not_exist(self):
        fake = reconciled_fake()
        fake.roles.discard(EXPERIMENT)
        (reason,) = reasons(check(fake), 3)
        assert EXPERIMENT in reason and "does not exist" in reason

    def test_an_invoker_role_that_cannot_be_read(self):
        fake = reconciled_fake()
        del fake.role_policies[INVOKER]
        assert any("could not read the invoker role" in r for r in reasons(check(fake), 3))

    def test_an_ngrh_stack_without_the_role_output(self):
        fake = reconciled_fake()
        del fake.outputs["InvokerRoleName"]
        assert any("has no output InvokerRoleName" in r for r in reasons(check(fake), 3))


# --- 4: the alarms -------------------------------------------------------------------------------------

class TestAlarms:

    @pytest.mark.parametrize("mode", [LIVE, STATIC])
    def test_an_alarm_that_does_not_exist_refuses_in_both_modes(self, mode):
        fake = reconciled_fake()
        del fake.alarms[(PRIMARY, OBSERVABILITY[1])]
        assert reasons(check(fake, mode), 4) == [f"alarm {OBSERVABILITY[1]} does not exist in {PRIMARY}"]

    def test_an_evidence_alarm_that_does_not_exist(self):
        fake = reconciled_fake()
        del fake.alarms[(STANDBY, PEER_DEGRADED)]
        assert reasons(check(fake), 4) == [f"alarm {PEER_DEGRADED} does not exist in {STANDBY}"]

    def test_a_success_alarm_already_in_alarm_refuses_live_only(self):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, SUCCESS[0])]["state"] = "ALARM"
        assert reasons(check(fake), 4) == [f"success alarm {SUCCESS[0]} is ALARM, not OK, in {PRIMARY}"]
        assert check(fake, STATIC).passed

    @pytest.mark.parametrize("state", ["ALARM", "INSUFFICIENT_DATA"])
    def test_a_stop_alarm_that_is_not_ok_refuses_live(self, state):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, STOP)]["state"] = state                   # a composite alarm
        assert reasons(check(fake), 4) == [f"stop alarm {STOP} is {state}, not OK, in {PRIMARY}"]

    def test_an_observability_or_evidence_alarm_in_alarm_does_not_refuse(self):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, OBSERVABILITY[0])]["state"] = "ALARM"
        fake.alarms[(STANDBY, PEER_DEGRADED)]["state"] = "ALARM"
        fake.alarms[(PRIMARY, "hop-ui-slow-us-east-1-t")]["state"] = "INSUFFICIENT_DATA"
        assert check(fake).passed


# --- 4: the alarms are ones the service discovers -----------------------------------------------------------
#
# 2026-10-07: StartTestRun refused the orders test with "One or more alarms referenced by this test were not
# discovered for this service", after a fresh assessment that had looked at every orders alarm but one. The
# one was hop-checkout-errors, tagged checkout, and orders discovers the tags orders and shared.

class TestAlarmScope:

    def tag(self, fake, name, tags, region=PRIMARY):
        fake.alarm_tags[(region, name)] = tags

    @pytest.mark.parametrize("mode", [LIVE, STATIC])
    @pytest.mark.parametrize("source", [SUCCESS[0], OBSERVABILITY[0]])
    def test_a_source_alarm_tagged_for_another_service_refuses_in_both_modes(self, mode, source):
        fake = reconciled_fake()
        self.tag(fake, source, {"service": "checkout"})
        kind = "success" if source in SUCCESS else "observability"
        assert reasons(check(fake, mode), 4) == [
            f"{kind} source alarm {source} is tagged service=checkout, but service orders discovers only alarms tagged "
            "service=orders|shared; Resilience Hub would refuse the run (alarms not discovered). Use an alarm of that "
            "service or a shared one as a source; this one can stay in the evidence alarms"]
        assert numbers(check(fake, mode)) == [4]

    def test_the_refusal_names_the_test(self):
        fake = reconciled_fake()
        self.tag(fake, OBSERVABILITY[0], {"service": "checkout"})
        assert [r.test for r in check(fake).refusals] == ["orders-broker-dependency"]
        assert str(check(fake).refusals[0]).startswith("[4] orders-broker-dependency: observability source alarm ")

    @pytest.mark.parametrize("tags", [{"service": "orders"}, {"service": "shared"}, {"service": "shared", "team": "x"}])
    def test_the_services_own_tag_and_shared_are_accepted(self, tags):
        fake = reconciled_fake()
        self.tag(fake, OBSERVABILITY[0], tags)
        assert check(fake).passed

    @pytest.mark.parametrize("tags,said", [({}, "none of those tags"), ({"team": "x"}, "none of those tags"),
                                           ({"Service": "orders"}, "none of those tags")])
    def test_an_alarm_without_the_service_tag_refuses(self, tags, said):
        fake = reconciled_fake()
        self.tag(fake, SUCCESS[1], tags)
        (reason,) = reasons(check(fake), 4)
        assert f"success source alarm {SUCCESS[1]} is tagged {said}" in reason

    def test_stop_and_evidence_alarms_may_belong_to_other_services(self):
        # They are not test sources; region-degraded is shared in practice, and the hop alarms of the
        # services behind ui are the evidence the report lays out.
        fake = reconciled_fake()
        self.tag(fake, STOP, {"service": "checkout"})
        self.tag(fake, PEER_DEGRADED, {"service": "ui"}, region=STANDBY)
        assert check(fake).passed

    def test_any_tag_filter_of_the_service_will_do(self):
        fake = reconciled_fake()
        fake.service_scopes[service_arn("orders")] = [
            {"type": "TAGS", "resourceTags": [{"key": "service", "values": ["orders"]}, {"key": "tier", "values": ["gold"]}]}]
        self.tag(fake, SUCCESS[0], {"tier": "gold"})
        self.tag(fake, SUCCESS[1], {"service": "orders"})
        self.tag(fake, OBSERVABILITY[0], {"service": "orders"})
        self.tag(fake, OBSERVABILITY[1], {"service": "orders"})
        assert check(fake).passed
        self.tag(fake, SUCCESS[0], {"tier": "silver"})
        (reason,) = reasons(check(fake), 4)
        assert "is tagged tier=silver, but" in reason and "only alarms tagged service=orders or tier=gold;" in reason

    def test_a_service_no_tag_scopes_is_a_note_not_a_verdict(self):
        fake = reconciled_fake()
        fake.service_scopes[service_arn("orders")] = [{"type": "DESIGN_FILE", "designFileS3Url": "s3://b/hld.md"}]
        self.tag(fake, OBSERVABILITY[0], {"service": "checkout"})
        result = check(fake)
        assert result.passed
        assert result.notes == ["orders-broker-dependency: service orders has no tag input source, so which alarms it discovers is not checked"]

    def test_an_alarm_that_does_not_exist_is_reported_once_by_the_existence_check(self):
        fake = reconciled_fake()
        del fake.alarms[(PRIMARY, OBSERVABILITY[1])]
        assert reasons(check(fake), 4) == [f"alarm {OBSERVABILITY[1]} does not exist in {PRIMARY}"]

    def test_every_source_alarm_is_read_once_and_the_service_once(self):
        fake = reconciled_fake()
        check(fake)
        tagged = [c["resource_arn"] for c in fake.calls_of("list-tags-for-resource")]
        assert sorted(tagged) == sorted(alarm_arn(n) for n in SUCCESS + OBSERVABILITY)
        assert len(fake.calls_of("list-input-sources")) == 1

    @pytest.mark.parametrize("operation", ["list-input-sources", "list-tags-for-resource"])
    def test_an_error_reading_the_scope_is_a_refusal_not_a_crash(self, operation):
        fake = reconciled_fake()
        service = "resiliencehubv2" if operation == "list-input-sources" else "cloudwatch"
        fake.fail_on(service, operation, "An error occurred (AccessDeniedException): no")
        (reason,) = reasons(check(fake), 4)
        assert reason.startswith("could not check which alarms each service discovers: ") and "AccessDeniedException" in reason


# --- 5, 6, 7: nothing else is running ---------------------------------------------------------------------------

class TestNothingElseIsRunning:

    def test_a_run_on_a_service_that_is_not_ours_refuses(self):
        fake = reconciled_fake()
        run_id = foreign_run(fake)
        (reason,) = reasons(check(fake), 5)
        assert "service tester-service-id" in reason and run_id in reason and "RUNNING" in reason

    def test_a_run_of_ours_on_another_service_refuses_too(self):
        fake = reconciled_fake()
        fake.add_run("ui", fake.add_test("ui", "aws-multi-region-isolation:rtmr001"), "RUNNING")
        assert len(reasons(check(fake), 5)) == 1

    @pytest.mark.parametrize("status", ["INITIALIZING", "RUNNING", "STOPPING"])
    def test_every_active_status_refuses(self, status):
        fake = reconciled_fake()
        foreign_run(fake, status)
        assert len(reasons(check(fake), 5)) == 1

    @pytest.mark.parametrize("status", ["PASSED", "FAILED", "STOPPED", "ERROR"])
    def test_a_finished_run_does_not(self, status):
        fake = reconciled_fake()
        foreign_run(fake, status)
        assert check(fake).passed

    def test_a_service_deleted_while_we_looked_is_ignored(self):
        fake = reconciled_fake()
        fake.fail_on("resiliencehubv2", "list-test-runs", "An error occurred (ResourceNotFoundException): gone")
        assert check(fake).passed

    def test_the_active_run_check_is_live_only(self):
        fake = reconciled_fake()
        foreign_run(fake)
        assert check(fake, STATIC).passed

    @pytest.mark.parametrize("status", ["pending", "initiating", "running", "stopping"])
    def test_a_fis_experiment_in_either_region_refuses(self, status):
        fake = reconciled_fake()
        fake.fis[STANDBY].append({"id": "EXP123", "state": {"status": status}})
        assert reasons(check(fake), 6) == [f"FIS experiment EXP123 is {status} in {STANDBY}"]
        assert check(fake, STATIC).passed

    @pytest.mark.parametrize("status", ["completed", "stopped", "failed", "cancelled"])
    def test_a_finished_fis_experiment_does_not(self, status):
        fake = reconciled_fake()
        fake.fis[PRIMARY].append({"id": "EXP123", "state": {"status": status}})
        assert check(fake).passed

    @pytest.mark.parametrize("state", ["inProgress", "pausedByFailedStep", "pausedByOperator", "pendingManualApproval", "pending"])
    def test_a_plan_execution_in_progress_refuses(self, state):
        fake = reconciled_fake()
        fake.plan_executions[STANDBY].append(execution("deactivate", PRIMARY, state))
        (reason,) = reasons(check(fake), 7)
        assert reason == f"plan execution exec-1 (deactivate {PRIMARY}) is {state}"
        assert check(fake, STATIC).passed

    @pytest.mark.parametrize("state", ["completed", "completedWithExceptions", "completedMonitoringApplicationHealth"])
    def test_a_region_left_deactivated_says_to_fail_back(self, state):
        fake = reconciled_fake()
        fake.plan_executions[STANDBY].append(execution("deactivate", PRIMARY, state))
        assert reasons(check(fake), 7) == [f"{PRIMARY} was deactivated by plan execution exec-1 and not activated since; "
                                           f"run make failback REGION={PRIMARY} first"]

    def test_a_deactivated_region_that_was_activated_afterwards_is_fine(self):
        fake = reconciled_fake()
        fake.plan_executions[STANDBY].append(execution("deactivate", PRIMARY, at="2026-10-06T14:00:00+00:00", eid="exec-1"))
        fake.plan_executions[PRIMARY].append(execution("activate", PRIMARY, at="2026-10-06T15:00:00+00:00", eid="exec-2"))
        assert check(fake).passed

    def test_the_latest_execution_decides_so_a_later_deactivate_refuses(self):
        fake = reconciled_fake()
        fake.plan_executions[PRIMARY].append(execution("activate", PRIMARY, at="2026-10-06T14:00:00+00:00", eid="exec-1"))
        fake.plan_executions[STANDBY].append(execution("deactivate", PRIMARY, at="2026-10-06T15:00:00+00:00", eid="exec-2"))
        assert "exec-2" in reasons(check(fake), 7)[0]

    def test_an_execution_both_endpoints_list_counts_once(self):
        fake = reconciled_fake()
        same = execution("deactivate", PRIMARY, "inProgress")
        fake.plan_executions[PRIMARY].append(same)
        fake.plan_executions[STANDBY].append(dict(same))
        assert len(reasons(check(fake), 7)) == 1

    @pytest.mark.parametrize("state", ["failed", "canceled", "planExecutionTimedOut"])
    def test_an_execution_that_did_not_finish_deactivated_nothing(self, state):
        fake = reconciled_fake()
        fake.plan_executions[STANDBY].append(execution("deactivate", PRIMARY, state))
        assert check(fake).passed

    def test_a_post_recovery_execution_is_not_an_activation_or_a_deactivation(self):
        fake = reconciled_fake()
        fake.plan_executions[STANDBY].append(execution("postRecovery", PRIMARY))
        assert check(fake).passed

    def test_an_endpoint_that_cannot_be_read_refuses(self):
        fake = reconciled_fake()
        fake.fail_on("arc-region-switch", "list-plan-executions", "An error occurred (AccessDeniedException): no")
        (reason,) = reasons(check(fake), 7)
        assert reason.startswith(f"could not list plan executions at the {PRIMARY} endpoint") and "AccessDeniedException" in reason

    def test_a_deployment_without_the_plan_stack_has_no_executions_to_check(self):
        fake = reconciled_fake()
        del fake.stacks[(PRIMARY, f"region-switch{ENV}")]
        result = check(fake)
        assert result.passed
        assert result.notes == [f"stack region-switch{ENV} is not deployed in {PRIMARY}: no plan executions to check"]
        assert "list-plan-executions" not in [c[1] for c in fake.calls]


# --- 9: the service under test -------------------------------------------------------------------------

class TestServiceConfiguration:

    @pytest.mark.parametrize("kwargs,message", [
        ({"sidecar": False}, "has no amazon-ssm-agent sidecar container"),
        ({"pid_mode": None}, "does not have PidMode task"),
        ({"pid_mode": "host"}, "does not have PidMode task"),
        ({"fault_injection": False}, "does not have EnableFaultInjection on"),
        ({"providers": ("FARGATE", "FARGATE_SPOT")}, "is not on on-demand Fargate only (FARGATE, FARGATE_SPOT)"),
        ({"providers": ("FARGATE_SPOT",)}, "is not on on-demand Fargate only (FARGATE_SPOT)"),
        ({"exec_on": True}, "has ECS Exec on"),
    ])
    @pytest.mark.parametrize("mode", [LIVE, STATIC])
    def test_each_thing_the_fault_needs_is_checked_in_both_modes(self, kwargs, message, mode):
        fake = reconciled_fake()
        fake.add_ecs_service("orders", **kwargs)
        (reason,) = reasons(check(fake, mode), 9)
        assert message in reason and f"orders{ENV} in {PRIMARY}" in reason

    def test_a_service_that_is_not_there(self):
        fake = reconciled_fake()
        del fake.ecs_services[(PRIMARY, f"orders{ENV}")]
        assert "is not running in cluster" in reasons(check(fake), 9)[0]

    def test_a_service_that_is_inactive(self):
        fake = reconciled_fake()
        fake.add_ecs_service("orders", status="INACTIVE")
        assert "is not running in cluster" in reasons(check(fake), 9)[0]

    def test_a_service_with_no_strategy_is_fine_when_its_launch_type_is_fargate(self):
        fake = reconciled_fake()
        fake.add_ecs_service("orders", providers=())
        fake.ecs_services[(PRIMARY, f"orders{ENV}")]["launchType"] = "FARGATE"
        assert check(fake).passed

    def test_a_service_with_neither_a_strategy_nor_a_launch_type_is_refused(self):
        fake = reconciled_fake()
        fake.add_ecs_service("orders", providers=())
        (reason,) = reasons(check(fake), 9)
        assert "is not on on-demand Fargate only (no capacity provider or launch type)" in reason

    def test_the_cluster_and_task_definition_come_from_the_stack_and_the_service(self):
        fake = reconciled_fake()
        check(fake)
        describe = fake.calls_of("describe-services")[0]
        assert describe["cluster"] == CLUSTER and describe["services"] == [f"orders{ENV}"]
        assert fake.calls_of("describe-task-definition")[0]["task_definition"].endswith(f"apps{ENV}-orders{ENV}:7")

    def test_an_error_reading_ecs_is_a_refusal_not_a_crash(self):
        fake = reconciled_fake()
        fake.fail_on("ecs", "describe-services", "An error occurred (AccessDeniedException): no")
        (reason,) = reasons(check(fake), 9)
        assert reason.startswith("could not check the service's configuration") and "AccessDeniedException" in reason
