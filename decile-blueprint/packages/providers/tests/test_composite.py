"""CompositeProvider and the circuit breaker (Prompt 2 deliverable 4).

docs/09's table: "routes by capability; retries; per-provider circuit breaker".
docs/02: "Adding a paid vendor later is a new adapter, not a rewrite" — which only holds if call
sites depend on capabilities rather than on vendors, so that is what these assert.
"""

from __future__ import annotations

import datetime as dt

import pytest
from stubs import StubProvider

from baskfy_providers.circuit import CircuitBreaker, CircuitState
from baskfy_providers.composite import CompositeProvider
from baskfy_providers.errors import (
    CapabilityNotAvailable,
    CircuitOpen,
    CredentialsMissing,
    ProviderUnavailable,
    UpstreamUnavailable,
)
from baskfy_providers.ports import (
    BARS_CAPABILITIES,
    REFERENCE_CAPABILITIES,
    Capability,
    ProviderHealth,
)

ON = dt.date(2026, 8, 18)


class TestRouting:
    def test_a_bars_call_goes_to_the_bars_provider(self) -> None:
        kite = StubProvider("kite", BARS_CAPABILITIES)
        nse = StubProvider("nse", REFERENCE_CAPABILITIES)
        CompositeProvider([kite, nse]).list_instruments()
        assert (kite.calls, nse.calls) == (1, 0)

    def test_a_reference_call_goes_to_the_reference_provider(self) -> None:
        kite = StubProvider("kite", BARS_CAPABILITIES)
        nse = StubProvider("nse", REFERENCE_CAPABILITIES)
        CompositeProvider([kite, nse]).listings()
        assert (kite.calls, nse.calls) == (0, 1)

    def test_registration_order_is_preference_order(self) -> None:
        primary = StubProvider("primary", REFERENCE_CAPABILITIES)
        fallback = StubProvider("fallback", REFERENCE_CAPABILITIES)
        CompositeProvider([primary, fallback]).listings()
        assert (primary.calls, fallback.calls) == (1, 0)

    def test_an_unserved_capability_names_what_was_registered(self) -> None:
        composite = CompositeProvider([StubProvider("kite", BARS_CAPABILITIES)])
        with pytest.raises(CapabilityNotAvailable, match="kite"):
            composite.listings()

    def test_capabilities_are_the_union_of_the_registered_adapters(self) -> None:
        composite = CompositeProvider(
            [
                StubProvider("kite", BARS_CAPABILITIES),
                StubProvider("nse", REFERENCE_CAPABILITIES),
            ]
        )
        assert composite.capabilities() == BARS_CAPABILITIES | REFERENCE_CAPABILITIES

    def test_duplicate_provider_names_are_rejected(self) -> None:
        """Names key the breakers; two providers sharing one would share a circuit."""
        with pytest.raises(ValueError, match="unique"):
            CompositeProvider(
                [
                    StubProvider("nse", REFERENCE_CAPABILITIES),
                    StubProvider("nse", REFERENCE_CAPABILITIES),
                ]
            )

    def test_providers_for_lists_candidates_in_order(self) -> None:
        primary = StubProvider("primary", REFERENCE_CAPABILITIES)
        fallback = StubProvider("fallback", REFERENCE_CAPABILITIES)
        composite = CompositeProvider([primary, fallback])
        assert [p.name for p in composite.providers_for(Capability.LISTINGS)] == [
            "primary",
            "fallback",
        ]


class TestFailover:
    def test_an_unavailable_provider_falls_through_to_the_next(self) -> None:
        """ "This provider cannot serve" is exactly when a fallback is correct."""
        primary = StubProvider(
            "primary", REFERENCE_CAPABILITIES, raises=CredentialsMissing("no key")
        )
        fallback = StubProvider("fallback", REFERENCE_CAPABILITIES)
        result = CompositeProvider([primary, fallback]).listings()
        assert [r.symbol for r in result] == ["SBIN"]
        assert (primary.calls, fallback.calls) == (1, 1)

    def test_a_transient_error_propagates_rather_than_failing_over(self) -> None:
        """A timeout means "slow", not "down". Silently failing over would mask it, and the
        caller's own retry policy owns that decision."""
        primary = StubProvider("primary", REFERENCE_CAPABILITIES, raises=UpstreamUnavailable("503"))
        fallback = StubProvider("fallback", REFERENCE_CAPABILITIES)
        with pytest.raises(UpstreamUnavailable):
            CompositeProvider([primary, fallback]).listings()
        assert fallback.calls == 0

    def test_when_every_candidate_is_unavailable_the_reason_is_reported(self) -> None:
        composite = CompositeProvider(
            [
                StubProvider("a", REFERENCE_CAPABILITIES, raises=CredentialsMissing("no key")),
                StubProvider("b", REFERENCE_CAPABILITIES, raises=ProviderUnavailable("no bucket")),
            ]
        )
        with pytest.raises(CapabilityNotAvailable) as raised:
            composite.listings()
        assert "no key" in str(raised.value)
        assert "no bucket" in str(raised.value)


class TestCircuitBreaking:
    def test_repeated_transient_failures_open_the_circuit(self) -> None:
        provider = StubProvider("nse", REFERENCE_CAPABILITIES, raises=UpstreamUnavailable("503"))
        composite = CompositeProvider([provider], failure_threshold=3, reset_seconds=60)
        for _ in range(3):
            with pytest.raises(UpstreamUnavailable):
                composite.listings()
        assert composite.breaker("nse").state is CircuitState.OPEN

    def test_an_open_circuit_stops_calling_the_provider(self) -> None:
        """The point: a dead upstream must not consume the whole nightly window."""
        provider = StubProvider("nse", REFERENCE_CAPABILITIES, raises=UpstreamUnavailable("503"))
        composite = CompositeProvider([provider], failure_threshold=2, reset_seconds=60)
        for _ in range(2):
            with pytest.raises(UpstreamUnavailable):
                composite.listings()
        calls_when_tripped = provider.calls
        with pytest.raises(CapabilityNotAvailable, match="circuit open"):
            composite.listings()
        assert provider.calls == calls_when_tripped

    def test_a_tripped_primary_fails_over_to_a_healthy_fallback(self) -> None:
        primary = StubProvider("primary", REFERENCE_CAPABILITIES, raises=UpstreamUnavailable("x"))
        fallback = StubProvider("fallback", REFERENCE_CAPABILITIES)
        composite = CompositeProvider([primary, fallback], failure_threshold=1, reset_seconds=60)
        with pytest.raises(UpstreamUnavailable):
            composite.listings()
        assert composite.listings()
        assert fallback.calls == 1

    def test_a_success_closes_the_circuit(self) -> None:
        breaker = CircuitBreaker("nse", failure_threshold=2, reset_seconds=60)
        with pytest.raises(UpstreamUnavailable):
            breaker.call(_fail)
        assert breaker.failures == 1
        breaker.call(lambda: None)
        assert breaker.failures == 0
        assert breaker.state is CircuitState.CLOSED

    def test_the_circuit_half_opens_after_the_reset_interval(self) -> None:
        now = [0.0]
        breaker = CircuitBreaker("nse", failure_threshold=1, reset_seconds=30, clock=lambda: now[0])
        with pytest.raises(UpstreamUnavailable):
            breaker.call(_fail)
        while_open = breaker.state
        now[0] = 31.0
        after_reset = breaker.state
        assert while_open is CircuitState.OPEN
        assert after_reset is CircuitState.HALF_OPEN

    def test_a_failed_probe_reopens_for_the_full_interval(self) -> None:
        """Otherwise a dead upstream gets probed on every single call."""
        now = [0.0]
        breaker = CircuitBreaker("nse", failure_threshold=1, reset_seconds=30, clock=lambda: now[0])
        with pytest.raises(UpstreamUnavailable):
            breaker.call(_fail)
        now[0] = 31.0
        with pytest.raises(UpstreamUnavailable):
            breaker.call(_fail)
        assert breaker.state is CircuitState.OPEN
        assert breaker.retry_after() == pytest.approx(30.0)

    def test_a_non_transient_failure_does_not_trip_the_breaker(self) -> None:
        """A malformed payload is a fact about the request, not evidence the vendor is down."""
        breaker = CircuitBreaker("nse", failure_threshold=1, reset_seconds=30)
        with pytest.raises(CredentialsMissing):
            breaker.call(_missing_credentials)
        assert breaker.state is CircuitState.CLOSED

    def test_an_open_circuit_reports_when_it_will_retry(self) -> None:
        breaker = CircuitBreaker("nse", failure_threshold=1, reset_seconds=45, clock=lambda: 0.0)
        with pytest.raises(UpstreamUnavailable):
            breaker.call(_fail)
        with pytest.raises(CircuitOpen) as raised:
            breaker.call(lambda: None)
        assert raised.value.retry_after_seconds == pytest.approx(45.0)

    def test_configuration_is_validated(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold"):
            CircuitBreaker("nse", failure_threshold=0)
        with pytest.raises(ValueError, match="reset_seconds"):
            CircuitBreaker("nse", reset_seconds=0)


class TestHealthAggregation:
    def test_it_reports_one_line_per_provider(self) -> None:
        composite = CompositeProvider(
            [
                StubProvider("kite", BARS_CAPABILITIES, available=False),
                StubProvider("nse", REFERENCE_CAPABILITIES),
            ]
        )
        reports = composite.health_reports()
        assert [(r.name, r.available) for r in reports] == [("kite", False), ("nse", True)]

    def test_an_adapter_whose_health_check_raises_still_produces_a_line(self) -> None:
        """`providers doctor` exists to report failure; it must not be taken down by one."""
        composite = CompositeProvider(
            [StubProvider("broken", BARS_CAPABILITIES, check_raises=RuntimeError("boom"))]
        )
        report = composite.health_reports()[0]
        assert report.available is False
        assert "RuntimeError" in report.detail

    def test_an_unavailable_provider_serves_nothing(self) -> None:
        health = ProviderHealth("kite", available=False, capabilities=BARS_CAPABILITIES)
        assert health.served_capabilities == frozenset()

    def test_the_composite_is_available_when_any_provider_is(self) -> None:
        composite = CompositeProvider(
            [
                StubProvider("kite", BARS_CAPABILITIES, available=False),
                StubProvider("nse", REFERENCE_CAPABILITIES),
            ]
        )
        assert composite.check().available is True

    def test_the_composite_is_unavailable_when_none_are(self) -> None:
        composite = CompositeProvider([StubProvider("kite", BARS_CAPABILITIES, available=False)])
        assert composite.check().available is False


def _fail() -> None:
    raise UpstreamUnavailable("503")


def _missing_credentials() -> None:
    raise CredentialsMissing("no key")
