# SPDX-License-Identifier: MIT-0
"""Command line for ngrh_testing. The Makefile passes its own variables (PRIMARY_REGION,
STANDBY_REGION, ENV, DAYS, TEST) as arguments; credentials come from the environment, as for the
Makefile's own aws calls.

Exit codes: 0 done, 1 the command could not run or failed, 3 it ran and found something to act on
(replay: a failover trigger's conditions were met; reconcile --check: the tests have drifted)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

from . import reconcile, replay, spec
from .aws import AwsCli, AwsCliError
from .context import ContextError, load_environment

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FOUND = 3

DEFAULT_SPEC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ngrh-tests.json")


def _add_deployment(p: argparse.ArgumentParser) -> None:
    p.add_argument("--primary-region", required=True)
    p.add_argument("--standby-region", required=True)
    p.add_argument("--env", default="",
                   help="the Makefile's ENV, for example --env=-dev (use =: the value starts with a dash); empty by default")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m ngrh_testing", description="NGRH test tooling (design 5.9)")
    commands = p.add_subparsers(dest="command", required=True)

    r = commands.add_parser("replay", help="how the journey alarms and failover triggers would have behaved")
    _add_deployment(r)
    r.add_argument("--days", type=int, default=14, help=f"1 to {replay.MAX_DAYS} (default 14)")

    c = commands.add_parser("reconcile", help="create or update the tests from the spec (make ngrh-tests)")
    _add_deployment(c)
    c.add_argument("--spec", default=DEFAULT_SPEC, help="the test spec (default deployment/ngrh-tests.json)")
    c.add_argument("--test", default="all", help="one test by name, or all (default)")
    c.add_argument("--check", action="store_true",
                   help="write nothing: say what would change, and exit 3 if the tests have drifted from the spec")

    d = commands.add_parser("delete-tests", help="delete every test on the ngrh stack's services (run by destroy-ngrh)")
    _add_deployment(d)
    return p


def _report(lines: List[str]) -> None:
    for line in lines:
        sys.stdout.write(line + "\n")


def main(argv: Optional[List[str]] = None, aws: Optional[AwsCli] = None, now_seconds: Optional[float] = None) -> int:
    args = parser().parse_args(argv)
    aws = aws or AwsCli()
    try:
        if args.command == "replay":
            result = replay.run(aws, args.primary_region, args.standby_region, args.env, args.days,
                                time.time() if now_seconds is None else now_seconds)
            sys.stdout.write(replay.render(result))
            return replay.EXIT_TRIGGER_MET if result.triggers_met else replay.EXIT_OK
        if args.command == "reconcile":
            tests = spec.load(args.spec).select(args.test)
            env = load_environment(aws, args.primary_region, args.standby_region, args.env)
            plans, lines = reconcile.reconcile(aws, env, tests, write=not args.check)
            _report(lines)
            if args.check and not all(p.in_sync for p in plans):
                sys.stdout.write("The tests have drifted from the spec: run make ngrh-tests.\n")
                return EXIT_FOUND
            return EXIT_OK
        if args.command == "delete-tests":
            _report(reconcile.delete_tests(aws, args.primary_region, args.env))
            return EXIT_OK
    except (spec.SpecError, ContextError, reconcile.ReconcileError) as e:
        for problem in e.problems:
            sys.stderr.write(f"ngrh_testing {args.command}: {problem}\n")
        return EXIT_ERROR
    except (replay.ReplayError, AwsCliError) as e:
        sys.stderr.write(f"ngrh_testing {args.command}: {e}\n")
        return replay.EXIT_ERROR
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover
