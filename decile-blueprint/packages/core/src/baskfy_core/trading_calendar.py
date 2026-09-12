"""The NSE trading calendar (PROMPTS.md Prompt 1 deliverable 3).

Why this exists
---------------
docs/13 §3 proved the factor windows are *calendar* offsets snapped to trading days, not fixed
bar counts. Everything downstream — window starts (docs/05), the as-of snap-backwards rule
(docs/06 step 1), the historical-date picker (docs/07 ``/meta/trading-days``) — needs one
authoritative answer to "was the NSE open on date D".

Confidence model
----------------
``trading_day.source`` records *how* a row was established, in increasing order of authority:

``weekend``   derived from the day of week; certain.
``holiday``   from the seeded holiday list; only as good as that list.
``derived``   assumed open because nothing said otherwise; the weakest claim in the table.
``bhavcopy``  corroborated by real NSE market data for that date; authoritative.

The seeded list covers only the holidays that are deterministic (fixed Gregorian dates plus
Good Friday). India's lunar-calendar holidays — Holi, Diwali, Dussehra, the Ids, Janmashtami,
Ganesh Chaturthi, Guru Nanak Jayanti and friends — are declared per year by NSE circular and are
**not** in the seed file, because guessing them would put plausible-looking wrong dates into the
spine of every factor window. See ``data/README.md``.

Consequence: a freshly seeded calendar is *provisional*. :func:`reconcile_from_bars` promotes
real trading dates to ``bhavcopy`` and demotes the rest; Prompt 3 must run it across the whole
backfill before any factor is published, and Prompt 5 asserts the recovered window lengths from
docs/13 §3 against it.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final, Literal

TradingDaySource = Literal["weekend", "holiday", "derived", "bhavcopy"]

#: Sources that are provisional: a calendar containing any of these has not been reconciled
#: against real market data and must not be used to publish a factor.
PROVISIONAL_SOURCES: Final[frozenset[str]] = frozenset({"holiday", "derived"})

#: docs/13 §3 — the calendar must reproduce these trading-day counts for as-of 2026-08-18.
#: Asserted by Prompt 5; recorded here so the target is stated next to the calendar itself.
EXPECTED_WINDOW_LENGTHS: Final[dict[int, int]] = {1: 22, 3: 64, 6: 121, 9: 185, 12: 247}

#: Committed holiday CSV name — callers open ``baskfy_core.data`` / this file themselves.
HOLIDAY_FILE: Final = "nse_trading_holidays.csv"
_HOLIDAY_FILE: Final = HOLIDAY_FILE  # backwards-compatible alias for importers

#: ``date.weekday()`` values from Saturday onward.
_SATURDAY: Final = 5


@dataclass(frozen=True, slots=True)
class TradingDayRow:
    """One calendar day, ready to be upserted into ``trading_day``."""

    date: dt.date
    is_trading_day: bool
    holiday_name: str | None
    source: TradingDaySource


def parse_seed_holidays(text: str) -> dict[dt.date, str]:
    """Parse a holiday CSV body. Keys are dates; values are the holiday name.

    The package no longer opens ``baskfy_core.data`` itself (Law 1 / AF 3.10): the caller —
    seed job, test, or worker — reads the committed file and hands the text in.
    """
    holidays: dict[dt.date, str] = {}
    for row in csv.DictReader(text.splitlines()):
        holidays[dt.date.fromisoformat(row["date"])] = row["name"]
    return holidays


def iter_days(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    """Every calendar day in the inclusive range ``[start, end]``."""
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def build_calendar(
    start: dt.date,
    end: dt.date,
    holidays: Mapping[dt.date, str],
) -> list[TradingDayRow]:
    """Materialise every calendar day in ``[start, end]``.

    Every day gets a row — including weekends and holidays — so that "not a trading day" and
    "outside the loaded range" are distinguishable, and so snapping is an indexed lookup rather
    than a loop over an unbounded gap.
    """
    rows: list[TradingDayRow] = []
    for day in iter_days(start, end):
        if day.weekday() >= _SATURDAY:
            rows.append(TradingDayRow(day, False, None, "weekend"))
        elif (name := holidays.get(day)) is not None:
            rows.append(TradingDayRow(day, False, name, "holiday"))
        else:
            rows.append(TradingDayRow(day, True, None, "derived"))
    return rows


def default_calendar_range(today: dt.date) -> tuple[dt.date, dt.date]:
    """Prompt 1 deliverable 3: "2011..current year", inclusive of the whole current year."""
    return dt.date(2011, 1, 1), dt.date(today.year, 12, 31)


def is_provisional(rows: list[TradingDayRow]) -> bool:
    """True while any row still rests on the seed list rather than on real market data."""
    return any(row.source in PROVISIONAL_SOURCES for row in rows)


def reconcile(
    rows: list[TradingDayRow], dates_with_bars: frozenset[dt.date]
) -> list[TradingDayRow]:
    """Promote every date that has real NSE bars to the authoritative ``bhavcopy`` source.

    A date with bars was unambiguously a trading day, whatever the seed list claimed. The
    reverse is *not* inferred here: an absent bar can mean a holiday or merely a gap in the
    backfill, so weekday dates without bars keep whatever they had. Prompt 3 owns the
    "the whole universe has no bar on D, therefore D was a holiday" judgement, which needs the
    universe-wide view this pure function does not have.
    """
    reconciled: list[TradingDayRow] = []
    for row in rows:
        if row.date in dates_with_bars:
            reconciled.append(TradingDayRow(row.date, True, None, "bhavcopy"))
        else:
            reconciled.append(row)
    return reconciled


def snap_backward(day: dt.date, trading_days: frozenset[dt.date], *, limit: int = 14) -> dt.date:
    """The as-of rule (docs/06 step 1): a non-trading date snaps to the previous trading day."""
    for offset in range(limit + 1):
        candidate = day - dt.timedelta(days=offset)
        if candidate in trading_days:
            return candidate
    raise ValueError(f"no trading day within {limit} days before {day}")


def snap_forward(day: dt.date, trading_days: frozenset[dt.date], *, limit: int = 14) -> dt.date:
    """The window-start rule (docs/05, docs/13 §3): window starts snap forward."""
    for offset in range(limit + 1):
        candidate = day + dt.timedelta(days=offset)
        if candidate in trading_days:
            return candidate
    raise ValueError(f"no trading day within {limit} days after {day}")
