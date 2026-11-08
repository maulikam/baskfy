"""Immutable versions + apply-preview diffs — docs/smallcase/04 §5 (SC3 leaf 1.2.1).

Tests assert the *spec*: versions are append-only; the apply preview diffs the investor's
current intended holdings (not the previous version) against new target weights at current
prices → buy/sell amounts; sells fund buys; residual cash and top-up cover min-amount gaps.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.curated_baskets import VersionImmutableError, assert_weights_sum_to_one
from baskfy_core.curated_metrics import min_amount
from baskfy_core.curated_versions import (
    BUY,
    SELL,
    ConstituentDraft,
    assert_next_version_no,
    build_published_version,
    classify_version_change,
    desk_orders_from_diff,
    diff_holdings_vs_weights,
    publish_side_effects,
    refuse_version_mutation,
)
from baskfy_core.gst import money


def _d(value: str) -> Decimal:
    return Decimal(value)


# ---------------------------------------------------------------------------
# Hand-computed fixture (04 §5 semantics)
#
# Holdings (intended ledger):
#   A: 10 @ ₹100 = ₹1,000
#   B:  5 @ ₹200 = ₹1,000
# Portfolio value V = ₹2,000
#
# New target weights: A 0.25, B 0.25, C 0.50  (C @ ₹100)
#   target A = 500 → sell ₹500 → floor(500/100) = 5 shares
#   target B = 500 → sell ₹500 → floor(500/200) = 2 shares (₹400)
#   target C = 1,000 → buy ₹1,000 → floor(1000/100) = 10 shares
# Sell proceeds = 500 + 400 = 900; buy cost = 1,000; residual = -100 → top-up ≥ 100
# ---------------------------------------------------------------------------

FIXTURE_HOLDINGS = {1: _d("10"), 2: _d("5")}
FIXTURE_PRICES = {1: _d("100"), 2: _d("200"), 3: _d("100")}
FIXTURE_TARGETS = {1: _d("0.2500"), 2: _d("0.2500"), 3: _d("0.5000")}


class TestClassifyVersionChange:
    def test_genesis(self) -> None:
        label, added, removed = classify_version_change((), (10, 20, 30), is_genesis=True)
        assert label == "GENESIS"
        assert added == 3
        assert removed == 0

    def test_changed_membership(self) -> None:
        label, added, removed = classify_version_change((1, 2), (2, 3), is_genesis=False)
        assert label == "CHANGED"
        assert added == 1
        assert removed == 1

    def test_no_change_same_set(self) -> None:
        label, added, removed = classify_version_change((1, 2), (2, 1), is_genesis=False)
        assert label == "NO_CHANGE"
        assert added == 0
        assert removed == 0


class TestAssertNextVersionNo:
    def test_genesis_is_one(self) -> None:
        assert_next_version_no(existing_version_nos=(), next_version_no=1)

    def test_sequential_ok(self) -> None:
        assert_next_version_no(existing_version_nos=(1, 2), next_version_no=3)

    def test_duplicate_refused(self) -> None:
        with pytest.raises(VersionImmutableError, match="version_no 2"):
            assert_next_version_no(existing_version_nos=(1, 2), next_version_no=2)

    def test_gap_refused(self) -> None:
        with pytest.raises(VersionImmutableError, match="expected 3"):
            assert_next_version_no(existing_version_nos=(1, 2), next_version_no=4)

    def test_refuse_mutation_raises(self) -> None:
        with pytest.raises(VersionImmutableError, match="append-only"):
            refuse_version_mutation()


class TestBuildPublishedVersion:
    def test_genesis_draft(self) -> None:
        draft = build_published_version(
            version_no=1,
            effective_date=dt.date(2026, 8, 22),
            constituents=[
                ConstituentDraft(1, _d("0.5000")),
                ConstituentDraft(2, _d("0.5000")),
            ],
            existing_version_nos=(),
        )
        assert draft.label == "GENESIS"
        assert draft.added_count == 2
        assert draft.removed_count == 0
        assert_weights_sum_to_one(draft.weights)

    def test_refuses_duplicate_version_no(self) -> None:
        with pytest.raises(VersionImmutableError, match="version_no 2"):
            build_published_version(
                version_no=2,
                effective_date=dt.date(2026, 8, 22),
                constituents=[ConstituentDraft(1, _d("1.0000"))],
                existing_version_nos=(1, 2),
            )

    def test_weight_only_change_is_changed(self) -> None:
        draft = build_published_version(
            version_no=2,
            effective_date=dt.date(2026, 8, 22),
            constituents=[
                ConstituentDraft(1, _d("0.6000")),
                ConstituentDraft(2, _d("0.4000")),
            ],
            existing_version_nos=(1,),
            previous_instrument_ids=(1, 2),
            previous_weights_by_id={1: _d("0.5000"), 2: _d("0.5000")},
        )
        assert draft.label == "CHANGED"
        assert draft.added_count == 0
        assert draft.removed_count == 0

    def test_identical_weights_are_no_change(self) -> None:
        draft = build_published_version(
            version_no=2,
            effective_date=dt.date(2026, 8, 22),
            constituents=[
                ConstituentDraft(1, _d("0.5000")),
                ConstituentDraft(2, _d("0.5000")),
            ],
            existing_version_nos=(1,),
            previous_instrument_ids=(1, 2),
            previous_weights_by_id={1: _d("0.5000"), 2: _d("0.5000")},
        )
        assert draft.label == "NO_CHANGE"

    def test_rejects_bad_weight_sum(self) -> None:
        with pytest.raises(ValueError, match="must sum to"):
            build_published_version(
                version_no=1,
                effective_date=dt.date(2026, 8, 22),
                constituents=[
                    ConstituentDraft(1, _d("0.6000")),
                    ConstituentDraft(2, _d("0.5000")),
                ],
                existing_version_nos=(),
            )

    def test_rejects_duplicate_instrument(self) -> None:
        with pytest.raises(ValueError, match="duplicate instrument_id"):
            build_published_version(
                version_no=1,
                effective_date=dt.date(2026, 8, 22),
                constituents=[
                    ConstituentDraft(1, _d("0.5000")),
                    ConstituentDraft(1, _d("0.5000")),
                ],
                existing_version_nos=(),
            )


class TestPublishSideEffects:
    def test_04_section5_shapes(self) -> None:
        effects = publish_side_effects(
            version_no=2,
            label="CHANGED",
            added_count=1,
            removed_count=1,
            active_investor_user_ids=(42, 42, 7),
        )
        assert effects.update_post_source == "ENGINE"
        assert effects.pending_action_type == "REBALANCE_AVAILABLE"
        assert effects.rebalance_state == "PENDING"
        assert effects.affected_user_ids == (42, 7)
        assert "Version 2" in effects.update_post_title
        assert "+1 / -1" in effects.update_post_body_md


class TestDiffHoldingsVsWeights:
    """Hand-computed fixture: holdings vs new targets — not vs previous version."""

    def test_fixture_buy_sell_amounts_and_qtys(self) -> None:
        diff = diff_holdings_vs_weights(FIXTURE_HOLDINGS, FIXTURE_TARGETS, FIXTURE_PRICES)
        assert diff.portfolio_value == _d("2000.00")

        sells = {line.instrument_id: line for line in diff.sells}
        buys = {line.instrument_id: line for line in diff.buys}

        assert set(sells) == {1, 2}
        assert set(buys) == {3}

        assert sells[1].qty == _d("5")
        assert sells[1].amount == _d("500.00")
        assert sells[1].current_weight == _d("0.5000")
        assert sells[1].target_weight == _d("0.2500")

        assert sells[2].qty == _d("2")
        assert sells[2].amount == _d("400.00")

        assert buys[3].qty == _d("10")
        assert buys[3].amount == _d("1000.00")
        assert buys[3].side == BUY
        assert sells[1].side == SELL

        assert diff.residual_cash == money(_d("900") - _d("1000"))
        assert diff.residual_cash == _d("-100.00")

        required = min_amount(
            [_d("100"), _d("200"), _d("100")],
            [_d("0.2500"), _d("0.2500"), _d("0.5000")],
        )
        assert diff.min_amount == required
        assert diff.top_up == max(_d("100.00"), money(max(_d("0"), required - _d("2000"))))

    def test_diffs_holdings_not_previous_version(self) -> None:
        """04 §5: preview uses intended holdings, even if they diverge from last version."""
        holdings = {1: _d("20")}  # ₹2,000 at 100
        prices = {1: _d("100"), 2: _d("100")}
        targets = {2: _d("1.0000")}
        diff = diff_holdings_vs_weights(holdings, targets, prices)
        assert {line.instrument_id for line in diff.sells} == {1}
        assert {line.instrument_id for line in diff.buys} == {2}
        assert diff.sells[0].qty == _d("20")
        assert diff.buys[0].qty == _d("20")
        assert diff.residual_cash == _d("0.00")
        assert diff.top_up == _d("0.00")

    def test_already_on_target_yields_empty_lines(self) -> None:
        holdings = {1: _d("10"), 2: _d("5")}  # 1000 + 1000
        prices = {1: _d("100"), 2: _d("200")}
        targets = {1: _d("0.5000"), 2: _d("0.5000")}
        diff = diff_holdings_vs_weights(holdings, targets, prices)
        assert diff.lines == ()
        assert diff.residual_cash == _d("0.00")

    def test_never_sells_more_than_held(self) -> None:
        holdings = {1: _d("1")}
        prices = {1: _d("100"), 2: _d("100")}
        targets = {2: _d("1.0000")}
        diff = diff_holdings_vs_weights(holdings, targets, prices)
        assert diff.sells[0].qty == _d("1")

    def test_missing_price_fails(self) -> None:
        with pytest.raises(ValueError, match="missing price"):
            diff_holdings_vs_weights({1: _d("1")}, {1: _d("1")}, {})

    def test_empty_targets_fail(self) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            diff_holdings_vs_weights({}, {}, {1: _d("1")})

    def test_all_amounts_are_money_decimals(self) -> None:
        diff = diff_holdings_vs_weights(FIXTURE_HOLDINGS, FIXTURE_TARGETS, FIXTURE_PRICES)
        for line in diff.lines:
            assert line.amount == money(line.amount)
            assert isinstance(line.amount, Decimal)
            assert isinstance(line.qty, Decimal)
            assert isinstance(line.weight_delta, Decimal)
        assert isinstance(diff.residual_cash, Decimal)
        assert isinstance(diff.top_up, Decimal)


class TestDeskOrdersFromDiff:
    def test_maps_symbols_sides_qtys_weights(self) -> None:
        diff = diff_holdings_vs_weights(FIXTURE_HOLDINGS, FIXTURE_TARGETS, FIXTURE_PRICES)
        orders = desk_orders_from_diff(diff, {1: "aaa", 2: "BBB", 3: "CCC"})
        by_symbol = {o.symbol: o for o in orders}
        assert set(by_symbol) == {"AAA", "BBB", "CCC"}
        assert by_symbol["AAA"].side == SELL and by_symbol["AAA"].qty == 5
        assert by_symbol["BBB"].side == SELL and by_symbol["BBB"].qty == 2
        assert by_symbol["CCC"].side == BUY and by_symbol["CCC"].qty == 10
        assert by_symbol["CCC"].weight == _d("0.5000")

    def test_missing_symbol_fails(self) -> None:
        diff = diff_holdings_vs_weights(FIXTURE_HOLDINGS, FIXTURE_TARGETS, FIXTURE_PRICES)
        with pytest.raises(ValueError, match="missing symbol"):
            desk_orders_from_diff(diff, {1: "AAA"})
