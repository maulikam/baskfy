//! The pure pieces of the options tab no Rust home holds yet (DECISIONS-RS R2.3's rule: a Python
//! module with no Rust port lives as a private submodule of its one caller):
//!
//! * `baskfy_core.options.calendar` — `role` and `next_session` (`04` §1.2), over the expiry
//!   dates `op_expiry` lists (`options_read._stub`'s one stub contract per expiry, so the
//!   underlying filter and the contract fields are moot);
//! * `baskfy_core.options.journal` — `summarize`, the `04` §12 summary per (sleeve, simulated,
//!   sizing mode), never pooled, in [`WideDecimal`] so it rounds where Python's `Decimal` does;
//! * `baskfy_core.market_hours_cb.is_nse_session_open`;
//! * `options_read.thresholds()` — the `OptionsConfig` fields as Python's `str()` prints them,
//!   a table generated from the live Python (`DEFAULT_OPTIONS_CONFIG`; regenerate it when a
//!   DECISIONS-OP entry changes a threshold), and `OptionsConfig.tier3_min_sessions`.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::decimal::WideDecimal;
use chrono::{Datelike, NaiveDate, NaiveTime};

// --- options.config enums --------------------------------------------------------------------

/// `Sleeve` values in `DISPLAY_ORDER` (premium selling, directional, expiry-day setups).
pub(crate) const DISPLAY_ORDER: [&str; 5] = ["O1M", "O1W", "O2", "O3A", "O3B"];

/// `Sleeve(value.upper())`: the sleeve code, or `None` (Python's `ValueError`).
pub(crate) fn sleeve(value: &str) -> Option<&'static str> {
    let upper = value.to_uppercase();
    DISPLAY_ORDER.iter().copied().find(|s| *s == upper)
}

/// `options_read._group(sleeve)`: O3A and O3B share the O3 group.
pub(crate) fn group_of(sleeve: &str) -> &str {
    if sleeve == "O3A" || sleeve == "O3B" {
        "O3"
    } else {
        sleeve
    }
}

/// `SleeveGroup` in declaration order, with `PAPER_PERIOD` (sessions, of which traded).
pub(crate) const PAPER_PERIOD: [(&str, i64, i64); 4] =
    [("O1M", 6, 3), ("O1W", 12, 6), ("O2", 60, 25), ("O3", 20, 8)];

/// `DEFAULT_OPTIONS_CONFIG.tier3_min_sessions(sleeve)`.
pub(crate) fn tier3_min_sessions(sleeve: &str) -> i64 {
    match sleeve {
        "O1M" => 12,
        "O2" => 60,
        _ => 20,
    }
}

// --- market_hours_cb -------------------------------------------------------------------------

/// `is_nse_session_open(now, trading_dates)`, with the IST wall clock already taken: true iff
/// the day is a session and the clock is inside [09:15, 15:30] inclusive.
pub(crate) fn is_nse_session_open(session_day: bool, clock: NaiveTime) -> bool {
    let open = NaiveTime::from_hms_opt(9, 15, 0).unwrap_or_default();
    let close = NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default();
    session_day && open <= clock && clock <= close
}

// --- options.calendar ------------------------------------------------------------------------

/// `RoleReason` values.
pub(crate) mod reason {
    /// `TRADES`.
    pub(crate) const TRADES: &str = "TRADES";
    /// `NOT_TRADING_DAY`.
    pub(crate) const NOT_TRADING_DAY: &str = "NOT_TRADING_DAY";
    /// `EVENT_DAY`.
    pub(crate) const EVENT_DAY: &str = "EVENT_DAY";
    /// `NOT_EXPIRY`.
    pub(crate) const NOT_EXPIRY: &str = "NOT_EXPIRY";
    /// `NOT_MONTHLY`.
    pub(crate) const NOT_MONTHLY: &str = "NOT_MONTHLY";
    /// `MONTHLY_EXPIRY`.
    pub(crate) const MONTHLY_EXPIRY: &str = "MONTHLY_EXPIRY";
}

/// `kind(rows, expiry) is MONTHLY`: the expiry is its month's last listed one.
fn is_monthly(listed: &BTreeSet<NaiveDate>, expiry: NaiveDate) -> bool {
    listed
        .iter()
        .rfind(|d| d.year() == expiry.year() && d.month() == expiry.month())
        == Some(&expiry)
}

/// `role(day, sleeve, rows=, event_days=, trading_day=)` -> `(trades, reason)`.
pub(crate) fn role(
    day: NaiveDate,
    sleeve: &str,
    listed: &BTreeSet<NaiveDate>,
    events: &BTreeSet<NaiveDate>,
    trading_day: bool,
) -> (bool, &'static str) {
    if !trading_day {
        return (false, reason::NOT_TRADING_DAY);
    }
    if events.contains(&day) {
        return (false, reason::EVENT_DAY);
    }
    if sleeve == "O2" {
        return (true, reason::TRADES);
    }
    if !listed.contains(&day) {
        return (false, reason::NOT_EXPIRY);
    }
    let monthly = is_monthly(listed, day);
    if sleeve == "O1M" && !monthly {
        return (false, reason::NOT_MONTHLY);
    }
    if sleeve == "O1W" && monthly {
        return (false, reason::MONTHLY_EXPIRY);
    }
    (true, reason::TRADES)
}

/// `next_session(after, sleeve, rows=, event_days=)`: the next listed expiry strictly after
/// `after` on which the expiry sleeve trades. Not asked for O2 (Python raises; `roles_for`
/// never asks).
pub(crate) fn next_session(
    after: NaiveDate,
    sleeve: &str,
    listed: &BTreeSet<NaiveDate>,
    events: &BTreeSet<NaiveDate>,
) -> Option<NaiveDate> {
    listed
        .iter()
        .copied()
        .filter(|d| *d > after)
        .find(|d| role(*d, sleeve, listed, events, true).0)
}

// --- options.journal -------------------------------------------------------------------------

/// `JournalRow`, the fields `summarize` reads (every row from `op_journal` is traded).
#[derive(Debug, Clone)]
pub(crate) struct JournalRow {
    pub(crate) sleeve: String,
    pub(crate) trade_date: NaiveDate,
    pub(crate) simulated: bool,
    pub(crate) sizing_mode: String,
    pub(crate) net_pnl_inr: WideDecimal,
    pub(crate) r_inr: WideDecimal,
    pub(crate) closed_reason: Option<String>,
}

impl JournalRow {
    /// `row.r`: `net_pnl_inr / r_inr` in Python's context.
    pub(crate) fn r(&self) -> Option<WideDecimal> {
        self.net_pnl_inr.div(&self.r_inr).ok()
    }
}

/// `PoolKey`: `(sleeve, simulated, sizing_mode)`.
pub(crate) type PoolKey = (String, bool, String);

/// `key_of(row)`.
pub(crate) fn key_of(row: &JournalRow) -> PoolKey {
    (row.sleeve.clone(), row.simulated, row.sizing_mode.clone())
}

/// `Bucket`.
#[derive(Debug, Clone)]
pub(crate) struct Bucket {
    pub(crate) count: i64,
    pub(crate) mean_r: WideDecimal,
}

/// `Summary`, the fields the journal route serves.
#[derive(Debug, Clone)]
pub(crate) struct Summary {
    pub(crate) key: PoolKey,
    pub(crate) count: i64,
    pub(crate) traded: i64,
    pub(crate) win_rate: Option<WideDecimal>,
    pub(crate) mean_r: Option<WideDecimal>,
    pub(crate) expectancy_inr: Option<WideDecimal>,
    pub(crate) expectancy_r: Option<WideDecimal>,
    pub(crate) worst_r: Option<WideDecimal>,
    pub(crate) max_drawdown_r: WideDecimal,
    pub(crate) max_drawdown_inr: WideDecimal,
    pub(crate) by_closed_reason: Vec<(String, Bucket)>,
}

/// `sum(values, Decimal(0))`.
fn py_sum(values: &[WideDecimal]) -> WideDecimal {
    values
        .iter()
        .fold(WideDecimal::from(0_i64), |acc, v| acc.add(v))
}

/// `max(a, b)`: `b` only when it is strictly greater (Python returns the first maximal).
fn py_max(a: WideDecimal, b: WideDecimal) -> WideDecimal {
    if b > a { b } else { a }
}

/// `_mean(values)`.
fn mean(values: &[WideDecimal]) -> Option<WideDecimal> {
    if values.is_empty() {
        return None;
    }
    py_sum(values).div(&WideDecimal::from(values.len())).ok()
}

/// `max_drawdown(values)`: the largest peak-to-trough fall of the running sum, the peak
/// starting at 0.
pub(crate) fn max_drawdown(values: &[WideDecimal]) -> WideDecimal {
    let mut peak = WideDecimal::from(0_i64);
    let mut running = WideDecimal::from(0_i64);
    let mut worst = WideDecimal::from(0_i64);
    for v in values {
        running = running.add(v);
        peak = py_max(peak, running.clone());
        worst = py_max(worst, peak.sub(&running));
    }
    worst
}

/// `summarize_one(rows)` for rows of one key.
fn summarize_one(key: PoolKey, rows: &[JournalRow]) -> Option<Summary> {
    let mut traded: Vec<&JournalRow> = rows.iter().collect();
    traded.sort_by_key(|r| r.trade_date);
    let mut rs = Vec::with_capacity(traded.len());
    for r in &traded {
        rs.push(r.r()?);
    }
    let zero = WideDecimal::from(0_i64);
    let wins = rs.iter().filter(|r| **r > zero).count();
    let win_rate = if rs.is_empty() {
        None
    } else {
        Some(
            WideDecimal::from(wins)
                .div(&WideDecimal::from(rs.len()))
                .ok()?,
        )
    };
    let mut worst: Option<WideDecimal> = None;
    for r in &rs {
        if worst.as_ref().is_none_or(|w| r < w) {
            worst = Some(r.clone());
        }
    }
    let pnl: Vec<WideDecimal> = traded.iter().map(|r| r.net_pnl_inr.clone()).collect();
    let mut grouped: BTreeMap<String, Vec<WideDecimal>> = BTreeMap::new();
    for (row, r) in traded.iter().zip(&rs) {
        let label = row
            .closed_reason
            .clone()
            .unwrap_or_else(|| "UNSPECIFIED".to_owned());
        grouped.entry(label).or_default().push(r.clone());
    }
    let mut by_closed_reason = Vec::with_capacity(grouped.len());
    for (label, values) in grouped {
        by_closed_reason.push((
            label,
            Bucket {
                count: i64::try_from(values.len()).ok()?,
                mean_r: mean(&values)?,
            },
        ));
    }
    Some(Summary {
        key,
        count: i64::try_from(rows.len()).ok()?,
        traded: i64::try_from(traded.len()).ok()?,
        win_rate,
        mean_r: mean(&rs),
        expectancy_inr: mean(&pnl),
        expectancy_r: mean(&rs),
        worst_r: worst,
        max_drawdown_r: max_drawdown(&rs),
        max_drawdown_inr: max_drawdown(&pnl),
        by_closed_reason,
    })
}

/// `summarize(rows)`: one summary per key present, ordered by (sleeve, simulated, sizing mode).
/// `None` when a row's R cannot be computed (a zero `r_inr`, which `_journal_row` never sends).
pub(crate) fn summarize(rows: &[JournalRow]) -> Option<Vec<Summary>> {
    let mut grouped: BTreeMap<PoolKey, Vec<JournalRow>> = BTreeMap::new();
    for row in rows {
        grouped.entry(key_of(row)).or_default().push(row.clone());
    }
    grouped
        .into_iter()
        .map(|(key, rows)| summarize_one(key, &rows))
        .collect()
}

// --- options_read.thresholds() ---------------------------------------------------------------

/// `thresholds()`: `(section, key, value, anchor)` for every `04` field, in `OptionsConfig`'s
/// field order, values as Python's `str()` (a `time` as `isoformat()`).
pub(crate) const THRESHOLDS: [(&str, &str, &str, &str); 148] = [
    ("calendar", "underlying", "NIFTY", "04 §1"),
    ("calendar", "allowed_underlyings", "('NIFTY',)", "04 §1"),
    ("calendar", "market_open", "09:15:00", "04 §1"),
    ("calendar", "market_close", "15:30:00", "04 §1"),
    ("chain", "snapshot_strikes", "15", "04 §2"),
    ("chain", "rate", "0.065", "04 §2"),
    ("chain", "iv_lower", "0.01", "04 §2"),
    ("chain", "iv_upper", "5.0", "04 §2"),
    ("chain", "iv_tolerance", "1e-06", "04 §2"),
    ("chain", "min_premium", "0.50", "04 §2"),
    ("chain", "max_spread_pct", "3.0", "04 §2"),
    ("chain", "depth_levels", "3", "04 §2"),
    ("chain", "min_oi_lots", "200", "04 §2"),
    ("chain", "oi_unit", "UNITS", "04 §2"),
    ("chain", "stale_quote_seconds", "15", "04 §2"),
    ("chain", "stale_index_seconds", "30", "04 §2"),
    ("chain", "stale_scan_seconds", "120", "04 §2"),
    ("chain", "greeks_model", "black76-parity-v1", "04 §2"),
    ("condor_monthly", "variant", "MONTHLY", "04 §3"),
    ("condor_monthly", "gap_max_pct", "0.75", "04 §3"),
    ("condor_monthly", "range_max_pct", "0.80", "04 §3"),
    ("condor_monthly", "er_max", "0.30", "04 §3"),
    ("condor_monthly", "observation_start", "09:15:00", "04 §3"),
    ("condor_monthly", "observation_end", "09:59:00", "04 §3"),
    ("condor_monthly", "opening_range_end", "09:44:00", "04 §3"),
    ("condor_monthly", "min_bars", "45", "04 §3"),
    ("condor_monthly", "delta_min", "0.20", "04 §3"),
    ("condor_monthly", "delta_max", "0.25", "04 §3"),
    ("condor_monthly", "delta_target", "0.22", "04 §3"),
    ("condor_monthly", "strike_buffer_points", "0", "04 §3"),
    ("condor_monthly", "wing_width_points", "150", "04 §3"),
    ("condor_monthly", "credit_floor_frac", "0.25", "04 §3"),
    ("condor_monthly", "credit_target_frac", "0.30", "04 §3"),
    ("condor_monthly", "profit_take_frac", "0.50", "04 §3"),
    ("condor_monthly", "stop_frac", "1.50", "04 §3"),
    ("condor_monthly", "plan_time", "10:00:00", "04 §3"),
    ("condor_monthly", "entry_window_end", "10:15:00", "04 §3"),
    ("condor_monthly", "hard_exit_time", "14:30:00", "04 §3"),
    ("condor_monthly", "cost_share_max", "0.20", "04 §3"),
    ("condor_monthly", "risk_per_trade_pct", "1.0", "04 §3"),
    ("condor_monthly", "max_lots", "3", "04 §3"),
    ("condor_monthly", "reserve_per_lot_inr", "1000", "04 §3"),
    ("condor_monthly", "tier3_min_sessions", "12", "04 §3"),
    ("condor_weekly", "variant", "WEEKLY", "04 §3"),
    ("condor_weekly", "gap_max_pct", "0.75", "04 §3"),
    ("condor_weekly", "range_max_pct", "0.80", "04 §3"),
    ("condor_weekly", "er_max", "0.30", "04 §3"),
    ("condor_weekly", "observation_start", "09:15:00", "04 §3"),
    ("condor_weekly", "observation_end", "09:59:00", "04 §3"),
    ("condor_weekly", "opening_range_end", "09:44:00", "04 §3"),
    ("condor_weekly", "min_bars", "45", "04 §3"),
    ("condor_weekly", "delta_min", "0.20", "04 §3"),
    ("condor_weekly", "delta_max", "0.25", "04 §3"),
    ("condor_weekly", "delta_target", "0.22", "04 §3"),
    ("condor_weekly", "strike_buffer_points", "0", "04 §3"),
    ("condor_weekly", "wing_width_points", "150", "04 §3"),
    ("condor_weekly", "credit_floor_frac", "0.25", "04 §3"),
    ("condor_weekly", "credit_target_frac", "0.30", "04 §3"),
    ("condor_weekly", "profit_take_frac", "0.50", "04 §3"),
    ("condor_weekly", "stop_frac", "1.50", "04 §3"),
    ("condor_weekly", "plan_time", "10:00:00", "04 §3"),
    ("condor_weekly", "entry_window_end", "10:15:00", "04 §3"),
    ("condor_weekly", "hard_exit_time", "14:30:00", "04 §3"),
    ("condor_weekly", "cost_share_max", "0.20", "04 §3"),
    ("condor_weekly", "risk_per_trade_pct", "0.5", "04 §3"),
    ("condor_weekly", "max_lots", "2", "04 §3"),
    ("condor_weekly", "reserve_per_lot_inr", "1000", "04 §3"),
    ("condor_weekly", "tier3_min_sessions", "20", "04 §3"),
    ("directional", "trend_ema_days", "20", "04 §4"),
    ("directional", "gap_max_pct", "1.0", "04 §4"),
    ("directional", "opening_range_start", "09:15:00", "04 §4"),
    ("directional", "opening_range_end", "09:29:00", "04 §4"),
    ("directional", "opening_range_bars", "15", "04 §4"),
    ("directional", "or_max_pct", "0.90", "04 §4"),
    ("directional", "vix_max", "22", "04 §4"),
    ("directional", "bar_minutes", "5", "04 §4"),
    ("directional", "entry_window_start", "09:30:00", "04 §4"),
    ("directional", "entry_window_end", "13:30:00", "04 §4"),
    ("directional", "buffer_pct", "0.05", "04 §4"),
    ("directional", "itm_steps", "1", "04 §4"),
    ("directional", "delta_min", "0.50", "04 §4"),
    ("directional", "delta_max", "0.75", "04 §4"),
    ("directional", "stop_frac", "0.30", "04 §4"),
    ("directional", "target_frac", "0.60", "04 §4"),
    ("directional", "time_stop_minutes", "45", "04 §4"),
    ("directional", "time_stop_min_gain", "0.10", "04 §4"),
    ("directional", "hard_exit_time", "15:00:00", "04 §4"),
    ("directional", "reserve_per_lot_inr", "300", "04 §4"),
    ("directional", "premium_cap_pct", "10", "04 §4"),
    ("directional", "cost_share_max", "0.15", "04 §4"),
    ("directional", "risk_per_trade_pct", "0.5", "04 §4"),
    ("directional", "max_lots", "2", "04 §4"),
    ("directional", "tier3_min_sessions", "60", "04 §4"),
    ("expiry_setups", "o3a_enabled", "True", "04 §5"),
    ("expiry_setups", "o3b_enabled", "True", "04 §5"),
    ("expiry_setups", "width_points", "100", "04 §5"),
    ("expiry_setups", "max_debit_frac", "0.55", "04 §5"),
    ("expiry_setups", "o3a_range_start", "09:15:00", "04 §5"),
    ("expiry_setups", "o3a_range_end", "10:14:00", "04 §5"),
    ("expiry_setups", "o3a_range_bars", "60", "04 §5"),
    ("expiry_setups", "o3a_range_max_pct", "1.20", "04 §5"),
    ("expiry_setups", "o3a_window_start", "10:19:00", "04 §5"),
    ("expiry_setups", "o3a_window_end", "13:00:00", "04 §5"),
    ("expiry_setups", "o3a_entry_window_end", "13:30:00", "04 §5"),
    ("expiry_setups", "o3a_buffer_pct", "0.05", "04 §5"),
    ("expiry_setups", "o3a_er_min", "0.40", "04 §5"),
    ("expiry_setups", "o3b_gap_min_pct", "0.50", "04 §5"),
    ("expiry_setups", "o3b_gap_max_pct", "1.50", "04 §5"),
    ("expiry_setups", "o3b_hold_end", "09:44:00", "04 §5"),
    ("expiry_setups", "o3b_plan_time", "09:45:00", "04 §5"),
    ("expiry_setups", "o3b_window_end", "10:00:00", "04 §5"),
    ("expiry_setups", "bar_minutes", "5", "04 §5"),
    ("expiry_setups", "target_frac_of_width", "0.80", "04 §5"),
    ("expiry_setups", "stop_frac_of_debit", "0.50", "04 §5"),
    ("expiry_setups", "hard_exit_time", "14:45:00", "04 §5"),
    ("expiry_setups", "reserve_per_lot_inr", "500", "04 §5"),
    ("expiry_setups", "cost_share_max", "0.20", "04 §5"),
    ("expiry_setups", "risk_per_trade_pct", "0.5", "04 §5"),
    ("expiry_setups", "max_lots", "2", "04 §5"),
    ("expiry_setups", "tier3_min_sessions", "20", "04 §5"),
    ("costs", "brokerage_per_order_inr", "20", "04 §6"),
    ("costs", "stt_sell_premium_pct", "0.15", "04 §6"),
    ("costs", "stt_exercise_intrinsic_pct", "0.15", "04 §6"),
    ("costs", "exchange_txn_pct", "0.03553", "04 §6"),
    ("costs", "sebi_per_crore_inr", "10", "04 §6"),
    ("costs", "ipft_per_crore_inr", "0.01", "04 §6"),
    ("costs", "stamp_buy_pct", "0.003", "04 §6"),
    ("costs", "gst_pct", "18", "04 §6"),
    ("costs", "auto_squareoff_inr", "50", "04 §6"),
    ("costs", "synthetic_half_spread_pct", "1.5", "04 §6"),
    ("costs", "synthetic_half_spread_min_inr", "0.10", "04 §6"),
    ("costs", "reviewed_on", "2026-09-22", "04 §6"),
    ("costs", "review_after_days", "90", "04 §6"),
    ("sizing", "first_live_trades", "5", "04 §7"),
    ("sizing", "first_live_risk_multiplier", "0.5", "04 §7"),
    ("execution", "limit_improve_ticks", "1", "04 §8"),
    ("execution", "fill_wait_seconds", "20", "04 §8"),
    ("execution", "exit_final_marketable", "True", "04 §8"),
    ("execution", "latency_ticks", "1", "04 §8"),
    ("execution", "plan_ttl_minutes", "30", "04 §8"),
    ("execution", "feed_watch_from", "14:00:00", "04 §8"),
    ("execution", "feed_grace_seconds", "60", "04 §8"),
    ("risk", "daily_loss_r", "2", "04 §9"),
    ("risk", "weekly_loss_r", "4", "04 §9"),
    ("risk", "monthly_loss_r", "8", "04 §9"),
    ("risk", "budget_breach_frac", "1.0", "04 §9"),
    ("risk", "book_daily_loss_pct", "1.5", "04 §9"),
    ("risk", "book_monthly_loss_pct", "5", "04 §9"),
];

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    #[test]
    fn roles_follow_04_1_2() {
        // Two weeklies and a monthly in October, one in November.
        let listed: BTreeSet<NaiveDate> = [
            d(2026, 10, 13),
            d(2026, 10, 20),
            d(2026, 10, 27),
            d(2026, 11, 3),
            d(2026, 11, 24),
        ]
        .into();
        let none = BTreeSet::new();
        assert_eq!(
            role(d(2026, 10, 27), "O1M", &listed, &none, true),
            (true, "TRADES")
        );
        assert_eq!(
            role(d(2026, 10, 27), "O1W", &listed, &none, true),
            (false, "MONTHLY_EXPIRY")
        );
        assert_eq!(
            role(d(2026, 10, 20), "O1M", &listed, &none, true),
            (false, "NOT_MONTHLY")
        );
        assert_eq!(
            role(d(2026, 10, 21), "O3A", &listed, &none, true),
            (false, "NOT_EXPIRY")
        );
        assert_eq!(
            role(d(2026, 10, 21), "O2", &listed, &none, true),
            (true, "TRADES")
        );
        assert_eq!(
            role(d(2026, 10, 21), "O2", &listed, &none, false),
            (false, "NOT_TRADING_DAY")
        );
        let events: BTreeSet<NaiveDate> = [d(2026, 10, 20)].into();
        assert_eq!(
            role(d(2026, 10, 20), "O2", &listed, &events, true),
            (false, "EVENT_DAY")
        );
        assert_eq!(
            next_session(d(2026, 10, 13), "O1W", &listed, &events),
            Some(d(2026, 11, 3))
        );
        assert_eq!(
            next_session(d(2026, 10, 13), "O1M", &listed, &events),
            Some(d(2026, 10, 27))
        );
        assert_eq!(
            next_session(d(2026, 11, 3), "O3B", &listed, &none),
            Some(d(2026, 11, 24))
        );
        assert_eq!(next_session(d(2026, 11, 24), "O3B", &listed, &none), None);
    }

    #[test]
    fn the_session_is_09_15_to_15_30_inclusive() {
        let t = |h, m, s| NaiveTime::from_hms_opt(h, m, s).unwrap();
        assert!(is_nse_session_open(true, t(9, 15, 0)));
        assert!(is_nse_session_open(true, t(15, 30, 0)));
        assert!(!is_nse_session_open(true, t(15, 30, 1)));
        assert!(!is_nse_session_open(false, t(12, 0, 0)));
    }

    #[test]
    fn a_summary_is_never_pooled_and_rounds_as_python() {
        let w = |s: &str| s.parse::<WideDecimal>().unwrap();
        let row = |sleeve: &str, day: u32, pnl: &str, reason: &str| JournalRow {
            sleeve: sleeve.to_owned(),
            trade_date: d(2026, 10, day),
            simulated: true,
            sizing_mode: "PAPER_ONE_LOT".to_owned(),
            net_pnl_inr: w(pnl),
            r_inr: w("300.00"),
            closed_reason: Some(reason.to_owned()),
        };
        let rows = [
            row("O2", 2, "100.00", "TARGET"),
            row("O1M", 1, "-150.00", "STOP"),
            row("O2", 1, "-300.00", "STOP"),
        ];
        let out = summarize(&rows).unwrap();
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].key.0, "O1M");
        let o2 = &out[1];
        assert_eq!(o2.count, 2);
        // Python: Decimal(1) / 2, (-1 + 0.3333…) / 2, max drawdown 1 (ordered by date).
        assert_eq!(o2.win_rate.as_ref().unwrap().to_sci_string(), "0.5");
        assert_eq!(
            o2.mean_r.as_ref().unwrap().to_sci_string(),
            "-0.3333333333333333333333333334"
        );
        assert_eq!(o2.worst_r.as_ref().unwrap().to_sci_string(), "-1");
        assert_eq!(o2.max_drawdown_r.to_sci_string(), "1");
        assert_eq!(o2.max_drawdown_inr.to_sci_string(), "300.00");
        assert_eq!(o2.by_closed_reason[0].0, "STOP");
    }

    #[test]
    fn the_thresholds_table_is_the_configs_148_fields() {
        assert_eq!(THRESHOLDS[0], ("calendar", "underlying", "NIFTY", "04 §1"));
        assert_eq!(THRESHOLDS[147].0, "risk");
        assert_eq!(tier3_min_sessions("O1W"), 20);
        assert_eq!(sleeve("o3a"), Some("O3A"));
        assert_eq!(sleeve("O4"), None);
    }
}
