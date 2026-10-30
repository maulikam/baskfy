//! Step 10 — `publish`. Ports `baskfy_worker.tasks.publish.run_publish`, and the half of
//! `baskfy_api.screener` it stands on: `current_data_version`, `purge_screen_cache`,
//! `warm_screen_cache` (with `top_screen_definitions`, `run_screen`, `resolve_as_of`, the
//! legacy `execute_screen` path, `screen_provenance`, `payload_with_provenance`, `record_run`).
//!
//! `data_version` is monotonic and only ever set on a run whose gate passed, so "the current
//! version" is the maximum over published runs. The purge runs before the warm-up (warming first
//! would delete what it had just computed); both record a cache failure on the step instead of
//! failing a publish whose data is good.
//!
//! **Not ported (DECISIONS-RS R-gate.2):** warming a saved screen with `ranking_terms` — the
//! ranking-engine path (`rank_definition`) over a pandas object frame. No screen on the bed or
//! the box has one; meeting one, the step **raises** (`NotPorted`) rather than warm it
//! differently from Python, so the chain rolls back and says why. A cutover gate.

use std::fmt::Write as _;

use baskfy_core::screener::{
    self as core_screener, BindValue, CompiledStatement, DESK_FRAME_COLUMNS, DESK_SCORE_KEY,
    DecimalValue, MAX_RESULT_ROWS, Payload, PyDecimal, ScreenDefinition, ScreenError, ScreenResult,
    ScreenResultRow,
};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use redis::AsyncCommands as _;
use sqlx::Arguments as _;
use sqlx::postgres::{PgArguments, PgRow};
use sqlx::{PgConnection, Row as _, TypeInfo as _, ValueRef as _};

use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::pyjson::{self, int};
use crate::steps::{PipelineRun, RunStatus, StepOutcome};

/// `SCREEN_CACHE_FALLBACK_TTL_SECONDS`: 48 hours, the expiry of a key no publish purged.
pub(crate) const SCREEN_CACHE_FALLBACK_TTL_SECONDS: u64 = 48 * 60 * 60;
/// `WARM_CACHE_SCREEN_LIMIT`: "warm the top 200 most-run screen definitions".
pub(crate) const WARM_CACHE_SCREEN_LIMIT: i64 = 200;
/// `DATA_START_DATE`: docs/01 §2.13's "from 1 Nov 2024".
pub(crate) const DATA_START_DATE: NaiveDate = match NaiveDate::from_ymd_opt(2024, 11, 1) {
    Some(d) => d,
    None => NaiveDate::MIN,
};
/// `LEGACY_RANKING_ENGINE_VERSION`.
pub(crate) const LEGACY_RANKING_ENGINE_VERSION: &str = "legacy-sql";

/// `baskfy_core.ranking.DESK_SCORE_EXPLAIN_COLUMNS`.
const DESK_SCORE_EXPLAIN_COLUMNS: [&str; 8] = [
    "desk_a_trend",
    "desk_b_momentum",
    "desk_c_sharpe",
    "desk_d_consistency",
    "desk_e_liquidity",
    "desk_f_penalty",
    "desk_reject",
    "desk_eligible",
];

/// `UNIVERSES`' `(slug, name)` — the provenance's `universe_label`.
const UNIVERSE_NAMES: [(&str, &str); 15] = [
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

/// What `run_publish` returns.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Published {
    pub(crate) data_version: i64,
    pub(crate) purged: i64,
    pub(crate) warmed: i64,
}

fn now_utc() -> chrono::DateTime<chrono::Utc> {
    use baskfy_providers::clock::Clock as _;
    baskfy_providers::clock::system_clock().now()
}

/// `screener.current_data_version`: the highest version ever published, `0` before the first.
pub(crate) async fn current_data_version(conn: &mut PgConnection) -> Result<i64, WorkerError> {
    let (value,): (Option<i64>,) = sqlx::query_as(
        "SELECT max(pipeline_run.data_version) AS max_1 \nFROM pipeline_run \nWHERE \
         pipeline_run.data_version IS NOT NULL",
    )
    .fetch_one(&mut *conn)
    .await?;
    Ok(value.unwrap_or(0))
}

/// `run_publish(session, outcome, run, cache)`.
pub(crate) async fn run_publish(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    run: &mut PipelineRun,
    cache: Option<&redis::Client>,
) -> Result<Published, WorkerError> {
    let previous = current_data_version(conn).await?;
    let version = previous + 1;
    // `run.data_version = version; run.status = SUCCEEDED; run.finished_at = now(); flush()`.
    sqlx::query(
        "UPDATE pipeline_run SET status = $1, finished_at = $2, data_version = $3 WHERE id = $4",
    )
    .bind(RunStatus::Succeeded.value())
    .bind(now_utc())
    .bind(version)
    .bind(run.id)
    .execute(&mut *conn)
    .await?;
    RunStatus::Succeeded.value().clone_into(&mut run.status);

    let purged = purge(cache, outcome).await;
    let warmed = warm(conn, cache, outcome).await?;

    outcome.rows_in = 1;
    outcome.rows_out = 1;
    let (warmed_count, failures) = match &warmed {
        Some(w) => (
            w.warmed,
            (!w.failures.is_empty()).then(|| w.failures.clone()),
        ),
        None => (0, None),
    };
    outcome.note("previous_data_version", int(previous));
    outcome.note("data_version", int(version));
    outcome.note("cache_keys_purged", int(purged));
    outcome.note("cache_warmed", int(warmed_count));
    outcome.note(
        "cache_warm_failures",
        failures.map_or(OrderedJson::Null, pyjson::strings),
    );
    Ok(Published {
        data_version: version,
        purged,
        warmed: warmed_count,
    })
}

/// `_purge_screen_cache`: delete every `screen:*` key; a missing or unreachable cache is noted.
async fn purge(cache: Option<&redis::Client>, outcome: &mut StepOutcome) -> i64 {
    let Some(client) = cache else {
        outcome.note("cache", pyjson::s("not configured"));
        return 0;
    };
    match purge_screen_cache(client).await {
        Ok(n) => n,
        Err(e) => {
            outcome.note("cache_purge_failed", pyjson::s(e.to_string()));
            0
        }
    }
}

/// `screener.purge_screen_cache`: `scan_iter(match="screen:*")`, then one `DEL` of every key.
async fn purge_screen_cache(client: &redis::Client) -> Result<i64, redis::RedisError> {
    let mut con = client.get_multiplexed_async_connection().await?;
    let pattern = format!("{}*", core_screener::CACHE_NAMESPACE);
    let mut keys: Vec<Vec<u8>> = Vec::new();
    let mut cursor: u64 = 0;
    loop {
        let (next, batch): (u64, Vec<Vec<u8>>) = redis::cmd("SCAN")
            .arg(cursor)
            .arg("MATCH")
            .arg(&pattern)
            .query_async(&mut con)
            .await?;
        keys.extend(batch);
        if next == 0 {
            break;
        }
        cursor = next;
    }
    if keys.is_empty() {
        return Ok(0);
    }
    let _: i64 = redis::cmd("DEL").arg(&keys).query_async(&mut con).await?;
    Ok(i64::try_from(keys.len()).unwrap_or(i64::MAX))
}

/// `WarmCacheResult`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct WarmCacheResult {
    pub(crate) warmed: i64,
    pub(crate) skipped: i64,
    pub(crate) failures: Vec<String>,
}

/// Why a warm-up stopped: the cache (noted, the step continues) or anything else (raised).
enum WarmStop {
    Cache(redis::RedisError),
    Raised(WorkerError),
}

impl From<WorkerError> for WarmStop {
    fn from(e: WorkerError) -> Self {
        Self::Raised(e)
    }
}

impl From<sqlx::Error> for WarmStop {
    fn from(e: sqlx::Error) -> Self {
        Self::Raised(e.into())
    }
}

impl From<redis::RedisError> for WarmStop {
    fn from(e: redis::RedisError) -> Self {
        Self::Cache(e)
    }
}

/// `_warm_screen_cache`: `None` without a cache, or when the cache failed (noted).
async fn warm(
    conn: &mut PgConnection,
    cache: Option<&redis::Client>,
    outcome: &mut StepOutcome,
) -> Result<Option<WarmCacheResult>, WorkerError> {
    let Some(client) = cache else {
        return Ok(None);
    };
    match warm_screen_cache(conn, client).await {
        Ok(result) => Ok(Some(result)),
        Err(WarmStop::Cache(e)) => {
            outcome.note("cache_warm_failed", pyjson::s(e.to_string()));
            Ok(None)
        }
        Err(WarmStop::Raised(e)) => Err(e),
    }
}

/// `WarmTarget`.
struct WarmTarget {
    screen_id: i64,
    public_id: String,
    definition: ScreenDefinition,
    columns: Vec<String>,
}

/// `top_screen_definitions`: the most-run screens, newest-first among never-run ones; a stored
/// definition that no longer validates is listed as unreadable rather than raised.
async fn top_screen_definitions(
    conn: &mut PgConnection,
    limit: i64,
) -> Result<(Vec<WarmTarget>, Vec<String>), WorkerError> {
    let rows = sqlx::query(
        "SELECT screen.id, screen.public_id, screen.definition, screen.columns, \
         coalesce(anon_1.run_count, $1::INTEGER) AS run_count \nFROM screen LEFT OUTER JOIN \
         (SELECT screen_run.screen_id AS screen_id, count(*) AS run_count \nFROM screen_run \
         GROUP BY screen_run.screen_id) AS anon_1 ON anon_1.screen_id = screen.id ORDER BY \
         coalesce(anon_1.run_count, $2::INTEGER) DESC, screen.updated_at DESC, screen.id ASC \n \
         LIMIT $3::INTEGER",
    )
    .bind(0_i32)
    .bind(0_i32)
    .bind(i32::try_from(limit).unwrap_or(i32::MAX))
    .fetch_all(&mut *conn)
    .await?;
    let mut targets = Vec::new();
    let mut unreadable = Vec::new();
    for row in &rows {
        let screen_id: i64 = row.try_get("id")?;
        let public_id: String = row.try_get("public_id")?;
        let definition = baskfy_db::row::jsonb(row, "definition")?;
        let columns = baskfy_db::row::jsonb(row, "columns")?;
        let payload = baskfy_api::routes::screens::payload_of(&definition);
        match ScreenDefinition::model_validate(&payload) {
            Ok(definition) => {
                let OrderedJson::Array(items) = columns else {
                    return Err(WorkerError::python(
                        "TypeError",
                        format!("screen {public_id}: columns is not a list"),
                    ));
                };
                let columns = items
                    .into_iter()
                    .map(|c| match c {
                        OrderedJson::Str(s) => Ok(s),
                        other => Err(WorkerError::python(
                            "TypeError",
                            format!("screen {public_id}: a column is not a string: {other:?}"),
                        )),
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                targets.push(WarmTarget {
                    screen_id,
                    public_id,
                    definition,
                    columns,
                });
            }
            Err(ScreenError::Validation(e)) => {
                unreadable.push(format!(
                    "{public_id}: {} validation error(s)",
                    e.errors.len()
                ));
            }
            Err(other) => return Err(screen_raise(&other)),
        }
    }
    Ok((targets, unreadable))
}

/// A screener error Python's warm-up does not catch, raised with its class.
fn screen_raise(e: &ScreenError) -> WorkerError {
    WorkerError::python(e.python_type(), e.to_string())
}

/// `warm_screen_cache(session, cache)`.
async fn warm_screen_cache(
    conn: &mut PgConnection,
    client: &redis::Client,
) -> Result<WarmCacheResult, WarmStop> {
    warm_screen_cache_limited(conn, client, WARM_CACHE_SCREEN_LIMIT).await
}

/// Why [`warm_screen_cache_with_limit`] stopped: the cache (a `RedisError`, which the task
/// raises) or anything else, raised with its Python class. Added for wave 6's screener lane
/// (`baskfy.compute.warm_screen_cache`), which raises both where `publish` notes the first.
#[derive(Debug)]
pub(crate) enum WarmFailure {
    /// A Redis failure (redis-py's `RedisError` family).
    Cache(redis::RedisError),
    /// Anything else.
    Raised(WorkerError),
}

/// `warm_screen_cache(session, cache, limit=limit)` — the task's out-of-band warm-up (wave 6,
/// lane screener). The same body `publish` runs, with the caller's `limit`.
pub(crate) async fn warm_screen_cache_with_limit(
    conn: &mut PgConnection,
    client: &redis::Client,
    limit: i64,
) -> Result<WarmCacheResult, WarmFailure> {
    warm_screen_cache_limited(conn, client, limit)
        .await
        .map_err(|stop| match stop {
            WarmStop::Cache(e) => WarmFailure::Cache(e),
            WarmStop::Raised(e) => WarmFailure::Raised(e),
        })
}

/// `execute_screen(session, definition, as_of=..., data_version=..., columns=...,
/// requested_as_of=...)` as a caller outside the warm-up sees it: every error raised with its
/// Python class (the alert dispatch's `ensure_run` catches none). Added for wave 6 (lane
/// screener). A `ranking_terms` definition raises `NotPorted` (R-gate.2).
pub(crate) async fn execute_screen_raising(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
    columns: &[String],
    requested_as_of: Option<NaiveDate>,
) -> Result<ScreenResult, WorkerError> {
    // The legacy builder first, so a definition it refuses raises its own class
    // (`ScreenQueryError`, ...) rather than the warm-up's caught message.
    if definition.ranking_terms.is_empty() {
        core_screener::build_screen_query(definition, as_of, columns, MAX_RESULT_ROWS + 1)
            .map_err(|e| screen_raise(&e))?;
    }
    execute_screen(
        conn,
        definition,
        as_of,
        data_version,
        columns,
        requested_as_of,
    )
    .await
    .map_err(|stop| match stop {
        RunStop::Caught(message) => WorkerError::python("ValueError", message),
        RunStop::Stop(WarmStop::Raised(e)) => e,
        RunStop::Stop(WarmStop::Cache(e)) => WorkerError::python("RedisError", e.to_string()),
    })
}

/// `execute_screen(session, definition, as_of=..., data_version=..., columns=..., limit=...)`
/// with the caller's `limit` — the backtest loader asks for `top_n + hold_buffer` rows (wave 6,
/// lane backtest_engine). Every error is raised with its Python class: a [`ScreenError`] as
/// its own (`ScreenQueryError` is a `ValueError`, which the backtest job catches), the rest as
/// the [`WorkerError`] it is. A `ranking_terms` definition raises `NotPorted` (R-gate.2).
pub(crate) async fn execute_screen_with_limit(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
    columns: &[String],
    limit: i64,
) -> Result<ScreenResult, WorkerError> {
    let raised = |stop: RunStop| match stop {
        RunStop::Caught(message) => WorkerError::python("ValueError", message),
        RunStop::Stop(WarmStop::Raised(e)) => e,
        RunStop::Stop(WarmStop::Cache(e)) => WorkerError::python("RedisError", e.to_string()),
    };
    if !definition.ranking_terms.is_empty() {
        return Err(WorkerError::NotPorted {
            what: "backtest: screening with ranking_terms (the ranking-engine path; \
                   DECISIONS-RS R-gate.2)",
        });
    }
    let query = core_screener::build_screen_query(definition, as_of, columns, limit + 1)
        .map_err(|e| screen_raise(&e))?;
    let compiled = query.statement.compile();
    let (sql, args) = executable(&compiled).map_err(|m| WorkerError::python("RuntimeError", m))?;
    let rows = sqlx::query_with(&sql, args).fetch_all(&mut *conn).await?;
    let truncated = i64::try_from(rows.len()).unwrap_or(i64::MAX) > limit;
    let keep = usize::try_from(limit).unwrap_or(0);
    let mut kept = Vec::with_capacity(rows.len().min(keep));
    for row in rows.iter().take(keep) {
        kept.push(row_to_result(row, &query.columns).map_err(raised)?);
    }
    let mut result_columns = query.columns.clone();
    if definition.sort_by == DESK_SCORE_KEY {
        kept = attach_desk_columns(conn, as_of, kept)
            .await
            .map_err(raised)?;
        for name in DESK_SCORE_EXPLAIN_COLUMNS {
            if !query.columns.iter().any(|c| c == name) {
                result_columns.push(name.to_owned());
            }
        }
    }
    Ok(ScreenResult {
        as_of,
        data_version,
        sorting_factor: query.sorting_factor.clone(),
        columns: result_columns,
        rows: kept,
        requested_as_of: None,
        truncated,
        ranking_factors: query.ranking_factors.clone(),
    })
}

/// `record_run(session, screen_id, result, definition_hash)` for callers outside this module
/// (wave 6, lane screener: the alert dispatch's `ensure_run`).
pub(crate) async fn record_screen_run(
    conn: &mut PgConnection,
    screen_id: i64,
    result: &ScreenResult,
    definition_hash: &str,
) -> Result<(), WorkerError> {
    record_run(conn, screen_id, result, definition_hash).await
}

/// The warm-up body over `limit` screens.
async fn warm_screen_cache_limited(
    conn: &mut PgConnection,
    client: &redis::Client,
    limit: i64,
) -> Result<WarmCacheResult, WarmStop> {
    let (targets, unreadable) = top_screen_definitions(conn, limit).await?;
    // redis-py connects on a command, not on construction: with no screen to warm, or every
    // screen refused before its `GET`, Python never touches Redis and notes no failure. So the
    // connection is opened by the first `GET`, never up front.
    let mut con: Option<redis::aio::MultiplexedConnection> = None;
    let mut warmed = 0;
    let mut skipped = 0;
    let mut failures = unreadable;
    for target in &targets {
        let ran =
            match run_screen(conn, client, &mut con, &target.definition, &target.columns).await {
                Ok(ran) => ran,
                Err(RunStop::Caught(message)) => {
                    failures.push(format!("{}: {message}", target.public_id));
                    continue;
                }
                Err(RunStop::Stop(stop)) => return Err(stop),
            };
        if let Some(result) = &ran.result {
            record_run(
                conn,
                target.screen_id,
                result,
                &target.definition.definition_hash(),
            )
            .await?;
        }
        if ran.cache_hit {
            skipped += 1;
        } else {
            warmed += 1;
        }
    }
    Ok(WarmCacheResult {
        warmed,
        skipped,
        failures,
    })
}

/// A run that ended: Python's `except (ValueError, NoPublishedData)` (the message, caught by the
/// warm-up), or anything else (propagated).
enum RunStop {
    Caught(String),
    Stop(WarmStop),
}

impl From<WarmStop> for RunStop {
    fn from(e: WarmStop) -> Self {
        Self::Stop(e)
    }
}

impl From<WorkerError> for RunStop {
    fn from(e: WorkerError) -> Self {
        Self::Stop(WarmStop::Raised(e))
    }
}

impl From<sqlx::Error> for RunStop {
    fn from(e: sqlx::Error) -> Self {
        Self::Stop(WarmStop::Raised(e.into()))
    }
}

impl From<redis::RedisError> for RunStop {
    fn from(e: redis::RedisError) -> Self {
        Self::Stop(WarmStop::Cache(e))
    }
}

/// `ScreenQueryError` and pydantic's `ValidationError` are `ValueError`s in Python, and so are
/// caught by the warm-up; `TypeError` and `KeyError` are not.
fn screen_stop(e: ScreenError) -> RunStop {
    match e {
        ScreenError::Query(_) | ScreenError::Value(_) | ScreenError::Validation(_) => {
            RunStop::Caught(e.to_string())
        }
        other => RunStop::Stop(WarmStop::Raised(screen_raise(&other))),
    }
}

/// `ScreenRunResult`, as far as the warm-up reads it.
struct ScreenRun {
    cache_hit: bool,
    result: Option<ScreenResult>,
}

/// `screener.latest_published_date`.
async fn latest_published_date(conn: &mut PgConnection) -> Result<Option<NaiveDate>, WorkerError> {
    let (value,): (Option<NaiveDate>,) = sqlx::query_as(
        "SELECT max(pipeline_run.trade_date) AS max_1 \nFROM pipeline_run \nWHERE \
         pipeline_run.data_version IS NOT NULL",
    )
    .fetch_one(&mut *conn)
    .await?;
    Ok(value)
}

/// `screener.snap_backward_to_trading_day`.
async fn snap_backward_to_trading_day(
    conn: &mut PgConnection,
    date: NaiveDate,
) -> Result<Option<NaiveDate>, WorkerError> {
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

/// `screener.resolve_as_of`: the date a screen runs for, or the message Python's
/// `NoPublishedData` / `AsOfOutOfRange` carries (both caught by the warm-up).
async fn resolve_as_of(
    conn: &mut PgConnection,
    requested: Option<NaiveDate>,
) -> Result<NaiveDate, RunStop> {
    let Some(latest) = latest_published_date(conn).await? else {
        return Err(RunStop::Caught(
            "no pipeline_run has been published, so factor_daily has no trustworthy as-of date"
                .to_owned(),
        ));
    };
    let Some(requested) = requested else {
        return Ok(latest);
    };
    let out_of_range = || {
        RunStop::Caught(format!(
            "{} is outside the range this service can serve ({} to {})",
            requested.format("%Y-%m-%d"),
            DATA_START_DATE.format("%Y-%m-%d"),
            latest.format("%Y-%m-%d")
        ))
    };
    if requested > latest || requested < DATA_START_DATE {
        return Err(out_of_range());
    }
    match snap_backward_to_trading_day(conn, requested).await? {
        Some(snapped) if snapped >= DATA_START_DATE => Ok(snapped),
        _ => Err(out_of_range()),
    }
}

/// `screener.run_screen(session, definition, columns=..., cache=..., ttl_seconds=...)`, as the
/// warm-up calls it (no requested date, one process, so no single-flight wait).
async fn run_screen(
    conn: &mut PgConnection,
    client: &redis::Client,
    lazy: &mut Option<redis::aio::MultiplexedConnection>,
    definition: &ScreenDefinition,
    columns: &[String],
) -> Result<ScreenRun, RunStop> {
    let requested = definition.historical_date;
    let as_of = resolve_as_of(conn, requested).await?;
    let data_version = current_data_version(conn).await?;
    let key =
        core_screener::cache_key(definition, as_of, data_version, columns).map_err(screen_stop)?;

    let con = match lazy {
        Some(con) => con,
        None => lazy.insert(client.get_multiplexed_async_connection().await?),
    };
    let cached: Option<Vec<u8>> = con.get(&key).await?;
    if let Some(bytes) = cached {
        // `_cached_payload`'s `bytes.decode("utf-8")`: a `UnicodeDecodeError` is a `ValueError`,
        // so the warm-up records it against the screen and carries on.
        let text = String::from_utf8(bytes)
            .map_err(|e| RunStop::Caught(unicode_decode_error(e.as_bytes(), &e.utf8_error())))?;
        if !predates_provenance(&text) {
            return Ok(ScreenRun {
                cache_hit: true,
                result: None,
            });
        }
    }

    let result = execute_screen(conn, definition, as_of, data_version, columns, requested).await?;
    let provenance = screen_provenance(conn, definition, as_of, data_version).await?;
    let payload = payload_with_provenance(&result, provenance).map_err(screen_stop)?;
    let _: () = con
        .set_ex(&key, payload, SCREEN_CACHE_FALLBACK_TTL_SECONDS)
        .await?;
    Ok(ScreenRun {
        cache_hit: false,
        result: Some(result),
    })
}

/// `str(UnicodeDecodeError)` for `bytes.decode("utf-8")`, as CPython words it.
fn unicode_decode_error(bytes: &[u8], e: &std::str::Utf8Error) -> String {
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

/// `_predates_provenance`: a cached run envelope written before C6.
fn predates_provenance(payload: &str) -> bool {
    payload.contains("\"rows\":") && !payload.contains("\"provenance\":")
}

/// `uses_desk_score`.
fn uses_desk_score(definition: &ScreenDefinition) -> bool {
    definition.sort_by == DESK_SCORE_KEY
        || definition
            .ranking_terms
            .iter()
            .any(|t| t.factor == DESK_SCORE_KEY)
}

/// `execute_screen`: the legacy docs/06 statement (a `ranking_terms` definition is refused here,
/// see the module docs).
async fn execute_screen(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
    columns: &[String],
    requested_as_of: Option<NaiveDate>,
) -> Result<ScreenResult, RunStop> {
    if !definition.ranking_terms.is_empty() {
        return Err(RunStop::Stop(WarmStop::Raised(WorkerError::NotPorted {
            what: "publish: warming a screen with ranking_terms (the ranking-engine path; \
                   DECISIONS-RS R-gate.2)",
        })));
    }
    let limit = MAX_RESULT_ROWS;
    let query = core_screener::build_screen_query(definition, as_of, columns, limit + 1)
        .map_err(screen_stop)?;
    let compiled = query.statement.compile();
    let (sql, args) = executable(&compiled)
        .map_err(|m| RunStop::Stop(WarmStop::Raised(WorkerError::python("RuntimeError", m))))?;
    let rows = sqlx::query_with(&sql, args).fetch_all(&mut *conn).await?;
    let truncated = i64::try_from(rows.len()).unwrap_or(i64::MAX) > limit;
    let keep = usize::try_from(limit).unwrap_or(usize::MAX);
    let mut kept = Vec::with_capacity(rows.len().min(keep));
    for row in rows.iter().take(keep) {
        kept.push(row_to_result(row, &query.columns)?);
    }
    let mut result_columns = query.columns.clone();
    if definition.sort_by == DESK_SCORE_KEY {
        kept = attach_desk_columns(conn, as_of, kept).await?;
        for name in DESK_SCORE_EXPLAIN_COLUMNS {
            if !query.columns.iter().any(|c| c == name) {
                result_columns.push(name.to_owned());
            }
        }
    }
    Ok(ScreenResult {
        as_of,
        data_version,
        sorting_factor: query.sorting_factor.clone(),
        columns: result_columns,
        rows: kept,
        requested_as_of,
        truncated,
        ranking_factors: query.ranking_factors.clone(),
    })
}

/// The compiled statement as asyncpg executes it: SQLAlchemy's expanding `IN
/// (__[POSTCOMPILE_x])` lists rendered one typed `$n` per element, every later position
/// renumbered, and the arguments bound in that order. The same rendering as the API's
/// `csv_export::executable` (crate-private there, so restated here; R-gate.2).
fn executable(compiled: &CompiledStatement) -> Result<(String, PgArguments), String> {
    let value_of = |name: &str| {
        compiled
            .params
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, v)| v.clone())
    };
    let bind_err = |e: sqlx::error::BoxDynError| format!("cannot bind a screen parameter: {e}");
    let mut args = PgArguments::default();
    let mut renumber: Vec<usize> = Vec::new(); // old position (1-based) -> new position
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
fn renumber_placeholders(sql: &str, renumber: &[usize]) -> Result<String, String> {
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

/// `_as_int`: a rank column (`bigint`), or Python's `TypeError`.
fn as_int(row: &PgRow, column: &str) -> Result<i64, RunStop> {
    match cell(row, column)? {
        Payload::Int(i) => Ok(i),
        other => Err(RunStop::Stop(WarmStop::Raised(WorkerError::python(
            "TypeError",
            format!("expected an integer rank column, got {other:?}"),
        )))),
    }
}

/// `_row_to_result`.
fn row_to_result(row: &PgRow, columns: &[String]) -> Result<ScreenResultRow, RunStop> {
    let mut values = Vec::with_capacity(columns.len());
    for name in columns {
        let value = cell(row, name)?;
        match values
            .iter_mut()
            .find(|(k, _): &&mut (String, Payload)| k == name)
        {
            Some(slot) => slot.1 = value,
            None => values.push((name.clone(), value)),
        }
    }
    Ok(ScreenResultRow {
        rank: as_int(row, "rank")?,
        instrument_id: as_int(row, "instrument_id")?,
        combined_rank: as_int(row, "combined_rank")?,
        ranks: [as_int(row, "r1")?, as_int(row, "r2")?, as_int(row, "r3")?],
        values,
    })
}

/// One result cell as asyncpg hands it to Python: `numeric` → `Decimal` (scale kept), integers
/// → `int`, `float4`/`float8` → `float`, text → `str`, `bool`, `date`; `NULL` → `None`.
fn cell(row: &PgRow, column: &str) -> Result<Payload, RunStop> {
    let raise = |m: String| RunStop::Stop(WarmStop::Raised(WorkerError::python("TypeError", m)));
    let raw = row.try_get_raw(column).map_err(WorkerError::from)?;
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
        "NUMERIC" => match baskfy_db::row::opt_decimal(row, column).map_err(WorkerError::from)? {
            Some(d) => Payload::Decimal(PyDecimal::Finite(DecimalValue::from(d))),
            None => Payload::Null,
        },
        other => {
            return Err(raise(format!(
                "column {column} has unsupported type {other}"
            )));
        }
    })
}

/// `_attach_desk_columns`: legacy `sort_by=desk_score` adds the stored A-F breakdown.
async fn attach_desk_columns(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    rows: Vec<ScreenResultRow>,
) -> Result<Vec<ScreenResultRow>, RunStop> {
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
            match values.iter_mut().find(|(key, _)| *key == k) {
                Some(slot) => slot.1 = v,
                None => values.push((k, v)),
            }
        }
        attached.push(ScreenResultRow { values, ..row });
    }
    Ok(attached)
}

/// `_desk_explain_values`.
fn desk_explain_values(
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

/// `screen_provenance`: what produced the payload (C6).
async fn screen_provenance(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
) -> Result<Vec<(String, Payload)>, RunStop> {
    let Some((slug, label)) = UNIVERSE_NAMES
        .iter()
        .find(|(s, _)| *s == definition.index)
        .copied()
    else {
        return Err(RunStop::Stop(WarmStop::Raised(WorkerError::python(
            "KeyError",
            format!("'{}'", definition.index),
        ))));
    };
    let desk_version = if uses_desk_score(definition) {
        let (v,): (Option<String>,) = sqlx::query_as(
            "SELECT max(desk_score_daily.score_version) AS max_1 \nFROM desk_score_daily \n\
             WHERE desk_score_daily.date = $1::DATE",
        )
        .bind(as_of)
        .fetch_one(&mut *conn)
        .await?;
        v.map_or(Payload::Null, Payload::Str)
    } else {
        Payload::Null
    };
    let engine = if definition.ranking_terms.is_empty() {
        LEGACY_RANKING_ENGINE_VERSION
    } else {
        baskfy_core::ranking_engine::RANKING_ENGINE_VERSION
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

/// `payload_with_provenance`: `canonical_json({**result.payload(), "provenance": ...})`.
fn payload_with_provenance(
    result: &ScreenResult,
    provenance: Vec<(String, Payload)>,
) -> Result<String, ScreenError> {
    let Payload::Map(mut entries) = result.payload() else {
        return Err(ScreenError::Type("payload() is not a mapping".to_owned()));
    };
    let provenance = Payload::Map(provenance);
    match entries.iter_mut().find(|(k, _)| k == "provenance") {
        Some(slot) => slot.1 = provenance,
        None => entries.push(("provenance".to_owned(), provenance)),
    }
    core_screener::canonical_json(&Payload::Map(entries))
}

/// `json_number`: a factor value as `screen_run.results` stores it — the exact decimal as a
/// string (`format(d, "f")`), `str(value)` otherwise, `None` as JSON `null`.
fn json_number(value: Option<&Payload>) -> OrderedJson {
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

/// `record_run`: the idempotent `screen_run` upsert (docs/04's audit trail).
async fn record_run(
    conn: &mut PgConnection,
    screen_id: i64,
    result: &ScreenResult,
    definition_hash: &str,
) -> Result<(), WorkerError> {
    let rows: Vec<OrderedJson> = result
        .rows
        .iter()
        .map(|row| {
            let factor = row
                .values
                .iter()
                .find(|(k, _)| k == "sorting_factor")
                .map(|(_, v)| v);
            pyjson::object([
                ("rank", int(row.rank)),
                ("instrument_id", int(row.instrument_id)),
                ("factor_value", json_number(factor)),
            ])
        })
        .collect();
    let results = OrderedJson::Array(rows)
        .to_python_json()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))?;
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
        // Each expectation is CPython 3.12's `str(exc)` for `b.decode("utf-8")`.
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

    #[test]
    fn the_universe_labels_cover_every_slug() {
        for (_, slug) in super::super::quality::UNIVERSES {
            assert!(UNIVERSE_NAMES.iter().any(|(s, _)| *s == slug), "{slug}");
        }
    }
}
