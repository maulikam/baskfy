//! Port of `baskfy_core.vbt.published` (`decile-blueprint/packages/core/src/baskfy_core/vbt/published.py`).
//!
//! The study's own answers, as a record (`research/volume-breakout/STRATEGY.md` §4-§5). These
//! are **not** parameters: every number is a result the research measured once. The tests on
//! the Python side read the study's file and check this record against it.
//!
//! Goldens: `published.PUBLISHED` (the record and its `cagr` Decimal).

use std::borrow::Cow;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::CoreResult;
use crate::vbt::exits::wide;

/// Ports `baskfy_core.vbt.published.PublishedStudy` — `out/final_metrics.json`, typed. Every
/// field is a measurement, never a setting.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PublishedStudy {
    /// The window's first session.
    pub start: Cow<'static, str>,
    /// The window's last session.
    pub end: Cow<'static, str>,
    /// Years covered (8.9, not nine).
    pub years: f64,
    /// The headline CAGR, percent.
    pub cagr_pct: f64,
    /// The worst drawdown, percent (negative).
    pub max_drawdown_pct: f64,
    /// Closed trades.
    pub trades: i64,
    /// Winners, percent.
    pub win_rate_pct: f64,
    /// Gross profit over gross loss.
    pub profit_factor: f64,
    /// Average holding period, sessions.
    pub avg_hold_sessions: f64,
    /// Time invested, percent.
    pub exposure_pct: f64,
    /// Annualised Sharpe.
    pub sharpe: f64,
    /// In-sample CAGR (`01` §4).
    pub in_sample_cagr_pct: f64,
    /// In-sample drawdown.
    pub in_sample_dd_pct: f64,
    /// Out-of-sample CAGR.
    pub out_of_sample_cagr_pct: f64,
    /// Out-of-sample drawdown.
    pub out_of_sample_dd_pct: f64,
    /// 761 fills of 908 offered orders, measured by `BacktestResult.fill_rate_pct()`.
    pub modelled_fill_rate_pct: f64,
}

impl PublishedStudy {
    /// Ports the `cagr` property: the headline as `Decimal(str(cagr_pct))`.
    ///
    /// # Errors
    /// A non-finite `cagr_pct`.
    pub fn cagr(&self) -> CoreResult<Decimal> {
        // `Decimal(str(x))` keeps repr's ".0" on a whole float: `26.0` is `Decimal("26.0")`.
        wide::to_dec(&wide::from_py_float_str(self.cagr_pct)?)
    }
}

/// Ports `baskfy_core.vbt.published.PUBLISHED` — STRATEGY §4, read off
/// `research/volume-breakout/out/final_metrics.json` on 10 Sep 2026.
pub const PUBLISHED: PublishedStudy = PublishedStudy {
    start: Cow::Borrowed("2017-10-16"),
    end: Cow::Borrowed("2026-09-09"),
    years: 8.9,
    cagr_pct: 18.23,
    max_drawdown_pct: -27.94,
    trades: 761,
    win_rate_pct: 37.8,
    profit_factor: 1.55,
    avg_hold_sessions: 18.3,
    exposure_pct: 63.2,
    sharpe: 0.97,
    in_sample_cagr_pct: 12.6,
    in_sample_dd_pct: -27.9,
    out_of_sample_cagr_pct: 26.0,
    out_of_sample_dd_pct: -19.8,
    modelled_fill_rate_pct: 83.8,
};
