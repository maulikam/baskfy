//! `reconcile_calendar` — the calendar asserted against observed bar dates (docs/09 §Schedule).
//! Ports `baskfy_worker.calendar.reconcile_calendar` and `_mark`, with the M62 publication check
//! (`baskfy_providers.publication.bhavcopy_publication_check`) asked through the composite.
//!
//! A date with bars becomes `source='bhavcopy'`, a trading day. A dense range's weekdays with no
//! bars become inferred holidays — **unless NSE published a bhavcopy for them**, in which case
//! the day is a session we failed to ingest and is reported, never recorded as a holiday.

use std::collections::BTreeSet;

use baskfy_providers::composite::CompositeProvider;
use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;

/// `MIN_INSTRUMENTS_FOR_HOLIDAY_INFERENCE`: below this many instruments with bars, "no bars"
/// means "no backfill", not "holiday".
pub(super) const MIN_INSTRUMENTS_FOR_HOLIDAY_INFERENCE: i64 = 20;

/// `baskfy_core.models.reference.INFERRED_HOLIDAY_NAME`.
pub(super) const INFERRED_HOLIDAY_NAME: &str = "inferred: no instrument traded";

/// `ReconciliationResult`, as far as anything reads it: the orchestrator warns on
/// `missed_sessions` and nothing reads `confirmed`, `inferred_holidays` or `still_provisional`,
/// so the count query behind the last (a read with no effect) is not issued.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct ReconciliationResult {
    pub(super) missed_sessions: Vec<NaiveDate>,
}

/// `bhavcopy_publication_check(provider)(day)`: a frame with rows is `true`; a provider error
/// (no file, an unreadable archive) is `false` — "no evidence", never "the exchange was shut".
async fn published(provider: &CompositeProvider, day: NaiveDate) -> Result<bool, WorkerError> {
    match provider.bhavcopy(day).await {
        Ok(frame) => Ok(frame.height() > 0),
        Err(e) if e.is_provider_error() => Ok(false),
        Err(e) => Err(e.into()),
    }
}

/// `_mark`, for many days with one verdict (each an upsert on `(exchange_id, date)`).
async fn mark(
    conn: &mut PgConnection,
    days: &[NaiveDate],
    is_trading_day: bool,
    holiday_name: Option<&str>,
) -> Result<(), WorkerError> {
    if days.is_empty() {
        return Ok(());
    }
    sqlx::query(
        "INSERT INTO trading_day (exchange_id, date, is_trading_day, holiday_name, source) \
         SELECT $1::smallint, u.date, $2, $3, 'bhavcopy' FROM UNNEST($4::date[]) AS u(date) \
         ON CONFLICT (exchange_id, date) DO UPDATE SET is_trading_day = \
         excluded.is_trading_day, holiday_name = excluded.holiday_name, source = excluded.source",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(is_trading_day)
    .bind(holiday_name)
    .bind(days)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// `reconcile_calendar(session, start, end, infer_holidays=, published=)`. `provider = None` is
/// Python's `published=None` (the pre-M62 behaviour, for callers with no provider).
pub(super) async fn reconcile_calendar(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
    infer_holidays: bool,
    provider: Option<&CompositeProvider>,
) -> Result<ReconciliationResult, WorkerError> {
    let counts: Vec<(NaiveDate, i64)> = sqlx::query_as(
        "SELECT ohlcv_daily.date, count(*) AS count_1 FROM ohlcv_daily \
         WHERE ohlcv_daily.date >= $1 AND ohlcv_daily.date <= $2 GROUP BY ohlcv_daily.date",
    )
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    let observed: BTreeSet<NaiveDate> = counts
        .iter()
        .filter(|(_, n)| *n > 0)
        .map(|(d, _)| *d)
        .collect();
    let dense = counts
        .iter()
        .map(|(_, n)| *n)
        .max()
        .is_some_and(|m| m >= MIN_INSTRUMENTS_FOR_HOLIDAY_INFERENCE);

    let observed_days: Vec<NaiveDate> = observed.iter().copied().collect();
    mark(conn, &observed_days, true, None).await?;

    let mut inferred: Vec<NaiveDate> = Vec::new();
    let mut skipped_published: Vec<NaiveDate> = Vec::new();
    if infer_holidays && dense {
        let weekdays: Vec<(NaiveDate,)> = sqlx::query_as(
            "SELECT trading_day.date FROM trading_day WHERE trading_day.exchange_id = $1 AND \
             trading_day.date >= $2 AND trading_day.date <= $3 AND \
             trading_day.is_trading_day IS true AND trading_day.source = 'derived'",
        )
        .bind(NSE_EXCHANGE_ID)
        .bind(start)
        .bind(end)
        .fetch_all(&mut *conn)
        .await?;
        for (day,) in weekdays {
            if observed.contains(&day) {
                continue;
            }
            // The exchange's own answer beats our absence of data.
            if let Some(p) = provider
                && published(p, day).await?
            {
                skipped_published.push(day);
                continue;
            }
            inferred.push(day);
        }
        mark(conn, &inferred, false, Some(INFERRED_HOLIDAY_NAME)).await?;
    }

    skipped_published.sort();
    Ok(ReconciliationResult {
        missed_sessions: skipped_published,
    })
}
