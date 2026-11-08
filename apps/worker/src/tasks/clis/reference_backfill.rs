//! `baskfy_worker.reference_backfill`: listings once, then membership, snapshots and market
//! health for each trading day, each day in its own transaction.

#[cfg(test)]
mod db_tests;

use std::sync::Arc;

use baskfy_db::WriteDb;
use baskfy_providers::composite::CompositeProvider;
use chrono::NaiveDate;

use super::cli::{Failure, Outcome, Parsed, Spec, code, connect, parse_date, say, today_ist};
use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::steps::StepOutcome;
use crate::steps::compute::market_health;
use crate::steps::index::membership::refresh_membership;
use crate::steps::index::run_refresh_index_snapshots;
use crate::steps::ingest::instruments::run_refresh_listings;
use crate::window::DateWindow;

/// `ReferenceBackfillReport`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Report {
    pub trading_days: usize,
    pub listings_written: usize,
    pub membership_rows: i64,
    pub snapshot_rows: i64,
    pub market_health_rows: i64,
    pub failures: Vec<(String, String)>,
}

impl Report {
    pub(crate) fn succeeded(&self) -> bool {
        self.failures.is_empty()
    }
}

/// `trading_days_in(session, window)`.
pub(crate) async fn trading_days_in(
    db: &WriteDb,
    window: DateWindow,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let mut tx = db.begin().await?;
    let days = super::factors::ranking::trading_days_between(&mut tx, window.start(), window.end())
        .await?;
    tx.commit().await?;
    Ok(days)
}

async fn one_day(
    db: &WriteDb,
    deps: &PipelineDependencies,
    day: NaiveDate,
    allow_reconstruction: bool,
) -> Result<(i64, i64, i64), WorkerError> {
    let mut tx = db.begin().await?;
    let membership =
        refresh_membership(&mut tx, deps.provider.as_ref(), day, allow_reconstruction).await?;
    let snapshots =
        run_refresh_index_snapshots(&mut tx, deps, &mut StepOutcome::default(), day).await?;
    let health = market_health::run(&mut tx, &mut StepOutcome::default(), day).await?;
    tx.commit().await?;
    Ok((membership.rows_written, snapshots, health))
}

/// `backfill_reference_data(provider, window, include_listings=..., allow_reconstruction=...)`.
pub(crate) async fn backfill_reference_data(
    db: &WriteDb,
    provider: Arc<CompositeProvider>,
    window: DateWindow,
    include_listings: bool,
    allow_reconstruction: bool,
) -> Result<Report, WorkerError> {
    let mut report = Report::default();
    let deps = PipelineDependencies::new(provider);
    if include_listings {
        let mut tx = db.begin().await?;
        report.listings_written =
            run_refresh_listings(&mut tx, &deps.provider, &mut StepOutcome::default()).await?;
        tx.commit().await?;
    }
    let days = trading_days_in(db, window).await?;
    report.trading_days = days.len();
    for day in days {
        match one_day(db, &deps, day, allow_reconstruction).await {
            Ok((membership, snapshots, health)) => {
                report.membership_rows += membership;
                report.snapshot_rows += snapshots;
                report.market_health_rows += health;
            }
            Err(e) => report.failures.push((
                day.format("%Y-%m-%d").to_string(),
                format!("{}: {e}", e.python_type()),
            )),
        }
    }
    Ok(report)
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let parsed = Parsed::parse(
        args,
        Spec {
            valued: &["--from", "--to", "--database-url"],
            switches: &["--skip-listings", "--no-reconstruct"],
            ..Spec::default()
        },
    )?;
    let start = parse_date(parsed.required("--from")?)?;
    let end = match parsed.get("--to") {
        Some(t) => parse_date(t)?,
        None => today_ist(),
    };
    let window = DateWindow::new(start, end)?;
    let provider = crate::tasks::context::provider(None)
        .await
        .map_err(|e| Failure::Raised(WorkerError::Settings(e)))?;
    let db = connect(parsed.get("--database-url"))?;
    let outcome = backfill_reference_data(
        &db,
        Arc::new(provider),
        window,
        !parsed.on("--skip-listings"),
        !parsed.on("--no-reconstruct"),
    )
    .await;
    db.close().await;
    let report = outcome?;
    say(&format!(
        "{} trading day(s) in {window}; listings {}; membership {}; snapshots {}; market health {}",
        report.trading_days,
        report.listings_written,
        report.membership_rows,
        report.snapshot_rows,
        report.market_health_rows
    ));
    for (day, error) in report.failures.iter().take(20) {
        say(&format!("  {day}: {error}"));
    }
    Ok(code(report.succeeded()))
}
