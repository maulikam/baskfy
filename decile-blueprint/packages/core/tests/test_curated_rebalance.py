from __future__ import annotations

import datetime as dt
import time
import tracemalloc
from decimal import Decimal
from fractions import Fraction
from itertools import permutations
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from baskfy_core.curated_rebalance import (
    CuratedPlanKind,
    CuratedRebalanceError,
    IntendedHolding,
    OrderSide,
    PlanPreview,
    PriceQuote,
    QuoteFreshness,
    TargetAllocation,
    plan_apply_rebalance,
    plan_full_exit,
    plan_invest,
    plan_invest_more,
    plan_partial_exit,
)

D = Decimal
NOW = dt.datetime(2026, 8, 21, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
MAX_AGE = dt.timedelta(minutes=5)
FRESHNESS = QuoteFreshness(NOW, MAX_AGE)


def _quote(instrument_id: int, price: str, *, observed_at: dt.datetime = NOW) -> PriceQuote:
    return PriceQuote(instrument_id, D(price), observed_at)


def _targets() -> tuple[TargetAllocation, ...]:
    return (TargetAllocation(1, D("0.6000")), TargetAllocation(2, D("0.4000")))


def _quotes() -> tuple[PriceQuote, ...]:
    return (_quote(1, "120.25"), _quote(2, "80.10"))


def _invest(amount: str = "500") -> PlanPreview:
    return plan_invest(D(amount), _targets(), _quotes(), FRESHNESS)


def test_invest_golden_allocates_whole_shares_and_exact_residual() -> None:
    plan = _invest()

    assert plan.kind is CuratedPlanKind.BUY
    assert plan.minimum_amount == D(201)
    assert plan.executable
    assert [(line.instrument_id, line.quantity) for line in plan.lines] == [(1, 2), (2, 2)]
    assert plan.buy_cost == D("400.70")
    assert plan.residual_cash == D("99.30")
    assert plan.sell_proceeds == D(0)
    assert plan.top_up_amount == D(0)
    assert [(row.instrument_id, row.quantity) for row in plan.resulting_holdings] == [
        (1, 2),
        (2, 2),
    ]


def test_invest_at_exact_minimum_buys_every_constituent() -> None:
    plan = _invest("201")
    assert plan.executable
    assert all(line.quantity >= 1 for line in plan.lines)
    assert plan.buy_cost == D("200.35")
    assert plan.residual_cash == D("0.65")


def test_invest_below_minimum_discloses_shortfall_without_orders() -> None:
    plan = _invest("200")
    assert not plan.executable
    assert plan.lines == ()
    assert plan.resulting_holdings == ()
    assert plan.minimum_amount == D(201)
    assert plan.top_up_amount == D(1)
    assert plan.residual_cash == D(200)


def test_invest_more_has_distinct_batch_kind_and_same_exact_allocation() -> None:
    plan = plan_invest_more(D(500), _targets(), _quotes(), FRESHNESS)
    assert plan.kind is CuratedPlanKind.INVEST_MORE
    assert plan.lines == _invest().lines
    assert plan.buy_cost == D("400.70")


def test_apply_golden_diffs_current_intended_holdings_not_previous_version() -> None:
    holdings = (IntendedHolding(1, D(10)), IntendedHolding(2, D(5)))
    targets = (TargetAllocation(1, D("0.2500")), TargetAllocation(3, D("0.7500")))
    quotes = (_quote(1, "100"), _quote(2, "200"), _quote(3, "300"))

    plan = plan_apply_rebalance(
        holdings,
        targets,
        quotes,
        cash_contribution=D(0),
        quote_freshness=FRESHNESS,
    )

    assert plan.current_value == D(2000)
    assert [
        (line.instrument_id, line.side, line.quantity, line.notional) for line in plan.lines
    ] == [
        (1, OrderSide.SELL, 5, D(500)),
        (2, OrderSide.SELL, 5, D(1000)),
        (3, OrderSide.BUY, 5, D(1500)),
    ]
    assert plan.sell_proceeds == plan.buy_cost == D(1500)
    assert plan.residual_cash == plan.top_up_amount == D(0)
    assert plan.resulting_holdings == (
        # The old second instrument is absent because the target, not v1, is authoritative.
        type(plan.resulting_holdings[0])(1, 5),
        type(plan.resulting_holdings[0])(3, 5),
    )


def test_apply_sells_fund_buys_with_exact_residual_cash() -> None:
    plan = plan_apply_rebalance(
        (IntendedHolding(1, D(3)),),
        (TargetAllocation(2, D("1.0000")),),
        (_quote(1, "101"), _quote(2, "100")),
        cash_contribution=D(0),
        quote_freshness=FRESHNESS,
    )
    assert plan.sell_proceeds == D(303)
    assert plan.buy_cost == D(300)
    assert plan.residual_cash == D(3)
    assert plan.sell_proceeds == plan.buy_cost + plan.residual_cash


def test_apply_top_up_is_minimum_additional_contribution_needed() -> None:
    holdings = (IntendedHolding(1, D(1)),)
    targets = (TargetAllocation(2, D("0.5000")), TargetAllocation(3, D("0.5000")))
    quotes = (_quote(1, "100"), _quote(2, "100"), _quote(3, "500"))

    short = plan_apply_rebalance(
        holdings,
        targets,
        quotes,
        cash_contribution=D(0),
        quote_freshness=FRESHNESS,
    )
    assert not short.executable
    assert short.minimum_amount == D(1000)
    assert short.top_up_amount == D(900)
    assert short.lines == ()
    assert short.resulting_holdings[0].quantity == 1

    partly_funded = plan_apply_rebalance(
        holdings,
        targets,
        quotes,
        cash_contribution=D(899),
        quote_freshness=FRESHNESS,
    )
    assert not partly_funded.executable
    assert partly_funded.top_up_amount == D(1)
    assert partly_funded.residual_cash == D(899)

    funded = plan_apply_rebalance(
        holdings,
        targets,
        quotes,
        cash_contribution=D(900),
        quote_freshness=FRESHNESS,
    )
    assert funded.executable
    assert funded.top_up_amount == D(0)
    assert funded.sell_proceeds + D(900) == funded.buy_cost + funded.residual_cash


def test_apply_uses_current_holdings_even_when_their_shape_is_unlike_any_version() -> None:
    plan = plan_apply_rebalance(
        (IntendedHolding(9, D(7)), IntendedHolding(1, D(1))),
        (TargetAllocation(1, D("1.0000")),),
        (_quote(9, "30"), _quote(1, "100")),
        cash_contribution=D(0),
        quote_freshness=FRESHNESS,
    )
    assert [(line.instrument_id, line.side, line.quantity) for line in plan.lines] == [
        (1, OrderSide.BUY, 2),
        (9, OrderSide.SELL, 7),
    ]
    assert plan.residual_cash == D(10)


def test_full_exit_sells_every_owned_share_and_preserves_exact_proceeds() -> None:
    plan = plan_full_exit(
        (IntendedHolding(2, D(3)), IntendedHolding(1, D(7))),
        (_quote(1, "11.25"), _quote(2, "100.10")),
        quote_freshness=FRESHNESS,
    )
    assert plan.kind is CuratedPlanKind.EXIT
    assert [(line.instrument_id, line.quantity) for line in plan.lines] == [(1, 7), (2, 3)]
    assert plan.sell_proceeds == D("379.05")
    assert plan.current_value == plan.residual_cash == D("379.05")
    assert plan.resulting_holdings == ()


def test_partial_exit_is_amount_based_proportional_floor_with_visible_remainder() -> None:
    plan = plan_partial_exit(
        (IntendedHolding(1, D(10)), IntendedHolding(2, D(5))),
        (_quote(1, "100"), _quote(2, "200")),
        D(1000),
        quote_freshness=FRESHNESS,
    )
    assert plan.kind is CuratedPlanKind.PARTIAL_EXIT
    assert [(line.instrument_id, line.quantity) for line in plan.lines] == [(1, 5), (2, 2)]
    assert plan.sell_proceeds == plan.residual_cash == D(900)
    assert plan.unallocated_amount == D(100)
    assert [(row.instrument_id, row.quantity) for row in plan.resulting_holdings] == [
        (1, 5),
        (2, 3),
    ]


def test_partial_exit_never_sells_beyond_owned_and_retained_value_is_exact() -> None:
    holdings = (IntendedHolding(1, D(2)), IntendedHolding(2, D(3)))
    prices = (_quote(1, "13"), _quote(2, "17"))
    plan = plan_partial_exit(
        holdings,
        prices,
        D(76),
        quote_freshness=FRESHNESS,
    )
    owned = {row.instrument_id: int(row.quantity) for row in holdings}
    sold = {line.instrument_id: line.quantity for line in plan.lines}
    retained = {row.instrument_id: row.quantity for row in plan.resulting_holdings}
    assert all(sold.get(key, 0) <= value for key, value in owned.items())
    assert all(sold.get(key, 0) + retained.get(key, 0) == value for key, value in owned.items())
    assert plan.sell_proceeds <= D(76)


@pytest.mark.parametrize("requested", ["0", "-1", "77", "100"])
def test_partial_exit_rejects_invalid_or_full_exit_amount(requested: str) -> None:
    with pytest.raises(CuratedRebalanceError):
        plan_partial_exit(
            (IntendedHolding(1, D(7)),),
            (_quote(1, "11"),),
            D(requested),
            quote_freshness=FRESHNESS,
        )


def test_partial_exit_rejects_amount_too_small_for_one_whole_share() -> None:
    with pytest.raises(CuratedRebalanceError, match="too small"):
        plan_partial_exit(
            (IntendedHolding(1, D(1)), IntendedHolding(2, D(1))),
            (_quote(1, "1000"), _quote(2, "2000")),
            D(1),
            quote_freshness=FRESHNESS,
        )


def test_fractional_stock_holding_is_rejected_never_truncated() -> None:
    with pytest.raises(CuratedRebalanceError, match="whole shares"):
        plan_full_exit(
            (IntendedHolding(1, D("1.0001")),),
            (_quote(1, "100"),),
            quote_freshness=FRESHNESS,
        )


@pytest.mark.parametrize(
    ("targets", "message"),
    [
        ((TargetAllocation(1, D("0.5000")), TargetAllocation(1, D("0.5000"))), "duplicate"),
        ((TargetAllocation(1, D("0.9999")),), "total exactly"),
        (
            (TargetAllocation(1, D("0.50001")), TargetAllocation(2, D("0.49999"))),
            "four decimal",
        ),
        ((TargetAllocation(1, D("NaN")),), "finite Decimal"),
    ],
)
def test_invest_rejects_duplicate_or_malformed_weights(
    targets: tuple[TargetAllocation, ...], message: str
) -> None:
    quotes = tuple(_quote(row.instrument_id, "100") for row in targets)
    with pytest.raises(CuratedRebalanceError, match=message):
        plan_invest(D(1000), targets, quotes, FRESHNESS)


def test_plan_rejects_missing_duplicate_unexpected_and_nonpositive_quotes() -> None:
    with pytest.raises(CuratedRebalanceError, match="missing"):
        plan_invest(D(500), _targets(), (_quote(1, "100"),), FRESHNESS)
    with pytest.raises(CuratedRebalanceError, match="duplicate"):
        plan_invest(
            D(500),
            _targets(),
            (_quote(1, "100"), _quote(1, "101"), _quote(2, "80")),
            quote_freshness=FRESHNESS,
        )
    with pytest.raises(CuratedRebalanceError, match="unexpected"):
        plan_invest(
            D(500),
            _targets(),
            (*_quotes(), _quote(3, "90")),
            quote_freshness=FRESHNESS,
        )
    with pytest.raises(CuratedRebalanceError, match="greater than zero"):
        plan_invest(
            D(500),
            _targets(),
            (_quote(1, "0"), _quote(2, "80")),
            quote_freshness=FRESHNESS,
        )


def test_plan_rejects_stale_future_naive_and_float_quotes() -> None:
    exactly_fresh = PriceQuote(1, D(100), NOW - MAX_AGE)
    plan = plan_invest(
        D(500),
        (TargetAllocation(1, D("1.0000")),),
        (exactly_fresh,),
        quote_freshness=FRESHNESS,
    )
    assert plan.executable

    with pytest.raises(CuratedRebalanceError, match="stale"):
        plan_invest(
            D(500),
            (TargetAllocation(1, D("1.0000")),),
            (PriceQuote(1, D(100), NOW - MAX_AGE - dt.timedelta(microseconds=1)),),
            quote_freshness=FRESHNESS,
        )
    with pytest.raises(CuratedRebalanceError, match="future"):
        plan_invest(
            D(500),
            (TargetAllocation(1, D("1.0000")),),
            (PriceQuote(1, D(100), NOW + dt.timedelta(microseconds=1)),),
            quote_freshness=FRESHNESS,
        )
    with pytest.raises(CuratedRebalanceError, match="timezone-aware"):
        plan_invest(
            D(500),
            (TargetAllocation(1, D("1.0000")),),
            (PriceQuote(1, D(100), NOW.replace(tzinfo=None)),),
            quote_freshness=FRESHNESS,
        )
    with pytest.raises(CuratedRebalanceError, match="finite Decimal"):
        plan_invest(
            D(500),
            (TargetAllocation(1, D("1.0000")),),
            (PriceQuote(1, cast(Decimal, 100.0), NOW),),
            quote_freshness=FRESHNESS,
        )


def test_plan_rejects_invalid_amount_holdings_and_time_contracts() -> None:
    with pytest.raises(CuratedRebalanceError, match="greater than zero"):
        _invest("-1")
    with pytest.raises(CuratedRebalanceError, match="duplicate holding"):
        plan_full_exit(
            (IntendedHolding(1, D(1)), IntendedHolding(1, D(2))),
            (_quote(1, "100"),),
            quote_freshness=FRESHNESS,
        )
    with pytest.raises(CuratedRebalanceError, match="timezone-aware"):
        plan_invest(
            D(500),
            _targets(),
            _quotes(),
            quote_freshness=QuoteFreshness(NOW.replace(tzinfo=None), MAX_AGE),
        )
    with pytest.raises(CuratedRebalanceError, match="non-negative timedelta"):
        plan_invest(
            D(500),
            _targets(),
            _quotes(),
            quote_freshness=QuoteFreshness(NOW, dt.timedelta(seconds=-1)),
        )


def test_all_plan_results_are_permutation_invariant_and_id_sorted() -> None:
    holdings = (IntendedHolding(3, D(4)), IntendedHolding(1, D(5)), IntendedHolding(2, D(6)))
    targets = (
        TargetAllocation(2, D("0.2000")),
        TargetAllocation(1, D("0.3000")),
        TargetAllocation(4, D("0.5000")),
    )
    quotes = (_quote(4, "41"), _quote(2, "19"), _quote(1, "11"), _quote(3, "29"))
    expected = plan_apply_rebalance(
        holdings,
        targets,
        quotes,
        cash_contribution=D(0),
        quote_freshness=FRESHNESS,
    )
    for holding_order in permutations(holdings):
        for target_order in permutations(targets):
            observed = plan_apply_rebalance(
                holding_order,
                target_order,
                tuple(reversed(quotes)),
                cash_contribution=D(0),
                quote_freshness=FRESHNESS,
            )
            assert observed == expected


@given(
    first_quantity=st.integers(min_value=1, max_value=10_000),
    second_quantity=st.integers(min_value=1, max_value=10_000),
    first_price_paise=st.integers(min_value=1, max_value=1_000_000),
    second_price_paise=st.integers(min_value=1, max_value=1_000_000),
    target_price_paise=st.integers(min_value=1, max_value=1_000_000),
)
@settings(max_examples=150, deadline=None)
def test_apply_cash_conservation_property_matches_fraction_oracle(
    first_quantity: int,
    second_quantity: int,
    first_price_paise: int,
    second_price_paise: int,
    target_price_paise: int,
) -> None:
    first_price = D(first_price_paise) / 100
    second_price = D(second_price_paise) / 100
    target_price = D(target_price_paise) / 100
    current_value = first_price * first_quantity + second_price * second_quantity
    minimum = D((target_price_paise + 99) // 100)
    contribution = max(D(0), minimum - current_value)
    plan = plan_apply_rebalance(
        (IntendedHolding(1, D(first_quantity)), IntendedHolding(2, D(second_quantity))),
        (TargetAllocation(3, D("1.0000")),),
        (
            _quote(1, str(first_price)),
            _quote(2, str(second_price)),
            _quote(3, str(target_price)),
        ),
        cash_contribution=contribution,
        quote_freshness=FRESHNESS,
    )
    oracle_quantity = int(Fraction(current_value + contribution) / Fraction(target_price))
    assert plan.resulting_holdings[0].quantity == oracle_quantity
    assert plan.sell_proceeds + contribution == plan.buy_cost + plan.residual_cash
    assert plan.residual_cash >= 0


@given(
    first_quantity=st.integers(min_value=1, max_value=10_000),
    second_quantity=st.integers(min_value=1, max_value=10_000),
    first_price_paise=st.integers(min_value=1, max_value=1_000_000),
    second_price_paise=st.integers(min_value=1, max_value=1_000_000),
    exit_basis_points=st.integers(min_value=1, max_value=9_999),
)
@settings(max_examples=150, deadline=None)
def test_partial_exit_fraction_oracle_never_oversells_or_overpays(
    first_quantity: int,
    second_quantity: int,
    first_price_paise: int,
    second_price_paise: int,
    exit_basis_points: int,
) -> None:
    first_price = D(first_price_paise) / 100
    second_price = D(second_price_paise) / 100
    current = first_price * first_quantity + second_price * second_quantity
    requested = current * D(exit_basis_points) / D(10_000)
    oracle = (
        int(Fraction(first_quantity) * Fraction(requested) / Fraction(current)),
        int(Fraction(second_quantity) * Fraction(requested) / Fraction(current)),
    )
    if oracle == (0, 0):
        with pytest.raises(CuratedRebalanceError, match="too small"):
            plan_partial_exit(
                (IntendedHolding(1, D(first_quantity)), IntendedHolding(2, D(second_quantity))),
                (_quote(1, str(first_price)), _quote(2, str(second_price))),
                requested,
                quote_freshness=FRESHNESS,
            )
        return
    plan = plan_partial_exit(
        (IntendedHolding(1, D(first_quantity)), IntendedHolding(2, D(second_quantity))),
        (_quote(1, str(first_price)), _quote(2, str(second_price))),
        requested,
        quote_freshness=FRESHNESS,
    )
    sold = {line.instrument_id: line.quantity for line in plan.lines}
    assert (sold.get(1, 0), sold.get(2, 0)) == oracle
    assert plan.sell_proceeds <= requested
    assert plan.unallocated_amount == requested - plan.sell_proceeds


def test_ten_thousand_name_apply_is_linear_and_memory_bounded() -> None:
    count = 10_000
    weight = D("0.0001")
    holdings = tuple(IntendedHolding(index, D(1)) for index in range(1, count + 1))
    targets = tuple(TargetAllocation(index, weight) for index in range(1, count + 1))
    quotes = tuple(_quote(index, str(index % 997 + 1)) for index in range(1, count + 1))

    tracemalloc.start()
    started = time.perf_counter()
    plan = plan_apply_rebalance(
        holdings,
        targets,
        quotes,
        cash_contribution=D(10_000_000),
        quote_freshness=FRESHNESS,
    )
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert plan.executable
    assert len(plan.resulting_holdings) == count
    assert elapsed < 3.0
    assert peak < 35 * 1024 * 1024
