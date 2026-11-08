"""The run's own calendar: thin sessions out, and sessions counted rather than days.

``docs/vbt/04`` §2. This is the least glamorous rule in the pack and one of the two that
changed the study's numbers: on the research's first run the 200-DMA filter appeared to vanish
for most of 2024-25, because six muhurat and special-Saturday columns were poisoning every 50-
and 200-session window that spanned them.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest
from vbt_fixtures import sessions

from baskfy_core.vbt.calendar import (
    build_calendar,
    drop_thin_sessions,
    session_counts,
    thin_sessions,
)
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, DataConfig


def _panel(counts: list[int]) -> pl.DataFrame:
    """One row per traded name per session, with ``counts[i]`` names on session ``i``."""
    days = sessions(len(counts))
    rows = [
        {"instrument_id": name, "date": day}
        for day, traded in zip(days, counts, strict=True)
        for name in range(traded)
    ]
    return pl.DataFrame(rows)


class TestTheThinSessionRule:
    def test_an_ordinary_history_drops_nothing(self) -> None:
        assert thin_sessions(_panel([1000] * 80)) == []

    def test_a_muhurat_session_is_dropped(self) -> None:
        counts = [1000] * 80
        counts[40] = 200  # ~20% of a normal session: the shape STRATEGY §1 describes
        days = sessions(80)
        assert thin_sessions(_panel(counts)) == [days[40]]

    def test_the_threshold_is_a_share_of_the_neighbourhood_not_an_absolute(self) -> None:
        """A universe that grows does not retroactively make its early sessions thin."""
        counts = [200] * 40 + [1000] * 40
        assert thin_sessions(_panel(counts)) == []

    def test_a_session_at_exactly_the_share_is_kept(self) -> None:
        """``< min_share x median`` — a session at the line is a session, not a rounding call."""
        counts = [1000] * 80
        counts[40] = 250  # exactly 25% of 1000
        assert thin_sessions(_panel(counts)) == []

    def test_lowering_the_share_keeps_more(self) -> None:
        counts = [1000] * 80
        counts[40] = 200
        strict = DataConfig(thin_session_min_share=0.1)
        assert thin_sessions(_panel(counts), strict) == []

    def test_the_window_is_centred(self) -> None:
        """The comparison a person makes is with the sessions on either side, not the fortnight
        before — so a thin session in the *first* half of the history is still found."""
        counts = [1000] * 80
        counts[3] = 150
        days = sessions(80)
        assert thin_sessions(_panel(counts)) == [days[3]]

    def test_the_six_dates_are_not_hard_coded_anywhere_in_the_package(self) -> None:
        """``04`` §2.1: the rule is the contract, the six dates are a test's expectation. A
        seventh muhurat session in 2027 must be found by the rule, not by an edit."""
        package = Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "vbt"
        code = "\n".join(
            line
            for path in package.glob("*.py")
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        for known in ("2017-10-19", "2018-11-07", "2024-01-20", "2024-05-18", "2025-02-01"):
            assert code.count(known) <= 1, f"{known} looks hard-coded outside a docstring"


class TestDropping:
    def test_the_frame_loses_the_thin_session_and_the_calendar_names_it(self) -> None:
        counts = [1000] * 80
        counts[40] = 200
        bars = _panel(counts)
        kept, calendar = drop_thin_sessions(bars)
        days = sessions(80)
        assert days[40] not in kept["date"].to_list()
        assert calendar.dropped == (days[40],)
        assert len(calendar.sessions) == 79
        assert calendar.counts[days[40]] == 200

    def test_nothing_to_drop_returns_the_same_rows(self) -> None:
        bars = _panel([1000] * 40)
        kept, calendar = drop_thin_sessions(bars)
        assert kept.height == bars.height
        assert calendar.dropped == ()

    def test_session_counts_needs_a_date_column(self) -> None:
        with pytest.raises(ValueError, match="date"):
            session_counts(pl.DataFrame({"instrument_id": [1]}))


class TestCountingSessions:
    """A limit that rests over a holiday has not worked a session (``04`` §7.2)."""

    def test_sessions_between_ignores_the_gap(self) -> None:
        days = [dt.date(2024, 1, 4), dt.date(2024, 1, 5), dt.date(2024, 1, 8)]
        bars = pl.DataFrame({"instrument_id": [1, 1, 1], "date": days})
        calendar = build_calendar(bars)
        # Friday to Monday is three calendar days and one session.
        assert calendar.sessions_between(days[1], days[2]) == 1
        assert (days[2] - days[1]).days == 3

    def test_advance_walks_the_calendar(self) -> None:
        days = sessions(10)
        calendar = build_calendar(_panel([5] * 10))
        assert calendar.advance(days[0], 3) == days[3]
        assert calendar.advance(days[8], 3) is None

    def test_a_session_that_is_not_in_the_calendar_answers_nothing(self) -> None:
        calendar = build_calendar(_panel([5] * 10))
        assert calendar.index_of(dt.date(1999, 1, 1)) == -1
        assert calendar.sessions_between(dt.date(1999, 1, 1), sessions(10)[2]) == 0


class TestTheMissingBarTolerance:
    """``04`` §2.2 — a window of n sessions is valid once it holds ``round(0.9 n)`` bars."""

    @pytest.mark.parametrize(("window", "expected"), [(50, 45), (200, 180), (20, 18), (21, 19)])
    def test_the_documented_windows(self, window: int, expected: int) -> None:
        assert DEFAULT_VBT_CONFIG.data.min_samples(window) == expected

    def test_a_tiny_window_still_needs_two_bars(self) -> None:
        assert DEFAULT_VBT_CONFIG.data.min_samples(1) == 2
