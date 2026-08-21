"""CompositeProvider — routes by capability, one circuit breaker per provider (deliverable 4).

docs/09's table: "``CompositeProvider`` — routes by capability; retries; per-provider circuit
breaker". docs/02 §"Why Kite ... and why it is not sufficient alone" says why it exists: Kite
serves bars, NSE serves everything else, and "adding a paid vendor later is a new adapter, not a
rewrite". Call sites ask the composite for a capability; they never name a vendor.

Routing rules
-------------
* Providers are consulted in registration order, so the first registered provider that offers a
  capability and is not tripped wins. Registration order is therefore a preference order — put
  the primary vendor first and the fallback after it.
* A provider whose breaker is open is skipped rather than tried, and only if *every* candidate is
  skipped does the call fail. That is the difference between "the primary is down" degrading to a
  fallback and taking the whole pipeline with it.
* The failure raised when nothing can serve names every provider that was considered and why it
  was not used. An operator reading the nightly log should not have to guess.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Sequence
from typing import Final

import polars as pl

from baskfy_providers.circuit import CircuitBreaker, CircuitState
from baskfy_providers.errors import (
    CapabilityNotAvailable,
    CircuitOpen,
    ProviderUnavailable,
)
from baskfy_providers.ports import (
    BarsProvider,
    Capability,
    HealthReporting,
    ProviderHealth,
    ReferenceProvider,
)
from baskfy_providers.records import (
    CorporateAction,
    IndexSnapshot,
    InstrumentRecord,
    ListingRecord,
)

PROVIDER_NAME: Final = "composite"


class CompositeProvider:
    """Implements both ports by delegating each call to whichever adapter can serve it."""

    def __init__(
        self,
        providers: Sequence[HealthReporting],
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._providers = list(providers)
        names = [p.name for p in self._providers]
        if len(set(names)) != len(names):
            raise ValueError(f"provider names must be unique; got {names}")
        self._breakers = {
            p.name: _build_breaker(p.name, failure_threshold, reset_seconds, clock)
            for p in self._providers
        }

    # --- introspection ---------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def providers(self) -> tuple[HealthReporting, ...]:
        return tuple(self._providers)

    def breaker(self, provider_name: str) -> CircuitBreaker:
        try:
            return self._breakers[provider_name]
        except KeyError as exc:
            raise KeyError(f"no provider registered under {provider_name!r}") from exc

    def capabilities(self) -> frozenset[Capability]:
        """The union of what the registered adapters offer, in principle."""
        return all_capabilities(self._providers)

    def check(self) -> ProviderHealth:
        """Aggregate health. Available when at least one registered adapter is."""
        reports = self.health_reports()
        served: frozenset[Capability] = frozenset()
        for report in reports:
            served |= report.served_capabilities
        available = bool(served)
        detail = ", ".join(f"{r.name}={'up' if r.available else 'down'}" for r in reports)
        return ProviderHealth(
            name=self.name,
            available=available,
            capabilities=self.capabilities(),
            detail=detail or "no providers registered",
        )

    def health_reports(self) -> tuple[ProviderHealth, ...]:
        """One report per registered adapter, in registration order."""
        return tuple(_safe_check(p) for p in self._providers)

    def providers_for(self, capability: Capability) -> tuple[HealthReporting, ...]:
        """Registered adapters offering ``capability``, in preference order."""
        return tuple(p for p in self._providers if capability in p.capabilities())

    # --- routing ---------------------------------------------------------

    def route[T](self, capability: Capability, call: Callable[[HealthReporting], T]) -> T:
        """Run ``call`` against the first healthy provider offering ``capability``.

        Only :class:`ProviderUnavailable` moves on to the next candidate — that is the family
        meaning "this provider cannot serve", which is exactly when a fallback is correct. A
        transient error still propagates after the breaker records it, because the caller's own
        retry policy owns that decision and silently failing over on a timeout would mask an
        upstream that is merely slow.
        """
        candidates = self.providers_for(capability)
        if not candidates:
            raise CapabilityNotAvailable(
                f"no registered provider offers {capability.value!r}; "
                f"registered: {[p.name for p in self._providers]}",
                provider=self.name,
            )

        skipped: list[str] = []
        for provider in candidates:
            breaker = self._breakers[provider.name]
            if breaker.state is CircuitState.OPEN:
                skipped.append(f"{provider.name}: circuit open for {breaker.retry_after():.0f}s")
                continue
            attempt = _bind(call, provider)
            try:
                return breaker.call(attempt)
            except CircuitOpen as exc:
                skipped.append(f"{provider.name}: {exc}")
            except ProviderUnavailable as exc:
                skipped.append(f"{provider.name}: {exc}")

        raise CapabilityNotAvailable(
            f"no provider could serve {capability.value!r}. Tried: " + "; ".join(skipped),
            provider=self.name,
        )

    # --- BarsProvider ----------------------------------------------------

    def list_instruments(self) -> list[InstrumentRecord]:
        return self.route(
            Capability.LIST_INSTRUMENTS,
            lambda p: _bars(p).list_instruments(),
        )

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
        return self.route(
            Capability.DAILY_BARS,
            lambda p: _bars(p).daily_bars(token, start, end),
        )

    # --- ReferenceProvider -----------------------------------------------

    def index_constituents(self, index_slug: str, on: dt.date) -> list[str]:
        return self.route(
            Capability.INDEX_CONSTITUENTS,
            lambda p: _reference(p).index_constituents(index_slug, on),
        )

    def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]:
        return self.route(
            Capability.INDEX_SNAPSHOTS,
            lambda p: _reference(p).index_snapshots(on),
        )

    def corporate_actions(self, since: dt.date) -> list[CorporateAction]:
        return self.route(
            Capability.CORPORATE_ACTIONS,
            lambda p: _reference(p).corporate_actions(since),
        )

    def listings(self) -> list[ListingRecord]:
        return self.route(Capability.LISTINGS, lambda p: _reference(p).listings())

    def bhavcopy(self, on: dt.date) -> pl.DataFrame:
        return self.route(Capability.BHAVCOPY, lambda p: _reference(p).bhavcopy(on))


def _safe_check(provider: HealthReporting) -> ProviderHealth:
    """``check()`` must never raise, but a third-party adapter might; contain it here.

    This is the one place a broad catch is right: `providers doctor` exists to report failure,
    so an adapter that crashes while reporting its own health must still produce a line rather
    than take the whole command down (Prompt 2 acceptance criterion 4).
    """
    try:
        return provider.check()
    except Exception as exc:
        return ProviderHealth(
            name=provider.name,
            available=False,
            capabilities=frozenset(),
            detail=f"health check raised {type(exc).__name__}: {exc}",
        )


def _bind[T](call: Callable[[HealthReporting], T], provider: HealthReporting) -> Callable[[], T]:
    """Freeze ``provider`` into a nullary call, so the breaker sees one concrete attempt."""

    def attempt() -> T:
        return call(provider)

    return attempt


def _build_breaker(
    name: str,
    failure_threshold: int,
    reset_seconds: float,
    clock: Callable[[], float] | None,
) -> CircuitBreaker:
    if clock is None:
        return CircuitBreaker(
            name, failure_threshold=failure_threshold, reset_seconds=reset_seconds
        )
    return CircuitBreaker(
        name, failure_threshold=failure_threshold, reset_seconds=reset_seconds, clock=clock
    )


def _bars(provider: HealthReporting) -> BarsProvider:
    if not isinstance(provider, BarsProvider):
        raise ProviderUnavailable(
            f"{provider.name} advertises a bars capability but does not implement BarsProvider",
            provider=provider.name,
        )
    return provider


def _reference(provider: HealthReporting) -> ReferenceProvider:
    if not isinstance(provider, ReferenceProvider):
        raise ProviderUnavailable(
            f"{provider.name} advertises a reference capability but does not implement "
            "ReferenceProvider",
            provider=provider.name,
        )
    return provider


def all_capabilities(providers: Iterable[HealthReporting]) -> frozenset[Capability]:
    collected: frozenset[Capability] = frozenset()
    for provider in providers:
        collected |= provider.capabilities()
    return collected
