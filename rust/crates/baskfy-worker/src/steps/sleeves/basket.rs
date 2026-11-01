//! Step 11, `refresh_basket` — `baskfy_worker.tasks.basket.run_refresh_basket`: the build is
//! `baskfy_api::basket_build::build_current_basket` (shared with `GET /baskets`), upserted into
//! `basket_snapshot` on `(as_of, data_version)`. A build failure is recorded as
//! `skipped_reason = "{type}: {message}"` and the chain carries on.

use std::time::Instant;

use baskfy_api::basket_build::{Built, SCAN_DATA_VERSION, build_current_basket, screen_run_id};
use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

fn now_utc() -> chrono::DateTime<chrono::Utc> {
    use baskfy_providers::clock::Clock as _;
    baskfy_providers::clock::system_clock().now()
}

/// `run_refresh_basket(session, outcome, as_of)`: the number of names stored (0 when nothing
/// could be built, which the orchestrator marks `skipped`).
///
/// # Errors
/// The upsert's database error (the build's own failures are recorded, not raised).
pub(crate) async fn run_refresh_basket(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    as_of: NaiveDate,
) -> Result<i64, WorkerError> {
    let started = Instant::now();
    let built = match build_current_basket(conn, Some(as_of))
        .await
        .map_err(WorkerError::from)
    {
        Ok(b) => b,
        Err(exc) => {
            outcome.note(
                "skipped_reason",
                pyjson::s(format!("{}: {exc}", exc.python_type())),
            );
            outcome.note(
                "hint",
                pyjson::s(
                    "the basket needs bars and one uploaded scan; neither is this step's to create",
                ),
            );
            return Ok(0);
        }
    };
    let Some(Built {
        payload,
        names,
        suspects,
    }) = built
    else {
        outcome.note("skipped_reason", pyjson::s("no basket could be built"));
        outcome.note(
            "hint",
            pyjson::s(
                "needs bars in the pipeline and one uploaded scan for marketcap, beta and F&O",
            ),
        );
        return Ok(0);
    };
    let elapsed_ms = i64::try_from(started.elapsed().as_millis()).unwrap_or(i64::MAX);
    let text = pyjson::dumps(&payload).map_err(|e| WorkerError::python("ValueError", e))?;
    sqlx::query(
        "INSERT INTO basket_snapshot (as_of, screen_run_id, data_version, payload, computed_ms, \
         computed_at) VALUES ($1, $2, $3, $4::jsonb, $5, $6) \
         ON CONFLICT (as_of, data_version) DO UPDATE SET screen_run_id = excluded.screen_run_id, \
         payload = excluded.payload, computed_ms = excluded.computed_ms, \
         computed_at = excluded.computed_at",
    )
    .bind(as_of)
    .bind(screen_run_id(as_of))
    .bind(SCAN_DATA_VERSION)
    .bind(text)
    .bind(elapsed_ms)
    .bind(now_utc())
    .execute(&mut *conn)
    .await?;
    outcome.rows_out = names;
    outcome.note("as_of", pyjson::s(as_of.format("%Y-%m-%d").to_string()));
    outcome.note("data_version", pyjson::int(SCAN_DATA_VERSION));
    outcome.note("names", pyjson::int(names));
    outcome.note(
        "suspect_symbols",
        pyjson::int(i64::try_from(suspects).unwrap_or(i64::MAX)),
    );
    outcome.note("computed_ms", pyjson::int(elapsed_ms));
    Ok(names)
}
