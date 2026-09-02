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
decides every exit, :func:`~baskfy_core.swing.market.market_gate`,
:func:`~baskfy_core.swing.market.drawdown_locked` and
:func:`~baskfy_core.swing.market.exposure_tier` run the gate, the lock-out and the ladder, and
:func:`~baskfy_core.swing.journal.summarize`
produces the statistics. From the screener's engine (:mod:`baskfy_core.backtest`) it borrows the
price grid, the money exponent and the missing-bar tolerance, so a "delisted" name means the
same thing in both backtests.

The frame is Maulik's (STANDING-ANSWERS A12, SW9.6)
--------------------------------------------------
A **constant ₹10 lakh sleeve** and **the index rule**. The caller may hand in the benchmark's
close series (NIFTY 500 from ``index_snapshot_daily``, NIFTY 50 when the runner falls back);
the 10- and 20-bar SMAs of `04` §8.2 are computed in-frame from the closes up to **and
including** the session's own close — the same reading the nightly job takes off the last
twenty rows on or before the date — and never from a later one. The **drawdown lock-out** of
§8.5 runs on the sleeve's own equity curve (realised plus open positions marked at the close),
peak-to-trough as a percentage of the constant sleeve, with the live rule's 15 % / 10 %
hysteresis. And because "what does the gate buy" is the question the page has to answer, every
run keeps **three books** side by side over one detection pass — the gate off, breadth only,
and breadth with the index — and reports them per year and per setup, with breadth's and the
index rule's contributions labelled separately (:class:`GateComparison`).

Shape of the computation
------------------------
Detection is vectorised per session: the indicator frame is computed once, sorted by date, and
each session's detection window (the last ``SwingConfig.bars_required`` sessions) is a contiguous
slice handed to the detectors as one frame — 2,500 instruments are one ``group_by``, not a loop.
Detection, breadth and the index reading are shared by the three books; the per-trade loop is
plain Python over a dense ``(instrument, session)`` price panel, which is where an EOD simulation
spends almost none of its time.

Pure (law 1): no clock, no I/O; the caller supplies the bars, the calendar, the index and the
delisting dates, and the same inputs produce a byte-identical :meth:`BacktestResult.to_json`.

Where prices are Decimal and where they are float
-------------------------------------------------
The same departure the screener's engine records in ``docs/DECISIONS.md`` §15: the price panel is
float64 for lookup and every price is snapped back onto the four-decimal grid ``ohlcv_daily``
stores (:data:`~baskfy_core.backtest.PRICE_EXPONENT`) the moment it becomes a fill, a stop or a
mark. Every rupee that moves is ``Decimal``.

Judgement calls are recorded in ``docs/swing/DECISIONS-SW.md`` SW9.1 to SW9.5 and SW9.6.1 to
SW9.6.4.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
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
    IndexReading,
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
    "GATE_MODE_DEFINITIONS",
    "INDEX_ABSENT_CAVEAT",
    "BacktestCloseReason",
    "BacktestParams",
    "BacktestResult",
    "BacktestTrade",
    "DrawdownSummary",
    "GateCell",
    "GateComparison",
    "GateContribution",
    "GateMode",
    "run_backtest",
]

#: `04` §11's caveats, one sentence each, for the journal page to show verbatim: the three the
#: section has carried since SW9 and STANDING-ANSWERS B1's circuit caveat (SW9.6).
CAVEATS: Final[tuple[str, ...]] = (
    "No intraday data (so no ORH filter — real entries are more selective).",
    "No circuit history before 2020.",
    "Survivorship handled by instrument.delisted_on.",
    "Where upper_circuit is absent no lock is assumed, so a name that was locked may have been "
    "entered here.",
)

#: Added to a result's caveats when no index series was supplied: the gate was breadth-only and
#: the index rule (`04` §8.2) was not applied (STANDING-ANSWERS A12).
INDEX_ABSENT_CAVEAT: Final = (
    "No index series was supplied, so the gate is breadth-only and the index rule was not applied."
)

#: The candidate statuses `04` §11 enters. ``BREAKOUT_TODAY`` is not one of them: its trigger is
#: the breakout day's own high and the method buys the pivot, not the day after the pivot broke.
ENTERABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {CandidateStatus.SETTING_UP.value, CandidateStatus.GAP_DAY.value}
)

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_PCT: Final = Decimal(100)
_TWO_DP: Final = Decimal("0.01")
_EPOCH: Final = dt.date(1970, 1, 1)
_PANEL_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "ma_fast", "ma_slow")
#: The fewest bars a name may bring to a detection window: today and one bar before it.
_MIN_WINDOW_BARS: Final = 2


class BacktestCloseReason(StrEnum):
    """Why a backtest position closed when no rule of `04` §6 closed it (DECISIONS-SW SW9.4).

    Every other close carries the :class:`~baskfy_core.swing.stops.ActionReason` that
    :func:`~baskfy_core.swing.stops.manage` produced.
    """

    #: The series stopped printing bars for ``MISSING_BAR_TOLERANCE_DAYS`` sessions with no
    #: delisting date known: sold at its last close, the way the screener's engine liquidates a
    #: holding that went quiet.
    NO_BAR = "NO_BAR"
    #: The instrument's ``delisted_on`` is known (the runner reads it): sold at its last close on
    #: its last bar, and counted ``DELISTED`` in the funnel (STANDING-ANSWERS B2).
    DELISTED = "DELISTED"
    #: Still open on the run's last session: marked at that close, so the trade list and the
    #: equity curve agree.
    END_OF_RUN = "END_OF_RUN"


class GateMode(StrEnum):
    """The three books a run keeps, so the page can show what the gate buys (A12).

    * ``GATE_OFF``: :func:`market_gate` replaced by GREEN every session. The ladder and the
      drawdown lock-out still apply — they are the trader's own results, not the tape's.
    * ``BREADTH_ONLY``: ``market_gate(breadth, None)``, the gate SW9 shipped.
    * ``FULL``: ``market_gate(breadth, index)`` — breadth and the index rule.

    Breadth's contribution is ``BREADTH_ONLY - GATE_OFF``; the index rule's is
    ``FULL - BREADTH_ONLY``. The primary result is ``FULL`` when an index was supplied and
    ``BREADTH_ONLY`` otherwise (the caveats say so).
    """

    GATE_OFF = "gate_off"
    BREADTH_ONLY = "breadth_only"
    FULL = "full"


#: One sentence per mode, for the page — the definitions above, in the reader's words.
GATE_MODE_DEFINITIONS: Final[dict[GateMode, str]] = {
    GateMode.GATE_OFF: (
        "Gate off: the market gate is GREEN every session; the ladder and the drawdown lock-out "
        "still apply."
    ),
    GateMode.BREADTH_ONLY: "Breadth only: the gate reads breadth and no index.",
    GateMode.FULL: (
        "Full: the gate reads breadth and the index rule (the 10-day average over the 20-day)."
    ),
}


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
    #: The name of the index series handed to :func:`run_backtest` (``nifty-500``, or the
    #: ``nifty-50`` fallback) — a label the runner sets so the stored run says which benchmark
    #: the index rule read; ``None`` when no series is supplied (SW9.6.1).
    index_slug: str | None = None


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
class DrawdownSummary:
    """The constant-sleeve curve's deepest peak-to-trough, as a percentage of the sleeve (A12).

    ``peak`` is the highest close-of-session equity before the trough, ``trough`` the equity at
    the bottom, ``locked_sessions`` how many sessions' entries the lock-out of `04` §8.5 refused.
    """

    max_pct: Decimal
    peak: Decimal
    trough: Decimal
    trough_date: dt.date | None
    locked_sessions: int


@dataclass(frozen=True, slots=True)
class GateCell:
    """One book's numbers over one scope (the whole run, a year, a setup) — or, in a
    contribution, the difference between two books' numbers over that scope."""

    entered: int
    net_r: Decimal
    expectancy_r: Decimal
    win_rate_pct: Decimal
    #: The deepest drawdown of the book's curve inside the scope; ``None`` for a setup, whose
    #: trades share one curve with the other setup's.
    max_drawdown_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class GateContribution:
    """What one part of the gate bought: the numbers with it, less the numbers without it."""

    overall: GateCell
    by_year: dict[int, GateCell]
    by_setup: dict[str, GateCell]


@dataclass(frozen=True, slots=True)
class GateComparison:
    """Gate-on against gate-off, per year and per setup (STANDING-ANSWERS A12).

    ``by_year`` is keyed by the year a trade was **entered** in, because the gate decides
    entries: a refusal in December belongs to December's tape, whatever January would have done
    with the position. (``BacktestResult.by_year`` stays keyed by the close, as `04` §11's
    statistics are.) ``breadth`` is ``BREADTH_ONLY - GATE_OFF``; ``index_rule`` is
    ``FULL - BREADTH_ONLY`` and ``None`` when no index was supplied.
    """

    index_supplied: bool
    primary: GateMode
    overall: dict[GateMode, GateCell]
    by_year: dict[int, dict[GateMode, GateCell]]
    by_setup: dict[str, dict[GateMode, GateCell]]
    breadth: GateContribution
    index_rule: GateContribution | None

    @property
    def modes(self) -> tuple[GateMode, ...]:
        return tuple(self.overall)


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
    #: The primary book's deepest drawdown and how long the lock-out held (SW9.6).
    drawdown: DrawdownSummary = DrawdownSummary(_ZERO, _ZERO, _ZERO, None, 0)
    #: The three books side by side (SW9.6).
    comparison: GateComparison | None = None

    @property
    def caveats(self) -> tuple[str, ...]:
        """:data:`CAVEATS`, plus :data:`INDEX_ABSENT_CAVEAT` when the run had no index."""
        if self.comparison is None or not self.comparison.index_supplied:
            return (*CAVEATS, INDEX_ABSENT_CAVEAT)
        return CAVEATS

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
            "drawdown": _plain(asdict(self.drawdown)),
            "comparison": None if self.comparison is None else _comparison_json(self.comparison),
            "caveats": list(self.caveats),
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


def _cells_json(cells: Mapping[GateMode, GateCell]) -> dict[str, object]:
    return {mode.value: _plain(asdict(cell)) for mode, cell in cells.items()}


def _contribution_json(contribution: GateContribution) -> dict[str, object]:
    return {
        "overall": _plain(asdict(contribution.overall)),
        "by_year": {
            str(year): _plain(asdict(cell)) for year, cell in sorted(contribution.by_year.items())
        },
        "by_setup": {setup: _plain(asdict(cell)) for setup, cell in contribution.by_setup.items()},
    }


def _comparison_json(comparison: GateComparison) -> dict[str, object]:
    return {
        "index_supplied": comparison.index_supplied,
        "primary": comparison.primary.value,
        "modes": {mode.value: GATE_MODE_DEFINITIONS[mode] for mode in comparison.modes},
        "overall": _cells_json(comparison.overall),
        "by_year": {
            str(year): _cells_json(cells) for year, cells in sorted(comparison.by_year.items())
        },
        "by_setup": {setup: _cells_json(cells) for setup, cells in comparison.by_setup.items()},
        "contribution": {
            "breadth": _contribution_json(comparison.breadth),
            "index_rule": (
                None if comparison.index_rule is None else _contribution_json(comparison.index_rule)
            ),
        },
    }


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


def _sleeve_drawdown_pct(*, peak: Decimal, equity: Decimal, sleeve: Decimal) -> Decimal:
    """How far below its peak the curve sits, as a percentage of the **constant sleeve** (A12).

    `04` §8.5 measures the live sleeve against its peak because the live sleeve compounds; the
    backtest's sleeve never does (SW9.1), so its drawdown is measured against the number every
    trade is sized on — 15 % of the sleeve is thirty trades' risk, whatever the curve has made.
    Zero at or above the peak; two decimals, the precision the lock-out compares at.
    """
    if equity >= peak:
        return _ZERO
    return ((peak - equity) / sleeve * _PCT).quantize(_TWO_DP, rounding=ROUND_HALF_UP)


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
# The index: the benchmark's closes, read up to and including the session's own (A12)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _IndexSeries:
    """The index closes, sorted, and the two SMA lengths of `04` §8.2.

    :meth:`reading_on` mirrors the worker's ``load_index_reading``: the last ``slow`` closes
    dated on or before the session, averaged; ``None`` (the index is ignored) until that many
    exist. A close dated after the session is never in the window — house rule 5.
    """

    days: np.ndarray
    closes: np.ndarray
    fast: int
    slow: int

    @classmethod
    def build(cls, index: pl.DataFrame, config: SwingConfig) -> _IndexSeries:
        for column in ("date", "close"):
            if column not in index.columns:
                raise ValueError(f"index needs a {column} column")
        frame = (
            index.select(pl.col("date").cast(pl.Date), pl.col("close").cast(pl.Float64))
            .drop_nulls()
            .sort("date")
        )
        if frame["date"].n_unique() != frame.height:
            raise ValueError("index has two closes for one date")
        return cls(
            days=frame["date"].cast(pl.Int64).to_numpy(),
            closes=frame["close"].to_numpy(),
            fast=config.market.index_ma_fast,
            slow=config.market.index_ma_slow,
        )

    def reading_on(self, day: int) -> IndexReading | None:
        n = int(np.searchsorted(self.days, day, side="right"))
        if n < self.slow:
            return None
        return IndexReading(
            close=float(self.closes[n - 1]),
            ma_fast=float(self.closes[n - self.fast : n].mean()),
            ma_slow=float(self.closes[n - self.slow : n].mean()),
        )


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


@dataclass(frozen=True, slots=True)
class _Close:
    """What one session's close told every book: the candidates for tomorrow, breadth, the index."""

    candidates: list[_Candidate]
    detected: int
    breadth: BreadthSnapshot
    reading: IndexReading | None


_SKIP_KEY: Final[dict[SkipReason, str]] = {
    SkipReason.DRAWDOWN_LOCKOUT: "skipped_drawdown",
    SkipReason.GATE_RED: "skipped_gate",
    SkipReason.ALREADY_HELD: "skipped_held",
    SkipReason.LOCKED_UPPER_CIRCUIT: "skipped_locked",
    SkipReason.SESSION_CAP: "skipped_session_cap",
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
    "skipped_drawdown",
    "skipped_gate",
    "skipped_held",
    "skipped_no_next_session",
    "skipped_no_bar",
    "skipped_no_trigger",
    "skipped_session_cap",
    "skipped_tier",
    "skipped_size",
    "skipped_exposure",
    "skipped_not_tradeable",
    "closed",
    "closed_no_bar",
    "closed_delisted",
    "closed_end_of_run",
)


class _Book:
    """One gate mode's mutable state, advanced one session at a time.

    The order inside a session mirrors the live book's day: fills at the open (yesterday's sells,
    then yesterday's candidates), the rules at the close (``manage`` on every position), the mark,
    and then — from the detection, breadth and index reading the run shares between its books —
    the gate and the ladder for tomorrow.
    """

    def __init__(
        self,
        *,
        mode: GateMode,
        params: BacktestParams,
        panel: _Panel | None,
        calendar: list[dt.date],
        delisted: Mapping[int, dt.date],
    ) -> None:
        self.mode = mode
        self.params = params
        self.config = params.config
        self.panel = panel
        self.calendar = calendar
        self.delisted = delisted
        self.cost = params.cost_pct_per_side / _PCT
        self.cash = params.sleeve_inr
        self.positions: dict[str, _Position] = {}
        self.closed: list[BacktestTrade] = []
        self.closed_r: list[Decimal] = []
        self.level = 0
        self.gate = MarketGate.RED
        # The sleeve's drawdown, `04` §8.5 on the constant sleeve (A12): the peak is the highest
        # close-of-session equity the run has seen, starting at the sleeve itself (a sleeve with
        # no session behind it is at its peak, not in drawdown); `drawdown_pct` is how far below
        # it tonight's mark sits, as a percentage of the sleeve, and `drawdown_locked` is
        # yesterday's lock-out state, which `exposure_tier` needs for the hysteresis (locked at
        # `max_drawdown_pct`, released inside `resume_drawdown_pct`).
        self.peak = params.sleeve_inr
        self.drawdown_pct = _ZERO
        self.drawdown_locked = False
        self.max_drawdown = DrawdownSummary(_ZERO, params.sleeve_inr, params.sleeve_inr, None, 0)
        self.locked_sessions = 0
        self.drawdowns: list[tuple[dt.date, Decimal]] = []
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
            drawdown_pct=float(self.drawdown_pct),
            was_drawdown_locked=self.drawdown_locked,
        )

    # -- the session ----------------------------------------------------------

    def morning(self, col: int) -> None:
        self.funnel["sessions"] += 1
        self._fill_pending(col)
        self._enter(col)

    def evening(self, col: int, day: dt.date, *, last: bool) -> None:
        self._manage(col, day)
        if last:
            self._close_everything(day)
        # The mark comes before the ladder: tomorrow's rung reads tonight's equity against the
        # peak, the same way the evening job reads the sleeve's EOD NAV (SW9.5.1).
        self._mark(day)

    def settle(self, day: dt.date, close: _Close, *, last: bool) -> None:
        """The gate and the ladder for tomorrow, from what the close told every book."""
        self.held_at_close = frozenset(self.positions)
        self.funnel["detected"] += close.detected
        self.funnel["candidates"] += len(close.candidates)
        if last:
            self.funnel["skipped_no_next_session"] += len(close.candidates)
        else:
            self.pending_candidates = list(close.candidates)
        self.gate = self._gate(close)
        self.tier = self._tier_for(self.gate)
        self.level = self.tier.level
        self.drawdown_locked = self.tier.drawdown_locked
        if self.tier.drawdown_locked:
            self.locked_sessions += 1
        self.ladder.append((day, self.gate.value, self.level))

    def _gate(self, close: _Close) -> MarketGate:
        if self.mode is GateMode.GATE_OFF:
            return MarketGate.GREEN
        reading = close.reading if self.mode is GateMode.FULL else None
        return market_gate(close.breadth, reading, self.config.market)

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
        """What the evening plan already knows: the band, the drawdown, the gate, the book."""
        if candidate.locked:
            return "skipped_locked"
        if self.tier.drawdown_locked:
            return "skipped_drawdown"
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
            gone = self.delisted.get(position.instrument_id)
            if not self._bar(position, col):
                if gone is not None:
                    # STANDING-ANSWERS B2: a name whose delisting is known is dead the session it
                    # stops printing — sold at its last close, not after the screener's tolerance.
                    self._liquidate(position, day, BacktestCloseReason.DELISTED)
                    continue
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
            if gone is not None and day >= gone and symbol in self.positions:
                # Its last session on the exchange: whatever `manage` planned for tomorrow, there
                # is no tomorrow — sold at today's close.
                self._liquidate(position, day, BacktestCloseReason.DELISTED)

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

    def _close_everything(self, day: dt.date) -> None:
        for symbol in sorted(self.positions):
            self._liquidate(self.positions[symbol], day, BacktestCloseReason.END_OF_RUN)

    def _liquidate(self, position: _Position, day: dt.date, reason: BacktestCloseReason) -> None:
        quantity = position.state.quantity
        position.fills.append((quantity, self._received(position.last_close)))
        self.cash += self._money(position.last_close, quantity, received=True)
        position.pending = []
        position.state = apply(
            position.state, [Action(ActionKind.SELL_ALL, ActionReason.NOTHING_TO_DO, quantity)]
        )
        self._close(position, day, reason.value)
        self.funnel[f"closed_{reason.value.lower()}"] += 1

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

    def _mark(self, day: dt.date) -> None:
        marked = sum((p.last_close * p.state.quantity for p in self.positions.values()), _ZERO)
        equity = _money(self.cash + marked)
        self.equity_curve.append((day, equity))
        self.peak = max(self.peak, equity)
        self.drawdown_pct = _sleeve_drawdown_pct(
            peak=self.peak, equity=equity, sleeve=self.params.sleeve_inr
        )
        self.drawdowns.append((day, self.drawdown_pct))
        if self.drawdown_pct > self.max_drawdown.max_pct:
            self.max_drawdown = DrawdownSummary(
                self.drawdown_pct, self.peak, equity, day, self.locked_sessions
            )

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

    # -- the result ---------------------------------------------------------------

    def drawdown_summary(self) -> DrawdownSummary:
        summary = self.max_drawdown
        return DrawdownSummary(
            summary.max_pct, summary.peak, summary.trough, summary.trough_date, self.locked_sessions
        )

    def max_drawdown_in(self, year: int | None) -> Decimal:
        inside = (dd for day, dd in self.drawdowns if year is None or day.year == year)
        return max(inside, default=_ZERO)


class _Run:
    """Every book over one detection pass: the detectors, breadth and the index are read once a
    session and each book takes the gate its mode allows."""

    def __init__(  # noqa: PLR0913 - one keyword per input the run is built from
        self,
        *,
        params: BacktestParams,
        by_date: pl.DataFrame,
        panel: _Panel | None,
        calendar: list[dt.date],
        calendar_days: np.ndarray,
        index: _IndexSeries | None,
        delisted: Mapping[int, dt.date],
    ) -> None:
        self.config = params.config
        self.by_date = by_date
        self.row_days = by_date["date"].cast(pl.Int64).to_numpy() if by_date.height else _days([])
        self.calendar = calendar
        self.calendar_days = calendar_days
        self.index = index
        modes = [GateMode.GATE_OFF, GateMode.BREADTH_ONLY]
        if index is not None:
            modes.append(GateMode.FULL)
        self.books = {
            mode: _Book(mode=mode, params=params, panel=panel, calendar=calendar, delisted=delisted)
            for mode in modes
        }
        self.primary = self.books[GateMode.FULL if index is not None else GateMode.BREADTH_ONLY]

    def session(self, col: int, *, last: bool) -> None:
        day = self.calendar[col]
        for book in self.books.values():
            book.morning(col)
            book.evening(col, day, last=last)
        close = self._close(col, day)
        for book in self.books.values():
            book.settle(day, close, last=last)

    def _close(self, col: int, day: dt.date) -> _Close:
        """Detection, breadth and the index reading, all at ``day``'s close."""
        window, today = self._slices(col)
        found = detect_setups(window, day, self.config) if window.height else None
        detected = 0
        candidates: list[_Candidate] = []
        if found is not None and found.height:
            detected = found.height
            found = found.filter(
                pl.col("status").is_in(list(ENTERABLE_STATUSES))
                & pl.col("setup").is_in([s.value for s in TRADEABLE_SETUPS])
            )
            candidates = _candidates(found)
        if today.height:
            liquid = today.filter(liquid_expr(self.config))
            breadth = breadth_snapshot(
                liquid.select("ret_20", "close", "ma_slow", "high_1y"), self.config.market
            )
        else:
            breadth = BreadthSnapshot(0, 0.0, 0.0, 0.0)
        reading = (
            None if self.index is None else self.index.reading_on(int(self.calendar_days[col]))
        )
        return _Close(candidates=candidates, detected=detected, breadth=breadth, reading=reading)

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


# ---------------------------------------------------------------------------
# The comparison: gate-on against gate-off (A12)
# ---------------------------------------------------------------------------


def _cell(trades: Sequence[BacktestTrade], max_drawdown_pct: Decimal | None) -> GateCell:
    stats = summarize([t.as_closed_trade() for t in trades])
    return GateCell(
        entered=len(trades),
        net_r=stats.net_r,
        expectancy_r=stats.expectancy_r,
        win_rate_pct=stats.win_rate_pct,
        max_drawdown_pct=max_drawdown_pct,
    )


def _delta(with_it: GateCell, without: GateCell) -> GateCell:
    drawdown = (
        None
        if with_it.max_drawdown_pct is None or without.max_drawdown_pct is None
        else with_it.max_drawdown_pct - without.max_drawdown_pct
    )
    return GateCell(
        entered=with_it.entered - without.entered,
        net_r=with_it.net_r - without.net_r,
        expectancy_r=with_it.expectancy_r - without.expectancy_r,
        win_rate_pct=with_it.win_rate_pct - without.win_rate_pct,
        max_drawdown_pct=drawdown,
    )


def _contribution(
    comparison_overall: Mapping[GateMode, GateCell],
    by_year: Mapping[int, Mapping[GateMode, GateCell]],
    by_setup: Mapping[str, Mapping[GateMode, GateCell]],
    *,
    with_it: GateMode,
    without: GateMode,
) -> GateContribution:
    return GateContribution(
        overall=_delta(comparison_overall[with_it], comparison_overall[without]),
        by_year={year: _delta(cells[with_it], cells[without]) for year, cells in by_year.items()},
        by_setup={
            setup: _delta(cells[with_it], cells[without]) for setup, cells in by_setup.items()
        },
    )


def _comparison(run: _Run, params: BacktestParams) -> GateComparison:
    books = run.books
    years = range(params.start.year, params.end.year + 1)
    setups = [setup.value for setup in Setup if setup in TRADEABLE_SETUPS]
    overall = {mode: _cell(book.closed, book.max_drawdown_in(None)) for mode, book in books.items()}
    by_year = {
        year: {
            mode: _cell(
                [t for t in book.closed if t.entry_date.year == year], book.max_drawdown_in(year)
            )
            for mode, book in books.items()
        }
        for year in years
    }
    by_setup = {
        setup: {
            mode: _cell([t for t in book.closed if t.setup == setup], None)
            for mode, book in books.items()
        }
        for setup in setups
    }
    index_supplied = GateMode.FULL in books
    return GateComparison(
        index_supplied=index_supplied,
        primary=run.primary.mode,
        overall=overall,
        by_year=by_year,
        by_setup=by_setup,
        breadth=_contribution(
            overall,
            by_year,
            by_setup,
            with_it=GateMode.BREADTH_ONLY,
            without=GateMode.GATE_OFF,
        ),
        index_rule=(
            _contribution(
                overall, by_year, by_setup, with_it=GateMode.FULL, without=GateMode.BREADTH_ONLY
            )
            if index_supplied
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _check(params: BacktestParams, calendar: Sequence[dt.date], index: pl.DataFrame | None) -> None:
    if params.end < params.start:
        raise ValueError(f"end {params.end} is before start {params.start}")
    if params.sleeve_inr <= _ZERO:
        raise ValueError(f"sleeve_inr must be positive, got {params.sleeve_inr}")
    if params.cost_pct_per_side < _ZERO:
        raise ValueError(f"cost_pct_per_side must not be negative, got {params.cost_pct_per_side}")
    if any(later <= earlier for earlier, later in pairwise(calendar)):
        raise ValueError("calendar must be strictly increasing")
    if index is None and params.index_slug is not None:
        raise ValueError(f"index_slug {params.index_slug!r} names a series that was not supplied")


def run_backtest(
    bars: pl.DataFrame,
    params: BacktestParams,
    *,
    calendar: Sequence[dt.date],
    index: pl.DataFrame | None = None,
    delisted: Mapping[int, dt.date] | None = None,
) -> BacktestResult:
    """`04` §11 over ``[params.start, params.end]``.

    ``bars`` is the adjusted frame ``load_swing_bars`` produces — ``instrument_id``, ``symbol``,
    ``date``, ``open``, ``high``, ``low``, ``close``, ``volume`` and optionally ``turnover``,
    ``upper_circuit``, ``adj_factor`` — covering the lookback before ``start`` as well.
    ``calendar`` is every trading day in order, covering the bars; a session is a calendar day
    inside the run's bounds, and a bar on a date the calendar does not name is read by the
    detectors but never traded on.

    ``index`` is the benchmark's close series (``date``, ``close``) for the index rule of `04`
    §8.2, read in-frame up to and including each session's own close; ``None`` keeps the gate
    breadth-only and the result's caveats say so. ``delisted`` maps ``instrument_id`` to
    ``instrument.delisted_on`` for the names the runner kept past their end (STANDING-ANSWERS
    B2): a held name is sold at its last close on its last bar and counted ``DELISTED``.
    """
    _check(params, calendar, index)
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
        index=None if index is None else _IndexSeries.build(index, params.config),
        delisted={int(k): v for k, v in (delisted or {}).items()},
    )
    for index_, col in enumerate(session_cols):
        run.session(col, last=index_ == len(session_cols) - 1)

    primary = run.primary
    trades = tuple(primary.closed)
    return BacktestResult(
        params=params,
        trades=trades,
        stats=summarize([t.as_closed_trade() for t in trades]),
        by_setup=_by_setup(trades),
        by_year=_by_year(trades, params),
        equity_curve=tuple(primary.equity_curve),
        funnel=dict(primary.funnel),
        ladder=tuple(primary.ladder),
        drawdown=primary.drawdown_summary(),
        comparison=_comparison(run, params),
    )
