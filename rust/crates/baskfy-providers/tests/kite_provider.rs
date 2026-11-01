//! `KiteProvider` — ports `tests/test_kite.py` case for case (Prompt 2 acceptance criterion 3:
//! "a test proves KiteProvider retries and eventually raises a typed error after N attempts").
//!
//! Everything runs against a fake client injected through `KiteRuntime`, so no network call is
//! possible and every failure mode a live broker would only produce at 3am is reachable.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

mod common;
mod kite_common;

use std::str::FromStr;
use std::sync::Arc;

use baskfy_providers::errors::ErrorKind;
use baskfy_providers::kite::{
    DEFAULT_MAX_DAYS_PER_REQUEST, KiteExceptionKind, KiteMap, KiteProvider, KiteRuntime, KiteValue,
    QUOTE_BATCH_SIZE,
};
use baskfy_providers::ports::{
    Capability, HealthReporting, bars_capabilities, holdings_capabilities,
};
use baskfy_providers::records::{InstrumentRecord, InstrumentType, daily_bars_schema};
use baskfy_providers::secret::Secret;
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::AccessTokenStore;
use chrono::{Duration, NaiveDate};
use common::arm;
use kite_common::{
    CountingLimiter, FakeKiteClient, Sleeps, build_provider, configured_settings, date, dict,
    factory, naive, policy, ref_, settings, store_for, stored_token, today,
};
use polars::prelude::{AnyValue, DataType};
use rust_decimal::Decimal;

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

/// `_candles(start, count)`.
fn candles(start: NaiveDate, count: i64) -> Vec<KiteValue> {
    (0..count)
        .map(|offset| {
            dict(vec![
                ("date", (start + Duration::days(offset)).into()),
                ("open", (100 + offset).into()),
                ("high", (105 + offset).into()),
                ("low", (95 + offset).into()),
                ("close", (101 + offset).into()),
                ("volume", (1000 + offset).into()),
            ])
        })
        .collect()
}

fn raising(kind: KiteExceptionKind, message: &str) -> FakeKiteClient {
    FakeKiteClient {
        raises: Some((kind, message.to_owned())),
        ..FakeKiteClient::default()
    }
}

// --- TestCapabilities ------------------------------------------------------------------------

#[test]
fn it_offers_bars_and_the_broker_ledger() {
    arm();
    let env = settings();
    let provider = KiteProvider::new(env.settings.clone(), KiteRuntime::default()).unwrap();
    let mut expected = bars_capabilities();
    expected.extend(holdings_capabilities());
    assert_eq!(provider.capabilities(), expected);
    assert!(
        !provider
            .capabilities()
            .contains(&Capability::IndexConstituents)
    );
    assert!(
        !provider
            .capabilities()
            .contains(&Capability::CorporateActions)
    );
}

// --- TestHealth ------------------------------------------------------------------------------

#[test]
fn unconfigured_is_unavailable_not_an_exception() {
    arm();
    let env = settings();
    let health = KiteProvider::new(env.settings.clone(), KiteRuntime::default())
        .unwrap()
        .check();
    assert!(!health.available);
    assert!(
        health.detail.contains("BASKFY_KITE_API_KEY"),
        "{}",
        health.detail
    );
}

#[test]
fn missing_token_is_reported() {
    arm();
    let env = configured_settings();
    let health = KiteProvider::new(env.settings.clone(), KiteRuntime::default())
        .unwrap()
        .check();
    assert!(!health.available);
    assert!(
        health.detail.contains("no encrypted access token"),
        "{}",
        health.detail
    );
}

#[test]
fn expired_token_is_reported() {
    arm();
    // Python's `"expired" in health.detail.lower()` holds because pytest's `tmp_path` — which
    // the message quotes — is named after the test; the message's own words are "no longer
    // valid". Both are asserted, under the same directory name.
    let env = kite_common::configured_settings_named("test_expired_token_is_reported");
    let store = store_for(&env.settings);
    store
        .save("stale", Some(today() - Duration::days(2)))
        .unwrap();
    let health = KiteProvider::new(
        env.settings.clone(),
        KiteRuntime {
            rate_limiter: Some(Arc::new(CountingLimiter::default())),
            token_store: Some(store),
            ..KiteRuntime::default()
        },
    )
    .unwrap()
    .check();
    assert!(!health.available);
    assert!(
        health.detail.to_lowercase().contains("expired"),
        "{}",
        health.detail
    );
    assert!(
        health.detail.contains("is no longer valid"),
        "{}",
        health.detail
    );
}

#[test]
fn without_a_limiter_it_refuses_to_be_available() {
    arm();
    let env = configured_settings();
    let health = KiteProvider::new(
        env.settings.clone(),
        KiteRuntime {
            token_store: Some(stored_token(&env.settings)),
            ..KiteRuntime::default()
        },
    )
    .unwrap()
    .check();
    assert!(!health.available);
    assert!(health.detail.contains("rate limiter"), "{}", health.detail);
}

#[test]
fn fully_configured_is_available() {
    arm();
    let env = configured_settings();
    let provider = KiteProvider::new(
        env.settings.clone(),
        KiteRuntime {
            rate_limiter: Some(Arc::new(CountingLimiter::default())),
            token_store: Some(stored_token(&env.settings)),
            ..KiteRuntime::default()
        },
    )
    .unwrap();
    let health = provider.check();
    assert!(health.available, "{}", health.detail);
    assert_eq!(health.detail, "access token valid, 3 req/s");
}

// --- TestTokenHandling -----------------------------------------------------------------------

#[tokio::test]
async fn an_expired_token_stops_the_call_before_it_is_made() {
    arm();
    let env = configured_settings();
    let store = store_for(&env.settings);
    store
        .save("stale", Some(today() - Duration::days(1)))
        .unwrap();
    let client = Arc::new(FakeKiteClient::default());
    let provider = build_provider(&env.settings, client.clone(), store, None, None);
    let err = provider.list_instruments().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::AccessTokenExpired);
    assert_eq!(client.calls().instrument_calls, 0);
}

#[tokio::test]
async fn a_missing_api_key_raises_credentials_missing() {
    arm();
    let env = settings();
    let store = Arc::new(AccessTokenStore::new(
        env.settings.kite_token_path.clone(),
        Secret::new(""),
    ));
    let provider = build_provider(
        &env.settings,
        Arc::new(FakeKiteClient::default()),
        store,
        None,
        None,
    );
    let err = provider.list_instruments().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CredentialsMissing);
    assert_eq!(err.to_string(), "[kite] BASKFY_KITE_API_KEY is not set");
}

#[tokio::test]
async fn token_expiry_is_never_retried() {
    arm();
    let env = configured_settings();
    let store = store_for(&env.settings);
    store.save("fresh", Some(today())).unwrap();
    let client = Arc::new(raising(KiteExceptionKind::Token, "token expired"));
    let sleeps = Arc::new(Sleeps::default());
    let provider = build_provider(
        &env.settings,
        client.clone(),
        store,
        None,
        Some(sleeps.clone()),
    );
    let err = provider.list_instruments().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::AccessTokenExpired);
    assert!(
        err.to_string()
            .starts_with("[kite] Kite rejected the access token: token expired. Regenerate"),
        "{err}"
    );
    assert_eq!(client.calls().instrument_calls, 1);
    assert!(sleeps.taken().is_empty());
}

// --- TestRetries -----------------------------------------------------------------------------

#[tokio::test]
async fn it_retries_then_raises_a_typed_error_after_n_attempts() {
    arm();
    let env = configured_settings();
    let attempts = 4;
    let client = Arc::new(raising(KiteExceptionKind::Network, "503 from upstream"));
    let sleeps = Arc::new(Sleeps::default());
    let provider = build_provider(
        &env.settings,
        client.clone(),
        stored_token(&env.settings),
        Some(policy(attempts, 0.5)),
        Some(sleeps.clone()),
    );
    let err = provider.list_instruments().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::RetryBudgetExhausted);
    assert_eq!(client.calls().instrument_calls, attempts as usize);
    assert_eq!(sleeps.taken().len(), attempts as usize - 1);
    assert_eq!(err.attempts(), Some(attempts));
    assert_eq!(
        err.last_error().unwrap().kind(),
        ErrorKind::UpstreamUnavailable
    );
    assert!(err.to_string().contains("503 from upstream"), "{err}");
    assert_eq!(
        err.to_string(),
        "[kite] gave up after 4 attempt(s); last error: UpstreamUnavailable: [kite] Kite \
         network failure: 503 from upstream"
    );
}

#[tokio::test]
async fn backoff_is_exponential() {
    arm();
    let env = configured_settings();
    let sleeps = Arc::new(Sleeps::default());
    let provider = build_provider(
        &env.settings,
        Arc::new(raising(KiteExceptionKind::Network, "503")),
        stored_token(&env.settings),
        Some(policy(5, 0.5)),
        Some(sleeps.clone()),
    );
    let err = provider.list_instruments().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::RetryBudgetExhausted);
    assert_eq!(sleeps.taken(), vec![0.5, 1.0, 2.0, 4.0]);
}

#[tokio::test]
async fn a_transient_failure_that_clears_is_not_surfaced() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        instruments: vec![dict(vec![
            ("tradingsymbol", "CUPID".into()),
            ("name", "CUPID LIMITED".into()),
            ("instrument_type", "EQ".into()),
            ("segment", "NSE".into()),
            ("instrument_token", 12345_i64.into()),
            ("exchange", "NSE".into()),
        ])],
        raises: Some((KiteExceptionKind::Network, "flaky".to_owned())),
        fail_times: Some(2),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client.clone(),
        stored_token(&env.settings),
        Some(policy(5, 0.1)),
        None,
    );
    let records = provider.list_instruments().await.unwrap();
    assert_eq!(
        records.iter().map(|r| r.symbol.clone()).collect::<Vec<_>>(),
        vec!["CUPID"]
    );
    assert_eq!(client.calls().instrument_calls, 3);
}

#[tokio::test]
async fn a_malformed_request_is_not_retried() {
    arm();
    let env = configured_settings();
    let client = Arc::new(raising(KiteExceptionKind::Input, "bad interval"));
    let provider = build_provider(
        &env.settings,
        client.clone(),
        stored_token(&env.settings),
        None,
        None,
    );
    let err = provider.list_instruments().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    assert_eq!(client.calls().instrument_calls, 1);
}

#[tokio::test]
async fn throttling_is_translated_to_rate_limited() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        raises: Some((KiteExceptionKind::Network, "Too many requests".to_owned())),
        fail_times: Some(1),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        Some(policy(1, 0.1)),
        None,
    );
    let err = provider.list_instruments().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::RetryBudgetExhausted);
    assert_eq!(err.last_error().unwrap().kind(), ErrorKind::RateLimited);
}

#[tokio::test]
async fn a_permission_failure_is_unavailable_not_transient() {
    arm();
    let env = configured_settings();
    let client = Arc::new(raising(
        KiteExceptionKind::Permission,
        "no historical access",
    ));
    let provider = build_provider(
        &env.settings,
        client.clone(),
        stored_token(&env.settings),
        None,
        None,
    );
    let err = provider.list_instruments().await.unwrap_err();
    assert!(err.is_unavailable(), "{err}");
    assert_eq!(client.calls().instrument_calls, 1);
}

// --- TestChunking ----------------------------------------------------------------------------

#[test]
fn the_default_cap_matches_the_doc() {
    arm();
    assert_eq!(DEFAULT_MAX_DAYS_PER_REQUEST, 2000);
    assert_eq!(ProviderSettings::default().kite_max_days_per_request, 2000);
}

#[test]
fn a_short_window_is_one_request() {
    arm();
    let env = configured_settings();
    let provider = build_provider(
        &env.settings,
        Arc::new(FakeKiteClient::default()),
        stored_token(&env.settings),
        None,
        None,
    );
    let windows = provider
        .chunk_windows(date(2026, 1, 1), date(2026, 3, 1))
        .unwrap();
    assert_eq!(windows, vec![(date(2026, 1, 1), date(2026, 3, 1))]);
}

#[test]
fn a_long_backfill_is_split_at_the_cap() {
    arm();
    let env = configured_settings();
    let store = stored_token(&env.settings);
    let capped = ProviderSettings {
        kite_max_days_per_request: 100,
        ..ProviderSettings::default()
    };
    let provider = build_provider(
        &capped,
        Arc::new(FakeKiteClient::default()),
        store,
        None,
        None,
    );
    let windows = provider
        .chunk_windows(date(2020, 1, 1), date(2020, 12, 31))
        .unwrap();
    assert_eq!(windows.len(), 4);
    for (start, end) in windows {
        assert!((end - start).num_days() < 100);
    }
}

#[test]
fn chunks_are_contiguous_and_cover_the_whole_window() {
    arm();
    let env = configured_settings();
    let mut capped = env.settings.clone();
    capped.kite_max_days_per_request = 90;
    let provider = build_provider(
        &capped,
        Arc::new(FakeKiteClient::default()),
        stored_token(&env.settings),
        None,
        None,
    );
    let (start, end) = (date(2011, 1, 1), date(2013, 6, 30));
    let windows = provider.chunk_windows(start, end).unwrap();
    assert_eq!(windows[0].0, start);
    assert_eq!(windows[windows.len() - 1].1, end);
    for pair in windows.windows(2) {
        assert_eq!(pair[1].0, pair[0].1 + Duration::days(1));
    }
}

#[tokio::test]
async fn daily_bars_issues_one_request_per_chunk() {
    arm();
    let env = configured_settings();
    let mut capped = env.settings.clone();
    capped.kite_max_days_per_request = 100;
    let client = Arc::new(FakeKiteClient {
        candles: candles(date(2020, 1, 1), 300),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &capped,
        client.clone(),
        stored_token(&env.settings),
        None,
        None,
    );
    let frame = provider
        .daily_bars(555, date(2020, 1, 1), date(2020, 10, 26))
        .await
        .unwrap();
    assert_eq!(client.calls().historical_calls.len(), 3);
    assert_eq!(frame.height(), 300);
}

#[tokio::test]
async fn an_inverted_window_is_rejected() {
    arm();
    let env = configured_settings();
    let provider = build_provider(
        &env.settings,
        Arc::new(FakeKiteClient::default()),
        stored_token(&env.settings),
        None,
        None,
    );
    let err = provider
        .daily_bars(1, date(2026, 2, 1), date(2026, 1, 1))
        .await
        .unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert_eq!(err.to_string(), "end 2026-01-01 precedes start 2026-02-01");
}

// --- TestBarFrame ----------------------------------------------------------------------------

async fn bars(count: i64) -> polars::prelude::DataFrame {
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        candles: candles(date(2026, 8, 3), count),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    provider
        .daily_bars(42, date(2026, 8, 1), date(2026, 8, 18))
        .await
        .unwrap()
}

#[tokio::test]
async fn the_schema_is_fixed() {
    arm();
    let frame = bars(5).await;
    assert_eq!(frame.schema().as_ref(), &daily_bars_schema());
}

#[tokio::test]
async fn prices_are_decimals_not_floats() {
    arm();
    let frame = bars(2).await;
    let close = frame.column("close").unwrap();
    assert_eq!(close.dtype(), &DataType::Decimal(18, 4));
    let AnyValue::Decimal(mantissa, _, scale) = close.get(0).unwrap() else {
        panic!("not a decimal");
    };
    let value = Decimal::from_i128_with_scale(mantissa, u32::try_from(scale).unwrap());
    assert_eq!(value, dec("101.0000"));
    assert_eq!(value.to_string(), "101.0000");
}

#[tokio::test]
async fn the_source_column_marks_the_bars_as_raw_kite_data() {
    arm();
    let frame = bars(1).await;
    let source = frame.column("source").unwrap();
    assert_eq!(source.n_unique().unwrap(), 1);
    assert_eq!(
        source.get(0).unwrap(),
        polars::prelude::AnyValue::String("kite")
    );
}

#[tokio::test]
async fn no_history_yields_an_empty_frame_with_the_schema() {
    arm();
    let env = configured_settings();
    let provider = build_provider(
        &env.settings,
        Arc::new(FakeKiteClient::default()),
        stored_token(&env.settings),
        None,
        None,
    );
    let frame = provider
        .daily_bars(42, date(2026, 8, 1), date(2026, 8, 18))
        .await
        .unwrap();
    assert_eq!(frame.height(), 0);
    assert_eq!(frame.schema().as_ref(), &daily_bars_schema());
}

#[tokio::test]
async fn overlapping_chunks_do_not_duplicate_bars() {
    arm();
    let env = configured_settings();
    let mut capped = env.settings.clone();
    capped.kite_max_days_per_request = 10;
    let client = Arc::new(FakeKiteClient {
        candles: candles(date(2026, 1, 1), 40),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(&capped, client, stored_token(&env.settings), None, None);
    let frame = provider
        .daily_bars(42, date(2026, 1, 1), date(2026, 2, 9))
        .await
        .unwrap();
    assert_eq!(
        frame.column("date").unwrap().n_unique().unwrap(),
        frame.height()
    );
    assert_eq!(frame.height(), 40);
}

/// Not a Python test case: a differential check against the live Python (29 Oct 2026). Each
/// chunk's frame is conformed as it is fetched, so a price `Decimal(18, 4)` cannot hold fails
/// the first chunk — Python made one request and raised polars' own message, with that chunk's
/// inferred scale and counts — rather than after every chunk has been fetched.
#[tokio::test]
async fn an_unrepresentable_price_fails_its_own_chunk_before_the_next_request() {
    arm();
    let env = configured_settings();
    let mut capped = env.settings.clone();
    capped.kite_max_days_per_request = 10;
    let mut bars = candles(date(2026, 1, 1), 30);
    for (i, value) in [(2, 1e16), (3, 2e16)] {
        if let KiteValue::Map(row) = &mut bars[i] {
            row.insert("open", value.into());
        }
    }
    let client = Arc::new(FakeKiteClient {
        candles: bars,
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &capped,
        client.clone(),
        stored_token(&env.settings),
        None,
        None,
    );
    let err = provider
        .daily_bars(1, date(2026, 1, 1), date(2026, 1, 30))
        .await
        .unwrap_err();
    assert_eq!(err.python_type(), "InvalidOperationError");
    assert!(
        err.to_string().starts_with(
            "conversion from `decimal[38,0]` to `decimal[18,4]` failed in column 'open' for 2 \
             out of 10 values: [10000000000000000, 20000000000000000]"
        ),
        "{err}"
    );
    assert_eq!(client.calls().historical_calls.len(), 1);
}

// --- TestInstrumentMapping -------------------------------------------------------------------

#[tokio::test]
async fn equities_and_indices_are_kept_and_derivatives_dropped() {
    arm();
    let env = configured_settings();
    let row = |symbol: &str, name: &str, itype: &str, segment: &str, token: i64| {
        dict(vec![
            ("tradingsymbol", symbol.into()),
            ("name", name.into()),
            ("instrument_type", itype.into()),
            ("segment", segment.into()),
            ("instrument_token", token.into()),
        ])
    };
    let client = Arc::new(FakeKiteClient {
        instruments: vec![
            row("SBIN", "STATE BANK", "EQ", "NSE", 1),
            row("NIFTY 50", "NIFTY 50", "EQ", "INDICES", 2),
            row("SBIN26AUGFUT", "SBIN", "FUT", "NFO-FUT", 3),
        ],
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    let records = provider.list_instruments().await.unwrap();
    let got: Vec<(String, InstrumentType)> = records
        .iter()
        .map(|r| (r.symbol.clone(), r.instrument_type))
        .collect();
    assert_eq!(
        got,
        vec![
            ("SBIN".to_owned(), InstrumentType::Eq),
            ("NIFTY 50".to_owned(), InstrumentType::Index),
        ]
    );
}

#[tokio::test]
async fn a_row_with_no_symbol_is_dropped() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        instruments: vec![dict(vec![
            ("tradingsymbol", "".into()),
            ("instrument_type", "EQ".into()),
            ("segment", "NSE".into()),
        ])],
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    assert!(provider.list_instruments().await.unwrap().is_empty());
}

// --- TestThrottling --------------------------------------------------------------------------

#[tokio::test]
async fn every_call_takes_a_token() {
    arm();
    let env = configured_settings();
    let limiter = Arc::new(CountingLimiter::default());
    let client = Arc::new(FakeKiteClient {
        candles: candles(date(2026, 8, 3), 1),
        ..FakeKiteClient::default()
    });
    let provider = KiteProvider::new(
        env.settings.clone(),
        KiteRuntime {
            rate_limiter: Some(limiter.clone()),
            client_factory: Some(factory(client)),
            token_store: Some(stored_token(&env.settings)),
            ..KiteRuntime::default()
        },
    )
    .unwrap();
    provider
        .daily_bars(42, date(2026, 8, 1), date(2026, 8, 18))
        .await
        .unwrap();
    assert_eq!(limiter.count(), 1);
}

#[tokio::test]
async fn without_a_limiter_calls_are_refused() {
    arm();
    let env = configured_settings();
    let provider = KiteProvider::new(
        env.settings.clone(),
        KiteRuntime {
            client_factory: Some(factory(Arc::new(FakeKiteClient::default()))),
            token_store: Some(stored_token(&env.settings)),
            ..KiteRuntime::default()
        },
    )
    .unwrap();
    let err = provider.list_instruments().await.unwrap_err();
    assert!(err.is_unavailable());
    assert!(err.to_string().contains("rate limiter"), "{err}");
}

// --- TestBrokerTrades ------------------------------------------------------------------------

#[tokio::test]
async fn maps_equity_trades_to_decimal_fills_and_leaves_out_derivatives() {
    arm();
    let env = configured_settings();
    let filled = naive(2026, 9, 11, 10, 5, 7);
    let client = Arc::new(FakeKiteClient {
        trades: vec![
            dict(vec![
                ("trade_id", "T1".into()),
                ("order_id", "O1".into()),
                ("exchange", "NSE".into()),
                ("tradingsymbol", "pwl".into()),
                ("transaction_type", "BUY".into()),
                ("quantity", 25_i64.into()),
                ("average_price", 412.35.into()),
                ("fill_timestamp", filled.into()),
            ]),
            dict(vec![
                ("trade_id", "F1".into()),
                ("exchange", "NFO".into()),
                ("tradingsymbol", "NIFTY26SEPFUT".into()),
                ("transaction_type", "BUY".into()),
                ("quantity", 75_i64.into()),
                ("average_price", 25000_i64.into()),
                ("fill_timestamp", filled.into()),
            ]),
        ],
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    let fills = provider.broker_trades(&ref_()).await.unwrap();
    assert_eq!(fills.len(), 1);
    let fill = &fills[0];
    assert_eq!(fill.symbol, "PWL");
    assert_eq!(fill.quantity, dec("25"));
    assert_eq!(fill.price, dec("412.35"));
    assert_eq!(fill.trade_date, date(2026, 9, 11));
    assert_eq!(
        fill.executed_at,
        Some(baskfy_providers::records::PyDateTime::Naive(filled))
    );
    assert_eq!(fill.order_id.as_deref(), Some("O1"));
}

#[tokio::test]
async fn an_unreadable_equity_trade_refuses_the_whole_read() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        trades: vec![dict(vec![
            ("trade_id", "T1".into()),
            ("exchange", "NSE".into()),
            ("tradingsymbol", "PWL".into()),
            ("quantity", 5_i64.into()),
        ])],
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    let err = provider.broker_trades(&ref_()).await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    assert_eq!(
        err.to_string(),
        "[kite] a trade row could not be read (trade_id='T1', symbol='PWL')"
    );
}

// --- TestBrokerHoldings ----------------------------------------------------------------------

fn holdings_provider(
    env: &kite_common::Env,
    positions: Vec<KiteValue>,
) -> (Arc<FakeKiteClient>, KiteProvider) {
    let client = Arc::new(FakeKiteClient {
        positions,
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client.clone(),
        stored_token(&env.settings),
        None,
        None,
    );
    (client, provider)
}

#[tokio::test]
async fn it_sums_settled_t1_and_collateral() {
    arm();
    let env = configured_settings();
    let (_, provider) = holdings_provider(
        &env,
        vec![dict(vec![
            ("tradingsymbol", "SBIN".into()),
            ("exchange", "NSE".into()),
            ("isin", "INE062A01020".into()),
            ("quantity", 60_i64.into()),
            ("t1_quantity", 10_i64.into()),
            ("collateral_quantity", 30_i64.into()),
            ("average_price", 512.25.into()),
            ("last_price", 540.10.into()),
            ("product", "CNC".into()),
        ])],
    );
    let rows = provider.broker_holdings(&ref_()).await.unwrap();
    assert_eq!(
        rows.iter().map(|r| r.symbol.clone()).collect::<Vec<_>>(),
        vec!["SBIN"]
    );
    assert_eq!(rows[0].total_quantity(), dec("100"));
    assert_eq!(rows[0].quantity, dec("60"));
    assert_eq!(rows[0].collateral_quantity, dec("30"));
    assert_eq!(rows[0].last_price.unwrap().to_string(), "540.1");
}

#[tokio::test]
async fn holding_prices_arrive_as_decimal_not_float() {
    arm();
    let env = configured_settings();
    let (_, provider) = holdings_provider(
        &env,
        vec![dict(vec![
            ("tradingsymbol", "INFY".into()),
            ("quantity", 5_i64.into()),
            ("average_price", 1477.35.into()),
        ])],
    );
    let row = &provider.broker_holdings(&ref_()).await.unwrap()[0];
    assert_eq!(row.average_price, Some(dec("1477.35")));
}

#[tokio::test]
async fn a_missing_average_price_is_unknown_not_zero() {
    arm();
    let env = configured_settings();
    let (_, provider) = holdings_provider(
        &env,
        vec![dict(vec![
            ("tradingsymbol", "INFY".into()),
            ("quantity", 5_i64.into()),
        ])],
    );
    assert_eq!(
        provider.broker_holdings(&ref_()).await.unwrap()[0].average_price,
        None
    );
}

#[tokio::test]
async fn one_unreadable_row_does_not_lose_the_others() {
    arm();
    let env = configured_settings();
    let (_, provider) = holdings_provider(
        &env,
        vec![
            dict(vec![
                ("tradingsymbol", "".into()),
                ("quantity", 1_i64.into()),
            ]),
            dict(vec![("quantity", 2_i64.into())]),
            dict(vec![
                ("tradingsymbol", "TCS".into()),
                ("quantity", 3_i64.into()),
            ]),
        ],
    );
    let symbols: Vec<String> = provider
        .broker_holdings(&ref_())
        .await
        .unwrap()
        .iter()
        .map(|r| r.symbol.clone())
        .collect();
    assert_eq!(symbols, vec!["TCS"]);
}

#[tokio::test]
async fn an_empty_account_is_an_answer_not_an_error() {
    arm();
    let env = configured_settings();
    let (_, provider) = holdings_provider(&env, Vec::new());
    assert!(provider.broker_holdings(&ref_()).await.unwrap().is_empty());
}

#[tokio::test]
async fn a_holdings_token_failure_is_raised_not_swallowed() {
    arm();
    let env = configured_settings();
    let client = Arc::new(raising(KiteExceptionKind::Token, "token expired"));
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    let err = provider.broker_holdings(&ref_()).await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::AccessTokenExpired);
}

#[tokio::test]
async fn another_brokers_account_is_refused() {
    arm();
    let env = configured_settings();
    let (client, provider) = holdings_provider(
        &env,
        vec![dict(vec![
            ("tradingsymbol", "TCS".into()),
            ("quantity", 1_i64.into()),
        ])],
    );
    let err = provider
        .broker_holdings(&kite_common::account(1, "upstox"))
        .await
        .unwrap_err();
    assert!(err.is_unavailable());
    assert!(err.to_string().contains("upstox"), "{err}");
    assert_eq!(
        err.to_string(),
        "[kite] this adapter serves 'zerodha' and was asked for 'upstox'; a broker's holdings \
         are not fetchable with another broker's session"
    );
    assert_eq!(client.calls().holdings_calls, 0);
}

#[tokio::test]
async fn another_tenants_account_is_refused_when_the_owner_is_known() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        positions: vec![dict(vec![
            ("tradingsymbol", "TCS".into()),
            ("quantity", 1_i64.into()),
        ])],
        ..FakeKiteClient::default()
    });
    let provider = KiteProvider::new(
        env.settings.clone(),
        KiteRuntime {
            rate_limiter: Some(Arc::new(CountingLimiter::default())),
            client_factory: Some(factory(client)),
            token_store: Some(stored_token(&env.settings)),
            holdings_account_id: Some(7),
            ..KiteRuntime::default()
        },
    )
    .unwrap();
    let mine = provider
        .broker_holdings(&kite_common::account(7, "zerodha"))
        .await
        .unwrap();
    assert_eq!(mine[0].symbol, "TCS");
    let err = provider
        .broker_holdings(&kite_common::account(8, "zerodha"))
        .await
        .unwrap_err();
    assert!(err.is_unavailable());
    assert!(err.to_string().contains("another tenant"), "{err}");
}

// --- TestBrokerCash --------------------------------------------------------------------------

#[tokio::test]
async fn it_reads_the_equity_segments_net_balance() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        margins: Some(dict(vec![
            ("net", 12345.67.into()),
            ("available", dict(vec![("cash", 12345.67.into())])),
        ])),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client.clone(),
        stored_token(&env.settings),
        None,
        None,
    );
    assert_eq!(
        provider.broker_cash(&ref_()).await.unwrap(),
        Some(dec("12345.67"))
    );
    assert_eq!(
        client.calls().margin_segments,
        vec![Some("equity".to_owned())]
    );
}

#[tokio::test]
async fn it_reads_the_whole_envelope_too() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        margins: Some(dict(vec![
            ("equity", dict(vec![("net", 900.5.into())])),
            ("commodity", dict(vec![("net", 1.0.into())])),
        ])),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    assert_eq!(
        provider.broker_cash(&ref_()).await.unwrap(),
        Some(dec("900.5"))
    );
}

#[tokio::test]
async fn no_reported_balance_is_none_not_zero() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        margins: Some(KiteValue::Map(KiteMap::new())),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    assert_eq!(provider.broker_cash(&ref_()).await.unwrap(), None);
}

// --- TestEmergeSymbolNormalisation -----------------------------------------------------------

async fn emerge_record(tradingsymbol: &str, exchange: &str) -> InstrumentRecord {
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        instruments: vec![dict(vec![
            ("tradingsymbol", tradingsymbol.into()),
            ("name", tradingsymbol.into()),
            ("instrument_type", "EQ".into()),
            ("segment", exchange.into()),
            ("instrument_token", 5_226_241_i64.into()),
            ("lot_size", 1000_i64.into()),
            ("exchange", exchange.into()),
        ])],
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    provider.list_instruments().await.unwrap().remove(0)
}

async fn assert_split(kite_symbol: &str, expected_symbol: &str, expected_series: &str) {
    let record = emerge_record(kite_symbol, "NSE").await;
    assert_eq!(record.symbol, expected_symbol);
    assert_eq!(record.series.as_deref(), Some(expected_series));
    // The whole point: the token must survive onto the row the register will merge with.
    assert_eq!(record.kite_token, Some(5_226_241));
}

#[tokio::test]
async fn the_series_suffix_is_split_off_sm() {
    arm();
    assert_split("SHEETAL-SM", "SHEETAL", "SM").await;
}

#[tokio::test]
async fn the_series_suffix_is_split_off_st() {
    arm();
    assert_split("TANKUP-ST", "TANKUP", "ST").await;
}

#[tokio::test]
async fn the_series_suffix_is_split_off_sz() {
    arm();
    assert_split("SOMENAME-SZ", "SOMENAME", "SZ").await;
}

#[tokio::test]
async fn the_series_suffix_is_split_off_a_hyphenated_symbol() {
    arm();
    // The register's own symbol already contains a hyphen; only the series comes off.
    assert_split("RCDL-RE-ST", "RCDL-RE", "ST").await;
}

#[tokio::test]
async fn a_main_board_symbol_is_untouched() {
    arm();
    let record = emerge_record("RELIANCE", "NSE").await;
    assert_eq!(record.symbol, "RELIANCE");
    assert_eq!(record.series, None);
}

#[tokio::test]
async fn a_rights_entitlement_is_not_treated_as_a_series() {
    arm();
    let record = emerge_record("RCDL-RE", "NSE").await;
    assert_eq!(record.symbol, "RCDL-RE");
    assert_eq!(record.series, None);
}

#[tokio::test]
async fn the_suffix_is_only_stripped_on_nse() {
    arm();
    let record = emerge_record("THING-SM", "BSE").await;
    assert_eq!(record.symbol, "THING-SM");
    assert_eq!(record.series, None);
}

#[tokio::test]
async fn kite_supplies_the_lot_size_the_emerge_register_omits() {
    arm();
    let record = emerge_record("SHEETAL-SM", "NSE").await;
    assert_eq!(record.lot_size, Some(1000));
}

// --- TestQuotes ------------------------------------------------------------------------------

/// `TestQuotes._quote`.
fn quote(last: &str, extra: Vec<(&str, KiteValue)>) -> KiteValue {
    let mut row = KiteMap::new();
    row.insert("instrument_token", 408_065_i64.into());
    row.insert("last_price", last.parse::<f64>().unwrap().into());
    row.insert("volume", 120_000_i64.into());
    row.insert(
        "ohlc",
        dict(vec![
            ("open", 101.0.into()),
            ("high", 104.0.into()),
            ("low", 100.5.into()),
            ("close", 96.0.into()),
        ]),
    );
    row.insert("upper_circuit_limit", 115.2.into());
    row.insert("lower_circuit_limit", 76.8.into());
    for (k, v) in extra {
        row.insert(k, v);
    }
    KiteValue::Map(row)
}

fn quote_client(symbols: &[String]) -> Arc<FakeKiteClient> {
    Arc::new(FakeKiteClient {
        quotes: symbols
            .iter()
            .map(|s| (format!("NSE:{s}"), quote("10", Vec::new())))
            .collect(),
        ..FakeKiteClient::default()
    })
}

#[tokio::test]
async fn a_universe_larger_than_the_cap_is_split_into_batches_of_at_most_500() {
    arm();
    let env = configured_settings();
    let symbols: Vec<String> = (0..1_234).map(|i| format!("SYM{i:04}")).collect();
    let client = quote_client(&symbols);
    let provider = build_provider(
        &env.settings,
        client.clone(),
        stored_token(&env.settings),
        None,
        None,
    );
    let wanted: Vec<&str> = symbols.iter().map(String::as_str).collect();
    let records = provider.quotes(&wanted, "NSE").await.unwrap();
    assert_eq!(records.len(), 1_234);
    let sizes: Vec<usize> = client.calls().quote_batches.iter().map(Vec::len).collect();
    assert_eq!(sizes, vec![500, 500, 234]);
    assert!(sizes.iter().all(|n| *n <= QUOTE_BATCH_SIZE));
}

#[tokio::test]
async fn every_batch_takes_one_token_from_the_limiter() {
    arm();
    let env = configured_settings();
    let limiter = Arc::new(CountingLimiter::default());
    let symbols: Vec<String> = (0..1_001).map(|i| format!("SYM{i:04}")).collect();
    let provider = KiteProvider::new(
        env.settings.clone(),
        KiteRuntime {
            rate_limiter: Some(limiter.clone()),
            client_factory: Some(factory(quote_client(&symbols))),
            token_store: Some(stored_token(&env.settings)),
            ..KiteRuntime::default()
        },
    )
    .unwrap();
    let wanted: Vec<&str> = symbols.iter().map(String::as_str).collect();
    provider.quotes(&wanted, "NSE").await.unwrap();
    assert_eq!(
        limiter.count(),
        3,
        "three batches, three tokens — never a burst"
    );
}

#[tokio::test]
async fn quote_prices_arrive_as_decimal_with_the_exchange_previous_close_and_bands() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        quotes: [("NSE:INFY", quote("110.5", Vec::new()))]
            .into_iter()
            .collect(),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    let records = provider.quotes(&["INFY"], "NSE").await.unwrap();
    assert_eq!(records.len(), 1);
    let record = &records[0];
    assert_eq!(record.symbol, "INFY");
    assert_eq!(record.exchange, "NSE");
    assert_eq!(record.instrument_token, Some(408_065));
    assert_eq!(record.last_price.to_string(), "110.5");
    assert_eq!(record.volume, 120_000);
    assert_eq!(record.prev_close.unwrap().to_string(), "96.0");
    assert_eq!(record.upper_circuit.unwrap().to_string(), "115.2");
    assert_eq!(record.lower_circuit.unwrap().to_string(), "76.8");
    assert_eq!(record.low.unwrap().to_string(), "100.5");
    assert_eq!(record.as_of, None);
}

#[tokio::test]
async fn a_symbol_kite_does_not_answer_for_is_absent_not_invented() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        quotes: [("NSE:INFY", quote("110.5", Vec::new()))]
            .into_iter()
            .collect(),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    let records = provider.quotes(&["INFY", "NOSUCH"], "NSE").await.unwrap();
    assert_eq!(
        records.iter().map(|r| r.symbol.clone()).collect::<Vec<_>>(),
        vec!["INFY"]
    );
}

#[tokio::test]
async fn a_row_without_a_price_is_skipped_and_costs_nobody_else_the_scan() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient {
        quotes: [
            ("NSE:INFY", quote("110.5", Vec::new())),
            (
                "NSE:BROKEN",
                dict(vec![("ohlc", KiteValue::Map(KiteMap::new()))]),
            ),
        ]
        .into_iter()
        .collect(),
        ..FakeKiteClient::default()
    });
    let provider = build_provider(
        &env.settings,
        client,
        stored_token(&env.settings),
        None,
        None,
    );
    let records = provider.quotes(&["BROKEN", "INFY"], "NSE").await.unwrap();
    assert_eq!(
        records.iter().map(|r| r.symbol.clone()).collect::<Vec<_>>(),
        vec!["INFY"]
    );
}

#[tokio::test]
async fn an_empty_request_makes_no_call() {
    arm();
    let env = configured_settings();
    let client = Arc::new(FakeKiteClient::default());
    let provider = build_provider(
        &env.settings,
        client.clone(),
        stored_token(&env.settings),
        None,
        None,
    );
    assert!(provider.quotes(&[], "NSE").await.unwrap().is_empty());
    assert!(client.calls().quote_batches.is_empty());
}
