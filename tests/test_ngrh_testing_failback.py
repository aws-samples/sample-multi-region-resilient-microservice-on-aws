# SPDX-License-Identifier: MIT-0
"""Step 10 (design 5.10): ``make failback REGION=<Region>``.

Everything runs against the shared fake AWS and a fake clock, so a wait of 45 minutes is a loop over simulated
minutes. The fake models what the tool asks of ARC (start an activate execution at a Region's endpoint, poll it,
list the plan's Route 53 health checks), ECS and Application Auto Scaling (read and write the counts), and the
catalog Aurora global cluster (members, writer, and a switchover that takes a few looks to finish).
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

TESTS = Path(__file__).resolve().parent
DEPLOYMENT = TESTS.parent / "deployment"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(DEPLOYMENT))

from ngrh_fake_aws import CLUSTER, ENV, PLAN_ARN, PRIMARY, STANDBY, FakeAws, db_cluster_arn  # noqa: E402

from ngrh_testing import alarms, cli, context, failback  # noqa: E402
from ngrh_testing.aws import AwsCli  # noqa: E402

ECS_TEMPLATE = DEPLOYMENT / "ecs.yaml"
MAKEFILE = DEPLOYMENT / "Makefile"

ARGS = ["--primary-region", PRIMARY, "--standby-region", STANDBY, f"--env={ENV}"]
NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)

NGRH_SERVICES = ("ui", "catalog", "cart", "checkout", "orders", "assets")   # the fake's names; carts is cart
ECS_NAMES = [f"{s}{ENV}" for s in ("ui", "catalog", "carts", "checkout", "orders", "assets")]
JOURNEYS = alarms.JOURNEYS
SCALED = 6      # what the failover left every service of the remaining Region at


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.now > 6 * 3600:        # a wait that never ends fails the test instead of hanging it
            raise AssertionError("waited six simulated hours")


def failed_over_fake(scaled=SCALED) -> FakeAws:
    """us-east-1 has been failed over and is healthy again: DNS names only us-west-2, the catalog writer and the
    scaled-up services are in us-west-2, and every journey alarm has been OK for a long time."""
    fake = FakeAws()
    for j in JOURNEYS:
        for region in (PRIMARY, STANDBY):
            fake.add_alarm(alarms.journey_alarm_name("lcl", j, region, ENV), region)
            fake.add_alarm(alarms.journey_alarm_name("rmt", j, region, ENV), region)
    for region in (PRIMARY, STANDBY):
        for service in NGRH_SERVICES:
            fake.add_ecs_service(service, region)
    for name in ECS_NAMES:
        fake.ecs_services[(STANDBY, name)]["desiredCount"] = scaled
        fake.scalable_targets[(STANDBY, f"service/{CLUSTER}/{name}")]["MinCapacity"] = scaled
    fake.health_checks = {PRIMARY: "unhealthy", STANDBY: "healthy"}
    fake.global_writer = STANDBY
    return fake


def environment(fake):
    return context.load_basic_environment(fake, PRIMARY, STANDBY, ENV)


def fail_back(fake, region=PRIMARY, **kw):
    clock, lines = Clock(), []
    outcome = failback.run(fake, environment(fake), region, lines.append, clock.sleep, clock, lambda: NOW, **kw)
    return outcome, lines, clock


def refusals(fake, region=PRIMARY):
    with pytest.raises(failback.FailbackRefused) as caught:
        fail_back(fake, region)
    return caught.value.problems


def write_ops(fake):
    return [op for _, op, _, _ in fake.writes()]


def execution(action, region, state, at="2026-10-08T11:00:00+00:00", eid="exec-1"):
    return {"executionId": eid, "executionAction": action, "executionRegion": region, "executionState": state, "startTime": at}


def targets(fake, region):
    return {name: (fake.ecs_services[(region, name)]["desiredCount"],
                   fake.scalable_targets[(region, f"service/{CLUSTER}/{name}")]["MinCapacity"],
                   fake.scalable_targets[(region, f"service/{CLUSTER}/{name}")]["MaxCapacity"]) for name in ECS_NAMES}


# --- the whole thing --------------------------------------------------------------------------------------

class TestFailback:

    def test_a_failed_over_region_comes_back_with_dns_capacity_and_the_writer(self):
        fake = failed_over_fake()
        outcome, lines, _ = fail_back(fake)
        assert outcome.exit_code == 0 and outcome.failed == [] and outcome.left == []
        assert fake.health_checks == {PRIMARY: "healthy", STANDBY: "healthy"}
        assert all(v == (2, 2, 10) for v in targets(fake, PRIMARY).values())
        assert all(v == (2, 2, 10) for v in targets(fake, STANDBY).values())      # the remaining Region goes back down too
        assert fake.global_writer == PRIMARY and fake.global_cluster_status == "available"
        steps = [line.split(":")[0] for line in lines if line.split(":")[0] in ("Preflight passed", "Activate", "Capacity", "Catalog writer")]
        assert steps == ["Preflight passed", "Activate", "Capacity", "Catalog writer"]

    def test_the_steps_happen_in_order(self):
        fake = failed_over_fake()
        fail_back(fake)
        ops = write_ops(fake)
        assert ops[0] == "start-plan-execution"
        assert ops[-1] == "switchover-global-cluster"
        capacity = {"register-scalable-target", "update-service"}
        assert set(ops[1:-1]) == capacity and ops.index("start-plan-execution") < min(i for i, o in enumerate(ops) if o in capacity)

    def test_nothing_is_written_before_preflight_has_passed(self):
        fake = failed_over_fake()
        fake.alarms[(PRIMARY, f"journey-lcl-home-{PRIMARY}{ENV}")]["state"] = "ALARM"
        refusals(fake)
        assert fake.writes() == []

    def test_a_second_run_changes_nothing(self):
        fake = failed_over_fake()
        fail_back(fake)
        fake.calls.clear()
        outcome, lines, _ = fail_back(fake)
        assert outcome.exit_code == 0
        assert fake.writes() == []
        assert any("nothing to activate" in line for line in lines)
        assert any("already at the reset values" in line for line in lines)
        assert any("already" in line and "writer" in line for line in lines)

    def test_it_never_fails_the_database_over(self):
        # What can lose data stays a person's decision in the paused run. This module only ever switches over.
        source = Path(failback.__file__).read_text()
        assert "failover-global-cluster" not in source and "failover_global_cluster" not in source
        assert 'mode="graceful"' in source and "ungraceful" not in source.replace("failing the database over ungracefully", "")


# --- preflight ----------------------------------------------------------------------------------------------

class TestPreflight:

    @pytest.mark.parametrize("region,name", [(PRIMARY, "journey-lcl-cart"), (STANDBY, "journey-rmt-orders")])
    def test_an_alarm_that_is_not_ok_refuses(self, region, name):
        fake = failed_over_fake()
        fake.alarms[(region, f"{name}-{region}{ENV}")]["state"] = "ALARM"
        (problem,) = refusals(fake)
        assert f"{name}-{region}{ENV}" in problem and "ALARM, not OK" in problem

    def test_the_regions_view_and_its_peers_view_of_it_are_the_ones_read(self):
        fake = failed_over_fake()
        fail_back(fake)
        asked = {(region, name) for _, op, region, p in fake.calls if op == "describe-alarm-history" for name in [p["alarm_name"]]}
        assert asked == ({(PRIMARY, f"journey-lcl-{j}-{PRIMARY}{ENV}") for j in JOURNEYS}
                         | {(STANDBY, f"journey-rmt-{j}-{STANDBY}{ENV}") for j in JOURNEYS})

    def test_the_standbys_failback_reads_its_own_views(self):
        fake = failed_over_fake()
        fake.health_checks = {PRIMARY: "healthy", STANDBY: "unhealthy"}
        fake.global_writer = PRIMARY
        fail_back(fake, STANDBY)
        asked = {(region, p["alarm_name"]) for _, op, region, p in fake.calls if op == "describe-alarm-history"}
        assert asked == ({(STANDBY, f"journey-lcl-{j}-{STANDBY}{ENV}") for j in JOURNEYS}
                         | {(PRIMARY, f"journey-rmt-{j}-{PRIMARY}{ENV}") for j in JOURNEYS})

    def test_an_alarm_that_went_ok_minutes_ago_refuses(self):
        fake = failed_over_fake()
        name = f"journey-lcl-orders-{PRIMARY}{ENV}"
        fake.alarm_history[(PRIMARY, name)] = [{"Timestamp": "2026-10-08T11:55:00+00:00", "HistoryItemType": "StateUpdate"}]
        (problem,) = refusals(fake)
        assert name in problem and "changed state 5 min 0 s ago, at 11:55:00 UTC" in problem and "stay OK for 10 min" in problem

    def test_the_history_is_asked_for_exactly_the_last_ten_minutes_of_state_changes(self):
        fake = failed_over_fake()
        fail_back(fake)
        asked = [p for _, op, _, p in fake.calls if op == "describe-alarm-history"]
        assert asked and all(p["start_date"] == "2026-10-08T11:50:00Z" and p["end_date"] == "2026-10-08T12:00:00Z"
                             and p["history_item_type"] == "StateUpdate" for p in asked)

    def test_a_missing_alarm_refuses(self):
        fake = failed_over_fake()
        del fake.alarms[(STANDBY, f"journey-rmt-catalog-{STANDBY}{ENV}")]
        (problem,) = refusals(fake)
        assert "does not exist in us-west-2" in problem and "make monitoring" in problem

    @pytest.mark.parametrize("state", ["inProgress", "pausedByFailedStep", "pausedByOperator", "pendingManualApproval", "pending"])
    def test_a_plan_execution_that_is_going_refuses(self, state):
        fake = failed_over_fake()
        fake.plan_executions[STANDBY].append(execution("deactivate", PRIMARY, state))
        (problem,) = refusals(fake)
        assert "exec-1" in problem and state in problem

    def test_a_paused_execution_says_it_holds_the_plan(self):
        fake = failed_over_fake()
        fake.plan_executions[STANDBY].append(execution("deactivate", PRIMARY, "pausedByFailedStep"))
        (problem,) = refusals(fake)
        assert "holds the plan until a person resolves it" in problem

    @pytest.mark.parametrize("state", ["completed", "completedWithExceptions", "failed", "canceled"])
    def test_a_finished_execution_does_not_refuse(self, state):
        fake = failed_over_fake()
        fake.plan_executions[STANDBY].append(execution("deactivate", PRIMARY, state))
        outcome, _, _ = fail_back(fake)
        assert outcome.exit_code == 0

    def test_both_health_checks_unhealthy_refuses(self):
        fake = failed_over_fake()
        fake.health_checks = {PRIMARY: "unhealthy", STANDBY: "unhealthy"}
        (problem,) = refusals(fake)
        assert "both of the plan's Route 53 health checks are unhealthy" in problem

    def test_a_plan_without_health_checks_refuses(self):
        fake = failed_over_fake()
        fake.health_checks = {STANDBY: "healthy"}
        (problem,) = refusals(fake)
        assert "no Route 53 health check for us-east-1" in problem

    def test_the_region_that_is_serving_while_its_peer_is_not_is_the_wrong_one(self):
        fake = failed_over_fake()           # DNS has moved away from us-east-1
        (problem,) = refusals(fake, STANDBY)
        assert "us-west-2 is serving already" in problem and "make failback REGION=us-east-1" in problem

    def test_every_reason_is_listed(self):
        fake = failed_over_fake()
        fake.alarms[(PRIMARY, f"journey-lcl-home-{PRIMARY}{ENV}")]["state"] = "ALARM"
        fake.plan_executions[PRIMARY].append(execution("deactivate", PRIMARY, "inProgress"))
        fake.health_checks = {PRIMARY: "unhealthy", STANDBY: "unhealthy"}
        assert len(refusals(fake)) == 3

    @pytest.mark.parametrize("service,operation,what", [
        ("cloudwatch", "describe-alarms", "the alarms"),
        ("cloudwatch", "describe-alarm-history", "the alarms"),
        ("arc-region-switch", "list-route53-health-checks", "the plan's health checks"),
    ])
    def test_a_call_that_fails_refuses_and_says_what_it_could_not_check(self, service, operation, what):
        fake = failed_over_fake()
        fake.fail_on(service, operation, "AccessDeniedException: no", times=1)
        problems = refusals(fake)
        assert any(p.startswith(f"could not check {what}: ") and "AccessDeniedException" in p for p in problems)
        assert fake.writes() == []

    def test_what_cannot_be_read_refuses(self):
        fake = failed_over_fake()
        fake.fail_on("arc-region-switch", "list-plan-executions", "ThrottlingException", times=2)
        problems = refusals(fake)
        assert len(problems) == 2 and all(p.startswith("could not list plan executions at the") for p in problems)
        assert fake.writes() == []

    def test_a_plan_that_is_not_deployed_refuses(self):
        fake = failed_over_fake()
        del fake.stacks[(PRIMARY, f"region-switch{ENV}")]
        (problem,) = refusals(fake)
        assert f"region-switch{ENV} is not deployed in {PRIMARY}" in problem

    def test_a_region_that_is_neither_is_a_usage_error(self):
        with pytest.raises(ValueError, match="neither the primary Region"):
            fail_back(failed_over_fake(), "eu-west-1")


# --- activate -------------------------------------------------------------------------------------------------

class TestActivate:

    def test_the_activate_workflow_is_started_graceful_at_the_regions_own_endpoint(self):
        fake = failed_over_fake()
        fail_back(fake)
        ((_, _, region, params),) = [c for c in fake.calls if c[1] == "start-plan-execution"]
        assert region == PRIMARY
        assert params == {"plan_arn": PLAN_ARN, "target_region": PRIMARY, "action": "activate", "mode": "graceful",
                          "comment": f"make failback REGION={PRIMARY}"}

    def test_the_standby_is_activated_at_the_standbys_endpoint(self):
        fake = failed_over_fake()
        fake.health_checks = {PRIMARY: "healthy", STANDBY: "unhealthy"}
        fake.global_writer = PRIMARY
        fail_back(fake, STANDBY)
        ((_, _, region, params),) = [c for c in fake.calls if c[1] == "start-plan-execution"]
        assert region == STANDBY and params["target_region"] == STANDBY

    def test_it_waits_for_the_execution_and_reports_each_state_once(self):
        fake = failed_over_fake()
        fake.execution_script = ["inProgress", "inProgress", "inProgress", "completed"]
        outcome, lines, clock = fail_back(fake)
        assert outcome.exit_code == 0
        assert len(fake.calls_of("get-plan-execution")) == 4
        states = [line for line in lines if line.startswith("exec-")]
        assert [s.split(": ")[1].split(" ")[0] for s in states] == ["inProgress", "completed"]
        assert clock.now >= 45

    def test_completed_while_monitoring_the_application_counts_as_done(self):
        fake = failed_over_fake()
        fake.execution_script = ["completedMonitoringApplicationHealth"]
        outcome, _, _ = fail_back(fake)
        assert outcome.exit_code == 0

    @pytest.mark.parametrize("state", ["failed", "canceled", "planExecutionTimedOut", "completedWithExceptions",
                                       "pausedByFailedStep", "pausedByOperator", "pendingManualApproval"])
    def test_an_execution_that_does_not_complete_stops_before_capacity_and_the_writer(self, state):
        fake = failed_over_fake()
        fake.execution_script = ["inProgress", state]
        with pytest.raises(failback.FailbackError) as caught:
            fail_back(fake)
        assert f"is {state}, so {PRIMARY} is not back in DNS" in str(caught.value) and "left alone" in str(caught.value)
        assert write_ops(fake) == ["start-plan-execution"]
        assert f"make failback REGION={PRIMARY} again" in str(caught.value)

    def test_an_execution_that_takes_too_long_stops_there_too(self):
        fake = failed_over_fake()
        fake.execution_script = ["inProgress"]
        with pytest.raises(failback.FailbackError) as caught:
            fail_back(fake)
        assert "still inProgress after 10 min" in str(caught.value) and "goes on in AWS" in str(caught.value)
        assert write_ops(fake) == ["start-plan-execution"]

    def test_a_region_already_serving_is_not_activated_again(self):
        fake = failed_over_fake()
        fake.health_checks = {PRIMARY: "healthy", STANDBY: "healthy"}
        outcome, lines, _ = fail_back(fake)
        assert outcome.exit_code == 0
        assert "start-plan-execution" not in write_ops(fake)
        assert any("nothing to activate" in line for line in lines)

    def test_an_unknown_health_check_is_activated(self):
        fake = failed_over_fake()
        fake.health_checks[PRIMARY] = "unknown"
        fail_back(fake)
        assert "start-plan-execution" in write_ops(fake)


# --- capacity ------------------------------------------------------------------------------------------------

class TestCapacity:

    def test_every_service_of_both_regions_goes_back_to_the_reset_values(self):
        fake = failed_over_fake()
        for name in ECS_NAMES:                                    # us-east-1 was left scaled too
            fake.ecs_services[(PRIMARY, name)]["desiredCount"] = 4
            fake.scalable_targets[(PRIMARY, f"service/{CLUSTER}/{name}")]["MinCapacity"] = 4
        fail_back(fake)
        for region in (PRIMARY, STANDBY):
            assert all(v == (2, 2, 10) for v in targets(fake, region).values()), region
        assert len(fake.calls_of("register-scalable-target")) == 12 and len(fake.calls_of("update-service")) == 12

    def test_the_minimum_is_lowered_before_the_desired_count(self):
        # While the minimum is above the desired count, Auto Scaling would put the count back.
        fake = failed_over_fake()
        fail_back(fake)
        ops = [(op, p.get("resource_id", "").split("/")[-1] or p.get("service")) for _, op, region, p in fake.calls
               if region == STANDBY and op in ("register-scalable-target", "update-service")]
        for name in ECS_NAMES:
            assert ops.index(("register-scalable-target", name)) < ops.index(("update-service", name))

    def test_the_maximum_is_kept_as_it_is(self):
        fake = failed_over_fake()
        fake.scalable_targets[(STANDBY, f"service/{CLUSTER}/orders{ENV}")]["MaxCapacity"] = 14
        fail_back(fake)
        assert fake.scalable_targets[(STANDBY, f"service/{CLUSTER}/orders{ENV}")]["MaxCapacity"] == 14
        sent = [p for _, op, region, p in fake.calls if op == "register-scalable-target" and p["resource_id"].endswith(f"orders{ENV}") and region == STANDBY]
        assert [p["max_capacity"] for p in sent] == [14]

    def test_only_what_differs_is_written(self):
        fake = failed_over_fake(scaled=2)                 # nothing scaled up
        name = f"carts{ENV}"
        fake.ecs_services[(STANDBY, name)]["desiredCount"] = 5                       # the count only
        fake.scalable_targets[(STANDBY, f"service/{CLUSTER}/ui{ENV}")]["MinCapacity"] = 3   # the minimum only
        fail_back(fake)
        writes = [(op, region, p) for _, op, region, p in fake.writes() if op in ("register-scalable-target", "update-service")]
        assert len(writes) == 2
        assert ("update-service", STANDBY, {"cluster": CLUSTER, "service": name, "desired_count": 2}) in writes
        assert any(op == "register-scalable-target" and p["resource_id"].endswith(f"ui{ENV}") and p["min_capacity"] == 2 for op, _, p in writes)

    def test_the_cluster_is_the_one_the_apps_stack_made(self):
        fake = failed_over_fake()
        fail_back(fake)
        assert {p["cluster"] for _, op, _, p in fake.calls if op == "update-service"} == {CLUSTER}
        assert {p["resource_id"].split("/")[1] for _, op, _, p in fake.calls if op == "register-scalable-target"} == {CLUSTER}

    def test_one_service_failing_does_not_stop_the_others_or_the_writer(self):
        fake = failed_over_fake()
        fake.fail_on("application-autoscaling", "register-scalable-target", "AccessDeniedException: not allowed")
        outcome, _, _ = fail_back(fake)
        assert outcome.exit_code == 1 and len(outcome.failed) == 1 and "AccessDeniedException" in outcome.failed[0]
        assert fake.global_writer == PRIMARY
        other = [v for region in (PRIMARY, STANDBY) for v in targets(fake, region).values() if v != (2, 2, 10)]
        assert len(other) <= 1            # the one that failed, which was not lowered further

    def test_a_service_with_no_scalable_target_is_a_failure(self):
        fake = failed_over_fake()
        del fake.scalable_targets[(STANDBY, f"service/{CLUSTER}/assets{ENV}")]
        outcome, _, _ = fail_back(fake)
        assert outcome.exit_code == 1 and f"assets{ENV} in {STANDBY}: no scalable target" in outcome.failed[0]

    def test_capacity_that_cannot_be_read_does_not_stop_the_writer(self):
        fake = failed_over_fake()
        fake.fail_on("application-autoscaling", "describe-scalable-targets", "AccessDeniedException", times=1)
        outcome, _, _ = fail_back(fake)
        assert outcome.exit_code == 1 and outcome.failed[0].startswith("capacity: could not read the capacity")
        assert fake.global_writer == PRIMARY


def _load(path):
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: {"!" + suffix: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
                                                                   else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
                                                                   else loader.construct_mapping(node, deep=True)})
    loader = Loader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


class TestConstantsMatchEcsYaml:
    """The reset values are written down in failback.py. ecs.yaml is where they come from, so a change there fails here."""

    RESOURCES = _load(ECS_TEMPLATE)["Resources"]

    def _of(self, kind):
        return [r["Properties"] for r in self.RESOURCES.values() if r["Type"] == kind]

    def test_every_service_starts_at_the_reset_desired_count(self):
        services = self._of("AWS::ECS::Service")
        assert len(services) == 6 and {s["DesiredCount"] for s in services} == {failback.RESET_DESIRED_COUNT}

    def test_every_scalable_target_has_the_reset_minimum_and_a_maximum_to_keep(self):
        scalable = self._of("AWS::ApplicationAutoScaling::ScalableTarget")
        assert len(scalable) == 6
        assert {t["MinCapacity"] for t in scalable} == {failback.RESET_MIN_CAPACITY}
        assert {t["MaxCapacity"] for t in scalable} == {10}
        assert {t["ScalableDimension"] for t in scalable} == {failback.SCALABLE_DIMENSION}
        assert {t["ServiceNamespace"] for t in scalable} == {"ecs"}

    def test_the_services_are_the_ones_the_tool_resets(self):
        names = {s["ServiceName"]["!Sub"] for s in self._of("AWS::ECS::Service")}
        assert names == {f"{s}${{Env}}" for s in failback.ECS_SERVICES}


# --- the catalog writer -----------------------------------------------------------------------------------------

class TestWriter:

    def test_a_writer_in_the_primary_region_is_left_alone(self):
        fake = failed_over_fake()
        fake.global_writer = PRIMARY
        outcome, lines, _ = fail_back(fake)
        assert outcome.exit_code == 0
        assert "switchover-global-cluster" not in write_ops(fake) and fake.calls_of("describe-db-clusters") == []
        assert "Catalog writer: the catalog writer is in us-east-1 already." in lines

    def test_a_writer_in_the_standby_is_switched_over_to_the_primary_cluster(self):
        fake = failed_over_fake()
        outcome, lines, _ = fail_back(fake)
        ((_, _, region, params),) = [c for c in fake.calls if c[1] == "switchover-global-cluster"]
        assert region == PRIMARY
        assert params == {"global_cluster_identifier": f"catalog-global-db-cluster{ENV}",
                          "target_db_cluster_identifier": db_cluster_arn(PRIMARY)}
        assert fake.global_writer == PRIMARY and outcome.exit_code == 0
        assert any(line.startswith("Catalog writer: moved the catalog writer back to us-east-1") for line in lines)

    def test_it_waits_for_the_switchover_to_finish(self):
        fake = failed_over_fake()
        fake.switchover_script = ["switching-over", "switching-over", "switching-over", "available"]
        outcome, _, clock = fail_back(fake)
        assert outcome.exit_code == 0 and fake.global_writer == PRIMARY and clock.now >= 45

    def test_the_writer_goes_to_the_plans_primary_region_even_when_the_standby_is_failed_back(self):
        fake = failed_over_fake()
        fake.health_checks = {PRIMARY: "healthy", STANDBY: "unhealthy"}
        fail_back(fake, STANDBY)
        assert fake.global_writer == PRIMARY

    def test_it_waits_for_an_old_primary_to_rejoin_and_then_switches_over(self):
        fake = failed_over_fake()
        fake.global_members = {STANDBY: "available"}          # failed over by a person: the old primary has not rejoined
        fake.at_global_poll(4, lambda: fake.global_members.update({PRIMARY: "available"}))
        outcome, lines, clock = fail_back(fake)
        assert outcome.exit_code == 0 and fake.global_writer == PRIMARY
        assert any("is not a member of" in line and "Waiting up to 45 min" in line for line in lines)
        assert clock.now >= 3 * 15

    def test_it_waits_while_the_old_primary_is_still_catching_up(self):
        fake = failed_over_fake()
        fake.global_members = {PRIMARY: "resyncing", STANDBY: "available"}
        fake.at_global_poll(3, lambda: fake.global_members.update({PRIMARY: "available"}))
        outcome, lines, _ = fail_back(fake)
        assert outcome.exit_code == 0 and fake.global_writer == PRIMARY
        assert any("the cluster in us-east-1 is resyncing" in line for line in lines)

    def test_it_waits_while_the_global_cluster_is_busy(self):
        fake = failed_over_fake()
        fake.global_cluster_status = "failing-over"
        fake.at_global_poll(2, lambda: setattr(fake, "global_cluster_status", "available"))
        outcome, lines, _ = fail_back(fake)
        assert outcome.exit_code == 0 and fake.global_writer == PRIMARY
        assert len(fake.calls_of("switchover-global-cluster")) == 1        # asked once, when it was possible, not tried and refused
        assert any("catalog-global-db-cluster-t is failing-over" in line for line in lines)

    def test_it_tells_each_reason_once(self):
        fake = failed_over_fake()
        fake.global_members = {STANDBY: "available"}
        _, lines, _ = fail_back(fake)
        assert len([line for line in lines if "is not a member of" in line]) == 1

    def test_it_gives_up_after_forty_five_minutes_and_leaves_the_writer_where_it_is(self):
        fake = failed_over_fake()
        fake.global_members = {STANDBY: "available"}          # never rejoins
        outcome, _, clock = fail_back(fake)
        assert outcome.exit_code == 3 and outcome.failed == []
        (left,) = outcome.left
        assert "still in us-west-2 after 45 min" in left and "not a member of" in left
        assert f"make failback REGION={PRIMARY}" in left
        assert "switchover-global-cluster" not in write_ops(fake) and fake.global_writer == STANDBY
        assert 45 * 60 <= clock.now < 46 * 60

    def test_the_wait_can_be_shortened(self):
        fake = failed_over_fake()
        fake.global_members = {STANDBY: "available"}
        outcome, _, clock = fail_back(fake, writer_wait_minutes=5)
        assert outcome.exit_code == 3 and "after 5 min" in outcome.left[0] and clock.now < 6 * 60

    def test_a_cluster_that_is_a_member_but_never_available_leaves_the_command_to_finish_by_hand(self):
        fake = failed_over_fake()
        fake.global_members = {PRIMARY: "resyncing", STANDBY: "available"}
        outcome, _, _ = fail_back(fake)
        (left,) = outcome.left
        assert "the cluster in us-east-1 is resyncing" in left
        assert f"aws rds switchover-global-cluster --global-cluster-identifier catalog-global-db-cluster{ENV} "
        assert f"--target-db-cluster-identifier {db_cluster_arn(PRIMARY)}" in left

    def test_aurora_not_being_ready_is_retried(self):
        fake = failed_over_fake()
        fake.fail_on("rds", "switchover-global-cluster", "An error occurred (InvalidGlobalClusterStateFault): not ready", times=2)
        outcome, lines, _ = fail_back(fake)
        assert outcome.exit_code == 0 and fake.global_writer == PRIMARY
        assert len(fake.calls_of("switchover-global-cluster")) == 3
        assert any("Aurora is not ready to switch over" in line for line in lines)

    def test_any_other_error_is_a_failure_and_is_not_retried(self):
        fake = failed_over_fake()
        fake.fail_on("rds", "switchover-global-cluster", "An error occurred (AccessDenied): no", times=1)
        outcome, _, _ = fail_back(fake)
        assert outcome.exit_code == 1 and "moving the catalog writer back" in outcome.failed[0] and "AccessDenied" in outcome.failed[0]
        assert len(fake.calls_of("switchover-global-cluster")) == 1

    def test_a_switchover_that_never_finishes_is_left_for_the_operator(self):
        fake = failed_over_fake()
        fake.switchover_script = ["switching-over"]
        outcome, _, clock = fail_back(fake)
        assert outcome.exit_code == 3
        (left,) = outcome.left
        assert "was accepted but" in left and "switching-over" in left and "describe-global-clusters" in left
        assert clock.now >= 20 * 60

    def test_a_missing_global_cluster_is_a_failure(self):
        fake = failed_over_fake()
        fake.fail_on("rds", "describe-global-clusters", "An error occurred (GlobalClusterNotFoundFault): gone")
        outcome, _, _ = fail_back(fake)
        assert outcome.exit_code == 1 and "GlobalClusterNotFoundFault" in outcome.failed[0]


# --- the command and the make target ---------------------------------------------------------------------------------

def cli_main(fake, *extra, region=PRIMARY):
    clock = Clock()
    code = cli.main(["failback", *ARGS, "--region", region, *extra], aws=fake, now_seconds=NOW.timestamp(), sleep=clock.sleep, clock=clock)
    return code, clock


class TestCli:

    def test_done_exits_zero(self, capsys):
        code, _ = cli_main(failed_over_fake())
        out = capsys.readouterr()
        assert code == 0 and "Fail-back of us-east-1 is done." in out.out and out.err == ""

    def test_refused_exits_two_and_lists_every_reason(self, capsys):
        fake = failed_over_fake()
        fake.alarms[(PRIMARY, f"journey-lcl-home-{PRIMARY}{ENV}")]["state"] = "ALARM"
        fake.health_checks = {PRIMARY: "unhealthy", STANDBY: "unhealthy"}
        code, _ = cli_main(fake)
        out = capsys.readouterr().out
        assert code == 2 and "refused, 2 reason(s); nothing was changed:" in out and fake.writes() == []

    def test_a_failed_activation_exits_one_with_the_reason(self, capsys):
        fake = failed_over_fake()
        fake.execution_script = ["failed"]
        code, _ = cli_main(fake)
        err = capsys.readouterr().err
        assert code == 1 and "is failed" in err and "left alone" in err

    def test_a_writer_left_behind_exits_three(self, capsys):
        fake = failed_over_fake()
        fake.global_members = {STANDBY: "available"}
        code, _ = cli_main(fake, "--writer-wait-minutes", "2")
        out = capsys.readouterr().out
        assert code == 3 and "Left for you:" in out and "is not finished" in out

    def test_a_failed_step_exits_one_and_says_so(self, capsys):
        fake = failed_over_fake()
        fake.fail_on("ecs", "update-service", "AccessDeniedException", times=1)
        code, _ = cli_main(fake)
        captured = capsys.readouterr()
        assert code == 1 and "AccessDeniedException" in captured.err and "is not finished" in captured.out

    def test_credentials_that_do_not_work_exit_one_before_anything_else(self, capsys):
        fake = failed_over_fake()
        fake.fail_on("sts", "get-caller-identity", "ExpiredToken", times=1)
        code, _ = cli_main(fake)
        assert code == 1 and "the credentials do not work" in capsys.readouterr().err and fake.writes() == []

    def test_it_needs_no_ngrh_stack(self, capsys):
        fake = failed_over_fake()
        fake.outputs = None                      # make ngrh was never run
        code, _ = cli_main(fake)
        assert code == 0 and capsys.readouterr().err == ""

    def test_an_unknown_region_exits_one(self, capsys):
        code, _ = cli_main(failed_over_fake(), region="eu-west-1")
        assert code == 1 and "neither the primary Region" in capsys.readouterr().err

    def test_ctrl_c_says_nothing_is_undone(self, capsys, monkeypatch):
        fake = failed_over_fake()

        def interrupt(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(failback, "run", interrupt)
        code, _ = cli_main(fake)
        assert code == cli.EXIT_INTERRUPTED and "safe to repeat" in capsys.readouterr().out

    def test_the_options_default_to_the_documented_waits(self):
        args = cli.parser().parse_args(["failback", *ARGS, "--region", PRIMARY])
        assert (args.poll_seconds, args.writer_wait_minutes) == (failback.POLL_SECONDS, failback.WRITER_WAIT_MINUTES == 45 and 45)


class TestAwsCommandLine:

    def test_an_option_named_service_does_not_collide_with_the_service_argument(self):
        # ecs update-service takes --service. AwsCli.call took `service` as a parameter name, so the first version of
        # the capacity step died with "got multiple values for argument 'service'" on the real layer too.
        args = AwsCli.command("ecs", "update-service", PRIMARY, cluster="c", service="orders-t", desired_count=2)
        assert args[:3] == ["aws", "ecs", "update-service"]
        assert args[args.index("--service") + 1] == "orders-t" and args[args.index("--desired-count") + 1] == "2"

    def test_the_region_goes_in_as_the_regional_endpoint(self):
        args = AwsCli.command("arc-region-switch", "start-plan-execution", STANDBY, plan_arn="p", target_region=STANDBY,
                              action="activate", mode="graceful")
        assert args[args.index("--region") + 1] == STANDBY and args[args.index("--target-region") + 1] == STANDBY


README = (DEPLOYMENT.parent / "README.md").read_text()


class TestReadme:
    """The README says what the command does and for how long, so a number changed in the tool has to change there."""

    SECTION = README[README.index("### 3. Automatic failover and fail-back"):]
    SECTION = SECTION[:SECTION.index("\n## ")]

    def test_the_command_and_its_required_argument_are_shown(self):
        assert "make failback REGION=<Region>" in self.SECTION and "must be on the command line" in self.SECTION

    def test_the_waits_and_the_reset_values_are_the_tools(self):
        assert f"have been `OK` for {failback.STABLE_MINUTES} minutes" in self.SECTION
        assert f"and waits ({failback.ACTIVATE_WAIT_MINUTES} minutes at most)" in self.SECTION
        assert f"waits up to {failback.WRITER_WAIT_MINUTES} minutes" in self.SECTION
        assert f"back to {failback.RESET_DESIRED_COUNT} in both Regions" in self.SECTION

    def test_the_exit_codes_are_the_tools(self):
        assert "exits 0 when done, 1 when a step failed, 2 when preflight refused (nothing was changed), and 3 when it finished except" in self.SECTION
        outcome = failback.Outcome()
        assert outcome.exit_code == 0
        assert failback.Outcome(failed=["x"]).exit_code == 1 and failback.Outcome(left=["x"]).exit_code == 3
        assert cli.EXIT_REFUSED == 2

    def test_it_warns_that_the_console_activation_is_only_the_dns_step(self):
        assert "Activating the Region in the ARC console runs only the DNS step" in self.SECTION


class TestMakeTarget:

    def _recipe(self):
        lines = MAKEFILE.read_text().split("\n")
        start = next(i for i, line in enumerate(lines) if re.match(r"^failback\s*:", line))
        return "\n".join(line for line in lines[start + 1:start + 8] if line.startswith("\t"))

    def test_region_must_come_from_the_command_line(self):
        recipe = self._recipe()
        assert '$(origin REGION)' in recipe and '"command line"' in recipe and "exit 2" in recipe

    def test_it_runs_the_tool_with_the_deployment_arguments_and_the_region(self):
        assert 'python3 -m ngrh_testing failback $(NGRH_TESTING_ARGS) --region "$(REGION)"' in self._recipe()
