"""The per-bar columns the rules read (``docs/vbt/04`` §1), and the densification behind them.

The interesting assertions here are all about **sessions versus rows**: a rolling window counts
sessions of the calendar, so a name that did not print yesterday has a null ``change_pct`` rather
than a two-day move recorded as a one-day one.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from vbt_fixtures import (
    calendar_for,
    flat_bars,
    rising_bars,
    sessions,
    set_bar,
    with_background,
)

from baskfy_core.vbt.calendar import build_calendar
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG
from baskfy_core.vbt.indicators import densify, require_columns, with_vbt_indicators


def indicated(bars: pl.DataFrame, *, subject: int = 1) -> pl.DataFrame:
    """Indicators for the subject instrument, with a full-history background name present.

    The background keeps every session in the calendar, so a hole punched in the subject is a
    **missing bar** rather than a missing session.
    """
    count = bars.filter(pl.col("instrument_id") == subject).height
    full = with_background(bars, count=max(count, bars["date"].n_unique()))
    return with_vbt_indicators(full, calendar_for(full)).filter(pl.col("instrument_id") == subject)


def at(frame: pl.DataFrame, session: dt.date, column: str) -> float | bool | None:
    """One cell of the subject's indicated frame, for the session named."""
    value = frame.filter(pl.col("date") == session)[column].to_list()[0]
    if value is None or isinstance(value, bool):
        return value
    return float(value)


class TestRequiredColumns:
    def test_a_missing_column_is_named(self) -> None:
        with pytest.raises(ValueError, match="close_raw"):
            require_columns(flat_bars().drop("close_raw"))

    def test_the_optional_ones_are_tolerated(self) -> None:
        frame = indicated(flat_bars().drop("upper_circuit"))
        assert frame["locked_upper_circuit"].to_list()[-1] is False


class TestDensify:
    def test_a_missing_bar_becomes_a_null_row_not_an_absent_one(self) -> None:
        days = sessions(30)
        bars = flat_bars(count=30)
        calendar = build_calendar(bars)
        holed = bars.filter(pl.col("date") != days[10])
        dense = densify(holed, calendar)
        assert dense.height == 30
        assert dense.filter(pl.col("date") == days[10])["close"].to_list() == [None]

    def test_nothing_is_forward_filled(self) -> None:
        """A bar that did not happen must never become a fill price."""
        days = sessions(30)
        bars = flat_bars(count=30, close=100.0)
        calendar = build_calendar(bars)
        dense = densify(bars.filter(pl.col("date") != days[10]), calendar)
        row = dense.filter(pl.col("date") == days[10])
        assert row["open"].to_list() == [None]
        assert row["low"].to_list() == [None]

    def test_the_symbol_rides_onto_the_blank_rows(self) -> None:
        days = sessions(30)
        bars = flat_bars(count=30, symbol="GAPPY")
        dense = densify(bars.filter(pl.col("date") != days[10]), build_calendar(bars))
        assert set(dense["symbol"].to_list()) == {"GAPPY"}


class TestPrevCloseAndChange:
    def test_change_is_against_the_previous_session(self) -> None:
        days = sessions(30)
        bars = set_bar(flat_bars(count=30, close=100.0), days[20], close=110.0, high=110.0)
        frame = indicated(bars)
        assert at(frame, days[20], "change_pct") == pytest.approx(10.0)

    def test_a_blank_session_leaves_the_next_change_null(self) -> None:
        """`04` §1: null rather than a two-day move recorded as a one-day one — which would
        hand filter E a 12% two-session drift as if it were a 12% bar."""
        days = sessions(30)
        bars = flat_bars(count=30).filter(pl.col("date") != days[20])
        frame = indicated(bars)
        assert at(frame, days[20], "close") is None
        assert at(frame, days[21], "change_pct") is None


class TestTheRollingWindows:
    def test_the_volume_average_includes_today(self) -> None:
        """Chartink reads ``Sma(Volume, 50)`` on the closed bar, the signal day included."""
        days = sessions(80)
        bars = set_bar(flat_bars(count=80, volume=100.0), days[70], volume=5_100.0)
        frame = indicated(bars)
        # 49 sessions at 100 and one at 5,100 → 200.
        assert at(frame, days[70], "vol_sma") == pytest.approx(200.0)

    def test_the_prior_high_excludes_today(self) -> None:
        """Filter B compares today's close with the 20 sessions **ending yesterday**; including
        today, ``close > high`` could never be true."""
        days = sessions(60)
        bars = set_bar(flat_bars(count=60, close=100.0), days[50], high=140.0, close=139.0)
        frame = indicated(bars)
        assert at(frame, days[50], "high_prior") == pytest.approx(100.0)
        assert at(frame, days[51], "high_prior") == pytest.approx(140.0)

    def test_the_lookback_return_counts_sessions(self) -> None:
        days = sessions(60)
        bars = flat_bars(count=60, close=100.0)
        bars = set_bar(bars, days[50], close=120.0, high=120.0)
        frame = indicated(bars)
        assert at(frame, days[50], "ret_lookback_pct") == pytest.approx(20.0)

    @pytest.mark.parametrize(("blanks", "expected"), [(5, True), (6, False)])
    def test_the_window_tolerates_a_tenth_of_its_bars_and_no_more(
        self, blanks: int, expected: bool
    ) -> None:
        """45 of 50 bars is 90% and the average is computed; 44 is 88% and it is not."""
        days = sessions(60)
        holes = days[5 : 5 + blanks]
        bars = flat_bars(count=60).filter(~pl.col("date").is_in(holes))
        frame = indicated(bars)
        assert (at(frame, days[49], "vol_sma") is not None) is expected

    def test_the_dma_needs_its_own_tolerance(self) -> None:
        frame = indicated(rising_bars(count=260))
        days = sessions(260)
        assert at(frame, days[178], "sma_dma") is None
        assert at(frame, days[179], "sma_dma") is not None


class TestClosePosition:
    def test_a_strong_close_is_one(self) -> None:
        days = sessions(30)
        bars = set_bar(flat_bars(count=30), days[10], high=110.0, low=100.0, close=110.0)
        assert at(indicated(bars), days[10], "close_position") == pytest.approx(1.0)

    def test_a_locked_bar_is_half_not_a_division_by_zero(self) -> None:
        """`04` §1: 0.5 refuses to call a bar with no range either strong or weak."""
        frame = indicated(flat_bars(count=30))
        assert at(frame, sessions(30)[10], "close_position") == pytest.approx(0.5)


class TestTurnover:
    def test_turnover_is_the_exchange_print_times_the_quantity(self) -> None:
        days = sessions(30)
        bars = flat_bars(count=30, close=100.0, volume=1_000.0)
        bars = bars.with_columns(pl.lit(80.0).alias("close_raw"))
        frame = indicated(bars)
        assert at(frame, days[10], "turnover_inr") == pytest.approx(80_000.0)

    def test_a_stored_turnover_column_is_ignored(self) -> None:
        """One definition of "₹2 crore of turnover", so a page and a test cannot disagree."""
        bars = flat_bars(count=30, close=100.0, volume=1_000.0).with_columns(
            pl.lit(1.0).alias("turnover")
        )
        frame = indicated(bars)
        assert at(frame, sessions(30)[10], "turnover_inr") == pytest.approx(100_000.0)


class TestTheExitAverage:
    def test_the_ema_is_null_until_its_span_exists(self) -> None:
        frame = indicated(rising_bars(count=40))
        days = sessions(40)
        assert at(frame, days[19], "ema_exit") is None
        assert at(frame, days[20], "ema_exit") is not None

    def test_the_ema_lags_a_rising_series(self) -> None:
        frame = indicated(rising_bars(count=60, start_close=50.0, step=1.0))
        days = sessions(60)
        ema, close = at(frame, days[50], "ema_exit"), at(frame, days[50], "close")
        assert ema is not None and close is not None
        assert ema < close


class TestTheCircuitFlag:
    def test_a_bar_at_the_band_is_flagged_and_kept(self) -> None:
        days = sessions(30)
        bars = set_bar(flat_bars(count=30), days[10], high=110.0, upper_circuit=110.0)
        frame = indicated(bars)
        assert at(frame, days[10], "locked_upper_circuit") is True
        assert frame.height == 30

    def test_no_band_is_no_lock(self) -> None:
        """`04` §3.3: where ``upper_circuit`` is absent, no lock is assumed."""
        frame = indicated(flat_bars(count=30))
        assert set(frame["locked_upper_circuit"].to_list()) == {False}


def test_bars_required_is_two_hundred_and_one() -> None:
    assert DEFAULT_VBT_CONFIG.bars_required == 201
