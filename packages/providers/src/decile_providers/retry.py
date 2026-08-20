"""Exponential backoff with jitter (Prompt 2 deliverable 2).

Retries only :class:`TransientProviderError` — a 5xx, a timeout, or a 429. Everything else is
re-raised immediately, because retrying it is either pointless or harmful:

* :class:`AccessTokenExpired` will fail identically five times in a row while delaying the alert
  that a human needs to see (docs/09 calls token expiry "the #1 pipeline failure").
* :class:`CredentialsMissing` cannot fix itself.
* A malformed payload will still be malformed.

Jitter is full jitter — a uniform draw over ``[0, backoff]`` rather than ``backoff ± ε``. With N
workers failing against the same upstream at the same moment, ± jitter keeps them synchronised
and they retry in a thundering herd; full jitter spreads them across the whole interval.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from decile_providers.errors import RetryBudgetExhausted, TransientProviderError


@dataclass(frozen=True, slots=True)
class RetryHooks:
    """The seams the retry loop is driven and observed through.

    Grouped rather than passed as loose keyword arguments so that adding an observation point
    later does not change every call site. ``sleeper`` and ``jitter`` are injected so the retry
    behaviour can be asserted without a test suite spending real seconds asleep.
    """

    sleeper: Callable[[float], None] = time.sleep
    #: Draws the actual delay from ``[0, ceiling]``. Full jitter by default.
    jitter: Callable[[float], float] | None = None
    #: Called before each sleep with (attempt, delay, error) — the hook Prompt 17 hangs
    #: OpenTelemetry spans and the provider error-rate metric on.
    on_retry: Callable[[int, float, BaseException], None] | None = None

    def draw(self, ceiling: float) -> float:
        if self.jitter is not None:
            return self.jitter(ceiling)
        return random.uniform(0.0, ceiling)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times, and how long between."""

    max_attempts: int = 5
    base_seconds: float = 0.5
    max_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1; got {self.max_attempts}")
        if self.base_seconds <= 0:
            raise ValueError(f"base_seconds must be positive; got {self.base_seconds}")
        if self.max_seconds < self.base_seconds:
            raise ValueError(
                f"max_seconds ({self.max_seconds}) cannot be below base_seconds "
                f"({self.base_seconds})"
            )

    def backoff_ceiling(self, attempt: int) -> float:
        """The upper bound of the jitter window before attempt ``attempt`` (1-based)."""
        if attempt < 1:
            raise ValueError(f"attempt is 1-based; got {attempt}")
        growth = float(2 ** (attempt - 1))
        return min(self.max_seconds, self.base_seconds * growth)


def call_with_retry[T](
    operation: Callable[[], T],
    policy: RetryPolicy,
    *,
    provider: str | None = None,
    hooks: RetryHooks | None = None,
) -> T:
    """Run ``operation``, retrying transient failures per ``policy``.

    Raises :class:`RetryBudgetExhausted` — carrying the final underlying error — once the budget
    is spent, so the caller gets both "we tried" and "here is what actually broke".
    """
    seams = hooks or RetryHooks()
    last_error: BaseException | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except TransientProviderError as exc:
            last_error = exc
            if attempt == policy.max_attempts:
                break
            delay = seams.draw(policy.backoff_ceiling(attempt))
            if seams.on_retry is not None:
                seams.on_retry(attempt, delay, exc)
            seams.sleeper(delay)

    if last_error is None:  # pragma: no cover - only reachable if the loop never ran
        raise RuntimeError("retry loop exited without running the operation")
    raise RetryBudgetExhausted(policy.max_attempts, last_error, provider=provider)
