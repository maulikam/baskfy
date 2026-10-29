//! Port of `baskfy_core.vbt.orders` (`decile-blueprint/packages/core/src/baskfy_core/vbt/orders.py`).
//!
//! The working limit order (`docs/vbt/04` §7). STRATEGY §3: *"Do not chase the open."* A VBT-1
//! entry is a limit at the signal bar's close, working for three sessions, then cancelled.
//! **Three is the parameter with a cliff.** Sessions, not days: everything counts positions in
//! a [`SessionCalendar`] and never subtracts two dates. Pure: the caller says which session it is.
//!
//! Goldens: `orders.expires_after`, `orders.sessions_since`, `orders.is_expired`,
//! `orders.advance_session`, `orders.expire_orders`, `orders.fill_if_touched`,
//! `orders.apply_fill`.

use std::cmp::Ordering;
use std::str::FromStr;

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::backtest::helpers::WideDecimal;
use crate::error::{CoreError, CoreResult};
use crate::vbt::calendar::SessionCalendar;
use crate::vbt::config::EntryConfig;

/// Ports `baskfy_core.vbt.orders.OrderState` — where a working order is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
pub enum OrderState {
    /// A plan line nobody has confirmed yet.
    #[default]
    #[serde(rename = "PROPOSED")]
    Proposed,
    /// Confirmed, not yet sent.
    #[serde(rename = "CONFIRMED")]
    Confirmed,
    /// Held by the broker.
    #[serde(rename = "SENT")]
    Sent,
    /// Partly filled, still working.
    #[serde(rename = "PARTIAL")]
    Partial,
    /// Nothing left.
    #[serde(rename = "FILLED")]
    Filled,
    /// Cancelled at the broker.
    #[serde(rename = "CANCELLED")]
    Cancelled,
    /// Expired before it reached the broker.
    #[serde(rename = "EXPIRED")]
    Expired,
    /// Rejected.
    #[serde(rename = "REJECTED")]
    Rejected,
}

impl OrderState {
    /// Ports `LIVE_STATES` membership: still capable of filling, still holds a slot.
    #[must_use]
    pub fn is_live(self) -> bool {
        matches!(
            self,
            Self::Proposed | Self::Confirmed | Self::Sent | Self::Partial
        )
    }

    /// Ports `TERMINAL_STATES` membership: nothing further happens to it.
    #[must_use]
    pub fn is_terminal(self) -> bool {
        !self.is_live()
    }
}

/// Ports `baskfy_core.vbt.orders.CancelReason`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CancelReason {
    /// The evening sweep after the window closed.
    #[serde(rename = "EXPIRY_SWEEP")]
    ExpirySweep,
    /// A person cancelled it.
    #[serde(rename = "MANUAL")]
    Manual,
    /// The breadth gate shut.
    #[serde(rename = "GATE_SHUT")]
    GateShut,
    /// The slot went to another order.
    #[serde(rename = "SLOT_TAKEN")]
    SlotTaken,
}

/// Ports `baskfy_core.vbt.orders.WorkingOrder` — a limit bid resting for a bounded number of
/// sessions.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkingOrder {
    /// The instrument.
    pub instrument_id: i64,
    /// The signal session.
    pub signal_date: NaiveDate,
    /// The bid.
    pub limit_price: Decimal,
    /// The stop the position will carry.
    pub stop_price: Decimal,
    /// Shares bid for.
    pub quantity: i64,
    /// Where the order is.
    #[serde(default)]
    pub state: OrderState,
    /// Sessions it has actually been live for (advanced once per published session).
    #[serde(default)]
    pub sessions_worked: i64,
    /// Shares filled so far.
    #[serde(default)]
    pub filled_quantity: i64,
    /// Why it was cancelled or expired.
    #[serde(default)]
    pub cancel_reason: Option<CancelReason>,
}

impl WorkingOrder {
    /// Ports the `is_live` property.
    #[must_use]
    pub fn is_live(&self) -> bool {
        self.state.is_live()
    }

    /// Ports the `remaining` property: `max(quantity - filled_quantity, 0)`.
    #[must_use]
    pub fn remaining(&self) -> i64 {
        self.quantity.saturating_sub(self.filled_quantity).max(0)
    }
}

/// Ports `baskfy_core.vbt.orders.Fill` — what one session did to one working order.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fill {
    /// The order as it stood before the fill.
    pub order: WorkingOrder,
    /// Shares filled.
    pub quantity: i64,
    /// The fill price.
    pub price: Decimal,
    /// The session it filled on.
    pub session: NaiveDate,
}

/// Ports `baskfy_core.vbt.orders.expires_after`: the **last** session the order may fill on,
/// `valid_sessions` after the signal; `None` when the calendar does not reach that far.
///
/// # Errors
/// [`SessionCalendar::advance`]'s `IndexError` for a negative window reaching past the start.
pub fn expires_after(
    signal_date: NaiveDate,
    calendar: &SessionCalendar,
    config: &EntryConfig,
) -> CoreResult<Option<NaiveDate>> {
    calendar.advance(signal_date, config.valid_sessions)
}

/// Ports `baskfy_core.vbt.orders.sessions_since`: calendar sessions between the signal and
/// `session` (a holiday counts for none; `0` when either is not a session).
#[must_use]
pub fn sessions_since(
    signal_date: NaiveDate,
    session: NaiveDate,
    calendar: &SessionCalendar,
) -> i64 {
    calendar.sessions_between(signal_date, session)
}

/// Ports `baskfy_core.vbt.orders.is_expired`: has the order worked out its window by the
/// **close** of `session`? `sessions_since >= valid_sessions` (cancelled at the close of t+3).
#[must_use]
pub fn is_expired(
    order: &WorkingOrder,
    session: NaiveDate,
    calendar: &SessionCalendar,
    config: &EntryConfig,
) -> bool {
    sessions_since(order.signal_date, session, calendar) >= config.valid_sessions
}

/// Ports `baskfy_core.vbt.orders.advance_session`: one published session has passed with this
/// order live.
#[must_use]
pub fn advance_session(order: &WorkingOrder) -> WorkingOrder {
    WorkingOrder {
        sessions_worked: order.sessions_worked.saturating_add(1),
        ..order.clone()
    }
}

/// Ports `baskfy_core.vbt.orders.expire_orders`: `(still working, expired this session)`.
///
/// Terminal orders are in neither list. An order that never reached the broker (`PROPOSED`,
/// `CONFIRMED`) becomes `EXPIRED`; one the broker holds becomes `CANCELLED`, a partial for its
/// remainder.
#[must_use]
pub fn expire_orders(
    orders: &[WorkingOrder],
    session: NaiveDate,
    calendar: &SessionCalendar,
    config: &EntryConfig,
    reason: CancelReason,
) -> (Vec<WorkingOrder>, Vec<WorkingOrder>) {
    let (mut live, mut done) = (Vec::new(), Vec::new());
    for order in orders {
        if !order.is_live() {
            continue;
        }
        if is_expired(order, session, calendar, config) {
            let state = if matches!(order.state, OrderState::Proposed | OrderState::Confirmed) {
                OrderState::Expired
            } else {
                OrderState::Cancelled
            };
            done.push(WorkingOrder {
                state,
                cancel_reason: Some(reason),
                ..order.clone()
            });
        } else {
            live.push(order.clone());
        }
    }
    (live, done)
}

/// `low > order.limit_price * (1 - Decimal(str(fill_through_pct)) / 100)` in Python's decimal
/// context, `Decimal('Infinity')` included.
fn low_above_threshold(low: Decimal, limit: Decimal, fill_through_pct: f64) -> CoreResult<bool> {
    if fill_through_pct.is_nan() {
        return Err(CoreError::InvalidInput(
            "NaN is not a usable config number".to_owned(),
        ));
    }
    let limit_w = WideDecimal::from(limit);
    if fill_through_pct.is_infinite() {
        // 1 - (+inf)/100 = -inf; 1 - (-inf)/100 = +inf; then limit x that.
        if limit.is_zero() {
            return Err(CoreError::Arithmetic(
                "[<class 'decimal.InvalidOperation'>] 0 * Infinity".to_owned(),
            ));
        }
        let factor_negative = fill_through_pct > 0.0;
        let threshold_negative = factor_negative != limit.is_sign_negative();
        // low > -inf is always true; low > +inf never.
        return Ok(threshold_negative);
    }
    let through = WideDecimal::from_str(&format!("{fill_through_pct}"))
        .map_err(|e| CoreError::InvalidInput(e.to_string()))?;
    let share = through
        .div(&WideDecimal::from(100_i64))
        .map_err(|e| CoreError::Arithmetic(e.to_string()))?;
    let factor = WideDecimal::from(1_i64).sub(&share);
    let threshold = limit_w.mul(&factor);
    Ok(WideDecimal::from(low).cmp(&threshold) == Ordering::Greater)
}

/// Ports `baskfy_core.vbt.orders.fill_if_touched`: `04` §7.3's fill model.
///
/// No fill without an open, high and low, on a non-positive open, or on a locked bar
/// (`open == high == low`). The order fills when the low trades at or through the limit
/// (less `fill_through_pct`), at **the better of the open and the limit** (Python's
/// `min(open, limit)`: the open on a tie), for its whole remainder.
///
/// # Errors
/// A NaN `fill_through_pct`, or `0 x Infinity` (Python's `InvalidOperation`).
pub fn fill_if_touched(
    order: &WorkingOrder,
    session: NaiveDate,
    open_price: Option<Decimal>,
    high: Option<Decimal>,
    low: Option<Decimal>,
    config: &EntryConfig,
) -> CoreResult<Option<Fill>> {
    let (Some(open), Some(low), Some(high)) = (open_price, low, high) else {
        return Ok(None);
    };
    if open <= Decimal::ZERO {
        return Ok(None);
    }
    if open == high && high == low {
        return Ok(None);
    }
    if low_above_threshold(low, order.limit_price, config.fill_through_pct)? {
        return Ok(None);
    }
    let price = if order.limit_price < open {
        order.limit_price
    } else {
        open
    };
    let quantity = order.remaining();
    if quantity <= 0 {
        return Ok(None);
    }
    Ok(Some(Fill {
        order: order.clone(),
        quantity,
        price,
        session,
    }))
}

/// Ports `baskfy_core.vbt.orders.apply_fill`: the order after a fill — `FILLED` when nothing is
/// left, `PARTIAL` while some is.
#[must_use]
pub fn apply_fill(order: &WorkingOrder, fill: &Fill) -> WorkingOrder {
    let filled = order.filled_quantity.saturating_add(fill.quantity);
    let state = if filled >= order.quantity {
        OrderState::Filled
    } else {
        OrderState::Partial
    };
    WorkingOrder {
        filled_quantity: filled,
        state,
        ..order.clone()
    }
}
