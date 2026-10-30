//! Trading-day awareness: the guard every task calls. Ports `baskfy_worker.calendar.classify`
//! and `require_trading_day`; `reconcile_calendar` is the ingest lane's
//! ([`crate::steps::ingest::reconcile_calendar`]).

use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::error::WorkerError;

/// `baskfy_core.seed_data.NSE_EXCHANGE_ID`.
pub const NSE_EXCHANGE_ID: i16 = 1;

/// `CALENDAR_LOOKBACK_DAYS`: 12-month factor windows need the lunar holidays a year behind
/// `as_of`; 400 calendar days covers 12 months plus snap slack (T9.2).
pub const CALENDAR_LOOKBACK_DAYS: i64 = 400;

/// `NotATradingDay`: work attempted for a date the exchange was closed. Not a failure — the
/// orchestrator records the run as aborted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotATradingDay {
    /// The date.
    pub date: NaiveDate,
    /// The holiday's name, or the calendar row's source, or why there is no row.
    pub reason: String,
}

impl std::fmt::Display for NotATradingDay {
    /// `f"{date.isoformat()} is not an NSE trading day: {reason}"`.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{} is not an NSE trading day: {}",
            self.date.format("%Y-%m-%d"),
            self.reason
        )
    }
}

/// `CalendarVerdict`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CalendarVerdict {
    /// The date.
    pub date: NaiveDate,
    /// Was there a session.
    pub is_trading_day: bool,
    /// `seed`, `derived`, `holiday`, `observed`, ...
    pub source: String,
    /// The holiday's name, when it has one.
    pub holiday_name: Option<String>,
}

/// `classify`: what the calendar says about `date`.
///
/// # Errors
/// `Ok(Err(NotATradingDay))` for a date outside the loaded calendar; the database's otherwise.
pub async fn classify(
    conn: &mut PgConnection,
    date: NaiveDate,
) -> Result<Result<CalendarVerdict, NotATradingDay>, WorkerError> {
    let row: Option<(bool, String, Option<String>)> = sqlx::query_as(
        "SELECT is_trading_day, source, holiday_name FROM trading_day \
         WHERE exchange_id = $1 AND date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(date)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(match row {
        None => Err(NotATradingDay {
            date,
            reason: "outside the loaded calendar range; run `make seed`".to_owned(),
        }),
        Some((is_trading_day, source, holiday_name)) => Ok(CalendarVerdict {
            date,
            is_trading_day,
            source,
            holiday_name,
        }),
    })
}

/// `require_trading_day`: the guard every task calls before touching data for `date`.
///
/// # Errors
/// As [`classify`]; `Ok(Err(..))` for a holiday or a weekend (reason: the holiday's name, or
/// the row's source).
pub async fn require_trading_day(
    conn: &mut PgConnection,
    date: NaiveDate,
) -> Result<Result<CalendarVerdict, NotATradingDay>, WorkerError> {
    Ok(match classify(conn, date).await? {
        Err(e) => Err(e),
        Ok(verdict) if !verdict.is_trading_day => Err(NotATradingDay {
            date,
            reason: verdict
                .holiday_name
                .clone()
                .filter(|n| !n.is_empty())
                .unwrap_or_else(|| verdict.source.clone()),
        }),
        Ok(verdict) => Ok(verdict),
    })
}
