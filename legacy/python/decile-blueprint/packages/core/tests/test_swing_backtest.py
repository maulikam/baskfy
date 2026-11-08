"""docs/swing/04 §11 — the EOD backtest reproduces a planted trade, and says what it skipped.

Every expectation here is worked from the document's own arithmetic (§5 sizing, §6 management,
§10 R-multiples, §11 entry and costs) on prices that are constants of
``swing_backtest_fixtures`` — never from a number the engine printed once.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import FrozenInstanceError, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from time import perf_counter

import polars as pl
import pytest
from swing_backtest_fixtures import (
    BELOW_TRIGGER_DAY,
    CRASH_TAIL,
    DETECTION_BAR,
    ENTRY_BAR,
    HOLD_TAIL,
    LOSE_TAIL,
    LOW_FACTOR,
    PLANTED,
    RECOVER_TAIL,
    THROUGH_TRIGGER_DAY,
    WIN_TAIL,
    Bar,
    entry_variant,
    ep_rows,
    flag_detected_on,
    index_series,
    planted_frame,
    second_flag,
    speed_frame,
    tape_series,
)

import baskfy_core.swing.backtest as backtest_module
from baskfy_core.backtest import MISSING_BAR_TOLERANCE_DAYS
from baskfy_core.swing import (
    CAVEATS,
    BacktestParams,
    BacktestResult,
    BacktestTrade,
    run_backtest,
)
from baskfy_core.swing.backtest import (
    INDEX_ABSENT_CAVEAT,
    BacktestCloseReason,
    GateCell,
    GateMode,
)
from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    MarketConfig,
    Setup,
    SizingConfig,
    SwingConfig,
)
from baskfy_core.swing.indicators import with_swing_indicators
from baskfy_core.swing.journal import summarize
from baskfy_core.swing.market import (
    BreadthSnapshot,
    IndexReading,
    MarketGate,
    drawdown_pct,
    market_gate,
)
from baskfy_core.swing.setups import detect_flags
from baskfy_core.swing.stops import (
    ActionKind,
    ActionReason,
    DailyBar,
    OpenPosition,
    TrailMa,
    manage,
    widest_stop_pct,
)

D = Decimal
FOUR_DP = D("0.0001")
TWO_DP = D("0.01")
COST = D("0.0013")


def params_for(  # noqa: PLR0913 - one keyword per parameter of a run
    calendar: list[dt.date],
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    sleeve_inr: Decimal | None = None,
    cost_pct_per_side: Decimal | None = None,
    config: SwingConfig | None = None,
) -> BacktestParams:
    params = BacktestParams(start=start or calendar[0], end=end or calendar[-1])
    if sleeve_inr is not None:
        params = replace(params, sleeve_inr=sleeve_inr)
    if cost_pct_per_side is not None:
        params = replace(params, cost_pct_per_side=cost_pct_per_side)
    if config is not None:
        params = replace(params, config=config)
    return params


def run(  # noqa: PLR0913 - one keyword per parameter of a run
    frame: pl.DataFrame,
    calendar: list[dt.date],
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    sleeve_inr: Decimal | None = None,
    cost_pct_per_side: Decimal | None = None,
    config: SwingConfig | None = None,
    index: pl.DataFrame | None = None,
    delisted: dict[int, dt.date] | None = None,
) -> BacktestResult:
    params = params_for(
        calendar,
        start=start,
        end=end,
        sleeve_inr=sleeve_inr,
        cost_pct_per_side=cost_pct_per_side,
        config=config,
    )
    return run_backtest(frame, params, calendar=calendar, index=index, delisted=delisted)


def only_trade(result: BacktestResult) -> BacktestTrade:
    assert len(result.trades) == 1, result.trades
    return result.trades[0]


def planted_trigger(frame: pl.DataFrame, calendar: list[dt.date]) -> Decimal:
    """The pivot the detector itself reports on the detection day — `04` §2.6's trigger."""
    found = detect_flags(with_swing_indicators(frame), calendar[DETECTION_BAR])
    row = found.filter(pl.col("symbol") == PLANTED.symbol).row(0, named=True)
    assert row["status"] == "SETTING_UP"
    return D(repr(row["trigger"])).quantize(FOUR_DP, rounding=ROUND_HALF_UP)


def paid(price: Decimal, cost: Decimal = COST) -> Decimal:
    return (price * (1 + cost)).quantize(FOUR_DP, rounding=ROUND_HALF_UP)


def received(price: Decimal, cost: Decimal = COST) -> Decimal:
    return (price * (1 - cost)).quantize(FOUR_DP, rounding=ROUND_HALF_UP)


# --- the planted flag (G2) ------------------------------------------------------


def test_planted_flag_reproduces_its_r_to_2dp() -> None:
    """The one trade the planted year contains comes out exactly as worked by hand."""
    frame, calendar = planted_frame()
    trade = only_trade(run(frame, calendar))
    assert trade.symbol == PLANTED.symbol
    assert trade.setup == Setup.FLAG.value
    assert trade.r_multiple == PLANTED.r_multiple
    assert trade.quantity == PLANTED.quantity
    assert trade.entry == PLANTED.entry_paid
    assert trade.initial_stop == PLANTED.stop
    assert trade.exit_avg == PLANTED.exit_avg
    assert trade.entry_date == calendar[ENTRY_BAR]
    assert trade.exit_date == calendar[ENTRY_BAR + PLANTED.exit_day]
    assert trade.close_reason == ActionReason.HARD_STOP_HIT.value


def test_planted_stop_sits_inside_one_adr_of_a_leaders_range() -> None:
    """`04` §6 (SW9.5): the widest stop is one ADR. The re-planted fixture is a name with a
    leader's range — nineteen bars 9.68% wide and a tight 4.08% detection bar — whose 20-bar
    ADR is 9.40%; the planted stop is 6.67% under the entry, inside it, so the trade is lined
    rather than refused ``STOP_TOO_WIDE``. On the ±2% bars ``swing_fixtures`` draws (ADR 4.08%)
    the same stop would be refused, which is why the fixture had to change and the rule did not.
    """
    frame, calendar = planted_frame()
    detection = with_swing_indicators(frame).filter(
        (pl.col("symbol") == PLANTED.symbol) & (pl.col("date") == calendar[DETECTION_BAR])
    )
    adr = D(repr(detection["adr_pct"].item())).quantize(TWO_DP)
    wide = D(repr(1.02 / LOW_FACTOR - 1)) * 100
    tight = D(repr(1.02 / 0.98 - 1)) * 100
    assert adr == ((19 * wide + tight) / 20).quantize(TWO_DP) == PLANTED.adr_pct
    distance = ((PLANTED.entry_open - PLANTED.stop) / PLANTED.entry_open * 100).quantize(TWO_DP)
    assert distance == PLANTED.stop_distance_pct
    assert distance <= widest_stop_pct(adr, DEFAULT_SWING_CONFIG.stops) == adr
    assert D(repr(1.02 / 0.98 - 1)) * 100 < distance, "the ±2% fixture would have refused it"
    assert only_trade(run(frame, calendar)).r_multiple == PLANTED.r_multiple


def test_planted_r_agrees_with_the_documents_formulas() -> None:
    """§5, §6.3, §10 and §11 recomputed from the fixture's constants, not from the engine."""
    risk = D(1_000_000) * D("0.5") / 100
    quantity = int(risk / (PLANTED.entry_open - PLANTED.stop))
    partial = quantity * 1 // 3
    entry = paid(PLANTED.entry_open)
    exit_avg = (
        (
            partial * received(PLANTED.partial_fill)
            + (quantity - partial) * received(PLANTED.final_fill)
        )
        / quantity
    ).quantize(TWO_DP)
    expected_r = ((exit_avg - entry) / (entry - PLANTED.stop)).quantize(TWO_DP)
    assert (quantity, partial, entry, exit_avg, expected_r) == (
        PLANTED.quantity,
        PLANTED.partial_quantity,
        PLANTED.entry_paid,
        PLANTED.exit_avg,
        PLANTED.r_multiple,
    )
    frame, calendar = planted_frame()
    trade = only_trade(run(frame, calendar))
    assert trade.r_multiple == expected_r
    assert trade.pnl_inr == ((exit_avg - entry) * quantity).quantize(TWO_DP)


def test_planted_partial_fills_at_the_next_open_and_the_rest_at_the_breakeven_stop() -> None:
    """§6.4.4 then §6.4.1: a third at the day-4 open, the remainder at the stop raised to the
    entry — visible as the share-weighted exit average, before costs."""
    frame, calendar = planted_frame()
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
    q, part = PLANTED.quantity, PLANTED.partial_quantity
    expected = ((part * PLANTED.partial_fill + (q - part) * PLANTED.final_fill) / q).quantize(
        TWO_DP
    )
    assert trade.exit_avg == expected
    assert trade.exit_date == calendar[ENTRY_BAR + PLANTED.exit_day]


def test_entry_rule_fills_at_the_open_when_it_is_at_or_above_the_trigger() -> None:
    frame, calendar = planted_frame()
    trigger = planted_trigger(frame, calendar)
    assert PLANTED.entry_open > trigger
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
    assert trade.entry == PLANTED.entry_open
    assert trade.entry_date == calendar[DETECTION_BAR + 1]


def test_entry_rule_fills_at_the_trigger_when_only_the_high_reaches_it() -> None:
    frame, calendar = planted_frame(tail=entry_variant(THROUGH_TRIGGER_DAY))
    trigger = planted_trigger(frame, calendar)
    assert D(repr(THROUGH_TRIGGER_DAY.open)) < trigger <= D(repr(THROUGH_TRIGGER_DAY.high))
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
    assert trade.entry == trigger


def test_entry_rule_treats_an_open_exactly_at_the_trigger_as_a_fill_at_the_open() -> None:
    """`>=`, not `>`: an open on the pivot is an entry."""
    frame, calendar = planted_frame()
    trigger = float(planted_trigger(frame, calendar))
    frame, calendar = planted_frame(tail=entry_variant(Bar(trigger, 155.0, 150.0, 154.0)))
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
    assert trade.entry == D(repr(trigger)).quantize(FOUR_DP)


def test_entry_rule_gives_no_trade_when_the_high_stays_below_the_trigger() -> None:
    """The candidate found on the detection day is not entered the next session; a run that
    ends there has no trade at all, and the candidate is counted as a no-trigger skip."""
    frame, calendar = planted_frame(tail=entry_variant(BELOW_TRIGGER_DAY))
    assert D(repr(BELOW_TRIGGER_DAY.high)) < planted_trigger(frame, calendar)
    result = run(frame, calendar, end=calendar[ENTRY_BAR])
    assert result.trades == ()
    assert result.funnel["entered"] == 0
    assert result.funnel["skipped_no_trigger"] >= 1
    assert result.stats == summarize([])
    # The name may set up again on the entry day itself and trade later — but never on the day
    # whose high stayed below the pivot.
    later = run(frame, calendar)
    assert all(t.entry_date != calendar[ENTRY_BAR] for t in later.trades)


def test_prior_low_is_the_initial_stop() -> None:
    """§11: the stop is the prior day's low — the detection day's, not the entry day's."""
    frame, calendar = planted_frame()
    detection_low = frame.filter(
        (pl.col("symbol") == PLANTED.symbol) & (pl.col("date") == calendar[DETECTION_BAR])
    )["low"].item()
    entry_low = WIN_TAIL[0].low
    assert entry_low != detection_low
    trade = only_trade(run(frame, calendar))
    assert trade.initial_stop == D(repr(detection_low)).quantize(FOUR_DP, rounding=ROUND_HALF_UP)
    assert trade.initial_stop == PLANTED.stop


def test_cost_is_charged_on_the_entry_and_on_every_exit_price() -> None:
    frame, calendar = planted_frame()
    free = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
    charged = only_trade(run(frame, calendar))
    assert free.entry == PLANTED.entry_open
    assert charged.entry == paid(PLANTED.entry_open)
    q, part = PLANTED.quantity, PLANTED.partial_quantity
    expected_exit = (
        (part * received(PLANTED.partial_fill) + (q - part) * received(PLANTED.final_fill)) / q
    ).quantize(TWO_DP)
    assert charged.exit_avg == expected_exit
    assert free.exit_avg > charged.exit_avg
    assert free.r_multiple > charged.r_multiple


def test_cost_makes_a_full_stop_out_worse_than_minus_one_r() -> None:
    """A stop-out pays the round trip: R sits below -1 by exactly the costs."""
    frame, calendar = planted_frame(tail=LOSE_TAIL)
    trade = only_trade(run(frame, calendar))
    assert trade.close_reason == ActionReason.HARD_STOP_HIT.value
    fill = D(repr(LOSE_TAIL[8].open))
    entry = paid(PLANTED.entry_open)
    expected = ((received(fill) - entry) / (entry - PLANTED.stop)).quantize(TWO_DP)
    assert trade.r_multiple == expected < D(-1)


def test_cost_is_read_from_params_not_from_a_literal() -> None:
    frame, calendar = planted_frame()
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D("1")))
    assert trade.entry == paid(PLANTED.entry_open, D("0.01"))


# --- sizing, the ladder, the statistics, the curve, the funnel (G3) --------------------


def test_sizing_reads_the_constant_sleeve_and_the_risk_from_config() -> None:
    """§5: doubling risk per trade doubles the shares; the sleeve is the constant `params` names."""
    frame, calendar = planted_frame()
    distance = PLANTED.entry_open - PLANTED.stop
    doubled = SwingConfig(sizing=SizingConfig(risk_per_trade_pct=1.0))
    trade = only_trade(run(frame, calendar, config=doubled))
    assert trade.quantity == int(D(1_000_000) * D("1.0") / 100 / distance)
    assert trade.quantity > PLANTED.quantity
    half_sleeve = only_trade(run(frame, calendar, sleeve_inr=D(500_000)))
    assert half_sleeve.quantity == int(D(500_000) * D("0.5") / 100 / distance)
    assert half_sleeve.quantity < PLANTED.quantity


def test_ladder_steps_down_after_a_losing_run_and_the_book_is_smaller() -> None:
    """§8.4 inside the run: a win in a GREEN tape climbs a rung per session; a loss streak of the
    configured length steps down one rung per session; a rung never skips. The ceilings at the
    lower rung are the smaller book (`tiers`)."""
    calendar = planted_frame()[1]
    frame, calendar = planted_frame(extra=second_flag("FLAGLOSE", LOSE_TAIL, calendar))
    market = MarketConfig(lookback_trades=1, step_down_loss_streak=1)
    result = run(frame, calendar, config=SwingConfig(market=market))
    assert {t.symbol: t.r_multiple > 0 for t in result.trades} == {
        PLANTED.symbol: True,
        "FLAGLOSE": False,
    }
    levels = {day: level for day, gate, level in result.ladder}
    gates = {gate for _, gate, _ in result.ladder[ENTRY_BAR:]}
    assert gates == {MarketGate.GREEN.value}
    e = ENTRY_BAR
    assert [levels[calendar[e + k]] for k in range(4, 12)] == [0, 1, 2, 3, 2, 1, 0, 0]
    top = len(market.tiers) - 1
    assert market.tiers[0][1] < market.tiers[top][1]


def test_ladder_red_gate_allows_no_entry() -> None:
    """§8.3-8.4: with no name up 25% in a month the gate is RED, the rung is 0 and every
    candidate is refused as a gate skip — the planted flag never trades."""
    frame, calendar = planted_frame()
    frame = frame.filter(pl.col("symbol") != "TAPECO")
    result = run(frame, calendar)
    assert result.trades == ()
    assert result.funnel["candidates"] > 0
    assert result.funnel["skipped_gate"] == result.funnel["candidates"]
    # The flag's own pole is a month of "up 25%"; from the base on, nothing in the universe is.
    in_the_base = result.ladder[DETECTION_BAR - 10 :]
    assert {(gate, level) for _, gate, level in in_the_base} == {(MarketGate.RED.value, 0)}


def test_ladder_is_evaluated_from_the_default_config_when_none_is_given() -> None:
    """With the pack's lookback of five, one closed trade moves nothing."""
    frame, calendar = planted_frame()
    result = run(frame, calendar)
    assert {level for _, _, level in result.ladder} == {0}
    assert result.params.config == DEFAULT_SWING_CONFIG


def test_by_setup_stats_come_from_summarize_over_that_setups_trades() -> None:
    frame, calendar = planted_frame()
    result = run(frame, calendar)
    closed = [t.as_closed_trade() for t in result.trades]
    assert set(result.by_setup) == {Setup.FLAG.value, Setup.EP.value}
    assert result.by_setup[Setup.FLAG.value] == summarize(closed)
    assert result.by_setup[Setup.EP.value] == summarize([])
    assert result.stats == summarize(closed)


def test_by_year_is_keyed_by_the_close_year_and_covers_every_year_of_the_run() -> None:
    frame, calendar = planted_frame()
    result = run(frame, calendar)
    assert calendar[0].year == 2025 and calendar[-1].year == 2026
    assert set(result.by_year) == {2025, 2026}
    assert result.by_year[2025] == summarize([])
    assert result.by_year[2026] == summarize([t.as_closed_trade() for t in result.trades])
    assert result.trades[0].exit_date.year == 2026


def test_equity_curve_has_one_point_per_session_in_date_order() -> None:
    frame, calendar = planted_frame()
    result = run(frame, calendar)
    assert [day for day, _ in result.equity_curve] == calendar
    assert result.funnel["sessions"] == len(calendar)
    assert result.equity_curve[0][1] == D("1000000.00")


def test_equity_curve_marks_open_positions_at_the_close_and_ends_at_realised() -> None:
    """The sleeve, less what was paid, plus the open shares at the close; after the exit, the
    sleeve plus the realised rupees."""
    frame, calendar = planted_frame()
    result = run(frame, calendar)
    curve = dict(result.equity_curve)
    q, part = PLANTED.quantity, PLANTED.partial_quantity
    cash_after_entry = D(1_000_000) - (paid(PLANTED.entry_open) * q).quantize(TWO_DP)
    assert curve[calendar[ENTRY_BAR]] == cash_after_entry + q * D(repr(WIN_TAIL[0].close))
    realised = (
        cash_after_entry
        + (received(PLANTED.partial_fill) * part).quantize(TWO_DP)
        + (received(PLANTED.final_fill) * (q - part)).quantize(TWO_DP)
    )
    assert curve[calendar[-1]] == realised
    assert curve[calendar[-1]] > D(1_000_000)


def test_funnel_counts_are_consistent_on_every_frame() -> None:
    calendar = planted_frame()[1]
    frames = [
        planted_frame(),
        planted_frame(tail=LOSE_TAIL),
        planted_frame(extra=second_flag("FLAGLOSE", LOSE_TAIL, calendar)),
        (planted_frame()[0].filter(pl.col("symbol") != "TAPECO"), calendar),
        speed_frame(instruments=20, sessions=300, seed=3),
    ]
    for frame, days in frames:
        funnel = run(frame, days).funnel
        skipped = sum(v for k, v in funnel.items() if k.startswith("skipped_"))
        assert funnel["entered"] + skipped == funnel["candidates"], funnel
        assert funnel["candidates"] <= funnel["detected"]
        assert funnel["closed"] == funnel["entered"], funnel


def test_funnel_counts_a_locked_name_as_skipped_locked() -> None:
    """§3.5 / §11: a GAP_DAY EP that touched its band is counted, never entered."""
    calendar = planted_frame()[1]
    frame, calendar = planted_frame(extra=ep_rows(calendar, locked=True))
    result = run(frame, calendar)
    assert result.funnel["skipped_locked"] == 1
    assert {t.symbol for t in result.trades} == {PLANTED.symbol}


def test_funnel_counts_a_candidate_on_the_last_session_as_never_enterable() -> None:
    frame, calendar = planted_frame()
    result = run(frame, calendar, end=calendar[DETECTION_BAR])
    assert result.trades == ()
    assert result.funnel["skipped_no_next_session"] >= 1
    assert result.funnel["sessions"] == DETECTION_BAR + 1


def test_funnel_enters_a_symbol_once_when_two_candidates_carry_it() -> None:
    calendar = planted_frame()[1]
    frame, calendar = planted_frame(extra=second_flag(PLANTED.symbol, WIN_TAIL, calendar))
    result = run(frame, calendar)
    assert len(result.trades) == 1
    assert result.funnel["entered"] == 1
    assert result.funnel["skipped_held"] >= 1


# --- the exits the rules do not name --------------------------------------------------


def test_ep_gap_day_is_entered_the_next_session_and_a_red_close_then_is_not_ep_failure() -> None:
    """§11 enters the EP the session after its gap; §6.4.2 belongs to the gap day only."""
    calendar = planted_frame()[1]
    frame, calendar = planted_frame(extra=ep_rows(calendar))
    result = run(frame, calendar)
    ep = next(t for t in result.trades if t.setup == Setup.EP.value)
    assert ep.entry_date == calendar[DETECTION_BAR + 1]
    assert ep.close_reason == BacktestCloseReason.END_OF_RUN.value
    assert ep.exit_date == calendar[-1]


def test_a_gap_through_the_stop_fills_at_the_open_not_at_the_stop() -> None:
    frame, calendar = planted_frame(tail=LOSE_TAIL)
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
    assert D(repr(LOSE_TAIL[8].open)) < PLANTED.stop
    assert trade.exit_avg == D(repr(LOSE_TAIL[8].open)).quantize(TWO_DP)
    assert trade.exit_date == calendar[ENTRY_BAR + 8]


def test_a_stop_hit_on_the_entry_day_fills_at_the_stop_even_below_the_open() -> None:
    """The entry (at the trigger) happened before the low did, so the open is not the fill.

    The run ends on the entry day: on a leader's-range name (SW9.5's re-plant) the stopped-out
    bar still reads as a base at its close, and the next session would open a second trade
    that is not the one this test is about.
    """
    frame, calendar = planted_frame()
    trigger = float(planted_trigger(frame, calendar))
    frame, calendar = planted_frame(tail=entry_variant(Bar(140.0, trigger + 1.0, 139.0, 141.0)))
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0), end=calendar[ENTRY_BAR]))
    assert trade.entry == D(repr(trigger)).quantize(FOUR_DP)
    assert trade.exit_avg == PLANTED.stop.quantize(TWO_DP)
    assert trade.exit_date == trade.entry_date


def test_open_positions_at_the_end_are_closed_at_the_last_close_as_end_of_run() -> None:
    frame, calendar = planted_frame(tail=WIN_TAIL[:3])
    result = run(frame, calendar)
    trade = only_trade(result)
    assert trade.close_reason == BacktestCloseReason.END_OF_RUN.value
    assert trade.exit_date == calendar[-1]
    assert trade.exit_avg == received(D(repr(WIN_TAIL[2].close))).quantize(TWO_DP)
    assert result.funnel["closed_end_of_run"] == 1
    # The curve's last point is cash after the sale at the price received, not the price paid.
    cash_after_entry = D(1_000_000) - (paid(PLANTED.entry_open) * trade.quantity).quantize(TWO_DP)
    proceeds = (received(D(repr(WIN_TAIL[2].close))) * trade.quantity).quantize(TWO_DP)
    assert result.equity_curve[-1][1] == cash_after_entry + proceeds


def test_a_name_that_stops_printing_bars_is_sold_at_its_last_close() -> None:
    """The screener engine's tolerance, reused: after that many missing sessions the name is
    dead, and the sale is at the last close it printed."""
    frame, calendar = planted_frame()
    gone_from = calendar[ENTRY_BAR + 2]
    frame = frame.filter(~((pl.col("symbol") == PLANTED.symbol) & (pl.col("date") >= gone_from)))
    result = run(frame, calendar, cost_pct_per_side=D(0))
    trade = only_trade(result)
    assert trade.close_reason == BacktestCloseReason.NO_BAR.value
    assert trade.exit_avg == D(repr(WIN_TAIL[1].close)).quantize(TWO_DP)
    assert trade.exit_date == calendar[ENTRY_BAR + 1 + MISSING_BAR_TOLERANCE_DAYS]
    assert result.funnel["closed_no_bar"] == 1


def test_a_pending_sell_waits_for_a_bar_and_a_dead_name_is_sold_at_its_last_close() -> None:
    """A partial decided at the day-3 close has no open to fill at when the bars stop; it
    waits, and the whole position goes at the last close once the tolerance runs out."""
    frame, calendar = planted_frame()
    gone_from = calendar[ENTRY_BAR + 4]
    frame = frame.filter(~((pl.col("symbol") == PLANTED.symbol) & (pl.col("date") >= gone_from)))
    result = run(frame, calendar, cost_pct_per_side=D(0))
    trade = only_trade(result)
    assert trade.close_reason == BacktestCloseReason.NO_BAR.value
    assert trade.quantity == PLANTED.quantity
    assert trade.exit_avg == D(repr(WIN_TAIL[3].close)).quantize(TWO_DP)
    assert trade.exit_date == calendar[ENTRY_BAR + 3 + MISSING_BAR_TOLERANCE_DAYS]


def test_a_close_below_the_trail_ma_sells_everything_at_the_next_open() -> None:
    """§6.4.3 with §6.5's "sell lines execute at the next open": the whole position at the
    following session's open, with that reason."""
    tail = (
        Bar(152.0, 155.0, 150.0, 154.0),
        Bar(153.0, 154.0, 142.5, 143.0),  # above the stop, below the 20-day MA (~146)
        Bar(144.0, 146.0, 143.0, 145.0),
        *WIN_TAIL[3:],
    )
    frame, calendar = planted_frame(tail=tail)
    result = run(frame, calendar, cost_pct_per_side=D(0))
    trade = only_trade(result)
    assert trade.close_reason == ActionReason.CLOSE_BELOW_TRAIL_MA.value
    assert trade.exit_date == calendar[ENTRY_BAR + 2]
    assert trade.exit_avg == D(repr(tail[2].open)).quantize(TWO_DP)
    assert trade.quantity == PLANTED.quantity
    assert result.funnel["closed"] == 1


def test_a_gap_through_the_stop_on_the_first_day_after_entry_fills_at_the_open() -> None:
    tail = (Bar(152.0, 155.0, 150.0, 154.0), Bar(138.0, 140.0, 136.0, 139.0), *WIN_TAIL[2:])
    frame, calendar = planted_frame(tail=tail)
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
    assert D(repr(tail[1].open)) < PLANTED.stop
    assert trade.close_reason == ActionReason.HARD_STOP_HIT.value
    assert trade.exit_avg == D(repr(tail[1].open)).quantize(TWO_DP)
    assert trade.exit_date == calendar[ENTRY_BAR + 1]


def test_entry_rule_fills_at_the_trigger_when_the_high_exactly_touches_it() -> None:
    frame, calendar = planted_frame()
    trigger = float(planted_trigger(frame, calendar))
    frame, calendar = planted_frame(tail=entry_variant(Bar(147.0, trigger, 146.0, 147.0)))
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
    assert trade.entry == D(repr(trigger)).quantize(FOUR_DP)


def test_ladder_starts_at_rung_zero() -> None:
    """`02` §3.5 / `04` §8.4: the book starts on the lowest rung, whatever the tape says."""
    frame, calendar = planted_frame()
    result = run(frame, calendar, start=calendar[ENTRY_BAR - 5])
    assert result.ladder[0][1] == MarketGate.GREEN.value
    assert result.ladder[0][2] == 0


def test_the_gate_reads_only_the_sessions_own_close() -> None:
    """House rule 5 inside the breadth: a tape that turns up later is RED until it does."""
    frame, calendar = planted_frame()
    frame = frame.filter(pl.col("symbol") != "TAPECO")
    turning = pl.DataFrame(tape_series(2, "TAPECO", calendar, rise_from=100))
    result = run(pl.concat([frame, turning], how="diagonal"), calendar)
    early = {gate for day, gate, _ in result.ladder[20:80]}
    assert early == {MarketGate.RED.value}
    assert only_trade(result).r_multiple == PLANTED.r_multiple


def test_funnel_counts_exposure_full_against_the_open_book_at_cost() -> None:
    """SW9.1: the tier's ceiling is measured against what is already spent. At rung 0 (25%)
    a second 20% position the day after the first is refused, once."""
    calendar = planted_frame()[1]
    frame, calendar = planted_frame(extra=second_flag("FLAGLATE", LOSE_TAIL, calendar, offset=1))
    heavy = SwingConfig(sizing=SizingConfig(risk_per_trade_pct=2.0))
    result = run(frame, calendar, config=heavy)
    trade = only_trade(result)
    assert trade.symbol == PLANTED.symbol
    assert trade.quantity == int(D(1_000_000) * 20 / 100 / PLANTED.entry_open)
    assert result.funnel["skipped_exposure"] == 1


def test_a_one_session_run_is_allowed() -> None:
    frame, calendar = planted_frame()
    result = run(frame, calendar, start=calendar[ENTRY_BAR], end=calendar[ENTRY_BAR])
    assert result.funnel["sessions"] == 1
    assert len(result.equity_curve) == 1


def test_a_calendar_with_a_repeated_day_is_refused() -> None:
    frame, calendar = planted_frame()
    with pytest.raises(ValueError, match="increasing"):
        run_backtest(frame, params_for(calendar), calendar=[calendar[0], *calendar])


# --- SW9.5: the session cap and the drawdown lock-out ------------------------------------


def test_at_most_three_new_entries_a_session_and_the_rest_are_session_cap_skips() -> None:
    """`04` §5 / §9.1 (SW9.5): "1, 2, 3 stocks per day" — `max_new_entries_per_session` [3].

    Five identical flags set up on the same day. At a rung that allows five positions (the
    ladder's own rung 0 allows two, and `TIER_FULL` would bind first), the plan lines exactly
    three — best score first, then symbol — and answers the other two `SESSION_CAP`, which the
    funnel counts as ``skipped_session_cap``. Nothing else refused them: the sleeve had the
    cash (five 7.5% positions), the exposure ceiling was 100%, and every stop was inside its
    ADR.
    """
    calendar = planted_frame()[1]
    extra: list[dict[str, object]] = []
    for index, symbol in enumerate(("FLAGB", "FLAGC", "FLAGD", "FLAGE")):
        extra += second_flag(symbol, WIN_TAIL, calendar, instrument_id=10 + index)
    frame, calendar = planted_frame(extra=extra)
    roomy = SwingConfig(market=MarketConfig(tiers=((5, 100.0), (10, 100.0))))
    result = run(frame, calendar, config=roomy, end=calendar[ENTRY_BAR])
    entered = {t.symbol for t in result.trades}
    assert len(entered) == DEFAULT_SWING_CONFIG.sizing.max_new_entries_per_session == 3
    assert entered == {"FLAGB", "FLAGC", "FLAGD"}, "same score, so the symbol order decides"
    assert result.funnel["entered"] == 3
    assert result.funnel["skipped_session_cap"] == 2
    assert result.funnel["skipped_tier"] == 0
    assert result.funnel["skipped_exposure"] == 0
    assert result.funnel["skipped_size"] == 0


def test_a_sleeve_in_drawdown_is_locked_out_of_new_entries_until_it_recovers() -> None:
    """`04` §8.5 (SW9.5): a sleeve `max_drawdown_pct` below its peak plans no entries, every
    refusal says so (``skipped_drawdown``), and it stays locked until the drawdown is back
    inside `resume_drawdown_pct` — `market.drawdown_locked`'s hysteresis, which the backtest
    reads off its own equity curve exactly as the evening reads the sleeve's NAV.

    Two positions open on the entry day: FLAGLOSE (LOSE_TAIL) and FLAGHOLD (HOLD_TAIL). The
    peak is the untouched sleeve (₹10,00,000: both marks sit under cost until the loss). On
    day 8 FLAGLOSE gaps through its stop — 492 shares bought at 152.1976, sold at 140 less
    costs, 0.61% of the sleeve — and the sleeve closes 0.56% under its peak (FLAGHOLD carries
    a small unrealised gain). With the lock-out at 0.5% the ladder locks that evening; FLAGMID,
    which sets up on days 8 and 9, is refused on days 9 and 10. FLAGHOLD then rallies: day 10
    closes the sleeve 0.23% under the peak, day 11 above it. A release line of 0.25% lifts the
    lock on day 10's evening and FLAGLATE, set up that close, enters on day 11 at rung 0; a
    release line of 0.10% keeps the lock through day 10 (0.23% is still outside it), FLAGLATE is
    refused on day 11 and enters on day 12 once the sleeve is back at its peak.
    """
    calendar = planted_frame()[1]
    extra = (
        second_flag("FLAGLOSE", LOSE_TAIL, calendar)
        + second_flag("FLAGMID", WIN_TAIL, calendar, offset=9, instrument_id=6)
        + second_flag("FLAGLATE", WIN_TAIL, calendar, offset=11, instrument_id=7)
    )
    frame, calendar = planted_frame(tail=HOLD_TAIL, symbol="FLAGHOLD", extra=extra)
    loss = (received(D(repr(LOSE_TAIL[8].open))) - paid(PLANTED.entry_open)) * PLANTED.quantity
    assert (loss / D(1_000_000) * 100).quantize(TWO_DP) == D("-0.61")
    e = ENTRY_BAR

    def drawdown_on(result: BacktestResult, day: dt.date) -> Decimal:
        curve = dict(result.equity_curve)
        peak = max(equity for on, equity in result.equity_curve if on <= day)
        return ((peak - curve[day]) / peak * 100).quantize(TWO_DP)

    releasing = SwingConfig(market=MarketConfig(max_drawdown_pct=0.5, resume_drawdown_pct=0.25))
    result = run(frame, calendar, config=releasing)
    assert drawdown_on(result, calendar[e + 8]) == D("0.56") >= D("0.5")
    assert drawdown_on(result, calendar[e + 9]) == D("0.54")
    assert drawdown_on(result, calendar[e + 10]) == D("0.23") <= D("0.25")
    assert drawdown_on(result, calendar[e + 11]) == D("0.00")
    entries = {t.symbol: t.entry_date for t in result.trades}
    assert entries == {
        "FLAGLOSE": calendar[e],
        "FLAGHOLD": calendar[e],
        "FLAGLATE": calendar[e + 11],
    }, "FLAGMID never entered; FLAGLATE entered the morning after the release"
    assert result.funnel["skipped_drawdown"] >= 2, "FLAGMID on days 9 and 10, at least"
    assert result.funnel["skipped_gate"] == 0, "the tape stayed GREEN; the drawdown did it"
    assert {level for day, _, level in result.ladder if day >= calendar[e + 8]} == {0}

    holding = SwingConfig(market=MarketConfig(max_drawdown_pct=0.5, resume_drawdown_pct=0.10))
    result = run(frame, calendar, config=holding)
    entries = {t.symbol: t.entry_date for t in result.trades}
    assert entries["FLAGLATE"] == calendar[e + 12], "0.23% is outside a 0.10% release line"
    assert result.funnel["skipped_drawdown"] >= 3, "FLAGMID twice, FLAGLATE once, at least"


def test_a_sleeve_with_no_session_behind_it_is_not_in_drawdown() -> None:
    """The peak starts at the sleeve: on the first session nothing is locked, and the planted
    flag is entered on day one as before."""
    frame, calendar = planted_frame()
    result = run(frame, calendar)
    assert result.funnel["skipped_drawdown"] == 0
    assert only_trade(result).entry_date == calendar[ENTRY_BAR]
    assert drawdown_pct(peak=D(0), equity=D(-5)) == 0.0, "a ₹0 peak divides nothing"
    assert drawdown_pct(peak=D(100), equity=D(120)) == 0.0
    assert drawdown_pct(peak=D(100), equity=D(85)) == 15.0


# --- SW9.6: the index rule, the drawdown on the constant sleeve, gate-on vs gate-off ----------
#
# STANDING-ANSWERS A12 and B1-B5. The index is `index_series`: rising a point a session (the
# 10-day average five points over the 20-day: `long_bias`) until an optional crash pulls the fast
# average under the slow one on the crash session itself (`bearish`), so the gate turns RED on a
# session the test names.


def gates_of(result: BacktestResult) -> dict[dt.date, str]:
    return {day: gate for day, gate, _ in result.ladder}


def test_index_rule_turns_the_gate_red_on_the_session_of_its_own_close_and_refuses_the_entry() -> (
    None
):
    """`04` §8.2-8.3 inside the run: the 10-day under the 20-day is RED, whatever breadth says.
    A crash in the detection day's own close is read at that close (A12: "up to and including
    the session's own close"), the gate is RED that evening, and the planted flag — GREEN by
    breadth — is refused as a gate skip and never entered."""
    frame, calendar = planted_frame()
    result = run(frame, calendar, index=index_series(calendar, crash_on=DETECTION_BAR))
    gates = gates_of(result)
    assert gates[calendar[DETECTION_BAR - 1]] == MarketGate.GREEN.value
    assert gates[calendar[DETECTION_BAR]] == MarketGate.RED.value
    assert result.trades == ()
    assert result.funnel["skipped_gate"] >= 1
    assert result.comparison is not None
    assert result.comparison.index_supplied is True
    assert result.comparison.primary is GateMode.FULL
    assert result.params.index_slug is None, "a label the runner sets; the engine never needs it"


def test_index_look_ahead_shifting_the_series_one_session_moves_the_gate_by_exactly_one() -> None:
    """House rule 5 for the index: the same closes dated one session later change the gate on
    the boundary session only — the crash session reads GREEN until the crash is in its own
    close — and never a session earlier. The flag is then entered, because the detection close
    saw a rising benchmark."""
    frame, calendar = planted_frame()
    k = DETECTION_BAR
    on_time = gates_of(run(frame, calendar, index=index_series(calendar, crash_on=k)))
    late = run(frame, calendar, index=index_series(calendar, crash_on=k, shift=1))
    late_gates = gates_of(late)
    for day in calendar[:k]:
        assert on_time[day] == late_gates[day], (
            f"{day}: the shift reached back to an earlier session"
        )
    assert on_time[calendar[k]] == MarketGate.RED.value
    assert late_gates[calendar[k]] == MarketGate.GREEN.value
    for day in calendar[k + 1 :]:
        assert on_time[day] == late_gates[day] == MarketGate.RED.value
    assert only_trade(late).r_multiple == PLANTED.r_multiple


def test_index_reading_mirrors_the_workers_last_twenty_closes_and_is_ignored_until_they_exist() -> (
    None
):
    """The reading is the mean of the last `index_ma_fast` / `index_ma_slow` closes on or before
    the session — the nightly job's `load_index_reading` — and until `index_ma_slow` closes
    exist the index is ignored (`04` §8.2: "none → the index is ignored"), so the gate is
    breadth's. With nineteen closes before the detection day a crash reads as nothing; with
    twenty it reads as bearish."""
    frame, calendar = planted_frame()
    slow = DEFAULT_SWING_CONFIG.market.index_ma_slow
    fast = DEFAULT_SWING_CONFIG.market.index_ma_fast
    full = index_series(calendar, crash_on=DETECTION_BAR)
    closes = full["close"].to_list()
    reading = IndexReading(
        close=closes[DETECTION_BAR],
        ma_fast=sum(closes[DETECTION_BAR + 1 - fast : DETECTION_BAR + 1]) / fast,
        ma_slow=sum(closes[DETECTION_BAR + 1 - slow : DETECTION_BAR + 1]) / slow,
    )
    assert reading.bearish, "the fixture's crash crosses the averages on its own session"
    green_breadth = BreadthSnapshot(3, 33.33, 0.0, 66.67)
    assert market_gate(green_breadth, reading, DEFAULT_SWING_CONFIG.market) is MarketGate.RED
    assert market_gate(green_breadth, None, DEFAULT_SWING_CONFIG.market) is MarketGate.GREEN

    too_few = full.slice(DETECTION_BAR + 2 - slow, slow - 1)  # nineteen closes ending on the day
    assert too_few.height == slow - 1 and too_few["date"].max() == calendar[DETECTION_BAR]
    ignored = run(frame, calendar, index=too_few)
    assert gates_of(ignored)[calendar[DETECTION_BAR]] == MarketGate.GREEN.value
    assert only_trade(ignored).r_multiple == PLANTED.r_multiple

    just_enough = full.slice(DETECTION_BAR + 1 - slow, slow)
    assert just_enough.height == slow
    read = run(frame, calendar, index=just_enough)
    assert gates_of(read)[calendar[DETECTION_BAR]] == MarketGate.RED.value
    assert read.trades == ()


def test_index_none_keeps_the_breadth_only_gate_and_the_caveats_say_so() -> None:
    frame, calendar = planted_frame()
    without = run(frame, calendar)
    assert without.comparison is not None
    assert without.comparison.index_supplied is False
    assert without.comparison.primary is GateMode.BREADTH_ONLY
    assert without.comparison.index_rule is None
    assert without.caveats == (*CAVEATS, INDEX_ABSENT_CAVEAT)
    assert without.to_json()["caveats"] == [*CAVEATS, INDEX_ABSENT_CAVEAT]
    with_index = run(frame, calendar, index=index_series(calendar))
    assert with_index.caveats == CAVEATS
    assert with_index.comparison is not None
    assert with_index.comparison.index_rule is not None
    assert only_trade(with_index).r_multiple == only_trade(without).r_multiple


def test_index_frame_is_checked_before_a_session_runs() -> None:
    frame, calendar = planted_frame()
    with pytest.raises(ValueError, match="close column"):
        run(frame, calendar, index=index_series(calendar).drop("close"))
    doubled = pl.concat([index_series(calendar), index_series(calendar).head(1)])
    with pytest.raises(ValueError, match="two closes"):
        run(frame, calendar, index=doubled)
    labelled = replace(params_for(calendar), index_slug="nifty-500")
    with pytest.raises(ValueError, match="not supplied"):
        run_backtest(frame, labelled, calendar=calendar)
    named = run_backtest(frame, labelled, calendar=calendar, index=index_series(calendar))
    stored = named.to_json()["params"]
    assert isinstance(stored, dict)
    assert stored["index_slug"] == "nifty-500"


# -- the drawdown on the constant-sleeve curve --------------------------------------------------


SLEEVE = D(1_000_000)


def curve_drawdown_pct(result: BacktestResult, sleeve: Decimal = SLEEVE) -> list[Decimal]:
    """A12's measurement, by hand: peak-to-trough as a percentage of the sleeve, per session."""
    out: list[Decimal] = []
    peak = sleeve
    for _, equity in result.equity_curve:
        peak = max(peak, equity)
        out.append(((peak - equity) / sleeve * 100).quantize(TWO_DP) if equity < peak else D(0))
    return out


def test_drawdown_is_peak_to_trough_as_a_percentage_of_the_constant_sleeve() -> None:
    """The curve is realised P&L plus open positions marked at the close; the drawdown is how
    far it sits under its highest close, in percent of the ₹10 lakh every trade is sized on."""
    frame, calendar = planted_frame(tail=LOSE_TAIL)
    result = run(frame, calendar)
    by_hand = curve_drawdown_pct(result)
    assert result.drawdown.max_pct == max(by_hand) > D(0)
    trough_index = by_hand.index(max(by_hand))
    assert result.drawdown.trough_date == calendar[trough_index]
    assert result.drawdown.trough == result.equity_curve[trough_index][1]
    assert result.drawdown.peak == max(e for _, e in result.equity_curve[: trough_index + 1])
    assert result.drawdown.locked_sessions == 0, "0.61% is nowhere near the 15% line"
    # The loss itself: 492 shares bought at 152.1976, sold at the day-8 open less costs.
    loss = (paid(PLANTED.entry_open) - received(D(repr(LOSE_TAIL[8].open)))) * PLANTED.quantity
    assert result.drawdown.max_pct == (loss / D(1_000_000) * 100).quantize(TWO_DP) == D("0.61")
    assert result.to_json()["drawdown"] == {
        "max_pct": "0.61",
        "peak": str(result.drawdown.peak),
        "trough": str(result.drawdown.trough),
        "trough_date": calendar[trough_index].isoformat(),
        "locked_sessions": 0,
    }


def test_the_gate_cells_carry_the_curves_drawdown_inside_their_own_scope() -> None:
    """A12 / B4: the gate-on-versus-gate-off table reports the constant-sleeve curve's max
    drawdown per scope — the whole run's in `overall`, and in `by_year` only what the curve did
    inside that year: the losing trade's year carries the 0.61%, the year before it nothing."""
    frame, calendar = planted_frame(tail=LOSE_TAIL)
    result = run(frame, calendar)
    assert result.comparison is not None
    primary = result.comparison.primary
    assert result.comparison.overall[primary].max_drawdown_pct == result.drawdown.max_pct > D(0)
    trough_year = result.drawdown.trough_date.year if result.drawdown.trough_date else None
    assert trough_year is not None
    for year, cells in result.comparison.by_year.items():
        expected = result.drawdown.max_pct if year == trough_year else D(0)
        assert cells[primary].max_drawdown_pct == expected, year


def test_drawdown_of_sixteen_percent_locks_out_new_entries_until_back_within_ten() -> None:
    """`04` §8.5 at its real numbers, on the constant-sleeve curve (A12). Two 20 % positions
    (risk 2 % a trade caps at `max_position_pct`, 1,315 shares each): FLAGCRASH gaps to 22 on
    day 8 and the sleeve closes about 17 % under its peak — locked; FLAGMID, set up that
    evening, is refused `DRAWDOWN_LOCKOUT`; the lock holds through 10.84 % on day 14 (outside
    the 10 % release line) and lifts on day 15 at 9.52 %, so FLAGLATE, set up that close, is
    entered on day 16 at rung 0. Every percentage is worked by hand from the fixture's prices."""
    calendar = planted_frame(tail=RECOVER_TAIL)[1]
    extra = (
        second_flag("FLAGCRASH", CRASH_TAIL, calendar)
        + second_flag("FLAGMID", WIN_TAIL, calendar, offset=9, instrument_id=6)
        + flag_detected_on(
            "FLAGLATE",
            calendar,
            detection_index=ENTRY_BAR + 15,
            tail=WIN_TAIL[:2],
            instrument_id=7,
        )
    )
    frame, calendar = planted_frame(tail=RECOVER_TAIL, symbol="FLAGHOLD", extra=extra)
    heavy = SwingConfig(
        sizing=SizingConfig(risk_per_trade_pct=2.0),
        market=MarketConfig(tiers=((10, 100.0),)),
    )
    result = run(frame, calendar, config=heavy)
    e = ENTRY_BAR
    quantity = int(D(1_000_000) * 20 / 100 / PLANTED.entry_open)
    assert quantity == 1315
    paid_each = (paid(PLANTED.entry_open) * quantity).quantize(TWO_DP)
    crash_proceeds = (received(D(repr(CRASH_TAIL[8].open))) * quantity).quantize(TWO_DP)

    def equity_on(day: int) -> Decimal:
        cash = D(1_000_000) - 2 * paid_each + crash_proceeds
        return cash + quantity * D(repr(RECOVER_TAIL[day].close))

    def drawdown_on(day: int) -> Decimal:
        return ((D(1_000_000) - equity_on(day)) / D(1_000_000) * 100).quantize(TWO_DP)

    curve = dict(result.equity_curve)
    for day in range(8, 16):
        assert curve[calendar[e + day]] == equity_on(day), day
    assert drawdown_on(8) == D("16.99") >= D(15)
    assert drawdown_on(14) == D("10.84") > D(10)
    assert drawdown_on(15) == D("9.52") <= D(10)
    assert result.drawdown.max_pct == D("16.99")
    assert result.drawdown.trough_date == calendar[e + 8]
    entries = {t.symbol: t.entry_date for t in result.trades}
    assert entries == {
        "FLAGCRASH": calendar[e],
        "FLAGHOLD": calendar[e],
        "FLAGLATE": calendar[e + 16],
    }, "FLAGMID never entered; FLAGLATE the morning after the release"
    assert result.funnel["skipped_drawdown"] >= 1
    assert result.funnel["skipped_gate"] == 0, "the tape stayed GREEN; the drawdown did it"
    locked = [day for day, _, _ in result.ladder if calendar[e + 8] <= day <= calendar[e + 14]]
    assert len(locked) == 7
    assert result.drawdown.locked_sessions == 7
    assert {level for day, _, level in result.ladder if day >= calendar[e + 8]} == {0}


# -- gate-on against gate-off (A12) -----------------------------------------------------------


def test_gate_off_never_enters_fewer_than_gate_on_and_a_red_tape_shows_what_breadth_costs() -> None:
    """Gate-off is GREEN every session with the ladder and the lock-out still in force, so on
    these frames it enters everything the gated books enter and more. Without TAPECO the tape
    is RED: breadth-only enters nothing, gate-off enters the planted flag, and breadth's
    contribution is exactly minus that trade."""
    calendar = planted_frame()[1]
    frames = [
        planted_frame(),
        planted_frame(tail=LOSE_TAIL),
        planted_frame(extra=second_flag("FLAGLOSE", LOSE_TAIL, calendar)),
        (planted_frame()[0].filter(pl.col("symbol") != "TAPECO"), calendar),
    ]
    for frame, days in frames:
        comparison = run(frame, days, index=index_series(days)).comparison
        assert comparison is not None
        off = comparison.overall[GateMode.GATE_OFF].entered
        assert off >= comparison.overall[GateMode.BREADTH_ONLY].entered
        assert off >= comparison.overall[GateMode.FULL].entered
    red = run(frames[3][0], calendar)
    assert red.trades == ()
    assert red.comparison is not None
    assert red.comparison.overall[GateMode.BREADTH_ONLY].entered == 0
    assert red.comparison.overall[GateMode.GATE_OFF] == GateCell(
        entered=1,
        net_r=PLANTED.r_multiple,
        expectancy_r=PLANTED.r_multiple,
        win_rate_pct=D("100.00"),
        max_drawdown_pct=red.comparison.overall[GateMode.GATE_OFF].max_drawdown_pct,
    )
    assert red.comparison.breadth.overall.entered == -1
    assert red.comparison.breadth.overall.net_r == -PLANTED.r_multiple


def test_comparison_reports_the_three_books_per_year_and_per_setup() -> None:
    """A crash in the detection day's close: full refuses the flag, breadth-only and gate-off
    take it. Three columns, keyed by the year entered and by setup, with the index rule's
    contribution — full less breadth-only — labelled apart from breadth's."""
    frame, calendar = planted_frame()
    result = run(frame, calendar, index=index_series(calendar, crash_on=DETECTION_BAR))
    comparison = result.comparison
    assert comparison is not None
    assert comparison.modes == (GateMode.GATE_OFF, GateMode.BREADTH_ONLY, GateMode.FULL)
    assert [cell.entered for cell in comparison.overall.values()] == [1, 1, 0]
    year = calendar[ENTRY_BAR].year
    assert set(comparison.by_year) == {2025, 2026} and year == 2026
    assert [comparison.by_year[year][m].entered for m in comparison.modes] == [1, 1, 0]
    assert [comparison.by_year[2025][m].entered for m in comparison.modes] == [0, 0, 0]
    assert set(comparison.by_setup) == {Setup.FLAG.value, Setup.EP.value}
    assert [comparison.by_setup["FLAG"][m].net_r for m in comparison.modes] == [
        PLANTED.r_multiple,
        PLANTED.r_multiple,
        D(0),
    ]
    assert all(cell.max_drawdown_pct is None for cell in comparison.by_setup["FLAG"].values())
    assert comparison.breadth.overall == GateCell(0, D(0), D(0), D(0), D(0))
    assert comparison.index_rule is not None
    assert comparison.index_rule.overall.entered == -1
    assert comparison.index_rule.overall.net_r == -PLANTED.r_multiple
    assert comparison.index_rule.by_year[year].entered == -1
    assert comparison.index_rule.by_setup["FLAG"].net_r == -PLANTED.r_multiple
    assert comparison.index_rule.by_setup["EP"].entered == 0


def test_contribution_is_with_minus_without_on_every_scope() -> None:
    calendar = planted_frame()[1]
    frame, calendar = planted_frame(extra=second_flag("FLAGLOSE", LOSE_TAIL, calendar))
    comparison = run(
        frame, calendar, index=index_series(calendar, crash_on=ENTRY_BAR + 2)
    ).comparison
    assert comparison is not None
    assert comparison.index_rule is not None

    def check(
        cells: dict[GateMode, GateCell], delta: GateCell, *, with_it: GateMode, without: GateMode
    ) -> None:
        a, b = cells[with_it], cells[without]
        assert delta.entered == a.entered - b.entered
        assert delta.net_r == a.net_r - b.net_r
        assert delta.expectancy_r == a.expectancy_r - b.expectancy_r
        assert delta.win_rate_pct == a.win_rate_pct - b.win_rate_pct
        if a.max_drawdown_pct is None:
            assert delta.max_drawdown_pct is None
        else:
            assert b.max_drawdown_pct is not None
            assert delta.max_drawdown_pct == a.max_drawdown_pct - b.max_drawdown_pct

    for contribution, with_it, without in (
        (comparison.breadth, GateMode.BREADTH_ONLY, GateMode.GATE_OFF),
        (comparison.index_rule, GateMode.FULL, GateMode.BREADTH_ONLY),
    ):
        check(comparison.overall, contribution.overall, with_it=with_it, without=without)
        for year, cells in comparison.by_year.items():
            check(cells, contribution.by_year[year], with_it=with_it, without=without)
        for setup, cells in comparison.by_setup.items():
            check(cells, contribution.by_setup[setup], with_it=with_it, without=without)


def test_comparison_is_on_the_wire_with_the_mode_definitions() -> None:
    frame, calendar = planted_frame()
    payload = run(frame, calendar, index=index_series(calendar)).to_json()
    comparison = payload["comparison"]
    assert isinstance(comparison, dict)
    assert list(comparison) == [
        "index_supplied",
        "primary",
        "modes",
        "overall",
        "by_year",
        "by_setup",
        "contribution",
    ]
    assert comparison["index_supplied"] is True
    assert comparison["primary"] == "full"
    assert list(comparison["modes"]) == ["gate_off", "breadth_only", "full"]
    assert "GREEN every session" in comparison["modes"]["gate_off"]
    assert comparison["overall"]["full"]["net_r"] == str(PLANTED.r_multiple)
    assert comparison["contribution"]["breadth"]["overall"]["entered"] == 0
    assert comparison["contribution"]["index_rule"]["by_setup"]["EP"]["max_drawdown_pct"] is None
    without = run(frame, calendar).to_json()["comparison"]
    assert isinstance(without, dict)
    assert list(without["modes"]) == ["gate_off", "breadth_only"]
    assert without["contribution"]["index_rule"] is None
    json.dumps(payload)


def test_gate_on_books_share_one_detection_pass_so_the_primary_book_is_unchanged() -> None:
    """Three books cost one detection: the primary book's trade, funnel and curve are what the
    single-book engine produced before the comparison existed."""
    frame, calendar = planted_frame()
    result = run(frame, calendar)
    assert only_trade(result).r_multiple == PLANTED.r_multiple
    assert result.funnel["entered"] == 1
    assert result.comparison is not None
    assert result.comparison.overall[GateMode.BREADTH_ONLY].entered == 1
    assert result.comparison.overall[GateMode.BREADTH_ONLY].net_r == result.stats.net_r


# -- B1-B3: costs, circuits, the calendar, delisted names, fills at the next open --------------


def test_circuit_lock_is_read_from_upper_circuit_where_present_and_never_assumed_where_absent() -> (
    None
):
    """B1: `upper_circuit` at the high is a lock (`04` §3.5); a band above the high is not; and
    a null band — every bar before 2020 — is no lock at all, which the fourth caveat says."""
    calendar = planted_frame()[1]
    locked = planted_frame(extra=ep_rows(calendar, locked=True))[0]
    assert run(locked, calendar).funnel["skipped_locked"] == 1

    absent = planted_frame(extra=ep_rows(calendar))[0]
    gap_day = absent.filter(
        (pl.col("symbol") == "EPCO") & (pl.col("date") == calendar[DETECTION_BAR])
    )
    assert gap_day["upper_circuit"].item() is None
    entered = run(absent, calendar)
    assert entered.funnel["skipped_locked"] == 0
    assert {t.symbol for t in entered.trades} == {PLANTED.symbol, "EPCO"}

    above = absent.with_columns(
        pl.when((pl.col("symbol") == "EPCO") & (pl.col("date") == calendar[DETECTION_BAR]))
        .then(pl.col("high") * 1.5)
        .otherwise(pl.col("upper_circuit"))
        .alias("upper_circuit")
    )
    assert run(above, calendar).funnel["skipped_locked"] == 0
    assert "upper_circuit is absent no lock is assumed" in CAVEATS[3]


def test_delisted_name_is_sold_at_its_last_close_on_its_last_bar_and_counted_delisted() -> None:
    """B2: a name the runner keeps past its end (`instrument.delisted_on`) is sold at its last
    close — on the delisting day when it printed a bar, or the first session it did not — and
    counted `DELISTED`, never `NO_BAR` and never after the screener's five-session tolerance."""
    frame, calendar = planted_frame()
    last_bar = calendar[ENTRY_BAR + 1]
    gone = frame.filter(~((pl.col("symbol") == PLANTED.symbol) & (pl.col("date") > last_bar)))

    on_its_last_bar = only_trade(
        run(gone, calendar, cost_pct_per_side=D(0), delisted={1: last_bar})
    )
    assert on_its_last_bar.close_reason == BacktestCloseReason.DELISTED.value
    assert on_its_last_bar.exit_date == last_bar
    assert on_its_last_bar.exit_avg == D(repr(WIN_TAIL[1].close)).quantize(TWO_DP)

    later = calendar[ENTRY_BAR + 4]
    result = run(gone, calendar, cost_pct_per_side=D(0), delisted={1: later})
    trade = only_trade(result)
    assert trade.close_reason == BacktestCloseReason.DELISTED.value
    assert trade.exit_date == calendar[ENTRY_BAR + 2], "the first session it printed nothing"
    assert trade.exit_avg == D(repr(WIN_TAIL[1].close)).quantize(TWO_DP)
    assert result.funnel["closed_delisted"] == 1
    assert result.funnel["closed_no_bar"] == 0
    assert result.funnel["closed"] == result.funnel["entered"] == 1

    unknown = only_trade(run(gone, calendar, cost_pct_per_side=D(0)))
    assert unknown.close_reason == BacktestCloseReason.NO_BAR.value
    assert unknown.exit_date == calendar[ENTRY_BAR + 1 + MISSING_BAR_TOLERANCE_DAYS]


def test_calendar_holiday_bar_is_not_a_session_so_a_low_through_the_stop_on_it_never_fills() -> (
    None
):
    """B2: only sessions the calendar names trade. A bar printed on a weekday the calendar
    leaves out (a holiday in the data) is neither managed nor filled — a low through the stop on
    it is not a stop-out — and the planted trade comes out exactly as on the clean calendar."""
    holiday_bar = Bar(150.0, 151.0, 120.0, 149.0)
    tail = (WIN_TAIL[0], WIN_TAIL[1], holiday_bar, *WIN_TAIL[2:])
    frame, dated = planted_frame(tail=tail)
    holiday = dated[ENTRY_BAR + 2]
    assert D(repr(holiday_bar.low)) < PLANTED.stop
    calendar = [day for day in dated if day != holiday]
    result = run(frame, calendar)
    trade = only_trade(result)
    assert trade.r_multiple == PLANTED.r_multiple
    assert trade.close_reason == ActionReason.HARD_STOP_HIT.value
    assert trade.exit_date == calendar[ENTRY_BAR + PLANTED.exit_day]
    assert holiday not in dict(result.equity_curve)
    assert result.funnel["sessions"] == len(calendar)


def test_partial_next_open_after_the_day_3_to_5_signal_is_stops_manage_own_decision() -> None:
    """B3: the third sells at the next open after the day-3 close above the entry, and the
    decision is `stops.manage`'s — the same call, on the same bar, answers `SELL_PARTIAL` with
    the same quantity."""
    frame, calendar = planted_frame()
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
    signal_day = 3
    assert WIN_TAIL[signal_day].close > float(PLANTED.entry_open)
    assert WIN_TAIL[signal_day - 1].close > float(PLANTED.entry_open), "day 2 is before the window"
    position = OpenPosition(
        symbol=PLANTED.symbol,
        entry_date=calendar[ENTRY_BAR],
        entry=PLANTED.entry_open,
        initial_stop=PLANTED.stop,
        stop=PLANTED.stop,
        quantity=PLANTED.quantity,
        partial_done=False,
        trail=TrailMa(PLANTED.trail),
        is_ep_gap_day=False,
    )
    bar = WIN_TAIL[signal_day]
    decided = manage(
        position,
        DailyBar(
            date=calendar[ENTRY_BAR + signal_day],
            open=D(repr(bar.open)),
            high=D(repr(bar.high)),
            low=D(repr(bar.low)),
            close=D(repr(bar.close)),
            ma10=D(150),
            ma20=D(146),
            bars_since_entry=signal_day,
        ),
        DEFAULT_SWING_CONFIG.stops,
    )
    assert [a.kind for a in decided] == [ActionKind.SELL_PARTIAL, ActionKind.RAISE_STOP]
    assert decided[0].quantity == PLANTED.partial_quantity
    # The fill: the *next* session's open, not the signal day's close.
    q, part = PLANTED.quantity, PLANTED.partial_quantity
    expected = ((part * PLANTED.partial_fill + (q - part) * PLANTED.final_fill) / q).quantize(
        TWO_DP
    )
    assert PLANTED.partial_fill == D(repr(WIN_TAIL[signal_day + 1].open))
    assert PLANTED.partial_fill != D(repr(WIN_TAIL[signal_day].close))
    assert trade.exit_avg == expected


def test_trail_next_open_after_the_close_below_the_ma_is_stops_manage_own_decision() -> None:
    """B3: a close below the trail MA sells everything at the next session's open, and it is
    `stops.manage` that says so (`CLOSE_BELOW_TRAIL_MA`) — the backtest adds no exit rule."""
    tail = (
        Bar(152.0, 155.0, 150.0, 154.0),
        Bar(153.0, 154.0, 142.5, 143.0),  # above the stop, below the 10-day MA
        Bar(144.0, 146.0, 143.0, 145.0),
        *WIN_TAIL[3:],
    )
    frame, calendar = planted_frame(tail=tail)
    result = run(frame, calendar, cost_pct_per_side=D(0))
    trade = only_trade(result)
    assert trade.close_reason == ActionReason.CLOSE_BELOW_TRAIL_MA.value
    assert trade.exit_date == calendar[ENTRY_BAR + 2]
    assert trade.exit_avg == D(repr(tail[2].open)).quantize(TWO_DP)
    assert trade.exit_avg != D(repr(tail[1].close)).quantize(TWO_DP)
    indicated = with_swing_indicators(frame).filter(
        (pl.col("symbol") == PLANTED.symbol) & (pl.col("date") == calendar[ENTRY_BAR + 1])
    )
    ma10 = D(repr(indicated["ma_fast"].item())).quantize(FOUR_DP)
    assert D(repr(tail[1].close)) < ma10
    position = OpenPosition(
        symbol=PLANTED.symbol,
        entry_date=calendar[ENTRY_BAR],
        entry=PLANTED.entry_open,
        initial_stop=PLANTED.stop,
        stop=PLANTED.stop,
        quantity=PLANTED.quantity,
        partial_done=False,
        trail=TrailMa(PLANTED.trail),
        is_ep_gap_day=False,
    )
    decided = manage(
        position,
        DailyBar(
            date=calendar[ENTRY_BAR + 1],
            open=D(repr(tail[1].open)),
            high=D(repr(tail[1].high)),
            low=D(repr(tail[1].low)),
            close=D(repr(tail[1].close)),
            ma10=ma10,
            ma20=D(repr(indicated["ma_slow"].item())).quantize(FOUR_DP),
            bars_since_entry=1,
        ),
        DEFAULT_SWING_CONFIG.stops,
    )
    assert [(a.kind, a.reason) for a in decided] == [
        (ActionKind.SELL_ALL, ActionReason.CLOSE_BELOW_TRAIL_MA)
    ]


def test_determinism_holds_with_the_index_and_the_delisting_map() -> None:
    """B5: two runs with the same params and data are byte-identical, comparison included."""
    calendar = planted_frame()[1]
    frame, calendar = planted_frame(extra=second_flag("FLAGLOSE", LOSE_TAIL, calendar))
    index = index_series(calendar, crash_on=ENTRY_BAR + 6)
    delisted = {4: calendar[ENTRY_BAR + 4]}  # FLAGLOSE, four sessions before its stop-out
    first = json.dumps(run(frame, calendar, index=index, delisted=delisted).to_json())
    second = json.dumps(run(frame, calendar, index=index, delisted=delisted).to_json())
    assert first.encode() == second.encode()
    assert '"comparison"' in first and '"DELISTED"' in first


# --- determinism, purity, the wire shape (G4) -------------------------------------------


def test_determinism_two_runs_give_byte_identical_json() -> None:
    calendar = planted_frame()[1]
    frame, calendar = planted_frame(extra=second_flag("FLAGLOSE", LOSE_TAIL, calendar))
    first = json.dumps(run(frame, calendar).to_json())
    second = json.dumps(run(frame, calendar).to_json())
    assert first == second
    assert first.encode() == second.encode()


def test_to_json_is_plain_and_its_keys_are_in_a_fixed_order() -> None:
    frame, calendar = planted_frame()
    payload = run(frame, calendar).to_json()
    assert list(payload) == [
        "params",
        "trades",
        "stats",
        "by_setup",
        "by_year",
        "equity_curve",
        "funnel",
        "ladder",
        "drawdown",
        "comparison",
        "caveats",
    ]
    json.dumps(payload)  # nothing left that json cannot carry
    trade = payload["trades"]
    assert isinstance(trade, list)
    assert trade[0]["r_multiple"] == str(PLANTED.r_multiple)
    assert trade[0]["entry_date"] == calendar[ENTRY_BAR].isoformat()
    params = payload["params"]
    assert isinstance(params, dict)
    assert params["sleeve_inr"] == "1000000"
    assert params["cost_pct_per_side"] == "0.13"
    config = params["config"]
    assert isinstance(config, dict)
    market = config["market"]
    assert isinstance(market, dict)
    # `04` §8.4 as amended by `07`: the top rung is his "typical" ten, not eight.
    assert market["tiers"] == [[2, 25.0], [4, 50.0], [6, 75.0], [10, 100.0]]
    assert payload["caveats"] == [*CAVEATS, INDEX_ABSENT_CAVEAT], "no index in this run"
    by_year = payload["by_year"]
    assert isinstance(by_year, dict)
    assert list(by_year) == ["2025", "2026"]


def test_caveats_are_the_sentences_of_section_11() -> None:
    """Every standing caveat is a sentence of `04` §11: the three SW9 carried and B1's circuit
    caveat (SW9.6). The index-absent caveat is a run's own and is tested with the index."""
    rules = next(
        parent / "docs" / "swing" / "04-business-rules.md"
        for parent in Path(__file__).resolve().parents
        if (parent / "docs" / "swing" / "04-business-rules.md").is_file()
    ).read_text(encoding="utf-8")
    section = re.sub(r"\s+", " ", rules[rules.index("## §11") :].replace("`", ""))
    assert len(CAVEATS) == 4
    for caveat in CAVEATS:
        phrase = caveat.rstrip(".")
        phrase = phrase[0].lower() + phrase[1:]
        assert re.search(re.escape(phrase), section, flags=re.IGNORECASE), caveat


def test_empty_bars_run_flat() -> None:
    """The worker's typed empty frame: every session counted, the sleeve untouched, no error."""
    frame, calendar = planted_frame()
    empty = frame.clear()
    result = run(empty, calendar)
    assert result.trades == ()
    assert result.funnel["sessions"] == len(calendar)
    assert result.funnel["detected"] == 0, "B4: the funnel counts detections, not sessions"
    assert {equity for _, equity in result.equity_curve} == {D("1000000.00")}
    assert result.stats == summarize([])


def test_bad_parameters_are_refused() -> None:
    frame, calendar = planted_frame()
    with pytest.raises(ValueError, match="before start"):
        run(frame, calendar, start=calendar[-1], end=calendar[0])
    with pytest.raises(ValueError, match="increasing"):
        run_backtest(frame, params_for(calendar), calendar=list(reversed(calendar)))
    with pytest.raises(ValueError, match="sleeve"):
        run(frame, calendar, sleeve_inr=D(0))
    with pytest.raises(ValueError, match="symbol"):
        run(frame.drop("symbol"), calendar)


def test_nothing_the_backtest_hands_out_can_be_mutated() -> None:
    """The runner stores the result and the page reads it; neither may edit a number in place."""
    frame, calendar = planted_frame()
    result = run(frame, calendar)
    for target, attribute, value in (
        (result, "trades", ()),
        (result.params, "sleeve_inr", D(1)),
        (result.trades[0], "r_multiple", D(9)),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(target, attribute, value)


def test_a_bar_on_a_day_the_calendar_does_not_name_is_never_traded() -> None:
    """The calendar governs: a Saturday print (or a bar after the calendar's last day) is
    neither a session nor a fill price, and the run neither trades it nor trips over it."""
    frame, calendar = planted_frame()
    saturday = calendar[ENTRY_BAR] + dt.timedelta(days=(5 - calendar[ENTRY_BAR].weekday()) % 7)
    assert saturday.weekday() == 5 and saturday not in calendar
    beyond = calendar[-1] + dt.timedelta(days=1)
    extra = pl.DataFrame(
        {
            "instrument_id": [1, 1],
            "symbol": [PLANTED.symbol] * 2,
            "date": [saturday, beyond],
            "open": [10.0, 10.0],
            "high": [11.0, 11.0],
            "low": [9.0, 9.0],
            "close": [10.0, 10.0],
            "volume": [1e6, 1e6],
        }
    )
    with_strays = pl.concat([frame, extra], how="diagonal")
    result = run(with_strays, calendar)
    trade = only_trade(result)
    assert trade.r_multiple == PLANTED.r_multiple
    assert result.funnel["sessions"] == len(calendar)
    assert [day for day, _ in result.equity_curve] == calendar


# --- speed (G5) -------------------------------------------------------------------------


def test_speed_300_instruments_over_8_years_runs_under_60s() -> None:
    """SW9's acceptance: the full history in under 30 minutes. 300 names x 2,000 sessions here;
    the detectors are linear in the universe, so 2,500 names x 2,300 sessions extrapolates to
    about ten times this number. The measured seconds are recorded in STATUS.md.

    Skipped inside the mutation harness's workspace: a mutant's speed is not evidence of
    anything, and the harness's `-x` would otherwise spend a minute per surviving mutant here.
    """
    if ".mutants" in backtest_module.__file__:
        pytest.skip("speed is measured on the real module, not on a mutant")
    frame, calendar = speed_frame()
    assert frame.height == 300 * 2000
    started = perf_counter()
    result = run(frame, calendar)
    elapsed = perf_counter() - started
    assert result.funnel["sessions"] == 2000
    assert len(result.equity_curve) == 2000
    assert elapsed < 60, f"{elapsed:.1f}s"
