"""VB12: the desk's **Re-detect** button, and the sweep that picks it up.

    vb_scan_run(QUEUED)  ->  sweep publishes  ->  baskfy.vbt.rescan  ->  DONE + the funnel

**Why a row and not a request.** The desk has no Celery client — its venv carries none and it
reaches Baskfy through Postgres alone (`app/vbt_desk.py`). So its button writes one `vb_scan_run`
row and the worker's sweep publishes it, exactly the path SW15 built for the swing book's
"Scan now" and for the same reason.

**What it re-detects, and what it refuses to.** A session that has already **closed**. This is
not the swing book's scan: three of VBT-1's five Chartink lines read the day's volume against its
50-day average, the close's position inside the day's range and the day's change, and the entry
limit *is* the signal bar's close. Asking for those before 15:30 does not give a provisional
answer, it gives a different question with no action attached — `04` §10's clock is the published
session's, and this module keeps it.

**It writes signals and breadth, and nothing else.** No plan, no order, no broker. Detection is
idempotent per ``(user_id, date)`` (house rule 7), so pressing the button twice overwrites the
same rows and moves no counter. The evening job is still what turns a signal into a plan line,
and a person is still what turns a plan line into an order.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import TradingDay, VbScanRun
from baskfy_core.models.base import JsonObject
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.vbt import run_detect_vbt

log = logging.getLogger(__name__)

#: The task the sweep publishes. Named here because the desk and the sweep both need the string
#: and neither imports the other.
RESCAN_TASK_NAME: Final = "baskfy.vbt.rescan"

#: Statuses that mean "somebody is already asking". A second press inside one of these gets the
#: same row back rather than starting a second detection over the same bars.
IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")

#: How long a `QUEUED`/`RUNNING` row stays "in flight" before it is treated as a worker that
#: died. Detection over the full universe takes a couple of minutes; ten gives it room without
#: leaving the button dead for an afternoon.
STALE_AFTER_SECONDS: Final = 600

#: One press a minute. The button is not a toy and the work is not free.
MIN_INTERVAL_SECONDS: Final = 60

__all__ = [
    "IN_FLIGHT",
    "MIN_INTERVAL_SECONDS",
    "RESCAN_TASK_NAME",
    "STALE_AFTER_SECONDS",
    "claim_run",
    "latest_published_session",
    "newest_run",
    "run_vbt_rescan",
    "unpublished_runs",
]


async def newest_run(session: AsyncSession, *, user_id: int) -> VbScanRun | None:
    """This user's most recent request, whatever state it is in."""
    return (
        await session.execute(
            select(VbScanRun)
            .where(VbScanRun.user_id == user_id)
            .order_by(VbScanRun.requested_at.desc(), VbScanRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def latest_published_session(session: AsyncSession, on_or_before: dt.date) -> dt.date | None:
    """The most recent trading day at or before ``on_or_before``.

    The exchange's calendar, not the sleeve's: the sleeve's calendar is built from bars it has
    already detected, and the whole point of a re-detect is that those rows may be missing.
    """
    return (
        await session.execute(
            select(TradingDay.date)
            .where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.is_trading_day.is_(True),
                TradingDay.date <= on_or_before,
            )
            .order_by(TradingDay.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def unpublished_runs(session: AsyncSession, *, limit: int = 10) -> list[VbScanRun]:
    """`QUEUED` rows with no `task_id` — what the desk's button leaves behind.

    The sweep's whole job. A row the API published carries its message id and is left alone; a row
    the desk wrote has none, and without this it would sit `QUEUED` forever.
    """
    rows = (
        await session.execute(
            select(VbScanRun)
            .where(VbScanRun.status == "QUEUED", VbScanRun.task_id.is_(None))
            .order_by(VbScanRun.requested_at)
            .limit(limit)
        )
    ).scalars()
    return list(rows)


async def claim_run(session: AsyncSession, run_id: int) -> VbScanRun | None:
    """Mark a row `RUNNING`, or answer `None` if somebody else already has it.

    The sweep can publish a row twice if a beat overlaps a slow broker, so the task claims rather
    than assumes. A row that is not `QUEUED` is one another worker is already running or has
    finished, and the second copy does nothing rather than detecting the same session twice.
    """
    row = (
        await session.execute(select(VbScanRun).where(VbScanRun.id == run_id))
    ).scalar_one_or_none()
    if row is None or row.status != "QUEUED":
        return None
    row.status = "RUNNING"
    row.started_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    return row


async def run_vbt_rescan(
    session: AsyncSession, run_id: int, *, now: dt.datetime | None = None
) -> JsonObject:
    """Re-detect the latest published session for the row's user, and record what it saw.

    Never raises into the worker: a failure is `FAILED` with the reason on the row, because the
    desk's button needs to be able to *show* what went wrong. The exception is logged.
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)
    row = await claim_run(session, run_id)
    if row is None:
        return {"run_id": run_id, "skipped": "not QUEUED — already claimed or finished"}
    try:
        day = await latest_published_session(session, stamp.date())
        if day is None:
            raise ValueError(
                "the exchange calendar has no trading day on or before "
                f"{stamp.date().isoformat()}; run the reference-data job first"
            )
        outcome = StepOutcome()
        signals = await run_detect_vbt(session, outcome, day, user_id=row.user_id)
        row.session_date = day
        row.status = "DONE"
        # `run_detect_vbt` flattens `VbtFunnel.as_detail()` onto the outcome, so the funnel's
        # counts are already the outcome's keys. Copied verbatim, so the row a person reads after
        # pressing the button is the same shape the nightly step writes.
        row.detail = {
            "signals": signals,
            "status": outcome.status.value,
            **dict(outcome.detail),
        }
    except Exception as error:
        row.status = "FAILED"
        row.error = f"{type(error).__name__}: {error}"
        log.exception("vbt rescan failed", extra={"run_id": run_id, "user_id": row.user_id})
    row.finished_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    return {
        "run_id": row.id,
        "status": row.status,
        "session_date": row.session_date.isoformat() if row.session_date else None,
        "detail": row.detail,
        "error": row.error,
    }
