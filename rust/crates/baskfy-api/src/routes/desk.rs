//! `/desk/*` — M26. The desk's read-only surfaces, on the merged backend.
//!
//! Ports `baskfy_api.routers.desk` (and `baskfy_api.desk_schema.require_desk_schema`):
//!
//! * `GET /desk/performance` — NAV against the benchmark, both rebased over the desk's span;
//! * `GET /desk/holdings` — the latest end-of-day snapshot's positions;
//! * `GET /desk/tradebook?limit=` — every trade, closed and open;
//! * `GET /desk/regime` — the policy tier in force;
//! * `GET /desk/reconcile` — the last plan against its fills.
//!
//! There is no write route here and there will not be (`test_desk_readonly.py`). Every route
//! needs a signed-in caller and, before any query of its own, the `desk` schema
//! (`require_desk_schema`: 404 when this deployment has none).
//!
//! Numbers are Python `float`s: `numeric` is read as text and parsed (`float(Decimal)` is the
//! correctly rounded double), rounding is Python's `round(x, n)`, sums are CPython 3.12's
//! compensated `sum()`.

use std::cmp::Ordering;

use axum::extract::{RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use axum::{Extension, Router};
use baskfy_db::OrderedJson;
use serde::Serialize;
use sqlx::Row;
use sqlx::postgres::PgRow;

use super::meta::reference::{self as r, Params, py_round};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType};
use crate::state::AppState;
use crate::wire;

/// `DESK_SCHEMA`.
pub const DESK_SCHEMA: &str = "desk";
/// `BENCHMARK`.
pub const BENCHMARK: &str = "NIFTY500MOMENTM50";
/// `DEFAULT_LIMIT`.
pub const DEFAULT_LIMIT: i64 = 200;
/// `MAX_LIMIT`.
pub const MAX_LIMIT: i64 = 1000;

// ---------------------------------------------------------------------------------------------
// Shapes
// ---------------------------------------------------------------------------------------------

/// `NavPointOut`.
#[derive(Debug, Clone, Serialize)]
struct NavPointOut {
    date: String,
    #[serde(serialize_with = "wire::ser::float")]
    nav: f64,
    #[serde(serialize_with = "wire::ser::float")]
    invested: f64,
    #[serde(serialize_with = "wire::ser::float")]
    cash: f64,
    #[serde(serialize_with = "wire::ser::opt_float")]
    index_value: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    benchmark_value: Option<f64>,
}

/// `PerformanceOut`.
#[derive(Debug, Serialize)]
struct PerformanceOut {
    as_of: String,
    #[serde(serialize_with = "wire::ser::float")]
    nav: f64,
    #[serde(serialize_with = "wire::ser::float")]
    invested: f64,
    #[serde(serialize_with = "wire::ser::float")]
    cash: f64,
    #[serde(serialize_with = "wire::ser::opt_float")]
    return_pct: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    benchmark_return_pct: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    excess_pct: Option<f64>,
    series: Vec<NavPointOut>,
}

/// `DeskHoldingOut`.
#[derive(Debug, Serialize)]
struct DeskHoldingOut {
    symbol: String,
    quantity: i64,
    #[serde(serialize_with = "wire::ser::opt_float")]
    average_price: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    price: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    value: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    unrealised: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    unrealised_pct: Option<f64>,
    pledged_qty: i64,
    excluded: bool,
}

/// `HoldingsOut`.
#[derive(Debug, Serialize)]
struct HoldingsOut {
    as_of: String,
    rows: Vec<DeskHoldingOut>,
    #[serde(serialize_with = "wire::ser::float")]
    total_value: f64,
    #[serde(serialize_with = "wire::ser::float")]
    excluded_value: f64,
}

/// `DeskTradeOut`.
#[derive(Debug, Serialize)]
struct DeskTradeOut {
    symbol: String,
    quantity: i64,
    entry_date: Option<String>,
    exit_date: Option<String>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    entry_price: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    exit_price: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    pnl: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    pnl_pct: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    costs: Option<f64>,
    exit_reason: Option<String>,
    open: bool,
}

/// `TradebookOut`.
#[derive(Debug, Serialize)]
struct TradebookOut {
    rows: Vec<DeskTradeOut>,
    total: i64,
    open_count: i64,
    closed_count: i64,
    #[serde(serialize_with = "wire::ser::float")]
    realised_pnl: f64,
    winners: i64,
    losers: i64,
}

/// `RegimeOut`.
#[derive(Debug, Serialize)]
struct RegimeOut {
    evaluated_at: String,
    signal_date: Option<String>,
    tier: String,
    previous_tier: Option<String>,
    new_buys: Option<String>,
    mode: Option<String>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    breadth_pct: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    actual_equity_pct: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    target_equity_cap_pct: Option<f64>,
    reasons: Vec<String>,
    data_stale: bool,
    manual_action_required: bool,
    next_evaluation_date: Option<String>,
}

/// `ReconcileRowOut`.
#[derive(Debug, Serialize)]
struct ReconcileRowOut {
    symbol: String,
    side: String,
    planned_qty: i64,
    filled_qty: i64,
    #[serde(serialize_with = "wire::ser::opt_float")]
    planned_ref_price: Option<f64>,
    #[serde(serialize_with = "wire::ser::opt_float")]
    avg_fill_price: Option<f64>,
    status: Option<String>,
    shortfall: i64,
    #[serde(serialize_with = "wire::ser::opt_float")]
    slippage: Option<f64>,
}

/// `ReconcileOut`.
#[derive(Debug, Serialize)]
struct ReconcileOut {
    plan_id: String,
    created_at: String,
    note: Option<String>,
    rows: Vec<ReconcileRowOut>,
    planned_count: i64,
    complete_count: i64,
    partial_count: i64,
    unfilled_count: i64,
    settled: bool,
}

// ---------------------------------------------------------------------------------------------
// Python's conversions
// ---------------------------------------------------------------------------------------------

/// `float(str)` for a numeric's text (`NaN` and the infinities included).
fn py_float_str(text: &str) -> Option<f64> {
    let t = r::py_strip(text);
    if t.starts_with('_') || t.ends_with('_') || t.contains("__") {
        return None;
    }
    let cleaned: String = t.chars().filter(|c| *c != '_').collect();
    cleaned.parse::<f64>().ok()
}

/// A nullable column read as text, then `_f`.
fn col_f(row: &PgRow, column: &str) -> Result<Option<f64>, Problem> {
    let text: Option<String> = row.try_get(column).map_err(r::db)?;
    Ok(text.and_then(|t| py_float_str(&t)))
}

/// `_f(value)` over a JSON value: numbers and strings convert, `None` stays `None`, anything
/// else is Python's `TypeError` (a 500).
fn json_f(value: Option<&OrderedJson>) -> Result<Option<f64>, Problem> {
    #[allow(clippy::cast_precision_loss)]
    match value {
        None | Some(OrderedJson::Null) => Ok(None),
        Some(OrderedJson::Int(i)) => Ok(Some(*i as f64)),
        Some(OrderedJson::UInt(u)) => Ok(Some(*u as f64)),
        Some(OrderedJson::BigInt(big)) => Ok(big.parse::<f64>().ok()),
        Some(OrderedJson::Float(f)) => Ok(Some(*f)),
        Some(OrderedJson::Bool(b)) => Ok(Some(if *b { 1.0 } else { 0.0 })),
        Some(OrderedJson::Str(s)) => Ok(py_float_str(s)),
        Some(_) => Err(r::unexpected(
            "TypeError: expected a number from the database",
        )),
    }
}

/// `_num(value)`: `_f(value) or 0.0`.
fn or_zero(value: Option<f64>) -> f64 {
    match value {
        Some(x) if x != 0.0 => x,
        _ => 0.0,
    }
}

/// `int(float)`: truncation; NaN and the infinities raise in Python (a 500).
fn py_int(x: f64) -> Result<i64, Problem> {
    if !x.is_finite() {
        return Err(r::unexpected("cannot convert float to integer"));
    }
    #[allow(clippy::cast_possible_truncation)]
    let t = x.trunc() as i64;
    Ok(t)
}

/// `_change_pct(start, end)`.
fn change_pct(start: Option<f64>, end: Option<f64>) -> Option<f64> {
    match (start, end) {
        (Some(s), Some(e)) if s != 0.0 => Some(py_round((e - s) / s * 100.0, 2)),
        _ => None,
    }
}

/// `_rebase(close, base)`.
fn rebase(close: Option<f64>, base: Option<f64>) -> Option<f64> {
    match (close, base) {
        (Some(c), Some(b)) if b != 0.0 => Some(py_round(c / b * 100.0, 4)),
        _ => None,
    }
}

/// `_str_or_none(value)` for a text column.
fn str_or_none(value: Option<String>) -> Option<String> {
    value.filter(|v| !v.is_empty())
}

/// Python's `str()` of a value `json.loads` produced.
fn py_str(v: &OrderedJson) -> String {
    match v {
        OrderedJson::Str(s) => s.clone(),
        other => py_repr(other),
    }
}

/// Python's `repr()` of a value `json.loads` produced.
fn py_repr(v: &OrderedJson) -> String {
    match v {
        OrderedJson::Null => "None".to_owned(),
        OrderedJson::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
        OrderedJson::Int(i) => i.to_string(),
        OrderedJson::UInt(u) => u.to_string(),
        OrderedJson::BigInt(big) => big.clone(),
        OrderedJson::Float(f) => {
            if f.is_nan() {
                "nan".to_owned()
            } else if f.is_infinite() {
                if *f > 0.0 { "inf" } else { "-inf" }.to_owned()
            } else {
                baskfy_core::py::float_repr(*f)
            }
        }
        OrderedJson::Str(s) => baskfy_providers::py::repr_str(s),
        OrderedJson::Array(items) => {
            format!(
                "[{}]",
                items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
            )
        }
        OrderedJson::Object(entries) => format!(
            "{{{}}}",
            entries
                .iter()
                .map(|(k, v)| format!("{}: {}", baskfy_providers::py::repr_str(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// Python truthiness of a JSON value.
fn truthy(v: Option<&OrderedJson>) -> bool {
    match v {
        None | Some(OrderedJson::Null) => false,
        Some(OrderedJson::Bool(b)) => *b,
        Some(OrderedJson::Int(i)) => *i != 0,
        Some(OrderedJson::UInt(u)) => *u != 0,
        Some(OrderedJson::BigInt(_)) => true,
        Some(OrderedJson::Float(f)) => *f != 0.0,
        Some(OrderedJson::Str(s)) => !s.is_empty(),
        Some(OrderedJson::Array(a)) => !a.is_empty(),
        Some(OrderedJson::Object(o)) => !o.is_empty(),
    }
}

/// `_as_object` / `_as_list`: `json.loads` of a text column (`""`/`NULL` are empty).
fn parse_json(text: Option<&str>) -> Result<Option<OrderedJson>, Problem> {
    match text {
        None | Some("") => Ok(None),
        Some(t) => OrderedJson::parse(t)
            .map(Some)
            .map_err(|e| r::unexpected(&format!("JSONDecodeError: {e}"))),
    }
}

/// `_day(value)`: epoch seconds as an ISO date.
fn day(seconds: Option<f64>) -> Option<String> {
    r::utc_from_seconds(seconds?).map(|t| wire::py_date(t.date_naive()))
}

// ---------------------------------------------------------------------------------------------
// The router's dependencies
// ---------------------------------------------------------------------------------------------

/// `[Depends(require_authenticated), Depends(require_desk_schema)]`.
async fn desk_dependencies(state: &AppState, principal: &Principal) -> Result<(), Problem> {
    require_authenticated(principal)?;
    let present: Option<i32> =
        sqlx::query_scalar("SELECT 1 FROM information_schema.schemata WHERE schema_name = $1")
            .bind(DESK_SCHEMA)
            .fetch_optional(state.db.pool())
            .await
            .map_err(r::db)?;
    if present.is_none() {
        return Err(Problem::new(
            ProblemType::NotFound,
            "this deployment has no desk history.",
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------------------------

/// `GET /desk/performance`: portfolio value over time against the benchmark.
///
/// # Errors
/// 401, 404 (no desk schema, or no snapshots), 500.
pub async fn performance(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    desk_dependencies(&state, &principal).await?;
    let rows = sqlx::query(
        "SELECT date, nav::text AS nav, invested::text AS invested, cash::text AS cash, \
         index_value::text AS index_value FROM \"desk\".snapshots ORDER BY date",
    )
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    if rows.is_empty() {
        return Err(Problem::new(
            ProblemType::NotFound,
            "the desk has recorded no end-of-day snapshots yet.",
        ));
    }
    let marks = sqlx::query(
        "SELECT date, close::text AS close FROM \"desk\".benchmark WHERE index_name = $1 \
         ORDER BY date",
    )
    .bind(BENCHMARK)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut closes: Vec<(String, Option<f64>)> = Vec::new();
    for m in &marks {
        let date: String = m.try_get("date").map_err(r::db)?;
        let close = col_f(m, "close")?;
        match closes.iter_mut().find(|(d, _)| *d == date) {
            Some(slot) => slot.1 = close,
            None => closes.push((date, close)),
        }
    }
    let close_on = |date: &str| -> Option<f64> {
        closes.iter().find(|(d, _)| d == date).and_then(|(_, c)| *c)
    };
    let mut dates = Vec::with_capacity(rows.len());
    for row in &rows {
        dates.push(row.try_get::<String, _>("date").map_err(r::db)?);
    }
    // `next(closes[d] for r in rows if closes.get(d) not in (None, 0))`.
    let base = dates.iter().filter_map(|d| close_on(d)).find(|c| *c != 0.0);
    let mut series = Vec::with_capacity(rows.len());
    for (row, date) in rows.iter().zip(&dates) {
        series.push(NavPointOut {
            date: date.clone(),
            nav: or_zero(col_f(row, "nav")?),
            invested: or_zero(col_f(row, "invested")?),
            cash: or_zero(col_f(row, "cash")?),
            index_value: col_f(row, "index_value")?,
            benchmark_value: rebase(close_on(date), base),
        });
    }
    let (Some(first), Some(last)) = (series.first().cloned(), series.last().cloned()) else {
        return Err(r::unexpected("no snapshots"));
    };
    let return_pct = change_pct(first.index_value, last.index_value);
    let benchmark_return_pct = change_pct(first.benchmark_value, last.benchmark_value);
    let excess_pct = match (return_pct, benchmark_return_pct) {
        (Some(a), Some(b)) => Some(py_round(a - b, 2)),
        _ => None,
    };
    Ok(wire::json(&PerformanceOut {
        as_of: last.date,
        nav: last.nav,
        invested: last.invested,
        cash: last.cash,
        return_pct,
        benchmark_return_pct,
        excess_pct,
        series,
    }))
}

/// `_holding(payload)`.
fn holding(p: &OrderedJson) -> Result<DeskHoldingOut, Problem> {
    let quantity = py_int(or_zero(json_f(p.get("quantity"))?))?;
    let average = json_f(p.get("average_price"))?;
    let price = json_f(p.get("price"))?;
    let value = json_f(p.get("value"))?;
    #[allow(clippy::cast_precision_loss)]
    let unrealised = match (price, average) {
        (Some(pr), Some(av)) => Some(py_round((pr - av) * quantity as f64, 2)),
        _ => None,
    };
    let symbol = match p.get("symbol") {
        Some(v) if truthy(Some(v)) => py_str(v),
        _ => String::new(),
    };
    Ok(DeskHoldingOut {
        symbol,
        quantity,
        average_price: average,
        price,
        value,
        unrealised,
        unrealised_pct: change_pct(average, price),
        pledged_qty: py_int(or_zero(json_f(p.get("pledged_qty"))?))?,
        excluded: truthy(p.get("excluded")),
    })
}

/// `GET /desk/holdings`: the latest snapshot's positions, largest first.
///
/// # Errors
/// 401, 404 (no desk schema, or no snapshots), 500.
pub async fn holdings(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    desk_dependencies(&state, &principal).await?;
    let row = sqlx::query(
        "SELECT date, holdings_json FROM \"desk\".snapshots ORDER BY date DESC LIMIT 1",
    )
    .fetch_optional(state.db.pool())
    .await
    .map_err(r::db)?;
    let Some(row) = row else {
        return Err(Problem::new(
            ProblemType::NotFound,
            "the desk has recorded no end-of-day snapshots yet.",
        ));
    };
    let date: String = row.try_get("date").map_err(r::db)?;
    let text: Option<String> = row.try_get("holdings_json").map_err(r::db)?;
    let payload = match parse_json(text.as_deref())? {
        Some(o @ OrderedJson::Object(_)) => o,
        _ => OrderedJson::Object(Vec::new()),
    };
    let mut rows = Vec::new();
    if let Some(OrderedJson::Array(positions)) = payload.get("positions") {
        for p in positions {
            if matches!(p, OrderedJson::Object(_)) {
                rows.push(holding(p)?);
            }
        }
    }
    let key = |h: &DeskHoldingOut| or_zero(h.value);
    rows.sort_by(|a, b| key(b).partial_cmp(&key(a)).unwrap_or(Ordering::Equal));
    let values: Vec<f64> = rows.iter().map(key).collect();
    let excluded: Vec<f64> = rows.iter().filter(|h| h.excluded).map(key).collect();
    let as_of = match payload.get("as_of") {
        Some(v) if truthy(Some(v)) => py_str(v),
        _ => date,
    };
    Ok(wire::json(&HoldingsOut {
        as_of,
        rows,
        total_value: py_round(r::py_sum(&values), 2),
        excluded_value: py_round(r::py_sum(&excluded), 2),
    }))
}

/// `_trade(data)`.
fn trade(row: &PgRow) -> Result<DeskTradeOut, Problem> {
    let qty: Option<i64> = row.try_get("qty").map_err(r::db)?;
    #[allow(clippy::cast_precision_loss)]
    let quantity = py_int(or_zero(qty.map(|q| q as f64)))?;
    let entry = col_f(row, "entry_price")?;
    let pnl = col_f(row, "pnl")?;
    #[allow(clippy::cast_precision_loss)]
    let basis = entry.unwrap_or(0.0) * quantity as f64;
    let entry_ts: Option<f64> = row.try_get("entry_ts").map_err(r::db)?;
    let exit_ts: Option<f64> = row.try_get("exit_ts").map_err(r::db)?;
    let symbol: Option<String> = row.try_get("symbol").map_err(r::db)?;
    Ok(DeskTradeOut {
        symbol: symbol.unwrap_or_default(),
        quantity,
        entry_date: day(entry_ts),
        exit_date: day(exit_ts),
        entry_price: entry,
        exit_price: col_f(row, "exit_price")?,
        pnl_pct: match pnl {
            Some(p) if basis != 0.0 => Some(py_round(p / basis * 100.0, 2)),
            _ => None,
        },
        pnl,
        costs: col_f(row, "costs")?,
        exit_reason: str_or_none(row.try_get("exit_reason").map_err(r::db)?),
        open: exit_ts.is_none(),
    })
}

/// `GET /desk/tradebook?limit=`: every trade, newest exit (or entry) first.
///
/// # Errors
/// 401, 404 (no desk schema), 400 for a bad limit, 500.
pub async fn tradebook(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    desk_dependencies(&state, &principal).await?;
    let mut p = Params::new(raw.as_deref());
    let limit = p.int("limit", Some(DEFAULT_LIMIT), Some(1), Some(MAX_LIMIT));
    p.finish()?;
    let limit = limit.flatten().unwrap_or(DEFAULT_LIMIT);
    let totals = sqlx::query(
        "SELECT count(*) AS total, count(*) FILTER (WHERE exit_ts IS NULL) AS open_count, \
         coalesce(sum(pnl) FILTER (WHERE exit_ts IS NOT NULL), 0)::text AS realised, \
         count(*) FILTER (WHERE exit_ts IS NOT NULL AND pnl > 0) AS winners, \
         count(*) FILTER (WHERE exit_ts IS NOT NULL AND pnl <= 0) AS losers \
         FROM \"desk\".trades",
    )
    .fetch_one(state.db.pool())
    .await
    .map_err(r::db)?;
    let rows = sqlx::query(
        "SELECT symbol, entry_ts, exit_ts, qty, entry_price::text AS entry_price, \
         exit_price::text AS exit_price, pnl::text AS pnl, costs::text AS costs, exit_reason \
         FROM \"desk\".trades ORDER BY coalesce(exit_ts, entry_ts) DESC NULLS LAST LIMIT $1",
    )
    .bind(limit)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let total: i64 = totals.try_get("total").map_err(r::db)?;
    let open_count: i64 = totals.try_get("open_count").map_err(r::db)?;
    Ok(wire::json(&TradebookOut {
        rows: rows.iter().map(trade).collect::<Result<_, _>>()?,
        total,
        open_count,
        closed_count: total - open_count,
        realised_pnl: py_round(or_zero(col_f(&totals, "realised")?), 2),
        winners: totals.try_get("winners").map_err(r::db)?,
        losers: totals.try_get("losers").map_err(r::db)?,
    }))
}

/// `GET /desk/regime`: the latest regime evaluation.
///
/// # Errors
/// 401, 404 (no desk schema, or no evaluations), 500.
pub async fn regime(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    desk_dependencies(&state, &principal).await?;
    let row = sqlx::query(
        "SELECT e.evaluation_id, e.created_at, e.signal_session_date, e.policy_tier, \
         e.previous_policy_tier, e.new_buys, e.mode, e.breadth_pct::text AS breadth_pct, \
         e.reasons_json, e.data_stale, e.manual_action_required, e.next_evaluation_date, \
         x.actual_equity_pct::text AS actual_equity_pct, \
         x.target_equity_cap_pct::text AS target_equity_cap_pct \
         FROM \"desk\".regime_evaluations e \
         LEFT JOIN \"desk\".regime_exposure x ON x.evaluation_id = e.evaluation_id \
         ORDER BY e.id DESC LIMIT 1",
    )
    .fetch_optional(state.db.pool())
    .await
    .map_err(r::db)?;
    let Some(row) = row else {
        return Err(Problem::new(
            ProblemType::NotFound,
            "the desk has recorded no regime evaluations yet.",
        ));
    };
    let reasons_text: Option<String> = row.try_get("reasons_json").map_err(r::db)?;
    let reasons = match parse_json(reasons_text.as_deref())? {
        Some(OrderedJson::Array(items)) => items.iter().map(py_str).collect(),
        _ => Vec::new(),
    };
    let created_at: Option<String> = row.try_get("created_at").map_err(r::db)?;
    let tier: Option<String> = row.try_get("policy_tier").map_err(r::db)?;
    let stale: Option<i64> = row.try_get("data_stale").map_err(r::db)?;
    let manual: Option<i64> = row.try_get("manual_action_required").map_err(r::db)?;
    Ok(wire::json(&RegimeOut {
        evaluated_at: created_at.unwrap_or_else(|| "None".to_owned()),
        signal_date: str_or_none(row.try_get("signal_session_date").map_err(r::db)?),
        tier: tier
            .filter(|t| !t.is_empty())
            .unwrap_or_else(|| "unknown".to_owned()),
        previous_tier: str_or_none(row.try_get("previous_policy_tier").map_err(r::db)?),
        new_buys: str_or_none(row.try_get("new_buys").map_err(r::db)?),
        mode: str_or_none(row.try_get("mode").map_err(r::db)?),
        breadth_pct: col_f(&row, "breadth_pct")?,
        actual_equity_pct: col_f(&row, "actual_equity_pct")?,
        target_equity_cap_pct: col_f(&row, "target_equity_cap_pct")?,
        reasons,
        data_stale: stale.is_some_and(|s| s != 0),
        manual_action_required: manual.is_some_and(|m| m != 0),
        next_evaluation_date: str_or_none(row.try_get("next_evaluation_date").map_err(r::db)?),
    }))
}

/// `_reconcile_row(data)`.
fn reconcile_row(row: &PgRow) -> Result<ReconcileRowOut, Problem> {
    #[allow(clippy::cast_precision_loss)]
    let int_col = |c: &str| -> Result<i64, Problem> {
        let v: Option<i64> = row.try_get(c).map_err(r::db)?;
        py_int(or_zero(v.map(|x| x as f64)))
    };
    let planned = int_col("planned_qty")?;
    let filled = int_col("filled_qty")?;
    let reference = col_f(row, "planned_ref_price")?;
    let average = col_f(row, "avg_fill_price")?;
    let symbol: Option<String> = row.try_get("symbol").map_err(r::db)?;
    let side: Option<String> = row.try_get("side").map_err(r::db)?;
    #[allow(clippy::cast_precision_loss)]
    let slippage = match (reference, average) {
        (Some(rf), Some(av)) => Some(py_round((av - rf) * filled as f64, 2)),
        _ => None,
    };
    Ok(ReconcileRowOut {
        symbol: symbol.unwrap_or_default(),
        side: side.unwrap_or_default(),
        planned_qty: planned,
        filled_qty: filled,
        planned_ref_price: reference,
        avg_fill_price: average,
        status: str_or_none(row.try_get("status").map_err(r::db)?),
        shortfall: filled - planned,
        slippage,
    })
}

/// `GET /desk/reconcile`: the last plan against what filled.
///
/// # Errors
/// 401, 404 (no desk schema, or no plans), 500.
pub async fn reconcile(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    desk_dependencies(&state, &principal).await?;
    let version = sqlx::query(
        "SELECT version_id, created_ts, note FROM \"desk\".rebalance_versions \
         ORDER BY created_ts DESC LIMIT 1",
    )
    .fetch_optional(state.db.pool())
    .await
    .map_err(r::db)?;
    let Some(version) = version else {
        return Err(Problem::new(
            ProblemType::NotFound,
            "the desk has recorded no plans yet.",
        ));
    };
    let version_id: String = version.try_get("version_id").map_err(r::db)?;
    let orders = sqlx::query(
        "SELECT symbol, side, planned_qty, planned_ref_price::text AS planned_ref_price, \
         filled_qty, avg_fill_price::text AS avg_fill_price, status \
         FROM \"desk\".rebalance_orders WHERE version_id = $1 ORDER BY side DESC, symbol",
    )
    .bind(&version_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let rows: Vec<ReconcileRowOut> = orders.iter().map(reconcile_row).collect::<Result<_, _>>()?;
    let complete = rows
        .iter()
        .filter(|r| r.filled_qty >= r.planned_qty && r.planned_qty > 0)
        .count();
    let unfilled = rows.iter().filter(|r| r.filled_qty == 0).count();
    let created_ts: f64 = version.try_get("created_ts").map_err(r::db)?;
    let created =
        r::utc_from_seconds(created_ts).ok_or_else(|| r::unexpected("created_ts out of range"))?;
    let n = |x: usize| i64::try_from(x).unwrap_or(i64::MAX);
    Ok(wire::json(&ReconcileOut {
        plan_id: version_id,
        created_at: r::py_isoformat_utc(created),
        note: str_or_none(version.try_get("note").map_err(r::db)?),
        planned_count: n(rows.len()),
        complete_count: n(complete),
        partial_count: n(rows.len()) - n(complete) - n(unfilled),
        unfilled_count: n(unfilled),
        settled: !rows.is_empty() && complete == rows.len(),
        rows,
    }))
}

/// The routes of `baskfy_api.routers.desk`, relative to `/api/v1`. All GET; there is no other.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/desk/performance", get(performance))
        .route("/desk/holdings", get(holdings))
        .route("/desk/tradebook", get(tradebook))
        .route("/desk/regime", get(regime))
        .route("/desk/reconcile", get(reconcile))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::float_cmp)]
    use super::*;

    #[test]
    fn pythons_percent_and_rebase() {
        // Python: round((112.345 - 100) / 100 * 100, 2) == 12.34 (the quotient is 12.3449…).
        assert_eq!(change_pct(Some(100.0), Some(112.345)), Some(12.34));
        assert_eq!(change_pct(Some(0.0), Some(1.0)), None);
        assert_eq!(change_pct(None, Some(1.0)), None);
        assert_eq!(rebase(Some(50.0), Some(200.0)), Some(25.0));
        assert_eq!(rebase(Some(50.0), Some(0.0)), None);
    }

    #[test]
    fn json_values_print_as_python_str() {
        let v = OrderedJson::parse(r#"["a", 1, 1.5, true, null, {"k": [2]}]"#)
            .unwrap_or(OrderedJson::Null);
        let OrderedJson::Array(items) = v else {
            unreachable!("parsed an array")
        };
        let out: Vec<String> = items.iter().map(py_str).collect();
        assert_eq!(out, vec!["a", "1", "1.5", "True", "None", "{'k': [2]}"]);
    }
}
