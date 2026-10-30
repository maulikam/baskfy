//! Lane **twt_jobs** — the three-weeks-tight sleeve's jobs: detection retry (`detect_session`,
//! keyed on the breadth table), the evening and morning plans, the live scan and its publisher.
//!
//! Python: `tasks/twt.detect_session`, `twt_evening.run_twt_evening`, `twt_scan.run_twt_scan`,
//! `live_scan.run_twt_live`. Plans are `PROPOSED`; the worker never confirms (TW17).
//!
//! | Celery name | Python | Rust |
//! |---|---|---|
//! | `baskfy.twt.detect` | `celery_tasks.twt_detect_task` → `twt.detect_session(force=False)` | [`twt_detect_task`] → `detect::detect_session` over wave 5's detector |
//! | `baskfy.twt.evening` | `celery_tasks.twt_evening_task` → `twt_evening.run_twt_evening` | [`twt_evening_task`] → `evening::run_twt_evening` |
//! | `baskfy.twt.morning` | `celery_tasks.twt_morning_task` (same body, `MORNING`) | [`twt_morning_task`] |
//! | `baskfy.twt.scan` | `celery_tasks.twt_scan_task` → `twt_scan.run_twt_scan` (+ `live_scan.run_twt_live`) | [`twt_scan_task`] → `scan::run_twt_scan` |
//! | `baskfy.twt.scan_publish` | `celery_tasks.twt_scan_publish_task` | [`twt_scan_publish_task`] → `celery::apply_async` |
//!
//! Each Python `run_in_session` is one transaction here (commit on return, roll back on raise).
//!
//! **Law 2 and TW17.** Nothing here places, arms, modifies or cancels an order or a GTT, or calls
//! a Kite write endpoint: the only Kite call is the live scan's `GET /quote`. Every plan line is
//! `PROPOSED`. `BASKFY_TWT_EXECUTION_ENABLED` decides a session's mode and the half-size
//! multiplier, never whether anything is sent, and this worker reads **no** auto-execute flag —
//! `BASKFY_TWT_AUTO_EXECUTE` is the desk's alone.
//!
//! Grade with `tools/parity/task_diff.py --cases tools/parity/task_cases/twt_jobs.json`.

pub(in crate::tasks) mod alerting;
pub(in crate::tasks) mod celery;
#[cfg(test)]
mod clone_tests;
#[cfg(test)]
mod db_tests;
mod detect;
mod evening;
mod live;
pub(in crate::tasks) mod quotes;
mod scan;
mod sleeve;

use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use super::args::fromisoformat_date;
use super::{BoundArgs, TaskContext, TaskResult};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::StepOutcome;

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.twt.detect",
    "baskfy.twt.evening",
    "baskfy.twt.morning",
    "baskfy.twt.scan",
    "baskfy.twt.scan_publish",
];

/// `twt_scan.SCAN_TASK_NAME`: the task the publisher sends.
const SCAN_TASK_NAME: &str = "baskfy.twt.scan";

const NO_SOLE_USER: &str = "no BASKFY_SOLE_USER_ID configured";

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `dt.date.fromisoformat(trade_date) if trade_date else None`: an absent or empty value is
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

/// Ports `baskfy_worker.tasks.celery_tasks.twt_detect_task` — `baskfy.twt.detect(trade_date=None)`.
///
/// TW4's 21:00 retry: detect the session unless its breadth row exists (TW4.3). Idempotent per
/// `(user_id, date)`; `pipeline_run_id` is `NULL` from here.
///
/// # Errors
/// Python's: a bad date (`ValueError`), the database's.
pub async fn twt_detect_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = trade_date_arg(&args)?.unwrap_or_else(|| ctx.today_ist());
    let deps = ctx.pipeline_dependencies().await?;
    if !deps.twt_nightly_enabled {
        return Ok(object([
            ("date", s(iso(day))),
            ("skipped", s("BASKFY_TWT_NIGHTLY_ENABLED is false")),
        ]));
    }
    let Some(user_id) = deps.twt_user_id else {
        return Ok(object([
            ("date", s(iso(day))),
            ("skipped", s(NO_SOLE_USER)),
        ]));
    };
    let mut tx = ctx.db.begin().await?;
    let out = detect::detect_session(&mut tx, day, user_id, false).await?;
    tx.commit().await?;
    Ok(out)
}

/// The evening/morning body inside one transaction.
async fn plan_in_session(
    ctx: &TaskContext,
    day: NaiveDate,
    user_id: i64,
    execution_enabled: bool,
    source: &str,
) -> TaskResult {
    let mut tx = ctx.db.begin().await?;
    let mut outcome = StepOutcome::default();
    let inputs = evening::EveningInputs {
        trade_date: day,
        user_id,
        execution_enabled,
        source,
        now: ctx.now_utc(),
        settings: &ctx.settings,
    };
    let report = evening::run_twt_evening(&mut tx, &mut outcome, &inputs).await?;
    tx.commit().await?;
    Ok(match report {
        Some(report) => report.as_detail(),
        None => object([("date", s(iso(day))), ("skipped", outcome.detail_json())]),
    })
}

/// Ports `baskfy_worker.tasks.celery_tasks.twt_evening_task` — `baskfy.twt.evening(trade_date=None)`.
///
/// TW6: exits, entries, the session row and the digest — every line `PROPOSED`.
///
/// # Errors
/// Python's: a bad date (`ValueError`), the database's.
pub async fn twt_evening_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = trade_date_arg(&args)?.unwrap_or_else(|| ctx.today_ist());
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.twt_user_id else {
        return Ok(object([
            ("date", s(iso(day))),
            ("skipped", s(NO_SOLE_USER)),
        ]));
    };
    plan_in_session(
        ctx,
        day,
        user_id,
        deps.twt_execution_enabled,
        evening::SOURCE_EVENING,
    )
    .await
}

/// Ports `baskfy_worker.tasks.celery_tasks.twt_morning_task` — `baskfy.twt.morning(trade_date=None)`.
///
/// The same plan rebuilt before the open — re-sized, not re-detected (`04` §11.3); the signal
/// session defaults to the last tradeable session the detector wrote.
///
/// # Errors
/// Python's: a bad date (`ValueError`), the database's.
pub async fn twt_morning_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let today = ctx.today_ist();
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.twt_user_id else {
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
    let mut outcome = StepOutcome::default();
    let inputs = evening::EveningInputs {
        trade_date: day,
        user_id,
        execution_enabled: deps.twt_execution_enabled,
        source: evening::SOURCE_MORNING,
        now: ctx.now_utc(),
        settings: &ctx.settings,
    };
    let report = evening::run_twt_evening(&mut tx, &mut outcome, &inputs).await?;
    tx.commit().await?;
    Ok(match report {
        Some(report) => report.as_detail(),
        None => object([("date", s(iso(day))), ("skipped", outcome.detail_json())]),
    })
}

/// Ports `baskfy_worker.tasks.celery_tasks.twt_scan_task` — `baskfy.twt.scan(run_id)`.
///
/// One press of **Scan now**: the latest published session (`force=True`), or during a session
/// with a Kite session today's provisional bar and a `LIVE` plan. Never raises for the work: a
/// failure is `FAILED` with its reason on the row.
///
/// # Errors
/// An argument `int()` refuses; the database's.
pub async fn twt_scan_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let deps = ctx.pipeline_dependencies().await?;
    let run_id = run_id_arg(&args)?;
    let mut tx = ctx.db.begin().await?;
    let out = scan::run_twt_scan(ctx, &mut tx, run_id, deps.twt_execution_enabled).await?;
    tx.commit().await?;
    Ok(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.twt_scan_publish_task` — `baskfy.twt.scan_publish()`.
///
/// Publishes every `QUEUED` `tw_scan_run` row nobody has published (the desk's button has no
/// Celery client), oldest first, at most [`scan::PUBLISH_LIMIT`] a pass.
///
/// # Errors
/// The database's; the broker's (`OperationalError`) when a row is waiting and it is down.
pub async fn twt_scan_publish_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let mut tx = ctx.db.begin().await?;
    let rows = scan::unpublished_runs(&mut tx, scan::PUBLISH_LIMIT).await?;
    let mut published = Vec::with_capacity(rows.len());
    if !rows.is_empty() {
        let mut broker = celery::broker(ctx.worker.broker_url()).await?;
        for id in rows {
            let task_id = celery::apply_async(
                &mut tx,
                &mut broker,
                SCAN_TASK_NAME,
                id,
                celery::QUEUE_COMPUTE,
            )
            .await?;
            sqlx::query("UPDATE tw_scan_run SET task_id = $2 WHERE id = $1")
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

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]
    use super::*;

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
        assert_eq!(
            run_id_arg(&args(vec![("run_id", OrderedJson::Float(3.9))])).ok(),
            Some(3)
        );
        assert!(run_id_arg(&args(vec![("run_id", s("x"))])).is_err());
    }

    fn entry(name: &str) -> &'static crate::tasks::beat::BeatEntry {
        crate::tasks::beat::BEAT_SCHEDULE
            .iter()
            .find(|e| e.name == name)
            .unwrap_or_else(|| panic!("{name} is not in the desk's clock"))
    }

    fn first_minute(name: &str) -> (u32, u32, Vec<u32>) {
        let crate::tasks::beat::Schedule::Crontab(cron) = entry(name).schedule else {
            panic!("{name} is not a crontab");
        };
        let expanded = cron.expand().unwrap_or_else(|e| panic!("{e}"));
        (
            expanded.hour.iter().copied().min().unwrap_or(99),
            expanded.minute.iter().copied().min().unwrap_or(99),
            expanded.day_of_week.iter().copied().collect(),
        )
    }

    #[test]
    fn the_evening_runs_after_the_detector_on_the_compute_queue() {
        // test_twt_beat.py::test_twt_evening_runs_after_the_detector.
        let e = entry("twt-evening");
        assert_eq!(e.task, "baskfy.twt.evening");
        assert_eq!(e.options.queue, crate::tasks::Queue::Compute);
        let (h, m, days) = first_minute("twt-evening");
        assert_eq!((h, m, days), (21, 20, vec![1, 2, 3, 4, 5]));
        let (dh, dm, _) = first_minute("twt-detect");
        assert!(dh * 60 + dm < 21 * 60 + 20);
    }

    #[test]
    fn the_morning_rebuilds_the_plan_before_the_open() {
        // test_twt_beat.py::test_twt_morning_rebuilds_the_plan_before_the_open.
        assert_eq!(entry("twt-morning").task, "baskfy.twt.morning");
        let (h, m, days) = first_minute("twt-morning");
        assert_eq!((h, m, days), (9, 5, vec![1, 2, 3, 4, 5]));
        assert!(h * 60 + m < 9 * 60 + 15);
    }

    #[test]
    fn every_twt_entry_names_a_registered_task_and_none_is_a_sweep() {
        // test_twt_beat.py: the four scheduled tasks, all registered, and no sweep on a timer
        // (non-negotiable 1; TW12.4).
        let twt: Vec<&crate::tasks::beat::BeatEntry> = crate::tasks::beat::BEAT_SCHEDULE
            .iter()
            .filter(|e| e.name.starts_with("twt-") || e.task.starts_with("baskfy.twt."))
            .collect();
        let tasks: std::collections::BTreeSet<&str> = twt.iter().map(|e| e.task).collect();
        assert_eq!(
            tasks.into_iter().collect::<Vec<_>>(),
            [
                "baskfy.twt.detect",
                "baskfy.twt.evening",
                "baskfy.twt.morning",
                "baskfy.twt.scan_publish"
            ]
        );
        for e in &twt {
            assert!(
                crate::tasks::find(e.task).is_some(),
                "{} is not registered",
                e.task
            );
            assert!(!e.name.contains("sweep") && !e.task.contains("sweep"));
        }
    }

    const LANE_SOURCES: [(&str, &str); 8] = [
        ("twt_jobs.rs", include_str!("twt_jobs.rs")),
        ("alerting.rs", include_str!("twt_jobs/alerting.rs")),
        ("celery.rs", include_str!("twt_jobs/celery.rs")),
        ("detect.rs", include_str!("twt_jobs/detect.rs")),
        ("evening.rs", include_str!("twt_jobs/evening.rs")),
        ("live.rs", include_str!("twt_jobs/live.rs")),
        ("scan.rs", include_str!("twt_jobs/scan.rs")),
        ("sleeve.rs", include_str!("twt_jobs/sleeve.rs")),
    ];

    fn code(source: &str) -> String {
        let body = source.split("#[cfg(test)]\nmod tests").next().unwrap_or("");
        body.lines()
            .map(|line| line.split("//").next().unwrap_or(""))
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn nothing_in_the_lane_names_a_broker_verb_or_writes_an_order() {
        let verbs = [
            "place_order",
            "place_gtt",
            "modify_gtt",
            "cancel_order",
            "OrderGateway",
            "baskfy_execution",
            "execute_line",
            "rearm_gtt",
            "KiteConnect",
        ];
        let writes = [
            "INTO tw_order",
            "UPDATE tw_order",
            "INTO tw_position",
            "UPDATE tw_position",
            "INTO tw_fill",
            "UPDATE tw_config",
            "AUTO_EXECUTE",
        ];
        for (file, source) in LANE_SOURCES {
            let code = code(source);
            for forbidden in verbs.iter().chain(writes.iter()) {
                assert!(!code.contains(forbidden), "{file} names {forbidden}");
            }
        }
        let scan = code(include_str!("twt_jobs/scan.rs"));
        assert!(scan.contains("detect_session("));
        assert!(!scan.contains("sleeve_capital_inr"));
        assert!(!scan.contains("fn detect_session"));
    }

    #[test]
    fn every_task_is_ported_and_none_is_an_execution_path() {
        assert_eq!(PORTED.len(), 5);
        // TW12.4: no TWT task name carries "sweep" (a scheduled TWT sweep would be a second
        // auto-execute exception) — test_twt_beat.py::test_the_sweep_is_not_on_a_timer.
        assert!(PORTED.iter().all(|name| !name.contains("sweep")));
        assert!(PORTED.iter().all(|name| !name.contains("execute")));
    }
}
