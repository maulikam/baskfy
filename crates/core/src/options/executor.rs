//! Port of `baskfy_core.options.executor`: sending a plan's legs over a caller's venue, pure (`04` §8.2-§8.4).

use std::collections::{BTreeMap, BTreeSet};

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::config::{ExecutionConfig, Side, Sleeve};
use crate::options::execution::{
    Attempt, EntryStep, LegRole, advance_entry, entry_sequence, never_naked, next_attempt,
    next_exit_leg,
};
use crate::options::num;

/// The most attempts an order ever gets (§8.2).
pub const MAX_ATTEMPTS: i64 = 3;

/// Where a run ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Outcome {
    /// Every leg filled.
    Open,
    /// A leg fell short; what filled was closed.
    AbandonedEntry,
    /// Everything closed.
    Flat,
    /// A leg's attempts ran out; the caller retries.
    PartialExit,
}

impl Outcome {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            Outcome::Open => "OPEN",
            Outcome::AbandonedEntry => "ABANDONED_ENTRY",
            Outcome::Flat => "FLAT",
            Outcome::PartialExit => "PARTIAL_EXIT",
        }
    }
}

/// A leg's quote at the moment of an attempt.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Book {
    /// Bid.
    pub bid: Decimal,
    /// Ask.
    pub ask: Decimal,
}

/// What one attempt filled.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fill {
    /// Units filled.
    pub quantity: i64,
    /// Average price; `None` when nothing filled.
    pub avg_price: Option<Decimal>,
}

/// Where orders go: `quote` reads the book, `send` places one attempt and returns what filled.
pub trait Venue {
    /// The leg's book now.
    fn quote(&mut self, role: LegRole) -> CoreResult<Book>;
    /// One attempt for `quantity` units.
    fn send(
        &mut self,
        role: LegRole,
        attempt: &Attempt,
        quantity: i64,
        closing: bool,
    ) -> CoreResult<Fill>;
}

/// One step of the procedure.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Event {
    /// The leg.
    pub role: LegRole,
    /// Buy or sell.
    pub side: Side,
    /// 1, 2 or 3.
    pub attempt: i64,
    /// The limit.
    pub price: Decimal,
    /// Units asked for.
    pub requested: i64,
    /// Units filled.
    pub filled: i64,
    /// A close.
    pub closing: bool,
    /// The marketable third attempt.
    pub marketable: bool,
}

/// Python's `executor.Result`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecResult {
    /// Where it ended.
    pub outcome: Outcome,
    /// Open units per leg.
    pub position: BTreeMap<LegRole, i64>,
    /// Every fill per leg.
    pub fills: BTreeMap<LegRole, Vec<Fill>>,
    /// Every attempt.
    pub events: Vec<Event>,
}

impl ExecResult {
    fn new(outcome: Outcome, position: BTreeMap<LegRole, i64>) -> Self {
        Self {
            outcome,
            position,
            fills: BTreeMap::new(),
            events: Vec::new(),
        }
    }

    /// The leg's average fill price over its filled attempts.
    pub fn avg_price(&self, role: LegRole) -> CoreResult<Option<Decimal>> {
        let done: Vec<(i64, Decimal)> = self
            .fills
            .get(&role)
            .into_iter()
            .flatten()
            .filter(|f| f.quantity != 0)
            .filter_map(|f| f.avg_price.map(|p| (f.quantity, p)))
            .collect();
        let total: i64 = done.iter().map(|(q, _)| q).sum();
        if total == 0 {
            return Ok(None);
        }
        let cash = done
            .iter()
            .map(|(q, p)| num::muli(*p, *q))
            .collect::<CoreResult<Vec<_>>>()?;
        Ok(Some(num::div(num::sum(cash)?, Decimal::from(total))?))
    }
}

fn reduces_risk(role: LegRole, sleeve: Sleeve) -> bool {
    !role.is_long() || sleeve == Sleeve::O2
}

/// The rules every attempt is priced by.
#[derive(Debug, Clone, Copy)]
pub struct Rules<'a> {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The price step.
    pub tick: Decimal,
    /// `04` §8.
    pub config: &'a ExecutionConfig,
}

fn send(
    venue: &mut dyn Venue,
    role: LegRole,
    quantity: i64,
    closing: bool,
    rules: Rules<'_>,
    result: &mut ExecResult,
) -> CoreResult<i64> {
    let side = if closing {
        role.exit_side()
    } else {
        role.entry_side()
    };
    let reduces = closing && reduces_risk(role, rules.sleeve);
    let mut remaining = quantity;
    for number in 1..=MAX_ATTEMPTS {
        let book = venue.quote(role)?;
        let Some(attempt) = next_attempt(
            number,
            side,
            book.bid,
            book.ask,
            rules.tick,
            reduces,
            rules.config,
        )?
        else {
            break;
        };
        let mut worst = result.position.clone();
        *worst.entry(role).or_insert(0) += if closing { -remaining } else { remaining };
        if !never_naked(&worst) {
            return Err(CoreError::InvalidInput(format!(
                "{} would leave the book short more than long",
                role.value()
            )));
        }
        let fill = venue.send(role, &attempt, remaining, closing)?;
        if !(0..=remaining).contains(&fill.quantity) {
            return Err(CoreError::InvalidInput(format!(
                "{}: filled {} of {remaining}",
                role.value(),
                fill.quantity
            )));
        }
        *result.position.entry(role).or_insert(0) += if closing {
            -fill.quantity
        } else {
            fill.quantity
        };
        let filled = fill.quantity;
        result.fills.entry(role).or_default().push(fill);
        result.events.push(Event {
            role,
            side,
            attempt: number,
            price: attempt.price,
            requested: remaining,
            filled,
            closing,
            marketable: attempt.marketable,
        });
        remaining -= filled;
        if remaining == 0 {
            break;
        }
    }
    Ok(quantity - remaining)
}

/// §8.3's entry: longs first; the first leg short of `quantity` abandons the entry and every
/// filled leg is closed in exit order.
pub fn run_entry(
    venue: &mut dyn Venue,
    roles: &[LegRole],
    quantity: i64,
    rules: Rules<'_>,
) -> CoreResult<ExecResult> {
    let sequence = entry_sequence(roles)?;
    let mut result = ExecResult::new(Outcome::Open, BTreeMap::new());
    let mut outcomes: Vec<i64> = Vec::new();
    loop {
        let action = advance_entry(&sequence, quantity, &outcomes)?;
        match action.step {
            EntryStep::Open => return Ok(result),
            EntryStep::Abandon => {
                let position = result.position.clone();
                let mut closed = run_exit_into(venue, position, rules, result)?;
                closed.outcome = Outcome::AbandonedEntry;
                return Ok(closed);
            }
            EntryStep::Send => {
                let role = action.leg.ok_or_else(|| {
                    CoreError::InvalidInput("an entry step with no leg".to_owned())
                })?;
                outcomes.push(send(venue, role, quantity, false, rules, &mut result)?);
            }
        }
    }
}

/// §8.3's exit: shorts first, a protecting long never before its short. `FLAT` when everything
/// closed, `PARTIAL_EXIT` when a leg's attempts ran out.
pub fn run_exit(
    venue: &mut dyn Venue,
    position: BTreeMap<LegRole, i64>,
    rules: Rules<'_>,
) -> CoreResult<ExecResult> {
    let out = ExecResult::new(Outcome::Flat, position.clone());
    run_exit_into(venue, position, rules, out)
}

/// [`run_exit`] recording into an existing result (Python's `result=`).
pub fn run_exit_into(
    venue: &mut dyn Venue,
    position: BTreeMap<LegRole, i64>,
    rules: Rules<'_>,
    mut out: ExecResult,
) -> CoreResult<ExecResult> {
    out.position = position;
    let mut tried: BTreeSet<LegRole> = BTreeSet::new();
    loop {
        let open: Vec<(LegRole, i64)> = out.position.iter().map(|(r, q)| (*r, *q)).collect();
        let Some(role) = next_exit_leg(&open) else {
            out.outcome = Outcome::Flat;
            return Ok(out);
        };
        if !tried.insert(role) {
            out.outcome = Outcome::PartialExit;
            return Ok(out);
        }
        let qty = out.position.get(&role).copied().unwrap_or(0);
        send(venue, role, qty, true, rules, &mut out)?;
    }
}
