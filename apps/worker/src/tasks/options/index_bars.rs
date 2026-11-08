//! NIFTY 50, INDIA VIX and NIFTY BANK one-minute bars into `op_index_minute`
//! (`options.index_bars`, OP3).
//!
//! Three writers, one upsert: the **intraday** pull (the closed bars since the last one stored
//! today), the **end-of-day reconcile** (the whole session again) and the resumable **Tier-1
//! backfill** (Kite's 60-day windows from the newest stored bar). Only closed minutes are written
//! (`ts + 1 min <= now`); the upsert on `(instrument_id, ts)` overwrites prices with Kite's latest
//! reading (house rule 7), each rounded half-up to the column's two places (house rule 8). The
//! indices are found in `instrument` by symbol with Kite's token from the same row.

use std::collections::BTreeMap;
use std::time::Instant;

use baskfy_db::OrderedJson;
use baskfy_providers::kite::{KiteProvider, MINUTE_MAX_DAYS_PER_REQUEST, MinuteBound};
use baskfy_providers::records::MinuteBarRecord;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Postgres, QueryBuilder};

use super::common::{count, in_ist, ist_at, quantize_half_up, seconds_json};
use super::model::{market_close, market_open};
use crate::error::WorkerError;
use crate::pyjson::{object, s};
use crate::steps::log_warning;

/// `index_bars.NIFTY_50`.
pub(crate) const NIFTY_50: &str = "NIFTY 50";
/// `index_bars.INDEX_SYMBOLS`, in Python's order.
pub(crate) const INDEX_SYMBOLS: [&str; 3] = [NIFTY_50, "INDIA VIX", "NIFTY BANK"];
const SOURCE_KITE: &str = "KITE_HIST";
const UPSERT_CHUNK: usize = 2000;
const NO_ROW: &str = "no INDEX row with a Kite token in `instrument`";

/// `index_bars.BarsReport`.
#[derive(Debug, Clone, Default)]
pub(crate) struct BarsReport {
    pub(crate) written: Vec<(String, usize)>,
    pub(crate) calls: usize,
    pub(crate) skipped: Vec<(String, String)>,
    pub(crate) seconds: f64,
}

impl BarsReport {
    /// `as_dict()`, as pairs (the tasks spread it after their own keys).
    pub(crate) fn pairs(&self) -> Vec<(String, OrderedJson)> {
        vec![
            (
                "written".to_owned(),
                object(self.written.iter().map(|(k, n)| (k.clone(), count(*n)))),
            ),
            ("calls".to_owned(), count(self.calls)),
            (
                "skipped".to_owned(),
                object(self.skipped.iter().map(|(k, v)| (k.clone(), s(v.clone())))),
            ),
            ("seconds".to_owned(), seconds_json(self.seconds)),
        ]
    }
}

/// `session_bounds(day)`: 09:15 and 15:30 IST on `day`, aware.
pub(crate) fn session_bounds(
    day: NaiveDate,
) -> Result<(DateTime<FixedOffset>, DateTime<FixedOffset>), WorkerError> {
    Ok((ist_at(day, market_open())?, ist_at(day, market_close())?))
}

/// `closed_bars(bars, now)`: only the minutes finished by `now`.
fn closed_bars(bars: Vec<MinuteBarRecord>, now: DateTime<FixedOffset>) -> Vec<MinuteBarRecord> {
    bars.into_iter()
        .filter(|bar| {
            bar.ts
                .aware()
                .is_some_and(|ts| ts + Duration::minutes(1) <= now)
        })
        .collect()
}

/// An index's `instrument` row: our id and Kite's token.
#[derive(Debug, Clone, Copy)]
struct IndexRow {
    instrument_id: i64,
    kite_token: i64,
}

/// `index_rows(session)`.
async fn index_rows(conn: &mut PgConnection) -> Result<BTreeMap<String, IndexRow>, WorkerError> {
    let rows: Vec<(String, i64, Option<i64>)> = sqlx::query_as(
        "SELECT instrument.symbol, instrument.id, instrument.kite_token FROM instrument \
         WHERE instrument.symbol = ANY($1) AND instrument.instrument_type = 'INDEX' \
         AND instrument.kite_token IS NOT NULL",
    )
    .bind(INDEX_SYMBOLS.to_vec())
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .filter_map(|(symbol, id, token)| {
            token.map(|kite_token| {
                (
                    symbol,
                    IndexRow {
                        instrument_id: id,
                        kite_token,
                    },
                )
            })
        })
        .collect())
}

/// `upsert_bars(session, instrument_id, bars)`: the rows sent. A bar whose high is below its low
/// is dropped and logged, never repaired.
async fn upsert_bars(
    conn: &mut PgConnection,
    instrument_id: i64,
    bars: Vec<MinuteBarRecord>,
) -> Result<usize, WorkerError> {
    let total = bars.len();
    let sane: Vec<MinuteBarRecord> = bars.into_iter().filter(|b| b.high >= b.low).collect();
    if sane.len() != total {
        log_warning(&format!(
            "dropped {} minute bars with high < low",
            total - sane.len()
        ));
    }
    let mut sent = 0;
    for batch in sane.chunks(UPSERT_CHUNK) {
        let rows: Vec<(DateTime<FixedOffset>, [Decimal; 4])> = batch
            .iter()
            .filter_map(|bar| {
                bar.ts.aware().map(|ts| {
                    (
                        in_ist(ts),
                        [bar.open, bar.high, bar.low, bar.close].map(|v| quantize_half_up(v, 2)),
                    )
                })
            })
            .collect();
        if rows.is_empty() {
            continue;
        }
        let mut q: QueryBuilder<'_, Postgres> = QueryBuilder::new(
            "INSERT INTO op_index_minute (instrument_id, ts, open, high, low, close, source) ",
        );
        q.push_values(rows.iter(), |mut b, (ts, [o, h, l, c])| {
            b.push_bind(instrument_id)
                .push_bind(*ts)
                .push_bind(*o)
                .push_bind(*h)
                .push_bind(*l)
                .push_bind(*c)
                .push_bind(SOURCE_KITE);
        });
        q.push(
            " ON CONFLICT (instrument_id, ts) DO UPDATE SET open = excluded.open, \
             high = excluded.high, low = excluded.low, close = excluded.close, \
             source = excluded.source",
        );
        q.build().execute(&mut *conn).await?;
        sent += rows.len();
    }
    Ok(sent)
}

/// `latest_ts(session, instrument_id, lo, hi)`.
async fn latest_ts(
    conn: &mut PgConnection,
    instrument_id: i64,
    lo: DateTime<FixedOffset>,
    hi: DateTime<FixedOffset>,
) -> Result<Option<DateTime<FixedOffset>>, WorkerError> {
    let value: Option<DateTime<chrono::Utc>> = sqlx::query_scalar(
        "SELECT max(op_index_minute.ts) FROM op_index_minute \
         WHERE op_index_minute.instrument_id = $1 AND op_index_minute.ts >= $2 \
         AND op_index_minute.ts <= $3",
    )
    .bind(instrument_id)
    .bind(lo)
    .bind(hi)
    .fetch_one(&mut *conn)
    .await?;
    Ok(value.map(|v| in_ist(v.fixed_offset())))
}

/// `latest_close(session, symbol, since)`: the newest stored close at or after `since` — the
/// collector's ATM hint.
pub(crate) async fn latest_close(
    conn: &mut PgConnection,
    symbol: &str,
    since: DateTime<FixedOffset>,
) -> Result<Option<Decimal>, WorkerError> {
    let row: Option<(DateTime<chrono::Utc>, Decimal)> = sqlx::query_as(
        "SELECT op_index_minute.ts, op_index_minute.close FROM op_index_minute \
         JOIN instrument ON instrument.id = op_index_minute.instrument_id \
         WHERE instrument.symbol = $1 AND op_index_minute.ts >= $2 \
         ORDER BY op_index_minute.ts DESC LIMIT 1",
    )
    .bind(symbol)
    .bind(since)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|(_, close)| close))
}

async fn read(
    reader: &KiteProvider,
    token: i64,
    lo: impl Into<MinuteBound> + Send,
    hi: impl Into<MinuteBound> + Send,
) -> Result<Vec<MinuteBarRecord>, WorkerError> {
    reader
        .minute_bars(token, lo, hi)
        .await
        .map_err(WorkerError::Provider)
}

/// `pull_intraday(session, reader, now)`: per index, the closed bars since the newest stored
/// minute of today (or 09:15), one call each.
pub(crate) async fn pull_intraday(
    conn: &mut PgConnection,
    reader: &KiteProvider,
    now: DateTime<FixedOffset>,
) -> Result<BarsReport, WorkerError> {
    let started = Instant::now();
    let mut report = BarsReport::default();
    let (open_at, close_at) = session_bounds(in_ist(now).date_naive())?;
    let rows = index_rows(conn).await?;
    for symbol in INDEX_SYMBOLS {
        let Some(row) = rows.get(symbol).copied() else {
            report.skipped.push((symbol.to_owned(), NO_ROW.to_owned()));
            continue;
        };
        let last = latest_ts(conn, row.instrument_id, open_at, close_at).await?;
        let lo = last.unwrap_or(open_at);
        let hi = now.min(close_at);
        let bars = read(reader, row.kite_token, lo, hi).await?;
        report.calls += 1;
        let written = upsert_bars(conn, row.instrument_id, closed_bars(bars, now)).await?;
        report.written.push((symbol.to_owned(), written));
    }
    report.seconds = started.elapsed().as_secs_f64();
    Ok(report)
}

/// `reconcile_day(session, reader, day, now)`: the whole of `day`'s session again, per index.
pub(crate) async fn reconcile_day(
    conn: &mut PgConnection,
    reader: &KiteProvider,
    day: NaiveDate,
    now: DateTime<FixedOffset>,
) -> Result<BarsReport, WorkerError> {
    let started = Instant::now();
    let mut report = BarsReport::default();
    let (open_at, close_at) = session_bounds(day)?;
    let rows = index_rows(conn).await?;
    for symbol in INDEX_SYMBOLS {
        let Some(row) = rows.get(symbol).copied() else {
            report.skipped.push((symbol.to_owned(), NO_ROW.to_owned()));
            continue;
        };
        let bars = read(reader, row.kite_token, open_at, close_at).await?;
        report.calls += 1;
        let written = upsert_bars(conn, row.instrument_id, closed_bars(bars, now)).await?;
        report.written.push((symbol.to_owned(), written));
    }
    report.seconds = started.elapsed().as_secs_f64();
    Ok(report)
}

/// `_day_windows(start, end)`: Kite's 60-day minute windows.
fn day_windows(start: NaiveDate, end: NaiveDate) -> Vec<(NaiveDate, NaiveDate)> {
    let span = Duration::days(MINUTE_MAX_DAYS_PER_REQUEST - 1);
    let mut out = Vec::new();
    let mut cursor = start;
    while cursor <= end {
        let chunk_end = (cursor + span).min(end);
        out.push((cursor, chunk_end));
        cursor = chunk_end + Duration::days(1);
    }
    out
}

/// `backfill(session, reader, start, end, now=..., checkpoint=session.commit)` under
/// `run_checkpointed`: per index, from the date of the newest stored bar in the range, window by
/// window, **committed after each window** so an interruption keeps what finished; a raise rolls
/// back only the window in flight.
///
/// # Errors
/// `ValueError` for an inverted range; the provider's and the database's otherwise.
pub(crate) async fn backfill(
    db: &baskfy_db::WriteDb,
    reader: &KiteProvider,
    start: NaiveDate,
    end: NaiveDate,
    now: DateTime<FixedOffset>,
) -> Result<BarsReport, WorkerError> {
    if end < start {
        return Err(WorkerError::python(
            "ValueError",
            format!(
                "end {} precedes start {}",
                end.format("%Y-%m-%d"),
                start.format("%Y-%m-%d")
            ),
        ));
    }
    let started = Instant::now();
    let mut report = BarsReport::default();
    let mut tx = db.begin().await?;
    let rows = index_rows(&mut tx).await?;
    let last_second = NaiveTime::from_hms_opt(23, 59, 59).unwrap_or(NaiveTime::MIN);
    for symbol in INDEX_SYMBOLS {
        let Some(row) = rows.get(symbol).copied() else {
            report.skipped.push((symbol.to_owned(), NO_ROW.to_owned()));
            continue;
        };
        let lo = ist_at(start, NaiveTime::MIN)?;
        let hi = ist_at(end, last_second)?;
        let last = latest_ts(&mut tx, row.instrument_id, lo, hi).await?;
        let resume = last.map_or(start, |t| start.max(in_ist(t).date_naive()));
        let mut written = 0;
        for (w_lo, w_hi) in day_windows(resume, end) {
            let bars = read(reader, row.kite_token, w_lo, w_hi).await?;
            report.calls += 1;
            written += upsert_bars(&mut tx, row.instrument_id, closed_bars(bars, now)).await?;
            tx.commit().await?;
            tx = db.begin().await?;
        }
        report.written.push((symbol.to_owned(), written));
    }
    tx.commit().await?;
    report.seconds = started.elapsed().as_secs_f64();
    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn windows_are_sixty_days_and_contiguous() {
        let d = |m, day| NaiveDate::from_ymd_opt(2026, m, day).unwrap();
        let w = day_windows(d(1, 1), d(4, 30));
        assert_eq!(w.first().copied(), Some((d(1, 1), d(3, 1))));
        assert_eq!(w.last().map(|x| x.1), Some(d(4, 30)));
        for pair in w.windows(2) {
            assert_eq!(pair[0].1 + Duration::days(1), pair[1].0);
        }
        assert!(day_windows(d(2, 1), d(1, 1)).is_empty());
    }

    #[test]
    fn the_session_is_0915_to_1530_ist() {
        let (open, close) = session_bounds(NaiveDate::from_ymd_opt(2026, 10, 29).unwrap()).unwrap();
        assert_eq!(open.to_rfc3339(), "2026-10-29T09:15:00+05:30");
        assert_eq!(close.to_rfc3339(), "2026-10-29T15:30:00+05:30");
    }
}
