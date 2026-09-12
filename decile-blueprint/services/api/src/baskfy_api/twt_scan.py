"""The TWT hub's "Scan now" request — TW12, and the ``/twt`` surface's **first** write.

``docs/twt/02`` Track A gives the web app a read-only ``/twt`` hub, "the same rule as ``/baskets``,
``/swing`` and ``/vbt``: every mutation on it is a 405 except notes and dismissals, which change no
money." The swing book's equivalent sentence admitted "Scan now" when SW15 built it, for the reason
that a scan asks the worker to run the detectors — which read bars and write detection rows — and
cannot size, place or cancel anything. This module is that same write for this sleeve, and the
widening is recorded as DECISIONS-TW **TW12.3** rather than assumed.

**Track C §4 is untouched.** "``apps/web`` gets no route under ``/twt`` that can reach the
gateway." Nothing here names the execution package or a broker verb; the row it writes
(``tw_scan_run``) is read by a task that has no order path either
(``baskfy_worker.tasks.twt_scan``), and that task calls the nightly's own detector.
``services/api/tests/test_twt_readonly.py`` asserts it over the source and over the OpenAPI
document.

TWO REFUSALS, BOTH FROM THE TABLE
---------------------------------
**One in flight per user (409).** A ``QUEUED`` or ``RUNNING`` row younger than
``twt_scan_stale_after_seconds`` is a scan on its way; a second press gets its id back rather than
a second detection over the same bars. Older than that and it is a worker that died — the row is
left as it is, and a new one is allowed.

**One a minute (429).** The newest row's ``requested_at`` inside ``twt_scan_min_interval_seconds``
is "too soon", with ``Retry-After``. Answered from the row rather than from the API's Redis limiter
so the rule holds with no cache configured and is testable against the database alone — the
argument SW15.1 made, and the reason the desk's copy of this logic can agree with it.

The row is inserted first and published second: a broker that is down leaves a ``QUEUED`` row with
no ``task_id``, and the worker's minute sweep publishes it — the same path the desk console's
button takes, which has no Celery client at all.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.problems import Problem, ProblemType
from baskfy_api.queue import TaskQueue
from baskfy_core.models import TwScanRun

log = logging.getLogger(__name__)

#: The worker's ``baskfy_worker.tasks.twt_scan.SCAN_TASK_NAME``, copied for the reason
#: ``baskfy_api.admin`` and ``baskfy_api.swing_scan`` copy theirs: the dependency runs
#: worker -> api, and importing across it would close a cycle. ``test_celery_config.py`` holds the
#: routing equal, so a copy that drifts fails the build rather than misrouting work.
SCAN_TASK_NAME: Final = "baskfy.twt.scan"

IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")


@dataclasses.dataclass(frozen=True, slots=True)
class ScanRunView:
    """One ``tw_scan_run`` row, as ``GET /twt/scan/{id}`` reads it.

    **There is no ``provisional`` field** and the absence is the design, not an omission: this
    sleeve's signal is read off closed weekly bars, so there is no intraday scan to label
    (DECISIONS-TW **TW12.2**). A page that showed one would be promising a freshness the strategy
    cannot use.
    """

    run_id: int
    status: str
    requested_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    session_date: dt.date | None
    detail: dict[str, object] | None
    error: str | None


def scan_run_view(row: TwScanRun) -> ScanRunView:
    return ScanRunView(
        run_id=int(row.id),
        status=str(row.status),
        requested_at=row.requested_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        session_date=row.session_date,
        detail=dict(row.detail) if isinstance(row.detail, dict) else None,
        error=row.error,
    )


async def newest_run(session: AsyncSession, *, user_id: int) -> TwScanRun | None:
    return (
        await session.execute(
            select(TwScanRun)
            .where(TwScanRun.user_id == user_id)
            .order_by(TwScanRun.requested_at.desc(), TwScanRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def scan_run(session: AsyncSession, *, user_id: int, run_id: int) -> ScanRunView | None:
    """One run by id, **scoped to the user**. ``None`` for somebody else's, so a caller who
    guesses an id learns nothing about whether it exists."""
    row = (
        await session.execute(
            select(TwScanRun).where(TwScanRun.user_id == user_id, TwScanRun.id == run_id)
        )
    ).scalar_one_or_none()
    return None if row is None else scan_run_view(row)


async def in_flight(
    session: AsyncSession, *, user_id: int, now: dt.datetime, stale_after: dt.timedelta
) -> TwScanRun | None:
    """The scan on its way, if one is: queued or running, and requested recently enough that a
    live worker could still be on it."""
    rows = (
        await session.execute(
            select(TwScanRun)
            .where(
                TwScanRun.user_id == user_id,
                TwScanRun.status.in_(IN_FLIGHT),
                TwScanRun.requested_at > now - stale_after,
            )
            .order_by(TwScanRun.requested_at.desc(), TwScanRun.id.desc())
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
) -> TwScanRun:
    """Insert the run and publish it. Raises the 409 / 429 problems; **never places anything**.

    The in-flight check comes first, deliberately: a second press two seconds after the first
    should be told what is happening ("scan 4 is queued") rather than how long to wait, which is
    the less useful of the two true answers. The desk's copy of this logic orders it the same way.
    """
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
    row = TwScanRun(user_id=user_id, requested_at=now, status="QUEUED", source=source)
    session.add(row)
    await session.flush()
    if queue is None:
        log.warning("twt scan %s queued with no broker; the worker's sweep will publish it", row.id)
        return row
    try:
        row.task_id = str(queue.send_task(SCAN_TASK_NAME, [int(row.id)]))
    except Exception as exc:  # a broker that is down is not the person's fault
        log.warning("twt scan %s could not be published (%s); the sweep will", row.id, exc)
    await session.flush()
    return row


__all__ = [
    "IN_FLIGHT",
    "SCAN_TASK_NAME",
    "ScanRunView",
    "in_flight",
    "newest_run",
    "request_scan",
    "scan_run",
    "scan_run_view",
]
