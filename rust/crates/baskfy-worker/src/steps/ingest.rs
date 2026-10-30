//! Lane **ingest** — steps 1-4 and the calendar reconcile between steps 2 and 3.
//!
//! | Python | here |
//! |---|---|
//! | `tasks/instruments.run_refresh_instruments` + `tasks/listings.run_refresh_listings` (step 1's block) | [`refresh_instruments`] |
//! | `tasks/instruments.active_instruments` | [`active_instruments`] |
//! | `tasks/bars.run_fetch_daily_bars` (step 2; `bhavcopy_backfill` as the bars need it) | [`fetch_daily_bars`] |
//! | `calendar.reconcile_calendar(..., published=bhavcopy_publication_check(provider))` | [`reconcile_calendar`] |
//! | `tasks/corporate_actions.run_fetch_corporate_actions` (step 3) | [`fetch_corporate_actions`] |
//! | `tasks/adjustments.run_apply_adjustments` (step 4; the unbounded call is deliberate, house rule 5) | [`apply_adjustments`] |
//! | `tasks/adjustments.reprocess_instrument` (with its `as_of` bound) | [`reprocess_instrument`] |
//! | `baskfy_core.adjustments` (pure; no Rust home before this lane) | `ingest/adjust.rs` (private) |
//!
//! Each step writes what Python's writes, statement for statement where a statement's *count*
//! is observable (every identity-keyed upsert consumes one sequence value per proposed row, so
//! the row counts per table are Python's), and value for value everywhere: the replay grading
//! (`tools/parity/nightly_diff.py --step`) compares every row, sequence and step row.
//!
//! **House rule 5.** The nightly passes **no** `as_of` to the adjustment rebuild: the adjusted
//! `close` is an "as of today" series on purpose (the root CLAUDE.md, rule 5's note). Do not
//! "fix" it here; a point-in-time reader calls [`reprocess_instrument`] with its own `as_of`.
//!
//! **Law 2.** Every provider call here is a read (instrument dumps, bars, bhavcopies, corporate
//! actions); nothing reaches an order, a GTT or a Kite write endpoint.

mod adjust;
mod adjustments;
mod bars;
mod calendar;
mod corporate_actions;
mod frame;
mod instruments;

use std::collections::BTreeSet;

use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::steps::StepOutcome;
use crate::window::DateWindow;

pub use adjustments::InstrumentAdjustment;

/// One row of `active_instruments`: `(instrument_id, symbol, kite_token)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActiveInstrument {
    /// `instrument.id`.
    pub instrument_id: i64,
    /// `instrument.symbol`.
    pub symbol: String,
    /// `instrument.kite_token`; `None` for a listing Kite does not carry.
    pub kite_token: Option<i64>,
}

/// What `reconcile_calendar` reports (`ReconciliationResult`); the orchestrator reads only
/// `missed_sessions`, to warn about them.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CalendarReconciliation {
    /// Weekdays with no bars that NSE did publish a file for: sessions we failed to ingest.
    pub missed_sessions: Vec<NaiveDate>,
}

/// Step 1's whole block: the instrument merge, then the listings register on its own outcome,
/// folded in as `step.note(listings=..., listings_rows=...)`.
///
/// # Errors
/// Python's, as the step raises them.
pub async fn refresh_instruments(
    conn: &mut PgConnection,
    deps: &PipelineDependencies,
    trade_date: NaiveDate,
    step: &mut StepOutcome,
) -> Result<(), WorkerError> {
    instruments::run_refresh_instruments(conn, &deps.provider, step, Some(trade_date)).await?;
    // Its own outcome: sharing `step` would let the register's row counts overwrite the
    // instrument merge's.
    let mut register = StepOutcome::default();
    instruments::run_refresh_listings(conn, &deps.provider, &mut register).await?;
    step.note("listings", register.detail_json());
    step.note("listings_rows", crate::pyjson::int(register.rows_out));
    Ok(())
}

/// `active_instruments(session)`: the instruments step 2 fetches, ordered by symbol.
///
/// # Errors
/// The database's.
pub async fn active_instruments(
    conn: &mut PgConnection,
) -> Result<Vec<ActiveInstrument>, WorkerError> {
    instruments::active_instruments(conn).await
}

/// Step 2: `run_fetch_daily_bars(session, provider, step, active, window)`.
///
/// # Errors
/// Python's, as the step raises them.
pub async fn fetch_daily_bars(
    conn: &mut PgConnection,
    deps: &PipelineDependencies,
    step: &mut StepOutcome,
    active: &[ActiveInstrument],
    window: DateWindow,
) -> Result<(), WorkerError> {
    bars::run_fetch_daily_bars(conn, &deps.provider, step, active, window).await?;
    Ok(())
}

/// `reconcile_calendar(session, start, end, published=bhavcopy_publication_check(provider))`,
/// run after step 2's row is written and before step 3 starts.
///
/// # Errors
/// The database's.
pub async fn reconcile_calendar(
    conn: &mut PgConnection,
    deps: &PipelineDependencies,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<CalendarReconciliation, WorkerError> {
    let result = calendar::reconcile_calendar(conn, start, end, true, Some(&deps.provider)).await?;
    Ok(CalendarReconciliation {
        missed_sessions: result.missed_sessions,
    })
}

/// Step 3: `run_fetch_corporate_actions(session, provider, step, since)`; returns the instrument
/// ids whose action set changed (step 4's input).
///
/// # Errors
/// Python's, as the step raises them.
pub async fn fetch_corporate_actions(
    conn: &mut PgConnection,
    deps: &PipelineDependencies,
    step: &mut StepOutcome,
    since: NaiveDate,
) -> Result<BTreeSet<i64>, WorkerError> {
    corporate_actions::run_fetch_corporate_actions(conn, &deps.provider, step, since).await
}

/// Step 4: `run_apply_adjustments(session, step, touched)`. The orchestrator has already set the
/// status to `SKIPPED` when `touched` is empty, exactly as Python does, and still calls this.
///
/// # Errors
/// Python's, as the step raises them.
pub async fn apply_adjustments(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    touched: &BTreeSet<i64>,
) -> Result<(), WorkerError> {
    adjustments::run_apply_adjustments(conn, step, touched).await?;
    Ok(())
}

/// `reprocess_instrument(session, instrument_id, as_of=...)`: rebuild one instrument's whole
/// adjusted history from its raw prints. `as_of = None` is the nightly's call (every action
/// known now — the "as of today" series); a point-in-time reader passes the date it is
/// reproducing and only actions with `ex_date <= as_of` are applied (house rule 5).
///
/// # Errors
/// The database's, or a value polars would refuse (a decimal wider than 38 digits).
pub async fn reprocess_instrument(
    conn: &mut PgConnection,
    instrument_id: i64,
    as_of: Option<NaiveDate>,
) -> Result<InstrumentAdjustment, WorkerError> {
    adjustments::reprocess_instrument(conn, instrument_id, as_of).await
}
