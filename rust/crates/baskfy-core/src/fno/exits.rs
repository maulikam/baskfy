//! Port of `baskfy_core.fno.exits` (`decile-blueprint/packages/core/src/baskfy_core/fno/exits.py`).
//!
//! Exits and pauses (`04` §1, §7, §10).
//!
//! **F1**: `LATE_EXIT` when the hard-exit date has passed (a rule violation, exited at the next
//! open), the loss close, the profit take, and the hard exit at 15:00 on `E - n`. The cost to
//! close is valued by [`super::condor::cost_to_close`] (clamped, so never beyond max loss).
//!
//! **F2**: initial stop `entry - stop_atr x ATR14`; the trail `max(stop, highest close since
//! entry - stop_atr x ATR14 at entry)`, **never lowered**; the GTT triggers at
//! `max(stop, stop_from_vol(entry, ann_vol))`; the time exit at 15:00 on the
//! `f2_max_sessions`-th session of the hold (the entry session is the first); the roll at 15:00
//! on `E - n`.
//!
//! [`stop_from_vol`] is the desk's formula, re-implemented pure: `pct = clip(ann_vol ÷ √52 x
//! STOP_VOL_MULT, STOP_MIN, STOP_MAX)`, `round(price x (1 - pct), 1)` — on floats, as Python
//! computes it, then `Decimal(repr(...))`.
//!
//! **Pauses** (`04` §7) stop new entries only; open structures run to their own exits.
//!
//! Goldens: `L1/fno/exits.<function>.case_NNN.json`.

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use super::calendar::PyDateTime;
use super::condor::{CondorStrikes, loss_close_hit, num, profit_take_hit};
use super::config::py::{add, mul, sub};
use super::config::{CommonConfig, F1Config, F2Config, FnoError, FnoResult, StopVolConfig, py};

const WEEKS_PER_YEAR: f64 = 52.0;

/// Why a position is closed.
///
/// Ports `baskfy_core.fno.exits.ExitReason`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ExitReason {
    /// The hard-exit date passed while the desk was down.
    LateExit,
    /// F1's loss close.
    LossClose,
    /// F1's profit take.
    ProfitTake,
    /// 15:00 on `E - n` (F1), or F3's expiry-day exit.
    HardExit,
    /// F2's stop.
    Stop,
    /// F2's session limit.
    TimeExit,
    /// F2's calendar roll.
    Roll,
    /// F2's future without its resting GTT (`04` §10; FO7).
    NakedFuture,
    /// F3: the key level broke.
    LevelBreak,
    /// F3: the loss cut.
    LossCut,
    /// F3: the decay target.
    DecayTarget,
}

/// An exit and its reason in words.
///
/// Ports `baskfy_core.fno.exits.ExitDecision`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ExitDecision {
    /// The reason.
    pub reason: ExitReason,
    /// The decision in words.
    pub message: String,
}

fn decision(reason: ExitReason, message: impl Into<String>) -> ExitDecision {
    ExitDecision {
        reason,
        message: message.into(),
    }
}

/// The F1 exit due at `now`, or `None`. `close_cost` is `None` without marks.
///
/// Ports `baskfy_core.fno.exits.f1_exit`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn f1_exit(
    now: PyDateTime,
    hard_exit_date: NaiveDate,
    close_cost: Option<Decimal>,
    entry_credit: Decimal,
    strikes: &CondorStrikes,
    f1: &F1Config,
    common: &CommonConfig,
) -> FnoResult<Option<ExitDecision>> {
    let today = now.date();
    if today > hard_exit_date {
        return Ok(Some(decision(
            ExitReason::LateExit,
            format!(
                "hard-exit date {} passed while the desk was down",
                hard_exit_date.format("%Y-%m-%d")
            ),
        )));
    }
    if let Some(cost) = close_cost {
        if loss_close_hit(cost, entry_credit, strikes, f1)? {
            return Ok(Some(decision(
                ExitReason::LossClose,
                format!(
                    "cost to close {} ≥ (1 + {}) x credit {}",
                    py::str(cost),
                    py::str(f1.loss_close_mult),
                    py::str(entry_credit)
                ),
            )));
        }
        if profit_take_hit(cost, entry_credit, f1)? {
            return Ok(Some(decision(
                ExitReason::ProfitTake,
                format!(
                    "cost to close {} ≤ {} % of {}",
                    py::str(cost),
                    py::str(sub(Decimal::ONE_HUNDRED, f1.profit_take_pct)?),
                    py::str(entry_credit)
                ),
            )));
        }
    }
    if today == hard_exit_date && now.time() >= common.hard_exit_time {
        return Ok(Some(decision(
            ExitReason::HardExit,
            "15:00 on E - n; never into expiry day",
        )));
    }
    Ok(None)
}

/// Python's `max(a, b)` on floats: the first unless the second is strictly greater.
fn fmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's `min(a, b)` on floats: the first unless the second is strictly smaller.
fn fmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// The desk's vol-scaled stop, 8-12 % below `price` (see the module docs).
///
/// Ports `baskfy_core.fno.exits.stop_from_vol`.
///
/// # Errors
/// A price that is not a finite float, or a stop that is not (Python's `Decimal('nan')` has no
/// `rust_decimal` form).
pub fn stop_from_vol(price: Decimal, ann_vol: f64, config: &StopVolConfig) -> FnoResult<Decimal> {
    let raw = ann_vol / WEEKS_PER_YEAR.sqrt() * config.vol_mult;
    let pct = fmin(fmax(raw, config.stop_min), config.stop_max);
    let stop = num::py_round(num::to_f64(price)? * (1.0 - pct), 1);
    num::dec_from_float(stop)
}

/// `entry - f2_stop_atr x ATR14` (`04` §10).
///
/// Ports `baskfy_core.fno.exits.f2_initial_stop`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn f2_initial_stop(
    entry: Decimal,
    atr_at_signal: Decimal,
    f2: &F2Config,
) -> FnoResult<Decimal> {
    sub(entry, mul(f2.stop_atr, atr_at_signal)?)
}

/// Each close: `max(stop, highest close - mult x ATR at entry)`. Never lowered; with
/// `f2_trail` off the stop stays where it is.
///
/// Ports `baskfy_core.fno.exits.f2_trail`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn f2_trail(
    stop: Decimal,
    highest_close: Decimal,
    atr_at_entry: Decimal,
    f2: &F2Config,
) -> FnoResult<Decimal> {
    if !f2.trail {
        return Ok(stop);
    }
    Ok(py::max(
        stop,
        sub(highest_close, mul(f2.stop_atr, atr_at_entry)?)?,
    ))
}

/// `max(stop, stop_from_vol(entry, ann_vol))` — the tighter of the two for a long.
///
/// Ports `baskfy_core.fno.exits.gtt_trigger`.
///
/// # Errors
/// As [`stop_from_vol`].
pub fn gtt_trigger(
    stop: Decimal,
    entry: Decimal,
    ann_vol: f64,
    config: &StopVolConfig,
) -> FnoResult<Decimal> {
    Ok(py::max(stop, stop_from_vol(entry, ann_vol, config)?))
}

/// The `f2_max_sessions`-th session of the hold, the entry session counted as the first.
///
/// Ports `baskfy_core.fno.exits.f2_time_exit_session`.
///
/// # Errors
/// Python's `IndexError` (as `ValueError`) for a negative index beyond the calendar — only a
/// `max_sessions` below 1, which the config's bounds refuse.
pub fn f2_time_exit_session(
    sessions: &[NaiveDate],
    entry_session: NaiveDate,
    f2: &F2Config,
) -> FnoResult<Option<NaiveDate>> {
    let Some(i) = sessions.iter().position(|d| *d == entry_session) else {
        return Ok(None);
    };
    let len = i64::try_from(sessions.len()).unwrap_or(i64::MAX);
    let j = i64::try_from(i).unwrap_or(i64::MAX) + f2.max_sessions - 1;
    if j >= len {
        return Ok(None);
    }
    // Python indexes a negative `j` from the end.
    let at = if j < 0 { len + j } else { j };
    usize::try_from(at)
        .ok()
        .and_then(|k| sessions.get(k))
        .copied()
        .map(Some)
        .ok_or_else(|| FnoError::Value("list index out of range".to_owned()))
}

/// The F2 action due at `now`: the stop first, then the time exit, then the roll.
///
/// Ports `baskfy_core.fno.exits.f2_exit`.
#[must_use]
pub fn f2_exit(
    now: PyDateTime,
    price: Decimal,
    stop: Decimal,
    time_exit_date: Option<NaiveDate>,
    roll_date: Option<NaiveDate>,
    common: &CommonConfig,
) -> Option<ExitDecision> {
    if price <= stop {
        return Some(decision(
            ExitReason::Stop,
            format!("price {} ≤ stop {}", py::str(price), py::str(stop)),
        ));
    }
    let at_close = now.time() >= common.hard_exit_time;
    let today = now.date();
    if let Some(t) = time_exit_date
        && (today > t || (today == t && at_close))
    {
        return Some(decision(
            ExitReason::TimeExit,
            format!("session limit reached {}", t.format("%Y-%m-%d")),
        ));
    }
    if roll_date == Some(today) && at_close {
        return Some(decision(
            ExitReason::Roll,
            "15:00 on E - n: sell the held month, buy the next",
        ));
    }
    None
}

/// `rows[-n:]` with Python's slice rules.
fn py_tail<T>(rows: &[T], n: i64) -> &[T] {
    let len = i64::try_from(rows.len()).unwrap_or(i64::MAX);
    let start = if n == 0 {
        0
    } else if -n < 0 {
        (len - n).max(0)
    } else {
        (-n).min(len)
    };
    usize::try_from(start)
        .ok()
        .and_then(|s| rows.get(s..))
        .unwrap_or(&[])
}

/// `04` §7: the last `pause_consecutive` closed F1 trades on one underlying were each ≤
/// `pause_loss_r`. `closed_r` is in close order, oldest first.
///
/// Ports `baskfy_core.fno.exits.f1_paused`.
#[must_use]
pub fn f1_paused(closed_r: &[Decimal], f1: &F1Config) -> bool {
    let tail = py_tail(closed_r, f1.pause_consecutive);
    i64::try_from(tail.len()).is_ok_and(|n| n == f1.pause_consecutive)
        && tail.iter().all(|r| *r <= f1.pause_loss_r)
}

/// `04` §7: the calendar month's closed F2 trades sum to ≤ `month_pause_r`.
///
/// Ports `baskfy_core.fno.exits.f2_month_paused`.
///
/// # Errors
/// A sum the default context cannot hold.
pub fn f2_month_paused(month_closed_r: &[Decimal], f2: &F2Config) -> FnoResult<bool> {
    let mut total = Decimal::ZERO;
    for r in month_closed_r {
        total = add(total, *r)?;
    }
    Ok(total <= f2.month_pause_r)
}

/// `04` §7: the month's realised FO loss has reached `monthly_pause_inr`.
///
/// Ports `baskfy_core.fno.exits.book_paused`.
#[must_use]
pub fn book_paused(month_realised_inr: Decimal, monthly_pause_inr: Decimal) -> bool {
    month_realised_inr <= -monthly_pause_inr
}
