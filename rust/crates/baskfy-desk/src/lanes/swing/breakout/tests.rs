//! `tests/test_swing_monitor.py`'s `TestTheStrategy` and `TestTheRangeIsTheTicks`: the
//! opening-range strategy's own rules (one signal per name per session, the locked name, the
//! stranger's tick, the afternoon break, the close, a failing store; the range from the ticks
//! inside the window, the one candle read a minute later, the reconcile note).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::str::FromStr;
use std::sync::Mutex;

use async_trait::async_trait;
use baskfy_core::swing::opening_range::TriggerState;
use baskfy_core::swing::setups::{Setup, SwingConfig};
use chrono::{NaiveDate, NaiveDateTime};
use rust_decimal::Decimal;

use super::{CandleSource, Signal, SignalStore, SwingBreakout, Tick, WatchedName};
use crate::lanes::swing::rules::Candle;

fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 19).unwrap()
}

fn d(text: &str) -> Decimal {
    Decimal::from_str(text).unwrap()
}

fn hm(text: &str) -> NaiveDateTime {
    let (h, m) = text.split_once(':').unwrap();
    day()
        .and_hms_opt(h.parse().unwrap(), m.parse().unwrap(), 0)
        .unwrap()
}

fn name(symbol: &str, token: i64, pivot: Option<&str>, circuit: Option<&str>) -> WatchedName {
    WatchedName {
        watch_id: token,
        instrument_id: token * 10,
        symbol: symbol.to_owned(),
        token,
        setup: Setup::Flag,
        pivot_high: pivot.map(d),
        upper_circuit: circuit.map(d),
        focus: false,
    }
}

fn tick(token: i64, price: &str, at: &str, low: Option<&str>) -> Tick {
    Tick {
        instrument_token: token,
        last_price: Some(d(price)),
        day_low: Some(d(low.unwrap_or(price))),
        at: hm(at),
    }
}

#[derive(Default)]
struct ListStore {
    signals: Vec<Signal>,
}

#[async_trait]
impl SignalStore for ListStore {
    async fn raise_signal(&mut self, signal: &Signal) -> Result<(), String> {
        self.signals.push(signal.clone());
        Ok(())
    }
}

struct Broken;

#[async_trait]
impl SignalStore for Broken {
    async fn raise_signal(&mut self, _signal: &Signal) -> Result<(), String> {
        Err("db down".to_owned())
    }
}

#[derive(Default)]
struct Counting {
    candles: Vec<Candle>,
    asks: Mutex<Vec<NaiveDateTime>>,
}

#[async_trait]
impl CandleSource for Counting {
    async fn minute_candles(
        &self,
        _token: i64,
        _day: NaiveDate,
        until: NaiveDateTime,
    ) -> Result<Vec<Candle>, String> {
        self.asks.lock().unwrap().push(until);
        Ok(self.candles.clone())
    }
}

fn candle(start: &str, o: &str, h: &str, l: &str, c: &str) -> Candle {
    Candle {
        start: hm(start),
        open: d(o),
        high: d(h),
        low: d(l),
        close: d(c),
        volume: 1,
    }
}

fn strategy(names: Vec<WatchedName>) -> SwingBreakout {
    SwingBreakout::new(names, day(), None, SwingConfig::default()).unwrap()
}

async fn drive(
    s: &mut SwingBreakout,
    ticks: &[Tick],
    store: &mut dyn SignalStore,
    candles: &dyn CandleSource,
) {
    for t in ticks {
        s.on_tick(t, store, candles).await;
    }
}

fn states(signals: &[Signal]) -> Vec<TriggerState> {
    signals.iter().map(|s| s.verdict.state).collect()
}

#[tokio::test]
async fn a_name_triggers_once_and_is_then_done_for_the_session() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let ticks = [
        tick(1, "99.0", "09:15", None),
        tick(1, "98.0", "09:17", Some("98.0")),
        tick(1, "99.5", "09:19", None),
        tick(1, "99.4", "09:20", None),
        tick(1, "100.5", "09:25", None),
        tick(1, "101.0", "09:26", None),
        tick(1, "102.0", "09:30", None),
    ];
    drive(&mut s, &ticks, &mut store, &Counting::default()).await;
    assert_eq!(states(&store.signals), [TriggerState::Triggered]);
    assert_eq!(store.signals[0].verdict.entry, Some(d("100.5")));
    assert_eq!(store.signals[0].verdict.stop, Some(d("98.0")));
}

#[tokio::test]
async fn a_locked_name_yields_one_locked_signal_and_never_triggers() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("LCK", 3, Some("50"), Some("52.5"))]);
    let ticks: Vec<Tick> = (15..40)
        .map(|m| tick(3, "52.5", &format!("09:{m:02}"), None))
        .collect();
    drive(&mut s, &ticks, &mut store, &Counting::default()).await;
    assert_eq!(states(&store.signals), [TriggerState::LockedUpperCircuit]);
    assert!(store.signals.iter().all(|x| x.verdict.entry.is_none()));
}

#[tokio::test]
async fn a_tick_for_a_token_not_on_the_list_is_ignored() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    drive(
        &mut s,
        &[tick(99, "1000.0", "09:30", None)],
        &mut store,
        &Counting::default(),
    )
    .await;
    assert!(store.signals.is_empty());
    assert!(!s.tokens().contains(&99));
    assert!(s.last_state(99).is_none());
}

#[tokio::test]
async fn a_break_after_1045_is_now_a_trigger() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let ticks = [
        tick(1, "99.0", "09:15", None),
        tick(1, "99.0", "09:20", None),
        tick(1, "150.0", "10:46", None),
    ];
    drive(&mut s, &ticks, &mut store, &Counting::default()).await;
    assert_eq!(states(&store.signals), [TriggerState::Triggered]);
}

#[tokio::test]
async fn after_the_market_closes_nothing_is_raised() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let ticks = [
        tick(1, "99.0", "09:15", None),
        tick(1, "99.0", "09:20", None),
        tick(1, "150.0", "15:31", None),
    ];
    drive(&mut s, &ticks, &mut store, &Counting::default()).await;
    assert!(store.signals.is_empty());
    assert!(s.session_over(hm("15:31")));
    assert!(!s.session_over(hm("15:30")));
    assert!(!s.session_over(hm("10:46")));
}

#[tokio::test]
async fn a_failing_store_does_not_stop_the_monitor() {
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let ticks = [
        tick(1, "99.0", "09:15", None),
        tick(1, "99.0", "09:20", None),
        tick(1, "101.0", "09:25", None),
    ];
    drive(&mut s, &ticks, &mut Broken, &Counting::default()).await;
    assert_eq!(states(&s.signals), [TriggerState::Triggered]);
}

#[test]
fn the_window_must_be_one_the_contract_names() {
    let refused = SwingBreakout::new(Vec::new(), day(), Some(7), SwingConfig::default());
    assert!(refused.is_err());
    let s = SwingBreakout::new(Vec::new(), day(), Some(60), SwingConfig::default()).unwrap();
    assert_eq!(s.window_minutes, 60);
}

#[test]
fn the_strategy_never_places() {
    let s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    assert!(s.generate_targets().is_empty());
    let code: String = include_str!("../breakout.rs")
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    for verb in [
        "Gateway",
        concat!("Kite", "Writes"),
        "place_order",
        "place_gtt",
        "cancel_order",
        ".place(",
    ] {
        assert!(!code.contains(verb), "breakout.rs names {verb}");
    }
}

#[tokio::test]
async fn the_range_is_built_from_the_ticks_inside_the_window_at_its_close() {
    let source = Counting::default();
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let ticks = [
        tick(1, "99.0", "09:15", None),
        tick(1, "98.0", "09:17", Some("98.0")),
        tick(1, "99.5", "09:19", None),
        tick(1, "99.9", "09:20", None),
    ];
    drive(&mut s, &ticks, &mut store, &source).await;
    let opening = s.opening(1).unwrap();
    assert_eq!(
        (opening.high, opening.low, opening.complete),
        (d("99.5"), d("98.0"), true)
    );
    assert!(source.asks.lock().unwrap().is_empty());
}

#[tokio::test]
async fn the_minute_candle_is_fetched_once_a_minute_later_only_to_reconcile() {
    let source = Counting {
        candles: vec![
            candle("09:15", "99", "99.6", "97.9", "99.2"),
            candle("09:20", "99.2", "99.4", "99.0", "99.1"),
        ],
        ..Counting::default()
    };
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let ticks = [
        tick(1, "99.0", "09:15", None),
        tick(1, "99.5", "09:19", None),
        tick(1, "99.3", "09:20", None),
        tick(1, "99.2", "09:21", None),
        tick(1, "99.1", "09:22", None),
        tick(1, "99.0", "09:30", None),
    ];
    drive(&mut s, &ticks, &mut store, &source).await;
    assert_eq!(*source.asks.lock().unwrap(), [hm("09:21")]);
    let opening = s.opening(1).unwrap();
    assert_eq!((opening.high, opening.low), (d("99.6"), d("97.9")));
    assert!(
        s.reconcile_note(1)
            .unwrap()
            .contains("reconciled to candles 97.9-99.6")
    );
}

#[tokio::test]
async fn a_matching_candle_range_changes_nothing() {
    let source = Counting {
        candles: vec![
            candle("09:15", "99", "99.5", "99.0", "99.2"),
            candle("09:20", "99.2", "99.4", "99.0", "99.1"),
        ],
        ..Counting::default()
    };
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let ticks = [
        tick(1, "99.0", "09:15", None),
        tick(1, "99.5", "09:19", None),
        tick(1, "99.3", "09:20", None),
        tick(1, "99.2", "09:21", None),
    ];
    drive(&mut s, &ticks, &mut store, &source).await;
    assert_eq!(source.asks.lock().unwrap().len(), 1);
    assert!(s.reconcile_note(1).is_none());
}

#[tokio::test]
async fn a_signal_raised_on_the_tick_range_stands_after_the_reconcile() {
    let source = Counting {
        candles: vec![
            candle("09:15", "99", "101.0", "98.0", "99.2"),
            candle("09:20", "99.2", "100.6", "99.0", "100.5"),
        ],
        ..Counting::default()
    };
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let ticks = [
        tick(1, "99.0", "09:15", None),
        tick(1, "99.5", "09:19", Some("98.0")),
        tick(1, "100.5", "09:20", None),
        tick(1, "100.6", "09:21", None),
    ];
    drive(&mut s, &ticks, &mut store, &source).await;
    assert_eq!(states(&store.signals), [TriggerState::Triggered]);
    assert_eq!(store.signals[0].verdict.entry, Some(d("100.5")));
    assert!(source.asks.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_name_with_no_tick_inside_the_window_gets_its_range_from_the_candles() {
    let source = Counting {
        candles: vec![
            candle("09:15", "99", "99.5", "98.0", "99.2"),
            candle("09:20", "99.2", "99.4", "99.0", "99.1"),
        ],
        ..Counting::default()
    };
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let first = [
        tick(1, "99.3", "09:20", None),
        tick(1, "99.2", "09:20", None),
    ];
    drive(&mut s, &first, &mut store, &source).await;
    assert!(s.opening(1).is_none());
    assert!(source.asks.lock().unwrap().is_empty());
    drive(
        &mut s,
        &[tick(1, "99.2", "09:21", None)],
        &mut store,
        &source,
    )
    .await;
    assert_eq!(s.opening(1).unwrap().high, d("99.5"));
    assert!(
        s.reconcile_note(1)
            .unwrap()
            .contains("no tick inside the window")
    );
}

/// Review: Python's `max(state.tick_high, price)` keeps the FIRST of two equal Decimals, so a
/// later `100.00` does not replace an earlier `100.0` — the range (and any note or entry that
/// prints it) keeps the first print's scale. `Ord::max` would keep the second.
#[tokio::test]
async fn equal_prints_keep_the_first_scale_like_python_max() {
    let mut s = strategy(vec![name("ABC", 1, None, None)]);
    let mut store = ListStore::default();
    let candles = Counting::default();
    let ticks = [
        Tick {
            instrument_token: 1,
            last_price: Some(d("100.0")),
            day_low: None,
            at: hm("09:15"),
        },
        Tick {
            instrument_token: 1,
            last_price: Some(d("99.50")),
            day_low: None,
            at: hm("09:16"),
        },
        Tick {
            instrument_token: 1,
            last_price: Some(d("100.00")),
            day_low: None,
            at: hm("09:17"),
        },
        Tick {
            instrument_token: 1,
            last_price: Some(d("99.5")),
            day_low: None,
            at: hm("09:18"),
        },
        Tick {
            instrument_token: 1,
            last_price: Some(d("99.8")),
            day_low: None,
            at: hm("09:20"),
        },
    ];
    drive(&mut s, &ticks, &mut store, &candles).await;
    let opening = s.opening(1).unwrap();
    assert_eq!(opening.high.to_string(), "100.0");
    assert_eq!(opening.low.to_string(), "99.50");
}

/// `NoCandles`: a source with nothing to say.
struct NoCandles;

#[async_trait]
impl CandleSource for NoCandles {
    async fn minute_candles(
        &self,
        _token: i64,
        _day: NaiveDate,
        _until: NaiveDateTime,
    ) -> Result<Vec<Candle>, String> {
        Ok(Vec::new())
    }
}

/// `Exploding`: the historical API is down.
struct Exploding;

#[async_trait]
impl CandleSource for Exploding {
    async fn minute_candles(
        &self,
        _token: i64,
        _day: NaiveDate,
        _until: NaiveDateTime,
    ) -> Result<Vec<Candle>, String> {
        Err("historical API down".to_owned())
    }
}

#[tokio::test]
async fn test_a_failing_candle_source_leaves_the_tick_range_standing() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    let ticks = [
        tick(1, "99.0", "09:15", None),
        tick(1, "99.5", "09:19", None),
        tick(1, "99.3", "09:20", None),
        tick(1, "99.2", "09:21", None),
        tick(1, "99.2", "09:22", None),
    ];
    drive(&mut s, &ticks, &mut store, &Exploding).await;
    assert_eq!(s.opening(1).unwrap().high, d("99.5"));
    assert!(s.state[&1].reconciled);
}

/// SW26: a monitor that comes up at 13:10 measures the break against the MORNING range (from
/// Kite's 09:15 candles), never a range built from the afternoon's own ticks.
#[tokio::test]
#[allow(non_snake_case)]
async fn test_a_monitor_started_in_the_afternoon_still_uses_the_MORNING_range() {
    let source = Counting {
        candles: vec![
            candle("09:15", "99", "99.5", "98.0", "99.2"),
            candle("09:20", "99.2", "99.4", "99.0", "99.1"),
        ],
        ..Counting::default()
    };
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    drive(
        &mut s,
        &[tick(1, "101.0", "13:10", Some("99.8"))],
        &mut store,
        &source,
    )
    .await;
    let opening = s.opening(1).unwrap();
    assert_eq!(
        (opening.high, opening.low),
        (d("99.5"), d("98.0")),
        "the afternoon's own prints became the opening range"
    );
    assert_eq!(states(&store.signals), [TriggerState::Triggered]);
    assert_eq!(store.signals[0].verdict.entry, Some(d("101.0")));
}

/// The fail-safe half: no morning range, no verdict — the name is silent for the day.
#[tokio::test]
async fn test_an_afternoon_start_with_no_candles_refuses_to_trigger_at_all() {
    let mut store = ListStore::default();
    let mut s = strategy(vec![name("AAA", 1, Some("100"), None)]);
    drive(
        &mut s,
        &[
            tick(1, "101.0", "13:10", None),
            tick(1, "120.0", "14:00", None),
        ],
        &mut store,
        &NoCandles,
    )
    .await;
    assert!(s.opening(1).is_none());
    assert!(store.signals.is_empty());
}
