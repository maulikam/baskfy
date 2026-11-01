//! `CompositeProvider` (Prompt 2 deliverable 4) — the routing, failover, circuit-breaking and
//! health-aggregation cases of `test_composite.py`, case for case (the breaker-only cases are in
//! `circuit_and_publication.rs`), plus the duck-typed delegations Python's composite carries.
//!
//! docs/02: "Adding a paid vendor later is a new adapter, not a rewrite" — which only holds if
//! call sites depend on capabilities rather than on vendors, so that is what these assert.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use baskfy_providers::circuit::CircuitState;
use baskfy_providers::composite::{CompositeProvider, Registered, SupplementaryReads};
use baskfy_providers::errors::{ErrorKind, ProviderError};
use baskfy_providers::ports::{
    BarsProvider, Capability, CapabilitySet, HealthReporting, ProviderHealth, ReferenceProvider,
    bars_capabilities, holdings_capabilities, reference_capabilities,
};
use baskfy_providers::records::{
    BrokerAccountFields, BrokerAccountRef, CorporateAction, EquityFundamental, IndexSnapshot,
    IndexSnapshotFields, InstrumentFields, InstrumentRecord, InstrumentType, ListingFields,
    ListingRecord, daily_bars_schema, empty_frame,
};
use chrono::NaiveDate;
use common::arm;
use polars::prelude::DataFrame;

fn on() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 18).unwrap()
}

/// `StubProvider`: advertises a capability set and answers however the test wants.
struct Stub {
    name: String,
    capabilities: CapabilitySet,
    raises: Option<fn() -> ProviderError>,
    available: bool,
    check_panics: bool,
    etf: Vec<String>,
    calls: AtomicUsize,
}

impl Stub {
    fn new(name: &str, capabilities: CapabilitySet) -> Self {
        Self {
            name: name.to_owned(),
            capabilities,
            raises: None,
            available: true,
            check_panics: false,
            etf: Vec::new(),
            calls: AtomicUsize::new(0),
        }
    }

    fn raising(mut self, error: fn() -> ProviderError) -> Self {
        self.raises = Some(error);
        self
    }

    fn unavailable(mut self) -> Self {
        self.available = false;
        self
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    fn answer(&self) -> Result<(), ProviderError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        match self.raises {
            Some(e) => Err(e()),
            None => Ok(()),
        }
    }
}

impl HealthReporting for Stub {
    fn name(&self) -> &str {
        &self.name
    }

    fn capabilities(&self) -> CapabilitySet {
        self.capabilities.clone()
    }

    #[allow(clippy::manual_assert)]
    fn check(&self) -> ProviderHealth {
        if self.check_panics {
            panic!("boom");
        }
        ProviderHealth {
            name: self.name.clone(),
            available: self.available,
            capabilities: self.capabilities.clone(),
            detail: "stub".to_owned(),
        }
    }
}

#[async_trait]
impl BarsProvider for Stub {
    async fn list_instruments(&self) -> Result<Vec<InstrumentRecord>, ProviderError> {
        self.answer()?;
        let mut f = InstrumentFields::new("SBIN", "SBIN", InstrumentType::Eq);
        f.series = Some("EQ".to_owned());
        Ok(vec![InstrumentRecord::new(f).unwrap()])
    }

    async fn daily_bars(
        &self,
        _token: i64,
        _start: NaiveDate,
        _end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        self.answer()?;
        Ok(empty_frame(&daily_bars_schema()))
    }
}

#[async_trait]
impl ReferenceProvider for Stub {
    async fn index_constituents(
        &self,
        _: &str,
        _: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        self.answer()?;
        Ok(vec!["SBIN".to_owned()])
    }

    async fn index_snapshots(&self, on: NaiveDate) -> Result<Vec<IndexSnapshot>, ProviderError> {
        self.answer()?;
        Ok(vec![
            IndexSnapshot::new(IndexSnapshotFields::new("nifty-50", on)).unwrap(),
        ])
    }

    async fn corporate_actions(&self, _: NaiveDate) -> Result<Vec<CorporateAction>, ProviderError> {
        self.answer()?;
        Ok(Vec::new())
    }

    async fn listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        self.answer()?;
        Ok(vec![
            ListingRecord::new(ListingFields::new("SBIN", "SBIN")).unwrap(),
        ])
    }

    async fn bhavcopy(&self, _: NaiveDate) -> Result<DataFrame, ProviderError> {
        self.answer()?;
        Ok(empty_frame(&daily_bars_schema()))
    }

    async fn equity_fundamentals(
        &self,
        _: NaiveDate,
        _: &[String],
        _: Option<&HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        self.answer()?;
        Ok(Vec::new())
    }
}

#[async_trait]
impl SupplementaryReads for Stub {
    async fn etf_symbols(&self) -> Result<Vec<String>, ProviderError> {
        self.answer()?;
        Ok(self.etf.clone())
    }
}

/// Registers a stub with every port it implements, as Python's `isinstance` would find them.
fn reg(stub: &Arc<Stub>) -> Registered {
    Registered::new(stub.clone())
        .with_bars(stub.clone())
        .with_reference(stub.clone())
        .with_supplementary(stub.clone())
}

fn composite(stubs: &[&Arc<Stub>]) -> CompositeProvider {
    CompositeProvider::with_defaults(stubs.iter().map(|s| reg(s)).collect()).unwrap()
}

fn stub(name: &str, caps: CapabilitySet) -> Arc<Stub> {
    Arc::new(Stub::new(name, caps))
}

fn upstream() -> ProviderError {
    ProviderError::upstream_unavailable("503")
}

fn no_key() -> ProviderError {
    ProviderError::credentials_missing("no key")
}

fn union(a: CapabilitySet, b: CapabilitySet) -> CapabilitySet {
    a.into_iter().chain(b).collect()
}

// --- TestRouting -----------------------------------------------------------------------------

#[tokio::test]
async fn a_bars_call_goes_to_the_bars_provider() {
    arm();
    let kite = stub("kite", bars_capabilities());
    let nse = stub("nse", reference_capabilities());
    composite(&[&kite, &nse]).list_instruments().await.unwrap();
    assert_eq!((kite.calls(), nse.calls()), (1, 0));
}

#[tokio::test]
async fn a_reference_call_goes_to_the_reference_provider() {
    arm();
    let kite = stub("kite", bars_capabilities());
    let nse = stub("nse", reference_capabilities());
    composite(&[&kite, &nse]).listings().await.unwrap();
    assert_eq!((kite.calls(), nse.calls()), (0, 1));
}

#[tokio::test]
async fn registration_order_is_preference_order() {
    arm();
    let primary = stub("primary", reference_capabilities());
    let fallback = stub("fallback", reference_capabilities());
    composite(&[&primary, &fallback]).listings().await.unwrap();
    assert_eq!((primary.calls(), fallback.calls()), (1, 0));
}

#[tokio::test]
async fn an_unserved_capability_names_what_was_registered() {
    arm();
    let err = composite(&[&stub("kite", bars_capabilities())])
        .listings()
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CapabilityNotAvailable);
    assert!(err.to_string().contains("kite"));
    assert_eq!(
        err.to_string(),
        "[composite] no registered provider offers 'listings'; registered: ['kite']"
    );
}

#[test]
fn capabilities_are_the_union_of_the_registered_adapters() {
    arm();
    let c = composite(&[
        &stub("kite", bars_capabilities()),
        &stub("nse", reference_capabilities()),
    ]);
    assert_eq!(
        c.capabilities(),
        union(bars_capabilities(), reference_capabilities())
    );
}

/// Names key the breakers; two providers sharing one would share a circuit.
#[test]
fn duplicate_provider_names_are_rejected() {
    arm();
    let err = CompositeProvider::with_defaults(vec![
        reg(&stub("nse", reference_capabilities())),
        reg(&stub("nse", reference_capabilities())),
    ])
    .unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert!(err.to_string().contains("unique"));
    assert_eq!(
        err.message(),
        "provider names must be unique; got ['nse', 'nse']"
    );
}

#[test]
fn providers_for_lists_candidates_in_order() {
    arm();
    let c = composite(&[
        &stub("primary", reference_capabilities()),
        &stub("fallback", reference_capabilities()),
    ]);
    let names: Vec<String> = c
        .providers_for(Capability::Listings)
        .iter()
        .map(|p| p.name().to_owned())
        .collect();
    assert_eq!(names, ["primary", "fallback"]);
}

// --- TestFailover ----------------------------------------------------------------------------

/// "This provider cannot serve" is exactly when a fallback is correct.
#[tokio::test]
async fn an_unavailable_provider_falls_through_to_the_next() {
    arm();
    let primary = Arc::new(Stub::new("primary", reference_capabilities()).raising(no_key));
    let fallback = stub("fallback", reference_capabilities());
    let result = composite(&[&primary, &fallback]).listings().await.unwrap();
    let symbols: Vec<&str> = result.iter().map(|r| r.symbol.as_str()).collect();
    assert_eq!(symbols, ["SBIN"]);
    assert_eq!((primary.calls(), fallback.calls()), (1, 1));
}

/// The one ProviderUnavailable that must stop the routing instead of falling through: the next
/// bars provider in the real stack is FixtureProvider, real symbols with synthetic prices.
#[tokio::test]
async fn a_stale_access_token_is_never_substituted_for() {
    arm();
    let primary = Arc::new(
        Stub::new("kite", bars_capabilities())
            .raising(|| ProviderError::access_token_expired(Some("token is from yesterday"))),
    );
    let fallback = stub("fixture", bars_capabilities());
    let err = composite(&[&primary, &fallback])
        .daily_bars(1, on(), on())
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::AccessTokenExpired);
    assert_eq!(
        fallback.calls(),
        0,
        "the fixture provider must never see a bars call"
    );
}

/// *Configured but stale* against *not configured at all*: docs/03's `local` environment.
#[tokio::test]
async fn missing_credentials_still_fall_through_to_fixtures() {
    arm();
    let primary = Arc::new(Stub::new("kite", bars_capabilities()).raising(no_key));
    let fallback = stub("fixture", bars_capabilities());
    composite(&[&primary, &fallback])
        .daily_bars(1, on(), on())
        .await
        .unwrap();
    assert_eq!(fallback.calls(), 1);
}

/// A timeout means "slow", not "down"; the caller's own retry policy owns that decision.
#[tokio::test]
async fn a_transient_error_propagates_rather_than_failing_over() {
    arm();
    let primary = Arc::new(Stub::new("primary", reference_capabilities()).raising(upstream));
    let fallback = stub("fallback", reference_capabilities());
    let err = composite(&[&primary, &fallback])
        .listings()
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UpstreamUnavailable);
    assert_eq!(fallback.calls(), 0);
}

#[tokio::test]
async fn when_every_candidate_is_unavailable_the_reason_is_reported() {
    arm();
    let a = Arc::new(Stub::new("a", reference_capabilities()).raising(no_key));
    let b = Arc::new(
        Stub::new("b", reference_capabilities())
            .raising(|| ProviderError::provider_unavailable("no bucket")),
    );
    let err = composite(&[&a, &b]).listings().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CapabilityNotAvailable);
    let text = err.to_string();
    assert!(text.contains("no key"));
    assert!(text.contains("no bucket"));
    assert_eq!(
        text,
        "[composite] no provider could serve 'listings'. Tried: a: no key; b: no bucket"
    );
}

// --- TestCircuitBreaking (through the composite) ---------------------------------------------

#[tokio::test]
async fn repeated_transient_failures_open_the_circuit() {
    arm();
    let provider = Arc::new(Stub::new("nse", reference_capabilities()).raising(upstream));
    let c = CompositeProvider::new(vec![reg(&provider)], 3, 60.0, None).unwrap();
    for _ in 0..3 {
        let err = c.listings().await.unwrap_err();
        assert_eq!(err.kind(), ErrorKind::UpstreamUnavailable);
    }
    assert_eq!(c.breaker("nse").unwrap().state(), CircuitState::Open);
}

/// The point: a dead upstream must not consume the whole nightly window.
#[tokio::test]
async fn an_open_circuit_stops_calling_the_provider() {
    arm();
    let provider = Arc::new(Stub::new("nse", reference_capabilities()).raising(upstream));
    let clock = common::ManualClock::at(0.0);
    let c = CompositeProvider::new(vec![reg(&provider)], 2, 60.0, Some(clock)).unwrap();
    for _ in 0..2 {
        c.listings().await.unwrap_err();
    }
    let tripped = provider.calls();
    let err = c.listings().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CapabilityNotAvailable);
    assert!(err.to_string().contains("circuit open"));
    assert_eq!(
        err.message(),
        "no provider could serve 'listings'. Tried: nse: circuit open for 60s"
    );
    assert_eq!(provider.calls(), tripped);
}

#[tokio::test]
async fn a_tripped_primary_fails_over_to_a_healthy_fallback() {
    arm();
    let primary = Arc::new(
        Stub::new("primary", reference_capabilities())
            .raising(|| ProviderError::upstream_unavailable("x")),
    );
    let fallback = stub("fallback", reference_capabilities());
    let c = CompositeProvider::new(vec![reg(&primary), reg(&fallback)], 1, 60.0, None).unwrap();
    assert_eq!(
        c.listings().await.unwrap_err().kind(),
        ErrorKind::UpstreamUnavailable
    );
    assert!(!c.listings().await.unwrap().is_empty());
    assert_eq!(fallback.calls(), 1);
}

#[test]
fn an_unknown_breaker_is_a_key_error() {
    arm();
    let c = composite(&[&stub("nse", reference_capabilities())]);
    let err = c.breaker("kite").unwrap_err();
    assert_eq!(err.python_type(), "KeyError");
    assert_eq!(err.message(), "no provider registered under 'kite'");
}

// --- TestHealthAggregation -------------------------------------------------------------------

#[test]
fn it_reports_one_line_per_provider() {
    arm();
    let c = composite(&[
        &Arc::new(Stub::new("kite", bars_capabilities()).unavailable()),
        &stub("nse", reference_capabilities()),
    ]);
    let reports: Vec<(String, bool)> = c
        .health_reports()
        .into_iter()
        .map(|r| (r.name, r.available))
        .collect();
    assert_eq!(
        reports,
        [("kite".to_owned(), false), ("nse".to_owned(), true)]
    );
}

/// `providers doctor` exists to report failure; it must not be taken down by one. A Rust
/// `check()` cannot raise, so the failure contained is a panic (Python's is `RuntimeError`).
#[test]
fn an_adapter_whose_health_check_panics_still_produces_a_line() {
    arm();
    let broken = Arc::new(Stub {
        check_panics: true,
        ..Stub::new("broken", bars_capabilities())
    });
    let report = composite(&[&broken]).health_reports().remove(0);
    assert!(!report.available);
    assert_eq!(report.name, "broken");
    assert!(report.capabilities.is_empty());
    assert_eq!(report.detail, "health check raised panic: boom");
}

#[test]
fn an_unavailable_provider_serves_nothing() {
    arm();
    let health = ProviderHealth {
        name: "kite".to_owned(),
        available: false,
        capabilities: bars_capabilities(),
        detail: String::new(),
    };
    assert!(health.served_capabilities().is_empty());
}

#[test]
fn the_composite_is_available_when_any_provider_is() {
    arm();
    let c = composite(&[
        &Arc::new(Stub::new("kite", bars_capabilities()).unavailable()),
        &stub("nse", reference_capabilities()),
    ]);
    let health = c.check();
    assert!(health.available);
    assert_eq!(health.detail, "kite=down, nse=up");
    assert_eq!(health.name, "composite");
}

#[test]
fn the_composite_is_unavailable_when_none_are() {
    arm();
    let c = composite(&[&Arc::new(
        Stub::new("kite", bars_capabilities()).unavailable(),
    )]);
    assert!(!c.check().available);
    let empty = CompositeProvider::with_defaults(Vec::new()).unwrap();
    assert_eq!(empty.check().detail, "no providers registered");
}

// --- The duck-typed delegations --------------------------------------------------------------

/// `etf_symbols` routes on LISTINGS to the first provider that has it; a provider without one
/// answers `[]` (Python's `getattr(p, "etf_symbols", list)`).
#[tokio::test]
async fn etf_symbols_and_sme_listings_are_delegated_or_empty() {
    arm();
    let nse = Arc::new(Stub {
        etf: vec!["NIFTYBEES".to_owned()],
        ..Stub::new("nse", reference_capabilities())
    });
    assert_eq!(
        composite(&[&nse]).etf_symbols().await.unwrap(),
        ["NIFTYBEES"]
    );
    let bare = stub("bare", reference_capabilities());
    let c = CompositeProvider::with_defaults(vec![
        Registered::new(bare.clone()).with_reference(bare.clone()),
    ])
    .unwrap();
    assert!(c.etf_symbols().await.unwrap().is_empty());
    assert!(c.sme_listings().await.unwrap().is_empty());
    assert_eq!(bare.calls(), 0);
}

/// `index_level` routes on DAILY_BARS; `fno_underlyings` on LIST_INSTRUMENTS; absent, `None`
/// and `[]`.
#[tokio::test]
async fn index_level_and_fno_underlyings_fall_back_to_nothing() {
    arm();
    let kite = stub("kite", bars_capabilities());
    let c = composite(&[&kite]);
    assert_eq!(c.index_level("nifty-50").await.unwrap(), None);
    assert!(c.fno_underlyings().await.unwrap().is_empty());
}

/// An adapter advertising a capability whose port it does not implement is
/// `ProviderUnavailable`, so the routing moves on.
#[tokio::test]
async fn an_advertised_but_unimplemented_port_falls_through() {
    arm();
    let liar = stub("liar", reference_capabilities());
    let honest = stub("honest", reference_capabilities());
    let c = CompositeProvider::with_defaults(vec![Registered::new(liar), reg(&honest)]).unwrap();
    assert_eq!(c.listings().await.unwrap().len(), 1);
    assert_eq!(honest.calls(), 1);
}

/// Nothing registers a fallback holdings adapter: a dead broker session is
/// `CapabilityNotAvailable`, never invented positions.
#[tokio::test]
async fn holdings_have_no_fallback() {
    arm();
    let c = composite(&[
        &stub("kite", bars_capabilities()),
        &stub(
            "fixture",
            union(bars_capabilities(), reference_capabilities()),
        ),
    ]);
    assert!(
        !c.capabilities()
            .iter()
            .any(|cap| holdings_capabilities().contains(cap))
    );
    let account = BrokerAccountRef::new(BrokerAccountFields {
        broker_account_id: 7,
        broker_id: "zerodha".to_owned(),
        kite_user_id: None,
    })
    .unwrap();
    let err = c.broker_holdings(&account).await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CapabilityNotAvailable);
}
