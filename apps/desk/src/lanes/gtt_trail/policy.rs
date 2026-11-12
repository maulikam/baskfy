//! Pure trailing-stop math (GTT-TRAIL): no I/O, no clock. A stop only ever moves up.

use chrono::NaiveDate;
use serde::{Deserialize, Serialize};

/// Every sleeve whose GTT stops the policy covers.
pub const STRATEGIES: [&str; 5] = ["swing", "twt", "vbt", "screen", "weekly"];
/// Sleeves whose positions carry a stored entry and GTT id the job can act on.
pub const WIRED: [&str; 3] = ["swing", "twt", "vbt"];
/// TWT's trail floor (`BASKFY_TWT_TRAIL_PCT_MIN`, DECISIONS-TW TW0.5): tighter is the failure mode.
pub const TWT_TRAIL_PCT_MIN: f64 = 18.0;
/// A new stop stays at least this far under the last close, so it is never a sell at once.
pub const CAP_GAP_PCT: f64 = 0.5;
const TICK_PAISE: i64 = 5;
const EPS: f64 = 1e-9;

/// One strategy's policy, in percent.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Policy {
    /// Sleeve.
    pub strategy: String,
    /// The strategy's policy is on.
    pub enabled: bool,
    /// Move the stop to entry (plus buffer) once the highest close is up this much.
    pub breakeven_after_pct: f64,
    /// The breakeven stop sits this far above entry (costs and the GTT limit's slippage).
    pub breakeven_buffer_pct: f64,
    /// Start trailing once the highest close is up this much.
    pub trail_after_pct: f64,
    /// Trail this far under the highest close since entry.
    pub trail_pct: f64,
    /// Skip a raise smaller than this, relative to the resting stop.
    pub min_step_pct: f64,
}

impl Policy {
    /// The recorded default: off, breakeven at +3% (Maulik: "if stock is in profit, we always exit in
    /// profit"), trail 10% from +10%, 1% steps (TWT trails 20% from +20%).
    #[must_use]
    pub fn default_for(strategy: &str) -> Self {
        let twt = strategy == "twt";
        Self {
            strategy: strategy.to_owned(),
            enabled: false,
            breakeven_after_pct: 3.0,
            breakeven_buffer_pct: 0.5,
            trail_after_pct: if twt { 20.0 } else { 10.0 },
            trail_pct: if twt { 20.0 } else { 10.0 },
            min_step_pct: 1.0,
        }
    }

    /// Bounds a saved policy must meet.
    ///
    /// # Errors
    /// The first out-of-range field, in words.
    pub fn validate(&self) -> Result<(), String> {
        let s = self.strategy.as_str();
        if !STRATEGIES.contains(&s) {
            return Err(format!("unknown strategy {s}"));
        }
        let check = |name: &str, v: f64, lo: f64, hi: f64| {
            if v.is_finite() && v >= lo && v <= hi {
                Ok(())
            } else {
                Err(format!(
                    "{s}: {name} must be between {lo} and {hi}, got {v}"
                ))
            }
        };
        check("breakeven_after_pct", self.breakeven_after_pct, 0.5, 100.0)?;
        check("breakeven_buffer_pct", self.breakeven_buffer_pct, 0.0, 5.0)?;
        check("trail_after_pct", self.trail_after_pct, 0.5, 200.0)?;
        check("trail_pct", self.trail_pct, 1.0, 50.0)?;
        check("min_step_pct", self.min_step_pct, 0.0, 10.0)?;
        if self.breakeven_buffer_pct >= self.breakeven_after_pct {
            return Err(format!(
                "{s}: breakeven_buffer_pct must be below breakeven_after_pct"
            ));
        }
        if s == "twt" && self.trail_pct < TWT_TRAIL_PCT_MIN {
            return Err(format!(
                "twt: trail_pct below {TWT_TRAIL_PCT_MIN}% is refused (TW0.5: tightening the trail is the failure mode)"
            ));
        }
        Ok(())
    }
}

/// What the math reads for one position.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Inputs {
    /// Entry average.
    pub entry: f64,
    /// The resting trigger.
    pub current_stop: f64,
    /// Highest exchange close since entry.
    pub highest_close: f64,
    /// Last exchange close.
    pub last_close: f64,
}

/// The proposed stop and why.
#[derive(Debug, Clone, PartialEq)]
pub struct Proposal {
    /// The resting stop when nothing changes.
    pub new_stop: f64,
    /// The stop moves up.
    pub changed: bool,
    /// Why.
    pub reason: String,
    /// `(new_stop / entry - 1) * 100`, two places.
    pub locked_pct: f64,
}

fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

/// Up to the 0.05 tick: the breakeven floor never rounds below entry plus its buffer.
#[must_use]
#[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
pub fn ceil_tick(x: f64) -> f64 {
    let paise = (x * 100.0 - EPS).ceil() as i64;
    let rem = paise.rem_euclid(TICK_PAISE);
    (if rem == 0 {
        paise
    } else {
        paise + TICK_PAISE - rem
    }) as f64
        / 100.0
}

/// Down to the 0.05 tick, so the stop never rounds above what the rule allows.
#[must_use]
#[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
pub fn floor_tick(x: f64) -> f64 {
    let paise = (x * 100.0 + EPS).floor() as i64;
    (paise - paise.rem_euclid(TICK_PAISE)) as f64 / 100.0
}

fn hold(current: f64, entry: f64, reason: impl Into<String>) -> Proposal {
    Proposal {
        new_stop: current,
        changed: false,
        reason: reason.into(),
        locked_pct: if entry > 0.0 {
            round2((current / entry - 1.0) * 100.0)
        } else {
            0.0
        },
    }
}

/// The proposed stop: breakeven, then trail, capped under the last close, floored to the tick;
/// never below the resting stop, never a step smaller than `min_step_pct`.
#[must_use]
pub fn propose(p: &Policy, i: &Inputs) -> Proposal {
    let ok = |v: f64| v.is_finite() && v > 0.0;
    if !ok(i.entry) || !ok(i.current_stop) || !ok(i.highest_close) || !ok(i.last_close) {
        return hold(i.current_stop, i.entry, "missing entry, stop or close");
    }
    let gain = (i.highest_close / i.entry - 1.0) * 100.0;
    let floor = ceil_tick(i.entry * (1.0 + p.breakeven_buffer_pct / 100.0));
    let mut cand = i.current_stop;
    let mut why = Vec::new();
    if gain + EPS >= p.breakeven_after_pct && floor > cand {
        cand = floor;
        why.push(format!("breakeven (high close +{gain:.2}%)"));
    }
    if gain + EPS >= p.trail_after_pct {
        let trail = (i.highest_close * (1.0 - p.trail_pct / 100.0)).max(floor);
        if trail > cand {
            cand = trail;
            why.push(format!(
                "trail {}% under high close {:.2}",
                p.trail_pct, i.highest_close
            ));
        }
    }
    if why.is_empty() {
        return hold(
            i.current_stop,
            i.entry,
            format!("high close +{gain:.2}% has not reached a rule"),
        );
    }
    let cap = i.last_close * (1.0 - CAP_GAP_PCT / 100.0);
    if cand > cap {
        if cap < floor {
            return hold(
                i.current_stop,
                i.entry,
                "last close is under the breakeven floor; stop left as it is",
            );
        }
        cand = cap;
        why.push(format!("capped {CAP_GAP_PCT}% under last close"));
    }
    let cand = if cand <= floor + EPS {
        floor
    } else {
        floor_tick(cand).max(floor)
    };
    if cand <= i.current_stop + EPS {
        return hold(i.current_stop, i.entry, "already at or above the proposal");
    }
    let step = (cand / i.current_stop - 1.0) * 100.0;
    if step + EPS < p.min_step_pct {
        return hold(
            i.current_stop,
            i.entry,
            format!("step {step:.2}% is under min step {}%", p.min_step_pct),
        );
    }
    Proposal {
        new_stop: cand,
        changed: true,
        reason: why.join("; "),
        locked_pct: round2((cand / i.entry - 1.0) * 100.0),
    }
}

/// One published bar: date, adjusted close, exchange close.
pub type Bar = (NaiveDate, f64, f64);

/// Highest and last exchange close over `[entry_date, as_of]` — bars after `as_of` are ignored
/// (no look-ahead). `Err` when a corporate action moved the adjustment since entry.
///
/// # Errors
/// No bar in the window, or the adjustment ratio changed inside it.
pub fn closes_since(
    bars: &[Bar],
    entry_date: NaiveDate,
    as_of: NaiveDate,
) -> Result<(f64, f64, NaiveDate), String> {
    let mut window: Vec<&Bar> = bars
        .iter()
        .filter(|(d, _, raw)| *d >= entry_date && *d <= as_of && *raw > 0.0)
        .collect();
    window.sort_by_key(|b| b.0);
    let Some(last) = window.last() else {
        return Err("no published bar since entry".to_owned());
    };
    let ratio0 = window[0].1 / window[0].2;
    if window
        .iter()
        .any(|(_, adj, raw)| ((adj / raw) - ratio0).abs() > 1e-6)
    {
        return Err("a corporate action moved the series since entry; review by hand".to_owned());
    }
    let hi = window.iter().map(|b| b.2).fold(f64::MIN, f64::max);
    Ok((hi, last.2, last.0))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    fn pol() -> Policy {
        Policy {
            enabled: true,
            ..Policy::default_for("swing")
        }
    }

    fn inp(entry: f64, stop: f64, hi: f64, last: f64) -> Inputs {
        Inputs {
            entry,
            current_stop: stop,
            highest_close: hi,
            last_close: last,
        }
    }

    #[test]
    fn breakeven_never_rounds_below_entry_plus_buffer() {
        let mut p = pol();
        p.breakeven_buffer_pct = 0.0;
        let out = propose(&p, &inp(610.7669, 488.6, 653.9, 653.9));
        assert!(out.changed);
        assert!(out.new_stop >= 610.7669, "{}", out.new_stop);
    }

    #[test]
    fn under_breakeven_holds() {
        let p = propose(&pol(), &inp(100.0, 90.0, 102.5, 102.0));
        assert!(!p.changed);
        assert!((p.new_stop - 90.0).abs() < 1e-9);
    }

    #[test]
    fn breakeven_moves_to_entry_plus_buffer() {
        let p = propose(&pol(), &inp(100.0, 90.0, 106.0, 105.0));
        assert!(p.changed, "{p:?}");
        assert!((p.new_stop - 100.5).abs() < 1e-9, "{p:?}");
    }

    #[test]
    fn trail_from_highest_close() {
        let p = propose(&pol(), &inp(100.0, 90.0, 130.0, 128.0));
        assert!(p.changed);
        assert!((p.new_stop - 117.0).abs() < 1e-9, "{p:?}");
    }

    #[test]
    fn never_moves_down() {
        let p = propose(&pol(), &inp(100.0, 125.0, 130.0, 128.0));
        assert!(!p.changed);
        assert!((p.new_stop - 125.0).abs() < 1e-9);
    }

    #[test]
    fn min_step_skips_churn() {
        let p = propose(&pol(), &inp(100.0, 116.5, 130.0, 128.0));
        assert!(!p.changed, "{p:?}");
    }

    #[test]
    fn capped_under_last_close_and_never_below_entry() {
        // trail says 117 but price fell to 110: cap 109.45, still above the floor.
        let p = propose(&pol(), &inp(100.0, 90.0, 130.0, 110.0));
        assert!(p.changed);
        assert!((p.new_stop - 109.45).abs() < 1e-9, "{p:?}");
        assert!(p.new_stop < 110.0);
        // fallen under the floor: nothing.
        let p = propose(&pol(), &inp(100.0, 90.0, 130.0, 100.6));
        assert!(!p.changed, "{p:?}");
    }

    #[test]
    fn tick_floor() {
        assert!((floor_tick(117.04) - 117.0).abs() < 1e-9);
        assert!((floor_tick(613.82) - 613.8).abs() < 1e-9);
        assert!((floor_tick(100.5) - 100.5).abs() < 1e-9);
    }

    #[test]
    fn no_look_ahead() {
        let d = |n: u32| NaiveDate::from_ymd_opt(2026, 11, n).unwrap();
        let bars = vec![
            (d(8), 90.0, 90.0),
            (d(9), 100.0, 100.0),
            (d(10), 104.0, 104.0),
            (d(12), 200.0, 200.0),
        ];
        let (hi, last, on) = closes_since(&bars, d(9), d(11)).unwrap();
        assert!((hi - 104.0).abs() < 1e-9);
        assert!((last - 104.0).abs() < 1e-9);
        assert_eq!(on, d(10));
    }

    #[test]
    fn corporate_action_refuses() {
        let d = |n: u32| NaiveDate::from_ymd_opt(2026, 11, n).unwrap();
        let bars = vec![(d(9), 50.0, 100.0), (d(10), 104.0, 104.0)];
        assert!(closes_since(&bars, d(9), d(10)).is_err());
    }

    #[test]
    fn validation() {
        assert!(Policy::default_for("twt").validate().is_ok());
        for s in STRATEGIES {
            assert!(Policy::default_for(s).validate().is_ok(), "{s}");
            assert!(!Policy::default_for(s).enabled);
        }
        let mut t = Policy::default_for("twt");
        t.trail_pct = 15.0;
        assert!(t.validate().is_err());
        let mut b = pol();
        b.breakeven_buffer_pct = 6.0;
        assert!(b.validate().is_err());
    }
}
