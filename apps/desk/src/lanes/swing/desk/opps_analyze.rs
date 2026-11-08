//! `POST /swing/analyze` — Opportunities row Buy sizes a one-line SIGNAL plan from a rupee
//! amount. It writes rows; it never places. Confirm is `POST /swing/execute`.

use std::str::FromStr;

use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::Response;
use rust_decimal::Decimal;

use super::super::execute::{OPPS_NOTE_PREFIX, swing_gates};
use super::super::rules::{PLAN_TTL_MINUTES, dec_of_float, gate_str, py_dec, q2};
use super::super::store::SwingStore;
use super::super::swing_monitor::runner::uuid_text;
use super::super::values::{Val, bind, col, int};
use super::{open_store, opps_ticket};
use crate::app::AppState;
use crate::lanes::exec_core::exec_kite::status_session;
use crate::lanes::twt::common::{FormFields, HttpError, json_response, server_error};
use crate::storage::Param;
use crate::wire::{PyDict, PyValue};

fn opt_dec(form: &FormFields, name: &str) -> Result<Option<Decimal>, Box<Response<Body>>> {
    match form.opt(name) {
        None => Ok(None),
        Some(v) => Decimal::from_str(v.trim()).map(Some).map_err(|_| {
            Box::new(HttpError::new(400, format!("{name} must be a number")).response())
        }),
    }
}

fn book_setup(setup: &str) -> &'static str {
    match setup.trim().to_uppercase().as_str() {
        "EP" | "EPISODIC_PIVOT" => "EP",
        _ => "FLAG",
    }
}

#[allow(clippy::too_many_lines)]
pub async fn swing_analyze(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let symbol = form.str("symbol").trim().to_uppercase();
    let instrument_id = form.int("instrument_id");
    let product_raw = form.str("product");
    let last_raw = form.str("last_price");
    let setup = form.opt("setup").unwrap_or_default();
    let source = form
        .opt("source")
        .unwrap_or_else(|| "opportunities".to_owned());
    let order_type = form
        .opt("order_type")
        .unwrap_or_else(|| "MARKET".to_owned())
        .trim()
        .to_uppercase();
    let basis = form
        .opt("amount_basis")
        .unwrap_or_else(|| "order_value".to_owned());
    let lot = form
        .opt("lot_size")
        .and_then(|v| v.trim().parse::<i64>().ok())
        .unwrap_or(1)
        .max(1);
    let qty_in = form
        .opt("quantity")
        .and_then(|v| v.trim().parse::<i64>().ok())
        .unwrap_or(0);
    let (amount, limit, trigger_in, stop_in) = match (
        opt_dec(&form, "amount_inr"),
        opt_dec(&form, "limit_price"),
        opt_dec(&form, "trigger"),
        opt_dec(&form, "stop"),
    ) {
        (Ok(a), Ok(l), Ok(t), Ok(s)) => (a, l, t, s),
        (Err(r), ..) | (_, Err(r), ..) | (_, _, Err(r), _) | (.., Err(r)) => return *r,
    };
    if let Some(r) = form.finish() {
        return r;
    }
    if symbol.is_empty() {
        return HttpError::new(400, "symbol is required").response();
    }
    if instrument_id <= 0 {
        return HttpError::new(400, "instrument_id is required").response();
    }
    let product = product_raw.trim().to_uppercase();
    if product != "CNC" && product != "MTF" {
        return HttpError::new(
            400,
            "product must be CNC or MTF (MIS stays behind INTRADAY_ENABLED)",
        )
        .response();
    }
    if order_type != "MARKET" && order_type != "LIMIT" {
        return HttpError::new(400, "order_type must be MARKET or LIMIT").response();
    }
    if basis != "order_value" && basis != "own" {
        return HttpError::new(400, "amount_basis must be order_value or own").response();
    }
    let limit = limit.filter(|l| *l > Decimal::ZERO);
    if order_type == "LIMIT" && limit.is_none() {
        return HttpError::new(400, "a LIMIT order needs a positive limit_price").response();
    }
    if qty_in <= 0 && !amount.is_some_and(|a| a > Decimal::ZERO) {
        return HttpError::new(400, "give a positive quantity or amount_inr").response();
    }
    let Ok(form_price) = Decimal::from_str(last_raw.trim()) else {
        return HttpError::new(400, "last_price must be a price").response();
    };
    let settings = state.settings_snapshot();
    let gates = swing_gates(&settings);
    let simulated = gates.dry_run;
    let session = status_session(&state).await;
    let kite_authed = matches!(session.get("authed"), Some(PyValue::Bool(true)));
    let q = if kite_authed {
        opps_ticket::quote(&state, &symbol, "NSE").await
    } else {
        None
    };
    let quote_price = q.as_ref().map_or(form_price, |x| x.price);
    let stale = q.as_ref().is_none_or(|x| x.stale);
    if quote_price <= Decimal::ZERO {
        return HttpError::new(400, "last_price must be positive").response();
    }
    let price = if order_type == "LIMIT" {
        limit.unwrap_or(quote_price)
    } else {
        quote_price
    };
    let band_mid = (dec_of_float(settings.swing_stop_band_min)
        + dec_of_float(settings.swing_stop_band_max))
        / Decimal::TWO;
    let (stop, stop_basis) = match stop_in.filter(|s| *s > Decimal::ZERO) {
        Some(s) => (s, "setup"),
        None => (q2(price * (Decimal::ONE - band_mid)), "band"),
    };
    let trigger = trigger_in.filter(|t| *t > Decimal::ZERO).unwrap_or(price);
    if stop >= trigger || stop >= price {
        return HttpError::new(400, "stop must be below the trigger and the order price")
            .response();
    }
    let lot_floor = |n: i64| (n / lot) * lot;
    let to_i = |d: Decimal| -> i64 { d.trunc().to_string().parse().unwrap_or(0) };
    let mut qty = if qty_in > 0 {
        lot_floor(qty_in)
    } else {
        lot_floor(to_i(amount.unwrap_or(Decimal::ZERO) / price))
    };
    let mut margin: Option<opps_ticket::MarginQuote> = None;
    let mut margin_err: Option<String> = None;
    if kite_authed && qty > 0 {
        if qty_in <= 0 && basis == "own" && product == "MTF" {
            let budget = amount.unwrap_or(Decimal::ZERO);
            match opps_ticket::margin_detail(
                &state,
                &symbol,
                "NSE",
                lot,
                &product,
                &order_type,
                price,
            )
            .await
            {
                Ok(one) if one.total > Decimal::ZERO => {
                    qty = lot_floor(to_i(budget / (one.total / Decimal::from(lot))));
                }
                Ok(_) => {}
                Err(e) => margin_err = Some(e),
            }
            for _ in 0..4 {
                if qty <= 0 || margin_err.is_some() {
                    break;
                }
                match opps_ticket::margin_detail(
                    &state,
                    &symbol,
                    "NSE",
                    qty,
                    &product,
                    &order_type,
                    price,
                )
                .await
                {
                    Ok(m) if m.total + m.charges > budget => {
                        let per = m.total / Decimal::from(qty);
                        let over = ((m.total + m.charges - budget) / per).ceil();
                        qty = lot_floor(qty - to_i(over).max(1));
                    }
                    Ok(m) => {
                        margin = Some(m);
                        break;
                    }
                    Err(e) => margin_err = Some(e),
                }
            }
        } else {
            match opps_ticket::margin_detail(
                &state,
                &symbol,
                "NSE",
                qty,
                &product,
                &order_type,
                price,
            )
            .await
            {
                Ok(m) => margin = Some(m),
                Err(e) => margin_err = Some(e),
            }
        }
    }
    let funds = if kite_authed {
        opps_ticket::available_funds(&state).await
    } else {
        None
    };
    let now = state.clock.now_ist();
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let context = match store.session_context(now.date_naive()).await {
        Ok(c) => c,
        Err(_) => return server_error(),
    };
    let order_value = q2(price * Decimal::from(qty.max(0)));
    let own = margin.as_ref().map(|m| m.total);
    let charges = margin.as_ref().map(|m| m.charges);
    let funding = own.map(|o| (order_value - o).max(Decimal::ZERO));
    let rate = opps_ticket::mtf_interest_pct_per_day();
    let interest = if product == "MTF" {
        funding.map(|f| q2(f * rate / Decimal::ONE_HUNDRED))
    } else {
        None
    };
    let mut skip = String::new();
    if qty <= 0 {
        skip = format!(
            "{symbol}: the amount is below one lot at {}",
            py_dec(q2(price))
        );
    } else if !context.tier.new_entries_allowed {
        skip = format!(
            "GATE_RED: {symbol} — new entries are closed (rung {})",
            context.tier.level
        );
    } else if context
        .per_stock
        .as_ref()
        .is_some_and(|g| !g.allows(&symbol))
    {
        skip = format!(
            "REGIME_RED: {symbol} — none of its indices is in an uptrend (per-stock regime)"
        );
    } else if product == "MTF" && margin.is_none() {
        skip = format!(
            "MTF_UNAVAILABLE: {}",
            margin_err
                .clone()
                .unwrap_or_else(|| "connect Kite to quote MTF margin".to_owned())
        );
    } else if !simulated && !kite_authed {
        skip = "KITE_NOT_CONNECTED: connect Kite on the desk before a live order".to_owned();
    } else if !simulated && margin.is_none() {
        skip = format!(
            "MARGIN_UNAVAILABLE: {}",
            margin_err
                .clone()
                .unwrap_or_else(|| "the broker returned no margin".to_owned())
        );
    } else if !simulated && order_type == "MARKET" && stale {
        skip = format!(
            "STALE_QUOTE: {symbol} — no fresh broker quote; a live market order is not sent on it"
        );
    } else if !simulated && funds.is_none() {
        skip = "FUNDS_UNAVAILABLE: the broker did not return available funds".to_owned();
    } else if !simulated
        && let (Some(f), Some(o), Some(c)) = (funds, own, charges)
        && f < o + c
    {
        skip = format!(
            "INSUFFICIENT_FUNDS: needs ₹{} (margin + charges), ₹{} available",
            py_dec(q2(o + c)),
            py_dec(q2(f))
        );
    }
    let gtt_note = if product == "MTF" {
        "A same-session GTT stop is placed after the fill (vol-scaled 8–12%). Kite GTT does not \
         accept product MTF, so the stop is a CNC cash trigger that sells the shares."
            .to_owned()
    } else {
        "A same-session GTT stop is placed after the fill (vol-scaled 8–12%).".to_owned()
    };
    let mut plan_id = String::new();
    let mut line_id: Option<i64> = None;
    let mut expires_at = None;
    if skip.is_empty() {
        let mut note = format!(
            "product={product} amount={} order={order_type} ref={} src={} setup={}",
            py_dec(q2(amount.unwrap_or(order_value))),
            py_dec(q2(quote_price)),
            source.split_whitespace().collect::<String>(),
            if setup.trim().is_empty() {
                "-".to_owned()
            } else {
                setup.split_whitespace().collect::<String>()
            },
        );
        if let Some(l) = limit.filter(|_| order_type == "LIMIT") {
            note.push_str(&format!(" price={}", py_dec(q2(l))));
        }
        let built = persist_plan(
            &mut store,
            &state,
            now,
            &symbol,
            instrument_id,
            book_setup(&setup),
            qty,
            &note,
            trigger,
            stop,
            price,
            context.tier.level,
            gate_str(context.gate),
        )
        .await;
        match built {
            Ok((pid, lid, exp)) => {
                plan_id = pid;
                line_id = Some(lid);
                expires_at = Some(exp);
            }
            Err(_) => return server_error(),
        }
    }
    let dv = |d: Option<Decimal>| d.map_or(PyValue::None, |v| PyValue::Str(py_dec(q2(v))));
    let mut body = PyDict::new();
    body.insert(
        "confirmable",
        PyValue::Bool(skip.is_empty() && line_id.is_some()),
    );
    body.insert(
        "plan_id",
        if plan_id.is_empty() {
            PyValue::None
        } else {
            PyValue::Str(plan_id)
        },
    );
    body.insert("line_id", line_id.map_or(PyValue::None, PyValue::Int));
    body.insert("symbol", PyValue::Str(symbol));
    body.insert("exchange", PyValue::Str("NSE".to_owned()));
    body.insert("product", PyValue::Str(product));
    body.insert("order_type", PyValue::Str(order_type));
    body.insert("limit_price", dv(limit));
    body.insert("quantity", PyValue::Int(qty.max(0)));
    body.insert("amount_inr", dv(amount));
    body.insert("amount_basis", PyValue::Str(basis));
    body.insert("last_price", PyValue::Str(py_dec(q2(quote_price))));
    body.insert("order_price", PyValue::Str(py_dec(q2(price))));
    body.insert(
        "quote_at",
        q.as_ref()
            .and_then(|x| x.at.clone())
            .map_or(PyValue::None, PyValue::Str),
    );
    body.insert("quote_live", PyValue::Bool(q.is_some()));
    body.insert("stale_quote", PyValue::Bool(stale));
    body.insert("order_value", PyValue::Str(py_dec(order_value)));
    body.insert("required_margin_inr", dv(own));
    body.insert("own_contribution", dv(own));
    body.insert("broker_funding", dv(funding));
    body.insert("charges", dv(charges));
    body.insert("leverage", dv(margin.as_ref().and_then(|m| m.leverage)));
    body.insert("available_funds", dv(funds));
    body.insert(
        "margin_error",
        margin_err.map_or(PyValue::None, PyValue::Str),
    );
    body.insert("mtf_interest_pct_per_day", PyValue::Str(py_dec(rate)));
    body.insert("mtf_interest_per_day", dv(interest));
    body.insert("stop", PyValue::Str(py_dec(q2(stop))));
    body.insert("stop_basis", PyValue::Str(stop_basis.to_owned()));
    body.insert("trigger", PyValue::Str(py_dec(q2(trigger))));
    body.insert(
        "skip",
        if skip.is_empty() {
            PyValue::None
        } else {
            PyValue::Str(skip)
        },
    );
    body.insert("simulated", PyValue::Bool(simulated));
    body.insert("kite_authed", PyValue::Bool(kite_authed));
    body.insert("dry_run", PyValue::Bool(settings.dry_run));
    body.insert(
        "swing_execution_enabled",
        PyValue::Bool(settings.swing_execution_enabled),
    );
    body.insert(
        "expires_at",
        expires_at.map_or(PyValue::None, |t| {
            PyValue::Str(t.format("%Y-%m-%dT%H:%M:%S%z").to_string())
        }),
    );
    body.insert("gtt_note", PyValue::Str(gtt_note));
    body.insert("gate", PyValue::Str(gate_str(context.gate).to_owned()));
    json_response(200, &PyValue::Map(body))
}

#[allow(clippy::too_many_arguments)]
async fn persist_plan(
    store: &mut super::super::store::PgSwingStore,
    state: &AppState,
    now: chrono::DateTime<chrono::FixedOffset>,
    symbol: &str,
    instrument_id: i64,
    setup: &str,
    qty: i64,
    note_body: &str,
    trigger: Decimal,
    stop: Decimal,
    price: Decimal,
    exposure_level: i64,
    gate: &str,
) -> Result<(String, i64, chrono::DateTime<chrono::FixedOffset>), String> {
    let plan_id = uuid_text(state.ids.uuid4_bytes());
    let expires_at = now + chrono::Duration::minutes(PLAN_TTL_MINUTES);
    let risk = q2((trigger - stop) * Decimal::from(qty));
    let value = q2(price * Decimal::from(qty));
    let note = format!("{OPPS_NOTE_PREFIX}{note_body}");
    let plan_hash = format!("opps:{plan_id}");
    let plan_row = store
        .conn
        .execute(
            &format!(
                "INSERT INTO {} (plan_id, user_id, as_of, source, built_at, expires_at, \
                 plan_hash, gate, exposure_level, total_risk_inr, total_new_exposure_inr) \
                 VALUES (?, ?, ?, 'SIGNAL', ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                store.t("sw_plan")
            ),
            &[
                Param::Text(plan_id.clone()),
                Param::Int(store.user_id),
                Param::Date(now.date_naive()),
                Param::Aware(now),
                Param::Aware(expires_at),
                Param::Text(plan_hash),
                Param::Text(gate.to_owned()),
                Param::Int(exposure_level),
                bind(&Val::Dec(risk), "total_risk_inr"),
                bind(&Val::Dec(value), "total_new_exposure_inr"),
            ],
        )
        .await
        .map_err(|e| e.to_string())?;
    let plan_pk = plan_row
        .rows
        .first()
        .map(|r| int(col(r, "id")))
        .filter(|i| *i > 0)
        .ok_or_else(|| "no plan id".to_owned())?;
    let setup_param = if setup.trim().is_empty() {
        Param::Null
    } else {
        Param::Text(setup.trim().to_owned())
    };
    let line_row = store
        .conn
        .execute(
            &format!(
                "INSERT INTO {} (plan_id, user_id, kind, instrument_id, setup, quantity, \
                 trigger, stop, risk_inr, position_value, trail, note, state, client_id) \
                 VALUES (?, ?, 'BUY_ON_TRIGGER', ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'PROPOSED', ?) \
                 RETURNING id",
                store.t("sw_plan_line")
            ),
            &[
                Param::Int(plan_pk),
                Param::Int(store.user_id),
                Param::Int(instrument_id),
                setup_param,
                Param::Int(qty),
                bind(&Val::Dec(trigger), "trigger"),
                bind(&Val::Dec(stop), "stop"),
                bind(&Val::Dec(risk), "risk_inr"),
                bind(&Val::Dec(value), "position_value"),
                Param::Text(note),
                Param::Text(format!("{plan_id}:{symbol}:BUY_ON_TRIGGER")),
            ],
        )
        .await
        .map_err(|e| e.to_string())?;
    let line_id = line_row
        .rows
        .first()
        .map(|r| int(col(r, "id")))
        .filter(|i| *i > 0)
        .ok_or_else(|| "no line id".to_owned())?;
    Ok((plan_id, line_id, expires_at))
}
