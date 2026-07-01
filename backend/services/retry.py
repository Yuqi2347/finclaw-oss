from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import requests

from backend.core.models import Permission
from backend.services.resource_guard import ResourceLimitExceeded
from backend.services.safety import SafetyViolation


T = TypeVar("T")


class RetryPolicy:
    def __init__(self, max_attempts: int = 3, base_delay: float = 0.35, max_delay: float = 2.0) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay


DEFAULT_TOOL_RETRY = RetryPolicy()


def should_retry_tool_error(exc: Exception) -> bool:
    if isinstance(exc, (PermissionError, ValueError, SafetyViolation, ResourceLimitExceeded)):
        return False
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        return status == 429 or 500 <= status < 600
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    return False


def should_retry_llm_error(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        return status == 429 or 500 <= status < 600
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    return False


def tool_retry_allowed(permission: Permission) -> bool:
    return permission in {
        Permission.READ,
        Permission.ANALYZE_CACHED,
        Permission.EXPENSIVE_CONFIRM,
    }


def run_with_retry(
    fn: Callable[[], T],
    *,
    retryable: Callable[[Exception], bool],
    policy: RetryPolicy = DEFAULT_TOOL_RETRY,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= policy.max_attempts or not retryable(exc):
                raise
            delay = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
            if on_retry:
                on_retry(attempt, exc, delay)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
