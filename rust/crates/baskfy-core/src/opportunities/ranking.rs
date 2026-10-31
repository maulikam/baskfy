//! Port of `baskfy_core.opportunities.ranking`: the visible, weighted 0-100 scores of swing
//! setups and option candidates.

use serde::{Deserialize, Serialize};

use crate::exposure::py_round;
use crate::opportunities::config::{ScoreWeights, SwingParams, TriggerStatus};
use crate::opportunities::swing::Evaluation;

const SCALE: f64 = 100.0;
const RR_FULL: f64 = 3.0;
const RS_FULL_PP: f64 = 30.0;
const LIQUIDITY_FULL_CR: f64 = 100.0;
const VOLUME_FULL_MULTIPLE: f64 = 2.0;
const DIGITS: usize = 4;

/// Ports `ScoreComponent`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScoreComponent {
    /// Stable key.
    pub key: String,
    /// Human label.
    pub label: String,
    /// Normalised value in `[0, 1]` once composed.
    pub value: f64,
    /// Weight (normalised to sum to 1 once composed).
    pub weight: f64,
}

impl ScoreComponent {
    /// A component from its parts.
    #[must_use]
    pub fn new(key: &str, label: &str, value: f64, weight: f64) -> Self {
        Self {
            key: key.to_owned(),
            label: label.to_owned(),
            value,
            weight,
        }
    }

    /// Ports the `contribution` property: points this component adds to the total.
    #[must_use]
    pub fn contribution(&self) -> f64 {
        py_round(self.value * self.weight * SCALE, DIGITS)
    }
}

/// Ports `ScoreBreakdown`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScoreBreakdown {
    /// The 0-100 total, one decimal.
    pub total: f64,
    /// The normalised components.
    pub components: Vec<ScoreComponent>,
}

/// Python's `max(0.0, min(1.0, value))`, NaN read as 0.
fn clip(value: f64) -> f64 {
    if value.is_nan() {
        return 0.0;
    }
    let upper = if value < 1.0 { value } else { 1.0 };
    if upper > 0.0 { upper } else { 0.0 }
}

/// Python's `max(a, b)` on floats: `a` unless `b > a`.
fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Ports `compose`: clip each value to `[0, 1]`, normalise the weights, total out of 100.
#[must_use]
pub fn compose(components: Vec<ScoreComponent>) -> ScoreBreakdown {
    let weight: f64 = components.iter().fold(0.0, |acc, c| acc + c.weight);
    if weight <= 0.0 {
        return ScoreBreakdown {
            total: 0.0,
            components,
        };
    }
    let normalised: Vec<ScoreComponent> = components
        .into_iter()
        .map(|c| ScoreComponent {
            value: py_round(clip(c.value), DIGITS),
            weight: c.weight / weight,
            key: c.key,
            label: c.label,
        })
        .collect();
    let total = normalised
        .iter()
        .fold(0.0, |acc, c| acc + c.value * c.weight)
        * SCALE;
    ScoreBreakdown {
        total: py_round(total, 1),
        components: normalised,
    }
}

/// Ports `score_swing`.
#[allow(clippy::too_many_arguments)]
#[must_use]
pub fn score_swing(
    evaluation: &Evaluation,
    rvol: Option<f64>,
    close: f64,
    sma_mid: Option<f64>,
    sma_mid_prev: Option<f64>,
    sma_long: Option<f64>,
    rs_excess_pct: Option<f64>,
    turnover_cr: f64,
    params: &SwingParams,
) -> ScoreBreakdown {
    let weights: &ScoreWeights = &params.weights;
    let triggered = evaluation.status == TriggerStatus::Triggered;
    let volume = match rvol {
        None => 0.0,
        Some(r) if triggered => r / (VOLUME_FULL_MULTIPLE * params.breakout.volume_multiple),
        Some(r) => {
            let capped = if VOLUME_FULL_MULTIPLE < r {
                VOLUME_FULL_MULTIPLE
            } else {
                r
            };
            1.0 - capped / VOLUME_FULL_MULTIPLE
        }
    };
    let checks = [
        sma_mid.is_some_and(|m| close > m),
        matches!((sma_mid, sma_mid_prev), (Some(m), Some(p)) if m > p),
        sma_long.is_some_and(|l| close > l),
    ];
    let passed = checks.iter().filter(|c| **c).count();
    let trend = passed as f64 / checks.len() as f64;
    let liquidity = if turnover_cr > 1.0 {
        turnover_cr.log10() / LIQUIDITY_FULL_CR.log10()
    } else {
        0.0
    };
    let proximity = if triggered {
        let extension = evaluation.extension_atr.unwrap_or(0.0);
        1.0 - extension / py_max(params.breakout.max_extension_atr, 1e-9)
    } else {
        let distance = evaluation.distance_pct.unwrap_or(0.0);
        1.0 - distance / py_max(params.breakout.near_pivot_pct, 1e-9)
    };
    compose(vec![
        ScoreComponent::new(
            "reward_risk",
            "Reward-to-risk at T1",
            evaluation.risk_reward() / RR_FULL,
            weights.reward_risk,
        ),
        ScoreComponent::new(
            "volume",
            "Volume confirmation (triggered) or dry-up (pending)",
            volume,
            weights.volume,
        ),
        ScoreComponent::new("trend", "Trend alignment (3 checks)", trend, weights.trend),
        ScoreComponent::new(
            "relative_strength",
            "Excess return vs benchmark",
            rs_excess_pct.map_or(0.0, |x| x / RS_FULL_PP),
            weights.relative_strength,
        ),
        ScoreComponent::new(
            "liquidity",
            "Average daily turnover",
            liquidity,
            weights.liquidity,
        ),
        ScoreComponent::new(
            "proximity",
            "Closeness to trigger / limited extension",
            proximity,
            weights.proximity,
        ),
    ])
}

/// Ports `OptionScoreWeights`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct OptionScoreWeights {
    /// Payoff weight.
    pub payoff: f64,
    /// Breakeven weight.
    pub breakeven: f64,
    /// Liquidity weight.
    pub liquidity: f64,
    /// Cost weight.
    pub cost: f64,
    /// Days-to-expiry fit weight.
    pub dte_fit: f64,
}

impl OptionScoreWeights {
    /// Python's defaults.
    pub const DEFAULT: Self = Self {
        payoff: 0.3,
        breakeven: 0.25,
        liquidity: 0.25,
        cost: 0.1,
        dte_fit: 0.1,
    };
}

impl Default for OptionScoreWeights {
    fn default() -> Self {
        Self::DEFAULT
    }
}

/// Ports `DEFAULT_OPTION_WEIGHTS`.
pub const DEFAULT_OPTION_WEIGHTS: OptionScoreWeights = OptionScoreWeights::DEFAULT;

/// Ports `score_option`.
#[allow(clippy::too_many_arguments)]
#[must_use]
pub fn score_option(
    payoff_ratio: Option<f64>,
    breakeven_move_pct: f64,
    expected_move_pct: Option<f64>,
    worst_spread_pct: f64,
    max_spread_pct: f64,
    cost_share: f64,
    dte: i64,
    preferred: (i64, i64),
    weights: &OptionScoreWeights,
) -> ScoreBreakdown {
    let payoff = payoff_ratio.map_or(0.5, |r| r / RR_FULL);
    let breakeven = match expected_move_pct {
        Some(e) if e > 0.0 => 1.0 - py_max(breakeven_move_pct, 0.0) / e,
        _ => 0.5,
    };
    let liquidity = 1.0 - worst_spread_pct / py_max(max_spread_pct, 1e-9);
    let (low, high) = preferred;
    let fit = if low <= dte && dte <= high {
        1.0
    } else {
        let gap = if dte < low { low - dte } else { dte - high };
        1.0 - gap as f64 / low.max(1) as f64
    };
    compose(vec![
        ScoreComponent::new(
            "payoff",
            "Max profit / max loss (neutral when unlimited)",
            payoff,
            weights.payoff,
        ),
        ScoreComponent::new(
            "breakeven",
            "Breakeven move vs model expected move",
            breakeven,
            weights.breakeven,
        ),
        ScoreComponent::new(
            "liquidity",
            "Widest leg spread vs limit",
            liquidity,
            weights.liquidity,
        ),
        ScoreComponent::new(
            "cost",
            "Round-trip cost vs max loss",
            1.0 - cost_share,
            weights.cost,
        ),
        ScoreComponent::new(
            "dte_fit",
            "Days to expiry in preferred window",
            fit,
            weights.dte_fit,
        ),
    ])
}
