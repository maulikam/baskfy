//! `baskfy.ops.resync` — ports `baskfy_worker.tasks.resync.run_resync` (leaf 3.1's resync
//! button: inspect, repair what can be repaired, re-inspect, and report all three honestly).
//!
//! The inspection is the API's own (`baskfy_api::routes::admin::resync::inspect_pending`, the
//! function `GET /admin/resync` serves), run three times as Python runs it: before the repairs to
//! find the work, and after them to measure what is still wrong. Each `session_scope` is one
//! transaction here.
//!
//! | class | remedy |
//! |---|---|
//! | `kite_session` | `kite_session_cli.refresh_quietly()` (the `ops_alerts` lane's; a no-op under the harness) |
//! | `wrong_holiday` | `trading_day` upserted as a session (`source = 'bhavcopy'`), then as `thin_bars` |
//! | `thin_bars` | the day's bhavcopy re-ingested (`steps::ingest::backfill_summary`, the chain's own code), then a nightly re-run |
//! | `missing_run` | `baskfy.pipeline.nightly` published for the date (the worker's Celery producer) |
//!
//! **It cannot place an order.** It reaches ingestion, the trading calendar and the Kite
//! *session* bridge only — never the execution gateway, a broker order or a GTT (law 2).

use std::collections::BTreeSet;
use std::sync::Arc;

use baskfy_api::routes::admin::resync::{
    InspectedPlan, RESYNC_COMPLETED_ACTION, ResyncFindingOut, inspect_pending,
};
use baskfy_db::OrderedJson;
use baskfy_providers::ports::ReferenceProvider;
use baskfy_providers::publication::{PublicationCheck, bhavcopy_publication_check};
use chrono::{FixedOffset, NaiveDate};

use super::super::swing_live::producer;
use super::super::{TaskContext, ops_alerts, route_by_name};
use crate::error::WorkerError;
use crate::pyjson::{int, object, strings};
use crate::steps::{ingest, log_warning};
use crate::window::DateWindow;

/// `MAX_BAR_REPAIRS`: days of bars one press repairs.
pub const MAX_BAR_REPAIRS: usize = 30;
/// `MAX_RERUNS`: nightly chains one press queues.
pub const MAX_RERUNS: usize = 10;
/// `CORRECTED_SOURCE`.
pub const CORRECTED_SOURCE: &str = "bhavcopy";
/// `NIGHTLY`.
pub const NIGHTLY: &str = "baskfy.pipeline.nightly";
/// `seed_data.NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i16 = 1;

/// `ResyncOutcome`.
#[derive(Debug, Default)]
pub struct ResyncOutcome {
    /// `repaired`.
    pub repaired: Vec<String>,
    /// `queued`.
    pub queued: Vec<String>,
    /// `failed`.
    pub failed: Vec<String>,
    /// `deferred`.
    pub deferred: Vec<String>,
    /// `still_pending`.
    pub still_pending: Vec<String>,
    /// `unresolved`.
    pub unresolved: Vec<String>,
    /// `pending_at_start`.
    pub pending_at_start: i64,
}

impl ResyncOutcome {
    /// `complete`: nothing this press could act on is left over.
    #[must_use]
    pub fn complete(&self) -> bool {
        self.failed.is_empty() && self.deferred.is_empty() && self.still_pending.is_empty()
    }

    /// `as_json()`, in Python's key order.
    #[must_use]
    pub fn as_json(&self) -> OrderedJson {
        object([
            ("repaired", strings(self.repaired.iter().cloned())),
            ("queued", strings(self.queued.iter().cloned())),
            ("failed", strings(self.failed.iter().cloned())),
            ("deferred", strings(self.deferred.iter().cloned())),
            ("still_pending", strings(self.still_pending.iter().cloned())),
            ("unresolved", strings(self.unresolved.iter().cloned())),
            ("pending_at_start", int(self.pending_at_start)),
            ("complete", OrderedJson::Bool(self.complete())),
        ])
    }
}

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `f"{n:,}"`.
fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

fn problem(p: &baskfy_api::problems::Problem) -> WorkerError {
    WorkerError::python("RuntimeError", p.detail.clone())
}

/// `plan.dates(kind)`: the dated findings of one class, in the plan's order.
fn dates(plan: &InspectedPlan, kind: &str) -> Vec<NaiveDate> {
    plan.findings
        .iter()
        .filter(|f| f.kind == kind)
        .filter_map(|f| f.trade_date)
        .collect()
}

/// One inspection in its own transaction (`async with session_scope(...)`).
async fn inspect(
    ctx: &TaskContext,
    days: i64,
    publication: Option<PublicationCheck>,
) -> Result<InspectedPlan, WorkerError> {
    let ist =
        FixedOffset::east_opt(19_800).ok_or_else(|| WorkerError::python("ValueError", "IST"))?;
    let now = ctx.now_utc().with_timezone(&ist);
    let mut tx = ctx.db.begin().await?;
    let plan = inspect_pending(
        &mut tx,
        &ctx.settings.publish_deadline_ist,
        days,
        now,
        || async move { Ok(publication) },
    )
    .await
    .map_err(|p| problem(&p))?;
    tx.commit().await?;
    Ok(plan)
}

/// `_record_completion`: the repair's own report on the staff audit trail.
async fn record_completion(
    ctx: &TaskContext,
    actor_user_id: i64,
    target: &str,
    outcome: &ResyncOutcome,
) -> Result<(), WorkerError> {
    let detail = outcome
        .as_json()
        .to_python_json()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))?;
    let mut tx = ctx.db.begin().await?;
    sqlx::query(
        "INSERT INTO admin_action (actor_user_id, action, target, detail) VALUES \
         ($1::BIGINT, $2::VARCHAR, $3::VARCHAR, CAST($4 AS JSONB)) RETURNING admin_action.id, \
         admin_action.created_at",
    )
    .bind(actor_user_id)
    .bind(RESYNC_COMPLETED_ACTION)
    .bind(target)
    .bind(detail)
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

/// `_promote_trading_day` for each day, in one transaction: the exchange traded, so say so.
async fn promote_trading_days(ctx: &TaskContext, days: &[NaiveDate]) -> Result<(), WorkerError> {
    let mut tx = ctx.db.begin().await?;
    for day in days {
        sqlx::query(
            "INSERT INTO trading_day (exchange_id, date, is_trading_day, holiday_name, source) \
             VALUES ($1::SMALLINT, $2::DATE, $3::BOOLEAN, $4::VARCHAR, $5::VARCHAR) \
             ON CONFLICT (exchange_id, date) DO UPDATE SET is_trading_day = \
             excluded.is_trading_day, holiday_name = excluded.holiday_name, source = \
             excluded.source",
        )
        .bind(NSE_EXCHANGE_ID)
        .bind(*day)
        .bind(true)
        .bind(None::<String>)
        .bind(CORRECTED_SOURCE)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(())
}

/// `_repair_bars`: re-ingest one day's bhavcopy; whether anything landed.
async fn repair_bars(
    ctx: &TaskContext,
    provider: &baskfy_providers::composite::CompositeProvider,
    day: NaiveDate,
    outcome: &mut ResyncOutcome,
) -> Result<bool, WorkerError> {
    let mut tx = ctx.db.begin().await?;
    let report = ingest::backfill_summary(&mut tx, provider, DateWindow::new(day, day)?).await?;
    tx.commit().await?;
    if !report.failures.is_empty() {
        let mut failures = report.failures.clone();
        failures.sort_by(|a, b| a.0.cmp(&b.0));
        let reason = failures
            .iter()
            .map(|(k, v)| match v {
                OrderedJson::Str(text) => format!("{k}: {text}"),
                other => format!("{k}: {}", other.to_python_json().unwrap_or_default()),
            })
            .collect::<Vec<_>>()
            .join("; ");
        outcome.failed.push(format!(
            "{}: bhavcopy re-ingest failed — {reason}",
            iso(day)
        ));
        return Ok(false);
    }
    if !report.missing_days.is_empty() {
        outcome.failed.push(format!(
            "{}: NSE published no bhavcopy for this date, so the gap cannot be closed from the \
             bhavcopy archive (the UDiFF layout only reaches back to 2024).",
            iso(day)
        ));
        return Ok(false);
    }
    if report.bars_written == 0 {
        outcome.failed.push(format!(
            "{}: the bhavcopy parsed but matched no instrument in the register — run the \
             reference backfill (`make refdata`) before retrying.",
            iso(day)
        ));
        return Ok(false);
    }
    outcome.repaired.push(format!(
        "{}: re-ingested {} bars from the NSE bhavcopy",
        iso(day),
        thousands(report.bars_written)
    ));
    Ok(true)
}

/// `_send(producer, day)`: publish one `baskfy.pipeline.nightly`; `None` when the broker refuses.
async fn send(ctx: &TaskContext, day: NaiveDate) -> Option<String> {
    let queue = route_by_name(NIGHTLY);
    match producer::apply_async(
        ctx.worker.broker_url(),
        NIGHTLY,
        &[OrderedJson::Str(iso(day))],
        queue.name(),
    )
    .await
    {
        Ok(task_id) => Some(task_id),
        Err(exc) => {
            log_warning(&format!(
                "resync could not queue a nightly (date={}, error={exc})",
                iso(day)
            ));
            None
        }
    }
}

/// `run_resync(actor_user_id, days=days)`.
///
/// # Errors
/// The database's; the Kite refresh's own (its lane's).
pub async fn run_resync(
    ctx: &TaskContext,
    actor_user_id: i64,
    days: i64,
) -> Result<ResyncOutcome, WorkerError> {
    let deps = ctx.pipeline_dependencies().await?;
    let provider = Arc::clone(&deps.provider);
    let reference: Arc<dyn ReferenceProvider> = provider.clone();
    let publication = bhavcopy_publication_check(Some(reference));

    let plan = inspect(ctx, days, publication.clone()).await?;
    let target = format!("{}..{}", iso(plan.window_start), iso(plan.window_end));
    let mut outcome = ResyncOutcome {
        pending_at_start: i64::try_from(plan.findings.len()).unwrap_or(i64::MAX),
        ..ResyncOutcome::default()
    };

    if plan.findings.is_empty() {
        outcome.unresolved.clone_from(&plan.unresolved);
        record_completion(ctx, actor_user_id, &target, &outcome).await?;
        return Ok(outcome);
    }

    // (d) the Kite session, first: history and holdings depend on it, bars do not.
    if plan.findings.iter().any(|f| f.kind == "kite_session") {
        if ops_alerts::refresh_kite_session_quietly(ctx).await? {
            outcome
                .repaired
                .push("Kite session: pulled a fresh access token from the desk".to_owned());
        } else {
            outcome.failed.push(
                "Kite session: the desk could not be reached or its token was refused. Daily \
                 bars are unaffected (they come from the NSE bhavcopy); history before 2024, \
                 holdings sync and instrument metadata stay stale until this succeeds."
                    .to_owned(),
            );
        }
    }

    // (c) wrong inferred holidays, second: a day marked shut is skipped by every backfill.
    let wrong_holidays = dates(&plan, "wrong_holiday");
    if !wrong_holidays.is_empty() {
        promote_trading_days(ctx, &wrong_holidays).await?;
        for day in &wrong_holidays {
            outcome.repaired.push(format!(
                "{}: corrected trading_day — NSE published a bhavcopy, so the inferred holiday \
                 was wrong and backfills will no longer skip the date",
                iso(*day)
            ));
        }
    }

    // (b)+(c) the bars themselves.
    let bar_days: Vec<NaiveDate> = dates(&plan, "thin_bars")
        .into_iter()
        .chain(wrong_holidays.iter().copied())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let mut repaired_days: Vec<NaiveDate> = Vec::new();
    for day in bar_days.iter().take(MAX_BAR_REPAIRS) {
        if repair_bars(ctx, &provider, *day, &mut outcome).await? {
            repaired_days.push(*day);
        }
    }
    for day in bar_days.iter().skip(MAX_BAR_REPAIRS) {
        outcome.deferred.push(format!(
            "{}: not re-ingested this pass ({MAX_BAR_REPAIRS} days per press). Press resync \
             again.",
            iso(*day)
        ));
    }

    // (a) and the recompute repaired bars need.
    let rerun_days: Vec<NaiveDate> = dates(&plan, "missing_run")
        .into_iter()
        .chain(repaired_days.iter().copied())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let mut queued_days: BTreeSet<NaiveDate> = BTreeSet::new();
    for day in rerun_days.iter().take(MAX_RERUNS) {
        match send(ctx, *day).await {
            None => outcome.failed.push(format!(
                "{}: no task broker is reachable, so the nightly chain could not be queued for \
                 this date.",
                iso(*day)
            )),
            Some(sent) => {
                queued_days.insert(*day);
                outcome.queued.push(format!(
                    "{}: nightly chain queued (task {sent}). It publishes only if the quality \
                     gate passes — watch the run history.",
                    iso(*day)
                ));
            }
        }
    }
    for day in rerun_days.iter().skip(MAX_RERUNS) {
        outcome.deferred.push(format!(
            "{}: nightly chain not queued this pass ({MAX_RERUNS} per press). Press resync \
             again.",
            iso(*day)
        ));
    }

    // The third inspection: what is still wrong, measured rather than assumed.
    let after = inspect(ctx, days, publication).await?;
    outcome.still_pending = after
        .findings
        .iter()
        .filter(|f: &&ResyncFindingOut| {
            !(f.kind == "missing_run" && f.trade_date.is_some_and(|d| queued_days.contains(&d)))
        })
        .map(|f| f.summary.clone())
        .collect();
    outcome.unresolved = after.unresolved;
    record_completion(ctx, actor_user_id, &target, &outcome).await?;
    Ok(outcome)
}

/// The task's payload for an outcome (`run_resync(...).as_json()`).
#[must_use]
pub fn payload(outcome: &ResyncOutcome) -> OrderedJson {
    outcome.as_json()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn complete_is_narrow() {
        let mut outcome = ResyncOutcome::default();
        assert!(outcome.complete());
        outcome.unresolved.push("could not look".to_owned());
        assert!(outcome.complete(), "unresolved does not clear complete");
        outcome.queued.push("queued".to_owned());
        assert!(outcome.complete(), "a queued chain is not a failure");
        outcome.still_pending.push("x".to_owned());
        assert!(!outcome.complete());
        let text = crate::pyjson::dumps(&outcome.as_json()).unwrap_or_default();
        assert!(text.starts_with(r#"{"repaired": [], "queued": ["queued"], "failed": []"#));
        assert!(text.ends_with(r#""pending_at_start": 0, "complete": false}"#));
    }

    #[test]
    fn numbers_take_thousands_separators() {
        assert_eq!(thousands(2_310), "2,310");
        assert_eq!(thousands(322), "322");
        assert_eq!(thousands(1_234_567), "1,234,567");
    }
}
