"""Market calendar, expiry resolution and session parameters.

EVERYTHING KEYS OFF trading_days_to_expiry, NEVER OFF A WEEKDAY NAME.
NIFTY weeklies moved from Thursday to Tuesday, and they will move again. A rule written
as "if today is Monday" is correct for as long as nobody changes the calendar, which is
not a property any exchange offers.

The expiry must also be re-resolved for the session being asked about, not resolved once
and reused. Pinning the nearest expiry and then asking about later dates makes every one
of them come back dte=0 — the expiry-day bucket, the one with the most gamma risk and the
tightest stop. That failure was reproduced on this codebase before this module existed.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Sequence


class ExpiryResolutionError(RuntimeError):
    """The expiry series could not be resolved for a session. Never fall through."""


@dataclass(frozen=True)
class SessionParams:
    dte: int
    bucket: str
    target_points: float
    size_mult: float
    stop_points: float
    tradeable: bool
    reason: str = ""


def is_trading_day(day: dt.date, holidays: Iterable[dt.date] = ()) -> bool:
    return day.weekday() < 5 and day not in set(holidays)


def trading_days_between(start: dt.date, end: dt.date,
                         holidays: Iterable[dt.date] = ()) -> int:
    """Sessions strictly after `start` up to and including `end`.

    start == end gives 0: expiry day itself is dte=0, not dte=1.
    """
    if end < start:
        return -1
    hol = set(holidays)
    n, cur = 0, start
    while cur < end:
        cur += dt.timedelta(days=1)
        if is_trading_day(cur, hol):
            n += 1
    return n


def resolve_expiry(session: dt.date, expiries: Sequence[dt.date],
                   holidays: Iterable[dt.date] = ()) -> dt.date:
    """The weekly expiry that governs `session`: the first one not before it.

    Takes the session date as an argument precisely so it cannot be resolved once and
    reused for a later day.
    """
    later = sorted(e for e in expiries if e >= session)
    if not later:
        raise ExpiryResolutionError(
            f"no expiry on or after {session} in a list of {len(expiries)} — the "
            "instrument dump is stale, and a stale series mis-prices every strike")
    return later[0]


def expiry_is_expected_weekday(expiry: dt.date, weekday: str = "TUESDAY",
                               holidays: Iterable[dt.date] = ()) -> bool:
    """True when the expiry falls on the configured weekday, or is the trading day before
    it because that weekday was a holiday."""
    want = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
            "SATURDAY", "SUNDAY"].index(weekday.upper())
    if expiry.weekday() == want:
        return True
    hol = set(holidays)
    probe = expiry + dt.timedelta(days=1)
    for _ in range(6):                       # walk forward to the intended weekday
        if probe.weekday() == want:
            return probe in hol or not is_trading_day(probe, hol)
        probe += dt.timedelta(days=1)
    return False


def session_params(session: dt.date, expiry: dt.date, cfg: dict,
                   holidays: Iterable[dt.date] = ()) -> SessionParams:
    """Target, size multiplier and stop for one session.

    stop is ALWAYS target * stop_to_target_ratio. That ratio is the strategy's edge and is
    read from config only so it is visible, never so it can be fitted.
    """
    dte = trading_days_between(session, expiry, holidays)
    sess = cfg["session"]
    if dte < 0:
        if sess.get("reject_negative_dte", True):
            raise ExpiryResolutionError(
                f"dte={dte} for session {session} against expiry {expiry}: the series is "
                "behind the session. Never fall through to the expiry-day bucket — it has "
                "the loosest target and the highest gamma.")
        dte = 0

    bucket = "3+" if dte >= 3 else str(dte)
    row = sess["by_days_to_expiry"][bucket]
    target = float(row["target_points"])
    stop = target * float(sess["stop_to_target_ratio"])

    tradeable, reason = True, ""
    if dte == 0 and not sess.get("allow_expiry_day", False):
        tradeable, reason = False, "expiry day disabled (allow_expiry_day=false)"

    return SessionParams(dte=dte, bucket=bucket, target_points=target,
                         size_mult=float(row["size_mult"]), stop_points=stop,
                         tradeable=tradeable, reason=reason)


def tradeable_sessions_per_week(cfg: dict) -> int:
    """Wed/Thu/Fri/Mon with a Tuesday expiry and expiry-day trading off.

    Exposed as a function because every expectancy model needs it and the intuitive
    answer (5, or 20/month) overstates returns by roughly 2.5x: only the dte=1 session
    runs the full target at full size.
    """
    return 4 if not cfg["session"].get("allow_expiry_day", False) else 5
