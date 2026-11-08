"""The holdings port — PORTFOLIO_REDESIGN.md §4.6 layer 1, and the record it exchanges.

The Kite adapter's own behaviour is proven in ``test_kite.py`` against its fake client. What is
proven here is the *port*: the record's quantity contract, the fixture adapter a test or a local
stack wires in Kite's place, the composite's routing, and — the one that is really a policy test
rather than a plumbing test — that nothing in the production stack will quietly answer a holdings
call with invented positions.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from baskfy_providers.composite import CompositeProvider
from baskfy_providers.errors import CapabilityNotAvailable, ProviderUnavailable
from baskfy_providers.factory import build_provider_stack
from baskfy_providers.fixtures import FixtureHoldingsProvider, FixtureProvider
from baskfy_providers.kite import KiteProvider
from baskfy_providers.ports import (
    BARS_CAPABILITIES,
    HOLDINGS_CAPABILITIES,
    REFERENCE_CAPABILITIES,
    Capability,
    HoldingsProvider,
    ProviderHealth,
)
from baskfy_providers.records import BrokerAccountRef, BrokerHoldingRecord
from baskfy_providers.settings import ProviderSettings

ACCOUNT = BrokerAccountRef(broker_account_id=1, broker_id="zerodha")
OTHER_ACCOUNT = BrokerAccountRef(broker_account_id=2, broker_id="zerodha")


def holding(symbol: str, **kwargs: object) -> BrokerHoldingRecord:
    return BrokerHoldingRecord.model_validate({"symbol": symbol, **kwargs})


class TestTheQuantityContract:
    """Desk non-negotiable #2, which survives verbatim into this product."""

    def test_total_is_settled_plus_t1_plus_collateral(self) -> None:
        row = holding("SBIN", quantity=60, t1_quantity=10, collateral_quantity=30)
        assert row.total_quantity == Decimal("100")

    def test_absent_segments_are_zero(self) -> None:
        assert holding("SBIN", quantity=7).total_quantity == Decimal("7")

    def test_a_negative_quantity_is_refused(self) -> None:
        """A broker cannot hold minus five shares; a record that says so is a bug upstream."""
        with pytest.raises(ValidationError):
            holding("SBIN", quantity=-5)

    def test_a_negative_price_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            holding("SBIN", quantity=1, average_price=-1)

    def test_an_unknown_field_is_refused(self) -> None:
        """These cross a trust boundary; a key we do not understand must not pass silently."""
        with pytest.raises(ValidationError):
            holding("SBIN", quantity=1, realised_pnl=10)

    def test_the_record_is_immutable(self) -> None:
        """A record is a reading of the world at a moment, never a mutable buffer."""
        row = holding("SBIN", quantity=1)
        with pytest.raises(ValidationError):
            setattr(row, "quantity", Decimal("2"))  # noqa: B010 - the point is that it fails

    def test_an_account_ref_needs_a_real_account_id(self) -> None:
        with pytest.raises(ValidationError):
            BrokerAccountRef(broker_account_id=0, broker_id="zerodha")


class TestPortConformance:
    def test_kite_satisfies_the_holdings_port(self, settings: ProviderSettings) -> None:
        assert isinstance(KiteProvider(settings), HoldingsProvider)

    def test_the_fixture_holdings_adapter_satisfies_it(self) -> None:
        assert isinstance(FixtureHoldingsProvider({}), HoldingsProvider)

    def test_the_parquet_fixture_provider_does_not(self, fixture_provider: FixtureProvider) -> None:
        """It serves bars and reference from Parquet and holds nobody's positions.

        Kept apart on purpose: ``FixtureProvider`` is registered at the back of the production
        stack, and a holdings method on it would make invented positions reachable there.
        """
        assert not isinstance(fixture_provider, HoldingsProvider)
        assert HOLDINGS_CAPABILITIES.isdisjoint(fixture_provider.capabilities())

    def test_capability_values_match_the_port_method_names(self) -> None:
        for capability in HOLDINGS_CAPABILITIES:
            assert hasattr(HoldingsProvider, capability.value)

    def test_the_three_capability_sets_are_disjoint_and_complete(self) -> None:
        assert HOLDINGS_CAPABILITIES.isdisjoint(BARS_CAPABILITIES)
        assert HOLDINGS_CAPABILITIES.isdisjoint(REFERENCE_CAPABILITIES)
        assert set(Capability) == BARS_CAPABILITIES | REFERENCE_CAPABILITIES | HOLDINGS_CAPABILITIES


class TestTheFixtureAdapter:
    def test_it_answers_per_broker_account(self) -> None:
        """Two accounts are two answers — the property a per-tenant test needs to exist."""
        provider = FixtureHoldingsProvider(
            {
                ACCOUNT.broker_account_id: [holding("SBIN", quantity=10)],
                OTHER_ACCOUNT.broker_account_id: [holding("INFY", quantity=4)],
            }
        )
        assert [row.symbol for row in provider.broker_holdings(ACCOUNT)] == ["SBIN"]
        assert [row.symbol for row in provider.broker_holdings(OTHER_ACCOUNT)] == ["INFY"]

    def test_an_empty_account_must_be_said_not_inferred(self) -> None:
        """An empty list means "sold everything". A typo must not be able to impersonate it."""
        provider = FixtureHoldingsProvider({ACCOUNT.broker_account_id: []})
        assert provider.broker_holdings(ACCOUNT) == []
        with pytest.raises(ProviderUnavailable, match="no answer for broker account"):
            provider.broker_holdings(OTHER_ACCOUNT)

    def test_cash_is_none_when_none_was_configured(self) -> None:
        provider = FixtureHoldingsProvider({ACCOUNT.broker_account_id: []})
        assert provider.broker_cash(ACCOUNT) is None

    def test_cash_is_returned_as_decimal(self) -> None:
        provider = FixtureHoldingsProvider(
            {ACCOUNT.broker_account_id: []}, {ACCOUNT.broker_account_id: Decimal("2500.75")}
        )
        assert provider.broker_cash(ACCOUNT) == Decimal("2500.75")

    def test_it_reports_itself_as_fabricated(self) -> None:
        """`providers doctor` must never let this be mistaken for a broker."""
        health = FixtureHoldingsProvider({ACCOUNT.broker_account_id: [holding("X", quantity=1)]})
        report = health.check()
        assert report.available is True
        assert "not anyone's real holdings" in report.detail


class TestCompositeRouting:
    def test_it_routes_a_holdings_call_to_the_adapter_that_offers_it(self) -> None:
        fixture = FixtureHoldingsProvider(
            {ACCOUNT.broker_account_id: [holding("SBIN", quantity=3)]},
            {ACCOUNT.broker_account_id: Decimal("100")},
        )
        composite = CompositeProvider([fixture])

        assert [row.symbol for row in composite.broker_holdings(ACCOUNT)] == ["SBIN"]
        assert composite.broker_cash(ACCOUNT) == Decimal("100")

    def test_with_no_holdings_adapter_it_fails_loudly(
        self, fixture_provider: FixtureProvider
    ) -> None:
        """The whole policy, in one assertion.

        A stack that can serve bars and reference but has no broker session must raise rather
        than degrade. Tree-5's leaf C1 exists because a real fetch was labelled fake; the mirror
        failure — invented positions served as real — is the one this prevents.
        """
        composite = CompositeProvider([fixture_provider])
        with pytest.raises(CapabilityNotAvailable, match="broker_holdings"):
            composite.broker_holdings(ACCOUNT)

    def test_the_production_stack_registers_no_fixture_holdings_fallback(
        self, settings: ProviderSettings
    ) -> None:
        """``build_provider_stack`` must never make invented holdings reachable in production."""
        stack = build_provider_stack(settings)
        offering = [p.name for p in stack.providers_for(Capability.BROKER_HOLDINGS)]
        assert offering == ["kite"]

    def test_a_provider_that_only_claims_the_capability_is_refused(self) -> None:
        """Claiming a capability without implementing the port is a wiring bug, not a fallback."""

        class Liar:
            @property
            def name(self) -> str:
                return "liar"

            def capabilities(self) -> frozenset[Capability]:
                return HOLDINGS_CAPABILITIES

            def check(self) -> ProviderHealth:
                return ProviderHealth(
                    name=self.name, available=True, capabilities=HOLDINGS_CAPABILITIES
                )

        composite = CompositeProvider([Liar()])
        with pytest.raises(CapabilityNotAvailable, match="does not implement HoldingsProvider"):
            composite.broker_holdings(ACCOUNT)


class TestNoOrderPathHere:
    """Law 2: ``packages/execution`` is the only path to an order, and this port is not it."""

    def test_the_port_declares_no_order_verb(self) -> None:
        forbidden = {"place_order", "place_gtt_stop", "modify_order", "cancel_order", "execute"}
        assert forbidden.isdisjoint(dir(HoldingsProvider))

    def test_the_capability_set_is_read_only(self) -> None:
        assert {c.value for c in HOLDINGS_CAPABILITIES} == {"broker_holdings", "broker_cash"}
