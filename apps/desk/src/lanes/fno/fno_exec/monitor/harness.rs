//! `POST /__harness/fno-monitor` — **harness-only** (`--harness`; `tools/parity/desk_diff.py
//! --plans`, `desk_cases/plans_fno.json`): drive the real FO monitor and executor on the side's
//! database through the side's real paper-pinned FO gateway, with the market seams (books, the
//! index, the margin, the NFO master, the calendar and the bhavcopy) given in the body, so the
//! fno plan kinds — F1/F2/F3 entries, F3's add, EXIT and ROLL plans, the night's marks and F2's
//! trail — and every fill they lead to are graded table for table and Kite call for Kite call
//! against Python's (`desk_harness.py` mounts the same route over `app.fno_monitor`). Without
//! the harness every method answers Starlette's 404: production cannot tell it exists.
//!
//! Body: `op` (`tick` | `night` | `confirm` | `click`), `at` (RFC 3339; `tick`, `confirm`,
//! `click`), `day` (`night`), `plan_id` (`confirm`, `click`), `books` (`{symbol: {bid, ask,
//! last, depth}}`), `index` (`{underlying: {last, open}}`), `margin` (`{required, free}` or
//! null), `options` / `futures` (the master), `sessions` (`{from, days}`: the weekdays),
//! `landed` and `prints` (the bhavcopy), `auto_exit` (M.5's answer for this call; under the
//! harness `DRY_RUN` is true and the FO gateway is paper-pinned whatever it says).

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderValue, Method, Response, StatusCode};
use axum::routing::any;
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, Weekday};
use rust_decimal::Decimal;

use super::super::execute::{
    ExecError, FoCtx, execute_click_exit, execute_entry, fo_gateway, fo_gateway_ready,
    outcome_json, paper_book,
};
use super::super::store::{FoQuote, FoStore};
use super::engine::FnoMonitor;
use super::seams::{
    Contract, IndexQuote, MarginQuote, Print, PrintKey, SharedBooks, StaticIndex,
    StaticInstruments, StaticMargin, StaticMarket,
};
use crate::app::AppState;
use crate::lanes::options::py;
use crate::wire::{PyDict, PyValue, dict, orjson_dumps};
use baskfy_core::options::execution::Level;

/// The harness route (merged by the fno lane's router; inert without `--harness`).
pub fn routes() -> Router<AppState> {
    Router::new().route("/__harness/fno-monitor", any(fno_monitor))
}

fn json(status: StatusCode, value: &PyValue) -> Response<Body> {
    let mut r = Response::new(Body::from(orjson_dumps(value).unwrap_or_default()));
    *r.status_mut() = status;
    r.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    r
}

fn detail(status: StatusCode, text: &str) -> Response<Body> {
    json(
        status,
        &PyValue::Map(dict([("detail", PyValue::Str(text.to_owned()))])),
    )
}

#[allow(clippy::print_stderr)]
fn server_error(e: &str) -> Response<Body> {
    eprintln!("ERROR fno_monitor harness failed: {e}");
    let mut r = Response::new(Body::from("Internal Server Error"));
    *r.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
    r.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    r
}

async fn fno_monitor(State(state): State<AppState>, method: Method, body: Bytes) -> Response<Body> {
    if state.harness.is_none() {
        return detail(StatusCode::NOT_FOUND, "Not Found");
    }
    if method != Method::POST {
        let mut r = detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
        r.headers_mut()
            .insert(axum::http::header::ALLOW, HeaderValue::from_static("POST"));
        return r;
    }
    match drive(&state, &body).await {
        Ok(v) => json(StatusCode::OK, &v),
        Err(e) => server_error(&e),
    }
}

type J = serde_json::Value;

fn s_of(v: Option<&J>) -> Option<String> {
    match v? {
        J::Null => None,
        J::String(s) => Some(s.clone()),
        other => Some(other.to_string()),
    }
}

fn dec_of(v: Option<&J>) -> Result<Option<Decimal>, String> {
    match s_of(v) {
        None => Ok(None),
        Some(t) => py::dec_from_str(&t)
            .map(Some)
            .ok_or_else(|| format!("InvalidOperation: {t}")),
    }
}

fn need_dec(v: Option<&J>, what: &str) -> Result<Decimal, String> {
    dec_of(v)?.ok_or_else(|| format!("KeyError: '{what}'"))
}

fn date_of(v: Option<&J>) -> Result<NaiveDate, String> {
    let t = s_of(v).ok_or_else(|| "KeyError: date".to_owned())?;
    NaiveDate::parse_from_str(&t, "%Y-%m-%d").map_err(|_| format!("ValueError: {t}"))
}

fn at_of(v: Option<&J>) -> Result<DateTime<FixedOffset>, String> {
    let t = s_of(v).ok_or_else(|| "KeyError: 'at'".to_owned())?;
    DateTime::parse_from_rfc3339(&t).map_err(|_| format!("ValueError: {t}"))
}

fn int_of(v: Option<&J>) -> i64 {
    v.and_then(J::as_i64).unwrap_or(0)
}

/// `{symbol: {bid, ask, last, depth}}` → books (`book(bid, ask, last=, depth=)`).
fn books_of(v: Option<&J>) -> Result<BTreeMap<String, FoQuote>, String> {
    let mut out = BTreeMap::new();
    if let Some(J::Object(m)) = v {
        for (symbol, b) in m {
            let bid = dec_of(b.get("bid"))?;
            let ask = dec_of(b.get("ask"))?;
            let depth = b.get("depth").and_then(J::as_i64).unwrap_or(100_000);
            let level = |p: Option<Decimal>| {
                p.map(|price| {
                    vec![Level {
                        price,
                        quantity: depth,
                    }]
                })
                .unwrap_or_default()
            };
            let last = match dec_of(b.get("last"))? {
                Some(l) => Some(l),
                None => match (bid, ask) {
                    (Some(x), Some(y)) => Some(py::div(x + y, Decimal::from(2))?),
                    _ => None,
                },
            };
            out.insert(
                symbol.clone(),
                FoQuote {
                    bid,
                    ask,
                    bids: level(bid),
                    asks: level(ask),
                    last,
                    oi: Some(b.get("oi").and_then(J::as_i64).unwrap_or(1_000_000)),
                },
            );
        }
    }
    Ok(out)
}

/// The weekdays from `from`, `days` calendar days long (the replay's calendar).
fn sessions_of(v: Option<&J>) -> Result<Vec<NaiveDate>, String> {
    let Some(spec) = v.filter(|x| !x.is_null()) else {
        return Ok(Vec::new());
    };
    let start = date_of(spec.get("from"))?;
    let days = int_of(spec.get("days"));
    Ok((0..days)
        .map(|i| start + Duration::days(i))
        .filter(|d| !matches!(d.weekday(), Weekday::Sat | Weekday::Sun))
        .collect())
}

fn contract_of(c: &J) -> Result<Contract, String> {
    Ok(Contract {
        tradingsymbol: s_of(c.get("tradingsymbol")).unwrap_or_default(),
        instrument_token: int_of(c.get("instrument_token")),
        lot_size: int_of(c.get("lot_size")),
        expiry: date_of(c.get("expiry"))?,
    })
}

fn report_dict(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Map(dict(pairs))
}

fn strs(v: &[String]) -> PyValue {
    PyValue::List(v.iter().cloned().map(PyValue::Str).collect())
}

#[allow(clippy::too_many_lines)]
async fn drive(state: &AppState, body: &[u8]) -> Result<PyValue, String> {
    let req: J = if body.is_empty() {
        J::Object(serde_json::Map::new())
    } else {
        serde_json::from_slice(body).map_err(|e| format!("JSONDecodeError: {e}"))?
    };
    let settings = state.settings_snapshot();
    let books = Arc::new(SharedBooks::default());
    books.update(books_of(req.get("books"))?);
    let index = Arc::new(StaticIndex::default());
    if let Some(J::Object(m)) = req.get("index") {
        for (u, q) in m {
            let quote = if q.is_null() {
                None
            } else {
                Some(IndexQuote {
                    last: need_dec(q.get("last"), "last")?,
                    open: dec_of(q.get("open"))?,
                })
            };
            index.set(u, quote);
        }
    }
    let margin = match req.get("margin") {
        None => Some(MarginQuote {
            required_inr: Decimal::from(150_000),
            free_inr: Decimal::from(1_000_000),
        }),
        Some(J::Null) => None,
        Some(m) => Some(MarginQuote {
            required_inr: need_dec(m.get("required"), "required")?,
            free_inr: need_dec(m.get("free"), "free")?,
        }),
    };
    let mut instruments = StaticInstruments::default();
    if let Some(J::Array(items)) = req.get("options") {
        for o in items {
            instruments.options.push((
                s_of(o.get("underlying")).unwrap_or_default(),
                date_of(o.get("expiry"))?,
                need_dec(o.get("strike"), "strike")?,
                s_of(o.get("option_type")).unwrap_or_default(),
                contract_of(o)?,
            ));
        }
    }
    if let Some(J::Array(items)) = req.get("futures") {
        for f in items {
            instruments
                .futures
                .push((s_of(f.get("symbol")).unwrap_or_default(), contract_of(f)?));
        }
    }
    let market = Arc::new(StaticMarket::new(sessions_of(req.get("sessions"))?));
    if let Some(J::Array(days)) = req.get("landed") {
        for d in days {
            market.land(date_of(Some(d))?, []);
        }
    }
    if let Some(J::Array(items)) = req.get("prints") {
        for p in items {
            let key: PrintKey = (
                s_of(p.get("symbol")).unwrap_or_default(),
                date_of(p.get("expiry"))?,
                dec_of(p.get("strike"))?,
                s_of(p.get("option_type")).unwrap_or_default(),
            );
            let print = Print {
                close: need_dec(p.get("close"), "close")?,
                settle: need_dec(p.get("settle"), "settle")?,
            };
            market.land(date_of(p.get("day"))?, [(key, print)]);
        }
    }
    let alerts: Arc<std::sync::Mutex<Vec<String>>> = Arc::default();
    let sink = alerts.clone();
    let auto_exit = req.get("auto_exit").and_then(J::as_bool).unwrap_or(false);
    // `_main.gateway()` first, on both desks: the shared risk manager (on Postgres the day's
    // `risk_ledger` row), so `gateway_for` below derives the sleeve's FO gateway from it.
    if let Some(desk) = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
    {
        let _ = desk.risk().await;
    }
    let gateway_state = state.clone();
    let mut monitor = FnoMonitor::new(
        &settings,
        Arc::new(move |sleeve| fo_gateway(&gateway_state, sleeve)),
        books.clone(),
        Arc::new(instruments),
        Arc::new(StaticMargin::new(margin)),
        market,
    );
    monitor.index_quotes = index;
    monitor.exit_by_monitor = Arc::new(move |_| auto_exit);
    monitor.alert = Arc::new(move |m: &str| {
        if let Ok(mut a) = sink.lock() {
            a.push(m.to_owned());
        }
    });
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    let mut store = FoStore::new(&mut conn, settings.sole_user_id);
    let op = s_of(req.get("op")).unwrap_or_default();
    let taken =
        |a: &Arc<std::sync::Mutex<Vec<String>>>| a.lock().map(|v| v.clone()).unwrap_or_default();
    let out = match op.as_str() {
        "tick" => {
            let now = at_of(req.get("at"))?;
            let r = monitor.tick(&mut store, now).await.map_err(err_text)?;
            report_dict(vec![
                ("lapsed", strs(&r.lapsed)),
                ("raised", strs(&r.raised)),
                ("actions", strs(&r.actions)),
                (
                    "done",
                    PyValue::List(
                        r.done
                            .iter()
                            .map(|o| PyValue::Map(outcome_json(o)))
                            .collect(),
                    ),
                ),
                ("alerts", strs(&taken(&alerts))),
            ])
        }
        "night" => {
            let day = date_of(req.get("day"))?;
            let r = monitor.nightly(&mut store, day).await.map_err(err_text)?;
            report_dict(vec![
                ("landed", PyValue::Bool(r.landed)),
                ("marked", PyValue::Int(r.marked)),
                ("trailed", strs(&r.trailed)),
                ("missing", strs(&r.missing)),
                ("pauses", strs(&r.pauses)),
                ("alerts", strs(&taken(&alerts))),
            ])
        }
        "confirm" | "click" => {
            let now = at_of(req.get("at"))?;
            let plan_id = s_of(req.get("plan_id")).unwrap_or_default();
            let sleeve = store
                .plan(&plan_id)
                .await
                .map_err(|e| e.py_text())?
                .map(|p| p.sleeve);
            let Some(sleeve) = sleeve else {
                return Ok(refusal(404, "UNKNOWN_PLAN", "no such plan"));
            };
            let gateway = fo_gateway_ready(state, sleeve)
                .await
                .ok_or_else(|| "RuntimeError: the FO gateway could not be built".to_owned())?;
            let now_fn = move || now;
            let book = |f: &[baskfy_core::fno::covered::OptionPosition]| paper_book(f);
            let sink = alerts.clone();
            let alert = move |m: &str| {
                if let Ok(mut a) = sink.lock() {
                    a.push(m.to_owned());
                }
            };
            let ledger = crate::lanes::fno::ledger::DeskLedger;
            let ctx = FoCtx {
                quotes: &*books,
                now: &now_fn,
                broker_book: &book,
                alert: &alert,
                ledger: &ledger,
                tenant: super::super::execute::tenancy(&settings).ok(),
            };
            let s = settings.clone();
            let modes = move |sl| crate::lanes::fno::gates::mode_of(&s, sl);
            let sent = if op == "confirm" {
                execute_entry(&mut store, &gateway, &ctx, &plan_id, true, &modes).await
            } else {
                execute_click_exit(&mut store, &gateway, &ctx, &plan_id, true).await
            };
            match sent {
                Ok(o) => {
                    let mut m = outcome_json(&o);
                    m.insert("alerts", strs(&taken(&alerts)));
                    PyValue::Map(m)
                }
                Err(ExecError::Refused(r)) => refusal(r.status, &r.code, &r.message),
                Err(ExecError::Other(m)) => return Err(m),
            }
        }
        other => return Err(format!("ValueError: unknown op {other:?}")),
    };
    Ok(out)
}

fn refusal(status: u16, code: &str, message: &str) -> PyValue {
    let d: PyDict = dict([
        ("refused", PyValue::Int(i64::from(status))),
        ("code", PyValue::Str(code.to_owned())),
        ("message", PyValue::Str(message.to_owned())),
    ]);
    PyValue::Map(d)
}

fn err_text(e: ExecError) -> String {
    match e {
        ExecError::Other(m) => m,
        ExecError::Refused(r) => format!("Refused: {}", r.message),
    }
}
