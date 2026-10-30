//! `tests/test_swing_monitor.py`'s strategy tests, driven through the runner's tick reading
//! ([`super::runner::tick_of`]) exactly as the loop drives them: `TestTheStrategy`,
//! `TestTheRangeIsTheTicks`, `TestTheReplay` (the fixture morning in `tools/swing/fixtures`,
//! with `tools/swing/replay.py`'s candle-to-tick convention ported below) and
//! `TestTelemetryNeverStopsAVerdict`'s verdict half. Where Python reads a private attribute
//! (`state[t].reconciled`), the test observes the same fact through the candle source's asks.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_core::swing::opening_range::TriggerState;
use baskfy_core::swing::setups::{Setup, SwingConfig};
use chrono::{NaiveDate, NaiveDateTime};
use rust_decimal::{Decimal, RoundingStrategy};

use super::runner::tick_of;
use super::tests::{ListStore, NoCandles, at, day, drive, name, named, tick};
use crate::lanes::swing::breakout::{
    CandleSource, Signal, SignalStore, SwingBreakout, WatchedName, trigger_state_str,
};
use crate::lanes::swing::rules::Candle;
use crate::wire::{PyDict, PyValue};
use baskfy_providers::records::PyDateTime;

fn d(s: &str) -> Decimal {
    s.parse().unwrap()
}

fn strategy(watchlist: Vec<WatchedName>) -> SwingBreakout {
    SwingBreakout::new(watchlist, day(), None, SwingConfig::default()).unwrap()
}

fn states(store: &ListStore) -> Vec<TriggerState> {
    store.signals.iter().map(|s| s.verdict.state).collect()
}

/// `CountingCandles`: records every ask and answers a fixed candle set.
struct CountingCandles {
    candles: Vec<Candle>,
    asks: Mutex<Vec<NaiveDateTime>>,
    explode: bool,
}

impl CountingCandles {
    fn new(candles: Vec<Candle>) -> Self {
        Self {
            candles,
            asks: Mutex::new(Vec::new()),
            explode: false,
        }
    }
    fn asks(&self) -> Vec<NaiveDateTime> {
        self.asks.lock().unwrap().clone()
    }
}

#[async_trait]
impl CandleSource for CountingCandles {
    async fn minute_candles(
        &self,
        _token: i64,
        _day: NaiveDate,
        until: NaiveDateTime,
    ) -> Result<Vec<Candle>, String> {
        self.asks.lock().unwrap().push(until);
        if self.explode {
            return Err("RuntimeError: historical API down".to_owned());
        }
        Ok(self
            .candles
            .iter()
            .filter(|c| c.start <= until)
            .cloned()
            .collect())
    }
}

/// `_candle("HH:MM", o, h, l, c)`.
fn candle(h: u32, m: u32, o: &str, hi: &str, lo: &str, c: &str) -> Candle {
    Candle {
        start: at(h, m),
        open: d(o),
        high: d(hi),
        low: d(lo),
        close: d(c),
        volume: 1,
    }
}

// =============================================================================================
// TestTheStrategy
// =============================================================================================

#[tokio::test]
async fn test_a_name_triggers_once_and_is_then_done_for_the_session() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut store,
        &NoCandles,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 98.0, 9, 17, Some(98.0)),
            tick(1, 99.5, 9, 19, None),
            // The window closes; the range is the ticks INSIDE [09:15, 09:20): 98.0-99.5 (A4).
            tick(1, 99.4, 9, 20, None),
            // > 99.5 × 1.001 and > the pivot: TRIGGERED.
            tick(1, 100.5, 9, 25, None),
            tick(1, 101.0, 9, 26, None),
            tick(1, 102.0, 9, 30, None),
        ],
    )
    .await;
    assert_eq!(states(&store), vec![TriggerState::Triggered]);
    assert_eq!(store.signals[0].verdict.entry, Some(d("100.5")));
    assert_eq!(store.signals[0].verdict.stop, Some(d("98.0")));
}

#[tokio::test]
async fn test_a_locked_name_yields_one_locked_signal_and_never_triggers() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![named("LCK", 3, Setup::Flag, Some("50"), Some("52.5"))]);
    let ticks: Vec<PyDict> = (15..40).map(|m| tick(3, 52.5, 9, m, None)).collect();
    drive(&mut s, &mut store, &NoCandles, &ticks).await;
    assert_eq!(states(&store), vec![TriggerState::LockedUpperCircuit]);
    assert!(store.signals.iter().all(|s| s.verdict.entry.is_none()));
}

#[tokio::test]
async fn test_a_tick_for_a_token_not_on_the_list_is_ignored() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut store,
        &NoCandles,
        &[tick(99, 1000.0, 9, 30, None)],
    )
    .await;
    assert!(store.signals.is_empty());
    assert!(s.opening(99).is_none() && s.last_state(99).is_none());
    assert!(
        s.verdict_seconds.is_empty(),
        "an unwatched token is not evaluated"
    );
}

#[tokio::test]
async fn test_a_break_after_1045_is_now_a_trigger() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut store,
        &NoCandles,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 99.0, 9, 20, None),
            tick(1, 150.0, 10, 46, None),
        ],
    )
    .await;
    assert_eq!(store.signals.len(), 1, "an afternoon break raised nothing");
    assert_eq!(store.signals[0].verdict.state, TriggerState::Triggered);
}

#[tokio::test]
async fn test_after_the_market_closes_nothing_is_raised() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut store,
        &NoCandles,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 99.0, 9, 20, None),
            tick(1, 150.0, 15, 31, None),
        ],
    )
    .await;
    assert!(store.signals.is_empty());
    assert!(s.session_over(at(15, 31)));
    assert!(!s.session_over(at(15, 30)));
    // The old boundary is squarely inside the window now — that IS the change.
    assert!(!s.session_over(at(10, 46)));
}

#[tokio::test]
async fn test_a_failing_store_does_not_stop_the_monitor() {
    struct Broken;
    #[async_trait]
    impl SignalStore for Broken {
        async fn raise_signal(&mut self, _signal: &Signal) -> Result<(), String> {
            Err("RuntimeError: db down".to_owned())
        }
    }
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut Broken,
        &NoCandles,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 99.0, 9, 20, None),
            tick(1, 101.0, 9, 25, None),
        ],
    )
    .await;
    let raised: Vec<TriggerState> = s.signals.iter().map(|s| s.verdict.state).collect();
    assert_eq!(raised, vec![TriggerState::Triggered]);
}

#[test]
fn test_the_window_must_be_one_the_contract_names() {
    let err = SwingBreakout::new(vec![name("AAA", 1)], day(), Some(7), SwingConfig::default())
        .err()
        .unwrap();
    assert!(err.0.contains("window"), "{}", err.0);
}

// =============================================================================================
// TestTheRangeIsTheTicks
// =============================================================================================

#[tokio::test]
async fn test_the_range_is_built_from_the_ticks_inside_the_window_at_its_close() {
    let source = CountingCandles::new(Vec::new());
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut ListStore::default(),
        &source,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 98.0, 9, 17, Some(98.0)),
            tick(1, 99.5, 9, 19, None),
            tick(1, 99.9, 9, 20, None),
        ],
    )
    .await;
    let opening = s.opening(1).unwrap();
    assert_eq!(
        (opening.high, opening.low, opening.complete),
        (d("99.5"), d("98.0"), true)
    );
    assert!(
        source.asks().is_empty(),
        "the candle source was asked at window close"
    );
}

#[tokio::test]
async fn test_the_minute_candle_is_fetched_once_a_minute_later_only_to_reconcile() {
    let source = CountingCandles::new(vec![
        candle(9, 15, "99", "99.6", "97.9", "99.2"),
        candle(9, 20, "99.2", "99.4", "99.0", "99.1"),
    ]);
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut ListStore::default(),
        &source,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 99.5, 9, 19, None),
            tick(1, 99.3, 9, 20, None),
            tick(1, 99.2, 9, 21, None),
            tick(1, 99.1, 9, 22, None),
            tick(1, 99.0, 9, 30, None),
        ],
    )
    .await;
    assert_eq!(source.asks(), vec![at(9, 21)]);
    let opening = s.opening(1).unwrap();
    assert_eq!((opening.high, opening.low), (d("99.6"), d("97.9")));
    assert!(
        s.reconcile_note(1)
            .unwrap()
            .contains("reconciled to candles 97.9-99.6")
    );
}

#[tokio::test]
async fn test_a_matching_candle_range_changes_nothing() {
    let source = CountingCandles::new(vec![
        candle(9, 15, "99", "99.5", "99.0", "99.2"),
        candle(9, 20, "99.2", "99.4", "99.0", "99.1"),
    ]);
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut ListStore::default(),
        &source,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 99.5, 9, 19, None),
            tick(1, 99.3, 9, 20, None),
            tick(1, 99.2, 9, 21, None),
        ],
    )
    .await;
    assert_eq!(source.asks().len(), 1);
    assert!(s.reconcile_note(1).is_none());
}

#[tokio::test]
async fn test_a_signal_raised_on_the_tick_range_stands_after_the_reconcile() {
    let mut store = ListStore::default();
    let source = CountingCandles::new(vec![
        candle(9, 15, "99", "101.0", "98.0", "99.2"),
        candle(9, 20, "99.2", "100.6", "99.0", "100.5"),
    ]);
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut store,
        &source,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 99.5, 9, 19, Some(98.0)),
            tick(1, 100.5, 9, 20, None),
            tick(1, 100.6, 9, 21, None),
        ],
    )
    .await;
    assert_eq!(states(&store), vec![TriggerState::Triggered]);
    assert_eq!(store.signals[0].verdict.entry, Some(d("100.5")));
    // A name that has triggered is done for the session: its candles are never asked for (so
    // never reconciled).
    assert!(source.asks().is_empty());
}

#[tokio::test]
async fn test_a_name_with_no_tick_inside_the_window_gets_its_range_from_the_candles() {
    let source = CountingCandles::new(vec![
        candle(9, 15, "99", "99.5", "98.0", "99.2"),
        candle(9, 20, "99.2", "99.4", "99.0", "99.1"),
    ]);
    let mut s = strategy(vec![name("AAA", 1)]);
    let mut store = ListStore::default();
    drive(
        &mut s,
        &mut store,
        &source,
        &[tick(1, 99.3, 9, 20, None), tick(1, 99.2, 9, 20, None)],
    )
    .await;
    assert!(s.opening(1).is_none() && source.asks().is_empty());
    drive(&mut s, &mut store, &source, &[tick(1, 99.2, 9, 21, None)]).await;
    assert_eq!(s.opening(1).unwrap().high, d("99.5"));
    assert!(
        s.reconcile_note(1)
            .unwrap()
            .contains("no tick inside the window")
    );
}

#[tokio::test]
async fn test_a_failing_candle_source_leaves_the_tick_range_standing() {
    let mut source = CountingCandles::new(Vec::new());
    source.explode = true;
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut ListStore::default(),
        &source,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 99.5, 9, 19, None),
            tick(1, 99.3, 9, 20, None),
            tick(1, 99.2, 9, 21, None),
            tick(1, 99.2, 9, 22, None),
        ],
    )
    .await;
    assert_eq!(s.opening(1).unwrap().high, d("99.5"));
    // `reconciled is True`: asked once at 09:21, and not again at 09:22.
    assert_eq!(source.asks(), vec![at(9, 21)]);
}

#[tokio::test]
#[allow(non_snake_case)]
async fn test_a_monitor_started_in_the_afternoon_still_uses_the_MORNING_range() {
    let source = CountingCandles::new(vec![
        candle(9, 15, "99", "99.5", "98.0", "99.2"),
        candle(9, 20, "99.2", "99.4", "99.0", "99.1"),
    ]);
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut store,
        &source,
        &[tick(1, 101.0, 13, 10, Some(99.8))],
    )
    .await;
    let opening = s.opening(1).unwrap();
    assert_eq!(
        (opening.high, opening.low),
        (d("99.5"), d("98.0")),
        "the afternoon's own prints became the opening range"
    );
    assert_eq!(states(&store), vec![TriggerState::Triggered]);
    assert_eq!(store.signals[0].verdict.entry, Some(d("101.0")));
}

#[tokio::test]
async fn test_an_afternoon_start_with_no_candles_refuses_to_trigger_at_all() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1)]);
    drive(
        &mut s,
        &mut store,
        &NoCandles,
        &[tick(1, 101.0, 13, 10, None), tick(1, 120.0, 14, 0, None)],
    )
    .await;
    assert!(s.opening(1).is_none());
    assert!(store.signals.is_empty());
}

// =============================================================================================
// TestTelemetryNeverStopsAVerdict: the verdict half
// =============================================================================================

#[tokio::test]
async fn test_observe_raises_in_the_verdict_sink_is_dropped_and_the_signal_still_fires() {
    let calls = Arc::new(Mutex::new(0_usize));
    let seen = calls.clone();
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1)]);
    s.observe_with(Some(Box::new(move |_: f64| {
        *seen.lock().unwrap() += 1;
        Err("RuntimeError: metrics down".to_owned())
    })));
    drive(
        &mut s,
        &mut store,
        &NoCandles,
        &[
            tick(1, 99.0, 9, 15, None),
            tick(1, 99.5, 9, 19, None),
            tick(1, 99.4, 9, 20, None),
            tick(1, 100.5, 9, 25, None),
        ],
    )
    .await;
    assert_eq!(states(&store), vec![TriggerState::Triggered]);
    assert_eq!(s.verdict_seconds.len(), 4);
    assert_eq!(
        *calls.lock().unwrap(),
        1,
        "the sink is dropped after it raises (_observe is None)"
    );
}

// =============================================================================================
// TestTheReplay: `tools/swing/replay.py`, ported for the fixture morning
// =============================================================================================

fn fixtures() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tools/swing/fixtures")
}

/// `read_candles`: `token,symbol,start,open,high,low,close,volume` → candles per token, sorted.
fn read_candles(path: &std::path::Path) -> BTreeMap<i64, Vec<Candle>> {
    let text = std::fs::read_to_string(path).unwrap();
    let mut out: BTreeMap<i64, Vec<Candle>> = BTreeMap::new();
    let mut lines = text.lines();
    let header: Vec<&str> = lines.next().unwrap().split(',').collect();
    for line in lines.filter(|l| !l.trim().is_empty()) {
        let f: BTreeMap<&str, &str> = header.iter().copied().zip(line.split(',')).collect();
        out.entry(f["token"].parse().unwrap())
            .or_default()
            .push(Candle {
                start: NaiveDateTime::parse_from_str(f["start"], "%Y-%m-%dT%H:%M:%S").unwrap(),
                open: d(f["open"]),
                high: d(f["high"]),
                low: d(f["low"]),
                close: d(f["close"]),
                volume: f["volume"].parse().unwrap_or(0),
            });
    }
    for series in out.values_mut() {
        series.sort_by_key(|c| c.start);
    }
    out
}

/// `ticks_from_candles`: each candle as four ticks — open, low, high, close at +0, +15, +30,
/// +45 s — with the session's running low, merged in time then token order.
fn ticks_from_candles(candles: &BTreeMap<i64, Vec<Candle>>) -> Vec<PyDict> {
    let mut ticks: Vec<(NaiveDateTime, i64, usize, PyDict)> = Vec::new();
    let mut n = 0;
    for (token, series) in candles {
        let mut running: Option<Decimal> = None;
        for c in series {
            running = Some(running.map_or(c.low, |r| r.min(c.low)));
            for (price, offset) in [(c.open, 0), (c.low, 15), (c.high, 30), (c.close, 45)] {
                let stamp = c.start + chrono::Duration::seconds(offset);
                let mut t = PyDict::default();
                t.insert("instrument_token", PyValue::Int(*token));
                t.insert(
                    "last_price",
                    PyValue::Float(price.to_string().parse().unwrap()),
                );
                t.insert(
                    "exchange_timestamp",
                    PyValue::DateTime(PyDateTime::Naive(stamp)),
                );
                let mut ohlc = PyDict::default();
                ohlc.insert(
                    "low",
                    PyValue::Float(running.unwrap().to_string().parse().unwrap()),
                );
                t.insert("ohlc", PyValue::Map(ohlc));
                ticks.push((stamp, *token, n, t));
                n += 1;
            }
        }
    }
    ticks.sort_by_key(|t| (t.0, t.1, t.2));
    ticks.into_iter().map(|t| t.3).collect()
}

/// `read_watchlist`: the day, the window, the names.
fn read_watchlist(path: &std::path::Path) -> (NaiveDate, Option<i64>, Vec<WatchedName>) {
    let v: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let names = v["names"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| WatchedName {
            watch_id: e["watch_id"].as_i64().unwrap(),
            instrument_id: e["instrument_id"].as_i64().unwrap(),
            symbol: e["symbol"].as_str().unwrap().to_owned(),
            token: e["token"].as_i64().unwrap(),
            setup: match e["setup"].as_str().unwrap() {
                "FLAG" => Setup::Flag,
                "EP" => Setup::Ep,
                other => panic!("{other}"),
            },
            pivot_high: e["pivot_high"].as_str().map(d),
            upper_circuit: e["upper_circuit"].as_str().map(d),
            focus: false,
        })
        .collect();
    (
        NaiveDate::parse_from_str(v["day"].as_str().unwrap(), "%Y-%m-%d").unwrap(),
        v["window_minutes"].as_i64(),
        names,
    )
}

/// `CsvCandles`: the recording as the candle source, every request recorded.
struct CsvCandles {
    candles: BTreeMap<i64, Vec<Candle>>,
    requests: Mutex<Vec<(i64, NaiveDateTime)>>,
}

#[async_trait]
impl CandleSource for CsvCandles {
    async fn minute_candles(
        &self,
        token: i64,
        day: NaiveDate,
        until: NaiveDateTime,
    ) -> Result<Vec<Candle>, String> {
        self.requests.lock().unwrap().push((token, until));
        Ok(self
            .candles
            .get(&token)
            .map(|cs| {
                cs.iter()
                    .filter(|c| c.start.date() == day && c.start <= until)
                    .cloned()
                    .collect()
            })
            .unwrap_or_default())
    }
}

/// `Raised`: what a signal is compared as.
#[derive(Debug, PartialEq, Eq)]
struct Raised {
    symbol: String,
    state: String,
    at: String,
    entry: Option<String>,
    stop: Option<String>,
}

fn two_places(x: Decimal) -> String {
    format!(
        "{:.2}",
        x.round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven)
    )
}

impl Raised {
    fn of(s: &Signal) -> Self {
        Self {
            symbol: s.watch.symbol.clone(),
            state: trigger_state_str(s.verdict.state).to_owned(),
            at: s.at.format("%Y-%m-%dT%H:%M:00").to_string(),
            entry: s.verdict.entry.map(two_places),
            stop: s.verdict.stop.map(two_places),
        }
    }
}

/// `replay(...)`: drive the strategy with the recording; the signals and the candle source.
async fn replay(stem: &str) -> (Vec<Signal>, CsvCandles, Vec<Raised>) {
    let (day, window, watchlist) =
        read_watchlist(&fixtures().join(format!("{stem}.watchlist.json")));
    let candles = read_candles(&fixtures().join(format!("{stem}.csv")));
    let ticks = ticks_from_candles(&candles);
    let source = CsvCandles {
        candles,
        requests: Mutex::new(Vec::new()),
    };
    let mut store = ListStore::default();
    let mut s = SwingBreakout::new(watchlist, day, window, SwingConfig::default()).unwrap();
    for t in &ticks {
        let read = tick_of(t, day.and_hms_opt(12, 0, 0).unwrap()).unwrap();
        s.on_tick(&read, &mut store, &source).await;
    }
    let expected: Vec<serde_json::Value> = serde_json::from_str(
        &std::fs::read_to_string(fixtures().join(format!("{stem}.expected.json"))).unwrap(),
    )
    .unwrap();
    let expected = expected
        .iter()
        .map(|e| Raised {
            symbol: e["symbol"].as_str().unwrap().to_owned(),
            state: e["state"].as_str().unwrap().to_owned(),
            at: e["at"].as_str().unwrap().to_owned(),
            entry: e["entry"].as_str().map(str::to_owned),
            stop: e["stop"].as_str().map(str::to_owned),
        })
        .collect();
    (store.signals, source, expected)
}

#[tokio::test]
async fn test_the_fixture_morning_raises_exactly_the_expected_signals() {
    let (signals, _, expected) = replay("morning-synthetic").await;
    let raised: Vec<Raised> = signals.iter().map(Raised::of).collect();
    assert_eq!(raised, expected);
}

/// `replay.main([...])`: the harness's verdict line and exit code.
fn harness_verdict(raised: &[Raised], expected: &[Raised]) -> (i32, String) {
    if raised == expected {
        (
            0,
            format!("OK — {} signals, exactly as expected", raised.len()),
        )
    } else {
        (1, "MISMATCH".to_owned())
    }
}

#[tokio::test]
async fn test_the_harness_exit_code_says_so() {
    let (signals, _, expected) = replay("morning-synthetic").await;
    let raised: Vec<Raised> = signals.iter().map(Raised::of).collect();
    let (code, out) = harness_verdict(&raised, &expected);
    assert_eq!(code, 0);
    assert!(out.contains("OK"), "{out}");
    let (code, _) = harness_verdict(&raised[1..], &expected);
    assert_eq!(code, 1);
}

#[test]
fn test_the_expected_entry_and_stop_are_the_rules_not_the_fixture() {
    let expected: Vec<serde_json::Value> = serde_json::from_str(
        &std::fs::read_to_string(fixtures().join("morning-synthetic.expected.json")).unwrap(),
    )
    .unwrap();
    let alpha = expected
        .iter()
        .find(|e| e["symbol"] == "ALPHAFLAG")
        .unwrap();
    assert_eq!(
        (alpha["entry"].as_str(), alpha["stop"].as_str()),
        (Some("100.80"), Some("97.80"))
    );
}

#[tokio::test]
async fn test_the_fixture_replay_asks_for_candles_once_per_name_a_minute_after_the_window() {
    let (signals, source, _) = replay("morning-synthetic").await;
    assert_eq!(signals.len(), 4);
    let mut by_token: BTreeMap<i64, Vec<NaiveDateTime>> = BTreeMap::new();
    for (token, until) in source.requests.lock().unwrap().iter() {
        by_token.entry(*token).or_default().push(*until);
    }
    assert!(by_token.values().all(|v| v.len() == 1), "{by_token:?}");
    assert!(by_token.values().all(|v| v[0] >= at(9, 21)), "{by_token:?}");
}
