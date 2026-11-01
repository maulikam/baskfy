//! Port of `baskfy_core.twt.indicators` — the per-bar columns the TWT-1 rules read
//! (`docs/twt/04` §2, §3).
//!
//! One frame in, one frame out. The frame is **densified first** (one row per instrument and
//! calendar session, nulls where no bar printed — `baskfy_core.vbt.indicators.densify`, called
//! as Python calls it), so a rolling window counts sessions, not rows,
//! and every rolling statistic carries `min_samples` from `DataConfig.rolling_min_share`. The
//! rolling means call polars' own kernel on each instrument's slice, which is what Python's
//! `.over("instrument_id")` does.
//!
//! `week_key` is built from the **ISO year** (2024-12-30 is 2025-W01, key 202501).

use chrono::{Datelike, NaiveDate};
use polars::prelude::{Column, DataFrame, DataType, IntoColumn, NamedFrom, Series};

use crate::error::{CoreError, CoreResult};
use crate::twt::calendar::SessionCalendar;
use crate::twt::config::TwtConfig;

use self::frame::{F, bools, column_names, dates, floats, has, ints, put, py_list};

/// Ports `REQUIRED_COLUMNS`: the columns [`with_twt_indicators`] requires.
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

/// Ports `OPTIONAL_COLUMNS`: used when present, added as nulls when absent.
pub const OPTIONAL_COLUMNS: [&str; 3] = ["adj_factor", "symbol", "is_etf"];

/// Ports `INDICATOR_COLUMNS`: the columns [`with_twt_indicators`] adds, in order.
pub const INDICATOR_COLUMNS: [&str; 7] = [
    "week_key",
    "month_key",
    "turnover_inr",
    "vol_sma",
    "turnover_avg_20",
    "sma_dma",
    "limit_locked",
];

/// Ports `MONTHS_IN_YEAR`: `year x 12 + month - 1` is a monotone month key.
pub const MONTHS_IN_YEAR: i64 = 12;

/// Ports `WEEK_KEY_SCALE`: `iso_year x 100 + iso_week`.
pub const WEEK_KEY_SCALE: i64 = 100;

/// Ports `require_columns`: every required column present, else Python's `ValueError` text.
///
/// # Errors
/// [`CoreError::InvalidInput`] naming the missing columns and the frame's columns.
pub fn require_columns(bars: &DataFrame) -> CoreResult<()> {
    let missing: Vec<String> = REQUIRED_COLUMNS
        .iter()
        .filter(|c| !has(bars, c))
        .map(|c| (*c).to_owned())
        .collect();
    if missing.is_empty() {
        return Ok(());
    }
    Err(CoreError::InvalidInput(format!(
        "bars are missing required columns {}; got {}",
        py_list(&missing),
        py_list(&column_names(bars))
    )))
}

/// `(week_key, month_key)` for one date: the ISO-week and calendar-month buckets.
#[must_use]
pub fn bucket_key_of(day: NaiveDate) -> (i64, i64) {
    let iso = day.iso_week();
    let week = i64::from(iso.year()) * WEEK_KEY_SCALE + i64::from(iso.week());
    let month = i64::from(day.year()) * MONTHS_IN_YEAR + i64::from(day.month()) - 1;
    (week, month)
}

/// Ports `bucket_keys`: Python returns the two expressions; here they are evaluated over the
/// frame's `date` column and returned as the `Int64` columns `week_key` and `month_key`.
///
/// # Errors
/// When the frame has no `date` column (polars' `ColumnNotFoundError`).
pub fn bucket_keys(frame: &DataFrame) -> CoreResult<(Column, Column)> {
    let days = dates(frame, "date")?;
    let mut weeks = Vec::with_capacity(days.len());
    let mut months = Vec::with_capacity(days.len());
    for day in days {
        match day.map(frame::date_of).transpose()? {
            Some(d) => {
                let (w, m) = bucket_key_of(d);
                weeks.push(Some(w));
                months.push(Some(m));
            }
            None => {
                weeks.push(None);
                months.push(None);
            }
        }
    }
    Ok((
        Series::new("week_key".into(), weeks).into_column(),
        Series::new("month_key".into(), months).into_column(),
    ))
}

/// Ports `baskfy_core.vbt.indicators.densify`, which `twt.indicators` imports: one row per
/// (instrument, calendar session), nulls where the name did not print. VBT's own function (a
/// private copy until the wave-3 housekeeping, DECISIONS-RS R3.4).
pub use crate::vbt::indicators::densify;

/// Ports `_prepare`: the optional columns added as typed nulls when absent (in
/// [`OPTIONAL_COLUMNS`] order), prices and volume cast to `Float64`, `adj_factor` null-filled
/// with 1 and `is_etf` with `false`.
fn prepare(bars: &DataFrame) -> CoreResult<DataFrame> {
    let mut frame = bars.clone();
    let height = frame.height();
    for name in OPTIONAL_COLUMNS {
        if !has(&frame, name) {
            let dtype = match name {
                "symbol" => DataType::String,
                "is_etf" => DataType::Boolean,
                _ => DataType::Float64,
            };
            put(
                &mut frame,
                Series::full_null(name.into(), height, &dtype).into_column(),
            )?;
        }
    }
    for name in ["open", "high", "low", "close", "close_raw", "volume"] {
        let cast = Series::new(name.into(), floats(&frame, name)?).into_column();
        put(&mut frame, cast)?;
    }
    let adj: Vec<F> = floats(&frame, "adj_factor")?
        .into_iter()
        .map(|v| Some(v.unwrap_or(1.0)))
        .collect();
    put(
        &mut frame,
        Series::new("adj_factor".into(), adj).into_column(),
    )?;
    let etf: Vec<bool> = bools(&frame, "is_etf")?
        .into_iter()
        .map(|v| v.unwrap_or(false))
        .collect();
    put(&mut frame, Series::new("is_etf".into(), etf).into_column())?;
    Ok(frame)
}

/// Ports `with_twt_indicators`: densify against the calendar, prepare, then add `week_key`,
/// `month_key` (`Int64`), `turnover_inr = close_raw x volume`, `vol_sma` (mean volume over
/// `scan.vol_sma_bars`, today included), `turnover_avg_20` (mean turnover over
/// `entry.turnover_avg_bars`), `sma_dma` (mean close over `breadth.dma_bars`) and
/// `limit_locked` (`open == high == low` on a printed bar).
///
/// # Errors
/// [`CoreError::InvalidInput`] for a missing required column (Python's `ValueError`), or a
/// polars error from the kernels.
pub fn with_twt_indicators(
    bars: &DataFrame,
    calendar: &SessionCalendar,
    config: &TwtConfig,
) -> CoreResult<DataFrame> {
    require_columns(bars)?;
    let mut frame = prepare(&densify(bars, calendar)?)?;
    let (week_key, month_key) = bucket_keys(&frame)?;
    put(&mut frame, week_key)?;
    put(&mut frame, month_key)?;
    let close_raw = floats(&frame, "close_raw")?;
    let volume = floats(&frame, "volume")?;
    let turnover: Vec<F> = close_raw
        .iter()
        .zip(&volume)
        .map(|(c, v)| Some((*c)? * (*v)?))
        .collect();
    put(
        &mut frame,
        Series::new("turnover_inr".into(), turnover.clone()).into_column(),
    )?;

    let groups = frame::groups(&ints(&frame, "instrument_id")?);
    let data = &config.data;
    let close = floats(&frame, "close")?;
    let vol_sma = frame::rolling_mean_over(
        &volume,
        &groups,
        config.scan.vol_sma_bars,
        data.min_samples(config.scan.vol_sma_bars),
    )?;
    let turnover_avg = frame::rolling_mean_over(
        &turnover,
        &groups,
        config.entry.turnover_avg_bars,
        data.min_samples(config.entry.turnover_avg_bars),
    )?;
    let sma = frame::rolling_mean_over(
        &close,
        &groups,
        config.breadth.dma_bars,
        data.min_samples(config.breadth.dma_bars),
    )?;
    let open = floats(&frame, "open")?;
    let high = floats(&frame, "high")?;
    let low = floats(&frame, "low")?;
    let locked: Vec<bool> = (0..frame.height())
        .map(|i| match (open[i], high[i], low[i]) {
            (Some(o), Some(h), Some(l)) => crate::pl::eq(o, h) && crate::pl::eq(h, l),
            _ => false,
        })
        .collect();
    put(
        &mut frame,
        Series::new("vol_sma".into(), vol_sma).into_column(),
    )?;
    put(
        &mut frame,
        Series::new("turnover_avg_20".into(), turnover_avg).into_column(),
    )?;
    put(&mut frame, Series::new("sma_dma".into(), sma).into_column())?;
    put(
        &mut frame,
        Series::new("limit_locked".into(), locked).into_column(),
    )?;
    Ok(frame)
}

/// Frame plumbing shared by the TWT ports: typed column reads, Python reprs for error text,
/// stable sorts with polars' null placement, and polars' rolling-mean kernel per instrument.
pub(crate) mod frame {
    use std::cmp::Ordering;
    use std::ops::Range;

    use chrono::NaiveDate;
    use polars::prelude::{Column, DataFrame, DataType, IdxCa, IdxSize, NamedFrom, Series};

    use crate::error::{CoreError, CoreResult};

    /// A nullable float cell.
    pub(crate) type F = Option<f64>;

    /// True when the frame holds `name`.
    pub(crate) fn has(df: &DataFrame, name: &str) -> bool {
        df.get_column_index(name).is_some()
    }

    /// The frame's column names, in order.
    pub(crate) fn column_names(df: &DataFrame) -> Vec<String> {
        df.columns().iter().map(|c| c.name().to_string()).collect()
    }

    fn series(df: &DataFrame, name: &str, dtype: &DataType) -> CoreResult<Series> {
        Ok(df.column(name)?.as_materialized_series().cast(dtype)?)
    }

    /// `name` as nullable floats (cast to `Float64`).
    pub(crate) fn floats(df: &DataFrame, name: &str) -> CoreResult<Vec<F>> {
        Ok(series(df, name, &DataType::Float64)?
            .f64()?
            .iter()
            .collect())
    }

    /// `name` as nullable 64-bit integers.
    pub(crate) fn ints(df: &DataFrame, name: &str) -> CoreResult<Vec<Option<i64>>> {
        Ok(series(df, name, &DataType::Int64)?.i64()?.iter().collect())
    }

    /// `name` as nullable booleans.
    pub(crate) fn bools(df: &DataFrame, name: &str) -> CoreResult<Vec<Option<bool>>> {
        Ok(series(df, name, &DataType::Boolean)?
            .bool()?
            .iter()
            .collect())
    }

    /// `name` as nullable strings.
    pub(crate) fn strings(df: &DataFrame, name: &str) -> CoreResult<Vec<Option<String>>> {
        Ok(series(df, name, &DataType::String)?
            .str()?
            .iter()
            .map(|s| s.map(str::to_owned))
            .collect())
    }

    /// `name` as nullable days since the epoch (cast to `Date`).
    pub(crate) fn dates(df: &DataFrame, name: &str) -> CoreResult<Vec<Option<i32>>> {
        Ok(series(df, name, &DataType::Date)?
            .date()?
            .physical()
            .iter()
            .collect())
    }

    fn epoch() -> NaiveDate {
        NaiveDate::from_ymd_opt(1970, 1, 1).unwrap_or_default()
    }

    /// Days since the epoch to a date.
    pub(crate) fn date_of(days: i32) -> CoreResult<NaiveDate> {
        epoch()
            .checked_add_signed(chrono::Duration::days(i64::from(days)))
            .ok_or_else(|| CoreError::InvalidInput(format!("date out of range: {days}")))
    }

    /// `with_columns(col)`: replaces a column of the same name in place, else appends.
    pub(crate) fn put(df: &mut DataFrame, col: Column) -> CoreResult<()> {
        df.with_column(col)?;
        Ok(())
    }

    /// Row positions as polars' take indices.
    pub(crate) fn idx(rows: &[usize]) -> CoreResult<IdxCa> {
        let v = rows
            .iter()
            .map(|&r| IdxSize::try_from(r))
            .collect::<Result<Vec<IdxSize>, _>>()
            .map_err(|e| CoreError::InvalidInput(format!("row index: {e}")))?;
        Ok(IdxCa::from_vec("".into(), v))
    }

    /// `df` reordered (or filtered) to `rows`.
    pub(crate) fn take_rows(df: &DataFrame, rows: &[usize]) -> CoreResult<DataFrame> {
        Ok(df.take(&idx(rows)?)?)
    }

    /// Ascending with nulls first (polars' default `sort`).
    pub(crate) fn cmp_nulls_first<T: PartialOrd>(a: &Option<T>, b: &Option<T>) -> Ordering {
        match (a, b) {
            (None, None) => Ordering::Equal,
            (None, Some(_)) => Ordering::Less,
            (Some(_), None) => Ordering::Greater,
            (Some(x), Some(y)) => x.partial_cmp(y).unwrap_or(Ordering::Equal),
        }
    }

    /// Contiguous runs of equal instrument ids — the groups of `.over("instrument_id")` on a
    /// frame sorted by instrument.
    pub(crate) fn groups(ids: &[Option<i64>]) -> Vec<Range<usize>> {
        let mut out = Vec::new();
        let mut start = 0;
        for i in 1..=ids.len() {
            if i == ids.len() || ids[i] != ids[start] {
                if i > start {
                    out.push(start..i);
                }
                start = i;
            }
        }
        out
    }

    fn usize_of(value: i64, what: &str) -> CoreResult<usize> {
        usize::try_from(value)
            .map_err(|_| CoreError::InvalidInput(format!("{what} must be >= 0, got {value}")))
    }

    /// `rolling_mean(window, min_samples=min).over(...)` — polars' kernel on each group.
    pub(crate) fn rolling_mean_over(
        values: &[F],
        groups: &[Range<usize>],
        window: i64,
        min: i64,
    ) -> CoreResult<Vec<F>> {
        let window = usize_of(window, "window_size")?;
        let min = usize_of(min, "min_samples")?;
        let mut out = Vec::with_capacity(values.len());
        for g in groups {
            out.extend(rolling(&values[g.clone()], window, min, false, false)?);
        }
        Ok(out)
    }

    /// polars' fixed-window rolling mean (`median == false`) or linear median, optionally
    /// centred, over one slice.
    pub(crate) fn rolling(
        values: &[F],
        window: usize,
        min: usize,
        center: bool,
        median: bool,
    ) -> CoreResult<Vec<F>> {
        use polars::polars_compute::rolling::{
            QuantileMethod, RollingFnParams, RollingQuantileParams,
        };
        use polars::prelude::{RollingOptionsFixedWindow, SeriesOpsTime};
        if values.is_empty() {
            return Ok(Vec::new());
        }
        let s = Series::new("".into(), values.to_vec());
        let mut options = RollingOptionsFixedWindow {
            window_size: window,
            min_periods: min,
            weights: None,
            center,
            fn_params: None,
        };
        let out = if median {
            options.fn_params = Some(RollingFnParams::Quantile(RollingQuantileParams {
                prob: 0.5,
                method: QuantileMethod::Linear,
            }));
            s.rolling_quantile(options)?
        } else {
            s.rolling_mean(options)?
        };
        Ok(out.cast(&DataType::Float64)?.f64()?.iter().collect())
    }

    /// Python's `repr` of a list of strings: `['a', 'b']`.
    pub(crate) fn py_list(items: &[String]) -> String {
        let inner: Vec<String> = items
            .iter()
            .map(|s| crate::factor_registry::py_repr_str(s))
            .collect();
        format!("[{}]", inner.join(", "))
    }
}
