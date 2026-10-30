//! `baskfy_core.screen_diff` — what changed between two runs of one screen (Prompt 20 §3).
//!
//! Pure arithmetic over two `screen_run.results` payloads (`[{rank, instrument_id,
//! factor_value}]`): entries, exits, rank changes. Kept private to the screener lane rather than
//! added to `baskfy-core` because its only caller is the alert dispatch; it touches no database,
//! clock or network either way (law 1 holds wherever it lives).
//!
//! **One approximation, unreachable from `record_run`'s writes:** Python sorts `RankedRow`s as
//! `(rank, instrument_id, factor_value)` tuples, so two rows with the same rank *and* the same
//! instrument compare their `Decimal | None` factor values — a `TypeError` when one is `None`, an
//! `InvalidOperation` for a NaN. `record_run` never stores one instrument twice, so the port
//! orders ties by `None` first and then by value, and raises Python's `TypeError` only for the
//! `None`-against-a-value tie (lane label R-scr.3).

use std::cmp::Ordering;

use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use rust_decimal::Decimal;

use crate::error::WorkerError;

/// `MIN_REPORTABLE_MOVE`.
pub(super) const MIN_REPORTABLE_MOVE: i64 = 1;

/// `RankedRow`: one row of a stored run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct RankedRow {
    pub(super) rank: i64,
    pub(super) instrument_id: i64,
    pub(super) factor_value: Option<Decimal>,
}

/// `RankChange`: a name in both runs that moved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct RankChange {
    pub(super) instrument_id: i64,
    pub(super) previous_rank: i64,
    pub(super) current_rank: i64,
}

impl RankChange {
    /// `places_moved`: positive when the name climbed.
    pub(super) const fn places_moved(&self) -> i64 {
        self.previous_rank - self.current_rank
    }
}

/// `ScreenDiff`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ScreenDiff {
    pub(super) previous_as_of: NaiveDate,
    pub(super) current_as_of: NaiveDate,
    pub(super) entries: Vec<RankedRow>,
    pub(super) exits: Vec<RankedRow>,
    pub(super) rank_changes: Vec<RankChange>,
    pub(super) held_count: i64,
}

impl ScreenDiff {
    /// `is_empty`.
    pub(super) fn is_empty(&self) -> bool {
        self.entries.is_empty() && self.exits.is_empty() && self.rank_changes.is_empty()
    }

    /// `entry_count`.
    pub(super) fn entry_count(&self) -> i64 {
        len_i64(self.entries.len())
    }

    /// `exit_count`.
    pub(super) fn exit_count(&self) -> i64 {
        len_i64(self.exits.len())
    }

    /// `len(rank_changes)`.
    pub(super) fn change_count(&self) -> i64 {
        len_i64(self.rank_changes.len())
    }

    /// `instrument_ids`: every id the diff mentions, sorted, distinct.
    pub(super) fn instrument_ids(&self) -> Vec<i64> {
        let mut ids: Vec<i64> = self
            .entries
            .iter()
            .chain(self.exits.iter())
            .map(|r| r.instrument_id)
            .chain(self.rank_changes.iter().map(|c| c.instrument_id))
            .collect();
        ids.sort_unstable();
        ids.dedup();
        ids
    }
}

pub(super) fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `_decimal(value)`: the stored sorting value (a string, normally), or `None`.
fn decimal_of(value: Option<&OrderedJson>) -> Option<Decimal> {
    match value? {
        OrderedJson::Int(i) => Some(Decimal::from(*i)),
        OrderedJson::UInt(u) => Some(Decimal::from(*u)),
        OrderedJson::Str(s) => {
            let t = s.trim();
            t.parse::<Decimal>()
                .ok()
                .or_else(|| Decimal::from_scientific(t).ok())
        }
        _ => None,
    }
}

/// An integer member that is a Python `int` (not a `bool`), as `i64`.
fn int_member(entry: &OrderedJson, key: &str) -> Option<i64> {
    match entry.get(key)? {
        OrderedJson::Int(i) => Some(*i),
        _ => None,
    }
}

/// Python's tuple order on `(rank, instrument_id, factor_value)`.
fn compare(a: &RankedRow, b: &RankedRow, tie_error: &mut bool) -> Ordering {
    a.rank
        .cmp(&b.rank)
        .then(a.instrument_id.cmp(&b.instrument_id))
        .then_with(|| match (&a.factor_value, &b.factor_value) {
            (None, None) => Ordering::Equal,
            (Some(x), Some(y)) => x.cmp(y),
            (None, Some(_)) => {
                *tie_error = true;
                Ordering::Less
            }
            (Some(_), None) => {
                *tie_error = true;
                Ordering::Greater
            }
        })
}

/// `sorted(rows)`.
fn sorted(rows: &[RankedRow]) -> Result<Vec<RankedRow>, WorkerError> {
    let mut out = rows.to_vec();
    let mut tie_error = false;
    out.sort_by(|a, b| compare(a, b, &mut tie_error));
    if tie_error {
        return Err(WorkerError::python(
            "TypeError",
            "'<' not supported between instances of 'decimal.Decimal' and 'NoneType'",
        ));
    }
    Ok(out)
}

/// `rows_from_results(results)`: rows with an integer `rank` and `instrument_id`, in order.
pub(super) fn rows_from_results(results: &OrderedJson) -> Result<Vec<RankedRow>, WorkerError> {
    let OrderedJson::Array(items) = results else {
        return Ok(Vec::new());
    };
    let mut parsed = Vec::with_capacity(items.len());
    for entry in items {
        if !matches!(entry, OrderedJson::Object(_)) {
            continue;
        }
        let (Some(rank), Some(instrument_id)) = (
            int_member(entry, "rank"),
            int_member(entry, "instrument_id"),
        ) else {
            continue;
        };
        parsed.push(RankedRow {
            rank,
            instrument_id,
            factor_value: decimal_of(entry.get("factor_value")),
        });
    }
    sorted(&parsed)
}

/// `_clip(rows, top_n)`.
fn clip(rows: &[RankedRow], top_n: Option<i64>) -> Result<Vec<RankedRow>, WorkerError> {
    let ordered = sorted(rows)?;
    Ok(match top_n {
        None => ordered,
        Some(n) => ordered.into_iter().filter(|r| r.rank <= n).collect(),
    })
}

/// `_by_instrument(rows)`: the best (lowest) rank wins.
fn by_instrument(rows: &[RankedRow]) -> Vec<(i64, i64)> {
    let mut best: Vec<(i64, i64)> = Vec::new();
    for row in rows {
        match best.iter_mut().find(|(id, _)| *id == row.instrument_id) {
            Some(slot) => {
                if row.rank < slot.1 {
                    slot.1 = row.rank;
                }
            }
            None => best.push((row.instrument_id, row.rank)),
        }
    }
    best.sort_unstable();
    best
}

fn rank_of(index: &[(i64, i64)], id: i64) -> Option<i64> {
    index
        .binary_search_by_key(&id, |(k, _)| *k)
        .ok()
        .and_then(|i| index.get(i))
        .map(|(_, r)| *r)
}

/// `diff_runs(previous, current, previous_as_of=, current_as_of=, min_move=, top_n=)`.
pub(super) fn diff_runs(
    previous: &[RankedRow],
    current: &[RankedRow],
    previous_as_of: NaiveDate,
    current_as_of: NaiveDate,
    min_move: i64,
    top_n: Option<i64>,
) -> Result<ScreenDiff, WorkerError> {
    if min_move < 1 {
        return Err(WorkerError::python(
            "ValueError",
            "min_move must be at least 1; a move of zero is not a move",
        ));
    }
    if top_n.is_some_and(|n| n < 1) {
        return Err(WorkerError::python(
            "ValueError",
            "top_n must be at least 1",
        ));
    }
    let before = clip(previous, top_n)?;
    let after = clip(current, top_n)?;
    let before_by_id = by_instrument(&before);
    let after_by_id = by_instrument(&after);

    let entries: Vec<RankedRow> = after
        .iter()
        .filter(|r| rank_of(&before_by_id, r.instrument_id).is_none())
        .cloned()
        .collect();
    let exits: Vec<RankedRow> = before
        .iter()
        .filter(|r| rank_of(&after_by_id, r.instrument_id).is_none())
        .cloned()
        .collect();

    let mut changes: Vec<RankChange> = Vec::new();
    let mut held = 0_i64;
    for row in &after {
        let Some(was) = rank_of(&before_by_id, row.instrument_id) else {
            continue;
        };
        if was == row.rank {
            held += 1;
            continue;
        }
        let change = RankChange {
            instrument_id: row.instrument_id,
            previous_rank: was,
            current_rank: row.rank,
        };
        if change.places_moved().abs() >= min_move {
            changes.push(change);
        } else {
            held += 1;
        }
    }
    // A stable sort, as Python's `list.sort`.
    changes.sort_by(|a, b| {
        b.places_moved()
            .abs()
            .cmp(&a.places_moved().abs())
            .then(a.current_rank.cmp(&b.current_rank))
    });
    Ok(ScreenDiff {
        previous_as_of,
        current_as_of,
        entries,
        exits,
        rank_changes: changes,
        held_count: held,
    })
}

#[cfg(test)]
mod tests {
    //! Ports `packages/core/tests/test_screen_diff.py`'s spec assertions.
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 10, day).unwrap()
    }

    fn row(rank: i64, id: i64) -> RankedRow {
        RankedRow {
            rank,
            instrument_id: id,
            factor_value: None,
        }
    }

    fn json(text: &str) -> OrderedJson {
        OrderedJson::parse(text).unwrap()
    }

    #[test]
    fn entries_exits_and_moves_are_reported() {
        let before = [row(1, 10), row(2, 20), row(3, 30)];
        let after = [row(1, 20), row(2, 10), row(3, 40)];
        let diff = diff_runs(&before, &after, d(28), d(29), 1, None).unwrap();
        assert_eq!(diff.entries, vec![row(3, 40)]);
        assert_eq!(diff.exits, vec![row(3, 30)]);
        assert_eq!(diff.rank_changes.len(), 2);
        // Ties on the size of the move break on the current rank.
        assert_eq!(diff.rank_changes[0].instrument_id, 20);
        assert_eq!(diff.rank_changes[0].places_moved(), 1);
        assert_eq!(diff.rank_changes[1].places_moved(), -1);
        assert_eq!(diff.held_count, 0);
        assert_eq!(diff.instrument_ids(), vec![10, 20, 30, 40]);
        assert!(!diff.is_empty());
    }

    #[test]
    fn an_identical_run_is_empty_and_counts_every_name_as_held() {
        let rows = [row(1, 1), row(2, 2)];
        let diff = diff_runs(&rows, &rows, d(28), d(29), 1, None).unwrap();
        assert!(diff.is_empty());
        assert_eq!(diff.held_count, 2);
    }

    #[test]
    fn min_move_filters_changes_only_never_entries_or_exits() {
        let before = [row(1, 1), row(2, 2), row(3, 3)];
        let after = [row(1, 2), row(2, 1), row(3, 4)];
        let diff = diff_runs(&before, &after, d(28), d(29), 5, None).unwrap();
        assert!(diff.rank_changes.is_empty());
        assert_eq!(diff.held_count, 2);
        assert_eq!(diff.entry_count(), 1);
        assert_eq!(diff.exit_count(), 1);
    }

    #[test]
    fn top_n_clips_both_sides_before_diffing() {
        let before = [row(1, 1), row(2, 2), row(900, 3)];
        let after = [row(1, 1), row(2, 3), row(3, 2)];
        let diff = diff_runs(&before, &after, d(28), d(29), 1, Some(2)).unwrap();
        assert_eq!(diff.entries, vec![row(2, 3)]);
        assert_eq!(diff.exits, vec![row(2, 2)]);
    }

    #[test]
    fn bad_filters_raise_value_error_min_move_first() {
        let e = diff_runs(&[], &[], d(28), d(29), 0, Some(0)).unwrap_err();
        assert_eq!(e.python_type(), "ValueError");
        assert!(e.to_string().starts_with("min_move"));
        let e = diff_runs(&[], &[], d(28), d(29), 1, Some(0)).unwrap_err();
        assert_eq!(e.to_string(), "top_n must be at least 1");
    }

    #[test]
    fn the_best_rank_wins_for_an_instrument_listed_twice() {
        let before = [row(5, 1), row(2, 1)];
        let after = [row(2, 1)];
        let diff = diff_runs(&before, &after, d(28), d(29), 1, None).unwrap();
        assert!(diff.rank_changes.is_empty());
        assert!(diff.exits.is_empty());
    }

    #[test]
    fn rows_without_integer_rank_or_id_are_dropped_and_values_parsed() {
        let rows = rows_from_results(&json(
            r#"[{"rank": 2, "instrument_id": 7, "factor_value": "13.50"},
                {"rank": true, "instrument_id": 8},
                {"rank": 1.0, "instrument_id": 9},
                {"rank": 1, "instrument_id": "10"},
                {"rank": 1, "instrument_id": 11, "factor_value": 3.5},
                {"rank": 3, "instrument_id": 12, "factor_value": "bad"},
                {"rank": 4, "instrument_id": 13, "factor_value": 4}]"#,
        ))
        .unwrap();
        let ids: Vec<i64> = rows.iter().map(|r| r.instrument_id).collect();
        assert_eq!(ids, vec![11, 7, 12, 13]);
        assert_eq!(rows[0].factor_value, None);
        assert_eq!(rows[1].factor_value, Some(Decimal::new(1350, 2)));
        assert_eq!(rows[2].factor_value, None);
        assert_eq!(rows[3].factor_value, Some(Decimal::from(4)));
    }

    /// `test_every_name_lands_in_exactly_one_bucket` / `test_the_counts_add_up` /
    /// `test_a_run_diffed_against_itself_reports_nothing`, over seeded random runs.
    #[test]
    fn every_name_lands_in_exactly_one_bucket_and_the_counts_add_up() {
        let mut seed: u64 = 0x5eed;
        let mut next = move |m: u64| {
            seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
            i64::try_from((seed >> 33) % m).unwrap()
        };
        let run = |ids: Vec<i64>| {
            let mut ids = ids;
            ids.sort_unstable();
            ids.dedup();
            ids.iter()
                .enumerate()
                .map(|(i, id)| row(i64::try_from(i).unwrap() + 1, *id))
                .collect::<Vec<_>>()
        };
        for _ in 0..200 {
            let (n1, n2) = (next(30), next(30));
            let before = run((0..n1).map(|_| next(40)).collect());
            let after = run((0..n2).map(|_| next(40)).collect());
            let diff = diff_runs(&before, &after, d(28), d(29), 1, None).unwrap();
            let changed = len_i64(diff.rank_changes.len());
            assert_eq!(
                diff.entry_count() + changed + diff.held_count,
                len_i64(after.len())
            );
            assert_eq!(
                diff.exit_count() + changed + diff.held_count,
                len_i64(before.len())
            );
            for r in &after {
                let buckets = usize::from(diff.entries.contains(r))
                    + usize::from(
                        diff.rank_changes
                            .iter()
                            .any(|c| c.instrument_id == r.instrument_id),
                    )
                    + usize::from(
                        before
                            .iter()
                            .any(|b| b.instrument_id == r.instrument_id && b.rank == r.rank),
                    );
                assert_eq!(buckets, 1);
            }
            assert!(
                diff_runs(&after, &after, d(29), d(29), 1, None)
                    .unwrap()
                    .is_empty()
            );
            assert_eq!(
                diff,
                diff_runs(&before, &after, d(28), d(29), 1, None).unwrap()
            );
        }
    }
}
