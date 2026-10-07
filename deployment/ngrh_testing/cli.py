# SPDX-License-Identifier: MIT-0
"""Command line for ngrh_testing. The Makefile passes its own variables (PRIMARY_REGION,
STANDBY_REGION, ENV, DAYS, TEST, MODE, RUN, ALARM_WAIT, STOP_WAIT) as arguments; credentials come from the
environment, as for the Makefile's own aws calls.

Exit codes: 0 done; 1 the command could not run or failed; 2 preflight refused the run; 3 it ran and
found something to act on (replay: a failover trigger's conditions were met; reconcile --check: the tests
have drifted; run: the verdict was not the one the spec expects); 130 interrupted (the run goes on)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, List, Optional

from . import context, preflight, reconcile, replay, report, run, spec
from .aws import AwsCli, AwsCliError
from .context import ContextError, Environment, ResolvedTest, load_environment

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_FOUND = 3
EXIT_INTERRUPTED = 130

DEFAULT_SPEC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ngrh-tests.json")


def _add_deployment(p: argparse.ArgumentParser) -> None:
    p.add_argument("--primary-region", required=True)
    p.add_argument("--standby-region", required=True)
    p.add_argument("--env", default="",
                   help="the Makefile's ENV, for example --env=-dev (use =: the value starts with a dash); empty by default")


def _add_spec(p: argparse.ArgumentParser, test_default: Optional[str] = None) -> None:
    p.add_argument("--spec", default=DEFAULT_SPEC, help="the test spec (default deployment/ngrh-tests.json)")
    if test_default is None:
        p.add_argument("--test", required=True, help="the test's name (see ngrh-tests.json)")
    else:
        p.add_argument("--test", default=test_default, help="one test by name, or all (default)")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m ngrh_testing", description="NGRH test tooling (design 5.9)")
    commands = p.add_subparsers(dest="command", required=True)

    r = commands.add_parser("replay", help="how the journey alarms and failover triggers would have behaved")
    _add_deployment(r)
    r.add_argument("--days", type=int, default=14, help=f"1 to {replay.MAX_DAYS} (default 14)")

    c = commands.add_parser("reconcile", help="create or update the tests from the spec (make ngrh-tests)")
    _add_deployment(c)
    _add_spec(c, "all")
    c.add_argument("--check", action="store_true",
                   help="write nothing: say what would change, and exit 3 if the tests have drifted from the spec")

    d = commands.add_parser("delete-tests", help="delete every test on the ngrh stack's services (run by destroy-ngrh)")
    _add_deployment(d)

    f = commands.add_parser("preflight", help="refuse a run, with reasons, unless everything it needs is in place")
    _add_deployment(f)
    _add_spec(f, "all")
    f.add_argument("--mode", choices=preflight.MODES, default=preflight.LIVE,
                   help="live does every check; static skips the ones about what is happening right now (default live)")

    u = commands.add_parser("run", help="preflight, run one test to its end and write its report (make ngrh-test)")
    _add_deployment(u)
    _add_spec(u)
    u.add_argument("--poll-seconds", type=float, default=run.DEFAULT_POLL_SECONDS)
    u.add_argument("--alarm-wait-minutes", type=float, default=0.0,
                   help="first wait up to this long for the test's success and stop alarms to have data (a new "
                        "deployment's alarms start without it); 0, the default, does not wait")
    u.add_argument("--settle-minutes", type=float, default=report.AFTER.total_seconds() / 60,
                   help="after the run ends, wait this long before writing the report, so the alarms' recovery is in it "
                        f"(default {report.AFTER.total_seconds() / 60:g}, the length of the evidence window); 0 writes it at once, "
                        "and the report then says it is early")
    u.add_argument("--reports-dir", default=report.REPORT_DIR)

    s = commands.add_parser("stop", help="stop a test's active run (make ngrh-test-stop)")
    _add_deployment(s)
    _add_spec(s)
    s.add_argument("--wait-minutes", type=float, default=0.0,
                   help="after asking, wait up to this long for the run to end (delete-tests refuses while one is active); default 0")

    t = commands.add_parser("report", help="collect a run's report (make ngrh-test-report)")
    _add_deployment(t)
    _add_spec(t)
    t.add_argument("--run", default="", help="the run's id; the test's latest run by default")
    t.add_argument("--reports-dir", default=report.REPORT_DIR)
    return p


def _say(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _one(aws: AwsCli, env: Environment, args: argparse.Namespace) -> ResolvedTest:
    """The single test a command acts on, resolved. ``all`` is not a test to run."""
    if args.test == "all":
        raise spec.SpecError(["name one test, for example TEST=orders-broker-dependency"])
    return context.resolve_all(aws, env, spec.load(args.spec).select(args.test))[0]


def _wait_for_alarm_data(aws: AwsCli, env: Environment, args: argparse.Namespace, sleep: Callable[[float], None],
                         clock: Callable[[], float]) -> None:
    """Give a new deployment's alarms time to get data. Whatever is still wrong afterwards, preflight reports."""
    try:
        t = _one(aws, env, args)
    except (spec.SpecError, ContextError):
        return  # a test that does not resolve is preflight's to report
    _say(f"Waiting up to {args.alarm_wait_minutes:g} min for the success and stop alarms of {t.name} to have data.")
    still = run.wait_for_alarm_data(aws, t, args.alarm_wait_minutes, progress=_say, sleep=sleep, clock=clock)
    if still:
        _say(f"After {args.alarm_wait_minutes:g} min these alarms still have no data: {', '.join(still)}.")


def _run_command(aws: AwsCli, env: Environment, args: argparse.Namespace, sleep: Callable[[float], None],
                 clock: Callable[[], float]) -> int:
    tests = spec.load(args.spec).select(args.test)
    if args.test == "all":
        raise spec.SpecError(["name one test to run, for example TEST=orders-broker-dependency"])
    if args.alarm_wait_minutes > 0:
        _wait_for_alarm_data(aws, env, args, sleep, clock)
    checked = preflight.run_checks(aws, env, tests, preflight.LIVE)
    sys.stdout.write(preflight.render(checked, preflight.LIVE, [t.name for t in tests]))
    if not checked.passed:
        return EXIT_REFUSED
    t = _one(aws, env, args)
    test_id = reconcile.find_test(aws, env, t)
    if test_id is None:
        raise run.RunError(f"{t.name}: the test does not exist; run make ngrh-tests")
    started = run.start(aws, env, t, test_id)
    run_id = started["testRunId"]
    later = (f"The run goes on in AWS. Stop it with: make ngrh-test-stop TEST={t.name}\n"
             f"Collect its report with: make ngrh-test-report TEST={t.name} RUN={run_id}")
    _say(f"Started run {run_id} of {t.name}, {started['status']}; waiting up to {run.budget_seconds(t) // 60} min "
         f"(the test's {t.test.duration_minutes} min plus {run.GRACE_MINUTES}).")
    try:
        outcome = run.wait(aws, env, t, run_id, args.poll_seconds, _say, sleep, clock)
    except KeyboardInterrupt:
        _say("\nInterrupted. " + later)
        return EXIT_INTERRUPTED
    except AwsCliError as e:
        sys.stderr.write(f"ngrh_testing run: lost contact with AWS while waiting: {e}\n{later}\n")
        return EXIT_ERROR
    if outcome.finished and args.settle_minutes > 0:
        _say(f"The run ended. Waiting {args.settle_minutes:g} min for the alarms' recovery to show before the report is written "
             "(--settle-minutes 0 writes it now, and the report then says it is early).")
        try:
            run.settle(args.settle_minutes, args.poll_seconds, sleep)
        except KeyboardInterrupt:
            # The run is over: nothing needs stopping, and the report can be collected whenever the window has closed.
            _say(f"\nInterrupted. The run has ended. Collect its report, once ten minutes have passed since it ended, with: "
                 f"make ngrh-test-report TEST={t.name} RUN={run_id}")
            return EXIT_INTERRUPTED
    data = report.collect(aws, env, t, run_id)
    json_path, md_path = report.write(data, args.reports_dir, env.invoker_role_name)
    _say(f"{t.name}: {outcome.status}. Expected {data['expected']}, observed {data['observed']}: "
         + ("as expected." if data["matches"] else "NOT as expected."))
    if data["faultNotRun"]:
        _say(f"The fault did not run, so this is not a verdict on the application: {data['faultNotRun']}")
    _say(f"Report: {md_path}\nData: {json_path}")
    if outcome.timed_out:
        sys.stderr.write(f"ngrh_testing run: gave up waiting after {int(outcome.waited_seconds // 60)} min; the run is still "
                         f"{outcome.status}.\n{later}\n")
        return EXIT_ERROR
    return EXIT_OK if data["matches"] else EXIT_FOUND


def main(argv: Optional[List[str]] = None, aws: Optional[AwsCli] = None, now_seconds: Optional[float] = None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    args = parser().parse_args(argv)
    aws = aws or AwsCli()
    try:
        if args.command == "replay":
            result = replay.run(aws, args.primary_region, args.standby_region, args.env, args.days,
                                time.time() if now_seconds is None else now_seconds)
            sys.stdout.write(replay.render(result))
            return replay.EXIT_TRIGGER_MET if result.triggers_met else replay.EXIT_OK
        if args.command == "delete-tests":
            for line in reconcile.delete_tests(aws, args.primary_region, args.env):
                _say(line)
            return EXIT_OK
        if args.command == "preflight":  # check 1: the credential sentinel
            try:
                aws.account_id()
            except AwsCliError as e:
                sys.stdout.write(f"Preflight ({args.mode}) for {args.test}: refused, 1 reason(s)\n  [1] the credentials do not work: {e}\n")
                return EXIT_REFUSED
        env = load_environment(aws, args.primary_region, args.standby_region, args.env)
        if args.command == "reconcile":
            tests = spec.load(args.spec).select(args.test)
            plans, lines = reconcile.reconcile(aws, env, tests, write=not args.check)
            for line in lines:
                _say(line)
            if args.check and not all(p.in_sync for p in plans):
                _say("The tests have drifted from the spec: run make ngrh-tests.")
                return EXIT_FOUND
            return EXIT_OK
        if args.command == "preflight":
            tests = spec.load(args.spec).select(args.test)
            checked = preflight.run_checks(aws, env, tests, args.mode)
            sys.stdout.write(preflight.render(checked, args.mode, [t.name for t in tests]))
            return EXIT_OK if checked.passed else EXIT_REFUSED
        if args.command == "run":
            return _run_command(aws, env, args, sleep, clock)
        if args.command == "stop":
            for line in run.stop(aws, env, _one(aws, env, args), args.wait_minutes, sleep=sleep, clock=clock):
                _say(line)
            return EXIT_OK
        if args.command == "report":
            t = _one(aws, env, args)
            test_id = reconcile.find_test(aws, env, t)
            if test_id is None:
                raise run.RunError(f"{t.name}: the test does not exist, so it has no runs")
            run_id = args.run or report.latest_run_id(aws, env, t, test_id)
            data = report.collect(aws, env, t, run_id)
            json_path, md_path = report.write(data, args.reports_dir, env.invoker_role_name)
            _say(f"{t.name} run {run_id}: {data['testRun']['status']}. Expected {data['expected']}, observed {data['observed']}.")
            _say(f"Report: {md_path}\nData: {json_path}")
            return EXIT_OK
    except (spec.SpecError, ContextError, reconcile.ReconcileError) as e:
        for problem in e.problems:
            sys.stderr.write(f"ngrh_testing {args.command}: {problem}\n")
        return EXIT_ERROR
    except (replay.ReplayError, AwsCliError, run.RunError, ValueError) as e:
        sys.stderr.write(f"ngrh_testing {args.command}: {e}\n")
        return EXIT_ERROR
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover
