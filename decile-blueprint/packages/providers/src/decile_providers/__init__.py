"""decile_providers — market-data adapters behind the ports in docs/09 §"Provider ports".

    KiteProvider       bars           (docs/02: Kite gives candles and nothing else)
    NSEProvider        reference      (constituents, index snapshots, corp actions, listings,
                                       bhavcopy)
    CompositeProvider  routes by capability, one circuit breaker per provider
    FixtureProvider    both ports from Parquet, zero network — local dev and every test

Call sites depend on ``Capability`` and the Protocols, never on a vendor: docs/02 promises that
"adding a paid vendor later is a new adapter, not a rewrite".
"""

from decile_providers.archive import (
    LocalRawArchive,
    RawArchive,
    S3RawArchive,
    archive_key,
    fetch_and_archive,
)
from decile_providers.circuit import CircuitBreaker, CircuitState
from decile_providers.composite import CompositeProvider
from decile_providers.errors import (
    AccessTokenExpired,
    ArchiveError,
    CapabilityNotAvailable,
    CircuitOpen,
    CredentialsMissing,
    ProviderDataError,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    RetryBudgetExhausted,
    TransientProviderError,
    UnexpectedPayload,
    UpstreamUnavailable,
)
from decile_providers.fixtures import FixtureProvider, default_fixture_dir
from decile_providers.kite import KiteProvider, KiteRuntime
from decile_providers.nse import NSEProvider, NSERuntime
from decile_providers.ports import (
    BARS_CAPABILITIES,
    REFERENCE_CAPABILITIES,
    BarsProvider,
    Capability,
    ProviderHealth,
    ReferenceProvider,
)
from decile_providers.ratelimit import RateLimiter, RedisTokenBucket, TokenBucketConfig
from decile_providers.records import (
    BHAVCOPY_SCHEMA,
    DAILY_BARS_SCHEMA,
    CorporateAction,
    IndexSnapshot,
    InstrumentRecord,
    ListingRecord,
)
from decile_providers.retry import RetryHooks, RetryPolicy, call_with_retry
from decile_providers.settings import ProviderSettings, get_provider_settings
from decile_providers.tokens import AccessToken, AccessTokenStore

__all__ = [
    "BARS_CAPABILITIES",
    "BHAVCOPY_SCHEMA",
    "DAILY_BARS_SCHEMA",
    "REFERENCE_CAPABILITIES",
    "AccessToken",
    "AccessTokenExpired",
    "AccessTokenStore",
    "ArchiveError",
    "BarsProvider",
    "Capability",
    "CapabilityNotAvailable",
    "CircuitBreaker",
    "CircuitOpen",
    "CircuitState",
    "CompositeProvider",
    "CorporateAction",
    "CredentialsMissing",
    "FixtureProvider",
    "IndexSnapshot",
    "InstrumentRecord",
    "KiteProvider",
    "KiteRuntime",
    "ListingRecord",
    "LocalRawArchive",
    "NSEProvider",
    "NSERuntime",
    "ProviderDataError",
    "ProviderError",
    "ProviderHealth",
    "ProviderSettings",
    "ProviderUnavailable",
    "RateLimited",
    "RateLimiter",
    "RawArchive",
    "RedisTokenBucket",
    "ReferenceProvider",
    "RetryBudgetExhausted",
    "RetryHooks",
    "RetryPolicy",
    "S3RawArchive",
    "TokenBucketConfig",
    "TransientProviderError",
    "UnexpectedPayload",
    "UpstreamUnavailable",
    "archive_key",
    "call_with_retry",
    "default_fixture_dir",
    "fetch_and_archive",
    "get_provider_settings",
]
