"""A compose-ready clock for the FO book: ``python -m app.fno_monitor`` each weekday (FO7).

The sibling of ``scripts.options_monitor_loop`` (OP15), with the schedule in a module so a test
can import it. FO12 wires it into compose; this module only has to be ready.

    09:14 IST, Mon-Fri    python -m app.fno_monitor   (ticks to 15:30, then marks to 23:30)

The FO book carries positions overnight, so its process outlives the session: after the close it
waits for the F&O bhavcopy and writes the night's ``fo_mark`` rows and F2's trail. A container that
comes up inside that window on a weekday starts the monitor at once — it resumes from
``fo_position`` and raises whatever the clock already says, including a passed hard exit
(``LATE_EXIT``). A non-zero exit inside the window is started again after ``RETRY_SECONDS``: an
open overnight position with no monitor is the one state the book must not sit in. A clean exit,
or any exit after 23:30, ends the day.

THIS LOOP DECIDES NOTHING and holds no gateway. ``app.fno_monitor`` exits 0 at once unless
``BASKFY_FNO_MONITOR_ENABLED`` is true; the monitor raises plans and sends exits and rolls under
the entry's confirm, never an entry.
"""

from __future__ import annotations

import calendar
import datetime as dt
import subprocess
import sys
import time
import zoneinfo
from collections.abc import Callable

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

#: A minute before the open.
RUN_AT = dt.time(9, 14)
#: The night's end (``04`` §4: the bhavcopy retries to 23:30): a start after it waits for tomorrow.
SESSION_END = dt.time(23, 30)
#: After a crash inside the window, wait this long and start again (restart resume).
RETRY_SECONDS = 30

ARGV: tuple[str, ...] = ("-m", "app.fno_monitor")


def next_run(now: dt.datetime, *, ran_today: bool = False) -> dt.datetime:
    """The next moment to start. ``now`` itself inside a weekday's session not yet run."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(IST)
    run = dt.datetime.combine(local.date(), RUN_AT, tzinfo=IST)
    if local.weekday() <= calendar.FRIDAY and not ran_today:
        if local < run:
            return run
        if local.time() < SESSION_END:
            return local
    run += dt.timedelta(days=1)
    while run.weekday() > calendar.FRIDAY:
        run += dt.timedelta(days=1)
    return run


def in_session(now: dt.datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() <= calendar.FRIDAY and RUN_AT <= local.time() < SESSION_END


def run_forever(
    *,
    now_fn: Callable[[], dt.datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    call_fn: Callable[[list[str]], int] | None = None,
    limit: int | None = None,
) -> None:
    """Sleep until the next start, run the monitor, repeat. ``limit`` is a test seam."""
    now = now_fn or (lambda: dt.datetime.now(IST))
    sleep = sleep_fn or time.sleep
    call = call_fn or (lambda argv: int(subprocess.call(argv)))
    last: dt.date | None = None
    fired = 0
    while True:
        current = now()
        when = next_run(current, ran_today=last == current.astimezone(IST).date())
        wait = max(0.0, (when - now()).total_seconds())
        print(f"fno-monitor: next start {when:%a %Y-%m-%d %H:%M} IST (wait {wait:.0f}s)",
              flush=True)  # fmt: skip
        if wait:
            sleep(wait)
        rc = call([sys.executable, *ARGV])
        print(f"fno-monitor: app.fno_monitor exited {rc}", flush=True)
        fired += 1
        if rc != 0 and in_session(now()):
            sleep(float(RETRY_SECONDS))  # restart resume: the day is not spent
        else:
            last = now().astimezone(IST).date()
        if limit is not None and fired >= limit:
            return


def main() -> int:
    """``python -m scripts.fno_monitor_loop`` — the command FO12 gives the compose service."""
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
