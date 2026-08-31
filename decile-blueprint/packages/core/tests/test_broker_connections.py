"""Broker connection catalog and the D3 gate — M41 / D3 unlock."""

from __future__ import annotations

from baskfy_core.broker_connections import (
    BROKER_OAUTH_REVIEW,
    BROKERS,
    broker_catalog,
    get_broker,
)


class TestTheD3Gate:
    def test_live_oauth_is_signed_off_with_d3_reference(self) -> None:
        """D3 posture B is written in DECISIONS-MERGE.md; the source gate must match.

        Reversal: set signed_off=False and clear decision_reference.
        """
        assert BROKER_OAUTH_REVIEW.signed_off is True
        assert BROKER_OAUTH_REVIEW.blocks_live_oauth is False
        assert "D3" in BROKER_OAUTH_REVIEW.decision_reference
        assert BROKER_OAUTH_REVIEW.decision_reference.startswith("DECISIONS-MERGE")

    def test_the_requirement_names_posture_b_and_no_web_execute(self) -> None:
        assert "Posture B" in BROKER_OAUTH_REVIEW.requirement
        assert "never places orders" in BROKER_OAUTH_REVIEW.requirement.lower() or (
            "never place" in BROKER_OAUTH_REVIEW.requirement.lower()
        )


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

    def test_zerodha_is_a_publisher_handoff_and_says_so(self) -> None:
        """Zerodha is **Kite Publisher**, not Kite Connect, and the row must not blur them.

        This asserted three "ready"s, which was true of the Kite *Connect* adapter in this
        codebase and false of the integration Baskfy actually ships. Rendered, those three
        became green "Ready" labels beside a Connect button that could only ever answer "app key
        is not configured" — which is what a reader met, twice, after supplying a perfectly good
        Publisher key.

        Kite Connect is ₹2,000/month and licensed for the app owner's own account: the wrong
        shape for a service other people sign into. Publisher is free, needs no API secret, and
        does the one thing needed — hand a prepared basket to whichever Zerodha session the
        reader has, for them to confirm. `docs/DECISIONS-MERGE.md` M55.
        """
        zerodha = get_broker("zerodha")
        assert zerodha is not None
        assert zerodha.api_name == "Kite Publisher"
        # Nothing is linked: the basket opens in the session the browser already has.
        assert zerodha.capabilities.oauth == "not_applicable"
        # M55 asserted `not_available` here, on the reasoning that Publisher is one-way and there
        # would never be a session to read holdings with. M57/M58 made that false: the desk's
        # morning Kite login produces a Connect session, the bridge borrows it, and
        # `broker_holdings` reads GET /portfolio/holdings with it (18 live positions, 1 Sep 2026).
        # Publisher is still one-way and still how an order reaches Kite — hence `handoff` below,
        # unchanged. Only the claim about holdings moved.
        assert zerodha.capabilities.holdings_sync == "ready"
        # The user trades, in their own terminal, after reviewing every line.
        assert zerodha.capabilities.trading == "handoff"

    def test_no_broker_claims_baskfy_places_the_order(self) -> None:
        """Desk non-negotiable #1, read off the catalog rather than trusted to the copy.

        `handoff` is the strongest thing any row may say about trading. A `ready` here would be
        the catalog advertising an execute path the web app does not have and must never gain.
        """
        for broker in BROKERS:
            assert broker.capabilities.trading in {"handoff", "planned", "partner", "not_available"}, (
                f"{broker.id} advertises trading as {broker.capabilities.trading!r}"
            )
