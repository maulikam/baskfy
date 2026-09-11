"""The per-bar columns the rules read (``docs/twt/04`` §2, §3).

The rolling windows count **sessions, not rows**: the 50-session volume average of a name that did
not trade on two of the last fifty sessions is the average of the forty-eight bars it did print,
over that same fifty-session span. A densification that quietly fell back to "the last fifty traded
bars" would reach further back in time for exactly the illiquid names the liquidity floor exists to
judge, and it would do so without a single null to give it away.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from twt_fixtures import calendar_for, drop_bar, flat_bars, sessions, set_bar, with_background

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG
from baskfy_core.twt.indicators import (
    INDICATOR_COLUMNS,
    REQUIRED_COLUMNS,
    bucket_keys,
    require_columns,
    with_twt_indicators,
)

COUNT = 60
DAYS = sessions(COUNT)


def indicate(bars: pl.DataFrame) -> pl.DataFrame:
    full = with_background(bars, count=COUNT)
    return with_twt_indicators(full, calendar_for(full))


def cell(frame: pl.DataFrame, session: dt.date, column: str) -> object:
    return frame.filter((pl.col("date") == session) & (pl.col("instrument_id") == 1)).to_dicts()[0][
        column
    ]


def test_every_indicator_column_is_added() -> None:
    frame = indicate(flat_bars(count=COUNT))
    assert set(INDICATOR_COLUMNS) <= set(frame.columns)


def test_a_frame_missing_a_required_column_is_an_error_not_a_null() -> None:
    """A silently-null column is a rule that stops firing without saying so."""
    with pytest.raises(ValueError, match="missing required columns"):
        require_columns(flat_bars(count=4).drop("close_raw"))
    assert "close_raw" in REQUIRED_COLUMNS


class TestTheBucketKeys:
    def test_the_week_key_is_the_iso_year_and_never_the_calendar_year(self) -> None:
        days = [dt.date(2024, 12, 27), dt.date(2024, 12, 30), dt.date(2025, 1, 2)]
        frame = pl.DataFrame({"date": days}).with_columns(*bucket_keys())
        assert frame["week_key"].to_list() == [202452, 202501, 202501]

    def test_the_month_key_is_monotone_across_a_year_boundary(self) -> None:
        days = [
            dt.date(2024, 11, 1),
            dt.date(2024, 12, 2),
            dt.date(2025, 1, 2),
            dt.date(2025, 3, 3),
        ]
        frame = pl.DataFrame({"date": days}).with_columns(*bucket_keys())
        keys = frame["month_key"].to_list()
        assert keys == sorted(keys)
        assert keys[3] - keys[1] == 3, "December to March is three months"


class TestTheRollingWindowsCountSessions:
    def test_a_blank_session_does_not_shift_a_window_further_back_in_time(self) -> None:
        """The bar that did not happen stays a null in the window rather than being replaced by an
        older one, so the average still covers the same fifty sessions."""
        bars = flat_bars(count=COUNT, close=100.0, volume=100_000.0)
        bars = set_bar(bars, DAYS[COUNT - 1], volume=200_000.0)
        whole = indicate(bars)
        holed = indicate(drop_bar(bars, DAYS[COUNT - 2]))
        window = DEFAULT_TWT_CONFIG.scan.vol_sma_bars
        assert cell(whole, DAYS[-1], "vol_sma") == (100_000.0 * (window - 1) + 200_000.0) / window
        assert cell(holed, DAYS[-1], "vol_sma") == (100_000.0 * (window - 2) + 200_000.0) / (
            window - 1
        )

    def test_the_volume_average_includes_the_signal_day(self) -> None:
        """Chartink's ``Sma(Volume, 50)`` on a daily run includes today's bar."""
        bars = set_bar(flat_bars(count=COUNT, volume=100_000.0), DAYS[-1], volume=5_000_000.0)
        window = DEFAULT_TWT_CONFIG.scan.vol_sma_bars
        expected = (100_000.0 * (window - 1) + 5_000_000.0) / window
        assert cell(indicate(bars), DAYS[-1], "vol_sma") == expected

    def test_the_turnover_average_reads_the_exchange_print(self) -> None:
        """``04`` §3.5's ₹5 crore is a rupee number about ``close_raw x volume``, not about the
        adjusted series: a 1:2 split does not double a name's rupee turnover."""
        bars = flat_bars(count=COUNT, close=100.0, volume=100_000.0)
        bars = bars.with_columns(
            pl.when(pl.col("instrument_id") == 1)
            .then(pl.col("close_raw") / 2)
            .otherwise(pl.col("close_raw"))
            .alias("close_raw")
        )
        frame = indicate(bars)
        assert cell(frame, DAYS[-1], "turnover_inr") == 50.0 * 100_000.0
        assert cell(frame, DAYS[-1], "turnover_avg_20") == 50.0 * 100_000.0


class TestTheBarShapeColumns:
    def test_a_bar_with_no_range_is_limit_locked(self) -> None:
        """``04`` §5.2: ``open == high == low``, and no fill is possible at a price the book would
        accept."""
        bars = set_bar(flat_bars(count=COUNT), DAYS[-1], close=120.0)
        assert cell(indicate(bars), DAYS[-1], "limit_locked") is True

    def test_a_bar_with_a_range_is_not(self) -> None:
        bars = set_bar(flat_bars(count=COUNT), DAYS[-1], close=120.0, low=118.0)
        assert cell(indicate(bars), DAYS[-1], "limit_locked") is False

    def test_a_session_with_no_bar_is_not_locked_it_is_absent(self) -> None:
        """The plan tells the two apart by the null ``open``, which is its own skip (``NO_BAR``);
        a blank session read as "locked" would send a person looking for a circuit that never
        happened."""
        frame = indicate(drop_bar(flat_bars(count=COUNT), DAYS[-1]))
        assert cell(frame, DAYS[-1], "limit_locked") is False
        assert cell(frame, DAYS[-1], "open") is None


class TestTheOptionalColumns:
    def test_a_frame_without_is_etf_reads_as_nothing_being_an_etf(self) -> None:
        """Right for a hand-built fixture and wrong for a production panel, which is why the
        worker always passes it (``04`` §1.3: the ``etf`` index universe is the authority)."""
        bars = with_background(flat_bars(count=COUNT).drop("is_etf"), count=COUNT)
        frame = with_twt_indicators(bars, calendar_for(bars))
        assert frame["is_etf"].to_list() == [False] * frame.height

    def test_a_frame_without_adj_factor_reads_as_unadjusted(self) -> None:
        bars = with_background(flat_bars(count=COUNT).drop("adj_factor"), count=COUNT)
        frame = with_twt_indicators(bars, calendar_for(bars))
        assert frame["adj_factor"].unique().to_list() == [1.0]
