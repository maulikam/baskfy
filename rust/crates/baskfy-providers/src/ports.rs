//! The provider ports, transcribed from docs/09 §"Provider ports". Ports
//! `baskfy_providers.ports`.
//!
//! Python's Protocols are synchronous, because every Python caller is a Celery task. The Rust
//! services are tokio (axum, the worker), so the ports are **async** traits — the call is still
//! one blocking network device behind a boundary; only the waiting is cooperative
//! (DECISIONS-RS R4.1). `async-trait` keeps them object-safe, so the composite can hold
//! `Arc<dyn BarsProvider>`.
//!
//! [`Capability`] exists so a composite routes by capability rather than by type — "adding a
//! paid vendor later is a new adapter, not a rewrite". A trait implementation is checked by the
//! compiler, which is what Python's `isinstance(x, BarsProvider)` checks at runtime.

use std::collections::BTreeSet;

use async_trait::async_trait;
use chrono::NaiveDate;
use polars::prelude::DataFrame;
use rust_decimal::Decimal;

use crate::errors::ProviderError;
use crate::records::{
    BrokerAccountRef, BrokerHoldingRecord, CorporateAction, EquityFundamental, IndexSnapshot,
    InstrumentRecord, ListingRecord,
};

/// One routable operation. Values match the port method names (`Capability`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Capability {
    /// `list_instruments`.
    ListInstruments,
    /// `daily_bars`.
    DailyBars,
    /// `index_constituents`.
    IndexConstituents,
    /// `index_snapshots`.
    IndexSnapshots,
    /// `corporate_actions`.
    CorporateActions,
    /// `listings`.
    Listings,
    /// `bhavcopy`.
    Bhavcopy,
    /// `equity_fundamentals`.
    EquityFundamentals,
    /// `broker_holdings` — what a broker says the user actually holds.
    BrokerHoldings,
    /// `broker_cash` — the broker's own cash balance.
    BrokerCash,
}

impl Capability {
    /// Every member, in Python's declaration order.
    pub const ALL: [Self; 10] = [
        Self::ListInstruments,
        Self::DailyBars,
        Self::IndexConstituents,
        Self::IndexSnapshots,
        Self::CorporateActions,
        Self::Listings,
        Self::Bhavcopy,
        Self::EquityFundamentals,
        Self::BrokerHoldings,
        Self::BrokerCash,
    ];

    /// The `StrEnum` value — the port method's name.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::ListInstruments => "list_instruments",
            Self::DailyBars => "daily_bars",
            Self::IndexConstituents => "index_constituents",
            Self::IndexSnapshots => "index_snapshots",
            Self::CorporateActions => "corporate_actions",
            Self::Listings => "listings",
            Self::Bhavcopy => "bhavcopy",
            Self::EquityFundamentals => "equity_fundamentals",
            Self::BrokerHoldings => "broker_holdings",
            Self::BrokerCash => "broker_cash",
        }
    }

    /// `Capability(value)`.
    #[must_use]
    pub fn from_value(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|c| c.value() == value)
    }
}

/// A set of capabilities (`frozenset[Capability]`).
pub type CapabilitySet = BTreeSet<Capability>;

/// `BARS_CAPABILITIES` — docs/09's table: Kite provides bars.
#[must_use]
pub fn bars_capabilities() -> CapabilitySet {
    [Capability::ListInstruments, Capability::DailyBars].into()
}

/// `REFERENCE_CAPABILITIES` — docs/09's table: NSE provides reference data.
#[must_use]
pub fn reference_capabilities() -> CapabilitySet {
    [
        Capability::IndexConstituents,
        Capability::IndexSnapshots,
        Capability::CorporateActions,
        Capability::Listings,
        Capability::Bhavcopy,
        Capability::EquityFundamentals,
    ]
    .into()
}

/// `HOLDINGS_CAPABILITIES` — the broker ledger, deliberately a third set: a wrong candle is a
/// wrong chart, a wrong holdings list is somebody else's money on somebody's screen.
#[must_use]
pub fn holdings_capabilities() -> CapabilitySet {
    [Capability::BrokerHoldings, Capability::BrokerCash].into()
}

/// What one adapter can currently serve, and why not if it cannot (`ProviderHealth`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderHealth {
    /// Adapter name.
    pub name: String,
    /// Would a call succeed right now, as far as configuration can tell.
    pub available: bool,
    /// What it serves in principle.
    pub capabilities: CapabilitySet,
    /// Why, in words.
    pub detail: String,
}

impl ProviderHealth {
    /// `served_capabilities`: what it can serve right now — nothing when unavailable.
    #[must_use]
    pub fn served_capabilities(&self) -> CapabilitySet {
        if self.available {
            self.capabilities.clone()
        } else {
            CapabilitySet::new()
        }
    }
}

/// What `providers doctor` needs from any adapter (`HealthReporting`).
pub trait HealthReporting: Send + Sync {
    /// Short identifier used in errors and in the doctor output.
    fn name(&self) -> &str;

    /// What this adapter can serve, in principle.
    fn capabilities(&self) -> CapabilitySet;

    /// Probe configuration and report. Never fails and never touches the network.
    fn check(&self) -> ProviderHealth;
}

/// docs/09 §"Provider ports" (`BarsProvider`).
#[async_trait]
pub trait BarsProvider: HealthReporting {
    /// Every tradable instrument.
    async fn list_instruments(&self) -> Result<Vec<InstrumentRecord>, ProviderError>;

    /// Raw daily candles for one instrument, conforming to
    /// [`daily_bars_schema`](crate::records::daily_bars_schema). Unadjusted: the adjusted series
    /// is derived later by the pipeline, never by a provider.
    async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError>;
}

/// docs/09 §"Provider ports" (`ReferenceProvider`).
#[async_trait]
pub trait ReferenceProvider: HealthReporting {
    /// Constituent symbols of `index_slug` on `on`.
    async fn index_constituents(
        &self,
        index_slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError>;

    /// Every published index level on `on`.
    async fn index_snapshots(&self, on: NaiveDate) -> Result<Vec<IndexSnapshot>, ProviderError>;

    /// Corporate actions from `since`.
    async fn corporate_actions(
        &self,
        since: NaiveDate,
    ) -> Result<Vec<CorporateAction>, ProviderError>;

    /// The listings register.
    async fn listings(&self) -> Result<Vec<ListingRecord>, ProviderError>;

    /// One day's bhavcopy, conforming to [`bhavcopy_schema`](crate::records::bhavcopy_schema).
    async fn bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError>;

    /// Issued capital, P/E and derived market cap for cash equities on `on`. A name the
    /// exchange does not quote that day is omitted. `series_by_symbol` is an optional hint an
    /// adapter may ignore; a wrong hint must never produce a wrong row.
    async fn equity_fundamentals(
        &self,
        on: NaiveDate,
        symbols: &[String],
        series_by_symbol: Option<&std::collections::HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError>;
}

/// The broker ledger — read-only, forever (`HoldingsProvider`).
///
/// **This port cannot place an order and must never gain a method that can** (law 2). Both
/// methods name the account they read, because holdings are per-tenant.
#[async_trait]
pub trait HoldingsProvider: HealthReporting {
    /// Every equity position the broker reports for `account`. Empty is an answer (the user
    /// sold everything); a failed fetch is an error, never an empty list.
    async fn broker_holdings(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Vec<BrokerHoldingRecord>, ProviderError>;

    /// The broker's reported cash for `account`, or `None` if it publishes none. `None` and
    /// zero are different statements. Decimal, never float.
    async fn broker_cash(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Option<Decimal>, ProviderError>;
}
