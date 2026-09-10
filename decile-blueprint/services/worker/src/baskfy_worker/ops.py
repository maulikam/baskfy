"""The operational checks behind Prompt 17's alerts, and durable run bookkeeping.

Two jobs that belong together because they are two halves of one answer to Prompt 17's first
acceptance criterion — "killing the worker mid-pipeline produces a correct failed run record and
an alert".

Why a run record needs help to survive
--------------------------------------
``baskfy_worker.orchestrator`` runs the whole ten-step chain inside **one** transaction, and says
why: a run that dies between ``apply_adjustments`` and ``compute_factors`` would leave adjusted
prices beside stale factor rows. That is the right call for the *data*. It is the wrong call for
the *audit trail*: a ``SIGKILL`` rolls the transaction back, and the ``pipeline_run`` row the
orchestrator opened goes with it. The operator is then looking at a night with no run at all,
which is indistinguishable from a night Beat never fired.

So the run row is written **before** the chain starts, on its own session, and committed
(:func:`begin_run`). The chain then updates that row inside its own transaction. A clean failure
updates it to ``failed``; a killed worker leaves it in ``running`` forever, which is a *distinct*
and detectable state — and :func:`reap_abandoned_runs` is what detects it.

Why the reaper uses ``started_at`` and not a heartbeat
-------------------------------------------------------
A heartbeat would detect abandonment in seconds instead of
:attr:`Settings.pipeline_stale_after_minutes` (90 by default). It would also need its own
connection held open across the whole run, updating a
row the chain's own transaction is also writing — two writers on one row, one of them outside the
transaction that owns it. The stale window is above docs/11's 45-minute end-to-end budget with
room for a slow night, so a *healthy* long run is never reaped, and an abandoned one is found by
the next sweep. Detection latency is the price; a lock-order bug at 3am is not worth avoiding it.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.metrics import IST
from baskfy_api.settings import Settings, get_settings
from baskfy_core.models import PipelineRun, TradingDay
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.alerts import Alert, AlertName, Severity
from baskfy_worker.steps import RunStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from baskfy_worker.orchestrator import PipelineOutcome

log = logging.getLogger(__name__)

__all__ = [
    "RUNBOOKS",
    "alert_for_outcome",
    "begin_run",
    "check_kite_token",
    "check_publish_deadline",
    "check_queue_backlog",
    "fail_run",
    "reap_abandoned_runs",
]

#: Alert name -> the runbook that says what to do. Prompt 17 deliverable 5 writes the files; this
#: is what puts the path on the page so it is read rather than rediscovered.
RUNBOOKS: Final[dict[AlertName, str]] = {
    AlertName.PIPELINE_FAILED: "docs/runbooks/pipeline-failed.md",
    AlertName.PIPELINE_ABANDONED: "docs/runbooks/pipeline-failed.md",
    AlertName.GATE_FAILED: "docs/runbooks/bad-data-published.md",
    AlertName.KITE_TOKEN_EXPIRING: "docs/runbooks/kite-token-expired.md",
    AlertName.PUBLISH_LATE: "docs/runbooks/pipeline-failed.md",
    AlertName.ERROR_RATE_HIGH: "docs/runbooks/pipeline-failed.md",
    AlertName.QUEUE_BACKLOG: "docs/runbooks/pipeline-failed.md",
    # SW11: the swing book's five, one runbook (`baskfy_worker.tasks.swing_ops.RUNBOOK`).
    AlertName.SWING_POSITION_NAKED: "docs/runbooks/06-swing-morning.md",
    AlertName.SWING_MONITOR_DID_NOT_START: "docs/runbooks/06-swing-morning.md",
    AlertName.SWING_DETECT_STALE: "docs/runbooks/06-swing-morning.md",
    AlertName.SWING_ORDER_OPEN_AFTER_CUTOFF: "docs/runbooks/06-swing-morning.md",
    AlertName.SWING_GTT_MISSING_AT_1515: "docs/runbooks/06-swing-morning.md",
    # VB7/VB8: the volume-breakout sleeve's four, one runbook
    # (`baskfy_worker.tasks.vbt_ops.RUNBOOK`).
    AlertName.VBT_POSITION_NAKED: "docs/runbooks/08-vbt-evening.md",
    AlertName.VBT_DETECT_STALE: "docs/runbooks/08-vbt-evening.md",
    AlertName.VBT_ORDER_PAST_EXPIRY: "docs/runbooks/08-vbt-evening.md",
    AlertName.VBT_POSITION_NO_BAR: "docs/runbooks/08-vbt-evening.md",
}


# ---------------------------------------------------------------------------
# Durable run bookkeeping
# ---------------------------------------------------------------------------


async def is_trading_day(session: AsyncSession, day: dt.date) -> bool:
    """Whether the exchange had a session on *day*, per the ``trading_day`` calendar.

    Read from the calendar rather than derived from the weekday, because holidays are the case
    that matters and no weekday rule knows them: 2026-08-28 was a Friday and shut.

    **A date the calendar does not carry is not a trading day.** The nightly task runs unattended,
    and the two ways to be wrong are not symmetric — skipping a real session delays a publish by a
    day and is visible in the data-freshness pill, while running a phantom one produces a failed
    run and a CRITICAL alert for a day the exchange was closed. The calendar is seeded years ahead
    (`trading_day` currently reaches 2026-12-31), so an absent row means the far future or a gap,
    and in both cases not running is the safe answer.
    """
    found = await session.scalar(select(TradingDay.is_trading_day).where(TradingDay.date == day))
    return bool(found)


async def begin_run(session: AsyncSession, trade_date: dt.date) -> int:
    """Insert (or reuse) the ``pipeline_run`` row for ``trade_date`` and return its id.

    Called on a session of its own that commits before the chain starts — see the module
    docstring. Reuses a row already in ``running`` for the same date, exactly as
    :func:`baskfy_worker.steps.open_run` does, so a re-delivered Celery message does not open a
    second run for the same night.
    """
    existing = (
        await session.execute(
            select(PipelineRun)
            .where(PipelineRun.trade_date == trade_date, PipelineRun.status == RunStatus.RUNNING)
            .order_by(PipelineRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id

    run = PipelineRun(
        trade_date=trade_date, status=RunStatus.RUNNING, started_at=dt.datetime.now(tz=dt.UTC)
    )
    session.add(run)
    await session.flush()
    return run.id


async def fail_run(session: AsyncSession, run_id: int, error: str) -> PipelineRun | None:
    """Mark a run failed from outside the transaction that was running it.

    Used by the Celery task when the chain raised something the orchestrator did not turn into a
    :class:`~baskfy_worker.steps.HardFailure` — at that point the chain's own transaction has
    already rolled back, taking any in-transaction status update with it.
    """
    run = await session.get(PipelineRun, run_id)
    if run is None:
        return None
    if run.status != RunStatus.RUNNING:
        # Somebody already recorded an outcome. Do not overwrite it with a worse one.
        return run
    run.status = RunStatus.FAILED
    run.finished_at = dt.datetime.now(tz=dt.UTC)
    log.error("pipeline run failed", extra={"run_id": run_id, "error": error})
    return run


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


async def reap_abandoned_runs(
    session: AsyncSession, settings: Settings | None = None, *, now: dt.datetime | None = None
) -> list[Alert]:
    """Mark runs that have been ``running`` past the stale window as ``failed``, and alert.

    This is the half of acceptance criterion 1 that turns "a row nobody will ever update" into
    "a correct failed run record and an alert". It is idempotent: a run it has already failed is
    no longer ``running``, so a second sweep finds nothing.
    """
    resolved = settings or get_settings()
    moment = now or dt.datetime.now(tz=dt.UTC)
    cutoff = moment - dt.timedelta(minutes=resolved.pipeline_stale_after_minutes)

    stale = (
        (
            await session.execute(
                select(PipelineRun)
                .where(PipelineRun.status == RunStatus.RUNNING, PipelineRun.started_at < cutoff)
                .order_by(PipelineRun.trade_date.asc())
            )
        )
        .scalars()
        .all()
    )

    alerts: list[Alert] = []
    for run in stale:
        started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=dt.UTC)
        age_minutes = int((moment - started).total_seconds() // 60)
        run.status = RunStatus.FAILED
        run.finished_at = moment
        alerts.append(
            Alert(
                name=AlertName.PIPELINE_ABANDONED,
                severity=Severity.CRITICAL,
                summary=(
                    f"Pipeline run {run.id} for {run.trade_date.isoformat()} was abandoned "
                    f"after {age_minutes} minutes and has been marked failed."
                ),
                labels={"trade_date": run.trade_date.isoformat(), "run_id": str(run.id)},
                detail={
                    "started_at": started.isoformat(),
                    "age_minutes": age_minutes,
                    "stale_after_minutes": resolved.pipeline_stale_after_minutes,
                    "likely_cause": (
                        "the worker process died mid-run; its transaction rolled back, so no "
                        "step rows survive for the steps that had completed"
                    ),
                },
                runbook=RUNBOOKS[AlertName.PIPELINE_ABANDONED],
            )
        )
    if alerts:
        # Flushed here rather than left to the caller's commit: the alert this returns says the
        # run "has been marked failed", and it must be true of the database by the time anyone
        # can read the alert — not merely staged in a session that might still roll back.
        await session.flush()
    return alerts


def alert_for_outcome(outcome: PipelineOutcome) -> Alert | None:
    """The alert a finished :class:`PipelineOutcome` deserves, or ``None`` if it deserves none.

    A gate failure is its own alert rather than a generic pipeline failure: docs/03 makes step 9
    "a hard gate", and what an operator does about a failed assertion (read the findings, decide
    whether yesterday's data can keep serving) is nothing like what they do about a Kite timeout.

    ``ABORTED`` is not an alert. It is what a non-trading day returns.
    """
    if outcome.status is RunStatus.SUCCEEDED:
        return None
    if outcome.status is RunStatus.ABORTED:
        return None

    gate = outcome.gate
    if gate is not None and not gate.passed:
        return Alert(
            name=AlertName.GATE_FAILED,
            severity=Severity.CRITICAL,
            summary=(
                f"The data-quality gate failed for {outcome.trade_date.isoformat()}; "
                f"data_version was not bumped."
            ),
            labels={"trade_date": outcome.trade_date.isoformat(), "step": "data_quality_gate"},
            detail={
                "failures": [
                    {"assertion": failure.assertion, "message": failure.message}
                    for failure in gate.failures
                ],
                "steps_completed": [step.value for step in outcome.steps_completed],
            },
            runbook=RUNBOOKS[AlertName.GATE_FAILED],
        )

    return Alert(
        name=AlertName.PIPELINE_FAILED,
        severity=Severity.CRITICAL,
        summary=f"The nightly pipeline failed for {outcome.trade_date.isoformat()}.",
        labels={
            "trade_date": outcome.trade_date.isoformat(),
            "step": outcome.failed_step.value if outcome.failed_step else "unknown",
        },
        detail={
            "error": outcome.error,
            "steps_completed": [step.value for step in outcome.steps_completed],
        },
        runbook=RUNBOOKS[AlertName.PIPELINE_FAILED],
    )


def _deadline(settings: Settings) -> dt.time:
    """``BASKFY_PUBLISH_DEADLINE_IST`` as a time. Falls back to docs/11's 20:15 on a bad value."""
    try:
        hour, minute = (int(part) for part in settings.publish_deadline_ist.split(":", 1))
        return dt.time(hour, minute)
    except (TypeError, ValueError):
        log.warning(
            "BASKFY_PUBLISH_DEADLINE_IST is not HH:MM; using docs/11's 20:15",
            extra={"configured": settings.publish_deadline_ist},
        )
        return dt.time(20, 15)


async def check_publish_deadline(
    session: AsyncSession, settings: Settings | None = None, *, now: dt.datetime | None = None
) -> Alert | None:
    """docs/11 §Reliability: "data published by 20:15 IST on >=95% of trading days."

    Run by Beat at the deadline itself. Answers ``None`` on a non-trading day, and ``None`` once
    the day *has* published — so the alert fires exactly once, for the case it exists for.
    """
    resolved = settings or get_settings()
    moment = (now or dt.datetime.now(tz=dt.UTC)).astimezone(IST)
    today = moment.date()

    is_trading_day = (
        await session.execute(
            select(TradingDay.is_trading_day).where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID, TradingDay.date == today
            )
        )
    ).scalar_one_or_none()
    if not is_trading_day:
        return None

    published = (
        (
            await session.execute(
                select(PipelineRun).where(
                    PipelineRun.trade_date == today, PipelineRun.data_version.is_not(None)
                )
            )
        )
        .scalars()
        .first()
    )
    if published is not None:
        return None

    deadline = _deadline(resolved)
    return Alert(
        name=AlertName.PUBLISH_LATE,
        severity=Severity.CRITICAL,
        summary=(
            f"{today.isoformat()} is a trading day and nothing has published by "
            f"{deadline.strftime('%H:%M')} IST."
        ),
        labels={"trade_date": today.isoformat()},
        detail={
            "deadline_ist": deadline.strftime("%H:%M"),
            "checked_at_ist": moment.isoformat(),
            "slo": "docs/11 §Reliability: published by 20:15 IST on >=95% of trading days",
        },
        runbook=RUNBOOKS[AlertName.PUBLISH_LATE],
    )


def check_kite_token(
    settings: Settings | None = None, *, now: dt.datetime | None = None
) -> Alert | None:
    """docs/09 calls Kite token expiry "the #1 pipeline failure". Warn before the pipeline hits it.

    Reads the encrypted token store directly rather than calling Kite: the check runs hourly, the
    expiry is *computed* from the issue time (``baskfy_providers.tokens``), and a network call to
    confirm what arithmetic already knows is a rate-limit budget spent on nothing.

    Returns ``None`` when no token store is configured at all — that is local development with the
    fixture provider, not an incident.
    """
    resolved = settings or get_settings()
    moment = now or dt.datetime.now(tz=dt.UTC)

    from baskfy_providers.settings import get_provider_settings  # noqa: PLC0415
    from baskfy_providers.tokens import AccessTokenStore  # noqa: PLC0415

    provider_settings = get_provider_settings()
    if not (provider_settings.kite_token_path and provider_settings.kite_token_encryption_key):
        return None

    store = AccessTokenStore(
        provider_settings.kite_token_path, provider_settings.kite_token_encryption_key
    )
    if not store.exists():
        return _token_alert("No Kite access token is stored; the next bar fetch will fail.", {})

    try:
        token = store.load()
    except Exception as exc:  # a token we cannot read is a token we do not have
        return _token_alert(
            "The stored Kite access token could not be read.", {"error": type(exc).__name__}
        )

    if token.is_expired(now=moment):
        return _token_alert(
            "The stored Kite access token has expired; a new login is required.",
            {"issued_at": token.issued_at.isoformat()},
        )

    warn_from = moment + dt.timedelta(hours=resolved.kite_token_warning_hours)
    if token.is_expired(now=warn_from):
        return _token_alert(
            (
                f"The stored Kite access token expires within "
                f"{resolved.kite_token_warning_hours} hours."
            ),
            {"issued_at": token.issued_at.isoformat()},
            severity=Severity.WARNING,
        )
    return None


def _token_alert(
    summary: str, detail: dict[str, object], severity: Severity = Severity.CRITICAL
) -> Alert:
    return Alert(
        name=AlertName.KITE_TOKEN_EXPIRING,
        severity=severity,
        summary=summary,
        labels={"provider": "kite"},
        detail=detail,
        runbook=RUNBOOKS[AlertName.KITE_TOKEN_EXPIRING],
    )


async def check_queue_backlog(
    broker: object, queues: tuple[str, ...], settings: Settings | None = None
) -> list[Alert]:
    """One alert per queue that is deeper than :attr:`Settings.queue_backlog_threshold`.

    Also carried as a Prometheus rule over ``baskfy_queue_depth`` (``infra/prometheus/alerts.yml``)
    — this one exists so a deployment with no Prometheus still gets paged, and so the alert can
    name the queue and the depth rather than a threshold crossing.
    """
    resolved = settings or get_settings()
    llen = getattr(broker, "llen", None)
    if not callable(llen):
        return []

    alerts: list[Alert] = []
    for queue in queues:
        try:
            depth = await llen(queue)
        except Exception as exc:  # an unreachable broker is its own problem
            log.warning("queue depth unavailable", extra={"queue": queue, "error": str(exc)})
            continue
        if not isinstance(depth, int) or depth < resolved.queue_backlog_threshold:
            continue
        alerts.append(
            Alert(
                name=AlertName.QUEUE_BACKLOG,
                severity=Severity.WARNING,
                summary=f"Celery queue {queue!r} has {depth} messages waiting.",
                labels={"queue": queue},
                detail={"depth": depth, "threshold": resolved.queue_backlog_threshold},
                runbook=RUNBOOKS[AlertName.QUEUE_BACKLOG],
            )
        )
    return alerts
