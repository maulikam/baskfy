//! The nightly pipeline orchestrator. Ports `baskfy_worker.orchestrator`.
//!
//! > "A PipelineRun orchestrator that runs the chain for a trade date, stops on the first hard
//! > failure, and only bumps `data_version` after the quality gate passes."
//!
//! # Transaction shape (Python's, exactly)
//!
//! The whole chain runs inside **one** transaction on the connection it is handed. A step that
//! raises propagates out of [`run_nightly_pipeline`] and the caller rolls back — data and step
//! rows together (`nightly.rs` then records the failure on a third transaction). A **hard
//! failure** (the gate refusing) is different: it is caught here, the run is closed `failed`,
//! and the caller **commits** — every row the chain wrote lands, the `data_version` does not.
//! That is what Python's `run_nightly_pipeline` + `session_scope` do, whatever the module
//! docstring says about rolling back.
//!
//! # Blocks
//!
//! `_run_chain` is one linear function in Python. Here each step's block — the `record_step`
//! and the glue around it that belongs to it (`active_instruments` before step 2, the calendar
//! reconcile after it, the desk score inside step 7, the hard-failure check after step 9) — is
//! [`run_block`], and [`run_chain`] walks [`NIGHTLY_CHAIN`] through it. The statements run in
//! Python's order; the split exists so `baskfy-worker step <name>` can run exactly one block
//! into an open run for grading (`tools/parity/nightly_diff.py --step`).
//!
//! The post-publish steps cannot fail the run: their wrappers turn any error into `skipped`
//! with `skipped_reason = "{type}: {message}"`, as Python's do.

use std::collections::BTreeSet;

use baskfy_db::OrderedJson;
use chrono::{Days, NaiveDate};
use sqlx::{Connection as _, PgConnection};

use crate::calendar::{CALENDAR_LOOKBACK_DAYS, require_trading_day};
use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::gate::GateReport;
use crate::steps::{
    HardFailure, NIGHTLY_CHAIN, PipelineRun, PipelineStep, RunStatus, StepOutcome, StepStatus,
    adopt_run, close_run, compute, gate, index, ingest, log_warning, open_run, record_step,
    sleeves,
};
use crate::window::DateWindow;

/// `CORPORATE_ACTION_LOOKBACK_DAYS`: a week covers a long weekend plus a late NSE publication.
pub const CORPORATE_ACTION_LOOKBACK_DAYS: u64 = 7;

/// `PIPELINE_CHAIN_LOCK_KEY = 0x0BA5_C1FA`: the advisory-lock key the chain serialises on —
/// **the same key as Python's**, so a Rust chain and a Python chain can never run concurrently.
pub const PIPELINE_CHAIN_LOCK_KEY: i64 = 0x0BA5_C1FA;

/// The message a chain that loses the lock returns with.
pub const LOCK_HELD_MESSAGE: &str = "another pipeline run holds the chain lock; this attempt \
                                     was abandoned rather than queued behind it";

/// `PipelineOutcome`: what a run did, in memory — survives the rollback a raise triggers.
#[derive(Debug, Clone, PartialEq)]
pub struct PipelineOutcome {
    /// The session.
    pub trade_date: NaiveDate,
    /// The run's status.
    pub status: RunStatus,
    /// The `pipeline_run` row; `None` only when the run never opened.
    pub run_id: Option<i64>,
    /// What `publish` assigned.
    pub data_version: Option<i64>,
    /// The gate's report, once it ran.
    pub gate: Option<GateReport>,
    /// The step that stopped the chain.
    pub failed_step: Option<PipelineStep>,
    /// Why.
    pub error: Option<String>,
    /// Every step whose block finished, in order.
    pub steps_completed: Vec<PipelineStep>,
}

impl PipelineOutcome {
    fn new(trade_date: NaiveDate, status: RunStatus, run_id: Option<i64>) -> Self {
        Self {
            trade_date,
            status,
            run_id,
            data_version: None,
            gate: None,
            failed_step: None,
            error: None,
            steps_completed: Vec::new(),
        }
    }

    /// `published`: succeeded with a `data_version`.
    #[must_use]
    pub fn published(&self) -> bool {
        self.status == RunStatus::Succeeded && self.data_version.is_some()
    }
}

/// What the chain carries from one block to a later one. In a full run each block fills its
/// part; `baskfy-worker step` takes it from a context file (`touched`, the only value a block
/// needs from an earlier one that the database cannot answer).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ChainState {
    /// Step 3's answer, step 4's input: the instrument ids whose actions changed.
    pub touched: Option<BTreeSet<i64>>,
}

/// Why a block stopped the chain.
#[derive(Debug)]
pub enum ChainStop {
    /// The gate refused (`HardFailure`): caught by [`run_nightly_pipeline`].
    Hard(HardFailure),
    /// A step raised: propagates to the caller, which rolls back.
    Raised(WorkerError),
}

impl From<WorkerError> for ChainStop {
    fn from(e: WorkerError) -> Self {
        Self::Raised(e)
    }
}

/// `try_lock_pipeline`: claim the chain lock for this transaction (`pg_try_advisory_xact_lock`),
/// or answer `false` if another chain — in either language — holds it.
///
/// # Errors
/// The database's.
pub async fn try_lock_pipeline(conn: &mut PgConnection) -> Result<bool, WorkerError> {
    let (held,): (bool,) = sqlx::query_as("SELECT pg_try_advisory_xact_lock($1)")
        .bind(PIPELINE_CHAIN_LOCK_KEY)
        .fetch_one(&mut *conn)
        .await?;
    Ok(held)
}

/// `run_nightly_pipeline`: docs/03's chain for `trade_date`. Returns rather than raises for a
/// non-trading day, a held lock and a hard failure; a raised step is `Err` and the caller rolls
/// back.
///
/// `run_id` names a `pipeline_run` row already committed by [`crate::ops::begin_run`]; `None`
/// opens the row inside this transaction (the in-process acceptance shape).
///
/// # Errors
/// A raised step, or the database's.
pub async fn run_nightly_pipeline(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
    deps: &PipelineDependencies,
    run_id: Option<i64>,
) -> Result<PipelineOutcome, WorkerError> {
    if let Err(not_trading) = require_trading_day(conn, trade_date).await? {
        if let Some(id) = run_id {
            let mut run = adopt_run(conn, id).await?;
            close_run(conn, &mut run, RunStatus::Aborted, None).await?;
        }
        let mut outcome = PipelineOutcome::new(trade_date, RunStatus::Aborted, run_id);
        outcome.error = Some(not_trading.to_string());
        return Ok(outcome);
    }
    if !try_lock_pipeline(conn).await? {
        if let Some(id) = run_id {
            let mut run = adopt_run(conn, id).await?;
            close_run(conn, &mut run, RunStatus::Aborted, None).await?;
        }
        let mut outcome = PipelineOutcome::new(trade_date, RunStatus::Aborted, run_id);
        outcome.error = Some(LOCK_HELD_MESSAGE.to_owned());
        return Ok(outcome);
    }
    let mut run = match run_id {
        Some(id) => adopt_run(conn, id).await?,
        None => open_run(conn, trade_date).await?,
    };
    let mut outcome = PipelineOutcome::new(trade_date, RunStatus::Running, Some(run.id));
    let mut state = ChainState::default();
    match run_chain(conn, &mut run, trade_date, deps, &mut outcome, &mut state).await {
        Ok(()) => {
            outcome.status = RunStatus::Succeeded;
            Ok(outcome)
        }
        Err(ChainStop::Hard(HardFailure(message))) => {
            outcome.status = RunStatus::Failed;
            outcome.error = Some(message);
            close_run(conn, &mut run, RunStatus::Failed, None).await?;
            Ok(outcome)
        }
        Err(ChainStop::Raised(e)) => Err(e),
    }
}

/// `_run_chain`: every block of [`NIGHTLY_CHAIN`], in order.
///
/// # Errors
/// [`ChainStop`].
pub async fn run_chain(
    conn: &mut PgConnection,
    run: &mut PipelineRun,
    trade_date: NaiveDate,
    deps: &PipelineDependencies,
    outcome: &mut PipelineOutcome,
    state: &mut ChainState,
) -> Result<(), ChainStop> {
    for step in NIGHTLY_CHAIN {
        run_block(conn, run, trade_date, deps, step, outcome, state).await?;
    }
    Ok(())
}

/// One step's block of `_run_chain`, glue included; appends the step to
/// `outcome.steps_completed` when the block finishes.
///
/// # Errors
/// [`ChainStop`]: `Hard` after the gate's row is written when it refused; `Raised` otherwise.
pub async fn run_block(
    conn: &mut PgConnection,
    run: &mut PipelineRun,
    trade_date: NaiveDate,
    deps: &PipelineDependencies,
    step: PipelineStep,
    outcome: &mut PipelineOutcome,
    state: &mut ChainState,
) -> Result<(), ChainStop> {
    let run_id = run.id;
    let day = Some(trade_date);
    let window = deps
        .bars_window
        .unwrap_or_else(|| DateWindow::single(trade_date));
    match step {
        PipelineStep::RefreshInstruments => {
            record_step(conn, run_id, step, day, async |c, s| {
                ingest::refresh_instruments(c, deps, trade_date, s).await
            })
            .await?;
        }
        PipelineStep::FetchDailyBars => {
            let active = ingest::active_instruments(conn).await?;
            record_step(conn, run_id, step, day, async |c, s| {
                ingest::fetch_daily_bars(c, deps, s, &active, window).await
            })
            .await?;
            // docs/09: the calendar is "asserted against observed bar dates", over the longest
            // factor window and not only tonight (T9.2).
            let lookback = trade_date
                .checked_sub_days(Days::new(CALENDAR_LOOKBACK_DAYS.unsigned_abs()))
                .unwrap_or(trade_date);
            let start = window.start().min(lookback);
            let calendar = ingest::reconcile_calendar(conn, deps, start, window.end()).await?;
            if !calendar.missed_sessions.is_empty() {
                let days: Vec<String> = calendar
                    .missed_sessions
                    .iter()
                    .map(|d| d.format("%Y-%m-%d").to_string())
                    .collect();
                log_warning(&format!(
                    "sessions NSE published but we did not ingest: {}",
                    days.join(", ")
                ));
            }
        }
        PipelineStep::FetchCorporateActions => {
            let since = trade_date
                .checked_sub_days(Days::new(CORPORATE_ACTION_LOOKBACK_DAYS))
                .unwrap_or(trade_date);
            let touched = record_step(conn, run_id, step, day, async |c, s| {
                ingest::fetch_corporate_actions(c, deps, s, since).await
            })
            .await?;
            state.touched = Some(touched);
        }
        PipelineStep::ApplyAdjustments => {
            let Some(touched) = state.touched.clone() else {
                return Err(ChainStop::Raised(WorkerError::python(
                    "RuntimeError",
                    "apply_adjustments needs fetch_corporate_actions' touched instruments; run \
                     the chain, or pass them in the step context",
                )));
            };
            record_step(conn, run_id, step, day, async |c, s| {
                if touched.is_empty() {
                    s.status = StepStatus::Skipped;
                }
                ingest::apply_adjustments(c, s, &touched).await
            })
            .await?;
        }
        PipelineStep::RefreshIndexMembership => {
            record_step(conn, run_id, step, day, async |c, s| {
                index::refresh_index_membership(c, deps, s, trade_date).await
            })
            .await?;
        }
        PipelineStep::RefreshIndexSnapshots => {
            record_step(conn, run_id, step, day, async |c, s| {
                index::refresh_index_snapshots(c, deps, s, trade_date).await
            })
            .await?;
        }
        PipelineStep::ComputeFactors => {
            record_step(conn, run_id, step, day, async |c, s| {
                compute::compute_factors(c, deps, s, trade_date).await?;
                let desk = run_compute_desk_score_step(c, trade_date).await;
                let mut detail = desk.detail.clone();
                pyjson::update(&mut detail, "status", pyjson::s(desk.status.value()));
                s.note("desk_score", OrderedJson::Object(detail));
                s.note("desk_score_rows", OrderedJson::Int(desk.rows_out));
                Ok(())
            })
            .await?;
        }
        PipelineStep::ComputeMarketHealth => {
            record_step(conn, run_id, step, day, async |c, s| {
                compute::compute_market_health(c, s, trade_date).await
            })
            .await?;
        }
        PipelineStep::DataQualityGate => {
            let report = record_step(conn, run_id, step, day, async |c, s| {
                let report = gate::data_quality_gate(c, s, trade_date).await?;
                if !report.passed() {
                    s.status = StepStatus::Failed;
                }
                Ok(report)
            })
            .await?;
            let passed = report.passed();
            let failures: Vec<String> = report
                .failures()
                .iter()
                .map(|r| format!("[{}] {}", r.assertion, r.message))
                .collect();
            outcome.gate = Some(report);
            outcome.steps_completed.push(step);
            if !passed {
                outcome.failed_step = Some(PipelineStep::DataQualityGate);
                return Err(ChainStop::Hard(HardFailure(format!(
                    "the data-quality gate failed: {}",
                    failures.join("; ")
                ))));
            }
            return Ok(());
        }
        PipelineStep::Publish => {
            let result = record_step(conn, run_id, step, day, async |c, s| {
                gate::publish(c, deps, s, run).await
            })
            .await?;
            outcome.data_version = Some(result.data_version);
        }
        PipelineStep::RefreshBasket => {
            record_step(conn, run_id, step, day, async |c, s| {
                let names = sleeves::refresh_basket(c, s, trade_date).await?;
                if names == 0 {
                    s.status = StepStatus::Skipped;
                }
                Ok(())
            })
            .await?;
        }
        PipelineStep::ComputeSwing => {
            run_compute_swing_step(conn, run_id, trade_date, deps).await?;
        }
        PipelineStep::ComputeVbt => {
            run_compute_vbt_step(conn, run_id, trade_date, deps).await?;
        }
        PipelineStep::ComputeTwt => {
            run_compute_twt_step(conn, run_id, trade_date, deps).await?;
        }
    }
    outcome.steps_completed.push(step);
    Ok(())
}

/// The `skipped` outcome a post-publish wrapper records for a detector that raised.
fn skipped_by(
    step: &mut StepOutcome,
    name: PipelineStep,
    trade_date: NaiveDate,
    exc: &WorkerError,
) {
    step.status = StepStatus::Skipped;
    step.note(
        "skipped_reason",
        pyjson::s(format!("{}: {exc}", exc.python_type())),
    );
    log_warning(&format!(
        "{} failed for {}: {exc}",
        name.value(),
        trade_date.format("%Y-%m-%d")
    ));
}

/// `run_compute_desk_score_step`: C2's write inside a **savepoint**, which cannot raise. A
/// failure rolls back to the savepoint (the screener's rows and the audit trail survive) and is
/// recorded on the returned outcome as `failed` with `error = "{type}: {message}"`.
pub async fn run_compute_desk_score_step(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> StepOutcome {
    let mut desk = StepOutcome::default();
    let result: Result<(), WorkerError> = async {
        let mut savepoint = conn.begin().await?;
        match compute::compute_desk_score(&mut savepoint, &mut desk, trade_date).await {
            Ok(()) => {
                savepoint.commit().await?;
                Ok(())
            }
            Err(e) => {
                // Rolling back to the savepoint is what `begin_nested` does on the way out; if
                // even that fails the outer transaction is lost and the next statement says so.
                let _ = savepoint.rollback().await;
                Err(e)
            }
        }
    }
    .await;
    if let Err(exc) = result {
        desk.status = StepStatus::Failed;
        desk.rows_out = 0;
        desk.note("error", pyjson::s(format!("{}: {exc}", exc.python_type())));
        log_warning(&format!(
            "compute_desk_score failed for {}: {exc}",
            trade_date.format("%Y-%m-%d")
        ));
    }
    desk
}

/// Step 14 (TW4): the three-weeks-tight detector. **Cannot raise** (beyond the step row's own
/// writes): `twt_nightly_enabled` false or no sole tenant skip it; a detector error is
/// `skipped`. `twt_execution_enabled` is not read here at all.
///
/// # Errors
/// The database's, on the step row itself.
pub async fn run_compute_twt_step(
    conn: &mut PgConnection,
    run_id: i64,
    trade_date: NaiveDate,
    deps: &PipelineDependencies,
) -> Result<(), WorkerError> {
    let name = PipelineStep::ComputeTwt;
    record_step(conn, run_id, name, Some(trade_date), async |c, s| {
        if !deps.twt_nightly_enabled {
            s.status = StepStatus::Skipped;
            s.note(
                "skipped_reason",
                pyjson::s("BASKFY_TWT_NIGHTLY_ENABLED is false"),
            );
            return Ok(());
        }
        let Some(user_id) = deps.twt_user_id else {
            s.status = StepStatus::Skipped;
            s.note(
                "skipped_reason",
                pyjson::s("no BASKFY_SOLE_USER_ID configured"),
            );
            return Ok(());
        };
        if let Err(exc) = sleeves::detect_twt(c, s, trade_date, user_id, run_id).await {
            skipped_by(s, name, trade_date, &exc);
        }
        Ok(())
    })
    .await
}

/// Step 13 (VB4): the volume-breakout detector. Cannot raise, as [`run_compute_twt_step`].
///
/// # Errors
/// The database's, on the step row itself.
pub async fn run_compute_vbt_step(
    conn: &mut PgConnection,
    run_id: i64,
    trade_date: NaiveDate,
    deps: &PipelineDependencies,
) -> Result<(), WorkerError> {
    let name = PipelineStep::ComputeVbt;
    record_step(conn, run_id, name, Some(trade_date), async |c, s| {
        if !deps.vbt_nightly_enabled {
            s.status = StepStatus::Skipped;
            s.note(
                "skipped_reason",
                pyjson::s("BASKFY_VBT_NIGHTLY_ENABLED is false"),
            );
            return Ok(());
        }
        let Some(user_id) = deps.vbt_user_id else {
            s.status = StepStatus::Skipped;
            s.note(
                "skipped_reason",
                pyjson::s("no BASKFY_SOLE_USER_ID configured"),
            );
            return Ok(());
        };
        if let Err(exc) = sleeves::detect_vbt(c, s, trade_date, user_id, run_id).await {
            skipped_by(s, name, trade_date, &exc);
        }
        Ok(())
    })
    .await
}

/// Step 12 (SW3): the swing detectors. Cannot raise, as [`run_compute_twt_step`]; no sole tenant
/// skips it (the `sw_` schema is keyed by user and a nightly job may not invent one).
///
/// # Errors
/// The database's, on the step row itself.
pub async fn run_compute_swing_step(
    conn: &mut PgConnection,
    run_id: i64,
    trade_date: NaiveDate,
    deps: &PipelineDependencies,
) -> Result<(), WorkerError> {
    let name = PipelineStep::ComputeSwing;
    record_step(conn, run_id, name, Some(trade_date), async |c, s| {
        let Some(user_id) = deps.swing_user_id else {
            s.status = StepStatus::Skipped;
            s.note(
                "skipped_reason",
                pyjson::s("no BASKFY_SOLE_USER_ID configured"),
            );
            return Ok(());
        };
        if let Err(exc) = sleeves::detect_swing(
            c,
            s,
            trade_date,
            user_id,
            &deps.swing_index_slug,
            deps.swing_execution_enabled,
            run_id,
        )
        .await
        {
            skipped_by(s, name, trade_date, &exc);
        }
        Ok(())
    })
    .await
}

/// What `baskfy-worker step` reports for one block.
#[derive(Debug, Clone, PartialEq)]
pub struct SingleStep {
    /// The run's outcome bookkeeping after the block (gate, data_version, failed_step).
    pub outcome: PipelineOutcome,
    /// The gate's refusal, when the block was the gate and it refused (the row is written and
    /// committed; the run is **not** closed — that is the full chain's job).
    pub hard_failure: Option<String>,
}

/// Run exactly one block into the open run `run_id` (grading: `nightly_diff.py --step`). Takes
/// the chain lock first, as a chain would.
///
/// # Errors
/// A raised step (the caller rolls back), a missing run (`LookupError`), or the lock held.
pub async fn run_single_step(
    conn: &mut PgConnection,
    run_id: i64,
    trade_date: NaiveDate,
    deps: &PipelineDependencies,
    step: PipelineStep,
    mut state: ChainState,
) -> Result<SingleStep, WorkerError> {
    if !try_lock_pipeline(conn).await? {
        return Err(WorkerError::python("RuntimeError", LOCK_HELD_MESSAGE));
    }
    let mut run = adopt_run(conn, run_id).await?;
    let mut outcome = PipelineOutcome::new(trade_date, RunStatus::Running, Some(run.id));
    match run_block(
        conn,
        &mut run,
        trade_date,
        deps,
        step,
        &mut outcome,
        &mut state,
    )
    .await
    {
        Ok(()) => Ok(SingleStep {
            outcome,
            hard_failure: None,
        }),
        Err(ChainStop::Hard(HardFailure(message))) => Ok(SingleStep {
            outcome,
            hard_failure: Some(message),
        }),
        Err(ChainStop::Raised(e)) => Err(e),
    }
}
