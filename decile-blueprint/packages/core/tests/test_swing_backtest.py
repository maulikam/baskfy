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
    DETECTION_BAR,
    ENTRY_BAR,
    LOSE_TAIL,
    PLANTED,
    THROUGH_TRIGGER_DAY,
    WIN_TAIL,
    Bar,
    entry_variant,
    ep_rows,
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
from baskfy_core.swing.backtest import BacktestCloseReason
from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    MarketConfig,
    Setup,
    SizingConfig,
    SwingConfig,
)
from baskfy_core.swing.indicators import with_swing_indicators
from baskfy_core.swing.journal import summarize
from baskfy_core.swing.market import MarketGate
from baskfy_core.swing.setups import detect_flags
from baskfy_core.swing.stops import ActionReason

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
) -> BacktestResult:
    params = params_for(
        calendar,
        start=start,
        end=end,
        sleeve_inr=sleeve_inr,
        cost_pct_per_side=cost_pct_per_side,
        config=config,
    )
    return run_backtest(frame, params, calendar=calendar)


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
    """The entry (at the trigger) happened before the low did, so the open is not the fill."""
    frame, calendar = planted_frame()
    trigger = float(planted_trigger(frame, calendar))
    frame, calendar = planted_frame(tail=entry_variant(Bar(140.0, trigger + 1.0, 139.0, 141.0)))
    trade = only_trade(run(frame, calendar, cost_pct_per_side=D(0)))
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
    assert market["tiers"] == [[2, 25.0], [4, 50.0], [6, 75.0], [8, 100.0]]
    assert payload["caveats"] == list(CAVEATS)
    by_year = payload["by_year"]
    assert isinstance(by_year, dict)
    assert list(by_year) == ["2025", "2026"]


def test_caveats_are_the_three_sentences_of_section_11() -> None:
    rules = next(
        parent / "docs" / "swing" / "04-business-rules.md"
        for parent in Path(__file__).resolve().parents
        if (parent / "docs" / "swing" / "04-business-rules.md").is_file()
    ).read_text(encoding="utf-8")
    section = re.sub(r"\s+", " ", rules[rules.index("## §11") :].replace("`", ""))
    assert len(CAVEATS) == 3
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
