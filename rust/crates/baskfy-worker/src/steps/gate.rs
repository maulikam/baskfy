//! Lane **gate** — step 9 (the hard gate) and step 10 (publish); the lane also owns
//! `catch_up.rs`. The signatures here are the orchestrator's contract.
//!
//! | Python | here |
//! |---|---|
//! | `tasks/quality.run_data_quality_gate` → `GateReport` (step 9) | [`data_quality_gate`], [`GateReport`] (`gate/quality.rs`) |
//! | `tasks/publish.run_publish(session, step, run, deps.cache)` → `PublishResult` (step 10) | [`publish`], [`PublishResult`] (`gate/publish.rs`) |
//! | `baskfy_api.screener`'s purge and warm-up (legacy SQL path) | `gate/publish.rs` |
//! | `statistics.median` / `fmean` / `pstdev`, `f"{x:.Nf}"` | `gate/pystats.rs` |
//!
//! The gate is a **hard failure**: it never raises on a tripped assertion; it reports, the
//! orchestrator marks the step `failed`, writes the row, and stops the chain before `publish`,
//! so `data_version` is not bumped. `publish` owns the run row's publish: `data_version =
//! current + 1`, `status = succeeded`, `finished_at = now`, then the screen-cache purge and
//! warm-up (Redis, `deps.cache`) — exactly once per run that reaches it.

#[cfg(test)]
mod db_tests;
mod publish;
mod pystats;
mod quality;
#[cfg(test)]
mod tests;

/// The screener half of `publish`, for wave 6's lanes (added, never forked): the screener
/// lane's out-of-band warm-up, `execute_screen` and `record_run` (`tasks/screener.rs`), and the
/// backtest loader's screen run with its own row limit (lane backtest_engine).
pub(crate) use publish::{
    WARM_CACHE_SCREEN_LIMIT, WarmCacheResult, WarmFailure, current_data_version,
    execute_screen_raising, execute_screen_with_limit, record_screen_run,
    warm_screen_cache_with_limit,
};

use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::settings::WorkerSettings;
use crate::steps::{PipelineRun, StepOutcome};

/// One assertion's verdict (`CheckResult`), as far as the orchestrator and the alerts read it.
/// What it observed is in the step row's detail (`checks[i].observed`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateResult {
    /// The docs/09 assertion number.
    pub assertion: i64,
    /// Its name (`bar_count_against_baseline`, ...).
    pub name: String,
    /// `passed`, `failed`, `skipped`, ...
    pub status: String,
    /// The human sentence.
    pub message: String,
}

/// `GateReport`: every assertion's result, in order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateReport {
    /// The session judged.
    pub trade_date: NaiveDate,
    /// Every result, in the order the gate ran them.
    pub results: Vec<GateResult>,
}

impl GateReport {
    /// The failing results (`report.failures`).
    #[must_use]
    pub fn failures(&self) -> Vec<&GateResult> {
        self.results
            .iter()
            .filter(|r| r.status == "failed")
            .collect()
    }

    /// The skipped results (`report.skipped`): honest reporting, never a failure.
    #[must_use]
    pub fn skipped(&self) -> Vec<&GateResult> {
        self.results
            .iter()
            .filter(|r| r.status == "skipped")
            .collect()
    }

    /// `report.passed`: no failure (a skip does not block).
    #[must_use]
    pub fn passed(&self) -> bool {
        self.failures().is_empty()
    }
}

/// `PublishResult`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PublishResult {
    /// The `data_version` this run published.
    pub data_version: i64,
    /// `screen:*` keys purged.
    pub purged: i64,
    /// Cache entries warmed.
    pub warmed: i64,
}

/// Step 9: `run_data_quality_gate(session, step, trade_date)` with `get_worker_settings()`'s
/// thresholds. The orchestrator marks the step `failed` when the report did not pass, and raises
/// the [`HardFailure`](crate::steps::HardFailure) after the row is written.
///
/// # Errors
/// The database's (and an unparseable `WorkerSettings`, as `get_worker_settings()` raises).
pub async fn data_quality_gate(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
) -> Result<GateReport, WorkerError> {
    let settings = WorkerSettings::from_env()?;
    let thresholds = quality::Thresholds::from_settings(&settings);
    let results = quality::run_data_quality_gate(conn, step, on, thresholds).await?;
    Ok(report_of(on, &results))
}

fn report_of(on: NaiveDate, results: &[quality::CheckResult]) -> GateReport {
    GateReport {
        trade_date: on,
        results: results
            .iter()
            .map(|r| GateResult {
                assertion: r.assertion,
                name: r.name.to_owned(),
                status: r.status.value().to_owned(),
                message: r.message.clone(),
            })
            .collect(),
    }
}

/// Step 10: `run_publish(session, step, run, deps.cache)`.
///
/// # Errors
/// The database's; a cache failure is noted on the step, never raised. A saved screen with
/// `ranking_terms` raises `NotPorted` (DECISIONS-RS R-gate.2).
pub async fn publish(
    conn: &mut PgConnection,
    deps: &PipelineDependencies,
    step: &mut StepOutcome,
    run: &mut PipelineRun,
) -> Result<PublishResult, WorkerError> {
    let done = publish::run_publish(conn, step, run, deps.cache.as_ref()).await?;
    Ok(PublishResult {
        data_version: done.data_version,
        purged: done.purged,
        warmed: done.warmed,
    })
}
