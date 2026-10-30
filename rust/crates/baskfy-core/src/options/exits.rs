//! Port of `baskfy_core.options.exits` (`decile-blueprint/packages/core/src/baskfy_core/options/exits.py`).
//!
//! One open position's exit, evaluated per tick (`06` OP9: "`exits.evaluate` per tick"). Every
//! sleeve's rule is its own pure function — [`condor::exit_decision`] (condor §7),
//! [`directional::exit_decision`] (`04` §4.5), [`expiry_setups::exit_decision`] (§5.3) — and the
//! feed rule is [`feed_lost`] (§8.5). This module turns an open position, its legs' latest
//! quotes and the index's state into their inputs and returns the verdict. It decides; it never
//! sends (law 2).
//!
//! * **The mark** is the conservative close — longs at the bid, shorts at the ask. A leg with
//!   no two-sided quote, or one older than `chain.stale_quote_seconds`, makes the mark *stale*:
//!   the profit rules (`PROFIT`, `TARGET`) are dropped, stops and invalidations still run on the
//!   last known mark, and with no mark at all only the clock can close.
//! * **The loss** is the marked loss in ₹ against the entry from fills (§9.2).
//! * **Feed loss** is checked first and wins: `HARD_EXIT` with the reason `FEED_LOST`.
//!
//! **Times** are IST wall-clock ([`NaiveDateTime`]); Python's `ist()` is applied by the caller
//! ([`crate::options::bars::ist`]).

use std::collections::BTreeMap;

use chrono::{NaiveDateTime, NaiveTime, TimeZone};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::bars::{Bar, closed, five_minute_bars, ist_offset, minute};
use crate::options::condor::{self, CondorExit, CondorTick};
use crate::options::config::OptionsConfig;
use crate::options::costs::{add, mul, sub};
use crate::options::directional::{self, LongTick};
use crate::options::execution::{LegRole, feed_lost};
use crate::options::expiry_setups::{self, SpreadTick};
use crate::options::gating::Sleeve;
use crate::options::structures::{Direction, Structure};

/// The reason on a `HARD_EXIT` the feed forced (`04` §8.5).
pub const FEED_LOST: &str = "FEED_LOST";
/// The reason on a rule-driven exit: the rule's own code is the verdict.
pub const RULE: &str = "RULE";

/// One filled leg: its role, strike, units and the average fill price.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenLeg {
    /// The leg's role.
    pub role: LegRole,
    /// Strike.
    pub strike: Decimal,
    /// Units.
    pub quantity: i64,
    /// Average fill price.
    pub fill_price: Decimal,
    /// Kite instrument token.
    pub instrument_token: i64,
}

/// What the exit rules need of an `op_position` and its plan.
///
/// `entry_points` is the structure's premium per unit from fills: the condor's credit, the
/// long's entry, the spread's debit. The invalidation fields are the plan's own (`or_high` /
/// `or_low` for O2, the morning range for O3-A, `half_gap` for O3-B).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenPosition {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The structure.
    pub structure: Structure,
    /// The legs, in `op_leg.seq` order.
    pub legs: Vec<OpenLeg>,
    /// Premium per unit from fills.
    pub entry_points: Decimal,
    /// When it opened, IST wall-clock.
    pub opened_at: NaiveDateTime,
    /// R in ₹.
    pub risk_budget_inr: Decimal,
    /// `UP`/`DOWN` (O2, O3).
    pub direction: Option<Direction>,
    /// The invalidation range's high.
    pub range_high: Option<Decimal>,
    /// The invalidation range's low.
    pub range_low: Option<Decimal>,
    /// O3-B's half gap.
    pub half_gap: Option<Decimal>,
    /// The spread's width.
    pub width_points: Option<Decimal>,
}

impl OpenPosition {
    /// `quantity`: the first leg's units (`0` with no legs).
    #[must_use]
    pub fn quantity(&self) -> i64 {
        self.legs.first().map_or(0, |l| l.quantity)
    }

    /// `leg(role)`: the first leg with that role.
    #[must_use]
    pub fn leg(&self, role: LegRole) -> Option<&OpenLeg> {
        self.legs.iter().find(|l| l.role == role)
    }
}

/// A leg's latest quote and when it was taken (IST wall-clock).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegMark {
    /// Best bid.
    pub bid: Option<Decimal>,
    /// Best ask.
    pub ask: Option<Decimal>,
    /// When.
    pub at: NaiveDateTime,
}

/// What the desk knows of NIFTY 50 at `now`: the last tick, the spot, and the bars so far.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct IndexState {
    /// The spot; `None` before the first index tick.
    pub spot: Option<Decimal>,
    /// The last index tick's time.
    pub last_tick_at: Option<NaiveDateTime>,
    /// The day's minute bars.
    pub bars: Vec<Bar>,
}

/// The exit to send, why, and the mark it was decided on (`None` when there was none).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExitVerdict {
    /// The rule's code (`PROFIT`, `HARD_EXIT`, …).
    pub code: String,
    /// `RULE` or `FEED_LOST`.
    pub reason: String,
    /// The mark it was decided on.
    pub value_points: Option<Decimal>,
    /// The marked loss, ₹.
    pub marked_loss_inr: Option<Decimal>,
    /// Whether the mark was stale.
    pub stale: bool,
}

impl ExitVerdict {
    /// `ExitVerdict("HARD_EXIT", FEED_LOST, None, None, True)`.
    #[must_use]
    pub fn feed_lost() -> Self {
        Self {
            code: "HARD_EXIT".to_owned(),
            reason: FEED_LOST.to_owned(),
            value_points: None,
            marked_loss_inr: None,
            stale: true,
        }
    }
}

/// The conservative close of a whole position: its value per unit and whether it is stale.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Mark {
    /// Value per unit; `None` when some leg has never been quoted two-sided.
    pub value_points: Option<Decimal>,
    /// Any leg's quote is stale.
    pub stale: bool,
}

/// The legs' latest quotes by role (Python's `Mapping[LegRole, LegMark]`).
pub type Marks = BTreeMap<LegRole, LegMark>;

fn older_than(a: NaiveDateTime, b: NaiveDateTime, seconds: i64) -> bool {
    (a - b).num_microseconds().map_or(a > b, |us| {
        i128::from(us) > i128::from(seconds) * 1_000_000
    })
}

/// `mark(position, marks, now, options)`: the position's close value per unit — longs sold at
/// the bid, shorts bought at the ask. For a condor it is the cost to close (`D`); for a long or
/// a debit spread what closing receives (`V`).
///
/// # Errors
/// Decimal overflow.
pub fn mark(
    position: &OpenPosition,
    marks: &Marks,
    now: NaiveDateTime,
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
        if older_than(now, quote.at, options.chain.stale_quote_seconds) {
            stale = true;
        }
        total = if position.structure == Structure::IronCondor {
            if lg.role.is_long() {
                sub(total, bid)?
            } else {
                add(total, ask)?
            }
        } else if lg.role.is_long() {
            add(total, bid)?
        } else {
            sub(total, ask)?
        };
    }
    Ok(Mark {
        value_points: Some(total),
        stale,
    })
}

/// `marked_loss_inr(position, value)`: `04` §9.2's marked loss in ₹ (positive = a loss).
///
/// # Errors
/// Decimal overflow.
pub fn marked_loss_inr(position: &OpenPosition, value: Decimal) -> CoreResult<Decimal> {
    let per_unit = if position.structure == Structure::IronCondor {
        sub(value, position.entry_points)?
    } else {
        sub(position.entry_points, value)?
    };
    mul(per_unit, Decimal::from(position.quantity()))
}

/// `latest_five_minute_close(bars, now, after, session_open, width)`: the close of the last
/// *completed* `width`-minute bar that ended after `after`.
///
/// # Errors
/// `width <= 0`.
pub fn latest_five_minute_close(
    bars: &[Bar],
    now: NaiveDateTime,
    after: NaiveDateTime,
    session_open: NaiveTime,
    width: i64,
) -> CoreResult<Option<Decimal>> {
    let seen = closed(bars, now);
    let after_minute = minute(after);
    let done: Vec<_> = five_minute_bars(&seen, session_open, width)?
        .into_iter()
        .filter(|b| b.complete() && b.close_time > after_minute)
        .collect();
    Ok(done.last().and_then(|b| b.close))
}

/// `latest_bar_after(bars, now, after)`: the last closed one-minute bar that started at or
/// after `after`'s minute.
#[must_use]
pub fn latest_bar_after(bars: &[Bar], now: NaiveDateTime, after: NaiveDateTime) -> Option<Bar> {
    let after_minute = minute(after);
    closed(bars, now)
        .into_iter()
        .rev()
        .find(|b| minute(b.ts) >= after_minute)
}

const fn condor_code(code: CondorExit) -> &'static str {
    match code {
        CondorExit::HardExit => "HARD_EXIT",
        CondorExit::StrikeTouch => "STRIKE_TOUCH",
        CondorExit::Stop => "STOP",
        CondorExit::Profit => "PROFIT",
        CondorExit::Manual => "MANUAL",
    }
}

/// `evaluate(position, marks, index, now, options=, manual=)`: one tick's exit for `position`,
/// or `None` to hold.
///
/// # Errors
/// Python's `ValueError`s: a condor outside O1 or without both shorts, a directional position
/// without its direction or its invalidation numbers; decimal overflow.
pub fn evaluate(
    position: &OpenPosition,
    marks: &Marks,
    index: &IndexState,
    now: NaiveDateTime,
    options: &OptionsConfig,
    manual: bool,
) -> CoreResult<Option<ExitVerdict>> {
    if feed_lost(
        position.sleeve,
        now,
        index.last_tick_at,
        Some(position.opened_at),
        options.chain.stale_index_seconds,
        &options.execution,
    ) {
        return Ok(Some(ExitVerdict::feed_lost()));
    }
    let current = mark(position, marks, now, options)?;
    let Some(value) = current.value_points else {
        // No mark at all: only the clock can close it; a stop cannot be judged.
        if now.time() >= options.hard_exit_time(position.sleeve) {
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
    let code = decide(position, value, loss, current.stale, index, now, options, manual)?;
    Ok(code.map(|code| ExitVerdict {
        code: code.to_owned(),
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
    now: NaiveDateTime,
    options: &OptionsConfig,
    manual: bool,
) -> CoreResult<Option<&'static str>> {
    let session_open = options.calendar.market_open;
    if position.structure == Structure::IronCondor {
        let cfg = match position.sleeve {
            Sleeve::O1m => &options.condor_monthly,
            Sleeve::O1w => &options.condor_weekly,
            other => {
                return Err(CoreError::InvalidInput(format!(
                    "{} does not trade a condor",
                    sleeve_value(other)
                )));
            }
        };
        let (Some(short_call), Some(short_put)) = (
            position.leg(LegRole::ShortCall),
            position.leg(LegRole::ShortPut),
        ) else {
            return Err(invalid("a condor exit needs both shorts"));
        };
        let aware = ist_offset()
            .from_local_datetime(&now)
            .single()
            .ok_or_else(|| invalid("an IST wall-clock time"))?;
        let verdict = condor::exit_decision(
            &CondorTick {
                entry_credit: position.entry_points,
                cost_now: value,
                spot: index.spot,
                short_call_strike: short_call.strike,
                short_put_strike: short_put.strike,
                now: aware,
                stale,
                marked_loss_inr: loss,
                risk_budget_inr: position.risk_budget_inr,
                manual,
            },
            cfg,
            &options.risk,
        )?;
        return Ok(verdict.map(condor_code));
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
            position.opened_at,
            session_open,
            options.directional.bar_minutes,
        )?;
        let verdict = directional::exit_decision(
            &LongTick {
                entry: position.entry_points,
                bid: value,
                entered_at: position.opened_at,
                now,
                is_invalidated: directional::invalidated(five, direction, high, low),
                stale,
                marked_loss_inr: loss,
                risk_budget_inr: position.risk_budget_inr,
                manual,
            },
            &options.directional,
            &options.risk,
        )?;
        return Ok(verdict.map(directional::LongExit::as_str));
    }
    let cfg3 = &options.expiry_setups;
    let is_invalidated = if position.sleeve == Sleeve::O3b {
        let Some(half_gap) = position.half_gap else {
            return Err(invalid("an O3-B position carries its half-gap"));
        };
        expiry_setups::gap_hold_invalidated(
            latest_bar_after(&index.bars, now, position.opened_at).as_ref(),
            direction,
            half_gap,
        )
    } else {
        let (Some(high), Some(low)) = (position.range_high, position.range_low) else {
            return Err(invalid("an O3-A position carries its morning range"));
        };
        let five = latest_five_minute_close(
            &index.bars,
            now,
            position.opened_at,
            session_open,
            cfg3.bar_minutes,
        )?;
        expiry_setups::range_break_invalidated(five, direction, high, low)
    };
    let width = match position.width_points {
        Some(w) if !w.is_zero() => w,
        _ => cfg3.width_points,
    };
    let verdict = expiry_setups::exit_decision(
        &SpreadTick {
            value,
            entry_debit: position.entry_points,
            width,
            is_invalidated,
            now,
            stale,
            marked_loss_inr: loss,
            risk_budget_inr: position.risk_budget_inr,
            manual,
        },
        cfg3,
        &options.risk,
    )?;
    Ok(verdict.map(expiry_setups::SpreadExit::as_str))
}

const fn sleeve_value(sleeve: Sleeve) -> &'static str {
    match sleeve {
        Sleeve::O1m => "O1M",
        Sleeve::O1w => "O1W",
        Sleeve::O2 => "O2",
        Sleeve::O3a => "O3A",
        Sleeve::O3b => "O3B",
    }
}
