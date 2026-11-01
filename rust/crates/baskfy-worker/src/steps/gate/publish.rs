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
//! The screen executor itself is `baskfy_api::screener` (R8.ar6), ranking-engine path included.

use baskfy_api::screener::{self as api_screener, NoCacheError, RunError, ScreenCache};
use baskfy_core::screener::{
    self as core_screener, MAX_RESULT_ROWS, ScreenDefinition, ScreenError, ScreenResult,
};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use redis::AsyncCommands as _;
use sqlx::{PgConnection, Row as _};

use crate::error::WorkerError;
use crate::pyjson::{self, int};
use crate::steps::{PipelineRun, RunStatus, StepOutcome};

pub(crate) use baskfy_api::screener::{SCREEN_CACHE_FALLBACK_TTL_SECONDS, WARM_CACHE_SCREEN_LIMIT};

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
    Ok(api_screener::current_data_version(conn).await?)
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
/// Python class. The executor is the API's (`baskfy_api::screener`, R8.ar6).
pub(crate) async fn execute_screen_raising(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
    columns: &[String],
    requested_as_of: Option<NaiveDate>,
) -> Result<ScreenResult, WorkerError> {
    api_screener::execute_screen::<NoCacheError>(
        conn,
        definition,
        as_of,
        data_version,
        columns,
        requested_as_of,
        MAX_RESULT_ROWS,
    )
    .await
    .map_err(raised)
}

/// `execute_screen(..., limit=...)` with the caller's `limit` (the backtest loader).
pub(crate) async fn execute_screen_with_limit(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
    columns: &[String],
    limit: i64,
) -> Result<ScreenResult, WorkerError> {
    api_screener::execute_screen::<NoCacheError>(
        conn,
        definition,
        as_of,
        data_version,
        columns,
        None,
        limit,
    )
    .await
    .map_err(raised)
}

/// `record_run(session, screen_id, result, definition_hash)`.
pub(crate) async fn record_screen_run(
    conn: &mut PgConnection,
    screen_id: i64,
    result: &ScreenResult,
    definition_hash: &str,
) -> Result<(), WorkerError> {
    api_screener::record_run::<NoCacheError>(conn, screen_id, result, definition_hash)
        .await
        .map_err(raised)
}

/// A run error raised with its Python class.
fn raised<C: std::fmt::Display>(e: RunError<C>) -> WorkerError {
    match e {
        RunError::Screen(e) => screen_raise(&e),
        RunError::AsOfOutOfRange { .. } => WorkerError::python("AsOfOutOfRange", e.message()),
        RunError::NoPublishedData(m) => WorkerError::python("NoPublishedData", m),
        RunError::Raised(class, m) => WorkerError::python(class, m),
        RunError::Db(e) => e.into(),
        RunError::Cache(c) => WorkerError::python("RedisError", c.to_string()),
    }
}

/// The warm-up body over `limit` screens.
async fn warm_screen_cache_limited(
    conn: &mut PgConnection,
    client: &redis::Client,
    limit: i64,
) -> Result<WarmCacheResult, WarmStop> {
    let (targets, unreadable) = top_screen_definitions(conn, limit).await?;
    // redis-py connects on a command, not on construction: the first `GET` opens it.
    let mut cache = WorkerCache { client, con: None };
    let mut warmed = 0;
    let mut skipped = 0;
    let mut failures = unreadable;
    for target in &targets {
        let ran = match api_screener::run_screen(
            conn,
            &target.definition,
            None,
            &target.columns,
            Some(&mut cache),
            SCREEN_CACHE_FALLBACK_TTL_SECONDS,
        )
        .await
        {
            Ok(ran) => ran,
            Err(e) => match run_stop(e) {
                RunStop::Caught(message) => {
                    failures.push(format!("{}: {message}", target.public_id));
                    continue;
                }
                RunStop::Stop(stop) => return Err(stop),
            },
        };
        if let Some(result) = &ran.result {
            record_screen_run(
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

/// The worker's screen cache: one lazily opened multiplexed connection.
struct WorkerCache<'a> {
    client: &'a redis::Client,
    con: Option<redis::aio::MultiplexedConnection>,
}

impl WorkerCache<'_> {
    async fn con(&mut self) -> Result<&mut redis::aio::MultiplexedConnection, redis::RedisError> {
        if self.con.is_none() {
            self.con = Some(self.client.get_multiplexed_async_connection().await?);
        }
        self.con
            .as_mut()
            .ok_or_else(|| redis::RedisError::from((redis::ErrorKind::IoError, "no connection")))
    }
}

impl ScreenCache for WorkerCache<'_> {
    type Error = redis::RedisError;

    async fn get(&mut self, key: &str) -> Result<Option<Vec<u8>>, redis::RedisError> {
        let con = self.con().await?;
        con.get(key).await
    }

    async fn set_ex(&mut self, key: &str, value: &str, ttl: u64) -> Result<(), redis::RedisError> {
        let con = self.con().await?;
        con.set_ex(key, value, ttl).await
    }
}

/// A run that ended: Python's `except (ValueError, NoPublishedData)` (caught by the warm-up), or
/// anything else (propagated).
enum RunStop {
    Caught(String),
    Stop(WarmStop),
}

/// `ScreenQueryError`, pydantic's `ValidationError`, `AsOfOutOfRange`, `UnicodeDecodeError` and
/// `NoPublishedData` are caught by the warm-up; `TypeError` and `KeyError` are not.
fn run_stop(e: RunError<redis::RedisError>) -> RunStop {
    match e {
        RunError::Screen(
            ref s @ (ScreenError::Query(_) | ScreenError::Value(_) | ScreenError::Validation(_)),
        ) => RunStop::Caught(s.to_string()),
        RunError::Screen(other) => RunStop::Stop(WarmStop::Raised(screen_raise(&other))),
        RunError::AsOfOutOfRange { .. } | RunError::NoPublishedData(_) => {
            RunStop::Caught(e.message())
        }
        RunError::Raised(class @ ("ValueError" | "UnicodeDecodeError"), m) => {
            let _ = class;
            RunStop::Caught(m)
        }
        RunError::Raised(class, m) => {
            RunStop::Stop(WarmStop::Raised(WorkerError::python(class, m)))
        }
        RunError::Db(e) => RunStop::Stop(WarmStop::Raised(e.into())),
        RunError::Cache(c) => RunStop::Stop(WarmStop::Cache(c)),
    }
}

#[cfg(test)]
mod tests {
    use baskfy_api::screener::UNIVERSE_NAMES;

    #[test]
    fn the_universe_labels_cover_every_slug() {
        for (_, slug) in super::super::quality::UNIVERSES {
            assert!(UNIVERSE_NAMES.iter().any(|(s, _)| *s == slug), "{slug}");
        }
    }
}
