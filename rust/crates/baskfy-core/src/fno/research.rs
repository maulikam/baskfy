//! Port of `baskfy_core.fno.research` (`decile-blueprint/packages/core/src/baskfy_core/fno/research.py`).
//!
//! The `RESEARCH.md` families as pure functions of polars frames (`04` §6; FO1, FO9): a
//! faithful port of the research scripts — `cont.py` ([`continuous`]), `res_options.py`
//! ([`build_option_panel`], [`condor_trades`]), `res_futures.py`/`res_f2.py`
//! ([`futures_panel`], [`futures_signals`], [`simulate_futures`]), `res_debit.py`
//! ([`debit_trades`]) and `res_basis.py` ([`basis_study`]), quirks included.
//!
//! Python writes most of this as polars expressions. The port walks the same steps over plain
//! rows and calls **polars' own kernels** where a float depends on the summation order
//! (`rolling_mean`/`rolling_std`/`rolling_median` per symbol, `mean`, `std`, `median`,
//! `quantile`), so the floats are the ones polars computes. Python's built-in `sum()` over
//! floats is CPython 3.12's compensated (Neumaier) sum (DECISIONS-RS R2.2). numpy's `exp`/`log`
//! and `math.erf` are the platform's libm in Python; Rust uses its own `exp`/`ln` (the same
//! libm on macOS) and the fdlibm `erf` already ported in `fno::vol` (1-2 ulp at most).
//!
//! Goldens: `L1/fno/research.*.case_NNN.json`.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::{Datelike, NaiveDate};
use polars::prelude::DataFrame;
use serde::{Deserialize, Serialize};

use super::config::{FnoError, FnoResult};
use super::series::cols;

/// `res_options.R_RATE`: the research discounted its Black-76 at 6.5 %.
///
/// Ports `baskfy_core.fno.research.RESEARCH_RATE`.
pub const RESEARCH_RATE: f64 = 0.065;
/// `res_futures.COST`/`res_f2.COST`: 0.12 % of notional per futures round trip.
///
/// Ports `baskfy_core.fno.research.FUTURES_ROUND_TRIP_COST`.
pub const FUTURES_ROUND_TRIP_COST: f64 = 0.0012;
/// The two index underlyings, sorted.
///
/// Ports `baskfy_core.fno.research.INDEX_UNDERLYINGS`.
pub const INDEX_UNDERLYINGS: [&str; 2] = ["BANKNIFTY", "NIFTY"];

/// `symbol in INDEX_UNDERLYINGS`.
#[must_use]
pub fn is_index(symbol: &str) -> bool {
    INDEX_UNDERLYINGS.contains(&symbol)
}

// =============================================================================================
// Frame plumbing shared by the research, retest and directional re-test ports
// =============================================================================================

/// Rows-to-frame and frame-to-rows helpers (Python's `pl.DataFrame(rows, schema=[...])`
/// inference, `take`, average ranks, polars' own reductions).
pub mod frames {
    use chrono::NaiveDate;
    use polars::error::PolarsError;
    use polars::prelude::{
        Column, DataFrame, DataType, IdxCa, IdxSize, IntoColumn, NamedFrom, QuantileMethod, Series,
    };

    use super::super::config::{FnoError, FnoResult};
    use super::super::series::cols;

    /// One cell of a row-built frame.
    #[derive(Debug, Clone, PartialEq)]
    pub enum Cell {
        /// `None`.
        Null,
        /// A `str`.
        Str(String),
        /// A `date`.
        Date(NaiveDate),
        /// An `int`.
        Int(i64),
        /// A `float`.
        Float(f64),
    }

    impl From<Option<NaiveDate>> for Cell {
        fn from(v: Option<NaiveDate>) -> Self {
            v.map_or(Self::Null, Self::Date)
        }
    }

    impl From<Option<f64>> for Cell {
        fn from(v: Option<f64>) -> Self {
            v.map_or(Self::Null, Self::Float)
        }
    }

    /// `pl.DataFrame(rows, schema=names, orient="row")`: each column's dtype inferred from its
    /// values — `Null` when every value is `None`, `Float64` when any is a float (ints beside
    /// floats become floats), else `Int64`, `String` or `Date`.
    ///
    /// # Errors
    /// A column mixing strings or dates with other kinds (Python refuses it too), or one null
    /// in its first 100 rows and set after them (polars' `ComputeError`).
    pub fn from_rows(names: &[&str], rows: &[Vec<Cell>]) -> FnoResult<DataFrame> {
        let n = rows.len();
        let mut columns = Vec::with_capacity(names.len());
        for (k, name) in names.iter().enumerate() {
            let cells: Vec<&Cell> = rows.iter().filter_map(|r| r.get(k)).collect();
            if cells.len() != n {
                return Err(FnoError::Value(format!("row too short for column {name}")));
            }
            // polars infers a row-built column's dtype from its first 100 rows: a column null
            // there and set later cannot be built (`ComputeError`), so the frame is refused.
            if n > INFER_SCHEMA_LENGTH
                && cells[..INFER_SCHEMA_LENGTH]
                    .iter()
                    .all(|c| matches!(c, Cell::Null))
                && let Some(late) = cells.iter().find(|c| !matches!(c, Cell::Null))
            {
                return Err(PolarsError::ComputeError(
                    format!(
                        "could not append value: {late:?} to the builder; make sure that all rows have the same schema or consider increasing `infer_schema_length`"
                    )
                    .into(),
                )
                .into());
            }
            let has = |f: fn(&Cell) -> bool| cells.iter().any(|c| f(c));
            let column = if has(|c| matches!(c, Cell::Str(_))) {
                let v: FnoResult<Vec<Option<String>>> = cells
                    .iter()
                    .map(|c| match c {
                        Cell::Null => Ok(None),
                        Cell::Str(s) => Ok(Some(s.clone())),
                        _ => Err(mixed(name)),
                    })
                    .collect();
                cols::string_column(name, &v?)
            } else if has(|c| matches!(c, Cell::Date(_))) {
                let v: FnoResult<Vec<Option<NaiveDate>>> = cells
                    .iter()
                    .map(|c| match c {
                        Cell::Null => Ok(None),
                        Cell::Date(d) => Ok(Some(*d)),
                        _ => Err(mixed(name)),
                    })
                    .collect();
                cols::date_column(name, &v?)?
            } else if has(|c| matches!(c, Cell::Float(_))) {
                #[allow(clippy::cast_precision_loss)]
                let v: Vec<Option<f64>> = cells
                    .iter()
                    .map(|c| match c {
                        Cell::Float(f) => Some(*f),
                        Cell::Int(i) => Some(*i as f64),
                        _ => None,
                    })
                    .collect();
                cols::float_column(name, &v)
            } else if has(|c| matches!(c, Cell::Int(_))) {
                let v: Vec<Option<i64>> = cells
                    .iter()
                    .map(|c| match c {
                        Cell::Int(i) => Some(*i),
                        _ => None,
                    })
                    .collect();
                cols::int_column(name, &v)
            } else {
                Series::new_null((*name).into(), n).into_column()
            };
            columns.push(column);
        }
        Ok(DataFrame::new(n, columns)?)
    }

    /// polars' `infer_schema_length` default for row data.
    const INFER_SCHEMA_LENGTH: usize = 100;

    fn mixed(name: &str) -> FnoError {
        FnoError::Type(format!("unexpected value type in column {name}"))
    }

    /// A `UInt32` column.
    #[must_use]
    pub fn u32_column(name: &str, values: &[Option<u32>]) -> Column {
        Series::new(name.into(), values.to_vec()).into_column()
    }

    /// An `Int32` column.
    #[must_use]
    pub fn i32_column(name: &str, values: &[Option<i32>]) -> Column {
        Series::new(name.into(), values.to_vec()).into_column()
    }

    /// The rows of `df` at `indices`, in that order (`df[indices]`).
    ///
    /// # Errors
    /// An index out of range.
    pub fn take(df: &DataFrame, indices: &[usize]) -> FnoResult<DataFrame> {
        let idx: Vec<IdxSize> = indices
            .iter()
            .map(|i| IdxSize::try_from(*i).map_err(|e| FnoError::Value(e.to_string())))
            .collect::<FnoResult<_>>()?;
        Ok(df.take(&IdxCa::from_vec("".into(), idx))?)
    }

    /// `df.with_columns(columns)`: each appended (or replaced in place) in order.
    ///
    /// # Errors
    /// A column of the wrong length.
    pub fn with_columns(mut df: DataFrame, columns: Vec<Column>) -> FnoResult<DataFrame> {
        for c in columns {
            df.with_column(c)?;
        }
        Ok(df)
    }

    /// polars' `rank(method="average")` of one group: 1-based, ties averaged, nulls null.
    #[must_use]
    pub fn rank_average(values: &[Option<f64>], descending: bool) -> Vec<Option<f64>> {
        let present: Vec<(usize, f64)> = values
            .iter()
            .enumerate()
            .filter_map(|(i, v)| v.map(|x| (i, x)))
            .collect();
        // polars' total order: every NaN equal and largest, -0.0 equal to 0.0.
        let canon = |x: f64| {
            if x.is_nan() {
                f64::NAN
            } else if x == 0.0 {
                0.0
            } else {
                x
            }
        };
        let mut present: Vec<(usize, f64)> =
            present.into_iter().map(|(i, x)| (i, canon(x))).collect();
        present.sort_by(|a, b| {
            let o = a.1.total_cmp(&b.1);
            if descending { o.reverse() } else { o }
        });
        let mut out = vec![None; values.len()];
        let mut start = 0;
        while start < present.len() {
            let mut end = start + 1;
            while end < present.len() && present[end].1.total_cmp(&present[start].1).is_eq() {
                end += 1;
            }
            // Ranks start..end (0-based) averaged, 1-based: (start + 1 + end) / 2.
            #[allow(clippy::cast_precision_loss)]
            let rank = (start + 1 + end) as f64 / 2.0;
            for item in &present[start..end] {
                out[item.0] = Some(rank);
            }
            start = end;
        }
        out
    }

    fn series(values: &[Option<f64>]) -> Series {
        Series::new("".into(), values.to_vec())
    }

    /// `Series.sum()` of a float column: polars' own (pairwise, blocked) float sum.
    ///
    /// # Errors
    /// A polars error.
    pub fn sum(values: &[Option<f64>]) -> FnoResult<f64> {
        Ok(series(values).sum::<f64>()?)
    }

    /// `Series.mean()`.
    #[must_use]
    pub fn mean(values: &[Option<f64>]) -> Option<f64> {
        series(values).mean()
    }

    /// `Series.std(ddof=1)`.
    #[must_use]
    pub fn std(values: &[Option<f64>]) -> Option<f64> {
        series(values).std(1)
    }

    /// `Series.median()`.
    #[must_use]
    pub fn median(values: &[Option<f64>]) -> Option<f64> {
        series(values).median()
    }

    /// `Series.quantile(q)` with polars' default `nearest` interpolation.
    ///
    /// # Errors
    /// A polars error.
    pub fn quantile_nearest(values: &[Option<f64>], q: f64) -> FnoResult<Option<f64>> {
        let scalar = series(values).quantile_reduce(q, QuantileMethod::Nearest)?;
        let v = scalar.into_series("".into()).cast(&DataType::Float64)?;
        Ok(v.f64()?.get(0))
    }

    /// `Series.mean()` of a boolean column (nulls excluded).
    #[must_use]
    pub fn bool_mean(values: &[Option<bool>]) -> Option<f64> {
        Series::new("".into(), values.to_vec()).mean()
    }

    /// The session column's name: `trade_date` (the table) or `date` (the day files).
    #[must_use]
    pub fn session_column(df: &DataFrame) -> &'static str {
        if df
            .get_column_names()
            .iter()
            .any(|c| c.as_str() == "trade_date")
        {
            "trade_date"
        } else {
            "date"
        }
    }

    /// `df.rename({"trade_date": "date"})` when the table's name is used.
    ///
    /// # Errors
    /// A polars error.
    pub fn renamed(df: &DataFrame) -> FnoResult<DataFrame> {
        let mut out = df.clone();
        if session_column(df) == "trade_date" {
            out.rename("trade_date", "date".into())?;
        }
        Ok(out)
    }

    /// A column as `u32` (`UInt32` dtype, read through `Int64`).
    ///
    /// # Errors
    /// A missing column, or a value out of `u32`'s range.
    pub fn u32s(df: &DataFrame, name: &str) -> FnoResult<Vec<Option<u32>>> {
        cols::ints(df, name)?
            .into_iter()
            .map(|v| {
                v.map(|x| u32::try_from(x).map_err(|e| FnoError::Value(e.to_string())))
                    .transpose()
            })
            .collect()
    }

    /// Contiguous runs of equal keys (a frame sorted by its group key).
    #[must_use]
    pub fn runs<K: PartialEq>(keys: &[K]) -> Vec<std::ops::Range<usize>> {
        let mut out = Vec::new();
        let mut start = 0;
        for i in 1..=keys.len() {
            if i == keys.len() || keys[i] != keys[start] {
                if i > start {
                    out.push(start..i);
                }
                start = i;
            }
        }
        out
    }
}

use frames::Cell;

/// `float / float` as Python divides: `ZeroDivisionError` on a zero divisor.
fn fdiv(a: f64, b: f64) -> FnoResult<f64> {
    if b == 0.0 {
        return Err(FnoError::Value("float division by zero".to_owned()));
    }
    Ok(a / b)
}

/// `float(x)` of a polars reduction, or Python's `TypeError` for a null.
fn as_float(value: Option<f64>) -> FnoResult<f64> {
    value.ok_or_else(|| FnoError::Type("expected a number, got NoneType".to_owned()))
}

/// CPython 3.12's `sum()` over floats: Neumaier-compensated.
#[must_use]
pub(crate) fn py_sum(values: &[f64]) -> f64 {
    let mut total = 0.0f64;
    let mut c = 0.0f64;
    for x in values {
        let t = total + x;
        if total.abs() >= x.abs() {
            c += (total - t) + x;
        } else {
            c += (x - t) + total;
        }
        total = t;
    }
    if c != 0.0 && c.is_finite() {
        total += c;
    }
    total
}

/// Python's `max(a, b)`: the first unless the second is strictly greater.
fn pmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's `min(a, b)`: the first unless the second is strictly smaller.
fn pmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

fn days_f64(a: NaiveDate, b: NaiveDate) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let d = (a - b).num_days() as f64;
    d
}

/// `max((e - d).days, 0.5) / 365.0`.
fn years_floor(e: NaiveDate, d: NaiveDate) -> f64 {
    pmax(days_f64(e, d), 0.5) / 365.0
}

// =============================================================================================
// cont.py
// =============================================================================================

/// The columns of [`continuous`]'s output, in order.
pub const CONT_COLUMNS: [&str; 14] = [
    "symbol",
    "instrument",
    "date",
    "di",
    "expiry",
    "o",
    "h",
    "l",
    "c",
    "r",
    "oi_total",
    "fut_turnover",
    "lot_size",
    "spot",
];

#[derive(Debug, Clone)]
struct FutRow {
    symbol: Option<String>,
    instrument: Option<String>,
    date: NaiveDate,
    di: u32,
    expiry: Option<NaiveDate>,
    open: Option<f64>,
    high: Option<f64>,
    low: Option<f64>,
    settle: f64,
    underlying: Option<f64>,
    oi: Option<i64>,
    turnover: Option<f64>,
    lot: Option<i64>,
}

#[derive(Debug, Clone, Default)]
struct OiAgg {
    oi_total: i64,
    turnover: Vec<Option<f64>>,
    lot_size: Option<i64>,
    spot: Option<f64>,
    instrument: Option<Option<String>>,
}

/// `cont.py`: the continuous, tradeable front-month series per symbol (no look-ahead).
///
/// Ports `baskfy_core.fno.research.continuous`.
///
/// # Errors
/// A missing column or a polars error.
#[allow(clippy::too_many_lines)]
pub fn continuous(futures: &DataFrame) -> FnoResult<DataFrame> {
    let date = cols::dates(futures, "date")?;
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
    let keep: Vec<usize> = (0..futures.height())
        .filter(|&i| pl_positive(settle[i]))
        .collect();
    // `dates`: the unique sessions, sorted (a null first), numbered from 0.
    let unique: BTreeSet<Option<NaiveDate>> = keep.iter().map(|&i| date[i]).collect();
    let di_of: HashMap<NaiveDate, u32> = unique
        .iter()
        .enumerate()
        .filter_map(|(k, d)| d.map(|d| (d, u32::try_from(k).unwrap_or(u32::MAX))))
        .collect();
    let rows: Vec<FutRow> = keep
        .iter()
        .filter_map(|&i| {
            let d = date[i]?;
            Some(FutRow {
                symbol: symbol[i].clone(),
                instrument: instrument[i].clone(),
                date: d,
                di: *di_of.get(&d)?,
                expiry: expiry[i],
                open: open[i],
                high: high[i],
                low: low[i],
                settle: settle[i].unwrap_or(0.0),
                underlying: underlying[i],
                oi: oi[i],
                turnover: turnover[i],
                lot: lot[i],
            })
        })
        .collect();
    // `oi`: per (symbol, date).
    let mut oi_map: HashMap<(Option<String>, NaiveDate), OiAgg> = HashMap::new();
    for r in &rows {
        let a = oi_map.entry((r.symbol.clone(), r.date)).or_default();
        a.oi_total += r.oi.unwrap_or(0);
        a.turnover.push(r.turnover);
        a.lot_size = match (a.lot_size, r.lot) {
            (Some(x), Some(y)) => Some(x.max(y)),
            (x, y) => x.or(y),
        };
        // polars' `max` skips NaN (NaN only when every value is NaN).
        a.spot = match (a.spot, r.underlying) {
            (Some(x), Some(y)) => Some(if x.is_nan() || y > x { y } else { x }),
            (x, y) => x.or(y),
        };
        if a.instrument.is_none() {
            a.instrument = Some(r.instrument.clone());
        }
    }
    // `front`: per (symbol, di), the nearest expiry strictly after the session.
    let mut front: BTreeMap<(Option<String>, u32), (NaiveDate, f64)> = BTreeMap::new();
    for r in &rows {
        let Some(e) = r.expiry else { continue };
        if e <= r.date {
            continue;
        }
        let entry = front
            .entry((r.symbol.clone(), r.di))
            .or_insert((e, r.settle));
        if e < entry.0 {
            *entry = (e, r.settle);
        }
    }
    let mut by_contract: HashMap<(String, u32, NaiveDate), Vec<usize>> = HashMap::new();
    for (k, r) in rows.iter().enumerate() {
        if let (Some(s), Some(e)) = (&r.symbol, r.expiry) {
            by_contract.entry((s.clone(), r.di, e)).or_default().push(k);
        }
    }
    struct Next {
        symbol: String,
        date: NaiveDate,
        di: u32,
        gc: f64,
        go: f64,
        gh: f64,
        gl: f64,
        expiry: NaiveDate,
    }
    let mut nxt: Vec<Next> = Vec::new();
    for ((sym, di), (held_expiry, held_settle)) in &front {
        let Some(sym) = sym else { continue };
        let Some(di_next) = di.checked_add(1) else {
            continue;
        };
        let Some(matches) = by_contract.get(&(sym.clone(), di_next, *held_expiry)) else {
            continue;
        };
        for &k in matches {
            let r = &rows[k];
            let gc = r.settle / held_settle;
            if !(gc > 0.7 && gc < 1.4) {
                continue;
            }
            let ratio = |v: Option<f64>| v.map(|x| x / held_settle);
            let go = match ratio(r.open) {
                Some(x) if x > 0.0 || x.is_nan() => x,
                _ => 1.0,
            };
            let gh = match ratio(r.high) {
                Some(x) if x > 0.0 || x.is_nan() => x,
                _ => pmax_h(gc, 1.0),
            };
            let gl = match ratio(r.low) {
                Some(x) if x > 0.0 || x.is_nan() => x,
                _ => pmin_h(gc, 1.0),
            };
            nxt.push(Next {
                symbol: sym.clone(),
                date: r.date,
                di: di_next,
                gc,
                go,
                gh,
                gl,
                expiry: *held_expiry,
            });
        }
    }
    nxt.sort_by(|a, b| (&a.symbol, a.di).cmp(&(&b.symbol, b.di)));
    let n = nxt.len();
    let mut c_l = vec![0.0; n];
    for g in frames::runs(&nxt.iter().map(|x| x.symbol.as_str()).collect::<Vec<_>>()) {
        let mut acc = 0.0f64;
        for i in g {
            acc += nxt[i].gc.ln();
            c_l[i] = acc.exp();
        }
    }
    let mut out_rows: Vec<Vec<Cell>> = Vec::with_capacity(n);
    let mut di_col = Vec::with_capacity(n);
    let mut oi_col = Vec::with_capacity(n);
    let mut lot_col = Vec::with_capacity(n);
    let mut inst_col = Vec::with_capacity(n);
    let mut turn_col = Vec::with_capacity(n);
    let mut spot_col = Vec::with_capacity(n);
    for (k, x) in nxt.iter().enumerate() {
        let big_l = c_l[k];
        let lprev = big_l / x.gc;
        let agg = oi_map.get(&(Some(x.symbol.clone()), x.date));
        inst_col.push(agg.and_then(|a| a.instrument.clone().flatten()));
        oi_col.push(agg.map(|a| a.oi_total));
        turn_col.push(match agg {
            Some(a) => Some(frames::sum(&a.turnover)?),
            None => None,
        });
        lot_col.push(agg.and_then(|a| a.lot_size));
        spot_col.push(agg.and_then(|a| a.spot));
        di_col.push(Some(x.di));
        out_rows.push(vec![
            Cell::Float(lprev * x.go),
            Cell::Float(lprev * x.gh),
            Cell::Float(lprev * x.gl),
            Cell::Float(big_l),
            Cell::Float(x.gc.ln()),
        ]);
    }
    let pick = |j: usize| -> Vec<Option<f64>> {
        out_rows
            .iter()
            .map(|r| match r.get(j) {
                Some(Cell::Float(f)) => Some(*f),
                _ => None,
            })
            .collect()
    };
    let syms: Vec<Option<String>> = nxt.iter().map(|x| Some(x.symbol.clone())).collect();
    let dates: Vec<Option<NaiveDate>> = nxt.iter().map(|x| Some(x.date)).collect();
    let exps: Vec<Option<NaiveDate>> = nxt.iter().map(|x| Some(x.expiry)).collect();
    Ok(DataFrame::new(
        n,
        vec![
            cols::string_column("symbol", &syms),
            cols::string_column("instrument", &inst_col),
            cols::date_column("date", &dates)?,
            frames::u32_column("di", &di_col),
            cols::date_column("expiry", &exps)?,
            cols::float_column("o", &pick(0)),
            cols::float_column("h", &pick(1)),
            cols::float_column("l", &pick(2)),
            cols::float_column("c", &pick(3)),
            cols::float_column("r", &pick(4)),
            cols::int_column("oi_total", &oi_col),
            cols::float_column("fut_turnover", &turn_col),
            cols::int_column("lot_size", &lot_col),
            cols::float_column("spot", &spot_col),
        ],
    )?)
}

/// `pl.max_horizontal(a, b)` of two non-null floats.
fn pmax_h(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// `pl.min_horizontal(a, b)` of two non-null floats.
fn pmin_h(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

// =============================================================================================
// res_options.py — Black-76 as the research priced it
// =============================================================================================

fn ncdf(x: f64) -> f64 {
    0.5 * (1.0 + super::vol::greeks::erf(x / 2.0_f64.sqrt()))
}

/// numpy's `np.maximum(a, b)`: NaN propagates.
fn np_maximum(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if b > a {
        b
    } else {
        a
    }
}

/// One element of `res_options.b76`.
fn b76_one(forward: f64, strike: f64, years: f64, vol: f64, kind: &str, rate: f64) -> f64 {
    let vol = np_maximum(vol, 1e-4);
    let years = np_maximum(years, 1e-6);
    let root = vol * years.sqrt();
    let d1 = ((forward / strike).ln() + 0.5 * vol * vol * years) / root;
    let d2 = d1 - root;
    let df = (-rate * 0.0).exp() * (-rate * years).exp();
    if kind == "CE" {
        df * (forward * ncdf(d1) - strike * ncdf(d2))
    } else {
        df * (strike * ncdf(-d2) - forward * ncdf(-d1))
    }
}

fn same_length(lengths: &[usize]) -> FnoResult<usize> {
    let n = lengths.first().copied().unwrap_or(0);
    if lengths.iter().any(|l| *l != n) {
        return Err(FnoError::Value(
            "operands could not be broadcast together".to_owned(),
        ));
    }
    Ok(n)
}

/// `res_options.b76`, vectorised exactly as the script ran it (`kind == "CE"` a call, anything
/// else a put).
///
/// Ports `baskfy_core.fno.research.b76`.
///
/// # Errors
/// Arrays of different lengths.
pub fn b76(
    forward: &[f64],
    strike: &[f64],
    years: &[f64],
    vol: &[f64],
    kind: &[String],
    rate: f64,
) -> FnoResult<Vec<f64>> {
    let n = same_length(&[
        forward.len(),
        strike.len(),
        years.len(),
        vol.len(),
        kind.len(),
    ])?;
    Ok((0..n)
        .map(|i| b76_one(forward[i], strike[i], years[i], vol[i], &kind[i], rate))
        .collect())
}

/// `res_options.implied`: 50 bisections on `[0.01, 3.0]`.
///
/// Ports `baskfy_core.fno.research.implied`.
///
/// # Errors
/// Arrays of different lengths.
pub fn implied(
    forward: &[f64],
    strike: &[f64],
    years: &[f64],
    price: &[f64],
    kind: &[String],
    rate: f64,
) -> FnoResult<Vec<f64>> {
    let n = same_length(&[
        forward.len(),
        strike.len(),
        years.len(),
        price.len(),
        kind.len(),
    ])?;
    Ok((0..n)
        .map(|i| implied_one(forward[i], strike[i], years[i], price[i], &kind[i], rate))
        .collect())
}

fn implied_one(forward: f64, strike: f64, years: f64, price: f64, kind: &str, rate: f64) -> f64 {
    let (mut lo, mut hi) = (0.01_f64, 3.0_f64);
    for _ in 0..50 {
        let mid = 0.5 * (lo + hi);
        let p = b76_one(forward, strike, years, mid, kind, rate);
        if p > price {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    0.5 * (lo + hi)
}

// =============================================================================================
// Parameters
// =============================================================================================

/// The condor family's mode.
///
/// Ports `baskfy_core.fno.research.CondorMode`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CondorMode {
    /// An iron condor.
    #[default]
    Condor,
    /// One credit spread on the trend's side.
    TrendCredit,
}

impl CondorMode {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Condor => "condor",
            Self::TrendCredit => "trend_credit",
        }
    }
}

/// `condor_trades`'s arguments plus the script's env knobs.
///
/// Ports `baskfy_core.fno.research.CondorParams`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct CondorParams {
    /// Sessions before expiry to enter.
    pub n_before: i64,
    /// The short strikes' distance, in sigma.
    pub k_short: f64,
    /// The wings' distance beyond the shorts, in sigma.
    pub k_wing: f64,
    /// Take profit at this share of the credit.
    pub profit_take: f64,
    /// Exit at the close of `E - exit_n`.
    pub exit_n: i64,
    /// Slippage per leg per crossing, share of premium.
    pub slip_pct: f64,
    /// Slippage floor per leg per crossing, ₹.
    pub slip_min: f64,
    /// Liquidity rank cut (0: none).
    pub top_n: i64,
    /// Condor or trend credit.
    pub mode: CondorMode,
    /// Close when the future crosses a short strike.
    pub defend: bool,
    /// Loss close at `(1 + loss_mult) x credit` (0: none).
    pub loss_mult: f64,
    /// Only these symbols (sorted), or every one.
    pub symbols: Option<BTreeSet<String>>,
}

impl Default for CondorParams {
    fn default() -> Self {
        Self {
            n_before: 15,
            k_short: 1.0,
            k_wing: 0.5,
            profit_take: 0.5,
            exit_n: 1,
            slip_pct: 0.03,
            slip_min: 0.05,
            top_n: 0,
            mode: CondorMode::Condor,
            defend: false,
            loss_mult: 0.0,
            symbols: None,
        }
    }
}

fn index_set() -> BTreeSet<String> {
    INDEX_UNDERLYINGS.iter().map(|s| (*s).to_owned()).collect()
}

/// B4 as `RESEARCH.md` reports it: NIFTY + BANKNIFTY monthlies, N = 15, 0.5 % slippage.
///
/// Ports `baskfy_core.fno.research.B4`.
#[must_use]
pub fn b4() -> CondorParams {
    CondorParams {
        n_before: 15,
        slip_pct: 0.005,
        symbols: Some(index_set()),
        ..CondorParams::default()
    }
}

/// B4 with Maulik's loss close (M.1).
///
/// Ports `baskfy_core.fno.research.B4_LOSS_CLOSE`.
#[must_use]
pub fn b4_loss_close() -> CondorParams {
    CondorParams {
        loss_mult: 1.5,
        ..b4()
    }
}

/// `res_options.leg_costs`: slippage + STT on sells + GST on brokerage and exchange.
///
/// Ports `baskfy_core.fno.research.leg_costs`. A zero `lot` gives an infinite brokerage where
/// Python raises `ZeroDivisionError` (no caller passes one: lots fall back to 500).
#[must_use]
pub fn leg_costs(price: f64, sell: bool, lot: f64, params: &CondorParams) -> f64 {
    let slip = pmax(params.slip_min, params.slip_pct * price);
    let brokerage = 20.0 / lot;
    let stt = if sell { 0.0015 * price } else { 0.0 };
    let exch = 0.000355 * price;
    slip + stt + 1.18 * (brokerage + exch)
}

// =============================================================================================
// Summaries
// =============================================================================================

/// `n`, expectancy in R, its t-statistic, the win rate, the worst trade, and by year.
///
/// Ports `baskfy_core.fno.research.Summary`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Summary {
    /// Trades.
    pub n: i64,
    /// Mean R.
    pub exp_r: f64,
    /// Its t-statistic.
    pub t_stat: f64,
    /// Share of trades with R > 0.
    pub win: f64,
    /// The worst R.
    pub worst: f64,
    /// Mean of R + cost_R (option families).
    pub gross_r: Option<f64>,
    /// Mean cost_R (option families).
    pub cost_r: Option<f64>,
    /// Year -> (n, mean R).
    pub per_year: BTreeMap<i32, (i64, f64)>,
}

impl Summary {
    /// The empty summary (`Summary(0, 0.0, 0.0, 0.0, 0.0, None, None, {})`).
    #[must_use]
    pub fn empty() -> Self {
        Self {
            n: 0,
            exp_r: 0.0,
            t_stat: 0.0,
            win: 0.0,
            worst: 0.0,
            gross_r: None,
            cost_r: None,
            per_year: BTreeMap::new(),
        }
    }
}

fn has_column(df: &DataFrame, name: &str) -> bool {
    df.get_column_names().iter().any(|c| c.as_str() == name)
}

/// The scripts' `summarise`/`report` headline numbers.
///
/// Ports `baskfy_core.fno.research.summarise`.
///
/// # Errors
/// Python's `TypeError` when a reduction is null — notably the standard deviation of a single
/// trade (`_as_float(None)`; R2.9, `known_bug`, reproduced) — or a missing column.
pub fn summarise(trades: &DataFrame, r_column: &str, date_column: &str) -> FnoResult<Summary> {
    if trades.height() == 0 {
        return Ok(Summary::empty());
    }
    let r = cols::floats(trades, r_column)?;
    let mean = as_float(frames::mean(&r))?;
    let std = as_float(frames::std(&r))?;
    #[allow(clippy::cast_precision_loss)]
    let height = trades.height() as f64;
    let t_stat = if std > 0.0 {
        mean / (std / height.sqrt())
    } else {
        0.0
    };
    let wins: Vec<Option<bool>> = r.iter().map(|v| v.map(|x| x > 0.0)).collect();
    let win = as_float(frames::bool_mean(&wins))?;
    let worst = as_float(
        r.iter()
            .flatten()
            .copied()
            .reduce(|a, b| if b < a { b } else { a }),
    )?;
    let (mut gross, mut cost) = (None, None);
    if has_column(trades, "cost_R") {
        let c = cols::floats(trades, "cost_R")?;
        let sums: Vec<Option<f64>> = r
            .iter()
            .zip(&c)
            .map(|(a, b)| match (a, b) {
                (Some(a), Some(b)) => Some(a + b),
                _ => None,
            })
            .collect();
        gross = Some(as_float(frames::mean(&sums))?);
        cost = Some(as_float(frames::mean(&c))?);
    }
    let dates = cols::dates(trades, date_column)?;
    let mut years: BTreeMap<Option<i32>, Vec<Option<f64>>> = BTreeMap::new();
    for (d, v) in dates.iter().zip(&r) {
        years.entry(d.map(|d| d.year())).or_default().push(*v);
    }
    let mut per_year = BTreeMap::new();
    for (y, vals) in years {
        let Some(y) = y else {
            return Err(FnoError::Type(
                "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"
                    .to_owned(),
            ));
        };
        let exp = frames::mean(&vals).ok_or_else(|| {
            FnoError::Type(
                "float() argument must be a string or a real number, not 'NoneType'".to_owned(),
            )
        })?;
        per_year.insert(y, (i64::try_from(vals.len()).unwrap_or(i64::MAX), exp));
    }
    Ok(Summary {
        n: i64::try_from(trades.height()).unwrap_or(i64::MAX),
        exp_r: mean,
        t_stat,
        win,
        worst,
        gross_r: gross,
        cost_r: cost,
        per_year,
    })
}

// =============================================================================================
// res_futures.py / res_f2.py
// =============================================================================================

fn sort_permutation<K: Ord>(keys: &[K]) -> Vec<usize> {
    let mut idx: Vec<usize> = (0..keys.len()).collect();
    idx.sort_by(|a, b| keys[*a].cmp(&keys[*b]));
    idx
}

fn shifted<T: Copy>(values: &[Option<T>], k: usize) -> Vec<Option<T>> {
    (0..values.len())
        .map(|i| if i >= k { values[i - k] } else { None })
        .collect()
}

/// `s.rolling_max(w)` / `rolling_min(w)` with `min_samples = w`.
fn rolling_extreme(values: &[Option<f64>], w: usize, max: bool) -> Vec<Option<f64>> {
    (0..values.len())
        .map(|i| {
            if i + 1 < w {
                return None;
            }
            let window = &values[i + 1 - w..=i];
            let mut acc: Option<f64> = None;
            for v in window {
                let x = (*v)?;
                acc = Some(match acc {
                    None => x,
                    Some(a) if (max && x > a) || (!max && x < a) => x,
                    Some(a) => a,
                });
            }
            acc
        })
        .collect()
}

fn ratio_minus_one(a: Option<f64>, b: Option<f64>) -> Option<f64> {
    Some(a? / b? - 1.0)
}

#[allow(clippy::cast_precision_loss)]
fn ratio_minus_one_int(a: Option<i64>, b: Option<i64>) -> Option<f64> {
    Some(a? as f64 / b? as f64 - 1.0)
}

/// NIFTY's `c > c.rolling_mean(50)` by `key` (`di` or `date`).
fn nifty_up_by<K: Ord + Copy>(
    symbols: &[Option<String>],
    keys: &[Option<K>],
    sort_key: &[Option<u32>],
    c: &[Option<f64>],
) -> FnoResult<BTreeMap<K, Option<bool>>> {
    let mut idx: Vec<usize> = (0..symbols.len())
        .filter(|&i| symbols[i].as_deref() == Some("NIFTY"))
        .collect();
    idx.sort_by_key(|&i| sort_key[i]);
    let closes: Vec<Option<f64>> = idx.iter().map(|&i| c[i]).collect();
    let ma = cols::rolling(&closes, cols::Roll::Mean, 50)?;
    let mut out = BTreeMap::new();
    for (k, &i) in idx.iter().enumerate() {
        let up = match (closes[k], ma[k]) {
            (Some(a), Some(b)) => Some(a > b),
            _ => None,
        };
        if let Some(key) = keys[i] {
            // A dict built from rows: the last row of a key wins.
            out.insert(key, up);
        }
    }
    Ok(out)
}

/// The indicator frame both futures scripts build over `cont.parquet`.
///
/// Ports `baskfy_core.fno.research.futures_panel`.
///
/// # Errors
/// A missing column or a polars error.
#[allow(clippy::too_many_lines)]
pub fn futures_panel(cont: &DataFrame) -> FnoResult<DataFrame> {
    let sym0 = cols::strings(cont, "symbol")?;
    let di0 = frames::u32s(cont, "di")?;
    let keys: Vec<(Option<String>, Option<u32>)> =
        sym0.iter().cloned().zip(di0.iter().copied()).collect();
    let perm = sort_permutation(&keys);
    let df = frames::take(cont, &perm)?;
    let symbol = cols::strings(&df, "symbol")?;
    let di = frames::u32s(&df, "di")?;
    let h = cols::floats(&df, "h")?;
    let l = cols::floats(&df, "l")?;
    let c = cols::floats(&df, "c")?;
    let oi = cols::ints(&df, "oi_total")?;
    let turn = cols::floats(&df, "fut_turnover")?;
    let n = df.height();
    let mut tr = vec![None; n];
    let mut atr = vec![None; n];
    let mut hi20 = vec![None; n];
    let mut lo20 = vec![None; n];
    let mut ma50 = vec![None; n];
    let mut ma200 = vec![None; n];
    let mut oi5 = vec![None; n];
    let mut ret1 = vec![None; n];
    let mut oi1 = vec![None; n];
    let mut mom63 = vec![None; n];
    let mut turn20 = vec![None; n];
    for g in frames::runs(&symbol) {
        let cg: Vec<Option<f64>> = c[g.clone()].to_vec();
        let og: Vec<Option<i64>> = oi[g.clone()].to_vec();
        let c1 = shifted(&cg, 1);
        let c5 = shifted(&cg, 5);
        let c68 = shifted(&cg, 68);
        let o1 = shifted(&og, 1);
        let o5 = shifted(&og, 5);
        let mut trg = Vec::with_capacity(g.len());
        for (k, i) in g.clone().enumerate() {
            let a = match (h[i], l[i]) {
                (Some(x), Some(y)) => Some(x - y),
                _ => None,
            };
            let b = match (h[i], c1[k]) {
                (Some(x), Some(y)) => Some((x - y).abs()),
                _ => None,
            };
            let cc = match (l[i], c1[k]) {
                (Some(x), Some(y)) => Some((x - y).abs()),
                _ => None,
            };
            let v = [a, b, cc]
                .into_iter()
                .flatten()
                .reduce(|p, q| if q > p { q } else { p });
            trg.push(v);
        }
        let atrg = cols::rolling(&trg, cols::Roll::Mean, 14)?;
        let hig = rolling_extreme(&c1, 20, true);
        let log = rolling_extreme(&c1, 20, false);
        let ma50g = cols::rolling(&cg, cols::Roll::Mean, 50)?;
        let ma200g = cols::rolling(&cg, cols::Roll::Mean, 200)?;
        let turng = cols::rolling(&turn[g.clone()], cols::Roll::Median, 20)?;
        for (k, i) in g.clone().enumerate() {
            tr[i] = trg[k];
            atr[i] = atrg[k];
            hi20[i] = hig[k];
            lo20[i] = log[k];
            ma50[i] = ma50g[k];
            ma200[i] = ma200g[k];
            oi5[i] = ratio_minus_one_int(og[k], o5[k]);
            ret1[i] = ratio_minus_one(cg[k], c1[k]);
            oi1[i] = ratio_minus_one_int(og[k], o1[k]);
            mom63[i] = ratio_minus_one(c5[k], c68[k]);
            turn20[i] = turng[k];
        }
    }
    let nifty = nifty_up_by(&sym0, &di0, &di0, &cols::floats(cont, "c")?)?;
    let nifty_up: Vec<Option<bool>> = di
        .iter()
        .map(|d| d.and_then(|d| nifty.get(&d).copied().flatten()))
        .collect();
    frames::with_columns(
        df,
        vec![
            cols::float_column("tr", &tr),
            cols::float_column("atr", &atr),
            cols::float_column("hi20", &hi20),
            cols::float_column("lo20", &lo20),
            cols::float_column("ma50", &ma50),
            cols::float_column("ma200", &ma200),
            cols::float_column("oi5", &oi5),
            cols::float_column("ret1", &ret1),
            cols::float_column("oi1", &oi1),
            cols::float_column("mom63", &mom63),
            cols::float_column("turn20", &turn20),
            cols::bool_column("nifty_up", &nifty_up),
        ],
    )
}

/// `RESEARCH.md` §A's signal families.
///
/// Ports `baskfy_core.fno.research.FuturesFamily`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum FuturesFamily {
    /// 20-day breakout + trend + OI rising, both sides.
    A1,
    /// A1 aligned with NIFTY's 50-day regime.
    A1R,
    /// Control: aligned breakout, no OI filter, both sides.
    A0,
    /// A0, long only: F2's signal.
    #[serde(rename = "A0_LONG")]
    A0Long,
    /// One-day long/short buildup.
    A3,
    /// Weekly cross-sectional momentum, top/bottom 10.
    A2,
    /// Index futures trend.
    A4,
}

impl FuturesFamily {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::A1 => "A1",
            Self::A1R => "A1R",
            Self::A0 => "A0",
            Self::A0Long => "A0_LONG",
            Self::A3 => "A3",
            Self::A2 => "A2",
            Self::A4 => "A4",
        }
    }
}

/// The rows of `liquid_base(panel, share)` and its two added columns.
struct Liquid {
    rows: Vec<usize>,
    trank: Vec<Option<f64>>,
    tn: Vec<Option<u32>>,
}

fn liquid_rows(panel: &DataFrame, share: f64) -> FnoResult<Liquid> {
    let instrument = cols::strings(panel, "instrument")?;
    let atr = cols::floats(panel, "atr")?;
    let ma50 = cols::floats(panel, "ma50")?;
    let di = frames::u32s(panel, "di")?;
    let turn20 = cols::floats(panel, "turn20")?;
    let base: Vec<usize> = (0..panel.height())
        .filter(|&i| {
            instrument[i].as_deref() == Some("FUTSTK") && atr[i].is_some() && ma50[i].is_some()
        })
        .collect();
    let mut groups: BTreeMap<Option<u32>, Vec<usize>> = BTreeMap::new();
    for (k, &i) in base.iter().enumerate() {
        groups.entry(di[i]).or_default().push(k);
    }
    let mut trank = vec![None; base.len()];
    let mut tn = vec![None; base.len()];
    for members in groups.values() {
        let vals: Vec<Option<f64>> = members.iter().map(|&k| turn20[base[k]]).collect();
        let ranks = frames::rank_average(&vals, true);
        let count = u32::try_from(vals.iter().flatten().count()).unwrap_or(u32::MAX);
        for (j, &k) in members.iter().enumerate() {
            trank[k] = ranks[j];
            tn[k] = Some(count);
        }
    }
    let mut out = Liquid {
        rows: Vec::new(),
        trank: Vec::new(),
        tn: Vec::new(),
    };
    for (k, &i) in base.iter().enumerate() {
        let keep = match (trank[k], tn[k]) {
            (Some(r), Some(t)) => r <= share * f64::from(t),
            _ => false,
        };
        if keep {
            out.rows.push(i);
            out.trank.push(trank[k]);
            out.tn.push(tn[k]);
        }
    }
    Ok(out)
}

/// `base`: stock futures with ATR and MA50, in the top `share` by 20-day turnover.
///
/// Ports `baskfy_core.fno.research.liquid_base`.
///
/// # Errors
/// A missing column or a polars error.
pub fn liquid_base(panel: &DataFrame, share: f64) -> FnoResult<DataFrame> {
    let liq = liquid_rows(panel, share)?;
    let df = frames::take(panel, &liq.rows)?;
    frames::with_columns(
        df,
        vec![
            cols::float_column("trank", &liq.trank),
            frames::u32_column("tn", &liq.tn),
        ],
    )
}

struct Signal {
    symbol: Option<String>,
    di: Option<u32>,
    side: i32,
}

fn signals_frame(signals: &[Signal]) -> FnoResult<DataFrame> {
    let sym: Vec<Option<String>> = signals.iter().map(|s| s.symbol.clone()).collect();
    let di: Vec<Option<u32>> = signals.iter().map(|s| s.di).collect();
    let side: Vec<Option<i32>> = signals.iter().map(|s| Some(s.side)).collect();
    Ok(DataFrame::new(
        signals.len(),
        vec![
            cols::string_column("symbol", &sym),
            frames::u32_column("di", &di),
            frames::i32_column("side", &side),
        ],
    )?)
}

/// polars' float `>`: a total order in which every NaN equals every other NaN and sits above
/// every number (`NaN > 0` is true, `1 > NaN` false), unlike IEEE.
fn pl_gt(x: f64, y: f64) -> bool {
    if x.is_nan() {
        !y.is_nan()
    } else if y.is_nan() {
        false
    } else {
        x > y
    }
}

/// `a > b` in a polars filter: null is false, NaN the largest value.
fn gt(a: Option<f64>, b: Option<f64>) -> bool {
    matches!((a, b), (Some(x), Some(y)) if pl_gt(x, y))
}

/// `a < b` in a polars filter: null is false, NaN the largest value.
fn lt(a: Option<f64>, b: Option<f64>) -> bool {
    matches!((a, b), (Some(x), Some(y)) if pl_gt(y, x))
}

/// `settle > 0` in a polars filter: a NaN settle passes it (NaN is the largest value).
fn pl_positive(v: Option<f64>) -> bool {
    v.is_some_and(|s| pl_gt(s, 0.0))
}

/// `symbol, di, side` for one family, exactly as the scripts filter.
///
/// Ports `baskfy_core.fno.research.futures_signals`. A2's long leg compares the rank with
/// `mn - 10` in `UInt32`, which wraps below ten names (no long then), as polars does.
///
/// # Errors
/// A missing column or a polars error.
#[allow(clippy::too_many_lines)]
pub fn futures_signals(panel: &DataFrame, family: FuturesFamily) -> FnoResult<DataFrame> {
    let symbol = cols::strings(panel, "symbol")?;
    let di = frames::u32s(panel, "di")?;
    let c = cols::floats(panel, "c")?;
    let hi = cols::floats(panel, "hi20")?;
    let lo = cols::floats(panel, "lo20")?;
    let ma = cols::floats(panel, "ma50")?;
    let atr = cols::floats(panel, "atr")?;
    let up = cols::bools(panel, "nifty_up")?;
    let oi5 = cols::floats(panel, "oi5")?;
    let sig = |i: usize, side: i32| Signal {
        symbol: symbol[i].clone(),
        di: di[i],
        side,
    };
    let is_up = |i: usize| up[i] == Some(true);
    let is_down = |i: usize| up[i] == Some(false);
    let oi_rising = |i: usize| gt(oi5[i], Some(0.0));
    let long = |i: usize| gt(c[i], hi[i]) && gt(c[i], ma[i]);
    let short = |i: usize| lt(c[i], lo[i]) && lt(c[i], ma[i]);
    let mut out: Vec<Signal> = Vec::new();
    if family == FuturesFamily::A4 {
        let idx: Vec<usize> = (0..panel.height())
            .filter(|&i| {
                matches!(
                    symbol[i].as_deref(),
                    Some("NIFTY" | "BANKNIFTY" | "MIDCPNIFTY")
                ) && atr[i].is_some()
                    && ma[i].is_some()
            })
            .collect();
        out.extend(idx.iter().filter(|&&i| long(i)).map(|&i| sig(i, 1)));
        out.extend(idx.iter().filter(|&&i| short(i)).map(|&i| sig(i, -1)));
        return signals_frame(&out);
    }
    let base = liquid_rows(panel, 0.6)?.rows;
    match family {
        FuturesFamily::A1 => {
            out.extend(
                base.iter()
                    .filter(|&&i| long(i) && oi_rising(i))
                    .map(|&i| sig(i, 1)),
            );
            out.extend(
                base.iter()
                    .filter(|&&i| short(i) && oi_rising(i))
                    .map(|&i| sig(i, -1)),
            );
        }
        FuturesFamily::A1R => {
            out.extend(
                base.iter()
                    .filter(|&&i| long(i) && oi_rising(i) && is_up(i))
                    .map(|&i| sig(i, 1)),
            );
            out.extend(
                base.iter()
                    .filter(|&&i| short(i) && oi_rising(i) && is_down(i))
                    .map(|&i| sig(i, -1)),
            );
        }
        FuturesFamily::A0 => {
            out.extend(
                base.iter()
                    .filter(|&&i| long(i) && is_up(i))
                    .map(|&i| sig(i, 1)),
            );
            out.extend(
                base.iter()
                    .filter(|&&i| short(i) && is_down(i))
                    .map(|&i| sig(i, -1)),
            );
        }
        FuturesFamily::A0Long => {
            out.extend(
                base.iter()
                    .filter(|&&i| long(i) && is_up(i))
                    .map(|&i| sig(i, 1)),
            );
        }
        FuturesFamily::A3 => {
            let ret1 = cols::floats(panel, "ret1")?;
            let oi1 = cols::floats(panel, "oi1")?;
            out.extend(
                base.iter()
                    .filter(|&&i| gt(ret1[i], Some(0.02)) && gt(oi1[i], Some(0.05)))
                    .map(|&i| sig(i, 1)),
            );
            out.extend(
                base.iter()
                    .filter(|&&i| lt(ret1[i], Some(-0.02)) && gt(oi1[i], Some(0.05)))
                    .map(|&i| sig(i, -1)),
            );
        }
        FuturesFamily::A2 => {
            let mom = cols::floats(panel, "mom63")?;
            let wk: Vec<usize> = base
                .iter()
                .copied()
                .filter(|&i| mom[i].is_some() && di[i].is_some_and(|d| d % 5 == 0))
                .collect();
            let mut groups: BTreeMap<Option<u32>, Vec<usize>> = BTreeMap::new();
            for (k, &i) in wk.iter().enumerate() {
                groups.entry(di[i]).or_default().push(k);
            }
            let mut mr = vec![None; wk.len()];
            let mut mn = vec![0_u32; wk.len()];
            for members in groups.values() {
                let vals: Vec<Option<f64>> = members.iter().map(|&k| mom[wk[k]]).collect();
                let ranks = frames::rank_average(&vals, false);
                let len = u32::try_from(members.len()).unwrap_or(u32::MAX);
                for (j, &k) in members.iter().enumerate() {
                    mr[k] = ranks[j];
                    mn[k] = len;
                }
            }
            for (k, &i) in wk.iter().enumerate() {
                if gt(mr[k], Some(f64::from(mn[k].wrapping_sub(10)))) {
                    out.push(sig(i, 1));
                }
            }
            for (k, &i) in wk.iter().enumerate() {
                if mr[k].is_some_and(|r| r <= 10.0) {
                    out.push(sig(i, -1));
                }
            }
        }
        FuturesFamily::A4 => {}
    }
    signals_frame(&out)
}

/// `simulate`'s parameters.
///
/// Ports `baskfy_core.fno.research.FuturesParams`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FuturesParams {
    /// The longest hold, sessions.
    pub horizon: i64,
    /// The stop's distance, in ATR.
    pub k_atr: f64,
    /// A chandelier trail.
    #[serde(default)]
    pub trail: bool,
    /// `res_f2`: charge one more round trip per calendar roll inside the hold.
    #[serde(default)]
    pub charge_rolls: bool,
    /// The round-trip cost, share of notional.
    #[serde(default = "default_cost")]
    pub cost: f64,
}

fn default_cost() -> f64 {
    FUTURES_ROUND_TRIP_COST
}

/// F2's spec as `res_f2.py` ran it: A0 long, 3 ATR chandelier, ≤ 40 sessions, rolls costed.
///
/// Ports `baskfy_core.fno.research.F2_SPEC`.
#[must_use]
pub fn f2_spec() -> FuturesParams {
    FuturesParams {
        horizon: 40,
        k_atr: 3.0,
        trail: true,
        charge_rolls: true,
        cost: FUTURES_ROUND_TRIP_COST,
    }
}

/// Group row indices by a key, the groups in order of first appearance.
fn partition<K: Eq + std::hash::Hash + Clone>(keys: &[K]) -> Vec<(K, Vec<usize>)> {
    let mut order: Vec<(K, Vec<usize>)> = Vec::new();
    let mut at: HashMap<K, usize> = HashMap::new();
    for (i, k) in keys.iter().enumerate() {
        if let Some(&g) = at.get(k) {
            order[g].1.push(i);
        } else {
            at.insert(k.clone(), order.len());
            order.push((k.clone(), vec![i]));
        }
    }
    order
}

fn nan(v: Option<f64>) -> f64 {
    v.unwrap_or(f64::NAN)
}

/// The columns of [`simulate_futures`]'s output.
const SIM_COLUMNS: [&str; 7] = ["symbol", "entry", "side", "R", "ret", "days", "rolls"];

/// `simulate`: signal at close t, entry at open t+1, a k-ATR stop touched intraday (at the
/// stop, or the open if gapped through), trailing if asked, else the close after `horizon`
/// sessions. One trade per symbol at a time.
///
/// Ports `baskfy_core.fno.research.simulate_futures`.
///
/// # Errors
/// `KeyError` for a signal's symbol the panel lacks; a missing column.
#[allow(clippy::too_many_lines)]
pub fn simulate_futures(
    panel: &DataFrame,
    signals: &DataFrame,
    params: &FuturesParams,
) -> FnoResult<DataFrame> {
    let p_sym = cols::strings(panel, "symbol")?;
    let p_di = cols::ints(panel, "di")?;
    let p_o = cols::floats(panel, "o")?;
    let p_h = cols::floats(panel, "h")?;
    let p_l = cols::floats(panel, "l")?;
    let p_c = cols::floats(panel, "c")?;
    let p_atr = cols::floats(panel, "atr")?;
    let p_date = cols::dates(panel, "date")?;
    let p_exp = cols::dates(panel, "expiry")?;
    let arr: HashMap<Option<String>, Vec<usize>> = partition(&p_sym).into_iter().collect();
    let s_sym = cols::strings(signals, "symbol")?;
    let s_di = cols::ints(signals, "di")?;
    let s_side = cols::ints(signals, "side")?;
    let mut out: Vec<Vec<Cell>> = Vec::new();
    for (key, members) in partition(&s_sym) {
        let Some(rows) = arr.get(&key) else {
            return Err(FnoError::Key(key.unwrap_or_else(|| "None".to_owned())));
        };
        let di: Vec<i64> = rows
            .iter()
            .map(|&r| {
                p_di[r].ok_or_else(|| {
                    FnoError::Type(
                        "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"
                            .to_owned(),
                    )
                })
            })
            .collect::<FnoResult<_>>()?;
        let o: Vec<f64> = rows.iter().map(|&r| nan(p_o[r])).collect();
        let h: Vec<f64> = rows.iter().map(|&r| nan(p_h[r])).collect();
        let lo: Vec<f64> = rows.iter().map(|&r| nan(p_l[r])).collect();
        let cl: Vec<f64> = rows.iter().map(|&r| nan(p_c[r])).collect();
        let atr: Vec<f64> = rows.iter().map(|&r| nan(p_atr[r])).collect();
        let dates: Vec<Option<NaiveDate>> = rows.iter().map(|&r| p_date[r]).collect();
        let exps: Vec<Option<NaiveDate>> = rows.iter().map(|&r| p_exp[r]).collect();
        let mut pos: HashMap<i64, usize> = HashMap::new();
        for (i, d) in di.iter().enumerate() {
            pos.insert(*d, i);
        }
        let n = di.len();
        let mut busy_until: i64 = -1;
        let mut ordered = members.clone();
        ordered.sort_by_key(|&k| s_di[k]);
        for k in ordered {
            let sd = s_di[k].ok_or_else(|| {
                FnoError::Type(
                    "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"
                        .to_owned(),
                )
            })?;
            let Some(&i) = pos.get(&sd) else { continue };
            let i_signed = i64::try_from(i).unwrap_or(i64::MAX);
            if i + 1 >= n || i_signed <= busy_until || !atr[i].is_finite() {
                continue;
            }
            let side = s_side[k].ok_or_else(|| {
                FnoError::Type(
                    "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"
                        .to_owned(),
                )
            })?;
            #[allow(clippy::cast_precision_loss)]
            let side_f = side as f64;
            let e = o[i + 1];
            let risk = params.k_atr * atr[i];
            if risk <= 0.0 {
                continue;
            }
            let mut stop = e - side_f * risk;
            let mut exit_px: Option<f64> = None;
            let mut j = i + 1;
            let horizon = usize::try_from(params.horizon.max(0)).unwrap_or(usize::MAX);
            let last = (i.saturating_add(horizon)).min(n - 1);
            let mut best = e;
            while j <= last {
                if params.trail && j > i + 1 {
                    if side > 0 {
                        best = pmax(best, cl[j - 1]);
                        stop = pmax(stop, best - risk);
                    } else {
                        best = pmin(best, cl[j - 1]);
                        stop = pmin(stop, best + risk);
                    }
                }
                if side > 0 && lo[j] <= stop {
                    exit_px = Some(if j > i + 1 { pmin(stop, o[j]) } else { stop });
                    break;
                }
                if side < 0 && h[j] >= stop {
                    exit_px = Some(if j > i + 1 { pmax(stop, o[j]) } else { stop });
                    break;
                }
                j += 1;
            }
            let exit_px = match exit_px {
                Some(x) => x,
                None => {
                    j = last;
                    cl[j]
                }
            };
            let rolls: i64 = if params.charge_rolls {
                let count = (i + 2..=j).filter(|&m| exps[m] != exps[m - 1]).count();
                i64::try_from(count).unwrap_or(i64::MAX)
            } else {
                0
            };
            #[allow(clippy::cast_precision_loss)]
            let pnl = side_f * (exit_px - e) - params.cost * e * (1 + rolls) as f64;
            out.push(vec![
                key.clone().map_or(Cell::Str("None".to_owned()), Cell::Str),
                Cell::from(dates[i + 1]),
                Cell::Int(side),
                Cell::Float(fdiv(pnl, risk)?),
                Cell::Float(fdiv(pnl, e)?),
                Cell::Int(i64::try_from(j - i).unwrap_or(i64::MAX)),
                Cell::Int(rolls),
            ]);
            busy_until = i64::try_from(j).unwrap_or(i64::MAX);
        }
    }
    frames::from_rows(&SIM_COLUMNS, &out)
}

/// `res_f2.py`: F2's spec with roll costs.
///
/// Ports `baskfy_core.fno.research.f2_trades`.
///
/// # Errors
/// As [`simulate_futures`].
pub fn f2_trades(cont: &DataFrame) -> FnoResult<DataFrame> {
    let panel = futures_panel(cont)?;
    let signals = futures_signals(&panel, FuturesFamily::A0Long)?;
    simulate_futures(&panel, &signals, &f2_spec())
}

// =============================================================================================
// res_options.py — the panel
// =============================================================================================

/// `(symbol, expiry, date)`, nulls kept as Python keeps `None` keys.
pub type FutureKey = (Option<String>, Option<NaiveDate>, Option<NaiveDate>);

/// `(symbol, date, lr)`.
type RankRow = (Option<String>, Option<NaiveDate>, Option<f64>);

/// A month's top-50 annualised bases and their net-above-7 % flags.
type MonthCells = (Vec<Option<f64>>, Vec<Option<bool>>);

/// A strike as a dict key: a float hashed by value (`-0.0` is `0.0`, as in Python).
#[derive(Debug, Clone, Copy)]
pub struct Strike(pub f64);

impl Strike {
    fn norm(self) -> f64 {
        if self.0 == 0.0 { 0.0 } else { self.0 }
    }
}

impl PartialEq for Strike {
    fn eq(&self, other: &Self) -> bool {
        self.norm().total_cmp(&other.norm()).is_eq()
    }
}

impl Eq for Strike {}

impl PartialOrd for Strike {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Strike {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.norm().total_cmp(&other.norm())
    }
}

/// One chain: `(strike, "CE"/"PE") -> (close, volume)`.
pub type Chain = BTreeMap<(Strike, String), (Option<f64>, Option<i64>)>;

/// `res_options`'s `GROUPS`, `FGROUPS`, `lots`, `dates`, `didx` and `rv`.
///
/// Ports `baskfy_core.fno.research.OptionPanel`. `groups` keeps Python's dict order (the
/// first appearance of each `(symbol, expiry)` in the options rows).
#[derive(Debug, Clone)]
pub struct OptionPanel {
    /// `(symbol, expiry) -> date -> chain`, in insertion order.
    pub groups: Vec<((String, NaiveDate), BTreeMap<NaiveDate, Chain>)>,
    /// `(symbol, expiry, date) -> settle`.
    pub fgroups: HashMap<FutureKey, f64>,
    /// `symbol -> median lot`.
    pub lots: BTreeMap<String, f64>,
    /// The sessions, sorted.
    pub dates: Vec<NaiveDate>,
    /// `date -> its index in dates`.
    pub didx: HashMap<NaiveDate, usize>,
    /// `(symbol, date) -> rv20`.
    pub rv20: HashMap<(Option<String>, Option<NaiveDate>), Option<f64>>,
    /// The continuous series.
    pub cont: DataFrame,
    group_at: HashMap<(String, NaiveDate), usize>,
}

impl OptionPanel {
    /// The session `n` trading days before `expiry` (expiry itself is 0).
    ///
    /// Ports `baskfy_core.fno.research.OptionPanel.trading_days_before`.
    #[must_use]
    pub fn trading_days_before(&self, expiry: NaiveDate, n: i64) -> Option<NaiveDate> {
        let at = i64::try_from(*self.didx.get(&expiry)?).ok()?;
        let i = at - n;
        if i < 0 {
            return None;
        }
        self.dates.get(usize::try_from(i).ok()?).copied()
    }

    /// `panel.groups[(symbol, expiry)]`.
    #[must_use]
    pub fn group(&self, symbol: &str, expiry: NaiveDate) -> Option<&BTreeMap<NaiveDate, Chain>> {
        let at = *self.group_at.get(&(symbol.to_owned(), expiry))?;
        self.groups.get(at).map(|g| &g.1)
    }

    fn fgroup(&self, symbol: &str, expiry: NaiveDate, date: NaiveDate) -> Option<f64> {
        self.fgroups
            .get(&(Some(symbol.to_owned()), Some(expiry), Some(date)))
            .copied()
    }
}

/// `_liquidity_rank`: stock futures, `t20` (20-session median turnover) and its rank `lr`
/// (descending, average) among the session's names: `(symbol, date, lr)` rows.
fn liquidity_rank(cont: &DataFrame) -> FnoResult<Vec<RankRow>> {
    let instrument = cols::strings(cont, "instrument")?;
    let symbol = cols::strings(cont, "symbol")?;
    let di = frames::u32s(cont, "di")?;
    let date = cols::dates(cont, "date")?;
    let turn = cols::floats(cont, "fut_turnover")?;
    let mut idx: Vec<usize> = (0..cont.height())
        .filter(|&i| instrument[i].as_deref() == Some("FUTSTK"))
        .collect();
    idx.sort_by(|a, b| (&symbol[*a], di[*a]).cmp(&(&symbol[*b], di[*b])));
    let syms: Vec<&Option<String>> = idx.iter().map(|&i| &symbol[i]).collect();
    let mut t20 = vec![None; idx.len()];
    for g in frames::runs(&syms) {
        let vals: Vec<Option<f64>> = g.clone().map(|k| turn[idx[k]]).collect();
        let med = cols::rolling(&vals, cols::Roll::Median, 20)?;
        for (j, k) in g.enumerate() {
            t20[k] = med[j];
        }
    }
    let mut by_date: BTreeMap<Option<NaiveDate>, Vec<usize>> = BTreeMap::new();
    for (k, &i) in idx.iter().enumerate() {
        by_date.entry(date[i]).or_default().push(k);
    }
    let mut lr = vec![None; idx.len()];
    for members in by_date.values() {
        let vals: Vec<Option<f64>> = members.iter().map(|&k| t20[k]).collect();
        let ranks = frames::rank_average(&vals, true);
        for (j, &k) in members.iter().enumerate() {
            lr[k] = ranks[j];
        }
    }
    Ok(idx
        .iter()
        .enumerate()
        .map(|(k, &i)| (symbol[i].clone(), date[i], lr[k]))
        .collect())
}

/// `load_groups(None)`'s universe: every stock ever in the top `max(top_n, 60)`.
///
/// Ports `baskfy_core.fno.research.liquid_symbols`.
///
/// # Errors
/// A missing column or a polars error.
pub fn liquid_symbols(cont: &DataFrame, top_n: i64) -> FnoResult<BTreeSet<String>> {
    #[allow(clippy::cast_precision_loss)]
    let cut = top_n.max(60) as f64;
    Ok(liquidity_rank(cont)?
        .into_iter()
        .filter(|(_, _, lr)| lr.is_some_and(|r| r <= cut))
        .map(|(s, _, _)| s.unwrap_or_else(|| "None".to_owned()))
        .collect())
}

/// `res_options`'s module state plus `load_groups(symbols)`.
///
/// Ports `baskfy_core.fno.research.build_option_panel`.
///
/// # Errors
/// A missing column, a null strike (`float(None)`), or a polars error.
#[allow(clippy::too_many_lines)]
pub fn build_option_panel(
    options: &DataFrame,
    futures: &DataFrame,
    cont: &DataFrame,
    symbols: &BTreeSet<String>,
) -> FnoResult<OptionPanel> {
    let o_date = cols::dates(options, "date")?;
    let o_exp = cols::dates(options, "expiry")?;
    let o_sym = cols::strings(options, "symbol")?;
    let o_strike = cols::floats(options, "strike")?;
    let o_type = cols::strings(options, "option_type")?;
    let o_close = cols::floats(options, "close")?;
    let o_vol = cols::ints(options, "volume")?;
    let opt: Vec<usize> = (0..options.height())
        .filter(|&i| match (o_date[i], o_exp[i], &o_sym[i]) {
            (Some(d), Some(e), Some(s)) => {
                e.checked_sub_signed(chrono::TimeDelta::days(32))
                    .is_some_and(|lim| d >= lim)
                    && symbols.contains(s)
            }
            _ => false,
        })
        .collect();
    let f_settle = cols::floats(futures, "settle")?;
    let f_sym = cols::strings(futures, "symbol")?;
    let f_exp = cols::dates(futures, "expiry")?;
    let f_date = cols::dates(futures, "date")?;
    let f_lot = cols::ints(futures, "lot_size")?;
    let fut: Vec<usize> = (0..futures.height())
        .filter(|&i| pl_positive(f_settle[i]))
        .collect();
    let mut lot_groups: BTreeMap<Option<String>, Vec<Option<f64>>> = BTreeMap::new();
    for &i in &fut {
        if let Some(l) = f_lot[i] {
            #[allow(clippy::cast_precision_loss)]
            lot_groups
                .entry(f_sym[i].clone())
                .or_default()
                .push(Some(l as f64));
        }
    }
    let mut lots = BTreeMap::new();
    for (s, vals) in lot_groups {
        if let Some(m) = frames::median(&vals) {
            lots.insert(s.unwrap_or_else(|| "None".to_owned()), m);
        }
    }
    let dates: Vec<NaiveDate> = fut
        .iter()
        .filter_map(|&i| f_date[i])
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let didx: HashMap<NaiveDate, usize> = dates.iter().enumerate().map(|(i, d)| (*d, i)).collect();
    // rv20: per symbol, sorted by di.
    let c_sym = cols::strings(cont, "symbol")?;
    let c_di = frames::u32s(cont, "di")?;
    let c_date = cols::dates(cont, "date")?;
    let c_r = cols::floats(cont, "r")?;
    let mut cidx: Vec<usize> = (0..cont.height()).collect();
    cidx.sort_by(|a, b| (&c_sym[*a], c_di[*a]).cmp(&(&c_sym[*b], c_di[*b])));
    let mut rv20 = HashMap::new();
    let csyms: Vec<&Option<String>> = cidx.iter().map(|&i| &c_sym[i]).collect();
    for g in frames::runs(&csyms) {
        let vals: Vec<Option<f64>> = g.clone().map(|k| c_r[cidx[k]]).collect();
        let sd = cols::rolling(&vals, cols::Roll::Std, 20)?;
        for (j, k) in g.enumerate() {
            let i = cidx[k];
            rv20.insert(
                (c_sym[i].clone(), c_date[i]),
                sd[j].map(|v| v * 252.0_f64.sqrt()),
            );
        }
    }
    let mut groups: Vec<((String, NaiveDate), BTreeMap<NaiveDate, Chain>)> = Vec::new();
    let mut group_at: HashMap<(String, NaiveDate), usize> = HashMap::new();
    for &i in &opt {
        let (Some(s), Some(e), Some(d)) = (o_sym[i].clone(), o_exp[i], o_date[i]) else {
            continue;
        };
        let strike = o_strike[i].ok_or_else(|| {
            FnoError::Type(
                "float() argument must be a string or a real number, not 'NoneType'".to_owned(),
            )
        })?;
        let key = (s, e);
        let at = match group_at.get(&key) {
            Some(&at) => at,
            None => {
                group_at.insert(key.clone(), groups.len());
                groups.push((key, BTreeMap::new()));
                groups.len() - 1
            }
        };
        let kind = o_type[i].clone().unwrap_or_else(|| "None".to_owned());
        groups[at]
            .1
            .entry(d)
            .or_default()
            .insert((Strike(strike), kind), (o_close[i], o_vol[i]));
    }
    let mut fgroups = HashMap::new();
    for &i in &fut {
        fgroups.insert(
            (f_sym[i].clone(), f_exp[i], f_date[i]),
            f_settle[i].unwrap_or(0.0),
        );
    }
    Ok(OptionPanel {
        groups,
        fgroups,
        lots,
        dates,
        didx,
        rv20,
        cont: cont.clone(),
        group_at,
    })
}

// =============================================================================================
// res_options.py — condor_trades
// =============================================================================================

/// What `condor_trades` returns.
///
/// Ports `baskfy_core.fno.research.CondorRun`.
#[derive(Debug, Clone)]
pub struct CondorRun {
    /// One row per traded cycle.
    pub trades: DataFrame,
    /// `CA_SKIPPED`: cycles whose underlying moved outside (0.7, 1.4) in one session.
    pub ca_skipped: Vec<(String, NaiveDate)>,
}

const CONDOR_COLUMNS: [&str; 13] = [
    "symbol",
    "entry",
    "expiry",
    "exit",
    "iv",
    "rv20",
    "credit",
    "maxloss",
    "pnl",
    "R",
    "modelled_legs",
    "credit_pct",
    "cost_R",
];

/// `(strike, kind, quantity)`.
type Leg = (f64, &'static str, i64);

#[derive(Debug, Default)]
struct Walk {
    exit_val: Option<f64>,
    exit_d: Option<NaiveDate>,
    modelled: i64,
    exit_legs: Vec<(f64, bool)>,
    broken: bool,
}

fn chain_get<'a>(ch: &'a Chain, strike: f64, kind: &str) -> Option<&'a (Option<f64>, Option<i64>)> {
    ch.get(&(Strike(strike), kind.to_owned()))
}

/// Python truthiness of a float cell (`None`, `0.0` false; NaN true).
fn truthy_f(v: Option<f64>) -> bool {
    v.is_some_and(|x| x != 0.0)
}

/// Python truthiness of an int cell.
fn truthy_i(v: Option<i64>) -> bool {
    v.is_some_and(|x| x != 0)
}

fn pick(strikes: &[f64], target: f64, side: i64) -> Option<f64> {
    if side > 0 {
        strikes
            .iter()
            .copied()
            .filter(|k| *k >= target)
            .reduce(|a, b| if b < a { b } else { a })
    } else {
        strikes
            .iter()
            .copied()
            .filter(|k| *k <= target)
            .reduce(|a, b| if b > a { b } else { a })
    }
}

/// `np.argmin(np.abs(strikes - target))`: the first of the nearest.
fn nearest(strikes: &[f64], target: f64) -> Option<f64> {
    let mut best: Option<(f64, f64)> = None;
    for k in strikes {
        let d = (k - target).abs();
        match best {
            Some((bd, _)) if d.partial_cmp(&bd) != Some(std::cmp::Ordering::Less) => {}
            _ => best = Some((d, *k)),
        }
    }
    best.map(|(_, k)| k)
}

#[allow(clippy::too_many_arguments)]
fn walk(
    panel: &OptionPanel,
    sym: &str,
    e: NaiveDate,
    d0: NaiveDate,
    dx: NaiveDate,
    legs: &[Leg],
    iv: f64,
    credit: f64,
    widths: (f64, f64, f64, f64),
    params: &CondorParams,
) -> FnoResult<Walk> {
    let empty = BTreeMap::new();
    let by_day = panel.group(sym, e).unwrap_or(&empty);
    let (wc, sc, sp, wp) = widths;
    let mut out = Walk {
        exit_d: Some(dx),
        ..Walk::default()
    };
    let mut f_prev = panel
        .fgroup(sym, e, d0)
        .ok_or_else(|| FnoError::Key(format!("('{sym}', {e}, {d0})")))?;
    let mut breached = false;
    let i0 = panel.didx.get(&d0).copied().unwrap_or(0);
    let ix = panel.didx.get(&dx).copied().unwrap_or(0);
    let no_chain = Chain::new();
    for i in i0 + 1..=ix {
        let d = panel.dates[i];
        let chd = by_day.get(&d).unwrap_or(&no_chain);
        let Some(fdv) = panel.fgroup(sym, e, d) else {
            continue;
        };
        let move_ = fdv / f_prev;
        if !(0.7 < move_ && move_ < 1.4) {
            out.broken = true;
            break;
        }
        f_prev = fdv;
        let td = years_floor(e, d);
        let (mut ce, mut pe) = (0.0_f64, 0.0_f64);
        let mut modelled = 0;
        let mut legpx = Vec::with_capacity(legs.len());
        for &(k, cp, q) in legs {
            let p = match chain_get(chd, k, cp) {
                Some((close, vol)) if truthy_i(*vol) && truthy_f(*close) => close.unwrap_or(0.0),
                _ => {
                    modelled += 1;
                    b76_one(fdv, k, td, iv, cp, RESEARCH_RATE)
                }
            };
            #[allow(clippy::cast_precision_loss)]
            let v = -(q as f64) * p;
            if cp == "CE" {
                ce += v;
            } else {
                pe += v;
            }
            legpx.push((p, q > 0));
        }
        let val = pmin(pmax(ce, 0.0), wc - sc) + pmin(pmax(pe, 0.0), sp - wp);
        let stopped = params.loss_mult > 0.0 && val >= (1.0 + params.loss_mult) * credit;
        let take = val <= (1.0 - params.profit_take) * credit;
        if stopped || breached || take || i == ix {
            out.exit_val = Some(val);
            out.exit_d = Some(d);
            out.modelled = modelled;
            out.exit_legs = legpx;
            if stopped || breached || take {
                break;
            }
        }
        if params.defend && (fdv >= sc || fdv <= sp) {
            breached = true;
        }
    }
    Ok(out)
}

/// `_entry_iv`: the chain's sorted strikes and the mean IV of the ATM call and put.
fn entry_iv(ch: &Chain, forward: f64, years: f64) -> Option<(Vec<f64>, f64)> {
    let strikes: Vec<f64> = ch
        .keys()
        .map(|(k, _)| *k)
        .collect::<BTreeSet<Strike>>()
        .into_iter()
        .map(|s| s.0)
        .collect();
    let atm = nearest(&strikes, forward)?;
    let ce = chain_get(ch, atm, "CE")?.0?;
    let pe = chain_get(ch, atm, "PE")?.0?;
    let a = implied_one(forward, atm, years, ce, "CE", RESEARCH_RATE);
    let b = implied_one(forward, atm, years, pe, "PE", RESEARCH_RATE);
    Some((strikes, (a + b) / 2.0))
}

fn trend_map(cont: &DataFrame) -> FnoResult<HashMap<(String, NaiveDate), i32>> {
    let symbol = cols::strings(cont, "symbol")?;
    let di = frames::u32s(cont, "di")?;
    let date = cols::dates(cont, "date")?;
    let c = cols::floats(cont, "c")?;
    let nifd = nifty_up_by(&symbol, &date, &di, &c)?;
    let mut idx: Vec<usize> = (0..cont.height()).collect();
    idx.sort_by(|a, b| (&symbol[*a], di[*a]).cmp(&(&symbol[*b], di[*b])));
    let syms: Vec<&Option<String>> = idx.iter().map(|&i| &symbol[i]).collect();
    let mut trend = HashMap::new();
    for g in frames::runs(&syms) {
        let vals: Vec<Option<f64>> = g.clone().map(|k| c[idx[k]]).collect();
        let ma = cols::rolling(&vals, cols::Roll::Mean, 50)?;
        for (j, k) in g.enumerate() {
            let i = idx[k];
            let Some(m) = ma[j] else { continue };
            let Some(d) = date[i] else { continue };
            let Some(Some(nup)) = nifd.get(&d).copied() else {
                continue;
            };
            let Some(s) = symbol[i].clone() else { continue };
            let Some(cv) = c[i] else { continue };
            let up = if is_index(&s) { cv > m } else { nup };
            let t = if cv > m && up {
                1
            } else if cv < m && !up {
                -1
            } else {
                0
            };
            trend.insert((s, d), t);
        }
    }
    Ok(trend)
}

/// `res_options.condor_trades`: one row per cycle entered `n_before` sessions before each
/// monthly expiry, exited at the close of `E - exit_n` or earlier.
///
/// Ports `baskfy_core.fno.research.condor_trades`.
///
/// # Errors
/// A missing column or a polars error.
#[allow(clippy::too_many_lines)]
pub fn condor_trades(panel: &OptionPanel, params: &CondorParams) -> FnoResult<CondorRun> {
    let mut rows: Vec<Vec<Cell>> = Vec::new();
    let mut ca_skipped = Vec::new();
    let mut liqd: HashMap<(String, NaiveDate), Option<f64>> = HashMap::new();
    if params.top_n != 0 {
        for (s, d, r) in liquidity_rank(&panel.cont)? {
            if let (Some(s), Some(d)) = (s, d) {
                liqd.insert((s, d), r);
            }
        }
    }
    let trend = if params.mode == CondorMode::TrendCredit {
        trend_map(&panel.cont)?
    } else {
        HashMap::new()
    };
    for ((sym, e), by_day) in &panel.groups {
        if let Some(only) = &params.symbols
            && !only.is_empty()
            && !only.contains(sym)
        {
            continue;
        }
        let (Some(d0), Some(dx)) = (
            panel.trading_days_before(*e, params.n_before),
            panel.trading_days_before(*e, params.exit_n),
        ) else {
            continue;
        };
        if d0 >= dx || !by_day.contains_key(&d0) {
            continue;
        }
        let Some(forward) = panel.fgroup(sym, *e, d0) else {
            continue;
        };
        if params.top_n != 0 && !is_index(sym) {
            #[allow(clippy::cast_precision_loss)]
            let top = params.top_n as f64;
            match liqd.get(&(sym.clone(), d0)).copied().flatten() {
                Some(rank) if rank.partial_cmp(&top) != Some(std::cmp::Ordering::Greater) => {}
                _ => continue,
            }
        }
        let lot = match panel.lots.get(sym) {
            Some(l) if *l != 0.0 => *l,
            _ => 500.0,
        };
        let ch: Chain = by_day
            .get(&d0)
            .map(|c| {
                c.iter()
                    .filter(|(_, v)| truthy_f(v.0) && v.0.is_some_and(|x| x > 0.0))
                    .map(|(k, v)| (k.clone(), *v))
                    .collect()
            })
            .unwrap_or_default();
        if ch.len() < 8 {
            continue;
        }
        let years = days_f64(*e, d0) / 365.0;
        let Some((strikes, iv)) = entry_iv(&ch, forward, years) else {
            continue;
        };
        if !(0.05 < iv && iv < 1.5) {
            continue;
        }
        let sd = forward * iv * years.sqrt();
        let sc = pick(&strikes, forward + params.k_short * sd, 1);
        let sp = pick(&strikes, forward - params.k_short * sd, -1);
        let wc = pick(&strikes, forward + (params.k_short + params.k_wing) * sd, 1);
        let wp = pick(
            &strikes,
            forward - (params.k_short + params.k_wing) * sd,
            -1,
        );
        let (Some(sc), Some(sp), Some(wc), Some(wp)) = (sc, sp, wc, wp) else {
            continue;
        };
        if wc <= sc || wp >= sp {
            continue;
        }
        let mut legs: Vec<Leg> = vec![(sc, "CE", -1), (wc, "CE", 1), (sp, "PE", -1), (wp, "PE", 1)];
        if params.mode == CondorMode::TrendCredit {
            let tdir = trend.get(&(sym.clone(), d0)).copied().unwrap_or(0);
            if tdir == 0 {
                continue;
            }
            legs = if tdir > 0 {
                vec![(sp, "PE", -1), (wp, "PE", 1)]
            } else {
                vec![(sc, "CE", -1), (wc, "CE", 1)]
            };
        }
        let mut px0 = Vec::with_capacity(legs.len());
        let mut ok = true;
        for &(k, cp, q) in &legs {
            match chain_get(&ch, k, cp) {
                Some((Some(p), vol)) if !(q < 0 && !truthy_i(*vol)) => px0.push(*p),
                _ => {
                    ok = false;
                    break;
                }
            }
        }
        if !ok {
            continue;
        }
        #[allow(clippy::cast_precision_loss)]
        let credit = py_sum(
            &legs
                .iter()
                .zip(&px0)
                .map(|(l, p)| -(l.2 as f64) * p)
                .collect::<Vec<_>>(),
        );
        let has_ce = legs.iter().any(|l| l.1 == "CE");
        let has_pe = legs.iter().any(|l| l.1 == "PE");
        let width = pmax(
            if has_ce { wc - sc } else { 0.0 },
            if has_pe { sp - wp } else { 0.0 },
        );
        let maxloss = width - credit;
        if credit <= 0.0 || maxloss <= 0.0 {
            continue;
        }
        let cost = py_sum(
            &legs
                .iter()
                .zip(&px0)
                .map(|(l, p)| leg_costs(*p, l.2 < 0, lot, params))
                .collect::<Vec<_>>(),
        );
        let w = walk(
            panel,
            sym,
            *e,
            d0,
            dx,
            &legs,
            iv,
            credit,
            (wc, sc, sp, wp),
            params,
        )?;
        if w.broken {
            ca_skipped.push((sym.clone(), *e));
            continue;
        }
        let Some(exit_val) = w.exit_val else {
            continue;
        };
        let exit_cost = py_sum(
            &w.exit_legs
                .iter()
                .map(|(p, sells)| leg_costs(*p, *sells, lot, params))
                .collect::<Vec<_>>(),
        );
        let pnl = credit - exit_val - cost - exit_cost;
        rows.push(vec![
            Cell::Str(sym.clone()),
            Cell::Date(d0),
            Cell::Date(*e),
            Cell::from(w.exit_d),
            Cell::Float(iv),
            Cell::from(
                panel
                    .rv20
                    .get(&(Some(sym.clone()), Some(d0)))
                    .copied()
                    .flatten(),
            ),
            Cell::Float(credit),
            Cell::Float(maxloss),
            Cell::Float(pnl),
            Cell::Float(pnl / maxloss),
            Cell::Int(w.modelled),
            Cell::Float(fdiv(credit, forward)?),
            Cell::Float((cost + exit_cost) / maxloss),
        ]);
    }
    Ok(CondorRun {
        trades: frames::from_rows(&CONDOR_COLUMNS, &rows)?,
        ca_skipped,
    })
}

// =============================================================================================
// res_debit.py
// =============================================================================================

/// C1's spread or C2's long option.
///
/// Ports `baskfy_core.fno.research.DebitStructure`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DebitStructure {
    /// A debit spread.
    Spread,
    /// A long ATM option.
    Long,
}

impl DebitStructure {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Spread => "spread",
            Self::Long => "long",
        }
    }
}

/// `res_debit`'s inner `px`: the traded close if the leg traded that day, else Black-76 at the
/// entry IV (modelled).
fn exit_mark(chx: &Chain, strike: f64, cp: &str, forward: f64, years: f64, iv: f64) -> (f64, i64) {
    if let Some((close, vol)) = chain_get(chx, strike, cp)
        && truthy_f(*close)
        && truthy_i(*vol)
    {
        return (close.unwrap_or(0.0), 0);
    }
    (b76_one(forward, strike, years, iv, cp, RESEARCH_RATE), 1)
}

const DEBIT_COLUMNS: [&str; 8] = [
    "symbol",
    "entry",
    "side",
    "iv",
    "debit_pct",
    "R_spread",
    "fut_ret",
    "modelled",
];

/// `res_debit.py` (C1 spread / C2 long) on the A0 signal.
///
/// Ports `baskfy_core.fno.research.debit_trades`.
///
/// # Errors
/// A missing column or a polars error.
#[allow(clippy::too_many_lines)]
pub fn debit_trades(
    panel: &OptionPanel,
    horizon: i64,
    structure: DebitStructure,
) -> FnoResult<DataFrame> {
    let params = CondorParams {
        slip_pct: 0.03,
        ..CondorParams::default()
    };
    let cont = &panel.cont;
    let instrument = cols::strings(cont, "instrument")?;
    let symbol = cols::strings(cont, "symbol")?;
    let di = frames::u32s(cont, "di")?;
    let date = cols::dates(cont, "date")?;
    let c = cols::floats(cont, "c")?;
    let turn = cols::floats(cont, "fut_turnover")?;
    let mut idx: Vec<usize> = (0..cont.height())
        .filter(|&i| instrument[i].as_deref() == Some("FUTSTK"))
        .collect();
    idx.sort_by(|a, b| (&symbol[*a], di[*a]).cmp(&(&symbol[*b], di[*b])));
    let m = idx.len();
    let mut hi20 = vec![None; m];
    let mut lo20 = vec![None; m];
    let mut ma50 = vec![None; m];
    let mut turn20 = vec![None; m];
    let syms: Vec<&Option<String>> = idx.iter().map(|&i| &symbol[i]).collect();
    for g in frames::runs(&syms) {
        let cg: Vec<Option<f64>> = g.clone().map(|k| c[idx[k]]).collect();
        let c1 = shifted(&cg, 1);
        let hi = rolling_extreme(&c1, 20, true);
        let lo = rolling_extreme(&c1, 20, false);
        let ma = cols::rolling(&cg, cols::Roll::Mean, 50)?;
        let tg: Vec<Option<f64>> = g.clone().map(|k| turn[idx[k]]).collect();
        let t20 = cols::rolling(&tg, cols::Roll::Median, 20)?;
        for (j, k) in g.enumerate() {
            hi20[k] = hi[j];
            lo20[k] = lo[j];
            ma50[k] = ma[j];
            turn20[k] = t20[j];
        }
    }
    let nifty = nifty_up_by(&symbol, &di, &di, &c)?;
    let mut by_di: BTreeMap<Option<u32>, Vec<usize>> = BTreeMap::new();
    for (k, &i) in idx.iter().enumerate() {
        by_di.entry(di[i]).or_default().push(k);
    }
    let mut keep = vec![false; m];
    for members in by_di.values() {
        let vals: Vec<Option<f64>> = members.iter().map(|&k| turn20[k]).collect();
        let ranks = frames::rank_average(&vals, true);
        #[allow(clippy::cast_precision_loss)]
        let tn = members.len() as f64;
        for (j, &k) in members.iter().enumerate() {
            keep[k] = ranks[j].is_some_and(|r| r <= 0.6 * tn);
        }
    }
    let mut sig: Vec<(Option<String>, Option<NaiveDate>, i64)> = Vec::new();
    for side in [1_i64, -1] {
        for k in 0..m {
            if !keep[k] {
                continue;
            }
            let i = idx[k];
            let up = di[i].and_then(|d| nifty.get(&d).copied().flatten());
            let hit = if side > 0 {
                gt(c[i], hi20[k]) && gt(c[i], ma50[k]) && up == Some(true)
            } else {
                lt(c[i], lo20[k]) && lt(c[i], ma50[k]) && up == Some(false)
            };
            if hit {
                sig.push((symbol[i].clone(), date[i], side));
            }
        }
    }
    sig.sort_by(|a, b| (&a.0, a.1).cmp(&(&b.0, b.1)));
    let mut expiries: HashMap<String, Vec<NaiveDate>> = HashMap::new();
    for ((s, e), _) in &panel.groups {
        expiries.entry(s.clone()).or_default().push(*e);
    }
    let mut rows: Vec<Vec<Cell>> = Vec::new();
    let mut busy: HashMap<String, NaiveDate> = HashMap::new();
    let n_dates = panel.dates.len();
    for (sym, d, side) in sig {
        let (Some(sym), Some(d)) = (sym, d) else {
            continue;
        };
        let Some(&dpos) = panel.didx.get(&d) else {
            continue;
        };
        if dpos + 1 >= n_dates {
            continue;
        }
        if busy.get(&sym).is_some_and(|b| d <= *b) {
            continue;
        }
        let d1 = panel.dates[dpos + 1];
        let d1pos = dpos + 1;
        let mut es: Vec<NaiveDate> = expiries
            .get(&sym)
            .map(|v| {
                v.iter()
                    .copied()
                    .filter(|e| {
                        panel.didx.get(e).is_some_and(|&p| {
                            i64::try_from(p).unwrap_or(0) - i64::try_from(d1pos).unwrap_or(0) >= 12
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();
        es.sort_unstable();
        let Some(&e) = es.first() else { continue };
        let Some(by_day) = panel.group(&sym, e) else {
            continue;
        };
        let Some(forward) = panel.fgroup(&sym, e, d1) else {
            continue;
        };
        let Some(day_chain) = by_day.get(&d1) else {
            continue;
        };
        let ch: Chain = day_chain
            .iter()
            .filter(|(_, v)| truthy_f(v.0) && v.0.is_some_and(|x| x > 0.0) && truthy_i(v.1))
            .map(|(k, v)| (k.clone(), *v))
            .collect();
        let cp = if side > 0 { "CE" } else { "PE" };
        let strikes: Vec<f64> = ch
            .keys()
            .filter(|(_, t)| t == cp)
            .map(|(k, _)| *k)
            .collect::<BTreeSet<Strike>>()
            .into_iter()
            .map(|s| s.0)
            .collect();
        if strikes.len() < 4 {
            continue;
        }
        let years = days_f64(e, d1) / 365.0;
        let Some(kb) = nearest(&strikes, forward) else {
            continue;
        };
        let Some(kb_px) = chain_get(&ch, kb, cp).and_then(|v| v.0) else {
            continue;
        };
        let iv = implied_one(forward, kb, years, kb_px, cp, RESEARCH_RATE);
        #[allow(clippy::cast_precision_loss)]
        let side_f = side as f64;
        let target = forward * (1.0 + side_f * 0.5 * iv * years.sqrt());
        let mut ks = nearest(&strikes, target);
        if structure == DebitStructure::Long {
            ks = None;
        } else if let Some(k) = ks
            && (k - kb) * side_f <= 0.0
        {
            continue;
        }
        let ks_px: Option<f64> = match ks {
            Some(k) => chain_get(&ch, k, cp).and_then(|v| v.0),
            None => Some(0.0),
        };
        let ks_or = if truthy_f(ks_px) {
            ks_px.unwrap_or(0.0)
        } else {
            0.0
        };
        let debit = kb_px - ks_or;
        let width = ks.map_or(1e12, |k| (k - kb).abs());
        if debit <= 0.0 || debit >= width {
            continue;
        }
        let lot = match panel.lots.get(&sym) {
            Some(l) if *l != 0.0 => *l,
            _ => 500.0,
        };
        let h = usize::try_from(horizon).unwrap_or(0);
        let Some(&epos) = panel.didx.get(&e) else {
            continue;
        };
        let ix = (d1pos + h).min(epos.saturating_sub(1));
        let dx = panel.dates[ix];
        let fx = panel.fgroup(&sym, e, dx);
        let no_chain = Chain::new();
        let chx = by_day.get(&dx).unwrap_or(&no_chain);
        let Some(fx) = fx else { continue };
        let mv = fx / forward;
        if !(0.7 < mv && mv < 1.4) {
            continue;
        }
        let tx = years_floor(e, dx);
        let (pb, m1) = exit_mark(chx, kb, cp, fx, tx, iv);
        let (ps, m2) = match ks {
            Some(k) => exit_mark(chx, k, cp, fx, tx, iv),
            None => (0.0, 0),
        };
        let val = pmin(pmax(pb - ps, 0.0), width);
        let mut cost = leg_costs(kb_px, false, lot, &params) + leg_costs(pb, true, lot, &params);
        if ks.is_some() {
            cost += leg_costs(ks_or_none(ks_px), true, lot, &params)
                + leg_costs(ps, false, lot, &params);
        }
        let pnl = val - debit - cost;
        let fut_ret = side_f * (fx / forward - 1.0) - FUTURES_ROUND_TRIP_COST;
        rows.push(vec![
            Cell::Str(sym.clone()),
            Cell::Date(d1),
            Cell::Int(side),
            Cell::Float(iv),
            Cell::Float(debit / forward),
            Cell::Float(pnl / debit),
            Cell::Float(fut_ret),
            Cell::Int(m1 + m2),
        ]);
        busy.insert(sym, dx);
    }
    frames::from_rows(&DEBIT_COLUMNS, &rows)
}

/// `ks_px or 0.0`.
fn ks_or_none(v: Option<f64>) -> f64 {
    if truthy_f(v) { v.unwrap_or(0.0) } else { 0.0 }
}

// =============================================================================================
// res_basis.py
// =============================================================================================

/// The basis study's numbers.
///
/// Ports `baskfy_core.fno.research.BasisStudy`.
#[derive(Debug, Clone)]
pub struct BasisStudy {
    /// Distinct sessions with a qualifying row.
    pub sessions: i64,
    /// `q -> quantile of the annualised basis`, for 0.1, 0.25, 0.5, 0.75, 0.9.
    pub quantiles: Vec<(f64, f64)>,
    /// Share of rows whose net annualised basis is above 7 %.
    pub share_net_above_7pct: f64,
    /// `m, median_ann_top50, share_net_above_7pct` by month (top 50 by turnover).
    pub by_month: DataFrame,
}

/// `res_basis.py` (E): front-month annualised basis on UDiFF days, ≥ 5 days to expiry.
///
/// Ports `baskfy_core.fno.research.basis_study`.
///
/// # Errors
/// Python's `TypeError` when no row qualifies (a null quantile), or a missing column.
#[allow(clippy::too_many_lines)]
pub fn basis_study(futures: &DataFrame, cycle_cost: f64) -> FnoResult<BasisStudy> {
    let instrument = cols::strings(futures, "instrument")?;
    let underlying = cols::floats(futures, "underlying")?;
    let settle = cols::floats(futures, "settle")?;
    let symbol = cols::strings(futures, "symbol")?;
    let date = cols::dates(futures, "date")?;
    let expiry = cols::dates(futures, "expiry")?;
    let turnover = cols::floats(futures, "turnover")?;
    // `front`: per (symbol, date), the row with the smallest expiry (a null expiry first).
    let mut front: HashMap<(Option<String>, Option<NaiveDate>), usize> = HashMap::new();
    for i in 0..futures.height() {
        if instrument[i].as_deref() != Some("FUTSTK")
            || underlying[i].is_none()
            || !pl_positive(settle[i])
        {
            continue;
        }
        let key = (symbol[i].clone(), date[i]);
        match front.get(&key) {
            Some(&j) if expiry[j] <= expiry[i] => {}
            _ => {
                front.insert(key, i);
            }
        }
    }
    struct Row {
        date: NaiveDate,
        ann: f64,
        net_ann: f64,
        turnover: Option<f64>,
    }
    let mut rows: Vec<Row> = Vec::new();
    for &i in front.values() {
        let (Some(e), Some(d)) = (expiry[i], date[i]) else {
            continue;
        };
        let dte = (e - d).num_days();
        if dte < 5 {
            continue;
        }
        let (Some(s), Some(u)) = (settle[i], underlying[i]) else {
            continue;
        };
        #[allow(clippy::cast_precision_loss)]
        let dte_f = dte as f64;
        let basis = s / u - 1.0;
        let ann = basis * 365.0 / dte_f;
        if ann.abs().partial_cmp(&1.0) != Some(std::cmp::Ordering::Less) {
            continue;
        }
        let net_ann = (basis - cycle_cost) * 365.0 / dte_f;
        rows.push(Row {
            date: d,
            ann,
            net_ann,
            turnover: turnover[i],
        });
    }
    // Every reduction below is order-free except the sums polars takes inside `median` and
    // `mean`; sort to a fixed order (date, ann) so the result does not depend on hashing.
    rows.sort_by(|a, b| a.date.cmp(&b.date).then(a.ann.total_cmp(&b.ann)));
    let anns: Vec<Option<f64>> = rows.iter().map(|r| Some(r.ann)).collect();
    let mut quantiles = Vec::new();
    for q in [0.1, 0.25, 0.5, 0.75, 0.9] {
        quantiles.push((q, as_float(frames::quantile_nearest(&anns, q)?)?));
    }
    let above = rows.iter().filter(|r| r.net_ann > 0.07).count();
    #[allow(clippy::cast_precision_loss)]
    let share = fdiv(above as f64, rows.len() as f64)?;
    let mut by_date: BTreeMap<NaiveDate, Vec<usize>> = BTreeMap::new();
    for (k, r) in rows.iter().enumerate() {
        by_date.entry(r.date).or_default().push(k);
    }
    let mut by_month: BTreeMap<String, MonthCells> = BTreeMap::new();
    for (d, members) in &by_date {
        let vals: Vec<Option<f64>> = members.iter().map(|&k| rows[k].turnover).collect();
        let ranks = frames::rank_average(&vals, true);
        for (j, &k) in members.iter().enumerate() {
            if ranks[j].is_some_and(|r| r <= 50.0) {
                let m = by_month.entry(d.format("%Y-%m").to_string()).or_default();
                m.0.push(Some(rows[k].ann));
                m.1.push(Some(rows[k].net_ann > 0.07));
            }
        }
    }
    let mut months = Vec::new();
    let mut medians = Vec::new();
    let mut shares = Vec::new();
    for (m, (anns, flags)) in &by_month {
        months.push(Some(m.clone()));
        medians.push(frames::median(anns));
        shares.push(frames::bool_mean(flags));
    }
    let by_month = DataFrame::new(
        months.len(),
        vec![
            cols::string_column("m", &months),
            cols::float_column("median_ann_top50", &medians),
            cols::float_column("share_net_above_7pct", &shares),
        ],
    )?;
    let sessions = by_date.len();
    Ok(BasisStudy {
        sessions: i64::try_from(sessions).unwrap_or(i64::MAX),
        quantiles,
        share_net_above_7pct: share,
        by_month,
    })
}
