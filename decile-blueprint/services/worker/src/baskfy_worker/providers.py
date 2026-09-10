"""Wires the provider stack the pipeline runs against.

docs/03 §Environments defines the two shapes: ``local`` is a "100-instrument sample, 3 years,
seeded from fixtures ... no Kite calls, provider stubbed"; ``staging``/``prod`` use "real
providers". Both are a :class:`CompositeProvider`, so the pipeline code is identical either way —
which is what docs/02 means by "adding a paid vendor later is a new adapter, not a rewrite".
"""

from __future__ import annotations

import logging
import os

from redis.asyncio import Redis
from redis.exceptions import RedisError

from baskfy_providers.factory import build_provider_stack
from baskfy_providers.settings import get_provider_settings
from baskfy_worker.deps import PipelineDependencies
from baskfy_worker.settings import get_worker_settings
from baskfy_worker.telemetry import provider_retry_hooks

log = logging.getLogger(__name__)


def build_cache() -> Redis | None:
    """A Redis client for the publish step's purge and warm-up, or ``None`` if none can be built.

    An **async** client: every caller is inside a coroutine, and ``baskfy_api.screener`` runs the
    screen cache asynchronously for the reason set out there — a blocking Redis call inside an
    event loop stops every other request for its duration.

    Reachability is not probed here, because probing it would need an ``await`` in a synchronous
    factory and would only tell us about a moment that has already passed. ``publish`` treats a
    ``RedisError`` at use time as an un-purged namespace and records it, which is what docs/03
    step 10 needs: an unreachable cache must not abort a run whose data is fine.
    """
    try:
        client: Redis = Redis.from_url(get_worker_settings().redis_url)
    except (RedisError, ValueError):
        return None
    return client


def build_pipeline_dependencies() -> PipelineDependencies:
    return PipelineDependencies(
        # `provider_retry_hooks()` is what makes docs/09 §Observability's "provider error rate"
        # a number rather than a grep: every backoff increments
        # `baskfy_provider_calls_total{outcome="retry"}` and logs the adapter that failed.
        provider=build_provider_stack(get_provider_settings(), provider_retry_hooks()),
        cache=build_cache(),
        # docs/05's engine is Prompt 5. Until then compute_factors runs its skeleton and says so
        # in the step payload; see baskfy_worker.tasks.factors.
        factor_engine=None,
        # SW3. Read once, here, rather than inside the step: `docs/swing/02` requires a flag to be
        # "read once per process at startup ... and never from a form", and the sole tenant is the
        # only user the `sw_` schema has in this run.
        swing_user_id=_sole_user_id(),
        swing_index_slug=get_worker_settings().swing_index_slug,
        swing_execution_enabled=get_worker_settings().swing_execution_enabled,
        # VB4. The same tenant and the same rule: read once, here, never inside the step.
        vbt_user_id=_sole_user_id(),
        vbt_nightly_enabled=get_worker_settings().vbt_nightly_enabled,
        vbt_execution_enabled=get_worker_settings().vbt_execution_enabled,
    )


def sole_user_id() -> int | None:
    """The public name for :func:`_sole_user_id`.

    SW18's login nudge needs exactly the same tenant — the ``state`` it mints is bound to a
    user id and the callback refuses one that does not match the signed-in account — and it has
    no reason to build the whole provider stack to learn it. One reader of
    ``BASKFY_SOLE_USER_ID`` in this process, two callers.
    """
    return _sole_user_id()


def _sole_user_id() -> int | None:
    """``BASKFY_SOLE_USER_ID``, or ``None`` on a deployment that has not set one.

    ``None`` rather than a default of 1: the swing step is keyed by user, and inventing a tenant
    for a deployment that never declared one would write another account's book.
    """
    raw = os.environ.get("BASKFY_SOLE_USER_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("BASKFY_SOLE_USER_ID is not a number (%r); the swing step will skip", raw)
        return None
