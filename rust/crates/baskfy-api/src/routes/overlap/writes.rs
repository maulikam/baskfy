//! The overlap page's money-free writes (lane writes-sleeves): `PUT /overlap/tags` (a person's
//! correction of a headline's tag), `DELETE /overlap/tags` and `POST /overlap/catalyst-scan`
//! (the "Scan filings" button) — ports of `routers/overlap.py`'s handlers with
//! `overlap.correct_tag/clear_correction/laya_answers` and `overlap_scan.start_scan`.
//!
//! A correction is a label on display context; a filings scan publishes the worker's
//! `baskfy.overlap.catalyst_scan`, which fills `sw_catalyst`. Nothing reaches a rank, a size or
//! an order (law 2). `PUT`/`DELETE /overlap/reviews` are **not** ported: they re-read the row
//! through `GET /overlap`'s view, which is not ported yet (DECISIONS-RS R5.21), so Python keeps
//! serving them.

use axum::extract::{Extension, RawQuery, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_db::OrderedJson;
use baskfy_providers::ratelimit::ScriptValue;
use sqlx::Row;

use super::tags::{EVENT_TYPES, cache_key, resolve_tag, tag_from_laya, tag_headline, tag_out};
use crate::auth::{Principal, require_authenticated};
use crate::body::receive;
use crate::problems::{Problem, ProblemType, bad_request, internal_error, not_found};
use crate::query::{QueryParams, Validation};
use crate::routes::options::derivatives::shared::{scoped_sole_user_id, validate_literal};
use crate::routes::sleeves::common::{canonical_response, db, field_error, py_isoformat};
use crate::routes::sleeves::pyd::{Default as D, Model, PV, PyDec, Ty, py_strip, validate_body};
use crate::routes::sleeves::writes::{Bind, Changes, now_micros, pool, read_body, send_task};
use crate::session::{self, WritePool};
use crate::state::AppState;

/// `CORRECTIONS`: the sole-tenant surface name these writes are refused under.
const CORRECTIONS: &str = "corrections";
/// `overlap.WANTED_KEY`.
const WANTED_KEY: &str = "catalyst_tag:wanted";
/// `overlap.WANTED_TTL_S`.
const WANTED_TTL_S: i64 = 3 * 24 * 3600;
/// `overlap_scan.SCAN_TASK`.
pub const SCAN_TASK: &str = "baskfy.overlap.catalyst_scan";
/// `overlap_scan.STALE_AFTER` in seconds.
const STALE_AFTER_SECONDS: i64 = 20 * 60;
/// `overlap_scan.STATUS_TTL_SECONDS`.
const STATUS_TTL_SECONDS: i64 = 7 * 24 * 3600;

/// `laya_answers(cache, [headline])` for one headline: the sidecar's cached answer, or `None` on
/// a miss (which notes the headline in the wanted set, three days' TTL) or a Redis failure.
const LAYA_SCRIPT: &str = "local v = redis.call('GET', KEYS[1]) \
    if v then return v end \
    redis.call('SADD', KEYS[2], ARGV[1]) \
    redis.call('EXPIRE', KEYS[2], ARGV[2]) \
    return false";

async fn laya_answer(state: &AppState, headline: &str) -> Option<OrderedJson> {
    let url = &state.settings.redis_url;
    if url.expose().is_empty() {
        return None;
    }
    match crate::cache::eval(
        url,
        LAYA_SCRIPT,
        &[cache_key(headline), WANTED_KEY.to_owned()],
        &[headline.to_owned(), WANTED_TTL_S.to_string()],
    )
    .await
    {
        Ok(ScriptValue::Bytes(b)) => String::from_utf8(b)
            .ok()
            .and_then(|t| OrderedJson::parse(&t).ok()),
        Ok(_) => None,
        Err(e) => {
            crate::logging::warning(
                "baskfy_api.overlap",
                &format!("could not read the sidecar's answers: {e}"),
                &[],
            );
            None
        }
    }
}

fn correction_in() -> Model {
    Model {
        fields: vec![
            ("headline", Ty::str_len(Some(1), Some(2000)), D::Required),
            ("event_type", Ty::Enum(&EVENT_TYPES), D::Required),
            ("note", Ty::str_len(None, Some(2000)).opt(), D::Unset),
        ],
        forbid_extra: false,
        before: None,
        after: None,
    }
}

/// `_confidence(tag)`: four decimals, as the column stores it.
fn stored_confidence(c: Option<f64>) -> Option<rust_decimal::Decimal> {
    c.and_then(|x| PyDec::parse(&format!("{x:.4}")))
        .and_then(|d| d.to_decimal())
}

/// `PUT /overlap/tags`: store a person's word on a headline; answer with the tag now shown.
///
/// # Errors
/// 400 (body; a blank headline), 401, 404/503 (tenant), 500.
pub async fn put_tag(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let payload =
        validate_body(&received, &correction_in()).map_err(|f| f.into_problem(Vec::new()))?;
    let user_id = principal.require_user()?;
    let user_id = scoped_sole_user_id(&state.db, Some(user_id), Some(CORRECTIONS)).await?;
    let text = |name: &str| match payload.field(name) {
        Some(PV::Str(s)) => Some(s.clone()),
        _ => None,
    };
    let headline = py_strip(&text("headline").unwrap_or_default()).to_owned();
    if headline.is_empty() {
        return Err(bad_request(
            "A correction needs a headline to correct.",
            Some(vec![field_error("headline", "blank")]),
        ));
    }
    let event_type: &'static str = text("event_type")
        .and_then(|e| EVENT_TYPES.iter().copied().find(|x| *x == e))
        .ok_or_else(internal_error)?;
    let note = text("note")
        .map(|n| py_strip(&n).to_owned())
        .filter(|n| !n.is_empty());
    let answer = laya_answer(&state, &headline).await;
    let rules = tag_headline(&headline);
    let laya = tag_from_laya(answer.as_ref());
    let key = cache_key(&headline);
    let laya_event = laya.as_ref().map(|l| l.event_type.to_owned());
    let laya_conf = stored_confidence(laya.as_ref().and_then(|l| l.confidence));
    let rules_event = rules.event_type.to_owned();
    let pool = pool(write)?;
    let h2 = headline.clone();
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let row = sqlx::query(
                "SELECT id, headline, event_type, note, rules_event_type, laya_event_type, \
                 laya_confidence FROM catalyst_tag_correction \
                 WHERE user_id = $1 AND headline_key = $2",
            )
            .bind(user_id)
            .bind(&key)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            match row {
                None => {
                    sqlx::query(
                        "INSERT INTO catalyst_tag_correction (user_id, headline_key, headline, \
                         event_type, note, rules_event_type, laya_event_type, laya_confidence) \
                         VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    )
                    .bind(user_id)
                    .bind(&key)
                    .bind(&h2)
                    .bind(event_type)
                    .bind(&note)
                    .bind(&rules_event)
                    .bind(&laya_event)
                    .bind(laya_conf)
                    .execute(&mut **tx)
                    .await
                    .map_err(db)?;
                }
                Some(row) => {
                    let id: i64 = row.try_get("id").map_err(db)?;
                    let get = |c: &str| -> Result<Option<String>, Problem> {
                        row.try_get::<Option<String>, _>(c).map_err(db)
                    };
                    let mut c = Changes::default();
                    if get("headline")?.as_deref() != Some(h2.as_str()) {
                        c.set("headline", Bind::Str(h2.clone()));
                    }
                    if get("event_type")?.as_deref() != Some(event_type) {
                        c.set("event_type", Bind::Str(event_type.to_owned()));
                    }
                    if get("note")? != note {
                        c.set("note", Bind::OptStr(note.clone()));
                    }
                    if get("rules_event_type")?.as_deref() != Some(rules_event.as_str()) {
                        c.set("rules_event_type", Bind::Str(rules_event.clone()));
                    }
                    if get("laya_event_type")? != laya_event {
                        c.set("laya_event_type", Bind::OptStr(laya_event.clone()));
                    }
                    let stored: Option<rust_decimal::Decimal> =
                        crate::routes::sleeves::common::odec(&row, "laya_confidence")?;
                    if stored != laya_conf {
                        c.set("laya_confidence", Bind::OptDec(laya_conf));
                    }
                    c.apply(tx, "catalyst_tag_correction", "id = ?", &[id])
                        .await?;
                }
            }
            Ok(())
        })
    })
    .await?;
    Ok(canonical_response(&tag_out(&resolve_tag(
        &rules,
        laya.as_ref(),
        Some(event_type),
    ))))
}

/// `DELETE /overlap/tags?headline=`: the readers' word shows again (404 when there was none).
///
/// # Errors
/// 400 (the query), 401, 404, 503, 500.
pub async fn delete_tag(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let headline = v.required(&q, "headline").map(str::to_owned);
    if let Some(h) = &headline
        && h.chars().count() < 1
    {
        v.error(
            "headline",
            "String should have at least 1 character",
            "string_too_short",
        );
    }
    v.finish()?;
    let headline = headline.ok_or_else(internal_error)?;
    let user_id = principal.require_user()?;
    let user_id = scoped_sole_user_id(&state.db, Some(user_id), Some(CORRECTIONS)).await?;
    let key = cache_key(&headline);
    let pool = pool(write)?;
    let removed = session::run(&pool, move |tx| {
        Box::pin(async move {
            let done = sqlx::query(
                "DELETE FROM catalyst_tag_correction WHERE id = (SELECT id FROM \
                 catalyst_tag_correction WHERE user_id = $1 AND headline_key = $2)",
            )
            .bind(user_id)
            .bind(&key)
            .execute(&mut **tx)
            .await
            .map_err(db)?;
            Ok(done.rows_affected() > 0)
        })
    })
    .await?;
    if !removed {
        return Err(not_found("correction", &headline));
    }
    Ok(StatusCode::NO_CONTENT.into_response())
}

/// `SET key value NX EX ttl`: `1` when set.
const SET_NX_SCRIPT: &str =
    "if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then return 1 end return 0";
/// `SET key value EX ttl`.
const SET_EX_SCRIPT: &str = "redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2]) return 1";

/// `in_busy_window(now)`: 09:10-09:30 IST on a weekday.
fn in_busy_window(now: chrono::DateTime<chrono::Utc>) -> bool {
    use chrono::{Datelike, NaiveTime};
    let local = now.with_timezone(&crate::routes::common::ist());
    let t = local.time();
    local.weekday().num_days_from_monday() < 5
        && NaiveTime::from_hms_opt(9, 10, 0).is_some_and(|from| t >= from)
        && NaiveTime::from_hms_opt(9, 30, 0).is_some_and(|until| t < until)
}

/// `POST /overlap/catalyst-scan?scope=`: queue one filings scan of the names the page lists.
///
/// # Errors
/// 400 (scope), 401, 404/503 (tenant), 409 (running, or the morning window), 500.
pub async fn post_catalyst_scan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let scope = validate_literal(&mut v, "scope", q.get("scope"), &["actionable", "all"]);
    v.finish()?;
    let scope = scope.unwrap_or("actionable");
    let user_id = principal.require_user()?;
    let user_id = scoped_sole_user_id(&state.db, Some(user_id), Some(CORRECTIONS)).await?;
    let url = &state.settings.redis_url;
    if url.expose().is_empty() {
        return Err(Problem::new(
            ProblemType::InternalError,
            "No task broker or cache is configured, so filings cannot be scanned from here.",
        ));
    }
    let now = now_micros();
    if in_busy_window(now) {
        return Err(Problem::new(
            ProblemType::ScanInFlight,
            "09:10-09:30 IST belongs to the swing monitor and the morning catalyst feed; \
             scan filings after 09:30.",
        ));
    }
    let queued_at = py_isoformat(&now.fixed_offset());
    let redis_failure = |e: String| {
        crate::logging::error("baskfy_api.app", "unhandled error", &[("error", &e)]);
        internal_error()
    };
    let locked = crate::cache::eval(
        url,
        SET_NX_SCRIPT,
        &[format!("overlap:catalyst-scan:{user_id}:lock")],
        &[queued_at.clone(), STALE_AFTER_SECONDS.to_string()],
    )
    .await
    .map_err(redis_failure)?;
    if !matches!(locked, ScriptValue::Int(1)) {
        return Err(Problem::new(
            ProblemType::ScanInFlight,
            "A filings scan is already running; its progress is shown on the page.",
        ));
    }
    let status = OrderedJson::Object(vec![
        ("state".to_owned(), OrderedJson::Str("queued".to_owned())),
        ("scope".to_owned(), OrderedJson::Str(scope.to_owned())),
        ("queued_at".to_owned(), OrderedJson::Str(queued_at)),
        ("total".to_owned(), OrderedJson::Null),
        ("done".to_owned(), OrderedJson::Int(0)),
    ]);
    let text = serde_json::to_string(&status).map_err(|e| redis_failure(e.to_string()))?;
    crate::cache::eval(
        url,
        SET_EX_SCRIPT,
        &[super::status_key(user_id)],
        &[text, STATUS_TTL_SECONDS.to_string()],
    )
    .await
    .map_err(redis_failure)?;
    send_task(
        url,
        SCAN_TASK,
        &[
            OrderedJson::Int(user_id),
            OrderedJson::Str(scope.to_owned()),
        ],
    )
    .await
    .map_err(redis_failure)?;
    let body = super::scan_out(&status)?;
    let mut response = (StatusCode::ACCEPTED, body).into_response();
    response.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        axum::http::HeaderValue::from_static(crate::wire::JSON),
    );
    Ok(response)
}
