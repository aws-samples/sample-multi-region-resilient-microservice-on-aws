# SPDX-License-Identifier: MIT-0
"""Preflight (design 5.9): refuse a run, with every reason, unless what it depends on is in place.

``run_checks`` returns a ``Result``: the ``Refusal``s (empty when the run may go ahead) and notes about
checks that had nothing to look at. Each refusal carries the number of the check in design 5.9 it
belongs to. ``live`` does all of them; ``static`` leaves out the checks about what is happening right
now (alarm states, active runs, FIS experiments, plan executions), which is what CI wants right after
a deploy. A check that cannot run because an AWS call failed is a refusal too: a preflight that cannot
see is not a pass.

The sentinel (check 1) is the credential lookup every command starts with. Check 8, which only the
recovery test needs (triggers present, 60 minutes since the last execution, capacity headroom), arrives
with that test in step 11.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence, Tuple

from . import api, context, reconcile, spec
from .aws import AwsCli, AwsCliError
from .context import ContextError, Environment, ResolvedTest

LIVE = "live"
STATIC = "static"
MODES = (LIVE, STATIC)

TESTING_POLICY = "AWSResilienceHubResilienceTestingPolicy"
SIDECAR = "amazon-ssm-agent"  # the FIS SSM agent sidecar container in every task definition (ecs.yaml)

ACTIVE_FIS = ("pending", "initiating", "running", "stopping")
# ARC Region Switch execution states (ListPlanExecutions): a plan still working, and one that is done.
RUNNING_PLAN = ("inProgress", "pausedByFailedStep", "pausedByOperator", "pendingManualApproval", "pending")
FINISHED_PLAN = ("completed", "completedWithExceptions", "completedMonitoringApplicationHealth")


@dataclass(frozen=True)
class Refusal:
    check: int  # the number in design 5.9
    reason: str
    test: str = ""  # the test it concerns; empty when it concerns the account or every test

    def __str__(self) -> str:
        return f"[{self.check}] " + (f"{self.test}: " if self.test else "") + self.reason


@dataclass
class Result:
    refusals: List[Refusal] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.refusals


def _seeing(check: int, what: str, fn: Callable[[], List[Refusal]], test: str = "") -> List[Refusal]:
    try:
        return fn()
    except AwsCliError as e:
        return [Refusal(check, f"could not check {what}: {e}", test)]


# --- 3: the roles ----------------------------------------------------------------------------

def check_roles(aws: AwsCli, env: Environment) -> List[Refusal]:
    out: List[Refusal] = []
    try:
        invoker, experiment = env.invoker_role_name, env.experiment_role_name
    except ContextError as e:
        return [Refusal(3, p) for p in e.problems]
    try:
        attached = {p["PolicyName"] for p in aws.call("iam", "list-attached-role-policies", role_name=invoker).get("AttachedPolicies", [])}
    except AwsCliError as e:
        out.append(Refusal(3, f"could not read the invoker role {invoker}: {e}"))
    else:
        if TESTING_POLICY not in attached:
            out.append(Refusal(3, f"the invoker role {invoker} does not have {TESTING_POLICY} attached; deploy the current ngrh.yaml (make ngrh)"))
    try:
        aws.call("iam", "get-role", role_name=experiment)
    except AwsCliError as e:
        out.append(Refusal(3, f"the experiment role {experiment} does not exist or cannot be read: {e}"))
    return out


# --- 2: the tests match the spec ---------------------------------------------------------------

def check_templates(aws: AwsCli, env: Environment, tests: Sequence[ResolvedTest]) -> List[Refusal]:
    return [Refusal(2, p) for p in reconcile.template_problems(aws, env, tests)]


def check_drift(aws: AwsCli, env: Environment, tests: Sequence[ResolvedTest]) -> List[Refusal]:
    out: List[Refusal] = []
    for t in tests:
        try:
            plan = reconcile.plan_test(aws, env, t)
        except reconcile.ReconcileError as e:
            out.extend(Refusal(2, p) for p in e.problems)
            continue
        except AwsCliError as e:
            out.append(Refusal(2, f"could not read the test: {e}", t.name))
            continue
        if plan.action == "create":
            out.append(Refusal(2, "the test does not exist yet; run make ngrh-tests", t.name))
        elif not plan.in_sync:
            what = []
            if plan.differences:
                what.append("settings (" + ", ".join(plan.differences) + ")")
            if plan.put_sources or plan.delete_sources:
                what.append(f"sources (+{len(plan.put_sources)} -{len(plan.delete_sources)})")
            out.append(Refusal(2, f"test {plan.test_id} differs from the spec in {' and '.join(what)}; run make ngrh-tests", t.name))
    return out


# --- 4: the alarms -----------------------------------------------------------------------------

def check_alarms(aws: AwsCli, tests: Sequence[ResolvedTest], live: bool) -> List[Refusal]:
    """Every alarm must exist (metric or composite). Live, the success and stop alarms must also be OK:
    a success alarm already in ALARM at the start makes the run fail, and a stop alarm in ALARM stops it at once."""
    out: List[Refusal] = []
    states: Dict[Tuple[str, str], str] = {}
    for region, names in sorted(reconcile.wanted_alarms(tests).items()):
        found = api.describe_alarms(aws, region, sorted(names))
        for name in sorted(names):
            if name in found:
                states[(region, name)] = found[name]["state"]
            else:
                out.append(Refusal(4, f"alarm {name} does not exist in {region}"))
    if live:
        for t in tests:
            for kind, alarms in (("success", t.success), ("stop", t.stop)):
                for alarm in alarms:
                    state = states.get((alarm.region, alarm.name))
                    if state is not None and state != "OK":
                        out.append(Refusal(4, f"{kind} alarm {alarm.name} is {state}, not OK, in {alarm.region}", t.name))
    return out


def check_alarm_scope(aws: AwsCli, env: Environment, tests: Sequence[ResolvedTest], notes: List[str]) -> List[Refusal]:
    """Resilience Hub takes only the alarms it discovered for the service as test sources, and it finds
    a service's alarms by the tags its input sources name (orders: ``service`` is orders or shared).
    A source alarm tagged for another service is never discovered, and StartTestRun then refuses the
    whole run with "alarms not discovered for this service": the orders test's ``hop-checkout-errors``,
    tagged checkout, did so on 2026-10-07, after a fresh assessment had looked at every orders alarm.
    Only success and observability alarms are sources. Stop conditions and evidence alarms are not,
    and are not checked here. A service no tag scopes gets a note instead of a verdict."""
    out: List[Refusal] = []
    for t in tests:
        scope = api.service_tag_scope(aws, env.ngrh_region, t.service_arn)
        if not scope:
            notes.append(f"{t.name}: service {t.test.service} has no tag input source, so which alarms it discovers is not checked")
            continue
        accepted = " or ".join(f"{key}={'|'.join(sorted(values))}" for key, values in scope)
        for kind, alarms in (("success", t.success), ("observability", t.observability)):
            for alarm in alarms:
                try:
                    found = api.alarm_tags(aws, alarm.region, alarm.arn)
                except AwsCliError as e:
                    if "ResourceNotFound" not in str(e):
                        raise
                    continue  # it does not exist, which check 4 reports
                if any(found.get(key) in values for key, values in scope):
                    continue
                has = ", ".join(f"{k}={v}" for k, v in sorted(found.items()) if k in {key for key, _ in scope}) or "none of those tags"
                out.append(Refusal(4, f"{kind} source alarm {alarm.name} is tagged {has}, but service {t.test.service} discovers only "
                                      f"alarms tagged {accepted}; Resilience Hub would refuse the run (alarms not discovered). "
                                      f"Use an alarm of that service or a shared one as a source; this one can stay in the evidence alarms", t.name))
    return out


# --- 5, 6, 7: nothing else is running -------------------------------------------------------------

def check_no_active_runs(aws: AwsCli, env: Environment) -> List[Refusal]:
    """One active run per service is Resilience Hub's own limit, and the faults share resources, so
    any active run on any service in the account refuses, testers' own services included."""
    out: List[Refusal] = []
    for arn in api.list_service_arns(aws, env.ngrh_region):
        try:
            runs = api.active_runs(aws, env.ngrh_region, [arn])
        except AwsCliError as e:
            if "ResourceNotFoundException" in str(e):  # deleted while we looked
                continue
            raise
        for _, run in runs:
            out.append(Refusal(5, f"service {arn.rsplit('/', 1)[-1]} has test run {run['testRunId']} {run['status']} (started {run.get('startedAt', '?')}); "
                                  "wait for it, or stop it (make ngrh-test-stop if it is ours)"))
    return out


def check_no_fis_experiments(aws: AwsCli, env: Environment) -> List[Refusal]:
    out: List[Refusal] = []
    for region in env.regions:
        for experiment in aws.call("fis", "list-experiments", region).get("experiments", []):
            status = experiment["state"]["status"]
            if status in ACTIVE_FIS:
                out.append(Refusal(6, f"FIS experiment {experiment['id']} is {status} in {region}"))
    return out


def check_plan_executions(aws: AwsCli, env: Environment, notes: List[str]) -> List[Refusal]:
    """No plan execution in progress at either Regional endpoint, and no Region left deactivated by an
    earlier one. ``executionRegion`` is read as the Region the execution activated or deactivated."""
    outputs = context.stack_outputs(aws, env.primary_region, f"region-switch{env.env}")
    plan_arn = (outputs or {}).get("RegionSwitchPlanArn")
    if not plan_arn:
        notes.append(f"stack region-switch{env.env} is not deployed in {env.primary_region}: no plan executions to check")
        return []
    out: List[Refusal] = []
    executions: Dict[str, Dict[str, str]] = {}
    for region in env.regions:
        try:
            items = aws.call("arc-region-switch", "list-plan-executions", region, plan_arn=plan_arn).get("items", [])
        except AwsCliError as e:
            out.append(Refusal(7, f"could not list plan executions at the {region} endpoint: {e}"))
            continue
        executions.update({e["executionId"]: e for e in items})
    for e in sorted(executions.values(), key=lambda e: str(e["startTime"])):
        if e["executionState"] in RUNNING_PLAN:
            out.append(Refusal(7, f"plan execution {e['executionId']} ({e['executionAction']} {e['executionRegion']}) is {e['executionState']}"))
    state: Dict[str, Dict[str, str]] = {}
    for e in sorted(executions.values(), key=lambda e: str(e["startTime"])):
        if e["executionState"] in FINISHED_PLAN and e["executionAction"] in ("activate", "deactivate"):
            state[e["executionRegion"]] = e
    for region, e in sorted(state.items()):
        if e["executionAction"] == "deactivate":
            out.append(Refusal(7, f"{region} was deactivated by plan execution {e['executionId']} and not activated since; "
                                  f"run make failback REGION={region} first"))
    return out


# --- 9: the service under test -------------------------------------------------------------------

def check_service_configuration(aws: AwsCli, env: Environment, t: ResolvedTest) -> List[Refusal]:
    """The faulted Region's ECS service must be able to take the fault: its current task definition has
    the FIS sidecar, PidMode task and EnableFaultInjection, the service runs on on-demand Fargate only
    (a reclaimed Spot task fails a run), and ECS Exec is off."""
    region = t.fault_region
    cluster = aws.call("cloudformation", "describe-stack-resource", region, stack_name=f"apps{env.env}",
                       logical_resource_id="EcsCluster")["StackResourceDetail"]["PhysicalResourceId"]
    name = spec.ECS_SERVICE_NAMES[t.test.service] + env.env
    services = aws.call("ecs", "describe-services", region, cluster=cluster, services=[name]).get("services", [])
    if not services or services[0].get("status") == "INACTIVE":
        return [Refusal(9, f"ECS service {name} is not running in cluster {cluster} in {region}", t.name)]
    service = services[0]
    task = aws.call("ecs", "describe-task-definition", region, task_definition=service["taskDefinition"])["taskDefinition"]
    where = f"{name} in {region} (task definition {task['family']}:{task['revision']})"
    problems = []
    if SIDECAR not in [c["name"] for c in task.get("containerDefinitions", [])]:
        problems.append(f"{where} has no {SIDECAR} sidecar container")
    if task.get("pidMode") != "task":
        problems.append(f"{where} does not have PidMode task")
    if not task.get("enableFaultInjection"):
        problems.append(f"{where} does not have EnableFaultInjection on")
    providers = {s["capacityProvider"] for s in service.get("capacityProviderStrategy", [])}
    if providers - {"FARGATE"} or (not providers and service.get("launchType") != "FARGATE"):
        problems.append(f"{name} in {region} is not on on-demand Fargate only "
                        f"({', '.join(sorted(providers)) or service.get('launchType') or 'no capacity provider or launch type'})")
    if service.get("enableExecuteCommand"):
        problems.append(f"{name} in {region} has ECS Exec on")
    return [Refusal(9, p, t.name) for p in problems]


# --- all of them -----------------------------------------------------------------------------------

def run_checks(aws: AwsCli, env: Environment, tests: Sequence[spec.Test], mode: str = LIVE) -> Result:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    result = Result()
    refusals = result.refusals
    try:
        resolved = context.resolve_all(aws, env, tests)
    except ContextError as e:
        resolved = []
        refusals.extend(Refusal(10 if "lookup" in p else 2, p) for p in e.problems)
    refusals.extend(check_roles(aws, env))
    if resolved:
        refusals.extend(_seeing(2, "which templates Resilience Hub offers", lambda: check_templates(aws, env, resolved)))
        refusals.extend(_seeing(4, "the alarms", lambda: check_alarms(aws, resolved, mode == LIVE)))
        refusals.extend(_seeing(4, "which alarms each service discovers", lambda: check_alarm_scope(aws, env, resolved, result.notes)))
        refusals.extend(check_drift(aws, env, resolved))
        for t in resolved:
            refusals.extend(_seeing(9, "the service's configuration", lambda t=t: check_service_configuration(aws, env, t), t.name))
    if mode == LIVE:
        refusals.extend(_seeing(5, "the active test runs", lambda: check_no_active_runs(aws, env)))
        refusals.extend(_seeing(6, "the FIS experiments", lambda: check_no_fis_experiments(aws, env)))
        refusals.extend(_seeing(7, "the plan executions", lambda: check_plan_executions(aws, env, result.notes)))
    return result


def render(result: Result, mode: str, names: Sequence[str]) -> str:
    head = f"Preflight ({mode}) for {', '.join(names)}: " + ("all checks passed" if result.passed else f"refused, {len(result.refusals)} reason(s)")
    lines = [head] + [f"  {r}" for r in result.refusals] + [f"  note: {n}" for n in result.notes]
    return "\n".join(lines) + "\n"
