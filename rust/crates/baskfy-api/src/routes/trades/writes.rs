//! The write routes of `baskfy_api.routers.trades` with `baskfy_api.broker_trades`:
//! `POST /trades/import` (a Console tradebook CSV, multipart) and `POST /trades/sync-today`.
//!
//! Bookkeeping only (law 2): rows go to `broker_trade`, and a holding's purchase date may be
//! filled from trades that add up to it. `sync-today` is ported for every answer that reads no
//! broker (`DRY_RUN`, no app key, no stored session, an unusable one); Kite's read-only
//! `GET /trades` itself is not, so with a fresh session it answers 503 and Python keeps serving
//! it (a cutover gate, DECISIONS-RS R6.22).

use std::collections::{BTreeSet, HashMap};

use axum::extract::{Extension, RawQuery, Request, State};
use axum::response::Response;
use chrono::{NaiveDateTime, TimeZone, Utc};
use serde::Serialize;
use sqlx::{PgConnection, QueryBuilder};

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType};
use crate::query::QueryParams;
use crate::routes::brokers::writes::{dry_run_enabled, live_read_not_ported};
use crate::routes::brokers::{env_stripped, token_store_path};
use crate::routes::portfolios::portfolio::model::{self, Errors};
use crate::routes::portfolios::portfolio::multipart::{self, FormValue};
use crate::routes::portfolios::portfolio::pydec::Dec;
use crate::routes::portfolios::portfolio::request::{pool, read_body};
use crate::routes::portfolios::portfolio::symbols::{
    ensure_default_broker_account, resolve_symbols,
};
use crate::routes::portfolios::portfolio::tradebook::{TradeFill, history_for};
use crate::routes::portfolios::portfolio::tradebook_csv::{
    ParsedFill, TradebookFailure, parse_tradebook_csv,
};
use crate::routes::portfolios::portfolio::{db, fail, tenant};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

/// `MAX_TRADEBOOK_BYTES`.
const MAX_TRADEBOOK_BYTES: usize = 20 * 1024 * 1024;

/// `TradebookUndatedHoldingOut`.
#[derive(Debug, Clone, Serialize)]
struct UndatedOut {
    symbol: String,
    held: String,
    traded_net: String,
    reason: &'static str,
}

/// `TradebookImportOut`.
#[derive(Debug, Clone, Serialize, Default)]
struct TradebookImportOut {
    received: usize,
    inserted: usize,
    already_present: usize,
    skipped_non_equity: usize,
    unresolved_symbols: Vec<String>,
    dated_holdings: usize,
    undated_holdings: Vec<UndatedOut>,
    note: String,
}

/// `StoreReport`.
struct StoreReport {
    received: usize,
    inserted: usize,
    unresolved: Vec<String>,
}

/// `HistoryReport`.
struct HistoryReport {
    dated: usize,
    undated: Vec<UndatedOut>,
}

fn utc_of_ist(t: NaiveDateTime) -> chrono::DateTime<Utc> {
    crate::routes::common::ist()
        .from_local_datetime(&t)
        .single()
        .map_or_else(|| t.and_utc(), |d| d.with_timezone(&Utc))
}

/// `store_fills(session, fills, user_id=, broker_account_id=, source=)`.
async fn store_fills(
    conn: &mut PgConnection,
    fills: &[ParsedFill],
    user_id: i64,
    broker_account_id: i64,
    source: &str,
) -> Result<StoreReport, Problem> {
    if fills.is_empty() {
        return Ok(StoreReport {
            received: 0,
            inserted: 0,
            unresolved: Vec::new(),
        });
    }
    let symbols: Vec<String> = fills.iter().map(|f| f.symbol.clone()).collect();
    let resolutions = resolve_symbols(conn, &symbols).await?;
    let mut unresolved = BTreeSet::new();
    let mut instruments = Vec::with_capacity(fills.len());
    for fill in fills {
        let id = resolutions
            .get(&fill.symbol)
            .and_then(|r| r.instrument_id());
        if id.is_none() {
            unresolved.insert(fill.symbol.clone());
        }
        instruments.push(id);
    }
    let mut inserted = 0usize;
    for (chunk, ids) in fills.chunks(1000).zip(instruments.chunks(1000)) {
        let mut q: QueryBuilder<'_, sqlx::Postgres> = QueryBuilder::new(
            "INSERT INTO broker_trade (user_id, broker_account_id, instrument_id, symbol, isin, \
             exchange, side, quantity, price, trade_date, executed_at, trade_id, order_id, \
             source) ",
        );
        q.push_values(chunk.iter().zip(ids), |mut b, (fill, id)| {
            b.push_bind(user_id)
                .push_bind(broker_account_id)
                .push_bind(*id)
                .push_bind(fill.symbol.clone())
                .push_bind(fill.isin.clone())
                .push_bind(fill.exchange.clone())
                .push_bind(if fill.is_buy { "BUY" } else { "SELL" })
                .push_bind(fill.quantity.to_sci_string())
                .push_unseparated("::numeric")
                .push_bind(fill.price.to_sci_string())
                .push_unseparated("::numeric")
                .push_bind(fill.trade_date)
                .push_bind(fill.executed_at.map(utc_of_ist))
                .push_bind(fill.trade_id.clone())
                .push_bind(fill.order_id.clone())
                .push_bind(source);
        });
        q.push(" ON CONFLICT ON CONSTRAINT uq_broker_trade_account_trade DO NOTHING RETURNING id");
        let rows = q.build().fetch_all(&mut *conn).await.map_err(db::fail)?;
        inserted += rows.len();
    }
    Ok(StoreReport {
        received: fills.len(),
        inserted,
        unresolved: unresolved.into_iter().collect(),
    })
}

/// `apply_trade_history(session, user_id=, broker_account_id=)`.
async fn apply_trade_history(
    conn: &mut PgConnection,
    user_id: i64,
    broker_account_id: i64,
) -> Result<HistoryReport, Problem> {
    let held_rows = sqlx::query(
        "SELECT portfolio_holding.instrument_id, sum(portfolio_holding.quantity) AS held \
         FROM portfolio_holding WHERE portfolio_holding.broker_account_id = $1 \
         AND portfolio_holding.portfolio_kind = $2 GROUP BY portfolio_holding.instrument_id",
    )
    .bind(broker_account_id)
    .bind("CAPITAL")
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    if held_rows.is_empty() {
        return Ok(HistoryReport {
            dated: 0,
            undated: Vec::new(),
        });
    }
    let fill_rows = sqlx::query(
        "SELECT instrument_id, side, quantity, price, trade_date, trade_id, executed_at \
         FROM broker_trade WHERE user_id = $1 AND instrument_id IS NOT NULL \
         AND broker_account_id = $2",
    )
    .bind(user_id)
    .bind(broker_account_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut fills: HashMap<i64, Vec<TradeFill>> = HashMap::new();
    for row in &fill_rows {
        let side: String = db::get(row, "side")?;
        let is_buy = match side.as_str() {
            "BUY" => true,
            "SELL" => false,
            other => return Err(fail(&format!("'{other}' is not a valid TradeSide"))),
        };
        fills
            .entry(db::get(row, "instrument_id")?)
            .or_default()
            .push(TradeFill {
                is_buy,
                quantity: db::pdec(row, "quantity")?,
                price: db::pdec(row, "price")?,
                trade_date: db::date(row, "trade_date")?,
                trade_id: db::get(row, "trade_id")?,
                executed_at: db::opt_ts(row, "executed_at")?,
            });
    }
    let symbol_rows = sqlx::query(
        "SELECT instrument_id, min(symbol) AS symbol FROM broker_trade \
         WHERE broker_account_id = $1 GROUP BY instrument_id",
    )
    .bind(broker_account_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut symbols: HashMap<i64, String> = HashMap::new();
    for row in &symbol_rows {
        if let Some(id) = db::get::<Option<i64>>(row, "instrument_id")? {
            symbols.insert(id, db::get(row, "symbol")?);
        }
    }
    let mut dated = 0usize;
    let mut undated = Vec::new();
    for row in &held_rows {
        let instrument_id: i64 = db::get(row, "instrument_id")?;
        // `Decimal(held)`: a sum of NULLs is Python's TypeError.
        let held = db::opt_pdec(row, "held")?
            .ok_or_else(|| fail("TypeError: conversion from NoneType to Decimal"))?;
        let empty = Vec::new();
        let own = fills.get(&instrument_id).unwrap_or(&empty);
        let history = history_for(own, &held).map_err(|e| fail(&e))?;
        let Some(first) = history.first_bought_on.filter(|_| history.reconciles) else {
            if fills.contains_key(&instrument_id) {
                undated.push(UndatedOut {
                    symbol: symbols
                        .get(&instrument_id)
                        .cloned()
                        .unwrap_or_else(|| instrument_id.to_string()),
                    held: held.to_sci_string(),
                    traded_net: history.net_quantity.to_sci_string(),
                    reason: if history.unmatched_sell_quantity > Dec::zero() {
                        "the file starts after this position was opened"
                    } else {
                        "trades do not add up to what is held (a bonus, split or transfer-in \
                         changes quantity without a trade)"
                    },
                });
            }
            continue;
        };
        sqlx::query(
            "UPDATE portfolio_holding SET first_bought_on = $1, history_source = 'BROKER' \
             WHERE broker_account_id = $2 AND instrument_id = $3 \
             AND history_source IN ('NONE', 'BROKER')",
        )
        .bind(first)
        .bind(broker_account_id)
        .bind(instrument_id)
        .execute(&mut *conn)
        .await
        .map_err(db::fail)?;
        dated += 1;
    }
    Ok(HistoryReport { dated, undated })
}

fn report(
    store: Option<StoreReport>,
    history: Option<HistoryReport>,
    skipped: usize,
    note: String,
) -> TradebookImportOut {
    let (received, inserted, unresolved) = store
        .map(|s| (s.received, s.inserted, s.unresolved))
        .unwrap_or_default();
    let (dated, undated) = history.map(|h| (h.dated, h.undated)).unwrap_or_default();
    TradebookImportOut {
        received,
        inserted,
        already_present: received - inserted,
        skipped_non_equity: skipped,
        unresolved_symbols: unresolved,
        dated_holdings: dated,
        undated_holdings: undated,
        note,
    }
}

fn bad_request(detail: String) -> Problem {
    Problem::new(ProblemType::BadRequest, detail)
}

/// `POST /trades/import?broker_id=zerodha` (multipart `file`): a Console tradebook, re-importable.
///
/// # Errors
/// 400 for the form, `broker_id`, the size, the encoding or an unreadable line; 401 anonymous.
pub async fn import_tradebook(
    State(_state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    RawQuery(raw): RawQuery,
    request: Request,
) -> Result<Response, Problem> {
    let (_, content_type, bytes) = read_body(request).await?;
    let file = multipart::form_field(content_type.as_deref(), &bytes, "file")?;
    let principal = require_authenticated(&principal)?.clone();
    let q = QueryParams::parse(raw.as_deref());
    let mut errors = Errors::default();
    let broker_id = match q.get("broker_id") {
        None => Some("zerodha".to_owned()),
        Some(b) => model::string(
            &baskfy_db::OrderedJson::Str(b.to_owned()),
            "broker_id",
            Some(2),
            Some(32),
            &mut errors,
        ),
    };
    let data = match file {
        None => {
            errors.push("file", "Field required", "missing");
            None
        }
        Some(FormValue::Text(_)) => {
            errors.push("file", multipart::NOT_A_FILE, "value_error");
            None
        }
        Some(FormValue::File { data, .. }) => Some(data),
    };
    errors.finish()?;
    let (Some(broker_id), Some(data)) = (broker_id, data) else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    if data.len() > MAX_TRADEBOOK_BYTES {
        return Err(bad_request(format!(
            "The file is {} bytes; the limit is {MAX_TRADEBOOK_BYTES}. Export one year at a time.",
            data.len()
        )));
    }
    let text = std::str::from_utf8(&data)
        .map_err(|_| bad_request("The file is not UTF-8 text.".to_owned()))?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let parsed = match parse_tradebook_csv(text) {
        Ok(p) => p,
        Err(TradebookFailure::Refused(m)) => {
            return Err(bad_request(format!("The tradebook could not be read: {m}")));
        }
        Err(TradebookFailure::Csv(e)) => return Err(fail(&format!("csv.Error: {}", e.0))),
    };
    let pool = pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let account = ensure_default_broker_account(conn, user_id, &broker_id).await?;
            let store = store_fills(conn, &parsed.fills, user_id, account, "CONSOLE_CSV").await?;
            let history = apply_trade_history(conn, user_id, account).await?;
            Ok(report(
                Some(store),
                Some(history),
                parsed.skipped_non_equity,
                String::new(),
            ))
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `read_kite_trades_today()`'s reasons for reading nothing; `Err` where Kite would be read.
fn read_reason() -> Result<String, Problem> {
    if dry_run_enabled() {
        return Ok("DRY_RUN is on; no broker read was made".to_owned());
    }
    if env_stripped("BASKFY_KITE_API_KEY").is_empty() {
        return Ok("no broker app key is configured on this deployment".to_owned());
    }
    let store = baskfy_providers::tokens::AccessTokenStore::new(
        token_store_path(),
        env_stripped("BASKFY_KITE_TOKEN_ENCRYPTION_KEY").into(),
    );
    if !store.exists() {
        return Ok("no Kite session has been stored yet".to_owned());
    }
    if let Err(e) = store.require_fresh(None) {
        let kind = e.python_type().to_owned();
        crate::logging::warning(
            "baskfy_api.broker_trades",
            "trades: the Kite read failed",
            &[("error", &kind)],
        );
        return Ok(format!("the Kite trades read failed ({kind})"));
    }
    Err(live_read_not_ported())
}

/// `POST /trades/sync-today`: today's Kite trades, stored and applied — sole-tenant.
///
/// # Errors
/// 401 anonymous; the sole-tenant guard's 503/404; 503 where Kite would be read (not ported).
pub async fn sync_todays_trades(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    tenant::scoped_sole_user_id(&state, Some(user_id), Some("broker trades sync")).await?;
    let reason = read_reason()?;
    Ok(wire::json(&report(None, None, 0, reason)))
}

// ---------------------------------------------------------------------------------------------
// The worker's evening capture (wave 6, lane portfolio)
// ---------------------------------------------------------------------------------------------

/// One execution from Kite's read-only `GET /trades`, as the worker's
/// `baskfy.pipeline.capture_kite_trades` hands it to [`capture_store_and_date`]
/// (`baskfy_core.tradebook.TradeFill`). `executed_at` is IST wall time: `_executed_at` reads a
/// naive stamp as IST, and the caller converts an aware one to IST first.
#[derive(Debug, Clone)]
pub struct CaptureFill {
    /// The symbol.
    pub symbol: String,
    /// `NSE` or `BSE`.
    pub exchange: String,
    /// `BUY` (else `SELL`).
    pub is_buy: bool,
    /// Shares.
    pub quantity: baskfy_core::decimal::WideDecimal,
    /// Rupees.
    pub price: baskfy_core::decimal::WideDecimal,
    /// The fill's date.
    pub trade_date: chrono::NaiveDate,
    /// Kite's trade id.
    pub trade_id: String,
    /// Kite's order id.
    pub order_id: Option<String>,
    /// ISIN.
    pub isin: Option<String>,
    /// IST wall time.
    pub executed_at: Option<NaiveDateTime>,
}

/// What [`capture_store_and_date`] did: `StoreReport` and `HistoryReport`'s counts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureCounts {
    /// `store.received`.
    pub received: usize,
    /// `store.inserted`.
    pub inserted: usize,
    /// `store.unresolved`, sorted.
    pub unresolved: Vec<String>,
    /// `history.dated`.
    pub dated_holdings: usize,
}

/// The storage half of `broker_trades.capture_kite_trades(session, user_id=)`, after the read:
/// `ensure_default_broker_account(session, user_id, broker_id="zerodha")`, `store_fills(...,
/// source=TradeSource.KITE_API)` and `apply_trade_history`, on the caller's transaction (the
/// caller commits). Added for the worker (DECISIONS-RS R8.pf1); the routes above are unchanged.
///
/// # Errors
/// As the three steps.
pub async fn capture_store_and_date(
    conn: &mut PgConnection,
    user_id: i64,
    fills: Vec<CaptureFill>,
) -> Result<CaptureCounts, Problem> {
    let account = ensure_default_broker_account(conn, user_id, "zerodha").await?;
    let parsed: Vec<ParsedFill> = fills
        .into_iter()
        .map(|f| ParsedFill {
            symbol: f.symbol,
            exchange: f.exchange,
            is_buy: f.is_buy,
            quantity: f.quantity,
            price: f.price,
            trade_date: f.trade_date,
            trade_id: f.trade_id,
            order_id: f.order_id,
            isin: f.isin,
            executed_at: f.executed_at,
        })
        .collect();
    let store = store_fills(conn, &parsed, user_id, account, "KITE_API").await?;
    let history = apply_trade_history(conn, user_id, account).await?;
    Ok(CaptureCounts {
        received: store.received,
        inserted: store.inserted,
        unresolved: store.unresolved,
        dated_holdings: history.dated,
    })
}
