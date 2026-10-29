//! Port of `baskfy_core.twt.breadth` — the regime gate: how much of the tradable universe is
//! above its own 200-session average (`docs/twt/04` §4).
//!
//! **One arithmetic, two rows** (`04` §4.4, DECISIONS-TW TW0.4): Python calls
//! `baskfy_core.vbt.breadth` with TWT's own thresholds spelled out. Until the VBT lane lands
//! `vbt::breadth`, that shared arithmetic is ported here as [`shared`] and called the same way.
//!
//! The denominator is the names that printed a bar **and** have a valid average; the numerator is
//! those whose adjusted close is strictly above it. A zero denominator is `0.0000` and SHUT.

use std::str::FromStr;

use chrono::NaiveDate;
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::backtest::helpers::WideDecimal;
use crate::error::{CoreError, CoreResult};
use crate::twt::config::vbt_shared::{BreadthConfig as VbtBreadthConfig, VbtConfig};
use crate::twt::config::{BreadthConfig, Gate, TwtConfig};
use crate::twt::indicators::frame::{F, floats, put};
use crate::twt::published::py_float_repr;

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
/// [`CoreError::Arithmetic`] for a NaN threshold (Python's `InvalidOperation` on the compare).
pub fn gate_for(pct_above_dma: Decimal, config: Option<&BreadthConfig>) -> CoreResult<Gate> {
    let default = BreadthConfig::default();
    let cfg = config.unwrap_or(&default);
    shared::gate_for(pct_above_dma, &as_shared_config(cfg).breadth)
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
    let gate = gate_for(reading.pct_above_dma, Some(&config.breadth))?;
    Ok(BreadthReading { gate, ..reading })
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
            if p.is_some_and(|x| x > cfg.min_pct_above_dma) {
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

/// `Decimal(str(threshold))` compared with `pct`: `pct > threshold`.
fn above(pct: Decimal, threshold: f64) -> CoreResult<bool> {
    if threshold.is_nan() {
        return Err(CoreError::Arithmetic(
            "[<class 'decimal.InvalidOperation'>]".to_owned(),
        ));
    }
    if threshold.is_infinite() {
        return Ok(threshold < 0.0);
    }
    let t = WideDecimal::from_str(&py_float_repr(threshold))
        .map_err(|e| CoreError::Arithmetic(e.to_string()))?;
    Ok(WideDecimal::from(pct) > t)
}

/// The shared arithmetic of `baskfy_core.vbt.breadth`, ported as far as TWT calls it.
pub(crate) mod shared {
    use std::collections::BTreeMap;

    use chrono::NaiveDate;
    use polars::prelude::{DataFrame, DataType, IntoColumn, NamedFrom, Series};
    use rust_decimal::Decimal;

    use super::BreadthReading;
    use crate::backtest::helpers::{Rounding, WideDecimal};
    use crate::error::{CoreError, CoreResult};
    use crate::twt::config::Gate;
    use crate::twt::config::vbt_shared::{BreadthConfig, VbtConfig};
    use crate::twt::indicators::frame::{cmp_nulls_first, date_of, date_series, dates, floats};

    /// Ports `vbt.breadth.gate_for`.
    pub(crate) fn gate_for(pct: Decimal, config: &BreadthConfig) -> CoreResult<Gate> {
        Ok(if super::above(pct, config.min_pct_above_dma)? {
            Gate::Open
        } else {
            Gate::Shut
        })
    }

    fn count(n: usize) -> i64 {
        i64::try_from(n).unwrap_or(i64::MAX)
    }

    /// Ports `vbt.breadth.breadth_above_dma`.
    pub(crate) fn breadth_above_dma(
        indicated: &DataFrame,
        as_of: Option<NaiveDate>,
        config: &VbtConfig,
    ) -> CoreResult<BreadthReading> {
        let days = dates(indicated, "date")?;
        let close = floats(indicated, "close")?;
        let sma = floats(indicated, "sma_dma")?;
        let rows: Vec<usize> = (0..indicated.height())
            .filter(|&i| match as_of {
                None => true,
                Some(d) => days[i].is_some_and(|x| date_of(x).is_ok_and(|y| y == d)),
            })
            .collect();
        let session = match as_of {
            Some(d) => Some(d),
            None => rows
                .iter()
                .filter_map(|&i| days[i])
                .max()
                .map(date_of)
                .transpose()?,
        };
        let with_bar: Vec<usize> = rows.into_iter().filter(|&i| close[i].is_some()).collect();
        let measured: Vec<usize> = with_bar
            .iter()
            .copied()
            .filter(|&i| sma[i].is_some())
            .collect();
        let above = measured
            .iter()
            .filter(|&&i| matches!((close[i], sma[i]), (Some(c), Some(s)) if c > s))
            .count();
        let pct = if measured.is_empty() {
            Decimal::new(0, 4)
        } else {
            let ratio = WideDecimal::from(above)
                .div(&WideDecimal::from(measured.len()))
                .map_err(|e| CoreError::Arithmetic(e.to_string()))?
                .mul(&WideDecimal::from(100_i64));
            ratio
                .quantize(-4, Rounding::HalfEven)
                .and_then(|q| q.try_to_decimal())
                .map_err(|e| CoreError::Arithmetic(e.to_string()))?
        };
        let gate = gate_for(pct, &config.breadth)?;
        Ok(BreadthReading {
            session,
            universe_count: count(with_bar.len()),
            measured_count: count(measured.len()),
            above_count: count(above),
            pct_above_dma: pct,
            gate,
        })
    }

    /// Ports `vbt.breadth.breadth_series` (the VBT gate column included; TWT overwrites it).
    pub(crate) fn breadth_series(
        indicated: &DataFrame,
        config: &VbtConfig,
    ) -> CoreResult<DataFrame> {
        let days = dates(indicated, "date")?;
        let close = floats(indicated, "close")?;
        let sma = floats(indicated, "sma_dma")?;
        // group_by("date") over rows that printed: (universe, measured, above).
        let mut by_day: BTreeMap<Option<i32>, (u32, u32, u32)> = BTreeMap::new();
        for i in 0..indicated.height() {
            let Some(c) = close[i] else { continue };
            let entry = by_day.entry(days[i]).or_insert((0, 0, 0));
            entry.0 += 1;
            if let Some(s) = sma[i] {
                entry.1 += 1;
                if c > s {
                    entry.2 += 1;
                }
            }
        }
        let mut rows: Vec<(Option<i32>, (u32, u32, u32))> = by_day.into_iter().collect();
        rows.sort_by(|a, b| cmp_nulls_first(&a.0, &b.0));
        let threshold = config.breadth.min_pct_above_dma;
        let pct: Vec<f64> = rows
            .iter()
            .map(|(_, (_, m, a))| {
                if *m > 0 {
                    f64::from(*a) / f64::from(*m) * 100.0
                } else {
                    0.0
                }
            })
            .collect();
        let gate: Vec<&str> = pct
            .iter()
            .map(|p| {
                if *p > threshold {
                    Gate::Open.value()
                } else {
                    Gate::Shut.value()
                }
            })
            .collect();
        let height = rows.len();
        let col_u32 = |name: &str, f: fn(&(u32, u32, u32)) -> u32| {
            Series::new(
                name.into(),
                rows.iter().map(|r| f(&r.1)).collect::<Vec<u32>>(),
            )
            .cast(&DataType::UInt32)
            .map(IntoColumn::into_column)
        };
        Ok(DataFrame::new(
            height,
            vec![
                date_series("date", rows.iter().map(|r| r.0).collect())?.into_column(),
                col_u32("universe_count", |r| r.0)?,
                col_u32("measured_count", |r| r.1)?,
                col_u32("above_count", |r| r.2)?,
                Series::new("pct_above_dma".into(), pct).into_column(),
                Series::new("gate".into(), gate).into_column(),
            ],
        )?)
    }
}
