//! The **write** routes of `baskfy_api.routers.managers` (wave 4c, lane writes-screens), with the
//! pure rules they call — `baskfy_core.manager_onboarding` (`next_state`) and
//! `baskfy_core.sebi_registration` (`check_format`), ported here privately (DECISIONS-RS R2.3):
//!
//! * `POST /managers/me` — apply (a new identity in SUBMITTED), or resubmit (DRAFT/REJECTED →
//!   SUBMITTED; editing a SUBMITTED application is not a transition; APPROVED and SUSPENDED are
//!   refused); the verification stamp never carries over;
//! * `PATCH /managers/{slug}` — staff review: the move `next_state` allows, nothing else;
//! * `POST`/`DELETE /managers/me/baskets/{basket_slug}/publish` — list or unlist one of your own
//!   baskets. **Publishing is a listing, never an order** (law 2): `placed_an_order` is always
//!   `false`.

use axum::extract::{Extension, Path, Request};
use axum::http::StatusCode;
use axum::response::Response;
use serde::Serialize;
use sqlx::Row;
use sqlx::postgres::PgRow;

use super::manager_out;
use crate::auth::{Principal, require_authenticated, require_staff};
use crate::problems::{Problem, ProblemType, internal_error, not_found};
use crate::routes::common::py_space;
use crate::routes::screens::body_model::{self, Errors, Model, Ty, opt, req};
use crate::routes::screens::write_kit::{self, db};
use crate::session::{self, Tx, WritePool};
use crate::wire;

// ---------------------------------------------------------------------------------------------
// baskfy_core.manager_onboarding
// ---------------------------------------------------------------------------------------------

/// `MANAGER_STATES`, in the happy path's order.
const MANAGER_STATES: [&str; 5] = ["DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "SUSPENDED"];

/// `TRANSITIONS[state]`, sorted (the order `ManagerStateError` prints them in).
fn transitions(state: &str) -> &'static [&'static str] {
    match state {
        "DRAFT" | "REJECTED" | "SUSPENDED" => &["SUBMITTED"],
        "SUBMITTED" => &["APPROVED", "REJECTED"],
        "APPROVED" => &["SUSPENDED"],
        _ => &[],
    }
}

/// Why `next_state` refused.
#[derive(Debug, Clone, PartialEq, Eq)]
enum StateError {
    /// `ManagerStateError` (a `ValueError` the routes turn into a 400).
    Illegal(String),
    /// A plain `ValueError`: not a manager state at all (a 500 where the routes do not catch it).
    NotAState(String),
}

/// `next_state(current, requested)`.
fn next_state(current: &str, requested: &str) -> Result<String, StateError> {
    for (name, value) in [("current", current), ("requested", requested)] {
        if !MANAGER_STATES.contains(&value) {
            return Err(StateError::NotAState(format!(
                "{name} state {} is not a manager state; expected one of {}",
                baskfy_providers::py::repr_str(value),
                MANAGER_STATES.join(", ")
            )));
        }
    }
    let legal = transitions(current);
    if !legal.contains(&requested) {
        let listed = if legal.is_empty() {
            "nothing".to_owned()
        } else {
            legal.join(", ")
        };
        return Err(StateError::Illegal(format!(
            "a manager in {current} cannot become {requested}; from {current} the legal next \
             states are {listed}"
        )));
    }
    Ok(requested.to_owned())
}

// ---------------------------------------------------------------------------------------------
// baskfy_core.sebi_registration
// ---------------------------------------------------------------------------------------------

/// `REGISTRATION_TYPES`.
const REGISTRATION_TYPES: [&str; 7] = [
    "NONE",
    "RESEARCH_ANALYST",
    "INVESTMENT_ADVISER",
    "PORTFOLIO_MANAGER",
    "AIF",
    "MF_DISTRIBUTOR",
    "UNKNOWN",
];

/// Python's `re` `\d` on `str`: a Unicode decimal digit (`Nd`).
fn is_py_digit(c: char) -> bool {
    crate::routes::common::py_decimal(c).is_some()
}

fn digits(s: &str, n: usize) -> bool {
    s.chars().count() == n && s.chars().all(is_py_digit)
}

/// `REGISTRATION_PATTERNS[kind].match(cleaned)` — the anchored patterns, by hand.
fn pattern_matches(kind: &str, s: &str) -> bool {
    let prefixed = |prefix: &str| s.strip_prefix(prefix).is_some_and(|rest| digits(rest, 9));
    match kind {
        "RESEARCH_ANALYST" => prefixed("INH"),
        "INVESTMENT_ADVISER" => prefixed("INA"),
        "PORTFOLIO_MANAGER" => prefixed("INP"),
        // ^IN/AIF[123]/\d{2}-\d{2}/\d{4}$
        "AIF" => s
            .strip_prefix("IN/AIF")
            .and_then(|rest| {
                let mut chars = rest.chars();
                let category = chars.next()?;
                ['1', '2', '3']
                    .contains(&category)
                    .then(|| chars.as_str().to_owned())
            })
            .and_then(|rest| rest.strip_prefix('/').map(str::to_owned))
            .is_some_and(|rest| {
                let parts: Vec<&str> = rest.splitn(2, '/').collect();
                let [years, serial] = parts.as_slice() else {
                    return false;
                };
                let yy: Vec<&str> = years.splitn(2, '-').collect();
                matches!(yy.as_slice(), [a, b] if digits(a, 2) && digits(b, 2)) && digits(serial, 4)
            }),
        // ^ARN-\d{1,6}$
        "MF_DISTRIBUTOR" => s.strip_prefix("ARN-").is_some_and(|rest| {
            let n = rest.chars().count();
            (1..=6).contains(&n) && rest.chars().all(is_py_digit)
        }),
        _ => false,
    }
}

/// The pattern-bearing types, in `REGISTRATION_PATTERNS`' order.
const PATTERNED: [&str; 5] = [
    "RESEARCH_ANALYST",
    "INVESTMENT_ADVISER",
    "PORTFOLIO_MANAGER",
    "AIF",
    "MF_DISTRIBUTOR",
];

/// `normalise(number)`: every whitespace run removed, upper-cased.
fn normalise(number: &str) -> String {
    number
        .chars()
        .filter(|c| !py_space(*c))
        .collect::<String>()
        .to_uppercase()
}

/// `check_format(registration_type, number)` reduced to what the route stores:
/// `(registration_type, normalised)`, or the `ValueError`'s message.
fn check_format(kind: &str, number: Option<&str>) -> Result<(String, String), String> {
    if !REGISTRATION_TYPES.contains(&kind) {
        return Err(format!(
            "{} is not a registration type; expected one of {}",
            baskfy_providers::py::repr_str(kind),
            REGISTRATION_TYPES.join(", ")
        ));
    }
    if kind == "NONE" {
        if !matches!(number, None | Some("")) {
            return Err(
                "a registration type of NONE cannot carry a number; declare the type \
                        that number belongs to, or UNKNOWN"
                    .to_owned(),
            );
        }
        return Ok(("NONE".to_owned(), String::new()));
    }
    let Some(number) = number.filter(|n| !n.trim_matches(py_space).is_empty()) else {
        return Err(format!("registration type {kind} requires a number"));
    };
    let cleaned = normalise(number);
    if !PATTERNED.contains(&kind) {
        return Ok(("UNKNOWN".to_owned(), cleaned));
    }
    if pattern_matches(kind, &cleaned) {
        return Ok((kind.to_owned(), cleaned));
    }
    Ok(("UNKNOWN".to_owned(), cleaned))
}

// ---------------------------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------------------------

/// `RegistrationType`.
const REGISTRATION_LITERAL: &[&str] = &REGISTRATION_TYPES;
/// `ManagerReviewIn.state`.
const REVIEW_STATES: &[&str] = &["APPROVED", "REJECTED", "SUSPENDED", "SUBMITTED"];

/// `ManagerApplyIn` (a plain `BaseModel`: extra keys are ignored).
fn apply_in() -> Model {
    Model {
        fields: vec![
            req("name", Ty::Str(Some(2), Some(120))),
            opt("bio", Ty::Opt(Box::new(Ty::Str(None, Some(4000))))),
            opt("sebi_reg_type", Ty::Lit(REGISTRATION_LITERAL)),
            opt("sebi_reg_no", Ty::Opt(Box::new(Ty::Str(None, Some(64))))),
            opt("sebi_reg_valid_from", Ty::Opt(Box::new(Ty::Date))),
            opt("sebi_reg_valid_to", Ty::Opt(Box::new(Ty::Date))),
        ],
        forbid_extra: false,
    }
}

/// `ManagerReviewIn` (extra keys ignored; `note` is validated and never stored).
fn review_in() -> Model {
    Model {
        fields: vec![
            req("state", Ty::Lit(REVIEW_STATES)),
            opt("note", Ty::Opt(Box::new(Ty::Str(None, Some(2000))))),
        ],
        forbid_extra: false,
    }
}

const MANAGER_COLUMNS: &str = "id, slug, name, kind, state, sebi_reg_type, sebi_reg_no, \
                               sebi_reg_valid_from, sebi_reg_valid_to, sebi_reg_verified_at";

fn invalid(detail: impl Into<String>) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, detail)
}

/// A `next_state` refusal as the routes surface it: `ManagerStateError` is the 400, any other
/// `ValueError` escapes to the 500 catch-all.
fn state_problem(e: StateError) -> Problem {
    match e {
        StateError::Illegal(m) => invalid(m),
        StateError::NotAState(m) => {
            crate::logging::error("baskfy_api.app", "unhandled error", &[("error", &m)]);
            internal_error()
        }
    }
}

async fn mine(tx: &mut Tx, user_id: i64) -> Result<PgRow, Problem> {
    sqlx::query(&format!(
        "SELECT {MANAGER_COLUMNS} FROM cb_manager WHERE user_id = $1"
    ))
    .bind(user_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(db)?
    .ok_or_else(|| not_found("manager", "me"))
}

/// `POST /managers/me` (`apply`): 201, for a new application and a resubmission alike.
///
/// # Errors
/// 400 (body, an incoherent registration, a refused transition), 401, 500.
pub async fn apply_as_manager(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &apply_in(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let user_id = principal.require_user()?;
    let kind = body.get("sebi_reg_type").str().unwrap_or("NONE").to_owned();
    let (reg_type, normalised) =
        check_format(&kind, body.get("sebi_reg_no").str()).map_err(invalid)?;
    let reg_no = (reg_type != "NONE").then_some(normalised);
    let name = body.get("name").str().unwrap_or_default().to_owned();
    let bio = body.get("bio").str().map(str::to_owned);
    let valid_from = body.get("sebi_reg_valid_from").date();
    let valid_to = body.get("sebi_reg_valid_to").date();
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let existing = sqlx::query("SELECT id, state FROM cb_manager WHERE user_id = $1")
                .bind(user_id)
                .fetch_optional(&mut **tx)
                .await
                .map_err(db)?;
            let r = if let Some(found) = existing {
                let id: i64 = found.try_get("id").map_err(db)?;
                let mut state: String = found.try_get("state").map_err(db)?;
                if state != "SUBMITTED" {
                    state = next_state(&state, "SUBMITTED").map_err(state_problem)?;
                }
                // The ORM writes only a net change: no UPDATE when every value is unchanged.
                sqlx::query(
                    "UPDATE cb_manager SET state = $2, name = $3, bio = $4, sebi_reg_type = $5, \
                     sebi_reg_no = $6, sebi_reg_valid_from = $7, sebi_reg_valid_to = $8, \
                     sebi_reg_verified_at = NULL WHERE id = $1 AND (state, name, bio, \
                     sebi_reg_type, sebi_reg_no, sebi_reg_valid_from, sebi_reg_valid_to, \
                     sebi_reg_verified_at) IS DISTINCT FROM ($2, $3, $4, $5, $6, $7, $8, NULL)",
                )
                .bind(id)
                .bind(&state)
                .bind(&name)
                .bind(&bio)
                .bind(&reg_type)
                .bind(&reg_no)
                .bind(valid_from)
                .bind(valid_to)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
                sqlx::query(&format!(
                    "SELECT {MANAGER_COLUMNS} FROM cb_manager WHERE id = $1"
                ))
                .bind(id)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?
            } else {
                let state = next_state("DRAFT", "SUBMITTED").map_err(state_problem)?;
                sqlx::query(&format!(
                    "INSERT INTO cb_manager (slug, name, kind, user_id, state, bio, strategies, \
                     sebi_reg_type, sebi_reg_no, sebi_reg_valid_from, sebi_reg_valid_to) \
                     VALUES ($1, $2, 'EXTERNAL', $3, $4, $5, '{{}}'::varchar[], $6, $7, $8, $9) \
                     RETURNING {MANAGER_COLUMNS}"
                ))
                .bind(format!("m-{user_id}"))
                .bind(&name)
                .bind(user_id)
                .bind(&state)
                .bind(&bio)
                .bind(&reg_type)
                .bind(&reg_no)
                .bind(valid_from)
                .bind(valid_to)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?
            };
            manager_out(&r)
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// `PATCH /managers/{slug}` (`review`): staff only.
///
/// # Errors
/// 401 (anonymous), 404 (not staff — never a 403 — or no such manager), 400 (body, the slug's
/// length, an illegal transition), 500.
pub async fn review_manager(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(slug): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    require_staff(&principal)?;
    let mut e = Errors::new();
    body_model::path_str(&mut e, "slug", &slug, Some(1), Some(120));
    let body = body_model::body(&mut e, &received, &review_in(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let requested = body.get("state").str().unwrap_or_default().to_owned();
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let found = sqlx::query("SELECT id, state FROM cb_manager WHERE slug = $1")
                .bind(&slug)
                .fetch_optional(&mut **tx)
                .await
                .map_err(db)?
                .ok_or_else(|| not_found("manager", &slug))?;
            let id: i64 = found.try_get("id").map_err(db)?;
            let current: String = found.try_get("state").map_err(db)?;
            let state = next_state(&current, &requested).map_err(state_problem)?;
            let r = sqlx::query(&format!(
                "UPDATE cb_manager SET state = $2 WHERE id = $1 RETURNING {MANAGER_COLUMNS}"
            ))
            .bind(id)
            .bind(&state)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            manager_out(&r)
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `PATCH /managers/me`: Starlette routes it to `PATCH /managers/{slug}` with `slug="me"` (the
/// static `GET`/`POST /managers/me` routes match the path but not the method).
///
/// # Errors
/// As [`review_manager`].
pub async fn review_manager_me(
    principal: Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    review_manager(principal, write, Path("me".to_owned()), request).await
}

/// `PublishOut`.
#[derive(Debug, Clone, Serialize)]
struct PublishOut {
    basket_slug: String,
    visibility: String,
    placed_an_order: bool,
}

/// `_my_basket`: one of the caller's own baskets (joined through `cb_manager.user_id`), or 404.
async fn my_basket(tx: &mut Tx, user_id: i64, basket_slug: &str) -> Result<i64, Problem> {
    sqlx::query_scalar(
        "SELECT cb_basket.id FROM cb_basket JOIN cb_manager ON cb_manager.id = cb_basket.manager_id \
         WHERE cb_basket.slug = $1 AND cb_manager.user_id = $2",
    )
    .bind(basket_slug)
    .bind(user_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(db)?
    .ok_or_else(|| not_found("basket", basket_slug))
}

async fn set_visibility(
    write: Option<Extension<WritePool>>,
    principal: &Principal,
    basket_slug: String,
    publishing: bool,
) -> Result<Response, Problem> {
    let principal = require_authenticated(principal)?.clone();
    let mut e = Errors::new();
    body_model::path_str(&mut e, "basket_slug", &basket_slug, Some(1), Some(120));
    e.finish()?;
    let user_id = principal.require_user()?;
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let manager = mine(tx, user_id).await?;
            if publishing {
                let state: String = manager.try_get("state").map_err(db)?;
                if !super::may_publish(&state) {
                    return Err(invalid(format!(
                        "a manager in {state} may not publish; an approved review is required \
                         first"
                    )));
                }
            }
            let id = my_basket(tx, user_id, &basket_slug).await?;
            let visibility = if publishing { "PUBLISHED" } else { "PRIVATE" };
            // The ORM writes only a net change.
            sqlx::query(
                "UPDATE cb_basket SET visibility = $2 WHERE id = $1 \
                 AND visibility IS DISTINCT FROM $2",
            )
            .bind(id)
            .bind(visibility)
            .execute(&mut **tx)
            .await
            .map_err(db)?;
            let slug: String = sqlx::query_scalar("SELECT slug FROM cb_basket WHERE id = $1")
                .bind(id)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?;
            Ok(PublishOut {
                basket_slug: slug,
                visibility: visibility.to_owned(),
                placed_an_order: false,
            })
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `POST /managers/me/baskets/{basket_slug}/publish` (`publish`): a listing, never an order.
///
/// # Errors
/// 401, 400 (the slug's length; a manager not APPROVED), 404 (no identity, not your basket), 500.
pub async fn publish_basket(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(basket_slug): Path<String>,
) -> Result<Response, Problem> {
    set_visibility(write, &principal, basket_slug, true).await
}

/// `DELETE /managers/me/baskets/{basket_slug}/publish` (`unpublish`): allowed from any state.
///
/// # Errors
/// 401, 400 (the slug's length), 404 (no identity, not your basket), 500.
pub async fn unpublish_basket(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(basket_slug): Path<String>,
) -> Result<Response, Problem> {
    set_visibility(write, &principal, basket_slug, false).await
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    // `packages/core/tests/test_manager_onboarding.py`, the assertions that bind the routes.
    #[test]
    fn suspension_cannot_be_undone_in_one_step() {
        assert_eq!(next_state("DRAFT", "SUBMITTED").unwrap(), "SUBMITTED");
        assert_eq!(next_state("SUSPENDED", "SUBMITTED").unwrap(), "SUBMITTED");
        assert_eq!(next_state("REJECTED", "SUBMITTED").unwrap(), "SUBMITTED");
        assert_eq!(
            next_state("SUSPENDED", "APPROVED"),
            Err(StateError::Illegal(
                "a manager in SUSPENDED cannot become APPROVED; from SUSPENDED the legal next \
                 states are SUBMITTED"
                    .to_owned()
            ))
        );
        assert_eq!(
            next_state("SUBMITTED", "SUBMITTED"),
            Err(StateError::Illegal(
                "a manager in SUBMITTED cannot become SUBMITTED; from SUBMITTED the legal next \
                 states are APPROVED, REJECTED"
                    .to_owned()
            ))
        );
        assert!(matches!(
            next_state("LIMBO", "SUBMITTED"),
            Err(StateError::NotAState(_))
        ));
    }

    // `packages/core/tests/test_sebi_registration.py`, the format half.
    #[test]
    fn registration_numbers_are_checked_for_shape_only() {
        assert_eq!(
            check_format("RESEARCH_ANALYST", Some(" inh 000001234 ")).unwrap(),
            ("RESEARCH_ANALYST".to_owned(), "INH000001234".to_owned())
        );
        assert_eq!(
            check_format("RESEARCH_ANALYST", Some("INA000001234"))
                .unwrap()
                .0,
            "UNKNOWN"
        );
        assert_eq!(
            check_format("AIF", Some("IN/AIF2/21-22/0999")).unwrap().0,
            "AIF"
        );
        assert_eq!(
            check_format("MF_DISTRIBUTOR", Some("ARN-123456"))
                .unwrap()
                .0,
            "MF_DISTRIBUTOR"
        );
        assert_eq!(
            check_format("MF_DISTRIBUTOR", Some("ARN-1234567"))
                .unwrap()
                .0,
            "UNKNOWN"
        );
        assert_eq!(
            check_format("UNKNOWN", Some("x y")).unwrap(),
            ("UNKNOWN".to_owned(), "XY".to_owned())
        );
        assert_eq!(check_format("NONE", None).unwrap().0, "NONE");
        assert!(check_format("NONE", Some("INH000001234")).is_err());
        assert_eq!(
            check_format("AIF", Some("  ")).unwrap_err(),
            "registration type AIF requires a number"
        );
    }
}
