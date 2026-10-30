//! `index_repair` (SW16): re-fetch a window of index levels from NSE through step 6's guarded writer.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use baskfy_db::{OrderedJson, WriteDb};
use baskfy_providers::nse::NseProvider;
use baskfy_providers::records::IndexSnapshot;
use chrono::{Days, NaiveDate};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{Canned, canned, described, iso, slugify_index, trading_days_in};
use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::steps::StepOutcome;
use crate::steps::index::refresh_index_snapshots;
use crate::window::DateWindow;

const GUARD_LOOKBACK_DAYS: u64 = 14;

fn reported_slugs() -> BTreeSet<String> {
    baskfy_providers::nse::INDEX_SLUG_TO_NSE_NAME
        .iter()
        .map(|(slug, _)| (*slug).to_owned())
        .collect()
}

#[derive(Debug, Clone)]
pub(super) struct DayChange {
    day: NaiveDate,
    slug: String,
    before: Option<Decimal>,
    after: Option<Decimal>,
}

fn show(level: Option<Decimal>) -> String {
    level.map_or_else(|| "None".to_owned(), |d| d.to_string())
}

impl DayChange {
    fn line(&self) -> String {
        let mark = if self.before == self.after { "=" } else { "->" };
        format!(
            "{} {}: {} {mark} {}",
            iso(self.day),
            self.slug,
            show(self.before),
            show(self.after)
        )
    }
}

#[derive(Debug)]
pub(super) struct Report {
    window: DateWindow,
    trading_days: usize,
    days_done: usize,
    rows_written: i64,
    changes: Vec<DayChange>,
    refused: Vec<String>,
    failures: Vec<(String, String)>,
    anchors: BTreeMap<String, String>,
}

impl Report {
    pub(super) fn succeeded(&self) -> bool {
        self.failures.is_empty() && self.trading_days > 0
    }

    fn rewritten(&self) -> usize {
        self.changes.iter().filter(|c| c.before != c.after).count()
    }
}

async fn levels(
    conn: &mut PgConnection,
    day: NaiveDate,
    slugs: &[String],
) -> Result<BTreeMap<String, Option<Decimal>>, WorkerError> {
    let rows: Vec<(String, Option<Decimal>)> = sqlx::query_as(
        "SELECT d.slug, s.level FROM index_snapshot_daily s JOIN index_def d ON d.id = s.index_id \
         WHERE s.date = $1 AND d.slug = ANY($2)",
    )
    .bind(day)
    .bind(slugs)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().collect())
}

async fn anchors(
    conn: &mut PgConnection,
    start: NaiveDate,
    slugs: &[String],
) -> Result<BTreeMap<String, String>, WorkerError> {
    let floor = start
        .checked_sub_days(Days::new(GUARD_LOOKBACK_DAYS))
        .unwrap_or(NaiveDate::MIN);
    let rows: Vec<(String, NaiveDate, Option<Decimal>)> = sqlx::query_as(
        "SELECT d.slug, s.date, s.level FROM index_snapshot_daily s \
         JOIN index_def d ON d.id = s.index_id \
         WHERE d.slug = ANY($1) AND s.date < $2 AND s.date >= $3 AND s.level IS NOT NULL \
         ORDER BY d.slug, s.date",
    )
    .bind(slugs)
    .bind(start)
    .bind(floor)
    .fetch_all(&mut *conn)
    .await?;
    let mut out = BTreeMap::new();
    for (slug, day, level) in rows {
        if let Some(level) = level {
            out.insert(slug, format!("{level} on {}", iso(day)));
        }
    }
    Ok(out)
}

fn select(snapshots: Vec<IndexSnapshot>, wanted: Option<&BTreeSet<String>>) -> Vec<IndexSnapshot> {
    match wanted {
        None => snapshots,
        Some(w) => snapshots
            .into_iter()
            .filter(|s| w.contains(&s.index_slug) || w.contains(&slugify_index(&s.index_slug)))
            .collect(),
    }
}

fn refused_of(step: &StepOutcome) -> Vec<String> {
    step.detail
        .iter()
        .find(|(k, _)| k == "refused")
        .map(|(_, v)| match v {
            OrderedJson::Array(items) => items
                .iter()
                .filter_map(|i| match i {
                    OrderedJson::Str(s) => Some(s.clone()),
                    _ => None,
                })
                .collect(),
            _ => Vec::new(),
        })
        .unwrap_or_default()
}

struct DayResult {
    rows_written: i64,
    refused: Vec<String>,
    before: BTreeMap<String, Option<Decimal>>,
    after: BTreeMap<String, Option<Decimal>>,
}

async fn repair_day(
    db: &WriteDb,
    day: NaiveDate,
    snapshots: Vec<IndexSnapshot>,
    reported: &[String],
    dry_run: bool,
) -> Result<DayResult, WorkerError> {
    let deps = PipelineDependencies::new(Arc::new(canned(Canned {
        bhavcopy: None,
        snapshots,
    })?));
    let mut tx = db.begin().await?;
    let attempt = async {
        let before = levels(&mut tx, day, reported).await?;
        let mut step = StepOutcome::default();
        refresh_index_snapshots(&mut tx, &deps, &mut step, day).await?;
        let after = levels(&mut tx, day, reported).await?;
        Ok::<_, WorkerError>(DayResult {
            rows_written: step.rows_out,
            refused: refused_of(&step),
            before,
            after,
        })
    }
    .await;
    match attempt {
        Ok(result) if !dry_run => {
            tx.commit().await?;
            Ok(result)
        }
        other => {
            tx.rollback().await?;
            other
        }
    }
}

pub(super) async fn repair_index_snapshots(
    db: &WriteDb,
    provider: &NseProvider,
    window: DateWindow,
    slugs: Option<&[String]>,
    dry_run: bool,
) -> Result<Report, WorkerError> {
    let wanted: Option<BTreeSet<String>> = slugs.map(|s| s.iter().cloned().collect());
    let reported: Vec<String> = wanted
        .clone()
        .unwrap_or_else(reported_slugs)
        .into_iter()
        .collect();
    let mut tx = db.begin().await?;
    let days = trading_days_in(&mut tx, window).await?;
    let anchors = anchors(&mut tx, window.start(), &reported).await?;
    tx.rollback().await?;
    let mut report = Report {
        window,
        trading_days: days.len(),
        days_done: 0,
        rows_written: 0,
        changes: Vec::new(),
        refused: Vec::new(),
        failures: Vec::new(),
        anchors,
    };
    for day in days {
        let snapshots = match provider.index_snapshots(day).await {
            Ok(s) => select(s, wanted.as_ref()),
            Err(e) if e.is_provider_error() => {
                report
                    .failures
                    .push((iso(day), format!("{}: {e}", e.python_type())));
                continue;
            }
            Err(e) => return Err(e.into()),
        };
        if snapshots.is_empty() {
            report.failures.push((
                iso(day),
                "NSE published no row for the requested slugs".to_owned(),
            ));
            continue;
        }
        let result = match repair_day(db, day, snapshots, &reported, dry_run).await {
            Ok(r) => r,
            Err(e) => {
                report.failures.push((iso(day), described(&e)));
                continue;
            }
        };
        report.days_done += 1;
        report.rows_written += result.rows_written;
        report.refused.extend(result.refused);
        for slug in &reported {
            if result.before.contains_key(slug) || result.after.contains_key(slug) {
                report.changes.push(DayChange {
                    day,
                    slug: slug.clone(),
                    before: result.before.get(slug).copied().flatten(),
                    after: result.after.get(slug).copied().flatten(),
                });
            }
        }
    }
    Ok(report)
}

pub(super) fn format_report(report: &Report) -> String {
    let mut lines = vec![format!(
        "{} trading day(s) in {}; {} repaired; {} row(s) written; {} reported level(s) changed; \
         {} refused; {} day(s) failed",
        report.trading_days,
        report.window,
        report.days_done,
        report.rows_written,
        report.rewritten(),
        report.refused.len(),
        report.failures.len()
    )];
    lines.extend(
        report
            .anchors
            .iter()
            .map(|(s, a)| format!("anchor {s}: {a}")),
    );
    lines.extend(report.changes.iter().map(DayChange::line));
    lines.extend(report.refused.iter().map(|m| format!("REFUSED {m}")));
    lines.extend(
        report
            .failures
            .iter()
            .map(|(d, e)| format!("FAILED {d}: {e}")),
    );
    lines.join("\n")
}
