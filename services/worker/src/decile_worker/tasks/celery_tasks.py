"""Celery bindings for the pipeline steps.

The work itself lives in the ``run_*`` coroutines beside this module; these are thin wrappers that
give each one a broker identity, a queue and a retry policy. Keeping them separate is what lets
the acceptance tests drive the whole chain in-process against a real database, with no broker and
no worker — a pipeline you can only exercise through Celery is a pipeline you cannot test.

Task names follow the routing prefixes in ``decile_worker.celery_app``:
``decile.ingest.*`` -> the ingest queue, ``decile.compute.*`` -> compute,
``decile.pipeline.*`` -> default.
"""

from __future__ import annotations

import datetime as dt

from celery import shared_task
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.screener import (
    WARM_CACHE_SCREEN_LIMIT,
    ScreenCache,
    WarmCacheResult,
    warm_screen_cache,
)
from decile_core.models.base import JsonObject
from decile_providers.errors import TransientProviderError
from decile_worker.celery_app import IST
from decile_worker.db import run_in_session
from decile_worker.orchestrator import PipelineOutcome, run_nightly_pipeline
from decile_worker.providers import build_cache, build_pipeline_dependencies
from decile_worker.steps import StepOutcome
from decile_worker.tasks.adjustments import instruments_with_actions, reprocess_instrument
from decile_worker.tasks.purge_accounts import run_purge_accounts

#: docs/09 §"Kite specifics" — a rate-limited or flaky upstream is worth retrying; a malformed
#: payload or a missing credential is not. Only transient provider failures auto-retry.
RETRYABLE: tuple[type[Exception], ...] = (TransientProviderError,)


@shared_task(name="decile.pipeline.nightly", acks_late=True)
def nightly_pipeline(trade_date: str | None = None) -> JsonObject:
    """docs/03's ten-step chain for one trade date.

    Deliberately not auto-retried. A failed gate must not be re-attempted on a timer: docs/03 says
    the site keeps serving the previous ``data_version``, and a retry loop would either publish
    the same broken day later or bury the alert under repeated identical failures.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    outcome: PipelineOutcome = run_in_session(
        lambda session: run_nightly_pipeline(session, day, deps)
    )
    return {
        "trade_date": outcome.trade_date.isoformat(),
        "status": outcome.status.value,
        "data_version": outcome.data_version,
        "failed_step": outcome.failed_step.value if outcome.failed_step else None,
        "error": outcome.error,
        "steps_completed": [s.value for s in outcome.steps_completed],
    }


@shared_task(
    name="decile.compute.reprocess_instrument",
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


@shared_task(name="decile.pipeline.integrity_audit")
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


@shared_task(name="decile.accounts.purge")
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


@shared_task(name="decile.compute.warm_screen_cache")
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
