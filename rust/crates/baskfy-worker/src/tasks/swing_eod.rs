//! Lane **swing_eod** — the swing book's evening jobs: detection (the same detector as nightly step
//! 12, `steps::sleeves::detect_swing`, called with `pipeline_run_id = None` — the lane widened that
//! signature, it never forked the detector), the EOD plan, the weekend scan, the catalyst tags and
//! the 21:30 freshness check.
//!
//! Python: `tasks/swing.run_detect_swing`, `swing_eod.run_swing_eod`, `swing.WEEKEND_SCAN_SESSIONS`,
//! `swing_catalyst.run_swing_catalyst`, `swing_ops.check_detect_fresh`.
//!
//! | Celery name | Python | here |
//! |---|---|---|
//! | `baskfy.swing.detect` | `celery_tasks.swing_detect_task` | [`swing_detect_task`] |
//! | `baskfy.swing.weekend` | `celery_tasks.swing_weekend_task` | [`swing_weekend_task`] |
//! | `baskfy.swing.eod` | `celery_tasks.swing_eod_task` → `swing_eod.run_swing_eod` | [`swing_eod_task`] (`eod`, `watch`, `plan`, `mail`) |
//! | `baskfy.swing.catalyst` | `celery_tasks.swing_catalyst_task` → `swing_catalyst.run_swing_catalyst` | [`swing_catalyst_task`] (`catalyst`) |
//! | `baskfy.swing.check_detect_fresh` | `celery_tasks.swing_check_detect_fresh_task` → `swing_ops.check_detect_fresh` | [`swing_check_detect_fresh_task`] |
//!
//! Each Python `run_in_session` is one transaction (commit on return, roll back on raise).
//!
//! **Law 2.** Nothing here confirms, sends or arms anything. The evening writes an
//! `sw_plan(source=EOD_PREVIEW)` with a 30-minute expiry that a person confirms on the desk, line
//! by line; the gate reads `BASKFY_SWING_INDEX_SLUG` (`nifty-smallcap-250`, NIFTY 500 fallback —
//! SW29) through the wave-5 detector, unchanged. `swing_execution_enabled` is read exactly where
//! Python reads it (the session's mode and the first-live countdown) and is never defaulted true;
//! no auto-execute flag is read at all.

mod catalyst;
#[cfg(test)]
mod db_tests;
mod eod;
mod mail;
mod plan;
mod watch;

use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use super::{BoundArgs, Lane, TaskContext, TaskResult};
use crate::alerts::{Alert, AlertName, Severity, dispatch};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::StepOutcome;
use crate::steps::sleeves::for_swing_eod::{NSE_EXCHANGE_ID, run_detect_swing_for};

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.swing.detect",
    "baskfy.swing.eod",
    "baskfy.swing.weekend",
    "baskfy.swing.catalyst",
    "baskfy.swing.check_detect_fresh",
];

#[allow(dead_code)]
const LANE: Lane = Lane::SwingEod;

/// `swing.WEEKEND_SCAN_SESSIONS`: the Saturday scan re-detects this many sessions.
pub const WEEKEND_SCAN_SESSIONS: i64 = 5;

/// What every task answers when no sole tenant is configured.
const NO_USER: &str = "no BASKFY_SOLE_USER_ID configured";

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `dt.date.fromisoformat(arg) if arg else dt.datetime.now(tz=IST).date()`.
fn day_or_today(ctx: &TaskContext, arg: Option<&str>) -> Result<NaiveDate, WorkerError> {
    match arg {
        Some(text) if !text.is_empty() => super::args::fromisoformat_date(text),
        _ => Ok(ctx.today_ist()),
    }
}

/// `swing.recent_trading_days(session, as_of, count)`: the last `count` NSE sessions up to and
/// including `as_of`, oldest first.
pub(crate) async fn recent_trading_days(
    conn: &mut sqlx::PgConnection,
    as_of: NaiveDate,
    count: i64,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trading_day.date FROM trading_day WHERE trading_day.exchange_id = $1 \
         AND trading_day.is_trading_day IS true AND trading_day.date <= $2 \
         ORDER BY trading_day.date DESC LIMIT $3",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(as_of)
    .bind(count)
    .fetch_all(&mut *conn)
    .await?;
    let mut days: Vec<NaiveDate> = rows.into_iter().map(|r| r.0).collect();
    days.sort_unstable();
    Ok(days)
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_detect_task` — `baskfy.swing.detect(trade_date=None)`.
///
/// Detect the day's setups and write the day's market row; idempotent per `(user_id, date)`.
/// Defaults to today in IST (the injected clock). Not wrapped like the nightly's step 12: a
/// failure here is visible, as Python's is.
///
/// # Errors
/// `ValueError` for a date that does not parse; the detector's and the database's.
pub async fn swing_detect_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let arg = args.opt_str("trade_date")?;
    let day = day_or_today(ctx, arg.as_deref())?;
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.swing_user_id else {
        return Ok(object([("date", s(iso(day))), ("skipped", s(NO_USER))]));
    };
    let mut tx = ctx.db.begin().await?;
    let mut outcome = StepOutcome::default();
    let written = run_detect_swing_for(
        &mut tx,
        &mut outcome,
        day,
        user_id,
        &deps.swing_index_slug,
        None,
    )
    .await?;
    tx.commit().await?;
    Ok(object([
        ("date", s(iso(day))),
        ("candidates", int(written)),
        ("detail", outcome.detail_json()),
    ]))
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_eod_task` — `baskfy.swing.eod(trade_date=None)`.
///
/// SW5: manage the book, settle the ladder, count the session, plan tomorrow, email the summary.
/// Places nothing.
///
/// # Errors
/// `ValueError` for a date that does not parse; the database's (rolled back).
pub async fn swing_eod_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let arg = args.opt_str("trade_date")?;
    let day = day_or_today(ctx, arg.as_deref())?;
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.swing_user_id else {
        return Ok(object([("date", s(iso(day))), ("skipped", s(NO_USER))]));
    };
    let mut tx = ctx.db.begin().await?;
    let mut outcome = StepOutcome::default();
    let report = eod::run_swing_eod(
        &mut tx,
        &mut outcome,
        day,
        user_id,
        deps.swing_execution_enabled,
        ctx.now_utc(),
        &ctx.settings,
    )
    .await?;
    tx.commit().await?;
    let OrderedJson::Object(detail) = report.as_detail() else {
        return Err(WorkerError::python("TypeError", "the report is not a dict"));
    };
    let mut payload = vec![("date".to_owned(), s(iso(day)))];
    for (k, v) in detail {
        crate::pyjson::update(&mut payload, &k, v);
    }
    Ok(OrderedJson::Object(payload))
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_weekend_task` — `baskfy.swing.weekend(as_of=None)`.
///
/// SW4: re-detect the last [`WEEKEND_SCAN_SESSIONS`] sessions up to `as_of`, each date
/// overwriting its own rows (idempotent), in one transaction.
///
/// # Errors
/// `ValueError` for a date that does not parse; the detector's and the database's.
pub async fn swing_weekend_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let arg = args.opt_str("as_of")?;
    let day = day_or_today(ctx, arg.as_deref())?;
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.swing_user_id else {
        return Ok(object([("as_of", s(iso(day))), ("skipped", s(NO_USER))]));
    };
    let mut tx = ctx.db.begin().await?;
    let sessions = recent_trading_days(&mut tx, day, WEEKEND_SCAN_SESSIONS).await?;
    let mut results = Vec::with_capacity(sessions.len());
    for one in sessions {
        let mut outcome = StepOutcome::default();
        let written = run_detect_swing_for(
            &mut tx,
            &mut outcome,
            one,
            user_id,
            &deps.swing_index_slug,
            None,
        )
        .await?;
        results.push(object([
            ("date", s(iso(one))),
            ("candidates", int(written)),
        ]));
    }
    tx.commit().await?;
    Ok(object([
        ("as_of", s(iso(day))),
        ("sessions", OrderedJson::Array(results)),
    ]))
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_catalyst_task` — `baskfy.swing.catalyst(session_date=None)`.
///
/// SW11B: announcements and result dates for the watched names and the day's candidates, through
/// the NSE provider (archive-first, its limiter); fail soft on a provider error.
///
/// # Errors
/// `ValueError` for a date that does not parse; the provider's construction; the database's.
pub async fn swing_catalyst_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let arg = args.opt_str("session_date")?;
    let day = day_or_today(ctx, arg.as_deref())?;
    let deps = ctx.pipeline_dependencies().await?;
    let Some(user_id) = deps.swing_user_id else {
        return Ok(object([("date", s(iso(day))), ("skipped", s(NO_USER))]));
    };
    let provider = catalyst::build_provider().await?;
    let mut tx = ctx.db.begin().await?;
    let mut outcome = StepOutcome::default();
    let report =
        catalyst::run_swing_catalyst(&mut tx, &mut outcome, day, user_id, Some(&provider)).await?;
    tx.commit().await?;
    let mut payload = vec![("date".to_owned(), s(iso(day)))];
    if let OrderedJson::Object(detail) = report.as_detail() {
        for (k, v) in detail {
            crate::pyjson::update(&mut payload, &k, v);
        }
    }
    Ok(OrderedJson::Object(payload))
}

/// `swing_health.published_trade_date`: the newest published run's trade date.
async fn published_trade_date(
    conn: &mut sqlx::PgConnection,
) -> Result<Option<NaiveDate>, WorkerError> {
    let row: Option<(NaiveDate,)> = sqlx::query_as(
        "SELECT pipeline_run.trade_date FROM pipeline_run \
         WHERE pipeline_run.data_version IS NOT NULL \
         ORDER BY pipeline_run.data_version DESC LIMIT 1",
    )
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|r| r.0))
}

/// Ports `baskfy_worker.tasks.celery_tasks.swing_check_detect_fresh_task` — `baskfy.swing.check_detect_fresh()`.
///
/// 21:30 (`swing_ops.check_detect_fresh`): the detect step must have written the published
/// date's market row; otherwise `SWING_DETECT_STALE` (critical) through the alert sinks. Silent
/// when all is well.
///
/// # Errors
/// The database's.
pub async fn swing_check_detect_fresh_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let now = ctx.now_ist();
    let day = iso(now.date());
    let mut tx = ctx.db.begin().await?;
    let published = published_trade_date(&mut tx).await?;
    let ran = match published {
        None => true,
        Some(on) => {
            let (found,): (bool,) = sqlx::query_as(
                "SELECT EXISTS (SELECT * FROM sw_market_daily WHERE sw_market_daily.date = $1)",
            )
            .bind(on)
            .fetch_one(&mut *tx)
            .await?;
            found
        }
    };
    tx.commit().await?;
    let published_text = published.map(iso);
    if ran {
        return Ok(object([
            ("date", s(day)),
            ("checked", OrderedJson::Bool(true)),
            ("published", crate::pyjson::opt_s(published_text)),
            ("alert", OrderedJson::Null),
        ]));
    }
    let Some(published_text) = published_text else {
        // Unreachable, as in Python: `ran` is true whenever nothing is published.
        return Ok(object([
            ("date", s(day)),
            ("checked", OrderedJson::Bool(false)),
            ("reason", s("nothing published")),
        ]));
    };
    let alert = Alert {
        name: AlertName::SwingDetectStale,
        severity: Severity::Critical,
        summary: format!(
            "No swing detection for the published date {published_text} by 21:30 — \
             tomorrow's plan would be built on a stale gate."
        ),
        labels: vec![("trade_date".to_owned(), published_text.clone())],
        detail: object([(
            "checked_at",
            s(format!("{}+05:30", now.format("%Y-%m-%dT%H:%M:%S"))),
        )]),
        runbook: AlertName::SwingDetectStale.runbook().to_owned(),
    };
    let dispatched = dispatch(&alert, &ctx.settings).await;
    Ok(object([
        ("date", s(day)),
        ("checked", OrderedJson::Bool(true)),
        ("published", s(published_text)),
        ("alert", dispatched),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_task_of_the_lane_is_ported_and_registered_to_it() {
        for name in PORTED {
            let spec = super::super::find(name);
            assert!(spec.is_some_and(|s| s.lane == Lane::SwingEod), "{name}");
        }
        assert_eq!(WEEKEND_SCAN_SESSIONS, 5);
    }
}

/// Wave 6, lane `swing_live`: the evening's plan pieces the morning (`swing_premarket`) and the
/// intraday rebuild (`swing_intraday`) call — imported there, never reimplemented, exactly as
/// Python's modules import them from `swing_eod` and `swing_watch` (added by lane swing_live; the
/// items above only had their visibility widened, nothing changed).
pub(crate) mod for_swing_live {
    pub(crate) use super::eod::{
        MarketRow, held_instrument_ids, manage_open_positions, risk_pct_in_force, settle_ladder,
        sleeve_account, watch_items,
    };
    pub(crate) use super::plan::{assemble, store_plan};
    pub(crate) use super::watch::{auto_watch, refresh_focus};
}
