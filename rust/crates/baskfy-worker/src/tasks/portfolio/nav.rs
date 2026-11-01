//! `baskfy_worker.tasks.portfolio_nav_job` — the official end-of-day NAV (`PORTFOLIO_REDESIGN.md`
//! §5.1, §4.3, §5.2), line for line.
//!
//! The I/O half reads holdings, prices, cash flows and open reconciliation items; the arithmetic
//! is the parts of `baskfy_core.allocation_ledger` and `baskfy_core.cash_ledger` the job calls
//! ([`ledger`]), on `rust_decimal` — every operand is a stored `numeric` (quantities and prices at
//! four places, money at two), so each product and sum is exact, as Python's 28-digit context is
//! for the same values, and money is quantised half-up to paise exactly where Python quantises it
//! (house rules 8 and 9).
//!
//! The four refusals of the Python module hold here unchanged: a day with no bar gets no rows; a
//! monitoring view is never loaded into a total; an open reconciliation item, or a holding with
//! no print on or before the date, drops the holding and flags its rows `pending_reconciliation`.
//! Every write is an upsert keyed on `(user_id, date, portfolio_id)`, a prune of rows that no
//! longer earn one, and the "since grouped" mark that touches only undated `NONE` rows — so a
//! re-run of a date converges on the same table (house rule 7). Nothing here reaches a broker.

use std::collections::{BTreeSet, HashMap, HashSet};

use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row};

use super::ledger::{
    self, Allocation, CashFlowKind, Holding, HoldingKey, LedgerCashFlow, LedgerPortfolio,
    PortfolioKind,
};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};

/// `RECONCILE_OPEN`.
const RECONCILE_OPEN: &str = "OPEN";
/// `HISTORY_SOURCE_NONE`.
const HISTORY_SOURCE_NONE: &str = "NONE";

/// `NavRow`. `portfolio_id` is `None` for the consolidated row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NavRow {
    /// `user_id`.
    pub user_id: i64,
    /// `portfolio_id`.
    pub portfolio_id: Option<i64>,
    /// `market_value`.
    pub market_value: Decimal,
    /// `cash`.
    pub cash: Decimal,
    /// `net_flow`.
    pub net_flow: Decimal,
    /// `pending_reconciliation`.
    pub pending_reconciliation: bool,
}

impl NavRow {
    /// `total_value`: `money(market_value + cash)`.
    ///
    /// # Errors
    /// An overflow Python's context would have rounded instead (never on stored values).
    pub fn total_value(&self) -> Result<Decimal, WorkerError> {
        Ok(ledger::money(ledger::add(self.market_value, self.cash)?))
    }
}

/// `UserNav`.
#[derive(Debug, Clone)]
pub struct UserNav {
    /// `user_id`.
    pub user_id: i64,
    /// The consolidated row.
    pub consolidated: NavRow,
    /// One row per capital portfolio, by id.
    pub portfolio_rows: Vec<NavRow>,
    /// Unallocated stocks.
    pub unallocated_market_value: Decimal,
    /// Unallocated cash.
    pub unallocated_cash: Decimal,
    /// Holdings dropped because an open question concerns them.
    pub frozen_holdings: usize,
    /// Holdings dropped because no print exists on or before the date.
    pub unpriced_holdings: usize,
}

impl UserNav {
    /// `all_rows`: consolidated first.
    pub fn all_rows(&self) -> impl Iterator<Item = &NavRow> {
        std::iter::once(&self.consolidated).chain(self.portfolio_rows.iter())
    }
}

/// `NavRunResult`.
#[derive(Debug, Clone)]
pub struct NavRunResult {
    /// The date valued.
    pub as_of: NaiveDate,
    /// Every user valued.
    pub users: Vec<UserNav>,
    /// Rows upserted.
    pub rows_written: usize,
    /// Rows pruned.
    pub rows_removed: usize,
    /// "Since grouped" marks stamped.
    pub marks_written: usize,
    /// Set when the job wrote nothing on purpose.
    pub skipped_reason: Option<String>,
}

impl NavRunResult {
    /// `as_json()`, key order and all; money as strings.
    ///
    /// # Errors
    /// As [`NavRow::total_value`].
    pub fn as_json(&self) -> Result<OrderedJson, WorkerError> {
        let pending = self
            .users
            .iter()
            .flat_map(UserNav::all_rows)
            .filter(|row| row.pending_reconciliation)
            .count();
        let mut net_worth = Vec::with_capacity(self.users.len());
        for user in &self.users {
            net_worth.push((
                user.user_id.to_string(),
                s(user.consolidated.total_value()?.to_string()),
            ));
        }
        Ok(object([
            ("as_of", s(self.as_of.format("%Y-%m-%d").to_string())),
            ("users", count(self.users.len())),
            ("rows_written", count(self.rows_written)),
            ("rows_removed", count(self.rows_removed)),
            ("marks_written", count(self.marks_written)),
            ("pending_reconciliation", count(pending)),
            ("net_worth", OrderedJson::Object(net_worth)),
            (
                "skipped_reason",
                self.skipped_reason
                    .as_ref()
                    .map_or(OrderedJson::Null, |r| s(r.clone())),
            ),
        ]))
    }
}

fn count(n: usize) -> OrderedJson {
    int(i64::try_from(n).unwrap_or(i64::MAX))
}

/// `run_portfolio_nav(session, as_of)` on the caller's transaction (the caller commits).
///
/// # Errors
/// A database failure; the `ValueError`s the ledger raises on a row that breaks §4.1/§4.4.
pub async fn run_portfolio_nav(
    conn: &mut PgConnection,
    as_of: NaiveDate,
) -> Result<NavRunResult, WorkerError> {
    let mut result = NavRunResult {
        as_of,
        users: Vec::new(),
        rows_written: 0,
        rows_removed: 0,
        marks_written: 0,
        skipped_reason: None,
    };
    if !market_printed(conn, as_of).await? {
        result.skipped_reason = Some(format!(
            "no bars on {}; the market printed no close",
            as_of.format("%Y-%m-%d")
        ));
        return Ok(result);
    }
    for user_id in users_with_a_ledger(conn).await? {
        let Some(user_nav) = value_user(conn, as_of, user_id).await? else {
            result.rows_removed += remove_every_row(conn, as_of, user_id).await?;
            continue;
        };
        result.rows_written += write_rows(conn, as_of, &user_nav).await?;
        result.rows_removed += remove_stale_rows(conn, as_of, &user_nav).await?;
        result.marks_written += mark_since_grouped(conn, as_of, &user_nav).await?;
        result.users.push(user_nav);
    }
    Ok(result)
}

// ---------------------------------------------------------------------------------------------
// Reading
// ---------------------------------------------------------------------------------------------

/// `_market_printed`: any bar on the date.
async fn market_printed(conn: &mut PgConnection, as_of: NaiveDate) -> Result<bool, WorkerError> {
    let found: Option<i64> = sqlx::query_scalar(
        "SELECT ohlcv_daily.instrument_id FROM ohlcv_daily WHERE ohlcv_daily.date = $1 LIMIT 1",
    )
    .bind(as_of)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(found.is_some())
}

/// `_users_with_a_ledger`: a portfolio or a broker account, sorted.
async fn users_with_a_ledger(conn: &mut PgConnection) -> Result<Vec<i64>, WorkerError> {
    Ok(sqlx::query_scalar(
        "SELECT portfolio.user_id FROM portfolio UNION SELECT broker_account.user_id \
         FROM broker_account ORDER BY user_id",
    )
    .fetch_all(&mut *conn)
    .await?)
}

/// `_load_portfolios`: both kinds, in the order the database returns them.
async fn load_portfolios(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<LedgerPortfolio>, WorkerError> {
    let rows = sqlx::query(
        "SELECT portfolio.id, portfolio.name, portfolio.kind, portfolio.source, \
         portfolio.started_on FROM portfolio WHERE portfolio.user_id = $1",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    let mut out: Vec<LedgerPortfolio> = Vec::with_capacity(rows.len());
    for row in &rows {
        let kind: String = row.try_get("kind")?;
        let source: String = row.try_get("source")?;
        let portfolio = LedgerPortfolio {
            portfolio_id: row.try_get("id")?,
            name: row.try_get("name")?,
            kind: PortfolioKind::parse(&kind)?,
            started_on: row.try_get("started_on")?,
        };
        ledger::check_portfolio_source(&source)?;
        // A dict keyed by id: a repeated id (impossible under the primary key) keeps its first
        // position and its last value.
        if let Some(existing) = out
            .iter_mut()
            .find(|p| p.portfolio_id == portfolio.portfolio_id)
        {
            *existing = portfolio;
        } else {
            out.push(portfolio);
        }
    }
    Ok(out)
}

/// `_load_holdings`: `(portfolio_id, key, quantity)` for every positive holding in a capital
/// portfolio, in the order the database returns them.
async fn load_holdings(
    conn: &mut PgConnection,
    capital_ids: &[i64],
) -> Result<Vec<(i64, HoldingKey, Decimal)>, WorkerError> {
    if capital_ids.is_empty() {
        return Ok(Vec::new());
    }
    let rows = sqlx::query(
        "SELECT portfolio_holding.portfolio_id, portfolio_holding.instrument_id, \
         portfolio_holding.broker_account_id, portfolio_holding.quantity \
         FROM portfolio_holding WHERE portfolio_holding.portfolio_id = ANY($1)",
    )
    .bind(capital_ids)
    .fetch_all(&mut *conn)
    .await?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        let quantity: Option<Decimal> = row.try_get("quantity")?;
        let Some(quantity) = quantity.filter(|q| *q > Decimal::ZERO) else {
            continue;
        };
        let key = HoldingKey {
            instrument_id: row.try_get("instrument_id")?,
            broker_account_id: row.try_get("broker_account_id")?,
        };
        out.push((row.try_get("portfolio_id")?, key, quantity));
    }
    Ok(out)
}

/// `_load_prices`: the latest `close_raw` on or before the date, per instrument.
async fn load_prices(
    conn: &mut PgConnection,
    instrument_ids: &[i64],
    as_of: NaiveDate,
) -> Result<HashMap<i64, Decimal>, WorkerError> {
    if instrument_ids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = sqlx::query(
        "SELECT DISTINCT ON (ohlcv_daily.instrument_id) ohlcv_daily.instrument_id, \
         ohlcv_daily.close_raw FROM ohlcv_daily \
         WHERE ohlcv_daily.instrument_id = ANY($1) AND ohlcv_daily.date <= $2 \
         ORDER BY ohlcv_daily.instrument_id, ohlcv_daily.date DESC",
    )
    .bind(instrument_ids)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut prices = HashMap::with_capacity(rows.len());
    for row in &rows {
        let close: Option<Decimal> = row.try_get("close_raw")?;
        // `Decimal(None)` is Python's TypeError; `close_raw` is NOT NULL in the schema.
        let close = close.ok_or_else(|| {
            WorkerError::python(
                "TypeError",
                "conversion from NoneType to Decimal is not supported",
            )
        })?;
        prices.insert(row.try_get("instrument_id")?, close);
    }
    Ok(prices)
}

/// `_load_open_reconciliations`: OPEN items detected on or before the date.
async fn load_open_reconciliations(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<HashSet<HoldingKey>, WorkerError> {
    let rows = sqlx::query(
        "SELECT reconciliation_item.instrument_id, reconciliation_item.broker_account_id \
         FROM reconciliation_item WHERE reconciliation_item.user_id = $1 \
         AND reconciliation_item.state = $2 AND reconciliation_item.detected_on <= $3",
    )
    .bind(user_id)
    .bind(RECONCILE_OPEN)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut keys = HashSet::with_capacity(rows.len());
    for row in &rows {
        keys.insert(HoldingKey {
            instrument_id: row.try_get("instrument_id")?,
            broker_account_id: row.try_get("broker_account_id")?,
        });
    }
    Ok(keys)
}

/// `_load_flows`: every flow in the user's broker accounts up to and including the date, each
/// through `PortfolioCashFlow`'s constructor.
async fn load_flows(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<Vec<LedgerCashFlow>, WorkerError> {
    let rows = sqlx::query(
        "SELECT portfolio_cash_flow.broker_account_id, portfolio_cash_flow.portfolio_id, \
         portfolio_cash_flow.kind, portfolio_cash_flow.amount, portfolio_cash_flow.occurred_on, \
         portfolio_cash_flow.instrument_id, portfolio_cash_flow.quantity \
         FROM portfolio_cash_flow JOIN broker_account \
         ON broker_account.id = portfolio_cash_flow.broker_account_id \
         WHERE broker_account.user_id = $1 AND portfolio_cash_flow.occurred_on <= $2",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut flows = Vec::with_capacity(rows.len());
    for row in &rows {
        let kind: String = row.try_get("kind")?;
        flows.push(LedgerCashFlow::new(
            row.try_get("broker_account_id")?,
            CashFlowKind::parse(&kind)?,
            row.try_get("amount")?,
            row.try_get("occurred_on")?,
            row.try_get("portfolio_id")?,
            row.try_get("instrument_id")?,
            row.try_get("quantity")?,
        )?);
    }
    Ok(flows)
}

// ---------------------------------------------------------------------------------------------
// Valuing one user
// ---------------------------------------------------------------------------------------------

/// `_value_user`: the rows one user's day resolves to; `None` when they should have none.
async fn value_user(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    user_id: i64,
) -> Result<Option<UserNav>, WorkerError> {
    let portfolios = load_portfolios(conn, user_id).await?;
    let capital_ids: Vec<i64> = portfolios
        .iter()
        .filter(|p| p.kind == PortfolioKind::Capital)
        .map(|p| p.portfolio_id)
        .collect::<BTreeSet<i64>>()
        .into_iter()
        .collect();
    let flows = load_flows(conn, user_id, as_of).await?;
    if portfolios.is_empty() && flows.is_empty() {
        return Ok(None);
    }
    let holding_rows = load_holdings(conn, &capital_ids).await?;

    refuse_monitoring_flows(&portfolios, &flows)?;

    let frozen_keys = load_open_reconciliations(conn, user_id, as_of).await?;
    let instrument_ids: Vec<i64> = holding_rows
        .iter()
        .map(|(_, key, _)| key.instrument_id)
        .collect::<BTreeSet<i64>>()
        .into_iter()
        .collect();
    let prices = load_prices(conn, &instrument_ids, as_of).await?;

    let mut holdings: Vec<Holding> = Vec::new();
    let mut allocations: Vec<Allocation> = Vec::new();
    let mut pending_portfolios: HashSet<i64> = HashSet::new();
    let mut frozen_count = 0usize;
    let mut unpriced_count = 0usize;
    for (portfolio_id, key, quantity) in &holding_rows {
        if frozen_keys.contains(key) {
            frozen_count += 1;
            pending_portfolios.insert(*portfolio_id);
            continue;
        }
        if !prices.contains_key(&key.instrument_id) {
            unpriced_count += 1;
            pending_portfolios.insert(*portfolio_id);
            continue;
        }
        holdings.push(Holding::new(*key, *quantity)?);
        allocations.push(Allocation::new(*key, Some(*portfolio_id), *quantity)?);
    }

    let values = ledger::portfolio_values(&holdings, &allocations, &portfolios, &prices)?;
    let consolidated_market_value =
        ledger::consolidated_value(&holdings, &allocations, &portfolios, &prices)?;

    let portfolio_cash = ledger::portfolio_cash_by_portfolio(&flows)?;
    let mut unallocated_cash = Decimal::ZERO;
    for (_, value) in ledger::unallocated_cash_by_broker(&flows)? {
        unallocated_cash = ledger::add(unallocated_cash, value)?;
    }
    let day_flows: Vec<&LedgerCashFlow> = flows.iter().filter(|f| f.occurred_on == as_of).collect();

    let consolidated_pending = !frozen_keys.is_empty() || !pending_portfolios.is_empty();

    let mut rows = Vec::with_capacity(capital_ids.len());
    for portfolio_id in &capital_ids {
        rows.push(NavRow {
            user_id,
            portfolio_id: Some(*portfolio_id),
            market_value: values.get(Some(*portfolio_id)).unwrap_or(Decimal::ZERO),
            cash: portfolio_cash
                .iter()
                .find(|(id, _)| id == portfolio_id)
                .map_or(Decimal::ZERO, |(_, v)| *v),
            net_flow: internal_net_flow(&day_flows, *portfolio_id)?,
            pending_reconciliation: pending_portfolios.contains(portfolio_id),
        });
    }
    let consolidated = NavRow {
        user_id,
        portfolio_id: None,
        market_value: consolidated_market_value,
        cash: ledger::total_cash(&flows)?,
        net_flow: external_net_flow(&day_flows)?,
        pending_reconciliation: consolidated_pending,
    };
    Ok(Some(UserNav {
        user_id,
        consolidated,
        portfolio_rows: rows,
        unallocated_market_value: values.get(None).unwrap_or(Decimal::ZERO),
        unallocated_cash: ledger::money(unallocated_cash),
        frozen_holdings: frozen_count,
        unpriced_holdings: unpriced_count,
    }))
}

/// `_refuse_monitoring_flows`: a flow naming another tenant's portfolio or a monitoring view is a
/// writer bug, refused rather than skipped.
fn refuse_monitoring_flows(
    portfolios: &[LedgerPortfolio],
    flows: &[LedgerCashFlow],
) -> Result<(), WorkerError> {
    for flow in flows {
        let Some(portfolio_id) = flow.portfolio_id else {
            continue;
        };
        let Some(portfolio) = portfolios.iter().find(|p| p.portfolio_id == portfolio_id) else {
            return Err(ledger::value_error(format!(
                "cash flow names portfolio {portfolio_id}, which does not belong to this user; a \
                 flow is attributed to the broker account it moved in, so this is a \
                 cross-tenant row and not a valuation problem"
            )));
        };
        if portfolio.kind == PortfolioKind::Monitoring {
            return Err(ledger::value_error(format!(
                "{} is a monitoring view and holds no cash: it overlaps other portfolios by \
                 design and is excluded from every total (spec section 4.1). The flow would \
                 enter consolidated cash and no portfolio row, breaking acceptance criterion 1 \
                 by its own amount",
                baskfy_providers::py::repr_str(&portfolio.name)
            )));
        }
    }
    Ok(())
}

/// `_internal_net_flow`: `ASSIGN` minus `RELEASE` for one portfolio on the day.
fn internal_net_flow(
    day_flows: &[&LedgerCashFlow],
    portfolio_id: i64,
) -> Result<Decimal, WorkerError> {
    let mut total = Decimal::ZERO;
    for flow in day_flows {
        if flow.portfolio_id != Some(portfolio_id) {
            continue;
        }
        match flow.kind {
            CashFlowKind::Assign => total = ledger::add(total, flow.amount)?,
            CashFlowKind::Release => total = ledger::sub(total, flow.amount)?,
            _ => {}
        }
    }
    Ok(ledger::money(total))
}

/// `_external_net_flow`: deposits minus withdrawals on the day.
fn external_net_flow(day_flows: &[&LedgerCashFlow]) -> Result<Decimal, WorkerError> {
    let mut total = Decimal::ZERO;
    for flow in day_flows {
        match flow.kind {
            CashFlowKind::ExternalDeposit => total = ledger::add(total, flow.amount)?,
            CashFlowKind::ExternalWithdrawal => total = ledger::sub(total, flow.amount)?,
            _ => {}
        }
    }
    Ok(ledger::money(total))
}

// ---------------------------------------------------------------------------------------------
// Writing — upsert, prune, mark
// ---------------------------------------------------------------------------------------------

/// `_write_rows`: one statement, `ON CONFLICT (user_id, date, portfolio_id) DO UPDATE`.
async fn write_rows(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    user: &UserNav,
) -> Result<usize, WorkerError> {
    let rows: Vec<&NavRow> = user.all_rows().collect();
    let mut q: sqlx::QueryBuilder<'_, sqlx::Postgres> = sqlx::QueryBuilder::new(
        "INSERT INTO portfolio_nav_daily (user_id, portfolio_id, date, market_value, cash, \
         net_flow, pending_reconciliation) ",
    );
    q.push_values(rows.iter(), |mut b, row| {
        b.push_bind(row.user_id)
            .push_bind(row.portfolio_id)
            .push_bind(as_of)
            .push_bind(row.market_value)
            .push_bind(row.cash)
            .push_bind(row.net_flow)
            .push_bind(row.pending_reconciliation);
    });
    q.push(
        " ON CONFLICT (user_id, date, portfolio_id) DO UPDATE SET \
         market_value = excluded.market_value, cash = excluded.cash, \
         net_flow = excluded.net_flow, pending_reconciliation = excluded.pending_reconciliation",
    );
    q.build().execute(&mut *conn).await?;
    Ok(rows.len())
}

/// `_remove_every_row`: the user has nothing left to value on this date.
async fn remove_every_row(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    user_id: i64,
) -> Result<usize, WorkerError> {
    let removed = sqlx::query(
        "DELETE FROM portfolio_nav_daily WHERE portfolio_nav_daily.user_id = $1 \
         AND portfolio_nav_daily.date = $2 RETURNING portfolio_nav_daily.user_id",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    Ok(removed.len())
}

/// `_remove_stale_rows`: this date's rows for portfolios that no longer earn one.
async fn remove_stale_rows(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    user: &UserNav,
) -> Result<usize, WorkerError> {
    let kept: Vec<i64> = user
        .portfolio_rows
        .iter()
        .filter_map(|row| row.portfolio_id)
        .collect();
    let removed = sqlx::query(
        "DELETE FROM portfolio_nav_daily WHERE portfolio_nav_daily.user_id = $1 \
         AND portfolio_nav_daily.date = $2 AND portfolio_nav_daily.portfolio_id IS NOT NULL \
         AND (cardinality($3::bigint[]) = 0 OR portfolio_nav_daily.portfolio_id <> ALL($3)) \
         RETURNING portfolio_nav_daily.portfolio_id",
    )
    .bind(user.user_id)
    .bind(as_of)
    .bind(&kept)
    .fetch_all(&mut *conn)
    .await?;
    Ok(removed.len())
}

/// `_mark_since_grouped`: `first_bought_on = added_on` on undated `NONE` holdings grouped on or
/// before the date; `history_source` stays `NONE` (§5.2).
async fn mark_since_grouped(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    user: &UserNav,
) -> Result<usize, WorkerError> {
    let portfolio_ids: Vec<i64> = user
        .portfolio_rows
        .iter()
        .filter_map(|row| row.portfolio_id)
        .collect();
    if portfolio_ids.is_empty() {
        return Ok(0);
    }
    let marked = sqlx::query(
        "UPDATE portfolio_holding SET first_bought_on = added_on \
         WHERE portfolio_id = ANY($1) AND first_bought_on IS NULL \
         AND history_source = $2 AND added_on <= $3 \
         RETURNING portfolio_id, instrument_id, broker_account_id",
    )
    .bind(&portfolio_ids)
    .bind(HISTORY_SOURCE_NONE)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    Ok(marked.len())
}
