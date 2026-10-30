//! Durable run bookkeeping. Ports `baskfy_worker.ops.is_trading_day`, `begin_run` and
//! `fail_run` — the half of `ops.py` the nightly task needs. The scheduled checks (reaper,
//! publish deadline, Kite token) are not ported yet.
//!
//! Why the run row is written before the chain: the chain is **one** transaction, so a killed
//! worker would roll its `pipeline_run` row back with it. [`begin_run`] commits the row on its own
//! transaction first; the chain then updates it inside its own.

use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::steps::{RunStatus, insert_run, log_error};

/// `is_trading_day`: whether the exchange had a session on `day`, per the `trading_day`
/// calendar. **A date the calendar does not carry is not a trading day.** (Python reads the row
/// without an exchange filter; so does this.)
///
/// # Errors
/// The database's.
pub async fn is_trading_day(conn: &mut PgConnection, day: NaiveDate) -> Result<bool, WorkerError> {
    let found: Option<(bool,)> =
        sqlx::query_as("SELECT is_trading_day FROM trading_day WHERE date = $1")
            .bind(day)
            .fetch_optional(&mut *conn)
            .await?;
    Ok(found.is_some_and(|(b,)| b))
}

/// `begin_run`: insert (or reuse a `running`) `pipeline_run` row for `trade_date`; the caller
/// commits it before the chain starts.
///
/// # Errors
/// The database's.
pub async fn begin_run(conn: &mut PgConnection, trade_date: NaiveDate) -> Result<i64, WorkerError> {
    let existing: Option<(i64,)> = sqlx::query_as(
        "SELECT id FROM pipeline_run WHERE trade_date = $1 AND status = $2 \
         ORDER BY id DESC LIMIT 1",
    )
    .bind(trade_date)
    .bind(RunStatus::Running.value())
    .fetch_optional(&mut *conn)
    .await?;
    if let Some((id,)) = existing {
        return Ok(id);
    }
    Ok(insert_run(conn, trade_date).await?.id)
}

/// `fail_run`: mark a run failed from outside the transaction that was running it — never
/// overwriting an outcome somebody already recorded. Returns whether a row was found.
///
/// # Errors
/// The database's.
pub async fn fail_run(
    conn: &mut PgConnection,
    run_id: i64,
    error: &str,
) -> Result<bool, WorkerError> {
    let row: Option<(String,)> = sqlx::query_as("SELECT status FROM pipeline_run WHERE id = $1")
        .bind(run_id)
        .fetch_optional(&mut *conn)
        .await?;
    let Some((status,)) = row else {
        return Ok(false);
    };
    if status != RunStatus::Running.value() {
        return Ok(true);
    }
    use baskfy_providers::clock::Clock as _;
    sqlx::query("UPDATE pipeline_run SET status = $1, finished_at = $2 WHERE id = $3")
        .bind(RunStatus::Failed.value())
        .bind(baskfy_providers::clock::system_clock().now())
        .bind(run_id)
        .execute(&mut *conn)
        .await?;
    log_error(&format!(
        "pipeline run failed run_id={run_id} error={error}"
    ));
    Ok(true)
}
