//! `bhavcopy_backfill`: step 2's bhavcopy writer over a window, one transaction per trading day.

use std::collections::BTreeSet;
use std::io::Write as _;

use baskfy_db::{OrderedJson, WriteDb};
use baskfy_providers::composite::CompositeProvider;
use chrono::NaiveDate;

use super::common::{described, iso, trading_days_in};
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::ingest::bars::backfill_bars_from_bhavcopy as backfill_day;
use crate::window::DateWindow;

/// `EQUITY_SERIES`: main board and Emerge; never G-secs, T-bills or debt.
pub const EQUITY_SERIES: [&str; 6] = crate::steps::ingest::bars::EQUITY_SERIES;

/// `BhavcopyBackfillReport`.
#[derive(Debug, Default)]
pub struct Report {
    /// Trading days in the window.
    pub trading_days: usize,
    /// Days that wrote at least one bar.
    pub days_written: i64,
    /// Bars upserted.
    pub bars_written: i64,
    /// `symbol:series` keys with no `instrument` row.
    pub unmatched_symbols: BTreeSet<String>,
    /// Sessions with no bhavcopy (a 404 or any fetch failure).
    pub missing_days: Vec<NaiveDate>,
    /// Day (or `setup`) to its error, insertion-ordered.
    pub failures: Vec<(String, OrderedJson)>,
}

impl Report {
    /// `succeeded`: no failures (a missing day is not one).
    #[must_use]
    pub fn succeeded(&self) -> bool {
        self.failures.is_empty()
    }
}

/// `backfill_bars_from_bhavcopy(provider, window, series=, progress_every=)`.
///
/// # Errors
/// The database's.
pub async fn backfill_bars_from_bhavcopy(
    db: &WriteDb,
    provider: &CompositeProvider,
    window: DateWindow,
    series: &[&str],
    progress_every: usize,
) -> Result<Report, WorkerError> {
    let mut report = Report::default();
    let mut tx = db.begin().await?;
    let days = trading_days_in(&mut tx, window).await?;
    tx.rollback().await?;
    report.trading_days = days.len();
    let total = days.len();
    for (index, day) in days.into_iter().enumerate() {
        let mut tx = db.begin().await?;
        match backfill_day(&mut tx, provider, DateWindow::single(day), series).await {
            Ok(one) => {
                if one.failures.is_empty() {
                    tx.commit().await?;
                } else {
                    tx.rollback().await?;
                }
                let setup = one.failures.iter().any(|(k, _)| k == "setup");
                report.days_written += one.days_written;
                report.bars_written += one.bars_written;
                report.unmatched_symbols.extend(one.unmatched_symbols);
                report.missing_days.extend(one.missing_days);
                for (key, value) in one.failures {
                    pyjson::update(&mut report.failures, &key, value);
                }
                if setup {
                    return Ok(report);
                }
            }
            Err(e) => {
                let _ = tx.rollback().await;
                pyjson::update(&mut report.failures, &iso(day), pyjson::s(described(&e)));
            }
        }
        let done = index + 1;
        if progress_every > 0 && done % progress_every == 0 {
            let _ = writeln!(
                std::io::stdout(),
                "  {done}/{total} days · {} bars · {} missing",
                report.bars_written,
                report.missing_days.len()
            );
        }
    }
    Ok(report)
}
