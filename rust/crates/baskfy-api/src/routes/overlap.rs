//! ``/overlap`` — the day's candidates across the sleeves. Ports part of
//! `baskfy_api.routers.overlap` (`APIRouter(prefix="/overlap", tags=["overlap"])`) and the
//! pieces of `baskfy_api.overlap` / `baskfy_api.overlap_scan` those routes call.
//!
//! Ported (the derivatives lane, RS5): `GET /tags/export` and `GET /reviews/export` (the two
//! fine-tuning sets, NDJSON) and `GET /catalyst-scan` (the filings scan's progress, from Redis).
//! All three are the sole tenant's (`surface="corrections"`).
//!
//! Ported (lane writes-sleeves, RS4c): `PUT`/`DELETE /tags` and `POST /catalyst-scan`
//! (`writes.rs`). Ported at wave 6 (lane api_routes, R5.21 and R6.29 closed): `GET ` — the
//! overlap read itself ([`view`], with `baskfy_core.candidate_review` in [`review`]) — and
//! `PUT`/`DELETE /reviews`, which re-read the row through that same view. The live-marks overlay
//! is the shared one, which in Rust is always the closed answer (no Kite quote client yet), so
//! `last_price` is `null` exactly when Python's is without a session.
//!
//! No route here reaches a rank, a size or an order: the tags and labels are display context.

pub mod review;
mod tags;
pub mod view;
mod writes;

use axum::Router;
use axum::extract::{Extension, RawQuery, Request, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, put};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;

use super::options::derivatives::shared::{
    cache_get, col, db_err, py_json_str_utf8, scoped_sole_user_id, validate_literal,
};
use super::sleeves::common::{J, Obj, canonical_response};
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

// --- The read, and the labels that re-read it (wave 6, lane api_routes) ----------------------

/// `Scope`.
const SCOPES: [&str; 2] = ["actionable", "all"];
/// The sole-tenant surface the read names when it degrades (strategies not read).
const OVERLAP: &str = "overlap";

fn opt_date_j(d: Option<NaiveDate>) -> J {
    J::opt_date(d)
}

fn tag_j(tag: &tags::Tag) -> J {
    tags::tag_out(tag)
}

/// `_opinion_out(opinion, laya)` (`OverlapOpinionOut`).
fn opinion_j(opinion: &review::ReviewOpinion, laya: Option<&review::ReviewOpinion>) -> J {
    Obj::new()
        .put("label", J::s(opinion.label))
        .put("confidence", J::Float(opinion.confidence))
        .put("source", J::s(opinion.source))
        .put("shown", J::Bool(review::shown(opinion)))
        .put("floor", J::Float(review::REVIEW_CONFIDENCE_FLOOR))
        .put(
            "labelled",
            J::Bool(opinion.source == review::SOURCE_LABELLED),
        )
        .put("reason", J::opt_str(opinion.reason.clone()))
        .put(
            "laya",
            laya.map_or(J::Null, |l| {
                Obj::new()
                    .put("label", J::s(l.label))
                    .put("confidence", J::Float(l.confidence))
                    .done()
            }),
        )
        .done()
}

/// `OverlapRowOut`.
fn row_j(row: &view::CandidateRow) -> J {
    let strategies = row
        .strategies
        .iter()
        .map(|h| {
            Obj::new()
                .put("strategy", J::s(h.strategy))
                .put("name", J::s(view::strategy_name(h.strategy)))
                .put("ref", J::s(view::strategy_ref(h.strategy)))
                .put("as_of", J::Date(h.as_of))
                .put("detail", J::s(&h.detail))
                .put("actionable", J::Bool(h.actionable))
                .done()
        })
        .collect();
    let screens = row
        .screens
        .iter()
        .map(|s| {
            Obj::new()
                .put("name", J::s(&s.name))
                .put("public_id", J::s(&s.public_id))
                .put("is_template", J::Bool(s.is_template))
                .put("as_of", J::Date(s.as_of))
                .put("rank", J::opt_int(s.rank))
                .put("of", J::opt_int(s.of))
                .put("definition_changed", J::Bool(s.definition_changed))
                .done()
        })
        .collect();
    let catalyst = match &row.catalyst {
        Some(c) if !c.empty() => Obj::new()
            .put("headline", J::opt_str(c.headline.clone()))
            .put("published_at", J::opt_dt(c.published_at))
            .put("url", J::opt_str(c.url.clone()))
            .put("earnings_date", opt_date_j(c.earnings_date))
            .put("tag", row.catalyst_tag.as_ref().map_or(J::Null, tag_j))
            .done(),
        _ => J::Null,
    };
    Obj::new()
        .put("instrument_id", J::Int(row.instrument_id))
        .put("symbol", J::s(&row.symbol))
        .put("name", J::s(&row.name))
        .put("close", J::opt_dec(row.close))
        // The shared live-marks overlay: always the closed answer in Rust (`/meta/live-marks`).
        .put("last_price", J::Null)
        .put(
            "strategy_count",
            J::Int(i64::try_from(row.strategy_count()).unwrap_or(i64::MAX)),
        )
        .put("actionable", J::Bool(row.actionable()))
        .put("strategies", J::Arr(strategies))
        .put("screens", J::Arr(screens))
        .put("catalyst", catalyst)
        .put(
            "opinion",
            row.opinion
                .as_ref()
                .map_or(J::Null, |o| opinion_j(o, row.laya.as_ref())),
        )
        .done()
}

/// The deployment's Redis, as `request.app.state.cache` (none when the URL is empty).
fn cache_url(state: &AppState) -> Option<&baskfy_providers::secret::Secret> {
    let url = &state.settings.redis_url;
    (!url.expose().is_empty()).then_some(url)
}

/// `datetime.now(UTC).date()`.
fn today_utc() -> NaiveDate {
    crate::routes::screens::write_kit::now_utc().date_naive()
}

/// `GET /overlap?scope=actionable|all` (`get_overlap`): read-only, nothing re-run.
///
/// # Errors
/// 401 anonymous; 400 a bad `scope`; 500 a database failure.
pub async fn get_overlap(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = crate::query::QueryParams::parse(raw.as_deref());
    let mut v = crate::query::Validation::new();
    let scope = validate_literal(&mut v, "scope", q.get("scope"), &SCOPES).unwrap_or("actionable");
    v.finish()?;
    let user_id = principal.require_user()?;
    let sole = scoped_sole_user_id(&state.db, Some(user_id), Some(OVERLAP))
        .await
        .ok();
    let cache = cache_url(&state);
    let mut conn = state.db.pool().acquire().await.map_err(db_err)?;
    let view = view::overlap(&mut conn, user_id, sole, scope == "all", cache, today_utc()).await?;
    drop(conn);
    let last_pass = view::laya_last_pass(cache).await;
    let (answered, shown, of) = view::laya_status(&view.rows);
    let out = Obj::new()
        .put(
            "sessions",
            Obj::new()
                .put("swing", opt_date_j(view.sessions[0]))
                .put("volume_breakout", opt_date_j(view.sessions[1]))
                .put("three_weeks_tight", opt_date_j(view.sessions[2]))
                .done(),
        )
        .put("scope", J::s(scope))
        .put("strategies_read", J::Bool(view.strategies_read))
        .put("screens_checked", J::Int(view.screens_checked))
        .put(
            "laya",
            Obj::new()
                .put("last_pass_at", last_pass.map_or(J::Null, J::DtFixed))
                .put("answered", J::Int(answered))
                .put("shown", J::Int(shown))
                .put("of", J::Int(of))
                .done(),
        )
        .put("data", J::Arr(view.rows.iter().map(row_j).collect()))
        .done();
    Ok(canonical_response(&out))
}

/// `_row_on_the_list(...)`: the row as the page shows it, re-read through the same view (scope
/// `all`); 404 when the name is not on today's list.
async fn row_on_the_list(
    state: &AppState,
    conn: &mut sqlx::PgConnection,
    user_id: i64,
    instrument_id: &crate::routes::common::PyInt,
) -> Result<view::CandidateRow, Problem> {
    let view = view::overlap(
        conn,
        user_id,
        Some(user_id),
        true,
        cache_url(state),
        today_utc(),
    )
    .await?;
    let wanted = instrument_id.as_i64();
    view.rows
        .into_iter()
        .find(|r| Some(r.instrument_id) == wanted)
        .ok_or_else(|| crate::problems::not_found("candidate", &instrument_id.canonical))
}

/// `OverlapReviewLabelIn`.
fn review_in() -> super::sleeves::pyd::Model {
    use super::sleeves::pyd::{Default as D, Model, Ty};
    Model {
        fields: vec![
            ("instrument_id", Ty::Int(Vec::new()), D::Required),
            ("label", Ty::Enum(&review::REVIEW_LABELS), D::Required),
            ("note", Ty::str_len(None, Some(2000)).opt(), D::Unset),
        ],
        forbid_extra: false,
        before: None,
        after: None,
    }
}

/// `PUT /overlap/reviews` (`put_review`): store a person's word on a row's state and answer with
/// the opinion the page now shows. Sole tenant only.
///
/// # Errors
/// 400 (body), 401, 404 (not on today's list) or 503 (tenant), 500.
pub async fn put_review(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<crate::session::WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    use super::sleeves::pyd::{PV, validate_body};
    let (content_type, bytes) = super::sleeves::writes::read_body(request).await?;
    let received = crate::body::receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let payload = validate_body(&received, &review_in()).map_err(|f| f.into_problem(Vec::new()))?;
    let user_id = principal.require_user()?;
    let user_id = scoped_sole_user_id(&state.db, Some(user_id), Some(CORRECTIONS)).await?;
    let Some(PV::Int(instrument_id)) = payload.field("instrument_id").cloned() else {
        return Err(internal_error());
    };
    let label: &'static str = match payload.field("label") {
        Some(PV::Str(l)) => review::REVIEW_LABELS
            .iter()
            .copied()
            .find(|x| x == l)
            .ok_or_else(internal_error)?,
        _ => return Err(internal_error()),
    };
    let note = match payload.field("note") {
        Some(PV::Str(n)) => {
            Some(super::sleeves::pyd::py_strip(n).to_owned()).filter(|n| !n.is_empty())
        }
        _ => None,
    };
    let pool = super::sleeves::writes::pool(write)?;
    let st = state.clone();
    let out = crate::session::run(&pool, move |tx| {
        Box::pin(async move {
            let row = row_on_the_list(&st, tx, user_id, &instrument_id).await?;
            let state_words = row.state();
            let key = review::review_key(&state_words);
            let cached = view::review_opinions(cache_url(&st), &[(key.clone(), state_words)]).await;
            let laya = cached.get(&key).cloned();
            let opinion = view::label_row(tx, user_id, &row, label, note, laya.as_ref()).await?;
            Ok(opinion_j(&opinion, laya.as_ref()))
        })
    })
    .await?;
    Ok(canonical_response(&out))
}

/// `DELETE /overlap/reviews?instrument_id=` (`delete_review`): the model's opinion shows again;
/// 404 when the name is not on today's list or there was no label.
///
/// # Errors
/// 400 (the query), 401, 404, 503, 500.
pub async fn delete_review(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<crate::session::WritePool>>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let q = crate::query::QueryParams::parse(raw.as_deref());
    let mut v = crate::query::Validation::new();
    let instrument_id = match v.required(&q, "instrument_id") {
        Some(text) => match crate::routes::common::lax_int(text) {
            Some(p) => Some(p),
            None => {
                v.error(
                    "instrument_id",
                    "Input should be a valid integer, unable to parse string as an integer",
                    "int_parsing",
                );
                None
            }
        },
        None => None,
    };
    v.finish()?;
    let instrument_id = instrument_id.ok_or_else(internal_error)?;
    let user_id = principal.require_user()?;
    let user_id = scoped_sole_user_id(&state.db, Some(user_id), Some(CORRECTIONS)).await?;
    let pool = super::sleeves::writes::pool(write)?;
    let st = state.clone();
    let id_text = instrument_id.canonical.clone();
    let removed = crate::session::run(&pool, move |tx| {
        Box::pin(async move {
            let row = row_on_the_list(&st, tx, user_id, &instrument_id).await?;
            view::clear_label(tx, user_id, &row).await
        })
    })
    .await?;
    if !removed {
        return Err(crate::problems::not_found("label", &id_text));
    }
    Ok(StatusCode::NO_CONTENT.into_response())
}

/// The `/overlap` routes, relative to `/api/v1` — every route of Python's router.
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/overlap/tags",
            put(writes::put_tag).delete(writes::delete_tag),
        )
        .route("/overlap", get(get_overlap))
        .route(
            "/overlap/reviews",
            put(self::put_review).delete(self::delete_review),
        )
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
