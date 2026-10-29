//! Port of `baskfy_core.vbt.breadth` (`decile-blueprint/packages/core/src/baskfy_core/vbt/breadth.py`).
//!
//! The regime gate: how much of the tradable universe is above its own 200-day average
//! (`docs/vbt/04` §4). A breadth reading of **the universe the book trades**, never the plant's
//! index-membership `market_health_daily.pct_above_200dma` (DECISIONS-VB VB0.3).
//!
//! The gate verdict is money: a wrong OPEN is a wrong trade. `pct_above_dma` is Python's
//! `Decimal` arithmetic reproduced exactly (28 significant digits, then `quantize`), and the
//! threshold is `Decimal(str(float))`, as Python reads it.
//!
//! Goldens: `breadth.gate_for`, `breadth.breadth_above_dma`, `breadth.breadth_series`.

use std::cmp::Ordering;
use std::collections::BTreeMap;

use chrono::NaiveDate;
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::backtest::helpers::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::swing::sizing::{ConfigNumber, config_number};
use crate::vbt::calendar::frames;
use crate::vbt::config::{BreadthConfig, Gate, VbtConfig};

/// Ports `baskfy_core.vbt.breadth.BreadthReading` — one session's reading, and the gate it
/// implies.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BreadthReading {
    /// The session read (`None` for an empty frame read without `as_of`).
    pub session: Option<NaiveDate>,
    /// Names that printed a bar on the session.
    pub universe_count: i64,
    /// ...and have a valid 200-day average.
    pub measured_count: i64,
    /// ...and closed strictly above it.
    pub above_count: i64,
    /// `above / measured x 100`, quantized to four places (`"0.0000"` when nobody is measured).
    pub pct_above_dma: Decimal,
    /// The gate this reading implies.
    pub gate: Gate,
}

impl BreadthReading {
    /// Ports the `is_open` property.
    #[must_use]
    pub fn is_open(&self) -> bool {
        self.gate == Gate::Open
    }
}

/// Ports `baskfy_core.vbt.breadth.gate_for`: `OPEN` above the threshold, `SHUT` at or below
/// it — strict, as the research read it. The threshold is `Decimal(str(min_pct_above_dma))`.
/// `None` reads the default `BreadthConfig`.
///
/// # Errors
/// [`CoreError::InvalidInput`] on a NaN threshold (Python's `InvalidOperation` on comparing
/// with `Decimal('NaN')`).
pub fn gate_for(pct_above_dma: Decimal, config: Option<&BreadthConfig>) -> CoreResult<Gate> {
    let default = BreadthConfig::default();
    let cfg = config.unwrap_or(&default);
    let above = match config_number(cfg.min_pct_above_dma)? {
        ConfigNumber::Finite(threshold) => pct_above_dma > threshold,
        // Any Decimal is below +huge/+inf and above -huge/-inf.
        ConfigNumber::Beyond { positive, .. } => !positive,
    };
    Ok(if above { Gate::Open } else { Gate::Shut })
}

/// `(Decimal(above) / Decimal(measured) * 100).quantize(Decimal("0.0001"))`, or `0.0000`.
fn pct(above: i64, measured: i64) -> CoreResult<Decimal> {
    if measured == 0 {
        return Ok(Decimal::new(0, 4));
    }
    let ratio = WideDecimal::from(above)
        .div(&WideDecimal::from(measured))
        .map_err(|e| CoreError::Arithmetic(e.to_string()))?;
    let scaled = ratio.mul(&WideDecimal::from(100_i64));
    scaled
        .quantize(-4, Rounding::HalfEven)
        .and_then(|q| q.try_to_decimal())
        .map_err(|e| CoreError::Arithmetic(e.to_string()))
}

fn count(n: usize) -> CoreResult<i64> {
    i64::try_from(n).map_err(|e| CoreError::InvalidInput(e.to_string()))
}

/// Ports `baskfy_core.vbt.breadth.breadth_above_dma`: the reading for one session.
///
/// The denominator is the names that both **printed a bar** (`close` not null) and **have a
/// valid average** (`sma_dma` not null); a name still warming up is in neither count. With
/// `as_of = None` every row of the frame is counted and the reading is stamped with the frame's
/// latest date (R2.9).
///
/// # Errors
/// Missing `date`/`close`/`sma_dma` columns, or a `date` that is not a polars `Date`.
pub fn breadth_above_dma(
    indicated: &DataFrame,
    as_of: Option<NaiveDate>,
    config: &VbtConfig,
) -> CoreResult<BreadthReading> {
    let dates = frames::dates(indicated, "date")?;
    let close = frames::floats(indicated, "close")?;
    let sma = frames::floats(indicated, "sma_dma")?;
    let target = as_of.map(frames::day_number).transpose()?;
    let rows: Vec<usize> = (0..indicated.height())
        .filter(|&i| target.is_none_or(|t| dates[i] == Some(t)))
        .collect();
    let session = match as_of {
        Some(d) => Some(d),
        // `frame["date"].max()`: the latest non-null date, None when there is none.
        None => rows
            .iter()
            .filter_map(|&i| dates[i])
            .max()
            .map(frames::from_day_number)
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
        .filter(|&&i| frames::cmp_opt(close[i], sma[i], |o| o == Ordering::Greater) == Some(true))
        .count();
    let (measured_count, above_count) = (count(measured.len())?, count(above)?);
    let pct_above_dma = pct(above_count, measured_count)?;
    Ok(BreadthReading {
        session,
        universe_count: count(with_bar.len())?,
        measured_count,
        above_count,
        pct_above_dma,
        gate: gate_for(pct_above_dma, Some(&config.breadth))?,
    })
}

/// Ports `baskfy_core.vbt.breadth.breadth_series`: `(date, universe_count, measured_count,
/// above_count, pct_above_dma, gate)` per session, sorted by date. The counts are `UInt32`
/// (polars' `len()`/boolean `sum()`), `pct_above_dma` a `Float64` (`above / measured * 100.0`,
/// `0.0` when nobody is measured), `gate` the strings `OPEN`/`SHUT` (strict `>` against the
/// float threshold).
///
/// # Errors
/// Missing columns, or a `date` that is not a polars `Date`.
pub fn breadth_series(indicated: &DataFrame, config: &VbtConfig) -> CoreResult<DataFrame> {
    let threshold = config.breadth.min_pct_above_dma;
    let dates = frames::dates(indicated, "date")?;
    let close = frames::floats(indicated, "close")?;
    let sma = frames::floats(indicated, "sma_dma")?;
    // date -> (universe, measured, above); a null date is its own group, sorted first.
    let mut groups: BTreeMap<Option<i32>, (u32, u32, u32)> = BTreeMap::new();
    for i in 0..indicated.height() {
        if close[i].is_none() {
            continue;
        }
        let entry = groups.entry(dates[i]).or_insert((0, 0, 0));
        entry.0 = entry.0.saturating_add(1);
        if sma[i].is_some() {
            entry.1 = entry.1.saturating_add(1);
            if frames::cmp_opt(close[i], sma[i], |o| o == Ordering::Greater) == Some(true) {
                entry.2 = entry.2.saturating_add(1);
            }
        }
    }
    let mut day = Vec::with_capacity(groups.len());
    let (mut universe, mut measured, mut above) = (Vec::new(), Vec::new(), Vec::new());
    let (mut pct, mut gate) = (Vec::new(), Vec::new());
    for (d, (u, m, a)) in groups {
        day.push(d);
        universe.push(u);
        measured.push(m);
        above.push(a);
        let p = if m > 0 {
            f64::from(a) / f64::from(m) * 100.0
        } else {
            0.0
        };
        pct.push(p);
        let open = frames::tot_cmp(p, threshold) == Ordering::Greater;
        gate.push(if open { Gate::Open } else { Gate::Shut }.value());
    }
    let height = day.len();
    Ok(DataFrame::new(
        height,
        vec![
            frames::date_series("date", day)?.into_column(),
            Series::new("universe_count".into(), universe).into_column(),
            Series::new("measured_count".into(), measured).into_column(),
            Series::new("above_count".into(), above).into_column(),
            Series::new("pct_above_dma".into(), pct).into_column(),
            Series::new("gate".into(), gate).into_column(),
        ],
    )?)
}
