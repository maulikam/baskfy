//! `CompositeProvider` — routes by capability, one circuit breaker per provider (deliverable 4).
//! Ports `baskfy_providers.composite`.
//!
//! docs/09's table: "`CompositeProvider` — routes by capability; retries; per-provider circuit
//! breaker". Kite serves bars, NSE serves everything else, and "adding a paid vendor later is a
//! new adapter, not a rewrite". Call sites ask the composite for a capability; they never name a
//! vendor.
//!
//! Routing rules
//! -------------
//! * Providers are consulted in registration order, so the first registered provider that offers
//!   a capability and is not tripped wins. Registration order is a preference order.
//! * A provider whose breaker is open is skipped rather than tried, and only if *every*
//!   candidate is skipped does the call fail.
//! * The failure raised when nothing can serve names every provider considered and why it was
//!   not used.
//! * **A stale access token never falls through** — `AccessTokenExpired` is the one
//!   `ProviderUnavailable` that stops the routing rather than moving down the list.
//!
//! Python duck-types its providers (`isinstance(p, BarsProvider)`, `getattr(p, "sme_listings",
//! list)`). Rust registers each one as a [`Registered`]: its health face plus whichever port
//! traits it implements, and the duck-typed extras as [`SupplementaryReads`], whose default
//! methods are Python's `getattr` fallbacks.

use std::collections::HashMap;
use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::sync::Arc;

use async_trait::async_trait;
use chrono::NaiveDate;
use polars::prelude::DataFrame;
use rust_decimal::Decimal;

use crate::circuit::{CircuitBreaker, CircuitState};
use crate::clock::Clock;
use crate::errors::{ErrorKind, ProviderError};
use crate::ports::{
    BarsProvider, Capability, CapabilitySet, HealthReporting, HoldingsProvider, ProviderHealth,
    ReferenceProvider,
};
use crate::py::{fixed, repr_str};
use crate::records::{
    BrokerAccountRef, BrokerHoldingRecord, CorporateAction, EquityFundamental, IndexSnapshot,
    InstrumentRecord, ListingRecord,
};

/// `PROVIDER_NAME`.
pub const PROVIDER_NAME: &str = "composite";

/// The duck-typed reads the composite delegates (`getattr(p, name, fallback)`): a provider that
/// lacks one contributes Python's fallback — an empty list, or `None`.
#[async_trait]
pub trait SupplementaryReads: Send + Sync {
    /// `sme_listings` — the NSE Emerge register.
    ///
    /// # Errors
    /// The provider's own failure.
    async fn sme_listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        Ok(Vec::new())
    }

    /// `etf_symbols` — NSE's ETF security list.
    ///
    /// # Errors
    /// The provider's own failure.
    async fn etf_symbols(&self) -> Result<Vec<String>, ProviderError> {
        Ok(Vec::new())
    }

    /// `index_level` — today's live level for a benchmark index.
    ///
    /// # Errors
    /// The provider's own failure.
    async fn index_level(&self, _slug: &str) -> Result<Option<Decimal>, ProviderError> {
        Ok(None)
    }

    /// `fno_underlyings` — the F&O universe from the broker's instrument dump.
    ///
    /// # Errors
    /// The provider's own failure.
    async fn fno_underlyings(&self) -> Result<Vec<String>, ProviderError> {
        Ok(Vec::new())
    }
}

/// One registered adapter: its health face, and each port it implements (Python's `isinstance`
/// answers, fixed at registration).
#[derive(Clone)]
pub struct Registered {
    health: Arc<dyn HealthReporting>,
    bars: Option<Arc<dyn BarsProvider>>,
    reference: Option<Arc<dyn ReferenceProvider>>,
    holdings: Option<Arc<dyn HoldingsProvider>>,
    supplementary: Option<Arc<dyn SupplementaryReads>>,
}

impl std::fmt::Debug for Registered {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Registered")
            .field("name", &self.name())
            .field("bars", &self.bars.is_some())
            .field("reference", &self.reference.is_some())
            .field("holdings", &self.holdings.is_some())
            .field("supplementary", &self.supplementary.is_some())
            .finish()
    }
}

impl Registered {
    /// An adapter that only reports health; add its ports with the `with_*` methods.
    #[must_use]
    pub fn new(health: Arc<dyn HealthReporting>) -> Self {
        Self {
            health,
            bars: None,
            reference: None,
            holdings: None,
            supplementary: None,
        }
    }

    /// It implements [`BarsProvider`].
    #[must_use]
    pub fn with_bars(mut self, provider: Arc<dyn BarsProvider>) -> Self {
        self.bars = Some(provider);
        self
    }

    /// It implements [`ReferenceProvider`].
    #[must_use]
    pub fn with_reference(mut self, provider: Arc<dyn ReferenceProvider>) -> Self {
        self.reference = Some(provider);
        self
    }

    /// It implements [`HoldingsProvider`].
    #[must_use]
    pub fn with_holdings(mut self, provider: Arc<dyn HoldingsProvider>) -> Self {
        self.holdings = Some(provider);
        self
    }

    /// It has some of the duck-typed reads.
    #[must_use]
    pub fn with_supplementary(mut self, provider: Arc<dyn SupplementaryReads>) -> Self {
        self.supplementary = Some(provider);
        self
    }

    /// The adapter's name.
    #[must_use]
    pub fn name(&self) -> &str {
        self.health.name()
    }

    /// Its health face.
    #[must_use]
    pub fn health(&self) -> &Arc<dyn HealthReporting> {
        &self.health
    }

    /// `_bars`: the bars port, or `ProviderUnavailable` for an adapter that advertises bars
    /// without implementing them.
    ///
    /// # Errors
    /// As described.
    pub fn bars(&self) -> Result<Arc<dyn BarsProvider>, ProviderError> {
        self.bars.clone().ok_or_else(|| {
            ProviderError::provider_unavailable(format!(
                "{} advertises a bars capability but does not implement BarsProvider",
                self.name()
            ))
            .with_provider(self.name())
        })
    }

    /// `_reference`.
    ///
    /// # Errors
    /// `ProviderUnavailable` when the adapter does not implement [`ReferenceProvider`].
    pub fn reference(&self) -> Result<Arc<dyn ReferenceProvider>, ProviderError> {
        self.reference.clone().ok_or_else(|| {
            ProviderError::provider_unavailable(format!(
                "{} advertises a reference capability but does not implement ReferenceProvider",
                self.name()
            ))
            .with_provider(self.name())
        })
    }

    /// `_holdings`.
    ///
    /// # Errors
    /// `ProviderUnavailable` when the adapter does not implement [`HoldingsProvider`].
    pub fn holdings(&self) -> Result<Arc<dyn HoldingsProvider>, ProviderError> {
        self.holdings.clone().ok_or_else(|| {
            ProviderError::provider_unavailable(format!(
                "{} advertises a holdings capability but does not implement HoldingsProvider",
                self.name()
            ))
            .with_provider(self.name())
        })
    }

    fn supplementary(&self) -> Option<Arc<dyn SupplementaryReads>> {
        self.supplementary.clone()
    }
}

/// Implements every port by delegating each call to whichever adapter can serve it
/// (`CompositeProvider`).
pub struct CompositeProvider {
    providers: Vec<Registered>,
    breakers: HashMap<String, CircuitBreaker>,
}

impl std::fmt::Debug for CompositeProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CompositeProvider")
            .field("providers", &self.providers)
            .finish_non_exhaustive()
    }
}

fn py_names(names: &[String]) -> String {
    let inner: Vec<String> = names.iter().map(|n| repr_str(n)).collect();
    format!("[{}]", inner.join(", "))
}

impl CompositeProvider {
    /// `CompositeProvider(providers)` with Python's defaults: 5 failures, 60 s, the system clock.
    ///
    /// # Errors
    /// As [`CompositeProvider::new`].
    pub fn with_defaults(providers: Vec<Registered>) -> Result<Self, ProviderError> {
        Self::new(providers, 5, 60.0, None)
    }

    /// `CompositeProvider(providers, failure_threshold=, reset_seconds=, clock=)`.
    ///
    /// # Errors
    /// `ValueError` for duplicate provider names (names key the breakers) or an invalid breaker
    /// configuration.
    pub fn new(
        providers: Vec<Registered>,
        failure_threshold: u32,
        reset_seconds: f64,
        clock: Option<Arc<dyn Clock>>,
    ) -> Result<Self, ProviderError> {
        let names: Vec<String> = providers.iter().map(|p| p.name().to_owned()).collect();
        let mut unique = names.clone();
        unique.sort();
        unique.dedup();
        if unique.len() != names.len() {
            return Err(ProviderError::value_error(format!(
                "provider names must be unique; got {}",
                py_names(&names)
            )));
        }
        let mut breakers = HashMap::new();
        for name in &names {
            let breaker = match &clock {
                None => CircuitBreaker::new(name, failure_threshold, reset_seconds)?,
                Some(c) => CircuitBreaker::with_clock(
                    name,
                    failure_threshold,
                    reset_seconds,
                    Arc::clone(c),
                )?,
            };
            breakers.insert(name.clone(), breaker);
        }
        Ok(Self {
            providers,
            breakers,
        })
    }

    /// The registered adapters, in preference order.
    #[must_use]
    pub fn providers(&self) -> &[Registered] {
        &self.providers
    }

    /// The breaker guarding `provider_name`.
    ///
    /// # Errors
    /// `KeyError` for a name nothing was registered under.
    pub fn breaker(&self, provider_name: &str) -> Result<&CircuitBreaker, ProviderError> {
        self.breakers.get(provider_name).ok_or_else(|| {
            ProviderError::foreign(
                "KeyError",
                format!("no provider registered under {}", repr_str(provider_name)),
            )
        })
    }

    /// One report per registered adapter, in registration order.
    #[must_use]
    pub fn health_reports(&self) -> Vec<ProviderHealth> {
        self.providers.iter().map(safe_check).collect()
    }

    /// Registered adapters offering `capability`, in preference order.
    #[must_use]
    pub fn providers_for(&self, capability: Capability) -> Vec<Registered> {
        self.providers
            .iter()
            .filter(|p| p.health.capabilities().contains(&capability))
            .cloned()
            .collect()
    }

    /// `route`: run `call` against the first healthy provider offering `capability`.
    ///
    /// Only `ProviderUnavailable` moves on to the next candidate — "this provider cannot serve",
    /// exactly when a fallback is correct. A transient error still propagates after the breaker
    /// records it: the caller's own retry policy owns that decision, and silently failing over
    /// on a timeout would mask an upstream that is merely slow.
    ///
    /// **`AccessTokenExpired` is the exception to the exception, and it is deliberate.** Falling
    /// through on a dead token is a *substitution*, not a degradation: the next bars provider is
    /// `FixtureProvider`, whose instruments are real NSE symbols carrying synthetic prices, so a
    /// backfill with a stale token would write invented history over real bars and report
    /// success. `CredentialsMissing` still falls through — docs/03's `local` environment. Found
    /// on 22 Aug 2026 by a backfill that fetched fixture bars for a real instrument.
    ///
    /// # Errors
    /// `CapabilityNotAvailable` naming every provider tried and why; a stale token or a transient
    /// failure as it was raised.
    pub async fn route<T, F, Fut>(
        &self,
        capability: Capability,
        call: F,
    ) -> Result<T, ProviderError>
    where
        F: Fn(Registered) -> Fut,
        Fut: Future<Output = Result<T, ProviderError>>,
    {
        let candidates = self.providers_for(capability);
        if candidates.is_empty() {
            let names: Vec<String> = self.providers.iter().map(|p| p.name().to_owned()).collect();
            return Err(ProviderError::capability_not_available(format!(
                "no registered provider offers {}; registered: {}",
                repr_str(capability.value()),
                py_names(&names)
            ))
            .with_provider(PROVIDER_NAME));
        }
        let mut skipped: Vec<String> = Vec::new();
        for provider in candidates {
            let name = provider.name().to_owned();
            let breaker = self.breaker(&name)?;
            if breaker.state() == CircuitState::Open {
                skipped.push(format!(
                    "{name}: circuit open for {}s",
                    fixed(breaker.retry_after(), 0)
                ));
                continue;
            }
            match breaker.call(call(provider)).await {
                Ok(value) => return Ok(value),
                // Never substituted for. See the doc comment.
                Err(e) if e.kind() == ErrorKind::AccessTokenExpired => return Err(e),
                Err(e) if e.kind() == ErrorKind::CircuitOpen || e.is_unavailable() => {
                    skipped.push(format!("{name}: {e}"));
                }
                Err(e) => return Err(e),
            }
        }
        Err(ProviderError::capability_not_available(format!(
            "no provider could serve {}. Tried: {}",
            repr_str(capability.value()),
            skipped.join("; ")
        ))
        .with_provider(PROVIDER_NAME))
    }

    /// `list_instruments`, routed.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn list_instruments(&self) -> Result<Vec<InstrumentRecord>, ProviderError> {
        self.route(Capability::ListInstruments, |p| async move {
            p.bars()?.list_instruments().await
        })
        .await
    }

    /// `daily_bars`, routed.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        self.route(Capability::DailyBars, |p| async move {
            p.bars()?.daily_bars(token, start, end).await
        })
        .await
    }

    /// `index_constituents`, routed.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn index_constituents(
        &self,
        index_slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        self.route(Capability::IndexConstituents, |p| async move {
            p.reference()?.index_constituents(index_slug, on).await
        })
        .await
    }

    /// `index_snapshots`, routed.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn index_snapshots(
        &self,
        on: NaiveDate,
    ) -> Result<Vec<IndexSnapshot>, ProviderError> {
        self.route(Capability::IndexSnapshots, |p| async move {
            p.reference()?.index_snapshots(on).await
        })
        .await
    }

    /// `corporate_actions`, routed.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn corporate_actions(
        &self,
        since: NaiveDate,
    ) -> Result<Vec<CorporateAction>, ProviderError> {
        self.route(Capability::CorporateActions, |p| async move {
            p.reference()?.corporate_actions(since).await
        })
        .await
    }

    /// `listings`, routed.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        self.route(Capability::Listings, |p| async move {
            p.reference()?.listings().await
        })
        .await
    }

    /// The NSE Emerge register, from whichever routed provider publishes one. Duck-typed rather
    /// than part of [`ReferenceProvider`]: a provider without one contributes nothing.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn sme_listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        self.route(Capability::Listings, |p| async move {
            match p.supplementary() {
                Some(s) => s.sme_listings().await,
                None => Ok(Vec::new()),
            }
        })
        .await
    }

    /// NSE's ETF security list, routed on `LISTINGS`. WITHOUT THIS THE `etf` UNIVERSE STAYS EMPTY
    /// AND SAYS NOTHING (9 Sep 2026): the pipeline holds the composite, not an `NseProvider`.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn etf_symbols(&self) -> Result<Vec<String>, ProviderError> {
        self.route(Capability::Listings, |p| async move {
            match p.supplementary() {
                Some(s) => s.etf_symbols().await,
                None => Ok(Vec::new()),
            }
        })
        .await
    }

    /// Today's live level for a benchmark index, routed on `DAILY_BARS` — a live index level is
    /// price data from the same broker session the bars come from.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn index_level(&self, slug: &str) -> Result<Option<Decimal>, ProviderError> {
        self.route(Capability::DailyBars, |p| async move {
            match p.supplementary() {
                Some(s) => s.index_level(slug).await,
                None => Ok(None),
            }
        })
        .await
    }

    /// The F&O universe, routed on `LIST_INSTRUMENTS` (the broker's instrument dump).
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn fno_underlyings(&self) -> Result<Vec<String>, ProviderError> {
        self.route(Capability::ListInstruments, |p| async move {
            match p.supplementary() {
                Some(s) => s.fno_underlyings().await,
                None => Ok(Vec::new()),
            }
        })
        .await
    }

    /// `bhavcopy`, routed.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        self.route(Capability::Bhavcopy, |p| async move {
            p.reference()?.bhavcopy(on).await
        })
        .await
    }

    /// `equity_fundamentals`, routed.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn equity_fundamentals(
        &self,
        on: NaiveDate,
        symbols: &[String],
        series_by_symbol: Option<&HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        self.route(Capability::EquityFundamentals, |p| async move {
            p.reference()?
                .equity_fundamentals(on, symbols, series_by_symbol)
                .await
        })
        .await
    }

    /// `broker_holdings`, routed. Nothing registers a *fallback* holdings adapter, so a dead
    /// broker session raises `CapabilityNotAvailable` instead of degrading — substituting
    /// invented positions for a user's real money would be wrong numbers that look right.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn broker_holdings(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Vec<BrokerHoldingRecord>, ProviderError> {
        self.route(Capability::BrokerHoldings, |p| async move {
            p.holdings()?.broker_holdings(account).await
        })
        .await
    }

    /// `broker_cash`, routed.
    ///
    /// # Errors
    /// As [`CompositeProvider::route`].
    pub async fn broker_cash(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Option<Decimal>, ProviderError> {
        self.route(Capability::BrokerCash, |p| async move {
            p.holdings()?.broker_cash(account).await
        })
        .await
    }
}

/// `_safe_check`: `check()` must never fail, but a third-party adapter might panic; contain it
/// here so `providers doctor` still produces a line (Prompt 2 acceptance criterion 4). Rust's
/// `check` cannot raise, so a panic is the only failure there is to contain.
fn safe_check(provider: &Registered) -> ProviderHealth {
    let health = Arc::clone(&provider.health);
    match std::panic::catch_unwind(AssertUnwindSafe(|| health.check())) {
        Ok(report) => report,
        Err(payload) => {
            let message = payload
                .downcast_ref::<&str>()
                .map(|s| (*s).to_owned())
                .or_else(|| payload.downcast_ref::<String>().cloned())
                .unwrap_or_default();
            ProviderHealth {
                name: provider.name().to_owned(),
                available: false,
                capabilities: CapabilitySet::new(),
                detail: format!("health check raised panic: {message}"),
            }
        }
    }
}

/// `all_capabilities`: the union of what `providers` offer.
#[must_use]
pub fn all_capabilities(providers: &[Registered]) -> CapabilitySet {
    providers
        .iter()
        .flat_map(|p| p.health.capabilities())
        .collect()
}

impl HealthReporting for CompositeProvider {
    fn name(&self) -> &str {
        PROVIDER_NAME
    }

    /// The union of what the registered adapters offer, in principle.
    fn capabilities(&self) -> CapabilitySet {
        all_capabilities(&self.providers)
    }

    /// Aggregate health. Available when at least one registered adapter is.
    fn check(&self) -> ProviderHealth {
        let reports = self.health_reports();
        let served: CapabilitySet = reports
            .iter()
            .flat_map(ProviderHealth::served_capabilities)
            .collect();
        let detail: Vec<String> = reports
            .iter()
            .map(|r| format!("{}={}", r.name, if r.available { "up" } else { "down" }))
            .collect();
        let detail = detail.join(", ");
        ProviderHealth {
            name: PROVIDER_NAME.to_owned(),
            available: !served.is_empty(),
            capabilities: self.capabilities(),
            detail: if detail.is_empty() {
                "no providers registered".to_owned()
            } else {
                detail
            },
        }
    }
}

#[async_trait]
impl BarsProvider for CompositeProvider {
    async fn list_instruments(&self) -> Result<Vec<InstrumentRecord>, ProviderError> {
        Self::list_instruments(self).await
    }

    async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        Self::daily_bars(self, token, start, end).await
    }
}

#[async_trait]
impl ReferenceProvider for CompositeProvider {
    async fn index_constituents(
        &self,
        index_slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        Self::index_constituents(self, index_slug, on).await
    }

    async fn index_snapshots(&self, on: NaiveDate) -> Result<Vec<IndexSnapshot>, ProviderError> {
        Self::index_snapshots(self, on).await
    }

    async fn corporate_actions(
        &self,
        since: NaiveDate,
    ) -> Result<Vec<CorporateAction>, ProviderError> {
        Self::corporate_actions(self, since).await
    }

    async fn listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        Self::listings(self).await
    }

    async fn bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        Self::bhavcopy(self, on).await
    }

    async fn equity_fundamentals(
        &self,
        on: NaiveDate,
        symbols: &[String],
        series_by_symbol: Option<&HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        Self::equity_fundamentals(self, on, symbols, series_by_symbol).await
    }
}

#[async_trait]
impl HoldingsProvider for CompositeProvider {
    async fn broker_holdings(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Vec<BrokerHoldingRecord>, ProviderError> {
        Self::broker_holdings(self, account).await
    }

    async fn broker_cash(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Option<Decimal>, ProviderError> {
        Self::broker_cash(self, account).await
    }
}

#[async_trait]
impl SupplementaryReads for CompositeProvider {
    async fn sme_listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        Self::sme_listings(self).await
    }

    async fn etf_symbols(&self) -> Result<Vec<String>, ProviderError> {
        Self::etf_symbols(self).await
    }

    async fn index_level(&self, slug: &str) -> Result<Option<Decimal>, ProviderError> {
        Self::index_level(self, slug).await
    }

    async fn fno_underlyings(&self) -> Result<Vec<String>, ProviderError> {
        Self::fno_underlyings(self).await
    }
}
