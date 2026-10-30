//! The tasks wave 5's code already answers, wired by the wave-6 foundation (DECISIONS-RS R8.1).
//!
//! | Celery name | Python | Rust |
//! |---|---|---|
//! | `baskfy.pipeline.nightly` | `celery_tasks.nightly_pipeline` | [`nightly_pipeline`]: both paths — an explicit date ([`crate::nightly::run_nightly`]), and the scheduled path's three guards on the injected clock |
//! | `baskfy.pipeline.session_catch_up` | `celery_tasks.session_catch_up` | [`session_catch_up`]: the sweep ([`crate::catch_up`]), the nightly per day, the AF 3.11 demotion, and the swing EOD plan through the registry (the `swing_eod` lane's) |
//! | `baskfy.compute.reprocess_instrument` | `celery_tasks.reprocess_instrument_task` | [`reprocess_instrument_task`] over [`crate::steps::ingest::reprocess_instrument`] |
//! | `baskfy.pipeline.integrity_audit` | `celery_tasks.integrity_audit` | [`integrity_audit`] |
//!
//! Each Python `run_in_session` is one transaction here (`session_scope`: commit on return, roll
//! back on raise).

use std::sync::Arc;

use baskfy_db::OrderedJson;
use baskfy_providers::ports::ReferenceProvider;
use baskfy_providers::publication::bhavcopy_publication_check;
use chrono::NaiveDate;

use super::{BoundArgs, TaskContext, TaskResult, find, ops_alerts};
use crate::catch_up::{
    SESSION_DATA_READY_IST, demote_derived_after_missing_bhavcopy, unlanded_sessions,
};
use crate::error::WorkerError;
use crate::nightly::{NightlyResult, run_nightly};
use crate::ops;
use crate::pyjson::{int, object, s, strings};
use crate::steps::{ingest, log_warning};

/// The tasks this module runs (every one of its four).
pub const PORTED: &[&str] = &[
    "baskfy.pipeline.nightly",
    "baskfy.pipeline.session_catch_up",
    "baskfy.compute.reprocess_instrument",
    "baskfy.pipeline.integrity_audit",
];

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

fn skipped(day: NaiveDate, reason: String) -> OrderedJson {
    object([
        ("trade_date", s(iso(day))),
        ("status", s("skipped")),
        ("reason", s(reason)),
    ])
}

/// `run_in_session(lambda s: ops.is_trading_day(s, day))`.
async fn is_trading_day(ctx: &TaskContext, day: NaiveDate) -> Result<bool, WorkerError> {
    let mut tx = ctx.db.begin().await?;
    let answer = ops::is_trading_day(&mut tx, day).await?;
    tx.commit().await?;
    Ok(answer)
}

/// `run_already_published(day)`: a run for the date with a `data_version`. A failure to read
/// answers `false` ("a box that cannot tell should do the work").
async fn run_already_published(ctx: &TaskContext, day: NaiveDate) -> bool {
    let read = async {
        let mut tx = ctx.db.begin().await?;
        let row: Option<(i64,)> = sqlx::query_as(
            "SELECT pipeline_run.id \nFROM pipeline_run \nWHERE pipeline_run.trade_date = $1::DATE \
             AND pipeline_run.data_version IS NOT NULL \n LIMIT $2::INTEGER",
        )
        .bind(day)
        .bind(1_i32)
        .fetch_optional(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok::<bool, WorkerError>(row.is_some())
    };
    match read.await {
        Ok(answer) => answer,
        Err(_) => {
            log_warning(&format!(
                "could not tell whether {} is published; running the chain",
                iso(day)
            ));
            false
        }
    }
}

/// `nightly_pipeline(trade_date=None)` — `baskfy.pipeline.nightly`.
///
/// An explicit `trade_date` runs the chain for it (no guard: a human asking for a date means
/// it). Without one — Beat's path — the day is today in IST (the injected clock), and the task
/// skips a day that is not a session, a day already published, and today before
/// [`SESSION_DATA_READY_IST`] (18:00). Python tests `trade_date is None` for the guards but
/// `if trade_date` for the date, so an empty string runs today's chain unguarded; so does this.
/// Both paths then borrow the Kite session ([`ops_alerts::refresh_kite_session_quietly`]) and run
/// the chain; a raised chain is recorded and alerted by [`run_nightly`] and raised again.
///
/// # Errors
/// `ValueError` for a date that does not parse; the chain's own error after it was recorded.
pub async fn nightly_pipeline(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let trade_date = args.opt_str("trade_date")?;
    nightly_body(ctx, trade_date.as_deref()).await
}

async fn nightly_body(ctx: &TaskContext, trade_date: Option<&str>) -> TaskResult {
    let day = match trade_date {
        Some(text) if !text.is_empty() => super::args::fromisoformat_date(text)?,
        _ => ctx.today_ist(),
    };
    if trade_date.is_none() {
        if !is_trading_day(ctx, day).await? {
            return Ok(skipped(day, "not a trading day".to_owned()));
        }
        let now = ctx.now_ist();
        if run_already_published(ctx, day).await {
            return Ok(skipped(
                day,
                format!("{} is already published; nothing to re-run", iso(day)),
            ));
        }
        if now.date() == day && now.time() < SESSION_DATA_READY_IST {
            return Ok(skipped(
                day,
                format!(
                    "the {} session has not published yet (now {} IST, data expected after {})",
                    iso(day),
                    now.format("%H:%M"),
                    SESSION_DATA_READY_IST.format("%H:%M"),
                ),
            ));
        }
    }
    ops_alerts::refresh_kite_session_quietly(ctx).await?;
    let deps = ctx.pipeline_dependencies().await?;
    match run_nightly(&ctx.db, &ctx.settings, &deps, day).await? {
        NightlyResult::Returned { payload, .. } => Ok(payload),
        NightlyResult::Raised { error, .. } => Err(error),
    }
}

/// Python's `missing[:n]` / `missing[n:]` for an `int` bound (a negative one counts from the end).
fn slice_point(len: usize, bound: i64) -> usize {
    let len_i = i64::try_from(len).unwrap_or(i64::MAX);
    let at = if bound < 0 {
        (len_i + bound).max(0)
    } else {
        bound.min(len_i)
    };
    usize::try_from(at).unwrap_or(0)
}

/// `_probe_and_demote_derived(session, day)`: ask whether NSE published a bhavcopy for `day`
/// (through the provider stack — the replay under the harness), and demote a derived holiday
/// guess on a clear "nothing published".
async fn probe_and_demote(ctx: &TaskContext, day: NaiveDate) -> Result<bool, WorkerError> {
    let deps = ctx.pipeline_dependencies().await?;
    let reference: Arc<dyn ReferenceProvider> = deps.provider.clone();
    let probe = bhavcopy_publication_check(Some(reference));
    let published = match probe {
        Some(check) => check(day).await,
        None => false,
    };
    let mut tx = ctx.db.begin().await?;
    let demoted = demote_derived_after_missing_bhavcopy(&mut tx, day, published).await?;
    tx.commit().await?;
    Ok(demoted)
}

/// `session_catch_up(lookback_days=7, max_sessions=2)` — `baskfy.pipeline.session_catch_up`.
///
/// The sessions that never landed (published, or refused by the gate, count as landed; a day
/// still before [`SESSION_DATA_READY_IST`] is never proposed), oldest first; the nightly for at
/// most `max_sessions` of them in this process; for a chain that did not publish, the AF 3.11
/// probe-and-demote; for one that did, the swing EOD plan (`baskfy.swing.eod`'s body, through
/// the registry — a failure there is reported by class name, as Python's `except Exception`).
/// A chain that raises propagates, as Python's does.
///
/// # Errors
/// The database's; the nightly's raised error.
pub async fn session_catch_up(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let lookback_days = args.int("lookback_days")?;
    let max_sessions = args.int("max_sessions")?;
    let now = ctx.now_ist();
    let missing = {
        let mut tx = ctx.db.begin().await?;
        let days = unlanded_sessions(&mut tx, now.date(), lookback_days, Some(now)).await?;
        tx.commit().await?;
        days
    };
    if missing.is_empty() {
        return Ok(object([
            ("status", s("nothing to do")),
            ("checked_through", s(iso(now.date()))),
            ("lookback_days", int(lookback_days)),
            ("sessions", OrderedJson::Array(Vec::new())),
        ]));
    }
    let cut = slice_point(missing.len(), max_sessions);
    let (to_run, remaining) = missing.split_at(cut);
    let mut ran = Vec::new();
    let mut planned = Vec::new();
    let mut demoted = Vec::new();
    for day in to_run {
        log_warning(&format!(
            "catch-up: {} never landed; running the chain for it",
            iso(*day)
        ));
        let chain = nightly_body(ctx, Some(&iso(*day))).await?;
        let published = matches!(
            chain.get("data_version"),
            Some(v) if !matches!(v, OrderedJson::Null)
        );
        ran.push(chain);
        if !published {
            if probe_and_demote(ctx, *day).await? {
                demoted.push(iso(*day));
            }
            planned.push(object([
                ("date", s(iso(*day))),
                ("skipped", s("the chain did not publish")),
            ]));
            continue;
        }
        let plan = match find("baskfy.swing.eod") {
            Some(spec) => {
                let call = BoundArgs::from_pairs(vec![("trade_date", s(iso(*day)))]);
                (spec.run)(ctx, call).await
            }
            None => Err(WorkerError::python("NotRegistered", "'baskfy.swing.eod'")),
        };
        match plan {
            Ok(value) => planned.push(value),
            Err(e) => {
                log_warning(&format!(
                    "catch-up: {} landed but its swing plan did not: {e}",
                    iso(*day)
                ));
                planned.push(object([
                    ("date", s(iso(*day))),
                    ("error", s(e.python_type())),
                ]));
            }
        }
    }
    Ok(object([
        ("status", s("ran")),
        ("checked_through", s(iso(now.date()))),
        ("lookback_days", int(lookback_days)),
        ("sessions", strings(missing.iter().map(|d| iso(*d)))),
        ("ran", OrderedJson::Array(ran)),
        ("swing_plans", OrderedJson::Array(planned)),
        ("demoted_derived_holidays", strings(demoted)),
        ("remaining", strings(remaining.iter().map(|d| iso(*d)))),
    ]))
}

/// `reprocess_instrument_task(instrument_id)` — `baskfy.compute.reprocess_instrument`: rebuild one
/// instrument's whole adjusted history with every action known now (the "as of today" series the
/// nightly also writes — house rule 5's obligation sits with the reader). Idempotent.
///
/// # Errors
/// The database's (rolled back).
pub async fn reprocess_instrument_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let instrument_id = args.int("instrument_id")?;
    let mut tx = ctx.db.begin().await?;
    let result = ingest::reprocess_instrument(&mut tx, instrument_id, None).await?;
    tx.commit().await?;
    Ok(object([
        ("instrument_id", int(result.instrument_id)),
        ("bars_rewritten", int(result.bars_rewritten)),
        ("actions_applied", int(result.actions_applied)),
        ("unquantified", strings(result.unquantified)),
    ]))
}

/// `integrity_audit()` — `baskfy.pipeline.integrity_audit`: every instrument with any corporate
/// action, rebuilt on one transaction; a healthy history is rewritten to identical values.
///
/// # Errors
/// The database's (the whole audit rolls back).
pub async fn integrity_audit(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let mut tx = ctx.db.begin().await?;
    let targets: Vec<(i64,)> = sqlx::query_as(
        "SELECT DISTINCT corporate_action.instrument_id \nFROM corporate_action ORDER BY \
         corporate_action.instrument_id",
    )
    .fetch_all(&mut *tx)
    .await?;
    let mut rewritten = 0_i64;
    let mut unquantified = Vec::new();
    for (instrument_id,) in &targets {
        let result = ingest::reprocess_instrument(&mut tx, *instrument_id, None).await?;
        rewritten += result.bars_rewritten;
        unquantified.extend(
            result
                .unquantified
                .iter()
                .map(|u| format!("instrument {instrument_id}: {u}")),
        );
    }
    tx.commit().await?;
    Ok(object([
        (
            "instruments",
            int(i64::try_from(targets.len()).unwrap_or(i64::MAX)),
        ),
        ("bars_rewritten", int(rewritten)),
        (
            "unquantified_actions",
            if unquantified.is_empty() {
                OrderedJson::Null
            } else {
                strings(unquantified)
            },
        ),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slices_as_python() {
        assert_eq!(slice_point(3, 2), 2);
        assert_eq!(slice_point(3, 5), 3);
        assert_eq!(slice_point(3, -1), 2);
        assert_eq!(slice_point(3, -5), 0);
        assert_eq!(slice_point(0, 2), 0);
    }
}
