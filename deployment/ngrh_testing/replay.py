# SPDX-License-Identifier: MIT-0
"""make ngrh-alarm-replay (design 5.9): how the journey alarms and the failover triggers would have
behaved over the last DAYS days of canary data. Read-only: it reads CloudWatch metrics and the
caller's identity, nothing else.

For each Region it fetches every canary's SuccessPercent at one-minute resolution, evaluates each
journey alarm minute by minute as monitoring.yml defines it, derives region-degraded, and checks
the eight trigger condition sets. Expect zero trigger matches before arming automatic failover
(design 8); a journey alarm that fired needs a person to decide whether it was a real failure.

Exit status: 0 when no trigger condition was met, 3 when one was, 1 on an error.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import alarms
from .aws import AwsCli

# CloudWatch keeps 60-second datapoints for 15 days, then only 5-minute ones. 14 days leaves the
# lead-in minutes and the newest, still-arriving minutes inside the 1-minute retention.
MAX_DAYS = 14
# The newest minutes may not be published yet; reading them would count them as missing.
TAIL_LAG_MINUTES = 3
# The first reported minute needs this many minutes before it for a full evaluation window.
LEAD_MINUTES = alarms.EVALUATION_PERIODS - 1
# Episodes listed per alarm or trigger before "and N more".
LIST_LIMIT = 5

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TRIGGER_MET = 3


class ReplayError(RuntimeError):
    """The replay can't run or can't trust its input; the message says what to fix."""


@dataclass(frozen=True)
class Window:
    """The reported minutes, as epoch minutes: start, start + 1, ..., start + minutes - 1."""

    start: int
    minutes: int

    @property
    def end(self) -> int:
        return self.start + self.minutes


def make_window(now_seconds: float, days: int) -> Window:
    if not 1 <= days <= MAX_DAYS:
        raise ReplayError(
            f"DAYS must be 1 to {MAX_DAYS}: CloudWatch keeps one-minute canary datapoints for 15 days (got {days})")
    end = int(now_seconds // 60) - TAIL_LAG_MINUTES
    minutes = days * 24 * 60
    return Window(end - minutes, minutes)


def iso(epoch_minute: int) -> str:
    return dt.datetime.fromtimestamp(epoch_minute * 60, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def when(epoch_minute: int) -> str:
    return dt.datetime.fromtimestamp(epoch_minute * 60, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _epoch_minute(timestamp: str) -> int:
    # Python 3.9's fromisoformat doesn't take a trailing Z.
    return int(dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()) // 60


def metric_queries(canaries: Sequence[str]) -> List[dict]:
    return [
        {
            "Id": f"q{i}",
            "Label": name,
            "MetricStat": {
                "Metric": {
                    "Namespace": alarms.NAMESPACE,
                    "MetricName": alarms.METRIC,
                    "Dimensions": [{"Name": "CanaryName", "Value": name}],
                },
                "Period": alarms.PERIOD_SECONDS,
                "Stat": alarms.STATISTIC,
            },
            "ReturnData": True,
        }
        for i, name in enumerate(canaries)
    ]


def fetch(aws: AwsCli, region: str, canaries: Sequence[str], window: Window) -> Dict[str, List[Optional[float]]]:
    """Each canary's one-minute series from LEAD_MINUTES before the window to its end, None where
    a minute has no datapoint. Refuses a canary with no datapoints at all: that is a wrong ENV or
    Region, not 14 days of failures."""
    first = window.start - LEAD_MINUTES
    length = window.minutes + LEAD_MINUTES
    response = aws.call(
        "cloudwatch", "get-metric-data", region=region,
        metric_data_queries=metric_queries(canaries),
        start_time=iso(first), end_time=iso(window.end), scan_by="TimestampAscending",
    )
    by_id = {f"q{i}": name for i, name in enumerate(canaries)}
    series: Dict[str, List[Optional[float]]] = {name: [None] * length for name in canaries}
    # The CLI merges pages, so one query can appear once per page; merge them by Id.
    for result in response.get("MetricDataResults", []):
        name = by_id.get(result.get("Id"))
        if name is None:
            continue
        for timestamp, value in zip(result.get("Timestamps", []), result.get("Values", [])):
            i = _epoch_minute(timestamp) - first
            if 0 <= i < length:
                series[name][i] = float(value)
    empty = [name for name in canaries if all(v is None for v in series[name])]
    if empty:
        raise ReplayError(
            f"no {alarms.METRIC} datapoints in {region} from {when(first)} to {when(window.end)} for "
            f"{', '.join(empty)}: check ENV and the Regions, and that the canaries are deployed")
    return series


@dataclass(frozen=True)
class AlarmResult:
    region: str
    view: str
    journey: str
    env: str
    states: List[bool]
    missing_minutes: int

    @property
    def name(self) -> str:
        return alarms.journey_alarm_name(self.view, self.journey, self.region, self.env)

    @property
    def episodes(self) -> List[alarms.Episode]:
        return alarms.episodes(self.states)


@dataclass(frozen=True)
class TriggerResult:
    trigger: alarms.Trigger
    states: List[bool]

    @property
    def episodes(self) -> List[alarms.Episode]:
        return alarms.episodes(self.states)

    @property
    def executions(self) -> List[int]:
        return alarms.executions(self.states)


@dataclass(frozen=True)
class Result:
    account: str
    env: str
    regions: Sequence[str]
    window: Window
    days: int
    alarms: List[AlarmResult]
    degraded: Dict[str, List[bool]]
    triggers: List[TriggerResult]

    @property
    def triggers_met(self) -> List[TriggerResult]:
        return [t for t in self.triggers if any(t.states)]


def run(aws: AwsCli, primary: str, standby: str, env: str, days: int, now_seconds: float) -> Result:
    if not primary or not standby or primary == standby:
        raise ReplayError(f"need two different Regions, got {primary!r} and {standby!r}")
    window = make_window(now_seconds, days)
    account = aws.account_id()
    results: List[AlarmResult] = []
    states: Dict[str, Dict[str, List[bool]]] = {}
    for region in (primary, standby):
        canary = {(v, j): alarms.canary_name(v, j, env) for v in alarms.VIEWS for j in alarms.JOURNEYS}
        series = fetch(aws, region, list(canary.values()), window)
        states[region] = {}
        for (view, journey), name in canary.items():
            alarm = alarms.alarm_states(series[name])
            missing = sum(1 for v in series[name][LEAD_MINUTES:] if v is None)
            results.append(AlarmResult(region, view, journey, env, alarm, missing))
            states[region][f"{view}-{journey}"] = alarm
    degraded = {region: alarms.region_states(states[region]) for region in (primary, standby)}
    trigger_results = [
        TriggerResult(t, alarms.trigger_states(
            states[t.region][f"lcl-{t.journey}"], states[t.peer][f"rmt-{t.journey}"], degraded[t.peer]))
        for t in alarms.triggers(primary, standby)
    ]
    return Result(account, env, (primary, standby), window, days, results, degraded, trigger_results)


def _episode_list(window: Window, episodes: Sequence[alarms.Episode]) -> str:
    shown = ", ".join(f"{when(window.start + e.start)} ({e.minutes} min)" for e in episodes[:LIST_LIMIT])
    more = len(episodes) - LIST_LIMIT
    return shown + (f", and {more} more" if more > 0 else "")


def render(result: Result) -> str:
    w = result.window
    primary, standby = result.regions
    lines = [
        f"Alarm replay for account {result.account}, ENV '{result.env}', {primary} and {standby}",
        f"Window: {when(w.start)} to {when(w.end)} ({result.days} days, {w.minutes} minutes)",
        f"Journey alarms as in monitoring.yml: {alarms.METRIC} {alarms.STATISTIC} per {alarms.PERIOD_SECONDS} s, "
        f"ALARM when {alarms.DATAPOINTS_TO_ALARM} of {alarms.EVALUATION_PERIODS} minutes are below "
        f"{alarms.THRESHOLD:g}% or have no datapoint.",
        "",
        "Failover triggers (design 5.6): deactivate A when journey-lcl-J-A and journey-rmt-J-B are ALARM "
        "and region-degraded-B is OK",
    ]
    for t in result.triggers:
        label = f"  deactivate {t.trigger.region}, {t.trigger.journey}, confirmed from {t.trigger.peer}: "
        eps = t.episodes
        if not eps:
            lines.append(label + "never met")
            continue
        runs = len(t.executions)
        lines.append(
            label + f"met {len(eps)} time{'s' if len(eps) != 1 else ''}, {sum(e.minutes for e in eps)} min; "
            f"would have started up to {runs} execution{'s' if runs != 1 else ''}: {_episode_list(w, eps)}")
    met = result.triggers_met
    lines.append(
        f"Verdict: {len(met)} of {len(result.triggers)} triggers met"
        + (": don't arm automatic failover until each one is explained." if met else "; nothing here blocks arming."))

    fired = sorted((a for a in result.alarms if a.episodes), key=lambda a: -sum(e.minutes for e in a.episodes))
    lines += ["", f"Journey alarms that went into ALARM ({len(fired)} of {len(result.alarms)}): "
                  "a person decides whether each was a real failure or noise"]
    for a in fired:
        eps = a.episodes
        longest = max(eps, key=lambda e: e.minutes)
        lines.append(
            f"  {a.name}: {len(eps)} episode{'s' if len(eps) != 1 else ''}, {sum(e.minutes for e in eps)} min, "
            f"longest {longest.minutes} min at {when(w.start + longest.start)}; "
            f"{a.missing_minutes} minute{'s' if a.missing_minutes != 1 else ''} without a datapoint")
        lines.append(f"    {_episode_list(w, eps)}")
    if not fired:
        lines.append("  none")

    lines += ["", "region-degraded (any lcl journey alarm):"]
    for region in result.regions:
        eps = alarms.episodes(result.degraded[region])
        name = alarms.region_degraded_name(region, result.env)
        lines.append(f"  {name}: " + (
            f"{len(eps)} episode{'s' if len(eps) != 1 else ''}, {sum(e.minutes for e in eps)} min" if eps else "never"))

    missing = sum(a.missing_minutes for a in result.alarms)
    worst = max(result.alarms, key=lambda a: a.missing_minutes)
    lines += ["", f"Minutes without a datapoint, which the alarms count as failures: {missing} across "
                  f"{len(result.alarms)} canaries; most for {worst.name} ({worst.missing_minutes})"]
    return "\n".join(lines) + "\n"
