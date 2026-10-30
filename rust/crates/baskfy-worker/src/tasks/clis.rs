//! Lane **clis** — the desk tasks Beat no longer schedules (two refusal stubs and `--check`), and
//! the worker's command-line modules (`*_cli.py`), which become `baskfy-worker` subcommands.
//!
//! Python: `tasks/desk.py` (`_refused` returns exit code 2; `daily_check` runs the desk's
//! `scripts.daily --check` — the desk is being ported in its own workflow); the CLIs listed in
//! [`CLI_MODULES`].
//!
//! **Declared stubs** (wave-6 foundation, DECISIONS-RS R8.1): each function returns
//! [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until the lane lands its body
//! and adds the task's name to [`PORTED`]. The signatures are the registry's contract; a lane
//! adds private submodules here (`mod x;` → `tasks/clis/x.rs`) and never edits `lib.rs`,
//! `tasks.rs`, the registry or the Beat table. Grade with `tools/parity/task_diff.py --cases
//! tools/parity/task_cases/clis.json`.

use super::{BoundArgs, Lane, TaskContext, TaskResult, not_ported};

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[];

const LANE: Lane = Lane::Clis;

/// The worker's command-line modules (`python -m baskfy_worker.<module>`), each to become a
/// `baskfy-worker` subcommand. `pipeline_cli` is partly there (`nightly`, `step`, `catch-up`).
pub const CLI_MODULES: &[&str] = &[
    "cb_metrics_cli",
    "eq_bars_cli",
    "factors_cli",
    "fno_cli",
    "fundamentals_cli",
    "kite_session_cli",
    "options_cli",
    "pipeline_cli",
    "reconcile_cli",
    "swing_cli",
    "twt_cli",
    "vbt_cli",
];

/// Ports `baskfy_worker.tasks.desk.run_desk_daily` — `baskfy.desk.daily(source="schedule")`.
///
/// # Errors
/// [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until this lane lands it.
pub async fn run_desk_daily(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = (ctx, args);
    not_ported("baskfy.desk.daily", LANE)
}

/// Ports `baskfy_worker.tasks.desk.run_desk_autorun` — `baskfy.desk.autorun()`.
///
/// # Errors
/// [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until this lane lands it.
pub async fn run_desk_autorun(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = (ctx, args);
    not_ported("baskfy.desk.autorun", LANE)
}

/// Ports `baskfy_worker.tasks.desk.check_desk_daily` — `baskfy.desk.daily_check()`.
///
/// # Errors
/// [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until this lane lands it.
pub async fn check_desk_daily(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = (ctx, args);
    not_ported("baskfy.desk.daily_check", LANE)
}
