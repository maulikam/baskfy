"""Step 10 — ``publish`` (docs/03: "bump data_version, purge Redis screen cache, warm top screens").

``data_version`` is the whole point. docs/03: "Step 9 is a hard gate: if it fails, `data_version`
is **not** bumped, so the site continues serving yesterday's consistent snapshot rather than
today's broken one." docs/06 §Caching keys every cached screen result on it, and docs/07 returns
it on every analytics response — so a version that advances over broken data poisons the cache
*and* tells every client the poison is fresh.

The version is monotonic across runs and is only ever set on a run whose gate passed, which makes
"the current data_version" simply the maximum over published runs. No separate table, no counter
to get out of step.

Cache purge and warm-up: docs/06 §Caching says "Invalidate the whole namespace on publish" and
"Warm the top 200 most-run screen definitions after each publish". Both now run here, in that
order — warming before the purge would delete the rows it had just computed. The screener query
engine they both stand on is ``baskfy_api.screener`` (Prompt 6).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.screener import (
    ScreenCache,
    WarmCacheResult,
    current_data_version,
    purge_screen_cache,
    warm_screen_cache,
)
from baskfy_core.models import PipelineRun
from baskfy_core.screener import CACHE_NAMESPACE
from baskfy_worker.steps import RunStatus, StepOutcome

#: docs/06 §Caching: "Key: `screen:{sha256(...)}:{as_of}:{data_version}`". Purging by prefix is
#: what "invalidate the whole namespace" means in Redis terms. Defined in ``baskfy_core.screener``
#: so the writer here and the reader there cannot drift apart; re-exported under the name the
#: rest of the worker already uses.
SCREEN_CACHE_PREFIX: str = CACHE_NAMESPACE

__all__ = ["SCREEN_CACHE_PREFIX", "PublishResult", "current_data_version", "run_publish"]


@dataclass(frozen=True, slots=True)
class PublishResult:
    data_version: int
    cache_keys_purged: int
    screens_warmed: int = 0


async def run_publish(
    session: AsyncSession,
    outcome: StepOutcome,
    run: PipelineRun,
    cache: object = None,
) -> PublishResult:
    """Bump ``data_version`` for ``run`` and invalidate the screen cache.

    Callers must only reach this after the gate passed; the orchestrator enforces that, and
    :func:`run_publish` records the version it assigned so the step row is the audit trail.
    """
    previous = await current_data_version(session)
    version = previous + 1
    run.data_version = version
    run.status = RunStatus.SUCCEEDED
    run.finished_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()

    purged = await _purge_screen_cache(cache, outcome)
    warmed = await _warm_screen_cache(session, cache, outcome)

    outcome.rows_in = 1
    outcome.rows_out = 1
    outcome.note(
        previous_data_version=previous,
        data_version=version,
        cache_keys_purged=purged,
        cache_warmed=warmed.warmed if warmed else 0,
        cache_warm_failures=list(warmed.failures) if warmed and warmed.failures else None,
    )
    return PublishResult(version, purged, warmed.warmed if warmed else 0)


async def _purge_screen_cache(cache: object, outcome: StepOutcome) -> int:
    """Delete every ``screen:*`` key. A missing or unreachable cache is noted, not fatal.

    Publishing with a stale cache would serve yesterday's rows under today's ``data_version``,
    which is worse than a slow first request — so the failure is recorded loudly.
    """
    if cache is None:
        outcome.note(cache="not configured")
        return 0
    scanner = getattr(cache, "scan_iter", None)
    deleter = getattr(cache, "delete", None)
    if not callable(scanner) or not callable(deleter):
        outcome.note(cache="client does not support scan_iter/delete")
        return 0
    try:
        return await purge_screen_cache(cache)
    except RedisError as exc:
        outcome.note(cache_purge_failed=str(exc))
        return 0


async def _warm_screen_cache(
    session: AsyncSession, cache: object, outcome: StepOutcome
) -> WarmCacheResult | None:
    """docs/06 §Caching: "Warm the top 200 most-run screen definitions after each publish."

    Runs after the purge and after ``run.data_version`` has been flushed, so every key it writes
    already carries the new version. A screen that will not run is recorded, never raised: the
    gate has passed and the data is good, so one bad saved definition must not turn a successful
    publish into a failed one.
    """
    if not isinstance(cache, ScreenCache):
        if cache is not None:
            outcome.note(cache_warm="client does not support get/set")
        return None
    try:
        return await warm_screen_cache(session, cache)
    except RedisError as exc:
        outcome.note(cache_warm_failed=str(exc))
        return None
