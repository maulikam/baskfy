//! `deep_backfill`: Kite's adjusted history before our first bar, spliced at the seam, never into a compressed chunk.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_db::WriteDb;
use baskfy_providers::composite::CompositeProvider;
use chrono::{Duration, NaiveDate};
use sqlx::PgConnection;

use super::common::{date_at, decimal_at, decimal4, described, float_of, int_at, thousands};
use crate::error::WorkerError;

/// `kite_epoch`.
pub fn kite_epoch() -> NaiveDate {
    NaiveDate::from_ymd_opt(2000, 1, 3).unwrap_or(NaiveDate::MIN)
}
/// `default_start`.
pub fn default_start() -> NaiveDate {
    NaiveDate::from_ymd_opt(2011, 1, 1).unwrap_or(NaiveDate::MIN)
}
/// `MAX_SPAN_DAYS`.
pub const MAX_SPAN_DAYS: i64 = 2000;
const UPSERT_CHUNK: usize = 1500;
const SPLICE_EPSILON: f64 = 1e-9;

#[derive(Debug, Default)]
/// `Report`.
pub struct Report {
    /// `instruments`.
    pub instruments: usize,
    /// `fetched`.
    pub fetched: usize,
    /// `written`.
    pub written: usize,
    /// `skipped_no_overlap`.
    pub skipped_no_overlap: usize,
    /// `skipped_no_deep_history`.
    pub skipped_no_deep_history: usize,
    /// `skipped_compressed`.
    pub skipped_compressed: usize,
    /// `spliced`.
    pub spliced: usize,
    /// `compressed_ranges`.
    pub compressed_ranges: BTreeSet<String>,
    /// `errors`.
    pub errors: Vec<String>,
}

/// `chunk_windows`.
pub fn chunk_windows(start: NaiveDate, end: NaiveDate) -> Vec<(NaiveDate, NaiveDate)> {
    let mut out = Vec::new();
    let mut cursor = start.max(kite_epoch());
    while cursor <= end {
        let stop = (cursor + Duration::days(MAX_SPAN_DAYS - 1)).min(end);
        out.push((cursor, stop));
        cursor = stop + Duration::days(1);
    }
    out
}

/// `splice_factor`.
pub fn splice_factor(
    kite: &BTreeMap<NaiveDate, f64>,
    ours: &BTreeMap<NaiveDate, f64>,
) -> (Option<f64>, Option<NaiveDate>) {
    kite.iter()
        .filter_map(|(d, k)| ours.get(d).map(|o| (*d, *k, *o)))
        .find(|(_, k, o)| *k != 0.0 && *o != 0.0)
        .map_or((None, None), |(d, k, o)| (Some(o / k), Some(d)))
}

/// `Candle`.
pub type Candle = [f64; 5];

async fn existing(
    conn: &mut PgConnection,
    instrument_id: i64,
) -> Result<BTreeMap<NaiveDate, f64>, WorkerError> {
    let rows: Vec<(NaiveDate, Option<rust_decimal::Decimal>)> = sqlx::query_as(
        "SELECT date, close FROM ohlcv_daily WHERE instrument_id = $1 AND source <> 'kite' \
         ORDER BY date",
    )
    .bind(instrument_id)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .filter_map(|(d, c)| c.map(|c| (d, float_of(c))))
        .collect())
}

/// `compressed_ranges_covering`.
pub async fn compressed_ranges_covering(
    conn: &mut PgConnection,
    days: &[NaiveDate],
) -> Result<Vec<String>, WorkerError> {
    let (Some(lo), Some(hi)) = (days.iter().min(), days.iter().max()) else {
        return Ok(Vec::new());
    };
    let mut rows: Vec<(String,)> = sqlx::query_as(
        "SELECT DISTINCT range_start::date::text || '..' || range_end::date::text \
         FROM timescaledb_information.chunks WHERE hypertable_name = 'ohlcv_daily' \
         AND is_compressed AND range_start::date <= $2 AND range_end::date > $1",
    )
    .bind(lo)
    .bind(hi)
    .fetch_all(&mut *conn)
    .await?;
    rows.sort();
    Ok(rows.into_iter().map(|(r,)| r).collect())
}

/// `duplicate_pairs`.
pub async fn duplicate_pairs(conn: &mut PgConnection) -> Result<i64, WorkerError> {
    Ok(sqlx::query_scalar(
        "SELECT count(*) FROM (SELECT instrument_id, date FROM ohlcv_daily \
         GROUP BY instrument_id, date HAVING count(*) > 1) t",
    )
    .fetch_one(&mut *conn)
    .await?)
}

/// `write`.
pub async fn write(
    conn: &mut PgConnection,
    instrument_id: i64,
    bars: &[(NaiveDate, Candle)],
) -> Result<usize, WorkerError> {
    let mut written = 0;
    for chunk in bars.chunks(UPSERT_CHUNK) {
        let mut dates = Vec::with_capacity(chunk.len());
        let mut cols: [Vec<rust_decimal::Decimal>; 4] = Default::default();
        let mut volume = Vec::with_capacity(chunk.len());
        for (day, c) in chunk {
            dates.push(*day);
            for k in 0..4 {
                cols[k].push(decimal4(c[k])?);
            }
            #[allow(clippy::cast_possible_truncation)]
            volume.push(c[4].trunc() as i64);
        }
        sqlx::query(
            "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, \
             close_raw, volume_raw, adj_factor, source) \
             SELECT $1, u.d, u.o, u.h, u.l, u.c, u.v, u.c, u.v, 1, 'kite_adjusted' \
             FROM UNNEST($2::date[], $3::numeric[], $4::numeric[], $5::numeric[], \
             $6::numeric[], $7::int8[]) AS u(d, o, h, l, c, v) \
             ON CONFLICT (instrument_id, date) DO NOTHING",
        )
        .bind(instrument_id)
        .bind(&dates)
        .bind(&cols[0])
        .bind(&cols[1])
        .bind(&cols[2])
        .bind(&cols[3])
        .bind(&volume)
        .execute(&mut *conn)
        .await?;
        written += chunk.len();
    }
    Ok(written)
}

async fn fetch_candles(
    provider: &CompositeProvider,
    token: i64,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<BTreeMap<NaiveDate, Candle>, WorkerError> {
    let mut out = BTreeMap::new();
    for (lo, hi) in chunk_windows(start, end) {
        let frame = provider.daily_bars(token, lo, hi).await?;
        for i in 0..frame.height() {
            let Some(day) = date_at(&frame, "date", i)? else {
                continue;
            };
            let num = |name: &str| -> Result<f64, WorkerError> {
                decimal_at(&frame, name, i)?
                    .map(float_of)
                    .ok_or_else(|| WorkerError::python("TypeError", format!("{name} is None")))
            };
            #[allow(clippy::cast_precision_loss)]
            let volume = int_at(&frame, "volume", i)?.unwrap_or(0) as f64;
            out.insert(
                day,
                [
                    num("open")?,
                    num("high")?,
                    num("low")?,
                    num("close")?,
                    volume,
                ],
            );
        }
    }
    Ok(out)
}

/// `run`.
pub async fn run(
    db: &WriteDb,
    provider: &CompositeProvider,
    write_bars: bool,
    start: NaiveDate,
    end: NaiveDate,
    symbols: Option<&[String]>,
    limit: Option<i64>,
) -> Result<Report, WorkerError> {
    let mut report = Report::default();
    let mut sql = "SELECT id, symbol, kite_token FROM instrument WHERE kite_token IS NOT NULL \
                   AND series IN ('EQ','BE') AND delisted_on IS NULL"
        .to_owned();
    if symbols.is_some() {
        sql.push_str(" AND symbol = ANY($1)");
    }
    sql.push_str(" ORDER BY symbol");
    if let Some(n) = limit.filter(|n| *n != 0) {
        sql.push_str(&format!(" LIMIT {n}"));
    }
    let candidates: Vec<(i64, String, i64)> = {
        let mut tx = db.begin().await?;
        let query = sqlx::query_as(&sql);
        let query = match symbols {
            Some(list) => query.bind(list.to_vec()),
            None => query,
        };
        let rows = query.fetch_all(&mut *tx).await?;
        tx.rollback().await?;
        rows
    };

    for (instrument_id, symbol, token) in candidates {
        report.instruments += 1;
        let frames = match fetch_candles(provider, token, start, end).await {
            Ok(f) => f,
            Err(e) => {
                report.errors.push(format!("{symbol}: {}", described(&e)));
                continue;
            }
        };
        if frames.is_empty() {
            continue;
        }
        report.fetched += frames.len();
        let mut tx = db.begin().await?;
        let ours = existing(&mut tx, instrument_id).await?;
        tx.rollback().await?;
        let earliest = ours.keys().next().copied();
        let frames: BTreeMap<NaiveDate, Candle> = frames
            .into_iter()
            .filter(|(_, c)| c[3] > 0.0 && c[0] > 0.0)
            .collect();
        let deep: Vec<(NaiveDate, Candle)> = frames
            .iter()
            .filter(|(d, _)| earliest.is_none_or(|e| **d < e))
            .map(|(d, c)| (*d, *c))
            .collect();
        if deep.is_empty() {
            report.skipped_no_deep_history += 1;
            continue;
        }
        let closes: BTreeMap<NaiveDate, f64> = frames.iter().map(|(d, c)| (*d, c[3])).collect();
        let (factor, _joined) = splice_factor(&closes, &ours);
        if !ours.is_empty() && factor.is_none() {
            report.skipped_no_overlap += 1;
            continue;
        }
        let scale = factor.unwrap_or(1.0);
        if factor.is_some() && (scale - 1.0).abs() > SPLICE_EPSILON {
            report.spliced += 1;
        }
        let bars: Vec<(NaiveDate, Candle)> = deep
            .into_iter()
            .map(|(d, c)| {
                (
                    d,
                    [c[0] * scale, c[1] * scale, c[2] * scale, c[3] * scale, c[4]],
                )
            })
            .collect();
        let days: Vec<NaiveDate> = bars.iter().map(|(d, _)| *d).collect();
        let mut tx = db.begin().await?;
        let blocked = compressed_ranges_covering(&mut tx, &days).await?;
        tx.rollback().await?;
        if !blocked.is_empty() {
            report.skipped_compressed += 1;
            report.compressed_ranges.extend(blocked);
            continue;
        }
        if !write_bars {
            report.written += bars.len();
            continue;
        }
        let mut tx = db.begin().await?;
        report.written += write(&mut tx, instrument_id, &bars).await?;
        tx.commit().await?;
    }

    if write_bars {
        let mut tx = db.begin().await?;
        let duplicates = duplicate_pairs(&mut tx).await?;
        tx.rollback().await?;
        if duplicates > 0 {
            report.errors.push(format!(
                "{duplicates} duplicate (instrument_id, date) pair(s) exist after this run. \
                 The primary key did not stop them, which means a write reached a compressed \
                 chunk. Do not run again until they are removed."
            ));
        }
    }
    Ok(report)
}

fn as_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `render`.
pub fn render(report: &Report, write_bars: bool) -> String {
    let mut lines = vec![
        format!(
            "deep history from Kite{}",
            if write_bars {
                ""
            } else {
                "  [DRY RUN — nothing was written]"
            }
        ),
        "=".repeat(60),
        format!("instruments                 : {}", report.instruments),
        format!(
            "bars fetched                : {}",
            thousands(as_i64(report.fetched))
        ),
        format!(
            "bars {:<10}             : {}",
            if write_bars { "written" } else { "writable" },
            thousands(as_i64(report.written))
        ),
        format!("level-spliced at the seam   : {}", report.spliced),
        format!(
            "skipped, nothing deeper     : {}",
            report.skipped_no_deep_history
        ),
        format!(
            "skipped, no shared date     : {}",
            report.skipped_no_overlap
        ),
        format!(
            "REFUSED, compressed chunk   : {}",
            report.skipped_compressed
        ),
    ];
    if !report.compressed_ranges.is_empty() {
        lines.extend([
            String::new(),
            "  A compressed chunk cannot enforce the primary key, so writing into one creates"
                .to_owned(),
            "  duplicates that no ON CONFLICT can catch and no later delete can reach. Decompress"
                .to_owned(),
            "  these ranges, re-run, then recompress:".to_owned(),
        ]);
        lines.extend(report.compressed_ranges.iter().map(|r| format!("    {r}")));
    }
    if !report.errors.is_empty() {
        lines.push(String::new());
        lines.push(format!("errors ({}):", report.errors.len()));
        lines.extend(report.errors.iter().take(15).map(|e| format!("  {e}")));
    }
    lines.join("\n")
}
