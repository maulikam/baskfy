//! The numpy and pandas arithmetic the weekly scorer and planner are traded on, reproduced bit
//! for bit (graded by `fixtures/weekly/primitives.json`, dumped from numpy 2.5 / pandas 3.0 by
//! `tools/parity/desk_weekly_goldens.py`).
//!
//! * [`argsort_quicksort`] — `np.argsort(kind="quicksort")` for float64: numpy's introsort
//!   (median-of-three quicksort, insertion sort under 16, heapsort past `2·log2(n)` depth). It is
//!   **not stable**, and a tied SCORE's rank is whatever this order says, so it is ported rather
//!   than replaced by a stable sort.
//! * [`nargsort`] — `pandas.core.sorting.nargsort` (what `sort_values` calls): NaN last, the
//!   reverse-argsort-reverse trick for descending.
//! * [`rank_pct`] — `Series.rank(pct=True)` (average ties, NaN kept, divided by the non-NaN count).
//! * [`quantile`] — `Series.quantile(q)` (numpy's linear method, NaN skipped).
//! * [`np_round`] — `Series.round(d)` / `np.round` (scale, `rint`, unscale).
//! * [`py_round`] / [`py_round_int`] — Python's `round(x, n)` (correctly rounded) and `round(x)`.

use std::cmp::Ordering;

/// numpy's `Tag::less` for doubles: NaN sorts after every number.
fn less(a: f64, b: f64) -> bool {
    a < b || (b.is_nan() && !a.is_nan())
}

const SMALL_QUICKSORT: usize = 15;

fn msb(mut n: usize) -> i64 {
    let mut d = 0;
    while n > 1 {
        n >>= 1;
        d += 1;
    }
    d
}

fn aheapsort(v: &[f64], t: &mut [usize], start: usize, n: usize) {
    // 1-based heap over t[start .. start + n].
    let at = |k: usize| start + k - 1;
    let mut l = n >> 1;
    while l > 0 {
        let tmp = t[at(l)];
        let (mut i, mut j) = (l, l << 1);
        while j <= n {
            if j < n && less(v[t[at(j)]], v[t[at(j + 1)]]) {
                j += 1;
            }
            if less(v[tmp], v[t[at(j)]]) {
                t[at(i)] = t[at(j)];
                i = j;
                j += j;
            } else {
                break;
            }
        }
        t[at(i)] = tmp;
        l -= 1;
    }
    let mut n = n;
    while n > 1 {
        let tmp = t[at(n)];
        t[at(n)] = t[at(1)];
        n -= 1;
        let (mut i, mut j) = (1, 2);
        while j <= n {
            if j < n && less(v[t[at(j)]], v[t[at(j + 1)]]) {
                j += 1;
            }
            if less(v[tmp], v[t[at(j)]]) {
                t[at(i)] = t[at(j)];
                i = j;
                j += j;
            } else {
                break;
            }
        }
        t[at(i)] = tmp;
    }
}

/// `np.argsort(v, kind="quicksort")` for a float64 array (numpy's `aquicksort_`).
#[must_use]
pub fn argsort_quicksort(v: &[f64]) -> Vec<usize> {
    let num = v.len();
    let mut t: Vec<usize> = (0..num).collect();
    if num <= 1 {
        return t;
    }
    let (mut pl, mut pr) = (0_usize, num - 1);
    let mut stack: Vec<(usize, usize)> = Vec::new();
    let mut depth: Vec<i64> = Vec::new();
    let mut cdepth = msb(num) * 2;
    loop {
        if cdepth < 0 {
            aheapsort(v, &mut t, pl, pr - pl + 1);
        } else {
            while pr - pl > SMALL_QUICKSORT {
                let pm = pl + ((pr - pl) >> 1);
                if less(v[t[pm]], v[t[pl]]) {
                    t.swap(pm, pl);
                }
                if less(v[t[pr]], v[t[pm]]) {
                    t.swap(pr, pm);
                }
                if less(v[t[pm]], v[t[pl]]) {
                    t.swap(pm, pl);
                }
                let vp = v[t[pm]];
                let mut pi = pl;
                let mut pj = pr - 1;
                t.swap(pm, pj);
                loop {
                    pi += 1;
                    while less(v[t[pi]], vp) {
                        pi += 1;
                    }
                    pj -= 1;
                    while less(vp, v[t[pj]]) {
                        pj -= 1;
                    }
                    if pi >= pj {
                        break;
                    }
                    t.swap(pi, pj);
                }
                let pk = pr - 1;
                t.swap(pi, pk);
                if pi - pl < pr - pi {
                    stack.push((pi + 1, pr));
                    pr = pi - 1;
                } else {
                    stack.push((pl, pi - 1));
                    pl = pi + 1;
                }
                cdepth -= 1;
                depth.push(cdepth);
            }
            for i in (pl + 1)..=pr {
                let vi = t[i];
                let vp = v[vi];
                let mut j = i;
                while j > pl && less(vp, v[t[j - 1]]) {
                    t[j] = t[j - 1];
                    j -= 1;
                }
                t[j] = vi;
            }
        }
        match (stack.pop(), depth.pop()) {
            (Some((l, r)), Some(d)) => {
                pl = l;
                pr = r;
                cdepth = d;
            }
            _ => break,
        }
    }
    t
}

/// `pandas.core.sorting.nargsort(values, kind="quicksort", ascending, na_position="last")`:
/// the row order `DataFrame.sort_values(col, ascending=...)` produces.
#[must_use]
pub fn nargsort(values: &[f64], ascending: bool) -> Vec<usize> {
    let mut non_nan: Vec<f64> = Vec::new();
    let mut non_nan_idx: Vec<usize> = Vec::new();
    let mut nan_idx: Vec<usize> = Vec::new();
    for (i, v) in values.iter().enumerate() {
        if v.is_nan() {
            nan_idx.push(i);
        } else {
            non_nan.push(*v);
            non_nan_idx.push(i);
        }
    }
    if !ascending {
        non_nan.reverse();
        non_nan_idx.reverse();
    }
    let order = argsort_quicksort(&non_nan);
    let mut indexer: Vec<usize> = order.iter().map(|&k| non_nan_idx[k]).collect();
    if !ascending {
        indexer.reverse();
    }
    indexer.extend(nan_idx);
    indexer
}

/// `Series.rank(pct=True)` (method "average", NaN stays NaN): the averaged 1-based rank divided
/// by the count of non-NaN values.
#[must_use]
#[allow(clippy::cast_precision_loss)]
#[allow(clippy::float_cmp)] // a tie is pandas' exact `==` (so -0.0 ties 0.0)
pub fn rank_pct(values: &[f64]) -> Vec<f64> {
    let mut idx: Vec<usize> = (0..values.len()).filter(|&i| !values[i].is_nan()).collect();
    // A stable ascending order; ties are averaged, so the tie order inside a group is moot.
    idx.sort_by(|&a, &b| values[a].partial_cmp(&values[b]).unwrap_or(Ordering::Equal));
    let n = idx.len() as f64;
    let mut out = vec![f64::NAN; values.len()];
    let mut i = 0;
    while i < idx.len() {
        let mut j = i;
        while j + 1 < idx.len() && values[idx[j + 1]] == values[idx[i]] {
            j += 1;
        }
        let mut sum_ranks = 0.0_f64;
        for k in i..=j {
            sum_ranks += (k + 1) as f64;
        }
        let dups = (j - i + 1) as f64;
        let avg = sum_ranks / dups;
        for &k in &idx[i..=j] {
            out[k] = avg / n;
        }
        i = j + 1;
    }
    out
}

/// `Series.quantile(q)` with the default linear interpolation; NaN skipped; NaN if empty.
#[must_use]
#[allow(
    clippy::cast_precision_loss,
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss
)]
pub fn quantile(values: &[f64], q: f64) -> f64 {
    let mut v: Vec<f64> = values.iter().copied().filter(|x| !x.is_nan()).collect();
    if v.is_empty() {
        return f64::NAN;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    // pandas passes q * 100 to np.percentile, which divides by 100 again.
    let q = (q * 100.0) / 100.0;
    let n = v.len();
    let virtual_index = (n as f64 - 1.0) * q;
    let prev = virtual_index.floor();
    let gamma = virtual_index - prev;
    let lo = (prev.max(0.0) as usize).min(n - 1);
    let hi = (lo + 1).min(n - 1);
    let (a, b) = (v[lo], v[hi]);
    let diff = b - a;
    if gamma >= 0.5 {
        b - diff * (1.0 - gamma)
    } else {
        a + diff * gamma
    }
}

/// `np.round(x, decimals)` / `Series.round(decimals)` for `decimals >= 0`.
#[must_use]
pub fn np_round(x: f64, decimals: i32) -> f64 {
    let f = 10_f64.powi(decimals);
    (x * f).round_ties_even() / f
}

/// Python's `round(x, ndigits)` for a float: the double nearest the correctly rounded decimal
/// (NaN and infinities pass through).
#[must_use]
pub fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.ndigits$}").parse::<f64>().unwrap_or(x)
}

/// Python's `round(x)` to an `int` (half to even). `None` for NaN/inf (Python raises).
#[must_use]
#[allow(clippy::cast_possible_truncation)]
pub fn py_round_int(x: f64) -> Option<i64> {
    if !x.is_finite() {
        return None;
    }
    Some(x.round_ties_even() as i64)
}

/// `int(x)` for a float (truncation toward zero). `None` for NaN/inf (Python raises).
#[must_use]
#[allow(clippy::cast_possible_truncation)]
pub fn py_int(x: f64) -> Option<i64> {
    if !x.is_finite() {
        return None;
    }
    Some(x.trunc() as i64)
}

/// `pd.Series.clip(lo, hi)`: NaN stays NaN; a NaN bound is no bound.
#[must_use]
pub fn clip(x: f64, lo: f64, hi: f64) -> f64 {
    if x.is_nan() {
        return x;
    }
    let mut y = x;
    if !lo.is_nan() && y < lo {
        y = lo;
    }
    if !hi.is_nan() && y > hi {
        y = hi;
    }
    y
}

/// `np.clip(x, lo, hi)` for finite bounds (`minimum(maximum(x, lo), hi)`, NaN propagating).
#[must_use]
pub fn np_clip(x: f64, lo: f64, hi: f64) -> f64 {
    if x.is_nan() {
        return x;
    }
    x.max(lo).min(hi)
}

/// A Python number as `sum()` sees it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Num {
    /// `int`.
    Int(i64),
    /// `float`.
    Float(f64),
}

impl Num {
    /// `float(x)`.
    #[must_use]
    #[allow(clippy::cast_precision_loss)]
    pub const fn as_f64(self) -> f64 {
        match self {
            Self::Int(i) => i as f64,
            Self::Float(f) => f,
        }
    }

    /// `a * b` with Python's int/float promotion.
    #[must_use]
    #[allow(clippy::should_implement_trait)]
    pub fn mul(self, other: Self) -> Self {
        match (self, other) {
            (Self::Int(a), Self::Int(b)) => a
                .checked_mul(b)
                .map_or_else(|| Self::Float(self.as_f64() * other.as_f64()), Self::Int),
            _ => Self::Float(self.as_f64() * other.as_f64()),
        }
    }
}

/// CPython 3.12's built-in `sum(items)` (start `0`): ints added exactly while every item is an
/// int; from the first float on, Neumaier-compensated over floats, with later ints added
/// uncompensated, and the compensation folded in at the end when finite and non-zero.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn py_sum(items: impl IntoIterator<Item = Num>) -> Num {
    let mut i_res: i64 = 0;
    let mut float_mode = false;
    let (mut f, mut c) = (0.0_f64, 0.0_f64);
    for it in items {
        if !float_mode {
            match it {
                Num::Int(i) => {
                    i_res = i_res.wrapping_add(i);
                    continue;
                }
                Num::Float(x) => {
                    float_mode = true;
                    f = i_res as f64 + x;
                    c = 0.0;
                    continue;
                }
            }
        }
        match it {
            Num::Float(x) => {
                let t = f + x;
                if f.abs() >= x.abs() {
                    c += (f - t) + x;
                } else {
                    c += (x - t) + f;
                }
                f = t;
            }
            Num::Int(i) => f += i as f64,
        }
    }
    if !float_mode {
        return Num::Int(i_res);
    }
    if c != 0.0 && c.is_finite() {
        f += c;
    }
    Num::Float(f)
}
