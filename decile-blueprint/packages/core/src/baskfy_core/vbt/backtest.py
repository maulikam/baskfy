"""The study, re-run by the book's own functions (``docs/vbt/04`` §11).

VB2 reproduces ``research/volume-breakout/STRATEGY.md`` §4 from the same bars; VB9 re-runs it
from the plant's bars and flags drift. **Both walk the same functions the live sleeve walks** —
:func:`~baskfy_core.vbt.signals.detect_signals`, :func:`~baskfy_core.vbt.breadth.breadth_series`,
:func:`~baskfy_core.vbt.sizing.size_entry`, :func:`~baskfy_core.vbt.exits.manage` — so the
backtest cannot drift from the book by construction. What this module adds is the **sequencing**,
and the sequencing is the part that is easy to get subtly wrong.

The session, in order (``04`` §11):

1. **Exits at the open.** A queued EMA exit fills at the open. Otherwise, if the open is at or
   below the stop the stop fills at the open; else if the low is at or below it, at the stop. A
   name that has not printed for ``no_bar_tolerance_sessions`` is written off at its last close.
2. **Working orders.** Yesterday's signals become working orders; orders past their window
   expire. Both happen **whether or not the gate is open** — the gate governs fills, not
   bookkeeping.
3. **Fills**, only if the gate was OPEN at the previous session's close, ranked by signal-day
   turnover, subject to §5's caps and at most three a session.
4. **The same session's low can take a fresh entry out** — conservative: the stop is checked
   before any favourable move is counted.
5. **At the close**, mark equity and queue tomorrow's EMA exit for every position below its
   21-day EMA.
6. At the end, liquidate what is open at the last close. Those ten trades are **counted**: the
   study's 761 includes them, and dropping them would flatter the win rate by hiding the
   positions the book was still carrying when the history ran out.

Money is ``Decimal`` (house rule 9). Prices arrive as ``float64`` matrices because that is what a
bar is; every rupee derived from one is a ``Decimal`` from the moment it is money.

Pure: numpy and Decimal, no database, no network, no disk, no clock.
"""

from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_left
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from itertools import pairwise

import numpy as np
import polars as pl

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, Gate, VbtConfig
from baskfy_core.vbt.exits import ExitReason
from baskfy_core.vbt.sizing import SizeCap

PAISE = Decimal("0.01")
_ZERO = Decimal(0)
_ONE = Decimal(1)
_HUNDRED = Decimal(100)
#: Sessions in an NSE trading year, for the daily Sharpe's annualisation.
SESSIONS_A_YEAR = 252
#: A curve needs two points before it has a return.
_TWO_POINTS = 2

#: Days in an average year including the leap, for CAGR.
DAYS_A_YEAR = Decimal("365.25")

#: The columns :func:`panel_from_frame` reads off an indicated frame.
PANEL_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "ema_exit",
    "turnover_avg",
    "turnover_inr",
)


@dataclass(frozen=True, slots=True)
class Panel:
    """The history as matrices: rows are instruments, columns are sessions.

    A missing bar is ``NaN`` and is **never** forward-filled into a fill price. This is the same
    layout the research used, and it is the layout the sequencing above needs: "the session after
    the signal" has to mean the same thing for every name at once.
    """

    symbols: tuple[str, ...]
    instrument_ids: tuple[int, ...]
    sessions: tuple[dt.date, ...]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    ema_exit: np.ndarray
    turnover_avg: np.ndarray
    turnover_inr: np.ndarray
    signal: np.ndarray

    @property
    def instruments(self) -> int:
        return len(self.symbols)

    @property
    def sessions_count(self) -> int:
        return len(self.sessions)

    def column_of(self, session: dt.date) -> int:
        """The column at or after ``session``. Bisecting a tuple of dates, not a numpy array:
        ``searchsorted`` over ``dtype=object`` works but is neither typed nor faster here."""
        return bisect_left(self.sessions, session)


def panel_from_frame(indicated: pl.DataFrame, signal_column: str = "state") -> Panel:
    """Build a :class:`Panel` from an indicated, signal-tagged frame.

    ``signal_column`` may be the ``state`` column of
    :func:`~baskfy_core.vbt.signals.with_signal_columns` (``SIGNAL`` / ``SCAN_ONLY`` / null) or a
    boolean column. Both readings exist because VB2 wants to run the raw Chartink scan through
    the same engine, which is one of the three books ``04`` §11 reports.
    """
    frame = indicated.sort(["instrument_id", "date"])
    ids = frame["instrument_id"].unique(maintain_order=False).sort().to_list()
    sessions = frame["date"].unique(maintain_order=False).sort().to_list()
    row_of = {value: index for index, value in enumerate(ids)}
    col_of = {value: index for index, value in enumerate(sessions)}
    rows = np.array([row_of[value] for value in frame["instrument_id"].to_list()], dtype=np.int64)
    cols = np.array([col_of[value] for value in frame["date"].to_list()], dtype=np.int64)
    shape = (len(ids), len(sessions))

    def matrix(name: str) -> np.ndarray:
        out = np.full(shape, np.nan)
        if name in frame.columns:
            out[rows, cols] = frame[name].cast(pl.Float64, strict=False).to_numpy()
        return out

    signal = np.zeros(shape, dtype=bool)
    if signal_column in frame.columns:
        series = frame[signal_column]
        flags = (
            series.fill_null(value=False).to_numpy()
            if series.dtype == pl.Boolean
            else (series == "SIGNAL").fill_null(value=False).to_numpy()
        )
        signal[rows, cols] = flags

    symbols = ["" for _ in ids]
    if "symbol" in frame.columns:
        named = (
            frame.filter(pl.col("symbol").is_not_null())
            .group_by("instrument_id")
            .agg(pl.col("symbol").first())
        )
        for record in named.iter_rows(named=True):
            symbols[row_of[record["instrument_id"]]] = str(record["symbol"])

    return Panel(
        symbols=tuple(symbols),
        instrument_ids=tuple(int(value) for value in ids),
        sessions=tuple(sessions),
        signal=signal,
        **{name: matrix(name) for name in PANEL_COLUMNS},
    )


@dataclass(frozen=True, slots=True)
class BacktestParams:
    """What a run was asked for. Stored on the way **in**, so a run that failed still says."""

    sleeve_inr: Decimal = Decimal("1000000")
    #: The first session the book may trade. Everything before it is warm-up: the 200-day average
    #: does not exist, and a book that traded through its own warm-up would be reporting a
    #: different strategy.
    start: dt.date | None = None
    end: dt.date | None = None
    #: ``04`` §8 — the backtest's own cost, a side. A live fill's cost is the broker's.
    cost_pct_per_side: Decimal = Decimal("0.25")
    #: The tick prices are floored and rounded to. The study used the **paise**; the desk snaps
    #: the levels it sends to ₹0.05 (``04`` §7.1). Reproducing the study means using its tick.
    tick: Decimal = PAISE
    config: VbtConfig = DEFAULT_VBT_CONFIG
    label: str = "full"


DEFAULT_BACKTEST_PARAMS = BacktestParams()


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    """One round trip. ``reason`` is an :class:`~baskfy_core.vbt.exits.ExitReason`."""

    symbol: str
    instrument_id: int
    entry_date: dt.date
    exit_date: dt.date
    entry_price: Decimal
    exit_price: Decimal
    quantity: int
    pnl_inr: Decimal
    return_pct: Decimal
    r_multiple: Decimal | None
    hold_sessions: int
    reason: ExitReason
    cap: SizeCap | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    params: BacktestParams
    trades: tuple[BacktestTrade, ...]
    sessions: tuple[dt.date, ...]
    equity: tuple[Decimal, ...]
    open_positions: tuple[int, ...]
    skipped: dict[str, int] = field(default_factory=dict)

    def stats(self) -> dict[str, object]:
        return summarise(self)


@dataclass(slots=True)
class _Position:
    row: int
    quantity: int
    entry: Decimal
    entry_col: int
    stop: Decimal
    initial_stop: Decimal
    cap: SizeCap | None
    queued_exit: ExitReason | None = None


@dataclass(slots=True)
class _Working:
    row: int
    signal_col: int
    limit: Decimal


def _dec(value: float) -> Decimal:
    """A price as a Decimal, exactly. ``Decimal(float)`` is lossless — no string round-trip."""
    return Decimal(value)


def _floor_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def _round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """Half-even, matching Python's own ``round`` — which is what the study used."""
    return (value / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick


def run_backtest(  # noqa: PLR0912, PLR0915 - the sequencing is the module, and splitting
    #   it into helpers that each need the whole book's state would hide the order, which is the
    #   one thing `04` §11 is about.
    panel: Panel,
    gate_open: np.ndarray,
    params: BacktestParams = DEFAULT_BACKTEST_PARAMS,
) -> BacktestResult:
    """Walk the history one session at a time and return every trade and the equity curve.

    ``gate_open`` is one boolean per session: may the book open new positions at the **next**
    session, as read at this session's close. Passing all-``True`` gives ``04`` §11's ``gate_off``
    book, which is how breadth's contribution is measured.
    """
    config = params.config
    sizing, exits_config, entry_config = config.sizing, config.exits, config.entry
    cost = params.cost_pct_per_side / _HUNDRED
    tick = params.tick
    stop_fraction = _ONE - Decimal(str(exits_config.stop_pct)) / _HUNDRED
    fill_through = Decimal(str(entry_config.fill_through_pct))
    slot_divisor = Decimal(sizing.max_slots)
    position_fraction = Decimal(str(sizing.max_position_pct)) / _HUNDRED
    turnover_fraction = Decimal(str(sizing.max_position_vs_turnover))
    min_trade = Decimal(str(sizing.min_trade_value_inr))

    total_sessions = panel.sessions_count
    first = 0 if params.start is None else panel.column_of(params.start)
    last = total_sessions - 1 if params.end is None else panel.column_of(params.end)

    cash = params.sleeve_inr
    positions: dict[int, _Position] = {}
    working: dict[int, _Working] = {}
    trades: list[BacktestTrade] = []
    equity = [Decimal(0)] * total_sessions
    open_count = [0] * total_sessions
    skipped = {
        "expired": 0,
        "session_cap": 0,
        "slots_full": 0,
        "cash": 0,
        "turnover": 0,
        "gate_shut": 0,
        "no_bar": 0,
        "locked": 0,
    }

    def close_position(position: _Position, col: int, price: Decimal, reason: ExitReason) -> None:
        nonlocal cash
        price = _round_to_tick(price, tick)
        quantity = position.quantity
        proceeds = price * quantity * (_ONE - cost)
        cash += proceeds
        pnl = proceeds - position.entry * quantity
        basis = position.entry * quantity
        risk = position.entry - position.initial_stop
        trades.append(
            BacktestTrade(
                symbol=panel.symbols[position.row],
                instrument_id=panel.instrument_ids[position.row],
                entry_date=panel.sessions[position.entry_col],
                exit_date=panel.sessions[col],
                entry_price=position.entry,
                exit_price=price,
                quantity=quantity,
                pnl_inr=pnl,
                return_pct=(pnl / basis * _HUNDRED) if basis else _ZERO,
                r_multiple=(
                    ((pnl / basis) / (risk / position.entry)) if risk > _ZERO and basis else None
                ),
                hold_sessions=col - position.entry_col,
                reason=reason,
                cap=position.cap,
            )
        )
        del positions[position.row]

    for col in range(first, last + 1):
        # --- 1. exits at the open -------------------------------------------------------
        for row in list(positions):
            position = positions[row]
            open_price = panel.open[row, col]
            if not math.isfinite(open_price):
                previous = col - 1
                while previous > position.entry_col and not math.isfinite(
                    panel.close[row, previous]
                ):
                    previous -= 1
                if col - previous >= exits_config.no_bar_tolerance_sessions:
                    mark = panel.close[row, previous]
                    price = _dec(mark) if math.isfinite(mark) else position.stop
                    close_position(position, col, price, ExitReason.NO_BAR)
                continue
            if position.queued_exit is not None:
                close_position(position, col, _dec(open_price), position.queued_exit)
                continue
            if _dec(open_price) <= position.stop:
                close_position(position, col, _dec(open_price), ExitReason.STOP_GAP)
                continue
            low = panel.low[row, col]
            if math.isfinite(low) and _dec(low) <= position.stop:
                close_position(position, col, position.stop, ExitReason.STOP_HIT)
                continue

        # --- 2. working orders: yesterday's signals in, stale ones out --------------------
        if col >= 1:
            for row in np.nonzero(panel.signal[:, col - 1])[0]:
                key = int(row)
                if key not in positions and key not in working:
                    close_yesterday = panel.close[key, col - 1]
                    if math.isfinite(close_yesterday):
                        working[key] = _Working(key, col - 1, _dec(close_yesterday))
            for key in [
                key
                for key, order in working.items()
                if col - order.signal_col > entry_config.valid_sessions
            ]:
                skipped["expired"] += 1
                del working[key]

        # --- 3. fills, if the gate was open at yesterday's close --------------------------
        if col >= 1 and bool(gate_open[col - 1]):
            candidates = [key for key in working if key not in positions]
            if candidates:
                candidates.sort(
                    key=lambda key: -_rank_of(panel, key, working[key].signal_col),
                )
                marked = _mark_equity(panel, positions, cash, col - 1)
                taken = 0
                for row in candidates:
                    if taken >= sizing.max_new_entries_per_session:
                        skipped["session_cap"] += 1
                        break
                    if len(positions) >= sizing.max_slots:
                        skipped["slots_full"] += 1
                        break
                    open_price = panel.open[row, col]
                    high = panel.high[row, col]
                    low = panel.low[row, col]
                    if not math.isfinite(open_price) or open_price <= 0:
                        skipped["no_bar"] += 1
                        continue
                    if open_price == high == low:
                        skipped["locked"] += 1
                        continue
                    order = working[row]
                    if not math.isfinite(low) or _dec(low) > _fill_threshold(
                        order.limit, fill_through
                    ):
                        continue
                    fill = min(_dec(open_price), order.limit)
                    del working[row]

                    stop = _floor_to_tick(fill * stop_fraction, tick)
                    if stop >= fill:
                        stop = _floor_to_tick(fill * stop_fraction, tick)
                    entry_price = fill * (_ONE + cost)
                    budget, cap = _budget(
                        marked,
                        cash,
                        panel.turnover_avg[row, col - 1],
                        slot_divisor,
                        position_fraction,
                        turnover_fraction,
                    )
                    quantity = int((budget / entry_price).to_integral_value(rounding=ROUND_DOWN))
                    if quantity * entry_price < min_trade:
                        skipped["cash" if cash < min_trade else "turnover"] += 1
                        continue
                    cash -= quantity * entry_price
                    positions[row] = _Position(
                        row=row,
                        quantity=quantity,
                        entry=entry_price,
                        entry_col=col,
                        stop=stop,
                        initial_stop=stop,
                        cap=cap,
                    )
                    taken += 1
        elif col >= 1:
            skipped["gate_shut"] += int(panel.signal[:, col - 1].sum())

        # --- 4/5. the fresh entry's own stop, then the close -----------------------------
        for row in list(positions):
            position = positions[row]
            close = panel.close[row, col]
            if not math.isfinite(close):
                continue
            if position.entry_col == col:
                low = panel.low[row, col]
                if math.isfinite(low) and _dec(low) <= position.stop:
                    open_price = _dec(panel.open[row, col])
                    price = position.stop if open_price > position.stop else open_price
                    close_position(position, col, price, ExitReason.STOP_HIT)
                    continue
            ema = panel.ema_exit[row, col]
            if math.isfinite(ema) and _dec(close) < _dec(ema):
                position.queued_exit = ExitReason.EMA_EXIT

        equity[col] = _mark_equity(panel, positions, cash, col)
        open_count[col] = len(positions)

    for row in list(positions):
        position = positions[row]
        mark = panel.close[row, last]
        price = _dec(mark) if math.isfinite(mark) else position.entry
        close_position(position, last, price, ExitReason.END_OF_RUN)
    equity[last] = cash

    return BacktestResult(
        params=params,
        trades=tuple(trades),
        sessions=tuple(panel.sessions[first : last + 1]),
        equity=tuple(equity[first : last + 1]),
        open_positions=tuple(open_count[first : last + 1]),
        skipped=skipped,
    )


def _fill_threshold(limit: Decimal, fill_through_pct: Decimal) -> Decimal:
    """The price the session's low must reach for the limit to be considered filled.

    **With no through-requirement it is the limit itself, and not ``limit x 1``.** A bar price
    converted from a ``float`` carries about fifty significant digits of exact binary expansion;
    Decimal multiplication rounds its result to the context's twenty-eight. So a limit that a
    low touched *exactly* — the same price, the same bit pattern, which happens whenever
    yesterday's close is today's low — comes back from a needless multiplication a hair **above**
    that low, and the order never fills. The study's float arithmetic has no such step and does
    fill it; this is the one place where "multiply by one" is not a no-op.
    """
    if fill_through_pct == _ZERO:
        return limit
    return limit * (_ONE - fill_through_pct / _HUNDRED)


def _rank_of(panel: Panel, row: int, signal_col: int) -> float:
    """``04`` §3.4 — the signal-day rupee turnover. A NaN ranks last, never first."""
    value = panel.turnover_inr[row, signal_col]
    return float(value) if math.isfinite(value) else -math.inf


def _mark_equity(panel: Panel, positions: dict[int, _Position], cash: Decimal, col: int) -> Decimal:
    """Cash plus every open position marked at ``col``'s close, or at its entry if it has none."""
    total = cash
    for position in positions.values():
        mark = panel.close[position.row, col]
        price = _dec(mark) if math.isfinite(mark) else position.entry
        total += price * position.quantity
    return total


def _budget(  # noqa: PLR0913, PLR0917 - the four budgets and what they are measured against
    equity: Decimal,
    cash: Decimal,
    turnover_avg: float,
    slot_divisor: Decimal,
    position_fraction: Decimal,
    turnover_fraction: Decimal,
) -> tuple[Decimal, SizeCap | None]:
    """``04`` §5.2's four budgets, smallest wins, and which one it was.

    The turnover reading is the **previous session's** 20-day average — the last one a book
    sizing at the moment of the fill could have seen. The evening plan reads the signal day's,
    which for a next-session fill is the same number.
    """
    budgets: list[tuple[Decimal, SizeCap]] = [
        (equity / slot_divisor, SizeCap.SLOT),
        (equity * position_fraction, SizeCap.POSITION_PCT),
        (cash, SizeCap.CASH),
    ]
    if math.isfinite(turnover_avg):
        budgets.append((_dec(turnover_avg) * turnover_fraction, SizeCap.TURNOVER))
    return min(budgets, key=lambda pair: pair[0])


def summarise(result: BacktestResult) -> dict[str, object]:
    """``04`` §11's reported statistics, computed once so a page and a test cannot disagree."""
    curve = [value for value in result.equity if value != _ZERO]
    if len(curve) < _TWO_POINTS or not result.sessions:
        return {"trades": 0}
    start, end = result.sessions[0], result.sessions[-1]
    years = Decimal((end - start).days) / DAYS_A_YEAR
    total = curve[-1] / curve[0]
    cagr = (
        (Decimal(str(float(total) ** (1 / float(years)))) - _ONE) * _HUNDRED
        if years > _ZERO
        else _ZERO
    )
    peak, drawdown, trough_at, peak_at = curve[0], _ZERO, start, start
    running_peak_at = start
    for session, value in zip(result.sessions, result.equity, strict=True):
        if value == _ZERO:
            continue
        if value > peak:
            peak, running_peak_at = value, session
        fall = (value / peak - _ONE) * _HUNDRED
        if fall < drawdown:
            drawdown, trough_at, peak_at = fall, session, running_peak_at
    returns = [
        float(later / earlier - _ONE) for earlier, later in pairwise(curve) if earlier != _ZERO
    ]
    sharpe = (
        float(np.mean(returns)) / float(np.std(returns, ddof=1)) * math.sqrt(SESSIONS_A_YEAR)
        if len(returns) > 1 and float(np.std(returns, ddof=1)) > 0
        else float("nan")
    )
    wins = [trade for trade in result.trades if trade.pnl_inr > _ZERO]
    losses = [trade for trade in result.trades if trade.pnl_inr <= _ZERO]
    gross_win = sum((trade.pnl_inr for trade in wins), _ZERO)
    gross_loss = -sum((trade.pnl_inr for trade in losses), _ZERO)
    count = len(result.trades)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "years": float(years),
        "final_equity_inr": str(curve[-1].quantize(PAISE)),
        "cagr_pct": float(cagr),
        "max_drawdown_pct": float(drawdown),
        "drawdown_peak_on": peak_at.isoformat(),
        "drawdown_trough_on": trough_at.isoformat(),
        "calmar": float(cagr / abs(drawdown)) if drawdown < _ZERO else None,
        "sharpe": sharpe,
        "trades": count,
        "win_rate_pct": (len(wins) / count * 100) if count else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > _ZERO else None,
        "avg_win_pct": float(sum((t.return_pct for t in wins), _ZERO) / len(wins)) if wins else 0.0,
        "avg_loss_pct": (
            float(sum((t.return_pct for t in losses), _ZERO) / len(losses)) if losses else 0.0
        ),
        "avg_trade_pct": (
            float(sum((t.return_pct for t in result.trades), _ZERO) / count) if count else 0.0
        ),
        "avg_hold_sessions": (
            sum(trade.hold_sessions for trade in result.trades) / count if count else 0.0
        ),
        "avg_open_positions": (
            sum(result.open_positions) / len(result.open_positions)
            if result.open_positions
            else 0.0
        ),
        "exposure_pct": (
            sum(result.open_positions)
            / len(result.open_positions)
            / result.params.config.sizing.max_slots
            * 100
            if result.open_positions
            else 0.0
        ),
        "by_reason": {
            reason.value: sum(1 for trade in result.trades if trade.reason is reason)
            for reason in ExitReason
            if any(trade.reason is reason for trade in result.trades)
        },
    }


def gate_vector(breadth: pl.DataFrame, sessions: tuple[dt.date, ...]) -> np.ndarray:
    """``gate`` per session, aligned to the panel's columns. A session with no row is SHUT."""
    lookup = {
        record["date"]: record["gate"] == Gate.OPEN.value
        for record in breadth.iter_rows(named=True)
    }
    return np.array([lookup.get(session, False) for session in sessions], dtype=bool)


def yearly(result: BacktestResult) -> list[dict[str, object]]:
    """Calendar-year returns off the equity curve, and the trades that closed in each."""
    by_year: dict[int, list[Decimal]] = {}
    for session, value in zip(result.sessions, result.equity, strict=True):
        if value != _ZERO:
            by_year.setdefault(session.year, []).append(value)
    opening = result.equity[0]
    out: list[dict[str, object]] = []
    for year in sorted(by_year):
        values = by_year[year]
        closing = values[-1]
        closed = [trade for trade in result.trades if trade.exit_date.year == year]
        wins = sum(1 for trade in closed if trade.pnl_inr > _ZERO)
        out.append(
            {
                "year": year,
                "return_pct": float((closing / opening - _ONE) * _HUNDRED) if opening else 0.0,
                "trades": len(closed),
                "win_rate_pct": (wins / len(closed) * 100) if closed else 0.0,
            }
        )
        opening = closing
    return out
