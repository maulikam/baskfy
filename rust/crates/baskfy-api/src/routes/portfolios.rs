//! ``/portfolios/*`` — docs/07 §"Portfolios & rebalance". Ports the GET routes of
//! `baskfy_api.routers.portfolios` (Python declares
//! `APIRouter(prefix="/portfolios", tags=["portfolios"])`).
//!
//! Ported: `GET /sample-csv` (anonymous), `GET ` (the caller's portfolios as a forest),
//! `GET /{portfolio_id}`, `GET /{portfolio_id}/holdings` (the subtree roll-up by broker),
//! `GET /{portfolio_id}/rebalances` and `GET /{portfolio_id}/rebalances/{rebalance_id}`.
//! **Not ported** (they write; Python keeps serving them): `POST `, `POST /import-csv`,
//! `PATCH /{id}`, `DELETE /{id}`, `PUT /{id}/holdings`, `POST /{id}/rebalance`.
//!
//! `{id}` is the numeric `portfolio.id`; not-yours is a 404, never a 403. This module also owns
//! the lane's shared private machinery, [`portfolio`] (the `routes/portfolio/` directory).

#[path = "portfolio/mod.rs"]
pub(crate) mod portfolio;

use std::collections::HashMap;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;

use self::portfolio::graph::{self, GraphError, GraphHolding, PortfolioNode, Totals, TreeNode};
use self::portfolio::{db, num, params};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, not_found};
use crate::query::{QueryParams, Validation};
use crate::state::AppState;
use crate::wire;

/// `SAMPLE_CSV` (`baskfy_core.portfolio_csv`).
pub const SAMPLE_CSV: &str = "symbol,quantity,avg_price\nCUPID,100,284.56\nHFCL,250,89.10\n\
                              WELCORP,40,912.40\nDIACABS,60,1130.00\nSANSERA,75,1420.25\n";
/// `SAMPLE_CSV_FILENAME`.
pub const SAMPLE_CSV_FILENAME: &str = "baskfy-portfolio-sample.csv";
/// `CSV_MEDIA_TYPE`.
pub const CSV_MEDIA_TYPE: &str = "text/csv; charset=utf-8";
/// `DEFAULT_PAGE_SIZE`.
const DEFAULT_PAGE_SIZE: i128 = 100;
/// `MAX_PAGE_SIZE`.
const MAX_PAGE_SIZE: i128 = 500;

/// `PortfolioNodeOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PortfolioNodeOut {
    /// `id`.
    pub id: i64,
    /// `name`.
    pub name: String,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
    /// `holdings_count` — this portfolio's rows, not its subtree's.
    pub holdings_count: i64,
    /// `parent_id`.
    pub parent_id: Option<i64>,
    /// `broker_account_id`.
    pub broker_account_id: Option<i64>,
    /// `depth`.
    pub depth: usize,
    /// `children`.
    pub children: Vec<PortfolioNodeOut>,
}

/// `PortfolioForestOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PortfolioForestOut {
    /// `data`: the roots.
    pub data: Vec<PortfolioNodeOut>,
    /// `orphans`.
    pub orphans: Vec<PortfolioNodeOut>,
}

/// `HoldingOut` (`baskfy_api.schemas`).
#[derive(Debug, Clone, Serialize)]
pub struct HoldingOut {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `quantity`.
    #[serde(serialize_with = "num::ser_opt")]
    pub quantity: Option<WideDecimal>,
    /// `avg_price`.
    #[serde(serialize_with = "num::ser_opt")]
    pub avg_price: Option<WideDecimal>,
    /// `added_on`.
    #[serde(serialize_with = "wire::ser::date")]
    pub added_on: NaiveDate,
    /// `delisted_on`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub delisted_on: Option<NaiveDate>,
}

/// `PortfolioDetailOut` (this router's: `PortfolioOut` plus the tree columns).
#[derive(Debug, Clone, Serialize)]
pub struct PortfolioDetailOut {
    /// `id`.
    pub id: i64,
    /// `name`.
    pub name: String,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
    /// `holdings`.
    pub holdings: Vec<HoldingOut>,
    /// `parent_id`.
    pub parent_id: Option<i64>,
    /// `broker_account_id`.
    pub broker_account_id: Option<i64>,
    /// `depth`.
    pub depth: usize,
    /// `child_ids`.
    pub child_ids: Vec<i64>,
}

/// `TotalsOut`.
#[derive(Debug, Clone, Serialize)]
pub struct TotalsOut {
    /// `quantity`.
    #[serde(serialize_with = "num::ser")]
    pub quantity: WideDecimal,
    /// `cost`: `quantity * avg_price` summed, unrounded.
    #[serde(serialize_with = "num::ser")]
    pub cost: WideDecimal,
    /// `holdings`.
    pub holdings: i64,
}

/// `BrokerLineOut`.
#[derive(Debug, Clone, Serialize)]
pub struct BrokerLineOut {
    /// `broker_account_id`.
    pub broker_account_id: i64,
    /// `broker_id`.
    pub broker_id: Option<String>,
    /// `label`.
    pub label: Option<String>,
    /// `totals`.
    pub totals: TotalsOut,
}

/// `SubtreeHoldingOut`.
#[derive(Debug, Clone, Serialize)]
pub struct SubtreeHoldingOut {
    /// `portfolio_id`.
    pub portfolio_id: i64,
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `broker_account_id`.
    pub broker_account_id: i64,
    /// `quantity`.
    #[serde(serialize_with = "num::ser_opt")]
    pub quantity: Option<WideDecimal>,
    /// `avg_price`.
    #[serde(serialize_with = "num::ser_opt")]
    pub avg_price: Option<WideDecimal>,
    /// `added_on`.
    #[serde(serialize_with = "wire::ser::date")]
    pub added_on: NaiveDate,
}

/// `PortfolioRollupOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PortfolioRollupOut {
    /// `portfolio_id`.
    pub portfolio_id: i64,
    /// `declared_broker_account_id`.
    pub declared_broker_account_id: Option<i64>,
    /// `subtree_portfolio_ids`.
    pub subtree_portfolio_ids: Vec<i64>,
    /// `by_broker`.
    pub by_broker: Vec<BrokerLineOut>,
    /// `unattributed`.
    pub unattributed: TotalsOut,
    /// `total`.
    pub total: TotalsOut,
    /// `spans_brokers`.
    pub spans_brokers: bool,
    /// `declaration_conflicts`.
    pub declaration_conflicts: bool,
    /// `rows`.
    pub rows: Vec<SubtreeHoldingOut>,
}

/// `RebalanceSummaryOut`.
#[derive(Debug, Clone, Serialize)]
pub struct RebalanceSummaryOut {
    /// `id`.
    pub id: i64,
    /// `as_of`.
    #[serde(serialize_with = "wire::ser::date")]
    pub as_of: NaiveDate,
    /// `data_version`.
    pub data_version: Option<i64>,
    /// `top_n`.
    pub top_n: i64,
    /// `hold_buffer`.
    pub hold_buffer: i64,
    /// `screen_name`.
    pub screen_name: Option<String>,
    /// `screen_public_id`.
    pub screen_public_id: Option<String>,
    /// `exits`.
    pub exits: usize,
    /// `inside_wrh`.
    pub inside_wrh: usize,
    /// `entries`.
    pub entries: usize,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
}

/// `RebalanceHistoryPage`.
#[derive(Debug, Clone, Serialize)]
pub struct RebalanceHistoryPage {
    /// `data`.
    pub data: Vec<RebalanceSummaryOut>,
    /// `next_cursor`.
    pub next_cursor: Option<String>,
}

fn refuse(e: &GraphError) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, e.to_string())
}

/// A path `int` (`portfolio_id: int`), validated with pydantic's rules.
fn path_int(v: &mut Validation, field: &str, raw: &str) -> Option<params::PyInt> {
    params::int_value(v, field, raw, params::IntBounds::NONE)
}

/// `_graph_rows`: the caller's portfolios newest first, and their creation times.
pub(crate) async fn graph_rows(
    state: &AppState,
    user_id: i64,
) -> Result<(Vec<PortfolioNode>, HashMap<i64, DateTime<Utc>>), Problem> {
    let rows = sqlx::query(
        "SELECT id, name, parent_id, broker_account_id, created_at FROM portfolio \
         WHERE user_id = $1 ORDER BY created_at DESC, id DESC",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut nodes = Vec::with_capacity(rows.len());
    let mut created = HashMap::with_capacity(rows.len());
    for row in &rows {
        let id: i64 = db::get(row, "id")?;
        created.insert(id, db::ts(row, "created_at")?);
        nodes.push(PortfolioNode {
            id,
            name: db::get(row, "name")?,
            parent_id: db::get(row, "parent_id")?,
            broker_account_id: db::get(row, "broker_account_id")?,
        });
    }
    Ok((nodes, created))
}

/// One `portfolio` row the caller owns.
pub(crate) struct OwnedPortfolio {
    pub(crate) id: i64,
    pub(crate) user_id: i64,
    pub(crate) name: String,
    pub(crate) created_at: DateTime<Utc>,
    pub(crate) parent_id: Option<i64>,
    pub(crate) broker_account_id: Option<i64>,
}

/// `_load` / `service.load_portfolio`: the caller's portfolio, or `No portfolio with id '7'.`
pub(crate) async fn load(
    state: &AppState,
    portfolio_id: params::PyInt,
    user_id: i64,
) -> Result<OwnedPortfolio, Problem> {
    let id = params::bigint(portfolio_id)?;
    let row = sqlx::query(
        "SELECT id, user_id, name, created_at, parent_id, broker_account_id FROM portfolio \
         WHERE id = $1 AND user_id = $2",
    )
    .bind(id)
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db::fail)?
    .ok_or_else(|| not_found("portfolio", &portfolio_id.to_string()))?;
    Ok(OwnedPortfolio {
        id: db::get(&row, "id")?,
        user_id: db::get(&row, "user_id")?,
        name: db::get(&row, "name")?,
        created_at: db::ts(&row, "created_at")?,
        parent_id: db::get(&row, "parent_id")?,
        broker_account_id: db::get(&row, "broker_account_id")?,
    })
}

fn node_out(
    node: &TreeNode,
    counts: &HashMap<i64, i64>,
    created: &HashMap<i64, DateTime<Utc>>,
) -> Result<PortfolioNodeOut, Problem> {
    let created_at = *created
        .get(&node.portfolio.id)
        .ok_or_else(crate::problems::internal_error)?;
    let mut children = Vec::with_capacity(node.children.len());
    for child in &node.children {
        children.push(node_out(child, counts, created)?);
    }
    Ok(PortfolioNodeOut {
        id: node.portfolio.id,
        name: node.portfolio.name.clone(),
        created_at,
        holdings_count: counts.get(&node.portfolio.id).copied().unwrap_or(0),
        parent_id: node.portfolio.parent_id,
        broker_account_id: node.portfolio.broker_account_id,
        depth: node.depth,
        children,
    })
}

async fn holdings_out(state: &AppState, portfolio_id: i64) -> Result<Vec<HoldingOut>, Problem> {
    let rows = sqlx::query(
        "SELECT h.instrument_id, h.quantity, h.avg_price, h.added_on, i.symbol, i.name, \
         i.delisted_on FROM portfolio_holding h JOIN instrument i ON i.id = h.instrument_id \
         WHERE h.portfolio_id = $1 ORDER BY i.symbol",
    )
    .bind(portfolio_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        out.push(HoldingOut {
            instrument_id: db::get(row, "instrument_id")?,
            symbol: db::get(row, "symbol")?,
            name: db::get(row, "name")?,
            quantity: db::opt_dec(row, "quantity")?,
            avg_price: db::opt_dec(row, "avg_price")?,
            added_on: db::date(row, "added_on")?,
            delisted_on: db::opt_date(row, "delisted_on")?,
        });
    }
    Ok(out)
}

/// `GET /portfolios/sample-csv`: the expected format, as a download. Anonymous on purpose.
pub async fn sample_csv() -> Response {
    let mut response = (StatusCode::OK, SAMPLE_CSV).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static(CSV_MEDIA_TYPE),
    );
    if let Ok(v) = HeaderValue::from_str(&format!("attachment; filename=\"{SAMPLE_CSV_FILENAME}\""))
    {
        headers.insert(header::CONTENT_DISPOSITION, v);
    }
    response
}

/// `GET /portfolios`: the caller's portfolios as a forest, roots newest first.
///
/// # Errors
/// 401 anonymous; 400 for a damaged stored tree (a cycle, a breach of the depth cap).
pub async fn list_portfolios(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let user_id = require_authenticated(&principal)?.require_user()?;
    let (nodes, created) = graph_rows(&state, user_id).await?;
    let count_rows = sqlx::query(
        "SELECT h.portfolio_id, count(*) AS holdings FROM portfolio_holding h \
         JOIN portfolio p ON p.id = h.portfolio_id WHERE p.user_id = $1 GROUP BY h.portfolio_id",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut counts = HashMap::new();
    for row in &count_rows {
        counts.insert(
            db::get::<i64>(row, "portfolio_id")?,
            db::get::<i64>(row, "holdings")?,
        );
    }
    let forest = graph::assemble_tree(&nodes).map_err(|e| refuse(&e))?;
    let mut data = Vec::new();
    for root in &forest.roots {
        data.push(node_out(root, &counts, &created)?);
    }
    let mut orphans = Vec::new();
    for orphan in &forest.orphans {
        orphans.push(node_out(&orphan.subtree, &counts, &created)?);
    }
    Ok(wire::json(&PortfolioForestOut { data, orphans }))
}

/// `GET /portfolios/{portfolio_id}`.
///
/// # Errors
/// 401 anonymous; 400 for a non-integer id or a damaged tree; 404 when not the caller's.
pub async fn get_portfolio(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let mut v = Validation::new();
    let portfolio_id = path_int(&mut v, "portfolio_id", &raw_id);
    v.finish()?;
    let user_id = principal.require_user()?;
    let portfolio = load(&state, portfolio_id.unwrap_or_default(), user_id).await?;
    let (nodes, _) = graph_rows(&state, portfolio.user_id).await?;
    let depth = graph::depth_of(&nodes, portfolio.id).map_err(|e| refuse(&e))?;
    let out = PortfolioDetailOut {
        id: portfolio.id,
        name: portfolio.name,
        created_at: portfolio.created_at,
        holdings: holdings_out(&state, portfolio.id).await?,
        parent_id: portfolio.parent_id,
        broker_account_id: portfolio.broker_account_id,
        depth,
        child_ids: nodes
            .iter()
            .filter(|n| n.parent_id == Some(portfolio.id))
            .map(|n| n.id)
            .collect(),
    };
    Ok(wire::json(&out))
}

fn totals_out(t: &Totals) -> TotalsOut {
    TotalsOut {
        quantity: t.quantity.clone(),
        cost: t.cost.clone(),
        holdings: t.holdings,
    }
}

/// `GET /portfolios/{portfolio_id}/holdings`: the subtree's holdings, split by broker account.
///
/// # Errors
/// 401 anonymous; 400 for a non-integer id or a damaged tree; 404 when not the caller's.
pub async fn holdings_rollup(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let mut v = Validation::new();
    let portfolio_id = path_int(&mut v, "portfolio_id", &raw_id);
    v.finish()?;
    let user_id = principal.require_user()?;
    let portfolio = load(&state, portfolio_id.unwrap_or_default(), user_id).await?;
    let (nodes, _) = graph_rows(&state, user_id).await?;
    let inside = graph::descendant_ids(&nodes, portfolio.id).map_err(|e| refuse(&e))?;
    let rows = sqlx::query(
        "SELECT h.portfolio_id, h.instrument_id, h.broker_account_id, h.quantity, h.avg_price, \
         h.added_on, i.symbol, i.name FROM portfolio_holding h \
         JOIN instrument i ON i.id = h.instrument_id WHERE h.portfolio_id = ANY($1) \
         ORDER BY h.portfolio_id, i.symbol, h.broker_account_id",
    )
    .bind(&inside)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut out_rows = Vec::with_capacity(rows.len());
    let mut holdings = Vec::with_capacity(rows.len());
    for row in &rows {
        let r = SubtreeHoldingOut {
            portfolio_id: db::get(row, "portfolio_id")?,
            instrument_id: db::get(row, "instrument_id")?,
            symbol: db::get(row, "symbol")?,
            name: db::get(row, "name")?,
            broker_account_id: db::get(row, "broker_account_id")?,
            quantity: db::opt_dec(row, "quantity")?,
            avg_price: db::opt_dec(row, "avg_price")?,
            added_on: db::date(row, "added_on")?,
        };
        holdings.push(GraphHolding {
            portfolio_id: r.portfolio_id,
            broker_account_id: Some(r.broker_account_id),
            quantity: r.quantity.clone().unwrap_or_else(num::zero),
            avg_price: r.avg_price.clone().unwrap_or_else(num::zero),
        });
        out_rows.push(r);
    }
    let rollup =
        graph::rollup_by_broker(&nodes, &holdings, portfolio.id).map_err(|e| refuse(&e))?;
    let account_rows =
        sqlx::query("SELECT id, broker_id, label FROM broker_account WHERE user_id = $1")
            .bind(user_id)
            .fetch_all(state.db.pool())
            .await
            .map_err(db::fail)?;
    let mut accounts: HashMap<i64, (String, String)> = HashMap::new();
    for row in &account_rows {
        accounts.insert(
            db::get(row, "id")?,
            (db::get(row, "broker_id")?, db::get(row, "label")?),
        );
    }
    let out = PortfolioRollupOut {
        portfolio_id: rollup.portfolio_id,
        declared_broker_account_id: rollup.declared_broker_account_id,
        subtree_portfolio_ids: inside,
        by_broker: rollup
            .by_broker
            .iter()
            .map(|line| {
                let named = accounts.get(&line.broker_account_id);
                BrokerLineOut {
                    broker_account_id: line.broker_account_id,
                    broker_id: named.map(|n| n.0.clone()),
                    label: named.map(|n| n.1.clone()),
                    totals: totals_out(&line.totals),
                }
            })
            .collect(),
        unattributed: totals_out(&rollup.unattributed),
        total: totals_out(&rollup.total),
        spans_brokers: rollup.spans_brokers(),
        declaration_conflicts: rollup.declaration_conflicts(),
        rows: out_rows,
    };
    Ok(wire::json(&out))
}

fn text_of(value: Option<&OrderedJson>) -> Option<String> {
    match value {
        Some(OrderedJson::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

fn count_of(value: Option<&OrderedJson>) -> usize {
    match value {
        Some(OrderedJson::Array(items)) => items.len(),
        _ => 0,
    }
}

/// `GET /portfolios/{portfolio_id}/rebalances?limit=&cursor=`: past rebalances, newest first.
///
/// # Errors
/// 401 anonymous; 400 for a bad id or `limit`; 404 when not the caller's or for a cursor that
/// is not an integer.
pub async fn list_rebalances(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(raw_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let portfolio_id = path_int(&mut v, "portfolio_id", &raw_id);
    let limit = params::query_int(
        &mut v,
        &q,
        "limit",
        DEFAULT_PAGE_SIZE,
        params::IntBounds {
            ge: Some(1),
            le: Some(MAX_PAGE_SIZE),
            gt: None,
        },
    );
    let cursor = q.get("cursor").map(str::to_owned);
    v.finish()?;
    let limit = limit.unwrap_or(DEFAULT_PAGE_SIZE);
    let portfolio = load(
        &state,
        portfolio_id.unwrap_or_default(),
        principal.require_user()?,
    )
    .await?;
    let cursor_id = match cursor.as_deref() {
        Some(c) => Some(params::bigint(params::python_int(c).ok_or_else(|| {
            Problem::new(
                ProblemType::NotFound,
                format!(
                    "{} is not a valid pagination cursor.",
                    baskfy_providers::py::repr_str(c)
                ),
            )
        })?)?),
        None => None,
    };
    let rows = sqlx::query(
        "SELECT id, as_of, data_version, top_n, hold_buffer, payload, created_at \
         FROM portfolio_rebalance WHERE portfolio_id = $1 AND ($2::bigint IS NULL OR id < $2) \
         ORDER BY id DESC LIMIT $3",
    )
    .bind(portfolio.id)
    .bind(cursor_id)
    .bind(i64::try_from(limit + 1).unwrap_or(i64::MAX))
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let page_len = usize::try_from(limit).unwrap_or(usize::MAX).min(rows.len());
    let mut data = Vec::with_capacity(page_len);
    for row in &rows[..page_len] {
        let payload = db::json(row, "payload")?;
        let screen = payload
            .get("screen")
            .filter(|s| matches!(s, OrderedJson::Object(_)));
        data.push(RebalanceSummaryOut {
            id: db::get(row, "id")?,
            as_of: db::date(row, "as_of")?,
            data_version: db::get(row, "data_version")?,
            top_n: i64::from(db::get::<i32>(row, "top_n")?),
            hold_buffer: i64::from(db::get::<i32>(row, "hold_buffer")?),
            screen_name: text_of(screen.and_then(|s| s.get("name"))),
            screen_public_id: text_of(screen.and_then(|s| s.get("public_id"))),
            exits: count_of(payload.get("exits")),
            inside_wrh: count_of(payload.get("inside_wrh")),
            entries: count_of(payload.get("entries")),
            created_at: db::ts(row, "created_at")?,
        });
    }
    let next_cursor = if rows.len() > page_len && page_len > 0 {
        data.last().map(|d| d.id.to_string())
    } else {
        None
    };
    Ok(wire::json(&RebalanceHistoryPage { data, next_cursor }))
}

/// `GET /portfolios/{portfolio_id}/rebalances/{rebalance_id}`: the stored payload, verbatim,
/// through `RebalanceOut.model_validate`.
///
/// # Errors
/// 401 anonymous; 400 for a bad id; 404 when either is not the caller's; 500 for a stored
/// payload `RebalanceOut` refuses (pydantic's `ValidationError` is unhandled in Python too).
pub async fn get_rebalance(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path((raw_portfolio, raw_rebalance)): Path<(String, String)>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let mut v = Validation::new();
    let portfolio_id = path_int(&mut v, "portfolio_id", &raw_portfolio);
    let rebalance_id = path_int(&mut v, "rebalance_id", &raw_rebalance);
    v.finish()?;
    let portfolio = load(
        &state,
        portfolio_id.unwrap_or_default(),
        principal.require_user()?,
    )
    .await?;
    let rebalance_id = rebalance_id.unwrap_or_default();
    let row = sqlx::query(
        "SELECT id, payload, created_at FROM portfolio_rebalance WHERE id = $1 \
         AND portfolio_id = $2",
    )
    .bind(params::bigint(rebalance_id)?)
    .bind(portfolio.id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db::fail)?
    .ok_or_else(|| not_found("rebalance", &rebalance_id.to_string()))?;
    let payload = db::json(&row, "payload")?;
    let out = portfolio::rebalance::rebalance_out(
        &payload,
        db::get(&row, "id")?,
        db::ts(&row, "created_at")?,
    )
    .map_err(|e| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", &format!("RebalanceOut validation: {e}"))],
        );
        crate::problems::internal_error()
    })?;
    Ok(wire::json(&out))
}

/// The GET routes of `baskfy_api.routers.portfolios`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/portfolios/sample-csv", get(sample_csv))
        .route("/portfolios", get(list_portfolios))
        .route("/portfolios/{portfolio_id}", get(get_portfolio))
        .route("/portfolios/{portfolio_id}/holdings", get(holdings_rollup))
        .route(
            "/portfolios/{portfolio_id}/rebalances",
            get(list_rebalances),
        )
        .route(
            "/portfolios/{portfolio_id}/rebalances/{rebalance_id}",
            get(get_rebalance),
        )
        // `POST /{id}/rebalance` writes and is not served here; a GET is Starlette's 405.
        // (`POST /import-csv` is left unregistered: a GET there reaches `/{portfolio_id}`.)
        .route(
            "/portfolios/{portfolio_id}/rebalance",
            axum::routing::MethodRouter::new(),
        )
}
