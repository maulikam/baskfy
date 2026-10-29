//! Port of `baskfy_core.fno.directional_retest`
//! (`decile-blueprint/packages/core/src/baskfy_core/fno/directional_retest.py`).
//!
//! F3's EOD re-test: the daily half of `04` §11 on the option bhavcopy, pure (F3-3). The index
//! bars are the front-month future's OHLC; the entry is at the next session's settle; each
//! later session judges the level break on its high or low, the loss cut and the decay target
//! at the settle, the hard exit at expiry (intrinsic), and the next-session add. R is net P&L
//! per unit ÷ the spread's max loss per unit. Costs are the research's index-option costs per
//! leg per crossing (`research::leg_costs`, 0.5 % with a ₹0.05 floor).
//!
//! Money is `Decimal` as in Python, in Python's default context (`config::py`, and
//! `WideDecimal` where a quotient can print beyond 28 places); a cost crosses through `float`
//! and back (`Decimal(str(float))`) exactly where Python's does.
//!
//! Goldens: `L1/fno/directional_retest.*.case_NNN.json`.

use std::collections::{BTreeMap, BTreeSet};

use chrono::{Datelike, NaiveDate, Weekday};
use polars::prelude::{DataFrame, DataType};
use rust_decimal::Decimal;
use serde::Serialize;

use super::condor::num;
use super::config::py::{add, mul, sub};
use super::config::{F3Config, FnoError, FnoResult, py};
use super::directional::{
    DailyBar, Direction, Levels, SpreadStrikes, bars_needed, credit_per_unit, daily_direction,
    decay_target_mark, key_level, level_broken, levels, loss_cut_mark, max_loss_per_unit,
    spread_strikes,
};
use super::research::frames::{self, Cell};
use super::research::{CondorParams, Summary, leg_costs, summarise};
use super::series::cols;
use super::vol::OptionType;
use crate::backtest::helpers::WideDecimal;

/// The research's index-option cost knobs (`RESEARCH.md` method; `04` §3).
///
/// Ports `baskfy_core.fno.directional_retest.INDEX_COSTS`.
#[must_use]
pub fn index_costs() -> CondorParams {
    CondorParams {
        slip_pct: 0.005,
        slip_min: 0.05,
        ..CondorParams::default()
    }
}

/// Lot sizes for sessions before the UDiFF file carried `NewBrdLotQty` (8 Jul 2024).
///
/// Ports `baskfy_core.fno.directional_retest.FALLBACK_LOT`.
pub const FALLBACK_LOT: [(&str, i64); 2] = [("NIFTY", 50), ("BANKNIFTY", 15)];

/// The rules the closing file cannot test, named so the evidence can say so.
///
/// Ports `baskfy_core.fno.directional_retest.NOT_TESTED`.
pub const NOT_TESTED: [&str; 4] = [
    "the 75-minute confirm",
    "the intraday alignment with the session's open",
    "the intraday cut at the moment the level breaks (the proxy exits at that session's settle)",
    "the loss cut read from a live mark (the proxy reads the settle)",
];

const TRADE_COLUMNS: [&str; 17] = [
    "symbol",
    "direction",
    "signal",
    "entry",
    "exit",
    "expiry",
    "option_type",
    "short",
    "wing",
    "credit",
    "lots",
    "reason",
    "sessions",
    "gross_R",
    "cost_R",
    "R",
    "pnl_inr",
];

fn fallback_lot(symbol: &str) -> i64 {
    FALLBACK_LOT
        .iter()
        .find(|(s, _)| *s == symbol)
        .map_or(50, |(_, l)| *l)
}

/// `(date, expiry, strike, type)`.
pub type OptionKey = (NaiveDate, NaiveDate, Decimal, OptionType);

/// One underlying's daily bars (front-month future), lot sizes and option settles.
///
/// Ports `baskfy_core.fno.directional_retest.IndexPanel`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexPanel {
    /// The underlying.
    pub symbol: String,
    /// The front-month future's daily bars.
    pub bars: Vec<DailyBar>,
    /// The lot size in force each session.
    pub lot_by_date: BTreeMap<NaiveDate, i64>,
    /// `(date, expiry, strike, type) -> settle`; only positive settles.
    pub settle: BTreeMap<OptionKey, Decimal>,
    /// `(date, expiry, strike, type) -> traded` (OI or volume that day).
    pub traded: BTreeMap<OptionKey, bool>,
    /// `date -> the expiries listed that day`, sorted.
    pub expiries_by_date: BTreeMap<NaiveDate, Vec<NaiveDate>>,
    /// `(date, expiry) -> the strikes listed for that expiry that day`, sorted.
    pub strikes_by_expiry: BTreeMap<(NaiveDate, NaiveDate), Vec<Decimal>>,
}

/// `_dec(value)` of a frame column: `Decimal(str(value))` — a float's `repr`, an int's digits.
fn dec_column(df: &DataFrame, name: &str) -> FnoResult<Vec<Option<Decimal>>> {
    let dtype = df.column(name)?.dtype().clone();
    match dtype {
        DataType::Float64 | DataType::Float32 => cols::floats(df, name)?
            .into_iter()
            .map(|v| v.map(num::dec_from_float).transpose())
            .collect(),
        DataType::String => cols::strings(df, name)?
            .into_iter()
            .map(|v| {
                v.map(|s| {
                    s.trim().parse::<Decimal>().map_err(|_| {
                        FnoError::Decimal("[<class 'decimal.ConversionSyntax'>]".to_owned())
                    })
                })
                .transpose()
            })
            .collect(),
        DataType::Null => Ok(vec![None; df.height()]),
        _ => Ok(cols::ints(df, name)?
            .into_iter()
            .map(|v| v.map(Decimal::from))
            .collect()),
    }
}

fn has_column(df: &DataFrame, name: &str) -> bool {
    df.get_column_names().iter().any(|c| c.as_str() == name)
}

/// The panel for `symbol` from bhavcopy rows (`fo_contract_daily`'s columns, or the day files'
/// with `date`). The front month is the nearest unexpired future each session.
///
/// Ports `baskfy_core.fno.directional_retest.index_panel`.
///
/// # Errors
/// A missing column, or a strike `Decimal` cannot read.
#[allow(clippy::too_many_lines)]
pub fn index_panel(contracts: &DataFrame, symbol: &str) -> FnoResult<IndexPanel> {
    let session = frames::session_column(contracts);
    let date = cols::dates(contracts, session)?;
    let sym = cols::strings(contracts, "symbol")?;
    let instrument = cols::strings(contracts, "instrument")?;
    let expiry = cols::dates(contracts, "expiry")?;
    let mine: Vec<usize> = (0..contracts.height())
        .filter(|&i| sym[i].as_deref() == Some(symbol))
        .collect();
    // The front-month future per session: the smallest expiry on or after it.
    let mut front: BTreeMap<NaiveDate, usize> = BTreeMap::new();
    for &i in &mine {
        if instrument[i].as_deref() != Some("FUTIDX") {
            continue;
        }
        let (Some(d), Some(e)) = (date[i], expiry[i]) else {
            continue;
        };
        if e < d {
            continue;
        }
        match front.get(&d) {
            Some(&j) if expiry[j] <= Some(e) => {}
            _ => {
                front.insert(d, i);
            }
        }
    }
    let open = dec_column(contracts, "open")?;
    let high = dec_column(contracts, "high")?;
    let low = dec_column(contracts, "low")?;
    let close = dec_column(contracts, "close")?;
    let lot_size = if has_column(contracts, "lot_size") {
        cols::ints(contracts, "lot_size")?
    } else {
        vec![None; contracts.height()]
    };
    let mut bars = Vec::new();
    let mut lot_by_date = BTreeMap::new();
    let mut last_lot = fallback_lot(symbol);
    for (d, &i) in &front {
        let (Some(o), Some(h), Some(l), Some(c)) = (open[i], high[i], low[i], close[i]) else {
            continue;
        };
        bars.push(DailyBar {
            date: *d,
            open: o,
            high: h,
            low: l,
            close: c,
        });
        if let Some(lot) = lot_size[i]
            && lot != 0
        {
            last_lot = lot;
        }
        lot_by_date.insert(*d, last_lot);
    }
    let strike = dec_column(contracts, "strike")?;
    let kind = cols::strings(contracts, "option_type")?;
    let settle_col = dec_column(contracts, "settle")?;
    let oi = cols::ints(contracts, "open_interest")?;
    let volume = cols::ints(contracts, "volume")?;
    let mut settle = BTreeMap::new();
    let mut traded = BTreeMap::new();
    let mut expiries: BTreeMap<NaiveDate, BTreeSet<NaiveDate>> = BTreeMap::new();
    let mut strikes: BTreeMap<(NaiveDate, NaiveDate), Vec<Decimal>> = BTreeMap::new();
    for &i in &mine {
        if instrument[i].as_deref() != Some("OPTIDX") {
            continue;
        }
        let k = match kind[i].as_deref() {
            Some("CE") => OptionType::Ce,
            Some("PE") => OptionType::Pe,
            _ => continue,
        };
        let Some(price) = settle_col[i] else { continue };
        let (Some(d), Some(e)) = (date[i], expiry[i]) else {
            continue;
        };
        let Some(s) = strike[i] else {
            return Err(FnoError::Decimal(
                "[<class 'decimal.ConversionSyntax'>]".to_owned(),
            ));
        };
        let key = (d, e, s, k);
        if price > Decimal::ZERO {
            settle.insert(key, price);
        }
        traded.insert(key, oi[i].unwrap_or(0) > 0 || volume[i].unwrap_or(0) > 0);
        expiries.entry(d).or_default().insert(e);
        let listed = strikes.entry((d, e)).or_default();
        // A set: the first representation of a value is the one kept.
        if !listed.contains(&s) {
            listed.push(s);
        }
    }
    Ok(IndexPanel {
        symbol: symbol.to_owned(),
        bars,
        lot_by_date,
        settle,
        traded,
        expiries_by_date: expiries
            .into_iter()
            .map(|(k, v)| (k, v.into_iter().collect()))
            .collect(),
        strikes_by_expiry: strikes
            .into_iter()
            .map(|(k, mut v)| {
                v.sort();
                (k, v)
            })
            .collect(),
    })
}

/// The last listed expiry of each calendar month: the monthly contract.
///
/// Ports `baskfy_core.fno.directional_retest.monthly_expiries`.
#[must_use]
pub fn monthly_expiries(listed: &[NaiveDate]) -> Vec<NaiveDate> {
    let mut by_month: BTreeMap<(i32, u32), NaiveDate> = BTreeMap::new();
    for e in listed {
        let entry = by_month.entry((e.year(), e.month())).or_insert(*e);
        if *e > *entry {
            *entry = *e;
        }
    }
    let mut out: Vec<NaiveDate> = by_month.into_values().collect();
    out.sort_unstable();
    out
}

/// The most common gap between adjacent listed strikes around `near` (a tie to the finer).
///
/// Ports `baskfy_core.fno.directional_retest.strike_step`.
///
/// # Errors
/// A difference the default context cannot hold.
pub fn strike_step(strikes: &[Decimal], near: Decimal) -> FnoResult<Option<Decimal>> {
    if strikes.len() < 2 {
        return Ok(None);
    }
    let mut keyed: Vec<(Decimal, Decimal)> = strikes
        .iter()
        .map(|k| Ok((sub(*k, near)?.abs(), *k)))
        .collect::<FnoResult<_>>()?;
    keyed.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)));
    let mut ordered: Vec<Decimal> = keyed.into_iter().take(21).map(|(_, k)| k).collect();
    ordered.sort();
    let mut gaps: Vec<(Decimal, i64)> = Vec::new();
    for pair in ordered.windows(2) {
        let g = sub(pair[1], pair[0])?;
        match gaps.iter_mut().find(|(k, _)| *k == g) {
            Some(slot) => slot.1 += 1,
            None => gaps.push((g, 1)),
        }
    }
    let Some(top) = gaps.iter().map(|(_, n)| *n).max() else {
        return Ok(None);
    };
    Ok(gaps
        .iter()
        .filter(|(_, n)| *n == top)
        .map(|(g, _)| *g)
        .reduce(|a, b| if b < a { b } else { a }))
}

fn leg_cost(price: Decimal, sell: bool, lot: i64) -> FnoResult<Decimal> {
    #[allow(clippy::cast_precision_loss)]
    let lot = lot as f64;
    num::dec_from_float(leg_costs(num::to_f64(price)?, sell, lot, &index_costs()))
}

#[derive(Debug, Clone)]
struct Open {
    direction: Direction,
    level: Decimal,
    strikes: SpreadStrikes,
    expiry: NaiveDate,
    signal: NaiveDate,
    entry: NaiveDate,
    entry_index: usize,
    credit: Decimal,
    lots: i64,
    lot: i64,
    max_loss_unit: Decimal,
    credit_total: Decimal,
    cost_total: Decimal,
    added: bool,
}

/// What [`run_symbol`] returns.
///
/// Ports `baskfy_core.fno.directional_retest.F3RetestResult`.
#[derive(Debug, Clone)]
pub struct F3RetestResult {
    /// The underlying.
    pub symbol: String,
    /// One row per trade.
    pub trades: DataFrame,
    /// The headline numbers.
    pub summary: Summary,
    /// Signals not entered, by reason.
    pub skipped: BTreeMap<String, i64>,
}

fn intrinsic(strike: Decimal, kind: OptionType, close: Decimal) -> FnoResult<Decimal> {
    let v = if kind == OptionType::Pe {
        sub(strike, close)?
    } else {
        sub(close, strike)?
    };
    Ok(py::max(v, Decimal::ZERO))
}

/// The short's and the wing's values on `day`: intrinsic on and after expiry, else the settles
/// (a settle above a quarter of the index is not a premium: missing).
fn legs(
    panel: &IndexPanel,
    day: NaiveDate,
    o: &Open,
    close: Decimal,
) -> FnoResult<Option<(Decimal, Decimal)>> {
    let kind = o.strikes.option_type;
    if day >= o.expiry {
        return Ok(Some((
            intrinsic(o.strikes.short, kind, close)?,
            intrinsic(o.strikes.wing, kind, close)?,
        )));
    }
    let quarter = py::div(close, Decimal::from(4))?;
    let Some(short) = panel
        .settle
        .get(&(day, o.expiry, o.strikes.short, kind))
        .copied()
    else {
        return Ok(None);
    };
    if short > quarter {
        return Ok(None);
    }
    let wing = panel
        .settle
        .get(&(day, o.expiry, o.strikes.wing, kind))
        .copied()
        .filter(|w| !w.is_zero())
        .unwrap_or(Decimal::ZERO);
    if wing > quarter {
        return Ok(None);
    }
    Ok(Some((short, wing)))
}

fn wide_err(e: &crate::backtest::helpers::BacktestError) -> FnoError {
    FnoError::Decimal(e.to_string())
}

fn wide_f64(w: &WideDecimal) -> FnoResult<f64> {
    let text = w.to_sci_string();
    text.parse::<f64>()
        .map_err(|e| FnoError::Value(format!("float({text}): {e}")))
}

fn five_cents() -> Decimal {
    Decimal::new(5, 2)
}

fn close_row(
    o: &Open,
    day: NaiveDate,
    legs: (Decimal, Decimal),
    reason: &str,
    sessions: i64,
    symbol: &str,
) -> FnoResult<Vec<Cell>> {
    let (short, wing) = legs;
    let mark = WideDecimal::from(sub(short, wing)?);
    let exit_cost = WideDecimal::from(leg_cost(short, false, o.lot)?).add(&WideDecimal::from(
        leg_cost(py::max(wing, five_cents()), true, o.lot)?,
    ));
    let lots = WideDecimal::from(o.lots);
    let cost_unit = WideDecimal::from(o.cost_total)
        .div(&lots)
        .map_err(|e| wide_err(&e))?
        .add(&exit_cost);
    let gross_unit = WideDecimal::from(o.credit_total)
        .div(&lots)
        .map_err(|e| wide_err(&e))?
        .sub(&mark);
    let net_unit = gross_unit.sub(&cost_unit);
    let units = o.lots * o.lot;
    let max_loss = WideDecimal::from(o.max_loss_unit);
    let ratio = |x: &WideDecimal| -> FnoResult<f64> {
        wide_f64(&x.div(&max_loss).map_err(|e| wide_err(&e))?)
    };
    Ok(vec![
        Cell::Str(symbol.to_owned()),
        Cell::Str(o.direction.value().to_owned()),
        Cell::Date(o.signal),
        Cell::Date(o.entry),
        Cell::Date(day),
        Cell::Date(o.expiry),
        Cell::Str(option_type_value(o.strikes.option_type).to_owned()),
        Cell::Float(num::to_f64(o.strikes.short)?),
        Cell::Float(num::to_f64(o.strikes.wing)?),
        Cell::Float(num::to_f64(o.credit)?),
        Cell::Int(o.lots),
        Cell::Str(reason.to_owned()),
        Cell::Int(sessions),
        Cell::Float(ratio(&gross_unit)?),
        Cell::Float(ratio(&cost_unit)?),
        Cell::Float(ratio(&net_unit)?),
        Cell::Float(wide_f64(&net_unit.mul(&WideDecimal::from(units)))?),
    ])
}

fn option_type_value(kind: OptionType) -> &'static str {
    match kind {
        OptionType::Ce => "CE",
        OptionType::Pe => "PE",
    }
}

fn bump(skipped: &mut BTreeMap<String, i64>, reason: &str) {
    *skipped.entry(reason.to_owned()).or_insert(0) += 1;
}

#[allow(clippy::too_many_arguments)]
fn try_entry(
    panel: &IndexPanel,
    lv: &Levels,
    direction: Direction,
    signal: NaiveDate,
    day: NaiveDate,
    entry_index: usize,
    f3: &F3Config,
    skipped: &mut BTreeMap<String, i64>,
) -> FnoResult<Option<Open>> {
    let listed: Vec<NaiveDate> = panel
        .expiries_by_date
        .get(&day)
        .cloned()
        .unwrap_or_default();
    let expiry = if panel.symbol == "NIFTY" {
        choose_expiry(panel, &listed, day, f3.min_sessions_weekly)
    } else {
        choose_expiry(
            panel,
            &monthly_expiries(&listed),
            day,
            f3.min_sessions_monthly,
        )
    };
    let Some(expiry) = expiry else {
        bump(skipped, "no_expiry");
        return Ok(None);
    };
    let listed_strikes = panel
        .strikes_by_expiry
        .get(&(day, expiry))
        .cloned()
        .unwrap_or_default();
    let Some(step) = strike_step(&listed_strikes, lv.close)? else {
        bump(skipped, "no_strikes");
        return Ok(None);
    };
    let strikes = spread_strikes(direction, lv, step, f3)?;
    let short = panel
        .settle
        .get(&(day, expiry, strikes.short, strikes.option_type))
        .copied();
    let wing = panel
        .settle
        .get(&(day, expiry, strikes.wing, strikes.option_type))
        .copied();
    let (Some(short), Some(wing)) = (short, wing) else {
        bump(skipped, "no_settle");
        return Ok(None);
    };
    if !panel
        .traded
        .get(&(day, expiry, strikes.short, strikes.option_type))
        .copied()
        .unwrap_or(false)
    {
        bump(skipped, "short_not_traded");
        return Ok(None);
    }
    let credit = credit_per_unit(short, wing)?;
    if short < f3.short_premium_min_inr || credit <= Decimal::ZERO {
        bump(skipped, "thin_credit");
        return Ok(None);
    }
    let lot = panel
        .lot_by_date
        .get(&day)
        .copied()
        .unwrap_or_else(|| fallback_lot(&panel.symbol));
    let entry_cost = add(leg_cost(short, true, lot)?, leg_cost(wing, false, lot)?)?;
    let level = match key_level(direction, lv) {
        Some(l) if !l.is_zero() => l,
        _ => lv.close,
    };
    Ok(Some(Open {
        direction,
        level,
        strikes,
        expiry,
        signal,
        entry: day,
        entry_index,
        credit,
        lots: 1,
        lot,
        max_loss_unit: max_loss_per_unit(&strikes, credit)?,
        credit_total: credit,
        cost_total: entry_cost,
        added: false,
    }))
}

/// Sessions after `entry` up to `expiry`: the panel's own sessions, and weekdays beyond the
/// panel's last bar (an expiry past the sample's end is still a real date).
///
/// Ports `baskfy_core.fno.directional_retest.sessions_left`.
#[must_use]
pub fn sessions_left(panel: &IndexPanel, entry: NaiveDate, expiry: NaiveDate) -> i64 {
    let inside = panel
        .bars
        .iter()
        .filter(|b| entry < b.date && b.date <= expiry)
        .count();
    let last = panel.bars.last().map_or(entry, |b| b.date);
    let mut beyond = 0_i64;
    let mut day = last.max(entry).succ_opt();
    while let Some(d) = day {
        if d > expiry {
            break;
        }
        if !matches!(d.weekday(), Weekday::Sat | Weekday::Sun) {
            beyond += 1;
        }
        day = d.succ_opt();
    }
    i64::try_from(inside).unwrap_or(i64::MAX) + beyond
}

/// `directional.choose_expiry`'s rule on the panel's calendar: the nearest listed expiry with
/// at least `min_sessions` sessions left after `entry`.
///
/// Ports `baskfy_core.fno.directional_retest.choose_expiry`.
#[must_use]
pub fn choose_expiry(
    panel: &IndexPanel,
    listed: &[NaiveDate],
    entry: NaiveDate,
    min_sessions: i64,
) -> Option<NaiveDate> {
    let set: BTreeSet<NaiveDate> = listed.iter().copied().collect();
    set.into_iter()
        .find(|e| *e >= entry && sessions_left(panel, entry, *e) >= min_sessions)
}

fn level_broken_on_bar(o: &Open, bar: &DailyBar, f3: &F3Config) -> FnoResult<bool> {
    let extreme = if o.direction == Direction::Up {
        bar.low
    } else {
        bar.high
    };
    level_broken(o.direction, o.level, extreme, f3)
}

/// Every F3 trade the daily rules would have taken on `panel`, in sample `[start, end]`.
///
/// Ports `baskfy_core.fno.directional_retest.run_symbol`.
///
/// # Errors
/// A decimal the default context cannot hold, or a polars error.
#[allow(clippy::too_many_lines)]
pub fn run_symbol(
    panel: &IndexPanel,
    f3: &F3Config,
    start: Option<NaiveDate>,
    end: Option<NaiveDate>,
) -> FnoResult<F3RetestResult> {
    let bars = &panel.bars;
    let mut rows: Vec<Vec<Cell>> = Vec::new();
    let mut skipped: BTreeMap<String, i64> = BTreeMap::new();
    let mut open: Option<Open> = None;
    let mut pending: Option<(Levels, Direction, NaiveDate)> = None;
    let need = usize::try_from(bars_needed(f3).max(0)).unwrap_or(usize::MAX);
    let hundred = Decimal::ONE_HUNDRED;
    for (i, bar) in bars.iter().enumerate() {
        let day = bar.date;
        if let Some(o) = open.as_mut() {
            let sessions = i64::try_from(i - o.entry_index).unwrap_or(i64::MAX);
            let mut lg = legs(panel, day, o, bar.close)?;
            let mark = match lg {
                Some((s, w)) => Some(sub(s, w)?),
                None => None,
            };
            // Every exit on the expiry day is the same settle (04 §11's precedence is for a
            // live mark); then the level break on the bar, the loss cut, the decay target.
            let mut reason: Option<&str> = None;
            if day >= o.expiry {
                reason = Some("HARD_EXIT");
            } else if level_broken_on_bar(o, bar, f3)? {
                reason = Some("LEVEL_BREAK");
            } else if let Some(m) = mark {
                if m >= loss_cut_mark(o.credit, f3)? {
                    reason = Some("LOSS_CUT");
                } else if m <= decay_target_mark(o.credit, f3)? {
                    reason = Some("DECAY_TARGET");
                }
            }
            if let Some(reason) = reason {
                if lg.is_none() {
                    let kind = o.strikes.option_type;
                    lg = Some((
                        intrinsic(o.strikes.short, kind, bar.close)?,
                        intrinsic(o.strikes.wing, kind, bar.close)?,
                    ));
                }
                if let Some(l) = lg {
                    rows.push(close_row(o, day, l, reason, sessions, &panel.symbol)?);
                }
                open = None;
            } else if let (false, Some((s, w)), Some(m)) = (o.added, lg, mark) {
                let working = mul(
                    o.credit,
                    sub(Decimal::ONE, py::div(f3.add_working_pct, hundred)?)?,
                )?;
                if sessions >= 1 && m <= working {
                    let lv_now = if i + 1 >= need {
                        levels(&bars[..=i], f3)?
                    } else {
                        None
                    };
                    if let Some(lv) = lv_now
                        && daily_direction(&lv) == o.direction
                    {
                        o.added = true;
                        o.lots += 1;
                        o.credit_total = add(o.credit_total, m)?;
                        let more = add(
                            leg_cost(s, true, o.lot)?,
                            leg_cost(py::max(w, five_cents()), false, o.lot)?,
                        )?;
                        o.cost_total = add(o.cost_total, more)?;
                    }
                }
            }
        }
        if open.is_none()
            && let Some((lv, direction, signal)) = pending.take()
            && start.is_none_or(|s| day >= s)
            && end.is_none_or(|e| day <= e)
        {
            open = try_entry(panel, &lv, direction, signal, day, i, f3, &mut skipped)?;
        }
        if open.is_none()
            && i + 1 >= need
            && end.is_none_or(|e| day <= e)
            && let Some(tonight) = levels(&bars[..=i], f3)?
        {
            let direction = daily_direction(&tonight);
            if direction != Direction::None {
                pending = Some((tonight, direction, day));
            }
        }
    }
    if let (Some(o), Some(last)) = (open.as_ref(), bars.last())
        && let Some(l) = legs(panel, last.date, o, last.close)?
    {
        let sessions = i64::try_from(bars.len() - 1 - o.entry_index).unwrap_or(i64::MAX);
        rows.push(close_row(
            o,
            last.date,
            l,
            "OPEN_AT_END",
            sessions,
            &panel.symbol,
        )?);
    }
    // `.sort("entry")`: one position at a time, so the rows are already in entry order.
    rows.sort_by(|a, b| match (&a[3], &b[3]) {
        (Cell::Date(x), Cell::Date(y)) => x.cmp(y),
        _ => std::cmp::Ordering::Equal,
    });
    let trades = frames::from_rows(&TRADE_COLUMNS, &rows)?;
    let summary = summarise_trades(&trades)?;
    Ok(F3RetestResult {
        symbol: panel.symbol.clone(),
        trades,
        summary,
        skipped,
    })
}

/// `_summarise`: `research.summarise` for two trades or more; one trade is its own summary with
/// no t-statistic.
fn summarise_trades(trades: &DataFrame) -> FnoResult<Summary> {
    if trades.height() == 0 {
        return Ok(Summary::empty());
    }
    if trades.height() >= 2 {
        return summarise(trades, "R", "entry");
    }
    let none = || {
        FnoError::Type(
            "float() argument must be a string or a real number, not 'NoneType'".to_owned(),
        )
    };
    let net = cols::floats(trades, "R")?
        .first()
        .copied()
        .flatten()
        .ok_or_else(none)?;
    let cost = cols::floats(trades, "cost_R")?
        .first()
        .copied()
        .flatten()
        .ok_or_else(none)?;
    let year = cols::dates(trades, "entry")?
        .first()
        .copied()
        .flatten()
        .ok_or_else(none)?
        .year();
    let mut per_year = BTreeMap::new();
    per_year.insert(year, (1, net));
    Ok(Summary {
        n: 1,
        exp_r: net,
        t_stat: 0.0,
        win: if net > 0.0 { 1.0 } else { 0.0 },
        worst: net,
        gross_r: Some(net + cost),
        cost_r: Some(cost),
        per_year,
    })
}

/// Both underlyings, keyed by symbol.
///
/// Ports `baskfy_core.fno.directional_retest.run_f3_retest`.
///
/// # Errors
/// As [`index_panel`] and [`run_symbol`].
pub fn run_f3_retest(
    contracts: &DataFrame,
    f3: &F3Config,
    start: Option<NaiveDate>,
    end: Option<NaiveDate>,
) -> FnoResult<BTreeMap<String, F3RetestResult>> {
    let mut out = BTreeMap::new();
    for symbol in &f3.underlyings {
        out.insert(
            symbol.clone(),
            run_symbol(&index_panel(contracts, symbol)?, f3, start, end)?,
        );
    }
    Ok(out)
}

/// The skipped counts and the summary of a result, serialisable (the trades frame is graded on
/// its own).
#[derive(Debug, Clone, Serialize)]
pub struct F3RetestHeadline<'a> {
    /// The underlying.
    pub symbol: &'a str,
    /// The headline numbers.
    pub summary: &'a Summary,
    /// Signals not entered, by reason.
    pub skipped: &'a BTreeMap<String, i64>,
}

impl F3RetestResult {
    /// Everything but the trades frame, for serialisation.
    #[must_use]
    pub fn headline(&self) -> F3RetestHeadline<'_> {
        F3RetestHeadline {
            symbol: &self.symbol,
            summary: &self.summary,
            skipped: &self.skipped,
        }
    }
}
