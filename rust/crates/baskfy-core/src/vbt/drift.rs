//! Port of `baskfy_core.vbt.drift` (`decile-blueprint/packages/core/src/baskfy_core/vbt/drift.py`).
//!
//! VB9: how far a fresh run sits from the study, and when that is worth saying out loud. The
//! flag fires above one CAGR point in either direction; it says the two numbers stopped
//! agreeing, not which one is wrong.
//!
//! Goldens: `drift.compare` (the `Drift` and its `to_json()`).

use serde::{Deserialize, Serialize};

use crate::vbt::published::{PUBLISHED, PublishedStudy};

/// Ports `DRIFT_CAGR_POINTS`: flagged above one CAGR point, in either direction.
pub const DRIFT_CAGR_POINTS: f64 = 1.0;

/// Ports `baskfy_core.vbt.drift.Drift` — the three deltas (this run minus the study) and the
/// flag, in the shape `vb_backtest_run.drift` stores.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Drift {
    /// CAGR delta, rounded to two places.
    pub cagr_pct_delta: f64,
    /// Max-drawdown delta, rounded to two places.
    pub max_dd_pct_delta: f64,
    /// Trades delta.
    pub trades_delta: i64,
    /// `|cagr_pct_delta| > threshold_cagr_points`.
    pub flagged: bool,
    /// The threshold the flag used.
    pub threshold_cagr_points: f64,
}

impl Drift {
    /// Ports `to_json()`: the same five fields as a JSON object.
    #[must_use]
    pub fn to_json(&self) -> Self {
        self.clone()
    }
}

/// Python's `round(x, 2)` on a float: the double nearest the correctly rounded (half-even on
/// the exact binary value) two-place decimal; a negative value rounding to zero is `-0.0`.
fn py_round2(x: f64) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.2}").parse::<f64>().unwrap_or(x)
}

/// Ports `baskfy_core.vbt.drift.compare`: this run against the study (default [`PUBLISHED`]),
/// deltas rounded to two places, flagged when `|cagr delta| > threshold` (default
/// [`DRIFT_CAGR_POINTS`]).
#[must_use]
pub fn compare(
    cagr_pct: f64,
    max_drawdown_pct: f64,
    trades: i64,
    study: Option<&PublishedStudy>,
    threshold: Option<f64>,
) -> Drift {
    let study = study.unwrap_or(&PUBLISHED);
    let threshold = threshold.unwrap_or(DRIFT_CAGR_POINTS);
    let cagr_delta = py_round2(cagr_pct - study.cagr_pct);
    Drift {
        cagr_pct_delta: cagr_delta,
        max_dd_pct_delta: py_round2(max_drawdown_pct - study.max_drawdown_pct),
        trades_delta: trades.saturating_sub(study.trades),
        flagged: cagr_delta.abs() > threshold,
        threshold_cagr_points: threshold,
    }
}
