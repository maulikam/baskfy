//! Port of `baskfy_core.swing.opening_range` (`decile-blueprint/packages/core/src/baskfy_core/swing/opening_range.py`).
//!
//! The opening range and the live trigger (docs/swing/04 §7).
//!
//! Goldens: `L1/swing/evaluate_trigger.*` (`baskfy_core.swing.opening_range.evaluate_trigger`).
//!
//! Ported so far: [`OpeningRange`], [`TriggerState`], [`TriggerVerdict`] and
//! [`evaluate_trigger`]. `opening_range`, `live_gap` and `live_gap_score` are not yet ported.

use chrono::{NaiveDateTime, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::swing::sizing::{ConfigNumber, checked, config_number};

/// Ports `baskfy_core.swing.config.OpeningRangeConfig` — the live monitor (docs/swing/04 §7).
/// It lives here until `swing::config` is ported (that file is outside this port's
/// ownership); missing keys take Python's defaults. Times are IST `(hour, minute)`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct OpeningRangeConfig {
    /// Candidate opening-range windows, in minutes from the 09:15 open.
    pub windows_minutes: Vec<i64>,
    /// The window used when none is named.
    pub default_window_minutes: i64,
    /// The break must clear the range high by this percent.
    pub break_buffer_pct: f64,
    /// An EP at the open: minimum live gap, percent.
    pub live_min_gap_pct: f64,
    /// An EP at the open: minimum volume pace.
    pub live_min_volume_pace: f64,
    /// The session open.
    pub session_open: (i64, i64),
    /// When triggers stop being evaluated — the whole cash session since 9 Sep 2026.
    pub monitor_close: (i64, i64),
    /// When the session's housekeeping (A7/A8) runs.
    pub pending_cutoff_at: (i64, i64),
    /// Entry cap: percent of chase over the trigger.
    pub entry_limit_buffer_pct: f64,
    /// Entry cap: ADRs above the range high.
    pub entry_limit_max_adr: f64,
    /// `"MARKET"` (with protection) or `"LIMIT"` (A8).
    pub entry_order_type: String,
    /// Market protection floor, percent.
    pub market_protection_floor_pct: f64,
    /// Market protection ceiling, percent.
    pub market_protection_max_pct: f64,
    /// How long a confirm waits for a fill, seconds.
    pub fill_poll_seconds: f64,
    /// How often it asks, seconds.
    pub fill_poll_interval_seconds: f64,
    /// Minutes after the window before the historical candles reconcile the tick range.
    pub range_reconcile_delay_minutes: i64,
    /// The quote fallback's minimum interval, seconds.
    pub quote_poll_min_seconds: f64,
    /// When the desk sweeps for a filled quantity without a GTT.
    pub gtt_sweep_at: (i64, i64),
}

impl Default for OpeningRangeConfig {
    fn default() -> Self {
        Self {
            windows_minutes: vec![1, 5, 60],
            default_window_minutes: 5,
            break_buffer_pct: 0.1,
            live_min_gap_pct: 10.0,
            live_min_volume_pace: 3.0,
            session_open: (9, 15),
            monitor_close: (15, 30),
            pending_cutoff_at: (10, 45),
            entry_limit_buffer_pct: 0.5,
            entry_limit_max_adr: 0.25,
            entry_order_type: "MARKET".to_owned(),
            market_protection_floor_pct: 0.05,
            market_protection_max_pct: 3.0,
            fill_poll_seconds: 10.0,
            fill_poll_interval_seconds: 0.5,
            range_reconcile_delay_minutes: 1,
            quote_poll_min_seconds: 5.0,
            gtt_sweep_at: (15, 15),
        }
    }
}

/// The high/low of the first `window_minutes` from the session open.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OpeningRange {
    /// The range high.
    pub high: Decimal,
    /// The range low.
    pub low: Decimal,
    /// The window, in minutes from the open.
    pub window_minutes: i64,
    /// False while the window has not closed yet — a range is not a range until it is.
    pub complete: bool,
    /// How many candles fell inside the window.
    pub candles: i64,
}

/// The monitor's verdict on one name at one moment.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TriggerState {
    /// The ORH break happened: buy.
    Triggered,
    /// The range is complete and the price has not broken it.
    Waiting,
    /// The window has not closed, or it had no candles.
    RangeIncomplete,
    /// A FLAG broke its opening range but sits at or under the daily pivot.
    BelowPivot,
    /// The price is at or above the upper circuit: a lock, not a fill.
    LockedUpperCircuit,
    /// After `monitor_close`.
    SessionOver,
}

/// The state, and — when it triggered — the entry and stop.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TriggerVerdict {
    /// What the monitor should do.
    pub state: TriggerState,
    /// The entry price, only on TRIGGERED.
    pub entry: Option<Decimal>,
    /// The stop, only on TRIGGERED.
    pub stop: Option<Decimal>,
    /// The range high, once the range is known.
    pub range_high: Option<Decimal>,
    /// The range low, once the range is known.
    pub range_low: Option<Decimal>,
}

impl TriggerVerdict {
    fn bare(state: TriggerState) -> Self {
        Self {
            state,
            entry: None,
            stop: None,
            range_high: None,
            range_low: None,
        }
    }

    fn with_range(state: TriggerState, opening: &OpeningRange) -> Self {
        Self {
            state,
            entry: None,
            stop: None,
            range_high: Some(opening.high),
            range_low: Some(opening.low),
        }
    }
}

/// Python's `dt.time(hour, minute)`, which raises on an out-of-range hour or minute.
fn clock_time(hour: i64, minute: i64) -> CoreResult<NaiveTime> {
    let bad = || CoreError::InvalidInput("monitor_close is not a valid time".to_owned());
    let hour = u32::try_from(hour).map_err(|_| bad())?;
    let minute = u32::try_from(minute).map_err(|_| bad())?;
    NaiveTime::from_hms_opt(hour, minute, 0).ok_or_else(bad)
}

/// Python's `not (last_price <= high + high * Decimal(str(buffer_pct)) / 100)`.
///
/// A buffer beyond `Decimal`'s range (an infinity, or a float past ~7.9e28) makes the break
/// level `±inf` as it does in Python: unreachable for a positive range high, always cleared
/// for a negative one. A zero high with an infinite buffer is Python's `InvalidOperation`
/// (`0 x inf`); with a finite huge buffer it is a zero buffer.
fn clears_break_level(last_price: Decimal, high: Decimal, buffer_pct: f64) -> CoreResult<bool> {
    let buffer = match config_number(buffer_pct)? {
        ConfigNumber::Finite(pct) => checked(
            checked(high.checked_mul(pct), "break buffer")?.checked_div(Decimal::ONE_HUNDRED),
            "break buffer",
        )?,
        ConfigNumber::Beyond { infinite, .. } if high.is_zero() => {
            if infinite {
                return Err(CoreError::InvalidInput(
                    "a zero range high times an infinite break buffer".to_owned(),
                ));
            }
            Decimal::ZERO
        }
        ConfigNumber::Beyond { positive, .. } => {
            // The level is +inf when the buffer's sign matches the high's, -inf otherwise.
            let level_is_plus_inf = positive == high.is_sign_positive();
            return Ok(!level_is_plus_inf);
        }
    };
    let level = checked(high.checked_add(buffer), "break level")?;
    Ok(last_price > level)
}

/// Has the ORH break happened, and if so at what entry and stop?
///
/// * After `monitor_close` the answer is SESSION_OVER (strictly after: a reading at the close
///   minute itself still counts).
/// * The range must be complete and hold at least one candle.
/// * A positive upper circuit at or below the last price is a lock, not a trigger.
/// * `last_price` must exceed the range high by `break_buffer_pct`.
/// * For a FLAG the price must also be above the daily pivot; pass `pivot_high = None` for an
///   EP, whose pivot is the gap itself.
///
/// The stop is the lower of the range low and the low of the day so far (the range low on a
/// tie), never above the range low.
///
/// # Errors
///
/// [`CoreError::InvalidInput`] for an invalid `monitor_close`, a NaN buffer, or a zero range
/// high with an infinite buffer (Python's `InvalidOperation`);
/// [`CoreError::Arithmetic`] on a decimal overflow.
pub fn evaluate_trigger(
    last_price: Decimal,
    opening: &OpeningRange,
    pivot_high: Option<Decimal>,
    low_of_day: Decimal,
    upper_circuit: Option<Decimal>,
    at: NaiveDateTime,
    config: &OpeningRangeConfig,
) -> CoreResult<TriggerVerdict> {
    let (hour, minute) = config.monitor_close;
    if at.time() > clock_time(hour, minute)? {
        return Ok(TriggerVerdict::bare(TriggerState::SessionOver));
    }
    if !opening.complete || opening.candles == 0 {
        return Ok(TriggerVerdict::bare(TriggerState::RangeIncomplete));
    }
    if let Some(circuit) = upper_circuit
        && circuit > Decimal::ZERO
        && last_price >= circuit
    {
        return Ok(TriggerVerdict::with_range(
            TriggerState::LockedUpperCircuit,
            opening,
        ));
    }
    if !clears_break_level(last_price, opening.high, config.break_buffer_pct)? {
        return Ok(TriggerVerdict::with_range(TriggerState::Waiting, opening));
    }
    if let Some(pivot) = pivot_high
        && last_price <= pivot
    {
        return Ok(TriggerVerdict::with_range(
            TriggerState::BelowPivot,
            opening,
        ));
    }
    // Python's `min(opening.low, low_of_day)`: the first argument on a tie.
    let stop = if low_of_day < opening.low {
        low_of_day
    } else {
        opening.low
    };
    Ok(TriggerVerdict {
        state: TriggerState::Triggered,
        entry: Some(last_price),
        stop: Some(stop),
        range_high: Some(opening.high),
        range_low: Some(opening.low),
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use std::str::FromStr;

    use chrono::NaiveDate;

    use super::*;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    fn run(high: &str, buffer_pct: f64) -> CoreResult<TriggerVerdict> {
        let opening = OpeningRange {
            high: d(high),
            low: d("0"),
            window_minutes: 5,
            complete: true,
            candles: 5,
        };
        let at = NaiveDate::from_ymd_opt(2026, 9, 2)
            .unwrap()
            .and_hms_opt(10, 0, 0)
            .unwrap();
        evaluate_trigger(
            d("101"),
            &opening,
            None,
            d("0"),
            None,
            at,
            &OpeningRangeConfig {
                break_buffer_pct: buffer_pct,
                ..OpeningRangeConfig::default()
            },
        )
    }

    /// Values checked against the Python function: an infinite buffer puts the break level at
    /// `±Infinity`; `0 x Infinity` and a NaN buffer raise `InvalidOperation`.
    #[test]
    fn an_infinite_buffer_compares_as_python_does() {
        assert_eq!(
            run("100", f64::INFINITY).unwrap().state,
            TriggerState::Waiting
        );
        assert_eq!(run("100", 1e30).unwrap().state, TriggerState::Waiting);
        assert_eq!(
            run("100", f64::NEG_INFINITY).unwrap().state,
            TriggerState::Triggered
        );
        assert!(run("100", f64::NAN).is_err());
        assert!(run("0", f64::INFINITY).is_err());
        assert_eq!(run("0", 1e30).unwrap().state, TriggerState::Triggered);
    }
}
