//! Lane **vbt_jobs** — the volume-breakout sleeve's jobs: detection retry, evening and morning
//! plans, the rescan and its sweep, and the four 21:30–21:40 checks.
//!
//! Python: `tasks/vbt.py` (`published_signal_count`, `run_detect_vbt` — nightly step 13's
//! detector, `pipeline_run_id = None` here), `vbt_evening.py`, `vbt_rescan.py`, `vbt_ops.py`,
//! and the VBT half of `live_scan.py`.
//!
//! | Celery name | Python | Rust |
//! |---|---|---|
//! | `baskfy.vbt.detect` | `celery_tasks.vbt_detect_task` (asks `published_signal_count` first) | [`vbt_detect_task`] → wave 5's detector, widened (`run_detect_vbt_for`) |
//! | `baskfy.vbt.evening` | `celery_tasks.vbt_evening_task` → `vbt_evening.run_vbt_evening` | [`vbt_evening_task`] → `evening::run_vbt_evening` |
//! | `baskfy.vbt.morning` | `celery_tasks.vbt_morning_task` (same body, `MORNING`) | [`vbt_morning_task`] |
//! | `baskfy.vbt.rescan` | `celery_tasks.vbt_rescan_task` → `vbt_rescan.run_vbt_rescan` (+ `live_scan.run_vbt_live`) | [`vbt_rescan_task`] → `rescan::run_vbt_rescan` |
//! | `baskfy.vbt.rescan_sweep` | `celery_tasks.vbt_rescan_sweep_task` | [`vbt_rescan_sweep_task`] → `celery::apply_async` |
//! | `baskfy.vbt.check_*` (four) | `celery_tasks._vbt_check` → `vbt_ops.check_*` | `ops::check_*` |
//!
//! Each Python `run_in_session` is one transaction here (commit on return, roll back on raise).
//!
//! **Law 2.** Nothing here places, arms, modifies or cancels an order or a GTT, or calls a Kite
//! write endpoint: the only Kite call is the live re-detect's `GET /quote`. Every plan line is
//! `PROPOSED`; an expired limit the broker holds becomes a `CANCEL_LIMIT` *line*. VBT has **no**
//! auto-execute flag and this lane reads none; `BASKFY_VBT_EXECUTION_ENABLED` only sets a
//! session's mode, the first-live half size and which counter the evening moves.
//!
//! Grade with `tools/parity/task_diff.py --cases tools/parity/task_cases/vbt_jobs.json`.

mod book;
#[cfg(test)]
mod db_tests;
mod evening;
mod live;
mod ops;
mod rescan;
mod sleeve;

use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use sqlx::PgConnection;

use super::args::fromisoformat_date;
use super::twt_jobs::celery;
use super::{BoundArgs, TaskContext, TaskResult};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::StepOutcome;
use crate::steps::sleeves::for_vbt_jobs::run_detect_vbt_for;

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.vbt.detect",
    "baskfy.vbt.evening",
    "baskfy.vbt.morning",
    "baskfy.vbt.rescan",
    "baskfy.vbt.rescan_sweep",
    "baskfy.vbt.check_detect_fresh",
    "baskfy.vbt.check_orders_past_expiry",
    "baskfy.vbt.check_naked_positions",
    "baskfy.vbt.check_positions_without_bars",
];

/// `vbt_rescan.RESCAN_TASK_NAME`: the task the sweep publishes.
const RESCAN_TASK_NAME: &str = "baskfy.vbt.rescan";

const NO_SOLE_USER: &str = "no BASKFY_SOLE_USER_ID configured";

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `dt.date.fromisoformat(trade_date) if trade_date else None`: an absent or falsy value is
/// "today" (Python's truthiness), anything else is parsed.
fn trade_date_arg(args: &BoundArgs) -> Result<Option<NaiveDate>, WorkerError> {
    match args.value("trade_date") {
        None | Some(OrderedJson::Null) => Ok(None),
        Some(OrderedJson::Str(text)) if text.is_empty() => Ok(None),
        Some(OrderedJson::Str(text)) => fromisoformat_date(text).map(Some),
        Some(OrderedJson::Bool(false)) | Some(OrderedJson::Int(0)) => Ok(None),
        Some(_) => Err(WorkerError::python(
            "TypeError",
            "fromisoformat: argument must be str",
        )),
    }
}

/// `int(run_id)`: an int (a bool is one), a float truncated, a decimal string parsed.
fn run_id_arg(args: &BoundArgs) -> Result<i64, WorkerError> {
    match args.value("run_id") {
        Some(OrderedJson::Int(i)) => Ok(*i),
        Some(OrderedJson::Bool(b)) => Ok(i64::from(*b)),
        Some(OrderedJson::Float(f)) if f.is_finite() => format!("{:.0}", f.trunc())
            .parse::<i64>()
            .map_err(|e| WorkerError::python("OverflowError", e.to_string())),
        Some(OrderedJson::Str(text)) => {
            let t = text.trim();
            t.replace('_', "").parse::<i64>().map_err(|_| {
                WorkerError::python(
                    "ValueError",
                    format!(
                        "invalid literal for int() with base 10: {}",
                        baskfy_providers::py::repr_str(text)
                    ),
                )
            })
        }
        Some(OrderedJson::Null) => Err(WorkerError::python(
            "TypeError",
            "int() argument must be a string, a bytes-like object or a real number, not \
             'NoneType'",
        )),
        Some(other) => Err(WorkerError::python(
            "TypeError",
            format!(
                "int() argument must be a string, a bytes-like object or a real number, not \
                 '{}'",
                super::args::py_type_name(other)
            ),
        )),
        None => Err(WorkerError::python(
            "KeyError",
            "'run_id' is not a parameter of this task",
        )),
    }
}

/// `published_signal_count`: how many rows this session already has — what the 21:10 retry asks
/// before it works.
async fn published_signal_count(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let (n,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM vb_signal_daily WHERE user_id = $1 AND date = $2")
            .bind(user_id)
            .bind(on)
            .fetch_one(&mut *conn)
            .await?;
    Ok(n)
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_detect_task` — `baskfy.vbt.detect(trade_date=None)`.
///
/// VB4's 21:10 retry and the CLI's entry point: a session that already has rows is answered
/// without work; otherwise wave 5's detector runs with `pipeline_run_id = NULL`. Idempotent per
/// `(user_id, date)` (house rule 7).
///
/// # Errors
/// Python's: a bad date (`ValueError`), the database's.
pub async fn vbt_detect_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = trade_date_arg(&args)?.unwrap_or_else(|| ctx.today_ist());
    let deps = ctx.pipeline_dependencies().await?;
    if !deps.vbt_nightly_enabled {
        return Ok(object([
            ("date", s(iso(day))),
            ("skipped", s("BASKFY_VBT_NIGHTLY_ENABLED is false")),
        ]));
    }
    let Some(user_id) = deps.vbt_user_id else {
        return Ok(object([
            ("date", s(iso(day))),
            ("skipped", s(NO_SOLE_USER)),
        ]));
    };
    let mut tx = ctx.db.begin().await?;
    let already = published_signal_count(&mut tx, user_id, day).await?;
    if already != 0 {
        tx.commit().await?;
        return Ok(object([
            ("date", s(iso(day))),
            ("skipped", s("already detected")),
            ("rows", int(already)),
        ]));
    }
    let mut outcome = StepOutcome::default();
    let signals =
        run_detect_vbt_for(&mut tx, &mut outcome, day, user_id, None, None, false).await?;
    tx.commit().await?;
    Ok(object([
        ("date", s(iso(day))),
        ("signals", int(signals)),
        ("detail", outcome.detail_json()),
    ]))
}

/// The evening/morning body inside an open transaction.
async fn plan_in_session(
    ctx: &TaskContext,
    tx: &mut PgConnection,
    day: NaiveDate,
    user_id: i64,
    execution_enabled: bool,
    source: &str,
) -> TaskResult {
    let mut outcome = StepOutcome::default();
    let inputs = evening::EveningInputs {
        trade_date: day,
        user_id,
        execution_enabled,
        source,
        now: ctx.now_utc(),
    };
    let report = evening::run_vbt_evening(tx, &mut outcome, &inputs).await?;
    Ok(match report {
        Some(report) => OrderedJson::Object(report.as_detail()),
        None => object([("date", s(iso(day))), ("skipped", outcome.detail_json())]),
    })
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_evening_task` — `baskfy.vbt.evening(trade_date=None)`.
///
/// VB6: cancel what expired, queue the exits, plan the entries, settle the session — every line
/// `PROPOSED`.
///
/// # Errors
/// Python's: a bad date (`ValueError`), the database's.
pub async fn vbt_evening_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = trade_date_arg(&args)?.unwrap_or_else(|| ctx.today_ist());
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.vbt_user_id else {
        return Ok(object([
            ("date", s(iso(day))),
            ("skipped", s(NO_SOLE_USER)),
        ]));
    };
    let mut tx = ctx.db.begin().await?;
    let out = plan_in_session(
        ctx,
        &mut tx,
        day,
        user_id,
        deps.vbt_execution_enabled,
        evening::SOURCE_EVENING,
    )
    .await?;
    tx.commit().await?;
    Ok(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_morning_task` — `baskfy.vbt.morning(trade_date=None)`.
///
/// The same plan rebuilt before the open, re-sized against the sleeve as it stands; the signal
/// session defaults to the last tradeable session the detector wrote.
///
/// # Errors
/// Python's: a bad date (`ValueError`), the database's.
pub async fn vbt_morning_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let today = ctx.today_ist();
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.vbt_user_id else {
        return Ok(object([
            ("date", s(iso(today))),
            ("skipped", s(NO_SOLE_USER)),
        ]));
    };
    let mut tx = ctx.db.begin().await?;
    let day = match trade_date_arg(&args)? {
        Some(day) => Some(day),
        None => evening::last_detected_session(&mut tx, user_id, today).await?,
    };
    let Some(day) = day else {
        tx.commit().await?;
        return Ok(object([
            ("date", s(iso(today))),
            ("skipped", s("no detected session to plan from")),
        ]));
    };
    let out = plan_in_session(
        ctx,
        &mut tx,
        day,
        user_id,
        deps.vbt_execution_enabled,
        evening::SOURCE_MORNING,
    )
    .await?;
    tx.commit().await?;
    Ok(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_rescan_task` — `baskfy.vbt.rescan(run_id)`.
///
/// One press of **Re-detect**: the latest published session, or during a session (LV8) today's
/// provisional bar and a `LIVE` plan of `BUY_AT_MARKET` lines. Never raises for the work: a
/// failure is `FAILED` with its reason on the row.
///
/// # Errors
/// An argument `int()` refuses; the database's.
pub async fn vbt_rescan_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let deps = ctx.pipeline_dependencies().await?;
    let run_id = run_id_arg(&args)?;
    let mut tx = ctx.db.begin().await?;
    let out = rescan::run_vbt_rescan(ctx, &mut tx, run_id, deps.vbt_execution_enabled).await?;
    tx.commit().await?;
    Ok(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_rescan_sweep_task` — `baskfy.vbt.rescan_sweep()`.
///
/// Publishes every `QUEUED` `vb_scan_run` row nobody has published (the desk has no Celery
/// client), oldest first, at most [`rescan::PUBLISH_LIMIT`] a pass.
///
/// # Errors
/// The database's; the broker's (`OperationalError`) when a row is waiting and it is down.
pub async fn vbt_rescan_sweep_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let mut tx = ctx.db.begin().await?;
    let rows = rescan::unpublished_runs(&mut tx, rescan::PUBLISH_LIMIT).await?;
    let mut published = Vec::with_capacity(rows.len());
    if !rows.is_empty() {
        let mut broker = celery::broker(ctx.worker.broker_url()).await?;
        for id in rows {
            let task_id = celery::apply_async(
                &mut tx,
                &mut broker,
                RESCAN_TASK_NAME,
                id,
                celery::QUEUE_COMPUTE,
            )
            .await?;
            sqlx::query("UPDATE vb_scan_run SET task_id = $2 WHERE id = $1")
                .bind(id)
                .bind(task_id)
                .execute(&mut *tx)
                .await?;
            published.push(int(id));
        }
    }
    tx.commit().await?;
    Ok(object([("published", OrderedJson::Array(published))]))
}

/// Which of the four checks `_vbt_check` runs.
#[derive(Debug, Clone, Copy)]
enum Check {
    DetectFresh,
    OrdersPastExpiry,
    NakedPositions,
    PositionsWithoutBars,
}

/// `_vbt_check(check)`: no `BASKFY_SOLE_USER_ID`, no sleeve to check — `skipped`, never a check
/// of user zero; otherwise the check in one read-only transaction.
async fn vbt_check(ctx: &TaskContext, check: Check) -> TaskResult {
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.vbt_user_id else {
        return Ok(object([("skipped", s(NO_SOLE_USER))]));
    };
    let inputs = ops::CheckInputs {
        now: ctx.now_utc(),
        user_id,
        settings: &ctx.settings,
    };
    let mut tx = ctx.db.begin().await?;
    let out = match check {
        Check::DetectFresh => ops::check_detect_fresh(&mut tx, &inputs).await?,
        Check::OrdersPastExpiry => ops::check_orders_past_expiry(&mut tx, &inputs).await?,
        Check::NakedPositions => ops::check_naked_positions(&mut tx, &inputs).await?,
        Check::PositionsWithoutBars => ops::check_positions_without_bars(&mut tx, &inputs).await?,
    };
    tx.commit().await?;
    Ok(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_check_detect_fresh_task` — `baskfy.vbt.check_detect_fresh()`.
///
/// 21:30: `VBT_DETECT_STALE` when the detector has written nothing for four days.
///
/// # Errors
/// The database's.
pub async fn vbt_check_detect_fresh_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    vbt_check(ctx, Check::DetectFresh).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_check_orders_past_expiry_task` — `baskfy.vbt.check_orders_past_expiry()`.
///
/// 21:40: `VBT_ORDER_PAST_EXPIRY` for a limit still working past its third session.
///
/// # Errors
/// The database's.
pub async fn vbt_check_orders_past_expiry_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    vbt_check(ctx, Check::OrdersPastExpiry).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_check_naked_positions_task` — `baskfy.vbt.check_naked_positions()`.
///
/// 21:40: `VBT_POSITION_NAKED` for shares open with no resting GTT.
///
/// # Errors
/// The database's.
pub async fn vbt_check_naked_positions_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    vbt_check(ctx, Check::NakedPositions).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.vbt_check_positions_without_bars_task` — `baskfy.vbt.check_positions_without_bars()`.
///
/// 21:40: `VBT_POSITION_NO_BAR` for a held name that has stopped printing.
///
/// # Errors
/// The database's.
pub async fn vbt_check_positions_without_bars_task(
    ctx: &TaskContext,
    args: BoundArgs,
) -> TaskResult {
    let _ = args;
    vbt_check(ctx, Check::PositionsWithoutBars).await
}

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]
    use super::*;
    use crate::tasks::Queue;
    use crate::tasks::beat::{BEAT_SCHEDULE, BeatEntry, Schedule};

    fn args(pairs: Vec<(&'static str, OrderedJson)>) -> BoundArgs {
        BoundArgs::from_pairs(pairs)
    }

    #[test]
    fn an_empty_trade_date_is_today_and_a_bad_one_is_pythons_value_error() {
        assert_eq!(
            trade_date_arg(&args(vec![("trade_date", s(""))])).ok(),
            Some(None)
        );
        assert_eq!(
            trade_date_arg(&args(vec![("trade_date", OrderedJson::Null)])).ok(),
            Some(None)
        );
        let bad = trade_date_arg(&args(vec![("trade_date", s("29-10-2026"))]));
        assert!(matches!(bad, Err(ref e) if e.python_type() == "ValueError"));
        assert_eq!(
            trade_date_arg(&args(vec![("trade_date", s("2026-10-29"))])).ok(),
            Some(NaiveDate::from_ymd_opt(2026, 10, 29))
        );
    }

    #[test]
    fn run_id_is_read_as_int_reads_it() {
        assert_eq!(
            run_id_arg(&args(vec![("run_id", int(118))])).ok(),
            Some(118)
        );
        assert_eq!(run_id_arg(&args(vec![("run_id", s(" 7 "))])).ok(), Some(7));
        assert!(run_id_arg(&args(vec![("run_id", s("x"))])).is_err());
        assert!(run_id_arg(&args(vec![("run_id", OrderedJson::Null)])).is_err());
    }

    fn entry(task: &str) -> &'static BeatEntry {
        BEAT_SCHEDULE
            .iter()
            .find(|e| e.task == task)
            .unwrap_or_else(|| panic!("{task} is not on Beat"))
    }

    /// `(first hour, first minute, weekdays)` of a crontab entry.
    fn when(task: &str) -> (u32, u32, Vec<u32>) {
        let Schedule::Crontab(cron) = entry(task).schedule else {
            panic!("{task} is not a crontab");
        };
        let expanded = cron.expand().unwrap_or_else(|e| panic!("{e}"));
        (
            expanded.hour.iter().copied().min().unwrap_or(99),
            expanded.minute.iter().copied().min().unwrap_or(99),
            expanded.day_of_week.iter().copied().collect(),
        )
    }

    #[test]
    fn each_check_has_a_beat_entry_on_the_default_queue() {
        // test_vbt_ops.py::test_each_check_has_a_beat_entry_on_the_default_queue.
        for task in [
            "baskfy.vbt.check_detect_fresh",
            "baskfy.vbt.check_orders_past_expiry",
            "baskfy.vbt.check_naked_positions",
            "baskfy.vbt.check_positions_without_bars",
        ] {
            assert!(PORTED.contains(&task));
            assert_eq!(crate::tasks::route_by_name(task), Queue::Default, "{task}");
            assert_eq!(when(task).2, vec![1, 2, 3, 4, 5], "{task} runs on weekdays");
        }
    }

    #[test]
    fn the_checks_run_after_the_evening_that_is_supposed_to_fix_things() {
        // test_vbt_ops.py::test_the_checks_run_after_the_evening_that_is_supposed_to_fix_things.
        let minute = |task: &str| {
            let (h, m, _) = when(task);
            h * 60 + m
        };
        let evening = minute("baskfy.vbt.evening");
        assert!(minute("baskfy.vbt.detect") < evening);
        assert_eq!(minute("baskfy.vbt.check_detect_fresh"), 21 * 60 + 30);
        for task in [
            "baskfy.vbt.check_orders_past_expiry",
            "baskfy.vbt.check_naked_positions",
            "baskfy.vbt.check_positions_without_bars",
        ] {
            assert!(minute(task) > evening, "{task}");
            assert_eq!(minute(task), 21 * 60 + 40);
        }
        // The morning rebuild lands before the 09:15 open.
        assert_eq!(minute("baskfy.vbt.morning"), 9 * 60);
    }

    #[test]
    fn the_detector_and_the_plans_take_the_compute_queue_and_the_sweep_runs_every_minute() {
        for task in [
            "baskfy.vbt.detect",
            "baskfy.vbt.evening",
            "baskfy.vbt.morning",
        ] {
            assert_eq!(entry(task).options.queue, Queue::Compute, "{task}");
        }
        assert_eq!(
            crate::tasks::route_by_name("baskfy.vbt.rescan"),
            Queue::Compute
        );
        assert_eq!(
            crate::tasks::route_by_name("baskfy.vbt.rescan_sweep"),
            Queue::Default
        );
        let Schedule::Crontab(cron) = entry("baskfy.vbt.rescan_sweep").schedule else {
            panic!("the sweep is a crontab");
        };
        let expanded = cron.expand().unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(expanded.minute.len(), 60);
        assert_eq!(expanded.day_of_week.len(), 7);
    }

    #[test]
    fn every_task_is_ported_and_none_is_an_execution_path() {
        assert_eq!(PORTED.len(), 9);
        for name in PORTED {
            assert!(
                crate::tasks::find(name).is_some(),
                "{name} is not registered"
            );
            assert!(!name.contains("execute") && !name.contains("confirm"));
        }
    }
}
