from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

import polars as pl

from baskfy_core.opportunities.config import (
    DEFAULT_SWING_PARAMS,
    SetupType,
    SwingFilters,
    SwingParams,
    TriggerStatus,
)
from baskfy_core.opportunities.ranking import ScoreBreakdown, score_swing

_PCT: Final = 100.0
_CRORE: Final = 10_000_000.0
_PRICE_Q: Final = Decimal("0.01")
_RATIO_Q: Final = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Evidence:
    key: str
    label: str
    value: float | None
    threshold: float | None
    unit: str
    passed: bool


@dataclass(frozen=True, slots=True)
class Target:
    label: str
    price: float
    r_multiple: float


@dataclass(frozen=True, slots=True)
class Evaluation:
    setup: SetupType
    status: TriggerStatus
    trigger_condition: str
    trigger_level: float
    entry_basis: str
    entry: float
    invalidation: float
    targets: tuple[Target, ...]
    evidence: tuple[Evidence, ...]
    horizon: tuple[int, int]
    extension_atr: float | None
    distance_pct: float | None

    @property
    def risk(self) -> float:
        return self.entry - self.invalidation

    @property
    def reward(self) -> float:
        return self.targets[0].price - self.entry

    @property
    def risk_reward(self) -> float:
        return self.reward / self.risk if self.risk > 0 else 0.0


@dataclass(frozen=True, slots=True)
class InstrumentContext:
    name: str = ""
    sector: str | None = None
    missing_sessions: int = 0
    recent_actions: tuple[tuple[dt.date, str], ...] = ()
    upcoming_actions: tuple[tuple[dt.date, str], ...] = ()
    in_fno_ban: bool = False


@dataclass(frozen=True, slots=True)
class SwingOpportunity:
    instrument_id: int
    symbol: str
    name: str
    sector: str | None
    evaluation: Evaluation
    close: Decimal
    entry: Decimal
    trigger_level: Decimal
    invalidation: Decimal
    targets: tuple[tuple[str, Decimal, Decimal], ...]
    risk_per_share: Decimal
    reward_per_share: Decimal
    risk_reward: Decimal
    atr: Decimal
    atr_pct: Decimal
    hv_pct: Decimal | None
    avg_turnover_cr: Decimal
    avg_volume: int
    rs_excess_pct: Decimal | None
    rs_lookback: int
    score: ScoreBreakdown
    data_as_of: dt.date
    limitations: tuple[str, ...]
    actionable: bool

    @property
    def id(self) -> str:
        return f"{self.evaluation.setup.value}:{self.instrument_id}:{self.data_as_of.isoformat()}"


@dataclass(frozen=True, slots=True)
class ScanCounts:
    universe: int = 0
    evaluated: int = 0
    insufficient_history: int = 0
    qualified: int = 0
    pending: int = 0
    triggered: int = 0


@dataclass(frozen=True, slots=True)
class SwingScanResult:
    as_of: dt.date | None
    counts: ScanCounts
    opportunities: tuple[SwingOpportunity, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _num(row: Mapping[str, object], key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        out = float(value)
        return None if math.isnan(out) or math.isinf(out) else out
    return None


def _ok(value: float | None, predicate: bool) -> bool:
    return value is not None and predicate


def effective_horizon(
    setup: SetupType, params: SwingParams, filters: SwingFilters
) -> tuple[int, int] | None:
    low, high = params.horizon_for(setup)
    start = max(low, filters.horizon_min)
    end = min(high, filters.horizon_max)
    return (start, end) if start <= end else None


def _targets(
    entry: float, stop: float, structural: float | None, reach: float, extension_r: float
) -> tuple[Target, ...]:
    risk = entry - stop
    first = min(structural, reach) if structural is not None and structural > entry else reach
    label = (
        "T1 structural"
        if structural is not None and structural > entry and structural <= reach
        else "T1 ATR reach"
    )
    out = [Target(label, first, (first - entry) / risk if risk > 0 else 0.0)]
    second = entry + extension_r * risk
    if structural is not None and structural > first:
        second = max(second, structural)
    if second > first:
        out.append(Target("T2 extension", second, (second - entry) / risk if risk > 0 else 0.0))
    return tuple(out)


def _reach(entry: float, atr: float, horizon: tuple[int, int], params: SwingParams) -> float:
    return entry + params.risk.reach_atr_per_sqrt_session * atr * math.sqrt(horizon[1])


def evaluate_breakout(
    row: Mapping[str, object], params: SwingParams, horizon: tuple[int, int]
) -> Evaluation | None:
    p = params.breakout
    close, pivot, base_low = _num(row, "close"), _num(row, "pivot"), _num(row, "base_low")
    atr, sma_mid, rvol = _num(row, "atr"), _num(row, "sma_mid"), _num(row, "rvol")
    if close is None or pivot is None or base_low is None or atr is None or atr <= 0:
        return None
    depth = (pivot - base_low) / pivot * _PCT
    trend = _ok(sma_mid, sma_mid is not None and close > sma_mid)
    depth_ok = depth <= p.max_base_depth_pct
    extension = (close - pivot) / atr
    volume_ok = _ok(rvol, rvol is not None and rvol >= p.volume_multiple)
    distance = (pivot - close) / close * _PCT
    triggered = close > pivot and volume_ok and extension <= p.max_extension_atr
    pending = close <= pivot and distance <= p.near_pivot_pct
    if not (trend and depth_ok and (triggered or pending)):
        return None
    status = TriggerStatus.TRIGGERED if triggered else TriggerStatus.PENDING
    entry = close if triggered else pivot
    stop = entry - params.risk.stop_atr * atr
    evidence = (
        Evidence("trend", "Close above 50-session average", close, sma_mid, "₹", trend),
        Evidence("base_depth", "Base depth", depth, p.max_base_depth_pct, "%", depth_ok),
        Evidence(
            "close_vs_pivot",
            f"Close vs {p.pivot_lookback}-session pivot",
            close,
            pivot,
            "₹",
            close > pivot,
        ),
        Evidence("rvol", "Volume vs 20-session average", rvol, p.volume_multiple, "x", volume_ok),
        Evidence(
            "extension",
            "Extension above pivot",
            extension if triggered else None,
            p.max_extension_atr,
            "ATR",
            triggered,
        ),
    )
    return Evaluation(
        setup=SetupType.BREAKOUT,
        status=status,
        trigger_condition=(
            f"Daily close above {pivot:.2f} with volume at least {p.volume_multiple}x "
            "the 20-session average"
        ),
        trigger_level=pivot,
        entry_basis="CLOSE_OF_TRIGGER_BAR" if triggered else "TRIGGER_LEVEL",
        entry=entry,
        invalidation=stop,
        targets=_targets(
            entry,
            stop,
            entry + (pivot - base_low),
            _reach(entry, atr, horizon, params),
            params.risk.extension_r,
        ),
        evidence=evidence,
        horizon=horizon,
        extension_atr=extension if triggered else None,
        distance_pct=None if triggered else distance,
    )


def evaluate_pullback(
    row: Mapping[str, object], params: SwingParams, horizon: tuple[int, int]
) -> Evaluation | None:
    p = params.pullback
    close, high, prev_high = _num(row, "close"), _num(row, "high"), _num(row, "prev_high")
    atr, sma_mid, sma_prev = _num(row, "atr"), _num(row, "sma_mid"), _num(row, "sma_mid_prev")
    sma_long, swing_high = _num(row, "sma_long"), _num(row, "swing_high")
    recent_low = _num(row, "recent_low")
    touched = bool(row.get("touched_fast"))
    if (
        close is None
        or high is None
        or prev_high is None
        or atr is None
        or atr <= 0
        or swing_high is None
        or recent_low is None
        or sma_mid is None
        or sma_prev is None
    ):
        return None
    above = close > sma_mid
    rising = sma_mid > sma_prev
    long_ok = sma_long is None or sma_mid > sma_long
    depth = (swing_high - close) / swing_high * _PCT
    depth_ok = p.min_depth_pct <= depth <= p.max_depth_pct
    if not (above and rising and long_ok and depth_ok and touched):
        return None
    triggered = close > prev_high
    status = TriggerStatus.TRIGGERED if triggered else TriggerStatus.PENDING
    trigger_level = prev_high if triggered else high
    entry = close if triggered else high
    stop = recent_low - params.risk.stop_buffer_atr * atr
    if stop >= entry:
        return None
    evidence = (
        Evidence("trend", "Close above 50-session average", close, sma_mid, "₹", above),
        Evidence("slope", "50-session average rising", sma_mid, sma_prev, "₹", rising),
        Evidence("long_trend", "50 above 200-session average", sma_mid, sma_long, "₹", long_ok),
        Evidence(
            "depth",
            f"Pullback from {p.swing_lookback}-session high",
            depth,
            p.max_depth_pct,
            "%",
            depth_ok,
        ),
        Evidence("touch", "Tested 20-session average", None, p.touch_atr, "ATR", touched),
        Evidence("reversal", "Close above prior bar high", close, prev_high, "₹", triggered),
    )
    return Evaluation(
        setup=SetupType.PULLBACK,
        status=status,
        trigger_condition=f"Price trades above {trigger_level:.2f} (reversal bar high)",
        trigger_level=trigger_level,
        entry_basis="CLOSE_OF_TRIGGER_BAR" if triggered else "TRIGGER_LEVEL",
        entry=entry,
        invalidation=stop,
        targets=_targets(
            entry,
            stop,
            swing_high,
            _reach(entry, atr, horizon, params),
            params.risk.extension_r,
        ),
        evidence=evidence,
        horizon=horizon,
        extension_atr=(close - prev_high) / atr if triggered else None,
        distance_pct=None if triggered else (high - close) / close * _PCT,
    )


def evaluate_relative_strength(
    row: Mapping[str, object], params: SwingParams, horizon: tuple[int, int]
) -> Evaluation | None:
    p = params.relative_strength
    close, atr, sma_mid = _num(row, "close"), _num(row, "atr"), _num(row, "sma_mid")
    excess, rs_line = _num(row, "rs_excess_pct"), _num(row, "rs_line")
    rs_high, trigger = _num(row, "rs_line_high"), _num(row, "rs_trigger")
    if (
        close is None
        or atr is None
        or atr <= 0
        or excess is None
        or rs_line is None
        or rs_high is None
        or trigger is None
        or sma_mid is None
    ):
        return None
    excess_ok = excess >= p.min_excess_pct
    near_high = rs_line >= rs_high * (1.0 - p.rs_near_high_pct / _PCT)
    above = close > sma_mid
    if not (excess_ok and near_high and above):
        return None
    triggered = close > trigger
    entry = close if triggered else trigger
    stop = entry - params.risk.stop_atr * atr
    evidence = (
        Evidence(
            "excess",
            f"{p.lookback}-session return above benchmark",
            excess,
            p.min_excess_pct,
            "pp",
            excess_ok,
        ),
        Evidence(
            "rs_high",
            "Relative-strength line near its high",
            (rs_line / rs_high - 1.0) * _PCT,
            -p.rs_near_high_pct,
            "%",
            near_high,
        ),
        Evidence("trend", "Close above 50-session average", close, sma_mid, "₹", above),
        Evidence(
            "trigger",
            f"Close above {p.trigger_lookback}-session high",
            close,
            trigger,
            "₹",
            triggered,
        ),
    )
    return Evaluation(
        setup=SetupType.RELATIVE_STRENGTH,
        status=TriggerStatus.TRIGGERED if triggered else TriggerStatus.PENDING,
        trigger_condition=f"Daily close above {trigger:.2f} ({p.trigger_lookback}-session high)",
        trigger_level=trigger,
        entry_basis="CLOSE_OF_TRIGGER_BAR" if triggered else "TRIGGER_LEVEL",
        entry=entry,
        invalidation=stop,
        targets=_targets(
            entry, stop, None, _reach(entry, atr, horizon, params), params.risk.extension_r
        ),
        evidence=evidence,
        horizon=horizon,
        extension_atr=(close - trigger) / atr if triggered else None,
        distance_pct=None if triggered else (trigger - close) / close * _PCT,
    )


_EVALUATORS: Final = {
    SetupType.BREAKOUT: evaluate_breakout,
    SetupType.PULLBACK: evaluate_pullback,
    SetupType.RELATIVE_STRENGTH: evaluate_relative_strength,
}


def evaluate(
    row: Mapping[str, object], setup: SetupType, params: SwingParams, horizon: tuple[int, int]
) -> Evaluation | None:
    return _EVALUATORS[setup](row, params, horizon)


def _price(value: float, factor: float) -> Decimal:
    return Decimal(str(value / factor)).quantize(_PRICE_Q, rounding=ROUND_HALF_UP)


def _ratio(value: float) -> Decimal:
    return Decimal(str(value)).quantize(_RATIO_Q, rounding=ROUND_HALF_UP)


def limitations_for(
    row: Mapping[str, object], context: InstrumentContext, params: SwingParams
) -> tuple[list[str], bool]:
    notes: list[str] = []
    blocking = False
    bars = int(_num(row, "bars") or 0)
    if bars < params.data.long_trend_bars:
        notes.append(
            f"Only {bars} sessions of history; the {params.data.long_trend_bars}-session "
            "trend check was skipped."
        )
    if context.missing_sessions > 0:
        notes.append(
            f"{context.missing_sessions} trading session(s) missing in the last "
            f"{params.data.window_sessions}; indicators span the gap."
        )
        if context.missing_sessions > params.data.max_missing_sessions:
            blocking = True
    for day, kind in context.recent_actions:
        notes.append(
            f"Corporate action ({kind}) on {day.isoformat()}; levels use adjusted prices "
            "and are shown in today's price terms."
        )
    for day, kind in context.upcoming_actions:
        notes.append(f"Upcoming corporate action ({kind}) on {day.isoformat()} inside the horizon.")
    if _num(row, "bench_level") is None:
        notes.append("Benchmark level missing for this session; relative strength unavailable.")
    if context.in_fno_ban:
        notes.append("Stock is in the F&O ban list for the next session.")
    return notes, blocking


def build_opportunity(
    row: Mapping[str, object],
    evaluation: Evaluation,
    context: InstrumentContext,
    params: SwingParams,
) -> SwingOpportunity:
    factor = _num(row, "adj_factor") or 1.0
    atr = _num(row, "atr") or 0.0
    close = _num(row, "close") or 0.0
    excess = _num(row, "rs_excess_pct")
    turnover = (_num(row, "turnover_avg") or 0.0) / _CRORE
    notes, blocking = limitations_for(row, context, params)
    if evaluation.status is TriggerStatus.TRIGGERED:
        notes.append(
            "Triggered on the completed bar; the next session may open away from the entry "
            "assumption."
        )
    score = score_swing(
        evaluation=evaluation,
        rvol=_num(row, "rvol"),
        close=close,
        sma_mid=_num(row, "sma_mid"),
        sma_mid_prev=_num(row, "sma_mid_prev"),
        sma_long=_num(row, "sma_long"),
        rs_excess_pct=excess,
        turnover_cr=turnover,
        params=params,
    )
    date = row.get("date")
    if not isinstance(date, dt.date):
        raise ValueError("indicator row has no date")
    return SwingOpportunity(
        instrument_id=int(_num(row, "instrument_id") or 0),
        symbol=str(row.get("symbol") or ""),
        name=context.name,
        sector=context.sector,
        evaluation=evaluation,
        close=_price(close, factor),
        entry=_price(evaluation.entry, factor),
        trigger_level=_price(evaluation.trigger_level, factor),
        invalidation=_price(evaluation.invalidation, factor),
        targets=tuple(
            (t.label, _price(t.price, factor), _ratio(t.r_multiple)) for t in evaluation.targets
        ),
        risk_per_share=_price(evaluation.risk, factor),
        reward_per_share=_price(evaluation.reward, factor),
        risk_reward=_ratio(evaluation.risk_reward),
        atr=_price(atr, factor),
        atr_pct=_ratio(_num(row, "atr_pct") or 0.0),
        hv_pct=None if _num(row, "hv_pct") is None else _ratio(_num(row, "hv_pct") or 0.0),
        avg_turnover_cr=_ratio(turnover),
        avg_volume=round(_num(row, "volume_avg") or 0.0),
        rs_excess_pct=None if excess is None else _ratio(excess),
        rs_lookback=params.relative_strength.lookback,
        score=score,
        data_as_of=date,
        limitations=tuple(notes),
        actionable=not blocking,
    )


def passes_filters(
    row: Mapping[str, object],
    context: InstrumentContext,
    filters: SwingFilters,
) -> bool:
    factor = _num(row, "adj_factor") or 1.0
    close = (_num(row, "close") or 0.0) / factor
    atr_pct = _num(row, "atr_pct")
    turnover = (_num(row, "turnover_avg") or 0.0) / _CRORE
    if filters.sectors and context.sector not in filters.sectors:
        return False
    if close < filters.min_price or turnover < filters.min_turnover_cr:
        return False
    return atr_pct is not None and filters.atr_pct_min <= atr_pct <= filters.atr_pct_max


def scan(
    indicated: pl.DataFrame,
    as_of: dt.date,
    contexts: Mapping[int, InstrumentContext],
    filters: SwingFilters,
    params: SwingParams = DEFAULT_SWING_PARAMS,
) -> SwingScanResult:
    today = indicated.filter(pl.col("date") == as_of)
    universe = today.height
    horizons = {
        setup: h
        for setup in filters.setups
        if (h := effective_horizon(setup, params, filters)) is not None
    }
    warnings: list[str] = []
    skipped = sorted(s.value for s in filters.setups if s not in horizons)
    if skipped:
        warnings.append(
            f"Setups {', '.join(skipped)} were skipped: their holding horizon does not overlap "
            f"{filters.horizon_min}-{filters.horizon_max} sessions."
        )
    insufficient = 0
    evaluated = 0
    found: list[SwingOpportunity] = []
    for row in today.iter_rows(named=True):
        if int(row.get("bars") or 0) < params.data.min_bars:
            insufficient += 1
            continue
        context = contexts.get(int(row["instrument_id"]), InstrumentContext())
        if not passes_filters(row, context, filters):
            continue
        evaluated += 1
        for setup, horizon in horizons.items():
            evaluation = evaluate(row, setup, params, horizon)
            if evaluation is None or evaluation.risk <= 0:
                continue
            if evaluation.risk_reward < filters.min_rr:
                continue
            found.append(build_opportunity(row, evaluation, context, params))
    if any(o.rs_excess_pct is None for o in found):
        warnings.append("Benchmark series incomplete; relative-strength readings are partial.")
    ranked = rank(found)
    return SwingScanResult(
        as_of=as_of if universe else None,
        counts=ScanCounts(
            universe=universe,
            evaluated=evaluated,
            insufficient_history=insufficient,
            qualified=len(ranked),
            pending=sum(1 for o in ranked if o.evaluation.status is TriggerStatus.PENDING),
            triggered=sum(1 for o in ranked if o.evaluation.status is TriggerStatus.TRIGGERED),
        ),
        opportunities=tuple(ranked[: filters.limit]),
        warnings=tuple(warnings),
    )


def rank(opportunities: Iterable[SwingOpportunity]) -> list[SwingOpportunity]:
    return sorted(
        opportunities,
        key=lambda o: (-o.score.total, o.symbol, o.evaluation.setup.value),
    )


def missing_sessions(present: Sequence[dt.date], calendar: Sequence[dt.date], window: int) -> int:
    if not present:
        return 0
    first = min(present)
    have = set(present)
    expected = [day for day in sorted(calendar)[-window:] if day >= first]
    return sum(1 for day in expected if day not in have)
