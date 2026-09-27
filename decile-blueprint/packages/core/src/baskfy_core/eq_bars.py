"""Equity minute bars as a reader sees them — pure (LV5).

The three readings a live or replayed intraday rule needs, with no I/O and no clock (law 1):

* a **session window** — the bars whose start lies in ``[start, end]`` inclusive, IST wall-clock;
* **five-minute bars**, aligned to 09:15 and built exactly as the options sleeves build theirs
  (``baskfy_core.options.bars.five_minute_bars`` — one definition, never two);
* the **opening range** — the high and low of the first ``minutes`` of the session, and whether it
  is complete for a decision made at ``now`` (only closed minutes count: a bar exists once
  ``ts + 1 min <= now``).

``Bar`` is the options module's; an ``eq_minute_bar`` row maps onto it by its five prices. Volume
rides beside it in :class:`VolumeBar` for the rules that read it (a volume breakout, a dry-up).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from baskfy_core.options.bars import (
    IST,
    Bar,
    FiveMinuteBar,
    closed,
    five_minute_bars,
    high_low,
    window,
)

__all__ = [
    "IST",
    "Bar",
    "FiveMinuteBar",
    "OpeningRange",
    "VolumeBar",
    "closed",
    "five_minute_bars",
    "opening_range",
    "session_open",
    "session_volume",
    "window",
]

SESSION_OPEN: dt.time = dt.time(9, 15)
SESSION_CLOSE: dt.time = dt.time(15, 30)


@dataclass(frozen=True, slots=True)
class VolumeBar:
    """One equity minute: the options ``Bar`` plus the minute's volume."""

    bar: Bar
    volume: int

    @property
    def ts(self) -> dt.datetime:
        return self.bar.ts


@dataclass(frozen=True, slots=True)
class OpeningRange:
    """The first ``minutes`` of the session: its extremes, and whether all of them have closed."""

    minutes: int
    high: Decimal | None
    low: Decimal | None
    bars: int
    complete: bool


def session_open(day: dt.date) -> dt.datetime:
    """09:15 IST on ``day``, aware — the anchor every five-minute bar aligns to."""
    return dt.datetime.combine(day, SESSION_OPEN, tzinfo=IST)


def opening_range(bars: Iterable[Bar], *, minutes: int, now: dt.datetime) -> OpeningRange:
    """The high and low of the session's first ``minutes`` minutes, from closed bars only.

    ``complete`` is true once every minute of the range has closed — ``now`` is at or past
    ``09:15 + minutes`` — whatever the count of bars (a name may not print every minute). A range
    read before that is a range still forming, and the caller must not act on it.
    """
    if minutes <= 0:
        raise ValueError("an opening range is at least one minute long")
    end = (
        dt.datetime.combine(dt.date(2000, 1, 1), SESSION_OPEN) + dt.timedelta(minutes=minutes - 1)
    ).time()
    in_range = window(closed(bars, now), SESSION_OPEN, end)
    extremes = high_low(in_range)
    range_end = (
        dt.datetime.combine(dt.date(2000, 1, 1), SESSION_OPEN) + dt.timedelta(minutes=minutes)
    ).time()
    local = now.astimezone(IST) if now.tzinfo is not None else now.replace(tzinfo=IST)
    return OpeningRange(
        minutes=minutes,
        high=None if extremes is None else extremes[0],
        low=None if extremes is None else extremes[1],
        bars=len(in_range),
        complete=local.time() >= range_end,
    )


def session_volume(bars: Sequence[VolumeBar], now: dt.datetime) -> int:
    """The session's volume so far, from closed minutes only."""
    closed_ts = {bar.ts for bar in closed((vb.bar for vb in bars), now)}
    return sum(vb.volume for vb in bars if vb.ts in closed_ts)
