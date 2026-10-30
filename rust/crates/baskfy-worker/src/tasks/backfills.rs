//! Lane **backfills** — the bhavcopy's own 18:15 ingest and the 15:45 equity-bars session job.
//!
//! Python: `bhavcopy_backfill.backfill_bars_from_bhavcopy` (the ingest lane ported the part step 2
//! needs, `steps/ingest/bars.rs`; this lane reuses it, never forks it), `eq_bars.py`.
//!
//! **Declared stubs** (wave-6 foundation, DECISIONS-RS R8.1): each function returns
//! [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until the lane lands its body
//! and adds the task's name to [`PORTED`]. The signatures are the registry's contract; a lane
//! adds private submodules here (`mod x;` → `tasks/backfills/x.rs`) and never edits `lib.rs`,
//! `tasks.rs`, the registry or the Beat table. Grade with `tools/parity/task_diff.py --cases
//! tools/parity/task_cases/backfills.json`.

use super::{BoundArgs, Lane, TaskContext, TaskResult, not_ported};

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[];

const LANE: Lane = Lane::Backfills;

/// Ports `baskfy_worker.tasks.celery_tasks.bhavcopy_ingest` — `baskfy.pipeline.bhavcopy_ingest(trade_date=None)`.
///
/// # Errors
/// [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until this lane lands it.
pub async fn bhavcopy_ingest(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = (ctx, args);
    not_ported("baskfy.pipeline.bhavcopy_ingest", LANE)
}

/// Ports `baskfy_worker.tasks.celery_tasks.eq_bars_session_task` — `baskfy.eq_bars.session(trade_date=None)`.
///
/// # Errors
/// [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until this lane lands it.
pub async fn eq_bars_session_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = (ctx, args);
    not_ported("baskfy.eq_bars.session", LANE)
}
