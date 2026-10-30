//! `index_backfill` (M31): index levels from Kite's `INDICES` segment, matched to `index_def` by name.

use std::collections::{BTreeSet, HashMap};

use baskfy_db::WriteDb;
use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::records::InstrumentType;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{date_at, decimal_at, decimal4, described, float_of, thousands};
use super::deep::chunk_windows;
use crate::error::WorkerError;

/// `default_start`.
pub fn default_start() -> NaiveDate {
    NaiveDate::from_ymd_opt(2017, 1, 1).unwrap_or(NaiveDate::MIN)
}
const REPORTED_UNMATCHED: usize = 25;
const UPSERT_CHUNK: usize = 1500;

/// `ABBREVIATIONS`.
pub const ABBREVIATIONS: [(&str, &str); 14] = [
    ("SMLCAP", "SMALLCAP"),
    ("MIDSML", "MIDSMALL"),
    ("INFRA", "INFRASTRUCTURE"),
    ("FINSERVICE", "FINANCIALSERVICES"),
    ("SERVSECTOR", "CONSUMERSERVICES"),
    ("CONSUMPTION", "INDIACONSUMPTION"),
    ("DIVOPPS", "DIVIDENDOPPORTUNITIES"),
    ("PR2XLEV", "PR2XLEVERAGE"),
    ("PR1XINV", "PR1XINVERSE"),
    ("TR2XLEV", "TR2XLEVERAGE"),
    ("TR1XINV", "TR1XINVERSE"),
    ("CONSRDURBL", "CONSUMERDURABLES"),
    ("OILGAS", "OILANDGAS"),
    ("HEALTHCARE", "HEALTHCAREINDEX"),
];

/// `normalise`.
pub fn normalise(name: &str) -> String {
    name.to_uppercase()
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect()
}

/// `keys`.
pub fn keys(name: &str) -> Vec<String> {
    let flat = normalise(name);
    let mut expanded = flat.clone();
    for (short, long) in ABBREVIATIONS {
        expanded = expanded.replace(short, long);
    }
    if expanded == flat {
        vec![flat]
    } else {
        vec![flat, expanded]
    }
}

#[derive(Debug, Default)]
/// `Report`.
pub struct Report {
    /// `kite_indices`.
    pub kite_indices: usize,
    /// `matched`.
    pub matched: usize,
    /// `unmatched_ours`.
    pub unmatched_ours: Vec<String>,
    /// `unmatched_kite`.
    pub unmatched_kite: Vec<String>,
    /// `written`.
    pub written: usize,
    /// `errors`.
    pub errors: Vec<String>,
}

struct Row {
    date: NaiveDate,
    level: Decimal,
    change_abs: Option<Decimal>,
    change_pct: Option<Decimal>,
}

fn rows(candles: &[(NaiveDate, f64)]) -> Result<Vec<Row>, WorkerError> {
    let mut out = Vec::new();
    let mut previous: Option<f64> = None;
    for (date, close) in candles {
        let close = *close;
        if close <= 0.0 {
            previous = None;
            continue;
        }
        let change_abs = previous.map(|p| close - p);
        let change_pct = previous
            .filter(|p| *p != 0.0)
            .map(|p| (close / p - 1.0) * 100.0);
        out.push(Row {
            date: *date,
            level: decimal4(close)?,
            change_abs: change_abs.map(decimal4).transpose()?,
            change_pct: change_pct.map(decimal4).transpose()?,
        });
        previous = Some(close);
    }
    Ok(out)
}

async fn write(conn: &mut PgConnection, index_id: i16, rows: &[Row]) -> Result<usize, WorkerError> {
    let mut written = 0;
    for chunk in rows.chunks(UPSERT_CHUNK) {
        let dates: Vec<NaiveDate> = chunk.iter().map(|r| r.date).collect();
        let level: Vec<Decimal> = chunk.iter().map(|r| r.level).collect();
        let abs: Vec<Option<Decimal>> = chunk.iter().map(|r| r.change_abs).collect();
        let pct: Vec<Option<Decimal>> = chunk.iter().map(|r| r.change_pct).collect();
        sqlx::query(
            "INSERT INTO index_snapshot_daily (index_id, date, level, change_abs, change_pct) \
             SELECT $1, u.d, u.l, u.a, u.p \
             FROM UNNEST($2::date[], $3::numeric[], $4::numeric[], $5::numeric[]) \
             AS u(d, l, a, p) \
             ON CONFLICT (index_id, date) DO UPDATE SET level = excluded.level, \
             change_abs = excluded.change_abs, change_pct = excluded.change_pct",
        )
        .bind(index_id)
        .bind(&dates)
        .bind(&level)
        .bind(&abs)
        .bind(&pct)
        .execute(&mut *conn)
        .await?;
        written += chunk.len();
    }
    Ok(written)
}

async fn candles(
    provider: &CompositeProvider,
    token: i64,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<(NaiveDate, f64)>, WorkerError> {
    let mut out = Vec::new();
    for (lo, hi) in chunk_windows(start, end) {
        let frame = provider.daily_bars(token, lo, hi).await?;
        for i in 0..frame.height() {
            let date = date_at(&frame, "date", i)?
                .ok_or_else(|| WorkerError::python("TypeError", "a candle without a date"))?;
            let close = decimal_at(&frame, "close", i)?
                .map(float_of)
                .ok_or_else(|| {
                    WorkerError::python(
                        "TypeError",
                        "expected a number from the candle, got NoneType",
                    )
                })?;
            out.push((date, close));
        }
    }
    Ok(out)
}

/// `run`.
pub async fn run(
    db: &WriteDb,
    provider: &CompositeProvider,
    kite_indices: &[(String, i64)],
    write_rows: bool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Report, WorkerError> {
    let mut report = Report {
        kite_indices: kite_indices.len(),
        ..Report::default()
    };
    let mut by_name: HashMap<String, i64> = HashMap::new();
    for (name, token) in kite_indices {
        for key in keys(name) {
            by_name.entry(key).or_insert(*token);
        }
    }
    let ours: Vec<(i16, String, String)> = {
        let mut tx = db.begin().await?;
        let rows = sqlx::query_as("SELECT id, slug, name FROM index_def ORDER BY id")
            .fetch_all(&mut *tx)
            .await?;
        tx.rollback().await?;
        rows
    };
    let mut matched_names: BTreeSet<String> = BTreeSet::new();
    for (index_id, slug, name) in ours {
        let found = keys(&name)
            .into_iter()
            .chain(keys(&slug))
            .find_map(|k| by_name.get(&k).copied());
        let Some(token) = found else {
            report.unmatched_ours.push(format!("{slug} ({name})"));
            continue;
        };
        report.matched += 1;
        matched_names.extend(keys(&name));
        let fetched = match candles(provider, token, start, end).await {
            Ok(c) => c,
            Err(e) => {
                report.errors.push(format!("{slug}: {}", described(&e)));
                continue;
            }
        };
        let built = rows(&fetched)?;
        if built.is_empty() {
            continue;
        }
        if !write_rows {
            report.written += built.len();
            continue;
        }
        let mut tx = db.begin().await?;
        report.written += write(&mut tx, index_id, &built).await?;
        tx.commit().await?;
    }
    let mut unmatched: Vec<String> = kite_indices
        .iter()
        .filter(|(name, _)| !keys(name).iter().any(|k| matched_names.contains(k)))
        .map(|(name, _)| name.clone())
        .collect();
    unmatched.sort();
    report.unmatched_kite = unmatched;
    Ok(report)
}

/// `kite_indices`.
pub fn kite_indices(records: &[baskfy_providers::records::InstrumentRecord]) -> Vec<(String, i64)> {
    records
        .iter()
        .filter(|r| r.instrument_type == InstrumentType::Index)
        .filter_map(|r| r.kite_token.map(|t| (r.symbol.clone(), t)))
        .collect()
}

/// `render`.
pub fn render(report: &Report, write_rows: bool) -> String {
    let mut lines = vec![
        format!(
            "index and sector history from Kite{}",
            if write_rows {
                ""
            } else {
                "  [DRY RUN — nothing written]"
            }
        ),
        "=".repeat(62),
        format!("indices Kite carries        : {}", report.kite_indices),
        format!("matched to index_def        : {}", report.matched),
        format!(
            "rows {:<10}            : {}",
            if write_rows { "written" } else { "writable" },
            thousands(i64::try_from(report.written).unwrap_or(i64::MAX))
        ),
        format!(
            "ours with no Kite match     : {}",
            report.unmatched_ours.len()
        ),
        format!(
            "Kite's with no index_def    : {}",
            report.unmatched_kite.len()
        ),
    ];
    if !report.errors.is_empty() {
        lines.push(String::new());
        lines.push(format!("errors ({}):", report.errors.len()));
        lines.extend(report.errors.iter().take(10).map(|e| format!("  {e}")));
    }
    if !report.unmatched_ours.is_empty() {
        lines.push(String::new());
        lines.push("ours with no Kite match (no history available for these):".to_owned());
        lines.extend(
            report
                .unmatched_ours
                .iter()
                .take(REPORTED_UNMATCHED)
                .map(|n| format!("  {n}")),
        );
        if report.unmatched_ours.len() > REPORTED_UNMATCHED {
            lines.push(format!(
                "  ... and {} more",
                report.unmatched_ours.len() - REPORTED_UNMATCHED
            ));
        }
    }
    lines.join("\n")
}
