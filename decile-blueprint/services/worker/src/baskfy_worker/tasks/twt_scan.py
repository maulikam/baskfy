"""TW12: the TWT page's **Scan now** button, and the publisher that picks it up.

    tw_scan_run(QUEUED)  ->  publisher picks it up  ->  baskfy.twt.scan  ->  DONE + the funnel

**Why a row and not a request.** The desk console has no Celery client — its venv carries none and
it reaches Baskfy through Postgres alone (`app/twt_desk.py`). So its button writes one
`tw_scan_run` row and the worker's minute publisher picks it up, exactly the path SW15 built for
the swing book and VB12 for VBT-1, and for the same reason. The API publishes directly when it has
a broker to hand, and the publisher is its fallback when it does not.

**The publisher is `baskfy.twt.scan_publish`, not `scan_sweep`, and the two older sleeves do call
theirs a sweep.** On this sleeve "sweep" means :func:`app.twt_execute.sweep_naked` — the 15:15
chore that re-arms GTT stops *through the gateway* — and
``services/worker/tests/test_twt_beat.py::test_the_sweep_is_not_on_a_timer`` refuses any TWT Beat
entry whose name or task carries the word, because a scheduled TWT sweep would be a second
auto-execute exception. That test caught this task under its first name and was right to.
DECISIONS-TW **TW12.4**.

**THIS MODULE CONTAINS NO DETECTOR.** It claims a row, calls
:func:`baskfy_worker.tasks.twt.detect_session` — the *same* function ``baskfy.twt.detect`` calls —
and records what it answered. A second detector would be a second place for the tight-state rule
to live, and the two would drift the first time a threshold moved. ``gates/twt-scan-now.md`` G9 is
the assertion: this file names ``detect_session`` and defines no detection of its own.

``force=True`` is the one argument the nightly does not pass, and it is the point of the button.
The nightly's rule is "detect unless this session already has a breadth row" (DECISIONS-TW TW4.3);
a person pressing **Scan now** is asking for exactly the case that rule skips — the night a
threshold changed, or the night the chain's step was refused by the quality gate. Detection is
idempotent per ``(user_id, date)`` (house rule 7), so the re-run overwrites its own rows and moves
no counter.

**WHAT IT RE-DETECTS, AND WHAT IT REFUSES TO.** A session that has already been **published**.
This is not the swing book's scan: `docs/twt/04` §2 reads three *weekly* ranges that have closed,
a monthly low, and a sessions-out count over closed sessions. A provisional bar built from a live
quote would change the answer without making it truer, so there is no provisional path here and
no ``provisional`` column to write one into (DECISIONS-TW **TW12.2**).

**It writes signals, states and breadth, and nothing else.** No plan, no order, no broker. The
evening job is still what turns a signal into a plan line, and a person is still what turns a plan
line into an order (`docs/twt/02` Track C §3). Nothing in this module names the execution package
or a broker verb, and ``packages/core/tests/test_twt_safety_properties.py`` asserts that over the
routes that write the row.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import PipelineRun, TwScanRun
from baskfy_core.models.base import JsonObject
from baskfy_worker.tasks.twt import detect_session

log = logging.getLogger(__name__)

#: The task the publisher sends. Named here because the desk and the publisher both need the
#: string and neither imports the other.
SCAN_TASK_NAME: Final = "baskfy.twt.scan"

#: Statuses that mean "somebody is already asking". A second press inside one of these gets the
#: same row back rather than starting a second detection over the same bars.
IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")

#: How long a `QUEUED`/`RUNNING` row stays "in flight" before it is treated as a worker that
#: died. Detection over the full cash universe takes a couple of minutes; ten gives it room
#: without leaving the button dead for an afternoon. The desk copies this number
#: (`app/twt_desk.py`) because it cannot import this module, and `gates/twt-scan-now.md` G5
#: asserts the two copies agree.
STALE_AFTER_SECONDS: Final = 600

#: One press a minute. The button is not a toy and the work is not free.
MIN_INTERVAL_SECONDS: Final = 60

#: How many unpublished rows one pass will publish. A ceiling rather than "all of them": a
#: backlog means something is wrong upstream, and publishing two hundred detections at once
#: would turn that into a second outage.
PUBLISH_LIMIT: Final = 10

__all__ = [
    "IN_FLIGHT",
    "MIN_INTERVAL_SECONDS",
    "PUBLISH_LIMIT",
    "SCAN_TASK_NAME",
    "STALE_AFTER_SECONDS",
    "claim_run",
    "latest_published_session",
    "newest_run",
    "run_twt_scan",
    "unpublished_runs",
]


async def newest_run(session: AsyncSession, *, user_id: int) -> TwScanRun | None:
    """This user's most recent request, whatever state it is in."""
    return (
        await session.execute(
            select(TwScanRun)
            .where(TwScanRun.user_id == user_id)
            .order_by(TwScanRun.requested_at.desc(), TwScanRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def latest_published_session(session: AsyncSession, on_or_before: dt.date) -> dt.date | None:
    """The trade date of the most recently **published** run, at or before ``on_or_before``.

    **The pipeline's date, not the exchange calendar's.** ``trading_day`` says Friday is a trading
    day from the moment Friday begins; the bars for Friday do not exist until the chain publishes
    that evening. A scan that asked the calendar would, pressed at two in the afternoon, faithfully
    detect *today*, find no bars, and report a session the bars have never heard of — which is
    precisely what VB12's first live press did on 11 Sep 2026 before `vbt_rescan` was given this
    same query. Copied deliberately rather than re-derived: it is the product's own answer to
    "what is the latest session", the one the freshness pill reads (`docs/README`, the two clocks).
    """
    return (
        await session.execute(
            select(PipelineRun.trade_date)
            .where(
                PipelineRun.data_version.is_not(None),
                PipelineRun.trade_date <= on_or_before,
            )
            .order_by(PipelineRun.data_version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def unpublished_runs(session: AsyncSession, *, limit: int = PUBLISH_LIMIT) -> list[TwScanRun]:
    """``QUEUED`` rows with no ``task_id`` — what the desk's button leaves behind.

    The publisher's whole job. A row the API published carries its message id and is left alone; a
    row the desk wrote has none, and without this it would sit ``QUEUED`` forever.
    """
    rows = (
        await session.execute(
            select(TwScanRun)
            .where(TwScanRun.status == "QUEUED", TwScanRun.task_id.is_(None))
            .order_by(TwScanRun.requested_at)
            .limit(limit)
        )
    ).scalars()
    return list(rows)


async def claim_run(session: AsyncSession, run_id: int) -> TwScanRun | None:
    """Mark a row ``RUNNING``, or answer ``None`` if somebody else already has it.

    The publisher can publish a row twice if a beat overlaps a slow broker, so the task claims
    rather than assumes. A row that is not ``QUEUED`` is one another worker is already running or
    has finished, and the second copy does nothing rather than detecting the same session twice.
    """
    row = (
        await session.execute(select(TwScanRun).where(TwScanRun.id == run_id))
    ).scalar_one_or_none()
    if row is None or row.status != "QUEUED":
        return None
    row.status = "RUNNING"
    row.started_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    return row


async def run_twt_scan(
    session: AsyncSession, run_id: int, *, now: dt.datetime | None = None
) -> JsonObject:
    """Detect the latest published session for the row's user, and record what it saw.

    **Never raises into the worker**: a failure is ``FAILED`` with the reason on the row, because
    the button needs to be able to *show* what went wrong rather than leaving a request that
    simply stopped. The exception is logged.
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)
    row = await claim_run(session, run_id)
    if row is None:
        return {"run_id": run_id, "skipped": "not QUEUED — already claimed or finished"}
    try:
        day = await latest_published_session(session, stamp.date())
        if day is None:
            raise ValueError(
                "the pipeline has published no session on or before "
                f"{stamp.date().isoformat()}; there is nothing to detect until the chain runs"
            )
        # THE EXISTING DETECTOR, AND THE ONLY CALL IN THIS MODULE THAT DOES ANY WORK.
        # `force=True` because being asked for is the point: the nightly's rule is "skip a session
        # that already has a breadth row", and that is exactly the session a person presses this
        # button about. The write underneath is an upsert, so the re-run is still idempotent.
        detail = await detect_session(session, day, user_id=row.user_id, force=True)
        row.session_date = day
        row.status = "DONE"
        row.detail = dict(detail)
    except Exception as error:
        row.status = "FAILED"
        row.error = f"{type(error).__name__}: {error}"
        log.exception("twt scan failed", extra={"run_id": run_id, "user_id": row.user_id})
    row.finished_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    return {
        "run_id": row.id,
        "status": row.status,
        "session_date": row.session_date.isoformat() if row.session_date else None,
        "detail": row.detail,
        "error": row.error,
    }
