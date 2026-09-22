"""Index bars as every sleeve reads them: windows, 5-minute bars, Kaufman's ER (``04`` preamble).

The sleeves decide on NIFTY 50's one-minute bars (``op_index_minute``, OP3). This module holds
the three readings they share, so O1's gate, O2's trigger and O3's setups cannot disagree about
what a window, a 5-minute bar or an efficiency ratio is:

* **A window** is the one-minute bars whose *start* lies in ``[start, end]`` inclusive — "the
  09:15-09:44 bars" are thirty bars, 09:15 through 09:44 (condor ``04`` §2.3).
* **A 5-minute bar** is built from the one-minute bars, aligned to 09:15 (09:15-09:19,
  09:20-09:24, ...) and never fetched separately (``04`` preamble). Its **close time** is the
  start of its last minute — the 10:15-10:19 bar "closes at 10:19", as condor's "the 09:59 bar's
  close" names a minute by its start — so ``04`` §5.1's window "[10:19, 13:00]" admits the first
  bar after the 10:14 range and ``04`` §4.2's "[09:30, 13:30]" the first after the 09:29 range.
  It is **complete** once its last minute is stored: that minute's close *is* the bar's close,
  and without it the close is unknown (``DECISIONS-OP`` OP4.2).
* **Kaufman's efficiency ratio** is condor ``04`` §2.4's formula, no smoothing:
  ``|c_n - c_0| / sum |c_i - c_{i-1}|`` over the one-minute closes, with ``c_0`` the first bar's
  **open**; ``0`` when the path has no length.

Only **closed** minutes exist for a decision made at ``now``: a bar counts once
``ts + 1 min <= now`` (the collector writes only those, OP3.7; a replay passes a whole day and a
``now``, and must see exactly what the live scan saw at that minute).

Times are IST wall-clock. An aware ``ts`` (as the database returns it, in UTC) is converted to IST
before its minute is read; a naive one is taken as IST already.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

#: IST is a fixed offset — India has no daylight saving. A constant, not a clock (law 1).
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_ONE_MINUTE = dt.timedelta(minutes=1)
_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Bar:
    """One index minute: its start and OHLC, as ``op_index_minute`` stores it."""

    ts: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def ist(ts: dt.datetime) -> dt.datetime:
    """``ts`` as naive IST wall-clock (aware → converted; naive → taken as IST)."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(IST).replace(tzinfo=None)


def minute(ts: dt.datetime) -> dt.time:
    """The IST minute a bar starts at."""
    return ist(ts).time().replace(second=0, microsecond=0)


def closed(bars: Iterable[Bar], now: dt.datetime) -> tuple[Bar, ...]:
    """The bars finished by ``now`` (``ts + 1 min <= now``), in time order, one per minute.

    A duplicate minute keeps its last reading — the upsert's semantics (OP3.7).
    """
    cutoff = ist(now)
    by_minute: dict[dt.datetime, Bar] = {}
    for bar in bars:
        start = ist(bar.ts).replace(second=0, microsecond=0)
        if start + _ONE_MINUTE <= cutoff:
            by_minute[start] = bar
    return tuple(by_minute[k] for k in sorted(by_minute))


def window(bars: Iterable[Bar], start: dt.time, end: dt.time) -> tuple[Bar, ...]:
    """The bars whose start minute lies in ``[start, end]``, in time order."""
    return tuple(sorted((b for b in bars if start <= minute(b.ts) <= end), key=lambda b: ist(b.ts)))


def expected_minutes(start: dt.time, end: dt.time) -> int:
    """How many one-minute bars ``[start, end]`` holds when nothing is missing."""
    day = dt.date(2000, 1, 1)
    span = dt.datetime.combine(day, end) - dt.datetime.combine(day, start)
    return int(span.total_seconds() // 60) + 1


def bar_at(bars: Iterable[Bar], at: dt.time) -> Bar | None:
    """The bar starting at minute ``at``, if stored."""
    return next((b for b in bars if minute(b.ts) == at), None)


def high_low(bars: Sequence[Bar]) -> tuple[Decimal, Decimal] | None:
    """The window's high and low; ``None`` for an empty window."""
    if not bars:
        return None
    return max(b.high for b in bars), min(b.low for b in bars)


def minute_after(at: dt.time) -> dt.time:
    """The minute after ``at`` — when a bar starting at ``at`` has closed."""
    return (dt.datetime.combine(dt.date(2000, 1, 1), at) + _ONE_MINUTE).time()


def add_seconds(at: dt.time, seconds: int) -> dt.time:
    """``at`` plus ``seconds``, clamped to the same day."""
    moment = dt.datetime.combine(dt.date(2000, 1, 1), at) + dt.timedelta(seconds=seconds)
    return moment.time() if moment.date() == dt.date(2000, 1, 1) else dt.time.max


def window_settled(bars: Iterable[Bar], end: dt.time, now: dt.datetime, grace_seconds: int) -> bool:
    """Whether a window ending at minute ``end`` can be judged at ``now``.

    Once its last minute is stored — or, if that minute never arrives, once ``grace_seconds``
    have passed after it closed — the window is final. Before then a verdict could flip when the
    late minute lands, and a scan state is one-way within a day (``04`` §10; OP4.3).
    """
    if bar_at(bars, end) is not None:
        return True
    return ist(now).time() >= add_seconds(minute_after(end), grace_seconds)


@dataclass(frozen=True, slots=True)
class FiveMinuteBar:
    """A bar built from one-minute bars, aligned to the session open."""

    start: dt.time
    close_time: dt.time
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal | None
    minutes: int

    @property
    def complete(self) -> bool:
        """Its last minute is stored, so its close is known (``DECISIONS-OP`` OP4.2)."""
        return self.close is not None


def five_minute_bars(
    bars: Iterable[Bar], session_open: dt.time, width: int
) -> tuple[FiveMinuteBar, ...]:
    """``width``-minute bars aligned to ``session_open`` (``04`` preamble: 5, from 09:15)."""
    if width <= 0:
        raise ValueError("a bar is at least one minute wide")
    day = dt.date(2000, 1, 1)
    origin = dt.datetime.combine(day, session_open)
    buckets: dict[int, list[Bar]] = {}
    for bar in bars:
        offset = int((dt.datetime.combine(day, minute(bar.ts)) - origin).total_seconds() // 60)
        if offset < 0:
            continue
        buckets.setdefault(offset // width, []).append(bar)
    out: list[FiveMinuteBar] = []
    for index in sorted(buckets):
        members = sorted(buckets[index], key=lambda b: ist(b.ts))
        start = (origin + dt.timedelta(minutes=index * width)).time()
        last = (origin + dt.timedelta(minutes=index * width + width - 1)).time()
        closing = next((b for b in members if minute(b.ts) == last), None)
        out.append(
            FiveMinuteBar(
                start=start,
                close_time=last,
                open=members[0].open,
                high=max(b.high for b in members),
                low=min(b.low for b in members),
                close=closing.close if closing is not None else None,
                minutes=len(members),
            )
        )
    return tuple(out)


def efficiency_ratio(points: Sequence[Decimal]) -> Decimal:
    """Kaufman's ER: net move over path length; ``0`` when the path has no length."""
    if len(points) < 2:  # noqa: PLR2004 - a path needs two points
        return _ZERO
    path = sum((abs(b - a) for a, b in pairwise(points)), _ZERO)
    if path == 0:
        return _ZERO
    return abs(points[-1] - points[0]) / path


def er_points(bars: Sequence[Bar]) -> tuple[Decimal, ...]:
    """Condor ``04`` §2.4's path: the first bar's **open**, then each later bar's close."""
    if not bars:
        return ()
    return (bars[0].open, *(b.close for b in bars[1:]))


def pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    """``numerator / denominator * 100`` — a percent, as every ``*_pct`` field is."""
    if denominator == 0:
        raise ValueError("a percent of zero")
    return numerator / denominator * Decimal(100)
