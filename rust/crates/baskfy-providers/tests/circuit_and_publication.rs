//! The circuit breaker (ports the `CircuitBreaker` cases of `tests/test_composite.py`
//! `TestCircuitBreaking`) and the bhavcopy publication probe (`publication.py`'s contract).
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

mod common;

use std::sync::Arc;

use baskfy_providers::circuit::{CircuitBreaker, CircuitState};
use baskfy_providers::errors::{Detail, ErrorKind, ProviderError};
use baskfy_providers::ports::{ReferenceProvider, reference_capabilities};
use baskfy_providers::publication::bhavcopy_publication_check;
use chrono::NaiveDate;
use common::{Answer, ManualClock, StubProvider, arm};
use polars::prelude::df;

async fn fail() -> Result<(), ProviderError> {
    Err(ProviderError::upstream_unavailable("503"))
}

async fn missing_credentials() -> Result<(), ProviderError> {
    Err(ProviderError::credentials_missing("no key"))
}

#[tokio::test]
async fn repeated_transient_failures_open_the_circuit() {
    arm();
    let breaker = CircuitBreaker::with_clock("nse", 3, 60.0, ManualClock::at(0.0)).unwrap();
    for _ in 0..3 {
        assert_eq!(
            breaker.call(fail()).await.unwrap_err().kind(),
            ErrorKind::UpstreamUnavailable
        );
    }
    assert_eq!(breaker.state(), CircuitState::Open);
}

#[tokio::test]
async fn an_open_circuit_stops_calling_the_provider() {
    arm();
    let breaker = CircuitBreaker::with_clock("nse", 2, 60.0, ManualClock::at(0.0)).unwrap();
    for _ in 0..2 {
        assert!(breaker.call(fail()).await.is_err());
    }
    let mut called = false;
    let err = breaker
        .call(async {
            called = true;
            Ok::<(), ProviderError>(())
        })
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CircuitOpen);
    assert!(!called);
}

#[tokio::test]
async fn a_success_closes_the_circuit() {
    arm();
    let breaker = CircuitBreaker::new("nse", 2, 60.0).unwrap();
    assert!(breaker.call(fail()).await.is_err());
    assert_eq!(breaker.failures(), 1);
    breaker
        .call(async { Ok::<(), ProviderError>(()) })
        .await
        .unwrap();
    assert_eq!(breaker.failures(), 0);
    assert_eq!(breaker.state(), CircuitState::Closed);
}

#[tokio::test]
async fn the_circuit_half_opens_after_the_reset_interval() {
    arm();
    let clock = ManualClock::at(0.0);
    let breaker = CircuitBreaker::with_clock("nse", 1, 30.0, clock.clone()).unwrap();
    assert!(breaker.call(fail()).await.is_err());
    let while_open = breaker.state();
    clock.set(31.0);
    assert_eq!(while_open, CircuitState::Open);
    assert_eq!(breaker.state(), CircuitState::HalfOpen);
}

#[tokio::test]
async fn a_failed_probe_reopens_for_the_full_interval() {
    arm();
    let clock = ManualClock::at(0.0);
    let breaker = CircuitBreaker::with_clock("nse", 1, 30.0, clock.clone()).unwrap();
    assert!(breaker.call(fail()).await.is_err());
    clock.set(31.0);
    assert_eq!(
        breaker.call(fail()).await.unwrap_err().kind(),
        ErrorKind::UpstreamUnavailable
    );
    assert_eq!(breaker.state(), CircuitState::Open);
    assert!((breaker.retry_after() - 30.0).abs() < 1e-9);
}

#[tokio::test]
async fn a_non_transient_failure_does_not_trip_the_breaker() {
    arm();
    let breaker = CircuitBreaker::new("nse", 1, 30.0).unwrap();
    assert_eq!(
        breaker
            .call(missing_credentials())
            .await
            .unwrap_err()
            .kind(),
        ErrorKind::CredentialsMissing
    );
    assert_eq!(breaker.state(), CircuitState::Closed);
}

#[tokio::test]
async fn an_open_circuit_reports_when_it_will_retry() {
    arm();
    let breaker = CircuitBreaker::with_clock("nse", 1, 45.0, ManualClock::at(0.0)).unwrap();
    assert!(breaker.call(fail()).await.is_err());
    let err = breaker
        .call(async { Ok::<(), ProviderError>(()) })
        .await
        .unwrap_err();
    let Detail::CircuitOpen {
        retry_after_seconds,
        failures,
    } = err.detail()
    else {
        panic!("not CircuitOpen: {err}");
    };
    assert!((retry_after_seconds - 45.0).abs() < 1e-9);
    assert_eq!(*failures, 1);
    assert_eq!(
        err.to_string(),
        "[nse] circuit is open after 1 consecutive failures; retrying in 45.0s"
    );
}

#[test]
fn configuration_is_validated() {
    arm();
    assert!(
        CircuitBreaker::new("nse", 0, 60.0)
            .unwrap_err()
            .to_string()
            .contains("failure_threshold")
    );
    assert!(
        CircuitBreaker::new("nse", 5, 0.0)
            .unwrap_err()
            .to_string()
            .contains("reset_seconds")
    );
}

// --- publication ---------------------------------------------------------------------------

fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 18).unwrap()
}

#[tokio::test]
async fn a_bhavcopy_with_rows_is_evidence_of_publication() {
    arm();
    let mut stub = StubProvider::new("nse", reference_capabilities());
    stub.bhavcopy_frame = Some(df!("symbol" => ["SBIN"]).unwrap());
    let check =
        bhavcopy_publication_check(Some(Arc::new(stub) as Arc<dyn ReferenceProvider>)).unwrap();
    assert!(check(day()).await);
}

#[tokio::test]
async fn an_empty_bhavcopy_or_a_provider_error_is_no_evidence() {
    arm();
    let empty = StubProvider::new("nse", reference_capabilities());
    let check =
        bhavcopy_publication_check(Some(Arc::new(empty) as Arc<dyn ReferenceProvider>)).unwrap();
    assert!(!check(day()).await);
    let mut failing = StubProvider::new("nse", reference_capabilities());
    failing.answer = Answer::Transient;
    let check =
        bhavcopy_publication_check(Some(Arc::new(failing) as Arc<dyn ReferenceProvider>)).unwrap();
    assert!(!check(day()).await);
}

#[test]
fn no_provider_means_no_check() {
    arm();
    assert!(bhavcopy_publication_check(None).is_none());
}
