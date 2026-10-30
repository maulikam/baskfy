//! Pipeline step identity and bookkeeping. Ports `baskfy_worker.steps`.
//!
//! > "Each of the ten tasks as an idempotent unit that upserts (never blind-inserts) and writes a
//! > pipeline_run_step row with rows_in/rows_out/duration/error."
//!
//! docs/03: "Every step writes a row in `pipeline_run_step` with status, duration, row counts and
//! an error payload." What is recorded here is what an operator has at 3am on `/admin/pipeline`.
//!
//! # The step bodies are the lanes'
//!
//! Each step's body lives in one lane-owned file — [`ingest`], [`index`], [`compute`], [`gate`],
//! [`sleeves`] — declared here so a lane never edits this file, `lib.rs` or the orchestrator.
//! Until a lane lands, its functions return [`WorkerError::NotPorted`].
//!
//! # Persistence, exactly as Python's
//!
//! [`record_step`] upserts the `(run_id, step)` row **twice** — `running` with zero counts and a
//! JSON `null` error before the body, then the outcome after it — so the identity sequence
//! advances by two per step in both languages. On success the error column is the outcome's
//! detail, or JSON `null` when there is none (SQLAlchemy binds `None` into a `JSONB` column as
//! `'null'`, not SQL `NULL`; checked against `insert(...).compile()`); on failure it is
//! `{"type", "message", "traceback", **detail}`. A failure row that cannot be written (the
//! transaction is already aborted) is logged, and the step's **own** error is returned — the
//! failure row must never replace the failure (17 Sep 2026). There is no commit here: the chain
//! is one transaction (orchestrator.rs).
//!
//! **One deliberate difference.** Python's `traceback` is `traceback.format_exc(limit=20)`;
//! Rust writes the error's `Debug` chain under the same key (DECISIONS-RS R7.4). The key only
//! persists for a failure the chain survives, which today is none: a raising step rolls the
//! chain's transaction back, row and all.
//!
//! Not ported: the OpenTelemetry span and the Prometheus histogram (`observe_step`); the step
//! row is the record graded here.

pub mod compute;
pub mod gate;
pub mod index;
pub mod ingest;
pub mod sleeves;

use std::time::Instant;

use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::pyjson;

/// The chain's steps, in docs/03's order (`PipelineStep`). The strings are the persisted
/// `pipeline_run_step.step` values and never change.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum PipelineStep {
    /// Kite instruments dump + NSE listings (folded in).
    RefreshInstruments,
    /// Raw daily bars (bhavcopy first, Kite top-up).
    FetchDailyBars,
    /// Corporate actions over the lookback.
    FetchCorporateActions,
    /// The adjusted series for the instruments whose actions changed.
    ApplyAdjustments,
    /// Index membership for the day.
    RefreshIndexMembership,
    /// Index levels, with equity fundamentals folded in (T9.1).
    RefreshIndexSnapshots,
    /// Factor rows, with the desk score folded in (C2).
    ComputeFactors,
    /// Market health.
    ComputeMarketHealth,
    /// The hard gate.
    DataQualityGate,
    /// Bump `data_version`, purge and warm the screen cache.
    Publish,
    /// M30: the basket cache (after publish; cannot fail the run).
    RefreshBasket,
    /// SW3: the swing detectors (cannot fail the run).
    ComputeSwing,
    /// VB4: the volume-breakout detector (cannot fail the run).
    ComputeVbt,
    /// TW4: the three-weeks-tight detector, after VBT (cannot fail the run).
    ComputeTwt,
}

impl PipelineStep {
    /// The persisted string (`StrEnum` value).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::RefreshInstruments => "refresh_instruments",
            Self::FetchDailyBars => "fetch_daily_bars",
            Self::FetchCorporateActions => "fetch_corporate_actions",
            Self::ApplyAdjustments => "apply_adjustments",
            Self::RefreshIndexMembership => "refresh_index_membership",
            Self::RefreshIndexSnapshots => "refresh_index_snapshots",
            Self::ComputeFactors => "compute_factors",
            Self::ComputeMarketHealth => "compute_market_health",
            Self::DataQualityGate => "data_quality_gate",
            Self::Publish => "publish",
            Self::RefreshBasket => "refresh_basket",
            Self::ComputeSwing => "compute_swing",
            Self::ComputeVbt => "compute_vbt",
            Self::ComputeTwt => "compute_twt",
        }
    }

    /// The step a persisted string names (`PipelineStep(value)`).
    #[must_use]
    pub fn from_value(value: &str) -> Option<Self> {
        NIGHTLY_CHAIN.into_iter().find(|s| s.value() == value)
    }

    /// Does this step run after `publish` and leave the run's success alone?
    #[must_use]
    pub fn is_post_publish(self) -> bool {
        POST_PUBLISH_STEPS.contains(&self)
    }
}

impl std::fmt::Display for PipelineStep {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.value())
    }
}

/// `NIGHTLY_CHAIN = tuple(PipelineStep)`: the chain, in order. The orchestrator walks exactly this.
pub const NIGHTLY_CHAIN: [PipelineStep; 14] = [
    PipelineStep::RefreshInstruments,
    PipelineStep::FetchDailyBars,
    PipelineStep::FetchCorporateActions,
    PipelineStep::ApplyAdjustments,
    PipelineStep::RefreshIndexMembership,
    PipelineStep::RefreshIndexSnapshots,
    PipelineStep::ComputeFactors,
    PipelineStep::ComputeMarketHealth,
    PipelineStep::DataQualityGate,
    PipelineStep::Publish,
    PipelineStep::RefreshBasket,
    PipelineStep::ComputeSwing,
    PipelineStep::ComputeVbt,
    PipelineStep::ComputeTwt,
];

/// `POST_PUBLISH_STEPS`: the steps that run after `publish` and may not fail the run. A step
/// added after these must join this set or be one the run's success depends on — a decision,
/// not an edit (VB0.5, TW4.5).
pub const POST_PUBLISH_STEPS: [PipelineStep; 4] = [
    PipelineStep::RefreshBasket,
    PipelineStep::ComputeSwing,
    PipelineStep::ComputeVbt,
    PipelineStep::ComputeTwt,
];

/// `StepStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepStatus {
    /// Written before the body runs.
    Running,
    /// Done.
    Succeeded,
    /// Raised, or (the gate) refused.
    Failed,
    /// Nothing to do — a non-trading day, no instruments with new actions.
    Skipped,
}

impl StepStatus {
    /// The persisted string.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Skipped => "skipped",
        }
    }
}

/// `RunStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunStatus {
    /// Open.
    Running,
    /// Published.
    Succeeded,
    /// A hard failure or a raised chain.
    Failed,
    /// A non-trading day, or another chain held the lock.
    Aborted,
}

impl RunStatus {
    /// The persisted string.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Aborted => "aborted",
        }
    }
}

/// What a step did (`StepOutcome`): mutated by the step body, then persisted by [`record_step`].
#[derive(Debug, Clone, PartialEq)]
pub struct StepOutcome {
    /// `rows_in`.
    pub rows_in: i64,
    /// `rows_out`.
    pub rows_out: i64,
    /// `status` (`SUCCEEDED` unless the body says otherwise).
    pub status: StepStatus,
    /// Free-form detail folded into the `error` JSONB column even on success, in `dict` order.
    pub detail: Vec<(String, OrderedJson)>,
}

impl Default for StepOutcome {
    fn default() -> Self {
        Self {
            rows_in: 0,
            rows_out: 0,
            status: StepStatus::Succeeded,
            detail: Vec::new(),
        }
    }
}

impl StepOutcome {
    /// `note(**facts)`: `detail.update(facts)`, one fact at a time, in the order Python's keyword
    /// arguments are written.
    pub fn note(&mut self, key: &str, value: OrderedJson) {
        pyjson::update(&mut self.detail, key, value);
    }

    /// The detail as a JSON object.
    #[must_use]
    pub fn detail_json(&self) -> OrderedJson {
        OrderedJson::Object(self.detail.clone())
    }
}

/// `HardFailure`: a failure that must stop the chain rather than be retried. docs/03 step 9 is
/// "a hard gate: if it fails, `data_version` is **not** bumped".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HardFailure(pub String);

/// The `pipeline_run` row as the chain holds it (the ORM object Python passes around).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PipelineRun {
    /// `id`.
    pub id: i64,
    /// `trade_date`.
    pub trade_date: NaiveDate,
    /// `status`, as last read or written.
    pub status: String,
}

fn now_utc() -> chrono::DateTime<chrono::Utc> {
    use baskfy_providers::clock::Clock as _;
    baskfy_providers::clock::system_clock().now()
}

/// `open_run`: reuse the latest run for `trade_date` if it is still `running`, otherwise insert
/// a new `running` row (flushed, not committed).
///
/// # Errors
/// The database's.
pub async fn open_run(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<PipelineRun, WorkerError> {
    let existing: Option<(i64, NaiveDate, String)> = sqlx::query_as(
        "SELECT id, trade_date, status FROM pipeline_run WHERE trade_date = $1 \
         ORDER BY id DESC LIMIT 1",
    )
    .bind(trade_date)
    .fetch_optional(&mut *conn)
    .await?;
    if let Some((id, trade_date, status)) = existing
        && status == RunStatus::Running.value()
    {
        return Ok(PipelineRun {
            id,
            trade_date,
            status,
        });
    }
    insert_run(conn, trade_date).await
}

/// The ORM's `session.add(PipelineRun(...)); flush()`.
pub(crate) async fn insert_run(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<PipelineRun, WorkerError> {
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO pipeline_run (trade_date, status, started_at) VALUES ($1, $2, $3) \
         RETURNING id",
    )
    .bind(trade_date)
    .bind(RunStatus::Running.value())
    .bind(now_utc())
    .fetch_one(&mut *conn)
    .await?;
    Ok(PipelineRun {
        id,
        trade_date,
        status: RunStatus::Running.value().to_owned(),
    })
}

/// `adopt_run`: load a run another session already committed.
///
/// # Errors
/// `LookupError` (`pipeline_run {id} does not exist`) rather than silently opening a new one.
pub async fn adopt_run(conn: &mut PgConnection, run_id: i64) -> Result<PipelineRun, WorkerError> {
    let row: Option<(i64, NaiveDate, String)> =
        sqlx::query_as("SELECT id, trade_date, status FROM pipeline_run WHERE id = $1")
            .bind(run_id)
            .fetch_optional(&mut *conn)
            .await?;
    let Some((id, trade_date, status)) = row else {
        return Err(WorkerError::Lookup(format!(
            "pipeline_run {run_id} does not exist"
        )));
    };
    Ok(PipelineRun {
        id,
        trade_date,
        status,
    })
}

/// `close_run`: set the status and `finished_at` (and `data_version` when given).
///
/// # Errors
/// The database's.
pub async fn close_run(
    conn: &mut PgConnection,
    run: &mut PipelineRun,
    status: RunStatus,
    data_version: Option<i64>,
) -> Result<(), WorkerError> {
    let query = match data_version {
        Some(version) => sqlx::query(
            "UPDATE pipeline_run SET status = $1, finished_at = $2, data_version = $3 \
             WHERE id = $4",
        )
        .bind(status.value())
        .bind(now_utc())
        .bind(version)
        .bind(run.id),
        None => sqlx::query("UPDATE pipeline_run SET status = $1, finished_at = $2 WHERE id = $3")
            .bind(status.value())
            .bind(now_utc())
            .bind(run.id),
    };
    query.execute(&mut *conn).await?;
    status.value().clone_into(&mut run.status);
    Ok(())
}

/// `_upsert_step`: one `INSERT ... ON CONFLICT (run_id, step) DO UPDATE`.
async fn upsert_step(
    conn: &mut PgConnection,
    run_id: i64,
    step: PipelineStep,
    status: StepStatus,
    outcome: &StepOutcome,
    duration_ms: i64,
    error: Option<&OrderedJson>,
) -> Result<(), WorkerError> {
    let error_text = match error {
        // SQLAlchemy binds None into JSONB as the JSON literal null.
        None => "null".to_owned(),
        Some(value) => value
            .to_python_json()
            .map_err(|e| WorkerError::python("ValueError", e.to_string()))?,
    };
    let duration = i32::try_from(duration_ms).unwrap_or(i32::MAX);
    sqlx::query(
        "INSERT INTO pipeline_run_step (run_id, step, status, rows_in, rows_out, duration_ms, \
         error) VALUES ($1, $2, $3, $4, $5, $6, CAST($7 AS JSONB)) \
         ON CONFLICT (run_id, step) DO UPDATE SET status = excluded.status, \
         rows_in = excluded.rows_in, rows_out = excluded.rows_out, \
         duration_ms = excluded.duration_ms, error = excluded.error \
         RETURNING pipeline_run_step.id",
    )
    .bind(run_id)
    .bind(step.value())
    .bind(status.value())
    .bind(outcome.rows_in)
    .bind(outcome.rows_out)
    .bind(duration)
    .bind(error_text)
    .fetch_one(&mut *conn)
    .await?;
    Ok(())
}

/// `record_step`: time a step, persist exactly one `pipeline_run_step` row for it.
///
/// `body` receives the connection and the step's [`StepOutcome`]. Its error is recorded with its
/// Python class and message and then returned unchanged.
///
/// # Errors
/// The body's own error; or the database's on the `running` row or the final row of a step
/// that succeeded.
pub async fn record_step<T, F>(
    conn: &mut PgConnection,
    run_id: i64,
    step: PipelineStep,
    _trade_date: Option<NaiveDate>,
    body: F,
) -> Result<T, WorkerError>
where
    F: AsyncFnOnce(&mut PgConnection, &mut StepOutcome) -> Result<T, WorkerError>,
{
    let mut outcome = StepOutcome::default();
    let started = Instant::now();
    upsert_step(conn, run_id, step, StepStatus::Running, &outcome, 0, None).await?;
    let result = body(&mut *conn, &mut outcome).await;
    let duration_ms = i64::try_from(started.elapsed().as_millis()).unwrap_or(i64::MAX);
    match result {
        Err(exc) => {
            let mut error = vec![
                ("type".to_owned(), OrderedJson::Str(exc.python_type())),
                ("message".to_owned(), OrderedJson::Str(exc.to_string())),
                ("traceback".to_owned(), OrderedJson::Str(format!("{exc:?}"))),
            ];
            for (k, v) in &outcome.detail {
                pyjson::update(&mut error, k, v.clone());
            }
            let error = OrderedJson::Object(error);
            if let Err(write_error) = upsert_step(
                conn,
                run_id,
                step,
                StepStatus::Failed,
                &outcome,
                duration_ms,
                Some(&error),
            )
            .await
            {
                log_error(&format!(
                    "could not record the failure of step {} on run {run_id}; the step's own \
                     error follows and is the one that matters: {write_error}",
                    step.value()
                ));
            }
            Err(exc)
        }
        Ok(value) => {
            let detail = (!outcome.detail.is_empty()).then(|| outcome.detail_json());
            upsert_step(
                conn,
                run_id,
                step,
                outcome.status,
                &outcome,
                duration_ms,
                detail.as_ref(),
            )
            .await?;
            Ok(value)
        }
    }
}

/// One log line on standard error (the worker's log goes there; stdout carries the payload).
pub fn log_error(message: &str) {
    use std::io::Write as _;
    let _ = writeln!(std::io::stderr(), "ERROR baskfy_worker: {message}");
}

/// One warning line on standard error.
pub fn log_warning(message: &str) {
    use std::io::Write as _;
    let _ = writeln!(std::io::stderr(), "WARNING baskfy_worker: {message}");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_chain_is_pythons_in_order() {
        let values: Vec<&str> = NIGHTLY_CHAIN.iter().map(|s| s.value()).collect();
        assert_eq!(
            values,
            [
                "refresh_instruments",
                "fetch_daily_bars",
                "fetch_corporate_actions",
                "apply_adjustments",
                "refresh_index_membership",
                "refresh_index_snapshots",
                "compute_factors",
                "compute_market_health",
                "data_quality_gate",
                "publish",
                "refresh_basket",
                "compute_swing",
                "compute_vbt",
                "compute_twt",
            ]
        );
        for s in NIGHTLY_CHAIN {
            assert_eq!(PipelineStep::from_value(s.value()), Some(s));
        }
        assert_eq!(PipelineStep::from_value("nope"), None);
    }

    #[test]
    fn post_publish_steps_are_the_four_after_publish() {
        let after: Vec<PipelineStep> = NIGHTLY_CHAIN
            .iter()
            .skip_while(|s| **s != PipelineStep::Publish)
            .skip(1)
            .copied()
            .collect();
        assert_eq!(after, POST_PUBLISH_STEPS.to_vec());
        assert!(!PipelineStep::Publish.is_post_publish());
    }

    #[test]
    fn note_is_dict_update() {
        let mut o = StepOutcome::default();
        o.note("b", pyjson::int(1));
        o.note("a", pyjson::int(2));
        o.note("b", pyjson::int(3));
        assert_eq!(
            pyjson::dumps(&o.detail_json()).unwrap_or_default(),
            r#"{"b": 3, "a": 2}"#
        );
    }
}
