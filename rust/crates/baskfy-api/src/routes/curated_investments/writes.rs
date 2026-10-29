//! The **write** routes of `baskfy_api.routers.curated_investments` (wave 4c, lane
//! writes-screens), with `baskfy_api.curated_investments.mark_invested` and the fee arithmetic it
//! calls (`baskfy_core.curated_accounting.platform_fee`, `baskfy_core.gst.money`):
//!
//! * `POST /cb/investments/mark` — record a book the user **already traded at the broker**: an
//!   ACTIVE `cb_investment`, its holdings, a `PLANNED` BUY batch and an uncollected fee row. It
//!   records a broker action; it never places one;
//! * `PUT /cb/investments/{investment_id}/portfolio` — file the investment under one of the
//!   user's portfolios (a broker mismatch is reported, never rewritten);
//! * `DELETE /cb/investments/{investment_id}/portfolio` — un-file it (the investment stands).
//!
//! Bookkeeping only: nothing here reaches a broker or places, modifies or cancels an order
//! (law 2, the desk's first non-negotiable).

use std::collections::HashMap;

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_db::row;
use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::Row;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, internal_error, not_found};
use crate::routes::common::py_space;
use crate::routes::screens::accounts::investments::{InvestmentRow, load_investment_for_user};
use crate::routes::screens::accounts::scoped_sole_user_id;
use crate::routes::screens::body_model::{self, Errors, Model, Ty, opt, req};
use crate::routes::screens::write_kit::{self, db, numeric_text};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;
use crate::wire;
use baskfy_db::OrderedJson;

/// `broker_accounts.DEFAULT_BROKER_ID`.
const DEFAULT_BROKER_ID: &str = "zerodha";

/// `HoldingBody`.
fn holding_body() -> Model {
    Model {
        fields: vec![
            req("symbol", Ty::Str(Some(1), Some(32))),
            req(
                "qty",
                Ty::Dec {
                    gt: Some("0"),
                    ge: None,
                    le: None,
                },
            ),
            req(
                "avg_price",
                Ty::Dec {
                    gt: None,
                    ge: Some("0"),
                    le: None,
                },
            ),
        ],
        forbid_extra: false,
    }
}

/// `MarkBody`.
fn mark_body() -> Model {
    Model {
        fields: vec![
            req("basket_slug", Ty::Str(Some(1), Some(64))),
            req(
                "amount",
                Ty::Dec {
                    gt: Some("0"),
                    ge: None,
                    le: None,
                },
            ),
            req("confirmed", Ty::Bool),
            req(
                "holdings",
                Ty::List(Box::new(Ty::Model(holding_body())), Some(1)),
            ),
            opt("desk_plan_id", Ty::Opt(Box::new(Ty::Str(None, None)))),
        ],
        forbid_extra: false,
    }
}

/// `PortfolioLinkBody`.
fn link_body() -> Model {
    Model {
        fields: vec![req(
            "portfolio_id",
            Ty::Int {
                gt: Some(0),
                ge: None,
                le: None,
            },
        )],
        forbid_extra: false,
    }
}

fn invalid(detail: impl Into<String>) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, detail)
}

/// `money(value)`: quantise to paise, half-up; Python's `InvalidOperation` (beyond 28 digits) is
/// the 500.
fn money(value: &WideDecimal) -> Result<WideDecimal, Problem> {
    value.quantize(-2, Rounding::HalfUp).map_err(|e| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", &e.to_string())],
        );
        internal_error()
    })
}

fn wide(text: &str) -> Result<WideDecimal, Problem> {
    text.parse::<WideDecimal>().map_err(|_| internal_error())
}

/// `platform_fee("BUY", amount)`: `base = money(min(100, money(1.5% · amount)))`,
/// `gst = money(18% · base)`, `total = money(base + gst)`.
fn platform_fee_buy(
    amount: &WideDecimal,
) -> Result<(WideDecimal, WideDecimal, WideDecimal), Problem> {
    let cap = wide("100")?;
    let uncapped = money(&amount.mul(&wide("0.015")?))?;
    let base = money(if uncapped < cap { &uncapped } else { &cap })?;
    let gst = money(&base.mul(&wide("0.18")?))?;
    let total = money(&base.add(&gst))?;
    Ok((base, gst, total))
}

/// `BatchOut`.
#[derive(Debug, Clone, Serialize)]
struct BatchOut {
    id: i64,
    kind: String,
    status: String,
    desk_plan_id: Option<String>,
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    created_at: Option<DateTime<Utc>>,
}

/// `MarkOut`.
#[derive(Debug, Clone, Serialize)]
struct MarkOut {
    id: i64,
    status: String,
    basket_slug: String,
    batch: BatchOut,
}

/// One declared lot.
struct Lot {
    symbol: String,
    qty: WideDecimal,
    avg_price: WideDecimal,
}

/// `ensure_default_broker_account(session, user_id)`.
async fn ensure_default_broker_account(tx: &mut Tx, user_id: i64) -> Result<i64, Problem> {
    let existing: Option<i64> =
        sqlx::query_scalar("SELECT id FROM broker_account WHERE user_id = $1 AND broker_id = $2")
            .bind(user_id)
            .bind(DEFAULT_BROKER_ID)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
    if let Some(id) = existing {
        return Ok(id);
    }
    sqlx::query_scalar(
        "INSERT INTO broker_account (user_id, broker_id, label) VALUES ($1, $2, 'primary') \
         RETURNING id",
    )
    .bind(user_id)
    .bind(DEFAULT_BROKER_ID)
    .fetch_one(&mut **tx)
    .await
    .map_err(db)
}

/// `_instruments_by_symbol`: active instruments whose upper-cased symbol is listed, keyed by it
/// (a later row wins a collision, as the dict comprehension's does).
async fn instruments_by_symbol(
    tx: &mut Tx,
    symbols: &[String],
) -> Result<HashMap<String, i64>, Problem> {
    let rows = sqlx::query(
        "SELECT id, symbol FROM instrument WHERE upper(symbol) = ANY($1) AND is_active IS true",
    )
    .bind(symbols)
    .fetch_all(&mut **tx)
    .await
    .map_err(db)?;
    let mut out = HashMap::new();
    for r in &rows {
        let symbol: String = r.try_get("symbol").map_err(db)?;
        out.insert(
            symbol.to_uppercase(),
            r.try_get::<i64, _>("id").map_err(db)?,
        );
    }
    Ok(out)
}

/// `POST /cb/investments/mark` (`mark_as_invested`): 201.
///
/// # Errors
/// 400 (body, `confirmed` false, duplicate symbols), 401, the sole-tenant refusals, 404 (basket,
/// unknown symbols), 409 (an ACTIVE investment exists), 500.
pub async fn mark_as_invested(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &mark_body(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    if body.get("confirmed").bool() != Some(true) {
        return Err(invalid(
            "confirmed must be true — this records a broker action, it does not place one.",
        ));
    }
    let amount = body
        .get("amount")
        .dec()
        .cloned()
        .ok_or_else(internal_error)?;
    let lots: Vec<Lot> = body
        .get("holdings")
        .items()
        .iter()
        .map(|h| Lot {
            symbol: h.get("symbol").str().unwrap_or_default().to_owned(),
            qty: h.get("qty").dec().cloned().unwrap_or_default(),
            avg_price: h.get("avg_price").dec().cloned().unwrap_or_default(),
        })
        .collect();
    let desk_plan_id = body.get("desk_plan_id").str().map(str::to_owned);
    let basket_slug = body.get("basket_slug").str().unwrap_or_default().to_owned();
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let slug = basket_slug.trim_matches(py_space).to_owned();
            let basket = sqlx::query(
                "SELECT id, slug FROM cb_basket WHERE slug = $1 AND archived_at IS NULL",
            )
            .bind(&slug)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?
            .ok_or_else(|| not_found("basket", &slug))?;
            let basket_id: i64 = basket.try_get("id").map_err(db)?;
            let basket_slug_stored: String = basket.try_get("slug").map_err(db)?;
            let existing: Option<i64> = sqlx::query_scalar(
                "SELECT id FROM cb_investment WHERE user_id = $1 AND basket_id = $2 \
                 AND status = 'ACTIVE'",
            )
            .bind(user_id)
            .bind(basket_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            if let Some(id) = existing {
                return Err(Problem::new(
                    ProblemType::StaleDataVersion,
                    format!(
                        "an ACTIVE investment already exists for {}",
                        baskfy_providers::py::repr_str(&slug)
                    ),
                )
                .with("sent_data_version", OrderedJson::Int(id))
                .with("current_data_version", OrderedJson::Int(id)));
            }
            let version: Option<i64> = sqlx::query_scalar(
                "SELECT id FROM cb_basket_version WHERE basket_id = $1 \
                 ORDER BY version_no DESC LIMIT 1",
            )
            .bind(basket_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let Some(version_id) = version else {
                return Err(Problem::new(
                    ProblemType::InternalError,
                    format!(
                        "basket {} has no version",
                        baskfy_providers::py::repr_str(&slug)
                    ),
                ));
            };
            let symbols: Vec<String> = lots
                .iter()
                .map(|l| l.symbol.trim_matches(py_space).to_uppercase())
                .collect();
            let mut seen = std::collections::HashSet::new();
            if !symbols.iter().all(|s| seen.insert(s.clone())) {
                return Err(invalid("duplicate symbols are not allowed"));
            }
            let by_symbol = instruments_by_symbol(tx, &symbols).await?;
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
            let now = write_kit::now_utc();
            let broker_account_id = ensure_default_broker_account(tx, user_id).await?;
            let investment_id: i64 = sqlx::query_scalar(
                "INSERT INTO cb_investment (user_id, broker_account_id, basket_id, status, \
                 version_applied_id, exited_at, last_invested_at) \
                 VALUES ($1, $2, $3, 'ACTIVE', $4, NULL, $5) RETURNING id",
            )
            .bind(user_id)
            .bind(broker_account_id)
            .bind(basket_id)
            .bind(version_id)
            .bind(now)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            // One multi-row INSERT per 1,000 lots, as the ORM's insertmanyvalues flush sends
            // them: a quantity the column cannot hold fails the statement at planning, before
            // any identity value is drawn (the holdings flush before the batch row).
            let mut holding_rows: Vec<(i64, String, String)> = Vec::with_capacity(lots.len());
            for (lot, symbol) in lots.iter().zip(&symbols) {
                let instrument_id = by_symbol.get(symbol).copied().ok_or_else(internal_error)?;
                holding_rows.push((
                    instrument_id,
                    numeric_text(&lot.qty),
                    numeric_text(&lot.avg_price),
                ));
            }
            for page in holding_rows.chunks(write_kit::INSERT_PAGE_ROWS) {
                let values = write_kit::values_rows(page.len(), 3, |p| {
                    format!(
                        "($1, ${}, (${}::text)::numeric, (${}::text)::numeric)",
                        p[0], p[1], p[2]
                    )
                });
                let sql = format!(
                    "INSERT INTO cb_investment_holding (investment_id, instrument_id, qty, \
                     avg_price) VALUES {values}"
                );
                let mut q = sqlx::query(&sql).bind(investment_id);
                for (instrument_id, qty, avg_price) in page {
                    q = q.bind(*instrument_id).bind(qty).bind(avg_price);
                }
                q.execute(&mut **tx).await.map_err(db)?;
            }
            let batch_id: i64 = sqlx::query_scalar(
                "INSERT INTO cb_order_batch (user_id, broker_account_id, investment_id, kind, \
                 requested_amount, desk_plan_id, status, fee_entry_id, executed_at) \
                 VALUES ($1, $2, $3, 'BUY', ($4::text)::numeric, $5, 'PLANNED', NULL, NULL) \
                 RETURNING id",
            )
            .bind(user_id)
            .bind(broker_account_id)
            .bind(investment_id)
            .bind(numeric_text(&amount))
            .bind(&desk_plan_id)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            let (base, gst, total) = platform_fee_buy(&amount)?;
            let ledger_id: i64 = sqlx::query_scalar(
                "INSERT INTO cb_fee_ledger (user_id, batch_id, kind, base_fee, gst, total, \
                 collected, accrued_at) VALUES ($1, $2, 'BUY', ($3::text)::numeric, \
                 ($4::text)::numeric, ($5::text)::numeric, false, $6) RETURNING id",
            )
            .bind(user_id)
            .bind(batch_id)
            .bind(numeric_text(&base))
            .bind(numeric_text(&gst))
            .bind(numeric_text(&total))
            .bind(now)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            sqlx::query("UPDATE cb_order_batch SET fee_entry_id = $1 WHERE id = $2")
                .bind(ledger_id)
                .bind(batch_id)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            let status: String =
                sqlx::query_scalar("SELECT status FROM cb_investment WHERE id = $1")
                    .bind(investment_id)
                    .fetch_one(&mut **tx)
                    .await
                    .map_err(db)?;
            let b = sqlx::query(
                "SELECT id, kind, status, desk_plan_id, created_at FROM cb_order_batch \
                 WHERE investment_id = $1 ORDER BY id DESC LIMIT 1",
            )
            .bind(investment_id)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            Ok(MarkOut {
                id: investment_id,
                status,
                basket_slug: basket_slug_stored,
                batch: BatchOut {
                    id: b.try_get("id").map_err(db)?,
                    kind: b.try_get("kind").map_err(db)?,
                    status: b.try_get("status").map_err(db)?,
                    desk_plan_id: b.try_get("desk_plan_id").map_err(db)?,
                    created_at: row::opt_timestamptz(&b, "created_at")
                        .map_err(|e| crate::db_failure(&e))?,
                },
            })
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// `BrokerReconciliationOut`.
#[derive(Debug, Clone, Serialize)]
struct BrokerReconciliationOut {
    investment_broker_account_id: i64,
    investment_broker_id: Option<String>,
    portfolio_broker_account_id: Option<i64>,
    portfolio_broker_id: Option<String>,
    portfolio_spans_brokers: bool,
    conflict: bool,
    detail: Option<String>,
}

/// `InvestmentPortfolioLinkOut`.
#[derive(Debug, Clone, Serialize)]
struct InvestmentPortfolioLinkOut {
    investment_id: i64,
    portfolio_id: Option<i64>,
    portfolio_name: Option<String>,
    broker: Option<BrokerReconciliationOut>,
}

/// One portfolio as the link needs it.
struct Portfolio {
    id: i64,
    name: String,
    broker_account_id: Option<i64>,
}

/// `_portfolio_for_user`: one of *this* user's portfolios, or `not_found` (never a 403).
async fn portfolio_for_user(
    tx: &mut Tx,
    user_id: i64,
    portfolio_id: i128,
) -> Result<Portfolio, Problem> {
    // Beyond bigint asyncpg cannot bind the id: the 500 catch-all.
    let id = i64::try_from(portfolio_id).map_err(|_| internal_error())?;
    let r = sqlx::query(
        "SELECT id, name, broker_account_id FROM portfolio WHERE id = $1 AND user_id = $2",
    )
    .bind(id)
    .bind(user_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(db)?
    .ok_or_else(|| not_found("portfolio", &portfolio_id.to_string()))?;
    Ok(Portfolio {
        id: r.try_get("id").map_err(db)?,
        name: r.try_get("name").map_err(db)?,
        broker_account_id: r.try_get("broker_account_id").map_err(db)?,
    })
}

/// `_link_out(session, inv, portfolio)`.
async fn link_out(
    tx: &mut Tx,
    inv: &InvestmentRow,
    portfolio: Option<Portfolio>,
) -> Result<InvestmentPortfolioLinkOut, Problem> {
    let Some(portfolio) = portfolio else {
        return Ok(InvestmentPortfolioLinkOut {
            investment_id: inv.id,
            portfolio_id: None,
            portfolio_name: None,
            broker: None,
        });
    };
    let investment_account = inv.broker_account_id;
    let declared = portfolio.broker_account_id;
    let mut wanted = vec![investment_account];
    if let Some(d) = declared
        && d != investment_account
    {
        wanted.push(d);
    }
    let rows =
        sqlx::query("SELECT id, broker_id FROM broker_account WHERE id = ANY($1) AND user_id = $2")
            .bind(&wanted)
            .bind(inv.user_id)
            .fetch_all(&mut **tx)
            .await
            .map_err(db)?;
    let mut broker_id_for: HashMap<i64, String> = HashMap::new();
    for r in &rows {
        broker_id_for.insert(
            r.try_get("id").map_err(db)?,
            r.try_get("broker_id").map_err(db)?,
        );
    }
    let conflict = declared.is_some_and(|d| d != investment_account);
    let name_of = |id: i64| {
        broker_id_for
            .get(&id)
            .cloned()
            .unwrap_or_else(|| "unknown broker".to_owned())
    };
    let detail = match declared {
        Some(d) if conflict => Some(format!(
            "Investment {} is held at broker account {investment_account} ({}), but portfolio \
             {} ({}) declares broker account {d} ({}). The filing was recorded and both \
             attributions were left exactly as they are; neither side is rewritten to make the \
             other agree.",
            inv.id,
            name_of(investment_account),
            portfolio.id,
            baskfy_providers::py::repr_str(&portfolio.name),
            name_of(d)
        )),
        _ => None,
    };
    Ok(InvestmentPortfolioLinkOut {
        investment_id: inv.id,
        portfolio_id: Some(portfolio.id),
        portfolio_name: Some(portfolio.name),
        broker: Some(BrokerReconciliationOut {
            investment_broker_account_id: investment_account,
            investment_broker_id: broker_id_for.get(&investment_account).cloned(),
            portfolio_broker_account_id: declared,
            portfolio_broker_id: declared.and_then(|d| broker_id_for.get(&d).cloned()),
            portfolio_spans_brokers: declared.is_none(),
            conflict,
            detail,
        }),
    })
}

/// The `investment_id: int` path parameter, recorded on `e`; `Some(None)` is an integer beyond
/// bigint (it validates, then cannot be bound: the 500 after the sole-tenant gate).
fn investment_id(e: &mut Errors, raw: &str) -> Option<Option<i64>> {
    body_model::path_int(e, "investment_id", raw).map(|n| i64::try_from(n).ok())
}

/// `PUT /cb/investments/{investment_id}/portfolio` (`link_investment_to_portfolio`).
///
/// # Errors
/// 400 (path, body), 401, the sole-tenant refusals, 404 (investment, portfolio), 500.
pub async fn link_investment_to_portfolio(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let id = investment_id(&mut e, &raw_id);
    let body = body_model::body(&mut e, &received, &link_body(), true);
    e.finish()?;
    let (id, body) = (
        id.ok_or_else(internal_error)?,
        body.ok_or_else(internal_error)?,
    );
    let portfolio_id = body.get("portfolio_id").int().ok_or_else(internal_error)?;
    let investment = load_investment_for_user(&state, principal.user_id, id).await?;
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let portfolio = portfolio_for_user(tx, investment.user_id, portfolio_id).await?;
            // The ORM writes only a net change (no UPDATE when the filing is unchanged).
            sqlx::query(
                "UPDATE cb_investment SET portfolio_id = $1 WHERE id = $2 \
                 AND portfolio_id IS DISTINCT FROM $1",
            )
            .bind(portfolio.id)
            .bind(investment.id)
            .execute(&mut **tx)
            .await
            .map_err(db)?;
            link_out(tx, &investment, Some(portfolio)).await
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `DELETE /cb/investments/{investment_id}/portfolio` (`unlink_investment_from_portfolio`):
/// clears `portfolio_id` only; idempotent.
///
/// # Errors
/// 400 (path), 401, the sole-tenant refusals, 404, 500.
pub async fn unlink_investment_from_portfolio(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let id = investment_id(&mut e, &raw_id);
    e.finish()?;
    let id = id.ok_or_else(internal_error)?;
    let investment = load_investment_for_user(&state, principal.user_id, id).await?;
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            sqlx::query(
                "UPDATE cb_investment SET portfolio_id = NULL WHERE id = $1 \
                 AND portfolio_id IS NOT NULL",
            )
            .bind(investment.id)
            .execute(&mut **tx)
            .await
            .map_err(db)?;
            link_out(tx, &investment, None).await
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `GET /cb/investments/mark`: Starlette serves it with `GET /cb/investments/{investment_id}`
/// (`investment_id="mark"` — the 400 for a non-integer id, after the 401).
///
/// # Errors
/// As `get_investment`.
pub async fn get_mark(
    state: State<AppState>,
    principal: Extension<Principal>,
) -> Result<Response, Problem> {
    super::get_investment(state, principal, Path("mark".to_owned())).await
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    // `packages/core/tests/test_curated_accounting.py`: the BUY fee, capped at ₹100 + 18% GST.
    #[test]
    fn the_buy_fee_is_one_and_a_half_percent_capped_at_a_hundred() {
        let fee = |a: &str| {
            let (b, g, t) = platform_fee_buy(&a.parse().unwrap()).unwrap();
            (b.to_sci_string(), g.to_sci_string(), t.to_sci_string())
        };
        assert_eq!(fee("1000"), ("15.00".into(), "2.70".into(), "17.70".into()));
        assert_eq!(
            fee("100000"),
            ("100.00".into(), "18.00".into(), "118.00".into())
        );
        assert_eq!(
            fee("1000.005"),
            ("15.00".into(), "2.70".into(), "17.70".into())
        );
        assert_eq!(fee("333.33"), ("5.00".into(), "0.90".into(), "5.90".into()));
    }

    #[test]
    fn values_reach_the_columns_unrounded() {
        assert_eq!(numeric_text(&"1E+3".parse().unwrap()), "1E+3");
    }
}
