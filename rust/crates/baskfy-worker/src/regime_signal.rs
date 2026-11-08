//! Per-stock regime signal I/O (Maulik, 7 Nov 2026; docs/swing/REGIME-SIGNAL.md): reads
//! `index_constituents` (membership, industry), `sector_index_map`, `index_stats_daily` (EMA
//! columns) and the scanners' rows; writes `stock_regime_signal` (0062). The rule is
//! `baskfy_core::regime::signal`. **Law 2:** signal rows and plan-line filtering only — nothing
//! here builds, confirms or sends an order.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::regime::RegimeMode;
use baskfy_core::regime::signal::{
    FALLBACK_INDEX, Momentum, StockSignal, sector_indices, stock_signal,
};
use baskfy_core::swing::plan::{SkipReason, Skipped, WatchItem};
use chrono::{Days, NaiveDate};
use serde_json::{Value, json};
use sqlx::PgConnection;

use crate::error::WorkerError;

/// A stats row older than this many days before the date is not read.
const MAX_STATS_AGE_DAYS: u64 = 7;

async fn table_exists(conn: &mut PgConnection, name: &str) -> Result<bool, WorkerError> {
    let (ok,): (bool,) = sqlx::query_as("SELECT to_regclass($1) IS NOT NULL")
        .bind(name)
        .fetch_one(&mut *conn)
        .await?;
    Ok(ok)
}

/// Is 0062 there?
///
/// # Errors
/// The database's.
pub async fn tables_ready(conn: &mut PgConnection) -> Result<bool, WorkerError> {
    Ok(table_exists(conn, "stock_regime_signal").await?
        && table_exists(conn, "sector_index_map").await?)
}

/// Is the per-stock gate in force: mode `per_stock`, 0062 migrated and EMA rows present.
/// Otherwise the swing gate is the legacy one.
///
/// # Errors
/// The database's.
pub async fn per_stock_active(conn: &mut PgConnection) -> Result<bool, WorkerError> {
    if crate::regime::config().mode != RegimeMode::PerStock || !tables_ready(conn).await? {
        return Ok(false);
    }
    let (any,): (bool,) = sqlx::query_as(
        "SELECT EXISTS (SELECT 1 FROM index_stats_daily WHERE ema_cross IS NOT NULL)",
    )
    .fetch_one(&mut *conn)
    .await?;
    Ok(any)
}

/// Each index's EMA pair on its latest stats row on or before `on` (within a week).
async fn load_momentum(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<BTreeMap<String, Momentum>, WorkerError> {
    let oldest = on
        .checked_sub_days(Days::new(MAX_STATS_AGE_DAYS))
        .unwrap_or(on);
    let rows: Vec<(String, f64, f64, bool)> = sqlx::query_as(
        "SELECT DISTINCT ON (s.index_id) m.name, s.ema10::float8, s.ema20::float8, s.ema_cross \
         FROM index_stats_daily s JOIN index_master m ON m.id = s.index_id \
         WHERE s.date <= $1 AND s.date >= $2 AND s.ema10 IS NOT NULL AND s.ema20 IS NOT NULL \
         AND s.ema_cross IS NOT NULL ORDER BY s.index_id, s.date DESC",
    )
    .bind(on)
    .bind(oldest)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(name, ema10, ema20, up)| (name, Momentum { ema10, ema20, up }))
        .collect())
}

/// Symbols in the scanners' latest rows on or before `on`, plus every index constituent.
async fn scan_symbols(
    conn: &mut PgConnection,
    on: NaiveDate,
    user_id: Option<i64>,
) -> Result<BTreeSet<String>, WorkerError> {
    let mut out: BTreeSet<String> =
        sqlx::query_scalar::<_, String>("SELECT DISTINCT symbol FROM index_constituents")
            .fetch_all(&mut *conn)
            .await?
            .into_iter()
            .collect();
    if let Some(uid) = user_id {
        for table in ["sw_setup_daily", "tw_signal_daily", "vb_signal_daily"] {
            if !table_exists(conn, table).await? {
                continue;
            }
            let sql = format!(
                "SELECT DISTINCT i.symbol FROM {table} t JOIN instrument i ON i.id = t.instrument_id \
                 WHERE t.user_id = $1 AND t.date = \
                 (SELECT max(date) FROM {table} WHERE user_id = $1 AND date <= $2)"
            );
            let rows: Vec<String> = sqlx::query_scalar(&sql)
                .bind(uid)
                .bind(on)
                .fetch_all(&mut *conn)
                .await?;
            out.extend(rows);
        }
    }
    Ok(out)
}

fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

/// `stock_regime_signal.evidence`.
#[must_use]
pub fn evidence(s: &StockSignal) -> Value {
    json!({
        "indices": s.indices.iter().map(|c| json!({
            "name": c.name, "via": c.via.as_str(),
            "ema10": round2(c.ema10), "ema20": round2(c.ema20), "up": c.up,
        })).collect::<Vec<_>>(),
        "triggered": s.triggered,
        "fallback_reason": s.fallback_reason,
    })
}

/// What a run wrote.
#[derive(Debug, Clone, Default)]
pub struct Report {
    /// The date.
    pub as_of: Option<NaiveDate>,
    /// Why nothing was written.
    pub skipped: Option<String>,
    /// Rows.
    pub symbols: usize,
    /// GREEN rows.
    pub green: usize,
    /// Fallback rows.
    pub fallback: usize,
    /// Indices with an EMA reading.
    pub indices_read: usize,
}

impl Report {
    /// As JSON.
    #[must_use]
    pub fn as_json(&self) -> Value {
        json!({
            "as_of": self.as_of.map(|d| d.to_string()), "skipped": self.skipped,
            "symbols": self.symbols, "green": self.green, "red": self.symbols - self.green,
            "fallback": self.fallback, "indices_read": self.indices_read,
        })
    }
}

/// Compute and replace `on`'s rows for every scanned symbol and every index constituent.
///
/// # Errors
/// The database's.
pub async fn compute(
    conn: &mut PgConnection,
    on: NaiveDate,
    user_id: Option<i64>,
) -> Result<Report, WorkerError> {
    let mut report = Report {
        as_of: Some(on),
        ..Report::default()
    };
    if !tables_ready(conn).await? || !crate::regime::index_tables_ready(conn).await? {
        report.skipped = Some("stock_regime_signal (0062) is not migrated".to_owned());
        return Ok(report);
    }
    let momentum = load_momentum(conn, on).await?;
    report.indices_read = momentum.len();
    if momentum.is_empty() {
        report.skipped = Some("no index EMA rows on or before the date".to_owned());
        return Ok(report);
    }
    let smallcap_up = momentum.get(FALLBACK_INDEX).is_some_and(|m| m.up);
    let mut members: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT c.symbol, m.name FROM index_constituents c JOIN index_master m ON m.id = c.index_id \
         ORDER BY c.symbol, m.sort_order, m.name",
    )
    .fetch_all(&mut *conn)
    .await?;
    for (symbol, name) in rows {
        members.entry(symbol).or_default().push(name);
    }
    let industry: BTreeMap<String, String> = sqlx::query_as::<_, (String, String)>(
        "SELECT DISTINCT ON (symbol) symbol, industry FROM index_constituents \
         WHERE industry IS NOT NULL AND industry <> '' ORDER BY symbol, as_of DESC, index_id",
    )
    .fetch_all(&mut *conn)
    .await?
    .into_iter()
    .collect();
    let mut sector_map: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT industry, index_name FROM sector_index_map ORDER BY industry, index_name",
    )
    .fetch_all(&mut *conn)
    .await?;
    for (ind, name) in rows {
        sector_map.entry(ind).or_default().push(name);
    }
    // Market-cap rank among all listed companies (latest fundamentals on or before the date).
    let ranks: BTreeMap<String, i64> = sqlx::query_as::<_, (String, i64)>(
        "WITH f AS (SELECT DISTINCT ON (instrument_id) instrument_id, marketcap_cr \
           FROM fundamental_daily WHERE date <= $1 AND date >= $1 - 30 AND marketcap_cr > 0 \
           ORDER BY instrument_id, date DESC) \
         SELECT DISTINCT ON (i.symbol) i.symbol, rank() OVER (ORDER BY f.marketcap_cr DESC) \
         FROM f JOIN instrument i ON i.id = f.instrument_id ORDER BY i.symbol",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await?
    .into_iter()
    .collect();
    let symbols = scan_symbols(conn, on, user_id).await?;
    let empty: Vec<String> = Vec::new();
    let (mut syms, mut sigs, mut fbs, mut evs) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    for symbol in symbols {
        let member_of = members.get(&symbol).unwrap_or(&empty);
        let sector = sector_indices(industry.get(&symbol).map(String::as_str), &sector_map);
        let size = ranks
            .get(&symbol)
            .and_then(|r| u32::try_from(*r).ok())
            .map(baskfy_core::regime::signal::size_index);
        let s = stock_signal(member_of, sector, size, &momentum, smallcap_up);
        report.green += usize::from(s.green);
        report.fallback += usize::from(s.fallback);
        sigs.push(s.signal().to_owned());
        fbs.push(s.fallback);
        evs.push(evidence(&s).to_string());
        syms.push(symbol);
    }
    report.symbols = syms.len();
    sqlx::query("DELETE FROM stock_regime_signal WHERE as_of = $1")
        .bind(on)
        .execute(&mut *conn)
        .await?;
    sqlx::query(
        "INSERT INTO stock_regime_signal (as_of, symbol, signal, fallback, evidence, computed_at) \
         SELECT $1, t.symbol, t.signal, t.fallback, CAST(t.evidence AS JSONB), now() \
         FROM UNNEST($2::text[], $3::text[], $4::bool[], $5::text[]) \
         AS t(symbol, signal, fallback, evidence)",
    )
    .bind(on)
    .bind(&syms)
    .bind(&sigs)
    .bind(&fbs)
    .bind(&evs)
    .execute(&mut *conn)
    .await?;
    Ok(report)
}

/// symbol → GREEN on the latest signal date on or before `on` (within a week); `None` when
/// there are no rows (the caller keeps every name).
///
/// # Errors
/// The database's.
pub async fn signals_on(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<Option<BTreeMap<String, bool>>, WorkerError> {
    let oldest = on
        .checked_sub_days(Days::new(MAX_STATS_AGE_DAYS))
        .unwrap_or(on);
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT symbol, signal FROM stock_regime_signal WHERE as_of = \
         (SELECT max(as_of) FROM stock_regime_signal WHERE as_of <= $1 AND as_of >= $2)",
    )
    .bind(on)
    .bind(oldest)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        return Ok(None);
    }
    Ok(Some(
        rows.into_iter().map(|(s, g)| (s, g == "GREEN")).collect(),
    ))
}

/// The swing book's per-stock gate on the watchlist: in `per_stock` mode, RED names (and names
/// with no signal row, which follow the Smallcap rule) leave the list as `GATE_RED` skips.
/// Outside the mode, or with no signal rows, the list is unchanged.
///
/// # Errors
/// The database's.
pub async fn split_watch(
    conn: &mut PgConnection,
    on: NaiveDate,
    items: Vec<WatchItem>,
) -> Result<(Vec<WatchItem>, Vec<Skipped>), WorkerError> {
    if items.is_empty() || !per_stock_active(conn).await? {
        return Ok((items, Vec::new()));
    }
    let Some(signals) = signals_on(conn, on).await? else {
        return Ok((items, Vec::new()));
    };
    let smallcap_up = smallcap_up_on(conn, on).await?;
    let (mut keep, mut red) = (Vec::new(), Vec::new());
    for item in items {
        let green = signals.get(&item.symbol).copied().unwrap_or(smallcap_up);
        if green {
            keep.push(item);
        } else {
            red.push(Skipped {
                symbol: item.symbol.clone(),
                reason: SkipReason::GateRed,
                detail: format!("per-stock regime RED for {}", item.symbol),
            });
        }
    }
    Ok((keep, red))
}

/// The Smallcap-250 EMA cross on `on` (latest within a week); false when unread.
async fn smallcap_up_on(conn: &mut PgConnection, on: NaiveDate) -> Result<bool, WorkerError> {
    Ok(load_momentum(conn, on)
        .await?
        .get(FALLBACK_INDEX)
        .is_some_and(|m| m.up))
}
