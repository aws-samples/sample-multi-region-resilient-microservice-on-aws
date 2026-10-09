# SPDX-License-Identifier: MIT-0
"""Region Switch plan executions during a test run, and the run-level checks built on them (design 5.8, 6.2).

Resilience Hub's verdict says whether the success alarms recovered; it does not say how. The recovery test
means to show that the plan moved the traffic, so the report lists every plan execution that started while the
run was going, with its steps and ARC's own measurement of the recovery time, and the run-level checks the spec
names look at that list. The isolation test means to show that a Region keeps serving when cut off from its peer,
so its checks are that no execution started and that the peer's own health alarm stayed OK, which reads the
evidence alarms' history as well. ``run`` also reads the executions to know which Regions the run's failover
deactivated, so that ``make failback`` can bring them back.

An endpoint lists only the executions that ran at it (api.plan_executions reads every endpoint and merges). A deactivate
of A runs at the endpoint of the Region that stays, B, and an activate of A at A's own: of 56 distinct executions in two
accounts, read on 2026-10-08, every one was listed at one endpoint, every activate had ``executionRegion`` equal to that
endpoint, and every deactivate had the other Region there. ``executionRegion`` is therefore the Region the execution
targets, as ``StartPlanExecution``'s ``targetRegion`` is. The other endpoint answers GetPlanExecution for such an
execution with ResourceNotFoundException. So the detail is read from the endpoint of the surviving Region first for a
deactivate (the Region being deactivated may be the one in trouble) and from the target Region's for an activate; the
other endpoint is only a fallback, for an execution a trigger started that turns out to run elsewhere (not observed
yet: every trigger-started execution seen so far was an activate).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import api
from .aws import AwsCli, AwsCliError
from .context import Environment, ResolvedAlarm, ResolvedTest
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


def _at(moment: Any) -> str:
    return parse_time(moment).strftime("%H:%M:%S")


# What ARC wrote as the comment of the executions its triggers started in test4 (read 2026-10-08): this fixed text and
# the trigger's description in square brackets. Only activates have been seen, so a trigger-started deactivate may differ,
# and a comment that does not match is described as it stands instead of being called a person's.
TRIGGER_COMMENT = re.compile(r"^Execution started by automated trigger \[(.*)\]$", re.S)


def deactivate_completed(t: ResolvedTest, found: Sequence[Dict[str, Any]], problems: Sequence[str],
                         evidence: Sequence[Dict[str, Any]] = ()) -> CheckResult:
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


def _how_it_started(e: Dict[str, Any]) -> str:
    comment = e.get("comment") or ""
    trigger = TRIGGER_COMMENT.match(comment)
    if trigger:
        return f"started by the trigger [{trigger.group(1)}]"
    return f"started with the comment \"{comment}\"" if comment else "started without a comment"


def no_plan_execution(t: ResolvedTest, found: Sequence[Dict[str, Any]], problems: Sequence[str],
                      evidence: Sequence[Dict[str, Any]] = ()) -> CheckResult:
    """No plan execution started during the run. The isolation test cuts a Region off from its peer, which turns only the
    peer's view of it red (the rmt journey alarms); a trigger needs the Region's own journeys red too. So a run that shows
    the Region keeps serving starts no execution. One that does start one is either the plan doing its job (a journey
    failed locally, a trigger fired, and the run is FAILED as well: the comment names the journey) or a finding (the run
    passed and something started the plan anyway). The detail says which executions and how they started, and the report
    lists their steps."""
    name = "no-plan-execution"
    if problems:
        return CheckResult(name, False, "could not read the plan's executions, so this is not known: " + "; ".join(problems))
    if not found:
        return CheckResult(name, True, "no plan execution started during the run, so no failover started")
    lines = [f"execution {e['executionId']} ({e['action']} {e['region']}, {e['mode'] or 'graceful'}, {e['state']}) at "
             f"{_at(e['startTime'])}, {_how_it_started(e)}" for e in found]
    head = "a plan execution started during the run: " if len(found) == 1 else f"{len(found)} plan executions started during the run: "
    return CheckResult(name, False, head + "; ".join(lines) + ". If the isolated Region's own journeys failed, this is the plan doing its job "
                                    "and the trigger's description names the journey; if they stayed OK, nothing should have started it")


def destination_alarm(t: ResolvedTest) -> Optional[ResolvedAlarm]:
    """region-degraded in the destination Region, the alarm the check below reads; None when the test has no destination or
    does not list the alarm as evidence (spec.parse refuses a spec that names the check without it)."""
    values = t.parameters.get("destinationRegion") or []
    if not values:
        return None
    return next((a for a in t.evidence if a.region == values[0] and a.name.startswith("region-degraded-")), None)


def destination_stayed_ok(t: ResolvedTest, found: Sequence[Dict[str, Any]], problems: Sequence[str],
                          evidence: Sequence[Dict[str, Any]] = ()) -> CheckResult:
    """region-degraded in the destination Region stayed OK: the Region the isolated one was cut off from is the one traffic
    would move to, so if the cut or the failover it starts unsettled it, the test says nothing about the isolated Region
    alone. The history is the evidence the report reads, from a minute before the run until ten minutes after it ended. A
    history with no change in it means the alarm stayed in the state it started in, which a live preflight requires to be
    OK (check 4), so no change is read as OK."""
    name = "destination-stayed-ok"
    alarm = destination_alarm(t)
    if alarm is None:
        return CheckResult(name, False, "this test lists no region-degraded alarm for its destination Region, so there is nothing to read")
    row = next((r for r in evidence if r.get("alarm") == alarm.name and r.get("region") == alarm.region), None)
    if row is None or row.get("transitions") is None:
        return CheckResult(name, False, f"the history of {alarm.name} could not be read, so this is not known")
    transitions = row["transitions"]
    states = ([transitions[0]["from"]] + [tr["to"] for tr in transitions]) if transitions else ["OK"]
    if all(s == "OK" for s in states):
        return CheckResult(name, True, f"{alarm.name} stayed OK through the run and the window after it")
    changes = "; ".join(f"{tr['from']} -> {tr['to']} at {_at(tr['time'])}" for tr in transitions)
    return CheckResult(name, False, f"{alarm.name} did not stay OK: {changes}. {alarm.region} is where the isolated Region's traffic would "
                                    "go, so it has to be healthy for this test to show anything about the isolated one")


Check = Callable[[ResolvedTest, Sequence[Dict[str, Any]], Sequence[str], Sequence[Dict[str, Any]]], CheckResult]

CHECKS: Dict[str, Check] = {
    "deactivate-completed": deactivate_completed,
    "no-plan-execution": no_plan_execution,
    "destination-stayed-ok": destination_stayed_ok,
}


def evaluate(t: ResolvedTest, found: Sequence[Dict[str, Any]], problems: Sequence[str],
             evidence: Sequence[Dict[str, Any]] = ()) -> List[CheckResult]:
    """The test's run-level checks, in the order the spec lists them. ``evidence`` is the report's evidence rows (an alarm,
    its Region and its state changes), for the checks that read alarm history."""
    return [CHECKS[name](t, found, problems, evidence) for name in t.test.run_checks]
