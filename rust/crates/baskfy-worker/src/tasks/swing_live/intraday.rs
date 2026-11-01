//! The login scan's request (`celery_tasks.request_login_scan` over the API's
//! `swing_scan.request_scan`) and the plan rebuilt against the session in progress
//! (`tasks/swing_intraday.py`, `celery_tasks._rebuild_intraday_plan`).
//!
//! The request makes one current-session `sw_scan_run` row, or explains why a login needs none:
//! nothing published, a session that is not in progress, a scan already in flight (409), one a
//! minute (429). The row is this task's own (its `task_id` stamped before the commit — AF C.1),
//! so nothing is published.
//!
//! The rebuild re-runs the evening's own sequence against the provisional session (`auto_watch
//! → manage_open_positions → watch_items → build_entries → assemble → store_plan(source=
//! "INTRADAY")`), every piece the `swing_eod` lane's; it does not settle the ladder and does not
//! count a session. **Law 2:** the plan is a row a person confirms; nothing here sends it.

use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDateTime, Utc};
use sqlx::PgConnection;

use super::TaskContext;
use super::scan::{decide_session, is_not_runnable};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};

/// What `request_login_scan` answered: `{"run_id": id}` for the row it made, or a
/// `{"skipped": ...}` payload. A 409/429 refusal carries the *existing* row's `run_id` among its
/// extras, and the task reads any integer `run_id` as "run this row" — so Python re-runs the
/// in-flight or rate-limiting row. Reproduced on purpose (DECISIONS-RS, lane swing_live).
pub(super) type LoginRequest = OrderedJson;

/// `IN_FLIGHT`.
const IN_FLIGHT: [&str; 2] = ["QUEUED", "RUNNING"];

/// `swing_scan.in_flight`: the scan on its way, if one is.
async fn in_flight(
    conn: &mut PgConnection,
    user_id: i64,
    now: DateTime<Utc>,
    stale_after_seconds: i64,
) -> Result<Option<(i64, String)>, WorkerError> {
    let statuses: Vec<String> = IN_FLIGHT.iter().map(|x| (*x).to_owned()).collect();
    let row: Option<(i64, String)> = sqlx::query_as(
        "SELECT sw_scan_run.id, sw_scan_run.status FROM sw_scan_run \
         WHERE sw_scan_run.user_id = $1 AND sw_scan_run.status = ANY($2) \
         AND sw_scan_run.requested_at > $3 \
         ORDER BY sw_scan_run.requested_at DESC, sw_scan_run.id DESC LIMIT 1",
    )
    .bind(user_id)
    .bind(&statuses)
    .bind(now - chrono::Duration::seconds(stale_after_seconds))
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row)
}

/// `swing_scan.newest_run`.
async fn newest_run(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Option<(i64, DateTime<Utc>)>, WorkerError> {
    let row: Option<(i64, DateTime<Utc>)> = sqlx::query_as(
        "SELECT sw_scan_run.id, sw_scan_run.requested_at FROM sw_scan_run \
         WHERE sw_scan_run.user_id = $1 \
         ORDER BY sw_scan_run.requested_at DESC, sw_scan_run.id DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row)
}

/// `timedelta.total_seconds()`.
#[allow(clippy::cast_precision_loss)]
fn total_seconds(d: chrono::Duration) -> f64 {
    let micros = d.num_microseconds().unwrap_or(i64::MAX);
    micros as f64 / 1_000_000.0
}

/// `int(x)` for a float (toward zero).
#[allow(clippy::cast_possible_truncation)]
fn py_int(x: f64) -> i64 {
    x.trunc() as i64
}

/// `request_login_scan(session, user_id=, market_now=, requested_at=, task_id=)`.
///
/// # Errors
/// The database's.
pub(super) async fn request_login_scan(
    ctx: &TaskContext,
    conn: &mut PgConnection,
    user_id: i64,
    market_now: NaiveDateTime,
    requested_at: DateTime<Utc>,
    task_id: &str,
) -> Result<LoginRequest, WorkerError> {
    let decision = match decide_session(conn, market_now).await {
        Ok(d) => d,
        Err(e) if is_not_runnable(&e) => {
            return Ok(object([("skipped", s(e.to_string()))]));
        }
        Err(e) => return Err(e),
    };
    if !decision.provisional {
        return Ok(object([
            ("skipped", s(decision.reason)),
            (
                "session_date",
                s(decision.session_date.format("%Y-%m-%d").to_string()),
            ),
        ]));
    }
    let settings = &ctx.settings;
    if let Some((id, status)) = in_flight(
        conn,
        user_id,
        requested_at,
        settings.swing_scan_stale_after_seconds,
    )
    .await?
    {
        return Ok(object([
            ("skipped", s("scan-in-flight")),
            ("run_id", int(id)),
            ("status_of_run", s(status)),
        ]));
    }
    let min_interval = chrono::Duration::seconds(settings.swing_scan_min_interval_seconds);
    if let Some((id, newest_at)) = newest_run(conn, user_id).await?
        && newest_at > requested_at - min_interval
    {
        let wait = min_interval - (requested_at - newest_at);
        let seconds = py_int(total_seconds(wait) + 0.999).max(1);
        return Ok(object([
            ("skipped", s("rate-limited")),
            ("retry_after", int(seconds)),
            ("run_id", int(id)),
        ]));
    }
    let (run_id,): (i64,) = sqlx::query_as(
        "INSERT INTO sw_scan_run (user_id, requested_at, status, detail, task_id) \
         VALUES ($1, $2, 'QUEUED', CAST($3 AS JSONB), $4) RETURNING sw_scan_run.id",
    )
    .bind(user_id)
    .bind(requested_at)
    .bind(r#"{"source": "broker-login"}"#)
    .bind(task_id)
    .fetch_one(&mut *conn)
    .await?;
    Ok(object([("run_id", int(run_id))]))
}

/// `_rebuild_intraday_plan(user_id, scan)`: only after a scan that detected on **today**.
///
/// # Errors
/// The plan's own (the caller turns any of them into `{"error": ...}`).
pub(super) async fn rebuild_intraday_plan(
    ctx: &TaskContext,
    user_id: i64,
    scan: &[(String, OrderedJson)],
) -> Result<OrderedJson, WorkerError> {
    let get = |key: &str| scan.iter().find(|(k, _)| k == key).map(|(_, v)| v);
    if !matches!(get("provisional"), Some(OrderedJson::Bool(true))) {
        return Ok(object([(
            "skipped",
            s("the scan was not of a session in progress"),
        )]));
    }
    let Some(OrderedJson::Str(session_date)) = get("session_date") else {
        return Ok(object([(
            "skipped",
            s("the scan recorded no session date"),
        )]));
    };
    let on = super::super::args::fromisoformat_date(session_date)?;
    let now = ctx.now_utc();
    let mut tx = ctx.db.begin().await?;
    let report = super::premarket::build_intraday_plan(
        ctx,
        &mut tx,
        user_id,
        on,
        now,
        ctx.worker.swing_execution_enabled,
    )
    .await?;
    tx.commit().await?;
    Ok(report)
}
