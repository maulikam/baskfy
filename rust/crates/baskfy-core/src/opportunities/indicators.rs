//! Port of `baskfy_core.opportunities.indicators`: trailing per-bar columns the swing setups
//! read. Every window ends on the bar itself (or the bar before it), so nothing looks ahead.

use std::cmp::Ordering;
use std::collections::HashMap;
use std::ops::Range;

use chrono::NaiveDate;
use polars::prelude::{BooleanChunked, DataFrame, DataType, IntoColumn, NamedFrom, Series};

use crate::error::{CoreError, CoreResult};
use crate::opportunities::config::SwingParams;
use crate::twt::indicators::frame::{
    F, bools, cmp_nulls_first, date_of, dates, floats, groups, has, ints, put, py_list,
    strings, take_rows,
};

/// Ports `BAR_COLUMNS`: the columns [`with_indicators`] requires.
pub const BAR_COLUMNS: [&str; 8] = [
    "instrument_id",
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
];

const PCT: f64 = 100.0;
const TRADING_DAYS: f64 = 252.0;
const VOLUME_BARS: i64 = 20;
const HV_BARS: i64 = 20;
const SMA_FAST: i64 = 20;
const SMA_MID: i64 = 50;

/// Ports `require_bars`.
///
/// # Errors
/// [`CoreError::InvalidInput`] naming the missing columns.
pub fn require_bars(bars: &DataFrame) -> CoreResult<()> {
    let missing: Vec<String> = BAR_COLUMNS
        .iter()
        .filter(|c| !has(bars, c))
        .map(|c| (*c).to_owned())
        .collect();
    if missing.is_empty() {
        return Ok(());
    }
    Err(CoreError::InvalidInput(format!(
        "bars are missing required columns {}",
        py_list(&missing)
    )))
}

#[derive(Clone, Copy)]
enum Roll {
    Mean,
    Max,
    Min,
    Std,
}

fn window_of(window: i64) -> CoreResult<usize> {
    usize::try_from(window)
        .map_err(|_| CoreError::InvalidInput(format!("window_size must be >= 0, got {window}")))
}

/// `rolling_*(window).over(instrument_id)` with polars' kernel and `min_samples = window`.
fn roll_over(values: &[F], parts: &[Range<usize>], kind: Roll, window: i64) -> CoreResult<Vec<F>> {
    use polars::prelude::{
        RollingFnParams, RollingOptionsFixedWindow, RollingVarParams, SeriesOpsTime,
    };
    let window = window_of(window)?;
    let mut out = Vec::with_capacity(values.len());
    for part in parts {
        let slice = &values[part.clone()];
        if slice.is_empty() {
            continue;
        }
        let s = Series::new("".into(), slice.to_vec());
        let mut options = RollingOptionsFixedWindow {
            window_size: window,
            min_periods: window,
            weights: None,
            center: false,
            fn_params: None,
        };
        let rolled = match kind {
            Roll::Mean => s.rolling_mean(options)?,
            Roll::Max => s.rolling_max(options)?,
            Roll::Min => s.rolling_min(options)?,
            Roll::Std => {
                options.fn_params = Some(RollingFnParams::Var(RollingVarParams { ddof: 1 }));
                s.rolling_std(options)?
            }
        };
        out.extend(rolled.cast(&DataType::Float64)?.f64()?.iter());
    }
    Ok(out)
}

/// `shift(n).over(instrument_id)`: positive lags, negative leads, nulls at the edges.
fn shift_over<T: Copy>(values: &[Option<T>], parts: &[Range<usize>], n: i64) -> Vec<Option<T>> {
    let mut out = Vec::with_capacity(values.len());
    for part in parts {
        let slice = &values[part.clone()];
        let len = i64::try_from(slice.len()).unwrap_or(i64::MAX);
        for i in 0..len {
            let src = i - n;
            let v = if (0..len).contains(&src) {
                usize::try_from(src).ok().and_then(|k| slice.get(k).copied().flatten())
            } else {
                None
            };
            out.push(v);
        }
    }
    out
}

fn zip2(a: &[F], b: &[F], f: impl Fn(f64, f64) -> f64) -> Vec<F> {
    a.iter()
        .zip(b)
        .map(|(x, y)| Some(f((*x)?, (*y)?)))
        .collect()
}

fn map1(a: &[F], f: impl Fn(f64) -> f64) -> Vec<F> {
    a.iter().map(|x| x.map(&f)).collect()
}

fn float_col(name: &str, values: Vec<F>) -> polars::prelude::Column {
    Series::new(name.into(), values).into_column()
}

/// Stable `sort([instrument_id, date])`, nulls first.
fn sort_bars(bars: &DataFrame) -> CoreResult<DataFrame> {
    let ids = ints(bars, "instrument_id")?;
    let days = dates(bars, "date")?;
    let mut order: Vec<usize> = (0..bars.height()).collect();
    order.sort_by(|&a, &b| match cmp_nulls_first(&ids[a], &ids[b]) {
        Ordering::Equal => cmp_nulls_first(&days[a], &days[b]),
        other => other,
    });
    take_rows(bars, &order)
}

/// Left join of `benchmark.level` on `date`, left order kept, one row per matching level.
fn join_benchmark(frame: DataFrame, benchmark: Option<&DataFrame>) -> CoreResult<DataFrame> {
    let height = frame.height();
    let Some(bench) = benchmark.filter(|b| b.height() > 0) else {
        let mut frame = frame;
        put(&mut frame, float_col("bench_level", vec![None; height]))?;
        return Ok(frame);
    };
    let bench_days = dates(bench, "date")?;
    let levels = floats(bench, "level")?;
    let mut by_day: HashMap<i32, Vec<F>> = HashMap::new();
    for (day, level) in bench_days.iter().zip(&levels) {
        if let Some(d) = day {
            by_day.entry(*d).or_default().push(*level);
        }
    }
    let days = dates(&frame, "date")?;
    let mut rows = Vec::with_capacity(height);
    let mut joined = Vec::with_capacity(height);
    for (i, day) in days.iter().enumerate() {
        match day.and_then(|d| by_day.get(&d)) {
            Some(found) => {
                for level in found {
                    rows.push(i);
                    joined.push(*level);
                }
            }
            None => {
                rows.push(i);
                joined.push(None);
            }
        }
    }
    let mut out = if rows.len() == height && rows.iter().enumerate().all(|(i, r)| i == *r) {
        frame
    } else {
        take_rows(&frame, &rows)?
    };
    put(&mut out, float_col("bench_level", joined))?;
    Ok(out)
}

/// Ports `with_indicators`: sort by instrument and date, add the optional `turnover` and
/// `adj_factor` columns, left-join the benchmark level, then add the trailing indicator columns
/// (`prev_close` .. `touched_fast`), each computed per instrument.
///
/// # Errors
/// [`CoreError::InvalidInput`] for a missing bar column or a negative window; a polars error.
pub fn with_indicators(
    bars: &DataFrame,
    benchmark: Option<&DataFrame>,
    params: &SwingParams,
) -> CoreResult<DataFrame> {
    require_bars(bars)?;
    let mut frame = sort_bars(bars)?;
    let height = frame.height();
    for optional in ["turnover", "adj_factor"] {
        if !has(&frame, optional) {
            put(&mut frame, float_col(optional, vec![None; height]))?;
        }
    }
    for name in ["open", "high", "low", "close", "volume", "turnover"] {
        let cast = floats(&frame, name)?;
        put(&mut frame, float_col(name, cast))?;
    }
    let adj: Vec<F> = floats(&frame, "adj_factor")?
        .into_iter()
        .map(|v| Some(v.unwrap_or(1.0)))
        .collect();
    put(&mut frame, float_col("adj_factor", adj))?;
    let mut frame = join_benchmark(frame, benchmark)?;

    let risk = &params.risk;
    let brk = &params.breakout;
    let pb = &params.pullback;
    let rs = &params.relative_strength;

    let parts = groups(&ints(&frame, "instrument_id")?);
    let close = floats(&frame, "close")?;
    let high = floats(&frame, "high")?;
    let low = floats(&frame, "low")?;
    let volume = floats(&frame, "volume")?;
    let turnover = floats(&frame, "turnover")?;
    let bench = floats(&frame, "bench_level")?;

    // Block 1.
    let prev_close = shift_over(&close, &parts, 1);
    let prev_high = shift_over(&high, &parts, 1);
    let mut bars_n: Vec<Option<i64>> = Vec::with_capacity(frame.height());
    for part in &parts {
        bars_n.extend((1..=part.len()).map(|n| i64::try_from(n).ok()));
    }
    let turnover_inr: Vec<F> = turnover
        .iter()
        .zip(close.iter().zip(&volume))
        .map(|(t, (c, v))| match t {
            Some(t) if crate::pl::gt(*t, 0.0) => Some(*t),
            _ => Some((*c)? * (*v)?),
        })
        .collect();

    // Block 2.
    let hl = zip2(&high, &low, |h, l| h - l);
    let hpc = zip2(&high, &prev_close, |h, p| (h - p).abs());
    let lpc = zip2(&low, &prev_close, |l, p| (l - p).abs());
    let true_range = crate::pl::max_horizontal(&[hl.as_slice(), hpc.as_slice(), lpc.as_slice()]);
    let log_ret = zip2(&close, &prev_close, |c, p| (c / p).ln());

    // Block 3.
    let atr = roll_over(&true_range, &parts, Roll::Mean, risk.atr_bars)?;
    let sma_fast = roll_over(&close, &parts, Roll::Mean, SMA_FAST)?;
    let sma_mid = roll_over(&close, &parts, Roll::Mean, SMA_MID)?;
    let sma_long = roll_over(&close, &parts, Roll::Mean, params.data.long_trend_bars)?;
    let hv_pct = map1(&roll_over(&log_ret, &parts, Roll::Std, HV_BARS)?, |s| {
        s * TRADING_DAYS.sqrt() * PCT
    });
    let vol_avg = roll_over(
        &shift_over(&volume, &parts, 1),
        &parts,
        Roll::Mean,
        VOLUME_BARS,
    )?;
    let turnover_avg = roll_over(&turnover_inr, &parts, Roll::Mean, VOLUME_BARS)?;
    let volume_avg = roll_over(&volume, &parts, Roll::Mean, VOLUME_BARS)?;
    let pivot = roll_over(&prev_high, &parts, Roll::Max, brk.pivot_lookback)?;
    let base_low = roll_over(&shift_over(&low, &parts, 1), &parts, Roll::Min, brk.pivot_lookback)?;
    let swing_high = roll_over(&high, &parts, Roll::Max, pb.swing_lookback)?;
    let pullback_low = roll_over(&low, &parts, Roll::Min, pb.pullback_bars)?;
    let rs_trigger = roll_over(&prev_high, &parts, Roll::Max, rs.trigger_lookback)?;
    let ret_lb = zip2(&close, &shift_over(&close, &parts, rs.lookback), |c, p| {
        (c / p - 1.0) * PCT
    });
    let bench_ret_lb = zip2(&bench, &shift_over(&bench, &parts, rs.lookback), |c, p| {
        (c / p - 1.0) * PCT
    });
    let rs_line = zip2(&close, &bench, |c, b| c / b);

    // Block 4.
    let atr_pct = zip2(&atr, &close, |a, c| a / c * PCT);
    let rvol: Vec<F> = volume
        .iter()
        .zip(&vol_avg)
        .map(|(v, a)| match a {
            Some(a) if crate::pl::gt(*a, 0.0) => v.map(|v| v / *a),
            _ => None,
        })
        .collect();
    let sma_mid_prev = shift_over(&sma_mid, &parts, pb.slope_bars);
    let recent_low = roll_over(&low, &parts, Roll::Min, pb.pullback_bars)?;
    let rs_line_high = roll_over(&rs_line, &parts, Roll::Max, rs.lookback)?;
    let rs_excess_pct = zip2(&ret_lb, &bench_ret_lb, |a, b| a - b);
    let touch_gap = zip2(&sma_fast, &low, |s, l| s - l);

    // Block 5: the touch flag as 1/0 so polars' max kernel answers "any touch in the window".
    let reach = map1(&atr, |a| a * pb.touch_atr);
    let touch: Vec<F> = touch_gap
        .iter()
        .zip(&reach)
        .map(|(g, r)| crate::pl::cmp(*g, *r, |o| o != Ordering::Greater).map(f64::from))
        .collect();
    let touched_fast: Vec<bool> = roll_over(&touch, &parts, Roll::Max, pb.pullback_bars)?
        .into_iter()
        .map(|v| v.is_some_and(|x| x > 0.0))
        .collect();

    let columns: Vec<(&str, Vec<F>)> = vec![
        ("prev_close", prev_close),
        ("prev_high", prev_high),
    ];
    for (name, values) in columns {
        put(&mut frame, float_col(name, values))?;
    }
    put(&mut frame, Series::new("bars".into(), bars_n).into_column())?;
    let columns: Vec<(&str, Vec<F>)> = vec![
        ("turnover_inr", turnover_inr),
        ("true_range", true_range),
        ("log_ret", log_ret),
        ("atr", atr),
        ("sma_fast", sma_fast),
        ("sma_mid", sma_mid),
        ("sma_long", sma_long),
        ("hv_pct", hv_pct),
        ("vol_avg", vol_avg),
        ("turnover_avg", turnover_avg),
        ("volume_avg", volume_avg),
        ("pivot", pivot),
        ("base_low", base_low),
        ("swing_high", swing_high),
        ("pullback_low", pullback_low),
        ("rs_trigger", rs_trigger),
        ("ret_lb", ret_lb),
        ("bench_ret_lb", bench_ret_lb),
        ("rs_line", rs_line),
        ("atr_pct", atr_pct),
        ("rvol", rvol),
        ("sma_mid_prev", sma_mid_prev),
        ("recent_low", recent_low),
        ("rs_line_high", rs_line_high),
        ("rs_excess_pct", rs_excess_pct),
    ];
    for (name, values) in columns {
        put(&mut frame, float_col(name, values))?;
    }
    put(
        &mut frame,
        Series::new("touched_fast".into(), touched_fast).into_column(),
    )?;
    Ok(frame)
}

/// One indicated row, as the evaluators read it (Python reads `iter_rows(named=True)` dicts).
/// A column absent from the frame reads as `None`, as `row.get` does.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct IndicatorRow {
    /// `instrument_id`.
    pub instrument_id: Option<i64>,
    /// `symbol`.
    pub symbol: Option<String>,
    /// `date`.
    pub date: Option<NaiveDate>,
    /// `open`.
    pub open: F,
    /// `high`.
    pub high: F,
    /// `low`.
    pub low: F,
    /// `close`.
    pub close: F,
    /// `adj_factor`.
    pub adj_factor: F,
    /// `bench_level`.
    pub bench_level: F,
    /// `prev_high`.
    pub prev_high: F,
    /// `bars`: sessions of history up to and including this one.
    pub bars: Option<i64>,
    /// `atr`.
    pub atr: F,
    /// `sma_mid`.
    pub sma_mid: F,
    /// `sma_long`.
    pub sma_long: F,
    /// `hv_pct`.
    pub hv_pct: F,
    /// `turnover_avg`.
    pub turnover_avg: F,
    /// `volume_avg`.
    pub volume_avg: F,
    /// `pivot`.
    pub pivot: F,
    /// `base_low`.
    pub base_low: F,
    /// `swing_high`.
    pub swing_high: F,
    /// `rs_trigger`.
    pub rs_trigger: F,
    /// `rs_line`.
    pub rs_line: F,
    /// `atr_pct`.
    pub atr_pct: F,
    /// `rvol`.
    pub rvol: F,
    /// `sma_mid_prev`.
    pub sma_mid_prev: F,
    /// `recent_low`.
    pub recent_low: F,
    /// `rs_line_high`.
    pub rs_line_high: F,
    /// `rs_excess_pct`.
    pub rs_excess_pct: F,
    /// `touched_fast`.
    pub touched_fast: Option<bool>,
}

fn opt_floats(df: &DataFrame, name: &str) -> CoreResult<Vec<F>> {
    if has(df, name) {
        floats(df, name)
    } else {
        Ok(vec![None; df.height()])
    }
}

/// The frame's rows as [`IndicatorRow`]s, in frame order.
///
/// # Errors
/// A polars cast error (a column of an unexpected type).
pub fn indicator_rows(frame: &DataFrame) -> CoreResult<Vec<IndicatorRow>> {
    let h = frame.height();
    let ids = if has(frame, "instrument_id") {
        ints(frame, "instrument_id")?
    } else {
        vec![None; h]
    };
    let symbols = if has(frame, "symbol") {
        strings(frame, "symbol")?
    } else {
        vec![None; h]
    };
    let days = if has(frame, "date") {
        dates(frame, "date")?
            .into_iter()
            .map(|d| d.map(date_of).transpose())
            .collect::<CoreResult<Vec<_>>>()?
    } else {
        vec![None; h]
    };
    let bars = if has(frame, "bars") {
        ints(frame, "bars")?
    } else {
        vec![None; h]
    };
    let touched = if has(frame, "touched_fast") {
        bools(frame, "touched_fast")?
    } else {
        vec![None; h]
    };
    let mut cols: HashMap<&str, Vec<F>> = HashMap::new();
    for name in [
        "open",
        "high",
        "low",
        "close",
        "adj_factor",
        "bench_level",
        "prev_high",
        "atr",
        "sma_mid",
        "sma_long",
        "hv_pct",
        "turnover_avg",
        "volume_avg",
        "pivot",
        "base_low",
        "swing_high",
        "rs_trigger",
        "rs_line",
        "atr_pct",
        "rvol",
        "sma_mid_prev",
        "recent_low",
        "rs_line_high",
        "rs_excess_pct",
    ] {
        cols.insert(name, opt_floats(frame, name)?);
    }
    let get = |name: &str, i: usize| cols.get(name).and_then(|c| c.get(i).copied().flatten());
    Ok((0..h)
        .map(|i| IndicatorRow {
            instrument_id: ids[i],
            symbol: symbols[i].clone(),
            date: days[i],
            open: get("open", i),
            high: get("high", i),
            low: get("low", i),
            close: get("close", i),
            adj_factor: get("adj_factor", i),
            bench_level: get("bench_level", i),
            prev_high: get("prev_high", i),
            bars: bars[i],
            atr: get("atr", i),
            sma_mid: get("sma_mid", i),
            sma_long: get("sma_long", i),
            hv_pct: get("hv_pct", i),
            turnover_avg: get("turnover_avg", i),
            volume_avg: get("volume_avg", i),
            pivot: get("pivot", i),
            base_low: get("base_low", i),
            swing_high: get("swing_high", i),
            rs_trigger: get("rs_trigger", i),
            rs_line: get("rs_line", i),
            atr_pct: get("atr_pct", i),
            rvol: get("rvol", i),
            sma_mid_prev: get("sma_mid_prev", i),
            recent_low: get("recent_low", i),
            rs_line_high: get("rs_line_high", i),
            rs_excess_pct: get("rs_excess_pct", i),
            touched_fast: touched[i],
        })
        .collect())
}

/// The rows dated `as_of` (Python's `indicated.filter(pl.col("date") == as_of)`), frame order.
///
/// # Errors
/// A polars error reading or filtering the frame.
pub fn rows_on(frame: &DataFrame, as_of: NaiveDate) -> CoreResult<Vec<IndicatorRow>> {
    let days = dates(frame, "date")?;
    let mask: BooleanChunked = days
        .iter()
        .map(|d| Some(d.and_then(|d| date_of(d).ok()) == Some(as_of)))
        .collect();
    indicator_rows(&frame.filter(&mask)?)
}
