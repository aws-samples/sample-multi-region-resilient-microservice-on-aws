"""Step 11 (design 5.6, 5.8, 5.9, 5.10): the recovery test, catalog-recovery.

The test impairs the catalog database path in the primary Region and passes if the plan, started by its own alarm
triggers, moves the traffic within the objective. This file covers what is new in the tool: the spec entry and the two
lookups it uses, preflight check 8 (the triggers are there, the last execution is old enough, the scale-up fits), the
plan executions in the report and the run check built on them, and the fail-back a run chains after a failover.
Everything runs against the in-memory fake of the AWS calls; the first live run is still to come.
"""

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from ngrh_scenario import (  # noqa: E402  (first: it puts the deployment directory on the path)
    JOURNEYS, LCL, RECOVERY, RECOVERY_DESIRED, RECOVERY_OBSERVABILITY, RECOVERY_SOURCES, RECOVERY_SUCCESS, RMT,
    all_spec_tests, deployed_fake, environment, fully_reconciled_fake, recovery_fake, recovery_tests, reconciled_fake,
)
from ngrh_fake_aws import (  # noqa: E402
    CLUSTER, ENV, PLAN_ARN, PRIMARY, STANDBY, WRITE_OPERATIONS, alarm_arn, catalog_cluster_id, catalog_endpoint, default_triggers,
)

from ngrh_testing import alarms, cli, context, executions, failback, preflight, reconcile, report, spec  # noqa: E402
from ngrh_testing.aws import AwsCliError  # noqa: E402

ARGS = ["--primary-region", PRIMARY, "--standby-region", STANDBY, f"--env={ENV}"]
NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
SERVICES = ("ui", "catalog", "carts", "checkout", "orders", "assets")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.now > 6 * 3600:  # a wait that never ends fails the test instead of hanging it
            raise AssertionError("waited six simulated hours")


# --- helpers ---------------------------------------------------------------------------------------------------

STEPS = [("scale-up-ecs-services", "2026-10-06T16:04:15+00:00", "2026-10-06T16:05:20+00:00"),
         ("shift-dns-traffic-away", "2026-10-06T16:05:20+00:00", "2026-10-06T16:06:30+00:00"),
         ("switch-over-catalog-db", "2026-10-06T16:06:30+00:00", "2026-10-06T16:11:40+00:00")]


def execution(execution_id="exec-0001", action="deactivate", region=PRIMARY, state="completed",
              start="2026-10-06T16:04:10+00:00", end="2026-10-06T16:11:40+00:00", **extra):
    """An execution as ListPlanExecutions and GetPlanExecution give it, deactivating the primary during the fake run.
    The comment is made up: what ARC writes there for an execution a trigger started is not documented."""
    body = {"executionId": execution_id, "executionAction": action, "executionRegion": region, "executionState": state,
            "startTime": start, "mode": "graceful", "comment": "Execution started by automated trigger",
            "plan": {"recoveryTimeObjectiveMinutes": 10},
            "stepStates": [{"name": n, "status": "completed", "startTime": a, "endTime": b, "stepMode": "graceful"} for n, a, b in STEPS],
            "generatedReportDetails": [{"reportGenerationTime": end, "reportOutput": {"s3ReportOutput": {"s3ObjectKey": f"executions/{execution_id}/report.json"}}}]}
    if end:
        body["endTime"] = end
    if state in ("completed", "completedMonitoringApplicationHealth"):
        body["actualRecoveryTime"] = "PT7M30S"
    body.update(extra)
    return body


def add_execution(fake, **kwargs):
    """Put an execution in the plan's history, listed at both Regional endpoints as ARC does."""
    body = execution(**kwargs)
    for region in (PRIMARY, STANDBY):
        fake.plan_executions[region].append(dict(body))
    return body


def on_start(fake, change):
    """Run ``change`` the moment the test run is started: a trigger fires during the run, not before the preflight."""
    original = fake._handlers[("resiliencehubv2", "start-test-run")]

    def start(region, **params):
        out = original(region, **params)
        change()
        return out

    fake._handlers[("resiliencehubv2", "start-test-run")] = start


def recovery_ready(deactivate=True, state="completed", script=("INITIALIZING", "RUNNING", "RUNNING", "PASSED"), **execution_kwargs):
    """Both tests exist, the run goes to ``script``, and (unless ``deactivate`` is False) the plan deactivates the primary
    four minutes into it, which leaves DNS on the standby and the catalog writer there."""
    fake = fully_reconciled_fake()
    fake.run_script = list(script)
    if deactivate:
        fake.health_checks = {PRIMARY: "unhealthy", STANDBY: "healthy"}
        fake.global_writer = STANDBY
        for service in ("ui", "catalog", "cart", "checkout", "orders", "assets"):       # the plan scaled the standby up to twice the primary
            name = ("carts" if service == "cart" else service) + ENV
            fake.scalable_targets[(STANDBY, f"service/{CLUSTER}/{name}")].update(MinCapacity=4)
            fake.ecs_services[(STANDBY, name)]["desiredCount"] = 4
        on_start(fake, lambda: add_execution(fake, state=state, **execution_kwargs))
    return fake


def run_recovery(fake, tmp_path, *extra, clock=None, now_seconds=None):
    clock = clock or Clock()
    code = cli.main(["run", *ARGS, "--test", RECOVERY, "--reports-dir", str(tmp_path), "--settle-minutes", "0", *extra],
                    aws=fake, sleep=clock.sleep, clock=clock, now_seconds=now_seconds)
    return code, clock


def recovery_resolved(fake):
    (resolved,) = context.resolve_all(fake, environment(fake), recovery_tests())
    return resolved


def refusals(fake, number=8, mode=preflight.LIVE, now=NOW):
    result = preflight.run_checks(fake, environment(fake), recovery_tests(), mode, now)
    return [r for r in result.refusals if r.check == number]


# --- the spec entry ----------------------------------------------------------------------------------------------

class TestSpecEntry:

    def test_it_is_the_recovery_test_on_catalog_with_the_plan_and_the_database(self):
        (test,) = recovery_tests()
        assert (test.service, test.template) == ("catalog", spec.RECOVERY_TEMPLATE)
        assert test.parameters["impairedRegion"] == ("primary",) and test.parameters["recoveryRegion"] == ("standby",)
        assert test.parameters["regionSwitchPlan"] == (spec.Lookup("plan-arn", "primary"),)
        assert test.parameters["dependencies"] == (spec.Lookup("catalog-db-endpoints", "primary"),)
        assert test.duration_minutes == 20 and test.expected == "PASS"

    def test_the_success_alarms_are_the_four_global_journeys_of_the_impaired_region(self):
        (test,) = recovery_tests()
        assert [(a.name, a.region) for a in test.success_alarms] == [(f"journey-global-{j}", "primary") for j in JOURNEYS]
        assert [(a.name, a.region) for a in test.observability_alarms] == [("hop-catalog-slow", "primary")]
        assert len(test.success_alarms) + len(test.observability_alarms) == spec.MAX_SOURCES

    def test_there_is_no_stop_condition(self):
        # Stopping the fault cannot help if the standby fails after DNS has moved, and a guard on the primary's own journeys
        # would trip on the brief catalog pause during the switchover and end the run early (design 5.8).
        assert recovery_tests()[0].stop_alarms == ()

    def test_it_names_the_run_check_the_tool_evaluates(self):
        assert recovery_tests()[0].run_checks == ("deactivate-completed",)
        assert set(spec.KNOWN_RUN_CHECKS) == set(executions.CHECKS)

    def test_the_evidence_shows_every_input_of_the_triggers_that_deactivate_the_impaired_region(self):
        # A run that does not fail over has to show which condition was not met: the primary's own view of each journey,
        # the standby's view of the primary, and the standby's composite.
        shown = {(name, ref.region) for ref in recovery_tests()[0].evidence_alarms for name in ref.alarm_names()}
        for t in alarms.triggers("primary", "standby"):
            assert (f"journey-lcl-{t.journey}", "primary") in shown
            assert (f"journey-rmt-{t.journey}", "standby") in shown
        assert ("region-degraded", "standby") in shown and ("region-degraded", "primary") in shown

    def test_the_spec_names_two_tests_and_all_selects_both(self):
        assert [t.name for t in all_spec_tests()] == ["orders-broker-dependency", RECOVERY]

    def test_a_recovery_test_must_use_the_recovery_template_constant(self):
        assert spec.RECOVERY_TEMPLATE == "aws-multi-region-recovery:rtmr002"
        assert spec.TEMPLATES[spec.RECOVERY_TEMPLATE].fault_region == "impairedRegion"


# --- the two lookups -----------------------------------------------------------------------------------------------

class TestLookups:

    def test_the_catalog_database_endpoints_are_the_writer_and_the_reader_of_the_regions_cluster(self):
        fake = deployed_fake()
        assert context.catalog_db_endpoints(fake, environment(fake), PRIMARY) == [catalog_endpoint(PRIMARY, ENV), catalog_endpoint(PRIMARY, ENV, reader=True)]
        assert context.catalog_db_endpoints(fake, environment(fake), STANDBY) == [catalog_endpoint(STANDBY, ENV), catalog_endpoint(STANDBY, ENV, reader=True)]

    def test_the_cluster_comes_from_the_regions_catalog_db_stack_not_from_a_name_the_tool_builds(self):
        fake = deployed_fake()
        context.catalog_db_endpoints(fake, environment(fake), PRIMARY)
        (call,) = [c for c in fake.calls if c[1] == "describe-stack-resource"]
        assert call[2] == PRIMARY and call[3] == {"stack_name": f"catalog-db-stack{ENV}", "logical_resource_id": "DBCluster"}
        (cluster,) = [c for c in fake.calls if c[1] == "describe-db-clusters"]
        assert cluster[2] == PRIMARY and cluster[3] == {"db_cluster_identifier": catalog_cluster_id(PRIMARY, ENV)}

    def test_a_cluster_without_a_reader_endpoint_gives_the_writer_alone(self):
        fake = deployed_fake()
        handler = fake._handlers[("rds", "describe-db-clusters")]

        def writer_only(region, **params):
            out = handler(region, **params)
            del out["DBClusters"][0]["ReaderEndpoint"]
            return out

        fake._handlers[("rds", "describe-db-clusters")] = writer_only
        assert context.catalog_db_endpoints(fake, environment(fake), PRIMARY) == [catalog_endpoint(PRIMARY, ENV)]

    def test_a_stack_that_is_not_there_is_a_problem_naming_the_test_and_the_lookup(self):
        fake = deployed_fake()
        fake.fail_on("cloudformation", "describe-stack-resource", "Resource DBCluster does not exist for stack catalog-db-stack-t")
        with pytest.raises(context.ContextError) as e:
            context.resolve_all(fake, environment(fake), recovery_tests())
        assert any(p.startswith(f"{RECOVERY}: lookup catalog-db-endpoints in {PRIMARY} failed:") for p in e.value.problems)

    def test_the_plan_arn_is_the_region_switch_stacks_output(self):
        fake = deployed_fake()
        assert context.plan_arn(fake, environment(fake), PRIMARY) == [PLAN_ARN]
        assert context.plan_arn(fake, environment(fake), STANDBY) == [PLAN_ARN]      # one plan for the deployment

    def test_without_the_plan_stack_resolving_the_test_says_to_deploy_it(self):
        fake = deployed_fake()
        del fake.stacks[(PRIMARY, f"region-switch{ENV}")]
        with pytest.raises(context.ContextError) as e:
            context.resolve_all(fake, environment(fake), recovery_tests())
        (problem,) = e.value.problems
        assert problem.startswith(f"{RECOVERY}: lookup plan-arn in {PRIMARY} failed:") and "make region-switch" in problem

    def test_the_resolved_test_asks_resilience_hub_for_exactly_this(self):
        fake = deployed_fake()
        resolved = recovery_resolved(fake)
        assert resolved.parameters == RECOVERY_DESIRED["parameters"]
        assert resolved.service_arn == RECOVERY_DESIRED["service_arn"] and resolved.template_arn == RECOVERY_DESIRED["test_template_arn"]
        assert (resolved.role_name, resolved.logging_configuration) == (RECOVERY_DESIRED["role_name"], RECOVERY_DESIRED["logging_configuration"])
        assert resolved.fault_region == PRIMARY and resolved.stop_conditions == []
        assert sorted(resolved.sources()) == RECOVERY_SOURCES

    def test_the_log_group_is_the_impaired_regions(self):
        # The recovery test faults the primary, so FIS logs there even though the plan's work lands in the standby.
        assert recovery_resolved(deployed_fake()).logging_configuration["cloudWatchLogGroupArn"].split(":")[3] == PRIMARY


# --- reconcile -----------------------------------------------------------------------------------------------------

class TestReconcile:

    def test_the_test_is_created_without_stop_conditions_and_with_five_sources(self):
        fake = deployed_fake()
        plans, lines = reconcile.reconcile(fake, environment(fake), recovery_tests())
        assert lines == [f"{RECOVERY}: created test test-0001; sources +5 -0"]
        (create,) = fake.calls_of("create-test")
        assert "stop_conditions" not in create, "an empty list is not sent: there is nothing to stop on"
        assert create["parameters"] == RECOVERY_DESIRED["parameters"]
        (put,) = fake.calls_of("put-test-sources")
        assert sorted(api_pairs(put["test_sources"])) == RECOVERY_SOURCES

    def test_reconciling_again_writes_nothing(self):
        fake = fully_reconciled_fake()
        plans, lines = reconcile.reconcile(fake, environment(fake), all_spec_tests())
        assert all(p.in_sync for p in plans) and fake.writes() == []

    def test_both_tests_exist_after_reconciling_the_whole_spec(self):
        fake = deployed_fake()
        _, lines = reconcile.reconcile(fake, environment(fake), all_spec_tests())
        assert [line.split(":")[0] for line in lines] == ["orders-broker-dependency", RECOVERY]
        assert sorted(t["testTemplateArn"].rsplit("/", 1)[-1] for t in fake.tests.values()) == sorted(
            ["aws-dependency-validation:rtdep001", "aws-multi-region-recovery:rtmr002"])

    def test_a_changed_database_endpoint_is_drift_to_update(self):
        fake = fully_reconciled_fake()
        (test_id,) = [t for t, v in fake.tests.items() if v["testTemplateArn"].endswith("rtmr002")]
        fake.tests[test_id]["parameters"]["dependencies"] = ["old-writer.example.com"]
        plans, lines = reconcile.reconcile(fake, environment(fake), recovery_tests())
        assert lines == [f"{RECOVERY}: updated test {test_id} (parameters)"]
        (update,) = fake.calls_of("update-test")
        assert update["stop_conditions"] == [], "an update says so explicitly when the test has no stop condition"


def api_pairs(test_sources):
    from ngrh_testing import api
    return [(kind, arn) for kind, arn in
            ([("SUCCESS_CRITERIA", s["successCriteriaAlarm"]["alarmArn"]) for s in test_sources if "successCriteriaAlarm" in s]
             + [("OBSERVABILITY", s["observabilityAlarm"]["alarmArn"]) for s in test_sources if "observabilityAlarm" in s])]


# --- the catalog database lookup and the stack it reads are the repo's ------------------------------------------------

class TestAgainstTheTemplates:

    def test_both_regions_deploy_the_catalog_cluster_as_dbcluster_in_catalog_db_stack(self):
        import yaml
        root = TESTS.parent / "deployment"

        class Loader(yaml.SafeLoader):
            pass

        Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
        for name in ("aurora-global-primary-cluster.yml", "aurora-global-standby-cluster.yml"):
            loader = Loader((root / "database" / "aurora" / name).read_text())
            try:
                template = loader.get_single_data()
            finally:
                loader.dispose()
            assert template["Resources"]["DBCluster"]["Type"] == "AWS::RDS::DBCluster", name
        makefile = (root / "Makefile").read_text()
        assert makefile.count("--stack-name catalog-db-stack${ENV}") == 2

    def test_the_trigger_inputs_the_spec_shows_are_alarms_monitoring_creates(self):
        # test_ngrh_testing_spec checks every alarm of the spec against monitoring.yml; this pins the names the
        # recovery scenario of these tests uses to the same alarms the spec lists.
        (test,) = recovery_tests()
        names = {f"{n}-{context_region}{ENV}" for n, context_region in
                 [(a.name, PRIMARY if a.region == "primary" else STANDBY) for a in test.success_alarms + test.observability_alarms]}
        assert names == set(RECOVERY_SUCCESS + RECOVERY_OBSERVABILITY)


# --- preflight check 8 --------------------------------------------------------------------------------------------

class TestRecoveryPreflight:

    def test_a_ready_deployment_passes_every_check_in_both_modes(self):
        fake = fully_reconciled_fake()
        for mode in (preflight.LIVE, preflight.STATIC):
            result = preflight.run_checks(fake, environment(fake), all_spec_tests(), mode, NOW)
            assert result.refusals == [], [str(r) for r in result.refusals]

    def test_check_8_belongs_to_the_recovery_test_alone(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = []
        result = preflight.run_checks(fake, environment(fake), reconciled_tests_of(fake, "orders-broker-dependency"), preflight.LIVE, NOW)
        assert result.refusals == [] and not [c for c in fake.calls if c[1] == "get-plan"]

    def test_a_static_preflight_leaves_it_out_so_an_ordinary_deploy_with_the_switch_off_passes(self):
        # CI runs the static preflight after every deploy, and an ordinary deploy has automatic failover off.
        fake = fully_reconciled_fake()
        fake.plan_triggers = []
        result = preflight.run_checks(fake, environment(fake), all_spec_tests(), preflight.STATIC, NOW)
        assert result.refusals == []
        assert not [c for c in fake.calls if c[1] in ("get-plan", "get-metric-statistics")]

    # 8a: the triggers

    def test_a_plan_without_triggers_refuses_and_says_how_to_arm_them(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = []
        (r,) = refusals(fake)
        assert r.test == RECOVERY and f"no trigger that deactivates {PRIMARY}" in r.reason
        assert "AUTOMATIC_FAILOVER=disabled" in r.reason and "AUTOMATIC_FAILOVER=enabled" in r.reason and "make ngrh-alarm-replay" in r.reason

    def test_triggers_that_deactivate_only_the_other_region_do_not_count(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = [t for t in default_triggers() if t["targetRegion"] == STANDBY]
        assert len(refusals(fake)) == 1

    def test_a_trigger_that_activates_does_not_count(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = [dict(t, action="activate") for t in default_triggers()]
        assert len(refusals(fake)) == 1

    def test_one_trigger_for_the_impaired_region_is_enough(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = [t for t in default_triggers() if t["targetRegion"] == PRIMARY][:1]
        assert refusals(fake) == []

    def test_a_deployment_without_the_plan_stack_refuses_naming_the_stack(self):
        fake = fully_reconciled_fake()
        del fake.stacks[(PRIMARY, f"region-switch{ENV}")]
        result = preflight.run_checks(fake, environment(fake), recovery_tests(), preflight.LIVE, NOW)
        # Resolving the plan-arn lookup already fails: that is check 10, and nothing else is looked at.
        assert [r.check for r in result.refusals] == [10] and "make region-switch" in result.refusals[0].reason

    def test_the_plan_stack_going_away_after_the_lookup_is_still_a_refusal(self):
        fake = fully_reconciled_fake()
        resolved = recovery_resolved(fake)
        del fake.stacks[(PRIMARY, f"region-switch{ENV}")]
        (r,) = preflight.check_recovery(fake, environment(fake), resolved, NOW)
        assert r.check == 8 and r.test == RECOVERY and f"stack region-switch{ENV} is not deployed in {PRIMARY}" in r.reason

    # 8b: the last execution

    def test_an_execution_thirty_minutes_ago_refuses_and_says_when_to_come_back(self):
        fake = fully_reconciled_fake()
        started = NOW - timedelta(minutes=30)
        add_execution(fake, start=started.isoformat(), end=(started + timedelta(minutes=8)).isoformat())
        (r,) = refusals(fake)
        assert "exec-0001 (deactivate us-east-1) started 30 min ago" in r.reason and "at most one execution every 60 min" in r.reason
        assert "Wait until 12:30 UTC" in r.reason, "an hour after the start"

    def test_an_execution_just_over_the_delay_ago_is_fine(self):
        fake = fully_reconciled_fake()
        started = NOW - timedelta(minutes=61)
        add_execution(fake, start=started.isoformat(), end=(started + timedelta(minutes=8)).isoformat(), region=STANDBY, action="activate")
        assert refusals(fake) == []

    def test_exactly_the_delay_ago_is_fine(self):
        fake = fully_reconciled_fake()
        add_execution(fake, start=(NOW - timedelta(minutes=60)).isoformat(), end=None)
        assert refusals(fake) == []

    def test_the_latest_execution_decides_not_the_first_listed(self):
        fake = fully_reconciled_fake()
        add_execution(fake, execution_id="old", start=(NOW - timedelta(hours=5)).isoformat())
        add_execution(fake, execution_id="new", start=(NOW - timedelta(minutes=10)).isoformat())
        (r,) = refusals(fake)
        assert "new (" in r.reason and "started 10 min ago" in r.reason

    def test_the_delay_is_the_triggers_own_not_a_constant(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = default_triggers(delay=15)
        add_execution(fake, start=(NOW - timedelta(minutes=20)).isoformat())
        assert refusals(fake) == []
        fake.plan_triggers = default_triggers(delay=180)
        (r,) = refusals(fake)
        assert "every 180 min" in r.reason

    def test_with_no_trigger_there_is_no_delay_to_wait_for(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = []
        add_execution(fake, start=(NOW - timedelta(minutes=5)).isoformat())
        assert len(refusals(fake)) == 1 and "no trigger" in refusals(fake)[0].reason

    def test_triggers_with_different_delays_wait_for_the_longest(self):
        # Which trigger fires depends on which journey fails, which preflight can't know: it waits until all can.
        fake = fully_reconciled_fake()
        fake.plan_triggers = default_triggers(delay=30)
        fake.plan_triggers[0]["minDelayMinutesBetweenExecutions"] = 90   # the first one deactivates the primary too
        add_execution(fake, start=(NOW - timedelta(minutes=45)).isoformat())
        (r,) = refusals(fake)
        assert "at most one execution every 90 min" in r.reason and "Wait until 12:45 UTC" in r.reason

    def test_an_endpoint_that_cannot_be_listed_is_a_refusal(self):
        fake = fully_reconciled_fake()
        fake.fail_on("arc-region-switch", "list-plan-executions", "AccessDenied")
        assert any("could not list plan executions" in r.reason for r in refusals(fake))

    # 8c: the scale-up fits

    def test_a_peak_whose_double_is_the_maximum_fits(self):
        fake = fully_reconciled_fake()
        fake.task_peaks[(PRIMARY, f"orders{ENV}")] = 5.0
        assert refusals(fake) == []

    def test_a_peak_whose_double_passes_the_maximum_refuses_naming_the_service(self):
        fake = fully_reconciled_fake()
        fake.task_peaks[(PRIMARY, f"orders{ENV}")] = 6.0
        (r,) = refusals(fake)
        assert f"scale orders{ENV} in {STANDBY} to 12 tasks (200% of 6, the most it ran in {PRIMARY} in the last 24 hours)" in r.reason
        assert "past the 10 its Auto Scaling target allows" in r.reason and "wait" in r.reason

    def test_a_fractional_percent_rounds_up(self):
        fake = fully_reconciled_fake()
        fake.plan_target_percent = 150
        fake.task_peaks[(PRIMARY, f"catalog{ENV}")] = 7.0          # 10.5 -> 11
        (r,) = refusals(fake)
        assert "to 11 tasks (150% of 7" in r.reason
        fake.task_peaks[(PRIMARY, f"catalog{ENV}")] = 6.0          # 9
        assert refusals(fake) == []

    def test_the_percent_is_the_plans_own(self):
        fake = fully_reconciled_fake()
        fake.plan_target_percent = 100
        fake.task_peaks[(PRIMARY, f"orders{ENV}")] = 10.0
        assert refusals(fake) == []
        fake.plan_target_percent = 200
        assert len(refusals(fake)) == 1

    def test_a_block_with_no_percent_gets_the_default_of_a_hundred(self):
        # ARC's documentation: targetPercent "The default is 100". Twice the peak of 6 would not fit under 10, the peak would.
        fake = fully_reconciled_fake()
        fake.plan_target_percent = None
        fake.task_peaks[(PRIMARY, f"orders{ENV}")] = 6.0
        assert refusals(fake) == []
        fake.task_peaks[(PRIMARY, f"orders{ENV}")] = 11.0
        (r,) = refusals(fake)
        assert "to 11 tasks (100% of 11" in r.reason

    def test_every_service_is_checked_and_each_over_is_its_own_line(self):
        fake = fully_reconciled_fake()
        for service in ("ui", "carts", "assets"):
            fake.task_peaks[(PRIMARY, f"{service}{ENV}")] = 8.0
        assert len(refusals(fake)) == 3

    def test_the_maximum_is_the_recovery_regions_targets_not_a_constant(self):
        fake = fully_reconciled_fake()
        fake.task_peaks[(PRIMARY, f"ui{ENV}")] = 5.0
        fake.scalable_targets[(STANDBY, f"service/{CLUSTER}/ui{ENV}")]["MaxCapacity"] = 8
        (r,) = refusals(fake)
        assert "ui" in r.reason and "to 10 tasks" in r.reason and "past the 8 its Auto Scaling target allows" in r.reason

    def test_no_container_insights_data_is_a_refusal_not_a_pass(self):
        fake = fully_reconciled_fake()
        del fake.task_peaks[(PRIMARY, f"checkout{ENV}")]
        (r,) = refusals(fake)
        assert f"no Container Insights datapoint for checkout{ENV} in {PRIMARY}" in r.reason

    def test_a_service_with_no_scalable_target_in_the_recovery_region_is_a_refusal(self):
        fake = fully_reconciled_fake()
        del fake.scalable_targets[(STANDBY, f"service/{CLUSTER}/assets{ENV}")]
        (r,) = refusals(fake)
        assert f"assets{ENV} has no Application Auto Scaling target in {STANDBY}" in r.reason

    def test_the_peak_is_read_for_the_last_twenty_four_hours_from_the_impaired_region(self):
        fake = fully_reconciled_fake()
        refusals(fake)
        stats = [c for c in fake.calls if c[1] == "get-metric-statistics"]
        assert len(stats) == 6 and {c[2] for c in stats} == {PRIMARY}
        end = datetime.strptime(stats[0][3]["end_time"], "%Y-%m-%dT%H:%M:%SZ")
        start = datetime.strptime(stats[0][3]["start_time"], "%Y-%m-%dT%H:%M:%SZ")
        assert end - start == timedelta(hours=24) and end == NOW.replace(tzinfo=None)

    def test_a_plan_that_scales_nothing_up_has_no_ceiling_to_hit(self):
        fake = fully_reconciled_fake()
        handler = fake._handlers[("arc-region-switch", "get-plan")]

        def without_scaling(region, **params):
            out = handler(region, **params)
            out["plan"]["workflows"] = []
            return out

        fake._handlers[("arc-region-switch", "get-plan")] = without_scaling
        for service in SERVICES:
            fake.task_peaks[(PRIMARY, f"{service}{ENV}")] = 9.0
        assert refusals(fake) == [] and not [c for c in fake.calls if c[1] == "get-metric-statistics"]

    @pytest.mark.parametrize("service,operation", [("arc-region-switch", "get-plan"), ("cloudwatch", "get-metric-statistics"),
                                                   ("application-autoscaling", "describe-scalable-targets")])
    def test_a_call_that_fails_is_a_refusal_not_a_crash(self, service, operation):
        fake = fully_reconciled_fake()
        fake.fail_on(service, operation, "ThrottlingException")
        result = preflight.run_checks(fake, environment(fake), recovery_tests(), preflight.LIVE, NOW)
        assert any(r.check == 8 and "could not check what the recovery test needs" in r.reason for r in result.refusals)

    def test_the_rendered_refusal_carries_the_check_number_and_the_test(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = []
        text = preflight.render(preflight.run_checks(fake, environment(fake), recovery_tests(), preflight.LIVE, NOW), preflight.LIVE, [RECOVERY])
        assert f"[8] {RECOVERY}: the plan has no trigger" in text and text.startswith("Preflight (live) for catalog-recovery: refused")

    def test_a_check_8_refusal_does_not_hide_the_other_checks(self):
        fake = fully_reconciled_fake()
        fake.plan_triggers = []
        fake.alarms[(PRIMARY, RECOVERY_SUCCESS[0])]["state"] = "ALARM"
        numbers = sorted({r.check for r in preflight.run_checks(fake, environment(fake), recovery_tests(), preflight.LIVE, NOW).refusals})
        assert numbers == [4, 8]


def reconciled_tests_of(fake, name):
    return [t for t in all_spec_tests() if t.name == name]


# --- plan executions: reading them -----------------------------------------------------------------------------------

class TestParsingDurations:

    @pytest.mark.parametrize("text,seconds", [("PT7M30S", 450), ("PT45S", 45), ("PT1H", 3600), ("PT1H2M3S", 3723), ("P1DT2H", 93600),
                                              ("PT0S", 0), ("PT1.5S", 1.5)])
    def test_arcs_iso_durations(self, text, seconds):
        assert executions.parse_duration(text) == seconds

    @pytest.mark.parametrize("text", [None, "", "7 minutes", "P1Y", "P1M", "PT", "P", "T5M"])
    def test_anything_else_is_not_a_number(self, text):
        assert executions.parse_duration(text) is None


RUN_START = datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)
RUN_END = datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc)


class TestReadingExecutions:

    def test_a_deactivate_is_asked_about_at_the_region_that_stays_first(self):
        assert executions.endpoints_for("deactivate", PRIMARY, [PRIMARY, STANDBY]) == [STANDBY, PRIMARY]
        assert executions.endpoints_for("deactivate", STANDBY, [PRIMARY, STANDBY]) == [PRIMARY, STANDBY]

    def test_an_activate_is_asked_about_at_the_region_it_activates_first(self):
        assert executions.endpoints_for("activate", PRIMARY, [PRIMARY, STANDBY]) == [PRIMARY, STANDBY]

    def test_the_summary_keeps_the_steps_the_recovery_time_and_the_report_key(self):
        summary = executions.summarize(execution())
        assert (summary["executionId"], summary["action"], summary["region"], summary["state"], summary["mode"]) == (
            "exec-0001", "deactivate", PRIMARY, "completed", "graceful")
        assert summary["actualRecoverySeconds"] == 450 and summary["objectiveMinutes"] == 10
        assert [s["name"] for s in summary["steps"]] == [n for n, _, _ in STEPS]
        assert summary["reports"] == [{"generatedAt": "2026-10-06T16:11:40+00:00", "s3ObjectKey": "executions/exec-0001/report.json", "failure": None}]

    def test_an_execution_without_a_recovery_time_or_report_still_summarizes(self):
        raw = execution(state="inProgress", end=None)
        del raw["generatedReportDetails"], raw["plan"]
        summary = executions.summarize(raw)
        assert summary["actualRecoverySeconds"] is None and summary["objectiveMinutes"] is None
        assert summary["endTime"] is None and summary["reports"] == []

    def test_the_detail_comes_from_the_surviving_regions_endpoint(self):
        fake = fully_reconciled_fake()
        add_execution(fake)
        found, problems = executions.in_window(fake, environment(fake), PLAN_ARN, RUN_START, RUN_END)
        assert problems == [] and [e["executionId"] for e in found] == ["exec-0001"]
        details = [c for c in fake.calls if c[1] == "get-plan-execution"]
        assert [c[2] for c in details] == [STANDBY]                  # one read, at the Region that stays

    def test_when_that_endpoint_fails_the_other_one_is_asked(self):
        fake = fully_reconciled_fake()
        add_execution(fake)
        fake.fail_on("arc-region-switch", "get-plan-execution", "EndpointConnectionError")
        found, problems = executions.in_window(fake, environment(fake), PLAN_ARN, RUN_START, RUN_END)
        assert problems == [] and found[0]["steps"] is not None
        assert [c[2] for c in fake.calls if c[1] == "get-plan-execution"] == [STANDBY, PRIMARY]

    def test_when_no_endpoint_answers_the_summary_stands_in_without_steps_and_a_problem_says_why(self):
        fake = fully_reconciled_fake()
        add_execution(fake)
        fake.fail_on("arc-region-switch", "get-plan-execution", "AccessDenied", times=2)
        found, problems = executions.in_window(fake, environment(fake), PLAN_ARN, RUN_START, RUN_END)
        assert found[0]["steps"] is None and found[0]["state"] == "completed"
        (problem,) = problems
        assert "plan execution exec-0001 could not be read" in problem and STANDBY in problem and PRIMARY in problem

    def test_only_executions_that_started_during_the_run_are_read(self):
        fake = fully_reconciled_fake()
        add_execution(fake, execution_id="before", start="2026-10-06T15:30:00+00:00")
        add_execution(fake, execution_id="during", start="2026-10-06T16:05:00+00:00")
        add_execution(fake, execution_id="after", start="2026-10-06T17:00:00+00:00")
        found, _ = executions.in_window(fake, environment(fake), PLAN_ARN, RUN_START, RUN_END)
        assert [e["executionId"] for e in found] == ["during"]
        assert len([c for c in fake.calls if c[1] == "get-plan-execution"]) == 1, "the others are not even read"

    def test_a_minute_of_clock_difference_at_the_start_is_allowed(self):
        fake = fully_reconciled_fake()
        add_execution(fake, execution_id="just-before", start="2026-10-06T15:59:10+00:00")
        add_execution(fake, execution_id="too-early", start="2026-10-06T15:58:30+00:00")
        found, _ = executions.in_window(fake, environment(fake), PLAN_ARN, RUN_START, RUN_END)
        assert [e["executionId"] for e in found] == ["just-before"]

    def test_an_execution_listed_at_both_endpoints_is_one(self):
        fake = fully_reconciled_fake()
        add_execution(fake)
        found, _ = executions.in_window(fake, environment(fake), PLAN_ARN, RUN_START, RUN_END)
        assert len(found) == 1

    def test_an_endpoint_that_cannot_be_listed_is_a_problem_and_the_other_still_answers(self):
        fake = fully_reconciled_fake()
        add_execution(fake)
        fake.fail_on("arc-region-switch", "list-plan-executions", "AccessDenied")
        found, problems = executions.in_window(fake, environment(fake), PLAN_ARN, RUN_START, RUN_END)
        assert len(found) == 1 and len(problems) == 1 and "could not list plan executions at the us-east-1 endpoint" in problems[0]

    def test_the_regions_a_run_deactivated_are_listed_once_in_the_order_they_started(self):
        found = [executions.summarize(execution(execution_id="a", region=STANDBY)), executions.summarize(execution(execution_id="b", region=PRIMARY)),
                 executions.summarize(execution(execution_id="c", region=STANDBY)),
                 executions.summarize(execution(execution_id="d", action="activate", region="ap-south-1"))]
        assert executions.deactivated_regions(found) == [STANDBY, PRIMARY]

    @pytest.mark.parametrize("state", ["completed", "failed", "canceled", "inProgress", "pausedByFailedStep", "completedWithExceptions"])
    def test_a_deactivate_counts_whatever_state_it_ended_in(self, state):
        assert executions.deactivated_regions([executions.summarize(execution(state=state))]) == [PRIMARY]


# --- the run check --------------------------------------------------------------------------------------------------------

def check_result(found, problems=(), fake=None):
    fake = fake or deployed_fake()
    (result,) = executions.evaluate(recovery_resolved(fake), [executions.summarize(e) for e in found], list(problems))
    return result


class TestDeactivateCompleted:

    def test_a_completed_deactivate_of_the_impaired_region_passes_and_says_how_long_it_took(self):
        result = check_result([execution()])
        assert result.name == "deactivate-completed" and result.passed
        assert result.detail == ("execution exec-0001 deactivated us-east-1 (graceful) and completed; ARC measured a recovery time of "
                                 "7 min 30 s against the objective of 10 min")

    def test_completed_monitoring_application_health_counts_too(self):
        assert check_result([execution(state="completedMonitoringApplicationHealth")]).passed

    def test_no_execution_at_all_fails_and_points_at_the_triggers_and_the_evidence(self):
        result = check_result([])
        assert not result.passed and "no deactivate of us-east-1 started during the run" in result.detail
        assert "AUTOMATIC_FAILOVER=disabled" in result.detail and "Evidence alarms" in result.detail

    def test_a_deactivate_of_the_other_region_does_not_count(self):
        assert not check_result([execution(region=STANDBY)]).passed

    def test_an_activate_does_not_count(self):
        assert not check_result([execution(action="activate")]).passed

    @pytest.mark.parametrize("state,why", [("pausedByFailedStep", "a step failed and the execution waits for a person"),
                                           ("completedWithExceptions", "a step was skipped or failed and the run went on"),
                                           ("inProgress", "it had not finished when this was checked"),
                                           ("pausedByOperator", "an operator paused it"),
                                           ("pendingManualApproval", "it waits for an approval"),
                                           ("failed", "it did not complete"), ("canceled", "it did not complete")])
    def test_a_deactivate_that_did_not_complete_fails_with_its_state_and_why(self, state, why):
        result = check_result([execution(state=state)])
        assert not result.passed and f"is {state}: {why}" in result.detail and "exec-0001" in result.detail

    def test_one_completed_deactivate_among_failed_ones_is_enough(self):
        result = check_result([execution(execution_id="first", state="failed"), execution(execution_id="second", state="completed")])
        assert result.passed and "second" in result.detail

    def test_a_failed_one_after_a_completed_one_does_not_undo_it(self):
        assert check_result([execution(execution_id="first"), execution(execution_id="second", state="failed")]).passed

    def test_when_the_last_of_several_unfinished_ones_decides_the_message(self):
        result = check_result([execution(execution_id="first", state="failed"), execution(execution_id="second", state="pausedByFailedStep")])
        assert not result.passed and "second" in result.detail

    def test_executions_that_could_not_be_read_make_the_answer_unknown_not_a_pass(self):
        result = check_result([execution()], problems=["could not list plan executions at the us-west-2 endpoint: AccessDenied"])
        assert not result.passed and result.detail.startswith("could not read the plan's executions, so this is not known:")

    def test_without_a_recovery_time_the_detail_says_only_what_happened(self):
        raw = execution()
        del raw["actualRecoveryTime"]
        assert check_result([raw]).detail == "execution exec-0001 deactivated us-east-1 (graceful) and completed"

    def test_without_an_objective_the_recovery_time_stands_alone(self):
        raw = execution()
        raw["plan"] = {}
        assert check_result([raw]).detail.endswith("a recovery time of 7 min 30 s")

    def test_a_test_with_no_run_checks_has_none_evaluated(self):
        fake = deployed_fake()
        (resolved,) = context.resolve_all(fake, environment(fake), [t for t in all_spec_tests() if t.name != RECOVERY])
        assert executions.evaluate(resolved, [], []) == []


# --- the report ------------------------------------------------------------------------------------------------------------

COLLECTED_AT = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)


def collected(fake):
    from ngrh_testing import api
    resolved = recovery_resolved(fake)
    test_id = reconcile.find_test(fake, environment(fake), resolved)
    run_id = max(api.list_test_runs(fake, PRIMARY, resolved.service_arn, test_id), key=lambda r: r["startedAt"])["testRunId"]
    return report.collect(fake, environment(fake), resolved, run_id, COLLECTED_AT)


def finished_run(fake, status="PASSED"):
    """A run of the recovery test that has ended with ``status``."""
    resolved = recovery_resolved(fake)
    test_id = reconcile.find_test(fake, environment(fake), resolved)
    run_id = fake.add_run("catalog", test_id, status=status)
    fake.runs[run_id]["endedAt"] = "2026-10-06T16:20:00+00:00"
    return run_id


class TestTheReportsPlanExecutions:

    def test_the_executions_of_the_run_and_the_run_check_are_in_the_data(self):
        fake = fully_reconciled_fake()
        finished_run(fake)
        add_execution(fake)
        data = collected(fake)
        assert [e["executionId"] for e in data["planExecutions"]] == ["exec-0001"]
        assert data["runChecks"] == [{"name": "deactivate-completed", "passed": True, "detail": data["runChecks"][0]["detail"]}]
        assert (data["observed"], data["matches"]) == ("PASS", True)

    def test_a_run_resilience_hub_passed_is_a_fail_when_the_plan_did_not_move_the_traffic(self):
        fake = fully_reconciled_fake()
        finished_run(fake)                                  # PASSED, but no deactivate
        data = collected(fake)
        assert data["runChecks"][0]["passed"] is False
        assert (data["testRun"]["status"], data["observed"], data["matches"]) == ("PASSED", "FAIL", False)

    def test_a_run_the_sample_failed_stays_a_fail_and_its_check_is_still_evaluated(self):
        fake = fully_reconciled_fake()
        finished_run(fake, "FAILED")
        add_execution(fake)
        data = collected(fake)
        assert (data["observed"], data["matches"]) == ("FAIL", False) and data["runChecks"][0]["passed"] is True

    def test_a_run_whose_fault_never_ran_stays_inconclusive_whatever_the_check_says(self):
        fake = fully_reconciled_fake()
        finished_run(fake, "FAILED")
        fake.fis[PRIMARY].append({"id": "EXPF", "state": {"status": "failed", "reason": "At least one ECS Task is not registered as a SSM managed instance"}})
        data = collected(fake)
        assert data["observed"] == "INCONCLUSIVE" and data["runChecks"][0]["passed"] is False

    def test_a_run_that_was_stopped_is_not_made_a_fail_by_its_check(self):
        fake = fully_reconciled_fake()
        finished_run(fake, "STOPPED")
        assert collected(fake)["observed"] == "INCONCLUSIVE"

    def test_executions_outside_the_run_are_left_out(self):
        fake = fully_reconciled_fake()
        finished_run(fake)
        add_execution(fake, execution_id="yesterday", start="2026-10-05T10:00:00+00:00")
        add_execution(fake, execution_id="hours-before", start="2026-10-06T13:00:00+00:00")       # the run started 16:00
        add_execution(fake, execution_id="minutes-before", start="2026-10-06T15:58:00+00:00")     # more than the minute of slack
        add_execution(fake, execution_id="after-the-window", start="2026-10-06T16:45:00+00:00")   # the run ended 16:20; the window closes 16:30
        add_execution(fake, execution_id="much-later", start="2026-10-06T19:00:00+00:00")
        data = collected(fake)
        assert data["planExecutions"] == [] and data["runChecks"][0]["passed"] is False

    def test_an_execution_that_cannot_be_read_is_a_gap_and_the_check_does_not_pass(self):
        fake = fully_reconciled_fake()
        finished_run(fake)
        add_execution(fake)
        fake.fail_on("arc-region-switch", "get-plan-execution", "AccessDenied", times=2)
        data = collected(fake)
        assert any("plan execution exec-0001 could not be read" in g for g in data["gaps"])
        assert data["runChecks"][0]["passed"] is False and data["observed"] == "FAIL"

    def test_a_deployment_without_the_plan_stack_fails_the_check_by_saying_so(self):
        fake = fully_reconciled_fake()
        run_id = finished_run(fake)
        resolved = recovery_resolved(fake)                  # resolved while the plan was there, as when the run started
        del fake.stacks[(PRIMARY, f"region-switch{ENV}")]
        data = report.collect(fake, environment(fake), resolved, run_id, COLLECTED_AT)
        assert data["planExecutions"] == [] and "region-switch-t is not deployed" in data["runChecks"][0]["detail"]
        assert data["observed"] == "FAIL"

    def test_a_test_without_run_checks_reads_the_executions_too_but_has_nothing_to_judge(self):
        fake = reconciled_fake()
        resolved = context.resolve_all(fake, environment(fake), [t for t in all_spec_tests() if t.name != RECOVERY])[0]
        test_id = reconcile.find_test(fake, environment(fake), resolved)
        run_id = fake.add_run("orders", test_id, status="PASSED")
        fake.runs[run_id]["endedAt"] = "2026-10-06T16:20:00+00:00"
        add_execution(fake)
        data = report.collect(fake, environment(fake), resolved, run_id, COLLECTED_AT)
        assert [e["executionId"] for e in data["planExecutions"]] == ["exec-0001"] and data["runChecks"] == []
        assert data["observed"] == "PASS"

    def test_the_markdown_lists_the_check_and_the_execution_with_its_steps(self):
        fake = fully_reconciled_fake()
        finished_run(fake)
        add_execution(fake)
        text = report.render(collected(fake))
        assert "## Run checks" in text and "- PASS deactivate-completed: execution exec-0001 deactivated us-east-1" in text
        section = text.split("## Region Switch plan executions during the run")[1].split("## Timeline")[0]
        assert "- exec-0001: deactivate us-east-1, graceful, completed. 16:04:10 to 16:11:40. Recovery time 7 min 30 s against the objective of 10 min" in section
        assert 'Started with the comment "Execution started by automated trigger"' in section
        assert "  - scale-up-ecs-services: completed, 16:04:15 to 16:05:20" in section
        assert "  - switch-over-catalog-db: completed, 16:06:30 to 16:11:40" in section
        assert "  - ARC's report: s3 key executions/exec-0001/report.json" in section

    def test_a_failed_check_on_a_passed_run_is_explained_in_the_report(self):
        fake = fully_reconciled_fake()
        finished_run(fake)
        text = report.render(collected(fake))
        assert "Expected PASS, observed FAIL: NOT as expected." in text
        assert "Resilience Hub ended the run PASSED, but a run check failed" in text and "observed result is FAIL" in text
        assert "- FAIL deactivate-completed: no deactivate of us-east-1 started during the run" in text
        assert "- none started during the run" in text

    def test_an_execution_still_going_shows_it_has_not_ended(self):
        fake = fully_reconciled_fake()
        finished_run(fake)
        add_execution(fake, state="inProgress", end=None)
        text = report.render(collected(fake))
        assert "inProgress. from 16:04:10, not ended" in text

    def test_the_steps_that_could_not_be_read_are_said_so(self):
        fake = fully_reconciled_fake()
        finished_run(fake)
        add_execution(fake)
        fake.fail_on("arc-region-switch", "get-plan-execution", "AccessDenied", times=2)
        assert "its steps could not be read (see Not collected)" in report.render(collected(fake))

    def test_the_orders_report_has_no_run_checks_section_and_says_no_execution_started(self):
        fake = reconciled_fake()
        resolved = context.resolve_all(fake, environment(fake), [t for t in all_spec_tests() if t.name != RECOVERY])[0]
        test_id = reconcile.find_test(fake, environment(fake), resolved)
        run_id = fake.add_run("orders", test_id, status="PASSED")
        fake.runs[run_id]["endedAt"] = "2026-10-06T16:20:00+00:00"
        text = report.render(report.collect(fake, environment(fake), resolved, run_id, COLLECTED_AT))
        assert "## Run checks" not in text and "- none started during the run" in text

    def test_a_failback_is_written_into_the_report_when_there_is_one(self):
        fake = fully_reconciled_fake()
        finished_run(fake)
        add_execution(fake)
        data = collected(fake)
        data["failbacks"] = [{"region": PRIMARY, "lines": ["Preflight passed.", "", "Fail-back of us-east-1 is done."], "exit": 0}]
        text = report.render(data)
        assert "## Fail-back of us-east-1" in text and "- Fail-back of us-east-1 is done." in text
        assert "- \n" not in text, "blank lines are not bullets"
        assert "## Fail-back" not in report.render(collected(fake))


# --- the run chains the fail-back ----------------------------------------------------------------------------------------

def ran(fake, tmp_path, *extra, clock=None, now_seconds=None):
    code, clock = run_recovery(fake, tmp_path, *extra, clock=clock, now_seconds=now_seconds)
    return code, clock, fake.writes()


def operations(writes):
    return [op for _, op, _, _ in writes]


class TestTheRunFailsBack:

    def test_a_run_during_which_the_plan_deactivated_the_primary_brings_it_back(self, tmp_path, capsys):
        fake = recovery_ready()
        code, clock, writes = ran(fake, tmp_path)
        out = capsys.readouterr().out
        assert code == 0
        assert "The plan deactivated us-east-1 during the run. Waiting up to 30 min for us-east-1 to have been healthy for 10 min, then running make failback REGION=us-east-1." in out
        assert "Activate: activated us-east-1 with plan execution" in out and "Fail-back of us-east-1 is done." in out
        (started,) = [p for _, op, _, p in writes if op == "start-plan-execution"]
        assert (started["action"], started["target_region"], started["mode"]) == ("activate", PRIMARY, "graceful")
        assert fake.global_writer == PRIMARY, "the catalog writer was switched back"

    def test_the_fail_back_comes_after_the_report_and_is_written_into_it_afterwards(self, tmp_path, capsys):
        fake = recovery_ready()
        ran(fake, tmp_path)
        out = capsys.readouterr().out
        assert out.index("Report: ") < out.index("The plan deactivated") < out.index("Report updated with the fail-back")
        (md,) = tmp_path.glob("*.md")
        text = md.read_text()
        assert "## Fail-back of us-east-1" in text and "- Fail-back of us-east-1 is done." in text
        (js,) = tmp_path.glob("*.json")
        assert json.loads(js.read_text())["failbacks"][0]["exit"] == 0

    def test_the_only_writes_are_the_run_and_the_fail_back_and_in_that_order(self, tmp_path):
        fake = recovery_ready()
        _, _, writes = ran(fake, tmp_path)
        ops = operations(writes)
        assert set(ops) <= {"start-test-run", "start-plan-execution", "register-scalable-target", "update-service", "switchover-global-cluster"}
        assert ops[0] == "start-test-run"
        assert ops.index("start-plan-execution") < ops.index("update-service") < ops.index("switchover-global-cluster")

    def test_failback_skip_only_says_what_to_run(self, tmp_path, capsys):
        fake = recovery_ready()
        code, _, writes = ran(fake, tmp_path, "--failback", "skip")
        out = capsys.readouterr().out
        assert code == 0 and operations(writes) == ["start-test-run"]
        assert "--failback skip leaves it that way. Bring it back with: make failback REGION=us-east-1" in out
        assert "## Fail-back of us-east-1" in next(tmp_path.glob("*.md")).read_text()

    def test_a_run_that_started_no_failover_has_nothing_to_fail_back(self, tmp_path, capsys):
        fake = recovery_ready(deactivate=False)
        code, _, writes = ran(fake, tmp_path)
        out = capsys.readouterr().out
        assert code == 3, "the run check failed: Resilience Hub passed a run in which the plan did nothing"
        assert operations(writes) == ["start-test-run"] and "The plan deactivated" not in out and "Report updated" not in out
        assert "failbacks" not in json.loads(next(tmp_path.glob("*.json")).read_text()), "the report is rewritten only for a fail-back"

    def test_any_run_during_which_a_failover_started_brings_the_region_back_not_only_the_recovery_test(self, tmp_path):
        fake = fully_reconciled_fake()
        fake.run_script = ["INITIALIZING", "RUNNING", "PASSED"]
        fake.health_checks = {PRIMARY: "unhealthy", STANDBY: "healthy"}
        fake.global_writer = STANDBY
        on_start(fake, lambda: add_execution(fake))
        clock = Clock()
        code = cli.main(["run", *ARGS, "--test", "orders-broker-dependency", "--reports-dir", str(tmp_path), "--settle-minutes", "0"],
                        aws=fake, sleep=clock.sleep, clock=clock)
        assert code == 0 and "start-plan-execution" in operations(fake.writes())

    def test_a_run_that_ended_in_error_still_brings_back_a_region_the_plan_took_away(self, tmp_path):
        fake = recovery_ready(script=("INITIALIZING", "RUNNING", "ERROR"))
        code, _, writes = ran(fake, tmp_path)
        assert "start-plan-execution" in operations(writes)
        assert code == 3, "an ERROR is not the verdict the spec expects"

    def test_a_run_that_outlasts_its_budget_is_not_followed_by_a_fail_back(self, tmp_path):
        fake = recovery_ready(script=("RUNNING",))
        code, _, writes = ran(fake, tmp_path)
        assert code == cli.EXIT_ERROR and "start-plan-execution" not in operations(writes)

    def test_the_wait_for_the_region_to_be_healthy_is_a_wait_not_a_refusal(self, tmp_path, capsys):
        fake = recovery_ready()
        fake.alarms[(PRIMARY, LCL[2])]["state"] = "ALARM"
        clock = Clock()
        original = clock.sleep

        def recovering(seconds):
            original(seconds)
            if clock.now >= 90 + 300:                                   # the run took 90 s; the alarm clears five minutes later
                fake.alarms[(PRIMARY, LCL[2])]["state"] = "OK"

        clock.sleep = recovering
        code, _, writes = ran(fake, tmp_path, clock=clock)
        out = capsys.readouterr().out
        assert code == 0 and "start-plan-execution" in operations(writes)
        assert out.count(f"Waiting for us-east-1 to have been healthy for 10 min: {LCL[2]} (us-east-1) is ALARM, not OK") == 1, "said once, not every poll"

    def test_a_region_that_never_settles_is_not_failed_back_and_the_command_to_use_later_is_given(self, tmp_path, capsys):
        fake = recovery_ready()
        fake.alarms[(PRIMARY, LCL[0])]["state"] = "ALARM"
        code, clock, writes = ran(fake, tmp_path, "--failback-wait-minutes", "2")
        captured = capsys.readouterr()
        assert code == cli.EXIT_ERROR and "start-plan-execution" not in operations(writes)
        assert "us-east-1 was still not healthy after 2 min" in captured.err and "Run make failback REGION=us-east-1 when it is." in captured.err
        assert clock.now >= 90 + 120

    def test_a_fail_back_that_is_refused_leaves_the_run_in_error_and_says_why(self, tmp_path, capsys):
        fake = recovery_ready(state="pausedByFailedStep", end=None)
        code, _, writes = ran(fake, tmp_path)
        out = capsys.readouterr().out
        assert code == cli.EXIT_ERROR and "start-plan-execution" not in operations(writes)
        assert "Fail-back of us-east-1 refused" in out and "pausedByFailedStep" in out
        assert "## Fail-back of us-east-1" in next(tmp_path.glob("*.md")).read_text()

    def test_what_the_fail_back_leaves_for_the_operator_makes_the_run_a_finding(self, tmp_path, capsys):
        fake = recovery_ready()
        fake.global_members[PRIMARY] = "modifying"            # the old primary's database is not available yet
        code, _, _ = ran(fake, tmp_path)
        # failback.run waits up to WRITER_WAIT_MINUTES (45) for it; the fake clock makes that instant
        assert code == 3 and "Left for you: the catalog writer is still in" in capsys.readouterr().out

    def test_the_run_waits_for_the_old_primarys_database_as_long_as_make_failback_does(self, tmp_path, capsys):
        # A fail-back after a run must not give up sooner than the one a person starts: the old primary's database
        # takes a while to come back after a fault, and giving up leaves the catalog writer in the Region that was failed
        # over to. Here it is available 20 simulated minutes in, which only a wait of the full 45 minutes sees.
        fake = recovery_ready()
        fake.global_members[PRIMARY] = "modifying"
        clock = Clock()
        original = clock.sleep

        def coming_back(seconds):
            original(seconds)
            if clock.now >= 20 * 60:
                fake.global_members[PRIMARY] = "available"

        clock.sleep = coming_back
        code, _, _ = ran(fake, tmp_path, clock=clock)
        out = capsys.readouterr().out
        assert code == 0 and fake.global_writer == PRIMARY and "Left for you" not in out
        assert "Waiting up to 45 min for it to be possible to move it back" in out and failback.WRITER_WAIT_MINUTES == 45

    def test_a_fail_back_that_is_given_up_on_does_so_after_forty_five_minutes_not_before(self, tmp_path, capsys):
        fake = recovery_ready()
        fake.global_members[PRIMARY] = "modifying"
        code, clock, _ = ran(fake, tmp_path)
        assert code == 3 and "Left for you: the catalog writer is still in" in capsys.readouterr().out
        assert clock.now >= 45 * 60, "it waited the whole 45 minutes before it left the rest to the operator"

    def test_a_fail_back_that_fails_is_an_error(self, tmp_path, capsys):
        fake = recovery_ready()
        fake.execution_script = ["failed"]
        code, _, _ = ran(fake, tmp_path)
        captured = capsys.readouterr()
        assert code == cli.EXIT_ERROR and "ngrh_testing failback: plan execution" in captured.err and "is failed" in captured.err

    def test_ctrl_c_while_waiting_for_the_region_leaves_everything_safe_to_repeat(self, tmp_path, capsys):
        fake = recovery_ready()
        fake.alarms[(PRIMARY, LCL[0])]["state"] = "ALARM"
        clock = Clock()

        def interrupt(seconds):
            clock.now += seconds
            if clock.now > 120:
                raise KeyboardInterrupt

        clock.sleep = interrupt
        code, _, writes = ran(fake, tmp_path, clock=clock)
        assert code == cli.EXIT_INTERRUPTED and "start-plan-execution" not in operations(writes)
        assert "Interrupted while failing back. Nothing is undone and every step is safe to repeat" in capsys.readouterr().out

    def test_the_verdict_still_decides_when_the_fail_back_is_clean(self, tmp_path):
        fake = recovery_ready(script=("INITIALIZING", "RUNNING", "FAILED"))
        code, _, writes = ran(fake, tmp_path)
        assert code == 3 and "start-plan-execution" in operations(writes)


class TestExitCodes:

    @pytest.mark.parametrize("verdict,failback_code,expected", [
        (0, 0, 0), (3, 0, 3), (0, 3, 3), (3, 3, 3), (0, 1, 1), (3, 1, 1), (0, 2, 1), (3, 2, 1),
    ])
    def test_a_failed_or_refused_fail_back_is_an_error_a_partial_one_a_finding(self, verdict, failback_code, expected):
        assert cli._worst(verdict, failback_code) == expected


# --- the command line and the Makefile ------------------------------------------------------------------------------------

import os  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402

DEPLOYMENT = TESTS.parent / "deployment"
REAL_MAKE = shutil.which("make")


class TestCommandLine:

    def test_failing_back_after_a_run_is_automatic_and_waits_thirty_minutes(self):
        args = cli.parser().parse_args(["run", *ARGS, "--test", RECOVERY])
        assert args.failback == "auto" and args.failback_wait_minutes == 30 == failback.SETTLE_WAIT_MINUTES

    def test_only_auto_and_skip_are_choices(self):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(["run", *ARGS, "--test", RECOVERY, "--failback", "sometimes"])

    def test_the_documented_choices_are_the_parsers(self):
        assert cli.FAILBACK_CHOICES == ("auto", "skip")

    def test_the_preflight_command_hands_the_clock_to_check_8(self, capsys):
        fake = fully_reconciled_fake()
        started = NOW - timedelta(minutes=30)
        add_execution(fake, start=started.isoformat(), end=None)
        code = cli.main(["preflight", *ARGS, "--test", RECOVERY], aws=fake, now_seconds=NOW.timestamp())
        out = capsys.readouterr().out
        assert code == cli.EXIT_REFUSED and "[8] catalog-recovery: plan execution exec-0001" in out and "Wait until 12:30 UTC" in out

    def test_the_run_command_hands_the_same_clock_to_check_8(self, tmp_path, capsys):
        # `run` does its own live preflight; an execution 30 minutes before the injected time refuses it, though by the
        # real clock that execution is hours old (the fixtures are dated 2026-10-08).
        fake = fully_reconciled_fake()
        add_execution(fake, start=(NOW - timedelta(minutes=30)).isoformat(), end=(NOW - timedelta(minutes=22)).isoformat(),
                      region=STANDBY, action="activate")
        code, _, writes = ran(fake, tmp_path, now_seconds=NOW.timestamp())
        out = capsys.readouterr().out
        assert code == cli.EXIT_REFUSED and "[8] catalog-recovery: plan execution exec-0001" in out and "started 30 min ago" in out
        assert writes == [], "a refused run starts nothing"

    def test_the_report_is_collected_at_the_clock_the_run_was_given(self, tmp_path):
        # The fake run ended at 16:20Z and the evidence window is ten minutes long. At 16:21Z the report is early; by the
        # real clock it would be complete, which is what a report collected with the wrong clock would claim.
        fake = recovery_ready()
        early = datetime(2026, 10, 6, 16, 21, tzinfo=timezone.utc)
        code, _, _ = ran(fake, tmp_path, "--failback", "skip", now_seconds=early.timestamp())
        data = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert data["evidenceComplete"] is False and data["evidenceUntil"] == "2026-10-06T16:30:00Z"
        assert "This report is early" in next(tmp_path.glob("*.md")).read_text()


@pytest.mark.skipif(REAL_MAKE is None, reason="make not installed")
class TestMakefileFailback:

    def _line(self, tmp_path, *variables):
        stub = tmp_path / "aws"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
        r = subprocess.run([REAL_MAKE, "-C", str(DEPLOYMENT), "-n", "ngrh-test", f"ENV={ENV}", "TEST=catalog-recovery", *variables],
                           env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        (line,) = [l for l in r.stdout.splitlines() if "ngrh_testing" in l]
        return line

    def test_the_tools_own_default_stands_unless_told(self, tmp_path):
        assert "--failback" not in self._line(tmp_path)

    def test_the_choice_reaches_the_tool(self, tmp_path):
        assert self._line(tmp_path, "FAILBACK=skip").endswith('--failback "skip"')
        assert self._line(tmp_path, "FAILBACK=auto", "SETTLE_WAIT=3").endswith('--settle-minutes "3" --failback "auto"')


# --- the e2e role can make every call the run makes ------------------------------------------------------------------

import re  # noqa: E402

import yaml  # noqa: E402

E2E_ROLE = DEPLOYMENT / "github-oidc-role.yaml"
# The command line's service names that differ from the IAM prefix of their actions.
IAM_PREFIX = {"resiliencehubv2": "resiliencehub"}


class _CfnLoader(yaml.SafeLoader):
    pass


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def role_actions_on_everything():
    """The actions the e2e role's policies allow on Resource '*'."""
    # Drive the SafeLoader subclass directly rather than passing it to yaml.load (the repo's tests/test_yaml_loading.py).
    loader = _CfnLoader(E2E_ROLE.read_text())
    try:
        template = loader.get_single_data()
    finally:
        loader.dispose()
    allowed = set()
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::IAM::ManagedPolicy":
            continue
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            on = statement.get("Resource")
            if statement.get("Effect") == "Allow" and "*" in (on if isinstance(on, list) else [on]):
                allowed.update(statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    return allowed


def action_of(service, operation):
    return IAM_PREFIX.get(service, service) + ":" + "".join(part.title() for part in operation.split("-"))


class TestTheE2eRoleGrantsEveryCallTheRunMakes:
    """Design 5.12. The manual run of `catalog-recovery` executes `make ngrh-test` as the e2e role, and with it the live
    preflight, the report's reads of the plan executions and the fail-back's reads and writes. A call the role lacks would
    surface as a refusal in the middle of a four-hour job."""

    def _calls(self, tmp_path):
        fake = recovery_ready()
        fake.calls.clear()
        code, _ = run_recovery(fake, tmp_path)
        assert code == 0
        return {(service, operation) for service, operation, _, _ in fake.calls}

    def test_a_whole_recovery_run_with_its_fail_back_makes_the_calls_this_test_looks_at(self, tmp_path):
        calls = self._calls(tmp_path)
        # If the run stopped calling these, the test below would be passing on nothing.
        assert {("arc-region-switch", "start-plan-execution"), ("arc-region-switch", "list-plan-executions"),
                ("rds", "switchover-global-cluster"), ("ecs", "update-service"),
                ("application-autoscaling", "register-scalable-target"), ("resiliencehubv2", "start-test-run"),
                ("cloudwatch", "describe-alarm-history")} <= calls

    def test_the_role_allows_every_one_of_them(self, tmp_path):
        allowed = role_actions_on_everything()
        missing = sorted(action_of(*c) for c in self._calls(tmp_path)
                         if action_of(*c) not in allowed and IAM_PREFIX.get(c[0], c[0]) + ":*" not in allowed)
        assert not missing, f"github-oidc-role.yaml does not grant {missing}"

    def test_an_action_the_role_lacks_is_noticed(self):
        # The check itself: a call to a service the role has no statement for is reported.
        allowed = role_actions_on_everything()
        action = action_of("some-other-service", "do-a-thing")
        assert action == "some-other-service:DoAThing"
        assert action not in allowed and "some-other-service:*" not in allowed

    @pytest.mark.parametrize("service, operation, expected", [
        ("resiliencehubv2", "start-test-run", "resiliencehub:StartTestRun"),
        ("arc-region-switch", "list-route53-health-checks", "arc-region-switch:ListRoute53HealthChecks"),
        ("application-autoscaling", "register-scalable-target", "application-autoscaling:RegisterScalableTarget"),
    ])
    def test_the_command_line_names_become_the_actions_iam_knows(self, service, operation, expected):
        assert action_of(service, operation) == expected
