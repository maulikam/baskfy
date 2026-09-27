"""VB12: the desk's **Re-detect** button, and the sweep that picks it up.

    vb_scan_run(QUEUED)  ->  sweep publishes  ->  baskfy.vbt.rescan  ->  DONE + the funnel

**Why a row and not a request.** The desk has no Celery client — its venv carries none and it
reaches Baskfy through Postgres alone (`app/vbt_desk.py`). So its button writes one `vb_scan_run`
row and the worker's sweep publishes it, exactly the path SW15 built for the swing book's
"Scan now" and for the same reason.

**What it detects (LV8, 28 Sep 2026 — Maulik's reversal of VB12's "closed session only").** From
09:15 on a trading day until tonight's publish, **today**: one provisional bar per name from a Kite
quote — open, high, low and volume so far, the last price as the close — and the same five lines
run over the published history plus that bar, as the swing book's SW15 scan has since 3 Sep. A
signal becomes a **LIVE** plan whose entries are ``BUY_AT_MARKET`` — taken now, at the live price,
with Kite market protection, never a resting limit at a close that has not printed
(`baskfy_worker.tasks.live_scan`, DECISIONS-VB **VB14**, DECISIONS-LV LV8.0). Every row a live
scan writes is stamped ``provisional`` and is overwritten by the nightly's real bar. Before the
open, after the publish, or **without a Kite session** (no quote source), it re-detects the last
**published** session, plain, and says so in its detail.

**It writes signals, breadth and — on a live scan — a plan, and nothing else.** No order, no
broker; a VBT plan line is confirmed by a person and nothing else. Detection is
idempotent per ``(user_id, date)`` (house rule 7), so pressing the button twice overwrites the
same rows and moves no counter. The evening job is still what turns a signal into a plan line,
and a person is still what turns a plan line into an order.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import VbScanRun
from baskfy_core.models.base import JsonObject
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.live_scan import IST, LiveScan, run_vbt_live
from baskfy_worker.tasks.published_session import last_published_session
from baskfy_worker.tasks.swing_premarket import QuoteSource
from baskfy_worker.tasks.swing_scan_now import ScanNotRunnable, decide_session
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

#: What the row's detail says when the market is open but there is no Kite session to quote it
#: with: the scan fell back to the published session rather than failing.
LIVE_SKIPPED_NO_QUOTES: Final = "market open but no Kite session: today's bar could not be built"

__all__ = [
    "IN_FLIGHT",
    "LIVE_SKIPPED_NO_QUOTES",
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
    """The trade date of the most recently **published** run, at or before ``on_or_before``.

    **The pipeline's date, not the exchange calendar's**, and the difference is the whole point of
    the button. `trading_day` says Friday is a trading day from the moment Friday begins; the
    bars for Friday do not exist until the chain publishes that evening. A re-detect that asked
    the calendar would, pressed at two in the afternoon, faithfully re-detect *today* — find no
    bars, and report "not a session the bars know about".

    That is exactly what happened the first time this ran on the box (11 Sep 2026, 14:14 IST): it
    answered `DONE` with `skipped_reason: "2026-09-11 is not a session the bars know about"` and
    0 signals, when the session worth re-detecting was the one before it. Harmless, and useless.

    `published_trade_date`'s query is the product's own answer to "what is the latest session",
    the same one the freshness pill and the swing book's health check read — a run with a
    `data_version` is a run whose bars are on the page (`docs/README`, the two clocks).

    **The query itself now lives in one place** — `baskfy_worker.tasks.published_session` — and
    all three sleeves' scans delegate to it (`gates/sleeve-read-contract.md` C3). This one was
    already right; swing's was `max(ohlcv_daily.date)` and is the one that moved.
    """
    return await last_published_session(session, on_or_before)


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
    session: AsyncSession,
    run_id: int,
    *,
    now: dt.datetime | None = None,
    quote_source: Callable[[], QuoteSource] | None = None,
    live: LiveScan = run_vbt_live,
) -> JsonObject:
    """Detect for the row's user — today from live quotes when the market is open and a Kite
    session exists, the latest published session otherwise — and record what it saw.

    ``quote_source`` is called only on the provisional path, so a re-detect outside market hours
    never touches Kite. ``live`` is the provisional path itself (``live_scan.run_vbt_live``, which
    the Celery task binds with the sleeve's sizing inputs).

    Never raises into the worker: a failure is `FAILED` with the reason on the row, because the
    desk's button needs to be able to *show* what went wrong. The exception is logged.
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)
    row = await claim_run(session, run_id)
    if row is None:
        return {"run_id": run_id, "skipped": "not QUEUED — already claimed or finished"}
    try:
        try:
            decision = await decide_session(session, stamp.astimezone(IST).replace(tzinfo=None))
        except ScanNotRunnable as error:
            raise ValueError(
                "the pipeline has published no session on or before "
                f"{stamp.date().isoformat()}; there is nothing to re-detect until the chain runs"
                f" ({error})"
            ) from error
        if decision.provisional and quote_source is not None:
            report = await live(
                session, user_id=row.user_id, decision=decision, quotes=quote_source(), now=stamp
            )
            row.session_date = decision.session_date
            row.provisional = True
            row.status = "DONE"
            row.detail = {**report.as_detail(), "reason": decision.reason}
        else:
            day = decision.published_as_of
            outcome = StepOutcome()
            signals = await run_detect_vbt(session, outcome, day, user_id=row.user_id)
            row.session_date = day
            row.provisional = False
            row.status = "DONE"
            # `run_detect_vbt` flattens `VbtFunnel.as_detail()` onto the outcome, so the funnel's
            # counts are already the outcome's keys. Copied verbatim, so the row a person reads
            # after pressing the button is the same shape the nightly step writes.
            row.detail = {
                "signals": signals,
                "status": outcome.status.value,
                "provisional": False,
                "reason": decision.reason,
                **({"live": LIVE_SKIPPED_NO_QUOTES} if decision.provisional else {}),
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
        "provisional": bool(row.provisional),
        "detail": row.detail,
        "error": row.error,
    }
