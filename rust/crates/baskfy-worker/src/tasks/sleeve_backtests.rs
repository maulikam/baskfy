//! Lane **sleeve_backtests** — the sleeves' research backtests (swing, VBT, options).
//!
//! Python: `tasks/swing_backtest.run_and_commit`, `tasks/vbt_backtest.run_vbt_backtest`,
//! `options/backtest_run.run_backtest`. The cores are ported and golden-graded (waves 2–3); these
//! are the I/O around them.
//!
//! **Declared stubs** (wave-6 foundation, DECISIONS-RS R8.1): each function returns
//! [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until the lane lands its body
//! and adds the task's name to [`PORTED`]. The signatures are the registry's contract; a lane
//! adds private submodules here (`mod x;` → `tasks/sleeve_backtests/x.rs`) and never edits `lib.rs`,
//! `tasks.rs`, the registry or the Beat table. Grade with `tools/parity/task_diff.py --cases
//! tools/parity/task_cases/sleeve_backtests.json`.

mod common;
#[cfg(test)]
mod db_tests;
mod options;
mod swing;
mod swing_core;
mod twt;
mod vbt;

pub use twt::{SOURCE_PLANT as TWT_SOURCE_PLANT, TwtBacktestArgs, TwtRun, run_twt_backtest};

use super::{BoundArgs, TaskContext, TaskResult};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use baskfy_db::OrderedJson;

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.swing.backtest",
    "baskfy.vbt.backtest",
    "baskfy.options.backtest",
];

/// Ports `baskfy_worker.tasks.celery_tasks.swing_backtest_task` — `baskfy.swing.backtest(start=None, end=None, sleeve_inr=None, cost_pct_per_side=None)`.
///
/// # Errors
/// [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until this lane lands it.
pub async fn swing_backtest_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let first = common::date_if_given(&args, "start")?.unwrap_or_else(swing::default_start);
    // `end` defaults to today in IST.
    let last = common::date_if_given(&args, "end")?.unwrap_or_else(|| ctx.today_ist());
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.swing_user_id else {
        return Ok(object([
            ("start", s(common::iso_date(first))),
            ("skipped", s("no BASKFY_SOLE_USER_ID configured")),
        ]));
    };
    // `Decimal(x) if x else None`, read inside the session as Python's body reads them.
    let sleeve = common::decimal_if_given(&args, "sleeve_inr")?;
    let cost = common::decimal_if_given(&args, "cost_pct_per_side")?;
    swing::run_and_commit(ctx, user_id, first, last, sleeve, cost).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_backtest_task` — `baskfy.vbt.backtest(start=None, end=None)`.
///
/// # Errors
/// [`WorkerError::TaskNotPorted`](crate::WorkerError::TaskNotPorted) until this lane lands it.
pub async fn vbt_backtest_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    // `deps = build_pipeline_dependencies()` first, as Python builds it before the user check.
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.vbt_user_id else {
        return Ok(object([(
            "skipped",
            s("no BASKFY_SOLE_USER_ID configured"),
        )]));
    };
    // `run_in_session`: one transaction, committed on return, rolled back on a raise.
    let mut tx = ctx.db.begin().await?;
    let start = common::date_if_given(&args, "start")?.unwrap_or_else(vbt::default_start);
    let end = common::date_if_given(&args, "end")?;
    let row = vbt::run_vbt_backtest(&mut tx, ctx, user_id, start, end).await?;
    tx.commit().await?;
    Ok(object([
        ("run_id", int(row.id)),
        ("source", s(vbt::SOURCE_PLANT)),
        (
            "finished_at",
            row.finished_at
                .map_or(OrderedJson::Null, |at| s(common::iso_utc(at))),
        ),
        ("drift", row.drift.unwrap_or(OrderedJson::Null)),
    ]))
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_backtest_task` — `baskfy.options.backtest(sleeve, tier, date_from, date_to=None)`.
///
/// # Errors
/// What Python raises: a bad date, an unknown sleeve (`ValueError`) or tier (`KeyError`), a run
/// that ends before it starts, a database or core failure (the transaction rolls back).
pub async fn options_backtest_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let start = match args.value("date_from") {
        Some(OrderedJson::Str(text)) => crate::tasks::args::fromisoformat_date(text)?,
        _ => {
            return Err(WorkerError::python(
                "TypeError",
                "fromisoformat: argument must be str",
            ));
        }
    };
    let end = common::date_if_given(&args, "date_to")?.unwrap_or_else(|| ctx.today_ist());
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(object([
            ("from", s(common::iso_date(start))),
            ("skipped", s("no BASKFY_SOLE_USER_ID configured")),
        ]));
    };
    let mut tx = ctx.db.begin().await?;
    let sleeve = options::sleeve_of(args.value("sleeve").unwrap_or(&OrderedJson::Null))?;
    let tier = options::tier_of(args.value("tier").unwrap_or(&OrderedJson::Null))?;
    let run = options::OptionsRun {
        user_id,
        sleeve,
        tier,
        start,
        end,
        options: baskfy_core::options::config::OptionsConfig::default(),
        ceilings: options::ceilings(&ctx.worker),
        sha: options::git_sha().await,
    };
    let report = options::run_backtest(&mut tx, &run).await?;
    tx.commit().await?;
    Ok(report)
}
