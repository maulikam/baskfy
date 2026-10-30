//! SW11B — the catalyst feed at 09:10. Ports `baskfy_worker.tasks.swing_catalyst`
//! (`feed_symbols`, `nearest_result`, `upsert_catalysts`, `fill_watch`, `run_swing_catalyst`).
//!
//! Per symbol, `announcements` and `results_calendar` through the NSE provider (archive first,
//! its cookie discipline and limiter); `sw_catalyst` upserted on `(user_id, instrument_id, url)`;
//! `sw_watch.catalyst` filled only where empty, `sw_watch.earnings_date` refreshed. **Fail soft:**
//! a provider error on a symbol is a count and a note, never a raise. Nothing here places
//! anything anywhere.

use std::collections::BTreeMap;

use baskfy_db::OrderedJson;
use baskfy_providers::factory::{RedisSource, build_nse_provider};
use baskfy_providers::nse::{
    CATALYST_SOURCE_ANNOUNCEMENT, CATALYST_SOURCE_EVENT_CALENDAR, NseProvider,
};
use baskfy_providers::records::{CatalystRecord, EarningsDateRecord, PyDateTime};
use baskfy_providers::settings::ProviderSettings;
use chrono::{DateTime, Days, FixedOffset, NaiveDate};
use sqlx::PgConnection;

use super::recent_trading_days;
use super::watch::WATCHING;
use crate::error::WorkerError;
use crate::pyjson::{int, object, opt_s, s, strings};
use crate::steps::{StepOutcome, log_warning};

/// `CatalystReport`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct CatalystReport {
    session_date: NaiveDate,
    symbols: i64,
    announcements: i64,
    earnings_dates: i64,
    rows_written: i64,
    catalysts_filled: i64,
    earnings_flagged: i64,
    failed: Vec<String>,
    provider_error: Option<String>,
    skipped_reason: Option<String>,
}

impl CatalystReport {
    fn new(session_date: NaiveDate) -> Self {
        Self {
            session_date,
            symbols: 0,
            announcements: 0,
            earnings_dates: 0,
            rows_written: 0,
            catalysts_filled: 0,
            earnings_flagged: 0,
            failed: Vec::new(),
            provider_error: None,
            skipped_reason: None,
        }
    }

    /// `as_detail()`.
    pub(super) fn as_detail(&self) -> OrderedJson {
        object([
            (
                "session_date",
                s(self.session_date.format("%Y-%m-%d").to_string()),
            ),
            ("symbols", int(self.symbols)),
            ("announcements", int(self.announcements)),
            ("earnings_dates", int(self.earnings_dates)),
            ("rows_written", int(self.rows_written)),
            ("catalysts_filled", int(self.catalysts_filled)),
            ("earnings_flagged", int(self.earnings_flagged)),
            ("failed", strings(self.failed.clone())),
            ("provider_error", opt_s(self.provider_error.clone())),
            ("skipped_reason", opt_s(self.skipped_reason.clone())),
        ])
    }
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `build_nse_provider(get_provider_settings(), retry_hooks=provider_retry_hooks())` (the hooks
/// are metrics only and not ported).
///
/// # Errors
/// The settings' and the provider's construction.
pub(super) async fn build_provider() -> Result<NseProvider, WorkerError> {
    let settings =
        ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))?;
    let source = RedisSource::from_settings(&settings);
    Ok(build_nse_provider(&source, &settings, None, None).await?)
}

/// `feed_symbols`: `symbol -> instrument_id` for the WATCHING names, the last session's EP
/// candidates, and the latest VBT and TWT SIGNAL names — sorted, each once (first source wins).
async fn feed_symbols(
    conn: &mut PgConnection,
    user_id: i64,
    session_date: NaiveDate,
) -> Result<BTreeMap<String, i64>, WorkerError> {
    let watched: Vec<(String, i64)> = sqlx::query_as(
        "SELECT instrument.symbol, instrument.id FROM instrument \
         JOIN sw_watch ON sw_watch.instrument_id = instrument.id \
         WHERE sw_watch.user_id = $1 AND sw_watch.state = $2",
    )
    .bind(user_id)
    .bind(WATCHING)
    .fetch_all(&mut *conn)
    .await?;
    // A dict comprehension: a repeated symbol keeps its last id.
    let mut symbols: BTreeMap<String, i64> = BTreeMap::new();
    for (symbol, id) in watched {
        symbols.insert(symbol, id);
    }
    let before = session_date
        .checked_sub_days(Days::new(1))
        .ok_or_else(|| WorkerError::python("OverflowError", "date value out of range"))?;
    let sessions = recent_trading_days(conn, before, 1).await?;
    if let Some(last) = sessions.last() {
        let eps: Vec<(String, i64)> = sqlx::query_as(
            "SELECT instrument.symbol, instrument.id FROM instrument \
             JOIN sw_setup_daily ON sw_setup_daily.instrument_id = instrument.id \
             WHERE sw_setup_daily.user_id = $1 AND sw_setup_daily.date = $2 \
             AND sw_setup_daily.setup = 'EP'",
        )
        .bind(user_id)
        .bind(last)
        .fetch_all(&mut *conn)
        .await?;
        for (symbol, id) in eps {
            symbols.entry(symbol).or_insert(id);
        }
    }
    for (breadth, signal) in [
        ("vb_breadth_daily", "vb_signal_daily"),
        ("tw_breadth_daily", "tw_signal_daily"),
    ] {
        let day: Option<(NaiveDate,)> = sqlx::query_as(&format!(
            "SELECT date FROM {breadth} WHERE user_id = $1 ORDER BY date DESC LIMIT 1"
        ))
        .bind(user_id)
        .fetch_optional(&mut *conn)
        .await?;
        let Some((day,)) = day else {
            continue;
        };
        let rows: Vec<(String, i64)> = sqlx::query_as(&format!(
            "SELECT instrument.symbol, instrument.id FROM instrument \
             JOIN {signal} ON {signal}.instrument_id = instrument.id \
             WHERE {signal}.user_id = $1 AND {signal}.date = $2 AND {signal}.state = 'SIGNAL'"
        ))
        .bind(user_id)
        .bind(day)
        .fetch_all(&mut *conn)
        .await?;
        for (symbol, id) in rows {
            symbols.entry(symbol).or_insert(id);
        }
    }
    Ok(symbols)
}

/// `nearest_result`: the soonest result meeting on or after the session (`min` keeps the first
/// of equal dates).
fn nearest_result(
    records: &[EarningsDateRecord],
    on_or_after: NaiveDate,
) -> Option<&EarningsDateRecord> {
    let mut best: Option<&EarningsDateRecord> = None;
    for record in records {
        if record.event_date < on_or_after {
            continue;
        }
        if best.is_none_or(|b| record.event_date < b.event_date) {
            best = Some(record);
        }
    }
    best
}

/// A `published_at` as the column stores it: an aware stamp as given, a naive one as UTC (what
/// asyncpg does with a naive `datetime` for a `timestamptz`).
fn stamp(value: Option<&PyDateTime>) -> Option<DateTime<FixedOffset>> {
    match value? {
        PyDateTime::Aware(at) => Some(*at),
        PyDateTime::Naive(at) => Some(at.and_utc().fixed_offset()),
    }
}

/// One `sw_catalyst` row to upsert.
struct CatalystRow {
    headline: String,
    published_at: Option<DateTime<FixedOffset>>,
    url: String,
    source: &'static str,
    earnings_date: Option<NaiveDate>,
}

/// `upsert_catalysts`: one symbol's rows, idempotent on the URL (a URL twice in one answer keeps
/// its first row). With no result meeting ahead, an earlier calendar row is deleted.
async fn upsert_catalysts(
    conn: &mut PgConnection,
    user_id: i64,
    instrument_id: i64,
    announcements: &[CatalystRecord],
    result: Option<&EarningsDateRecord>,
) -> Result<i64, WorkerError> {
    let mut payload: Vec<CatalystRow> = announcements
        .iter()
        .map(|r| CatalystRow {
            headline: r.headline.clone(),
            published_at: stamp(r.published_at.as_ref()),
            url: r.url.clone(),
            source: CATALYST_SOURCE_ANNOUNCEMENT,
            earnings_date: None,
        })
        .collect();
    if let Some(meeting) = result {
        payload.push(CatalystRow {
            headline: meeting.purpose.clone(),
            published_at: None,
            url: meeting.url.clone(),
            source: CATALYST_SOURCE_EVENT_CALENDAR,
            earnings_date: Some(meeting.event_date),
        });
    }
    let mut by_url: Vec<CatalystRow> = Vec::new();
    for row in payload {
        if !by_url.iter().any(|r| r.url == row.url) {
            by_url.push(row);
        }
    }
    if result.is_none() {
        sqlx::query(
            "DELETE FROM sw_catalyst WHERE user_id = $1 AND instrument_id = $2 AND source = $3",
        )
        .bind(user_id)
        .bind(instrument_id)
        .bind(CATALYST_SOURCE_EVENT_CALENDAR)
        .execute(&mut *conn)
        .await?;
    }
    for row in &by_url {
        sqlx::query(
            "INSERT INTO sw_catalyst (user_id, instrument_id, headline, published_at, url, \
             source, earnings_date) VALUES ($1, $2, $3, $4, $5, $6, $7) \
             ON CONFLICT (user_id, instrument_id, url) DO UPDATE SET \
             headline = excluded.headline, published_at = excluded.published_at, \
             source = excluded.source, earnings_date = excluded.earnings_date",
        )
        .bind(user_id)
        .bind(instrument_id)
        .bind(&row.headline)
        .bind(row.published_at)
        .bind(&row.url)
        .bind(row.source)
        .bind(row.earnings_date)
        .execute(&mut *conn)
        .await?;
    }
    Ok(count(by_url.len()))
}

/// `fill_watch`: the newest headline into an empty `catalyst`; `earnings_date` set (or cleared)
/// on every WATCHING row of the name. Returns `(filled, flagged)`.
async fn fill_watch(
    conn: &mut PgConnection,
    user_id: i64,
    instrument_id: i64,
    newest: Option<&CatalystRecord>,
    result: Option<&EarningsDateRecord>,
) -> Result<(i64, i64), WorkerError> {
    let mut filled = 0;
    if let Some(newest) = newest {
        let rows: Vec<(i64,)> = sqlx::query_as(
            "UPDATE sw_watch SET catalyst = $1, updated_at = now() WHERE sw_watch.user_id = $2 \
             AND sw_watch.instrument_id = $3 AND sw_watch.state = $4 \
             AND (sw_watch.catalyst IS NULL OR sw_watch.catalyst = '') RETURNING sw_watch.id",
        )
        .bind(&newest.headline)
        .bind(user_id)
        .bind(instrument_id)
        .bind(WATCHING)
        .fetch_all(&mut *conn)
        .await?;
        filled = count(rows.len());
    }
    let flagged: Vec<(i64,)> = sqlx::query_as(
        "UPDATE sw_watch SET earnings_date = $1, updated_at = now() WHERE sw_watch.user_id = $2 \
         AND sw_watch.instrument_id = $3 AND sw_watch.state = $4 RETURNING sw_watch.id",
    )
    .bind(result.map(|r| r.event_date))
    .bind(user_id)
    .bind(instrument_id)
    .bind(WATCHING)
    .fetch_all(&mut *conn)
    .await?;
    Ok((
        filled,
        if result.is_some() {
            count(flagged.len())
        } else {
            0
        },
    ))
}

/// `run_swing_catalyst`: the 09:10 job. Never raises a provider error.
pub(super) async fn run_swing_catalyst(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    session_date: NaiveDate,
    user_id: i64,
    provider: Option<&NseProvider>,
) -> Result<CatalystReport, WorkerError> {
    let mut report = CatalystReport::new(session_date);
    let symbols = feed_symbols(conn, user_id, session_date).await?;
    report.symbols = count(symbols.len());
    let note_all = |outcome: &mut StepOutcome, report: &CatalystReport| {
        if let OrderedJson::Object(entries) = report.as_detail() {
            for (k, v) in entries {
                outcome.note(&k, v);
            }
        }
    };
    if symbols.is_empty() {
        report.skipped_reason = Some(
            "nothing watched, no EP candidate for the last session, \
             and no VBT or TWT signal at the latest session"
                .to_owned(),
        );
        note_all(outcome, &report);
        return Ok(report);
    }
    let Some(provider) = provider else {
        report.skipped_reason = Some("no catalyst provider configured".to_owned());
        note_all(outcome, &report);
        return Ok(report);
    };
    for (symbol, instrument_id) in &symbols {
        let one = [symbol.clone()];
        let read = async {
            let announcements = provider.announcements(&one, session_date).await?;
            let meetings = provider.results_calendar(&one, session_date).await?;
            Ok::<_, baskfy_providers::errors::ProviderError>((announcements, meetings))
        };
        let (announcements, meetings) = match read.await {
            Ok(answer) => answer,
            Err(e) => {
                log_warning(&format!("catalyst feed: {symbol} skipped: {e}"));
                report.failed.push(symbol.clone());
                report.provider_error = Some(e.to_string());
                continue;
            }
        };
        let result = nearest_result(&meetings, session_date);
        report.announcements += count(announcements.len());
        report.earnings_dates += i64::from(result.is_some());
        report.rows_written +=
            upsert_catalysts(conn, user_id, *instrument_id, &announcements, result).await?;
        let (filled, flagged) =
            fill_watch(conn, user_id, *instrument_id, announcements.first(), result).await?;
        report.catalysts_filled += filled;
        report.earnings_flagged += flagged;
    }
    outcome.rows_in = report.symbols;
    outcome.rows_out = report.rows_written;
    note_all(outcome, &report);
    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use baskfy_providers::records::EarningsDateFields;

    fn meeting(day: u32, purpose: &str) -> EarningsDateRecord {
        EarningsDateRecord::new(EarningsDateFields {
            symbol: "ABC".to_owned(),
            event_date: NaiveDate::from_ymd_opt(2026, 11, day).unwrap(),
            purpose: purpose.to_owned(),
            url: format!("https://x/{day}"),
        })
        .unwrap()
    }

    #[test]
    fn the_flag_is_the_soonest_meeting_on_or_after_the_session_and_past_ones_are_not() {
        let on = NaiveDate::from_ymd_opt(2026, 11, 5).unwrap();
        let records = vec![
            meeting(1, "past"),
            meeting(12, "later"),
            meeting(7, "first"),
            meeting(7, "second"),
        ];
        assert_eq!(nearest_result(&records, on).unwrap().purpose, "first");
        assert!(nearest_result(&records[..1], on).is_none());
        assert_eq!(
            nearest_result(&records, NaiveDate::from_ymd_opt(2026, 11, 1).unwrap())
                .unwrap()
                .purpose,
            "past"
        );
    }

    #[test]
    fn the_report_says_every_count_in_order() {
        let r = CatalystReport::new(NaiveDate::from_ymd_opt(2026, 10, 30).unwrap());
        let keys: Vec<String> = match r.as_detail() {
            OrderedJson::Object(e) => e.into_iter().map(|(k, _)| k).collect(),
            _ => Vec::new(),
        };
        assert_eq!(
            keys,
            [
                "session_date",
                "symbols",
                "announcements",
                "earnings_dates",
                "rows_written",
                "catalysts_filled",
                "earnings_flagged",
                "failed",
                "provider_error",
                "skipped_reason"
            ]
        );
    }
}
