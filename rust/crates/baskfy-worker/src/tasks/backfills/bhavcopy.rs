//! `bhavcopy_backfill`: one bhavcopy per trading day into `ohlcv_daily`, one transaction per day.

use std::collections::{BTreeSet, HashSet};
use std::io::Write as _;

use baskfy_db::{OrderedJson, WriteDb};
use baskfy_providers::ProviderError;
use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::nse::NseProvider;
use chrono::NaiveDate;
use polars::prelude::{BooleanChunked, DataFrame, NewChunkedArray};

use super::common::{Canned, canned, described, iso, text_at, trading_days_in};
use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::ingest::backfill_summary;
use crate::window::DateWindow;

pub(super) const EQUITY_SERIES: [&str; 6] = ["EQ", "BE", "BZ", "SM", "ST", "SZ"];

pub(super) enum Source<'a> {
    Composite(&'a CompositeProvider),
    Nse(&'a NseProvider),
}

impl Source<'_> {
    async fn bhavcopy(&self, day: NaiveDate) -> Result<DataFrame, ProviderError> {
        match self {
            Self::Composite(p) => p.bhavcopy(day).await,
            Self::Nse(p) => p.bhavcopy(day).await,
        }
    }
}

#[derive(Debug, Default)]
pub(super) struct Report {
    pub(super) trading_days: usize,
    pub(super) days_written: i64,
    pub(super) bars_written: i64,
    pub(super) unmatched_symbols: BTreeSet<String>,
    pub(super) missing_days: Vec<NaiveDate>,
    pub(super) failures: Vec<(String, OrderedJson)>,
}

impl Report {
    pub(super) fn succeeded(&self) -> bool {
        self.failures.is_empty()
    }
}

async fn instrument_keys(db: &WriteDb) -> Result<HashSet<(String, Option<String>)>, WorkerError> {
    let mut tx = db.begin().await?;
    let rows: Vec<(String, Option<String>)> =
        sqlx::query_as("SELECT symbol, series FROM instrument WHERE exchange_id = $1")
            .bind(NSE_EXCHANGE_ID)
            .fetch_all(&mut *tx)
            .await?;
    tx.rollback().await?;
    Ok(rows.into_iter().collect())
}

fn narrowed(
    frame: &DataFrame,
    keys: &HashSet<(String, Option<String>)>,
    series: &[String],
) -> Result<(DataFrame, BTreeSet<String>), WorkerError> {
    let mut keep = Vec::with_capacity(frame.height());
    let mut unmatched = BTreeSet::new();
    for i in 0..frame.height() {
        let row_series = text_at(frame, "series", i)?;
        let wanted = row_series.as_ref().is_some_and(|s| series.contains(s));
        keep.push(wanted);
        if !wanted {
            continue;
        }
        let symbol = text_at(frame, "symbol", i)?;
        let matched = symbol
            .as_ref()
            .is_some_and(|s| keys.contains(&(s.clone(), row_series.clone())));
        if !matched {
            unmatched.insert(format!(
                "{}:{}",
                symbol.unwrap_or_else(|| "None".to_owned()),
                row_series.unwrap_or_default()
            ));
        }
    }
    let mask = BooleanChunked::from_iter_values("keep".into(), keep.into_iter());
    let kept = frame
        .filter(&mask)
        .map_err(|e| WorkerError::python("ComputeError", e.to_string()))?;
    Ok((kept, unmatched))
}

pub(super) async fn backfill_bars_from_bhavcopy(
    db: &WriteDb,
    source: &Source<'_>,
    window: DateWindow,
    series: &[String],
    progress_every: usize,
) -> Result<Report, WorkerError> {
    let mut report = Report::default();
    let days = {
        let mut tx = db.begin().await?;
        let days = trading_days_in(&mut tx, window).await?;
        tx.rollback().await?;
        days
    };
    let keys = instrument_keys(db).await?;
    report.trading_days = days.len();
    if keys.is_empty() {
        report.failures.push((
            "setup".to_owned(),
            pyjson::s(
                "no instrument rows: run `make refdata` (or reference_backfill) first so the \
                 listings register exists to join bars against",
            ),
        ));
        return Ok(report);
    }
    let total = days.len();
    for (index, day) in days.into_iter().enumerate() {
        match source.bhavcopy(day).await {
            Err(exc) => {
                report.missing_days.push(day);
                let message = exc.to_string();
                if !message.contains("404") {
                    pyjson::update(
                        &mut report.failures,
                        &iso(day),
                        pyjson::s(format!("{}: {message}", exc.python_type())),
                    );
                }
            }
            Ok(frame) => {
                if let Err(exc) = write_day(db, &mut report, &frame, &keys, series, day).await {
                    pyjson::update(&mut report.failures, &iso(day), pyjson::s(described(&exc)));
                }
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

async fn write_day(
    db: &WriteDb,
    report: &mut Report,
    frame: &DataFrame,
    keys: &HashSet<(String, Option<String>)>,
    series: &[String],
    day: NaiveDate,
) -> Result<(), WorkerError> {
    let (kept, unmatched) = narrowed(frame, keys, series)?;
    report.unmatched_symbols.extend(unmatched);
    let provider = canned(Canned {
        bhavcopy: Some(kept),
        snapshots: Vec::new(),
    })?;
    let mut tx = db.begin().await?;
    let summary = backfill_summary(&mut tx, &provider, DateWindow::single(day)).await?;
    if summary.failures.is_empty() {
        tx.commit().await?;
    } else {
        tx.rollback().await?;
        for (key, value) in summary.failures {
            pyjson::update(&mut report.failures, &key, value);
        }
        return Ok(());
    }
    if summary.bars_written > 0 {
        report.bars_written += summary.bars_written;
        report.days_written += 1;
    }
    Ok(())
}
