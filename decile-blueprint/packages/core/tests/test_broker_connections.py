"""Broker connection catalog and the D3 gate — M41."""

from __future__ import annotations

from baskfy_core.broker_connections import (
    BROKER_OAUTH_REVIEW,
    BROKERS,
    broker_catalog,
    get_broker,
)


class TestTheD3Gate:
    def test_live_oauth_is_not_signed_off(self) -> None:
        """**If this fails, someone flipped the Phase-4 / Track-C gate.**

        docs/05, docs/06 D3 and docs/smallcase/02 require a written regulatory posture before
        per-user broker OAuth. The constant is a source value so turning it on is a reviewable
        commit.
        """
        assert BROKER_OAUTH_REVIEW.signed_off is False
        assert BROKER_OAUTH_REVIEW.blocks_live_oauth is True
        assert BROKER_OAUTH_REVIEW.decision_reference == ""

    def test_the_requirement_names_d3(self) -> None:
        assert "D3" in BROKER_OAUTH_REVIEW.requirement
        assert "Phase 4" in BROKER_OAUTH_REVIEW.requirement


class TestTheCatalog:
    def test_exactly_ten_brokers(self) -> None:
        assert len(BROKERS) == 10
        assert len(broker_catalog()) == 10

    def test_the_four_named_brokers_lead(self) -> None:
        ids = [b.id for b in broker_catalog()]
        assert ids[:4] == ["zerodha", "hdfc", "kotak", "icici"]

    def test_every_id_is_unique_and_lookupable(self) -> None:
        ids = [b.id for b in BROKERS]
        assert len(set(ids)) == len(ids)
        for broker_id in ids:
            assert get_broker(broker_id) is not None
        assert get_broker("not-a-broker") is None

    def test_every_broker_has_a_mark_and_colour(self) -> None:
        for broker in BROKERS:
            assert len(broker.mark) >= 1
            assert broker.color.startswith("#")
            assert len(broker.color) in (4, 7)

    def test_zerodha_is_the_only_fully_ready_trading_path(self) -> None:
        zerodha = get_broker("zerodha")
        assert zerodha is not None
        assert zerodha.capabilities.oauth == "ready"
        assert zerodha.capabilities.holdings_sync == "ready"
        assert zerodha.capabilities.trading == "ready"
