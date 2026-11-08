//! `POST /__harness/swing-signal` — **harness-only** (`--harness`; `tools/parity/desk_diff.py`):
//! raise one signal through the monitor's real [`PgSignalStore`] on the side's database, so the
//! SIGNAL plan kind is graded byte for byte against Python's (`desk_harness.py` mounts the same
//! route over `app.swing_monitor`). Without the harness the path answers Starlette's 404 for
//! every method, exactly as an unknown path does: production cannot tell it exists.
//!
//! The body names a WATCHING symbol and the verdict (`state`, `entry`, `stop`, `range_high`,
//! `range_low`, `last_price`, `low_of_day`, `at`, `window_minutes`), optionally `focus` (to
//! override the watch row's), `live_level` (a constant index level standing in for Kite's, so a
//! case can open SW30's live gate) and `drain` (run SW25's drain afterwards — under the harness
//! `DRY_RUN` is always true, so it confirms nothing). The answer is what the store recorded:
//! lines written, the queue, the drain's result, the notices and the notifier's sends.

use std::sync::Arc;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderValue, Method, Response, StatusCode};
use axum::routing::any;
use baskfy_core::swing::opening_range::{TriggerState, TriggerVerdict};
use rust_decimal::Decimal;

use super::runner::{PgSignalStore, drain_auto_execute, load_config, load_watchlist};
use super::service::DeskAutoConfirm;
use crate::app::AppState;
use crate::lanes::swing::breakout::{Signal, SignalStore};
use crate::lanes::swing::live_gate::LiveLevel;
use crate::lanes::swing::notify::{Notifier, build_senders, notice_json};
use crate::lanes::swing::rules::{dec, gate_str};
use crate::lanes::swing::store::SCHEMA;
use crate::wire::{PyDict, PyValue, orjson_dumps};

/// The harness route (merged by the swing lane's router; inert without `--harness`).
pub fn routes() -> Router<AppState> {
    Router::new().route("/__harness/swing-signal", any(swing_signal))
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
    let mut m = PyDict::default();
    m.insert("detail", PyValue::Str(text.to_owned()));
    json(status, &PyValue::Map(m))
}

fn server_error(e: &str) -> Response<Body> {
    log_line!("ERROR swing_monitor harness signal failed: {e}");
    let mut r = Response::new(Body::from("Internal Server Error"));
    *r.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
    r.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    r
}

/// A constant level standing in for Kite's (`lambda: float(level)`).
struct ConstLevel(f64);

#[async_trait::async_trait]
impl LiveLevel for ConstLevel {
    async fn level(&self) -> Option<f64> {
        Some(self.0)
    }
}

fn text_of(v: Option<&serde_json::Value>) -> Option<String> {
    match v? {
        serde_json::Value::Null => None,
        serde_json::Value::String(s) => Some(s.clone()),
        other => Some(other.to_string()),
    }
}

/// `Decimal(str(v))`, `None` for an absent or null value.
fn dec_of(v: Option<&serde_json::Value>) -> Result<Option<Decimal>, String> {
    match text_of(v) {
        None => Ok(None),
        Some(t) => dec(t.trim())
            .map(Some)
            .ok_or_else(|| format!("InvalidOperation: {t}")),
    }
}

fn state_of(value: &str) -> Result<TriggerState, String> {
    Ok(match value {
        "TRIGGERED" => TriggerState::Triggered,
        "WAITING" => TriggerState::Waiting,
        "RANGE_INCOMPLETE" => TriggerState::RangeIncomplete,
        "BELOW_PIVOT" => TriggerState::BelowPivot,
        "LOCKED_UPPER_CIRCUIT" => TriggerState::LockedUpperCircuit,
        "SESSION_OVER" => TriggerState::SessionOver,
        other => return Err(format!("ValueError: '{other}' is not a valid TriggerState")),
    })
}

async fn swing_signal(
    State(state): State<AppState>,
    method: Method,
    body: Bytes,
) -> Response<Body> {
    if state.harness.is_none() {
        return detail(StatusCode::NOT_FOUND, "Not Found");
    }
    if method != Method::POST {
        let mut r = detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
        r.headers_mut()
            .insert(axum::http::header::ALLOW, HeaderValue::from_static("POST"));
        return r;
    }
    match raise(&state, &body).await {
        Ok(r) => r,
        Err(e) => server_error(&e),
    }
}

#[allow(clippy::too_many_lines)]
async fn raise(state: &AppState, body: &[u8]) -> Result<Response<Body>, String> {
    let req: serde_json::Value = if body.is_empty() {
        serde_json::Value::Object(serde_json::Map::new())
    } else {
        serde_json::from_slice(body).map_err(|e| format!("JSONDecodeError: {e}"))?
    };
    let settings = state.settings_snapshot();
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    let user_id = settings.sole_user_id;
    let day = state.clock.now_ist().date_naive();
    let config = load_config(&mut conn, user_id, SCHEMA).await?;
    let names = load_watchlist(&mut conn, user_id, &std::collections::BTreeMap::new()).await?;
    let symbol = text_of(req.get("symbol")).unwrap_or_default();
    // `{w.symbol: w for w in names}`: the last row for a symbol wins.
    let Some(mut watch) = names.into_iter().rev().find(|w| w.symbol == symbol) else {
        return Ok(detail(StatusCode::NOT_FOUND, "not watched"));
    };
    if let Some(focus) = req.get("focus") {
        watch.focus = match focus {
            serde_json::Value::Bool(b) => *b,
            serde_json::Value::Null => false,
            serde_json::Value::Number(n) => n.as_f64().is_some_and(|x| x != 0.0),
            serde_json::Value::String(s) => !s.is_empty(),
            serde_json::Value::Array(a) => !a.is_empty(),
            serde_json::Value::Object(o) => !o.is_empty(),
        };
    }
    let live_level: Option<Arc<dyn LiveLevel>> = match req.get("live_level") {
        None | Some(serde_json::Value::Null) => None,
        Some(v) => {
            let level = text_of(Some(v))
                .and_then(|t| crate::settings::py_float(&t))
                .ok_or_else(|| "ValueError: live_level".to_owned())?;
            Some(Arc::new(ConstLevel(level)))
        }
    };
    let at = text_of(req.get("at"))
        .and_then(|t| super::runner::fromisoformat_naive(&t))
        .ok_or_else(|| "ValueError: at".to_owned())?;
    let verdict = TriggerVerdict {
        state: state_of(&text_of(req.get("state")).unwrap_or_default())?,
        entry: dec_of(req.get("entry"))?,
        stop: dec_of(req.get("stop"))?,
        range_high: dec_of(req.get("range_high"))?,
        range_low: dec_of(req.get("range_low"))?,
    };
    let signal = Signal {
        watch,
        at,
        verdict,
        last_price: dec_of(req.get("last_price"))?.ok_or("TypeError: last_price")?,
        low_of_day: dec_of(req.get("low_of_day"))?.ok_or("TypeError: low_of_day")?,
        window_minutes: req
            .get("window_minutes")
            .and_then(serde_json::Value::as_i64)
            .unwrap_or(5),
    };
    let clock = state.clock.clone();
    let mut store = PgSignalStore {
        conn,
        user_id,
        day,
        config,
        live_level,
        context: None,
        lines_written: Vec::new(),
        pending_confirms: Vec::new(),
        notifier: Some(Notifier::new(build_senders(&settings), None)),
        notices: Vec::new(),
        settings: settings.clone(),
        ids: state.ids.clone(),
        now: Arc::new(move || clock.now_ist()),
    };
    // `PgSignalStore.__init__` reads the context once (the morning's reading).
    store.read_context().await.map_err(|e| e.to_string())?;
    store.raise_signal(&signal).await?;
    let queued: Vec<PyValue> = store
        .pending_confirms
        .iter()
        .map(|(s, p)| {
            PyValue::List(vec![
                PyValue::Str(s.watch.symbol.clone()),
                p.plan_id.clone().map_or(PyValue::None, PyValue::Str),
                p.line_id.map_or(PyValue::None, PyValue::Int),
            ])
        })
        .collect();
    let drained = if req
        .get("drain")
        .is_some_and(|d| !matches!(d, serde_json::Value::Null | serde_json::Value::Bool(false)))
    {
        let confirm = DeskAutoConfirm::new(state.clone());
        let done = drain_auto_execute(&mut store.pending_confirms, &settings, &confirm).await;
        PyValue::List(
            done.into_iter()
                .map(|(s, st)| PyValue::List(vec![PyValue::Str(s), PyValue::Str(st)]))
                .collect(),
        )
    } else {
        PyValue::None
    };
    let mut out = PyDict::default();
    out.insert(
        "lines_written",
        PyValue::List(
            store
                .lines_written
                .iter()
                .map(|i| PyValue::Int(*i))
                .collect(),
        ),
    );
    out.insert("queued", PyValue::List(queued));
    out.insert("auto_executed", drained);
    out.insert(
        "notices",
        PyValue::List(
            store
                .notices
                .iter()
                .map(|n| PyValue::Str(notice_json(n)))
                .collect(),
        ),
    );
    out.insert(
        "sent",
        PyValue::List(
            store
                .notifier
                .as_ref()
                .map(|n| {
                    n.sent
                        .iter()
                        .map(|(c, s, ok)| {
                            PyValue::List(vec![
                                PyValue::Str(c.clone()),
                                PyValue::Str(s.clone()),
                                PyValue::Bool(*ok),
                            ])
                        })
                        .collect()
                })
                .unwrap_or_default(),
        ),
    );
    out.insert(
        "gate",
        store
            .context
            .as_ref()
            .map_or(PyValue::None, |c| PyValue::Str(gate_str(c.gate).to_owned())),
    );
    Ok(json(StatusCode::OK, &PyValue::Map(out)))
}
