//! ``/portfolios/{id}/sleeves`` and ``/allocation`` — a portfolio run as several sources (M34).
//!
//! Ports `baskfy_api.routers.sleeves` (its two GET routes). Python declares
//! `APIRouter(tags=["portfolios"])`.
//!
//! * `GET /portfolios/{portfolio_id}/sleeves` — ported.
//! * `PUT /portfolios/{portfolio_id}/sleeves` — a write, not in this wave (left unregistered).
//! * `GET /portfolios/{portfolio_id}/allocation` — ported for manual sleeves, a screen sleeve
//!   whose screen is gone, and a basket sleeve with no live version. A sleeve that would have to
//!   **run a saved screen** (`baskfy_api.screener.run_screen`) or **size a basket's live
//!   version** (`baskfy_core.portfolio_units.allocate_units`) is refused with a 503 naming what
//!   is missing, rather than answered with numbers Python would not give (DECISIONS-RS, lane api-sleeves).
//!
//! Amounts, target weights and whole unit counts — and nothing that could be handed to a broker.
//! **No route may place, modify or cancel an order** (law 2).
//!
//! This file also declares the lane's shared private modules (`sleeves/common.rs`), which the
//! swing, TWT, VBT, sleeve-state and baskets routers use.

pub(crate) mod common;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use rust_decimal::Decimal;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, not_found};
use crate::query::{QueryParams, Validation};
use crate::state::AppState;
use common::{
    J, LaxInt, Obj, db, dec, get as col, id_or_500, int_param, lax_bool, py_str_json,
    pydantic_response, quantise_half_up,
};

/// `TIER_LABELS`.
fn tier_label(tier: &str) -> String {
    match tier {
        "R1" => "Risk-on".to_owned(),
        "R2" => "Cautious".to_owned(),
        "R3" => "Defensive".to_owned(),
        "R4" => "Risk-off".to_owned(),
        other => other.to_owned(),
    }
}

/// `baskfy_core.sleeves.cap_for_tier(tier)` with the default table.
fn cap_for_tier(tier: &str) -> Option<Decimal> {
    match tier.to_uppercase().as_str() {
        "R1" => Some(Decimal::from(100)),
        "R2" => Some(Decimal::from(70)),
        "R3" => Some(Decimal::from(40)),
        "R4" => Some(Decimal::from(0)),
        _ => None,
    }
}

/// `_money(value)`: house rule 8's two places, half-up.
fn money(value: Decimal) -> Decimal {
    quantise_half_up(value, 2)
}

/// One `portfolio_sleeve` row as `SleeveOut` reads it.
#[derive(Debug, Clone)]
struct SleeveRow {
    id: i64,
    name: String,
    kind: String,
    capital: Decimal,
    screen_public_id: Option<String>,
    screen_name: Option<String>,
    basket_slug: Option<String>,
    basket_name: Option<String>,
    top_n: i64,
}

impl SleeveRow {
    fn to_j(&self) -> J {
        Obj::new()
            .put("id", J::Int(self.id))
            .put("name", J::s(&self.name))
            .put("kind", J::s(&self.kind))
            .put("capital", J::Dec(self.capital))
            .put(
                "screen_public_id",
                J::opt_str(self.screen_public_id.clone()),
            )
            .put("screen_name", J::opt_str(self.screen_name.clone()))
            .put("basket_slug", J::opt_str(self.basket_slug.clone()))
            .put("basket_name", J::opt_str(self.basket_name.clone()))
            .put("top_n", J::Int(self.top_n))
            .done()
    }
}

/// `_portfolio(session, portfolio_id, user_id)`: the ownership check (404 otherwise).
async fn owned_portfolio(state: &AppState, portfolio_id: i64, user_id: i64) -> Result<(), Problem> {
    let found: Option<i64> =
        sqlx::query_scalar("SELECT id FROM portfolio WHERE id = $1 AND user_id = $2")
            .bind(portfolio_id)
            .bind(user_id)
            .fetch_optional(state.db.pool())
            .await
            .map_err(db)?;
    found
        .map(|_| ())
        .ok_or_else(|| not_found("portfolio", &portfolio_id.to_string()))
}

async fn list_rows(state: &AppState, portfolio_id: i64) -> Result<Vec<SleeveRow>, Problem> {
    let rows = sqlx::query(
        "SELECT s.id, s.name, s.kind, s.capital, s.top_n::bigint AS top_n, \
                sc.public_id AS screen_public_id, sc.name AS screen_name, \
                b.slug AS basket_slug, b.name AS basket_name \
         FROM portfolio_sleeve s \
         LEFT JOIN screen sc ON sc.id = s.screen_id \
         LEFT JOIN cb_basket b ON b.id = s.basket_id \
         WHERE s.portfolio_id = $1 ORDER BY s.sort_order, s.id",
    )
    .bind(portfolio_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        out.push(SleeveRow {
            id: col(row, "id")?,
            name: col(row, "name")?,
            kind: col(row, "kind")?,
            capital: money(dec(row, "capital")?),
            screen_public_id: col(row, "screen_public_id")?,
            screen_name: col(row, "screen_name")?,
            basket_slug: col(row, "basket_slug")?,
            basket_name: col(row, "basket_name")?,
            top_n: col(row, "top_n")?,
        });
    }
    Ok(out)
}

fn portfolio_id_param(raw: &str, v: &mut Validation) -> Option<LaxInt> {
    int_param(v, "portfolio_id", Some(raw))
}

/// `GET /portfolios/{portfolio_id}/sleeves`.
///
/// # Errors
/// 401 anonymous; 400 for a non-integer id; 404 for a portfolio the caller does not own; 500 on
/// a database failure.
pub async fn list_sleeves(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(portfolio_id): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let mut v = Validation::new();
    let id = portfolio_id_param(&portfolio_id, &mut v);
    v.finish()?;
    let user_id = principal.require_user()?;
    let id = id_or_500(id.unwrap_or(LaxInt::Invalid))?;
    owned_portfolio(&state, id, user_id).await?;
    let rows = list_rows(&state, id).await?;
    let total = money(rows.iter().map(|r| r.capital).sum());
    Ok(pydantic_response(
        &Obj::new()
            .put(
                "sleeves",
                J::Arr(rows.iter().map(SleeveRow::to_j).collect()),
            )
            .put("total_capital", J::Dec(total))
            .done(),
    ))
}

/// `_stance(session)`: the desk's newest policy tier, or `None` with no desk table or no row.
async fn stance(state: &AppState) -> Result<Option<(J, Option<Decimal>)>, Problem> {
    let installed: Option<String> =
        sqlx::query_scalar("SELECT to_regclass('desk.regime_evaluations')::text")
            .fetch_one(state.db.pool())
            .await
            .map_err(db)?;
    if installed.is_none() {
        return Ok(None);
    }
    let row = sqlx::query(
        "SELECT policy_tier::text AS policy_tier, created_at::text AS created_at, \
                reasons_json::text AS reasons_json \
         FROM \"desk\".regime_evaluations ORDER BY id DESC LIMIT 1",
    )
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let Some(row) = row else {
        return Ok(None);
    };
    let tier = col::<Option<String>>(&row, "policy_tier")?
        .unwrap_or_default()
        .to_uppercase();
    let created: Option<String> = col(&row, "created_at")?;
    let raw: Option<String> = col(&row, "reasons_json")?;
    let parsed = match raw {
        Some(text) => Some(OrderedJson::parse(&text).map_err(|e| {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", &e.to_string())],
            );
            crate::problems::internal_error()
        })?),
        None => None,
    };
    let reasons: Vec<J> = match parsed {
        Some(OrderedJson::Array(items)) => items.iter().map(|r| J::Str(py_str_json(r))).collect(),
        _ => Vec::new(),
    };
    let cap = cap_for_tier(&tier);
    let out = Obj::new()
        .put("tier", J::s(&tier))
        .put("label", J::Str(tier_label(&tier)))
        .put("equity_cap_pct", J::opt_dec(cap))
        .put(
            "evaluated_at",
            J::opt_str(created.filter(|c| !c.is_empty())),
        )
        .put("reasons", J::Arr(reasons))
        .done();
    Ok(Some((out, cap)))
}

/// `latest_published_date(session)`.
async fn latest_published_date(state: &AppState) -> Result<Option<NaiveDate>, Problem> {
    sqlx::query_scalar("SELECT max(trade_date) FROM pipeline_run WHERE data_version IS NOT NULL")
        .fetch_one(state.db.pool())
        .await
        .map_err(db)
}

/// Whether a basket's live version has any constituent (`_basket_weights` non-empty).
async fn basket_has_weights(state: &AppState, slug: &str) -> Result<bool, Problem> {
    let found: Option<i64> = sqlx::query_scalar(
        "SELECT c.version_id FROM cb_constituent c WHERE c.version_id = ( \
             SELECT v.id FROM cb_basket_version v JOIN cb_basket b ON b.id = v.basket_id \
             WHERE b.slug = $1 ORDER BY v.version_no DESC LIMIT 1) LIMIT 1",
    )
    .bind(slug)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    Ok(found.is_some())
}

fn not_served(what: &str) -> Problem {
    Problem::new(
        ProblemType::PipelineDegraded,
        format!(
            "This server does not size {what} yet; the allocation is served by the Python API for now."
        ),
    )
}

/// `GET /portfolios/{portfolio_id}/allocation?apply_regime_cap=`.
///
/// # Errors
/// 401 anonymous; 400 for a bad id or flag; 404 for a portfolio the caller does not own; 503
/// for a sleeve this server cannot size yet (see the module docs); 500 on a database failure.
pub async fn allocation(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(portfolio_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let id = portfolio_id_param(&portfolio_id, &mut v);
    let mut apply = false;
    if let Some(flag) = q.get("apply_regime_cap") {
        match lax_bool(flag) {
            Some(b) => apply = b,
            None => v.error(
                "apply_regime_cap",
                "Input should be a valid boolean, unable to interpret input",
                "bool_parsing",
            ),
        }
    }
    v.finish()?;
    let user_id = principal.require_user()?;
    let id = id_or_500(id.unwrap_or(LaxInt::Invalid))?;
    // `_portfolio` and then `list_sleeves`' own `_portfolio`: one ownership read answers both.
    owned_portfolio(&state, id, user_id).await?;
    let sleeves = list_rows(&state, id).await?;
    let stance = stance(&state).await?;
    let cap = if apply {
        stance.as_ref().and_then(|(_, c)| *c)
    } else {
        None
    };

    // `_screen_symbols` / `_basket_weights`, in sleeve order, before anything is priced.
    for sleeve in &sleeves {
        if sleeve.kind == "screen"
            && sleeve
                .screen_public_id
                .as_deref()
                .is_some_and(|s| !s.is_empty())
        {
            return Err(not_served("a sleeve that runs a saved screen"));
        }
        if sleeve.kind == "basket"
            && let Some(slug) = sleeve.basket_slug.as_deref().filter(|s| !s.is_empty())
            && basket_has_weights(&state, slug).await?
        {
            return Err(not_served("a sleeve drawn from a basket's live version"));
        }
    }
    let priced_as_of = latest_published_date(&state).await?;

    let mut out = Vec::with_capacity(sleeves.len());
    let mut capital = Decimal::ZERO;
    let mut deployed = Decimal::ZERO;
    for sleeve in &sleeves {
        let entry = if sleeve.kind == "basket" {
            Obj::new()
                .put("name", J::s(&sleeve.name))
                .put("kind", J::s(&sleeve.kind))
                .put("capital", J::Dec(money(sleeve.capital)))
                .put("deployed", J::Dec(money(Decimal::ZERO)))
                .put("cash", J::Dec(money(sleeve.capital)))
                .put("screen_name", J::Null)
                .put("basket_slug", J::opt_str(sleeve.basket_slug.clone()))
                .put("basket_name", J::opt_str(sleeve.basket_name.clone()))
                .put("rows", J::Arr(Vec::new()))
                .put("unpriced", J::Arr(Vec::new()))
                .put("units_note", J::Null)
                .put(
                    "source_note",
                    J::Str(format!(
                        "The basket {} has no published version with constituents, so this \
                         sleeve proposes no names and its capital is reported as cash.",
                        sleeve
                            .basket_slug
                            .as_deref()
                            .map_or_else(|| "None".to_owned(), baskfy_providers::py::repr_str)
                    )),
                )
                .done()
        } else {
            // A manual sleeve, or a screen sleeve whose screen is gone: no names, all cash.
            Obj::new()
                .put("name", J::s(&sleeve.name))
                .put("kind", J::s(&sleeve.kind))
                .put("capital", J::Dec(money(sleeve.capital)))
                .put("deployed", J::Dec(money(Decimal::ZERO)))
                .put("cash", J::Dec(money(sleeve.capital)))
                .put("screen_name", J::opt_str(sleeve.screen_name.clone()))
                .put("basket_slug", J::Null)
                .put("basket_name", J::Null)
                .put("rows", J::Arr(Vec::new()))
                .put("unpriced", J::Arr(Vec::new()))
                .put("units_note", J::Null)
                .put("source_note", J::Null)
                .done()
        };
        capital += money(sleeve.capital);
        deployed += Decimal::ZERO;
        out.push(entry);
    }
    let capital = money(capital);
    let deployed = money(deployed);
    let body = Obj::new()
        .put("capital", J::Dec(capital))
        .put("deployed", J::Dec(deployed))
        .put("cash", J::Dec(money(capital - deployed)))
        .put("equity_cap_pct", J::opt_dec(cap))
        .put("applied_regime_cap", J::Bool(cap.is_some()))
        .put("stance", stance.map_or(J::Null, |(s, _)| s))
        .put("sleeves", J::Arr(out))
        .put("priced_as_of", J::opt_date(priced_as_of))
        .put("unpriced", J::Arr(Vec::new()))
        .done();
    Ok(pydantic_response(&body))
}

/// The GET routes of `baskfy_api.routers.sleeves`, relative to `/api/v1`. The `PUT` stays on
/// the Python API until writes are ported.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/portfolios/{portfolio_id}/sleeves", get(list_sleeves))
        .route("/portfolios/{portfolio_id}/allocation", get(allocation))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn tiers_and_caps_are_the_desks_defaults() {
        assert_eq!(cap_for_tier("r2"), Some(Decimal::from(70)));
        assert_eq!(cap_for_tier("R4"), Some(Decimal::ZERO));
        assert_eq!(cap_for_tier("R9"), None);
        assert_eq!(tier_label("R3"), "Defensive");
        assert_eq!(tier_label("X"), "X");
    }

    #[test]
    fn money_is_two_places_half_up() {
        use std::str::FromStr;
        assert_eq!(money(Decimal::from(333)).to_string(), "333.00");
        assert_eq!(
            money(Decimal::from_str("0.125").unwrap()).to_string(),
            "0.13"
        );
    }
}
