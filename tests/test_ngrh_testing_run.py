"""Step 7: running a test, stopping it and reporting on it (design 5.9, 6.2 and section 7).

The polling runs against a fake clock, so a run that never ends is a loop over simulated minutes, not a wait.
"""

import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from ngrh_scenario import (  # noqa: E402
    NAME, OBSERVABILITY, PEER_DEGRADED, STOP, SUCCESS, TEMPLATE, deployed_fake, environment, reconciled_fake, spec_tests,
)
from ngrh_fake_aws import BROKER_ID, ENV, PRIMARY, STANDBY, alarm_arn  # noqa: E402

from ngrh_testing import cli, context, report, run  # noqa: E402
from ngrh_testing.context import ResolvedAlarm  # noqa: E402

ARGS = ["--primary-region", PRIMARY, "--standby-region", STANDBY, f"--env={ENV}"]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def history(old, new, at):
    return {"Timestamp": at, "HistoryItemType": "StateUpdate", "HistorySummary": f"Alarm updated from {old} to {new}",
            "HistoryData": json.dumps({"oldState": {"stateValue": old}, "newState": {"stateValue": new}})}


def source_event(name, old, new, at):
    arn = alarm_arn(name)
    return arn, {"timestamp": at, "sourceArn": arn, "eventType": "ALARM", "detail": {"alarmStateChange": {"state": new, "previousState": old, "reason": "r"}}}


def ready_to_run():
    """A reconciled deployment where the run will go INITIALIZING, RUNNING, FAILED, with the evidence a report shows."""
    fake = reconciled_fake()
    fake.fis[PRIMARY].append({"id": "EXP111", "state": {"status": "completed", "reason": "Experiment completed"}})
    fake.run_events = [
        {"eventId": "e1", "eventType": "TEST_RUN_STARTED", "message": "Test run started", "timestamp": "2026-10-06T16:00:05+00:00"},
        {"eventId": "e2", "eventType": "TEST_RUN_FAILED", "message": "A success alarm went to ALARM", "timestamp": "2026-10-06T16:04:00+00:00"},
    ]
    for name, old, new, at in ((SUCCESS[0], "OK", "ALARM", "2026-10-06T16:03:30+00:00"), (OBSERVABILITY[0], "OK", "ALARM", "2026-10-06T16:02:30+00:00")):
        arn, event = source_event(name, old, new, at)
        fake.run_source_events[arn] = [event]
    fake.resolved_targets = [{"resourceType": "AWS::ECS::Task", "targetName": "orders-task-1", "targetInformation": {}}]
    fake.dependencies = [{"dependencyName": "broker", "dnsName": "b-3d43.mq.us-east-1.on.aws", "criticality": "HARD", "source": "MANUAL"}]
    fake.alarm_history[(PRIMARY, "hop-orders-slow-us-east-1-t")] = [history("OK", "ALARM", "2026-10-06T16:02:10+00:00"), history("ALARM", "OK", "2026-10-06T16:12:10+00:00")]
    fake.alarm_history[(PRIMARY, "hop-checkout-errors-us-east-1-t")] = [history("OK", "ALARM", "2026-10-06T16:02:40+00:00")]
    fake.alarm_history[(PRIMARY, "hop-ui-slow-us-east-1-t")] = [history("OK", "ALARM", "2026-10-06T16:02:20+00:00")]
    fake.alarm_history[(PRIMARY, "hop-ui-errors-us-east-1-t")] = []
    fake.alarm_history[(STANDBY, PEER_DEGRADED)] = [history("OK", "ALARM", "2026-10-06T16:05:00+00:00")]   # a composite alarm
    return fake


def run_cli(fake, tmp_path, *extra, test=NAME, clock=None, reports=True):
    clock = clock or Clock()
    tail = ["--reports-dir", str(tmp_path)] if reports else []
    return cli.main(["run", *ARGS, "--test", test, *tail, *extra], aws=fake, sleep=clock.sleep, clock=clock), clock


# --- waiting for alarm data ------------------------------------------------------------------------------

def resolved(fake):
    return context.resolve_all(fake, environment(fake), spec_tests())[0]


class WarmingClock(Clock):
    """A clock whose sleeps also give alarms their data: each (Region, name) turns OK at the second given."""

    def __init__(self, fake, warm_at):
        super().__init__()
        self.fake, self.warm_at = fake, warm_at

    def sleep(self, seconds):
        super().sleep(seconds)
        for key, at in self.warm_at.items():
            if self.now >= at:
                self.fake.alarms[key]["state"] = "OK"


def wait_for(fake, minutes=15, clock=None, lines=None, test=None):
    clock = clock or Clock()
    found = run.wait_for_alarm_data(fake, test or resolved(fake), minutes, progress=(lines if lines is not None else []).append,
                                    sleep=clock.sleep, clock=clock)
    return found, clock


class TestWaitForAlarmData:

    def test_alarms_that_have_data_are_not_waited_for(self):
        found, clock = wait_for(reconciled_fake())
        assert found == [] and clock.now == 0

    def test_it_waits_until_the_alarms_have_data_and_says_so_once(self):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, SUCCESS[0])]["state"] = "INSUFFICIENT_DATA"
        lines = []
        found, clock = wait_for(fake, clock=WarmingClock(fake, {(PRIMARY, SUCCESS[0]): 100}), lines=lines)
        assert found == []
        assert clock.now == 120                                    # polled every 30 s; the data arrived during the fourth sleep
        assert lines == [f"1 alarm(s) have no data yet: {SUCCESS[0]} ({PRIMARY})"]

    def test_it_gives_up_at_the_time_limit_and_names_what_is_still_without_data(self):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, SUCCESS[1])]["state"] = "INSUFFICIENT_DATA"
        found, clock = wait_for(fake, minutes=10)
        assert found == [f"{SUCCESS[1]} ({PRIMARY})"]
        assert clock.now == 600

    def test_the_stop_alarm_is_waited_for_too(self):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, STOP)]["state"] = "INSUFFICIENT_DATA"
        found, clock = wait_for(fake, clock=WarmingClock(fake, {(PRIMARY, STOP): 45}))
        assert found == [] and clock.now == 60

    def test_alarms_in_both_regions_are_looked_at(self):
        fake = reconciled_fake()
        t = resolved(fake)
        t = dataclasses.replace(t, stop=[*t.stop, ResolvedAlarm(PEER_DEGRADED, STANDBY, alarm_arn(PEER_DEGRADED))])
        fake.alarms[(STANDBY, PEER_DEGRADED)]["state"] = "INSUFFICIENT_DATA"
        found, _ = wait_for(fake, minutes=1, test=t)
        assert found == [f"{PEER_DEGRADED} ({STANDBY})"]

    def test_observability_alarms_are_not_waited_for(self):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, OBSERVABILITY[0])]["state"] = "INSUFFICIENT_DATA"
        found, clock = wait_for(fake)
        assert found == [] and clock.now == 0

    def test_an_alarm_already_in_alarm_is_not_waited_out(self):
        fake = reconciled_fake()
        fake.alarms[(PRIMARY, SUCCESS[0])]["state"] = "ALARM"
        found, clock = wait_for(fake)
        assert found == [] and clock.now == 0                      # waiting will not change it; preflight refuses

    def test_an_alarm_that_does_not_exist_is_not_waited_for(self):
        fake = reconciled_fake()
        del fake.alarms[(PRIMARY, SUCCESS[0])]
        found, clock = wait_for(fake)
        assert found == [] and clock.now == 0                      # preflight says it does not exist


class TestWaitBeforeTheRun:

    def test_the_run_waits_for_alarm_data_before_its_preflight(self, tmp_path, capsys):
        fake = ready_to_run()
        fake.alarms[(PRIMARY, SUCCESS[0])]["state"] = "INSUFFICIENT_DATA"
        clock = WarmingClock(fake, {(PRIMARY, SUCCESS[0]): 100})
        code, clock = run_cli(fake, tmp_path, "--alarm-wait-minutes", "15", clock=clock)
        out = capsys.readouterr().out
        assert code == 0
        assert "Waiting up to 15 min for the success and stop alarms of orders-broker-dependency to have data." in out
        assert out.index("Waiting up to 15 min") < out.index("Preflight (live)") < out.index("Started run")
        assert clock.now == 120 + 90                               # the wait, then the three polls of the run
        assert len(fake.calls_of("start-test-run")) == 1

    def test_alarms_that_never_get_data_leave_the_refusal_to_preflight(self, tmp_path, capsys):
        fake = ready_to_run()
        fake.alarms[(PRIMARY, SUCCESS[0])]["state"] = "INSUFFICIENT_DATA"
        code, clock = run_cli(fake, tmp_path, "--alarm-wait-minutes", "15")
        out = capsys.readouterr().out
        assert code == cli.EXIT_REFUSED
        assert clock.now == 900
        assert f"After 15 min these alarms still have no data: {SUCCESS[0]} ({PRIMARY})." in out
        assert f"[4] orders-broker-dependency: success alarm {SUCCESS[0]} is INSUFFICIENT_DATA, not OK, in {PRIMARY}" in out
        assert fake.calls_of("start-test-run") == []

    def test_without_the_option_nothing_is_waited_for(self, tmp_path, capsys):
        fake = ready_to_run()
        fake.alarms[(PRIMARY, SUCCESS[0])]["state"] = "INSUFFICIENT_DATA"
        code, clock = run_cli(fake, tmp_path)
        assert code == cli.EXIT_REFUSED and clock.now == 0
        assert "Waiting up to" not in capsys.readouterr().out

    def test_a_test_that_does_not_resolve_is_left_to_preflight(self, tmp_path, capsys):
        fake = ready_to_run()
        fake.brokers[BROKER_ID] = []
        code, clock = run_cli(fake, tmp_path, "--alarm-wait-minutes", "15")
        out = capsys.readouterr().out
        assert code == cli.EXIT_REFUSED and clock.now == 0
        assert "Waiting up to" not in out and "[10]" in out


# --- the run ---------------------------------------------------------------------------------------------

class TestRun:

    def test_the_run_is_followed_to_its_end_and_the_report_written(self, tmp_path, capsys):
        fake = ready_to_run()
        code, clock = run_cli(fake, tmp_path)
        out = capsys.readouterr().out
        assert code == 0                                                      # FAILED is what the spec expects
        assert "Started run run-0002 of orders-broker-dependency, INITIALIZING; waiting up to 35 min (the test's 15 min plus 20)." in out
        assert [l.split(": ")[1].split(" after")[0] for l in out.splitlines() if " after " in l] == ["INITIALIZING", "RUNNING", "FAILED"]
        assert "orders-broker-dependency: FAILED. Expected FAIL, observed FAIL: as expected." in out
        assert clock.now == 90                                                # three polls apart, 30 s each
        (started,) = fake.calls_of("start-test-run")
        (test_id,) = fake.tests
        assert started == {"service_arn": fake.tests[test_id]["serviceArn"], "test_id": test_id}
        assert sorted(p.name for p in tmp_path.iterdir()) == ["orders-broker-dependency-run-0002.json", "orders-broker-dependency-run-0002.md"]

    def test_preflight_runs_first_and_a_refusal_starts_nothing(self, tmp_path, capsys):
        fake = ready_to_run()
        fake.runs["run-foreign"] = {"testRunId": "run-foreign", "testId": "t", "status": "RUNNING", "startedAt": "2026-10-06T15:00:00+00:00",
                                    "serviceArn": "arn:aws:resiliencehub:us-east-1:111111111111:service/tester-id", "testTemplateArn": "x",
                                    "script": ["RUNNING"], "polls": 0}
        code, _ = run_cli(fake, tmp_path)
        assert code == cli.EXIT_REFUSED
        assert "[5] service tester-id has test run run-foreign RUNNING" in capsys.readouterr().out
        assert fake.calls_of("start-test-run") == [] and list(tmp_path.iterdir()) == []

    def test_a_verdict_other_than_the_one_expected_exits_3(self, tmp_path, capsys):
        fake = ready_to_run()
        fake.run_script = ["RUNNING", "PASSED"]
        code, _ = run_cli(fake, tmp_path)
        assert code == cli.EXIT_FOUND
        assert "Expected FAIL, observed PASS: NOT as expected." in capsys.readouterr().out

    @pytest.mark.parametrize("status", ["ERROR", "STOPPED"])
    def test_a_run_that_did_not_finish_is_not_a_verdict(self, tmp_path, capsys, status):
        fake = ready_to_run()
        fake.run_script = ["RUNNING", status]
        code, _ = run_cli(fake, tmp_path)
        assert code == cli.EXIT_FOUND
        assert f"observed INCONCLUSIVE: NOT as expected" in capsys.readouterr().out

    def test_a_run_that_outlasts_its_budget_is_reported_as_it_stands(self, tmp_path, capsys):
        fake = ready_to_run()
        fake.run_script = ["RUNNING"]
        code, clock = run_cli(fake, tmp_path)
        captured = capsys.readouterr()
        assert code == cli.EXIT_ERROR
        assert clock.now == 35 * 60                                            # the duration plus 20 minutes
        assert "gave up waiting after 35 min; the run is still RUNNING" in captured.err
        assert f"make ngrh-test-stop TEST={NAME}" in captured.err and f"make ngrh-test-report TEST={NAME} RUN=run-0002" in captured.err
        assert len(list(tmp_path.glob("*.md"))) == 1                            # what is known was written
        assert fake.calls_of("stop-test-run") == []                            # and nothing was stopped for the caller

    def test_ctrl_c_leaves_the_run_going_and_says_how_to_stop_it(self, tmp_path, capsys):
        fake = ready_to_run()
        clock = Clock()

        def interrupted(seconds):
            raise KeyboardInterrupt

        code = cli.main(["run", *ARGS, "--test", NAME, "--reports-dir", str(tmp_path)], aws=fake, sleep=interrupted, clock=clock)
        out = capsys.readouterr().out
        assert code == cli.EXIT_INTERRUPTED
        assert "Interrupted. The run goes on in AWS." in out and f"make ngrh-test-stop TEST={NAME}" in out
        assert f"make ngrh-test-report TEST={NAME} RUN=run-0002" in out
        assert fake.calls_of("stop-test-run") == []

    def test_losing_the_credentials_mid_run_says_the_run_goes_on(self, tmp_path, capsys):
        fake = ready_to_run()
        fake.fail_on("resiliencehubv2", "get-test-run", "An error occurred (ExpiredTokenException): The security token is expired")
        code, _ = run_cli(fake, tmp_path)
        err = capsys.readouterr().err
        assert code == cli.EXIT_ERROR
        assert "lost contact with AWS while waiting" in err and "ExpiredToken" in err
        assert "The run goes on in AWS." in err and "make ngrh-test-report" in err
        assert fake.calls_of("stop-test-run") == []

    def test_the_poll_interval_is_the_one_asked_for(self, tmp_path):
        fake = ready_to_run()
        _, clock = run_cli(fake, tmp_path, "--poll-seconds", "10")
        assert clock.now == 30

    def test_the_budget_is_the_tests_duration_plus_twenty_minutes(self):
        assert run.GRACE_MINUTES == 20
        fake = ready_to_run()
        from ngrh_scenario import environment, spec_tests
        from ngrh_testing import context
        (resolved,) = context.resolve_all(fake, environment(fake), spec_tests())
        assert run.budget_seconds(resolved) == (15 + 20) * 60

    def test_all_is_not_a_test_to_run(self, tmp_path, capsys):
        code, _ = run_cli(ready_to_run(), tmp_path, test="all")
        assert code == cli.EXIT_ERROR and "name one test to run" in capsys.readouterr().err

    def test_a_test_that_is_not_in_the_spec_is_an_error(self, tmp_path, capsys):
        code, _ = run_cli(ready_to_run(), tmp_path, test="nope")
        assert code == cli.EXIT_ERROR and "no test named 'nope'" in capsys.readouterr().err

    def test_a_service_that_refuses_a_second_active_run_is_reported(self, tmp_path, capsys):
        fake = ready_to_run()
        fake.fail_on("resiliencehubv2", "start-test-run", "An error occurred (ConflictException): An active test run already exists for this service.")
        code, _ = run_cli(fake, tmp_path)
        assert code == cli.EXIT_ERROR and "An active test run already exists" in capsys.readouterr().err


# --- stop --------------------------------------------------------------------------------------------------

def stop_cli(fake, test=NAME, *extra, clock=None):
    clock = clock or Clock()
    return cli.main(["stop", *ARGS, "--test", test, *extra], aws=fake, sleep=clock.sleep, clock=clock)


class TestStopWaiting:

    def _running(self):
        fake = reconciled_fake()
        (test_id,) = fake.tests
        return fake, fake.add_run("orders", test_id, "RUNNING")

    def test_it_waits_for_the_run_to_end_when_asked(self, capsys):
        fake, run_id = self._running()
        fake.stop_script = ["STOPPING", "STOPPING", "STOPPED"]
        clock = Clock()
        assert stop_cli(fake, NAME, "--wait-minutes", "5", clock=clock) == 0
        assert f"{NAME}: run {run_id} is STOPPED" in capsys.readouterr().out
        assert clock.now == 60                                     # two polls still STOPPING, 30 s apart

    def test_it_says_so_when_the_run_is_still_stopping_at_the_time_limit(self, capsys):
        fake, run_id = self._running()
        fake.stop_script = ["STOPPING"]
        clock = Clock()
        assert stop_cli(fake, NAME, "--wait-minutes", "2", clock=clock) == 0
        assert f"{NAME}: run {run_id} is still STOPPING after 2 min" in capsys.readouterr().out
        assert clock.now == 120

    def test_it_does_not_wait_unless_asked(self):
        fake, _ = self._running()
        clock = Clock()
        assert stop_cli(fake, clock=clock) == 0
        assert fake.calls_of("get-test-run") == [] and clock.now == 0

    def test_a_test_with_no_active_run_has_nothing_to_wait_for(self, capsys):
        fake = reconciled_fake()
        clock = Clock()
        assert stop_cli(fake, NAME, "--wait-minutes", "10", clock=clock) == 0
        assert "the test has no active run" in capsys.readouterr().out
        assert clock.now == 0 and fake.calls_of("get-test-run") == []


class TestStop:

    def test_the_tests_active_run_is_asked_to_stop(self, capsys):
        fake = reconciled_fake()
        (test_id,) = fake.tests
        run_id = fake.add_run("orders", test_id, "RUNNING")
        assert stop_cli(fake) == 0
        assert fake.calls_of("stop-test-run") == [{"service_arn": fake.tests[test_id]["serviceArn"], "test_run_id": run_id}]
        assert f"asked run {run_id} to stop; it is STOPPING" in capsys.readouterr().out

    def test_a_test_with_no_active_run_has_nothing_to_stop(self, capsys):
        fake = reconciled_fake()
        (test_id,) = fake.tests
        fake.add_run("orders", test_id, "PASSED")
        assert stop_cli(fake) == 0
        assert fake.writes() == [] and "the test has no active run" in capsys.readouterr().out

    def test_a_run_of_another_test_on_the_service_is_left_alone(self):
        fake = reconciled_fake()
        other = fake.add_test("orders", "aws-multi-region-isolation:rtmr001")
        fake.add_run("orders", other, "RUNNING")
        assert stop_cli(fake) == 0
        assert fake.writes() == []

    def test_a_test_that_does_not_exist_has_no_run(self, capsys):
        fake = deployed_fake()
        assert stop_cli(fake) == 0
        assert "the test does not exist, so it has no run to stop" in capsys.readouterr().out

    def test_the_test_must_be_named(self, capsys):
        assert stop_cli(reconciled_fake(), test="all") == cli.EXIT_ERROR
        assert "name one test" in capsys.readouterr().err
        with pytest.raises(SystemExit):
            cli.main(["stop", *ARGS], aws=reconciled_fake())


# --- report ---------------------------------------------------------------------------------------------------

def report_cli(fake, tmp_path, *extra):
    return cli.main(["report", *ARGS, "--test", NAME, "--reports-dir", str(tmp_path), *extra], aws=fake)


def collected(fake, status="FAILED"):
    (test_id,) = fake.tests
    run_id = fake.add_run("orders", test_id, status)
    from ngrh_scenario import environment, spec_tests
    from ngrh_testing import context
    (resolved,) = context.resolve_all(fake, environment(fake), spec_tests())
    return report.collect(fake, environment(fake), resolved, run_id, datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc))


class TestReport:

    def test_what_the_run_recorded_is_collected(self):
        fake = ready_to_run()
        data = collected(fake)
        assert (data["test"], data["service"], data["template"]) == (NAME, "orders", TEMPLATE)
        assert (data["expected"], data["observed"], data["matches"]) == ("FAIL", "FAIL", True)
        assert data["testRun"]["status"] == "FAILED" and data["testRun"]["parameters"]["duration"] == ["15"]
        assert [e["eventType"] for e in data["events"]] == ["TEST_RUN_STARTED", "TEST_RUN_FAILED"]
        assert len(data["sources"]) == len(SUCCESS + OBSERVABILITY) and set(data["sourceEvents"]) == set(alarm_arn(n) for n in SUCCESS + OBSERVABILITY)
        assert data["resolvedTargets"][0]["targetName"] == "orders-task-1"
        assert data["dependencies"][0]["dnsName"] == "b-3d43.mq.us-east-1.on.aws"
        assert data["experiments"][0]["experiment"]["state"]["status"] == "completed"
        assert data["gaps"] == [] and data["generatedAt"] == "2026-10-06T17:00:00Z"

    def test_evidence_is_laid_out_region_health_first_then_ui_then_each_back_end_errors_before_slow(self):
        data = collected(ready_to_run())
        assert [e["alarm"] for e in data["evidence"]] == [
            f"region-degraded-{STANDBY}{ENV}",
            *[f"hop-{s}-{k}-{PRIMARY}{ENV}" for s in ("ui", "catalog", "carts", "checkout", "orders") for k in ("errors", "slow")],
        ]
        assert [e["hop"] for e in data["evidence"]][:4] == [None, "ui", "ui", "catalog"]

    def test_a_composite_alarms_history_is_asked_for(self):
        fake = ready_to_run()
        data = collected(fake)
        peer = next(e for e in data["evidence"] if e["alarm"] == PEER_DEGRADED)
        assert [(t["from"], t["to"]) for t in peer["transitions"]] == [("OK", "ALARM")]
        assert all(c["alarm_types"] == ["MetricAlarm", "CompositeAlarm"] for c in fake.calls_of("describe-alarm-history"))

    def test_the_history_window_runs_from_a_minute_before_the_start_to_ten_after_the_end(self):
        fake = ready_to_run()
        collected(fake)
        window = {(c["start_date"], c["end_date"]) for c in fake.calls_of("describe-alarm-history")}
        assert window == {("2026-10-06T15:59:00Z", "2026-10-06T16:30:00Z")}            # started 16:00, ended 16:20

    def test_the_markdown_leads_with_the_verdict_and_lays_out_the_evidence(self):
        text = report.render(collected(ready_to_run()), "ngrh-invoker-t")
        lines = text.splitlines()
        assert lines[0] == "# orders-broker-dependency: FAILED"
        assert lines[2] == "Expected FAIL, observed FAIL: as expected."
        assert "| journey-lcl-orders-us-east-1-t | success | FAILED | alarm went to ALARM |" in text
        assert "| hop-orders-slow-us-east-1-t | observability | - |  |" in text
        order = [text.index(h) for h in ("### Region health (us-west-2)", "### ui (us-east-1)", "### catalog (us-east-1)", "### carts (us-east-1)",
                                          "### checkout (us-east-1)", "### orders (us-east-1)")]
        assert order == sorted(order)
        assert "- hop-orders-slow-us-east-1-t: 16:02:10 OK -> ALARM" in text and "- hop-orders-slow-us-east-1-t: 16:12:10 ALARM -> OK" in text
        assert "- hop-ui-errors-us-east-1-t: no state change" in text
        assert "- region-degraded-us-west-2-t: 16:05:00 OK -> ALARM" in text

    def test_the_timeline_merges_run_events_and_alarm_changes_in_time_order(self):
        text = report.render(collected(ready_to_run()))
        timeline = text.split("## Timeline (UTC)")[1].split("## Evidence")[0].strip().splitlines()
        assert timeline == [
            "- 16:00:05 TEST_RUN_STARTED: Test run started",
            f"- 16:02:30 {OBSERVABILITY[0]}: OK -> ALARM",
            f"- 16:03:30 {SUCCESS[0]}: OK -> ALARM",
            "- 16:04:00 TEST_RUN_FAILED: A success alarm went to ALARM",
        ]

    def test_the_fault_and_the_blocked_dependency_are_shown(self):
        text = report.render(collected(ready_to_run()))
        assert "## What the fault reached (1 resolved target(s))" in text and "- AWS::ECS::Task orders-task-1" in text
        assert "- b-3d43.mq.us-east-1.on.aws (HARD, MANUAL)" in text
        assert "- EXP111: completed (Experiment completed)" in text

    def test_a_run_that_ended_in_error_without_a_message_points_at_cloudtrail(self):
        fake = ready_to_run()
        text = report.render(collected(fake, "ERROR"), "ngrh-invoker-t")
        assert "No error message came with the run." in text and "by role ngrh-invoker-t" in text and "CloudTrail" in text
        assert "Expected FAIL, observed INCONCLUSIVE: NOT as expected." in text

    def test_the_runs_own_error_message_is_shown(self):
        fake = ready_to_run()
        fake.run_error = "The invoker role cannot assume the experiment role"
        fake.run_script = ["ERROR"]
        text = report.render(collected(fake, "ERROR"))
        assert "The run reported an error: The invoker role cannot assume the experiment role" in text
        assert "CloudTrail" not in text

    def test_a_part_that_cannot_be_read_is_listed_and_the_rest_still_written(self, tmp_path, capsys):
        fake = ready_to_run()
        (test_id,) = fake.tests
        fake.add_run("orders", test_id, "FAILED")
        fake.fail_on("resiliencehubv2", "list-resolved-test-run-target-resources", "An error occurred (AccessDeniedException): no")
        assert report_cli(fake, tmp_path) == 0
        data = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert data["resolvedTargets"] == [] and data["events"] != []
        assert data["gaps"] and "resolved targets" in data["gaps"][0] and "AccessDeniedException" in data["gaps"][0]
        assert "## Not collected" in next(tmp_path.glob("*.md")).read_text()

    def test_a_history_that_cannot_be_read_is_a_gap_not_a_crash(self):
        fake = ready_to_run()
        fake.fail_on("cloudwatch", "describe-alarm-history", "An error occurred (AccessDeniedException): no")
        data = collected(fake)
        assert any(g.startswith("history of ") for g in data["gaps"])

    def test_the_latest_run_is_the_default_and_a_named_run_can_be_asked_for(self, tmp_path, capsys):
        fake = ready_to_run()
        (test_id,) = fake.tests
        older = fake.add_run("orders", test_id, "PASSED", started="2026-10-05T09:00:00+00:00")
        newer = fake.add_run("orders", test_id, "FAILED", started="2026-10-06T09:00:00+00:00")
        assert report_cli(fake, tmp_path) == 0
        assert f"run {newer}: FAILED" in capsys.readouterr().out
        assert report_cli(fake, tmp_path, "--run", older) == 0
        assert f"run {older}: PASSED" in capsys.readouterr().out
        assert sorted(p.name for p in tmp_path.glob("*.md")) == sorted([f"{NAME}-{newer}.md", f"{NAME}-{older}.md"])

    def test_a_test_with_no_runs_has_no_report(self, tmp_path, capsys):
        assert report_cli(reconciled_fake(), tmp_path) == cli.EXIT_ERROR
        assert "the test has no runs yet" in capsys.readouterr().err

    def test_a_test_that_does_not_exist_has_no_report(self, tmp_path, capsys):
        assert report_cli(deployed_fake(), tmp_path) == cli.EXIT_ERROR
        assert "the test does not exist, so it has no runs" in capsys.readouterr().err


class TestVerdicts:

    @pytest.mark.parametrize("status,seen", [("PASSED", "PASS"), ("FAILED", "FAIL"), ("STOPPED", "INCONCLUSIVE"), ("ERROR", "INCONCLUSIVE"),
                                             ("RUNNING", "INCONCLUSIVE")])
    def test_a_status_amounts_to_a_verdict_only_when_the_run_finished_its_test(self, status, seen):
        assert report.observed(status) == seen

    @pytest.mark.parametrize("expected,seen,ok", [("PASS", "PASS", True), ("PASS", "FAIL", False), ("FAIL", "FAIL", True), ("FAIL", "PASS", False),
                                                  ("PASS", "INCONCLUSIVE", False), ("UNKNOWN", "PASS", True), ("UNKNOWN", "FAIL", True),
                                                  ("UNKNOWN", "INCONCLUSIVE", False)])
    def test_matching_the_expectation(self, expected, seen, ok):
        assert report.matches(expected, seen) is ok

    def test_times_are_read_as_utc_whatever_their_form(self):
        assert report.parse_time("2026-10-06T16:00:00Z") == datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)
        assert report.parse_time("2026-10-06T12:00:00-04:00") == datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)
        assert report.parse_time("2026-10-06T16:00:00") == datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)
        assert report.parse_time(1791302400) == datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)


# --- a fault that never ran ------------------------------------------------------------------------------
#
# 2026-10-07, run f3ecf7f7 of this test in test2: the report said "FAILED. Expected FAIL, observed FAIL: as
# expected." although FIS had refused the ECS packet-loss action (no task of orders was registered with SSM) and
# nothing was blocked. Resilience Hub ends a run FAILED in that case too. These are the shapes that run returned.

SSM_FIS_REASON = ("At least one ECS Task is not registered as a SSM managed instance. SSM Agent must be running as a "
                  "sidecar in the task, and the task must be registered within Systems Manager as a managed instance.")
SSM_RUN_ERROR = "The targeted resources could not be used for the aws:ecs:task-network-packet-loss action. " + SSM_FIS_REASON
ACTION = "aws:ecs:task-network-packet-loss"


def action_event(kind, status, count="0", at="2026-10-06T16:00:30+00:00"):
    return {"eventId": f"{kind}-{status}", "eventType": kind, "message": f"The {ACTION} action status is '{status}'.", "timestamp": at,
            "attributes": {"actionId": "block-ecs-dependency-traffic", "actionName": ACTION, "status": status, "targetCount": count}}


def fault_refused(fake, run_error=SSM_RUN_ERROR, experiment=True, event=True):
    """The fault could not be injected: FIS ended the experiment `failed` with InvalidTarget, the action failed with no
    target, and Resilience Hub ended the run with an error message. Each mark can be left out."""
    if experiment:
        fake.fis[PRIMARY][0]["state"] = {"status": "failed", "reason": SSM_FIS_REASON,
                                         "error": {"code": "InvalidTarget", "location": "actions.block-ecs-dependency-traffic"}}
    fake.run_events = [action_event("action_scheduled", "scheduled")]
    if event:
        fake.run_events.append(action_event("action_failed", "failed", at="2026-10-06T16:00:57+00:00"))
    fake.run_events.append({"eventId": "end", "eventType": "test_run_ended", "message": "Test run completed with result: failure",
                            "timestamp": "2026-10-06T16:01:00+00:00", "attributes": {"result": "failure", "status": "failed"}})
    fake.run_error = run_error
    return fake


class TestAFaultThatNeverRan:

    @pytest.mark.parametrize("status", ["FAILED", "PASSED"])
    def test_a_run_whose_fault_was_refused_is_not_a_verdict_whatever_status_it_ended_in(self, status):
        data = collected(fault_refused(ready_to_run()), status)
        assert data["observed"] == "INCONCLUSIVE" and data["matches"] is False
        assert data["faultNotRun"] == SSM_RUN_ERROR

    def test_the_explanation_is_the_runs_own_message_then_fis_s_then_the_events(self):
        assert collected(fault_refused(ready_to_run()))["faultNotRun"] == SSM_RUN_ERROR
        assert collected(fault_refused(ready_to_run(), run_error=None))["faultNotRun"] == SSM_FIS_REASON
        only_the_event = fault_refused(ready_to_run(), run_error=None, experiment=False)
        assert collected(only_the_event)["faultNotRun"] == f"The {ACTION} action status is 'failed'."

    @pytest.mark.parametrize("marks", [{"experiment": True, "event": False}, {"experiment": False, "event": True}])
    def test_either_mark_alone_is_enough(self, marks):
        data = collected(fault_refused(ready_to_run(), run_error=None, **marks))
        assert data["observed"] == "INCONCLUSIVE" and data["faultNotRun"]

    def test_an_experiment_with_no_reason_still_counts(self):
        fake = fault_refused(ready_to_run(), run_error=None, event=False)
        fake.fis[PRIMARY][0]["state"] = {"status": "failed"}
        assert collected(fake)["faultNotRun"] == "FIS reported the experiment failed without saying why"

    def test_event_types_are_matched_in_any_case(self):
        fake = ready_to_run()
        fake.run_events = [action_event("ACTION_FAILED", "failed")]
        assert collected(fake)["observed"] == "INCONCLUSIVE"

    def test_a_fault_that_ran_and_was_stopped_by_the_stop_condition_is_still_a_verdict(self):
        fake = ready_to_run()
        fake.fis[PRIMARY][0]["state"] = {"status": "stopped", "reason": "Stop condition triggered"}
        data = collected(fake)
        assert (data["observed"], data["matches"], data["faultNotRun"]) == ("FAIL", True, None)

    def test_a_run_the_sample_failed_keeps_its_verdict(self):
        fake = ready_to_run()
        fake.run_events = [action_event("action_scheduled", "scheduled", "2"), action_event("action_completed", "completed", "2"),
                           {"eventId": "end", "eventType": "test_run_ended", "message": "Test run completed with result: failure",
                            "timestamp": "2026-10-06T16:04:00+00:00", "attributes": {"result": "failure", "status": "failed"}}]
        data = collected(fake)
        assert (data["observed"], data["matches"], data["faultNotRun"]) == ("FAIL", True, None)

    @pytest.mark.parametrize("status", ["ERROR", "STOPPED"])
    def test_a_run_that_did_not_finish_stays_inconclusive_for_its_own_reason(self, status):
        data = collected(ready_to_run(), status)
        assert (data["observed"], data["faultNotRun"]) == ("INCONCLUSIVE", None)

    def test_the_markdown_says_the_fault_did_not_run_before_anything_else(self):
        text = report.render(collected(fault_refused(ready_to_run())), "ngrh-invoker-t")
        lines = text.splitlines()
        assert lines[0] == "# orders-broker-dependency: INCONCLUSIVE, the fault did not run"
        assert lines[2] == "Expected FAIL, observed INCONCLUSIVE: NOT as expected."
        assert f"Resilience Hub ended the run FAILED, but the fault never ran, so nothing was done to the application and this says nothing about it: {SSM_RUN_ERROR}" in text
        assert "amazon-ssm-agent sidecar registers" in text and "aws ssm describe-instance-information" in text
        assert "The run reported an error" not in text and "No error message came" not in text
        assert f"- EXP111: failed ({SSM_FIS_REASON})" in text

    def test_an_explanation_from_fis_alone_needs_no_hunt_through_cloudtrail(self):
        text = report.render(collected(fault_refused(ready_to_run(), run_error=None)), "ngrh-invoker-t")
        assert f"this says nothing about it: {SSM_FIS_REASON}" in text
        assert "No error message came" not in text and "CloudTrail" not in text

    def test_another_reason_gets_no_sidecar_advice(self):
        fake = fault_refused(ready_to_run(), run_error="The experiment role cannot be assumed")
        fake.fis[PRIMARY][0]["state"] = {"status": "failed", "reason": "role"}
        text = report.render(collected(fake))
        assert "this says nothing about it: The experiment role cannot be assumed" in text
        assert "sidecar" not in text

    def test_a_run_error_that_is_not_the_explanation_is_still_shown(self):
        fake = fault_refused(ready_to_run(), run_error="Insufficient permissions to write the report")
        fake.run_events = [action_event("action_failed", "failed")]
        text = report.render(collected(fake))
        assert "this says nothing about it: Insufficient permissions to write the report" in text
        assert "The run reported an error" not in text   # it is the explanation, said once

    def test_an_ordinary_failed_run_is_headed_by_its_status_as_before(self):
        text = report.render(collected(ready_to_run()))
        assert text.splitlines()[0] == "# orders-broker-dependency: FAILED" and "never ran" not in text

    def test_the_run_command_says_so_and_exits_3(self, tmp_path, capsys):
        fake = fault_refused(ready_to_run())
        code, _ = run_cli(fake, tmp_path)
        out = capsys.readouterr().out
        assert code == cli.EXIT_FOUND
        assert "orders-broker-dependency: FAILED. Expected FAIL, observed INCONCLUSIVE: NOT as expected." in out
        assert f"The fault did not run, so this is not a verdict on the application: {SSM_RUN_ERROR}" in out
        data = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert (data["observed"], data["matches"], data["faultNotRun"]) == ("INCONCLUSIVE", False, SSM_RUN_ERROR)

    def test_the_run_command_says_nothing_extra_when_the_fault_ran(self, tmp_path, capsys):
        run_cli(ready_to_run(), tmp_path)
        assert "did not run" not in capsys.readouterr().out

    def test_the_report_command_applies_the_same_rule_to_a_run_collected_later(self, tmp_path, capsys):
        fake = fault_refused(ready_to_run())
        (test_id,) = fake.tests
        fake.add_run("orders", test_id, "FAILED")
        assert report_cli(fake, tmp_path) == 0
        data = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert data["observed"] == "INCONCLUSIVE" and data["faultNotRun"] == SSM_RUN_ERROR
        assert next(tmp_path.glob("*.md")).read_text().startswith("# orders-broker-dependency: INCONCLUSIVE, the fault did not run")
