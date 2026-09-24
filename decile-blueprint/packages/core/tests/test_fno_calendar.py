"""``docs/fno/04`` §1 — monthly expiry from the master, sessions from ``trading_day``.

The load-bearing fixture is FO0 (b): BANKNIFTY's November 2026 monthly is **Monday 23 Nov**
because the Tuesday is a holiday. A weekday rule (the last Tuesday) must get it wrong, or the
fixture proves nothing.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as dt

import pytest

from baskfy_core.fno.calendar import (
    entry_session,
    f2_contract_expiry,
    hard_exit_at,
    hard_exit_session,
    held_expiry,
    in_entry_window,
    is_entry_day,
    is_late_exit,
    monthly_expiries,
    monthly_expiry,
    next_entry,
    plan_expires_at,
    roll_session,
    sessions_before,
    sessions_between,
)
from baskfy_core.fno.config import CommonConfig, F1Config

#: BANKNIFTY monthlies in Kite's master (FO0 b), plus a weekly to prove it is ignored.
MASTER = (
    dt.date(2026, 9, 29),
    dt.date(2026, 10, 20),  # a mid-month listing: not the monthly
    dt.date(2026, 10, 27),
    dt.date(2026, 11, 23),
    dt.date(2026, 12, 29),
)
#: Holidays inside the fixture's calendar (24 Nov is the one that moved the expiry).
HOLIDAYS = {dt.date(2026, 10, 2), dt.date(2026, 10, 21), dt.date(2026, 11, 24)}


def _sessions(start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
    out: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5 and day not in HOLIDAYS:
            out.append(day)
        day += dt.timedelta(days=1)
    return tuple(out)


SESSIONS = _sessions(dt.date(2026, 8, 1), dt.date(2026, 12, 31))


def last_tuesday(year: int, month: int) -> dt.date:
    day = dt.date(year, month, _calendar.monthrange(year, month)[1])
    while day.isoweekday() != 2:
        day -= dt.timedelta(days=1)
    return day


class TestMonthlyComesFromTheMaster:
    def test_the_last_listed_expiry_of_each_month(self) -> None:
        assert monthly_expiries(MASTER) == (
            dt.date(2026, 9, 29),
            dt.date(2026, 10, 27),
            dt.date(2026, 11, 23),
            dt.date(2026, 12, 29),
        )

    def test_the_holiday_monday_is_the_monthly_and_a_weekday_rule_is_wrong(self) -> None:
        assert monthly_expiry(MASTER, 2026, 11) == dt.date(2026, 11, 23)
        assert last_tuesday(2026, 11) != dt.date(2026, 11, 23)

    def test_a_month_with_no_listing_has_no_monthly(self) -> None:
        assert monthly_expiry(MASTER, 2027, 1) is None


class TestSessionArithmetic:
    def test_entry_is_n_sessions_before_skipping_holidays(self) -> None:
        # 23 Nov back 15 sessions: 20,19,18,17,16,13,12,11,10,9,6,5,4,3,2 Nov.
        assert entry_session(SESSIONS, dt.date(2026, 11, 23), 15) == dt.date(2026, 11, 2)
        # October: 21 Oct is a holiday, so the count steps over it.
        entry = entry_session(SESSIONS, dt.date(2026, 10, 27), 15)
        assert entry == dt.date(2026, 10, 5)
        assert sessions_between(SESSIONS, dt.date(2026, 10, 5), dt.date(2026, 10, 27)) == 15

    def test_expiry_itself_is_zero(self) -> None:
        assert sessions_before(SESSIONS, dt.date(2026, 11, 23), 0) == dt.date(2026, 11, 23)

    def test_hard_exit_is_e_minus_one_at_1500(self) -> None:
        assert hard_exit_session(SESSIONS, dt.date(2026, 11, 23), 1) == dt.date(2026, 11, 20)
        assert hard_exit_at(SESSIONS, dt.date(2026, 11, 23), CommonConfig()) == dt.datetime(
            2026, 11, 20, 15, 0
        )

    def test_hard_exit_of_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="never zero"):
            hard_exit_session(SESSIONS, dt.date(2026, 11, 23), 0)

    def test_a_date_off_the_calendar_is_none_not_a_guess(self) -> None:
        assert sessions_before(SESSIONS, dt.date(2026, 11, 24), 1) is None
        assert sessions_before(SESSIONS, dt.date(2026, 8, 3), 5) is None

    def test_next_entry_and_entry_day(self) -> None:
        assert next_entry(dt.date(2026, 10, 6), MASTER, SESSIONS, 15) == (
            dt.date(2026, 11, 2),
            dt.date(2026, 11, 23),
        )
        assert is_entry_day(dt.date(2026, 11, 2), MASTER, SESSIONS, 15) == dt.date(2026, 11, 23)
        assert is_entry_day(dt.date(2026, 11, 3), MASTER, SESSIONS, 15) is None

    def test_late_exit(self) -> None:
        assert is_late_exit(dt.date(2026, 11, 20), dt.date(2026, 11, 23))
        assert not is_late_exit(dt.date(2026, 11, 20), dt.date(2026, 11, 20))


class TestEntryWindow:
    def test_expires_thirty_minutes_after_issue(self) -> None:
        issued = dt.datetime(2026, 11, 2, 9, 20)
        assert plan_expires_at(issued, F1Config(), CommonConfig()) == dt.datetime(
            2026, 11, 2, 9, 50
        )

    def test_but_never_after_1030(self) -> None:
        issued = dt.datetime(2026, 11, 2, 10, 15)
        assert plan_expires_at(issued, F1Config(), CommonConfig()) == dt.datetime(
            2026, 11, 2, 10, 30
        )

    def test_window_bounds(self) -> None:
        f1 = F1Config()
        assert not in_entry_window(dt.datetime(2026, 11, 2, 9, 19), f1)
        assert in_entry_window(dt.datetime(2026, 11, 2, 9, 20), f1)
        assert not in_entry_window(dt.datetime(2026, 11, 2, 10, 30), f1)


class TestHeldAndF2Contracts:
    def test_held_expiry_is_strictly_after_the_previous_session(self) -> None:
        assert held_expiry(dt.date(2026, 9, 28), MASTER) == dt.date(2026, 9, 29)
        assert held_expiry(dt.date(2026, 9, 29), MASTER) == dt.date(2026, 10, 20)

    def test_f2_takes_the_next_month_inside_three_sessions(self) -> None:
        assert f2_contract_expiry(dt.date(2026, 11, 17), MASTER, SESSIONS, 3) == dt.date(
            2026, 11, 23
        )
        # 18 Nov → 23 Nov is 3 sessions: too close, so December.
        assert f2_contract_expiry(dt.date(2026, 11, 18), MASTER, SESSIONS, 3) == dt.date(
            2026, 12, 29
        )

    def test_roll_is_e_minus_n(self) -> None:
        assert roll_session(SESSIONS, dt.date(2026, 11, 23), 1) == dt.date(2026, 11, 20)
        with pytest.raises(ValueError):
            roll_session(SESSIONS, dt.date(2026, 11, 23), 0)
