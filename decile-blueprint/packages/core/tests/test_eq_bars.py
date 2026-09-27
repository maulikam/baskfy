"""LV5 — the pure readers over equity minute bars (``gates/live-5-eq-bars.md`` B5)."""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal

import pytest

from baskfy_core import eq_bars
from baskfy_core.eq_bars import Bar, VolumeBar, opening_range, session_open, session_volume
from baskfy_core.options import bars as option_bars

IST = eq_bars.IST
DAY = dt.date(2026, 9, 28)


def bar(hhmm: str, o: str, h: str, l: str, c: str) -> Bar:  # noqa: E741 - o/h/l/c are the prices
    hour, minute = (int(x) for x in hhmm.split(":"))
    return Bar(
        dt.datetime.combine(DAY, dt.time(hour, minute), tzinfo=IST),
        Decimal(o),
        Decimal(h),
        Decimal(l),
        Decimal(c),
    )


def at(hhmm: str, second: int = 0) -> dt.datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(DAY, dt.time(hour, minute, second), tzinfo=IST)


class TestOpeningRange:
    def test_the_first_five_minutes_from_closed_bars_only(self) -> None:
        bars = [
            bar("09:15", "100", "101", "99", "100.5"),
            bar("09:16", "100.5", "103", "100", "102"),
            bar("09:19", "102", "102.5", "101", "101.5"),
            bar("09:20", "101.5", "110", "101", "109"),
        ]
        rng = opening_range(bars, minutes=5, now=at("09:20"))
        assert (rng.high, rng.low) == (Decimal("103"), Decimal("99"))
        assert rng.bars == 3 and rng.complete is True  # 09:20 has begun: 09:15-09:19 have closed

    def test_a_range_still_forming_is_not_complete_and_ignores_the_forming_minute(self) -> None:
        bars = [
            bar("09:15", "100", "101", "99", "100.5"),
            bar("09:16", "100.5", "103", "100", "102"),
        ]
        rng = opening_range(bars, minutes=5, now=at("09:16", 30))
        assert rng.complete is False
        assert (rng.high, rng.low) == (Decimal("101"), Decimal("99"))  # 09:16 is still forming
        assert rng.bars == 1

    def test_an_empty_range_has_no_extremes(self) -> None:
        rng = opening_range([], minutes=15, now=at("09:30"))
        assert rng.high is None and rng.low is None and rng.bars == 0 and rng.complete is True

    def test_a_zero_minute_range_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one minute"):
            opening_range([], minutes=0, now=at("09:30"))


class TestTheSharedDefinitions:
    def test_five_minute_bars_are_the_options_modules_own(self) -> None:
        assert eq_bars.five_minute_bars is option_bars.five_minute_bars
        assert eq_bars.window is option_bars.window and eq_bars.closed is option_bars.closed

    def test_session_open_is_nine_fifteen_ist(self) -> None:
        assert session_open(DAY) == at("09:15")

    def test_session_volume_counts_closed_minutes_only(self) -> None:
        bars = [
            VolumeBar(bar("09:15", "1", "1", "1", "1"), 100),
            VolumeBar(bar("09:16", "1", "1", "1", "1"), 250),
            VolumeBar(bar("09:17", "1", "1", "1", "1"), 999),
        ]
        assert session_volume(bars, at("09:17", 10)) == 350
        assert session_volume(bars, at("09:18")) == 1349


def test_the_module_is_pure() -> None:
    """Law 1: no database, no network, no disk, no clock — read off the module's imports."""
    source = inspect.getsource(eq_bars)
    for banned in ("sqlalchemy", "import os", "requests", "httpx", "datetime.now", "time.time"):
        assert banned not in source, banned
