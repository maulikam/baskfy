//! Port of `baskfy_core.twt.config` — every threshold of TWT-1, named once
//! (`docs/twt/04-business-rules.md`).
//!
//! Units (`04`'s preamble): every `*_pct` is a **percent** (`20.0` means 20 %) except
//! `max_position_vs_turnover`, a **fraction**; every `*_bars` / `*_sessions` counts sessions of
//! the run's own calendar except `month_low_months_back`, which counts calendar months; every
//! `*_inr` is rupees. Money, prices and multipliers that land on a price are `Decimal` (house
//! rule 9); the genuine floats are `thin_session_min_share`, `rolling_min_share` and
//! `min_pct_above_dma`, exactly as in Python.
//!
//! Every struct deserialises from the golden encoding of the Python dataclass (its field dict,
//! Decimals as strings); a missing key takes Python's default, an unknown key is refused.
//!
//! The TWT calendar and breadth hand their thresholds across in VBT's own
//! [`crate::vbt::config::VbtConfig`] (`as_shared_config`), as Python does; until the wave-3
//! housekeeping a private copy of that shape lived here (DECISIONS-RS R3.4).

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::swing::stops::StopConfig;

/// `Decimal::new(mantissa, scale)` — Python's `Decimal("...")` literal with its scale kept.
const fn dec(mantissa: i64, scale: u32) -> Decimal {
    Decimal::from_parts(
        (mantissa as u64 & 0xFFFF_FFFF) as u32,
        ((mantissa as u64) >> 32) as u32,
        0,
        false,
        scale,
    )
}

/// Ports `baskfy_core.twt.config.Gate` — the regime gate (`04` §4.3). Two values, not three.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Gate {
    /// More than the threshold of the universe is above its own average.
    #[serde(rename = "OPEN")]
    Open,
    /// At or below the threshold: no new entries.
    #[serde(rename = "SHUT")]
    Shut,
}

impl Gate {
    /// The enum's value (`"OPEN"` / `"SHUT"`).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Open => "OPEN",
            Self::Shut => "SHUT",
        }
    }
}

/// Ports `baskfy_core.twt.config.SignalState` — what a row of `tw_signal_daily` is (`03` §3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SignalState {
    /// An entry event that clears the liquidity floor.
    #[serde(rename = "SIGNAL")]
    Signal,
    /// An entry event below the floor: stored, never dropped.
    #[serde(rename = "SCAN_ONLY")]
    ScanOnly,
}

impl SignalState {
    /// The enum's value (`"SIGNAL"` / `"SCAN_ONLY"`).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Signal => "SIGNAL",
            Self::ScanOnly => "SCAN_ONLY",
        }
    }
}

/// Ports `baskfy_core.twt.config.EntryTiming` (`04` §5.1). One member, on purpose.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
pub enum EntryTiming {
    /// Next session's open, at market.
    #[default]
    #[serde(rename = "NEXT_OPEN")]
    NextOpen,
}

/// Ports `baskfy_core.twt.config.RankKey` (`04` §6.3, DECISIONS-TW TW0.2).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
pub enum RankKey {
    /// The signal session's own rupee turnover.
    #[default]
    #[serde(rename = "SIGNAL_TURNOVER")]
    SignalTurnover,
}

/// Ports `baskfy_core.twt.config.DataConfig` — universe, calendar and missing-bar tolerance
/// (`04` §1, §2).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct DataConfig {
    /// NSE cash equities.
    pub instrument_type: String,
    /// The series kept (BE/BZ stay in; SME series are out).
    pub series_allowed: Vec<String>,
    /// A null series (a delisted name) is kept.
    pub keep_null_series: bool,
    /// The `etf` index universe is the authority on ETFs.
    pub etf_universe_slug: String,
    /// Name pattern that catches an ETF missing from the universe.
    pub etf_name_pattern: String,
    /// Symbol pattern that catches an ETF missing from the universe.
    pub etf_symbol_pattern: String,
    /// The thin-session rule's share of the centred rolling median (`04` §2.1).
    pub thin_session_min_share: f64,
    /// The thin-session rule's centred window, in sessions.
    pub thin_session_window_bars: i64,
    /// The thin-session rule's minimum periods.
    pub thin_session_min_periods: i64,
    /// The missing-bar tolerance (`04` §2.2).
    pub rolling_min_share: f64,
    /// Sessions of history a full detection needs (`04` §2.3).
    pub bars_required: i64,
}

impl Default for DataConfig {
    fn default() -> Self {
        Self {
            instrument_type: "EQ".to_owned(),
            series_allowed: vec!["EQ".to_owned(), "BE".to_owned(), "BZ".to_owned()],
            keep_null_series: true,
            etf_universe_slug: "etf".to_owned(),
            etf_name_pattern: r"\bETF\b".to_owned(),
            etf_symbol_pattern: "(BEES|ETF|IETF)$".to_owned(),
            thin_session_min_share: 0.25,
            thin_session_window_bars: 41,
            thin_session_min_periods: 5,
            rolling_min_share: 0.90,
            bars_required: 260,
        }
    }
}

impl DataConfig {
    /// Ports `DataConfig.min_samples`: `max(2, round(window_bars x rolling_min_share))`, with
    /// Python's `round` (ties to even on the float product).
    #[must_use]
    pub fn min_samples(&self, window_bars: i64) -> i64 {
        min_samples(window_bars, self.rolling_min_share)
    }
}

/// `max(2, round(window_bars * share))` — Python's banker's `round` of the float product.
fn min_samples(window_bars: i64, share: f64) -> i64 {
    #[allow(clippy::cast_precision_loss)]
    let product = window_bars as f64 * share;
    let rounded = product.round_ties_even();
    // `round()` of a finite float is an int; a non-finite share is not a calibration anyone
    // writes, and saturating keeps it a number rather than a panic.
    #[allow(clippy::cast_possible_truncation)]
    let as_int = rounded as i64;
    as_int.max(2)
}

/// Ports `baskfy_core.twt.config.ScanConfig` — Chartink's five lines (`04` §3.1).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ScanConfig {
    /// Line 1: `close_raw > this` — the exchange print.
    pub min_close_raw: Decimal,
    /// Line 2: `close >= this x month_low_back`.
    pub month_low_multiple: Decimal,
    /// Line 2: how far back the monthly low is read, in calendar months.
    pub month_low_months_back: i64,
    /// Line 3: `(max(W) / min(W) - 1) x 100 <= this`.
    pub tight_band_pct: Decimal,
    /// Line 3: how many weekly closes are in `W`.
    pub tight_weeks: i64,
    /// Line 5: `vol_sma >= this`.
    pub min_vol_sma: Decimal,
    /// Line 5: the volume average's window, including the signal day.
    pub vol_sma_bars: i64,
}

impl Default for ScanConfig {
    fn default() -> Self {
        Self {
            min_close_raw: dec(300, 1),
            month_low_multiple: dec(13, 1),
            month_low_months_back: 3,
            tight_band_pct: dec(301, 2),
            tight_weeks: 3,
            min_vol_sma: dec(100_000, 1),
            vol_sma_bars: 50,
        }
    }
}

/// Ports `baskfy_core.twt.config.BreadthConfig` — the regime gate (`04` §4).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct BreadthConfig {
    /// The average each name is compared with, in sessions.
    pub dma_bars: i64,
    /// OPEN strictly above this percent, SHUT at or below it.
    pub min_pct_above_dma: f64,
    /// `pct_above_dma` is stored to this many decimal places (`04` §4.2).
    pub pct_decimals: i64,
}

impl Default for BreadthConfig {
    fn default() -> Self {
        Self {
            dma_bars: 200,
            min_pct_above_dma: 40.0,
            pct_decimals: 4,
        }
    }
}

/// Ports `baskfy_core.twt.config.EntryConfig` (`04` §3.4-3.5, §5.1, §6.3).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct EntryConfig {
    /// Next session's open, at market.
    pub entry: EntryTiming,
    /// The state was false on each of this many sessions before the event (`04` §3.4).
    pub entry_min_sessions_out: i64,
    /// The liquidity floor on the 20-session mean turnover (`04` §3.5, TW15.1).
    pub min_turnover_inr: Decimal,
    /// The research's own floor, for TW2's golden parameter set only.
    pub research_min_turnover_inr: Decimal,
    /// The window the floor and the 1 %-of-turnover cap read.
    pub turnover_avg_bars: i64,
    /// How a crowded session is ordered.
    pub rank_key: RankKey,
}

impl Default for EntryConfig {
    fn default() -> Self {
        Self {
            entry: EntryTiming::NextOpen,
            entry_min_sessions_out: 5,
            min_turnover_inr: dec(20_000_000, 0),
            research_min_turnover_inr: dec(20_000_000, 0),
            turnover_avg_bars: 20,
            rank_key: RankKey::SignalTurnover,
        }
    }
}

/// Ports `baskfy_core.twt.config.SizingConfig` — ten equal slots, every cap named (`04` §6).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct SizingConfig {
    /// Equal slots.
    pub max_slots: i64,
    /// LV10: a fresh setup in a held name is a new entry.
    pub pyramiding: bool,
    /// At most this many open entries in one name.
    pub max_entries_per_name: i64,
    /// The per-position ceiling, a percent of sleeve equity.
    pub max_position_pct: Decimal,
    /// A fraction of the name's 20-session average turnover.
    pub max_position_vs_turnover: Decimal,
    /// Below this a line is skipped `BELOW_MIN_TRADE_VALUE`.
    pub min_trade_value_inr: Decimal,
    /// Lines plus already-sent orders in one session.
    pub max_new_entries_per_session: i64,
    /// Half a slot while the first live entries are taken (`04` §6.4).
    pub risk_multiplier_first_live: Decimal,
    /// How many live entries the half size lasts.
    pub first_live_entries: i64,
}

impl Default for SizingConfig {
    fn default() -> Self {
        Self {
            max_slots: 10,
            pyramiding: true,
            max_entries_per_name: 2,
            max_position_pct: dec(125, 1),
            max_position_vs_turnover: dec(1, 2),
            min_trade_value_inr: dec(10_000, 0),
            max_new_entries_per_session: 3,
            risk_multiplier_first_live: dec(5, 1),
            first_live_entries: 10,
        }
    }
}

/// Ports `baskfy_core.twt.config.ExitConfig` — the two stops (`04` §7) and LV9's Qullamaggie
/// exits.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ExitConfig {
    /// The initial stop below the fill, percent (`04` §7.1).
    pub stop_pct: Decimal,
    /// The high-water trail, percent (`04` §7.2).
    pub trail_pct: Decimal,
    /// The clamp under the close (`04` §7.2).
    pub close_clamp_fraction: Decimal,
    /// The clamp's fallback when the raw trigger already sits at or above the close.
    pub close_clamp_fallback: Decimal,
    /// A position with no bar for this many sessions is closed at its last close (`04` §7.5).
    pub no_bar_sessions: i64,
    /// The resting GTT limit as a fraction of its trigger (`04` §10.6).
    pub gtt_limit_fraction: Decimal,
    /// The GTT band's floor (`04` §10.7, TW0.8).
    pub gtt_band_min_pct: Decimal,
    /// The GTT band's ceiling.
    pub gtt_band_max_pct: Decimal,
    /// LV9: Qullamaggie's exits over the swing book's `manage`.
    pub qulla_exits: bool,
    /// The swing book's stop configuration the Qullamaggie exits read.
    pub qulla: StopConfig,
}

impl Default for ExitConfig {
    fn default() -> Self {
        Self {
            stop_pct: dec(200, 1),
            trail_pct: dec(200, 1),
            close_clamp_fraction: dec(9999, 4),
            close_clamp_fallback: dec(999, 3),
            no_bar_sessions: 5,
            gtt_limit_fraction: dec(97, 2),
            gtt_band_min_pct: dec(5, 3),
            gtt_band_max_pct: dec(30, 2),
            qulla_exits: true,
            qulla: StopConfig::default(),
        }
    }
}

/// Ports `baskfy_core.twt.config.CostConfig` (`04` §8).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct CostConfig {
    /// Charged on both sides in the backtest.
    pub cost_bps_per_side: Decimal,
}

impl Default for CostConfig {
    fn default() -> Self {
        Self {
            cost_bps_per_side: dec(250, 1),
        }
    }
}

/// Ports `baskfy_core.twt.config.BacktestConfig` (`04` §12).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct BacktestConfig {
    /// Starting capital.
    pub initial_capital_inr: Decimal,
    /// First session of the run.
    pub start: NaiveDate,
    /// In-sample / out-of-sample split.
    pub is_oos_split: NaiveDate,
}

impl Default for BacktestConfig {
    fn default() -> Self {
        Self {
            initial_capital_inr: dec(1_000_000, 0),
            start: NaiveDate::from_ymd_opt(2017, 10, 16).unwrap_or_default(),
            is_oos_split: NaiveDate::from_ymd_opt(2023, 1, 1).unwrap_or_default(),
        }
    }
}

/// Ports `baskfy_core.twt.config.TwtConfig` — everything the sleeve can be recalibrated on.
/// `TwtConfig::default()` is Python's `DEFAULT_TWT_CONFIG`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct TwtConfig {
    /// Universe, calendar, missing bars.
    pub data: DataConfig,
    /// Chartink's lines.
    pub scan: ScanConfig,
    /// The regime gate.
    pub breadth: BreadthConfig,
    /// The entry event and the floor.
    pub entry: EntryConfig,
    /// Slots and caps.
    pub sizing: SizingConfig,
    /// Stops.
    pub exits: ExitConfig,
    /// Backtest costs.
    pub costs: CostConfig,
    /// Backtest parameters.
    pub backtest: BacktestConfig,
}

impl TwtConfig {
    /// Ports `TwtConfig.deepest_window_bars`: the longest rolling window the sleeve reads.
    #[must_use]
    pub fn deepest_window_bars(&self) -> i64 {
        [
            self.breadth.dma_bars,
            self.scan.vol_sma_bars,
            self.entry.turnover_avg_bars,
            self.entry.entry_min_sessions_out,
            self.exits.no_bar_sessions,
        ]
        .into_iter()
        .max()
        .unwrap_or(0)
    }
}

/// Ports `TICK_INR`: the exchange's tick for NSE cash equities.
pub const TICK_INR: &str = "0.05";

/// Ports `RESEARCH_TICK_INR`: the research simulator's paisa tick (TW2's golden set only).
pub const RESEARCH_TICK_INR: &str = "0.01";

/// Ports `DRY_RUN_SESSIONS_REQUIRED`: zero — there is no paper phase (`docs/twt/02` §3).
pub const DRY_RUN_SESSIONS_REQUIRED: i64 = 0;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decimal_defaults_keep_python_scale() {
        let c = TwtConfig::default();
        assert_eq!(c.scan.min_close_raw.to_string(), "30.0");
        assert_eq!(c.scan.tight_band_pct.to_string(), "3.01");
        assert_eq!(c.scan.min_vol_sma.to_string(), "10000.0");
        assert_eq!(c.sizing.max_position_vs_turnover.to_string(), "0.01");
        assert_eq!(c.exits.gtt_band_max_pct.to_string(), "0.30");
        assert_eq!(c.exits.close_clamp_fraction.to_string(), "0.9999");
        assert_eq!(c.entry.min_turnover_inr.to_string(), "20000000");
        assert_eq!(c.deepest_window_bars(), 200);
        assert_eq!(c.data.min_samples(50), 45);
        assert_eq!(c.data.min_samples(1), 2);
    }
}
