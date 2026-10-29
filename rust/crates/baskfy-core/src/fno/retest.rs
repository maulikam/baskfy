//! Port of `baskfy_core.fno.retest` (`decile-blueprint/packages/core/src/baskfy_core/fno/retest.py`).
//!
//! The quarterly re-test (`docs/fno/04` §6, FO9): every `RESEARCH.md` family, from the table.
//! The worker reads `fo_contract_daily`; this module turns those rows into the research's two
//! panels exactly as `evidence/research/panel.py` did, runs each family through
//! [`super::research`] with the parameters of `RESEARCH.md`'s tables, and returns one
//! [`FamilyResult`] per family for `fo_backtest_run`. Nothing here decides anything.
//!
//! Slippage: FO3's measured 15:00 median half-spread replaces the research's assumption when
//! enough sessions and names were sampled (`MEASURED`), else `ASSUMED`. F3 (`F3N`, `F3B`) is the
//! daily half of `04` §11 run by [`super::directional_retest`] over the index's raw rows
//! (weeklies included), trimmed by [`trim_index_contracts`].
//!
//! Goldens: `L1/fno/retest.*.case_NNN.json`.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::{Datelike, NaiveDate};
use polars::error::PolarsError;
use polars::prelude::{DataFrame, DataType};
use serde::Serialize;

use super::condor::num;
use super::config::{F3Config, FnoError, FnoResult, py};
use super::directional_retest as dr;
use super::research::frames;
use super::research::{
    self as r, CondorMode, CondorParams, DebitStructure, FuturesFamily, FuturesParams, Summary,
};
use super::series::cols;

/// `07` §4's Tier 2E caveat, verbatim.
///
/// Ports `baskfy_core.fno.retest.TIER_2E_CAVEAT`.
pub const TIER_2E_CAVEAT: &str = "End-of-day closes, not fills. Slippage is an assumed 3 % of premium per leg per crossing (0.03 % for futures), not measured. `n` legs were modelled because they did not trade.";
/// The same caveat when FO3's measured spreads replaced the assumption.
///
/// Ports `baskfy_core.fno.retest.TIER_2E_CAVEAT_MEASURED`.
pub const TIER_2E_CAVEAT_MEASURED: &str = "End-of-day closes, not fills. Slippage is the measured 15:00 median half-spread of the names traded (FO3), not a fill. `n` legs were modelled because they did not trade.";

/// F3's caveat: the proxy's own costs, and the four rules a closing file cannot see.
///
/// Ports `baskfy_core.fno.retest.TIER_2E_CAVEAT_F3`.
#[must_use]
pub fn tier_2e_caveat_f3() -> String {
    format!(
        "End-of-day closes, not fills. Slippage is an assumed 0.5 % of premium per leg per crossing (₹0.05 floor), not measured. Only the daily half of 04 §11 is tested; not tested: {}.",
        dr::NOT_TESTED.join("; ")
    )
}

/// `04` §6: the measured number replaces the assumption at 20 sessions.
///
/// Ports `baskfy_core.fno.retest.MIN_MEASURED_SESSIONS`.
pub const MIN_MEASURED_SESSIONS: i64 = 20;
/// The stock option families need this many sampled names before a median stands for them.
///
/// Ports `baskfy_core.fno.retest.MIN_MEASURED_NAMES`.
pub const MIN_MEASURED_NAMES: usize = 20;
/// `04` §6: the re-test runs in these months.
///
/// Ports `baskfy_core.fno.retest.RETEST_MONTHS`.
pub const RETEST_MONTHS: [u32; 4] = [1, 4, 7, 10];
/// F3 trims an index's option rows to expiries at most this many days after the session.
///
/// Ports `baskfy_core.fno.retest.DIRECTIONAL_EXPIRY_DAYS`.
pub const DIRECTIONAL_EXPIRY_DAYS: i64 = 70;
/// ...and to strikes within this fraction of the session's front-month future close.
///
/// Ports `baskfy_core.fno.retest.DIRECTIONAL_STRIKE_BAND`.
pub const DIRECTIONAL_STRIKE_BAND: f64 = 0.25;
/// Stock option families run this many symbols at a time (DECISIONS-FO FO9.3).
///
/// Ports `baskfy_core.fno.retest.SYMBOL_BATCH`.
pub const SYMBOL_BATCH: i64 = 20;

const FUTURES_COLUMNS: [&str; 15] = [
    "date",
    "instrument",
    "symbol",
    "expiry",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "underlying",
    "open_interest",
    "oi_change",
    "volume",
    "turnover",
    "lot_size",
];
const OPTION_COLUMNS: [&str; 10] = [
    "date",
    "instrument",
    "symbol",
    "expiry",
    "strike",
    "option_type",
    "close",
    "settle",
    "open_interest",
    "volume",
];

/// Where a family's slippage came from.
///
/// Ports `baskfy_core.fno.retest.SlippageSource`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SlippageSource {
    /// The research's assumption.
    Assumed,
    /// FO3's measured spreads.
    Measured,
}

/// What a family trades, which decides the panel it needs and whose spreads apply.
///
/// Ports `baskfy_core.fno.retest.Scope`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Scope {
    /// NIFTY and BANKNIFTY options.
    IndexOptions,
    /// Stock options.
    StockOptions,
    /// Futures.
    Futures,
    /// The cash-futures basis.
    Basis,
    /// F3: one index's daily directional rules, on its raw rows (weeklies included).
    Directional,
}

impl Scope {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::IndexOptions => "INDEX_OPTIONS",
            Self::StockOptions => "STOCK_OPTIONS",
            Self::Futures => "FUTURES",
            Self::Basis => "BASIS",
            Self::Directional => "DIRECTIONAL",
        }
    }
}

/// One row of `RESEARCH.md`'s tables: its key, what it is, and how it was run.
///
/// Ports `baskfy_core.fno.retest.Family`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Family {
    /// The family's key.
    pub key: String,
    /// What it is.
    pub label: String,
    /// What it trades.
    pub scope: Scope,
    /// Condor parameters (B families).
    pub condor: Option<CondorParams>,
    /// The futures signal and its parameters (A families, F2).
    pub futures: Option<(FuturesFamily, FuturesParams)>,
    /// The debit structure (C families).
    pub debit: Option<DebitStructure>,
    /// F3's underlying when `scope` is `DIRECTIONAL`.
    pub directional: Option<String>,
}

fn family(key: &str, label: &str, scope: Scope) -> Family {
    Family {
        key: key.to_owned(),
        label: label.to_owned(),
        scope,
        condor: None,
        futures: None,
        debit: None,
        directional: None,
    }
}

fn stock(n_before: i64, top_n: i64) -> CondorParams {
    CondorParams {
        n_before,
        top_n,
        slip_pct: 0.03,
        ..CondorParams::default()
    }
}

fn fut(horizon: i64, k_atr: f64, trail: bool) -> FuturesParams {
    FuturesParams {
        horizon,
        k_atr,
        trail,
        charge_rolls: false,
        cost: r::FUTURES_ROUND_TRIP_COST,
    }
}

/// Every family `RESEARCH.md` reports, with the parameters of its table.
///
/// Ports `baskfy_core.fno.retest.FAMILIES`.
#[must_use]
#[allow(clippy::too_many_lines)]
pub fn families() -> Vec<Family> {
    let with_condor = |mut f: Family, c: CondorParams| {
        f.condor = Some(c);
        f
    };
    let with_futures = |mut f: Family, s: FuturesFamily, p: FuturesParams| {
        f.futures = Some((s, p));
        f
    };
    let with_debit = |mut f: Family, d: DebitStructure| {
        f.debit = Some(d);
        f
    };
    let with_dir = |mut f: Family, u: &str| {
        f.directional = Some(u.to_owned());
        f
    };
    vec![
        with_condor(
            family(
                "B4",
                "Index monthly iron condor, N = 15",
                Scope::IndexOptions,
            ),
            r::b4(),
        ),
        with_condor(
            family(
                "B4_LOSS_CLOSE",
                "B4 with the 1.5x-credit loss close (F1, M.1)",
                Scope::IndexOptions,
            ),
            r::b4_loss_close(),
        ),
        with_condor(
            family(
                "B1",
                "Stock iron condors, top 30, N = 10",
                Scope::StockOptions,
            ),
            stock(10, 30),
        ),
        with_condor(
            family(
                "B2",
                "Stock credit spreads on the trend's side, top 60, N = 15, 0.5 sigma",
                Scope::StockOptions,
            ),
            CondorParams {
                k_short: 0.5,
                k_wing: 0.5,
                mode: CondorMode::TrendCredit,
                ..stock(15, 60)
            },
        ),
        with_condor(
            family(
                "B3",
                "Stock iron condors exiting at E-4, top 30, N = 15",
                Scope::StockOptions,
            ),
            CondorParams {
                exit_n: 4,
                ..stock(15, 30)
            },
        ),
        with_debit(
            family(
                "C1",
                "Debit spreads on the breakout signal, 10 sessions",
                Scope::StockOptions,
            ),
            DebitStructure::Spread,
        ),
        with_debit(
            family(
                "C2",
                "Long ATM option on the breakout signal, 10 sessions",
                Scope::StockOptions,
            ),
            DebitStructure::Long,
        ),
        with_futures(
            family(
                "F2",
                "Stock-futures breakout, long only, 3 ATR chandelier, 40 sessions, rolls costed (F2)",
                Scope::Futures,
            ),
            FuturesFamily::A0Long,
            r::f2_spec(),
        ),
        with_futures(
            family(
                "A0",
                "Stock-futures breakout aligned, no OI filter, both sides, 3 ATR chandelier, 40 sessions",
                Scope::Futures,
            ),
            FuturesFamily::A0,
            fut(40, 3.0, true),
        ),
        with_futures(
            family(
                "A1",
                "Stock-futures breakout with OI rising, 10 sessions, 2 ATR",
                Scope::Futures,
            ),
            FuturesFamily::A1,
            fut(10, 2.0, false),
        ),
        with_futures(
            family(
                "A1R",
                "A1 aligned with NIFTY's regime, 10 sessions, 2 ATR",
                Scope::Futures,
            ),
            FuturesFamily::A1R,
            fut(10, 2.0, false),
        ),
        with_futures(
            family(
                "A3",
                "One-day long/short buildup, 5 sessions, 2 ATR",
                Scope::Futures,
            ),
            FuturesFamily::A3,
            fut(5, 2.0, false),
        ),
        with_futures(
            family(
                "A2",
                "Weekly cross-sectional momentum, top/bottom 10, 5 sessions, 3 ATR",
                Scope::Futures,
            ),
            FuturesFamily::A2,
            fut(5, 3.0, false),
        ),
        with_futures(
            family(
                "A4",
                "Index futures 20-day breakout with 50-day average, 20 sessions, 2 ATR",
                Scope::Futures,
            ),
            FuturesFamily::A4,
            fut(20, 2.0, false),
        ),
        family(
            "E",
            "Cash-futures carry (front month, >= 5 days to expiry)",
            Scope::Basis,
        ),
        with_dir(
            family(
                "F3N",
                "F3 directional credit spread, NIFTY weekly: the daily half of 04 §11 (M.5)",
                Scope::Directional,
            ),
            "NIFTY",
        ),
        with_dir(
            family(
                "F3B",
                "F3 directional credit spread, BANKNIFTY monthly: the daily half of 04 §11 (M.5)",
                Scope::Directional,
            ),
            "BANKNIFTY",
        ),
    ]
}

/// Every family's key.
///
/// Ports `baskfy_core.fno.retest.FAMILY_KEYS`.
#[must_use]
pub fn family_keys() -> BTreeSet<String> {
    families().into_iter().map(|f| f.key).collect()
}

/// `panel.py`'s two files, from `fo_contract_daily` rows.
///
/// Ports `baskfy_core.fno.retest.Panels`.
#[derive(Debug, Clone)]
pub struct Panels {
    /// `futures.parquet`.
    pub futures: DataFrame,
    /// `options.parquet`.
    pub options: DataFrame,
}

/// An options panel with no rows and `panel.py`'s columns (a batch with nothing stored).
///
/// Ports `baskfy_core.fno.retest.empty_options`.
///
/// # Errors
/// A polars error (none in practice).
pub fn empty_options() -> FnoResult<DataFrame> {
    Ok(DataFrame::new(
        0,
        vec![
            cols::date_column("date", &[])?,
            cols::string_column("instrument", &[]),
            cols::string_column("symbol", &[]),
            cols::date_column("expiry", &[])?,
            cols::float_column("strike", &[]),
            cols::string_column("option_type", &[]),
            cols::float_column("close", &[]),
            cols::float_column("settle", &[]),
            cols::int_column("open_interest", &[]),
            cols::int_column("volume", &[]),
        ],
    )?)
}

/// `symbol`'s FUTIDX rows, and its OPTIDX rows within [`DIRECTIONAL_EXPIRY_DAYS`] of the
/// session and [`DIRECTIONAL_STRIKE_BAND`] of that session's front-month future close.
///
/// Ports `baskfy_core.fno.retest.trim_index_contracts`.
///
/// # Errors
/// A missing column or a polars error.
pub fn trim_index_contracts(contracts: &DataFrame, symbol: &str) -> FnoResult<DataFrame> {
    let frame = frames::renamed(contracts)?;
    let sym = cols::strings(&frame, "symbol")?;
    let instrument = cols::strings(&frame, "instrument")?;
    let date = cols::dates(&frame, "date")?;
    let expiry = cols::dates(&frame, "expiry")?;
    let close = cols::floats(&frame, "close")?;
    let strike = cols::floats(&frame, "strike")?;
    let mine: Vec<usize> = (0..frame.height())
        .filter(|&i| sym[i].as_deref() == Some(symbol))
        .collect();
    let futures: Vec<usize> = mine
        .iter()
        .copied()
        .filter(|&i| instrument[i].as_deref() == Some("FUTIDX"))
        .collect();
    let mut front: HashMap<NaiveDate, usize> = HashMap::new();
    for &i in &futures {
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
    let band = DIRECTIONAL_STRIKE_BAND;
    let options: Vec<usize> = mine
        .iter()
        .copied()
        .filter(|&i| {
            if instrument[i].as_deref() != Some("OPTIDX") {
                return false;
            }
            let (Some(d), Some(e)) = (date[i], expiry[i]) else {
                return false;
            };
            let within = d
                .checked_add_signed(chrono::TimeDelta::days(DIRECTIONAL_EXPIRY_DAYS))
                .is_some_and(|lim| e <= lim);
            if !within {
                return false;
            }
            let Some(f) = front.get(&d).and_then(|&j| close[j]) else {
                return false;
            };
            strike[i].is_some_and(|k| k >= f * (1.0 - band) && k <= f * (1.0 + band))
        })
        .collect();
    let mut idx = futures;
    idx.extend(options);
    frames::take(&frame, &idx)
}

/// An options loader over an in-memory options panel.
///
/// Ports `baskfy_core.fno.retest.slice_loader`.
pub fn slice_loader(options: &DataFrame) -> impl Fn(&BTreeSet<String>) -> FnoResult<DataFrame> {
    let options = options.clone();
    move |symbols: &BTreeSet<String>| {
        let sym = cols::strings(&options, "symbol")?;
        let idx: Vec<usize> = (0..options.height())
            .filter(|&i| sym[i].as_ref().is_some_and(|s| symbols.contains(s)))
            .collect();
        frames::take(&options, &idx)
    }
}

/// `(date, symbol)`, nulls kept.
type SessionKey = (Option<NaiveDate>, Option<String>);

/// `evidence/research/panel.py` over `fo_contract_daily` (or the day files) rows.
///
/// Ports `baskfy_core.fno.retest.panels_from_contracts`.
///
/// # Errors
/// A missing column or a polars error.
pub fn panels_from_contracts(contracts: &DataFrame) -> FnoResult<Panels> {
    let frame = frames::renamed(contracts)?;
    let instrument = cols::strings(&frame, "instrument")?;
    let sym = cols::strings(&frame, "symbol")?;
    let date = cols::dates(&frame, "date")?;
    let expiry = cols::dates(&frame, "expiry")?;
    let oi = cols::ints(&frame, "open_interest")?;
    let volume = cols::ints(&frame, "volume")?;
    let fut: Vec<usize> = (0..frame.height())
        .filter(|&i| matches!(instrument[i].as_deref(), Some("FUTSTK" | "FUTIDX")))
        .collect();
    // near2: the two smallest distinct expiries per (date, symbol), a null sorting first.
    let mut listed: HashMap<SessionKey, BTreeSet<Option<NaiveDate>>> = HashMap::new();
    for &i in &fut {
        listed
            .entry((date[i], sym[i].clone()))
            .or_default()
            .insert(expiry[i]);
    }
    let near2: HashMap<(NaiveDate, String), Vec<NaiveDate>> = listed
        .into_iter()
        .filter_map(|((d, s), es)| {
            Some((
                (d?, s?),
                es.into_iter().take(2).flatten().collect::<Vec<_>>(),
            ))
        })
        .collect();
    let opts: Vec<usize> = (0..frame.height())
        .filter(|&i| {
            let wanted = match instrument[i].as_deref() {
                Some("OPTSTK") => true,
                Some("OPTIDX") => sym[i].as_deref().is_some_and(r::is_index),
                _ => false,
            };
            if !wanted {
                return false;
            }
            let (Some(d), Some(s), Some(e)) = (date[i], sym[i].clone(), expiry[i]) else {
                return false;
            };
            if !near2.get(&(d, s)).is_some_and(|v| v.contains(&e)) {
                return false;
            }
            oi[i].is_some_and(|x| x > 0) || volume[i].is_some_and(|x| x > 0)
        })
        .collect();
    let mut fut_sorted = fut;
    fut_sorted
        .sort_by(|a, b| (date[*a], &sym[*a], expiry[*a]).cmp(&(date[*b], &sym[*b], expiry[*b])));
    let futures = frames::take(&frame, &fut_sorted)?.select(FUTURES_COLUMNS)?;
    let options = frames::take(&frame, &opts)?.select(OPTION_COLUMNS)?;
    Ok(Panels { futures, options })
}

/// The per-leg slippage a family ran at, and where it came from.
///
/// Ports `baskfy_core.fno.retest.Slippage`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Slippage {
    /// The measured share of premium, or `None` (the family's own assumption).
    pub pct: Option<f64>,
    /// Where it came from.
    pub source: SlippageSource,
    /// How many names stood behind a measured number.
    pub names: i64,
}

fn assumed() -> Slippage {
    Slippage {
        pct: None,
        source: SlippageSource::Assumed,
        names: 0,
    }
}

/// `statistics.median` of floats.
fn py_median(values: &[f64]) -> Option<f64> {
    let mut v = values.to_vec();
    v.sort_by(f64::total_cmp);
    let n = v.len();
    if n == 0 {
        return None;
    }
    if n % 2 == 1 {
        return Some(v[n / 2]);
    }
    Some((v[n / 2 - 1] + v[n / 2]) / 2.0)
}

/// `measured`: symbol -> (median half-spread ÷ mid, sessions) from FO3.
///
/// Ports `baskfy_core.fno.retest.family_slippage`.
#[must_use]
pub fn family_slippage(family: &Family, measured: &BTreeMap<String, (f64, i64)>) -> Slippage {
    match family.scope {
        Scope::IndexOptions => {
            let ready: Vec<f64> = r::INDEX_UNDERLYINGS
                .iter()
                .filter_map(|s| measured.get(*s))
                .filter(|v| v.1 >= MIN_MEASURED_SESSIONS)
                .map(|v| v.0)
                .collect();
            if ready.len() == r::INDEX_UNDERLYINGS.len() {
                let worst = ready
                    .iter()
                    .copied()
                    .reduce(|a, b| if b > a { b } else { a });
                return Slippage {
                    pct: worst,
                    source: SlippageSource::Measured,
                    names: i64::try_from(ready.len()).unwrap_or(i64::MAX),
                };
            }
            assumed()
        }
        Scope::StockOptions => {
            let ready: Vec<f64> = measured
                .iter()
                .filter(|(s, v)| !r::is_index(s) && v.1 >= MIN_MEASURED_SESSIONS)
                .map(|(_, v)| v.0)
                .collect();
            if ready.len() >= MIN_MEASURED_NAMES {
                return Slippage {
                    pct: py_median(&ready),
                    source: SlippageSource::Measured,
                    names: i64::try_from(ready.len()).unwrap_or(i64::MAX),
                };
            }
            assumed()
        }
        _ => assumed(),
    }
}

/// A value of a family's `params` dict.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum Param {
    /// An `int`.
    Int(i64),
    /// A `float`.
    Float(f64),
    /// A `bool`.
    Bool(bool),
    /// A `str`.
    Str(String),
    /// A list of `str`.
    List(Vec<String>),
}

/// A value of a family's `per_year` dict: a year's `{n, exp_r}`, the basis study's quantiles,
/// or its share.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum PerYear {
    /// `{"n": n, "exp_r": round(exp, 4)}`.
    Year {
        /// Trades that year.
        n: i64,
        /// Their mean R, to four places.
        exp_r: f64,
    },
    /// `{str(q): round(v, 4)}`.
    Quantiles(BTreeMap<String, f64>),
    /// A float.
    Float(f64),
}

/// One `fo_backtest_run` row's content.
///
/// Ports `baskfy_core.fno.retest.FamilyResult`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct FamilyResult {
    /// The family's key.
    pub family: String,
    /// Its label.
    pub label: String,
    /// The parameters it ran at.
    pub params: BTreeMap<String, Param>,
    /// The caveat every row carries.
    pub caveat: String,
    /// The first session of the sample.
    pub sample_from: NaiveDate,
    /// The last session of the sample.
    pub sample_to: NaiveDate,
    /// Trades (sessions for the basis study).
    pub n: i64,
    /// Net expectancy in R, to four places; `None` with no trades.
    pub net_r: Option<f64>,
    /// Gross expectancy in R (option families), to four places.
    pub gross_r: Option<f64>,
    /// By year (or the basis study's numbers).
    pub per_year: BTreeMap<String, PerYear>,
    /// Where the slippage came from.
    pub slippage_source: SlippageSource,
}

fn dec_str(d: rust_decimal::Decimal) -> Param {
    Param::Str(py::str(d))
}

fn params_of(family: &Family, slippage: &Slippage) -> BTreeMap<String, Param> {
    let mut out = BTreeMap::new();
    let mut put = |k: &str, v: Param| {
        out.insert(k.to_owned(), v);
    };
    put("label", Param::Str(family.label.clone()));
    put("scope", Param::Str(family.scope.value().to_owned()));
    if let Some(c) = &family.condor {
        put("n_before", Param::Int(c.n_before));
        put("k_short", Param::Float(c.k_short));
        put("k_wing", Param::Float(c.k_wing));
        put("profit_take", Param::Float(c.profit_take));
        put("exit_n", Param::Int(c.exit_n));
        put("slip_pct", Param::Float(slippage.pct.unwrap_or(c.slip_pct)));
        put("top_n", Param::Int(c.top_n));
        put("mode", Param::Str(c.mode.value().to_owned()));
        put("loss_mult", Param::Float(c.loss_mult));
    }
    if let Some((sig, p)) = &family.futures {
        put("signal", Param::Str(sig.value().to_owned()));
        put("horizon", Param::Int(p.horizon));
        put("k_atr", Param::Float(p.k_atr));
        put("trail", Param::Bool(p.trail));
        put("charge_rolls", Param::Bool(p.charge_rolls));
        put("round_trip_cost", Param::Float(p.cost));
    }
    if let Some(d) = family.debit {
        put("structure", Param::Str(d.value().to_owned()));
        put("horizon", Param::Int(10));
        put("slip_pct", Param::Float(0.03));
    }
    if let Some(u) = &family.directional {
        let f3 = F3Config::default();
        let costs = dr::index_costs();
        put("underlying", Param::Str(u.clone()));
        put(
            "expiry_kind",
            Param::Str(if u == "NIFTY" { "weekly" } else { "monthly" }.to_owned()),
        );
        put("pivot_lookback", Param::Int(f3.pivot_lookback));
        put("trend_sessions", Param::Int(f3.trend_sessions));
        put("weekly_sessions", Param::Int(f3.weekly_sessions));
        put("distance_pct", dec_str(f3.distance_pct));
        put("wing_pct", dec_str(f3.wing_pct));
        put("short_premium_min_inr", dec_str(f3.short_premium_min_inr));
        put("decay_target_pct", dec_str(f3.decay_target_pct));
        put("loss_cut_mult", dec_str(f3.loss_cut_mult));
        put("level_buffer_pct", dec_str(f3.level_buffer_pct));
        put("add_working_pct", dec_str(f3.add_working_pct));
        put("slip_pct", Param::Float(costs.slip_pct));
        put("slip_min_inr", Param::Float(costs.slip_min));
        put(
            "not_tested",
            Param::List(dr::NOT_TESTED.iter().map(|s| (*s).to_owned()).collect()),
        );
    }
    if slippage.source == SlippageSource::Measured {
        put("measured_names", Param::Int(slippage.names));
    }
    out
}

fn per_year_of(summary: &Summary) -> BTreeMap<String, PerYear> {
    summary
        .per_year
        .iter()
        .map(|(y, (n, exp))| {
            (
                y.to_string(),
                PerYear::Year {
                    n: *n,
                    exp_r: num::py_round(*exp, 4),
                },
            )
        })
        .collect()
}

/// An options loader: `symbols -> options panel rows` for those symbols.
pub type OptionsLoader<'a> = &'a dyn Fn(&BTreeSet<String>) -> FnoResult<DataFrame>;
/// An index loader: `symbol -> that index's raw bhavcopy rows`.
pub type IndexLoader<'a> = &'a dyn Fn(&str) -> FnoResult<DataFrame>;

/// `run_family`'s keyword knobs.
#[derive(Clone, Copy)]
pub struct RunKnobs<'a> {
    /// A continuous series shared between families (`None`: built here).
    pub cont: Option<&'a DataFrame>,
    /// Symbols per stock-option batch.
    pub batch: i64,
    /// The index's raw rows, for F3.
    pub load_index: Option<IndexLoader<'a>>,
}

impl Default for RunKnobs<'_> {
    fn default() -> Self {
        Self {
            cont: None,
            batch: SYMBOL_BATCH,
            load_index: None,
        }
    }
}

/// `pl.concat(parts)` (vertical, strict): every part must carry the first part's dtypes; a
/// later part's `Null`-typed column is cast to the first's dtype, and any other mismatch
/// (notably a first part whose column is `Null` and a later part whose is not) is polars'
/// `SchemaError`, as Python raises it.
fn concat(parts: &[DataFrame]) -> FnoResult<DataFrame> {
    let Some(first) = parts.first() else {
        return Ok(DataFrame::empty());
    };
    let mut out = first.clone();
    for p in &parts[1..] {
        let mut cast = p.clone();
        for c in first.columns() {
            let want = c.dtype();
            let got = cast.column(c.name())?.dtype().clone();
            if got == *want {
                continue;
            }
            if got != DataType::Null {
                return Err(PolarsError::SchemaMismatch(
                    format!(
                        "type {got} is incompatible with expected type {want}\n\nThis error occurred with the following context stack:\n\t[1] failed to vstack column '{}'",
                        c.name()
                    )
                    .into(),
                )
                .into());
            }
            let col = cast.column(c.name())?.cast(want)?;
            cast.with_column(col)?;
        }
        out.vstack_mut(&cast)?;
    }
    Ok(out)
}

/// Run one family. `knobs.cont` may share one continuous series between families.
///
/// Ports `baskfy_core.fno.retest.run_family`.
///
/// # Errors
/// `ValueError` for an empty panel or an F3 family without `load_index`; whatever the family's
/// research function raises (notably `summarise`'s `TypeError` on exactly one trade, R2.9).
#[allow(clippy::too_many_lines)]
pub fn run_family(
    family: &Family,
    futures: &DataFrame,
    load_options: OptionsLoader<'_>,
    measured: &BTreeMap<String, (f64, i64)>,
    knobs: RunKnobs<'_>,
) -> FnoResult<FamilyResult> {
    if futures.height() == 0 {
        return Err(FnoError::Value(
            "the re-test needs futures rows; the panel is empty".to_owned(),
        ));
    }
    let dates = cols::dates(futures, "date")?;
    let (Some(first), Some(last)) = (
        dates.iter().flatten().min().copied(),
        dates.iter().flatten().max().copied(),
    ) else {
        return Err(FnoError::Type("the panel's dates are not dates".to_owned()));
    };
    let slippage = family_slippage(family, measured);
    let mut caveat = if slippage.source == SlippageSource::Measured {
        TIER_2E_CAVEAT_MEASURED.to_owned()
    } else {
        TIER_2E_CAVEAT.to_owned()
    };
    if family.scope == Scope::Directional {
        caveat = tier_2e_caveat_f3();
    }
    let result = |n: i64,
                  net: Option<f64>,
                  gross: Option<f64>,
                  per_year: BTreeMap<String, PerYear>|
     -> FamilyResult {
        FamilyResult {
            family: family.key.clone(),
            label: family.label.clone(),
            params: params_of(family, &slippage),
            caveat: caveat.clone(),
            sample_from: first,
            sample_to: last,
            n,
            net_r: net.filter(|_| n != 0).map(|x| num::py_round(x, 4)),
            gross_r: gross.filter(|_| n != 0).map(|x| num::py_round(x, 4)),
            per_year,
            slippage_source: slippage.source,
        }
    };
    if family.scope == Scope::Basis {
        let instrument = cols::strings(futures, "instrument")?;
        let underlying = cols::floats(futures, "underlying")?;
        let any = (0..futures.height())
            .any(|i| instrument[i].as_deref() == Some("FUTSTK") && underlying[i].is_some());
        if !any {
            return Ok(result(0, None, None, BTreeMap::new()));
        }
        let study = r::basis_study(futures, 0.0045)?;
        let mut per_year = BTreeMap::new();
        per_year.insert(
            "quantiles_ann".to_owned(),
            PerYear::Quantiles(
                study
                    .quantiles
                    .iter()
                    .map(|(q, v)| (num::py_float_repr(*q), num::py_round(*v, 4)))
                    .collect(),
            ),
        );
        per_year.insert(
            "share_net_above_7pct".to_owned(),
            PerYear::Float(num::py_round(study.share_net_above_7pct, 4)),
        );
        return Ok(result(study.sessions, None, None, per_year));
    }
    if family.scope == Scope::Directional {
        let (Some(symbol), Some(load_index)) = (&family.directional, knobs.load_index) else {
            return Err(FnoError::Value(format!(
                "family {} needs the index's raw rows (load_index): the options panel drops the weeklies F3 trades",
                family.key
            )));
        };
        let contracts = trim_index_contracts(&load_index(symbol)?, symbol)?;
        let instrument = cols::strings(&contracts, "instrument")?;
        if !instrument.iter().any(|i| i.as_deref() == Some("OPTIDX")) {
            return Ok(result(0, None, None, BTreeMap::new()));
        }
        let got = dr::run_symbol(
            &dr::index_panel(&contracts, symbol)?,
            &F3Config::default(),
            None,
            None,
        )?;
        return Ok(result(
            got.summary.n,
            Some(got.summary.exp_r),
            got.summary.gross_r,
            per_year_of(&got.summary),
        ));
    }
    let built;
    let series = match knobs.cont {
        Some(c) => c,
        None => {
            built = r::continuous(futures)?;
            &built
        }
    };
    if let Some((signal, params)) = &family.futures {
        let panel = r::futures_panel(series)?;
        let signals = r::futures_signals(&panel, *signal)?;
        let summary = r::summarise(
            &r::simulate_futures(&panel, &signals, params)?,
            "R",
            "entry",
        )?;
        return Ok(result(
            summary.n,
            Some(summary.exp_r),
            None,
            per_year_of(&summary),
        ));
    }
    let universe: BTreeSet<String> = if family.scope == Scope::IndexOptions {
        r::INDEX_UNDERLYINGS
            .iter()
            .map(|s| (*s).to_owned())
            .collect()
    } else {
        r::liquid_symbols(series, family.condor.as_ref().map_or(0, |c| c.top_n))?
    };
    let ordered: Vec<String> = universe.into_iter().collect();
    let step = usize::try_from(knobs.batch.max(1)).unwrap_or(1);
    let mut parts: Vec<DataFrame> = Vec::new();
    for chunk in ordered.chunks(step) {
        let symbols: BTreeSet<String> = chunk.iter().cloned().collect();
        let panel_o = r::build_option_panel(&load_options(&symbols)?, futures, series, &symbols)?;
        parts.push(trades_of(family, &panel_o, &slippage)?);
    }
    let traded: Vec<DataFrame> = parts.into_iter().filter(|p| p.height() > 0).collect();
    let trades = if traded.is_empty() {
        DataFrame::empty()
    } else {
        let all = concat(&traded)?;
        let entry = cols::dates(&all, "entry")?;
        let sym = cols::strings(&all, "symbol")?;
        let mut idx: Vec<usize> = (0..all.height()).collect();
        idx.sort_by(|a, b| (entry[*a], &sym[*a]).cmp(&(entry[*b], &sym[*b])));
        frames::take(&all, &idx)?
    };
    let r_column = if family.debit.is_some() {
        "R_spread"
    } else {
        "R"
    };
    let summary = r::summarise(&trades, r_column, "entry")?;
    Ok(result(
        summary.n,
        Some(summary.exp_r),
        summary.gross_r,
        per_year_of(&summary),
    ))
}

fn trades_of(family: &Family, panel: &r::OptionPanel, slippage: &Slippage) -> FnoResult<DataFrame> {
    if let Some(d) = family.debit {
        return r::debit_trades(panel, 10, d);
    }
    let Some(condor) = &family.condor else {
        return Err(FnoError::Value(format!(
            "family {} has no parameters",
            family.key
        )));
    };
    let params = match slippage.pct {
        Some(p) => CondorParams {
            slip_pct: p,
            ..condor.clone()
        },
        None => condor.clone(),
    };
    Ok(r::condor_trades(panel, &params)?.trades)
}

/// `04` §6: once in each of January, April, July and October.
///
/// Ports `baskfy_core.fno.retest.retest_due`.
#[must_use]
pub fn retest_due(today: NaiveDate, last_run: Option<NaiveDate>) -> bool {
    if !RETEST_MONTHS.contains(&today.month()) {
        return false;
    }
    last_run.is_none_or(|l| (l.year(), l.month()) != (today.year(), today.month()))
}

/// Every family, one continuous series shared between them.
///
/// Ports `baskfy_core.fno.retest.run_all`.
///
/// # Errors
/// The first family that raises.
pub fn run_all(
    futures: &DataFrame,
    load_options: OptionsLoader<'_>,
    measured: &BTreeMap<String, (f64, i64)>,
    families: &[Family],
    mut progress: Option<&mut dyn FnMut(&FamilyResult)>,
    load_index: Option<IndexLoader<'_>>,
) -> FnoResult<Vec<FamilyResult>> {
    let cont = r::continuous(futures)?;
    let mut out = Vec::with_capacity(families.len());
    for f in families {
        let res = run_family(
            f,
            futures,
            load_options,
            measured,
            RunKnobs {
                cont: Some(&cont),
                batch: SYMBOL_BATCH,
                load_index,
            },
        )?;
        if let Some(p) = progress.as_mut() {
            p(&res);
        }
        out.push(res);
    }
    Ok(out)
}
