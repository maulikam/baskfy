//! `/mb/*` — the MB sleeve's read surfaces (PM7). GET only; the sleeve has no write, no plan and no
//! order path (Track C, `docs/portfolio-ml/README.md`). Same gate as the other sleeves: signed in
//! and the deployment's sole tenant. The date is the last scored session, never "live".

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use sqlx::postgres::PgRow;

use crate::auth::{Principal, require_authenticated};
use crate::problems::Problem;
use crate::query::{QueryParams, Validation};
use crate::routes::sleeves::common::{
    J, LaxInt, Obj, canonical_response, db, dec, get as col, int_param, odec, scoped_sole_user_id,
};
use crate::state::AppState;

/// How many names `/mb/today` lists.
pub const TOP_N: i64 = 50;
/// The longest history `/mb/history/{id}` returns, in scored sessions.
pub const MAX_HISTORY: i64 = 750;

const MODEL_COLUMNS: &str =
    "id, trained_on, label, rows, positives, spearman_prev, published_at, published_by";

fn model_j(r: &PgRow) -> Result<J, Problem> {
    Ok(Obj::new()
        .put("id", J::Int(i64::from(col::<i32>(r, "id")?)))
        .put("trained_on", J::Date(col(r, "trained_on")?))
        .put("label", J::Str(col(r, "label")?))
        .put("rows", J::Int(i64::from(col::<i32>(r, "rows")?)))
        .put("positives", J::Int(i64::from(col::<i32>(r, "positives")?)))
        .put("spearman_prev", J::opt_dec(odec(r, "spearman_prev")?))
        .put("published_at", J::opt_dt(col(r, "published_at")?))
        .put("published_by", J::opt_str(col(r, "published_by")?))
        .done())
}

fn opt_i(r: &PgRow, c: &str) -> Result<J, Problem> {
    Ok(J::opt_int(col::<Option<i32>>(r, c)?.map(i64::from)))
}

/// `GET /mb/today?date=&all=` — the top 50 investable names of the newest scored session on or
/// before `date` (`all=true`: the top 50 of every scored name).
///
/// # Errors
/// 401 signed out, 404 not the sole tenant, 422 a bad date.
pub async fn get_today(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let day = v.date("date", q.get("date"));
    v.finish()?;
    let all = q.get("all").is_some_and(|s| s == "true");
    scoped_sole_user_id(&state, principal.user_id, None).await?;
    let pool = state.db.pool();
    let as_of: Option<chrono::NaiveDate> = sqlx::query_scalar(
        "SELECT max(date) FROM mb_score_daily WHERE ($1::date IS NULL OR date <= $1)",
    )
    .bind(day)
    .fetch_one(pool)
    .await
    .map_err(db)?;
    let Some(as_of) = as_of else {
        return Ok(canonical_response(
            &Obj::new()
                .put("as_of", J::Null)
                .put("model", J::Null)
                .put("universe_n", J::Null)
                .put("investable_n", J::Null)
                .put("names", J::Arr(Vec::new()))
                .done(),
        ));
    };
    let rows = sqlx::query(
        "SELECT s.rank, s.instrument_id, i.symbol, i.name, sec.sector, s.score, s.investable, \
         s.hi63_trigger, s.hi63_trigger_date, s.close_raw, s.turnover20, s.r252, s.dist_ath, \
         s.sessions_listed, s.ttm_rev_yoy, s.ttm_pat_yoy, s.model_id, s.universe_n \
         FROM mb_score_daily s JOIN instrument i ON i.id = s.instrument_id \
         LEFT JOIN instrument_sector sec ON sec.instrument_id = s.instrument_id \
         WHERE s.date = $1 AND ($2 OR s.investable) ORDER BY s.rank LIMIT $3",
    )
    .bind(as_of)
    .bind(all)
    .bind(TOP_N)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let mut names = Vec::with_capacity(rows.len());
    for r in &rows {
        names.push(
            Obj::new()
                .put("rank", J::Int(i64::from(col::<i32>(r, "rank")?)))
                .put("instrument_id", J::Int(col(r, "instrument_id")?))
                .put("symbol", J::Str(col(r, "symbol")?))
                .put("name", J::opt_str(col(r, "name")?))
                .put("sector", J::opt_str(col(r, "sector")?))
                .put("score", J::Dec(dec(r, "score")?))
                .put("investable", J::Bool(col(r, "investable")?))
                .put("hi63_trigger", J::Bool(col(r, "hi63_trigger")?))
                .put(
                    "hi63_trigger_date",
                    J::opt_date(col(r, "hi63_trigger_date")?),
                )
                .put("close", J::opt_dec(odec(r, "close_raw")?))
                .put("turnover20", J::opt_dec(odec(r, "turnover20")?))
                .put("r252_fraction", J::opt_dec(odec(r, "r252")?))
                .put("dist_ath_fraction", J::opt_dec(odec(r, "dist_ath")?))
                .put("sessions_listed", opt_i(r, "sessions_listed")?)
                .put("ttm_rev_yoy_fraction", J::opt_dec(odec(r, "ttm_rev_yoy")?))
                .put("ttm_pat_yoy_fraction", J::opt_dec(odec(r, "ttm_pat_yoy")?))
                .done(),
        );
    }
    let counts: (i64, i64, Option<i32>) = sqlx::query_as(
        "SELECT count(*), count(*) FILTER (WHERE investable), max(model_id) \
         FROM mb_score_daily WHERE date = $1",
    )
    .bind(as_of)
    .fetch_one(pool)
    .await
    .map_err(db)?;
    let model = match counts.2 {
        Some(id) => {
            let r = sqlx::query(&format!(
                "SELECT {MODEL_COLUMNS} FROM mb_model WHERE id = $1"
            ))
            .bind(id)
            .fetch_one(pool)
            .await
            .map_err(db)?;
            model_j(&r)?
        }
        None => J::Null,
    };
    Ok(canonical_response(
        &Obj::new()
            .put("as_of", J::Date(as_of))
            .put("model", model)
            .put("universe_n", J::Int(counts.0))
            .put("investable_n", J::Int(counts.1))
            .put("names", J::Arr(names))
            .done(),
    ))
}

/// `GET /mb/history/{instrument_id}?days=` — one name's rank and score, newest first.
///
/// # Errors
/// 401, 404 not the sole tenant, 422 a bad `days`.
pub async fn get_history(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(instrument_id): Path<i64>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let days = match int_param(&mut v, "days", q.get("days")) {
        Some(LaxInt::Small(n)) => n.clamp(1, MAX_HISTORY),
        Some(_) => MAX_HISTORY,
        None => 250,
    };
    v.finish()?;
    scoped_sole_user_id(&state, principal.user_id, None).await?;
    let rows = sqlx::query(
        "SELECT date, rank, score, universe_n, investable, hi63_trigger FROM mb_score_daily \
         WHERE instrument_id = $1 ORDER BY date DESC LIMIT $2",
    )
    .bind(instrument_id)
    .bind(days)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut points = Vec::with_capacity(rows.len());
    for r in &rows {
        points.push(
            Obj::new()
                .put("date", J::Date(col(r, "date")?))
                .put("rank", J::Int(i64::from(col::<i32>(r, "rank")?)))
                .put("score", J::Dec(dec(r, "score")?))
                .put(
                    "universe_n",
                    J::Int(i64::from(col::<i32>(r, "universe_n")?)),
                )
                .put("investable", J::Bool(col(r, "investable")?))
                .put("hi63_trigger", J::Bool(col(r, "hi63_trigger")?))
                .done(),
        );
    }
    Ok(canonical_response(
        &Obj::new()
            .put("instrument_id", J::Int(instrument_id))
            .put("points", J::Arr(points))
            .done(),
    ))
}

/// `GET /mb/model` — the scoring model and every model trained.
///
/// # Errors
/// 401, 404 not the sole tenant.
pub async fn get_model(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    scoped_sole_user_id(&state, principal.user_id, None).await?;
    let rows = sqlx::query(&format!(
        "SELECT {MODEL_COLUMNS} FROM mb_model ORDER BY id DESC"
    ))
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut models = Vec::with_capacity(rows.len());
    let mut current = J::Null;
    for r in &rows {
        let m = model_j(r)?;
        let published: Option<chrono::DateTime<chrono::Utc>> = col(r, "published_at")?;
        if current == J::Null && published.is_some() {
            current = m.clone();
        }
        models.push(m);
    }
    Ok(canonical_response(
        &Obj::new()
            .put("current", current)
            .put("models", J::Arr(models))
            .done(),
    ))
}

/// The `/mb` routes: GET only.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/mb/today", get(get_today))
        .route("/mb/history/{instrument_id}", get(get_history))
        .route("/mb/model", get(get_model))
}
