//! Step 7 — `compute_factors`. Ports `baskfy_worker.tasks.factors.run_compute_factors` and the
//! I/O half of `baskfy_worker.engine` (`load_history`, `load_index_levels`, `load_fundamentals`,
//! `PolarsFactorEngine.run`, `_apply_true_ath`). The arithmetic is `baskfy_core::factors`
//! (wave 2, graded by goldens); this module reads the bars, runs it and writes `factor_daily`.
//!
//! Exactly as Python:
//!
//! * the bar history is a 3-calendar-year lookback (`DEFAULT_LOOKBACK_DAYS = 365 * 3`), numbers
//!   as `float(str(Decimal))` (here: PostgreSQL's numeric text, parsed correctly rounded), read
//!   through a server-side cursor 50,000 rows at a time (`STREAM_PARTITION`);
//! * the all-time high comes from the **whole** table, not the lookback, and replaces the
//!   engine's lookback ATH with `max_horizontal`, `away_high_ath` recomputed with polars'
//!   half-to-even `round(2)`;
//! * fundamentals (`marketcap_cr`, `pe`) replace the engine's columns by a left join when the
//!   date has any;
//! * the universe masks and the top-decile beta/volatility masks are computed **per universe over
//!   every member** from `index_member_daily` (docs/06 §"Step 4");
//! * the upsert writes exactly the frame's columns that `factor_daily` has (a column the frame
//!   lacks is neither inserted nor updated), each float bound as asyncpg binds it
//!   (`Decimal(float)`, rounded to the column by PostgreSQL — [`float_to_numeric`]);
//! * then `mom_pctile` and `rank_persist_20` ([`super::ranking`]) and the mask assertion.

use std::collections::{BTreeMap, HashMap};

use baskfy_core::factors::{FactorConfig, TOP_RISK_FLAG_PERCENTILE, compute_factors};
use baskfy_db::OrderedJson;
use chrono::{Days, NaiveDate};
use polars::prelude::{Column, DataFrame, DataType, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row};

use super::numeric::{float_to_int, float_to_numeric, polars_round};
use super::{core_err, pl_err};
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

/// `engine.DEFAULT_LOOKBACK_DAYS`.
const DEFAULT_LOOKBACK_DAYS: u64 = 365 * 3;
/// `engine.STREAM_PARTITION`.
const STREAM_PARTITION: i64 = 50_000;
/// `engine.BENCHMARK_SLUG`: beta's and `resid_ret_12m`'s series.
const BENCHMARK_SLUG: &str = "nifty-50";
/// `engine.MARKET_BENCHMARK_SLUG`: the C1 benchmark-relative columns'.
const MARKET_BENCHMARK_SLUG: &str = "nifty-500";
/// `seed_data.NSE_EXCHANGE_ID`.
pub(crate) const NSE_EXCHANGE_ID: i16 = 1;
/// `tasks.factors.MAX_REPORTED_MISMATCHES`.
const MAX_REPORTED_MISMATCHES: usize = 20;
/// `baskfy_core.universes.UNIVERSES`' ids: the mask bit is `index_id - 1`. A dashboard-only
/// index above this range has no bit.
const UNIVERSE_IDS: std::ops::RangeInclusive<i64> = 1..=15;
/// The masks computed here, never by the engine (`_WRITABLE_COLUMNS` excludes them).
const MASK_COLUMNS: [&str; 3] = ["universe_mask", "top_beta_mask", "top_volatility_mask"];

/// `Universe.mask_value` for an `index_def.id`, `None` for an id that is not a universe.
pub(crate) fn mask_value(index_id: i64) -> Option<i64> {
    UNIVERSE_IDS
        .contains(&index_id)
        .then(|| 1_i64 << (index_id - 1))
}

/// Days since 1970-01-01, polars' `Date` physical value.
pub(crate) fn day_number(d: NaiveDate) -> i32 {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap_or_default();
    i32::try_from((d - epoch).num_days()).unwrap_or(i32::MAX)
}

/// A `Date` column.
pub(crate) fn date_column(name: &str, days: Vec<i32>) -> Result<Column, WorkerError> {
    Ok(Series::new(name.into(), days)
        .cast(&DataType::Date)
        .map_err(|e| pl_err(&e))?
        .into_column())
}

/// `float(str(value))` of a numeric's text (NULL stays NULL).
pub(crate) fn parse_float(text: Option<&str>) -> Result<Option<f64>, WorkerError> {
    text.map(|t| {
        t.parse::<f64>().map_err(|e| {
            WorkerError::python(
                "ValueError",
                format!("could not convert {t:?} to float: {e}"),
            )
        })
    })
    .transpose()
}

/// A column as nullable floats (cast like polars' `cast(Float64)`).
pub(crate) fn floats(df: &DataFrame, name: &str) -> Result<Vec<Option<f64>>, WorkerError> {
    let s = df
        .column(name)
        .map_err(|e| pl_err(&e))?
        .as_materialized_series()
        .cast(&DataType::Float64)
        .map_err(|e| pl_err(&e))?;
    Ok(s.f64().map_err(|e| pl_err(&e))?.iter().collect())
}

/// A column as nullable integers.
pub(crate) fn ints(df: &DataFrame, name: &str) -> Result<Vec<Option<i64>>, WorkerError> {
    let s = df
        .column(name)
        .map_err(|e| pl_err(&e))?
        .as_materialized_series()
        .cast(&DataType::Int64)
        .map_err(|e| pl_err(&e))?;
    Ok(s.i64().map_err(|e| pl_err(&e))?.iter().collect())
}

/// A column as nullable strings.
pub(crate) fn strings(df: &DataFrame, name: &str) -> Result<Vec<Option<String>>, WorkerError> {
    let s = df
        .column(name)
        .map_err(|e| pl_err(&e))?
        .as_materialized_series()
        .cast(&DataType::String)
        .map_err(|e| pl_err(&e))?;
    Ok(s.str()
        .map_err(|e| pl_err(&e))?
        .iter()
        .map(|v| v.map(str::to_owned))
        .collect())
}

/// `LoadedHistory`: everything `baskfy_core.factors` needs for one as-of date.
pub(crate) struct LoadedHistory {
    /// The bar frame (`BAR_SCHEMA`).
    pub bars: DataFrame,
    /// NIFTY 50 as `(date, close)`, NULL levels dropped.
    pub benchmark: DataFrame,
    /// The NSE calendar inside the lookback.
    pub trading_days: Vec<NaiveDate>,
    /// The true all-time high per instrument as at `as_of`.
    pub all_time_highs: BTreeMap<i64, f64>,
    /// NIFTY 500 as `(date, level)`, NULL levels kept.
    pub market_benchmark: DataFrame,
}

/// `load_history(session, as_of)`.
pub(crate) async fn load_history(
    conn: &mut PgConnection,
    as_of: NaiveDate,
) -> Result<LoadedHistory, WorkerError> {
    let start = as_of
        .checked_sub_days(Days::new(DEFAULT_LOOKBACK_DAYS))
        .ok_or_else(|| WorkerError::python("OverflowError", "date value out of range"))?;

    // Streamed through a server-side cursor (the chain is one transaction), STREAM_PARTITION
    // rows at a time; only the typed columns accumulate.
    sqlx::query(
        "DECLARE baskfy_compute_factors_bars NO SCROLL CURSOR FOR \
         SELECT ohlcv_daily.instrument_id, ohlcv_daily.date, ohlcv_daily.close::text, \
         ohlcv_daily.close_raw::text, ohlcv_daily.high::text, ohlcv_daily.low::text, \
         ohlcv_daily.volume_raw::text, ohlcv_daily.turnover::text, \
         ohlcv_daily.upper_circuit::text, ohlcv_daily.lower_circuit::text, instrument.series \
         FROM ohlcv_daily JOIN instrument ON instrument.id = ohlcv_daily.instrument_id \
         WHERE ohlcv_daily.date >= $1 AND ohlcv_daily.date <= $2 \
         ORDER BY ohlcv_daily.instrument_id, ohlcv_daily.date",
    )
    .bind(start)
    .bind(as_of)
    .execute(&mut *conn)
    .await?;
    let mut ids: Vec<i64> = Vec::new();
    let mut dates: Vec<i32> = Vec::new();
    let mut numbers: [Vec<Option<f64>>; 8] = Default::default();
    let mut series: Vec<Option<String>> = Vec::new();
    loop {
        let rows = sqlx::query(&format!(
            "FETCH FORWARD {STREAM_PARTITION} FROM baskfy_compute_factors_bars"
        ))
        .fetch_all(&mut *conn)
        .await?;
        if rows.is_empty() {
            break;
        }
        for row in &rows {
            ids.push(row.try_get(0)?);
            dates.push(day_number(row.try_get::<NaiveDate, _>(1)?));
            for (k, column) in numbers.iter_mut().enumerate() {
                let text: Option<String> = row.try_get(k + 2)?;
                column.push(parse_float(text.as_deref())?);
            }
            series.push(row.try_get(10)?);
        }
    }
    sqlx::query("CLOSE baskfy_compute_factors_bars")
        .execute(&mut *conn)
        .await?;
    let n = ids.len();
    let [
        close,
        close_raw,
        high,
        low,
        volume_raw,
        turnover,
        upper,
        lower,
    ] = numbers;
    let bars = DataFrame::new(
        n,
        vec![
            Series::new("instrument_id".into(), ids).into_column(),
            date_column("date", dates)?,
            Series::new("close".into(), close).into_column(),
            Series::new("close_raw".into(), close_raw).into_column(),
            Series::new("high".into(), high).into_column(),
            Series::new("low".into(), low).into_column(),
            Series::new("volume_raw".into(), volume_raw).into_column(),
            Series::new("turnover".into(), turnover).into_column(),
            Series::new("upper_circuit".into(), upper).into_column(),
            Series::new("lower_circuit".into(), lower).into_column(),
            Series::new("series".into(), series).into_column(),
        ],
    )
    .map_err(|e| pl_err(&e))?;

    let calendar: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trading_day.date FROM trading_day WHERE trading_day.exchange_id = $1 \
         AND trading_day.is_trading_day IS true AND trading_day.date >= $2 \
         AND trading_day.date <= $3 ORDER BY trading_day.date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let trading_days: Vec<NaiveDate> = calendar.into_iter().map(|r| r.0).collect();

    // Beta has always been handed NIFTY 50 without its NULL-level days; C1 keeps them.
    let nifty_50 = load_index_levels(conn, BENCHMARK_SLUG, start, as_of).await?;
    let kept: Vec<&(NaiveDate, Option<f64>)> = nifty_50.iter().filter(|r| r.1.is_some()).collect();
    let benchmark = DataFrame::new(
        kept.len(),
        vec![
            date_column("date", kept.iter().map(|r| day_number(r.0)).collect())?,
            Series::new(
                "close".into(),
                kept.iter().map(|r| r.1).collect::<Vec<Option<f64>>>(),
            )
            .into_column(),
        ],
    )
    .map_err(|e| pl_err(&e))?;
    let nifty_500 = load_index_levels(conn, MARKET_BENCHMARK_SLUG, start, as_of).await?;
    let market_benchmark = DataFrame::new(
        nifty_500.len(),
        vec![
            date_column("date", nifty_500.iter().map(|r| day_number(r.0)).collect())?,
            Series::new(
                "level".into(),
                nifty_500.iter().map(|r| r.1).collect::<Vec<Option<f64>>>(),
            )
            .into_column(),
        ],
    )
    .map_err(|e| pl_err(&e))?;

    // The true all-time high, from the whole table rather than the lookback.
    let ath_rows = sqlx::query(
        "SELECT ohlcv_daily.instrument_id, max(ohlcv_daily.close)::text AS max_1 \
         FROM ohlcv_daily WHERE ohlcv_daily.date <= $1 GROUP BY ohlcv_daily.instrument_id",
    )
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut all_time_highs = BTreeMap::new();
    for row in ath_rows {
        let text: Option<String> = row.try_get(1)?;
        if let Some(v) = parse_float(text.as_deref())? {
            all_time_highs.insert(row.try_get::<i64, _>(0)?, v);
        }
    }

    Ok(LoadedHistory {
        bars,
        benchmark,
        trading_days,
        all_time_highs,
        market_benchmark,
    })
}

/// `load_index_levels(session, slug, start, as_of)`: `(date, level)`, ascending.
async fn load_index_levels(
    conn: &mut PgConnection,
    slug: &str,
    start: NaiveDate,
    as_of: NaiveDate,
) -> Result<Vec<(NaiveDate, Option<f64>)>, WorkerError> {
    let rows = sqlx::query(
        "SELECT index_snapshot_daily.date, index_snapshot_daily.level::text \
         FROM index_snapshot_daily JOIN index_def ON index_def.id = index_snapshot_daily.index_id \
         WHERE index_def.slug = $1 AND index_snapshot_daily.date >= $2 \
         AND index_snapshot_daily.date <= $3 ORDER BY index_snapshot_daily.date",
    )
    .bind(slug)
    .bind(start)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let text: Option<String> = row.try_get(1)?;
        out.push((row.try_get(0)?, parse_float(text.as_deref())?));
    }
    Ok(out)
}

/// `_apply_true_ath`: the full-history ATH replaces the lookback one (`max_horizontal`), and
/// `away_high_ath` is recomputed as `((close / high_ath - 1) * 100).round(2)` (polars, half to
/// even), NULL where `high_ath` is NULL or 0.
pub(crate) fn apply_true_ath(
    frame: DataFrame,
    all_time_highs: &BTreeMap<i64, f64>,
) -> Result<DataFrame, WorkerError> {
    if frame.height() == 0 || all_time_highs.is_empty() {
        return Ok(frame);
    }
    let ids = ints(&frame, "instrument_id")?;
    let high = floats(&frame, "high_ath")?;
    let close = floats(&frame, "close")?;
    let mut new_high = Vec::with_capacity(ids.len());
    let mut away = Vec::with_capacity(ids.len());
    for ((id, h), c) in ids.iter().zip(&high).zip(&close) {
        let truth = id.and_then(|i| all_time_highs.get(&i).copied());
        // `max_horizontal` skips nulls (polars' max: a NaN never arises from stored closes).
        let merged = match (*h, truth) {
            (Some(a), Some(b)) => Some(if b > a { b } else { a }),
            (a, b) => a.or(b),
        };
        new_high.push(merged);
        // `high_ath.is_null() | (high_ath == 0)` -> NULL; a NULL close propagates.
        #[allow(clippy::float_cmp)]
        let zero = merged.is_some_and(|m| m == 0.0);
        away.push(match (merged, *c) {
            (Some(m), Some(c)) if !zero => Some(polars_round((c / m - 1.0) * 100.0, 2)),
            _ => None,
        });
    }
    let mut frame = frame;
    frame
        .with_column(Series::new("high_ath".into(), new_high).into_column())
        .map_err(|e| pl_err(&e))?;
    frame
        .with_column(Series::new("away_high_ath".into(), away).into_column())
        .map_err(|e| pl_err(&e))?;
    Ok(frame)
}

/// `load_fundamentals(session, as_of)`: `instrument_id -> (marketcap_cr, pe)` as floats.
async fn load_fundamentals(
    conn: &mut PgConnection,
    as_of: NaiveDate,
) -> Result<HashMap<i64, (Option<f64>, Option<f64>)>, WorkerError> {
    let rows = sqlx::query(
        "SELECT fundamental_daily.instrument_id, fundamental_daily.marketcap_cr, \
         fundamental_daily.pe::text FROM fundamental_daily WHERE fundamental_daily.date = $1",
    )
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut out = HashMap::with_capacity(rows.len());
    for row in rows {
        let marketcap: Option<i64> = row.try_get(1)?;
        let pe: Option<String> = row.try_get(2)?;
        #[allow(clippy::cast_precision_loss)]
        let marketcap = marketcap.map(|m| m as f64);
        out.insert(
            row.try_get::<i64, _>(0)?,
            (marketcap, parse_float(pe.as_deref())?),
        );
    }
    Ok(out)
}

/// `_attach_fundamentals`: a date with fundamentals replaces `marketcap_cr` and `pe` by a left
/// join; a date without passes the frame through.
async fn attach_fundamentals(
    conn: &mut PgConnection,
    frame: DataFrame,
    on: NaiveDate,
) -> Result<DataFrame, WorkerError> {
    let fundamentals = load_fundamentals(conn, on).await?;
    if fundamentals.is_empty() {
        return Ok(frame);
    }
    let ids = ints(&frame, "instrument_id")?;
    let mut marketcap = Vec::with_capacity(ids.len());
    let mut pe = Vec::with_capacity(ids.len());
    for id in &ids {
        let found = id.and_then(|i| fundamentals.get(&i).copied());
        marketcap.push(found.and_then(|f| f.0));
        pe.push(found.and_then(|f| f.1));
    }
    let mut frame = frame;
    for name in ["marketcap_cr", "pe"] {
        if frame.get_column_index(name).is_some() {
            frame = frame.drop(name).map_err(|e| pl_err(&e))?;
        }
    }
    frame
        .with_column(Series::new("marketcap_cr".into(), marketcap).into_column())
        .map_err(|e| pl_err(&e))?;
    frame
        .with_column(Series::new("pe".into(), pe).into_column())
        .map_err(|e| pl_err(&e))?;
    Ok(frame)
}

/// `index_member_daily` for `on` as `(index_id, instrument_id)`, in the order PostgreSQL returns
/// them (Python iterates the same unordered result; it only matters to a tie at a risk cut).
async fn members(
    conn: &mut PgConnection,
    on: NaiveDate,
    risk_order: bool,
) -> Result<Vec<(i64, i64)>, WorkerError> {
    // The two Python queries select the columns in different orders; kept, so each plan is the
    // one Python's statement gets.
    let sql = if risk_order {
        "SELECT index_member_daily.index_id, index_member_daily.instrument_id \
         FROM index_member_daily WHERE index_member_daily.date = $1"
    } else {
        "SELECT index_member_daily.instrument_id, index_member_daily.index_id \
         FROM index_member_daily WHERE index_member_daily.date = $1"
    };
    let rows = sqlx::query(sql).bind(on).fetch_all(&mut *conn).await?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        if risk_order {
            let index_id: i16 = row.try_get(0)?;
            out.push((i64::from(index_id), row.try_get::<i64, _>(1)?));
        } else {
            let index_id: i16 = row.try_get(1)?;
            out.push((i64::from(index_id), row.try_get::<i64, _>(0)?));
        }
    }
    Ok(out)
}

/// `universe_masks(session, on)`: one bitmask per instrument from `index_member_daily`.
pub(crate) async fn universe_masks(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<HashMap<i64, i64>, WorkerError> {
    let mut masks = HashMap::new();
    for (index_id, instrument_id) in members(conn, on, false).await? {
        if let Some(bit) = mask_value(index_id) {
            *masks.entry(instrument_id).or_insert(0) |= bit;
        }
    }
    Ok(masks)
}

/// `risk_masks`' cut for one universe: the top `max(1, round(n * 0.10))` (Python's round, half
/// to even) of `ranked` by value, descending, ties in input order (Python's stable sort).
pub(crate) fn top_decile(ranked: &mut [(i64, f64)]) -> &[(i64, f64)] {
    ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    #[allow(clippy::cast_precision_loss)]
    let raw = (ranked.len() as f64 * TOP_RISK_FLAG_PERCENTILE).round_ties_even();
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let cut = (raw as usize).max(1).min(ranked.len());
    &ranked[..cut]
}

/// `RiskMasks`: the top-beta and top-volatility bitmasks.
pub(crate) struct RiskMasks {
    beta: HashMap<i64, i64>,
    volatility: HashMap<i64, i64>,
}

/// `risk_masks(session, frame, on)`: docs/06 §"Step 4", per universe over every member.
async fn risk_masks(
    conn: &mut PgConnection,
    frame: &DataFrame,
    on: NaiveDate,
) -> Result<RiskMasks, WorkerError> {
    let mut masks = RiskMasks {
        beta: HashMap::new(),
        volatility: HashMap::new(),
    };
    if frame.height() == 0 {
        return Ok(masks);
    }
    let ids = ints(frame, "instrument_id")?;
    let beta = floats(frame, "beta_12m")?;
    let vol = floats(frame, "vol_12m")?;
    let mut values: HashMap<i64, (Option<f64>, Option<f64>)> = HashMap::new();
    for ((id, b), v) in ids.iter().zip(&beta).zip(&vol) {
        if let Some(id) = id {
            values.insert(*id, (*b, *v));
        }
    }
    // `by_universe` in first-seen order (a dict), members in query order.
    let mut by_universe: Vec<(i64, Vec<i64>)> = Vec::new();
    for (index_id, instrument_id) in members(conn, on, true).await? {
        match by_universe.iter_mut().find(|(i, _)| *i == index_id) {
            Some((_, list)) => list.push(instrument_id),
            None => by_universe.push((index_id, vec![instrument_id])),
        }
    }
    for (index_id, instrument_ids) in &by_universe {
        let Some(bit) = mask_value(*index_id) else {
            continue;
        };
        for slot in 0..2 {
            let mut ranked: Vec<(i64, f64)> = instrument_ids
                .iter()
                .filter_map(|id| {
                    let pair = values.get(id)?;
                    let value = if slot == 0 { pair.0 } else { pair.1 };
                    // `float(pair[1] or 0)`: a stored value is never NaN (precision NULLs it).
                    value.map(|v| (*id, v))
                })
                .collect();
            if ranked.is_empty() {
                continue;
            }
            let target = if slot == 0 {
                &mut masks.beta
            } else {
                &mut masks.volatility
            };
            for (id, _) in top_decile(&mut ranked) {
                *target.entry(*id).or_insert(0) |= bit;
            }
        }
    }
    Ok(masks)
}

/// How a `factor_daily` column stores a value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Kind {
    Numeric(u32),
    Int,
    Text,
    Date,
}

/// `_WRITABLE_COLUMNS`, from the table itself (the ORM model is the migration's), in column
/// order, minus the three masks.
pub(crate) async fn writable_columns(conn: &mut PgConnection) -> Result<Vec<(String, Kind)>, WorkerError> {
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT a.attname::text, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a \
         WHERE a.attrelid = 'factor_daily'::regclass AND a.attnum > 0 AND NOT a.attisdropped \
         ORDER BY a.attnum",
    )
    .fetch_all(&mut *conn)
    .await?;
    let mut out = Vec::with_capacity(rows.len());
    for (name, ty) in rows {
        if MASK_COLUMNS.contains(&name.as_str()) {
            continue;
        }
        let kind = if let Some(rest) = ty.strip_prefix("numeric(") {
            let scale = rest
                .trim_end_matches(')')
                .split(',')
                .nth(1)
                .and_then(|s| s.trim().parse::<u32>().ok())
                .ok_or_else(|| {
                    WorkerError::python("TypeError", format!("factor_daily.{name} is {ty}"))
                })?;
            Kind::Numeric(scale)
        } else if matches!(ty.as_str(), "bigint" | "integer" | "smallint") {
            Kind::Int
        } else if ty.starts_with("character varying") || ty == "text" {
            Kind::Text
        } else if ty == "date" {
            Kind::Date
        } else {
            return Err(WorkerError::python(
                "TypeError",
                format!("factor_daily.{name} has a type the writer does not bind: {ty}"),
            ));
        };
        out.push((name, kind));
    }
    Ok(out)
}

/// One bound column of the upsert.
pub(crate) enum Bound {
    Numeric(Vec<Option<Decimal>>),
    Int(Vec<Option<i64>>),
    Text(Vec<Option<String>>),
}

/// A frame column bound as Python binds `_clean(value)` for `kind`.
pub(crate) fn bind_column(df: &DataFrame, name: &str, kind: Kind) -> Result<Bound, WorkerError> {
    let dtype = df.column(name).map_err(|e| pl_err(&e))?.dtype().clone();
    let is_float = matches!(dtype, DataType::Float64 | DataType::Float32);
    let is_int = dtype.is_integer() || matches!(dtype, DataType::Boolean);
    let is_null = matches!(dtype, DataType::Null);
    let wrong = || {
        WorkerError::python(
            "DataError",
            format!("factor_daily.{name}: cannot bind a {dtype} column as {kind:?}"),
        )
    };
    match kind {
        Kind::Numeric(scale) => {
            if is_float || is_null {
                let values = floats(df, name)?;
                Ok(Bound::Numeric(
                    values
                        .into_iter()
                        .map(|v| match v {
                            Some(x) => float_to_numeric(x, scale),
                            None => Ok(None),
                        })
                        .collect::<Result<_, _>>()?,
                ))
            } else if is_int {
                Ok(Bound::Numeric(
                    ints(df, name)?
                        .into_iter()
                        .map(|v| v.map(Decimal::from))
                        .collect(),
                ))
            } else {
                Err(wrong())
            }
        }
        Kind::Int => {
            if is_float || is_null {
                Ok(Bound::Int(
                    floats(df, name)?
                        .into_iter()
                        .map(|v| match v {
                            Some(x) => float_to_int(x),
                            None => Ok(None),
                        })
                        .collect::<Result<_, _>>()?,
                ))
            } else if is_int {
                Ok(Bound::Int(ints(df, name)?))
            } else {
                Err(wrong())
            }
        }
        Kind::Text => {
            if matches!(dtype, DataType::String) || is_null {
                Ok(Bound::Text(strings(df, name)?))
            } else {
                Err(wrong())
            }
        }
        Kind::Date => Err(wrong()),
    }
}

/// The upsert: `INSERT ... ON CONFLICT (instrument_id, date) DO UPDATE` of every column the
/// payload carries, from one `UNNEST` (Python chunks by the bind-parameter cap; one statement is
/// the same rows). Returns the rows written.
async fn upsert(
    conn: &mut PgConnection,
    frame: &DataFrame,
    on: NaiveDate,
    masks: &HashMap<i64, i64>,
    risk: &RiskMasks,
) -> Result<i64, WorkerError> {
    let ids: Vec<i64> = ints(frame, "instrument_id")?
        .into_iter()
        .map(|v| {
            v.ok_or_else(|| {
                WorkerError::python(
                    "TypeError",
                    "int() argument must be a string, a bytes-like object or a real number, \
                     not 'NoneType'",
                )
            })
        })
        .collect::<Result<_, _>>()?;
    if ids.is_empty() {
        return Ok(0);
    }
    let mut names: Vec<String> = vec!["instrument_id".to_owned(), "date".to_owned()];
    let mut casts: Vec<&'static str> = vec!["bigint[]", "date[]"];
    let mut bound: Vec<Bound> = Vec::new();
    for (name, kind) in writable_columns(conn).await? {
        if name == "instrument_id" || name == "date" {
            continue;
        }
        if frame.get_column_index(&name).is_none() {
            continue;
        }
        let column = bind_column(frame, &name, kind)?;
        casts.push(match column {
            Bound::Numeric(_) => "numeric[]",
            Bound::Int(_) => "bigint[]",
            Bound::Text(_) => "text[]",
        });
        names.push(name);
        bound.push(column);
    }
    for name in MASK_COLUMNS {
        names.push(name.to_owned());
        casts.push("bigint[]");
    }
    let params: Vec<String> = casts
        .iter()
        .enumerate()
        .map(|(i, cast)| format!("${}::{cast}", i + 1))
        .collect();
    let updates: Vec<String> = names
        .iter()
        .skip(2)
        .map(|n| format!("{n} = excluded.{n}"))
        .collect();
    let sql = format!(
        "INSERT INTO factor_daily ({}) SELECT * FROM UNNEST({}) \
         ON CONFLICT (instrument_id, date) DO UPDATE SET {}",
        names.join(", "),
        params.join(", "),
        updates.join(", ")
    );
    let universe: Vec<i64> = ids
        .iter()
        .map(|i| masks.get(i).copied().unwrap_or(0))
        .collect();
    let beta: Vec<i64> = ids
        .iter()
        .map(|i| risk.beta.get(i).copied().unwrap_or(0))
        .collect();
    let volatility: Vec<i64> = ids
        .iter()
        .map(|i| risk.volatility.get(i).copied().unwrap_or(0))
        .collect();
    let mut query = sqlx::query(&sql)
        .bind(ids.clone())
        .bind(vec![on; ids.len()]);
    for column in bound {
        query = match column {
            Bound::Numeric(v) => query.bind(v),
            Bound::Int(v) => query.bind(v),
            Bound::Text(v) => query.bind(v),
        };
    }
    query
        .bind(universe)
        .bind(beta)
        .bind(volatility)
        .execute(&mut *conn)
        .await?;
    i64::try_from(ids.len()).map_err(|_| WorkerError::python("OverflowError", "too many rows"))
}

/// `assert_mask_matches_membership`: disagreements between the stored `universe_mask` and
/// `index_member_daily`, at most 20.
async fn assert_mask_matches_membership(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<Vec<String>, WorkerError> {
    let expected = universe_masks(conn, on).await?;
    let rows: Vec<(i64, i32)> = sqlx::query_as(
        "SELECT factor_daily.instrument_id, factor_daily.universe_mask FROM factor_daily \
         WHERE factor_daily.date = $1",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    let mut problems = Vec::new();
    for (instrument_id, mask) in rows {
        let want = expected.get(&instrument_id).copied().unwrap_or(0);
        if i64::from(mask) != want {
            problems.push(format!(
                "instrument {instrument_id}: mask {mask} != membership {want}"
            ));
        }
        if problems.len() >= MAX_REPORTED_MISMATCHES {
            break;
        }
    }
    Ok(problems)
}

/// `_turnover_sources(frame)`: rows per `turnover_source` (`str(None)` for a null).
fn turnover_sources(frame: &DataFrame) -> Result<OrderedJson, WorkerError> {
    if frame.get_column_index("turnover_source").is_none() {
        return Ok(OrderedJson::Object(Vec::new()));
    }
    let mut counts: Vec<(String, i64)> = Vec::new();
    for value in strings(frame, "turnover_source")? {
        let key = value.unwrap_or_else(|| "None".to_owned());
        match counts.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 += 1,
            None => counts.push((key, 1)),
        }
    }
    Ok(OrderedJson::Object(
        counts
            .into_iter()
            .map(|(k, v)| (k, OrderedJson::Int(v)))
            .collect(),
    ))
}

/// `run_compute_factors(session, outcome, on, PolarsFactorEngine())`; returns the rows written.
pub(crate) async fn run(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let history = load_history(conn, on).await?;
    let on_days = day_number(on);
    let bar_dates = history
        .bars
        .column("date")
        .map_err(|e| pl_err(&e))?
        .as_materialized_series()
        .cast(&DataType::Int32)
        .map_err(|e| pl_err(&e))?;
    let rows_in = bar_dates
        .i32()
        .map_err(|e| pl_err(&e))?
        .iter()
        .filter(|d| *d == Some(on_days))
        .count();
    outcome.rows_in = i64::try_from(rows_in).unwrap_or(i64::MAX);
    if rows_in == 0 {
        outcome.note("engine", pyjson::s("polars"));
        outcome.note("reason", pyjson::s("no bars for this date"));
        return Ok(0);
    }

    // PolarsFactorEngine.run: compute_factors(bars, as_of, days, benchmark, config,
    // market_benchmark=...), then the true ATH.
    let result = compute_factors(
        &history.bars,
        on,
        &history.trading_days,
        Some(&history.benchmark),
        &FactorConfig::default(),
        Some(&history.market_benchmark),
    )
    .map_err(core_err)?;
    let window_lengths = result.window_lengths();
    let frame = apply_true_ath(result.frame, &history.all_time_highs)?;
    drop(history);
    let frame = attach_fundamentals(conn, frame, on).await?;

    let masks = universe_masks(conn, on).await?;
    let risk = risk_masks(conn, &frame, on).await?;
    let written = upsert(conn, &frame, on, &masks, &risk).await?;

    let (mom_rows, persist_rows) = super::ranking::run_rank_columns(conn, on).await?;
    let mismatches = assert_mask_matches_membership(conn, on).await?;

    outcome.rows_out = written;
    outcome.note("engine", pyjson::s("polars"));
    outcome.note("date", pyjson::s(on.format("%Y-%m-%d").to_string()));
    outcome.note("factors_computed", OrderedJson::Bool(true));
    outcome.note(
        "window_lengths",
        OrderedJson::Object(
            window_lengths
                .into_iter()
                .map(|(m, n)| {
                    (
                        m.to_string(),
                        OrderedJson::Int(i64::try_from(n).unwrap_or(i64::MAX)),
                    )
                })
                .collect(),
        ),
    );
    outcome.note("turnover_sources", turnover_sources(&frame)?);
    let flagged = |m: &HashMap<i64, i64>| {
        OrderedJson::Int(i64::try_from(m.values().filter(|v| **v != 0).count()).unwrap_or(0))
    };
    outcome.note("top_beta_flagged", flagged(&risk.beta));
    outcome.note("top_volatility_flagged", flagged(&risk.volatility));
    outcome.note(
        "mask_mismatches",
        if mismatches.is_empty() {
            OrderedJson::Null
        } else {
            pyjson::strings(mismatches)
        },
    );
    outcome.note(
        "rank_columns",
        pyjson::object([
            ("mom_pctile_rows", OrderedJson::Int(mom_rows)),
            ("rank_persist_20_rows", OrderedJson::Int(persist_rows)),
        ]),
    );
    Ok(written)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_risk_cut_is_the_top_tenth_rounded_half_even_at_least_one() {
        let mut five: Vec<(i64, f64)> = (0..5)
            .map(|i| (i, f64::from(i32::try_from(i).unwrap())))
            .collect();
        assert_eq!(top_decile(&mut five), &[(4, 4.0)]);
        // 25 x 0.1 = 2.5 -> 2 (Python's round is half to even).
        let mut many: Vec<(i64, f64)> = (0..25)
            .map(|i| (i, f64::from(i32::try_from(i).unwrap())))
            .collect();
        assert_eq!(top_decile(&mut many).len(), 2);
        let mut thirty_five: Vec<(i64, f64)> = (0..35)
            .map(|i| (i, f64::from(i32::try_from(i).unwrap())))
            .collect();
        assert_eq!(top_decile(&mut thirty_five).len(), 4);
        // Ties keep the members' order (a stable sort, descending).
        let mut tied = vec![(7, 1.0), (3, 2.0), (9, 2.0), (1, 0.5)];
        assert_eq!(top_decile(&mut tied), &[(3, 2.0)]);
    }

    #[test]
    fn mask_bits_cover_the_fifteen_universes_only() {
        assert_eq!(mask_value(1), Some(1));
        assert_eq!(mask_value(13), Some(4096));
        assert_eq!(mask_value(15), Some(1 << 14));
        assert_eq!(mask_value(16), None);
        assert_eq!(mask_value(0), None);
    }

    #[test]
    fn the_true_ath_replaces_a_lower_lookback_high_and_moves_away_high_ath() {
        let frame = DataFrame::new(
            3,
            vec![
                Series::new("instrument_id".into(), vec![1_i64, 2, 3]).into_column(),
                Series::new("close".into(), vec![Some(90.0), Some(50.0), Some(10.0)]).into_column(),
                Series::new("high_ath".into(), vec![Some(100.0), Some(50.0), None]).into_column(),
                Series::new("away_high_ath".into(), vec![Some(-10.0), Some(0.0), None])
                    .into_column(),
            ],
        )
        .unwrap();
        let truth: BTreeMap<i64, f64> = [(1, 120.0), (2, 40.0)].into_iter().collect();
        let out = apply_true_ath(frame, &truth).unwrap();
        assert_eq!(
            floats(&out, "high_ath").unwrap(),
            vec![Some(120.0), Some(50.0), None]
        );
        assert_eq!(
            floats(&out, "away_high_ath").unwrap(),
            vec![Some(-25.0), Some(0.0), None]
        );
    }
}
