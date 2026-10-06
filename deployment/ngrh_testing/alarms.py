# SPDX-License-Identifier: MIT-0
"""The journey alarms, region-degraded composites and failover triggers, as the replay models them.

monitoring.yml defines the alarms (design 5.5) and step 11's failover.yaml the triggers (design
5.6). The package has no YAML parser (standard library only), so their settings are restated
here; tests/test_ngrh_testing_replay.py fails if these drift from monitoring.yml.

Everything works on one-minute periods. A series is a list with one entry per minute, oldest
first: a datapoint's value, or None where the minute has no datapoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

JOURNEYS = ("home", "cart", "catalog", "orders")
# Canary name prefix for each view. lcl: this Region's canary against this Region's ALB;
# rmt: this Region's canary against the other Region's ALB; global: against the Route 53 name.
CANARY_PREFIX = {"lcl": "lcl-rgnl-", "rmt": "rmt-rgnl-", "global": "global-"}
VIEWS = tuple(CANARY_PREFIX)

# The journey alarms' settings in monitoring.yml.
NAMESPACE = "CloudWatchSynthetics"
METRIC = "SuccessPercent"
STATISTIC = "Average"
PERIOD_SECONDS = 60
EVALUATION_PERIODS = 3
DATAPOINTS_TO_ALARM = 2
THRESHOLD = 50.0  # LessThanThreshold
MISSING_DATA_BREACHES = True  # TreatMissingData: breaching

# A trigger starts at most one plan execution per this many minutes (design 5.6).
TRIGGER_MIN_DELAY_MINUTES = 60


def canary_name(view: str, journey: str, env: str) -> str:
    return f"{CANARY_PREFIX[view]}{journey}{env}"


def journey_alarm_name(view: str, journey: str, region: str, env: str) -> str:
    return f"journey-{view}-{journey}-{region}{env}"


def region_degraded_name(region: str, env: str) -> str:
    return f"region-degraded-{region}{env}"


def breaches(value: Optional[float]) -> bool:
    """A minute breaches when its datapoint is below the threshold, or when it has none."""
    if value is None:
        return MISSING_DATA_BREACHES
    return value < THRESHOLD


def alarm_states(series: Sequence[Optional[float]]) -> List[bool]:
    """The alarm's state after each minute that has a full evaluation window behind it: True for
    ALARM (at least DATAPOINTS_TO_ALARM of the last EVALUATION_PERIODS minutes breach), False for
    OK. The result is EVALUATION_PERIODS - 1 entries shorter than the series, so a caller that
    reports on a window passes that many minutes from before it."""
    lead = EVALUATION_PERIODS - 1
    if len(series) <= lead:
        raise ValueError(f"need more than {lead} minutes, got {len(series)}")
    flags = [breaches(v) for v in series]
    states = []
    for end in range(lead, len(flags)):
        states.append(sum(flags[end - lead:end + 1]) >= DATAPOINTS_TO_ALARM)
    return states


@dataclass(frozen=True)
class Episode:
    """A run of consecutive minutes in ALARM (or matching a trigger): its first minute's index
    in the window, and how many minutes it lasted."""

    start: int
    minutes: int

    @property
    def end(self) -> int:
        return self.start + self.minutes


def episodes(states: Sequence[bool]) -> List[Episode]:
    """Every maximal run of True. A run still going at the window's end ends with the window."""
    out = []
    start = None
    for i, on in enumerate(states):
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append(Episode(start, i - start))
            start = None
    if start is not None:
        out.append(Episode(start, len(states) - start))
    return out


def any_of(*series: Sequence[bool]) -> List[bool]:
    """A composite of ALARM(a) OR ALARM(b) OR ..."""
    return [any(minute) for minute in zip(*series)]


def trigger_states(lcl_a: Sequence[bool], rmt_b: Sequence[bool], degraded_b: Sequence[bool]) -> List[bool]:
    """Design 5.6's trigger "deactivate A: journey J failing in A, confirmed from B": met while
    journey-lcl-J-A is red, journey-rmt-J-B (B's view of A) is red and region-degraded-B is green."""
    return [lcl and rmt and not degraded for lcl, rmt, degraded in zip(lcl_a, rmt_b, degraded_b)]


def executions(states: Sequence[bool], min_delay: int = TRIGGER_MIN_DELAY_MINUTES) -> List[int]:
    """The minutes a trigger would start a plan execution: the first minute its conditions hold,
    then the first such minute at least min_delay minutes after the previous start. An upper
    bound: in practice an execution in progress also holds the plan."""
    starts: List[int] = []
    for i, met in enumerate(states):
        if met and (not starts or i - starts[-1] >= min_delay):
            starts.append(i)
    return starts


@dataclass(frozen=True)
class Trigger:
    region: str  # A, the Region the trigger would deactivate
    peer: str  # B, the Region whose view confirms it
    journey: str


def triggers(primary: str, standby: str) -> List[Trigger]:
    """The eight triggers: each Region, confirmed by its peer, for each journey."""
    return [Trigger(a, b, j) for a, b in ((primary, standby), (standby, primary)) for j in JOURNEYS]


def region_states(states: Dict[str, List[bool]]) -> List[bool]:
    """region-degraded for one Region, from that Region's journey alarm states keyed by
    'lcl-<journey>'."""
    return any_of(*(states[f"lcl-{j}"] for j in JOURNEYS))
