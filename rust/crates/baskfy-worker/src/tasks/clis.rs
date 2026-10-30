//! Lane **clis** — the desk tasks Beat no longer schedules (two refusal stubs and `--check`), and
//! the worker's command-line modules (`*_cli.py`), which are `baskfy-worker` subcommands
//! ([`cli`]).
//!
//! Python: `tasks/desk.py` (`_refused` returns exit code 2; `daily_check` runs the desk's
//! `scripts.daily --check`); the CLIs listed in [`CLI_MODULES`].

use super::{BoundArgs, TaskContext, TaskResult};

mod cb_metrics;
pub mod cli;
mod desk;
mod eq_bars;
mod factors;
mod fno;
mod fundamentals;
mod kite_session;
mod options;
mod pipeline;
mod reconcile;
mod reference_backfill;
mod swing;
mod twt;
mod vbt;

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.desk.daily",
    "baskfy.desk.autorun",
    "baskfy.desk.daily_check",
];

/// The worker's command-line modules (`python -m baskfy_worker.<module>`), each a
/// `baskfy-worker` subcommand ([`cli::SUBCOMMANDS`]).
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
    "reference_backfill",
    "swing_cli",
    "twt_cli",
    "vbt_cli",
];

/// Ports `baskfy_worker.tasks.desk.run_desk_daily` — `baskfy.desk.daily(source="schedule")`.
///
/// # Errors
/// None: a refusal is a result, as in Python.
pub async fn run_desk_daily(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = ctx;
    let source = args
        .value("source")
        .cloned()
        .unwrap_or_else(|| baskfy_db::OrderedJson::Str("schedule".to_owned()));
    Ok(desk::refused(
        "daily",
        vec![
            baskfy_db::OrderedJson::Str("--quiet".to_owned()),
            baskfy_db::OrderedJson::Str("--source".to_owned()),
            source,
        ],
    ))
}

/// Ports `baskfy_worker.tasks.desk.run_desk_autorun` — `baskfy.desk.autorun()`.
///
/// # Errors
/// None: a refusal is a result, as in Python.
pub async fn run_desk_autorun(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = (ctx, args);
    Ok(desk::refused("autorun", Vec::new()))
}

/// Ports `baskfy_worker.tasks.desk.check_desk_daily` — `baskfy.desk.daily_check()`.
///
/// # Errors
/// None: a missing tree or a failing check is reported in the result, never raised.
pub async fn check_desk_daily(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = (ctx, args);
    Ok(desk::run_entry(&desk::desk_root(), "daily", &["--check"]).await)
}
