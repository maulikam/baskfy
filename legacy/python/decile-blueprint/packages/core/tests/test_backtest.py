"""docs/10 §"Correctness harness" — all six, plus the engine's own unit tests.

    "**Correctness harness (non-negotiable tests)**
     1. Look-ahead trap … 2. Buy-and-hold identity … 3. Zero-cost, zero-turnover identity …
     4. Determinism … 5. Survivorship … 6. Cost monotonicity."

Each of the six has a test named after it below, quoting the sentence it enforces. The market
they run against is ``backtest_fixtures.build_market`` — see that module for why a synthetic
market is the only thing an *identity* can be checked against.

Tolerances
----------
Two of the six are identities that whole-share rounding breaks by a knowable amount, and the
tests state that amount rather than waving at it. docs/10 §5 requires whole shares; a portfolio
that can only buy 2,317 shares of a ₹431.55 stock cannot hold exactly 2.5% of its book in it, so
the residue sits in cash and drags. Every tolerance below is a **stated multiple of that
residue**, not a number chosen until the test went green.
"""

from __future__ import annotations

import datetime as dt
import inspect
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from itertools import pairwise

import backtest_fixtures as fixtures
import numpy as np
import polars as pl
import pytest
from benchmarks.budgets import record

from baskfy_core.backtest import (
    BacktestConfig,
    BacktestConfigError,
    BacktestData,
    BacktestDataError,
    BacktestResult,
    CashPolicy,
    CostSpec,
    DividendPolicy,
    LookAheadError,
    PointInTimeReader,
    PositionLimits,
    PricePanel,
    RebalanceDay,
    RebalanceFrequency,
    RebalanceSpec,
    RiskOverlay,
    SelectionSpec,
    TradeReason,
    Weighting,
    apply_position_limits,
    rebalance_dates,
    run_backtest,
    run_fragility,
    shift_schedule,
    year_fraction,
)
from baskfy_core.backtest_metrics import (
    compute_metrics,
    downsample_curve,
    drawdown_series,
    metrics_hash,
    monthly_returns,
    round_trips,
)

MONTHLY = RebalanceSpec(frequency=RebalanceFrequency.MONTHLY)
NO_COSTS = CostSpec(brokerage_bps=Decimal(0), stt_bps=Decimal(0), slippage_bps=Decimal(0))
#: A backtest that must reproduce an index has to be allowed to hold the index's own weights.
UNCAPPED = PositionLimits(max_weight=Decimal(1), min_weight=Decimal(0))


def _market_and_data(  # noqa: PLR0913 - one knob per axis the six tests vary
    *,
    end: dt.date = dt.date(2013, 12, 31),
    instruments: int = 40,
    delist: tuple[int, dt.date] | None = None,
    spec: RebalanceSpec = MONTHLY,
    start: dt.date = dt.date(2011, 1, 3),
    offsets: bool = False,
) -> tuple[fixtures.SyntheticMarket, BacktestData, tuple[dt.date, ...]]:
    market = fixtures.build_market(start=start, end=end, instruments=instruments, delist=delist)
    schedule = rebalance_dates(market.calendar, start, end, spec)
    wanted = set(schedule)
    if offsets:
        wanted |= set(shift_schedule(market.calendar, schedule, -1))
        wanted |= set(shift_schedule(market.calendar, schedule, 1))
    return market, market.data(sorted(wanted)), schedule


def _config(**overrides: object) -> BacktestConfig:
    base: dict[str, object] = {
        "start": dt.date(2011, 1, 3),
        "end": dt.date(2013, 12, 31),
        "initial_capital": Decimal(10_000_000),
        "rebalance": MONTHLY,
        "selection": SelectionSpec(top_n=20, hold_buffer=10),
        "weighting": Weighting.EQUAL,
    }
    base.update(overrides)
    return BacktestConfig.model_validate(base)


# ---------------------------------------------------------------------------
# docs/10 §"Correctness harness" 1 — look-ahead trap
# ---------------------------------------------------------------------------


def test_look_ahead_trap_raises() -> None:
    """docs/10: "inject a factor column that is deliberately shifted forward one day; the guard
    must raise, and the test asserts it does"."""
    market, data, schedule = _market_and_data()
    poisoned_day = schedule[3]
    clean = data.screens[poisoned_day]
    shifted = clean.with_columns((pl.col("date") + pl.duration(days=1)).alias("date"))
    screens = dict(data.screens)
    screens[poisoned_day] = shifted
    poisoned = BacktestData(
        calendar=data.calendar,
        prices=data.prices,
        screens=screens,
        benchmark=data.benchmark,
        delistings=data.delistings,
        symbols=data.symbols,
        names=data.names,
        data_version=data.data_version,
    )

    with pytest.raises(LookAheadError) as raised:
        run_backtest(_config(), poisoned)

    assert raised.value.as_of == poisoned_day
    assert raised.value.offending == poisoned_day + dt.timedelta(days=1)
    assert "look-ahead" in str(raised.value).lower()
    del market


def test_the_guard_is_not_a_flag() -> None:
    """There is no way to turn the guard off — no parameter, no environment variable.

    Asserted rather than assumed, because "always on in backtests, not a debug flag" is exactly
    the kind of requirement that decays into a keyword argument someone sets in a hurry.
    """
    signature = inspect.signature(run_backtest)
    assert set(signature.parameters) == {"config", "data", "progress", "schedule"}
    assert not any("guard" in name or "check" in name for name in signature.parameters)


def test_the_clock_cannot_move_backwards() -> None:
    market, data, schedule = _market_and_data()
    reader = PointInTimeReader(data)
    reader.advance_to(schedule[2])
    with pytest.raises(Exception, match="cannot move back"):
        reader.advance_to(schedule[1])
    del market


def test_a_price_read_beyond_the_cursor_raises() -> None:
    market, data, schedule = _market_and_data()
    reader = PointInTimeReader(data)
    reader.advance_to(schedule[0])
    with pytest.raises(LookAheadError):
        reader.price_close(1, schedule[1])
    del market


# ---------------------------------------------------------------------------
# docs/10 §"Correctness harness" 2 — buy-and-hold identity
# ---------------------------------------------------------------------------


def test_buy_and_hold_reproduces_the_benchmark() -> None:
    """docs/10: "a 'screen' that always returns the benchmark's constituents with marketcap
    weighting must reproduce the benchmark's return within costs".

    The fixture's benchmark **is** the value-weighted basket of its own constituents
    (``SyntheticMarket.benchmark_frame``), so the only differences left are whole-share rounding
    and the overnight gap between the close the weights are measured at and the open they are
    filled at. Both are bounded, and the tolerance below is stated in those terms.
    """
    market, data, _ = _market_and_data(instruments=40)
    config = _config(
        initial_capital=Decimal(1_000_000_000),
        selection=SelectionSpec(top_n=40, hold_buffer=0),
        weighting=Weighting.MARKETCAP,
        position_limits=UNCAPPED,
        costs=NO_COSTS,
    )
    result = run_backtest(config, data)
    metrics = compute_metrics(result)

    assert metrics.benchmark_total_return is not None
    index_growth = 1.0 + metrics.benchmark_total_return
    portfolio_growth = 1.0 + metrics.total_return

    # Rounding residue: at most one share per name, so at most sum(open_i) of the book sits in
    # cash. On the first fill that is well under 0.01% of a hundred-crore book.
    first_fill = market.calendar[market.calendar.index(data.screens and min(data.screens)) + 1]
    residue = sum(market.open_prices(first_fill).values()) / config.initial_capital
    assert residue < Decimal("0.0001")

    # Measured: 0.085% on this fixture. The bound is ~2x that — tight enough that a real
    # execution bug (wrong price, wrong day, a missed rebalance) blows straight through it.
    relative = abs(portfolio_growth - index_growth) / index_growth
    assert relative < 0.002, (
        f"buy-and-hold grew {portfolio_growth:.6f} against the index's {index_growth:.6f}"
    )


# ---------------------------------------------------------------------------
# docs/10 §"Correctness harness" 3 — zero-cost, zero-turnover identity
# ---------------------------------------------------------------------------


def _book_value(quantities: Mapping[int, Decimal], prices: Mapping[int, Decimal]) -> Decimal:
    return sum(
        (quantities.get(key, Decimal(0)) * price for key, price in prices.items()), Decimal(0)
    )


def _equal_weighted_reference(
    market: fixtures.SyntheticMarket,
    schedule: Sequence[dt.date],
    capital: Decimal,
    universe: int,
) -> Decimal:
    """The same portfolio with **fractional** shares and no costs, computed independently.

    This is the closed-form answer docs/10's third test compares against: hold every name in the
    universe at 1/n, rebalanced on the same dates, filled at the same opens. The only thing the
    engine does differently is round to whole shares.
    """
    calendar = list(market.calendar)
    fills = [
        calendar[calendar.index(day) + 1]
        for day in schedule
        if calendar.index(day) + 1 < len(calendar)
    ]
    weight = Decimal(1) / Decimal(universe)
    quantities: dict[int, Decimal] = {}
    equity = capital
    for index, fill in enumerate(fills):
        opens = market.open_prices(fill)
        equity = _book_value(quantities, opens) if index else capital
        quantities = {key: equity * weight / price for key, price in opens.items()}
    return _book_value(quantities, market.close_prices(calendar[-1]))


def test_zero_cost_zero_turnover_equals_the_equal_weighted_universe() -> None:
    """docs/10: "`top_n` = universe size, equal weight, no costs -> equals the equal-weighted
    universe return"."""
    market, data, schedule = _market_and_data(instruments=40)
    capital = Decimal(1_000_000_000)
    config = _config(
        initial_capital=capital,
        selection=SelectionSpec(top_n=40, hold_buffer=0),
        weighting=Weighting.EQUAL,
        costs=NO_COSTS,
    )
    result = run_backtest(config, data)

    reference = _equal_weighted_reference(market, schedule, capital, 40)
    engine = result.final_equity
    relative = abs(engine - reference) / reference

    # Measured: 3.2e-6, which is the whole-share residue and nothing else.
    assert result.total_costs == Decimal(0)
    assert relative < Decimal("0.00005"), (
        f"engine finished at {engine} against the fractional-share reference {reference}"
    )


# ---------------------------------------------------------------------------
# docs/10 §"Correctness harness" 4 — determinism
# ---------------------------------------------------------------------------


def test_same_config_and_data_version_gives_the_same_metrics_hash() -> None:
    """docs/10: "same config + same `data_version` -> identical metrics hash"."""
    _, first_data, _ = _market_and_data()
    _, second_data, _ = _market_and_data()
    config = _config()

    first = run_backtest(config, first_data)
    second = run_backtest(config, second_data)

    left = metrics_hash(compute_metrics(first), config, first.data_version)
    right = metrics_hash(compute_metrics(second), config, second.data_version)
    assert left == right
    assert len(left) == 64


def test_a_different_data_version_gives_a_different_hash() -> None:
    """The converse, which is what makes the hash worth storing: it changes when the data does."""
    _, data, _ = _market_and_data()
    config = _config()
    result = run_backtest(config, data)
    metrics = compute_metrics(result)
    assert metrics_hash(metrics, config, 1) != metrics_hash(metrics, config, 2)


def test_a_different_config_gives_a_different_hash() -> None:
    _, data, _ = _market_and_data()
    base = _config()
    other = _config(selection=SelectionSpec(top_n=15, hold_buffer=10))
    first = compute_metrics(run_backtest(base, data))
    second = compute_metrics(run_backtest(other, data))
    assert metrics_hash(first, base, 1) != metrics_hash(second, other, 1)


# ---------------------------------------------------------------------------
# docs/10 §"Correctness harness" 5 — survivorship
# ---------------------------------------------------------------------------


def test_a_delisted_name_shows_the_loss() -> None:
    """docs/10: "a fixture universe containing a delisted name must show the loss; a run that
    silently drops it fails the test"."""
    stop_on = dt.date(2012, 6, 15)
    victim = 7
    _, honest, _ = _market_and_data(delist=(victim, stop_on))

    config = _config(
        selection=SelectionSpec(top_n=40, hold_buffer=0),
        weighting=Weighting.EQUAL,
        costs=NO_COSTS,
    )
    with_delisting = run_backtest(config, honest)

    # The survivorship-biased counterfactual: the same market with the doomed name never in it.
    survivors = BacktestData(
        calendar=honest.calendar,
        prices=honest.prices,
        screens={
            day: frame.filter(pl.col("instrument_id") != victim)
            for day, frame in honest.screens.items()
        },
        benchmark=honest.benchmark,
        delistings={},
        symbols=honest.symbols,
        names=honest.names,
        data_version=honest.data_version,
    )
    without = run_backtest(config, survivors)

    delisted = [trade for trade in with_delisting.trades if trade.reason is TradeReason.DELIST]
    assert delisted, "the delisted name was never liquidated — it was silently dropped"
    assert delisted[0].instrument_id == victim
    assert with_delisting.delistings and with_delisting.delistings[0].symbol == "SYN007"
    # The event is logged with the price it settled at and the day that price came from.
    assert with_delisting.delistings[0].priced_on < with_delisting.delistings[0].date

    assert with_delisting.final_equity < without.final_equity, (
        "carrying a name that went to zero and delisted did not cost the portfolio anything, "
        "which means the loss was not taken"
    )


def test_a_delisted_holding_is_never_forward_filled() -> None:
    """docs/10 §8: "Never forward-fill a dead instrument (this is exactly how survivorship bias
    sneaks in)"."""
    stop_on = dt.date(2012, 6, 15)
    victim = 7
    _, data, _ = _market_and_data(delist=(victim, stop_on))
    result = run_backtest(
        _config(selection=SelectionSpec(top_n=40, hold_buffer=0), costs=NO_COSTS), data
    )
    liquidated_on = next(
        trade.date for trade in result.trades if trade.reason is TradeReason.DELIST
    )
    # After the liquidation the name contributes nothing: no further trade, no carried value.
    assert not [
        trade
        for trade in result.trades
        if trade.instrument_id == victim and trade.date > liquidated_on
    ]
    assert all(
        holding.instrument_id != victim
        for holding in result.holdings
        if holding.executed_on > liquidated_on
    )


# ---------------------------------------------------------------------------
# docs/10 §"Correctness harness" 6 — cost monotonicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("weighting", [Weighting.EQUAL, Weighting.RANK])
def test_raising_slippage_never_raises_net_return(weighting: Weighting) -> None:
    """docs/10: "raising `slippage_bps` must never increase net return"."""
    _, data, _ = _market_and_data()
    ladder = [Decimal(0), Decimal(5), Decimal(15), Decimal(30), Decimal(60), Decimal(120)]
    finals: list[Decimal] = []
    for slippage in ladder:
        config = _config(
            weighting=weighting,
            costs=CostSpec(brokerage_bps=Decimal(3), stt_bps=Decimal(10), slippage_bps=slippage),
        )
        finals.append(run_backtest(config, data).final_equity)

    for cheaper, dearer in pairwise(finals):
        assert dearer <= cheaper, (
            f"net return rose when slippage did: {finals} for slippage {ladder}"
        )


def test_costs_are_charged_on_traded_notional() -> None:
    """docs/10 §5: "Apply costs on traded notional"."""
    _, data, _ = _market_and_data()
    costs = CostSpec(brokerage_bps=Decimal(3), stt_bps=Decimal(10), slippage_bps=Decimal(15))
    result = run_backtest(_config(costs=costs), data)
    expected = sum((costs.charge(trade.notional) for trade in result.trades), Decimal(0))
    assert result.total_costs == expected
    assert result.total_costs > 0


# ---------------------------------------------------------------------------
# Execution model details docs/10 fixes by name
# ---------------------------------------------------------------------------


def test_orders_execute_at_the_next_trading_days_open() -> None:
    """docs/10 §4: "Execute at the **next trading day's open** (`d+1`), not at `d`'s close"."""
    market, data, schedule = _market_and_data()
    result = run_backtest(_config(), data)
    calendar = list(market.calendar)

    first_decision = schedule[0]
    first_fill = calendar[calendar.index(first_decision) + 1]
    first_trades = [trade for trade in result.trades if trade.date == first_fill]
    assert first_trades, "nothing was filled on the day after the first rebalance"
    assert not [trade for trade in result.trades if trade.date <= first_decision]

    opens = market.open_prices(first_fill)
    for trade in first_trades:
        assert trade.price == opens[trade.instrument_id]


def test_share_counts_are_whole() -> None:
    """docs/10 §5: "Round to whole shares"."""
    _, data, _ = _market_and_data()
    result = run_backtest(_config(), data)
    assert all(isinstance(trade.quantity, int) and trade.quantity > 0 for trade in result.trades)
    assert all(holding.quantity > 0 for holding in result.holdings)


def test_the_book_is_never_overdrawn() -> None:
    """Cash may reach zero. It may not go below it: this engine has no margin account."""
    _, data, _ = _market_and_data()
    result = run_backtest(_config(), data)
    assert min(result.cash) >= Decimal(0)


def test_the_hold_buffer_reduces_turnover() -> None:
    """docs/01 §8, quoted by `baskfy_core.rank_buffer`: the buffer "materially reduces turnover".

    Measured in **traded notional**, not in fills. A buffered book holds more names (up to
    ``top_n + hold_buffer``), so equal-weighting it produces *more* small adjustments while
    churning far fewer positions — counting fills would report the opposite of the truth.
    """
    _, data, _ = _market_and_data()
    strict = run_backtest(_config(selection=SelectionSpec(top_n=20, hold_buffer=0)), data)
    buffered = run_backtest(_config(selection=SelectionSpec(top_n=20, hold_buffer=10)), data)

    def churn(result: BacktestResult) -> int:
        return sum(
            1 for trade in result.trades if trade.reason in {TradeReason.ENTER, TradeReason.EXIT}
        )

    buffered_turnover = compute_metrics(buffered).annual_turnover
    strict_turnover = compute_metrics(strict).annual_turnover
    assert buffered_turnover is not None
    assert strict_turnover is not None
    assert buffered_turnover < strict_turnover
    assert churn(buffered) < churn(strict)
    assert buffered.total_costs < strict.total_costs


def test_a_decision_on_the_final_day_is_never_filled() -> None:
    """There is no ``d+1`` inside the window, and filling at ``d``'s close is the shortcut
    docs/10 §4 exists to forbid."""
    market, data, schedule = _market_and_data()
    last_day = market.calendar[-1]
    result = run_backtest(_config(), data)
    assert last_day not in [trade.date for trade in result.trades]
    assert schedule[-1] <= last_day


# ---------------------------------------------------------------------------
# Weighting and position limits
# ---------------------------------------------------------------------------


def test_position_limits_clip_and_renormalise() -> None:
    """docs/10 §3: "Compute target weights from `weighting`, clipped by `position_limits`,
    renormalised"."""
    raw = {1: Decimal(90), 2: Decimal(5), 3: Decimal(3), 4: Decimal(2)}
    limits = PositionLimits(max_weight=Decimal("0.40"), min_weight=Decimal("0.05"))
    weights = apply_position_limits(raw, limits)

    assert sum(weights.values()) == Decimal(1)
    assert max(weights.values()) <= Decimal("0.40")
    assert min(weights.values()) >= Decimal("0.05")


def test_position_limits_relax_an_impossible_floor() -> None:
    """Twenty names cannot each hold 10%. The floor gives way to ``1/n`` rather than the run."""
    raw = {key: Decimal(1) for key in range(20)}
    weights = apply_position_limits(
        raw, PositionLimits(max_weight=Decimal("1"), min_weight=Decimal("0.10"))
    )
    assert sum(weights.values()) == Decimal(1)
    assert all(value == Decimal("0.05") for value in weights.values())


@pytest.mark.parametrize(
    "weighting",
    [Weighting.EQUAL, Weighting.RANK, Weighting.MARKETCAP, Weighting.INVERSE_VOLATILITY],
)
def test_every_weighting_scheme_runs_and_sums_to_one(weighting: Weighting) -> None:
    _, data, _ = _market_and_data()
    result = run_backtest(_config(weighting=weighting), data)
    by_rebalance: dict[dt.date, Decimal] = {}
    for holding in result.holdings:
        by_rebalance[holding.rebalance_date] = (
            by_rebalance.get(holding.rebalance_date, Decimal(0)) + holding.target_weight
        )
    assert by_rebalance
    assert all(abs(total - Decimal(1)) < Decimal("0.000001") for total in by_rebalance.values())


def test_rank_weighting_favours_the_best_ranked_name() -> None:
    _, data, _ = _market_and_data()
    result = run_backtest(_config(weighting=Weighting.RANK), data)
    first = min(result.holdings, key=lambda holding: (holding.executed_on, holding.rank or 999))
    peers = [
        holding
        for holding in result.holdings
        if holding.executed_on == first.executed_on and holding.instrument_id != first.instrument_id
    ]
    assert all(first.target_weight >= peer.target_weight for peer in peers)


# ---------------------------------------------------------------------------
# The risk overlay and the cash policy (docs/10 §Config)
# ---------------------------------------------------------------------------


def _falling_market() -> tuple[fixtures.SyntheticMarket, BacktestData, tuple[dt.date, ...]]:
    """A market that trends down, so ``index_above_200dma`` has something to trigger on."""
    start, end = dt.date(2011, 1, 3), dt.date(2013, 12, 31)
    market = fixtures.build_market(start=start, end=end, instruments=40, drift=-0.0009)
    schedule = rebalance_dates(market.calendar, start, end, MONTHLY)
    return market, market.data(schedule), schedule


def test_the_risk_overlay_goes_to_cash_below_the_200_day_average() -> None:
    """docs/10 §Config: ``"risk_overlay": { "enabled": false, "rule": "index_above_200dma" }``."""
    _, data, _ = _falling_market()
    off = run_backtest(_config(costs=NO_COSTS), data)
    on = run_backtest(_config(costs=NO_COSTS, risk_overlay=RiskOverlay(enabled=True)), data)

    def cash_days(result: BacktestResult) -> int:
        # Day one is always fully in cash — the first order is not filled until day two — so the
        # comparison starts after the first fill, where the difference is the overlay's doing.
        return sum(1 for value in result.invested[2:] if value == Decimal(0))

    assert any("risk overlay was triggered" in note for note in on.notes)
    # The overlay's whole purpose: after it fires the book is entirely in cash, for a while.
    assert cash_days(on) > 100
    assert cash_days(off) == 0
    # Sitting out a falling market beats riding it down.
    assert on.final_equity > off.final_equity


def test_the_overlay_stays_invested_until_it_has_200_observations() -> None:
    """A 200-day average needs 200 days. Computing one from 40 and acting on it would be worse
    than not having an overlay at all, so the engine says so in the notes and stays invested."""
    start, end = dt.date(2011, 1, 3), dt.date(2011, 6, 30)
    market = fixtures.build_market(start=start, end=end, instruments=40)
    schedule = rebalance_dates(market.calendar, start, end, MONTHLY)
    result = run_backtest(
        _config(
            start=start,
            end=end,
            costs=NO_COSTS,
            risk_overlay=RiskOverlay(enabled=True),
        ),
        market.data(schedule),
    )
    assert any("fewer than the 200" in note for note in result.notes)
    assert max(result.invested) > Decimal(0)


def test_cash_policy_benchmark_makes_idle_cash_track_the_index() -> None:
    """docs/10 §Config: ``"cash_policy": "hold_cash" | "benchmark"``.

    Measured on the overlay's own market, because that is the only configuration in which a
    meaningful share of the book *is* idle: once the overlay fires the whole portfolio is cash,
    and the two policies then say different things about what happens to it. In a falling market
    tracking the index is worse than holding cash, which is exactly the direction asserted.
    """
    _, data, _ = _falling_market()
    overlay = RiskOverlay(enabled=True)
    held = run_backtest(_config(costs=NO_COSTS, risk_overlay=overlay), data)
    tracked = run_backtest(
        _config(costs=NO_COSTS, risk_overlay=overlay, cash_policy=CashPolicy.BENCHMARK), data
    )
    assert tracked.final_equity != held.final_equity
    assert tracked.final_equity < held.final_equity


# ---------------------------------------------------------------------------
# The rebalance calendar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frequency", "at_least", "at_most"),
    [
        (RebalanceFrequency.WEEKLY, 150, 160),
        (RebalanceFrequency.FORTNIGHTLY, 75, 85),
        (RebalanceFrequency.MONTHLY, 36, 37),
        (RebalanceFrequency.QUARTERLY, 12, 13),
    ],
)
def test_rebalance_frequencies(frequency: RebalanceFrequency, at_least: int, at_most: int) -> None:
    """docs/10 §Config: ``weekly|fortnightly|monthly|quarterly`` over three calendar years."""
    calendar = fixtures.weekday_calendar(dt.date(2011, 1, 3), dt.date(2013, 12, 31))
    dates = rebalance_dates(
        calendar, dt.date(2011, 1, 3), dt.date(2013, 12, 31), RebalanceSpec(frequency=frequency)
    )
    assert at_least <= len(dates) <= at_most
    assert dates[0] == calendar[0], "the first trading day is always a rebalance"
    assert list(dates) == sorted(set(dates))


def test_first_and_last_trading_day_of_period_differ() -> None:
    calendar = fixtures.weekday_calendar(dt.date(2011, 1, 3), dt.date(2011, 6, 30))
    last = rebalance_dates(
        calendar, calendar[0], calendar[-1], RebalanceSpec(day=RebalanceDay.LAST_TRADING_DAY)
    )
    first = rebalance_dates(
        calendar, calendar[0], calendar[-1], RebalanceSpec(day=RebalanceDay.FIRST_TRADING_DAY)
    )
    assert last != first
    assert dt.date(2011, 1, 31) in last
    assert dt.date(2011, 2, 1) in first


def test_shift_schedule_moves_every_date_by_one_trading_day() -> None:
    calendar = fixtures.weekday_calendar(dt.date(2011, 1, 3), dt.date(2011, 4, 29))
    dates = rebalance_dates(calendar, calendar[0], dt.date(2011, 3, 31), MONTHLY)
    forward = shift_schedule(calendar, dates, 1)
    assert len(forward) == len(dates)
    for before, after in zip(dates, forward, strict=True):
        assert calendar.index(after) == calendar.index(before) + 1


def test_shift_schedule_drops_a_date_that_falls_off_the_calendar() -> None:
    """Dropped rather than clamped: clamping would land two rebalances on one day, which changes
    the schedule's shape instead of shifting it."""
    calendar = fixtures.weekday_calendar(dt.date(2011, 1, 3), dt.date(2011, 3, 31))
    dates = rebalance_dates(calendar, calendar[0], calendar[-1], MONTHLY)
    assert dates[-1] == calendar[-1]
    forward = shift_schedule(calendar, dates, 1)
    assert len(forward) == len(dates) - 1
    assert calendar[-1] not in forward


# ---------------------------------------------------------------------------
# Outputs (docs/10 §Outputs) and the fragility readout (§honesty features)
# ---------------------------------------------------------------------------


def test_every_metric_docs_10_names_is_produced() -> None:
    _, data, _ = _market_and_data()
    metrics = compute_metrics(run_backtest(_config(), data))
    payload = metrics.as_dict()
    for key in (
        "cagr",
        "total_return",
        "annualised_volatility",
        "sharpe",
        "sortino",
        "max_drawdown",
        "max_drawdown_peak",
        "max_drawdown_trough",
        "calmar",
        "hit_rate",
        "average_win",
        "average_loss",
        "annual_turnover",
        "total_costs",
        "exposure",
        "best_month",
        "worst_month",
        "rolling_12m",
        "alpha",
        "beta",
        "tracking_error",
        "information_ratio",
    ):
        assert key in payload, f"docs/10 §Outputs names {key} and the payload has no such key"
        assert payload[key] is not None, f"{key} came back null on a three-year run"


def test_artefacts_line_up_with_the_run() -> None:
    """docs/10 §Artefacts: equity curve, drawdown series, per-rebalance holdings, trade log,
    monthly heatmap."""
    _, data, _ = _market_and_data()
    result = run_backtest(_config(), data)

    assert len(result.dates) == len(result.equity) == len(result.cash) == len(result.invested)
    drawdowns = drawdown_series(result.dates, result.equity)
    assert len(drawdowns) == len(result.dates)
    assert max(point.drawdown for point in drawdowns) <= 0

    months = monthly_returns(result.dates, result.equity)
    assert len(months) == 36
    assert {month.year for month in months} == {2011, 2012, 2013}

    assert result.holdings
    assert {holding.rebalance_date for holding in result.holdings} <= set(result.rebalance_dates)
    assert all(trade.reason in set(TradeReason) for trade in result.trades)


def test_downsampled_curve_keeps_the_ends() -> None:
    _, data, _ = _market_and_data()
    result = run_backtest(_config(), data)
    curve = downsample_curve(result.dates, result.equity, result.benchmark, limit=100)
    assert len(curve) <= 101
    assert curve[0]["date"] == result.dates[0].isoformat()
    assert curve[-1]["date"] == result.dates[-1].isoformat()
    assert curve[-1]["equity"] == str(result.equity[-1])


def test_round_trips_reconstruct_closed_positions() -> None:
    _, data, _ = _market_and_data()
    result = run_backtest(_config(), data)
    trips = round_trips(result.trades)
    assert trips
    for trip in trips:
        assert trip.closed_on >= trip.opened_on
        assert trip.invested > 0


def test_fragility_reruns_the_configuration_four_ways() -> None:
    """docs/10 §"honesty features": "+/-1 rebalance-day offset and +/-25% costs"."""
    _, data, _ = _market_and_data(offsets=True)
    config = _config()
    report = run_fragility(config, data)

    labels = [run.label for run in report.variants]
    assert labels == [
        "costs_minus_25pct",
        "costs_plus_25pct",
        "rebalance_minus_1d",
        "rebalance_plus_1d",
    ]
    cheaper = next(run for run in report.variants if run.label == "costs_minus_25pct")
    dearer = next(run for run in report.variants if run.label == "costs_plus_25pct")
    assert cheaper.result.final_equity >= report.base.result.final_equity
    assert dearer.result.final_equity <= report.base.result.final_equity

    outcomes = {run.result.final_equity for run in (report.base, *report.variants)}
    assert len(outcomes) > 1, "four perturbations produced one identical answer"


def test_a_missing_screen_for_a_shifted_date_is_refused() -> None:
    """The offset probe must not quietly reuse the neighbouring date's screen."""
    _, data, _ = _market_and_data(offsets=False)
    with pytest.raises(BacktestDataError, match="no screen result"):
        run_fragility(_config(), data)


# ---------------------------------------------------------------------------
# Configuration guards
# ---------------------------------------------------------------------------


def test_unknown_configuration_keys_are_rejected() -> None:
    """The same ``extra="forbid"`` contract ``ScreenDefinition`` has (docs/07 §Screens)."""
    with pytest.raises(ValueError, match="leverage"):
        BacktestConfig.model_validate({"start": "2015-01-01", "end": "2016-01-01", "leverage": 2})


def test_end_must_follow_start() -> None:
    with pytest.raises(ValueError, match="must be after"):
        BacktestConfig.model_validate({"start": "2016-01-01", "end": "2015-01-01"})


def test_min_weight_above_max_weight_is_rejected() -> None:
    with pytest.raises(ValueError, match="above max_weight"):
        PositionLimits(max_weight=Decimal("0.05"), min_weight=Decimal("0.10"))


@pytest.mark.parametrize("policy", [DividendPolicy.CASH, DividendPolicy.REINVEST])
def test_a_dividend_policy_we_cannot_serve_is_refused(policy: DividendPolicy) -> None:
    """M39 turned this test around, because the premise under it was measured and refuted.

    It used to assert that ``cash`` and ``ignore`` were the impossible pair, on the grounds that
    the adjusted close "already contains the dividend" and only ``reinvest`` could be served by
    marking to it. M27 asked the reference corpus and got the opposite answer — the price
    convention won 42 of 45 deciding windows — and M28 applied the share-count actions and not
    the dividend ones (`reconciliation/RECOVERED-ACTIONS.md`).

    So the stored close is a price-return series. ``ignore`` is the one that needs nothing, and
    the two that need a dividend schedule are the two that have to *add* a dividend back.
    """
    _, data, _ = _market_and_data()
    with pytest.raises(BacktestConfigError, match="dividend schedule"):
        run_backtest(_config(dividends=policy), data)


def test_the_price_return_policy_needs_no_extra_data() -> None:
    """``ignore`` is what the engine has always computed. M39 stopped calling it something else."""
    _, data, _ = _market_and_data()
    result = run_backtest(_config(dividends=DividendPolicy.IGNORE), data)

    assert result.dividends_credited == Decimal(0)
    assert result.final_equity > Decimal(0)


def test_the_default_policy_is_the_one_the_data_supports() -> None:
    """A default that needs data nobody supplies is a default that fails every run."""
    assert (
        BacktestConfig(start=dt.date(2011, 1, 3), end=dt.date(2013, 12, 31)).dividends
        is DividendPolicy.IGNORE
    )


def test_a_calendar_with_no_days_in_range_is_refused() -> None:
    _, data, _ = _market_and_data()
    with pytest.raises(BacktestDataError, match="no day between"):
        run_backtest(_config(start=dt.date(2020, 1, 1), end=dt.date(2020, 12, 31)), data)


# ---------------------------------------------------------------------------
# docs/10 §Performance / docs/11: "Backtest (15y, monthly, 20 names) | < 10 s"
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
def test_fifteen_year_monthly_backtest_over_twenty_positions_is_under_ten_seconds() -> None:
    """docs/11 §"Performance budgets": "Backtest (15y, monthly, 20 names) — < 10 s".

    Timed over the engine and its metrics, which is the work docs/10 §Performance budgets: "run
    the portfolio simulation as an array walk over ~2,800 trading days". Loading the panel out of
    PostgreSQL is a separate cost, measured in ``services/worker``.
    """
    start, end = dt.date(2011, 1, 3), dt.date(2026, 1, 2)
    market = fixtures.build_market(start=start, end=end, instruments=300)
    schedule = rebalance_dates(market.calendar, start, end, MONTHLY)
    data = market.data(schedule)
    config = BacktestConfig(
        start=start,
        end=end,
        initial_capital=Decimal(10_000_000),
        rebalance=MONTHLY,
        selection=SelectionSpec(top_n=20, hold_buffer=10),
    )

    began = time.perf_counter()
    result = run_backtest(config, data)
    metrics = compute_metrics(result)
    elapsed = time.perf_counter() - began

    assert len(result.dates) > 3_800
    assert len(result.rebalance_dates) >= 180
    assert metrics.cagr is not None
    assert elapsed < 10.0, f"the 15-year run took {elapsed:.2f}s; docs/11 budgets 10s"
    # Prompt 16 acceptance criterion 1: the number, for benchmarks/AS-MEASURED.md.
    record(
        "backtest_15y",
        elapsed,
        unit="s",
        method="run_backtest + compute_metrics over 15 years, monthly, top 20 of 300 names",
        dataset=(
            "the synthetic market in packages/core/tests/backtest_fixtures.py — NOT the seeded "
            "dataset, which holds no price history (docs/DECISIONS.md §15)"
        ),
    )


# ---------------------------------------------------------------------------
# M39: a blindfolded rebalance is not a decision to hold cash
# ---------------------------------------------------------------------------


class TestBlindRebalances:
    """The engine cannot tell "the screen excluded everything" from "there were no factor rows".

    Both arrive as an empty frame and both send the book to cash, which means a run whose data is
    missing produces a plausible equity curve rather than an error. The engine is not the layer
    that can distinguish them — it never sees the database — so it counts them instead, and the
    loader that *does* know refuses (`baskfy_worker.backtest._require_factor_coverage`).

    Found by running a real 2022-2026 monthly test against nine years of Kite history: the screen
    had factor rows on 15 of 57 rebalance dates and the run reported +13.8% without a murmur.
    """

    def test_an_empty_screen_is_counted_rather_than_passed_over(self) -> None:
        _, data, schedule = _market_and_data()
        blinded = set(schedule[::2])
        emptied = {
            day: (frame.clear() if day in blinded else frame) for day, frame in data.screens.items()
        }
        result = run_backtest(_config(), replace(data, screens=emptied))

        # The last rebalance date is decided but never filled — there is no next trading day
        # inside the window — so the engine never reaches its screen. Everything else is counted.
        assert set(result.blind_rebalances) == blinded - {schedule[-1]}
        assert result.blind_fraction > Decimal("0.4")

    def test_a_run_whose_screens_all_work_reports_none(self) -> None:
        _, data, _ = _market_and_data()
        result = run_backtest(_config(), data)

        assert result.blind_rebalances == ()
        assert result.blind_fraction == Decimal(0)

    def test_the_overlay_going_to_cash_is_not_counted_as_blind(self) -> None:
        """A deliberate move to cash is a decision. Only an empty *screen* is blindness.

        Without this the count would fire on every risk-off month and mean nothing.
        """
        _, data, _ = _market_and_data()
        result = run_backtest(_config(risk_overlay=RiskOverlay(enabled=True)), data)

        assert result.blind_rebalances == ()


class TestTheBookAddsUp:
    """`equity == cash + invested`, on every single day of the run.

    Not covered before M39, and it is the one identity that makes every other number on the page
    trustworthy: a curve that does not reconcile with its own two components is a curve nobody can
    act on. Asserted to the paisa rather than approximately -- these are `Decimal` throughout, and
    a rounding drift is exactly the sort of bug this would otherwise hide.
    """

    def test_equity_reconciles_every_day(self) -> None:
        _, data, _ = _market_and_data()
        result = run_backtest(_config(), data)

        assert len(result.equity) == len(result.cash) == len(result.invested) == len(result.dates)
        for day, equity, cash, invested in zip(
            result.dates, result.equity, result.cash, result.invested, strict=True
        ):
            assert equity == cash + invested, f"{day} does not reconcile"

    def test_it_reconciles_with_a_delisting_in_the_window(self) -> None:
        """The path most likely to lose a rupee: a forced sale outside the rebalance schedule."""
        _, data, _ = _market_and_data(delist=(7, dt.date(2012, 6, 15)))
        # Hold the whole universe, so the delisted name is certainly on the book when it stops.
        result = run_backtest(_config(selection=SelectionSpec(top_n=40, hold_buffer=0)), data)

        assert result.delistings
        for equity, cash, invested in zip(result.equity, result.cash, result.invested, strict=True):
            assert equity == cash + invested


# ---------------------------------------------------------------------------
# M43: metrics that were confidently wrong
# ---------------------------------------------------------------------------


def _result_with_benchmark(benchmark: tuple[Decimal | None, ...]) -> BacktestResult:
    """A three-day run with a benchmark supplied by the caller. Nothing else varies."""
    _, data, _ = _market_and_data()
    result = run_backtest(_config(), data)
    days = len(result.dates)
    padded = benchmark + (benchmark[-1],) * (days - len(benchmark))
    return replace(result, benchmark=padded[:days])


class TestAMissingBenchmarkIsNotAResult:
    """With no benchmark, the benchmark metrics must be absent — not the portfolio's own numbers.

    `_benchmark_returns` used to fill every gap with a flat day and return an array of zeros when
    there was no benchmark at all. `active = portfolio - 0` is `portfolio`, so the page published
    the portfolio's **own volatility** labelled "tracking error" and its **own Sharpe** labelled
    "information ratio" — against a benchmark that did not exist. It is reachable in production:
    `_benchmark_frame` returns nothing whenever the slug is unknown or the index has no rows in
    the window.
    """

    def test_tracking_error_is_not_the_portfolios_own_volatility(self) -> None:
        metrics = compute_metrics(_result_with_benchmark((None, None, None)))

        assert metrics.tracking_error is None
        assert metrics.information_ratio is None
        assert metrics.annualised_volatility is not None, "the portfolio's own vol still computes"

    def test_a_benchmark_that_exists_still_produces_the_metrics(self) -> None:
        """The guard must not swallow the working case."""
        _, data, _ = _market_and_data()
        result = run_backtest(_config(), data)
        metrics = compute_metrics(result)

        assert metrics.tracking_error is not None
        assert metrics.beta is not None


class TestTheBenchmarkIsAnnualisedOverItsOwnSpan:
    """`benchmark_cagr` divided a partial span's return by the *portfolio's* elapsed years.

    The error is one-directional — it always understates the benchmark, so it always flatters the
    strategy, which is the direction a backtest must never be wrong in. Measured before the fix on
    a 4.742-year run: a quarter of the benchmark missing reported 1.3380%/yr against 1.7879% true.
    """

    def test_a_late_starting_benchmark_is_not_stretched(self) -> None:
        _, data, _ = _market_and_data()
        result = run_backtest(_config(), data)
        days = len(result.dates)
        # The benchmark only exists for the final quarter of the run, doubling over that span.
        start = (days * 3) // 4
        levels: list[Decimal | None] = [None] * start
        levels += [Decimal(100) + Decimal(index) for index in range(days - start)]

        metrics = compute_metrics(replace(result, benchmark=tuple(levels)))
        assert metrics.benchmark_cagr is not None

        covered_years = float(year_fraction(result.dates[start], result.dates[-1]))
        observed = [value for value in levels if value is not None]
        expected = float(observed[-1] / observed[0]) ** (1.0 / covered_years) - 1.0
        assert metrics.benchmark_cagr == pytest.approx(expected, rel=1e-9)


class TestRatiosRefuseNoiseAsADenominator:
    """A volatility of 6e-08 is rounding noise, and dividing by it published a Sharpe of -4.29e6."""

    def test_a_constant_drift_with_rounding_noise_has_no_sharpe(self) -> None:
        """The measured case: a fixed daily decay, stored at 2 dp.

        Every return is -0.1% to within storage precision, so the *spread* is rounding noise —
        about 6e-08 annualised — while the mean is large. Dividing one by the other published
        `sharpe = -4292786.11`, a number with no meaning that the page renders without comment.
        """
        _, data, _ = _market_and_data()
        result = run_backtest(_config(), data)
        decayed: list[Decimal] = [Decimal("1000000.00")]
        for _ in range(len(result.dates) - 1):
            decayed.append((decayed[-1] * Decimal("0.999")).quantize(Decimal("0.01")))
        series = tuple(decayed)

        metrics = compute_metrics(replace(result, equity=series, benchmark=(None,) * len(series)))

        assert metrics.annualised_volatility is not None
        assert metrics.annualised_volatility < 1e-4, (
            "the fixture must be near-constant to be a test"
        )
        assert metrics.sharpe is None, f"published sharpe={metrics.sharpe} on rounding noise"
        # Sortino is NOT None here and should not be: it divides by the magnitude of the downside
        # (0.0158 annualised), not by its dispersion, and a book losing 0.1% every day really does
        # have a Sortino near -16. Only the ratio whose denominator collapsed is refused.
        assert metrics.sortino is not None
        assert metrics.sortino < 0


class TestATotalLossHasACompoundRate:
    """A portfolio that went to zero compounded at -100% a year. It is not undefined."""

    def test_zero_final_equity_reports_minus_one(self) -> None:
        _, data, _ = _market_and_data()
        result = run_backtest(_config(), data)
        wiped = (*result.equity[:-1], Decimal(0))
        metrics = compute_metrics(replace(result, equity=wiped))

        assert metrics.total_return == pytest.approx(-1.0)
        assert metrics.cagr == pytest.approx(-1.0), "cagr was None on the least ambiguous run"
        assert metrics.calmar is not None


class TestAThinSessionIsNotAHold:
    """A fill day the market barely opened on must not read like a deliberate decision (M45).

    NSE holds sessions — Diwali Muhurat, Budget-day Saturdays, 2024's special Saturdays — that
    print between 1% and 18% of a normal day's instruments. Seven of them are in this dataset.
    `_execute` used to skip a name with no opening print with a bare `continue`, which is the
    right *action* (a name that did not trade cannot be traded) and was silent.

    Measured on live data before the fix: a full 30-name decision on 2026-01-30, filling on
    2026-02-01, produced **zero** trades — and `blind_rebalances` was empty, because that counts
    an empty *screen* and this screen was full. Every honesty counter on the result read clean
    while none of the strategy had happened.
    """

    def _panel_without_opens_on(self, data: BacktestData, day: dt.date) -> BacktestData:
        """The same panel with every opening price on `day` removed."""
        position = data.prices.calendar.index(day)
        panel = data.prices
        # Reaching into the panel deliberately: this constructs a fixture market, it does not
        # read production state. There is no public way to say "this day did not trade".
        opens = panel._open.copy()
        opens[position, :] = np.nan
        clone = object.__new__(PricePanel)
        clone.__dict__.update(panel.__dict__)
        object.__setattr__(clone, "_open", opens)
        return replace(data, prices=clone)

    def test_a_day_that_fills_nothing_is_recorded(self) -> None:
        _, data, schedule = _market_and_data()
        calendar = list(data.calendar)
        fill_day = calendar[calendar.index(schedule[2]) + 1]

        result = run_backtest(_config(), self._panel_without_opens_on(data, fill_day))

        assert fill_day in result.unexecuted_fills, (
            "a rebalance that filled nothing at all was not recorded anywhere"
        )
        assert any("could not be filled" in note for note in result.notes)
        assert any("NOTHING was filled" in note for note in result.notes)

    def test_an_ordinary_run_records_none(self) -> None:
        _, data, _ = _market_and_data()
        result = run_backtest(_config(), data)

        assert result.unexecuted_fills == ()
        assert not any("could not be filled" in note for note in result.notes)
