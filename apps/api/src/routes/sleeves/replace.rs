//! `PUT /portfolios/{portfolio_id}/sleeves` (M34) — replace how a portfolio is divided. Ports
//! `routers/sleeves.replace_sleeves` with `SleeveSetIn`'s validation, `_screens_by_public_id` and
//! `_baskets_by_slug` (lane writes-sleeves).
//!
//! The whole set is deleted and re-inserted in one transaction: a division is one decision. A
//! sleeve names a source (a screen, a basket, or neither for manual capital) and a rupee amount;
//! nothing here sizes an order or reaches a broker (law 2).

use axum::extract::{Extension, Path, Request, State};
use axum::response::Response;
use baskfy_db::OrderedJson;

use super::pyd::{Bound, Default as D, Model, PV, Ty, py_strip, validate_body};
use super::writes::{bindable, pool, read_body};
use crate::auth::{Principal, require_authenticated};
use crate::body::receive;
use crate::problems::{Problem, invalid_screen_definition, not_found};
use crate::query::Validation;
use crate::routes::sleeves::common::{LaxInt, db, id_or_500, int_param};
use crate::session::{self, WritePool};
use crate::state::AppState;

/// `MAX_SLEEVES`.
const MAX_SLEEVES: usize = 12;
/// `DEFAULT_TOP_N`.
const DEFAULT_TOP_N: i64 = 15;
/// `PUBLISHED`.
const PUBLISHED: &str = "PUBLISHED";

fn kind_matches(s: &str) -> bool {
    matches!(s, "screen" | "manual" | "basket")
}

fn field<'a>(fields: &'a [(String, Option<PV>)], name: &str) -> Option<&'a PV> {
    fields
        .iter()
        .find(|(n, _)| n == name)
        .and_then(|(_, v)| v.as_ref())
        .filter(|v| !matches!(v, PV::Null))
}

fn text(v: Option<&PV>) -> Option<String> {
    match v {
        Some(PV::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

/// `_check_source(spec)` over a validated `SleeveIn`.
fn check_source(spec: &[(String, Option<PV>)]) -> Result<(), String> {
    let name = text(field(spec, "name")).unwrap_or_default();
    let kind = text(field(spec, "kind")).unwrap_or_default();
    let screen = text(field(spec, "screen_public_id")).is_some_and(|s| !s.is_empty());
    let basket = text(field(spec, "basket_slug")).is_some_and(|s| !s.is_empty());
    let refuse = |why: &str| Err(format!("{name}: {why}"));
    match kind.as_str() {
        "screen" => {
            if !screen {
                return refuse("a screen sleeve must name a screen");
            }
            if basket {
                return refuse("a screen sleeve cannot name a basket");
            }
        }
        "basket" => {
            if !basket {
                return refuse("a basket sleeve must name a basket");
            }
            if screen {
                return refuse("a basket sleeve cannot name a screen");
            }
        }
        _ => {
            if screen {
                return refuse("a manual sleeve cannot name a screen");
            }
            if basket {
                return refuse("a manual sleeve cannot name a basket");
            }
        }
    }
    Ok(())
}

/// `SleeveSetIn` (and its `SleeveIn`s; neither forbids extra keys).
fn sleeve_set_in() -> Model {
    let sleeve_in = Model {
        fields: vec![
            ("name", Ty::str_len(Some(1), Some(80)), D::Required),
            (
                "kind",
                Ty::Str {
                    min: None,
                    max: None,
                    pattern: Some(("^(screen|manual|basket)$", kind_matches)),
                },
                D::Required,
            ),
            ("capital", Ty::Dec(vec![Bound::Ge("0")]), D::Required),
            ("screen_public_id", Ty::str_len(None, None).opt(), D::Unset),
            ("basket_slug", Ty::str_len(None, None).opt(), D::Unset),
            (
                "top_n",
                Ty::Int(vec![Bound::Ge("1"), Bound::Le("100")]),
                D::Unset,
            ),
        ],
        forbid_extra: false,
        before: None,
        after: None,
    };
    Model {
        fields: vec![(
            "sleeves",
            Ty::List(Box::new(Ty::Model(Box::new(sleeve_in)))),
            D::Required,
        )],
        forbid_extra: false,
        before: None,
        after: Some(|fields| {
            let Some(PV::List(items)) = field(fields, "sleeves") else {
                return Ok(());
            };
            if items.len() > MAX_SLEEVES {
                return Err(format!(
                    "a portfolio may have at most {MAX_SLEEVES} sleeves"
                ));
            }
            let names: Vec<String> = items
                .iter()
                .map(|i| match i {
                    PV::Model(f) => {
                        py_strip(&text(field(f, "name")).unwrap_or_default()).to_owned()
                    }
                    _ => String::new(),
                })
                .collect();
            let mut seen = std::collections::HashSet::new();
            if !names.iter().all(|n| seen.insert(n.clone())) {
                return Err("two sleeves cannot share a name".to_owned());
            }
            for item in items {
                if let PV::Model(f) = item {
                    check_source(f)?;
                }
            }
            Ok(())
        }),
    }
}

/// One validated sleeve.
struct Spec {
    name: String,
    kind: String,
    capital: super::pyd::PyDec,
    screen: Option<String>,
    basket: Option<String>,
    top_n: i64,
}

/// `PUT /portfolios/{portfolio_id}/sleeves`: replace the whole division.
///
/// # Errors
/// 400 (path, body), 401, 404 (portfolio, screen, basket), 503 (no sole tenant, for a private
/// basket), 500.
pub async fn replace_sleeves(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    require_authenticated(&principal)?;
    let mut v = Validation::new();
    let id = int_param(&mut v, "portfolio_id", Some(&raw_id));
    let path_errors = match v.finish() {
        Ok(()) => Vec::new(),
        Err(p) => errors_of(&p),
    };
    let body = match validate_body(&received, &sleeve_set_in()) {
        Ok(b) if path_errors.is_empty() => b,
        Ok(_) => return Err(invalid_screen_definition(path_errors)),
        Err(f) => return Err(f.into_problem(path_errors)),
    };
    let user_id = principal.require_user()?;
    let portfolio_id = id_or_500(id.unwrap_or(LaxInt::Invalid))?;
    let mut specs = Vec::new();
    if let Some(PV::List(items)) = body.field("sleeves") {
        for item in items {
            let PV::Model(f) = item else { continue };
            specs.push(Spec {
                name: text(field(f, "name")).unwrap_or_default(),
                kind: text(field(f, "kind")).unwrap_or_default(),
                capital: match field(f, "capital") {
                    Some(PV::Dec(d)) => d.clone(),
                    _ => return Err(crate::problems::internal_error()),
                },
                screen: text(field(f, "screen_public_id")),
                basket: text(field(f, "basket_slug")),
                top_n: match field(f, "top_n") {
                    Some(PV::Int(i)) => i.as_i64().unwrap_or(DEFAULT_TOP_N),
                    _ => DEFAULT_TOP_N,
                },
            });
        }
    }
    let owned: Option<i64> =
        sqlx::query_scalar("SELECT id FROM portfolio WHERE id = $1 AND user_id = $2")
            .bind(portfolio_id)
            .bind(user_id)
            .fetch_optional(state.db.pool())
            .await
            .map_err(db)?;
    if owned.is_none() {
        return Err(not_found("portfolio", &portfolio_id.to_string()));
    }
    // `_screens_by_public_id`: any screen with that public id (no visibility check, as Python).
    let wanted_screens: Vec<String> = specs
        .iter()
        .filter_map(|s| s.screen.clone().filter(|x| !x.is_empty()))
        .collect();
    let screens: Vec<(i64, String)> = if wanted_screens.is_empty() {
        Vec::new()
    } else {
        sqlx::query_as("SELECT id, public_id FROM screen WHERE public_id = ANY($1)")
            .bind(&wanted_screens)
            .fetch_all(state.db.pool())
            .await
            .map_err(db)?
    };
    // `_baskets_by_slug`: unarchived; a private one only for the sole tenant.
    let wanted_baskets: Vec<String> = specs
        .iter()
        .filter_map(|s| s.basket.clone().filter(|x| !x.is_empty()))
        .collect();
    let mut baskets: Vec<(i64, String)> = Vec::new();
    if !wanted_baskets.is_empty() {
        let rows: Vec<(i64, String, String)> = sqlx::query_as(
            "SELECT id, slug, visibility FROM cb_basket WHERE slug = ANY($1) AND archived_at IS NULL",
        )
        .bind(&wanted_baskets)
        .fetch_all(state.db.pool())
        .await
        .map_err(db)?;
        let mut sole: Option<crate::routes::common::SoleId> = None;
        for (bid, slug, visibility) in rows {
            if visibility != PUBLISHED {
                if sole.is_none() {
                    sole = Some(crate::routes::common::resolve_sole_tenant(&state.db).await?);
                }
                if sole != Some(crate::routes::common::SoleId::Id(user_id)) {
                    continue;
                }
            }
            baskets.push((bid, slug));
        }
    }
    let screen_id = |p: &str| screens.iter().find(|(_, s)| s == p).map(|(i, _)| *i);
    let basket_id = |p: &str| baskets.iter().rev().find(|(_, s)| s == p).map(|(i, _)| *i);
    let mut rows = Vec::with_capacity(specs.len());
    for spec in &specs {
        let sid = match (&spec.kind[..], &spec.screen) {
            ("screen", Some(p)) => {
                let found = screen_id(p);
                if found.is_none() {
                    return Err(not_found("screen", p));
                }
                found
            }
            ("screen", None) => return Err(not_found("screen", "")),
            _ => None,
        };
        let bid = match (&spec.kind[..], &spec.basket) {
            ("basket", Some(p)) => {
                let found = basket_id(p);
                if found.is_none() {
                    return Err(not_found("basket", p));
                }
                found
            }
            ("basket", None) => return Err(not_found("basket", "")),
            _ => None,
        };
        let sid = if spec.kind == "screen" && spec.screen.as_deref().is_some_and(|s| !s.is_empty())
        {
            sid
        } else {
            None
        };
        let bid = if spec.kind == "basket" && spec.basket.as_deref().is_some_and(|s| !s.is_empty())
        {
            bid
        } else {
            None
        };
        rows.push((
            py_strip(&spec.name).to_owned(),
            spec.kind.clone(),
            sid,
            bid,
            bindable(&spec.capital)?,
            spec.top_n,
        ));
    }
    let pool = pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            sqlx::query("DELETE FROM portfolio_sleeve WHERE portfolio_id = $1")
                .bind(portfolio_id)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            for (order, (name, kind, sid, bid, capital, top_n)) in rows.into_iter().enumerate() {
                sqlx::query(
                    "INSERT INTO portfolio_sleeve (portfolio_id, name, kind, screen_id, basket_id, \
                     capital, top_n, sort_order) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                )
                .bind(portfolio_id)
                .bind(name)
                .bind(kind)
                .bind(sid)
                .bind(bid)
                .bind(capital)
                .bind(i16::try_from(top_n).unwrap_or(i16::MAX))
                .bind(i16::try_from(order).unwrap_or(i16::MAX))
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            }
            Ok(())
        })
    })
    .await?;
    super::sleeves_response(&state, portfolio_id).await
}

/// The `errors` entries of a 400 the query/path validator built.
pub(crate) fn errors_of(p: &Problem) -> Vec<crate::problems::ErrorEntry> {
    p.extra
        .iter()
        .find(|(k, _)| k == "errors")
        .and_then(|(_, e)| match e {
            OrderedJson::Array(items) => Some(
                items
                    .iter()
                    .filter_map(|i| match i {
                        OrderedJson::Object(entries) => Some(entries.clone()),
                        _ => None,
                    })
                    .collect(),
            ),
            _ => None,
        })
        .unwrap_or_default()
}
