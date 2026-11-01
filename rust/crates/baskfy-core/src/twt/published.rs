//! Port of `baskfy_core.twt.published` — the study's own answers, as a record
//! (`docs/twt/01-method.md` §6-§7). **Not parameters**: every number is a result the research
//! measured once, read off `research/tight-close/out/final_metrics.json` on 11 Sep 2026 (plus the
//! two gate-off and three ₹5-crore-floor figures from `01` §7).

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::WideDecimal;
use crate::error::{CoreError, CoreResult};
use crate::py::float_repr as py_float_repr;

/// Ports `baskfy_core.twt.published.PublishedStudy` — `out/final_metrics.json`, typed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishedStudy {
    /// First session of the study.
    pub start: String,
    /// Last session of the study.
    pub end: String,
    /// Years covered.
    pub years: f64,
    /// The headline CAGR, percent.
    pub cagr_pct: f64,
    /// Maximum drawdown, percent.
    pub max_drawdown_pct: f64,
    /// CAGR over drawdown.
    pub calmar: f64,
    /// Sharpe ratio.
    pub sharpe: f64,
    /// Round trips.
    pub trades: i64,
    /// Winning share, percent.
    pub win_rate_pct: f64,
    /// Gross profit over gross loss.
    pub profit_factor: f64,
    /// Mean winner, percent.
    pub avg_win_pct: f64,
    /// Mean loser, percent.
    pub avg_loss_pct: f64,
    /// Mean trade, percent.
    pub avg_trade_pct: f64,
    /// Mean hold, sessions.
    pub avg_hold_sessions: f64,
    /// Time invested, percent.
    pub exposure_pct: f64,
    /// Final equity, rupees (a string, as the study printed it).
    pub final_equity_inr: String,
    /// In-sample CAGR, percent.
    pub in_sample_cagr_pct: f64,
    /// In-sample drawdown, percent.
    pub in_sample_dd_pct: f64,
    /// Out-of-sample CAGR, percent.
    pub out_of_sample_cagr_pct: f64,
    /// Out-of-sample drawdown, percent.
    pub out_of_sample_dd_pct: f64,
    /// `01` §7: CAGR without the breadth gate.
    pub gate_off_cagr_pct: f64,
    /// `01` §7: drawdown without the breadth gate.
    pub gate_off_max_drawdown_pct: f64,
    /// `01` §7: CAGR at the ₹5 crore floor.
    pub shipped_floor_cagr_pct: f64,
    /// `01` §7: drawdown at the ₹5 crore floor.
    pub shipped_floor_max_drawdown_pct: f64,
    /// `01` §7: trades at the ₹5 crore floor.
    pub shipped_floor_trades: i64,
}

impl PublishedStudy {
    /// Ports `PublishedStudy.cagr`: the headline as `Decimal(str(cagr_pct))`.
    ///
    /// # Errors
    /// [`CoreError::Arithmetic`] when the float is not finite or does not fit a `Decimal`.
    pub fn cagr(&self) -> CoreResult<Decimal> {
        decimal_of_float(self.cagr_pct)?
            .try_to_decimal()
            .map_err(|e| CoreError::Arithmetic(e.to_string()))
    }
}

/// Ports `PUBLISHED`: the study's record (`01` §6, §7).
#[must_use]
pub fn published() -> PublishedStudy {
    PublishedStudy {
        start: "2017-10-16".to_owned(),
        end: "2026-09-09".to_owned(),
        years: 8.9,
        cagr_pct: 20.92,
        max_drawdown_pct: -24.7,
        calmar: 0.85,
        sharpe: 1.21,
        trades: 164,
        win_rate_pct: 40.9,
        profit_factor: 2.71,
        avg_win_pct: 55.57,
        avg_loss_pct: -12.35,
        avg_trade_pct: 15.4,
        avg_hold_sessions: 104.6,
        exposure_pct: 78.0,
        final_equity_inr: "5413123.00".to_owned(),
        in_sample_cagr_pct: 11.1,
        in_sample_dd_pct: -20.8,
        out_of_sample_cagr_pct: 36.1,
        out_of_sample_dd_pct: -24.7,
        gate_off_cagr_pct: 17.2,
        gate_off_max_drawdown_pct: -43.0,
        shipped_floor_cagr_pct: 22.5,
        shipped_floor_max_drawdown_pct: -27.0,
        shipped_floor_trades: 169,
    }
}

/// `Decimal(str(x))` for a finite float, exactly (scale included: `20.0` is `Decimal('20.0')`).
pub(crate) fn decimal_of_float(x: f64) -> CoreResult<WideDecimal> {
    if !x.is_finite() {
        return Err(CoreError::Arithmetic(format!(
            "[<class 'decimal.InvalidOperation'>]: {}",
            py_float_repr(x)
        )));
    }
    crate::py::wide_from_float_str(x).map_err(|e| CoreError::Arithmetic(e.to_string()))
}
