//! Port of `baskfy_core.twt.sleeve` — the sleeve's own money (`docs/twt/04` §9).
//!
//! This book sizes against its own cash, never the account, and never sells what it did not
//! buy. The arithmetic, stated once:
//!
//! * realised = Σ `pnl_inr` over closed positions (passed in);
//! * cost of open = Σ `entry_avg x quantity_open`; value of open = Σ `mark x quantity_open`;
//! * cash = `capital + realised - cost of open`; equity = `cash + value of open`;
//! * cash available = `max(equity - value of open, 0)`.
//!
//! Sums and products run in [`WideDecimal`] (Python's `Decimal` context), so scale, signed zero
//! and the 28-digit rounding are Python's.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};

/// Ports `MarkSource`: where a position's mark came from (`04` §9.1).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
pub enum MarkSource {
    /// The instrument's published close on the session.
    #[default]
    #[serde(rename = "SESSION_CLOSE")]
    SessionClose,
    /// The name did not print; the last known close.
    #[serde(rename = "LAST_KNOWN_CLOSE")]
    LastKnownClose,
    /// Nothing has printed since the fill.
    #[serde(rename = "ENTRY")]
    Entry,
}

/// Ports `OpenPositionValue`: one open position, as the sleeve's arithmetic needs it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenPositionValue {
    /// The instrument.
    pub instrument_id: i64,
    /// Shares still open.
    pub quantity_open: i64,
    /// Average entry price.
    pub entry_avg: Decimal,
    /// The mark.
    pub mark: Decimal,
    /// Where the mark came from.
    #[serde(default)]
    pub mark_source: MarkSource,
}

fn arith(e: impl std::fmt::Display) -> CoreError {
    CoreError::Arithmetic(e.to_string())
}

fn out(x: &WideDecimal) -> CoreResult<Decimal> {
    x.try_to_decimal().map_err(arith)
}

fn w(d: Decimal) -> WideDecimal {
    WideDecimal::from(d)
}

impl OpenPositionValue {
    fn wide_cost(&self) -> WideDecimal {
        w(self.entry_avg).mul(&WideDecimal::from(self.quantity_open))
    }

    fn wide_value(&self) -> WideDecimal {
        w(self.mark).mul(&WideDecimal::from(self.quantity_open))
    }

    /// Ports `OpenPositionValue.cost`: `entry_avg x quantity_open`.
    ///
    /// # Errors
    /// When the product does not fit a `Decimal`.
    pub fn cost(&self) -> CoreResult<Decimal> {
        out(&self.wide_cost())
    }

    /// Ports `OpenPositionValue.value`: `mark x quantity_open`.
    ///
    /// # Errors
    /// When the product does not fit a `Decimal`.
    pub fn value(&self) -> CoreResult<Decimal> {
        out(&self.wide_value())
    }

    /// Ports `OpenPositionValue.is_stale_mark`: the mark is not this session's close.
    #[must_use]
    pub fn is_stale_mark(&self) -> bool {
        self.mark_source != MarkSource::SessionClose
    }
}

/// Ports `SleeveValue`: everything the plan needs to know about the sleeve's money.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SleeveValue {
    /// The capital Maulik set.
    pub capital_inr: Decimal,
    /// Realised P&L of closed positions.
    pub realised_inr: Decimal,
    /// Σ cost of open positions.
    pub cost_of_open_inr: Decimal,
    /// Σ value of open positions.
    pub value_of_open_inr: Decimal,
}

impl SleeveValue {
    fn wide_cash(&self) -> WideDecimal {
        w(self.capital_inr)
            .add(&w(self.realised_inr))
            .sub(&w(self.cost_of_open_inr))
    }

    fn wide_equity(&self) -> WideDecimal {
        self.wide_cash().add(&w(self.value_of_open_inr))
    }

    /// Ports `SleeveValue.cash_inr`: `capital + realised - cost of open`.
    ///
    /// # Errors
    /// When the result does not fit a `Decimal`.
    pub fn cash_inr(&self) -> CoreResult<Decimal> {
        out(&self.wide_cash())
    }

    /// Ports `SleeveValue.equity_inr`: `cash + value of open`.
    ///
    /// # Errors
    /// When the result does not fit a `Decimal`.
    pub fn equity_inr(&self) -> CoreResult<Decimal> {
        out(&self.wide_equity())
    }

    /// Ports `SleeveValue.cash_available_inr`: `max(equity - value of open, 0)`.
    ///
    /// # Errors
    /// When the result does not fit a `Decimal`.
    pub fn cash_available_inr(&self) -> CoreResult<Decimal> {
        let free = self.wide_equity().sub(&w(self.value_of_open_inr));
        // Python's `max(a, b)` keeps `a` unless `b` is strictly greater.
        if WideDecimal::zero() > free {
            Ok(Decimal::ZERO)
        } else {
            out(&free)
        }
    }

    /// Ports `SleeveValue.open_exposure_inr`: the value of open positions.
    #[must_use]
    pub fn open_exposure_inr(&self) -> Decimal {
        self.value_of_open_inr
    }

    /// Ports `SleeveValue.unrealised_inr`: `value of open - cost of open`.
    ///
    /// # Errors
    /// When the result does not fit a `Decimal`.
    pub fn unrealised_inr(&self) -> CoreResult<Decimal> {
        out(&w(self.value_of_open_inr).sub(&w(self.cost_of_open_inr)))
    }

    /// Ports `SleeveValue.has_capital`: `equity > 0` — a sleeve at ₹0 plans nothing.
    #[must_use]
    pub fn has_capital(&self) -> bool {
        self.wide_equity() > WideDecimal::zero()
    }

    /// Ports `SleeveValue.quantize`: every rupee to the paise (half-even).
    ///
    /// # Errors
    /// When a value cannot be quantized (Python's `InvalidOperation`).
    pub fn quantize(&self) -> CoreResult<Self> {
        let q = |d: Decimal| {
            w(d).quantize(-2, Rounding::HalfEven)
                .and_then(|x| x.try_to_decimal())
                .map_err(arith)
        };
        Ok(Self {
            capital_inr: q(self.capital_inr)?,
            realised_inr: q(self.realised_inr)?,
            cost_of_open_inr: q(self.cost_of_open_inr)?,
            value_of_open_inr: q(self.value_of_open_inr)?,
        })
    }
}

/// Ports `sleeve_value`: the sleeve's money from its own rows and nothing else (`04` §9.1).
///
/// # Errors
/// When a sum does not fit a `Decimal`.
pub fn sleeve_value(
    capital_inr: Decimal,
    realised_inr: Decimal,
    open_positions: &[OpenPositionValue],
) -> CoreResult<SleeveValue> {
    let costs: Vec<WideDecimal> = open_positions
        .iter()
        .map(OpenPositionValue::wide_cost)
        .collect();
    let values: Vec<WideDecimal> = open_positions
        .iter()
        .map(OpenPositionValue::wide_value)
        .collect();
    Ok(SleeveValue {
        capital_inr,
        realised_inr,
        cost_of_open_inr: out(&WideDecimal::sum(&costs))?,
        value_of_open_inr: out(&WideDecimal::sum(&values))?,
    })
}
