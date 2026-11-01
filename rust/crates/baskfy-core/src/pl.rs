//! polars' float semantics, written once for every lane (DECISIONS-RS R3.1).
//!
//! Python's `baskfy_core` computes in polars, and polars does **not** compare floats the IEEE
//! way. Each rule below was read off the live Python (polars 1.43.2, the tree's venv) and is
//! pinned by this module's tests and by `tests/pl_semantics.rs`, which builds NaN, `±inf`,
//! `±0.0` and null inputs in-process (the golden encoder cannot express a NaN — it writes
//! `null`) and compares them with answers the live Python printed:
//!
//! * **Comparisons** (`>`, `>=`, `<`, `<=`, `==` between `Float64` values) use a **total
//!   order**: every NaN equals every other NaN and sits above every number, `+inf` included —
//!   `NaN > 1.0` and `NaN == NaN` are true, `1.0 > NaN` is false. `-0.0 == 0.0`. A null on either
//!   side gives null (Kleene logic); a filter then reads null as false. [`tot_cmp`], [`cmp`].
//! * **`pl.max_horizontal` / `pl.min_horizontal`** are *not* one rule; they reduce the columns
//!   pairwise, and each pair runs one of two kernels chosen by the **columns**, not the row:
//!   - when either column of the pair holds a null anywhere, or the frame is one row high (or a
//!     side is a broadcast literal), the **total-order kernel**: nulls skipped, NaN the largest,
//!     a tie taken by the right-hand side (`max(0.0, -0.0)` is `-0.0`, `max(-0.0, 0.0)` is
//!     `0.0`) — so a NaN wins a maximum and loses a minimum;
//!   - otherwise the **IEEE kernel**: `if l > r { l } else { r }` (`<` for the minimum), the x86
//!     `maxsd` shape — a NaN on the right wins, a NaN on the left loses, so argument order
//!     matters (`max_horizontal(NaN, 101, 102)` is `102`, `max_horizontal(101, NaN)` is NaN).
//!
//!   The pairs form polars' parallel reduction tree (rayon's `try_reduce_with` over the columns,
//!   split at `len / 2`): three columns are `a op (b op c)`, four `(a op b) op (c op d)`. That
//!   tree is fuzzed against the live Python by `tests/fuzz/pl_semantics_fuzz.py` (3,000 frames of
//!   one to four columns and one to four rows: NaN, `±0.0`, `inf`, null); the wave-3 review re-ran
//!   it over 8,000 more frames of one to six columns, up to 17 rows, `-inf` included, with no
//!   mismatch. Five or more columns fold a sub-slice left
//!   to right instead when rayon has fewer threads than splits; no caller passes more than four.
//!   [`max_horizontal`], [`min_horizontal`] take the columns; [`max_horizontal_total`] /
//!   [`min_horizontal_total`] are the total-order kernel alone, for a row against a literal.
//! * **Aggregations** (`col.min()` / `col.max()`, in a `group_by().agg()` or `.over()`, and a
//!   `Series.min()`) **skip NaN** unless every non-null value is NaN, then answer NaN; nulls are
//!   skipped, all null is null; `-0.0` is below `0.0` (the minimum of `0.0, -0.0` is `-0.0`, the
//!   maximum `0.0`). [`nan_min`], [`nan_max`].
//!
//! Pure functions over `f64` and `Option<f64>`; no frame is touched here.

use std::cmp::Ordering;

/// polars' total order on floats: NaN equals NaN and sorts above every number; otherwise IEEE
/// (so `-0.0` equals `0.0`).
#[must_use]
pub fn tot_cmp(a: f64, b: f64) -> Ordering {
    match (a.is_nan(), b.is_nan()) {
        (true, true) => Ordering::Equal,
        (true, false) => Ordering::Greater,
        (false, true) => Ordering::Less,
        (false, false) => a.partial_cmp(&b).unwrap_or(Ordering::Equal),
    }
}

/// A polars float comparison of two values: `accept` reads the [`tot_cmp`] ordering.
#[must_use]
pub fn holds(a: f64, b: f64, accept: fn(Ordering) -> bool) -> bool {
    accept(tot_cmp(a, b))
}

/// A polars float comparison under Kleene logic: null when either side is null, else
/// `accept(tot_cmp(a, b))`.
#[must_use]
pub fn cmp(a: Option<f64>, b: Option<f64>, accept: fn(Ordering) -> bool) -> Option<bool> {
    Some(accept(tot_cmp(a?, b?)))
}

/// `a > b` in polars (total order).
#[must_use]
pub fn gt(a: f64, b: f64) -> bool {
    tot_cmp(a, b) == Ordering::Greater
}

/// `a >= b` in polars (total order).
#[must_use]
pub fn ge(a: f64, b: f64) -> bool {
    tot_cmp(a, b) != Ordering::Less
}

/// `a < b` in polars (total order).
#[must_use]
pub fn lt(a: f64, b: f64) -> bool {
    tot_cmp(a, b) == Ordering::Less
}

/// `a <= b` in polars (total order).
#[must_use]
pub fn le(a: f64, b: f64) -> bool {
    tot_cmp(a, b) != Ordering::Greater
}

/// `a == b` in polars (total order: NaN equals NaN, `-0.0` equals `0.0`).
#[must_use]
pub fn eq(a: f64, b: f64) -> bool {
    tot_cmp(a, b) == Ordering::Equal
}

/// The total-order kernel of `pl.max_horizontal` over one row (nulls skipped, NaN the largest,
/// a tie taken by the later value); null when every value is null. What `max_horizontal(col,
/// literal)` computes, and what [`max_horizontal`] computes when a null is about.
#[must_use]
pub fn max_horizontal_total(values: impl IntoIterator<Item = Option<f64>>) -> Option<f64> {
    values
        .into_iter()
        .reduce(|acc, v| total_pair(acc, v, true))
        .flatten()
}

/// The total-order kernel of `pl.min_horizontal` over one row (nulls skipped, NaN the largest,
/// a tie taken by the later value); null when every value is null.
#[must_use]
pub fn min_horizontal_total(values: impl IntoIterator<Item = Option<f64>>) -> Option<f64> {
    values
        .into_iter()
        .reduce(|acc, v| total_pair(acc, v, false))
        .flatten()
}

/// One cell of the total-order kernel.
fn total_pair(l: Option<f64>, r: Option<f64>, max: bool) -> Option<f64> {
    match (l, r) {
        (None, x) | (x, None) => x,
        (Some(l), Some(r)) => {
            let keep_left = if max {
                tot_cmp(r, l) == Ordering::Less
            } else {
                tot_cmp(r, l) == Ordering::Greater
            };
            Some(if keep_left { l } else { r })
        }
    }
}

/// One cell of the IEEE kernel (both sides non-null by construction).
fn ieee_pair(l: f64, r: f64, max: bool) -> f64 {
    let keep_left = if max { l > r } else { l < r };
    if keep_left { l } else { r }
}

/// One pair of columns, with the kernel the pair's nulls (and the frame's height) choose.
fn pair_columns(l: &[Option<f64>], r: &[Option<f64>], max: bool) -> Vec<Option<f64>> {
    let nullable = l.len() == 1 || l.iter().any(Option::is_none) || r.iter().any(Option::is_none);
    l.iter()
        .zip(r)
        .map(|(a, b)| match (nullable, a, b) {
            (false, Some(a), Some(b)) => Some(ieee_pair(*a, *b, max)),
            _ => total_pair(*a, *b, max),
        })
        .collect()
}

/// polars' reduction tree over the columns: split at `len / 2`, reduce each half, combine.
fn reduce_columns(columns: &[&[Option<f64>]], max: bool) -> Vec<Option<f64>> {
    match columns {
        [] => Vec::new(),
        [only] => only.to_vec(),
        _ => {
            let (left, right) = columns.split_at(columns.len() / 2);
            pair_columns(&reduce_columns(left, max), &reduce_columns(right, max), max)
        }
    }
}

/// `pl.max_horizontal(columns...)` over whole, equally long columns, each pair reduced by the
/// kernel its nulls choose (module docs). Zero columns give an empty result.
#[must_use]
pub fn max_horizontal(columns: &[&[Option<f64>]]) -> Vec<Option<f64>> {
    reduce_columns(columns, true)
}

/// `pl.min_horizontal(columns...)` over whole, equally long columns, each pair reduced by the
/// kernel its nulls choose (module docs). Zero columns give an empty result.
#[must_use]
pub fn min_horizontal(columns: &[&[Option<f64>]]) -> Vec<Option<f64>> {
    reduce_columns(columns, false)
}

/// The aggregation both [`nan_min`] and [`nan_max`] share: nulls and NaN skipped, `pick` keeps
/// the extreme under IEEE `totalOrder` (so `-0.0 < 0.0`); NaN when only NaN was seen.
fn nan_skipping(
    values: impl IntoIterator<Item = Option<f64>>,
    keep_new: fn(Ordering) -> bool,
) -> Option<f64> {
    let mut best: Option<f64> = None;
    let mut saw_nan = false;
    for v in values.into_iter().flatten() {
        if v.is_nan() {
            saw_nan = true;
            continue;
        }
        best = Some(match best {
            Some(b) if !keep_new(v.total_cmp(&b)) => b,
            _ => v,
        });
    }
    match best {
        Some(b) => Some(b),
        None if saw_nan => Some(f64::NAN),
        None => None,
    }
}

/// polars' `col.min()` aggregation (`group_by().agg`, `.over()`, `Series.min()`): NaN and nulls
/// skipped, NaN only when every non-null value is NaN, null when all are null; `-0.0` is below
/// `0.0`.
#[must_use]
pub fn nan_min(values: impl IntoIterator<Item = Option<f64>>) -> Option<f64> {
    nan_skipping(values, |o| o == Ordering::Less)
}

/// polars' `col.max()` aggregation: NaN and nulls skipped, NaN only when every non-null value
/// is NaN, null when all are null; `0.0` is above `-0.0`.
#[must_use]
pub fn nan_max(values: impl IntoIterator<Item = Option<f64>>) -> Option<f64> {
    nan_skipping(values, |o| o == Ordering::Greater)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::float_cmp)]
    use super::*;

    const NAN: f64 = f64::NAN;

    fn bits(x: Option<f64>) -> Option<u64> {
        x.map(|v| {
            if v.is_nan() {
                f64::NAN.to_bits()
            } else {
                v.to_bits()
            }
        })
    }

    #[test]
    fn nan_is_the_largest_and_equals_itself() {
        assert!(gt(NAN, 1.0) && gt(NAN, f64::INFINITY) && !gt(1.0, NAN));
        assert!(eq(NAN, NAN) && ge(NAN, NAN) && !gt(NAN, NAN));
        assert!(eq(-0.0, 0.0) && le(-0.0, 0.0) && !lt(-0.0, 0.0));
        assert_eq!(cmp(None, Some(1.0), |o| o == Ordering::Greater), None);
        assert_eq!(
            cmp(Some(NAN), Some(1.0), |o| o == Ordering::Greater),
            Some(true)
        );
    }

    fn col(values: &[Option<f64>]) -> Vec<Option<u64>> {
        values.iter().map(|v| bits(*v)).collect()
    }

    #[test]
    fn horizontal_kernels_follow_the_columns_nulls() {
        // Answers printed by polars 1.43.2.
        let (nan, a, b) = (
            [Some(NAN), Some(5.0)],
            [Some(101.0), Some(1.0)],
            [Some(102.0), Some(2.0)],
        );
        // No nulls: the IEEE kernel, `nan op (101 op 102)`.
        assert_eq!(
            col(&max_horizontal(&[&nan, &a, &b])),
            col(&[Some(102.0), Some(5.0)])
        );
        assert_eq!(
            col(&min_horizontal(&[&nan, &a, &b])),
            col(&[Some(101.0), Some(1.0)])
        );
        assert_eq!(
            col(&max_horizontal(&[&a, &nan])),
            col(&[Some(NAN), Some(5.0)])
        );
        // One row: the total-order kernel.
        assert_eq!(
            col(&max_horizontal(&[&[Some(NAN)], &[Some(1.0)]])),
            col(&[Some(NAN)])
        );
        // A null in a column: the total-order kernel for that pair.
        let with_null = [Some(NAN), None];
        let one = [Some(1.0), Some(5.0)];
        let two = [Some(2.0), Some(1.0)];
        assert_eq!(
            col(&max_horizontal(&[&one, &with_null, &two])),
            col(&[Some(NAN), Some(5.0)])
        );
        assert_eq!(
            col(&min_horizontal(&[&one, &with_null, &two])),
            col(&[Some(1.0), Some(1.0)])
        );
        // Signed zeros: the IEEE kernel keeps the right on a tie, as does the total one.
        let (z, nz) = ([Some(0.0), Some(0.0)], [Some(-0.0), Some(-0.0)]);
        assert_eq!(col(&max_horizontal(&[&z, &nz])), col(&nz));
        assert_eq!(col(&min_horizontal(&[&nz, &z])), col(&z));
        assert!(max_horizontal(&[]).is_empty());
    }

    #[test]
    fn the_total_kernel_alone() {
        assert_eq!(
            bits(max_horizontal_total([Some(NAN), Some(1.0)])),
            bits(Some(NAN))
        );
        assert_eq!(
            bits(min_horizontal_total([Some(NAN), Some(1.0)])),
            bits(Some(1.0))
        );
        assert_eq!(
            bits(max_horizontal_total([Some(0.0), Some(-0.0)])),
            bits(Some(-0.0))
        );
        assert_eq!(max_horizontal_total([None, None]), None);
        assert_eq!(min_horizontal_total([None, Some(1.0)]), Some(1.0));
    }

    #[test]
    fn aggregations_skip_nan() {
        assert_eq!(nan_min([Some(NAN), Some(1.0), Some(2.0)]), Some(1.0));
        assert_eq!(nan_max([Some(NAN), Some(1.0), Some(2.0)]), Some(2.0));
        assert_eq!(bits(nan_min([Some(NAN), Some(NAN)])), bits(Some(NAN)));
        assert_eq!(
            bits(nan_min([Some(0.0), Some(-0.0), Some(0.0)])),
            bits(Some(-0.0))
        );
        assert_eq!(
            bits(nan_max([Some(-0.0), Some(0.0), Some(-0.0)])),
            bits(Some(0.0))
        );
        assert_eq!(nan_min([None, None]), None);
    }
}
