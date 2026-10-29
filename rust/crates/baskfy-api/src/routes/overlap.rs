//! ``/overlap`` — the day's candidates across the sleeves. Ports part of
//! `baskfy_api.routers.overlap` (`APIRouter(prefix="/overlap", tags=["overlap"])`) and the
//! pieces of `baskfy_api.overlap` / `baskfy_api.overlap_scan` those routes call.
//!
//! Ported (the derivatives lane, RS5): `GET /tags/export` and `GET /reviews/export` (the two
//! fine-tuning sets, NDJSON) and `GET /catalyst-scan` (the filings scan's progress, from Redis).
//! All three are the sole tenant's (`surface="corrections"`).
//!
//! **Not ported yet:** `GET ` — the overlap read itself. It needs `baskfy_core.candidate_review`
//! (the row's state in words and the rules' opinion), `baskfy_core.catalyst_tags` (the keyword
//! tagger and its resolution against Laya's cached answer), the screen-definition hash, the
//! shared live-marks overlay, and two Redis writes Python makes on that read (the sidecar's
//! "wanted" set and hash). None has a Rust home yet (DECISIONS-RS R5.21); Python keeps serving it.
//! Ported (lane writes-sleeves, RS4c): `PUT`/`DELETE /tags` and `POST /catalyst-scan`
//! (`writes.rs`). **Not registered:** `PUT`/`DELETE /reviews` — they re-read the row through the
//! unported `GET ` above, so Python keeps serving them (DECISIONS-RS R6.29).
//!
//! No route here reaches a rank, a size or an order: the tags and labels are display context.

mod tags;
mod writes;

use axum::Router;
use axum::extract::{Extension, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, put};
use baskfy_db::OrderedJson;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;

use super::options::derivatives::shared::{
    cache_get, col, db_err, py_json_str_utf8, scoped_sole_user_id,
};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, internal_error};
use crate::state::AppState;
use crate::wire;

/// `NDJSON_MEDIA_TYPE`.
pub const NDJSON_MEDIA_TYPE: &str = "application/x-ndjson";
/// The surface the corrections routes name in a tenant refusal.
const CORRECTIONS: &str = "corrections";

/// A value `baskfy_core.screener.canonical_json` encodes: insertion-ordered objects, decimals
/// unquoted in fixed notation, strings with `ensure_ascii=False`, numbers as `json.dumps`.
#[derive(Debug, Clone)]
enum Canon {
    Null,
    Str(String),
    Dec(Decimal),
    Json(OrderedJson),
    Obj(Vec<(String, Canon)>),
}

fn canonical(value: &Canon, out: &mut String) -> Result<(), Problem> {
    match value {
        Canon::Null => out.push_str("null"),
        Canon::Str(s) => out.push_str(&py_json_str_utf8(s)),
        Canon::Dec(d) => {
            // `format(value, "f")`, `-0` kept as Python keeps it.
            let text = baskfy_core::decimal::WideDecimal::from(*d).to_string();
            out.push_str(&text);
        }
        Canon::Json(j) => canonical_json(j, out)?,
        Canon::Obj(entries) => {
            out.push('{');
            for (i, (k, v)) in entries.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&py_json_str_utf8(k));
                out.push(':');
                canonical(v, out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// `canonical_json` over a value read from `jsonb` (a dict keeps its stored order).
fn canonical_json(value: &OrderedJson, out: &mut String) -> Result<(), Problem> {
    match value {
        OrderedJson::Str(s) => out.push_str(&py_json_str_utf8(s)),
        OrderedJson::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                canonical_json(item, out)?;
            }
            out.push(']');
        }
        OrderedJson::Object(entries) => {
            out.push('{');
            for (i, (k, v)) in entries.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&py_json_str_utf8(k));
                out.push(':');
                canonical_json(v, out)?;
            }
            out.push('}');
        }
        // `json.dumps` of a scalar: null, true/false, an int, `repr(float)` (NaN refused).
        other => out.push_str(&other.to_python_json().map_err(|_| internal_error())?),
    }
    Ok(())
}

/// `datetime.isoformat()` of a UTC instant: `+00:00`, microseconds only when set.
fn py_isoformat_utc(t: DateTime<Utc>) -> String {
    let base = wire::py_datetime(t);
    format!("{}+00:00", base.trim_end_matches('Z'))
}

fn ndjson(lines: Vec<String>) -> Response {
    let body: String = lines.into_iter().map(|l| l + "\n").collect();
    let mut response = (StatusCode::OK, body).into_response();
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static(NDJSON_MEDIA_TYPE),
    );
    response
}

fn opt_str(value: Option<String>) -> Canon {
    value.map_or(Canon::Null, Canon::Str)
}

fn opt_dec(value: Option<Decimal>) -> Canon {
    value.map_or(Canon::Null, Canon::Dec)
}

/// `EventType` / `ReviewLabel` values, which `EventType(row.event_type)` would refuse otherwise.
const EVENT_TYPES: [&str; 8] = [
    "earnings",
    "order",
    "approval",
    "fundraising",
    "governance",
    "corporate_action",
    "routine",
    "other",
];
const REVIEW_LABELS: [&str; 3] = ["look_first", "worth_a_look", "skip"];

fn enum_value(value: Option<String>, allowed: &[&str]) -> Result<Canon, Problem> {
    match value {
        None => Ok(Canon::Null),
        Some(v) if allowed.contains(&v.as_str()) => Ok(Canon::Str(v)),
        Some(_) => Err(internal_error()),
    }
}

/// `GET /overlap/tags/export`: every correction, oldest first, one JSON document per line.
///
/// # Errors
/// 401 anonymous; 404 not the sole tenant; 503 none configured; 500 a database failure.
pub async fn get_tags_export(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let user_id = scoped_sole_user_id(&state.db, Some(user_id), Some(CORRECTIONS)).await?;
    let rows = sqlx::query(
        "SELECT headline, event_type, rules_event_type, laya_event_type, laya_confidence, note, \
         updated_at FROM catalyst_tag_correction WHERE user_id = $1 ORDER BY created_at, id",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db_err)?;
    let mut lines = Vec::with_capacity(rows.len());
    for r in &rows {
        let record = Canon::Obj(vec![
            ("headline".into(), Canon::Str(col::text(r, "headline")?)),
            (
                "event_type".into(),
                enum_value(Some(col::text(r, "event_type")?), &EVENT_TYPES)?,
            ),
            (
                "rules_event_type".into(),
                enum_value(col::opt_text(r, "rules_event_type")?, &EVENT_TYPES)?,
            ),
            (
                "laya_event_type".into(),
                enum_value(col::opt_text(r, "laya_event_type")?, &EVENT_TYPES)?,
            ),
            (
                "laya_confidence".into(),
                opt_dec(col::opt_dec(r, "laya_confidence")?),
            ),
            ("note".into(), opt_str(col::opt_text(r, "note")?)),
            (
                "corrected_at".into(),
                Canon::Str(py_isoformat_utc(col::ts(r, "updated_at")?)),
            ),
        ]);
        let mut line = String::new();
        canonical(&record, &mut line)?;
        lines.push(line);
    }
    Ok(ndjson(lines))
}

/// `GET /overlap/reviews/export`: every row label, oldest first, one JSON document per line.
///
/// # Errors
/// As [`get_tags_export`].
pub async fn get_reviews_export(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let user_id = scoped_sole_user_id(&state.db, Some(user_id), Some(CORRECTIONS)).await?;
    let rows = sqlx::query(
        "SELECT symbol, state, label, laya_label, laya_confidence, note, updated_at \
         FROM candidate_review_label WHERE user_id = $1 ORDER BY created_at, id",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db_err)?;
    let mut lines = Vec::with_capacity(rows.len());
    for r in &rows {
        let record = Canon::Obj(vec![
            ("symbol".into(), Canon::Str(col::text(r, "symbol")?)),
            ("state".into(), Canon::Json(col::object(r, "state")?)),
            (
                "label".into(),
                enum_value(Some(col::text(r, "label")?), &REVIEW_LABELS)?,
            ),
            (
                "laya_label".into(),
                enum_value(col::opt_text(r, "laya_label")?, &REVIEW_LABELS)?,
            ),
            (
                "laya_confidence".into(),
                opt_dec(col::opt_dec(r, "laya_confidence")?),
            ),
            ("note".into(), opt_str(col::opt_text(r, "note")?)),
            (
                "labelled_at".into(),
                Canon::Str(py_isoformat_utc(col::ts(r, "updated_at")?)),
            ),
        ]);
        let mut line = String::new();
        canonical(&record, &mut line)?;
        lines.push(line);
    }
    Ok(ndjson(lines))
}

/// `overlap_scan.status_key(user_id)`.
#[must_use]
pub fn status_key(user_id: i64) -> String {
    format!("overlap:catalyst-scan:{user_id}")
}

/// `ScanState`.
const SCAN_STATES: [&str; 5] = ["idle", "queued", "running", "done", "failed"];
/// `ScanScope`.
const SCAN_SCOPES: [&str; 2] = ["actionable", "all"];

/// pydantic's lax `int | None` from JSON (a bool is an int; an integral float is accepted).
fn lax_opt_int(v: Option<&OrderedJson>) -> Result<Canon, Problem> {
    match v {
        None | Some(OrderedJson::Null) => Ok(Canon::Null),
        Some(v) => lax_int(v).map(|j| Canon::Json(OrderedJson::Int(j))),
    }
}

fn lax_int(v: &OrderedJson) -> Result<i64, Problem> {
    match v {
        OrderedJson::Int(i) => Ok(*i),
        OrderedJson::Bool(b) => Ok(i64::from(*b)),
        #[allow(clippy::cast_possible_truncation)]
        OrderedJson::Float(f) if f.fract() == 0.0 && f.abs() < 9.0e15 => Ok(*f as i64),
        OrderedJson::Str(s) => super::options::derivatives::shared::lax_int(s)
            .and_then(|p| p.value)
            .and_then(|x| i64::try_from(x).ok())
            .ok_or_else(internal_error),
        _ => Err(internal_error()),
    }
}

fn opt_string(v: Option<&OrderedJson>) -> Result<Canon, Problem> {
    match v {
        None | Some(OrderedJson::Null) => Ok(Canon::Null),
        Some(OrderedJson::Str(s)) => Ok(Canon::Str(s.clone())),
        Some(_) => Err(internal_error()),
    }
}

/// `OverlapScanOut.model_validate(status)` dumped by `canonical_json`: every field in order,
/// defaults filled, unknown keys ignored; a value the model refuses is the 500 Python raises.
pub(crate) fn scan_out(status: &OrderedJson) -> Result<String, Problem> {
    let state = match status.get("state") {
        Some(OrderedJson::Str(s)) if SCAN_STATES.contains(&s.as_str()) => Canon::Str(s.clone()),
        _ => return Err(internal_error()),
    };
    let scope = match status.get("scope") {
        None | Some(OrderedJson::Null) => Canon::Null,
        Some(OrderedJson::Str(s)) if SCAN_SCOPES.contains(&s.as_str()) => Canon::Str(s.clone()),
        Some(_) => return Err(internal_error()),
    };
    let done = match status.get("done") {
        None => 0,
        Some(v) => lax_int(v)?,
    };
    let failed = match status.get("failed") {
        None => Vec::new(),
        Some(OrderedJson::Array(items)) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                match item {
                    OrderedJson::Str(s) => out.push(OrderedJson::Str(s.clone())),
                    _ => return Err(internal_error()),
                }
            }
            out
        }
        Some(_) => return Err(internal_error()),
    };
    let record = Canon::Obj(vec![
        ("state".into(), state),
        ("scope".into(), scope),
        ("total".into(), lax_opt_int(status.get("total"))?),
        ("done".into(), Canon::Json(OrderedJson::Int(done))),
        (
            "announcements".into(),
            lax_opt_int(status.get("announcements"))?,
        ),
        (
            "earnings_dates".into(),
            lax_opt_int(status.get("earnings_dates"))?,
        ),
        (
            "rows_written".into(),
            lax_opt_int(status.get("rows_written"))?,
        ),
        ("failed".into(), Canon::Json(OrderedJson::Array(failed))),
        ("error".into(), opt_string(status.get("error"))?),
        ("queued_at".into(), opt_string(status.get("queued_at"))?),
        ("started_at".into(), opt_string(status.get("started_at"))?),
        ("finished_at".into(), opt_string(status.get("finished_at"))?),
    ]);
    let mut out = String::new();
    canonical(&record, &mut out)?;
    Ok(out)
}

/// `GET /overlap/catalyst-scan`: `state: idle` when no scan has run; otherwise the last one's
/// progress, as the worker wrote it to Redis.
///
/// # Errors
/// As [`get_tags_export`]; 500 a stored status the model refuses.
pub async fn get_catalyst_scan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let user_id = scoped_sole_user_id(&state.db, Some(user_id), Some(CORRECTIONS)).await?;
    let idle = OrderedJson::Object(vec![("state".into(), OrderedJson::Str("idle".into()))]);
    // `read_status`: a missing key, an unreachable Redis or a non-object is "idle"; text that is
    // not JSON escapes as Python's `JSONDecodeError` (the 500 catch-all).
    let status = match cache_get(&state.settings.redis_url, &status_key(user_id)).await {
        None => idle,
        Some(raw) => {
            let text = String::from_utf8(raw).map_err(|_| internal_error())?;
            match OrderedJson::parse(&text).map_err(|_| internal_error())? {
                parsed @ OrderedJson::Object(_) => parsed,
                _ => idle,
            }
        }
    };
    let body = scan_out(&status)?;
    let mut response = (StatusCode::OK, body).into_response();
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static(wire::JSON));
    Ok(response)
}

/// The ported `/overlap` routes, relative to `/api/v1` (the read itself and the writes stay on
/// Python).
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/overlap/tags",
            put(writes::put_tag).delete(writes::delete_tag),
        )
        .route("/overlap/reviews", super::common::write_only())
        .route("/overlap/tags/export", get(get_tags_export))
        .route("/overlap/reviews/export", get(get_reviews_export))
        .route(
            "/overlap/catalyst-scan",
            get(get_catalyst_scan).post(writes::post_catalyst_scan),
        )
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_idle_status_has_every_default() {
        let idle = OrderedJson::parse(r#"{"state": "idle"}"#).unwrap();
        assert_eq!(
            scan_out(&idle).unwrap(),
            r#"{"state":"idle","scope":null,"total":null,"done":0,"announcements":null,"earnings_dates":null,"rows_written":null,"failed":[],"error":null,"queued_at":null,"started_at":null,"finished_at":null}"#
        );
        let queued = OrderedJson::parse(
            r#"{"state": "queued", "scope": "all", "queued_at": "2026-10-30T10:00:00+00:00", "total": null, "done": 0, "task_id": "x"}"#,
        )
        .unwrap();
        assert!(
            scan_out(&queued)
                .unwrap()
                .starts_with(r#"{"state":"queued","scope":"all","total":null,"done":0,"#)
        );
        let bad = OrderedJson::parse(r#"{"state": "lost"}"#).unwrap();
        assert!(scan_out(&bad).is_err());
    }

    #[test]
    fn canonical_json_keeps_decimals_exact_and_order() {
        let mut out = String::new();
        canonical(
            &Canon::Obj(vec![
                ("b".into(), Canon::Dec("0.8100".parse().unwrap())),
                ("a".into(), Canon::Str("é".into())),
                ("c".into(), Canon::Null),
            ]),
            &mut out,
        )
        .unwrap();
        assert_eq!(out, r#"{"b":0.8100,"a":"é","c":null}"#);
    }

    #[test]
    fn isoformat_is_pythons() {
        let t = DateTime::parse_from_rfc3339("2026-10-30T01:02:03.000500Z").unwrap();
        assert_eq!(
            py_isoformat_utc(t.with_timezone(&Utc)),
            "2026-10-30T01:02:03.000500+00:00"
        );
        let t = DateTime::parse_from_rfc3339("2026-10-30T01:02:03Z").unwrap();
        assert_eq!(
            py_isoformat_utc(t.with_timezone(&Utc)),
            "2026-10-30T01:02:03+00:00"
        );
    }
}
