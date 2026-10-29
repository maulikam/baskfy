//! Port of `baskfy_core.fno.scan` (`decile-blueprint/packages/core/src/baskfy_core/fno/scan.py`).
//!
//! The nightly scan's arithmetic (`docs/fno/04` §2, §3, §8, §10; `06` FO4). The worker reads
//! the database and writes `fo_scan`; every number it writes is computed here (law 1).
//!
//! **F1** — [`propose_condor`]: the monthly condor `04` §2 would enter, proposed from the
//! bhavcopy of the session the scan read; the liquidity the bhavcopy can prove is checked by
//! name, then sizing (never rounded up) and the round trip as a share of the credit. The first
//! failure in the order structure → liquidity → size → cost is the state; every reason found is
//! kept.
//!
//! **F2** — [`f2_signals`]: the continuous futures series re-derived over one window, then
//! `04` §10's universe (top `f2_universe_turnover_pct` of F&O stocks by the 20-session median
//! futures turnover), breakout, trend, rescaled to rupees on the session's held-contract
//! settle. [`propose_future`] sizes and costs one candidate; [`allocate_capacity`] applies
//! `f2_max_open` and `max_per_industry` in the order given.
//!
//! Python writes `f2_signals` as polars expressions; the port walks the same steps over plain
//! rows of [`continuous_futures`]' output and calls polars' own rolling-mean kernel per symbol
//! (what `.over("symbol")` does), so the floats are the ones polars computes.
//!
//! Goldens: `L1/fno/scan.<function>.case_NNN.json`.

use std::collections::BTreeMap;

use chrono::NaiveDate;
use polars::prelude::{Column, DataFrame, DataType, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use super::condor::{
    CondorStrikes, ENTRY_SEQUENCE, LegRole, RoleMap, num, option_type_of, price_structure,
    role_prices, select_strikes, sign_of,
};
use super::config::py::{mul, sub};
use super::config::{
    F2Config, FnoCeilings, FnoConfig, FnoError, FnoResult, PlanState, SeriesConfig, py,
};
use super::costs::{CondorCost, FutureCharges, condor_round_trip, future_round_trip};
use super::exits::{f2_initial_stop, gtt_trigger};
use super::monitor::cost_reason;
use super::series::{cols, continuous_futures};
use super::sizing::{FoSizing, size};
use super::vol::OptionType;
use crate::decimal::{Rounding, WideDecimal};
use crate::options::costs::CostRates;
use crate::options::gating::Mode;

/// The index underlying whose continuous future is F2's regime gauge (`04` §10).
///
/// Ports `baskfy_core.fno.scan.REGIME_UNDERLYING`.
pub const REGIME_UNDERLYING: &str = "NIFTY";
const STOCK_FUTURE: &str = "FUTSTK";

// =============================================================================================
// F1 — the proposed condor
// =============================================================================================

/// One option's row of the bhavcopy: its settle, open interest (units) and volume.
///
/// Ports `baskfy_core.fno.scan.LegPrint`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegPrint {
    /// The settle.
    pub settle: Decimal,
    /// Open interest, units.
    pub open_interest: Option<i64>,
    /// Contracts traded.
    pub volume: Option<i64>,
}

/// The bhavcopy's prints keyed by `(strike, option type)`: Python's dict, compared by value; a
/// repeated key keeps the last print written.
pub type LegPrints = Vec<((Decimal, OptionType), LegPrint)>;

fn print_at(prints: &LegPrints, strike: Decimal, kind: OptionType) -> Option<LegPrint> {
    prints
        .iter()
        .rev()
        .find(|((k, t), _)| *k == strike && *t == kind)
        .map(|(_, p)| *p)
}

/// What the scan proposes for one underlying on its entry session.
///
/// Ports `baskfy_core.fno.scan.CondorProposal`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CondorProposal {
    /// `None` for a clean proposal, or the first rejection.
    pub state: Option<PlanState>,
    /// Every refusal found, by name.
    pub reasons: Vec<String>,
    /// One standard deviation in points.
    pub sd: Decimal,
    /// The strikes.
    pub strikes: Option<CondorStrikes>,
    /// The settle of each leg that printed.
    pub entry: RoleMap,
    /// The credit per unit.
    pub credit: Option<Decimal>,
    /// The max loss per unit.
    pub max_loss_per_unit: Option<Decimal>,
    /// The max loss per lot, ₹.
    pub max_loss_per_lot_inr: Option<Decimal>,
    /// The sizing.
    pub sizing: Option<FoSizing>,
    /// The round trip.
    pub cost: Option<CondorCost>,
}

/// Python's `a // b` on ints (floor division).
fn floor_div(a: i64, b: i64) -> FnoResult<i64> {
    if b == 0 {
        return Err(FnoError::Value(
            "integer division or modulo by zero".to_owned(),
        ));
    }
    let q = a / b;
    Ok(if (a % b != 0) && ((a < 0) != (b < 0)) {
        q - 1
    } else {
        q
    })
}

/// `04` §2's liquidity, as far as a closing file can prove it, refusals by name.
fn bhavcopy_liquidity(
    strikes: &CondorStrikes,
    prints: &LegPrints,
    lot_size: i64,
    min_short_oi_lots: i64,
) -> FnoResult<Vec<String>> {
    let mut found = Vec::new();
    for role in ENTRY_SEQUENCE {
        let strike = strikes.strike(role);
        let name = format!("{} {}", role.words(), py::str(strike));
        let Some(row) =
            print_at(prints, strike, option_type_of(role)).filter(|r| r.settle > Decimal::ZERO)
        else {
            found.push(format!("{name}: no settle in the bhavcopy"));
            continue;
        };
        if sign_of(role) > 0 {
            if row.volume.unwrap_or(0) == 0 {
                found.push(format!("{name}: the wing did not trade (volume 0)"));
            }
            continue;
        }
        let oi_lots = floor_div(row.open_interest.unwrap_or(0), lot_size)?;
        if oi_lots < min_short_oi_lots {
            found.push(format!("{name}: OI {oi_lots} lots < {min_short_oi_lots}"));
        }
    }
    Ok(found)
}

/// `04` §2-§3 on one session's bhavcopy for one underlying's monthly.
///
/// Ports `baskfy_core.fno.scan.propose_condor`.
///
/// # Errors
/// `ValueError` from [`super::condor::one_sd`] or sizing; a decimal the context cannot hold.
#[allow(clippy::too_many_arguments)] // every input 04 §2-§3 names, as in Python
pub fn propose_condor(
    forward: Decimal,
    iv: f64,
    days_to_expiry: i64,
    call_strikes: &[Decimal],
    put_strikes: &[Decimal],
    prints: &LegPrints,
    lot_size: i64,
    capital_inr: Decimal,
    config: &FnoConfig,
    rates: &CostRates,
    ceilings: &FnoCeilings,
) -> FnoResult<CondorProposal> {
    let choice = select_strikes(
        forward,
        iv,
        days_to_expiry,
        call_strikes,
        put_strikes,
        &config.f1,
    )?;
    let Some(strikes) = choice.strikes else {
        return Ok(CondorProposal {
            state: Some(PlanState::RejectedStructure),
            reasons: choice.reasons,
            sd: choice.sd,
            strikes: None,
            entry: RoleMap::new(),
            credit: None,
            max_loss_per_unit: None,
            max_loss_per_lot_inr: None,
            sizing: None,
            cost: None,
        });
    };
    let mut entry = RoleMap::new();
    for role in LegRole::ALL {
        if let Some(row) = print_at(prints, strikes.strike(role), option_type_of(role))
            && row.settle > Decimal::ZERO
        {
            entry.insert(role, row.settle);
        }
    }
    let liquidity = bhavcopy_liquidity(&strikes, prints, lot_size, config.f1.min_short_oi_lots)?;
    if entry.len() < LegRole::ALL.len() {
        return Ok(CondorProposal {
            state: Some(PlanState::RejectedLiquidity),
            reasons: liquidity,
            sd: choice.sd,
            strikes: Some(strikes),
            entry,
            credit: None,
            max_loss_per_unit: None,
            max_loss_per_lot_inr: None,
            sizing: None,
            cost: None,
        });
    }
    let priced = price_structure(&strikes, &entry)?;
    let mut reasons: Vec<String> = priced.reasons.clone();
    reasons.extend(liquidity.iter().cloned());
    let mut states: Vec<PlanState> = Vec::new();
    if let Some(state) = priced.state {
        states.push(state);
    }
    if !liquidity.is_empty() {
        states.push(PlanState::RejectedLiquidity);
    }
    let loss_per_lot = num::money(mul(priced.max_loss_per_unit, Decimal::from(lot_size))?)?;
    let mut sizing = None;
    let mut cost = None;
    if priced.state.is_none() {
        let sized = size(
            Mode::Paper,
            capital_inr,
            config.f1.risk_per_trade_pct,
            priced.max_loss_per_unit,
            Some(lot_size),
            &config.common,
            ceilings,
        )?;
        if let Some(state) = sized.state {
            states.push(state);
            reasons.push(sized.message.clone());
        }
        let costed = condor_round_trip(
            &role_prices(&entry),
            None,
            sized.lots.max(1) * lot_size,
            rates,
            &config.f1,
        )?;
        if let Some(state) = costed.state {
            states.push(state);
            reasons.push(cost_reason(&costed, config.f1.max_cost_share_pct));
        }
        sizing = Some(sized);
        cost = Some(costed);
    }
    Ok(CondorProposal {
        state: states.first().copied(),
        reasons,
        sd: choice.sd,
        strikes: Some(strikes),
        entry,
        credit: Some(priced.credit),
        max_loss_per_unit: Some(priced.max_loss_per_unit),
        max_loss_per_lot_inr: Some(loss_per_lot),
        sizing,
        cost,
    })
}

// =============================================================================================
// F2 — signals over one re-derived window
// =============================================================================================

/// The columns [`f2_signals`] returns, one row per underlying that printed on the session.
///
/// Ports `baskfy_core.fno.scan.F2_SIGNAL_COLUMNS`.
pub const F2_SIGNAL_COLUMNS: [&str; 19] = [
    "symbol",
    "instrument",
    "held_expiry",
    "held_settle",
    "lot_size",
    "good_sessions",
    "close_inr",
    "prior_high_inr",
    "average_inr",
    "atr_inr",
    "rv20",
    "ca_flag",
    "ca_recent",
    "turnover_20d",
    "turnover_rank",
    "ranked",
    "in_universe",
    "breakout",
    "trend",
];

/// The dtype of each [`F2_SIGNAL_COLUMNS`] column (`_SIGNAL_SCHEMA`).
fn signal_dtype(column: &str) -> DataType {
    match column {
        "symbol" | "instrument" => DataType::String,
        "held_expiry" => DataType::Date,
        "lot_size" => DataType::Int64,
        "good_sessions" | "ranked" => DataType::UInt32,
        "ca_flag" | "ca_recent" | "in_universe" | "breakout" | "trend" => DataType::Boolean,
        _ => DataType::Float64,
    }
}

fn empty_signals() -> FnoResult<DataFrame> {
    let columns: Vec<Column> = F2_SIGNAL_COLUMNS
        .iter()
        .map(|c| Series::new_empty((*c).into(), &signal_dtype(c)).into_column())
        .collect();
    Ok(DataFrame::new(0, columns)?)
}

fn u32_column(name: &str, values: &[Option<u32>]) -> Column {
    Series::new(name.into(), values.to_vec()).into_column()
}

/// One row of the continuous series, as `f2_signals` reads it.
struct ContRow {
    trade_date: Option<NaiveDate>,
    symbol: String,
    instrument: Option<String>,
    held_expiry: Option<NaiveDate>,
    held_settle: Option<f64>,
    level_c: Option<f64>,
    atr14: Option<f64>,
    rv20: Option<f64>,
    lot_size: Option<i64>,
    turnover_20d: Option<f64>,
    ca_flag: Option<bool>,
    ca_recent: Option<bool>,
}

fn read_cont(cont: &DataFrame) -> FnoResult<Vec<ContRow>> {
    let trade = cols::dates(cont, "trade_date")?;
    let symbol = cols::strings(cont, "symbol")?;
    let instrument = cols::strings(cont, "instrument")?;
    let expiry = cols::dates(cont, "held_expiry")?;
    let settle = cols::floats(cont, "held_settle")?;
    let level_c = cols::floats(cont, "level_c")?;
    let atr = cols::floats(cont, "atr14")?;
    let rv = cols::floats(cont, "rv20")?;
    let lot = cols::ints(cont, "lot_size")?;
    let turnover = cols::floats(cont, "fut_turnover_20d")?;
    let ca = cols::bools(cont, "ca_flag")?;
    let recent = cols::bools(cont, "ca_recent")?;
    Ok((0..cont.height())
        .map(|i| ContRow {
            trade_date: trade[i],
            symbol: symbol[i].clone().unwrap_or_default(),
            instrument: instrument[i].clone(),
            held_expiry: expiry[i],
            held_settle: settle[i],
            level_c: level_c[i],
            atr14: atr[i],
            rv20: rv[i],
            lot_size: lot[i],
            turnover_20d: turnover[i],
            ca_flag: ca[i],
            ca_recent: recent[i],
        })
        .collect())
}

/// Per symbol over its non-CA rows (sorted by date): the prior `breakout_sessions` closes'
/// max, the `trend_sessions` mean, and how many good sessions the window holds.
struct Good {
    prior_high: Option<f64>,
    average: Option<f64>,
    sessions: u32,
}

fn usize_window(n: i64) -> FnoResult<usize> {
    usize::try_from(n)
        .ok()
        .filter(|w| *w > 0)
        .ok_or_else(|| FnoError::Value(format!("window_size {n} must be positive")))
}

fn good_windows(rows: &[ContRow], f2: &F2Config) -> FnoResult<BTreeMap<(String, NaiveDate), Good>> {
    let mut by_symbol: BTreeMap<&str, Vec<(NaiveDate, f64)>> = BTreeMap::new();
    for r in rows {
        if let (Some(level), Some(day)) = (r.level_c, r.trade_date) {
            by_symbol
                .entry(r.symbol.as_str())
                .or_default()
                .push((day, level));
        }
    }
    let breakout = usize_window(f2.breakout_sessions)?;
    let trend = usize_window(f2.trend_sessions)?;
    let mut out = BTreeMap::new();
    for (symbol, mut series) in by_symbol {
        series.sort_by_key(|(d, _)| *d);
        let levels: Vec<Option<f64>> = series.iter().map(|(_, l)| Some(*l)).collect();
        let means = cols::rolling(&levels, cols::Roll::Mean, trend)?;
        let sessions = u32::try_from(series.len()).unwrap_or(u32::MAX);
        for (i, (day, _)) in series.iter().enumerate() {
            // `shift(1).rolling_max(n)`: the n closes before this one, all present.
            let prior_high = (i >= breakout).then(|| {
                series[i - breakout..i]
                    .iter()
                    .map(|(_, l)| *l)
                    .fold(f64::NEG_INFINITY, f64::max)
            });
            out.insert(
                (symbol.to_owned(), *day),
                Good {
                    prior_high,
                    average: means[i],
                    sessions,
                },
            );
        }
    }
    Ok(out)
}

fn times(a: Option<f64>, b: Option<f64>) -> Option<f64> {
    Some(a? * b?)
}

fn above(a: Option<f64>, b: Option<f64>) -> Option<bool> {
    Some(a? > b?)
}

/// `rank(method="average", descending=True)` of each value among `values`.
fn average_rank_desc(values: &[f64]) -> Vec<f64> {
    values
        .iter()
        .map(|v| {
            let greater = values.iter().filter(|x| **x > *v).count();
            let equal = values
                .iter()
                .filter(|x| x.partial_cmp(&v) == Some(std::cmp::Ordering::Equal))
                .count();
            #[allow(clippy::cast_precision_loss)] // counts far below 2^52
            let rank = greater as f64 + (equal as f64 + 1.0) / 2.0;
            rank
        })
        .collect()
}

/// `04` §10 on `trade_date` from a window of futures rows ending there. A row after
/// `trade_date` is refused (house rule 5). `breakout` and `trend` are null where the window is
/// too short to say — a null is not a `False`. A corporate-action session has no level and is
/// dropped from the rolling windows; `ca_recent` still carries it.
///
/// Ports `baskfy_core.fno.scan.f2_signals`.
///
/// # Errors
/// `ValueError` on look-ahead; a polars error from the frame.
pub fn f2_signals(
    futures: &DataFrame,
    trade_date: NaiveDate,
    f2: &F2Config,
    series: &SeriesConfig,
) -> FnoResult<DataFrame> {
    if futures.height() == 0 {
        return empty_signals();
    }
    if let Some(latest) = cols::dates(futures, "trade_date")?
        .into_iter()
        .flatten()
        .max()
        && latest > trade_date
    {
        return Err(FnoError::Value(format!(
            "the window holds {}, after {}: look-ahead",
            latest.format("%Y-%m-%d"),
            trade_date.format("%Y-%m-%d")
        )));
    }
    let cont = continuous_futures(futures, series)?;
    let rows = read_cont(&cont)?;
    let good = good_windows(&rows, f2)?;
    let mut today: Vec<&ContRow> = rows
        .iter()
        .filter(|r| r.trade_date == Some(trade_date))
        .collect();
    if today.is_empty() {
        return empty_signals();
    }
    // `.sort("symbol")` at the end: one row per symbol on a session.
    today.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    let n = today.len();
    let mut c_close = Vec::with_capacity(n);
    let mut c_prior = Vec::with_capacity(n);
    let mut c_avg = Vec::with_capacity(n);
    let mut c_atr = Vec::with_capacity(n);
    let mut c_good = Vec::with_capacity(n);
    let mut c_breakout = Vec::with_capacity(n);
    let mut c_trend = Vec::with_capacity(n);
    for r in &today {
        let g = good.get(&(r.symbol.clone(), trade_date));
        let prior = g.and_then(|g| g.prior_high);
        let average = g.and_then(|g| g.average);
        let scale = match (r.level_c, r.held_settle) {
            (Some(level), Some(settle)) if level > 0.0 => Some(settle / level),
            _ => None,
        };
        c_close.push(times(r.level_c, scale));
        c_prior.push(times(prior, scale));
        c_avg.push(times(average, scale));
        c_atr.push(times(r.atr14, scale));
        c_good.push(Some(g.map_or(0, |g| g.sessions)));
        c_breakout.push(above(r.level_c, prior));
        c_trend.push(above(r.level_c, average));
    }
    // The universe: FUTSTK rows with a turnover, ranked descending by it.
    let rankable: Vec<usize> = (0..n)
        .filter(|&i| {
            today[i].instrument.as_deref() == Some(STOCK_FUTURE) && today[i].turnover_20d.is_some()
        })
        .collect();
    let turnovers: Vec<f64> = rankable
        .iter()
        .filter_map(|&i| today[i].turnover_20d)
        .collect();
    let ranks = average_rank_desc(&turnovers);
    let ranked = u32::try_from(rankable.len()).unwrap_or(u32::MAX);
    let mut c_rank: Vec<Option<f64>> = vec![None; n];
    let mut c_ranked: Vec<Option<u32>> = vec![None; n];
    for (k, &i) in rankable.iter().enumerate() {
        c_rank[i] = ranks.get(k).copied();
        c_ranked[i] = Some(ranked);
    }
    let share = num::to_f64(f2.universe_turnover_pct)? / 100.0;
    let c_universe: Vec<Option<bool>> = (0..n)
        .map(|i| {
            let stock = today[i].instrument.as_deref() == Some(STOCK_FUTURE);
            Some(match (c_rank[i], c_ranked[i]) {
                (Some(rank), Some(count)) => stock && rank <= share * f64::from(count),
                _ => false,
            })
        })
        .collect();
    let pick = |f: fn(&ContRow) -> Option<f64>| -> Vec<Option<f64>> {
        today.iter().map(|r| f(r)).collect()
    };
    let columns = vec![
        cols::string_column(
            "symbol",
            &today
                .iter()
                .map(|r| Some(r.symbol.clone()))
                .collect::<Vec<_>>(),
        ),
        cols::string_column(
            "instrument",
            &today
                .iter()
                .map(|r| r.instrument.clone())
                .collect::<Vec<_>>(),
        ),
        cols::date_column(
            "held_expiry",
            &today.iter().map(|r| r.held_expiry).collect::<Vec<_>>(),
        )?,
        cols::float_column("held_settle", &pick(|r| r.held_settle)),
        cols::int_column(
            "lot_size",
            &today.iter().map(|r| r.lot_size).collect::<Vec<_>>(),
        ),
        u32_column("good_sessions", &c_good),
        cols::float_column("close_inr", &c_close),
        cols::float_column("prior_high_inr", &c_prior),
        cols::float_column("average_inr", &c_avg),
        cols::float_column("atr_inr", &c_atr),
        cols::float_column("rv20", &pick(|r| r.rv20)),
        cols::bool_column(
            "ca_flag",
            &today
                .iter()
                .map(|r| Some(r.ca_flag.unwrap_or(false)))
                .collect::<Vec<_>>(),
        ),
        cols::bool_column(
            "ca_recent",
            &today
                .iter()
                .map(|r| Some(r.ca_recent.unwrap_or(false)))
                .collect::<Vec<_>>(),
        ),
        cols::float_column("turnover_20d", &pick(|r| r.turnover_20d)),
        cols::float_column("turnover_rank", &c_rank),
        u32_column("ranked", &c_ranked),
        cols::bool_column("in_universe", &c_universe),
        cols::bool_column("breakout", &c_breakout),
        cols::bool_column("trend", &c_trend),
    ];
    Ok(DataFrame::new(n, columns)?)
}

/// One F2 candidate's plan numbers (`04` §10), in rupees per unit unless named.
///
/// Ports `baskfy_core.fno.scan.FutureProposal`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FutureProposal {
    /// The entry.
    pub entry: Decimal,
    /// ATR14 at the signal close, to the paisa.
    pub atr: Decimal,
    /// The ATR stop.
    pub stop: Decimal,
    /// The GTT trigger.
    pub gtt_trigger: Decimal,
    /// `entry - stop`.
    pub risk_per_unit: Decimal,
    /// One lot's risk, ₹.
    pub risk_per_lot_inr: Decimal,
    /// The sizing.
    pub sizing: FoSizing,
    /// The round trip.
    pub cost: FutureCharges,
    /// Round trip ÷ one lot's risk, as a percent: the cost in R.
    pub cost_share_pct: Decimal,
}

/// Stop `entry - 3 x ATR14`, the GTT at the tighter of that and `stop_from_vol`, lots from the
/// budget, and the round trip with the exit priced at the entry.
///
/// Ports `baskfy_core.fno.scan.propose_future`.
///
/// # Errors
/// `ValueError` for a non-positive ATR (or from sizing); a decimal the context cannot hold.
pub fn propose_future(
    entry: Decimal,
    atr: Decimal,
    ann_vol: f64,
    lot_size: i64,
    capital_inr: Decimal,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> FnoResult<FutureProposal> {
    if atr <= Decimal::ZERO {
        return Err(FnoError::Value(
            "ATR must be positive to place a stop".to_owned(),
        ));
    }
    let stop = num::money(f2_initial_stop(entry, atr, &config.f2)?)?;
    let trigger = num::money(gtt_trigger(stop, entry, ann_vol, &config.stop_vol)?)?;
    let risk = sub(entry, stop)?;
    let sizing = size(
        Mode::Paper,
        capital_inr,
        config.f2.risk_per_trade_pct,
        risk,
        Some(lot_size),
        &config.common,
        ceilings,
    )?;
    let lots = sizing.lots.max(1);
    let cost = future_round_trip(entry, entry, lots * lot_size, &config.future_costs)?;
    let per_lot = num::money(mul(risk, Decimal::from(lot_size))?)?;
    // `(cost.total / (per_lot * lots) * 100).quantize(Decimal("0.01"))`, half even.
    let share = py::div_wide(cost.total()?, mul(per_lot, Decimal::from(lots))?)?
        .mul(&WideDecimal::from(Decimal::ONE_HUNDRED))
        .quantize(-2, Rounding::HalfEven)
        .map_err(|e| num::wide_err(&e))?;
    Ok(FutureProposal {
        entry,
        atr: num::money(atr)?,
        stop,
        gtt_trigger: trigger,
        risk_per_unit: risk,
        risk_per_lot_inr: per_lot,
        sizing,
        cost,
        cost_share_pct: num::narrow(&share)?,
    })
}

/// `04` §10's concurrency over `ordered` `(symbol, industry)` candidates: admitted (`None`)
/// while the book holds fewer than `f2_max_open` and the industry fewer than
/// `max_per_industry`, else the reason it is blocked. A name with no known industry is capped
/// only by `f2_max_open`. The answer keeps Python's dict: one entry per symbol, in first-seen
/// order, the last verdict written.
///
/// Ports `baskfy_core.fno.scan.allocate_capacity`.
#[must_use]
pub fn allocate_capacity(
    ordered: &[(String, Option<String>)],
    open_industries: &[Option<String>],
    f2: &F2Config,
) -> Vec<(String, Option<String>)> {
    let mut held = i64::try_from(open_industries.len()).unwrap_or(i64::MAX);
    let mut per_industry: BTreeMap<String, i64> = BTreeMap::new();
    for industry in open_industries.iter().flatten() {
        *per_industry.entry(industry.clone()).or_insert(0) += 1;
    }
    let mut out: Vec<(String, Option<String>)> = Vec::new();
    let mut put =
        |symbol: &str, verdict: Option<String>| match out.iter_mut().find(|(s, _)| s == symbol) {
            Some(slot) => slot.1 = verdict,
            None => out.push((symbol.to_owned(), verdict)),
        };
    for (symbol, industry) in ordered {
        if held >= f2.max_open {
            put(
                symbol,
                Some(format!(
                    "the book holds {held} of f2_max_open {} positions",
                    f2.max_open
                )),
            );
            continue;
        }
        if let Some(ind) = industry {
            let n = per_industry.get(ind).copied().unwrap_or(0);
            if n >= f2.max_per_industry {
                put(
                    symbol,
                    Some(format!(
                        "industry {ind} already holds {n} of {} positions",
                        f2.max_per_industry
                    )),
                );
                continue;
            }
        }
        put(symbol, None);
        held += 1;
        if let Some(ind) = industry {
            *per_industry.entry(ind.clone()).or_insert(0) += 1;
        }
    }
    out
}
