//! Paper invest / rebalance from a saved screen, with optional MTF leverage sizing.
//!
//! No live order path (law 2). MTF here is a paper mark: the desk already names `MTF` as a
//! Kite product, and this lane reuses that word plus EQ-series eligibility. House leverage is 4.

use std::collections::{HashMap, HashSet};

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use serde::Serialize;
use sqlx::PgConnection;

use super::model::{self, Bounds, Errors};
use super::portfolio_csv::MAX_IMPORT_ROWS;
use super::portfolios_writes::{
    HoldingIn, ImportReportOut, PortfolioWriteDetailOut, apply, broker_account_for,
    insert_portfolio, portfolio_out, rows_from_body, too_many,
};
use super::request::{pool, read_body};
use super::{db, num};
use crate::auth::{Principal, require_authenticated};
use crate::body::{Received, receive};
use crate::problems::{Problem, ProblemType};
use crate::routes::common::lax_bool;
use crate::routes::portfolios::{OwnedPortfolio, holdings_out, load};
use crate::routes::screens::{ScreenRow, load_visible, load_visible_on};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

/// Names taken from a screen when the investor does not say otherwise (BALANCED profile).
const DEFAULT_TOP_N: usize = 20;
/// Whole-rupee leftover floor used by `basket_sizing` when no cash % is given.
const CASH_BUFFER_PCT: i64 = 5;
/// Zerodha-style overnight MTF when the caller does not send a factor. ⚠ UNREVIEWED.
const DEFAULT_LEVERAGE: i64 = 4;
const MAX_LEVERAGE: i64 = 10;
const MINIMUM_STEP: i64 = 100;

fn dec(n: i64) -> WideDecimal {
    WideDecimal::from(n)
}

fn refuse(message: impl Into<String>) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, message.into())
}

fn money(value: &WideDecimal) -> WideDecimal {
    num::quantize(value, -2)
}

fn json_i64(value: &OrderedJson) -> Option<i64> {
    match value {
        OrderedJson::Int(i) => Some(*i),
        OrderedJson::UInt(u) => i64::try_from(*u).ok(),
        OrderedJson::Str(s) => s.parse().ok(),
        OrderedJson::BigInt(s) => s.parse().ok(),
        _ => None,
    }
}

fn parse_bool(value: &OrderedJson, loc: &str, errors: &mut Errors) -> Option<bool> {
    match value {
        OrderedJson::Bool(b) => Some(*b),
        OrderedJson::Int(0) => Some(false),
        OrderedJson::Int(1) => Some(true),
        OrderedJson::Str(s) => match lax_bool(s) {
            Some(b) => Some(b),
            None => {
                errors.push(
                    loc,
                    "Input should be a valid boolean, unable to interpret input",
                    "bool_parsing",
                );
                None
            }
        },
        _ => {
            errors.push(loc, "Input should be a valid boolean", "bool_type");
            None
        }
    }
}

/// One ranked name after joining the stored run to the instrument and its close.
#[derive(Debug, Clone)]
pub(crate) struct InvestCandidate {
    symbol: String,
    name: String,
    rank: i64,
    price: Option<WideDecimal>,
    /// Kite's tradingsymbol. A BE listing is `SWANDEF-BE`, not the NSE symbol `SWANDEF`.
    tradingsymbol: String,
}

/// Why a name was not sized under MTF (or had no print).
#[derive(Debug, Clone, Serialize)]
pub struct ScreenInvestSkip {
    /// Symbol.
    pub symbol: String,
    /// Display name.
    pub name: String,
    /// `mtf_ineligible` or `no_price`.
    pub reason: String,
}

/// One reviewed line. `quantity` is omitted when the amount cannot buy a share.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenInvestHoldingOut {
    /// Symbol.
    pub symbol: String,
    /// Display name.
    pub name: String,
    /// Screen rank.
    pub rank: i64,
    /// Whole shares, when filled.
    pub quantity: Option<i64>,
    /// `qty * price` (leveraged exposure of the line).
    #[serde(serialize_with = "num::ser")]
    pub amount: WideDecimal,
    /// Share of deployed exposure.
    #[serde(serialize_with = "num::ser")]
    pub weight: WideDecimal,
    /// Exchange print used to size.
    #[serde(serialize_with = "num::ser_opt")]
    pub price: Option<WideDecimal>,
    /// This name's broker leverage. 1 means a cash position.
    #[serde(serialize_with = "num::ser")]
    pub leverage: WideDecimal,
    /// `MTF` when the broker quotes leverage above 1, otherwise `CNC`.
    pub product: String,
    /// Kite's tradingsymbol. Not the NSE symbol when the series is not EQ.
    #[serde(skip)]
    pub tradingsymbol: String,
}

/// Sized review, and the portfolio when this request wrote one.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenInvestOut {
    /// The stored run's `as_of`, if any.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub as_of: Option<NaiveDate>,
    /// Cash the investor puts in.
    #[serde(serialize_with = "num::ser")]
    pub cash_outlay: WideDecimal,
    /// `qty * price` across filled names (leveraged when MTF).
    #[serde(serialize_with = "num::ser")]
    pub exposure: WideDecimal,
    /// 1 when MTF is off.
    #[serde(serialize_with = "num::ser")]
    pub leverage: WideDecimal,
    /// Whether MTF sizing was requested.
    pub mtf: bool,
    /// Smallest cash that fills every chosen name.
    #[serde(serialize_with = "num::ser")]
    pub min_investment: WideDecimal,
    /// Notional not sitting in a filled share (buffer + leftover + skipped slices).
    #[serde(serialize_with = "num::ser")]
    pub leftover: WideDecimal,
    /// Why there is nothing to confirm, or `null`.
    pub empty_reason: Option<String>,
    /// Chosen names, including zero-qty rows.
    pub holdings: Vec<ScreenInvestHoldingOut>,
    /// Flagged, not silently treated as cash.
    pub skipped: Vec<ScreenInvestSkip>,
    /// Present after a create or a rebalance write.
    pub report: Option<PortfolioWriteDetailOut>,
    /// Broker results when MTF was confirmed. Empty for a cash paper book.
    pub orders: Vec<ScreenOrderOut>,
}

/// One MTF buy the gateway accepted or refused.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenOrderOut {
    /// Symbol.
    pub symbol: String,
    /// Gateway status (`PLACED`, `DRY_RUN`, `BLOCKED`, …).
    pub status: String,
    /// Broker order id, when the gateway returned one.
    pub order_id: Option<String>,
    /// Stop status, when a GTT was armed.
    pub stop: Option<String>,
    /// Refusal, when the line did not send.
    pub error: Option<String>,
}

#[derive(Debug, Clone)]
struct InvestBody {
    name: Option<String>,
    amount: WideDecimal,
    mtf: bool,
    leverage: WideDecimal,
    parent_id: Option<model::PyInt>,
    broker: Option<model::PyInt>,
}

#[derive(Debug)]
pub(crate) struct SizedBook {
    cash_outlay: WideDecimal,
    exposure: WideDecimal,
    leverage: WideDecimal,
    mtf: bool,
    min_investment: WideDecimal,
    leftover: WideDecimal,
    empty_reason: Option<String>,
    holdings: Vec<ScreenInvestHoldingOut>,
    skipped: Vec<ScreenInvestSkip>,
}

fn qty_of(notional_slice: &WideDecimal, price: &WideDecimal) -> Result<i64, Problem> {
    if *price <= dec(0) {
        return Ok(0);
    }
    let raw = notional_slice.div(price).map_err(|e| refuse(e.to_string()))?;
    let whole = raw
        .quantize(0, Rounding::Down)
        .map_err(|e| refuse(e.to_string()))?;
    let text = whole.to_sci_string();
    text.parse::<i64>().map_err(|_| refuse("quantity overflow"))
}

fn line_factor(mtf: bool, symbol: &str, factors: &HashMap<String, WideDecimal>) -> WideDecimal {
    if !mtf {
        return dec(1);
    }
    let raw = factors
        .get(&symbol.trim().to_ascii_uppercase())
        .cloned()
        .unwrap_or_else(|| dec(1));
    if raw <= dec(1) {
        dec(1)
    } else if raw > dec(MAX_LEVERAGE) {
        dec(MAX_LEVERAGE)
    } else {
        raw
    }
}

/// Each name gets the same position size. MTF only reduces the cash that size needs
/// (`position / leverage`). A name the broker will not margin is a cash (`CNC`) position.
pub(crate) fn size_book(
    rows: &[InvestCandidate],
    amount: &WideDecimal,
    mtf: bool,
    factors: &HashMap<String, WideDecimal>,
) -> Result<SizedBook, Problem> {
    if *amount <= dec(0) {
        return Err(refuse("amount must be greater than 0"));
    }
    let notional = amount.clone();
    let hundred = dec(100);
    let deploy_frac = dec(100 - CASH_BUFFER_PCT)
        .div(&hundred)
        .map_err(|e| refuse(e.to_string()))?;
    let deployable = notional.mul(&deploy_frac);

    let mut seen = HashSet::new();
    let mut ordered: Vec<&InvestCandidate> = Vec::new();
    let mut skipped = Vec::new();
    let mut ranked: Vec<&InvestCandidate> = rows.iter().collect();
    ranked.sort_by_key(|r| r.rank);
    for row in ranked {
        let symbol = row.symbol.trim().to_ascii_uppercase();
        if symbol.is_empty() || !seen.insert(symbol) {
            continue;
        }
        let price_ok = row.price.as_ref().is_some_and(|p| *p > dec(0));
        if !price_ok {
            skipped.push(ScreenInvestSkip {
                symbol: row.symbol.clone(),
                name: row.name.clone(),
                reason: "no_price".to_owned(),
            });
            continue;
        }
        ordered.push(row);
        if ordered.len() == DEFAULT_TOP_N {
            break;
        }
    }

    if ordered.is_empty() {
        let empty_reason = if rows.is_empty() {
            "Nothing survived your filters, so there is nothing to put in a portfolio."
        } else {
            "None of these names have a price, so there is nothing to put in a portfolio."
        };
        return Ok(SizedBook {
            cash_outlay: money(amount),
            exposure: money(&dec(0)),
            leverage: dec(1),
            mtf,
            min_investment: money(&dec(0)),
            leftover: money(&notional),
            empty_reason: Some(empty_reason.to_owned()),
            holdings: Vec::new(),
            skipped,
        });
    }

    let n = ordered.len();
    let mut cash_weights = dec(0);
    let mut planned: Vec<(&InvestCandidate, WideDecimal)> = Vec::with_capacity(n);
    for row in ordered.iter().copied() {
        let lev = line_factor(mtf, &row.symbol, factors);
        let cash_weight = dec(1).div(&lev).map_err(|e| refuse(e.to_string()))?;
        cash_weights = cash_weights.add(&cash_weight);
        planned.push((row, lev));
    }
    let target = deployable
        .div(&cash_weights)
        .map_err(|e| refuse(e.to_string()))?;
    let mut one_share_cash = dec(0);
    let mut lines = Vec::with_capacity(n);
    let mut exposure = dec(0);
    let mut margin_used = dec(0);
    for (row, lev) in planned {
        let price = row.price.clone().unwrap_or_else(WideDecimal::zero);
        let qty = qty_of(&target, &price)?;
        let filled = if qty > 0 {
            price.mul(&dec(qty))
        } else {
            dec(0)
        };
        let margin = filled
            .div(&lev)
            .map_err(|e| refuse(e.to_string()))?;
        exposure = exposure.add(&filled);
        margin_used = margin_used.add(&margin);
        let one = price.div(&lev).map_err(|e| refuse(e.to_string()))?;
        one_share_cash = one_share_cash.add(&one);
        let product = if lev > dec(1) {
            "MTF".to_owned()
        } else {
            "CNC".to_owned()
        };
        lines.push((row, qty, filled, price, lev, product));
    }

    let leftover = notional.sub(&margin_used);
    let min_cash = one_share_cash
        .div(&deploy_frac)
        .map_err(|e| refuse(e.to_string()))?;
    let steps = min_cash
        .div(&dec(MINIMUM_STEP))
        .map_err(|e| refuse(e.to_string()))?;
    let rounded_steps = steps
        .quantize(0, Rounding::HalfUp)
        .map_err(|e| refuse(e.to_string()))?;
    let min_investment = money(&rounded_steps.mul(&dec(MINIMUM_STEP)));
    let blended = if *amount > dec(0) {
        exposure.div(amount).map_err(|e| refuse(e.to_string()))?
    } else {
        dec(1)
    };

    let mut holdings = Vec::with_capacity(lines.len());
    let last = lines.len().saturating_sub(1);
    let mut weight_acc = dec(0);
    for (i, (row, qty, filled, price, lev, product)) in lines.into_iter().enumerate() {
        let weight = if exposure > dec(0) {
            if i == last {
                dec(1).sub(&weight_acc)
            } else {
                let w = filled
                    .div(&exposure)
                    .map_err(|e| refuse(e.to_string()))?;
                let w = num::quantize(&w, -4);
                weight_acc = weight_acc.add(&w);
                w
            }
        } else {
            dec(0)
        };
        holdings.push(ScreenInvestHoldingOut {
            symbol: row.symbol.clone(),
            name: row.name.clone(),
            rank: row.rank,
            quantity: (qty > 0).then_some(qty),
            amount: money(&filled),
            weight: num::quantize(&weight, -4),
            price: Some(money(&price)),
            leverage: num::quantize(&lev, -2),
            product,
            tradingsymbol: row.tradingsymbol.clone(),
        });
    }

    let empty_reason = if holdings.iter().all(|h| h.quantity.is_none()) {
        Some("Raise the amount so at least one name can be filled.".to_owned())
    } else {
        None
    };

    Ok(SizedBook {
        cash_outlay: money(amount),
        exposure: money(&exposure),
        leverage: num::quantize(&blended, -2),
        mtf,
        min_investment,
        leftover: money(&leftover),
        empty_reason,
        holdings,
        skipped,
    })
}

fn persistable(book: &SizedBook) -> Vec<HoldingIn> {
    book.holdings
        .iter()
        .filter_map(|row| {
            let qty = row.quantity?;
            let price = row.price.clone()?;
            Some(HoldingIn::sized(row.symbol.clone(), dec(qty), price))
        })
        .collect()
}

fn out_of(book: SizedBook, as_of: Option<NaiveDate>, report: Option<PortfolioWriteDetailOut>) -> ScreenInvestOut {
    ScreenInvestOut {
        as_of,
        cash_outlay: book.cash_outlay,
        exposure: book.exposure,
        leverage: book.leverage,
        mtf: book.mtf,
        min_investment: book.min_investment,
        leftover: book.leftover,
        empty_reason: book.empty_reason,
        holdings: book.holdings,
        skipped: book.skipped,
        report,
        orders: Vec::new(),
    }
}

fn parse_invest_body(
    received: &Received,
    require_name: bool,
    errors: &mut Errors,
) -> Option<InvestBody> {
    let entries = match received {
        Received::Missing | Received::Json(OrderedJson::Null) => {
            errors.push("<root>", "Field required", "missing");
            return None;
        }
        Received::Raw => {
            errors.push(
                "<root>",
                "Input should be a valid dictionary or object to extract fields from",
                "model_attributes_type",
            );
            return None;
        }
        Received::Json(v) => model::object(v, "", errors)?,
    };
    let name = if require_name {
        model::required_string(entries, "name", "", (Some(1), Some(120)), errors)
    } else {
        match model::get(entries, "name") {
            None | Some(OrderedJson::Null) => Some(String::new()),
            Some(v) => model::string(v, "name", Some(1), Some(120), errors),
        }
    };
    let amount = match model::get(entries, "amount") {
        None => {
            errors.push("amount", "Field required", "missing");
            None
        }
        Some(v) => model::decimal(v, "amount", true, errors),
    };
    let mtf = match model::get(entries, "mtf") {
        None | Some(OrderedJson::Null) => Some(false),
        Some(v) => parse_bool(v, "mtf", errors),
    };
    let leverage = match model::get(entries, "leverage") {
        None | Some(OrderedJson::Null) => Some(dec(DEFAULT_LEVERAGE)),
        Some(v) => model::decimal(v, "leverage", true, errors),
    };
    let parent_id = model::optional_int(entries, "parent_id", "", Bounds::default(), errors);
    let broker = model::optional_int(
        entries,
        "broker_account_id",
        "",
        Bounds::default(),
        errors,
    );
    model::forbid_extra(
        entries,
        &[
            "name",
            "amount",
            "mtf",
            "leverage",
            "parent_id",
            "broker_account_id",
        ],
        "",
        errors,
    );
    match (name, amount, mtf, leverage, parent_id, broker) {
        (Some(name), Some(amount), Some(mtf), Some(leverage), Ok(parent_id), Ok(broker)) => {
            Some(InvestBody {
                name: {
                    let trimmed = name.trim();
                    if require_name || !trimmed.is_empty() {
                        Some(if require_name {
                            name
                        } else {
                            trimmed.to_owned()
                        })
                    } else {
                        None
                    }
                },
                amount,
                mtf,
                leverage,
                parent_id,
                broker,
            })
        }
        _ => None,
    }
}

async fn latest_run(
    conn: &mut PgConnection,
    screen_id: i64,
) -> Result<Option<(NaiveDate, OrderedJson)>, Problem> {
    let row = sqlx::query(
        "SELECT as_of, results FROM screen_run WHERE screen_id = $1 \
         ORDER BY as_of DESC, id DESC LIMIT 1",
    )
    .bind(screen_id)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db::fail)?;
    let Some(row) = row else {
        return Ok(None);
    };
    Ok(Some((db::date(&row, "as_of")?, db::json(&row, "results")?)))
}

fn ranks_from(results: &OrderedJson) -> Vec<(i64, i64)> {
    let OrderedJson::Array(items) = results else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for item in items {
        let Some(id) = item.get("instrument_id").and_then(json_i64) else {
            continue;
        };
        let rank = item.get("rank").and_then(json_i64).unwrap_or(i64::MAX);
        out.push((id, rank));
    }
    out.sort_by_key(|(_, rank)| *rank);
    out
}

async fn candidates_for(
    conn: &mut PgConnection,
    results: &OrderedJson,
    as_of: NaiveDate,
) -> Result<Vec<InvestCandidate>, Problem> {
    let ranks = ranks_from(results);
    if ranks.is_empty() {
        return Ok(Vec::new());
    }
    let ids: Vec<i64> = ranks.iter().map(|(id, _)| *id).collect();
    let instruments = sqlx::query(
        "SELECT i.id, i.symbol, i.name, i.series, \
                COALESCE( \
                  k.symbol, \
                  CASE WHEN i.series IS NOT NULL AND i.series <> '' AND i.series <> 'EQ' \
                       THEN i.symbol || '-' || i.series ELSE i.symbol END \
                ) AS tradingsymbol \
         FROM instrument i \
         LEFT JOIN LATERAL ( \
           SELECT symbol FROM instrument \
           WHERE kite_token IS NOT NULL AND is_active \
             AND (symbol = i.symbol OR symbol = i.symbol || '-' || i.series) \
           ORDER BY CASE WHEN symbol = i.symbol THEN 0 ELSE 1 END \
           LIMIT 1 \
         ) k ON true \
         WHERE i.id = ANY($1)",
    )
    .bind(&ids)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut meta: HashMap<i64, (String, String, String)> = HashMap::new();
    for row in &instruments {
        meta.insert(
            db::get(row, "id")?,
            (
                db::get(row, "symbol")?,
                db::get(row, "name")?,
                db::get(row, "tradingsymbol")?,
            ),
        );
    }
    let prices = sqlx::query(
        "SELECT instrument_id, close_raw FROM ( \
            SELECT instrument_id, close_raw, \
                   row_number() OVER (PARTITION BY instrument_id ORDER BY date DESC) AS recency \
            FROM ohlcv_daily \
            WHERE instrument_id = ANY($1) AND date <= $2 \
         ) priced WHERE recency = 1",
    )
    .bind(&ids)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut close: HashMap<i64, WideDecimal> = HashMap::new();
    for row in &prices {
        if let Some(px) = db::opt_dec(row, "close_raw")? {
            close.insert(db::get(row, "instrument_id")?, px);
        }
    }
    Ok(ranks
        .into_iter()
        .filter_map(|(id, rank)| {
            let (symbol, name, tradingsymbol) = meta.get(&id)?.clone();
            Some(InvestCandidate {
                symbol,
                name,
                rank,
                price: close.get(&id).cloned(),
                tradingsymbol,
            })
        })
        .collect())
}

async fn preview_on(
    conn: &mut PgConnection,
    screen: &ScreenRow,
    amount: &WideDecimal,
    mtf: bool,
    leverage: &WideDecimal,
) -> Result<(Option<NaiveDate>, SizedBook), Problem> {
    let Some((as_of, results)) = latest_run(conn, screen.id).await? else {
        return Ok((
            None,
            SizedBook {
                cash_outlay: money(amount),
                exposure: money(&dec(0)),
                leverage: if mtf { leverage.clone() } else { dec(1) },
                mtf,
                min_investment: money(&dec(0)),
                leftover: money(amount),
                empty_reason: Some(
                    "Wait for the screen to finish ranking before investing.".to_owned(),
                ),
                holdings: Vec::new(),
                skipped: Vec::new(),
            },
        ));
    };
    let _ = leverage;
    let rows = candidates_for(conn, &results, as_of).await?;
    let factors = if mtf {
        broker_factors(&rows).await?
    } else {
        HashMap::new()
    };
    Ok((Some(as_of), size_book(&rows, amount, mtf, &factors)?))
}

async fn broker_factors(rows: &[InvestCandidate]) -> Result<HashMap<String, WideDecimal>, Problem> {
    let lines = probe_lines(rows);
    if lines.is_empty() {
        return Ok(HashMap::new());
    }
    let client = kite_for_margins()?;
    let mut out = HashMap::new();
    for (symbol, tradingsymbol, price) in lines {
        if let Some(cached) = cached_factor(&symbol) {
            out.insert(symbol, cached);
            continue;
        }
        let order = serde_json::json!({
            "exchange": "NSE",
            "tradingsymbol": tradingsymbol,
            "transaction_type": "BUY",
            "variety": "regular",
            "product": "MTF",
            "order_type": "LIMIT",
            "quantity": 1,
            "price": price,
        });
        let body = match baskfy_providers::kite::KiteValue::from_json(order.to_string().as_bytes()) {
            Ok(v) => v,
            Err(e) => return Err(refuse(e.to_string())),
        };
        match client.margins_orders(&[body]).await {
            Ok(reply) => {
                let lev = leverage_from_margin(&reply, price);
                remember_factor(&symbol, lev.clone());
                out.insert(symbol, lev);
            }
            Err(error) => {
                let message = error.to_string();
                if margin_auth_failure(&message) {
                    return Err(refuse(message));
                }
                remember_factor(&symbol, dec(1));
                out.insert(symbol, dec(1));
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(350)).await;
    }
    Ok(out)
}

fn probe_lines(rows: &[InvestCandidate]) -> Vec<(String, String, f64)> {
    let mut seen = HashSet::new();
    let mut ranked: Vec<&InvestCandidate> = rows.iter().collect();
    ranked.sort_by_key(|row| row.rank);
    let mut lines = Vec::new();
    for row in ranked {
        let symbol = row.symbol.trim().to_ascii_uppercase();
        let Some(price) = row.price.as_ref().filter(|p| **p > dec(0)) else {
            continue;
        };
        if symbol.is_empty() || !seen.insert(symbol.clone()) {
            continue;
        }
        let px = price.to_sci_string().parse::<f64>().unwrap_or(0.0);
        if px <= 0.0 {
            continue;
        }
        lines.push((symbol, row.tradingsymbol.clone(), px));
        if lines.len() == DEFAULT_TOP_N {
            break;
        }
    }
    lines
}

fn kite_for_margins() -> Result<baskfy_providers::kite::KiteConnect, Problem> {
    use baskfy_providers::kite::KiteClient;
    use baskfy_providers::secret::Secret;
    use baskfy_providers::tokens::AccessTokenStore;
    let key = std::env::var("BASKFY_KITE_API_KEY").unwrap_or_default();
    let path = std::env::var("BASKFY_KITE_TOKEN_PATH").unwrap_or_default();
    let encryption = std::env::var("BASKFY_KITE_TOKEN_ENCRYPTION_KEY").unwrap_or_default();
    if key.is_empty() || path.is_empty() || encryption.is_empty() {
        return Err(refuse(
            "Kite is not configured, so each stock's MTF leverage cannot be read",
        ));
    }
    let store = AccessTokenStore::new(path, Secret::new(encryption));
    let token = store.require_fresh(None).map_err(|e| refuse(e.to_string()))?;
    let client = baskfy_providers::kite::KiteConnect::new(&key, Some(7.0))
        .map_err(|e| refuse(e.to_string()))?;
    client.set_access_token(&token.value);
    Ok(client)
}

fn margin_auth_failure(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    lower.contains("token")
        || lower.contains("api_key")
        || lower.contains("session")
        || lower.contains("incorrect `api_key`")
}

fn leverage_from_margin(reply: &baskfy_providers::kite::KiteValue, price: f64) -> WideDecimal {
    use baskfy_providers::kite::KiteValue;
    let map = match reply {
        KiteValue::List(items) => match items.first() {
            Some(KiteValue::Map(map)) => map,
            _ => return dec(1),
        },
        KiteValue::Map(map) => map,
        _ => return dec(1),
    };
    let stated = margin_number(map.get("leverage"));
    let total = margin_number(map.get("total"));
    let lev = if stated > 1.05 {
        stated
    } else if total > 0.0 && price > 0.0 && total < price * 0.98 {
        price / total
    } else {
        1.0
    };
    let clamped = if lev.is_finite() && lev > 1.05 {
        lev.min(10.0)
    } else {
        1.0
    };
    format!("{clamped:.2}")
        .parse::<WideDecimal>()
        .unwrap_or_else(|_| dec(1))
}

fn margin_number(value: Option<&baskfy_providers::kite::KiteValue>) -> f64 {
    use baskfy_providers::kite::KiteValue;
    match value {
        Some(KiteValue::Float(v)) => *v,
        Some(KiteValue::Int(v)) => *v as f64,
        Some(KiteValue::Str(s)) => s.parse().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn factor_cache() -> &'static std::sync::Mutex<HashMap<String, (WideDecimal, std::time::Instant)>> {
    static CACHE: std::sync::LazyLock<
        std::sync::Mutex<HashMap<String, (WideDecimal, std::time::Instant)>>,
    > = std::sync::LazyLock::new(|| std::sync::Mutex::new(HashMap::new()));
    &CACHE
}

fn cached_factor(symbol: &str) -> Option<WideDecimal> {
    let Ok(cache) = factor_cache().lock() else {
        return None;
    };
    let (lev, at) = cache.get(symbol)?;
    (at.elapsed().as_secs() < 30 * 60).then(|| lev.clone())
}

fn remember_factor(symbol: &str, leverage: WideDecimal) {
    if let Ok(mut cache) = factor_cache().lock() {
        cache.insert(symbol.to_owned(), (leverage, std::time::Instant::now()));
    }
}

/// Capital of filled lines, for the screen sleeve.
fn sleeve_capital(book: &SizedBook) -> WideDecimal {
    money(&book.exposure)
}

async fn attach_screen_sleeve(
    conn: &mut PgConnection,
    portfolio_id: i64,
    screen: &ScreenRow,
    book: &SizedBook,
) -> Result<(), Problem> {
    let top_n = i16::try_from(book.holdings.len().clamp(1, 100)).unwrap_or(20);
    let name: String = screen.name.chars().take(80).collect();
    sqlx::query(
        "INSERT INTO portfolio_sleeve (portfolio_id, name, kind, screen_id, capital, top_n, \
         sort_order) VALUES ($1, $2, 'screen', $3, $4::numeric, $5, 0)",
    )
    .bind(portfolio_id)
    .bind(&name)
    .bind(screen.id)
    .bind(sleeve_capital(book).to_sci_string())
    .bind(top_n)
    .execute(&mut *conn)
    .await
    .map_err(db::fail)?;
    Ok(())
}

async fn write_holdings(
    conn: &mut PgConnection,
    portfolio: &OwnedPortfolio,
    book: &SizedBook,
) -> Result<ImportReportOut, Problem> {
    let holdings = persistable(book);
    if holdings.len() > MAX_IMPORT_ROWS {
        return Err(too_many(holdings.len()));
    }
    apply(conn, portfolio, &rows_from_body(&holdings)).await
}

/// `POST /screens/{public_id}/invest-preview`: size only.
///
/// # Errors
/// 400 for the body; 401 anonymous; 404 when the screen is not visible; 500 on a database failure.
pub async fn invest_preview(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let (_, content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let mut errors = Errors::default();
    let body = parse_invest_body(&received, false, &mut errors);
    errors.finish()?;
    let Some(body) = body else {
        return Err(crate::problems::internal_error());
    };
    let screen = load_visible(&state, &public_id, &principal).await?;
    let mut conn = db::acquire(&state).await?;
    let (as_of, book) =
        preview_on(&mut conn, &screen, &body.amount, body.mtf, &body.leverage).await?;
    Ok(wire::json(&out_of(book, as_of, None)))
}

/// `POST /screens/{public_id}/invest`: size and create a dedicated `MY_SCREEN` portfolio.
///
/// # Errors
/// 400 for the body or an empty book; 401; 404; 500.
pub async fn invest_from_screen(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let (_, content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let mut errors = Errors::default();
    let body = parse_invest_body(&received, true, &mut errors);
    errors.finish()?;
    let Some(body) = body else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    let write = pool(write)?;
    let public_id = public_id.clone();
    let (seed, orders) = if body.mtf {
        let mut conn = db::acquire(&state).await?;
        let screen = load_visible_on(&mut conn, &public_id, &principal).await?;
        let (as_of, book) =
            preview_on(&mut conn, &screen, &body.amount, true, &body.leverage).await?;
        drop(conn);
        if let Some(reason) = &book.empty_reason {
            return Err(refuse(reason.clone()));
        }
        let orders = send_screen_mtf(&book.holdings).await;
        let all_placed = !orders.is_empty() && orders.iter().all(|row| row.status == "PLACED");
        if !all_placed {
            let mut out = out_of(book, as_of, None);
            out.orders = orders;
            out.empty_reason = Some(
                "No portfolio was created. A market buy did not confirm, so later names were not sent."
                    .to_owned(),
            );
            return Ok(wire::json_status(StatusCode::CONFLICT, &out));
        }
        (Some((as_of, book)), orders)
    } else {
        (None, Vec::new())
    };
    let mut out = session::run(&write, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let screen = load_visible_on(conn, &public_id, &principal).await?;
            let (as_of, book) = if let Some((as_of, book)) = seed {
                (as_of, book)
            } else {
                let (as_of, book) =
                    preview_on(conn, &screen, &body.amount, body.mtf, &body.leverage).await?;
                if let Some(reason) = &book.empty_reason {
                    return Err(refuse(reason.clone()));
                }
                (as_of, book)
            };
            let name = body.name.unwrap_or_else(|| screen.name.clone());
            let parent = match body.parent_id {
                Some(raw) => Some(load(conn, raw, user_id).await?.id),
                None => None,
            };
            let broker_account_id = broker_account_for(conn, body.broker, user_id).await?;
            let portfolio = insert_portfolio(
                conn,
                user_id,
                &name,
                parent,
                broker_account_id,
                "MY_SCREEN",
            )
            .await?;
            let report = write_holdings(conn, &portfolio, &book).await?;
            attach_screen_sleeve(conn, portfolio.id, &screen, &book).await?;
            let detail = PortfolioWriteDetailOut {
                portfolio: portfolio_out(conn, &portfolio).await?,
                report,
            };
            Ok(out_of(book, as_of, Some(detail)))
        })
    })
    .await?;
    out.orders = orders;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

async fn send_screen_mtf(holdings: &[ScreenInvestHoldingOut]) -> Vec<ScreenOrderOut> {
    let client = match kite_for_margins() {
        Ok(client) => client,
        Err(error) => {
            return vec![ScreenOrderOut {
                symbol: String::new(),
                status: "ERROR".to_owned(),
                order_id: None,
                stop: None,
                error: Some(error.detail.clone()),
            }];
        }
    };
    let mut orders = Vec::new();
    let mut stopped = false;
    for row in holdings {
        let Some(qty) = row.quantity.filter(|qty| *qty > 0) else {
            continue;
        };
        let Some(price) = row
            .price
            .as_ref()
            .and_then(|px| px.to_sci_string().parse::<f64>().ok())
            .filter(|price| *price > 0.0)
        else {
            continue;
        };
        if stopped {
            orders.push(ScreenOrderOut {
                symbol: row.symbol.clone(),
                status: "NOT_SENT".to_owned(),
                order_id: None,
                stop: None,
                error: Some("Not sent. An earlier buy did not confirm.".to_owned()),
            });
            continue;
        }
        let product = if row.product == "MTF" { "MTF" } else { "CNC" };
        let fields = vec![
            ("exchange".to_owned(), "NSE".to_owned()),
            ("tradingsymbol".to_owned(), row.tradingsymbol.clone()),
            ("transaction_type".to_owned(), "BUY".to_owned()),
            ("order_type".to_owned(), "MARKET".to_owned()),
            ("quantity".to_owned(), qty.to_string()),
            ("product".to_owned(), product.to_owned()),
            ("validity".to_owned(), "DAY".to_owned()),
            ("market_protection".to_owned(), "-1".to_owned()),
        ];
        match client.post_form("/orders/regular", &fields).await {
            Ok(reply) => {
                let order_id = kite_text(&reply, "order_id");
                if order_id.is_empty() {
                    stopped = true;
                    orders.push(ScreenOrderOut {
                        symbol: row.symbol.clone(),
                        status: "ERROR".to_owned(),
                        order_id: None,
                        stop: None,
                        error: Some("Kite did not confirm an order id.".to_owned()),
                    });
                    continue;
                }
                let stop = place_stop(&client, &row.tradingsymbol, qty, price, product).await;
                orders.push(ScreenOrderOut {
                    symbol: row.symbol.clone(),
                    status: "PLACED".to_owned(),
                    order_id: Some(order_id),
                    stop: Some(stop),
                    error: None,
                });
            }
            Err(error) => {
                stopped = true;
                orders.push(ScreenOrderOut {
                    symbol: row.symbol.clone(),
                    status: "ERROR".to_owned(),
                    order_id: None,
                    stop: None,
                    error: Some(error.to_string()),
                });
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(350)).await;
    }
    if orders.is_empty() {
        return vec![ScreenOrderOut {
            symbol: String::new(),
            status: "ERROR".to_owned(),
            order_id: None,
            stop: None,
            error: Some("No share quantity was large enough to send".to_owned()),
        }];
    }
    orders
}

async fn place_stop(
    client: &baskfy_providers::kite::KiteConnect,
    symbol: &str,
    qty: i64,
    price: f64,
    product: &str,
) -> String {
    let trigger = (price * 0.92 * 10.0).round() / 10.0;
    let limit = (trigger * 0.99 * 10.0).round() / 10.0;
    let condition = serde_json::json!({
        "exchange": "NSE",
        "tradingsymbol": symbol,
        "trigger_values": [trigger],
        "last_price": price,
    });
    let legs = serde_json::json!([{
        "exchange": "NSE",
        "tradingsymbol": symbol,
        "transaction_type": "SELL",
        "quantity": qty,
        "order_type": "LIMIT",
        "product": product,
        "price": limit,
    }]);
    let fields = vec![
        ("type".to_owned(), "single".to_owned()),
        ("condition".to_owned(), condition.to_string()),
        ("orders".to_owned(), legs.to_string()),
    ];
    match client.post_form("/gtt/triggers", &fields).await {
        Ok(reply) => {
            let id = kite_text(&reply, "trigger_id");
            if id.is_empty() { "ARMED".to_owned() } else { format!("ARMED {id}") }
        }
        Err(error) => format!("STOP {}", error),
    }
}

fn kite_text(value: &baskfy_providers::kite::KiteValue, key: &str) -> String {
    use baskfy_providers::kite::KiteValue;
    let KiteValue::Map(map) = value else {
        return String::new();
    };
    match map.get(key) {
        Some(KiteValue::Str(text)) => text.clone(),
        Some(KiteValue::Int(n)) => n.to_string(),
        _ => String::new(),
    }
}
async fn linked_screen(
    conn: &mut PgConnection,
    portfolio_id: i64,
    user_id: i64,
    principal: &Principal,
) -> Result<ScreenRow, Problem> {
    let public_id: Option<String> = sqlx::query_scalar(
        "SELECT sc.public_id FROM portfolio_sleeve s \
         JOIN screen sc ON sc.id = s.screen_id \
         WHERE s.portfolio_id = $1 AND s.kind = 'screen' AND s.screen_id IS NOT NULL \
         ORDER BY s.sort_order, s.id LIMIT 1",
    )
    .bind(portfolio_id)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db::fail)?;
    let Some(public_id) = public_id else {
        return Err(refuse(
            "This portfolio is not linked to a screen. Invest from a screen first.",
        ));
    };
    let screen = load_visible_on(conn, &public_id, principal).await?;
    let _ = user_id;
    Ok(screen)
}

/// `POST /portfolios/{portfolio_id}/from-screen`: replace holdings from the linked screen.
///
/// # Errors
/// 400 for the body, an unlinked book, or an empty size; 401; 404; 500.
pub async fn rebalance_from_screen(
    State(_state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let (_, content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let mut errors = Errors::default();
    let id = model::int(
        &OrderedJson::Str(raw_id.clone()),
        "portfolio_id",
        Bounds::default(),
        &mut errors,
    );
    let body = parse_invest_body(&received, false, &mut errors);
    errors.finish()?;
    let (Some(id), Some(body)) = (id, body) else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    let write = pool(write)?;
    let out = session::run(&write, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let portfolio = load(conn, id, user_id).await?;
            let screen = linked_screen(conn, portfolio.id, user_id, &principal).await?;
            let (as_of, book) =
                preview_on(conn, &screen, &body.amount, body.mtf, &body.leverage).await?;
            if let Some(reason) = &book.empty_reason {
                return Err(refuse(reason.clone()));
            }
            let report = write_holdings(conn, &portfolio, &book).await?;
            sqlx::query(
                "UPDATE portfolio_sleeve SET capital = $2::numeric, top_n = $3 \
                 WHERE portfolio_id = $1 AND kind = 'screen' AND screen_id = $4",
            )
            .bind(portfolio.id)
            .bind(sleeve_capital(&book).to_sci_string())
            .bind(i16::try_from(book.holdings.len().clamp(1, 100)).unwrap_or(20))
            .bind(screen.id)
            .execute(&mut *conn)
            .await
            .map_err(db::fail)?;
            let _holdings = holdings_out(conn, portfolio.id).await?;
            let detail = PortfolioWriteDetailOut {
                portfolio: portfolio_out(conn, &portfolio).await?,
                report,
            };
            Ok(out_of(book, as_of, Some(detail)))
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// Optional `screen_public_id` / `amount` / `mtf` / `leverage` on `POST /portfolios`.
pub(crate) struct CreateScreenLink {
    /// Visible screen to attach.
    pub screen_public_id: Option<String>,
    /// When set, holdings are sized here instead of taken from the body.
    pub amount: Option<WideDecimal>,
    /// Paper MTF.
    pub mtf: bool,
    /// Leverage when `mtf` is on.
    pub leverage: WideDecimal,
}

impl Default for CreateScreenLink {
    fn default() -> Self {
        Self {
            screen_public_id: None,
            amount: None,
            mtf: false,
            leverage: dec(DEFAULT_LEVERAGE),
        }
    }
}

/// Parse the optional screen-invest fields on `POST /portfolios`.
pub(crate) fn parse_create_link(
    entries: &[(String, OrderedJson)],
    errors: &mut Errors,
) -> Result<CreateScreenLink, ()> {
    let screen_public_id = match model::get(entries, "screen_public_id") {
        None | Some(OrderedJson::Null) => Ok(None),
        Some(v) => model::string(v, "screen_public_id", Some(1), Some(64), errors)
            .map(Some)
            .ok_or(()),
    }?;
    let amount = model::optional_decimal(entries, "amount", "", true, errors)?;
    let mtf = match model::get(entries, "mtf") {
        None | Some(OrderedJson::Null) => false,
        Some(v) => parse_bool(v, "mtf", errors).ok_or(())?,
    };
    let leverage = match model::get(entries, "leverage") {
        None | Some(OrderedJson::Null) => dec(DEFAULT_LEVERAGE),
        Some(v) => model::decimal(v, "leverage", true, errors).ok_or(())?,
    };
    if mtf && amount.is_none() {
        errors.push(
            "amount",
            "MTF sizing needs an amount",
            "value_error",
        );
        return Err(());
    }
    Ok(CreateScreenLink {
        screen_public_id,
        amount,
        mtf,
        leverage,
    })
}

/// Size from a screen when `POST /portfolios` carried `amount`, else `None`.
pub(crate) async fn sized_holdings_for_create(
    conn: &mut PgConnection,
    principal: &Principal,
    link: &CreateScreenLink,
) -> Result<Option<(ScreenRow, SizedBook, Vec<HoldingIn>)>, Problem> {
    let Some(public_id) = &link.screen_public_id else {
        return Ok(None);
    };
    let screen = load_visible_on(conn, public_id, principal).await?;
    let Some(amount) = &link.amount else {
        return Ok(Some((
            screen,
            SizedBook {
                cash_outlay: money(&dec(0)),
                exposure: money(&dec(0)),
                leverage: dec(1),
                mtf: false,
                min_investment: money(&dec(0)),
                leftover: money(&dec(0)),
                empty_reason: None,
                holdings: Vec::new(),
                skipped: Vec::new(),
            },
            Vec::new(),
        )));
    };
    let (_, book) = preview_on(conn, &screen, amount, link.mtf, &link.leverage).await?;
    if let Some(reason) = &book.empty_reason {
        return Err(refuse(reason.clone()));
    }
    let holdings = persistable(&book);
    Ok(Some((screen, book, holdings)))
}

/// Attach the sleeve after a `MY_SCREEN` create.
pub(crate) async fn attach_if_screen(
    conn: &mut PgConnection,
    portfolio_id: i64,
    screen: &ScreenRow,
    book: Option<&SizedBook>,
    holdings_count: usize,
) -> Result<(), Problem> {
    let fallback = SizedBook {
        cash_outlay: money(&dec(0)),
        exposure: money(&dec(0)),
        leverage: dec(1),
        mtf: false,
        min_investment: money(&dec(0)),
        leftover: money(&dec(0)),
        empty_reason: None,
        holdings: Vec::new(),
        skipped: Vec::new(),
    };
    let mut use_book = book;
    let synthesized;
    if use_book.is_none() {
        synthesized = SizedBook {
            holdings: (0..holdings_count.max(1))
                .map(|i| ScreenInvestHoldingOut {
                    symbol: String::new(),
                    name: String::new(),
                    rank: i64::try_from(i).unwrap_or(0),
                    quantity: None,
                    amount: money(&dec(0)),
                    weight: money(&dec(0)),
                    price: None,
                    leverage: dec(1),
                    product: "CNC".to_owned(),
                    tradingsymbol: String::new(),
                })
                .collect(),
            ..fallback
        };
        use_book = Some(&synthesized);
    }
    if let Some(book) = use_book {
        attach_screen_sleeve(conn, portfolio_id, screen, book).await?;
    }
    Ok(())
}

/// `portfolio_id -> screen public_id` from the first screen sleeve.
pub(crate) async fn screen_links(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<HashMap<i64, String>, Problem> {
    let rows = sqlx::query(
        "SELECT DISTINCT ON (s.portfolio_id) s.portfolio_id, sc.public_id \
         FROM portfolio_sleeve s \
         JOIN screen sc ON sc.id = s.screen_id \
         JOIN portfolio p ON p.id = s.portfolio_id \
         WHERE p.user_id = $1 AND s.kind = 'screen' AND s.screen_id IS NOT NULL \
         ORDER BY s.portfolio_id, s.sort_order, s.id",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut out = HashMap::new();
    for row in &rows {
        out.insert(db::get(row, "portfolio_id")?, db::get(row, "public_id")?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    fn px(symbol: &str, rank: i64, price: i64, series: &str) -> InvestCandidate {
        let tradingsymbol = if series.is_empty() || series.eq_ignore_ascii_case("EQ") {
            symbol.to_owned()
        } else {
            format!("{symbol}-{series}")
        };
        InvestCandidate {
            symbol: symbol.to_owned(),
            name: symbol.to_owned(),
            rank,
            price: Some(dec(price)),
            tradingsymbol,
        }
    }

    #[test]
    fn mtf_uses_each_brokers_leverage_and_keeps_cash_names() {
        let rows = [
            px("AAA", 1, 100, "EQ"),
            px("BBB", 2, 100, "EQ"),
            px("CCC", 3, 100, "BE"),
        ];
        let mut factors = HashMap::new();
        factors.insert("AAA".to_owned(), dec(4));
        factors.insert("BBB".to_owned(), dec(2));
        let book = size_book(&rows, &dec(10_000), true, &factors).unwrap();
        assert!(book.mtf);
        assert!(book.exposure > book.cash_outlay);
        let aaa = book.holdings.iter().find(|h| h.symbol == "AAA").unwrap();
        let bbb = book.holdings.iter().find(|h| h.symbol == "BBB").unwrap();
        let ccc = book.holdings.iter().find(|h| h.symbol == "CCC").unwrap();
        assert_eq!(aaa.product, "MTF");
        assert_eq!(aaa.leverage.to_sci_string(), "4.00");
        assert_eq!(bbb.product, "MTF");
        assert_eq!(bbb.leverage.to_sci_string(), "2.00");
        assert_eq!(ccc.product, "CNC");
        assert_eq!(ccc.leverage.to_sci_string(), "1.00");
        assert_eq!(ccc.tradingsymbol, "CCC-BE");
        assert_eq!(aaa.amount.to_sci_string(), bbb.amount.to_sci_string());
        assert_eq!(bbb.amount.to_sci_string(), ccc.amount.to_sci_string());
        assert!(book.skipped.is_empty());
    }

    #[test]
    fn cash_mode_sizes_non_eq() {
        let rows = [px("BE1", 1, 50, "BE"), px("BE2", 2, 50, "BE")];
        let book = size_book(&rows, &dec(10_000), false, &HashMap::new()).unwrap();
        assert!(!book.mtf);
        assert_eq!(book.leverage.to_sci_string(), "1");
        assert_eq!(book.holdings.len(), 2);
        assert!(book.skipped.is_empty());
    }
}
