"""Monthly expiries, entry and hard-exit sessions, the entry window (``04`` §1, ``03`` §4).

Two sources and no arithmetic on weekdays. **Expiries come from the NFO master** (the listed
``expiry`` values for an underlying); the monthly is the last listed expiry of its calendar month,
so a holiday that moves BANKNIFTY's November monthly to Monday 23 Nov 2026 (FO0 b) moves the
answer. **Sessions come from ``trading_day``**, passed in as a sorted sequence of exchange dates;
"N sessions before" is an index step on that list, so a holiday inside the count is skipped, not
counted.

Pure: the caller supplies the master's expiries, the calendar and ``now``.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections.abc import Iterable, Sequence

from baskfy_core.fno.config import CommonConfig, F1Config


def monthly_expiries(expiries: Iterable[dt.date]) -> tuple[dt.date, ...]:
    """The last listed expiry of each calendar month, ascending (``04`` §1: "never a weekday
    rule")."""
    last: dict[tuple[int, int], dt.date] = {}
    for day in sorted(set(expiries)):
        last[(day.year, day.month)] = day
    return tuple(sorted(last.values()))


def monthly_expiry(expiries: Iterable[dt.date], year: int, month: int) -> dt.date | None:
    """The monthly of ``year``-``month``, or ``None`` if the master lists nothing that month."""
    inside = [d for d in set(expiries) if (d.year, d.month) == (year, month)]
    return max(inside) if inside else None


def _index(sessions: Sequence[dt.date], day: dt.date) -> int | None:
    i = bisect.bisect_left(sessions, day)
    return i if i < len(sessions) and sessions[i] == day else None


def sessions_before(sessions: Sequence[dt.date], anchor: dt.date, n: int) -> dt.date | None:
    """The exchange session ``n`` sessions before ``anchor`` (``anchor`` itself is 0).

    ``None`` when ``anchor`` is not a session of the calendar or the calendar does not reach back
    that far — never a guessed date.
    """
    if n < 0:
        raise ValueError("n counts sessions backwards and cannot be negative")
    i = _index(sessions, anchor)
    if i is None or i - n < 0:
        return None
    return sessions[i - n]


def sessions_between(sessions: Sequence[dt.date], start: dt.date, end: dt.date) -> int | None:
    """How many sessions ``end`` lies after ``start`` (both must be sessions)."""
    i, j = _index(sessions, start), _index(sessions, end)
    if i is None or j is None:
        return None
    return j - i


def entry_session(sessions: Sequence[dt.date], expiry: dt.date, n: int) -> dt.date | None:
    """F1's entry day: ``f1_entry_sessions_before`` sessions before the monthly (``04`` §1)."""
    return sessions_before(sessions, expiry, n)


def hard_exit_session(sessions: Sequence[dt.date], expiry: dt.date, n: int) -> dt.date | None:
    """``fo_plan.hard_exit_date = E - n`` in sessions (``03`` §4). ``n`` is never zero."""
    if n < 1:
        raise ValueError("fo_hard_exit_before_expiry is never zero (02 §2.2)")
    return sessions_before(sessions, expiry, n)


def hard_exit_at(
    sessions: Sequence[dt.date], expiry: dt.date, common: CommonConfig
) -> dt.datetime | None:
    """15:00 IST (naive) on ``E - fo_hard_exit_before_expiry``."""
    day = hard_exit_session(sessions, expiry, common.hard_exit_before_expiry)
    return None if day is None else dt.datetime.combine(day, common.hard_exit_time)


def is_late_exit(hard_exit_date: dt.date, today: dt.date) -> bool:
    """A position still open after its hard-exit date is exited at the next open as
    ``LATE_EXIT`` (``04`` §1) — a rule violation for the paper checklist."""
    return today > hard_exit_date


def next_entry(
    today: dt.date, expiries: Iterable[dt.date], sessions: Sequence[dt.date], n: int
) -> tuple[dt.date, dt.date] | None:
    """The first ``(entry session, monthly expiry)`` with entry on or after ``today``.

    For the scan's ``NOT_ENTRY_DAY`` "with the next entry date" (``04`` §8). ``None`` when the
    master or the calendar does not reach far enough.
    """
    for expiry in monthly_expiries(expiries):
        entry = entry_session(sessions, expiry, n)
        if entry is not None and entry >= today:
            return entry, expiry
    return None


def is_entry_day(
    today: dt.date, expiries: Iterable[dt.date], sessions: Sequence[dt.date], n: int
) -> dt.date | None:
    """The monthly expiry whose entry day is ``today``, or ``None``."""
    found = next_entry(today, expiries, sessions, n)
    return found[1] if found is not None and found[0] == today else None


def plan_expires_at(issued: dt.datetime, f1: F1Config, common: CommonConfig) -> dt.datetime:
    """``expires_at = min(issued + 30 min, 10:30)`` on the issue date (``04`` §1)."""
    ttl = issued + dt.timedelta(minutes=common.plan_ttl_minutes)
    window_end = dt.datetime.combine(issued.date(), f1.entry_window_end, tzinfo=issued.tzinfo)
    return min(ttl, window_end)


def in_entry_window(now: dt.datetime, f1: F1Config) -> bool:
    """09:20 ≤ ``now`` < 10:30 (``04`` §1). A missed window is ``LAPSED``, never a retry."""
    return f1.plan_time <= now.time() < f1.entry_window_end


def held_expiry(previous_session: dt.date, expiries: Iterable[dt.date]) -> dt.date | None:
    """The nearest expiry **strictly after** the previous session (``04`` §4)."""
    later = sorted(d for d in set(expiries) if d > previous_session)
    return later[0] if later else None


def f2_contract_expiry(
    day: dt.date, expiries: Iterable[dt.date], sessions: Sequence[dt.date], min_sessions: int
) -> dt.date | None:
    """F2 buys the near month, or the next month when the near one expires within
    ``min_sessions`` sessions of ``day`` (``01`` §1b)."""
    ahead = [d for d in monthly_expiries(expiries) if d > day]
    for expiry in ahead:
        gap = sessions_between(sessions, day, expiry)
        if gap is not None and gap > min_sessions:
            return expiry
    return None


def roll_session(sessions: Sequence[dt.date], expiry: dt.date, n: int) -> dt.date | None:
    """F2's roll day, ``E - f2_roll_before_expiry`` at 15:00 (``04`` §10)."""
    if n < 1:
        raise ValueError("f2_roll_before_expiry is at least one session")
    return sessions_before(sessions, expiry, n)
