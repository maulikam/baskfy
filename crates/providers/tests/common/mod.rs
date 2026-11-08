//! Shared test seams: the network block, a manual clock, recording sleepers and the
//! configurable provider stub (`tests/stubs.py`).
#![allow(dead_code, clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_providers::clock::{Clock, Random, Sleeper};
use baskfy_providers::errors::ProviderError;
use baskfy_providers::ports::{
    BarsProvider, CapabilitySet, HealthReporting, ProviderHealth, ReferenceProvider,
};
use baskfy_providers::records::{
    CorporateAction, CorporateActionFields, CorporateActionType, EquityFundamental, IndexSnapshot,
    IndexSnapshotFields, InstrumentFields, InstrumentRecord, InstrumentType, ListingFields,
    ListingRecord, daily_bars_schema, empty_frame,
};
use chrono::{DateTime, NaiveDate, Utc};
use polars::prelude::DataFrame;

/// Arms the suite-wide network block (`conftest.py`'s autouse fixture). Every test calls it
/// first; `network_is_blocked.rs` checks that they do.
pub fn arm() {
    baskfy_providers::net::block_network();
}

/// `clock=lambda: now[0]`.
#[derive(Debug, Default)]
pub struct ManualClock {
    now: Mutex<f64>,
}

impl ManualClock {
    pub fn at(t: f64) -> Arc<Self> {
        Arc::new(Self { now: Mutex::new(t) })
    }

    pub fn set(&self, t: f64) {
        *self.now.lock().unwrap() = t;
    }
}

impl Clock for ManualClock {
    fn monotonic(&self) -> f64 {
        *self.now.lock().unwrap()
    }

    fn now(&self) -> DateTime<Utc> {
        DateTime::UNIX_EPOCH
    }
}

/// `sleeper=sleeps.append`.
#[derive(Debug, Default)]
pub struct RecordingSleeper {
    pub sleeps: Mutex<Vec<f64>>,
}

impl RecordingSleeper {
    pub fn sleeps(&self) -> Vec<f64> {
        self.sleeps.lock().unwrap().clone()
    }
}

#[async_trait]
impl Sleeper for RecordingSleeper {
    async fn sleep(&self, seconds: f64) {
        self.sleeps.lock().unwrap().push(seconds);
    }
}

/// A fixed draw for `random.random()`.
#[derive(Debug, Clone, Copy)]
pub struct FixedRandom(pub f64);

impl Random for FixedRandom {
    fn random(&self) -> f64 {
        self.0
    }
}

/// How the stub answers.
#[derive(Debug, Clone)]
pub enum Answer {
    Ok,
    Transient,
    CredentialsMissing,
}

/// `StubProvider`: advertises a capability set and answers however the test wants.
pub struct StubProvider {
    pub name: String,
    pub capabilities: CapabilitySet,
    pub answer: Answer,
    pub available: bool,
    pub symbols: Vec<String>,
    pub bhavcopy_frame: Option<DataFrame>,
    pub calls: AtomicUsize,
}

impl StubProvider {
    pub fn new(name: &str, capabilities: CapabilitySet) -> Self {
        Self {
            name: name.to_owned(),
            capabilities,
            answer: Answer::Ok,
            available: true,
            symbols: vec!["SBIN".to_owned()],
            bhavcopy_frame: None,
            calls: AtomicUsize::new(0),
        }
    }

    fn answer(&self) -> Result<(), ProviderError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        match self.answer {
            Answer::Ok => Ok(()),
            Answer::Transient => Err(ProviderError::upstream_unavailable("503")),
            Answer::CredentialsMissing => Err(ProviderError::credentials_missing("no key")),
        }
    }
}

impl HealthReporting for StubProvider {
    fn name(&self) -> &str {
        &self.name
    }

    fn capabilities(&self) -> CapabilitySet {
        self.capabilities.clone()
    }

    fn check(&self) -> ProviderHealth {
        ProviderHealth {
            name: self.name.clone(),
            available: self.available,
            capabilities: self.capabilities.clone(),
            detail: "stub".to_owned(),
        }
    }
}

#[async_trait]
impl BarsProvider for StubProvider {
    async fn list_instruments(&self) -> Result<Vec<InstrumentRecord>, ProviderError> {
        self.answer()?;
        Ok(self
            .symbols
            .iter()
            .map(|s| {
                let mut f = InstrumentFields::new(s, s, InstrumentType::Eq);
                f.series = Some("EQ".to_owned());
                InstrumentRecord::new(f).unwrap()
            })
            .collect())
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
impl ReferenceProvider for StubProvider {
    async fn index_constituents(
        &self,
        _index_slug: &str,
        _on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        self.answer()?;
        Ok(self.symbols.clone())
    }

    async fn index_snapshots(&self, on: NaiveDate) -> Result<Vec<IndexSnapshot>, ProviderError> {
        self.answer()?;
        Ok(vec![
            IndexSnapshot::new(IndexSnapshotFields::new("nifty-50", on)).unwrap(),
        ])
    }

    async fn corporate_actions(
        &self,
        since: NaiveDate,
    ) -> Result<Vec<CorporateAction>, ProviderError> {
        self.answer()?;
        Ok(vec![
            CorporateAction::new(CorporateActionFields::new(
                "CUPID",
                CorporateActionType::Bonus,
                since,
            ))
            .unwrap(),
        ])
    }

    async fn listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        self.answer()?;
        Ok(self
            .symbols
            .iter()
            .map(|s| ListingRecord::new(ListingFields::new(s, s)).unwrap())
            .collect())
    }

    async fn bhavcopy(&self, _on: NaiveDate) -> Result<DataFrame, ProviderError> {
        self.answer()?;
        Ok(self
            .bhavcopy_frame
            .clone()
            .unwrap_or_else(|| empty_frame(&daily_bars_schema())))
    }

    async fn equity_fundamentals(
        &self,
        _on: NaiveDate,
        _symbols: &[String],
        _series_by_symbol: Option<&std::collections::HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        self.answer()?;
        Ok(Vec::new())
    }
}
