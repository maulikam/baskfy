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
    J, LaxInt, Obj, canonical_response, db, dec, decimal_of_float, get as col, int_param, odec,
    quantize, scoped_sole_user_id,
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

/// One book's span, growth and worst fall, from its marks.
fn book_j(r: &PgRow) -> Result<J, Problem> {
    let start: chrono::NaiveDate = col(r, "start")?;
    let end: chrono::NaiveDate = col(r, "end")?;
    let first: f64 = col(r, "first")?;
    let last: f64 = col(r, "last")?;
    let years = (end - start).num_days() as f64 / 365.25;
    let cagr = if years > 0.5 && first > 0.0 {
        J::Dec(quantize(
            decimal_of_float((last / first).powf(1.0 / years) - 1.0),
            4,
        ))
    } else {
        J::Null
    };
    Ok(Obj::new()
        .put("source", J::Str(col(r, "source")?))
        .put("book", J::Str(col(r, "book")?))
        .put("start", J::Date(start))
        .put("end", J::Date(end))
        .put(
            "growth_fraction",
            J::Dec(quantize(decimal_of_float(last / first - 1.0), 4)),
        )
        .put("cagr_fraction", cagr)
        .put(
            "max_dd_fraction",
            J::Dec(quantize(decimal_of_float(col::<f64>(r, "max_dd")?), 4)),
        )
        .put(
            "invested_fraction",
            J::Dec(quantize(decimal_of_float(col::<f64>(r, "invested")?), 4)),
        )
        .done())
}

fn outcome_j(r: &PgRow) -> Result<J, Problem> {
    let f = |c: &str| -> Result<J, Problem> {
        Ok(col::<Option<f64>>(r, c)?.map_or(J::Null, |v| J::Dec(quantize(decimal_of_float(v), 4))))
    };
    Ok(Obj::new()
        .put("lines", J::Int(col(r, "lines")?))
        .put("touched_2x_fraction", f("touched")?)
        .put("halved_fraction", f("halved")?)
        .put("median_return_fraction", f("median")?)
        .done())
}

/// `GET /mb/paper` — the paper book (PM8): each book's marks summarised, a weekly equity series, the live
/// tranches, and realised outcomes against the research's expectation. Paper only; `backtest` is labelled so.
///
/// # Errors
/// 401, 404 not the sole tenant.
pub async fn get_paper(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    scoped_sole_user_id(&state, principal.user_id, None).await?;
    let pool = state.db.pool();
    let live_from: Option<String> =
        sqlx::query_scalar("SELECT value FROM mb_paper_state WHERE key = 'live_from_month'")
            .fetch_optional(pool)
            .await
            .map_err(db)?;
    let books = sqlx::query(
        "WITH m AS (SELECT source, book, date, equity::float8 AS equity, invested::float8 AS invested, \
           equity::float8 / max(equity::float8) OVER (PARTITION BY source, book ORDER BY date) - 1 AS dd \
           FROM mb_paper_mark) \
         SELECT source, book, min(date) AS start, max(date) AS end, \
           (array_agg(equity ORDER BY date))[1] AS first, (array_agg(equity ORDER BY date DESC))[1] AS last, \
           min(dd) AS max_dd, (array_agg(invested ORDER BY date DESC))[1] AS invested \
         FROM m GROUP BY source, book ORDER BY source DESC, book",
    )
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let series_rows = sqlx::query(
        "SELECT source, book, date, equity FROM (SELECT source, book, date, equity, \
           row_number() OVER (PARTITION BY source, book ORDER BY date) AS rn, \
           count(*) OVER (PARTITION BY source, book) AS n FROM mb_paper_mark) x \
         WHERE (rn - 1) % 5 = 0 OR rn = n ORDER BY source DESC, book, date",
    )
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let outcomes = sqlx::query(
        "SELECT t.source, l.line, l.closed, count(*) AS lines, \
           avg((l.max_mult >= 2)::int)::float8 AS touched, avg((l.mult <= 0.5)::int)::float8 AS halved, \
           percentile_cont(0.5) WITHIN GROUP (ORDER BY l.mult - 1)::float8 AS median \
         FROM mb_paper_line l JOIN mb_paper_tranche t ON t.id = l.tranche_id \
         WHERE l.entry_date IS NOT NULL GROUP BY t.source, l.line, l.closed \
         ORDER BY t.source DESC, l.line, l.closed DESC",
    )
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let tranches = sqlx::query(
        "SELECT t.signal_date, t.model_id, count(*) FILTER (WHERE l.line = 'hi63') AS hi63_lines, \
           count(*) FILTER (WHERE l.line = 'book') AS lines, \
           avg((l.max_mult >= 2)::int) FILTER (WHERE l.line = 'book')::float8 AS touched, \
           avg((l.mult <= 0.5)::int) FILTER (WHERE l.line = 'book')::float8 AS halved, \
           (percentile_cont(0.5) WITHIN GROUP (ORDER BY l.mult - 1) FILTER (WHERE l.line = 'book'))::float8 AS median, \
           max(l.sessions_held) FILTER (WHERE l.line = 'book') AS sessions_held \
         FROM mb_paper_tranche t LEFT JOIN mb_paper_line l ON l.tranche_id = t.id \
         WHERE t.source = 'live' GROUP BY t.id ORDER BY t.signal_date DESC",
    )
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let mut series: Vec<J> = Vec::new();
    let mut current: Option<(String, String, Vec<J>)> = None;
    for r in &series_rows {
        let key: (String, String) = (col(r, "source")?, col(r, "book")?);
        if current
            .as_ref()
            .is_none_or(|c| (c.0.as_str(), c.1.as_str()) != (key.0.as_str(), key.1.as_str()))
        {
            if let Some((src, bk, pts)) = current.take() {
                series.push(
                    Obj::new()
                        .put("source", J::Str(src))
                        .put("book", J::Str(bk))
                        .put("points", J::Arr(pts))
                        .done(),
                );
            }
            current = Some((key.0, key.1, Vec::new()));
        }
        if let Some(c) = current.as_mut() {
            c.2.push(J::Arr(vec![
                J::Date(col(r, "date")?),
                J::Dec(dec(r, "equity")?),
            ]));
        }
    }
    if let Some((src, bk, pts)) = current {
        series.push(
            Obj::new()
                .put("source", J::Str(src))
                .put("book", J::Str(bk))
                .put("points", J::Arr(pts))
                .done(),
        );
    }
    let mut out_rows = Vec::with_capacity(outcomes.len());
    for r in &outcomes {
        let mut o = outcome_j(r)?;
        if let J::Obj(fields) = &mut o {
            fields.insert(0, ("matured".to_owned(), J::Bool(col(r, "closed")?)));
            fields.insert(0, ("line".to_owned(), J::Str(col(r, "line")?)));
            fields.insert(0, ("source".to_owned(), J::Str(col(r, "source")?)));
        }
        out_rows.push(o);
    }
    let mut tranche_rows = Vec::with_capacity(tranches.len());
    for r in &tranches {
        let mut o = outcome_j(r)?;
        if let J::Obj(fields) = &mut o {
            fields.insert(0, ("hi63_lines".to_owned(), J::Int(col(r, "hi63_lines")?)));
            fields.insert(0, ("sessions_held".to_owned(), opt_i(r, "sessions_held")?));
            fields.insert(0, ("model_id".to_owned(), opt_i(r, "model_id")?));
            fields.insert(
                0,
                ("signal_date".to_owned(), J::Date(col(r, "signal_date")?)),
            );
        }
        tranche_rows.push(o);
    }
    let mut book_rows = Vec::with_capacity(books.len());
    for r in &books {
        book_rows.push(book_j(r)?);
    }
    Ok(canonical_response(
        &Obj::new()
            .put("live_from_month", J::opt_str(live_from))
            .put("books", J::Arr(book_rows))
            .put("series", J::Arr(series))
            .put("outcomes", J::Arr(out_rows))
            .put("tranches", J::Arr(tranche_rows))
            .done(),
    ))
}

/// The `/mb` routes: GET only.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/mb/today", get(get_today))
        .route("/mb/history/{instrument_id}", get(get_history))
        .route("/mb/model", get(get_model))
        .route("/mb/paper", get(get_paper))
}
