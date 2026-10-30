//! The **write** routes of `baskfy_api.routers.explore` (wave 6, lane api_routes; R6.40's two
//! unassigned routes):
//!
//! * `POST /watchlist` — watch a visible basket (re-watching restamps it and its NAV), 201;
//! * `DELETE /watchlist/{slug}` — stop watching it, 204.
//!
//! Sole-tenant surfaces (`scoped_sole_user_id`), one transaction per request; every read of
//! `cb_basket` applies `curated_visibility.visible_baskets()`. A watchlist is a list of names:
//! nothing here reaches an order (law 2).
//!
//! `WatchlistAddIn.nav_at_watch` is `Annotated[Decimal, Field(gt=0, max_digits=18,
//! decimal_places=2)] | None`: pydantic-core checks the digits **before** the bound, on the value
//! `normalize()`d in the default context (28 digits, half-even) — reproduced in
//! [`nav_constraint`] and held to pydantic's own answers by its tests.

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_core::decimal::WideDecimal;
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::Serialize;

use super::SLUG_MAX_LENGTH;
use crate::auth::{Principal, require_authenticated};
use crate::body::Received;
use crate::problems::{Problem, internal_error, not_found};
use crate::routes::screens::accounts::scoped_sole_user_id;
use crate::routes::screens::body_model::{self, Errors, Model, Ty, V, req};
use crate::routes::screens::write_kit::{self, db};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

/// `NAV_MAX_DIGITS` / `NAV_DECIMAL_PLACES` (`PRICE` is `Numeric(18, 2)`).
const NAV_MAX_DIGITS: u64 = 18;
const NAV_DECIMAL_PLACES: u64 = 2;

/// `WatchlistItemOut`.
#[derive(Debug, Serialize)]
struct WatchlistItemOut {
    basket_slug: String,
    basket_name: String,
    #[serde(serialize_with = "wire::ser::datetime")]
    watched_at: DateTime<Utc>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    nav_at_watch: Option<Decimal>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    moved_pct: Option<Decimal>,
}

/// `(decimals, digits)` as pydantic-core counts them: `normalize()` in the default context, then
/// `as_tuple()` — a positive exponent adds digits, a negative one is the decimals (and at least as
/// many digits).
fn digits_info(value: &WideDecimal) -> (u64, u64) {
    // `x + 0` rounds to the context's 28 digits exactly as `normalize()` does before it strips
    // the trailing zeros below.
    let rounded = value.add(&WideDecimal::zero());
    let text = rounded.to_sci_string();
    let body = text.trim_start_matches('-');
    let (mantissa, exp_text) = body.split_once(['E', 'e']).unwrap_or((body, "0"));
    let mut exponent: i64 = exp_text.parse().unwrap_or(0);
    let (whole, frac) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    exponent -= i64::try_from(frac.len()).unwrap_or(0);
    let mut coefficient = format!("{whole}{frac}").trim_start_matches('0').to_owned();
    if coefficient.is_empty() {
        // `Decimal(0).normalize()` is `0`: one digit, exponent 0.
        return (0, 1);
    }
    while coefficient.ends_with('0') {
        coefficient.pop();
        exponent += 1;
    }
    let mut digits = u64::try_from(coefficient.len()).unwrap_or(u64::MAX);
    if exponent >= 0 {
        digits = digits.saturating_add(exponent.unsigned_abs());
        (0, digits)
    } else {
        let decimals = exponent.unsigned_abs();
        (decimals, digits.max(decimals))
    }
}

fn plural(n: u64, word: &str) -> String {
    if n == 1 {
        word.to_owned()
    } else {
        format!("{word}s")
    }
}

/// The first constraint error pydantic reports for a parsed `nav_at_watch`, in its order:
/// `max_digits`, `decimal_places`, the whole digits, then `gt=0`.
fn nav_constraint(value: &WideDecimal) -> Option<(String, &'static str)> {
    let (decimals, digits) = digits_info(value);
    if digits > NAV_MAX_DIGITS {
        return Some((
            format!(
                "Decimal input should have no more than {NAV_MAX_DIGITS} {} in total",
                plural(NAV_MAX_DIGITS, "digit")
            ),
            "decimal_max_digits",
        ));
    }
    if decimals > NAV_DECIMAL_PLACES {
        return Some((
            format!(
                "Decimal input should have no more than {NAV_DECIMAL_PLACES} {}",
                plural(NAV_DECIMAL_PLACES, "decimal place")
            ),
            "decimal_max_places",
        ));
    }
    let whole = digits.saturating_sub(decimals);
    let allowed = NAV_MAX_DIGITS - NAV_DECIMAL_PLACES;
    if whole > allowed {
        return Some((
            format!(
                "Decimal input should have no more than {allowed} {} before the decimal point",
                plural(allowed, "digit")
            ),
            "decimal_whole_digits",
        ));
    }
    if value.is_zero() || value.is_sign_negative() {
        return Some(("Input should be greater than 0".to_owned(), "greater_than"));
    }
    None
}

/// `WatchlistAddIn` (`extra="forbid"`): the slug and the optional, bounded NAV. Errors in
/// pydantic's order — the fields as declared, then each extra key.
fn add_in(e: &mut Errors, received: &Received) -> Option<(String, Option<WideDecimal>)> {
    let Received::Json(OrderedJson::Object(entries)) = received else {
        // Missing, `null`, or not an object: the model's own root errors.
        let model = Model {
            fields: vec![req("basket_slug", Ty::Str(Some(1), Some(SLUG_MAX_LENGTH)))],
            forbid_extra: true,
        };
        let _ = body_model::body(e, received, &model, true);
        return None;
    };
    let mut failed = false;
    let field = |name: &str| entries.iter().find(|(k, _)| k == name).map(|(_, v)| v);
    let slug = match field("basket_slug") {
        None => {
            e.push(&["basket_slug".to_owned()], "Field required", "missing");
            failed = true;
            None
        }
        Some(raw) => {
            let v = body_model::validate(
                e,
                &Ty::Str(Some(1), Some(SLUG_MAX_LENGTH)),
                raw,
                &["basket_slug".to_owned()],
            )
            .and_then(|v| v.str().map(str::to_owned));
            failed |= v.is_none();
            v
        }
    };
    let nav = match field("nav_at_watch") {
        None | Some(OrderedJson::Null) => None,
        Some(raw) => {
            let loc = ["nav_at_watch".to_owned()];
            let unbounded = Ty::Dec {
                gt: None,
                ge: None,
                le: None,
            };
            match body_model::validate(e, &unbounded, raw, &loc) {
                Some(V::Dec(d)) => {
                    if let Some((message, kind)) = nav_constraint(&d) {
                        e.push(&loc, &message, kind);
                        failed = true;
                    }
                    Some(d)
                }
                _ => {
                    failed = true;
                    None
                }
            }
        }
    };
    for (k, _) in entries {
        if k != "basket_slug" && k != "nav_at_watch" {
            e.push(
                std::slice::from_ref(k),
                "Extra inputs are not permitted",
                "extra_forbidden",
            );
            failed = true;
        }
    }
    if failed {
        return None;
    }
    slug.map(|s| (s, nav))
}

/// The visible basket with this slug: `(id, slug, name)`.
async fn visible_basket(
    tx: &mut crate::session::Tx,
    slug: &str,
) -> Result<Option<(i64, String, String)>, Problem> {
    let found = sqlx::query_as::<_, (i64, String, String)>(
        "SELECT b.id, b.slug, b.name FROM cb_basket b WHERE b.slug = $1 \
         AND b.archived_at IS NULL AND b.visibility = 'PUBLISHED'",
    )
    .bind(slug)
    .fetch_optional(&mut **tx)
    .await
    .map_err(db)?;
    Ok(found)
}

/// `POST /watchlist` (`add_watchlist`): 201.
///
/// # Errors
/// 400 (body), 401, the sole-tenant refusals, 404 (no such visible basket), 500.
pub async fn add_watchlist(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let parsed = add_in(&mut e, &received);
    e.finish()?;
    let (slug, nav) = parsed.ok_or_else(internal_error)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let pool = write_kit::pool(write)?;
    let nav_text = nav.map(|d| d.to_sci_string());
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let Some((basket_id, basket_slug, basket_name)) = visible_basket(tx, &slug).await?
            else {
                return Err(not_found("basket", &slug));
            };
            let existing: Option<i64> = sqlx::query_scalar(
                "SELECT id FROM cb_watchlist_item WHERE user_id = $1 AND basket_id = $2",
            )
            .bind(user_id)
            .bind(basket_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let now = write_kit::now_utc();
            let r = match existing {
                Some(id) => sqlx::query(
                    "UPDATE cb_watchlist_item SET watched_at = $1, \
                     nav_at_watch = ($2::text)::numeric WHERE id = $3 \
                     RETURNING watched_at, nav_at_watch",
                )
                .bind(now)
                .bind(nav_text)
                .bind(id)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?,
                None => sqlx::query(
                    "INSERT INTO cb_watchlist_item (user_id, basket_id, watched_at, nav_at_watch) \
                     VALUES ($1, $2, $3, ($4::text)::numeric) RETURNING watched_at, nav_at_watch",
                )
                .bind(user_id)
                .bind(basket_id)
                .bind(now)
                .bind(nav_text)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?,
            };
            // `session.refresh(item)`: the stored (rounded) values.
            Ok(WatchlistItemOut {
                basket_slug,
                basket_name,
                watched_at: row::timestamptz(&r, "watched_at")
                    .map_err(|e| crate::db_failure(&e))?,
                nav_at_watch: row::opt_decimal(&r, "nav_at_watch")
                    .map_err(|e| crate::db_failure(&e))?,
                moved_pct: None,
            })
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// `DELETE /watchlist/{slug}` (`remove_watchlist`): 204.
///
/// Also mounted on `/watchlist/instruments` and `/watchlist/discover-preferences` for `DELETE`:
/// Starlette tries this route before the instrument router's, so those two paths reach it with
/// the static segment as the slug.
///
/// # Errors
/// 400 (the slug's length), 401, the sole-tenant refusals, 404 (no such visible basket, or not
/// watched), 500.
pub async fn remove_watchlist(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(slug): Path<String>,
) -> Result<Response, Problem> {
    remove(state, principal, write, slug).await
}

/// [`remove_watchlist`] for `DELETE /watchlist/instruments` (Starlette matches it as a slug).
///
/// # Errors
/// As [`remove_watchlist`].
pub async fn remove_watchlist_instruments(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
) -> Result<Response, Problem> {
    remove(state, principal, write, "instruments".to_owned()).await
}

/// [`remove_watchlist`] for `DELETE /watchlist/discover-preferences` (matched as a slug).
///
/// # Errors
/// As [`remove_watchlist`].
pub async fn remove_watchlist_discover_preferences(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
) -> Result<Response, Problem> {
    remove(state, principal, write, "discover-preferences".to_owned()).await
}

async fn remove(
    state: AppState,
    principal: Principal,
    write: Option<Extension<WritePool>>,
    slug: String,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    body_model::path_str(&mut e, "slug", &slug, Some(1), Some(SLUG_MAX_LENGTH));
    e.finish()?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let pool = write_kit::pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let Some((basket_id, _, _)) = visible_basket(tx, &slug).await? else {
                return Err(not_found("basket", &slug));
            };
            let item: Option<i64> = sqlx::query_scalar(
                "SELECT id FROM cb_watchlist_item WHERE user_id = $1 AND basket_id = $2",
            )
            .bind(user_id)
            .bind(basket_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let Some(id) = item else {
                return Err(not_found("watchlist item", &slug));
            };
            sqlx::query("DELETE FROM cb_watchlist_item WHERE id = $1")
                .bind(id)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            Ok(())
        })
    })
    .await?;
    // FastAPI's 204 for a handler returning `None`: an empty `JSONResponse`, its content type set.
    Ok((
        StatusCode::NO_CONTENT,
        [(axum::http::header::CONTENT_TYPE, crate::wire::JSON)],
    )
        .into_response())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn check(text: &str) -> Option<&'static str> {
        nav_constraint(&text.parse().unwrap()).map(|(_, kind)| kind)
    }

    /// Each answer is pydantic's (`WatchlistAddIn.model_validate_json`, the API venv, 30 Oct
    /// 2026).
    #[test]
    fn the_nav_bounds_are_pydantics() {
        for (text, want) in [
            ("12.345", Some("decimal_max_places")),
            ("1234567890123456789", Some("decimal_max_digits")),
            ("1234567890123456.7", None),
            ("123456789012345678", Some("decimal_whole_digits")),
            ("0", Some("greater_than")),
            ("-1", Some("greater_than")),
            ("1E+2", None),
            ("1E+20", Some("decimal_max_digits")),
            ("0.001", Some("decimal_max_places")),
            ("100.00", None),
            ("100.000", None),
            ("1.10", None),
            ("12345678901234567.0", Some("decimal_whole_digits")),
            ("0.01", None),
            ("1E-3", Some("decimal_max_places")),
            ("-1234567890123456789012", Some("decimal_max_digits")),
            ("-0.001", Some("decimal_max_places")),
            ("1234567890123456.789", Some("decimal_max_digits")),
            (
                "0.0000000000000000000000000000000000001",
                Some("decimal_max_digits"),
            ),
            ("1.000000000000000000000000000000000001", None),
            (
                "99999999999999999999999999999.5",
                Some("decimal_max_digits"),
            ),
        ] {
            assert_eq!(check(text), want, "{text}");
        }
        assert_eq!(
            nav_constraint(&"12.345".parse().unwrap()).unwrap().0,
            "Decimal input should have no more than 2 decimal places"
        );
        assert_eq!(
            nav_constraint(&"123456789012345678".parse().unwrap())
                .unwrap()
                .0,
            "Decimal input should have no more than 16 digits before the decimal point"
        );
    }
}
