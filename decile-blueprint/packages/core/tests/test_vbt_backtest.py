"""The engine's arithmetic, on a planted trade a person can check on paper.

``vbt_backtest_fixtures`` plants one signal and works the trade out by hand in its docstring;
this file asserts each of those numbers. It needs no export and runs everywhere, which is the
point: the reproduction against the study (``test_vbt_goldens.py``) is the stronger evidence and
it skips on a machine without the 68 MB research bars.

``docs/vbt/04`` §11.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import numpy as np
import polars as pl
import pytest
from vbt_backtest_fixtures import (
    CLOSED_BELOW_EMA_ON,
    EXPECTED_ENTRY,
    EXPECTED_EXIT,
    EXPECTED_LIMIT,
    EXPECTED_QUANTITY,
    EXPECTED_STOP,
    FILLED_ON,
    SIGNAL_ON,
    SLEEVE,
    SOLD_ON,
    planted_bars,
)

from baskfy_core.vbt.backtest import (
    BacktestParams,
    BacktestResult,
    gate_vector,
    panel_from_frame,
    run_backtest,
    summarise,
    yearly,
)
from baskfy_core.vbt.breadth import breadth_series
from baskfy_core.vbt.calendar import build_calendar
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, EntryConfig, ExitConfig, Gate, VbtConfig
from baskfy_core.vbt.exits import ExitReason
from baskfy_core.vbt.indicators import with_vbt_indicators
from baskfy_core.vbt.signals import with_signal_columns
from baskfy_core.vbt.sizing import SizeCap


def tagged(config: VbtConfig = DEFAULT_VBT_CONFIG) -> pl.DataFrame:
    bars = planted_bars()
    return with_signal_columns(with_vbt_indicators(bars, build_calendar(bars), config), config)


def go(
    config: VbtConfig = DEFAULT_VBT_CONFIG,
    *,
    gate: bool = True,
    start: dt.date = FILLED_ON,
) -> BacktestResult:
    frame = tagged(config)
    panel = panel_from_frame(frame, "state")
    open_at = (
        gate_vector(breadth_series(frame, config), panel.sessions)
        if gate
        else np.ones(panel.sessions_count, dtype=bool)
    )
    return run_backtest(
        panel, open_at, BacktestParams(sleeve_inr=SLEEVE, start=start, config=config)
    )


class TestThePlantedTrade:
    def test_exactly_one_signal_is_planted(self) -> None:
        frame = tagged()
        signals = frame.filter(pl.col("state") == "SIGNAL")
        assert signals.height == 1
        assert signals["date"].to_list() == [SIGNAL_ON]
        assert signals["symbol"].to_list() == ["PLANTED"]

    def test_it_becomes_exactly_one_trade(self) -> None:
        assert len(go().trades) == 1

    def test_the_limit_is_the_signal_bar_s_close(self) -> None:
        """`04` §7.1 — not its high, not a buffer above it, not the next open."""
        frame = tagged()
        close = frame.filter((pl.col("date") == SIGNAL_ON) & (pl.col("instrument_id") == 1))[
            "close"
        ].to_list()[0]
        assert Decimal(str(close)) == EXPECTED_LIMIT

    def test_the_fill_is_the_better_of_the_open_and_the_limit(self) -> None:
        """The session opened at ₹97 and traded down to ₹95; the resting bid got ₹96."""
        trade = go().trades[0]
        assert trade.entry_date == FILLED_ON
        assert trade.entry_price == EXPECTED_ENTRY  # the fill plus 25 bps

    def test_the_quantity_is_the_slot_divided_by_the_cost_basis(self) -> None:
        """`04` §5.2 — ``floor(₹1,00,000 / ₹96.24)``, and the slot is what bound it."""
        trade = go().trades[0]
        assert trade.quantity == EXPECTED_QUANTITY
        assert trade.cap is SizeCap.SLOT

    def test_the_exit_is_the_next_open_after_the_close_below_the_ema(self) -> None:
        """`04` §6.2 — the working exit, and the only one this trade meets."""
        trade = go().trades[0]
        assert trade.exit_date == SOLD_ON
        assert trade.exit_price == EXPECTED_EXIT
        assert trade.reason is ExitReason.EMA_EXIT

    def test_the_profit_is_the_proceeds_less_the_cost_basis(self) -> None:
        trade = go().trades[0]
        proceeds = EXPECTED_EXIT * EXPECTED_QUANTITY * Decimal("0.9975")
        basis = EXPECTED_ENTRY * EXPECTED_QUANTITY
        assert trade.pnl_inr == proceeds - basis
        assert float(trade.return_pct) == pytest.approx(-6.7176, abs=1e-4)

    def test_the_r_multiple_reads_the_stop_the_fill_set(self) -> None:
        """`04` §6.1 — 12% below **the fill**, floored to the paise."""
        trade = go().trades[0]
        risk = EXPECTED_ENTRY - EXPECTED_STOP
        expected = (trade.pnl_inr / (EXPECTED_ENTRY * EXPECTED_QUANTITY)) / (risk / EXPECTED_ENTRY)
        assert trade.r_multiple is not None
        assert float(trade.r_multiple) == pytest.approx(float(expected), abs=1e-9)
        assert float(trade.r_multiple) == pytest.approx(-0.55, abs=0.005)

    def test_it_was_held_two_sessions(self) -> None:
        assert go().trades[0].hold_sessions == 2


class TestTheSequencing:
    def test_the_close_below_the_ema_is_the_session_before_the_sale(self) -> None:
        """`04` §11 step 5 — the exit is *queued* at the close and fills at the next open, which
        is what a book that only sees closed bars can actually do."""
        frame = tagged()
        row = frame.filter(
            (pl.col("date") == CLOSED_BELOW_EMA_ON) & (pl.col("instrument_id") == 1)
        ).to_dicts()[0]
        assert row["close"] < row["ema_exit"]
        assert go().trades[0].exit_date == SOLD_ON

    def test_a_shut_gate_lets_no_entry_through(self) -> None:
        """`04` §4.3 — and the position that never opened is not a trade."""
        impossible = VbtConfig(breadth=type(DEFAULT_VBT_CONFIG.breadth)(min_pct_above_dma=99.0))
        assert go(impossible) == go(impossible)
        assert len(go(impossible).trades) == 0

    def test_the_gate_is_read_at_the_signal_s_own_close(self) -> None:
        frame = tagged()
        breadth = breadth_series(frame)
        verdict = breadth.filter(pl.col("date") == SIGNAL_ON)["gate"].to_list()[0]
        assert verdict == Gate.OPEN.value

    def test_a_two_session_window_still_fills_this_one(self) -> None:
        """The fill is on the very next session, so shortening the window changes nothing here —
        which is the honest thing for this fixture to say. The cliff is measured against the
        study, in ``test_vbt_goldens``."""
        short = VbtConfig(entry=EntryConfig(valid_sessions=2))
        assert len(go(short).trades) == 1

    def test_a_tighter_stop_takes_this_trade_out_at_the_stop_instead(self) -> None:
        """The session before the sale traded down to ₹88. A 9% stop sits at ₹87.36 and misses
        it; a 5% stop sits at ₹91.20 and does not."""
        tight = VbtConfig(exits=ExitConfig(stop_pct=5.0))
        trade = go(tight).trades[0]
        assert trade.reason is ExitReason.STOP_HIT
        assert trade.exit_date == CLOSED_BELOW_EMA_ON
        assert trade.exit_price == Decimal("91.20")


class TestTheCurveAndTheStatistics:
    def test_the_equity_curve_starts_at_the_sleeve_and_ends_in_cash(self) -> None:
        result = go()
        assert result.equity[-1] < SLEEVE  # the planted trade loses money
        stats = summarise(result)
        assert stats is not None
        assert stats.trades == 1
        assert stats.win_rate_pct == 0.0

    def test_the_drawdown_is_measured_against_the_running_peak(self) -> None:
        stats = summarise(go())
        assert stats is not None
        assert stats.max_drawdown_pct < 0
        assert stats.drawdown_peak_on <= stats.drawdown_trough_on

    def test_the_exit_reasons_are_counted(self) -> None:
        stats = summarise(go())
        assert stats is not None
        assert stats.by_reason == {ExitReason.EMA_EXIT.value: 1}

    def test_the_statistics_survive_a_round_trip_through_json(self) -> None:
        """``vb_backtest_run.stats`` stores this shape; a float in it would be a lie about money."""
        stats = summarise(go())
        assert stats is not None
        payload = stats.to_json()
        assert payload["trades"] == 1
        assert isinstance(payload["final_equity_inr"], str)
        assert payload["start"] == stats.start.isoformat()

    def test_the_yearly_table_credits_the_year_the_trade_closed_in(self) -> None:
        """The planted run straddles a new year, so this also pins that a trade is counted in
        the year it **exited**, which is the year whose return it moved."""
        rows = {row.year: row for row in yearly(go())}
        assert rows[SOLD_ON.year].trades == 1
        assert sum(row.trades for row in rows.values()) == 1

    def test_an_empty_run_summarises_to_nothing_rather_than_raising(self) -> None:
        """``None``, not a record of zeros: zeros would claim the book was measured."""
        empty = BacktestResult(
            params=BacktestParams(), trades=(), sessions=(), equity=(), open_positions=()
        )
        assert summarise(empty) is None


class TestThePanel:
    def test_a_missing_bar_is_nan_and_is_never_filled_forward(self) -> None:
        bars = planted_bars().filter(
            ~((pl.col("instrument_id") == 1) & (pl.col("date") == CLOSED_BELOW_EMA_ON))
        )
        frame = with_signal_columns(with_vbt_indicators(bars, build_calendar(bars)))
        panel = panel_from_frame(frame, "state")
        column = panel.column_of(CLOSED_BELOW_EMA_ON)
        assert np.isnan(panel.close[0, column])

    def test_the_scan_column_can_drive_the_engine_too(self) -> None:
        """`04` §11's third book: the five Chartink lines with the same gate and execution."""
        frame = tagged()
        panel = panel_from_frame(frame, "scan_hit")
        assert panel.signal.sum() >= 1

    def test_the_symbols_ride_through(self) -> None:
        panel = panel_from_frame(tagged(), "state")
        assert set(panel.symbols) == {"PLANTED", "FLATCO"}
