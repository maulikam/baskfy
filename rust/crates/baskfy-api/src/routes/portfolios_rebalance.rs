//! `POST /portfolios/{portfolio_id}/rebalance`: the rank-buffer rule against a screen, persisted
//! as `portfolio_rebalance` (Prompt 14). No order path (law 2).

use axum::extract::{Extension, Path, Request, State};
use axum::response::Response;
use baskfy_core::rank_buffer::{HeldName, RebalancePlan, RebalanceRow, ScreenRank, plan_rebalance};
use baskfy_core::screener::{MAX_RESULT_ROWS, Payload};
use baskfy_db::OrderedJson;

use super::portfolio::{self, db};
use super::{load, path_int};
use crate::auth::{Principal, require_authenticated};
use crate::entitlements::entitlements_for;
use crate::problems::{Problem, internal_error, not_found};
use crate::query::Validation;
use crate::routes::screens::body_model::{self, Errors, Model, Ty, opt, req};
use crate::routes::screens::write_kit;
use crate::routes::screens::{stored_definition, visible_to};
use crate::screener::{self, NoCacheError};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

fn rebalance_in() -> Model {
    Model {
        fields: vec![
            req("screen_public_id", Ty::Str(None, None)),
            req(
                "top_n",
                Ty::Int {
                    gt: None,
                    ge: Some(1),
                    le: Some(1000),
                },
            ),
            req(
                "hold_buffer",
                Ty::Int {
                    gt: None,
                    ge: Some(0),
                    le: Some(1000),
                },
            ),
            opt("as_of", Ty::Opt(Box::new(Ty::Date))),
            opt(
                "data_version",
                Ty::Opt(Box::new(Ty::Int {
                    gt: None,
                    ge: None,
                    le: None,
                })),
            ),
        ],
        forbid_extra: true,
    }
}

fn name_out(r: &RebalanceRow) -> OrderedJson {
    OrderedJson::Object(vec![
        (
            "instrument_id".to_owned(),
            OrderedJson::Int(r.instrument_id),
        ),
        ("symbol".to_owned(), OrderedJson::Str(r.symbol.clone())),
        ("name".to_owned(), OrderedJson::Str(r.name.clone())),
        (
            "rank".to_owned(),
            r.rank.map_or(OrderedJson::Null, OrderedJson::Int),
        ),
        (
            "reason".to_owned(),
            r.reason.map_or(OrderedJson::Null, |x| {
                OrderedJson::Str(x.value().to_owned())
            }),
        ),
    ])
}

fn names(rows: &[RebalanceRow]) -> OrderedJson {
    OrderedJson::Array(rows.iter().map(name_out).collect())
}

fn payload_of(
    plan: &RebalancePlan,
    resolution: &screener::AsOfResolution,
    data_version: i64,
    screen: (&str, &str),
    counts: (i64, i64, i64),
) -> OrderedJson {
    let weights = plan
        .target_weights
        .iter()
        .map(|w| {
            OrderedJson::Object(vec![
                (
                    "instrument_id".to_owned(),
                    OrderedJson::Int(w.instrument_id),
                ),
                ("symbol".to_owned(), OrderedJson::Str(w.symbol.clone())),
                ("name".to_owned(), OrderedJson::Str(w.name.clone())),
                ("rank".to_owned(), OrderedJson::Int(w.rank)),
                (
                    "weight".to_owned(),
                    OrderedJson::Str(wire::py_decimal(w.weight)),
                ),
                (
                    "action".to_owned(),
                    OrderedJson::Str(w.action.value().to_owned()),
                ),
            ])
        })
        .collect();
    OrderedJson::Object(vec![
        (
            "as_of".to_owned(),
            OrderedJson::Str(wire::py_date(resolution.as_of)),
        ),
        (
            "requested_as_of".to_owned(),
            resolution
                .requested
                .map_or(OrderedJson::Null, |d| OrderedJson::Str(wire::py_date(d))),
        ),
        ("data_version".to_owned(), OrderedJson::Int(data_version)),
        (
            "screen".to_owned(),
            OrderedJson::Object(vec![
                (
                    "public_id".to_owned(),
                    OrderedJson::Str(screen.0.to_owned()),
                ),
                ("name".to_owned(), OrderedJson::Str(screen.1.to_owned())),
            ]),
        ),
        ("top_n".to_owned(), OrderedJson::Int(plan.top_n)),
        ("hold_buffer".to_owned(), OrderedJson::Int(plan.hold_buffer)),
        ("exits".to_owned(), names(&plan.exits)),
        ("inside_wrh".to_owned(), names(&plan.inside_wrh)),
        ("entries".to_owned(), names(&plan.entries)),
        ("holds".to_owned(), names(&plan.holds)),
        ("target_weights".to_owned(), OrderedJson::Array(weights)),
        ("holdings_count".to_owned(), OrderedJson::Int(counts.0)),
        ("screen_result_count".to_owned(), OrderedJson::Int(counts.1)),
        ("delisted_count".to_owned(), OrderedJson::Int(counts.2)),
    ])
}

fn text_of(p: Option<&Payload>, fallback: &str) -> String {
    match p {
        Some(Payload::Str(s)) => s.clone(),
        Some(Payload::Null) => "None".to_owned(),
        None => fallback.to_owned(),
        Some(_) => fallback.to_owned(),
    }
}

/// `POST /portfolios/{portfolio_id}/rebalance` (`rebalance`).
///
/// # Errors
/// 400, 401, 402, 404, 409, 422, 503, 500.
pub async fn rebalance(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_portfolio): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let mut v = Validation::new();
    let portfolio_id = path_int(&mut v, "portfolio_id", &raw_portfolio);
    v.finish()?;
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &rebalance_in(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let user_id = principal.require_user()?;
    let screen_public_id = body
        .get("screen_public_id")
        .str()
        .unwrap_or_default()
        .to_owned();
    let top_n = body
        .get("top_n")
        .int()
        .and_then(|i| i64::try_from(i).ok())
        .unwrap_or(1);
    let hold_buffer = body
        .get("hold_buffer")
        .int()
        .and_then(|i| i64::try_from(i).ok())
        .unwrap_or(0);
    let as_of_in = body.get("as_of").date();
    let sent = body.get("data_version").int();
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let portfolio = load(tx, portfolio_id.unwrap_or_default(), user_id).await?;
            let screen = sqlx::query(
                "SELECT id, public_id, name, user_id, definition FROM screen WHERE public_id = $1",
            )
            .bind(&screen_public_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db::fail)?;
            let screen = match screen {
                Some(r) => r,
                None => return Err(not_found("screen", &screen_public_id)),
            };
            let owner: Option<i64> = db::get(&screen, "user_id")?;
            if !visible_to(owner, &principal) {
                return Err(not_found("screen", &screen_public_id));
            }
            let screen_id: i64 = db::get(&screen, "id")?;
            let public_id: String = db::get(&screen, "public_id")?;
            let screen_name: String = db::get(&screen, "name")?;
            let current = screener::current_data_version(tx).await.map_err(db::fail)?;
            if let Some(s) = sent
                && s != i128::from(current)
            {
                return Err(crate::problems::stale_data_version(
                    i64::try_from(s).unwrap_or(i64::MAX),
                    current,
                ));
            }
            let definition = stored_definition(&db::json(&screen, "definition")?)?;
            crate::routes::screens::require_universe(&entitlements, &definition.index)?;
            let requested = as_of_in.or(definition.historical_date);
            let resolution = screener::resolve_as_of::<NoCacheError>(tx, requested)
                .await
                .map_err(|e| screener::problem(&e))?;
            let data_version = screener::current_data_version(tx).await.map_err(db::fail)?;
            let result = screener::execute_screen::<NoCacheError>(
                tx,
                &definition,
                resolution.as_of,
                data_version,
                &[],
                requested,
                MAX_RESULT_ROWS,
            )
            .await
            .map_err(|e| screener::problem(&e))?;
            let mut ranked = Vec::with_capacity(result.rows.len());
            for row in &result.rows {
                let symbol = row.symbol().map_err(|_| internal_error())?;
                let name = text_of(
                    row.values.iter().find(|(k, _)| k == "name").map(|(_, v)| v),
                    &symbol,
                );
                ranked.push(ScreenRank {
                    instrument_id: row.instrument_id,
                    symbol,
                    name,
                    rank: row.rank,
                });
            }
            let rows = sqlx::query(
                "SELECT portfolio_holding.instrument_id, instrument.symbol, instrument.name, \
                 instrument.delisted_on IS NOT NULL AS delisted FROM portfolio_holding JOIN \
                 instrument ON instrument.id = portfolio_holding.instrument_id WHERE \
                 portfolio_holding.portfolio_id = $1 ORDER BY instrument.symbol",
            )
            .bind(portfolio.id)
            .fetch_all(&mut **tx)
            .await
            .map_err(db::fail)?;
            let mut held = Vec::with_capacity(rows.len());
            for r in &rows {
                held.push(HeldName {
                    instrument_id: db::get(r, "instrument_id")?,
                    symbol: db::get(r, "symbol")?,
                    name: db::get(r, "name")?,
                    quantity: None,
                    delisted: db::get(r, "delisted")?,
                });
            }
            let plan = plan_rebalance(&ranked, &held, top_n, hold_buffer).map_err(|m| {
                crate::logging::error("baskfy_api.app", "unhandled error", &[("error", &m)]);
                internal_error()
            })?;
            if resolution.requested.is_some() && resolution.as_of != resolution.latest_published {
                crate::routes::screens::require_feature(&entitlements, "historical_ranks")?;
            }
            let delisted = i64::try_from(held.iter().filter(|h| h.delisted).count()).unwrap_or(0);
            let payload = payload_of(
                &plan,
                &resolution,
                data_version,
                (&public_id, &screen_name),
                (
                    i64::try_from(held.len()).unwrap_or(0),
                    i64::try_from(result.result_count()).unwrap_or(0),
                    delisted,
                ),
            );
            let stored = payload.to_python_json().map_err(|_| internal_error())?;
            let row = sqlx::query(
                "INSERT INTO portfolio_rebalance (portfolio_id, screen_id, as_of, data_version, \
                 top_n, hold_buffer, payload) VALUES ($1, $2, $3, $4, $5, $6, ($7::text)::jsonb) \
                 RETURNING id, created_at",
            )
            .bind(portfolio.id)
            .bind(screen_id)
            .bind(resolution.as_of)
            .bind(data_version)
            .bind(i32::try_from(top_n).map_err(|_| internal_error())?)
            .bind(i32::try_from(hold_buffer).map_err(|_| internal_error())?)
            .bind(stored)
            .fetch_one(&mut **tx)
            .await
            .map_err(db::fail)?;
            portfolio::rebalance::rebalance_out(
                &payload,
                db::get(&row, "id")?,
                db::ts(&row, "created_at")?,
            )
            .map_err(|m| {
                crate::logging::error("baskfy_api.app", "unhandled error", &[("error", &m)]);
                internal_error()
            })
        })
    })
    .await?;
    Ok(wire::json(&out))
}
