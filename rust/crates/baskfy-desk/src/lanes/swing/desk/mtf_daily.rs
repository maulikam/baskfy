//! Daily Zerodha MTF leverage (Maulik, 7 Nov 2026): quoted once each morning for every NSE stock
//! and stored in `public.mtf_leverage`, so multi-buy reads a table instead of asking the broker
//! per stock. Read-only Kite calls (basket margins), batched and run concurrently.

use std::collections::HashMap;

use axum::body::Body;
use axum::extract::State;
use axum::http::{Response, Uri};
use chrono::{Datelike, NaiveDate, Timelike, Weekday};
use futures_util::stream::{self, StreamExt};
use rust_decimal::Decimal;

use super::opps_ticket::margin_detail;
use super::super::rules::{py_dec, q2};
use crate::app::AppState;
use crate::lanes::exec_core::exec_kite::status_session;
use crate::lanes::twt::common::{FormFields, HttpError, json_response};
use crate::storage::{DbValue, Param};
use crate::wire::{PyDict, PyValue, dict};

const BASKET_SIZE: usize = 50;

type Basket = (Vec<String>, Result<HashMap<String, Decimal>, String>);

#[allow(clippy::print_stderr)]
fn log(line: &str) {
    eprintln!("{line}");
}
const BASKET_CONCURRENCY: usize = 3;
const SINGLE_CONCURRENCY: usize = 6;
const MAX_REQUEST_SYMBOLS: usize = 200;
/// A stored row this many days old is still used (weekends, a missed morning).
const MAX_AGE_DAYS: i64 = 4;

#[derive(Debug, Clone)]
struct Lev {
    eligible: bool,
    leverage: Option<Decimal>,
    reason: Option<String>,
}

fn text(v: Option<&DbValue>) -> Option<String> {
    match v {
        Some(DbValue::Text(s)) => Some(s.clone()),
        _ => None,
    }
}

fn dec(v: Option<&PyValue>) -> Option<Decimal> {
    match v {
        Some(PyValue::Float(f)) => Decimal::try_from(*f).ok(),
        Some(PyValue::Int(i)) => Some(Decimal::from(*i)),
        Some(PyValue::Str(s)) => s.parse().ok(),
        _ => None,
    }
}

fn basket_order(symbol: &str) -> PyValue {
    PyValue::Map(dict([
        ("exchange", PyValue::Str("NSE".to_owned())),
        ("tradingsymbol", PyValue::Str(symbol.to_owned())),
        ("transaction_type", PyValue::Str("BUY".to_owned())),
        ("variety", PyValue::Str("regular".to_owned())),
        ("product", PyValue::Str("MTF".to_owned())),
        ("order_type", PyValue::Str("MARKET".to_owned())),
        ("quantity", PyValue::Int(1)),
    ]))
}

/// One basket margin call; each order line carries its own `leverage`.
async fn basket(state: &AppState, symbols: &[String]) -> Result<HashMap<String, Decimal>, String> {
    let orders: Vec<PyValue> = symbols.iter().map(|s| basket_order(s)).collect();
    let resp = state
        .kite
        .basket_order_margins(&orders, false, None)
        .await
        .map_err(|e| e.to_string())?;
    let PyValue::Map(m) = &resp else { return Err("no margin".to_owned()) };
    let Some(PyValue::List(lines)) = m.get("orders") else { return Err("no per-order margins".to_owned()) };
    let mut out = HashMap::new();
    for (i, line) in lines.iter().enumerate() {
        let PyValue::Map(l) = line else { continue };
        let sym = match l.get("tradingsymbol") {
            Some(PyValue::Str(s)) => Some(s.to_uppercase()),
            _ => symbols.get(i).cloned(),
        };
        if let (Some(sym), Some(lev)) = (sym, dec(l.get("leverage")).filter(|x| *x > Decimal::ONE)) {
            out.insert(sym, lev);
        }
    }
    Ok(out)
}

/// Quotes every symbol: baskets of 50 in parallel; a basket the broker refuses falls back to
/// one call per symbol, also in parallel. Kite's shared limiter paces the actual requests.
async fn quote_all(state: &AppState, symbols: &[String]) -> HashMap<String, Lev> {
    let chunks: Vec<Vec<String>> = symbols.chunks(BASKET_SIZE).map(<[String]>::to_vec).collect();
    let answered: Vec<Basket> = stream::iter(chunks)
        .map(|chunk| async move {
            let r = basket(state, &chunk).await;
            (chunk, r)
        })
        .buffer_unordered(BASKET_CONCURRENCY)
        .collect()
        .await;
    let mut out = HashMap::new();
    let mut retry = Vec::new();
    for (chunk, r) in answered {
        match r {
            Ok(found) => {
                for s in chunk {
                    let lev = found.get(&s).copied();
                    out.insert(
                        s,
                        Lev {
                            eligible: lev.is_some(),
                            leverage: lev,
                            reason: lev.is_none().then(|| "not on Zerodha's MTF list (no funding)".to_owned()),
                        },
                    );
                }
            }
            Err(_) => retry.extend(chunk),
        }
    }
    let singles: Vec<(String, Result<Option<Decimal>, String>)> = stream::iter(retry)
        .map(|s| async move {
            let r = margin_detail(state, &s, "NSE", 1, "MTF", "MARKET", Decimal::ZERO)
                .await
                .map(|m| m.leverage);
            (s, r)
        })
        .buffer_unordered(SINGLE_CONCURRENCY)
        .collect()
        .await;
    for (s, r) in singles {
        let lev = match r {
            // 1× means Zerodha funds nothing: the stock is not on its MTF list.
            Ok(Some(l)) if l > Decimal::ONE => Lev { eligible: true, leverage: Some(l), reason: None },
            Ok(Some(_)) => Lev { eligible: false, leverage: None, reason: Some("not on Zerodha's MTF list (1×, no funding)".to_owned()) },
            Ok(None) => Lev { eligible: false, leverage: None, reason: Some("Zerodha returned no MTF leverage".to_owned()) },
            Err(e) => Lev { eligible: false, leverage: None, reason: Some(format!("Zerodha refused an MTF margin quote: {e}")) },
        };
        out.insert(s, lev);
    }
    out
}

async fn store(state: &AppState, day: NaiveDate, rows: &HashMap<String, Lev>) -> Result<(), String> {
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    for (symbol, l) in rows {
        conn.execute(
            "INSERT INTO public.mtf_leverage (as_of, symbol, eligible, leverage, reason) VALUES (?, ?, ?, ?, ?) \
             ON CONFLICT (as_of, symbol) DO UPDATE SET eligible = excluded.eligible, \
             leverage = excluded.leverage, reason = excluded.reason, fetched_at = now()",
            &[
                Param::Date(day),
                Param::Text(symbol.clone()),
                Param::Text(if l.eligible { "true" } else { "false" }.to_owned()),
                l.leverage.map_or(Param::Null, |v| Param::Text(q2(v).to_string())),
                l.reason.clone().map_or(Param::Null, Param::Text),
            ],
        )
        .await
        .map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// The latest stored row per symbol, no older than `MAX_AGE_DAYS`.
async fn stored(state: &AppState, day: NaiveDate, symbols: &[String]) -> Result<HashMap<String, Lev>, String> {
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    let list = symbols
        .iter()
        .map(|s| format!("'{}'", s.replace('\'', "")))
        .collect::<Vec<_>>()
        .join(",");
    let since = day - chrono::Duration::days(MAX_AGE_DAYS);
    let cur = conn
        .execute(
            &format!(
                "SELECT DISTINCT ON (symbol) symbol, CASE WHEN eligible THEN 'true' ELSE 'false' END AS eligible, \
                 CAST(leverage AS TEXT) AS leverage, reason FROM public.mtf_leverage \
                 WHERE symbol IN ({list}) AND as_of >= ? AND as_of <= ? ORDER BY symbol, as_of DESC"
            ),
            &[Param::Date(since), Param::Date(day)],
        )
        .await
        .map_err(|e| e.to_string())?;
    let mut out = HashMap::new();
    for row in &cur.rows {
        let Some(sym) = text(row.get("symbol")) else { continue };
        out.insert(
            sym,
            Lev {
                eligible: text(row.get("eligible")).as_deref() == Some("true"),
                leverage: text(row.get("leverage")).and_then(|v| v.parse().ok()),
                reason: text(row.get("reason")),
            },
        );
    }
    Ok(out)
}

async fn authed(state: &AppState) -> bool {
    matches!(status_session(state).await.get("authed"), Some(PyValue::Bool(true)))
}

/// Every active NSE cash stock (EQ and BE series).
async fn universe(state: &AppState) -> Result<Vec<String>, String> {
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    let cur = conn
        .execute(
            "SELECT DISTINCT i.symbol AS symbol FROM public.instrument i \
             JOIN public.exchange e ON e.id = i.exchange_id \
             WHERE e.code = 'NSE' AND i.is_active AND i.series IN ('EQ', 'BE') ORDER BY 1",
            &[],
        )
        .await
        .map_err(|e| e.to_string())?;
    Ok(cur.rows.iter().filter_map(|r| text(r.get("symbol"))).collect())
}

async fn refreshed_today(state: &AppState, day: NaiveDate) -> bool {
    let Ok(mut conn) = state.db.connect().await else { return false };
    conn.execute("SELECT 1 AS one FROM public.mtf_leverage WHERE as_of = ? LIMIT 1", &[Param::Date(day)])
        .await
        .is_ok_and(|c| !c.rows.is_empty())
}

/// The morning job: once Kite is logged in on a weekday from 08:00, quote the whole universe.
pub async fn refresh_if_due(state: &AppState) {
    let now = state.clock.now_ist();
    let day = now.date_naive();
    if matches!(day.weekday(), Weekday::Sat | Weekday::Sun) || now.hour() < 8 || now.hour() >= 16 {
        return;
    }
    if refreshed_today(state, day).await || !authed(state).await {
        return;
    }
    let symbols = match universe(state).await {
        Ok(s) if !s.is_empty() => s,
        Ok(_) => return,
        Err(e) => {
            log(&format!("mtf-leverage: universe read failed: {e}"));
            return;
        }
    };
    let started = std::time::Instant::now();
    let rows = quote_all(state, &symbols).await;
    let eligible = rows.values().filter(|l| l.eligible).count();
    match store(state, day, &rows).await {
        Ok(()) => log(&format!(
            "mtf-leverage: {} symbols quoted, {eligible} MTF-eligible, in {:.1}s",
            rows.len(),
            started.elapsed().as_secs_f64()
        )),
        Err(e) => log(&format!("mtf-leverage: store failed: {e}")),
    }
}

/// Checks every five minutes; the work runs once a day.
pub fn spawn_daily(state: AppState) {
    tokio::spawn(async move {
        loop {
            refresh_if_due(&state).await;
            tokio::time::sleep(std::time::Duration::from_secs(300)).await;
        }
    });
}

/// `GET /swing/mtf-leverage?symbols=A,B,…` — stored leverage; only symbols the morning job has
/// not covered are quoted live (in parallel) and stored for the rest of the day.
pub(super) async fn swing_mtf_leverage(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let form = FormFields::parse(uri.query().unwrap_or("").as_bytes());
    let mut symbols: Vec<String> = form
        .opt("symbols")
        .unwrap_or_default()
        .split(',')
        .map(|s| s.trim().to_uppercase())
        .filter(|s| !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || "-&_.".contains(c)))
        .collect();
    symbols.sort();
    symbols.dedup();
    if symbols.is_empty() {
        return HttpError::new(400, "symbols is required").response();
    }
    if symbols.len() > MAX_REQUEST_SYMBOLS {
        return HttpError::new(400, "at most 200 symbols").response();
    }
    let day = state.clock.now_ist().date_naive();
    let mut found = stored(&state, day, &symbols).await.unwrap_or_default();
    let missing: Vec<String> = symbols.iter().filter(|s| !found.contains_key(*s)).cloned().collect();
    // Only ask whether Kite is logged in when something must be quoted live.
    let is_authed = missing.is_empty() || authed(&state).await;
    if !missing.is_empty() && is_authed {
        let fresh = quote_all(&state, &missing).await;
        if let Err(e) = store(&state, day, &fresh).await {
            log(&format!("mtf-leverage: store failed: {e}"));
        }
        found.extend(fresh);
    }
    let mut data = PyDict::new();
    for s in &symbols {
        let entry = match found.get(s) {
            Some(l) => dict([
                ("eligible", PyValue::Bool(l.eligible)),
                ("leverage", l.leverage.map_or(PyValue::None, |v| PyValue::Str(py_dec(q2(v))))),
                ("reason", l.reason.clone().map_or(PyValue::None, PyValue::Str)),
            ]),
            None => dict([
                ("eligible", PyValue::Bool(false)),
                ("leverage", PyValue::None),
                ("reason", PyValue::Str("Connect Kite on the desk to check MTF eligibility.".to_owned())),
            ]),
        };
        data.insert(s.as_str(), PyValue::Map(entry));
    }
    let mut body = PyDict::new();
    body.insert("kite_authed", PyValue::Bool(is_authed));
    body.insert("data", PyValue::Map(data));
    json_response(200, &PyValue::Map(body))
}
