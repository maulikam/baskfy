use std::str::FromStr;

use axum::body::Body;
use axum::extract::State;
use axum::http::{Response, Uri};
use chrono::{Datelike, NaiveDateTime, Timelike};
use rust_decimal::Decimal;

use super::super::execute::{OPPS_NOTE_PREFIX, cash_product, opps_note_field, swing_gates};
use super::super::rules::{dec_of_float, py_dec, q2};
use super::open_store;
use super::super::store::SwingStore;
use crate::app::AppState;
use crate::lanes::exec_core::exec_kite::status_session;
use crate::lanes::twt::common::{FormFields, HttpError, json_response, server_error};
use crate::wire::{PyDict, PyValue, dict};

pub(super) const STALE_AFTER_SECONDS: i64 = 300;
pub(super) const QUOTE_DRIFT_PCT: &str = "1";

#[derive(Debug, Clone)]
pub(super) struct Quote {
    pub(super) price: Decimal,
    pub(super) at: Option<String>,
    pub(super) stale: bool,
}

#[derive(Debug, Clone)]
pub(super) struct MarginQuote {
    pub(super) total: Decimal,
    pub(super) charges: Decimal,
    pub(super) leverage: Option<Decimal>,
}

fn dec_of(v: Option<&PyValue>) -> Option<Decimal> {
    match v? {
        PyValue::Float(f) => Some(dec_of_float(*f)),
        PyValue::Int(i) => Some(Decimal::from(*i)),
        PyValue::Str(s) => Decimal::from_str(s.trim()).ok(),
        _ => None,
    }
}

fn map_of(v: Option<&PyValue>) -> Option<&PyDict> {
    match v? {
        PyValue::Map(m) => Some(m),
        _ => None,
    }
}

fn str_of(v: Option<&PyValue>) -> Option<String> {
    match v? {
        PyValue::Str(s) if !s.is_empty() => Some(s.clone()),
        PyValue::Int(i) => Some(i.to_string()),
        PyValue::DateTime(baskfy_providers::records::PyDateTime::Naive(t)) => {
            Some(t.format("%Y-%m-%d %H:%M:%S").to_string())
        }
        PyValue::DateTime(baskfy_providers::records::PyDateTime::Aware(t)) => Some(
            t.with_timezone(&crate::clock::ist())
                .format("%Y-%m-%d %H:%M:%S")
                .to_string(),
        ),
        _ => None,
    }
}

pub(super) fn market_open(state: &AppState) -> bool {
    let now = state.clock.now_ist();
    if baskfy_core::special_session::special_session_open(now) {
        return true;
    }
    let wd = now.weekday().num_days_from_monday();
    let minutes = now.hour() * 60 + now.minute();
    wd < 5 && (9 * 60 + 15..=15 * 60 + 30).contains(&minutes)
}

pub(super) fn mtf_interest_pct_per_day() -> Decimal {
    std::env::var("BASKFY_MTF_INTEREST_PCT_DAY")
        .ok()
        .and_then(|s| Decimal::from_str(s.trim()).ok())
        .filter(|d| *d > Decimal::ZERO)
        .unwrap_or_else(|| Decimal::new(4, 2))
}

pub(super) async fn quote(state: &AppState, symbol: &str, exchange: &str) -> Option<Quote> {
    if state.settings_snapshot().kite_api_key.is_empty() {
        return None;
    }
    let key = format!("{exchange}:{symbol}");
    let answer = state.kite.quote(std::slice::from_ref(&key)).await.ok()?;
    let PyValue::Map(m) = answer else { return None };
    let row = map_of(m.get(&key))?;
    let price = dec_of(row.get("last_price")).filter(|p| *p > Decimal::ZERO)?;
    let at = str_of(row.get("last_trade_time")).or_else(|| str_of(row.get("timestamp")));
    let stale = market_open(state)
        && at
            .as_deref()
            .and_then(|t| NaiveDateTime::parse_from_str(t.get(..19)?, "%Y-%m-%d %H:%M:%S").ok())
            .is_none_or(|t| {
                (state.clock.now_ist().naive_local() - t).num_seconds() > STALE_AFTER_SECONDS
            });
    Some(Quote { price, at, stale })
}

pub(super) async fn margin_detail(
    state: &AppState,
    symbol: &str,
    exchange: &str,
    qty: i64,
    product: &str,
    order_type: &str,
    price: Decimal,
) -> Result<MarginQuote, String> {
    if qty <= 0 {
        return Err("quantity must be positive".to_owned());
    }
    let mut order = dict([
        ("exchange", PyValue::Str(exchange.to_owned())),
        ("tradingsymbol", PyValue::Str(symbol.to_owned())),
        ("transaction_type", PyValue::Str("BUY".to_owned())),
        ("variety", PyValue::Str("regular".to_owned())),
        ("product", PyValue::Str(product.to_owned())),
        ("order_type", PyValue::Str(order_type.to_owned())),
        ("quantity", PyValue::Int(qty)),
    ]);
    if order_type == "LIMIT" {
        order.insert(
            "price",
            PyValue::Float(price.to_string().parse().unwrap_or(0.0)),
        );
    }
    let resp = state
        .kite
        .basket_order_margins(&[PyValue::Map(order)], true, None)
        .await
        .map_err(|e| e.to_string())?;
    let PyValue::Map(m) = &resp else {
        return Err("broker returned no margin".to_owned());
    };
    let line = match m.get("orders") {
        Some(PyValue::List(v)) => map_of(v.first()),
        _ => None,
    };
    let total = dec_of(map_of(m.get("final")).and_then(|f| f.get("total")))
        .or_else(|| line.and_then(|l| dec_of(l.get("total"))))
        .filter(|t| *t > Decimal::ZERO)
        .ok_or_else(|| "broker returned no margin for this order".to_owned())?;
    let charges = line
        .and_then(|l| map_of(l.get("charges")))
        .and_then(|c| dec_of(c.get("total")))
        .or_else(|| {
            map_of(m.get("final"))
                .and_then(|f| map_of(f.get("charges")))
                .and_then(|c| dec_of(c.get("total")))
        })
        .unwrap_or(Decimal::ZERO);
    let leverage = line
        .and_then(|l| dec_of(l.get("leverage")))
        .filter(|l| *l > Decimal::ZERO);
    Ok(MarginQuote {
        total,
        charges,
        leverage,
    })
}

pub(super) async fn available_funds(state: &AppState) -> Option<Decimal> {
    let m = state.kite.margins(None).await.ok()?;
    let PyValue::Map(m) = m else { return None };
    let equity = map_of(m.get("equity"))?;
    dec_of(map_of(equity.get("available")).and_then(|a| a.get("live_balance")))
        .or_else(|| dec_of(equity.get("net")))
}

async fn account(state: &AppState) -> PyValue {
    let Ok(PyValue::Map(p)) = state.kite.profile().await else {
        return PyValue::None;
    };
    PyValue::Map(dict([
        ("broker", PyValue::Str("Zerodha".to_owned())),
        (
            "user_id",
            str_of(p.get("user_id")).map_or(PyValue::None, PyValue::Str),
        ),
        (
            "user_name",
            str_of(p.get("user_name")).map_or(PyValue::None, PyValue::Str),
        ),
    ]))
}

fn dec_v(d: Option<Decimal>) -> PyValue {
    d.map_or(PyValue::None, |v| PyValue::Str(py_dec(q2(v))))
}

pub(super) async fn swing_ticket(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let form = FormFields::parse(uri.query().unwrap_or("").as_bytes());
    let symbol = form.opt("symbol").unwrap_or_default().trim().to_uppercase();
    let exchange = form
        .opt("exchange")
        .unwrap_or_else(|| "NSE".to_owned())
        .to_uppercase();
    if symbol.is_empty() {
        return HttpError::new(400, "symbol is required").response();
    }
    if exchange != "NSE" {
        return HttpError::new(400, "only NSE cash equity is supported").response();
    }
    let settings = state.settings_snapshot();
    let gates = swing_gates(&settings);
    let session = status_session(&state).await;
    let authed = matches!(session.get("authed"), Some(PyValue::Bool(true)));
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let Ok(context) = store.session_context(state.clock.now_ist().date_naive()).await else {
        return server_error();
    };
    let q = if authed { quote(&state, &symbol, &exchange).await } else { None };
    let (funds, acct) = if authed {
        (available_funds(&state).await, account(&state).await)
    } else {
        (None, PyValue::None)
    };
    let rate = mtf_interest_pct_per_day();
    let mtf = if authed {
        match margin_detail(
            &state,
            &symbol,
            &exchange,
            1,
            "MTF",
            "MARKET",
            q.as_ref().map_or(Decimal::ZERO, |x| x.price),
        )
        .await
        {
            Ok(m) => dict([
                ("eligible", PyValue::Bool(true)),
                ("reason", PyValue::None),
                ("leverage", dec_v(m.leverage)),
                ("margin_per_share", dec_v(Some(m.total))),
            ]),
            Err(e) => dict([
                ("eligible", PyValue::Bool(false)),
                (
                    "reason",
                    PyValue::Str(format!("Zerodha refused an MTF margin quote: {e}")),
                ),
                ("leverage", PyValue::None),
                ("margin_per_share", PyValue::None),
            ]),
        }
    } else {
        dict([
            ("eligible", PyValue::Bool(false)),
            (
                "reason",
                PyValue::Str("Connect Kite on the desk to check MTF eligibility.".to_owned()),
            ),
            ("leverage", PyValue::None),
            ("margin_per_share", PyValue::None),
        ])
    };
    let mut mtf = mtf;
    mtf.insert("interest_pct_per_day", PyValue::Str(py_dec(rate)));
    mtf.insert(
        "interest_basis",
        PyValue::Str(format!(
            "Estimate: {rate}% a day on the broker-funded amount (BASKFY_MTF_INTEREST_PCT_DAY, \
             default Zerodha's published MTF rate). Your contract note is the actual charge."
        )),
    );
    let mut body = PyDict::new();
    body.insert("symbol", PyValue::Str(symbol));
    body.insert("exchange", PyValue::Str(exchange));
    body.insert("kite_authed", PyValue::Bool(authed));
    body.insert("dry_run", PyValue::Bool(settings.dry_run));
    body.insert("simulated", PyValue::Bool(gates.dry_run));
    body.insert(
        "swing_execution_enabled",
        PyValue::Bool(settings.swing_execution_enabled),
    );
    body.insert("account", acct);
    body.insert("available_funds", dec_v(funds));
    body.insert(
        "quote",
        q.map_or(PyValue::None, |x| {
            PyValue::Map(dict([
                ("last_price", PyValue::Str(py_dec(q2(x.price)))),
                ("at", x.at.map_or(PyValue::None, PyValue::Str)),
                ("stale", PyValue::Bool(x.stale)),
            ]))
        }),
    );
    body.insert("market_open", PyValue::Bool(market_open(&state)));
    body.insert("mtf", PyValue::Map(mtf));
    body.insert(
        "gate",
        PyValue::Str(super::super::rules::gate_str(context.gate).to_owned()),
    );
    body.insert(
        "new_entries_allowed",
        PyValue::Bool(context.tier.new_entries_allowed),
    );
    json_response(200, &PyValue::Map(body))
}

fn display_status(line_state: &str, broker: Option<&str>, filled: i64, simulated: bool) -> &'static str {
    if simulated {
        return "SIMULATED";
    }
    match broker {
        Some("COMPLETE") => "FILLED",
        Some("REJECTED") => "REJECTED",
        Some("CANCELLED" | "CANCEL PENDING") if filled > 0 => "PARTIALLY_FILLED_CANCELLED",
        Some("CANCELLED" | "CANCEL PENDING") => "CANCELLED",
        Some("PUT ORDER REQ RECEIVED" | "VALIDATION PENDING" | "OPEN PENDING") => "SUBMITTED",
        Some(_) if filled > 0 => "PARTIALLY_FILLED",
        Some(_) => "OPEN",
        None => match line_state {
            "PROPOSED" => "NOT_SENT",
            "CONFIRMED" => "IN_FLIGHT",
            "SENT" => "SUBMITTED",
            "FILLED" => "FILLED",
            "EXPIRED" => "EXPIRED",
            _ => "REJECTED",
        },
    }
}

pub(super) async fn swing_order_status(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let mut form = FormFields::parse(uri.query().unwrap_or("").as_bytes());
    let line_id = form.int("line_id");
    if let Some(r) = form.finish() {
        return r;
    }
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let line = match store.line(line_id).await {
        Ok(Some(l)) => l,
        Ok(None) => return HttpError::new(404, "Unknown line_id").response(),
        Err(_) => return server_error(),
    };
    let order_id = line.journal_ref.clone().filter(|r| !r.is_empty());
    let simulated = order_id
        .as_deref()
        .is_some_and(|r| r.starts_with("DRY") || r.starts_with("SIM"))
        || (line.state == "FILLED" && swing_gates(&state.settings_snapshot()).dry_run && order_id.is_none());
    let mut broker = PyDict::new();
    let mut broker_status: Option<String> = None;
    let mut filled = 0;
    if let Some(id) = order_id.as_deref().filter(|_| !simulated)
        && !state.settings_snapshot().kite_api_key.is_empty()
        && let Ok(PyValue::List(history)) = state.kite.order_history(id).await
        && let Some(PyValue::Map(last)) = history.last()
    {
        broker_status = str_of(last.get("status"));
        filled = dec_of(last.get("filled_quantity"))
            .and_then(|d| d.to_string().parse::<i64>().ok())
            .unwrap_or(0);
        for k in [
            "status",
            "status_message",
            "filled_quantity",
            "pending_quantity",
            "cancelled_quantity",
            "average_price",
            "price",
            "order_type",
            "product",
            "order_timestamp",
            "exchange_order_id",
        ] {
            broker.insert(k, last.get(k).cloned().unwrap_or(PyValue::None));
        }
    }
    let status = display_status(&line.state, broker_status.as_deref(), filled, simulated);
    let mut body = PyDict::new();
    body.insert("line_id", PyValue::Int(line.id));
    body.insert("plan_id", PyValue::Str(line.plan_id.clone()));
    body.insert("symbol", PyValue::Str(line.symbol.clone()));
    body.insert("line_state", PyValue::Str(line.state.clone()));
    body.insert("product", PyValue::Str(cash_product(line.note.as_deref()).to_owned()));
    body.insert("quantity", PyValue::Int(line.quantity));
    body.insert("order_id", order_id.map_or(PyValue::None, PyValue::Str));
    body.insert("position_id", line.position_id.map_or(PyValue::None, PyValue::Int));
    body.insert("status", PyValue::Str(status.to_owned()));
    body.insert("filled_quantity", PyValue::Int(filled));
    body.insert(
        "broker",
        if broker.is_empty() { PyValue::None } else { PyValue::Map(broker) },
    );
    body.insert(
        "needs_reconcile",
        PyValue::Bool(line.state == "SENT" && filled > 0),
    );
    json_response(200, &PyValue::Map(body))
}

pub(super) async fn revalidate(state: &AppState, line: &super::super::store::LineRow) -> Option<String> {
    let note = line.note.as_deref()?;
    if !note.starts_with(OPPS_NOTE_PREFIX) || line.state != "PROPOSED" {
        return None;
    }
    if swing_gates(&state.settings_snapshot()).dry_run {
        return None;
    }
    let symbol = &line.symbol;
    let product = cash_product(Some(note));
    let order_type = opps_note_field(note, "order").unwrap_or_else(|| "MARKET".to_owned());
    let Some(q) = quote(state, symbol, "NSE").await else {
        return Some(format!("STALE_QUOTE: {symbol} — no broker quote; review again"));
    };
    if order_type == "MARKET" && q.stale {
        return Some(format!("STALE_QUOTE: {symbol} — the last trade is older than five minutes; review again"));
    }
    let reference = opps_note_field(note, "ref").and_then(|r| Decimal::from_str(&r).ok());
    if order_type == "MARKET"
        && let Some(r) = reference.filter(|r| *r > Decimal::ZERO)
    {
        let drift = ((q.price - r) / r * Decimal::ONE_HUNDRED).abs();
        if drift > Decimal::from_str(QUOTE_DRIFT_PCT).unwrap_or(Decimal::ONE) {
            return Some(format!(
                "QUOTE_MOVED: {symbol} {} → {} ({}%); review again",
                py_dec(q2(r)),
                py_dec(q2(q.price)),
                py_dec(q2(drift))
            ));
        }
    }
    let price = if order_type == "LIMIT" {
        opps_note_field(note, "price")
            .and_then(|p| Decimal::from_str(&p).ok())
            .unwrap_or(q.price)
    } else {
        q.price
    };
    let margin = match margin_detail(state, symbol, "NSE", line.quantity, product, &order_type, price).await {
        Ok(m) => m,
        Err(e) if product == "MTF" => return Some(format!("MTF_UNAVAILABLE: {symbol} — {e}")),
        Err(e) => return Some(format!("MARGIN_UNAVAILABLE: {symbol} — {e}")),
    };
    let Some(funds) = available_funds(state).await else {
        return Some("FUNDS_UNAVAILABLE: the broker did not return available funds; review again".to_owned());
    };
    let need = margin.total + margin.charges;
    if funds < need {
        return Some(format!(
            "INSUFFICIENT_FUNDS: {symbol} needs ₹{} (margin + charges), ₹{} available",
            py_dec(q2(need)),
            py_dec(q2(funds))
        ));
    }
    None
}

