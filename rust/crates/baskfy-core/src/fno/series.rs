//! Port of `baskfy_core.fno.series` (`decile-blueprint/packages/core/src/baskfy_core/fno/series.py`).
//!
//! The continuous futures series and its derived columns (`04` §4, `03` §2):
//!
//! * `held_expiry` — the nearest expiry **strictly after the previous session**;
//! * `ret = ln(settle_t / settle_{t-1})` **of that one contract**, so a roll is never a jump;
//!   the levels `level_o/h/l/c` compound `ret` into a roll-free line from 1.0;
//! * `ca_flag` — the held contract's settle moved above `ca_up_ratio` or below
//!   `ca_down_ratio`: excluded from the series (no level, no ret) and `ca_recent` keeps it out
//!   of signals for `ca_exclusion_sessions` sessions, the flagged one included;
//! * `atr14` (true range on the levels, simple mean), `rv20 = stdev(ret, 20) x √252`,
//!   `oi_total`, `fut_turnover_20d` (20-session median), and `basis_ann` on UDiFF days only.
//!
//! Python writes this as polars expressions (joins, `group_by`, `.over("symbol")` windows).
//! The port walks the same steps over plain rows and calls **polars' own rolling kernels**
//! per symbol (what `.over` does), so the floats are the ones polars computes; the cumulative
//! sum is polars' sequential one.
//!
//! Goldens: `L1/fno/series.continuous_futures.case_NNN.json`.

use std::cmp::Ordering;
use std::collections::BTreeMap;

use chrono::NaiveDate;
use polars::prelude::{DataFrame, DataType};

use super::config::{FnoError, FnoResult, SeriesConfig};

/// The columns [`continuous_futures`] reads (`FO_BHAVCOPY_SCHEMA`'s futures subset).
///
/// Ports `baskfy_core.fno.series.FUTURES_COLUMNS`.
pub const FUTURES_COLUMNS: [&str; 12] = [
    "trade_date",
    "symbol",
    "instrument",
    "expiry",
    "open",
    "high",
    "low",
    "settle",
    "underlying",
    "open_interest",
    "turnover",
    "lot_size",
];

/// Reading and writing polars columns as plain values (shared by the fno frame ports).
pub mod cols {
    use chrono::NaiveDate;
    use polars::prelude::{
        Column, DataFrame, DataType, IntoColumn, NamedFrom, RollingFnParams,
        RollingOptionsFixedWindow, RollingVarParams, Series, SeriesOpsTime,
    };

    use super::super::config::FnoResult;

    fn epoch() -> NaiveDate {
        NaiveDate::from_ymd_opt(1970, 1, 1).unwrap_or_default()
    }

    /// A date as polars stores it (days since 1970-01-01).
    #[must_use]
    pub fn to_days(d: NaiveDate) -> i32 {
        i32::try_from((d - epoch()).num_days()).unwrap_or(i32::MAX)
    }

    /// A polars date back to a `NaiveDate`.
    #[must_use]
    pub fn from_days(days: i32) -> Option<NaiveDate> {
        epoch().checked_add_signed(chrono::TimeDelta::try_days(i64::from(days))?)
    }

    fn series(df: &DataFrame, name: &str, dtype: &DataType) -> FnoResult<Series> {
        Ok(df.column(name)?.as_materialized_series().cast(dtype)?)
    }

    /// A column as dates.
    ///
    /// # Errors
    /// A missing column or one that does not cast.
    pub fn dates(df: &DataFrame, name: &str) -> FnoResult<Vec<Option<NaiveDate>>> {
        let s = series(df, name, &DataType::Date)?;
        Ok(s.date()?
            .physical()
            .iter()
            .map(|d| d.and_then(from_days))
            .collect())
    }

    /// A column as `f64`.
    ///
    /// # Errors
    /// A missing column or one that does not cast.
    pub fn floats(df: &DataFrame, name: &str) -> FnoResult<Vec<Option<f64>>> {
        Ok(series(df, name, &DataType::Float64)?
            .f64()?
            .iter()
            .collect())
    }

    /// A column as `i64`.
    ///
    /// # Errors
    /// A missing column or one that does not cast.
    pub fn ints(df: &DataFrame, name: &str) -> FnoResult<Vec<Option<i64>>> {
        Ok(series(df, name, &DataType::Int64)?.i64()?.iter().collect())
    }

    /// A column as strings.
    ///
    /// # Errors
    /// A missing column or one that does not cast.
    pub fn strings(df: &DataFrame, name: &str) -> FnoResult<Vec<Option<String>>> {
        Ok(series(df, name, &DataType::String)?
            .str()?
            .iter()
            .map(|s| s.map(str::to_owned))
            .collect())
    }

    /// A column as booleans.
    ///
    /// # Errors
    /// A missing column or one that does not cast.
    pub fn bools(df: &DataFrame, name: &str) -> FnoResult<Vec<Option<bool>>> {
        Ok(series(df, name, &DataType::Boolean)?
            .bool()?
            .iter()
            .collect())
    }

    /// A `Date` column.
    ///
    /// # Errors
    /// Never in practice (an Int32 casts to Date).
    pub fn date_column(name: &str, values: &[Option<NaiveDate>]) -> FnoResult<Column> {
        let days: Vec<Option<i32>> = values.iter().map(|d| d.map(to_days)).collect();
        Ok(Series::new(name.into(), days)
            .cast(&DataType::Date)?
            .into_column())
    }

    /// A `Float64` column.
    #[must_use]
    pub fn float_column(name: &str, values: &[Option<f64>]) -> Column {
        Series::new(name.into(), values.to_vec()).into_column()
    }

    /// An `Int64` column.
    #[must_use]
    pub fn int_column(name: &str, values: &[Option<i64>]) -> Column {
        Series::new(name.into(), values.to_vec()).into_column()
    }

    /// A `String` column.
    #[must_use]
    pub fn string_column(name: &str, values: &[Option<String>]) -> Column {
        Series::new(name.into(), values.to_vec()).into_column()
    }

    /// A `Boolean` column.
    #[must_use]
    pub fn bool_column(name: &str, values: &[Option<bool>]) -> Column {
        Series::new(name.into(), values.to_vec()).into_column()
    }

    /// Which polars rolling kernel to call.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum Roll {
        /// `rolling_mean`.
        Mean,
        /// `rolling_std` (ddof 1).
        Std,
        /// `rolling_median` (`rolling_quantile(0.5, "linear")`).
        Median,
    }

    /// One slice's `rolling_*(window)` with `min_samples = window` — polars' kernel itself.
    ///
    /// # Errors
    /// A polars error (a zero window).
    pub fn rolling(
        values: &[Option<f64>],
        kind: Roll,
        window: usize,
    ) -> FnoResult<Vec<Option<f64>>> {
        use polars::polars_compute::rolling::RollingQuantileParams;
        use polars::prelude::QuantileMethod;
        if values.is_empty() {
            return Ok(Vec::new());
        }
        let s = Series::new("".into(), values.to_vec());
        let mut options = RollingOptionsFixedWindow {
            window_size: window,
            min_periods: window,
            weights: None,
            center: false,
            fn_params: None,
        };
        let out = match kind {
            Roll::Mean => s.rolling_mean(options)?,
            Roll::Std => {
                options.fn_params = Some(RollingFnParams::Var(RollingVarParams { ddof: 1 }));
                s.rolling_std(options)?
            }
            Roll::Median => {
                options.fn_params = Some(RollingFnParams::Quantile(RollingQuantileParams {
                    prob: 0.5,
                    method: QuantileMethod::Linear,
                }));
                s.rolling_quantile(options)?
            }
        };
        Ok(out.cast(&DataType::Float64)?.f64()?.iter().collect())
    }

    /// polars' `round(decimals)` (half to even), as its float kernel computes it.
    #[must_use]
    pub fn round(value: Option<f64>, decimals: i32) -> Option<f64> {
        value.map(|v| {
            let multiplier = 10.0_f64.powi(decimals);
            let ret = (v * multiplier).round_ties_even() / multiplier;
            if ret.is_finite() { ret } else { v }
        })
    }
}

/// One futures row after the `settle > 0` filter.
#[derive(Debug, Clone)]
struct Row {
    trade_date: NaiveDate,
    di: usize,
    symbol: Option<String>,
    instrument: Option<String>,
    expiry: Option<NaiveDate>,
    open: Option<f64>,
    high: Option<f64>,
    low: Option<f64>,
    settle: f64,
    underlying: Option<f64>,
    open_interest: Option<i64>,
    turnover: Option<f64>,
    lot_size: Option<i64>,
}

/// One held bar (`_held_bars`' output).
#[derive(Debug, Clone)]
struct Held {
    symbol: String,
    trade_date: NaiveDate,
    di: usize,
    held_expiry: NaiveDate,
    held_settle: f64,
    gc: f64,
    go: Option<f64>,
    gh: Option<f64>,
    gl: Option<f64>,
    ca_flag: bool,
}

/// `_levels`' columns for one non-CA held bar.
#[derive(Debug, Clone, Copy, Default)]
struct Levels {
    level_o: Option<f64>,
    level_h: Option<f64>,
    level_l: Option<f64>,
    level_c: Option<f64>,
    ret: Option<f64>,
    atr14: Option<f64>,
    rv20: Option<f64>,
}

/// `_daily_aggregates`: per (symbol, session).
#[derive(Debug, Clone, Default)]
struct Daily {
    oi_total: i64,
    fut_turnover: f64,
    lot_size: Option<i64>,
    spot: Option<f64>,
    instrument: Option<String>,
    seen: bool,
}

fn read_rows(futures: &DataFrame) -> FnoResult<(Vec<Row>, usize)> {
    for c in FUTURES_COLUMNS {
        futures.column(c)?;
    }
    let trade_date = cols::dates(futures, "trade_date")?;
    let symbol = cols::strings(futures, "symbol")?;
    let instrument = cols::strings(futures, "instrument")?;
    let expiry = cols::dates(futures, "expiry")?;
    let open = cols::floats(futures, "open")?;
    let high = cols::floats(futures, "high")?;
    let low = cols::floats(futures, "low")?;
    let settle = cols::floats(futures, "settle")?;
    let underlying = cols::floats(futures, "underlying")?;
    let oi = cols::ints(futures, "open_interest")?;
    let turnover = cols::floats(futures, "turnover")?;
    let lot = cols::ints(futures, "lot_size")?;
    // `filter(settle > 0)`, then `dates` = the distinct sessions (a null one would sort first
    // and take index 0), joined back on `trade_date` (a null date matches nothing).
    let kept: Vec<usize> = (0..settle.len())
        .filter(|&i| settle[i].is_some_and(|s| s > 0.0))
        .collect();
    let mut distinct: Vec<NaiveDate> = kept.iter().filter_map(|&i| trade_date[i]).collect();
    distinct.sort();
    distinct.dedup();
    let offset = usize::from(kept.iter().any(|&i| trade_date[i].is_none()));
    let index: BTreeMap<NaiveDate, usize> = distinct
        .iter()
        .enumerate()
        .map(|(k, d)| (*d, k + offset))
        .collect();
    let mut rows = Vec::with_capacity(kept.len());
    for i in kept {
        let (Some(day), Some(s)) = (trade_date[i], settle[i]) else {
            continue;
        };
        let Some(&di) = index.get(&day) else {
            continue;
        };
        rows.push(Row {
            trade_date: day,
            di,
            symbol: symbol[i].clone(),
            instrument: instrument[i].clone(),
            expiry: expiry[i],
            open: open[i],
            high: high[i],
            low: low[i],
            settle: s,
            underlying: underlying[i],
            open_interest: oi[i],
            turnover: turnover[i],
            lot_size: lot[i],
        });
    }
    Ok((rows, distinct.len() + offset))
}

fn ratio(x: Option<f64>, prev: f64) -> Option<f64> {
    x.map(|v| v / prev)
}

/// `_held_bars`: one row per (symbol, session) with the held contract's bar and its ratios.
fn held_bars(rows: &[Row], config: &SeriesConfig) -> Vec<Held> {
    // `front`: rows with expiry > trade_date, sorted by expiry, first per (symbol, di).
    let mut front_rows: Vec<&Row> = rows
        .iter()
        .filter(|r| r.expiry.is_some_and(|e| e > r.trade_date))
        .collect();
    front_rows.sort_by_key(|r| r.expiry);
    let mut front: BTreeMap<(String, usize), (NaiveDate, f64)> = BTreeMap::new();
    for r in front_rows {
        let (Some(sym), Some(exp)) = (r.symbol.clone(), r.expiry) else {
            continue;
        };
        front.entry((sym, r.di)).or_insert((exp, r.settle));
    }
    // `bars` keyed (symbol, di, expiry), joined inner on (symbol, di + 1, held_expiry).
    let mut bars: BTreeMap<(&str, usize, NaiveDate), Vec<&Row>> = BTreeMap::new();
    for r in rows {
        if let (Some(sym), Some(exp)) = (r.symbol.as_deref(), r.expiry) {
            bars.entry((sym, r.di, exp)).or_default().push(r);
        }
    }
    let mut out = Vec::new();
    for ((sym, di), (held_expiry, prev_settle)) in &front {
        let matched = bars
            .get(&(sym.as_str(), di + 1, *held_expiry))
            .map_or(&[][..], Vec::as_slice);
        for r in matched {
            let gc = r.settle / prev_settle;
            out.push(Held {
                symbol: sym.clone(),
                trade_date: r.trade_date,
                di: r.di,
                held_expiry: *held_expiry,
                held_settle: r.settle,
                gc,
                go: ratio(r.open, *prev_settle),
                gh: ratio(r.high, *prev_settle),
                gl: ratio(r.low, *prev_settle),
                // polars' total order: a NaN `gc` (NaN settle, 0/0) is the largest value, so it
                // flags the row as a corporate action and Python drops it (DECISIONS-RS R3.1).
                ca_flag: crate::pl::gt(gc, config.ca_up_ratio)
                    || crate::pl::lt(gc, config.ca_down_ratio),
            });
        }
    }
    out
}

/// Contiguous runs of one symbol in a frame sorted by it (`.over("symbol")`'s groups).
fn runs(symbols: &[&str]) -> Vec<std::ops::Range<usize>> {
    let mut out = Vec::new();
    let mut start = 0;
    for i in 1..=symbols.len() {
        if i == symbols.len() || symbols[i] != symbols[start] {
            if start < i {
                out.push(start..i);
            }
            start = i;
        }
    }
    out
}

fn usize_window(n: i64) -> FnoResult<usize> {
    usize::try_from(n).map_err(|_| FnoError::Value(format!("window_size {n} must be positive")))
}

/// `_levels`: compound the held contract's ratios into levels, then ATR and RV (non-CA rows,
/// sorted by symbol and session).
fn levels(good: &[&Held], config: &SeriesConfig) -> FnoResult<Vec<Levels>> {
    let replace = |g: Option<f64>, fallback: f64| match g {
        // `when((g <= 0) | g.is_null()).then(fallback)`: a NaN is kept, as polars keeps it.
        Some(v) if !matches!(v.partial_cmp(&0.0), Some(Ordering::Less | Ordering::Equal)) => v,
        _ => fallback,
    };
    let n = good.len();
    let mut out = vec![Levels::default(); n];
    let mut tr = vec![None; n];
    let mut ret = vec![None; n];
    let symbols: Vec<&str> = good.iter().map(|h| h.symbol.as_str()).collect();
    let groups = runs(&symbols);
    for g in &groups {
        let mut cum = 0.0_f64;
        let mut prev_c: Option<f64> = None;
        for i in g.clone() {
            let h = good[i];
            let go = replace(h.go, 1.0);
            let gh = replace(h.gh, if h.gc > 1.0 { h.gc } else { 1.0 });
            let gl = replace(h.gl, if h.gc < 1.0 { h.gc } else { 1.0 });
            let log_gc = h.gc.ln();
            cum += log_gc;
            let level_c = cum.exp();
            let prev = level_c / h.gc;
            let (level_o, level_h, level_l) = (prev * go, prev * gh, prev * gl);
            let mut range = level_h - level_l;
            if let Some(pc) = prev_c {
                for x in [(level_h - pc).abs(), (level_l - pc).abs()] {
                    if x > range {
                        range = x;
                    }
                }
            }
            tr[i] = Some(range);
            ret[i] = Some(log_gc);
            out[i] = Levels {
                level_o: Some(level_o),
                level_h: Some(level_h),
                level_l: Some(level_l),
                level_c: Some(level_c),
                ret: Some(log_gc),
                atr14: None,
                rv20: None,
            };
            prev_c = Some(level_c);
        }
    }
    #[allow(clippy::cast_precision_loss)]
    let annual = (config.annualisation_sessions as f64).sqrt();
    let atr_w = usize_window(config.atr_sessions)?;
    let rv_w = usize_window(config.rv_sessions)?;
    for g in &groups {
        let atr = cols::rolling(&tr[g.clone()], cols::Roll::Mean, atr_w)?;
        let rv = cols::rolling(&ret[g.clone()], cols::Roll::Std, rv_w)?;
        for (k, i) in g.clone().enumerate() {
            out[i].atr14 = atr[k];
            out[i].rv20 = rv[k].map(|v| v * annual);
        }
    }
    Ok(out)
}

/// `fo_underlying_daily`'s series columns from the futures rows of the bhavcopy.
///
/// `futures` carries [`FUTURES_COLUMNS`] (one row per contract per session; rows with a
/// non-positive settle are ignored). The sessions are the frame's own distinct dates. Returns
/// one row per (symbol, session the held contract printed), sorted by symbol and date.
///
/// Ports `baskfy_core.fno.series.continuous_futures`.
///
/// # Errors
/// A missing or mistyped column; a zero window.
pub fn continuous_futures(futures: &DataFrame, config: &SeriesConfig) -> FnoResult<DataFrame> {
    let (rows, _) = read_rows(futures)?;
    let mut held = held_bars(&rows, config);
    held.sort_by(|a, b| (a.symbol.as_str(), a.di).cmp(&(b.symbol.as_str(), b.di)));
    // `_levels` over the non-CA rows (already sorted by symbol, di).
    let good: Vec<&Held> = held.iter().filter(|h| !h.ca_flag).collect();
    let good_levels = levels(&good, config)?;
    let mut by_key: BTreeMap<(String, usize), Levels> = BTreeMap::new();
    for (h, l) in good.iter().zip(good_levels) {
        by_key.insert((h.symbol.clone(), h.di), l);
    }
    // `_daily_aggregates` over every kept futures row.
    let mut daily: BTreeMap<(String, NaiveDate), Daily> = BTreeMap::new();
    for r in &rows {
        let Some(sym) = r.symbol.clone() else {
            continue;
        };
        let d = daily.entry((sym, r.trade_date)).or_default();
        if !d.seen {
            d.instrument = r.instrument.clone();
            d.seen = true;
        }
        d.oi_total += r.open_interest.unwrap_or(0);
        d.fut_turnover += r.turnover.unwrap_or(0.0);
        if let Some(l) = r.lot_size {
            d.lot_size = Some(d.lot_size.map_or(l, |m| m.max(l)));
        }
        if let Some(u) = r.underlying {
            d.spot = Some(d.spot.map_or(u, |m| if u > m { u } else { m }));
        }
    }
    let n = held.len();
    let mut c_trade = Vec::with_capacity(n);
    let mut c_symbol = Vec::with_capacity(n);
    let mut c_instrument = Vec::with_capacity(n);
    let mut c_expiry = Vec::with_capacity(n);
    let mut c_settle = Vec::with_capacity(n);
    let mut c_lv: [Vec<Option<f64>>; 7] = Default::default();
    let mut c_oi = Vec::with_capacity(n);
    let mut c_turn = Vec::with_capacity(n);
    let mut c_lot = Vec::with_capacity(n);
    let mut c_spot = Vec::with_capacity(n);
    let mut c_basis = Vec::with_capacity(n);
    let mut c_ca = Vec::with_capacity(n);
    for h in &held {
        let l = by_key
            .get(&(h.symbol.clone(), h.di))
            .copied()
            .unwrap_or_default();
        let d = daily.get(&(h.symbol.clone(), h.trade_date));
        c_trade.push(Some(h.trade_date));
        c_symbol.push(Some(h.symbol.clone()));
        c_instrument.push(d.and_then(|d| d.instrument.clone()));
        c_expiry.push(Some(h.held_expiry));
        c_settle.push(Some(h.held_settle));
        for (k, v) in [
            l.level_o, l.level_h, l.level_l, l.level_c, l.ret, l.atr14, l.rv20,
        ]
        .into_iter()
        .enumerate()
        {
            c_lv[k].push(v);
        }
        c_oi.push(d.map(|d| d.oi_total));
        c_turn.push(d.map(|d| d.fut_turnover));
        c_lot.push(d.and_then(|d| d.lot_size));
        let spot = d.and_then(|d| d.spot);
        c_spot.push(spot);
        let days = (h.held_expiry - h.trade_date).num_days();
        let basis = match spot {
            Some(s) if h.trade_date >= config.basis_from && s > 0.0 && days > 0 => {
                #[allow(clippy::cast_precision_loss)]
                let d = days as f64;
                Some((h.held_settle / s - 1.0) * 365.0 / d)
            }
            _ => None,
        };
        c_basis.push(basis);
        c_ca.push(h.ca_flag);
    }
    // The `.over("symbol")` windows on the output, sorted by (symbol, di).
    let symbols: Vec<&str> = held.iter().map(|h| h.symbol.as_str()).collect();
    let groups = runs(&symbols);
    let mut c_turn20 = vec![None; n];
    let mut c_recent = vec![None; n];
    let turn_w = usize_window(config.turnover_sessions)?;
    let ca_w = usize_window(config.ca_exclusion_sessions)?;
    if ca_w == 0 {
        return Err(FnoError::Value("window_size 0 must be positive".to_owned()));
    }
    for g in &groups {
        let med = cols::rolling(&c_turn[g.clone()], cols::Roll::Median, turn_w)?;
        for (k, i) in g.clone().enumerate() {
            c_turn20[i] = med[k];
            // `rolling_max(ca_exclusion_sessions, min_samples=1)` of the flag as Int8.
            let lo = i.saturating_sub(ca_w - 1).max(g.start);
            c_recent[i] = Some(c_ca[lo..=i].iter().any(|f| *f));
        }
    }
    let [lo_, lh, ll, lc, ret, atr, rv] = c_lv;
    let ca: Vec<Option<bool>> = c_ca.iter().map(|f| Some(*f)).collect();
    Ok(DataFrame::new(
        n,
        vec![
            cols::date_column("trade_date", &c_trade)?,
            cols::string_column("symbol", &c_symbol),
            cols::string_column("instrument", &c_instrument),
            cols::date_column("held_expiry", &c_expiry)?,
            cols::float_column("held_settle", &c_settle),
            cols::float_column("level_o", &lo_),
            cols::float_column("level_h", &lh),
            cols::float_column("level_l", &ll),
            cols::float_column("level_c", &lc),
            cols::float_column("ret", &ret),
            cols::float_column("atr14", &atr),
            cols::float_column("rv20", &rv),
            cols::int_column("oi_total", &c_oi),
            cols::float_column("fut_turnover", &c_turn),
            cols::float_column("fut_turnover_20d", &c_turn20),
            cols::int_column("lot_size", &c_lot),
            cols::float_column("spot", &c_spot),
            cols::float_column("basis_ann", &c_basis),
            cols::bool_column("ca_flag", &ca),
            cols::bool_column("ca_recent", &c_recent),
        ],
    )?)
}

/// The dtype `continuous_futures` gives a column (for callers building an empty frame).
#[must_use]
pub fn output_dtype(column: &str) -> DataType {
    match column {
        "trade_date" | "held_expiry" => DataType::Date,
        "symbol" | "instrument" => DataType::String,
        "oi_total" | "lot_size" => DataType::Int64,
        "ca_flag" | "ca_recent" => DataType::Boolean,
        _ => DataType::Float64,
    }
}
