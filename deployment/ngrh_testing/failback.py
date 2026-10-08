# SPDX-License-Identifier: MIT-0
"""Fail a Region back (design 5.10): serve from it again after the failover plan moved traffic away from it.

``make failback REGION=<Region>`` does four things in order:

1. Preflight. The Region's own journeys (lcl) and the other Region's view of it (rmt) must have been OK for ten
   minutes, no plan execution may be in progress or paused (a paused switchover holds the plan until a person
   resolves it), and the plan's Route 53 health checks must not all be unhealthy. Every reason is listed.
2. Activate. Start the plan's activate workflow for the Region, graceful, at that Region's own endpoint, and wait
   for it. If the Region's health check is healthy already there is nothing to activate. An execution that fails,
   pauses or runs out of time stops everything here: capacity and the writer are not touched while DNS is in doubt.
3. Reset capacity. The failover scaled the remaining Region up and nothing scales it down again, so every
   service's desired count and Application Auto Scaling minimum go back to what ecs.yaml declares, in both
   Regions. The maximum is left as it is.
4. Move the catalog writer back to the plan's primary Region with a switchover, which Aurora does without losing
   data. After an automatic failover the clusters are in sync and it takes minutes. After a person chose to fail the
   database over, the old primary first has to rejoin the global cluster and catch up, so this waits for it, up to
   45 minutes. If it has not by then the writer stays where it is, which is safe, and the command says how to finish.

Every step reads before it writes and writes only what differs, so running it again after a partial failure
repeats nothing that is done. Nothing here can lose data: what could, failing the database over ungracefully, is a
person's decision in the paused run (README) and is never taken here.

Exit codes (cli.py): 0 done; 1 a step failed; 2 preflight refused and nothing was changed; 3 done except for what
the output says is left for the operator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import alarms, api, context, spec
from .aws import AwsCli, AwsCliError
from .context import Environment
from .report import parse_time

POLL_SECONDS = 15
STABLE_MINUTES = 10  # how long the Region's alarms must have been OK before traffic goes back
ACTIVATE_WAIT_MINUTES = 10  # the activate workflow is one DNS step with a 5-minute limit
WRITER_WAIT_MINUTES = 45  # how long to wait for the old primary to rejoin and catch up (design 5.10)
SWITCHOVER_WAIT_MINUTES = 20  # how long to wait for a switchover Aurora accepted

# What ecs.yaml declares for every service. tests/test_ngrh_testing_failback.py reads ecs.yaml and fails when these drift.
RESET_DESIRED_COUNT = 2
RESET_MIN_CAPACITY = 2
ECS_SERVICES = tuple(spec.ECS_SERVICE_NAMES.values())  # ui, catalog, carts, checkout, orders, assets
SCALABLE_DIMENSION = "ecs:service:DesiredCount"

# ARC execution states (GetPlanExecution). The others are still going.
SUCCEEDED = ("completed", "completedMonitoringApplicationHealth")
PAUSED = ("pausedByFailedStep", "pausedByOperator", "pendingManualApproval")
ENDED_BADLY = ("failed", "canceled", "planExecutionTimedOut", "completedWithExceptions")

# Aurora's answers to a switchover it is not ready for: wait and ask again.
NOT_READY = ("InvalidGlobalClusterStateFault", "InvalidDBClusterStateFault")


class FailbackRefused(RuntimeError):
    """Preflight found reasons not to go ahead, and nothing was changed. ``problems`` has one line for each."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(problems))


class FailbackError(RuntimeError):
    """A step failed in a way that ends the command."""


@dataclass
class Outcome:
    failed: List[str] = field(default_factory=list)  # steps that failed: exit 1
    left: List[str] = field(default_factory=list)  # what the operator still has to do: exit 3

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 3 if self.left else 0


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _elapsed(seconds: float) -> str:
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


def other_region(env: Environment, region: str) -> str:
    if region not in env.regions:
        raise ValueError(f"{region} is neither the primary Region ({env.primary_region}) nor the standby ({env.standby_region})")
    return env.standby_region if region == env.primary_region else env.primary_region


def global_cluster_id(env: Environment) -> str:
    return f"catalog-global-db-cluster{env.env}"


# --- 1: preflight ---------------------------------------------------------------------------------

def journey_alarms(env: Environment, region: str) -> List[Tuple[str, str]]:
    """(Region, alarm name) of the alarms that say whether a Region is fit to serve again: its own view of each
    journey (lcl), and the other Region's view of it (rmt)."""
    peer = other_region(env, region)
    return ([(region, alarms.journey_alarm_name("lcl", j, region, env.env)) for j in alarms.JOURNEYS]
            + [(peer, alarms.journey_alarm_name("rmt", j, peer, env.env)) for j in alarms.JOURNEYS])


def alarm_problems(aws: AwsCli, env: Environment, region: str, now: datetime, stable_minutes: float = STABLE_MINUTES) -> List[str]:
    """Each alarm must exist, be OK now, and not have changed state in the last ``stable_minutes``: an alarm that
    went OK a minute ago is a Region that has only just stopped failing."""
    wanted = journey_alarms(env, region)
    problems: List[str] = []
    states: Dict[Tuple[str, str], str] = {}
    for r in sorted({r for r, _ in wanted}):
        names = [n for rr, n in wanted if rr == r]
        found = api.describe_alarms(aws, r, names)
        for n in names:
            if n in found:
                states[(r, n)] = found[n]["state"]
            else:
                problems.append(f"alarm {n} does not exist in {r}; deploy the current monitoring.yml (make monitoring)")
    for (r, n), state in sorted(states.items()):
        if state != "OK":
            problems.append(f"{n} ({r}) is {state}, not OK")
            continue
        recent = api.alarm_state_updates(aws, r, n, _iso(now - timedelta(minutes=stable_minutes)), _iso(now))
        if recent:
            last = max(parse_time(item["Timestamp"]) for item in recent)
            problems.append(f"{n} ({r}) changed state {_elapsed((now - last).total_seconds())} ago, at {last:%H:%M:%S} UTC; "
                            f"it has to stay OK for {stable_minutes:g} min")
    return problems


def execution_problems(aws: AwsCli, env: Environment, plan_arn: str) -> List[str]:
    """No plan execution may be going, at either endpoint. A paused one counts: it holds the plan."""
    executions, problems = api.plan_executions(aws, plan_arn, env.regions)
    for e in executions:
        if e["executionState"] in api.RUNNING_PLAN:
            hint = " (it holds the plan until a person resolves it: see the README)" if e["executionState"] in PAUSED else ""
            problems.append(f"plan execution {e['executionId']} ({e['executionAction']} {e['executionRegion']}) is {e['executionState']}{hint}")
    return problems


def health_problems(env: Environment, region: str, statuses: Dict[str, List[str]]) -> List[str]:
    """DNS answers with a Region only while the plan's health checks for it are healthy. When every Region's checks
    are unhealthy nothing is being served and failing one Region back is not the fix. And when the Region asked for
    is serving while the other is not, the other is the one that needs failing back."""
    missing = [r for r in env.regions if not statuses.get(r)]
    if missing:
        return [f"the plan reports no Route 53 health check for {', '.join(missing)}; the wiring step of make region-switch has not run"]
    if all(s == "unhealthy" for r in env.regions for s in statuses[r]):
        return ["both of the plan's Route 53 health checks are unhealthy, so DNS names no Region; a person has to decide which to restore"]
    peer = other_region(env, region)
    if all(s == "healthy" for s in statuses[region]) and any(s == "unhealthy" for s in statuses[peer]):
        return [f"{region} is serving already (its health check is healthy) and {peer} is the Region DNS has moved away from; "
                f"did you mean make failback REGION={peer}?"]
    return []


def preflight(aws: AwsCli, env: Environment, region: str, plan_arn: str, now: datetime) -> List[str]:
    problems: List[str] = []
    checks: List[Tuple[str, Callable[[], List[str]]]] = [
        ("the alarms", lambda: alarm_problems(aws, env, region, now)),
        ("the plan's executions", lambda: execution_problems(aws, env, plan_arn)),
        ("the plan's health checks", lambda: health_problems(env, region, api.route53_health_checks(aws, plan_arn, env.primary_region))),
    ]
    for what, check in checks:
        try:
            problems.extend(check())
        except AwsCliError as e:
            problems.append(f"could not check {what}: {e}")
    return problems


# --- 2: activate -----------------------------------------------------------------------------------

def activate(aws: AwsCli, env: Environment, plan_arn: str, region: str, progress: Callable[[str], None],
             sleep: Callable[[float], None], clock: Callable[[], float], poll_seconds: float = POLL_SECONDS,
             wait_minutes: float = ACTIVATE_WAIT_MINUTES) -> str:
    """Start the activate workflow for the Region and wait for it. Returns the line for the summary."""
    statuses = api.route53_health_checks(aws, plan_arn, env.primary_region).get(region, [])
    if statuses and all(s == "healthy" for s in statuses):
        return f"{region} is active in DNS already (the plan's health check for it is healthy): nothing to activate"
    started = aws.call("arc-region-switch", "start-plan-execution", region, plan_arn=plan_arn, target_region=region,
                       action="activate", mode="graceful", comment=f"make failback REGION={region}")
    execution_id = started["executionId"]
    progress(f"Started plan execution {execution_id}: activate {region}, graceful.")
    look = f"aws arc-region-switch get-plan-execution --plan-arn {plan_arn} --execution-id {execution_id} --region {region}"
    began, last = clock(), None
    while True:
        state = aws.call("arc-region-switch", "get-plan-execution", region, plan_arn=plan_arn,
                         execution_id=execution_id)["executionState"]
        waited = clock() - began
        if state != last:
            progress(f"{execution_id}: {state} after {_elapsed(waited)}")
            last = state
        if state in SUCCEEDED:
            return f"activated {region} with plan execution {execution_id} ({_elapsed(waited)})"
        if state in ENDED_BADLY or state in PAUSED:
            raise FailbackError(f"plan execution {execution_id} is {state}, so {region} is not back in DNS, and capacity and the "
                                f"writer were left alone. Look at it with: {look}. Once it is resolved or cancelled, run "
                                f"make failback REGION={region} again")
        if waited >= wait_minutes * 60:
            raise FailbackError(f"plan execution {execution_id} is still {state} after {wait_minutes:g} min. It goes on in AWS; "
                                f"look at it with: {look}. Capacity and the writer were left alone; run make failback "
                                f"REGION={region} again once it has ended")
        sleep(poll_seconds)


# --- 3: reset capacity ------------------------------------------------------------------------------

def reset_capacity(aws: AwsCli, env: Environment, progress: Callable[[str], None]) -> Tuple[List[str], List[str]]:
    """Put every service's desired count and Auto Scaling minimum back, in both Regions. Returns the changes made
    and the problems. A service that fails does not stop the others: they are independent."""
    changes: List[str] = []
    problems: List[str] = []
    for region in env.regions:
        cluster = api.ecs_cluster(aws, region, env.env)
        names = [f"{s}{env.env}" for s in ECS_SERVICES]
        resource_ids = {n: f"service/{cluster}/{n}" for n in names}
        targets = {t["ResourceId"]: t for t in aws.call(
            "application-autoscaling", "describe-scalable-targets", region, service_namespace="ecs",
            scalable_dimension=SCALABLE_DIMENSION, resource_ids=list(resource_ids.values())).get("ScalableTargets", [])}
        services = {s["serviceName"]: s for s in aws.call("ecs", "describe-services", region, cluster=cluster,
                                                          services=names).get("services", [])}
        for name in names:
            target, service = targets.get(resource_ids[name]), services.get(name)
            if target is None or service is None:
                problems.append(f"{name} in {region}: " + ("no scalable target" if target is None else "no ECS service") + ", so its capacity was not reset")
                continue
            done: List[str] = []
            try:
                if target["MinCapacity"] != RESET_MIN_CAPACITY:
                    # The minimum first: while it is above the desired count Auto Scaling would put the count back.
                    aws.call("application-autoscaling", "register-scalable-target", region, service_namespace="ecs",
                             scalable_dimension=SCALABLE_DIMENSION, resource_id=resource_ids[name],
                             min_capacity=RESET_MIN_CAPACITY, max_capacity=target["MaxCapacity"])
                    done.append(f"minimum {target['MinCapacity']} to {RESET_MIN_CAPACITY}")
                if service["desiredCount"] != RESET_DESIRED_COUNT:
                    aws.call("ecs", "update-service", region, cluster=cluster, service=name, desired_count=RESET_DESIRED_COUNT)
                    done.append(f"desired count {service['desiredCount']} to {RESET_DESIRED_COUNT}")
            except AwsCliError as e:
                problems.append(f"{name} in {region}: {e}")
            if done:
                changes.append(f"{name} in {region}: " + ", ".join(done))
                progress(changes[-1])
    return changes, problems


# --- 4: move the writer back -------------------------------------------------------------------------

def _global_cluster(aws: AwsCli, env: Environment) -> Dict[str, Any]:
    clusters = aws.call("rds", "describe-global-clusters", env.primary_region,
                        global_cluster_identifier=global_cluster_id(env)).get("GlobalClusters", [])
    if not clusters:
        raise FailbackError(f"the global cluster {global_cluster_id(env)} does not exist")
    return clusters[0]


def _members(cluster: Dict[str, Any]) -> List[Tuple[str, str, bool]]:
    """(cluster ARN, Region, is writer) of each member."""
    return [(m["DBClusterArn"], m["DBClusterArn"].split(":")[3], bool(m.get("IsWriter")))
            for m in cluster.get("GlobalClusterMembers", [])]


def move_writer(aws: AwsCli, env: Environment, progress: Callable[[str], None], sleep: Callable[[float], None],
                clock: Callable[[], float], poll_seconds: float = POLL_SECONDS,
                wait_minutes: float = WRITER_WAIT_MINUTES) -> Tuple[str, Optional[str]]:
    """Switch the catalog database over to the plan's primary Region unless its writer is there. Returns the line
    for the summary and, when the writer could not be moved in time, what is left for the operator."""
    gc_id = global_cluster_id(env)
    began, told = clock(), None
    while True:
        cluster = _global_cluster(aws, env)
        members = _members(cluster)
        writer = next((r for _, r, w in members if w), None)
        if writer == env.primary_region:
            return f"the catalog writer is in {env.primary_region} already", None
        target = next((arn for arn, r, _ in members if r == env.primary_region), None)
        if target is None:
            reason = f"the cluster in {env.primary_region} is not a member of {gc_id}"
        elif cluster.get("Status") != "available":
            reason = f"{gc_id} is {cluster.get('Status')}"
        else:
            status = aws.call("rds", "describe-db-clusters", env.primary_region, db_cluster_identifier=target)["DBClusters"][0]["Status"]
            if status != "available":
                reason = f"the cluster in {env.primary_region} is {status}"
            else:
                try:
                    aws.call("rds", "switchover-global-cluster", env.primary_region, global_cluster_identifier=gc_id,
                             target_db_cluster_identifier=target)
                except AwsCliError as e:
                    if not any(fault in str(e) for fault in NOT_READY):
                        raise
                    reason = "Aurora is not ready to switch over: " + str(e).splitlines()[0]
                else:
                    progress(f"Switching the catalog database over to {env.primary_region}.")
                    return _wait_for_switchover(aws, env, progress, sleep, clock, poll_seconds)
        if reason != told:
            progress(f"The catalog writer is in {writer or 'no Region'}, not {env.primary_region}. Waiting up to {wait_minutes:g} min "
                     f"for it to be possible to move it back: {reason}.")
            told = reason
        if clock() - began >= wait_minutes * 60:
            by_hand = (f"aws rds switchover-global-cluster --global-cluster-identifier {gc_id} --target-db-cluster-identifier {target} "
                       f"--region {env.primary_region}" if target else "")
            return (f"the catalog writer is still in {writer or 'no Region'}",
                    f"the catalog writer is still in {writer or 'no Region'} after {wait_minutes:g} min: {reason}. That is safe: each Region "
                    f"reads its own cluster, so only catalog writes are affected. Run make failback REGION={env.primary_region} again once "
                    f"the cluster in {env.primary_region} is available" + (f", or switch over by hand: {by_hand}" if by_hand else ""))
        sleep(poll_seconds)


def _wait_for_switchover(aws: AwsCli, env: Environment, progress: Callable[[str], None], sleep: Callable[[float], None],
                         clock: Callable[[], float], poll_seconds: float) -> Tuple[str, Optional[str]]:
    began = clock()
    while True:
        cluster = _global_cluster(aws, env)
        writer = next((r for _, r, w in _members(cluster) if w), None)
        waited = clock() - began
        if writer == env.primary_region and cluster.get("Status") == "available":
            return f"moved the catalog writer back to {env.primary_region} ({_elapsed(waited)})", None
        if waited >= SWITCHOVER_WAIT_MINUTES * 60:
            left = (f"the switchover to {env.primary_region} was accepted but {global_cluster_id(env)} is {cluster.get('Status')} with its writer "
                    f"in {writer or 'no Region'} after {SWITCHOVER_WAIT_MINUTES} min; check it with: aws rds describe-global-clusters "
                    f"--global-cluster-identifier {global_cluster_id(env)} --region {env.primary_region}")
            return "the catalog switchover has not finished", left
        sleep(poll_seconds)


# --- all of it ----------------------------------------------------------------------------------------

def run(aws: AwsCli, env: Environment, region: str, progress: Callable[[str], None],
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
        now: Optional[Callable[[], datetime]] = None, poll_seconds: float = POLL_SECONDS,
        writer_wait_minutes: float = WRITER_WAIT_MINUTES) -> Outcome:
    """Fail ``region`` back. Raises FailbackRefused when preflight finds reasons not to (nothing is changed) and
    FailbackError when the activation fails; otherwise returns what was left or failed after the later steps."""
    other_region(env, region)  # refuses a Region that is not one of the two
    now = now or (lambda: datetime.now(timezone.utc))
    plan_arn = context.region_switch_plan_arn(aws, env.primary_region, env.env)
    if not plan_arn:
        raise FailbackRefused([f"stack region-switch{env.env} is not deployed in {env.primary_region}, so there is no plan to activate {region} with"])
    problems = preflight(aws, env, region, plan_arn, now())
    if problems:
        raise FailbackRefused(problems)
    progress(f"Preflight passed: {region} and its view from {other_region(env, region)} have been OK for {STABLE_MINUTES} min, "
             f"no plan execution is going, DNS names a Region.")
    outcome = Outcome()
    progress("Activate: " + activate(aws, env, plan_arn, region, progress, sleep, clock, poll_seconds) + ".")

    try:
        changes, problems = reset_capacity(aws, env, progress)
    except AwsCliError as e:
        changes, problems = [], [f"could not read the capacity: {e}"]
    if changes:
        progress(f"Capacity: reset {len(changes)} of {len(ECS_SERVICES) * len(env.regions)} service-Regions.")
    elif not problems:
        progress("Capacity: already at the reset values.")
    outcome.failed.extend("capacity: " + p for p in problems)

    try:
        line, left = move_writer(aws, env, progress, sleep, clock, poll_seconds, writer_wait_minutes)
    except (AwsCliError, FailbackError) as e:
        outcome.failed.append(f"moving the catalog writer back: {e}")
    else:
        progress("Catalog writer: " + line + ".")
        if left:
            outcome.left.append(left)
    return outcome
