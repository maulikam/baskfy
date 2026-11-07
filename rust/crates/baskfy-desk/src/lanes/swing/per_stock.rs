//! Per-stock regime gate for swing entries (Maulik, 7 Nov 2026; docs/swing/REGIME-SIGNAL.md).
//! The worker's `stock_regime_signal` rows decide; during the session each index's EMA(10)/EMA(20)
//! is re-read with today's Kite level appended (as the SW30 live gate does for Smallcap 250).
//! Reads only — never places, confirms or cancels anything.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::regime::signal::FALLBACK_INDEX;
use baskfy_core::regime::{PER_STOCK_SLUG, RegimeConfig, RegimeMode};
use baskfy_core::swing::market::{INDEX_HISTORY_SESSIONS, gate_ema_pair};
use chrono::{DateTime, Days, FixedOffset, NaiveDate};

use super::live_gate::{LiveLevel, in_session};
use super::values::{boolean, col, text};
use crate::storage::{Conn, DbValue, Param, StorageError};

/// One symbol's stored verdict.
#[derive(Debug, Clone, PartialEq)]
pub struct StockGate {
    /// Stored GREEN.
    pub green: bool,
    /// Decided by the Smallcap rule.
    pub fallback: bool,
    /// The indices it read, each with its stored EMA(10) > EMA(20).
    pub indices: Vec<(String, bool)>,
}

/// The per-stock gate for the day.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct PerStockGate {
    /// The signal date read.
    pub as_of: Option<NaiveDate>,
    /// symbol → stored verdict.
    pub stocks: BTreeMap<String, StockGate>,
    /// Stored Smallcap-250 cross.
    pub smallcap_up: bool,
    /// index name → live EMA(10) > EMA(20), where today's level was read.
    pub live_up: BTreeMap<String, bool>,
}

impl PerStockGate {
    /// May a new swing entry in `symbol` be planned?
    #[must_use]
    pub fn allows(&self, symbol: &str) -> bool {
        let smallcap = self
            .live_up
            .get(FALLBACK_INDEX)
            .copied()
            .unwrap_or(self.smallcap_up);
        let Some(s) = self.stocks.get(symbol) else {
            return smallcap;
        };
        if s.fallback {
            return smallcap;
        }
        if self.live_up.is_empty() {
            return s.green;
        }
        // Live where the index has today's level, stored otherwise.
        s.indices
            .iter()
            .any(|(name, up)| self.live_up.get(name).copied().unwrap_or(*up))
    }
}

fn f64_of(v: Option<&DbValue>) -> Option<f64> {
    match v? {
        DbValue::Text(s) => s.trim().parse().ok(),
        other => other.as_f64(),
    }
}

fn tbl(schema: &str, table: &str) -> String {
    if schema.is_empty() {
        table.to_owned()
    } else {
        format!("{schema}.{table}")
    }
}

async fn ready(conn: &mut Conn, schema: &str) -> Result<bool, StorageError> {
    let name = if schema.is_empty() {
        "stock_regime_signal".to_owned()
    } else {
        format!("{schema}.stock_regime_signal")
    };
    Ok(conn
        .execute(
            "SELECT to_regclass(?) IS NOT NULL AS ok",
            &[Param::Text(name)],
        )
        .await?
        .rows
        .first()
        .is_some_and(|r| boolean(col(r, "ok"))))
}

/// `None` unless `BASKFY_REGIME_MODE=per_stock`, Postgres, 0062 migrated, the close's market row
/// was written per-stock, and signal rows exist within a week of `day`.
///
/// # Errors
/// The driver's.
pub async fn load(
    conn: &mut Conn,
    user_id: i64,
    day: NaiveDate,
    schema: &str,
    live_level: Option<&dyn LiveLevel>,
    now: Option<DateTime<FixedOffset>>,
) -> Result<Option<PerStockGate>, StorageError> {
    let cfg = RegimeConfig::from_lookup(&|k| std::env::var(k).ok()).unwrap_or_default();
    if cfg.mode != RegimeMode::PerStock || !conn.is_postgres() || !ready(conn, schema).await? {
        return Ok(None);
    }
    let t = |table: &str| tbl(schema, table);
    let slug = conn
        .execute(
            &format!(
                "SELECT index_slug FROM {} WHERE user_id = ? AND date < CAST(? AS DATE)                  ORDER BY date DESC LIMIT 1",
                t("sw_market_daily")
            ),
            &[Param::Int(user_id), Param::Date(day)],
        )
        .await?
        .rows
        .first()
        .map(|r| text(col(r, "index_slug")));
    if slug.as_deref() != Some(PER_STOCK_SLUG) {
        return Ok(None);
    }
    let oldest = day.checked_sub_days(Days::new(7)).unwrap_or(day);
    let rows = conn
        .execute(
            &format!(
                "SELECT as_of::text AS as_of, symbol, signal, fallback, evidence::text AS evidence \
                 FROM {sig} WHERE as_of = (SELECT max(as_of) FROM {sig} \
                 WHERE as_of <= CAST(? AS DATE) AND as_of >= CAST(? AS DATE))",
                sig = t("stock_regime_signal")
            ),
            &[Param::Date(day), Param::Date(oldest)],
        )
        .await?
        .rows;
    if rows.is_empty() {
        return Ok(None);
    }
    let mut gate = PerStockGate::default();
    let mut names: BTreeSet<String> = BTreeSet::new();
    for r in &rows {
        gate.as_of = NaiveDate::parse_from_str(&text(col(r, "as_of")), "%Y-%m-%d").ok();
        let evidence: serde_json::Value =
            serde_json::from_str(&text(col(r, "evidence"))).unwrap_or_default();
        let indices: Vec<(String, bool)> = evidence["indices"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|i| {
                        Some((i["name"].as_str()?.to_owned(), i["up"].as_bool().unwrap_or(false)))
                    })
                    .collect()
            })
            .unwrap_or_default();
        names.extend(indices.iter().map(|(n, _)| n.clone()));
        gate.stocks.insert(
            text(col(r, "symbol")),
            StockGate {
                green: text(col(r, "signal")) == "GREEN",
                fallback: boolean(col(r, "fallback")),
                indices,
            },
        );
    }
    gate.smallcap_up = conn
        .execute(
            &format!(
                "SELECT s.ema_cross FROM {st} s JOIN {m} m ON m.id = s.index_id \
                 WHERE m.name = ? AND s.date <= CAST(? AS DATE) AND s.ema_cross IS NOT NULL \
                 ORDER BY s.date DESC LIMIT 1",
                st = t("index_stats_daily"),
                m = t("index_master")
            ),
            &[Param::Text(FALLBACK_INDEX.to_owned()), Param::Date(day)],
        )
        .await?
        .rows
        .first()
        .is_some_and(|r| boolean(col(r, "ema_cross")));
    names.insert(FALLBACK_INDEX.to_owned());
    if let (Some(level), Some(moment)) = (live_level, now) {
        let moment = moment.with_timezone(&crate::clock::ist());
        if moment.date_naive() == day && in_session(&moment) {
            gate.live_up = live_up(conn, &names, day, schema, level).await?;
        }
    }
    Ok(Some(gate))
}

/// Each index's live cross: the last 249 published closes before `day` plus today's level.
async fn live_up(
    conn: &mut Conn,
    names: &BTreeSet<String>,
    day: NaiveDate,
    schema: &str,
    level: &dyn LiveLevel,
) -> Result<BTreeMap<String, bool>, StorageError> {
    let t = |table: &str| tbl(schema, table);
    let list: Vec<String> = names.iter().cloned().collect();
    let placeholders = vec!["?"; list.len()].join(", ");
    let mut params: Vec<Param> = list.iter().cloned().map(Param::Text).collect();
    params.push(Param::Date(day));
    params.push(Param::Int(i64::try_from(INDEX_HISTORY_SESSIONS - 1).unwrap_or(249)));
    let rows = conn
        .execute(
            &format!(
                "SELECT name, sym, close FROM (SELECT m.name, m.kite_tradingsymbol AS sym, \
                 b.date, b.close::float8 AS close, row_number() OVER \
                 (PARTITION BY m.id ORDER BY b.date DESC) AS rn FROM {m} m \
                 JOIN {b} b ON b.index_id = m.id WHERE m.name IN ({placeholders}) \
                 AND m.kite_tradingsymbol IS NOT NULL AND b.date < CAST(? AS DATE)) x \
                 WHERE rn <= ? ORDER BY name, date",
                m = t("index_master"),
                b = t("index_bar_daily"),
            ),
            &params,
        )
        .await?
        .rows;
    let mut closes: BTreeMap<String, (String, Vec<f64>)> = BTreeMap::new();
    for r in &rows {
        let Some(c) = f64_of(col(r, "close")) else { continue };
        closes
            .entry(text(col(r, "name")))
            .or_insert_with(|| (text(col(r, "sym")), Vec::new()))
            .1
            .push(c);
    }
    let symbols: Vec<String> = closes.values().map(|(s, _)| s.clone()).collect();
    let levels = level.levels(&symbols).await;
    let mut out = BTreeMap::new();
    for (name, (sym, mut series)) in closes {
        let Some(l) = levels.get(&sym) else { continue };
        series.push(*l);
        if let Some((fast, slow)) = gate_ema_pair(&series, 10, 20) {
            out.insert(name, fast > slow);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn live_readings_override_the_stored_verdict_and_fallbacks_follow_smallcap() {
        let mut g = PerStockGate {
            smallcap_up: true,
            ..PerStockGate::default()
        };
        g.stocks.insert(
            "AAA".into(),
            StockGate {
                green: true,
                fallback: false,
                indices: vec![("Nifty IT".into(), true)],
            },
        );
        g.stocks.insert(
            "BBB".into(),
            StockGate {
                green: false,
                fallback: true,
                indices: Vec::new(),
            },
        );
        assert!(g.allows("AAA") && g.allows("BBB") && g.allows("ZZZ"));
        g.live_up.insert("Nifty IT".into(), false);
        g.live_up.insert(FALLBACK_INDEX.into(), false);
        assert!(!g.allows("AAA") && !g.allows("BBB") && !g.allows("ZZZ"));
    }
}
