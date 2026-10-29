//! Port of `baskfy_core.vbt.sleeve` (`decile-blueprint/packages/core/src/baskfy_core/vbt/sleeve.py`).
//!
//! The sleeve's own money (`docs/vbt/06` VB5, DECISIONS-VB PACK.4). **This book sizes against
//! its own cash and never against the account.** The arithmetic is stated once here:
//!
//! | | |
//! |---|---|
//! | realised | Σ `pnl_inr` over the sleeve's closed positions (passed in) |
//! | cost of open | Σ `entry_avg x quantity_open` |
//! | value of open | Σ `mark x quantity_open` |
//! | cash | `capital + realised - cost of open` |
//! | committed | Σ `limit_price x quantity` over the working orders |
//! | cash available | `max(cash - committed, 0)` |
//! | equity | `cash + value of open` |
//! | open exposure | `value of open + committed` |
//!
//! Every product, sum and difference is Python's `Decimal` arithmetic, scale and zero sign
//! included (R1.2): `rust_decimal` drops a zero's scale, Python keeps it (`0.000`).
//!
//! Goldens: `sleeve.sleeve_value` (with every derived property and `quantize()`).

use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::costs::quantize;

/// `value` at exactly `scale` places when it has fewer (exact: trailing zeros only). Python's
/// exact sums and products carry the ideal exponent; `rust_decimal` returns an operand
/// unchanged when the other is zero or one, at its own scale.
fn widen(value: Decimal, scale: u32) -> CoreResult<Decimal> {
    if value.scale() >= scale || scale > 28 {
        return Ok(value);
    }
    let factor = 10_i128
        .checked_pow(scale - value.scale())
        .and_then(|f| value.mantissa().checked_mul(f))
        .ok_or_else(|| CoreError::Arithmetic(format!("cannot hold {value} at {scale} places")))?;
    let mut out = Decimal::try_from_i128_with_scale(factor, scale).map_err(|e| {
        CoreError::Arithmetic(format!("cannot hold {value} at {scale} places: {e}"))
    })?;
    out.set_sign_negative(value.is_sign_negative());
    Ok(out)
}

/// Python's `a * b`: the product's exponent is the sum of the operands' (`Decimal("1.00") * 1`
/// is `1.00`, `Decimal("123.456") * 0` is `0.000`), and a zero carries the XOR of the signs.
fn mul(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    let mut out = a
        .checked_mul(b)
        .ok_or_else(|| CoreError::Arithmetic(format!("{a} * {b} overflowed")))?;
    if out.is_zero() {
        out.set_sign_negative(a.is_sign_negative() != b.is_sign_negative());
    }
    widen(out, a.scale() + b.scale())
}

/// Python's `a + b`: the sum's exponent is the smaller of the operands' (`Decimal("200.0") +
/// Decimal("0.00")` is `200.00`), and a zero sum is negative only when both operands are.
fn add(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    let mut out = a
        .checked_add(b)
        .ok_or_else(|| CoreError::Arithmetic(format!("{a} + {b} overflowed")))?;
    if out.is_zero() {
        out.set_sign_negative(a.is_sign_negative() && b.is_sign_negative());
    }
    widen(out, a.scale().max(b.scale()))
}

/// Python's `a - b`, i.e. `a + (-b)`, with [`add`]'s zero rules.
fn sub(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    add(a, -b)
}

/// Ports `baskfy_core.vbt.sleeve.OpenPositionValue` — one open position, as the sleeve's
/// arithmetic needs it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenPositionValue {
    /// The instrument.
    pub instrument_id: i64,
    /// Shares still open.
    pub quantity_open: i64,
    /// The average entry price.
    pub entry_avg: Decimal,
    /// The latest published close on or before the session, or the entry when there is none.
    pub mark: Decimal,
}

impl OpenPositionValue {
    /// Ports the `cost` property: `entry_avg x quantity_open`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn cost(&self) -> CoreResult<Decimal> {
        mul(self.entry_avg, Decimal::from(self.quantity_open))
    }

    /// Ports the `value` property: `mark x quantity_open`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn value(&self) -> CoreResult<Decimal> {
        mul(self.mark, Decimal::from(self.quantity_open))
    }
}

/// Ports `baskfy_core.vbt.sleeve.WorkingCommitment` — one resting limit, and the money it has
/// spoken for but not yet spent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkingCommitment {
    /// The instrument.
    pub instrument_id: i64,
    /// Shares bid for.
    pub quantity: i64,
    /// The limit.
    pub limit_price: Decimal,
}

impl WorkingCommitment {
    /// Ports the `value` property: `limit_price x quantity`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn value(&self) -> CoreResult<Decimal> {
        mul(self.limit_price, Decimal::from(self.quantity))
    }
}

/// Ports `baskfy_core.vbt.sleeve.SleeveValue` — everything the plan needs to know about the
/// sleeve's money, computed once.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SleeveValue {
    /// The capital the sleeve was given.
    pub capital_inr: Decimal,
    /// Realised P&L.
    pub realised_inr: Decimal,
    /// Σ cost of the open positions.
    pub cost_of_open_inr: Decimal,
    /// Σ value of the open positions at their marks.
    pub value_of_open_inr: Decimal,
    /// Σ value of the working orders.
    pub committed_inr: Decimal,
}

impl SleeveValue {
    /// Ports `cash_inr`: `capital + realised - cost of open`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn cash_inr(&self) -> CoreResult<Decimal> {
        sub(
            add(self.capital_inr, self.realised_inr)?,
            self.cost_of_open_inr,
        )
    }

    /// Ports `cash_available_inr`: cash a **new** line may spend, never negative.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn cash_available_inr(&self) -> CoreResult<Decimal> {
        let free = sub(self.cash_inr()?, self.committed_inr)?;
        // `max(free, Decimal(0))` keeps `free` unless zero is strictly greater.
        Ok(if Decimal::ZERO > free {
            Decimal::ZERO
        } else {
            free
        })
    }

    /// Ports `equity_inr`: `cash + value of open` (the slot is a tenth of this).
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn equity_inr(&self) -> CoreResult<Decimal> {
        add(self.cash_inr()?, self.value_of_open_inr)
    }

    /// Ports `open_exposure_inr`: `value of open + committed`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn open_exposure_inr(&self) -> CoreResult<Decimal> {
        add(self.value_of_open_inr, self.committed_inr)
    }

    /// Ports `unrealised_inr`: `value of open - cost of open`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn unrealised_inr(&self) -> CoreResult<Decimal> {
        sub(self.value_of_open_inr, self.cost_of_open_inr)
    }

    /// Ports `quantize()`: every rupee to the paisa, half-even (house rule 8).
    ///
    /// # Errors
    /// A value that cannot be held at two places.
    pub fn quantize(&self) -> CoreResult<Self> {
        let paise = |d: Decimal| quantize(d, 2, RoundingStrategy::MidpointNearestEven);
        Ok(Self {
            capital_inr: paise(self.capital_inr)?,
            realised_inr: paise(self.realised_inr)?,
            cost_of_open_inr: paise(self.cost_of_open_inr)?,
            value_of_open_inr: paise(self.value_of_open_inr)?,
            committed_inr: paise(self.committed_inr)?,
        })
    }
}

/// `sum(values, Decimal(0))`, added left to right.
fn py_sum(values: impl IntoIterator<Item = CoreResult<Decimal>>) -> CoreResult<Decimal> {
    let mut total = Decimal::ZERO;
    for v in values {
        total = add(total, v?)?;
    }
    Ok(total)
}

/// Ports `baskfy_core.vbt.sleeve.sleeve_value`: the sleeve's money, from its own rows.
///
/// # Errors
/// Decimal overflow.
pub fn sleeve_value(
    capital_inr: Decimal,
    realised_inr: Decimal,
    open_positions: &[OpenPositionValue],
    working_orders: &[WorkingCommitment],
) -> CoreResult<SleeveValue> {
    Ok(SleeveValue {
        capital_inr,
        realised_inr,
        cost_of_open_inr: py_sum(open_positions.iter().map(OpenPositionValue::cost))?,
        value_of_open_inr: py_sum(open_positions.iter().map(OpenPositionValue::value))?,
        committed_inr: py_sum(working_orders.iter().map(WorkingCommitment::value))?,
    })
}
