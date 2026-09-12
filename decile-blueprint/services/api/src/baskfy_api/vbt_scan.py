"""The VBT sleeve's "Scan now" request — the `/vbt` surface's second write, and its first POST.

`docs/vbt/02` Track C §4 keeps this surface away from the gateway; it does not keep it away from
a **detection**. A scan asks the worker to run the detector, which reads bars and writes
`vb_signal_daily` and `vb_breadth_daily`. It cannot size, place or cancel anything: nothing here
names the execution package, and the task the row is published to (`baskfy.vbt.rescan`,
`baskfy_worker.tasks.vbt_rescan`) has no order path either. `test_vbt_readonly.py` lists
`POST /vbt/scan` as the surface's one POST and asserts this module writes to that one table.

WHAT IT ASKS FOR, AND WHAT IT REFUSES TO ASK FOR
------------------------------------------------
**The latest published session, detected again** — not today. That distinction is VB12's whole
design and it is kept here rather than re-litigated: three of VBT-1's five lines read the day's
volume against its 50-day average, the close's position inside the day's range and the day's
change, and the entry limit *is* the signal bar's close. There is no provisional VBT row and no
column to put one in, which is why `vb_scan_run` has no `provisional` where `sw_scan_run` does.
The worker decides which session that is (`latest_published_session`), because only it can.

TWO REFUSALS, BOTH FROM THE TABLE
---------------------------------
**One in flight per user (409).** A `QUEUED` or `RUNNING` row younger than
`vbt_scan_stale_after_seconds` is a scan on its way; a second press gets its id back rather than
a second detection over the same bars. Older than that and it is a worker that died — the row is
left as it is, and a new one is allowed.

**One a minute (429).** The newest row's `requested_at` inside `vbt_scan_min_interval_seconds` is
"too soon", with `Retry-After`. Answered from the row rather than from a Redis limiter so the
rule holds with no cache configured and is testable against the database alone — the same choice
SW15.1 made, and the same one `app/vbt_desk.py` makes with no Celery client at all.

The row is inserted first and published second: a broker that is down leaves a `QUEUED` row with
no `task_id`, and `vbt-rescan-sweep` publishes it within the minute — the path the desk's button
has always taken.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.problems import Problem, ProblemType
from baskfy_api.queue import TaskQueue
from baskfy_core.models import VbScanRun

log = logging.getLogger(__name__)

#: The worker's `RESCAN_TASK_NAME`, copied for the reason `baskfy_api.swing_scan` copies its own:
#: the dependency runs worker -> api. `test_celery_config.py` holds the routing equal, and this
#: is the task that already exists — a scan queues the detector VB12 shipped, never a second one.
SCAN_TASK_NAME: Final = "baskfy.vbt.rescan"

IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")

#: What goes in the ``source`` **column**, which `vb_scan_run`'s CHECK constraint limits to
#: ``desk`` | ``cli`` (`baskfy_core.models.vbt.VB_SCAN_SOURCES`). A web press is a button press,
#: so it takes the button's bucket — and the precise surface is written into ``detail.source``,
#: exactly the way `sw_scan_run` records it, because `sw_scan_run` has no column at all.
#: Widening the CHECK to a third value is a migration, and this run holds no migration slot
#: (DECISIONS-VB VB12.1). `source_of()` below is what a reader should use; it prefers the detail.
COLUMN_SOURCE: Final = "desk"


@dataclass(frozen=True, slots=True)
class ScanRunView:
    """One `vb_scan_run` row, as `GET /vbt/scan/{run_id}` reads it.

    No `provisional`: see the module docstring. `funnel` is the detector's own counts, lifted out
    of `detail` so a page reads them the same way it reads the nightly's.
    """

    run_id: int
    status: str
    source: str
    requested_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    session_date: dt.date | None
    funnel: dict[str, object] | None
    detail: dict[str, object] | None
    error: str | None


def source_of(row: VbScanRun) -> str:
    """Which surface asked. ``detail.source`` when the writer recorded one, else the column.

    The column is a two-value bucket the CHECK constraint owns; `detail.source` is the precise
    answer and the one the swing book's row carries. Note that the worker **replaces** `detail`
    with the funnel when the run finishes (`baskfy_worker.tasks.vbt_rescan`), so a DONE row falls
    back to the column — the provenance is there while the page is polling, which is when it is
    read. Recorded as a known limit in DECISIONS-VB VB12.1 rather than papered over here.
    """
    detail = row.detail if isinstance(row.detail, dict) else None
    named = detail.get("source") if detail is not None else None
    return named if isinstance(named, str) and named else str(row.source)


def scan_run_view(row: VbScanRun) -> ScanRunView:
    detail = row.detail if isinstance(row.detail, dict) else None
    funnel = detail.get("funnel") if detail is not None else None
    return ScanRunView(
        run_id=int(row.id),
        status=str(row.status),
        source=source_of(row),
        requested_at=row.requested_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        session_date=row.session_date,
        funnel=funnel if isinstance(funnel, dict) else None,
        detail=detail,
        error=row.error,
    )


async def scan_run(session: AsyncSession, *, user_id: int, run_id: int) -> ScanRunView | None:
    """One run of this user's, by id. Somebody else's is `None` — a 404, not a peek."""
    row = (
        await session.execute(
            select(VbScanRun).where(VbScanRun.user_id == user_id, VbScanRun.id == run_id)
        )
    ).scalar_one_or_none()
    return None if row is None else scan_run_view(row)


async def newest_run(session: AsyncSession, *, user_id: int) -> VbScanRun | None:
    return (
        await session.execute(
            select(VbScanRun)
            .where(VbScanRun.user_id == user_id)
            .order_by(VbScanRun.requested_at.desc(), VbScanRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def in_flight(
    session: AsyncSession, *, user_id: int, now: dt.datetime, stale_after: dt.timedelta
) -> VbScanRun | None:
    """The scan on its way, if one is: queued or running, and requested recently enough that a
    live worker could still be on it."""
    rows = (
        await session.execute(
            select(VbScanRun)
            .where(
                VbScanRun.user_id == user_id,
                VbScanRun.status.in_(IN_FLIGHT),
                VbScanRun.requested_at > now - stale_after,
            )
            .order_by(VbScanRun.requested_at.desc(), VbScanRun.id.desc())
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
) -> VbScanRun:
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
    row = VbScanRun(
        user_id=user_id,
        requested_at=now,
        status="QUEUED",
        source=COLUMN_SOURCE,
        detail={"source": source},
    )
    session.add(row)
    await session.flush()
    if queue is None:
        log.warning("vbt scan %s queued with no broker; the worker's sweep will publish it", row.id)
        return row
    try:
        row.task_id = str(queue.send_task(SCAN_TASK_NAME, [int(row.id)]))
    except Exception as exc:  # a broker that is down is not the person's fault
        log.warning("vbt scan %s could not be published (%s); the sweep will", row.id, exc)
    await session.flush()
    return row
