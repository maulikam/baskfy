//! `backfill`: the resumable Kite bar backfill over `ingest_cursor`, a transaction per unit.

use std::sync::Arc;

use baskfy_db::WriteDb;
use baskfy_providers::composite::CompositeProvider;
use chrono::{Duration, NaiveDate};
use polars::prelude::DataFrame;
use sqlx::PgConnection;
use tokio::task::JoinSet;

use crate::error::WorkerError;
use crate::steps::ingest::bars::{kite_rows, upsert_kite_rows};
use crate::window::DateWindow;

/// docs/09: at most 2,000-day slices per instrument.
pub const DEFAULT_CHUNK_DAYS: i64 = 2000;
const KIND_BARS: &str = "bars";
const PENDING: &str = "pending";
const RUNNING: &str = "running";
const DONE: &str = "done";
const FAILED: &str = "failed";

#[derive(Debug, Clone)]
struct Unit {
    instrument_id: i64,
    symbol: String,
    kite_token: Option<i64>,
    window: DateWindow,
}

/// `BackfillReport`.
#[derive(Debug, Default)]
pub struct Report {
    /// Cursor rows offered by the plan.
    pub planned: usize,
    /// Units already `done` before this run.
    pub already_done: i64,
    /// Units completed by this run.
    pub completed: usize,
    /// Units that failed in this run.
    pub failed: usize,
    /// Bar rows offered to the upsert.
    pub rows_written: i64,
    /// `"{symbol} {window}: {error}"` per failed unit.
    pub errors: Vec<String>,
}

fn chunk_windows(window: DateWindow, chunk_days: i64) -> Result<Vec<DateWindow>, WorkerError> {
    if chunk_days < 1 {
        return Err(WorkerError::python(
            "ValueError",
            format!("chunk_days must be at least 1; got {chunk_days}"),
        ));
    }
    let span = Duration::days(chunk_days - 1);
    let mut out = Vec::new();
    let mut cursor = window.start();
    while cursor <= window.end() {
        let end = (cursor + span).min(window.end());
        out.push(DateWindow::new(cursor, end)?);
        cursor = end + Duration::days(1);
    }
    Ok(out)
}

async fn plan(
    conn: &mut PgConnection,
    window: DateWindow,
    symbols: Option<&[String]>,
    chunk_days: i64,
) -> Result<usize, WorkerError> {
    let ids: Vec<(i64,)> = match symbols {
        Some(list) if !list.is_empty() => {
            sqlx::query_as(
                "SELECT id FROM instrument WHERE delisted_on IS NULL AND symbol = ANY($1) \
                 ORDER BY symbol",
            )
            .bind(list)
            .fetch_all(&mut *conn)
            .await?
        }
        _ => {
            sqlx::query_as("SELECT id FROM instrument WHERE delisted_on IS NULL ORDER BY symbol")
                .fetch_all(&mut *conn)
                .await?
        }
    };
    let chunks = chunk_windows(window, chunk_days)?;
    let mut instrument = Vec::new();
    let mut starts = Vec::new();
    let mut ends = Vec::new();
    for (id,) in ids {
        for chunk in &chunks {
            instrument.push(id);
            starts.push(chunk.start());
            ends.push(chunk.end());
        }
    }
    if instrument.is_empty() {
        return Ok(0);
    }
    for at in (0..instrument.len()).step_by(10_000) {
        let to = (at + 10_000).min(instrument.len());
        sqlx::query(
            "INSERT INTO ingest_cursor (kind, instrument_id, window_start, window_end, status, \
             attempts) SELECT $1, u.i, u.s, u.e, $2, 0 \
             FROM UNNEST($3::int8[], $4::date[], $5::date[]) AS u(i, s, e) \
             ON CONFLICT (kind, instrument_id, window_start) DO NOTHING",
        )
        .bind(KIND_BARS)
        .bind(PENDING)
        .bind(&instrument[at..to])
        .bind(&starts[at..to])
        .bind(&ends[at..to])
        .execute(&mut *conn)
        .await?;
    }
    Ok(instrument.len())
}

async fn pending_units(conn: &mut PgConnection, resume: bool) -> Result<Vec<Unit>, WorkerError> {
    let statuses: Vec<&str> = if resume {
        vec![PENDING, RUNNING, FAILED]
    } else {
        vec![PENDING, RUNNING, FAILED, DONE]
    };
    let rows: Vec<(i64, String, Option<i64>, NaiveDate, NaiveDate)> = sqlx::query_as(
        "SELECT c.instrument_id, i.symbol, i.kite_token, c.window_start, c.window_end \
         FROM ingest_cursor c JOIN instrument i ON i.id = c.instrument_id \
         WHERE c.kind = $1 AND c.status = ANY($2) ORDER BY i.symbol, c.window_start",
    )
    .bind(KIND_BARS)
    .bind(&statuses)
    .fetch_all(&mut *conn)
    .await?;
    rows.into_iter()
        .map(|(instrument_id, symbol, kite_token, s, e)| {
            Ok(Unit {
                instrument_id,
                symbol,
                kite_token,
                window: DateWindow::new(s, e)?,
            })
        })
        .collect()
}

async fn count_done(conn: &mut PgConnection) -> Result<i64, WorkerError> {
    Ok(
        sqlx::query_scalar("SELECT count(*) FROM ingest_cursor WHERE kind = $1 AND status = $2")
            .bind(KIND_BARS)
            .bind(DONE)
            .fetch_one(&mut *conn)
            .await?,
    )
}

async fn mark_on(
    conn: &mut PgConnection,
    unit: &Unit,
    status: &str,
    rows_written: Option<i64>,
    error: Option<&str>,
    bump_attempt: bool,
) -> Result<(), WorkerError> {
    sqlx::query(
        "UPDATE ingest_cursor SET status = $4, \
         rows_written = CASE WHEN $5 THEN $6 ELSE rows_written END, \
         last_error = CASE WHEN $7 THEN jsonb_build_object('message', $8::text) ELSE last_error END, \
         attempts = CASE WHEN $9 THEN attempts + 1 ELSE attempts END \
         WHERE kind = $1 AND instrument_id = $2 AND window_start = $3",
    )
    .bind(KIND_BARS)
    .bind(unit.instrument_id)
    .bind(unit.window.start())
    .bind(status)
    .bind(rows_written.is_some())
    .bind(rows_written)
    .bind(error.is_some())
    .bind(error)
    .bind(bump_attempt)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

async fn mark(
    db: &WriteDb,
    unit: &Unit,
    status: &str,
    rows_written: Option<i64>,
    error: Option<&str>,
    bump_attempt: bool,
) -> Result<(), WorkerError> {
    let mut tx = db.begin().await?;
    mark_on(&mut tx, unit, status, rows_written, error, bump_attempt).await?;
    tx.commit().await?;
    Ok(())
}

/// `run_backfill`'s keyword arguments.
#[derive(Debug, Clone, Copy)]
pub struct Options<'a> {
    /// Symbols to plan, or every listed instrument.
    pub symbols: Option<&'a [String]>,
    /// Fetches in flight at once.
    pub concurrency: usize,
    /// Skip `done` units.
    pub resume: bool,
    /// Days per cursor.
    pub chunk_days: i64,
    /// Stop starting units after this many complete (a kill, for tests).
    pub stop_after: Option<usize>,
}

/// `run_backfill(provider, window, ...)`: plan, then every outstanding unit, a transaction each.
///
/// # Errors
/// The database's, and a non-provider error from a fetch.
pub async fn run_backfill(
    db: &WriteDb,
    provider: Arc<CompositeProvider>,
    window: DateWindow,
    options: Options<'_>,
) -> Result<Report, WorkerError> {
    let Options {
        symbols,
        concurrency,
        resume,
        chunk_days,
        stop_after,
    } = options;
    let mut report = Report::default();
    let mut tx = db.begin().await?;
    report.planned = plan(&mut tx, window, symbols, chunk_days).await?;
    tx.commit().await?;
    let mut tx = db.begin().await?;
    report.already_done = count_done(&mut tx).await?;
    let units = pending_units(&mut tx, resume).await?;
    tx.rollback().await?;

    let width = concurrency.max(1);
    let stopped = |r: &Report| stop_after.is_some_and(|n| r.completed >= n);
    for batch in units.chunks(width) {
        if stopped(&report) {
            break;
        }
        let mut flights: JoinSet<(usize, Result<DataFrame, baskfy_providers::ProviderError>)> =
            JoinSet::new();
        for (slot, unit) in batch.iter().enumerate() {
            let Some(token) = unit.kite_token else {
                mark(db, unit, DONE, Some(0), Some("no provider token"), false).await?;
                report.completed += 1;
                continue;
            };
            mark(db, unit, RUNNING, None, None, true).await?;
            let provider = Arc::clone(&provider);
            let (start, end) = (unit.window.start(), unit.window.end());
            flights.spawn(async move { (slot, provider.daily_bars(token, start, end).await) });
        }
        let mut results = Vec::new();
        while let Some(joined) = flights.join_next().await {
            results.push(joined.map_err(|e| WorkerError::python("RuntimeError", e.to_string()))?);
        }
        results.sort_by_key(|(slot, _)| *slot);
        for (slot, fetched) in results {
            let unit = &batch[slot];
            match fetched {
                Ok(frame) => {
                    let mut tx = db.begin().await?;
                    let rows = kite_rows(unit.instrument_id, &frame)?;
                    let written = upsert_kite_rows(&mut tx, &rows).await?;
                    mark_on(&mut tx, unit, DONE, Some(written), None, false).await?;
                    tx.commit().await?;
                    report.rows_written += written;
                    report.completed += 1;
                }
                Err(e) if e.is_provider_error() => {
                    let message = e.to_string();
                    mark(db, unit, FAILED, None, Some(&message), false).await?;
                    report.failed += 1;
                    report
                        .errors
                        .push(format!("{} {}: {message}", unit.symbol, unit.window));
                }
                Err(e) => return Err(e.into()),
            }
        }
    }
    Ok(report)
}
