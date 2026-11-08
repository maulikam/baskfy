"""The run's own calendar and the missing-bar tolerance (``docs/twt/04`` §2).

``docs/twt/06`` TW1: *"``04`` §2.1's thin sessions and §2.2's 10 % tolerance are tests, not
comments."* Both come from ``research/tight-close/STRATEGY.md`` §1 by way of
``research/volume-breakout/STRATEGY.md`` §1, where a single muhurat column made the 200-session
average appear to vanish for most of 2024-25.

The arithmetic is VBT-1's and is *called* (``06`` TW1), so these tests are about TWT's own
thresholds reaching it — a sleeve that shared the numbers as well as the code would have its
calendar moved by somebody else's recalibration.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from twt_fixtures import calendar_for, drop_bar, flat_bars, sessions, with_background

from baskfy_core.twt.calendar import (
    as_shared_config,
    build_calendar,
    drop_thin_sessions,
    session_counts,
    thin_sessions,
)
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, DataConfig
from baskfy_core.twt.indicators import with_twt_indicators

DATA = DEFAULT_TWT_CONFIG.data


def attendance(
    count: int, *, names: int, thin_session: dt.date | None, present: int
) -> pl.DataFrame:
    """A panel of ``names`` instruments, with ``present`` of them printing on one session."""
    days = sessions(count)
    rows: list[dict[str, object]] = []
    for day in days:
        showing = present if day == thin_session else names
        rows.extend({"instrument_id": i, "date": day} for i in range(showing))
    return pl.DataFrame(rows, schema={"instrument_id": pl.Int64, "date": pl.Date})


class TestTheThinSessionRule:
    """``04`` §2.1. A session whose traded-name count is below ``thin_session_min_share`` [0.25] of
    the **centred** rolling median of that count is not a trading session for this strategy."""

    def test_a_session_below_a_quarter_of_the_median_is_not_a_trading_session(self) -> None:
        muhurat = sessions(60)[30]
        found = thin_sessions(attendance(60, names=100, thin_session=muhurat, present=10))
        assert found == [muhurat]

    def test_a_quiet_session_above_the_share_is_still_a_trading_session(self) -> None:
        """30 of 100 is a quiet day; 25 of 100 is the line, and the rule reads *below* it."""
        quiet = sessions(60)[30]
        assert thin_sessions(attendance(60, names=100, thin_session=quiet, present=30)) == []

    def test_the_share_is_read_from_twt_s_own_config_and_not_vbt_s(self) -> None:
        """A sleeve that inherited the number would have its calendar moved by another sleeve's
        recalibration. ``as_shared_config`` writes every field out, and this is what says so."""
        strict = DataConfig(thin_session_min_share=0.5)
        quiet = sessions(60)[30]
        bars = attendance(60, names=100, thin_session=quiet, present=30)
        assert thin_sessions(bars, strict) == [quiet]
        assert thin_sessions(bars) == []

    def test_the_shared_config_carries_every_calendar_threshold(self) -> None:
        shared = as_shared_config(DATA).data
        assert shared.thin_session_min_share == DATA.thin_session_min_share
        assert shared.thin_session_window_bars == DATA.thin_session_window_bars
        assert shared.thin_session_min_periods == DATA.thin_session_min_periods
        assert shared.rolling_min_share == DATA.rolling_min_share

    def test_a_thin_session_is_removed_before_any_rolling_statistic(self) -> None:
        """It is *removed*, not down-weighted: a single such column poisons every window that
        spans it, which is the whole reason the rule exists."""
        muhurat = sessions(60)[30]
        bars = attendance(60, names=100, thin_session=muhurat, present=10)
        kept, calendar = drop_thin_sessions(bars)
        assert muhurat not in kept["date"].to_list()
        assert calendar.dropped == (muhurat,)
        assert len(calendar.sessions) == 59

    def test_the_calendar_counts_every_session_including_the_ones_it_drops(self) -> None:
        """The funnel has to be able to say *why* a session is not there."""
        muhurat = sessions(60)[30]
        calendar = build_calendar(attendance(60, names=100, thin_session=muhurat, present=10))
        assert calendar.counts[muhurat] == 10
        assert calendar.index_of(muhurat) == -1

    def test_session_counts_are_one_row_a_session(self) -> None:
        counts = session_counts(attendance(10, names=7, thin_session=None, present=7))
        assert counts.height == 10
        assert counts["traded"].to_list() == [7] * 10


class TestTheMissingBarTolerance:
    """``04`` §2.2. A rolling window over *n* sessions is valid once it holds
    ``max(2, round(n x rolling_min_share))`` bars — the way a screener that only sees traded bars
    computes an average."""

    @pytest.mark.parametrize(
        ("window", "expected"),
        [(50, 45), (200, 180), (20, 18), (2, 2), (1, 2)],
    )
    def test_the_tolerance_is_ninety_percent_of_the_window_floored_at_two(
        self, window: int, expected: int
    ) -> None:
        assert DATA.min_samples(window) == expected

    def test_a_name_that_missed_five_of_fifty_sessions_still_has_a_volume_average(self) -> None:
        """Demanding a full window would blank every name with an occasional no-trade day and
        silently shrink the universe to the most liquid names — which is the job the liquidity
        floor does explicitly, later and on purpose."""
        count = DATA.min_samples(DEFAULT_TWT_CONFIG.scan.vol_sma_bars)
        days = sessions(60)
        bars = with_background(flat_bars(count=60), count=60)
        for day in days[len(days) - DEFAULT_TWT_CONFIG.scan.vol_sma_bars :][
            : DEFAULT_TWT_CONFIG.scan.vol_sma_bars - count
        ]:
            bars = drop_bar(bars, day)
        indicated = with_twt_indicators(bars, calendar_for(bars))
        row = indicated.filter(
            (pl.col("date") == days[-1]) & (pl.col("instrument_id") == 1)
        ).to_dicts()[0]
        assert row["vol_sma"] is not None

    def test_a_name_that_missed_six_of_fifty_sessions_has_none(self) -> None:
        count = DATA.min_samples(DEFAULT_TWT_CONFIG.scan.vol_sma_bars)
        days = sessions(60)
        bars = with_background(flat_bars(count=60), count=60)
        for day in days[len(days) - DEFAULT_TWT_CONFIG.scan.vol_sma_bars :][
            : DEFAULT_TWT_CONFIG.scan.vol_sma_bars - count + 1
        ]:
            bars = drop_bar(bars, day)
        indicated = with_twt_indicators(bars, calendar_for(bars))
        row = indicated.filter(
            (pl.col("date") == days[-1]) & (pl.col("instrument_id") == 1)
        ).to_dicts()[0]
        assert row["vol_sma"] is None


class TestHowMuchHistoryADetectionNeeds:
    """``04`` §2.3."""

    def test_bars_required_covers_the_deepest_window_the_sleeve_reads(self) -> None:
        assert DATA.bars_required >= DEFAULT_TWT_CONFIG.deepest_window_bars

    def test_bars_required_leaves_room_for_the_tolerance_and_the_thin_sessions(self) -> None:
        """200 governs, but the 200-session average also needs 200 **valid** bars under §2.2 and
        the thin-session drops of §2.1 consume some. A ``bars_required`` equal to the window would
        hand the detector a column that is null on its first day."""
        assert DATA.bars_required > DEFAULT_TWT_CONFIG.breadth.dma_bars
