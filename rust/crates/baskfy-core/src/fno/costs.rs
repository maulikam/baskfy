//! Port of `baskfy_core.fno.costs` (`decile-blueprint/packages/core/src/baskfy_core/fno/costs.py`).
//!
//! What an FO round trip costs, itemised, in `Decimal` (`04` §3, §10).
//!
//! **F1**: the options pack's rates on all eight orders through
//! [`crate::options::costs::charges`] — no exercise STT, because nothing is held to expiry —
//! plus a crossing cost per leg per side of `max(slippage_min, slippage_pct of premium)`. Above
//! `f1_max_cost_share` (25 %) of the credit the plan is `REJECTED_COST`.
//!
//! **F2**: `04` §10's futures charges per order — STT 0.05 % on the sale, exchange 0.00173 % a
//! side, stamp 0.002 % on the buy, ₹20 an order, GST 18 % on brokerage + exchange — plus
//! slippage 0.03 % a side; a roll is one sale and one buy, charged the same way.
//!
//! Every component is rounded to the paisa, as a contract note rounds it (house rule 8).
//!
//! **Leg roles.** Python keys the condor's prices by `fno.condor.LegRole`, a `StrEnum`, so a
//! mapping keyed by the role's string value is the same mapping. This file takes [`RolePrices`]
//! (role value → price, the golden encoding) and walks [`LegRole::ALL`] in declaration order with
//! [`sign_of`]'s signs — `fno::condor`'s own, not a copy (DECISIONS-RS R3.4).
//!
//! Goldens: `L1/fno/costs.<function>.case_NNN.json`.

use std::collections::BTreeMap;

use rust_decimal::Decimal;
use serde::{Serialize, Serializer, ser::SerializeStruct};

use super::condor::{LegRole, sign_of};
use super::config::py::{add, mul};
use super::config::{F1Config, FnoError, FnoResult, FutureCostRates, PlanState, py};
use crate::decimal::{Rounding, WideDecimal};
use crate::options::costs::{ChargeBreakdown, CostFill, CostRates, Side, charges, paise};

/// A condor's per-unit prices keyed by `LegRole`'s value (`"LONG_PUT"`, ...).
pub type RolePrices = BTreeMap<String, Decimal>;

/// `[(role.value, sign_of(role)) for role in LegRole]`: the roles in declaration order, with
/// `sign_of` (+1 long, -1 short).
fn leg_roles() -> [(&'static str, i64); 4] {
    LegRole::ALL.map(|role| (role.value(), sign_of(role)))
}

fn price_of(prices: &RolePrices, role: &str) -> FnoResult<Decimal> {
    prices
        .get(role)
        .copied()
        .ok_or_else(|| FnoError::Key(role.to_owned()))
}

/// `Decimal(100)`.
fn hundred() -> Decimal {
    Decimal::ONE_HUNDRED
}

/// Slippage per unit per crossing: `max(min ₹, pct of premium)` (`RESEARCH.md`).
///
/// Ports `baskfy_core.fno.costs.crossing_cost`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn crossing_cost(premium: Decimal, config: &F1Config) -> FnoResult<Decimal> {
    let pct = py::div(mul(premium, config.slippage_pct)?, hundred())?;
    Ok(py::max(config.slippage_min_inr, pct))
}

/// A [`CostFill`] as the golden encodes it: its fields plus the `turnover` property.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fill(pub CostFill);

impl Serialize for Fill {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let turnover = self.0.turnover().map_err(serde::ser::Error::custom)?;
        let mut s = serializer.serialize_struct("CostFill", 4)?;
        s.serialize_field("side", &self.0.side)?;
        s.serialize_field("price", &self.0.price)?;
        s.serialize_field("quantity", &self.0.quantity)?;
        s.serialize_field("turnover", &turnover)?;
        s.end()
    }
}

/// The eight orders: longs bought and shorts sold at entry, the reverse at exit — role by role,
/// each role's opening fill then its closing fill.
///
/// Ports `baskfy_core.fno.costs.condor_fills`.
///
/// # Errors
/// `KeyError` naming the first role a mapping lacks.
pub fn condor_fills(
    entry: &RolePrices,
    exit: &RolePrices,
    quantity: i64,
) -> FnoResult<Vec<CostFill>> {
    let mut fills = Vec::with_capacity(8);
    for (role, sign) in leg_roles() {
        let opening = if sign > 0 { Side::Buy } else { Side::Sell };
        let closing = if opening == Side::Buy {
            Side::Sell
        } else {
            Side::Buy
        };
        fills.push(CostFill {
            side: opening,
            price: price_of(entry, role)?,
            quantity,
        });
        fills.push(CostFill {
            side: closing,
            price: price_of(exit, role)?,
            quantity,
        });
    }
    Ok(fills)
}

/// A [`ChargeBreakdown`] as the golden encodes it: its fields plus the `total` property.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Charges(pub ChargeBreakdown);

impl Serialize for Charges {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let b = &self.0;
        let total = b.total().map_err(serde::ser::Error::custom)?;
        let mut s = serializer.serialize_struct("ChargeBreakdown", 9)?;
        s.serialize_field("orders", &b.orders)?;
        s.serialize_field("brokerage", &b.brokerage)?;
        s.serialize_field("stt", &b.stt)?;
        s.serialize_field("exchange_txn", &b.exchange_txn)?;
        s.serialize_field("sebi", &b.sebi)?;
        s.serialize_field("ipft", &b.ipft)?;
        s.serialize_field("stamp", &b.stamp)?;
        s.serialize_field("gst", &b.gst)?;
        s.serialize_field("total", &total)?;
        s.end()
    }
}

/// A condor's round trip (`04` §3).
///
/// Ports `baskfy_core.fno.costs.CondorCost`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CondorCost {
    /// The eight orders' statutory charges and brokerage.
    pub charges: Charges,
    /// The research crossing cost, ₹.
    pub slippage_inr: Decimal,
    /// Charges plus slippage, ₹.
    pub round_trip_inr: Decimal,
    /// The entry credit, ₹.
    pub credit_inr: Decimal,
    /// Round trip ÷ credit, as a percent; `None` when the credit is not positive.
    pub cost_share_pct: Option<Decimal>,
    /// `REJECTED_COST` above `f1_max_cost_share` (or with no credit), else `None`.
    pub state: Option<PlanState>,
}

/// `04` §3: the round trip in ₹ and as a share of the credit; `REJECTED_COST` above
/// `f1_max_cost_share`. With no `exit`, the exit is priced at the entry prices.
///
/// Ports `baskfy_core.fno.costs.condor_round_trip`.
///
/// # Errors
/// `KeyError` for a missing role; a decimal the default context cannot hold.
pub fn condor_round_trip(
    entry: &RolePrices,
    exit: Option<&RolePrices>,
    quantity: i64,
    rates: &CostRates,
    config: &F1Config,
) -> FnoResult<CondorCost> {
    let closing = exit.unwrap_or(entry);
    let breakdown = charges(&condor_fills(entry, closing, quantity)?, rates)?;
    let mut slip_units = Decimal::ZERO;
    for (role, _) in leg_roles() {
        let leg = add(
            crossing_cost(price_of(entry, role)?, config)?,
            crossing_cost(price_of(closing, role)?, config)?,
        )?;
        slip_units = add(slip_units, leg)?;
    }
    let qty = Decimal::from(quantity);
    let slippage = paise(mul(slip_units, qty)?)?;
    let round_trip = add(breakdown.total()?, slippage)?;
    let mut credit = Decimal::ZERO;
    for (role, sign) in leg_roles() {
        credit = add(credit, mul(Decimal::from(-sign), price_of(entry, role)?)?)?;
    }
    let credit_inr = paise(mul(credit, qty)?)?;
    let charges = Charges(breakdown);
    if credit_inr <= Decimal::ZERO {
        return Ok(CondorCost {
            charges,
            slippage_inr: slippage,
            round_trip_inr: round_trip,
            credit_inr,
            cost_share_pct: None,
            state: Some(PlanState::RejectedCost),
        });
    }
    // `(round_trip / credit * 100).quantize(Decimal("0.01"))`, half even (the context's).
    let share = py::div_wide(round_trip, credit_inr)?
        .mul(&WideDecimal::from(hundred()))
        .quantize(-2, Rounding::HalfEven)
        .map_err(|e| FnoError::Decimal(e.to_string()))?
        .try_to_decimal()
        .map_err(|e| FnoError::Decimal(e.to_string()))?;
    let state = (share > config.max_cost_share_pct).then_some(PlanState::RejectedCost);
    Ok(CondorCost {
        charges,
        slippage_inr: slippage,
        round_trip_inr: round_trip,
        credit_inr,
        cost_share_pct: Some(share),
        state,
    })
}

/// `04` §10's charges over a set of futures orders.
///
/// Ports `baskfy_core.fno.costs.FutureCharges`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FutureCharges {
    /// Executed orders.
    pub orders: i64,
    /// Brokerage.
    pub brokerage: Decimal,
    /// STT on the sale.
    pub stt: Decimal,
    /// Exchange transaction charge.
    pub exchange_txn: Decimal,
    /// Stamp on the buy.
    pub stamp: Decimal,
    /// GST on brokerage and exchange.
    pub gst: Decimal,
    /// Research slippage.
    pub slippage: Decimal,
}

impl FutureCharges {
    /// The sum of every line, slippage included.
    ///
    /// Ports `baskfy_core.fno.costs.FutureCharges.total`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn total(&self) -> FnoResult<Decimal> {
        let mut t = self.brokerage;
        for x in [
            self.stt,
            self.exchange_txn,
            self.stamp,
            self.gst,
            self.slippage,
        ] {
            t = add(t, x)?;
        }
        Ok(t)
    }
}

impl Serialize for FutureCharges {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let total = self.total().map_err(serde::ser::Error::custom)?;
        let mut s = serializer.serialize_struct("FutureCharges", 8)?;
        s.serialize_field("orders", &self.orders)?;
        s.serialize_field("brokerage", &self.brokerage)?;
        s.serialize_field("stt", &self.stt)?;
        s.serialize_field("exchange_txn", &self.exchange_txn)?;
        s.serialize_field("stamp", &self.stamp)?;
        s.serialize_field("gst", &self.gst)?;
        s.serialize_field("slippage", &self.slippage)?;
        s.serialize_field("total", &total)?;
        s.end()
    }
}

/// `paise(x * pct / 100)`: Python's 28-digit quotient, then the paisa (half up).
fn paise_pct(x: Decimal, pct: Decimal) -> FnoResult<Decimal> {
    py::div_quantize(mul(x, pct)?, hundred(), -2, Rounding::HalfUp)
}

/// `04` §10 over a set of futures orders with the given buy and sell turnover.
///
/// Ports `baskfy_core.fno.costs.future_charges`.
///
/// # Errors
/// Decimal overflow.
pub fn future_charges(
    buys_inr: Decimal,
    sells_inr: Decimal,
    orders: i64,
    rates: &FutureCostRates,
) -> FnoResult<FutureCharges> {
    let turnover = add(buys_inr, sells_inr)?;
    let brokerage = paise(mul(rates.brokerage_per_order_inr, Decimal::from(orders))?)?;
    let exchange = paise_pct(turnover, rates.exchange_txn_pct)?;
    Ok(FutureCharges {
        orders,
        brokerage,
        stt: paise_pct(sells_inr, rates.stt_sell_pct)?,
        exchange_txn: exchange,
        stamp: paise_pct(buys_inr, rates.stamp_buy_pct)?,
        gst: paise_pct(add(brokerage, exchange)?, rates.gst_pct)?,
        slippage: paise_pct(turnover, rates.slippage_pct)?,
    })
}

/// A long future bought at `entry` and sold at `exit`: two orders.
///
/// Ports `baskfy_core.fno.costs.future_round_trip`.
///
/// # Errors
/// Decimal overflow.
pub fn future_round_trip(
    entry: Decimal,
    exit: Decimal,
    quantity: i64,
    rates: &FutureCostRates,
) -> FnoResult<FutureCharges> {
    let q = Decimal::from(quantity);
    future_charges(mul(entry, q)?, mul(exit, q)?, 2, rates)
}

/// F2's calendar roll: the held contract sold and the next bought (`04` §10).
///
/// Ports `baskfy_core.fno.costs.future_roll`.
///
/// # Errors
/// Decimal overflow.
pub fn future_roll(
    sell_old: Decimal,
    buy_new: Decimal,
    quantity: i64,
    rates: &FutureCostRates,
) -> FnoResult<FutureCharges> {
    let q = Decimal::from(quantity);
    future_charges(mul(buy_new, q)?, mul(sell_old, q)?, 2, rates)
}
