//! Port of `baskfy_core.vbt.signals` (`decile-blueprint/packages/core/src/baskfy_core/vbt/signals.py`).
//!
//! The VBT-1 signal: Chartink's five lines, plus the six filters that make them a strategy
//! (`docs/vbt/04` §3). Every predicate reads a field of [`crate::vbt::config`]. **A null on
//! either side of a comparison is a fail, never a pass** (`_true`: Kleene logic, then
//! `fill_null(False)`).
//!
//! Python's `chartink_scan` and `trend_filters` return polars *expressions*; the Rust port
//! returns them **evaluated over a frame** (a `Boolean` series per expression), which is what
//! every caller does with them and what the goldens store (DECISIONS-RS R3.x). Comparisons use
//! polars' total float order (NaN equals NaN and is above every number), as the polars kernels
//! Python calls do.
//!
//! Goldens: `signals.chartink_scan`, `signals.trend_filters`, `signals.with_signal_columns`,
//! `signals.detect_signals`, `signals.signal_mask`.

use std::cmp::Ordering;

use chrono::NaiveDate;
use polars::prelude::{
    BooleanChunked, DataFrame, DataType, IntoColumn, IntoSeries, ListChunked, NamedFrom, Series,
};

use crate::error::CoreResult;
use crate::vbt::calendar::frames;
use crate::vbt::config::{ScanConfig, SignalState, TrendConfig, TrendFilter, VbtConfig};

/// Ports `FILTER_COLUMN_PREFIX`: one boolean column per filter, `filter_A` … `filter_F`.
pub const FILTER_COLUMN_PREFIX: &str = "filter_";

/// Ports `SIGNAL_COLUMNS`: the columns [`detect_signals`] returns beyond the indicated frame's.
pub const SIGNAL_COLUMNS: [&str; 3] = ["scan_hit", "state", "failed_filters"];

type F = Option<f64>;

fn gt(o: Ordering) -> bool {
    o == Ordering::Greater
}
fn ge(o: Ordering) -> bool {
    o != Ordering::Less
}
fn lt(o: Ordering) -> bool {
    o == Ordering::Less
}
fn le(o: Ordering) -> bool {
    o != Ordering::Greater
}

/// `_true(a & b & ...)`: Kleene AND of the comparisons, null read as False — true only when
/// every comparison is a non-null true.
fn all_true(parts: &[Option<bool>]) -> bool {
    parts.iter().all(|p| *p == Some(true))
}

fn col(frame: &DataFrame, name: &str) -> CoreResult<Vec<F>> {
    frames::floats(frame, name)
}

/// The five lines, row by row.
fn scan_values(frame: &DataFrame, cfg: &ScanConfig) -> CoreResult<Vec<bool>> {
    let volume = col(frame, "volume")?;
    let vol_sma = col(frame, "vol_sma")?;
    let close_raw = col(frame, "close_raw")?;
    let change = col(frame, "change_pct")?;
    Ok((0..frame.height())
        .map(|i| {
            let line1 = frames::cmp_opt(volume[i], vol_sma[i].map(|s| s * cfg.vol_mult), gt);
            let line2 = frames::cmp_opt(close_raw[i], Some(cfg.min_close_raw_inr), gt);
            let line3 = frames::cmp_opt(change[i], Some(cfg.min_change_pct), ge);
            let line4 = frames::cmp_opt(vol_sma[i], Some(cfg.min_vol_sma), ge);
            let line5 = frames::cmp_opt(volume[i], Some(cfg.min_volume), gt);
            all_true(&[line1, line2, line3, line4, line5])
        })
        .collect())
}

/// Ports `baskfy_core.vbt.signals.chartink_scan`, evaluated over `frame`: Chartink's five lines
/// read literally on the closed daily bar (`04` §3.1). Lines 1 and 5 are strict `>`, 3 and 4
/// are `>=`; line 2 reads `close_raw`. `None` reads the default `ScanConfig`. The series is
/// named `scan_hit`.
///
/// # Errors
/// A missing column (polars' `ColumnNotFound`).
pub fn chartink_scan(frame: &DataFrame, config: Option<&ScanConfig>) -> CoreResult<Series> {
    let default = ScanConfig::default();
    let cfg = config.unwrap_or(&default);
    Ok(BooleanChunked::new("scan_hit".into(), scan_values(frame, cfg)?).into_series())
}

/// One filter, row by row.
fn filter_values(frame: &DataFrame, cfg: &TrendConfig, f: TrendFilter) -> CoreResult<Vec<bool>> {
    let pair = |a: &str, b: &str, accept: fn(Ordering) -> bool| -> CoreResult<Vec<bool>> {
        let (x, y) = (col(frame, a)?, col(frame, b)?);
        Ok(x.iter()
            .zip(&y)
            .map(|(x, y)| frames::cmp_opt(*x, *y, accept) == Some(true))
            .collect())
    };
    let lit = |a: &str, v: f64, accept: fn(Ordering) -> bool| -> CoreResult<Vec<bool>> {
        Ok(col(frame, a)?
            .iter()
            .map(|x| frames::cmp_opt(*x, Some(v), accept) == Some(true))
            .collect())
    };
    match f {
        TrendFilter::AAbove200dma => pair("close", "sma_dma", gt),
        TrendFilter::BTwentyDayHigh => pair("close", "high_prior", gt),
        TrendFilter::CNotExtended => lit("ret_lookback_pct", cfg.max_ret_20_pct, lt),
        TrendFilter::DStrongClose => lit("close_position", cfg.min_close_position, ge),
        TrendFilter::EChangeCeiling => lit("change_pct", cfg.max_change_pct, le),
        TrendFilter::FTurnoverFloor => lit("turnover_avg", cfg.min_turnover_avg_inr, ge),
    }
}

/// Ports `baskfy_core.vbt.signals.trend_filters`, evaluated over `frame`: the six filters of
/// `04` §3.2, one `Boolean` series each, in `TrendFilter` order (`A` … `F`). `None` reads the
/// default `TrendConfig`.
///
/// # Errors
/// A missing column (polars' `ColumnNotFound`).
pub fn trend_filters(
    frame: &DataFrame,
    config: Option<&TrendConfig>,
) -> CoreResult<Vec<(TrendFilter, Series)>> {
    let default = TrendConfig::default();
    let cfg = config.unwrap_or(&default);
    TrendFilter::ALL
        .iter()
        .map(|&f| {
            let name = format!("{FILTER_COLUMN_PREFIX}{}", f.value());
            let values = filter_values(frame, cfg, f)?;
            Ok((
                f,
                BooleanChunked::new(name.as_str().into(), values).into_series(),
            ))
        })
        .collect()
}

/// Ports `baskfy_core.vbt.signals.with_signal_columns`: `scan_hit`, the six `filter_*`
/// booleans, `state` (`SIGNAL` / `SCAN_ONLY` / null) and `failed_filters` (`List(String)`, the
/// failing letters in order), appended in that order (a same-named column is replaced in place).
///
/// # Errors
/// A missing column (polars' `ColumnNotFound`).
pub fn with_signal_columns(indicated: &DataFrame, config: &VbtConfig) -> CoreResult<DataFrame> {
    let scan = scan_values(indicated, &config.scan)?;
    let mut filters: Vec<(TrendFilter, Vec<bool>)> = Vec::with_capacity(6);
    for f in TrendFilter::ALL {
        filters.push((f, filter_values(indicated, &config.trend, f)?));
    }
    let mut frame = indicated.clone();
    frame.with_column(BooleanChunked::new("scan_hit".into(), scan.clone()).into_column())?;
    for (f, values) in &filters {
        let name = format!("{FILTER_COLUMN_PREFIX}{}", f.value());
        frame
            .with_column(BooleanChunked::new(name.as_str().into(), values.clone()).into_column())?;
    }
    let height = indicated.height();
    let mut state: Vec<Option<&str>> = Vec::with_capacity(height);
    let mut failed: Vec<Series> = Vec::with_capacity(height);
    for (i, hit) in scan.iter().enumerate() {
        let every = filters.iter().all(|(_, v)| v[i]);
        state.push(if *hit && every {
            Some(SignalState::Signal.value())
        } else if *hit {
            Some(SignalState::ScanOnly.value())
        } else {
            None
        });
        let letters: Vec<&str> = filters
            .iter()
            .filter(|(_, v)| !v[i])
            .map(|(f, _)| f.value())
            .collect();
        failed.push(Series::new("".into(), letters));
    }
    frame.with_column(Series::new("state".into(), state).into_column())?;
    let mut list: ListChunked = failed.into_iter().map(Some).collect();
    if height == 0 {
        list = ListChunked::full_null_with_dtype("failed_filters".into(), 0, &DataType::String);
    }
    list.rename("failed_filters".into());
    frame.with_column(list.into_series().into_column())?;
    Ok(frame)
}

/// Ports `baskfy_core.vbt.signals.detect_signals`: every scan hit of the frame — `SIGNAL` and
/// `SCAN_ONLY` rows — with `rank_key` (the signal-day `turnover_inr`), sorted by `date`
/// ascending, `rank_key` descending, `instrument_id` ascending. As polars' multi-key sort with
/// `nulls_last=False`, a null sorts **first in every key, even a descending one** (R2.9).
/// `as_of` restricts the answer to one session.
///
/// # Errors
/// A missing column, or a `date` column that is not a polars `Date`.
pub fn detect_signals(
    indicated: &DataFrame,
    as_of: Option<NaiveDate>,
    config: &VbtConfig,
) -> CoreResult<DataFrame> {
    let mut frame = with_signal_columns(indicated, config)?;
    if let Some(day) = as_of {
        let day = frames::day_number(day)?;
        let mask: Vec<bool> = frames::dates(&frame, "date")?
            .into_iter()
            .map(|d| d == Some(day))
            .collect();
        frame = frame.filter(&BooleanChunked::new("".into(), mask))?;
    }
    let hits: Vec<bool> = frame
        .column("scan_hit")?
        .as_materialized_series()
        .bool()?
        .iter()
        .map(|b| b == Some(true))
        .collect();
    frame = frame.filter(&BooleanChunked::new("".into(), hits))?;
    let rank = frame
        .column("turnover_inr")?
        .as_materialized_series()
        .clone()
        .with_name("rank_key".into());
    frame.with_column(rank.into_column())?;

    let dates = frames::dates(&frame, "date")?;
    let keys = frames::floats(&frame, "rank_key")?;
    let ids: Vec<Option<i64>> = frame
        .column("instrument_id")?
        .as_materialized_series()
        .cast(&DataType::Int64)?
        .i64()?
        .iter()
        .collect();
    let mut order: Vec<usize> = (0..frame.height()).collect();
    let nulls_first = |a: bool, b: bool| -> Option<Ordering> {
        match (a, b) {
            (true, true) => Some(Ordering::Equal),
            (true, false) => Some(Ordering::Less),
            (false, true) => Some(Ordering::Greater),
            (false, false) => None,
        }
    };
    order.sort_by(|&a, &b| {
        let by_date = nulls_first(dates[a].is_none(), dates[b].is_none())
            .unwrap_or_else(|| dates[a].cmp(&dates[b]));
        let by_rank = || match (keys[a], keys[b]) {
            (Some(x), Some(y)) => frames::tot_cmp(y, x),
            (x, y) => nulls_first(x.is_none(), y.is_none()).unwrap_or(Ordering::Equal),
        };
        let by_id = || {
            nulls_first(ids[a].is_none(), ids[b].is_none()).unwrap_or_else(|| ids[a].cmp(&ids[b]))
        };
        by_date.then_with(by_rank).then_with(by_id)
    });
    Ok(frame.take(&frames::idx(&order)?)?)
}

/// Ports `baskfy_core.vbt.signals.signal_mask`: a `Boolean` series over `indicated` (named
/// `state`, as Python's) — is this row a full VBT-1 signal?
///
/// # Errors
/// A missing column (polars' `ColumnNotFound`).
pub fn signal_mask(indicated: &DataFrame, config: &VbtConfig) -> CoreResult<Series> {
    let frame = with_signal_columns(indicated, config)?;
    let state = frame.column("state")?.as_materialized_series().clone();
    let values: Vec<bool> = state
        .str()?
        .iter()
        .map(|s| s == Some(SignalState::Signal.value()))
        .collect();
    Ok(BooleanChunked::new("state".into(), values).into_series())
}
