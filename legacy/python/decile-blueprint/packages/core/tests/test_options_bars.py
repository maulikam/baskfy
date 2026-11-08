"""OP4 — the readings every sleeve shares (``04`` preamble): windows, 5-minute bars aligned to
09:15 and built from one-minute bars, closed minutes only, IST whatever the stored zone."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from options_scan_fixtures import QUIET_MONTHLY, at, bars_from_closes, ramp

from baskfy_core.options import bars as b

DAY = QUIET_MONTHLY
OPEN = dt.time(9, 15)


def day_bars(count: int = 30) -> tuple[b.Bar, ...]:
    return bars_from_closes(DAY, Decimal(25000), ramp(Decimal(25000), Decimal(1), count))


class TestMinutes:
    def test_an_aware_utc_timestamp_is_read_in_ist(self) -> None:
        utc = dt.datetime(2026, 10, 27, 3, 45, tzinfo=dt.UTC)  # 09:15 IST
        assert b.minute(utc) == dt.time(9, 15)
        assert b.ist(utc) == dt.datetime(2026, 10, 27, 9, 15)

    def test_only_closed_minutes_count(self) -> None:
        seen = b.closed(day_bars(), dt.datetime(2026, 10, 27, 9, 20, 59, tzinfo=b.IST))
        assert [b.minute(x.ts) for x in seen][-1] == dt.time(9, 19)
        assert len(b.closed(day_bars(), at(DAY, 9, 20))) == 5

    def test_a_duplicate_minute_keeps_the_last_reading(self) -> None:
        first = day_bars(1)[0]
        again = b.Bar(first.ts, first.open, first.high, first.low, Decimal(1))
        assert b.closed((first, again), at(DAY, 10, 0)) == (again,)

    def test_windows_are_inclusive_by_start_minute(self) -> None:
        assert len(b.window(day_bars(), dt.time(9, 15), dt.time(9, 44))) == 30
        assert b.expected_minutes(dt.time(9, 15), dt.time(9, 44)) == 30
        assert b.expected_minutes(dt.time(9, 15), dt.time(10, 14)) == 60

    def test_a_window_settles_on_its_last_minute_or_after_the_grace(self) -> None:
        bars = day_bars(29)  # 09:15-09:43: the 09:44 minute never arrives
        assert not b.window_settled(bars, dt.time(9, 44), at(DAY, 9, 46), 120)
        assert b.window_settled(bars, dt.time(9, 44), at(DAY, 9, 47), 120)
        assert b.window_settled(day_bars(30), dt.time(9, 44), at(DAY, 9, 45), 120)

    def test_the_clock_helpers(self) -> None:
        assert b.minute_after(dt.time(9, 59)) == dt.time(10, 0)
        assert b.add_seconds(dt.time(23, 59), 120) == dt.time.max
        with pytest.raises(ValueError, match="percent of zero"):
            b.pct(Decimal(1), Decimal(0))


class TestFiveMinuteBars:
    def test_aligned_to_0915_and_named_by_the_last_minute(self) -> None:
        fives = b.five_minute_bars(day_bars(12), OPEN, 5)
        assert [(f.start, f.close_time) for f in fives] == [
            (dt.time(9, 15), dt.time(9, 19)),
            (dt.time(9, 20), dt.time(9, 24)),
            (dt.time(9, 25), dt.time(9, 29)),
        ]
        first = fives[0]
        assert first.open == Decimal(25000) and first.close == Decimal(25005)
        assert first.high == Decimal(25007) and first.low == Decimal(24998)
        assert first.complete and first.minutes == 5

    def test_a_bucket_without_its_last_minute_is_not_complete(self) -> None:
        fives = b.five_minute_bars(day_bars(12), OPEN, 5)
        assert not fives[-1].complete and fives[-1].close is None and fives[-1].minutes == 2

    def test_a_missing_middle_minute_keeps_the_close(self) -> None:
        bars = tuple(x for x in day_bars(5) if b.minute(x.ts) != dt.time(9, 17))
        (five,) = b.five_minute_bars(bars, OPEN, 5)
        assert five.complete and five.minutes == 4 and five.close == Decimal(25005)

    def test_bars_before_the_open_are_ignored_and_width_must_be_positive(self) -> None:
        early = b.Bar(at(DAY, 9, 10), Decimal(1), Decimal(1), Decimal(1), Decimal(1))
        assert b.five_minute_bars((early,), OPEN, 5) == ()
        with pytest.raises(ValueError, match="one minute"):
            b.five_minute_bars((), OPEN, 0)
