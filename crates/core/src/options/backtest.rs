//! Port of `baskfy_core.options.backtest`: one sleeve, one tier, one row (`04` §13; `07` §4).

use std::collections::BTreeMap;

use chrono::{Datelike, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::WideDecimal;
use crate::error::{CoreError, CoreResult};
use crate::options::config::{CostRates, OptionType, Sleeve, SleeveGroup, group_of};
use crate::options::greeks::black76_price;
use crate::options::journal::max_drawdown;
use crate::options::num;
use crate::py;

/// A backtest tier.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Tier {
    /// Tier 1: signals only.
    #[serde(rename = "1")]
    Signals,
    /// Tier 2: modelled prices.
    #[serde(rename = "2")]
    Modelled,
    /// Tier 3: observed prices.
    #[serde(rename = "3")]
    Observed,
}

impl Tier {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            Tier::Signals => "1",
            Tier::Modelled => "2",
            Tier::Observed => "3",
        }
    }
}

/// `07` §4, Tier 1's "must say".
pub const TIER1_CAVEAT: &str = "No P&L. This measures selectivity, not profitability.";
/// Condor `07` §4, verbatim — Tier 2 for every sleeve.
pub const TIER2_CAVEAT: &str = "Prices are modelled, not observed. Real expiry-day premiums, skew and slippage differ from a flat-VIX Black\u{2013}Scholes; treat the P&L as a shape, not a number.";
/// `04` §13.2's extra sentence for O2, verbatim.
pub const TIER2_O2_CAVEAT: &str = "A bought option's model price ignores intraday IV changes; real premiums often fall after the open and on a move's reversal. Expect Tier 2 to flatter O2.";

/// The text a run's row stores and its card renders (`07` §4).
pub fn caveats(
    sleeve: Sleeve,
    tier: Tier,
    observed_sessions: i64,
    min_sessions: i64,
) -> Vec<String> {
    match tier {
        Tier::Signals => vec![TIER1_CAVEAT.to_owned()],
        Tier::Modelled if group_of(sleeve) == SleeveGroup::O2 => {
            vec![TIER2_CAVEAT.to_owned(), TIER2_O2_CAVEAT.to_owned()]
        }
        Tier::Modelled => vec![TIER2_CAVEAT.to_owned()],
        Tier::Observed => {
            let sample = format!("Observed sample: {observed_sessions} sessions.");
            match sample_banner(observed_sessions, min_sessions) {
                Some(banner) => vec![sample, banner],
                None => vec![sample],
            }
        }
    }
}

/// Tier 3's "insufficient sample" banner below `tier3_min_sessions` (§13.3).
pub fn sample_banner(observed_sessions: i64, min_sessions: i64) -> Option<String> {
    (observed_sessions < min_sessions).then(|| {
        format!("Insufficient sample: {observed_sessions} of {min_sessions} observed sessions.")
    })
}

/// What a sleeve's day function returns for one session.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DayOutcome {
    /// The session.
    pub trade_date: NaiveDate,
    /// Traded (a signal at Tier 1).
    pub traded: bool,
    /// Why a skipped day skipped.
    #[serde(default)]
    pub skip_reason: Option<String>,
    /// Net ₹ (Tier 2 up).
    #[serde(default)]
    pub net_pnl_inr: Option<Decimal>,
    /// R (Tier 2 up).
    #[serde(default)]
    pub r_multiple: Option<Decimal>,
    /// The exit code.
    #[serde(default)]
    pub closed_reason: Option<String>,
    /// When the trigger fired.
    #[serde(default)]
    pub trigger_time: Option<NaiveTime>,
    /// The index's best move after the trigger, points.
    #[serde(default)]
    pub mfe_points: Option<Decimal>,
    /// The index's worst move after the trigger, points.
    #[serde(default)]
    pub mae_points: Option<Decimal>,
}

impl DayOutcome {
    /// A skipped session.
    pub fn skipped(trade_date: NaiveDate, reason: &str) -> Self {
        Self {
            skip_reason: Some(reason.to_owned()),
            ..Self::bare(trade_date, false)
        }
    }

    /// A session with every optional field empty.
    pub fn bare(trade_date: NaiveDate, traded: bool) -> Self {
        Self {
            trade_date,
            traded,
            skip_reason: None,
            net_pnl_inr: None,
            r_multiple: None,
            closed_reason: None,
            trigger_time: None,
            mfe_points: None,
            mae_points: None,
        }
    }
}

/// One run's row: one sleeve, one tier.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BacktestResult {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The tier.
    pub tier: Tier,
    /// First session.
    pub date_from: Option<NaiveDate>,
    /// Last session.
    pub date_to: Option<NaiveDate>,
    /// Sessions.
    pub sessions: i64,
    /// Traded sessions.
    pub traded: i64,
    /// `(reason, count)`, by reason.
    pub skipped_by_reason: Vec<(String, i64)>,
    /// `(year, sessions, traded)`.
    pub by_year: Vec<(i32, i64, i64)>,
    /// Share of trades with R > 0.
    pub win_rate: Option<Decimal>,
    /// Mean R.
    pub expectancy_r: Option<Decimal>,
    /// Net ₹.
    pub net_pnl_inr: Option<Decimal>,
    /// Largest fall of the running R.
    pub max_drawdown_r: Option<Decimal>,
    /// The row's caveats, verbatim.
    pub caveats: Vec<String>,
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// Python's `x or Decimal(0)`.
pub(crate) fn or_zero(value: Option<Decimal>) -> Decimal {
    match value {
        Some(v) if !v.is_zero() => v,
        _ => Decimal::ZERO,
    }
}

/// Runs `decide` over `days` and aggregates one row. A Tier 1 outcome with ₹ or R, or a priced
/// tier's traded outcome without R, is an error.
pub fn run<D>(
    sleeve: Sleeve,
    tier: Tier,
    days: impl IntoIterator<Item = D>,
    decide: impl FnMut(D) -> DayOutcome,
    min_sessions: i64,
) -> CoreResult<BacktestResult> {
    let mut ordered: Vec<DayOutcome> = days.into_iter().map(decide).collect();
    ordered.sort_by_key(|o| o.trade_date);
    for o in &ordered {
        let carries_pnl = o.net_pnl_inr.is_some() || o.r_multiple.is_some();
        if tier == Tier::Signals && carries_pnl {
            return Err(CoreError::InvalidInput(
                "Tier 1 measures signals; it has no P&L (04 §13.1)".to_owned(),
            ));
        }
        if tier != Tier::Signals && o.traded && o.r_multiple.is_none() {
            return Err(CoreError::InvalidInput(format!(
                "a Tier {} traded day needs its R",
                tier.value()
            )));
        }
    }
    let traded: Vec<&DayOutcome> = ordered.iter().filter(|o| o.traded).collect();
    let mut skipped: BTreeMap<String, i64> = BTreeMap::new();
    for o in ordered.iter().filter(|o| !o.traded) {
        let reason = o
            .skip_reason
            .clone()
            .filter(|r| !r.is_empty())
            .unwrap_or_else(|| "UNSPECIFIED".to_owned());
        *skipped.entry(reason).or_insert(0) += 1;
    }
    let mut years: BTreeMap<i32, (i64, i64)> = BTreeMap::new();
    for o in &ordered {
        let tally = years.entry(o.trade_date.year()).or_insert((0, 0));
        tally.0 += 1;
        tally.1 += i64::from(o.traded);
    }
    let rs: Vec<Decimal> = traded.iter().filter_map(|o| o.r_multiple).collect();
    let has_pnl = tier != Tier::Signals;
    let n = Decimal::from(count(rs.len()));
    let (win_rate, expectancy_r) = if has_pnl && !rs.is_empty() {
        let wins = Decimal::from(count(rs.iter().filter(|r| **r > Decimal::ZERO).count()));
        (
            Some(num::div(wins, n)?),
            Some(num::div(num::sum(rs.iter().copied())?, n)?),
        )
    } else {
        (None, None)
    };
    let net_pnl_inr = if has_pnl {
        Some(num::sum(traded.iter().map(|o| or_zero(o.net_pnl_inr)))?)
    } else {
        None
    };
    let max_drawdown_r = if has_pnl {
        Some(max_drawdown(&rs)?)
    } else {
        None
    };
    Ok(BacktestResult {
        sleeve,
        tier,
        date_from: ordered.first().map(|o| o.trade_date),
        date_to: ordered.last().map(|o| o.trade_date),
        sessions: count(ordered.len()),
        traded: count(traded.len()),
        skipped_by_reason: skipped.into_iter().collect(),
        by_year: years.into_iter().map(|(y, (s, t))| (y, s, t)).collect(),
        win_rate,
        expectancy_r,
        net_pnl_inr,
        max_drawdown_r,
        caveats: caveats(sleeve, tier, count(ordered.len()), min_sessions),
    })
}

/// Tier 2's quote: the model premium and the synthetic bid/ask around it, wide (a deep wing's
/// model premium needs more than 28 decimal places).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelQuote {
    /// Black-76 premium.
    pub model: WideDecimal,
    /// Model less the half spread, floored at zero.
    pub bid: WideDecimal,
    /// Model plus the half spread.
    pub ask: WideDecimal,
}

fn wide_max(a: WideDecimal, b: WideDecimal) -> WideDecimal {
    if b > a { b } else { a }
}

/// `04` §13.2: Black-76 at the previous India VIX as a flat IV, forward = spot, and §6.2's
/// synthetic half spread either side (the bid floored at zero).
#[allow(clippy::too_many_arguments)]
pub fn tier2_quote(
    spot: Decimal,
    strike: Decimal,
    years: f64,
    vix_close: Decimal,
    kind: OptionType,
    rate: f64,
    rates: &CostRates,
) -> CoreResult<ModelQuote> {
    let arith = |e: crate::decimal::DecimalError| CoreError::Arithmetic(e.to_string());
    let vol = num::to_float(num::div(vix_close, Decimal::ONE_HUNDRED)?);
    let price = black76_price(
        num::to_float(spot),
        num::to_float(strike),
        years,
        rate,
        vol,
        kind,
    );
    let model = py::wide_from_float_str(price).map_err(arith)?;
    let pct = model
        .mul(&WideDecimal::from(rates.synthetic_half_spread_pct))
        .div(&WideDecimal::from(100_i64))
        .map_err(arith)?;
    let half = wide_max(pct, WideDecimal::from(rates.synthetic_half_spread_min_inr));
    Ok(ModelQuote {
        bid: wide_max(model.sub(&half), WideDecimal::zero()),
        ask: model.add(&half),
        model,
    })
}
