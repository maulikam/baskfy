//! Lane **api_routes** — tasks whose bodies are API modules the API enqueues: the admin resync,
//! the overlap catalyst scan, the account purge (wave 6; DECISIONS-RS R8.ar1–R8.ar6).
//!
//! | Celery name | Python | Rust | State |
//! |---|---|---|---|
//! | `baskfy.accounts.purge` | `purge_accounts.run_purge_accounts` | [`purge`] | ported |
//! | `baskfy.ops.resync` | `resync.run_resync` (inspection: `baskfy_api.resync.inspect_pending`) | [`resync`], over the API's own `inspect_pending` | ported |
//! | `baskfy.overlap.catalyst_scan` | `overlap_catalyst_scan_task` (`baskfy_api.overlap`, `swing_catalyst`) | [`catalyst_scan`], over the API's own overlap view | **gated** (R8.ar4): the feed's loop is the `swing_eod` lane's, unreachable with `symbols=` |
//!
//! Each Python `run_in_session` / `session_scope` is one transaction here. Grade with
//! `tools/parity/task_diff.py --cases tools/parity/task_cases/api_routes.json`.
//!
//! **Law 2.** The resync reaches ingestion, the trading calendar, the Kite *session* bridge (the
//! `ops_alerts` lane's) and the worker's own Celery queue; the purge erases accounts; the scan
//! reads filings. None reaches an order, a GTT or a Kite write endpoint.

mod catalyst_scan;
mod purge;
#[cfg(test)]
mod purge_db_tests;
mod resync;

use super::{BoundArgs, TaskContext, TaskResult};
use crate::steps::StepOutcome;

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.accounts.purge",
    "baskfy.ops.resync",
    "baskfy.overlap.catalyst_scan",
];

/// Ports `baskfy_worker.tasks.celery_tasks.resync_task` — `baskfy.ops.resync(actor_user_id, days)`:
/// [`resync::run_resync`] (inspect, repair, re-inspect, record). Not auto-retried, as in Python.
///
/// # Errors
/// The database's; a wrong-typed argument's `TypeError`.
pub async fn resync_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let actor_user_id = args.int("actor_user_id")?;
    let days = args.int("days")?;
    let outcome = resync::run_resync(ctx, actor_user_id, days).await?;
    Ok(resync::payload(&outcome))
}

/// Ports `baskfy_worker.tasks.celery_tasks.overlap_catalyst_scan_task` — `baskfy.overlap.catalyst_scan(user_id, scope="actionable")`.
///
///
/// # Errors
/// Redis's and the scan's own.
pub async fn overlap_catalyst_scan_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let user_id = args.int("user_id")?;
    let scope = args.str("scope")?;
    catalyst_scan::run(ctx, user_id, &scope).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.purge_accounts_task` — `baskfy.accounts.purge()`:
/// one transaction over [`purge::run_purge_accounts`] at the task clock's UTC moment.
///
/// # Errors
/// The database's (the transaction is rolled back).
pub async fn purge_accounts_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let mut tx = ctx.db.begin().await?;
    let mut outcome = StepOutcome::default();
    let purged = purge::run_purge_accounts(&mut tx, &mut outcome, ctx.now_utc()).await?;
    tx.commit().await?;
    Ok(purge::payload(purged, &outcome))
}
