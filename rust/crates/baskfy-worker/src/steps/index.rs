//! Lane **index** — steps 5 and 6 (with equity fundamentals folded into step 6, T9.1).
//!
//! | Python | here |
//! |---|---|
//! | `tasks/membership.run_refresh_index_membership` (step 5) | [`refresh_index_membership`] (`index/membership.rs`) |
//! | `tasks/snapshots.run_refresh_index_snapshots` + `tasks/fundamentals.run_fetch_fundamentals` over `fundamentals_scope` (step 6's whole block, the `ProviderError` from fundamentals noted, not raised) | [`refresh_index_snapshots`] (`index/snapshots.rs`, `index/fundamentals.rs`) |
//! | `baskfy_core.universes` (`UNIVERSES`' ids and slugs, `slugify_index`, `SME_SERIES`) | `index/universes.rs` (private) |
//!
//! Point-in-time throughout (house rule 5): membership rows are written for `on` only and an
//! observed (`nse_file`) row is never downgraded; snapshots and fundamentals are written for `on`
//! only, the P/E re-priced onto `on`'s exchange print. Idempotent (house rule 7): every write is
//! an upsert keyed by `(…, date)`. Nothing here reaches an order, a GTT or a Kite write (law 2):
//! the provider calls are the composite's read ports.

pub(crate) mod fundamentals;
pub(crate) mod membership;
pub(crate) mod snapshots;
pub(crate) mod universes;

#[cfg(test)]
mod db_tests;

use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

/// Step 5: `run_refresh_index_membership(session, provider, step, trade_date)`.
///
/// # Errors
/// Python's, as the step raises them: a database error, or a provider error outside
/// `ProviderError`'s tree. A `ProviderError` for one universe is recorded under `failures` and
/// the other universes still land.
pub async fn refresh_index_membership(
    conn: &mut PgConnection,
    deps: &PipelineDependencies,
    step: &mut StepOutcome,
    on: NaiveDate,
) -> Result<(), WorkerError> {
    membership::run_refresh_index_membership(conn, deps.provider.as_ref(), step, on).await?;
    Ok(())
}

/// Step 6's block: the snapshots, then the fundamentals on their own outcome, folded in as
/// `step.note(fundamentals=..., fundamentals_rows=...)`.
///
/// # Errors
/// Python's, as the step raises them: a provider error from `index_snapshots` (noted as `error`
/// first), a database error, or `ValueError` once `index_def` outgrows smallint. A
/// `ProviderError` from the fundamentals fetch is noted, never raised: NULL is publishable.
pub async fn refresh_index_snapshots(
    conn: &mut PgConnection,
    deps: &PipelineDependencies,
    step: &mut StepOutcome,
    on: NaiveDate,
) -> Result<(), WorkerError> {
    run_refresh_index_snapshots(conn, deps, step, on).await?;

    // T9.1: scoped by the day's traded names, not by every listing.
    let mut fundamentals = StepOutcome::default();
    let scope = fundamentals::fundamentals_scope(conn, on).await?;
    match fundamentals::run_fetch_fundamentals(
        conn,
        deps.provider.as_ref(),
        &mut fundamentals,
        on,
        &scope,
    )
    .await
    {
        Ok(_) => {}
        // `except ProviderError as exc: fundamentals.note(error=str(exc))`.
        Err(WorkerError::Provider(exc)) if exc.is_provider_error() => {
            fundamentals.note("error", pyjson::s(exc.to_string()));
        }
        Err(other) => return Err(other),
    }
    step.note("fundamentals", fundamentals.detail_json());
    step.note("fundamentals_rows", pyjson::int(fundamentals.rows_out));
    Ok(())
}

/// `run_refresh_index_snapshots(session, provider, outcome, on)`.
pub(crate) async fn run_refresh_index_snapshots(
    conn: &mut PgConnection,
    deps: &PipelineDependencies,
    outcome: &mut StepOutcome,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let snapshots = match deps.provider.index_snapshots(on).await {
        Ok(snapshots) => snapshots,
        Err(exc) => {
            if exc.is_provider_error() {
                outcome.note("error", pyjson::s(exc.to_string()));
            }
            return Err(WorkerError::Provider(exc));
        }
    };
    let result = snapshots::store_snapshots(conn, on, &snapshots).await?;
    outcome.rows_in = i64::try_from(snapshots.len()).unwrap_or(i64::MAX);
    outcome.rows_out = result.rows_written;
    snapshots::note_result(outcome, on, &result);
    Ok(result.rows_written)
}
