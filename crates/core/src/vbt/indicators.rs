//! Port of `baskfy_core.vbt.indicators` (`decile-blueprint/packages/core/src/baskfy_core/vbt/indicators.py`).
//!
//! Per-bar columns the VBT-1 rules read (`docs/vbt/04` §1). One frame in, one frame out. Every
//! column is Python's polars window expression `.over("instrument_id")`; here each instrument's
//! slice runs through the **same polars kernels** (`rolling_mean`, `rolling_max`, `ewm_mean`) so
//! the floats agree bit for bit.
//!
//! **The frame is densified first**: a rolling window counts *sessions*, not rows, so each
//! instrument is laid out against the calendar with a null where no bar exists, and every
//! rolling statistic carries `min_samples` from `DataConfig::rolling_min_share` (`04` §2.2).
//!
//! Goldens: `indicators.require_columns`, `indicators.densify`, `indicators.with_vbt_indicators`.

use std::cmp::Ordering;
use std::collections::HashMap;
use std::ops::Range;

use polars::prelude::{DataFrame, DataType, IdxCa, IdxSize, IntoColumn, NamedFrom, Series};

use crate::error::{CoreError, CoreResult};
use crate::vbt::calendar::{SessionCalendar, frames};
use crate::vbt::config::VbtConfig;

/// Ports `REQUIRED_COLUMNS`: the columns [`with_vbt_indicators`] requires.
pub const REQUIRED_COLUMNS: [&str; 8] = [
    "instrument_id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "close_raw",
    "volume",
];

/// Ports `OPTIONAL_COLUMNS`: used when present and tolerated when absent.
pub const OPTIONAL_COLUMNS: [&str; 3] = ["upper_circuit", "adj_factor", "symbol"];

/// Ports `INDICATOR_COLUMNS`: the columns [`with_vbt_indicators`] adds (the docstring's order;
/// the frame's own order is Python's `with_columns` order).
pub const INDICATOR_COLUMNS: [&str; 12] = [
    "prev_close",
    "change_pct",
    "vol_sma",
    "rvol",
    "sma_dma",
    "high_prior",
    "ret_lookback_pct",
    "close_position",
    "turnover_inr",
    "turnover_avg",
    "ema_exit",
    "locked_upper_circuit",
];

const PCT: f64 = 100.0;
const OVER: &str = "instrument_id";

type F = Option<f64>;

/// Ports `baskfy_core.vbt.indicators.require_columns`.
///
/// # Errors
/// [`CoreError::InvalidInput`] with Python's `ValueError` message, naming every missing column.
pub fn require_columns(bars: &DataFrame) -> CoreResult<()> {
    let missing: Vec<String> = REQUIRED_COLUMNS
        .iter()
        .filter(|c| !frames::has(bars, c))
        .map(|c| (*c).to_owned())
        .collect();
    if missing.is_empty() {
        return Ok(());
    }
    Err(CoreError::InvalidInput(format!(
        "bars are missing required columns {}; got {}",
        frames::py_list(&missing),
        frames::py_list(&frames::column_names(bars))
    )))
}

/// `_prepare`: the optional columns added as nulls when absent (in `OPTIONAL_COLUMNS` order),
/// the price/volume columns cast to `Float64`, `adj_factor` defaulted to 1.0.
fn prepare(bars: &DataFrame) -> CoreResult<DataFrame> {
    let mut frame = bars.clone();
    let h = frame.height();
    for column in OPTIONAL_COLUMNS {
        if !frames::has(&frame, column) {
            let dtype = if column == "symbol" {
                DataType::String
            } else {
                DataType::Float64
            };
            frame.with_column(Series::full_null(column.into(), h, &dtype).into_column())?;
        }
    }
    for c in [
        "open",
        "high",
        "low",
        "close",
        "close_raw",
        "volume",
        "upper_circuit",
    ] {
        let cast = frame.column(c)?.cast(&DataType::Float64)?;
        frame.with_column(cast)?;
    }
    let adj: Vec<F> = frames::floats(&frame, "adj_factor")?
        .into_iter()
        .map(|v| Some(v.unwrap_or(1.0)))
        .collect();
    frame.with_column(Series::new("adj_factor".into(), adj).into_column())?;
    Ok(frame)
}

/// `instrument_id` as `Int64` values (the key of the densified grid).
fn ids(df: &DataFrame) -> CoreResult<Vec<Option<i64>>> {
    Ok(df
        .column(OVER)?
        .as_materialized_series()
        .cast(&DataType::Int64)?
        .i64()?
        .iter()
        .collect())
}

/// Ports `baskfy_core.vbt.indicators.densify`: one row per (instrument, session) of the
/// calendar, nulls where the name did not print.
///
/// The identity columns (`instrument_id`, and `symbol` — the first non-null one each name
/// printed) are carried onto the blank rows; every price and volume stays null. Bars on dates
/// outside the calendar are dropped. Column order is Python's join: `instrument_id`, `symbol`
/// (when present), `date`, then the bars' other columns in their order.
///
/// # Errors
/// A `date` column that is not a polars `Date`, or a polars error.
pub fn densify(bars: &DataFrame, calendar: &SessionCalendar) -> CoreResult<DataFrame> {
    let id_dtype = bars.column(OVER)?.dtype().clone();
    let bar_ids = ids(bars)?;
    let bar_dates = frames::dates(bars, "date")?;
    let has_symbol = frames::has(bars, "symbol");

    // names = bars.select("instrument_id").unique().sort("instrument_id") — nulls first.
    let mut names: Vec<Option<i64>> = bar_ids.clone();
    names.sort_unstable();
    names.dedup();

    // symbols: the first non-null symbol each id printed, joined on (non-null) id.
    let mut symbol_of: HashMap<i64, String> = HashMap::new();
    if has_symbol {
        let symbols = bars.column("symbol")?.as_materialized_series().clone();
        let symbols = symbols.cast(&DataType::String)?;
        for (id, sym) in bar_ids.iter().zip(symbols.str()?.iter()) {
            if let (Some(id), Some(sym)) = (id, sym) {
                symbol_of.entry(*id).or_insert_with(|| sym.to_owned());
            }
        }
    }

    let sessions: Vec<i32> = calendar
        .sessions
        .iter()
        .map(|d| frames::day_number(*d))
        .collect::<CoreResult<_>>()?;

    // The left join: payload rows by (id, date), in frame order. Null keys never match.
    let mut payload_rows: HashMap<(i64, i32), Vec<usize>> = HashMap::new();
    for (r, (id, d)) in bar_ids.iter().zip(&bar_dates).enumerate() {
        if let (Some(id), Some(d)) = (id, d) {
            payload_rows.entry((*id, *d)).or_default().push(r);
        }
    }

    // (id, date, payload row) for every grid cell, then sorted by (id, date), nulls first.
    let mut grid: Vec<(Option<i64>, i32, Option<usize>)> = Vec::new();
    for id in &names {
        for &d in &sessions {
            let matched = id.and_then(|id| payload_rows.get(&(id, d)));
            match matched {
                Some(rows) => grid.extend(rows.iter().map(|&r| (*id, d, Some(r)))),
                None => grid.push((*id, d, None)),
            }
        }
    }
    grid.sort_by(|a, b| match a.0.cmp(&b.0) {
        Ordering::Equal => a.1.cmp(&b.1),
        other => other,
    });

    let mut columns = Vec::new();
    let out_ids: Vec<Option<i64>> = grid.iter().map(|g| g.0).collect();
    columns.push(
        Series::new(OVER.into(), out_ids)
            .cast(&id_dtype)?
            .into_column(),
    );
    if has_symbol {
        let syms: Vec<Option<String>> = grid
            .iter()
            .map(|g| g.0.and_then(|id| symbol_of.get(&id).cloned()))
            .collect();
        columns.push(Series::new("symbol".into(), syms).into_column());
    }
    let out_dates: Vec<Option<i32>> = grid.iter().map(|g| Some(g.1)).collect();
    columns.push(frames::date_series("date", out_dates)?.into_column());

    let take: Vec<Option<IdxSize>> = grid
        .iter()
        .map(|g| {
            g.2.map(|r| IdxSize::try_from(r).map_err(|e| CoreError::InvalidInput(e.to_string())))
                .transpose()
        })
        .collect::<CoreResult<_>>()?;
    let take: IdxCa = take.into_iter().collect();
    for c in bars.columns() {
        let name = c.name().as_str();
        if name == OVER || name == "date" || name == "symbol" {
            continue;
        }
        let gathered = c.as_materialized_series().take(&take)?;
        columns.push(gathered.into_column());
    }
    Ok(DataFrame::new(grid.len(), columns)?)
}

/// One instrument's `rolling_mean`/`rolling_max(window, min_samples)` — polars' own kernel.
fn rolling_slice(values: &[F], max: bool, window: usize, min: usize) -> CoreResult<Vec<F>> {
    use polars::prelude::{RollingOptionsFixedWindow, SeriesOpsTime};
    if values.is_empty() {
        return Ok(Vec::new());
    }
    let s = Series::new("".into(), values.to_vec());
    let options = RollingOptionsFixedWindow {
        window_size: window,
        min_periods: min,
        weights: None,
        center: false,
        fn_params: None,
    };
    let out = if max {
        s.rolling_max(options)?
    } else {
        s.rolling_mean(options)?
    };
    Ok(out.cast(&DataType::Float64)?.f64()?.iter().collect())
}

/// `expr.rolling_*(window, min_samples).over(...)` over contiguous groups.
fn rolling(
    values: &[F],
    groups: &[Range<usize>],
    max: bool,
    window: i64,
    min: i64,
) -> CoreResult<Vec<F>> {
    let window = usize::try_from(window)
        .map_err(|e| CoreError::InvalidInput(format!("window_size {window}: {e}")))?;
    let min = usize::try_from(min)
        .map_err(|e| CoreError::InvalidInput(format!("min_samples {min}: {e}")))?;
    let mut out = Vec::with_capacity(values.len());
    for g in groups {
        out.extend(rolling_slice(&values[g.clone()], max, window, min)?);
    }
    Ok(out)
}

/// `expr.shift(k).over(...)`.
fn shift(values: &[F], groups: &[Range<usize>], k: usize) -> Vec<F> {
    let mut out = vec![None; values.len()];
    for g in groups {
        for i in g.clone() {
            if i >= g.start + k {
                out[i] = values[i - k];
            }
        }
    }
    out
}

/// `expr.ewm_mean(span, adjust=False, min_samples=span, ignore_nulls=False).over(...)`.
fn ewm(values: &[F], groups: &[Range<usize>], span: i64) -> CoreResult<Vec<F>> {
    use polars::prelude::EWMOptions;
    if span < 1 {
        return Err(CoreError::InvalidInput(format!(
            "require 'span' >= 1 (found {span})"
        )));
    }
    let min = usize::try_from(span).map_err(|e| CoreError::InvalidInput(e.to_string()))?;
    // Python: `alpha = 2 / (span + 1)`, both ints, true division.
    #[allow(clippy::cast_precision_loss)]
    let alpha = 2.0 / (span as f64 + 1.0);
    let mut out = Vec::with_capacity(values.len());
    for g in groups {
        if g.is_empty() {
            continue;
        }
        let s = Series::new("".into(), values[g.clone()].to_vec());
        let r = polars::prelude::ewm_mean(
            &s,
            EWMOptions {
                alpha,
                adjust: false,
                bias: false,
                min_periods: min,
                ignore_nulls: false,
            },
        )?;
        out.extend(r.cast(&DataType::Float64)?.f64()?.iter());
    }
    Ok(out)
}

fn zip2(a: &[F], b: &[F], f: impl Fn(f64, f64) -> f64) -> Vec<F> {
    a.iter()
        .zip(b)
        .map(|(x, y)| Some(f((*x)?, (*y)?)))
        .collect()
}

fn put(df: &mut DataFrame, name: &str, values: Vec<F>) -> CoreResult<()> {
    df.with_column(Series::new(name.into(), values).into_column())?;
    Ok(())
}

/// Ports `baskfy_core.vbt.indicators.with_vbt_indicators`: every indicator column, over the
/// instrument, against the calendar.
///
/// Added (all `Float64` but the last): `prev_close`, `turnover_inr`, `change_pct`, `vol_sma`,
/// `sma_dma`, `high_prior`, `ret_lookback_pct`, `close_position` (0.5 when `high == low`),
/// `turnover_avg`, `ema_exit`, `rvol`, `locked_upper_circuit` (`Boolean`) — in that order, the
/// order of Python's three `with_columns` calls.
///
/// # Errors
/// [`require_columns`]'s `ValueError`; a non-finite `rolling_min_share`; a negative window.
pub fn with_vbt_indicators(
    bars: &DataFrame,
    calendar: &SessionCalendar,
    config: &VbtConfig,
) -> CoreResult<DataFrame> {
    require_columns(bars)?;
    let mut frame = prepare(&densify(bars, calendar)?)?;
    let (data, scan, trend, exits) = (&config.data, &config.scan, &config.trend, &config.exits);
    let groups = frames::runs(&frame, OVER)?;

    let close = frames::floats(&frame, "close")?;
    let close_raw = frames::floats(&frame, "close_raw")?;
    let volume = frames::floats(&frame, "volume")?;
    let high = frames::floats(&frame, "high")?;
    let low = frames::floats(&frame, "low")?;
    let upper = frames::floats(&frame, "upper_circuit")?;

    // with_columns #1
    let prev_close = shift(&close, &groups, 1);
    let turnover_inr = zip2(&close_raw, &volume, |a, b| a * b);
    put(&mut frame, "prev_close", prev_close.clone())?;
    put(&mut frame, "turnover_inr", turnover_inr.clone())?;

    // with_columns #2 (every expression reads the frame after #1)
    let change_pct: Vec<F> = zip2(&close, &prev_close, |c, p| (c / p - 1.0) * PCT);
    let vol_sma = rolling(
        &volume,
        &groups,
        false,
        scan.vol_sma_bars,
        data.min_samples(scan.vol_sma_bars)?,
    )?;
    let sma_dma = rolling(
        &close,
        &groups,
        false,
        trend.dma_bars,
        data.min_samples(trend.dma_bars)?,
    )?;
    let high_max = rolling(
        &high,
        &groups,
        true,
        trend.breakout_high_bars,
        data.min_samples(trend.breakout_high_bars)?,
    )?;
    let high_prior = shift(&high_max, &groups, 1);
    let ret_bars = usize::try_from(trend.ret_bars)
        .map_err(|e| CoreError::InvalidInput(format!("ret_bars: {e}")))?;
    let close_back = shift(&close, &groups, ret_bars);
    let ret_lookback_pct = zip2(&close, &close_back, |c, b| (c / b - 1.0) * PCT);
    let close_position: Vec<F> = close
        .iter()
        .zip(high.iter().zip(&low))
        .map(|(c, (h, l))| {
            let range_ok = frames::cmp_opt(*h, *l, |o| o == Ordering::Greater) == Some(true);
            if range_ok {
                match (c, h, l) {
                    (Some(c), Some(h), Some(l)) => Some((c - l) / (h - l)),
                    _ => None,
                }
            } else if c.is_none() {
                None
            } else {
                Some(0.5)
            }
        })
        .collect();
    let turnover_avg = rolling(
        &turnover_inr,
        &groups,
        false,
        trend.turnover_bars,
        data.min_samples(trend.turnover_bars)?,
    )?;
    let ema_exit = ewm(&close, &groups, exits.trail_ema_bars)?;
    put(&mut frame, "change_pct", change_pct)?;
    put(&mut frame, "vol_sma", vol_sma.clone())?;
    put(&mut frame, "sma_dma", sma_dma)?;
    put(&mut frame, "high_prior", high_prior)?;
    put(&mut frame, "ret_lookback_pct", ret_lookback_pct)?;
    put(&mut frame, "close_position", close_position)?;
    put(&mut frame, "turnover_avg", turnover_avg)?;
    put(&mut frame, "ema_exit", ema_exit)?;

    // with_columns #3
    let rvol: Vec<F> = volume
        .iter()
        .zip(&vol_sma)
        .map(|(v, s)| {
            if frames::cmp_opt(*s, Some(0.0), |o| o == Ordering::Greater) == Some(true) {
                Some((*v)? / (*s)?)
            } else {
                None
            }
        })
        .collect();
    let locked: Vec<bool> = upper
        .iter()
        .zip(&high)
        .map(|(u, h)| {
            u.is_some()
                && frames::cmp_opt(*u, Some(0.0), |o| o == Ordering::Greater) == Some(true)
                && frames::cmp_opt(*h, *u, |o| o != Ordering::Less) == Some(true)
        })
        .collect();
    put(&mut frame, "rvol", rvol)?;
    frame.with_column(Series::new("locked_upper_circuit".into(), locked).into_column())?;
    Ok(frame)
}
