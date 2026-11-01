from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import polars as pl

from baskfy_core.costs import cost_pct
from baskfy_core.opportunities.config import (
    DEFAULT_SWING_PARAMS,
    SetupType,
    SwingFilters,
    SwingParams,
    TriggerStatus,
)
from baskfy_core.opportunities.swing import InstrumentContext, evaluate, passes_filters

_BPS: Final = 10_000.0
_PCT: Final = 100.0
NOTIONAL_INR: Final = 100_000.0


class ExitReason:
    STOP = "STOP"
    TARGET = "TARGET"
    TIME = "TIME"


@dataclass(frozen=True, slots=True)
class SignalTrade:
    instrument_id: int
    signal_date: dt.date
    entry_date: dt.date
    exit_date: dt.date
    entry: float
    stop: float
    target: float
    exit: float
    reason: str
    net_return_pct: float
    r_multiple: float
    min_r: float


@dataclass(frozen=True, slots=True)
class YearStats:
    year: int
    trades: int
    avg_r: float | None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    setup: SetupType
    start: dt.date
    end: dt.date
    trades: tuple[SignalTrade, ...]
    skipped_gap: int
    assumptions: tuple[str, ...]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.r_multiple > 0)

    @property
    def hit_rate_pct(self) -> float | None:
        return self.wins / len(self.trades) * _PCT if self.trades else None

    @property
    def avg_return_pct(self) -> float | None:
        return _mean([t.net_return_pct for t in self.trades])

    @property
    def avg_r(self) -> float | None:
        return _mean([t.r_multiple for t in self.trades])

    @property
    def max_adverse_r(self) -> float | None:
        return min((t.min_r for t in self.trades), default=None)

    @property
    def by_year(self) -> tuple[YearStats, ...]:
        grouped: dict[int, list[float]] = defaultdict(list)
        for trade in self.trades:
            grouped[trade.signal_date.year].append(trade.r_multiple)
        return tuple(
            YearStats(year, len(values), _mean(values)) for year, values in sorted(grouped.items())
        )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def members_as_of(membership: Mapping[dt.date, frozenset[int]], day: dt.date) -> frozenset[int]:
    dates = sorted(membership)
    index = bisect.bisect_right(dates, day) - 1
    return membership[dates[index]] if index >= 0 else frozenset()


def round_trip_cost_pct(slippage_bps: float) -> float:
    fees = cost_pct(NOTIONAL_INR, "BUY") + cost_pct(NOTIONAL_INR, "SELL")
    return fees + 2.0 * slippage_bps / _BPS * _PCT


def simulate(
    rows: Sequence[Mapping[str, object]],
    signal_index: int,
    stop: float,
    target: float,
    horizon: int,
) -> tuple[int, float, str, float] | None:
    entry_row = signal_index + 1
    if entry_row >= len(rows):
        return None
    entry = float(_f(rows[entry_row], "open"))
    risk = entry - stop
    if risk <= 0 or entry >= target:
        return None
    worst = 0.0
    last = min(entry_row + horizon - 1, len(rows) - 1)
    for index in range(entry_row, last + 1):
        bar = rows[index]
        open_, high, low = _f(bar, "open"), _f(bar, "high"), _f(bar, "low")
        worst = min(worst, (low - entry) / risk)
        if low <= stop:
            fill = open_ if index > entry_row and open_ < stop else stop
            return index, fill, ExitReason.STOP, worst
        if high >= target:
            fill = open_ if index > entry_row and open_ > target else target
            return index, fill, ExitReason.TARGET, worst
    return last, _f(rows[last], "close"), ExitReason.TIME, worst


def _f(row: Mapping[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"bar is missing {key}")


def validate_setup(  # noqa: PLR0913 - the test's full specification is explicit
    indicated: pl.DataFrame,
    setup: SetupType,
    start: dt.date,
    end: dt.date,
    *,
    membership: Mapping[dt.date, frozenset[int]] | None,
    filters: SwingFilters,
    slippage_bps: float,
    params: SwingParams = DEFAULT_SWING_PARAMS,
) -> ValidationResult:
    low, high = params.horizon_for(setup)
    horizon = (max(low, filters.horizon_min), min(high, filters.horizon_max))
    if horizon[0] > horizon[1]:
        horizon = (low, high)
    costs = round_trip_cost_pct(slippage_bps)
    trades: list[SignalTrade] = []
    skipped = 0
    for frame in indicated.sort(["instrument_id", "date"]).partition_by(
        "instrument_id", maintain_order=True
    ):
        rows = frame.to_dicts()
        busy_until = -1
        for index, row in enumerate(rows):
            day = row["date"]
            if not isinstance(day, dt.date) or day < start or day > end or index <= busy_until:
                continue
            instrument_id = int(row["instrument_id"])
            if membership is not None and instrument_id not in members_as_of(membership, day):
                continue
            if int(row.get("bars") or 0) < params.data.min_bars:
                continue
            if not passes_filters(row, InstrumentContext(), filters):
                continue
            evaluation = evaluate(row, setup, params, horizon)
            if evaluation is None or evaluation.status is not TriggerStatus.TRIGGERED:
                continue
            target = evaluation.targets[0].price
            outcome = simulate(rows, index, evaluation.invalidation, target, horizon[1])
            if outcome is None:
                if index + 1 < len(rows):
                    skipped += 1
                continue
            exit_index, fill, reason, worst = outcome
            entry_row = rows[index + 1]
            entry = _f(entry_row, "open")
            gross_pct = (fill / entry - 1.0) * _PCT
            net_pct = gross_pct - costs
            risk_pct = (entry - evaluation.invalidation) / entry * _PCT
            exit_day = rows[exit_index]["date"]
            entry_day = entry_row["date"]
            if not isinstance(exit_day, dt.date) or not isinstance(entry_day, dt.date):
                continue
            trades.append(
                SignalTrade(
                    instrument_id=instrument_id,
                    signal_date=day,
                    entry_date=entry_day,
                    exit_date=exit_day,
                    entry=entry,
                    stop=evaluation.invalidation,
                    target=target,
                    exit=fill,
                    reason=reason,
                    net_return_pct=net_pct,
                    r_multiple=net_pct / risk_pct,
                    min_r=worst,
                )
            )
            busy_until = exit_index
    return ValidationResult(
        setup=setup,
        start=start,
        end=end,
        trades=tuple(trades),
        skipped_gap=skipped,
        assumptions=(
            "Signals use completed daily bars only; indicators are trailing windows.",
            "Entry at the next session's open; skipped when it opens at or below invalidation "
            "or at or above the first target.",
            "Exit at invalidation, first target, or the close after the horizon; a bar touching "
            "both counts as the stop.",
            f"Costs: NSE delivery fees on a ₹{NOTIONAL_INR:,.0f} notional plus "
            f"{slippage_bps:g} bps slippage per side ({costs:.3f}% round trip).",
            "Universe membership is point-in-time when the index has membership rows.",
            "Prices are today's adjusted series; adjustments use corporate actions known now.",
        ),
    )
