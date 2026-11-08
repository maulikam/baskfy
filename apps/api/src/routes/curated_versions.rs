//! Basket version list + diff between any two versions (AF I.1).
//!
//! Ports `baskfy_api.routers.curated_versions` (`APIRouter(tags=["explore"])`, mounted inside
//! `explore`'s router in Python). Read-only; both routes are ported:
//!
//! * `GET /explore/{slug}/versions` — every version of a visible basket, newest first, with its
//!   constituent count;
//! * `GET /explore/{slug}/versions/diff?from=&to=` — constituent weights compared between any two
//!   versions (added / removed / weight changed, by symbol).
//!
//! Visibility is the shared predicate (`curated_visibility.visible_baskets`: not archived,
//! `PUBLISHED`). Both need a signed-in account. **No order route** (law 2).

use std::collections::{BTreeSet, HashMap};

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_db::row;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::Serialize;
use sqlx::Row;

use super::screens::accounts::{self, db, dbe};
use super::watchlist::quantize_2;
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, bad_request, not_found};
use crate::query::{QueryParams, Validation};
use crate::state::AppState;
use crate::wire;

/// `SLUG_MAX`.
pub const SLUG_MAX: usize = 120;

/// `VersionSummaryOut`.
#[derive(Debug, Clone, Serialize)]
pub struct VersionSummaryOut {
    /// `version_no`.
    pub version_no: i64,
    /// `effective_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub effective_date: NaiveDate,
    /// `label`.
    pub label: String,
    /// `added_count`.
    pub added_count: i64,
    /// `removed_count`.
    pub removed_count: i64,
    /// `notes_md`.
    pub notes_md: Option<String>,
    /// `constituent_count`.
    pub constituent_count: i64,
}

/// `VersionsOut`.
#[derive(Debug, Clone, Serialize)]
pub struct VersionsOut {
    /// `slug`.
    pub slug: String,
    /// `versions`.
    pub versions: Vec<VersionSummaryOut>,
    /// `count`.
    pub count: i64,
}

/// `DiffLineOut`.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DiffLineOut {
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: Option<String>,
    /// `change`: `added` | `removed` | `weight_changed`.
    pub change: &'static str,
    /// `weight_from`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub weight_from: Option<Decimal>,
    /// `weight_to`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub weight_to: Option<Decimal>,
    /// `weight_pct_from`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub weight_pct_from: Option<Decimal>,
    /// `weight_pct_to`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub weight_pct_to: Option<Decimal>,
}

/// `VersionDiffOut`.
#[derive(Debug, Clone, Serialize)]
pub struct VersionDiffOut {
    /// `slug`.
    pub slug: String,
    /// `from_version`.
    pub from_version: i64,
    /// `to_version`.
    pub to_version: i64,
    /// `from_effective_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub from_effective_date: NaiveDate,
    /// `to_effective_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub to_effective_date: NaiveDate,
    /// `added`.
    pub added: Vec<DiffLineOut>,
    /// `removed`.
    pub removed: Vec<DiffLineOut>,
    /// `weight_changed`.
    pub weight_changed: Vec<DiffLineOut>,
    /// `unchanged_count`.
    pub unchanged_count: i64,
}

/// `{symbol: (name, weight)}` for one version.
pub type WeightMap = HashMap<String, (Option<String>, Decimal)>;

/// `_weight_pct`: `(weight * 100).quantize(Decimal("0.01"))`.
fn weight_pct(weight: Decimal) -> Decimal {
    quantize_2(weight * Decimal::ONE_HUNDRED)
}

/// `diff_weight_maps(from, to)`: the pure diff, symbols in sorted order.
#[must_use]
pub fn diff_weight_maps(
    from: &WeightMap,
    to: &WeightMap,
) -> (Vec<DiffLineOut>, Vec<DiffLineOut>, Vec<DiffLineOut>, i64) {
    let mut added = Vec::new();
    let mut removed = Vec::new();
    let mut changed = Vec::new();
    let mut unchanged = 0;
    let symbols: BTreeSet<&String> = from.keys().chain(to.keys()).collect();
    for symbol in symbols {
        match (from.get(symbol), to.get(symbol)) {
            (None, Some((name, weight))) => added.push(DiffLineOut {
                symbol: symbol.clone(),
                name: name.clone(),
                change: "added",
                weight_from: None,
                weight_to: Some(*weight),
                weight_pct_from: None,
                weight_pct_to: Some(weight_pct(*weight)),
            }),
            (Some((name, weight)), None) => removed.push(DiffLineOut {
                symbol: symbol.clone(),
                name: name.clone(),
                change: "removed",
                weight_from: Some(*weight),
                weight_to: None,
                weight_pct_from: Some(weight_pct(*weight)),
                weight_pct_to: None,
            }),
            (Some((name_from, w_from)), Some((name_to, w_to))) => {
                if w_from == w_to {
                    unchanged += 1;
                } else {
                    // `name_to or name_from`: an empty name is falsy too.
                    let name = match name_to {
                        Some(n) if !n.is_empty() => Some(n.clone()),
                        _ => name_from.clone(),
                    };
                    changed.push(DiffLineOut {
                        symbol: symbol.clone(),
                        name,
                        change: "weight_changed",
                        weight_from: Some(*w_from),
                        weight_to: Some(*w_to),
                        weight_pct_from: Some(weight_pct(*w_from)),
                        weight_pct_to: Some(weight_pct(*w_to)),
                    });
                }
            }
            (None, None) => {}
        }
    }
    (added, removed, changed, unchanged)
}

async fn visible_basket_id(state: &AppState, slug: &str) -> Result<i64, Problem> {
    let id: Option<i64> = sqlx::query_scalar(
        "SELECT id FROM cb_basket WHERE slug = $1 AND archived_at IS NULL \
         AND visibility = 'PUBLISHED'",
    )
    .bind(slug)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    id.ok_or_else(|| not_found("basket", slug))
}

fn i32_col(r: &sqlx::postgres::PgRow, c: &str) -> Result<i64, Problem> {
    r.try_get::<i32, _>(c).map(i64::from).map_err(db)
}

/// `GET /explore/{slug}/versions` (`list_basket_versions`).
///
/// # Errors
/// 401 for an anonymous caller; 400 for a slug over 120 characters; 404 for a basket that is
/// not visible; 500.
pub async fn list_basket_versions(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(slug): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let mut v = Validation::new();
    accounts::str_param(&mut v, "slug", Some(&slug), Some(1), Some(SLUG_MAX));
    v.finish()?;
    principal.require_user()?;
    let basket_id = visible_basket_id(&state, &slug).await?;
    let versions = sqlx::query(
        "SELECT id, version_no, effective_date, label, added_count, removed_count, notes_md \
         FROM cb_basket_version WHERE basket_id = $1 ORDER BY version_no DESC",
    )
    .bind(basket_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut ids = Vec::with_capacity(versions.len());
    for r in &versions {
        ids.push(r.try_get::<i64, _>("id").map_err(db)?);
    }
    let lookup = if ids.is_empty() { vec![0] } else { ids.clone() };
    let counts: HashMap<i64, i64> = sqlx::query(
        "SELECT version_id, count(*) AS n FROM cb_constituent WHERE version_id = ANY($1) \
         GROUP BY version_id",
    )
    .bind(&lookup)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?
    .iter()
    .map(|r| {
        Ok((
            r.try_get::<i64, _>("version_id").map_err(db)?,
            r.try_get::<i64, _>("n").map_err(db)?,
        ))
    })
    .collect::<Result<_, Problem>>()?;
    let mut items = Vec::with_capacity(versions.len());
    for (r, id) in versions.iter().zip(&ids) {
        items.push(VersionSummaryOut {
            version_no: i32_col(r, "version_no")?,
            effective_date: row::date(r, "effective_date").map_err(|e| dbe(&e))?,
            label: r.try_get("label").map_err(db)?,
            added_count: i32_col(r, "added_count")?,
            removed_count: i32_col(r, "removed_count")?,
            notes_md: r.try_get("notes_md").map_err(db)?,
            constituent_count: counts.get(id).copied().unwrap_or(0),
        });
    }
    let count = i64::try_from(items.len()).unwrap_or(i64::MAX);
    Ok(wire::json(&VersionsOut {
        slug,
        versions: items,
        count,
    }))
}

async fn weights_for_version(state: &AppState, version_id: i64) -> Result<WeightMap, Problem> {
    let rows = sqlx::query(
        "SELECT i.symbol, i.name, c.weight FROM cb_constituent c \
         JOIN instrument i ON i.id = c.instrument_id WHERE c.version_id = $1",
    )
    .bind(version_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut out = WeightMap::new();
    for r in &rows {
        out.insert(
            r.try_get("symbol").map_err(db)?,
            (
                r.try_get("name").map_err(db)?,
                row::decimal(r, "weight").map_err(|e| dbe(&e))?,
            ),
        );
    }
    Ok(out)
}

/// `GET /explore/{slug}/versions/diff?from=&to=` (`diff_basket_versions`).
///
/// # Errors
/// 401 for an anonymous caller; 400 for a bad slug or a missing/non-positive `from`/`to`;
/// 400 `bad-request` when they are equal; 404 for an invisible basket or a missing version; 500.
pub async fn diff_basket_versions(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(slug): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    accounts::str_param(&mut v, "slug", Some(&slug), Some(1), Some(SLUG_MAX));
    let from_raw = v.required(&q, "from");
    let from = accounts::int_param(&mut v, "from", from_raw, Some(1), None);
    let to_raw = v.required(&q, "to");
    let to = accounts::int_param(&mut v, "to", to_raw, Some(1), None);
    v.finish()?;
    let (Some(from), Some(to)) = (from, to) else {
        return Err(crate::problems::internal_error());
    };
    principal.require_user()?;
    if from == to {
        return Err(bad_request(
            "from and to must name different versions",
            None,
        ));
    }
    let basket_id = visible_basket_id(&state, &slug).await?;
    let load = |version_no: i128| {
        let state = state.clone();
        let slug = slug.clone();
        async move {
            // Beyond int4 no row can match (asyncpg would refuse the bind: a 500 in Python).
            let n = i32::try_from(version_no).map_err(|_| crate::problems::internal_error())?;
            let found = sqlx::query(
                "SELECT id, effective_date FROM cb_basket_version WHERE basket_id = $1 \
                 AND version_no = $2",
            )
            .bind(basket_id)
            .bind(n)
            .fetch_optional(state.db.pool())
            .await
            .map_err(db)?;
            match found {
                Some(r) => Ok::<_, Problem>((
                    r.try_get::<i64, _>("id").map_err(db)?,
                    row::date(&r, "effective_date").map_err(|e| dbe(&e))?,
                )),
                None => Err(not_found("basket version", &format!("{slug}#{version_no}"))),
            }
        }
    };
    let (left_id, left_date) = load(from).await?;
    let (right_id, right_date) = load(to).await?;
    let from_map = weights_for_version(&state, left_id).await?;
    let to_map = weights_for_version(&state, right_id).await?;
    let (added, removed, weight_changed, unchanged_count) = diff_weight_maps(&from_map, &to_map);
    Ok(wire::json(&VersionDiffOut {
        slug,
        from_version: i64::try_from(from).unwrap_or(i64::MAX),
        to_version: i64::try_from(to).unwrap_or(i64::MAX),
        from_effective_date: left_date,
        to_effective_date: right_date,
        added,
        removed,
        weight_changed,
        unchanged_count,
    }))
}

/// The version routes, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/explore/{slug}/versions", get(list_basket_versions))
        .route("/explore/{slug}/versions/diff", get(diff_basket_versions))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use std::str::FromStr;

    #[test]
    fn the_diff_reports_added_removed_and_changed_by_symbol() {
        // Ports test_curated_versions.py's pure-diff assertion: the old no-diff behaviour fails.
        let d = |s: &str| Decimal::from_str(s).unwrap();
        let from: WeightMap = [
            ("A".to_owned(), (Some("Alpha".to_owned()), d("0.5"))),
            ("B".to_owned(), (Some("Beta".to_owned()), d("0.5"))),
        ]
        .into();
        let to: WeightMap = [
            ("A".to_owned(), (Some("Alpha".to_owned()), d("0.4"))),
            ("C".to_owned(), (None, d("0.6"))),
        ]
        .into();
        let (added, removed, changed, unchanged) = diff_weight_maps(&from, &to);
        assert_eq!(added.len(), 1);
        assert_eq!(added[0].symbol, "C");
        assert_eq!(added[0].weight_pct_to.unwrap().to_string(), "60.00");
        assert_eq!(removed[0].symbol, "B");
        assert_eq!(changed[0].weight_pct_from.unwrap().to_string(), "50.00");
        assert_eq!(unchanged, 0);
    }
}
