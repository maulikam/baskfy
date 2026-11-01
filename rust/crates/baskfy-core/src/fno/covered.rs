//! Port of `baskfy_core.fno.covered` (`decile-blueprint/packages/core/src/baskfy_core/fno/covered.py`).
//!
//! The covered-overnight predicate (`02` §2.1, `04` §2).
//!
//! **A short option is never open without a long option of the same underlying, type and
//! expiry, further from the money, in at least the same quantity.** For calls "further" is a
//! higher strike, for puts a lower one.
//!
//! The decision per `(underlying, expiry, option_type)`: net the quantities per strike, then walk
//! from the furthest-out strike toward the money (calls: descending, puts: ascending) keeping the
//! running sum; a short strike is covered only by longs already passed, so the book is covered
//! iff the running sum never goes negative.
//!
//! The book after a step is the broker's positions **plus** the plan's already-filled legs
//! **plus** the order itself; `broker_positions` is the broker's book **net of this plan's own
//! fills** (DECISIONS-FO FO1), so a fill is counted once.
//!
//! Python nets in dicts keyed by `Decimal`, which hash by value and keep the **first** key
//! inserted (`20800` and `20800.0` are one strike, printed as the first); the port keeps that.
//!
//! Goldens: `L1/fno/covered.<function>.case_NNN.json`.

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use super::config::py;
use super::vol::OptionType;

/// A signed option quantity in units: positive long, negative short.
///
/// Ports `baskfy_core.fno.covered.OptionPosition`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OptionPosition {
    /// The underlying.
    pub underlying: String,
    /// The contract's expiry.
    pub expiry: NaiveDate,
    /// `CE` or `PE`.
    pub option_type: OptionType,
    /// The strike.
    pub strike: Decimal,
    /// Signed units.
    pub quantity: i64,
}

fn type_value(kind: OptionType) -> &'static str {
    match kind {
        OptionType::Ce => "CE",
        OptionType::Pe => "PE",
    }
}

/// One `(underlying, expiry, option_type)` side: strikes in first-seen order with their net.
type Side = ((String, NaiveDate, OptionType), Vec<(Decimal, i64)>);

fn net_by_side<'a>(book: impl IntoIterator<Item = &'a OptionPosition>) -> Vec<Side> {
    let mut sides: Vec<Side> = Vec::new();
    for row in book {
        let key = (row.underlying.clone(), row.expiry, row.option_type);
        let i = match sides.iter().position(|(k, _)| *k == key) {
            Some(i) => i,
            None => {
                sides.push((key, Vec::new()));
                sides.len() - 1
            }
        };
        let strikes = &mut sides[i].1;
        match strikes.iter_mut().find(|(k, _)| *k == row.strike) {
            Some((_, q)) => *q += row.quantity,
            None => strikes.push((row.strike, row.quantity)),
        }
    }
    sides
}

/// Every uncovered short, in words. Empty means the book is covered.
///
/// Ports `baskfy_core.fno.covered.uncovered`.
#[must_use]
pub fn uncovered<'a>(book: impl IntoIterator<Item = &'a OptionPosition>) -> Vec<String> {
    let mut sides = net_by_side(book);
    sides.sort_by(|((u1, e1, k1), _), ((u2, e2, k2), _)| {
        (u1, e1, type_value(*k1)).cmp(&(u2, e2, type_value(*k2)))
    });
    let mut found = Vec::new();
    for ((underlying, expiry, kind), mut by_strike) in sides {
        // Strikes are unique by value, so the sort direction has no ties to keep stable.
        if kind == OptionType::Ce {
            by_strike.sort_by_key(|(k, _)| std::cmp::Reverse(*k));
        } else {
            by_strike.sort_by_key(|(k, _)| *k);
        }
        let mut running: i64 = 0;
        for (strike, net) in by_strike {
            running += net;
            if net < 0 && running < 0 {
                found.push(format!(
                    "{underlying} {} {} {}: short {} with only {} long further out",
                    expiry.format("%Y-%m-%d"),
                    py::str(strike),
                    type_value(kind),
                    -net,
                    running - net
                ));
                running = 0;
            }
        }
    }
    found
}

/// `02` §2.1 on the book after an order.
///
/// Ports `baskfy_core.fno.covered.is_covered`.
#[must_use]
pub fn is_covered<'a>(book_after: impl IntoIterator<Item = &'a OptionPosition>) -> bool {
    uncovered(book_after).is_empty()
}

/// Broker book (net of this plan's fills) + this plan's filled legs + the order.
///
/// Ports `baskfy_core.fno.covered.book_after`.
#[must_use]
pub fn book_after(
    broker_positions: &[OptionPosition],
    plan_filled: &[OptionPosition],
    step: &OptionPosition,
) -> Vec<OptionPosition> {
    broker_positions
        .iter()
        .chain(plan_filled)
        .chain(std::iter::once(step))
        .cloned()
        .collect()
}

/// The broker's book with this plan's filled legs taken out (FO1.2's caller contract, FO6). A
/// fill the broker has not yet reflected leaves a negative (short) residue — the stricter, safe
/// direction.
///
/// Ports `baskfy_core.fno.covered.net_of_plan`.
#[must_use]
pub fn net_of_plan(
    broker_raw: &[OptionPosition],
    plan_filled: &[OptionPosition],
) -> Vec<OptionPosition> {
    let mut net: Vec<OptionPosition> = Vec::new();
    let rows = broker_raw
        .iter()
        .map(|r| (r, 1_i64))
        .chain(plan_filled.iter().map(|r| (r, -1_i64)));
    for (row, sign) in rows {
        let same = |p: &&mut OptionPosition| {
            p.underlying == row.underlying
                && p.expiry == row.expiry
                && p.option_type == row.option_type
                && p.strike == row.strike
        };
        match net.iter_mut().find(same) {
            Some(p) => p.quantity += sign * row.quantity,
            None => net.push(OptionPosition {
                quantity: sign * row.quantity,
                ..row.clone()
            }),
        }
    }
    net.retain(|p| p.quantity != 0);
    net
}

/// The guard's answer for one order of a plan.
///
/// Ports `baskfy_core.fno.covered.StepVerdict`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StepVerdict {
    /// The book after the order is covered.
    pub covered: bool,
    /// Every uncovered short, in words.
    pub reasons: Vec<String>,
}

/// The guard's answer for one order of a plan (`04` §2). A refused entry step abandons the
/// entry (`ABANDONED_PARTIAL`); the legs already filled are longs and close at once.
///
/// Ports `baskfy_core.fno.covered.assess_step`.
#[must_use]
pub fn assess_step(
    step: &OptionPosition,
    broker_positions: &[OptionPosition],
    plan_filled: &[OptionPosition],
) -> StepVerdict {
    let reasons = uncovered(&book_after(broker_positions, plan_filled, step));
    StepVerdict {
        covered: reasons.is_empty(),
        reasons,
    }
}
