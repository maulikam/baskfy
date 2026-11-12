//! GTT-TRAIL's rows: `public.gtt_trail_policy`, `public.gtt_trail_journal` (db/migrations/0014),
//! the sleeves' open positions and the published daily bars. Postgres only.

use std::collections::HashMap;

use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;
use rust_decimal::prelude::FromPrimitive;

use super::policy::{Bar, Policy, STRATEGIES};
use crate::storage::{Conn, DbValue, Param, Row, StorageError};

type R<T> = Result<T, StorageError>;

fn f(row: &Row, k: &str) -> Option<f64> {
    match row.get(k) {
        Some(DbValue::Text(s)) => s.parse().ok(),
        Some(v) => v.as_f64(),
        None => None,
    }
}

fn t(row: &Row, k: &str) -> Option<String> {
    match row.get(k) {
        Some(DbValue::Null) | None => None,
        Some(DbValue::Int(i)) => Some(i.to_string()),
        Some(v) => v.as_str().map(str::to_owned),
    }
}

fn d(row: &Row, k: &str) -> Option<NaiveDate> {
    t(row, k).and_then(|s| NaiveDate::parse_from_str(s.get(..10)?, "%Y-%m-%d").ok())
}

fn b(row: &Row, k: &str) -> bool {
    matches!(
        row.get(k),
        Some(DbValue::Bool(true)) | Some(DbValue::Int(1))
    )
}

/// Two places, as `numeric` (round at write time).
#[must_use]
pub fn dec2(x: f64) -> Param {
    Decimal::from_f64(x).map_or(Param::Null, |v| Param::Decimal(v.round_dp(2)))
}

fn opt2(x: Option<f64>) -> Param {
    x.map_or(Param::Null, dec2)
}

/// Every strategy's policy; a missing row is the default (off).
///
/// # Errors
/// The database (the table is missing before migration 0014).
pub async fn policies(conn: &mut Conn) -> R<Vec<Policy>> {
    let cur = conn
        .execute(
            "SELECT strategy, enabled, breakeven_after_pct, breakeven_buffer_pct, \
             trail_after_pct, trail_pct, min_step_pct FROM public.gtt_trail_policy",
            &[],
        )
        .await?;
    let mut out: Vec<Policy> = STRATEGIES.iter().map(|s| Policy::default_for(s)).collect();
    for row in &cur.rows {
        let Some(s) = t(row, "strategy") else {
            continue;
        };
        let Some(slot) = out.iter_mut().find(|p| p.strategy == s) else {
            continue;
        };
        let dflt = Policy::default_for(&s);
        *slot = Policy {
            strategy: s,
            enabled: b(row, "enabled"),
            breakeven_after_pct: f(row, "breakeven_after_pct").unwrap_or(dflt.breakeven_after_pct),
            breakeven_buffer_pct: f(row, "breakeven_buffer_pct")
                .unwrap_or(dflt.breakeven_buffer_pct),
            trail_after_pct: f(row, "trail_after_pct").unwrap_or(dflt.trail_after_pct),
            trail_pct: f(row, "trail_pct").unwrap_or(dflt.trail_pct),
            min_step_pct: f(row, "min_step_pct").unwrap_or(dflt.min_step_pct),
        };
    }
    Ok(out)
}

/// Upsert one validated policy.
///
/// # Errors
/// The database.
pub async fn save_policy(
    conn: &mut Conn,
    p: &Policy,
    now: DateTime<FixedOffset>,
    by: &str,
) -> R<()> {
    conn.execute(
        "INSERT INTO public.gtt_trail_policy (strategy, enabled, breakeven_after_pct, \
         breakeven_buffer_pct, trail_after_pct, trail_pct, min_step_pct, updated_at, updated_by) \
         VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (strategy) DO UPDATE SET \
         enabled = excluded.enabled, breakeven_after_pct = excluded.breakeven_after_pct, \
         breakeven_buffer_pct = excluded.breakeven_buffer_pct, \
         trail_after_pct = excluded.trail_after_pct, trail_pct = excluded.trail_pct, \
         min_step_pct = excluded.min_step_pct, updated_at = excluded.updated_at, \
         updated_by = excluded.updated_by",
        &[
            Param::Text(p.strategy.clone()),
            Param::Bool(p.enabled),
            dec2(p.breakeven_after_pct),
            dec2(p.breakeven_buffer_pct),
            dec2(p.trail_after_pct),
            dec2(p.trail_pct),
            dec2(p.min_step_pct),
            Param::Aware(now),
            Param::Text(by.to_owned()),
        ],
    )
    .await?;
    Ok(())
}

/// One open sleeve position with what the trail needs.
#[derive(Debug, Clone, PartialEq)]
pub struct Position {
    /// Sleeve.
    pub strategy: String,
    /// Position id.
    pub id: i64,
    /// Instrument.
    pub instrument_id: i64,
    /// Trading symbol.
    pub symbol: String,
    /// Exchange series.
    pub series: Option<String>,
    /// Quantity.
    pub qty: i64,
    /// Entry average.
    pub entry: Option<f64>,
    /// Entry date.
    pub entry_date: Option<NaiveDate>,
    /// The book's stop.
    pub stop: Option<f64>,
    /// Broker GTT id.
    pub gtt_id: Option<String>,
    /// The GTT's recorded trigger.
    pub gtt_trigger: Option<f64>,
    /// Paper position.
    pub simulated: bool,
}

impl Position {
    /// The resting trigger: the GTT's own, else the book's stop.
    #[must_use]
    pub fn current_stop(&self) -> Option<f64> {
        self.gtt_trigger.or(self.stop)
    }

    /// The broker's GTT id, when it is one (a `SIM-…` id is a paper stop).
    #[must_use]
    pub fn broker_gtt_id(&self) -> Option<i64> {
        self.gtt_id.as_deref().and_then(|s| s.trim().parse().ok())
    }
}

/// `(table, stop column)` per wired sleeve.
#[must_use]
pub fn table_of(strategy: &str) -> Option<(&'static str, &'static str)> {
    match strategy {
        "swing" => Some(("public.sw_position", "stop")),
        "twt" => Some(("public.tw_position", "stop_price")),
        "vbt" => Some(("public.vb_position", "stop_price")),
        _ => None,
    }
}

/// Every open position of the wired sleeves for `user_id`.
///
/// # Errors
/// The database.
pub async fn open_positions(conn: &mut Conn, user_id: i64) -> R<Vec<Position>> {
    let mut out = Vec::new();
    for s in super::policy::WIRED {
        let Some((table, stop)) = table_of(s) else {
            continue;
        };
        let sql = format!(
            "SELECT p.id, p.instrument_id, i.symbol, i.series, p.quantity_open AS qty, \
             p.entry_avg AS entry, p.entry_date, p.{stop} AS stop, p.gtt_id, p.gtt_trigger, \
             p.simulated FROM {table} p JOIN public.instrument i ON i.id = p.instrument_id \
             WHERE p.user_id = ? AND p.state IN ('OPEN', 'PARTIAL') AND p.quantity_open > 0 \
             ORDER BY i.symbol"
        );
        let cur = conn.execute(&sql, &[Param::Int(user_id)]).await?;
        for row in &cur.rows {
            out.push(Position {
                strategy: s.to_owned(),
                id: row.get("id").and_then(DbValue::as_i64).unwrap_or(0),
                instrument_id: row
                    .get("instrument_id")
                    .and_then(DbValue::as_i64)
                    .unwrap_or(0),
                symbol: t(row, "symbol").unwrap_or_default(),
                series: t(row, "series"),
                qty: row.get("qty").and_then(DbValue::as_i64).unwrap_or(0),
                entry: f(row, "entry"),
                entry_date: d(row, "entry_date"),
                stop: f(row, "stop"),
                gtt_id: t(row, "gtt_id"),
                gtt_trigger: f(row, "gtt_trigger"),
                simulated: b(row, "simulated"),
            });
        }
    }
    Ok(out)
}

/// Published bars for these instruments from `from` to `as_of` inclusive.
///
/// # Errors
/// The database.
pub async fn bars(
    conn: &mut Conn,
    ids: &[i64],
    from: NaiveDate,
    as_of: NaiveDate,
) -> R<HashMap<i64, Vec<Bar>>> {
    let mut out: HashMap<i64, Vec<Bar>> = HashMap::new();
    if ids.is_empty() {
        return Ok(out);
    }
    let marks = vec!["?"; ids.len()].join(", ");
    let sql = format!(
        "SELECT instrument_id, date, close, close_raw FROM public.ohlcv_daily \
         WHERE instrument_id IN ({marks}) AND date >= ? AND date <= ? ORDER BY instrument_id, date"
    );
    let mut params: Vec<Param> = ids.iter().map(|i| Param::Int(*i)).collect();
    params.push(Param::Date(from));
    params.push(Param::Date(as_of));
    let cur = conn.execute(&sql, &params).await?;
    for row in &cur.rows {
        let (Some(id), Some(day), Some(adj)) = (
            row.get("instrument_id").and_then(DbValue::as_i64),
            d(row, "date"),
            f(row, "close"),
        ) else {
            continue;
        };
        let raw = f(row, "close_raw").unwrap_or(adj);
        out.entry(id).or_default().push((day, adj, raw));
    }
    Ok(out)
}

/// One journal line.
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    /// daily or manual.
    pub source: String,
    /// Sleeve.
    pub strategy: String,
    /// Position id.
    pub position_id: i64,
    /// Trading symbol.
    pub symbol: String,
    /// Quantity.
    pub qty: i64,
    /// Entry average.
    pub entry: Option<f64>,
    /// The last bar read.
    pub as_of: Option<NaiveDate>,
    /// Highest exchange close since entry.
    pub highest_close: Option<f64>,
    /// Last exchange close.
    pub last_close: Option<f64>,
    /// Old stop.
    pub old_stop: Option<f64>,
    /// New stop.
    pub new_stop: Option<f64>,
    /// Why.
    pub reason: String,
    /// Outcome label.
    pub outcome: String,
    /// Detail.
    pub detail: String,
}

/// Append one journal line.
///
/// # Errors
/// The database.
pub async fn journal(conn: &mut Conn, e: &Entry, now: DateTime<FixedOffset>) -> R<()> {
    conn.execute(
        "INSERT INTO public.gtt_trail_journal (at, source, strategy, position_id, symbol, qty, \
         entry, as_of, highest_close, last_close, old_stop, new_stop, reason, outcome, detail) \
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        &[
            Param::Aware(now),
            Param::Text(e.source.clone()),
            Param::Text(e.strategy.clone()),
            Param::Int(e.position_id),
            Param::Text(e.symbol.clone()),
            Param::Int(e.qty),
            opt2(e.entry),
            e.as_of.map_or(Param::Null, Param::Date),
            opt2(e.highest_close),
            opt2(e.last_close),
            opt2(e.old_stop),
            opt2(e.new_stop),
            Param::Text(e.reason.clone()),
            Param::Text(e.outcome.clone()),
            Param::Text(e.detail.clone()),
        ],
    )
    .await?;
    Ok(())
}

/// The newest journal lines.
///
/// # Errors
/// The database.
pub async fn journal_rows(conn: &mut Conn, limit: i64) -> R<Vec<serde_json::Value>> {
    let cur = conn
        .execute(
            "SELECT id, at, source, strategy, position_id, symbol, qty, entry, as_of, \
             highest_close, last_close, old_stop, new_stop, reason, outcome, detail \
             FROM public.gtt_trail_journal ORDER BY id DESC LIMIT ?",
            &[Param::Int(limit.clamp(1, 500))],
        )
        .await?;
    Ok(cur
        .rows
        .iter()
        .map(|r| {
            let at = match r.get("at") {
                Some(DbValue::Aware(a)) => Some(a.to_rfc3339()),
                _ => t(r, "at"),
            };
            serde_json::json!({
                "id": r.get("id").and_then(DbValue::as_i64),
                "at": at,
                "source": t(r, "source"),
                "strategy": t(r, "strategy"),
                "position_id": r.get("position_id").and_then(DbValue::as_i64),
                "symbol": t(r, "symbol"),
                "qty": r.get("qty").and_then(DbValue::as_i64),
                "entry": f(r, "entry"),
                "as_of": t(r, "as_of"),
                "highest_close": f(r, "highest_close"),
                "last_close": f(r, "last_close"),
                "old_stop": f(r, "old_stop"),
                "new_stop": f(r, "new_stop"),
                "reason": t(r, "reason"),
                "outcome": t(r, "outcome"),
                "detail": t(r, "detail"),
            })
        })
        .collect())
}

/// Whether the daily job already ran since `since`.
///
/// # Errors
/// The database.
pub async fn ran_since(conn: &mut Conn, since: DateTime<FixedOffset>) -> R<bool> {
    let cur = conn
        .execute(
            "SELECT 1 AS one FROM public.gtt_trail_journal WHERE source = 'daily' \
             AND outcome = 'RUN' AND at >= ? LIMIT 1",
            &[Param::Aware(since)],
        )
        .await?;
    Ok(!cur.rows.is_empty())
}

/// After a broker-confirmed modify: the book's stop and resting trigger move up (never down).
///
/// # Errors
/// The database.
pub async fn raise_stop(
    conn: &mut Conn,
    strategy: &str,
    position_id: i64,
    user_id: i64,
    new_stop: f64,
    now: DateTime<FixedOffset>,
) -> R<()> {
    let Some((table, stop)) = table_of(strategy) else {
        return Ok(());
    };
    let sql = format!(
        "UPDATE {table} SET {stop} = GREATEST(COALESCE({stop}, 0), ?), \
         gtt_trigger = GREATEST(COALESCE(gtt_trigger, 0), ?), updated_at = ? \
         WHERE id = ? AND user_id = ? AND state IN ('OPEN', 'PARTIAL')"
    );
    conn.execute(
        &sql,
        &[
            dec2(new_stop),
            dec2(new_stop),
            Param::Aware(now),
            Param::Int(position_id),
            Param::Int(user_id),
        ],
    )
    .await?;
    Ok(())
}
