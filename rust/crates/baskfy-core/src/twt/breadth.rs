//! Port of `baskfy_core.twt.breadth` — the regime gate: how much of the tradable universe is
//! above its own 200-session average (`docs/twt/04` §4).
//!
//! **One arithmetic, two rows** (`04` §4.4, DECISIONS-TW TW0.4): Python calls
//! `baskfy_core.vbt.breadth` with TWT's own thresholds spelled out, and so does this port —
//! [`crate::vbt::breadth`] (a private copy until the wave-3 housekeeping, DECISIONS-RS R3.4).
//!
//! The denominator is the names that printed a bar **and** have a valid average; the numerator is
//! those whose adjusted close is strictly above it. A zero denominator is `0.0000` and SHUT.

use chrono::NaiveDate;
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::twt::config::{BreadthConfig, Gate, TwtConfig};
use crate::twt::indicators::frame::{F, floats, put};
use crate::vbt::breadth as shared;
use crate::vbt::config::{BreadthConfig as VbtBreadthConfig, VbtConfig};

/// Ports `baskfy_core.twt.breadth.BreadthReading`: one session's reading and its gate.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BreadthReading {
    /// The session read; `None` for an empty frame read without `as_of`.
    pub session: Option<NaiveDate>,
    /// Names that printed a bar.
    pub universe_count: i64,
    /// Of those, names with a valid average.
    pub measured_count: i64,
    /// Of those, names strictly above it.
    pub above_count: i64,
    /// `above / measured x 100`, four places.
    pub pct_above_dma: Decimal,
    /// The gate it implies.
    pub gate: Gate,
}

impl BreadthReading {
    /// Ports `BreadthReading.is_open`.
    #[must_use]
    pub fn is_open(&self) -> bool {
        self.gate == Gate::Open
    }
}

/// Ports `as_shared_config`: TWT's breadth thresholds in VBT's shape, written out.
#[must_use]
pub fn as_shared_config(config: &BreadthConfig) -> VbtConfig {
    VbtConfig {
        breadth: VbtBreadthConfig {
            dma_bars: config.dma_bars,
            min_pct_above_dma: config.min_pct_above_dma,
        },
        ..VbtConfig::default()
    }
}

/// Ports `gate_for`: `OPEN` **strictly above** the threshold, `SHUT` at or below it. The
/// threshold is a float read as `Decimal(str(threshold))`. `None` is TWT's default config.
///
/// # Errors
/// [`CoreError::InvalidInput`] for a NaN threshold (Python's `InvalidOperation` on the compare).
pub fn gate_for(pct_above_dma: Decimal, config: Option<&BreadthConfig>) -> CoreResult<Gate> {
    let default = BreadthConfig::default();
    let cfg = config.unwrap_or(&default);
    Ok(gate_of(shared::gate_for(
        pct_above_dma,
        Some(&as_shared_config(cfg).breadth),
    )?))
}

/// `Gate(vbt_gate.value)`: VBT's verdict as TWT's own enum.
fn gate_of(gate: crate::vbt::config::Gate) -> Gate {
    match gate {
        crate::vbt::config::Gate::Open => Gate::Open,
        crate::vbt::config::Gate::Shut => Gate::Shut,
    }
}

/// Ports `breadth_above_dma`: the reading for one session (`as_of`), or — with `as_of = None` —
/// for **every row of the frame**, labelled with its latest session (R2.9: inherited from VBT).
///
/// # Errors
/// A missing `date`, `close` or `sma_dma` column, or a NaN threshold.
pub fn breadth_above_dma(
    indicated: &DataFrame,
    as_of: Option<NaiveDate>,
    config: &TwtConfig,
) -> CoreResult<BreadthReading> {
    let reading = shared::breadth_above_dma(indicated, as_of, &as_shared_config(&config.breadth))?;
    Ok(BreadthReading {
        session: reading.session,
        universe_count: reading.universe_count,
        measured_count: reading.measured_count,
        above_count: reading.above_count,
        pct_above_dma: reading.pct_above_dma,
        gate: gate_for(reading.pct_above_dma, Some(&config.breadth))?,
    })
}

/// Ports `breadth_series`: `(date, universe_count, measured_count, above_count, pct_above_dma,
/// gate)` per session, sorted by date; the percentage rounded to `pct_decimals` (polars'
/// half-to-even) **before** the gate reads it.
///
/// # Errors
/// A missing column, or a negative `pct_decimals`.
pub fn breadth_series(indicated: &DataFrame, config: &TwtConfig) -> CoreResult<DataFrame> {
    let cfg = &config.breadth;
    let mut frame = shared::breadth_series(indicated, &as_shared_config(cfg))?;
    let decimals = u32::try_from(cfg.pct_decimals).map_err(|_| {
        CoreError::InvalidInput(format!(
            "decimals must be a non-negative integer, got {}",
            cfg.pct_decimals
        ))
    })?;
    let pct: Vec<F> = floats(&frame, "pct_above_dma")?
        .into_iter()
        .map(|v| v.map(|x| polars_round(x, decimals)))
        .collect();
    let gate: Vec<&str> = pct
        .iter()
        .map(|p| {
            if p.is_some_and(|x| crate::pl::gt(x, cfg.min_pct_above_dma)) {
                Gate::Open.value()
            } else {
                Gate::Shut.value()
            }
        })
        .collect();
    put(
        &mut frame,
        Series::new("pct_above_dma".into(), pct).into_column(),
    )?;
    put(&mut frame, Series::new("gate".into(), gate).into_column())?;
    Ok(frame)
}

/// polars' `Expr.round(decimals)` on a float (`mode="half_to_even"`, polars-ops' kernel):
/// `round_ties_even(x * 10^d) / 10^d`, the input itself when that is not finite.
fn polars_round(x: f64, decimals: u32) -> f64 {
    if decimals == 0 {
        return x.round_ties_even();
    }
    if decimals >= 300 {
        // Beyond any value a percentage takes; polars splits the multiplier here.
        return x;
    }
    let multiplier = 10.0_f64.powi(i32::try_from(decimals).unwrap_or(300));
    let ret = (x * multiplier).round_ties_even() / multiplier;
    if ret.is_finite() { ret } else { x }
}
