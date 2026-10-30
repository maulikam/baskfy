//! The rest of `tests/test_twt_auto.py`: `TestTheRealPathStillRefuses` over `AutoStore` (the
//! in-memory store) and the real gateway behind the spy — exactly Python's rig, the runner's
//! switch forced on and the sleeve's gates dry — plus `main`, `drain_now`'s shape, and
//! `TestTheLoop` (`scripts/twt_auto_loop.py`, the Dockerfile and compose wiring).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use chrono::{DateTime, Duration, FixedOffset, TimeZone};
use rust_decimal::Decimal;

use super::auto::{self as a, Collaborators, RunReport};
use super::auto_loop as l;
use super::execute::{self as x, ExecOutcome, ExecResult};
use super::memory::{MemoryStore, d, fifty_crore, session, twenty_five_lakh};
use super::rig::{Spy, TAPE, dry, flags};
use crate::gateway::Gateway;
use crate::lanes::twt::common::{Rec, Val};
use crate::lanes::twt::store::TwtStore;
use crate::settings::Settings;

fn ist() -> FixedOffset {
    crate::clock::ist()
}

fn at(y: i32, mo: u32, da: u32, h: u32, mi: u32, s: u32) -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(y, mo, da, h, mi, s).unwrap()
}

/// `OPEN`: Monday 21 Sep 2026, ten seconds past the open — when the loop runs it.
fn open() -> DateTime<FixedOffset> {
    at(2026, 9, 21, 9, 15, 10)
}

/// `BUILT`: 09:05, when `twt-morning` builds the plan.
fn built() -> DateTime<FixedOffset> {
    at(2026, 9, 21, 9, 5, 0)
}

// =============================================================================================
// AutoStore's fixtures
// =============================================================================================

/// `a_plan(store, source=..., built_at=..., ttl_minutes=...)`.
fn a_plan(store: &MemoryStore, source: &str, built_at: DateTime<FixedOffset>) -> (String, i64) {
    let mut m = store.m();
    let pk = i64::try_from(m.plans.len()).unwrap() + 1;
    let plan_id = format!("00000000-0000-4000-8000-{pk:012}");
    m.plans.push(Rec::of([
        ("id", Val::Int(pk)),
        ("plan_id", Val::Str(plan_id.clone())),
        ("session_date", Val::Date(session())),
        ("source", Val::from(source)),
        ("built_at", Val::Stamp(built_at)),
        ("expires_at", Val::Stamp(built_at + Duration::minutes(30))),
        ("gate", Val::from("OPEN")),
    ]));
    (plan_id, pk)
}

/// `a_line(store, plan_id, pk, line_id=..., symbol=...)`: a PROPOSED `BUY_AT_OPEN`.
fn a_line(store: &MemoryStore, plan: &(String, i64), line_id: i64, symbol: &str) {
    store.m().lines.push(Rec::of([
        ("id", Val::Int(line_id)),
        ("plan_pk", Val::Int(plan.1)),
        ("plan_id", Val::Str(plan.0.clone())),
        ("kind", Val::from("BUY_AT_OPEN")),
        ("instrument_id", Val::Int(1000 + line_id)),
        ("symbol", Val::from(symbol)),
        ("quantity", Val::Int(2_500)),
        ("stop_price", Val::Dec(d("80.00"))),
        ("value_inr", Val::Dec(d("250000.00"))),
        ("high_since", Val::None),
        ("previous_trigger", Val::None),
        ("note", Val::from("")),
        ("state", Val::from("PROPOSED")),
        ("client_id", Val::Str(format!("{}:{symbol}", plan.0))),
        ("order_id", Val::None),
        ("position_id", Val::None),
        ("session_date", Val::Date(session())),
        ("turnover_avg_inr", Val::Dec(fifty_crore())),
    ]));
}

/// `_four_buys()`: ₹25 lakh, one MORNING plan built today, four `BUY_AT_OPEN` lines.
fn four_buys() -> (MemoryStore, String) {
    let store = MemoryStore::new(twenty_five_lakh(), 10);
    let plan = a_plan(&store, "MORNING", built());
    for (index, symbol) in ["AAA", "BBB", "CCC", "DDD"].iter().enumerate() {
        a_line(&store, &plan, i64::try_from(index).unwrap() + 1, symbol);
    }
    (store, plan.0)
}

// =============================================================================================
// `_run(store, gateway=gateway)` with the runner forced on and the sleeve's gates dry
// =============================================================================================

struct RealPath {
    store: MemoryStore,
    /// `gateway=gateway`: a fresh spy per run, as Python's `gateway()` builds one per call.
    gateways: Mutex<Vec<Arc<Spy>>>,
}

impl RealPath {
    fn new(store: &MemoryStore) -> Self {
        Self {
            store: store.clone(),
            gateways: Mutex::new(Vec::new()),
        }
    }

    async fn run(&self) -> RunReport {
        a::run_once(self, a::SESSION_WAIT_SECONDS, a::SESSION_POLL_SECONDS)
            .await
            .unwrap()
    }

    fn broker_writes(&self) -> usize {
        self.gateways
            .lock()
            .unwrap()
            .iter()
            .map(|g| g.kite.write_calls().len())
            .sum()
    }
}

#[async_trait]
impl Collaborators for RealPath {
    /// `monkeypatch.setattr(A, "auto_execute_enabled", lambda: True)`: the runner's switch on.
    fn settings(&self) -> Settings {
        flags(false, true, true)
    }
    fn now(&self) -> DateTime<FixedOffset> {
        open()
    }
    async fn open_store(&self) -> Result<Box<dyn TwtStore>, String> {
        Ok(Box::new(self.store.clone()))
    }
    async fn gateway(&self) -> Arc<dyn Gateway> {
        let spy = Arc::new(Spy::new(&dry()).await);
        self.gateways.lock().unwrap().push(spy.clone());
        spy
    }
    async fn price_for(&self, _symbol: &str) -> Option<Decimal> {
        Some(d("100.00"))
    }
    async fn authed(&self) -> bool {
        true
    }
    async fn sleep(&self, _seconds: f64) {}
    /// The real `execute_line` with the sleeve's gates dry: `X.twt_gates().dry_run is True`.
    async fn execute(
        &self,
        store: &mut dyn TwtStore,
        gateway: &dyn Gateway,
        _settings: &Settings,
        plan_id: &str,
        line_id: i64,
        now: DateTime<FixedOffset>,
        last_price: Option<Decimal>,
    ) -> ExecResult<ExecOutcome> {
        let sleeve = dry();
        assert!(x::twt_gates(&sleeve).dry_run);
        x::execute_line(
            store, gateway, &sleeve, plan_id, line_id, "true", now, last_price,
        )
        .await
    }
}

#[tokio::test]
async fn test_the_per_session_cap_still_refuses_a_fourth_entry() {
    let (store, _plan_id) = four_buys();
    let runner = RealPath::new(&store);
    let tape_before = TAPE.lock().unwrap().len();
    let report = runner.run().await;
    let statuses: Vec<&str> = report
        .attempts
        .iter()
        .map(|(_, _, s)| s.as_str())
        .collect();
    assert_eq!(
        statuses[..3],
        ["SIMULATED"; 3],
        "{:?}",
        report.attempts
    );
    assert_eq!(statuses[3], "BLOCKED");
    assert!(store.m().line(4).s("note").starts_with("SESSION_CAP"));
    // LV10.2: counted by the day the orders were sent — this morning, not the signal's date
    assert_eq!(store.m().entries_taken(open().date_naive()), 3);
    assert!(
        TAPE.lock().unwrap().len() > tape_before,
        "the real gateway was not exercised"
    );
    assert_eq!(runner.broker_writes(), 0);
}

#[tokio::test]
async fn test_re_running_does_not_double_send() {
    let (store, _plan_id) = four_buys();
    let runner = RealPath::new(&store);
    runner.run().await;
    let (orders, fills) = (store.m().orders.len(), store.m().fills.len());
    let journalled = runner.gateways.lock().unwrap()[0].journal_lines();
    let again = runner.run().await;
    assert!(again.attempts.is_empty(), "{:?}", again.attempts);
    assert_eq!(
        (store.m().orders.len(), store.m().fills.len()),
        (orders, fills)
    );
    // `len(SpyGateway.tape)` unchanged: the second run's gateway was asked nothing.
    assert_eq!(runner.gateways.lock().unwrap()[1].journal_lines(), 0);
    assert!(journalled > 0);
    assert_eq!(runner.broker_writes(), 0);
}

#[tokio::test]
async fn test_every_buy_gets_its_stop_in_the_same_request() {
    let (store, _plan_id) = four_buys();
    RealPath::new(&store).run().await;
    let positions = store.m().open_positions();
    assert_eq!(positions.len(), 3);
    assert!(
        positions.iter().all(|p| !p.is_none("gtt_id")),
        "a buy was left without a stop"
    );
}

#[tokio::test]
async fn test_a_halted_sleeve_sends_nothing() {
    let (store, _plan_id) = four_buys();
    let mut handle = store.clone();
    x::halt_sleeve(&mut handle, "true", open() - Duration::seconds(5))
        .await
        .unwrap();
    let report = RealPath::new(&store).run().await;
    assert!(!report.ran);
    assert!(report.reason.starts_with("EXPIRED"), "{report:?}");
    assert!(store.m().orders.is_empty());
}

/// Non-negotiable 6 on the auto path: every order the drain sent carries `plan_id:symbol`.
#[tokio::test]
async fn test_the_drains_orders_carry_plan_id_colon_symbol() {
    let (store, plan_id) = four_buys();
    RealPath::new(&store).run().await;
    let ids: Vec<String> = store
        .m()
        .orders
        .iter()
        .map(|o| o.s("client_id"))
        .collect();
    assert_eq!(
        ids,
        ["AAA", "BBB", "CCC"]
            .iter()
            .map(|s| format!("{plan_id}:{s}"))
            .collect::<Vec<_>>()
    );
}

// =============================================================================================
// `main()` and `drain_now()`
// =============================================================================================

struct Desk {
    state: crate::app::AppState,
    kite: Arc<crate::fake_kite::ReplayKite>,
    _dir: tempfile::TempDir,
}

/// A desk over a SQLite file with **no tables** (a read of a plan would raise "no such table")
/// and a replay Kite whose session is `authed`.
async fn desk(settings: Settings, authed: bool) -> Desk {
    use crate::app::{DeskPaths, StateParts, build_state};
    let dir = tempfile::tempdir().unwrap();
    let mut settings = settings;
    settings.db_backend = "sqlite".to_owned();
    settings.db_path = dir.path().join("empty.db").display().to_string();
    settings.kite_api_key = "replay-test-key".to_owned();
    settings.token_file = dir.path().join("no-token.json").display().to_string();
    std::fs::write(
        dir.path().join("kite.json"),
        format!(r#"{{"session": {{"authed": {authed}}}, "routes": {{}}, "quotes": {{}}}}"#),
    )
    .unwrap();
    let kite = Arc::new(
        crate::fake_kite::ReplayKite::load(dir.path(), None, &settings.kite_api_key).unwrap(),
    );
    let db = crate::storage::Db::open(crate::storage::DbConfig::from_settings(&settings, None))
        .await
        .unwrap();
    let root =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer");
    let state = build_state(StateParts {
        settings,
        clock: Arc::new(crate::clock::FixedClock::new(open())),
        ids: Arc::new(crate::ids::HarnessIds::new("twt-auto-tests")),
        db,
        kite: kite.clone(),
        paths: DeskPaths {
            root: dir.path().to_path_buf(),
            templates: root.join("app").join("templates"),
            static_dir: root.join("app").join("static"),
        },
        harness: None,
    });
    Desk {
        state,
        kite,
        _dir: dir,
    }
}

#[tokio::test]
async fn test_main_exits_zero_without_reading_a_plan() {
    let desk = desk(dry(), true).await;
    assert_eq!(a::main(&desk.state).await, Ok(()));
    // Nothing was asked of the broker either: not even the session check.
    assert!(desk.kite.calls().is_empty(), "{:?}", desk.kite.calls());
}

#[tokio::test]
async fn test_drain_now_is_run_once_without_a_wait() {
    // The supervisor's entry point: no sleep, no wait, the same refusals. With the three flags
    // on and no Kite session, `run_once(wait_seconds=0, poll_seconds=0)` gives up at once —
    // it neither sleeps nor reads a plan (the database has no tables).
    let later = open() + Duration::hours(1) + Duration::minutes(1);
    let desk = desk(flags(false, true, true), false).await;
    let started = std::time::Instant::now();
    let report = a::drain_now(&desk.state, Some(later)).await.unwrap();
    assert!(started.elapsed() < std::time::Duration::from_secs(5));
    assert!(!report.ran);
    assert_eq!(report.reason, "no Kite session after 0s");
    // `seen["now"]() == OPEN + 1h1m`: the collaborators' clock is the moment handed in.
    let c = a::DeskCollaborators::new(desk.state.clone(), Some(later));
    assert_eq!(c.now(), later);
}

// =============================================================================================
// The clock: scripts/twt_auto_loop.py, and the wiring that runs it
// =============================================================================================

#[test]
fn test_before_the_open_it_waits_for_nine_fifteen_ten() {
    let early = at(2026, 9, 21, 8, 0, 0);
    assert_eq!(l::next_run(early, false), at(2026, 9, 21, 9, 15, 10));
}

#[test]
fn test_inside_the_window_it_runs_now() {
    let late = at(2026, 9, 21, 9, 20, 0);
    assert_eq!(l::next_run(late, false), late);
}

#[test]
fn test_after_the_window_it_waits_for_the_next_weekday() {
    let friday = at(2026, 9, 18, 10, 0, 0);
    assert_eq!(l::next_run(friday, false), at(2026, 9, 21, 9, 15, 10));
}

#[test]
fn test_once_it_has_run_today_it_waits_for_tomorrow() {
    let late = at(2026, 9, 21, 9, 20, 0);
    assert_eq!(l::next_run(late, true), at(2026, 9, 22, 9, 15, 10));
}

#[test]
fn test_a_weekend_waits_for_monday() {
    let saturday = at(2026, 9, 19, 9, 20, 0);
    assert_eq!(l::next_run(saturday, false), at(2026, 9, 21, 9, 15, 10));
}

#[test]
fn test_run_forever_calls_the_module_once_per_day() {
    let mut clock = vec![
        at(2026, 9, 21, 9, 20, 0),
        at(2026, 9, 21, 9, 20, 0),
        at(2026, 9, 21, 9, 21, 0),
    ]
    .into_iter();
    let mut calls: Vec<Vec<String>> = Vec::new();
    let mut printed: Vec<String> = Vec::new();
    l::run_forever(
        &mut || clock.next().unwrap(),
        &mut |_s| {},
        &mut |argv| {
            calls.push(argv.iter().map(|s| (*s).to_owned()).collect());
            0
        },
        Some(1),
        &mut |line| printed.push(line),
    );
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0], vec!["service", "twt-auto"]);
    assert_eq!(
        printed,
        vec![
            "twt-auto: next run Mon 2026-09-21 09:20:00 IST (wait 0s)",
            "twt-auto: app.twt_auto exited 0",
        ]
    );
}

fn repo_file(relative: &str) -> String {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    std::fs::read_to_string(root.join(relative)).unwrap()
}

#[test]
fn test_the_image_runs_this_module() {
    let text = repo_file("decile-blueprint/infra/docker/Dockerfile.desk");
    assert!(text.contains("RUN cat > /usr/local/bin/twt-auto-loop"));
    assert!(text.contains("exec python -m scripts.twt_auto_loop"));
    let chmod = text.split_once("RUN chmod 0755").unwrap().1;
    assert!(chmod.lines().next().unwrap().contains("/usr/local/bin/twt-auto-loop"));
}

/// The lines of the block opened by the line `header` (indentation `indent`), up to the next
/// line at that indentation or less that is not blank or a comment.
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

/// A service's `environment:` mapping as `yaml.safe_load` reads it for this file's shape: the
/// service's own block, or — with no `environment:` of its own — the one it merges with `<<:`.
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

#[test]
fn test_compose_runs_it_from_the_desk_block_with_the_flag_named_false() {
    let text = repo_file("decile-blueprint/infra/docker/compose.prod.yml");
    assert!(text.contains("  twt-auto:\n    <<: *desk\n    command: [twt-auto-loop]"));
    let env = service_env(&text, "twt-auto");
    let get = |k: &str| env.iter().find(|(key, _)| key == k).map(|(_, v)| v.clone());
    assert_eq!(
        get("BASKFY_TWT_AUTO_EXECUTE").as_deref(),
        Some("${BASKFY_TWT_AUTO_EXECUTE:-false}"),
        "{env:?}"
    );
    assert_eq!(
        get("DRY_RUN").as_deref(),
        Some("${BASKFY_DESK_DRY_RUN:-true}"),
        "{env:?}"
    );
    // The desk's own flag only: the api/worker/beat block does not name it.
    let common = block(&text, "x-python-image: &python-image", 0);
    assert!(!common.iter().any(|l| l.contains("BASKFY_TWT_AUTO_EXECUTE")));
}
