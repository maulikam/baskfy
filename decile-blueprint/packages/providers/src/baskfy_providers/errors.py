"""Typed provider failures.

docs/09 §"Kite specifics": the daily access token expiring is "the #1 pipeline failure", and it
must "alert loudly". That only works if the failure is distinguishable — a pipeline that catches
one broad exception cannot tell "renew the token" from "NSE published late" from "we are being
rate limited", and will retry all three identically.

The hierarchy below exists so a caller can decide, and so the house rule "no silently swallowed
exceptions" has something specific to catch.

    ProviderError
    ├─ ProviderUnavailable        the provider cannot serve at all right now
    │  ├─ CredentialsMissing      nothing configured; expected in local dev
    │  ├─ AccessTokenExpired      configured but stale — LOUD, needs a human, never retried
    │  └─ CircuitOpen             we stopped calling it on purpose
    ├─ TransientProviderError     worth retrying
    │  ├─ RateLimited             429, or our own limiter said no
    │  └─ UpstreamUnavailable     5xx, timeout, connection reset
    └─ ProviderDataError          it answered, but the answer is unusable
       ├─ UnexpectedPayload       shape we do not recognise
       └─ ArchiveError            we could not archive a raw file, so parsing must not proceed
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every failure originating in a provider adapter."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider

    def __str__(self) -> str:
        base = super().__str__()
        return f"[{self.provider}] {base}" if self.provider else base


class ProviderUnavailable(ProviderError):
    """The provider cannot serve requests at all. Not worth retrying in this run."""


class CredentialsMissing(ProviderUnavailable):
    """No credentials configured.

    Expected and benign in local development, where FixtureProvider serves instead. `providers
    doctor` reports it rather than crashing (Prompt 2 acceptance criterion 4).
    """


class AccessTokenExpired(ProviderUnavailable):
    """The Kite access token is stale and must be regenerated through the login flow.

    Deliberately *not* a TransientProviderError. Retrying an expired token burns the rate limit
    and delays the alert; docs/09 wants a human paged, not a backoff loop.
    """

    def __init__(
        self, message: str = "Kite access token has expired", *, provider: str | None = "kite"
    ) -> None:
        super().__init__(
            f"{message}. Regenerate it through the Kite login flow and store it with "
            "`AccessTokenStore.save()`; the nightly pipeline cannot fetch bars until then.",
            provider=provider,
        )


class CircuitOpen(ProviderUnavailable):
    """The circuit breaker is open, so we are deliberately not calling this provider."""

    def __init__(self, provider: str, failures: int, retry_after_seconds: float) -> None:
        super().__init__(
            f"circuit is open after {failures} consecutive failures; "
            f"retrying in {retry_after_seconds:.1f}s",
            provider=provider,
        )
        self.failures = failures
        self.retry_after_seconds = retry_after_seconds


class TransientProviderError(ProviderError):
    """A failure that a retry might resolve."""


class RateLimited(TransientProviderError):
    """Upstream returned 429, or our own token bucket refused to issue a token in time."""


class UpstreamUnavailable(TransientProviderError):
    """5xx, timeout, or a dropped connection."""


class RetryBudgetExhausted(ProviderError):
    """Every retry was used and the call still failed.

    Carries the last underlying error so the operator sees the actual cause, not just the count.
    """

    def __init__(
        self, attempts: int, last_error: BaseException, *, provider: str | None = None
    ) -> None:
        super().__init__(
            f"gave up after {attempts} attempt(s); last error: "
            f"{type(last_error).__name__}: {last_error}",
            provider=provider,
        )
        self.attempts = attempts
        self.last_error = last_error


class ProviderDataError(ProviderError):
    """The provider answered, but the answer cannot be used."""


class UnexpectedPayload(ProviderDataError):
    """The response shape is not one we recognise."""


class ArchiveError(ProviderDataError):
    """A raw file could not be archived.

    docs/09 §"NSE specifics" makes the archive the reproducibility record and requires it to be
    written *before* parsing. If archiving fails we must not parse anyway — that would produce a
    number nobody can ever re-derive.
    """


class CapabilityNotAvailable(ProviderError):
    """No configured provider offers the requested capability."""


__all__ = [
    "AccessTokenExpired",
    "ArchiveError",
    "CapabilityNotAvailable",
    "CircuitOpen",
    "CredentialsMissing",
    "ProviderDataError",
    "ProviderError",
    "ProviderUnavailable",
    "RateLimited",
    "RetryBudgetExhausted",
    "TransientProviderError",
    "UnexpectedPayload",
    "UpstreamUnavailable",
]
