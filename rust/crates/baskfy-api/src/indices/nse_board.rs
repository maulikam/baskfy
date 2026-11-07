//! NSE `allIndices`: live levels for indices Kite does not quote, and a fallback when Kite fails.
//! One request per [`TTL`]; a failure serves the last good board.

use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use rust_decimal::Decimal;
use serde_json::Value;
use tokio::sync::Mutex;

use super::quotes::Quote;

/// `allIndices`.
pub const URL: &str = "https://www.nseindia.com/api/allIndices";
/// Cache life.
pub const TTL: Duration = Duration::from_secs(60);
const UA: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 \
                  (KHTML, like Gecko) Chrome/130.0 Safari/537.36";

struct Cache {
    at: Option<Instant>,
    map: HashMap<String, Quote>,
}

static CACHE: LazyLock<Mutex<Cache>> = LazyLock::new(|| {
    Mutex::new(Cache {
        at: None,
        map: HashMap::new(),
    })
});

/// Match key: uppercase alphanumerics, "INDEX" dropped (`NIFTY HEALTHCARE INDEX` = `Nifty Healthcare`).
#[must_use]
pub fn key(name: &str) -> String {
    name.to_uppercase()
        .replace("INDEX", "")
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect()
}

fn dec(v: &Value) -> Option<Decimal> {
    match v {
        Value::Number(n) => n.to_string().parse().ok(),
        Value::String(s) => s.replace(',', "").trim().parse().ok(),
        _ => None,
    }
}

fn parse(body: &str) -> HashMap<String, Quote> {
    let Ok(v) = serde_json::from_str::<Value>(body) else {
        return HashMap::new();
    };
    let Some(rows) = v.get("data").and_then(Value::as_array) else {
        return HashMap::new();
    };
    let mut out = HashMap::new();
    for r in rows {
        let (Some(name), Some(last)) = (
            r.get("index").and_then(Value::as_str),
            r.get("last").and_then(dec),
        ) else {
            continue;
        };
        if last <= Decimal::ZERO {
            continue;
        }
        let pos = |k: &str| r.get(k).and_then(dec).filter(|d| *d > Decimal::ZERO);
        out.insert(
            key(name),
            Quote {
                last,
                prev_close: pos("previousClose"),
                open: pos("open"),
                high: pos("high"),
                low: pos("low"),
            },
        );
    }
    out
}

async fn fetch() -> Result<HashMap<String, Quote>, String> {
    let client = super::nse::client()?;
    let r = client
        .get(URL)
        .map_err(|e| e.to_string())?
        .header("User-Agent", UA)
        .header("Referer", "https://www.nseindia.com/")
        .header("Accept", "application/json")
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = r.status().as_u16();
    let body = r.text().await.map_err(|e| e.to_string())?;
    let map = parse(&body);
    if status != 200 || map.is_empty() {
        return Err(format!("allIndices HTTP {status}"));
    }
    Ok(map)
}

/// The board keyed by [`key`]; empty when NSE never answered.
pub async fn board() -> HashMap<String, Quote> {
    let mut c = CACHE.lock().await;
    if c.at.is_some_and(|at| at.elapsed() < TTL) {
        return c.map.clone();
    }
    match fetch().await {
        Ok(map) => {
            c.at = Some(Instant::now());
            c.map = map;
        }
        Err(e) => {
            crate::logging::warning("baskfy_api.indices", "nse allIndices", &[("error", &e)]);
            c.at = Some(Instant::now());
        }
    }
    c.map.clone()
}
