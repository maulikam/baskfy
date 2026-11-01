//! Builds a ready-to-use provider stack from settings. Ports `baskfy_providers.factory`.
//!
//! Everything degrades rather than raises. A missing Redis, an absent bucket or no Kite
//! credentials produce an adapter that reports itself unavailable, because that is what
//! `providers doctor` has to be able to print (Prompt 2 acceptance criterion 4) and what
//! docs/03's `local` environment ("no Kite calls, provider stubbed") needs.
//!
//! Python opens a fresh Redis client per limiter (`redis.Redis.from_url` + `ping`), so one clock
//! can come up while the next does not. [`BackendSource`] keeps that shape: the production
//! [`RedisSource`] connects on every ask, and a test can hand out an in-memory backend, or none.
//!
//! The Kite adapter is `kite.rs`'s [`KiteProvider`]; [`build_provider_stack_with`] takes any bars
//! adapter as a [`Registered`], and [`build_provider_stack`] is the production wiring with Kite.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use async_trait::async_trait;

use crate::archive::{LocalRawArchive, RawArchive, S3RawArchive, SigV4S3Client};
use crate::composite::{CompositeProvider, Registered, SupplementaryReads};
use crate::errors::ProviderError;
use crate::fixtures::{FixtureProvider, default_fixture_dir};
use crate::kite::{KiteProvider, KiteRuntime};
use crate::nse::{NseProvider, NseRuntime, build_http_client};
use crate::ratelimit::{
    CallSpacingConfig, LayeredCallSpacer, RateLimiter, RedisCallSpacer, RedisScriptBackend,
    RedisTokenBucket, ScriptBackend, SpacedLimiter, TokenBucketConfig,
};
use crate::retry::RetryHooks;
use crate::secret::Secret;
use crate::settings::ProviderSettings;

/// Where the local archive lives when no S3 bucket is configured.
pub const LOCAL_ARCHIVE_DIRNAME: &str = ".archive";

/// The NSE token bucket's Redis key (`build_nse_provider`).
pub const NSE_RATE_LIMIT_KEY: &str = "baskfy:ratelimit:nse";

/// Kite's combined read ceiling, as a Redis departure clock. Every Kite read in this deployment
/// — API, worker, desk — waits on this one key, which makes "3 req/s" a property of the box.
/// The desk names the same key (`kite-momentum-rebalancer/app/core/kite_limits.py`, `read`).
pub const KITE_READ_CLOCK_KEY: &str = "baskfy:ratelimit:kite:read";

/// The bulk lane's own clock, taken *before* the ceiling (see [`LayeredCallSpacer`]).
pub const KITE_BULK_CLOCK_KEY: &str = "baskfy:ratelimit:kite:bulk";

/// Kite's **per-endpoint** caps, as departure clocks under the prefix the desk uses.
pub const KITE_CLOCK_PREFIX: &str = "baskfy:ratelimit:kite";

/// Kite's endpoint families, each with its own published cap (`KiteFamily`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum KiteFamily {
    /// `quote`.
    Quote,
    /// `historical`.
    Historical,
    /// `general`.
    General,
}

impl KiteFamily {
    /// The `StrEnum` value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Quote => "quote",
            Self::Historical => "historical",
            Self::General => "general",
        }
    }

    /// `KITE_FAMILY_RATE_PER_SECOND[family]` — identical to the desk's `RATE_FOR_FAMILY`.
    #[must_use]
    pub const fn rate_per_second(self) -> f64 {
        match self {
            Self::Quote => 1.0,
            Self::Historical => 3.0,
            Self::General => 9.0,
        }
    }
}

/// `kite_family_key`: `baskfy:ratelimit:kite:<family>` — the desk's key for the same family.
#[must_use]
pub fn kite_family_key(family: KiteFamily) -> String {
    format!("{KITE_CLOCK_PREFIX}:{}", family.value())
}

/// How long an interactive caller waits for a slot before giving up.
pub const INTERACTIVE_MAX_WAIT_SECONDS: f64 = 30.0;

/// How long a bulk caller waits: a backfill has nowhere to be.
pub const BULK_MAX_WAIT_SECONDS: f64 = 300.0;

/// Which side of Kite's combined ceiling a provider is built for (`KiteLane`). `Interactive` is
/// the default deliberately: a call site nobody classified only ever takes the ceiling clock.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum KiteLane {
    /// `interactive`.
    #[default]
    Interactive,
    /// `bulk`.
    Bulk,
}

impl KiteLane {
    /// The `StrEnum` value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Interactive => "interactive",
            Self::Bulk => "bulk",
        }
    }

    const fn max_wait(self) -> f64 {
        match self {
            Self::Interactive => INTERACTIVE_MAX_WAIT_SECONDS,
            Self::Bulk => BULK_MAX_WAIT_SECONDS,
        }
    }
}

/// Where a limiter's Redis comes from: `redis.Redis.from_url(url)` plus a `ping`, per limiter.
#[async_trait]
pub trait BackendSource: Send + Sync {
    /// A live backend, or `None` when Redis is unreachable (never an error: an unreachable Redis
    /// must show up as "this provider is not available", not as a stack trace).
    async fn backend(&self) -> Option<Arc<dyn ScriptBackend>>;
}

/// How long [`RedisSource`] waits for Redis before calling it unreachable. Python's
/// `Redis.from_url(url).ping()` fails at once on a refused connection; redis-rs's connection
/// manager retries with backoff instead, so the wait is bounded here (DECISIONS-RS R4.13).
pub const REDIS_CONNECT_TIMEOUT_SECONDS: f64 = 5.0;

/// The production [`BackendSource`]: connects to `redis_url` on every ask.
#[derive(Debug, Clone)]
pub struct RedisSource {
    url: Secret,
    timeout: std::time::Duration,
}

impl RedisSource {
    /// A source for `settings.redis_url`, waiting [`REDIS_CONNECT_TIMEOUT_SECONDS`].
    #[must_use]
    pub fn from_settings(settings: &ProviderSettings) -> Self {
        Self {
            url: settings.redis_url.clone(),
            timeout: crate::clock::duration(REDIS_CONNECT_TIMEOUT_SECONDS),
        }
    }

    /// The same source with another connect bound.
    #[must_use]
    pub const fn with_timeout(mut self, timeout: std::time::Duration) -> Self {
        self.timeout = timeout;
        self
    }
}

#[async_trait]
impl BackendSource for RedisSource {
    async fn backend(&self) -> Option<Arc<dyn ScriptBackend>> {
        let connect = RedisScriptBackend::connect(&self.url);
        let backend = tokio::time::timeout(self.timeout, connect)
            .await
            .ok()?
            .ok()?;
        Some(Arc::new(backend))
    }
}

/// `build_rate_limiter`: a shared token bucket (capacity one second's worth), or `None` when
/// Redis is unreachable. The adapter then refuses to call upstream at all — unthrottled is worse
/// than offline.
///
/// # Errors
/// `ValueError` for a non-positive rate (Python raises it from the config too).
pub async fn build_rate_limiter(
    source: &dyn BackendSource,
    key: &str,
    rate_per_second: f64,
) -> Result<Option<Arc<RedisTokenBucket>>, ProviderError> {
    let Some(backend) = source.backend().await else {
        return Ok(None);
    };
    let config = TokenBucketConfig::new(rate_per_second, rate_per_second)?;
    Ok(Some(Arc::new(RedisTokenBucket::new(backend, key, config))))
}

/// `build_spaced_rate_limiter`: a shared no-burst request clock, or `None` when Redis is
/// unreachable. Every call is at least 1/rate seconds after the preceding call on this key.
///
/// # Errors
/// `ValueError` for a non-positive rate or negative wait.
pub async fn build_spaced_rate_limiter(
    source: &dyn BackendSource,
    key: &str,
    rate_per_second: f64,
    max_wait_seconds: f64,
) -> Result<Option<Arc<RedisCallSpacer>>, ProviderError> {
    let Some(backend) = source.backend().await else {
        return Ok(None);
    };
    let config = CallSpacingConfig::with_max_wait(rate_per_second, max_wait_seconds)?;
    Ok(Some(Arc::new(RedisCallSpacer::new(backend, key, config))))
}

/// What [`build_kite_read_limiter`] returns: the bare ceiling (`RedisCallSpacer`) or the bulk
/// lane's layered clocks (`LayeredCallSpacer`), as Python returns one or the other.
#[derive(Clone)]
pub enum KiteReadLimiter {
    /// The ceiling alone.
    Ceiling(Arc<RedisCallSpacer>),
    /// The lane clock, then the ceiling.
    Layered(Arc<LayeredCallSpacer>),
}

impl std::fmt::Debug for KiteReadLimiter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_tuple("KiteReadLimiter")
            .field(&self.keys())
            .finish()
    }
}

impl KiteReadLimiter {
    /// The Redis keys it waits on, in order.
    #[must_use]
    pub fn keys(&self) -> Vec<String> {
        match self {
            Self::Ceiling(c) => vec![c.key().to_owned()],
            Self::Layered(l) => l.spacers().iter().map(|s| s.key().to_owned()).collect(),
        }
    }

    /// Each clock's rate and wait budget, in order.
    #[must_use]
    pub fn configs(&self) -> Vec<CallSpacingConfig> {
        match self {
            Self::Ceiling(c) => vec![c.config()],
            Self::Layered(l) => l.spacers().iter().map(|s| s.config()).collect(),
        }
    }

    /// As the [`RateLimiter`] a provider is wired with.
    #[must_use]
    pub fn into_limiter(self) -> Arc<dyn RateLimiter> {
        match self {
            Self::Ceiling(c) => c,
            Self::Layered(l) => l,
        }
    }
}

/// `build_kite_read_limiter`: the clock (or clocks) a Kite read of this `lane` must wait on —
/// M85. Both lanes end on [`KITE_READ_CLOCK_KEY`]; `Bulk` takes [`KITE_BULK_CLOCK_KEY`] first,
/// which holds a backfill below the ceiling and leaves headroom for the login-time reads. If
/// the lane clock cannot be built, the ceiling alone is still correct.
///
/// # Errors
/// As [`build_spaced_rate_limiter`].
pub async fn build_kite_read_limiter(
    source: &dyn BackendSource,
    settings: &ProviderSettings,
    lane: KiteLane,
) -> Result<Option<KiteReadLimiter>, ProviderError> {
    let ceiling = build_spaced_rate_limiter(
        source,
        KITE_READ_CLOCK_KEY,
        settings.kite_rate_limit_per_second,
        lane.max_wait(),
    )
    .await?;
    let Some(ceiling) = ceiling else {
        return Ok(None);
    };
    if lane == KiteLane::Interactive {
        return Ok(Some(KiteReadLimiter::Ceiling(ceiling)));
    }
    let lane_clock = build_spaced_rate_limiter(
        source,
        KITE_BULK_CLOCK_KEY,
        settings.kite_bulk_rate_limit_per_second,
        BULK_MAX_WAIT_SECONDS,
    )
    .await?;
    let Some(lane_clock) = lane_clock else {
        // The ceiling alone is still correct — it is the broker's actual limit. What is lost is
        // the headroom.
        return Ok(Some(KiteReadLimiter::Ceiling(ceiling)));
    };
    let layered = LayeredCallSpacer::new(vec![
        lane_clock as Arc<dyn SpacedLimiter>,
        ceiling as Arc<dyn SpacedLimiter>,
    ])?;
    Ok(Some(KiteReadLimiter::Layered(Arc::new(layered))))
}

/// `build_kite_family_limiter`: the lane's clocks, then the box's `read` ceiling, then
/// `family`'s own cap (OP3) — the desk's order, so the tightest per-endpoint clock is the one
/// whose departures are exact. Any clock missing returns `None` rather than a partial limiter:
/// the quote cap is the tightest and silently losing it is the failure this exists to prevent.
///
/// # Errors
/// As [`build_spaced_rate_limiter`].
pub async fn build_kite_family_limiter(
    source: &dyn BackendSource,
    settings: &ProviderSettings,
    family: KiteFamily,
    lane: KiteLane,
) -> Result<Option<Arc<LayeredCallSpacer>>, ProviderError> {
    let max_wait = lane.max_wait();
    let mut clocks: Vec<Option<Arc<RedisCallSpacer>>> = Vec::new();
    if lane == KiteLane::Bulk {
        clocks.push(
            build_spaced_rate_limiter(
                source,
                KITE_BULK_CLOCK_KEY,
                settings.kite_bulk_rate_limit_per_second,
                BULK_MAX_WAIT_SECONDS,
            )
            .await?,
        );
    }
    clocks.push(
        build_spaced_rate_limiter(
            source,
            KITE_READ_CLOCK_KEY,
            settings.kite_rate_limit_per_second,
            max_wait,
        )
        .await?,
    );
    clocks.push(
        build_spaced_rate_limiter(
            source,
            &kite_family_key(family),
            family.rate_per_second(),
            max_wait,
        )
        .await?,
    );
    let total = clocks.len();
    let built: Vec<Arc<dyn SpacedLimiter>> = clocks
        .into_iter()
        .flatten()
        .map(|c| c as Arc<dyn SpacedLimiter>)
        .collect();
    if built.len() != total {
        return Ok(None);
    }
    Ok(Some(Arc::new(LayeredCallSpacer::new(built)?)))
}

/// `build_kite_provider`: a Kite adapter throttled for `lane`. Interactive by default — see
/// [`KiteLane`]. No Redis leaves it without a limiter, so it refuses to call upstream at all.
///
/// # Errors
/// As [`build_kite_read_limiter`] and [`KiteProvider::new`].
pub async fn build_kite_provider(
    source: &dyn BackendSource,
    settings: &ProviderSettings,
    retry_hooks: Option<RetryHooks>,
    lane: KiteLane,
) -> Result<KiteProvider, ProviderError> {
    let limiter = build_kite_read_limiter(source, settings, lane).await?;
    KiteProvider::new(
        settings.clone(),
        KiteRuntime {
            rate_limiter: limiter.map(KiteReadLimiter::into_limiter),
            retry_hooks,
            ..KiteRuntime::default()
        },
    )
}

/// `build_kite_family_provider`: a Kite adapter whose every call waits on `family`'s cap as well
/// as the ceiling (OP3).
///
/// # Errors
/// As [`build_kite_family_limiter`] and [`KiteProvider::new`].
pub async fn build_kite_family_provider(
    source: &dyn BackendSource,
    settings: &ProviderSettings,
    family: KiteFamily,
    retry_hooks: Option<RetryHooks>,
    lane: KiteLane,
) -> Result<KiteProvider, ProviderError> {
    let limiter = build_kite_family_limiter(source, settings, family, lane).await?;
    KiteProvider::new(
        settings.clone(),
        KiteRuntime {
            rate_limiter: limiter.map(|l| l as Arc<dyn RateLimiter>),
            retry_hooks,
            ..KiteRuntime::default()
        },
    )
}

/// Kite's duck-typed reads (`index_level`, `fno_underlyings`) as the composite asks for them.
struct KiteReads(Arc<KiteProvider>);

#[async_trait]
impl SupplementaryReads for KiteReads {
    async fn index_level(
        &self,
        slug: &str,
    ) -> Result<Option<rust_decimal::Decimal>, ProviderError> {
        self.0.index_level(slug).await
    }

    async fn fno_underlyings(&self) -> Result<Vec<String>, ProviderError> {
        self.0.fno_underlyings().await
    }
}

/// The Kite adapter as the composite registers it: bars, the broker ledger, and its two
/// duck-typed reads.
#[must_use]
pub fn register_kite(provider: KiteProvider) -> Registered {
    let shared = Arc::new(provider);
    Registered::new(shared.clone())
        .with_bars(shared.clone())
        .with_holdings(shared.clone())
        .with_supplementary(Arc::new(KiteReads(shared)))
}

/// `build_archive`: R2/S3 when configured, otherwise a directory on disk. Local development still
/// goes through the archive-then-parse path. An explicit `local_root` (tests) wins, then the
/// configured directory, then the relative default.
///
/// # Errors
/// `CredentialsMissing` for an empty bucket; `UpstreamUnavailable` when the S3 HTTP client
/// cannot be built.
pub fn build_archive(
    settings: &ProviderSettings,
    local_root: Option<&Path>,
) -> Result<Arc<dyn RawArchive>, ProviderError> {
    if settings.s3_configured() {
        let client = SigV4S3Client::configure(
            &settings.s3_endpoint_url,
            &settings.s3_region,
            settings.s3_access_key_id.clone(),
            settings.s3_secret_access_key.clone(),
        )?;
        return Ok(Arc::new(S3RawArchive::new(
            Arc::new(client),
            &settings.s3_bucket,
        )?));
    }
    let root: PathBuf = match local_root {
        Some(r) => r.to_path_buf(),
        None if !settings.raw_archive_dir.is_empty() => PathBuf::from(&settings.raw_archive_dir),
        None => PathBuf::from(LOCAL_ARCHIVE_DIRNAME),
    };
    Ok(Arc::new(LocalRawArchive::new(root)))
}

/// `build_nse_provider`: the token bucket on [`NSE_RATE_LIMIT_KEY`], the browser client only when
/// the bucket exists, and the given archive or [`build_archive`]'s.
///
/// # Errors
/// As [`build_archive`], [`build_http_client`] and [`NseProvider::new`].
pub async fn build_nse_provider(
    source: &dyn BackendSource,
    settings: &ProviderSettings,
    archive: Option<Arc<dyn RawArchive>>,
    retry_hooks: Option<RetryHooks>,
) -> Result<NseProvider, ProviderError> {
    let limiter = build_rate_limiter(
        source,
        NSE_RATE_LIMIT_KEY,
        settings.nse_rate_limit_per_second,
    )
    .await?;
    let client = match &limiter {
        Some(_) => Some(build_http_client(settings)?),
        None => None,
    };
    let archive = match archive {
        Some(a) => a,
        None => build_archive(settings, None)?,
    };
    NseProvider::new(
        settings.clone(),
        Some(archive),
        NseRuntime {
            client,
            rate_limiter: limiter.map(|l| l as Arc<dyn RateLimiter>),
            retry_policy: None,
            retry_hooks,
            clock: None,
        },
    )
}

/// `build_fixture_provider`: `None` when no fixtures are present, so the doctor can say so.
#[must_use]
pub fn build_fixture_provider(settings: &ProviderSettings) -> Option<FixtureProvider> {
    let directory = if settings.fixture_dir.is_empty() {
        default_fixture_dir().ok()?
    } else {
        PathBuf::from(&settings.fixture_dir)
    };
    Some(FixtureProvider::new(directory))
}

/// The NSE adapter as the composite registers it.
#[must_use]
pub fn register_nse(provider: NseProvider) -> Registered {
    let shared = Arc::new(provider);
    Registered::new(shared.clone())
        .with_reference(shared.clone())
        .with_supplementary(shared)
}

/// The fixture adapter as the composite registers it (bars and reference).
#[must_use]
pub fn register_fixtures(provider: FixtureProvider) -> Registered {
    let shared = Arc::new(provider);
    Registered::new(shared.clone())
        .with_bars(shared.clone())
        .with_reference(shared.clone())
        .with_supplementary(shared)
}

/// `build_provider_stack`: the production wiring — Kite on the **bulk** lane (this stack runs
/// the nightly chain and the backfills, thousands of `historical_data` calls; M85), NSE, then
/// fixtures. `retry_hooks` is Prompt 17's observation seam.
///
/// # Errors
/// As [`build_kite_provider`] and [`build_provider_stack_with`].
pub async fn build_provider_stack(
    source: &dyn BackendSource,
    settings: &ProviderSettings,
    retry_hooks: Option<RetryHooks>,
) -> Result<CompositeProvider, ProviderError> {
    let kite = build_kite_provider(source, settings, retry_hooks.clone(), KiteLane::Bulk).await?;
    build_provider_stack_with(source, settings, retry_hooks, register_kite(kite)).await
}

/// `build_provider_stack`, with the bars adapter supplied: Kite for bars, NSE for reference,
/// fixtures last. Registration order is preference order, so fixtures only serve what the real
/// vendors could not — in a configured production environment nothing, in local development
/// everything. `kite` should be built for the **bulk** lane: this stack runs the nightly chain
/// and the backfills (M85).
///
/// `retry_hooks` is Prompt 17's observation seam (the worker counts every backoff).
///
/// # Errors
/// As [`build_nse_provider`] and [`CompositeProvider::new`].
pub async fn build_provider_stack_with(
    source: &dyn BackendSource,
    settings: &ProviderSettings,
    retry_hooks: Option<RetryHooks>,
    kite: Registered,
) -> Result<CompositeProvider, ProviderError> {
    let mut providers = vec![
        kite,
        register_nse(build_nse_provider(source, settings, None, retry_hooks).await?),
    ];
    if let Some(fixtures) = build_fixture_provider(settings) {
        providers.push(register_fixtures(fixtures));
    }
    let threshold = u32::try_from(settings.provider_circuit_failure_threshold).map_err(|_| {
        ProviderError::value_error(format!(
            "failure_threshold must be at least 1; got {}",
            settings.provider_circuit_failure_threshold
        ))
    })?;
    CompositeProvider::new(
        providers,
        threshold,
        settings.provider_circuit_reset_seconds,
        None,
    )
}
