//! Ports `app/swing_live_gate.py` (SW30, Maulik, 29 Sep 2026): between 09:15 and 15:30 on a
//! weekday the gate is `market_gate` over the last close's breadth and the benchmark's EMA(10)
//! and EMA(20) over the last 249 published closes plus today's Kite quote. The index is
//! `SWING_INDEX_SLUG` — **NIFTY Smallcap 250** (SW29), NIFTY 500 the fallback the worker uses;
//! this reads whatever the setting says and never "fixes" it to a document.
//!
//! Outside the session, without a Kite session, without a quote symbol for the index or
//! without enough published closes, the gate is the last close's. Nothing here places,
//! confirms or cancels anything.

use std::sync::Arc;

use baskfy_core::swing::market::{ExposureTier, MarketConfig, MarketGate};
use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveTime, Weekday};

use super::super::rules::{
    BreadthSnapshot, IndexReading, index_reading, intraday_tier, market_gate,
};
use crate::kite::KiteReads;
use crate::storage::{Conn, DbValue, Param, StorageError};

/// `SESSION_OPEN`.
#[must_use]
pub fn session_open() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 0).unwrap_or_default()
}

/// `SESSION_CLOSE`.
#[must_use]
pub fn session_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default()
}

/// `INDEX_QUOTE_SYMBOL`: our index slug → Kite's tradingsymbol on NSE.
pub const INDEX_QUOTE_SYMBOL: [(&str, &str); 3] = [
    ("nifty-smallcap-250", "NIFTY SMLCAP 250"),
    ("nifty-mid-small-400", "NIFTY MIDSML 400"),
    ("nifty-500", "NIFTY 500"),
];

/// Kite's symbol for an index slug.
#[must_use]
pub fn quote_symbol(slug: &str) -> Option<&'static str> {
    INDEX_QUOTE_SYMBOL
        .iter()
        .find(|(s, _)| *s == slug)
        .map(|(_, k)| *k)
}

/// `LiveGate`: the gate as the live index reads it, with what it was read from.
#[derive(Debug, Clone, PartialEq)]
pub struct LiveGate {
    /// The gate.
    pub gate: MarketGate,
    /// The rung it allows.
    pub tier: ExposureTier,
    /// The index reading.
    pub reading: IndexReading,
    /// Which index.
    pub index_slug: String,
}

/// `in_session`: 09:15–15:30 on a weekday (both ends included).
#[must_use]
pub fn in_session(moment: &DateTime<FixedOffset>) -> bool {
    let weekday = !matches!(moment.weekday(), Weekday::Sat | Weekday::Sun);
    let t = moment.time();
    weekday && session_open() <= t && t <= session_close()
}

/// `LiveLevel`: today's level of the gate's benchmark, or `None` when it cannot be read.
#[async_trait::async_trait]
pub trait LiveLevel: Send + Sync {
    /// The level now.
    async fn level(&self) -> Option<f64>;
}

/// `kite_level`: a [`LiveLevel`] over the desk's Kite client (`Kite.ltp`).
pub struct KiteLevel {
    kite: Arc<dyn KiteReads>,
    slug: String,
}

impl KiteLevel {
    /// Over `kite`, for `slug` (the setting's `SWING_INDEX_SLUG`).
    #[must_use]
    pub fn new(kite: Arc<dyn KiteReads>, slug: &str) -> Self {
        Self {
            kite,
            slug: slug.to_owned(),
        }
    }
}

#[async_trait::async_trait]
impl LiveLevel for KiteLevel {
    async fn level(&self) -> Option<f64> {
        let symbol = quote_symbol(&self.slug)?;
        // `Kite.ltp([symbol])` is the desk wrapper's read: the SDK's `ltp` keyed `NSE:<sym>`,
        // flattened to `{symbol: last_price}`.
        let key = format!("NSE:{symbol}");
        let answer = self.kite.ltp(std::slice::from_ref(&key)).await.ok()?;
        let value = match &answer {
            crate::wire::PyValue::Map(m) => m.get(&key).cloned(),
            _ => None,
        }?;
        let price = match value {
            crate::wire::PyValue::Map(row) => row.get("last_price").cloned(),
            other => Some(other),
        }?;
        let level = match price {
            crate::wire::PyValue::Float(f) => f,
            #[allow(clippy::cast_precision_loss)]
            crate::wire::PyValue::Int(i) => i as f64,
            _ => return None,
        };
        // Python: `float(value) if value else None` — a zero is no level.
        if level == 0.0 { None } else { Some(level) }
    }
}

/// `desk_level()`: a [`LiveLevel`] over the desk server's own Kite wrapper
/// (`app.kite_client.Kite.ltp`: the LTP cache and the read limiter first, and
/// `refresh_token_if_changed` before the read), resolved at read time — where [`KiteLevel`]
/// reads the SDK directly. `None` at every read when `Kite()` cannot be constructed (no
/// `KITE_API_KEY`) or the desk carries no wrapper: Python's `except Exception: return None`.
/// The index is the setting's `SWING_INDEX_SLUG` as it stands at the read (`kite_level(kite)`
/// with no slug).
pub struct DeskLevel {
    kite: Option<Arc<crate::lanes::exec_core::kite_client::DeskKite>>,
    settings: crate::settings::SharedSettings,
}

impl DeskLevel {
    /// Over the desk's wrapper (`None` when the desk has none) and its live settings.
    #[must_use]
    pub const fn new(
        kite: Option<Arc<crate::lanes::exec_core::kite_client::DeskKite>>,
        settings: crate::settings::SharedSettings,
    ) -> Self {
        Self { kite, settings }
    }
}

#[async_trait::async_trait]
impl LiveLevel for DeskLevel {
    async fn level(&self) -> Option<f64> {
        let (key_set, slug) = match self.settings.read() {
            Ok(s) => (!s.kite_api_key.is_empty(), s.swing_index_slug.clone()),
            Err(poisoned) => {
                let s = poisoned.into_inner();
                (!s.kite_api_key.is_empty(), s.swing_index_slug.clone())
            }
        };
        if !key_set {
            return None;
        }
        let kite = self.kite.as_ref()?;
        let symbol = quote_symbol(&slug)?;
        // `kite.ltp([symbol]).get(symbol)`, any failure `None` — the close's gate stands.
        let prices = kite.ltp(&[symbol.to_owned()], "NSE").await.ok()?;
        let level = match prices.get(symbol)? {
            crate::wire::PyValue::Float(f) => *f,
            #[allow(clippy::cast_precision_loss)]
            crate::wire::PyValue::Int(i) => *i as f64,
            _ => return None,
        };
        // `float(value) if value else None`.
        if level == 0.0 { None } else { Some(level) }
    }
}

/// `published_levels`: the last `count` published levels of `slug` strictly before `before`,
/// oldest first.
///
/// # Errors
/// The driver's.
pub async fn published_levels(
    conn: &mut Conn,
    slug: &str,
    before: NaiveDate,
    count: i64,
    schema: &str,
) -> Result<Vec<f64>, StorageError> {
    let prefix = if schema.is_empty() {
        String::new()
    } else {
        format!("{schema}.")
    };
    let rows = conn
        .execute(
            &format!(
                "SELECT s.level FROM {prefix}index_snapshot_daily s \
                 JOIN {prefix}index_def d ON d.id = s.index_id \
                 WHERE d.slug = ? AND s.date < ? AND s.level IS NOT NULL ORDER BY s.date DESC LIMIT ?"
            ),
            &[
                Param::Text(slug.to_owned()),
                Param::Text(before.format("%Y-%m-%d").to_string()),
                Param::Int(count),
            ],
        )
        .await?
        .rows;
    Ok(rows
        .iter()
        .rev()
        .filter_map(|r| {
            // `float(r["level"])`: a number, or a numeric string (SQLite may hand back TEXT).
            r.get("level").and_then(|v| match v {
                DbValue::Text(t) => t.trim().parse::<f64>().ok(),
                other => other.as_f64(),
            })
        })
        .collect())
}

/// `live_gate`: the gate from the last close's breadth and the index with today's level
/// appended; `None` when the closes are too few for the slow average.
#[must_use]
pub fn live_gate(
    breadth: &BreadthSnapshot,
    close_tier: &ExposureTier,
    closes: &[f64],
    level: f64,
    slug: &str,
    config: &MarketConfig,
) -> Option<LiveGate> {
    let mut levels = closes.to_vec();
    levels.push(level);
    let reading = index_reading(&levels, config)?;
    let gate = market_gate(breadth, Some(&reading), config);
    Some(LiveGate {
        gate,
        tier: intraday_tier(close_tier, gate, config),
        reading,
        index_slug: slug.to_owned(),
    })
}
