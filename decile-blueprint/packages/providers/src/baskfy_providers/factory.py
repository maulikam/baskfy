"""Builds a ready-to-use provider stack from settings.

Everything degrades rather than raises. A missing Redis, an absent bucket or no Kite credentials
produce an adapter that reports itself unavailable, because that is what `providers doctor` has
to be able to print (Prompt 2 acceptance criterion 4) and what docs/03's ``local`` environment
("no Kite calls, provider stubbed") needs.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final

import boto3
import redis

from baskfy_providers.archive import LocalRawArchive, RawArchive, S3RawArchive
from baskfy_providers.composite import CompositeProvider
from baskfy_providers.fixtures import FixtureProvider, default_fixture_dir
from baskfy_providers.kite import KiteProvider, KiteRuntime
from baskfy_providers.nse import NSEProvider, NSERuntime, build_http_client
from baskfy_providers.ports import HealthReporting
from baskfy_providers.ratelimit import (
    CallSpacingConfig,
    LayeredCallSpacer,
    RateLimiter,
    RedisCallSpacer,
    RedisTokenBucket,
    TokenBucketConfig,
)
from baskfy_providers.retry import RetryHooks
from baskfy_providers.settings import ProviderSettings, get_provider_settings

#: Where the local archive lives when no S3 bucket is configured.
LOCAL_ARCHIVE_DIRNAME = ".archive"

#: Kite's combined read ceiling, as a Redis departure clock. Every Kite read in this deployment
#: — API, worker, desk — waits on this one key, which is what makes "3 req/s" a property of the
#: box rather than a hope about each container. The desk names the same key from its own side
#: (`kite-momentum-rebalancer/app/core/kite_limits.py`, family ``read``).
KITE_READ_CLOCK_KEY: Final = "baskfy:ratelimit:kite:read"

#: The bulk lane's own clock, taken *before* the ceiling. See `LayeredCallSpacer`: the gap
#: between this rate and the ceiling is the headroom a login-time read finds free.
KITE_BULK_CLOCK_KEY: Final = "baskfy:ratelimit:kite:bulk"

#: Kite's **per-endpoint** caps, as departure clocks under the same prefix the desk uses
#: (`kite-momentum-rebalancer/app/core/kite_limits.py`, `SHARED_KEY_PREFIX` + family). The desk's
#: swing monitor and page reads take ``read`` then the family; the options collector (OP3) takes
#: the same two keys in the same order, so the box holds one 1 req/s quote clock and one 3 req/s
#: historical clock, not one per container. The rates are the desk's `RATE_FOR_FAMILY`.
KITE_CLOCK_PREFIX: Final = "baskfy:ratelimit:kite"


class KiteFamily(StrEnum):
    """Kite's endpoint families, each with its own published cap (Kite Connect docs)."""

    QUOTE = "quote"
    HISTORICAL = "historical"
    GENERAL = "general"


#: Per-family rates, identical to the desk's `RATE_FOR_FAMILY` (a test holds the two equal).
KITE_FAMILY_RATE_PER_SECOND: Final[dict[KiteFamily, float]] = {
    KiteFamily.QUOTE: 1.0,
    KiteFamily.HISTORICAL: 3.0,
    KiteFamily.GENERAL: 9.0,
}


def kite_family_key(family: KiteFamily) -> str:
    """``baskfy:ratelimit:kite:<family>`` — the desk's key for the same family."""
    return f"{KITE_CLOCK_PREFIX}:{family.value}"


#: How long an interactive caller waits for a slot before giving up. Generous, because with the
#: bulk lane in place the wait is a fraction of a second and a real wait this long means the
#: ceiling is genuinely oversubscribed — at which point failing is better than hanging a page.
INTERACTIVE_MAX_WAIT_SECONDS: Final = 30.0

#: And how long a bulk caller waits. Much longer: a backfill has nowhere to be, and a bulk call
#: that gives up costs a bar that then has to be caught up on another day.
BULK_MAX_WAIT_SECONDS: Final = 300.0


class KiteLane(StrEnum):
    """Which side of Kite's combined ceiling a provider is built for.

    ``INTERACTIVE`` is the default deliberately: it is the lane that only ever takes the ceiling
    clock, so a call site nobody has classified is throttled correctly and is never the one
    starving somebody. A caller has to *ask* to be bulk.
    """

    INTERACTIVE = "interactive"
    BULK = "bulk"


def build_rate_limiter(
    settings: ProviderSettings, key: str, rate_per_second: float
) -> RedisTokenBucket | None:
    """A shared token bucket, or ``None`` when Redis is unreachable.

    ``None`` rather than an exception: an unreachable Redis must show up as "this provider is not
    available" in the doctor, not as a stack trace at import time. The Kite adapter then refuses
    to call upstream at all, which is the safe direction — unthrottled is worse than offline.
    """
    try:
        client = redis.Redis.from_url(settings.redis_url)
        client.ping()
    except Exception:
        return None
    return RedisTokenBucket(
        client,
        key,
        TokenBucketConfig(rate_per_second=rate_per_second, capacity=rate_per_second),
    )


def build_spaced_rate_limiter(
    settings: ProviderSettings,
    key: str,
    rate_per_second: float,
    *,
    max_wait_seconds: float = INTERACTIVE_MAX_WAIT_SECONDS,
) -> RedisCallSpacer | None:
    """A shared no-burst request clock, or ``None`` when Redis is unreachable.

    Kite's limit is shared across endpoints and containers. A token bucket permits its full
    capacity immediately after idle time, so the broker can see more than three calls inside a
    second. The departure clock is conservative by construction: every call is at least 1/rate
    seconds after the preceding call on this Redis key.
    """
    try:
        client = redis.Redis.from_url(settings.redis_url)
        client.ping()
    except Exception:
        return None
    return RedisCallSpacer(
        client,
        key,
        CallSpacingConfig(rate_per_second=rate_per_second, max_wait_seconds=max_wait_seconds),
    )


def build_kite_read_limiter(
    settings: ProviderSettings, lane: KiteLane = KiteLane.INTERACTIVE
) -> RateLimiter | None:
    """The clock (or clocks) a Kite read of this ``lane`` must wait on — M85.

    Both lanes end on `KITE_READ_CLOCK_KEY`, so the broker's combined ceiling is one number for
    the whole box no matter who is calling. ``BULK`` takes `KITE_BULK_CLOCK_KEY` first, which
    holds a backfill below that ceiling and leaves the remainder standing free for the login-time
    holdings read and the live swing scan. `LayeredCallSpacer` carries the arithmetic.

    ``None`` when Redis is unreachable, exactly as before: `KiteProvider` then refuses to call
    upstream at all, which is the safe direction.
    """
    ceiling = build_spaced_rate_limiter(
        settings,
        KITE_READ_CLOCK_KEY,
        settings.kite_rate_limit_per_second,
        max_wait_seconds=(
            INTERACTIVE_MAX_WAIT_SECONDS if lane is KiteLane.INTERACTIVE else BULK_MAX_WAIT_SECONDS
        ),
    )
    if ceiling is None or lane is KiteLane.INTERACTIVE:
        return ceiling
    lane_clock = build_spaced_rate_limiter(
        settings,
        KITE_BULK_CLOCK_KEY,
        settings.kite_bulk_rate_limit_per_second,
        max_wait_seconds=BULK_MAX_WAIT_SECONDS,
    )
    if lane_clock is None:
        # The ceiling alone is still correct — it is the broker's actual limit. What is lost is
        # the headroom, so say so rather than pretending the lane exists.
        return ceiling
    return LayeredCallSpacer((lane_clock, ceiling))


def build_kite_family_limiter(
    settings: ProviderSettings,
    family: KiteFamily,
    lane: KiteLane = KiteLane.INTERACTIVE,
) -> RateLimiter | None:
    """The lane's clocks, then the box's ``read`` ceiling, then ``family``'s own cap (OP3).

    `build_kite_read_limiter` holds a caller to Kite's combined ceiling; this adds the endpoint's
    cap on top, which is what the options reads need: the chain collector's ``quote()`` shares the
    **1 req/s** quote clock with the desk's swing monitor and page reads, and the index-bar reads
    share the **3 req/s** historical clock. The family clock is taken **last**, the desk's order
    (`DeskLimits.slot` takes ``read`` then the family), so the tightest per-endpoint clock is the
    one whose departures are exact.

    ``BULK`` takes `KITE_BULK_CLOCK_KEY` first, as `build_kite_read_limiter` does.

    ``None`` when Redis is unreachable — `KiteProvider` then refuses to call at all. Any clock
    missing (Redis dropped between builds) also returns ``None`` rather than a partial limiter:
    the quote cap is the tightest and silently losing it is the failure this exists to prevent.
    """
    max_wait = (
        INTERACTIVE_MAX_WAIT_SECONDS if lane is KiteLane.INTERACTIVE else BULK_MAX_WAIT_SECONDS
    )
    clocks: list[RedisCallSpacer | None] = []
    if lane is KiteLane.BULK:
        clocks.append(
            build_spaced_rate_limiter(
                settings,
                KITE_BULK_CLOCK_KEY,
                settings.kite_bulk_rate_limit_per_second,
                max_wait_seconds=BULK_MAX_WAIT_SECONDS,
            )
        )
    clocks.append(
        build_spaced_rate_limiter(
            settings,
            KITE_READ_CLOCK_KEY,
            settings.kite_rate_limit_per_second,
            max_wait_seconds=max_wait,
        )
    )
    clocks.append(
        build_spaced_rate_limiter(
            settings,
            kite_family_key(family),
            KITE_FAMILY_RATE_PER_SECOND[family],
            max_wait_seconds=max_wait,
        )
    )
    built = [clock for clock in clocks if clock is not None]
    if len(built) != len(clocks):
        return None
    return LayeredCallSpacer(built)


def build_kite_family_provider(
    settings: ProviderSettings,
    family: KiteFamily,
    retry_hooks: RetryHooks | None = None,
    *,
    lane: KiteLane = KiteLane.INTERACTIVE,
) -> KiteProvider:
    """A Kite adapter whose every call waits on ``family``'s cap as well as the ceiling (OP3)."""
    limiter = build_kite_family_limiter(settings, family, lane)
    return KiteProvider(settings, KiteRuntime(rate_limiter=limiter, retry_hooks=retry_hooks))


def build_archive(settings: ProviderSettings, *, local_root: Path | None = None) -> RawArchive:
    """R2/S3 when configured, otherwise a directory on disk.

    Local development still goes through the archive-then-parse path (docs/09) rather than
    bypassing it, so the ordering is exercised everywhere and cannot rot.
    """
    if settings.s3_configured():
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
        )
        return S3RawArchive(client, settings.s3_bucket)
    # An explicit `local_root` (tests) wins, then the configured directory, then the relative
    # default. The middle one is what a deployment needs: `.archive` resolves against a
    # root-owned working directory in the image and fails every write.
    root = local_root or (
        Path(settings.raw_archive_dir) if settings.raw_archive_dir else Path(LOCAL_ARCHIVE_DIRNAME)
    )
    return LocalRawArchive(root)


def build_kite_provider(
    settings: ProviderSettings,
    retry_hooks: RetryHooks | None = None,
    *,
    lane: KiteLane = KiteLane.INTERACTIVE,
) -> KiteProvider:
    """A Kite adapter throttled for ``lane``. Interactive by default — see `KiteLane`."""
    limiter = build_kite_read_limiter(settings, lane)
    return KiteProvider(settings, KiteRuntime(rate_limiter=limiter, retry_hooks=retry_hooks))


def build_nse_provider(
    settings: ProviderSettings,
    archive: RawArchive | None = None,
    retry_hooks: RetryHooks | None = None,
) -> NSEProvider:
    limiter = build_rate_limiter(
        settings, "baskfy:ratelimit:nse", settings.nse_rate_limit_per_second
    )
    client = build_http_client(settings) if limiter is not None else None
    return NSEProvider(
        settings,
        archive if archive is not None else build_archive(settings),
        NSERuntime(client=client, rate_limiter=limiter, retry_hooks=retry_hooks),
    )


def build_fixture_provider(settings: ProviderSettings) -> FixtureProvider | None:
    """``None`` when no fixtures are present, so the doctor can say so."""
    directory = Path(settings.fixture_dir) if settings.fixture_dir else _default_fixture_dir()
    if directory is None:
        return None
    return FixtureProvider(directory)


def _default_fixture_dir() -> Path | None:
    try:
        return default_fixture_dir()
    except FileNotFoundError:
        return None


def build_provider_stack(
    settings: ProviderSettings | None = None, retry_hooks: RetryHooks | None = None
) -> CompositeProvider:
    """The production wiring: Kite for bars, NSE for reference, fixtures last.

    Registration order is preference order (see CompositeProvider), so fixtures sit at the back
    and only serve what the real vendors could not — which in a configured production environment
    is nothing, and in local development is everything.

    ``retry_hooks`` is Prompt 17's observation seam: the worker passes hooks that count every
    backoff into ``baskfy_provider_calls_total`` (docs/09 §Observability: "provider error rate").
    ``None`` leaves the default hooks, which observe nothing — which is what the test suite and
    ``providers doctor`` want, neither of them having a metrics registry to write to.
    """
    resolved = settings or get_provider_settings()
    providers: list[HealthReporting] = [
        # BULK: this stack is what the nightly chain and the backfills run on, and its Kite
        # pass is thousands of `historical_data` calls. It must leave the ceiling's headroom for
        # whoever is on the other side of a login (M85).
        build_kite_provider(resolved, retry_hooks, lane=KiteLane.BULK),
        build_nse_provider(resolved, retry_hooks=retry_hooks),
    ]
    fixtures = build_fixture_provider(resolved)
    if fixtures is not None:
        providers.append(fixtures)
    return CompositeProvider(
        providers,
        failure_threshold=resolved.provider_circuit_failure_threshold,
        reset_seconds=resolved.provider_circuit_reset_seconds,
    )
