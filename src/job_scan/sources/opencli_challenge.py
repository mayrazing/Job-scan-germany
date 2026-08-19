from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from job_scan.sources.base import BrowserSourceError

DEFAULT_CHALLENGE_WAIT_SECONDS = 90.0
_POLL_SECONDS = 2.0

_T = TypeVar("_T")


def wait_for_challenge_clearance(
    read: Callable[[], _T],
    is_challenge: Callable[[_T], bool],
    *,
    wait_seconds: float = DEFAULT_CHALLENGE_WAIT_SECONDS,
    read_with_timeout: Callable[[float], _T] | None = None,
) -> _T:
    """Keep reading one browser page while a user can clear its challenge."""
    if wait_seconds < 0:
        raise ValueError("wait_seconds must not be negative")

    result = read()
    if not is_challenge(result) or wait_seconds == 0:
        return result

    deadline = time.monotonic() + wait_seconds
    while is_challenge(result):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_POLL_SECONDS, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        result = read_with_timeout(remaining) if read_with_timeout is not None else read()
    return result


def is_challenge_payload(payload: object) -> bool:
    """Return whether one OpenCLI page payload reports a human challenge."""
    return isinstance(payload, dict) and payload.get("status") == "challenge"


def is_challenge_error(error: Exception) -> bool:
    """Return whether one source error means its human challenge timed out."""
    return isinstance(error, BrowserSourceError) and error.error_code.endswith("_challenge")
