//! `GET /swing/candidates?run=latest&mode=index_led` — the index-led regime's latest run and
//! its scanner candidates (`swing_regime_run` / `swing_candidates`, 0061). Read only.

use axum::extract::{Extension, RawQuery, State};
use axum::response::Response;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, bad_request};
use crate::query::QueryParams;
use crate::routes::sleeves::common::{
    J, Obj, canonical_response, db, dec, field_error, get as col, odec, ojson,
};
use crate::state::AppState;

fn empty(mode: &str) -> Response {
    canonical_response(
        &Obj::new()
            .put("run_at", J::Null)
            .put("mode", J::Str(mode.to_owned()))
            .put("leading", J::Arr(Vec::new()))
            .put("data", J::Arr(Vec::new()))
            .done(),
    )
}

/// `GET /swing/candidates`.
///
/// # Errors
/// 401, 404 (not the tenant), 400 (bad `run`/`mode`), 500.
pub async fn get_candidates(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    super::swing_tenant(&state, &principal).await?;
    let q = QueryParams::parse(raw.as_deref());
    let mode = q.get("mode").unwrap_or("index_led").to_owned();
    if !matches!(mode.as_str(), "index_led" | "legacy") {
        return Err(bad_request(
            "mode must be index_led or legacy.",
            Some(vec![field_error("mode", "choose index_led or legacy")]),
        ));
    }
    let run = q.get("run").unwrap_or("latest");
    let run_id: Option<i64> = if run == "latest" {
        None
    } else {
        Some(run.parse().map_err(|_| {
            bad_request(
                "run must be latest or a run id.",
                Some(vec![field_error("run", "latest or an integer")]),
            )
        })?)
    };
    let pool = state.db.pool();
    let (ready,): (bool,) = sqlx::query_as(
        "SELECT to_regclass('swing_regime_run') IS NOT NULL \
         AND to_regclass('swing_candidates') IS NOT NULL",
    )
    .fetch_one(pool)
    .await
    .map_err(db)?;
    if !ready {
        return Ok(empty(&mode));
    }
    let head = sqlx::query(
        "SELECT id, run_at, kind, as_of, gate_open, \"leading\" FROM swing_regime_run \
         WHERE mode = $1 AND ($2::bigint IS NULL OR id = $2) ORDER BY run_at DESC LIMIT 1",
    )
    .bind(&mode)
    .bind(run_id)
    .fetch_optional(pool)
    .await
    .map_err(db)?;
    let Some(head) = head else {
        return Ok(empty(&mode));
    };
    let id: i64 = col(&head, "id")?;
    let rows = sqlx::query(
        "SELECT symbol, scanner, setup, entry, stop, risk_pct, via_indices, universe_score, \
         score, rank::bigint AS rank FROM swing_candidates WHERE run_id = $1 ORDER BY rank",
    )
    .bind(id)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let mut data = Vec::with_capacity(rows.len());
    for r in &rows {
        data.push(
            Obj::new()
                .put("symbol", J::Str(col(r, "symbol")?))
                .put("scanner", J::Str(col(r, "scanner")?))
                .put("setup", J::Str(col(r, "setup")?))
                .put("entry", J::opt_dec(odec(r, "entry")?))
                .put("stop", J::opt_dec(odec(r, "stop")?))
                .put("risk_pct", J::opt_dec(odec(r, "risk_pct")?))
                .put(
                    "via_indices",
                    ojson(r, "via_indices")?.map_or(J::Arr(Vec::new()), J::Raw),
                )
                .put("universe_score", J::Dec(dec(r, "universe_score")?))
                .put("score", J::Dec(dec(r, "score")?))
                .put("rank", J::Int(col(r, "rank")?))
                .done(),
        );
    }
    Ok(canonical_response(
        &Obj::new()
            .put("run_id", J::Int(id))
            .put("run_at", J::Dt(col(&head, "run_at")?))
            .put("mode", J::Str(mode))
            .put("kind", J::Str(col(&head, "kind")?))
            .put("as_of", J::Date(col(&head, "as_of")?))
            .put("gate_open", J::Bool(col(&head, "gate_open")?))
            .put(
                "leading",
                ojson(&head, "leading")?.map_or(J::Arr(Vec::new()), J::Raw),
            )
            .put("data", J::Arr(data))
            .done(),
    ))
}
