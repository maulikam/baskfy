//! `backfill`: the resumable Kite bar backfill over `ingest_cursor`, a transaction per unit.

use std::sync::Arc;

use baskfy_db::WriteDb;
use baskfy_providers::composite::CompositeProvider;
use chrono::{Duration, NaiveDate};
use polars::prelude::DataFrame;
use rust_decimal::Decimal;
use sqlx::PgConnection;
use tokio::task::JoinSet;

use super::common::{date_at, decimal_at, int_at, text_at};
use crate::error::WorkerError;
use crate::window::DateWindow;

pub(super) const DEFAULT_CHUNK_DAYS: i64 = 2000;
const KIND_BARS: &str = "bars";
const PENDING: &str = "pending";
const RUNNING: &str = "running";
const DONE: &str = "done";
const FAILED: &str = "failed";
const UPSERT_CHUNK: usize = 2000;

#[derive(Debug, Clone)]
struct Unit {
    instrument_id: i64,
    symbol: String,
    kite_token: Option<i64>,
    window: DateWindow,
}

#[derive(Debug, Default)]
pub(super) struct Report {
    pub(super) planned: usize,
    pub(super) already_done: i64,
    pub(super) completed: usize,
    pub(super) failed: usize,
    pub(super) rows_written: i64,
    pub(super) errors: Vec<String>,
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

async fn upsert_bars(
    conn: &mut PgConnection,
    instrument_id: i64,
    frame: &DataFrame,
) -> Result<i64, WorkerError> {
    let n = frame.height();
    if n == 0 {
        return Ok(0);
    }
    let mut dates = Vec::with_capacity(n);
    let mut prices: [Vec<Option<Decimal>>; 4] = Default::default();
    let mut volume = Vec::with_capacity(n);
    let mut source = Vec::with_capacity(n);
    for i in 0..n {
        dates.push(date_at(frame, "date", i)?);
        for (k, col) in ["open", "high", "low", "close"].iter().enumerate() {
            prices[k].push(decimal_at(frame, col, i)?);
        }
        volume.push(int_at(frame, "volume", i)?);
        source.push(text_at(frame, "source", i)?);
    }
    let mut written = 0;
    for at in (0..n).step_by(UPSERT_CHUNK) {
        let to = (at + UPSERT_CHUNK).min(n);
        sqlx::query(
            "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, \
             close_raw, volume_raw, open_raw, high_raw, low_raw, adj_factor, source) \
             SELECT $1, u.date, u.open, u.high, u.low, u.close, u.volume, u.close, u.volume, \
             u.open, u.high, u.low, 1, u.source \
             FROM UNNEST($2::date[], $3::numeric[], $4::numeric[], $5::numeric[], \
             $6::numeric[], $7::int8[], $8::varchar[]) \
             AS u(date, open, high, low, close, volume, source) \
             ON CONFLICT (instrument_id, date) DO UPDATE SET \
             open = CASE WHEN (ohlcv_daily.adj_factor != 1) THEN ohlcv_daily.open ELSE excluded.open END, \
             high = CASE WHEN (ohlcv_daily.adj_factor != 1) THEN ohlcv_daily.high ELSE excluded.high END, \
             low = CASE WHEN (ohlcv_daily.adj_factor != 1) THEN ohlcv_daily.low ELSE excluded.low END, \
             close_raw = excluded.close_raw, volume_raw = excluded.volume_raw, \
             open_raw = excluded.open_raw, high_raw = excluded.high_raw, \
             low_raw = excluded.low_raw, source = excluded.source",
        )
        .bind(instrument_id)
        .bind(&dates[at..to])
        .bind(&prices[0][at..to])
        .bind(&prices[1][at..to])
        .bind(&prices[2][at..to])
        .bind(&prices[3][at..to])
        .bind(&volume[at..to])
        .bind(&source[at..to])
        .execute(&mut *conn)
        .await?;
        written += i64::try_from(to - at).unwrap_or(i64::MAX);
    }
    Ok(written)
}

pub(super) struct Options<'a> {
    pub(super) symbols: Option<&'a [String]>,
    pub(super) concurrency: usize,
    pub(super) resume: bool,
    pub(super) chunk_days: i64,
    pub(super) stop_after: Option<usize>,
}

pub(super) async fn run_backfill(
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
                    let written = upsert_bars(&mut tx, unit.instrument_id, &frame).await?;
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
