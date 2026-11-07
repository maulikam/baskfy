//! Live index quotes: Kite `quote` for the live tradingsymbols, cached [`TTL`] in market hours;
//! on a failure the last good answer is served as stale.

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;

/// Cache life in market hours.
pub const TTL: Duration = Duration::from_secs(15);

/// One index quote.
#[derive(Debug, Clone, Copy)]
pub struct Quote {
    /// Last.
    pub last: Decimal,
    /// Previous close.
    pub prev_close: Option<Decimal>,
    /// Day open/high/low.
    pub open: Option<Decimal>,
    /// High.
    pub high: Option<Decimal>,
    /// Low.
    pub low: Option<Decimal>,
}

/// Where the numbers came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fresh {
    /// A quote within [`TTL`].
    Live,
    /// Market shut: the last close.
    Close,
    /// Kite failed: the last good answer.
    Stale,
}

impl Fresh {
    /// The wire word.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Live => "live",
            Self::Close => "close",
            Self::Stale => "stale",
        }
    }
}

struct Cache {
    at: Option<Instant>,
    fetched_at: Option<DateTime<Utc>>,
    map: HashMap<String, Quote>,
}

static CACHE: LazyLock<Mutex<Cache>> = LazyLock::new(|| {
    Mutex::new(Cache {
        at: None,
        fetched_at: None,
        map: HashMap::new(),
    })
});

/// Quotes keyed by Kite tradingsymbol, the freshness, and when they were fetched.
/// Outside market hours, or with no Kite session, nothing is fetched (`Close`).
pub async fn index_quotes(symbols: &[String]) -> (HashMap<String, Quote>, Fresh, Option<DateTime<Utc>>) {
    if !super::market_open(super::now_ist()) || symbols.is_empty() {
        return (HashMap::new(), Fresh::Close, None);
    }
    let snapshot = |fresh: Fresh| -> (HashMap<String, Quote>, Fresh, Option<DateTime<Utc>>) {
        CACHE.lock().map_or_else(
            |_| (HashMap::new(), fresh, None),
            |c| (c.map.clone(), fresh, c.fetched_at),
        )
    };
    let fresh = CACHE
        .lock()
        .ok()
        .and_then(|c| c.at)
        .is_some_and(|at| at.elapsed() < TTL);
    if fresh {
        return snapshot(Fresh::Live);
    }
    let permitted = crate::live_prices::quotes_permitted().unwrap_or(false);
    let api_key = std::env::var("BASKFY_KITE_API_KEY").unwrap_or_default();
    if !permitted || api_key.trim().is_empty() {
        let had = CACHE.lock().map(|c| c.at.is_some()).unwrap_or(false);
        return if had { snapshot(Fresh::Stale) } else { (HashMap::new(), Fresh::Close, None) };
    }
    let fetched = match crate::live_prices::kite_provider(api_key.trim()).await {
        Ok(p) => {
            let wanted: Vec<&str> = symbols.iter().map(String::as_str).collect();
            p.quotes(&wanted, "NSE").await.map_err(|e| e.to_string())
        }
        Err(e) => Err(e),
    };
    match fetched {
        Ok(records) if !records.is_empty() => {
            let map: HashMap<String, Quote> = records
                .into_iter()
                .filter(|r| r.last_price > Decimal::ZERO)
                .map(|r| {
                    (
                        r.symbol.clone(),
                        Quote {
                            last: r.last_price,
                            prev_close: r.prev_close.filter(|p| *p > Decimal::ZERO),
                            open: r.open,
                            high: r.high,
                            low: r.low,
                        },
                    )
                })
                .collect();
            if let Ok(mut c) = CACHE.lock() {
                c.at = Some(Instant::now());
                c.fetched_at = Some(Utc::now());
                c.map = map;
            }
            snapshot(Fresh::Live)
        }
        _ => snapshot(Fresh::Stale),
    }
}
