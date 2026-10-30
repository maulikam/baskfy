//! Lane **swing_live** — the swing book's session jobs: the premarket levels and gaps (Kite
//! quotes), "Scan now" and the post-login scan (bound tasks: `self.retry` on a row not yet
//! committed), the sweep that publishes queued scans, the 09:04 timing probe and the desk checks.
//!
//! | Celery name | Python | here |
//! |---|---|---|
//! | `baskfy.swing.premarket` | `celery_tasks.swing_premarket_task` → `swing_premarket.run_swing_premarket` | [`swing_premarket_task`] (`premarket`) |
//! | `baskfy.swing.scan_now` | `celery_tasks.swing_scan_now_task` → `swing_scan_now.run_scan_now`, `swing_intraday.build_intraday_plan` | [`swing_scan_now_task`] (`scan`, `intraday`) |
//! | `baskfy.swing.scan_after_login` | `celery_tasks.swing_scan_after_login_task` → `request_login_scan` (`baskfy_api.swing_scan.request_scan`) | [`swing_scan_after_login_task`] |
//! | `baskfy.swing.scan_sweep` | `celery_tasks.swing_scan_sweep_task` → `swing_scan_now.sweep_queued` | [`swing_scan_sweep_task`] (`producer`) |
//! | `baskfy.swing.timing_probe` | `celery_tasks.swing_timing_probe_task` → `swing_timing_probe.probe_once` | [`swing_timing_probe_task`] (`probe`) |
//! | `baskfy.swing.check_monitor_started` / `check_orders_after_cutoff` / `check_gtt_at_1515` | `swing_ops.*` over `baskfy_api.swing_health` | the three check tasks (`checks`) |
//!
//! `published_session` (`tasks/published_session.py`, the one "last published session" rule) is
//! public here so the VBT and TWT lanes read the same rule; `producer` likewise.
//!
//! **The two clocks** (CLAUDE.md): a scan of a session in progress writes rows stamped
//! `provisional`; the published series never claims today. Every "now" is the task's clock.
//!
//! **Law 2.** Nothing here confirms or sends a plan, places, modifies or cancels an order or a
//! GTT, or calls a Kite write endpoint. The checks *read* the desk's books; the scans read
//! quotes; the plans this lane writes are rows a person confirms on the desk. No auto-execute
//! flag is read at all, and `swing_execution_enabled` (the first-live sizing) is read only where
//! Python reads it, never defaulted true.

mod checks;
mod intraday;
mod premarket;
mod probe;
pub mod producer;
pub mod published_session;
mod quotes;
mod scan;

use std::path::PathBuf;

use baskfy_db::OrderedJson;
use baskfy_providers::factory::KiteLane;

use super::{BoundArgs, Lane, Queue, TaskContext, TaskResult};
use crate::error::WorkerError;
use crate::pyjson::{self, int, object, s};

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.swing.premarket",
    "baskfy.swing.scan_now",
    "baskfy.swing.scan_after_login",
    "baskfy.swing.scan_sweep",
    "baskfy.swing.timing_probe",
    "baskfy.swing.check_monitor_started",
    "baskfy.swing.check_orders_after_cutoff",
    "baskfy.swing.check_gtt_at_1515",
];

#[allow(dead_code)]
const LANE: Lane = Lane::SwingLive;

/// What every task answers when no sole tenant is configured.
const NO_USER: &str = "no BASKFY_SOLE_USER_ID configured";

/// `SWING_SCAN_NOW_TASK`.
const SWING_SCAN_NOW_TASK: &str = "baskfy.swing.scan_now";

/// Ports `baskfy_worker.tasks.celery_tasks.swing_premarket_task` — `baskfy.swing.premarket(session_date=None, stage="GAPS")`.
///
/// SW6: refresh the levels (08:50), scan the open for gaps and plan the morning (09:16). The
/// Kite provider is built only when `BASKFY_SWING_EP_PREMARKET_ENABLED` is true and the stage is
/// `GAPS` — with the flag off no Kite call is made at all.
///
/// # Errors
/// `ValueError` for a date that does not parse; the database's (rolled back).
pub async fn swing_premarket_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let arg = args.opt_str("session_date")?;
    let stage = args.str("stage")?;
    let day = match arg.as_deref() {
        Some(text) if !text.is_empty() => super::args::fromisoformat_date(text)?,
        _ => ctx.today_ist(),
    };
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(object([
            ("date", s(day.format("%Y-%m-%d").to_string())),
            ("skipped", s(NO_USER)),
        ]));
    };
    let enabled = ctx.worker.swing_ep_premarket_enabled;
    let quotes = if enabled && stage == premarket::STAGE_GAPS {
        // INTERACTIVE: 09:16 is one minute after the open and the gap read is worthless late.
        Some(quotes::kite_provider(ctx, KiteLane::Interactive).await?)
    } else {
        None
    };
    let mut tx = ctx.db.begin().await?;
    let report = premarket::run_swing_premarket(
        ctx,
        &mut tx,
        day,
        user_id,
        &stage,
        enabled,
        quotes.as_ref(),
        ctx.worker.swing_execution_enabled,
    )
    .await?;
    tx.commit().await?;
    let mut out = vec![("date".to_owned(), s(day.format("%Y-%m-%d").to_string()))];
    for (k, v) in report {
        pyjson::update(&mut out, &k, v);
    }
    Ok(OrderedJson::Object(out))
}

/// `_execute_swing_scan(run_id)`: one already-committed scan row through `run_scan_now`, in its
/// own transaction (committed on return, rolled back on a raise).
async fn execute_swing_scan(
    ctx: &TaskContext,
    run_id: i64,
    user_id: i64,
) -> Result<Vec<(String, OrderedJson)>, WorkerError> {
    let mut tx = ctx.db.begin().await?;
    let report =
        scan::run_scan_now(ctx, &mut tx, run_id, user_id, &ctx.worker.swing_index_slug).await?;
    tx.commit().await?;
    Ok(report.as_detail())
}

/// `{**scan, "intraday_plan": plan}` with the plan rebuilt fail-soft.
async fn with_plan(
    ctx: &TaskContext,
    user_id: i64,
    scan: Vec<(String, OrderedJson)>,
    label: &str,
) -> OrderedJson {
    let plan = match intraday::rebuild_intraday_plan(ctx, user_id, &scan).await {
        Ok(plan) => plan,
        Err(e) => {
            crate::steps::log_error(&format!("{label}: {}", scan::describe(&e)));
            object([("error", s(scan::describe(&e)))])
        }
    };
    let mut out = scan;
    pyjson::update(&mut out, "intraday_plan", plan);
    OrderedJson::Object(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_scan_now_task` — `baskfy.swing.scan_now(run_id)`.
///
/// SW15: one press of "Scan now". The Kite provider is built only on the provisional path. A
/// Kite session that cannot be built is a `FAILED` row with the reason, not a raise. The one
/// raise is `ScanNotRunnable` for a row not yet committed — Python's `self.retry` (countdown
/// 2 s, at most 5); run directly, as here, the retry raises the exception itself.
///
/// # Errors
/// `ScanNotRunnable` for a run row that is not there; the database's.
pub async fn swing_scan_now_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let run_id = args.int("run_id")?;
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(object([("run_id", int(run_id)), ("skipped", s(NO_USER))]));
    };
    let scan = execute_swing_scan(ctx, run_id, user_id).await?;
    Ok(with_plan(
        ctx,
        user_id,
        scan,
        &format!("scan {run_id} landed but its intraday plan did not"),
    )
    .await)
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_scan_after_login_task` — `baskfy.swing.scan_after_login(user_id)`.
///
/// Start today's provisional swing scan right after a verified broker login; outside an open
/// session this records nothing (`request_login_scan` explains why). Run directly the task has
/// no request id, so the row's `task_id` is `broker-login-<requested_at>` as Python's fallback.
///
/// # Errors
/// The database's.
pub async fn swing_scan_after_login_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let login_user = args.int("user_id")?;
    let Some(configured) = ctx.sole_user_id() else {
        return Ok(object([("skipped", s(NO_USER))]));
    };
    if login_user != configured {
        return Ok(object([(
            "skipped",
            s("login user does not match BASKFY_SOLE_USER_ID"),
        )]));
    }
    let requested_at = ctx.now_utc();
    let market_now = ctx.now_ist();
    let task_id = format!("broker-login-{}", crate::alerts::py_isoformat(requested_at));
    let mut tx = ctx.db.begin().await?;
    let request =
        intraday::request_login_scan(ctx, &mut tx, login_user, market_now, requested_at, &task_id)
            .await?;
    tx.commit().await?;
    // `run_id = request_result.get("run_id"); if not isinstance(run_id, int): return ...`
    let run_id = match request.get("run_id") {
        Some(OrderedJson::Int(id)) => *id,
        _ => return Ok(request),
    };
    let scan = execute_swing_scan(ctx, run_id, login_user).await?;
    Ok(with_plan(
        ctx,
        login_user,
        scan,
        "the live scan landed but its intraday plan did not",
    )
    .await)
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_scan_sweep_task` — `baskfy.swing.scan_sweep()`.
///
/// SW15: publish every queued `sw_scan_run` row nobody has published (the desk's rows, and the
/// API's when it had no broker) as `baskfy.swing.scan_now` on the `compute` queue, stamping each
/// row's `task_id`. Beat, every minute; one indexed SELECT when idle.
///
/// # Errors
/// The broker's (`OperationalError`) and the database's; the transaction is rolled back.
pub async fn swing_scan_sweep_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(object([
            ("published", OrderedJson::Array(Vec::new())),
            ("skipped", s(NO_USER)),
        ]));
    };
    let mut tx = ctx.db.begin().await?;
    let queued = scan::queued_unpublished(&mut tx, user_id).await?;
    let mut published = Vec::with_capacity(queued.len());
    for run_id in queued {
        let task_id = producer::apply_async(
            ctx.worker.broker_url(),
            SWING_SCAN_NOW_TASK,
            &[OrderedJson::Int(run_id)],
            Queue::Compute.name(),
        )
        .await?;
        scan::stamp_task_id(&mut tx, run_id, &task_id).await?;
        published.push(OrderedJson::Int(run_id));
    }
    tx.commit().await?;
    Ok(object([("published", OrderedJson::Array(published))]))
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_timing_probe_task` — `baskfy.swing.timing_probe()`.
///
/// SW11 / A4: one morning of Kite timing samples, then never again. With
/// `BASKFY_SWING_TIMING_PROBE` false — the default — this returns at once and touches nothing.
///
/// # Errors
/// The database's; a report that cannot be written.
pub async fn swing_timing_probe_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    if !ctx.worker.swing_timing_probe {
        return Ok(object([(
            "skipped",
            s("BASKFY_SWING_TIMING_PROBE is false"),
        )]));
    }
    let out_dir = PathBuf::from(&ctx.worker.swing_timing_probe_dir);
    let marker = out_dir.join(probe::DONE_MARKER);
    if marker.exists() {
        return Ok(object([(
            "skipped",
            s(format!(
                "{} exists — the probe already had its good run",
                marker.display()
            )),
        )]));
    }
    let mut tx = ctx.db.begin().await?;
    let names = probe::probe_names(&mut tx).await?;
    tx.commit().await?;
    let Some((_, token)) = names.first().cloned() else {
        return Ok(object([(
            "skipped",
            s("no instrument with a Kite token to probe"),
        )]));
    };
    // Python's default `build_kite_provider(...)`: the interactive lane.
    let provider = quotes::kite_provider(ctx, KiteLane::Interactive).await?;
    let symbols: Vec<String> = names.into_iter().map(|(symbol, _)| symbol).collect();
    probe::probe_once(&provider, ctx, &out_dir, &symbols, token).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_check_monitor_started_task` — `baskfy.swing.check_monitor_started()`.
///
/// 09:20: `SWING_MONITOR_DID_NOT_START` when the flag is on and no `monitor_ran` today.
///
/// # Errors
/// The database's.
pub async fn swing_check_monitor_started_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let mut tx = ctx.db.begin().await?;
    let out = checks::check_monitor_started(
        &mut tx,
        ctx.now_ist(),
        ctx.worker.swing_monitor_enabled,
        &ctx.settings,
    )
    .await?;
    tx.commit().await?;
    Ok(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_check_orders_after_cutoff_task` — `baskfy.swing.check_orders_after_cutoff()`.
///
/// 10:50: `SWING_ORDER_OPEN_AFTER_CUTOFF` for a BUY line still `SENT`.
///
/// # Errors
/// The database's.
pub async fn swing_check_orders_after_cutoff_task(
    ctx: &TaskContext,
    args: BoundArgs,
) -> TaskResult {
    let _ = args;
    let mut tx = ctx.db.begin().await?;
    let out = checks::check_orders_after_cutoff(&mut tx, ctx.now_ist(), &ctx.settings).await?;
    tx.commit().await?;
    Ok(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_check_gtt_at_1515_task` — `baskfy.swing.check_gtt_at_1515()`.
///
/// 15:20: `SWING_GTT_MISSING_AT_1515` for shares open with no GTT after the desk's sweep. Reads
/// `sw_position`; arms nothing.
///
/// # Errors
/// The database's.
pub async fn swing_check_gtt_at_1515_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let mut tx = ctx.db.begin().await?;
    let out = checks::check_gtt_at_1515(&mut tx, ctx.now_ist(), &ctx.settings).await?;
    tx.commit().await?;
    Ok(out)
}
