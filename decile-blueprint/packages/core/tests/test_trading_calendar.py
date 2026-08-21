"""The trading calendar (Prompt 1 deliverable 3).

These assert the calendar's *contract*, not the contents of the seed holiday list. The list is
deliberately incomplete — India's lunar-calendar holidays are declared per year by NSE circular
and are not in the bundle — so the tests that matter are the ones proving the calendar knows it
is provisional and that real market data overrides it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from decile_core.trading_calendar import (
    EXPECTED_WINDOW_LENGTHS,
    build_calendar,
    default_calendar_range,
    is_provisional,
    load_seed_holidays,
    reconcile,
    snap_backward,
    snap_forward,
)


class TestRange:
    def test_covers_2011_to_the_current_year(self) -> None:
        """Prompt 1 deliverable 3: "seeds NSE trading holidays for 2011..current year"."""
        start, end = default_calendar_range(dt.date(2026, 8, 20))
        assert start == dt.date(2011, 1, 1)
        assert end == dt.date(2026, 12, 31)

    def test_every_calendar_day_gets_a_row(self) -> None:
        rows = build_calendar(dt.date(2026, 1, 1), dt.date(2026, 1, 31))
        assert len(rows) == 31
        assert [r.date.day for r in rows] == list(range(1, 32))

    def test_rejects_an_inverted_range(self) -> None:
        with pytest.raises(ValueError, match="precedes"):
            build_calendar(dt.date(2026, 2, 1), dt.date(2026, 1, 1))


class TestClassification:
    def test_weekends_are_not_trading_days(self) -> None:
        rows = {r.date: r for r in build_calendar(dt.date(2026, 8, 14), dt.date(2026, 8, 17))}
        assert rows[dt.date(2026, 8, 15)].source == "weekend"  # Saturday
        assert rows[dt.date(2026, 8, 16)].source == "weekend"  # Sunday
        assert not rows[dt.date(2026, 8, 15)].is_trading_day
        assert rows[dt.date(2026, 8, 17)].is_trading_day  # Monday

    def test_a_seeded_holiday_is_not_a_trading_day(self) -> None:
        rows = {r.date: r for r in build_calendar(dt.date(2026, 1, 20), dt.date(2026, 1, 31))}
        republic_day = rows[dt.date(2026, 1, 26)]
        assert not republic_day.is_trading_day
        assert republic_day.source == "holiday"
        assert republic_day.holiday_name == "Republic Day"

    def test_a_weekday_with_nothing_against_it_is_only_derived(self) -> None:
        """The weakest claim in the table must not masquerade as an observed fact."""
        rows = {r.date: r for r in build_calendar(dt.date(2026, 8, 17), dt.date(2026, 8, 18))}
        assert rows[dt.date(2026, 8, 18)].source == "derived"


class TestProvisionality:
    def test_a_freshly_seeded_calendar_is_provisional(self) -> None:
        """It rests on an incomplete holiday list, and must say so rather than imply authority."""
        assert is_provisional(build_calendar(dt.date(2026, 1, 1), dt.date(2026, 12, 31)))

    def test_reconciliation_promotes_dates_with_real_bars(self) -> None:
        rows = build_calendar(dt.date(2026, 8, 17), dt.date(2026, 8, 21))
        reconciled = reconcile(rows, frozenset({dt.date(2026, 8, 18)}))
        promoted = next(r for r in reconciled if r.date == dt.date(2026, 8, 18))
        assert promoted.source == "bhavcopy"
        assert promoted.is_trading_day

    def test_reconciliation_does_not_close_a_day_merely_for_missing_bars(self) -> None:
        """An absent bar can be a backfill gap; only Prompt 3's universe-wide view can decide."""
        rows = build_calendar(dt.date(2026, 8, 17), dt.date(2026, 8, 18))
        reconciled = reconcile(rows, frozenset())
        assert all(r.is_trading_day for r in reconciled)

    def test_a_fully_reconciled_calendar_is_no_longer_provisional(self) -> None:
        rows = build_calendar(dt.date(2026, 8, 17), dt.date(2026, 8, 18))
        every_day = frozenset(r.date for r in rows)
        assert not is_provisional(reconcile(rows, every_day))


class TestSnapping:
    """docs/06 step 1 snaps the as-of date backwards; docs/13 §3 snaps window starts forwards."""

    TRADING = frozenset({dt.date(2026, 8, 14), dt.date(2026, 8, 17), dt.date(2026, 8, 18)})

    def test_snap_backward_from_a_weekend(self) -> None:
        assert snap_backward(dt.date(2026, 8, 16), self.TRADING) == dt.date(2026, 8, 14)

    def test_snap_backward_is_a_no_op_on_a_trading_day(self) -> None:
        assert snap_backward(dt.date(2026, 8, 18), self.TRADING) == dt.date(2026, 8, 18)

    def test_snap_forward_from_a_weekend(self) -> None:
        assert snap_forward(dt.date(2026, 8, 15), self.TRADING) == dt.date(2026, 8, 17)

    def test_snap_forward_is_a_no_op_on_a_trading_day(self) -> None:
        assert snap_forward(dt.date(2026, 8, 17), self.TRADING) == dt.date(2026, 8, 17)

    def test_snapping_refuses_to_wander(self) -> None:
        """Silently returning a date weeks away would corrupt a window start."""
        with pytest.raises(ValueError, match="no trading day"):
            snap_backward(dt.date(2026, 8, 16), self.TRADING, limit=1)


class TestSeedFile:
    def test_holiday_dates_are_unique(self) -> None:
        holidays = load_seed_holidays()
        assert len(holidays) == len({d for d in holidays})

    def test_no_holiday_falls_on_a_weekend(self) -> None:
        """A weekend entry is noise: the calendar already closes those days."""
        assert [d for d in load_seed_holidays() if d.weekday() >= 5] == []

    def test_every_year_from_2011_is_represented(self) -> None:
        years = {d.year for d in load_seed_holidays()}
        assert set(range(2011, 2027)) <= years


def test_expected_window_lengths_are_the_docs_13_figures() -> None:
    """docs/13 §3 recovered 22 / 64 / 121 / 185 / 247 trading days as of 2026-08-18.

    Prompt 5 asserts the calendar reproduces them. Recording the target here keeps the number
    next to the component that has to hit it.
    """
    assert EXPECTED_WINDOW_LENGTHS == {1: 22, 3: 64, 6: 121, 9: 185, 12: 247}
