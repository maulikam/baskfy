"""The acting half of the resync button (leaf 3.1).

    "Give a button to resync so it resyncs everything if any data is pending, so I don't have to
     come to this machine."

:mod:`baskfy_api.resync` finds the work; this closes it. The split is not tidiness — the operator
is on a phone and must be able to read what is about to happen before it happens, so the finding
is a synchronous ``GET`` and the acting is a ``POST`` that answers 202.

**This module runs the same detector the preview ran.** It does not act on the plan the API sent
it; it re-inspects. Minutes can pass in the queue, and repairing a picture that has already
changed is how a repair fixes something that healed and misses something that did not. It then
inspects a *third* time, after the repairs, and that last inspection is what the report calls
``still_pending`` — the honest answer to "did it work", produced by measurement rather than by
assuming the write succeeded.

WHAT IT RUNS, PER CLASS
=======================
=================  ==========================================================================
missing_run        enqueue ``baskfy.pipeline.nightly`` for the date — the same task the admin
                   page's existing re-run button publishes, keyed the same way
thin_bars          ``backfill_bars_from_bhavcopy`` for the one date, then a nightly re-run so
                   the factor windows crossing it are recomputed against the repaired bars
wrong_holiday      correct ``trading_day`` first (while the day is marked shut, every backfill
                   skips it — that is why 2026-08-28 could not heal itself), then as thin_bars
kite_session       ``kite_session_cli.refresh_quietly()`` — SSH to the desk, verify, store
=================  ==========================================================================

IT CANNOT PLACE AN ORDER
========================
Nothing here imports ``packages/execution``, constructs an order gateway, or touches a GTT. The
Kite work is the *session bridge* only: it fetches an access token the desk already obtained and
writes it to the encrypted store. Reading a credential is not trading with it, and the desk's
sshd binds that key to a forced command that can emit one string and nothing else
(``baskfy_worker.kite_session_cli``, M58).

A FAILED KITE PULL IS A WARNING, NEVER A STOPPED RUN
====================================================
``kite_session_cli``'s own docstring is explicit: the day's bars come from the NSE bhavcopy, which
needs no credential. A missing session costs history before 2024, holdings sync and instrument
metadata. So an unreachable desk is reported as a failure *of that item* and the bar repairs carry
on — the alternative trades a working data plant for a broker login, which is the wrong way round.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.resync import RESYNC_COMPLETED_ACTION, ResyncKind, inspect_pending
from baskfy_api.settings import Settings, get_settings
from baskfy_core.models import AdminAction, TradingDay
from baskfy_core.models.base import JsonObject
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.publication import PublicationCheck, bhavcopy_publication_check
from baskfy_worker import kite_session_cli
from baskfy_worker.bhavcopy_backfill import backfill_bars_from_bhavcopy
from baskfy_worker.db import session_scope
from baskfy_worker.providers import build_pipeline_dependencies
from baskfy_worker.window import DateWindow

log = logging.getLogger(__name__)

__all__ = ["MAX_BAR_REPAIRS", "MAX_RERUNS", "NIGHTLY", "ResyncOutcome", "run_resync"]

#: How many days of bars one press repairs. Each is one bhavcopy fetch (served from the raw
#: archive on a re-run) plus a ~3,000-row upsert — seconds, so thirty is a couple of minutes and
#: not an hour of a held worker. Everything past the cap is reported as ``deferred``, by name, so
#: the operator knows to press again rather than believing the job is done.
MAX_BAR_REPAIRS: Final = 30

#: How many nightly chains one press queues. A nightly is minutes of compute on the ``default``
#: queue, so a hundred of them would wedge the worker for the rest of the day. Ten is a long
#: outage's worth; the rest is deferred and named.
MAX_RERUNS: Final = 10

#: Set on a day promoted out of a wrong inferred holiday. ``bhavcopy`` is the ``trading_day``
#: source that means "corroborated by the exchange's own file" (docs/04a) — which is exactly what
#: the publication probe just corroborated, so it is a fact and not a second guess.
CORRECTED_SOURCE: Final = "bhavcopy"

#: The chain a repaired day needs re-run. The same name the admin page's existing re-run
#: button publishes (``baskfy_api.admin.NIGHTLY_TASK_NAME``), duplicated here for the reason
#: ``baskfy_api.queue`` gives about the dependency direction, and pinned by
#: ``services/worker/tests/test_celery_config.py``.
NIGHTLY: Final = "baskfy.pipeline.nightly"


@dataclass(slots=True)
class ResyncOutcome:
    """What one press actually managed — and, first-class, what it did not.

    G7 in one dataclass. A resync that fixes three of five gaps and answers "done" has lied, so
    "could not" and "did not get to" are fields of the result rather than lines in a log nobody
    reads. :attr:`complete` is deliberately narrow: it is *not* "everything is now perfect", it
    is "nothing this press could act on is left over".
    """

    #: Things this press fixed and then measured. Each entry names what changed.
    repaired: list[str] = field(default_factory=list)
    #: Work handed to another task, which has not run yet. Not success — a promise.
    queued: list[str] = field(default_factory=list)
    #: Attempted and could not, each with the reason it gave.
    failed: list[str] = field(default_factory=list)
    #: Not attempted this press, because of :data:`MAX_BAR_REPAIRS` / :data:`MAX_RERUNS`.
    deferred: list[str] = field(default_factory=list)
    #: What the re-inspection *after* the repairs still found — excluding the days whose repair
    #: is sitting in the queue, which would otherwise report as a failure every single time.
    still_pending: list[str] = field(default_factory=list)
    #: Questions the detector could not answer at all: NSE unreachable, no provider on the host,
    #: more suspect weekdays than one pass probes. Kept apart from :attr:`still_pending` because
    #: "I looked and it is broken" and "I could not look" are different sentences and the second
    #: one is the one an operator must never read as the first. It does not clear
    #: :attr:`complete` — but the page is required to render it beside the headline, so
    #: "everything found was closed" can never be mistaken for "everything was checked".
    unresolved: list[str] = field(default_factory=list)
    #: How many findings the pre-repair inspection saw. Zero is a real and common answer.
    pending_at_start: int = 0

    @property
    def complete(self) -> bool:
        return not (self.failed or self.deferred or self.still_pending)

    def as_json(self) -> JsonObject:
        return {
            "repaired": list(self.repaired),
            "queued": list(self.queued),
            "failed": list(self.failed),
            "deferred": list(self.deferred),
            "still_pending": list(self.still_pending),
            "unresolved": list(self.unresolved),
            "pending_at_start": self.pending_at_start,
            "complete": self.complete,
        }


async def _promote_trading_day(session: AsyncSession, day: dt.date) -> None:
    """Undo a wrong inferred holiday: the exchange traded, so say so.

    An upsert rather than an ``UPDATE`` for the reason every write in this tree is an upsert
    (house rule 7): running the repair twice must produce the identical row, and it does — the
    second pass finds ``is_trading_day`` already true, writes the same three values, and the
    detector no longer lists the day at all.
    """
    stmt = insert(TradingDay).values(
        exchange_id=NSE_EXCHANGE_ID,
        date=day,
        is_trading_day=True,
        holiday_name=None,
        source=CORRECTED_SOURCE,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TradingDay.exchange_id, TradingDay.date],
            set_={
                "is_trading_day": stmt.excluded.is_trading_day,
                "holiday_name": stmt.excluded.holiday_name,
                "source": stmt.excluded.source,
            },
        )
    )


async def _record_completion(
    session: AsyncSession, actor_user_id: int, target: str, outcome: ResyncOutcome
) -> None:
    """Append the repair's own report to the staff audit trail.

    The same table the request row went into (``baskfy_api.admin.enqueue_resync``), because "who
    pressed it" and "what it managed" belong on one timeline, and because the admin page already
    renders that timeline. The API reads this row back to render the button's last-result state.
    """
    session.add(
        AdminAction(
            actor_user_id=actor_user_id,
            action=RESYNC_COMPLETED_ACTION,
            target=target,
            detail=outcome.as_json(),
        )
    )
    await session.flush()


async def _repair_bars(
    provider: object, day: dt.date, database_url: str | None, outcome: ResyncOutcome
) -> bool:
    """Re-ingest one day's bhavcopy. Returns whether anything landed.

    Reuses ``baskfy_worker.bhavcopy_backfill`` rather than writing a second ingest path: that
    module is the one that actually repaired 2026-02-01 by hand, it upserts on
    ``(instrument_id, date)``, and it deliberately leaves ``close``/``volume``/``adj_factor``
    alone so a re-ingest cannot un-adjust a series step 4 has already adjusted.
    """
    report = await backfill_bars_from_bhavcopy(
        provider, DateWindow(day, day), database_url=database_url, progress_every=0
    )
    if report.failures:
        reason = "; ".join(f"{k}: {v}" for k, v in sorted(report.failures.items()))
        outcome.failed.append(f"{day.isoformat()}: bhavcopy re-ingest failed — {reason}")
        return False
    if report.missing_days:
        outcome.failed.append(
            f"{day.isoformat()}: NSE published no bhavcopy for this date, so the gap cannot be "
            "closed from the bhavcopy archive (the UDiFF layout only reaches back to 2024)."
        )
        return False
    if report.bars_written == 0:
        outcome.failed.append(
            f"{day.isoformat()}: the bhavcopy parsed but matched no instrument in the register — "
            "run the reference backfill (`make refdata`) before retrying."
        )
        return False
    outcome.repaired.append(
        f"{day.isoformat()}: re-ingested {report.bars_written:,} bars from the NSE bhavcopy"
    )
    return True


async def run_resync(  # noqa: PLR0912, PLR0913 - one branch and one knob per class of gap
    actor_user_id: int,
    *,
    days: int,
    settings: Settings | None = None,
    database_url: str | None = None,
    provider: object | None = None,
    enqueue: object | None = None,
    now: dt.datetime | None = None,
) -> ResyncOutcome:
    """Inspect, repair what can be repaired, re-inspect, and report all three honestly.

    ``enqueue`` is the producer used for nightly re-runs — anything with
    ``send_task(name, args)``. Defaults to the worker's own Celery app. Injectable so a test can
    prove which chains a repair asks for without a broker in the room.
    """
    resolved = settings or get_settings()
    if provider is None:
        provider = build_pipeline_dependencies().provider
    publication = bhavcopy_publication_check(provider)

    def source() -> PublicationCheck | None:
        return publication

    async with session_scope(database_url) as session:
        plan = await inspect_pending(
            session, settings=resolved, publication=source, days=days, now=now
        )
    target = f"{plan.window_start.isoformat()}..{plan.window_end.isoformat()}"
    outcome = ResyncOutcome(pending_at_start=len(plan.findings))

    if not plan.findings:
        outcome.unresolved = list(plan.unresolved)
        async with session_scope(database_url) as session:
            await _record_completion(session, actor_user_id, target, outcome)
        return outcome

    # --- (d) the Kite session, first: history and holdings depend on it, bars do not ---------
    if any(f.kind is ResyncKind.KITE_SESSION for f in plan.findings):
        if await asyncio.to_thread(kite_session_cli.refresh_quietly):
            outcome.repaired.append("Kite session: pulled a fresh access token from the desk")
        else:
            outcome.failed.append(
                "Kite session: the desk could not be reached or its token was refused. Daily "
                "bars are unaffected (they come from the NSE bhavcopy); history before 2024, "
                "holdings sync and instrument metadata stay stale until this succeeds."
            )

    # --- (c) wrong inferred holidays, second: a day marked shut is skipped by every backfill --
    wrong_holidays = plan.dates(ResyncKind.WRONG_HOLIDAY)
    if wrong_holidays:
        async with session_scope(database_url) as session:
            for day in wrong_holidays:
                await _promote_trading_day(session, day)
        for day in wrong_holidays:
            outcome.repaired.append(
                f"{day.isoformat()}: corrected trading_day — NSE published a bhavcopy, so the "
                "inferred holiday was wrong and backfills will no longer skip the date"
            )

    # --- (b)+(c) the bars themselves ---------------------------------------------------------
    bar_days = sorted(set(plan.dates(ResyncKind.THIN_BARS)) | set(wrong_holidays))
    repaired_days: list[dt.date] = []
    for day in bar_days[:MAX_BAR_REPAIRS]:
        if await _repair_bars(provider, day, database_url, outcome):
            repaired_days.append(day)
    for day in bar_days[MAX_BAR_REPAIRS:]:
        outcome.deferred.append(
            f"{day.isoformat()}: not re-ingested this pass ({MAX_BAR_REPAIRS} days per press). "
            "Press resync again."
        )

    # --- (a) and the recompute repaired bars need -------------------------------------------
    # A repaired day's factors are still the ones computed from the thin data, so every day whose
    # bars changed gets a nightly re-run too. Same task, same key, as the admin page's existing
    # re-run button — `baskfy_api.admin.enqueue_rerun`'s docstring covers why that is safe.
    rerun_days = sorted(set(plan.dates(ResyncKind.MISSING_RUN)) | set(repaired_days))
    queued_days: set[dt.date] = set()
    producer = (enqueue if enqueue is not None else _default_producer()) if rerun_days else None
    for day in rerun_days[:MAX_RERUNS]:
        sent = None if producer is None else _send(producer, day)
        if sent is None:
            outcome.failed.append(
                f"{day.isoformat()}: no task broker is reachable, so the nightly chain could not "
                "be queued for this date."
            )
        else:
            queued_days.add(day)
            outcome.queued.append(
                f"{day.isoformat()}: nightly chain queued (task {sent}). It publishes only if the "
                "quality gate passes — watch the run history."
            )
    for day in rerun_days[MAX_RERUNS:]:
        outcome.deferred.append(
            f"{day.isoformat()}: nightly chain not queued this pass ({MAX_RERUNS} per press). "
            "Press resync again."
        )

    # --- the third inspection: what is still wrong, measured rather than assumed --------------
    async with session_scope(database_url) as session:
        after = await inspect_pending(
            session, settings=resolved, publication=source, days=days, now=now
        )
    # `queued_days`, not "the first MAX_RERUNS of rerun_days": a day the broker refused was never
    # queued, and excusing it here would hide a broker outage behind an empty pending list — the
    # precise failure this report exists to prevent.
    outcome.still_pending = [
        finding.summary
        for finding in after.findings
        # A day whose nightly is sitting in the queue is not a failure of this press. It would
        # otherwise report as one on every single run, which is the fastest way to teach an
        # operator to ignore the field that matters most.
        if not (finding.kind is ResyncKind.MISSING_RUN and finding.trade_date in queued_days)
    ]
    outcome.unresolved = list(after.unresolved)

    async with session_scope(database_url) as session:
        await _record_completion(session, actor_user_id, target, outcome)
    log.info(
        "resync finished",
        extra={
            "target": target,
            "repaired": len(outcome.repaired),
            "failed": len(outcome.failed),
            "still_pending": len(outcome.still_pending),
        },
    )
    return outcome


def _default_producer() -> object:
    from baskfy_worker.celery_app import app  # noqa: PLC0415

    return app


def _send(producer: object, day: dt.date) -> str | None:
    """Publish one ``baskfy.pipeline.nightly``. ``None`` when the broker refuses.

    Broad by intention and not silently: Celery's publish path raises anything its transport
    raises (a connection error, a DNS failure, an operational error), and the whole point of this
    function is that a broker outage becomes one honest line in the report instead of aborting
    every repair that had already succeeded.
    """
    send = getattr(producer, "send_task", None)
    if not callable(send):
        return None
    try:
        return str(send(NIGHTLY, [day.isoformat()]))
    except Exception as exc:
        log.warning("resync could not queue a nightly", extra={"date": day, "error": str(exc)})
        return None
