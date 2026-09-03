"""The "Scan now" request — SW15's one write, beside the watchlist's (`swing_watch`).

`docs/swing/02` Track A lets this surface write what "changes no money", and a scan is the
purest case: it asks the worker to run the detectors, which read bars and quotes and write
detection rows. It cannot size, place or cancel anything — nothing here names the execution
package, and the row it writes (`sw_scan_run`) is read by a task that has no order path either
(`baskfy_worker.tasks.swing_scan_now`). `test_swing_readonly.py` lists `POST /swing/scan` as the
fifth money-free write and asserts the module writes to that table alone.

TWO REFUSALS, BOTH FROM THE TABLE
---------------------------------
**One in flight per user (409).** A `QUEUED` or `RUNNING` row younger than
`swing_scan_stale_after_seconds` is a scan on its way; a second press gets its id back rather
than a second run. Older than that and it is a worker that died — the row is left as it is, and
a new one is allowed.

**One a minute (429).** The newest row's `requested_at` inside `swing_scan_min_interval_seconds`
is "too soon", with `Retry-After`. Answered from the row rather than from the API's Redis
limiter so the rule holds with no cache configured and is testable against the database alone
(DECISIONS-SW SW15.1).

The row is inserted first and published second: a broker that is down leaves a `QUEUED` row with
no `task_id`, and the worker's minute sweep publishes it — the same path the desk console's
button takes, which has no Celery client at all.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.problems import Problem, ProblemType
from baskfy_api.queue import TaskQueue
from baskfy_core.models import SwScanRun

log = logging.getLogger(__name__)

#: The worker's `SWING_SCAN_NOW_TASK`, copied for the reason `baskfy_api.admin` copies its two:
#: the dependency runs worker -> api. `test_celery_config.py` holds the routing equal.
SCAN_TASK_NAME: Final = "baskfy.swing.scan_now"

IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")


async def newest_run(session: AsyncSession, *, user_id: int) -> SwScanRun | None:
    return (
        await session.execute(
            select(SwScanRun)
            .where(SwScanRun.user_id == user_id)
            .order_by(SwScanRun.requested_at.desc(), SwScanRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def in_flight(
    session: AsyncSession, *, user_id: int, now: dt.datetime, stale_after: dt.timedelta
) -> SwScanRun | None:
    """The scan on its way, if one is: queued or running, and requested recently enough that
    a live worker could still be on it."""
    rows = (
        await session.execute(
            select(SwScanRun)
            .where(
                SwScanRun.user_id == user_id,
                SwScanRun.status.in_(IN_FLIGHT),
                SwScanRun.requested_at > now - stale_after,
            )
            .order_by(SwScanRun.requested_at.desc(), SwScanRun.id.desc())
            .limit(1)
        )
    ).scalars()
    return next(iter(rows), None)


async def request_scan(  # noqa: PLR0913 - one keyword per rule the request is checked against
    session: AsyncSession,
    *,
    user_id: int,
    now: dt.datetime,
    min_interval: dt.timedelta,
    stale_after: dt.timedelta,
    source: str,
    queue: TaskQueue | None,
) -> SwScanRun:
    """Insert the run and publish it. Raises the 409 / 429 problems; never places anything."""
    running = await in_flight(session, user_id=user_id, now=now, stale_after=stale_after)
    if running is not None:
        raise Problem(
            ProblemType.SCAN_IN_FLIGHT,
            f"Scan {running.id} is {running.status.lower()}; its result is on its way.",
            run_id=int(running.id),
            status_of_run=str(running.status),
        )
    newest = await newest_run(session, user_id=user_id)
    if newest is not None and newest.requested_at > now - min_interval:
        wait = min_interval - (now - newest.requested_at)
        seconds = max(1, int(wait.total_seconds() + 0.999))
        raise Problem(
            ProblemType.RATE_LIMITED,
            f"A scan was requested {int((now - newest.requested_at).total_seconds())} s ago; "
            f"one a minute is the limit. Try again in {seconds} s.",
            headers={"Retry-After": str(seconds)},
            retry_after=seconds,
            run_id=int(newest.id),
        )
    row = SwScanRun(user_id=user_id, requested_at=now, status="QUEUED", detail={"source": source})
    session.add(row)
    await session.flush()
    if queue is None:
        log.warning("scan now %s queued with no broker; the worker's sweep will publish it", row.id)
        return row
    try:
        row.task_id = str(queue.send_task(SCAN_TASK_NAME, [int(row.id)]))
    except Exception as exc:  # a broker that is down is not the person's fault
        log.warning("scan now %s could not be published (%s); the sweep will", row.id, exc)
    await session.flush()
    return row
