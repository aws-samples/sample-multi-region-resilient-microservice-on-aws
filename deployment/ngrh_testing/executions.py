# SPDX-License-Identifier: MIT-0
"""Region Switch plan executions during a test run, and the run-level checks built on them (design 5.8, 6.2).

Resilience Hub's verdict says whether the success alarms recovered; it does not say how. The recovery test
means to show that the plan moved the traffic, so the report lists every plan execution that started while the
run was going, with its steps and ARC's own measurement of the recovery time, and the run-level checks the spec
names look at that list. ``run`` also reads it to know which Regions the run's failover deactivated, so that
``make failback`` can bring them back.

An execution is listed at both Regional endpoints, so the list is merged by id (api.plan_executions). Its detail
is read from the endpoint of the surviving Region first for a deactivate, because the Region being deactivated may
be the one in trouble, and from the target Region's for an activate, which runs there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import api
from .aws import AwsCli, AwsCliError
from .context import Environment, ResolvedTest
from .timeutil import parse_time

# An execution can start a moment before the run's own start time says: the two clocks are not the same.
SLACK = timedelta(minutes=1)

DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_duration(text: Any) -> Optional[float]:
    """ARC's ISO 8601 durations (PT7M30S) in seconds; None for anything else, years and months included."""
    match = DURATION.match(str(text)) if text else None
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _t(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def summarize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """The parts of GetPlanExecution the report and the checks use, in plain types."""
    reports = []
    for generated in raw.get("generatedReportDetails") or []:
        output = generated.get("reportOutput") or {}
        reports.append({"generatedAt": _t(generated.get("reportGenerationTime")),
                        "s3ObjectKey": (output.get("s3ReportOutput") or {}).get("s3ObjectKey"),
                        "failure": output.get("failedReportOutput")})
    return {
        "executionId": raw["executionId"], "action": raw["executionAction"], "region": raw["executionRegion"],
        "state": raw["executionState"], "mode": raw.get("mode"), "comment": raw.get("comment"),
        "startTime": _t(raw["startTime"]), "endTime": _t(raw.get("endTime")),
        "actualRecoveryTime": raw.get("actualRecoveryTime"), "actualRecoverySeconds": parse_duration(raw.get("actualRecoveryTime")),
        "objectiveMinutes": (raw.get("plan") or {}).get("recoveryTimeObjectiveMinutes"),
        "steps": [{"name": s.get("name"), "status": s.get("status"), "startTime": _t(s.get("startTime")),
                   "endTime": _t(s.get("endTime")), "mode": s.get("stepMode")} for s in raw.get("stepStates") or []],
        "reports": reports,
    }


def endpoints_for(action: str, region: str, regions: Sequence[str]) -> List[str]:
    """The endpoints to ask about an execution, best first. A deactivate is asked about at the Region that stays, an
    activate at the Region it activates."""
    peer = [r for r in regions if r != region]
    return peer + [region] if action == "deactivate" else [region] + peer


def describe(aws: AwsCli, plan_arn: str, listed: Dict[str, Any], regions: Sequence[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    """The execution in detail, and a line saying why not when no endpoint would give it (the summary from the list
    stands in, without steps)."""
    errors = []
    for endpoint in endpoints_for(listed["executionAction"], listed["executionRegion"], regions):
        try:
            return summarize(aws.call("arc-region-switch", "get-plan-execution", endpoint, plan_arn=plan_arn,
                                      execution_id=listed["executionId"])), None
        except AwsCliError as e:
            errors.append(f"{endpoint}: {e}")
    summary = summarize({**listed, "stepStates": None})
    summary["steps"] = None
    return summary, f"plan execution {listed['executionId']} could not be read ({'; '.join(errors)})"


def in_window(aws: AwsCli, env: Environment, plan_arn: str, start: datetime, end: datetime) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The plan's executions that started between ``start`` (less a minute) and ``end``, oldest first, in detail, and
    one line for each thing that could not be read."""
    listed, problems = api.plan_executions(aws, plan_arn, env.regions)
    found = []
    for item in listed:
        if start - SLACK <= parse_time(item["startTime"]) <= end:
            detail, problem = describe(aws, plan_arn, item, env.regions)
            found.append(detail)
            if problem:
                problems.append(problem)
    return found, problems


def deactivated_regions(found: Sequence[Dict[str, Any]]) -> List[str]:
    """The Regions the run's executions deactivated, whatever state they ended in, in the order they started: each is a
    Region ``make failback`` may have to bring back."""
    out: List[str] = []
    for e in found:
        if e["action"] == "deactivate" and e["region"] not in out:
            out.append(e["region"])
    return out


# --- run-level checks ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


def deactivate_completed(t: ResolvedTest, found: Sequence[Dict[str, Any]], problems: Sequence[str]) -> CheckResult:
    """A deactivate of the impaired Region started during the run and completed. The recovery test passes when the
    success alarms come back in time; without this check a run could pass because the fault did not bite, or because a
    person moved the traffic by hand, and show nothing about the triggers."""
    name, region = "deactivate-completed", t.fault_region
    if problems:
        return CheckResult(name, False, "could not read the plan's executions, so this is not known: " + "; ".join(problems))
    mine = [e for e in found if e["action"] == "deactivate" and e["region"] == region]
    if not mine:
        return CheckResult(name, False, f"no deactivate of {region} started during the run, so the traffic did not move because of the plan. "
                                        "If the plan has no triggers (AUTOMATIC_FAILOVER=disabled) a person has to start it; otherwise the "
                                        "trigger conditions were not met, and the journey alarms under Evidence alarms show why")
    done = [e for e in mine if e["state"] in api.SUCCEEDED_PLAN]
    if not done:
        last = mine[-1]
        why = {"completedWithExceptions": "a step was skipped or failed and the run went on",
               "pausedByFailedStep": "a step failed and the execution waits for a person (it holds the plan until then)",
               "pausedByOperator": "an operator paused it", "pendingManualApproval": "it waits for an approval",
               "inProgress": "it had not finished when this was checked"}.get(last["state"], "it did not complete")
        return CheckResult(name, False, f"execution {last['executionId']} deactivating {region} is {last['state']}: {why}")
    e = done[-1]
    took = (f"; ARC measured a recovery time of {_minutes(e['actualRecoverySeconds'])}"
            + (f" against the objective of {e['objectiveMinutes']} min" if e.get("objectiveMinutes") else "")) if e.get("actualRecoverySeconds") else ""
    return CheckResult(name, True, f"execution {e['executionId']} deactivated {region} ({e['mode'] or 'graceful'}) and {e['state']}{took}")


CHECKS: Dict[str, Callable[[ResolvedTest, Sequence[Dict[str, Any]], Sequence[str]], CheckResult]] = {
    "deactivate-completed": deactivate_completed,
}


def evaluate(t: ResolvedTest, found: Sequence[Dict[str, Any]], problems: Sequence[str]) -> List[CheckResult]:
    """The test's run-level checks, in the order the spec lists them."""
    return [CHECKS[name](t, found, problems) for name in t.test.run_checks]
