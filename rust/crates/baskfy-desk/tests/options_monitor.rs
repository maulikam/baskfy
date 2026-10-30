//! Lane **options**, OP9's monitor: ports the desk's `tests/test_options_monitor.py`,
//! `tests/test_options_monitor_loop.py` (OP15) and `tests/test_options_hardening.py`'s
//! `TestTokenDeath` (OP14).
//!
//! `06` OP9's acceptance criteria, each with its test: flag off → not instantiated; the five
//! recorded fixtures (`tools/options/fixtures`, `make_fixtures.py`'s independent answer key)
//! replay to exactly the expected exit; a restart at 11:30 resumes from `op_position`; a source
//! scan finds no placing verb. The store's SQL on a real Postgres is
//! `tests/options_confirm_db.rs::the_position_store_raises_one_exit_and_marks` (bed only).
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_core::options::bars::Bar;
use baskfy_core::options::exits::ExitVerdict;
use baskfy_desk::app::StateParts;
use baskfy_desk::clock::{FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::options::clock::{
    self as op_clock, Bus, LegQuotes, QuoteRaw, Seams, run_session,
};
use baskfy_desk::lanes::options::monitor::{
    Cadence, NIFTY_50_TOKEN, NiftyOptionsMonitor, PositionStore, TrackedPosition, aware_ist,
    build_monitor, local_wall_time,
};
use baskfy_desk::lanes::options::monitor_loop as L;
use baskfy_desk::lanes::options::replay::{self, ListStore};
use baskfy_desk::lanes::options::store::position_from_rows;
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Db, DbConfig, DbValue, Row};
use baskfy_desk::wire::{PyDict, PyValue, dict};
use baskfy_desk::{DeskPaths, build_state};
use baskfy_providers::records::PyDateTime;
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, TimeZone};
use rust_decimal::Decimal;
use serde_json::Value;

const SCENARIOS: [&str; 5] = [
    "o1-profit",
    "o1-restart-1130",
    "o1-feed-lost",
    "o2-time-stop",
    "o3a-target",
];

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap()
}

fn fixtures() -> PathBuf {
    repo().join("tools/options/fixtures")
}

fn fixture(name: &str) -> Value {
    serde_json::from_str(&std::fs::read_to_string(fixtures().join(format!("{name}.json"))).unwrap())
        .unwrap()
}

fn expected(name: &str) -> Value {
    serde_json::from_str(
        &std::fs::read_to_string(fixtures().join(format!("{name}.expected.json"))).unwrap(),
    )
    .unwrap()
}

fn src(rel: &str) -> String {
    std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join(rel)).unwrap()
}

fn d(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

fn dt(y: i32, mo: u32, d: u32, h: u32, mi: u32, s: u32) -> NaiveDateTime {
    NaiveDate::from_ymd_opt(y, mo, d)
        .unwrap()
        .and_hms_opt(h, mi, s)
        .unwrap()
}

fn day27() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, 27).unwrap()
}

fn tick_at(tick: &PyDict) -> NaiveDateTime {
    match tick.get("exchange_timestamp") {
        Some(PyValue::DateTime(PyDateTime::Naive(n))) => *n,
        other => panic!("{other:?}"),
    }
}

/// The source without comments — what runs, not what explains (the swing suite's rule).
fn code_only(source: &str) -> String {
    source
        .lines()
        .map(|l| l.split_once("//").map_or(l, |(code, _)| code))
        .collect::<Vec<_>>()
        .join("\n")
}

// --- the flag ----------------------------------------------------------------------------------

#[test]
fn flag_off_the_strategy_is_never_constructed() {
    let store = ListStore::new(vec![], vec![]);
    assert!(build_monitor(false, store.clone(), day27()).is_none());
    assert!(build_monitor(true, store, day27()).is_some());
}

#[test]
fn the_flag_defaults_off() {
    assert!(!Settings::defaults().unwrap().options_monitor_enabled);
}

const KITE: &str = r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;
/// A database the bed does not have: any connect is refused, so a test that reaches the
/// database fails loudly.
const DEAD: &str = "postgres://baskfy:rs-test-only@127.0.0.1:55432/rsd_nowhere";

async fn state_with(
    tweak: impl FnOnce(&mut Settings),
) -> (baskfy_desk::AppState, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let kite_dir = dir.path().join("kite");
    std::fs::create_dir_all(kite_dir.join("instruments")).unwrap();
    std::fs::write(kite_dir.join("kite.json"), KITE).unwrap();
    let kite = Arc::new(ReplayKite::load(&kite_dir, None, "test-api-key").unwrap());
    let mut settings = Settings::defaults().unwrap();
    tweak(&mut settings);
    let db = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "desk".to_owned(),
        database_url: DEAD.to_owned(),
        db_path: dir.path().join("unused.db"),
    })
    .await
    .unwrap();
    let state = build_state(StateParts {
        settings,
        clock: Arc::new(FixedClock::new(
            ist().with_ymd_and_hms(2026, 10, 27, 9, 14, 0).unwrap(),
        )),
        ids: Arc::new(HarnessIds::new("options-monitor-test")),
        db,
        kite,
        paths: DeskPaths::under(dir.path()),
        harness: None,
    });
    (state, dir)
}

#[tokio::test]
async fn main_returns_zero_with_the_flag_off_before_the_world() {
    // `main` checks the flag before the database, the ticker or Kite: the database here is one
    // that refuses every connect, and no Kite key is set.
    let (state, _dir) = state_with(|s| s.options_monitor_enabled = false).await;
    let env = |_: &str| -> Option<String> { Some("7".to_owned()) };
    assert_eq!(
        baskfy_desk::lanes::options::options_monitor_main(&state, &env).await,
        Ok(0)
    );
}

#[tokio::test]
async fn main_without_a_user_is_exit_2_before_the_world() {
    let (state, _dir) = state_with(|s| s.options_monitor_enabled = true).await;
    let none = |_: &str| -> Option<String> { None };
    assert_eq!(
        baskfy_desk::lanes::options::options_monitor_main(&state, &none).await,
        Ok(2)
    );
    let bad = |_: &str| -> Option<String> { Some("seven".to_owned()) };
    let err = baskfy_desk::lanes::options::options_monitor_main(&state, &bad)
        .await
        .unwrap_err();
    assert!(
        err.starts_with("ValueError: invalid literal for int()"),
        "{err}"
    );
}

#[test]
fn main_checks_the_flag_before_the_world_in_the_source() {
    let source = src("src/lanes/options.rs");
    let body = &source[source.find("pub async fn options_monitor_main(").unwrap()..];
    let flag = body.find("options_monitor_enabled").unwrap();
    for heavy in ["state.db.connect()", "desk.kite()", "kite_api_key"] {
        assert!(flag < body.find(heavy).unwrap(), "{heavy}");
    }
}

#[test]
fn the_runner_holds_no_gateway() {
    // Python: `"gateway=None" in inspect.getsource(main)`. Here the strategy has no gateway field
    // and `build_monitor` no gateway argument.
    let source = code_only(&src("src/lanes/options/monitor.rs"));
    let strukt = &source[source.find("pub struct NiftyOptionsMonitor<").unwrap()..];
    let strukt = &strukt[..strukt.find("\n}").unwrap()];
    assert!(!strukt.contains("gateway"), "{strukt}");
    let build = &source[source.find("pub fn build_monitor<").unwrap()..];
    let build = &build[..build.find('{').unwrap()];
    assert!(!build.contains("gateway"), "{build}");
}

// --- the acceptance replays --------------------------------------------------------------------

#[tokio::test]
async fn each_fixture_raises_exactly_the_expected_exit() {
    for name in SCENARIOS {
        let monitor = replay::replay(&fixture(name)).await.unwrap();
        assert_eq!(
            replay::as_json(&monitor.store.exits),
            expected(name),
            "{name}"
        );
    }
}

#[test]
fn the_expectations_are_the_ones_06_names() {
    let codes = |name: &str| -> Vec<(String, String)> {
        expected(name)
            .as_array()
            .unwrap()
            .iter()
            .map(|e| {
                (
                    e["code"].as_str().unwrap().to_owned(),
                    e["reason"].as_str().unwrap().to_owned(),
                )
            })
            .collect()
    };
    let pair = |a: &str, b: &str| vec![(a.to_owned(), b.to_owned())];
    assert_eq!(codes("o1-profit"), pair("PROFIT", "RULE"));
    assert_eq!(codes("o2-time-stop"), pair("TIME_STOP", "RULE"));
    assert_eq!(codes("o3a-target"), pair("TARGET", "RULE"));
    assert_eq!(codes("o1-feed-lost"), pair("HARD_EXIT", "FEED_LOST"));
    let feed = expected("o1-feed-lost")[0]["at"]
        .as_str()
        .unwrap()
        .to_owned();
    assert!(feed.starts_with("2026-10-27T14:05"));
    assert_eq!(expected("o2-time-stop")[0]["at"], "2026-10-19T10:51:00");
}

#[tokio::test]
async fn the_cli_agrees() {
    for name in SCENARIOS {
        let args = vec![
            fixtures()
                .join(format!("{name}.json"))
                .display()
                .to_string(),
            "--expect".to_owned(),
            fixtures()
                .join(format!("{name}.expected.json"))
                .display()
                .to_string(),
        ];
        let (code, lines) = replay::main(&args).await.unwrap();
        assert_eq!(code, 0, "{name}: {lines:?}");
        assert_eq!(lines.last().unwrap(), "OK: the exits match the expectation");
    }
}

#[tokio::test]
async fn one_exit_per_position_whatever_follows() {
    let monitor = replay::replay(&fixture("o1-profit")).await.unwrap();
    assert_eq!(monitor.store.exits.len(), 1);
    assert_eq!(monitor.exits.len(), 1);
}

#[tokio::test]
async fn marks_are_recorded_every_30_seconds_at_most() {
    let monitor = replay::replay(&fixture("o3a-target")).await.unwrap();
    let stamps: Vec<NaiveDateTime> = monitor.store.marks.iter().map(|m| m.2).collect();
    assert!(!stamps.is_empty());
    assert!(stamps.windows(2).all(|w| (w[1] - w[0]).num_seconds() >= 30));
}

// --- the restart -------------------------------------------------------------------------------

#[tokio::test]
async fn a_restart_at_1130_resumes_from_op_position() {
    let monitor = replay::replay(&fixture("o1-restart-1130")).await.unwrap();
    let ids: Vec<i64> = monitor
        .store
        .positions
        .iter()
        .map(|t| t.session_id)
        .collect();
    assert_eq!(ids, vec![1]);
    let bars = monitor.bars();
    assert_eq!(bars[0].ts, aware_ist(dt(2026, 10, 27, 9, 15, 0)));
    assert_eq!(
        replay::as_json(&monitor.store.exits),
        expected("o1-restart-1130")
    );
}

#[tokio::test]
async fn a_position_already_exiting_is_not_evaluated_again() {
    let fx = fixture("o1-profit");
    let mut tracked = replay::position_of(&fx["position"]).unwrap();
    tracked.exit_plan_id = Some("X-1".to_owned());
    let mut monitor = NiftyOptionsMonitor::new(ListStore::new(vec![tracked], vec![]), day27());
    monitor.start().await;
    for tick in replay::ticks_of(&fx).unwrap() {
        monitor.tick(&tick).await.unwrap();
    }
    assert!(monitor.store.exits.is_empty() && monitor.exits.is_empty());
}

// --- nothing places ----------------------------------------------------------------------------

#[test]
fn no_placing_verb_in_the_code() {
    let options = src("src/lanes/options.rs");
    let runner = &options[options
        .find("/// `Kite()`'s refusal without a key")
        .unwrap()
        ..options
            .find("/// Run one of the lane's background services by name")
            .unwrap()];
    for (name, text) in [
        ("monitor.rs", src("src/lanes/options/monitor.rs")),
        ("clock.rs", src("src/lanes/options/clock.rs")),
        ("options.rs (the runner)", runner.to_owned()),
        ("monitor_loop.rs", src("src/lanes/options/monitor_loop.rs")),
    ] {
        let code = code_only(&text);
        for verb in [
            r"\.place\(",
            r"\bplace\(",
            r"\bplace_order\b",
            r"\bplace_gtt",
            r"\bOrderGateway\b",
            r"\bgw\.",
            r"\bkc\.place",
        ] {
            assert!(
                regex::Regex::new(verb).unwrap().find(&code).is_none(),
                "{name}: {verb}"
            );
        }
    }
}

#[test]
fn the_strategy_targets_nothing() {
    let monitor = NiftyOptionsMonitor::new(ListStore::new(vec![], vec![]), day27());
    assert!(monitor.generate_targets().is_empty());
}

#[test]
fn the_quote_fallback_uses_the_slot_taking_read() {
    let options = code_only(&src("src/lanes/options.rs"));
    assert!(options.contains(".quote_raw(keys)"));
    let clock = code_only(&src("src/lanes/options/clock.rs"));
    assert!(clock.contains("quote_raw(") && !clock.contains("kc.quote("));
}

// --- the loop, the quotes, the tokens ----------------------------------------------------------

struct FakeKite {
    calls: Mutex<Vec<Vec<String>>>,
}

#[async_trait]
impl QuoteRaw for FakeKite {
    async fn quote_raw(&self, keys: &[String]) -> Result<PyDict, String> {
        self.calls.lock().unwrap().push(keys.to_vec());
        let mut out = PyDict::new();
        for key in keys {
            let level =
                |p: f64| PyValue::List(vec![PyValue::Map(dict([("price", PyValue::Float(p))]))]);
            out.insert(
                key.clone(),
                PyValue::Map(dict([
                    ("instrument_token", PyValue::Int(key.parse().unwrap())),
                    ("last_price", PyValue::Float(25010.0)),
                    (
                        "depth",
                        PyValue::Map(dict([("buy", level(10.0)), ("sell", level(10.2))])),
                    ),
                    (
                        "timestamp",
                        PyValue::DateTime(PyDateTime::Naive(dt(2026, 10, 27, 14, 6, 0))),
                    ),
                ])),
            );
        }
        Ok(out)
    }
}

#[tokio::test]
async fn at_most_one_call_per_interval() {
    let kite = FakeKite {
        calls: Mutex::new(vec![]),
    };
    let mut quotes = LegQuotes::new(&kite, 5.0);
    let now = dt(2026, 10, 27, 14, 6, 0);
    assert!(
        quotes
            .poll(&[NIFTY_50_TOKEN, 9001], now, 0.0)
            .await
            .is_some()
    );
    assert!(
        quotes
            .poll(&[NIFTY_50_TOKEN, 9001], now, 4.9)
            .await
            .is_none()
    );
    assert!(
        quotes
            .poll(&[NIFTY_50_TOKEN, 9001], now, 5.0)
            .await
            .is_some()
    );
    let want = vec!["256265".to_owned(), "9001".to_owned()];
    assert_eq!(*kite.calls.lock().unwrap(), vec![want.clone(), want]);
}

#[tokio::test]
async fn a_quote_becomes_a_depth_tick() {
    let kite = FakeKite {
        calls: Mutex::new(vec![]),
    };
    let mut quotes = LegQuotes::new(&kite, 5.0);
    let ticks = quotes
        .poll(&[9001, 9002], dt(2026, 10, 27, 0, 0, 0), 0.0)
        .await
        .unwrap();
    let tick = &ticks[0];
    assert_eq!(tick.get("instrument_token"), Some(&PyValue::Int(9001)));
    let Some(PyValue::Map(depth)) = tick.get("depth") else {
        panic!()
    };
    let Some(PyValue::List(buy)) = depth.get("buy") else {
        panic!()
    };
    let PyValue::Map(top) = &buy[0] else { panic!() };
    assert_eq!(top.get("price"), Some(&PyValue::Float(10.0)));
}

struct Broken(&'static str);

#[async_trait]
impl QuoteRaw for Broken {
    async fn quote_raw(&self, _keys: &[String]) -> Result<PyDict, String> {
        Err(self.0.to_owned())
    }
}

#[tokio::test]
async fn a_failed_call_is_counted_and_empty() {
    let kite = Broken("RuntimeError");
    let mut quotes = LegQuotes::new(&kite, 5.0);
    assert_eq!(
        quotes.poll(&[9001], dt(2026, 10, 27, 0, 0, 0), 0.0).await,
        Some(vec![])
    );
    assert_eq!(quotes.failures, 1);
}

/// `Bus()`: a queue per subscribed token, never fed (the tests drive the clock, not ticks).
#[derive(Default)]
struct TestBus {
    subscribed: Vec<i64>,
}

impl Bus for TestBus {
    fn subscribe(&mut self, token: i64) {
        self.subscribed.push(token);
    }

    fn drain(&mut self, _token: i64) -> Vec<PyDict> {
        Vec::new()
    }
}

/// `now=lambda: next(moments)`, `sleep=asyncio.sleep(0)`, `clock=` a seam (default: still).
struct Script {
    moments: std::vec::IntoIter<NaiveDateTime>,
    on_now: Box<dyn FnMut(NaiveDateTime) + Send>,
    ticking: Option<f64>,
    t: f64,
}

impl Script {
    fn new(moments: Vec<NaiveDateTime>) -> Self {
        Self {
            moments: moments.into_iter(),
            on_now: Box::new(|_| {}),
            ticking: None,
            t: 0.0,
        }
    }
}

#[async_trait]
impl Seams for Script {
    fn now(&mut self) -> NaiveDateTime {
        let m = self.moments.next().expect("the script ran out of moments");
        (self.on_now)(m);
        m
    }

    fn clock(&mut self) -> f64 {
        if let Some(step) = self.ticking {
            self.t += step;
        }
        self.t
    }

    async fn sleep(&mut self, _seconds: f64) {
        tokio::task::yield_now().await;
    }
}

/// A store two parties hold: the monitor, and the test standing in for the executor.
#[derive(Clone, Default)]
struct Shared(Arc<Mutex<ListStore>>);

#[async_trait]
impl PositionStore for Shared {
    async fn open_positions(&mut self, day: NaiveDate) -> Result<Vec<TrackedPosition>, String> {
        let mut s = self.0.lock().unwrap().clone();
        s.open_positions(day).await
    }

    async fn raise_exit(
        &mut self,
        tracked: &TrackedPosition,
        verdict: &ExitVerdict,
        at: NaiveDateTime,
    ) -> Result<Option<String>, String> {
        let mut s = self.0.lock().unwrap().clone();
        let out = s.raise_exit(tracked, verdict, at).await;
        *self.0.lock().unwrap() = s;
        out
    }

    async fn record_mark(
        &mut self,
        tracked: &TrackedPosition,
        value: Decimal,
        at: NaiveDateTime,
    ) -> Result<(), String> {
        let mut s = self.0.lock().unwrap().clone();
        let out = s.record_mark(tracked, value, at).await;
        *self.0.lock().unwrap() = s;
        out
    }

    async fn index_minutes(
        &mut self,
        day: NaiveDate,
        until: NaiveDateTime,
    ) -> Result<Vec<Bar>, String> {
        let mut s = self.0.lock().unwrap().clone();
        s.index_minutes(day, until).await
    }
}

#[tokio::test]
async fn an_idle_pass_judges_a_silent_feed() {
    let fx = fixture("o1-feed-lost");
    let tracked = replay::position_of(&fx["position"]).unwrap();
    let mut monitor = NiftyOptionsMonitor::new(ListStore::new(vec![tracked], vec![]), day27());
    monitor.start().await;
    for tick in replay::ticks_of(&fx).unwrap() {
        if tick_at(&tick) < dt(2026, 10, 27, 14, 5, 0) {
            monitor.tick(&tick).await.unwrap();
        }
    }
    assert!(monitor.store.exits.is_empty());
    let mut moments = vec![dt(2026, 10, 27, 14, 5, 30); 2];
    moments.extend([dt(2026, 10, 27, 15, 31, 0); 4]);
    let mut seams = Script::new(moments);
    let raised = run_session(
        &mut monitor,
        &mut TestBus::default(),
        None,
        None,
        None,
        &mut seams,
        1.0,
    )
    .await
    .unwrap();
    assert_eq!(raised, 1);
    let codes: Vec<(String, String)> = monitor
        .store
        .exits
        .iter()
        .map(|e| (e.1.clone(), e.2.clone()))
        .collect();
    assert_eq!(
        codes,
        vec![("HARD_EXIT".to_owned(), "FEED_LOST".to_owned())]
    );
}

#[tokio::test]
async fn new_legs_are_followed_on_the_ticker() {
    let fx = fixture("o3a-target");
    let tracked = replay::position_of(&fx["position"]).unwrap();
    let shared = Shared::default();
    let mut monitor = NiftyOptionsMonitor::with(
        shared.clone(),
        NaiveDate::from_ymd_opt(2026, 10, 20).unwrap(),
        Cadence {
            refresh_seconds: 0,
            ..Cadence::default()
        },
        local_wall_time(),
    );
    let mut moments = vec![dt(2026, 10, 20, 10, 23, 0); 3];
    moments.extend([dt(2026, 10, 20, 15, 31, 0); 3]);
    let mut seams = Script::new(moments);
    let opened = shared.clone();
    seams.on_now = Box::new(move |m| {
        use chrono::Timelike as _;
        let mut s = opened.0.lock().unwrap();
        if m.minute() == 23 && s.positions.is_empty() {
            s.positions.push(tracked.clone()); // the executor opened it (OP10)
        }
    });
    let mut followed: Vec<Vec<i64>> = Vec::new();
    let mut follow = |t: &[i64]| -> Result<(), String> {
        followed.push(t.to_vec());
        Ok(())
    };
    run_session(
        &mut monitor,
        &mut TestBus::default(),
        Some(&mut follow),
        None,
        None,
        &mut seams,
        1.0,
    )
    .await
    .unwrap();
    assert_eq!(followed[0], vec![NIFTY_50_TOKEN]);
    let mut second = followed[1].clone();
    second.sort_unstable();
    assert_eq!(second, vec![9201, 9202]);
}

#[tokio::test]
async fn the_loop_ends_at_the_close() {
    let mut monitor = NiftyOptionsMonitor::new(ListStore::new(vec![], vec![]), day27());
    let mut seams = Script::new(vec![dt(2026, 10, 27, 15, 30, 0); 2]);
    let raised = run_session(
        &mut monitor,
        &mut TestBus::default(),
        None,
        None,
        None,
        &mut seams,
        1.0,
    )
    .await
    .unwrap();
    assert_eq!(raised, 0);
}

struct Recording {
    calls: Vec<NaiveDateTime>,
    fail: bool,
}

#[async_trait]
impl op_clock::Act for Recording {
    async fn act(&mut self, now: NaiveDateTime) -> Result<(), String> {
        self.calls.push(now);
        if self.fail {
            Err("RuntimeError: sweep".to_owned())
        } else {
            Ok(())
        }
    }
}

#[tokio::test]
async fn the_sweep_runs_after_every_pass_and_its_error_does_not_stop_the_loop() {
    // `act(now())` after every evaluation pass; an executor error is logged, never fatal.
    let mut monitor = NiftyOptionsMonitor::new(ListStore::new(vec![], vec![]), day27());
    let m = |s| dt(2026, 10, 27, 11, 0, s);
    let mut seams = Script::new(vec![
        m(0),
        m(1),
        m(2),
        m(3),
        m(4),
        m(5),
        dt(2026, 10, 27, 15, 31, 0),
    ]);
    let mut act = Recording {
        calls: vec![],
        fail: true,
    };
    let raised = run_session(
        &mut monitor,
        &mut TestBus::default(),
        None,
        None,
        Some(&mut act),
        &mut seams,
        1.0,
    )
    .await
    .unwrap();
    assert_eq!(raised, 0);
    assert_eq!(act.calls, vec![m(2), m(5)]);
}

// --- the store's rows become a position --------------------------------------------------------

fn head(sleeve: &str, structure: &str, detail: &str) -> Row {
    Row(vec![
        ("session_id".to_owned(), DbValue::Int(7)),
        ("entry_points".to_owned(), DbValue::Text("30.00".to_owned())),
        (
            "opened_at".to_owned(),
            DbValue::Naive(dt(2026, 10, 20, 10, 22, 0)),
        ),
        ("exit_plan_id".to_owned(), DbValue::Null),
        ("sleeve".to_owned(), DbValue::Text(sleeve.to_owned())),
        ("structure".to_owned(), DbValue::Text(structure.to_owned())),
        (
            "risk_budget_inr".to_owned(),
            DbValue::Text("2500.00".to_owned()),
        ),
        (
            "width_points".to_owned(),
            DbValue::Text("100.00".to_owned()),
        ),
        ("detail".to_owned(), DbValue::Json(detail.to_owned())),
    ])
}

fn legs() -> Vec<Row> {
    let leg = |id, role: &str, strike: &str, price: &str, token| {
        Row(vec![
            ("id".to_owned(), DbValue::Int(id)),
            ("role".to_owned(), DbValue::Text(role.to_owned())),
            ("strike".to_owned(), DbValue::Text(strike.to_owned())),
            ("quantity".to_owned(), DbValue::Int(65)),
            ("filled_qty".to_owned(), DbValue::Int(65)),
            ("avg_price".to_owned(), DbValue::Text(price.to_owned())),
            ("instrument_token".to_owned(), DbValue::Int(token)),
        ])
    };
    vec![
        leg(1, "LONG_CALL", "25200", "35.05", 9201),
        leg(2, "SHORT_CALL", "25300", "5.00", 9202),
    ]
}

#[test]
fn o3a_reads_the_morning_range() {
    let detail =
        r#"{"direction": "UP", "gate": {"range_high": "25182.00", "range_low": "24998.00"}}"#;
    let row = position_from_rows(&head("O3A", "DEBIT_SPREAD", detail), &legs()).unwrap();
    let tracked = TrackedPosition::from_tracked(&row);
    let p = &tracked.position;
    assert_eq!(
        (p.range_high, p.range_low, p.width_points),
        (Some(d("25182.00")), Some(d("24998.00")), Some(d("100.00")))
    );
    let fills: Vec<Decimal> = p.legs.iter().map(|l| l.fill_price).collect();
    assert_eq!(fills, vec![d("35.05"), d("5.00")]);
    assert_eq!(tracked.tokens(), vec![9201, 9202]);
    // `_aware(opened_at)`: a naive time is IST.
    assert_eq!(p.opened_at, aware_ist(dt(2026, 10, 20, 10, 22, 0)));
}

#[test]
fn o2_reads_the_opening_range_and_o3b_the_half_gap() {
    let o2 = position_from_rows(
        &head(
            "O2",
            "LONG_OPTION",
            r#"{"direction": "UP", "gate": {"or_high": "25032", "or_low": "25008"}}"#,
        ),
        &legs()[..1],
    )
    .unwrap();
    assert_eq!(
        (o2.range_high, o2.range_low),
        (Some(d("25032")), Some(d("25008")))
    );
    let o3b = position_from_rows(
        &head(
            "O3B",
            "DEBIT_SPREAD",
            r#"{"direction": "UP", "gate": {"half_gap": "25100.00"}}"#,
        ),
        &legs(),
    )
    .unwrap();
    assert_eq!(o3b.half_gap, Some(d("25100.00")));
}

// --- OP14: token death (test_options_hardening.py) ---------------------------------------------

async fn run_dead(kind: &'static str) -> Vec<(String, String, NaiveDateTime)> {
    let fx = fixture("o1-profit");
    let tracked = replay::position_of(&fx["position"]).unwrap();
    let mut monitor = NiftyOptionsMonitor::new(ListStore::new(vec![tracked], vec![]), day27());
    let kite = Broken(kind);
    let mut quotes = LegQuotes::new(&kite, 5.0);
    let mut moments: Vec<NaiveDateTime> = (0..20)
        .step_by(2)
        .map(|s| dt(2026, 10, 27, 11, 0, s))
        .collect();
    moments.extend([dt(2026, 10, 27, 15, 31, 0); 4]);
    let mut seams = Script::new(moments);
    seams.ticking = Some(10.0);
    run_session(
        &mut monitor,
        &mut TestBus::default(),
        None,
        Some(&mut quotes),
        None,
        &mut seams,
        1.0,
    )
    .await
    .unwrap();
    monitor
        .store
        .exits
        .iter()
        .map(|e| (e.1.clone(), e.2.clone(), e.3))
        .collect()
}

#[tokio::test]
async fn a_refused_token_raises_every_exit_at_once() {
    let exits = run_dead("TokenException").await;
    let codes: Vec<(&str, &str)> = exits.iter().map(|e| (e.0.as_str(), e.1.as_str())).collect();
    assert_eq!(codes, vec![("HARD_EXIT", "FEED_LOST")]);
    assert!(exits[0].2 < dt(2026, 10, 27, 11, 1, 0));
}

#[tokio::test]
async fn a_network_error_is_not_a_dead_token() {
    let exits = run_dead("RuntimeError").await;
    assert!(exits.iter().all(|e| e.2 >= dt(2026, 10, 27, 14, 0, 0)));
}

#[test]
fn the_real_kiteconnect_exception_is_recognised() {
    assert!(op_clock::is_token_error("TokenException"));
    assert!(!op_clock::is_token_error("RuntimeError"));
}

#[tokio::test]
async fn a_successful_poll_clears_the_state() {
    struct Flaky(Mutex<bool>);
    #[async_trait]
    impl QuoteRaw for Flaky {
        async fn quote_raw(&self, _keys: &[String]) -> Result<PyDict, String> {
            if *self.0.lock().unwrap() {
                Err("TokenException".to_owned())
            } else {
                Ok(PyDict::new())
            }
        }
    }
    let kite = Flaky(Mutex::new(true));
    let mut quotes = LegQuotes::new(&kite, 5.0);
    quotes.poll(&[9001], dt(2026, 10, 27, 11, 0, 0), 10.0).await;
    assert!(quotes.token_dead);
    *kite.0.lock().unwrap() = false;
    quotes.poll(&[9001], dt(2026, 10, 27, 11, 1, 0), 20.0).await;
    assert!(!quotes.token_dead);
}

// --- OP15: the container's clock (test_options_monitor_loop.py) --------------------------------

fn at(day: NaiveDate, hh: u32, mm: u32) -> DateTime<FixedOffset> {
    ist()
        .from_local_datetime(&day.and_hms_opt(hh, mm, 0).unwrap())
        .unwrap()
}

fn wed() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, 28).unwrap()
}

#[test]
fn before_the_open_it_waits_for_0914() {
    assert_eq!(L::next_run(at(wed(), 8, 0), false), at(wed(), 9, 14));
}

#[test]
fn inside_the_session_it_starts_at_once() {
    assert_eq!(L::next_run(at(wed(), 11, 30), false), at(wed(), 11, 30));
}

#[test]
fn after_the_close_or_once_run_it_waits_for_the_next_weekday() {
    let thu = wed().succ_opt().unwrap();
    assert_eq!(L::next_run(at(wed(), 15, 45), false), at(thu, 9, 14));
    assert_eq!(L::next_run(at(wed(), 11, 30), true), at(thu, 9, 14));
    let friday = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
    let monday = NaiveDate::from_ymd_opt(2026, 11, 2).unwrap();
    assert_eq!(L::next_run(at(friday, 16, 0), false), at(monday, 9, 14));
}

struct LoopScript {
    moments: std::vec::IntoIter<DateTime<FixedOffset>>,
    codes: std::vec::IntoIter<i32>,
    calls: Vec<Vec<String>>,
    sleeps: Vec<f64>,
}

impl L::LoopSeams for LoopScript {
    fn now(&mut self) -> DateTime<FixedOffset> {
        self.moments.next().unwrap()
    }

    fn sleep(&mut self, seconds: f64) {
        self.sleeps.push(seconds);
    }

    fn call(&mut self, argv: &[String]) -> i32 {
        self.calls.push(argv.to_vec());
        self.codes.next().unwrap()
    }
}

#[test]
fn a_crash_in_session_restarts_and_a_clean_exit_ends_the_day() {
    let mut moments = vec![at(wed(), 10, 0); 3];
    moments.extend(vec![at(wed(), 10, 1); 3]);
    moments.extend(vec![at(wed(), 15, 31); 6]);
    let mut seams = LoopScript {
        moments: moments.into_iter(),
        codes: vec![1, 0].into_iter(),
        calls: vec![],
        sleeps: vec![],
    };
    L::run_forever(&mut seams, "baskfy-desk", Some(2));
    assert_eq!(seams.calls.len(), 2);
    assert_eq!(seams.calls[0][1..], ["service", "options-monitor"]);
    #[allow(clippy::cast_precision_loss)]
    let retry = L::RETRY_SECONDS as f64;
    assert!(
        seams
            .sleeps
            .iter()
            .any(|s| (s - retry).abs() < f64::EPSILON)
    );
}

/// The lines of the block opened by the line `header` (indentation `indent`).
fn block<'t>(text: &'t str, header: &str, indent: usize) -> Vec<&'t str> {
    let mut lines = text.lines().skip_while(|l| *l != header);
    assert!(lines.next().is_some(), "no {header:?} in the file");
    lines
        .take_while(|l| {
            let t = l.trim_start();
            t.is_empty() || t.starts_with('#') || l.len() - t.len() > indent
        })
        .collect()
}

/// A service's `environment:` as `yaml.safe_load` reads this file's shape (its own, or the
/// anchor it merges with `<<:`).
fn service_env(text: &str, service: &str) -> Vec<(String, String)> {
    let own = block(text, &format!("  {service}:"), 2);
    let lines = if own.iter().any(|l| l.trim() == "environment:") {
        own
    } else {
        let anchor = own
            .iter()
            .find_map(|l| l.trim().strip_prefix("<<: *"))
            .unwrap()
            .to_owned();
        block(text, &format!("  {anchor}: &{anchor}"), 2)
    };
    let env_at = lines
        .iter()
        .position(|l| l.trim() == "environment:")
        .unwrap();
    let mut env = Vec::new();
    for line in &lines[env_at + 1..] {
        let t = line.trim_start();
        if t.is_empty() || t.starts_with('#') {
            continue;
        }
        if line.len() - t.len() <= 4 {
            break;
        }
        if let Some((k, v)) = t.split_once(": ") {
            env.push((k.to_owned(), v.trim().trim_matches('"').to_owned()));
        }
    }
    env
}

fn repo_file(rel: &str) -> String {
    std::fs::read_to_string(repo().join(rel)).unwrap()
}

#[test]
fn the_image_carries_the_loop() {
    let text = repo_file("decile-blueprint/infra/docker/Dockerfile.desk");
    assert!(text.contains("options-monitor-loop") && text.contains("scripts.options_monitor_loop"));
}

#[test]
fn compose_runs_it_from_the_desk_block_with_money_flags_false() {
    let text = repo_file("decile-blueprint/infra/docker/compose.prod.yml");
    let own = block(&text, "  options-monitor:", 2);
    assert!(
        own.iter()
            .any(|l| l.trim() == "command: [options-monitor-loop]")
    );
    let env = service_env(&text, "options-monitor");
    let get = |k: &str| env.iter().find(|(key, _)| key == k).map(|(_, v)| v.clone());
    for sleeve in ["O1M", "O1W", "O2", "O3"] {
        let name = format!("BASKFY_OPTIONS_{sleeve}_EXECUTION_ENABLED");
        assert_eq!(get(&name), Some(format!("${{{name}:-false}}")), "{name}");
    }
    assert_eq!(
        get("BASKFY_OPTIONS_MONITOR_ENABLED").as_deref(),
        Some("${BASKFY_OPTIONS_MONITOR_ENABLED:-false}")
    );
    assert_eq!(get("OPTIONS_ENABLED").as_deref(), Some("false"));
    assert_eq!(get("INTRADAY_ENABLED").as_deref(), Some("false"));
    assert_eq!(
        get("DRY_RUN").as_deref(),
        Some("${BASKFY_DESK_DRY_RUN:-true}")
    );
}

#[test]
fn no_options_auto_execute_variable_anywhere_in_compose() {
    let text = repo_file("decile-blueprint/infra/docker/compose.prod.yml");
    assert!(
        regex::Regex::new(r"OPTIONS\w*AUTO")
            .unwrap()
            .find(&text)
            .is_none()
    );
}

#[test]
fn the_rust_loop_runs_this_binarys_monitor_service() {
    // The Rust twin of the image's loop: `baskfy-desk service options-monitor-loop` runs
    // `baskfy-desk service options-monitor` (R8.20.2).
    let options = src("src/lanes/options.rs");
    assert!(options.contains(r#""options-monitor-loop" =>"#));
    assert_eq!(L::ARGV, ["service", "options-monitor"]);
}
