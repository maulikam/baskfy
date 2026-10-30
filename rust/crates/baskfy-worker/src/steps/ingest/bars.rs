//! Step 2 — `fetch_daily_bars`, with the bhavcopy leading for a completed session (M90).
//! Ports `baskfy_worker.tasks.bars` and the part of `baskfy_worker.bhavcopy_backfill` the step
//! calls (`backfill_bars_from_bhavcopy` on the chain's own transaction, `_rows_for_day`,
//! `upsert_day`, `trading_days_in`, `instrument_ids_by_key`).
//!
//! The order is Python's: for a one-day window the session's bhavcopy is read first (one
//! request for ~4,000 rows), then Kite's historical endpoint is asked only for the active
//! instruments the bhavcopy left without a bar; a Kite pass that wrote nothing falls back to the
//! bhavcopy, and a single-session Kite pass with gaps is topped up from it (M84) unless the
//! bhavcopy already led. Every write is an upsert on `(instrument_id, date)` that refreshes the
//! `*_raw` columns and keeps an adjusted row's OHL (AF 0.6), so a re-run writes identical rows.

use std::collections::{BTreeSet, HashMap};

use baskfy_db::OrderedJson;
use baskfy_providers::ErrorKind;
use baskfy_providers::composite::CompositeProvider;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::ActiveInstrument;
use super::frame;
use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;
use crate::window::DateWindow;

/// `bars.UPSERT_CHUNK`.
const KITE_UPSERT_CHUNK: usize = 2000;
/// `bhavcopy_backfill.UPSERT_CHUNK`.
const BHAVCOPY_UPSERT_CHUNK: usize = 1000;
/// `bhavcopy_backfill.BAR_SOURCE`.
const BAR_SOURCE: &str = "nse";
/// `bhavcopy_backfill.EQUITY_SERIES`: main board and Emerge; never G-secs, T-bills or debt.
pub(crate) const EQUITY_SERIES: [&str; 6] = ["EQ", "BE", "BZ", "SM", "ST", "SZ"];

fn to_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// One `ohlcv_daily` row as the ingest writes it (raw in, raw out).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct BarRow {
    pub(crate) instrument_id: i64,
    pub(crate) date: Option<NaiveDate>,
    pub(crate) open: Option<Decimal>,
    pub(crate) high: Option<Decimal>,
    pub(crate) low: Option<Decimal>,
    pub(crate) close: Option<Decimal>,
    pub(crate) volume: Option<i64>,
    pub(crate) source: Option<String>,
    /// Bhavcopy only (`None` and not written for a Kite row).
    pub(crate) turnover: Option<Decimal>,
    pub(crate) upper_circuit: Option<Decimal>,
    pub(crate) lower_circuit: Option<Decimal>,
}

/// The rows' columns, for `UNNEST`.
struct Columns {
    ids: Vec<i64>,
    dates: Vec<Option<NaiveDate>>,
    open: Vec<Option<Decimal>>,
    high: Vec<Option<Decimal>>,
    low: Vec<Option<Decimal>>,
    close: Vec<Option<Decimal>>,
    volume: Vec<Option<i64>>,
    source: Vec<Option<String>>,
    turnover: Vec<Option<Decimal>>,
    upper: Vec<Option<Decimal>>,
    lower: Vec<Option<Decimal>>,
}

fn columns(rows: &[BarRow]) -> Columns {
    Columns {
        ids: rows.iter().map(|r| r.instrument_id).collect(),
        dates: rows.iter().map(|r| r.date).collect(),
        open: rows.iter().map(|r| r.open).collect(),
        high: rows.iter().map(|r| r.high).collect(),
        low: rows.iter().map(|r| r.low).collect(),
        close: rows.iter().map(|r| r.close).collect(),
        volume: rows.iter().map(|r| r.volume).collect(),
        source: rows.iter().map(|r| r.source.clone()).collect(),
        turnover: rows.iter().map(|r| r.turnover).collect(),
        upper: rows.iter().map(|r| r.upper_circuit).collect(),
        lower: rows.iter().map(|r| r.lower_circuit).collect(),
    }
}

/// AF 0.6: an adjusted row (`adj_factor != 1`) keeps its stored OHL; `*_raw` always refresh.
const KEEP_OHL: &str = "open = CASE WHEN (ohlcv_daily.adj_factor != 1) THEN ohlcv_daily.open \
                        ELSE excluded.open END, \
                        high = CASE WHEN (ohlcv_daily.adj_factor != 1) THEN ohlcv_daily.high \
                        ELSE excluded.high END, \
                        low = CASE WHEN (ohlcv_daily.adj_factor != 1) THEN ohlcv_daily.low \
                        ELSE excluded.low END, \
                        close_raw = excluded.close_raw, volume_raw = excluded.volume_raw, \
                        open_raw = excluded.open_raw, high_raw = excluded.high_raw, \
                        low_raw = excluded.low_raw";

/// `tasks.bars.upsert_bars` for many instruments at once (each row's conflict target is its own
/// `(instrument_id, date)`; `close`, `volume` and `adj_factor` are step 4's and never
/// overwritten). Returns the rows offered, as Python counts them.
pub(crate) async fn upsert_kite_rows(
    conn: &mut PgConnection,
    rows: &[BarRow],
) -> Result<i64, WorkerError> {
    let mut written = 0;
    for chunk in rows.chunks(KITE_UPSERT_CHUNK) {
        let c = columns(chunk);
        let sql = format!(
            "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, \
             close_raw, volume_raw, open_raw, high_raw, low_raw, adj_factor, source) \
             SELECT u.instrument_id, u.date, u.open, u.high, u.low, u.close, u.volume, u.close, \
             u.volume, u.open, u.high, u.low, 1, u.source \
             FROM UNNEST($1::int8[], $2::date[], $3::numeric[], $4::numeric[], $5::numeric[], \
             $6::numeric[], $7::int8[], $8::varchar[]) \
             AS u(instrument_id, date, open, high, low, close, volume, source) \
             ON CONFLICT (instrument_id, date) DO UPDATE SET {KEEP_OHL}, \
             source = excluded.source"
        );
        sqlx::query(&sql)
            .bind(c.ids)
            .bind(c.dates)
            .bind(c.open)
            .bind(c.high)
            .bind(c.low)
            .bind(c.close)
            .bind(c.volume)
            .bind(c.source)
            .execute(&mut *conn)
            .await?;
        written += to_i64(chunk.len());
    }
    Ok(written)
}

/// `bhavcopy_backfill.upsert_day`: one day's bhavcopy bars, turnover and circuit bands included.
async fn upsert_day(conn: &mut PgConnection, rows: &[BarRow]) -> Result<i64, WorkerError> {
    let mut written = 0;
    for chunk in rows.chunks(BHAVCOPY_UPSERT_CHUNK) {
        let c = columns(chunk);
        let sql = format!(
            "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, close_raw, \
             volume, volume_raw, turnover, upper_circuit, lower_circuit, adj_factor, source, \
             open_raw, high_raw, low_raw) \
             SELECT u.instrument_id, u.date, u.open, u.high, u.low, u.close, u.close, u.volume, \
             u.volume, u.turnover, u.upper_circuit, u.lower_circuit, 1, u.source, u.open, u.high, \
             u.low \
             FROM UNNEST($1::int8[], $2::date[], $3::numeric[], $4::numeric[], $5::numeric[], \
             $6::numeric[], $7::int8[], $8::varchar[], $9::numeric[], $10::numeric[], \
             $11::numeric[]) \
             AS u(instrument_id, date, open, high, low, close, volume, source, turnover, \
             upper_circuit, lower_circuit) \
             ON CONFLICT (instrument_id, date) DO UPDATE SET {KEEP_OHL}, \
             turnover = excluded.turnover, upper_circuit = excluded.upper_circuit, \
             lower_circuit = excluded.lower_circuit, source = excluded.source"
        );
        sqlx::query(&sql)
            .bind(c.ids)
            .bind(c.dates)
            .bind(c.open)
            .bind(c.high)
            .bind(c.low)
            .bind(c.close)
            .bind(c.volume)
            .bind(c.source)
            .bind(c.turnover)
            .bind(c.upper)
            .bind(c.lower)
            .execute(&mut *conn)
            .await?;
        written += to_i64(chunk.len());
    }
    Ok(written)
}

/// `BhavcopyBackfillReport`.
#[derive(Debug, Default)]
pub(crate) struct BhavcopyReport {
    pub(crate) trading_days: usize,
    pub(crate) days_written: i64,
    pub(crate) bars_written: i64,
    pub(crate) unmatched_symbols: BTreeSet<String>,
    pub(crate) missing_days: Vec<NaiveDate>,
    /// Insertion-ordered (`dict`).
    pub(crate) failures: Vec<(String, OrderedJson)>,
}

/// `trading_days_in(session, window)`.
async fn trading_days_in(
    conn: &mut PgConnection,
    window: DateWindow,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trading_day.date FROM trading_day WHERE trading_day.exchange_id = $1 AND \
         trading_day.is_trading_day IS true AND trading_day.date >= $2 AND \
         trading_day.date <= $3 ORDER BY trading_day.date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(window.start())
    .bind(window.end())
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(d,)| d).collect())
}

/// `instrument_ids_by_key`: `(symbol, series) -> id` — keyed on both, so a BE row's bars never
/// land on the EQ listing.
async fn instrument_ids_by_key(
    conn: &mut PgConnection,
) -> Result<HashMap<(String, Option<String>), i64>, WorkerError> {
    let rows: Vec<(String, Option<String>, i64)> = sqlx::query_as(
        "SELECT instrument.symbol, instrument.series, instrument.id FROM instrument \
         WHERE instrument.exchange_id = $1",
    )
    .bind(NSE_EXCHANGE_ID)
    .fetch_all(&mut *conn)
    .await?;
    let mut ids = HashMap::with_capacity(rows.len());
    for (symbol, series, id) in rows {
        ids.insert((symbol, series), id);
    }
    Ok(ids)
}

/// `_rows_for_day`: bhavcopy frame -> `ohlcv_daily` rows for the equity series, plus the
/// `symbol:series` keys nothing matched. Frame order is kept.
pub(super) fn rows_for_day(
    bhav: &polars::prelude::DataFrame,
    ids: &HashMap<(String, Option<String>), i64>,
    series_allowed: &[&str],
) -> Result<(Vec<BarRow>, BTreeSet<String>), WorkerError> {
    let symbol = frame::column(bhav, "symbol")?;
    let series = frame::column(bhav, "series")?;
    let date = frame::column(bhav, "date")?;
    let open = frame::column(bhav, "open")?;
    let high = frame::column(bhav, "high")?;
    let low = frame::column(bhav, "low")?;
    let close = frame::column(bhav, "close")?;
    let volume = frame::column(bhav, "volume")?;
    let turnover = frame::column(bhav, "turnover")?;
    let upper = frame::column(bhav, "upper_circuit")?;
    let lower = frame::column(bhav, "lower_circuit")?;
    let mut values = Vec::new();
    let mut unmatched = BTreeSet::new();
    for i in 0..bhav.height() {
        // `pl.col("series").is_in(series)`: a null series is never in.
        let Some(row_series) = frame::string(series, i)? else {
            continue;
        };
        if !series_allowed.contains(&row_series.as_str()) {
            continue;
        }
        let row_symbol = frame::string(symbol, i)?;
        let key = (
            row_symbol.clone().unwrap_or_default(),
            Some(row_series.clone()),
        );
        let found = match &row_symbol {
            Some(_) => ids.get(&key).copied(),
            None => None,
        };
        let Some(instrument_id) = found else {
            unmatched.insert(format!(
                "{}:{row_series}",
                row_symbol.unwrap_or_else(|| "None".to_owned())
            ));
            continue;
        };
        values.push(BarRow {
            instrument_id,
            date: frame::date(date, i)?,
            open: frame::decimal(open, i)?,
            high: frame::decimal(high, i)?,
            low: frame::decimal(low, i)?,
            close: frame::decimal(close, i)?,
            volume: frame::int(volume, i)?,
            source: Some(BAR_SOURCE.to_owned()),
            turnover: frame::decimal(turnover, i)?,
            upper_circuit: frame::decimal(upper, i)?,
            lower_circuit: frame::decimal(lower, i)?,
        });
    }
    Ok((values, unmatched))
}

/// `backfill_bars_from_bhavcopy(provider, window, progress_every=0, session=session)`: on the
/// chain's own transaction (never a second connection — the M84 wait-for cycle).
pub(crate) async fn backfill_bars_from_bhavcopy(
    conn: &mut PgConnection,
    provider: &CompositeProvider,
    window: DateWindow,
    series: &[&str],
) -> Result<BhavcopyReport, WorkerError> {
    let mut report = BhavcopyReport::default();
    let days = trading_days_in(conn, window).await?;
    let ids = instrument_ids_by_key(conn).await?;
    report.trading_days = days.len();
    if ids.is_empty() {
        report.failures.push((
            "setup".to_owned(),
            pyjson::s(
                "no instrument rows: run `make refdata` (or reference_backfill) first so the \
                 listings register exists to join bars against",
            ),
        ));
        return Ok(report);
    }
    for day in days {
        let bhav = match provider.bhavcopy(day).await {
            Ok(frame) => frame,
            Err(exc) => {
                // A 404 is "this predates the UDiFF archive" or a day NSE published nothing for:
                // missing, not failed — the gate decides what is publishable.
                report.missing_days.push(day);
                let message = exc.to_string();
                if !message.contains("404") {
                    pyjson::update(
                        &mut report.failures,
                        &iso(day),
                        pyjson::s(format!("{}: {message}", exc.python_type())),
                    );
                }
                continue;
            }
        };
        let attempt: Result<(), WorkerError> = async {
            let (values, unmatched) = rows_for_day(&bhav, &ids, series)?;
            report.unmatched_symbols.extend(unmatched);
            if !values.is_empty() {
                report.bars_written += upsert_day(conn, &values).await?;
                report.days_written += 1;
            }
            Ok(())
        }
        .await;
        if let Err(exc) = attempt {
            pyjson::update(
                &mut report.failures,
                &iso(day),
                pyjson::s(format!("{}: {exc}", exc.python_type())),
            );
        }
    }
    Ok(report)
}

/// What a caller outside the chain reads of a bhavcopy backfill (wave 6, lane api_routes:
/// `resync._repair_bars` reads `bars_written`, `missing_days` and `failures`).
#[derive(Debug, Clone, Default)]
pub(crate) struct BackfillSummary {
    /// `report.bars_written`.
    pub(crate) bars_written: i64,
    /// `report.missing_days`.
    pub(crate) missing_days: Vec<NaiveDate>,
    /// `report.failures`, insertion-ordered (`dict`).
    pub(crate) failures: Vec<(String, OrderedJson)>,
}

/// [`backfill_bars_from_bhavcopy`] on `conn`, summarised for a task outside the chain (added at
/// wave 6; the step's own function is unchanged).
///
/// # Errors
/// As [`backfill_bars_from_bhavcopy`].
pub(crate) async fn backfill_summary(
    conn: &mut PgConnection,
    provider: &CompositeProvider,
    window: DateWindow,
) -> Result<BackfillSummary, WorkerError> {
    let report = backfill_bars_from_bhavcopy(conn, provider, window, &EQUITY_SERIES).await?;
    Ok(BackfillSummary {
        bars_written: report.bars_written,
        missing_days: report.missing_days,
        failures: report.failures,
    })
}

/// `_fetch_from_bhavcopy`: the bhavcopy as lead, fallback or top-up; returns its bars written.
async fn fetch_from_bhavcopy(
    conn: &mut PgConnection,
    provider: &CompositeProvider,
    outcome: &mut StepOutcome,
    window: DateWindow,
    reason: &str,
    top_up: Option<i64>,
) -> Result<i64, WorkerError> {
    // `callable(getattr(provider, "bhavcopy", None))` is always true of the composite.
    let report = backfill_bars_from_bhavcopy(conn, provider, window, &EQUITY_SERIES).await?;
    match top_up {
        None => outcome.rows_out = report.bars_written,
        Some(kite_rows) => {
            // Beside a Kite pass that already wrote rows: neither a sum nor a replacement would
            // be true, so `rows_out` stays Kite's and the second number is its own fact.
            outcome.note("kite_rows", pyjson::int(kite_rows));
            outcome.note("bhavcopy_rows", pyjson::int(report.bars_written));
        }
    }
    outcome.note(
        if top_up.is_some() {
            "top_up"
        } else {
            "fallback"
        },
        pyjson::s("bhavcopy"),
    );
    outcome.note("fallback_reason", pyjson::s(reason));
    outcome.note("window", pyjson::s(window.to_string()));
    outcome.note("days_written", pyjson::int(report.days_written));
    outcome.note(
        "missing_days",
        if report.missing_days.is_empty() {
            OrderedJson::Null
        } else {
            pyjson::strings(report.missing_days.iter().map(|d| iso(*d)))
        },
    );
    outcome.note(
        "unmatched_symbol_count",
        if report.unmatched_symbols.is_empty() {
            OrderedJson::Null
        } else {
            pyjson::int(to_i64(report.unmatched_symbols.len()))
        },
    );
    // NOT `failures=` (M84.2): the Kite pass's own per-symbol failures live under that key.
    outcome.note(
        "bhavcopy_failures",
        if report.failures.is_empty() {
            OrderedJson::Null
        } else {
            OrderedJson::Object(report.failures)
        },
    );
    Ok(report.bars_written)
}

/// `_instruments_with_bars(session, on)`: ids already holding a bar for `on`, including rows
/// this transaction wrote but has not committed.
async fn instruments_with_bars(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<BTreeSet<i64>, WorkerError> {
    let rows: Vec<(i64,)> = sqlx::query_as(
        "SELECT DISTINCT ohlcv_daily.instrument_id FROM ohlcv_daily WHERE ohlcv_daily.date = $1",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(id,)| id).collect())
}

/// `_lead_with_bhavcopy`: the session's bhavcopy first (one request); returns its rows and the
/// names Kite must still fetch. Never fatal: Kite does the whole pass on any failure.
async fn lead_with_bhavcopy(
    conn: &mut PgConnection,
    provider: &CompositeProvider,
    outcome: &mut StepOutcome,
    window: DateWindow,
    instruments: &[ActiveInstrument],
) -> Result<(i64, Vec<ActiveInstrument>), WorkerError> {
    let remaining = instruments.to_vec();
    if window.days() != 1 {
        return Ok((0, remaining));
    }
    let rows = match fetch_from_bhavcopy(
        conn,
        provider,
        outcome,
        window,
        "the exchange's own record, one request for the session",
        None,
    )
    .await
    {
        Ok(rows) => rows,
        Err(exc) => {
            outcome.note(
                "bhavcopy_first_error",
                pyjson::s(format!("{}: {exc}", exc.python_type())),
            );
            return Ok((0, remaining));
        }
    };
    if rows <= 0 {
        return Ok((0, remaining));
    }
    let covered = instruments_with_bars(conn, window.end()).await?;
    let kept: Vec<ActiveInstrument> = remaining
        .iter()
        .filter(|r| !covered.contains(&r.instrument_id))
        .cloned()
        .collect();
    outcome.note("bhavcopy_first", pyjson::int(rows));
    outcome.note(
        "kite_calls_saved",
        pyjson::int(to_i64(remaining.len() - kept.len())),
    );
    outcome.note("kite_calls_remaining", pyjson::int(to_i64(kept.len())));
    Ok((rows, kept))
}

/// `_credentials_are_missing`: read off the recorded failures, so the note and the decision to
/// stop are one fact.
fn credentials_are_missing(failures: &[(String, OrderedJson)]) -> bool {
    failures
        .iter()
        .any(|(_, m)| matches!(m, OrderedJson::Str(s) if s.starts_with("CredentialsMissing")))
}

/// Frame rows of one Kite `daily_bars` answer, for `instrument_id`.
pub(crate) fn kite_rows(
    instrument_id: i64,
    bars: &polars::prelude::DataFrame,
) -> Result<Vec<BarRow>, WorkerError> {
    let date = frame::column(bars, "date")?;
    let open = frame::column(bars, "open")?;
    let high = frame::column(bars, "high")?;
    let low = frame::column(bars, "low")?;
    let close = frame::column(bars, "close")?;
    let volume = frame::column(bars, "volume")?;
    let source = frame::column(bars, "source")?;
    let mut out = Vec::with_capacity(bars.height());
    for i in 0..bars.height() {
        out.push(BarRow {
            instrument_id,
            date: frame::date(date, i)?,
            open: frame::decimal(open, i)?,
            high: frame::decimal(high, i)?,
            low: frame::decimal(low, i)?,
            close: frame::decimal(close, i)?,
            volume: frame::int(volume, i)?,
            source: frame::string(source, i)?,
            turnover: None,
            upper_circuit: None,
            lower_circuit: None,
        });
    }
    Ok(out)
}

/// `run_fetch_daily_bars`: fetch and upsert raw bars for `instruments` over `window`.
pub(super) async fn run_fetch_daily_bars(
    conn: &mut PgConnection,
    provider: &CompositeProvider,
    outcome: &mut StepOutcome,
    instruments: &[ActiveInstrument],
    window: DateWindow,
) -> Result<i64, WorkerError> {
    // `callable(getattr(provider, "daily_bars", None))` is always true of the composite.
    let mut written: i64 = 0;
    let mut failures: Vec<(String, OrderedJson)> = Vec::new();
    let mut skipped_no_token: i64 = 0;

    let (bhavcopy_first, instruments) =
        lead_with_bhavcopy(conn, provider, outcome, window, instruments).await?;
    written += bhavcopy_first;

    // Fetch first, write at the end: the network phase holds no row locks (8 Sep 2026). The
    // buffer is written in one pass; Python's mid-fetch flush at 250,000 rows changes when the
    // rows are written, not what is written.
    let mut pending: Vec<BarRow> = Vec::new();
    for instrument in &instruments {
        if credentials_are_missing(&failures) {
            // CredentialsMissing is the deployment's, not the instrument's: stop asking.
            break;
        }
        let Some(token) = instrument.kite_token else {
            skipped_no_token += 1;
            continue;
        };
        match provider
            .daily_bars(token, window.start(), window.end())
            .await
        {
            Ok(bars) => pending.extend(kite_rows(instrument.instrument_id, &bars)?),
            Err(exc) if exc.kind() == ErrorKind::CredentialsMissing => {
                pyjson::update(
                    &mut failures,
                    &instrument.symbol,
                    pyjson::s(format!("CredentialsMissing: {exc}")),
                );
            }
            Err(exc) if exc.is_provider_error() => {
                pyjson::update(
                    &mut failures,
                    &instrument.symbol,
                    pyjson::s(exc.to_string()),
                );
            }
            Err(exc) => return Err(exc.into()),
        }
    }
    written += upsert_kite_rows(conn, &pending).await?;

    outcome.rows_in = to_i64(instruments.len());
    outcome.rows_out = written;
    outcome.note("window", pyjson::s(window.to_string()));
    outcome.note("instruments_without_token", pyjson::int(skipped_no_token));
    let failure_count = failures.len();
    outcome.note(
        "failures",
        if failures.is_empty() {
            OrderedJson::Null
        } else {
            OrderedJson::Object(failures)
        },
    );
    if failure_count > 0 {
        outcome.note("failure_count", pyjson::int(to_i64(failure_count)));
    }

    if written == 0 && !instruments.is_empty() {
        return fetch_from_bhavcopy(
            conn,
            provider,
            outcome,
            window,
            "daily_bars produced no rows",
            None,
        )
        .await;
    }
    // M84: a partial Kite pass for one session is completed from the bhavcopy — unless the
    // bhavcopy already led.
    if window.days() == 1 && bhavcopy_first == 0 && (failure_count > 0 || skipped_no_token > 0) {
        let reason =
            format!("kite left gaps: {failure_count} failed, {skipped_no_token} without a token");
        fetch_from_bhavcopy(conn, provider, outcome, window, &reason, Some(written)).await?;
    }
    Ok(written)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;
    use polars::prelude::{
        Column, DataFrame, Int128Chunked, IntoColumn, IntoSeries, NamedFrom, NewChunkedArray,
        Series,
    };

    #[test]
    fn credentials_missing_is_read_off_the_failures() {
        let mut failures = vec![("A".to_owned(), pyjson::s("[kite] rate limited"))];
        assert!(!credentials_are_missing(&failures));
        failures.push(("B".to_owned(), pyjson::s("CredentialsMissing: no key")));
        assert!(credentials_are_missing(&failures));
    }

    #[test]
    fn the_equity_series_exclude_government_paper() {
        for s in ["GS", "GB", "TB", "N0", "NF"] {
            assert!(!EQUITY_SERIES.contains(&s));
        }
        for s in ["EQ", "BE", "BZ", "SM", "ST", "SZ"] {
            assert!(EQUITY_SERIES.contains(&s));
        }
    }

    #[test]
    fn rows_for_day_matches_on_symbol_and_series() {
        let on = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap();
        let bhav = DataFrame::new(
            4,
            vec![
                Series::new("symbol".into(), ["AAA", "AAA", "ZZZ", "AAA"]).into_column(),
                Series::new("series".into(), [Some("EQ"), Some("GS"), Some("BE"), None])
                    .into_column(),
                Series::new("date".into(), [on; 4]).into_column(),
                dec4("open", [15_000, 20_000, 30_000, 1]),
                dec4("high", [15_000, 20_000, 30_000, 1]),
                dec4("low", [15_000, 20_000, 30_000, 1]),
                dec4("close", [15_100, 20_000, 30_000, 1]),
                dec4("prev_close", [15_000, 20_000, 30_000, 1]),
                Series::new("volume".into(), [10i64, 20, 30, 1]).into_column(),
                Int128Chunked::from_iter_options(
                    "turnover".into(),
                    [Some(1500i128), None, None, None].into_iter(),
                )
                .into_decimal_unchecked(20, 2)
                .into_series()
                .into_column(),
                Series::new("trades".into(), [1i64, 2, 3, 4]).into_column(),
                dec4("upper_circuit", [16_000, 21_000, 31_000, 1]),
                dec4("lower_circuit", [14_000, 19_000, 29_000, 1]),
            ],
        )
        .unwrap();
        let mut ids = HashMap::new();
        ids.insert(("AAA".to_owned(), Some("EQ".to_owned())), 7);
        ids.insert(("AAA".to_owned(), Some("GS".to_owned())), 8);
        let (rows, unmatched) = rows_for_day(&bhav, &ids, &EQUITY_SERIES).unwrap();
        // GS is government paper and a null series is never in: only the EQ row is written.
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].instrument_id, 7);
        assert_eq!(rows[0].date, Some(on));
        assert_eq!(rows[0].close, Some(Decimal::new(15_100, 4)));
        assert_eq!(rows[0].turnover, Some(Decimal::new(1500, 2)));
        assert_eq!(rows[0].source.as_deref(), Some("nse"));
        assert_eq!(unmatched.into_iter().collect::<Vec<_>>(), ["ZZZ:BE"]);
    }

    fn dec4(name: &str, mantissas: [i128; 4]) -> Column {
        Int128Chunked::from_iter_options(name.into(), mantissas.into_iter().map(Some))
            .into_decimal_unchecked(18, 4)
            .into_series()
            .into_column()
    }
}
