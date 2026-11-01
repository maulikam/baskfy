//! OP3 — the Kite reads the options run needs. Ports the Kite half of
//! `tests/test_kite_options.py` case for case: depth/OI/timestamps from `quote()`, the 500-key
//! batch cap with one limiter token per batch, `minute_bars`' 60-day windows and aware bars,
//! and the read-only margin calculations. The rate-limit proof against the desk's clocks
//! (`TestTheOptionsReadsWaitOnTheDesksClocks`, `TestTheSharedClock`, `TestTheBudget`) is the
//! factory's (`build_kite_family_limiter`), and is ported with `factory.rs`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;
mod kite_common;

use std::collections::BTreeSet;
use std::sync::Arc;

use baskfy_providers::kite::{KiteMap, KiteValue, MINUTE_MAX_DAYS_PER_REQUEST, QUOTE_BATCH_SIZE};
use baskfy_providers::records::{
    MarginLegFields, MarginLegRecord, MarginOrderType, MarginProduct, MarginVariety, PyDateTime,
    TransactionType,
};
use chrono::{DateTime, Duration, FixedOffset, Utc};
use common::arm;
use kite_common::{
    CountingLimiter, OptionsKite, aware, date, dict, naive, options_provider, today,
};
use rust_decimal::Decimal;

/// `_option_row`: `/quote` for one NFO option, as Kite Connect documents it (5 levels a side).
fn option_row(token: i64, oi: i64, padded: bool) -> KiteValue {
    let level = |price: f64, quantity: i64, orders: i64| {
        dict(vec![
            ("price", price.into()),
            ("quantity", quantity.into()),
            ("orders", orders.into()),
        ])
    };
    let zero = || {
        dict(vec![
            ("price", 0_i64.into()),
            ("quantity", 0_i64.into()),
            ("orders", 0_i64.into()),
        ])
    };
    let mut buy: Vec<KiteValue> = (0..3_i32)
        .map(|i| {
            level(
                101.0 - f64::from(i) * 0.05,
                650 * (i64::from(i) + 1),
                i64::from(i) + 1,
            )
        })
        .collect();
    let mut sell: Vec<KiteValue> = (0..3_i32)
        .map(|i| {
            level(
                101.5 + f64::from(i) * 0.05,
                325 * (i64::from(i) + 1),
                i64::from(i) + 1,
            )
        })
        .collect();
    if padded {
        buy.extend([zero(), zero()]);
        sell.extend([zero(), zero()]);
    }
    dict(vec![
        ("instrument_token", token.into()),
        ("timestamp", naive(2026, 9, 22, 10, 0, 5).into()),
        ("last_trade_time", naive(2026, 9, 22, 10, 0, 1).into()),
        ("last_price", 101.25.into()),
        ("volume", 123_450_i64.into()),
        ("oi", oi.into()),
        ("oi_day_high", (oi + 6500).into()),
        ("oi_day_low", (oi - 6500).into()),
        (
            "depth",
            dict(vec![("buy", buy.into()), ("sell", sell.into())]),
        ),
        (
            "ohlc",
            dict(vec![
                ("open", 99.0.into()),
                ("high", 110.0.into()),
                ("low", 95.0.into()),
                ("close", 98.0.into()),
            ]),
        ),
    ])
}

fn options_kite(quotes: Vec<(&str, KiteValue)>) -> Arc<OptionsKite> {
    Arc::new(OptionsKite {
        quotes: quotes.into_iter().collect(),
        basket: Some(KiteValue::Map(KiteMap::new())),
        ..OptionsKite::default()
    })
}

// --- TestOptionQuotes ------------------------------------------------------------------------

#[tokio::test]
async fn depth_oi_and_both_timestamps_are_read() {
    arm();
    let fake = options_kite(vec![(
        "NFO:NIFTY26SEP25000CE",
        option_row(11, 1_300_000, true),
    )]);
    let (_dir, provider) = options_provider(fake, Arc::new(CountingLimiter::default()));
    let quotes = provider
        .option_quotes(&["NFO:NIFTY26SEP25000CE"])
        .await
        .unwrap();
    assert_eq!(quotes.len(), 1);
    let q = &quotes[0];
    assert_eq!(q.key(), "NFO:NIFTY26SEP25000CE");
    assert_eq!(
        (q.oi, q.oi_day_high, q.oi_day_low),
        (Some(1_300_000), Some(1_306_500), Some(1_293_500))
    );
    let bids: Vec<String> = q.bids.iter().map(|lv| lv.price.to_string()).collect();
    assert_eq!(bids, vec!["101.0", "100.95", "100.9"]);
    assert_eq!(q.asks[0].price.to_string(), "101.5");
    assert_eq!(q.asks[0].quantity, 325);
    // Kite's zero padding is not a zero-priced bid.
    assert_eq!(q.bids.len(), 3);
    assert_eq!(q.asks.len(), 3);
    // Naive Kite stamps are IST; both are kept.
    let as_of = q.as_of.unwrap().aware().unwrap();
    assert_eq!(as_of.offset().local_minus_utc(), 19_800);
    let last_trade = q.last_trade_time.unwrap().aware().unwrap();
    assert!(last_trade < as_of);
}

#[tokio::test]
async fn the_index_and_the_options_share_one_call() {
    arm();
    let fake = options_kite(vec![
        ("NFO:A", option_row(1, 1_300_000, true)),
        (
            "NSE:NIFTY 50",
            dict(vec![
                ("instrument_token", 256_265_i64.into()),
                ("last_price", 25012.35.into()),
            ]),
        ),
    ]);
    let (_dir, provider) = options_provider(fake.clone(), Arc::new(CountingLimiter::default()));
    let got = provider
        .option_quotes(&["NFO:A", "NSE:NIFTY 50"])
        .await
        .unwrap();
    assert_eq!(fake.calls().quote_batches.len(), 1);
    let nifty = got.iter().find(|q| q.key() == "NSE:NIFTY 50").unwrap();
    assert_eq!(nifty.last_price.unwrap().to_string(), "25012.35");
    assert!(nifty.bids.is_empty());
}

async fn batch_case(count: usize) {
    let limiter = Arc::new(CountingLimiter::default());
    let fake = options_kite(Vec::new());
    let (_dir, provider) = options_provider(fake.clone(), limiter.clone());
    let keys: Vec<String> = (0..count).map(|i| format!("NFO:S{i}")).collect();
    let wanted: Vec<&str> = keys.iter().map(String::as_str).collect();
    provider.option_quotes(&wanted).await.unwrap();
    let batches = fake.calls().quote_batches;
    assert!(batches.iter().all(|b| b.len() <= QUOTE_BATCH_SIZE));
    assert_eq!(batches.iter().map(Vec::len).sum::<usize>(), count);
    assert_eq!(limiter.count(), batches.len());
    assert_eq!(batches.len(), count.div_ceil(QUOTE_BATCH_SIZE));
}

#[tokio::test]
async fn a_batch_never_exceeds_500_and_each_batch_is_one_limiter_token_1() {
    arm();
    batch_case(1).await;
}

#[tokio::test]
async fn a_batch_never_exceeds_500_and_each_batch_is_one_limiter_token_125() {
    arm();
    batch_case(125).await;
}

#[tokio::test]
async fn a_batch_never_exceeds_500_and_each_batch_is_one_limiter_token_500() {
    arm();
    batch_case(QUOTE_BATCH_SIZE).await;
}

#[tokio::test]
async fn a_batch_never_exceeds_500_and_each_batch_is_one_limiter_token_501() {
    arm();
    batch_case(QUOTE_BATCH_SIZE + 1).await;
}

#[tokio::test]
async fn a_batch_never_exceeds_500_and_each_batch_is_one_limiter_token_1234() {
    arm();
    batch_case(1_234).await;
}

#[tokio::test]
async fn duplicates_and_blanks_are_not_sent_twice() {
    arm();
    let fake = options_kite(Vec::new());
    let (_dir, provider) = options_provider(fake.clone(), Arc::new(CountingLimiter::default()));
    provider
        .option_quotes(&["NFO:A", "", "NFO:A", "NFO:B"])
        .await
        .unwrap();
    assert_eq!(
        fake.calls().quote_batches,
        vec![vec!["NFO:A".to_owned(), "NFO:B".to_owned()]]
    );
}

#[tokio::test]
async fn an_unreadable_row_costs_only_itself() {
    arm();
    let KiteValue::Map(mut bad) = option_row(2, 1_300_000, true) else {
        panic!("a row is a dict");
    };
    bad.insert("oi", "not a number".into());
    let fake = options_kite(vec![
        ("NFO:A", option_row(1, 1_300_000, true)),
        ("NFO:B", KiteValue::Map(bad)),
    ]);
    let (_dir, provider) = options_provider(fake, Arc::new(CountingLimiter::default()));
    let got = provider.option_quotes(&["NFO:A", "NFO:B"]).await.unwrap();
    assert_eq!(
        got.iter()
            .map(baskfy_providers::records::OptionQuoteRecord::key)
            .collect::<Vec<_>>(),
        vec!["NFO:A"]
    );
}

// --- TestMinuteBars --------------------------------------------------------------------------

/// `_candle(minute, close)`.
fn candle(minute: DateTime<FixedOffset>, close: i64) -> KiteValue {
    dict(vec![
        ("date", minute.into()),
        ("open", (close - 1).into()),
        ("high", (close + 2).into()),
        ("low", (close - 2).into()),
        ("close", close.into()),
        ("volume", 0_i64.into()),
    ])
}

#[test]
fn windows_respect_kites_60_day_minute_cap() {
    arm();
    let (_dir, provider) = options_provider(
        options_kite(Vec::new()),
        Arc::new(CountingLimiter::default()),
    );
    let windows = provider
        .minute_windows(date(2015, 1, 1), date(2026, 9, 22))
        .unwrap();
    assert!(
        windows
            .iter()
            .all(|(lo, hi)| (*hi - *lo).num_days() < MINUTE_MAX_DAYS_PER_REQUEST)
    );
    assert_eq!(windows[0].0, date(2015, 1, 1));
    assert_eq!(windows[windows.len() - 1].1, date(2026, 9, 22));
    for pair in windows.windows(2) {
        assert_eq!(pair[1].0, pair[0].1 + Duration::days(1));
    }
}

#[tokio::test]
async fn each_window_is_one_minute_interval_call_and_bars_come_back_aware() {
    arm();
    let ist = 19_800;
    let fake = Arc::new(OptionsKite {
        candles: vec![
            candle(aware(naive(2026, 7, 1, 9, 15, 0), ist), 25_000),
            candle(aware(naive(2026, 9, 21, 15, 29, 0), ist), 25_100),
        ],
        ..OptionsKite::default()
    });
    let limiter = Arc::new(CountingLimiter::default());
    let (_dir, provider) = options_provider(fake.clone(), limiter.clone());
    let bars = provider
        .minute_bars(256_265, date(2026, 6, 1), date(2026, 9, 22))
        .await
        .unwrap();
    let calls = fake.calls().historical_calls;
    assert_eq!(
        calls.iter().map(|c| c.3.clone()).collect::<BTreeSet<_>>(),
        BTreeSet::from(["minute".to_owned()])
    );
    // 114 days -> two windows.
    assert_eq!(limiter.count(), calls.len());
    assert_eq!(calls.len(), 2);
    assert_eq!(
        bars.iter().map(|b| b.close).collect::<Vec<_>>(),
        vec![Decimal::from(25_000), Decimal::from(25_100)]
    );
    assert!(
        bars.iter()
            .all(|b| matches!(b.ts, PyDateTime::Aware(t) if t.offset().local_minus_utc() == ist))
    );
}

#[tokio::test]
async fn an_aware_bound_is_read_in_ist() {
    arm();
    let fake = options_kite(Vec::new());
    let (_dir, provider) = options_provider(fake.clone(), Arc::new(CountingLimiter::default()));
    // 09:15 IST.
    let utc_open: DateTime<FixedOffset> = aware(naive(2026, 9, 22, 3, 45, 0), 0);
    assert_eq!(
        utc_open.with_timezone(&Utc).to_rfc3339(),
        "2026-09-22T03:45:00+00:00"
    );
    provider
        .minute_bars(256_265, utc_open, utc_open + Duration::minutes(5))
        .await
        .unwrap();
    let (_, lo, hi, _) = fake.calls().historical_calls[0].clone();
    assert_eq!(lo, naive(2026, 9, 22, 9, 15, 0));
    assert_eq!(hi, naive(2026, 9, 22, 9, 20, 0));
}

#[tokio::test]
async fn an_empty_window_is_an_empty_list() {
    arm();
    let (_dir, provider) = options_provider(
        options_kite(Vec::new()),
        Arc::new(CountingLimiter::default()),
    );
    let now = today().date_naive();
    assert!(provider.minute_bars(1, now, now).await.unwrap().is_empty());
}

// --- TestReadOnlyMargins ---------------------------------------------------------------------

fn leg(symbol: &str, side: TransactionType, quantity: i64) -> MarginLegRecord {
    MarginLegRecord::new(MarginLegFields {
        exchange: "NFO".to_owned(),
        tradingsymbol: symbol.to_owned(),
        transaction_type: side,
        quantity,
        product: MarginProduct::default(),
        order_type: MarginOrderType::default(),
        variety: MarginVariety::default(),
        price: Decimal::ZERO,
    })
    .unwrap()
}

#[tokio::test]
async fn basket_margins_is_a_calculation_without_positions() {
    arm();
    let fake = Arc::new(OptionsKite {
        basket: Some(dict(vec![
            (
                "initial",
                dict(vec![("total", 180_000.5.into()), ("span", 1_i64.into())]),
            ),
            (
                "final",
                dict(vec![("total", 42_000.25.into()), ("span", 1_i64.into())]),
            ),
            ("orders", KiteValue::List(Vec::new())),
        ])),
        ..OptionsKite::default()
    });
    let (_dir, provider) = options_provider(fake.clone(), Arc::new(CountingLimiter::default()));
    let legs = vec![
        leg("W", TransactionType::Buy, 65),
        leg("S", TransactionType::Sell, 65),
    ];
    let answer = provider.basket_order_margins(&legs, false).await.unwrap();
    assert_eq!(
        (
            answer.initial_total.unwrap().to_string(),
            answer.final_total.unwrap().to_string()
        ),
        ("180000.5".to_owned(), "42000.25".to_owned())
    );
    let calls = fake.calls().basket_calls;
    assert_eq!(calls.len(), 1);
    let (params, consider_positions) = &calls[0];
    assert!(!consider_positions);
    let products: BTreeSet<String> = params
        .iter()
        .map(|p| p.get("product").unwrap().py_str())
        .collect();
    assert_eq!(products, BTreeSet::from(["MIS".to_owned()]));
    assert_eq!(
        answer.shape["top"],
        vec![
            "final".to_owned(),
            "initial".to_owned(),
            "orders".to_owned()
        ]
    );
    assert_eq!(answer.legs, 2);
}

#[tokio::test]
async fn a_client_without_the_verb_is_refused_not_guessed() {
    arm();
    let plain = Arc::new(OptionsKite::default());
    let (_dir, provider) = options_provider(plain, Arc::new(CountingLimiter::default()));
    let err = provider
        .basket_order_margins(&[leg("W", TransactionType::Buy, 1)], false)
        .await
        .unwrap_err();
    assert!(err.is_unavailable());
    assert!(err.to_string().contains("basket_order_margins"), "{err}");
}

#[tokio::test]
async fn the_margins_read_names_fields_and_never_a_figure() {
    arm();
    let fake = Arc::new(OptionsKite {
        margins: Some(dict(vec![
            (
                "equity",
                dict(vec![
                    ("net", 123_456.78.into()),
                    ("available", dict(vec![("cash", 99.5.into())])),
                    ("enabled", true.into()),
                ]),
            ),
            ("commodity", dict(vec![("net", 0.0.into())])),
        ])),
        ..OptionsKite::default()
    });
    let (_dir, provider) = options_provider(fake, Arc::new(CountingLimiter::default()));
    let shape = provider.margins_shape().await.unwrap();
    assert_eq!(
        shape,
        serde_json::json!({
            "commodity": {"net": "float"},
            "equity": {"available": ["cash"], "enabled": "bool", "net": "float"},
        })
    );
    assert!(!shape.to_string().contains("123456"));
}
