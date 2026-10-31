//! Port of `baskfy_core.opportunities.validation`: a point-in-time walk-forward test of one
//! setup's triggered signals (entry next open, exit at stop, first target or horizon close).

use std::collections::{BTreeMap, BTreeSet};

use chrono::{Datelike, NaiveDate};
use polars::prelude::DataFrame;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::opportunities::config::{SetupType, SwingFilters, SwingParams, TriggerStatus};
use crate::opportunities::indicators::{IndicatorRow, indicator_rows};
use crate::opportunities::swing::{InstrumentContext, evaluate, passes_filters};
use crate::ranking_engine::py_format_g;
use crate::twt::indicators::frame::cmp_nulls_first;

const BPS: f64 = 10_000.0;
const PCT: f64 = 100.0;
/// Ports `NOTIONAL_INR`: the order size the fee model is priced on.
pub const NOTIONAL_INR: f64 = 100_000.0;

// `costs.py` rates (Zerodha NSE delivery), as `order_cost` uses them.
const STT_DELIVERY: f64 = 0.001;
const EXCHANGE_TXN_NSE: f64 = 0.000_029_7;
const SEBI_TURNOVER: f64 = 0.000_001;
const STAMP_DUTY_BUY: f64 = 0.000_15;
const GST: f64 = 0.18;
const BROKERAGE_DELIVERY: f64 = 0.0;
const DP_CHARGE_PER_SELL: f64 = 15.93;

/// Ports `ExitReason`.
#[derive(Debug, Clone, Copy)]
pub struct ExitReason;

impl ExitReason {
    /// Stopped out at invalidation.
    pub const STOP: &'static str = "STOP";
    /// First target reached.
    pub const TARGET: &'static str = "TARGET";
    /// Horizon close.
    pub const TIME: &'static str = "TIME";
}

/// Ports `SignalTrade`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SignalTrade {
    /// Instrument id.
    pub instrument_id: i64,
    /// Signal bar's date.
    pub signal_date: NaiveDate,
    /// Entry bar's date (the next session).
    pub entry_date: NaiveDate,
    /// Exit bar's date.
    pub exit_date: NaiveDate,
    /// Entry (next open).
    pub entry: f64,
    /// Stop.
    pub stop: f64,
    /// First target.
    pub target: f64,
    /// Exit fill.
    pub exit: f64,
    /// [`ExitReason`] value.
    pub reason: String,
    /// Net return after costs, %.
    pub net_return_pct: f64,
    /// Net return in R.
    pub r_multiple: f64,
    /// Worst excursion in R.
    pub min_r: f64,
}

/// Ports `YearStats`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct YearStats {
    /// Calendar year of the signals.
    pub year: i32,
    /// Trades that year.
    pub trades: i64,
    /// Mean R that year.
    pub avg_r: Option<f64>,
}

/// Ports `ValidationResult`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ValidationResult {
    /// Setup tested.
    pub setup: SetupType,
    /// First signal date.
    pub start: NaiveDate,
    /// Last signal date.
    pub end: NaiveDate,
    /// Trades taken.
    pub trades: Vec<SignalTrade>,
    /// Signals skipped because the next open gapped past a level.
    pub skipped_gap: i64,
    /// The test's assumptions, in words.
    pub assumptions: Vec<String>,
}

fn mean(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        None
    } else {
        Some(values.iter().fold(0.0, |a, v| a + v) / values.len() as f64)
    }
}

impl ValidationResult {
    /// Ports `wins`: trades with a positive R.
    #[must_use]
    pub fn wins(&self) -> i64 {
        self.trades.iter().filter(|t| t.r_multiple > 0.0).count() as i64
    }

    /// Ports `hit_rate_pct`.
    #[must_use]
    pub fn hit_rate_pct(&self) -> Option<f64> {
        (!self.trades.is_empty())
            .then(|| self.wins() as f64 / self.trades.len() as f64 * PCT)
    }

    /// Ports `avg_return_pct`.
    #[must_use]
    pub fn avg_return_pct(&self) -> Option<f64> {
        mean(&self.trades.iter().map(|t| t.net_return_pct).collect::<Vec<_>>())
    }

    /// Ports `avg_r`.
    #[must_use]
    pub fn avg_r(&self) -> Option<f64> {
        mean(&self.trades.iter().map(|t| t.r_multiple).collect::<Vec<_>>())
    }

    /// Ports `max_adverse_r`: the worst `min_r` over all trades.
    #[must_use]
    pub fn max_adverse_r(&self) -> Option<f64> {
        self.trades
            .iter()
            .map(|t| t.min_r)
            .reduce(|a, b| if b < a { b } else { a })
    }

    /// Ports `by_year`: trades and mean R per signal year, ascending.
    #[must_use]
    pub fn by_year(&self) -> Vec<YearStats> {
        let mut grouped: BTreeMap<i32, Vec<f64>> = BTreeMap::new();
        for trade in &self.trades {
            grouped
                .entry(trade.signal_date.year())
                .or_default()
                .push(trade.r_multiple);
        }
        grouped
            .into_iter()
            .map(|(year, values)| YearStats {
                year,
                trades: values.len() as i64,
                avg_r: mean(&values),
            })
            .collect()
    }
}

/// Ports `members_as_of`: the latest membership snapshot on or before `day`; `None` reads as
/// Python's empty `frozenset()`.
#[must_use]
pub fn members_as_of(
    membership: &BTreeMap<NaiveDate, BTreeSet<i64>>,
    day: NaiveDate,
) -> Option<&BTreeSet<i64>> {
    membership.range(..=day).next_back().map(|(_, m)| m)
}

/// `costs.cost_pct`: one delivery order's fees as % of its value.
fn cost_pct(value: f64, sell: bool) -> f64 {
    let v = value.abs();
    if v == 0.0 {
        return 100.0;
    }
    let exchange = v * EXCHANGE_TXN_NSE;
    let sebi = v * SEBI_TURNOVER;
    let brokerage = v * BROKERAGE_DELIVERY;
    let stt = v * STT_DELIVERY;
    let stamp = if sell { 0.0 } else { v * STAMP_DUTY_BUY };
    let gst = GST * (brokerage + exchange + sebi);
    let dp = if sell { DP_CHARGE_PER_SELL } else { 0.0 };
    let total = stt + exchange + sebi + stamp + gst + dp + brokerage;
    total / v * 100.0
}

/// Ports `round_trip_cost_pct`: buy and sell fees on [`NOTIONAL_INR`] plus slippage both sides.
#[must_use]
pub fn round_trip_cost_pct(slippage_bps: f64) -> f64 {
    let fees = cost_pct(NOTIONAL_INR, false) + cost_pct(NOTIONAL_INR, true);
    fees + 2.0 * slippage_bps / BPS * PCT
}

/// Ports `_f`: a bar's price, or Python's `ValueError("bar is missing <key>")`.
fn f(value: Option<f64>, key: &str) -> CoreResult<f64> {
    value.ok_or_else(|| CoreError::InvalidInput(format!("bar is missing {key}")))
}

/// Ports `simulate`: `(exit_index, fill, reason, worst_r)`, or `None` when there is no next bar
/// or the next open is at/below the stop or at/above the target.
///
/// # Errors
/// [`CoreError::InvalidInput`] when a bar in the path lacks open/high/low/close.
pub fn simulate(
    rows: &[IndicatorRow],
    signal_index: usize,
    stop: f64,
    target: f64,
    horizon: i64,
) -> CoreResult<Option<(usize, f64, &'static str, f64)>> {
    let entry_row = signal_index + 1;
    let Some(first) = rows.get(entry_row) else {
        return Ok(None);
    };
    let entry = f(first.open, "open")?;
    let risk = entry - stop;
    if risk <= 0.0 || entry >= target {
        return Ok(None);
    }
    let mut worst = 0.0_f64;
    let entry_i = i64::try_from(entry_row).unwrap_or(i64::MAX);
    let tail = i64::try_from(rows.len()).unwrap_or(i64::MAX) - 1;
    let last_i = (entry_i + horizon - 1).min(tail);
    let last = usize::try_from(last_i)
        .map_err(|_| CoreError::InvalidInput(format!("horizon must be >= 0, got {horizon}")))?;
    for (index, bar) in rows.iter().enumerate().take(last + 1).skip(entry_row) {
        let (open, high, low) = (f(bar.open, "open")?, f(bar.high, "high")?, f(bar.low, "low")?);
        let r = (low - entry) / risk;
        if r < worst {
            worst = r;
        }
        if low <= stop {
            let fill = if index > entry_row && open < stop { open } else { stop };
            return Ok(Some((index, fill, ExitReason::STOP, worst)));
        }
        if high >= target {
            let fill = if index > entry_row && open > target { open } else { target };
            return Ok(Some((index, fill, ExitReason::TARGET, worst)));
        }
    }
    let close = f(rows.get(last).and_then(|r| r.close), "close")?;
    Ok(Some((last, close, ExitReason::TIME, worst)))
}

/// Ports `validate_setup`: walk every instrument's bars in `[start, end]`, take each triggered
/// signal (one at a time per name), and simulate it on the bars that follow.
///
/// # Errors
/// A polars error reading the frame; [`CoreError::InvalidInput`] for a bar missing a price or
/// an in-range row without an instrument id.
#[allow(clippy::too_many_arguments)]
pub fn validate_setup(
    indicated: &DataFrame,
    setup: SetupType,
    start: NaiveDate,
    end: NaiveDate,
    membership: Option<&BTreeMap<NaiveDate, BTreeSet<i64>>>,
    filters: &SwingFilters,
    slippage_bps: f64,
    params: &SwingParams,
) -> CoreResult<ValidationResult> {
    let (low, high) = params.horizon_for(setup);
    let mut horizon = (low.max(filters.horizon_min), high.min(filters.horizon_max));
    if horizon.0 > horizon.1 {
        horizon = (low, high);
    }
    let costs = round_trip_cost_pct(slippage_bps);
    let mut all = indicator_rows(indicated)?;
    all.sort_by(|a, b| {
        cmp_nulls_first(&a.instrument_id, &b.instrument_id)
            .then_with(|| cmp_nulls_first(&a.date, &b.date))
    });
    let context = InstrumentContext::default();
    let mut trades = Vec::new();
    let mut skipped = 0_i64;
    let mut begin = 0;
    while begin < all.len() {
        let id = all[begin].instrument_id;
        let stop_at = all[begin..]
            .iter()
            .position(|r| r.instrument_id != id)
            .map_or(all.len(), |k| begin + k);
        let rows = &all[begin..stop_at];
        begin = stop_at;
        let mut busy_until: i64 = -1;
        for (index, row) in rows.iter().enumerate() {
            let Some(day) = row.date else { continue };
            let idx = i64::try_from(index).unwrap_or(i64::MAX);
            if day < start || day > end || idx <= busy_until {
                continue;
            }
            let instrument_id = row
                .instrument_id
                .ok_or_else(|| CoreError::InvalidInput("row has no instrument_id".to_owned()))?;
            if let Some(m) = membership
                && !members_as_of(m, day).is_some_and(|s| s.contains(&instrument_id))
            {
                continue;
            }
            if row.bars.unwrap_or(0) < params.data.min_bars {
                continue;
            }
            if !passes_filters(row, &context, filters) {
                continue;
            }
            let Some(evaluation) = evaluate(row, setup, params, horizon) else {
                continue;
            };
            if evaluation.status != TriggerStatus::Triggered {
                continue;
            }
            let target = evaluation.targets.first().map_or(f64::NAN, |t| t.price);
            let outcome = simulate(rows, index, evaluation.invalidation, target, horizon.1)?;
            let Some((exit_index, fill, reason, worst)) = outcome else {
                if index + 1 < rows.len() {
                    skipped += 1;
                }
                continue;
            };
            let entry_row = &rows[index + 1];
            let entry = f(entry_row.open, "open")?;
            let gross_pct = (fill / entry - 1.0) * PCT;
            let net_pct = gross_pct - costs;
            let risk_pct = (entry - evaluation.invalidation) / entry * PCT;
            let (Some(exit_day), Some(entry_day)) = (rows[exit_index].date, entry_row.date) else {
                continue;
            };
            trades.push(SignalTrade {
                instrument_id,
                signal_date: day,
                entry_date: entry_day,
                exit_date: exit_day,
                entry,
                stop: evaluation.invalidation,
                target,
                exit: fill,
                reason: reason.to_owned(),
                net_return_pct: net_pct,
                r_multiple: net_pct / risk_pct,
                min_r: worst,
            });
            busy_until = i64::try_from(exit_index).unwrap_or(i64::MAX);
        }
    }
    Ok(ValidationResult {
        setup,
        start,
        end,
        trades,
        skipped_gap: skipped,
        assumptions: vec![
            "Signals use completed daily bars only; indicators are trailing windows.".to_owned(),
            "Entry at the next session's open; skipped when it opens at or below invalidation \
             or at or above the first target."
                .to_owned(),
            "Exit at invalidation, first target, or the close after the horizon; a bar touching \
             both counts as the stop."
                .to_owned(),
            // NOTIONAL_INR formatted as Python's `:,.0f`.
            format!(
                "Costs: NSE delivery fees on a ₹100,000 notional plus {} bps slippage per side \
                 ({costs:.3}% round trip).",
                py_format_g(slippage_bps, 6)
            ),
            "Universe membership is point-in-time when the index has membership rows.".to_owned(),
            "Prices are today's adjusted series; adjustments use corporate actions known now."
                .to_owned(),
        ],
    })
}
