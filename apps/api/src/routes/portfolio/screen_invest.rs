//! Invest / rebalance from a saved screen.
//!
//! Sizing is [`baskfy_core::gross_allocation`]: one gross position value per name, with MTF
//! changing only the cash that value needs. Preview stores that plan. Confirm sends those
//! quantities, or returns the plan for another look when prices or leverage moved.
//! Cash invest writes the paper book and does not call the broker.

use std::collections::{HashMap, HashSet};

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::gross_allocation::{
    self, AllocInput, AllocName, AllocOutput, allocate_equal_gross, allocate_with_required,
    scaled_cash,
};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use serde::Serialize;
use sha2::{Digest, Sha256};
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
/// Parsed when a caller still sends `leverage`. Sizing does not use it.
const DEFAULT_LEVERAGE: i64 = 4;
const MAX_LEVERAGE: i64 = 10;
const PLAN_REPLAY: Duration = Duration::from_secs(120);
const PLAN_CAP: usize = 128;

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
    /// Shares per board lot. NSE cash is 1 until a lot column is on the instrument.
    lot_size: i64,
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
    /// `MTF` when leverage is above 1, otherwise `CNC`.
    pub product: String,
    /// Cash this name needs from the investor.
    #[serde(serialize_with = "num::ser")]
    pub contribution: WideDecimal,
    /// Broker funding. Zero on a cash row.
    #[serde(serialize_with = "num::ser")]
    pub funding: WideDecimal,
    /// Position value minus the common target, after lot flooring.
    #[serde(serialize_with = "num::ser")]
    pub deviation: WideDecimal,
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
    /// Notional not used after lot flooring. Reserve and costs are separate.
    #[serde(serialize_with = "num::ser")]
    pub leftover: WideDecimal,
    /// Explicit cash reserve. Zero when none was configured.
    #[serde(serialize_with = "num::ser")]
    pub reserve: WideDecimal,
    /// Explicit estimated costs. Zero when none were supplied.
    #[serde(serialize_with = "num::ser")]
    pub costs: WideDecimal,
    /// Sum of investor cash across filled names.
    #[serde(serialize_with = "num::ser")]
    pub contribution: WideDecimal,
    /// Sum of broker funding.
    #[serde(serialize_with = "num::ser")]
    pub funding: WideDecimal,
    /// Common position value before lot flooring.
    #[serde(serialize_with = "num::ser")]
    pub gross_target: WideDecimal,
    /// Stable id of this allocation. Confirm sends it back.
    pub plan_id: String,
    /// [`gross_allocation::ALLOCATION_VERSION`].
    pub allocation_version: String,
    /// When the prices in this plan were read.
    pub quoted_at: String,
    /// Why the common target was lowered. Names stay equal.
    pub constraint: Option<String>,
    /// This plan was already sent. Confirm did not place it again.
    pub already_sent: bool,
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
    /// Shares the broker reports filled. Absent until the order is read back.
    pub filled_quantity: Option<i64>,
}

#[derive(Debug, Clone)]
struct InvestBody {
    name: Option<String>,
    amount: WideDecimal,
    mtf: bool,
    /// Still accepted on the request. Sizing uses each name's verified leverage, not this.
    #[allow(dead_code)]
    leverage: WideDecimal,
    reserve: WideDecimal,
    costs: WideDecimal,
    plan_id: String,
    parent_id: Option<model::PyInt>,
    broker: Option<model::PyInt>,
}

#[derive(Debug, Clone)]
pub(crate) struct SizedBook {
    cash_outlay: WideDecimal,
    exposure: WideDecimal,
    leverage: WideDecimal,
    mtf: bool,
    min_investment: WideDecimal,
    leftover: WideDecimal,
    reserve: WideDecimal,
    costs: WideDecimal,
    contribution: WideDecimal,
    funding: WideDecimal,
    gross_target: WideDecimal,
    plan_id: String,
    allocation_version: String,
    quoted_at: String,
    constraint: Option<String>,
    empty_reason: Option<String>,
    holdings: Vec<ScreenInvestHoldingOut>,
    skipped: Vec<ScreenInvestSkip>,
}

fn bare_book(amount: &WideDecimal, mtf: bool) -> SizedBook {
    SizedBook {
        cash_outlay: money(amount),
        exposure: money(&dec(0)),
        leverage: dec(1),
        mtf,
        min_investment: money(&dec(0)),
        leftover: money(amount),
        reserve: money(&dec(0)),
        costs: money(&dec(0)),
        contribution: money(&dec(0)),
        funding: money(&dec(0)),
        gross_target: money(&dec(0)),
        plan_id: String::new(),
        allocation_version: gross_allocation::ALLOCATION_VERSION.to_owned(),
        quoted_at: String::new(),
        constraint: None,
        empty_reason: None,
        holdings: Vec::new(),
        skipped: Vec::new(),
    }
}

fn blank_holding(rank: i64) -> ScreenInvestHoldingOut {
    ScreenInvestHoldingOut {
        symbol: String::new(),
        name: String::new(),
        rank,
        quantity: None,
        amount: money(&dec(0)),
        weight: money(&dec(0)),
        price: None,
        leverage: dec(1),
        product: "CNC".to_owned(),
        contribution: money(&dec(0)),
        funding: money(&dec(0)),
        deviation: money(&dec(0)),
        tradingsymbol: String::new(),
    }
}

fn ceil_rupee(value: &WideDecimal) -> WideDecimal {
    let Ok(down) = value.quantize(0, Rounding::Down) else {
        return value.clone();
    };
    if &down < value {
        down.add(&dec(1))
    } else {
        down
    }
}

fn clamp_leverage(raw: &WideDecimal) -> WideDecimal {
    if *raw <= dec(1) {
        dec(1)
    } else if *raw > dec(MAX_LEVERAGE) {
        dec(MAX_LEVERAGE)
    } else {
        raw.clone()
    }
}

/// Equal gross exposure. `reserve` and `costs` are explicit; nothing else is held back.
///
/// # Errors
/// A non-positive amount, or an allocation the inputs cannot describe.
pub(crate) fn size_book(
    rows: &[InvestCandidate],
    amount: &WideDecimal,
    mtf: bool,
    factors: &HashMap<String, WideDecimal>,
    reserve: &WideDecimal,
    costs: &WideDecimal,
) -> Result<SizedBook, Problem> {
    if *amount <= dec(0) {
        return Err(refuse("amount must be greater than 0"));
    }
    let mut book = bare_book(amount, mtf);
    book.reserve = money(reserve);
    book.costs = money(costs);

    let mut seen = HashSet::new();
    let mut ordered: Vec<&InvestCandidate> = Vec::new();
    let mut ranked: Vec<&InvestCandidate> = rows.iter().collect();
    ranked.sort_by_key(|row| row.rank);
    for row in ranked {
        let symbol = row.symbol.trim().to_ascii_uppercase();
        if symbol.is_empty() || !seen.insert(symbol) {
            continue;
        }
        if !row.price.as_ref().is_some_and(|price| *price > dec(0)) {
            book.skipped.push(ScreenInvestSkip {
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
        book.empty_reason = Some(
            if rows.is_empty() {
                "Nothing survived your filters, so there is nothing to put in a portfolio."
            } else {
                "None of these names have a price, so there is nothing to put in a portfolio."
            }
            .to_owned(),
        );
        return Ok(book);
    }

    let mut missing = Vec::new();
    let mut names = Vec::new();
    let mut meta = Vec::new();
    for row in &ordered {
        let symbol = row.symbol.trim().to_ascii_uppercase();
        let leverage = if mtf {
            match factors.get(&symbol) {
                Some(factor) => clamp_leverage(factor),
                None => {
                    missing.push(symbol);
                    continue;
                }
            }
        } else {
            dec(1)
        };
        let Some(price) = row.price.clone() else {
            continue;
        };
        names.push(AllocName {
            symbol,
            price,
            leverage,
            lot_size: row.lot_size.max(1),
            max_gross: None,
        });
        meta.push(*row);
    }
    if !missing.is_empty() {
        book.empty_reason = Some(format!(
            "Leverage for {} was not verified, so this basket was not sized. Nothing was given a guessed multiple, and the other names were not increased to fill the gap.",
            missing.join(", ")
        ));
        for symbol in &missing {
            if let Some(row) = ordered
                .iter()
                .find(|row| row.symbol.eq_ignore_ascii_case(symbol))
            {
                book.skipped.push(ScreenInvestSkip {
                    symbol: row.symbol.clone(),
                    name: row.name.clone(),
                    reason: "leverage_unverified".to_owned(),
                });
            }
        }
        book.holdings = ordered
            .iter()
            .map(|row| {
                let mut holding = blank_holding(row.rank);
                holding.symbol = row.symbol.clone();
                holding.name = row.name.clone();
                holding.price = row.price.clone().map(|price| money(&price));
                holding.tradingsymbol = row.tradingsymbol.clone();
                holding
            })
            .collect();
        return Ok(book);
    }

    let sized = allocate_equal_gross(&AllocInput {
        budget: amount.clone(),
        reserve: reserve.clone(),
        costs: costs.clone(),
        names,
    })
    .map_err(refuse)?;
    book.exposure = money(&sized.gross_total);
    book.contribution = money(&sized.contribution_total);
    book.funding = money(&sized.funding_total);
    book.leftover = money(&sized.unallocated);
    book.gross_target = money(&sized.target);
    book.min_investment = money(&ceil_rupee(&sized.minimum_budget));
    book.constraint = sized.constraint;
    book.empty_reason = sized.shortfall;
    book.leverage = if !mtf || sized.contribution_total.is_zero() {
        dec(1)
    } else {
        let blended = sized
            .gross_total
            .div(&sized.contribution_total)
            .map_err(|error| refuse(error.to_string()))?;
        num::quantize(&blended, -2)
    };

    let mut weight_acc = dec(0);
    let last = sized.lines.len().saturating_sub(1);
    for (index, line) in sized.lines.iter().enumerate() {
        let Some(row) = meta.get(index).copied() else {
            return Err(refuse("allocation lost a name"));
        };
        let weight = if sized.gross_total > dec(0) {
            if index == last {
                dec(1).sub(&weight_acc)
            } else {
                let share = line
                    .gross
                    .div(&sized.gross_total)
                    .map_err(|error| refuse(error.to_string()))?;
                let share = num::quantize(&share, -4);
                weight_acc = weight_acc.add(&share);
                share
            }
        } else {
            dec(0)
        };
        book.holdings.push(ScreenInvestHoldingOut {
            symbol: row.symbol.clone(),
            name: row.name.clone(),
            rank: row.rank,
            quantity: (line.quantity > 0).then_some(line.quantity),
            amount: money(&line.gross),
            weight: num::quantize(&weight, -4),
            price: Some(money(&line.price)),
            leverage: num::quantize(&line.leverage, -2),
            product: if line.leverage > dec(1) { "MTF" } else { "CNC" }.to_owned(),
            contribution: money(&line.contribution),
            funding: money(&line.funding),
            deviation: money(&line.deviation),
            tradingsymbol: row.tradingsymbol.clone(),
        });
    }
    Ok(book)
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

fn out_of(
    book: SizedBook,
    as_of: Option<NaiveDate>,
    report: Option<PortfolioWriteDetailOut>,
) -> ScreenInvestOut {
    ScreenInvestOut {
        as_of,
        cash_outlay: book.cash_outlay,
        exposure: book.exposure,
        leverage: book.leverage,
        mtf: book.mtf,
        min_investment: book.min_investment,
        leftover: book.leftover,
        reserve: book.reserve,
        costs: book.costs,
        contribution: book.contribution,
        funding: book.funding,
        gross_target: book.gross_target,
        plan_id: book.plan_id,
        allocation_version: book.allocation_version,
        quoted_at: book.quoted_at,
        constraint: book.constraint,
        already_sent: false,
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
    let reserve = match model::optional_decimal(entries, "reserve", "", false, errors) {
        Ok(Some(value)) if value.is_sign_negative() => {
            errors.push(
                "reserve",
                "reserve cannot be negative",
                "greater_than_equal",
            );
            None
        }
        Ok(value) => Some(value.unwrap_or_else(WideDecimal::zero)),
        Err(()) => None,
    };
    let costs = match model::optional_decimal(entries, "costs", "", false, errors) {
        Ok(Some(value)) if value.is_sign_negative() => {
            errors.push("costs", "costs cannot be negative", "greater_than_equal");
            None
        }
        Ok(value) => Some(value.unwrap_or_else(WideDecimal::zero)),
        Err(()) => None,
    };
    let plan_id = match model::get(entries, "plan_id") {
        None | Some(OrderedJson::Null) => Some(String::new()),
        Some(v) => model::string(v, "plan_id", Some(8), Some(80), errors),
    };
    let parent_id = model::optional_int(entries, "parent_id", "", Bounds::default(), errors);
    let broker = model::optional_int(entries, "broker_account_id", "", Bounds::default(), errors);
    model::forbid_extra(
        entries,
        &[
            "name",
            "amount",
            "mtf",
            "leverage",
            "reserve",
            "costs",
            "plan_id",
            "parent_id",
            "broker_account_id",
        ],
        "",
        errors,
    );
    match (
        name, amount, mtf, leverage, reserve, costs, plan_id, parent_id, broker,
    ) {
        (
            Some(name),
            Some(amount),
            Some(mtf),
            Some(leverage),
            Some(reserve),
            Some(costs),
            Some(plan_id),
            Ok(parent_id),
            Ok(broker),
        ) => Some(InvestBody {
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
            reserve,
            costs,
            plan_id,
            parent_id,
            broker,
        }),
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
                lot_size: 1,
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
    reserve: &WideDecimal,
    costs: &WideDecimal,
    fresh: bool,
) -> Result<(Option<NaiveDate>, SizedBook), Problem> {
    let Some((as_of, results)) = latest_run(conn, screen.id).await? else {
        let mut waiting = bare_book(amount, mtf);
        waiting.empty_reason =
            Some("Wait for the screen to finish ranking before investing.".to_owned());
        return Ok((None, waiting));
    };
    let mut rows = candidates_for(conn, &results, as_of).await?;
    let symbols: Vec<String> = rows.iter().map(|row| row.symbol.clone()).collect();
    if let Ok(quotes) = crate::live_prices::live_quotes_by_symbol(&symbols).await {
        for row in &mut rows {
            let key = row.symbol.trim().to_ascii_uppercase();
            if let Some(price) = quotes.get(&key) {
                let wide = WideDecimal::from(*price);
                if wide > dec(0) {
                    row.price = Some(wide);
                }
            }
        }
    }
    let factors = if mtf {
        broker_factors(&rows, fresh).await?
    } else {
        HashMap::new()
    };
    let mut book = size_book(&rows, amount, mtf, &factors, reserve, costs)?;
    book.quoted_at = quote_stamp(Some(as_of));
    remember_plan(&screen.public_id, &mut book);
    Ok((Some(as_of), book))
}

async fn broker_factors(
    rows: &[InvestCandidate],
    fresh: bool,
) -> Result<HashMap<String, WideDecimal>, Problem> {
    let lines = probe_lines(rows);
    if lines.is_empty() {
        return Ok(HashMap::new());
    }
    let client = kite_for_margins()?;
    let mut out = HashMap::new();
    for (symbol, tradingsymbol, price) in lines {
        if !fresh && let Some(cached) = cached_factor(&symbol) {
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
        let body = match baskfy_providers::kite::KiteValue::from_json(order.to_string().as_bytes())
        {
            Ok(v) => v,
            Err(e) => return Err(refuse(e.to_string())),
        };
        match client.margins_orders(&[body]).await {
            Ok(reply) => {
                if let Some(lev) = leverage_from_margin(&reply, price) {
                    remember_factor(&symbol, lev.clone());
                    out.insert(symbol, lev);
                }
            }
            Err(error) => {
                let message = error.to_string();
                if margin_auth_failure(&message) {
                    return Err(refuse(message));
                }
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
    let token = store
        .require_fresh(None)
        .map_err(|e| refuse(e.to_string()))?;
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

fn leverage_from_margin(
    reply: &baskfy_providers::kite::KiteValue,
    price: f64,
) -> Option<WideDecimal> {
    use baskfy_providers::kite::KiteValue;
    let map = match reply {
        KiteValue::List(items) => match items.first() {
            Some(KiteValue::Map(map)) => map,
            _ => return None,
        },
        KiteValue::Map(map) => map,
        _ => return None,
    };
    let block = match map.get("final") {
        Some(KiteValue::Map(inner)) => inner,
        _ => map,
    };
    let stated = margin_number(block.get("leverage")).max(margin_number(map.get("leverage")));
    let total = margin_number(block.get("total")).max(margin_number(map.get("total")));
    let lev = if stated > 1.05 {
        stated
    } else if total > 0.0 && price > 0.0 && total < price * 0.98 {
        price / total
    } else if total > 0.0 {
        1.0
    } else {
        return None;
    };
    let clamped = if lev.is_finite() && lev > 1.05 {
        lev.min(10.0)
    } else if lev.is_finite() {
        1.0
    } else {
        return None;
    };
    format!("{clamped:.2}").parse::<WideDecimal>().ok()
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

fn quote_stamp(as_of: Option<NaiveDate>) -> String {
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis())
        .unwrap_or(0);
    match as_of {
        Some(day) => format!("{day}@{millis}"),
        None => millis.to_string(),
    }
}

fn fingerprint(screen_id: &str, book: &SizedBook) -> String {
    let mut hasher = Sha256::new();
    hasher.update(gross_allocation::ALLOCATION_VERSION.as_bytes());
    hasher.update(screen_id.as_bytes());
    hasher.update(book.cash_outlay.to_sci_string().as_bytes());
    hasher.update(book.reserve.to_sci_string().as_bytes());
    hasher.update(book.costs.to_sci_string().as_bytes());
    for row in &book.holdings {
        hasher.update(row.symbol.as_bytes());
        hasher.update(row.product.as_bytes());
        hasher.update(row.quantity.unwrap_or(0).to_string().as_bytes());
        hasher.update(row.leverage.to_sci_string().as_bytes());
        let price = row
            .price
            .as_ref()
            .map(WideDecimal::to_sci_string)
            .unwrap_or_default();
        hasher.update(price.as_bytes());
    }
    hasher
        .finalize()
        .iter()
        .take(12)
        .fold(String::new(), |mut hex, byte| {
            hex.push_str(&format!("{byte:02x}"));
            hex
        })
}

struct StoredPlan {
    screen_id: String,
    book: SizedBook,
    orders: Option<Vec<ScreenOrderOut>>,
    portfolio_id: Option<i64>,
    sending: bool,
    at: Instant,
}

fn plan_store() -> &'static Mutex<HashMap<String, StoredPlan>> {
    static STORE: std::sync::LazyLock<Mutex<HashMap<String, StoredPlan>>> =
        std::sync::LazyLock::new(|| Mutex::new(HashMap::new()));
    &STORE
}

fn remember_plan(screen_id: &str, book: &mut SizedBook) {
    book.allocation_version = gross_allocation::ALLOCATION_VERSION.to_owned();
    book.plan_id = fingerprint(screen_id, book);
    let Ok(mut plans) = plan_store().lock() else {
        return;
    };
    if let Some(existing) = plans.get(&book.plan_id)
        && (existing.sending || (existing.orders.is_some() && existing.at.elapsed() < PLAN_REPLAY))
    {
        return;
    }
    if plans.len() >= PLAN_CAP
        && let Some(old) = plans
            .iter()
            .min_by_key(|(_, plan)| plan.at)
            .map(|(id, _)| id.clone())
    {
        plans.remove(&old);
    }
    plans.insert(
        book.plan_id.clone(),
        StoredPlan {
            screen_id: screen_id.to_owned(),
            book: book.clone(),
            orders: None,
            portfolio_id: None,
            sending: false,
            at: Instant::now(),
        },
    );
}

enum Claim {
    Missing,
    Ready(Box<SizedBook>),
    Replay(Vec<ScreenOrderOut>, Option<i64>),
    Busy,
}

fn claim_plan(screen_id: &str, plan_id: &str) -> Claim {
    let Ok(mut plans) = plan_store().lock() else {
        return Claim::Busy;
    };
    let Some(plan) = plans.get_mut(plan_id) else {
        return Claim::Missing;
    };
    if plan.screen_id != screen_id {
        return Claim::Missing;
    }
    if let Some(orders) = plan.orders.clone() {
        return Claim::Replay(orders, plan.portfolio_id);
    }
    if plan.sending {
        return Claim::Busy;
    }
    plan.sending = true;
    plan.at = Instant::now();
    Claim::Ready(Box::new(plan.book.clone()))
}

fn finish_plan(plan_id: &str, orders: Vec<ScreenOrderOut>, portfolio_id: Option<i64>) {
    let Ok(mut plans) = plan_store().lock() else {
        return;
    };
    let Some(plan) = plans.get_mut(plan_id) else {
        return;
    };
    plan.sending = false;
    plan.orders = Some(orders);
    plan.portfolio_id = portfolio_id.or(plan.portfolio_id);
    plan.at = Instant::now();
}

fn release_plan(plan_id: &str) {
    let Ok(mut plans) = plan_store().lock() else {
        return;
    };
    let Some(plan) = plans.get_mut(plan_id) else {
        return;
    };
    if plan.orders.is_none() {
        plan.sending = false;
    }
}

fn stored_book(plan_id: &str) -> Option<SizedBook> {
    plan_store()
        .lock()
        .ok()?
        .get(plan_id)
        .map(|plan| plan.book.clone())
}

fn allocation_changed(approved: &SizedBook, fresh: &SizedBook) -> bool {
    if approved.holdings.len() != fresh.holdings.len() {
        return true;
    }
    approved
        .holdings
        .iter()
        .zip(&fresh.holdings)
        .any(|(left, right)| {
            left.symbol != right.symbol
                || left.quantity != right.quantity
                || left.product != right.product
                || left.leverage != right.leverage
        })
}

fn accepted_qty(order: &ScreenOrderOut, planned: i64) -> Option<i64> {
    match order.status.as_str() {
        "REJECTED" | "CANCELLED" | "ERROR" | "BLOCKED" | "RISK_BLOCKED" | "NOT_SENT"
        | "UNKNOWN" => None,
        _ => {
            let filled = order.filled_quantity.unwrap_or(0);
            if filled > 0 {
                Some(filled.min(planned))
            } else {
                Some(planned)
            }
        }
    }
}

fn book_after_orders(book: &SizedBook, orders: &[ScreenOrderOut]) -> SizedBook {
    let mut out = book.clone();
    let by: HashMap<&str, &ScreenOrderOut> = orders
        .iter()
        .map(|row| (row.symbol.as_str(), row))
        .collect();
    let mut exposure = dec(0);
    let mut contribution = dec(0);
    let mut funding = dec(0);
    for holding in &mut out.holdings {
        let planned = holding.quantity.unwrap_or(0);
        let qty = by
            .get(holding.symbol.as_str())
            .and_then(|order| accepted_qty(order, planned))
            .filter(|qty| *qty > 0);
        if let Some(qty) = qty {
            let price = holding.price.clone().unwrap_or_else(WideDecimal::zero);
            let leverage = if holding.leverage.is_zero() || holding.leverage.is_sign_negative() {
                dec(1)
            } else {
                holding.leverage.clone()
            };
            let gross = money(&price.mul(&dec(qty)));
            let own = money(&gross.div(&leverage).unwrap_or_else(|_| gross.clone()));
            holding.quantity = Some(qty);
            holding.amount = gross.clone();
            holding.contribution = own.clone();
            holding.funding = gross.sub(&own);
            exposure = exposure.add(&holding.amount);
            contribution = contribution.add(&holding.contribution);
            funding = funding.add(&holding.funding);
        } else {
            holding.quantity = None;
            holding.amount = money(&dec(0));
            holding.contribution = money(&dec(0));
            holding.funding = money(&dec(0));
        }
    }
    out.exposure = money(&exposure);
    out.contribution = money(&contribution);
    out.funding = money(&funding);
    let cash = out.cash_outlay.clone().sub(&out.reserve).sub(&out.costs);
    out.leftover = money(&cash.sub(&contribution));
    out
}

fn alloc_input(book: &SizedBook) -> AllocInput {
    AllocInput {
        budget: book.cash_outlay.clone(),
        reserve: book.reserve.clone(),
        costs: book.costs.clone(),
        names: book
            .holdings
            .iter()
            .filter_map(|row| {
                let price = row.price.clone()?;
                if price <= dec(0) {
                    return None;
                }
                Some(AllocName {
                    symbol: row.symbol.clone(),
                    price,
                    leverage: if row.leverage < dec(1) {
                        dec(1)
                    } else {
                        row.leverage.clone()
                    },
                    lot_size: 1,
                    max_gross: None,
                })
            })
            .collect(),
    }
}

fn book_from_output(book: &SizedBook, sized: &AllocOutput) -> SizedBook {
    let mut out = book.clone();
    out.exposure = money(&sized.gross_total);
    out.contribution = money(&sized.contribution_total);
    out.funding = money(&sized.funding_total);
    out.leftover = money(&sized.unallocated);
    out.gross_target = money(&sized.target);
    out.min_investment = money(&ceil_rupee(&sized.minimum_budget));
    out.constraint = sized.constraint.clone();
    out.empty_reason = sized.shortfall.clone();
    out.plan_id.clear();
    let lines: HashMap<&str, &gross_allocation::AllocLine> = sized
        .lines
        .iter()
        .map(|line| (line.symbol.as_str(), line))
        .collect();
    for holding in &mut out.holdings {
        let Some(line) = lines.get(holding.symbol.as_str()) else {
            continue;
        };
        holding.quantity = (line.quantity > 0).then_some(line.quantity);
        holding.amount = money(&line.gross);
        holding.contribution = money(&line.contribution);
        holding.funding = money(&line.funding);
        holding.deviation = money(&line.deviation);
        holding.leverage = num::quantize(&line.leverage, -2);
        holding.price = Some(money(&line.price));
        holding.product = if line.leverage > dec(1) { "MTF" } else { "CNC" }.to_owned();
    }
    out
}

fn one_margin_total(value: &baskfy_providers::kite::KiteValue) -> Result<WideDecimal, String> {
    use baskfy_providers::kite::KiteValue;
    let KiteValue::Map(map) = value else {
        return Err("margin row is not an object".to_owned());
    };
    let block = match map.get("final") {
        Some(KiteValue::Map(inner)) => inner,
        _ => map,
    };
    let total = margin_number(block.get("total")).max(margin_number(map.get("total")));
    if total <= 0.0 {
        return Err("margin total was not a positive number".to_owned());
    }
    format!("{total:.2}")
        .parse::<WideDecimal>()
        .map_err(|_| "margin total was not decimal".to_owned())
}

fn order_attempted(orders: &[ScreenOrderOut]) -> bool {
    orders.iter().any(|row| {
        row.order_id.is_some()
            || matches!(
                row.status.as_str(),
                "PLACED"
                    | "DRY_RUN"
                    | "COMPLETE"
                    | "OPEN"
                    | "REJECTED"
                    | "CANCELLED"
                    | "BLOCKED"
                    | "RISK_BLOCKED"
                    | "UNKNOWN"
                    | "DUPLICATE"
                    | "PARTIAL"
            )
    })
}

fn desk_line(row: &ScreenInvestHoldingOut) -> serde_json::Value {
    let price = row
        .price
        .as_ref()
        .and_then(|px| px.to_sci_string().parse::<f64>().ok())
        .unwrap_or(0.0);
    serde_json::json!({
        "symbol": row.tradingsymbol,
        "qty": row.quantity.unwrap_or(0),
        "price": price,
        "product": if row.product == "MTF" { "MTF" } else { "CNC" },
    })
}

fn order_result(
    symbol: &str,
    status: &str,
    order_id: Option<String>,
    stop: Option<String>,
    error: Option<String>,
    filled_quantity: Option<i64>,
) -> ScreenOrderOut {
    ScreenOrderOut {
        symbol: symbol.to_owned(),
        status: status.to_owned(),
        order_id,
        stop,
        error,
        filled_quantity,
    }
}

async fn confirm_margin(book: &SizedBook) -> Result<Option<SizedBook>, Problem> {
    use baskfy_providers::kite::KiteValue;
    let rows: Vec<&ScreenInvestHoldingOut> = book
        .holdings
        .iter()
        .filter(|row| row.quantity.unwrap_or(0) > 0)
        .collect();
    if rows.is_empty() {
        return Ok(None);
    }
    let client = kite_for_margins()?;
    let mut bodies = Vec::with_capacity(rows.len());
    for row in &rows {
        let price = row
            .price
            .as_ref()
            .and_then(|px| px.to_sci_string().parse::<f64>().ok())
            .unwrap_or(0.0);
        let order = serde_json::json!({
            "exchange": "NSE",
            "tradingsymbol": row.tradingsymbol,
            "transaction_type": "BUY",
            "variety": "regular",
            "product": if row.product == "MTF" { "MTF" } else { "CNC" },
            "order_type": "LIMIT",
            "quantity": row.quantity.unwrap_or(0),
            "price": price,
        });
        bodies.push(KiteValue::from_json(order.to_string().as_bytes()).map_err(refuse)?);
    }
    let reply = client
        .margins_orders(&bodies)
        .await
        .map_err(|error| refuse(error.to_string()))?;
    let totals = match &reply {
        KiteValue::List(items) if items.len() == rows.len() => items
            .iter()
            .map(one_margin_total)
            .collect::<Result<Vec<_>, _>>()
            .map_err(refuse)?,
        KiteValue::Map(_) => vec![one_margin_total(&reply).map_err(refuse)?],
        _ => return Err(refuse("Broker margin did not cover every order")),
    };
    let available = book.cash_outlay.clone().sub(&book.reserve).sub(&book.costs);
    let summed = totals.iter().fold(dec(0), |acc, total| acc.add(total));
    if totals.len() == rows.len() && summed <= available {
        return Ok(None);
    }
    if totals.len() == 1 && rows.len() > 1 {
        if summed <= available {
            return Ok(None);
        }
        let fitted = scaled_cash(&available, &summed).map_err(refuse)?;
        let mut input = alloc_input(book);
        let mut costs = input.budget.clone().sub(&input.reserve).sub(&fitted);
        if costs.is_sign_negative() {
            costs = dec(0);
        }
        input.costs = costs;
        let sized = allocate_equal_gross(&input).map_err(refuse)?;
        let revised = book_from_output(book, &sized);
        return Ok(
            (allocation_changed(book, &revised) || revised.empty_reason.is_some())
                .then_some(revised),
        );
    }
    let mut rates = HashMap::new();
    for (row, total) in rows.iter().zip(totals) {
        let qty = dec(row.quantity.unwrap_or(1).max(1));
        let per = total.div(&qty).unwrap_or(total);
        rates.insert(row.symbol.clone(), per);
    }
    let sized = allocate_with_required(&alloc_input(book), |symbol, qty, _price| {
        let per = rates.get(symbol).cloned().unwrap_or_else(WideDecimal::zero);
        Ok(per.mul(&dec(qty)))
    })
    .map_err(refuse)?;
    let revised = book_from_output(book, &sized);
    Ok((allocation_changed(book, &revised) || revised.empty_reason.is_some()).then_some(revised))
}

fn needs_review(
    fresh: SizedBook,
    as_of: Option<NaiveDate>,
    reason: String,
) -> Result<Response, Problem> {
    let mut out = out_of(fresh, as_of, None);
    out.empty_reason = Some(reason);
    Ok(wire::json_status(StatusCode::CONFLICT, &out))
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
    let (as_of, book) = preview_on(
        &mut conn,
        &screen,
        &body.amount,
        body.mtf,
        &body.reserve,
        &body.costs,
        false,
    )
    .await?;
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
    // Cash and MTF both send their buys through the desk (Maulik, 12 Nov 2026: cash invest
    // re-priced at confirm and refused on any tick, and never bought; it now takes the MTF path).
    let (seed, orders, sent_plan) = {
        let mut conn = db::acquire(&state).await?;
        let screen = load_visible_on(&mut conn, &public_id, &principal).await?;
        let (as_of, fresh) = preview_on(
            &mut conn,
            &screen,
            &body.amount,
            body.mtf,
            &body.reserve,
            &body.costs,
            true,
        )
        .await?;
        drop(conn);
        if body.plan_id.is_empty() {
            return needs_review(
                fresh,
                as_of,
                "Review this allocation, then confirm it.".to_owned(),
            );
        }
        let screen_id = screen.public_id.clone();
        match claim_plan(&screen_id, &body.plan_id) {
            Claim::Missing => {
                return Err(refuse("This allocation expired. Review the basket again."));
            }
            Claim::Busy => return Err(refuse("This basket is already being sent.")),
            Claim::Replay(previous, portfolio_id) => {
                let mut out = out_of(fresh, as_of, None);
                out.orders = previous;
                out.already_sent = true;
                out.empty_reason = Some(match portfolio_id {
                    Some(id) => format!("These orders were already sent. Portfolio {id}."),
                    None => "These orders were already sent.".to_owned(),
                });
                return Ok(wire::json_status(StatusCode::CONFLICT, &out));
            }
            Claim::Ready(approved) => {
                let approved = *approved;
                if approved.empty_reason.is_some()
                    || fresh.empty_reason.is_some()
                    || allocation_changed(&approved, &fresh)
                    || approved.cash_outlay != money(&body.amount)
                {
                    release_plan(&body.plan_id);
                    let reason = fresh.empty_reason.clone().unwrap_or_else(|| {
                        "Prices or leverage changed. Review the updated allocation before sending."
                            .to_owned()
                    });
                    return needs_review(fresh, as_of, reason);
                }
                if !crate::live_prices::dry_run_enabled() {
                    match confirm_margin(&approved).await {
                        Ok(None) => {}
                        Ok(Some(mut revised)) => {
                            release_plan(&body.plan_id);
                            revised.quoted_at = quote_stamp(as_of);
                            remember_plan(&screen_id, &mut revised);
                            return needs_review(
                                revised,
                                as_of,
                                "Broker margin no longer fits this allocation. Review the updated basket."
                                    .to_owned(),
                            );
                        }
                        Err(error) => {
                            release_plan(&body.plan_id);
                            return Err(error);
                        }
                    }
                }
                let orders = send_screen_mtf(&approved.holdings, &approved.plan_id).await;
                if !order_attempted(&orders) {
                    release_plan(&body.plan_id);
                    let mut out = out_of(approved, as_of, None);
                    out.orders = orders;
                    out.empty_reason = Some(
                        "No order was sent. Nothing was bought, so you can confirm this allocation again."
                            .to_owned(),
                    );
                    return Ok(wire::json_status(StatusCode::CONFLICT, &out));
                }
                let filled = book_after_orders(&approved, &orders);
                let any = filled
                    .holdings
                    .iter()
                    .any(|row| row.quantity.unwrap_or(0) > 0);
                let plan_id = approved.plan_id.clone();
                finish_plan(&plan_id, orders.clone(), None);
                if !any {
                    let mut out = out_of(filled, as_of, None);
                    out.orders = orders;
                    let unconfirmed = out.orders.iter().any(|row| row.status == "UNKNOWN");
                    out.empty_reason = Some(if unconfirmed {
                        "No portfolio was created. The desk did not confirm these orders, so check the order book. This plan will not be sent again."
                            .to_owned()
                    } else {
                        "No portfolio was created. Every buy was rejected, and this plan will not be sent again."
                            .to_owned()
                    });
                    return Ok(wire::json_status(StatusCode::CONFLICT, &out));
                }
                (Some((as_of, filled)), orders, Some(plan_id))
            }
        }
    };
    let mut out = session::run(&write, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let screen = load_visible_on(conn, &public_id, &principal).await?;
            let (as_of, book) = if let Some((as_of, book)) = seed {
                (as_of, book)
            } else {
                let (as_of, mut book) = preview_on(
                    conn,
                    &screen,
                    &body.amount,
                    body.mtf,
                    &body.reserve,
                    &body.costs,
                    false,
                )
                .await?;
                if let Some(reason) = &book.empty_reason {
                    return Err(refuse(reason.clone()));
                }
                if !body.plan_id.is_empty()
                    && let Some(approved) = stored_book(&body.plan_id)
                {
                    if allocation_changed(&approved, &book) {
                        return Err(refuse(
                            "Prices changed. Review the updated allocation before saving.",
                        ));
                    }
                    book = approved;
                }
                (as_of, book)
            };
            let name = body.name.unwrap_or_else(|| screen.name.clone());
            let parent = match body.parent_id {
                Some(raw) => Some(load(conn, raw, user_id).await?.id),
                None => None,
            };
            let broker_account_id = broker_account_for(conn, body.broker, user_id).await?;
            let portfolio =
                insert_portfolio(conn, user_id, &name, parent, broker_account_id, "MY_SCREEN")
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
    out.orders = orders.clone();
    if let Some(plan_id) = sent_plan {
        let portfolio_id = out.report.as_ref().map(|detail| detail.portfolio.id);
        finish_plan(&plan_id, orders, portfolio_id);
    }
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

#[derive(serde::Deserialize)]
struct DeskReply {
    orders: Vec<DeskOrder>,
}

#[derive(serde::Deserialize)]
struct DeskOrder {
    symbol: String,
    status: String,
    order_id: Option<String>,
    stop: Option<String>,
    error: Option<String>,
}

/// Every row with one status: the desk answered nothing row-by-row.
fn all_rows(planned: &[&ScreenInvestHoldingOut], status: &str, error: &str) -> Vec<ScreenOrderOut> {
    planned
        .iter()
        .map(|row| {
            order_result(
                &row.symbol,
                status,
                None,
                None,
                Some(error.to_owned()),
                None,
            )
        })
        .collect()
}

/// The desk owns every broker write (Law 2): it runs the guards, risk, limits, the durable intent
/// and the stop, and `DRY_RUN` is its call. `plan_id` makes a retry idempotent row by row.
async fn send_screen_mtf(
    holdings: &[ScreenInvestHoldingOut],
    plan_id: &str,
) -> Vec<ScreenOrderOut> {
    const UNKNOWN_OUTCOME: &str =
        "The desk did not confirm this order. Check the order book before trying again.";
    let planned: Vec<&ScreenInvestHoldingOut> = holdings
        .iter()
        .filter(|row| row.quantity.unwrap_or(0) > 0)
        .collect();
    if planned.is_empty() {
        return vec![order_result(
            "",
            "ERROR",
            None,
            None,
            Some("No share quantity was large enough to send".to_owned()),
            None,
        )];
    }
    let base =
        std::env::var("BASKFY_DESK_INTERNAL_URL").unwrap_or_else(|_| "http://desk:8420".to_owned());
    let lines: Vec<serde_json::Value> = planned.iter().map(|row| desk_line(row)).collect();
    let wait = 30 + 10 * u64::try_from(lines.len()).unwrap_or(u64::MAX / 20);
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(wait))
        .build()
    {
        Ok(client) => client,
        Err(error) => return all_rows(&planned, "NOT_SENT", &error.to_string()),
    };
    let mut request = client
        .post(format!(
            "{}/screen-invest/execute",
            base.trim_end_matches('/')
        ))
        .header("Origin", "http://desk")
        .json(&serde_json::json!({"confirm": true, "plan_id": plan_id, "lines": lines}));
    if let Ok(password) = std::env::var("DESK_PASSWORD")
        && !password.is_empty()
    {
        request = request.basic_auth("desk", Some(password));
    }
    let response = match request.send().await {
        Ok(response) => response,
        // A refused connection never reached the desk; anything later may have.
        Err(error) if error.is_connect() => {
            return all_rows(&planned, "NOT_SENT", &error.to_string());
        }
        Err(_) => return all_rows(&planned, "UNKNOWN", UNKNOWN_OUTCOME),
    };
    let status = response.status();
    if status.is_client_error() {
        let detail = response.text().await.unwrap_or_default();
        return all_rows(&planned, "NOT_SENT", &detail);
    }
    if !status.is_success() {
        return all_rows(&planned, "UNKNOWN", UNKNOWN_OUTCOME);
    }
    let Ok(reply) = response.json::<DeskReply>().await else {
        return all_rows(&planned, "UNKNOWN", UNKNOWN_OUTCOME);
    };
    planned
        .iter()
        .map(|row| {
            let wanted = row.tradingsymbol.trim().to_ascii_uppercase();
            let Some(order) = reply.orders.iter().find(|o| o.symbol == wanted) else {
                return order_result(
                    &row.symbol,
                    "NOT_SENT",
                    None,
                    None,
                    Some("The desk did not accept this line.".to_owned()),
                    None,
                );
            };
            let filled = (order.status == "DRY_RUN")
                .then_some(row.quantity)
                .flatten();
            order_result(
                &row.symbol,
                &order.status,
                order.order_id.clone(),
                order.stop.clone(),
                order.error.clone(),
                filled,
            )
        })
        .collect()
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
            let (as_of, book) = preview_on(
                conn,
                &screen,
                &body.amount,
                body.mtf,
                &body.reserve,
                &body.costs,
                false,
            )
            .await?;
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
    /// Still accepted on create. Sizing uses each name's verified leverage, not this.
    #[allow(dead_code)]
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
        errors.push("amount", "MTF sizing needs an amount", "value_error");
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
        return Ok(Some((screen, bare_book(&dec(0), false), Vec::new())));
    };
    let (_, book) = preview_on(conn, &screen, amount, link.mtf, &dec(0), &dec(0), false).await?;
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
    let fallback = bare_book(&dec(0), false);
    let mut use_book = book;
    let synthesized;
    if use_book.is_none() {
        synthesized = SizedBook {
            holdings: (0..holdings_count.max(1))
                .map(|i| blank_holding(i64::try_from(i).unwrap_or(0)))
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
            lot_size: 1,
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
        factors.insert("CCC".to_owned(), dec(1));
        let book = size_book(&rows, &dec(10_000), true, &factors, &dec(0), &dec(0)).unwrap();
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
        let book = size_book(
            &rows,
            &dec(10_000),
            false,
            &HashMap::new(),
            &dec(0),
            &dec(0),
        )
        .unwrap();
        assert!(!book.mtf);
        assert_eq!(book.leverage.to_sci_string(), "1");
        assert_eq!(book.holdings.len(), 2);
        assert!(book.skipped.is_empty());
    }

    fn named(symbol: &str, qty: i64, leverage: i64, product: &str) -> ScreenInvestHoldingOut {
        let mut holding = blank_holding(1);
        holding.symbol = symbol.to_owned();
        holding.quantity = Some(qty);
        holding.price = Some(dec(500));
        holding.leverage = dec(leverage);
        holding.product = product.to_owned();
        holding.amount = dec(qty * 500);
        holding.tradingsymbol = symbol.to_owned();
        holding
    }

    #[test]
    fn the_acceptance_basket_equalises_position_value() {
        let rows = [
            px("A", 1, 500, "EQ"),
            px("B", 2, 500, "EQ"),
            px("C", 3, 500, "EQ"),
        ];
        let mut factors = HashMap::new();
        factors.insert("A".to_owned(), dec(1));
        factors.insert("B".to_owned(), dec(2));
        factors.insert("C".to_owned(), dec(4));
        let book = size_book(&rows, &dec(350_000), true, &factors, &dec(0), &dec(0)).unwrap();
        assert!(book.empty_reason.is_none(), "{:?}", book.empty_reason);
        assert!(book.reserve.is_zero());
        assert!(book.costs.is_zero());
        assert!(book.leftover.is_zero());
        assert_eq!(book.exposure, dec(600_000));
        assert_eq!(book.contribution, dec(350_000));
        assert_eq!(book.funding, dec(250_000));
        let a = book.holdings.iter().find(|row| row.symbol == "A").unwrap();
        let b = book.holdings.iter().find(|row| row.symbol == "B").unwrap();
        let c = book.holdings.iter().find(|row| row.symbol == "C").unwrap();
        for row in [a, b, c] {
            assert_eq!(row.quantity, Some(400));
            assert_eq!(row.amount, dec(200_000));
        }
        assert_eq!(a.product, "CNC");
        assert!(a.funding.is_zero());
        assert_eq!(a.contribution, dec(200_000));
        assert_eq!(b.product, "MTF");
        assert_eq!(b.contribution, dec(100_000));
        assert_eq!(b.funding, dec(100_000));
        assert_eq!(c.product, "MTF");
        assert_eq!(c.contribution, dec(50_000));
        assert_eq!(c.funding, dec(150_000));
        assert_ne!(c.quantity, Some(1_600));
    }

    #[test]
    fn missing_leverage_does_not_guess_four_or_resize_the_rest() {
        let rows = [px("A", 1, 500, "EQ"), px("B", 2, 500, "EQ")];
        let mut factors = HashMap::new();
        factors.insert("A".to_owned(), dec(4));
        let book = size_book(&rows, &dec(350_000), true, &factors, &dec(0), &dec(0)).unwrap();
        let reason = book.empty_reason.expect("shortfall");
        assert!(reason.contains('B'), "{reason}");
        assert!(book.holdings.iter().all(|row| row.quantity.is_none()));
        assert!(book.exposure.is_zero());
    }

    #[test]
    fn a_short_budget_names_the_name_it_cannot_buy() {
        let rows = [px("CHEAP", 1, 100, "EQ"), px("DEAR", 2, 10_000, "EQ")];
        let book = size_book(&rows, &dec(5_000), false, &HashMap::new(), &dec(0), &dec(0)).unwrap();
        let reason = book.empty_reason.expect("shortfall");
        assert!(reason.contains("DEAR"), "{reason}");
        assert!(book.holdings.iter().all(|row| row.quantity.is_none()));
    }

    #[test]
    fn the_same_inputs_keep_the_same_quantities() {
        let rows = [
            px("A", 1, 500, "EQ"),
            px("B", 2, 500, "EQ"),
            px("C", 3, 500, "EQ"),
        ];
        let mut factors = HashMap::new();
        factors.insert("A".to_owned(), dec(1));
        factors.insert("B".to_owned(), dec(2));
        factors.insert("C".to_owned(), dec(4));
        let first = size_book(&rows, &dec(350_000), true, &factors, &dec(0), &dec(0)).unwrap();
        let second = size_book(&rows, &dec(350_000), true, &factors, &dec(0), &dec(0)).unwrap();
        assert!(!allocation_changed(&first, &second));
        let dear = [
            px("A", 1, 500, "EQ"),
            px("B", 2, 500, "EQ"),
            px("C", 3, 620, "EQ"),
        ];
        let moved = size_book(&dear, &dec(350_000), true, &factors, &dec(0), &dec(0)).unwrap();
        assert!(allocation_changed(&first, &moved));
    }

    #[test]
    fn order_quantity_is_not_multiplied_by_leverage() {
        let line = desk_line(&named("C", 400, 4, "MTF"));
        assert_eq!(line["qty"], 400);
        assert_eq!(line["product"], "MTF");
        assert_eq!(desk_line(&named("A", 400, 1, "CNC"))["product"], "CNC");
    }

    #[test]
    fn a_rejected_order_does_not_hide_the_fills() {
        let mut book = bare_book(&dec(350_000), true);
        book.holdings = vec![
            named("A", 400, 1, "CNC"),
            named("B", 400, 2, "MTF"),
            named("C", 400, 4, "MTF"),
        ];
        let orders = vec![
            order_result("A", "PLACED", Some("1".to_owned()), None, None, Some(400)),
            order_result("B", "REJECTED", None, None, Some("margin".to_owned()), None),
            order_result("C", "COMPLETE", Some("3".to_owned()), None, None, Some(100)),
        ];
        let filled = book_after_orders(&book, &orders);
        assert_eq!(filled.holdings[0].quantity, Some(400));
        assert!(filled.holdings[1].quantity.is_none());
        assert_eq!(filled.holdings[2].quantity, Some(100));
        assert_eq!(filled.holdings[2].amount, dec(50_000));
    }

    #[test]
    fn a_second_claim_replays_instead_of_sending_again() {
        let mut book = bare_book(&dec(1_000), true);
        let symbol = format!("DUP{}", std::process::id());
        book.holdings = vec![named(&symbol, 2, 1, "CNC")];
        let screen = format!("screen-{symbol}");
        remember_plan(&screen, &mut book);
        let id = book.plan_id.clone();
        assert!(matches!(claim_plan(&screen, &id), Claim::Ready(_)));
        finish_plan(
            &id,
            vec![order_result(
                &symbol,
                "PLACED",
                Some("9".to_owned()),
                None,
                None,
                Some(2),
            )],
            Some(44),
        );
        match claim_plan(&screen, &id) {
            Claim::Replay(orders, portfolio) => {
                assert_eq!(orders.len(), 1);
                assert_eq!(orders[0].status, "PLACED");
                assert_eq!(portfolio, Some(44));
            }
            Claim::Busy => panic!("plan stayed busy"),
            Claim::Missing => panic!("plan was dropped"),
            Claim::Ready(_) => panic!("plan was sent twice"),
        }
    }

    #[test]
    fn nested_final_leverage_is_not_read_as_cash() {
        let reply = baskfy_providers::kite::KiteValue::from_json(
            br#"{"final":{"leverage":4,"total":125},"initial":{"leverage":1,"total":500}}"#,
        )
        .unwrap();
        let leverage = leverage_from_margin(&reply, 500.0).unwrap();
        assert!(leverage > dec(3));
    }

    #[test]
    fn an_unreadable_margin_is_not_stored_as_one_times() {
        let reply = baskfy_providers::kite::KiteValue::from_json(br#"{"note":"empty"}"#).unwrap();
        assert!(leverage_from_margin(&reply, 500.0).is_none());
    }
}
