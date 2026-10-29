//! ``/baskets`` and ``/baskets/plan`` — M22, the merged face.
//!
//! Ports `baskfy_api.routers.baskets`. Python declares
//! `APIRouter(tags=["baskets"], dependencies=[Depends(require_authenticated)])`.
//!
//! * `GET /baskets/plan` — ported whole: the desk's newest rebalance plan from the `desk` schema
//!   (a deployment without that schema answers 404, not 500).
//! * `GET /baskets` — ported for the **nightly snapshot** (`basket_snapshot`, M30). Python's
//!   fallback when no snapshot exists builds the basket live (bars → `compute_factors` →
//!   `MomentumScan` → `score` → the basket engine, plus the desk's newest uploaded scan CSV),
//!   none of which exists in Rust before the desk wave; this server answers that case with the
//!   404 Python gives when the live build misses its 3 s budget (DECISIONS-RS, lane api-sleeves).
//!
//! READ-ONLY: there is no write here and there never will be — execution stays in the desk
//! console. **No route may place, modify or cancel an order** (law 2).

use axum::Router;
use axum::extract::{Extension, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_db::OrderedJson;
use chrono::DateTime;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType};
use crate::query::{QueryParams, Validation};
use crate::routes::sleeves::common::{J, Obj, db, get as col, py_str_json, pydantic_response};
use crate::state::AppState;

/// `DESK_SCHEMA`.
pub const DESK_SCHEMA: &str = "desk";

/// Postgres's `undefined_table`: what `is_missing_desk_data` matches.
const UNDEFINED_TABLE: &str = "42P01";

fn invalid_payload() -> Problem {
    crate::logging::error(
        "baskfy_api.app",
        "unhandled error",
        &[(
            "error",
            "basket_snapshot.payload does not validate as BasketOut",
        )],
    );
    crate::problems::internal_error()
}

/// pydantic's lax `int` from a stored JSON value: a number, a bool, or a numeric string
/// (`" 7 "`, `"1_0"`, `"7.0"`), as `BasketOut.model_validate` accepts them.
fn as_int(v: Option<&OrderedJson>) -> Result<i64, Problem> {
    match v {
        Some(OrderedJson::Int(i)) => Ok(*i),
        Some(OrderedJson::Bool(b)) => Ok(i64::from(*b)),
        #[allow(clippy::cast_possible_truncation)]
        Some(OrderedJson::Float(f)) if f.is_finite() && f.fract() == 0.0 => Ok(*f as i64),
        Some(OrderedJson::Str(s)) => match crate::routes::sleeves::common::lax_int(s) {
            crate::routes::sleeves::common::LaxInt::Small(i) => Ok(i),
            _ => Err(invalid_payload()),
        },
        _ => Err(invalid_payload()),
    }
}

/// pydantic-core's `str` -> `float`: Rust's float grammar on the trimmed text (which takes
/// `inf`, `nan`, `.5`, `5.`, `1e3` and a sign, as Python does); failing that, the **untrimmed**
/// text with its underscores removed, refused when one leads, trails or doubles. So `"1_000.5"`
/// and `"1_.5"` parse while `" 1_000"` does not, exactly as pydantic 2.13 answers.
fn lax_float_str(raw: &str) -> Option<f64> {
    if let Ok(f) = raw.trim().parse::<f64>() {
        return Some(f);
    }
    if !raw.contains('_') || raw.starts_with('_') || raw.ends_with('_') || raw.contains("__") {
        return None;
    }
    raw.replace('_', "").parse::<f64>().ok()
}

/// pydantic's lax `float` from a stored JSON value: a number, a bool, or a numeric string.
fn as_float(v: Option<&OrderedJson>) -> Result<f64, Problem> {
    match v {
        #[allow(clippy::cast_precision_loss)]
        Some(OrderedJson::Int(i)) => Ok(*i as f64),
        #[allow(clippy::cast_precision_loss)]
        Some(OrderedJson::UInt(u)) => Ok(*u as f64),
        Some(OrderedJson::Bool(b)) => Ok(if *b { 1.0 } else { 0.0 }),
        Some(OrderedJson::Float(f)) => Ok(*f),
        Some(OrderedJson::Str(s)) => lax_float_str(s).ok_or_else(invalid_payload),
        _ => Err(invalid_payload()),
    }
}

fn as_opt_float(v: Option<&OrderedJson>) -> Result<J, Problem> {
    match v {
        None | Some(OrderedJson::Null) => Ok(J::Null),
        other => as_float(other).map(J::Float),
    }
}

fn as_str(v: Option<&OrderedJson>) -> Result<String, Problem> {
    match v {
        Some(OrderedJson::Str(s)) => Ok(s.clone()),
        _ => Err(invalid_payload()),
    }
}

/// `BasketOut.model_validate(stored)`, re-emitted in the model's field order.
fn basket_out(stored: &OrderedJson) -> Result<J, Problem> {
    let rows = match stored.get("rows") {
        Some(OrderedJson::Array(items)) => items,
        _ => return Err(invalid_payload()),
    };
    let mut row_js = Vec::with_capacity(rows.len());
    for r in rows {
        row_js.push(
            Obj::new()
                .put("rank", J::Int(as_int(r.get("rank"))?))
                .put("symbol", J::Str(as_str(r.get("symbol"))?))
                .put("score", J::Float(as_float(r.get("score"))?))
                .put("weight", J::Float(as_float(r.get("weight"))?))
                .put("ref_price", J::Float(as_float(r.get("ref_price"))?))
                .put("stop", J::Float(as_float(r.get("stop"))?))
                .put("value", J::Int(as_int(r.get("value"))?))
                .put("a_trend", as_opt_float(r.get("a_trend"))?)
                .put("b_momentum", as_opt_float(r.get("b_momentum"))?)
                .put("c_sharpe", as_opt_float(r.get("c_sharpe"))?)
                .put("d_consistency", as_opt_float(r.get("d_consistency"))?)
                .put("e_liquidity", as_opt_float(r.get("e_liquidity"))?)
                .put("f_penalty", as_opt_float(r.get("f_penalty"))?)
                .done(),
        );
    }
    let suspects = match stored.get("suspect_symbols") {
        Some(OrderedJson::Array(items)) => items
            .iter()
            .map(|i| as_str(Some(i)).map(J::Str))
            .collect::<Result<Vec<_>, _>>()?,
        _ => return Err(invalid_payload()),
    };
    Ok(Obj::new()
        .put("as_of", J::Str(as_str(stored.get("as_of"))?))
        .put(
            "screen_run_id",
            J::Str(as_str(stored.get("screen_run_id"))?),
        )
        .put("data_version", J::Int(as_int(stored.get("data_version"))?))
        .put("capital", J::Int(as_int(stored.get("capital"))?))
        .put(
            "cash_target_pct",
            J::Float(as_float(stored.get("cash_target_pct"))?),
        )
        .put(
            "breadth_above_20dma",
            J::Float(as_float(stored.get("breadth_above_20dma"))?),
        )
        .put("suspect_symbols", J::Arr(suspects))
        .put("rows", J::Arr(row_js))
        .done())
}

/// `GET /baskets?date=`: the nightly basket snapshot (see the module docs for the live fallback).
///
/// # Errors
/// 401 anonymous; 400 for a bad date; 404 when no snapshot exists; 500.
pub async fn current_basket(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let date = v.date("date", q.get("date"));
    v.finish()?;
    let row = match date {
        Some(d) => {
            sqlx::query(
                "SELECT payload FROM basket_snapshot WHERE as_of = $1 \
             ORDER BY as_of DESC, data_version DESC LIMIT 1",
            )
            .bind(d)
            .fetch_optional(state.db.pool())
            .await
        }
        None => sqlx::query(
            "SELECT payload FROM basket_snapshot ORDER BY as_of DESC, data_version DESC LIMIT 1",
        )
        .fetch_optional(state.db.pool())
        .await,
    }
    .map_err(db)?;
    let Some(row) = row else {
        return Err(Problem::new(
            ProblemType::NotFound,
            "no basket snapshot is ready yet, and the live rebuild exceeded the 3s page budget. \
             Wait for the nightly basket_snapshot job, or retry later.",
        ));
    };
    let payload =
        baskfy_db::row::jsonb(&row, "payload").map_err(crate::routes::sleeves::common::dbe)?;
    Ok(pydantic_response(&basket_out(&payload)?))
}

/// Python's `float(x)` for a driver `NUMERIC` (`_f`): the exact decimal, correctly rounded.
fn float_of(d: rust_decimal::Decimal) -> f64 {
    crate::wire::py_decimal(d)
        .parse::<f64>()
        .unwrap_or(f64::NAN)
}

/// `dt.datetime.fromtimestamp(ts, tz=UTC).isoformat()`: microseconds rounded half-even.
fn from_timestamp_iso(ts: f64) -> Result<String, Problem> {
    let mut secs = ts.trunc();
    let mut micros = (ts.fract() * 1e6).round_ties_even();
    if micros >= 1e6 {
        secs += 1.0;
        micros -= 1e6;
    } else if micros < 0.0 {
        secs -= 1.0;
        micros += 1e6;
    }
    #[allow(clippy::cast_possible_truncation)]
    let (secs, micros) = (secs as i64, micros as u32);
    let t = DateTime::from_timestamp(secs, micros * 1000)
        .ok_or_else(crate::problems::internal_error)?;
    Ok(crate::routes::sleeves::common::py_isoformat(
        &t.fixed_offset(),
    ))
}

/// Python's `float(text)`: stripped, an underscore only between two digits, then the float
/// grammar (`inf`, `nan`, `.5`, `5.`, `1e3`, a sign). Non-ASCII digits are not read.
fn py_float_str(raw: &str) -> Option<f64> {
    let s = raw.trim();
    let bytes = s.as_bytes();
    for (i, b) in bytes.iter().enumerate() {
        if *b == b'_' {
            let before = i.checked_sub(1).and_then(|j| bytes.get(j));
            let after = bytes.get(i + 1);
            if !(before.is_some_and(u8::is_ascii_digit) && after.is_some_and(u8::is_ascii_digit)) {
                return None;
            }
        }
    }
    s.replace('_', "").parse::<f64>().ok()
}

/// Python's `float(v)` for a stored weight (`_f`): numbers and numeric strings.
fn weight_float(v: &OrderedJson) -> Result<Option<f64>, Problem> {
    match v {
        OrderedJson::Null => Ok(None),
        OrderedJson::Bool(b) => Ok(Some(if *b { 1.0 } else { 0.0 })),
        #[allow(clippy::cast_precision_loss)]
        OrderedJson::Int(i) => Ok(Some(*i as f64)),
        #[allow(clippy::cast_precision_loss)]
        OrderedJson::UInt(u) => Ok(Some(*u as f64)),
        OrderedJson::Float(f) => Ok(Some(*f)),
        OrderedJson::Str(s) => py_float_str(s)
            .map(Some)
            .ok_or_else(crate::problems::internal_error),
        _ => Err(crate::problems::internal_error()),
    }
}

fn parse_json_text(value: Option<String>) -> Result<Option<OrderedJson>, Problem> {
    match value {
        None => Ok(None),
        Some(s) if s.is_empty() => Ok(None),
        Some(s) => OrderedJson::parse(&s)
            .map(Some)
            .map_err(|_| crate::problems::internal_error()),
    }
}

/// `GET /baskets/plan`: the desk's most recent rebalance plan, in full.
///
/// # Errors
/// 401 anonymous; 404 when the desk has recorded no plan (or has no schema here); 500.
pub async fn latest_plan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let no_plans = || Problem::new(ProblemType::NotFound, "the desk has recorded no plans yet.");
    let version = sqlx::query(&format!(
        "SELECT version_id::text AS version_id, created_ts::float8 AS created_ts, \
                note::text AS note, evaluation_id::text AS evaluation_id, \
                constituents_json::text AS constituents_json, weights_json::text AS weights_json \
         FROM \"{DESK_SCHEMA}\".rebalance_versions ORDER BY created_ts DESC LIMIT 1"
    ))
    .fetch_optional(state.db.pool())
    .await;
    let version = match version {
        Ok(v) => v,
        Err(e) => {
            let e: baskfy_db::DbError = e.into();
            if e.sqlstate() == Some(UNDEFINED_TABLE) {
                return Err(no_plans());
            }
            return Err(crate::db_failure(&e));
        }
    };
    let Some(version) = version else {
        return Err(no_plans());
    };
    let version_id: Option<String> = col(&version, "version_id")?;
    let orders = sqlx::query(&format!(
        "SELECT symbol::text AS symbol, side::text AS side, planned_qty::bigint AS planned_qty, \
                planned_ref_price, filled_qty::bigint AS filled_qty, avg_fill_price, \
                status::text AS status, order_id::text AS order_id, \
                reconciled_at::text AS reconciled_at \
         FROM \"{DESK_SCHEMA}\".rebalance_orders WHERE version_id = $1 \
         ORDER BY side DESC, symbol"
    ))
    .bind(version_id.clone())
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;

    let created: f64 = col(&version, "created_ts")?;
    let constituents = match parse_json_text(col(&version, "constituents_json")?)? {
        Some(OrderedJson::Array(items)) => items.iter().map(|i| J::Str(py_str_json(i))).collect(),
        _ => Vec::new(),
    };
    let mut weights = Vec::new();
    if let Some(OrderedJson::Object(entries)) = parse_json_text(col(&version, "weights_json")?)? {
        for (k, v) in &entries {
            if let Some(f) = weight_float(v)? {
                weights.push((k.clone(), J::Float(f)));
            }
        }
    }
    let mut order_js = Vec::with_capacity(orders.len());
    for o in &orders {
        let reconciled: Option<String> = col(o, "reconciled_at")?;
        order_js.push(
            Obj::new()
                .put("symbol", J::Str(col(o, "symbol")?))
                .put("side", J::Str(col(o, "side")?))
                .put(
                    "planned_qty",
                    J::Int(col::<Option<i64>>(o, "planned_qty")?.unwrap_or(0)),
                )
                .put(
                    "planned_ref_price",
                    crate::routes::sleeves::common::odec(o, "planned_ref_price")?
                        .map_or(J::Null, |d| J::Float(float_of(d))),
                )
                .put("filled_qty", J::opt_int(col(o, "filled_qty")?))
                .put(
                    "avg_fill_price",
                    crate::routes::sleeves::common::odec(o, "avg_fill_price")?
                        .map_or(J::Null, |d| J::Float(float_of(d))),
                )
                .put("status", J::opt_str(col(o, "status")?))
                .put("order_id", J::opt_str(col(o, "order_id")?))
                .put(
                    "reconciled_at",
                    J::opt_str(reconciled.filter(|r| !r.is_empty())),
                )
                .done(),
        );
    }
    let body = Obj::new()
        .put(
            "plan_id",
            J::Str(version_id.unwrap_or_else(|| "None".to_owned())),
        )
        .put("created_at", J::Str(from_timestamp_iso(created)?))
        .put("note", J::opt_str(col(&version, "note")?))
        .put("evaluation_id", J::opt_str(col(&version, "evaluation_id")?))
        .put("constituents", J::Arr(constituents))
        .put("weights", J::Obj(weights))
        .put("orders", J::Arr(order_js))
        .done();
    Ok(pydantic_response(&body))
}

/// The routes of `baskfy_api.routers.baskets`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/baskets", get(current_basket))
        .route("/baskets/plan", get(latest_plan))
}

#[cfg(test)]
mod tests {
    //! The structural half of `test_baskets_readonly.py` and the pure conversions.
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn timestamps_are_pythons_fromtimestamp() {
        assert_eq!(
            from_timestamp_iso(1_791_958_195.850_439).unwrap(),
            "2026-10-14T06:09:55.850439+00:00"
        );
        assert_eq!(
            from_timestamp_iso(1_791_958_195.0).unwrap(),
            "2026-10-14T06:09:55+00:00"
        );
    }

    #[test]
    fn numerics_become_the_nearest_double() {
        use std::str::FromStr;
        let d = rust_decimal::Decimal::from_str("25.66").unwrap();
        assert_eq!(float_of(d).to_bits(), 25.66_f64.to_bits());
    }

    #[test]
    fn a_snapshot_is_re_emitted_in_field_order() {
        let stored = OrderedJson::parse(
            r#"{"rows":[{"symbol":"A","rank":1,"score":86,"weight":7.76,"ref_price":2244.0,"stop":2039.3,"value":776424,"a_trend":25.0}],"as_of":"2026-10-29","screen_run_id":"x","data_version":1,"capital":10000000,"cash_target_pct":0.0,"breadth_above_20dma":35.6,"suspect_symbols":["B"]}"#,
        )
        .unwrap();
        let out = basket_out(&stored).unwrap().pydantic();
        assert_eq!(
            out,
            r#"{"as_of":"2026-10-29","screen_run_id":"x","data_version":1,"capital":10000000,"cash_target_pct":0.0,"breadth_above_20dma":35.6,"suspect_symbols":["B"],"rows":[{"rank":1,"symbol":"A","score":86.0,"weight":7.76,"ref_price":2244.0,"stop":2039.3,"value":776424,"a_trend":25.0,"b_momentum":null,"c_sharpe":null,"d_consistency":null,"e_liquidity":null,"f_penalty":null}]}"#
        );
    }

    #[test]
    fn stored_numeric_strings_are_read_as_pydantic_reads_them() {
        let f = |s: &str| lax_float_str(s);
        assert_eq!(f(" 1.5 "), Some(1.5));
        assert_eq!(f("1_000.5"), Some(1000.5));
        assert_eq!(f(" 1_000.5 "), None);
        assert_eq!(f("1_.5"), Some(1.5));
        assert_eq!(f("1e1_0"), Some(1e10));
        assert_eq!(f(".5"), Some(0.5));
        assert_eq!(f("5."), Some(5.0));
        assert_eq!(f("1e3"), Some(1000.0));
        assert!(f("-Infinity").is_some_and(|x| x.is_infinite() && x < 0.0));
        assert!(f("NaN").is_some_and(f64::is_nan));
        assert_eq!(f("0x10"), None);
        assert_eq!(f("1__0"), None);
        assert_eq!(f("_1"), None);
        assert_eq!(f(""), None);
        assert_eq!(py_float_str(" 1_000.5 "), Some(1000.5));
        assert_eq!(py_float_str("1_.5"), None);
        assert_eq!(py_float_str("1_e5"), None);
        assert_eq!(py_float_str("1e1_0"), Some(1e10));
        assert_eq!(py_float_str(" -inf "), Some(f64::NEG_INFINITY));
        let s = |v: &str| Some(OrderedJson::Str(v.to_owned()));
        assert_eq!(as_int(s(" 7 ").as_ref()).unwrap(), 7);
        assert_eq!(as_int(s("1_0").as_ref()).unwrap(), 10);
        assert_eq!(as_int(s("7.0").as_ref()).unwrap(), 7);
        assert!(as_int(s("1.5").as_ref()).is_err());
        assert!(as_int(s("1e2").as_ref()).is_err());
    }

    #[test]
    fn the_module_has_no_write_and_no_execution_import() {
        let source = include_str!("baskets.rs");
        let code = source.split("#[cfg(test)]").next().unwrap();
        for verb in [
            "routing::post",
            "routing::put",
            "routing::patch",
            "routing::delete",
        ] {
            assert!(!code.contains(verb), "{verb}");
        }
        for verb in [" post(", " patch(", " delete(", "get(current_basket).post"] {
            assert!(!code.contains(verb), "{verb}");
        }
        assert!(!code.contains("baskfy_execution"));
    }
}
