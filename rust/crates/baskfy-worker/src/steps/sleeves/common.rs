//! What the three detectors share: the calendar read, the universe read, the bar loader, and
//! the conversions between a polars cell and what Python writes for it.
//!
//! Python spells each of these once **per sleeve** on purpose ("a sleeve reaching into another
//! sleeve's task module is how two books come to share a bug"); the Rust port keeps one copy of
//! the parts that are character-for-character the same SQL and parameterises the rest, and
//! each sleeve module names the Python function it reproduces.

use std::collections::BTreeSet;

use baskfy_core::error::CoreError;
use baskfy_core::py::decimal_from_float_str;
use chrono::{Days, NaiveDate};
use polars::prelude::{Column, DataFrame, DataType, IntoColumn, NamedFrom, PolarsError, Series};
use regex::RegexBuilder;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::precision::{self, Rule};
use crate::error::WorkerError;

/// `baskfy_core.seed_data.NSE_EXCHANGE_ID`.
pub(crate) const NSE_EXCHANGE_ID: i16 = 1;

/// A core error as the Python exception it ports (`ValueError`, the core's only class).
pub(crate) fn core_err(e: CoreError) -> WorkerError {
    match e {
        CoreError::Polars(p) => pl_err(&p),
        CoreError::Arithmetic(m) => WorkerError::python("InvalidOperation", m),
        CoreError::InvalidInput(m) | CoreError::MissingColumn(m) => {
            WorkerError::python("ValueError", m)
        }
    }
}

/// A polars error (Python's polars raises its own classes; the message is polars').
pub(crate) fn pl_err(e: &PolarsError) -> WorkerError {
    WorkerError::python("ComputeError", e.to_string())
}

/// A conversion that Python would raise on (`int(float('nan'))`, `Decimal` beyond 28 digits).
pub(crate) fn value_err(message: impl Into<String>) -> WorkerError {
    WorkerError::python("ValueError", message)
}

/// `lookback_start` (identical in `tasks/swing.py`, `tasks/vbt.py`, `tasks/twt.py`): the date
/// `sessions` trading days before `as_of`, from the calendar; with no calendar at all, `as_of -
/// sessions * 2` calendar days.
pub(crate) async fn lookback_start(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    sessions: i64,
) -> Result<NaiveDate, WorkerError> {
    let dates: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date <= $2 ORDER BY date DESC LIMIT $3",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(as_of)
    .bind(sessions)
    .fetch_all(&mut *conn)
    .await?;
    match dates.iter().map(|d| d.0).min() {
        Some(first) => Ok(first),
        None => {
            let back = u64::try_from(sessions.saturating_mul(2)).unwrap_or(0);
            as_of
                .checked_sub_days(Days::new(back))
                .ok_or_else(|| WorkerError::python("OverflowError", "date value out of range"))
        }
    }
}

/// `etf_instrument_ids`: every instrument that has **ever** been in the `etf` universe.
pub(crate) async fn etf_instrument_ids(
    conn: &mut PgConnection,
    slug: &str,
) -> Result<BTreeSet<i64>, WorkerError> {
    let rows: Vec<(i64,)> = sqlx::query_as(
        "SELECT DISTINCT m.instrument_id FROM index_member_daily m \
         JOIN index_def d ON d.id = m.index_id WHERE d.slug = $1",
    )
    .bind(slug)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|r| r.0).collect())
}

/// The universe filter VBT-1 and TWT-1 share (`04` §1): the register's names of
/// `instrument_type` in `series_allowed` (and a null series when `keep_null_series`), less the
/// ETF universe and the two narrow ETF patterns (`re.IGNORECASE`, `search`).
pub(crate) struct UniverseRule<'a> {
    /// `data.instrument_type`.
    pub instrument_type: &'a str,
    /// `data.series_allowed`.
    pub series_allowed: &'a [String],
    /// TWT's `keep_null_series` arm (VBT's loader does not carry it).
    pub keep_null_series: bool,
    /// `data.etf_universe_slug`.
    pub etf_universe_slug: &'a str,
    /// `data.etf_name_pattern`.
    pub etf_name_pattern: &'a str,
    /// `data.etf_symbol_pattern`.
    pub etf_symbol_pattern: &'a str,
}

/// The two narrow ETF patterns, `re.IGNORECASE`, matched with `search`.
pub(crate) struct EtfPatterns {
    name: regex::Regex,
    symbol: regex::Regex,
}

impl EtfPatterns {
    pub(crate) fn new(rule: &UniverseRule<'_>) -> Result<Self, WorkerError> {
        let compile = |pattern: &str| {
            RegexBuilder::new(pattern)
                .case_insensitive(true)
                .build()
                .map_err(|e| WorkerError::python("error", e.to_string()))
        };
        Ok(Self {
            name: compile(rule.etf_name_pattern)?,
            symbol: compile(rule.etf_symbol_pattern)?,
        })
    }

    /// `if name and name_pattern.search(name)` or `symbol_pattern.search(symbol)`.
    pub(crate) fn refuses(&self, name: Option<&str>, symbol: &str) -> bool {
        name.is_some_and(|n| !n.is_empty() && self.name.is_match(n)) || self.symbol.is_match(symbol)
    }
}

/// `load_universe`.
pub(crate) async fn load_universe(
    conn: &mut PgConnection,
    rule: &UniverseRule<'_>,
) -> Result<BTreeSet<i64>, WorkerError> {
    let patterns = EtfPatterns::new(rule)?;
    let etfs = etf_instrument_ids(conn, rule.etf_universe_slug).await?;
    let null_arm = if rule.keep_null_series {
        " OR series IS NULL"
    } else {
        ""
    };
    let sql = format!(
        "SELECT id, symbol, name FROM instrument WHERE instrument_type = $1 \
         AND (series = ANY($2){null_arm})"
    );
    let rows: Vec<(i64, String, Option<String>)> = sqlx::query_as(&sql)
        .bind(rule.instrument_type)
        .bind(rule.series_allowed)
        .fetch_all(&mut *conn)
        .await?;
    let mut keep = BTreeSet::new();
    for (id, symbol, name) in rows {
        if etfs.contains(&id) || patterns.refuses(name.as_deref(), &symbol) {
            continue;
        }
        keep.insert(id);
    }
    Ok(keep)
}

/// Days since 1970-01-01 (polars' `Date` physical value).
pub(crate) fn day_number(d: NaiveDate) -> i32 {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap_or_default();
    i32::try_from((d - epoch).num_days()).unwrap_or(i32::MAX)
}

/// A `Date` column from dates.
pub(crate) fn date_column(name: &str, values: Vec<Option<i32>>) -> Result<Column, WorkerError> {
    Ok(Series::new(name.into(), values)
        .cast(&DataType::Date)
        .map_err(|e| pl_err(&e))?
        .into_column())
}

/// A column cast to `dtype`, materialised.
fn series(df: &DataFrame, name: &str, dtype: &DataType) -> Result<Series, WorkerError> {
    let col = df.column(name).map_err(|e| pl_err(&e))?;
    col.as_materialized_series()
        .cast(dtype)
        .map_err(|e| pl_err(&e))
}

/// `name` as nullable floats.
pub(crate) fn floats(df: &DataFrame, name: &str) -> Result<Vec<Option<f64>>, WorkerError> {
    Ok(series(df, name, &DataType::Float64)?
        .f64()
        .map_err(|e| pl_err(&e))?
        .iter()
        .collect())
}

/// `name` as nullable integers.
pub(crate) fn ints(df: &DataFrame, name: &str) -> Result<Vec<Option<i64>>, WorkerError> {
    Ok(series(df, name, &DataType::Int64)?
        .i64()
        .map_err(|e| pl_err(&e))?
        .iter()
        .collect())
}

/// `name` as nullable booleans.
pub(crate) fn bools(df: &DataFrame, name: &str) -> Result<Vec<Option<bool>>, WorkerError> {
    Ok(series(df, name, &DataType::Boolean)?
        .bool()
        .map_err(|e| pl_err(&e))?
        .iter()
        .collect())
}

/// `name` as nullable strings.
pub(crate) fn strings(df: &DataFrame, name: &str) -> Result<Vec<Option<String>>, WorkerError> {
    Ok(series(df, name, &DataType::String)?
        .str()
        .map_err(|e| pl_err(&e))?
        .iter()
        .map(|s| s.map(str::to_owned))
        .collect())
}

/// `name` as nullable day numbers.
pub(crate) fn dates(df: &DataFrame, name: &str) -> Result<Vec<Option<i32>>, WorkerError> {
    Ok(series(df, name, &DataType::Date)?
        .date()
        .map_err(|e| pl_err(&e))?
        .physical()
        .iter()
        .collect())
}

/// `name` as lists of strings (`failed_filters`): `list(row[name] or [])`.
pub(crate) fn string_lists(df: &DataFrame, name: &str) -> Result<Vec<Vec<String>>, WorkerError> {
    let col = df.column(name).map_err(|e| pl_err(&e))?;
    let s = col.as_materialized_series();
    let list = s.list().map_err(|e| pl_err(&e))?;
    let mut out = Vec::with_capacity(list.len());
    for i in 0..list.len() {
        let mut values = Vec::new();
        if let Some(inner) = list.get_as_series(i) {
            let inner = inner.cast(&DataType::String).map_err(|e| pl_err(&e))?;
            for v in inner.str().map_err(|e| pl_err(&e))?.iter() {
                // `list(...)` of a polars list keeps a null as `None`; the column is NOT NULL
                // text[], and no filter letter is ever null, so a null here is a bug either way.
                values.push(
                    v.map(str::to_owned)
                        .ok_or_else(|| value_err(format!("a null entry in {name}")))?,
                );
            }
        }
        out.push(values);
    }
    Ok(out)
}

/// A number as Python holds it after `apply_storage_precision`: a float, an int, or `None`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) enum Num {
    /// `None`.
    Null,
    /// A Python `float`.
    F(f64),
    /// A Python `int`.
    I(i64),
}

impl Num {
    /// `_decimal(value)`: `Decimal(str(value))`, `None` passing through.
    pub(crate) fn decimal(self) -> Result<Option<Decimal>, WorkerError> {
        match self {
            Self::Null => Ok(None),
            Self::I(i) => Ok(Some(Decimal::from(i))),
            Self::F(f) => decimal_from_float_str(f)
                .map(Some)
                .map_err(|e| value_err(e.0)),
        }
    }

    /// `_int(value)`: `int(float(str(value)))` (truncation), `None` passing through.
    pub(crate) fn int(self) -> Result<Option<i64>, WorkerError> {
        match self {
            Self::Null => Ok(None),
            Self::I(i) => Ok(Some(i)),
            Self::F(f) => float_to_int(f).map(Some),
        }
    }
}

/// `int(x)` of a float: truncation toward zero; Python raises on NaN and the infinities.
pub(crate) fn float_to_int(f: f64) -> Result<i64, WorkerError> {
    if f.is_nan() {
        return Err(value_err("cannot convert float NaN to integer"));
    }
    if f.is_infinite() {
        return Err(WorkerError::python(
            "OverflowError",
            "cannot convert float infinity to integer",
        ));
    }
    let t = f.trunc();
    // Python's int is unbounded; every stored figure is a bigint, so out of range is an
    // error at the database in Python and here.
    #[allow(clippy::cast_possible_truncation)]
    if (-9.223_372_036_854_776e18..9.223_372_036_854_776e18).contains(&t) {
        Ok(t as i64)
    } else {
        Err(value_err(format!("{t} is out of range for bigint")))
    }
}

/// A float as `apply_storage_precision` leaves a float column named `name`.
pub(crate) fn store_float(name: &str, value: Option<f64>) -> Num {
    match precision::rule(name) {
        Rule::Places(p) => precision::round_half_up(value, p).map_or(Num::Null, Num::F),
        Rule::Whole => precision::whole(value).map_or(Num::Null, Num::I),
        Rule::Keep => value.map_or(Num::Null, Num::F),
    }
}

/// Column `source` of `df` as Python reads it from a frame that went through
/// `apply_storage_precision` under the name `dest`: a float column takes `dest`'s rule; an
/// integer column is left alone; a `Null` column is all `None`.
pub(crate) fn stored(df: &DataFrame, source: &str, dest: &str) -> Result<Vec<Num>, WorkerError> {
    let col = df.column(source).map_err(|e| pl_err(&e))?;
    match col.dtype() {
        DataType::Float64 | DataType::Float32 => Ok(floats(df, source)?
            .into_iter()
            .map(|v| store_float(dest, v))
            .collect()),
        DataType::Null => Ok(vec![Num::Null; df.height()]),
        DataType::Boolean => Ok(bools(df, source)?
            .into_iter()
            .map(|v| v.map_or(Num::Null, |b| Num::I(i64::from(b))))
            .collect()),
        _ => Ok(ints(df, source)?
            .into_iter()
            .map(|v| v.map_or(Num::Null, Num::I))
            .collect()),
    }
}

/// Column `name` of `df` as Python's row dict holds it **without** precision applied.
pub(crate) fn raw(df: &DataFrame, name: &str) -> Result<Vec<Num>, WorkerError> {
    let col = df.column(name).map_err(|e| pl_err(&e))?;
    match col.dtype() {
        DataType::Float64 | DataType::Float32 => Ok(floats(df, name)?
            .into_iter()
            .map(|v| v.map_or(Num::Null, Num::F))
            .collect()),
        DataType::Null => Ok(vec![Num::Null; df.height()]),
        _ => Ok(ints(df, name)?
            .into_iter()
            .map(|v| v.map_or(Num::Null, Num::I))
            .collect()),
    }
}

/// One bar row as the VBT and TWT loaders read it: every numeric as `::float8` (Postgres's
/// `numeric → float8` is correctly rounded, which is Python's `float(Decimal)`).
type RawBar = (
    i64,
    String,
    NaiveDate,
    f64,
    f64,
    f64,
    f64,
    f64,
    f64,
    Option<f64>,
    f64,
);

/// `load_vbt_bars` / `load_twt_bars`: the adjusted bars of `universe` in `[start, as_of]`, the
/// circuit band multiplied into the adjusted space. `with_is_etf` adds TWT's all-false column.
pub(crate) async fn load_raw_bars(
    conn: &mut PgConnection,
    start: NaiveDate,
    as_of: NaiveDate,
    universe: &BTreeSet<i64>,
    with_is_etf: bool,
) -> Result<DataFrame, WorkerError> {
    let ids: Vec<i64> = universe.iter().copied().collect();
    let records: Vec<RawBar> = if ids.is_empty() {
        Vec::new()
    } else {
        sqlx::query_as(
            "SELECT o.instrument_id, i.symbol, o.date, o.open::float8, o.high::float8, \
             o.low::float8, o.close::float8, o.close_raw::float8, o.volume::float8, \
             o.upper_circuit::float8, o.adj_factor::float8 \
             FROM ohlcv_daily o JOIN instrument i ON i.id = o.instrument_id \
             WHERE o.date >= $1 AND o.date <= $2 AND o.instrument_id = ANY($3) \
             ORDER BY o.instrument_id, o.date",
        )
        .bind(start)
        .bind(as_of)
        .bind(&ids)
        .fetch_all(&mut *conn)
        .await?
    };
    let n = records.len();
    let mut id = Vec::with_capacity(n);
    let mut symbol = Vec::with_capacity(n);
    let mut date = Vec::with_capacity(n);
    let (mut open, mut high, mut low, mut close) = (
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
    );
    let (mut close_raw, mut volume, mut circuit, mut factor_col) = (
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
    );
    for r in records {
        // `float(adj_factor)`; the column is NOT NULL, so Python's `else 1.0` never runs.
        let factor = r.10;
        id.push(r.0);
        symbol.push(r.1);
        date.push(Some(day_number(r.2)));
        open.push(Some(r.3));
        high.push(Some(r.4));
        low.push(Some(r.5));
        close.push(Some(r.6));
        close_raw.push(Some(r.7));
        volume.push(Some(r.8));
        circuit.push(r.9.map(|c| c * factor));
        factor_col.push(Some(factor));
    }
    let mut columns = vec![
        Series::new("instrument_id".into(), id).into_column(),
        Series::new("symbol".into(), symbol).into_column(),
        date_column("date", date)?,
        Series::new("open".into(), open).into_column(),
        Series::new("high".into(), high).into_column(),
        Series::new("low".into(), low).into_column(),
        Series::new("close".into(), close).into_column(),
        Series::new("close_raw".into(), close_raw).into_column(),
        Series::new("volume".into(), volume).into_column(),
        Series::new("upper_circuit".into(), circuit).into_column(),
        Series::new("adj_factor".into(), factor_col).into_column(),
    ];
    if with_is_etf {
        columns.push(Series::new("is_etf".into(), vec![Some(false); n]).into_column());
    }
    DataFrame::new(n, columns).map_err(|e| pl_err(&e))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_int_of_a_float_truncates() {
        assert_eq!(float_to_int(2.9).ok(), Some(2));
        assert_eq!(float_to_int(-2.9).ok(), Some(-2));
        assert!(float_to_int(f64::NAN).is_err());
        assert!(float_to_int(f64::INFINITY).is_err());
    }

    #[test]
    fn decimal_of_a_float_is_its_repr() {
        assert_eq!(
            Num::F(137.35)
                .decimal()
                .ok()
                .flatten()
                .map(|d| d.to_string()),
            Some("137.35".to_owned())
        );
        assert_eq!(
            Num::F(1.0).decimal().ok().flatten().map(|d| d.to_string()),
            Some("1.0".to_owned())
        );
        assert_eq!(Num::Null.decimal().ok().flatten(), None);
        assert_eq!(Num::I(7).int().ok().flatten(), Some(7));
    }

    #[test]
    fn store_float_follows_the_table() {
        assert_eq!(store_float("limit_price", Some(1.005_1)), Num::F(1.01));
        assert_eq!(store_float("rank_key", Some(2.5)), Num::I(3));
        assert_eq!(store_float("open", Some(1.234_56)), Num::F(1.234_56));
        assert_eq!(store_float("close", None), Num::Null);
    }

    #[test]
    fn the_etf_patterns_catch_a_stray_fund_and_keep_a_company() {
        let config = baskfy_core::vbt::config::VbtConfig::default();
        let rule = UniverseRule {
            instrument_type: &config.data.instrument_type,
            series_allowed: &config.data.series_allowed,
            keep_null_series: false,
            etf_universe_slug: &config.data.etf_universe_slug,
            etf_name_pattern: &config.data.etf_name_pattern,
            etf_symbol_pattern: &config.data.etf_symbol_pattern,
        };
        let Ok(p) = EtfPatterns::new(&rule) else {
            return;
        };
        assert!(p.refuses(None, "SOMETHINGETF"));
        assert!(p.refuses(None, "goldbees"));
        assert!(p.refuses(Some("Nippon India ETF Nifty 50"), "NIFTY1"));
        assert!(!p.refuses(Some("Goldiam International"), "GOLDIAM"));
        assert!(!p.refuses(Some(""), "ACMECO"));
        assert!(!p.refuses(Some("PETFOOD Ltd"), "PETFOOD"));
    }

    #[test]
    fn day_numbers_round_trip() {
        let d = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap_or_default();
        assert_eq!(day_number(d), 20_755);
    }
}
