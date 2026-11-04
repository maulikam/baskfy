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
use super::open_store;
use crate::app::AppState;
use crate::lanes::exec_core::exec_kite::status_session;
use crate::lanes::twt::common::{FormFields, HttpError, json_response, server_error};
use crate::storage::Param;
use crate::wire::{PyDict, PyValue, dict};

#[allow(clippy::too_many_lines)]
pub async fn swing_analyze(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let symbol = form.str("symbol").trim().to_uppercase();
    let instrument_id = form.int("instrument_id");
    let setup = form.str("setup");
    let amount_raw = form.str("amount_inr");
    let product_raw = form.str("product");
    let last_raw = form.str("last_price");
    let trigger_raw = form.str("trigger");
    let stop_raw = form.str("stop");
    let lot_size = form.int("lot_size");
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
    let Ok(amount) = Decimal::from_str(amount_raw.trim()) else {
        return HttpError::new(400, "amount_inr must be a number of rupees").response();
    };
    if amount <= Decimal::ZERO {
        return HttpError::new(400, "amount_inr must be positive").response();
    }
    let Ok(form_price) = Decimal::from_str(last_raw.trim()) else {
        return HttpError::new(400, "last_price must be a price").response();
    };
    let Ok(trigger) = Decimal::from_str(trigger_raw.trim()) else {
        return HttpError::new(400, "trigger must be a price").response();
    };
    let Ok(stop) = Decimal::from_str(stop_raw.trim()) else {
        return HttpError::new(400, "stop must be a price").response();
    };
    if stop >= trigger {
        return HttpError::new(400, "stop must be below the trigger").response();
    }
    let lot = lot_size.max(1);
    let settings = state.settings_snapshot();
    let gates = swing_gates(&settings);
    let session = status_session(&state).await;
    let kite_authed = matches!(session.get("authed"), Some(PyValue::Bool(true)));
    let live = super::last_price(&state, &symbol).await;
    let price = live.unwrap_or(form_price);
    if price <= Decimal::ZERO {
        return HttpError::new(400, "last_price must be positive").response();
    }
    let shares = (amount / price).trunc();
    let mut qty: i64 = shares.to_string().parse().unwrap_or(0);
    qty = (qty / lot) * lot;
    let now = state.clock.now_ist();
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let day = now.date_naive();
    let context = match store.session_context(day).await {
        Ok(c) => c,
        Err(_) => return server_error(),
    };
    let mut skip = String::new();
    if qty <= 0 {
        skip = format!("{symbol}: amount {amount} is below one lot at {price}");
    } else if !context.tier.new_entries_allowed {
        skip = format!(
            "GATE_RED: {symbol} — new entries are closed (rung {})",
            context.tier.level
        );
    }
    let simulated = gates.dry_run;
    let gtt_note = if product == "MTF" {
        "A same-session GTT stop is still required (vol-scaled 8–12%). Kite GTT legs are CNC \
         cash triggers — Kite GTT does not accept product MTF — so the stop sells the shares, \
         it does not attach to the MTF facility. If Kite refuses the GTT, arm it from the desk."
            .to_owned()
    } else {
        "A same-session GTT stop is required (vol-scaled 8–12%).".to_owned()
    };
    let mut margin: Option<Decimal> = None;
    if skip.is_empty() && qty > 0 {
        margin = margin_quote(&state, &symbol, qty, &product).await;
    }
    let mut plan_id = String::new();
    let mut line_id: Option<i64> = None;
    let mut expires_at = None;
    if skip.is_empty() {
        let built = persist_plan(
            &mut store,
            &state,
            now,
            &symbol,
            instrument_id,
            &setup,
            qty,
            &product,
            &amount,
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
    let mut body = PyDict::new();
    body.insert("confirmable", PyValue::Bool(skip.is_empty() && line_id.is_some()));
    body.insert(
        "plan_id",
        if plan_id.is_empty() {
            PyValue::None
        } else {
            PyValue::Str(plan_id)
        },
    );
    body.insert(
        "line_id",
        line_id.map_or(PyValue::None, PyValue::Int),
    );
    body.insert("symbol", PyValue::Str(symbol));
    body.insert("product", PyValue::Str(product));
    body.insert("quantity", PyValue::Int(qty));
    body.insert("amount_inr", PyValue::Str(py_dec(q2(amount))));
    body.insert("last_price", PyValue::Str(py_dec(q2(price))));
    body.insert(
        "required_margin_inr",
        margin.map_or(PyValue::None, |m| PyValue::Str(py_dec(q2(m)))),
    );
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
    product: &str,
    amount: &Decimal,
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
    let note = format!("{OPPS_NOTE_PREFIX}product={product} amount={amount}");
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

async fn margin_quote(
    state: &crate::app::AppState,
    symbol: &str,
    qty: i64,
    product: &str,
) -> Option<Decimal> {
    if qty <= 0 {
        return None;
    }
    let basket = [PyValue::Map(dict([
        ("exchange", PyValue::Str("NSE".to_owned())),
        ("tradingsymbol", PyValue::Str(symbol.to_owned())),
        ("transaction_type", PyValue::Str("BUY".to_owned())),
        ("variety", PyValue::Str("regular".to_owned())),
        ("product", PyValue::Str(product.to_owned())),
        ("order_type", PyValue::Str("MARKET".to_owned())),
        ("quantity", PyValue::Int(qty)),
    ]))];
    let resp = state
        .kite
        .basket_order_margins(&basket, true, None)
        .await
        .ok()?;
    let fin = match &resp {
        PyValue::Map(m) => m.get("final").cloned().unwrap_or(PyValue::None),
        _ => PyValue::None,
    };
    let total = match &fin {
        PyValue::Map(m) => m.get("total").cloned().unwrap_or(PyValue::None),
        _ => PyValue::None,
    };
    match total {
        PyValue::Float(f) if f > 0.0 => Some(dec_of_float(f)),
        PyValue::Int(i) if i > 0 => Some(Decimal::from(i)),
        PyValue::Str(s) => Decimal::from_str(s.trim()).ok().filter(|d| *d > Decimal::ZERO),
        _ => None,
    }
}
