//! `_load_ledger` and the reads around it (`routers/portfolio_overview.py`): one user's whole
//! portfolio picture in a fixed number of statements, every one scoped by `user_id`.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveTime, Utc};

use super::cash::{CashFlowKind, LedgerCashFlow};
use super::db;
use super::ledger::{
    Allocation, Holding, HoldingKey, LedgerPortfolio, LedgerQuestion, PortfolioKind,
    PortfolioSource, ReconciliationReason, holding_value,
};
use super::pydec::Dec;
use super::recon::{InboxEntry, LedgerPosition, ReconciliationState};
use super::tradebook::TradeFill;
use crate::problems::Problem;
use crate::state::AppState;

/// `NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i16 = 1;
/// `MONTH_WINDOW` (days).
pub(crate) const MONTH_WINDOW_DAYS: i64 = 30;
/// `_CALENDAR_LOOKBACK` (days).
const CALENDAR_LOOKBACK_DAYS: i64 = 21;

/// IST, `+05:30`.
pub(crate) fn ist() -> FixedOffset {
    baskfy_providers::tokens::ist()
}

/// `_now_ist()`.
pub(crate) fn now_ist() -> DateTime<FixedOffset> {
    crate::entitlements::utc_now().with_timezone(&ist())
}

/// `today_ist()`.
pub(crate) fn today_ist() -> NaiveDate {
    now_ist().date_naive()
}

fn fail(e: String) -> Problem {
    super::fail(&e)
}

/// A `portfolio` row, as the ledger keeps it.
#[derive(Debug, Clone)]
pub(crate) struct PortfolioRow {
    pub(crate) benchmark_index_id: Option<i64>,
}

/// An `instrument` row (id, symbol, name).
#[derive(Debug, Clone)]
pub(crate) struct InstrumentRow {
    pub(crate) id: i64,
    pub(crate) symbol: String,
    pub(crate) name: String,
}

/// A `broker_account` row.
#[derive(Debug, Clone)]
pub(crate) struct BrokerRow {
    pub(crate) id: i64,
    pub(crate) broker_id: String,
    pub(crate) label: String,
    pub(crate) holdings_synced_at: Option<DateTime<Utc>>,
}

/// A `broker_cash` row.
#[derive(Debug, Clone)]
pub(crate) struct BrokerCashRow {
    pub(crate) broker_account_id: i64,
    pub(crate) balance: Dec,
    pub(crate) as_of: NaiveDate,
}

/// A `portfolio_cash_flow` row.
#[derive(Debug, Clone)]
pub(crate) struct FlowRow {
    pub(crate) broker_account_id: i64,
    pub(crate) portfolio_id: Option<i64>,
    pub(crate) kind: CashFlowKind,
    pub(crate) amount: Dec,
    pub(crate) occurred_on: NaiveDate,
    pub(crate) instrument_id: Option<i64>,
    pub(crate) quantity: Option<Dec>,
    pub(crate) note: Option<String>,
}

/// A `cb_basket` row (what the source panel reads).
#[derive(Debug, Clone)]
pub(crate) struct BasketRow {
    pub(crate) id: i64,
    pub(crate) slug: String,
    pub(crate) name: String,
}

/// A `screen` row (what the source panel reads).
#[derive(Debug, Clone)]
pub(crate) struct ScreenRow {
    pub(crate) public_id: String,
    pub(crate) name: String,
}

/// `_Prices`.
#[derive(Debug, Clone, Default)]
pub(crate) struct Prices {
    pub(crate) as_of: Option<NaiveDate>,
    pub(crate) latest: HashMap<i64, Dec>,
    pub(crate) previous: HashMap<i64, Dec>,
    pub(crate) dated: HashMap<i64, NaiveDate>,
    pub(crate) unpriced: Vec<i64>,
    pub(crate) live_overlay: bool,
    pub(crate) week_base: HashMap<i64, Dec>,
    pub(crate) week_since: Option<NaiveDate>,
    pub(crate) month_base: HashMap<i64, Dec>,
    pub(crate) month_since: Option<NaiveDate>,
}

impl Prices {
    /// `instrument_id in prices.unpriced`.
    pub(crate) fn is_unpriced(&self, instrument_id: i64) -> bool {
        self.unpriced.contains(&instrument_id)
    }
}

/// `_Position`.
#[derive(Debug, Clone)]
pub(crate) struct Position {
    pub(crate) key: HoldingKey,
    pub(crate) holding: Holding,
    /// `capital_slices`, ordered by portfolio id.
    pub(crate) capital_slices: Vec<(i64, Dec)>,
    pub(crate) monitoring_portfolio_ids: Vec<i64>,
    pub(crate) first_bought_on: Option<NaiveDate>,
    pub(crate) history_source: String,
}

impl Position {
    /// `sole_capital_portfolio_id`.
    pub(crate) fn sole_capital_portfolio_id(&self) -> Option<i64> {
        if self.capital_slices.len() == 1 {
            Some(self.capital_slices[0].0)
        } else {
            None
        }
    }

    /// `portfolio_id in capital_slices`.
    pub(crate) fn has_slice(&self, portfolio_id: i64) -> bool {
        self.capital_slices.iter().any(|(p, _)| *p == portfolio_id)
    }

    /// `allocated_quantity`.
    pub(crate) fn allocated_quantity(&self) -> Dec {
        self.capital_slices
            .iter()
            .fold(Dec::lit("0"), |acc, (_, q)| acc.add(q))
    }

    /// `unallocated_quantity`.
    pub(crate) fn unallocated_quantity(&self) -> Dec {
        let remainder = self.holding.quantity.sub(&self.allocated_quantity());
        if remainder > Dec::lit("0") {
            remainder
        } else {
            Dec::lit("0")
        }
    }
}

/// `_Ledger`.
#[derive(Debug, Clone, Default)]
pub(crate) struct Ledger {
    pub(crate) portfolios: BTreeMap<i64, LedgerPortfolio>,
    pub(crate) rows: HashMap<i64, PortfolioRow>,
    pub(crate) positions: Vec<Position>,
    pub(crate) prices: Prices,
    pub(crate) instruments: HashMap<i64, InstrumentRow>,
    pub(crate) brokers: BTreeMap<i64, BrokerRow>,
    pub(crate) broker_cash: Vec<BrokerCashRow>,
    pub(crate) flows: Vec<LedgerCashFlow>,
    pub(crate) flow_rows: Vec<FlowRow>,
    pub(crate) entries: Vec<InboxEntry>,
    pub(crate) item_resolved_at: HashMap<i64, Option<DateTime<Utc>>>,
    pub(crate) rebalance_due_portfolio_ids: BTreeSet<i64>,
    pub(crate) pile_ids: HashSet<i64>,
    pub(crate) baskets: HashMap<i64, BasketRow>,
    pub(crate) publishers: HashMap<i64, String>,
    pub(crate) screens: HashMap<i64, ScreenRow>,
    pub(crate) benchmarks: HashMap<i64, String>,
    pub(crate) trades: Vec<(i64, Vec<TradeFill>)>,
}

impl Ledger {
    /// `holdings`.
    pub(crate) fn holdings(&self) -> Vec<Holding> {
        self.positions.iter().map(|p| p.holding.clone()).collect()
    }

    /// `allocations`: one per capital slice with a positive quantity.
    pub(crate) fn allocations(&self) -> Vec<Allocation> {
        let zero = Dec::lit("0");
        self.positions
            .iter()
            .flat_map(|p| {
                p.capital_slices
                    .iter()
                    .filter(|(_, q)| *q > zero)
                    .map(|(pid, q)| Allocation {
                        key: p.key,
                        portfolio_id: *pid,
                        quantity: q.clone(),
                    })
            })
            .collect()
    }

    /// `ledger_positions`: a row per slice, the Unallocated remainder, and every lens.
    pub(crate) fn ledger_positions(&self) -> Result<Vec<LedgerPosition>, Problem> {
        let mut found = Vec::new();
        for p in &self.positions {
            for (pid, q) in &p.capital_slices {
                found.push(LedgerPosition {
                    portfolio_id: Some(*pid),
                    holding: p.holding.with_quantity(q.clone()).map_err(fail)?,
                });
            }
            let remainder = p.unallocated_quantity();
            if remainder > Dec::lit("0") || p.capital_slices.is_empty() {
                found.push(LedgerPosition {
                    portfolio_id: None,
                    holding: p.holding.with_quantity(remainder).map_err(fail)?,
                });
            }
            for view in &p.monitoring_portfolio_ids {
                found.push(LedgerPosition {
                    portfolio_id: Some(*view),
                    holding: p.holding.clone(),
                });
            }
        }
        Ok(found)
    }

    /// `priced()`: the price map the ledger is handed.
    pub(crate) fn priced(&self) -> HashMap<i64, Dec> {
        self.prices.latest.clone()
    }

    /// `value_of(holding)`: `None` for an unpriced instrument.
    pub(crate) fn value_of(&self, holding: &Holding) -> Result<Option<Dec>, Problem> {
        if self.prices.is_unpriced(holding.key.instrument_id) {
            return Ok(None);
        }
        holding_value(holding, &self.prices.latest)
            .map(Some)
            .map_err(fail)
    }
}

struct HoldingDbRow {
    portfolio_id: i64,
    instrument_id: i64,
    broker_account_id: i64,
    quantity: Option<Dec>,
    avg_price: Option<Dec>,
    portfolio_kind: String,
    first_bought_on: Option<NaiveDate>,
    history_source: String,
}

/// `_positions_from`.
fn positions_from(
    rows: Vec<HoldingDbRow>,
    pile_ids: &HashSet<i64>,
) -> Result<Vec<Position>, Problem> {
    let mut order: Vec<HoldingKey> = Vec::new();
    let mut grouped: HashMap<HoldingKey, Vec<HoldingDbRow>> = HashMap::new();
    for row in rows {
        let key = HoldingKey {
            instrument_id: row.instrument_id,
            broker_account_id: row.broker_account_id,
        };
        if !grouped.contains_key(&key) {
            order.push(key);
        }
        grouped.entry(key).or_default().push(row);
    }
    let zero = Dec::lit("0");
    let mut positions = Vec::with_capacity(order.len());
    for key in order {
        let group = grouped.remove(&key).unwrap_or_default();
        let capital: Vec<&HoldingDbRow> = group
            .iter()
            .filter(|r| r.portfolio_kind == "CAPITAL")
            .collect();
        let monitoring: Vec<i64> = group
            .iter()
            .filter(|r| r.portfolio_kind == "MONITORING")
            .map(|r| r.portfolio_id)
            .collect();
        let source_row = match capital.first() {
            Some(r) => *r,
            None => {
                // max(group, key=quantity or 0): the first of the largest.
                let mut best = &group[0];
                for r in &group[1..] {
                    let q = r.quantity.clone().unwrap_or_else(|| zero.clone());
                    let b = best.quantity.clone().unwrap_or_else(|| zero.clone());
                    if q > b {
                        best = r;
                    }
                }
                best
            }
        };
        let mut sorted_capital = capital.clone();
        sorted_capital.sort_by_key(|r| r.portfolio_id);
        let mut filed: Vec<(i64, Dec)> = Vec::new();
        for r in sorted_capital {
            if let Some(q) = &r.quantity {
                match filed.iter_mut().find(|(p, _)| *p == r.portfolio_id) {
                    Some(slot) => slot.1 = q.clone(),
                    None => filed.push((r.portfolio_id, q.clone())),
                }
            }
        }
        let quantity = if filed.is_empty() {
            source_row.quantity.clone().unwrap_or_else(|| zero.clone())
        } else {
            filed.iter().fold(Dec::lit("0"), |acc, (_, q)| acc.add(q))
        };
        let slices: Vec<(i64, Dec)> = filed
            .iter()
            .filter(|(p, _)| !pile_ids.contains(p))
            .cloned()
            .collect();
        positions.push(Position {
            key,
            holding: Holding::new(key, quantity, source_row.avg_price.clone()).map_err(fail)?,
            capital_slices: slices,
            monitoring_portfolio_ids: monitoring,
            first_bought_on: source_row.first_bought_on,
            history_source: source_row.history_source.clone(),
        });
    }
    Ok(positions)
}

/// `_last_session_on_or_before`.
pub(crate) fn last_session_on_or_before(
    day: NaiveDate,
    closed: &HashSet<NaiveDate>,
    open_days: &HashSet<NaiveDate>,
) -> NaiveDate {
    let mut candidate = day;
    for _ in 0..CALENDAR_LOOKBACK_DAYS {
        if open_days.contains(&candidate) {
            return candidate;
        }
        if !closed.contains(&candidate) && candidate.weekday().num_days_from_monday() < 5 {
            return candidate;
        }
        candidate -= Duration::days(1);
    }
    candidate
}

async fn closes_on_or_before(
    state: &AppState,
    ids: &[i64],
    day: NaiveDate,
) -> Result<HashMap<i64, Dec>, Problem> {
    let rows = sqlx::query(
        "SELECT instrument_id, close_raw FROM (SELECT instrument_id, close_raw, \
         row_number() OVER (PARTITION BY instrument_id ORDER BY date DESC) AS recency \
         FROM ohlcv_daily WHERE instrument_id = ANY($1) AND date <= $2 AND date >= $3) AS anon_1 \
         WHERE recency = 1",
    )
    .bind(ids)
    .bind(day)
    .bind(day - Duration::days(CALENDAR_LOOKBACK_DAYS))
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut out = HashMap::new();
    for row in &rows {
        let close = db::opt_pdec(row, "close_raw")?;
        if let Some(c) = close {
            out.insert(db::get::<i64>(row, "instrument_id")?, c);
        } else {
            return Err(super::fail("close_raw is NULL"));
        }
    }
    Ok(out)
}

/// `live_prices_by_instrument`'s gates, without a network call: the broker-holdings read needs
/// `DRY_RUN` off, the quotes read needs `BASKFY_LIVE_QUOTES` on, and both need
/// `BASKFY_KITE_API_KEY` and a stored session. On a deployment with no usable Kite session both
/// are shut and the overlay is empty — Python's answer too. When one may be open, Python would
/// read Kite; that read is not ported (DECISIONS-RS, api-portfolio P3), so the close stands and it is logged.
fn live_overlay_possible() -> bool {
    let flag = |name: &str| {
        let raw = std::env::var(name)
            .unwrap_or_else(|_| "true".to_owned())
            .trim()
            .to_lowercase();
        matches!(raw.as_str(), "" | "1" | "true" | "yes" | "on")
    };
    let api_key = !std::env::var("BASKFY_KITE_API_KEY")
        .unwrap_or_default()
        .trim()
        .is_empty();
    api_key && (!flag("DRY_RUN") || flag("BASKFY_LIVE_QUOTES"))
}

/// `_load_prices`.
async fn load_prices(state: &AppState, ids: &[i64]) -> Result<Prices, Problem> {
    if ids.is_empty() {
        return Ok(Prices::default());
    }
    let rows = sqlx::query(
        "SELECT instrument_id, date, close_raw, recency FROM (SELECT instrument_id, date, \
         close_raw, row_number() OVER (PARTITION BY instrument_id ORDER BY date DESC) AS recency \
         FROM ohlcv_daily WHERE instrument_id = ANY($1)) AS anon_1 WHERE recency <= 2 \
         ORDER BY instrument_id, recency",
    )
    .bind(ids)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut prices = Prices::default();
    for row in &rows {
        let id: i64 = db::get(row, "instrument_id")?;
        let recency: i64 = db::get(row, "recency")?;
        let close =
            db::opt_pdec(row, "close_raw")?.ok_or_else(|| super::fail("close_raw is NULL"))?;
        if recency == 1 {
            prices.latest.insert(id, close);
            prices.dated.insert(id, db::date(row, "date")?);
        } else {
            prices.previous.insert(id, close);
        }
    }
    let now = now_ist();
    let today = now.date_naive();
    let (closed, open_days) = calendar_near(state, today).await?;
    let open_at = NaiveTime::from_hms_opt(9, 15, 0).unwrap_or_default();
    let quote_bound = if now.time() >= open_at {
        today
    } else {
        today - Duration::days(1)
    };
    let quote_session = last_session_on_or_before(quote_bound, &closed, &open_days);
    if live_overlay_possible() {
        crate::logging::warning(
            "baskfy_api.routers.portfolio_overview",
            "a Kite session may exist but the live overlay is not ported; serving the close",
            &[],
        );
    }
    let week_since = last_session_on_or_before(
        quote_session
            - Duration::days(i64::from(quote_session.weekday().num_days_from_monday()) + 1),
        &closed,
        &open_days,
    );
    prices.week_base = closes_on_or_before(state, ids, week_since).await?;
    prices.week_since = Some(week_since);
    let month_since = last_session_on_or_before(
        quote_session - Duration::days(MONTH_WINDOW_DAYS),
        &closed,
        &open_days,
    );
    prices.month_base = closes_on_or_before(state, ids, month_since).await?;
    prices.month_since = Some(month_since);
    let mut unpriced: Vec<i64> = ids
        .iter()
        .copied()
        .filter(|i| !prices.latest.contains_key(i))
        .collect::<BTreeSet<i64>>()
        .into_iter()
        .collect();
    unpriced.sort_unstable();
    prices.unpriced = unpriced;
    prices.as_of = prices.dated.values().max().copied();
    Ok(prices)
}

/// `_calendar_near`: (named closures, named sessions) over the last few weeks.
async fn calendar_near(
    state: &AppState,
    today: NaiveDate,
) -> Result<(HashSet<NaiveDate>, HashSet<NaiveDate>), Problem> {
    let rows = sqlx::query(
        "SELECT date, is_trading_day FROM trading_day WHERE exchange_id = $1 AND date >= $2 \
         AND date <= $3",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(today - Duration::days(MONTH_WINDOW_DAYS + CALENDAR_LOOKBACK_DAYS + 7))
    .bind(today)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut closed = HashSet::new();
    let mut open_days = HashSet::new();
    for row in &rows {
        let d = db::date(row, "date")?;
        if db::get::<bool>(row, "is_trading_day")? {
            open_days.insert(d);
        } else {
            closed.insert(d);
        }
    }
    Ok((closed, open_days))
}

struct SleeveRow {
    portfolio_id: i64,
    basket_id: Option<i64>,
    screen_id: Option<i64>,
}

/// `_load_ledger`.
pub(crate) async fn load_ledger(state: &AppState, user_id: i64) -> Result<Ledger, Problem> {
    let pool = state.db.pool();
    let portfolio_rows = sqlx::query(
        "SELECT id, name, kind, source, started_on, benchmark_index_id::bigint AS benchmark_index_id, \
         is_broker_pile FROM portfolio WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(db::fail)?;
    let mut ledger = Ledger::default();
    for row in &portfolio_rows {
        let id: i64 = db::get(row, "id")?;
        if db::get::<bool>(row, "is_broker_pile")? {
            ledger.pile_ids.insert(id);
        }
        let kind: String = db::get(row, "kind")?;
        let source: String = db::get(row, "source")?;
        ledger.portfolios.insert(
            id,
            LedgerPortfolio {
                portfolio_id: id,
                name: db::get(row, "name")?,
                kind: PortfolioKind::parse(&kind).map_err(fail)?,
                source: PortfolioSource::parse(&source).map_err(fail)?,
                started_on: db::date(row, "started_on")?,
            },
        );
        ledger.rows.insert(
            id,
            PortfolioRow {
                benchmark_index_id: db::get(row, "benchmark_index_id")?,
            },
        );
    }

    let holding_rows = sqlx::query(
        "SELECT h.portfolio_id, h.instrument_id, h.broker_account_id, h.quantity, h.avg_price, \
         h.portfolio_kind, h.first_bought_on, h.history_source FROM portfolio_holding h \
         JOIN portfolio p ON p.id = h.portfolio_id WHERE p.user_id = $1 \
         ORDER BY h.instrument_id, h.broker_account_id, h.portfolio_id",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(db::fail)?;
    let mut raw = Vec::with_capacity(holding_rows.len());
    for row in &holding_rows {
        raw.push(HoldingDbRow {
            portfolio_id: db::get(row, "portfolio_id")?,
            instrument_id: db::get(row, "instrument_id")?,
            broker_account_id: db::get(row, "broker_account_id")?,
            quantity: db::opt_pdec(row, "quantity")?,
            avg_price: db::opt_pdec(row, "avg_price")?,
            portfolio_kind: db::get(row, "portfolio_kind")?,
            first_bought_on: db::opt_date(row, "first_bought_on")?,
            history_source: db::get(row, "history_source")?,
        });
    }
    ledger.positions = positions_from(raw, &ledger.pile_ids)?;

    let instrument_ids: Vec<i64> = ledger
        .positions
        .iter()
        .map(|p| p.key.instrument_id)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    ledger.prices = load_prices(state, &instrument_ids).await?;
    if !instrument_ids.is_empty() {
        let rows = sqlx::query("SELECT id, symbol, name FROM instrument WHERE id = ANY($1)")
            .bind(&instrument_ids)
            .fetch_all(pool)
            .await
            .map_err(db::fail)?;
        for row in &rows {
            let id: i64 = db::get(row, "id")?;
            ledger.instruments.insert(
                id,
                InstrumentRow {
                    id,
                    symbol: db::get(row, "symbol")?,
                    name: db::get(row, "name")?,
                },
            );
        }
    }

    let broker_rows = sqlx::query(
        "SELECT id, broker_id, label, holdings_synced_at FROM broker_account WHERE user_id = $1 \
         ORDER BY id",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(db::fail)?;
    for row in &broker_rows {
        let id: i64 = db::get(row, "id")?;
        ledger.brokers.insert(
            id,
            BrokerRow {
                id,
                broker_id: db::get(row, "broker_id")?,
                label: db::get(row, "label")?,
                holdings_synced_at: db::opt_ts(row, "holdings_synced_at")?,
            },
        );
    }
    let broker_ids: Vec<i64> = ledger.brokers.keys().copied().collect();
    if !broker_ids.is_empty() {
        let rows = sqlx::query(
            "SELECT broker_account_id, balance, as_of FROM broker_cash \
             WHERE broker_account_id = ANY($1) ORDER BY broker_account_id",
        )
        .bind(&broker_ids)
        .fetch_all(pool)
        .await
        .map_err(db::fail)?;
        for row in &rows {
            ledger.broker_cash.push(BrokerCashRow {
                broker_account_id: db::get(row, "broker_account_id")?,
                balance: db::pdec(row, "balance")?,
                as_of: db::date(row, "as_of")?,
            });
        }
        let rows = sqlx::query(
            "SELECT broker_account_id, portfolio_id, kind, amount, occurred_on, instrument_id, \
             quantity, note FROM portfolio_cash_flow WHERE broker_account_id = ANY($1) \
             ORDER BY occurred_on, id",
        )
        .bind(&broker_ids)
        .fetch_all(pool)
        .await
        .map_err(db::fail)?;
        for row in &rows {
            let kind: String = db::get(row, "kind")?;
            let flow = FlowRow {
                broker_account_id: db::get(row, "broker_account_id")?,
                portfolio_id: db::get(row, "portfolio_id")?,
                kind: CashFlowKind::parse(&kind).map_err(fail)?,
                amount: db::pdec(row, "amount")?,
                occurred_on: db::date(row, "occurred_on")?,
                instrument_id: db::get(row, "instrument_id")?,
                quantity: db::opt_pdec(row, "quantity")?,
                note: db::get(row, "note")?,
            };
            ledger.flows.push(
                LedgerCashFlow::new(
                    flow.broker_account_id,
                    flow.kind,
                    flow.amount.clone(),
                    flow.occurred_on,
                    flow.portfolio_id,
                    flow.instrument_id,
                    flow.quantity.as_ref(),
                )
                .map_err(fail)?,
            );
            ledger.flow_rows.push(flow);
        }
    }

    let item_rows = sqlx::query(
        "SELECT id, instrument_id, broker_account_id, quantity, reason, state, \
         suggested_portfolio_id, resolved_portfolio_id, detected_on, resolved_at \
         FROM reconciliation_item WHERE user_id = $1 ORDER BY detected_on DESC, id DESC",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(db::fail)?;
    for row in &item_rows {
        let id: i64 = db::get(row, "id")?;
        let reason: String = db::get(row, "reason")?;
        let state_text: String = db::get(row, "state")?;
        let resolved_at = db::opt_ts(row, "resolved_at")?;
        // `int(x) if x else None`: a zero id reads as none, as Python's truthiness does.
        let nonzero = |v: Option<i64>| v.filter(|x| *x != 0);
        let entry = InboxEntry::new(
            id,
            LedgerQuestion {
                key: HoldingKey {
                    instrument_id: db::get(row, "instrument_id")?,
                    broker_account_id: db::get(row, "broker_account_id")?,
                },
                quantity: db::pdec(row, "quantity")?,
                reason: ReconciliationReason::parse(&reason).map_err(fail)?,
                suggested_portfolio_id: nonzero(db::get(row, "suggested_portfolio_id")?),
            },
            db::date(row, "detected_on")?,
            ReconciliationState::parse(&state_text).map_err(fail)?,
            nonzero(db::get(row, "resolved_portfolio_id")?),
            resolved_at.map(|t| t.date_naive()),
        )
        .map_err(fail)?;
        ledger.entries.push(entry);
        ledger.item_resolved_at.insert(id, resolved_at);
    }

    let portfolio_ids: Vec<i64> = ledger.portfolios.keys().copied().collect();
    let mut sleeves = Vec::new();
    if !portfolio_ids.is_empty() {
        let rows = sqlx::query(
            "SELECT portfolio_id, basket_id, screen_id FROM portfolio_sleeve \
             WHERE portfolio_id = ANY($1) ORDER BY sort_order, id",
        )
        .bind(&portfolio_ids)
        .fetch_all(pool)
        .await
        .map_err(db::fail)?;
        for row in &rows {
            sleeves.push(SleeveRow {
                portfolio_id: db::get(row, "portfolio_id")?,
                basket_id: db::get(row, "basket_id")?,
                screen_id: db::get(row, "screen_id")?,
            });
        }
    }
    load_sources(state, &sleeves, &mut ledger).await?;

    let benchmark_ids: Vec<i64> = ledger
        .rows
        .values()
        .filter_map(|r| r.benchmark_index_id.filter(|id| *id != 0))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if !benchmark_ids.is_empty() {
        let rows = sqlx::query(
            "SELECT id::bigint AS id, name FROM index_def WHERE id = ANY($1::bigint[])",
        )
        .bind(&benchmark_ids)
        .fetch_all(pool)
        .await
        .map_err(db::fail)?;
        for row in &rows {
            ledger
                .benchmarks
                .insert(db::get(row, "id")?, db::get(row, "name")?);
        }
    }

    ledger.rebalance_due_portfolio_ids = rebalance_due(state, user_id, &sleeves).await?;
    Ok(ledger)
}

/// `_load_sources`: the basket, publisher and screen behind each portfolio, via its sleeves.
async fn load_sources(
    state: &AppState,
    sleeves: &[SleeveRow],
    ledger: &mut Ledger,
) -> Result<(), Problem> {
    let pool = state.db.pool();
    let basket_ids: Vec<i64> = sleeves
        .iter()
        .filter_map(|s| s.basket_id)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let screen_ids: Vec<i64> = sleeves
        .iter()
        .filter_map(|s| s.screen_id)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let mut basket_by_id: HashMap<i64, (BasketRow, String)> = HashMap::new();
    if !basket_ids.is_empty() {
        let rows = sqlx::query(
            "SELECT b.id, b.slug, b.name, m.name AS manager_name FROM cb_basket b \
             JOIN cb_manager m ON m.id = b.manager_id WHERE b.id = ANY($1)",
        )
        .bind(&basket_ids)
        .fetch_all(pool)
        .await
        .map_err(db::fail)?;
        for row in &rows {
            let id: i64 = db::get(row, "id")?;
            basket_by_id.insert(
                id,
                (
                    BasketRow {
                        id,
                        slug: db::get(row, "slug")?,
                        name: db::get(row, "name")?,
                    },
                    db::get(row, "manager_name")?,
                ),
            );
        }
    }
    let mut screen_by_id: HashMap<i64, ScreenRow> = HashMap::new();
    if !screen_ids.is_empty() {
        let rows = sqlx::query("SELECT id, public_id, name FROM screen WHERE id = ANY($1)")
            .bind(&screen_ids)
            .fetch_all(pool)
            .await
            .map_err(db::fail)?;
        for row in &rows {
            screen_by_id.insert(
                db::get(row, "id")?,
                ScreenRow {
                    public_id: db::get(row, "public_id")?,
                    name: db::get(row, "name")?,
                },
            );
        }
    }
    for sleeve in sleeves {
        let pid = sleeve.portfolio_id;
        if let Some(bid) = sleeve.basket_id
            && !ledger.baskets.contains_key(&pid)
            && let Some((basket, publisher)) = basket_by_id.get(&bid)
        {
            ledger.baskets.insert(pid, basket.clone());
            ledger.publishers.insert(pid, publisher.clone());
        }
        if let Some(sid) = sleeve.screen_id
            && !ledger.screens.contains_key(&pid)
            && let Some(screen) = screen_by_id.get(&sid)
        {
            ledger.screens.insert(pid, screen.clone());
        }
    }
    Ok(())
}

/// `_rebalance_due`.
async fn rebalance_due(
    state: &AppState,
    user_id: i64,
    sleeves: &[SleeveRow],
) -> Result<BTreeSet<i64>, Problem> {
    let mut basket_to_portfolios: BTreeMap<i64, Vec<i64>> = BTreeMap::new();
    for s in sleeves {
        if let Some(b) = s.basket_id {
            basket_to_portfolios
                .entry(b)
                .or_default()
                .push(s.portfolio_id);
        }
    }
    if basket_to_portfolios.is_empty() {
        return Ok(BTreeSet::new());
    }
    let ids: Vec<i64> = basket_to_portfolios.keys().copied().collect();
    let rows = sqlx::query(
        "SELECT v.basket_id FROM cb_basket_version v JOIN cb_user_rebalance_state s \
         ON s.version_id = v.id WHERE s.user_id = $1 AND s.state = 'PENDING' \
         AND v.basket_id = ANY($2)",
    )
    .bind(user_id)
    .bind(&ids)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut due = BTreeSet::new();
    for row in &rows {
        let b: i64 = db::get(row, "basket_id")?;
        if let Some(ps) = basket_to_portfolios.get(&b) {
            due.extend(ps.iter().copied());
        }
    }
    Ok(due)
}

/// A `portfolio_nav_daily` row.
#[derive(Debug, Clone)]
pub(crate) struct NavRow {
    pub(crate) portfolio_id: Option<i64>,
    pub(crate) date: NaiveDate,
    pub(crate) market_value: Dec,
    pub(crate) cash: Dec,
    pub(crate) net_flow: Dec,
    pub(crate) pending_reconciliation: bool,
}

fn nav_row(row: &sqlx::postgres::PgRow) -> Result<NavRow, Problem> {
    Ok(NavRow {
        portfolio_id: db::get(row, "portfolio_id")?,
        date: db::date(row, "date")?,
        market_value: db::pdec(row, "market_value")?,
        cash: db::pdec(row, "cash")?,
        net_flow: db::pdec(row, "net_flow")?,
        pending_reconciliation: db::get(row, "pending_reconciliation")?,
    })
}

/// `_nav_rows(user, portfolio)`: one series (the consolidated one for `None`), by date.
pub(crate) async fn nav_rows(
    state: &AppState,
    user_id: i64,
    portfolio_id: Option<i64>,
) -> Result<Vec<NavRow>, Problem> {
    let rows = sqlx::query(
        "SELECT portfolio_id, date, market_value, cash, net_flow, pending_reconciliation \
         FROM portfolio_nav_daily WHERE user_id = $1 AND portfolio_id IS NOT DISTINCT FROM $2 \
         ORDER BY date",
    )
    .bind(user_id)
    .bind(portfolio_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    rows.iter().map(nav_row).collect()
}

/// `_nav_by_portfolio`: every series this user has, grouped.
pub(crate) async fn nav_by_portfolio(
    state: &AppState,
    user_id: i64,
) -> Result<HashMap<Option<i64>, Vec<NavRow>>, Problem> {
    let rows = sqlx::query(
        "SELECT portfolio_id, date, market_value, cash, net_flow, pending_reconciliation \
         FROM portfolio_nav_daily WHERE user_id = $1 ORDER BY date",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut grouped: HashMap<Option<i64>, Vec<NavRow>> = HashMap::new();
    for row in &rows {
        let r = nav_row(row)?;
        grouped.entry(r.portfolio_id).or_default().push(r);
    }
    Ok(grouped)
}

/// `_model_return_values`: the publisher's TWR over each subscribed portfolio's own window.
pub(crate) async fn model_return_values(
    state: &AppState,
    ledger: &Ledger,
) -> Result<HashMap<i64, Option<Dec>>, Problem> {
    let mut values = HashMap::new();
    let hundred = Dec::lit("100");
    for (pid, portfolio) in &ledger.portfolios {
        if portfolio.source != PortfolioSource::Subscribed {
            continue;
        }
        values.insert(*pid, None);
        let Some(basket) = ledger.baskets.get(pid) else {
            continue;
        };
        let anchor = sqlx::query(
            "SELECT since_inception_pct FROM cb_metrics WHERE basket_id = $1 AND as_of_date <= $2 \
             AND since_inception_pct IS NOT NULL ORDER BY as_of_date DESC LIMIT 1",
        )
        .bind(basket.id)
        .bind(portfolio.started_on)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db::fail)?;
        let current = sqlx::query(
            "SELECT since_inception_pct FROM cb_metrics WHERE basket_id = $1 \
             AND since_inception_pct IS NOT NULL ORDER BY as_of_date DESC LIMIT 1",
        )
        .bind(basket.id)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db::fail)?;
        let (Some(anchor), Some(current)) = (anchor, current) else {
            continue;
        };
        let anchor = db::pdec(&anchor, "since_inception_pct")?;
        let current = db::pdec(&current, "since_inception_pct")?;
        let base = Dec::int(1).add(&anchor.div(&hundred).ok_or_else(|| super::fail("div"))?);
        if base.is_zero() {
            continue;
        }
        let ratio = Dec::int(1)
            .add(&current.div(&hundred).ok_or_else(|| super::fail("div"))?)
            .div(&base)
            .ok_or_else(|| super::fail("div"))?
            .sub(&Dec::int(1));
        values.insert(*pid, Some(super::nav::quantize_return_even(&ratio)));
    }
    Ok(values)
}
