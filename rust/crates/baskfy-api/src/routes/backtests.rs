//! ``/backtests/*`` — docs/07 §Backtests. Ports the read half of `baskfy_api.routers.backtests`
//! (`APIRouter(prefix="/backtests", tags=["backtests"])`) and the service helpers it calls
//! (`baskfy_api.backtests`: artefact keys, the signed download link, the SSE progress frame).
//!
//! Ported (the derivatives lane, RS5): `GET ` (the list), `GET /{public_id}`,
//! `GET /{public_id}/trades`, `GET /{public_id}/holdings`, `GET /{public_id}/export`,
//! `GET /{public_id}/download/{artefact}` and `GET /{public_id}/events`.
//!
//! **Not registered:** `POST ` (queues a run: writes `backtest`, publishes a task) and
//! `DELETE /{public_id}` — the pool is read-only (DECISIONS-RS R5.2); Python keeps serving them.
//!
//! `Feature.BACKTESTS` gates every route but the download, whose signature is the credential. A
//! row that is not yours is a 404, never a 403. **The event stream** reproduces Python exactly
//! for a finished run (`progress` then `end`, the frame synthesised from the row) and streams a
//! run in flight over the Redis pub/sub channel, as Python's `event_stream` does.

use std::collections::BTreeSet;

mod writes;
use std::str::FromStr;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use baskfy_core::decimal::WideDecimal;
use baskfy_db::{Db, OrderedJson};
use chrono::{DateTime, NaiveDate, Utc};
use hmac::{Hmac, Mac};
use serde::Serialize;
use sha2::Sha256;
use sqlx::postgres::PgRow;

use super::options::derivatives::shared::{
    PyInt, cache_get, col, db_err, py_json_str_ascii, validate_int,
};
use crate::auth::{Principal, require_authenticated};
use crate::entitlements::{entitlements_for, utc_now};
use crate::problems::{Problem, ProblemType, internal_error, not_found, payment_required};
use crate::query::{QueryParams, Validation};
use crate::settings::{API_PREFIX, Settings};
use crate::state::AppState;
use crate::wire::{self, ser};

/// `DISCLAIMER`.
pub const DISCLAIMER: &str = "Past backtest results do not predict future results.";
/// `DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE` (`schemas.Limit`).
pub const DEFAULT_PAGE_SIZE: i64 = 100;
/// `MAX_PAGE_SIZE`.
pub const MAX_PAGE_SIZE: i64 = 500;
/// `ARTEFACTS`.
pub const ARTEFACTS: [&str; 3] = ["trades", "holdings", "equity"];
/// `DOWNLOAD_TTL_SECONDS`.
pub const DOWNLOAD_TTL_SECONDS: i64 = 15 * 60;
const ARTEFACT_PATTERN: &str = "^(trades|holdings|equity)$";
const FEATURE: &str = "backtests";
const STATUSES: [&str; 4] = ["queued", "running", "done", "failed"];

/// `artefact_key(public_id, artefact)`.
#[must_use]
pub fn artefact_key(public_id: &str, artefact: &str) -> String {
    format!("backtests/{public_id}/{artefact}.csv")
}

/// `progress_key(public_id)`.
#[must_use]
pub fn progress_key(public_id: &str) -> String {
    format!("backtest:progress:{public_id}")
}

/// `_signature(settings, public_id, artefact, expires)`: URL-safe base64 (unpadded) of the
/// HMAC-SHA256 over `"{public_id}:{artefact}:{expires}"`; `None` with no signing key.
#[must_use]
pub fn signature(secret: &str, public_id: &str, artefact: &str, expires: &str) -> Option<String> {
    use base64::Engine;
    if secret.is_empty() {
        return None;
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).ok()?;
    mac.update(format!("{public_id}:{artefact}:{expires}").as_bytes());
    Some(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes()))
}

/// `verify_download(settings, public_id, artefact, expires, token)`: the signature first
/// (constant time), the expiry after it.
#[must_use]
pub(crate) fn verify_download(
    secret: &str,
    public_id: &str,
    artefact: &str,
    expires: &PyInt,
    token: &str,
    now: i64,
) -> bool {
    if !ARTEFACTS.contains(&artefact) || !token.is_ascii() {
        return false;
    }
    let Some(expected) = signature(secret, public_id, artefact, &expires.canonical) else {
        return false;
    };
    if !constant_time_eq(expected.as_bytes(), token.as_bytes()) {
        return false;
    }
    match expires.value {
        Some(e) => e >= i128::from(now),
        None => !expires.negative,
    }
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0_u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// `progress_frame(public_id, status, stage=, completed=, total=, as_of=None, detail=)`:
/// `json.dumps(..., separators=(",", ":"), sort_keys=True)`.
#[must_use]
pub fn progress_frame(
    public_id: &str,
    status: &str,
    stage: &str,
    completed: i64,
    total: i64,
    detail: Option<&str>,
) -> String {
    let percent = if total <= 0 {
        0
    } else {
        // `round(100 * completed / total)`: banker's rounding of the true quotient.
        let num = 100 * completed;
        let q = num.div_euclid(total);
        let r = num.rem_euclid(total);
        let twice = 2 * r;
        let rounded = if twice > total || (twice == total && q % 2 != 0) {
            q + 1
        } else {
            q
        };
        rounded.min(100)
    };
    let detail = detail.map_or_else(|| "null".to_owned(), py_json_str_ascii);
    format!(
        "{{\"as_of\":null,\"completed\":{completed},\"detail\":{detail},\"percent\":{percent},\
         \"public_id\":{},\"stage\":{},\"status\":{},\"total\":{total}}}",
        py_json_str_ascii(public_id),
        py_json_str_ascii(stage),
        py_json_str_ascii(status)
    )
}

// --- models ----------------------------------------------------------------------------------

/// `BacktestSummaryOut`.
#[derive(Debug, Clone, Serialize)]
struct SummaryOut {
    public_id: String,
    status: String,
    #[serde(serialize_with = "ser::datetime")]
    created_at: DateTime<Utc>,
    #[serde(serialize_with = "ser::opt_datetime")]
    finished_at: Option<DateTime<Utc>>,
    screen_public_id: Option<String>,
    screen_name: Option<String>,
    #[serde(serialize_with = "ser::date")]
    start: NaiveDate,
    #[serde(serialize_with = "ser::date")]
    end: NaiveDate,
    top_n: i64,
    weighting: String,
    rebalance_frequency: String,
    #[serde(serialize_with = "ser::opt_float")]
    cagr: Option<f64>,
    #[serde(serialize_with = "ser::opt_float")]
    total_return: Option<f64>,
    #[serde(serialize_with = "ser::opt_float")]
    max_drawdown: Option<f64>,
    error: Option<String>,
}

/// `BacktestListOut`.
#[derive(Debug, Clone, Serialize)]
struct ListOut {
    data: Vec<SummaryOut>,
}

/// `BacktestOut`.
#[derive(Debug, Clone, Serialize)]
struct DetailOut {
    public_id: String,
    status: String,
    #[serde(serialize_with = "ser::datetime")]
    created_at: DateTime<Utc>,
    #[serde(serialize_with = "ser::opt_datetime")]
    finished_at: Option<DateTime<Utc>>,
    config: OrderedJson,
    screen_public_id: Option<String>,
    screen_name: Option<String>,
    error: Option<String>,
    data_version: Option<i64>,
    metrics_hash: Option<String>,
    metrics: OrderedJson,
    equity_curve: Vec<OrderedJson>,
    drawdown: Vec<OrderedJson>,
    monthly_returns: Vec<OrderedJson>,
    fragility: Vec<OrderedJson>,
    assumptions: Vec<String>,
    disclaimer: &'static str,
}

/// `TradePage` / `HoldingPage`.
#[derive(Debug, Clone, Serialize)]
struct PageOut {
    data: Vec<OrderedJson>,
    next_cursor: Option<String>,
}

/// `ExportLinkOut`.
#[derive(Debug, Clone, Serialize)]
struct ExportLinkOut {
    artefact: String,
    format: &'static str,
    url: String,
    #[serde(serialize_with = "ser::datetime")]
    expires_at: DateTime<Utc>,
}

// --- pydantic coercions for the stored JSON --------------------------------------------------

/// A `Decimal` field validated from JSON as pydantic does and printed `str(Decimal)`.
fn py_decimal_of(v: &OrderedJson) -> Result<String, Problem> {
    let parsed = match v {
        OrderedJson::Str(s) => WideDecimal::from_str(s.trim()).ok(),
        OrderedJson::Int(i) => Some(WideDecimal::from(*i)),
        OrderedJson::UInt(u) => WideDecimal::from_str(&u.to_string()).ok(),
        OrderedJson::BigInt(big) => WideDecimal::from_str(big).ok(),
        OrderedJson::Float(f) if f.is_finite() => {
            WideDecimal::from_str(&baskfy_core::py::float_repr(*f)).ok()
        }
        _ => None,
    };
    parsed.map(|d| d.to_sci_string()).ok_or_else(internal_error)
}

/// An `int` field validated from JSON (an integral float is accepted, as pydantic's lax mode).
fn py_int_of(v: &OrderedJson) -> Result<OrderedJson, Problem> {
    match v {
        OrderedJson::Int(_) | OrderedJson::UInt(_) | OrderedJson::BigInt(_) => Ok(v.clone()),
        #[allow(clippy::cast_possible_truncation)]
        OrderedJson::Float(f) if f.fract() == 0.0 && f.abs() < 9.0e15 => {
            Ok(OrderedJson::Int(*f as i64))
        }
        OrderedJson::Str(s) => super::options::derivatives::shared::lax_int(s)
            .and_then(|p| p.value)
            .and_then(|x| i64::try_from(x).ok())
            .map(OrderedJson::Int)
            .ok_or_else(internal_error),
        _ => Err(internal_error()),
    }
}

/// A `float` field validated from JSON: ints and numeric strings become floats.
fn py_float_of(v: &OrderedJson) -> Result<OrderedJson, Problem> {
    #[allow(clippy::cast_precision_loss)]
    match v {
        OrderedJson::Float(_) => Ok(v.clone()),
        OrderedJson::Int(i) => Ok(OrderedJson::Float(*i as f64)),
        OrderedJson::UInt(u) => Ok(OrderedJson::Float(*u as f64)),
        OrderedJson::BigInt(big) => big
            .parse::<f64>()
            .map(OrderedJson::Float)
            .map_err(|_| internal_error()),
        OrderedJson::Str(s) => s
            .trim()
            .parse::<f64>()
            .map(OrderedJson::Float)
            .map_err(|_| internal_error()),
        _ => Err(internal_error()),
    }
}

/// A `date` field: the lax parse, printed back as `YYYY-MM-DD`.
fn py_date_of(v: &OrderedJson) -> Result<NaiveDate, Problem> {
    match v {
        OrderedJson::Str(s) => {
            baskfy_core::screener::lax_date_from_str(s).map_err(|_| internal_error())
        }
        _ => Err(internal_error()),
    }
}

/// One field of a closed (`extra="forbid"`) model.
#[derive(Clone, Copy)]
enum Kind {
    Str,
    OptStr,
    Date,
    Decimal,
    OptDecimal,
    Int,
    OptInt,
    Float,
    OptFloat,
    Bool,
    /// A sub-model, with its own fields and defaults.
    Model(&'static [Field]),
    /// Passed through as stored (`dict | None`, `list`).
    Raw,
    /// `risk_free_curve`: `list[tuple[date, Decimal]]`.
    Curve,
}

/// `(name, kind, default as JSON text — None when required)`.
type Field = (&'static str, Kind, Option<&'static str>);

const REBALANCE: &[Field] = &[
    ("frequency", Kind::Str, Some("\"monthly\"")),
    ("day", Kind::Str, Some("\"last_trading_day\"")),
];
const SELECTION: &[Field] = &[
    ("top_n", Kind::Int, Some("20")),
    ("hold_buffer", Kind::Int, Some("10")),
];
const POSITION_LIMITS: &[Field] = &[
    ("max_weight", Kind::Decimal, Some("\"0.10\"")),
    ("min_weight", Kind::Decimal, Some("\"0.01\"")),
];
const COSTS: &[Field] = &[
    ("brokerage_bps", Kind::Decimal, Some("\"3\"")),
    ("stt_bps", Kind::Decimal, Some("\"10\"")),
    ("slippage_bps", Kind::Decimal, Some("\"15\"")),
    ("impact_model", Kind::Str, Some("\"fixed\"")),
];
const RISK_OVERLAY: &[Field] = &[
    ("enabled", Kind::Bool, Some("false")),
    ("rule", Kind::Str, Some("\"index_above_200dma\"")),
];
/// `baskfy_core.backtest.BacktestConfig`, in field order, with its defaults.
const BACKTEST_CONFIG: &[Field] = &[
    ("screen_public_id", Kind::OptStr, Some("null")),
    ("screen_definition", Kind::Raw, Some("null")),
    ("start", Kind::Date, None),
    ("end", Kind::Date, None),
    ("initial_capital", Kind::Decimal, Some("\"1000000\"")),
    ("rebalance", Kind::Model(REBALANCE), Some("{}")),
    ("selection", Kind::Model(SELECTION), Some("{}")),
    ("weighting", Kind::Str, Some("\"equal\"")),
    ("position_limits", Kind::Model(POSITION_LIMITS), Some("{}")),
    ("costs", Kind::Model(COSTS), Some("{}")),
    ("cash_policy", Kind::Str, Some("\"hold_cash\"")),
    ("benchmark", Kind::Str, Some("\"nifty-500\"")),
    ("risk_overlay", Kind::Model(RISK_OVERLAY), Some("{}")),
    ("dividends", Kind::Str, Some("\"ignore\"")),
    ("risk_free_rate", Kind::Decimal, Some("\"0\"")),
    ("risk_free_curve", Kind::Curve, Some("[]")),
];
/// `EquityPointOut`.
const EQUITY_POINT: &[Field] = &[
    ("date", Kind::Date, None),
    ("equity", Kind::Decimal, None),
    ("benchmark", Kind::OptDecimal, Some("null")),
];
/// `DrawdownPointOut`.
const DRAWDOWN_POINT: &[Field] = &[("date", Kind::Date, None), ("drawdown", Kind::Float, None)];
/// `FragilityRunOut`.
const FRAGILITY_RUN: &[Field] = &[
    ("label", Kind::Str, None),
    ("description", Kind::Str, None),
    ("cagr", Kind::OptFloat, Some("null")),
    ("total_return", Kind::OptFloat, Some("null")),
    ("max_drawdown", Kind::OptFloat, Some("null")),
    ("final_equity", Kind::Decimal, None),
    ("trades", Kind::Int, None),
    ("blind_pct", Kind::Float, Some("0.0")),
    ("rebalances", Kind::Int, Some("0")),
];
/// `TradeOut`.
const TRADE: &[Field] = &[
    ("date", Kind::Date, None),
    ("symbol", Kind::Str, None),
    ("side", Kind::Str, None),
    ("quantity", Kind::Int, None),
    ("price", Kind::Decimal, None),
    ("notional", Kind::Decimal, None),
    ("cost", Kind::Decimal, None),
    ("reason", Kind::Str, None),
    ("realised_pnl", Kind::OptDecimal, Some("null")),
];
/// `BacktestHoldingOut`.
const HOLDING: &[Field] = &[
    ("rebalance_date", Kind::Date, None),
    ("executed_on", Kind::Date, None),
    ("symbol", Kind::Str, None),
    ("name", Kind::Str, None),
    ("rank", Kind::OptInt, Some("null")),
    ("target_weight", Kind::Decimal, None),
    ("quantity", Kind::Int, None),
    ("price", Kind::Decimal, None),
    ("value", Kind::Decimal, None),
    ("actual_weight", Kind::Decimal, None),
];

fn coerce(kind: Kind, v: &OrderedJson) -> Result<OrderedJson, Problem> {
    let null = matches!(v, OrderedJson::Null);
    Ok(match kind {
        Kind::Str => match v {
            OrderedJson::Str(_) => v.clone(),
            _ => return Err(internal_error()),
        },
        Kind::OptStr if null => OrderedJson::Null,
        Kind::OptStr => coerce(Kind::Str, v)?,
        Kind::Date => OrderedJson::Str(wire::py_date(py_date_of(v)?)),
        Kind::OptDecimal if null => OrderedJson::Null,
        Kind::Decimal | Kind::OptDecimal => OrderedJson::Str(py_decimal_of(v)?),
        Kind::OptInt if null => OrderedJson::Null,
        Kind::Int | Kind::OptInt => py_int_of(v)?,
        Kind::OptFloat if null => OrderedJson::Null,
        Kind::Float | Kind::OptFloat => py_float_of(v)?,
        Kind::Bool => match v {
            OrderedJson::Bool(_) => v.clone(),
            _ => return Err(internal_error()),
        },
        Kind::Model(fields) => model(fields, v)?,
        Kind::Raw => v.clone(),
        Kind::Curve => match v {
            OrderedJson::Array(items) => {
                let mut out = Vec::with_capacity(items.len());
                for item in items {
                    let OrderedJson::Array(pair) = item else {
                        return Err(internal_error());
                    };
                    let [day, rate] = pair.as_slice() else {
                        return Err(internal_error());
                    };
                    out.push(OrderedJson::Array(vec![
                        coerce(Kind::Date, day)?,
                        coerce(Kind::Decimal, rate)?,
                    ]));
                }
                OrderedJson::Array(out)
            }
            _ => return Err(internal_error()),
        },
    })
}

/// `Model.model_validate(stored).model_dump(mode="json")`: the model's fields in order, defaults
/// filled, values coerced; an unknown key or a missing required one is the 500 Python raises.
fn model(fields: &[Field], stored: &OrderedJson) -> Result<OrderedJson, Problem> {
    let OrderedJson::Object(entries) = stored else {
        return Err(internal_error());
    };
    if entries
        .iter()
        .any(|(k, _)| !fields.iter().any(|(name, _, _)| name == k))
    {
        return Err(internal_error());
    }
    let mut out = Vec::with_capacity(fields.len());
    for (name, kind, default) in fields {
        let value = match stored.get(name) {
            Some(v) => coerce(*kind, v)?,
            None => match default {
                Some(text) => {
                    let parsed = OrderedJson::parse(text).map_err(|_| internal_error())?;
                    match kind {
                        Kind::Model(sub) => model(sub, &parsed)?,
                        _ => parsed,
                    }
                }
                None => return Err(internal_error()),
            },
        };
        out.push(((*name).to_owned(), value));
    }
    Ok(OrderedJson::Object(out))
}

/// `MonthlyReturnOut`: `month`, and `ret` serialised by its alias `return` (either accepted).
fn monthly_return(stored: &OrderedJson) -> Result<OrderedJson, Problem> {
    let OrderedJson::Object(entries) = stored else {
        return Err(internal_error());
    };
    if entries
        .iter()
        .any(|(k, _)| k != "month" && k != "return" && k != "ret")
    {
        return Err(internal_error());
    }
    let month = coerce(Kind::Str, stored.get("month").ok_or_else(internal_error)?)?;
    let ret = stored
        .get("return")
        .or_else(|| stored.get("ret"))
        .ok_or_else(internal_error)?;
    Ok(OrderedJson::Object(vec![
        ("month".to_owned(), month),
        ("return".to_owned(), py_float_of(ret)?),
    ]))
}

// --- reads -----------------------------------------------------------------------------------

const ROW_COLUMNS: &str = "id, public_id, user_id, screen_id, config, status, metrics, \
     equity_curve, error, created_at, finished_at";

/// `_load(session, public_id, principal)`: yours, or a 404.
async fn load(db: &Db, public_id: &str, principal: &Principal) -> Result<PgRow, Problem> {
    let row = sqlx::query(&format!(
        "SELECT {ROW_COLUMNS} FROM backtest WHERE public_id = $1"
    ))
    .bind(public_id)
    .fetch_optional(db.pool())
    .await
    .map_err(db_err)?;
    match row {
        Some(r) if Some(col::i64(&r, "user_id")?) == principal.user_id => Ok(r),
        _ => Err(not_found("backtest", public_id)),
    }
}

/// `_status(row)`: the four statuses the CHECK constraint allows.
fn status_of(row: &PgRow) -> Result<String, Problem> {
    let status = col::text(row, "status")?;
    if STATUSES.contains(&status.as_str()) {
        Ok(status)
    } else {
        Err(Problem::new(
            ProblemType::InternalError,
            format!("backtest {} has a bad status", col::text(row, "public_id")?),
        ))
    }
}

/// `_screen_labels(session, rows)`: `screen.id -> (public_id, name)`.
async fn screen_labels(
    db: &Db,
    ids: &BTreeSet<i64>,
) -> Result<std::collections::BTreeMap<i64, (String, String)>, Problem> {
    let mut out = std::collections::BTreeMap::new();
    if ids.is_empty() {
        return Ok(out);
    }
    let ids: Vec<i64> = ids.iter().copied().collect();
    let rows = sqlx::query("SELECT id, public_id, name FROM screen WHERE id = ANY($1)")
        .bind(&ids)
        .fetch_all(db.pool())
        .await
        .map_err(db_err)?;
    for r in &rows {
        out.insert(
            col::i64(r, "id")?,
            (col::text(r, "public_id")?, col::text(r, "name")?),
        );
    }
    Ok(out)
}

/// `_metric(metrics, key)`: a number (not a bool) as a float.
fn metric(metrics: Option<&OrderedJson>, key: &str) -> Option<f64> {
    #[allow(clippy::cast_precision_loss)]
    match metrics?.get(key)? {
        OrderedJson::Int(i) => Some(*i as f64),
        OrderedJson::UInt(u) => Some(*u as f64),
        OrderedJson::BigInt(big) => big.parse::<f64>().ok(),
        OrderedJson::Float(f) => Some(*f),
        _ => None,
    }
}

/// The stored `metrics` when it is an object (`isinstance(metrics, Mapping)`).
fn metrics_object(row: &PgRow) -> Result<Option<OrderedJson>, Problem> {
    Ok(col::opt_json(row, "metrics")?.filter(|m| matches!(m, OrderedJson::Object(_))))
}

/// `list[dict]` members of `payload[key]` (`_sub`, `_curve`).
fn dicts(payload: Option<&OrderedJson>, key: &str) -> Vec<OrderedJson> {
    match payload.and_then(|p| p.get(key)) {
        Some(OrderedJson::Array(items)) => items
            .iter()
            .filter(|i| matches!(i, OrderedJson::Object(_)))
            .cloned()
            .collect(),
        _ => Vec::new(),
    }
}

/// The entitlement every route but the download checks (`entitlements.require`).
async fn require_backtests(state: &AppState, principal: &Principal) -> Result<(), Problem> {
    let entitlements = entitlements_for(&state.db, principal, &state.settings).await?;
    if entitlements.allows(FEATURE) {
        Ok(())
    } else {
        Err(payment_required(FEATURE, None))
    }
}

/// `limit: Limit = 100` (`ge=1, le=500`).
fn limit_param(v: &mut Validation, q: &QueryParams) -> i64 {
    validate_int(v, "limit", q.get("limit"), Some(1), Some(MAX_PAGE_SIZE))
        .and_then(|p| p.value)
        .and_then(|x| i64::try_from(x).ok())
        .unwrap_or(DEFAULT_PAGE_SIZE)
}

/// `GET /backtests?limit=`: your backtests, newest first.
///
/// # Errors
/// 401 anonymous; 400 a bad `limit`; 402 without `backtests`; 500 a database failure.
pub async fn list_backtests(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let entitlements = entitlements_for(&state.db, principal, &state.settings).await?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let limit = limit_param(&mut v, &q);
    v.finish()?;
    if !entitlements.allows(FEATURE) {
        return Err(payment_required(FEATURE, None));
    }
    let user_id = principal.require_user()?;
    let rows = sqlx::query(&format!(
        "SELECT {ROW_COLUMNS} FROM backtest WHERE user_id = $1 \
         ORDER BY created_at DESC, id DESC LIMIT $2"
    ))
    .bind(user_id)
    .bind(limit)
    .fetch_all(state.db.pool())
    .await
    .map_err(db_err)?;
    let mut ids = BTreeSet::new();
    for r in &rows {
        if let Some(id) = col::opt_i64(r, "screen_id")? {
            ids.insert(id);
        }
    }
    let labels = screen_labels(&state.db, &ids).await?;
    let mut data = Vec::with_capacity(rows.len());
    for r in &rows {
        let stored = col::opt_json(r, "config")?.ok_or_else(internal_error)?;
        let config = model(BACKTEST_CONFIG, &stored)?;
        let label = col::opt_i64(r, "screen_id")?.and_then(|id| labels.get(&id));
        let metrics = metrics_object(r)?;
        let text = |key: &str| match config.get(key) {
            Some(OrderedJson::Str(s)) => Ok(s.clone()),
            _ => Err(internal_error()),
        };
        let sub = |model: &str, key: &str| config.get(model).and_then(|m| m.get(key)).cloned();
        data.push(SummaryOut {
            public_id: col::text(r, "public_id")?,
            status: status_of(r)?,
            created_at: col::ts(r, "created_at")?,
            finished_at: col::opt_ts(r, "finished_at")?,
            screen_public_id: match label {
                Some((public, _)) => Some(public.clone()),
                None => match config.get("screen_public_id") {
                    Some(OrderedJson::Str(s)) => Some(s.clone()),
                    _ => None,
                },
            },
            screen_name: label.map(|(_, name)| name.clone()),
            start: py_date_of(&OrderedJson::Str(text("start")?))?,
            end: py_date_of(&OrderedJson::Str(text("end")?))?,
            top_n: match sub("selection", "top_n") {
                Some(OrderedJson::Int(n)) => n,
                _ => return Err(internal_error()),
            },
            weighting: text("weighting")?,
            rebalance_frequency: match sub("rebalance", "frequency") {
                Some(OrderedJson::Str(s)) => s,
                _ => return Err(internal_error()),
            },
            cagr: metric(metrics.as_ref(), "cagr"),
            total_return: metric(metrics.as_ref(), "total_return"),
            max_drawdown: metric(metrics.as_ref(), "max_drawdown"),
            error: col::opt_text(r, "error")?,
        });
    }
    Ok(wire::json(&ListOut { data }))
}

/// `GET /backtests/{public_id}`: status, metrics, the equity curve and the honesty panels.
///
/// # Errors
/// 401 anonymous; 402 without `backtests`; 404 not yours or not there; 500 a database failure
/// or a stored row the model refuses.
pub async fn get_backtest(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    require_backtests(&state, principal).await?;
    let row = load(&state.db, &public_id, principal).await?;
    let metrics = metrics_object(&row)?;
    let stored = col::opt_json(&row, "config")?.ok_or_else(internal_error)?;
    let config = model(BACKTEST_CONFIG, &stored)?;
    let curve = col::opt_json(&row, "equity_curve")?;
    let mut equity_curve = Vec::new();
    for point in dicts(curve.as_ref(), "points") {
        equity_curve.push(model(EQUITY_POINT, &point)?);
    }
    let mut drawdown = Vec::new();
    for point in dicts(curve.as_ref(), "drawdown") {
        drawdown.push(model(DRAWDOWN_POINT, &point)?);
    }
    let mut monthly_returns = Vec::new();
    for point in dicts(metrics.as_ref(), "monthly_returns") {
        monthly_returns.push(monthly_return(&point)?);
    }
    let mut fragility = Vec::new();
    for run in dicts(metrics.as_ref(), "fragility") {
        fragility.push(model(FRAGILITY_RUN, &run)?);
    }
    let assumptions = match metrics.as_ref().and_then(|m| m.get("assumptions")) {
        Some(OrderedJson::Array(items)) => items
            .iter()
            .filter_map(|i| match i {
                OrderedJson::Str(s) => Some(s.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    };
    let data_version = match metrics.as_ref().and_then(|m| m.get("data_version")) {
        Some(OrderedJson::Int(i)) => Some(*i),
        Some(OrderedJson::Bool(b)) => Some(i64::from(*b)),
        _ => None,
    };
    let metrics_hash = match metrics.as_ref().and_then(|m| m.get("metrics_hash")) {
        Some(OrderedJson::Str(s)) => Some(s.clone()),
        _ => None,
    };
    let promoted = ["fragility", "assumptions", "monthly_returns"];
    let metrics_out = match &metrics {
        Some(OrderedJson::Object(entries)) => OrderedJson::Object(
            entries
                .iter()
                .filter(|(k, _)| !promoted.contains(&k.as_str()))
                .cloned()
                .collect(),
        ),
        _ => OrderedJson::Null,
    };
    let mut screen_public_id = match config.get("screen_public_id") {
        Some(OrderedJson::Str(s)) => Some(s.clone()),
        _ => None,
    };
    let mut screen_name = None;
    if let Some(id) = col::opt_i64(&row, "screen_id")? {
        let labels = screen_labels(&state.db, &BTreeSet::from([id])).await?;
        if let Some((public, name)) = labels.get(&id) {
            screen_public_id = Some(public.clone());
            screen_name = Some(name.clone());
        }
    }
    Ok(wire::json(&DetailOut {
        public_id: col::text(&row, "public_id")?,
        status: status_of(&row)?,
        created_at: col::ts(&row, "created_at")?,
        finished_at: col::opt_ts(&row, "finished_at")?,
        config,
        screen_public_id,
        screen_name,
        error: col::opt_text(&row, "error")?,
        data_version,
        metrics_hash,
        metrics: metrics_out,
        equity_curve,
        drawdown,
        monthly_returns,
        fragility,
        assumptions,
        disclaimer: DISCLAIMER,
    }))
}

// --- artefacts -------------------------------------------------------------------------------

/// `_require_done(row)`.
fn require_done(row: &PgRow) -> Result<(), Problem> {
    let status = col::text(row, "status")?;
    if status == "done" {
        return Ok(());
    }
    Err(Problem::new(
        ProblemType::NotFound,
        format!(
            "Backtest {} is {}; its artefacts do not exist yet.",
            col::text(row, "public_id")?,
            baskfy_providers::py::repr_str(&status)
        ),
    ))
}

/// The process's archive (`app.state.invoice_archive`): R2 when configured, else
/// `BASKFY_INVOICE_LOCAL_DIR`.
fn archive(
    settings: &Settings,
) -> Result<std::sync::Arc<dyn baskfy_providers::archive::RawArchive>, Problem> {
    let provider = baskfy_providers::settings::get_provider_settings().map_err(|e| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", &e.to_string())],
        );
        internal_error()
    })?;
    baskfy_providers::factory::build_archive(
        provider,
        Some(std::path::Path::new(&settings.invoice_local_dir)),
    )
    .map_err(|e| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", &e.to_string())],
        );
        internal_error()
    })
}

/// `archive.get(key)`, an `ArchiveError` read as the 404 the routes answer.
async fn artefact_bytes(settings: &Settings, key: &str) -> Result<Vec<u8>, Problem> {
    archive(settings)?
        .get(key)
        .await
        .map_err(|_| not_found("backtest artefact", key))
}

/// `csv.DictReader` over UTF-8 text (the excel dialect), blank rows skipped, a short row's
/// missing cells `None`; `_blanks_to_none` applied.
/// One CSV row as `(column, cell)` pairs, an empty cell read as `None`.
type CsvRow = Vec<(String, Option<String>)>;

fn csv_rows(text: &str) -> Result<Vec<CsvRow>, Problem> {
    let mut records: Vec<Vec<String>> = Vec::new();
    let mut field = String::new();
    let mut record: Vec<String> = Vec::new();
    let mut quoted = false;
    let mut chars = text.chars().peekable();
    let mut started = false;
    while let Some(c) = chars.next() {
        if quoted {
            if c == '"' {
                if chars.peek() == Some(&'"') {
                    field.push('"');
                    chars.next();
                } else {
                    quoted = false;
                }
            } else {
                field.push(c);
            }
            continue;
        }
        match c {
            '"' if field.is_empty() => {
                quoted = true;
                started = true;
            }
            ',' => {
                record.push(std::mem::take(&mut field));
                started = true;
            }
            '\r' | '\n' => {
                if c == '\r' && chars.peek() == Some(&'\n') {
                    chars.next();
                }
                if started || !field.is_empty() {
                    record.push(std::mem::take(&mut field));
                }
                records.push(std::mem::take(&mut record));
                started = false;
            }
            c => {
                field.push(c);
                started = true;
            }
        }
    }
    if started || !field.is_empty() || !record.is_empty() {
        record.push(field);
        records.push(record);
    }
    let mut iter = records.into_iter().filter(|r| !r.is_empty());
    let Some(header) = iter.next() else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    for r in iter {
        if r.len() > header.len() {
            return Err(internal_error());
        }
        out.push(
            header
                .iter()
                .enumerate()
                .map(|(i, name)| {
                    let cell = r.get(i).cloned().filter(|s| !s.is_empty());
                    (name.clone(), cell)
                })
                .collect(),
        );
    }
    Ok(out)
}

/// One CSV row validated into a closed model.
fn csv_model(
    fields: &[Field],
    row: &[(String, Option<String>)],
    literals: &[(&str, &[&str])],
) -> Result<OrderedJson, Problem> {
    let stored = OrderedJson::Object(
        row.iter()
            .map(|(k, v)| {
                (
                    k.clone(),
                    v.clone().map_or(OrderedJson::Null, OrderedJson::Str),
                )
            })
            .collect(),
    );
    let out = model(fields, &stored)?;
    for (name, allowed) in literals {
        match out.get(name) {
            Some(OrderedJson::Str(s)) if allowed.contains(&s.as_str()) => {}
            _ => return Err(internal_error()),
        }
    }
    Ok(out)
}

use crate::routes::common::{PY_DIGIT_ONLY, py_decimal};

/// `cursor.isdigit()` then `int(cursor)`, as `_page` runs them: not every character a digit is
/// the 404; a digit `int()` cannot read (`'²'`) is the `ValueError` Python lets escape as a 500;
/// otherwise the offset (any Unicode decimal digits, as `int()` reads them).
fn cursor_offset(c: &str) -> Result<usize, Problem> {
    let mut digits = String::with_capacity(c.len());
    let mut unreadable = false;
    let mut isdigit = !c.is_empty();
    for ch in c.chars() {
        if let Some(d) = py_decimal(ch) {
            digits.push(char::from_digit(d, 10).ok_or_else(internal_error)?);
        } else if PY_DIGIT_ONLY
            .iter()
            .any(|&(lo, hi)| (lo..=hi).contains(&u32::from(ch)))
        {
            unreadable = true;
        } else {
            isdigit = false;
            break;
        }
    }
    if !isdigit {
        return Err(Problem::new(
            ProblemType::NotFound,
            format!(
                "{} is not a valid pagination cursor.",
                baskfy_providers::py::repr_str(c)
            ),
        ));
    }
    if unreadable {
        return Err(internal_error());
    }
    Ok(digits.parse::<usize>().unwrap_or(usize::MAX))
}

/// `_page(rows, cursor, limit)`: the cursor is the row offset.
fn page<T: Clone>(
    rows: &[T],
    cursor: Option<&str>,
    limit: i64,
) -> Result<(Vec<T>, Option<String>), Problem> {
    let mut start = 0_usize;
    if let Some(c) = cursor {
        start = cursor_offset(c)?;
    }
    let limit = usize::try_from(limit).unwrap_or(0);
    let window = rows
        .iter()
        .skip(start)
        .take(limit)
        .cloned()
        .collect::<Vec<_>>();
    let following = start.saturating_add(limit);
    Ok((
        window,
        (following < rows.len()).then(|| following.to_string()),
    ))
}

/// `GET /backtests/{public_id}/trades?limit=&cursor=`: the trade log, paginated.
///
/// # Errors
/// 401, 400 a bad `limit`, 402, 404 (not yours, not done, no artefact, a bad cursor), 500.
pub async fn get_trades(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    artefact_page(state, principal, public_id, raw, "trades").await
}

/// `GET /backtests/{public_id}/holdings?limit=&cursor=&rebalance_date=`: per-rebalance holdings.
///
/// # Errors
/// As [`get_trades`], plus 400 a bad `rebalance_date`.
pub async fn get_holdings(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    artefact_page(state, principal, public_id, raw, "holdings").await
}

async fn artefact_page(
    state: AppState,
    principal: Principal,
    public_id: String,
    raw: Option<String>,
    artefact: &str,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let entitlements = entitlements_for(&state.db, principal, &state.settings).await?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let limit = limit_param(&mut v, &q);
    let cursor = q.get("cursor");
    let rebalance_date = if artefact == "holdings" {
        v.date("rebalance_date", q.get("rebalance_date"))
    } else {
        None
    };
    v.finish()?;
    if !entitlements.allows(FEATURE) {
        return Err(payment_required(FEATURE, None));
    }
    let row = load(&state.db, &public_id, principal).await?;
    require_done(&row)?;
    let bytes = artefact_bytes(&state.settings, &artefact_key(&public_id, artefact)).await?;
    let text = String::from_utf8(bytes).map_err(|_| internal_error())?;
    let mut rows = csv_rows(&text)?;
    if let Some(day) = rebalance_date {
        let wanted = wire::py_date(day);
        rows.retain(|r| {
            r.iter()
                .any(|(k, v)| k == "rebalance_date" && v.as_deref() == Some(wanted.as_str()))
        });
    }
    let (window, next_cursor) = page(&rows, cursor, limit)?;
    let mut data = Vec::with_capacity(window.len());
    for r in &window {
        data.push(if artefact == "trades" {
            csv_model(
                TRADE,
                r,
                &[
                    ("side", &["buy", "sell"]),
                    ("reason", &["enter", "exit", "rebalance", "delist"]),
                ],
            )?
        } else {
            csv_model(HOLDING, r, &[])?
        });
    }
    Ok(wire::json(&PageOut { data, next_cursor }))
}

/// `GET /backtests/{public_id}/export?artefact=`: a signed, fifteen-minute download link.
///
/// # Errors
/// 401, 400 an artefact outside the pattern, 402, 404 (not yours, not done), 500 (no signing key).
pub async fn export_backtest(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let entitlements = entitlements_for(&state.db, principal, &state.settings).await?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let artefact = match q.get("artefact") {
        None => Some("trades"),
        Some(a) if ARTEFACTS.contains(&a) => ARTEFACTS.iter().copied().find(|x| *x == a),
        Some(_) => {
            v.error(
                "artefact",
                &format!("String should match pattern '{ARTEFACT_PATTERN}'"),
                "string_pattern_mismatch",
            );
            None
        }
    };
    v.finish()?;
    let artefact = artefact.ok_or_else(internal_error)?;
    if !entitlements.allows(FEATURE) {
        return Err(payment_required(FEATURE, None));
    }
    let row = load(&state.db, &public_id, principal).await?;
    require_done(&row)?;
    let now = utc_now();
    let expires = (now + chrono::Duration::seconds(DOWNLOAD_TTL_SECONDS)).timestamp();
    let token = signature(
        state.settings.jwt_secret.expose(),
        &public_id,
        artefact,
        &expires.to_string(),
    )
    .ok_or_else(internal_error)?;
    Ok(wire::json(&ExportLinkOut {
        artefact: artefact.to_owned(),
        format: "csv",
        url: format!(
            "{API_PREFIX}/backtests/{public_id}/download/{artefact}?expires={expires}&token={token}"
        ),
        expires_at: DateTime::from_timestamp(expires, 0).ok_or_else(internal_error)?,
    }))
}

/// `GET /backtests/{public_id}/download/{artefact}?expires=&token=`: redeem a signed link.
/// Deliberately unauthenticated (the signature is the credential); the row is re-checked so a
/// deleted run's link stops working.
///
/// # Errors
/// 400 a bad artefact, `expires` or `token`; 404 a bad or expired signature, a deleted run, or a
/// missing object; 500 a database failure.
pub async fn download_artefact(
    State(state): State<AppState>,
    Path((public_id, artefact)): Path<(String, String)>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    if !ARTEFACTS.contains(&artefact.as_str()) {
        v.error(
            "artefact",
            &format!("String should match pattern '{ARTEFACT_PATTERN}'"),
            "string_pattern_mismatch",
        );
    }
    let expires_raw = v.required(&q, "expires");
    let expires = validate_int(&mut v, "expires", expires_raw, None, None);
    let token = v.required(&q, "token");
    v.finish()?;
    let (Some(expires), Some(token)) = (expires, token) else {
        return Err(internal_error());
    };
    let refused = || not_found("backtest artefact", &format!("{public_id}/{artefact}"));
    if !verify_download(
        state.settings.jwt_secret.expose(),
        &public_id,
        &artefact,
        &expires,
        token,
        utc_now().timestamp(),
    ) {
        return Err(refused());
    }
    let still_there: Option<i64> =
        sqlx::query_scalar("SELECT id FROM backtest WHERE public_id = $1")
            .bind(&public_id)
            .fetch_optional(state.db.pool())
            .await
            .map_err(db_err)?;
    if still_there.is_none() {
        return Err(refused());
    }
    let payload = artefact_bytes(&state.settings, &artefact_key(&public_id, &artefact)).await?;
    let mut response = (StatusCode::OK, payload).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/csv; charset=utf-8"),
    );
    if let Ok(v) = HeaderValue::from_str(&format!(
        "attachment; filename=\"baskfy-backtest-{public_id}-{artefact}.csv\""
    )) {
        headers.insert(header::CONTENT_DISPOSITION, v);
    }
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("private, no-store"),
    );
    Ok(response)
}

/// `_sse(event, data)`.
fn sse(event: &str, data: &str) -> String {
    format!("event: {event}\ndata: {data}\n\n")
}

/// `GET /backtests/{public_id}/events`: progress as Server-Sent Events (see the module docs for
/// the run that is still in flight).
///
/// # Errors
/// 401, 402, 404 (not yours), 500.
pub async fn backtest_events(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    require_backtests(&state, principal).await?;
    let row = load(&state.db, &public_id, principal).await?;
    let stored = cache_get(&state.settings.redis_url, &progress_key(&public_id))
        .await
        .and_then(|b| String::from_utf8(b).ok());
    let status = col::text(&row, "status")?;
    let terminal = status == "done" || status == "failed";
    let initial = match stored {
        Some(frame) if !terminal => frame,
        _ => progress_frame(
            &public_id,
            &status_of(&row)?,
            &status,
            i64::from(terminal),
            1,
            col::opt_text(&row, "error")?.as_deref(),
        ),
    };
    let url = state.settings.redis_url.expose().to_owned();
    let mut response = if terminal || url.is_empty() {
        let body = format!("{}{}", sse("progress", &initial), sse("end", &initial));
        (StatusCode::OK, body).into_response()
    } else {
        let (tx, rx) = tokio::sync::mpsc::channel::<String>(16);
        tokio::spawn(event_stream(url, events_channel(&public_id), initial, tx));
        let stream = futures_util::stream::unfold(rx, |mut rx| async move {
            rx.recv()
                .await
                .map(|frame| (Ok::<_, std::convert::Infallible>(frame), rx))
        });
        (StatusCode::OK, axum::body::Body::from_stream(stream)).into_response()
    };
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/event-stream; charset=utf-8"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert("x-accel-buffering", HeaderValue::from_static("no"));
    Ok(response)
}

/// `SSE_HEARTBEAT_SECONDS`.
const SSE_HEARTBEAT: std::time::Duration = std::time::Duration::from_secs(15);
/// `SSE_MAX_SECONDS`.
const SSE_MAX: std::time::Duration = std::time::Duration::from_secs(30 * 60);

/// `service.events_channel(public_id)`.
fn events_channel(public_id: &str) -> String {
    format!("backtest:events:{public_id}")
}

/// `_is_terminal(payload)`.
fn is_terminal_frame(payload: &str) -> bool {
    serde_json::from_str::<serde_json::Value>(payload)
        .ok()
        .and_then(|v| v.get("status").and_then(|s| s.as_str().map(str::to_owned)))
        .is_some_and(|s| s == "done" || s == "failed")
}

/// `event_stream(cache, public_id, initial, terminal)` for a run in flight: subscribe, send the
/// current frame, relay published frames until a terminal one (heartbeat every 15 s, 30 min cap).
async fn event_stream(
    url: String,
    channel: String,
    initial: String,
    tx: tokio::sync::mpsc::Sender<String>,
) {
    use futures_util::StreamExt as _;
    let failed = |tx: tokio::sync::mpsc::Sender<String>, initial: String| async move {
        crate::logging::warning(
            "baskfy_api.routers.backtests",
            "backtest event stream failed",
            &[],
        );
        let _ = tx.send(sse("progress", &initial)).await;
        let _ = tx.send(sse("end", &initial)).await;
    };
    let Ok(client) = redis::Client::open(url.as_str()) else {
        return failed(tx, initial).await;
    };
    let mut pubsub = match client.get_async_pubsub().await {
        Ok(p) => p,
        Err(_) => return failed(tx, initial).await,
    };
    if pubsub.subscribe(&channel).await.is_err() {
        return failed(tx, initial).await;
    }
    if tx.send(sse("progress", &initial)).await.is_err() {
        return;
    }
    let began = std::time::Instant::now();
    let mut messages = pubsub.on_message();
    while began.elapsed() < SSE_MAX {
        match tokio::time::timeout(SSE_HEARTBEAT, messages.next()).await {
            Err(_) => {
                if tx.send(": keep-alive\n\n".to_owned()).await.is_err() {
                    return;
                }
            }
            Ok(None) => {
                drop(messages);
                return failed(tx, initial).await;
            }
            Ok(Some(message)) => {
                let Ok(bytes) = message.get_payload::<Vec<u8>>() else {
                    continue;
                };
                let Ok(payload) = String::from_utf8(bytes) else {
                    continue;
                };
                if tx.send(sse("progress", &payload)).await.is_err() {
                    return;
                }
                if is_terminal_frame(&payload) {
                    let _ = tx.send(sse("end", &payload)).await;
                    return;
                }
            }
        }
    }
    let _ = tx.send(sse("end", &initial)).await;
}

/// The ported `/backtests` routes, relative to `/api/v1` (`POST` and `DELETE` stay on Python).
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/backtests",
            get(list_backtests).post(writes::create_backtest),
        )
        .route(
            "/backtests/{public_id}",
            get(get_backtest).delete(writes::delete_backtest),
        )
        .route("/backtests/{public_id}/trades", get(get_trades))
        .route("/backtests/{public_id}/holdings", get(get_holdings))
        .route("/backtests/{public_id}/export", get(export_backtest))
        .route(
            "/backtests/{public_id}/download/{artefact}",
            get(download_artefact),
        )
        .route("/backtests/{public_id}/events", get(backtest_events))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_progress_frame_is_pythons_sorted_compact_json() {
        // json.dumps(progress_frame(...)) from the live Python, byte for byte.
        assert_eq!(
            progress_frame("abc", "failed", "failed", 1, 1, Some("é \"x\"")),
            "{\"as_of\":null,\"completed\":1,\"detail\":\"\\u00e9 \\\"x\\\"\",\"percent\":100,\
             \"public_id\":\"abc\",\"stage\":\"failed\",\"status\":\"failed\",\"total\":1}"
        );
        assert_eq!(
            progress_frame("abc", "queued", "queued", 0, 1, None),
            r#"{"as_of":null,"completed":0,"detail":null,"percent":0,"public_id":"abc","stage":"queued","status":"queued","total":1}"#
        );
    }

    #[test]
    fn a_signature_is_urlsafe_unpadded_hmac() {
        let sig = signature("k", "p", "trades", "10").unwrap();
        assert!(!sig.contains('=') && !sig.contains('+') && !sig.contains('/'));
        assert_eq!(sig.len(), 43);
        assert!(signature("", "p", "trades", "10").is_none());
        let expires = super::super::options::derivatives::shared::lax_int("10").unwrap();
        assert!(verify_download("k", "p", "trades", &expires, &sig, 10));
        assert!(!verify_download("k", "p", "trades", &expires, &sig, 11));
        assert!(!verify_download("k", "p", "holdings", &expires, &sig, 10));
        assert!(!verify_download("k", "p", "trades", &expires, "é", 10));
    }

    #[test]
    fn the_config_is_dumped_in_field_order_with_defaults() {
        let stored = OrderedJson::parse(
            r#"{"end": "2026-09-01", "costs": {"stt_bps": "10"}, "start": "2024-11-01", "selection": {"top_n": 20}, "position_limits": {"max_weight": 0.1}}"#,
        )
        .unwrap();
        let out = model(BACKTEST_CONFIG, &stored)
            .unwrap()
            .to_python_json()
            .unwrap();
        assert_eq!(
            out,
            r#"{"screen_public_id":null,"screen_definition":null,"start":"2024-11-01","end":"2026-09-01","initial_capital":"1000000","rebalance":{"frequency":"monthly","day":"last_trading_day"},"selection":{"top_n":20,"hold_buffer":10},"weighting":"equal","position_limits":{"max_weight":"0.1","min_weight":"0.01"},"costs":{"brokerage_bps":"3","stt_bps":"10","slippage_bps":"15","impact_model":"fixed"},"cash_policy":"hold_cash","benchmark":"nifty-500","risk_overlay":{"enabled":false,"rule":"index_above_200dma"},"dividends":"ignore","risk_free_rate":"0","risk_free_curve":[]}"#
        );
        let unknown =
            OrderedJson::parse(r#"{"start": "2024-01-01", "end": "2024-02-01", "x": 1}"#).unwrap();
        assert!(model(BACKTEST_CONFIG, &unknown).is_err());
    }

    #[test]
    fn csv_reads_as_dictreader_does() {
        let rows = csv_rows("a,b,c\n1,\"x,\"\"y\"\"\",\n\n2,3\n").unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0][1].1.as_deref(), Some("x,\"y\""));
        assert_eq!(rows[0][2].1, None);
        assert_eq!(rows[1][2].1, None);
    }

    #[test]
    fn the_cursor_is_an_offset() {
        let rows: Vec<i32> = (0..5).collect();
        assert_eq!(
            page(&rows, None, 2).unwrap(),
            (vec![0, 1], Some("2".to_owned()))
        );
        assert_eq!(page(&rows, Some("4"), 2).unwrap(), (vec![4], None));
        assert!(page(&rows, Some("x"), 2).is_err());
    }

    #[test]
    fn the_cursor_reads_digits_as_python_does() {
        // `'١'.isdigit()` and `int('١') == 1`: an Arabic-Indic one is offset 1.
        assert_eq!(cursor_offset("\u{661}").unwrap(), 1);
        assert_eq!(cursor_offset("0\u{FF11}").unwrap(), 1);
        // `'²'.isdigit()` but `int('²')` raises: Python's 500.
        assert_eq!(cursor_offset("\u{B2}").unwrap_err().status, 500);
        assert_eq!(cursor_offset("1\u{B2}").unwrap_err().status, 500);
        // Not a digit anywhere: the 404, even after a superscript.
        for bad in ["", "-1", "+1", " 1", "\u{B2}x", "\u{BD}", "1_0"] {
            assert_eq!(cursor_offset(bad).unwrap_err().status, 404, "{bad:?}");
        }
    }
}
