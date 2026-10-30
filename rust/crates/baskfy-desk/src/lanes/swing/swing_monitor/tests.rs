//! The spec of `tests/test_swing_monitor.py`, `test_swing_clock.py` and `test_swing_notify.py`
//! as far as it binds this lane's Rust: the flag, the store and runner that cannot place, the
//! three-flag auto-execute truth table and its queue, the quote cap, the chores at their own
//! hours, the late-login wait, the notifier's fail-soft fan-out, and the backstop clock.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_core::swing::market::{ExposureTier, MarketGate};
use baskfy_core::swing::opening_range::{TriggerState, TriggerVerdict};
use baskfy_core::swing::setups::{Setup, SwingConfig};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeZone};
use rust_decimal::Decimal;

use super::clock::{
    ChoreSeams, NoCounter, PriceOf, SweepCounter, run_after_close, run_chore, sweep_time,
};
use super::notify::{Notifier, Sender, SignalNotice, build_senders, notice_json};
use super::runner::{
    AutoConfirm, Chores, LoopTimings, MonitorStore, Planned, QuoteFallback, QuoteReader, Quotes,
    TickBus, WatchHooks, build_monitor, drain_auto_execute, monitor_metrics_port, run_until_close,
    session_wait_until, wait_for_session,
};
use crate::clock::ist;
use crate::gateway::Gateway;
use crate::lanes::swing::breakout::{CandleSource, Signal, SignalStore, Tick, WatchedName};
use crate::lanes::swing::context::auto_execute_enabled;
use crate::lanes::swing::execute::OrderSource;
use crate::lanes::swing::rules::{Candle, assemble};
use crate::lanes::swing::store::SwingStore;
use crate::settings::Settings;
use crate::wire::{PyDict, PyValue};

fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 19).unwrap()
}

fn at(h: u32, m: u32) -> NaiveDateTime {
    day().and_hms_opt(h, m, 0).unwrap()
}

fn hm(t: (i64, i64)) -> NaiveTime {
    NaiveTime::from_hms_opt(u32::try_from(t.0).unwrap(), u32::try_from(t.1).unwrap(), 0).unwrap()
}

/// One minute after `monitor_close` — derived from the config, so a loop test ends.
fn past_close() -> NaiveDateTime {
    day().and_time(hm(SwingConfig::default().opening_range.monitor_close)) + Duration::minutes(1)
}

fn settings() -> Settings {
    Settings::defaults().unwrap()
}

fn name(symbol: &str, token: i64) -> WatchedName {
    WatchedName {
        watch_id: token,
        instrument_id: token * 10,
        symbol: symbol.to_owned(),
        token,
        setup: Setup::Flag,
        pivot_high: Some(Decimal::from(100)),
        upper_circuit: None,
        focus: false,
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

#[derive(Default)]
struct ListStore {
    signals: Vec<Signal>,
    queue: Vec<(Signal, Planned)>,
}

#[async_trait]
impl SignalStore for ListStore {
    async fn raise_signal(&mut self, signal: &Signal) -> Result<(), String> {
        self.signals.push(signal.clone());
        Ok(())
    }
}

impl MonitorStore for ListStore {
    fn take_pending(&mut self) -> Vec<(Signal, Planned)> {
        std::mem::take(&mut self.queue)
    }
}

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

/// Every confirm attempted; answers `status` or fails.
struct RecordingConfirm {
    seen: Mutex<Vec<(String, i64, Decimal)>>,
    fail: bool,
}

impl RecordingConfirm {
    fn new(fail: bool) -> Self {
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
    ) -> Result<String, String> {
        self.seen
            .lock()
            .unwrap()
            .push((plan_id.to_owned(), line_id, last_price));
        if self.fail {
            Err("RuntimeError: no database".to_owned())
        } else {
            Ok("SIMULATED".to_owned())
        }
    }
}

fn with_flags(dry_run: bool, execution: bool, auto: bool) -> Settings {
    let mut s = settings();
    s.dry_run = dry_run;
    s.swing_execution_enabled = execution;
    s.swing_auto_execute = auto;
    s
}

/// Strip `//` comments (doc and line) — what *runs*, not what explains.
fn code_only(source: &str) -> String {
    source
        .lines()
        .map(|l| match l.find("//") {
            Some(i) => &l[..i],
            None => l,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

// --- the flag -----------------------------------------------------------------------------

#[test]
fn with_the_flag_off_the_strategy_is_not_instantiated() {
    let built = build_monitor(
        false,
        vec![name("AAA", 1)],
        day(),
        SwingConfig::default(),
        None,
    );
    assert!(built.unwrap().is_none());
}

#[test]
fn with_the_flag_on_it_is() {
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
fn the_process_default_is_off_and_every_auto_flag_defaults_false() {
    let s = settings();
    assert!(!s.swing_monitor_enabled);
    assert!(!s.swing_auto_execute);
    assert!(!s.swing_execution_enabled);
    assert!(s.dry_run);
    assert!(!auto_execute_enabled(&s));
}

#[test]
fn the_window_must_be_one_the_contract_names() {
    assert!(
        build_monitor(
            true,
            vec![name("AAA", 1)],
            day(),
            SwingConfig::default(),
            Some(7)
        )
        .is_err()
    );
}

// --- it cannot place ----------------------------------------------------------------------

#[test]
fn generate_targets_is_empty() {
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
fn the_runner_never_names_a_placing_verb_either() {
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
}

#[test]
fn the_store_holds_the_queue_but_never_the_order_path() {
    let code = code_only(include_str!("runner.rs"));
    let start = code.find("impl PgSignalStore {").unwrap();
    let end = code[start..]
        .find("/// What confirms")
        .map_or(code.len(), |e| start + e);
    let store = &code[start..end.min(code.len())];
    let store_impl_end = code.find("pub trait AutoConfirm").unwrap();
    let both = &code[start..store_impl_end];
    assert!(
        both.contains("pending_confirms"),
        "the store no longer records what to confirm"
    );
    assert!(
        !both.contains("execute_line"),
        "the store reaches the order path again"
    );
    assert!(!both.contains("confirm("), "the store confirms");
    assert!(!store.is_empty());
}

#[test]
fn the_clock_names_no_placing_verb() {
    let code = code_only(include_str!("clock.rs"));
    for forbidden in [
        "place(",
        "place_order",
        "place_gtt",
        ".place(",
        "delete_gtt",
        "OrderGateway",
        "kiteconnect",
    ] {
        assert!(!code.contains(forbidden), "clock.rs names {forbidden}");
    }
}

#[test]
fn the_sweep_time_is_the_config_field_not_a_literal() {
    let expected = ist()
        .from_local_datetime(&day().and_time(hm(SwingConfig::default().opening_range.gtt_sweep_at)))
        .single()
        .unwrap();
    assert_eq!(sweep_time(day()), expected);
    assert_eq!(expected.time(), NaiveTime::from_hms_opt(15, 15, 0).unwrap());
    let code = code_only(include_str!("clock.rs"));
    assert!(
        !code.contains("15, 15"),
        "the sweep time is typed into the clock"
    );
}

// --- SW25: auto-execute -------------------------------------------------------------------

#[test]
fn it_needs_all_three_flags_and_any_one_off_is_off() {
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
async fn with_the_flag_off_the_queue_is_drained_and_nothing_is_sent() {
    for (dry_run, execution, auto) in [
        (false, true, false),
        (true, true, true),
        (false, false, true),
        (true, false, false),
    ] {
        let confirm = RecordingConfirm::new(false);
        let mut queue = vec![(signal("AAA"), planned(1))];
        let done =
            drain_auto_execute(&mut queue, &with_flags(dry_run, execution, auto), &confirm).await;
        assert!(done.is_empty());
        assert!(queue.is_empty(), "the queue was left to go stale");
        assert!(
            confirm.seen.lock().unwrap().is_empty(),
            "auto-execute ran with a flag off"
        );
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
async fn an_execution_that_raises_does_not_stop_the_monitor() {
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
async fn a_skip_is_never_confirmed() {
    let confirm = RecordingConfirm::new(false);
    let mut queue = Vec::new();
    assert!(
        drain_auto_execute(&mut queue, &with_flags(false, true, true), &confirm)
            .await
            .is_empty()
    );
}

// --- B10: the quote cap -------------------------------------------------------------------

struct FakeQuotes {
    calls: Mutex<Vec<Vec<String>>>,
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
            row.insert("timestamp", PyValue::Str("2026-08-19 09:25:00".to_owned()));
            out.insert(k.clone(), PyValue::Map(row));
        }
        Ok(PyValue::Map(out))
    }
}

fn fake_quotes(price: f64) -> FakeQuotes {
    FakeQuotes {
        calls: Mutex::new(Vec::new()),
        price,
    }
}

#[tokio::test]
async fn the_quote_fallback_polls_at_most_once_per_five_seconds() {
    let kite = fake_quotes(100.0);
    let mut fb = QuoteFallback::new(
        BTreeMap::from([(1, "AAA".to_owned()), (2, "BBB".to_owned())]),
        None,
    );
    assert!((fb.min_interval - 5.0).abs() < f64::EPSILON);
    let mut t = 1000.0;
    let mut polled = Vec::new();
    for _ in 0..100 {
        polled.push(fb.poll(&kite, at(9, 25), t).await);
    }
    assert_eq!(kite.calls.lock().unwrap().len(), 1);
    assert!(polled[0].is_some() && polled[1].is_none());
    t += 4.99;
    assert!(fb.poll(&kite, at(9, 25), t).await.is_none());
    t += 0.01;
    assert!(fb.poll(&kite, at(9, 25), t).await.is_some());
    let calls = kite.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0], vec!["NSE:AAA".to_owned(), "NSE:BBB".to_owned()]);
    assert_eq!(fb.calls, 2);
}

#[tokio::test]
async fn the_quote_fallback_answers_ticks_the_strategy_can_read() {
    let kite = fake_quotes(101.5);
    let mut fb = QuoteFallback::new(BTreeMap::from([(1, "AAA".to_owned())]), None);
    let ticks = fb.poll(&kite, at(9, 30), 0.0).await.unwrap();
    assert_eq!(
        ticks,
        vec![Tick {
            instrument_token: 1,
            last_price: Some(Decimal::new(1015, 1)),
            day_low: Some(Decimal::new(1005, 1)),
            at: at(9, 25),
        }]
    );
}

// --- the loop: chores, quotes, first tick, reload -------------------------------------------

/// A bus whose queues a test fills; per-token queues in subscription order.
#[derive(Default)]
struct Bus {
    queues: BTreeMap<i64, Vec<Tick>>,
    subscribed: Vec<i64>,
}

impl TickBus for Bus {
    fn subscribe(&mut self, token: i64) {
        self.subscribed.push(token);
        self.queues.entry(token).or_default();
    }
    fn drain(&mut self, token: i64) -> Vec<Tick> {
        self.queues
            .get_mut(&token)
            .map(std::mem::take)
            .unwrap_or_default()
    }
}

/// Moments walked one pass at a time (each sleep advances), a monotonic clock, recorders.
struct Hooks {
    moments: Vec<NaiveDateTime>,
    i: usize,
    mono: f64,
    fired: Vec<(String, NaiveDate)>,
    chore_fails: bool,
    first_ticks: usize,
    first_tick_fails: bool,
    reload_with: Option<Result<Vec<WatchedName>, String>>,
    subscribed: Vec<Vec<i64>>,
    unsubscribed: Vec<Vec<i64>>,
    beats: Vec<String>,
    sleeps: usize,
    /// Advance the moment by the sleep instead of walking `moments` (the quote test).
    advance: bool,
}

impl Hooks {
    fn walk(moments: Vec<NaiveDateTime>) -> Self {
        Self {
            moments,
            i: 0,
            mono: 1000.0,
            fired: Vec::new(),
            chore_fails: false,
            first_ticks: 0,
            first_tick_fails: false,
            reload_with: None,
            subscribed: Vec::new(),
            unsubscribed: Vec::new(),
            beats: Vec::new(),
            sleeps: 0,
            advance: false,
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
        self.sleeps += 1;
        self.mono += seconds;
        if self.advance {
            #[allow(clippy::cast_possible_truncation)]
            let ms = (seconds * 1000.0) as i64;
            let next = self.moments[0] + Duration::milliseconds(ms);
            self.moments[0] = if self.mono - 1000.0 >= 40.0 {
                past_close()
            } else {
                next
            };
        } else {
            self.i += 1;
        }
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
            Err("no database".to_owned())
        } else {
            Ok(())
        }
    }
    fn has_reload(&self) -> bool {
        self.reload_with.is_some()
    }
    async fn reload(&mut self) -> Result<Vec<WatchedName>, String> {
        self.reload_with
            .clone()
            .unwrap_or_else(|| Err("none".to_owned()))
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
    async fn heartbeat(&mut self, detail: &str) -> Result<(), String> {
        self.beats.push(detail.to_owned());
        Ok(())
    }
}

fn strategy() -> crate::lanes::swing::breakout::SwingBreakout {
    build_monitor(
        true,
        vec![name("AAA", 1)],
        day(),
        SwingConfig::default(),
        None,
    )
    .unwrap()
    .unwrap()
}

#[tokio::test]
async fn each_chore_fires_once_at_its_own_hour_while_the_watch_continues() {
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
    let mut store = ListStore::default();
    let confirm = RecordingConfirm::new(false);
    let raised = run_until_close(
        &mut strat,
        &mut store,
        &NoCandles,
        &mut Bus::default(),
        &mut hooks,
        None,
        &settings(),
        &confirm,
        LoopTimings::default(),
    )
    .await;
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
async fn a_chore_that_raises_does_not_stop_the_watch() {
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
    .await;
    assert_eq!(raised, 0);
    assert_eq!(
        hooks.fired.len(),
        1,
        "the cutoff is fired once, even when it fails"
    );
}

#[test]
fn chores_each_fire_once_and_the_cutoff_precedes_the_sweep() {
    let window = SwingConfig::default().opening_range;
    let cutoff = hm(window.pending_cutoff_at);
    let sweep = hm(window.gtt_sweep_at);
    let close = hm(window.monitor_close);
    assert!(cutoff < sweep, "the cutoff would run after the GTT sweep");
    assert!(
        sweep <= close,
        "the sweep would fall outside the watch that runs it"
    );
    assert_eq!(window.session_open, (9, 15));
    assert_eq!(window.monitor_close, (15, 30));
    assert_eq!(window.pending_cutoff_at, (10, 45));
    // A watch started after 15:15 runs both at once, cutoff first.
    let mut chores = Chores::default();
    assert_eq!(chores.due(at(15, 20)), vec!["cutoff", "gtt_sweep"]);
    assert!(chores.due(at(15, 21)).is_empty());
}

#[tokio::test]
async fn the_loop_polls_quotes_only_when_the_ticker_is_quiet_and_never_faster_than_the_cap() {
    // Ticks flow for the first ten seconds (queued at each pass), then thirty quiet seconds.
    struct FlowingBus {
        inner: Bus,
        hooks_mono: Arc<Mutex<f64>>,
    }
    impl TickBus for FlowingBus {
        fn subscribe(&mut self, token: i64) {
            self.inner.subscribe(token);
        }
        fn drain(&mut self, token: i64) -> Vec<Tick> {
            let t = *self.hooks_mono.lock().unwrap();
            if t - 1000.0 < 10.0 {
                return vec![Tick {
                    instrument_token: token,
                    last_price: Some(Decimal::from(99)),
                    day_low: Some(Decimal::from(99)),
                    at: at(9, 25),
                }];
            }
            Vec::new()
        }
    }
    struct Clocked {
        hooks: Hooks,
        mono: Arc<Mutex<f64>>,
        flowed: bool,
    }
    #[async_trait]
    impl WatchHooks for Clocked {
        fn now(&mut self) -> NaiveDateTime {
            self.hooks.now()
        }
        fn monotonic(&mut self) -> f64 {
            self.hooks.mono
        }
        async fn sleep(&mut self, seconds: f64) {
            self.hooks.sleep(seconds).await;
            *self.mono.lock().unwrap() = self.hooks.mono;
        }
        async fn run_chore(&mut self, _name: &str, _day: NaiveDate) -> Result<(), String> {
            Ok(())
        }
        async fn on_first_tick(&mut self) -> Result<(), String> {
            self.flowed = true;
            Ok(())
        }
    }
    let mono = Arc::new(Mutex::new(1000.0));
    let mut bus = FlowingBus {
        inner: Bus::default(),
        hooks_mono: mono.clone(),
    };
    let mut hooks = Clocked {
        hooks: Hooks::walk(vec![at(9, 25)]),
        mono: mono.clone(),
        flowed: false,
    };
    hooks.hooks.advance = true;
    let kite = fake_quotes(100.0);
    let quotes = Quotes {
        fallback: QuoteFallback::new(BTreeMap::from([(1, "AAA".to_owned())]), None),
        reader: &kite,
    };
    let mut strat = strategy();
    // A pass that drained ticks does not sleep; let ticks stop by advancing in the bus itself.
    struct OnceBus<'a> {
        inner: &'a mut FlowingBus,
        passes: usize,
    }
    impl TickBus for OnceBus<'_> {
        fn subscribe(&mut self, token: i64) {
            self.inner.subscribe(token);
        }
        fn drain(&mut self, token: i64) -> Vec<Tick> {
            self.passes += 1;
            // A tick on alternate passes, so time (the sleeps) advances while ticks flow.
            if self.passes.is_multiple_of(2) {
                self.inner.drain(token)
            } else {
                Vec::new()
            }
        }
    }
    let mut once = OnceBus {
        inner: &mut bus,
        passes: 0,
    };
    run_until_close(
        &mut strat,
        &mut ListStore::default(),
        &NoCandles,
        &mut once,
        &mut hooks,
        Some(quotes),
        &settings(),
        &RecordingConfirm::new(false),
        LoopTimings::default(),
    )
    .await;
    let calls = kite.calls.lock().unwrap().len();
    assert!(hooks.flowed);
    assert!((1..=6).contains(&calls), "{calls} quote calls");
}

#[tokio::test]
async fn the_loop_fires_on_first_tick_exactly_once_and_survives_its_failure() {
    let mut bus = Bus::default();
    let mut hooks = Hooks::walk(vec![at(9, 25), at(9, 26), at(9, 27), past_close()]);
    hooks.first_tick_fails = true;
    let mut strat = strategy();
    // Two ticks queued before the first pass, one more later is handled by the same queue.
    bus.queues.insert(
        1,
        vec![
            Tick {
                instrument_token: 1,
                last_price: Some(Decimal::from(99)),
                day_low: None,
                at: at(9, 25),
            },
            Tick {
                instrument_token: 1,
                last_price: Some(Decimal::from(99)),
                day_low: None,
                at: at(9, 25),
            },
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
    .await;
    assert_eq!(raised, 0);
    assert_eq!(hooks.first_ticks, 1);
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
    .await;
    assert!(store.queue.is_empty());
    assert!(confirm.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn reload_adds_a_setup_and_drops_a_name_the_list_lost() {
    let mut hooks = Hooks::walk(vec![at(11, 0), at(11, 1), past_close()]);
    hooks.reload_with = Some(Ok(vec![name("BBB", 2)]));
    let timings = LoopTimings {
        reload_every_seconds: 0.0,
        ..LoopTimings::default()
    };
    let mut strat = strategy();
    let mut bus = Bus::default();
    run_until_close(
        &mut strat,
        &mut ListStore::default(),
        &NoCandles,
        &mut bus,
        &mut hooks,
        None,
        &settings(),
        &RecordingConfirm::new(false),
        timings,
    )
    .await;
    assert_eq!(strat.tokens(), vec![2]);
    assert_eq!(hooks.subscribed, vec![vec![2]]);
    assert_eq!(hooks.unsubscribed, vec![vec![1]]);
    assert_eq!(bus.subscribed, vec![1, 2]);
}

#[tokio::test]
async fn reload_that_raises_keeps_the_list_and_the_heartbeat_keeps_beating() {
    let mut hooks = Hooks::walk(vec![at(11, 0), at(11, 1), past_close()]);
    hooks.reload_with = Some(Err("db down".to_owned()));
    let timings = LoopTimings {
        reload_every_seconds: 0.0,
        heartbeat_every_seconds: 0.0,
        ..LoopTimings::default()
    };
    let mut strat = strategy();
    run_until_close(
        &mut strat,
        &mut ListStore::default(),
        &NoCandles,
        &mut Bus::default(),
        &mut hooks,
        None,
        &settings(),
        &RecordingConfirm::new(false),
        timings,
    )
    .await;
    assert_eq!(strat.tokens(), vec![1]);
    assert_eq!(
        hooks.beats,
        vec![
            "watching 1 name(s), 0 signal(s) raised, at 11:00:00".to_owned(),
            "watching 1 name(s), 0 signal(s) raised, at 11:01:00".to_owned()
        ]
    );
}

// --- LV4: the late login --------------------------------------------------------------------

fn ist_at(h: u32, m: u32, s: u32) -> DateTime<FixedOffset> {
    ist()
        .from_local_datetime(&day().and_hms_opt(h, m, s).unwrap())
        .single()
        .unwrap()
}

#[tokio::test]
async fn late_login_the_monitor_waits_for_a_session_and_starts_when_it_comes() {
    let answers = Mutex::new(vec![Ok(false), Ok(false), Ok(true)].into_iter());
    let moment = Mutex::new(ist_at(9, 14, 0));
    let slept = Mutex::new(Vec::new());
    let ok = wait_for_session(
        || answers.lock().unwrap().next().unwrap(),
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
async fn late_login_gives_up_at_fifteen_twenty_and_says_so() {
    let moment = Mutex::new(ist_at(15, 19, 30));
    let ok = wait_for_session(
        || Ok(false),
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
async fn a_session_check_that_raises_is_not_yet_not_a_crash() {
    let answers = Mutex::new(vec![Err("kite down".to_owned()), Ok(true)].into_iter());
    let ok = wait_for_session(
        || answers.lock().unwrap().next().unwrap(),
        || ist_at(9, 0, 0),
        |_| async {},
        session_wait_until(),
        30.0,
    )
    .await;
    assert!(ok);
}

#[test]
fn the_metrics_port_is_its_own_or_the_desks_plus_one() {
    assert_eq!(monitor_metrics_port(Some(" 9200 "), 9100), Some(9200));
    assert_eq!(monitor_metrics_port(Some(""), 9100), Some(9101));
    assert_eq!(monitor_metrics_port(None, 0), None);
}

// --- the notifier (test_swing_notify.py) ------------------------------------------------------

fn notice(skipped: Option<&str>) -> SignalNotice {
    SignalNotice {
        symbol: "AAA".to_owned(),
        setup: "FLAG".to_owned(),
        at: ist_at(9, 31, 0),
        entry: Some(Decimal::new(10050, 2)),
        stop: Some(Decimal::new(9500, 2)),
        quantity: skipped.map_or(Some(80), |_| None),
        risk_inr: skipped.map_or(Some(Decimal::new(44000, 2)), |_| None),
        plan_expires_at: skipped.map_or(Some(ist_at(10, 1, 0)), |_| None),
        skipped: skipped.map(str::to_owned),
        mode: "DRY_RUN".to_owned(),
    }
}

#[test]
fn the_notice_carries_the_whole_line_and_says_it_cannot_act() {
    let n = notice(None);
    assert_eq!(n.subject(), "[swing] AAA triggered at 09:31 — DRY_RUN");
    let text = n.text();
    for part in [
        "entry   100.50",
        "stop    95.00",
        "qty     80",
        "risk    ₹440.00",
        "expires 10:01 IST",
        "mode    DRY_RUN",
    ] {
        assert!(text.contains(part), "{part} missing from {text}");
    }
    assert!(text.ends_with("This message tells; it cannot act. Open the desk page."));
}

#[test]
fn a_skipped_trigger_says_why() {
    let n = notice(Some("SESSION_CAP 3 entries today"));
    assert_eq!(
        n.subject(),
        "[swing] AAA triggered at 09:31 — skipped (SESSION_CAP 3 entries today)"
    );
    assert!(n.text().contains("No line: SESSION_CAP 3 entries today."));
    assert!(notice_json(&n).contains("\"quantity\": null"));
}

#[test]
fn the_process_default_is_no_channel_at_all() {
    assert!(build_senders(&settings()).is_empty());
}

struct RaisingSender;
impl Sender for RaisingSender {
    fn name(&self) -> &str {
        "raising"
    }
    fn send(&self, _s: &str, _t: &str) -> Result<bool, String> {
        Err("smtp down".to_owned())
    }
}

struct FlakySender;
impl Sender for FlakySender {
    fn name(&self) -> &str {
        "flaky"
    }
    fn send(&self, _s: &str, _t: &str) -> Result<bool, String> {
        Ok(false)
    }
}

struct OkSender;
impl Sender for OkSender {
    fn name(&self) -> &str {
        "ok"
    }
    fn send(&self, _s: &str, _t: &str) -> Result<bool, String> {
        Ok(true)
    }
}

#[test]
fn a_notifier_failure_never_stops_the_monitor_and_false_is_not_sent() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let sink = seen.clone();
    let mut n = Notifier::new(
        vec![
            Box::new(RaisingSender),
            Box::new(FlakySender),
            Box::new(OkSender),
        ],
        Some(Box::new(move |c: &str, o: &str| {
            sink.lock().unwrap().push((c.to_owned(), o.to_owned()));
            Ok(())
        })),
    );
    assert_eq!(n.notify(&notice(None)), 1);
    assert_eq!(n.channels(), vec!["raising", "flaky", "ok"]);
    let delivered: Vec<bool> = n.sent.iter().map(|s| s.2).collect();
    assert_eq!(delivered, vec![false, false, true]);
    assert_eq!(seen.lock().unwrap().len(), 3);
}

#[test]
fn notifier_observe_sink_that_raises_is_dropped() {
    let mut n = Notifier::new(
        vec![Box::new(OkSender)],
        Some(Box::new(|_: &str, _: &str| Err("metrics down".to_owned()))),
    );
    assert_eq!(n.notify(&notice(None)), 1);
}

#[test]
fn the_notifier_module_names_no_confirm_path() {
    let code = code_only(include_str!("../notify.rs"));
    for forbidden in ["execute", "confirm", "getUpdates", "Gateway", "place"] {
        assert!(!code.contains(forbidden), "notify.rs names {forbidden}");
    }
}

// --- the clock: run_chore and the backstop -----------------------------------------------------

#[derive(Default)]
struct Counter(Mutex<Vec<(String, String)>>);

impl SweepCounter for Counter {
    fn count(&self, sweep: &str, outcome: &str) {
        self.0
            .lock()
            .unwrap()
            .push((sweep.to_owned(), outcome.to_owned()));
    }
}

/// Seams that cannot build a store (no database): every chore is `unavailable`.
struct NoDatabase;

struct NoPrice;

#[async_trait]
impl PriceOf for NoPrice {
    async fn price_of(&self, _symbol: &str) -> Result<Option<Decimal>, String> {
        Ok(None)
    }
}

#[async_trait]
impl ChoreSeams for NoDatabase {
    async fn open_store(&self) -> Result<Box<dyn SwingStore>, String> {
        Err("OperationalError: no database".to_owned())
    }
    fn gateway(&self) -> Result<Arc<dyn Gateway>, String> {
        Err("no Kite session".to_owned())
    }
    fn orders(&self) -> Option<Arc<dyn OrderSource>> {
        None
    }
    fn prices(&self) -> Arc<dyn PriceOf> {
        Arc::new(NoPrice)
    }
}

#[tokio::test]
async fn run_chore_never_raises_and_counts_unavailable() {
    let counter = Counter::default();
    run_chore(
        "cutoff",
        &NoDatabase,
        &settings(),
        ist_at(10, 45, 0),
        &counter,
    )
    .await;
    run_chore(
        "gtt_sweep",
        &NoDatabase,
        &settings(),
        ist_at(15, 15, 0),
        &counter,
    )
    .await;
    assert_eq!(
        *counter.0.lock().unwrap(),
        vec![
            ("cutoff".to_owned(), "unavailable".to_owned()),
            ("gtt_sweep".to_owned(), "unavailable".to_owned())
        ]
    );
    run_chore(
        "cutoff",
        &NoDatabase,
        &settings(),
        ist_at(10, 45, 0),
        &NoCounter,
    )
    .await;
}

#[tokio::test]
async fn run_after_close_runs_the_cutoff_now_and_sleeps_to_1515_for_the_sweep() {
    let counter = Counter::default();
    let moment = Mutex::new(ist_at(15, 30, 0) - Duration::hours(5));
    let slept = Mutex::new(Vec::new());
    let code = run_after_close(
        day(),
        &NoDatabase,
        &settings(),
        || *moment.lock().unwrap(),
        |secs| {
            slept.lock().unwrap().push(secs);
            #[allow(clippy::cast_possible_truncation)]
            let next = *moment.lock().unwrap() + Duration::milliseconds((secs * 1000.0) as i64);
            *moment.lock().unwrap() = next;
            async {}
        },
        &counter,
    )
    .await;
    assert_eq!(code, 0);
    // 10:30 → 15:15 is 4 h 45 min.
    assert_eq!(*slept.lock().unwrap(), vec![17_100.0]);
    // A gateway that cannot be built at 10:45 does not cost the 15:15 sweep.
    assert_eq!(
        *counter.0.lock().unwrap(),
        vec![
            ("cutoff".to_owned(), "unavailable".to_owned()),
            ("gtt".to_owned(), "unavailable".to_owned())
        ]
    );
}

#[tokio::test]
async fn a_clock_started_after_1515_sweeps_at_once() {
    let slept = Mutex::new(Vec::<f64>::new());
    run_after_close(
        day(),
        &NoDatabase,
        &settings(),
        || ist_at(15, 20, 0),
        |s| {
            slept.lock().unwrap().push(s);
            async {}
        },
        &NoCounter,
    )
    .await;
    assert!(slept.lock().unwrap().is_empty());
}
