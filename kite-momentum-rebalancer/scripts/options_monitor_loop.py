"""The ``options-monitor`` container's clock: ``python -m app.options_monitor`` each weekday session.

OP15 (``docs/options/06``). The sibling of ``swing-monitor-loop`` and ``scripts.twt_auto_loop``,
with the schedule in a module so a test can import it.

    09:14 IST, Mon-Fri    python -m app.options_monitor   (runs until the 15:30 close)

A container that comes up **inside** the session on a weekday starts the monitor at once: it
resumes from ``op_position`` (OP9) and raises whatever the clock already says, including a passed
hard exit. If the monitor exits non-zero inside the session it is started again after
``RETRY_SECONDS``, because an open position with no monitor is the one state the book must not
sit in. A clean exit, or any exit after the close, ends the day.

THIS LOOP DECIDES NOTHING and holds no gateway. ``app.options_monitor`` exits 0 at once unless
``BASKFY_OPTIONS_MONITOR_ENABLED`` is true, and the monitor raises exit plans, never orders. The
exit sweep that closes a raised exit goes through the desk's gateway, under every flag in force.
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

#: A minute before the open, so the first index tick is subscribed when it prints.
RUN_AT = dt.time(9, 14)
#: The session's end: a start after it waits for tomorrow.
SESSION_END = dt.time(15, 30)
#: After a crash inside the session, wait this long and start again (restart resume, OP9).
RETRY_SECONDS = 30

ARGV: tuple[str, ...] = ("-m", "app.options_monitor")


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
        print(f"options-monitor: next start {when:%a %Y-%m-%d %H:%M} IST (wait {wait:.0f}s)",
              flush=True)  # fmt: skip
        if wait:
            sleep(wait)
        rc = call([sys.executable, *ARGV])
        print(f"options-monitor: app.options_monitor exited {rc}", flush=True)
        fired += 1
        if rc != 0 and in_session(now()):
            sleep(float(RETRY_SECONDS))  # restart resume: the day is not spent
        else:
            last = now().astimezone(IST).date()
        if limit is not None and fired >= limit:
            return


def main() -> int:
    """``python -m scripts.options_monitor_loop`` — the compose service's command."""
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
