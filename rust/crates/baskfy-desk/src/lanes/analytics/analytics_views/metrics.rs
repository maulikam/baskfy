//! `app/analytics/metrics.py`: the pure metrics engine. No I/O beyond [`load_sectors`].

use std::collections::{BTreeMap, HashMap};
use std::path::Path;

use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveDateTime};

use crate::wire::{PyDict, PyValue, dict};

/// Trading days per year (volatility scaling).
pub const TRADING_DAYS: usize = 252;
/// ACT/365.
pub const DAYS_PER_YEAR: f64 = 365.0;
/// The ratio guard.
pub const EPS: f64 = 1e-12;
/// `SECTORS_FILE`.
pub const SECTORS_FILE: &str = "data/sectors.csv";
/// STCG rate.
pub const STCG_RATE: f64 = 0.20;
/// LTCG rate.
pub const LTCG_RATE: f64 = 0.125;
/// LTCG exemption per financial year.
pub const LTCG_EXEMPTION: f64 = 125_000.0;
/// Long-term threshold, days.
pub const LONG_TERM_DAYS: i64 = 365;

/// A dated float series (`pd.Series` with a `DatetimeIndex`); NaN is a missing value.
pub type Series = Vec<(NaiveDate, f64)>;

// =============================================================================================
// numpy / pandas reductions
// =============================================================================================

/// numpy's pairwise `add.reduce`.
#[must_use]
pub fn np_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0;
        for x in a {
            res += x;
        }
        res
    } else if n <= 128 {
        let mut r = [0.0; 8];
        r.copy_from_slice(&a[..8]);
        let end = n - n % 8;
        let mut i = 8;
        while i < end {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        for x in &a[end..] {
            res += x;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        np_sum(&a[..n2]) + np_sum(&a[n2..])
    }
}

fn finite_vals(a: &[f64]) -> Vec<f64> {
    a.iter().copied().filter(|x| !x.is_nan()).collect()
}

/// `Series.sum()` (NaN skipped, empty is 0).
#[must_use]
pub fn nansum(a: &[f64]) -> f64 {
    let filled: Vec<f64> = a
        .iter()
        .map(|x| if x.is_nan() { 0.0 } else { *x })
        .collect();
    np_sum(&filled)
}

/// `Series.mean()` (NaN skipped, empty is NaN).
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn nanmean(a: &[f64]) -> f64 {
    let count = a.iter().filter(|x| !x.is_nan()).count();
    if count == 0 {
        return f64::NAN;
    }
    nansum(a) / count as f64
}

/// `Series.var(ddof=)` (pandas `nanvar`).
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn nanvar(a: &[f64], ddof: usize) -> f64 {
    let count = a.iter().filter(|x| !x.is_nan()).count();
    if count <= ddof {
        return f64::NAN;
    }
    let avg = nansum(a) / count as f64;
    let sqr: Vec<f64> = a
        .iter()
        .map(|x| {
            if x.is_nan() {
                0.0
            } else {
                (avg - x) * (avg - x)
            }
        })
        .collect();
    np_sum(&sqr) / (count - ddof) as f64
}

/// `Series.std(ddof=)`.
#[must_use]
pub fn nanstd(a: &[f64], ddof: usize) -> f64 {
    nanvar(a, ddof).sqrt()
}

/// `Series.median()`.
#[must_use]
pub fn nanmedian(a: &[f64]) -> f64 {
    quantile(a, 0.5)
}

/// `Series.quantile(q)` (numpy's linear method).
#[must_use]
#[allow(
    clippy::cast_precision_loss,
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss
)]
pub fn quantile(a: &[f64], q: f64) -> f64 {
    let mut v = finite_vals(a);
    if v.is_empty() {
        return f64::NAN;
    }
    v.sort_by(f64::total_cmp);
    let n = v.len() as f64;
    let (alpha, beta) = (1.0, 1.0);
    let virtual_index = n * q + (alpha + q * (1.0 - alpha - beta)) - 1.0;
    let virtual_index = virtual_index.clamp(0.0, n - 1.0);
    let prev = virtual_index.floor();
    let gamma = virtual_index - prev;
    let lo = prev as usize;
    let hi = (lo + 1).min(v.len() - 1);
    let (a0, b0) = (v[lo], v[hi]);
    let diff = b0 - a0;
    if gamma >= 0.5 {
        b0 - diff * (1.0 - gamma)
    } else {
        a0 + diff * gamma
    }
}

/// pandas' groupby sum (Kahan-compensated, NaN skipped).
#[must_use]
pub fn kahan_sum(a: &[f64]) -> f64 {
    let (mut sum, mut comp) = (0.0_f64, 0.0_f64);
    for &v in a {
        if v.is_nan() {
            continue;
        }
        let y = v - comp;
        let t = sum + y;
        comp = t - sum - y;
        sum = t;
    }
    sum
}

/// pandas' groupby mean.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn kahan_mean(a: &[f64]) -> f64 {
    let count = a.iter().filter(|x| !x.is_nan()).count();
    if count == 0 {
        return f64::NAN;
    }
    kahan_sum(a) / count as f64
}

#[allow(clippy::cast_precision_loss)]
fn np_mean(a: &[f64]) -> f64 {
    np_sum(a) / a.len() as f64
}

/// `np.cov(a, b, ddof=1)[0, 1]` over the pairs where both are present.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn cov(a: &[f64], b: &[f64]) -> f64 {
    let (x, y) = pairs(a, b);
    if x.is_empty() {
        return f64::NAN;
    }
    let (mx, my) = (np_mean(&x), np_mean(&y));
    let mut c = 0.0;
    for (p, q) in x.iter().zip(&y) {
        c += (p - mx) * (q - my);
    }
    c * (1.0 / (x.len() as f64 - 1.0))
}

/// `Series.corr(other)` (Pearson, `np.corrcoef`).
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn corr(a: &[f64], b: &[f64]) -> f64 {
    let (x, y) = pairs(a, b);
    if x.is_empty() {
        return f64::NAN;
    }
    let (mx, my) = (np_mean(&x), np_mean(&y));
    let (mut cxy, mut cxx, mut cyy) = (0.0, 0.0, 0.0);
    for (p, q) in x.iter().zip(&y) {
        cxy += (p - mx) * (q - my);
        cxx += (p - mx) * (p - mx);
        cyy += (q - my) * (q - my);
    }
    let f = 1.0 / (x.len() as f64 - 1.0);
    let r = (cxy * f / (cxx * f).sqrt()) / (cyy * f).sqrt();
    if r.is_nan() { r } else { r.clamp(-1.0, 1.0) }
}

fn pairs(a: &[f64], b: &[f64]) -> (Vec<f64>, Vec<f64>) {
    a.iter()
        .zip(b)
        .filter(|(x, y)| !x.is_nan() && !y.is_nan())
        .map(|(x, y)| (*x, *y))
        .unzip()
}

/// `Series.rank()` (average ties, NaN stays NaN).
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn rank(a: &[f64]) -> Vec<f64> {
    let mut idx: Vec<usize> = (0..a.len()).filter(|&i| !a[i].is_nan()).collect();
    idx.sort_by(|&i, &j| a[i].total_cmp(&a[j]));
    let mut out = vec![f64::NAN; a.len()];
    let mut k = 0;
    while k < idx.len() {
        let mut e = k;
        while e + 1 < idx.len() && a[idx[e + 1]].total_cmp(&a[idx[k]]).is_eq() {
            e += 1;
        }
        let avg = (k + e) as f64 / 2.0 + 1.0;
        for &i in &idx[k..=e] {
            out[i] = avg;
        }
        k = e + 1;
    }
    out
}

/// `np.linspace(start, stop, num)`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn linspace(start: f64, stop: f64, num: usize) -> Vec<f64> {
    if num == 0 {
        return Vec::new();
    }
    if num == 1 {
        return vec![start];
    }
    let step = (stop - start) / (num - 1) as f64;
    let mut v: Vec<f64> = (0..num).map(|i| i as f64 * step + start).collect();
    v[num - 1] = stop;
    v
}

// =============================================================================================
// helpers
// =============================================================================================

/// `_series(x)`: sorted by date (stable).
#[must_use]
pub fn sorted(s: &[(NaiveDate, f64)]) -> Series {
    let mut v = s.to_vec();
    v.sort_by_key(|p| p.0);
    v
}

fn vals(s: &[(NaiveDate, f64)]) -> Vec<f64> {
    s.iter().map(|p| p.1).collect()
}

fn dropna(s: &[(NaiveDate, f64)]) -> Series {
    s.iter().copied().filter(|p| !p.1.is_nan()).collect()
}

fn sorted_dropna(s: &[(NaiveDate, f64)]) -> Series {
    dropna(&sorted(s))
}

fn lookup(s: &[(NaiveDate, f64)]) -> BTreeMap<NaiveDate, f64> {
    s.iter().copied().collect()
}

/// `_flows_into(cashflows, index)`: net flow INTO the portfolio per date, aligned to `index`.
#[must_use]
pub fn flows_into(cashflows: Option<&[(NaiveDate, f64)]>, index: &[NaiveDate]) -> Vec<f64> {
    let Some(cf) = cashflows else {
        return vec![0.0; index.len()];
    };
    let mut grouped: BTreeMap<NaiveDate, Vec<f64>> = BTreeMap::new();
    for (d, v) in sorted(cf) {
        grouped.entry(d).or_default().push(v);
    }
    let net: BTreeMap<NaiveDate, f64> = grouped
        .into_iter()
        .map(|(d, v)| (d, -kahan_sum(&v)))
        .collect();
    index
        .iter()
        .map(|d| net.get(d).copied().filter(|x| !x.is_nan()).unwrap_or(0.0))
        .collect()
}

/// `_align(a, b)`: both sorted, inner-joined on the common dates.
#[must_use]
pub fn align(a: &[(NaiveDate, f64)], b: &[(NaiveDate, f64)]) -> (Series, Series) {
    let (a, b) = (sorted(a), sorted(b));
    let bm = lookup(&b);
    let am = lookup(&a);
    let mut common: Vec<NaiveDate> = a
        .iter()
        .map(|p| p.0)
        .filter(|d| bm.contains_key(d))
        .collect();
    common.dedup();
    (
        common.iter().map(|d| (*d, am[d])).collect(),
        common.iter().map(|d| (*d, bm[d])).collect(),
    )
}

/// `load_sectors(path)`: symbol → cluster from a `symbol,cluster` CSV; empty if absent.
#[must_use]
pub fn load_sectors(path: &Path) -> HashMap<String, String> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return HashMap::new();
    };
    let rows = crate::lanes::analytics::analytics_regime::nse_indices::csv_rows(
        text.trim_start_matches('\u{feff}'),
    );
    let mut it = rows.into_iter();
    let Some(header) = it.next() else {
        return HashMap::new();
    };
    let (Some(si), Some(ci)) = (
        header.iter().position(|h| h == "symbol"),
        header.iter().position(|h| h == "cluster"),
    ) else {
        return HashMap::new();
    };
    it.filter(|r| !r.is_empty())
        .map(|r| {
            let cell = |i: usize| {
                r.get(i)
                    .filter(|v| !v.is_empty())
                    .cloned()
                    .unwrap_or_else(|| "nan".to_owned())
            };
            (cell(si), cell(ci))
        })
        .collect()
}

// =============================================================================================
// returns
// =============================================================================================

/// `daily_returns(navs, cashflows)`: daily TWR with flows neutralised at end of day.
#[must_use]
pub fn daily_returns(navs: &[(NaiveDate, f64)], cashflows: Option<&[(NaiveDate, f64)]>) -> Series {
    let nav = sorted(navs);
    let index: Vec<NaiveDate> = nav.iter().map(|p| p.0).collect();
    let flows = flows_into(cashflows, &index);
    (1..nav.len())
        .map(|i| {
            let prev = nav[i - 1].1;
            let r = if prev > 0.0 {
                (nav[i].1 - flows[i]) / prev - 1.0
            } else {
                0.0
            };
            (nav[i].0, r)
        })
        .collect()
}

/// `twr_chained(navs, cashflows, base)`: the chained index, exactly `base` on the first date.
#[must_use]
pub fn twr_chained(
    navs: &[(NaiveDate, f64)],
    cashflows: Option<&[(NaiveDate, f64)]>,
    base: f64,
) -> Series {
    let nav = sorted(navs);
    let Some(first) = nav.first() else {
        return Vec::new();
    };
    let mut out = vec![(first.0, base)];
    let mut acc = 1.0;
    for (d, r) in daily_returns(&nav, cashflows) {
        let g = 1.0 + r;
        if g.is_nan() {
            out.push((d, f64::NAN));
        } else {
            acc *= g;
            out.push((d, acc * base));
        }
    }
    out
}

/// `twr_total(navs, cashflows)`.
#[must_use]
pub fn twr_total(navs: &[(NaiveDate, f64)], cashflows: Option<&[(NaiveDate, f64)]>) -> f64 {
    let idx = twr_chained(navs, cashflows, 100.0);
    if idx.len() < 2 {
        return 0.0;
    }
    idx[idx.len() - 1].1 / idx[0].1 - 1.0
}

/// `absolute(series)`: point-to-point return.
#[must_use]
#[allow(clippy::float_cmp)]
pub fn absolute(series: &[(NaiveDate, f64)]) -> f64 {
    let s = sorted(series);
    if s.len() < 2 || s[0].1 == 0.0 {
        return 0.0;
    }
    s[s.len() - 1].1 / s[0].1 - 1.0
}

/// `cagr(series, day_count=)`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn cagr_with(series: &[(NaiveDate, f64)], day_count: f64) -> f64 {
    let s = sorted(series);
    if s.len() < 2 || s[0].1 <= 0.0 {
        return 0.0;
    }
    let years = (s[s.len() - 1].0 - s[0].0).num_days() as f64 / day_count;
    if years <= 0.0 {
        return 0.0;
    }
    (s[s.len() - 1].1 / s[0].1).powf(1.0 / years) - 1.0
}

/// `cagr(series)` (ACT/365).
#[must_use]
pub fn cagr(series: &[(NaiveDate, f64)]) -> f64 {
    cagr_with(series, DAYS_PER_YEAR)
}

/// `xirr(cashflows, dates)` with the defaults (`guess=0.1, tol=1e-10, max_iter=100`).
#[must_use]
pub fn xirr(amounts: &[f64], dates: &[NaiveDate]) -> f64 {
    xirr_with(amounts, dates, 0.1, 1e-10, 100)
}

/// `xirr(...)`: Newton-Raphson, then a bracketed bisection. NaN when no root exists.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn xirr_with(
    amounts: &[f64],
    dates: &[NaiveDate],
    guess: f64,
    tol: f64,
    max_iter: usize,
) -> f64 {
    if amounts.len() != dates.len() || amounts.len() < 2 {
        return f64::NAN;
    }
    let mut order: Vec<usize> = (0..amounts.len()).collect();
    order.sort_by_key(|&i| dates[i]);
    let amounts: Vec<f64> = order.iter().map(|&i| amounts[i]).collect();
    let when: Vec<NaiveDate> = order.iter().map(|&i| dates[i]).collect();
    if !(amounts.iter().any(|a| *a > 0.0) && amounts.iter().any(|a| *a < 0.0)) {
        return f64::NAN;
    }
    let years: Vec<f64> = when
        .iter()
        .map(|d| (*d - when[0]).num_days() as f64 / DAYS_PER_YEAR)
        .collect();
    let npv = |rate: f64| -> f64 {
        let t: Vec<f64> = amounts
            .iter()
            .zip(&years)
            .map(|(a, y)| a / (1.0 + rate).powf(*y))
            .collect();
        np_sum(&t)
    };
    let d_npv = |rate: f64| -> f64 {
        let t: Vec<f64> = amounts
            .iter()
            .zip(&years)
            .map(|(a, y)| -a * y / (1.0 + rate).powf(y + 1.0))
            .collect();
        np_sum(&t)
    };

    let mut rate = guess;
    let mut broke = false;
    for _ in 0..max_iter {
        if rate <= -1.0 {
            broke = true;
            break;
        }
        let f = npv(rate);
        if f.abs() < tol {
            return rate;
        }
        let d = d_npv(rate);
        if d == 0.0 || !d.is_finite() {
            broke = true;
            break;
        }
        let nxt = rate - f / d;
        if !nxt.is_finite() || nxt <= -1.0 {
            broke = true;
            break;
        }
        if (nxt - rate).abs() < tol {
            return nxt;
        }
        rate = nxt;
    }
    if !broke && npv(rate).abs() < 1e-6 {
        return rate;
    }

    let mut lo = -0.9999;
    let mut f_lo = npv(lo);
    let mut grid = linspace(-0.99, 1.0, 200);
    grid.extend(linspace(1.0, 100.0, 200));
    let mut bracket = None;
    for hi in grid {
        if hi <= lo {
            continue;
        }
        let f_hi = npv(hi);
        if f_hi.is_finite() && f_lo * f_hi < 0.0 {
            bracket = Some((lo, hi));
            break;
        }
        lo = hi;
        f_lo = f_hi;
    }
    let Some((mut lo, mut hi)) = bracket else {
        return f64::NAN;
    };
    for _ in 0..200 {
        let mid = f64::midpoint(lo, hi);
        let f_mid = npv(mid);
        if f_mid.abs() < tol || (hi - lo) < tol {
            return mid;
        }
        if npv(lo) * f_mid < 0.0 {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    f64::midpoint(lo, hi)
}

/// `rolling_returns(level, windows)`: trailing `pct_change(n)` per window (NaN where short).
#[must_use]
pub fn rolling_returns(
    level: &[(NaiveDate, f64)],
    windows: &[(&str, usize)],
) -> Vec<(String, Series)> {
    let s = sorted(level);
    windows
        .iter()
        .map(|(name, n)| {
            let col = s
                .iter()
                .enumerate()
                .map(|(i, (d, v))| {
                    let r = if i >= *n {
                        v / s[i - n].1 - 1.0
                    } else {
                        f64::NAN
                    };
                    (*d, r)
                })
                .collect();
            ((*name).to_owned(), col)
        })
        .collect()
}

/// The default windows: 1M/3M/6M/1Y in trading days.
pub const ROLLING_WINDOWS: [(&str, usize); 4] =
    [("1M", 21), ("3M", 63), ("6M", 126), ("1Y", TRADING_DAYS)];

fn month_end(d: NaiveDate) -> NaiveDate {
    let (y, m) = if d.month() == 12 {
        (d.year() + 1, 1)
    } else {
        (d.year(), d.month() + 1)
    };
    NaiveDate::from_ymd_opt(y, m, 1).map_or(d, |n| n - Duration::days(1))
}

/// `monthly_returns(level)`: calendar-month returns indexed by month end, the first partial
/// month measured from the opening level.
#[must_use]
pub fn monthly_returns(level: &[(NaiveDate, f64)]) -> Series {
    let s = sorted(level);
    let Some(first) = s.first() else {
        return Vec::new();
    };
    let last_date = s[s.len() - 1].0;
    let mut by_month: BTreeMap<NaiveDate, f64> = BTreeMap::new();
    let mut m = month_end(first.0);
    while m <= month_end(last_date) {
        by_month.insert(m, f64::NAN);
        m = month_end(m + Duration::days(1));
    }
    for (d, v) in &s {
        if !v.is_nan() {
            by_month.insert(month_end(*d), *v);
        }
    }
    let mut chain = vec![(first.0 - Duration::days(1), first.1)];
    chain.extend(by_month);
    (1..chain.len())
        .map(|i| (chain[i].0, chain[i].1 / chain[i - 1].1 - 1.0))
        .filter(|p| !p.1.is_nan())
        .collect()
}

/// `monthly_return_matrix(level, pct=)`: year → twelve months (NaN where absent).
#[must_use]
pub fn monthly_return_matrix(level: &[(NaiveDate, f64)], pct: bool) -> Vec<(i32, [f64; 12])> {
    let mut out: BTreeMap<i32, [f64; 12]> = BTreeMap::new();
    for (d, r) in monthly_returns(level) {
        let row = out.entry(d.year()).or_insert([f64::NAN; 12]);
        row[d.month0() as usize] = r * if pct { 100.0 } else { 1.0 };
    }
    out.into_iter().collect()
}

// =============================================================================================
// risk
// =============================================================================================

/// `_ratio(num, den)`: a ~zero denominator with a real numerator is undefined (NaN), not zero.
#[must_use]
pub fn ratio(num: f64, den: f64) -> f64 {
    if den < EPS {
        return if num.abs() < EPS { 0.0 } else { f64::NAN };
    }
    num / den
}

#[allow(clippy::cast_precision_loss)]
fn sqrt_periods(periods: usize) -> f64 {
    (periods as f64).sqrt()
}

/// `ann_volatility(returns, periods)`.
#[must_use]
pub fn ann_volatility_with(returns: &[(NaiveDate, f64)], periods: usize) -> f64 {
    let r = sorted_dropna(returns);
    if r.len() < 2 {
        return 0.0;
    }
    nanstd(&vals(&r), 1) * sqrt_periods(periods)
}

/// `ann_volatility(returns)`.
#[must_use]
pub fn ann_volatility(returns: &[(NaiveDate, f64)]) -> f64 {
    ann_volatility_with(returns, TRADING_DAYS)
}

/// `downside_deviation(returns, mar, periods)`: `mar` is annual.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn downside_deviation(returns: &[(NaiveDate, f64)], mar: f64, periods: usize) -> f64 {
    let r = sorted_dropna(returns);
    if r.len() < 2 {
        return 0.0;
    }
    let daily_mar = mar / periods as f64;
    let sq: Vec<f64> = r
        .iter()
        .map(|(_, x)| {
            let s = (x - daily_mar).min(0.0);
            s * s
        })
        .collect();
    nanmean(&sq).sqrt() * sqrt_periods(periods)
}

/// `max_drawdown(level)`'s dict.
#[derive(Debug, Clone, PartialEq)]
pub struct Drawdown {
    /// Negative fraction.
    pub depth: f64,
    /// The same in percent.
    pub depth_pct: f64,
    /// First date at the peak.
    pub peak_date: Option<NaiveDate>,
    /// The trough.
    pub trough_date: Option<NaiveDate>,
    /// First date back at the peak.
    pub recovery_date: Option<NaiveDate>,
    /// Peak to recovery, or to the last observation.
    pub duration_days: i64,
    /// The drawdown on the last observation.
    pub current_dd: f64,
    /// Whether the peak was regained.
    pub recovered: bool,
}

/// `max_drawdown(level)`: the deepest peak-to-trough fall.
#[must_use]
pub fn max_drawdown(level: &[(NaiveDate, f64)]) -> Drawdown {
    let s = sorted_dropna(level);
    if s.is_empty() {
        return Drawdown {
            depth: 0.0,
            depth_pct: 0.0,
            peak_date: None,
            trough_date: None,
            recovery_date: None,
            duration_days: 0,
            current_dd: 0.0,
            recovered: false,
        };
    }
    let mut peak = Vec::with_capacity(s.len());
    let mut run = f64::NEG_INFINITY;
    for (_, v) in &s {
        run = run.max(*v);
        peak.push(run);
    }
    let dd: Vec<f64> = s.iter().zip(&peak).map(|((_, v), p)| v / p - 1.0).collect();
    let mut ti = 0;
    for (i, x) in dd.iter().enumerate() {
        if !x.is_nan() && (dd[ti].is_nan() || *x < dd[ti]) {
            ti = i;
        }
    }
    let depth = dd[ti];
    let peak_value = peak[ti];
    let peak_date = s[..=ti]
        .iter()
        .find(|(_, v)| *v >= peak_value)
        .map_or(s[0].0, |p| p.0);
    let recovery_date = s[ti..].iter().find(|(_, v)| *v >= peak_value).map(|p| p.0);
    let end = recovery_date.unwrap_or(s[s.len() - 1].0);
    Drawdown {
        depth,
        depth_pct: depth * 100.0,
        peak_date: Some(peak_date),
        trough_date: Some(s[ti].0),
        recovery_date,
        duration_days: (end - peak_date).num_days(),
        current_dd: dd[dd.len() - 1],
        recovered: recovery_date.is_some(),
    }
}

/// `drawdown_series(level)`: the underwater curve.
#[must_use]
pub fn drawdown_series(level: &[(NaiveDate, f64)]) -> Series {
    let s = sorted_dropna(level);
    let mut run = f64::NEG_INFINITY;
    s.iter()
        .map(|(d, v)| {
            run = run.max(*v);
            (*d, v / run - 1.0)
        })
        .collect()
}

/// `sharpe(returns, rf, periods)`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn sharpe_with(returns: &[(NaiveDate, f64)], rf: f64, periods: usize) -> f64 {
    let r = sorted_dropna(returns);
    let vol = ann_volatility_with(&r, periods);
    ratio(nanmean(&vals(&r)) * periods as f64 - rf, vol)
}

/// `sharpe(returns, rf)`.
#[must_use]
pub fn sharpe(returns: &[(NaiveDate, f64)], rf: f64) -> f64 {
    sharpe_with(returns, rf, TRADING_DAYS)
}

/// `sortino(returns, mar)`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn sortino(returns: &[(NaiveDate, f64)], mar: f64) -> f64 {
    let r = sorted_dropna(returns);
    let dd = downside_deviation(&r, mar, TRADING_DAYS);
    ratio(nanmean(&vals(&r)) * TRADING_DAYS as f64 - mar, dd)
}

/// `calmar(level)`: CAGR over |max drawdown|.
#[must_use]
pub fn calmar(level: &[(NaiveDate, f64)]) -> f64 {
    ratio(cagr(level), max_drawdown(level).depth.abs())
}

/// `beta(port_returns, bench_returns)`.
#[must_use]
pub fn beta(port: &[(NaiveDate, f64)], bench: &[(NaiveDate, f64)]) -> f64 {
    let (p, b) = align(port, bench);
    let (p, b): (Vec<f64>, Vec<f64>) = pairs(&vals(&p), &vals(&b));
    let vb = nanvar(&b, 1);
    if p.len() < 2 || vb < EPS {
        return 0.0;
    }
    cov(&p, &b) / vb
}

/// `alpha_jensen(port, bench, rf)`: annualised Jensen's alpha.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn alpha_jensen(port: &[(NaiveDate, f64)], bench: &[(NaiveDate, f64)], rf: f64) -> f64 {
    let (p, b) = align(port, bench);
    if p.is_empty() {
        return 0.0;
    }
    let bt = beta(&p, &b);
    let periods = TRADING_DAYS as f64;
    let (ann_p, ann_b) = (nanmean(&vals(&p)) * periods, nanmean(&vals(&b)) * periods);
    ann_p - (rf + bt * (ann_b - rf))
}

/// `tracking_error(port, bench)`.
#[must_use]
pub fn tracking_error(port: &[(NaiveDate, f64)], bench: &[(NaiveDate, f64)]) -> f64 {
    let (p, b) = align(port, bench);
    let diff: Vec<f64> = p
        .iter()
        .zip(&b)
        .map(|(x, y)| x.1 - y.1)
        .filter(|x| !x.is_nan())
        .collect();
    if diff.len() < 2 {
        return 0.0;
    }
    nanstd(&diff, 1) * sqrt_periods(TRADING_DAYS)
}

/// `information_ratio(port, bench)`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn information_ratio(port: &[(NaiveDate, f64)], bench: &[(NaiveDate, f64)]) -> f64 {
    let (p, b) = align(port, bench);
    let te = tracking_error(&p, &b);
    ratio(
        (nanmean(&vals(&p)) - nanmean(&vals(&b))) * TRADING_DAYS as f64,
        te,
    )
}

/// `var_parametric(returns, z=)`: Gaussian VaR, a positive daily loss (z defaults to 1.645).
#[must_use]
pub fn var_parametric(returns: &[(NaiveDate, f64)], z: Option<f64>) -> f64 {
    let r = vals(&sorted_dropna(returns));
    if r.len() < 2 {
        return 0.0;
    }
    -(nanmean(&r) - z.unwrap_or(1.645) * nanstd(&r, 1))
}

/// `var_historical(returns, confidence)`: empirical VaR, a positive daily loss.
#[must_use]
pub fn var_historical(returns: &[(NaiveDate, f64)], confidence: f64) -> f64 {
    let r = vals(&sorted_dropna(returns));
    if r.is_empty() {
        return 0.0;
    }
    -quantile(&r, 1.0 - confidence)
}

// =============================================================================================
// trades
// =============================================================================================

/// One trade row (the `trades` table, plus the optional diagnostic columns).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Trade {
    /// `symbol`.
    pub symbol: String,
    /// `pnl` (NaN when NULL).
    pub pnl: f64,
    /// `costs`.
    pub costs: Option<f64>,
    /// `entry_ts` (epoch seconds).
    pub entry_ts: Option<f64>,
    /// `exit_ts` (epoch seconds); `None` is an open trade.
    pub exit_ts: Option<f64>,
    /// `qty`.
    pub qty: Option<f64>,
    /// `entry_price`.
    pub entry_price: Option<f64>,
    /// `exit_price`.
    pub exit_price: Option<f64>,
    /// `entry_score`.
    pub entry_score: Option<f64>,
    /// `entry_rank`.
    pub entry_rank: Option<f64>,
    /// `exit_reason`.
    pub exit_reason: Option<String>,
    /// `fwd_return_20d`.
    pub fwd_return_20d: Option<f64>,
}

fn closed(trades: &[Trade]) -> Vec<&Trade> {
    trades.iter().filter(|t| t.exit_ts.is_some()).collect()
}

fn pnls(df: &[&Trade]) -> Vec<f64> {
    df.iter().map(|t| t.pnl).collect()
}

/// `win_rate(trades)`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn win_rate(trades: &[Trade]) -> f64 {
    let df = closed(trades);
    if df.is_empty() {
        return 0.0;
    }
    df.iter().filter(|t| t.pnl > 0.0).count() as f64 / df.len() as f64
}

/// `avg_win(trades)`.
#[must_use]
pub fn avg_win(trades: &[Trade]) -> f64 {
    let w: Vec<f64> = pnls(&closed(trades))
        .into_iter()
        .filter(|p| *p > 0.0)
        .collect();
    if w.is_empty() { 0.0 } else { nanmean(&w) }
}

/// `avg_loss(trades)`: negative.
#[must_use]
pub fn avg_loss(trades: &[Trade]) -> f64 {
    let l: Vec<f64> = pnls(&closed(trades))
        .into_iter()
        .filter(|p| *p < 0.0)
        .collect();
    if l.is_empty() { 0.0 } else { nanmean(&l) }
}

/// `payoff_ratio(trades)`.
#[must_use]
#[allow(clippy::float_cmp)]
pub fn payoff_ratio(trades: &[Trade]) -> f64 {
    let al = avg_loss(trades);
    if al == 0.0 {
        0.0
    } else {
        avg_win(trades) / al.abs()
    }
}

/// `profit_factor(trades)`: infinite without losses.
#[must_use]
#[allow(clippy::float_cmp)]
pub fn profit_factor(trades: &[Trade]) -> f64 {
    let p = pnls(&closed(trades));
    let gains = nansum(&p.iter().copied().filter(|x| *x > 0.0).collect::<Vec<_>>());
    let losses = nansum(&p.iter().copied().filter(|x| *x < 0.0).collect::<Vec<_>>());
    if losses == 0.0 {
        return if gains > 0.0 { f64::INFINITY } else { 0.0 };
    }
    gains / losses.abs()
}

/// `expectancy(trades)`: rupees per trade.
#[must_use]
pub fn expectancy(trades: &[Trade]) -> f64 {
    let wr = win_rate(trades);
    wr * avg_win(trades) + (1.0 - wr) * avg_loss(trades)
}

/// `avg_holding_period(trades)`: mean days held.
#[must_use]
pub fn avg_holding_period(trades: &[Trade]) -> f64 {
    let df = closed(trades);
    if df.is_empty() || df.iter().all(|t| t.entry_ts.is_none()) {
        return 0.0;
    }
    let held: Vec<f64> = df
        .iter()
        .map(|t| (t.exit_ts.unwrap_or(f64::NAN) - t.entry_ts.unwrap_or(f64::NAN)) / 86400.0)
        .collect();
    nanmean(&held)
}

/// `turnover_pct(traded_value, avg_capital)`.
#[must_use]
pub fn turnover_pct(traded_value: f64, avg_capital: f64) -> f64 {
    if avg_capital <= 0.0 {
        0.0
    } else {
        traded_value / avg_capital * 100.0
    }
}

/// `cost_drag(total_costs, avg_capital)`.
#[must_use]
pub fn cost_drag(total_costs: f64, avg_capital: f64) -> f64 {
    if avg_capital <= 0.0 {
        0.0
    } else {
        total_costs / avg_capital * 100.0
    }
}

/// One group's trade statistics (`hit_rate_by_exit_reason`, the score and rank buckets).
#[derive(Debug, Clone, PartialEq)]
pub struct GroupStats {
    /// The group key (exit reason, bucket label).
    pub key: String,
    /// `trades`.
    pub trades: usize,
    /// `hit_rate` / `win_rate`.
    pub hit_rate: f64,
    /// `avg_pnl`.
    pub avg_pnl: f64,
    /// `total_pnl`.
    pub total_pnl: f64,
}

#[allow(clippy::cast_precision_loss)]
fn group_stats(key: String, p: &[f64]) -> GroupStats {
    GroupStats {
        key,
        trades: p.len(),
        hit_rate: if p.is_empty() {
            0.0
        } else {
            p.iter().filter(|x| **x > 0.0).count() as f64 / p.len() as f64
        },
        avg_pnl: kahan_mean(p),
        total_pnl: kahan_sum(p),
    }
}

fn sort_desc_by<T>(v: &mut [T], key: impl Fn(&T) -> f64) {
    v.sort_by(|a, b| {
        let (x, y) = (key(a), key(b));
        match (x.is_nan(), y.is_nan()) {
            (true, true) => std::cmp::Ordering::Equal,
            (true, false) => std::cmp::Ordering::Greater,
            (false, true) => std::cmp::Ordering::Less,
            (false, false) => y.total_cmp(&x),
        }
    });
}

/// `hit_rate_by_exit_reason(trades)`, sorted by total P&L descending.
#[must_use]
pub fn hit_rate_by_exit_reason(trades: &[Trade]) -> Vec<GroupStats> {
    let mut groups: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    for t in closed(trades) {
        if let Some(r) = &t.exit_reason {
            groups.entry(r.clone()).or_default().push(t.pnl);
        }
    }
    let mut out: Vec<GroupStats> = groups
        .into_iter()
        .map(|(k, p)| group_stats(k, &p))
        .collect();
    sort_desc_by(&mut out, |g| g.total_pnl);
    out
}

/// One `rebalance_orders` row as the slippage and turnover metrics read it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Order {
    /// `side`.
    pub side: String,
    /// `planned_ref_price`.
    pub planned_ref_price: Option<f64>,
    /// `avg_fill_price`.
    pub avg_fill_price: Option<f64>,
    /// `filled_qty`.
    pub filled_qty: Option<f64>,
    /// `planned_qty`.
    pub planned_qty: Option<f64>,
    /// `created_ts` (epoch seconds).
    pub created_ts: Option<f64>,
}

/// `slippage(orders)`'s dict.
#[derive(Debug, Clone, PartialEq)]
pub struct Slippage {
    /// `orders`.
    pub orders: usize,
    /// `mean_bps`.
    pub mean_bps: f64,
    /// `median_bps`.
    pub median_bps: f64,
    /// `total_cost`.
    pub total_cost: f64,
}

/// `slippage(orders)`: positive always means worse than planned.
#[must_use]
pub fn slippage(orders: &[Order]) -> Slippage {
    let none = Slippage {
        orders: 0,
        mean_bps: 0.0,
        median_bps: 0.0,
        total_cost: 0.0,
    };
    let df: Vec<&Order> = orders
        .iter()
        .filter(|o| o.filled_qty.unwrap_or(0.0) > 0.0)
        .filter(|o| o.planned_ref_price.unwrap_or(0.0) > 0.0)
        .collect();
    if df.is_empty() {
        return none;
    }
    let mut bps = Vec::new();
    let mut cost = Vec::new();
    for o in &df {
        let sign = if o.side.to_uppercase() == "BUY" {
            1.0
        } else {
            -1.0
        };
        let r = o.planned_ref_price.unwrap_or(f64::NAN);
        let diff = (o.avg_fill_price.unwrap_or(f64::NAN) - r) * sign;
        bps.push(diff / r * 10_000.0);
        cost.push(diff * o.filled_qty.unwrap_or(f64::NAN));
    }
    Slippage {
        orders: df.len(),
        mean_bps: nanmean(&bps),
        median_bps: nanmedian(&bps),
        total_cost: nansum(&cost),
    }
}

/// `_fy(ts)`: the Indian financial year label (Apr–Mar).
#[must_use]
pub fn fy(ts: NaiveDateTime) -> String {
    let y = if ts.month() >= 4 {
        ts.year()
    } else {
        ts.year() - 1
    };
    let next = (y + 1).to_string();
    format!("{y}-{}", &next[next.len().saturating_sub(2)..])
}

#[allow(clippy::cast_possible_truncation)]
fn utc_from_secs(ts: f64) -> Option<NaiveDateTime> {
    if !ts.is_finite() {
        return None;
    }
    let ns = (ts * 1e9).round() as i64;
    DateTime::from_timestamp_nanos(ns).naive_utc().into()
}

/// One financial year of [`tax_drag`].
#[derive(Debug, Clone, PartialEq)]
pub struct FyTax {
    /// `fy`.
    pub fy: String,
    /// `stcg_gain`.
    pub stcg_gain: f64,
    /// `ltcg_gain`.
    pub ltcg_gain: f64,
    /// `stcg_tax`.
    pub stcg_tax: f64,
    /// `ltcg_tax`.
    pub ltcg_tax: f64,
    /// `total_tax`.
    pub total_tax: f64,
}

/// `tax_drag(...)`'s dict.
#[derive(Debug, Clone, PartialEq)]
pub struct TaxDrag {
    /// `stcg_tax`.
    pub stcg_tax: f64,
    /// `ltcg_tax`.
    pub ltcg_tax: f64,
    /// `total_tax`.
    pub total_tax: f64,
    /// `by_fy`, sorted.
    pub by_fy: Vec<FyTax>,
    /// `drag_pct`.
    pub drag_pct: f64,
}

/// `tax_drag(trades, avg_capital=, costs_deductible=)`: STCG/LTCG with the per-FY exemption.
#[must_use]
pub fn tax_drag(trades: &[Trade], avg_capital: Option<f64>, costs_deductible: bool) -> TaxDrag {
    let df = closed(trades);
    if df.is_empty() {
        return TaxDrag {
            stcg_tax: 0.0,
            ltcg_tax: 0.0,
            total_tax: 0.0,
            by_fy: Vec::new(),
            drag_pct: 0.0,
        };
    }
    let has_costs = df.iter().any(|t| t.costs.is_some());
    let mut work: BTreeMap<String, (Vec<f64>, Vec<f64>)> = BTreeMap::new();
    for t in &df {
        let exit = t.exit_ts.and_then(utc_from_secs);
        let entry = t.entry_ts.and_then(utc_from_secs);
        let long_term = match (entry, exit) {
            (Some(e), Some(x)) => {
                (x - e).num_milliseconds().div_euclid(86_400_000) >= LONG_TERM_DAYS
            }
            _ => false,
        };
        let mut gain = t.pnl;
        if !costs_deductible {
            gain += if has_costs {
                t.costs.unwrap_or(f64::NAN)
            } else {
                0.0
            };
        }
        let key = exit.map_or_else(String::new, fy);
        let slot = work.entry(key).or_default();
        if long_term {
            slot.1.push(gain);
        } else {
            slot.0.push(gain);
        }
    }
    let by_fy: Vec<FyTax> = work
        .into_iter()
        .map(|(fy, (st, lt))| {
            let st_gain = nansum(&st);
            let lt_gain = nansum(&lt);
            let st_tax = st_gain.max(0.0) * STCG_RATE;
            let lt_tax = (lt_gain - LTCG_EXEMPTION).max(0.0) * LTCG_RATE;
            FyTax {
                fy,
                stcg_gain: st_gain,
                ltcg_gain: lt_gain,
                stcg_tax: st_tax,
                ltcg_tax: lt_tax,
                total_tax: st_tax + lt_tax,
            }
        })
        .collect();
    let total = np_sum(&by_fy.iter().map(|r| r.total_tax).collect::<Vec<_>>());
    TaxDrag {
        stcg_tax: np_sum(&by_fy.iter().map(|r| r.stcg_tax).collect::<Vec<_>>()),
        ltcg_tax: np_sum(&by_fy.iter().map(|r| r.ltcg_tax).collect::<Vec<_>>()),
        total_tax: total,
        drag_pct: match avg_capital {
            Some(c) if c != 0.0 => total / c * 100.0,
            _ => 0.0,
        },
        by_fy,
    }
}

/// One row of a contribution table.
#[derive(Debug, Clone, PartialEq)]
pub struct Contribution {
    /// The symbol or cluster.
    pub key: String,
    /// `pnl`.
    pub pnl: f64,
    /// `trades`.
    pub trades: usize,
    /// `share_pct` of the absolute total.
    pub share_pct: f64,
}

fn contribution(groups: BTreeMap<String, Vec<f64>>) -> Vec<Contribution> {
    let mut out: Vec<Contribution> = groups
        .into_iter()
        .map(|(key, p)| Contribution {
            key,
            pnl: kahan_sum(&p),
            trades: p.len(),
            share_pct: 0.0,
        })
        .collect();
    let total = nansum(&out.iter().map(|c| c.pnl.abs()).collect::<Vec<_>>());
    for c in &mut out {
        #[allow(clippy::float_cmp)]
        let zero = total == 0.0;
        c.share_pct = if zero { 0.0 } else { c.pnl / total * 100.0 };
    }
    sort_desc_by(&mut out, |c| c.pnl);
    out
}

/// `per_symbol_contribution(trades)`.
#[must_use]
pub fn per_symbol_contribution(trades: &[Trade]) -> Vec<Contribution> {
    let mut g: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    for t in closed(trades) {
        g.entry(t.symbol.clone()).or_default().push(t.pnl);
    }
    contribution(g)
}

/// `per_sector_contribution(trades, sectors)`: unmapped symbols are `other`.
#[must_use]
pub fn per_sector_contribution(
    trades: &[Trade],
    sectors: &HashMap<String, String>,
) -> Vec<Contribution> {
    let mut g: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    for t in closed(trades) {
        let cluster = sectors
            .get(&t.symbol)
            .cloned()
            .unwrap_or_else(|| "other".to_owned());
        g.entry(cluster).or_default().push(t.pnl);
    }
    contribution(g)
}

// =============================================================================================
// momentum diagnostics
// =============================================================================================

/// `DEFAULT_SCORE_BUCKETS`.
pub const DEFAULT_SCORE_BUCKETS: [(f64, f64); 5] = [
    (0.0, 50.0),
    (50.0, 60.0),
    (60.0, 70.0),
    (70.0, 80.0),
    (80.0, 101.0),
];
/// `perf_by_rank_bucket`'s default buckets.
pub const DEFAULT_RANK_BUCKETS: [(i64, i64); 3] = [(1, 6), (6, 15), (15, 26)];

fn g_fmt(x: f64) -> String {
    crate::pyfmt::format_value(&crate::pyfmt::PyArg::Float(x), "g")
        .unwrap_or_else(|_| x.to_string())
}

fn bucketed(
    df: &[&Trade],
    col: impl Fn(&Trade) -> Option<f64>,
    edges: &[f64],
    labels: Vec<String>,
) -> Vec<GroupStats> {
    let mut groups: Vec<Vec<f64>> = vec![Vec::new(); labels.len()];
    for t in df {
        let Some(x) = col(t).filter(|x| !x.is_nan()) else {
            continue;
        };
        if let Some(i) = (0..labels.len()).find(|&i| edges[i] <= x && x < edges[i + 1]) {
            groups[i].push(t.pnl);
        }
    }
    labels
        .into_iter()
        .zip(groups)
        .map(|(k, p)| group_stats(k, &p))
        .collect()
}

/// `perf_by_score_bucket(trades, buckets)`: did higher scores pay?
#[must_use]
pub fn perf_by_score_bucket(trades: &[Trade], buckets: Option<&[(f64, f64)]>) -> Vec<GroupStats> {
    let df = closed(trades);
    if df.is_empty() || df.iter().all(|t| t.entry_score.is_none()) {
        return Vec::new();
    }
    let bs = buckets.unwrap_or(&DEFAULT_SCORE_BUCKETS);
    let Some(last) = bs.last() else {
        return Vec::new();
    };
    let mut edges: Vec<f64> = bs.iter().map(|b| b.0).collect();
    edges.push(last.1);
    let labels = bs
        .iter()
        .map(|(lo, hi)| format!("{}-{}", g_fmt(*lo), g_fmt(*hi)))
        .collect();
    bucketed(&df, |t| t.entry_score, &edges, labels)
}

/// `perf_by_rank_bucket(trades, buckets=)`: rank 1–5 vs 15–25.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn perf_by_rank_bucket(trades: &[Trade], buckets: &[(i64, i64)]) -> Vec<GroupStats> {
    let df = closed(trades);
    if df.is_empty() || df.iter().all(|t| t.entry_rank.is_none()) {
        return Vec::new();
    }
    let Some(last) = buckets.last() else {
        return Vec::new();
    };
    let mut edges: Vec<f64> = buckets.iter().map(|b| b.0 as f64).collect();
    edges.push(last.1 as f64);
    let labels = buckets
        .iter()
        .map(|(lo, hi)| format!("{lo}-{}", hi - 1))
        .collect();
    bucketed(&df, |t| t.entry_rank, &edges, labels)
}

/// `score_decay_vs_return(...)`'s dict.
#[derive(Debug, Clone, PartialEq)]
pub struct ScoreDecay {
    /// Rows with both a score and a return.
    pub n: usize,
    /// Pearson.
    pub pearson: f64,
    /// Spearman (Pearson of the ranks).
    pub spearman: f64,
}

/// `score_decay_vs_return(trades, return_col=)`: `return_of` stands in for `return_col`;
/// without it the return is `pnl / (entry_price * qty)`.
#[must_use]
pub fn score_decay_vs_return(
    trades: &[Trade],
    return_of: Option<fn(&Trade) -> Option<f64>>,
) -> ScoreDecay {
    let nan = |n| ScoreDecay {
        n,
        pearson: f64::NAN,
        spearman: f64::NAN,
    };
    let df = closed(trades);
    if df.is_empty() || df.iter().all(|t| t.entry_score.is_none()) {
        return nan(0);
    }
    let (s, r): (Vec<f64>, Vec<f64>) = df
        .iter()
        .map(|t| {
            let ret = match return_of {
                Some(f) => f(t).unwrap_or(f64::NAN),
                None => {
                    let basis = t.entry_price.unwrap_or(f64::NAN) * t.qty.unwrap_or(f64::NAN);
                    #[allow(clippy::float_cmp)]
                    let basis = if basis == 0.0 { f64::NAN } else { basis };
                    t.pnl / basis
                }
            };
            (t.entry_score.unwrap_or(f64::NAN), ret)
        })
        .filter(|(a, b)| !a.is_nan() && !b.is_nan())
        .unzip();
    if s.len() < 3 {
        return nan(s.len());
    }
    ScoreDecay {
        n: s.len(),
        pearson: corr(&s, &r),
        spearman: corr(&rank(&s), &rank(&r)),
    }
}

/// `exit_efficiency(...)`'s dict.
#[derive(Debug, Clone, PartialEq)]
pub struct ExitEfficiency {
    /// Rows with a forward return.
    pub n: usize,
    /// `-mean(forward return)`: positive means selling helped.
    pub efficiency: f64,
    /// `mean_fwd_return`.
    pub mean_fwd_return: f64,
    /// `pct_good_exits`.
    pub pct_good_exits: f64,
}

#[allow(clippy::cast_precision_loss)]
fn efficiency_of(df: &[&Trade]) -> ExitEfficiency {
    let fwd: Vec<f64> = df
        .iter()
        .filter_map(|t| t.fwd_return_20d)
        .filter(|x| !x.is_nan())
        .collect();
    if fwd.is_empty() {
        return ExitEfficiency {
            n: 0,
            efficiency: f64::NAN,
            mean_fwd_return: f64::NAN,
            pct_good_exits: f64::NAN,
        };
    }
    let m = nanmean(&fwd);
    ExitEfficiency {
        n: fwd.len(),
        efficiency: -m,
        mean_fwd_return: m,
        pct_good_exits: fwd.iter().filter(|x| **x < 0.0).count() as f64 / fwd.len() as f64 * 100.0,
    }
}

/// `exit_efficiency(trades)` over `fwd_return_20d`.
#[must_use]
pub fn exit_efficiency(trades: &[Trade]) -> ExitEfficiency {
    efficiency_of(&closed(trades))
}

/// `stop_loss_effectiveness(...)`'s dict.
#[derive(Debug, Clone, PartialEq)]
pub struct StopEffectiveness {
    /// Stop-outs.
    pub n: usize,
    /// As [`ExitEfficiency::efficiency`], over the stop-outs.
    pub efficiency: f64,
    /// Share of stop-outs followed by a fall.
    pub saved_by_stop_pct: f64,
    /// Share followed by a rise.
    pub shaken_out_pct: f64,
}

/// `stop_loss_effectiveness(trades, reason_match=)`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn stop_loss_effectiveness(trades: &[Trade], reason_match: &str) -> StopEffectiveness {
    let df = closed(trades);
    if df.is_empty() || df.iter().all(|t| t.exit_reason.is_none()) {
        return StopEffectiveness {
            n: 0,
            efficiency: f64::NAN,
            saved_by_stop_pct: f64::NAN,
            shaken_out_pct: f64::NAN,
        };
    }
    let needle = reason_match.to_lowercase();
    let stops: Vec<&Trade> = df
        .iter()
        .copied()
        .filter(|t| {
            t.exit_reason
                .as_ref()
                .is_some_and(|r| r.to_lowercase().contains(&needle))
        })
        .collect();
    let eff = efficiency_of(&stops);
    let has_fwd = df.iter().any(|t| t.fwd_return_20d.is_some());
    let (saved, shaken) = if has_fwd {
        let fwd: Vec<f64> = stops
            .iter()
            .filter_map(|t| t.fwd_return_20d)
            .filter(|x| !x.is_nan())
            .collect();
        if fwd.is_empty() {
            (f64::NAN, f64::NAN)
        } else {
            let n = fwd.len() as f64;
            (
                fwd.iter().filter(|x| **x < 0.0).count() as f64 / n * 100.0,
                fwd.iter().filter(|x| **x > 0.0).count() as f64 / n * 100.0,
            )
        }
    } else {
        (f64::NAN, f64::NAN)
    };
    StopEffectiveness {
        n: stops.len(),
        efficiency: eff.efficiency,
        saved_by_stop_pct: saved,
        shaken_out_pct: shaken,
    }
}

/// `cash_drag(...)`'s dict.
#[derive(Debug, Clone, PartialEq)]
pub struct CashDrag {
    /// `avg_cash_weight_pct`.
    pub avg_cash_weight_pct: f64,
    /// `max_cash_weight_pct`.
    pub max_cash_weight_pct: f64,
    /// `invested_leg_cagr`.
    pub invested_leg_cagr: f64,
    /// `estimated_drag_pct`.
    pub estimated_drag_pct: f64,
}

fn reindex_ffill(s: &[(NaiveDate, f64)], index: &[NaiveDate]) -> Vec<f64> {
    let m = lookup(&sorted(s));
    let mut last = f64::NAN;
    index
        .iter()
        .map(|d| {
            if let Some(v) = m.get(d).filter(|v| !v.is_nan()) {
                last = *v;
            }
            last
        })
        .collect()
}

/// `cash_drag(navs, cash, invested=, cashflows=)`: an estimate of the return foregone.
#[must_use]
pub fn cash_drag(
    navs: &[(NaiveDate, f64)],
    cash: &[(NaiveDate, f64)],
    invested: Option<&[(NaiveDate, f64)]>,
    cashflows: Option<&[(NaiveDate, f64)]>,
) -> CashDrag {
    let nav = sorted(navs);
    let index: Vec<NaiveDate> = nav.iter().map(|p| p.0).collect();
    let c = reindex_ffill(cash, &index);
    let weight: Vec<f64> = nav
        .iter()
        .zip(&c)
        .map(|((_, n), c)| {
            #[allow(clippy::float_cmp)]
            let den = if *n == 0.0 { f64::NAN } else { *n };
            c / den
        })
        .filter(|w| !w.is_nan())
        .collect();
    let avg_w = if weight.is_empty() {
        0.0
    } else {
        nanmean(&weight)
    };
    let inv_ret = match invested {
        Some(inv) => {
            let inv = reindex_ffill(inv, &index);
            let pos: Series = index
                .iter()
                .zip(&inv)
                .filter(|(_, v)| **v > 0.0)
                .map(|(d, v)| (*d, *v))
                .collect();
            if pos.is_empty() { 0.0 } else { cagr(&pos) }
        }
        None => cagr(&twr_chained(&nav, cashflows, 100.0)),
    };
    CashDrag {
        avg_cash_weight_pct: avg_w * 100.0,
        max_cash_weight_pct: weight
            .iter()
            .copied()
            .reduce(f64::max)
            .map_or(0.0, |m| m * 100.0),
        invested_leg_cagr: inv_ret,
        estimated_drag_pct: avg_w * inv_ret * 100.0,
    }
}

/// The capital `weekly_turnover_trend` divides by.
#[derive(Debug, Clone, PartialEq)]
pub enum Capital {
    /// One number for every week.
    Scalar(f64),
    /// A dated series, forward-filled onto the week labels.
    Series(Series),
}

/// One week of [`weekly_turnover_trend`].
#[derive(Debug, Clone, PartialEq)]
pub struct WeekTurnover {
    /// The week's label (its Sunday).
    pub week: NaiveDate,
    /// `traded_value`.
    pub traded_value: f64,
    /// `capital`.
    pub capital: f64,
    /// `turnover_pct`.
    pub turnover_pct: f64,
}

/// `weekly_turnover_trend(orders, capital)`: one-way traded value per W-SUN week.
#[must_use]
pub fn weekly_turnover_trend(orders: &[Order], capital: &Capital) -> Vec<WeekTurnover> {
    if orders.is_empty() || orders.iter().all(|o| o.created_ts.is_none()) {
        return Vec::new();
    }
    let has_filled = orders.iter().any(|o| o.filled_qty.is_some());
    let has_planned = orders.iter().any(|o| o.planned_qty.is_some());
    let has_fill_px = orders.iter().any(|o| o.avg_fill_price.is_some());
    let has_ref_px = orders.iter().any(|o| o.planned_ref_price.is_some());
    let mut weeks: BTreeMap<NaiveDate, Vec<f64>> = BTreeMap::new();
    for o in orders {
        let Some(when) = o.created_ts.and_then(utc_from_secs) else {
            continue;
        };
        let qty = if has_filled {
            o.filled_qty.unwrap_or(f64::NAN)
        } else if has_planned {
            o.planned_qty.unwrap_or(f64::NAN)
        } else {
            0.0
        };
        let px = if has_fill_px {
            o.avg_fill_price.unwrap_or(f64::NAN)
        } else if has_ref_px {
            o.planned_ref_price.unwrap_or(f64::NAN)
        } else {
            0.0
        };
        let day = when.date();
        let sunday = day + Duration::days(6 - i64::from(day.weekday().num_days_from_monday()));
        weeks.entry(sunday).or_default().push(qty.abs() * px);
    }
    let (Some(first), Some(last)) = (weeks.keys().next().copied(), weeks.keys().last().copied())
    else {
        return Vec::new();
    };
    let mut labels = Vec::new();
    let mut w = first;
    while w <= last {
        labels.push(w);
        w += Duration::days(7);
    }
    let caps: Vec<f64> = match capital {
        Capital::Scalar(c) => vec![*c; labels.len()],
        Capital::Series(s) => {
            let s = sorted(s);
            labels
                .iter()
                .map(|d| s.iter().rev().find(|p| p.0 <= *d).map_or(f64::NAN, |p| p.1))
                .collect()
        }
    };
    labels
        .into_iter()
        .zip(caps)
        .map(|(week, cap)| {
            let traded = weeks.get(&week).map_or(0.0, |v| nansum(v));
            WeekTurnover {
                week,
                traded_value: traded,
                capital: cap,
                turnover_pct: if cap > 0.0 { traded / cap * 100.0 } else { 0.0 },
            }
        })
        .collect()
}

// =============================================================================================
// benchmarking
// =============================================================================================

fn capture(port: &[(NaiveDate, f64)], bench: &[(NaiveDate, f64)], up: bool) -> f64 {
    let (p, b) = align(port, bench);
    let mask: Vec<bool> = b
        .iter()
        .map(|x| if up { x.1 > 0.0 } else { x.1 < 0.0 })
        .collect();
    if !mask.iter().any(|m| *m) {
        return f64::NAN;
    }
    let prod = |s: &Series| {
        s.iter()
            .zip(&mask)
            .filter(|(_, m)| **m)
            .map(|(x, _)| 1.0 + x.1)
            .filter(|x| !x.is_nan())
            .fold(1.0, |a, x| a * x)
    };
    let bp = prod(&b) - 1.0;
    #[allow(clippy::float_cmp)]
    if bp == 0.0 {
        return f64::NAN;
    }
    (prod(&p) - 1.0) / bp
}

/// `up_capture(port_monthly, bench_monthly)`.
#[must_use]
pub fn up_capture(port: &[(NaiveDate, f64)], bench: &[(NaiveDate, f64)]) -> f64 {
    capture(port, bench, true)
}

/// `down_capture(port_monthly, bench_monthly)`: below 1.0 is good.
#[must_use]
pub fn down_capture(port: &[(NaiveDate, f64)], bench: &[(NaiveDate, f64)]) -> f64 {
    capture(port, bench, false)
}

/// A rebased curve: one row per date, one column per series (NaN where absent).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Curve {
    /// The dates.
    pub index: Vec<NaiveDate>,
    /// `(name, values)` in column order.
    pub columns: Vec<(String, Vec<f64>)>,
}

impl Curve {
    /// `curve[name]`.
    #[must_use]
    pub fn col(&self, name: &str) -> Option<&[f64]> {
        self.columns
            .iter()
            .find(|c| c.0 == name)
            .map(|c| c.1.as_slice())
    }

    /// `curve.min().min()` (NaN skipped).
    #[must_use]
    pub fn min(&self) -> f64 {
        self.columns
            .iter()
            .flat_map(|c| c.1.iter().copied())
            .filter(|x| !x.is_nan())
            .reduce(f64::min)
            .unwrap_or(f64::NAN)
    }

    /// `curve.max().max()` (NaN skipped).
    #[must_use]
    pub fn max(&self) -> f64 {
        self.columns
            .iter()
            .flat_map(|c| c.1.iter().copied())
            .filter(|x| !x.is_nan())
            .reduce(f64::max)
            .unwrap_or(f64::NAN)
    }
}

/// `equity_curve_vs_benchmark(...)`'s dict.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct EquityCurve {
    /// `curve`.
    pub curve: Curve,
    /// `series_used`: what each line is (`NAV`, `TRI`, `PRI`, `UNKNOWN`).
    pub series_used: Vec<(String, String)>,
    /// `dropped`: benchmarks with no overlap.
    pub dropped: Vec<String>,
    /// `inception`.
    pub inception: Option<String>,
}

/// `equity_curve_vs_benchmark(port_index, benchmarks, base=, series_types=)`.
#[must_use]
pub fn equity_curve_vs_benchmark(
    port_index: &[(NaiveDate, f64)],
    benchmarks: &[(String, Series)],
    base: f64,
    series_types: &HashMap<String, String>,
) -> EquityCurve {
    let p = sorted_dropna(port_index);
    let Some(start) = p.first().map(|x| x.0) else {
        return EquityCurve::default();
    };
    let p0 = p[0].1;
    let mut cols: Vec<(String, Series)> = vec![(
        "portfolio".to_owned(),
        p.iter().map(|(d, v)| (*d, v / p0 * base)).collect(),
    )];
    let mut dropped = Vec::new();
    for (name, series) in benchmarks {
        let b: Series = sorted_dropna(series)
            .into_iter()
            .filter(|x| x.0 >= start)
            .collect();
        #[allow(clippy::float_cmp)]
        if b.is_empty() || b[0].1 == 0.0 {
            dropped.push(name.clone());
            continue;
        }
        let b0 = b[0].1;
        let rebased = b.iter().map(|(d, v)| (*d, v / b0 * base)).collect();
        match cols.iter_mut().find(|c| c.0 == *name) {
            Some(slot) => slot.1 = rebased,
            None => cols.push((name.clone(), rebased)),
        }
    }
    let mut index: Vec<NaiveDate> = cols.iter().flat_map(|c| c.1.iter().map(|x| x.0)).collect();
    index.sort();
    index.dedup();
    let mut columns: Vec<(String, Vec<f64>)> = cols
        .iter()
        .map(|(name, s)| {
            let m = lookup(s);
            let mut last = f64::NAN;
            let v = index
                .iter()
                .map(|d| {
                    if let Some(x) = m.get(d).filter(|x| !x.is_nan()) {
                        last = *x;
                    }
                    last
                })
                .collect();
            (name.clone(), v)
        })
        .collect();
    let keep: Vec<bool> = (0..index.len())
        .map(|i| columns.iter().any(|c| !c.1[i].is_nan()))
        .collect();
    let index: Vec<NaiveDate> = index
        .into_iter()
        .zip(&keep)
        .filter(|x| *x.1)
        .map(|x| x.0)
        .collect();
    for c in &mut columns {
        c.1 =
            c.1.iter()
                .zip(&keep)
                .filter(|x| *x.1)
                .map(|x| *x.0)
                .collect();
    }
    let mut used = vec![("portfolio".to_owned(), "NAV".to_owned())];
    for (name, _) in cols.iter().skip(1) {
        let kind = series_types
            .get(name)
            .cloned()
            .unwrap_or_else(|| "UNKNOWN".to_owned());
        match used.iter_mut().find(|u| u.0 == *name) {
            Some(slot) => slot.1 = kind,
            None => used.push((name.clone(), kind)),
        }
    }
    EquityCurve {
        curve: Curve { index, columns },
        series_used: used,
        dropped,
        inception: Some(start.format("%Y-%m-%d").to_string()),
    }
}

/// `pct_change().dropna()` of a level series.
#[must_use]
pub fn pct_change_dropna(s: &[(NaiveDate, f64)]) -> Series {
    let s = sorted(s);
    (1..s.len())
        .map(|i| (s[i].0, s[i].1 / s[i - 1].1 - 1.0))
        .filter(|p| !p.1.is_nan())
        .collect()
}

fn iso(d: NaiveDate) -> PyValue {
    PyValue::Str(d.format("%Y-%m-%d").to_string())
}

/// `summary(navs, cashflows, benchmark=, rf=, series_type=)`: the headline bundle.
#[must_use]
pub fn summary(
    navs: &[(NaiveDate, f64)],
    cashflows: Option<&[(NaiveDate, f64)]>,
    benchmark: Option<&[(NaiveDate, f64)]>,
    rf: f64,
    series_type: &str,
) -> PyDict {
    let nav = sorted(navs);
    let idx = twr_chained(&nav, cashflows, 100.0);
    let r = daily_returns(&nav, cashflows);
    let mdd = max_drawdown(&idx);
    let days = i64::try_from(idx.len()).unwrap_or(i64::MAX);
    let mut out = dict([
        ("inception", idx.first().map_or(PyValue::None, |p| iso(p.0))),
        ("as_of", idx.last().map_or(PyValue::None, |p| iso(p.0))),
        ("days", PyValue::Int(days)),
        (
            "index_value",
            idx.last().map_or(PyValue::None, |p| PyValue::Float(p.1)),
        ),
        ("absolute", PyValue::Float(absolute(&idx))),
        ("cagr", PyValue::Float(cagr(&idx))),
        ("twr", PyValue::Float(twr_total(&nav, cashflows))),
        ("ann_volatility", PyValue::Float(ann_volatility(&r))),
        ("sharpe", PyValue::Float(sharpe(&r, rf))),
        ("sortino", PyValue::Float(sortino(&r, rf))),
        ("calmar", PyValue::Float(calmar(&idx))),
        ("max_drawdown", PyValue::Float(mdd.depth)),
        (
            "max_drawdown_duration_days",
            PyValue::Int(mdd.duration_days),
        ),
        ("max_drawdown_recovered", PyValue::Bool(mdd.recovered)),
        ("current_drawdown", PyValue::Float(mdd.current_dd)),
        ("var95_parametric", PyValue::Float(var_parametric(&r, None))),
        ("var95_historical", PyValue::Float(var_historical(&r, 0.95))),
        ("benchmark_series", PyValue::None),
    ]);
    if let Some(b) = benchmark {
        let b = sorted(b);
        let br = pct_change_dropna(&b);
        let (mi, mb) = (monthly_returns(&idx), monthly_returns(&b));
        out.insert("benchmark_series", PyValue::Str(series_type.to_owned()));
        out.insert("beta", PyValue::Float(beta(&r, &br)));
        out.insert("alpha_jensen", PyValue::Float(alpha_jensen(&r, &br, rf)));
        out.insert("tracking_error", PyValue::Float(tracking_error(&r, &br)));
        out.insert(
            "information_ratio",
            PyValue::Float(information_ratio(&r, &br)),
        );
        out.insert("up_capture", PyValue::Float(up_capture(&mi, &mb)));
        out.insert("down_capture", PyValue::Float(down_capture(&mi, &mb)));
    }
    out
}
