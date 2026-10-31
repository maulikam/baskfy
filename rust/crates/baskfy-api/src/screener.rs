//! `baskfy_api.screener`'s execution half: as-of resolution, the legacy statement and the
//! ranking-engine path, provenance, the Redis cache with its single-flight, and `record_run`.
//! Shared by the API routes and the worker's publish and screener lanes (R8.ar6).

use std::collections::HashMap;
use std::fmt::Write as _;
use std::future::Future;
use std::sync::{LazyLock, Mutex};

use baskfy_core::ranking_engine::{
    self, MissingPolicy, Mode, RankingResult, RankingSpec, Scope, TermSpec,
};
use baskfy_core::screener::{
    self as core_screener, BindValue, CompiledStatement, DESK_FRAME_COLUMNS, DESK_SCORE_KEY,
    DecimalValue, MAX_RESULT_ROWS, Payload, PyDecimal, RankingFactor, ScreenDefinition,
    ScreenError, ScreenResult, ScreenResultRow,
};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use polars::prelude::{AnyValue, Column, DataFrame, NamedFrom, Series};
use sqlx::postgres::{PgArguments, PgRow};
use sqlx::{Arguments as _, Column as _, PgConnection, Row as _, TypeInfo as _, ValueRef as _};

/// `SCREEN_CACHE_FALLBACK_TTL_SECONDS`: 48 hours.
pub const SCREEN_CACHE_FALLBACK_TTL_SECONDS: u64 = 48 * 60 * 60;
/// `WARM_CACHE_SCREEN_LIMIT`.
pub const WARM_CACHE_SCREEN_LIMIT: i64 = 200;
/// `DATA_START_DATE`: docs/01 §2.13's "from 1 Nov 2024".
pub const DATA_START_DATE: NaiveDate = match NaiveDate::from_ymd_opt(2024, 11, 1) {
    Some(d) => d,
    None => NaiveDate::MIN,
};
/// `LEGACY_RANKING_ENGINE_VERSION`.
pub const LEGACY_RANKING_ENGINE_VERSION: &str = "legacy-sql";
/// `seed_data.NSE_EXCHANGE_ID`.
pub const NSE_EXCHANGE_ID: i16 = 1;
/// `SINGLE_FLIGHT_TIMEOUT_SECONDS`.
const SINGLE_FLIGHT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

/// `baskfy_core.ranking.DESK_SCORE_EXPLAIN_COLUMNS`.
pub const DESK_SCORE_EXPLAIN_COLUMNS: [&str; 8] = [
    "desk_a_trend",
    "desk_b_momentum",
    "desk_c_sharpe",
    "desk_d_consistency",
    "desk_e_liquidity",
    "desk_f_penalty",
    "desk_reject",
    "desk_eligible",
];

/// `UNIVERSES`' `(slug, name)`.
pub const UNIVERSE_NAMES: [(&str, &str); 15] = [
    ("nifty-50", "NIFTY 50"),
    ("nifty-next-50", "NIFTY NEXT 50"),
    ("nifty-100", "NIFTY 100"),
    ("nifty-200", "NIFTY 200"),
    ("nifty-500", "NIFTY 500"),
    ("nifty-total-market", "NIFTY TOTAL MARKET"),
    ("nifty-large-mid-250", "NIFTY LARGE MID 250"),
    ("nifty-midcap-150", "NIFTY MIDCAP 150"),
    ("nifty-smallcap-250", "NIFTY SMALLCAP 250"),
    ("nifty-microcap-250", "NIFTY MICROCAP 250"),
    ("nifty-mid-small-400", "NIFTY MID SMALL 400"),
    ("nifty-allcap", "All NSE Listed Stocks"),
    ("nifty-fno", "NIFTY FNO"),
    ("etf", "All NSE Listed ETFs"),
    ("nse-sme-emerge", "NSE SME (Emerge)"),
];

/// What a screen run raises, by the Python class each variant stands for.
#[derive(Debug)]
pub enum RunError<C> {
    /// The core screener's (`ScreenQueryError`, `ValidationError`, `ValueError`, ...).
    Screen(ScreenError),
    /// `AsOfOutOfRange(requested, earliest, latest)` (a `ValueError`).
    AsOfOutOfRange {
        /// `requested`.
        requested: NaiveDate,
        /// `earliest`.
        earliest: NaiveDate,
        /// `latest`.
        latest: NaiveDate,
    },
    /// `NoPublishedData` (a `RuntimeError`).
    NoPublishedData(String),
    /// Any other Python raise: its class and message.
    Raised(&'static str, String),
    /// The database's.
    Db(sqlx::Error),
    /// The cache's.
    Cache(C),
}

impl<C> RunError<C> {
    /// `str(exc)`.
    #[must_use]
    pub fn message(&self) -> String
    where
        C: std::fmt::Display,
    {
        match self {
            Self::Screen(e) => e.to_string(),
            Self::AsOfOutOfRange {
                requested,
                earliest,
                latest,
            } => format!(
                "{} is outside the range this service can serve ({} to {})",
                requested.format("%Y-%m-%d"),
                earliest.format("%Y-%m-%d"),
                latest.format("%Y-%m-%d")
            ),
            Self::NoPublishedData(m) | Self::Raised(_, m) => m.clone(),
            Self::Db(e) => e.to_string(),
            Self::Cache(e) => e.to_string(),
        }
    }

    /// The same error with another cache type (no cache was involved).
    pub fn recast<D>(self) -> RunError<D>
    where
        C: Into<D>,
    {
        match self {
            Self::Screen(e) => RunError::Screen(e),
            Self::AsOfOutOfRange {
                requested,
                earliest,
                latest,
            } => RunError::AsOfOutOfRange {
                requested,
                earliest,
                latest,
            },
            Self::NoPublishedData(m) => RunError::NoPublishedData(m),
            Self::Raised(c, m) => RunError::Raised(c, m),
            Self::Db(e) => RunError::Db(e),
            Self::Cache(e) => RunError::Cache(e.into()),
        }
    }
}

impl<C> From<sqlx::Error> for RunError<C> {
    fn from(e: sqlx::Error) -> Self {
        Self::Db(e)
    }
}

impl<C> From<ScreenError> for RunError<C> {
    fn from(e: ScreenError) -> Self {
        Self::Screen(e)
    }
}

/// A run with no cache: the cache error type is uninhabited.
pub type NoCacheError = std::convert::Infallible;

impl From<RunError<NoCacheError>> for crate::problems::Problem {
    fn from(e: RunError<NoCacheError>) -> Self {
        problem(&e)
    }
}

/// The API's answer for a run error: app.py's handlers (`ScreenQueryError` 400, `AsOfOutOfRange`
/// 422, `NoPublishedData` 503), everything else the 500 catch-all.
#[must_use]
pub fn problem<C: std::fmt::Display>(e: &RunError<C>) -> crate::problems::Problem {
    match e {
        RunError::Screen(ScreenError::Query(m)) => {
            crate::problems::invalid_screen_definition(vec![vec![
                (
                    "field".to_owned(),
                    OrderedJson::Str("definition".to_owned()),
                ),
                ("message".to_owned(), OrderedJson::Str(m.clone())),
            ]])
        }
        RunError::AsOfOutOfRange {
            requested,
            earliest,
            latest,
        } => crate::problems::no_trading_day(*requested, *earliest, *latest),
        RunError::NoPublishedData(m) => crate::problems::pipeline_degraded(m),
        other => {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", &other.message())],
            );
            crate::problems::internal_error()
        }
    }
}

type R<T, C> = Result<T, RunError<C>>;

/// `ScreenCache`: the slice of Redis a run uses.
pub trait ScreenCache {
    /// The cache's failure.
    type Error: Send;
    /// `await cache.get(key)`.
    fn get(
        &mut self,
        key: &str,
    ) -> impl Future<Output = Result<Option<Vec<u8>>, Self::Error>> + Send;
    /// `await cache.set(key, value, ttl)`.
    fn set_ex(
        &mut self,
        key: &str,
        value: &str,
        ttl: u64,
    ) -> impl Future<Output = Result<(), Self::Error>> + Send;
}

/// The API's cache: `app.state.cache` through the shared script client.
pub struct ApiCache {
    url: baskfy_providers::secret::Secret,
}

impl ApiCache {
    /// The process cache at `url`.
    #[must_use]
    pub fn new(url: &baskfy_providers::secret::Secret) -> Self {
        Self { url: url.clone() }
    }
}

const GET_SCRIPT: &str = "return redis.call('GET', KEYS[1])";
const SET_EX_SCRIPT: &str = "return redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])";

impl ScreenCache for ApiCache {
    type Error = String;

    async fn get(&mut self, key: &str) -> Result<Option<Vec<u8>>, String> {
        use baskfy_providers::ratelimit::ScriptValue;
        match crate::cache::eval(&self.url, GET_SCRIPT, &[key.to_owned()], &[]).await? {
            ScriptValue::Bytes(b) => Ok(Some(b)),
            _ => Ok(None),
        }
    }

    async fn set_ex(&mut self, key: &str, value: &str, ttl: u64) -> Result<(), String> {
        crate::cache::eval(
            &self.url,
            SET_EX_SCRIPT,
            &[key.to_owned()],
            &[value.to_owned(), ttl.to_string()],
        )
        .await
        .map(|_| ())
    }
}

/// `AsOfResolution`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AsOfResolution {
    /// `requested`.
    pub requested: Option<NaiveDate>,
    /// `as_of`.
    pub as_of: NaiveDate,
    /// `latest_published`.
    pub latest_published: NaiveDate,
    /// `snapped`.
    pub snapped: bool,
}

/// `ScreenRunResult`.
#[derive(Debug, Clone)]
pub struct ScreenRunResult {
    /// The response bytes.
    pub payload: String,
    /// The as-of resolution.
    pub resolution: AsOfResolution,
    /// The data version.
    pub data_version: i64,
    /// Served from the cache.
    pub cache_hit: bool,
    /// `None` on a cache hit.
    pub result: Option<ScreenResult>,
}

/// `current_data_version`.
///
/// # Errors
/// The database's.
pub async fn current_data_version(conn: &mut PgConnection) -> Result<i64, sqlx::Error> {
    let (value,): (Option<i64>,) = sqlx::query_as(
        "SELECT max(pipeline_run.data_version) AS max_1 \nFROM pipeline_run \nWHERE \
         pipeline_run.data_version IS NOT NULL",
    )
    .fetch_one(&mut *conn)
    .await?;
    Ok(value.unwrap_or(0))
}

/// `latest_published_date`.
///
/// # Errors
/// The database's.
pub async fn latest_published_date(
    conn: &mut PgConnection,
) -> Result<Option<NaiveDate>, sqlx::Error> {
    let (value,): (Option<NaiveDate>,) = sqlx::query_as(
        "SELECT max(pipeline_run.trade_date) AS max_1 \nFROM pipeline_run \nWHERE \
         pipeline_run.data_version IS NOT NULL",
    )
    .fetch_one(&mut *conn)
    .await?;
    Ok(value)
}

/// `snap_backward_to_trading_day`.
///
/// # Errors
/// The database's.
pub async fn snap_backward_to_trading_day(
    conn: &mut PgConnection,
    date: NaiveDate,
) -> Result<Option<NaiveDate>, sqlx::Error> {
    let (value,): (Option<NaiveDate>,) = sqlx::query_as(
        "SELECT max(trading_day.date) AS max_1 \nFROM trading_day \nWHERE \
         trading_day.exchange_id = $1::SMALLINT AND trading_day.is_trading_day IS true AND \
         trading_day.date <= $2::DATE",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(date)
    .fetch_one(&mut *conn)
    .await?;
    Ok(value)
}

/// `resolve_as_of(session, requested)`.
///
/// # Errors
/// `NoPublishedData`, `AsOfOutOfRange`, the database's.
pub async fn resolve_as_of<C>(
    conn: &mut PgConnection,
    requested: Option<NaiveDate>,
) -> R<AsOfResolution, C> {
    let Some(latest) = latest_published_date(conn).await? else {
        return Err(RunError::NoPublishedData(
            "no pipeline_run has been published, so factor_daily has no trustworthy as-of date"
                .to_owned(),
        ));
    };
    let Some(requested) = requested else {
        return Ok(AsOfResolution {
            requested: None,
            as_of: latest,
            latest_published: latest,
            snapped: false,
        });
    };
    let out = || RunError::AsOfOutOfRange {
        requested,
        earliest: DATA_START_DATE,
        latest,
    };
    if requested > latest || requested < DATA_START_DATE {
        return Err(out());
    }
    match snap_backward_to_trading_day(conn, requested).await? {
        Some(s) if s >= DATA_START_DATE => Ok(AsOfResolution {
            requested: Some(requested),
            as_of: s,
            latest_published: latest,
            snapped: s != requested,
        }),
        _ => Err(out()),
    }
}

/// The compiled statement as asyncpg executes it: expanding lists one typed `$n` per element,
/// every later position renumbered, the arguments bound in that order.
///
/// # Errors
/// The rendering's message (an unbound name, a bind failure).
pub fn executable(compiled: &CompiledStatement) -> Result<(String, PgArguments), String> {
    let value_of = |name: &str| {
        compiled
            .params
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, v)| v.clone())
    };
    let bind_err = |e: sqlx::error::BoxDynError| format!("cannot bind a screen parameter: {e}");
    let mut args = PgArguments::default();
    let mut renumber: Vec<usize> = Vec::new();
    let mut expansions: Vec<(String, String)> = Vec::new();
    let mut next = 1usize;
    for name in &compiled.param_order {
        let value = value_of(name).ok_or_else(|| format!("no value bound for {name}"))?;
        match value {
            BindValue::StrList(items) => {
                let rendered = if items.is_empty() {
                    "SELECT CAST(NULL AS VARCHAR) WHERE 1!=1".to_owned()
                } else {
                    let mut parts = Vec::with_capacity(items.len());
                    for item in items {
                        parts.push(format!("${next}::VARCHAR"));
                        next += 1;
                        args.add(item).map_err(bind_err)?;
                    }
                    parts.join(", ")
                };
                expansions.push((format!("__[POSTCOMPILE_{name}]"), rendered));
            }
            other => {
                renumber.push(next);
                next += 1;
                match other {
                    BindValue::Int(i) => args.add(i).map_err(bind_err)?,
                    BindValue::Float(f) => args.add(f).map_err(bind_err)?,
                    BindValue::Decimal(d) => args.add(d.py_str()).map_err(bind_err)?,
                    BindValue::Date(d) => args.add(d).map_err(bind_err)?,
                    BindValue::StrList(_) => {}
                }
            }
        }
    }
    let mut sql = renumber_placeholders(&compiled.sql, &renumber)?;
    for (token, rendered) in expansions {
        sql = sql.replace(&token, &rendered);
    }
    Ok((sql, args))
}

/// Every `$<digits>` in `sql` replaced by its new position.
///
/// # Errors
/// A placeholder with no bound value.
pub fn renumber_placeholders(sql: &str, renumber: &[usize]) -> Result<String, String> {
    let mut out = String::with_capacity(sql.len() + 16);
    let mut chars = sql.char_indices().peekable();
    while let Some((_, c)) = chars.next() {
        if c != '$' {
            out.push(c);
            continue;
        }
        let mut digits = String::new();
        while let Some((_, d)) = chars.peek() {
            if d.is_ascii_digit() {
                digits.push(*d);
                chars.next();
            } else {
                break;
            }
        }
        if digits.is_empty() {
            out.push('$');
            continue;
        }
        let old: usize = digits
            .parse()
            .map_err(|_| format!("bad placeholder ${digits}"))?;
        let new = old
            .checked_sub(1)
            .and_then(|i| renumber.get(i))
            .ok_or_else(|| format!("placeholder ${old} has no bound value"))?;
        let _ = write!(out, "${new}");
    }
    Ok(out)
}

/// One result cell as asyncpg hands it to Python.
///
/// # Errors
/// `TypeError` for an unsupported column type; the database's.
pub fn cell<C>(row: &PgRow, column: &str) -> R<Payload, C> {
    let raw = row.try_get_raw(column)?;
    if raw.is_null() {
        return Ok(Payload::Null);
    }
    let type_name = raw.type_info().name().to_owned();
    Ok(match type_name.as_str() {
        "INT2" => Payload::Int(i64::from(row.try_get::<i16, _>(column)?)),
        "INT4" => Payload::Int(i64::from(row.try_get::<i32, _>(column)?)),
        "INT8" => Payload::Int(row.try_get::<i64, _>(column)?),
        "FLOAT4" => Payload::Float(f64::from(row.try_get::<f32, _>(column)?)),
        "FLOAT8" => Payload::Float(row.try_get::<f64, _>(column)?),
        "BOOL" => Payload::Bool(row.try_get::<bool, _>(column)?),
        "DATE" => Payload::Date(row.try_get::<NaiveDate, _>(column)?),
        "TEXT" | "VARCHAR" | "BPCHAR" | "NAME" => Payload::Str(row.try_get::<String, _>(column)?),
        "NUMERIC" => match baskfy_db::row::opt_decimal(row, column)
            .map_err(|e| RunError::Raised("TypeError", e.to_string()))?
        {
            Some(d) => Payload::Decimal(PyDecimal::Finite(DecimalValue::from(d))),
            None => Payload::Null,
        },
        other => {
            return Err(RunError::Raised(
                "TypeError",
                format!("column {column} has unsupported type {other}"),
            ));
        }
    })
}

fn as_int<C>(row: &PgRow, column: &str) -> R<i64, C> {
    match cell(row, column)? {
        Payload::Int(i) => Ok(i),
        other => Err(RunError::Raised(
            "TypeError",
            format!("expected an integer rank column, got {other:?}"),
        )),
    }
}

fn put(values: &mut Vec<(String, Payload)>, k: String, v: Payload) {
    match values.iter_mut().find(|(key, _)| *key == k) {
        Some(slot) => slot.1 = v,
        None => values.push((k, v)),
    }
}

/// `_row_to_result`.
///
/// # Errors
/// As [`cell`].
pub fn row_to_result<C>(row: &PgRow, columns: &[String]) -> R<ScreenResultRow, C> {
    let mut values = Vec::with_capacity(columns.len());
    for name in columns {
        let value = cell(row, name)?;
        put(&mut values, name.clone(), value);
    }
    Ok(ScreenResultRow {
        rank: as_int(row, "rank")?,
        instrument_id: as_int(row, "instrument_id")?,
        combined_rank: as_int(row, "combined_rank")?,
        ranks: [as_int(row, "r1")?, as_int(row, "r2")?, as_int(row, "r3")?],
        values,
    })
}

/// `uses_desk_score`.
#[must_use]
pub fn uses_desk_score(definition: &ScreenDefinition) -> bool {
    definition.sort_by == DESK_SCORE_KEY
        || definition
            .ranking_terms
            .iter()
            .any(|t| t.factor == DESK_SCORE_KEY)
}

/// `_desk_explain_values`.
#[must_use]
pub fn desk_explain_values(
    score: &Payload,
    reject: &Payload,
    parts: &[(&str, Payload)],
) -> Vec<(String, Payload)> {
    let reject_text = match reject {
        Payload::Str(s) => s.clone(),
        _ => String::new(),
    };
    let mut values: Vec<(String, Payload)> = DESK_SCORE_EXPLAIN_COLUMNS
        .iter()
        .map(|name| {
            let v = parts
                .iter()
                .find(|(k, _)| k == name)
                .map_or(Payload::Null, |(_, v)| v.clone());
            ((*name).to_owned(), v)
        })
        .collect();
    let eligible = reject_text.is_empty() && !matches!(score, Payload::Null);
    for (k, v) in [
        ("desk_reject", Payload::Str(reject_text)),
        ("desk_eligible", Payload::Bool(eligible)),
    ] {
        if let Some(slot) = values.iter_mut().find(|(key, _)| key == k) {
            slot.1 = v;
        }
    }
    values
}

/// `_attach_desk_columns`.
async fn attach_desk_columns<C>(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    rows: Vec<ScreenResultRow>,
) -> R<Vec<ScreenResultRow>, C> {
    if rows.is_empty() {
        return Ok(rows);
    }
    let ids: Vec<i64> = rows.iter().map(|r| r.instrument_id).collect();
    let mut sql = String::from(
        "SELECT desk_score_daily.instrument_id, desk_score_daily.date, desk_score_daily.score, \
         desk_score_daily.score_rank, desk_score_daily.a_trend, desk_score_daily.b_momentum, \
         desk_score_daily.c_sharpe, desk_score_daily.d_consistency, \
         desk_score_daily.e_liquidity, desk_score_daily.f_penalty, \
         desk_score_daily.ext_over_20dma, desk_score_daily.reject, \
         desk_score_daily.score_version \nFROM desk_score_daily \nWHERE desk_score_daily.date = \
         $1::DATE AND desk_score_daily.instrument_id IN (",
    );
    for i in 0..ids.len() {
        if i > 0 {
            sql.push_str(", ");
        }
        let _ = write!(sql, "${}::BIGINT", i + 2);
    }
    sql.push(')');
    let mut query = sqlx::query(&sql).bind(as_of);
    for id in &ids {
        query = query.bind(*id);
    }
    let stored = query.fetch_all(&mut *conn).await?;
    let mut by_instrument: Vec<(i64, &PgRow)> = Vec::with_capacity(stored.len());
    for item in &stored {
        let id: i64 = item.try_get("instrument_id")?;
        match by_instrument.iter_mut().find(|(k, _)| *k == id) {
            Some(slot) => slot.1 = item,
            None => by_instrument.push((id, item)),
        }
    }
    let mut attached = Vec::with_capacity(rows.len());
    for row in rows {
        let mapping = by_instrument
            .iter()
            .find(|(k, _)| *k == row.instrument_id)
            .map(|(_, r)| *r);
        let mut parts: Vec<(&str, Payload)> = Vec::new();
        let mut score = Payload::Null;
        let mut reject = Payload::Null;
        if let Some(m) = mapping {
            for (column, label) in DESK_FRAME_COLUMNS {
                if DESK_SCORE_EXPLAIN_COLUMNS.contains(&label) {
                    parts.push((label, cell(m, column)?));
                }
            }
            score = cell(m, "score")?;
            reject = cell(m, "reject")?;
        }
        let mut values = row.values.clone();
        for (k, v) in desk_explain_values(&score, &reject, &parts) {
            put(&mut values, k, v);
        }
        attached.push(ScreenResultRow { values, ..row });
    }
    Ok(attached)
}

// ---------------------------------------------------------------------------------------------
// The ranking-engine path
// ---------------------------------------------------------------------------------------------

/// `RankedFrame`: one definition ranked by C4 over its pre-filter frame.
#[derive(Debug, Clone)]
pub struct RankedFrame {
    /// `query.universe_slug`.
    pub universe_slug: String,
    /// `query.clause_details`.
    pub clause_details: Vec<(String, String)>,
    /// `spec`.
    pub spec: RankingSpec,
    /// `result` (`None` when the universe has no rows).
    pub result: Option<RankingResult>,
    /// Each input row's cells as asyncpg returned them, by instrument id.
    pub source: HashMap<i64, Vec<(String, Payload)>>,
}

fn serde_str<T: serde::Serialize>(value: &T) -> String {
    match serde_json::to_value(value) {
        Ok(serde_json::Value::String(s)) => s,
        _ => String::new(),
    }
}

fn parse_enum<T: serde::de::DeserializeOwned>(text: &str) -> Result<T, ScreenError> {
    serde_json::from_value(serde_json::Value::String(text.to_owned()))
        .map_err(|e| ScreenError::Value(e.to_string()))
}

/// `ranking_spec_for(definition)`.
///
/// # Errors
/// `ScreenQueryError` without terms; `KeyError` for an unknown factor.
pub fn ranking_spec_for(definition: &ScreenDefinition) -> Result<RankingSpec, ScreenError> {
    if definition.ranking_terms.is_empty() {
        return Err(ScreenError::Query(
            "ranking_spec_for needs a definition with ranking_terms".to_owned(),
        ));
    }
    let mut terms = Vec::with_capacity(definition.ranking_terms.len());
    for term in &definition.ranking_terms {
        let factor = baskfy_core::factor_registry::get(&term.factor)?;
        terms.push(TermSpec {
            key: term.factor.clone(),
            label: factor.label.clone(),
            weight_family: serde_str(&factor.weight_family),
            preference: parse_enum(&term.preference)?,
            weight: term.weight,
            target_min: term.target_min,
            target_max: term.target_max,
            null_policy: Some(serde_str(&factor.null_policy)),
        });
    }
    let mode: Mode = parse_enum(&definition.ranking_mode)?;
    let scope: Scope = parse_enum(&definition.ranking_scope)?;
    let missing_data: MissingPolicy = parse_enum(&definition.missing_data)?;
    Ok(RankingSpec {
        terms,
        mode,
        scope,
        missing_data,
        family_weights: definition.family_weights.as_ref().map(|w| {
            w.as_mapping()
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v))
                .collect()
        }),
    })
}

/// One column of the ranking frame, typed by the database column.
enum FrameCol {
    Int(Vec<Option<i64>>),
    Float(Vec<Option<f64>>),
    Bool(Vec<Option<bool>>),
    Str(Vec<Option<String>>),
    Date(Vec<Option<NaiveDate>>),
}

type Source = HashMap<i64, Vec<(String, Payload)>>;

fn frame_from_rows<C>(rows: &[PgRow]) -> R<(DataFrame, Source), C> {
    let mut names: Vec<String> = Vec::new();
    let mut cols: Vec<FrameCol> = Vec::new();
    if let Some(first) = rows.first() {
        for c in first.columns() {
            let name = c.name().to_owned();
            let col = match c.type_info().name() {
                "INT2" | "INT4" | "INT8" => FrameCol::Int(Vec::new()),
                "FLOAT4" | "FLOAT8" | "NUMERIC" => FrameCol::Float(Vec::new()),
                "BOOL" => FrameCol::Bool(Vec::new()),
                "DATE" => FrameCol::Date(Vec::new()),
                _ => FrameCol::Str(Vec::new()),
            };
            if let Some(i) = names.iter().position(|n| *n == name) {
                cols[i] = col;
            } else {
                names.push(name);
                cols.push(col);
            }
        }
    }
    let mut source = HashMap::new();
    for row in rows {
        let mut values: Vec<(String, Payload)> = Vec::with_capacity(names.len());
        for (name, col) in names.iter().zip(cols.iter_mut()) {
            let v = cell(row, name)?;
            match (col, &v) {
                (FrameCol::Int(c), Payload::Int(i)) => c.push(Some(*i)),
                (FrameCol::Float(c), Payload::Float(f)) => c.push(Some(*f)),
                (FrameCol::Float(c), Payload::Decimal(PyDecimal::Finite(d))) => {
                    c.push(d.py_str().parse::<f64>().ok());
                }
                (FrameCol::Bool(c), Payload::Bool(b)) => c.push(Some(*b)),
                (FrameCol::Str(c), Payload::Str(s)) => c.push(Some(s.clone())),
                (FrameCol::Date(c), Payload::Date(d)) => c.push(Some(*d)),
                (FrameCol::Int(c), _) => c.push(None),
                (FrameCol::Float(c), _) => c.push(None),
                (FrameCol::Bool(c), _) => c.push(None),
                (FrameCol::Str(c), _) => c.push(None),
                (FrameCol::Date(c), _) => c.push(None),
            }
            values.push((name.clone(), v));
        }
        let id = match values
            .iter()
            .find(|(k, _)| k == ranking_engine::INSTRUMENT_ID)
        {
            Some((_, Payload::Int(i))) => *i,
            _ => {
                return Err(RunError::Raised(
                    "TypeError",
                    "the ranking frame has a row without an instrument_id".to_owned(),
                ));
            }
        };
        source.insert(id, values);
    }
    let mut columns: Vec<Column> = Vec::with_capacity(names.len());
    for (name, col) in names.iter().zip(cols) {
        let n = name.as_str().into();
        let series = match col {
            FrameCol::Int(v) => Series::new(n, v),
            FrameCol::Float(v) => Series::new(n, v),
            FrameCol::Bool(v) => Series::new(n, v),
            FrameCol::Str(v) => Series::new(n, v),
            FrameCol::Date(v) => Series::new(n, v),
        };
        columns.push(series.into());
    }
    let frame = DataFrame::new(rows.len(), columns)
        .map_err(|e| RunError::Raised("ValueError", e.to_string()))?;
    Ok((frame, source))
}

/// `rank_definition(session, definition, as_of)`: the one engine path.
///
/// # Errors
/// `ScreenQueryError` (no terms, or a definition that cannot rank); the database's.
pub async fn rank_definition<C>(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
) -> R<RankedFrame, C> {
    let spec = ranking_spec_for(definition)?;
    let (sql, args, universe_slug, clause_details) = {
        let query = core_screener::build_ranking_frame_query(definition, as_of)?;
        let compiled = query.statement.compile();
        let (sql, args) = executable(&compiled).map_err(|m| RunError::Raised("RuntimeError", m))?;
        (
            sql,
            args,
            query.universe_slug.clone(),
            query.clause_details.clone(),
        )
    };
    let rows = sqlx::query_with(&sql, args).fetch_all(&mut *conn).await?;
    if rows.is_empty() {
        return Ok(RankedFrame {
            universe_slug,
            clause_details,
            spec,
            result: None,
            source: HashMap::new(),
        });
    }
    let (frame, source) = frame_from_rows(&rows)?;
    let result = ranking_engine::rank_frame(&frame, &spec).map_err(|e| match e {
        ranking_engine::RankingError::Value(m) => RunError::Screen(ScreenError::Query(format!(
            "ranking_terms cannot be ranked: {m}"
        ))),
        other => RunError::Raised(other.python_type(), other.message().to_owned()),
    })?;
    Ok(RankedFrame {
        universe_slug,
        clause_details,
        spec,
        result: Some(result),
        source,
    })
}

/// A frame cell as `_cell` hands it on.
#[must_use]
pub fn frame_payload(av: AnyValue<'_>) -> Payload {
    match av {
        AnyValue::Boolean(b) => Payload::Bool(b),
        AnyValue::String(s) => Payload::Str(s.to_owned()),
        AnyValue::StringOwned(s) => Payload::Str(s.to_string()),
        AnyValue::Int8(i) => Payload::Int(i64::from(i)),
        AnyValue::Int16(i) => Payload::Int(i64::from(i)),
        AnyValue::Int32(i) => Payload::Int(i64::from(i)),
        AnyValue::Int64(i) => Payload::Int(i),
        AnyValue::UInt8(i) => Payload::Int(i64::from(i)),
        AnyValue::UInt16(i) => Payload::Int(i64::from(i)),
        AnyValue::UInt32(i) => Payload::Int(i64::from(i)),
        AnyValue::UInt64(i) => i64::try_from(i).map_or(Payload::Null, Payload::Int),
        AnyValue::Float32(f) if !f.is_nan() => Payload::Float(f64::from(f)),
        AnyValue::Float64(f) if !f.is_nan() => Payload::Float(f),
        AnyValue::Date(days) => NaiveDate::from_ymd_opt(1970, 1, 1)
            .and_then(|e| e.checked_add_signed(chrono::TimeDelta::days(i64::from(days))))
            .map_or(Payload::Null, Payload::Date),
        _ => Payload::Null,
    }
}

impl RankedFrame {
    /// `record.get(name)` for row `i` of `frame` (`ranked` or `scored`): the database's cell
    /// where the engine passed the input through, the engine's otherwise.
    #[must_use]
    pub fn value(&self, frame: &DataFrame, i: usize, name: &str) -> Payload {
        let engine_owned = name == ranking_engine::SECTOR && self.spec.scope == Scope::WithinSector;
        if !engine_owned
            && let Some(id) = self.instrument_at(frame, i)
            && let Some(values) = self.source.get(&id)
            && let Some((_, v)) = values.iter().find(|(k, _)| k == name)
        {
            return v.clone();
        }
        frame
            .column(name)
            .ok()
            .and_then(|c| c.get(i).ok())
            .map_or(Payload::Null, frame_payload)
    }

    /// The instrument id of row `i` of `frame`.
    #[must_use]
    pub fn instrument_at(&self, frame: &DataFrame, i: usize) -> Option<i64> {
        match frame
            .column(ranking_engine::INSTRUMENT_ID)
            .ok()
            .and_then(|c| c.get(i).ok())
            .map(frame_payload)
        {
            Some(Payload::Int(id)) => Some(id),
            #[allow(clippy::cast_possible_truncation)]
            Some(Payload::Float(f)) => Some(f as i64),
            _ => None,
        }
    }

    /// `int(str(record[RANK]))` of row `i` of `frame`.
    #[must_use]
    pub fn rank_at(frame: &DataFrame, i: usize) -> Option<i64> {
        match frame
            .column(ranking_engine::RANK)
            .ok()
            .and_then(|c| c.get(i).ok())
            .map(frame_payload)
        {
            Some(Payload::Int(r)) => Some(r),
            _ => None,
        }
    }
}

/// `_composite_cell`: a 2 dp `Decimal`.
fn composite_cell<C>(value: &Payload) -> R<Payload, C> {
    let text = match value {
        Payload::Null => return Ok(Payload::Null),
        Payload::Float(f) => baskfy_core::py::float_repr(*f),
        Payload::Int(i) => i.to_string(),
        Payload::Decimal(PyDecimal::Finite(d)) => d.py_str(),
        other => {
            return Err(RunError::Raised(
                "TypeError",
                format!("composite_score must be numeric, got {other:?}"),
            ));
        }
    };
    let q = text
        .parse::<baskfy_core::decimal::WideDecimal>()
        .and_then(|d| d.quantize(-2, baskfy_core::decimal::Rounding::HalfEven))
        .map_err(|e| RunError::Raised("InvalidOperation", e.to_string()))?;
    let d = q
        .to_decimal()
        .ok_or_else(|| RunError::Raised("InvalidOperation", q.to_sci_string()))?;
    Ok(Payload::Decimal(PyDecimal::Finite(DecimalValue::from(d))))
}

async fn execute_ranking_screen<C>(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
    columns: &[String],
    requested_as_of: Option<NaiveDate>,
    limit: i64,
) -> R<ScreenResult, C> {
    let ranked = rank_definition(conn, definition, as_of).await?;
    let spec = &ranked.spec;
    let projection = core_screener::resolve_columns(columns)?;
    let first = spec
        .terms
        .first()
        .ok_or_else(|| RunError::Screen(ScreenError::Query("no ranking terms".to_owned())))?;
    let mut extra: Vec<String> = Vec::new();
    let composite = spec.mode == Mode::Composite
        && !projection
            .iter()
            .any(|p| p == ranking_engine::COMPOSITE_SCORE);
    if composite {
        extra.push(ranking_engine::COMPOSITE_SCORE.to_owned());
    }
    let desk_terms = spec.terms.iter().any(|t| t.key == DESK_SCORE_KEY);
    if desk_terms {
        for name in DESK_SCORE_EXPLAIN_COLUMNS {
            if !projection.iter().any(|p| p == name) {
                extra.push(name.to_owned());
            }
        }
    }
    let mut result_columns = projection.clone();
    result_columns.extend(extra.iter().cloned());
    let ranking_factors: Vec<RankingFactor> = spec
        .terms
        .iter()
        .enumerate()
        .map(|(i, t)| RankingFactor {
            position: i64::try_from(i).unwrap_or(0) + 1,
            key: t.key.clone(),
            label: t.label.clone(),
            direction: if t.preference == ranking_engine::Preference::Higher {
                "desc"
            } else {
                "asc"
            }
            .to_owned(),
        })
        .collect();
    let mut kept = Vec::new();
    let mut truncated = false;
    if let Some(result) = &ranked.result {
        let frame = &result.ranked;
        let n = frame.height();
        truncated = i64::try_from(n).unwrap_or(i64::MAX) > limit;
        let take = n.min(usize::try_from(limit).unwrap_or(0));
        for i in 0..take {
            let rank = RankedFrame::rank_at(frame, i).ok_or_else(|| {
                RunError::Raised(
                    "ValueError",
                    "invalid literal for int() with base 10: '<NA>'".to_owned(),
                )
            })?;
            let instrument_id = ranked.instrument_at(frame, i).ok_or_else(|| {
                RunError::Raised("ValueError", "a ranked row has no instrument_id".to_owned())
            })?;
            let mut values: Vec<(String, Payload)> = Vec::new();
            for name in &projection {
                let source = if name == "sorting_factor" {
                    first.key.as_str()
                } else {
                    name
                };
                let v = ranked.value(frame, i, source);
                put(&mut values, name.clone(), v);
            }
            if composite {
                let v = composite_cell(&ranked.value(frame, i, ranking_engine::COMPOSITE_SCORE))?;
                put(&mut values, ranking_engine::COMPOSITE_SCORE.to_owned(), v);
            }
            if desk_terms {
                let parts: Vec<(&str, Payload)> = DESK_SCORE_EXPLAIN_COLUMNS
                    .iter()
                    .map(|name| (*name, ranked.value(frame, i, name)))
                    .collect();
                let score = ranked.value(frame, i, ranking_engine::DESK_SCORE_KEY);
                let reject = ranked.value(frame, i, ranking_engine::DESK_REJECT);
                for (k, v) in desk_explain_values(&score, &reject, &parts) {
                    put(&mut values, k, v);
                }
            }
            kept.push(ScreenResultRow {
                rank,
                instrument_id,
                combined_rank: rank,
                ranks: [rank, 0, 0],
                values,
            });
        }
    }
    Ok(ScreenResult {
        as_of,
        data_version,
        sorting_factor: ranking_factors[0].clone(),
        ranking_factors,
        columns: result_columns,
        rows: kept,
        requested_as_of,
        truncated,
    })
}

/// `execute_screen(session, definition, as_of=, data_version=, columns=, requested_as_of=,
/// limit=)`.
///
/// # Errors
/// The screener's, the database's.
pub async fn execute_screen<C>(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
    columns: &[String],
    requested_as_of: Option<NaiveDate>,
    limit: i64,
) -> R<ScreenResult, C> {
    if !definition.ranking_terms.is_empty() {
        return execute_ranking_screen(
            conn,
            definition,
            as_of,
            data_version,
            columns,
            requested_as_of,
            limit,
        )
        .await;
    }
    let (sql, args, query_columns, sorting_factor, ranking_factors) = {
        let query = core_screener::build_screen_query(definition, as_of, columns, limit + 1)?;
        let compiled = query.statement.compile();
        let (sql, args) = executable(&compiled).map_err(|m| RunError::Raised("RuntimeError", m))?;
        (
            sql,
            args,
            query.columns.clone(),
            query.sorting_factor.clone(),
            query.ranking_factors.clone(),
        )
    };
    let rows = sqlx::query_with(&sql, args).fetch_all(&mut *conn).await?;
    let truncated = i64::try_from(rows.len()).unwrap_or(i64::MAX) > limit;
    let keep = usize::try_from(limit).unwrap_or(usize::MAX);
    let mut kept = Vec::with_capacity(rows.len().min(keep));
    for row in rows.iter().take(keep) {
        kept.push(row_to_result(row, &query_columns)?);
    }
    let mut result_columns = query_columns.clone();
    if definition.sort_by == DESK_SCORE_KEY {
        kept = attach_desk_columns(conn, as_of, kept).await?;
        for name in DESK_SCORE_EXPLAIN_COLUMNS {
            if !query_columns.iter().any(|c| c == name) {
                result_columns.push(name.to_owned());
            }
        }
    }
    Ok(ScreenResult {
        as_of,
        data_version,
        sorting_factor,
        columns: result_columns,
        rows: kept,
        requested_as_of,
        truncated,
        ranking_factors,
    })
}

/// `desk_score_version(session, as_of)`.
///
/// # Errors
/// The database's.
pub async fn desk_score_version(
    conn: &mut PgConnection,
    as_of: NaiveDate,
) -> Result<Option<String>, sqlx::Error> {
    let (v,): (Option<String>,) = sqlx::query_as(
        "SELECT max(desk_score_daily.score_version) AS max_1 \nFROM desk_score_daily \n\
         WHERE desk_score_daily.date = $1::DATE",
    )
    .bind(as_of)
    .fetch_one(&mut *conn)
    .await?;
    Ok(v)
}

/// `screen_provenance(session, definition, as_of, data_version)`.
///
/// # Errors
/// `KeyError` for an unknown universe; the database's.
pub async fn screen_provenance<C>(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
) -> R<Vec<(String, Payload)>, C> {
    let Some((slug, label)) = UNIVERSE_NAMES
        .iter()
        .find(|(s, _)| *s == definition.index)
        .copied()
    else {
        return Err(RunError::Raised(
            "KeyError",
            format!("'{}'", definition.index),
        ));
    };
    let desk_version = if uses_desk_score(definition) {
        desk_score_version(conn, as_of)
            .await?
            .map_or(Payload::Null, Payload::Str)
    } else {
        Payload::Null
    };
    let engine = if definition.ranking_terms.is_empty() {
        LEGACY_RANKING_ENGINE_VERSION
    } else {
        ranking_engine::RANKING_ENGINE_VERSION
    };
    Ok(vec![
        ("universe".to_owned(), Payload::Str(slug.to_owned())),
        ("universe_label".to_owned(), Payload::Str(label.to_owned())),
        (
            "as_of".to_owned(),
            Payload::Str(as_of.format("%Y-%m-%d").to_string()),
        ),
        ("data_version".to_owned(), Payload::Int(data_version)),
        (
            "ranking_engine_version".to_owned(),
            Payload::Str(engine.to_owned()),
        ),
        ("desk_score_version".to_owned(), desk_version),
        (
            "scope".to_owned(),
            Payload::Str(definition.ranking_scope.clone()),
        ),
        (
            "mode".to_owned(),
            Payload::Str(definition.ranking_mode.clone()),
        ),
    ])
}

/// `payload_with_provenance(result, provenance)`.
///
/// # Errors
/// The canonical encoder's.
pub fn payload_with_provenance(
    result: &ScreenResult,
    provenance: Vec<(String, Payload)>,
) -> Result<String, ScreenError> {
    let Payload::Map(mut entries) = result.payload() else {
        return Err(ScreenError::Type("payload() is not a mapping".to_owned()));
    };
    put(
        &mut entries,
        "provenance".to_owned(),
        Payload::Map(provenance),
    );
    core_screener::canonical_json(&Payload::Map(entries))
}

/// `_predates_provenance`.
#[must_use]
pub fn predates_provenance(payload: &str) -> bool {
    payload.contains("\"rows\":") && !payload.contains("\"provenance\":")
}

/// `str(UnicodeDecodeError)` for `bytes.decode("utf-8")`, as CPython words it.
#[must_use]
pub fn unicode_decode_error(bytes: &[u8], e: &std::str::Utf8Error) -> String {
    let at = e.valid_up_to();
    let lead = bytes.get(at).copied().unwrap_or(0);
    let (len, reason) = match e.error_len() {
        None => (
            bytes.len().saturating_sub(at).max(1),
            "unexpected end of data",
        ),
        Some(n) if (0xC2..=0xF4).contains(&lead) => (n, "invalid continuation byte"),
        Some(n) => (n, "invalid start byte"),
    };
    if len == 1 {
        format!("'utf-8' codec can't decode byte 0x{lead:02x} in position {at}: {reason}")
    } else {
        format!(
            "'utf-8' codec can't decode bytes in position {at}-{}: {reason}",
            at + len - 1
        )
    }
}

/// `json_number(value)`.
#[must_use]
pub fn json_number(value: Option<&Payload>) -> OrderedJson {
    match value {
        None | Some(Payload::Null) => OrderedJson::Null,
        Some(Payload::Decimal(PyDecimal::Finite(d))) => OrderedJson::Str(d.format_f()),
        Some(Payload::Decimal(PyDecimal::NonFinite(s))) => OrderedJson::Str(s.clone()),
        Some(Payload::Int(i)) => OrderedJson::Str(i.to_string()),
        Some(Payload::Float(f)) => OrderedJson::Str(baskfy_core::py::float_repr(*f)),
        Some(Payload::Bool(b)) => OrderedJson::Str(if *b { "True" } else { "False" }.to_owned()),
        Some(Payload::Str(s)) => OrderedJson::Str(s.clone()),
        Some(Payload::Date(d)) => OrderedJson::Str(d.format("%Y-%m-%d").to_string()),
        Some(other) => OrderedJson::Str(format!("{other:?}")),
    }
}

/// `record_run(session, screen_id, result, definition_hash)`: the idempotent upsert.
///
/// # Errors
/// The database's; `ValueError` when the rows do not encode.
pub async fn record_run<C>(
    conn: &mut PgConnection,
    screen_id: i64,
    result: &ScreenResult,
    definition_hash: &str,
) -> R<(), C> {
    let rows: Vec<OrderedJson> = result
        .rows
        .iter()
        .map(|row| {
            let factor = row
                .values
                .iter()
                .find(|(k, _)| k == "sorting_factor")
                .map(|(_, v)| v);
            OrderedJson::Object(vec![
                ("rank".to_owned(), OrderedJson::Int(row.rank)),
                (
                    "instrument_id".to_owned(),
                    OrderedJson::Int(row.instrument_id),
                ),
                ("factor_value".to_owned(), json_number(factor)),
            ])
        })
        .collect();
    let results = OrderedJson::Array(rows)
        .to_python_json()
        .map_err(|e| RunError::Raised("ValueError", e.to_string()))?;
    sqlx::query(
        "INSERT INTO screen_run (screen_id, as_of, definition_hash, result_count, results) \
         VALUES ($1::BIGINT, $2::DATE, $3::VARCHAR, $4::INTEGER, CAST($5 AS JSONB)) ON CONFLICT \
         (screen_id, as_of, definition_hash) DO UPDATE SET result_count = \
         excluded.result_count, results = excluded.results",
    )
    .bind(screen_id)
    .bind(result.as_of)
    .bind(definition_hash)
    .bind(i32::try_from(result.result_count()).unwrap_or(i32::MAX))
    .bind(results)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// `SCREEN_FLIGHT`: one per process.
static FLIGHT: LazyLock<Mutex<HashMap<String, tokio::sync::watch::Receiver<bool>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

enum Claim {
    Leader(tokio::sync::watch::Sender<bool>),
    Follower(tokio::sync::watch::Receiver<bool>),
}

fn claim(key: &str) -> Claim {
    let Ok(mut map) = FLIGHT.lock() else {
        let (tx, _) = tokio::sync::watch::channel(false);
        return Claim::Leader(tx);
    };
    if let Some(rx) = map.get(key) {
        return Claim::Follower(rx.clone());
    }
    let (tx, rx) = tokio::sync::watch::channel(false);
    map.insert(key.to_owned(), rx);
    Claim::Leader(tx)
}

fn release(key: &str, tx: &tokio::sync::watch::Sender<bool>) {
    if let Ok(mut map) = FLIGHT.lock() {
        map.remove(key);
    }
    let _ = tx.send(true);
}

fn cached_text<C>(bytes: Option<Vec<u8>>) -> R<Option<String>, C> {
    match bytes {
        None => Ok(None),
        Some(b) => String::from_utf8(b).map(Some).map_err(|e| {
            RunError::Raised(
                "UnicodeDecodeError",
                unicode_decode_error(e.as_bytes(), &e.utf8_error()),
            )
        }),
    }
}

/// `run_screen(session, definition, requested_as_of=, columns=, cache=, ttl_seconds=)`.
///
/// # Errors
/// As [`resolve_as_of`], [`execute_screen`] and the cache's.
pub async fn run_screen<K: ScreenCache + Send>(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    requested_as_of: Option<NaiveDate>,
    columns: &[String],
    mut cache: Option<&mut K>,
    ttl_seconds: u64,
) -> R<ScreenRunResult, K::Error> {
    let requested = requested_as_of.or(definition.historical_date);
    let resolution = resolve_as_of(conn, requested).await?;
    let data_version = current_data_version(conn).await?;
    let key = core_screener::cache_key(definition, resolution.as_of, data_version, columns)?;
    let mut leader: Option<tokio::sync::watch::Sender<bool>> = None;
    if let Some(c) = cache.as_deref_mut() {
        let hit = cached_text(c.get(&key).await.map_err(RunError::Cache)?)?;
        if let Some(text) = hit
            && !predates_provenance(&text)
        {
            return Ok(ScreenRunResult {
                payload: text,
                resolution,
                data_version,
                cache_hit: true,
                result: None,
            });
        }
        match claim(&key) {
            Claim::Leader(tx) => leader = Some(tx),
            Claim::Follower(mut rx) => {
                let _ =
                    tokio::time::timeout(SINGLE_FLIGHT_TIMEOUT, rx.wait_for(|done| *done)).await;
                let hit = cached_text(c.get(&key).await.map_err(RunError::Cache)?)?;
                if let Some(text) = hit
                    && !predates_provenance(&text)
                {
                    return Ok(ScreenRunResult {
                        payload: text,
                        resolution,
                        data_version,
                        cache_hit: true,
                        result: None,
                    });
                }
            }
        }
    }
    let outcome = compute(
        conn,
        definition,
        &resolution,
        data_version,
        columns,
        requested,
        cache,
        &key,
        ttl_seconds,
    )
    .await;
    if let Some(tx) = &leader {
        release(&key, tx);
    }
    let (payload, result) = outcome?;
    Ok(ScreenRunResult {
        payload,
        resolution,
        data_version,
        cache_hit: false,
        result: Some(result),
    })
}

#[allow(clippy::too_many_arguments)]
async fn compute<K: ScreenCache + Send>(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    resolution: &AsOfResolution,
    data_version: i64,
    columns: &[String],
    requested: Option<NaiveDate>,
    cache: Option<&mut K>,
    key: &str,
    ttl_seconds: u64,
) -> R<(String, ScreenResult), K::Error> {
    let result = execute_screen(
        conn,
        definition,
        resolution.as_of,
        data_version,
        columns,
        requested,
        MAX_RESULT_ROWS,
    )
    .await?;
    let provenance = screen_provenance(conn, definition, resolution.as_of, data_version).await?;
    let payload = payload_with_provenance(&result, provenance)?;
    if let Some(c) = cache {
        c.set_ex(key, &payload, ttl_seconds)
            .await
            .map_err(RunError::Cache)?;
    }
    Ok((payload, result))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn placeholders_are_renumbered_and_lists_expanded() {
        assert_eq!(
            renumber_placeholders("a = $1 AND b IN (x) AND c = $2", &[1, 4]).unwrap(),
            "a = $1 AND b IN (x) AND c = $4"
        );
        assert!(renumber_placeholders("a = $3", &[1]).is_err());
        assert_eq!(
            renumber_placeholders("price $ x", &[]).unwrap(),
            "price $ x"
        );
    }

    #[test]
    fn json_number_keeps_the_decimal_digits() {
        let d = rust_decimal::Decimal::new(1300, 2);
        let p = Payload::Decimal(PyDecimal::Finite(DecimalValue::from(d)));
        assert_eq!(json_number(Some(&p)), OrderedJson::Str("13.00".to_owned()));
        assert_eq!(json_number(None), OrderedJson::Null);
        assert_eq!(json_number(Some(&Payload::Null)), OrderedJson::Null);
        assert_eq!(
            json_number(Some(&Payload::Int(5))),
            OrderedJson::Str("5".to_owned())
        );
    }

    #[test]
    fn a_non_utf8_cache_value_is_worded_as_cpython_words_it() {
        let cases: [(&[u8], &str); 8] = [
            (
                b"\xff",
                "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
            ),
            (
                b"ab\xe2\x82",
                "'utf-8' codec can't decode bytes in position 2-3: unexpected end of data",
            ),
            (
                b"\xe2",
                "'utf-8' codec can't decode byte 0xe2 in position 0: unexpected end of data",
            ),
            (
                b"\xe0\x80",
                "'utf-8' codec can't decode byte 0xe0 in position 0: invalid continuation byte",
            ),
            (
                b"\xe2\x82x",
                "'utf-8' codec can't decode bytes in position 0-1: invalid continuation byte",
            ),
            (
                b"\xc3",
                "'utf-8' codec can't decode byte 0xc3 in position 0: unexpected end of data",
            ),
            (
                b"x\x80y",
                "'utf-8' codec can't decode byte 0x80 in position 1: invalid start byte",
            ),
            (
                b"\xf0\x9f\x98",
                "'utf-8' codec can't decode bytes in position 0-2: unexpected end of data",
            ),
        ];
        for (bytes, want) in cases {
            let err = String::from_utf8(bytes.to_vec()).unwrap_err();
            assert_eq!(
                unicode_decode_error(err.as_bytes(), &err.utf8_error()),
                want
            );
        }
    }

    #[test]
    fn an_envelope_without_provenance_predates_c6() {
        assert!(predates_provenance(r#"{"rows":[]}"#));
        assert!(!predates_provenance(r#"{"rows":[],"provenance":{}}"#));
        assert!(!predates_provenance(r#"{"x":1}"#));
    }

    #[test]
    fn desk_values_mark_a_rejected_or_unscored_row_ineligible() {
        let v = desk_explain_values(&Payload::Null, &Payload::Null, &[]);
        let names: Vec<&str> = v.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(names, DESK_SCORE_EXPLAIN_COLUMNS);
        assert!(matches!(v.last(), Some((_, Payload::Bool(false)))));
        let v = desk_explain_values(&Payload::Int(1), &Payload::Str(String::new()), &[]);
        assert!(matches!(v.last(), Some((_, Payload::Bool(true)))));
        let v = desk_explain_values(&Payload::Int(1), &Payload::Str("thin".to_owned()), &[]);
        assert!(matches!(v.last(), Some((_, Payload::Bool(false)))));
    }
}
