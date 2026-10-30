//! The nightly task with an explicit date. Ports `tasks/celery_tasks.nightly_pipeline` when it
//! is handed a `trade_date` — the path Beat's shell-out will take at cutover (DECISIONS-RS R7.1).
//!
//! Three transactions, as Python's:
//!
//! 1. [`ops::begin_run`](crate::ops::begin_run) commits the `pipeline_run` row **before** the chain;
//! 2. the chain, in the one transaction [`crate::orchestrator`] describes — committed when it
//!    returns (a hard failure included), rolled back when it raises;
//! 3. after a raise, [`ops::fail_run`](crate::ops::fail_run) on a third transaction, then the
//!    `pipeline_failed` alert, then the error is returned.
//!
//! An explicit date bypasses the scheduled path's guards (not a trading day, already published,
//! before [`SESSION_DATA_READY_IST`](crate::catch_up::SESSION_DATA_READY_IST)), exactly as
//! Python's does: a human or the catch-up sweep asking for a date means it. The Celery task
//! keeps those guards and its `kite_session_cli.refresh_quietly()` until the shell-out lands.

use baskfy_api::Settings;
use baskfy_db::{OrderedJson, WriteDb};
use chrono::NaiveDate;

use crate::alerts::{alert_for_outcome, chain_raised, dispatch};
use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::ops;
use crate::orchestrator::{PipelineOutcome, run_nightly_pipeline};
use crate::pyjson::{self, object, s};

/// What the task returns (`nightly_pipeline`'s dict), or the error it raised with the alert it
/// dispatched first.
#[derive(Debug)]
pub enum NightlyResult {
    /// The chain returned (succeeded, failed at the gate, or aborted).
    Returned {
        /// The outcome.
        outcome: PipelineOutcome,
        /// The task's return value, in Python's key order.
        payload: OrderedJson,
    },
    /// The chain raised; the run is marked failed and the alert sent.
    Raised {
        /// The run.
        run_id: i64,
        /// The error.
        error: WorkerError,
    },
}

/// The task's return value for an outcome.
#[must_use]
pub fn payload(outcome: &PipelineOutcome, alert: Option<&str>) -> OrderedJson {
    object([
        (
            "run_id",
            outcome.run_id.map_or(OrderedJson::Null, OrderedJson::Int),
        ),
        (
            "trade_date",
            s(outcome.trade_date.format("%Y-%m-%d").to_string()),
        ),
        ("status", s(outcome.status.value())),
        (
            "data_version",
            outcome
                .data_version
                .map_or(OrderedJson::Null, OrderedJson::Int),
        ),
        (
            "failed_step",
            pyjson::opt_s(outcome.failed_step.map(|s| s.value())),
        ),
        ("error", pyjson::opt_s(outcome.error.clone())),
        (
            "steps_completed",
            pyjson::strings(outcome.steps_completed.iter().map(|s| s.value())),
        ),
        ("alert", pyjson::opt_s(alert)),
    ])
}

/// `nightly_pipeline(trade_date)`.
///
/// # Errors
/// The database's on the bookkeeping transactions (1 and 3); a raised chain is
/// [`NightlyResult::Raised`], not an `Err`.
pub async fn run_nightly(
    db: &WriteDb,
    settings: &Settings,
    deps: &PipelineDependencies,
    day: NaiveDate,
) -> Result<NightlyResult, WorkerError> {
    let run_id = {
        let mut tx = db.begin().await?;
        let id = ops::begin_run(&mut tx, day).await?;
        tx.commit().await?;
        id
    };
    let chained = {
        let mut tx = db.begin().await?;
        match run_nightly_pipeline(&mut tx, day, deps, Some(run_id)).await {
            Ok(outcome) => {
                tx.commit().await?;
                Ok(outcome)
            }
            Err(e) => {
                // Dropping the transaction would roll it back too; say it.
                let _ = tx.rollback().await;
                Err(e)
            }
        }
    };
    match chained {
        Ok(outcome) => {
            let alert = alert_for_outcome(&outcome);
            if let Some(alert) = &alert {
                dispatch(alert, settings).await;
            }
            let payload = payload(&outcome, alert.as_ref().map(|a| a.name.value()));
            Ok(NightlyResult::Returned { outcome, payload })
        }
        Err(error) => {
            let message = error.to_string();
            let kind = error.python_type();
            let mut tx = db.begin().await?;
            ops::fail_run(&mut tx, run_id, &message).await?;
            tx.commit().await?;
            dispatch(&chain_raised(day, run_id, &kind, &message), settings).await;
            Ok(NightlyResult::Raised { run_id, error })
        }
    }
}
