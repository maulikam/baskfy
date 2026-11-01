//! Port of `baskfy_core.twt.drift` — TW9: how far a fresh run sits from the study, and when that
//! is worth saying out loud (`03` §9, `05` §3).
//!
//! Every delta is **this run minus the study**, in percentage points to two places, half-up
//! (`Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_UP)`). Flagged strictly above one
//! CAGR point, either direction. The flag is not a verdict.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::twt::published::{PublishedStudy, decimal_of_float};

/// Ports `DRIFT_CAGR_POINTS`: flagged above one CAGR point (`Decimal("1.0")`).
#[must_use]
pub fn drift_cagr_points() -> Decimal {
    Decimal::new(10, 1)
}

/// A `float | Decimal` input, as `compare` accepts either.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Points {
    /// A Python `float`.
    Float(f64),
    /// A Python `Decimal`.
    Decimal(Decimal),
}

/// Ports `baskfy_core.twt.drift.Drift`: the three deltas, the flag, and both numbers the banner
/// names.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Drift {
    /// Run minus study, CAGR points.
    pub cagr_pct_delta: Decimal,
    /// Run minus study, drawdown points.
    pub max_dd_pct_delta: Decimal,
    /// Run minus study, trades.
    pub trades_delta: i64,
    /// `abs(cagr_pct_delta) > threshold`.
    pub flagged: bool,
    /// The study's CAGR, two places.
    pub published_cagr_pct: Decimal,
    /// The run's CAGR, two places.
    pub run_cagr_pct: Decimal,
    /// The threshold applied.
    pub threshold_cagr_points: Decimal,
}

/// Ports `Drift.to_json`: the JSONB shape `tw_backtest_run.drift` stores — each Decimal as
/// Python's `str()` of it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DriftJson {
    /// `str(cagr_pct_delta)`.
    pub cagr_pct_delta: String,
    /// `str(max_dd_pct_delta)`.
    pub max_dd_pct_delta: String,
    /// The trades delta.
    pub trades_delta: i64,
    /// The flag.
    pub flagged: bool,
    /// `str(published_cagr_pct)`.
    pub published_cagr_pct: String,
    /// `str(run_cagr_pct)`.
    pub run_cagr_pct: String,
    /// `str(threshold_cagr_points)`.
    pub threshold_cagr_points: String,
}

impl Drift {
    /// Ports `Drift.to_json`.
    #[must_use]
    pub fn to_json(&self) -> DriftJson {
        let s = |d: Decimal| WideDecimal::from(d).to_sci_string();
        DriftJson {
            cagr_pct_delta: s(self.cagr_pct_delta),
            max_dd_pct_delta: s(self.max_dd_pct_delta),
            trades_delta: self.trades_delta,
            flagged: self.flagged,
            published_cagr_pct: s(self.published_cagr_pct),
            run_cagr_pct: s(self.run_cagr_pct),
            threshold_cagr_points: s(self.threshold_cagr_points),
        }
    }
}

fn arith(e: impl std::fmt::Display) -> CoreError {
    CoreError::Arithmetic(e.to_string())
}

/// `_points`: a percentage point to two places, half-up, without a second float round-trip.
fn points(value: Points) -> CoreResult<WideDecimal> {
    let exact = match value {
        Points::Float(x) => decimal_of_float(x)?,
        // `Decimal(str(d))` is `d` itself, scale and sign kept.
        Points::Decimal(d) => WideDecimal::from(d),
    };
    exact.quantize(-2, Rounding::HalfUp).map_err(arith)
}

fn out(value: &WideDecimal) -> CoreResult<Decimal> {
    value.try_to_decimal().map_err(arith)
}

/// Ports `compare`: this run against the study, in percentage points.
///
/// # Errors
/// [`CoreError::Arithmetic`] for a non-finite float (Python's `InvalidOperation`), or a value
/// beyond `Decimal`'s range.
pub fn compare(
    cagr_pct: Points,
    max_drawdown_pct: Points,
    trades: i64,
    study: &PublishedStudy,
    threshold: Decimal,
) -> CoreResult<Drift> {
    let run_cagr = points(cagr_pct)?;
    let published_cagr = points(Points::Float(study.cagr_pct))?;
    let delta = run_cagr.sub(&published_cagr);
    let dd_delta = points(max_drawdown_pct)?.sub(&points(Points::Float(study.max_drawdown_pct))?);
    let flagged = delta.abs() > WideDecimal::from(threshold);
    let trades_delta = trades
        .checked_sub(study.trades)
        .ok_or_else(|| CoreError::Arithmetic("trades delta overflows".to_owned()))?;
    Ok(Drift {
        cagr_pct_delta: out(&delta)?,
        max_dd_pct_delta: out(&dd_delta)?,
        trades_delta,
        flagged,
        published_cagr_pct: out(&published_cagr)?,
        run_cagr_pct: out(&run_cagr)?,
        threshold_cagr_points: threshold,
    })
}
