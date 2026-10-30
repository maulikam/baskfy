//! `breadth_backfill` (M32): sampled factor rows, carried `derived` membership and breadth rows, a transaction per day.

use std::sync::Arc;

use baskfy_db::WriteDb;
use baskfy_providers::composite::CompositeProvider;
use chrono::NaiveDate;
use sqlx::PgConnection;

use super::common::{described, thousands};
use crate::calendar::NSE_EXCHANGE_ID;
use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::steps::StepOutcome;
use crate::steps::compute::{compute_factors, compute_market_health};

const DERIVED: &str = "derived";
pub(super) const DEFAULT_EVERY: usize = 5;
const UPSERT_CHUNK: usize = 2000;

#[derive(Debug, Default)]
pub(super) struct Report {
    pub(super) dates: usize,
    pub(super) factors_computed: i64,
    pub(super) membership_rows: i64,
    pub(super) breadth_rows: i64,
    pub(super) skipped_have_factors: usize,
    pub(super) errors: Vec<String>,
}

async fn target_dates(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
    every: usize,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day \
         AND date BETWEEN $2 AND $3 ORDER BY date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .enumerate()
        .filter(|(i, _)| i % every.max(1) == 0)
        .map(|(_, (d,))| d)
        .collect())
}

async fn carry_membership(conn: &mut PgConnection, on: NaiveDate) -> Result<i64, WorkerError> {
    let source: Option<NaiveDate> = sqlx::query_scalar(
        "SELECT date FROM index_member_daily WHERE source <> $1 \
         ORDER BY abs(date - $2::date) LIMIT 1",
    )
    .bind(DERIVED)
    .bind(on)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(source) = source.filter(|d| *d != on) else {
        return Ok(0);
    };
    let rows: Vec<(i16, i64)> = sqlx::query_as(
        "SELECT index_id, instrument_id FROM index_member_daily WHERE date = $1 AND source <> $2",
    )
    .bind(source)
    .bind(DERIVED)
    .fetch_all(&mut *conn)
    .await?;
    let mut written = 0;
    for chunk in rows.chunks(UPSERT_CHUNK) {
        let index: Vec<i16> = chunk.iter().map(|r| r.0).collect();
        let instrument: Vec<i64> = chunk.iter().map(|r| r.1).collect();
        sqlx::query(
            "INSERT INTO index_member_daily (index_id, date, instrument_id, source) \
             SELECT u.x, $1, u.i, $2 FROM UNNEST($3::int2[], $4::int8[]) AS u(x, i) \
             ON CONFLICT (index_id, date, instrument_id) DO NOTHING",
        )
        .bind(on)
        .bind(DERIVED)
        .bind(&index)
        .bind(&instrument)
        .execute(&mut *conn)
        .await?;
        written += i64::try_from(chunk.len()).unwrap_or(i64::MAX);
    }
    Ok(written)
}

async fn has_factors(conn: &mut PgConnection, on: NaiveDate) -> Result<bool, WorkerError> {
    let found: Option<i32> =
        sqlx::query_scalar("SELECT 1 FROM factor_daily WHERE date = $1 LIMIT 1")
            .bind(on)
            .fetch_optional(&mut *conn)
            .await?;
    Ok(found.is_some())
}

async fn one_day(
    conn: &mut PgConnection,
    deps: &PipelineDependencies,
    report: &mut Report,
    day: NaiveDate,
) -> Result<(), WorkerError> {
    if has_factors(conn, day).await? {
        report.skipped_have_factors += 1;
    } else {
        let mut step = StepOutcome::default();
        compute_factors(conn, deps, &mut step, day).await?;
        report.factors_computed += step.rows_out;
    }
    report.membership_rows += carry_membership(conn, day).await?;
    let mut step = StepOutcome::default();
    compute_market_health(conn, &mut step, day).await?;
    report.breadth_rows += step.rows_out;
    Ok(())
}

pub(super) async fn run(
    db: &WriteDb,
    write: bool,
    start: NaiveDate,
    end: NaiveDate,
    every: usize,
) -> Result<Report, WorkerError> {
    let mut report = Report::default();
    let mut tx = db.begin().await?;
    let days = target_dates(&mut tx, start, end, every).await?;
    tx.rollback().await?;
    report.dates = days.len();
    if !write {
        return Ok(report);
    }
    let deps = PipelineDependencies::new(Arc::new(CompositeProvider::with_defaults(Vec::new())?));
    for day in days {
        let mut tx = db.begin().await?;
        let mut partial = Report::default();
        match one_day(&mut tx, &deps, &mut partial, day).await {
            Ok(()) => {
                tx.commit().await?;
                report.skipped_have_factors += partial.skipped_have_factors;
                report.factors_computed += partial.factors_computed;
                report.membership_rows += partial.membership_rows;
                report.breadth_rows += partial.breadth_rows;
            }
            Err(e) => {
                let _ = tx.rollback().await;
                report
                    .errors
                    .push(format!("{}: {}", day.format("%Y-%m-%d"), described(&e)));
            }
        }
    }
    Ok(report)
}

pub(super) fn render(report: &Report, write: bool) -> String {
    let mut lines = vec![
        format!(
            "breadth history{}",
            if write {
                ""
            } else {
                "  [DRY RUN — nothing written]"
            }
        ),
        "=".repeat(58),
        format!("target dates                : {}", report.dates),
    ];
    if write {
        lines.extend([
            format!(
                "factor rows computed        : {}",
                thousands(report.factors_computed)
            ),
            format!(
                "dates that already had them : {}",
                report.skipped_have_factors
            ),
            format!(
                "membership rows carried     : {}  (marked 'derived')",
                thousands(report.membership_rows)
            ),
            format!(
                "breadth rows written        : {}",
                thousands(report.breadth_rows)
            ),
        ]);
    } else {
        #[allow(clippy::cast_precision_loss)]
        let minutes = report.dates as f64 * 45.0 / 60.0;
        lines.push(format!(
            "estimated cost              : ~{} minutes (about 45s of factor computation per date)",
            minutes.round_ties_even()
        ));
    }
    if !report.errors.is_empty() {
        lines.push(String::new());
        lines.push(format!("errors ({}):", report.errors.len()));
        lines.extend(report.errors.iter().take(10).map(|e| format!("  {e}")));
    }
    lines.join("\n")
}
