//! Investment reads and the fee ledger (T7.2 b). No order path.
//!
//! Ports the **GET** routes of `baskfy_api.routers.curated_investments`
//! (`APIRouter(tags=["curated-investments"])`):
//!
//! * `GET /cb/investments?portfolio_id=&unassigned=` — the sole tenant's book, with each row's
//!   investor snapshot, the open pending actions, and the unfiled/broker-conflict counts;
//! * `GET /cb/investments/{investment_id}` — one investment with holdings and order batches;
//! * `GET /cb/fees` — the accrued (uncollected) fee ledger.
//!
//! Holdings are marked **at cost** unless a live Kite mark exists; this wave has no broker
//! session reader, so the overlay is empty — Python's own answer without a session
//! ([`accounts::investments::live_prices_by_instrument`]) — and `marked_at_cost` says so.
//!
//! The writes (`POST /cb/investments/mark`, `PUT`/`DELETE /cb/investments/{investment_id}/portfolio`)
//! are ported at wave 4c ([`writes`]). Marking an investment never reaches a broker, and nothing
//! here does (law 2).

mod writes;

use std::collections::{HashMap, HashSet};

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::{get, post, put};
use baskfy_core::decimal::WideDecimal;
use baskfy_db::row;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::Serialize;
use sqlx::Row;

use super::screens::accounts::investments::{
    Batch, Holding, INVESTMENT_COLUMNS, InvestmentRow, SnapshotOut, batches_of, buy_amounts,
    holdings_of, investment_row, json_equals_int, json_int, list_open_pending,
    live_prices_by_instrument, load_investment_for_user, marked_prices, pending_body,
    pending_title, snapshot,
};
use super::screens::accounts::{self, db, dbe, scoped_sole_user_id};
use crate::auth::{Principal, require_authenticated};
use crate::entitlements::utc_now;
use crate::problems::{Problem, ProblemType, internal_error, not_found};
use crate::query::{QueryParams, Validation};
use crate::state::AppState;
use crate::wire;

/// `PendingBriefOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PendingBriefOut {
    /// `id`.
    pub id: i64,
    /// `type`.
    #[serde(rename = "type")]
    pub kind: String,
    /// `title`.
    pub title: String,
    /// `body`.
    pub body: Option<String>,
}

/// `InvestmentRowOut`.
#[derive(Debug, Clone, Serialize)]
pub struct InvestmentRowOut {
    /// `id`.
    pub id: i64,
    /// `basket_slug`.
    pub basket_slug: String,
    /// `basket_name`.
    pub basket_name: String,
    /// `status`.
    pub status: String,
    /// `invested_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub invested_at: Option<DateTime<Utc>>,
    /// `last_invested_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub last_invested_at: Option<DateTime<Utc>>,
    /// `days_since_last_investment`.
    pub days_since_last_investment: Option<i64>,
    /// `rebalance_pending`.
    pub rebalance_pending: bool,
    /// `snapshot`.
    pub snapshot: Option<SnapshotOut>,
    /// `basket_source`.
    pub basket_source: String,
    /// `visibility`.
    pub visibility: String,
    /// `portfolio_id`.
    pub portfolio_id: Option<i64>,
    /// `portfolio_name`.
    pub portfolio_name: Option<String>,
    /// `broker_conflict`.
    pub broker_conflict: bool,
}

/// `InvestmentListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct InvestmentListOut {
    /// `items`.
    pub items: Vec<InvestmentRowOut>,
    /// `total`.
    pub total: i64,
    /// `net_worth`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub net_worth: Option<Decimal>,
    /// `pending_actions`.
    pub pending_actions: Vec<PendingBriefOut>,
    /// `unassigned_count`.
    pub unassigned_count: i64,
    /// `broker_conflicts`.
    pub broker_conflicts: i64,
}

/// `InvestmentHoldingOut`.
#[derive(Debug, Clone, Serialize)]
pub struct InvestmentHoldingOut {
    /// `symbol`.
    pub symbol: String,
    /// `qty`.
    #[serde(serialize_with = "wire::ser::decimal")]
    pub qty: Decimal,
    /// `weight`: `str(Decimal)` of a 28-digit Python quotient (kept wide, R1.3).
    pub weight: Option<String>,
    /// `returns_pct` (always `None`).
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub returns_pct: Option<Decimal>,
}

/// `BatchOut`.
#[derive(Debug, Clone, Serialize)]
pub struct BatchOut {
    /// `id`.
    pub id: i64,
    /// `kind`.
    pub kind: String,
    /// `status`.
    pub status: String,
    /// `desk_plan_id`.
    pub desk_plan_id: Option<String>,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub created_at: Option<DateTime<Utc>>,
}

/// `InvestmentDetailOut(InvestmentRowOut)`: the row's fields, then `holdings`, `orders`.
#[derive(Debug, Clone, Serialize)]
pub struct InvestmentDetailOut {
    /// The inherited `InvestmentRowOut` fields, in order.
    #[serde(flatten)]
    pub row: InvestmentRowOut,
    /// `holdings`.
    pub holdings: Vec<InvestmentHoldingOut>,
    /// `orders`.
    pub orders: Vec<BatchOut>,
}

/// `FeeRowOut`.
#[derive(Debug, Clone, Serialize)]
pub struct FeeRowOut {
    /// `id`.
    pub id: i64,
    /// `kind`.
    pub kind: String,
    /// `base_fee`.
    #[serde(serialize_with = "wire::ser::decimal")]
    pub base_fee: Decimal,
    /// `gst`.
    #[serde(serialize_with = "wire::ser::decimal")]
    pub gst: Decimal,
    /// `total`.
    #[serde(serialize_with = "wire::ser::decimal")]
    pub total: Decimal,
    /// `accrued_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub accrued_at: DateTime<Utc>,
    /// `collected`.
    pub collected: bool,
    /// `basket_name`.
    pub basket_name: Option<String>,
}

/// `FeeLedgerOut`.
#[derive(Debug, Clone, Serialize)]
pub struct FeeLedgerOut {
    /// `items`.
    pub items: Vec<FeeRowOut>,
    /// `accrued_total`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub accrued_total: Option<Decimal>,
    /// `collected_total`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub collected_total: Option<Decimal>,
}

/// A `cb_basket` row as the rows need it.
#[derive(Debug, Clone)]
struct BasketRow {
    slug: String,
    name: String,
    source: String,
    visibility: String,
}

/// A `portfolio` row as the rows need it.
#[derive(Debug, Clone)]
struct PortfolioRow {
    name: String,
    broker_account_id: Option<i64>,
}

async fn baskets(state: &AppState, ids: &[i64]) -> Result<HashMap<i64, BasketRow>, Problem> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows =
        sqlx::query("SELECT id, slug, name, source, visibility FROM cb_basket WHERE id = ANY($1)")
            .bind(ids)
            .fetch_all(state.db.pool())
            .await
            .map_err(db)?;
    rows.iter()
        .map(|r| {
            Ok((
                r.try_get("id").map_err(db)?,
                BasketRow {
                    slug: r.try_get("slug").map_err(db)?,
                    name: r.try_get("name").map_err(db)?,
                    source: r.try_get("source").map_err(db)?,
                    visibility: r.try_get("visibility").map_err(db)?,
                },
            ))
        })
        .collect()
}

async fn portfolios(
    state: &AppState,
    ids: &[i64],
    user_id: i64,
) -> Result<HashMap<i64, PortfolioRow>, Problem> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = sqlx::query(
        "SELECT id, name, broker_account_id FROM portfolio WHERE id = ANY($1) AND user_id = $2",
    )
    .bind(ids)
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    rows.iter()
        .map(|r| {
            Ok((
                r.try_get("id").map_err(db)?,
                PortfolioRow {
                    name: r.try_get("name").map_err(db)?,
                    broker_account_id: r.try_get("broker_account_id").map_err(db)?,
                },
            ))
        })
        .collect()
}

/// `_broker_conflict`: only a portfolio that names exactly one broker account can be
/// contradicted.
fn broker_conflict(investment_broker_account_id: i64, portfolio: Option<&PortfolioRow>) -> bool {
    portfolio
        .and_then(|p| p.broker_account_id)
        .is_some_and(|declared| declared != investment_broker_account_id)
}

/// `_RowContext`: everything a row needs that is not on the investment or its basket.
struct RowCtx<'a> {
    holdings: &'a [Holding],
    buy_amounts: &'a [Decimal],
    rebalance_pending: bool,
    portfolio: Option<&'a PortfolioRow>,
    now: DateTime<Utc>,
}

/// `_row_out(inv, basket, ctx, live_prices=)`.
fn row_out(
    inv: &InvestmentRow,
    basket: &BasketRow,
    ctx: &RowCtx<'_>,
    live: &HashMap<i64, Decimal>,
) -> Result<InvestmentRowOut, Problem> {
    let (holdings, now, portfolio, rebalance_pending) =
        (ctx.holdings, ctx.now, ctx.portfolio, ctx.rebalance_pending);
    let (prices, marked_at_cost) = marked_prices(holdings, live);
    let snap = snapshot(
        ctx.buy_amounts,
        holdings,
        &prices,
        inv.created_at.date_naive(),
        now.date_naive(),
        marked_at_cost,
    )?;
    Ok(InvestmentRowOut {
        id: inv.id,
        basket_slug: basket.slug.clone(),
        basket_name: basket.name.clone(),
        status: inv.status.clone(),
        invested_at: Some(inv.created_at),
        last_invested_at: inv.last_invested_at,
        days_since_last_investment: inv
            .last_invested_at
            .map(|w| (now.date_naive() - w.date_naive()).num_days()),
        rebalance_pending,
        snapshot: Some(snap),
        basket_source: basket.source.clone(),
        visibility: basket.visibility.clone(),
        portfolio_id: inv.portfolio_id,
        portfolio_name: portfolio.map(|p| p.name.clone()),
        broker_conflict: broker_conflict(inv.broker_account_id, portfolio),
    })
}

/// `GET /cb/investments?portfolio_id=&unassigned=` (`list_investments`).
///
/// # Errors
/// 401 for an anonymous caller; 400 for a bad parameter or both filters at once; the
/// sole-tenant refusals; 404 for a portfolio that is not the tenant's; 500.
pub async fn list_investments(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let portfolio_id =
        accounts::int_param(&mut v, "portfolio_id", q.get("portfolio_id"), Some(1), None);
    let unassigned = accounts::bool_param(&mut v, "unassigned", q.get("unassigned"));
    v.finish()?;
    let unassigned = unassigned.unwrap_or(false);
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    if portfolio_id.is_some() && unassigned {
        return Err(Problem::new(
            ProblemType::InvalidScreenDefinition,
            "portfolio_id and unassigned=true select different sets; send one or neither.",
        ));
    }
    let portfolio_id = match portfolio_id {
        Some(n) => Some(i64::try_from(n).map_err(|_| internal_error())?),
        None => None,
    };
    if let Some(pid) = portfolio_id {
        let owned: Option<i64> =
            sqlx::query_scalar("SELECT id FROM portfolio WHERE id = $1 AND user_id = $2")
                .bind(pid)
                .bind(user_id)
                .fetch_optional(state.db.pool())
                .await
                .map_err(db)?;
        if owned.is_none() {
            return Err(not_found("portfolio", &pid.to_string()));
        }
    }
    let now = utc_now();
    let base = format!("SELECT {INVESTMENT_COLUMNS} FROM cb_investment WHERE user_id = $1");
    let rows = match (portfolio_id, unassigned) {
        (Some(pid), _) => {
            sqlx::query(&format!("{base} AND portfolio_id = $2"))
                .bind(user_id)
                .bind(pid)
                .fetch_all(state.db.pool())
                .await
        }
        (None, true) => {
            sqlx::query(&format!("{base} AND portfolio_id IS NULL"))
                .bind(user_id)
                .fetch_all(state.db.pool())
                .await
        }
        (None, false) => {
            sqlx::query(&base)
                .bind(user_id)
                .fetch_all(state.db.pool())
                .await
        }
    }
    .map_err(db)?;
    let rows = rows
        .iter()
        .map(investment_row)
        .collect::<Result<Vec<_>, _>>()?;
    let linked: Vec<i64> = rows
        .iter()
        .filter_map(|r| r.portfolio_id)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    let portfolios = portfolios(&state, &linked, user_id).await?;
    let pending = list_open_pending(&state, user_id).await?;
    let pending_actions: Vec<PendingBriefOut> = pending
        .iter()
        .map(|p| PendingBriefOut {
            id: p.id,
            kind: p.kind.clone(),
            title: pending_title(p),
            body: pending_body(p),
        })
        .collect();
    let rebalance_baskets: HashSet<i64> = pending
        .iter()
        .filter(|p| p.kind == "REBALANCE_AVAILABLE")
        .filter_map(|p| json_int(p.payload.get("basket_id")))
        .collect();
    let inv_ids: Vec<i64> = rows.iter().map(|r| r.id).collect();
    let holdings = holdings_of(&state, &inv_ids).await?;
    let batches = batches_of(&state, &inv_ids, "").await?;
    let basket_ids: Vec<i64> = rows
        .iter()
        .map(|r| r.basket_id)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    let baskets = baskets(&state, &basket_ids).await?;
    let instrument_ids: Vec<i64> = holdings.iter().map(|(_, h)| h.instrument_id).collect();
    let live = live_prices_by_instrument(&state, &instrument_ids).await?;

    let mut items = Vec::new();
    let mut net = WideDecimal::zero();
    let mut any_value = false;
    for inv in &rows {
        let Some(basket) = baskets.get(&inv.basket_id) else {
            continue;
        };
        let held: Vec<Holding> = holdings
            .iter()
            .filter(|(id, _)| *id == inv.id)
            .map(|(_, h)| *h)
            .collect();
        let own: Vec<&Batch> = batches
            .iter()
            .filter(|b| b.investment_id == inv.id)
            .collect();
        let buy = buy_amounts(&own);
        let ctx = RowCtx {
            holdings: &held,
            buy_amounts: &buy,
            rebalance_pending: rebalance_baskets.contains(&inv.basket_id),
            portfolio: inv.portfolio_id.and_then(|p| portfolios.get(&p)),
            now,
        };
        let row = row_out(inv, basket, &ctx, &live)?;
        if let Some(s) = &row.snapshot {
            net = net.add(&WideDecimal::from(s.current_value));
            any_value = true;
        }
        items.push(row);
    }
    let net_worth = if any_value {
        Some(net.try_to_decimal().map_err(|_| internal_error())?)
    } else {
        None
    };
    let unassigned_count = i64::try_from(items.iter().filter(|r| r.portfolio_id.is_none()).count())
        .unwrap_or(i64::MAX);
    let broker_conflicts =
        i64::try_from(items.iter().filter(|r| r.broker_conflict).count()).unwrap_or(i64::MAX);
    Ok(wire::json(&InvestmentListOut {
        total: i64::try_from(items.len()).unwrap_or(i64::MAX),
        items,
        net_worth,
        pending_actions,
        unassigned_count,
        broker_conflicts,
    }))
}

/// The `investment_id: int` path parameter, validated after the dependencies (FastAPI's order).
///
/// `None` is an integer beyond bigint: it validates, but cannot be bound (asyncpg's
/// `DataError`), so [`load_investment_for_user`] answers the 500 only *after* the sole-tenant
/// gate has run — Python's order (a 503 or a `not-the-sole-tenant` 404 wins over the 500).
///
/// # Errors
/// 400 when it is not an integer.
pub fn investment_id_param(raw: &str) -> Result<Option<i64>, Problem> {
    let mut v = Validation::new();
    let n = accounts::int_param(&mut v, "investment_id", Some(raw), None, None);
    v.finish()?;
    Ok(n.and_then(|n| i64::try_from(n).ok()))
}

/// `GET /cb/investments/{investment_id}` (`get_investment`).
///
/// # Errors
/// 401 for an anonymous caller; 400 for a non-integer id; the sole-tenant refusals; 404; 500.
pub async fn get_investment(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let investment_id = investment_id_param(&raw_id)?;
    let inv = load_investment_for_user(&state, principal.user_id, investment_id).await?;
    let basket = baskets(&state, &[inv.basket_id]).await?;
    let Some(basket) = basket.get(&inv.basket_id) else {
        // `RuntimeError("investment references a missing basket")`: the catch-all.
        return Err(internal_error());
    };
    let now = utc_now();
    let held: Vec<Holding> = holdings_of(&state, &[inv.id])
        .await?
        .into_iter()
        .map(|(_, h)| h)
        .collect();
    let batches = batches_of(&state, &[inv.id], "ORDER BY id DESC").await?;
    let own: Vec<&Batch> = batches.iter().collect();
    let pending = list_open_pending(&state, inv.user_id).await?;
    let rebalance_pending = pending.iter().any(|a| {
        a.kind == "REBALANCE_AVAILABLE"
            && matches!(a.payload, baskfy_db::OrderedJson::Object(_))
            && json_equals_int(a.payload.get("basket_id"), inv.basket_id)
    });
    let instrument_ids: Vec<i64> = held.iter().map(|h| h.instrument_id).collect();
    let live = live_prices_by_instrument(&state, &instrument_ids).await?;
    let portfolio = match inv.portfolio_id {
        Some(pid) => portfolios(&state, &[pid], inv.user_id).await?.remove(&pid),
        None => None,
    };
    let buy = buy_amounts(&own);
    let ctx = RowCtx {
        holdings: &held,
        buy_amounts: &buy,
        rebalance_pending,
        portfolio: portfolio.as_ref(),
        now,
    };
    let row = row_out(&inv, basket, &ctx, &live)?;
    let deployed = held.iter().fold(WideDecimal::zero(), |acc, h| {
        acc.add(&WideDecimal::from(h.qty).mul(&WideDecimal::from(h.avg_price)))
    });
    let mut symbols: HashMap<i64, String> = HashMap::new();
    if !held.is_empty() {
        for r in sqlx::query("SELECT id, symbol FROM instrument WHERE id = ANY($1)")
            .bind(&instrument_ids)
            .fetch_all(state.db.pool())
            .await
            .map_err(db)?
        {
            symbols.insert(
                r.try_get("id").map_err(db)?,
                r.try_get("symbol").map_err(db)?,
            );
        }
    }
    let mut holdings_out = Vec::with_capacity(held.len());
    for h in &held {
        let weight = if deployed.is_zero() {
            None
        } else {
            let cost = WideDecimal::from(h.qty).mul(&WideDecimal::from(h.avg_price));
            Some(
                cost.div(&deployed)
                    .map_err(|_| internal_error())?
                    .to_sci_string(),
            )
        };
        holdings_out.push(InvestmentHoldingOut {
            symbol: symbols
                .get(&h.instrument_id)
                .cloned()
                .unwrap_or_else(|| h.instrument_id.to_string()),
            qty: h.qty,
            weight,
            returns_pct: None,
        });
    }
    let orders = batches
        .iter()
        .map(|b| BatchOut {
            id: b.id,
            kind: b.kind.clone(),
            status: b.status.clone(),
            desk_plan_id: b.desk_plan_id.clone(),
            created_at: Some(b.created_at),
        })
        .collect();
    Ok(wire::json(&InvestmentDetailOut {
        row,
        holdings: holdings_out,
        orders,
    }))
}

/// `GET /cb/fees` (`list_fees`): the sole tenant's fee rows, newest accrual first; the totals are
/// `None` when there are no rows.
///
/// # Errors
/// 401 for an anonymous caller; the sole-tenant refusals; 500.
pub async fn list_fees(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let rows = sqlx::query(
        "SELECT f.id, f.kind, f.base_fee, f.gst, f.total, f.accrued_at, f.collected, \
         b.name AS basket_name FROM cb_fee_ledger f \
         LEFT JOIN cb_order_batch o ON o.id = f.batch_id \
         LEFT JOIN cb_investment i ON i.id = o.investment_id \
         LEFT JOIN cb_basket b ON b.id = i.basket_id \
         WHERE f.user_id = $1 ORDER BY f.accrued_at DESC",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut items = Vec::with_capacity(rows.len());
    let mut accrued = WideDecimal::zero();
    let mut collected_total = WideDecimal::zero();
    for r in &rows {
        let total = row::decimal(r, "total").map_err(|e| dbe(&e))?;
        let collected: bool = r.try_get("collected").map_err(db)?;
        accrued = accrued.add(&WideDecimal::from(total));
        if collected {
            collected_total = collected_total.add(&WideDecimal::from(total));
        }
        items.push(FeeRowOut {
            id: r.try_get("id").map_err(db)?,
            kind: r.try_get("kind").map_err(db)?,
            base_fee: row::decimal(r, "base_fee").map_err(|e| dbe(&e))?,
            gst: row::decimal(r, "gst").map_err(|e| dbe(&e))?,
            total,
            accrued_at: row::timestamptz(r, "accrued_at").map_err(|e| dbe(&e))?,
            collected,
            basket_name: r.try_get("basket_name").map_err(db)?,
        });
    }
    let (accrued_total, collected_total) = if items.is_empty() {
        (None, None)
    } else {
        (
            Some(accrued.try_to_decimal().map_err(|_| internal_error())?),
            Some(
                collected_total
                    .try_to_decimal()
                    .map_err(|_| internal_error())?,
            ),
        )
    };
    Ok(wire::json(&FeeLedgerOut {
        items,
        accrued_total,
        collected_total,
    }))
}

/// The ported investment GET routes, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/cb/investments/{investment_id}/portfolio",
            put(writes::link_investment_to_portfolio)
                .delete(writes::unlink_investment_from_portfolio),
        )
        .route(
            "/cb/investments/mark",
            post(writes::mark_as_invested).get(writes::get_mark),
        )
        .route("/cb/investments", get(list_investments))
        .route("/cb/investments/{investment_id}", get(get_investment))
        .route("/cb/fees", get(list_fees))
}
