"""The EOD backtest of the swing method (docs/swing/04 §11) — an approximation, labelled as one.

    "A number, with its caveats, before any real money."  (docs/swing/06, SW9)

The rule, verbatim from `04` §11: for each session, run the detectors at the close; for each
``SETTING_UP`` flag / ``GAP_DAY`` EP that is liquid and not locked, enter at the next session's
open if it is at or above the trigger, else at the trigger if the next session's high reaches it;
the stop is the prior day's low; ``manage`` runs daily with fills at the next open; size by §5 on
a constant sleeve with the ladder in force; costs per side on entry and exit. Report §10's
statistics, by setup and by year, and the equity curve.

What is reused rather than rewritten
------------------------------------
Every rule is the function the live book calls, so the backtest cannot disagree with the desk
about what the method is: :func:`~baskfy_core.swing.setups.detect_setups` finds the candidates,
:func:`~baskfy_core.swing.plan.build_entries` sizes them and refuses them in the plan's own order
(tier, size, exposure — with its cash-not-spent-twice rule), :func:`~baskfy_core.swing.stops.manage`
decides every exit, :func:`~baskfy_core.swing.market.market_gate` and
:func:`~baskfy_core.swing.market.exposure_tier` run the gate and the ladder, and
:func:`~baskfy_core.swing.journal.summarize` produces the statistics. From the screener's engine
(:mod:`baskfy_core.backtest`) it borrows the price grid, the money exponent and the missing-bar
tolerance, so a "delisted" name means the same thing in both backtests.

Shape of the computation
------------------------
Detection is vectorised per session: the indicator frame is computed once, sorted by date, and
each session's detection window (the last ``SwingConfig.bars_required`` sessions) is a contiguous
slice handed to the detectors as one frame — 2,500 instruments are one ``group_by``, not a loop.
The per-trade loop is plain Python over a dense ``(instrument, session)`` price panel, which is
where an EOD simulation spends almost none of its time.

Pure (law 1): no clock, no I/O; the caller supplies the bars and the trading calendar, and the
same inputs produce a byte-identical :meth:`BacktestResult.to_json`.

Where prices are Decimal and where they are float
-------------------------------------------------
The same departure the screener's engine records in ``docs/DECISIONS.md`` §15: the price panel is
float64 for lookup and every price is snapped back onto the four-decimal grid ``ohlcv_daily``
stores (:data:`~baskfy_core.backtest.PRICE_EXPONENT`) the moment it becomes a fill, a stop or a
mark. Every rupee that moves is ``Decimal``.

Judgement calls are recorded in ``docs/swing/DECISIONS-SW.md`` SW9.1 to SW9.5.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Final

import numpy as np
import polars as pl

from baskfy_core.backtest import MISSING_BAR_TOLERANCE_DAYS, MONEY_EXPONENT, PRICE_EXPONENT
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, TRADEABLE_SETUPS, Setup, SwingConfig
from baskfy_core.swing.indicators import liquid_expr, with_swing_indicators
from baskfy_core.swing.journal import ClosedTrade, JournalStats, exit_average, summarize
from baskfy_core.swing.market import (
    BreadthSnapshot,
    ExposureTier,
    MarketGate,
    breadth_snapshot,
    exposure_tier,
    market_gate,
)
from baskfy_core.swing.plan import SkipReason, SwingAccount, WatchItem, build_entries
from baskfy_core.swing.setups import CandidateStatus, detect_setups
from baskfy_core.swing.stops import (
    Action,
    ActionKind,
    ActionReason,
    DailyBar,
    OpenPosition,
    apply,
    manage,
)
from baskfy_core.windows import HIGH_1Y_BARS

__all__ = [
    "CAVEATS",
    "BacktestCloseReason",
    "BacktestParams",
    "BacktestResult",
    "BacktestTrade",
    "run_backtest",
]

#: `04` §11's three caveats, one sentence each, for the journal page to show verbatim.
CAVEATS: Final[tuple[str, ...]] = (
    "No intraday data (so no ORH filter — real entries are more selective).",
    "No circuit history before 2020.",
    "Survivorship handled by instrument.delisted_on.",
)

#: The candidate statuses `04` §11 enters. ``BREAKOUT_TODAY`` is not one of them: its trigger is
#: the breakout day's own high and the method buys the pivot, not the day after the pivot broke.
ENTERABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {CandidateStatus.SETTING_UP.value, CandidateStatus.GAP_DAY.value}
)

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_PCT: Final = Decimal(100)
_EPOCH: Final = dt.date(1970, 1, 1)
_PANEL_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "ma_fast", "ma_slow")
#: The fewest bars a name may bring to a detection window: today and one bar before it.
_MIN_WINDOW_BARS: Final = 2


class BacktestCloseReason(StrEnum):
    """Why a backtest position closed when no rule of `04` §6 closed it (DECISIONS-SW SW9.4).

    Every other close carries the :class:`~baskfy_core.swing.stops.ActionReason` that
    :func:`~baskfy_core.swing.stops.manage` produced.
    """

    #: The series stopped printing bars for ``MISSING_BAR_TOLERANCE_DAYS`` sessions: sold at its
    #: last close, the way the screener's engine liquidates a delisted holding.
    NO_BAR = "NO_BAR"
    #: Still open on the run's last session: marked at that close, so the trade list and the
    #: equity curve agree.
    END_OF_RUN = "END_OF_RUN"


@dataclass(frozen=True, slots=True)
class BacktestParams:
    """What a run is parameterised on. Everything else is a field of ``config``."""

    start: dt.date
    end: dt.date
    #: The constant sleeve every trade is sized against (SW9.1: no compounding).
    sleeve_inr: Decimal = Decimal(1_000_000)
    #: Charged on the entry price and on every exit price, in percent (0.13 = 0.13%).
    cost_pct_per_side: Decimal = Decimal("0.13")
    config: SwingConfig = DEFAULT_SWING_CONFIG


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    """One closed trade. ``entry`` and ``exit_avg`` are **cost-inclusive** (SW9.4): the entry
    carries the buy-side cost and every exit fill the sell-side cost, so ``r_multiple`` and
    ``pnl_inr`` are `04` §10's formulas applied to what the trade actually cost and returned."""

    symbol: str
    setup: str
    entry_date: dt.date
    exit_date: dt.date
    entry: Decimal
    initial_stop: Decimal
    exit_avg: Decimal
    quantity: int
    r_multiple: Decimal
    pnl_inr: Decimal
    close_reason: str

    def as_closed_trade(self) -> ClosedTrade:
        return ClosedTrade(
            symbol=self.symbol,
            setup=self.setup,
            entry_date=self.entry_date,
            exit_date=self.exit_date,
            entry=self.entry,
            initial_stop=self.initial_stop,
            exit_avg=self.exit_avg,
            quantity=self.quantity,
        )


@dataclass(frozen=True, slots=True)
class BacktestResult:
    params: BacktestParams
    #: In close order.
    trades: tuple[BacktestTrade, ...]
    #: :func:`~baskfy_core.swing.journal.summarize` over ``trades``.
    stats: JournalStats
    #: One entry per tradeable setup, zeros when it never traded.
    by_setup: dict[str, JournalStats]
    #: One entry per calendar year of the run, keyed by the year the trade **closed** in.
    by_year: dict[int, JournalStats]
    #: Sleeve equity after each session: the sleeve plus realised P&L plus open positions marked
    #: at that session's close.
    equity_curve: tuple[tuple[dt.date, Decimal], ...]
    #: Counts. ``entered + sum(skipped_*) == candidates`` always holds.
    funnel: dict[str, int]
    #: The gate and the rung after each session's close — what tomorrow's entries were allowed.
    #: Additive to contract C3 (SW9.1).
    ladder: tuple[tuple[dt.date, str, int], ...] = ()

    def to_json(self) -> dict[str, object]:
        """Plain JSON: ``str`` for every ``Decimal`` and date, lists for tuples, fixed key order."""
        return {
            "params": _plain(asdict(self.params)),
            "trades": [_plain(asdict(trade)) for trade in self.trades],
            "stats": _plain(asdict(self.stats)),
            "by_setup": {setup: _plain(asdict(stats)) for setup, stats in self.by_setup.items()},
            "by_year": {
                str(year): _plain(asdict(stats)) for year, stats in sorted(self.by_year.items())
            },
            "equity_curve": [[day.isoformat(), str(equity)] for day, equity in self.equity_curve],
            "funnel": dict(self.funnel),
            "ladder": [[day.isoformat(), gate, level] for day, gate, level in self.ladder],
            "caveats": list(CAVEATS),
        }


def _plain(value: object) -> object:
    if isinstance(value, Decimal | dt.date):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Prices and money
# ---------------------------------------------------------------------------


def _price(value: float) -> Decimal:
    """Snap a float64 lookup back onto the four-decimal grid ``ohlcv_daily`` stores."""
    return Decimal(value).quantize(PRICE_EXPONENT, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def _decimal(value: float) -> Decimal:
    """A detector reading (a score, an ADR%, a turnover) as the ``Decimal`` the plan wants."""
    return Decimal(repr(float(value)))


# ---------------------------------------------------------------------------
# The panel: every price the trade loop reads, addressable in O(1)
# ---------------------------------------------------------------------------


def _days(dates: Sequence[dt.date]) -> np.ndarray:
    return np.array([(day - _EPOCH).days for day in dates], dtype=np.int64)


@dataclass(frozen=True, slots=True)
class _Panel:
    """Dense ``(instrument, session)`` arrays of the prices ``manage`` and the fills read."""

    instruments: np.ndarray
    columns: dict[str, np.ndarray]

    @classmethod
    def build(cls, indicated: pl.DataFrame, calendar_days: np.ndarray) -> _Panel:
        instruments = np.unique(indicated["instrument_id"].to_numpy())
        row_days = indicated["date"].cast(pl.Int64).to_numpy()
        col = np.searchsorted(calendar_days, row_days)
        col_clipped = np.minimum(col, len(calendar_days) - 1)
        on_calendar = calendar_days[col_clipped] == row_days
        row = np.searchsorted(instruments, indicated["instrument_id"].to_numpy())
        columns: dict[str, np.ndarray] = {}
        for name in _PANEL_COLUMNS:
            array = np.full((len(instruments), len(calendar_days)), np.nan)
            array[row[on_calendar], col_clipped[on_calendar]] = indicated[name].to_numpy()[
                on_calendar
            ]
            columns[name] = array
        return cls(instruments, columns)

    def row_of(self, instrument_id: int) -> int:
        index = int(np.searchsorted(self.instruments, instrument_id))
        if index >= len(self.instruments) or self.instruments[index] != instrument_id:
            raise KeyError(instrument_id)
        return index

    def has_bar(self, row: int, col: int) -> bool:
        return not np.isnan(self.columns["close"][row, col])

    def price(self, name: str, row: int, col: int) -> Decimal:
        return _price(float(self.columns[name][row, col]))

    def optional(self, name: str, row: int, col: int) -> Decimal | None:
        value = float(self.columns[name][row, col])
        return None if np.isnan(value) else _price(value)


# ---------------------------------------------------------------------------
# The book
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Position:
    instrument_id: int
    row: int
    setup: str
    state: OpenPosition
    entry_col: int
    #: Cost-inclusive entry — what a share actually cost.
    entry_paid: Decimal
    #: Exit fills so far, ``(quantity, cost-inclusive price)``.
    fills: list[tuple[int, Decimal]] = field(default_factory=list)
    #: Sells decided at the last close, to fill at the next open.
    pending: list[Action] = field(default_factory=list)
    last_close: Decimal = _ZERO
    missing: int = 0


@dataclass(frozen=True, slots=True)
class _Candidate:
    instrument_id: int
    symbol: str
    setup: str
    score: Decimal
    trigger: Decimal
    stop: Decimal
    adr_pct: Decimal
    avg_turnover_inr: Decimal | None
    locked: bool


_SKIP_KEY: Final[dict[SkipReason, str]] = {
    SkipReason.GATE_RED: "skipped_gate",
    SkipReason.ALREADY_HELD: "skipped_held",
    SkipReason.LOCKED_UPPER_CIRCUIT: "skipped_locked",
    SkipReason.TIER_FULL: "skipped_tier",
    SkipReason.SIZE_REFUSED: "skipped_size",
    SkipReason.EXPOSURE_FULL: "skipped_exposure",
    SkipReason.NOT_TRADEABLE_SETUP: "skipped_not_tradeable",
}

_FUNNEL_KEYS: Final[tuple[str, ...]] = (
    "sessions",
    "detected",
    "candidates",
    "entered",
    "skipped_locked",
    "skipped_gate",
    "skipped_held",
    "skipped_no_next_session",
    "skipped_no_bar",
    "skipped_no_trigger",
    "skipped_tier",
    "skipped_size",
    "skipped_exposure",
    "skipped_not_tradeable",
    "closed",
    "closed_no_bar",
    "closed_end_of_run",
)


class _Run:
    """One backtest's mutable state, advanced one session at a time.

    The order inside a session mirrors the live book's day: fills at the open (yesterday's sells,
    then yesterday's candidates), the rules at the close (``manage`` on every position, then
    detection, breadth, gate and ladder for tomorrow), then the mark.
    """

    def __init__(
        self,
        *,
        params: BacktestParams,
        by_date: pl.DataFrame,
        panel: _Panel | None,
        calendar: list[dt.date],
        calendar_days: np.ndarray,
    ) -> None:
        self.params = params
        self.config = params.config
        self.by_date = by_date
        self.row_days = by_date["date"].cast(pl.Int64).to_numpy() if by_date.height else _days([])
        self.panel = panel
        self.calendar = calendar
        self.calendar_days = calendar_days
        self.cost = params.cost_pct_per_side / _PCT
        self.cash = params.sleeve_inr
        self.positions: dict[str, _Position] = {}
        self.closed: list[BacktestTrade] = []
        self.closed_r: list[Decimal] = []
        self.level = 0
        self.gate = MarketGate.RED
        self.tier = self._tier_for(MarketGate.RED)
        self.pending_candidates: list[_Candidate] = []
        self.held_at_close: frozenset[str] = frozenset()
        self.funnel: dict[str, int] = dict.fromkeys(_FUNNEL_KEYS, 0)
        self.equity_curve: list[tuple[dt.date, Decimal]] = []
        self.ladder: list[tuple[dt.date, str, int]] = []

    # -- prices -------------------------------------------------------------

    def _paid(self, price: Decimal) -> Decimal:
        return (price * (_ONE + self.cost)).quantize(PRICE_EXPONENT, rounding=ROUND_HALF_UP)

    def _received(self, price: Decimal) -> Decimal:
        return (price * (_ONE - self.cost)).quantize(PRICE_EXPONENT, rounding=ROUND_HALF_UP)

    def _tier_for(self, gate: MarketGate) -> ExposureTier:
        return exposure_tier(
            current_level=self.level,
            closed_r_multiples=self.closed_r,
            gate=gate,
            config=self.config.market,
        )

    # -- the session ----------------------------------------------------------

    def session(self, col: int, *, last: bool) -> None:
        day = self.calendar[col]
        self.funnel["sessions"] += 1
        self._fill_pending(col)
        self._enter(col)
        self._manage(col, day)
        if last:
            self._close_everything(day)
        self._detect(col, day, last=last)
        self._mark(col, day)

    # -- morning ----------------------------------------------------------------

    def _fill_pending(self, col: int) -> None:
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            if not position.pending or not self._bar(position, col):
                continue
            open_ = self._panel().price("open", position.row, col)
            actions = position.pending
            position.pending = []
            for action in actions:
                position.fills.append((action.quantity, self._received(open_)))
                self.cash += self._money(open_, action.quantity, received=True)
            position.state = apply(position.state, actions)
            if position.state.quantity == 0:
                self._close(position, self.calendar[col], actions[-1].reason.value)

    def _enter(self, col: int) -> None:
        candidates: list[tuple[_Candidate, Decimal]] = []
        pending = self.pending_candidates
        self.pending_candidates = []
        seen: set[str] = set()
        for candidate in pending:
            fill = self._entry_fill(candidate, col, seen)
            if fill is not None:
                seen.add(candidate.symbol)
                candidates.append((candidate, fill))
        if not candidates:
            return
        open_cost = sum((p.entry_paid * p.state.quantity for p in self.positions.values()), _ZERO)
        account = SwingAccount(
            equity=self.params.sleeve_inr,
            cash_available=self.params.sleeve_inr - open_cost,
            open_symbols=frozenset(self.positions),
            open_exposure_inr=open_cost,
        )
        watch = [
            WatchItem(
                symbol=candidate.symbol,
                setup=Setup(candidate.setup),
                trigger=fill,
                stop_ref=candidate.stop,
                adr_pct=candidate.adr_pct,
                avg_turnover_inr=candidate.avg_turnover_inr,
                score=candidate.score,
                locked_upper_circuit=candidate.locked,
            )
            for candidate, fill in candidates
        ]
        lines, skipped = build_entries(
            as_of=self.calendar[col],
            watch=watch,
            account=account,
            gate=self.gate,
            tier=self.tier,
            config=self.config,
        )
        for skip in skipped:
            self.funnel[_SKIP_KEY[skip.reason]] += 1
        by_symbol = {candidate.symbol: (candidate, fill) for candidate, fill in candidates}
        for line in lines:
            candidate, fill = by_symbol[line.symbol]
            if line.trail is None:
                raise ValueError(f"{line.symbol}: a BUY line without a trail")
            paid = self._paid(fill)
            self.cash -= self._money(fill, line.quantity, received=False)
            self.positions[line.symbol] = _Position(
                instrument_id=candidate.instrument_id,
                row=self._panel().row_of(candidate.instrument_id),
                setup=candidate.setup,
                # The rules read the fill, as the live book's ``entry_avg`` is the fill: a
                # breakeven stop sits at the price paid on the exchange, not at price-plus-cost.
                # The cost is the journal's business (``entry_paid``), SW9.4.
                state=OpenPosition(
                    symbol=line.symbol,
                    entry_date=self.calendar[col],
                    entry=fill,
                    initial_stop=candidate.stop,
                    stop=candidate.stop,
                    quantity=line.quantity,
                    partial_done=False,
                    trail=line.trail,
                    is_ep_gap_day=False,
                ),
                entry_col=col,
                entry_paid=paid,
                last_close=fill,
            )
            self.funnel["entered"] += 1

    def _entry_fill(self, candidate: _Candidate, col: int, seen: set[str]) -> Decimal | None:
        """`04` §11's entry rule, or the funnel key that explains why there is no entry."""
        fill: Decimal | None = None
        reason = self._refusal_at_the_close(candidate, seen)
        if reason is None:
            fill, reason = self._fill_at_the_open(candidate, col)
        if reason is not None:
            self.funnel[reason] += 1
            return None
        return fill

    def _refusal_at_the_close(self, candidate: _Candidate, seen: set[str]) -> str | None:
        """What the evening plan already knows: the band, the gate, the book."""
        if candidate.locked:
            return "skipped_locked"
        if self.gate is MarketGate.RED or not self.tier.new_entries_allowed:
            return "skipped_gate"
        if candidate.symbol in self.held_at_close or candidate.symbol in seen:
            return "skipped_held"
        return None

    def _fill_at_the_open(
        self, candidate: _Candidate, col: int
    ) -> tuple[Decimal | None, str | None]:
        """The next session's open if it is at or above the trigger, else the trigger if the
        high reaches it, else no fill and the reason."""
        panel = self._panel()
        row = panel.row_of(candidate.instrument_id)
        if not panel.has_bar(row, col):
            return None, "skipped_no_bar"
        open_ = panel.price("open", row, col)
        if open_ >= candidate.trigger:
            return open_, None
        if panel.price("high", row, col) >= candidate.trigger:
            return candidate.trigger, None
        return None, "skipped_no_trigger"

    # -- the close ----------------------------------------------------------------

    def _manage(self, col: int, day: dt.date) -> None:
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            if not self._bar(position, col):
                self._missing(position, col, day)
                continue
            position.missing = 0
            panel = self._panel()
            bar = DailyBar(
                date=day,
                open=panel.price("open", position.row, col),
                high=panel.price("high", position.row, col),
                low=panel.price("low", position.row, col),
                close=panel.price("close", position.row, col),
                ma10=panel.optional("ma_fast", position.row, col),
                ma20=panel.optional("ma_slow", position.row, col),
                bars_since_entry=col - position.entry_col,
            )
            position.last_close = bar.close
            actions = manage(position.state, bar, self.config.stops)
            self._apply_actions(position, bar, actions)

    def _apply_actions(self, position: _Position, bar: DailyBar, actions: list[Action]) -> None:
        stopped = [a for a in actions if a.kind is ActionKind.STOPPED_OUT]
        if stopped:
            # SW9.3: the GTT fills at the stop, or at the open when the day gapped through it —
            # except on the entry day, when the entry happened before the low did.
            fill = position.state.stop
            if bar.bars_since_entry > 0:
                fill = min(fill, bar.open)
            position.fills.append((position.state.quantity, self._received(fill)))
            self.cash += self._money(fill, position.state.quantity, received=True)
            position.state = apply(position.state, stopped)
            self._close(position, bar.date, ActionReason.HARD_STOP_HIT.value)
            return
        sells = [a for a in actions if a.kind in (ActionKind.SELL_ALL, ActionKind.SELL_PARTIAL)]
        raises = [a for a in actions if a.kind is ActionKind.RAISE_STOP]
        position.pending = sells
        if raises:
            position.state = apply(position.state, raises)

    def _missing(self, position: _Position, col: int, day: dt.date) -> None:
        position.missing += 1
        if position.missing < MISSING_BAR_TOLERANCE_DAYS:
            return
        self._liquidate(position, day, BacktestCloseReason.NO_BAR)
        self.funnel["closed_no_bar"] += 1

    def _close_everything(self, day: dt.date) -> None:
        for symbol in sorted(self.positions):
            self._liquidate(self.positions[symbol], day, BacktestCloseReason.END_OF_RUN)
            self.funnel["closed_end_of_run"] += 1

    def _liquidate(self, position: _Position, day: dt.date, reason: BacktestCloseReason) -> None:
        quantity = position.state.quantity
        position.fills.append((quantity, self._received(position.last_close)))
        self.cash += self._money(position.last_close, quantity, received=True)
        position.pending = []
        position.state = apply(
            position.state, [Action(ActionKind.SELL_ALL, ActionReason.NOTHING_TO_DO, quantity)]
        )
        self._close(position, day, reason.value)

    def _close(self, position: _Position, day: dt.date, reason: str) -> None:
        entered = sum(quantity for quantity, _ in position.fills)
        closed = ClosedTrade(
            symbol=position.state.symbol,
            setup=position.setup,
            entry_date=position.state.entry_date,
            exit_date=day,
            entry=position.entry_paid,
            initial_stop=position.state.initial_stop,
            exit_avg=exit_average(position.fills),
            quantity=entered,
        )
        self.closed.append(
            BacktestTrade(
                symbol=closed.symbol,
                setup=closed.setup,
                entry_date=closed.entry_date,
                exit_date=closed.exit_date,
                entry=closed.entry,
                initial_stop=closed.initial_stop,
                exit_avg=closed.exit_avg,
                quantity=closed.quantity,
                r_multiple=closed.r_multiple,
                pnl_inr=closed.pnl_inr,
                close_reason=reason,
            )
        )
        self.closed_r.append(closed.r_multiple)
        self.funnel["closed"] += 1
        del self.positions[position.state.symbol]

    def _detect(self, col: int, day: dt.date, *, last: bool) -> None:
        """Detection, breadth, the gate and the ladder, all at ``day``'s close."""
        self.held_at_close = frozenset(self.positions)
        window, today = self._slices(col)
        found = detect_setups(window, day, self.config) if window.height else None
        if found is not None and found.height:
            self.funnel["detected"] += found.height
            found = found.filter(
                pl.col("status").is_in(list(ENTERABLE_STATUSES))
                & pl.col("setup").is_in([s.value for s in TRADEABLE_SETUPS])
            )
            self.funnel["candidates"] += found.height
            if last:
                self.funnel["skipped_no_next_session"] += found.height
            else:
                self.pending_candidates = _candidates(found)
        if today.height:
            liquid = today.filter(liquid_expr(self.config))
            breadth = breadth_snapshot(
                liquid.select("ret_20", "close", "ma_slow", "high_1y"), self.config.market
            )
        else:
            breadth = BreadthSnapshot(0, 0.0, 0.0, 0.0)
        self.gate = market_gate(breadth, None, self.config.market)
        self.tier = self._tier_for(self.gate)
        self.level = self.tier.level
        self.ladder.append((day, self.gate.value, self.level))

    def _slices(self, col: int) -> tuple[pl.DataFrame, pl.DataFrame]:
        """The detection window (``bars_required`` sessions ending today) and today's rows."""
        if not self.by_date.height:
            return self.by_date, self.by_date
        first = max(col - self.config.bars_required + 1, 0)
        lo = int(np.searchsorted(self.row_days, self.calendar_days[first]))
        day_lo = int(np.searchsorted(self.row_days, self.calendar_days[col]))
        hi = int(np.searchsorted(self.row_days, self.calendar_days[col], side="right"))
        window = self.by_date.slice(lo, hi - lo)
        # A name with a single bar in the window (its listing day) has no "bars before today"
        # for the flag detector's pole search to slice, and the detector raises rather than
        # answering "no". One bar cannot be a setup, so it is left out of the window here;
        # STATUS.md records the edge for the module that owns ``setups.py``.
        window = window.filter(pl.len().over("instrument_id") >= _MIN_WINDOW_BARS)
        return window, self.by_date.slice(day_lo, hi - day_lo)

    def _mark(self, col: int, day: dt.date) -> None:
        marked = sum((p.last_close * p.state.quantity for p in self.positions.values()), _ZERO)
        self.equity_curve.append((day, _money(self.cash + marked)))

    # -- helpers ------------------------------------------------------------------

    def _panel(self) -> _Panel:
        if self.panel is None:
            raise ValueError("no bars, so no prices to fill at")
        return self.panel

    def _bar(self, position: _Position, col: int) -> bool:
        return self._panel().has_bar(position.row, col)

    def _money(self, price: Decimal, quantity: int, *, received: bool) -> Decimal:
        effective = self._received(price) if received else self._paid(price)
        return _money(effective * quantity)


def _candidates(found: pl.DataFrame) -> list[_Candidate]:
    """Best score first, then symbol, setup and instrument — a total order, so two runs agree."""
    rows = [
        _Candidate(
            instrument_id=int(row["instrument_id"]),
            symbol=str(row["symbol"]),
            setup=str(row["setup"]),
            score=_decimal(row["score"]),
            trigger=_price(row["trigger"]),
            stop=_price(row["stop_ref"]),
            adr_pct=_decimal(row["adr_pct"]),
            avg_turnover_inr=(
                None if row["turnover_avg"] is None else _decimal(row["turnover_avg"])
            ),
            locked=bool(row["locked_upper_circuit"]),
        )
        for row in found.iter_rows(named=True)
    ]
    return sorted(rows, key=lambda c: (-c.score, c.symbol, c.setup, c.instrument_id))


def _indicated(bars: pl.DataFrame, config: SwingConfig) -> pl.DataFrame:
    """The indicator frame, plus ``high_1y`` for breadth, sorted by date for per-session slices.

    ``high_1y`` is the factor engine's definition (the highest high of the last
    :data:`~baskfy_core.windows.HIGH_1Y_BARS` bars, null until a year exists) with the same
    fallback the worker's ``breadth_frame`` uses — the bar's own high, a lower bound.
    """
    if "symbol" not in bars.columns:
        raise ValueError("bars need a symbol column; the journal is kept by symbol")
    indicated = with_swing_indicators(bars, config)
    return indicated.with_columns(
        pl.col("high")
        .rolling_max(window_size=HIGH_1Y_BARS, min_samples=HIGH_1Y_BARS)
        .over("instrument_id")
        .fill_null(pl.col("high"))
        .alias("high_1y")
    ).sort(["date", "instrument_id"])


def _by_year(trades: Sequence[BacktestTrade], params: BacktestParams) -> dict[int, JournalStats]:
    closed = [trade.as_closed_trade() for trade in trades]
    return {
        year: summarize([t for t in closed if t.exit_date.year == year])
        for year in range(params.start.year, params.end.year + 1)
    }


def _by_setup(trades: Sequence[BacktestTrade]) -> dict[str, JournalStats]:
    closed = [trade.as_closed_trade() for trade in trades]
    return {
        setup.value: summarize([t for t in closed if t.setup == setup.value])
        for setup in Setup
        if setup in TRADEABLE_SETUPS
    }


def _check(params: BacktestParams, calendar: Sequence[dt.date]) -> None:
    if params.end < params.start:
        raise ValueError(f"end {params.end} is before start {params.start}")
    if params.sleeve_inr <= _ZERO:
        raise ValueError(f"sleeve_inr must be positive, got {params.sleeve_inr}")
    if params.cost_pct_per_side < _ZERO:
        raise ValueError(f"cost_pct_per_side must not be negative, got {params.cost_pct_per_side}")
    if any(later <= earlier for earlier, later in pairwise(calendar)):
        raise ValueError("calendar must be strictly increasing")


def run_backtest(
    bars: pl.DataFrame, params: BacktestParams, *, calendar: Sequence[dt.date]
) -> BacktestResult:
    """`04` §11 over ``[params.start, params.end]``.

    ``bars`` is the adjusted frame ``load_swing_bars`` produces — ``instrument_id``, ``symbol``,
    ``date``, ``open``, ``high``, ``low``, ``close``, ``volume`` and optionally ``turnover``,
    ``upper_circuit``, ``adj_factor`` — covering the lookback before ``start`` as well.
    ``calendar`` is every trading day in order, covering the bars; a session is a calendar day
    inside the run's bounds, and a bar on a date the calendar does not name is read by the
    detectors but never traded on.
    """
    _check(params, calendar)
    days = list(calendar)
    calendar_days = _days(days)
    session_cols = [i for i, day in enumerate(days) if params.start <= day <= params.end]

    if bars.height:
        by_date = _indicated(bars, params.config)
        panel: _Panel | None = _Panel.build(by_date, calendar_days) if days else None
    else:
        by_date = pl.DataFrame(schema={"date": pl.Date, "instrument_id": pl.Int64})
        panel = None

    run = _Run(
        params=params,
        by_date=by_date,
        panel=panel,
        calendar=days,
        calendar_days=calendar_days,
    )
    for index, col in enumerate(session_cols):
        run.session(col, last=index == len(session_cols) - 1)

    trades = tuple(run.closed)
    return BacktestResult(
        params=params,
        trades=trades,
        stats=summarize([t.as_closed_trade() for t in trades]),
        by_setup=_by_setup(trades),
        by_year=_by_year(trades, params),
        equity_curve=tuple(run.equity_curve),
        funnel=dict(run.funnel),
        ladder=tuple(run.ladder),
    )
