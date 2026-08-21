"""A per-provider circuit breaker (Prompt 2 deliverable 4).

docs/09's table: ``CompositeProvider`` — "routes by capability; retries; per-provider circuit
breaker".

The point is not to make a failing provider work; it is to stop a failing provider from consuming
the whole nightly window. Without a breaker, 2,300 instruments x 5 retries x exponential backoff
against a dead upstream will still be running when the market reopens.

Standard three states:

    CLOSED     calls pass through; consecutive failures are counted
    OPEN       calls are refused immediately with CircuitOpen, until the reset interval elapses
    HALF_OPEN  one probe call is allowed; success closes the circuit, failure re-opens it

Only :class:`TransientProviderError` trips the breaker. A malformed payload or a missing
credential is a fact about the request, not evidence the provider is down, and tripping on those
would take a healthy provider offline for the wrong reason.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum

from baskfy_providers.errors import CircuitOpen, TransientProviderError


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Trips after ``failure_threshold`` consecutive transient failures."""

    def __init__(
        self,
        provider: str,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be at least 1; got {failure_threshold}")
        if reset_seconds <= 0:
            raise ValueError(f"reset_seconds must be positive; got {reset_seconds}")
        self._provider = provider
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def failures(self) -> int:
        return self._failures

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at >= self._reset_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def retry_after(self) -> float:
        """Seconds until the breaker will admit a probe. ``0.0`` when it already would."""
        if self._opened_at is None:
            return 0.0
        remaining = self._reset_seconds - (self._clock() - self._opened_at)
        return max(0.0, remaining)

    def call[T](self, operation: Callable[[], T]) -> T:
        """Run ``operation`` under the breaker."""
        if self.state is CircuitState.OPEN:
            raise CircuitOpen(self._provider, self._failures, self.retry_after())

        try:
            result = operation()
        except TransientProviderError:
            self._record_failure()
            raise
        self._record_success()
        return result

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            # Re-stamp on every failure so a failing half-open probe restarts the full interval
            # rather than immediately admitting another probe.
            self._opened_at = self._clock()

    def _record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
