//! Port of `baskfy_core.options.exits`: one open position's exit, evaluated per tick.

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::bars::{Bar, closed, five_minute_bars, ist, minute};
use crate::options::chain::as_f64;
use crate::options::condor::{self, CondorTick};
use crate::options::config::{OptionsConfig, Sleeve};
use crate::options::directional::{self, LongTick};
use crate::options::execution::{LegRole, feed_lost};
use crate::options::expiry_setups::{self, SpreadTick};
use crate::options::num::{self, ist_offset};
use crate::options::structures::{Direction, Structure};

/// The reason on a `HARD_EXIT` the feed forced.
pub const FEED_LOST: &str = "FEED_LOST";
/// The reason on a rule-driven exit.
pub const RULE: &str = "RULE";

/// One filled leg.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenLeg {
    /// Its role.
    pub role: LegRole,
    /// Strike.
    pub strike: Decimal,
    /// Units.
    pub quantity: i64,
    /// Average fill price.
    pub fill_price: Decimal,
    /// The instrument token.
    pub instrument_token: i64,
}

/// What the exit rules need of an open position and its plan.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenPosition {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The structure.
    pub structure: Structure,
    /// Its legs.
    pub legs: Vec<OpenLeg>,
    /// Premium per unit from fills.
    pub entry_points: Decimal,
    /// When it opened.
    pub opened_at: DateTime<FixedOffset>,
    /// The risk budget.
    pub risk_budget_inr: Decimal,
    /// Direction (O2/O3).
    #[serde(default)]
    pub direction: Option<Direction>,
    /// The range high invalidation reads.
    #[serde(default)]
    pub range_high: Option<Decimal>,
    /// The range low invalidation reads.
    #[serde(default)]
    pub range_low: Option<Decimal>,
    /// O3-B's half gap.
    #[serde(default)]
    pub half_gap: Option<Decimal>,
    /// O3's width.
    #[serde(default)]
    pub width_points: Option<Decimal>,
}

impl OpenPosition {
    /// The first leg's units.
    pub fn quantity(&self) -> i64 {
        self.legs.first().map_or(0, |l| l.quantity)
    }

    /// The leg in `role`.
    pub fn leg(&self, role: LegRole) -> Option<&OpenLeg> {
        self.legs.iter().find(|l| l.role == role)
    }
}

/// A leg's latest quote and when it was taken.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegMark {
    /// Bid.
    pub bid: Option<Decimal>,
    /// Ask.
    pub ask: Option<Decimal>,
    /// When.
    pub at: DateTime<FixedOffset>,
}

/// What the desk knows of NIFTY 50 at `now`.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct IndexState {
    /// Spot.
    pub spot: Option<Decimal>,
    /// The last tick.
    pub last_tick_at: Option<DateTime<FixedOffset>>,
    /// The bars so far.
    #[serde(default)]
    pub bars: Vec<Bar>,
}

/// The exit to send, why, and the mark it was decided on.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExitVerdict {
    /// The exit code.
    pub code: String,
    /// `FEED_LOST` or `RULE`.
    pub reason: String,
    /// The mark, per unit.
    pub value_points: Option<Decimal>,
    /// The marked loss.
    pub marked_loss_inr: Option<Decimal>,
    /// The mark was stale.
    pub stale: bool,
}

/// The conservative close of a whole position.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Mark {
    /// Value per unit; `None` when some leg has no two-sided quote.
    pub value_points: Option<Decimal>,
    /// Some quote is older than `stale_quote_seconds`.
    pub stale: bool,
}

/// The position's close value per unit — longs sold at the bid, shorts bought at the ask.
pub fn mark(
    position: &OpenPosition,
    marks: &BTreeMap<LegRole, LegMark>,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
) -> CoreResult<Mark> {
    let mut total = Decimal::ZERO;
    let mut stale = false;
    for lg in &position.legs {
        let Some(quote) = marks.get(&lg.role) else {
            return Ok(Mark {
                value_points: None,
                stale: true,
            });
        };
        let (Some(bid), Some(ask)) = (quote.bid, quote.ask) else {
            return Ok(Mark {
                value_points: None,
                stale: true,
            });
        };
        let age = (ist(now) - ist(&quote.at))
            .num_microseconds()
            .unwrap_or(i64::MAX);
        if as_f64(age) / 1_000_000.0 > as_f64(options.chain.stale_quote_seconds) {
            stale = true;
        }
        let term = if position.structure == Structure::IronCondor {
            if lg.role.is_long() {
                num::neg(bid)?
            } else {
                ask
            }
        } else if lg.role.is_long() {
            bid
        } else {
            num::neg(ask)?
        };
        total = num::add(total, term)?;
    }
    Ok(Mark {
        value_points: Some(total),
        stale,
    })
}

/// `04` §9.2's marked loss in rupees (positive = a loss) at close value `value` per unit.
pub fn marked_loss_inr(position: &OpenPosition, value: Decimal) -> CoreResult<Decimal> {
    let per_unit = if position.structure == Structure::IronCondor {
        num::sub(value, position.entry_points)?
    } else {
        num::sub(position.entry_points, value)?
    };
    num::muli(per_unit, position.quantity())
}

/// The close of the last completed `width`-minute bar that ended after `after`.
pub fn latest_five_minute_close(
    bars: &[Bar],
    now: &DateTime<FixedOffset>,
    after: &DateTime<FixedOffset>,
    session_open: NaiveTime,
    width: i64,
) -> CoreResult<Option<Decimal>> {
    let seen = closed(bars, now);
    let floor = minute(after);
    Ok(five_minute_bars(&seen, session_open, width)?
        .into_iter()
        .rfind(|b| b.complete() && b.close_time > floor)
        .and_then(|b| b.close))
}

/// The last closed one-minute bar that started at or after `after`'s minute.
pub fn latest_bar_after(
    bars: &[Bar],
    now: &DateTime<FixedOffset>,
    after: &DateTime<FixedOffset>,
) -> Option<Bar> {
    let floor = minute(after);
    closed(bars, now)
        .into_iter()
        .rfind(|b| minute(&b.ts) >= floor)
}

/// One tick's exit for `position`, or `None` to hold.
pub fn evaluate(
    position: &OpenPosition,
    marks: &BTreeMap<LegRole, LegMark>,
    index: &IndexState,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
    manual: bool,
) -> CoreResult<Option<ExitVerdict>> {
    let tz = ist_offset();
    let now_ist = now.with_timezone(&tz);
    let last = index.last_tick_at.map(|t| t.with_timezone(&tz));
    let opened = position.opened_at.with_timezone(&tz);
    if feed_lost(
        position.sleeve,
        &now_ist,
        last.as_ref(),
        Some(&opened),
        options.chain.stale_index_seconds,
        &options.execution,
    ) {
        return Ok(Some(ExitVerdict {
            code: "HARD_EXIT".to_owned(),
            reason: FEED_LOST.to_owned(),
            value_points: None,
            marked_loss_inr: None,
            stale: true,
        }));
    }
    let current = mark(position, marks, now, options)?;
    let Some(value) = current.value_points else {
        if ist(now).time() >= options.hard_exit_time(position.sleeve) {
            return Ok(Some(ExitVerdict {
                code: "HARD_EXIT".to_owned(),
                reason: RULE.to_owned(),
                value_points: None,
                marked_loss_inr: None,
                stale: true,
            }));
        }
        return Ok(None);
    };
    let loss = marked_loss_inr(position, value)?;
    let code = decide(
        position,
        value,
        loss,
        current.stale,
        index,
        now,
        options,
        manual,
    )?;
    Ok(code.map(|code| ExitVerdict {
        code,
        reason: RULE.to_owned(),
        value_points: Some(value),
        marked_loss_inr: Some(loss),
        stale: current.stale,
    }))
}

fn invalid(message: &str) -> CoreError {
    CoreError::InvalidInput(message.to_owned())
}

#[allow(clippy::too_many_arguments)]
fn decide(
    position: &OpenPosition,
    value: Decimal,
    loss: Decimal,
    stale: bool,
    index: &IndexState,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
    manual: bool,
) -> CoreResult<Option<String>> {
    let session_open = options.calendar.market_open;
    if position.structure == Structure::IronCondor {
        if !matches!(position.sleeve, Sleeve::O1m | Sleeve::O1w) {
            return Err(CoreError::InvalidInput(format!(
                "{} does not trade a condor",
                position.sleeve.value()
            )));
        }
        let (Some(short_call), Some(short_put)) = (
            position.leg(LegRole::ShortCall),
            position.leg(LegRole::ShortPut),
        ) else {
            return Err(invalid("a condor exit needs both shorts"));
        };
        let cfg = if position.sleeve == Sleeve::O1m {
            &options.condor_monthly
        } else {
            &options.condor_weekly
        };
        let tick = CondorTick {
            entry_credit: position.entry_points,
            cost_now: value,
            spot: index.spot,
            short_call_strike: short_call.strike,
            short_put_strike: short_put.strike,
            now: *now,
            stale,
            marked_loss_inr: loss,
            risk_budget_inr: position.risk_budget_inr,
            manual,
        };
        let verdict = condor::exit_decision(&tick, cfg, &options.risk)?;
        return Ok(verdict.map(|v| condor_code(v).to_owned()));
    }
    let Some(direction) = position.direction else {
        return Err(invalid("a directional position has a direction"));
    };
    if position.structure == Structure::LongOption {
        let (Some(high), Some(low)) = (position.range_high, position.range_low) else {
            return Err(invalid("an O2 position carries its opening range"));
        };
        let five = latest_five_minute_close(
            &index.bars,
            now,
            &position.opened_at,
            session_open,
            options.directional.bar_minutes,
        )?;
        let tick = LongTick {
            entry: position.entry_points,
            bid: value,
            entered_at: position.opened_at,
            now: *now,
            is_invalidated: directional::invalidated(five, direction, high, low),
            stale,
            marked_loss_inr: loss,
            risk_budget_inr: position.risk_budget_inr,
            manual,
        };
        let verdict = directional::exit_decision(&tick, &options.directional, options)?;
        return Ok(verdict.map(|v| v.value().to_owned()));
    }
    let cfg3 = &options.expiry_setups;
    let is_invalidated = if position.sleeve == Sleeve::O3b {
        let Some(half) = position.half_gap else {
            return Err(invalid("an O3-B position carries its half-gap"));
        };
        let bar = latest_bar_after(&index.bars, now, &position.opened_at);
        expiry_setups::gap_hold_invalidated(bar.as_ref(), direction, half)
    } else {
        let (Some(high), Some(low)) = (position.range_high, position.range_low) else {
            return Err(invalid("an O3-A position carries its morning range"));
        };
        let five = latest_five_minute_close(
            &index.bars,
            now,
            &position.opened_at,
            session_open,
            cfg3.bar_minutes,
        )?;
        expiry_setups::range_break_invalidated(five, direction, high, low)
    };
    let width = match position.width_points {
        Some(w) if !w.is_zero() => w,
        _ => cfg3.width_points,
    };
    let tick = SpreadTick {
        value,
        entry_debit: position.entry_points,
        width,
        is_invalidated,
        now: *now,
        stale,
        marked_loss_inr: loss,
        risk_budget_inr: position.risk_budget_inr,
        manual,
    };
    let verdict = expiry_setups::exit_decision(&tick, cfg3, options)?;
    Ok(verdict.map(|v| v.value().to_owned()))
}

fn condor_code(code: condor::CondorExit) -> &'static str {
    match code {
        condor::CondorExit::HardExit => "HARD_EXIT",
        condor::CondorExit::StrikeTouch => "STRIKE_TOUCH",
        condor::CondorExit::Stop => "STOP",
        condor::CondorExit::Profit => "PROFIT",
        condor::CondorExit::Manual => "MANUAL",
    }
}
