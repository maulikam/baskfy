"""Ranking validation — a monthly-rebalance simulator over a quality rank. Contract C8.

docs/ranking/PLAN.md C8: "monthly rebalance, signal at close, fill at next session open, 25 bps a
side, top 20 with entry/retention buffer … Metrics: CAGR net, max DD, annual turnover, mean
max-sector weight …, per-year returns, IS/OOS." This module is the pure half of leaf F; the
runner under ``research/ranking-validation/`` loads the export, computes ranks point in time and
calls :func:`simulate_monthly_rebalance`. Nothing here knows a file, a database or a clock exists
(root CLAUDE.md, law 1).

It is deliberately smaller than :mod:`baskfy_core.backtest`. That engine runs a screen, sizes in
whole shares and carries rupees in ``Decimal``; a factor ablation over a dozen models needs a
fast, scale-free comparison of *rankings*, so the book here is a unitless NAV index starting at
``1.0`` and positions are fractional. The NAV is not money (house rule 9 governs rupees), which is
why it is float — the same departure :mod:`baskfy_core.backtest` records for its price lookup.

The execution model, step for step
----------------------------------
1. **Signal at close.** ``signals`` carries one ranked candidate list per signal date ``d`` (a
   session in the price panel). The decision at ``d`` reads the rows dated ``d`` and the book as it
   stands — never a price, and never a later row. That is the whole of the look-ahead guarantee,
   and ``tests/test_ranking_validation.py`` proves it by editing the future.
2. **Selection is not reimplemented.** Holds, exits and entries come from
   :func:`baskfy_core.ranking_selection.select_portfolio` (contract C5) with ``max_names``,
   ``entry_rank``, ``retention_rank`` and ``max_per_sector``; its retention test is
   :func:`baskfy_core.rank_buffer.inside_hold_band`. A held name absent from ``d``'s list exits
   (``NOT_IN_RESULTS``) — that is how the point-in-time universe filter reaches the book.
3. **Fill at the next session's open.** Target set = ``hold`` + ``enter`` rows. Each target gets
   an equal slot of ``1 / max_names`` of the pre-trade equity ``E`` (marked at the fill open);
   unfilled slots stay in cash, which earns nothing. Holds are resized back to their slot, so
   drift trims count as turnover. An entrant with no open on the fill session is not bought and
   its slot stays cash (``unfilled_entries``).
4. **Costs per side.** Every trade's notional is ``|target - current value|`` measured on ``E``;
   the cost is ``cost_bps_per_side / 10_000`` of it, buys and sells alike. The total cost is taken
   from the book pro rata: post-trade equity ``E' = E - cost`` and each target holds
   ``E' / max_names``. (Charging it pro rata avoids a circular solve and keeps the arithmetic a
   person can do by hand; the difference from solving exactly is second order in the cost.)
5. **Mark to market** at every session's close.
6. **Delisting liquidation.** A held name with no close is carried at its last close. Once it has
   had no close for more than ``missing_bar_tolerance_sessions`` consecutive sessions it is sold
   at that last close, less the per-side cost, on the session the tolerance runs out — a point in
   time rule: it uses only the fact that today's bar is absent, never whether one comes later. It
   is never forward-filled beyond that, which is how survivorship bias would get in. A held name
   with no open on a fill session is *frozen*: carried, not traded, and its slot is withheld from
   selection for that rebalance.

Metrics (all computed on one path; IS and OOS are windows of it, not separate runs)
-----------------------------------------------------------------------------------
* ``cagr_net`` — ``(NAV_end / NAV_start) ** (1 / years) - 1`` with
  :func:`baskfy_core.backtest.year_fraction` (calendar days / 365.25).
* ``max_drawdown`` — the most negative ``NAV / running peak - 1`` inside the window (``≤ 0``).
* ``annual_turnover`` — per trade batch (a rebalance, or a liquidation), one-way turnover is
  ``Σ notional / 2 / equity before the batch``; the window's batches are summed and divided by
  ``years``. Selling a whole book and buying a new one is a turnover of 1, the factsheet
  convention :mod:`baskfy_core.backtest_metrics` uses.
* ``mean_max_sector_weight`` — after each rebalance, the largest classified sector's share of
  ``E'`` (cash included in the denominator); unclassified names are not a sector and do not
  count. A book with no classified name contributes nothing to the mean rather than a flattering
  zero.
* ``per_year`` — NAV at a year's last session over NAV at the previous year's last session (or
  the start), minus one.
* IS is the start through the last session before ``split_date``; OOS starts from that session's
  NAV and runs to the end (PLAN C8: "IS < 2020-01-01 ≤ OOS").

Every float in the result is rounded to :data:`baskfy_core.precision.RATIO_DP` places at the
point the result is built (house rule 8) — ``round()``, as :mod:`baskfy_core.backtest_metrics`
rounds its statistics — and every metric is computed from the unrounded path, so rounding never
compounds.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd

from baskfy_core.backtest import year_fraction
from baskfy_core.precision import RATIO_DP
from baskfy_core.ranking_selection import (
    UNCLASSIFIED_SECTOR,
    Candidate,
    Holding,
    SelectionAction,
    SelectionConstraints,
    select_portfolio,
)

__all__ = [
    "VALIDATION_VERSION",
    "NavPoint",
    "PricePanel",
    "RebalanceRecord",
    "TradeReason",
    "TradeSide",
    "ValidationConfig",
    "ValidationResult",
    "ValidationTrade",
    "WindowMetrics",
    "YearReturn",
    "build_price_panel",
    "monthly_signal_dates",
    "simulate_monthly_rebalance",
    "summary_row",
]

VALIDATION_VERSION: Final = "ranking-validation-2.0.0"

_BASIS_POINTS: Final = 10_000.0
_BARS_COLUMNS: Final = ("date", "instrument_id", "open", "close")
_SIGNAL_COLUMNS: Final = ("date", "instrument_id", "quality_rank")


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TradeReason(StrEnum):
    REBALANCE = "rebalance"
    DELISTED = "delisted"


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    """The C8 knobs. ``max_names=20`` is the contract's "top 20"; the buffer is a default."""

    max_names: int = 20
    entry_rank: int = 20
    retention_rank: int = 40
    max_per_sector: int | None = None
    cost_bps_per_side: float = 25.0
    split_date: dt.date = dt.date(2020, 1, 1)
    missing_bar_tolerance_sessions: int = 5

    def __post_init__(self) -> None:
        # SelectionConstraints owns the max_names / entry / retention / sector-cap rules.
        self.selection_constraints(self.max_names)
        if not (math.isfinite(self.cost_bps_per_side) and self.cost_bps_per_side >= 0):
            raise ValueError("cost_bps_per_side must be a finite value >= 0")
        if self.missing_bar_tolerance_sessions < 0:
            raise ValueError("missing_bar_tolerance_sessions must be >= 0")

    def selection_constraints(self, slots: int) -> SelectionConstraints:
        """C5 constraints for one rebalance with ``slots`` names available to select into."""
        return SelectionConstraints(
            max_names=slots,
            entry_rank=self.entry_rank,
            retention_rank=self.retention_rank,
            max_per_sector=self.max_per_sector,
            capital_inr=None,
            max_adv_participation_pct=None,
        )


@dataclass(frozen=True, eq=False)
class PricePanel:
    """Adjusted open and close, ``sessions x instruments``; NaN where there is no usable price."""

    sessions: tuple[dt.date, ...]
    instrument_ids: tuple[int, ...]
    open: npt.NDArray[np.float64]
    close: npt.NDArray[np.float64]
    column: Mapping[int, int] = field(default_factory=dict)
    row: Mapping[dt.date, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NavPoint:
    date: dt.date
    nav: float


@dataclass(frozen=True, slots=True)
class ValidationTrade:
    date: dt.date
    instrument_id: int
    side: TradeSide
    price: float
    notional: float
    cost: float
    reason: TradeReason


@dataclass(frozen=True, slots=True)
class RebalanceRecord:
    """One rebalance: the decision made at ``signal_date`` and filled at ``fill_date``'s open."""

    signal_date: dt.date
    fill_date: dt.date
    equity_before: float
    cost: float
    turnover: float
    entries: tuple[int, ...]
    exits: tuple[int, ...]
    holds: tuple[int, ...]
    unfilled_entries: tuple[int, ...]
    frozen: tuple[int, ...]
    names_after: int
    cash_weight: float
    max_sector_weight: float | None


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    start: dt.date
    end: dt.date
    years: float
    total_return: float
    cagr_net: float | None
    max_drawdown: float
    annual_turnover: float | None
    mean_max_sector_weight: float | None
    rebalances: int


@dataclass(frozen=True, slots=True)
class YearReturn:
    year: int
    start: dt.date
    end: dt.date
    return_net: float


@dataclass(frozen=True, slots=True)
class ValidationResult:
    config: ValidationConfig
    nav: tuple[NavPoint, ...]
    trades: tuple[ValidationTrade, ...]
    rebalances: tuple[RebalanceRecord, ...]
    full: WindowMetrics
    in_sample: WindowMetrics | None
    out_of_sample: WindowMetrics | None
    per_year: tuple[YearReturn, ...]
    dropped_signal_dates: tuple[dt.date, ...]
    version: str = VALIDATION_VERSION


@dataclass(slots=True)
class _Position:
    shares: float
    last_close: float
    sector: str | None
    missing: int = 0


@dataclass(slots=True)
class _Batch:
    """A trade batch's contribution to turnover, dated for window sums."""

    date: dt.date
    turnover: float


@dataclass(slots=True)
class _OpenBook:
    """The book marked at a fill session's open, before any trade."""

    equity: float
    values: dict[int, float] = field(default_factory=dict)
    opens: dict[int, float] = field(default_factory=dict)
    frozen: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Decision:
    holds: tuple[int, ...]
    entries: tuple[int, ...]
    exits: tuple[int, ...]


def _round(value: float) -> float:
    return round(value, RATIO_DP)


def _round_opt(value: float | None) -> float | None:
    return None if value is None else _round(value)


def _dates(column: pd.Series) -> list[dt.date]:
    return [ts.date() for ts in pd.to_datetime(column)]


def _require(frame: pd.DataFrame, columns: Sequence[str], what: str) -> None:
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise ValueError(f"{what} is missing columns: {', '.join(missing)}")


def build_price_panel(bars: pd.DataFrame) -> PricePanel:
    """Pivot long bars (``date, instrument_id, open, close``; adjusted) into a :class:`PricePanel`.

    Sessions are the distinct dates present. A non-finite or non-positive price is no price.
    Build it once and pass it to every model of an ablation; the pivot is the expensive part.
    """
    _require(bars, _BARS_COLUMNS, "bars")
    frame = pd.DataFrame(
        {
            "date": _dates(bars["date"]),
            "instrument_id": bars["instrument_id"].astype("int64").to_numpy(),
            "open": pd.to_numeric(bars["open"], errors="raise").astype("float64").to_numpy(),
            "close": pd.to_numeric(bars["close"], errors="raise").astype("float64").to_numpy(),
        }
    )
    if frame.duplicated(["date", "instrument_id"]).any():
        raise ValueError("bars has more than one row for an (instrument_id, date)")
    sessions = tuple(sorted(set(frame["date"])))
    ids = tuple(sorted({int(value) for value in frame["instrument_id"]}))

    def wide(values: str) -> npt.NDArray[np.float64]:
        pivot = frame.pivot(index="date", columns="instrument_id", values=values)
        array = pivot.reindex(index=list(sessions), columns=list(ids)).to_numpy(dtype=np.float64)
        return np.where(np.isfinite(array) & (array > 0), array, np.nan)

    return PricePanel(
        sessions=sessions,
        instrument_ids=ids,
        open=wide("open"),
        close=wide("close"),
        column={instrument: index for index, instrument in enumerate(ids)},
        row={session: index for index, session in enumerate(sessions)},
    )


def monthly_signal_dates(sessions: Sequence[dt.date]) -> tuple[dt.date, ...]:
    """The last session of each calendar month whose end is confirmed by a later session.

    The final month in ``sessions`` is excluded: without a session in a later month there is no
    way to know its last session has happened (and no next session to fill at).
    """
    ordered = sorted(set(sessions))
    return tuple(
        day
        for day, following in pairwise(ordered)
        if (day.year, day.month) != (following.year, following.month)
    )


@dataclass(frozen=True, slots=True)
class _SignalRow:
    instrument_id: int
    symbol: str
    quality_rank: int
    sector: str | None


def _signals_by_date(signals: pd.DataFrame, panel: PricePanel) -> dict[dt.date, list[_SignalRow]]:
    _require(signals, _SIGNAL_COLUMNS, "signals")
    dates = _dates(signals["date"])
    ids = [int(value) for value in signals["instrument_id"]]
    ranks = [int(value) for value in signals["quality_rank"]]
    sectors: list[str | None] = (
        [None if pd.isna(value) else str(value) for value in signals["sector"]]
        if "sector" in signals.columns
        else [None] * len(ids)
    )
    symbols = (
        [str(value) for value in signals["symbol"]]
        if "symbol" in signals.columns
        else [str(instrument) for instrument in ids]
    )
    grouped: dict[dt.date, list[_SignalRow]] = {}
    seen: set[tuple[dt.date, int]] = set()
    for day, instrument, rank, sector, symbol in zip(
        dates, ids, ranks, sectors, symbols, strict=True
    ):
        if day not in panel.row:
            raise ValueError(f"signal date {day.isoformat()} is not a session in the price panel")
        if (day, instrument) in seen:
            raise ValueError(f"signals repeats instrument {instrument} on {day.isoformat()}")
        seen.add((day, instrument))
        grouped.setdefault(day, []).append(_SignalRow(instrument, symbol, rank, sector))
    return grouped


def _classified(sector: str | None) -> str | None:
    if sector is None or not sector.strip() or sector == UNCLASSIFIED_SECTOR:
        return None
    return sector


def _price(array: npt.NDArray[np.float64], row: int, column: int | None) -> float | None:
    if column is None:
        return None
    value = float(array[row, column])
    return value if math.isfinite(value) else None


class _Simulator:
    def __init__(self, panel: PricePanel, config: ValidationConfig) -> None:
        self.panel = panel
        self.config = config
        self.rate = config.cost_bps_per_side / _BASIS_POINTS
        self.cash = 1.0
        self.positions: dict[int, _Position] = {}
        self.trades: list[ValidationTrade] = []
        self.rebalances: list[RebalanceRecord] = []
        self.batches: list[_Batch] = []

    def _equity_at_close(self) -> float:
        return self.cash + sum(p.shares * p.last_close for p in self.positions.values())

    def mark_close(self, row: int) -> float:
        day = self.panel.sessions[row]
        expired: list[int] = []
        for instrument in sorted(self.positions):
            position = self.positions[instrument]
            close = _price(self.panel.close, row, self.panel.column.get(instrument))
            if close is not None:
                position.last_close = close
                position.missing = 0
                continue
            position.missing += 1
            if position.missing > self.config.missing_bar_tolerance_sessions:
                expired.append(instrument)
        # Every close is marked before anything is sold, so the equity a liquidation's turnover
        # is measured against does not depend on the order instruments are visited in.
        equity = self._equity_at_close()
        for instrument in expired:
            self._liquidate(day, instrument, equity)
        return self._equity_at_close()

    def _liquidate(self, day: dt.date, instrument: int, equity: float) -> None:
        position = self.positions.pop(instrument)
        notional = position.shares * position.last_close
        cost = notional * self.rate
        self.cash += notional - cost
        self.trades.append(
            ValidationTrade(
                day,
                instrument,
                TradeSide.SELL,
                position.last_close,
                notional,
                cost,
                TradeReason.DELISTED,
            )
        )
        if equity > 0:
            self.batches.append(_Batch(day, notional / 2 / equity))

    def rebalance(self, signal_date: dt.date, row: int, ranked: Sequence[_SignalRow]) -> None:
        """Steps 2-4: decide on ``signal_date``'s list, fill at ``row``'s open, charge costs."""
        fill_date = self.panel.sessions[row]
        book = self._open_book(row)
        decision = self._decide(ranked, book)
        unfilled = tuple(i for i in decision.entries if self._open(row, i) is None)
        for instrument in decision.entries:
            price = self._open(row, instrument)
            if price is not None:
                book.opens[instrument] = price
        entered = tuple(i for i in decision.entries if i not in unfilled)
        targets = frozenset((*decision.holds, *entered))

        slot = 1.0 / self.config.max_names
        tradable = sorted(targets | (set(self.positions) - set(book.frozen)))
        legs = [
            (i, book.values.get(i, 0.0), slot * book.equity if i in targets else 0.0)
            for i in tradable
        ]
        traded = sum(abs(target - current) for _, current, target in legs)
        cost = traded * self.rate
        after = book.equity - cost
        for instrument, current, target in legs:
            if target != current:
                self._record_fill(fill_date, instrument, book.opens[instrument], current, target)

        sector_of = {signal.instrument_id: signal.sector for signal in ranked}
        kept = {instrument: self.positions[instrument] for instrument in book.frozen}
        for instrument in sorted(targets):
            previous = self.positions.get(instrument)
            kept[instrument] = _Position(
                shares=slot * after / book.opens[instrument],
                last_close=book.opens[instrument],
                sector=sector_of.get(instrument, previous.sector if previous else None),
            )
        self.positions = kept
        frozen_value = sum(book.values[i] for i in book.frozen)
        self.cash = after - slot * after * len(targets) - frozen_value
        turnover = traded / 2 / book.equity if book.equity > 0 else 0.0
        self.batches.append(_Batch(fill_date, turnover))
        self.rebalances.append(
            RebalanceRecord(
                signal_date=signal_date,
                fill_date=fill_date,
                equity_before=book.equity,
                cost=cost,
                turnover=turnover,
                entries=tuple(sorted(entered)),
                exits=tuple(sorted(decision.exits)),
                holds=tuple(sorted(decision.holds)),
                unfilled_entries=tuple(sorted(unfilled)),
                frozen=tuple(sorted(book.frozen)),
                names_after=len(kept),
                cash_weight=self.cash / after if after > 0 else 0.0,
                max_sector_weight=self._max_sector_weight(after, slot, targets, book),
            )
        )

    def _open(self, row: int, instrument: int) -> float | None:
        return _price(self.panel.open, row, self.panel.column.get(instrument))

    def _open_book(self, row: int) -> _OpenBook:
        """The book at the fill open; a held name with no open is frozen at its last close."""
        book = _OpenBook(equity=self.cash)
        for instrument, position in self.positions.items():
            price = self._open(row, instrument)
            if price is None:
                book.frozen.append(instrument)
                book.values[instrument] = position.shares * position.last_close
            else:
                book.opens[instrument] = price
                book.values[instrument] = position.shares * price
            book.equity += book.values[instrument]
        return book

    def _decide(self, ranked: Sequence[_SignalRow], book: _OpenBook) -> _Decision:
        """Step 2 — C5 selection over the signal date's list; frozen names keep their slots."""
        frozen = set(book.frozen)
        slots = self.config.max_names - len(frozen)
        if slots < 1:
            exits = tuple(i for i in self.positions if i not in frozen)
            return _Decision(holds=(), entries=(), exits=exits)
        result = select_portfolio(
            (
                Candidate(
                    instrument_id=signal.instrument_id,
                    symbol=signal.symbol,
                    quality_rank=signal.quality_rank,
                    sector=signal.sector,
                )
                for signal in ranked
            ),
            (
                Holding(
                    instrument_id=instrument,
                    symbol=str(instrument),
                    quantity=Decimal(1),
                    sector=position.sector,
                )
                for instrument, position in self.positions.items()
                if instrument not in frozen
            ),
            self.config.selection_constraints(slots),
        )

        def acting(action: SelectionAction) -> tuple[int, ...]:
            return tuple(r.instrument_id for r in result.rows if r.action is action)

        return _Decision(
            holds=acting(SelectionAction.HOLD),
            entries=acting(SelectionAction.ENTER),
            exits=acting(SelectionAction.EXIT),
        )

    def _record_fill(
        self, day: dt.date, instrument: int, price: float, current: float, target: float
    ) -> None:
        notional = abs(target - current)
        side = TradeSide.BUY if target > current else TradeSide.SELL
        self.trades.append(
            ValidationTrade(
                day, instrument, side, price, notional, notional * self.rate, TradeReason.REBALANCE
            )
        )

    def _max_sector_weight(
        self, after: float, slot: float, targets: frozenset[int], book: _OpenBook
    ) -> float | None:
        if after <= 0:
            return None
        sector_values: dict[str, float] = {}
        for instrument, position in self.positions.items():
            name = _classified(position.sector)
            if name is not None:
                value = slot * after if instrument in targets else book.values[instrument]
                sector_values[name] = sector_values.get(name, 0.0) + value
        return max(sector_values.values()) / after if sector_values else None


def simulate_monthly_rebalance(
    panel: PricePanel,
    signals: pd.DataFrame,
    config: ValidationConfig,
) -> ValidationResult:
    """Run the C8 execution model over ``panel`` for the ranked lists in ``signals``.

    ``signals`` columns: ``date`` (a session in ``panel``), ``instrument_id``, ``quality_rank``
    (1 = best), optional ``sector`` and ``symbol``. Each distinct date is one rebalance decision,
    filled at the next session's open; a signal on the panel's last session has no fill and is
    returned in ``dropped_signal_dates``. The runner decides the calendar — use
    :func:`monthly_signal_dates` for the contract's monthly schedule. The NAV starts at ``1.0``
    at the close of the first signal date.
    """
    by_date = _signals_by_date(signals, panel)
    if not by_date:
        raise ValueError("signals is empty; there is nothing to simulate")
    last_row = len(panel.sessions) - 1
    fills: dict[int, dt.date] = {}
    dropped: list[dt.date] = []
    for day in sorted(by_date):
        row = panel.row[day]
        if row == last_row:
            dropped.append(day)
        else:
            fills[row + 1] = day
    if not fills:
        raise ValueError("no signal date has a following session to fill at")

    first = min(panel.row[day] for day in fills.values())
    sim = _Simulator(panel, config)
    dates: list[dt.date] = [panel.sessions[first]]
    navs: list[float] = [1.0]
    for row in range(first + 1, len(panel.sessions)):
        signal_date = fills.get(row)
        if signal_date is not None:
            sim.rebalance(signal_date, row, by_date[signal_date])
        navs.append(sim.mark_close(row))
        dates.append(panel.sessions[row])

    nav = np.asarray(navs, dtype=np.float64)
    full = _window(dates, nav, sim, 0, len(dates) - 1)
    before_split = [index for index, day in enumerate(dates) if day < config.split_date]
    in_sample = out_of_sample = None
    if before_split and before_split[-1] > 0:
        in_sample = _window(dates, nav, sim, 0, before_split[-1])
    if before_split and before_split[-1] < len(dates) - 1:
        out_of_sample = _window(dates, nav, sim, before_split[-1], len(dates) - 1)

    return ValidationResult(
        config=config,
        nav=tuple(NavPoint(day, _round(value)) for day, value in zip(dates, navs, strict=True)),
        trades=tuple(
            ValidationTrade(
                t.date,
                t.instrument_id,
                t.side,
                _round(t.price),
                _round(t.notional),
                _round(t.cost),
                t.reason,
            )
            for t in sim.trades
        ),
        rebalances=tuple(_rounded_record(record) for record in sim.rebalances),
        full=full,
        in_sample=in_sample,
        out_of_sample=out_of_sample,
        per_year=_per_year(dates, nav),
        dropped_signal_dates=tuple(dropped),
    )


def _rounded_record(record: RebalanceRecord) -> RebalanceRecord:
    return RebalanceRecord(
        signal_date=record.signal_date,
        fill_date=record.fill_date,
        equity_before=_round(record.equity_before),
        cost=_round(record.cost),
        turnover=_round(record.turnover),
        entries=record.entries,
        exits=record.exits,
        holds=record.holds,
        unfilled_entries=record.unfilled_entries,
        frozen=record.frozen,
        names_after=record.names_after,
        cash_weight=_round(record.cash_weight),
        max_sector_weight=_round_opt(record.max_sector_weight),
    )


def _cagr(total: float, years: float) -> float | None:
    if years <= 0 or total < 0:
        return None
    if total == 0:
        return -1.0
    return float(total ** (1.0 / years)) - 1.0


def _window(
    dates: Sequence[dt.date],
    nav: npt.NDArray[np.float64],
    sim: _Simulator,
    start: int,
    end: int,
) -> WindowMetrics:
    first, last = dates[start], dates[end]
    path = nav[start : end + 1]
    years = float(year_fraction(first, last))
    total = float(path[-1] / path[0]) if path[0] > 0 else math.nan
    peaks = np.maximum.accumulate(path)
    drawdown = float(np.min(path / peaks - 1.0)) if bool(np.all(peaks > 0)) else -1.0
    turnover = sum(batch.turnover for batch in sim.batches if first < batch.date <= last)
    inside = [record for record in sim.rebalances if first < record.fill_date <= last]
    sectors = [r.max_sector_weight for r in inside if r.max_sector_weight is not None]
    return WindowMetrics(
        start=first,
        end=last,
        years=_round(years),
        total_return=_round(total - 1.0),
        cagr_net=_round_opt(_cagr(total, years)),
        max_drawdown=_round(drawdown),
        annual_turnover=_round(turnover / years) if years > 0 else None,
        mean_max_sector_weight=_round(sum(sectors) / len(sectors)) if sectors else None,
        rebalances=len(inside),
    )


def _per_year(dates: Sequence[dt.date], nav: npt.NDArray[np.float64]) -> tuple[YearReturn, ...]:
    last_of_year: dict[int, int] = {}
    for index, day in enumerate(dates):
        last_of_year[day.year] = index
    out: list[YearReturn] = []
    base = 0
    for year in sorted(last_of_year):
        end = last_of_year[year]
        if end > base:
            out.append(
                YearReturn(
                    year=year,
                    start=dates[base],
                    end=dates[end],
                    return_net=_round(float(nav[end] / nav[base]) - 1.0),
                )
            )
        base = end
    return tuple(out)


def summary_row(model: str, result: ValidationResult) -> dict[str, str | int | float | None]:
    """One flat row per model — the shape of ``research/ranking-validation/out/ablation.csv``."""
    row: dict[str, str | int | float | None] = {
        "model": model,
        "entry_rank": result.config.entry_rank,
        "retention_rank": result.config.retention_rank,
        "max_names": result.config.max_names,
        "cost_bps_per_side": result.config.cost_bps_per_side,
    }
    windows = (("full", result.full), ("is", result.in_sample), ("oos", result.out_of_sample))
    for prefix, window in windows:
        row[f"{prefix}_start"] = window.start.isoformat() if window else None
        row[f"{prefix}_end"] = window.end.isoformat() if window else None
        row[f"{prefix}_cagr_net"] = window.cagr_net if window else None
        row[f"{prefix}_max_drawdown"] = window.max_drawdown if window else None
        row[f"{prefix}_annual_turnover"] = window.annual_turnover if window else None
        row[f"{prefix}_mean_max_sector_weight"] = window.mean_max_sector_weight if window else None
    for year in result.per_year:
        row[f"return_{year.year}"] = year.return_net
    row["version"] = result.version
    return row
