//! The volume-breakout sleeve's own book and money, as the evening and the live scan read them.
//! Ports the reading half of `baskfy_api.vbt_sleeve` (`open_positions`, `working_orders`,
//! `realised_pnl`, `marks`, `sleeve_capital`, `load_sleeve`) and the rows `vbt_evening` reads
//! (`live_orders`, `open_positions`, `symbols_for`). The arithmetic is
//! [`baskfy_core::vbt::sleeve`]; this module reads rows (the Rust API has no shared copy, as the
//! `twt_jobs` lane found for its sleeve).
//!
//! **Two rules it keeps** (DECISIONS-VB PACK.4, `02` Track C §5): the sleeve sizes against its own
//! capital plus its own profit, never the account; and its book is `vb_position` and nothing else.

use std::collections::BTreeMap;

use baskfy_core::vbt::orders::OrderState;
use baskfy_core::vbt::sleeve::{OpenPositionValue, SleeveValue, WorkingCommitment, sleeve_value};
use baskfy_db::row::{date as row_date, decimal as row_decimal, opt_date, opt_decimal};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use crate::error::WorkerError;
use crate::steps::sleeves::for_vbt_jobs::core_err;

/// `LIVE_STATES`, sorted (`tuple(sorted(state.value for state in LIVE_STATES))`).
pub(crate) const LIVE_ORDER_STATES: [&str; 4] = ["CONFIRMED", "PARTIAL", "PROPOSED", "SENT"];

/// One `vb_position` row, the columns the evening, the scan and the checks read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PositionRow {
    pub(crate) id: i64,
    pub(crate) instrument_id: i64,
    pub(crate) entry_date: NaiveDate,
    pub(crate) entry_avg: Decimal,
    pub(crate) quantity_open: i64,
    pub(crate) initial_stop: Decimal,
    pub(crate) stop_price: Decimal,
    pub(crate) gtt_id: Option<String>,
    pub(crate) gtt_trigger: Option<Decimal>,
    pub(crate) exit_queued_for: Option<NaiveDate>,
    pub(crate) exit_reason_queued: Option<String>,
    pub(crate) partial_done: bool,
    pub(crate) trail: Option<String>,
}

/// One `vb_order` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct OrderRow {
    pub(crate) id: i64,
    pub(crate) instrument_id: i64,
    pub(crate) signal_date: NaiveDate,
    pub(crate) limit_price: Decimal,
    pub(crate) stop_price: Decimal,
    pub(crate) quantity: i64,
    pub(crate) state: String,
    pub(crate) sessions_worked: i64,
    pub(crate) expires_after_session: Option<NaiveDate>,
    pub(crate) filled_quantity: i64,
}

impl OrderRow {
    /// `OrderState(order.state)`.
    pub(crate) fn order_state(&self) -> Result<OrderState, WorkerError> {
        order_state(&self.state)
    }
}

/// `OrderState(value)`: Python's `ValueError` for a value the enum does not know.
pub(crate) fn order_state(value: &str) -> Result<OrderState, WorkerError> {
    Ok(match value {
        "PROPOSED" => OrderState::Proposed,
        "CONFIRMED" => OrderState::Confirmed,
        "SENT" => OrderState::Sent,
        "PARTIAL" => OrderState::Partial,
        "FILLED" => OrderState::Filled,
        "CANCELLED" => OrderState::Cancelled,
        "EXPIRED" => OrderState::Expired,
        "REJECTED" => OrderState::Rejected,
        other => {
            return Err(WorkerError::python(
                "ValueError",
                format!("'{other}' is not a valid OrderState"),
            ));
        }
    })
}

const POSITION_COLUMNS: &str = "id, instrument_id, entry_date, entry_avg, quantity_open::bigint \
     AS quantity_open, initial_stop, stop_price, gtt_id, gtt_trigger, exit_queued_for, \
     exit_reason_queued, partial_done, trail";

const ORDER_COLUMNS: &str = "id, instrument_id, signal_date, limit_price, stop_price, \
     quantity::bigint AS quantity, state, sessions_worked::bigint AS sessions_worked, \
     expires_after_session, filled_quantity::bigint AS filled_quantity";

fn position_row(row: &sqlx::postgres::PgRow) -> Result<PositionRow, WorkerError> {
    Ok(PositionRow {
        id: row.try_get("id")?,
        instrument_id: row.try_get("instrument_id")?,
        entry_date: row_date(row, "entry_date")?,
        entry_avg: row_decimal(row, "entry_avg")?,
        quantity_open: row.try_get("quantity_open")?,
        initial_stop: row_decimal(row, "initial_stop")?,
        stop_price: row_decimal(row, "stop_price")?,
        gtt_id: row.try_get("gtt_id")?,
        gtt_trigger: opt_decimal(row, "gtt_trigger")?,
        exit_queued_for: opt_date(row, "exit_queued_for")?,
        exit_reason_queued: row.try_get("exit_reason_queued")?,
        partial_done: row.try_get("partial_done")?,
        trail: row.try_get("trail")?,
    })
}

fn order_row(row: &sqlx::postgres::PgRow) -> Result<OrderRow, WorkerError> {
    Ok(OrderRow {
        id: row.try_get("id")?,
        instrument_id: row.try_get("instrument_id")?,
        signal_date: row_date(row, "signal_date")?,
        limit_price: row_decimal(row, "limit_price")?,
        stop_price: row_decimal(row, "stop_price")?,
        quantity: row.try_get("quantity")?,
        state: row.try_get("state")?,
        sessions_worked: row.try_get("sessions_worked")?,
        expires_after_session: opt_date(row, "expires_after_session")?,
        filled_quantity: row.try_get("filled_quantity")?,
    })
}

/// `open_positions`: the sleeve's book — `OPEN` with shares open. Never the account's holdings.
/// (No `ORDER BY`, as Python's query: the heap's order on both sides of a clone.)
pub(crate) async fn open_positions(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<PositionRow>, WorkerError> {
    let rows = sqlx::query(&format!(
        "SELECT {POSITION_COLUMNS} FROM vb_position \
         WHERE user_id = $1 AND state = 'OPEN' AND quantity_open > 0"
    ))
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter().map(position_row).collect()
}

/// `live_orders` / `working_orders`: orders still capable of filling (`LIVE_STATES`).
pub(crate) async fn live_orders(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<OrderRow>, WorkerError> {
    let rows = sqlx::query(&format!(
        "SELECT {ORDER_COLUMNS} FROM vb_order WHERE user_id = $1 AND state = ANY($2)"
    ))
    .bind(user_id)
    .bind(&LIVE_ORDER_STATES[..])
    .fetch_all(&mut *conn)
    .await?;
    rows.iter().map(order_row).collect()
}

/// `symbols_for`: `instrument_id -> symbol`.
pub(crate) async fn symbols_for(
    conn: &mut PgConnection,
    instrument_ids: &[i64],
) -> Result<BTreeMap<i64, String>, WorkerError> {
    if instrument_ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let rows: Vec<(i64, String)> =
        sqlx::query_as("SELECT id, symbol FROM instrument WHERE id = ANY($1)")
            .bind(instrument_ids)
            .fetch_all(&mut *conn)
            .await?;
    Ok(rows.into_iter().collect())
}

/// `realised_pnl(as_of)`: Σ `pnl_inr` over the sleeve's closed positions up to `as_of`.
async fn realised_pnl(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<Decimal, WorkerError> {
    let row = sqlx::query(
        "SELECT COALESCE(SUM(pnl_inr), 0) AS total FROM vb_position \
         WHERE user_id = $1 AND state = 'CLOSED' AND closed_on <= $2",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_one(&mut *conn)
    .await?;
    Ok(row_decimal(&row, "total")?)
}

/// `marks`: the latest published close **on or before** `as_of`, per instrument.
async fn marks(
    conn: &mut PgConnection,
    instrument_ids: &[i64],
    as_of: NaiveDate,
) -> Result<BTreeMap<i64, Decimal>, WorkerError> {
    if instrument_ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let rows = sqlx::query(
        "SELECT o.instrument_id, o.close FROM ohlcv_daily o JOIN ( \
         SELECT instrument_id, max(date) AS date FROM ohlcv_daily \
         WHERE instrument_id = ANY($1) AND date <= $2 GROUP BY instrument_id) latest \
         ON o.instrument_id = latest.instrument_id AND o.date = latest.date",
    )
    .bind(instrument_ids)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut out = BTreeMap::new();
    for row in &rows {
        out.insert(row.try_get("instrument_id")?, row_decimal(row, "close")?);
    }
    Ok(out)
}

/// `sleeve_capital`: `vb_config.sleeve_capital_inr`, or zero without a row.
async fn sleeve_capital(conn: &mut PgConnection, user_id: i64) -> Result<Decimal, WorkerError> {
    let row = sqlx::query("SELECT sleeve_capital_inr FROM vb_config WHERE user_id = $1")
        .bind(user_id)
        .fetch_optional(&mut *conn)
        .await?;
    match row {
        Some(row) => Ok(row_decimal(&row, "sleeve_capital_inr")?),
        None => Ok(Decimal::ZERO),
    }
}

/// `load_sleeve(session, user_id, as_of)`: the sleeve's money as of a session, from its own rows.
/// A position in a name that has not printed since it was bought is marked at its entry.
pub(crate) async fn load_sleeve(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<SleeveValue, WorkerError> {
    let positions = open_positions(conn, user_id).await?;
    let orders = live_orders(conn, user_id).await?;
    let ids: Vec<i64> = positions.iter().map(|p| p.instrument_id).collect();
    let prices = marks(conn, &ids, as_of).await?;
    let capital = sleeve_capital(conn, user_id).await?;
    let realised = realised_pnl(conn, user_id, as_of).await?;
    let open: Vec<OpenPositionValue> = positions
        .iter()
        .map(|row| OpenPositionValue {
            instrument_id: row.instrument_id,
            quantity_open: row.quantity_open,
            entry_avg: row.entry_avg,
            mark: prices
                .get(&row.instrument_id)
                .copied()
                .unwrap_or(row.entry_avg),
        })
        .collect();
    let working: Vec<WorkingCommitment> = orders
        .iter()
        .filter(|row| row.quantity > row.filled_quantity)
        .map(|row| WorkingCommitment {
            instrument_id: row.instrument_id,
            quantity: row.quantity - row.filled_quantity,
            limit_price: row.limit_price,
        })
        .collect();
    sleeve_value(capital, realised, &open, &working).map_err(core_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_live_states_are_pythons_sorted_tuple() {
        let mut sorted = LIVE_ORDER_STATES;
        sorted.sort_unstable();
        assert_eq!(sorted, LIVE_ORDER_STATES);
        for state in LIVE_ORDER_STATES {
            assert!(order_state(state).is_ok_and(OrderState::is_live));
        }
        for state in ["FILLED", "CANCELLED", "EXPIRED", "REJECTED"] {
            assert!(order_state(state).is_ok_and(|s| !s.is_live()));
        }
        assert!(order_state("LOST").is_err());
    }
}
