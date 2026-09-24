"""`baskfy_api.overlap_scan`: the morning window the NSE limiter belongs to the swing monitor.

A filings scan asked for between 09:10 and 09:30 IST on a weekday is refused rather than queued
behind the 09:16 monitor and the 09:17 catalyst feed; at any other time, or on a weekend, it may
run. Pure: the clock is an argument.
"""

from __future__ import annotations

import datetime as dt

import pytest

from baskfy_api.metrics import IST
from baskfy_api.overlap_scan import in_busy_window

THURSDAY = dt.date(2026, 9, 24)
SATURDAY = dt.date(2026, 9, 26)


def _at(day: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


@pytest.mark.parametrize(
    ("moment", "busy"),
    [
        (_at(THURSDAY, 9, 9), False),
        (_at(THURSDAY, 9, 10), True),
        (_at(THURSDAY, 9, 16), True),
        (_at(THURSDAY, 9, 29), True),
        (_at(THURSDAY, 9, 30), False),
        (_at(THURSDAY, 13, 0), False),
        (_at(SATURDAY, 9, 16), False),
    ],
)
def test_the_morning_window_is_refused_on_weekdays_only(moment: dt.datetime, busy: bool) -> None:
    assert in_busy_window(moment) is busy


def test_the_window_is_read_in_ist_whatever_the_callers_zone() -> None:
    """The route passes UTC; 03:46 UTC is 09:16 IST."""
    utc = dt.datetime(2026, 9, 24, 3, 46, tzinfo=dt.UTC)
    assert in_busy_window(utc) is True
