# SPDX-License-Identifier: MIT-0
"""Create and update the tests from the spec, and delete them at teardown (design 5.9 and 5.11).

CreateTest has no client token and no caller-chosen name, so the spec is reconciled client side:
a service has at most one test per template, so the test for a spec entry is the one on its service
with its template's ARN. Absent, it is created; present, it is updated only where it differs, and its
sources are made to match. Two tests for one service and template stop the run with their ids: nothing
is ever deleted automatically, except by ``delete_tests`` at teardown.

Every read happens before the first write, so a reference that doesn't resolve, a template or alarm
that doesn't exist, or a duplicate stops the run with nothing changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import api, context, spec
from .aws import AwsCli, AwsCliError
from .context import ContextError, Environment, ResolvedTest

Source = Tuple[str, str]  # (kind, alarm ARN)


class ReconcileError(RuntimeError):
    """The tests can't be reconciled or deleted as asked; ``problems`` says why, one line each."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass
class Plan:
    """What reconciling one spec test would do."""

    resolved: ResolvedTest
    test_id: Optional[str]  # None: the test doesn't exist yet
    differences: List[str] = field(default_factory=list)  # which settings of an existing test differ
    delete_sources: List[Source] = field(default_factory=list)
    put_sources: List[Source] = field(default_factory=list)

    @property
    def action(self) -> str:
        if self.test_id is None:
            return "create"
        return "update" if self.differences else "unchanged"

    @property
    def in_sync(self) -> bool:
        return self.action == "unchanged" and not self.delete_sources and not self.put_sources

    def describe(self) -> str:
        parts = []
        if self.test_id is None:
            parts.append("to create")
        elif self.differences:
            parts.append(f"test {self.test_id} differs in " + ", ".join(self.differences))
        else:
            parts.append(f"test {self.test_id} matches")
        if self.put_sources or self.delete_sources:
            parts.append(f"sources +{len(self.put_sources)} -{len(self.delete_sources)}")
        return f"{self.resolved.name}: " + "; ".join(parts)


def _comparable_parameters(parameters: Optional[Dict[str, Sequence[str]]]) -> Dict[str, List[str]]:
    return {key: sorted(values) for key, values in (parameters or {}).items()}


def _comparable_stop_conditions(conditions: Optional[Sequence[Dict[str, str]]]) -> Set[Tuple[str, str]]:
    return {(c["source"], c["value"]) for c in conditions or [] if c["source"] != "none"}


def differences(resolved: ResolvedTest, current: Dict[str, Any]) -> List[str]:
    """Which of the settings reconcile owns differ between the spec and an existing test. Logging is
    compared on the keys the spec sets only, so a default NGRH adds is not drift."""
    out = []
    if _comparable_parameters(current.get("parameters")) != _comparable_parameters(resolved.parameters):
        out.append("parameters")
    if current.get("roleName") != resolved.role_name:
        out.append("role")
    logging = current.get("loggingConfiguration") or {}
    if any(logging.get(key) != value for key, value in resolved.logging_configuration.items()):
        out.append("logging")
    if _comparable_stop_conditions(current.get("stopConditions")) != _comparable_stop_conditions(resolved.stop_conditions):
        out.append("stop conditions")
    return out


def verify_references(aws: AwsCli, env: Environment, tests: Sequence[ResolvedTest]) -> None:
    """The template and every alarm a test names must exist. Raises ContextError naming each that doesn't."""
    problems = []
    offered = api.list_test_templates(aws, env.ngrh_region)
    for t in tests:
        if t.template_arn not in offered:
            problems.append(f"{t.name}: template {t.test.template} is not offered in {env.ngrh_region}")
    wanted: Dict[str, Set[str]] = {}
    for t in tests:
        for region, names in t.alarms_by_region().items():
            wanted.setdefault(region, set()).update(names)
    for region in sorted(wanted):
        existing = api.describe_alarms(aws, region, sorted(wanted[region]))
        problems.extend(f"alarm {name} does not exist in {region}" for name in sorted(wanted[region] - set(existing)))
    if problems:
        raise ContextError(problems)


def plan_test(aws: AwsCli, env: Environment, resolved: ResolvedTest) -> Plan:
    region = env.ngrh_region
    matching = [t for t in api.list_tests(aws, region, resolved.service_arn) if t["testTemplateArn"] == resolved.template_arn]
    if len(matching) > 1:
        ids = ", ".join(sorted(t["testId"] for t in matching))
        raise ReconcileError([
            f"{resolved.name}: service {resolved.test.service} has {len(matching)} tests for {resolved.test.template} ({ids}); "
            "keep one and delete the others by hand, nothing is deleted automatically"
        ])
    wanted = resolved.sources()
    if not matching:
        return Plan(resolved, None, put_sources=sorted(wanted))
    test_id = matching[0]["testId"]
    current = api.get_test(aws, region, resolved.service_arn, test_id)
    have = api.list_sources(aws, region, resolved.service_arn, test_id)
    return Plan(resolved, test_id, differences(resolved, current), sorted(have - wanted), sorted(wanted - have))


def plan_tests(aws: AwsCli, env: Environment, tests: Sequence[spec.Test]) -> List[Plan]:
    """Resolve and read everything, write nothing."""
    resolved = context.resolve_all(aws, env, tests)
    verify_references(aws, env, resolved)
    return [plan_test(aws, env, r) for r in resolved]


def apply_plan(aws: AwsCli, env: Environment, plan: Plan) -> str:
    r, region = plan.resolved, env.ngrh_region
    settings: Dict[str, Any] = {"parameters": r.parameters, "role_name": r.role_name,
                                "logging_configuration": r.logging_configuration}
    done = []
    test_id = plan.test_id
    try:
        if test_id is None:
            if r.stop_conditions:
                settings["stop_conditions"] = r.stop_conditions
            created = aws.call(api.SERVICE, "create-test", region, service_arn=r.service_arn,
                               test_template_arn=r.template_arn, **settings)
            test_id = created["test"]["testId"]
            done.append(f"created test {test_id}")
        elif plan.differences:
            aws.call(api.SERVICE, "update-test", region, service_arn=r.service_arn, test_id=test_id,
                     stop_conditions=r.stop_conditions, **settings)
            done.append(f"updated test {test_id} ({', '.join(plan.differences)})")
        else:
            done.append(f"test {test_id} unchanged")
        if plan.delete_sources:
            aws.call(api.SERVICE, "delete-test-sources", region, service_arn=r.service_arn, test_id=test_id,
                     test_sources=api.source_inputs(plan.delete_sources))
        if plan.put_sources:
            aws.call(api.SERVICE, "put-test-sources", region, service_arn=r.service_arn, test_id=test_id,
                     test_sources=api.source_inputs(plan.put_sources))
        if plan.delete_sources or plan.put_sources:
            done.append(f"sources +{len(plan.put_sources)} -{len(plan.delete_sources)}")
    except AwsCliError as e:
        hint = ""
        if "active test run" in str(e):
            hint = " (a run is active on this service: wait for it or make ngrh-test-stop)"
        raise ReconcileError([f"{r.name}: {e}{hint}"] + ([f"already done: {', '.join(done)}"] if done else [])) from e
    return f"{r.name}: " + "; ".join(done)


def reconcile(aws: AwsCli, env: Environment, tests: Sequence[spec.Test], write: bool = True) -> Tuple[List[Plan], List[str]]:
    """Reconcile the tests, or with ``write=False`` only say what would change. Returns the plans
    and one line for each test."""
    plans = plan_tests(aws, env, tests)
    if not write:
        return plans, [p.describe() for p in plans]
    return plans, [apply_plan(aws, env, p) for p in plans]


# --- teardown ---------------------------------------------------------------------------------

def _gone(error: AwsCliError) -> bool:
    return "ResourceNotFoundException" in str(error)


def _tests_of(aws: AwsCli, region: str, service_arn: str) -> List[Dict[str, Any]]:
    """The tests of a service; a service that no longer exists has none."""
    try:
        return api.list_tests(aws, region, service_arn)
    except AwsCliError as e:
        if _gone(e):
            return []
        raise


def delete_tests(aws: AwsCli, primary_region: str, env_suffix: str) -> List[str]:
    """Delete every test on the ngrh stack's services (design 5.11), refusing while any run is
    active, and confirm none remain. A stack that is already gone, or a service that is, has no tests."""
    outputs = context.stack_outputs(aws, primary_region, env_suffix)
    if outputs is None:
        return [f"stack ngrh{env_suffix} does not exist in {primary_region}: no tests to delete"]
    arns = [a for a in outputs.get("ServiceArns", "").split(",") if a]
    active = []
    for arn in arns:
        try:
            active.extend(api.active_runs(aws, primary_region, [arn]))
        except AwsCliError as e:
            if not _gone(e):
                raise
    if active:
        raise ReconcileError([f"service {arn} has test run {run['testRunId']} {run['status']}; stop it first "
                              "(make ngrh-test-stop) or wait for it to end" for arn, run in active])
    lines = []
    for arn in arns:
        for test in _tests_of(aws, primary_region, arn):
            aws.call(api.SERVICE, "delete-test", primary_region, service_arn=arn, test_id=test["testId"])
            lines.append(f"deleted test {test['testId']} ({test['testTemplateArn'].rsplit('/', 1)[-1]}) of {arn.rsplit('/', 1)[-1]}")
    left = [(arn, t["testId"]) for arn in arns for t in _tests_of(aws, primary_region, arn)]
    if left:
        raise ReconcileError([f"test {test_id} of {arn} is still there after its delete" for arn, test_id in left])
    return lines or ["no tests to delete"]
