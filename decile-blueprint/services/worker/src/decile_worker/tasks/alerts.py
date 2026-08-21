"""The nightly alert dispatch and the webhook sweeper — Prompt 20 deliverables 3 and 4.

The work itself is ``decile_api.alerts.dispatch`` and ``decile_api.webhooks``; what is here is the
two ``run_*`` coroutines the Celery bindings wrap, in the same shape as every pipeline step
(``decile_worker.tasks.*``): a coroutine that takes a session and returns a JSON-able summary, so
the whole thing is drivable in-process with no broker.

**Alerts run after publish, not with it.** docs/09's schedule publishes the day's data and
``decile.pipeline.nightly`` ends there. An alert that failed to send must not roll back a publish,
and a publish that failed must not send alerts about a day nobody can see — two tasks on the
``default`` queue, in that order, is the arrangement that gives both.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api import alerts as alert_service
from decile_api import webhooks
from decile_api.email import Mailer, build_transport
from decile_api.settings import Settings, get_settings
from decile_core.models.base import JsonObject

#: How many due deliveries one sweep attempts. Bounded so a backlog is worked through over several
#: sweeps rather than in one task that holds a worker for an unbounded time.
SWEEP_BATCH = 100


def build_mailer(settings: Settings | None = None) -> Mailer:
    """One transport per task invocation. docs/02 §Email — the same builder the API uses."""
    return Mailer(build_transport(settings or get_settings()))


async def run_alert_dispatch(
    session: AsyncSession,
    trade_date: dt.date,
    *,
    mailer: Mailer | None = None,
    settings: Settings | None = None,
) -> JsonObject:
    """Send every alert due for ``trade_date``, and enqueue every webhook for it."""
    resolved = settings or get_settings()
    report = await alert_service.dispatch(
        session,
        as_of=trade_date,
        mailer=mailer or build_mailer(resolved),
        settings=resolved,
    )
    return report.as_dict()


@dataclass(slots=True)
class SweepReport:
    attempted: int = 0
    delivered: int = 0
    retrying: int = 0
    failed: int = 0

    def as_dict(self) -> JsonObject:
        return {
            "attempted": self.attempted,
            "delivered": self.delivered,
            "retrying": self.retrying,
            "failed": self.failed,
        }


async def run_webhook_sweep(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
    now: dt.datetime | None = None,
    limit: int = SWEEP_BATCH,
) -> JsonObject:
    """Attempt every delivery whose backoff has elapsed.

    ``client`` is injected so the suite can drive an ``httpx.MockTransport``; the whole test run
    is network-blocked, and a sender that could only be exercised against a real socket would not
    be exercised at all.
    """
    resolved = settings or get_settings()
    moment = now or dt.datetime.now(tz=dt.UTC)
    due = await webhooks.due_deliveries(session, limit=limit, now=moment)
    report = SweepReport()
    if not due:
        return report.as_dict()

    owned = client is None
    transport = client or httpx.AsyncClient()
    try:
        for delivery, endpoint in due:
            report.attempted += 1
            outcome = await webhooks.attempt_delivery(
                session,
                delivery,
                endpoint,
                settings=resolved,
                client=transport,
                now=moment,
            )
            if outcome.delivered:
                report.delivered += 1
            elif outcome.retrying:
                report.retrying += 1
            else:
                report.failed += 1
    finally:
        if owned:
            await transport.aclose()
    return report.as_dict()
