//! `factory` — the stack's wiring: which Redis keys each limiter waits on and at what rate, which
//! archive is built, how the NSE adapter degrades without Redis, and the stack's registration
//! order (Kite, NSE, fixtures; no holdings fallback). Python's `factory.py` has no test file of
//! its own; these assert its docstrings and the desk's key contract (`kite_limits.py`).
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

mod common;

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use baskfy_providers::composite::Registered;
use baskfy_providers::factory::{
    BULK_MAX_WAIT_SECONDS, BackendSource, INTERACTIVE_MAX_WAIT_SECONDS, KITE_BULK_CLOCK_KEY,
    KITE_CLOCK_PREFIX, KITE_READ_CLOCK_KEY, KiteFamily, KiteLane, LOCAL_ARCHIVE_DIRNAME,
    NSE_RATE_LIMIT_KEY, RedisSource, build_archive, build_fixture_provider,
    build_kite_family_limiter, build_kite_provider, build_kite_read_limiter, build_nse_provider,
    build_provider_stack, build_provider_stack_with, build_rate_limiter, build_spaced_rate_limiter,
    kite_family_key,
};
use baskfy_providers::ports::{
    Capability, CapabilitySet, HealthReporting, HoldingsProvider, ProviderHealth,
    bars_capabilities, holdings_capabilities,
};
use baskfy_providers::ratelimit::{InMemoryScriptBackend, ScriptBackend};
use baskfy_providers::records::{BrokerAccountRef, BrokerHoldingRecord};
use baskfy_providers::secret::Secret;
use baskfy_providers::settings::ProviderSettings;
use common::{ManualClock, arm};
use rust_decimal::Decimal;

/// Hands out an in-memory Redis for the first `up` asks, then reports Redis gone.
struct Source {
    up: usize,
    asked: AtomicUsize,
}

impl Source {
    fn up(n: usize) -> Self {
        Self {
            up: n,
            asked: AtomicUsize::new(0),
        }
    }
}

#[async_trait]
impl BackendSource for Source {
    async fn backend(&self) -> Option<Arc<dyn ScriptBackend>> {
        let n = self.asked.fetch_add(1, Ordering::SeqCst);
        (n < self.up).then(|| {
            Arc::new(InMemoryScriptBackend::new(ManualClock::at(0.0))) as Arc<dyn ScriptBackend>
        })
    }
}

fn settings() -> ProviderSettings {
    ProviderSettings::default()
}

// --- the desk's key contract -----------------------------------------------------------------

#[test]
fn the_keys_and_rates_are_the_desks() {
    arm();
    assert_eq!(KITE_READ_CLOCK_KEY, "baskfy:ratelimit:kite:read");
    assert_eq!(KITE_BULK_CLOCK_KEY, "baskfy:ratelimit:kite:bulk");
    assert_eq!(KITE_CLOCK_PREFIX, "baskfy:ratelimit:kite");
    assert_eq!(NSE_RATE_LIMIT_KEY, "baskfy:ratelimit:nse");
    assert_eq!(
        kite_family_key(KiteFamily::Quote),
        "baskfy:ratelimit:kite:quote"
    );
    assert_eq!(
        kite_family_key(KiteFamily::Historical),
        "baskfy:ratelimit:kite:historical"
    );
    assert_eq!(
        kite_family_key(KiteFamily::General),
        "baskfy:ratelimit:kite:general"
    );
    // `RATE_FOR_FAMILY`: quote 1/s, historical 3/s, general 9/s.
    assert_eq!(KiteFamily::Quote.rate_per_second(), 1.0);
    assert_eq!(KiteFamily::Historical.rate_per_second(), 3.0);
    assert_eq!(KiteFamily::General.rate_per_second(), 9.0);
    assert_eq!(INTERACTIVE_MAX_WAIT_SECONDS, 30.0);
    assert_eq!(BULK_MAX_WAIT_SECONDS, 300.0);
    assert_eq!(LOCAL_ARCHIVE_DIRNAME, ".archive");
}

/// A call site nobody classified is throttled as interactive, never the one starving somebody.
#[test]
fn the_default_lane_is_interactive() {
    arm();
    assert_eq!(KiteLane::default(), KiteLane::Interactive);
    assert_eq!(KiteLane::Bulk.value(), "bulk");
}

// --- the limiter builders --------------------------------------------------------------------

/// An unreachable Redis is "this provider is not available", never a stack trace.
#[tokio::test]
async fn no_redis_means_no_limiter() {
    arm();
    let gone = Source::up(0);
    assert!(
        build_rate_limiter(&gone, NSE_RATE_LIMIT_KEY, 1.0)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        build_spaced_rate_limiter(&gone, KITE_READ_CLOCK_KEY, 3.0, 30.0)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        build_kite_read_limiter(&gone, &settings(), KiteLane::Bulk)
            .await
            .unwrap()
            .is_none()
    );
}

/// The NSE bucket holds one second's worth: capacity equals the rate, no burst beyond it.
#[tokio::test]
async fn the_token_bucket_has_one_seconds_capacity() {
    arm();
    let bucket = build_rate_limiter(&Source::up(1), NSE_RATE_LIMIT_KEY, 1.0)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(bucket.key(), NSE_RATE_LIMIT_KEY);
    assert_eq!(bucket.config().rate_per_second, 1.0);
    assert_eq!(bucket.config().capacity, 1.0);
    // Python raises ValueError for a non-positive rate, Redis or not.
    let Err(err) = build_rate_limiter(&Source::up(1), NSE_RATE_LIMIT_KEY, 0.0).await else {
        panic!("a zero rate was accepted");
    };
    assert_eq!(err.python_type(), "ValueError");
}

#[tokio::test]
async fn an_interactive_read_waits_on_the_ceiling_alone() {
    arm();
    let limiter = build_kite_read_limiter(&Source::up(9), &settings(), KiteLane::Interactive)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(limiter.keys(), [KITE_READ_CLOCK_KEY]);
    let config = limiter.configs()[0];
    assert_eq!(config.rate_per_second, 3.0);
    assert_eq!(config.max_wait_seconds, INTERACTIVE_MAX_WAIT_SECONDS);
}

/// M85: bulk takes its own clock first, then the box's ceiling.
#[tokio::test]
async fn a_bulk_read_takes_its_lane_then_the_ceiling() {
    arm();
    let limiter = build_kite_read_limiter(&Source::up(9), &settings(), KiteLane::Bulk)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(limiter.keys(), [KITE_BULK_CLOCK_KEY, KITE_READ_CLOCK_KEY]);
    let configs = limiter.configs();
    assert_eq!(configs[0].rate_per_second, 2.0);
    assert_eq!(configs[0].max_wait_seconds, BULK_MAX_WAIT_SECONDS);
    assert_eq!(configs[1].rate_per_second, 3.0);
    assert_eq!(configs[1].max_wait_seconds, BULK_MAX_WAIT_SECONDS);
}

/// The ceiling alone is still correct if the lane clock cannot be built.
#[tokio::test]
async fn a_lost_lane_clock_leaves_the_ceiling() {
    arm();
    let limiter = build_kite_read_limiter(&Source::up(1), &settings(), KiteLane::Bulk)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(limiter.keys(), [KITE_READ_CLOCK_KEY]);
    assert_eq!(limiter.configs()[0].max_wait_seconds, BULK_MAX_WAIT_SECONDS);
}

/// OP3: lane, then the `read` ceiling, then the family's own cap — the desk's order.
#[tokio::test]
async fn a_family_read_takes_the_ceiling_then_its_family_cap() {
    arm();
    let quote = build_kite_family_limiter(
        &Source::up(9),
        &settings(),
        KiteFamily::Quote,
        KiteLane::Interactive,
    )
    .await
    .unwrap()
    .unwrap();
    let keys: Vec<&str> = quote.spacers().iter().map(|s| s.key()).collect();
    assert_eq!(keys, [KITE_READ_CLOCK_KEY, "baskfy:ratelimit:kite:quote"]);
    assert_eq!(quote.spacers()[1].config().rate_per_second, 1.0);
    let bulk = build_kite_family_limiter(
        &Source::up(9),
        &settings(),
        KiteFamily::Historical,
        KiteLane::Bulk,
    )
    .await
    .unwrap()
    .unwrap();
    let keys: Vec<&str> = bulk.spacers().iter().map(|s| s.key()).collect();
    assert_eq!(
        keys,
        [
            KITE_BULK_CLOCK_KEY,
            KITE_READ_CLOCK_KEY,
            "baskfy:ratelimit:kite:historical"
        ]
    );
    assert!(
        bulk.spacers()
            .iter()
            .all(|s| s.config().max_wait_seconds == BULK_MAX_WAIT_SECONDS)
    );
}

/// The quote cap is the tightest; silently losing it is the failure the builder exists to
/// prevent, so a missing clock is no limiter at all.
#[tokio::test]
async fn a_missing_family_clock_is_no_limiter() {
    arm();
    let partial = build_kite_family_limiter(
        &Source::up(1),
        &settings(),
        KiteFamily::Quote,
        KiteLane::Interactive,
    )
    .await
    .unwrap();
    assert!(partial.is_none());
}

/// The production source refuses a remote Redis while the block is armed, and answers `None`
/// for a closed loopback port: no limiter, no stack trace.
#[tokio::test]
async fn the_redis_source_degrades_to_none() {
    arm();
    let mut s = settings();
    s.redis_url = Secret::new("redis://redis.example.com:6379/0");
    assert!(RedisSource::from_settings(&s).backend().await.is_none());
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    s.redis_url = Secret::new(format!("redis://127.0.0.1:{port}/0"));
    let source = RedisSource::from_settings(&s).with_timeout(std::time::Duration::from_millis(300));
    assert!(source.backend().await.is_none());
}

// --- the archive -----------------------------------------------------------------------------

#[test]
fn the_local_archive_root_follows_its_precedence() {
    arm();
    let mut s = settings();
    assert_eq!(
        build_archive(&s, None).unwrap().describe(),
        "local directory .archive"
    );
    s.raw_archive_dir = "/var/lib/baskfy/raw".to_owned();
    assert_eq!(
        build_archive(&s, None).unwrap().describe(),
        "local directory /var/lib/baskfy/raw"
    );
    assert_eq!(
        build_archive(&s, Some(std::path::Path::new("/tmp/explicit")))
            .unwrap()
            .describe(),
        "local directory /tmp/explicit"
    );
}

#[test]
fn a_configured_bucket_builds_the_s3_archive() {
    arm();
    let mut s = settings();
    s.s3_bucket = "baskfy-raw".to_owned();
    s.s3_access_key_id = Secret::new("AKIA-TEST");
    s.s3_secret_access_key = Secret::new("not-a-secret");
    s.s3_endpoint_url = "https://example.r2.cloudflarestorage.com".to_owned();
    assert!(s.s3_configured());
    assert_eq!(
        build_archive(&s, None).unwrap().describe(),
        "s3://baskfy-raw"
    );
}

// --- the NSE adapter -------------------------------------------------------------------------

/// No Redis: no limiter, and so no client — an unthrottled NSE client is never built.
#[tokio::test]
async fn without_redis_the_nse_adapter_reports_itself_unavailable() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = build_archive(&settings(), Some(dir.path())).unwrap();
    let nse = build_nse_provider(&Source::up(0), &settings(), Some(archive), None)
        .await
        .unwrap();
    let health = nse.check();
    assert!(!health.available);
    assert_eq!(
        health.detail,
        "no HTTP client configured; no shared rate limiter configured; NSE public files are \
         rate-sensitive"
    );
}

#[tokio::test]
async fn with_redis_the_nse_adapter_is_wired() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let archive = build_archive(&settings(), Some(dir.path())).unwrap();
    let nse = build_nse_provider(&Source::up(1), &settings(), Some(archive), None)
        .await
        .unwrap();
    let health = nse.check();
    assert!(health.available, "{}", health.detail);
    assert!(health.detail.starts_with("archiving to local directory "));
}

#[test]
fn the_fixture_provider_follows_its_setting() {
    arm();
    let mut s = settings();
    s.fixture_dir = "/somewhere/else".to_owned();
    assert_eq!(
        build_fixture_provider(&s).unwrap().directory(),
        std::path::Path::new("/somewhere/else")
    );
    let found = build_fixture_provider(&settings()).unwrap();
    assert!(found.directory().ends_with("tests/fixtures/providers"));
}

// --- the stack -------------------------------------------------------------------------------

/// A stand-in for `KiteProvider`: bars and the broker ledger.
struct Kite;

impl HealthReporting for Kite {
    fn name(&self) -> &str {
        "kite"
    }

    fn capabilities(&self) -> CapabilitySet {
        bars_capabilities()
            .into_iter()
            .chain(holdings_capabilities())
            .collect()
    }

    fn check(&self) -> ProviderHealth {
        ProviderHealth {
            name: "kite".to_owned(),
            available: false,
            capabilities: self.capabilities(),
            detail: "stand-in".to_owned(),
        }
    }
}

#[async_trait]
impl HoldingsProvider for Kite {
    async fn broker_holdings(
        &self,
        _: &BrokerAccountRef,
    ) -> Result<Vec<BrokerHoldingRecord>, baskfy_providers::ProviderError> {
        Ok(Vec::new())
    }

    async fn broker_cash(
        &self,
        _: &BrokerAccountRef,
    ) -> Result<Option<Decimal>, baskfy_providers::ProviderError> {
        Ok(None)
    }
}

/// Kite first, NSE second, fixtures last — preference order — and `build_provider_stack` must
/// never make invented holdings reachable in production (`test_holdings_port.py`).
#[tokio::test]
async fn the_stack_is_kite_nse_fixtures_with_no_holdings_fallback() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let mut s = settings();
    s.raw_archive_dir = dir.path().display().to_string();
    s.provider_circuit_failure_threshold = 7;
    let kite = Arc::new(Kite);
    let stack = build_provider_stack_with(
        &Source::up(1),
        &s,
        None,
        Registered::new(kite.clone()).with_holdings(kite),
    )
    .await
    .unwrap();
    let names: Vec<&str> = stack.providers().iter().map(Registered::name).collect();
    assert_eq!(names, ["kite", "nse", "fixture"]);
    let offering: Vec<String> = stack
        .providers_for(Capability::BrokerHoldings)
        .iter()
        .map(|p| p.name().to_owned())
        .collect();
    assert_eq!(offering, ["kite"]);
    let reference: Vec<String> = stack
        .providers_for(Capability::Listings)
        .iter()
        .map(|p| p.name().to_owned())
        .collect();
    assert_eq!(reference, ["nse", "fixture"]);
    let nse = stack.breaker("nse").unwrap();
    assert_eq!(nse.provider(), "nse");
}

/// The production wiring with the real Kite adapter: no Redis means no limiter, so both vendors
/// report themselves unavailable instead of calling upstream unthrottled, and the fixtures are
/// still last. Only Kite offers holdings.
#[tokio::test]
async fn the_production_stack_wires_kite_first_and_degrades_without_redis() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let mut s = settings();
    s.raw_archive_dir = dir.path().display().to_string();
    s.kite_token_path = dir.path().join("kite-token.enc").display().to_string();
    let stack = build_provider_stack(&Source::up(0), &s, None)
        .await
        .unwrap();
    let names: Vec<&str> = stack.providers().iter().map(Registered::name).collect();
    assert_eq!(names, ["kite", "nse", "fixture"]);
    let offering: Vec<String> = stack
        .providers_for(Capability::BrokerHoldings)
        .iter()
        .map(|p| p.name().to_owned())
        .collect();
    assert_eq!(offering, ["kite"]);
    let reports = stack.health_reports();
    assert!(!reports[0].available);
    assert!(!reports[1].available);
    let kite = build_kite_provider(&Source::up(9), &s, None, KiteLane::Interactive)
        .await
        .unwrap();
    assert_eq!(kite.name(), "kite");
}
