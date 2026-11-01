//! `tests/test_session_supervisor.py`, ported: the supervisor over fakes — a token arriving is
//! the event. Every branch of `Supervisor::tick` and of the day's clock is a table here.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::float_cmp)]

use std::sync::{Arc, Mutex};

use chrono::{DateTime, Duration, FixedOffset, NaiveDate};

use super::*;
use crate::wire::{PyValue, dict};

fn monday() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 28).unwrap()
}

fn at_on(hhmm: &str, day: NaiveDate) -> DateTime<FixedOffset> {
    let (h, m) = hhmm.split_once(':').unwrap();
    let t = chrono::NaiveTime::from_hms_opt(h.parse().unwrap(), m.parse().unwrap(), 0).unwrap();
    ist()
        .from_local_datetime(&day.and_time(t))
        .single()
        .unwrap()
}

fn at(hhmm: &str) -> DateTime<FixedOffset> {
    at_on(hhmm, monday())
}

#[derive(Default)]
struct WorldState {
    authed: bool,
    mtime: Option<f64>,
    holdings_rows: Vec<PyDict>,
    seeded: Vec<Vec<(String, f64)>>,
    passes: Vec<DateTime<FixedOffset>>,
    beats: Vec<(String, String, String)>,
    reconcile_raises: bool,
    drain: Option<DrainMode>,
    drain_calls: Vec<DateTime<FixedOffset>>,
}

#[derive(Clone, Copy)]
enum DrainMode {
    Idle,
    Ran,
    Raises,
}

#[derive(Clone)]
struct World(Arc<Mutex<WorldState>>);

impl World {
    fn new() -> Self {
        let state = WorldState {
            holdings_rows: vec![
                dict([
                    ("symbol", PyValue::Str("HELD".into())),
                    ("quantity", PyValue::Int(10)),
                    ("last_price", PyValue::Float(100.0)),
                ]),
                dict([
                    ("symbol", PyValue::Str("ZERO".into())),
                    ("quantity", PyValue::Int(0)),
                    ("last_price", PyValue::Float(100.0)),
                ]),
            ],
            ..WorldState::default()
        };
        Self(Arc::new(Mutex::new(state)))
    }
    fn with_drain(mode: DrainMode) -> Self {
        let w = Self::new();
        w.0.lock().unwrap().drain = Some(mode);
        w
    }
    fn set(&self, mtime: f64, authed: bool) {
        let mut s = self.0.lock().unwrap();
        s.mtime = Some(mtime);
        s.authed = authed;
    }
    fn s(&self) -> std::sync::MutexGuard<'_, WorldState> {
        self.0.lock().unwrap()
    }
    fn deps(&self) -> Arc<dyn Deps> {
        Arc::new(self.clone())
    }
    fn has(&self, process: &str, state: &str) -> bool {
        self.s()
            .beats
            .iter()
            .any(|(p, st, _)| p == process && st == state)
    }
}

#[async_trait::async_trait]
impl Deps for World {
    async fn authed(&self) -> Result<bool, DepError> {
        Ok(self.s().authed)
    }
    fn token_mtime(&self) -> Result<Option<f64>, DepError> {
        Ok(self.s().mtime)
    }
    async fn holdings(&self) -> Result<Vec<PyDict>, DepError> {
        Ok(self.s().holdings_rows.clone())
    }
    async fn seed_positions(&self, values: &[(String, f64)]) -> Result<(), DepError> {
        self.s().seeded.push(values.to_vec());
        Ok(())
    }
    async fn reconcile(&self, now: DateTime<FixedOffset>) -> Result<PassOutcome, DepError> {
        let mut s = self.s();
        s.passes.push(now);
        if s.reconcile_raises {
            return Err(DepError::runtime("book unavailable"));
        }
        Ok(PassOutcome {
            ok: true,
            line: format!("reconcile at {}", now.format("%H:%M:%S")),
        })
    }
    async fn beat(
        &self,
        process: &str,
        state: &str,
        detail: &str,
        _now: DateTime<FixedOffset>,
    ) -> Result<(), DepError> {
        self.s()
            .beats
            .push((process.to_owned(), state.to_owned(), detail.to_owned()));
        Ok(())
    }
    fn has_drain(&self) -> bool {
        self.s().drain.is_some()
    }
    async fn drain_twt(&self, now: DateTime<FixedOffset>) -> Result<DrainOutcome, DepError> {
        let mut s = self.s();
        s.drain_calls.push(now);
        match s.drain {
            Some(DrainMode::Raises) => Err(DepError::runtime("desk db away")),
            Some(DrainMode::Ran) => Ok(DrainOutcome {
                ran: true,
                reason: String::new(),
                attempts: 1,
            }),
            _ => Ok(DrainOutcome {
                ran: false,
                reason: "NO_PLAN: no TWT plan exists".to_owned(),
                attempts: 0,
            }),
        }
    }
}

// --- TestTheLiveDrain ---------------------------------------------------------------------------

#[tokio::test]
async fn in_session_it_drains_once_a_minute_not_every_tick() {
    let world = World::with_drain(DrainMode::Idle);
    let mut sup = Supervisor::with_cadence(world.deps(), 10.0, 60.0);
    world.set(1.0, true);
    let start = at("10:00");
    for seconds in (0..130).step_by(10) {
        sup.tick(start + Duration::seconds(seconds)).await;
    }
    let offsets: Vec<i64> = world
        .s()
        .drain_calls
        .iter()
        .map(|c| (*c - start).num_seconds())
        .collect();
    assert_eq!(offsets, vec![0, 60, 120]);
    let s = world.s();
    let idle: Vec<_> = s.beats.iter().filter(|b| b.0 == "twt-auto").collect();
    let last = idle.last().unwrap();
    assert_eq!(last.1, "idle");
    assert!(last.2.contains("NO_PLAN"));
}

#[tokio::test]
async fn a_drain_that_ran_beats_with_its_line_count() {
    let world = World::with_drain(DrainMode::Ran);
    let mut sup = Supervisor::new(world.deps());
    world.set(1.0, true);
    sup.tick(at("11:40")).await;
    assert_eq!(sup.drains, 1);
    assert!(world.s().beats.contains(&(
        "twt-auto".into(),
        "ran".into(),
        "1 line(s) sent through execute_line".into()
    )));
}

#[tokio::test]
async fn outside_the_session_or_without_a_token_it_never_drains() {
    let world = World::with_drain(DrainMode::Idle);
    let mut sup = Supervisor::new(world.deps());
    sup.tick(at("10:00")).await; // no token
    world.set(1.0, true);
    sup.tick(at("08:50")).await; // token, before the open
    sup.tick(at("15:31")).await; // token, after the close
    assert!(world.s().drain_calls.is_empty());
}

#[tokio::test]
async fn no_drain_handed_in_means_nothing_is_called_and_nothing_breaks() {
    let world = World::new();
    let mut sup = Supervisor::new(world.deps());
    world.set(1.0, true);
    assert_eq!(sup.tick(at("10:00")).await, STATE_SESSION);
    assert_eq!(sup.drains, 0);
}

#[tokio::test]
async fn a_drain_that_raises_is_a_heartbeat_not_a_crash() {
    let world = World::with_drain(DrainMode::Raises);
    let mut sup = Supervisor::new(world.deps());
    world.set(1.0, true);
    assert_eq!(sup.tick(at("10:00")).await, STATE_SESSION);
    assert!(world.s().beats.contains(&(
        "twt-auto".into(),
        "error".into(),
        "drain raised RuntimeError".into()
    )));
}

#[test]
fn from_desk_wires_twt_autos_drain() {
    let source = include_str!("supervisor.rs");
    assert!(source.contains("crate::lanes::twt::auto::drain_now(&self.state, Some(now))"));
    // The supervisor adds no flag of its own (non-negotiable 1): it never reads one.
    let code = source.split("#[cfg(test)]").next().unwrap();
    for flag in [
        "auto_execute",
        "twt_execution_enabled",
        "AUTO_EXECUTE",
        "dry_run",
    ] {
        assert!(!code.contains(flag), "the supervisor reads {flag}");
    }
}

// --- TestTheLoginEvent ----------------------------------------------------------------------

#[tokio::test]
async fn no_token_is_waiting_for_login_and_nothing_else_runs() {
    let world = World::new();
    let mut sup = Supervisor::new(world.deps());
    assert_eq!(sup.tick(at("09:20")).await, STATE_WAITING);
    assert!(world.s().passes.is_empty() && world.s().seeded.is_empty());
    assert!(world.has("supervisor", STATE_WAITING));
}

#[tokio::test]
async fn a_token_arriving_seeds_exposure_and_reconciles_at_once_whatever_the_hour() {
    let world = World::new();
    let mut sup = Supervisor::new(world.deps());
    sup.tick(at("08:40")).await;
    world.set(1.0, true);
    let state = sup.tick(at("08:41")).await;
    assert_eq!(sup.logins, 1);
    assert_eq!(
        world.s().seeded,
        vec![vec![("HELD".to_owned(), 1000.0)]],
        "quantity x last price; zero quantities dropped"
    );
    assert_eq!(
        world.s().passes,
        vec![at("08:41")],
        "one recovery pass, even before the open"
    );
    assert_eq!(state, STATE_IDLE);
    assert!(world.has("supervisor", STATE_LOGIN));
}

#[tokio::test]
async fn a_late_login_at_eleven_starts_the_session_work() {
    let world = World::new();
    let mut sup = Supervisor::new(world.deps());
    sup.tick(at("10:59")).await;
    world.set(5.0, true);
    assert_eq!(sup.tick(at("11:00")).await, STATE_SESSION);
    assert_eq!(world.s().passes, vec![at("11:00")]);
}

#[tokio::test]
async fn a_rewritten_token_that_does_not_authenticate_is_not_a_login() {
    let world = World::new();
    let mut sup = Supervisor::new(world.deps());
    world.set(2.0, false); // written, but Kite refuses it (expired at 06:00)
    assert_eq!(sup.tick(at("09:20")).await, STATE_WAITING);
    assert_eq!(sup.logins, 0);
    assert!(world.s().seeded.is_empty());
}

#[tokio::test]
async fn the_same_token_is_one_login_not_one_a_tick() {
    let world = World::new();
    let mut sup = Supervisor::new(world.deps());
    world.set(1.0, true);
    sup.tick(at("09:20")).await;
    sup.tick(at("09:20") + Duration::seconds(10)).await;
    assert_eq!(sup.logins, 1);
    assert_eq!(world.s().seeded.len(), 1);
}

#[tokio::test]
async fn a_second_token_the_same_day_is_a_second_login() {
    let world = World::new();
    let mut sup = Supervisor::new(world.deps());
    world.set(1.0, true);
    sup.tick(at("09:20")).await;
    world.set(2.0, true);
    sup.tick(at("12:00")).await;
    assert_eq!(sup.logins, 2);
}

#[tokio::test]
async fn a_failing_seed_is_logged_and_the_recovery_pass_still_runs() {
    let world = World::new();
    world.s().holdings_rows = vec![dict([
        ("symbol", PyValue::Str("BAD".into())),
        ("quantity", PyValue::Str("ten".into())),
        ("last_price", PyValue::Float(1.0)),
    ])];
    let mut sup = Supervisor::new(world.deps());
    world.set(1.0, true);
    sup.tick(at("10:00")).await;
    assert!(world.s().seeded.is_empty());
    assert_eq!(world.s().passes, vec![at("10:00")]);
}

// --- TestTheReconcilerCadence -----------------------------------------------------------------

#[tokio::test]
async fn in_session_the_reconciler_runs_every_ten_seconds_and_beats() {
    let world = World::new();
    let mut sup = Supervisor::with_cadence(world.deps(), 10.0, DRAIN_EVERY_SECONDS);
    world.set(1.0, true);
    let start = at("10:00");
    for seconds in [0, 5, 10, 15, 20] {
        sup.tick(start + Duration::seconds(seconds)).await;
    }
    let offsets: Vec<i64> = world
        .s()
        .passes
        .iter()
        .map(|p| (*p - start).num_seconds())
        .collect();
    assert_eq!(offsets, vec![0, 10, 20]);
    let s = world.s();
    let rec: Vec<_> = s.beats.iter().filter(|b| b.0 == "reconciler").collect();
    let last = rec.last().unwrap();
    assert_eq!(last.1, "ok");
    assert!(last.2.contains("reconcile at 10:00:20"));
}

#[tokio::test]
async fn outside_the_session_with_a_token_it_idles_and_does_not_reconcile() {
    let world = World::new();
    let mut sup = Supervisor::new(world.deps());
    world.set(1.0, true);
    sup.tick(at("16:00")).await; // the login pass
    sup.tick(at("16:00") + Duration::seconds(10)).await;
    assert_eq!(world.s().passes.len(), 1);
    let s = world.s();
    let last = s.beats.last().unwrap();
    assert_eq!(
        (last.0.as_str(), last.1.as_str()),
        ("supervisor", STATE_IDLE)
    );
}

#[tokio::test]
async fn a_failing_pass_is_a_heartbeat_not_a_crash() {
    let world = World::new();
    world.s().reconcile_raises = true;
    let mut sup = Supervisor::new(world.deps());
    world.set(1.0, true);
    assert_eq!(sup.tick(at("10:00")).await, STATE_SESSION);
    assert!(world.s().beats.contains(&(
        "reconciler".into(),
        "error".into(),
        "RuntimeError: book unavailable".into()
    )));
}

// --- TestTheClock -----------------------------------------------------------------------------

#[test]
fn next_wake_is_now_inside_a_weekday_and_the_next_weekday_morning_outside() {
    let m = monday();
    assert_eq!(next_wake(at("10:00")), at("10:00"));
    assert_eq!(next_wake(at("08:29")), at("08:30"));
    assert_eq!(
        next_wake(at("16:00")),
        at_on("08:30", m + Duration::days(1))
    );
    let friday = m + Duration::days(4);
    assert_eq!(
        next_wake(at_on("16:00", friday)),
        at_on("08:30", m + Duration::days(7))
    );
    let saturday = m + Duration::days(5);
    assert_eq!(
        next_wake(at_on("10:00", saturday)),
        at_on("08:30", m + Duration::days(7))
    );
    // The wake is IST whatever zone `now` arrives in.
    let utc = at("02:00").with_timezone(&FixedOffset::east_opt(0).unwrap());
    assert_eq!(next_wake(utc), at("08:30"));
}

struct FakeClock {
    now: Mutex<DateTime<FixedOffset>>,
    slept: Mutex<Vec<f64>>,
}

#[async_trait::async_trait]
impl WallClock for FakeClock {
    fn now(&self) -> DateTime<FixedOffset> {
        *self.now.lock().unwrap()
    }
    async fn sleep(&self, seconds: f64) {
        self.slept.lock().unwrap().push(seconds);
        let mut n = self.now.lock().unwrap();
        #[allow(clippy::cast_possible_truncation)]
        let ms = (seconds * 1000.0).round() as i64;
        *n += Duration::milliseconds(ms);
    }
}

#[tokio::test]
async fn run_forever_sleeps_to_the_wake_then_ticks_every_ten_seconds() {
    let world = World::new();
    let mut sup = Supervisor::new(world.deps());
    let clock = FakeClock {
        now: Mutex::new(at("08:00")),
        slept: Mutex::new(Vec::new()),
    };
    let ticks = run_forever(&mut sup, &clock, Some(3)).await;
    assert_eq!(ticks, 3);
    let slept = clock.slept.lock().unwrap().clone();
    assert_eq!(slept[0], 1800.0); // 08:00 → 08:30
    assert_eq!(slept[1..].to_vec(), vec![TICK_SECONDS; 3]);
}

// --- TestTheHeartbeatTable --------------------------------------------------------------------

#[tokio::test]
async fn one_row_a_process_upserted() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("desk.db");
    let db = Db::open(crate::storage::DbConfig {
        backend: "sqlite".to_owned(),
        schema: "desk".to_owned(),
        database_url: String::new(),
        db_path: path.clone(),
    })
    .await
    .unwrap();
    {
        let mut conn = db.connect().await.unwrap();
        conn.execute(
            "CREATE TABLE lv_heartbeat (user_id INTEGER, process TEXT, state TEXT, detail TEXT, \
             at TEXT, PRIMARY KEY (user_id, process))",
            &[],
        )
        .await
        .unwrap();
    }
    let beats = PgHeartbeats::new(db.clone(), 1, "");
    beats
        .beat("supervisor", "waiting_for_login", "no token", at("09:00"))
        .await
        .unwrap();
    beats
        .beat("supervisor", "session", "open", at("09:20"))
        .await
        .unwrap();
    beats
        .beat("reconciler", "ok", "x", at("09:20"))
        .await
        .unwrap();
    let mut conn = db.connect().await.unwrap();
    let cur = conn
        .execute(
            "SELECT process, state, at FROM lv_heartbeat ORDER BY process",
            &[],
        )
        .await
        .unwrap();
    let rows: Vec<(String, String)> = cur
        .rows
        .iter()
        .map(|r| {
            (
                r.get("process")
                    .and_then(|v| v.as_str())
                    .unwrap()
                    .to_owned(),
                r.get("state").and_then(|v| v.as_str()).unwrap().to_owned(),
            )
        })
        .collect();
    assert_eq!(
        rows,
        vec![
            ("reconciler".to_owned(), "ok".to_owned()),
            ("supervisor".to_owned(), "session".to_owned())
        ]
    );
}

#[test]
fn heartbeat_detail_is_cut_at_two_thousand_characters() {
    assert_eq!(head(&"é".repeat(2500), 2000).chars().count(), 2000);
    assert_eq!(seed_values(&[]).unwrap(), Vec::<(String, f64)>::new());
}

#[test]
fn the_seed_reads_symbol_as_str_x_or_empty() {
    // `str(row.get("symbol") or "")`: a falsy float is "", a real one is its repr.
    let row = |sym: PyValue| {
        dict([
            ("symbol", sym),
            ("quantity", PyValue::Int(2)),
            ("last_price", PyValue::Float(10.0)),
        ])
    };
    assert!(seed_values(&[row(PyValue::Float(0.0))]).unwrap().is_empty());
    assert_eq!(
        seed_values(&[row(PyValue::Float(1.5))]).unwrap(),
        vec![("1.5".to_owned(), 20.0)]
    );
    assert!(
        seed_values(&[row(PyValue::List(vec![]))])
            .unwrap()
            .is_empty()
    );
}

#[test]
fn a_raising_drain_beats_the_class_python_would_name() {
    assert_eq!(drain_error("ValueError: bad band").kind, "ValueError");
    assert_eq!(drain_error("RuntimeError: x").kind, "RuntimeError");
    assert_eq!(
        drain_error("error returned from database: relation does not exist").kind,
        "DatabaseError"
    );
    assert_eq!(
        drain_error("pool timed out while waiting for an open connection").kind,
        "OperationalError"
    );
    assert_eq!(drain_error("something else").kind, "RuntimeError");
}
