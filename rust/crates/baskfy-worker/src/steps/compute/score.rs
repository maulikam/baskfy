//! The weekly book's Momentum Quality Score — `baskfy_core.score.apply_filters` and `score`
//! (pandas and NumPy in Python, "byte-for-byte what the desk was traded on"), with the
//! `DESK_CONFIG` values of `baskfy_core.desk_config`. No Rust home existed; the desk score is
//! its only worker caller, so it lives here (R2.3's pattern).
//!
//! Every pandas/NumPy operation is reproduced with its own semantics, because a 1-dp SCORE and
//! a rank turn last-bit differences into different rows:
//!
//! * missing values are `NaN` (the scan reaches pandas through `to_dicts()`, so an integer
//!   column with a `None` is float64 with `NaN`); every comparison with `NaN` is false;
//! * `Series.quantile([0.01, 0.99])` is `np.percentile(non_nan, q * 100.0)` with the linear
//!   method and NumPy's `_lerp` (`b - (b - a) * (1 - t)` when `t >= 0.5`);
//! * `Series.clip(lo, hi)` keeps `NaN` and treats a `NaN` bound as no bound;
//! * `Series.rank(pct=True)` is the average-tie rank over the non-`NaN` count;
//! * `sum(w * pct for ...)` starts from the integer 0 and adds left to right;
//! * `np.clip` and `np.maximum` propagate `NaN`; `Series.round(1)` is `rint(x * 10) / 10`;
//! * `sort_values("SCORE", ascending=False)` is pandas' `nargsort` over NumPy's **unstable**
//!   introsort (`kind="quicksort"`), reproduced in [`numpy_argsort`] — the tie order of equal
//!   SCOREs, and therefore their ranks, is that algorithm's.

/// `DESK_CONFIG.EXCLUDED_SYMBOLS`.
const EXCLUDED_SYMBOLS: [&str; 1] = ["SGBDE31III"];
/// `DESK_CONFIG.REJECT_SERIES`.
const REJECT_SERIES: [&str; 2] = ["BE", "BZ"];
/// `DESK_CONFIG.MIN_MEDIAN_DAILY_VALUE`.
const MIN_MEDIAN_DAILY_VALUE: f64 = 5e7;
/// `DESK_CONFIG.MAX_AWAY_FROM_HIGH`.
const MAX_AWAY_FROM_HIGH: f64 = -30.0;
/// `DESK_CONFIG.MAX_CIRCUITS_3M`.
const MAX_CIRCUITS_3M: f64 = 5.0;
/// `DESK_CONFIG.PENALTY_CIRCUITS_1Y`.
const PENALTY_CIRCUITS_1Y: f64 = 8.0;
/// `DESK_CONFIG.MOMENTUM_BLEND`, in its order, as indices into [`ScanRow::returns`]
/// (one_month, three_months, six_months, nine_months, one_year).
const MOMENTUM_BLEND: [(usize, f64); 5] = [(0, 0.10), (1, 0.30), (2, 0.30), (3, 0.15), (4, 0.15)];
/// `DESK_CONFIG.SHARPE_BLEND`, in its order, as indices into [`ScanRow::sharpe`].
const SHARPE_BLEND: [(usize, f64); 4] = [(1, 0.35), (2, 0.35), (3, 0.15), (4, 0.15)];

/// One row of the desk's scan frame (`score.required(DESK_CONFIG)`'s columns), missing as `NaN`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ScanRow {
    /// `symbol`.
    pub symbol: String,
    /// `series` (`None` is never in `REJECT_SERIES`).
    pub series: Option<String>,
    /// `close`.
    pub close: f64,
    /// `marketcap`.
    pub marketcap: f64,
    /// `median_volume_one_year`.
    pub median_volume_one_year: f64,
    /// `rsi_one_month`.
    pub rsi_one_month: f64,
    /// `volatility_one_year`.
    pub volatility_one_year: f64,
    /// `beta`.
    pub beta: f64,
    /// `circuits_three_months`.
    pub circuits_three_months: f64,
    /// `circuits_one_year`.
    pub circuits_one_year: f64,
    /// `positive_days_percent_three_months`.
    pub positive_days_3m: f64,
    /// `positive_days_percent_six_months`.
    pub positive_days_6m: f64,
    /// `ma_20`.
    pub ma_20: f64,
    /// `ma_50`.
    pub ma_50: f64,
    /// `ma_100`.
    pub ma_100: f64,
    /// `ma_200`.
    pub ma_200: f64,
    /// `away_from_high_one_year`.
    pub away_from_high_one_year: f64,
    /// `is_nifty_fno`.
    pub is_nifty_fno: f64,
    /// `absolute_return_{one_month, three_months, six_months, nine_months, one_year}`.
    pub returns: [f64; 5],
    /// `sharpe_return_{one_month, three_months, six_months, nine_months, one_year}`.
    pub sharpe: [f64; 5],
}

/// `score.score`'s added columns for one row of the input, in input order. Rejected rows (and
/// every row when nothing is eligible) carry `NaN`/`None` everywhere but `reject`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Scored {
    /// `reject` (`""` when eligible).
    pub reject: String,
    /// `SCORE`.
    pub score: f64,
    /// `rank` (1-based).
    pub rank: Option<i64>,
    /// `ext_over_20dma`.
    pub ext_over_20dma: f64,
    /// `A_trend` .. `F_penalty`.
    pub components: [f64; 6],
}

/// `apply_filters`: the reject reasons, concatenated in the desk's order.
pub(crate) fn reject_reason(u: &ScanRow) -> String {
    let mut reject = String::new();
    if u.ma_50.is_nan() || u.ma_200.is_nan() || (u.close < u.ma_50 && u.close < u.ma_200) {
        reject.push_str("below50&200DMA;");
    }
    if u.returns[1] < 0.0 && u.returns[2] < 0.0 {
        reject.push_str("neg3M&6M;");
    }
    if u.circuits_three_months > MAX_CIRCUITS_3M {
        reject.push_str("circuits;");
    }
    if u.median_volume_one_year.is_nan() || u.median_volume_one_year < MIN_MEDIAN_DAILY_VALUE {
        reject.push_str("illiquid;");
    }
    if u.away_from_high_one_year.is_nan() || u.away_from_high_one_year < MAX_AWAY_FROM_HIGH {
        reject.push_str("far_from_high;");
    }
    if u.series
        .as_deref()
        .is_some_and(|s| REJECT_SERIES.contains(&s))
    {
        reject.push_str("T2T_series;");
    }
    if EXCLUDED_SYMBOLS.contains(&u.symbol.as_str()) {
        reject.push_str("excluded_instrument;");
    }
    reject
}

/// NumPy's float `less`: NaN sorts last.
fn np_less(a: f64, b: f64) -> bool {
    a < b || (b.is_nan() && !a.is_nan())
}

/// NumPy's `aheapsort_` on `tosort[start..start + n]` (the introsort's depth-limit fallback).
fn aheapsort(v: &[f64], tosort: &mut [usize], start: usize, n: usize) {
    // 1-based heap indexing, as NumPy's `a = tosort - 1`.
    let get = |t: &[usize], i: usize| t[start + i - 1];
    let mut n = n;
    let mut l = n >> 1;
    while l > 0 {
        let tmp = get(tosort, l);
        let mut i = l;
        let mut j = l << 1;
        while j <= n {
            if j < n && np_less(v[get(tosort, j)], v[get(tosort, j + 1)]) {
                j += 1;
            }
            if np_less(v[tmp], v[get(tosort, j)]) {
                tosort[start + i - 1] = get(tosort, j);
                i = j;
                j += j;
            } else {
                break;
            }
        }
        tosort[start + i - 1] = tmp;
        l -= 1;
    }
    while n > 1 {
        let tmp = get(tosort, n);
        tosort[start + n - 1] = get(tosort, 1);
        n -= 1;
        let mut i = 1;
        let mut j = 2;
        while j <= n {
            if j < n && np_less(v[get(tosort, j)], v[get(tosort, j + 1)]) {
                j += 1;
            }
            if np_less(v[tmp], v[get(tosort, j)]) {
                tosort[start + i - 1] = get(tosort, j);
                i = j;
                j += j;
            } else {
                break;
            }
        }
        tosort[start + i - 1] = tmp;
    }
}

/// NumPy's `SMALL_QUICKSORT`.
const SMALL_QUICKSORT: usize = 15;

/// `np.argsort(v, kind="quicksort")` for float64 where NumPy has no SIMD argsort (arm64, the
/// box and the bed): `npysort/quicksort.cpp`'s `aquicksort_` — median-of-three introsort,
/// insertion sort below 16 elements, heapsort past `2 * floor(log2 n)` levels. Not stable.
pub(crate) fn numpy_argsort(v: &[f64]) -> Vec<usize> {
    let num = v.len();
    let mut t: Vec<usize> = (0..num).collect();
    if num <= 1 {
        return t;
    }
    let mut pl: usize = 0;
    let mut pr: usize = num - 1;
    let mut stack: Vec<(usize, usize, i64)> = Vec::new();
    let msb = i64::from(usize::BITS - 1 - num.leading_zeros());
    let mut cdepth: i64 = msb * 2;
    loop {
        if cdepth < 0 {
            aheapsort(v, &mut t, pl, pr - pl + 1);
        } else {
            while pr - pl > SMALL_QUICKSORT {
                let pm = pl + ((pr - pl) >> 1);
                if np_less(v[t[pm]], v[t[pl]]) {
                    t.swap(pm, pl);
                }
                if np_less(v[t[pr]], v[t[pm]]) {
                    t.swap(pr, pm);
                }
                if np_less(v[t[pm]], v[t[pl]]) {
                    t.swap(pm, pl);
                }
                let vp = v[t[pm]];
                let mut pi = pl;
                let mut pj = pr - 1;
                t.swap(pm, pj);
                loop {
                    pi += 1;
                    while np_less(v[t[pi]], vp) {
                        pi += 1;
                    }
                    pj -= 1;
                    while np_less(vp, v[t[pj]]) {
                        pj -= 1;
                    }
                    if pi >= pj {
                        break;
                    }
                    t.swap(pi, pj);
                }
                let pk = pr - 1;
                t.swap(pi, pk);
                cdepth -= 1;
                if pi - pl < pr - pi {
                    stack.push((pi + 1, pr, cdepth));
                    pr = pi - 1;
                } else {
                    stack.push((pl, pi - 1, cdepth));
                    pl = pi + 1;
                }
            }
            // insertion sort
            let mut pi = pl + 1;
            while pi <= pr {
                let vi = t[pi];
                let vp = v[vi];
                let mut pj = pi;
                while pj > pl && np_less(vp, v[t[pj - 1]]) {
                    t[pj] = t[pj - 1];
                    pj -= 1;
                }
                t[pj] = vi;
                pi += 1;
            }
        }
        match stack.pop() {
            None => break,
            Some((l, r, d)) => {
                pl = l;
                pr = r;
                cdepth = d;
            }
        }
    }
    t
}

/// pandas' `nargsort(items, kind="quicksort", ascending=False, na_position="last")`.
pub(crate) fn nargsort_descending(items: &[f64]) -> Vec<usize> {
    let non_nan_idx: Vec<usize> = (0..items.len()).filter(|i| !items[*i].is_nan()).collect();
    let nan_idx: Vec<usize> = (0..items.len()).filter(|i| items[*i].is_nan()).collect();
    let reversed_idx: Vec<usize> = non_nan_idx.iter().rev().copied().collect();
    let reversed_values: Vec<f64> = reversed_idx.iter().map(|i| items[*i]).collect();
    let order = numpy_argsort(&reversed_values);
    let mut indexer: Vec<usize> = order.iter().map(|k| reversed_idx[*k]).collect();
    indexer.reverse();
    indexer.extend(nan_idx);
    indexer
}

/// NumPy's `_lerp(a, b, t)`.
fn lerp(a: f64, b: f64, t: f64) -> f64 {
    let diff = b - a;
    if t >= 0.5 {
        b - diff * (1.0 - t)
    } else {
        a + diff * t
    }
}

/// `Series.quantile(q)` (linear): `np.percentile(non_nan, q * 100.0)`; `NaN` with no values.
pub(crate) fn pandas_quantile(values: &[f64], q: f64) -> f64 {
    let mut sorted: Vec<f64> = values.iter().copied().filter(|v| !v.is_nan()).collect();
    if sorted.is_empty() {
        return f64::NAN;
    }
    sorted.sort_by(f64::total_cmp);
    let n = sorted.len();
    // pandas multiplies by 100, NumPy's percentile divides by 100 again.
    let quantile = (q * 100.0) / 100.0;
    #[allow(clippy::cast_precision_loss)]
    let virtual_index = (n - 1) as f64 * quantile;
    let floor = virtual_index.floor();
    #[allow(clippy::cast_precision_loss)]
    let last = (n - 1) as f64;
    if virtual_index >= last {
        return lerp(sorted[n - 1], sorted[n - 1], virtual_index + 1.0);
    }
    if virtual_index < 0.0 {
        return lerp(sorted[0], sorted[0], virtual_index);
    }
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let previous = floor as usize;
    lerp(
        sorted[previous],
        sorted[previous + 1],
        virtual_index - floor,
    )
}

/// `Series.clip(lo, hi)`: `NaN` kept; a `NaN` bound is no bound.
fn pandas_clip(x: f64, lo: f64, hi: f64) -> f64 {
    if x.is_nan() {
        return x;
    }
    let mut out = x;
    // `where(x >= lo, lo)` then `where(x <= hi, hi)`, `x` not NaN here.
    if !lo.is_nan() && x < lo {
        out = lo;
    }
    if !hi.is_nan() && x > hi {
        out = hi;
    }
    out
}

/// `np.clip(x, lo, hi)`: `minimum(maximum(x, lo), hi)`, `NaN` propagating.
fn np_clip(x: f64, lo: f64, hi: f64) -> f64 {
    if x.is_nan() {
        return x;
    }
    x.max(lo).min(hi)
}

/// `_pct(s) = s.rank(pct=True) * 100`. Ties are exact float equality, as pandas'.
#[allow(clippy::float_cmp)]
pub(crate) fn pct_rank(values: &[f64]) -> Vec<f64> {
    let mut order: Vec<usize> = (0..values.len()).filter(|i| !values[*i].is_nan()).collect();
    order.sort_by(|a, b| values[*a].total_cmp(&values[*b]));
    let count = order.len();
    let mut out = vec![f64::NAN; values.len()];
    let mut start = 0;
    while start < count {
        let mut end = start + 1;
        while end < count && values[order[end]] == values[order[start]] {
            end += 1;
        }
        let sum: usize = (start + 1..=end).sum();
        #[allow(clippy::cast_precision_loss)]
        let rank = sum as f64 / (end - start) as f64;
        for i in &order[start..end] {
            #[allow(clippy::cast_precision_loss)]
            let pct = rank / count as f64;
            out[*i] = pct * 100.0;
        }
        start = end;
    }
    out
}

/// `np.where(cond, a, b)` as a float.
fn pick(cond: bool, a: f64, b: f64) -> f64 {
    if cond { a } else { b }
}

/// `between(lo, hi)`, inclusive.
fn between(x: f64, lo: f64, hi: f64) -> bool {
    x >= lo && x <= hi
}

/// `score.score(u, DESK_CONFIG)`: the filters, then the eligible rows clipped, scored and
/// ranked; one [`Scored`] per input row, in input order.
pub(crate) fn score(u: &[ScanRow]) -> Vec<Scored> {
    let rejects: Vec<String> = u.iter().map(reject_reason).collect();
    let mut out: Vec<Scored> = rejects
        .iter()
        .map(|r| Scored {
            reject: r.clone(),
            score: f64::NAN,
            rank: None,
            ext_over_20dma: f64::NAN,
            components: [f64::NAN; 6],
        })
        .collect();
    let eligible: Vec<usize> = (0..u.len()).filter(|i| rejects[*i].is_empty()).collect();
    if eligible.is_empty() {
        return out;
    }
    let mut e: Vec<ScanRow> = eligible.iter().map(|i| u[*i].clone()).collect();

    // ret_cols(cfg) + SHP_COLS, each clipped to its own 1st/99th percentile.
    for k in 0..5 {
        let column: Vec<f64> = e.iter().map(|r| r.returns[k]).collect();
        let (lo, hi) = (
            pandas_quantile(&column, 0.01),
            pandas_quantile(&column, 0.99),
        );
        for r in &mut e {
            r.returns[k] = pandas_clip(r.returns[k], lo, hi);
        }
    }
    for k in 0..5 {
        let column: Vec<f64> = e.iter().map(|r| r.sharpe[k]).collect();
        let (lo, hi) = (
            pandas_quantile(&column, 0.01),
            pandas_quantile(&column, 0.99),
        );
        for r in &mut e {
            r.sharpe[k] = pandas_clip(r.sharpe[k], lo, hi);
        }
    }

    let n = e.len();
    let ext: Vec<f64> = e
        .iter()
        .map(|r| (r.close / r.ma_20 - 1.0) * 100.0)
        .collect();

    // A — trend.
    let a_trend: Vec<f64> = e
        .iter()
        .zip(&ext)
        .map(|(r, x)| {
            let b = |c: bool| i64::from(c);
            let mut a = 4 * b(r.close > r.ma_20)
                + 4 * b(r.close > r.ma_50)
                + 4 * b(r.close > r.ma_100)
                + 4 * b(r.close > r.ma_200)
                + 5 * b(r.close > r.ma_20
                    && r.ma_20 > r.ma_50
                    && r.ma_50 > r.ma_100
                    && r.ma_100 > r.ma_200);
            a += if between(*x, 0.0, 12.0) {
                4
            } else if between(*x, 12.0, 20.0) {
                2
            } else {
                0
            };
            #[allow(clippy::cast_precision_loss)]
            let clipped = a.clamp(0, 25) as f64;
            clipped
        })
        .collect();

    // B — momentum.
    let mut b_sum = vec![0.0_f64; n];
    for (k, w) in MOMENTUM_BLEND {
        let pct = pct_rank(&e.iter().map(|r| r.returns[k]).collect::<Vec<_>>());
        for i in 0..n {
            b_sum[i] += w * pct[i];
        }
    }
    let b_momentum: Vec<f64> = (0..n)
        .map(|i| {
            let r = &e[i];
            let mut b = b_sum[i] / 100.0 * 25.0;
            b -= pick(r.returns[0] > 25.0 && r.returns[2] < 40.0, 4.0, 0.0);
            b -= pick(r.returns[0] > 35.0, 3.0, 0.0);
            np_clip(b, 0.0, 25.0)
        })
        .collect();

    // C — Sharpe.
    let mut c_sum = vec![0.0_f64; n];
    for (k, w) in SHARPE_BLEND {
        let pct = pct_rank(&e.iter().map(|r| r.sharpe[k]).collect::<Vec<_>>());
        for i in 0..n {
            c_sum[i] += w * pct[i];
        }
    }
    let c_sharpe: Vec<f64> = c_sum
        .iter()
        .map(|c| np_clip(c / 100.0 * 20.0, 0.0, 20.0))
        .collect();

    // D — consistency.
    let p3 = pct_rank(&e.iter().map(|r| r.positive_days_3m).collect::<Vec<_>>());
    let p6 = pct_rank(&e.iter().map(|r| r.positive_days_6m).collect::<Vec<_>>());
    let d_consistency: Vec<f64> = (0..n)
        .map(|i| {
            let away = e[i].away_from_high_one_year;
            let mut d = (0.4 * p3[i] + 0.3 * p6[i]) / 100.0 * 6.0;
            d += pick(away >= -8.0, 4.0, pick(away >= -15.0, 2.5, 0.0));
            np_clip(d, 0.0, 10.0)
        })
        .collect();

    // E — liquidity.
    let pm = pct_rank(
        &e.iter()
            .map(|r| r.median_volume_one_year)
            .collect::<Vec<_>>(),
    );
    let e_liquidity: Vec<f64> = (0..n)
        .map(|i| {
            let r = &e[i];
            let mut liq = pm[i] / 100.0 * 6.0;
            // `e.is_nifty_fno == 1`: the carried flag is exactly 0 or 1.
            #[allow(clippy::float_cmp)]
            let fno = r.is_nifty_fno == 1.0;
            liq += pick(fno, 2.0, 0.0);
            liq += pick(
                r.marketcap > 20000.0,
                2.0,
                pick(r.marketcap > 10000.0, 1.0, 0.0),
            );
            np_clip(liq, 0.0, 10.0)
        })
        .collect();

    // F — penalties.
    let f_penalty: Vec<f64> = (0..n)
        .map(|i| {
            let r = &e[i];
            let x = ext[i];
            let mut f = 0.0_f64;
            f -= pick(
                r.rsi_one_month > 82.0,
                4.0,
                pick(r.rsi_one_month > 78.0, 2.0, 0.0),
            );
            f -= pick(
                r.volatility_one_year > 0.55,
                3.0,
                pick(r.volatility_one_year > 0.45, 1.5, 0.0),
            );
            f -= pick(r.beta > 1.6, 2.0, 0.0);
            f -= pick(r.circuits_one_year > PENALTY_CIRCUITS_1Y, 2.0, 0.0);
            f -= pick(x > 25.0, 3.0, pick(x > 18.0, 1.5, 0.0));
            if f.is_nan() { f } else { f.max(-10.0) }
        })
        .collect();

    let scores: Vec<f64> = (0..n)
        .map(|i| {
            let total = a_trend[i]
                + b_momentum[i]
                + c_sharpe[i]
                + d_consistency[i]
                + e_liquidity[i]
                + f_penalty[i];
            super::numeric::numpy_round(total, 1)
        })
        .collect();
    let order = nargsort_descending(&scores);
    for (position, k) in order.iter().enumerate() {
        let target = &mut out[eligible[*k]];
        target.score = scores[*k];
        target.rank = Some(i64::try_from(position + 1).unwrap_or(i64::MAX));
        target.ext_over_20dma = super::numeric::numpy_round(ext[*k], 1);
        target.components = [
            a_trend[*k],
            b_momentum[*k],
            c_sharpe[*k],
            d_consistency[*k],
            e_liquidity[*k],
            f_penalty[*k],
        ];
    }
    out
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::float_cmp)]
    use super::*;

    fn row(symbol: &str) -> ScanRow {
        ScanRow {
            symbol: symbol.to_owned(),
            series: Some("EQ".to_owned()),
            close: 110.0,
            marketcap: 25_000.0,
            median_volume_one_year: 1e8,
            rsi_one_month: 60.0,
            volatility_one_year: 0.3,
            beta: 1.0,
            circuits_three_months: 0.0,
            circuits_one_year: 0.0,
            positive_days_3m: 55.0,
            positive_days_6m: 52.0,
            ma_20: 105.0,
            ma_50: 100.0,
            ma_100: 95.0,
            ma_200: 90.0,
            away_from_high_one_year: -5.0,
            is_nifty_fno: 1.0,
            returns: [5.0, 10.0, 20.0, 25.0, 40.0],
            sharpe: [1.0, 1.5, 2.0, 2.0, 2.5],
        }
    }

    #[test]
    fn filters_reject_in_the_desks_order_and_null_is_a_reject() {
        let mut r = row("A");
        r.ma_50 = f64::NAN;
        r.returns[1] = -1.0;
        r.returns[2] = -1.0;
        r.circuits_three_months = 6.0;
        r.median_volume_one_year = f64::NAN;
        r.away_from_high_one_year = -31.0;
        r.series = Some("BE".to_owned());
        r.symbol = "SGBDE31III".to_owned();
        assert_eq!(
            reject_reason(&r),
            "below50&200DMA;neg3M&6M;circuits;illiquid;far_from_high;T2T_series;excluded_instrument;"
        );
        assert_eq!(reject_reason(&row("B")), "");
        let mut none_series = row("C");
        none_series.series = None;
        assert_eq!(reject_reason(&none_series), "");
    }

    #[test]
    fn a_rejected_row_is_unscored_and_unranked() {
        let mut bad = row("BAD");
        bad.away_from_high_one_year = -50.0;
        let out = score(&[row("GOOD"), bad]);
        assert_eq!(out[0].rank, Some(1));
        assert!(!out[0].score.is_nan());
        assert_eq!(out[1].reject, "far_from_high;");
        assert!(out[1].score.is_nan());
        assert_eq!(out[1].rank, None);
    }

    #[test]
    fn the_trend_component_is_capped_at_25() {
        // All four averages under the close, stacked, extension 4.76% in (0, 12]: 16 + 5 + 4.
        let out = score(&[row("A")]);
        assert_eq!(out[0].components[0], 25.0);
    }

    #[test]
    fn quantile_is_numpy_linear() {
        let v: Vec<f64> = (1..=11).map(f64::from).collect();
        assert_eq!(pandas_quantile(&v, 0.99), 10.9);
        assert_eq!(pandas_quantile(&v, 0.01), 1.1);
        assert!(pandas_quantile(&[f64::NAN], 0.5).is_nan());
        assert_eq!(pandas_quantile(&[3.0], 0.99), 3.0);
    }

    #[test]
    fn pct_rank_averages_ties_over_the_non_nan_count() {
        let p = pct_rank(&[1.0, f64::NAN, 2.0, 2.0]);
        assert_eq!(p[0], 1.0 / 3.0 * 100.0);
        assert!(p[1].is_nan());
        assert_eq!(p[2], 2.5 / 3.0 * 100.0);
    }

    /// Differential check against Python's `score.score` + `desk_score_service._stored` over
    /// generated scans (`BASKFY_SCORE_DIFF_JSON` names the dump; see DECISIONS-RS R7.x of the
    /// compute lane). Ignored by default: the dump is generated, not committed.
    #[test]
    #[ignore = "needs BASKFY_SCORE_DIFF_JSON (a dump written by the lane's score_gen.py)"]
    fn score_matches_python_on_generated_scans() {
        use std::str::FromStr;

        use rust_decimal::Decimal;

        let path = std::env::var("BASKFY_SCORE_DIFF_JSON").unwrap();
        let text = std::fs::read_to_string(path).unwrap();
        let cases: serde_json::Value = serde_json::from_str(&text).unwrap();
        let f = |v: &serde_json::Value| v.as_f64().unwrap_or(f64::NAN);
        let mut checked = 0;
        for case in cases.as_array().unwrap() {
            let rows: Vec<ScanRow> = case["rows"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| {
                    let w = [
                        "one_month",
                        "three_months",
                        "six_months",
                        "nine_months",
                        "one_year",
                    ];
                    ScanRow {
                        symbol: r["symbol"].as_str().unwrap().to_owned(),
                        series: r["series"].as_str().map(str::to_owned),
                        close: f(&r["close"]),
                        marketcap: f(&r["marketcap"]),
                        median_volume_one_year: f(&r["median_volume_one_year"]),
                        rsi_one_month: f(&r["rsi_one_month"]),
                        volatility_one_year: f(&r["volatility_one_year"]),
                        beta: f(&r["beta"]),
                        circuits_three_months: f(&r["circuits_three_months"]),
                        circuits_one_year: f(&r["circuits_one_year"]),
                        positive_days_3m: f(&r["positive_days_percent_three_months"]),
                        positive_days_6m: f(&r["positive_days_percent_six_months"]),
                        ma_20: f(&r["ma_20"]),
                        ma_50: f(&r["ma_50"]),
                        ma_100: f(&r["ma_100"]),
                        ma_200: f(&r["ma_200"]),
                        away_from_high_one_year: f(&r["away_from_high_one_year"]),
                        is_nifty_fno: f(&r["is_nifty_fno"]),
                        returns: w.map(|k| f(&r[format!("absolute_return_{k}").as_str()])),
                        sharpe: w.map(|k| f(&r[format!("sharpe_return_{k}").as_str()])),
                    }
                })
                .collect();
            let out = score(&rows);
            let want = &case["out"];
            let dec = |v: &serde_json::Value| v.as_str().map(|s| Decimal::from_str(s).unwrap());
            for (i, s) in out.iter().enumerate() {
                let rejected = !s.reject.is_empty();
                assert_eq!(
                    s.reject,
                    want["reject"][i].as_str().unwrap(),
                    "reject row {i}"
                );
                let got_score = if rejected {
                    None
                } else {
                    crate::steps::compute::numeric::quantize_half_up_repr(s.score, 1).unwrap()
                };
                assert_eq!(got_score, dec(&want["score"][i]), "score row {i}");
                let got_rank = if rejected { None } else { s.rank };
                assert_eq!(got_rank, want["rank"][i].as_i64(), "rank row {i}");
                let names = [
                    "A_trend",
                    "B_momentum",
                    "C_sharpe",
                    "D_consistency",
                    "E_liquidity",
                    "F_penalty",
                    "ext_over_20dma",
                ];
                for (k, name) in names.iter().enumerate() {
                    let x = if k < 6 {
                        s.components[k]
                    } else {
                        s.ext_over_20dma
                    };
                    let got = if rejected {
                        None
                    } else {
                        crate::steps::compute::numeric::quantize_half_up_repr(x, 4).unwrap()
                    };
                    assert_eq!(got, dec(&want[*name][i]), "{name} row {i}");
                }
                checked += 1;
            }
        }
        assert!(checked > 0);
    }

    #[test]
    fn argsort_is_numpys_unstable_introsort() {
        // Cases checked against np.argsort(kind="quicksort") on arm64 (numpy 2.5.2).
        let x = [
            0.0, 3.0, 0.0, 1.0, 3.0, 3.0, 1.0, 2.0, 1.0, 0.0, 2.0, 3.0, 1.0, 1.0, 3.0, 0.0, 1.0,
            3.0, 2.0, 1.0,
        ];
        assert_eq!(
            numpy_argsort(&x),
            vec![
                0, 15, 9, 2, 3, 16, 13, 12, 19, 6, 8, 18, 10, 7, 5, 14, 1, 4, 17, 11
            ]
        );
        let small = [2.0, 1.0, 2.0, 1.0];
        assert_eq!(numpy_argsort(&small), vec![1, 3, 0, 2]);
    }
}
