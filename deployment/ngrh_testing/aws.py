# SPDX-License-Identifier: MIT-0
"""The AWS CLI layer every ngrh_testing command uses.

Each call runs ``aws <service> <operation> --output json --no-cli-pager [--region R] ...`` and
returns the parsed JSON. The CLI resolves credentials as usual (AWS_PROFILE and so on, as for the
Makefile's own aws calls) and auto-paginates, so a paginated operation returns every page's
results merged into one response. Throttling is retried with exponential backoff; any other
failure raises AwsCliError at once with the CLI's own message.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404: runs the AWS CLI with an argument list, never a shell
import time
from typing import Any, Callable, Dict, List, Optional

# Substrings of the CLI's error output that mean "slow down and retry".
THROTTLING_MARKERS = (
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "Rate exceeded",
    "SlowDown",
)


class AwsCliError(RuntimeError):
    """An AWS CLI call failed for a reason retrying won't fix, or ran out of retries."""


class AwsCli:
    def __init__(
        self,
        runner: Callable[..., Any] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 6,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
    ) -> None:
        self._run = runner
        self._sleep = sleep
        self._attempts = attempts
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds

    @staticmethod
    def command(service: str, operation: str, region: Optional[str] = None, /, **params: Any) -> List[str]:
        """The argument list for one call. Keyword names become flags (scan_by -> --scan-by);
        strings pass as they are, anything else as JSON, which the CLI accepts for structures.
        The service, operation and Region are positional-only so that no option can collide with them:
        ``ecs update-service`` has an option called --service."""
        args = ["aws", service, operation, "--output", "json", "--no-cli-pager"]
        if region:
            args += ["--region", region]
        for name, value in params.items():
            args += ["--" + name.replace("_", "-"), value if isinstance(value, str) else json.dumps(value)]
        return args

    def call(self, service: str, operation: str, region: Optional[str] = None, /, **params: Any) -> Dict[str, Any]:
        args = self.command(service, operation, region, **params)
        where = f" in {region}" if region else ""
        for attempt in range(1, self._attempts + 1):
            proc = self._run(args, capture_output=True, text=True, check=False)  # nosec B603
            if proc.returncode == 0:
                return json.loads(proc.stdout) if proc.stdout.strip() else {}
            error = (proc.stderr or "").strip()
            if attempt < self._attempts and any(m in error for m in THROTTLING_MARKERS):
                self._sleep(min(self._base_delay * 2 ** (attempt - 1), self._max_delay))
                continue
            tries = f" after {attempt} attempts" if attempt > 1 else ""
            raise AwsCliError(f"aws {service} {operation}{where} failed{tries}: {error or 'no error output'}")
        raise AssertionError("unreachable")  # pragma: no cover

    def account_id(self) -> str:
        """The account the credentials in effect belong to; also proves they work."""
        return self.call("sts", "get-caller-identity")["Account"]
