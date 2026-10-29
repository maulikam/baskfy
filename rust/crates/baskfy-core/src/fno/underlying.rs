//! Port of `baskfy_core.fno.underlying` (`decile-blueprint/packages/core/src/baskfy_core/fno/underlying.py`).
//!
//! One session's `fo_underlying_daily` rows from a window of `fo_contract_daily` (`03` §2):
//! the pure half of the nightly derivation. It joins [`continuous_futures`] and
//! [`iv_atm_frame`] and keeps the rows of `trade_date` only.
//!
//! **The levels are anchored to the session's settle** (DECISIONS-FO FO2.8): each night
//! rescales the window so that `level_c` on `trade_date` equals the held contract's settle — a
//! ratio back-adjusted series in rupees. `ret`, `rv20` and every ratio a signal reads are
//! scale-free and do not change. Rounding (house rule 8): prices to 2 places, `ret` to 10,
//! vols and basis to 6 — polars' `round`, half to even.
//!
//! Goldens: `L1/fno/underlying.derive_underlying.case_NNN.json`.

use std::collections::BTreeMap;

use chrono::NaiveDate;
use polars::prelude::{BooleanChunked, DataFrame, NamedFrom};

use super::config::{FnoError, FnoResult, SeriesConfig};
use super::series::{cols, continuous_futures};
use super::vol::iv_atm_frame;

/// The columns [`derive_underlying`] returns besides `symbol` (`03` §2; `in_ban` aside).
///
/// Ports `baskfy_core.fno.underlying.DERIVED_COLUMNS`.
pub const DERIVED_COLUMNS: [&str; 13] = [
    "held_expiry",
    "level_o",
    "level_h",
    "level_l",
    "level_c",
    "ret",
    "atr14",
    "oi_total",
    "fut_turnover_20d",
    "iv_atm",
    "rv20",
    "basis_ann",
    "ca_flag",
];

const FUTURES: [&str; 2] = ["FUTSTK", "FUTIDX"];
const OPTIONS: [&str; 2] = ["OPTSTK", "OPTIDX"];

fn filter(df: &DataFrame, keep: &[bool]) -> FnoResult<DataFrame> {
    let mask = BooleanChunked::new("".into(), keep);
    Ok(df.filter(&mask)?)
}

/// The output frame from its columns, in `symbol, DERIVED_COLUMNS` order.
struct Out {
    symbol: Vec<Option<String>>,
    held_expiry: Vec<Option<NaiveDate>>,
    prices: [Vec<Option<f64>>; 4],
    ret: Vec<Option<f64>>,
    atr14: Vec<Option<f64>>,
    oi_total: Vec<Option<i64>>,
    fut_turnover_20d: Vec<Option<f64>>,
    iv_atm: Vec<Option<f64>>,
    rv20: Vec<Option<f64>>,
    basis_ann: Vec<Option<f64>>,
    ca_flag: Vec<Option<bool>>,
}

impl Out {
    fn empty() -> Self {
        Self {
            symbol: Vec::new(),
            held_expiry: Vec::new(),
            prices: Default::default(),
            ret: Vec::new(),
            atr14: Vec::new(),
            oi_total: Vec::new(),
            fut_turnover_20d: Vec::new(),
            iv_atm: Vec::new(),
            rv20: Vec::new(),
            basis_ann: Vec::new(),
            ca_flag: Vec::new(),
        }
    }

    fn frame(self) -> FnoResult<DataFrame> {
        let [o, h, l, c] = self.prices;
        Ok(DataFrame::new(
            self.symbol.len(),
            vec![
                cols::string_column("symbol", &self.symbol),
                cols::date_column("held_expiry", &self.held_expiry)?,
                cols::float_column("level_o", &o),
                cols::float_column("level_h", &h),
                cols::float_column("level_l", &l),
                cols::float_column("level_c", &c),
                cols::float_column("ret", &self.ret),
                cols::float_column("atr14", &self.atr14),
                cols::int_column("oi_total", &self.oi_total),
                cols::float_column("fut_turnover_20d", &self.fut_turnover_20d),
                cols::float_column("iv_atm", &self.iv_atm),
                cols::float_column("rv20", &self.rv20),
                cols::float_column("basis_ann", &self.basis_ann),
                cols::bool_column("ca_flag", &self.ca_flag),
            ],
        )?)
    }
}

fn mul(a: Option<f64>, b: Option<f64>) -> Option<f64> {
    Some(a? * b?)
}

/// `fo_underlying_daily` for `trade_date`: one row per underlying, rounded for storage.
///
/// `window` is `fo_contract_daily` rows for sessions up to and including `trade_date`; a row
/// after it is refused (house rule 5). `sessions` is the exchange calendar, including sessions
/// after `trade_date` (`iv_atm` needs the sessions left to expiry).
///
/// Ports `baskfy_core.fno.underlying.derive_underlying`.
///
/// # Errors
/// `ValueError` for a window holding a later session (look-ahead); a missing column.
pub fn derive_underlying(
    window: &DataFrame,
    trade_date: NaiveDate,
    sessions: &[NaiveDate],
    config: &SeriesConfig,
) -> FnoResult<DataFrame> {
    if window.height() == 0 {
        return Out::empty().frame();
    }
    let days = cols::dates(window, "trade_date")?;
    if let Some(latest) = days.iter().flatten().max()
        && *latest > trade_date
    {
        return Err(FnoError::Value(format!(
            "the window holds {latest}, after {trade_date}: look-ahead"
        )));
    }
    let instruments = cols::strings(window, "instrument")?;
    let is_future: Vec<bool> = instruments
        .iter()
        .map(|i| i.as_deref().is_some_and(|i| FUTURES.contains(&i)))
        .collect();
    let futures = filter(window, &is_future)?;
    if futures.height() == 0 {
        return Out::empty().frame();
    }
    let series = continuous_futures(&futures, config)?;
    let on_day: Vec<bool> = cols::dates(&series, "trade_date")?
        .iter()
        .map(|d| *d == Some(trade_date))
        .collect();
    let series = filter(&series, &on_day)?;
    if series.height() == 0 {
        return Out::empty().frame();
    }
    let options_mask: Vec<bool> = instruments
        .iter()
        .zip(&days)
        .map(|(i, d)| i.as_deref().is_some_and(|i| OPTIONS.contains(&i)) && *d == Some(trade_date))
        .collect();
    let options = filter(window, &options_mask)?;
    let fut_days = cols::dates(&futures, "trade_date")?;
    let fut_today: Vec<bool> = fut_days.iter().map(|d| *d == Some(trade_date)).collect();
    let iv = iv_atm_frame(&options, &filter(&futures, &fut_today)?, sessions, config)?;
    let mut iv_by_symbol: BTreeMap<String, Vec<Option<f64>>> = BTreeMap::new();
    for (s, v) in cols::strings(&iv, "symbol")?
        .into_iter()
        .zip(cols::floats(&iv, "iv_atm")?)
    {
        if let Some(s) = s {
            iv_by_symbol.entry(s).or_default().push(v);
        }
    }
    let symbol = cols::strings(&series, "symbol")?;
    let held_expiry = cols::dates(&series, "held_expiry")?;
    let held_settle = cols::floats(&series, "held_settle")?;
    let level_c = cols::floats(&series, "level_c")?;
    let prices_in = [
        cols::floats(&series, "level_o")?,
        cols::floats(&series, "level_h")?,
        cols::floats(&series, "level_l")?,
        level_c.clone(),
    ];
    let atr_in = cols::floats(&series, "atr14")?;
    let ret_in = cols::floats(&series, "ret")?;
    let rv_in = cols::floats(&series, "rv20")?;
    let basis_in = cols::floats(&series, "basis_ann")?;
    let turn_in = cols::floats(&series, "fut_turnover_20d")?;
    let oi_in = cols::ints(&series, "oi_total")?;
    let ca_in = cols::bools(&series, "ca_flag")?;
    let mut out = Out::empty();
    // A left join on symbol (one iv row per symbol on one session; several would repeat it).
    let mut order: Vec<(usize, Option<f64>)> = Vec::new();
    for (i, s) in symbol.iter().enumerate() {
        match s.as_ref().and_then(|s| iv_by_symbol.get(s)) {
            Some(values) => order.extend(values.iter().map(|v| (i, *v))),
            None => order.push((i, None)),
        }
    }
    // `.sort("symbol")`: stable on the joined rows (nulls first).
    order.sort_by(|a, b| symbol[a.0].cmp(&symbol[b.0]));
    for (i, iv) in order {
        let scale = match level_c[i] {
            Some(c) if c > 0.0 => held_settle[i].map(|s| s / c),
            _ => None,
        };
        out.symbol.push(symbol[i].clone());
        out.held_expiry.push(held_expiry[i]);
        for (k, p) in prices_in.iter().enumerate() {
            out.prices[k].push(cols::round(mul(p[i], scale), 2));
        }
        out.atr14.push(cols::round(mul(atr_in[i], scale), 2));
        out.ret.push(cols::round(ret_in[i], 10));
        out.rv20.push(cols::round(rv_in[i], 6));
        out.iv_atm.push(cols::round(iv, 6));
        out.basis_ann.push(cols::round(basis_in[i], 6));
        out.fut_turnover_20d.push(cols::round(turn_in[i], 2));
        out.oi_total.push(oi_in[i]);
        out.ca_flag.push(Some(ca_in[i].unwrap_or(false)));
    }
    out.frame()
}
