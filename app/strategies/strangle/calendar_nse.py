"""The NSE trading calendar, DERIVED rather than hardcoded.

Kite exposes no holiday endpoint, so the calendar is inferred from two sources that the
account already provides, and neither is a list somebody typed:

  BACKWARDS, from index history. A weekday with no NIFTY candle is a day the market did not
  trade. This is authoritative — it is the exchange's own record of what happened — and it
  is exact for every day it covers.

  FORWARDS, from the expiry table. Weeklies expire every Tuesday and monthlies on the last
  Tuesday of the month; when that Tuesday is a holiday the expiry shifts to the preceding
  working day. So a listed expiry that is NOT a Tuesday tells you the Tuesday after it was
  a holiday. Checked against the live dump on 17 Aug 2026: 2029-12-24 is a Monday, because
  25 December is Christmas.

WHAT THIS CANNOT DO, stated rather than papered over: forward coverage is limited to
holidays that happen to have shifted a listed expiry. A Thursday holiday three weeks out
moves no expiry and leaves no trace in any Kite endpoint. Those must be added to
`extra_holidays` in config from the NSE circular, and `coverage` reports how far the
derived calendar actually reaches so the gap is visible instead of assumed away.

The failure mode without any of this is not dramatic, which is why it survived four
versions: dte comes out one too high, the session lands in a more conservative bucket, and
nothing raises. The two that DO bite are a legitimately shifted expiry refusing startup,
and the runner trading on a day the exchange is shut.
"""
from __future__ import annotations

import calendar as _calendar
import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

TUESDAY = 1


def last_weekday_of_month(year: int, month: int, weekday: int = TUESDAY) -> dt.date:
    """The last Tuesday of a month — where monthly contracts expire."""
    day = dt.date(year, month, _calendar.monthrange(year, month)[1])
    while day.weekday() != weekday:
        day -= dt.timedelta(days=1)
    return day


def holidays_from_index(rows: Sequence[Mapping], *, start: dt.date | None = None,
                        end: dt.date | None = None) -> set[dt.date]:
    """Weekdays in the covered span with no candle. The exchange's own record."""
    traded = set()
    for r in rows:
        d = r["date"]
        traded.add(d.date() if isinstance(d, dt.datetime) else d)
    if not traded:
        return set()
    start = start or min(traded)
    end = end or max(traded)
    out, day = set(), start
    while day <= end:
        if day.weekday() < 5 and day not in traded:
            out.add(day)
        day += dt.timedelta(days=1)
    return out


def holidays_from_expiries(expiries: Iterable[dt.date],
                           weekday: int = TUESDAY) -> set[dt.date]:
    """Holidays implied by an expiry that shifted off its weekday.

    An expiry on a Monday means the Tuesday that followed it was not a trading day. This is
    the only forward-looking holiday signal Kite carries, and it is a real one.
    """
    out = set()
    for e in expiries:
        if e.weekday() == weekday:
            continue
        probe = e + dt.timedelta(days=1)
        for _ in range(6):
            if probe.weekday() == weekday:
                out.add(probe)
                break
            if probe.weekday() < 5:
                out.add(probe)          # every weekday skipped over was also shut
            probe += dt.timedelta(days=1)
    return out


@dataclass(frozen=True)
class Calendar:
    holidays: frozenset[dt.date] = frozenset()
    derived_from: dt.date | None = None
    derived_to: dt.date | None = None
    inferred: frozenset[dt.date] = frozenset()

    @classmethod
    def build(cls, *, index_rows: Sequence[Mapping] = (),
              expiries: Iterable[dt.date] = (), extra: Iterable[dt.date] = ()) -> "Calendar":
        back = holidays_from_index(index_rows)
        fwd = holidays_from_expiries(expiries)
        extra = {d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))
                 for d in extra}
        days = sorted({(r["date"].date() if isinstance(r["date"], dt.datetime)
                        else r["date"]) for r in index_rows}) if index_rows else []
        return cls(holidays=frozenset(back | fwd | extra),
                   derived_from=days[0] if days else None,
                   derived_to=days[-1] if days else None,
                   inferred=frozenset(fwd))

    def is_trading_day(self, day: dt.date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def trading_days_between(self, start: dt.date, end: dt.date) -> int:
        if end < start:
            return -1
        n, cur = 0, start
        while cur < end:
            cur += dt.timedelta(days=1)
            if self.is_trading_day(cur):
                n += 1
        return n

    def previous_trading_day(self, day: dt.date) -> dt.date:
        probe = day - dt.timedelta(days=1)
        while not self.is_trading_day(probe):
            probe -= dt.timedelta(days=1)
        return probe

    def expiry_is_valid(self, expiry: dt.date, weekday: int = TUESDAY) -> tuple[bool, str]:
        """A Tuesday, or the trading day before a Tuesday that the calendar knows is shut."""
        if expiry.weekday() == weekday:
            return True, "on the expected weekday"
        probe = expiry + dt.timedelta(days=1)
        for _ in range(6):
            if probe.weekday() == weekday:
                if not self.is_trading_day(probe):
                    return True, f"shifted back from {probe}, a holiday"
                return False, (f"{expiry} is a {expiry.strftime('%A')} and {probe} is a "
                               "trading day, so nothing justifies the shift")
            probe += dt.timedelta(days=1)
        return False, f"{expiry} is not near a {_calendar.day_name[weekday]}"

    def coverage(self, through: dt.date | None = None) -> dict:
        """How far the derived calendar actually reaches. Honesty about the forward gap."""
        through = through or dt.date.today()
        forward = sorted(d for d in self.holidays if d > through)
        return {"holidays": len(self.holidays),
                "derived_from": self.derived_from, "derived_to": self.derived_to,
                "exact_through": self.derived_to,
                "inferred_forward": sorted(self.inferred),
                "known_future_holidays": forward,
                "note": ("exact to derived_to from index history; beyond that only "
                         "holidays that shifted a listed expiry are known. A holiday that "
                         "moves no expiry leaves no trace in any Kite endpoint and must "
                         "come from session.extra_holidays.")}


def build_from_kite(kc, *, name: str = "NIFTY", index_token: int = 256265,
                    lookback_days: int = 400, extra: Iterable = (),
                    today: dt.date | None = None) -> Calendar:
    """One index history call plus the instrument dump already in hand."""
    today = today or dt.date.today()
    rows = kc.historical_data(index_token, today - dt.timedelta(days=lookback_days),
                              today, "day")
    expiries = sorted({i["expiry"] for i in kc.instruments("NFO")
                       if i.get("name") == name and i.get("expiry")})
    return Calendar.build(index_rows=rows, expiries=expiries, extra=extra)
