"""Wires the provider stack the pipeline runs against.

docs/03 §Environments defines the two shapes: ``local`` is a "100-instrument sample, 3 years,
seeded from fixtures ... no Kite calls, provider stubbed"; ``staging``/``prod`` use "real
providers". Both are a :class:`CompositeProvider`, so the pipeline code is identical either way —
which is what docs/02 means by "adding a paid vendor later is a new adapter, not a rewrite".
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError

from decile_providers.factory import build_provider_stack
from decile_providers.settings import get_provider_settings
from decile_worker.deps import PipelineDependencies
from decile_worker.settings import get_worker_settings
from decile_worker.telemetry import provider_retry_hooks


def build_cache() -> Redis | None:
    """A Redis client for the publish step's purge and warm-up, or ``None`` if none can be built.

    An **async** client: every caller is inside a coroutine, and ``decile_api.screener`` runs the
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
        # `decile_provider_calls_total{outcome="retry"}` and logs the adapter that failed.
        provider=build_provider_stack(get_provider_settings(), provider_retry_hooks()),
        cache=build_cache(),
        # docs/05's engine is Prompt 5. Until then compute_factors runs its skeleton and says so
        # in the step payload; see decile_worker.tasks.factors.
        factor_engine=None,
    )
