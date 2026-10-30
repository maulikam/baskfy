//! Lane **curated** — the curated baskets' (SC/T8) end-of-day jobs.
//!
//! Python: `tasks/curated_metrics.py` (commits per chunk and per basket — its own session, not
//! one transaction), `curated_sip.py`, `curated_dividends.py`, `curated_batch_sync.py`,
//! `curated_rebalance_notify.py` (mail). None places an order.
//!
//! **Ported** (wave 6, lane curated; DECISIONS-RS R8.curated): all five tasks, over private
//! submodules — [`math`] (`baskfy_core.curated_metrics`, digit for digit over `WideDecimal`),
//! `metrics` (`baskfy_api.curated_metrics_service.compute_all_metrics`), `sip`
//! (`curated_sip` + `baskfy_core.curated_sip`), `dividends` (`curated_dividends` +
//! `baskfy_core.curated_dividends`), `batch_sync` and `notify`. Every task but the metrics job is
//! one transaction (`run_in_session`: commit on return, roll back on raise); the metrics job
//! commits per chunk of reads and per written basket, as Python's own session does.
//!
//! **No order path.** A SIP plan raises a `SIP_DUE` *reminder*; a batch sync *reads* the desk
//! journal's fills; the notify job sends mail. Nothing here reaches an order, a GTT, a Kite
//! endpoint or an auto-execute flag (`tests/law_two.rs` scans these files). Grade with
//! `tools/parity/task_diff.py --cases tools/parity/task_cases/curated.json`.

use chrono::{DateTime, Duration, NaiveDate, Utc};

use baskfy_db::OrderedJson;

use super::{BoundArgs, TaskContext, TaskResult};

mod batch_sync;
mod dividends;
pub mod math;
mod metrics;
mod notify;
mod sip;

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.cb.compute_metrics",
    "baskfy.cb.sip_reminders",
    "baskfy.cb.derive_dividends",
    "baskfy.cb.sync_batches",
    "baskfy.cb.rebalance_notify",
];

/// `dt.date.fromisoformat(x) if x else dt.datetime.now(tz=IST).date()` over the raw argument:
/// any falsy value (`None`, `""`, `0`, `false`, `[]`, `{}`) is today; a truthy non-`str` is
/// `fromisoformat`'s own `TypeError`.
fn day_or_today(
    ctx: &TaskContext,
    args: &BoundArgs,
    name: &str,
) -> Result<NaiveDate, crate::WorkerError> {
    match args.value(name) {
        Some(v) if notify::truthy(v) => match v {
            OrderedJson::Str(t) => super::args::fromisoformat_date(t),
            _ => Err(crate::WorkerError::python(
                "TypeError",
                "fromisoformat: argument must be str",
            )),
        },
        _ => Ok(ctx.today_ist()),
    }
}

/// `dt.datetime.now(tz=UTC)` at Python's resolution (microseconds).
fn now_micros(ctx: &TaskContext) -> DateTime<Utc> {
    let now = ctx.now_utc();
    now - Duration::nanoseconds(i64::from(now.timestamp_subsec_nanos() % 1_000))
}

/// Ports `baskfy_worker.tasks.celery_tasks.compute_curated_metrics_task` — `baskfy.cb.compute_metrics(trade_date=None)`.
///
/// Upserts `cb_metrics` for every non-archived basket on the day (today in IST without one).
///
/// # Errors
/// `ValueError` for a date that does not parse; the job's database or arithmetic error.
pub async fn compute_curated_metrics_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = day_or_today(ctx, &args, "trade_date")?;
    metrics::compute_all_metrics(ctx, day).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.curated_sip_reminders_task` — `baskfy.cb.sip_reminders(as_of=None)`.
///
/// Raises `SIP_DUE` pending actions for ACTIVE REMINDER plans due on the day. Never an order.
///
/// # Errors
/// `ValueError` for a date that does not parse or a calendar that cannot place a fire.
pub async fn curated_sip_reminders_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = day_or_today(ctx, &args, "as_of")?;
    sip::run_sip_reminders(ctx, day).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.curated_dividends_task` — `baskfy.cb.derive_dividends(as_of=None)`.
///
/// # Errors
/// `ValueError` for a date that does not parse; a database failure.
pub async fn curated_dividends_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = day_or_today(ctx, &args, "as_of")?;
    dividends::run_dividends(ctx, day).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.curated_batch_sync_task` — `baskfy.cb.sync_batches()`.
///
/// # Errors
/// A database failure.
pub async fn curated_batch_sync_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    batch_sync::run_batch_sync(ctx, now_micros(ctx)).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.curated_rebalance_notify_task` — `baskfy.cb.rebalance_notify()`.
///
/// # Errors
/// A database failure.
pub async fn curated_rebalance_notify_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    notify::run_rebalance_notify(ctx, now_micros(ctx)).await
}
