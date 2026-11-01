//! Lane **screener** — the tasks that run saved screens: the out-of-band cache warm and the nightly
//! screen-alert email, and the screener run itself as a pub surface for the other lanes.
//!
//! | Celery name | Python | Rust |
//! |---|---|---|
//! | `baskfy.compute.warm_screen_cache` | `celery_tasks.warm_screen_cache_task` → `baskfy_api.screener.warm_screen_cache` | [`warm_screen_cache_task`] over the publish step's warm-up (`steps::gate`, reused, never forked) |
//! | `baskfy.alerts.dispatch` | `celery_tasks.dispatch_screen_alerts_task` → `tasks/alerts.run_alert_dispatch` → `baskfy_api.alerts.dispatch` | [`dispatch_screen_alerts_task`] (`screener/dispatch.rs`, the diff in `screener/diff.rs`, the email in `screener/alert_mail.rs`) |
//!
//! `tasks/ranking.py` and `tasks/basket.py` are nightly steps, not tasks: wave 5 ported them
//! (`steps/compute/ranking.rs`, `steps/sleeves/basket.rs`) and this lane reuses, never forks.
//!
//! **The screener run** ([`execute_screen`], [`record_run`], [`warm_screen_cache`]): the legacy
//! docs/06 statement, the desk-score columns, provenance, the Redis cache and the `screen_run`
//! upsert, exactly as `publish` runs them (the executor is `baskfy_api::screener`, R8.ar6).
//!
//! Each Python `run_in_session` is one transaction: commit on return, roll back on raise.
//! **Law 2:** nothing here reaches an order, a GTT or a Kite endpoint.

mod alert_mail;
mod diff;
mod dispatch;

use baskfy_api::mailer::Mailer;
use baskfy_core::screener::{ScreenDefinition, ScreenResult};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use sqlx::PgConnection;

use super::args::fromisoformat_date;
use super::{BoundArgs, TaskContext, TaskResult};
use crate::error::WorkerError;
use crate::pyjson::{int, object, strings};
use crate::steps::gate::{
    WARM_CACHE_SCREEN_LIMIT, WarmCacheResult, WarmFailure, execute_screen_raising,
    record_screen_run, warm_screen_cache_with_limit,
};

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &["baskfy.compute.warm_screen_cache", "baskfy.alerts.dispatch"];

/// `WARM_CACHE_SCREEN_LIMIT`: "warm the top 200 most-run screen definitions".
pub const DEFAULT_WARM_LIMIT: i64 = WARM_CACHE_SCREEN_LIMIT;

/// `WarmCacheResult`, as the task returns it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WarmSummary {
    /// Screens computed and cached.
    pub warmed: i64,
    /// Screens already cached (a hit).
    pub skipped: i64,
    /// Screens that could not be read or run, each `"<public_id>: <why>"`.
    pub failures: Vec<String>,
}

impl From<WarmCacheResult> for WarmSummary {
    fn from(r: WarmCacheResult) -> Self {
        Self {
            warmed: r.warmed,
            skipped: r.skipped,
            failures: r.failures,
        }
    }
}

/// `screener.execute_screen(session, definition, as_of=, data_version=, columns=,
/// requested_as_of=)`: one statement over `factor_daily` for `as_of`, every error raised with
/// its Python class.
///
/// # Errors
/// The screener's (`ScreenQueryError`, `ValidationError`, ...), the database's.
pub async fn execute_screen(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
    columns: &[String],
    requested_as_of: Option<NaiveDate>,
) -> Result<ScreenResult, WorkerError> {
    execute_screen_raising(
        conn,
        definition,
        as_of,
        data_version,
        columns,
        requested_as_of,
    )
    .await
}

/// `screener.record_run(session, screen_id, result, definition_hash)`: the idempotent
/// `screen_run` upsert (docs/04's audit trail).
///
/// # Errors
/// The database's.
pub async fn record_run(
    conn: &mut PgConnection,
    screen_id: i64,
    result: &ScreenResult,
    definition_hash: &str,
) -> Result<(), WorkerError> {
    record_screen_run(conn, screen_id, result, definition_hash).await
}

/// `screener.warm_screen_cache(session, cache, limit=limit)`: the `limit` most-run screens run
/// for the current `data_version`, each cached (48 h) and recorded in `screen_run`; a stored
/// definition that no longer validates, or a screen that fails with a `ValueError`, is listed in
/// `failures` and skipped.
///
/// # Errors
/// A Redis failure (as redis-py's class and message), any other raise.
pub async fn warm_screen_cache(
    conn: &mut PgConnection,
    client: &redis::Client,
    limit: i64,
) -> Result<WarmSummary, WorkerError> {
    match warm_screen_cache_with_limit(conn, client, limit).await {
        Ok(r) => Ok(r.into()),
        Err(WarmFailure::Raised(e)) => Err(e),
        Err(WarmFailure::Cache(e)) => Err(redis_raise(client, &e)),
    }
}

/// A Redis failure as redis-py raises it from the asyncio client: a refused connection is
/// `ConnectionError("Error <errno> connecting to <host>:<port>. Connect call failed ('<host>',
/// <port>).")`; anything else keeps redis-rs's text under redis-py's base class.
fn redis_raise(client: &redis::Client, e: &redis::RedisError) -> WorkerError {
    let text = e.to_string();
    if e.is_connection_refusal()
        && let redis::ConnectionAddr::Tcp(host, port) = &client.get_connection_info().addr
    {
        let errno = text
            .rsplit_once("(os error ")
            .and_then(|(_, tail)| tail.split(')').next())
            .and_then(|n| n.parse::<i32>().ok())
            .unwrap_or(61);
        return WorkerError::python(
            "ConnectionError",
            format!(
                "Error {errno} connecting to {host}:{port}. Connect call failed \
                 ('{host}', {port})."
            ),
        );
    }
    let class = if e.is_io_error() || e.is_connection_dropped() {
        "ConnectionError"
    } else if e.is_timeout() {
        "TimeoutError"
    } else {
        "RedisError"
    };
    WorkerError::python(class, text)
}

/// `limit: int` as the task binds it (`int`, or a `bool`, which is an `int` in Python).
fn limit_arg(args: &BoundArgs) -> Result<i64, WorkerError> {
    args.int("limit")
}

/// Ports `baskfy_worker.tasks.celery_tasks.warm_screen_cache_task` —
/// `baskfy.compute.warm_screen_cache(limit=200)`.
///
/// Without a usable cache client (`build_cache()` is `None`: a URL redis-py refuses) the task
/// answers `failures: ["no usable cache client configured"]` and touches nothing. Otherwise the
/// warm-up runs in one transaction; a Redis failure raises (the task is not auto-retried) and
/// rolls back the `screen_run` rows it had written.
///
/// # Errors
/// As [`warm_screen_cache`]; the arguments' `TypeError`.
pub async fn warm_screen_cache_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let limit = limit_arg(&args)?;
    let Ok(client) = redis::Client::open(ctx.worker.redis_url.as_str()) else {
        return Ok(object([
            ("warmed", int(0)),
            ("skipped", int(0)),
            ("failures", strings(["no usable cache client configured"])),
        ]));
    };
    let mut tx = ctx.db.begin().await?;
    let summary = warm_screen_cache(&mut tx, &client, limit).await?;
    tx.commit().await?;
    Ok(object([
        ("warmed", int(summary.warmed)),
        ("skipped", int(summary.skipped)),
        ("failures", strings(summary.failures)),
    ]))
}

/// Python's truth value of a JSON argument.
fn truthy(value: &OrderedJson) -> bool {
    match value {
        OrderedJson::Null => false,
        OrderedJson::Bool(b) => *b,
        OrderedJson::Int(i) => *i != 0,
        OrderedJson::UInt(u) => *u != 0,
        OrderedJson::BigInt(_) => true,
        OrderedJson::Float(f) => *f != 0.0,
        OrderedJson::Str(s) => !s.is_empty(),
        OrderedJson::Array(items) => !items.is_empty(),
        OrderedJson::Object(items) => !items.is_empty(),
    }
}

/// `dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()`.
fn trade_date_arg(ctx: &TaskContext, args: &BoundArgs) -> Result<NaiveDate, WorkerError> {
    match args.value("trade_date") {
        Some(v) if truthy(v) => match v {
            OrderedJson::Str(text) => fromisoformat_date(text),
            _ => Err(WorkerError::python(
                "TypeError",
                "fromisoformat: argument must be str",
            )),
        },
        _ => Ok(ctx.today_ist()),
    }
}

/// Ports `baskfy_worker.tasks.celery_tasks.dispatch_screen_alerts_task` —
/// `baskfy.alerts.dispatch(trade_date=None)`.
///
/// Sends every screen alert due for the date (today in IST when none is given), records each send
/// or skip in `screen_alert_delivery` (unique on `(alert, as_of)`: a re-run sends nothing), and
/// enqueues the webhooks. One transaction (`run_in_session`); a raise rolls everything back —
/// the mail already handed to the transport stays sent, as in Python.
///
/// # Errors
/// A bad date (`ValueError`/`TypeError`), a screen that raises, `RuntimeError` with no secret to
/// sign an unsubscribe link, the database's.
pub async fn dispatch_screen_alerts_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = trade_date_arg(ctx, &args)?;
    let mailer = Mailer::from_settings(&ctx.settings);
    let moment = ctx.now_utc();
    let mut tx = ctx.db.begin().await?;
    let report = dispatch::dispatch(&mut tx, day, &mailer, &ctx.settings, moment).await?;
    tx.commit().await?;
    Ok(report)
}

#[cfg(test)]
mod tests {
    //! Ports `services/worker/tests/test_alert_tasks.py` (the wiring) and the task-level
    //! argument rules; the dispatch and the warm-up are graded on the bed by `task_diff.py`.
    #![allow(clippy::unwrap_used)]
    use super::*;
    use crate::tasks::{Lane, Queue, beat, find};

    #[test]
    fn both_tasks_are_registered_to_this_lane_and_ported() {
        for name in PORTED {
            let spec = find(name).unwrap();
            assert_eq!(spec.lane, Lane::Screener);
            assert!(spec.is_ported());
        }
    }

    #[test]
    fn the_alert_task_routes_to_the_default_queue_and_the_warm_up_to_compute() {
        assert_eq!(
            find("baskfy.alerts.dispatch").unwrap().queue_by_name(),
            Queue::Default
        );
        assert_eq!(
            find("baskfy.compute.warm_screen_cache")
                .unwrap()
                .queue_by_name(),
            Queue::Compute
        );
    }

    #[test]
    fn the_dispatch_runs_after_the_publish_deadline_on_weekdays_only() {
        let at = |s: &str| chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M").unwrap();
        let due = |s: &str| {
            beat::due_at(at(s))
                .unwrap()
                .iter()
                .any(|e| e.task == "baskfy.alerts.dispatch")
        };
        assert!(due("2026-10-30T20:30")); // Friday
        assert!(due("2026-10-26T20:30")); // Monday
        assert!(!due("2026-10-31T20:30")); // Saturday
        assert!(!due("2026-10-30T18:00"));
    }

    #[test]
    fn python_truthiness_decides_whether_a_date_was_given() {
        assert!(!truthy(&OrderedJson::Null));
        assert!(!truthy(&OrderedJson::Str(String::new())));
        assert!(!truthy(&OrderedJson::Int(0)));
        assert!(!truthy(&OrderedJson::Array(Vec::new())));
        assert!(truthy(&OrderedJson::Int(20_261_029)));
        assert!(truthy(&OrderedJson::Str("2026-10-29".to_owned())));
    }

    #[test]
    fn a_refused_connection_is_worded_as_redis_py_words_it() {
        let client = redis::Client::open("redis://127.0.0.1:18801/1").unwrap();
        let e = redis::RedisError::from(std::io::Error::from_raw_os_error(61));
        let raised = redis_raise(&client, &e);
        assert_eq!(raised.python_type(), "ConnectionError");
        assert_eq!(
            raised.to_string(),
            "Error 61 connecting to 127.0.0.1:18801. Connect call failed ('127.0.0.1', 18801)."
        );
    }
}
