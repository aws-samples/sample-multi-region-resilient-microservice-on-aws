# SPDX-License-Identifier: MIT-0
"""The time helpers the modules share."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_time(value: Any) -> datetime:
    """A timestamp as the AWS CLI prints it (ISO 8601, with ``Z`` or an offset) or as epoch seconds, in UTC."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value)
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
