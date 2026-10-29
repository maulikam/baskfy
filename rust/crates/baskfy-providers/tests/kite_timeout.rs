//! AF 3.11 — `KiteConnect` is constructed with an explicit timeout. Ports
//! `tests/test_kite_timeout.py`, and adds what the Python cannot show without a socket: the
//! timeout really bounds a read, against a loopback server that never answers.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

mod common;
mod kite_common;

use std::time::Instant;

use baskfy_providers::kite::{
    DEFAULT_TIMEOUT_SECONDS, KiteClient, KiteConnect, KiteError, default_client_factory,
};
use baskfy_providers::settings::ProviderSettings;
use common::arm;
use kite_common::{Canned, MockKite};

#[test]
fn kite_client_factory_passes_an_explicit_timeout() {
    arm();
    let settings = ProviderSettings {
        kite_request_timeout_seconds: 42.0,
        ..ProviderSettings::default()
    };
    let client = default_client_factory("key", settings.kite_request_timeout_seconds).unwrap();
    assert_eq!(client.timeout(), 42.0);
    assert_eq!(client.root(), "https://api.kite.trade/");
}

#[test]
fn the_settings_default_is_thirty_seconds_not_the_sdks_seven() {
    arm();
    assert_eq!(
        ProviderSettings::default().kite_request_timeout_seconds,
        30.0
    );
    assert_eq!(DEFAULT_TIMEOUT_SECONDS, 7.0);
    // `timeout or self._default_timeout`: none and zero both fall back to the SDK's 7 s.
    assert_eq!(KiteConnect::new("k", None).unwrap().timeout(), 7.0);
    assert_eq!(KiteConnect::new("k", Some(0.0)).unwrap().timeout(), 7.0);
}

#[tokio::test]
async fn the_timeout_bounds_a_read_that_never_answers() {
    arm();
    let server = MockKite::start(vec![Canned::hang()]).await;
    let client = KiteConnect::with_root("k", &server.root(), Some(0.3)).unwrap();
    let started = Instant::now();
    let err = client.holdings().await.unwrap_err();
    assert!(started.elapsed().as_secs_f64() < 5.0);
    let KiteError::Other(inner) = &err else {
        panic!("a transport failure passes through untranslated: {err:?}");
    };
    assert_eq!(inner.python_type(), "ReadTimeout");
    assert_eq!(server.requests().len(), 1);
}
