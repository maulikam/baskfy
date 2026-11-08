"""The desk container's clock for same-day fill capture.

Beat used to fire ``baskfy.desk.daily`` and ``baskfy.desk.autorun`` on the ``baskfy-py``
worker. That image does not contain this tree, so both entries failed every weekday
(NEEDS-MAULIK §33). Maulik, 13 Sep 2026: this process, in the ``desk-daily`` compose
service on the ``baskfy-desk`` image, is the scheduler.

NOTHING HERE PLACES AN ORDER. It launches ``scripts.daily`` and ``scripts.autorun``,
which are reads from Kite and writes to the desk's own database. The order path is
``packages/execution``; this module does not import it.

Schedule (IST, Mon-Fri), matching ``momentum-daily.timer`` and the retired Beat entries:

    18:30  python -m scripts.daily --quiet --source schedule
    18:50  python -m scripts.autorun

Persistent=true of the systemd timer is preserved: a container that comes up after 18:30
on a weekday still owes today's slots once. Both jobs are independently idempotent, so a
restart after a successful run collects nothing extra. Weekends never catch up Friday —
Kite flushes ``/trades`` overnight, and a Saturday run cannot recover Friday's fills.
"""

from __future__ import annotations

import calendar
import datetime as dt
import subprocess
import sys
import time
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

DAILY_AT = dt.time(18, 30)
AUTORUN_AT = dt.time(18, 50)

#: After a job, sit still long enough that the next ``next_job`` cannot pick the same
#: slot again on the same wall-clock minute. Same 90s the swing-monitor loop uses.
SETTLE_SECONDS = 90


@dataclass(frozen=True)
class Slot:
    """One collection job and the IST clock it belongs to."""

    name: str
    at: dt.time
    argv: tuple[str, ...]


SLOTS: tuple[Slot, ...] = (
    Slot("daily", DAILY_AT, ("-m", "scripts.daily", "--quiet", "--source", "schedule")),
    Slot("autorun", AUTORUN_AT, ("-m", "scripts.autorun",)),
)


def next_weekday_at(now: dt.datetime, at: dt.time) -> dt.datetime:
    """The next Mon-Fri wall-clock ``at`` strictly after ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    run = dt.datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    if run <= now:
        run += dt.timedelta(days=1)
    while run.weekday() > calendar.FRIDAY:
        run += dt.timedelta(days=1)
    return run


def due_now(now: dt.datetime, at: dt.time, *, already_ran: bool) -> bool:
    """True when this weekday's slot has arrived and has not run in this process.

    After the slot's clock on a weekday, a restart still owes the day (the timer's
    ``Persistent=true``). A weekend does not catch up Friday.
    """
    if already_ran or now.weekday() > calendar.FRIDAY:
        return False
    slot = dt.datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    return now >= slot


def next_job(
    now: dt.datetime,
    *,
    ran: frozenset[tuple[dt.date, str]] = frozenset(),
) -> tuple[Slot, dt.datetime]:
    """The next (slot, when) to fire.

    Catch-up slots return ``when == now``. When daily and autorun are both due, daily
    runs first (18:30 before 18:50).
    """
    candidates: list[tuple[dt.datetime, dt.time, Slot]] = []
    for slot in SLOTS:
        already = (now.date(), slot.name) in ran
        when = now if due_now(now, slot.at, already_ran=already) else next_weekday_at(now, slot.at)
        candidates.append((when, slot.at, slot))
    candidates.sort(key=lambda row: (row[0], row[1]))
    when, _at, slot = candidates[0]
    return slot, when


def _argv(slot: Slot) -> list[str]:
    return [sys.executable, *slot.argv]


def run_forever(
    *,
    now_fn: Callable[[], dt.datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    call_fn: Callable[[list[str]], int] | None = None,
    limit: int | None = None,
) -> None:
    """Sleep until the next slot, run it, repeat.

    ``limit`` is a test seam. Production passes none and never returns.
    """
    now = now_fn or (lambda: dt.datetime.now(IST))
    sleep = sleep_fn or time.sleep
    call = call_fn or (lambda argv: int(subprocess.call(argv)))
    ran: set[tuple[dt.date, str]] = set()
    fired = 0
    while True:
        current = now()
        slot, when = next_job(current, ran=frozenset(ran))
        wait = max(0.0, (when - now()).total_seconds())
        print(
            f"desk-daily: next {slot.name} {when:%a %Y-%m-%d %H:%M} IST (wait {wait:.0f}s)",
            flush=True,
        )
        if wait:
            sleep(wait)
        rc = call(_argv(slot))
        print(f"desk-daily: {slot.name} exited {rc}", flush=True)
        ran.add((now().date(), slot.name))
        sleep(float(SETTLE_SECONDS))
        fired += 1
        if limit is not None and fired >= limit:
            return


def main() -> int:
    """``python -m scripts.desk_daily_loop`` — the compose service's command."""
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
