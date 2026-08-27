"""Builds a ready-to-use provider stack from settings.

Everything degrades rather than raises. A missing Redis, an absent bucket or no Kite credentials
produce an adapter that reports itself unavailable, because that is what `providers doctor` has
to be able to print (Prompt 2 acceptance criterion 4) and what docs/03's ``local`` environment
("no Kite calls, provider stubbed") needs.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import redis

from baskfy_providers.archive import LocalRawArchive, RawArchive, S3RawArchive
from baskfy_providers.composite import CompositeProvider
from baskfy_providers.fixtures import FixtureProvider, default_fixture_dir
from baskfy_providers.kite import KiteProvider, KiteRuntime
from baskfy_providers.nse import NSEProvider, NSERuntime, build_http_client
from baskfy_providers.ports import HealthReporting
from baskfy_providers.ratelimit import RedisTokenBucket, TokenBucketConfig
from baskfy_providers.retry import RetryHooks
from baskfy_providers.settings import ProviderSettings, get_provider_settings

#: Where the local archive lives when no S3 bucket is configured.
LOCAL_ARCHIVE_DIRNAME = ".archive"


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
    settings: ProviderSettings, retry_hooks: RetryHooks | None = None
) -> KiteProvider:
    limiter = build_rate_limiter(
        settings, "baskfy:ratelimit:kite", settings.kite_rate_limit_per_second
    )
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
        build_kite_provider(resolved, retry_hooks),
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
