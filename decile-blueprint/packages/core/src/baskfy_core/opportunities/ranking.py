from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from baskfy_core.opportunities.config import ScoreWeights, SwingParams, TriggerStatus

if TYPE_CHECKING:
    from baskfy_core.opportunities.swing import Evaluation

_SCALE: Final = 100.0
_RR_FULL: Final = 3.0
_RS_FULL_PP: Final = 30.0
_LIQUIDITY_FULL_CR: Final = 100.0
_VOLUME_FULL_MULTIPLE: Final = 2.0
_DIGITS: Final = 4


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    key: str
    label: str
    value: float
    weight: float

    @property
    def contribution(self) -> float:
        return round(self.value * self.weight * _SCALE, _DIGITS)


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    total: float
    components: tuple[ScoreComponent, ...]


def _clip(value: float) -> float:
    if math.isnan(value):
        return 0.0
    return max(0.0, min(1.0, value))


def compose(components: Sequence[ScoreComponent]) -> ScoreBreakdown:
    weight = sum(c.weight for c in components)
    if weight <= 0:
        return ScoreBreakdown(0.0, tuple(components))
    normalised = tuple(
        ScoreComponent(c.key, c.label, round(_clip(c.value), _DIGITS), c.weight / weight)
        for c in components
    )
    total = sum(c.value * c.weight for c in normalised) * _SCALE
    return ScoreBreakdown(round(total, 1), normalised)


def score_swing(  # noqa: PLR0913 - each input is one visible score factor
    *,
    evaluation: Evaluation,
    rvol: float | None,
    close: float,
    sma_mid: float | None,
    sma_mid_prev: float | None,
    sma_long: float | None,
    rs_excess_pct: float | None,
    turnover_cr: float,
    params: SwingParams,
) -> ScoreBreakdown:
    weights: ScoreWeights = params.weights
    triggered = evaluation.status is TriggerStatus.TRIGGERED
    if rvol is None:
        volume = 0.0
    elif triggered:
        volume = rvol / (_VOLUME_FULL_MULTIPLE * params.breakout.volume_multiple)
    else:
        volume = 1.0 - min(rvol, _VOLUME_FULL_MULTIPLE) / _VOLUME_FULL_MULTIPLE
    checks = [
        sma_mid is not None and close > sma_mid,
        sma_mid is not None and sma_mid_prev is not None and sma_mid > sma_mid_prev,
        sma_long is not None and close > sma_long,
    ]
    trend = sum(1.0 for c in checks if c) / len(checks)
    liquidity = (
        math.log10(turnover_cr) / math.log10(_LIQUIDITY_FULL_CR) if turnover_cr > 1.0 else 0.0
    )
    if triggered:
        extension = evaluation.extension_atr or 0.0
        proximity = 1.0 - extension / max(params.breakout.max_extension_atr, 1e-9)
    else:
        distance = evaluation.distance_pct or 0.0
        proximity = 1.0 - distance / max(params.breakout.near_pivot_pct, 1e-9)
    return compose(
        [
            ScoreComponent(
                "reward_risk",
                "Reward-to-risk at T1",
                evaluation.risk_reward / _RR_FULL,
                weights.reward_risk,
            ),
            ScoreComponent(
                "volume",
                "Volume confirmation (triggered) or dry-up (pending)",
                volume,
                weights.volume,
            ),
            ScoreComponent("trend", "Trend alignment (3 checks)", trend, weights.trend),
            ScoreComponent(
                "relative_strength",
                "Excess return vs benchmark",
                0.0 if rs_excess_pct is None else rs_excess_pct / _RS_FULL_PP,
                weights.relative_strength,
            ),
            ScoreComponent("liquidity", "Average daily turnover", liquidity, weights.liquidity),
            ScoreComponent(
                "proximity",
                "Closeness to trigger / limited extension",
                proximity,
                weights.proximity,
            ),
        ]
    )


@dataclass(frozen=True, slots=True)
class OptionScoreWeights:
    payoff: float = 0.3
    breakeven: float = 0.25
    liquidity: float = 0.25
    cost: float = 0.1
    dte_fit: float = 0.1


DEFAULT_OPTION_WEIGHTS: Final = OptionScoreWeights()


def score_option(  # noqa: PLR0913 - each input is one visible score factor
    *,
    payoff_ratio: float | None,
    breakeven_move_pct: float,
    expected_move_pct: float | None,
    worst_spread_pct: float,
    max_spread_pct: float,
    cost_share: float,
    dte: int,
    preferred: tuple[int, int],
    weights: OptionScoreWeights = DEFAULT_OPTION_WEIGHTS,
) -> ScoreBreakdown:
    payoff = 0.5 if payoff_ratio is None else payoff_ratio / _RR_FULL
    if expected_move_pct is not None and expected_move_pct > 0:
        breakeven = 1.0 - max(breakeven_move_pct, 0.0) / expected_move_pct
    else:
        breakeven = 0.5
    liquidity = 1.0 - worst_spread_pct / max(max_spread_pct, 1e-9)
    low, high = preferred
    if low <= dte <= high:
        fit = 1.0
    else:
        gap = low - dte if dte < low else dte - high
        fit = 1.0 - gap / max(low, 1)
    return compose(
        [
            ScoreComponent(
                "payoff",
                "Max profit / max loss (neutral when unlimited)",
                payoff,
                weights.payoff,
            ),
            ScoreComponent(
                "breakeven",
                "Breakeven move vs model expected move",
                breakeven,
                weights.breakeven,
            ),
            ScoreComponent("liquidity", "Widest leg spread vs limit", liquidity, weights.liquidity),
            ScoreComponent("cost", "Round-trip cost vs max loss", 1.0 - cost_share, weights.cost),
            ScoreComponent("dte_fit", "Days to expiry in preferred window", fit, weights.dte_fit),
        ]
    )
