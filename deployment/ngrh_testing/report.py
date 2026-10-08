# SPDX-License-Identifier: MIT-0
"""The report for one test run (design 6.2): a JSON file with everything the run recorded, and a markdown
summary a person can read first.

Resilience Hub keeps a run's configuration snapshot, its timeline, the per-alarm outcomes and the
resolved targets, but access to them can later be narrower than the execution role's, so the report
copies them while the caller still can. On top of that it adds what Resilience Hub does not show:
the FIS experiments, and the state changes of the evidence alarms (the two Regions' ``region-degraded``
and every hop alarm of the faulted Region) laid out by hop, ui first and then each back-end, so a journey
that failed can be traced to the service behind ui that caused it.

A part that cannot be read is recorded under ``gaps`` instead of stopping the report: a report with a hole
in it is worth more than none.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import api, context, executions, spec
from .aws import AwsCli, AwsCliError
from .context import Environment, ResolvedAlarm, ResolvedTest
from .timeutil import parse_time as _parse_time

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"

REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ngrh-test-reports")

# Evidence alarm history is read from a minute before the run to ten minutes after it ended, so the
# recovery that follows a fault shows.
BEFORE = timedelta(minutes=1)
AFTER = timedelta(minutes=10)

HOP_NAME = re.compile(r"^hop-([a-z]+)-(errors|slow)-")


def observed(status: str) -> str:
    """The verdict a run's status amounts to. STOPPED and ERROR are not verdicts: the test did not finish."""
    return {"PASSED": PASS, "FAILED": FAIL}.get(status, INCONCLUSIVE)


def fault_problem(run: Dict[str, Any], experiments: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> Optional[str]:
    """Why the fault never ran, or None when nothing says it failed to.

    Resilience Hub ends a run FAILED when the sample failed the test and also when the fault could not be
    injected at all. On 2026-10-07 FIS refused the ECS packet-loss action with InvalidTarget because no task
    of orders was registered with SSM (the sidecar image had been overwritten): nothing was blocked, no
    alarm moved, and the run still ended FAILED, which the report took for the expected FAIL. A run whose
    fault did not run says nothing about the application, so it is not a verdict.

    The marks of it are a FIS experiment that ended ``failed`` (an experiment the stop condition ended is
    ``stopped``) and an ``action_failed`` event. The explanation comes from the run's own error message
    first, as it names the action, then from FIS, then from the event."""
    failed = [x for x in experiments if (((x.get("experiment") or {}).get("state") or {}).get("status")) == "failed"]
    actions = [e for e in events if str(e.get("eventType", "")).lower() == "action_failed"]
    if not failed and not actions:
        return None
    reasons = [run.get("errorMessage")] + [x["experiment"]["state"].get("reason") for x in failed] + [e.get("message") for e in actions]
    return next((r for r in reasons if r), "FIS reported the experiment failed without saying why")


def matches(expected: str, seen: str) -> bool:
    """UNKNOWN expects nothing in particular: any run that reached a verdict is recorded as expected."""
    return seen == expected or (expected == "UNKNOWN" and seen != INCONCLUSIVE)


def parse_time(value: Any) -> datetime:
    """Kept here for failback and the tests; the helper itself is in timeutil."""
    return _parse_time(value)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clock(value: Any) -> str:
    return parse_time(value).astimezone(timezone.utc).strftime("%H:%M:%S")


def latest_run_id(aws: AwsCli, env: Environment, t: ResolvedTest, test_id: str) -> str:
    runs = api.list_test_runs(aws, env.ngrh_region, t.service_arn, test_id)
    if not runs:
        raise ValueError(f"{t.name}: the test has no runs yet")
    return max(runs, key=lambda r: parse_time(r["startedAt"]))["testRunId"]


def _hop(alarm: ResolvedAlarm) -> Optional[str]:
    """The service behind ui that a hop alarm watches, None for any other alarm."""
    hop = HOP_NAME.match(alarm.name)
    return hop.group(1) if hop else None


def _hop_order(alarm: ResolvedAlarm) -> Tuple[int, int, str]:
    """Where an evidence alarm sits in the layout: Region-level alarms first in the order given, then
    ui, then each back-end, errors before slow."""
    hop = HOP_NAME.match(alarm.name)
    if not hop:
        return (0, 0, "")
    return (1, spec.HOP_SERVICES.index(hop.group(1)) * 2 + (0 if hop.group(2) == "errors" else 1), alarm.name)


def _transitions(aws: AwsCli, alarm: ResolvedAlarm, start: datetime, end: datetime) -> List[Dict[str, str]]:
    # describe-alarm-history returns metric alarms only unless composite alarms are asked for too.
    history = aws.call("cloudwatch", "describe-alarm-history", alarm.region, alarm_name=alarm.name,
                       alarm_types=api.ALARM_TYPES, history_item_type="StateUpdate",
                       start_date=_iso(start), end_date=_iso(end))
    out = []
    for item in history.get("AlarmHistoryItems", []):
        try:
            data = json.loads(item.get("HistoryData") or "{}")
            before, after = data["oldState"]["stateValue"], data["newState"]["stateValue"]
        except (ValueError, KeyError):
            before, after = "?", "?"
        out.append({"time": item["Timestamp"], "from": before, "to": after, "summary": item.get("HistorySummary", "")})
    return sorted(out, key=lambda x: parse_time(x["time"]))


def collect(aws: AwsCli, env: Environment, t: ResolvedTest, run_id: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read everything the run recorded. Nothing is written to AWS."""
    now = now or datetime.now(timezone.utc)
    region, gaps = env.ngrh_region, []

    def read(what: str, fn: Callable[[], Any], default: Any) -> Any:
        try:
            return fn()
        except AwsCliError as e:
            gaps.append(f"{what}: {e}")
            return default

    def hub(operation: str, key: str, **params: Any) -> List[Dict[str, Any]]:
        return aws.call(api.SERVICE, operation, region, service_arn=t.service_arn, test_run_id=run_id, **params).get(key, [])

    run = aws.call(api.SERVICE, "get-test-run", region, service_arn=t.service_arn, test_run_id=run_id)["testRun"]
    sources = read("sources", lambda: hub("list-test-run-sources", "testRunSources"), [])
    source_events = {}
    for source in sources:
        arn = (source.get("successCriteriaAlarm") or source.get("observabilityAlarm"))["alarmArn"]
        source_events[arn] = read(f"events of {arn.rsplit(':', 1)[-1]}",
                                  lambda arn=arn: hub("list-test-run-source-events", "testRunSourceEvents", source_arn=arn), [])
    experiments = []
    for experiment in run.get("experiments", []):
        arn = experiment["experimentArn"]
        where = arn.split(":")[3]
        experiments.append({
            "experimentArn": arn, "details": experiment.get("details"),
            "experiment": read(f"FIS experiment {arn.rsplit('/', 1)[-1]}",
                               lambda arn=arn, where=where: aws.call("fis", "get-experiment", where, id=arn.rsplit("/", 1)[-1])["experiment"], None),
        })
    start = parse_time(run["startedAt"]) - BEFORE
    end = (parse_time(run["endedAt"]) if run.get("endedAt") else now) + AFTER
    evidence = [{"alarm": a.name, "region": a.region, "hop": _hop(a),
                 "transitions": read(f"history of {a.name}", lambda a=a: _transitions(aws, a, start, end), None)}
                for a in sorted(t.evidence, key=_hop_order)]
    # What the success and observability alarms did, from CloudWatch: Resilience Hub's own events for a source alarm
    # can lag a minute or miss a change (the run of 2026-10-07 recorded only "Initial state" for an alarm that had
    # gone to ALARM half a minute before the run ended), and it returns no outcome for them at all. An alarm that is
    # also evidence is not read twice, and one that cannot be read is None, which the report says rather than "no change".
    read_already = {row["alarm"]: row["transitions"] for row in evidence}
    watched = {(a.region, a.name): a for a in list(t.success) + list(t.observability)}
    source_history = {name: read_already[name] if name in read_already else read(f"history of {name}", lambda a=a: _transitions(aws, a, start, end), None)
                      for (_, name), a in sorted(watched.items(), key=lambda kv: kv[0][1])}
    seen = observed(run["status"])
    events = read("events", lambda: hub("list-test-run-events", "events"), [])
    problem = fault_problem(run, experiments, events)
    if problem:
        seen = INCONCLUSIVE
    # The plan's executions during the run (the Region Switch plan, not Resilience Hub's test): what moved the traffic,
    # and the run-level checks the spec asks of them.
    started_at = parse_time(run["startedAt"])
    plan_arn = read("the Region Switch plan", lambda: context.region_switch_plan_arn(aws, env.primary_region, env.env), None)
    found: List[Dict[str, Any]] = []
    execution_problems: List[str] = []
    if plan_arn:
        found, execution_problems = executions.in_window(aws, env, plan_arn, started_at, end)
        gaps.extend(execution_problems)
    elif t.test.run_checks:
        execution_problems = [f"stack region-switch{env.env} is not deployed in {env.primary_region}"]
    checks = executions.evaluate(t, found, execution_problems)
    if seen == PASS and any(not c.passed for c in checks):
        seen = FAIL  # Resilience Hub passed it, but the run did not show what the test is for
    return {
        "test": t.name, "service": t.test.service, "template": t.test.template, "testRunId": run_id,
        "expected": t.test.expected, "observed": seen, "matches": matches(t.test.expected, seen),
        "faultNotRun": problem, "generatedAt": _iso(now), "testRun": run,
        # Alarm history is read up to ten minutes after the run ended, so the recovery shows. A report written
        # sooner than that is missing whatever the alarms do in the meantime, and says so. A run that has not ended
        # has its window end ten minutes from now, so its report is never complete.
        "evidenceUntil": _iso(end), "evidenceComplete": now >= end,
        "sourceHistory": source_history,
        "events": events,
        "planExecutions": found, "runChecks": [c.as_dict() for c in checks],
        "sources": sources, "sourceEvents": source_events,
        "resolvedTargets": read("resolved targets", lambda: hub("list-resolved-test-run-target-resources", "resolvedTargetResources"), []),
        "dependencies": read("blocked dependencies", lambda: hub("list-test-run-dependencies", "dependencies"), []),
        "experiments": experiments, "evidence": evidence, "gaps": gaps,
    }


def _source_rows(data: Dict[str, Any]) -> List[str]:
    """One row per success or observability alarm: what Resilience Hub says about it (the API returns no outcome
    for them in some runs) and what CloudWatch's history says it did."""
    rows = []
    history = data.get("sourceHistory", {})
    for source in data["sources"]:
        kind, alarm = ("success", source["successCriteriaAlarm"]) if "successCriteriaAlarm" in source else ("observability", source["observabilityAlarm"])
        changes = ("could not be read" if history.get(alarm["alarmName"]) is None and alarm["alarmName"] in history else
                   "; ".join(f"{tr['from']} -> {tr['to']} {_clock(tr['time'])}" for tr in history.get(alarm["alarmName"], [])) or "no change")
        rows.append(f"| {alarm['alarmName']} | {kind} | {alarm.get('outcome', '-')} | {alarm.get('outcomeReason', '')} | {changes} |")
    return rows


def _timeline(data: Dict[str, Any]) -> List[str]:
    items = [(parse_time(e["timestamp"]), f"{e['eventType']}: {e['message']}") for e in data["events"]]
    for arn, events in data["sourceEvents"].items():
        name = arn.rsplit(":", 1)[-1]
        for e in events:
            detail = e["detail"]
            if "alarmStateChange" in detail:
                change = detail["alarmStateChange"]
                items.append((parse_time(e["timestamp"]), f"{name}: {change.get('previousState', '?')} -> {change['state']}"))
            elif "error" in detail:
                items.append((parse_time(e["timestamp"]), f"{name}: could not be read, {detail['error']['errorCode']} {detail['error'].get('errorMessage', '')}"))
    return [f"- {when.strftime('%H:%M:%S')} {text}" for when, text in sorted(items, key=lambda x: x[0])]


def _evidence_lines(data: Dict[str, Any]) -> List[str]:
    lines = []
    heading = None
    for row in data["evidence"]:
        group = f"{row['hop']} ({row['region']})" if row["hop"] else f"Region health ({row['region']})"
        if group != heading:
            lines += ["", f"### {group}"]
            heading = group
        if row["transitions"] is None:
            lines.append(f"- {row['alarm']}: history could not be read (see Not collected)")
            continue
        if not row["transitions"]:
            lines.append(f"- {row['alarm']}: no state change")
        for tr in row["transitions"]:
            lines.append(f"- {row['alarm']}: {_clock(tr['time'])} {tr['from']} -> {tr['to']}")
    return lines


def _span(start: Any, end: Any) -> str:
    if not start:
        return "not started"
    return f"{_clock(start)} to {_clock(end)}" if end else f"from {_clock(start)}, not ended"


def _execution_lines(data: Dict[str, Any]) -> List[str]:
    """Each plan execution that started during the run: what it did, step by step, and how long ARC says the
    recovery took against the plan's objective."""
    lines: List[str] = []
    for e in data.get("planExecutions") or []:
        head = f"- {e['executionId']}: {e['action']} {e['region']}, {e.get('mode') or 'graceful'}, {e['state']}. {_span(e['startTime'], e.get('endTime'))}"
        if e.get("actualRecoverySeconds") is not None:
            seconds = int(e["actualRecoverySeconds"])
            head += f". Recovery time {seconds // 60} min {seconds % 60} s"
            head += f" against the objective of {e['objectiveMinutes']} min" if e.get("objectiveMinutes") else ""
        if e.get("comment"):
            head += f". Started with the comment \"{e['comment']}\""
        lines.append(head)
        if e.get("steps") is None:
            lines.append("  - its steps could not be read (see Not collected)")
        for step in e.get("steps") or []:
            lines.append(f"  - {step['name']}: {step['status']}, {_span(step.get('startTime'), step.get('endTime'))}")
        for r in e.get("reports") or []:
            lines.append("  - ARC's report: " + (f"s3 key {r['s3ObjectKey']}" if r.get("s3ObjectKey") else f"not written ({r.get('failure')})"))
    return lines or ["- none started during the run"]


def _failback_lines(data: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for fb in data.get("failbacks") or []:
        lines += ["", f"## Fail-back of {fb['region']}", "", *[f"- {line}" for line in fb["lines"] if line.strip()]]
    return lines


def render(data: Dict[str, Any], invoker_role: str = "") -> str:
    run = data["testRun"]
    started = parse_time(run["startedAt"])
    minutes = f"{(parse_time(run['endedAt']) - started).total_seconds() / 60:.1f} min" if run.get("endedAt") else "not ended"
    verdict = ("as expected" if data["matches"] else "NOT as expected")
    problem = data.get("faultNotRun")
    lines = [
        f"# {data['test']}: " + ("INCONCLUSIVE, the fault did not run" if problem else run["status"]),
        "",
        f"Expected {data['expected']}, observed {data['observed']}: {verdict}.",
        "",
        f"- Run {data['testRunId']} on service {data['service']} with {data['template']}",
        f"- Started {started.strftime('%Y-%m-%d %H:%M:%S')} UTC, {minutes}",
        "- Parameters: " + "; ".join(f"{k}={', '.join(v)}" for k, v in sorted((run.get("parameters") or {}).items())),
        f"- Role {run.get('roleName', '-')}, stop conditions: "
        + (", ".join(c["value"].rsplit(":", 1)[-1] for c in run.get("stopConditions", []) if c["source"] != "none") or "none"),
    ]
    if data.get("evidenceComplete") is False:
        lines += ["", f"**This report is early.** The evidence alarms are read until {data['evidenceUntil']}, ten minutes after the run ends, so "
                      f"their recovery shows, and it was written at {data['generatedAt']}: alarms that change in between are missing. "
                      f"Collect it again after that time with `make ngrh-test-report TEST={data['test']} RUN={data['testRunId']}`."]
    if problem:
        lines += ["", f"Resilience Hub ended the run {run['status']}, but the fault never ran, so nothing was done to the application and "
                      f"this says nothing about it: {problem}"]
        if "SSM managed instance" in problem:
            lines += ["", "FIS finds an ECS task through the SSM managed instance its amazon-ssm-agent sidecar registers. Look at the "
                          "sidecar of each task of the service (it should be RUNNING; its log shows why it stopped) and at "
                          "`aws ssm describe-instance-information` in the Region."]
    if run.get("errorMessage") and run["errorMessage"] != problem:
        lines += ["", f"The run reported an error: {run['errorMessage']}"]
    if run["status"] in ("ERROR", "FAILED") and not run.get("errorMessage") and not problem:
        who = f" by role {invoker_role}" if invoker_role else " by the invoker role"
        lines += ["", f"No error message came with the run. Calls denied{who} carry none, so look for AccessDenied events in CloudTrail "
                      "around the start time."]
    lines += ["", "## What Resilience Hub watched", "", "| Alarm | Kind | Outcome | Reason | State changes (CloudWatch, UTC) |", "|---|---|---|---|---|", *_source_rows(data)]
    checks = data.get("runChecks") or []
    if checks:
        failed = [c for c in checks if not c["passed"]]
        if failed and run["status"] == "PASSED" and not problem:
            lines += ["", f"Resilience Hub ended the run PASSED, but {'a run check' if len(failed) == 1 else 'run checks'} failed, so the run "
                          "does not show what this test is for, and the observed result is FAIL."]
        lines += ["", "## Run checks", "", *[f"- {'PASS' if c['passed'] else 'FAIL'} {c['name']}: {c['detail']}" for c in checks]]
    lines += ["", "## Region Switch plan executions during the run", "", *_execution_lines(data)]
    lines += ["", "## Timeline (UTC)", "", *(_timeline(data) or ["- nothing recorded"])]
    lines += ["", "## Evidence alarms, by hop", *(_evidence_lines(data) or ["", "- none configured"])]
    targets = data["resolvedTargets"]
    lines += ["", f"## What the fault reached ({len(targets)} resolved target(s))", ""]
    lines += [f"- {r['resourceType']} {r['targetName']}" for r in targets] or ["- nothing was resolved"]
    deps = data["dependencies"]
    lines += ["", "## Dependencies blocked", ""]
    lines += [f"- {d['dnsName']} ({d['criticality']}, {d['source']})" for d in deps] or ["- none listed"]
    lines += ["", "## FIS experiments", ""]
    for e in data["experiments"]:
        state = ((e["experiment"] or {}).get("state") or {})
        lines.append(f"- {e['experimentArn'].rsplit('/', 1)[-1]}: {state.get('status', 'unknown')}" + (f" ({state['reason']})" if state.get("reason") else ""))
    if not data["experiments"]:
        lines.append("- none")
    output = run.get("reportOutput")
    if output:
        lines += ["", f"Resilience Hub report: {output.get('status', '?')}"
                      + (f", s3 key {output['reportOutput']['s3ReportOutput']['s3ObjectKey']}" if (output.get("reportOutput") or {}).get("s3ReportOutput") else "")]
    if data["gaps"]:
        lines += ["", "## Not collected", "", *[f"- {g}" for g in data["gaps"]]]
    lines += _failback_lines(data)
    return "\n".join(lines) + "\n"


def write(data: Dict[str, Any], directory: str = REPORT_DIR, invoker_role: str = "") -> Tuple[str, str]:
    """Write <test>-<run>.json and .md into the directory and return their paths."""
    os.makedirs(directory, exist_ok=True)
    base = os.path.join(directory, f"{data['test']}-{data['testRunId']}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.write("\n")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(render(data, invoker_role))
    return base + ".json", base + ".md"
