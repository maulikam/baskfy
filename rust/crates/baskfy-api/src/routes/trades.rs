//! Trade history — what is stored in `broker_trade`, newest first. Ports the GET route of
//! `baskfy_api.routers.trades` (Python declares `APIRouter(prefix="/trades", tags=["trades"])`).
//!
//! Ported: `GET /trades`. **Not ported** (they write `broker_trade` and holdings; Python keeps
//! serving them): `POST /trades/import`, `POST /trades/sync-today`. Bookkeeping only — nothing
//! here places, modifies or cancels an order (law 2).

use axum::Router;
use axum::extract::{Extension, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_core::decimal::WideDecimal;
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;

use crate::auth::{Principal, require_authenticated};
use crate::problems::Problem;
use crate::query::{QueryParams, Validation};
use crate::routes::portfolios::portfolio::{db, num, params};
use crate::state::AppState;
use crate::wire;

/// `BrokerTradeOut`.
#[derive(Debug, Clone, Serialize)]
pub struct BrokerTradeOut {
    /// `trade_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub trade_date: NaiveDate,
    /// `executed_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub executed_at: Option<DateTime<Utc>>,
    /// `symbol`.
    pub symbol: String,
    /// `exchange`.
    pub exchange: String,
    /// `side`.
    pub side: String,
    /// `quantity`.
    #[serde(serialize_with = "num::ser")]
    pub quantity: WideDecimal,
    /// `price`.
    #[serde(serialize_with = "num::ser")]
    pub price: WideDecimal,
    /// `value`: `(quantity * price).quantize(Decimal("0.01"))`.
    #[serde(serialize_with = "num::ser")]
    pub value: WideDecimal,
    /// `trade_id`.
    pub trade_id: String,
    /// `source`.
    pub source: String,
}

/// `BrokerTradesOut`.
#[derive(Debug, Clone, Serialize)]
pub struct BrokerTradesOut {
    /// `rows`.
    pub rows: Vec<BrokerTradeOut>,
    /// `total`.
    pub total: i64,
    /// `first_trade_on`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub first_trade_on: Option<NaiveDate>,
    /// `last_trade_on`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub last_trade_on: Option<NaiveDate>,
}

/// `GET /trades?limit=100&offset=0&symbol=`: the caller's stored fills.
///
/// # Errors
/// 401 anonymous; 400 for a bad `limit` (1..=500), `offset` (>= 0) or `symbol` (<= 40
/// characters); 500 for a database failure (an `offset` beyond `bigint` included).
pub async fn list_trades(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let limit = params::query_int(
        &mut v,
        &q,
        "limit",
        100,
        params::IntBounds {
            ge: Some(1),
            le: Some(500),
            gt: None,
        },
    );
    let offset = params::query_int(
        &mut v,
        &q,
        "offset",
        0,
        params::IntBounds {
            ge: Some(0),
            le: None,
            gt: None,
        },
    );
    let symbol = match q.get("symbol") {
        Some(s) => params::str_value(&mut v, "symbol", s, None, Some(40)).map(str::to_owned),
        None => None,
    };
    v.finish()?;
    let user_id = principal.require_user()?;
    let symbol = symbol
        .filter(|s| !s.is_empty())
        .map(|s| s.trim().to_uppercase());
    let pool = state.db.pool();
    // KNOWN BUG (DECISIONS-RS, api-portfolio P6), reproduced: Python's bounds query is
    // `select(count(), min(BrokerTrade.trade_date), max(...)).select_from(scope.subquery())`, and
    // SQLAlchemy compiles the two aggregates against the *table*, not the subquery — `FROM
    // (scope) AS anon_1, broker_trade`. So `total` is |caller's rows| x |every user's rows| and
    // the two dates span every user's trades whenever the caller has any. Fix in Python first
    // (aggregate the subquery's columns), then here.
    let bounds = sqlx::query(
        "SELECT count(*) AS n, min(bt.trade_date) AS first_on, max(bt.trade_date) AS last_on \
         FROM (SELECT id FROM broker_trade WHERE user_id = $1 \
               AND ($2::varchar IS NULL OR symbol = $2)) AS anon_1, broker_trade bt",
    )
    .bind(user_id)
    .bind(symbol.as_deref())
    .fetch_one(pool)
    .await
    .map_err(db::fail)?;
    let limit = params::bigint(limit.unwrap_or(100))?;
    let offset = params::bigint(offset.unwrap_or(0))?;
    let rows = sqlx::query(
        "SELECT trade_date, executed_at, symbol, exchange, side, quantity, price, trade_id, \
         source FROM broker_trade WHERE user_id = $1 AND ($2::varchar IS NULL OR symbol = $2) \
         ORDER BY trade_date DESC, executed_at DESC NULLS LAST, id DESC LIMIT $3 OFFSET $4",
    )
    .bind(user_id)
    .bind(symbol.as_deref())
    .bind(limit)
    .bind(offset)
    .fetch_all(pool)
    .await
    .map_err(db::fail)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        let quantity = db::dec(row, "quantity")?;
        let price = db::dec(row, "price")?;
        out.push(BrokerTradeOut {
            trade_date: db::date(row, "trade_date")?,
            executed_at: db::opt_ts(row, "executed_at")?,
            symbol: db::get(row, "symbol")?,
            exchange: db::get(row, "exchange")?,
            side: db::get(row, "side")?,
            value: num::quantize(&quantity.mul(&price), -2),
            quantity,
            price,
            trade_id: db::get(row, "trade_id")?,
            source: db::get(row, "source")?,
        });
    }
    Ok(wire::json(&BrokerTradesOut {
        rows: out,
        total: db::get(&bounds, "n")?,
        first_trade_on: db::opt_date(&bounds, "first_on")?,
        last_trade_on: db::opt_date(&bounds, "last_on")?,
    }))
}

/// The GET route of `baskfy_api.routers.trades`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/trades", get(list_trades))
        // The write routes' paths, with no method: a GET there is Starlette's 405.
        .route("/trades/import", axum::routing::MethodRouter::new())
        .route("/trades/sync-today", axum::routing::MethodRouter::new())
}
