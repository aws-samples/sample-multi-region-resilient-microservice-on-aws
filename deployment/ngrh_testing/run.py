# SPDX-License-Identifier: MIT-0
"""Start a test run, follow it to its end, and stop one (design 5.9).

StartTestRun takes only the service and the test, so everything about the run was fixed by reconcile.
The run goes on in AWS whatever happens to this process: when the credentials expire or someone
presses Ctrl-C, the caller is told the stop and report commands to use later, and nothing is stopped
for them (design section 7). A run that outlasts its budget (the test's duration plus 20 minutes) is
reported as it stands, not waited for forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import api, reconcile
from .aws import AwsCli
from .context import Environment, ResolvedTest

GRACE_MINUTES = 20  # how long past the test's duration a run may take before the wait gives up
DEFAULT_POLL_SECONDS = 30
ALARM_POLL_SECONDS = 30


class RunError(RuntimeError):
    """A run could not be started or stopped as asked."""


@dataclass
class Outcome:
    run_id: str
    status: str  # the terminal status, or the last one seen
    finished: bool  # the run reached a terminal status
    timed_out: bool = False
    waited_seconds: float = 0.0


def budget_seconds(t: ResolvedTest) -> int:
    return (t.test.duration_minutes + GRACE_MINUTES) * 60


def wait_for_alarm_data(
    aws: AwsCli,
    t: ResolvedTest,
    minutes: float,
    poll_seconds: float = ALARM_POLL_SECONDS,
    progress: Callable[[str], None] = lambda line: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> List[str]:
    """Wait up to ``minutes`` for the test's success and stop alarms to have data.

    A new deployment's alarms start in INSUFFICIENT_DATA until the canaries have reported, and preflight
    refuses a run while a success or stop alarm is anything but OK. CI starts a run soon after a deploy,
    so it waits here first. Only INSUFFICIENT_DATA is waited out: an alarm in ALARM, or one that does
    not exist, will not change by waiting, and preflight says so. Returns the alarms still without
    data when the time is up (none when every alarm has data)."""
    needed = sorted({(a.region, a.name) for a in list(t.success) + list(t.stop)})
    began = clock()
    last: Optional[List[str]] = None
    while True:
        waiting: List[str] = []
        for region in sorted({r for r, _ in needed}):
            names = [n for r, n in needed if r == region]
            found = api.describe_alarms(aws, region, names)
            waiting += [f"{n} ({region})" for n in names if found.get(n, {}).get("state") == "INSUFFICIENT_DATA"]
        if not waiting or clock() - began >= minutes * 60:
            return waiting
        if waiting != last:
            progress(f"{len(waiting)} alarm(s) have no data yet: {', '.join(waiting)}")
            last = waiting
        sleep(poll_seconds)


def start(aws: AwsCli, env: Environment, t: ResolvedTest, test_id: str) -> Dict[str, Any]:
    return aws.call(api.SERVICE, "start-test-run", env.ngrh_region, service_arn=t.service_arn, test_id=test_id)


def wait(
    aws: AwsCli,
    env: Environment,
    t: ResolvedTest,
    run_id: str,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    progress: Callable[[str], None] = lambda line: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Outcome:
    """Poll GetTestRun until the run reaches a terminal status or its budget runs out."""
    began = clock()
    budget = budget_seconds(t)
    last = None
    while True:
        status = aws.call(api.SERVICE, "get-test-run", env.ngrh_region, service_arn=t.service_arn,
                          test_run_id=run_id)["testRun"]["status"]
        waited = clock() - began
        if status != last:
            progress(f"{run_id}: {status} after {int(waited // 60)} min {int(waited % 60)} s")
            last = status
        if status in api.TERMINAL_STATUSES:
            return Outcome(run_id, status, True, waited_seconds=waited)
        if waited >= budget:
            return Outcome(run_id, status, False, timed_out=True, waited_seconds=waited)
        sleep(poll_seconds)


def settle(
    minutes: float,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """Wait ``minutes`` after a run has ended, in poll-sized steps (so Ctrl-C is heard within one), before its
    report is written. The report reads the evidence alarms' history up to ten minutes past the end of the run,
    so the recovery shows; a report written at once misses the alarms that change in the minutes after the fault
    stops (on 2026-10-07 the ui and orders hop alarms fired one to three minutes after the run ended, and the
    report written 17 seconds after it said they had not changed). Returns the seconds waited."""
    total = max(0.0, minutes) * 60
    waited = 0.0
    while waited < total:
        step = min(poll_seconds, total - waited)
        sleep(step)
        waited += step
    return waited


def active_run(aws: AwsCli, env: Environment, t: ResolvedTest, test_id: str) -> Dict[str, Any]:
    """The test's active run. Resilience Hub allows one per service."""
    runs = [r for r in api.list_test_runs(aws, env.ngrh_region, t.service_arn, test_id) if r["status"] in api.ACTIVE_STATUSES]
    if not runs:
        raise RunError(f"{t.name}: the test has no active run")
    return runs[0]


def stop(
    aws: AwsCli,
    env: Environment,
    t: ResolvedTest,
    wait_minutes: float = 0.0,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> List[str]:
    """Stop the test's active run. A test with no active run has nothing to stop, and says so.

    With ``wait_minutes`` it then waits that long for the run to end: delete-tests refuses while a run is
    active, and a run that is still STOPPING counts as active."""
    test_id = reconcile.find_test(aws, env, t)
    if test_id is None:
        return [f"{t.name}: the test does not exist, so it has no run to stop"]
    try:
        run = active_run(aws, env, t, test_id)
    except RunError as e:
        return [str(e)]
    stopped = aws.call(api.SERVICE, "stop-test-run", env.ngrh_region, service_arn=t.service_arn, test_run_id=run["testRunId"])
    lines = [f"{t.name}: asked run {stopped['testRunId']} to stop; it is {stopped['status']}. "
             f"make ngrh-test-report TEST={t.name} RUN={stopped['testRunId']} collects what it recorded"]
    if wait_minutes > 0:
        lines.append(_wait_until_ended(aws, env, t, stopped["testRunId"], wait_minutes, poll_seconds, sleep, clock))
    return lines


def _wait_until_ended(aws: AwsCli, env: Environment, t: ResolvedTest, run_id: str, wait_minutes: float,
                      poll_seconds: float, sleep: Callable[[float], None], clock: Callable[[], float]) -> str:
    began = clock()
    while True:
        status = aws.call(api.SERVICE, "get-test-run", env.ngrh_region, service_arn=t.service_arn,
                          test_run_id=run_id)["testRun"]["status"]
        if status in api.TERMINAL_STATUSES:
            return f"{t.name}: run {run_id} is {status}"
        if clock() - began >= wait_minutes * 60:
            return f"{t.name}: run {run_id} is still {status} after {wait_minutes:g} min"
        sleep(poll_seconds)
