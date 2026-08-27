"""baskfy_providers — market-data adapters behind the ports in docs/09 §"Provider ports".

    KiteProvider       bars + the read-only broker ledger (holdings, cash)
    NSEProvider        reference      (constituents, index snapshots, corp actions, listings,
                                       bhavcopy)
    CompositeProvider  routes by capability, one circuit breaker per provider
    FixtureProvider    both ports from Parquet, zero network — local dev and every test

Call sites depend on ``Capability`` and the Protocols, never on a vendor: docs/02 promises that
"adding a paid vendor later is a new adapter, not a rewrite".
"""

from baskfy_providers.archive import (
    LocalRawArchive,
    RawArchive,
    S3RawArchive,
    archive_key,
    fetch_and_archive,
)
from baskfy_providers.circuit import CircuitBreaker, CircuitState
from baskfy_providers.composite import CompositeProvider
from baskfy_providers.errors import (
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
from baskfy_providers.fixtures import (
    FixtureHoldingsProvider,
    FixtureProvider,
    default_fixture_dir,
)
from baskfy_providers.kite import KiteProvider, KiteRuntime
from baskfy_providers.nse import NSEProvider, NSERuntime
from baskfy_providers.ports import (
    BARS_CAPABILITIES,
    HOLDINGS_CAPABILITIES,
    REFERENCE_CAPABILITIES,
    BarsProvider,
    Capability,
    HoldingsProvider,
    ProviderHealth,
    ReferenceProvider,
)
from baskfy_providers.ratelimit import RateLimiter, RedisTokenBucket, TokenBucketConfig
from baskfy_providers.records import (
    BHAVCOPY_SCHEMA,
    DAILY_BARS_SCHEMA,
    BrokerAccountRef,
    BrokerHoldingRecord,
    CorporateAction,
    IndexSnapshot,
    InstrumentRecord,
    ListingRecord,
)
from baskfy_providers.retry import RetryHooks, RetryPolicy, call_with_retry
from baskfy_providers.settings import ProviderSettings, get_provider_settings
from baskfy_providers.tokens import AccessToken, AccessTokenStore

__all__ = [
    "BARS_CAPABILITIES",
    "BHAVCOPY_SCHEMA",
    "DAILY_BARS_SCHEMA",
    "HOLDINGS_CAPABILITIES",
    "REFERENCE_CAPABILITIES",
    "AccessToken",
    "AccessTokenExpired",
    "AccessTokenStore",
    "ArchiveError",
    "BarsProvider",
    "BrokerAccountRef",
    "BrokerHoldingRecord",
    "Capability",
    "CapabilityNotAvailable",
    "CircuitBreaker",
    "CircuitOpen",
    "CircuitState",
    "CompositeProvider",
    "CorporateAction",
    "CredentialsMissing",
    "FixtureHoldingsProvider",
    "FixtureProvider",
    "HoldingsProvider",
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
