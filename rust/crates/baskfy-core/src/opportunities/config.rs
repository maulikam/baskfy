//! Port of `baskfy_core.opportunities.config`: setup/status enums and the swing and options
//! parameter sets.

use std::collections::BTreeSet;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// Ports `SetupType`. Declaration order is also the order of the values' text.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SetupType {
    /// `BREAKOUT`.
    Breakout,
    /// `PULLBACK`.
    Pullback,
    /// `RELATIVE_STRENGTH`.
    RelativeStrength,
}

impl SetupType {
    /// Every member, in declaration order.
    pub const ALL: [Self; 3] = [Self::Breakout, Self::Pullback, Self::RelativeStrength];

    /// The enum's string value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Breakout => "BREAKOUT",
            Self::Pullback => "PULLBACK",
            Self::RelativeStrength => "RELATIVE_STRENGTH",
        }
    }
}

/// Ports `TriggerStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TriggerStatus {
    /// `PENDING`.
    Pending,
    /// `TRIGGERED`.
    Triggered,
}

impl TriggerStatus {
    /// The enum's string value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Pending => "PENDING",
            Self::Triggered => "TRIGGERED",
        }
    }
}

/// Ports `DataStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DataStatus {
    /// `FRESH`.
    Fresh,
    /// `STALE`.
    Stale,
    /// `UNAVAILABLE`.
    Unavailable,
}

impl DataStatus {
    /// The enum's string value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Fresh => "FRESH",
            Self::Stale => "STALE",
            Self::Unavailable => "UNAVAILABLE",
        }
    }
}

/// Ports `Direction`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Direction {
    /// `BULLISH`.
    Bullish,
    /// `BEARISH`.
    Bearish,
}

impl Direction {
    /// The enum's string value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Bullish => "BULLISH",
            Self::Bearish => "BEARISH",
        }
    }
}

/// Ports `StrategyKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum StrategyKind {
    /// `LONG_CALL`.
    LongCall,
    /// `LONG_PUT`.
    LongPut,
    /// `BULL_CALL_SPREAD`.
    BullCallSpread,
    /// `BEAR_PUT_SPREAD`.
    BearPutSpread,
}

impl StrategyKind {
    /// The enum's string value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::LongCall => "LONG_CALL",
            Self::LongPut => "LONG_PUT",
            Self::BullCallSpread => "BULL_CALL_SPREAD",
            Self::BearPutSpread => "BEAR_PUT_SPREAD",
        }
    }
}

/// Ports `BULLISH_STRATEGIES`.
pub const BULLISH_STRATEGIES: [StrategyKind; 2] =
    [StrategyKind::LongCall, StrategyKind::BullCallSpread];

/// Ports `BEARISH_STRATEGIES`.
pub const BEARISH_STRATEGIES: [StrategyKind; 2] =
    [StrategyKind::LongPut, StrategyKind::BearPutSpread];

/// Ports `BreakoutParams`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct BreakoutParams {
    /// Sessions behind the pivot high.
    pub pivot_lookback: i64,
    /// Relative volume a triggered breakout needs.
    pub volume_multiple: f64,
    /// Largest extension above the pivot, in ATR.
    pub max_extension_atr: f64,
    /// Pending when the close is within this % below the pivot.
    pub near_pivot_pct: f64,
    /// Deepest base allowed, in %.
    pub max_base_depth_pct: f64,
    /// Holding horizon, low end (sessions).
    pub horizon_min: i64,
    /// Holding horizon, high end (sessions).
    pub horizon_max: i64,
}

impl BreakoutParams {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        pivot_lookback: 20,
        volume_multiple: 1.5,
        max_extension_atr: 1.0,
        near_pivot_pct: 3.0,
        max_base_depth_pct: 20.0,
        horizon_min: 5,
        horizon_max: 20,
    };
}

impl Default for BreakoutParams {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `PullbackParams`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct PullbackParams {
    /// Sessions behind the swing high.
    pub swing_lookback: i64,
    /// Sessions the pullback low and the touch look back.
    pub pullback_bars: i64,
    /// A touch is the low within this many ATR of the 20-session average.
    pub touch_atr: f64,
    /// Shallowest pullback, in %.
    pub min_depth_pct: f64,
    /// Deepest pullback, in %.
    pub max_depth_pct: f64,
    /// Sessions over which the 50-session average must rise.
    pub slope_bars: i64,
    /// Holding horizon, low end (sessions).
    pub horizon_min: i64,
    /// Holding horizon, high end (sessions).
    pub horizon_max: i64,
}

impl PullbackParams {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        swing_lookback: 20,
        pullback_bars: 5,
        touch_atr: 1.0,
        min_depth_pct: 3.0,
        max_depth_pct: 15.0,
        slope_bars: 10,
        horizon_min: 3,
        horizon_max: 12,
    };
}

impl Default for PullbackParams {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `RelativeStrengthParams`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct RelativeStrengthParams {
    /// Sessions of the excess-return comparison.
    pub lookback: i64,
    /// Minimum excess return over the benchmark, in percentage points.
    pub min_excess_pct: f64,
    /// RS line within this % of its high.
    pub rs_near_high_pct: f64,
    /// Sessions behind the trigger high.
    pub trigger_lookback: i64,
    /// Holding horizon, low end (sessions).
    pub horizon_min: i64,
    /// Holding horizon, high end (sessions).
    pub horizon_max: i64,
}

impl RelativeStrengthParams {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        lookback: 60,
        min_excess_pct: 10.0,
        rs_near_high_pct: 3.0,
        trigger_lookback: 10,
        horizon_min: 10,
        horizon_max: 30,
    };
}

impl Default for RelativeStrengthParams {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `RiskParams`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct RiskParams {
    /// ATR window.
    pub atr_bars: i64,
    /// Stop distance below entry, in ATR.
    pub stop_atr: f64,
    /// Buffer below a structural stop, in ATR.
    pub stop_buffer_atr: f64,
    /// ATR reach per square-root session of the horizon.
    pub reach_atr_per_sqrt_session: f64,
    /// Second target, in R.
    pub extension_r: f64,
}

impl RiskParams {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        atr_bars: 14,
        stop_atr: 1.5,
        stop_buffer_atr: 0.1,
        reach_atr_per_sqrt_session: 1.0,
        extension_r: 2.0,
    };
}

impl Default for RiskParams {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `DataRules`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct DataRules {
    /// Fewest sessions a name needs to be scanned.
    pub min_bars: i64,
    /// Window of the long trend average.
    pub long_trend_bars: i64,
    /// Sessions the missing-session count looks back.
    pub window_sessions: i64,
    /// More missing sessions than this makes an opportunity not actionable.
    pub max_missing_sessions: i64,
    /// Sessions a corporate action stays "recent".
    pub corporate_action_lookback_sessions: i64,
}

impl DataRules {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        min_bars: 60,
        long_trend_bars: 200,
        window_sessions: 60,
        max_missing_sessions: 2,
        corporate_action_lookback_sessions: 60,
    };
}

impl Default for DataRules {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `ScoreWeights`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ScoreWeights {
    /// Reward-to-risk weight.
    pub reward_risk: f64,
    /// Volume weight.
    pub volume: f64,
    /// Trend weight.
    pub trend: f64,
    /// Relative-strength weight.
    pub relative_strength: f64,
    /// Liquidity weight.
    pub liquidity: f64,
    /// Proximity weight.
    pub proximity: f64,
}

impl ScoreWeights {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        reward_risk: 0.25,
        volume: 0.15,
        trend: 0.2,
        relative_strength: 0.15,
        liquidity: 0.1,
        proximity: 0.15,
    };
}

impl Default for ScoreWeights {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `SwingParams`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SwingParams {
    /// Breakout setup.
    pub breakout: BreakoutParams,
    /// Pullback setup.
    pub pullback: PullbackParams,
    /// Relative-strength setup.
    pub relative_strength: RelativeStrengthParams,
    /// Stops and targets.
    pub risk: RiskParams,
    /// Data sufficiency rules.
    pub data: DataRules,
    /// Score weights.
    pub weights: ScoreWeights,
}

impl SwingParams {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        breakout: BreakoutParams::DEFAULT,
        pullback: PullbackParams::DEFAULT,
        relative_strength: RelativeStrengthParams::DEFAULT,
        risk: RiskParams::DEFAULT,
        data: DataRules::DEFAULT,
        weights: ScoreWeights::DEFAULT,
    };

    /// Ports `horizon_for`: the setup's own holding horizon.
    #[must_use]
    pub const fn horizon_for(&self, setup: SetupType) -> (i64, i64) {
        match setup {
            SetupType::Breakout => (self.breakout.horizon_min, self.breakout.horizon_max),
            SetupType::Pullback => (self.pullback.horizon_min, self.pullback.horizon_max),
            SetupType::RelativeStrength => (
                self.relative_strength.horizon_min,
                self.relative_strength.horizon_max,
            ),
        }
    }
}

impl Default for SwingParams {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `DEFAULT_SWING_PARAMS`.
pub const DEFAULT_SWING_PARAMS: SwingParams = SwingParams::DEFAULT;

/// Ports `SwingFilters`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SwingFilters {
    /// Setups to evaluate.
    pub setups: BTreeSet<SetupType>,
    /// Sectors to keep; empty keeps every sector.
    pub sectors: BTreeSet<String>,
    /// Minimum 20-session average turnover, in crore.
    pub min_turnover_cr: f64,
    /// Minimum close, unadjusted.
    pub min_price: f64,
    /// Requested horizon, low end (sessions).
    pub horizon_min: i64,
    /// Requested horizon, high end (sessions).
    pub horizon_max: i64,
    /// Lowest ATR %.
    pub atr_pct_min: f64,
    /// Highest ATR %.
    pub atr_pct_max: f64,
    /// Minimum reward-to-risk at T1.
    pub min_rr: f64,
    /// Opportunities returned (a Python slice bound).
    pub limit: i64,
}

impl Default for SwingFilters {
    fn default() -> Self {
        Self {
            setups: SetupType::ALL.into_iter().collect(),
            sectors: BTreeSet::new(),
            min_turnover_cr: 5.0,
            min_price: 50.0,
            horizon_min: 5,
            horizon_max: 20,
            atr_pct_min: 1.0,
            atr_pct_max: 8.0,
            min_rr: 1.5,
            limit: 50,
        }
    }
}

/// Ports `QuoteRules`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct QuoteRules {
    /// Widest bid/ask spread, in %.
    pub max_spread_pct: Decimal,
    /// Oldest usable quote, in seconds.
    pub max_quote_age_s: i64,
    /// Minimum open interest.
    pub min_oi: i64,
    /// Minimum traded volume.
    pub min_volume: i64,
}

impl QuoteRules {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        max_spread_pct: Decimal::from_parts(10, 0, 0, false, 0),
        max_quote_age_s: 120,
        min_oi: 500,
        min_volume: 1,
    };
}

impl Default for QuoteRules {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `OptionsParams`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct OptionsParams {
    /// Expiries considered.
    pub max_expiries: i64,
    /// Strikes each side of spot.
    pub strikes_each_side: i64,
    /// Widest spread, in strike steps.
    pub max_spread_width_steps: i64,
    /// Payoff chart range around spot, in %.
    pub payoff_range_pct: Decimal,
    /// Points on the payoff chart.
    pub payoff_points: i64,
    /// Model risk-free rate.
    pub model_rate: f64,
    /// IV solver lower bound.
    pub iv_lower: f64,
    /// IV solver upper bound.
    pub iv_upper: f64,
    /// IV solver tolerance.
    pub iv_tolerance: f64,
    /// Smallest premium the IV solver accepts.
    pub min_model_premium: f64,
    /// Preferred days to expiry, low end.
    pub preferred_dte_min: i64,
    /// Preferred days to expiry, high end.
    pub preferred_dte_max: i64,
    /// Candidates returned.
    pub max_candidates: i64,
}

impl OptionsParams {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        max_expiries: 2,
        strikes_each_side: 4,
        max_spread_width_steps: 4,
        payoff_range_pct: Decimal::from_parts(20, 0, 0, false, 0),
        payoff_points: 41,
        model_rate: 0.0,
        iv_lower: 0.01,
        iv_upper: 5.0,
        iv_tolerance: 1e-6,
        min_model_premium: 0.5,
        preferred_dte_min: 14,
        preferred_dte_max: 45,
        max_candidates: 30,
    };
}

impl Default for OptionsParams {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `DEFAULT_OPTIONS_PARAMS`.
pub const DEFAULT_OPTIONS_PARAMS: OptionsParams = OptionsParams::DEFAULT;
