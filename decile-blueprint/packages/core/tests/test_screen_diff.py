"""``decile_core.screen_diff`` — the arithmetic behind Prompt 20 §3's alert email.

These assert the *specification* of a diff, not what the implementation happens to produce:
entries are names present now and not before, exits the reverse, and a rank change is reported in
places moved with a climb as positive. The hypothesis properties state the invariants that must
hold for any two runs — a name is in exactly one of the four buckets, and the counts add up.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from decile_core.screen_diff import (
    RankedRow,
    diff_runs,
    rows_from_results,
)

BEFORE = dt.date(2026, 8, 18)
AFTER = dt.date(2026, 8, 19)


def rows(*pairs: tuple[int, int]) -> tuple[RankedRow, ...]:
    """``rows((1, 10), (2, 20))`` — ``(rank, instrument_id)`` pairs, in that order."""
    return tuple(RankedRow(rank=rank, instrument_id=instrument) for rank, instrument in pairs)


class TestEntriesAndExits:
    def test_a_name_only_in_the_current_run_is_an_entry(self) -> None:
        result = diff_runs(
            rows((1, 10)), rows((1, 10), (2, 20)), previous_as_of=BEFORE, current_as_of=AFTER
        )
        assert [row.instrument_id for row in result.entries] == [20]
        assert result.exits == ()

    def test_a_name_only_in_the_previous_run_is_an_exit(self) -> None:
        result = diff_runs(
            rows((1, 10), (2, 20)), rows((1, 10)), previous_as_of=BEFORE, current_as_of=AFTER
        )
        assert [row.instrument_id for row in result.exits] == [20]
        assert result.entries == ()

    def test_entries_are_ordered_by_current_rank(self) -> None:
        result = diff_runs(
            rows((1, 10)),
            rows((1, 10), (2, 30), (3, 20)),
            previous_as_of=BEFORE,
            current_as_of=AFTER,
        )
        assert [row.rank for row in result.entries] == [2, 3]

    def test_exits_are_ordered_by_the_rank_they_held(self) -> None:
        result = diff_runs(
            rows((1, 10), (2, 30), (3, 20)),
            rows((1, 10)),
            previous_as_of=BEFORE,
            current_as_of=AFTER,
        )
        assert [row.rank for row in result.exits] == [2, 3]


class TestRankChanges:
    def test_a_climb_is_positive(self) -> None:
        result = diff_runs(
            rows((16, 10)), rows((4, 10)), previous_as_of=BEFORE, current_as_of=AFTER
        )
        (change,) = result.rank_changes
        assert change.previous_rank == 16
        assert change.current_rank == 4
        assert change.places_moved == 12
        assert change.improved

    def test_a_fall_is_negative(self) -> None:
        result = diff_runs(
            rows((4, 10)), rows((16, 10)), previous_as_of=BEFORE, current_as_of=AFTER
        )
        (change,) = result.rank_changes
        assert change.places_moved == -12
        assert not change.improved

    def test_an_unchanged_rank_is_held_not_reported(self) -> None:
        result = diff_runs(rows((1, 10)), rows((1, 10)), previous_as_of=BEFORE, current_as_of=AFTER)
        assert result.rank_changes == ()
        assert result.held_count == 1
        assert result.is_empty

    def test_movers_are_ordered_by_the_size_of_the_move(self) -> None:
        result = diff_runs(
            rows((10, 1), (20, 2), (30, 3)),
            rows((9, 1), (5, 2), (28, 3)),
            previous_as_of=BEFORE,
            current_as_of=AFTER,
        )
        # 2 climbed 15 places, 3 climbed 2, 1 climbed 1 — biggest move first.
        assert [change.instrument_id for change in result.rank_changes] == [2, 3, 1]

    def test_min_move_filters_movers_but_never_entries_or_exits(self) -> None:
        result = diff_runs(
            rows((10, 1), (20, 2)),
            rows((11, 1), (99, 3)),
            previous_as_of=BEFORE,
            current_as_of=AFTER,
            min_move=5,
        )
        assert result.rank_changes == ()
        assert [row.instrument_id for row in result.entries] == [3]
        assert [row.instrument_id for row in result.exits] == [2]

    def test_min_move_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="min_move"):
            diff_runs(rows(), rows(), previous_as_of=BEFORE, current_as_of=AFTER, min_move=0)


class TestTopN:
    """``top_n`` is what makes "entered the top 20" a sentence with meaning."""

    def test_a_name_that_climbs_into_the_window_is_an_entry(self) -> None:
        result = diff_runs(
            rows((1, 10), (25, 20)),
            rows((1, 10), (5, 20)),
            previous_as_of=BEFORE,
            current_as_of=AFTER,
            top_n=20,
        )
        assert [row.instrument_id for row in result.entries] == [20]

    def test_a_name_that_falls_out_of_the_window_is_an_exit(self) -> None:
        result = diff_runs(
            rows((1, 10), (5, 20)),
            rows((1, 10), (25, 20)),
            previous_as_of=BEFORE,
            current_as_of=AFTER,
            top_n=20,
        )
        assert [row.instrument_id for row in result.exits] == [20]

    def test_top_n_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="top_n"):
            diff_runs(rows(), rows(), previous_as_of=BEFORE, current_as_of=AFTER, top_n=0)


class TestParsingStoredRows:
    """``screen_run.results`` is JSONB, and ``docs/07a`` §6 stores the factor value as a string."""

    def test_a_string_factor_value_survives_as_an_exact_decimal(self) -> None:
        (row,) = rows_from_results([{"rank": 1, "instrument_id": 7, "factor_value": "13.00"}])
        assert row.factor_value == Decimal("13.00")
        assert str(row.factor_value) == "13.00"

    def test_a_null_factor_value_is_kept_as_none(self) -> None:
        (row,) = rows_from_results([{"rank": 1, "instrument_id": 7, "factor_value": None}])
        assert row.factor_value is None

    def test_rows_without_a_rank_or_an_instrument_are_dropped_not_guessed(self) -> None:
        parsed = rows_from_results(
            [
                {"rank": 1, "instrument_id": 7},
                {"instrument_id": 8},
                {"rank": 2},
                {"rank": True, "instrument_id": 9},
            ]
        )
        assert [row.instrument_id for row in parsed] == [7]

    def test_parsed_rows_come_back_in_rank_order(self) -> None:
        parsed = rows_from_results(
            [{"rank": 3, "instrument_id": 1}, {"rank": 1, "instrument_id": 2}]
        )
        assert [row.rank for row in parsed] == [1, 3]


_runs = st.lists(
    st.tuples(st.integers(min_value=1, max_value=50), st.integers(min_value=1, max_value=25)),
    max_size=25,
)


def _unique_by_instrument(pairs: list[tuple[int, int]]) -> tuple[RankedRow, ...]:
    seen: dict[int, int] = {}
    for rank, instrument in pairs:
        seen.setdefault(instrument, rank)
    # Ranks must be distinct for the shape to be a real screen result.
    ordered = sorted(seen.items(), key=lambda item: (item[1], item[0]))
    return tuple(
        RankedRow(rank=index + 1, instrument_id=instrument)
        for index, (instrument, _) in enumerate(ordered)
    )


class TestProperties:
    @given(previous=_runs, current=_runs)
    def test_every_name_lands_in_exactly_one_bucket(
        self, previous: list[tuple[int, int]], current: list[tuple[int, int]]
    ) -> None:
        before = _unique_by_instrument(previous)
        after = _unique_by_instrument(current)
        result = diff_runs(before, after, previous_as_of=BEFORE, current_as_of=AFTER)

        entered = {row.instrument_id for row in result.entries}
        exited = {row.instrument_id for row in result.exits}
        moved = {change.instrument_id for change in result.rank_changes}
        assert entered & exited == set()
        assert entered & moved == set()
        assert exited & moved == set()
        assert entered == {row.instrument_id for row in after} - {
            row.instrument_id for row in before
        }
        assert exited == {row.instrument_id for row in before} - {
            row.instrument_id for row in after
        }

    @given(previous=_runs, current=_runs)
    def test_the_counts_add_up(
        self, previous: list[tuple[int, int]], current: list[tuple[int, int]]
    ) -> None:
        before = _unique_by_instrument(previous)
        after = _unique_by_instrument(current)
        result = diff_runs(before, after, previous_as_of=BEFORE, current_as_of=AFTER)
        assert result.entry_count + len(result.rank_changes) + result.held_count == len(after)
        assert result.exit_count + len(result.rank_changes) + result.held_count == len(before)

    @given(run=_runs)
    def test_a_run_diffed_against_itself_reports_nothing(self, run: list[tuple[int, int]]) -> None:
        rows_ = _unique_by_instrument(run)
        result = diff_runs(rows_, rows_, previous_as_of=BEFORE, current_as_of=AFTER)
        assert result.is_empty
        assert result.held_count == len(rows_)

    @given(previous=_runs, current=_runs)
    def test_the_diff_is_deterministic(
        self, previous: list[tuple[int, int]], current: list[tuple[int, int]]
    ) -> None:
        """Two calls on the same input produce the same ordering — the email depends on it."""
        before = _unique_by_instrument(previous)
        after = _unique_by_instrument(current)
        first = diff_runs(before, after, previous_as_of=BEFORE, current_as_of=AFTER)
        second = diff_runs(before, after, previous_as_of=BEFORE, current_as_of=AFTER)
        assert first == second
