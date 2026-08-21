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
from typing import Iterable, Mapping, Sequence


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

    # THE FAR-DATED GATE. The bucket table tops out at "3+", which was written for a weekly
    # cycle where the furthest session is four days from expiry. A monthly series — every
    # BANKNIFTY contract since the weeklies were withdrawn — spends most of its life at
    # dte 5..25, and every one of those days lands in "3+" and is handed a 5-point target
    # calibrated for an option about to expire.
    #
    # That is not conservative, it is a category error: intraday decay on a 22-day option is
    # a fraction of a 3-day one, so the same target asks a much slower trade to travel the
    # same distance, while the stop stays at half of it. Rather than invent bucket rows
    # nobody has calibrated, sessions beyond max_dte are simply not taken — which reduces a
    # monthly instrument to the final week, where its behaviour matches the table it is
    # being priced with.
    max_dte = sess.get("max_dte")
    if tradeable and max_dte is not None and dte > int(max_dte):
        tradeable = False
        reason = (f"dte={dte} exceeds max_dte={int(max_dte)}: the session table is "
                  "calibrated to the final week and has no row for a far-dated contract")

    return SessionParams(dte=dte, bucket=bucket, target_points=target,
                         size_mult=float(row["size_mult"]), stop_points=stop,
                         tradeable=tradeable, reason=reason)


DEFAULT_WINDOW_END = dt.time(9, 45)
# The first tick at which an option has a two-sided quote. NFO and BFO have no pre-open
# session, so nothing before this is a price.
OPTIONS_OPEN = dt.time(9, 15)


def entry_window_state(cfg: Mapping | None, now_t: dt.time, *,
                       default_end: dt.time = DEFAULT_WINDOW_END) -> dict:
    """Whether a FIRST entry may still be opened, and at what size.

    THE ONE DEFINITION. Three callers need this answer and each had its own copy: the
    runner deciding whether to enter, --check deciding what to report, and autorun deciding
    whether starting a process is worth it. Copies drift, and the two failure directions
    are both silent — autorun starts a session that immediately refuses, or declines to
    start one that would have traded and the day is simply lost.

    A late entry does not move the target or the stop. stop_to_target_ratio is [STRUCTURAL]
    and a late start does not change what the trade is worth; it changes how much session
    is left to be right in. Size is the honest lever.
    """
    tm = ((cfg or {}).get("timing") or {})
    opens = _hhmm(tm.get("entry_early"), OPTIONS_OPEN)
    end = _hhmm(tm.get("entry_window_end"), default_end)
    cutoff = _hhmm(tm.get("no_new_entry_after"), end)
    late_ok = bool(tm.get("allow_late_entry", False))
    mult = float(tm.get("late_entry_size_mult", 1.0)) if late_ok else 1.0

    # THE WINDOW HAS A FLOOR, and it did not until now. Only `now_t <= end` was checked, so
    # every hour before the open reported "open" — 01:38 and 06:00 alike — and that is the
    # one field answering "may I enter". The bound was not missing from the system, only
    # from here: autorun carried its own OPTIONS_OPEN constant, which is the same split
    # that let the upper bound drift before it was centralised.
    #
    # entry_early is the config's own name for the first tick with a chain to trade. There
    # is no pre-open session for options, so a decision taken before it is priced off a
    # book that does not exist.
    if now_t < opens:
        return {"state": "premarket", "size_mult": 0.0, "deadline": end,
                "veto": f"before {opens.strftime('%H:%M')}; there is no pre-open session "
                        "for options, so there is no chain to price an entry against"}

    if now_t <= end:
        return {"state": "open", "veto": None, "size_mult": 1.0,
                "closes": end.strftime("%H:%M"), "deadline": end}
    if not late_ok:
        return {"state": "closed", "size_mult": 0.0, "deadline": end,
                "veto": f"past {end.strftime('%H:%M')} and late entry is disabled"}
    if now_t > cutoff:
        return {"state": "closed", "size_mult": 0.0, "deadline": cutoff,
                "veto": f"past {cutoff.strftime('%H:%M')}, the last time a new position "
                        "may be opened"}
    return {"state": "late", "veto": None, "size_mult": mult,
            "closes": cutoff.strftime("%H:%M"), "deadline": cutoff}


def _hhmm(text, fallback: dt.time) -> dt.time:
    try:
        h, m = (int(x) for x in str(text).split(":")[:2])
        return dt.time(h, m)
    except (TypeError, ValueError):
        return fallback


def tradeable_sessions_per_cycle(cfg: dict) -> int:
    """How many sessions one expiry cycle actually offers.

    Exposed as a function because every expectancy model needs it and the intuitive answer
    overstates returns badly: only the dte=1 session runs the full target at full size, and
    on a monthly series max_dte discards most of the month outright. A monthly instrument
    is therefore NOT five times a weekly one — it is roughly a quarter of it.
    """
    sess = cfg["session"]
    # 4 when unset, matching the weekly cycle this function has always described: with a
    # Tuesday expiry the tradeable sessions are Wed, Thu, Fri and Mon. Defaulting to 3
    # here silently cut every expectancy model by a quarter for any config predating
    # max_dte.
    span = int(sess["max_dte"]) if sess.get("max_dte") is not None else 4
    n = min(span, 4)                       # dte 1..span, capped at a working week
    return n + (1 if sess.get("allow_expiry_day", False) else 0)


# Kept under the old name: callers outside this module still ask the weekly question.
def tradeable_sessions_per_week(cfg: dict) -> int:
    return tradeable_sessions_per_cycle(cfg)
