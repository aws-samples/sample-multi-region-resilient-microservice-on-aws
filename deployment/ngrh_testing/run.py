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
from typing import Any, Callable, Dict, List

from . import api, reconcile
from .aws import AwsCli
from .context import Environment, ResolvedTest

GRACE_MINUTES = 20  # how long past the test's duration a run may take before the wait gives up
DEFAULT_POLL_SECONDS = 30


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


def active_run(aws: AwsCli, env: Environment, t: ResolvedTest, test_id: str) -> Dict[str, Any]:
    """The test's active run. Resilience Hub allows one per service."""
    runs = [r for r in api.list_test_runs(aws, env.ngrh_region, t.service_arn, test_id) if r["status"] in api.ACTIVE_STATUSES]
    if not runs:
        raise RunError(f"{t.name}: the test has no active run")
    return runs[0]


def stop(aws: AwsCli, env: Environment, t: ResolvedTest) -> List[str]:
    """Stop the test's active run. A test with no active run has nothing to stop, and says so."""
    test_id = reconcile.find_test(aws, env, t)
    if test_id is None:
        return [f"{t.name}: the test does not exist, so it has no run to stop"]
    try:
        run = active_run(aws, env, t, test_id)
    except RunError as e:
        return [str(e)]
    stopped = aws.call(api.SERVICE, "stop-test-run", env.ngrh_region, service_arn=t.service_arn, test_run_id=run["testRunId"])
    return [f"{t.name}: asked run {stopped['testRunId']} to stop; it is {stopped['status']}. "
            f"make ngrh-test-report TEST={t.name} RUN={stopped['testRunId']} collects what it recorded"]
