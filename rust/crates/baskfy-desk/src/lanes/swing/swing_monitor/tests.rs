//! The spec of `tests/test_swing_monitor.py` as far as it binds the runner: the flag, the
//! runner and service that cannot place, the three-flag auto-execute truth table and its queue,
//! the quote cap, the loop (chores at their hours, quotes only when quiet, the first tick, the
//! reload, the heartbeat), the late-login wait and the metrics port. The store, the strategy,
//! the notifier, the clock and the service have their own files beside this one
//! (`tests_store.rs`, `tests_strategy.rs`, `tests_notify.rs`, `tests_clock.rs`,
//! `tests_service.rs`); every Python test function of the lane's four files is ported in one
//! of them, named after it.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_core::swing::market::{ExposureTier, MarketGate};
use baskfy_core::swing::opening_range::{TriggerState, TriggerVerdict};
use baskfy_core::swing::setups::{Setup, SwingConfig};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeZone};
use rust_decimal::Decimal;

use super::runner::{
    AutoConfirm, Chores, Confirmed, LoopTimings, MonitorStore, Planned, QuoteFallback, QuoteReader,
    Quotes, TickBus, WatchHooks, build_monitor, drain_auto_execute, monitor_metrics_port,
    py_int_value, run_until_close, session_wait_until, tick_of, wait_for_session,
};
use crate::clock::ist;
use crate::lanes::swing::breakout::{
    CandleSource, Signal, SignalStore, SwingBreakout, WatchedName,
};
use crate::lanes::swing::context::auto_execute_enabled;
use crate::lanes::swing::rules::{Candle, assemble};
use crate::settings::Settings;
use crate::wire::{PyDict, PyValue};
use baskfy_providers::records::PyDateTime;

pub(super) fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 19).unwrap()
}

pub(super) fn at(h: u32, m: u32) -> NaiveDateTime {
    day().and_hms_opt(h, m, 0).unwrap()
}

pub(super) fn hm(t: (i64, i64)) -> NaiveTime {
    NaiveTime::from_hms_opt(u32::try_from(t.0).unwrap(), u32::try_from(t.1).unwrap(), 0).unwrap()
}

/// `PAST_CLOSE`: one minute after `monitor_close` — derived from the config, so a loop test
/// ends when the session does.
pub(super) fn past_close() -> NaiveDateTime {
    day().and_time(hm(SwingConfig::default().opening_range.monitor_close)) + Duration::minutes(1)
}

pub(super) fn settings() -> Settings {
    Settings::defaults().unwrap()
}

/// `_name(symbol, token, setup=FLAG, pivot="100", circuit=None)`.
pub(super) fn named(
    symbol: &str,
    token: i64,
    setup: Setup,
    pivot: Option<&str>,
    circuit: Option<&str>,
) -> WatchedName {
    WatchedName {
        watch_id: token,
        instrument_id: token * 10,
        symbol: symbol.to_owned(),
        token,
        setup,
        pivot_high: pivot.map(|p| p.parse().unwrap()),
        upper_circuit: circuit.map(|c| c.parse().unwrap()),
        focus: false,
    }
}

pub(super) fn name(symbol: &str, token: i64) -> WatchedName {
    named(symbol, token, Setup::Flag, Some("100"), None)
}

/// `_tick(token, price, "HH:MM", low=None)`: the ticker's own shape.
pub(super) fn tick(token: i64, price: f64, h: u32, m: u32, low: Option<f64>) -> PyDict {
    let mut t = PyDict::default();
    t.insert("instrument_token", PyValue::Int(token));
    t.insert("last_price", PyValue::Float(price));
    t.insert(
        "exchange_timestamp",
        PyValue::DateTime(PyDateTime::Naive(at(h, m))),
    );
    let mut ohlc = PyDict::default();
    ohlc.insert("low", PyValue::Float(low.unwrap_or(price)));
    t.insert("ohlc", PyValue::Map(ohlc));
    t
}

/// `_drive(strategy, ticks)`.
pub(super) async fn drive(
    strategy: &mut SwingBreakout,
    store: &mut dyn SignalStore,
    candles: &dyn CandleSource,
    ticks: &[PyDict],
) {
    for t in ticks {
        let read = tick_of(t, at(12, 0)).unwrap();
        strategy.on_tick(&read, store, candles).await;
    }
}

fn signal(symbol: &str) -> Signal {
    Signal {
        watch: name(symbol, 1),
        at: at(9, 31),
        verdict: TriggerVerdict {
            state: TriggerState::Triggered,
            entry: Some(Decimal::from(100)),
            stop: Some(Decimal::from(95)),
            range_high: Some(Decimal::new(995, 1)),
            range_low: Some(Decimal::from(98)),
        },
        last_price: Decimal::new(10000, 2),
        low_of_day: Decimal::new(978, 1),
        window_minutes: 5,
    }
}

fn planned(line_id: i64) -> Planned {
    let tier = ExposureTier {
        level: 3,
        max_open_positions: 6,
        max_exposure_pct: 100.0,
        new_entries_allowed: true,
        drawdown_locked: false,
    };
    Planned {
        line_id: Some(line_id),
        plan: assemble(day(), MarketGate::Green, tier, vec![], vec![], vec![]),
        expires_at: ist().from_local_datetime(&at(10, 1)).single().unwrap(),
        plan_id: Some(format!("plan-{line_id}")),
    }
}

// --- the seams ----------------------------------------------------------------------------

/// `ListStore`.
#[derive(Default)]
pub(super) struct ListStore {
    pub(super) signals: Vec<Signal>,
    pub(super) queue: Vec<(Signal, Planned)>,
}

#[async_trait]
impl SignalStore for ListStore {
    async fn raise_signal(&mut self, signal: &Signal) -> Result<(), String> {
        self.signals.push(signal.clone());
        Ok(())
    }
}

#[async_trait]
impl MonitorStore for ListStore {
    fn take_pending(&mut self) -> Vec<(Signal, Planned)> {
        std::mem::take(&mut self.queue)
    }
}

/// `NoCandles`: the tick fallback's case.
pub(super) struct NoCandles;

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

/// Every confirm attempted; answers `SIMULATED` or fails.
pub(super) struct RecordingConfirm {
    pub(super) seen: Mutex<Vec<(String, i64, Decimal)>>,
    fail: bool,
}

impl RecordingConfirm {
    pub(super) fn new(fail: bool) -> Self {
        Self {
            seen: Mutex::new(Vec::new()),
            fail,
        }
    }
}

#[async_trait]
impl AutoConfirm for RecordingConfirm {
    async fn confirm(
        &self,
        plan_id: &str,
        line_id: i64,
        last_price: Decimal,
    ) -> Result<Confirmed, String> {
        self.seen
            .lock()
            .unwrap()
            .push((plan_id.to_owned(), line_id, last_price));
        if self.fail {
            Err("RuntimeError: no database".to_owned())
        } else {
            Ok(Confirmed {
                status: "SIMULATED".to_owned(),
                reason: String::new(),
            })
        }
    }
}

pub(super) fn with_flags(dry_run: bool, execution: bool, auto: bool) -> Settings {
    let mut s = settings();
    s.dry_run = dry_run;
    s.swing_execution_enabled = execution;
    s.swing_auto_execute = auto;
    s
}

/// `_code_only`: the source with every `//` comment (doc and line) removed — what *runs*,
/// not what explains.
pub(super) fn code_only(source: &str) -> String {
    source
        .lines()
        .map(|l| match l.find("//") {
            Some(i) => &l[..i],
            None => l,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// The text of `fn <name>` in `source` up to its body's opening brace (its signature).
pub(super) fn signature<'a>(source: &'a str, name: &str) -> &'a str {
    let start = source.find(name).unwrap();
    let end = source[start..].find('{').unwrap() + start;
    &source[start..end]
}

// =============================================================================================
// TestTheFlag
// =============================================================================================

#[test]
fn test_with_the_flag_off_the_strategy_is_not_instantiated() {
    let built = build_monitor(
        false,
        vec![name("AAA", 1)],
        day(),
        SwingConfig::default(),
        None,
    );
    assert!(built.unwrap().is_none());
    // `build_monitor` returns before `SwingBreakout::new` is reached.
    let code = code_only(include_str!("runner.rs"));
    let body = &code[code.find("pub fn build_monitor(").unwrap()..];
    let off = body.find("if !enabled").unwrap();
    let built_at = body.find("SwingBreakout::new").unwrap();
    let ret = body[off..].find("return Ok(None);").unwrap() + off;
    assert!(
        ret < built_at,
        "the flag is off and a SwingBreakout was constructed anyway"
    );
}

#[test]
fn test_with_the_flag_on_it_is() {
    let built = build_monitor(
        true,
        vec![name("AAA", 1)],
        day(),
        SwingConfig::default(),
        None,
    )
    .unwrap()
    .unwrap();
    assert_eq!(built.tokens(), vec![1]);
}

#[test]
fn test_the_process_default_is_off() {
    let s = settings();
    assert!(!s.swing_monitor_enabled);
    assert!(!s.swing_auto_execute);
    assert!(!s.swing_execution_enabled);
    assert!(s.dry_run);
    assert!(!auto_execute_enabled(&s));
    // `main` honours the flag before touching a database, a broker or a bus.
    let code = code_only(include_str!("service.rs"));
    let main = &code[code.find("pub async fn main(").unwrap()..];
    let flag = main.find("swing_monitor_enabled").unwrap();
    for later in [
        "sole_user_id(",
        "kite_api_key",
        "wait_for_session(",
        "watch_today(",
    ] {
        assert!(
            flag < main.find(later).unwrap(),
            "{later} runs before the flag"
        );
    }
    let watch = &code[code.find("async fn watch_today(").unwrap()..];
    assert!(
        watch.contains("state.db.connect()"),
        "the database is opened in the watch"
    );
}

// =============================================================================================
// TestItCannotPlace
// =============================================================================================

#[test]
fn test_generate_targets_is_empty() {
    let built = build_monitor(
        true,
        vec![name("AAA", 1)],
        day(),
        SwingConfig::default(),
        None,
    )
    .unwrap()
    .unwrap();
    assert!(built.generate_targets().is_empty());
}

#[test]
fn test_the_strategy_never_names_the_gateway_or_a_placing_verb() {
    let code = code_only(include_str!("../breakout.rs"));
    for forbidden in [
        "self.gw",
        "gw.place",
        ".place(",
        "place_order",
        "place_gtt",
        "kc.",
        "kiteconnect",
        "kite_client",
        "Gateway",
        concat!("Kite", "Writes"),
    ] {
        assert!(!code.contains(forbidden), "breakout.rs names {forbidden}");
    }
}

#[test]
fn test_the_runner_never_names_a_placing_verb_either() {
    let code = code_only(include_str!("runner.rs"));
    for forbidden in [
        "gw.place",
        ".place(",
        "place_order",
        "place_gtt",
        "OrderGateway(",
        "Gateway",
        concat!("Kite", "Writes"),
    ] {
        assert!(!code.contains(forbidden), "runner.rs names {forbidden}");
    }
    // `main` lives in service.rs: Python's scan of `swing_monitor.py` covers it too.
    let service = code_only(include_str!("service.rs"));
    for forbidden in [
        "gw.place",
        ".place(",
        "place_order",
        "place_gtt",
        "OrderGateway(",
    ] {
        assert!(!service.contains(forbidden), "service.rs names {forbidden}");
    }
}

#[test]
fn test_the_monitor_is_built_with_no_gateway_at_all() {
    let runner = code_only(include_str!("runner.rs"));
    let sig = signature(&runner, "pub fn build_monitor(");
    assert!(!sig.to_lowercase().contains("gateway"), "{sig}");
    let breakout = code_only(include_str!("../breakout.rs"));
    let new = signature(&breakout, "pub fn new(");
    assert!(!new.to_lowercase().contains("gateway"), "{new}");
    let service = code_only(include_str!("service.rs"));
    let call = &service[service.find("let strategy = build_monitor(").unwrap()..];
    let call = &call[..call.find(";").unwrap()];
    assert!(!call.to_lowercase().contains("gateway"), "{call}");
}

// =============================================================================================
// TestAutoExecute (SW25)
// =============================================================================================

#[test]
fn test_it_needs_all_three_flags_and_any_one_off_is_off() {
    let mut live = Vec::new();
    for dry_run in [true, false] {
        for execution in [false, true] {
            for auto in [false, true] {
                if auto_execute_enabled(&with_flags(dry_run, execution, auto)) {
                    live.push((dry_run, execution, auto));
                }
            }
        }
    }
    assert_eq!(live, vec![(false, true, true)], "more than one live cell");
}

#[tokio::test]
async fn test_with_the_flag_off_the_queue_is_drained_and_nothing_is_sent() {
    for dry_run in [true, false] {
        for execution in [false, true] {
            for auto in [false, true] {
                if !dry_run && execution && auto {
                    continue;
                }
                let confirm = RecordingConfirm::new(false);
                let mut queue = vec![(signal("AAA"), planned(1))];
                let done =
                    drain_auto_execute(&mut queue, &with_flags(dry_run, execution, auto), &confirm)
                        .await;
                assert!(done.is_empty());
                assert!(queue.is_empty(), "the queue was left to go stale");
                assert!(
                    confirm.seen.lock().unwrap().is_empty(),
                    "auto-execute ran with a flag off"
                );
            }
        }
    }
}

#[tokio::test]
async fn with_all_three_flags_each_line_is_confirmed_with_its_plan_and_tick_price() {
    let confirm = RecordingConfirm::new(false);
    let mut queue = vec![(signal("AAA"), planned(1)), (signal("BBB"), planned(2))];
    let done = drain_auto_execute(&mut queue, &with_flags(false, true, true), &confirm).await;
    assert_eq!(
        done,
        vec![
            ("AAA".to_owned(), "SIMULATED".to_owned()),
            ("BBB".to_owned(), "SIMULATED".to_owned())
        ]
    );
    let seen = confirm.seen.lock().unwrap().clone();
    assert_eq!(
        seen,
        vec![
            ("plan-1".to_owned(), 1, Decimal::new(10000, 2)),
            ("plan-2".to_owned(), 2, Decimal::new(10000, 2))
        ]
    );
}

#[tokio::test]
async fn test_an_execution_that_raises_does_not_stop_the_monitor() {
    let confirm = RecordingConfirm::new(true);
    let mut queue = vec![(signal("AAA"), planned(1)), (signal("BBB"), planned(2))];
    let done = drain_auto_execute(&mut queue, &with_flags(false, true, true), &confirm).await;
    assert_eq!(
        done,
        vec![
            ("AAA".to_owned(), "ERROR".to_owned()),
            ("BBB".to_owned(), "ERROR".to_owned())
        ]
    );
    assert!(queue.is_empty());
}

#[tokio::test]
async fn test_a_skip_is_never_confirmed() {
    let confirm = RecordingConfirm::new(false);
    let mut queue = Vec::new();
    assert!(
        drain_auto_execute(&mut queue, &with_flags(false, true, true), &confirm)
            .await
            .is_empty()
    );
    assert!(confirm.seen.lock().unwrap().is_empty());
}

#[test]
fn test_the_store_holds_the_queue_but_never_the_order_path() {
    let code = code_only(include_str!("runner.rs"));
    let start = code.find("impl PgSignalStore {").unwrap();
    let end = code.find("pub struct Confirmed").unwrap();
    let store = &code[start..end];
    assert!(
        store.contains("pending_confirms"),
        "the store no longer records what to confirm"
    );
    assert!(
        !store.contains("execute_line"),
        "the store reaches the order path again"
    );
    assert!(!store.contains("confirm("), "the store confirms");
    for forbidden in [
        ".place(",
        "place_order",
        "place_gtt",
        "kc.",
        "gateway",
        "order_",
    ] {
        assert!(
            !store.to_lowercase().contains(forbidden),
            "the store names {forbidden}"
        );
    }
}

// =============================================================================================
// TestTheQuoteCap (B10)
// =============================================================================================

/// `FakeKite.quote_raw`, counted; a tick-shaped quote per symbol.
pub(super) struct FakeQuotes {
    pub(super) calls: Mutex<Vec<Vec<String>>>,
    price: f64,
}

#[async_trait]
impl QuoteReader for FakeQuotes {
    async fn quote_raw(&self, keys: &[String]) -> Result<PyValue, String> {
        self.calls.lock().unwrap().push(keys.to_vec());
        let mut out = PyDict::default();
        for (i, k) in keys.iter().enumerate() {
            let mut row = PyDict::default();
            row.insert(
                "instrument_token",
                PyValue::Int(i64::try_from(i).unwrap() + 1),
            );
            row.insert("last_price", PyValue::Float(self.price));
            let mut ohlc = PyDict::default();
            ohlc.insert("low", PyValue::Float(self.price - 1.0));
            row.insert("ohlc", PyValue::Map(ohlc));
            row.insert("timestamp", PyValue::DateTime(PyDateTime::Naive(at(9, 25))));
            out.insert(k.clone(), PyValue::Map(row));
        }
        Ok(PyValue::Map(out))
    }
}

pub(super) fn fake_quotes(price: f64) -> FakeQuotes {
    FakeQuotes {
        calls: Mutex::new(Vec::new()),
        price,
    }
}

#[tokio::test]
async fn test_the_quote_fallback_polls_at_most_once_per_five_seconds() {
    let kite = fake_quotes(100.0);
    let mut fb = QuoteFallback::new(
        BTreeMap::from([(1, "AAA".to_owned()), (2, "BBB".to_owned())]),
        None,
    );
    assert!(
        (fb.min_interval - SwingConfig::default().opening_range.quote_poll_min_seconds).abs()
            < f64::EPSILON
    );
    assert!((fb.min_interval - 5.0).abs() < f64::EPSILON);
    let mut t = 1000.0;
    let mut polled = Vec::new();
    for _ in 0..100 {
        polled.push(fb.poll(&kite, at(9, 25), t).await.unwrap());
    }
    assert_eq!(kite.calls.lock().unwrap().len(), 1);
    assert!(polled[0].is_some() && polled[1].is_none());
    t += 4.99;
    assert!(fb.poll(&kite, at(9, 25), t).await.unwrap().is_none());
    t += 0.01;
    assert!(fb.poll(&kite, at(9, 25), t).await.unwrap().is_some());
    let calls = kite.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0], vec!["NSE:AAA".to_owned(), "NSE:BBB".to_owned()]);
    assert_eq!(fb.calls, 2);
}

#[tokio::test]
async fn test_the_quote_fallback_answers_ticks_the_strategy_can_read() {
    let kite = fake_quotes(101.5);
    let mut fb = QuoteFallback::new(BTreeMap::from([(1, "AAA".to_owned())]), None);
    let ticks = fb.poll(&kite, at(9, 30), 0.0).await.unwrap().unwrap();
    let mut expected = PyDict::default();
    expected.insert("instrument_token", PyValue::Int(1));
    expected.insert("last_price", PyValue::Float(101.5));
    let mut ohlc = PyDict::default();
    ohlc.insert("low", PyValue::Float(100.5));
    expected.insert("ohlc", PyValue::Map(ohlc));
    expected.insert(
        "exchange_timestamp",
        PyValue::DateTime(PyDateTime::Naive(at(9, 25))),
    );
    assert_eq!(ticks, vec![expected]);
    // ... and the strategy reads them as it reads a pushed tick.
    let read = tick_of(&ticks[0], at(12, 0)).unwrap();
    assert_eq!(read.instrument_token, 1);
    assert_eq!(read.last_price, Some(Decimal::new(1015, 1)));
    assert_eq!(read.day_low, Some(Decimal::new(1005, 1)));
    assert_eq!(read.at, at(9, 25));
}

/// Python's `int()` and the stamp passthrough, at the poll and at the strategy's door.
#[tokio::test]
async fn the_fallback_reads_rows_as_python_does() {
    struct Rows(PyValue);
    #[async_trait]
    impl QuoteReader for Rows {
        async fn quote_raw(&self, _keys: &[String]) -> Result<PyValue, String> {
            Ok(self.0.clone())
        }
    }
    let row = |token: PyValue, stamp: PyValue| {
        let mut r = PyDict::default();
        r.insert("instrument_token", token);
        r.insert("last_price", PyValue::Str("101.25".to_owned()));
        r.insert("timestamp", stamp);
        PyValue::Map(r)
    };
    let mut data = PyDict::default();
    // A float token is `int()`ed; a falsy stamp falls through to `now`; a string stamp is
    // passed through for the strategy's `_tick_time` (which parses it).
    data.insert(
        "NSE:AAA",
        row(PyValue::Float(7.9), PyValue::Str(String::new())),
    );
    data.insert(
        "NSE:BBB",
        row(
            PyValue::Int(8),
            PyValue::Str("2026-08-19T09:26:30".to_owned()),
        ),
    );
    let mut fb = QuoteFallback::new(BTreeMap::from([(7, "AAA".to_owned())]), None);
    let ticks = fb
        .poll(&Rows(PyValue::Map(data)), at(9, 30), 0.0)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(ticks[0].get("instrument_token"), Some(&PyValue::Int(7)));
    assert_eq!(
        ticks[0].get("exchange_timestamp"),
        Some(&PyValue::DateTime(PyDateTime::Naive(at(9, 30))))
    );
    assert_eq!(ticks[0].get("ohlc"), Some(&PyValue::Map(PyDict::default())));
    let second = tick_of(&ticks[1], at(12, 0)).unwrap();
    assert_eq!(second.at, day().and_hms_opt(9, 26, 30).unwrap());
    assert_eq!(second.last_price, Some(Decimal::new(10125, 2)));
    // A token `int()` refuses raises out of the poll, as Python's does.
    let mut bad = PyDict::default();
    bad.insert("NSE:AAA", row(PyValue::Str("x".to_owned()), PyValue::None));
    let mut fb = QuoteFallback::new(BTreeMap::from([(7, "AAA".to_owned())]), None);
    let err = fb
        .poll(&Rows(PyValue::Map(bad)), at(9, 30), 0.0)
        .await
        .unwrap_err();
    assert!(
        err.starts_with("ValueError: invalid literal for int()"),
        "{err}"
    );
    assert_eq!(py_int_value(&PyValue::Bool(true)), Ok(1));
    assert_eq!(py_int_value(&PyValue::Str(" 1_000 ".to_owned())), Ok(1000));
}

// =============================================================================================
// The loop's seams
// =============================================================================================

/// A bus whose queues a test fills; per-token queues in subscription order.
#[derive(Default)]
pub(super) struct Bus {
    pub(super) queues: BTreeMap<i64, Vec<PyDict>>,
    pub(super) subscribed: Vec<i64>,
}

impl TickBus for Bus {
    fn subscribe(&mut self, token: i64) {
        self.subscribed.push(token);
        self.queues.entry(token).or_default();
    }
    fn drain(&mut self, token: i64) -> Vec<PyDict> {
        self.queues
            .get_mut(&token)
            .map(std::mem::take)
            .unwrap_or_default()
    }
}

/// Moments walked one pass at a time (each sleep advances), a monotonic clock, recorders.
pub(super) struct Hooks {
    moments: Vec<NaiveDateTime>,
    i: usize,
    mono: f64,
    fired: Vec<(String, NaiveDate)>,
    chore_fails: bool,
    first_ticks: usize,
    first_tick_fails: bool,
}

impl Hooks {
    pub(super) fn walk(moments: Vec<NaiveDateTime>) -> Self {
        Self {
            moments,
            i: 0,
            mono: 1000.0,
            fired: Vec::new(),
            chore_fails: false,
            first_ticks: 0,
            first_tick_fails: false,
        }
    }
}

#[async_trait]
impl WatchHooks for Hooks {
    fn now(&mut self) -> NaiveDateTime {
        self.moments[self.i.min(self.moments.len() - 1)]
    }
    fn monotonic(&mut self) -> f64 {
        self.mono
    }
    async fn sleep(&mut self, seconds: f64) {
        self.mono += seconds;
        self.i += 1;
    }
    async fn run_chore(&mut self, name: &str, day: NaiveDate) -> Result<(), String> {
        self.fired.push((name.to_owned(), day));
        if self.chore_fails {
            Err("RuntimeError: no database".to_owned())
        } else {
            Ok(())
        }
    }
    async fn on_first_tick(&mut self) -> Result<(), String> {
        self.first_ticks += 1;
        if self.first_tick_fails {
            Err("RuntimeError: database gone".to_owned())
        } else {
            Ok(())
        }
    }
}

pub(super) fn strategy_over(watchlist: Vec<WatchedName>) -> SwingBreakout {
    build_monitor(true, watchlist, day(), SwingConfig::default(), None)
        .unwrap()
        .unwrap()
}

fn strategy() -> SwingBreakout {
    strategy_over(vec![name("AAA", 1)])
}

// =============================================================================================
// TestTheChoresFireFromInsideTheWatch (SW26)
// =============================================================================================

#[tokio::test]
async fn test_each_chore_fires_once_at_its_own_hour_while_the_watch_continues() {
    let window = SwingConfig::default().opening_range;
    let at_cutoff = day().and_time(hm(window.pending_cutoff_at));
    let before = at_cutoff - Duration::minutes(1);
    let at_sweep = day().and_time(hm(window.gtt_sweep_at));
    let mut hooks = Hooks::walk(vec![
        before,
        at_cutoff,
        at(13, 0),
        at_sweep,
        at_sweep,
        past_close(),
    ]);
    let mut strat = strategy();
    let raised = run_until_close(
        &mut strat,
        &mut ListStore::default(),
        &NoCandles,
        &mut Bus::default(),
        &mut hooks,
        None,
        &settings(),
        &RecordingConfirm::new(false),
        LoopTimings::default(),
    )
    .await
    .unwrap();
    assert_eq!(raised, 0);
    assert_eq!(
        hooks.fired,
        vec![
            ("cutoff".to_owned(), day()),
            ("gtt_sweep".to_owned(), day())
        ]
    );
}

#[tokio::test]
async fn test_a_chore_that_raises_does_not_stop_the_watch() {
    let cutoff = day().and_time(hm(SwingConfig::default().opening_range.pending_cutoff_at));
    let mut hooks = Hooks::walk(vec![cutoff, cutoff, past_close()]);
    hooks.chore_fails = true;
    let mut strat = strategy();
    let raised = run_until_close(
        &mut strat,
        &mut ListStore::default(),
        &NoCandles,
        &mut Bus::default(),
        &mut hooks,
        None,
        &settings(),
        &RecordingConfirm::new(false),
        LoopTimings::default(),
    )
    .await
    .unwrap();
    assert_eq!(raised, 0, "the loop reached its close, it did not blow up");
    assert_eq!(
        hooks.fired.len(),
        1,
        "the cutoff is fired once, even when it fails"
    );
}

#[test]
fn chores_each_fire_once_and_a_late_start_runs_the_cutoff_before_the_sweep() {
    let mut chores = Chores::default();
    assert_eq!(chores.due(at(15, 20)), vec!["cutoff", "gtt_sweep"]);
    assert!(chores.due(at(15, 21)).is_empty());
}

// =============================================================================================
// TestTheQuoteCap: the loop
// =============================================================================================

/// The fake clock and bus of Python's loop tests: each sleep advances the monotonic clock and
/// the moment by the seconds slept; ticks are scheduled by elapsed second onto a token's queue
/// once it is subscribed; the moment jumps past the close after `end_after` seconds.
pub(super) struct Timed {
    pub(super) shared: Arc<Mutex<BTreeMap<i64, Vec<PyDict>>>>,
    pub(super) bus_subscribed: Arc<Mutex<Vec<i64>>>,
    mono: f64,
    moment: NaiveDateTime,
    end_after: f64,
    /// `(from, until, token, price)`: a tick per pass while `from <= elapsed < until`.
    flowing: Vec<(f64, f64, i64, f64)>,
    /// `(at, token, price)`: one tick when `at <= elapsed < at + secs`.
    once: Vec<(f64, i64, f64)>,
    lists: Vec<Vec<WatchedName>>,
    reload_fails: bool,
    reloads: usize,
    pub(super) subscribed: Vec<Vec<i64>>,
    pub(super) unsubscribed: Vec<Vec<i64>>,
    pub(super) beats: Vec<String>,
    with_reload: bool,
}

impl Timed {
    pub(super) fn new(start: NaiveDateTime, end_after: f64) -> Self {
        Self {
            shared: Arc::new(Mutex::new(BTreeMap::new())),
            bus_subscribed: Arc::new(Mutex::new(Vec::new())),
            mono: 1000.0,
            moment: start,
            end_after,
            flowing: Vec::new(),
            once: Vec::new(),
            lists: Vec::new(),
            reload_fails: false,
            reloads: 0,
            subscribed: Vec::new(),
            unsubscribed: Vec::new(),
            beats: Vec::new(),
            with_reload: false,
        }
    }

    pub(super) fn bus(&self) -> TimedBus {
        TimedBus {
            shared: self.shared.clone(),
            subscribed: self.bus_subscribed.clone(),
        }
    }
}

/// `_ReloadBus`: one queue per token, created on first subscription.
pub(super) struct TimedBus {
    shared: Arc<Mutex<BTreeMap<i64, Vec<PyDict>>>>,
    subscribed: Arc<Mutex<Vec<i64>>>,
}

impl TickBus for TimedBus {
    fn subscribe(&mut self, token: i64) {
        self.subscribed.lock().unwrap().push(token);
        self.shared.lock().unwrap().entry(token).or_default();
    }
    fn drain(&mut self, token: i64) -> Vec<PyDict> {
        self.shared
            .lock()
            .unwrap()
            .get_mut(&token)
            .map(std::mem::take)
            .unwrap_or_default()
    }
}

#[async_trait]
impl WatchHooks for Timed {
    fn now(&mut self) -> NaiveDateTime {
        self.moment
    }
    fn monotonic(&mut self) -> f64 {
        self.mono
    }
    async fn sleep(&mut self, secs: f64) {
        self.mono += secs;
        #[allow(clippy::cast_possible_truncation)]
        let ms = (secs * 1000.0).round() as i64;
        self.moment += Duration::milliseconds(ms);
        let elapsed = self.mono - 1000.0;
        let mut queues = self.shared.lock().unwrap();
        for (from, until, token, price) in &self.flowing {
            if *from <= elapsed
                && elapsed < *until
                && let Some(q) = queues.get_mut(token)
            {
                q.push(tick(*token, *price, 9, 25, None));
            }
        }
        for (when, token, price) in &self.once {
            if *when <= elapsed
                && elapsed < when + secs
                && let Some(q) = queues.get_mut(token)
            {
                q.push(tick(*token, *price, 10, 30, None));
            }
        }
        if elapsed >= self.end_after {
            self.moment = past_close();
        }
    }
    async fn run_chore(&mut self, _name: &str, _day: NaiveDate) -> Result<(), String> {
        Ok(())
    }
    fn has_reload(&self) -> bool {
        self.with_reload
    }
    async fn reload(&mut self) -> Result<Vec<WatchedName>, String> {
        if self.reload_fails {
            return Err("RuntimeError: database away".to_owned());
        }
        self.reloads += 1;
        Ok(self.lists[self.reloads.min(self.lists.len() - 1)].clone())
    }
    fn subscribe(&mut self, tokens: &[i64]) -> Result<(), String> {
        self.subscribed.push(tokens.to_vec());
        Ok(())
    }
    fn unsubscribe(&mut self, tokens: &[i64]) -> Result<(), String> {
        self.unsubscribed.push(tokens.to_vec());
        Ok(())
    }
    fn has_heartbeat(&self) -> bool {
        true
    }
    async fn heartbeat(
        &mut self,
        _store: &mut dyn MonitorStore,
        detail: &str,
    ) -> Result<(), String> {
        self.beats.push(detail.to_owned());
        Ok(())
    }
}

#[tokio::test]
async fn test_the_loop_polls_quotes_only_when_the_ticker_is_quiet_and_never_faster_than_the_cap() {
    // Thirty seconds of a silent bus after ten of flowing ticks: six quote calls at most (one
    // per five seconds), none while ticks are flowing.
    let mut hooks = Timed::new(at(9, 25), 40.0);
    hooks.flowing.push((0.0, 10.0, 1, 99.0));
    let mut bus = hooks.bus();
    let kite = fake_quotes(100.0);
    let quotes = Quotes {
        fallback: QuoteFallback::new(BTreeMap::from([(1, "AAA".to_owned())]), None),
        reader: &kite,
    };
    let mut strat = strategy();
    run_until_close(
        &mut strat,
        &mut ListStore::default(),
        &NoCandles,
        &mut bus,
        &mut hooks,
        Some(quotes),
        &settings(),
        &RecordingConfirm::new(false),
        LoopTimings {
            heartbeat_every_seconds: 1e9,
            ..LoopTimings::default()
        },
    )
    .await
    .unwrap();
    let calls = kite.calls.lock().unwrap().len();
    assert!((1..=6).contains(&calls), "{calls} quote calls");
}

// =============================================================================================
// TestMonitorRanIsWrittenAtStart
// =============================================================================================

#[tokio::test]
async fn test_the_loop_fires_on_first_tick_exactly_once_and_survives_its_failure() {
    let mut bus = Bus::default();
    let mut hooks = Hooks::walk(vec![at(9, 25), past_close()]);
    hooks.first_tick_fails = true;
    let mut strat = strategy();
    bus.queues.insert(
        1,
        vec![
            tick(1, 99.0, 9, 25, None),
            tick(1, 99.0, 9, 25, None),
            tick(1, 99.0, 9, 25, None),
        ],
    );
    let raised = run_until_close(
        &mut strat,
        &mut ListStore::default(),
        &NoCandles,
        &mut bus,
        &mut hooks,
        None,
        &settings(),
        &RecordingConfirm::new(false),
        LoopTimings::default(),
    )
    .await
    .unwrap();
    assert_eq!(raised, 0);
    assert_eq!(hooks.first_ticks, 1);
    assert_eq!(strat.verdict_seconds.len(), 3);
}

#[test]
fn test_main_records_monitor_ran_on_the_first_tick_and_hands_off_to_the_clock() {
    let code = code_only(include_str!("service.rs"));
    // The first tick's hook writes the row...
    let first = &code[code.find("async fn on_first_tick(").unwrap()..];
    let first = &first[..first.find("fn has_reload").unwrap()];
    assert!(first.contains("record_monitor_ran("), "{first}");
    // ... an empty watchlist writes it at once, before the loop...
    let watch = &code[code.find("async fn watch_today(").unwrap()..];
    let empty = watch.find("nothing to watch today").unwrap();
    let record = watch[empty..].find("record_monitor_ran(").unwrap() + empty;
    let loop_at = watch.find("run_until_close(").unwrap();
    assert!(empty < record && record < loop_at);
    // ... and after the loop the process becomes the clock.
    let main = &code[code.find("pub async fn main(").unwrap()..];
    assert!(main.find("watch_today(").unwrap() < main.find("run_after_close(").unwrap());
}

#[tokio::test]
async fn the_loop_drains_the_auto_execute_queue_every_pass_and_sends_nothing_by_default() {
    let mut hooks = Hooks::walk(vec![at(9, 25), past_close()]);
    let mut store = ListStore::default();
    store.queue.push((signal("AAA"), planned(1)));
    let confirm = RecordingConfirm::new(false);
    run_until_close(
        &mut strategy(),
        &mut store,
        &NoCandles,
        &mut Bus::default(),
        &mut hooks,
        None,
        &settings(),
        &confirm,
        LoopTimings::default(),
    )
    .await
    .unwrap();
    assert!(store.queue.is_empty());
    assert!(confirm.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_tick_python_cannot_read_ends_the_loop_as_it_does_in_python() {
    let mut bus = Bus::default();
    let mut bad = tick(1, 99.0, 9, 25, None);
    bad.insert("instrument_token", PyValue::Str("not a token".to_owned()));
    bus.queues.insert(1, vec![bad]);
    let mut hooks = Hooks::walk(vec![at(9, 25), past_close()]);
    let err = run_until_close(
        &mut strategy(),
        &mut ListStore::default(),
        &NoCandles,
        &mut bus,
        &mut hooks,
        None,
        &settings(),
        &RecordingConfirm::new(false),
        LoopTimings::default(),
    )
    .await
    .unwrap_err();
    assert!(err.starts_with("ValueError"), "{err}");
}

// =============================================================================================
// TestTheWatchlistReloads (LV4)
// =============================================================================================

async fn reload_run(
    lists: Vec<Vec<WatchedName>>,
    once: Vec<(f64, i64, f64)>,
) -> (SwingBreakout, Timed) {
    let mut hooks = Timed::new(at(10, 0), 200.0);
    hooks.lists = lists.clone();
    hooks.with_reload = true;
    hooks.once = once;
    let mut bus = hooks.bus();
    let mut strat = strategy_over(lists[0].clone());
    run_until_close(
        &mut strat,
        &mut ListStore::default(),
        &NoCandles,
        &mut bus,
        &mut hooks,
        None,
        &settings(),
        &RecordingConfirm::new(false),
        LoopTimings::default(),
    )
    .await
    .unwrap();
    (strat, hooks)
}

#[tokio::test]
async fn test_reload_adds_a_setup_at_eleven_and_the_running_loop_watches_it_exactly_once() {
    let aaa = named("AAA", 1, Setup::Flag, Some("100"), None);
    let bbb = named("BBB", 2, Setup::Flag, Some("200"), None);
    let (strat, hooks) = reload_run(
        vec![vec![aaa.clone()], vec![aaa, bbb]],
        vec![(30.0, 2, 250.0), (90.0, 2, 250.0)],
    )
    .await;
    let bus_subscribed = hooks.bus_subscribed.lock().unwrap().clone();
    assert!(strat.watchlist.contains_key(&2) && bus_subscribed.contains(&2));
    assert_eq!(hooks.subscribed, vec![vec![2]]);
    assert!(hooks.unsubscribed.is_empty());
    assert_eq!(
        bus_subscribed.iter().filter(|t| **t == 2).count(),
        1,
        "one subscription, never a second"
    );
    // The tick at 90 s was handled by the running loop: BBB broke (no range → none raised,
    // but the name was evaluated — its state is the strategy's now).
    assert_eq!(strat.tokens(), vec![1, 2]);
}

#[tokio::test]
async fn test_reload_drops_a_name_the_list_lost_and_unsubscribes_it() {
    let aaa = named("AAA", 1, Setup::Flag, Some("100"), None);
    let bbb = named("BBB", 2, Setup::Flag, Some("200"), None);
    let (strat, hooks) = reload_run(vec![vec![aaa, bbb.clone()], vec![bbb]], vec![]).await;
    assert!(!strat.watchlist.contains_key(&1) && strat.watchlist.contains_key(&2));
    assert_eq!(hooks.unsubscribed, vec![vec![1]]);
    assert!(hooks.subscribed.is_empty());
}

#[tokio::test]
async fn test_reload_that_raises_keeps_the_list_and_the_heartbeat_keeps_beating() {
    let mut hooks = Timed::new(at(10, 0), 130.0);
    hooks.lists = vec![vec![name("AAA", 1)]];
    hooks.with_reload = true;
    hooks.reload_fails = true;
    let mut bus = hooks.bus();
    let mut strat = strategy();
    run_until_close(
        &mut strat,
        &mut ListStore::default(),
        &NoCandles,
        &mut bus,
        &mut hooks,
        None,
        &settings(),
        &RecordingConfirm::new(false),
        LoopTimings::default(),
    )
    .await
    .unwrap();
    assert_eq!(strat.tokens(), vec![1]);
    assert!(!hooks.beats.is_empty());
    assert!(hooks.beats.iter().all(|b| b.contains("watching 1 name(s)")));
    assert!((6..=10).contains(&hooks.beats.len()), "{:?}", hooks.beats);
}

#[tokio::test]
async fn test_an_empty_start_still_enters_the_loop_and_watches_the_first_setup_added() {
    let bbb = named("BBB", 2, Setup::Flag, Some("200"), None);
    let (strat, hooks) = reload_run(vec![vec![], vec![bbb]], vec![(90.0, 2, 250.0)]).await;
    assert_eq!(strat.tokens(), vec![2]);
    assert_eq!(hooks.subscribed, vec![vec![2]]);
}

// =============================================================================================
// TestLateLogin (LV4)
// =============================================================================================

pub(super) fn ist_at(h: u32, m: u32, s: u32) -> DateTime<FixedOffset> {
    ist()
        .from_local_datetime(&day().and_hms_opt(h, m, s).unwrap())
        .single()
        .unwrap()
}

#[tokio::test]
async fn test_late_login_the_monitor_waits_for_a_session_and_starts_when_it_comes() {
    let answers = Mutex::new(vec![Ok(false), Ok(false), Ok(true)].into_iter());
    let moment = Mutex::new(ist_at(9, 14, 0));
    let slept = Mutex::new(Vec::new());
    let ok = wait_for_session(
        || {
            let v = answers.lock().unwrap().next().unwrap();
            async move { v }
        },
        || *moment.lock().unwrap(),
        |secs| {
            slept.lock().unwrap().push(secs);
            let next = *moment.lock().unwrap() + Duration::seconds(30);
            *moment.lock().unwrap() = next;
            async {}
        },
        session_wait_until(),
        30.0,
    )
    .await;
    assert!(ok);
    assert_eq!(*slept.lock().unwrap(), vec![30.0, 30.0]);
}

#[tokio::test]
async fn test_late_login_gives_up_at_fifteen_twenty_and_says_so() {
    let moment = Mutex::new(ist_at(15, 19, 30));
    let ok = wait_for_session(
        || async { Ok(false) },
        || *moment.lock().unwrap(),
        |_| {
            let next = *moment.lock().unwrap() + Duration::seconds(30);
            *moment.lock().unwrap() = next;
            async {}
        },
        session_wait_until(),
        30.0,
    )
    .await;
    assert!(!ok);
    assert_eq!(
        session_wait_until(),
        NaiveTime::from_hms_opt(15, 20, 0).unwrap()
    );
}

#[tokio::test]
async fn test_a_session_check_that_raises_is_not_yet_not_a_crash() {
    let answers = Mutex::new(vec![Err("kite down".to_owned()), Ok(true)].into_iter());
    let ok = wait_for_session(
        || {
            let v = answers.lock().unwrap().next().unwrap();
            async move { v }
        },
        || ist_at(9, 0, 0),
        |_| async {},
        session_wait_until(),
        30.0,
    )
    .await;
    assert!(ok);
}

#[test]
fn the_metrics_port_is_its_own_or_the_desks_plus_one_and_a_bad_one_is_fatal() {
    assert_eq!(monitor_metrics_port(Some(" 9200 "), "9100"), Ok(Some(9200)));
    assert_eq!(monitor_metrics_port(Some(""), "9100"), Ok(Some(9101)));
    assert_eq!(monitor_metrics_port(None, ""), Ok(None));
    let err = monitor_metrics_port(Some("nine"), "").unwrap_err();
    assert_eq!(
        err,
        "ValueError: invalid literal for int() with base 10: 'nine'"
    );
    assert!(monitor_metrics_port(None, "x").is_err());
}
