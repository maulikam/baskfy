//! `POST /cb/baskets` (`create_private_basket`) and the slug rule it shares with the
//! from-screen route (`slug_for`), with `baskfy_core.curated_baskets.assert_weights_sum_to_one`.

use std::collections::{HashMap, HashSet};

use axum::extract::{Extension, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use baskfy_core::decimal::{Rounding, WideDecimal};
use serde::Serialize;
use sqlx::Row;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, internal_error};
use crate::routes::common::py_space;
use crate::routes::screens::accounts::scoped_sole_user_id;
use crate::routes::screens::body_model::{self, Errors, Model, Ty, opt, req};
use crate::routes::screens::write_kit::{self, db, numeric_text};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;
use crate::wire;

/// `_MIN_CONSTITUENTS`.
const MIN_CONSTITUENTS: usize = 2;
/// `_SLUG_MAX`.
const SLUG_MAX: usize = 64;
/// `curated_baskets.MANAGER_SLUG_MAULIK`.
pub(crate) const MANAGER_SLUG_MAULIK: &str = "maulik";

/// `CreateBasketIn` (a plain `BaseModel`: extra keys ignored).
fn create_in() -> Model {
    Model {
        fields: vec![
            req("name", Ty::Str(Some(1), Some(120))),
            req(
                "constituents",
                Ty::List(
                    Box::new(Ty::Model(Model {
                        fields: vec![
                            req("symbol", Ty::Str(Some(1), Some(32))),
                            req(
                                "weight",
                                Ty::Dec {
                                    gt: None,
                                    ge: None,
                                    le: None,
                                },
                            ),
                        ],
                        forbid_extra: false,
                    })),
                    Some(MIN_CONSTITUENTS),
                ),
            ),
            opt("description_md", Ty::Opt(Box::new(Ty::Str(None, None)))),
        ],
        forbid_extra: false,
    }
}

fn invalid(detail: impl Into<String>) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, detail)
}

/// `assert_weights_sum_to_one(weights)`: each weight quantised to four places (half-even, the
/// default context), summed, within 0.00005 of 1.0000 — or the `ValueError`'s message.
///
/// # Errors
/// The message, or the 500 for a weight `quantize` cannot hold (Python's `InvalidOperation`).
pub(crate) fn assert_weights_sum_to_one(
    weights: &[WideDecimal],
) -> Result<Result<(), String>, Problem> {
    if weights.is_empty() {
        return Ok(Err("constituent weights cannot be empty".to_owned()));
    }
    let mut total = WideDecimal::zero();
    for w in weights {
        let q = w.quantize(-4, Rounding::HalfEven).map_err(|e| {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", &e.to_string())],
            );
            internal_error()
        })?;
        total = total.add(&q);
    }
    let target: WideDecimal = "1.0000".parse().map_err(|_| internal_error())?;
    let tolerance: WideDecimal = "0.00005".parse().map_err(|_| internal_error())?;
    let delta = total.sub(&target).abs();
    if delta > tolerance {
        return Ok(Err(format!(
            "constituent weights must sum to 1.0000, got {} (delta {} exceeds tolerance 0.00005)",
            total.to_sci_string(),
            delta.to_sci_string()
        )));
    }
    Ok(Ok(()))
}

/// `_slugify(name)`: lower-cased, every run of characters outside `[a-z0-9]` one `-`, trimmed of
/// `-`, `basket` when empty, cut to 64 characters.
fn slugify(name: &str) -> String {
    let lowered = name.trim_matches(py_space).to_lowercase();
    let mut slug = String::with_capacity(lowered.len());
    let mut in_run = false;
    for c in lowered.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            slug.push(c);
            in_run = false;
        } else if !in_run {
            slug.push('-');
            in_run = true;
        }
    }
    let trimmed = slug.trim_matches('-');
    let base = if trimmed.is_empty() {
        "basket"
    } else {
        trimmed
    };
    base.chars().take(SLUG_MAX).collect()
}

async fn slug_taken(tx: &mut Tx, candidate: &str) -> Result<bool, Problem> {
    let found: Option<i64> = sqlx::query_scalar("SELECT id FROM cb_basket WHERE slug = $1")
        .bind(candidate)
        .fetch_optional(&mut **tx)
        .await
        .map_err(db)?;
    Ok(found.is_some())
}

/// `slug_for(session, name)`: a collision-free catalog slug (`_unique_slug(_slugify(name))`).
///
/// # Errors
/// 500 when no slug is free within 10,000 tries, or the database fails.
pub(crate) async fn slug_for(tx: &mut Tx, name: &str) -> Result<String, Problem> {
    let base = slugify(name);
    let candidate: String = base.chars().take(SLUG_MAX).collect();
    if !slug_taken(tx, &candidate).await? {
        return Ok(candidate);
    }
    for suffix in 2..10_000_usize {
        let keep = SLUG_MAX.saturating_sub(suffix.to_string().len() + 1).max(1);
        let trimmed: String = base.chars().take(keep).collect();
        let candidate = format!("{trimmed}-{suffix}");
        if !slug_taken(tx, &candidate).await? {
            return Ok(candidate);
        }
    }
    Err(Problem::new(
        ProblemType::InternalError,
        "could not allocate a unique basket slug",
    ))
}

/// `ConstituentOut`.
#[derive(Debug, Clone, Serialize)]
struct ConstituentOut {
    symbol: String,
    instrument_id: i64,
    weight: String,
    segment: &'static str,
}

/// `CreateBasketOut`.
#[derive(Debug, Clone, Serialize)]
struct CreateBasketOut {
    id: i64,
    slug: String,
    name: String,
    visibility: String,
    #[serde(rename = "type")]
    kind: String,
    source: String,
    version_no: i64,
    label: String,
    constituents: Vec<ConstituentOut>,
}

/// The curated manager's id (`maulik`), or the seed-missing 500.
pub(crate) async fn curated_manager_id(tx: &mut Tx) -> Result<i64, Problem> {
    let id: Option<i64> = sqlx::query_scalar("SELECT id FROM cb_manager WHERE slug = $1")
        .bind(MANAGER_SLUG_MAULIK)
        .fetch_optional(&mut **tx)
        .await
        .map_err(db)?;
    id.ok_or_else(|| {
        Problem::new(
            ProblemType::InternalError,
            "curated manager seed is missing",
        )
    })
}

/// `POST /cb/baskets` (`create_private_basket`): 201.
///
/// # Errors
/// 400 (body, duplicate symbols, weights that do not reconcile), 401, the sole-tenant refusals,
/// 404 (unknown symbols), 500.
pub async fn create_private_basket(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &create_in(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    scoped_sole_user_id(&state, principal.user_id, None).await?;
    let items = body.get("constituents").items();
    let symbols: Vec<String> = items
        .iter()
        .map(|c| {
            c.get("symbol")
                .str()
                .unwrap_or_default()
                .trim_matches(py_space)
                .to_uppercase()
        })
        .collect();
    let weights: Vec<WideDecimal> = items
        .iter()
        .map(|c| c.get("weight").dec().cloned().unwrap_or_default())
        .collect();
    if symbols.len() < MIN_CONSTITUENTS {
        return Err(invalid(format!(
            "at least {MIN_CONSTITUENTS} constituents are required"
        )));
    }
    let mut seen = HashSet::new();
    if !symbols.iter().all(|s| seen.insert(s.clone())) {
        return Err(invalid("duplicate symbols are not allowed"));
    }
    assert_weights_sum_to_one(&weights)?.map_err(invalid)?;
    let name = body.get("name").str().unwrap_or_default().to_owned();
    let description = body.get("description_md").str().map(str::to_owned);
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let rows = sqlx::query(
                "SELECT id, symbol FROM instrument WHERE upper(symbol) = ANY($1) \
                 AND is_active IS true",
            )
            .bind(&symbols)
            .fetch_all(&mut **tx)
            .await
            .map_err(db)?;
            let mut by_symbol: HashMap<String, i64> = HashMap::new();
            for r in &rows {
                let s: String = r.try_get("symbol").map_err(db)?;
                by_symbol.insert(s.to_uppercase(), r.try_get("id").map_err(db)?);
            }
            let missing: Vec<&str> = symbols
                .iter()
                .filter(|s| !by_symbol.contains_key(*s))
                .map(String::as_str)
                .collect();
            if !missing.is_empty() {
                return Err(Problem::new(
                    ProblemType::NotFound,
                    format!("unknown symbols: {}", missing.join(", ")),
                ));
            }
            let manager_id = curated_manager_id(tx).await?;
            let today = write_kit::now_utc().date_naive();
            let slug = slug_for(tx, &name).await?;
            let basket = sqlx::query(
                "INSERT INTO cb_basket (slug, name, manager_id, type, access, visibility, \
                 categories, description_md, rationale_md, rebalance_frequency, \
                 benchmark_instrument_id, launched_at, next_review_at, source, \
                 scan_strategy_key, archived_at) VALUES ($1, $2, $3, 'STOCK', 'FREE', 'PRIVATE', \
                 ARRAY['custom']::varchar[], $4, NULL, 'NEED_BASIS', NULL, $5, NULL, 'MANUAL', \
                 NULL, NULL) RETURNING id, slug, name, visibility, type, source",
            )
            .bind(&slug)
            .bind(name.trim_matches(py_space))
            .bind(manager_id)
            .bind(&description)
            .bind(today)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            let basket_id: i64 = basket.try_get("id").map_err(db)?;
            let version = sqlx::query(
                "INSERT INTO cb_basket_version (basket_id, version_no, effective_date, label, \
                 added_count, removed_count, notes_md, source_scan_run_id) \
                 VALUES ($1, 1, $2, 'GENESIS', $3, 0, 'User-created PRIVATE genesis version.', \
                 NULL) RETURNING id, version_no, label",
            )
            .bind(basket_id)
            .bind(today)
            .bind(i32::try_from(symbols.len()).map_err(|_| internal_error())?)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            let version_id: i64 = version.try_get("id").map_err(db)?;
            let mut constituents = Vec::with_capacity(symbols.len());
            let mut rows: Vec<(i64, String)> = Vec::with_capacity(symbols.len());
            for (symbol, weight) in symbols.iter().zip(&weights) {
                let instrument_id = by_symbol.get(symbol).copied().ok_or_else(internal_error)?;
                rows.push((instrument_id, numeric_text(weight)));
                constituents.push(ConstituentOut {
                    symbol: symbol.clone(),
                    instrument_id,
                    weight: weight.to_sci_string(),
                    segment: "Equity",
                });
            }
            // The ORM flushes the constituents as one multi-row INSERT (insertmanyvalues, pages
            // of 1,000): a weight the column cannot hold fails the whole statement when it is
            // planned, before any identity value is drawn. Row-at-a-time inserts would burn the
            // ids of the rows before it (found by the reviewer's `basket-create-overflow`).
            for page in rows.chunks(write_kit::INSERT_PAGE_ROWS) {
                let values = write_kit::values_rows(page.len(), 2, |p| {
                    format!("($1, ${}, 'Equity', (${}::text)::numeric)", p[0], p[1])
                });
                let sql = format!(
                    "INSERT INTO cb_constituent (version_id, instrument_id, segment, weight) \
                     VALUES {values}"
                );
                let mut q = sqlx::query(&sql).bind(version_id);
                for (instrument_id, weight) in page {
                    q = q.bind(*instrument_id).bind(weight);
                }
                q.execute(&mut **tx).await.map_err(db)?;
            }
            Ok(CreateBasketOut {
                id: basket_id,
                slug: basket.try_get("slug").map_err(db)?,
                name: basket.try_get("name").map_err(db)?,
                visibility: basket.try_get("visibility").map_err(db)?,
                kind: basket.try_get("type").map_err(db)?,
                source: basket.try_get("source").map_err(db)?,
                version_no: i64::from(version.try_get::<i32, _>("version_no").map_err(db)?),
                label: version.try_get("label").map_err(db)?,
                constituents,
            })
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn w(s: &str) -> WideDecimal {
        s.parse().unwrap()
    }

    // `packages/core/tests/test_curated_baskets.py` and `test_curated_create.py`.
    #[test]
    fn weights_reconcile_at_four_places() {
        assert!(
            assert_weights_sum_to_one(&[w("0.5"), w("0.5")])
                .unwrap()
                .is_ok()
        );
        assert!(
            assert_weights_sum_to_one(&[w("0.3333"), w("0.3333"), w("0.3334")])
                .unwrap()
                .is_ok()
        );
        // Each weight is rounded to four places first: 0.33333 x2 + 0.33334 sums to 0.9999.
        assert_eq!(
            assert_weights_sum_to_one(&[w("0.33333"), w("0.33333"), w("0.33334")])
                .unwrap()
                .unwrap_err(),
            "constituent weights must sum to 1.0000, got 0.9999 (delta 0.0001 exceeds tolerance \
             0.00005)"
        );
        assert_eq!(
            assert_weights_sum_to_one(&[w("0.5"), w("0.4")])
                .unwrap()
                .unwrap_err(),
            "constituent weights must sum to 1.0000, got 0.9000 (delta 0.1000 exceeds tolerance \
             0.00005)"
        );
        assert!(assert_weights_sum_to_one(&[]).unwrap().is_err());
    }

    #[test]
    fn slugs_are_python_slugify() {
        assert_eq!(slugify("  My Basket!! 2026 "), "my-basket-2026");
        assert_eq!(slugify("!!!"), "basket");
        assert_eq!(slugify("Énergie"), "nergie");
        assert_eq!(slugify(&"a".repeat(80)).len(), 64);
    }
}
