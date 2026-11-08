"""The FO monitor's day (``docs/fno/06`` FO7): a tick every 60 s through the session, then the
night's marks, retried until the bhavcopy lands.

    09:15-15:30  FnoMonitor.tick(now) every TICK_SECONDS (plans at 09:20, exits on live marks)
    15:30-23:30  FnoMonitor.nightly(day) every NIGHT_RETRY_SECONDS until the ingest has landed

``04`` §4: the F&O bhavcopy task runs at 18:30 and retries hourly to 23:30; a session whose file
never lands is marked on a later night (``nightly`` catches up every unmarked session), never
interpolated. The clock decides nothing — every verdict is the monitor's, every order the gateway's.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("desk.fno_clock")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)
NIGHT_END = dt.time(23, 30)
TICK_SECONDS = 60.0
NIGHT_RETRY_SECONDS = 900.0


class _Night(Protocol):
    landed: bool


class Monitor(Protocol):
    async def tick(self, now: dt.datetime) -> object: ...

    async def nightly(self, day: dt.date) -> _Night: ...


@dataclass
class DayReport:
    ticks: int = 0
    nights: int = 0
    marked: bool = False
    errors: list[str] = field(default_factory=list)


def ist_now() -> dt.datetime:
    return dt.datetime.now(tz=IST)


async def run_day(
    monitor: Monitor,
    *,
    now: Callable[[], dt.datetime] = ist_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DayReport:
    """One trading day. A tick that raises is logged and the next minute tries again: a monitor
    that stops on one bad quote leaves an overnight book unwatched."""
    report = DayReport()
    day = now().astimezone(IST).date()
    while (current := now().astimezone(IST)).time() < SESSION_CLOSE:
        if current.time() >= SESSION_OPEN:
            try:
                await monitor.tick(current)
            except Exception as exc:
                log.exception("FO tick at %s failed", current)
                report.errors.append(f"tick {current:%H:%M}: {type(exc).__name__}: {exc}")
            report.ticks += 1
            await sleep(TICK_SECONDS)
        else:
            opening = dt.datetime.combine(day, SESSION_OPEN, tzinfo=IST)
            await sleep(max(1.0, (opening - current).total_seconds()))
    while now().astimezone(IST).time() < NIGHT_END:
        report.nights += 1
        try:
            night = await monitor.nightly(day)
        except Exception as exc:
            log.exception("FO night for %s failed", day)
            report.errors.append(f"night: {type(exc).__name__}: {exc}")
        else:
            if night.landed:
                report.marked = True
                return report
        await sleep(NIGHT_RETRY_SECONDS)
    return report
