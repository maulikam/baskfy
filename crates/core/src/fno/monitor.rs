//! Port of `baskfy_core.fno.monitor` (`decile-blueprint/packages/core/src/baskfy_core/fno/monitor.py`).
//!
//! The desk monitor's decisions, pure (`docs/fno/04` §1-§3, §10; `06` FO7). The desk process
//! reads the scan, the live book and the bhavcopy and hands the numbers here; every verdict it
//! acts on is one of these functions.
//!
//! * [`reprice_condor`] — the 09:20 F1 plan on live quotes: the scan's strikes priced on live
//!   mids, the liquidity refusals by name, sizing (never rounded up) and the round trip as a
//!   share of the credit. The first failure in the order structure → liquidity → size → cost is
//!   the state, and every reason found is kept.
//! * [`reprice_future`] — the 09:20 F2 plan on the live ask.
//! * [`f1_close_cost`] / [`f1_pnl_inr`] / [`f2_pnl_inr`] — the marks.
//! * [`f2_trail_step`] — the evening trail: the stop moved **never lower**.
//! * [`f2_action`] — `NAKED_FUTURE`, then `LATE_EXIT` past the roll date, then
//!   [`super::exits::f2_exit`].
//!
//! Goldens: `L1/fno/monitor.<function>.case_NNN.json`.

use std::collections::BTreeMap;

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::Serialize;

use super::calendar::PyDateTime;
use super::condor::{
    CondorStrikes, LegQuote, LegRole, RoleMap, cost_to_close, liquidity_refusals, num,
    price_structure, role_prices,
};
use super::config::py::{add, mul, sub};
use super::config::{CommonConfig, FnoCeilings, FnoConfig, FnoResult, PlanState, py};
use super::costs::{CondorCost, FutureCharges, condor_round_trip, future_round_trip};
use super::exits::{ExitDecision, ExitReason, f2_exit, f2_initial_stop, f2_trail, gtt_trigger};
use super::sizing::{FoSizing, size};
use crate::options::costs::CostRates;
use crate::options::gating::Mode;

/// The reason a condor's round trip is refused, in words (shared with the scan).
pub(crate) fn cost_reason(cost: &CondorCost, max_cost_share_pct: Decimal) -> String {
    let share = cost
        .cost_share_pct
        .map_or_else(|| "None".to_owned(), py::str);
    format!(
        "round trip ₹{} is {share} % of the credit ₹{} > {} %",
        py::str(cost.round_trip_inr),
        py::str(cost.credit_inr),
        py::str(max_cost_share_pct)
    )
}

/// The live plan's numbers. `state` is `None` for a clean plan, or the first rejection.
///
/// Ports `baskfy_core.fno.monitor.CondorReprice`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CondorReprice {
    /// The first rejection, or `None`.
    pub state: Option<PlanState>,
    /// Every refusal found, in words.
    pub reasons: Vec<String>,
    /// The live mids of the legs that have one.
    pub mids: RoleMap,
    /// The credit per unit.
    pub credit: Option<Decimal>,
    /// The max loss per unit.
    pub max_loss_per_unit: Option<Decimal>,
    /// The sizing.
    pub sizing: Option<FoSizing>,
    /// The round trip.
    pub cost: Option<CondorCost>,
}

/// `04` §2-§3 on live quotes for the scan's strikes.
///
/// Ports `baskfy_core.fno.monitor.reprice_condor`.
///
/// # Errors
/// A decimal the default context cannot hold; `ValueError` from sizing.
#[allow(clippy::too_many_arguments)] // every input 04 §2-§3 names, as in Python
pub fn reprice_condor(
    strikes: &CondorStrikes,
    quotes: &BTreeMap<LegRole, LegQuote>,
    lot_size: i64,
    capital_inr: Decimal,
    mode: Mode,
    config: &FnoConfig,
    rates: &CostRates,
    ceilings: &FnoCeilings,
) -> FnoResult<CondorReprice> {
    let liquidity = liquidity_refusals(strikes, quotes, &config.f1)?;
    let mut mids = RoleMap::new();
    for role in LegRole::ALL {
        if let Some(quote) = quotes.get(&role)
            && let Some(mid) = quote.mid()?
        {
            mids.insert(role, mid);
        }
    }
    if mids.len() < LegRole::ALL.len() {
        return Ok(CondorReprice {
            state: Some(PlanState::RejectedLiquidity),
            reasons: liquidity,
            mids,
            credit: None,
            max_loss_per_unit: None,
            sizing: None,
            cost: None,
        });
    }
    let priced = price_structure(strikes, &mids)?;
    let mut states: Vec<PlanState> = Vec::new();
    let mut reasons: Vec<String> = priced.reasons.clone();
    reasons.extend(liquidity.iter().cloned());
    if let Some(state) = priced.state {
        states.push(state);
    }
    if !liquidity.is_empty() {
        states.push(PlanState::RejectedLiquidity);
    }
    let mut sizing = None;
    let mut cost = None;
    if priced.state.is_none() {
        let sized = size(
            mode,
            capital_inr,
            config.f1.risk_per_trade_pct,
            priced.max_loss_per_unit,
            Some(lot_size),
            &config.common,
            ceilings,
        )?;
        if let Some(state) = sized.state {
            states.push(state);
            reasons.push(sized.message.clone());
        }
        let costed = condor_round_trip(
            &role_prices(&mids),
            None,
            sized.lots.max(1) * lot_size,
            rates,
            &config.f1,
        )?;
        if let Some(state) = costed.state {
            states.push(state);
            reasons.push(cost_reason(&costed, config.f1.max_cost_share_pct));
        }
        sizing = Some(sized);
        cost = Some(costed);
    }
    Ok(CondorReprice {
        state: states.first().copied(),
        reasons,
        mids,
        credit: Some(priced.credit),
        max_loss_per_unit: Some(priced.max_loss_per_unit),
        sizing,
        cost,
    })
}

/// The cost to close per unit on `marks`, or `None` when a leg has no mark — a missing leg is
/// not a zero.
///
/// Ports `baskfy_core.fno.monitor.f1_close_cost`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn f1_close_cost(strikes: &CondorStrikes, marks: &RoleMap) -> FnoResult<Option<Decimal>> {
    if LegRole::ALL.iter().any(|r| !marks.contains_key(r)) {
        return Ok(None);
    }
    cost_to_close(strikes, marks).map(Some)
}

/// `(credit - cost to close) x quantity`: a condor earns what it does not buy back.
///
/// Ports `baskfy_core.fno.monitor.f1_pnl_inr`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn f1_pnl_inr(entry_credit: Decimal, close_cost: Decimal, quantity: i64) -> FnoResult<Decimal> {
    num::money(mul(
        sub(entry_credit, close_cost)?,
        Decimal::from(quantity),
    )?)
}

/// The live F2 plan's numbers.
///
/// Ports `baskfy_core.fno.monitor.FutureReprice`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FutureReprice {
    /// The sizing's refusal, or the quote's or structure's.
    pub state: Option<PlanState>,
    /// The plan in words.
    pub reasons: Vec<String>,
    /// The live ask.
    pub entry: Option<Decimal>,
    /// The ATR stop.
    pub stop: Option<Decimal>,
    /// The GTT trigger.
    pub trigger: Option<Decimal>,
    /// `entry - stop`.
    pub risk_per_unit: Option<Decimal>,
    /// The sizing.
    pub sizing: Option<FoSizing>,
    /// The round trip.
    pub cost: Option<FutureCharges>,
}

fn future_refused(
    state: PlanState,
    reason: String,
    entry: Option<Decimal>,
    stop: Option<Decimal>,
    trigger: Option<Decimal>,
) -> FutureReprice {
    FutureReprice {
        state: Some(state),
        reasons: vec![reason],
        entry,
        stop,
        trigger,
        risk_per_unit: None,
        sizing: None,
        cost: None,
    }
}

/// `04` §10 at the live ask (what a buy pays). The stop is `entry - stop_atr x ATR14` at the
/// signal close's ATR; the GTT triggers at the tighter of it and `stop_from_vol`.
///
/// Ports `baskfy_core.fno.monitor.reprice_future`.
///
/// # Errors
/// A decimal the default context cannot hold.
#[allow(clippy::too_many_arguments)] // every input 04 §10 names, as in Python
pub fn reprice_future(
    bid: Option<Decimal>,
    ask: Option<Decimal>,
    atr: Decimal,
    ann_vol: f64,
    lot_size: i64,
    capital_inr: Decimal,
    mode: Mode,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> FnoResult<FutureReprice> {
    let (Some(bid), Some(ask)) = (bid, ask) else {
        return Ok(no_quote());
    };
    if bid <= Decimal::ZERO || ask <= Decimal::ZERO || ask < bid {
        return Ok(no_quote());
    }
    if atr <= Decimal::ZERO {
        return Ok(future_refused(
            PlanState::RejectedStructure,
            format!(
                "ATR14 {} is not positive; no stop can be placed",
                py::str(atr)
            ),
            Some(ask),
            None,
            None,
        ));
    }
    let entry = ask;
    let stop = num::money(f2_initial_stop(entry, atr, &config.f2)?)?;
    let trigger = num::money(gtt_trigger(stop, entry, ann_vol, &config.stop_vol)?)?;
    let risk = sub(entry, stop)?;
    if stop <= Decimal::ZERO || risk <= Decimal::ZERO {
        return Ok(future_refused(
            PlanState::RejectedStructure,
            format!(
                "stop {} is not a price below the entry {}",
                py::str(stop),
                py::str(entry)
            ),
            Some(entry),
            Some(stop),
            Some(trigger),
        ));
    }
    let sizing = size(
        mode,
        capital_inr,
        config.f2.risk_per_trade_pct,
        risk,
        Some(lot_size),
        &config.common,
        ceilings,
    )?;
    let cost = future_round_trip(
        entry,
        entry,
        sizing.lots.max(1) * lot_size,
        &config.future_costs,
    )?;
    Ok(FutureReprice {
        state: sizing.state,
        reasons: vec![sizing.message.clone()],
        entry: Some(entry),
        stop: Some(stop),
        trigger: Some(trigger),
        risk_per_unit: Some(risk),
        sizing: Some(sizing),
        cost: Some(cost),
    })
}

fn no_quote() -> FutureReprice {
    future_refused(
        PlanState::RejectedLiquidity,
        "the future has no two-sided quote".to_owned(),
        None,
        None,
        None,
    )
}

/// `(mark - entry) x quantity`, to the paisa.
///
/// Ports `baskfy_core.fno.monitor.f2_pnl_inr`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn f2_pnl_inr(entry: Decimal, mark: Decimal, quantity: i64) -> FnoResult<Decimal> {
    num::money(mul(sub(mark, entry)?, Decimal::from(quantity))?)
}

/// The evening's stop, its GTT trigger and the highest close; `moved` when the trigger rose.
///
/// Ports `baskfy_core.fno.monitor.TrailStep`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TrailStep {
    /// The stop.
    pub stop: Decimal,
    /// The GTT trigger.
    pub trigger: Decimal,
    /// The highest close since entry.
    pub highest_close: Decimal,
    /// The trigger rose (the GTT is modified).
    pub moved: bool,
}

/// Each close: `stop = max(stop, highest close - mult x ATR at entry)`, the trigger the tighter
/// of that and `stop_from_vol(entry)`. **Neither is ever lowered.**
///
/// Ports `baskfy_core.fno.monitor.f2_trail_step`.
///
/// # Errors
/// A decimal the default context cannot hold.
#[allow(clippy::too_many_arguments)] // every input 04 §10 names, as in Python
pub fn f2_trail_step(
    stop: Decimal,
    trigger: Decimal,
    highest_close: Decimal,
    close: Decimal,
    entry: Decimal,
    atr_at_entry: Decimal,
    ann_vol: f64,
    config: &FnoConfig,
) -> FnoResult<TrailStep> {
    let highest = py::max(highest_close, close);
    let new_stop = num::money(f2_trail(stop, highest, atr_at_entry, &config.f2)?)?;
    let candidate = num::money(gtt_trigger(new_stop, entry, ann_vol, &config.stop_vol)?)?;
    let new_trigger = py::max(trigger, candidate);
    Ok(TrailStep {
        stop: py::max(stop, new_stop),
        trigger: new_trigger,
        highest_close: highest,
        moved: new_trigger > trigger,
    })
}

/// The F2 action due at `now`: no resting stop first (`NAKED_FUTURE`), a roll date passed while
/// the desk was down (`LATE_EXIT`), then the stop, the time exit and the roll. With no price
/// the stop cannot be tested, but the calendar exits still can.
///
/// Ports `baskfy_core.fno.monitor.f2_action`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn f2_action(
    now: PyDateTime,
    price: Option<Decimal>,
    trigger: Decimal,
    time_exit_date: Option<NaiveDate>,
    roll_date: Option<NaiveDate>,
    naked: bool,
    common: &CommonConfig,
) -> FnoResult<Option<ExitDecision>> {
    if naked {
        return Ok(Some(ExitDecision {
            reason: ExitReason::NakedFuture,
            message: "the future has no resting GTT stop; exited at this check".to_owned(),
        }));
    }
    if let Some(roll) = roll_date
        && now.date() > roll
    {
        return Ok(Some(ExitDecision {
            reason: ExitReason::LateExit,
            message: format!(
                "roll date {} passed while the desk was down",
                roll.format("%Y-%m-%d")
            ),
        }));
    }
    let price = match price {
        Some(p) => p,
        None => add(trigger, Decimal::ONE)?,
    };
    Ok(f2_exit(
        now,
        price,
        trigger,
        time_exit_date,
        roll_date,
        common,
    ))
}
