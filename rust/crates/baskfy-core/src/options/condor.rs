//! Port of `baskfy_core.options.condor` (`decile-blueprint/packages/core/src/baskfy_core/options/condor.py`).
//!
//! O1 — the hedged condor on an expiry day, monthly and weekly (docs/options/04 §3).
//!
//! Goldens: `L1/options/exit_decision.*` (`baskfy_core.options.condor.exit_decision`).
//!
//! Ported so far: the exits (condor §7) — profit at `D ≤ profit_take_frac * C`, stop at
//! `D ≥ stop_frac * C`, a touch of either short strike, the hard exit, and `04` §9.2's budget
//! breach, one decision by `HARD_EXIT > STRIKE_TOUCH > STOP > PROFIT`; profit is never taken on
//! a stale mark (§7.7). The gate (`observe`) and the structure (`build`) are not yet ported.
//!
//! `CondorConfig`, `CondorVariant` and `RiskConfig` belong to `baskfy_core.options.config`, and
//! `trade_breached` to `baskfy_core.options.risk`; they live here until those modules are
//! ported (outside this port's ownership).

use chrono::{DateTime, FixedOffset, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::costs::mul;
use crate::options::execution::{drop_on_stale, first_by_precedence};

/// Ports `baskfy_core.options.config.CondorVariant`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum CondorVariant {
    /// O1-M.
    Monthly,
    /// O1-W.
    Weekly,
}

/// O1 (`04` §3; condor `04` §2, §4, §5.3, §6, §7, §9). Two instances, never shared.
///
/// Ports `baskfy_core.options.config.CondorConfig`; [`Default`] is `condor_monthly`,
/// [`CondorConfig::weekly`] is `condor_weekly`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CondorConfig {
    /// Monthly or weekly.
    pub variant: CondorVariant,
    /// Gate: largest opening gap, percent.
    pub gap_max_pct: Decimal,
    /// Gate: widest observation range, percent.
    pub range_max_pct: Decimal,
    /// Gate: highest Kaufman efficiency ratio.
    pub er_max: Decimal,
    /// Observation window start.
    pub observation_start: NaiveTime,
    /// Observation window end.
    pub observation_end: NaiveTime,
    /// Opening range end.
    pub opening_range_end: NaiveTime,
    /// Bars a complete observation window has.
    pub min_bars: i64,
    /// Short-strike `|delta|` floor.
    pub delta_min: Decimal,
    /// Short-strike `|delta|` ceiling.
    pub delta_max: Decimal,
    /// Short-strike `|delta|` target.
    pub delta_target: Decimal,
    /// Points beyond the opening range a short must sit.
    pub strike_buffer_points: Decimal,
    /// Wing width, points.
    pub wing_width_points: Decimal,
    /// Credit floor as a fraction of the width.
    pub credit_floor_frac: Decimal,
    /// Credit target as a fraction of the width.
    pub credit_target_frac: Decimal,
    /// Profit at `D ≤ profit_take_frac * C`.
    pub profit_take_frac: Decimal,
    /// Stop at `D ≥ stop_frac * C`.
    pub stop_frac: Decimal,
    /// When the plan is built.
    pub plan_time: NaiveTime,
    /// Last moment a plan may be confirmed.
    pub entry_window_end: NaiveTime,
    /// The hard exit.
    pub hard_exit_time: NaiveTime,
    /// Largest cost share of the expected gain.
    pub cost_share_max: Decimal,
    /// Risk per trade, percent of capital.
    pub risk_per_trade_pct: Decimal,
    /// Lot cap.
    pub max_lots: i64,
    /// Reserve per lot, ₹.
    pub reserve_per_lot_inr: Decimal,
    /// Sessions before tier 3.
    pub tier3_min_sessions: i64,
}

fn hm(h: u32, m: u32) -> NaiveTime {
    // Every caller passes a constant valid wall-clock time.
    NaiveTime::from_hms_opt(h, m, 0).unwrap_or_default()
}

impl Default for CondorConfig {
    fn default() -> Self {
        Self {
            variant: CondorVariant::Monthly,
            gap_max_pct: Decimal::new(75, 2),
            range_max_pct: Decimal::new(80, 2),
            er_max: Decimal::new(30, 2),
            observation_start: hm(9, 15),
            observation_end: hm(9, 59),
            opening_range_end: hm(9, 44),
            min_bars: 45,
            delta_min: Decimal::new(20, 2),
            delta_max: Decimal::new(25, 2),
            delta_target: Decimal::new(22, 2),
            strike_buffer_points: Decimal::new(0, 0),
            wing_width_points: Decimal::new(150, 0),
            credit_floor_frac: Decimal::new(25, 2),
            credit_target_frac: Decimal::new(30, 2),
            profit_take_frac: Decimal::new(50, 2),
            stop_frac: Decimal::new(150, 2),
            plan_time: hm(10, 0),
            entry_window_end: hm(10, 15),
            hard_exit_time: hm(14, 30),
            cost_share_max: Decimal::new(20, 2),
            risk_per_trade_pct: Decimal::new(10, 1),
            max_lots: 3,
            reserve_per_lot_inr: Decimal::new(1000, 0),
            tier3_min_sessions: 12,
        }
    }
}

impl CondorConfig {
    /// `OptionsConfig().condor_weekly`: the monthly defaults with the weekly variant, 0.5 %
    /// risk, two lots and twenty tier-3 sessions.
    pub fn weekly() -> Self {
        Self {
            variant: CondorVariant::Weekly,
            risk_per_trade_pct: Decimal::new(5, 1),
            max_lots: 2,
            tier3_min_sessions: 20,
            ..Self::default()
        }
    }

    /// The `OptionsConfig()` default instance for a variant.
    pub fn for_variant(variant: CondorVariant) -> Self {
        match variant {
            CondorVariant::Monthly => Self::default(),
            CondorVariant::Weekly => Self::weekly(),
        }
    }
}

/// `04` §9 — per-sleeve limits in R, per-trade breach, the book's derived ₹ limits.
///
/// Ports `baskfy_core.options.config.RiskConfig`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct RiskConfig {
    /// Daily loss limit, R.
    pub daily_loss_r: Decimal,
    /// Weekly loss limit, R.
    pub weekly_loss_r: Decimal,
    /// Monthly loss limit, R.
    pub monthly_loss_r: Decimal,
    /// A marked loss beyond `risk_budget * budget_breach_frac` closes the position.
    pub budget_breach_frac: Decimal,
    /// Book daily loss, percent.
    pub book_daily_loss_pct: Decimal,
    /// Book monthly loss, percent.
    pub book_monthly_loss_pct: Decimal,
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            daily_loss_r: Decimal::new(2, 0),
            weekly_loss_r: Decimal::new(4, 0),
            monthly_loss_r: Decimal::new(8, 0),
            budget_breach_frac: Decimal::new(10, 1),
            book_daily_loss_pct: Decimal::new(15, 1),
            book_monthly_loss_pct: Decimal::new(5, 0),
        }
    }
}

/// `04` §9.2: a marked loss beyond `risk_budget * budget_breach_frac` closes the position
/// (`STOP`) whatever the sleeve's own rule says. `marked_loss_inr` is a positive loss.
///
/// Ports `baskfy_core.options.risk.trade_breached`.
pub fn trade_breached(
    marked_loss_inr: Decimal,
    risk_budget_inr: Decimal,
    config: &RiskConfig,
) -> CoreResult<bool> {
    Ok(marked_loss_inr > mul(risk_budget_inr, config.budget_breach_frac)?)
}

/// Ports `baskfy_core.options.condor.CondorExit`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CondorExit {
    /// The clock.
    HardExit,
    /// Spot touched a short strike.
    StrikeTouch,
    /// `D ≥ stop_frac * C`, or the budget breach.
    Stop,
    /// `D ≤ profit_take_frac * C`.
    Profit,
    /// The operator's button.
    Manual,
}

/// Condor §7.6; `MANUAL` is the operator's button and outranks nothing automatic.
pub const PRECEDENCE: [CondorExit; 5] = [
    CondorExit::HardExit,
    CondorExit::StrikeTouch,
    CondorExit::Stop,
    CondorExit::Profit,
    CondorExit::Manual,
];

/// One tick's marks — the keyword arguments of Python's `exit_decision` other than the two
/// configs, as one value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CondorTick {
    /// C, the entry credit from fills (§4.8), points.
    pub entry_credit: Decimal,
    /// D, the cost to close now, points.
    pub cost_now: Decimal,
    /// The index level; `None` when no index tick has come yet (OP13.2).
    pub spot: Option<Decimal>,
    /// The short call's strike.
    pub short_call_strike: Decimal,
    /// The short put's strike.
    pub short_put_strike: Decimal,
    /// The tick's time; converted to IST wall clock before comparing.
    pub now: DateTime<FixedOffset>,
    /// Whether the mark is stale (§7.7).
    pub stale: bool,
    /// The marked loss, ₹, a loss positive.
    pub marked_loss_inr: Decimal,
    /// The trade's risk budget, ₹.
    pub risk_budget_inr: Decimal,
    /// The operator pressed close.
    #[serde(default)]
    pub manual: bool,
}

/// `now` as naive IST wall-clock (Python's `bars.ist`).
fn ist_time(now: &DateTime<FixedOffset>) -> CoreResult<NaiveTime> {
    let ist = FixedOffset::east_opt(5 * 3600 + 30 * 60)
        .ok_or_else(|| CoreError::InvalidInput("IST offset".to_string()))?;
    Ok(now.with_timezone(&ist).time())
}

/// One tick's exit, or `None`. `entry_credit` is C from fills (§4.8), `cost_now` is D.
///
/// `04` §9.2's budget breach closes as `STOP` whatever D/C says; a stale mark drops only
/// `PROFIT` (§7.7). An unknown `spot` cannot touch a strike, so only `STRIKE_TOUCH` is not
/// judged; every premium rule and the clock still are (DECISIONS-OP OP13.2).
///
/// Ports `baskfy_core.options.condor.exit_decision` (`options` narrowed to the `RiskConfig`
/// it reads).
pub fn exit_decision(
    tick: &CondorTick,
    config: &CondorConfig,
    risk: &RiskConfig,
) -> CoreResult<Option<CondorExit>> {
    let mut fired: Vec<CondorExit> = Vec::new();
    if ist_time(&tick.now)? >= config.hard_exit_time {
        fired.push(CondorExit::HardExit);
    }
    if let Some(spot) = tick.spot
        && (spot >= tick.short_call_strike || spot <= tick.short_put_strike)
    {
        fired.push(CondorExit::StrikeTouch);
    }
    if tick.cost_now >= mul(config.stop_frac, tick.entry_credit)?
        || trade_breached(tick.marked_loss_inr, tick.risk_budget_inr, risk)?
    {
        fired.push(CondorExit::Stop);
    }
    if tick.cost_now <= mul(config.profit_take_frac, tick.entry_credit)? {
        fired.push(CondorExit::Profit);
    }
    if tick.manual {
        fired.push(CondorExit::Manual);
    }
    let kept = drop_on_stale(&fired, &[CondorExit::Profit], tick.stale);
    first_by_precedence(&kept, &PRECEDENCE)
}
