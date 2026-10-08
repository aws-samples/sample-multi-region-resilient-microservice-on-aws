# SPDX-License-Identifier: MIT-0
"""Command line for ngrh_testing. The Makefile passes its own variables (PRIMARY_REGION,
STANDBY_REGION, ENV, DAYS, TEST, MODE, RUN, ALARM_WAIT, STOP_WAIT) as arguments; credentials come from the
environment, as for the Makefile's own aws calls.

Exit codes: 0 done; 1 the command could not run or failed; 2 preflight refused the run; 3 it ran and
found something to act on (replay: a failover trigger's conditions were met; reconcile --check: the tests
have drifted; run: the verdict was not the one the spec expects, or the fail-back after it is done except for what
the output says is left; failback: done except for what the output says is left to do); 130 interrupted (the run
goes on)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

from . import context, executions, failback, preflight, reconcile, replay, report, run, spec
from .aws import AwsCli, AwsCliError
from .context import ContextError, Environment, ResolvedTest, load_environment

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_FOUND = 3
EXIT_INTERRUPTED = 130

FAILBACK_CHOICES = ("auto", "skip")

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
    u.add_argument("--failback", choices=FAILBACK_CHOICES, default="auto",
                   help="when the plan deactivated a Region during the run: auto, the default, waits for the Region to be healthy for "
                        "10 minutes and then runs the fail-back; skip leaves the Region deactivated and says how to bring it back")
    u.add_argument("--failback-wait-minutes", type=float, default=failback.SETTLE_WAIT_MINUTES,
                   help="how long to wait for the Region's journey alarms to have been OK for 10 minutes before the fail-back gives "
                        f"up and says so (default {failback.SETTLE_WAIT_MINUTES})")
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

    b = commands.add_parser("failback", help="serve from a Region again after the failover plan moved traffic away from it (make failback)")
    _add_deployment(b)
    b.add_argument("--region", required=True, help="the Region to fail back: the primary or the standby")
    b.add_argument("--poll-seconds", type=float, default=failback.POLL_SECONDS)
    b.add_argument("--writer-wait-minutes", type=float, default=failback.WRITER_WAIT_MINUTES,
                   help="how long to wait for the old primary database to rejoin and catch up before giving up on moving "
                        f"the writer back (default {failback.WRITER_WAIT_MINUTES})")
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
                 clock: Callable[[], float], now: Callable[[], datetime]) -> int:
    tests = spec.load(args.spec).select(args.test)
    if args.test == "all":
        raise spec.SpecError(["name one test to run, for example TEST=orders-broker-dependency"])
    if args.alarm_wait_minutes > 0:
        _wait_for_alarm_data(aws, env, args, sleep, clock)
    checked = preflight.run_checks(aws, env, tests, preflight.LIVE, now())
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
    data = report.collect(aws, env, t, run_id, now())
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
    code = EXIT_OK if data["matches"] else EXIT_FOUND
    try:
        failed_back = _fail_back_after_run(aws, env, data, args, sleep, clock, now)
    except KeyboardInterrupt:
        _say("\nInterrupted while failing back. Nothing is undone and every step is safe to repeat: run make failback "
             "REGION=<the Region the plan deactivated> again.")
        return EXIT_INTERRUPTED
    if data.get("failbacks"):
        report.write(data, args.reports_dir, env.invoker_role_name)  # again, with what the fail-back did
        _say(f"Report updated with the fail-back: {md_path}")
    return _worst(code, failed_back)


def _worst(verdict: int, failback_code: int) -> int:
    """The run's exit code: a fail-back that failed or was refused leaves a Region deactivated, which is an error
    whatever the verdict was; one that finished except for what it printed is a finding, like a verdict that was not the
    one expected."""
    if failback_code in (EXIT_ERROR, EXIT_REFUSED):
        return EXIT_ERROR
    return EXIT_FOUND if failback_code == EXIT_FOUND else verdict


def _fail_back_after_run(aws: AwsCli, env: Environment, data: dict, args: argparse.Namespace, sleep: Callable[[float], None],
                         clock: Callable[[], float], now: Callable[[], datetime]) -> int:
    """Bring back each Region the plan deactivated during the run (design 5.9: for a recovery test, or any run during
    which a failover started). The fault has ended with the run, but the Region's alarms need a few minutes to settle,
    so this waits for them instead of being refused, up to ``--failback-wait-minutes``. ``--failback skip`` only says
    what to run. The lines of each fail-back are kept in ``data["failbacks"]`` for the report."""
    regions = executions.deactivated_regions(data.get("planExecutions") or [])
    code = EXIT_OK
    data["failbacks"] = []
    for region in regions:
        again = f"make failback REGION={region}"
        lines: List[str] = []

        def say(text: str = "", lines: List[str] = lines) -> None:
            _say(text)
            lines.append(text.strip("\n"))

        def err(text: str, lines: List[str] = lines) -> None:
            sys.stderr.write(text + "\n")
            lines.append(text)

        if args.failback == "skip":
            say(f"The plan deactivated {region} during the run, and --failback skip leaves it that way. Bring it back with: {again}")
            data["failbacks"].append({"region": region, "lines": lines, "exit": EXIT_OK})
            continue
        say(f"The plan deactivated {region} during the run. Waiting up to {args.failback_wait_minutes:g} min for {region} to have been "
            f"healthy for {failback.STABLE_MINUTES} min, then running {again}.")
        try:
            standing = failback.wait_until_stable(aws, env, region, args.failback_wait_minutes, say, sleep, clock, now, args.poll_seconds)
        except AwsCliError as e:
            err(f"ngrh_testing run: could not read the alarms to wait for {region}: {e}. Run {again} when you can.")
            result = EXIT_ERROR
        else:
            if standing:
                err(f"ngrh_testing run: {region} was still not healthy after {args.failback_wait_minutes:g} min ({'; '.join(standing)}), "
                    f"so it was not failed back. Run {again} when it is.")
                result = EXIT_ERROR
            else:
                result = _do_failback(aws, env, region, args.poll_seconds, failback.WRITER_WAIT_MINUTES, sleep, clock, now, say, err)
        data["failbacks"].append({"region": region, "lines": lines, "exit": result})
        if result in (EXIT_ERROR, EXIT_REFUSED):
            code = EXIT_ERROR
        elif result == EXIT_FOUND and code == EXIT_OK:
            code = EXIT_FOUND
    return code


def _do_failback(aws: AwsCli, env: Environment, region: str, poll_seconds: float, writer_wait_minutes: float,
                 sleep: Callable[[float], None], clock: Callable[[], float], now: Callable[[], datetime],
                 say: Callable[[str], None], err: Callable[[str], None]) -> int:
    """Run the fail-back and report how it went through ``say`` and ``err``; returns its exit code."""
    again = f"make failback REGION={region}"
    try:
        outcome = failback.run(aws, env, region, say, sleep, clock, now, poll_seconds, writer_wait_minutes)
    except failback.FailbackRefused as e:
        say(f"Fail-back of {region} refused, {len(e.problems)} reason(s); nothing was changed:")
        for problem in e.problems:
            say(f"  {problem}")
        return EXIT_REFUSED
    except failback.FailbackError as e:
        err(f"ngrh_testing failback: {e}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        say(f"\nInterrupted. Nothing is undone and every step is safe to repeat: run {again} again.")
        return EXIT_INTERRUPTED
    for line in outcome.failed:
        err(f"ngrh_testing failback: {line}")
    for line in outcome.left:
        say(f"Left for you: {line}")
    if outcome.exit_code:
        say(f"Fail-back of {region} is not finished. Every step is safe to repeat: {again}.")
    else:
        say(f"Fail-back of {region} is done.")
    return outcome.exit_code


def _failback_command(aws: AwsCli, args: argparse.Namespace, sleep: Callable[[float], None], clock: Callable[[], float],
                      now: Callable[[], datetime]) -> int:
    """Fail a Region back. It needs no ngrh stack, only the plan: a deployment that never ran make ngrh can use it."""
    try:
        aws.account_id()
    except AwsCliError as e:
        sys.stderr.write(f"ngrh_testing failback: the credentials do not work: {e}\n")
        return EXIT_ERROR
    env = context.load_basic_environment(aws, args.primary_region, args.standby_region, args.env)
    return _do_failback(aws, env, args.region, args.poll_seconds, args.writer_wait_minutes, sleep, clock, now, _say,
                        lambda line: sys.stderr.write(line + "\n"))


def main(argv: Optional[List[str]] = None, aws: Optional[AwsCli] = None, now_seconds: Optional[float] = None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    args = parser().parse_args(argv)
    aws = aws or AwsCli()

    def now() -> datetime:
        return datetime.fromtimestamp(time.time() if now_seconds is None else now_seconds, tz=timezone.utc)

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
        if args.command == "failback":
            return _failback_command(aws, args, sleep, clock, now)
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
            checked = preflight.run_checks(aws, env, tests, args.mode, now())
            sys.stdout.write(preflight.render(checked, args.mode, [t.name for t in tests]))
            return EXIT_OK if checked.passed else EXIT_REFUSED
        if args.command == "run":
            return _run_command(aws, env, args, sleep, clock, now)
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
