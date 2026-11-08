"""The ``twt-auto`` container's clock: ``python -m app.twt_auto`` once a weekday, at the open.

TW17 (Maulik, in session, 21 Sep 2026). The sibling of ``swing-monitor-loop`` and
``scripts.desk_daily_loop``, with the schedule in a module so a test can import it.

    09:15:10 IST, Mon-Fri    python -m app.twt_auto

``twt-morning`` (Beat, 09:05) builds the MORNING plan and it expires thirty minutes later, so the
window in which a run can do anything is 09:15-09:35. A container that comes up **inside** that
window on a weekday runs at once rather than forfeiting the day; one that comes up after it waits
for tomorrow — the plan has expired and ``app.twt_auto`` would refuse it anyway.

THIS LOOP DECIDES NOTHING. ``app.twt_auto`` exits 0 without reading a plan unless all three flags
(DRY_RUN off, ``BASKFY_TWT_EXECUTION_ENABLED``, ``BASKFY_TWT_AUTO_EXECUTE``) are on, so the loop
runs it every weekday regardless and the module is where the answer lives. With the flag off the
container logs one line a day and idles.
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

#: Ten seconds past the open: the first prints are in and a MARKET order is an open-session order.
RUN_AT = dt.time(9, 15, 10)
#: The end of the window a late start may still join: the 09:05 plan's thirty-minute expiry.
JOIN_UNTIL = dt.time(9, 35)
#: After a run, sit still long enough that ``next_run`` cannot pick today again.
SETTLE_SECONDS = 90

ARGV: tuple[str, ...] = ("-m", "app.twt_auto")


def next_run(now: dt.datetime, *, ran_today: bool = False) -> dt.datetime:
    """The next moment to run. ``now`` itself when a weekday's window is open and unspent."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(IST)
    run = dt.datetime.combine(local.date(), RUN_AT, tzinfo=IST)
    if local.weekday() <= calendar.FRIDAY and not ran_today:
        if local < run:
            return run
        if local.time() < JOIN_UNTIL:
            return local
    run += dt.timedelta(days=1)
    while run.weekday() > calendar.FRIDAY:
        run += dt.timedelta(days=1)
    return run


def run_forever(
    *,
    now_fn: Callable[[], dt.datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    call_fn: Callable[[list[str]], int] | None = None,
    limit: int | None = None,
) -> None:
    """Sleep until the next run, run it, repeat. ``limit`` is a test seam."""
    now = now_fn or (lambda: dt.datetime.now(IST))
    sleep = sleep_fn or time.sleep
    call = call_fn or (lambda argv: int(subprocess.call(argv)))
    last: dt.date | None = None
    fired = 0
    while True:
        current = now()
        when = next_run(current, ran_today=last == current.astimezone(IST).date())
        wait = max(0.0, (when - now()).total_seconds())
        print(f"twt-auto: next run {when:%a %Y-%m-%d %H:%M:%S} IST (wait {wait:.0f}s)", flush=True)
        if wait:
            sleep(wait)
        rc = call([sys.executable, *ARGV])
        print(f"twt-auto: app.twt_auto exited {rc}", flush=True)
        last = now().astimezone(IST).date()
        sleep(float(SETTLE_SECONDS))
        fired += 1
        if limit is not None and fired >= limit:
            return


def main() -> int:
    """``python -m scripts.twt_auto_loop`` — the compose service's command."""
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
