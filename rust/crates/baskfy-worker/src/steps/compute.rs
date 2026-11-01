//! Lane **compute** — steps 7 and 8, and the desk score folded into step 7 (C2). The lane owns
//! this file and `steps/compute/`; the signatures are the orchestrator's contract.
//!
//! | Python | here |
//! |---|---|
//! | `tasks/factors.run_compute_factors(session, step, trade_date, deps.factor_engine)` (step 7) | [`compute_factors`] → `compute/factors.rs` |
//! | `engine.load_history`, `load_index_levels`, `load_fundamentals`, `PolarsFactorEngine`, `_apply_true_ath` | `compute/factors.rs` |
//! | `tasks/ranking.run_rank_columns` + `factors_ranking.cross_sectional_pctile`, `rank_persistence` | `compute/ranking.rs` |
//! | `tasks/desk_score.run_compute_desk_score` (inside the orchestrator's savepoint, which cannot raise) | [`compute_desk_score`] → `compute/desk.rs` |
//! | `desk_score_service.score_day`, `momentum_scan.build` (the scan frame), `score.score` | `compute/desk.rs`, `compute/score.rs` |
//! | `tasks/market_health.run_compute_market_health` + `breadth.breadth_query` (step 8) | [`compute_market_health`] → `compute/market_health.rs` |
//!
//! The factor arithmetic is `baskfy_core::factors::compute_factors` (wave 2, goldens); what is
//! here is the I/O around it and the Python modules it needs that had no Rust home. Every
//! number is written as Python writes it (house rule 8): a float bound to a `numeric` column is
//! the exact double rounded by PostgreSQL to the column's scale (`compute/numeric.rs`).
//! Re-running a day writes identical rows (house rule 7); nothing reads past the as-of date
//! (house rule 5). Law 2: nothing here reaches a broker — the steps read the database and write
//! analytics tables only.

pub(crate) mod desk;
pub(crate) mod factors;
pub(crate) mod market_health;
mod numeric;
pub(crate) mod ranking;

use baskfy_core::error::CoreError;
use chrono::NaiveDate;
use polars::prelude::PolarsError;
use sqlx::PgConnection;

use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::steps::StepOutcome;

/// A core error as the Python exception it ports (`ValueError`, the core's class).
pub(crate) fn core_err(e: CoreError) -> WorkerError {
    match e {
        CoreError::Polars(p) => pl_err(&p),
        CoreError::Arithmetic(m) => WorkerError::python("InvalidOperation", m),
        CoreError::InvalidInput(m) | CoreError::MissingColumn(m) => {
            WorkerError::python("ValueError", m)
        }
    }
}

/// A polars error (Python's polars raises its own classes; the message is polars').
pub(crate) fn pl_err(e: &PolarsError) -> WorkerError {
    WorkerError::python("ComputeError", e.to_string())
}

/// Step 7: the factor rows (`factor_engine` is `None` in production: the polars engine), then
/// `mom_pctile`/`rank_persist_20` and the mask assertion. One `factor_daily` row per instrument
/// with a bar on `on` — the invariant the quality gate's assertion 4 checks.
///
/// # Errors
/// Python's, as the step raises them.
pub async fn compute_factors(
    conn: &mut PgConnection,
    _deps: &PipelineDependencies,
    step: &mut StepOutcome,
    on: NaiveDate,
) -> Result<(), WorkerError> {
    factors::run(conn, step, on).await.map(|_| ())
}

/// C2's `desk_score_daily` write, run by the orchestrator inside a savepoint after the factor
/// rows exist. An error here is caught there (status `failed`, `rows_out = 0`,
/// `error = "{type}: {message}"`), never raised.
///
/// # Errors
/// Python's, as the task raises them.
pub async fn compute_desk_score(
    conn: &mut PgConnection,
    desk: &mut StepOutcome,
    on: NaiveDate,
) -> Result<(), WorkerError> {
    desk::run(conn, desk, on).await.map(|_| ())
}

/// Step 8: `run_compute_market_health(session, step, trade_date)` — one breadth row per
/// market-health universe.
///
/// # Errors
/// Python's, as the step raises them.
pub async fn compute_market_health(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
) -> Result<(), WorkerError> {
    market_health::run(conn, step, on).await.map(|_| ())
}
