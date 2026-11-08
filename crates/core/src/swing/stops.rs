//! Port of `baskfy_core.swing.stops` (`decile-blueprint/packages/core/src/baskfy_core/swing/stops.py`).
//!
//! Stops and the day-by-day management of an open position (docs/swing/04 §6).
//!
//! Goldens: `L1/swing/manage.*` (`baskfy_core.swing.stops.manage`).
//!
//! Ported so far: the position, bar and action types, [`partial_quantity`] and [`manage`].
//! `initial_stop`, `widest_stop_pct`, `choose_trail` and `apply` are not yet ported.

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::swing::sizing::{ConfigNumber, checked, config_number};

/// Ports `baskfy_core.swing.config.StopConfig` — stops and exits (docs/swing/04 §6). It lives
/// here until `swing::config` is ported (that file is outside this port's ownership); missing
/// keys take Python's defaults.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct StopConfig {
    /// The partial sale's window opens this many bars after entry...
    pub partial_earliest_bar: i64,
    /// ...and closes after this many.
    pub partial_latest_bar: i64,
    /// The partial as a ratio of integers: numerator.
    pub partial_numerator: i64,
    /// The partial as a ratio of integers: denominator.
    pub partial_denominator: i64,
    /// Move the stop to breakeven once this many R is showing.
    pub breakeven_after_r: f64,
    /// ADR% at or above which the fast (10-day) trail is used.
    pub fast_trail_min_adr_pct: f64,
    /// An entry stop may not sit further below the entry than this.
    pub max_stop_distance_pct: f64,
    /// The widest stop is `min(max_stop_distance_pct, adr_pct x this)`.
    pub max_stop_adr_multiple: f64,
}

impl Default for StopConfig {
    fn default() -> Self {
        Self {
            partial_earliest_bar: 3,
            partial_latest_bar: 5,
            partial_numerator: 1,
            partial_denominator: 3,
            breakeven_after_r: 1.0,
            fast_trail_min_adr_pct: 6.0,
            max_stop_distance_pct: 10.0,
            max_stop_adr_multiple: 1.0,
        }
    }
}

/// Which moving average the remainder trails.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum TrailMa {
    /// The 10-day, for fast movers.
    #[serde(rename = "MA10")]
    Ma10,
    /// The 20-day, for slower names.
    #[serde(rename = "MA20")]
    Ma20,
}

/// What to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ActionKind {
    /// Sell part of the position at the next open.
    SellPartial,
    /// Sell the remainder at the next open.
    SellAll,
    /// Move the stop up.
    RaiseStop,
    /// The hard stop was hit inside the bar.
    StoppedOut,
    /// Nothing.
    Hold,
}

/// Why.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ActionReason {
    /// A green position inside the partial window.
    PartialIntoStrength,
    /// The stop goes to breakeven with the partial.
    BreakevenAfterPartial,
    /// `breakeven_after_r` R is showing.
    BreakevenAtR,
    /// A close below the trail MA.
    CloseBelowTrailMa,
    /// An EP bought on its gap day closed red that day.
    EpFailedRedOnDay,
    /// The bar's low reached the stop.
    HardStopHit,
    /// Nothing fired.
    NothingToDo,
}

/// The state the rules read. Quantities are shares; prices are exchange (raw) prices.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OpenPosition {
    /// The trading symbol.
    pub symbol: String,
    /// The entry session.
    pub entry_date: NaiveDate,
    /// The entry price.
    pub entry: Decimal,
    /// The stop set on entry day: one R is `entry - initial_stop`.
    pub initial_stop: Decimal,
    /// The stop in force now.
    pub stop: Decimal,
    /// Shares held.
    pub quantity: i64,
    /// Whether the partial sale has happened.
    pub partial_done: bool,
    /// The MA the remainder trails.
    pub trail: TrailMa,
    /// True for an EP bought on its gap day: a red close on that day is a failed EP.
    #[serde(default)]
    pub is_ep_gap_day: bool,
}

/// One daily bar of the held name.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DailyBar {
    /// The session.
    pub date: NaiveDate,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
    /// The 10-day MA, when there is enough history.
    pub ma10: Option<Decimal>,
    /// The 20-day MA, when there is enough history.
    pub ma20: Option<Decimal>,
    /// Bars since entry, counting the entry day as 0. Supplied by the caller, which owns the
    /// trading calendar (law 1: no calendar arithmetic in core).
    pub bars_since_entry: i64,
}

/// One instruction for the next session.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Action {
    /// What to do.
    pub kind: ActionKind,
    /// Why.
    pub reason: ActionReason,
    /// Shares the action sells; 0 for RAISE_STOP and HOLD.
    pub quantity: i64,
    /// The new stop, only on RAISE_STOP.
    pub new_stop: Option<Decimal>,
}

impl Action {
    fn sell(kind: ActionKind, reason: ActionReason, quantity: i64) -> Self {
        Self {
            kind,
            reason,
            quantity,
            new_stop: None,
        }
    }

    fn raise_stop(reason: ActionReason, new_stop: Decimal) -> Self {
        Self {
            kind: ActionKind::RaiseStop,
            reason,
            quantity: 0,
            new_stop: Some(new_stop),
        }
    }
}

/// Python's `a // b` on integers: floor division; `b == 0` is an error.
fn floor_div(a: i64, b: i64) -> CoreResult<i64> {
    let overflow = || CoreError::Arithmetic(format!("{a} // {b}"));
    let q = a.checked_div(b).ok_or_else(overflow)?;
    let r = a.checked_rem(b).ok_or_else(overflow)?;
    if r != 0 && ((r < 0) != (b < 0)) {
        q.checked_sub(1).ok_or_else(overflow)
    } else {
        Ok(q)
    }
}

/// How many shares the partial sale is; never the whole position, never zero unless tiny.
///
/// # Errors
///
/// [`CoreError::Arithmetic`] when `partial_denominator` is zero or the product overflows.
pub fn partial_quantity(quantity: i64, config: &StopConfig) -> CoreResult<i64> {
    let numerator = config.partial_numerator;
    let denominator = config.partial_denominator;
    let product = quantity
        .checked_mul(numerator)
        .ok_or_else(|| CoreError::Arithmetic("partial quantity overflowed".to_owned()))?;
    let part = floor_div(product, denominator)?;
    Ok(part.max(0).min(quantity.saturating_sub(1).max(0)))
}

/// Python's `showing_r >= Decimal(str(threshold))`. An infinite (or beyond-`Decimal`) threshold
/// compares as Python's does — `+inf` is never reached, `-inf` always is — and NaN raises.
fn reaches(showing_r: Decimal, threshold: f64) -> CoreResult<bool> {
    Ok(match config_number(threshold)? {
        ConfigNumber::Finite(t) => showing_r >= t,
        ConfigNumber::Beyond { positive, .. } => !positive,
    })
}

fn trail_level(position: &OpenPosition, bar: &DailyBar) -> Option<Decimal> {
    match position.trail {
        TrailMa::Ma10 => bar.ma10,
        TrailMa::Ma20 => bar.ma20,
    }
}

/// The actions after `bar`'s close, in the order they should be applied.
///
/// Precedence: a hard stop hit inside the bar ends the position (the GTT fired, or should
/// have); a failed EP on its gap day is sold at the next open; a close below the trail MA sells
/// the remainder at the next open; otherwise the partial and the breakeven rules may fire,
/// and a position with nothing to do says so explicitly.
///
/// # Errors
///
/// [`CoreError::Arithmetic`] on a decimal overflow or a zero `partial_denominator`;
/// [`CoreError::InvalidInput`] for a NaN `breakeven_after_r` (Python's `InvalidOperation`).
pub fn manage(
    position: &OpenPosition,
    bar: &DailyBar,
    config: &StopConfig,
) -> CoreResult<Vec<Action>> {
    if bar.low <= position.stop {
        return Ok(vec![Action::sell(
            ActionKind::StoppedOut,
            ActionReason::HardStopHit,
            position.quantity,
        )]);
    }
    if position.is_ep_gap_day && bar.bars_since_entry == 0 && bar.close < bar.open {
        return Ok(vec![Action::sell(
            ActionKind::SellAll,
            ActionReason::EpFailedRedOnDay,
            position.quantity,
        )]);
    }

    if let Some(trail) = trail_level(position, bar)
        && bar.bars_since_entry > 0
        && bar.close < trail
    {
        return Ok(vec![Action::sell(
            ActionKind::SellAll,
            ActionReason::CloseBelowTrailMa,
            position.quantity,
        )]);
    }

    let mut actions = Vec::new();
    let green = bar.close > position.entry;
    let earliest = config.partial_earliest_bar;
    let latest = config.partial_latest_bar;
    let in_partial_window = earliest <= bar.bars_since_entry && bar.bars_since_entry <= latest;
    if !position.partial_done && in_partial_window && green {
        let part = partial_quantity(position.quantity, config)?;
        if part > 0 {
            actions.push(Action::sell(
                ActionKind::SellPartial,
                ActionReason::PartialIntoStrength,
                part,
            ));
            if position.stop < position.entry {
                actions.push(Action::raise_stop(
                    ActionReason::BreakevenAfterPartial,
                    position.entry,
                ));
            }
            return Ok(actions);
        }
    }

    let one_r = checked(position.entry.checked_sub(position.initial_stop), "one R")?;
    let showing_r = if one_r > Decimal::ZERO {
        checked(
            checked(bar.close.checked_sub(position.entry), "gain")?.checked_div(one_r),
            "R showing",
        )?
    } else {
        Decimal::ZERO
    };
    if position.stop < position.entry && reaches(showing_r, config.breakeven_after_r)? {
        actions.push(Action::raise_stop(
            ActionReason::BreakevenAtR,
            position.entry,
        ));
    }
    if actions.is_empty() {
        actions.push(Action::sell(ActionKind::Hold, ActionReason::NothingToDo, 0));
    }
    Ok(actions)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use std::str::FromStr;

    use super::*;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    /// Values checked against the Python function: `Decimal("Infinity")` as the breakeven R is
    /// never reached, `-Infinity` always is, NaN raises `InvalidOperation`.
    #[test]
    fn an_infinite_breakeven_compares_as_python_does() {
        let date = NaiveDate::from_ymd_opt(2026, 9, 1).unwrap();
        let position = OpenPosition {
            symbol: "X".to_owned(),
            entry_date: date,
            entry: d("100"),
            initial_stop: d("95"),
            stop: d("95"),
            quantity: 30,
            partial_done: true,
            trail: TrailMa::Ma20,
            is_ep_gap_day: false,
        };
        let bar = DailyBar {
            date,
            open: d("100"),
            high: d("120"),
            low: d("99"),
            close: d("110"),
            ma10: None,
            ma20: None,
            bars_since_entry: 1,
        };
        let run = |r: f64| {
            manage(
                &position,
                &bar,
                &StopConfig {
                    breakeven_after_r: r,
                    ..StopConfig::default()
                },
            )
        };
        assert_eq!(run(f64::INFINITY).unwrap()[0].kind, ActionKind::Hold);
        assert_eq!(run(1e30).unwrap()[0].kind, ActionKind::Hold);
        let raised = run(f64::NEG_INFINITY).unwrap();
        assert_eq!(raised[0].reason, ActionReason::BreakevenAtR);
        assert!(run(f64::NAN).is_err());
    }
}
