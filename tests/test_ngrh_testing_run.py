"""Step 7: running a test, stopping it and reporting on it (design 5.9, 6.2 and section 7).

The polling runs against a fake clock, so a run that never ends is a loop over simulated minutes, not a wait.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from ngrh_scenario import NAME, OBSERVABILITY, PEER_DEGRADED, STOP, SUCCESS, TEMPLATE, deployed_fake, reconciled_fake  # noqa: E402
from ngrh_fake_aws import ENV, PRIMARY, STANDBY, alarm_arn  # noqa: E402

from ngrh_testing import cli, report, run  # noqa: E402

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

def stop_cli(fake, test=NAME):
    return cli.main(["stop", *ARGS, "--test", test], aws=fake)


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
        assert len(data["sources"]) == 5 and set(data["sourceEvents"]) == set(alarm_arn(n) for n in SUCCESS + OBSERVABILITY)
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
