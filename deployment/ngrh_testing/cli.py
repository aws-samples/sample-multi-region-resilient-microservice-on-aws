# SPDX-License-Identifier: MIT-0
"""Command line for ngrh_testing. The Makefile passes its own variables (PRIMARY_REGION,
STANDBY_REGION, ENV, DAYS) as arguments; credentials come from the environment, as for the
Makefile's own aws calls."""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from . import replay
from .aws import AwsCli, AwsCliError


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m ngrh_testing", description="NGRH test tooling (design 5.9)")
    commands = p.add_subparsers(dest="command", required=True)
    r = commands.add_parser("replay", help="how the journey alarms and failover triggers would have behaved")
    r.add_argument("--primary-region", required=True)
    r.add_argument("--standby-region", required=True)
    r.add_argument("--env", default="",
                   help="the Makefile's ENV, for example --env=-dev (use =: the value starts with a dash); empty by default")
    r.add_argument("--days", type=int, default=14, help=f"1 to {replay.MAX_DAYS} (default 14)")
    return p


def main(argv: Optional[List[str]] = None, aws: Optional[AwsCli] = None, now_seconds: Optional[float] = None) -> int:
    args = parser().parse_args(argv)
    aws = aws or AwsCli()
    try:
        if args.command == "replay":
            result = replay.run(aws, args.primary_region, args.standby_region, args.env, args.days,
                                time.time() if now_seconds is None else now_seconds)
            sys.stdout.write(replay.render(result))
            return replay.EXIT_TRIGGER_MET if result.triggers_met else replay.EXIT_OK
    except (replay.ReplayError, AwsCliError) as e:
        sys.stderr.write(f"ngrh_testing {args.command}: {e}\n")
        return replay.EXIT_ERROR
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover
