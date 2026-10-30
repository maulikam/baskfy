//! Lane **options** — the options sleeve's collector and planner: the contract master, the
//! once-a-minute chain collector, index minute bars, the scan, the three plan families, the
//! weekly report, the health checks and the index-bar backfill.
//!
//! Python: `baskfy_worker/options/*` (`master`, `collector`, `index_bars`, `partitions`, `reads`,
//! `checks`, `weekly`, and the free gates of `scan`, `plan`, `plan_o2`, `plan_o3`) and the task
//! bodies in `tasks/celery_tasks.py`. Chain quotes, the index minutes and the NFO instrument dump
//! come from the replay upstream under the harness. Everything is off by default (the collect,
//! scan and monitor flags all default false).
//!
//! **Ported** (in [`PORTED`]; graded by `tools/parity/task_diff.py --cases
//! tools/parity/task_cases/options.json`): `refresh_master`, `collect_chain`, `index_bars`,
//! `index_bars_eod`, `backfill_index_bars`, `checks`, `weekly`.
//!
//! **Gated, not ported** (DECISIONS-RS R8.op2): `scan`, `plan_o1`, `plan_o2`, `plan_o3` answer
//! every refusal Python answers before its first scan or plan computation — the flags, the
//! window, the tenant, the NSE calendar — and past them return `NotPorted`: the scan and the
//! plan builders drive ~4,000 lines of `baskfy_core.options` (`scan`, `bars`, `structures`,
//! `risk`, `session`, `plan*`, `directional`, `expiry_setups`, `sizing`, `costs`) with no Rust
//! port and no goldens. With the shipped defaults (every flag false) the gate is all Beat ever
//! reaches. `options/probe.py` (a CLI) and `options/backtest_run.py` (lane `sleeve_backtests`'
//! task) have no task in this lane.
//!
//! **Law 2.** Nothing here places, modifies or cancels an order or a GTT: the Kite reads are the
//! NFO dump, `quote()`, minute history; the plan tasks' margin *calculator* is never reached
//! (their bodies are not ported). No auto-execute setting exists for the options sleeves.

mod alerting;
mod checks;
mod collector;
mod common;
mod index_bars;
mod master;
mod model;
mod weekly;

use baskfy_db::OrderedJson;
use baskfy_providers::factory::{KiteFamily, KiteLane};
use chrono::{DateTime, Duration, FixedOffset};

use super::{BoundArgs, Lane, TaskContext, TaskResult, not_ported};
use crate::error::WorkerError;
use crate::pyjson::{object, s};
use common::{
    falsy_date, in_ist, is_session_day, iso, iso_dt, kite_session_usable, now_ist, options_now,
    required_date,
};
use model::hm;

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.options.refresh_master",
    "baskfy.options.collect_chain",
    "baskfy.options.index_bars",
    "baskfy.options.index_bars_eod",
    "baskfy.options.weekly",
    "baskfy.options.checks",
    "baskfy.options.backfill_index_bars",
];

const LANE: Lane = Lane::Options;

fn skipped_at(now: DateTime<FixedOffset>, why: impl Into<String>) -> OrderedJson {
    object([("at", s(iso_dt(now))), ("skipped", s(why.into()))])
}

fn skipped_on(key: &str, day: chrono::NaiveDate, why: &str) -> OrderedJson {
    object([(key, s(iso(day))), ("skipped", s(why))])
}

/// `_options_idle_reason(enabled=...)`.
const fn idle_reason(enabled: bool) -> &'static str {
    if enabled {
        "outside 09:15-15:30 IST"
    } else {
        "BASKFY_OPTIONS_COLLECT_ENABLED is false"
    }
}

/// `{**a, **b}` for two lists of pairs.
fn spread(head: Vec<(String, OrderedJson)>, tail: Vec<(String, OrderedJson)>) -> OrderedJson {
    object(head.into_iter().chain(tail))
}

/// `collector.collect_gate(now, enabled=..., kite_ok=..., trading_day=...)` once the two free
/// refusals have passed: the NSE calendar, then a usable Kite token.
async fn collect_gate(
    ctx: &TaskContext,
    conn: &mut sqlx::PgConnection,
    now: DateTime<FixedOffset>,
) -> Result<Option<&'static str>, WorkerError> {
    if !is_session_day(conn, in_ist(now).date_naive()).await? {
        return Ok(Some("not an NSE trading day"));
    }
    if !kite_session_usable(ctx) {
        return Ok(Some("no usable Kite session"));
    }
    Ok(None)
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_refresh_master_task` —
/// `baskfy.options.refresh_master(as_of=None)`: tonight's NFO master for every F&O underlying.
/// No Kite session → skipped; a dump without NIFTY → refused, nothing written.
///
/// # Errors
/// A bad date; the provider's (the dump is read before the session opens); the database's.
pub async fn options_refresh_master_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = falsy_date(&args, "as_of")?.unwrap_or_else(|| ctx.today_ist());
    if !kite_session_usable(ctx) {
        return Ok(skipped_on("date", day, "no usable Kite session"));
    }
    let provider = common::plain_kite(ctx, KiteLane::Bulk).await?;
    let records = provider
        .fno_option_contracts()
        .await
        .map_err(WorkerError::Provider)?;
    let mut tx = ctx.db.begin().await?;
    let reports = match master::refresh_master_all(&mut tx, &records, day).await? {
        Ok(reports) => reports,
        Err(master::EmptyMaster(why)) => {
            tx.commit().await?;
            return Ok(object([("date", s(iso(day))), ("refused", s(why))]));
        }
    };
    let Some(nifty) = reports.nifty() else {
        return Err(WorkerError::python("KeyError", "'NIFTY'"));
    };
    let alert = master::master_alert(nifty);
    let OrderedJson::Object(mut out) = spread(nifty.pairs(), reports.summary()) else {
        return Err(WorkerError::python("TypeError", "a report is a dict"));
    };
    if let Some(alert) = alert {
        let sent = alerting::dispatch(&alert, &ctx.settings, ctx.now_utc()).await;
        crate::pyjson::update(&mut out, "alert", sent);
    }
    tx.commit().await?;
    Ok(OrderedJson::Object(out))
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_collect_chain_task` —
/// `baskfy.options.collect_chain(at=None)`: one minute of the NIFTY chain.
///
/// # Errors
/// A bad `at`; the provider's and the database's.
pub async fn options_collect_chain_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let enabled = ctx.worker.options_collect_enabled;
    if !enabled || !collector::in_session(now) {
        // The two free refusals, before a database connection.
        return Ok(skipped_at(now, idle_reason(enabled)));
    }
    let mut tx = ctx.db.begin().await?;
    if let Some(why) = collect_gate(ctx, &mut tx, now).await? {
        tx.commit().await?;
        return Ok(skipped_at(now, why));
    }
    let quotes = common::family_kite(ctx, KiteFamily::Quote, KiteLane::Interactive).await?;
    let report = collector::collect_minute(&mut tx, &quotes, now).await?;
    tx.commit().await?;
    Ok(report.as_dict())
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_index_bars_task` —
/// `baskfy.options.index_bars(at=None)`: the closed index minutes since the last stored one.
///
/// # Errors
/// A bad `at`; the provider's and the database's.
pub async fn options_index_bars_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let enabled = ctx.worker.options_collect_enabled;
    if !enabled || !collector::in_session(now) {
        return Ok(skipped_at(now, idle_reason(enabled)));
    }
    let mut tx = ctx.db.begin().await?;
    if let Some(why) = collect_gate(ctx, &mut tx, now).await? {
        tx.commit().await?;
        return Ok(skipped_at(now, why));
    }
    let bars = common::family_kite(ctx, KiteFamily::Historical, KiteLane::Interactive).await?;
    let report = index_bars::pull_intraday(&mut tx, &bars, now).await?;
    tx.commit().await?;
    Ok(spread(
        vec![("at".to_owned(), s(iso_dt(now)))],
        report.pairs(),
    ))
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_index_bars_eod_task` —
/// `baskfy.options.index_bars_eod(trade_date=None)`: the whole session's index bars again.
///
/// # Errors
/// A bad date; the provider's and the database's.
pub async fn options_index_bars_eod_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = now_ist(ctx);
    let day = falsy_date(&args, "trade_date")?.unwrap_or_else(|| now.date_naive());
    if !ctx.worker.options_collect_enabled {
        return Ok(skipped_on(
            "date",
            day,
            "BASKFY_OPTIONS_COLLECT_ENABLED is false",
        ));
    }
    if !kite_session_usable(ctx) {
        return Ok(skipped_on("date", day, "no usable Kite session"));
    }
    let mut tx = ctx.db.begin().await?;
    if !is_session_day(&mut tx, day).await? {
        tx.commit().await?;
        return Ok(skipped_on("date", day, "not an NSE trading day"));
    }
    let bars = common::family_kite(ctx, KiteFamily::Historical, KiteLane::Interactive).await?;
    let report = index_bars::reconcile_day(&mut tx, &bars, day, now).await?;
    tx.commit().await?;
    Ok(spread(
        vec![("date".to_owned(), s(iso(day)))],
        report.pairs(),
    ))
}

/// `scan.scan_gate_free(now, scan_enabled=..., collect_enabled=..., user_id=...)`.
fn scan_gate_free(
    ctx: &TaskContext,
    now: DateTime<FixedOffset>,
    user: Option<i64>,
) -> Option<&'static str> {
    if !ctx.worker.options_scan_enabled {
        return Some("BASKFY_OPTIONS_SCAN_ENABLED is false");
    }
    if !ctx.worker.options_collect_enabled {
        return Some("BASKFY_OPTIONS_COLLECT_ENABLED is false (no chain to scan)");
    }
    if !collector::in_session(now) {
        return Some("outside 09:15-15:30 IST");
    }
    if user.is_none() {
        return Some("no BASKFY_SOLE_USER_ID configured");
    }
    None
}

/// The plan families' free gates (`plan.plan_gate_free`, `plan_o2.plan_o2_gate_free`,
/// `plan_o3.plan_o3_gate_free`): the monitor flag, the collect flag, the family's window
/// `[first, last]` (inclusive, to the microsecond), the tenant.
fn plan_gate_free(
    ctx: &TaskContext,
    now: DateTime<FixedOffset>,
    user: Option<i64>,
    window: (chrono::NaiveTime, chrono::NaiveTime),
    outside: &str,
) -> Option<String> {
    if !ctx.worker.options_monitor_enabled {
        return Some("BASKFY_OPTIONS_MONITOR_ENABLED is false".to_owned());
    }
    if !ctx.worker.options_collect_enabled {
        return Some("BASKFY_OPTIONS_COLLECT_ENABLED is false (no chain to plan from)".to_owned());
    }
    let clock = in_ist(now).time();
    if !(window.0 <= clock && clock <= window.1) {
        return Some(outside.to_owned());
    }
    if user.is_none() {
        return Some("no BASKFY_SOLE_USER_ID configured".to_owned());
    }
    None
}

/// The session-day refusal every gated task asks once its free gate has passed; `NotPorted`
/// past it (R8.op2).
async fn holiday_then_not_ported(
    ctx: &TaskContext,
    now: DateTime<FixedOffset>,
    task: &'static str,
) -> TaskResult {
    let mut tx = ctx.db.begin().await?;
    let session = is_session_day(&mut tx, in_ist(now).date_naive()).await?;
    tx.commit().await?;
    if !session {
        return Ok(skipped_at(now, "not an NSE trading day"));
    }
    not_ported(task, LANE)
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_scan_task` — `baskfy.options.scan(at=None)`:
/// the free gate and the NSE calendar; the scan itself is **not ported** (R8.op2).
///
/// # Errors
/// A bad `at`; `NotPorted` once every refusal has passed.
pub async fn options_scan_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let user = ctx.sole_user_id();
    if let Some(why) = scan_gate_free(ctx, now, user) {
        return Ok(skipped_at(now, why));
    }
    holiday_then_not_ported(ctx, now, "baskfy.options.scan").await
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_plan_o1_task` —
/// `baskfy.options.plan_o1(at=None)`: the free gate (10:00–10:16) and the NSE calendar; the O1
/// plan builder is **not ported** (R8.op2).
///
/// # Errors
/// A bad `at`; `NotPorted` once every refusal has passed.
pub async fn options_plan_o1_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let user = ctx.sole_user_id();
    // min/max of the two condors' plan time and entry window end (both 10:00 / 10:15), plus one
    // minute to lapse.
    let window = (hm(10, 0), hm(10, 15) + Duration::minutes(1));
    if let Some(why) = plan_gate_free(
        ctx,
        now,
        user,
        window,
        "outside 10:00-10:15 IST (+1 minute to lapse)",
    ) {
        return Ok(skipped_at(now, why));
    }
    holiday_then_not_ported(ctx, now, "baskfy.options.plan_o1").await
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_plan_o2_task` —
/// `baskfy.options.plan_o2(at=None)`: the free gate (09:30–13:34) and the NSE calendar; the O2
/// plan builder is **not ported** (R8.op2).
///
/// # Errors
/// A bad `at`; `NotPorted` once every refusal has passed.
pub async fn options_plan_o2_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let user = ctx.sole_user_id();
    let window = (hm(9, 30), hm(13, 30) + Duration::minutes(4));
    if let Some(why) = plan_gate_free(ctx, now, user, window, "outside 09:30-13:34 IST") {
        return Ok(skipped_at(now, why));
    }
    holiday_then_not_ported(ctx, now, "baskfy.options.plan_o2").await
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_plan_o3_task` —
/// `baskfy.options.plan_o3(at=None)`: the free gate (09:45–13:34) and the NSE calendar; the O3
/// plan builder is **not ported** (R8.op2).
///
/// # Errors
/// A bad `at`; `NotPorted` once every refusal has passed.
pub async fn options_plan_o3_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let user = ctx.sole_user_id();
    // min(O3-B plan time 09:45, O3-A window start 10:19); O3-A entry window end 13:30 + 4.
    let window = (hm(9, 45), hm(13, 30) + Duration::minutes(4));
    if let Some(why) = plan_gate_free(ctx, now, user, window, "outside 09:45-13:34 IST") {
        return Ok(skipped_at(now, why));
    }
    holiday_then_not_ported(ctx, now, "baskfy.options.plan_o3").await
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_weekly_task` —
/// `baskfy.options.weekly(at=None)`: the week's journal per pool as one `OPTIONS_WEEKLY`.
///
/// # Errors
/// A bad `at`; the database's.
pub async fn options_weekly_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let user = ctx.sole_user_id();
    if !ctx.worker.options_monitor_enabled {
        return Ok(skipped_at(now, "BASKFY_OPTIONS_MONITOR_ENABLED is false"));
    }
    let Some(user) = user else {
        return Ok(skipped_at(now, "no BASKFY_SOLE_USER_ID configured"));
    };
    let mut tx = ctx.db.begin().await?;
    let today = in_ist(now).date_naive();
    let rows = weekly::week_rows(&mut tx, user, today).await?;
    let alert = weekly::weekly_alert(&rows, today)?;
    let sent = alerting::dispatch(&alert, &ctx.settings, ctx.now_utc()).await;
    tx.commit().await?;
    Ok(object([
        ("at", s(iso_dt(now))),
        ("summary", s(alert.summary.clone())),
        ("sent", sent),
    ]))
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_checks_task` —
/// `baskfy.options.checks(at=None)`: the checks due this minute; each failure one
/// `OPTIONS_CHECK_FAILED`.
///
/// # Errors
/// A bad `at`; the database's.
pub async fn options_checks_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let names = checks::due_checks(now);
    if names.is_empty() {
        return Ok(skipped_at(now, "no options check is due at this minute"));
    }
    let switches = checks::Switches {
        scan_enabled: ctx.worker.options_scan_enabled,
        collect_enabled: ctx.worker.options_collect_enabled,
        monitor_enabled: ctx.worker.options_monitor_enabled,
    };
    let mut tx = ctx.db.begin().await?;
    if !is_session_day(&mut tx, in_ist(now).date_naive()).await? {
        tx.commit().await?;
        return Ok(skipped_at(now, "not an NSE trading day"));
    }
    let results = checks::run_checks(&mut tx, &names, now, switches).await?;
    let mut sent = Vec::new();
    for result in results.iter().filter(|r| !r.ok) {
        let alert = checks::check_alert(result, now);
        sent.push(alerting::dispatch(&alert, &ctx.settings, ctx.now_utc()).await);
    }
    tx.commit().await?;
    Ok(object([
        ("at", s(iso_dt(now))),
        (
            "checks",
            OrderedJson::Array(results.iter().map(checks::CheckResult::as_dict).collect()),
        ),
        ("sent", OrderedJson::Array(sent)),
    ]))
}

/// Ports `baskfy_worker.tasks.celery_tasks.options_backfill_index_bars_task` —
/// `baskfy.options.backfill_index_bars(date_from, date_to=None)`: the resumable Tier-1 index
/// minute backfill on the bulk lane, committed per window.
///
/// # Errors
/// A bad date; `ValueError` for an inverted range; the provider's and the database's.
pub async fn options_backfill_index_bars_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let start = required_date(&args, "date_from")?;
    let end = falsy_date(&args, "date_to")?.unwrap_or_else(|| ctx.today_ist());
    if !kite_session_usable(ctx) {
        return Ok(skipped_on("from", start, "no usable Kite session"));
    }
    let bars = common::family_kite(ctx, KiteFamily::Historical, KiteLane::Bulk).await?;
    let report = index_bars::backfill(&ctx.db, &bars, start, end, now_ist(ctx)).await?;
    Ok(spread(
        vec![
            ("from".to_owned(), s(iso(start))),
            ("to".to_owned(), s(iso(end))),
        ],
        report.pairs(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_ported_list_is_this_lanes_and_holds_no_order_task() {
        assert_eq!(PORTED.len(), 7);
        for name in PORTED {
            assert!(name.starts_with("baskfy.options."));
            let spec = crate::tasks::find(name);
            assert!(spec.is_some_and(|s| s.lane == Lane::Options), "{name}");
        }
        for gated in ["scan", "plan_o1", "plan_o2", "plan_o3"] {
            assert!(!PORTED.contains(&format!("baskfy.options.{gated}").as_str()));
        }
    }

    #[test]
    fn the_idle_reason_names_the_flag_first() {
        assert_eq!(
            idle_reason(false),
            "BASKFY_OPTIONS_COLLECT_ENABLED is false"
        );
        assert_eq!(idle_reason(true), "outside 09:15-15:30 IST");
    }
}
