"""Celery bindings for the pipeline steps.

The work itself lives in the ``run_*`` coroutines beside this module; these are thin wrappers that
give each one a broker identity, a queue and a retry policy. Keeping them separate is what lets
the acceptance tests drive the whole chain in-process against a real database, with no broker and
no worker — a pipeline you can only exercise through Celery is a pipeline you cannot test.

Task names follow the routing prefixes in ``baskfy_worker.celery_app``:
``baskfy.comgest.*`` -> the ingest queue, ``baskfy.compute.*`` -> compute,
``baskfy.pipeline.*`` -> default.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from celery import shared_task
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.screener import (
    WARM_CACHE_SCREEN_LIMIT,
    ScreenCache,
    WarmCacheResult,
    warm_screen_cache,
)
from baskfy_core.models.base import JsonObject
from baskfy_providers.errors import TransientProviderError
from baskfy_worker import ops
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.celery_app import IST, QUEUES
from baskfy_worker.db import run_in_session
from baskfy_worker.orchestrator import PipelineOutcome, run_nightly_pipeline
from baskfy_worker.providers import build_cache, build_pipeline_dependencies
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.adjustments import instruments_with_actions, reprocess_instrument
from baskfy_worker.tasks.alerts import SWEEP_BATCH, run_alert_dispatch, run_webhook_sweep
from baskfy_worker.tasks.backtests import (
    BacktestNotRunnable,
    build_backtest_archive,
    build_publisher,
    run_backtest_job,
)
from baskfy_worker.tasks.curated_batch_sync import run_curated_batch_sync
from baskfy_worker.tasks.curated_dividends import run_curated_dividends
from baskfy_worker.tasks.curated_metrics import run_curated_metrics
from baskfy_worker.tasks.curated_rebalance_notify import run_curated_rebalance_notify
from baskfy_worker.tasks.curated_sip import run_curated_sip_reminders
from baskfy_worker.tasks.purge_accounts import run_purge_accounts

#: docs/09 §"Kite specifics" — a rate-limited or flaky upstream is worth retrying; a malformed
#: payload or a missing credential is not. Only transient provider failures auto-retry.
RETRYABLE: tuple[type[Exception], ...] = (TransientProviderError,)


@shared_task(name="baskfy.pipeline.nightly", acks_late=True)
def nightly_pipeline(trade_date: str | None = None) -> JsonObject:
    """docs/03's ten-step chain for one trade date.

    Deliberately not auto-retried. A failed gate must not be re-attempted on a timer: docs/03 says
    the site keeps serving the previous ``data_version``, and a retry loop would either publish
    the same broken day later or bury the alert under repeated identical failures.

    Three transactions, not one (Prompt 17 deliverable 3 and its first acceptance criterion):

    1. ``begin_run`` commits the ``pipeline_run`` row **before** the chain starts, so the run
       exists even if this process is killed a second later.
    2. the chain itself, in the one transaction ``baskfy_worker.orchestrator`` describes.
    3. if the chain raised something it did not convert into a ``HardFailure``, its transaction
       has already rolled back — so the failure is recorded on a third session that can still
       commit. A ``SIGKILL`` reaches none of these, which is what
       ``baskfy.ops.reap_abandoned_runs`` is for.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    run_id: int = run_in_session(lambda session: ops.begin_run(session, day))

    try:
        outcome: PipelineOutcome = run_in_session(
            lambda session: run_nightly_pipeline(session, day, deps, run_id=run_id)
        )
    except Exception as exc:
        # Bound to locals before the lambda: Python unbinds the `except` name at the end of the
        # block, and a closure that outlives it would raise `NameError` instead of reporting the
        # failure it was written to report.
        message, kind = str(exc), type(exc).__name__
        run_in_session(lambda session: ops.fail_run(session, run_id, message))
        _raise_alert(
            Alert(
                name=AlertName.PIPELINE_FAILED,
                severity=Severity.CRITICAL,
                summary=f"The nightly pipeline raised {kind} for {day.isoformat()}.",
                labels={"trade_date": day.isoformat(), "run_id": str(run_id)},
                detail={"error": message, "error_type": kind},
                runbook=ops.RUNBOOKS[AlertName.PIPELINE_FAILED],
            )
        )
        raise

    alert = ops.alert_for_outcome(outcome)
    if alert is not None:
        _raise_alert(alert)
    return {
        "run_id": outcome.run_id,
        "trade_date": outcome.trade_date.isoformat(),
        "status": outcome.status.value,
        "data_version": outcome.data_version,
        "failed_step": outcome.failed_step.value if outcome.failed_step else None,
        "error": outcome.error,
        "steps_completed": [s.value for s in outcome.steps_completed],
        "alert": alert.name.value if alert else None,
    }


def _raise_alert(alert: Alert) -> JsonObject:
    """Dispatch one alert from synchronous Celery code.

    ``dispatch`` is a coroutine because the mail transport is; a Celery task body is not. Same
    ``asyncio.run`` boundary as ``baskfy_worker.db.run_in_session``, and for the same reason.
    """
    return asyncio.run(dispatch(alert))


@shared_task(name="baskfy.ops.reap_abandoned_runs")
def reap_abandoned_runs_task() -> JsonObject:
    """Find pipeline runs nobody is running any more, fail them, and alert.

    Prompt 17's first acceptance criterion in one task. Scheduled every fifteen minutes rather
    than nightly: an abandoned run is an outage in progress, and finding it the following evening
    is finding it after the market has already opened on stale data.
    """

    async def sweep(session: AsyncSession) -> list[JsonObject]:
        alerts = await ops.reap_abandoned_runs(session)
        return [await dispatch(alert) for alert in alerts]

    dispatched = run_in_session(sweep)
    return {"reaped": len(dispatched), "alerts": dispatched}


@shared_task(name="baskfy.ops.check_publish_deadline")
def check_publish_deadline_task() -> JsonObject:
    """docs/11 §Reliability: "data published by 20:15 IST on >=95% of trading days."

    Scheduled at the deadline itself. Silent on a non-trading day and on a day that published.
    """

    async def check(session: AsyncSession) -> JsonObject | None:
        alert = await ops.check_publish_deadline(session)
        return None if alert is None else await dispatch(alert)

    dispatched = run_in_session(check)
    return {"alert": dispatched}


@shared_task(name="baskfy.ops.check_kite_token")
def check_kite_token_task() -> JsonObject:
    """docs/09 calls Kite token expiry "the #1 pipeline failure" — so warn before it bites.

    Hourly. The check is arithmetic over the stored issue time and makes no network call, so
    running it often costs nothing and the warning lands with hours to spare.
    """
    alert = ops.check_kite_token()
    return {"alert": None if alert is None else _raise_alert(alert)}


@shared_task(name="baskfy.ops.check_queue_backlog")
def check_queue_backlog_task() -> JsonObject:
    """One alert per Celery queue that has backed up past the configured threshold."""
    cache = build_cache()
    if cache is None:
        return {"alerts": [], "detail": "no broker client could be built"}

    async def check() -> list[JsonObject]:
        try:
            alerts = await ops.check_queue_backlog(cache, QUEUES)
            return [await dispatch(alert) for alert in alerts]
        finally:
            await cache.aclose()

    dispatched = asyncio.run(check())
    return {"alerts": dispatched}


@shared_task(
    name="baskfy.compute.reprocess_instrument",
    autoretry_for=RETRYABLE,
    retry_backoff=True,
    retry_jitter=True,
)
def reprocess_instrument_task(instrument_id: int) -> JsonObject:
    """docs/09 §"Adjustment algorithm" rebuild rule — idempotent by construction."""
    result = run_in_session(lambda session: reprocess_instrument(session, instrument_id))
    return {
        "instrument_id": result.instrument_id,
        "bars_rewritten": result.bars_rewritten,
        "actions_applied": result.actions_applied,
        "unquantified": list(result.unquantified),
    }


@shared_task(name="baskfy.pipeline.integrity_audit")
def integrity_audit() -> JsonObject:
    """docs/09 §Schedule: "Sat 02:00 — full-history integrity audit".

    Rebuilds every instrument that carries a corporate action, which is the only way to notice an
    adjusted series that has drifted from its raw prints. Because ``reprocess_instrument`` is
    idempotent, a healthy history is rewritten to identical values and the audit is a no-op.
    """
    return run_in_session(_audit_all)


async def _audit_all(session: AsyncSession) -> JsonObject:
    targets = await instruments_with_actions(session)
    rewritten = 0
    unquantified: list[str] = []
    for instrument_id in targets:
        result = await reprocess_instrument(session, instrument_id)
        rewritten += result.bars_rewritten
        unquantified.extend(f"instrument {instrument_id}: {u}" for u in result.unquantified)
    return {
        "instruments": len(targets),
        "bars_rewritten": rewritten,
        "unquantified_actions": unquantified or None,
    }


@shared_task(name="baskfy.accounts.purge")
def purge_accounts_task() -> JsonObject:
    """Prompt 12 §5: close the seven-day soft-delete window.

    Daily rather than on a timer per account: an erasure a day late is within any reasonable
    reading of DPDP, and a scheduled sweep is one moving part instead of one per deletion.
    """

    async def purge(session: AsyncSession) -> JsonObject:
        outcome = StepOutcome()
        purged = await run_purge_accounts(session, outcome)
        return {"purged": purged, "considered": outcome.rows_in, "notes": outcome.detail or None}

    return run_in_session(purge)


@shared_task(name="baskfy.compute.warm_screen_cache")
def warm_screen_cache_task(limit: int = WARM_CACHE_SCREEN_LIMIT) -> JsonObject:
    """docs/06 §Caching: "Warm the top 200 most-run screen definitions after each publish."

    ``publish`` already warms inline, so this exists for the two cases that need it out of band:
    re-warming after a manual cache flush, and warming a freshly restarted Redis without waiting
    for the next nightly run. Not auto-retried — a cold cache is a slow first request, not an
    incident, and the next publish warms it anyway.
    """
    cache = build_cache()
    if cache is None or not isinstance(cache, ScreenCache):
        return {"warmed": 0, "skipped": 0, "failures": ["no usable cache client configured"]}

    async def warm(session: AsyncSession) -> WarmCacheResult:
        try:
            return await warm_screen_cache(session, cache, limit=limit)
        finally:
            await cache.aclose()

    result: WarmCacheResult = run_in_session(warm)
    return {
        "warmed": result.warmed,
        "skipped": result.skipped,
        "failures": list(result.failures),
    }


@shared_task(name="baskfy.backtest.run", acks_late=True)
def run_backtest_task(public_id: str, fragility: bool = True) -> JsonObject:
    """PROMPTS.md Prompt 15 §4: the backtest, on its own queue.

    Routed to ``backtest`` by the ``baskfy.backtest.*`` prefix in
    ``baskfy_worker.celery_app.TASK_ROUTES``, which is docs/03 §"Scaling plan" step 4: "Move
    backtest fan-out to a dedicated worker pool with its own queue."

    Deliberately **not** auto-retried. A backtest that failed on bad configuration will fail the
    same way in sixty seconds, and one that failed on a missing price series will fail until the
    backfill runs; both belong on the row where the user can read them (``backtest.error``), not
    in a retry loop that hides them. The concurrency caps are enforced when the run is *enqueued*
    (`baskfy_api.backtests.capacity_check`), so a retry storm would also breach them.
    """
    archive = build_backtest_archive()
    publisher = build_publisher()
    try:
        outcome = run_in_session(
            lambda session: run_backtest_job(
                session, public_id, archive=archive, publisher=publisher, fragility=fragility
            )
        )
    except BacktestNotRunnable as exc:
        # A redelivered message for a run somebody else already took. Not an error.
        return {"public_id": public_id, "status": "skipped", "detail": str(exc)}
    finally:
        if publisher is not None:
            publisher.close()
    return outcome.as_dict()


@shared_task(name="baskfy.alerts.dispatch", acks_late=True)
def dispatch_screen_alerts_task(trade_date: str | None = None) -> JsonObject:
    """PROMPTS.md Prompt 20 §3: the nightly screen-alert email, after publish.

    Deliberately **not** auto-retried. Every send is recorded in ``screen_alert_delivery``, which
    is unique on ``(alert, as_of)``, so re-running the task for a date is a no-op rather than a
    second email — but a retry loop on a broken mail transport would still hammer the provider
    for the alerts that *had* not been recorded yet, and the failure belongs in an alert (Prompt
    17) rather than in a loop.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    return run_in_session(lambda session: run_alert_dispatch(session, day))


@shared_task(name="baskfy.alerts.sweep_webhooks")
def sweep_webhooks_task(limit: int = SWEEP_BATCH) -> JsonObject:
    """PROMPTS.md Prompt 20 §4's "retry with backoff", driven by a timer rather than by Celery.

    The backoff schedule lives on the ``webhook_delivery`` row (``next_attempt_at``), not in
    Celery's retry machinery, for one reason: a retry held inside a task is lost when the worker
    dies, and a webhook that a receiver never got and nobody remembers to resend is worse than a
    late one. The row survives a restart; the sweep picks it up.
    """
    return run_in_session(lambda session: run_webhook_sweep(session, limit=limit))


@shared_task(name="baskfy.cb.compute_metrics", acks_late=True)
def compute_curated_metrics_task(trade_date: str | None = None) -> JsonObject:
    """SC2: upsert ``cb_metrics`` for every curated basket on a trading day.

    Idempotent per ``(basket_id, as_of_date)``. Defaults to today in IST when Beat fires without
    an argument.

    Uses its own session (no outer ``begin()``): ``compute_all_metrics`` commits per chunk and
    per basket. Wrapping it in ``run_in_session``'s transaction context closes the transaction
    on the first commit and raises ``Can't operate on closed transaction``.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()

    async def _run() -> JsonObject:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from baskfy_api.settings import get_settings

        engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await run_curated_metrics(session, day)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


@shared_task(name="baskfy.cb.sip_reminders", acks_late=True)
def curated_sip_reminders_task(as_of: str | None = None) -> JsonObject:
    """SC7: raise ``SIP_DUE`` pending actions for ACTIVE REMINDER plans due on *as_of*.

    Idempotent per ``(plan_id, YYYY-MM)``. Never places an order; AUTO plans are not selected.
    Defaults to today in IST when Beat fires without an argument.
    """
    day = dt.date.fromisoformat(as_of) if as_of else dt.datetime.now(tz=IST).date()
    return run_in_session(lambda session: run_curated_sip_reminders(session, day))


@shared_task(name="baskfy.cb.derive_dividends", acks_late=True)
def curated_dividends_task(as_of: str | None = None) -> JsonObject:
    """SC4: derive ``cb_dividend`` from cash corporate actions x ACTIVE holdings.

    Idempotent per ``(investment_id, instrument_id, ex_date)``. Never places an order.
    Defaults to today in IST when Beat fires without an argument.
    """
    day = dt.date.fromisoformat(as_of) if as_of else dt.datetime.now(tz=IST).date()
    return run_in_session(lambda session: run_curated_dividends(session, day))


@shared_task(name="baskfy.cb.sync_batches", acks_late=True)
def curated_batch_sync_task() -> JsonObject:
    """T8.2: promote PLANNED batches to EXECUTED from the desk journal.

    Synthetic ``cb-sim-*`` ids stay PLANNED. Never places an order.
    """
    return run_in_session(run_curated_batch_sync)


@shared_task(name="baskfy.cb.rebalance_notify", acks_late=True)
def curated_rebalance_notify_task() -> JsonObject:
    """T8.3: email once per open REBALANCE_AVAILABLE lacking payload.notified_at."""
    return run_in_session(run_curated_rebalance_notify)
