"""The isolation test, ``cart-isolation`` (step 12, design 5.8 and appendix C), and the tool changes it needed.

The test cuts the primary Region off from the standby for 30 minutes and asks whether the primary keeps serving on its own.
Its carts table is a multi-Region strong consistency table, which loses its quorum when FIS pauses the replication, and carts
reads with strong consistency, so the test is expected to FAIL; when it does, the triggers should fail the primary over.
This file covers what is new in the tool: the spec entry and its schema rule, the two run checks (no plan execution started,
and the standby's health alarm stayed OK), preflight check 8 for a test that can fail a Region over, and the verdict of a run
whose test expects to fail. Everything runs against the in-memory fake of the AWS calls; the first live run is still to come.
"""

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from ngrh_scenario import (  # noqa: E402  (first: it puts the deployment directory on the path)
    DYNAMODB_ERRORS, ISOLATION, ISOLATION_DESIRED, ISOLATION_OBSERVABILITY, ISOLATION_SOURCES, ISOLATION_SUCCESS, JOURNEYS,
    LCL, PEER_DEGRADED, RMT, STOP, all_spec_tests, deployed_fake, environment, fully_reconciled_fake, isolation_tests,
    reconciled_fake,
)
from ngrh_fake_aws import CLUSTER, ENV, PRIMARY, STANDBY, default_triggers  # noqa: E402
from test_ngrh_testing_recovery import Clock, add_execution, execution, on_start, operations  # noqa: E402

from ngrh_testing import alarms, cli, context, executions, preflight, reconcile, report, spec  # noqa: E402

ARGS = ["--primary-region", PRIMARY, "--standby-region", STANDBY, f"--env={ENV}"]
NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
COLLECTED_AT = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
SPEC_FILE = Path(__file__).resolve().parent.parent / "deployment" / "ngrh-tests.json"

# The comment ARC gave the executions its triggers started in test4 (read 2026-10-08), with the description failover.yaml gives
# the trigger that deactivates the primary when its cart journey fails.
TRIGGER = ("Execution started by automated trigger [Deactivate us-east-1: its cart journey fails there, and us-west-2 sees it "
           "fail too while us-west-2 is healthy]")


# --- helpers ------------------------------------------------------------------------------------------------------

def history(old, new, at):
    return {"Timestamp": at, "HistoryItemType": "StateUpdate", "HistorySummary": f"Alarm updated from {old} to {new}",
            "HistoryData": json.dumps({"oldState": {"stateValue": old}, "newState": {"stateValue": new}})}


def isolation_resolved(fake=None):
    fake = fake or deployed_fake()
    (resolved,) = context.resolve_all(fake, environment(fake), isolation_tests())
    return resolved


def row(alarm, region, transitions):
    """An evidence row as report.collect builds it: the alarm, its Region and its state changes (None when unreadable)."""
    return {"alarm": alarm, "region": region, "hop": None, "transitions": transitions}


def change(old, new, at="2026-10-06T16:05:30+00:00"):
    return {"time": at, "from": old, "to": new, "summary": ""}


def summarized(**kwargs):
    return executions.summarize(execution(comment=TRIGGER, **kwargs))


def refusals(fake, number=8, mode=preflight.LIVE, now=NOW):
    result = preflight.run_checks(fake, environment(fake), isolation_tests(), mode, now)
    return [r for r in result.refusals if r.check == number]


def finished_run(fake, status="FAILED"):
    """A run of the isolation test that has ended with ``status``, 16:00 to 16:20."""
    resolved = isolation_resolved(fake)
    test_id = reconcile.find_test(fake, environment(fake), resolved)
    run_id = fake.add_run("cart", test_id, status=status)
    fake.runs[run_id]["endedAt"] = "2026-10-06T16:20:00+00:00"
    return run_id


def collected(fake, run_id):
    return report.collect(fake, environment(fake), isolation_resolved(fake), run_id, COLLECTED_AT)


# --- the spec entry ------------------------------------------------------------------------------------------------

class TestSpecEntry:

    def test_it_is_the_isolation_test_on_cart_with_the_primary_cut_off_from_the_standby(self):
        (test,) = isolation_tests()
        assert (test.service, test.template) == ("cart", spec.ISOLATION_TEMPLATE)
        assert test.parameters == {"isolatedRegion": ("primary",), "destinationRegion": ("standby",), "duration": ("30",)}
        assert test.duration_minutes == 30 and "dependencies" not in test.parameters, "nothing is dropped by DNS name"

    def test_it_expects_to_fail_because_the_carts_table_cannot_reach_its_quorum(self):
        # The table is MRSC (global-table.yml), FIS makes the isolated replica answer writes and strongly consistent
        # reads with HTTP 500 (AWS Database Blog, 2026-05-20), and carts reads with strong consistency. docs/ngrh-test-ground-truth.md.
        assert isolation_tests()[0].expected == "FAIL"

    def test_the_success_alarms_are_the_four_local_journeys_of_the_isolated_region(self):
        (test,) = isolation_tests()
        assert [(a.name, a.region) for a in test.success_alarms] == [(f"journey-lcl-{j}", "primary") for j in JOURNEYS]
        assert [(a.name, a.region) for a in test.observability_alarms] == [("hop-carts-errors", "primary")]
        assert len(test.success_alarms) + len(test.observability_alarms) == spec.MAX_SOURCES

    def test_the_stop_condition_ends_the_fault_when_a_local_journey_fails(self):
        assert [(a.name, a.region) for a in isolation_tests()[0].stop_alarms] == [("region-degraded", "primary")]

    def test_it_names_the_two_run_checks_in_the_order_the_report_lists_them(self):
        assert isolation_tests()[0].run_checks == ("no-plan-execution", "destination-stayed-ok")
        assert set(spec.KNOWN_RUN_CHECKS) == set(executions.CHECKS)

    def test_the_evidence_shows_every_input_of_the_triggers_that_deactivate_the_isolated_region_and_the_table(self):
        # A run that starts no failover has to show which of the three conditions was missing; the success alarms are
        # the primary's own views, the evidence holds the standby's view and the composites. And the table's alarm.
        shown = {(name, ref.region) for ref in isolation_tests()[0].evidence_alarms for name in ref.alarm_names()}
        for t in alarms.triggers("primary", "standby"):
            if t.region == PRIMARY:
                assert (f"journey-rmt-{t.journey}", "standby") in shown
        assert {("region-degraded", "primary"), ("region-degraded", "standby"), ("dynamodb-errors", "primary")} <= shown
        assert all(("hop-" + s + "-" + k, "primary") in shown for s in spec.HOP_SERVICES for k in spec.HOP_KINDS)

    def test_the_templates_that_can_end_with_a_failover_are_the_recovery_and_the_isolation_templates(self):
        assert spec.FAILOVER_TEMPLATES == (spec.RECOVERY_TEMPLATE, spec.ISOLATION_TEMPLATE)
        assert spec.ISOLATION_TEMPLATE == "aws-multi-region-isolation:rtmr001"
        assert {t: s.peer_parameter for t, s in spec.TEMPLATES.items()} == {
            "aws-dependency-validation:rtdep001": None, spec.ISOLATION_TEMPLATE: "destinationRegion",
            spec.RECOVERY_TEMPLATE: "recoveryRegion"}

    def test_every_template_that_can_fail_over_has_a_peer_region_to_read(self):
        for template in spec.FAILOVER_TEMPLATES:
            assert spec.TEMPLATES[template].peer_parameter in spec.TEMPLATES[template].parameters

    def test_it_resolves_to_what_resilience_hub_is_asked_for(self):
        resolved = isolation_resolved()
        assert resolved.parameters == ISOLATION_DESIRED["parameters"]
        assert resolved.service_arn == ISOLATION_DESIRED["service_arn"] and resolved.template_arn == ISOLATION_DESIRED["test_template_arn"]
        assert resolved.stop_conditions == ISOLATION_DESIRED["stop_conditions"]
        assert resolved.logging_configuration == ISOLATION_DESIRED["logging_configuration"]
        assert sorted(resolved.sources()) == ISOLATION_SOURCES
        assert resolved.fault_region == PRIMARY and resolved.peer_region == STANDBY

    def test_reconcile_creates_it_with_those_settings_and_a_second_reconcile_changes_nothing(self):
        fake = deployed_fake()
        _, lines = reconcile.reconcile(fake, environment(fake), isolation_tests())
        assert len(lines) == 1 and lines[0].startswith(f"{ISOLATION}: created test ") and lines[0].endswith(f"; sources +{len(ISOLATION_SOURCES)} -0")
        (create,) = fake.calls_of("create-test")
        assert create["parameters"] == ISOLATION_DESIRED["parameters"] and create["role_name"] == ISOLATION_DESIRED["role_name"]
        writes = len(fake.writes())
        _, again = reconcile.reconcile(fake, environment(fake), isolation_tests())
        assert "unchanged" in again[0] and len(fake.writes()) == writes

    def test_the_peer_of_a_recovery_test_is_where_the_traffic_goes_and_a_test_without_one_has_none(self):
        fake = fully_reconciled_fake()
        resolved = {t.name: t for t in context.resolve_all(fake, environment(fake), all_spec_tests())}
        assert resolved["catalog-recovery"].peer_region == STANDBY and resolved[ISOLATION].peer_region == STANDBY
        with pytest.raises(ValueError, match="has no second Region"):
            resolved["orders-broker-dependency"].peer_region


# --- the schema rule for the destination check ------------------------------------------------------------------------

def shipped():
    return json.loads(SPEC_FILE.read_text())


def isolation_entry(data):
    return next(t for t in data["tests"] if t["name"] == ISOLATION)


def problems_of(data):
    with pytest.raises(spec.SpecError) as e:
        spec.parse(data)
    return e.value.problems


class TestTheDestinationCheckNeedsItsAlarm:

    def test_the_shipped_spec_satisfies_it(self):
        assert "destination-stayed-ok" in isolation_entry(shipped())["runChecks"]
        assert spec.parse(shipped())

    def test_a_spec_that_does_not_list_the_destinations_health_alarm_is_refused_before_any_fault(self):
        data = shipped()
        entry = isolation_entry(data)
        entry["evidenceAlarms"] = [e for e in entry["evidenceAlarms"] if e != {"name": "region-degraded", "region": "standby"}]
        problems = problems_of(data)
        assert len(problems) == 1
        assert "run check destination-stayed-ok reads region-degraded in standby, so evidenceAlarms must list" in problems[0]
        assert '{"name": "region-degraded", "region": "standby"}' in problems[0]

    def test_the_isolated_regions_alarm_is_not_the_destinations(self):
        data = shipped()
        entry = isolation_entry(data)
        entry["evidenceAlarms"] = [e for e in entry["evidenceAlarms"] if e != {"name": "region-degraded", "region": "standby"}]
        entry["evidenceAlarms"].append({"name": "region-degraded", "region": "primary"})
        assert any("reads region-degraded in standby" in p for p in problems_of(data))

    def test_a_group_of_alarms_does_not_stand_in_for_the_alarm(self):
        data = shipped()
        entry = isolation_entry(data)
        entry["evidenceAlarms"] = [{"group": "hop", "region": "standby"}]
        assert any("reads region-degraded in standby" in p for p in problems_of(data))

    def test_a_template_with_no_destination_cannot_name_the_check(self):
        data = shipped()
        orders = next(t for t in data["tests"] if t["name"] == "orders-broker-dependency")
        orders["runChecks"] = ["destination-stayed-ok"]
        assert any("orders-broker-dependency" in p and "needs a destinationRegion parameter" in p for p in problems_of(data))

    def test_other_checks_ask_nothing_of_the_evidence(self):
        data = shipped()
        entry = isolation_entry(data)
        entry["runChecks"] = ["no-plan-execution"]
        entry["evidenceAlarms"] = []
        assert spec.parse(data)

    def test_naming_the_destination_by_its_region_instead_of_standby_still_has_to_match_the_evidence(self):
        data = copy.deepcopy(shipped())
        entry = isolation_entry(data)
        entry["parameters"]["destinationRegion"] = ["us-west-2"]
        assert any("reads region-degraded in us-west-2" in p for p in problems_of(data))
        entry["evidenceAlarms"] = [{"name": "region-degraded", "region": "us-west-2"}]
        assert spec.parse(data)

    def test_an_unknown_template_is_reported_as_that_and_does_not_add_a_second_problem_about_the_check(self):
        data = shipped()
        isolation_entry(data)["template"] = "aws-az-recovery:rtaz001"
        problems = problems_of(data)
        assert any("is not one the suite uses" in p for p in problems)
        assert not any("destination-stayed-ok" in p for p in problems)


# --- no-plan-execution --------------------------------------------------------------------------------------------------------

def no_plan(found, problems=()):
    return executions.no_plan_execution(isolation_resolved(), found, list(problems))


class TestNoPlanExecution:

    def test_nothing_started_passes_and_says_no_failover_started(self):
        result = no_plan([])
        assert result.name == "no-plan-execution" and result.passed
        assert result.detail == "no plan execution started during the run, so no failover started"

    def test_a_trigger_started_deactivate_fails_and_names_the_journey_that_failed_locally(self):
        result = no_plan([summarized()])
        assert not result.passed
        assert result.detail.startswith("a plan execution started during the run: execution exec-0001 (deactivate us-east-1, graceful, completed) at 16:04:10, ")
        assert "started by the trigger [Deactivate us-east-1: its cart journey fails there, and us-west-2 sees it fail too while us-west-2 is healthy]" in result.detail
        assert "If the isolated Region's own journeys failed, this is the plan doing its job and the trigger's description names the journey" in result.detail
        assert "if they stayed OK, nothing should have started it" in result.detail

    def test_several_executions_are_all_listed_with_their_count(self):
        result = no_plan([summarized(execution_id="a"), summarized(execution_id="b", state="failed", start="2026-10-06T16:05:00+00:00")])
        assert not result.passed and result.detail.startswith("2 plan executions started during the run: ")
        assert "execution a (" in result.detail and "execution b (deactivate us-east-1, graceful, failed) at 16:05:00" in result.detail

    def test_a_comment_that_is_not_a_triggers_is_quoted_as_it_stands_and_not_called_a_persons(self):
        result = no_plan([executions.summarize(execution(comment="Operator-started timed failover of us-east-1"))])
        assert 'started with the comment "Operator-started timed failover of us-east-1"' in result.detail
        assert "by the trigger" not in result.detail

    def test_an_execution_with_no_comment_says_so(self):
        result = no_plan([executions.summarize(execution(comment=None))])
        assert "started without a comment" in result.detail

    @pytest.mark.parametrize("action,region", [("deactivate", PRIMARY), ("deactivate", STANDBY), ("activate", PRIMARY), ("activate", STANDBY)])
    def test_any_execution_counts_whatever_it_did_and_to_which_region(self, action, region):
        result = no_plan([summarized(action=action, region=region)])
        assert not result.passed and f"({action} {region}, graceful, completed)" in result.detail

    @pytest.mark.parametrize("state", ["completed", "failed", "canceled", "inProgress", "pausedByFailedStep", "completedWithExceptions"])
    def test_whatever_state_it_is_in_it_started(self, state):
        assert not no_plan([summarized(state=state)]).passed

    def test_executions_that_could_not_be_read_make_the_answer_unknown_not_a_pass(self):
        result = no_plan([], problems=["could not list plan executions at the us-west-2 endpoint: AccessDenied"])
        assert not result.passed and result.detail.startswith("could not read the plan's executions, so this is not known:")
        assert "AccessDenied" in result.detail

    def test_unknown_stays_unknown_when_something_was_found_as_well(self):
        result = no_plan([summarized()], problems=["could not list plan executions at the us-east-1 endpoint: AccessDenied"])
        assert result.detail.startswith("could not read the plan's executions")

    def test_the_trigger_text_is_the_fixed_comment_and_a_description_in_square_brackets(self):
        # ARC's own wording, read in test4; a description that itself holds brackets or a line break is kept whole.
        assert executions.TRIGGER_COMMENT.match("Execution started by automated trigger [a [b]\nc]").group(1) == "a [b]\nc"
        assert executions.TRIGGER_COMMENT.match("Execution started by hand [a]") is None
        assert executions.TRIGGER_COMMENT.match("Execution started by automated trigger [a] and more") is None


# --- destination-stayed-ok ----------------------------------------------------------------------------------------------------

def destination(evidence):
    return executions.destination_stayed_ok(isolation_resolved(), [], [], evidence)


class TestDestinationStayedOk:

    def test_no_change_in_the_window_is_a_pass(self):
        result = destination([row(PEER_DEGRADED, STANDBY, [])])
        assert result.name == "destination-stayed-ok" and result.passed
        assert result.detail == f"{PEER_DEGRADED} stayed OK through the run and the window after it"

    def test_a_change_to_alarm_is_a_failure_that_names_when_and_why_it_matters(self):
        result = destination([row(PEER_DEGRADED, STANDBY, [change("OK", "ALARM", "2026-10-06T16:05:30+00:00"), change("ALARM", "OK", "2026-10-06T16:09:00+00:00")])])
        assert not result.passed
        assert result.detail.startswith(f"{PEER_DEGRADED} did not stay OK: OK -> ALARM at 16:05:30; ALARM -> OK at 16:09:00.")
        assert f"{STANDBY} is where the isolated Region's traffic would go" in result.detail

    def test_insufficient_data_is_not_ok(self):
        assert not destination([row(PEER_DEGRADED, STANDBY, [change("OK", "INSUFFICIENT_DATA")])]).passed

    def test_an_alarm_that_started_out_of_ok_did_not_stay_ok_even_if_it_recovered(self):
        result = destination([row(PEER_DEGRADED, STANDBY, [change("ALARM", "OK")])])
        assert not result.passed and "ALARM -> OK at 16:05:30" in result.detail

    def test_a_change_whose_state_could_not_be_read_is_not_taken_for_ok(self):
        assert not destination([row(PEER_DEGRADED, STANDBY, [change("?", "?")])]).passed

    def test_a_history_that_could_not_be_read_makes_it_unknown_not_a_pass(self):
        result = destination([row(PEER_DEGRADED, STANDBY, None)])
        assert not result.passed and result.detail == f"the history of {PEER_DEGRADED} could not be read, so this is not known"

    def test_a_history_that_was_never_collected_is_the_same(self):
        assert not destination([]).passed

    def test_it_reads_the_destinations_alarm_and_not_the_isolated_regions(self):
        # The isolated Region's own composite is expected to go to ALARM in this test; it is the stop condition.
        result = destination([row(STOP, PRIMARY, [change("OK", "ALARM")]), row(PEER_DEGRADED, STANDBY, [])])
        assert result.passed

    def test_the_same_name_in_another_region_is_not_the_destinations_alarm(self):
        assert not destination([row(PEER_DEGRADED, PRIMARY, [])]).passed

    def test_a_row_of_another_alarm_in_the_destination_is_not_the_alarms_history(self):
        other = row(f"journey-rmt-home-{STANDBY}{ENV}", STANDBY, [change("OK", "ALARM")])      # the cut turns this one red
        assert destination([other, row(PEER_DEGRADED, STANDBY, [])]).passed
        assert not destination([other]).passed, "unknown: the history of the alarm is not borrowed from the first row of the Region"

    def test_the_alarm_is_found_by_its_name_and_not_by_being_the_first_of_the_destinations_evidence(self):
        data = copy.deepcopy(shipped())
        entry = isolation_entry(data)
        entry["evidenceAlarms"] = [{"name": "journey-rmt-home", "region": "standby"}, *entry["evidenceAlarms"]]
        fake = deployed_fake()
        (test,) = spec.parse(data).select(ISOLATION)
        (resolved,) = context.resolve_all(fake, environment(fake), [test])
        assert executions.destination_alarm(resolved).name == PEER_DEGRADED

    def test_the_destination_of_the_test_is_the_one_read_not_a_fixed_region(self):
        fake = deployed_fake()
        fake.add_alarm("region-degraded-eu-west-1-t", "eu-west-1", kind="CompositeAlarm")
        data = copy.deepcopy(shipped())
        entry = isolation_entry(data)
        entry["parameters"]["destinationRegion"] = ["eu-west-1"]
        entry["evidenceAlarms"] = [{"name": "region-degraded", "region": "eu-west-1"}]
        (test,) = spec.parse(data).select(ISOLATION)
        (resolved,) = context.resolve_all(fake, environment(fake), [test])
        assert executions.destination_alarm(resolved).name == "region-degraded-eu-west-1-t"
        ok = executions.destination_stayed_ok(resolved, [], [], [row("region-degraded-eu-west-1-t", "eu-west-1", [])])
        assert ok.passed

    def test_a_test_with_no_destination_alarm_says_there_is_nothing_to_read(self):
        fake = fully_reconciled_fake()
        resolved = {t.name: t for t in context.resolve_all(fake, environment(fake), all_spec_tests())}
        assert executions.destination_alarm(resolved["orders-broker-dependency"]) is None
        assert executions.destination_alarm(resolved["catalog-recovery"]) is None, "it lists region-degraded of both Regions, but has no destination"
        result = executions.destination_stayed_ok(resolved["orders-broker-dependency"], [], [], [])
        assert not result.passed and "lists no region-degraded alarm for its destination Region" in result.detail

    def test_the_alarm_the_check_reads_is_the_standbys_composite(self):
        alarm = executions.destination_alarm(isolation_resolved())
        assert (alarm.name, alarm.region) == (PEER_DEGRADED, STANDBY)


class TestEvaluate:

    def test_both_checks_are_evaluated_in_the_specs_order_and_given_the_evidence(self):
        results = executions.evaluate(isolation_resolved(), [], [], [row(PEER_DEGRADED, STANDBY, [])])
        assert [(r.name, r.passed) for r in results] == [("no-plan-execution", True), ("destination-stayed-ok", True)]

    def test_without_evidence_the_destination_check_cannot_pass(self):
        assert [r.passed for r in executions.evaluate(isolation_resolved(), [], [])] == [True, False]

    def test_the_recovery_checks_still_take_no_evidence(self):
        fake = fully_reconciled_fake()
        resolved = {t.name: t for t in context.resolve_all(fake, environment(fake), all_spec_tests())}
        (result,) = executions.evaluate(resolved["catalog-recovery"], [summarized()], [])
        assert result.name == "deactivate-completed" and result.passed


# --- preflight ---------------------------------------------------------------------------------------------------------------------

class TestPreflight:

    def test_a_ready_deployment_passes_the_live_and_the_static_preflight(self):
        fake = fully_reconciled_fake()
        for mode in (preflight.LIVE, preflight.STATIC):
            result = preflight.run_checks(fake, environment(fake), isolation_tests(), mode, NOW)
            assert result.refusals == [], [str(r) for r in result.refusals]

    def test_a_plan_without_triggers_refuses_the_isolation_test_too(self):
        # "No failover started" means something only if one could have started.
        fake = fully_reconciled_fake()
        fake.plan_triggers = []
        (r,) = refusals(fake)
        assert r.test == ISOLATION and f"no trigger that deactivates {PRIMARY}" in r.reason and "AUTOMATIC_FAILOVER=disabled" in r.reason

    def test_a_static_preflight_leaves_check_8_out_for_it_as_for_the_recovery_test(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = []
        assert refusals(fake, mode=preflight.STATIC) == []

    def test_an_execution_thirty_minutes_ago_refuses_it_because_a_trigger_would_not_fire(self):
        fake = fully_reconciled_fake()
        started = NOW - timedelta(minutes=30)
        add_execution(fake, start=started.isoformat(), end=(started + timedelta(minutes=8)).isoformat())
        (r,) = refusals(fake)
        assert "started 30 min ago" in r.reason and "the trigger would not fire during this run" in r.reason and r.test == ISOLATION

    def test_the_scale_up_is_checked_against_the_destination_regions_maximum(self):
        fake = fully_reconciled_fake()
        fake.task_peaks[(PRIMARY, f"ui{ENV}")] = 5.0
        fake.scalable_targets[(STANDBY, f"service/{CLUSTER}/ui{ENV}")]["MaxCapacity"] = 8
        (r,) = refusals(fake)
        assert f"scale ui{ENV} in {STANDBY} to 10 tasks" in r.reason and "past the 8 its Auto Scaling target allows" in r.reason

    def test_the_peak_is_read_in_the_isolated_region(self):
        fake = fully_reconciled_fake()
        refusals(fake)
        stats = [c for c in fake.calls if c[1] == "get-metric-statistics"]
        assert stats and {c[2] for c in stats} == {PRIMARY}

    def test_a_test_that_cannot_fail_over_is_not_asked_for_any_of_it(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = []
        orders = [t for t in all_spec_tests() if t.name == "orders-broker-dependency"]
        result = preflight.run_checks(fake, environment(fake), orders, preflight.LIVE, NOW)
        assert result.refusals == [] and not [c for c in fake.calls if c[1] == "get-plan"]

    def test_a_call_that_fails_is_a_refusal_and_not_a_crash_and_does_not_hide_the_other_checks(self):
        fake = fully_reconciled_fake()
        fake.fail_on("arc-region-switch", "get-plan", "ThrottlingException")
        fake.alarms[(PRIMARY, LCL[0])]["state"] = "ALARM"
        result = preflight.run_checks(fake, environment(fake), isolation_tests(), preflight.LIVE, NOW)
        assert any(r.check == 8 and "could not check what the test needs to fail a Region over" in r.reason for r in result.refusals)
        assert any(r.check == 4 and LCL[0] in r.reason for r in result.refusals)

    # 4: the destination's health alarm must start OK for the check on it to mean anything

    def test_a_destination_that_is_already_degraded_refuses_the_run(self):
        fake = fully_reconciled_fake()
        fake.alarms[(STANDBY, PEER_DEGRADED)]["state"] = "ALARM"
        (r,) = refusals(fake, number=4)
        assert r.test == ISOLATION
        assert f"alarm {PEER_DEGRADED} is ALARM, not OK, in {STANDBY}" in r.reason and "destination-stayed-ok" in r.reason

    def test_insufficient_data_in_the_destination_refuses_it_too(self):
        fake = fully_reconciled_fake()
        fake.alarms[(STANDBY, PEER_DEGRADED)]["state"] = "INSUFFICIENT_DATA"
        assert len(refusals(fake, number=4)) == 1

    def test_the_static_preflight_does_not_ask_what_state_it_is_in(self):
        fake = fully_reconciled_fake()
        fake.alarms[(STANDBY, PEER_DEGRADED)]["state"] = "ALARM"
        assert refusals(fake, number=4, mode=preflight.STATIC) == []

    def test_a_test_without_that_check_does_not_care_about_the_destination(self):
        fake = fully_reconciled_fake()
        fake.alarms[(STANDBY, PEER_DEGRADED)]["state"] = "ALARM"
        orders = [t for t in all_spec_tests() if t.name == "orders-broker-dependency"]
        assert preflight.run_checks(fake, environment(fake), orders, preflight.LIVE, NOW).refusals == []

    def test_an_isolation_test_that_does_not_name_the_check_does_not_either(self):
        data = copy.deepcopy(shipped())
        isolation_entry(data)["runChecks"] = ["no-plan-execution"]
        (test,) = spec.parse(data).select(ISOLATION)
        fake = fully_reconciled_fake()
        fake.alarms[(STANDBY, PEER_DEGRADED)]["state"] = "ALARM"
        result = preflight.run_checks(fake, environment(fake), [test], preflight.LIVE, NOW)
        assert [r for r in result.refusals if r.check == 4] == []

    def test_a_missing_destination_alarm_is_the_existence_check_not_a_second_refusal(self):
        fake = fully_reconciled_fake()
        del fake.alarms[(STANDBY, PEER_DEGRADED)]
        reasons = [r.reason for r in refusals(fake, number=4)]
        assert reasons == [f"alarm {PEER_DEGRADED} does not exist in {STANDBY}"]


# --- the report and the verdict -------------------------------------------------------------------------------------------------

class TestTheReport:

    def test_the_failure_the_test_expects_with_the_plan_failing_the_region_over_is_as_expected(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "FAILED")
        add_execution(fake, comment=TRIGGER)
        data = collected(fake, run_id)
        assert (data["expected"], data["observed"], data["matches"]) == ("FAIL", "FAIL", True)
        by_name = {c["name"]: c for c in data["runChecks"]}
        assert by_name["no-plan-execution"]["passed"] is False and "exec-0001" in by_name["no-plan-execution"]["detail"]
        assert by_name["destination-stayed-ok"]["passed"] is True

    def test_the_markdown_says_a_failover_fired_and_which_journey_failed(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "FAILED")
        add_execution(fake, comment=TRIGGER)
        text = report.render(collected(fake, run_id))
        assert "Expected FAIL, observed FAIL: as expected." in text
        assert "- FAIL no-plan-execution: a plan execution started during the run: execution exec-0001 (deactivate us-east-1, graceful, completed)" in text
        assert "started by the trigger [Deactivate us-east-1: its cart journey fails there" in text
        assert f"- PASS destination-stayed-ok: {PEER_DEGRADED} stayed OK through the run and the window after it" in text
        assert "Resilience Hub ended the run PASSED" not in text
        section = text.split("## Region Switch plan executions during the run")[1].split("## Timeline")[0]
        assert "- exec-0001: deactivate us-east-1, graceful, completed" in section

    def test_the_failure_with_no_failover_says_none_started(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "FAILED")
        data = collected(fake, run_id)
        assert data["matches"] is True
        assert {c["name"]: c["passed"] for c in data["runChecks"]} == {"no-plan-execution": True, "destination-stayed-ok": True}
        text = report.render(data)
        assert "- PASS no-plan-execution: no plan execution started during the run, so no failover started" in text
        assert "- none started during the run" in text

    def test_a_passed_run_is_the_surprise_the_expectation_exists_to_show(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "PASSED")
        data = collected(fake, run_id)
        assert (data["observed"], data["matches"]) == ("PASS", False)
        text = report.render(data)
        assert "Expected FAIL, observed PASS: NOT as expected." in text
        assert "- PASS no-plan-execution" in text and "- PASS destination-stayed-ok" in text

    def test_a_passed_run_in_which_the_plan_started_anyway_stays_a_pass_and_the_check_says_so(self):
        # Not turned into the FAIL that was expected: that would record a surprising run as an unsurprising one.
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "PASSED")
        add_execution(fake, comment=TRIGGER)
        data = collected(fake, run_id)
        assert (data["observed"], data["matches"]) == ("PASS", False)
        assert {c["name"]: c["passed"] for c in data["runChecks"]}["no-plan-execution"] is False
        text = report.render(data)
        assert "Expected FAIL, observed PASS: NOT as expected." in text and "- FAIL no-plan-execution" in text
        assert "does not show what this test is for" not in text, "that sentence is for a PASS the checks turned into a FAIL"

    def test_a_test_that_expects_to_pass_still_has_its_pass_turned_into_a_fail_by_a_failed_check(self):
        fake = fully_reconciled_fake()
        data = isolation_expecting(fake, "PASS", "PASSED", with_execution=True)
        assert (data["observed"], data["matches"]) == ("FAIL", False)
        assert "does not show what this test is for" in report.render(data)

    def test_a_test_that_expects_nothing_in_particular_is_not_credited_a_pass_either(self):
        fake = fully_reconciled_fake()
        data = isolation_expecting(fake, "UNKNOWN", "PASSED", with_execution=True)
        assert data["observed"] == "FAIL"

    def test_the_destination_going_unhealthy_fails_its_check_and_the_report_shows_the_alarm(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "FAILED")
        fake.alarm_history[(STANDBY, PEER_DEGRADED)] = [history("OK", "ALARM", "2026-10-06T16:08:00+00:00")]
        data = collected(fake, run_id)
        by_name = {c["name"]: c for c in data["runChecks"]}
        assert by_name["destination-stayed-ok"]["passed"] is False and "OK -> ALARM at 16:08:00" in by_name["destination-stayed-ok"]["detail"]
        assert data["observed"] == "FAIL" and data["matches"] is True, "a failed run stays the failure that was expected"
        assert f"- {PEER_DEGRADED}: 16:08:00 OK -> ALARM" in report.render(data)

    def test_the_isolated_regions_own_composite_going_to_alarm_is_expected_and_does_not_fail_that_check(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "FAILED")
        fake.alarm_history[(PRIMARY, STOP)] = [history("OK", "ALARM", "2026-10-06T16:06:00+00:00")]
        by_name = {c["name"]: c for c in collected(fake, run_id)["runChecks"]}
        assert by_name["destination-stayed-ok"]["passed"] is True

    def test_a_history_that_cannot_be_read_is_a_gap_and_the_check_is_unknown(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "FAILED")
        fake.fail_on("cloudwatch", "describe-alarm-history", "ThrottlingException", times=100, on_region=STANDBY)
        data = collected(fake, run_id)
        by_name = {c["name"]: c for c in data["runChecks"]}
        assert by_name["destination-stayed-ok"]["passed"] is False and "could not be read, so this is not known" in by_name["destination-stayed-ok"]["detail"]
        assert any(f"history of {PEER_DEGRADED}" in g for g in data["gaps"])

    def test_a_run_whose_fault_never_ran_is_inconclusive_whatever_the_checks_say(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "FAILED")
        fake.fis[PRIMARY].append({"id": "EXPF", "state": {"status": "failed", "reason": "dynamodb:InjectError is not allowed"}})
        data = collected(fake, run_id)
        assert data["observed"] == "INCONCLUSIVE" and data["matches"] is False

    def test_the_evidence_lists_the_tables_alarm_the_standbys_view_and_both_composites(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake, "FAILED")
        fake.alarm_history[(PRIMARY, DYNAMODB_ERRORS)] = [history("OK", "ALARM", "2026-10-06T16:03:00+00:00")]
        text = report.render(collected(fake, run_id))
        assert f"- {DYNAMODB_ERRORS}: 16:03:00 OK -> ALARM" in text
        for alarm in RMT + [STOP, PEER_DEGRADED]:
            assert alarm in text.split("## Evidence alarms, by hop")[1]


def isolation_expecting(fake, expected, status, with_execution):
    """The report of an isolation test whose spec says ``expected``, to show how the verdict depends on the expectation."""
    data = shipped()
    isolation_entry(data)["expected"] = expected
    tests = [t for t in spec.parse(data).tests if t.name == ISOLATION]
    (resolved,) = context.resolve_all(fake, environment(fake), tests)
    test_id = reconcile.find_test(fake, environment(fake), resolved)
    run_id = fake.add_run("cart", test_id, status=status)
    fake.runs[run_id]["endedAt"] = "2026-10-06T16:20:00+00:00"
    if with_execution:
        add_execution(fake, comment=TRIGGER)
    return report.collect(fake, environment(fake), resolved, run_id, COLLECTED_AT)


# --- a whole run ---------------------------------------------------------------------------------------------------------------------

def isolation_ready(deactivate=True, script=("INITIALIZING", "RUNNING", "FAILED"), **execution_kwargs):
    """Every test exists, the run goes to ``script`` and, unless ``deactivate`` is False, the plan deactivates the primary during it
    (a trigger started it), which leaves DNS on the standby and the catalog writer there."""
    from test_ngrh_testing_recovery import recovery_ready
    return recovery_ready(deactivate=deactivate, script=script, comment=TRIGGER, **execution_kwargs)


def run_isolation(fake, tmp_path, *extra, clock=None):
    clock = clock or Clock()
    code = cli.main(["run", *ARGS, "--test", ISOLATION, "--reports-dir", str(tmp_path), "--settle-minutes", "0", *extra],
                    aws=fake, sleep=clock.sleep, clock=clock)
    return code, clock


class TestTheRun:

    def test_the_failure_it_expects_with_the_region_failed_over_and_back_exits_zero(self, tmp_path, capsys):
        fake = isolation_ready()
        code, _ = run_isolation(fake, tmp_path)
        out = capsys.readouterr().out
        assert code == 0, out
        assert "The plan deactivated us-east-1 during the run. Waiting up to 30 min for us-east-1 to have been healthy for 10 min, then running make failback REGION=us-east-1." in out
        assert "Fail-back of us-east-1 is done." in out and fake.global_writer == PRIMARY
        text = next(tmp_path.glob(f"{ISOLATION}-*.md")).read_text()
        assert "Expected FAIL, observed FAIL: as expected." in text and "## Fail-back of us-east-1" in text

    def test_the_only_writes_are_the_run_and_the_fail_back(self, tmp_path):
        fake = isolation_ready()
        run_isolation(fake, tmp_path)
        ops = operations(fake.writes())
        assert ops[0] == "start-test-run" and set(ops) <= {"start-test-run", "start-plan-execution", "register-scalable-target",
                                                           "update-service", "switchover-global-cluster"}
        assert ops.index("start-plan-execution") < ops.index("update-service") < ops.index("switchover-global-cluster")

    def test_a_failure_with_no_failover_has_nothing_to_fail_back(self, tmp_path, capsys):
        fake = isolation_ready(deactivate=False)
        code, _ = run_isolation(fake, tmp_path)
        out = capsys.readouterr().out
        assert code == 0 and operations(fake.writes()) == ["start-test-run"] and "The plan deactivated" not in out

    def test_a_passed_run_is_not_the_expected_result_and_exits_three(self, tmp_path):
        fake = isolation_ready(deactivate=False, script=("INITIALIZING", "RUNNING", "PASSED"))
        code, _ = run_isolation(fake, tmp_path)
        assert code == 3 and operations(fake.writes()) == ["start-test-run"]

    def test_failback_skip_leaves_the_region_deactivated_and_says_what_to_run(self, tmp_path, capsys):
        fake = isolation_ready()
        code, _ = run_isolation(fake, tmp_path, "--failback", "skip")
        out = capsys.readouterr().out
        assert code == 0 and operations(fake.writes()) == ["start-test-run"]
        assert "--failback skip leaves it that way. Bring it back with: make failback REGION=us-east-1" in out

    def test_the_run_is_refused_before_the_fault_when_the_plan_has_no_triggers(self, tmp_path, capsys):
        fake = isolation_ready(deactivate=False)
        fake.plan_triggers = []
        code, _ = run_isolation(fake, tmp_path)
        captured = capsys.readouterr()
        assert code == cli.EXIT_REFUSED and operations(fake.writes()) == []
        assert "no trigger that deactivates us-east-1" in captured.out + captured.err
