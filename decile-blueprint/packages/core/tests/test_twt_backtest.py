"""``baskfy_core.twt.backtest`` — the sequencing of ``docs/twt/04`` §11, branch by branch.

``test_twt_goldens.py`` proves the engine reproduces the study, which is the strongest test there
is and the least *diagnostic* one: when 164 trades move together, the report says which numbers
changed and not which rule did. This module is the other half — one small hand-built panel per
rule, so that removing a branch fails a test whose name says what the branch was for.

The fixtures are matrices rather than frames on purpose. :func:`panel_from_frame` has its own
class here; everything below it wants to say "this name's low on this session was 75" without
also arranging for a tight base, three weekly closes and a 200-session average to exist.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import numpy as np
import polars as pl
import pytest
from twt_fixtures import calendar_for, flat_bars, sessions

from baskfy_core.twt.backtest import (
    PANEL_COLUMNS,
    BacktestParams,
    BacktestResult,
    BacktestTrade,
    Panel,
    gate_vector,
    panel_from_frame,
    run_backtest,
    summarise,
    yearly,
)
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, RESEARCH_TICK_INR, TICK_INR, Gate, TwtConfig
from baskfy_core.twt.exits import ExitReason
from baskfy_core.twt.indicators import INDICATOR_COLUMNS, REQUIRED_COLUMNS
from baskfy_core.twt.signals import SIGNAL_COLUMNS, signal_mask, with_twt_columns
from baskfy_core.twt.sizing import SizeCap

#: The study's tick, because these tests assert levels to the paisa the way TW2's goldens do.
PAISA = Decimal(RESEARCH_TICK_INR)
#: A turnover average so large that ``04`` §6.2's 1 % cap can never be the binding one.
DEEP = 1e12
SLEEVE = Decimal("1000000")


def book(
    *,
    count: int,
    names: dict[str, dict[str, list[float | None]]],
    signals: dict[str, list[bool]],
    turnover_avg: dict[str, float] | None = None,
    rank: dict[str, float] | None = None,
) -> Panel:
    """A panel from per-name lists. ``None`` in a bar list is a session the name did not print."""
    days = sessions(count)
    symbols = sorted(names)

    def matrix(field: str) -> np.ndarray:
        out = np.full((len(symbols), count), np.nan)
        for row, symbol in enumerate(symbols):
            for col, value in enumerate(names[symbol][field]):
                if value is not None:
                    out[row, col] = value
        return out

    def flat(values: dict[str, float] | None, fallback: float) -> np.ndarray:
        out = np.full((len(symbols), count), fallback)
        for row, symbol in enumerate(symbols):
            if values is not None and symbol in values:
                out[row, :] = values[symbol]
        return out

    flags = np.zeros((len(symbols), count), dtype=bool)
    for row, symbol in enumerate(symbols):
        for col, value in enumerate(signals.get(symbol, [])):
            flags[row, col] = value

    return Panel(
        symbols=tuple(symbols),
        instrument_ids=tuple(range(1, len(symbols) + 1)),
        sessions=tuple(days),
        open=matrix("open"),
        high=matrix("high"),
        low=matrix("low"),
        close=matrix("close"),
        turnover_inr=flat(rank, DEEP),
        turnover_avg_20=flat(turnover_avg, DEEP),
        signal=flags,
    )


def bars(*, count: int, price: float = 100.0) -> dict[str, list[float | None]]:
    """A quiet name: it opens and closes at ``price`` and trades a little under it.

    The low is **not** equal to the open, and that is deliberate: a bar with
    ``open == high == low`` is a name locked at a circuit, which ``04`` §5.2 refuses to fill. A
    fixture built out of perfectly flat bars would silently test the refusal instead of the rule.
    """
    return {
        "open": [price] * count,
        "high": [price] * count,
        "low": [round(price * 0.99, 4)] * count,
        "close": [price] * count,
    }


def one_name(
    count: int = 10, price: float = 100.0, signal_on: int = 0, **edits: dict[int, float | None]
) -> Panel:
    """One instrument, flat at ``price``, signalling once. ``edits`` overwrite single cells."""
    series = bars(count=count, price=price)
    for field, changes in edits.items():
        for col, value in changes.items():
            series[field][col] = value
    flags = [False] * count
    flags[signal_on] = True
    return book(count=count, names={"AAA": series}, signals={"AAA": flags})


def all_open(panel: Panel) -> np.ndarray:
    return np.ones(panel.sessions_count, dtype=bool)


def params(  # noqa: PLR0913 - one keyword per field a test ever varies
    *,
    sleeve_inr: Decimal = SLEEVE,
    tick: Decimal = PAISA,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
    start: dt.date | None = None,
    end: dt.date | None = None,
    cost_pct_per_side: Decimal = DEFAULT_TWT_CONFIG.costs.cost_bps_per_side / 100,
) -> BacktestParams:
    """The study's tick and money, as every test below wants them unless it says otherwise."""
    return BacktestParams(
        sleeve_inr=sleeve_inr,
        tick=tick,
        config=config,
        start=start,
        end=end,
        cost_pct_per_side=cost_pct_per_side,
    )


def run(  # noqa: PLR0913 - it forwards `params`, and a **kwargs would cost the type check
    panel: Panel,
    gate: np.ndarray | None = None,
    *,
    sleeve_inr: Decimal = SLEEVE,
    tick: Decimal = PAISA,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
    start: dt.date | None = None,
    end: dt.date | None = None,
    cost_pct_per_side: Decimal = DEFAULT_TWT_CONFIG.costs.cost_bps_per_side / 100,
) -> BacktestResult:
    return run_backtest(
        panel,
        all_open(panel) if gate is None else gate,
        params(
            sleeve_inr=sleeve_inr,
            tick=tick,
            config=config,
            start=start,
            end=end,
            cost_pct_per_side=cost_pct_per_side,
        ),
    )


class TestThePanelIsTheFrameTheSleeveProduces:
    """``panel_from_frame`` — every column comes from the sleeve's own detection."""

    def test_the_matrices_are_instruments_by_sessions(self) -> None:
        frame = self.detected()
        panel = panel_from_frame(frame, "entry_event")
        assert panel.instruments == 1
        assert panel.sessions_count == frame["date"].n_unique()
        assert panel.open.shape == (panel.instruments, panel.sessions_count)

    def test_a_session_one_name_did_not_print_is_nan_and_not_a_forward_fill(self) -> None:
        first = flat_bars(count=60, instrument_id=1, symbol="AAA")
        second = flat_bars(count=60, instrument_id=2, symbol="BBB")
        hole = second["date"][30]
        both = pl.concat([first, second.filter(pl.col("date") != hole)])
        frame = with_twt_columns(both, calendar_for(both), DEFAULT_TWT_CONFIG)
        panel = panel_from_frame(frame, "entry_event")
        column = panel.sessions.index(hole)
        assert np.isnan(panel.close[1, column]), "the name that did not trade has no close"
        assert not np.isnan(panel.close[0, column]), "the name that did trade still does"

    def test_a_boolean_column_is_read_as_itself(self) -> None:
        frame = self.detected().with_columns(
            pl.Series("signal", [True] + [False] * (self.height() - 1))
        )
        panel = panel_from_frame(frame, "signal")
        assert int(panel.signal.sum()) == 1

    def test_a_state_column_is_read_as_SIGNAL_and_SCAN_ONLY_is_not_a_signal(self) -> None:
        height = self.height()
        column = ["SCAN_ONLY"] * height
        column[0] = "SIGNAL"
        frame = self.detected().with_columns(pl.Series("signal_state", column))
        panel = panel_from_frame(frame, "signal_state")
        assert int(panel.signal.sum()) == 1

    def test_a_column_the_frame_does_not_carry_leaves_no_signal_rather_than_raising(self) -> None:
        panel = panel_from_frame(self.detected(), "a_column_nobody_computed")
        assert not panel.signal.any()
        assert not run(panel).trades

    def test_the_symbol_rides_along_so_a_trade_can_name_itself(self) -> None:
        panel = panel_from_frame(self.detected(), "entry_event")
        assert panel.symbols == ("TWTCO",)

    def height(self) -> int:
        return self.detected().height

    def detected(self) -> pl.DataFrame:
        raw = flat_bars(count=60)
        return with_twt_columns(raw, calendar_for(raw), DEFAULT_TWT_CONFIG)


class TestTheGateIsReadAtThePreviousClose:
    """``04`` §4.5 and §11.3 — the gate that governs a fill is the **signal** session's."""

    def test_a_session_with_no_breadth_row_is_shut_and_never_neutral(self) -> None:
        panel = one_name()
        breadth = pl.DataFrame({"date": [panel.sessions[0]], "gate": [Gate.OPEN.value]})
        vector = gate_vector(breadth, panel.sessions)
        assert bool(vector[0]) is True
        assert not vector[1:].any()

    def test_the_fill_reads_the_signal_sessions_gate_and_not_the_fill_sessions(self) -> None:
        panel = one_name(signal_on=0)
        gate = np.ones(panel.sessions_count, dtype=bool)
        gate[1] = False  # shut on the fill session, open on the signal session
        assert len(run(panel, gate).trades) == 1

    def test_a_gate_shut_at_the_signals_close_refuses_the_fill_and_counts_it(self) -> None:
        panel = one_name(signal_on=0)
        gate = np.ones(panel.sessions_count, dtype=bool)
        gate[0] = False
        result = run(panel, gate)
        assert result.trades == ()
        assert result.skipped["gate_shut"] == 1


class TestExitsHappenAtTheOpenBeforeAnythingElse:
    """``04`` §7.1, §7.2 and §11.1."""

    def test_an_open_at_or_below_the_stop_fills_at_the_open_not_at_the_stop(self) -> None:
        # Entry at 100 on session 1 -> stop 80.00. Session 2 opens at 70.
        panel = one_name(open={2: 70.0}, high={2: 70.5}, low={2: 69.0}, close={2: 70.0})
        trade = self.only(run(panel))
        assert trade.reason is ExitReason.STOP_GAP
        assert trade.exit_price == Decimal("70.00")
        assert trade.exit_date == panel.sessions[2]

    def test_a_low_through_the_stop_fills_at_the_stop(self) -> None:
        panel = one_name(low={2: 75.0}, close={2: 78.0})
        trade = self.only(run(panel))
        assert trade.reason is ExitReason.STOP_HIT
        assert trade.exit_price == Decimal("80.00")

    def test_a_name_that_stops_printing_is_written_off_at_its_last_close(self) -> None:
        blank: dict[int, float | None] = dict.fromkeys(range(2, 10))
        panel = one_name(count=10, open=blank, high=blank, low=blank, close=blank)
        trade = self.only(run(panel))
        assert trade.reason is ExitReason.NO_BAR
        assert trade.exit_price == Decimal("100.00")
        assert trade.exit_date == panel.sessions[6], "five blank sessions, then the write-off"

    def test_four_blank_sessions_are_tolerated_and_the_fifth_is_not(self) -> None:
        gap: dict[int, float | None] = dict.fromkeys(range(2, 6))
        panel = one_name(count=10, open=gap, high=gap, low=gap, close=gap)
        # Session 6 prints again, so the run of blanks never reaches `no_bar_sessions`.
        assert self.only(run(panel)).reason is ExitReason.END_OF_RUN

    def only(self, result: BacktestResult) -> BacktestTrade:
        assert len(result.trades) == 1, result.trades
        return result.trades[0]


class TestTheFillDayRule:
    """``04`` §7.4 — the entry session's own low can take the position out."""

    def test_the_entry_sessions_low_through_the_stop_is_a_day_zero_exit(self) -> None:
        panel = one_name(low={1: 75.0})
        result = run(panel)
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.reason is ExitReason.STOP_DAY0
        assert trade.exit_price == Decimal("80.00")
        assert trade.hold_sessions == 0
        assert trade.entry_date == trade.exit_date

    def test_the_stop_is_checked_before_any_favourable_move_is_counted(self) -> None:
        # The session that takes it out also printed a new high. The stop wins (`04` §11.4).
        panel = one_name(low={1: 75.0}, high={1: 140.0})
        assert run(panel).trades[0].reason is ExitReason.STOP_DAY0

    def test_a_stop_derived_from_the_open_can_never_be_gapped_through_on_the_fill_day(self) -> None:
        """The gapped branch of ``exits.fill_day_stop`` is unreachable **from this engine**.

        The initial stop is ``stop_pct`` below the same open the fill happened at, so
        ``open <= stop`` cannot hold. The branch exists for the live book, where the stop is the
        one resting at the exchange; ``test_twt_exits.py`` covers it there. Asserting the
        unreachability is what keeps the two readings from silently diverging.
        """
        stop_fraction = 1 - float(DEFAULT_TWT_CONFIG.exits.stop_pct) / 100
        assert stop_fraction < 1
        panel = one_name(low={1: 0.0})
        trade = run(panel).trades[0]
        assert trade.exit_price == Decimal("80.00"), "the stop, never the open"


class TestTheTrailRatchets:
    """``04`` §7.2 — the stop follows the highest high since entry, clamped under the close."""

    def test_the_stop_rises_with_the_high_and_the_exit_proves_it(self) -> None:
        # Session 2 prints a high of 200 -> the trail moves to 160.00, and session 3's low takes
        # it. Without the ratchet the stop would still be 80 and the trade would survive.
        panel = one_name(
            count=6,
            open={2: 150.0, 3: 170.0},
            high={2: 200.0, 3: 175.0},
            low={2: 150.0, 3: 150.0},
            close={2: 190.0, 3: 165.0},
        )
        trade = run(panel).trades[0]
        assert trade.reason is ExitReason.STOP_HIT
        assert trade.exit_price == Decimal("160.00")

    def test_the_trail_is_clamped_under_the_close_so_an_armed_stop_does_not_fire_at_once(
        self,
    ) -> None:
        # High 200 on session 2 with a close of 161: the raw trail (160.00) is below the close,
        # but `min(trail, close x 0.9999)` = 160.00 still wins. Push the close to 160.005 and the
        # clamp binds instead: tick_floor(160.005 x 0.9999) = 159.98.
        panel = one_name(
            count=6,
            open={2: 150.0, 3: 160.0},
            high={2: 200.0, 3: 160.5},
            low={2: 150.0, 3: 159.0},
            close={2: 160.005, 3: 159.5},
        )
        trade = run(panel).trades[0]
        assert trade.exit_price == Decimal("159.98")

    def test_a_run_that_never_trails_leaves_the_initial_stop_in_force(self) -> None:
        panel = one_name(count=6, low={3: 79.99})
        assert run(panel).trades[0].exit_price == Decimal("80.00")

    def test_the_study_never_reached_the_clamps_stop_lowering_branch(self) -> None:
        """DECISIONS-TW **TW1.4** and **TW2.12**: counted, not assumed away."""
        assert run(one_name()).clamped_below_stop == 0


class TestTheCapsDecideWhoGetsIn:
    """``04`` §6.1-6.3 — ten slots, three a session, ranked by the signal day's turnover."""

    def many(self, how_many: int, *, rank: dict[str, float] | None = None) -> Panel:
        names = {f"N{index:02d}": bars(count=6) for index in range(how_many)}
        return book(
            count=6,
            names=names,
            signals={name: [True] + [False] * 5 for name in names},
            rank=rank,
        )

    def test_at_most_three_positions_open_in_one_session(self) -> None:
        result = run(self.many(5))
        assert len({trade.symbol for trade in result.trades}) == 3
        assert result.skipped["session_cap"] == 1

    def test_the_three_are_the_three_with_the_most_signal_day_turnover(self) -> None:
        panel = self.many(5, rank={f"N{i:02d}": float(i) for i in range(5)})
        taken = {trade.symbol for trade in run(panel).trades}
        assert taken == {"N04", "N03", "N02"}

    def test_the_slot_ceiling_stops_an_eleventh_position(self) -> None:
        names = {f"N{index:02d}": bars(count=12) for index in range(12)}
        signals = {
            name: [index // 3 == col for col in range(12)]
            for index, name in enumerate(sorted(names))
        }
        result = run(book(count=12, names=names, signals=signals))
        assert len(result.trades) == 10
        assert result.skipped["slots_full"] >= 1

    def test_the_slot_is_the_ordinary_size_and_the_position_ceiling_cannot_bind_at_ten_slots(
        self,
    ) -> None:
        sizing = DEFAULT_TWT_CONFIG.sizing
        assert Decimal(1) / Decimal(sizing.max_slots) < sizing.max_position_pct / Decimal(100)
        trade = run(one_name()).trades[0]
        assert trade.cap is SizeCap.SLOT
        assert trade.quantity == int(SLEEVE / Decimal(10) / (Decimal(100) * Decimal("1.0025")))

    def test_a_thin_name_is_sized_by_one_percent_of_its_turnover(self) -> None:
        panel = book(
            count=6,
            names={"AAA": bars(count=6)},
            signals={"AAA": [True] + [False] * 5},
            turnover_avg={"AAA": 2_000_000.0},
        )
        trade = run(panel).trades[0]
        assert trade.cap is SizeCap.TURNOVER
        assert trade.quantity == int(Decimal("20000") / (Decimal(100) * Decimal("1.0025")))

    def test_a_line_that_cannot_clear_the_minimum_trade_value_is_refused_not_shrunk(self) -> None:
        panel = book(
            count=6,
            names={"AAA": bars(count=6)},
            signals={"AAA": [True] + [False] * 5},
            turnover_avg={"AAA": 500_000.0},
        )
        result = run(panel)
        assert result.trades == ()
        assert result.skipped["turnover"] == 1

    def test_an_empty_sleeve_is_a_cash_refusal_and_not_a_liquidity_one(self) -> None:
        result = run(one_name(), sleeve_inr=Decimal("5000"))
        assert result.trades == ()
        assert result.skipped["cash"] == 1

    def test_a_name_locked_at_the_open_is_skipped_rather_than_filled(self) -> None:
        # open == high == low on the fill session: no fill is possible at a price anyone accepts.
        panel = one_name(open={1: 120.0}, high={1: 120.0}, low={1: 120.0}, close={1: 120.0})
        result = run(panel)
        assert result.trades == ()
        assert result.skipped["locked"] == 1

    def test_a_name_with_no_bar_on_the_fill_session_is_skipped(self) -> None:
        blank: dict[int, float | None] = {1: None}
        panel = one_name(open=blank, high=blank, low=blank, close=blank)
        result = run(panel)
        assert result.trades == ()
        assert result.skipped["no_bar"] == 1

    def test_cash_binds_once_the_book_has_spent_itself(self) -> None:
        """``04`` §6.2's last budget: nine positions in, and the tenth is bought with what is left.

        The slot is a tenth of **equity**, which includes what the nine are now worth; the tenth
        line can only be bought with **cash**. Doubling the book makes the two diverge, which is
        the only state in which the cash budget is the binding one.
        """
        names = {f"N{index:02d}": bars(count=10) for index in range(10)}
        for series in names.values():
            for col in range(4, 10):
                for field in ("open", "high", "close"):
                    series[field][col] = 200.0
                series["low"][col] = 198.0
        order = sorted(names)
        signals = {name: [False] * 10 for name in order}
        for index, name in enumerate(order[:9]):
            signals[name][index // 3] = True
        signals[order[9]][4] = True

        result = run(book(count=10, names=names, signals=signals))
        trades = {trade.symbol: trade for trade in result.trades}
        assert len(trades) == 10
        assert trades[order[9]].cap is SizeCap.CASH
        assert {trades[name].cap for name in order[:9]} == {SizeCap.SLOT}


class TestTheRunEndsHonestly:
    """``04`` §11.6 — what is still open when the history stops is counted, not hidden."""

    def test_an_open_position_is_liquidated_at_the_last_close_and_counted(self) -> None:
        result = run(one_name())
        assert len(result.trades) == 1
        assert result.trades[0].reason is ExitReason.END_OF_RUN
        assert result.trades[0].exit_date == result.sessions[-1]

    def test_the_final_equity_is_all_cash(self) -> None:
        result = run(one_name())
        assert result.equity[-1] == sum((trade.pnl_inr for trade in result.trades), SLEEVE), (
            "cash + realised, with nothing left marked"
        )


class TestNoLookAhead:
    """House rule 5, at the engine's own level."""

    def rolling(self) -> Panel:
        names = {f"N{index}": bars(count=20, price=100.0 + index) for index in range(4)}
        return book(
            count=20,
            names=names,
            signals={
                name: [col == index * 4 for col in range(20)]
                for index, name in enumerate(sorted(names))
            },
        )

    @pytest.mark.parametrize("cut", [6, 10, 14, 18])
    def test_truncating_the_panel_changes_no_trade_that_had_already_closed(self, cut: int) -> None:
        panel = self.rolling()
        whole = run(panel)
        part = run(panel, end=panel.sessions[cut])
        boundary = panel.sessions[cut]
        before = {t.symbol: t for t in whole.trades if t.exit_date < boundary}
        theirs = {t.symbol: t for t in part.trades if t.exit_date < boundary}
        assert before.keys() == theirs.keys()
        assert all(before[name] == theirs[name] for name in before)

    def test_the_size_reads_the_signal_sessions_turnover_and_not_the_fill_sessions(self) -> None:
        panel = book(
            count=6,
            names={"AAA": bars(count=6)},
            signals={"AAA": [True] + [False] * 5},
            turnover_avg={"AAA": 2_000_000.0},
        )
        panel.turnover_avg_20[0, 1] = DEEP  # a fill-session reading the book must not see
        trade = run(panel).trades[0]
        assert trade.cap is SizeCap.TURNOVER

    def test_the_rank_reads_the_signal_sessions_turnover(self) -> None:
        one_a_session = replace(
            DEFAULT_TWT_CONFIG,
            sizing=replace(DEFAULT_TWT_CONFIG.sizing, max_new_entries_per_session=1),
        )
        panel = self_rank_panel()
        panel.turnover_inr[:, 1] = np.array([9.0e12, 1.0])  # the fill session, reversed
        taken = [trade.symbol for trade in run(panel, config=one_a_session).trades]
        assert taken == ["BBB"], "BBB out-turned AAA on the signal session"


def self_rank_panel() -> Panel:
    return book(
        count=6,
        names={"AAA": bars(count=6), "BBB": bars(count=6)},
        signals={"AAA": [True] + [False] * 5, "BBB": [True] + [False] * 5},
        rank={"AAA": 1.0, "BBB": 9.0e12},
    )


class TestTheEngineRunsOnThePlantsShape:
    """``06`` § TW9 — the same engine, the plant's bars, a different ``BacktestParams``."""

    def test_every_panel_column_is_one_the_sleeve_itself_computes(self) -> None:
        produced = set(REQUIRED_COLUMNS) | set(INDICATOR_COLUMNS) | set(SIGNAL_COLUMNS)
        assert set(PANEL_COLUMNS) <= produced

    def test_a_frame_built_from_bare_ohlcv_runs_end_to_end(self) -> None:
        raw = flat_bars(count=300)
        detected = with_twt_columns(raw, calendar_for(raw), DEFAULT_TWT_CONFIG)
        tagged = detected.with_columns(signal_mask(detected, DEFAULT_TWT_CONFIG).alias("signal"))
        panel = panel_from_frame(tagged, "signal")
        result = run_backtest(panel, all_open(panel), BacktestParams())
        assert result.sessions[0] == panel.sessions[0]
        assert len(result.equity) == panel.sessions_count

    def test_the_defaults_are_the_shipped_sleeves_and_not_the_studys(self) -> None:
        default = BacktestParams()
        assert default.tick == Decimal(TICK_INR), "the exchange's ₹0.05, not the study's paisa"
        assert default.config.entry.min_turnover_inr == Decimal("50000000")
        assert default.sleeve_inr == DEFAULT_TWT_CONFIG.backtest.initial_capital_inr
        assert default.cost_pct_per_side == DEFAULT_TWT_CONFIG.costs.cost_bps_per_side / 100

    def test_the_studys_tick_and_floor_reach_the_engine_only_as_parameters(self) -> None:
        assert Decimal(RESEARCH_TICK_INR) != Decimal(TICK_INR)
        assert params(tick=Decimal(RESEARCH_TICK_INR)).tick == Decimal("0.01")

    def test_the_window_is_a_parameter_and_the_warm_up_is_never_traded(self) -> None:
        panel = one_name(count=10, signal_on=0)
        result = run(panel, start=panel.sessions[4])
        assert result.sessions[0] == panel.sessions[4]
        assert result.trades == (), "the signal was before the window opened"

    def test_a_start_that_is_not_a_session_lands_on_the_next_one(self) -> None:
        panel = one_name(count=10)
        monday = panel.sessions[5]
        assert monday.weekday() == 0
        sunday = monday - dt.timedelta(days=1)
        assert sunday not in panel.sessions
        assert run(panel, start=sunday).sessions[0] == monday


class TestTheStatisticsSayWhatTheyMeasure:
    """``04`` §12 — computed once, so a page and a test cannot disagree."""

    def test_a_window_with_no_curve_is_None_and_not_a_record_of_zeros(self) -> None:
        empty = BacktestResult(
            params=params(), trades=(), sessions=(), equity=(), open_positions=()
        )
        assert summarise(empty) is None

    def test_the_exit_reasons_are_counted_by_name(self) -> None:
        stats = summarise(run(one_name()))
        assert stats is not None
        assert stats.by_reason == {ExitReason.END_OF_RUN.value: 1}

    def test_a_book_with_no_loser_has_no_profit_factor_rather_than_an_infinity(self) -> None:
        panel = one_name(count=6, close={5: 400.0}, high={5: 400.0}, open={5: 400.0})
        stats = summarise(run(panel))
        assert stats is not None
        assert stats.win_rate_pct == 100.0
        assert stats.profit_factor is None

    def test_exposure_is_average_open_positions_against_the_slot_count(self) -> None:
        stats = summarise(run(one_name(count=10)))
        assert stats is not None
        assert stats.avg_open_positions * Decimal(100) / Decimal(
            DEFAULT_TWT_CONFIG.sizing.max_slots
        ) == Decimal(str(stats.exposure_pct))

    def test_the_statistics_serialise_without_a_float_touching_the_money(self) -> None:
        stats = summarise(run(one_name()))
        assert stats is not None
        stored = stats.to_json()
        assert isinstance(stored["final_equity_inr"], str)
        assert isinstance(stored["avg_hold_sessions"], str)

    def test_a_calendar_year_row_exists_for_every_year_the_curve_covers(self) -> None:
        rows = yearly(run(one_name(count=10)))
        assert [row.year for row in rows] == [2024]
        assert rows[0].trades == 1


class TestTheConfigurationIsTheContract:
    """A run must not be able to quietly use a threshold nobody wrote down."""

    def test_a_recalibrated_config_changes_the_run_rather_than_being_ignored(self) -> None:
        tighter = TwtConfig(exits=replace(DEFAULT_TWT_CONFIG.exits, stop_pct=Decimal("10.0")))
        panel = one_name(low={2: 88.0})
        assert run(panel).trades[0].reason is ExitReason.END_OF_RUN
        assert run(panel, config=tighter).trades[0].reason is ExitReason.STOP_HIT

    def test_the_cost_is_charged_on_both_legs(self) -> None:
        trade = run(one_name(), cost_pct_per_side=Decimal("0")).trades[0]
        assert trade.entry_price == Decimal("100")
        assert trade.pnl_inr == Decimal(0), "a flat name at zero cost makes exactly nothing"
