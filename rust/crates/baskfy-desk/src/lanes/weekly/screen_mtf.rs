//! Confirmed screen-invest MTF buys. `POST /screen-invest/execute` sends only when
//! `confirm` is true. Each buy goes through `Gateway::place` with `screen_mtf`, then a
//! vol-floor GTT. Nothing here runs unless that confirm is present.

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderMap, Response, StatusCode, header};
use axum::routing::post;
use serde::Deserialize;
use serde_json::json;

use crate::app::AppState;
use crate::gateway::{FAILED_STATUSES, GttStop, PlaceOrder};
use crate::lanes::exec_core::stops::{StopConfig, stop_from_vol};
use crate::wire::{PyValue, dict};

/// Market protection Kite fills itself (`-1`), the same value VBT and TWT send.
const MARKET_PROTECTION: f64 = -1.0;

#[derive(Deserialize)]
struct ExecuteIn {
    confirm: bool,
    /// A stable id makes a retry idempotent; the browser's multi-buy has none and mints one.
    #[serde(default)]
    plan_id: Option<String>,
    lines: Vec<LineIn>,
}

#[derive(Deserialize)]
struct LineIn {
    symbol: String,
    qty: i64,
    price: f64,
    #[serde(default)]
    product: String,
}

#[derive(Deserialize)]
struct LeverageIn {
    lines: Vec<LeverageLine>,
}

#[derive(Deserialize)]
struct LeverageLine {
    symbol: String,
    price: f64,
}

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/screen-invest/leverage", post(leverage))
        .route("/screen-invest/execute", post(execute))
}

fn cached_leverage() -> &'static Mutex<HashMap<String, (f64, Instant)>> {
    static CACHE: LazyLock<Mutex<HashMap<String, (f64, Instant)>>> =
        LazyLock::new(|| Mutex::new(HashMap::new()));
    &CACHE
}

async fn leverage(
    State(state): State<AppState>,
    _headers: HeaderMap,
    body: Bytes,
) -> Response<Body> {
    let parsed: LeverageIn = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => {
            return json_response(
                StatusCode::BAD_REQUEST,
                json!({"detail": "lines are required"}),
            );
        }
    };
    let mut lines = Vec::new();
    for line in parsed.lines.into_iter().take(20) {
        let symbol = line.symbol.trim().to_ascii_uppercase();
        if symbol.is_empty() || !line.price.is_finite() || line.price <= 0.0 {
            continue;
        }
        let (lev, note) = broker_leverage(&state, &symbol, line.price).await;
        let product = if lev > 1.05 { "MTF" } else { "CNC" };
        lines.push(json!({
            "symbol": symbol,
            "leverage": format!("{lev:.2}"),
            "product": product,
            "note": note,
        }));
    }
    json_response(StatusCode::OK, json!({"lines": lines}))
}

async fn broker_leverage(state: &AppState, symbol: &str, price: f64) -> (f64, String) {
    if let Ok(cache) = cached_leverage().lock()
        && let Some((lev, at)) = cache.get(symbol)
        && at.elapsed().as_secs() < 30 * 60
    {
        return (*lev, String::new());
    }
    let basket = vec![PyValue::Map(dict([
        ("exchange", PyValue::Str("NSE".to_owned())),
        ("tradingsymbol", PyValue::Str(symbol.to_owned())),
        ("transaction_type", PyValue::Str("BUY".to_owned())),
        ("variety", PyValue::Str("regular".to_owned())),
        ("product", PyValue::Str("MTF".to_owned())),
        ("order_type", PyValue::Str("LIMIT".to_owned())),
        ("quantity", PyValue::Int(1)),
        ("price", PyValue::Float(price)),
    ]))];
    let kite = match crate::lanes::exec_core::exec_kite::kite(state) {
        Ok(kite) => kite,
        Err(e) => return (1.0, e.message.chars().take(180).collect()),
    };
    let (lev, note) = match kite.order_margin_basket(&basket).await {
        Ok(PyValue::Map(map)) => leverage_from_margin(&map, price),
        Ok(other) => (
            1.0,
            format!("margin reply {other:?}")
                .chars()
                .take(180)
                .collect(),
        ),
        Err(e) => (1.0, e.to_string().chars().take(180).collect()),
    };
    if note.is_empty()
        && let Ok(mut cache) = cached_leverage().lock()
    {
        cache.insert(symbol.to_owned(), (lev, Instant::now()));
    }
    (lev, note)
}

fn num_of(value: Option<&PyValue>) -> f64 {
    match value {
        Some(PyValue::Float(v)) => *v,
        Some(PyValue::Int(v)) => *v as f64,
        Some(PyValue::Str(s)) => s.parse().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn leverage_from_margin(map: &crate::wire::PyDict, price: f64) -> (f64, String) {
    let keys: Vec<&str> = map.iter().map(|(k, _)| k).collect();
    let direct = num_of(map.get("leverage"));
    let block = map.get("final").and_then(|v| match v {
        PyValue::Map(m) => Some(m),
        _ => None,
    });
    let stated = num_of(block.and_then(|m| m.get("leverage"))).max(direct);
    if stated > 1.05 {
        return (stated.min(10.0), String::new());
    }
    let total = num_of(block.and_then(|m| m.get("total")));
    if total > 0.0 && price > 0.0 && total < price * 0.98 {
        let lev = price / total;
        if lev.is_finite() && lev > 1.05 {
            return (lev.min(10.0), String::new());
        }
    }
    (1.0, format!("no MTF leverage (keys {})", keys.join(",")))
}

fn json_response(status: StatusCode, body: serde_json::Value) -> Response<Body> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body.to_string()))
        .unwrap_or_else(|_| Response::new(Body::from("{}")))
}

fn text(map: &crate::wire::PyDict, key: &str) -> String {
    match map.get(key) {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

async fn execute(
    State(state): State<AppState>,
    _headers: HeaderMap,
    body: Bytes,
) -> Response<Body> {
    let parsed: ExecuteIn = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => {
            return json_response(
                StatusCode::BAD_REQUEST,
                json!({"detail": "confirm and lines are required"}),
            );
        }
    };
    if !parsed.confirm {
        return json_response(
            StatusCode::BAD_REQUEST,
            json!({"detail": "MTF orders are sent only with confirm true"}),
        );
    }
    let plan_id = match parsed.plan_id.as_deref().map(str::trim) {
        Some(id) if !id.is_empty() => id.to_owned(),
        _ => {
            let millis = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_millis())
                .unwrap_or(0);
            format!("s{millis:x}")
        }
    };
    let mut orders = Vec::new();
    for line in &parsed.lines {
        let symbol = line.symbol.trim().to_ascii_uppercase();
        if symbol.is_empty() || line.qty <= 0 || !line.price.is_finite() || line.price <= 0.0 {
            continue;
        }
        let gross = line.price * line.qty as f64;
        let mtf = line.product.trim().eq_ignore_ascii_case("MTF");
        let placed = state
            .gateway
            .place(PlaceOrder {
                symbol: symbol.clone(),
                qty: line.qty,
                side: "BUY".to_owned(),
                product: if mtf { "MTF" } else { "CNC" }.to_owned(),
                order_type: "MARKET".to_owned(),
                price: None,
                exchange: "NSE".to_owned(),
                client_id: Some(format!("{plan_id}:{symbol}")),
                gross_exposure: Some(gross),
                series: Some(series_of(&symbol).to_owned()),
                market_protection: Some(MARKET_PROTECTION),
                reference_price: Some(line.price),
                screen_mtf: mtf,
                ..PlaceOrder::default()
            })
            .await;
        let (status, order_id, error, stop) = match placed {
            Ok(res) => {
                let unknown = outcome_unknown(&res);
                let status = if unknown { "UNKNOWN".to_owned() } else { text(&res, "status") };
                let order_id = if unknown { String::new() } else { text(&res, "order_id") };
                let error = text(&res, "error");
                let stop = if matches!(status.as_str(), "PLACED" | "DRY_RUN" | "DUPLICATE") {
                    arm_stop(&state, &plan_id, &symbol, line.qty, line.price).await
                } else {
                    String::new()
                };
                (status, order_id, error, stop)
            }
            Err(e) => (
                "ERROR".to_owned(),
                String::new(),
                e.to_string(),
                String::new(),
            ),
        };
        let failed = FAILED_STATUSES.contains(&status.as_str());
        orders.push(json!({
            "symbol": symbol,
            "status": status,
            "order_id": if order_id.is_empty() { serde_json::Value::Null } else { json!(order_id) },
            "stop": if stop.is_empty() { serde_json::Value::Null } else { json!(stop) },
            "error": if error.is_empty() || !failed && status != "UNKNOWN" {
                serde_json::Value::Null
            } else {
                json!(error)
            },
        }));
    }
    json_response(
        StatusCode::OK,
        json!({"plan_id": plan_id, "orders": orders}),
    )
}

/// The broker may or may not hold this order: an unanswered send, or a reservation still open.
fn outcome_unknown(res: &crate::wire::PyDict) -> bool {
    let status = text(res, "status");
    (status == "ERROR" && res.get("reached_exchange") == Some(&PyValue::None))
        || (status == "DUPLICATE" && text(res, "order_id").starts_with("PENDING-"))
}

/// The series in a Kite symbol such as `ABC-BE`. Only a two-letter suffix is one: `BAJAJ-AUTO`
/// is a name.
fn series_of(symbol: &str) -> &str {
    match symbol.rsplit_once('-') {
        Some((_, suffix)) if suffix.len() == 2 && suffix.bytes().all(|b| b.is_ascii_alphabetic()) => {
            suffix
        }
        _ => "EQ",
    }
}

async fn arm_stop(state: &AppState, plan_id: &str, symbol: &str, qty: i64, price: f64) -> String {
    let trigger = stop_from_vol(price, 0.0, StopConfig::default());
    match state
        .gateway
        .place_gtt_stop(GttStop {
            symbol: symbol.to_owned(),
            qty,
            trigger,
            last_price: price,
            exchange: "NSE".to_owned(),
            series: Some("EQ".to_owned()),
            client_id: Some(format!("{plan_id}:{symbol}:stop")),
            tenant: None,
            plan_tenant: None,
            limit_fraction: None,
            fo_plan: None,
        })
        .await
    {
        Ok(res) => {
            let status = text(&res, "status");
            if status.is_empty() {
                "UNKNOWN".to_owned()
            } else {
                status
            }
        }
        Err(e) => e.to_string(),
    }
}
