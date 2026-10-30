//! Port of `baskfy_core.options.backtest_suite`: a sleeve's runs by tier, and Tier 1's ±25 % sensitivity (`06` OP12).

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::backtest::{BacktestResult, DayOutcome, Tier, run};
use crate::options::config::{OptionsCeilings, OptionsConfig, Sleeve};
use crate::options::num;
use crate::options::replay_day::{SnapshotSource, priced_day, tier1_day};
use crate::options::scan::{DayContext, MarketDay};

/// The gate thresholds each sleeve's Tier 1 sensitivity varies, as (config group, field).
pub fn thresholds(sleeve: Sleeve) -> &'static [(&'static str, &'static str)] {
    match sleeve {
        Sleeve::O1m => &[
            ("condor_monthly", "gap_max_pct"),
            ("condor_monthly", "range_max_pct"),
            ("condor_monthly", "er_max"),
        ],
        Sleeve::O1w => &[
            ("condor_weekly", "gap_max_pct"),
            ("condor_weekly", "range_max_pct"),
            ("condor_weekly", "er_max"),
        ],
        Sleeve::O2 => &[
            ("directional", "gap_max_pct"),
            ("directional", "or_max_pct"),
            ("directional", "vix_max"),
            ("directional", "buffer_pct"),
        ],
        Sleeve::O3a => &[
            ("expiry_setups", "o3a_range_max_pct"),
            ("expiry_setups", "o3a_buffer_pct"),
            ("expiry_setups", "o3a_er_min"),
        ],
        Sleeve::O3b => &[
            ("expiry_setups", "o3b_gap_min_pct"),
            ("expiry_setups", "o3b_gap_max_pct"),
        ],
    }
}

/// x0.75 and x1.25.
pub fn sensitivity_factors() -> [Decimal; 2] {
    [Decimal::new(75, 2), Decimal::new(125, 2)]
}

/// `04` §13.3's `tier3_min_sessions` for the sleeve's config group.
pub fn min_sessions(sleeve: Sleeve, options: &OptionsConfig) -> i64 {
    match sleeve {
        Sleeve::O1m => options.condor_monthly.tier3_min_sessions,
        Sleeve::O1w => options.condor_weekly.tier3_min_sessions,
        Sleeve::O2 => options.directional.tier3_min_sessions,
        Sleeve::O3a | Sleeve::O3b => options.expiry_setups.tier3_min_sessions,
    }
}

/// Python's `source_for`: a snapshot source per day.
pub type SourceFor<'a> = &'a mut dyn FnMut(&MarketDay) -> Box<dyn SnapshotSource>;

/// One sleeve, one tier, over `days` (the sleeve's non-days are dropped, never counted).
pub fn run_tier(
    sleeve: Sleeve,
    tier: Tier,
    days: &[MarketDay],
    context: &DayContext,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
    mut source_for: Option<SourceFor<'_>>,
) -> CoreResult<BacktestResult> {
    let mut outcomes: Vec<DayOutcome> = Vec::new();
    for market in days {
        let outcome = if tier == Tier::Signals {
            tier1_day(sleeve, market, context, options)?
        } else {
            let make = source_for.as_mut().ok_or_else(|| {
                CoreError::InvalidInput(format!("Tier {} needs a snapshot source", tier.value()))
            })?;
            let mut source = make(market);
            priced_day(sleeve, market, context, source.as_mut(), options, ceilings)?
        };
        outcomes.extend(outcome);
    }
    run(sleeve, tier, outcomes, |o| o, min_sessions(sleeve, options))
}

/// One threshold at one factor, and Tier 1's count there.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Sensitivity {
    /// The config group.
    pub group: String,
    /// The field.
    pub field: String,
    /// The factor.
    pub factor: Decimal,
    /// The varied value.
    pub value: Decimal,
    /// Sessions.
    pub sessions: i64,
    /// Signals.
    pub signals: i64,
}

fn threshold_mut<'a>(
    options: &'a mut OptionsConfig,
    group: &str,
    field: &str,
) -> CoreResult<&'a mut Decimal> {
    let slot = match (group, field) {
        ("condor_monthly", "gap_max_pct") => &mut options.condor_monthly.gap_max_pct,
        ("condor_monthly", "range_max_pct") => &mut options.condor_monthly.range_max_pct,
        ("condor_monthly", "er_max") => &mut options.condor_monthly.er_max,
        ("condor_weekly", "gap_max_pct") => &mut options.condor_weekly.gap_max_pct,
        ("condor_weekly", "range_max_pct") => &mut options.condor_weekly.range_max_pct,
        ("condor_weekly", "er_max") => &mut options.condor_weekly.er_max,
        ("directional", "gap_max_pct") => &mut options.directional.gap_max_pct,
        ("directional", "or_max_pct") => &mut options.directional.or_max_pct,
        ("directional", "vix_max") => &mut options.directional.vix_max,
        ("directional", "buffer_pct") => &mut options.directional.buffer_pct,
        ("expiry_setups", "o3a_range_max_pct") => &mut options.expiry_setups.o3a_range_max_pct,
        ("expiry_setups", "o3a_buffer_pct") => &mut options.expiry_setups.o3a_buffer_pct,
        ("expiry_setups", "o3a_er_min") => &mut options.expiry_setups.o3a_er_min,
        ("expiry_setups", "o3b_gap_min_pct") => &mut options.expiry_setups.o3b_gap_min_pct,
        ("expiry_setups", "o3b_gap_max_pct") => &mut options.expiry_setups.o3b_gap_max_pct,
        _ => {
            return Err(CoreError::InvalidInput(format!(
                "no threshold {group}.{field}"
            )));
        }
    };
    Ok(slot)
}

/// Tier 1's signal count with each threshold of the sleeve's gate at x0.75 and x1.25.
pub fn tier1_sensitivity(
    sleeve: Sleeve,
    days: &[MarketDay],
    context: &DayContext,
    options: &OptionsConfig,
) -> CoreResult<Vec<Sensitivity>> {
    let ceilings = OptionsCeilings::default();
    let mut out = Vec::new();
    for (group, field) in thresholds(sleeve) {
        for factor in sensitivity_factors() {
            let mut varied = options.clone();
            let slot = threshold_mut(&mut varied, group, field)?;
            *slot = num::mul(*slot, factor)?;
            let value = *slot;
            let result = run_tier(
                sleeve,
                Tier::Signals,
                days,
                context,
                &varied,
                &ceilings,
                None,
            )?;
            out.push(Sensitivity {
                group: (*group).to_owned(),
                field: (*field).to_owned(),
                factor,
                value,
                sessions: result.sessions,
                signals: result.traded,
            });
        }
    }
    Ok(out)
}
