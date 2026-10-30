//! Lane **sleeves** — the four post-publish steps (`refresh_basket` ported by the RS5
//! integrator, `sleeves/basket.rs`). The signatures are the orchestrator's contract. The orchestrator's wrappers make steps 12-14 unable to raise
//! (an error becomes `skipped` with `skipped_reason = "{type}: {message}"`); `refresh_basket`
//! records its own failure and returns, as Python's does.
//!
//! | Python | here |
//! |---|---|
//! | `tasks/basket.run_refresh_basket` → names (step 11; 0 names → `skipped`) | [`refresh_basket`] |
//! | `tasks/swing.run_detect_swing` (step 12) | [`detect_swing`] |
//! | `tasks/vbt.run_detect_vbt` (step 13) | [`detect_vbt`] |
//! | `tasks/twt.run_detect_twt` (step 14; never reads `twt_execution_enabled`) | [`detect_twt`] |
//!
//! Law 2 binds hardest here: detection writes signal rows and places, arms or sizes nothing.

mod basket;
mod common;
mod precision;
mod qulla;
mod swing;
mod swing_market;
mod twt;
mod vbt;

use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::steps::StepOutcome;

/// Step 11: the basket cache; returns the number of names (0 → the orchestrator marks the step
/// `skipped`).
///
/// # Errors
/// Python's, as the task raises them (it records its own failures and returns).
pub async fn refresh_basket(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    basket::run_refresh_basket(conn, step, on).await
}

/// Step 12's detector: `run_detect_swing(session, step, trade_date, user_id=, index_slug=,
/// execution_enabled=, pipeline_run_id=)`.
///
/// # Errors
/// Python's; the orchestrator turns any of them into `skipped`.
pub async fn detect_swing(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
    user_id: i64,
    index_slug: &str,
    _execution_enabled: bool,
    pipeline_run_id: i64,
) -> Result<(), WorkerError> {
    // `execution_enabled` is read by nothing since A10 (SW10.5): the ladder reads real closes
    // whatever the flag says. It stays in the signature because the orchestrator passes it.
    swing::run_detect_swing(conn, step, on, user_id, index_slug, pipeline_run_id)
        .await
        .map(|_| ())
}

/// Step 13's detector: `run_detect_vbt(session, step, trade_date, user_id=, pipeline_run_id=)`.
///
/// # Errors
/// Python's; the orchestrator turns any of them into `skipped`.
pub async fn detect_vbt(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
    user_id: i64,
    pipeline_run_id: i64,
) -> Result<(), WorkerError> {
    vbt::run_detect_vbt(conn, step, on, user_id, pipeline_run_id)
        .await
        .map(|_| ())
}

/// Step 14's detector: `run_detect_twt(session, step, trade_date, user_id=, pipeline_run_id=)`.
///
/// # Errors
/// Python's; the orchestrator turns any of them into `skipped`.
pub async fn detect_twt(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
    user_id: i64,
    pipeline_run_id: i64,
) -> Result<(), WorkerError> {
    twt::run_detect_twt(conn, step, on, user_id, pipeline_run_id)
        .await
        .map(|_| ())
}
