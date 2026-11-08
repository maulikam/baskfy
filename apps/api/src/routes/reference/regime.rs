//! `baskfy_core.instrument_regime.classify_series` — the factsheet's Wasserstein regime label and
//! its two distances — reproduced **operation for operation** against numpy 2.5 (the decile
//! venv's), because the distances are floats on the wire and are graded at zero tolerance:
//!
//! * `ndarray.sum(axis=1)` and `np.mean` are numpy's **pairwise** summation (eight accumulators up
//!   to 128 elements, halving above), after the identity `0.0`;
//! * `np.argsort` is numpy's default introsort (`aquicksort`: median of three, insertion sort at
//!   sixteen, heapsort past the depth limit) — *not* a stable sort, so equal window sums are
//!   ordered exactly as numpy orders them;
//! * `np.quantile(..., method="linear")` is `(n - 1) * q`, `floor`, and numpy's `_lerp`, which
//!   interpolates from the upper neighbour when `gamma >= 0.5`;
//! * `np.linspace(0, 1, num)` is `i * (1 / (num - 1))` with the last point pinned to `1.0`;
//! * Python's `round(x)` (banker's) for the tail size.
//!
//! Pure: floats in, floats out.

/// `REGIME_SAMPLE_BARS`.
pub(crate) const SAMPLE_BARS: usize = 63;
/// `REGIME_TAIL_FRACTION`.
const TAIL_FRACTION: f64 = 0.20;
/// `REGIME_MARGIN`.
const MARGIN: f64 = 0.10;
/// `MIN_REGIME_WINDOWS`.
const MIN_WINDOWS: usize = 10;
/// numpy's `PW_BLOCKSIZE`.
const PW_BLOCKSIZE: usize = 128;
/// numpy's `SMALL_QUICKSORT`.
const SMALL_QUICKSORT: usize = 15;

/// numpy's `DOUBLE_pairwise_sum`.
pub(crate) fn pairwise_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0;
        for x in a {
            res += x;
        }
        res
    } else if n <= PW_BLOCKSIZE {
        let mut r = [0.0_f64; 8];
        r.copy_from_slice(&a[..8]);
        let mut i = 8;
        while i < n - (n % 8) {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
    }
}

/// `np.add.reduce(a)`: the identity, then the pairwise sum.
pub(crate) fn np_sum(a: &[f64]) -> f64 {
    0.0 + pairwise_sum(a)
}

/// numpy's `Tag::less` for doubles: NaNs sort last.
fn less(a: f64, b: f64) -> bool {
    a < b || (b.is_nan() && !a.is_nan())
}

/// numpy's `aheapsort_` over `tosort[lo..=hi]`.
fn aheapsort(v: &[f64], tosort: &mut [usize]) {
    let n = tosort.len();
    if n < 2 {
        return;
    }
    // One-based indexing: a[k] is tosort[k - 1].
    let get = |t: &[usize], k: usize| t[k - 1];
    let mut l = n >> 1;
    while l > 0 {
        let tmp = get(tosort, l);
        let (mut i, mut j) = (l, l << 1);
        while j <= n {
            if j < n && less(v[get(tosort, j)], v[get(tosort, j + 1)]) {
                j += 1;
            }
            if less(v[tmp], v[get(tosort, j)]) {
                tosort[i - 1] = get(tosort, j);
                i = j;
                j += j;
            } else {
                break;
            }
        }
        tosort[i - 1] = tmp;
        l -= 1;
    }
    let mut m = n;
    while m > 1 {
        let tmp = get(tosort, m);
        tosort[m - 1] = get(tosort, 1);
        m -= 1;
        let (mut i, mut j) = (1, 2);
        while j <= m {
            if j < m && less(v[get(tosort, j)], v[get(tosort, j + 1)]) {
                j += 1;
            }
            if less(v[tmp], v[get(tosort, j)]) {
                tosort[i - 1] = get(tosort, j);
                i = j;
                j += j;
            } else {
                break;
            }
        }
        tosort[i - 1] = tmp;
    }
}

/// `np.argsort(v)` (kind `quicksort`): numpy's `aquicksort_`, transcribed.
pub(crate) fn np_argsort(v: &[f64]) -> Vec<usize> {
    let num = v.len();
    let mut tosort: Vec<usize> = (0..num).collect();
    if num < 2 {
        return tosort;
    }
    let msb = usize::BITS - 1 - num.leading_zeros();
    let mut cdepth = i64::from(msb) * 2;
    let mut stack: Vec<(usize, usize, i64)> = Vec::new();
    let (mut pl, mut pr) = (0_usize, num - 1);
    loop {
        let mut heap = false;
        if cdepth < 0 {
            aheapsort(v, &mut tosort[pl..=pr]);
            heap = true;
        }
        if !heap {
            while pr - pl > SMALL_QUICKSORT {
                let pm = pl + ((pr - pl) >> 1);
                if less(v[tosort[pm]], v[tosort[pl]]) {
                    tosort.swap(pm, pl);
                }
                if less(v[tosort[pr]], v[tosort[pm]]) {
                    tosort.swap(pr, pm);
                }
                if less(v[tosort[pm]], v[tosort[pl]]) {
                    tosort.swap(pm, pl);
                }
                let vp = v[tosort[pm]];
                let mut pi = pl;
                let mut pj = pr - 1;
                tosort.swap(pm, pj);
                loop {
                    pi += 1;
                    while less(v[tosort[pi]], vp) {
                        pi += 1;
                    }
                    pj -= 1;
                    while less(vp, v[tosort[pj]]) {
                        pj -= 1;
                    }
                    if pi >= pj {
                        break;
                    }
                    tosort.swap(pi, pj);
                }
                let pk = pr - 1;
                tosort.swap(pi, pk);
                cdepth -= 1;
                if pi - pl < pr - pi {
                    stack.push((pi + 1, pr, cdepth));
                    pr = pi - 1;
                } else {
                    stack.push((pl, pi - 1, cdepth));
                    pl = pi + 1;
                }
            }
            // Insertion sort.
            let mut pi = pl + 1;
            while pi <= pr {
                let vi = tosort[pi];
                let vp = v[vi];
                let mut pj = pi;
                while pj > pl && less(vp, v[tosort[pj - 1]]) {
                    tosort[pj] = tosort[pj - 1];
                    pj -= 1;
                }
                tosort[pj] = vi;
                pi += 1;
            }
        }
        match stack.pop() {
            Some((l, r, d)) => {
                pl = l;
                pr = r;
                cdepth = d;
            }
            None => break,
        }
    }
    tosort
}

/// `np.linspace(0.0, 1.0, num=num)`.
pub(crate) fn linspace01(num: usize) -> Vec<f64> {
    if num == 0 {
        return Vec::new();
    }
    if num == 1 {
        return vec![0.0];
    }
    #[allow(clippy::cast_precision_loss)]
    let step = 1.0 / ((num - 1) as f64);
    #[allow(clippy::cast_precision_loss)]
    let mut y: Vec<f64> = (0..num).map(|i| (i as f64) * step + 0.0).collect();
    if let Some(last) = y.last_mut() {
        *last = 1.0;
    }
    y
}

/// `np.quantile(a, qs)` (method `linear`) over an already sorted sample.
fn quantiles_sorted(sorted: &[f64], qs: &[f64]) -> Vec<f64> {
    let n = sorted.len();
    let last = sorted.len().saturating_sub(1);
    #[allow(clippy::cast_precision_loss)]
    let n_minus_1 = (n as f64) - 1.0;
    qs.iter()
        .map(|&q| {
            let virtual_index = n_minus_1 * q;
            let (prev, next, prev_f) = if virtual_index >= n_minus_1 {
                (last, last, -1.0)
            } else if virtual_index < 0.0 {
                (0, 0, 0.0)
            } else {
                let floor = virtual_index.floor();
                #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
                let p = floor as usize;
                (p, (p + 1).min(last), floor)
            };
            let gamma = virtual_index - prev_f;
            let a = sorted[prev];
            let b = sorted[next];
            let diff = b - a;
            if gamma >= 0.5 {
                b - diff * (1.0 - gamma)
            } else {
                a + diff * gamma
            }
        })
        .collect()
}

fn sorted_copy(x: &[f64]) -> Vec<f64> {
    let mut s = x.to_vec();
    s.sort_by(f64::total_cmp);
    s
}

/// `wasserstein_1d(left, right)`.
pub(crate) fn wasserstein_1d(left: &[f64], right: &[f64]) -> f64 {
    if left.is_empty() || right.is_empty() {
        return f64::NAN;
    }
    let grid = linspace01(left.len().max(right.len()));
    let ql = quantiles_sorted(&sorted_copy(left), &grid);
    let qr = quantiles_sorted(&sorted_copy(right), &grid);
    let diffs: Vec<f64> = ql.iter().zip(&qr).map(|(a, b)| (a - b).abs()).collect();
    #[allow(clippy::cast_precision_loss)]
    let count = diffs.len() as f64;
    np_sum(&diffs) / count
}

/// Python's `round(x)` to an integer: half to even.
fn py_round_int(x: f64) -> i64 {
    #[allow(clippy::cast_possible_truncation)]
    let r = x.round_ties_even() as i64;
    r
}

/// `classify_series(returns, RegimeConfig())`: `(label, to_bull, to_bear)`, NaN distances when
/// there is too little history.
pub(crate) fn classify_series(returns: &[f64]) -> (&'static str, f64, f64) {
    let clean: Vec<f64> = returns.iter().copied().filter(|x| !x.is_nan()).collect();
    let n = SAMPLE_BARS;
    if clean.len() < n {
        return ("NEUTRAL", f64::NAN, f64::NAN);
    }
    let current = &clean[clean.len() - n..];
    let starts = clean.len() - n + 1;
    if starts < MIN_WINDOWS {
        return ("NEUTRAL", f64::NAN, f64::NAN);
    }
    let windows: Vec<&[f64]> = (0..starts).map(|s| &clean[s..s + n]).collect();
    let performance: Vec<f64> = windows.iter().map(|w| np_sum(w)).collect();
    let order = np_argsort(&performance);
    #[allow(clippy::cast_precision_loss)]
    let tail_f = (starts as f64) * TAIL_FRACTION;
    let tail = usize::try_from(py_round_int(tail_f).max(1))
        .unwrap_or(1)
        .min(starts);
    let bear: Vec<f64> = order[..tail]
        .iter()
        .flat_map(|&i| windows[i].iter().copied())
        .collect();
    let bull: Vec<f64> = order[order.len() - tail..]
        .iter()
        .flat_map(|&i| windows[i].iter().copied())
        .collect();
    let to_bull = wasserstein_1d(current, &bull);
    let to_bear = wasserstein_1d(current, &bear);
    if !to_bull.is_finite() || !to_bear.is_finite() {
        return ("NEUTRAL", to_bull, to_bear);
    }
    let threshold = 1.0 - MARGIN;
    if to_bull < to_bear * threshold {
        return ("BULL", to_bull, to_bear);
    }
    if to_bear < to_bull * threshold {
        return ("BEAR", to_bull, to_bear);
    }
    ("NEUTRAL", to_bull, to_bear)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn argsort_sorts_and_keeps_numpys_tie_order_shape() {
        let v: Vec<f64> = (0..200).map(|i| f64::from((i * 37) % 23)).collect();
        let order = np_argsort(&v);
        for w in order.windows(2) {
            assert!(v[w[0]] <= v[w[1]]);
        }
        let mut seen = order.clone();
        seen.sort_unstable();
        assert_eq!(seen, (0..200).collect::<Vec<_>>());
    }

    #[test]
    fn pairwise_matches_a_plain_sum_on_exact_values() {
        let v: Vec<f64> = (0..1000).map(f64::from).collect();
        assert_eq!(np_sum(&v).to_bits(), 499_500.0_f64.to_bits());
    }

    #[test]
    fn linspace_pins_the_endpoint() {
        let g = linspace01(4);
        assert_eq!(g.len(), 4);
        assert_eq!(g[3].to_bits(), 1.0_f64.to_bits());
        assert_eq!(g[0].to_bits(), 0.0_f64.to_bits());
    }

    /// Values printed by numpy 2.5 / `instrument_regime.classify_series` in the decile venv.
    #[test]
    fn classify_matches_numpy_bit_for_bit() {
        let r: Vec<f64> = (0..399)
            .map(|i| (f64::from(i) * 0.37).sin() * 0.02 + f64::from(i % 7 - 3) * 0.001)
            .collect();
        let (label, bull, bear) = classify_series(&r);
        assert_eq!(label, "BULL");
        assert_eq!(bull.to_bits(), 0.000_612_291_282_087_051_1_f64.to_bits());
        assert_eq!(bear.to_bits(), 0.002_310_198_308_617_413_6_f64.to_bits());
        let mut r2 = vec![0.0; 120];
        r2.extend(std::iter::repeat_n(0.01, 60));
        r2.extend((0..150).map(|i| f64::from(i).cos() * 0.01));
        let (label, bull, bear) = classify_series(&r2);
        assert_eq!(label, "BEAR");
        assert_eq!(bull.to_bits(), 0.007_655_320_548_216_594_f64.to_bits());
        assert_eq!(bear.to_bits(), 0.001_100_895_433_518_540_2_f64.to_bits());
    }

    /// `np.argsort` over many ties: numpy's introsort order, not a stable sort's.
    #[test]
    fn argsort_ties_follow_numpy() {
        let v: Vec<f64> = (0..200)
            .map(|i| (f64::from((i * 37) % 23) / 3.0).floor())
            .collect();
        let order = np_argsort(&v);
        assert_eq!(
            order[..40],
            [
                0, 51, 56, 69, 74, 79, 33, 92, 97, 102, 28, 115, 23, 125, 138, 143, 120, 46, 194,
                189, 161, 184, 5, 10, 166, 171, 148, 61, 66, 186, 71, 84, 89, 140, 176, 107, 112,
                163, 117, 48
            ]
        );
    }

    #[test]
    fn too_little_history_is_neutral_with_nan() {
        let (label, b, r) = classify_series(&[0.01; 40]);
        assert_eq!(label, "NEUTRAL");
        assert!(b.is_nan() && r.is_nan());
    }
}
