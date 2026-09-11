"""The study, re-run by the sleeve's own functions (``docs/twt/04`` §11).

TW2 reproduces ``research/tight-close/STRATEGY.md`` §4 from the bars the study itself ran on; TW9
re-runs the same engine over the plant's bars and names the drift. **Both walk the functions the
live sleeve walks** — :func:`~baskfy_core.twt.signals.with_twt_columns`,
:func:`~baskfy_core.twt.breadth.breadth_series`, :func:`~baskfy_core.twt.sizing.size_entry`,
:func:`~baskfy_core.twt.exits.stop_fill`, :func:`~baskfy_core.twt.exits.fill_day_stop` and
:func:`~baskfy_core.twt.exits.ratchet` — so the backtest cannot drift from the book by
construction. What this module adds is the **sequencing**, and the sequencing is the part that is
easy to get subtly wrong.

The session, in order (``04`` §11)
----------------------------------
1. **Exits at the open.** A name that has not printed for ``no_bar_sessions`` [5] sessions is
   written off at its last close. Otherwise, if the open is at or below the stop the stop fills at
   the **open** (it was never a price the market offered that morning); else if the low is at or
   below it, at the **stop**. :func:`~baskfy_core.twt.exits.stop_fill` is that rule and this
   module only sequences it. **There is no queued end-of-day exit** — TWT-1 has no EMA rule, no
   time stop and no state exit, which is why :class:`~baskfy_core.twt.exits.Action` has no fourth
   member.
2. **Fills at the open**, only if the gate was OPEN at the **previous** session's close, ranked by
   the signal session's own rupee turnover (``04`` §6.3, DECISIONS-TW TW0.2), at most
   ``max_new_entries_per_session`` [3] a session and ``max_slots`` [10] open at once. The initial
   stop comes from :func:`~baskfy_core.twt.exits.initial_stop` off the **bare open**; the size
   comes from :func:`~baskfy_core.twt.sizing.size_entry` against the **cost-inclusive** price the
   book actually pays (``04`` §5.3, DECISIONS-TW TW1.3).
3. **The same session's low can take a fresh entry out** — ``04`` §7.4's fill-day rule,
   conservative: the stop is checked before any favourable move is counted.
4. **At the close the trail ratchets** (``04`` §7.2) and equity is marked.
5. At the end, liquidate whatever is open at the last session's close. Those trades are
   **counted**: the study's 164 includes its ten, and dropping them would flatter the win rate by
   hiding the positions the book was still carrying when the history ran out.

Money, and why the reproduction is exact
----------------------------------------
Money and every price level are :class:`~decimal.Decimal` (house rule 9). Prices arrive as a
``float64`` matrix because that is what a panel of bars is, and :func:`_dec` converts one at the
boundary **through its shortest repr** — ``Decimal(str(x))``, not ``Decimal(x)``. That is not a
convenience. ``Decimal(x)`` preserves the binary noise of the double, so a bar that means ₹100.05
becomes ``100.0499999999999971578…`` and a level floored to the paisa comes out one paisa low
whenever the intended product lands **on** a paisa — which for a 20 % stop is one entry in five.
The research simulator hides the same noise behind an epsilon
(``_tick(x) = floor(x * 100 + 1e-9) / 100``); reading the float as the decimal it prints as is the
same correction made honestly, and it is what lets TW2's goldens be a tick comparison rather than
an approximation. DECISIONS-TW **TW2.9**.

Pure: numpy, polars and ``Decimal``. No database, no network, no disk, no clock (law 1).
"""

from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_left
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise

import numpy as np
import numpy.typing as npt
import polars as pl

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TICK_INR, Gate, TwtConfig
from baskfy_core.twt.exits import (
    Bar,
    ExitReason,
    OpenPosition,
    fill_day_stop,
    initial_stop,
    ratchet,
    stop_fill,
)
from baskfy_core.twt.sizing import SizeCap, SizeRefusal, size_entry

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HUNDRED = Decimal(100)
#: The smallest money the study's own reporting resolves, and what a final equity is quantised to.
PAISE = Decimal("0.01")
#: Sessions in an NSE trading year, for the daily Sharpe's annualisation.
SESSIONS_A_YEAR = 252
#: Days in an average year including the leap, for the CAGR the study printed.
DAYS_A_YEAR = Decimal("365.25")
#: A curve needs two points before it has a return.
_TWO_POINTS = 2

#: A price, as it arrives from a panel of bars.
_Price = np.float64
_Matrix = npt.NDArray[_Price]

#: The columns :func:`panel_from_frame` reads off a frame
#: :func:`~baskfy_core.twt.signals.with_twt_columns` produced. **Every one is either a bar or an
#: indicator the sleeve computes** — there is no column here that the study's panel has and the
#: plant's ``ohlcv_daily`` lacks, which is the whole of TW9's portability (``06`` § TW9).
PANEL_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "turnover_inr",
    "turnover_avg_20",
)


@dataclass(frozen=True, slots=True)
class Panel:
    """The history as matrices: rows are instruments, columns are sessions.

    A missing bar is ``NaN`` and is **never** forward-filled into a fill price. This is the layout
    the sequencing needs: "the session after the signal" has to mean the same thing for every name
    at once, and a per-instrument list of traded bars cannot say that.
    """

    symbols: tuple[str, ...]
    instrument_ids: tuple[int, ...]
    sessions: tuple[dt.date, ...]
    open: _Matrix
    high: _Matrix
    low: _Matrix
    close: _Matrix
    turnover_inr: _Matrix
    turnover_avg_20: _Matrix
    signal: npt.NDArray[np.bool_]

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


def panel_from_frame(indicated: pl.DataFrame, signal_column: str = "entry_event") -> Panel:
    """Build a :class:`Panel` from an indicated, signal-tagged frame.

    ``signal_column`` may be a boolean column — ``entry_event``, or
    :func:`~baskfy_core.twt.signals.signal_mask`'s output joined on, which is what TW2 passes — or
    the ``signal_state`` column of :func:`~baskfy_core.twt.signals.detect_signals`, in which case
    ``SIGNAL`` is the truth and ``SCAN_ONLY`` is not. Both readings exist because ``01`` §7's
    funnel is a real thing to want to backtest: the same engine over the events the floor rejected
    is how the floor is argued for.

    A column the frame does not carry leaves the mask all-false rather than raising, and a run with
    no signal produces no trade — which is a true statement about that frame and an obvious one to
    read in the result.
    """
    frame = indicated.sort(["instrument_id", "date"])
    ids = frame["instrument_id"].unique().sort()
    dates = frame["date"].unique().sort()
    rows = np.searchsorted(ids.to_numpy(), frame["instrument_id"].to_numpy())
    cols = np.searchsorted(dates.to_numpy(), frame["date"].to_numpy())
    shape = (ids.len(), dates.len())

    def matrix(name: str) -> _Matrix:
        out: _Matrix = np.full(shape, np.nan)
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

    symbols = ["" for _ in range(ids.len())]
    if "symbol" in frame.columns:
        named = (
            frame.filter(pl.col("symbol").is_not_null())
            .group_by("instrument_id")
            .agg(pl.col("symbol").first())
        )
        position = {value: index for index, value in enumerate(ids.to_list())}
        for record in named.iter_rows(named=True):
            symbols[position[record["instrument_id"]]] = str(record["symbol"])

    return Panel(
        symbols=tuple(symbols),
        instrument_ids=tuple(int(value) for value in ids.to_list()),
        sessions=tuple(dates.to_list()),
        signal=signal,
        **{name: matrix(name) for name in PANEL_COLUMNS},
    )


@dataclass(frozen=True, slots=True)
class BacktestParams:
    """What a run was asked for. Stored on the way **in**, so a run that failed still says.

    Every number the study used differently from the shipped sleeve is a field here, and the
    **defaults are the sleeve's**: ``04``'s own capital, the exchange's ₹0.05 tick and ``04`` §8's
    25 bps. TW2 passes the study's ₹10 lakh, ₹0.01 and the same 25 bps; TW9 passes the plant's.
    A parameter set is the only difference between the two runs (``06`` § TW9).
    """

    sleeve_inr: Decimal = DEFAULT_TWT_CONFIG.backtest.initial_capital_inr
    #: The first session the book may trade. Everything before it is warm-up: the 200-session
    #: average does not exist, and a book that traded through its own warm-up would be reporting a
    #: different strategy.
    start: dt.date | None = None
    end: dt.date | None = None
    #: ``04`` §8 — the backtest's own cost, a side, as a **percent**. A live fill's cost is the
    #: broker's and is journalled, not modelled.
    cost_pct_per_side: Decimal = DEFAULT_TWT_CONFIG.costs.cost_bps_per_side / _HUNDRED
    #: The tick every level is floored and rounded to. The exchange quotes NSE cash equities in
    #: ₹0.05 and that is the default; the study floored to the paisa, and TW2 passes
    #: ``RESEARCH_TICK_INR`` (``04`` §7.1, DECISIONS-TW **TW1.1**).
    tick: Decimal = Decimal(TICK_INR)
    config: TwtConfig = DEFAULT_TWT_CONFIG
    label: str = "full"


DEFAULT_BACKTEST_PARAMS = BacktestParams()


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    """One round trip. ``reason`` is a :class:`~baskfy_core.twt.exits.ExitReason`.

    ``entry_price`` is the **cost-inclusive** price the book paid (``04`` §5.3); ``exit_price`` is
    the exchange print the exit filled at, before the sell-side cost, which is taken out of the
    proceeds. That asymmetry is the research's own and it is why ``return_pct`` is measured
    against ``entry_price x quantity`` rather than against the fill.
    """

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
    #: Which of ``04`` §6.2's budgets the quantity was actually made of.
    cap: SizeCap | None = None


@dataclass(frozen=True, slots=True)
class YearRow:
    """One calendar year of the equity curve, and the trades that closed inside it."""

    year: int
    return_pct: float
    trades: int
    win_rate_pct: float

    def to_json(self) -> dict[str, object]:
        return {
            "year": self.year,
            "return_pct": self.return_pct,
            "trades": self.trades,
            "win_rate_pct": self.win_rate_pct,
        }


@dataclass(frozen=True, slots=True)
class BacktestStats:
    """``04`` §12's reported statistics, as a typed record rather than a bag of ``object``.

    A dictionary would be shorter here and worse everywhere else: the desk page reads these, TW9's
    drift flag compares them, ``tw_backtest_run.stats`` stores them and the goldens assert them.
    Four consumers reaching into a ``dict[str, object]`` is four places that each cast, and the day
    a key is renamed none of them says so.

    Every number that is a quantity rather than a ratio stays :class:`~decimal.Decimal`, so the
    figure a page prints is the figure the arithmetic produced.
    """

    start: dt.date
    end: dt.date
    years: Decimal
    final_equity_inr: Decimal
    cagr_pct: float
    max_drawdown_pct: float
    drawdown_peak_on: dt.date
    drawdown_trough_on: dt.date
    calmar: Decimal | None
    sharpe_ratio: float
    trades: int
    win_rate_pct: float
    profit_factor: Decimal | None
    avg_win_pct: float
    avg_loss_pct: float
    avg_trade_pct: float
    avg_hold_sessions: Decimal
    avg_open_positions: Decimal
    exposure_pct: float
    by_reason: dict[str, int]

    def to_json(self) -> dict[str, object]:
        """The JSONB shape ``tw_backtest_run.stats`` stores. Money and every exact quantity as a
        string of its own decimal, dates as ISO — nothing here goes through a float on its way to
        the database."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "years": str(self.years),
            "final_equity_inr": str(self.final_equity_inr),
            "cagr_pct": self.cagr_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "drawdown_peak_on": self.drawdown_peak_on.isoformat(),
            "drawdown_trough_on": self.drawdown_trough_on.isoformat(),
            "calmar": None if self.calmar is None else str(self.calmar),
            "sharpe_ratio": self.sharpe_ratio,
            "trades": self.trades,
            "win_rate_pct": self.win_rate_pct,
            "profit_factor": None if self.profit_factor is None else str(self.profit_factor),
            "avg_win_pct": self.avg_win_pct,
            "avg_loss_pct": self.avg_loss_pct,
            "avg_trade_pct": self.avg_trade_pct,
            "avg_hold_sessions": str(self.avg_hold_sessions),
            "avg_open_positions": str(self.avg_open_positions),
            "exposure_pct": self.exposure_pct,
            "by_reason": dict(self.by_reason),
        }


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Every trade, the curve, and an honest count of what the book did **not** do."""

    params: BacktestParams
    trades: tuple[BacktestTrade, ...]
    sessions: tuple[dt.date, ...]
    equity: tuple[Decimal, ...]
    open_positions: tuple[int, ...]
    #: Why a signal did not become a position, by cause. Counted rather than inferred: a book that
    #: took eighteen entries a year out of two thousand signals is mostly a story about the caps.
    skipped: dict[str, int] = field(default_factory=dict)
    #: How many times ``04`` §7.2's clamp produced a trigger **below** the stop in force
    #: (DECISIONS-TW **TW1.4**, **TW2.12**). The backtest carries the research's expression, which
    #: can lower a stop; the *plan* refuses to. Counting it here is what keeps the difference
    #: between the two visible instead of silent.
    clamped_below_stop: int = 0

    def stats(self) -> BacktestStats | None:
        return summarise(self)


@dataclass(slots=True)
class _Position:
    row: int
    quantity: int
    entry: Decimal
    entry_col: int
    stop: Decimal
    initial_stop: Decimal
    high_since: Decimal
    cap: SizeCap | None


def _dec(printed: _Price) -> Decimal:
    """A bar price as the decimal it prints as — see this module's docstring.

    ``Decimal(str(x))`` and never ``Decimal(x)``: the second is exact about the **double** and
    therefore wrong about the **price**, and every level floored to a tick inherits the error.
    """
    return Decimal(str(printed))


def _round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """Half-even, which is what the study's ``round(x * 100) / 100`` did."""
    return (value / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick


def _bar_at(panel: Panel, row: int, col: int) -> Bar:
    """One session for one instrument, as :mod:`baskfy_core.twt.exits` takes it.

    A cell that is not finite is ``None`` — "the name did not print" — and never a zero.
    """

    def level(matrix: _Matrix) -> Decimal | None:
        value = matrix[row, col]
        return _dec(value) if math.isfinite(value) else None

    return Bar(
        session=panel.sessions[col],
        open=level(panel.open),
        high=level(panel.high),
        low=level(panel.low),
        close=level(panel.close),
    )


def _as_open_position(panel: Panel, position: _Position) -> OpenPosition:
    return OpenPosition(
        instrument_id=panel.instrument_ids[position.row],
        entry_date=panel.sessions[position.entry_col],
        fill_price=position.entry,
        quantity=position.quantity,
        stop_price=position.stop,
        initial_stop=position.initial_stop,
        high_since=position.high_since,
    )


def _rank_of(panel: Panel, row: int, signal_col: int) -> Decimal:
    """``04`` §6.3 — the signal session's own rupee turnover. A missing reading ranks **last**.

    DECISIONS-TW TW0.2: the research's prose says "20-day turnover" and the code that produced
    every number in it ranks by the signal day's own ``close_raw x volume``. The numbers are the
    fact and the prose is the stale half.
    """
    value = panel.turnover_inr[row, signal_col]
    return _dec(value) if math.isfinite(value) else Decimal("-1e30")


def _mark_equity(panel: Panel, positions: dict[int, _Position], cash: Decimal, col: int) -> Decimal:
    """Cash plus every open position marked at ``col``'s close, or at its entry if it has none."""
    total = cash
    for position in positions.values():
        mark = panel.close[position.row, col]
        price = _dec(mark) if math.isfinite(mark) else position.entry
        total += price * position.quantity
    return total


def _last_printed_close(panel: Panel, position: _Position, col: int) -> tuple[int, Decimal | None]:
    """The most recent session at or before ``col - 1`` on which the name printed, and its close.

    Walks back no further than the entry session: a position's own history starts there, and a
    blank run that reaches the entry is a name that never traded after the book bought it.
    """
    previous = col - 1
    while previous > position.entry_col and not math.isfinite(panel.close[position.row, previous]):
        previous -= 1
    mark = panel.close[position.row, previous]
    return previous, (_dec(mark) if math.isfinite(mark) else None)


def gate_vector(breadth: pl.DataFrame, sessions: tuple[dt.date, ...]) -> npt.NDArray[np.bool_]:
    """``gate`` per session, aligned to the panel's columns. **A session with no row is SHUT.**

    An absent reading is not a neutral one: ``04`` §4.3's gate is strictly-above and its zero
    denominator is SHUT, so a session the breadth series could not measure must not be the session
    on which the book opens ten positions.
    """
    lookup = {
        record["date"]: record["gate"] == Gate.OPEN.value
        for record in breadth.iter_rows(named=True)
    }
    return np.array([lookup.get(session, False) for session in sessions], dtype=bool)


def run_backtest(  # noqa: PLR0912, PLR0915 - the sequencing is the module, and splitting it into
    #   helpers that each need the whole book's state would hide the order, which is the one thing
    #   `04` §11 is about.
    panel: Panel,
    gate_open: npt.NDArray[np.bool_],
    params: BacktestParams = DEFAULT_BACKTEST_PARAMS,
) -> BacktestResult:
    """Walk the history one session at a time and return every trade and the equity curve.

    ``gate_open`` is one boolean per session: may the book open new positions at the **next**
    session, as read at this session's close. Passing all-``True`` gives the ungated book, which is
    how ``01`` §5 measures the gate's contribution (17.2 % CAGR at -43 % against 20.9 % at
    -24.7 %).
    """
    config = params.config
    sizing, exits_config = config.sizing, config.exits
    cost = params.cost_pct_per_side / _HUNDRED
    tick = params.tick

    total_sessions = panel.sessions_count
    first = 0 if params.start is None else panel.column_of(params.start)
    last = total_sessions - 1 if params.end is None else panel.column_of(params.end)

    cash = params.sleeve_inr
    positions: dict[int, _Position] = {}
    trades: list[BacktestTrade] = []
    equity = [_ZERO] * total_sessions
    open_count = [0] * total_sessions
    clamped_below_stop = 0
    skipped = {
        "gate_shut": 0,
        "session_cap": 0,
        "slots_full": 0,
        "no_bar": 0,
        "locked": 0,
        "cash": 0,
        "turnover": 0,
    }

    def close_position(position: _Position, col: int, price: Decimal, reason: ExitReason) -> None:
        nonlocal cash
        price = _round_to_tick(price, tick)
        quantity = position.quantity
        proceeds = price * quantity * (_ONE - cost)
        cash += proceeds
        basis = position.entry * quantity
        pnl = proceeds - basis
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
        # --- 1. exits at the open -------------------------------------------------------------
        for row in list(positions):
            position = positions[row]
            bar = _bar_at(panel, row, col)
            if bar.open is None:
                previous, mark = _last_printed_close(panel, position, col)
                if col - previous >= exits_config.no_bar_sessions:
                    written_off = mark if mark is not None else position.stop
                    close_position(position, col, written_off, ExitReason.NO_BAR)
                continue
            hit = stop_fill(bar, position.stop)
            if hit is not None:
                reason, price = hit
                close_position(position, col, price, reason)

        # --- 2. fills at the open, if the gate was OPEN at yesterday's close -------------------
        if col >= 1 and bool(gate_open[col - 1]):
            signal_col = col - 1
            candidates = [
                int(row)
                for row in np.nonzero(panel.signal[:, signal_col])[0]
                if int(row) not in positions
            ]
            if candidates:
                candidates.sort(key=lambda row: (-_rank_of(panel, row, signal_col), row))
                marked = _mark_equity(panel, positions, cash, signal_col)
                taken = 0
                for row in candidates:
                    if taken >= sizing.max_new_entries_per_session:
                        skipped["session_cap"] += 1
                        break
                    if len(positions) >= sizing.max_slots:
                        skipped["slots_full"] += 1
                        break
                    bar = _bar_at(panel, row, col)
                    if bar.open is None or bar.open <= _ZERO:
                        skipped["no_bar"] += 1
                        continue
                    if bar.open == bar.high == bar.low:
                        skipped["locked"] += 1
                        continue
                    fill = bar.open
                    stop = initial_stop(fill, exits_config, tick)
                    # The **signal** session's 20-session average turnover — the last reading a
                    # book sizing at the moment of the fill could have seen (`04` §6.2).
                    turnover_avg = panel.turnover_avg_20[row, signal_col]
                    sized = size_entry(
                        equity=marked,
                        cash_available=cash,
                        entry_price=fill * (_ONE + cost),
                        stop_price=stop,
                        turnover_avg_inr=(
                            _dec(turnover_avg) if math.isfinite(turnover_avg) else None
                        ),
                        config=sizing,
                    )
                    if not sized.placed:
                        short_of_cash = cash < sizing.min_trade_value_inr
                        if sized.refusal is not SizeRefusal.BELOW_MIN_TRADE_VALUE:
                            short_of_cash = True
                        skipped["cash" if short_of_cash else "turnover"] += 1
                        continue
                    cash -= sized.entry_price * sized.quantity
                    positions[row] = _Position(
                        row=row,
                        quantity=sized.quantity,
                        entry=sized.entry_price,
                        entry_col=col,
                        stop=stop,
                        initial_stop=stop,
                        high_since=fill,
                        cap=sized.cap,
                    )
                    taken += 1
        elif col >= 1:
            skipped["gate_shut"] += int(panel.signal[:, col - 1].sum())

        # --- 3. the fill-day stop, then 4. the ratchet and the close --------------------------
        for row in list(positions):
            position = positions[row]
            bar = _bar_at(panel, row, col)
            if bar.close is None:
                continue
            if position.entry_col == col:
                day_zero = fill_day_stop(_as_open_position(panel, position), bar)
                if day_zero is not None and day_zero.price is not None:
                    close_position(position, col, day_zero.price, ExitReason.STOP_DAY0)
                    continue
            moved = ratchet(_as_open_position(panel, position), bar, exits_config, tick)
            if moved is None:  # pragma: no cover - `bar.close` is not None three lines above
                continue
            if moved.clamped_below_stop:
                clamped_below_stop += 1
            position.high_since = moved.high_since
            position.stop = moved.next_trigger

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
        clamped_below_stop=clamped_below_stop,
    )


def _annualised_pct(total_ratio: Decimal, years: Decimal) -> Decimal:
    """``total ** (1 / years) - 1``, as a percent.

    The one place a float is unavoidable: a fractional power of a ``Decimal`` has no exact decimal
    answer, and ``Decimal.ln``/``exp`` would give a different last digit from the study's own
    ``**``. The result is read back into a ``Decimal`` immediately.
    """
    if years <= _ZERO:
        return _ZERO
    grown = float(total_ratio) ** (1 / float(years))
    return (Decimal(str(grown)) - _ONE) * _HUNDRED


def summarise(result: BacktestResult) -> BacktestStats | None:
    """``04`` §12's statistics, computed once so a page and a test cannot disagree.

    ``None`` for a run with no equity curve — a window with no sessions in it. Returning a record
    of zeros instead would be a claim that the book was measured and made nothing.
    """
    curve = [value for value in result.equity if value != _ZERO]
    if len(curve) < _TWO_POINTS or not result.sessions:
        return None
    start, end = result.sessions[0], result.sessions[-1]
    years = Decimal((end - start).days) / DAYS_A_YEAR
    cagr = _annualised_pct(curve[-1] / curve[0], years)

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

    ratios = [float(later / earlier - _ONE) for earlier, later in pairwise(curve) if earlier]
    deviation = float(np.std(ratios, ddof=1)) if len(ratios) > 1 else 0.0
    sharpe_ratio = (
        float(np.mean(ratios)) / deviation * math.sqrt(SESSIONS_A_YEAR)
        if deviation > 0
        else math.nan
    )

    wins = [trade for trade in result.trades if trade.pnl_inr > _ZERO]
    losses = [trade for trade in result.trades if trade.pnl_inr <= _ZERO]
    gross_win = sum((trade.pnl_inr for trade in wins), _ZERO)
    gross_loss = -sum((trade.pnl_inr for trade in losses), _ZERO)
    count = len(result.trades)
    sessions_seen = len(result.open_positions) or 1
    average_open = Decimal(sum(result.open_positions)) / Decimal(sessions_seen)
    slots = Decimal(result.params.config.sizing.max_slots)
    return BacktestStats(
        start=start,
        end=end,
        years=years,
        final_equity_inr=curve[-1].quantize(PAISE),
        cagr_pct=float(cagr),
        max_drawdown_pct=float(drawdown),
        drawdown_peak_on=peak_at,
        drawdown_trough_on=trough_at,
        calmar=(cagr / abs(drawdown)) if drawdown < _ZERO else None,
        sharpe_ratio=sharpe_ratio,
        trades=count,
        win_rate_pct=(len(wins) / count * 100) if count else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > _ZERO else None,
        avg_win_pct=float(sum((t.return_pct for t in wins), _ZERO) / len(wins)) if wins else 0.0,
        avg_loss_pct=(
            float(sum((t.return_pct for t in losses), _ZERO) / len(losses)) if losses else 0.0
        ),
        avg_trade_pct=(
            float(sum((t.return_pct for t in result.trades), _ZERO) / count) if count else 0.0
        ),
        avg_hold_sessions=(
            Decimal(sum(trade.hold_sessions for trade in result.trades)) / Decimal(count)
            if count
            else _ZERO
        ),
        avg_open_positions=average_open,
        exposure_pct=float(average_open / slots * _HUNDRED),
        by_reason={
            reason.value: sum(1 for trade in result.trades if trade.reason is reason)
            for reason in ExitReason
            if any(trade.reason is reason for trade in result.trades)
        },
    )


def yearly(result: BacktestResult) -> list[YearRow]:
    """Calendar-year returns off the equity curve, and the trades that closed in each."""
    by_year: dict[int, list[Decimal]] = {}
    for session, value in zip(result.sessions, result.equity, strict=True):
        if value != _ZERO:
            by_year.setdefault(session.year, []).append(value)
    opening = result.equity[0] if result.equity else _ZERO
    out: list[YearRow] = []
    for year in sorted(by_year):
        closing = by_year[year][-1]
        closed = [trade for trade in result.trades if trade.exit_date.year == year]
        wins = sum(1 for trade in closed if trade.pnl_inr > _ZERO)
        out.append(
            YearRow(
                year=year,
                return_pct=float((closing / opening - _ONE) * _HUNDRED) if opening else 0.0,
                trades=len(closed),
                win_rate_pct=(wins / len(closed) * 100) if closed else 0.0,
            )
        )
        opening = closing
    return out
