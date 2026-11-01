from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final


class SetupType(StrEnum):
    BREAKOUT = "BREAKOUT"
    PULLBACK = "PULLBACK"
    RELATIVE_STRENGTH = "RELATIVE_STRENGTH"


class TriggerStatus(StrEnum):
    PENDING = "PENDING"
    TRIGGERED = "TRIGGERED"


class DataStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class Direction(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class StrategyKind(StrEnum):
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"


BULLISH_STRATEGIES: Final = frozenset({StrategyKind.LONG_CALL, StrategyKind.BULL_CALL_SPREAD})
BEARISH_STRATEGIES: Final = frozenset({StrategyKind.LONG_PUT, StrategyKind.BEAR_PUT_SPREAD})


@dataclass(frozen=True, slots=True)
class BreakoutParams:
    pivot_lookback: int = 20
    volume_multiple: float = 1.5
    max_extension_atr: float = 1.0
    near_pivot_pct: float = 3.0
    max_base_depth_pct: float = 20.0
    horizon_min: int = 5
    horizon_max: int = 20


@dataclass(frozen=True, slots=True)
class PullbackParams:
    swing_lookback: int = 20
    pullback_bars: int = 5
    touch_atr: float = 1.0
    min_depth_pct: float = 3.0
    max_depth_pct: float = 15.0
    slope_bars: int = 10
    horizon_min: int = 3
    horizon_max: int = 12


@dataclass(frozen=True, slots=True)
class RelativeStrengthParams:
    lookback: int = 60
    min_excess_pct: float = 10.0
    rs_near_high_pct: float = 3.0
    trigger_lookback: int = 10
    horizon_min: int = 10
    horizon_max: int = 30


@dataclass(frozen=True, slots=True)
class RiskParams:
    atr_bars: int = 14
    stop_atr: float = 1.5
    stop_buffer_atr: float = 0.1
    reach_atr_per_sqrt_session: float = 1.0
    extension_r: float = 2.0


@dataclass(frozen=True, slots=True)
class DataRules:
    min_bars: int = 60
    long_trend_bars: int = 200
    window_sessions: int = 60
    max_missing_sessions: int = 2
    corporate_action_lookback_sessions: int = 60


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    reward_risk: float = 0.25
    volume: float = 0.15
    trend: float = 0.2
    relative_strength: float = 0.15
    liquidity: float = 0.1
    proximity: float = 0.15


@dataclass(frozen=True, slots=True)
class SwingParams:
    breakout: BreakoutParams = field(default_factory=BreakoutParams)
    pullback: PullbackParams = field(default_factory=PullbackParams)
    relative_strength: RelativeStrengthParams = field(default_factory=RelativeStrengthParams)
    risk: RiskParams = field(default_factory=RiskParams)
    data: DataRules = field(default_factory=DataRules)
    weights: ScoreWeights = field(default_factory=ScoreWeights)

    def horizon_for(self, setup: SetupType) -> tuple[int, int]:
        if setup is SetupType.BREAKOUT:
            return self.breakout.horizon_min, self.breakout.horizon_max
        if setup is SetupType.PULLBACK:
            return self.pullback.horizon_min, self.pullback.horizon_max
        return self.relative_strength.horizon_min, self.relative_strength.horizon_max


DEFAULT_SWING_PARAMS: Final = SwingParams()


@dataclass(frozen=True, slots=True)
class SwingFilters:
    setups: frozenset[SetupType] = frozenset(SetupType)
    sectors: frozenset[str] = frozenset()
    min_turnover_cr: float = 5.0
    min_price: float = 50.0
    horizon_min: int = 5
    horizon_max: int = 20
    atr_pct_min: float = 1.0
    atr_pct_max: float = 8.0
    min_rr: float = 1.5
    limit: int = 50


@dataclass(frozen=True, slots=True)
class QuoteRules:
    max_spread_pct: Decimal = Decimal("10")
    max_quote_age_s: int = 120
    min_oi: int = 500
    min_volume: int = 1


@dataclass(frozen=True, slots=True)
class OptionsParams:
    max_expiries: int = 2
    strikes_each_side: int = 4
    max_spread_width_steps: int = 4
    payoff_range_pct: Decimal = Decimal("20")
    payoff_points: int = 41
    model_rate: float = 0.0
    iv_lower: float = 0.01
    iv_upper: float = 5.0
    iv_tolerance: float = 1e-6
    min_model_premium: float = 0.5
    preferred_dte_min: int = 14
    preferred_dte_max: int = 45
    max_candidates: int = 30


DEFAULT_OPTIONS_PARAMS: Final = OptionsParams()
