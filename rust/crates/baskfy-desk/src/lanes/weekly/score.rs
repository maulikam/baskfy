//! The Momentum Quality Score /100 — `baskfy_core.score` bound to the desk's config, as
//! `app/scoring.py` binds it (`load_scan`, `audit`, `apply_filters`, `score`, `stop_from_vol`).
//!
//! Every formula is the pandas one, operation for operation, because a 1-dp rounding can turn a
//! last-bit difference into a different SCORE and a different rank. Graded on
//! `fixtures/weekly/scans.json` and `corpus.json`.

use std::collections::HashSet;

use super::frame::{Column, CsvError, Frame, read_csv};
use super::pandas::{clip, nargsort, np_clip, np_round, py_round, quantile, rank_pct};
use crate::wire::{PyDict, PyValue};

/// The eleven scoring constants (`baskfy_core.score.ScoringConfig`), the desk's values.
#[derive(Debug, Clone, PartialEq)]
pub struct ScoringConfig {
    /// `EXCLUDED_SYMBOLS`.
    pub excluded_symbols: Vec<String>,
    /// `REJECT_SERIES`.
    pub reject_series: Vec<String>,
    /// `MIN_MEDIAN_DAILY_VALUE`.
    pub min_median_daily_value: f64,
    /// `MAX_AWAY_FROM_HIGH`.
    pub max_away_from_high: f64,
    /// `MAX_CIRCUITS_3M`.
    pub max_circuits_3m: f64,
    /// `PENALTY_CIRCUITS_1Y`.
    pub penalty_circuits_1y: f64,
    /// `MOMENTUM_BLEND`, in its order.
    pub momentum_blend: Vec<(&'static str, f64)>,
    /// `SHARPE_BLEND`, in its order.
    pub sharpe_blend: Vec<(&'static str, f64)>,
    /// `STOP_VOL_MULT`.
    pub stop_vol_mult: f64,
    /// `STOP_MIN`.
    pub stop_min: f64,
    /// `STOP_MAX`.
    pub stop_max: f64,
}

impl Default for ScoringConfig {
    fn default() -> Self {
        Self {
            excluded_symbols: vec!["SGBDE31III".to_owned()],
            reject_series: vec!["BE".to_owned(), "BZ".to_owned()],
            min_median_daily_value: 5e7,
            max_away_from_high: -30.0,
            max_circuits_3m: 5.0,
            penalty_circuits_1y: 8.0,
            momentum_blend: vec![
                ("one_month", 0.10),
                ("three_months", 0.30),
                ("six_months", 0.30),
                ("nine_months", 0.15),
                ("one_year", 0.15),
            ],
            sharpe_blend: vec![
                ("three_months", 0.35),
                ("six_months", 0.35),
                ("nine_months", 0.15),
                ("one_year", 0.15),
            ],
            stop_vol_mult: 2.2,
            stop_min: 0.08,
            stop_max: 0.12,
        }
    }
}

/// `SHP_COLS`.
pub const SHP_COLS: [&str; 5] = [
    "sharpe_return_one_month",
    "sharpe_return_three_months",
    "sharpe_return_six_months",
    "sharpe_return_nine_months",
    "sharpe_return_one_year",
];

impl ScoringConfig {
    /// `ret_cols(cfg)`.
    #[must_use]
    pub fn ret_cols(&self) -> Vec<String> {
        self.momentum_blend
            .iter()
            .map(|(w, _)| format!("absolute_return_{w}"))
            .collect()
    }

    /// `required(cfg)`: every column a scan must carry, in order.
    #[must_use]
    pub fn required(&self) -> Vec<String> {
        let mut out: Vec<String> = [
            "symbol",
            "series",
            "date",
            "close",
            "marketcap",
            "median_volume_one_year",
            "rsi_one_month",
            "volatility_one_year",
            "beta",
            "circuits_three_months",
            "circuits_one_year",
            "positive_days_percent_three_months",
            "positive_days_percent_six_months",
            "ma_20",
            "ma_50",
            "ma_100",
            "ma_200",
            "away_from_high_one_year",
            "is_nifty_fno",
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
        out.extend(self.ret_cols());
        out.extend(SHP_COLS.iter().map(|s| (*s).to_owned()));
        out
    }
}

/// Why a scan cannot be scored (Python raises; `/analyze` answers 500).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ScanError(pub String);

impl From<CsvError> for ScanError {
    fn from(e: CsvError) -> Self {
        Self(e.0)
    }
}

fn py_list_repr(items: &[String]) -> String {
    let inner: Vec<String> = items
        .iter()
        .map(|s| baskfy_providers::py::repr_str(s))
        .collect();
    format!("[{}]", inner.join(", "))
}

/// `drop_duplicates(subset="symbol").reset_index(drop=True)`: keep each symbol's first row.
#[must_use]
pub fn dedupe(frame: &Frame) -> Frame {
    let Some(sym) = frame.col("symbol") else {
        return frame.clone();
    };
    let mut seen: HashSet<String> = HashSet::new();
    let mut seen_na = false;
    let mut rows = Vec::new();
    for i in 0..frame.nrows {
        if sym.is_na(i) {
            if !seen_na {
                seen_na = true;
                rows.push(i);
            }
            continue;
        }
        if seen.insert(sym.py_str(i)) {
            rows.push(i);
        }
    }
    frame.take(&rows)
}

/// `app.scoring.load_scan`: read, check every required column, drop duplicate symbols.
///
/// # Errors
/// An unreadable CSV, or `ValueError: Scan CSV missing columns: [...]`.
pub fn load_scan(raw: &[u8], cfg: &ScoringConfig) -> Result<Frame, ScanError> {
    let u = read_csv(raw)?;
    let missing: Vec<String> = cfg.required().into_iter().filter(|c| !u.has(c)).collect();
    if !missing.is_empty() {
        return Err(ScanError(format!(
            "ValueError: Scan CSV missing columns: {}",
            py_list_repr(&missing)
        )));
    }
    Ok(dedupe(&u))
}

fn num(u: &Frame, name: &str) -> Result<Vec<f64>, ScanError> {
    u.f64s(name).ok_or_else(|| {
        ScanError(format!(
            "TypeError: column {name} is not numeric; pandas cannot compare it"
        ))
    })
}

/// A column on the **right** of a series-to-series comparison (`u.close > u.ma_20`). pandas
/// compares a `float64` series with an `object` one element by element and raises
/// `TypeError: '>' not supported between instances of 'float' and 'NoneType'` on the first
/// `None`; an all-`None` `object` column is what a generated scan's never-computed factor is
/// (`scan_source`, e.g. `ma_200` on a history shorter than 200 bars). `read_csv` never builds
/// one (an all-empty column is `float64`), so an uploaded scan is unaffected.
fn num_rhs(u: &Frame, name: &str) -> Result<Vec<f64>, ScanError> {
    if u.nrows > 0 && matches!(u.col(name), Some(Column::Str(_))) {
        return Err(ScanError(
            "TypeError: '>' not supported between instances of 'float' and 'NoneType'".to_owned(),
        ));
    }
    num(u, name)
}

#[allow(clippy::cast_precision_loss)]
fn mean_pct(flags: &[bool]) -> f64 {
    let n = flags.len();
    if n == 0 {
        return f64::NAN;
    }
    let hits = flags.iter().filter(|b| **b).count();
    hits as f64 / n as f64 * 100.0
}

/// `value_counts().to_dict()`: counts descending, ties in first-seen order (orjson writes a
/// non-string key as its `str`).
fn value_counts(col: &Column, n: usize) -> Vec<(String, i64)> {
    let mut keys: Vec<String> = Vec::new();
    let mut counts: Vec<i64> = Vec::new();
    for i in 0..n {
        if col.is_na(i) {
            continue;
        }
        let key = col.py_str(i);
        if let Some(k) = keys.iter().position(|x| *x == key) {
            counts[k] += 1;
        } else {
            keys.push(key);
            counts.push(1);
        }
    }
    #[allow(clippy::cast_precision_loss)]
    let as_f: Vec<f64> = counts.iter().map(|c| *c as f64).collect();
    nargsort(&as_f, false)
        .into_iter()
        .map(|i| (keys[i].clone(), counts[i]))
        .collect()
}

/// `audit(u)`, in Python's key order. `series_counts`' values are numpy ints (orjson writes them
/// as numbers; `json.dump(default=str)` as strings — see `execute`'s log).
///
/// # Errors
/// A non-numeric column where a number is compared.
pub fn audit(u: &Frame, cfg: &ScoringConfig) -> Result<PyDict, ScanError> {
    let n = u.nrows;
    let mut out = PyDict::new();
    out.insert("rows", PyValue::Int(i64::try_from(n).unwrap_or(i64::MAX)));
    let dates = u
        .col("date")
        .ok_or_else(|| ScanError("KeyError: 'date'".to_owned()))?;
    let mut uniq: Vec<String> = Vec::new();
    for i in 0..n {
        let s = dates.py_str(i);
        if !uniq.contains(&s) {
            uniq.push(s);
        }
    }
    uniq.sort();
    out.insert(
        "scan_date",
        PyValue::List(uniq.into_iter().map(PyValue::Str).collect()),
    );
    let series = u
        .col("series")
        .ok_or_else(|| ScanError("KeyError: 'series'".to_owned()))?;
    let mut counts = PyDict::new();
    for (k, c) in value_counts(series, n) {
        counts.insert(k, PyValue::Int(c));
    }
    out.insert("series_counts", PyValue::Map(counts));
    let mut nulls = 0_i64;
    for c in cfg.required() {
        if let Some(col) = u.col(&c) {
            nulls += i64::try_from((0..n).filter(|&i| col.is_na(i)).count()).unwrap_or(0);
        }
    }
    out.insert("null_cells", PyValue::Int(nulls));
    let close = num(u, "close")?;
    let gt = |a: &[f64], b: &[f64]| -> Vec<bool> { a.iter().zip(b).map(|(x, y)| x > y).collect() };
    out.insert(
        "breadth_above_20dma",
        PyValue::Float(py_round(mean_pct(&gt(&close, &num_rhs(u, "ma_20")?)), 1)),
    );
    out.insert(
        "breadth_above_50dma",
        PyValue::Float(py_round(mean_pct(&gt(&close, &num_rhs(u, "ma_50")?)), 1)),
    );
    let r1m = num(u, "absolute_return_one_month")?;
    out.insert(
        "positive_1m_pct",
        PyValue::Float(py_round(
            mean_pct(&r1m.iter().map(|x| *x > 0.0).collect::<Vec<_>>()),
            1,
        )),
    );
    let sh = num(u, "sharpe_return_one_year")?;
    let symbols = u
        .col("symbol")
        .ok_or_else(|| ScanError("KeyError: 'symbol'".to_owned()))?;
    let suspicious: Vec<PyValue> = (0..n)
        .filter(|&i| sh[i] > 8.0)
        .map(|i| cell_value(symbols, i))
        .collect();
    out.insert("suspicious_sharpe_gt_8", PyValue::List(suspicious));
    Ok(out)
}

/// A cell as the Python value `tolist()` gives.
#[must_use]
pub fn cell_value(col: &Column, i: usize) -> PyValue {
    match col {
        Column::Int(v) => PyValue::Int(v[i]),
        Column::Float(v) => PyValue::Float(v[i]),
        Column::Bool(v) => PyValue::Bool(v[i]),
        Column::Str(v) => v[i].clone().map_or(PyValue::Float(f64::NAN), PyValue::Str),
    }
}

fn in_set(col: &Column, i: usize, set: &[String]) -> bool {
    match col {
        Column::Str(v) => v[i].as_ref().is_some_and(|s| set.contains(s)),
        _ => false,
    }
}

/// `apply_filters(u)`: the `reject` reasons per row, in Python's order.
///
/// # Errors
/// A non-numeric column where a number is compared.
pub fn apply_filters(u: &Frame, cfg: &ScoringConfig) -> Result<Vec<String>, ScanError> {
    let n = u.nrows;
    let mut reject = vec![String::new(); n];
    let close = num(u, "close")?;
    let ma50 = num_rhs(u, "ma_50")?;
    let ma200 = num_rhs(u, "ma_200")?;
    let r3 = num(u, "absolute_return_three_months")?;
    let r6 = num(u, "absolute_return_six_months")?;
    let c3 = num(u, "circuits_three_months")?;
    let mv = num(u, "median_volume_one_year")?;
    let away = num(u, "away_from_high_one_year")?;
    let series = u
        .col("series")
        .ok_or_else(|| ScanError("KeyError: 'series'".to_owned()))?;
    let symbol = u
        .col("symbol")
        .ok_or_else(|| ScanError("KeyError: 'symbol'".to_owned()))?;
    for i in 0..n {
        let r = &mut reject[i];
        if ma50[i].is_nan() || ma200[i].is_nan() || (close[i] < ma50[i] && close[i] < ma200[i]) {
            r.push_str("below50&200DMA;");
        }
        if r3[i] < 0.0 && r6[i] < 0.0 {
            r.push_str("neg3M&6M;");
        }
        if c3[i] > cfg.max_circuits_3m {
            r.push_str("circuits;");
        }
        if mv[i].is_nan() || mv[i] < cfg.min_median_daily_value {
            r.push_str("illiquid;");
        }
        if away[i].is_nan() || away[i] < cfg.max_away_from_high {
            r.push_str("far_from_high;");
        }
        if in_set(series, i, &cfg.reject_series) {
            r.push_str("T2T_series;");
        }
        if in_set(symbol, i, &cfg.excluded_symbols) {
            r.push_str("excluded_instrument;");
        }
    }
    Ok(reject)
}

/// What `score(u)` adds to the scan, per row (NaN for a rejected row).
#[derive(Debug, Clone, PartialEq)]
pub struct Scored {
    /// The scan as loaded.
    pub frame: Frame,
    /// `reject`.
    pub reject: Vec<String>,
    /// `SCORE`.
    pub score: Vec<f64>,
    /// `rank` (1-based; NaN for a rejected row).
    pub rank: Vec<f64>,
    /// `ext_over_20dma`.
    pub ext_over_20dma: Vec<f64>,
    /// `A_trend` … `F_penalty`, in that order.
    pub parts: [Vec<f64>; 6],
    /// Was `rank` int64 after the merge (no rejected row)?
    pub rank_is_int: bool,
}

impl Scored {
    /// Rows.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.frame.nrows
    }

    /// No rows?
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.frame.nrows == 0
    }

    /// The `symbol` cell as a string.
    #[must_use]
    pub fn symbol(&self, i: usize) -> String {
        self.frame
            .col("symbol")
            .map(|c| c.py_str(i))
            .unwrap_or_default()
    }

    /// The row of `symbol` (`set_index("symbol").loc[symbol]`, the first match).
    #[must_use]
    pub fn row_of(&self, symbol: &str) -> Option<usize> {
        (0..self.len()).find(|&i| self.symbol(i) == symbol)
    }

    /// A numeric cell (NaN when absent or not numeric).
    #[must_use]
    pub fn num(&self, name: &str, i: usize) -> f64 {
        match self.frame.col(name) {
            Some(Column::Int(v)) => {
                #[allow(clippy::cast_precision_loss)]
                let x = v[i] as f64;
                x
            }
            Some(Column::Float(v)) => v[i],
            Some(Column::Bool(v)) => {
                if v[i] {
                    1.0
                } else {
                    0.0
                }
            }
            _ => f64::NAN,
        }
    }
}

fn pct_series(values: &[f64]) -> Vec<f64> {
    rank_pct(values).into_iter().map(|x| x * 100.0).collect()
}

fn where3(cond_a: bool, a: f64, cond_b: bool, b: f64, c: f64) -> f64 {
    if cond_a {
        a
    } else if cond_b {
        b
    } else {
        c
    }
}

/// `score(u)`: the full universe with `reject`, eligible rows scored and ranked.
///
/// # Errors
/// A non-numeric column where a number is compared.
#[allow(clippy::too_many_lines)]
pub fn score(u: &Frame, cfg: &ScoringConfig) -> Result<Scored, ScanError> {
    let n = u.nrows;
    let reject = apply_filters(u, cfg)?;
    let elig: Vec<usize> = (0..n).filter(|&i| reject[i].is_empty()).collect();
    let nan = || vec![f64::NAN; n];
    let mut out = Scored {
        frame: u.clone(),
        reject: reject.clone(),
        score: nan(),
        rank: nan(),
        ext_over_20dma: nan(),
        parts: [nan(), nan(), nan(), nan(), nan(), nan()],
        rank_is_int: false,
    };
    if elig.is_empty() {
        return Ok(out);
    }
    let m = elig.len();
    let col = |name: &str| -> Result<Vec<f64>, ScanError> {
        let all = num(u, name)?;
        Ok(elig.iter().map(|&i| all[i]).collect())
    };
    let col_rhs = |name: &str| -> Result<Vec<f64>, ScanError> {
        let all = num_rhs(u, name)?;
        Ok(elig.iter().map(|&i| all[i]).collect())
    };
    let clipped = |name: &str| -> Result<Vec<f64>, ScanError> {
        let v = col(name)?;
        let lo = quantile(&v, 0.01);
        let hi = quantile(&v, 0.99);
        Ok(v.into_iter().map(|x| clip(x, lo, hi)).collect())
    };
    let mut ret: Vec<(String, Vec<f64>)> = Vec::new();
    for c in cfg.ret_cols() {
        let v = clipped(&c)?;
        ret.push((c, v));
    }
    let mut shp: Vec<(String, Vec<f64>)> = Vec::new();
    for c in SHP_COLS {
        shp.push((c.to_owned(), clipped(c)?));
    }
    let get_ret = |w: &str| -> Vec<f64> {
        let name = format!("absolute_return_{w}");
        ret.iter()
            .find(|(k, _)| *k == name)
            .map(|(_, v)| v.clone())
            .unwrap_or_default()
    };
    let close = col("close")?;
    let ma20 = col_rhs("ma_20")?;
    let ma50 = col_rhs("ma_50")?;
    let ma100 = col_rhs("ma_100")?;
    let ma200 = col_rhs("ma_200")?;
    let ext: Vec<f64> = (0..m).map(|k| (close[k] / ma20[k] - 1.0) * 100.0).collect();

    // A: integer arithmetic on booleans, then the extension bonus.
    let a: Vec<f64> = (0..m)
        .map(|k| {
            let b = |x: bool| i64::from(x);
            let ai = 4 * b(close[k] > ma20[k])
                + 4 * b(close[k] > ma50[k])
                + 4 * b(close[k] > ma100[k])
                + 4 * b(close[k] > ma200[k])
                + 5 * b(close[k] > ma20[k]
                    && ma20[k] > ma50[k]
                    && ma50[k] > ma100[k]
                    && ma100[k] > ma200[k]);
            let e = ext[k];
            let bonus = if (0.0..=12.0).contains(&e) {
                4
            } else if (12.0..=20.0).contains(&e) {
                2
            } else {
                0
            };
            #[allow(clippy::cast_precision_loss)]
            let total = (ai + bonus).clamp(0, 25) as f64;
            total
        })
        .collect();

    // B: the momentum blend.
    let mut b = vec![0.0_f64; m];
    for (w, weight) in &cfg.momentum_blend {
        let p = pct_series(&get_ret(w));
        for k in 0..m {
            b[k] += weight * p[k];
        }
    }
    let r1 = get_ret("one_month");
    let r6 = get_ret("six_months");
    let b: Vec<f64> = (0..m)
        .map(|k| {
            let mut x = b[k] / 100.0 * 25.0;
            x -= if r1[k] > 25.0 && r6[k] < 40.0 {
                4.0
            } else {
                0.0
            };
            x -= if r1[k] > 35.0 { 3.0 } else { 0.0 };
            np_clip(x, 0.0, 25.0)
        })
        .collect();

    // C: the Sharpe blend.
    let mut c = vec![0.0_f64; m];
    for (w, weight) in &cfg.sharpe_blend {
        let name = format!("sharpe_return_{w}");
        let v = shp
            .iter()
            .find(|(k, _)| *k == name)
            .map(|(_, v)| v.clone())
            .unwrap_or_default();
        let p = pct_series(&v);
        for k in 0..m {
            c[k] += weight * p[k];
        }
    }
    let c: Vec<f64> = c
        .into_iter()
        .map(|x| np_clip(x / 100.0 * 20.0, 0.0, 20.0))
        .collect();

    // D: consistency.
    let p3 = pct_series(&col("positive_days_percent_three_months")?);
    let p6 = pct_series(&col("positive_days_percent_six_months")?);
    let away = col("away_from_high_one_year")?;
    let d: Vec<f64> = (0..m)
        .map(|k| {
            let mut x = (0.4 * p3[k] + 0.3 * p6[k]) / 100.0 * 6.0;
            x += where3(away[k] >= -8.0, 4.0, away[k] >= -15.0, 2.5, 0.0);
            np_clip(x, 0.0, 10.0)
        })
        .collect();

    // E: liquidity.
    let pmv = pct_series(&col("median_volume_one_year")?);
    let fno = col("is_nifty_fno")?;
    let mcap = col("marketcap")?;
    let e: Vec<f64> = (0..m)
        .map(|k| {
            let mut x = pmv[k] / 100.0 * 6.0;
            x += if fno[k].total_cmp(&1.0).is_eq() {
                2.0
            } else {
                0.0
            };
            x += where3(mcap[k] > 20000.0, 2.0, mcap[k] > 10000.0, 1.0, 0.0);
            np_clip(x, 0.0, 10.0)
        })
        .collect();

    // F: penalties.
    let rsi = col("rsi_one_month")?;
    let vol = col("volatility_one_year")?;
    let beta = col("beta")?;
    let c1y = col("circuits_one_year")?;
    let f: Vec<f64> = (0..m)
        .map(|k| {
            let mut x = 0.0_f64;
            x -= where3(rsi[k] > 82.0, 4.0, rsi[k] > 78.0, 2.0, 0.0);
            x -= where3(vol[k] > 0.55, 3.0, vol[k] > 0.45, 1.5, 0.0);
            x -= if beta[k] > 1.6 { 2.0 } else { 0.0 };
            x -= if c1y[k] > cfg.penalty_circuits_1y {
                2.0
            } else {
                0.0
            };
            x -= where3(ext[k] > 25.0, 3.0, ext[k] > 18.0, 1.5, 0.0);
            if x.is_nan() { x } else { x.max(-10.0) }
        })
        .collect();

    let score: Vec<f64> = (0..m)
        .map(|k| np_round(a[k] + b[k] + c[k] + d[k] + e[k] + f[k], 1))
        .collect();
    let ext_r: Vec<f64> = ext.iter().map(|x| np_round(*x, 1)).collect();
    let order = nargsort(&score, false);
    for (pos, &k) in order.iter().enumerate() {
        let i = elig[k];
        out.score[i] = score[k];
        #[allow(clippy::cast_precision_loss)]
        let r = (pos + 1) as f64;
        out.rank[i] = r;
        out.ext_over_20dma[i] = ext_r[k];
        out.parts[0][i] = a[k];
        out.parts[1][i] = b[k];
        out.parts[2][i] = c[k];
        out.parts[3][i] = d[k];
        out.parts[4][i] = e[k];
        out.parts[5][i] = f[k];
    }
    out.rank_is_int = m == n;
    Ok(out)
}

/// `stop_from_vol(price, ann_vol)`: a stop 8–12% below the price, scaled by weekly volatility
/// (non-negotiable 4).
#[must_use]
pub fn stop_from_vol(price: f64, ann_vol: f64, cfg: &ScoringConfig) -> f64 {
    let pct = np_clip(
        ann_vol / 52_f64.sqrt() * cfg.stop_vol_mult,
        cfg.stop_min,
        cfg.stop_max,
    );
    py_round(price * (1.0 - pct), 1)
}
