//! `baskfy_core.exits.qulla` — Qullamaggie's exit rule as the TWT and VBT books call it (LV9).
//!
//! The rule itself is [`baskfy_core::swing::stops::manage`]; this is the adapter: the trailing
//! means and the ADR in Python's `Decimal` context ([`WideDecimal`]), the trail chosen once, and
//! the actions folded into one [`Decision`]. Pure — it lives in the worker because
//! `baskfy_core::exits` has no Rust home yet (R2.3: a Python module with no Rust home lives with
//! the port that needed it). **Law 2:** a decision is a number on a row; nothing here sells.

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::swing::plan::choose_trail;
use baskfy_core::swing::stops::{ActionKind, DailyBar, OpenPosition, StopConfig, TrailMa, manage};
use chrono::NaiveDate;
use rust_decimal::Decimal;

use super::common::{core_err, value_err};
use crate::error::WorkerError;

/// `PARTIAL`: a sale that leaves the position open.
pub(crate) const PARTIAL: &str = "PARTIAL";
/// `MA_TRAIL`: the sale that closes it.
pub(crate) const MA_TRAIL: &str = "MA_TRAIL";
/// `MA_FAST`.
const MA_FAST: usize = 10;
/// `MA_SLOW`.
const MA_SLOW: usize = 20;
/// `ADR_WINDOW`.
const ADR_WINDOW: usize = 20;

/// `OhlcBar`: one session, exchange prices.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct OhlcBar {
    pub date: NaiveDate,
    pub open: WideDecimal,
    pub high: WideDecimal,
    pub low: WideDecimal,
    pub close: WideDecimal,
}

/// `Position`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Position {
    pub symbol: String,
    pub entry_date: NaiveDate,
    pub entry: Decimal,
    pub initial_stop: Decimal,
    pub stop: Decimal,
    pub quantity: i64,
    pub partial_done: bool,
    pub trail: Option<TrailMa>,
}

/// `Decision` (the fields the nightly reads).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Decision {
    pub trail: TrailMa,
    pub sell_quantity: i64,
    pub sell_reason: &'static str,
    pub raise_to: Option<Decimal>,
    pub stop_hit: bool,
}

/// `TrailMa.value`.
pub(crate) fn trail_value(trail: TrailMa) -> &'static str {
    match trail {
        TrailMa::Ma10 => "MA10",
        TrailMa::Ma20 => "MA20",
    }
}

/// `TrailMa(value)`.
pub(crate) fn trail_from(value: &str) -> Result<TrailMa, WorkerError> {
    match value {
        "MA10" => Ok(TrailMa::Ma10),
        "MA20" => Ok(TrailMa::Ma20),
        other => Err(value_err(format!("'{other}' is not a valid TrailMa"))),
    }
}

fn dec_err(e: impl std::fmt::Display) -> WorkerError {
    WorkerError::python("InvalidOperation", e.to_string())
}

/// `mean(values, window)`: `sum(values[-window:], Decimal(0)) / window`, `None` before the
/// window is full.
pub(crate) fn mean(
    values: &[WideDecimal],
    window: usize,
) -> Result<Option<WideDecimal>, WorkerError> {
    if values.len() < window {
        return Ok(None);
    }
    let tail = values.get(values.len() - window..).unwrap_or(&[]);
    let total = WideDecimal::sum(tail);
    total
        .div(&WideDecimal::from(window))
        .map(Some)
        .map_err(dec_err)
}

/// `adr_pct(series, window)`: mean of `(high / low - 1) * 100` over the last `window` bars
/// (each with a positive low), quantized to 0.01 in the context's rounding (half-even).
pub(crate) fn adr_pct(series: &[OhlcBar], window: usize) -> Result<Option<Decimal>, WorkerError> {
    if series.len() < window {
        return Ok(None);
    }
    let one = WideDecimal::from(1_i64);
    let hundred = WideDecimal::from(100_i64);
    let mut ranges = Vec::with_capacity(window);
    for bar in series.get(series.len() - window..).unwrap_or(&[]) {
        if bar.low > WideDecimal::zero() {
            let ratio = bar.high.div(&bar.low).map_err(dec_err)?;
            ranges.push(ratio.sub(&one).mul(&hundred));
        }
    }
    if ranges.len() < window {
        return Ok(None);
    }
    let average = WideDecimal::sum(&ranges)
        .div(&WideDecimal::from(window))
        .map_err(dec_err)?;
    let q = average.quantize(-2, Rounding::HalfEven).map_err(dec_err)?;
    q.try_to_decimal().map(Some).map_err(dec_err)
}

/// `trail_for`: the trail from the ADR; too young for one, the slow average.
pub(crate) fn trail_for(series: &[OhlcBar], config: &StopConfig) -> Result<TrailMa, WorkerError> {
    match adr_pct(series, ADR_WINDOW)? {
        None => Ok(TrailMa::Ma20),
        Some(adr) => choose_trail(adr, config).map_err(core_err),
    }
}

fn narrow(x: &WideDecimal) -> Result<Decimal, WorkerError> {
    x.try_to_decimal().map_err(dec_err)
}

/// `decide`: the rule over today's close; `None` when the name did not print on `on`.
pub(crate) fn decide(
    position: &Position,
    series: &[OhlcBar],
    on: NaiveDate,
    bars_since_entry: i64,
    config: &StopConfig,
) -> Result<Option<Decision>, WorkerError> {
    let Some(today) = series.last() else {
        return Ok(None);
    };
    if today.date != on {
        return Ok(None);
    }
    let trail = match position.trail {
        Some(t) => t,
        None => trail_for(series, config)?,
    };
    let closes: Vec<WideDecimal> = series.iter().map(|b| b.close.clone()).collect();
    let ma10 = mean(&closes, MA_FAST)?.as_ref().map(narrow).transpose()?;
    let ma20 = mean(&closes, MA_SLOW)?.as_ref().map(narrow).transpose()?;
    let bar = DailyBar {
        date: on,
        open: narrow(&today.open)?,
        high: narrow(&today.high)?,
        low: narrow(&today.low)?,
        close: narrow(&today.close)?,
        ma10,
        ma20,
        bars_since_entry,
    };
    let actions = manage(
        &OpenPosition {
            symbol: position.symbol.clone(),
            entry_date: position.entry_date,
            entry: position.entry,
            initial_stop: position.initial_stop,
            stop: position.stop,
            quantity: position.quantity,
            partial_done: position.partial_done,
            trail,
            is_ep_gap_day: false,
        },
        &bar,
        config,
    )
    .map_err(core_err)?;
    let mut decision = Decision {
        trail,
        sell_quantity: 0,
        sell_reason: "",
        raise_to: None,
        stop_hit: false,
    };
    for action in actions {
        match action.kind {
            ActionKind::StoppedOut => decision.stop_hit = true,
            ActionKind::SellAll => {
                decision.sell_quantity = position.quantity;
                decision.sell_reason = MA_TRAIL;
            }
            ActionKind::SellPartial => {
                decision.sell_quantity = action.quantity;
                decision.sell_reason = PARTIAL;
            }
            ActionKind::RaiseStop => {
                // The note's wording is not persisted by the nightly; only the level is.
                if let Some(level) = action.new_stop {
                    decision.raise_to = Some(level);
                }
            }
            ActionKind::Hold => {}
        }
    }
    Ok(Some(decision))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w(s: &str) -> WideDecimal {
        s.parse().unwrap_or_default()
    }

    fn day(n: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 9, n).unwrap_or_default()
    }

    fn bar(n: u32, low: &str, high: &str, close: &str) -> OhlcBar {
        OhlcBar {
            date: day(n),
            open: w(close),
            high: w(high),
            low: w(low),
            close: w(close),
        }
    }

    #[test]
    fn mean_is_none_before_the_window_is_full() {
        let v = vec![w("1"), w("2")];
        assert_eq!(mean(&v, 3).ok().flatten(), None);
        assert_eq!(
            mean(&[w("1"), w("2"), w("4")], 3)
                .ok()
                .flatten()
                .map(|d| d.to_sci_string()),
            Some("2.333333333333333333333333333".to_owned())
        );
    }

    #[test]
    fn adr_needs_a_full_window_of_positive_lows() {
        let series: Vec<OhlcBar> = (1..=20).map(|n| bar(n, "100", "110", "105")).collect();
        assert_eq!(
            adr_pct(&series, 20).ok().flatten().map(|d| d.to_string()),
            Some("10.00".to_owned())
        );
        let mut short = series.clone();
        short.remove(0);
        assert_eq!(adr_pct(&short, 20).ok().flatten(), None);
        let mut zero = series;
        if let Some(b) = zero.get_mut(3) {
            b.low = w("0");
        }
        assert_eq!(adr_pct(&zero, 20).ok().flatten(), None);
    }

    #[test]
    fn a_young_name_trails_the_slow_average_and_no_bar_today_is_no_decision() {
        let series: Vec<OhlcBar> = (1..=5).map(|n| bar(n, "100", "110", "105")).collect();
        let config = StopConfig::default();
        assert_eq!(trail_for(&series, &config).ok(), Some(TrailMa::Ma20));
        let position = Position {
            symbol: "X".to_owned(),
            entry_date: day(1),
            entry: Decimal::new(100, 0),
            initial_stop: Decimal::new(90, 0),
            stop: Decimal::new(90, 0),
            quantity: 30,
            partial_done: false,
            trail: None,
        };
        assert_eq!(
            decide(&position, &series, day(6), 5, &config).ok(),
            Some(None)
        );
        assert_eq!(decide(&position, &[], day(5), 4, &config).ok(), Some(None));
    }

    #[test]
    fn a_trail_round_trips_its_value() {
        for t in [TrailMa::Ma10, TrailMa::Ma20] {
            assert_eq!(trail_from(trail_value(t)).ok(), Some(t));
        }
        assert!(trail_from("MA50").is_err());
    }

    /// `test_twt_qulla.py`'s `_sessions`: the last `count` weekdays up to `end`, oldest first.
    fn sessions(count: usize) -> Vec<NaiveDate> {
        let mut out = Vec::new();
        let mut d = NaiveDate::from_ymd_opt(2026, 9, 25).unwrap_or_default();
        while out.len() < count {
            if chrono::Datelike::weekday(&d).number_from_monday() <= 5 {
                out.push(d);
            }
            d = d.pred_opt().unwrap_or_default();
        }
        out.reverse();
        out
    }

    /// `add_bar`: open = high = low = close.
    fn flat(days: &[NaiveDate], close_of: impl Fn(NaiveDate) -> &'static str) -> Vec<OhlcBar> {
        days.iter()
            .map(|&d| {
                let c = w(close_of(d));
                OhlcBar {
                    date: d,
                    open: c.clone(),
                    high: c.clone(),
                    low: c.clone(),
                    close: c,
                }
            })
            .collect()
    }

    fn held(entry: &str, stop: &str, partial_done: bool) -> Position {
        let d = |s: &str| s.parse::<Decimal>().unwrap_or_default();
        Position {
            symbol: "X".to_owned(),
            entry_date: NaiveDate::from_ymd_opt(2026, 9, 1).unwrap_or_default(),
            entry: d(entry),
            initial_stop: d(stop),
            stop: d(stop),
            quantity: 300,
            partial_done,
            trail: None,
        }
    }

    #[test]
    fn a_green_bar_three_sessions_in_queues_a_third_and_the_breakeven() {
        let days = sessions(60);
        let as_of = days[59];
        let bars = flat(&days, |d| if d == as_of { "110.00" } else { "100.00" });
        let decision = decide(
            &held("100.00", "80.00", false),
            &bars,
            as_of,
            3,
            &StopConfig::default(),
        )
        .ok()
        .flatten();
        let decision = decision.unwrap_or(Decision {
            trail: TrailMa::Ma10,
            sell_quantity: -1,
            sell_reason: "",
            raise_to: None,
            stop_hit: true,
        });
        assert_eq!(
            (decision.sell_reason, decision.sell_quantity),
            (PARTIAL, 100)
        );
        assert_eq!(decision.raise_to, "100.00".parse::<Decimal>().ok());
        assert_eq!(decision.trail, TrailMa::Ma20);
        assert!(!decision.stop_hit);
    }

    #[test]
    fn a_close_below_the_trail_queues_the_remainder() {
        let days = sessions(60);
        let as_of = days[59];
        let bars = flat(&days, |d| if d == as_of { "100.00" } else { "120.00" });
        let decision = decide(
            &held("120.00", "90.00", true),
            &bars,
            as_of,
            14,
            &StopConfig::default(),
        )
        .ok()
        .flatten();
        assert_eq!(
            decision.map(|d| (d.sell_reason, d.sell_quantity)),
            Some((MA_TRAIL, 300))
        );
    }

    #[test]
    fn a_young_history_sells_nothing() {
        let days = sessions(60);
        let as_of = days[59];
        let recent = days.get(54..).unwrap_or(&[]);
        let bars = flat(recent, |d| if d == as_of { "80.00" } else { "100.00" });
        let decision = decide(
            &held("100.00", "80.00", false),
            &bars,
            as_of,
            4,
            &StopConfig::default(),
        )
        .ok()
        .flatten();
        assert_eq!(decision.map(|d| d.sell_quantity), Some(0));
    }
}
