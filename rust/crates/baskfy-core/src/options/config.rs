//! Port of `baskfy_core.options.config` (`decile-blueprint/packages/core/src/baskfy_core/options/config.py`).
//!
//! Every threshold of the options run, named once (docs/options/04-business-rules.md).
//!
//! Goldens: none directly; `OptionsConfig` and its rate tables arrive as inputs (e.g. `rates`) of the options goldens.
//!
//! Ported so far: the parts the exits read — `CalendarConfig`, `ChainConfig`,
//! `DirectionalConfig`, `ExpirySetupsConfig` and an `OptionsConfig` holding them with the two
//! condor instances, the execution mechanics and the risk limits, and `hard_exit_time`. The
//! cost/sizing tables and the ceilings are not yet ported here (`CostRates` lives in
//! [`crate::options::costs`]). `CondorConfig`, `RiskConfig` and `ExecutionConfig` stay where
//! wave 1 put them ([`crate::options::condor`], [`crate::options::execution`]).

use chrono::NaiveTime;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::options::condor::{CondorConfig, RiskConfig};
use crate::options::execution::ExecutionConfig;
use crate::options::gating::{Sleeve, SleeveGroup, group_of};

fn hm(h: u32, m: u32) -> NaiveTime {
    // Every caller passes a constant valid wall-clock time.
    NaiveTime::from_hms_opt(h, m, 0).unwrap_or_default()
}

/// `04` §1 and the session clock every sleeve shares.
///
/// Ports `baskfy_core.options.config.CalendarConfig` (NIFTY only, PACK.12).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CalendarConfig {
    /// The underlying (`NIFTY`).
    pub underlying: String,
    /// The session's open.
    pub market_open: NaiveTime,
    /// The settlement moment of an expiring contract.
    pub market_close: NaiveTime,
}

impl Default for CalendarConfig {
    fn default() -> Self {
        Self {
            underlying: "NIFTY".to_owned(),
            market_open: hm(9, 15),
            market_close: hm(15, 30),
        }
    }
}

/// `04` §2's staleness thresholds (the chain-reading fields the exits do not read are not
/// ported here).
///
/// Ports `baskfy_core.options.config.ChainConfig` (`stale_*`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ChainConfig {
    /// A leg quote older than this is stale (§8.5).
    pub stale_quote_seconds: i64,
    /// No index tick for this long is a silent feed (§8.5).
    pub stale_index_seconds: i64,
    /// A scan older than this is stale.
    pub stale_scan_seconds: i64,
}

impl Default for ChainConfig {
    fn default() -> Self {
        Self {
            stale_quote_seconds: 15,
            stale_index_seconds: 30,
            stale_scan_seconds: 120,
        }
    }
}

/// O2 (`04` §4; PACK.5).
///
/// Ports `baskfy_core.options.config.DirectionalConfig`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct DirectionalConfig {
    /// Trend EMA, days.
    pub trend_ema_days: i64,
    /// Largest opening gap, percent.
    pub gap_max_pct: Decimal,
    /// Opening range start.
    pub opening_range_start: NaiveTime,
    /// Opening range end.
    pub opening_range_end: NaiveTime,
    /// Bars in a complete opening range.
    pub opening_range_bars: i64,
    /// Widest opening range, percent.
    pub or_max_pct: Decimal,
    /// Highest India VIX.
    pub vix_max: Decimal,
    /// The invalidation bar's width, minutes.
    pub bar_minutes: i64,
    /// Entry window start.
    pub entry_window_start: NaiveTime,
    /// Entry window end.
    pub entry_window_end: NaiveTime,
    /// Breakout buffer, percent.
    pub buffer_pct: Decimal,
    /// Strikes in the money.
    pub itm_steps: i64,
    /// `|delta|` floor.
    pub delta_min: Decimal,
    /// `|delta|` ceiling.
    pub delta_max: Decimal,
    /// Stop at `bid <= E * (1 - stop_frac)`.
    pub stop_frac: Decimal,
    /// Target at `bid >= E * (1 + target_frac)`.
    pub target_frac: Decimal,
    /// The time stop, minutes held.
    pub time_stop_minutes: i64,
    /// The time stop's minimum gain.
    pub time_stop_min_gain: Decimal,
    /// The hard exit.
    pub hard_exit_time: NaiveTime,
    /// Reserve per lot, ₹.
    pub reserve_per_lot_inr: Decimal,
    /// Premium cap, percent.
    pub premium_cap_pct: Decimal,
    /// Largest cost share.
    pub cost_share_max: Decimal,
    /// Risk per trade, percent.
    pub risk_per_trade_pct: Decimal,
    /// Lot cap.
    pub max_lots: i64,
    /// Sessions before tier 3.
    pub tier3_min_sessions: i64,
}

impl Default for DirectionalConfig {
    fn default() -> Self {
        Self {
            trend_ema_days: 20,
            gap_max_pct: Decimal::new(10, 1),
            opening_range_start: hm(9, 15),
            opening_range_end: hm(9, 29),
            opening_range_bars: 15,
            or_max_pct: Decimal::new(90, 2),
            vix_max: Decimal::new(22, 0),
            bar_minutes: 5,
            entry_window_start: hm(9, 30),
            entry_window_end: hm(13, 30),
            buffer_pct: Decimal::new(5, 2),
            itm_steps: 1,
            delta_min: Decimal::new(50, 2),
            delta_max: Decimal::new(75, 2),
            stop_frac: Decimal::new(30, 2),
            target_frac: Decimal::new(60, 2),
            time_stop_minutes: 45,
            time_stop_min_gain: Decimal::new(10, 2),
            hard_exit_time: hm(15, 0),
            reserve_per_lot_inr: Decimal::new(300, 0),
            premium_cap_pct: Decimal::new(10, 0),
            cost_share_max: Decimal::new(15, 2),
            risk_per_trade_pct: Decimal::new(5, 1),
            max_lots: 2,
            tier3_min_sessions: 60,
        }
    }
}

/// O3 (`04` §5; PACK.10).
///
/// Ports `baskfy_core.options.config.ExpirySetupsConfig`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ExpirySetupsConfig {
    /// O3-A on.
    pub o3a_enabled: bool,
    /// O3-B on.
    pub o3b_enabled: bool,
    /// The spread's width, points.
    pub width_points: Decimal,
    /// Largest debit as a fraction of the width.
    pub max_debit_frac: Decimal,
    /// O3-A range start.
    pub o3a_range_start: NaiveTime,
    /// O3-A range end.
    pub o3a_range_end: NaiveTime,
    /// O3-A range bars.
    pub o3a_range_bars: i64,
    /// O3-A widest range, percent.
    pub o3a_range_max_pct: Decimal,
    /// O3-A window start.
    pub o3a_window_start: NaiveTime,
    /// O3-A window end.
    pub o3a_window_end: NaiveTime,
    /// O3-A last confirm.
    pub o3a_entry_window_end: NaiveTime,
    /// O3-A buffer, percent.
    pub o3a_buffer_pct: Decimal,
    /// O3-A ER floor.
    pub o3a_er_min: Decimal,
    /// O3-B smallest gap, percent.
    pub o3b_gap_min_pct: Decimal,
    /// O3-B largest gap, percent.
    pub o3b_gap_max_pct: Decimal,
    /// O3-B hold end.
    pub o3b_hold_end: NaiveTime,
    /// O3-B plan time.
    pub o3b_plan_time: NaiveTime,
    /// O3-B window end.
    pub o3b_window_end: NaiveTime,
    /// The invalidation bar's width, minutes.
    pub bar_minutes: i64,
    /// Target at `V >= target_frac_of_width * width`.
    pub target_frac_of_width: Decimal,
    /// Stop at `V <= stop_frac_of_debit * D`.
    pub stop_frac_of_debit: Decimal,
    /// The hard exit.
    pub hard_exit_time: NaiveTime,
    /// Reserve per lot, ₹.
    pub reserve_per_lot_inr: Decimal,
    /// Largest cost share.
    pub cost_share_max: Decimal,
    /// Risk per trade, percent.
    pub risk_per_trade_pct: Decimal,
    /// Lot cap.
    pub max_lots: i64,
    /// Sessions before tier 3.
    pub tier3_min_sessions: i64,
}

impl Default for ExpirySetupsConfig {
    fn default() -> Self {
        Self {
            o3a_enabled: true,
            o3b_enabled: true,
            width_points: Decimal::new(100, 0),
            max_debit_frac: Decimal::new(55, 2),
            o3a_range_start: hm(9, 15),
            o3a_range_end: hm(10, 14),
            o3a_range_bars: 60,
            o3a_range_max_pct: Decimal::new(120, 2),
            o3a_window_start: hm(10, 19),
            o3a_window_end: hm(13, 0),
            o3a_entry_window_end: hm(13, 30),
            o3a_buffer_pct: Decimal::new(5, 2),
            o3a_er_min: Decimal::new(40, 2),
            o3b_gap_min_pct: Decimal::new(50, 2),
            o3b_gap_max_pct: Decimal::new(150, 2),
            o3b_hold_end: hm(9, 44),
            o3b_plan_time: hm(9, 45),
            o3b_window_end: hm(10, 0),
            bar_minutes: 5,
            target_frac_of_width: Decimal::new(80, 2),
            stop_frac_of_debit: Decimal::new(50, 2),
            hard_exit_time: hm(14, 45),
            reserve_per_lot_inr: Decimal::new(500, 0),
            cost_share_max: Decimal::new(20, 2),
            risk_per_trade_pct: Decimal::new(5, 1),
            max_lots: 2,
            tier3_min_sessions: 20,
        }
    }
}

/// The whole contract, as far as it is ported. `condor_monthly` and `condor_weekly` are
/// distinct instances.
///
/// Ports `baskfy_core.options.config.OptionsConfig` (without `costs` and `sizing`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct OptionsConfig {
    /// The session clock.
    pub calendar: CalendarConfig,
    /// Staleness.
    pub chain: ChainConfig,
    /// O1-M.
    pub condor_monthly: CondorConfig,
    /// O1-W.
    pub condor_weekly: CondorConfig,
    /// O2.
    pub directional: DirectionalConfig,
    /// O3.
    pub expiry_setups: ExpirySetupsConfig,
    /// `04` §8's mechanics.
    pub execution: ExecutionConfig,
    /// `04` §9's limits.
    pub risk: RiskConfig,
}

impl Default for OptionsConfig {
    fn default() -> Self {
        Self {
            calendar: CalendarConfig::default(),
            chain: ChainConfig::default(),
            condor_monthly: CondorConfig::default(),
            condor_weekly: CondorConfig::weekly(),
            directional: DirectionalConfig::default(),
            expiry_setups: ExpirySetupsConfig::default(),
            execution: ExecutionConfig::default(),
            risk: RiskConfig::default(),
        }
    }
}

impl OptionsConfig {
    /// `hard_exit_time(sleeve)`: each sleeve's hard exit (`04` §3.2, §4.5, §5.3).
    #[must_use]
    pub fn hard_exit_time(&self, sleeve: Sleeve) -> NaiveTime {
        match group_of(sleeve) {
            SleeveGroup::O1m => self.condor_monthly.hard_exit_time,
            SleeveGroup::O1w => self.condor_weekly.hard_exit_time,
            SleeveGroup::O2 => self.directional.hard_exit_time,
            SleeveGroup::O3 => self.expiry_setups.hard_exit_time,
        }
    }
}
