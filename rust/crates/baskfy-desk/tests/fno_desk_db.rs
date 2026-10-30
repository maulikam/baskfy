//! Lane **fno_desk**, the DB-backed halves of the desk's `tests/test_fno_desk.py` (FO8: the
//! route, the confirm, its refusals, the page) and `tests/test_fno_ledger.py` (FO10: a journal
//! row at every close, `04` §7's pauses, never pooled), ported as spec (wave D2).
//!
//! **The world.** Python replays these over FO7's world (`tests/test_fno_monitor.World`), whose
//! monitor raises the 09:20 plans and runs the exits. That half of the monitor belongs to the
//! sibling sub-lane and is not ported, so the rows it writes are seeded here instead — **the
//! rows the real Python monitor, executor and ledger wrote**, recorded by
//! `tools/parity/fno_desk_goldens.py` into `fixtures/fno/goldens.json` (ids and the plan ids'
//! `-u<uid>` remapped to this test's own user). Every assertion of the Python tests is kept;
//! where a Python test also ticks the monitor, the monitor's *input* from this lane is asserted
//! instead (`entry_block`'s reasons, the plan the page shows) and the test says so. On top,
//! what Rust writes is compared with what Python wrote: the confirm's fills, position and
//! outcome, and every journal row (`write_journal`, `journal_abandoned` + `after_close`).
//!
//! Like Python's `requires_db`, these need a Postgres carrying the `fo_` schema:
//! `BASKFY_RS_FNO_TEST_PG` (a bed clone `rsd_*`, never the template). Without it each test
//! returns early and says so. Every row a test writes hangs off its own `app_user`, deleted at
//! the end (`ON DELETE CASCADE`). Every order goes to the replay Kite, which records each call
//! and sends nothing: each confirm asserts the broker saw no write.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::print_stderr,
    clippy::too_many_lines
)]

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{Request, header};
use baskfy_core::fno::config::{FnoCeilings, FnoConfig, FoSleeve};
use baskfy_desk::app::StateParts;
use baskfy_desk::clock::{FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::fno::desk::{F1_SENTENCE, F2_SENTENCE, reset_badge};
use baskfy_desk::lanes::fno::fno_exec::store::FoStore;
use baskfy_desk::lanes::fno::ledger;
use baskfy_desk::lanes::options::py;
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Conn, Db, DbConfig, Param, Row};
use baskfy_desk::{AppState, DeskPaths, build_state};
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;
use serde_json::{Value, json};
use tower::ServiceExt as _;

const GOLDENS: &str = include_str!("../fixtures/fno/goldens.json");
const ENV: &str = "BASKFY_RS_FNO_TEST_PG";
/// A database the bed does not have (`rsd_nowhere` is never created): every connect refused.
const DEAD_PORT: &str = "/rsd_nowhere";
const DEAD: &str = "postgres://baskfy:rs-test-only@127.0.0.1:55432/rsd_nowhere";

// --- FO7's calendar ---------------------------------------------------------------------------

fn day(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).unwrap()
}

/// FO7's `SESSIONS`: the weekdays of the 130 days from 1 Oct 2026.
fn sessions() -> Vec<NaiveDate> {
    (0..130)
        .map(|i| day(2026, 10, 1) + Duration::days(i))
        .filter(|d| d.weekday().num_days_from_monday() < 5)
        .collect()
}

/// `before(anchor, n)`: the session `n` sessions before `anchor` (`sessions_before`).
fn before(anchor: NaiveDate, n: usize) -> NaiveDate {
    let s = sessions();
    let i = s.iter().position(|d| *d == anchor).unwrap();
    s[i - n]
}

fn nov() -> NaiveDate {
    day(2026, 11, 24)
}

fn f1_entry() -> NaiveDate {
    before(nov(), 15)
}

fn hard_exit() -> NaiveDate {
    before(nov(), 1)
}

fn f2_entry() -> NaiveDate {
    day(2026, 11, 2)
}

fn at(d: NaiveDate, hh: u32, mm: u32) -> DateTime<FixedOffset> {
    ist()
        .with_ymd_and_hms(d.year(), d.month(), d.day(), hh, mm, 0)
        .unwrap()
}

#[test]
fn fo7s_calendar_is_pythons() {
    assert_eq!(f1_entry(), day(2026, 11, 3));
    assert_eq!(hard_exit(), day(2026, 11, 23));
    assert_eq!(before(f1_entry(), 3), day(2026, 10, 29));
}

// --- the database -------------------------------------------------------------------------------

fn url() -> Option<String> {
    let found = std::env::var(ENV).ok();
    if found.is_none() {
        eprintln!("{ENV} is not set: skipped (Python's requires_db)");
    }
    found
}

async fn open_db(url: &str, dir: &std::path::Path) -> Db {
    Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "desk".to_owned(),
        database_url: url.to_owned(),
        db_path: dir.join("unused.db"),
    })
    .await
    .unwrap()
}

async fn connect(url: &str) -> Conn {
    let dir = tempfile::tempdir().unwrap();
    open_db(url, dir.path()).await.connect().await.unwrap()
}

async fn rows(conn: &mut Conn, sql: &str, params: &[Param]) -> Vec<Row> {
    conn.execute(sql, params).await.unwrap().rows
}

/// One column of one row as JSON (a `to_jsonb(...)::text` select).
async fn jrows(conn: &mut Conn, sql: &str, params: &[Param]) -> Vec<Value> {
    rows(conn, sql, params)
        .await
        .iter()
        .map(|r| serde_json::from_str(&py::text(py::col(r, "j"))).unwrap())
        .collect()
}

fn unique_tag() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let n = N.fetch_add(1, Ordering::Relaxed);
    format!(
        "{:016x}{:08x}{:08x}",
        (nanos as u64) ^ n.rotate_left(40),
        std::process::id(),
        n
    )
}

/// FO7's `user` fixture: a fresh `app_user` (and, unless a scenario seeds its own, the two
/// sleeve rows the fixture inserts).
async fn new_user(conn: &mut Conn, sleeve_rows: bool) -> i64 {
    let tag = unique_tag();
    let r = rows(
        conn,
        "INSERT INTO public.app_user (public_id, email) VALUES (?, ?) RETURNING id",
        &[
            Param::Text(format!("fo8-{}", &tag[..8])),
            Param::Text(format!("fo8-{}@x.test", &tag[8..16])),
        ],
    )
    .await;
    let uid = py::int(py::col(&r[0], "id"));
    if sleeve_rows {
        rows(
            conn,
            "INSERT INTO public.fo_sleeve_config (user_id, sleeve, capital_inr) VALUES \
             (?, 'F1', 2500000), (?, 'F2', 0)",
            &[Param::Int(uid), Param::Int(uid)],
        )
        .await;
    }
    uid
}

async fn drop_user(conn: &mut Conn, uid: i64) {
    rows(
        conn,
        "DELETE FROM public.app_user WHERE id = ?",
        &[Param::Int(uid)],
    )
    .await;
}

// --- the goldens: Python's rows, seeded under this test's user --------------------------------

fn scenario(name: &str) -> Value {
    let all: Value = serde_json::from_str(GOLDENS).unwrap();
    all["scenarios"][name].clone()
}

/// `-u<old>` → `-u<new>` where no digit follows (plan ids, client ids, roll ids).
fn remap_uid(text: &str, old: i64, new: i64) -> String {
    let needle = format!("-u{old}");
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(i) = rest.find(&needle) {
        let after = &rest[i + needle.len()..];
        out.push_str(&rest[..i]);
        if after.chars().next().is_some_and(|c| c.is_ascii_digit()) {
            out.push_str(&needle);
        } else {
            out.push_str(&format!("-u{new}"));
        }
        rest = after;
    }
    out.push_str(rest);
    out
}

#[test]
fn the_uid_remap_touches_only_whole_suffixes() {
    assert_eq!(
        remap_uid("F1N-20261103-NIFTY-u9:X -u91 -u9-R1", 9, 4),
        "F1N-20261103-NIFTY-u4:X -u91 -u4-R1"
    );
}

#[derive(Debug, Default)]
struct Seeded {
    uid: i64,
    old_uid: i64,
    scans: HashMap<i64, i64>,
    plans: HashMap<i64, i64>,
    legs: HashMap<i64, i64>,
    positions: HashMap<i64, i64>,
}

impl Seeded {
    /// The same user read against another scenario's golden (another world, the same inputs).
    fn against(&self, name: &str) -> Self {
        Self {
            uid: self.uid,
            old_uid: scenario(name)["uid"].as_i64().unwrap(),
            ..Self::default()
        }
    }

    fn plan_id(&self, golden: &str) -> String {
        remap_uid(golden, self.old_uid, self.uid)
    }

    fn position(&self, old: i64) -> i64 {
        self.positions[&old]
    }

    /// A golden row, remapped to this user (ids that are foreign keys mapped; `id`, `user_id`
    /// and the `drop` keys removed), for comparison with what Rust wrote.
    fn golden(&self, raw: &str, drop: &[&str]) -> Value {
        let mut v: Value = serde_json::from_str(&remap_uid(raw, self.old_uid, self.uid)).unwrap();
        let obj = v.as_object_mut().unwrap();
        for k in ["id", "user_id"].iter().chain(drop.iter()) {
            obj.remove(*k);
        }
        v
    }
}

/// What to leave out of a seed.
#[derive(Debug, Clone, Copy, Default)]
struct Skip {
    /// `fo_journal` (the test writes it).
    journal: bool,
    /// Positions, marks and the journal, with every fill unattached (before `journal_abandoned`).
    positions: bool,
}

/// Seed scenario `name`'s rows under `uid`, foreign keys remapped.
async fn seed(conn: &mut Conn, uid: i64, name: &str, skip: Skip) -> Seeded {
    let sc = scenario(name);
    let old_uid = sc["uid"].as_i64().unwrap();
    let mut s = Seeded {
        uid,
        old_uid,
        ..Seeded::default()
    };
    // Positions before plans: an exit's `detail.position_id` names its position.
    for table in [
        "fo_scan",
        "fo_sleeve_config",
        "fo_book_config",
        "fo_position",
        "fo_plan",
        "fo_leg",
        "fo_fill",
        "fo_mark",
        "fo_journal",
        "fo_config_audit",
    ] {
        if (skip.journal || skip.positions) && table == "fo_journal" {
            continue;
        }
        if skip.positions && matches!(table, "fo_position" | "fo_mark") {
            continue;
        }
        for raw in sc["rows"][table].as_array().unwrap() {
            let text = remap_uid(raw.as_str().unwrap(), old_uid, uid);
            let mut row: Value = serde_json::from_str(&text).unwrap();
            let obj = row.as_object_mut().unwrap();
            let old_id = obj.remove("id").and_then(|v| v.as_i64());
            obj.insert("user_id".into(), json!(uid));
            match table {
                "fo_plan" => {
                    if let Some(detail) = obj.get_mut("detail").and_then(Value::as_object_mut) {
                        for (key, map) in [("scan_id", &s.scans), ("position_id", &s.positions)] {
                            if let Some(old) = detail.get(key).and_then(Value::as_i64)
                                && let Some(new) = map.get(&old)
                            {
                                detail.insert(key.into(), json!(new));
                            }
                        }
                    }
                }
                "fo_leg" => {
                    let old = obj["plan_id"].as_i64().unwrap();
                    obj.insert("plan_id".into(), json!(s.plans[&old]));
                }
                "fo_fill" => {
                    let old = obj["leg_id"].as_i64().unwrap();
                    obj.insert("leg_id".into(), json!(s.legs[&old]));
                    let pos = obj.get("position_id").and_then(Value::as_i64);
                    let new = match pos {
                        Some(p) if !skip.positions => json!(s.positions[&p]),
                        _ => Value::Null,
                    };
                    obj.insert("position_id".into(), new);
                }
                "fo_mark" | "fo_journal" => {
                    let old = obj["position_id"].as_i64().unwrap();
                    obj.insert("position_id".into(), json!(s.positions[&old]));
                }
                _ => {}
            }
            let cols: Vec<String> = obj.keys().cloned().collect();
            let cols = cols.join(", ");
            let has_id = old_id.is_some();
            let sql = format!(
                "INSERT INTO public.{table} ({cols}) SELECT {cols} FROM \
                 jsonb_populate_record(NULL::public.{table}, ?) RETURNING {}",
                if has_id { "id" } else { "1 AS id" }
            );
            let r = rows(conn, &sql, &[Param::Jsonb(row.to_string())]).await;
            let new_id = py::int(py::col(&r[0], "id"));
            if let Some(old) = old_id {
                let map = match table {
                    "fo_scan" => Some(&mut s.scans),
                    "fo_plan" => Some(&mut s.plans),
                    "fo_leg" => Some(&mut s.legs),
                    "fo_position" => Some(&mut s.positions),
                    _ => None,
                };
                if let Some(m) = map {
                    m.insert(old, new_id);
                }
            }
        }
    }
    s
}

// --- the desk: the lane's routes over a real `AppState` -------------------------------------

/// The page's nav badge reads a process-wide slot; desk tests take turns (Python's tests run
/// one at a time too).
static DESK_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
static MIGRATED: tokio::sync::OnceCell<()> = tokio::sync::OnceCell::const_new();

fn desk_app() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer/app")
}

/// The scenario's quote books as Kite `quote` entries, plus the NIFTY index level.
fn kite_fixture(books: &Value) -> Value {
    let mut quotes = serde_json::Map::new();
    for (sym, q) in books.as_object().unwrap() {
        quotes.insert(format!("NFO:{sym}"), q.clone());
    }
    quotes.insert(
        "NSE:NIFTY 50".into(),
        json!({"instrument_token": 256_265, "last_price": 24_100.0}),
    );
    json!({
        "session": {"authed": true},
        "routes": {"user.profile": {"data": {"user_id": "REPLAY", "user_name": "Replay Desk",
            "email": "replay@example.invalid", "broker": "ZERODHA", "exchanges": ["NSE", "NFO"],
            "products": ["CNC", "NRML"]}}},
        "quotes": quotes,
        "historical": {},
    })
}

struct Desk {
    state: AppState,
    clock: Arc<FixedClock>,
    kite: Arc<ReplayKite>,
    _dir: tempfile::TempDir,
}

impl Desk {
    /// The route's seams pointed at the replay world: the desk's own `AppState` over the bed
    /// clone, the replay Kite answering `books`, the clock at `now`, the sole user `uid`.
    async fn new(
        db_url: &str,
        uid: i64,
        books: &Value,
        now: DateTime<FixedOffset>,
        tweak: impl FnOnce(&mut Settings),
    ) -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink(desk_app(), dir.path().join("app")).unwrap();
        std::fs::create_dir(dir.path().join("data")).unwrap();
        let fixture = dir.path().join("kite");
        std::fs::create_dir_all(fixture.join("instruments")).unwrap();
        std::fs::write(fixture.join("kite.json"), kite_fixture(books).to_string()).unwrap();
        let kite = Arc::new(ReplayKite::load(&fixture, None, "test-api-key").unwrap());
        let mut settings = Settings::defaults().unwrap();
        settings.sole_user_id = uid;
        settings.sole_broker_account_id = 1;
        // A test key, never a real one: the replay Kite answers whatever key it is given.
        settings.kite_api_key = "test-api-key".to_owned();
        tweak(&mut settings);
        let clock = Arc::new(FixedClock::new(now));
        let db = open_db(db_url, dir.path()).await;
        let state = build_state(StateParts {
            settings,
            clock: clock.clone(),
            ids: Arc::new(HarnessIds::new("fno-desk-db")),
            db,
            kite: kite.clone(),
            paths: DeskPaths::under(dir.path()),
            harness: None,
        });
        if !db_url.contains(DEAD_PORT) {
            MIGRATED
                .get_or_init(|| async {
                    state.db.connect().await.unwrap().migrate().await.unwrap();
                })
                .await;
        }
        Self {
            state,
            clock,
            kite,
            _dir: dir,
        }
    }

    fn router(&self) -> Router {
        baskfy_desk::lanes::fno::routes().with_state(self.state.clone())
    }

    async fn send(&self, req: Request<Body>) -> (u16, String) {
        let resp = self.router().oneshot(req).await.unwrap();
        let status = resp.status().as_u16();
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        (status, String::from_utf8(body.to_vec()).unwrap())
    }

    /// `POST /fno/execute`, form `plan_id` and (unless `None`) `confirm`.
    async fn post(&self, plan_id: &str, confirm: Option<&str>) -> (u16, Value) {
        let enc = |s: &str| {
            s.bytes()
                .map(|b| match b {
                    b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' => {
                        (b as char).to_string()
                    }
                    _ => format!("%{b:02X}"),
                })
                .collect::<String>()
        };
        let mut body = format!("plan_id={}", enc(plan_id));
        if let Some(c) = confirm {
            body.push_str(&format!("&confirm={}", enc(c)));
        }
        let req = Request::post("/fno/execute")
            .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
            .body(Body::from(body))
            .unwrap();
        let (status, text) = self.send(req).await;
        let body = serde_json::from_str(&text)
            .unwrap_or_else(|e| panic!("POST /fno/execute: {status} {text:?}: {e}"));
        (status, body)
    }

    /// `GET /fno` (the badge cache reset first, as Python's `Desk.page`).
    async fn page(&self) -> String {
        reset_badge();
        let (status, text) = self
            .send(Request::get("/fno").body(Body::empty()).unwrap())
            .await;
        assert_eq!(status, 200);
        text
    }

    async fn data(&self) -> Value {
        let (status, text) = self
            .send(Request::get("/fno/data").body(Body::empty()).unwrap())
            .await;
        assert_eq!(status, 200);
        serde_json::from_str(&text).unwrap()
    }

    /// The broker saw no write (order or GTT).
    fn assert_no_broker_write(&self) {
        assert_eq!(self.kite.write_calls(), Vec::new());
    }
}

/// `w.fills(plan_id)`: role, side, quantity, price, simulated, client id, by fill id.
async fn fills(conn: &mut Conn, plan_id: &str) -> Vec<Value> {
    jrows(
        conn,
        "SELECT jsonb_build_object('role', l.role, 'side', f.side, 'quantity', f.quantity, \
         'price', f.price, 'simulated', f.simulated, 'client_id', f.client_id)::text AS j \
         FROM public.fo_fill f JOIN public.fo_leg l ON l.id = f.leg_id \
         JOIN public.fo_plan p ON p.id = l.plan_id WHERE p.plan_id = ? ORDER BY f.id",
        &[Param::Text(plan_id.to_owned())],
    )
    .await
}

async fn plan_row(conn: &mut Conn, plan_id: &str) -> Value {
    jrows(
        conn,
        "SELECT to_jsonb(p)::text AS j FROM public.fo_plan p WHERE plan_id = ?",
        &[Param::Text(plan_id.to_owned())],
    )
    .await
    .remove(0)
}

async fn position(conn: &mut Conn, pid: i64) -> Value {
    jrows(
        conn,
        "SELECT to_jsonb(p)::text AS j FROM public.fo_position p WHERE id = ?",
        &[Param::Int(pid)],
    )
    .await
    .remove(0)
}

async fn count(conn: &mut Conn, sql: &str, uid: i64) -> i64 {
    py::int(py::col(&rows(conn, sql, &[Param::Int(uid)]).await[0], "n"))
}

fn d(v: &Value) -> Decimal {
    match v {
        Value::String(s) => s.parse().unwrap(),
        other => other.to_string().parse().unwrap(),
    }
}

/// The golden's fills (Python's confirm), remapped, for comparison with Rust's.
async fn golden_fills(s: &Seeded, name: &str) -> Vec<Value> {
    let sc = scenario(name);
    let legs: HashMap<i64, String> = sc["rows"]["fo_leg"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            let v: Value = serde_json::from_str(r.as_str().unwrap()).unwrap();
            (
                v["id"].as_i64().unwrap(),
                v["role"].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    sc["rows"]["fo_fill"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            let v = s.golden(r.as_str().unwrap(), &[]);
            let role = legs[&serde_json::from_str::<Value>(r.as_str().unwrap()).unwrap()["leg_id"]
                .as_i64()
                .unwrap()]
                .clone();
            json!({"role": role, "side": v["side"], "quantity": v["quantity"],
                   "price": v["price"], "simulated": v["simulated"], "client_id": v["client_id"]})
        })
        .collect()
}

/// The golden's position, remapped, without its id and the timestamps the clock decides.
fn golden_position(s: &Seeded, name: &str) -> Value {
    let sc = scenario(name);
    s.golden(sc["rows"]["fo_position"][0].as_str().unwrap(), &[])
}

/// Every `fo_` row of this user, as Python's world left them in scenario `name` — ids, foreign
/// keys, server timestamps and the paper GTT handle's position id aside. "What Rust wrote is what
/// Python wrote", table by table.
async fn assert_world_is(conn: &mut Conn, s: &Seeded, name: &str) {
    let sc = scenario(name);
    let order = |t: &str| match t {
        "fo_mark" => "position_id, trade_date",
        "fo_journal" => "position_id",
        _ => "id",
    };
    for table in [
        "fo_plan",
        "fo_leg",
        "fo_position",
        "fo_fill",
        "fo_mark",
        "fo_journal",
    ] {
        let ours = jrows(
            conn,
            &format!(
                "SELECT to_jsonb(t)::text AS j FROM public.{table} t WHERE user_id = ? ORDER BY {}",
                order(table)
            ),
            &[Param::Int(s.uid)],
        )
        .await;
        let theirs: Vec<Value> = sc["rows"][table]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| s.golden(r.as_str().unwrap(), &[]))
            .collect();
        let norm = |mut v: Value| {
            let obj = v.as_object_mut().unwrap();
            for k in [
                "id",
                "user_id",
                "leg_id",
                "position_id",
                "created_at",
                "changed_at",
            ] {
                obj.remove(k);
            }
            if table == "fo_leg" {
                obj.remove("plan_id");
            }
            if let Some(d) = obj.get_mut("detail").and_then(Value::as_object_mut) {
                d.remove("scan_id");
                d.remove("position_id");
                if let Some(g) = d.get_mut("gtt")
                    && g.as_str().is_some_and(|x| x.starts_with("PAPER-"))
                {
                    *g = json!("PAPER-<pid>");
                }
            }
            if let Some(g) = obj.get_mut("gtt_id")
                && g.as_str().is_some_and(|x| x.starts_with("PAPER-"))
            {
                *g = json!("PAPER-<pid>");
            }
            v
        };
        let ours: Vec<Value> = ours.into_iter().map(|v| strip(v, &[])).map(norm).collect();
        let theirs: Vec<Value> = theirs.into_iter().map(norm).collect();
        assert_eq!(ours, theirs, "{name}: {table} is what Python wrote");
    }
}

fn strip(mut v: Value, keys: &[&str]) -> Value {
    let obj = v.as_object_mut().unwrap();
    for k in ["id", "user_id"].iter().chain(keys.iter()) {
        obj.remove(*k);
    }
    v
}

fn opt_symbol(role: &str) -> String {
    let (strike, kind) = match role {
        "LONG_CALL" => (25_400, "CE"),
        "SHORT_CALL" => (25_000, "CE"),
        "SHORT_PUT" => (23_000, "PE"),
        _ => (22_600, "PE"),
    };
    format!("NIFTY26NOV{strike}{kind}")
}

// --- test_fno_desk.py: TestTheConfirm ----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn f1_confirm_is_four_simulated_fills_longs_first_and_open() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    let sc = scenario("f1_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f1_entry(), 9, 25), |_| {}).await;
    let (status, body) = desk.post(&plan_id, Some("true")).await;
    assert_eq!(status, 200, "{body}");
    assert_eq!(
        (&body["outcome"], &body["simulated"], &body["plan_id"]),
        (&json!("OPEN"), &json!(true), &json!(plan_id))
    );
    let got = fills(&mut conn, &plan_id).await;
    let roles: Vec<&str> = got.iter().map(|f| f["role"].as_str().unwrap()).collect();
    assert_eq!(roles, ["LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"]);
    assert!(got.iter().all(|f| f["simulated"] == json!(true)));
    let cids: Vec<String> = got
        .iter()
        .map(|f| f["client_id"].as_str().unwrap().to_owned())
        .collect();
    let want: Vec<String> = ["LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"]
        .iter()
        .map(|r| format!("{plan_id}:{}", opt_symbol(r)))
        .collect();
    assert_eq!(cids, want);
    let pid = body["position_id"].as_i64().unwrap();
    let pos = position(&mut conn, pid).await;
    assert_eq!(
        (&pos["closed_at"], &pos["simulated"]),
        (&Value::Null, &json!(true))
    );
    let plan = plan_row(&mut conn, &plan_id).await;
    assert_eq!(plan["status"], json!("OPEN"));
    assert!(!plan["confirmed_at"].is_null());
    desk.assert_no_broker_write();

    // What Python's confirm wrote on the same plan and the same books (f1_open's golden).
    let g = Seeded {
        uid,
        old_uid: scenario("f1_open")["uid"].as_i64().unwrap(),
        ..Seeded::default()
    };
    assert_eq!(got, golden_fills(&g, "f1_open").await);
    assert_eq!(strip(pos, &[]), strip(golden_position(&g, "f1_open"), &[]));
    let mut want_outcome = scenario("f1_open")["outcome"].clone();
    want_outcome["plan_id"] = json!(plan_id);
    want_outcome["position_id"] = json!(pid);
    assert_eq!(body, want_outcome);
    assert_world_is(&mut conn, &s.against("f1_open"), "f1_open").await;
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn f2_confirm_fills_the_future_and_rests_its_gtt_under_a_paper_handle() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f2_raised", Skip::default()).await;
    let sc = scenario("f2_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f2_entry(), 9, 22), |_| {}).await;
    let (status, body) = desk.post(&plan_id, Some("true")).await;
    assert_eq!(status, 200, "{body}");
    let pid = body["position_id"].as_i64().unwrap();
    assert_eq!(body["outcome"], json!("OPEN"));
    assert_eq!(body["detail"]["gtt"], json!(format!("PAPER-{pid}")));
    let pos = position(&mut conn, pid).await;
    assert_eq!(pos["gtt_id"], json!(format!("PAPER-{pid}")));
    assert!(!pos["stop_price"].is_null());
    let got = fills(&mut conn, &plan_id).await;
    assert!(!got.is_empty() && got.iter().all(|f| f["simulated"] == json!(true)));
    assert_eq!(
        got[0]["client_id"],
        json!(format!("{plan_id}:RELIANCE26NOVFUT"))
    );
    desk.assert_no_broker_write();

    let g = Seeded {
        uid,
        old_uid: scenario("f2_open")["uid"].as_i64().unwrap(),
        ..Seeded::default()
    };
    assert_eq!(got, golden_fills(&g, "f2_open").await);
    let mut want_pos = strip(golden_position(&g, "f2_open"), &[]);
    // The paper GTT handle names the position's own id.
    want_pos["gtt_id"] = json!(format!("PAPER-{pid}"));
    assert_eq!(strip(pos, &[]), want_pos);
    let mut want_outcome = scenario("f2_open")["outcome"].clone();
    want_outcome["plan_id"] = json!(plan_id);
    want_outcome["position_id"] = json!(pid);
    want_outcome["detail"]["gtt"] = json!(format!("PAPER-{pid}"));
    assert_eq!(body, want_outcome);
    assert_world_is(&mut conn, &s.against("f2_open"), "f2_open").await;
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn every_flag_false_is_simulated_even_with_dry_run_false() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    let sc = scenario("f1_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f1_entry(), 9, 25), |st| {
        st.dry_run = false;
    })
    .await;
    assert!(!desk.state.settings_snapshot().dry_run);
    let (_, body) = desk.post(&plan_id, Some("true")).await;
    assert_eq!(body["outcome"], json!("OPEN"), "{body}");
    let got = fills(&mut conn, &plan_id).await;
    assert_eq!(got.len(), 4);
    assert!(got.iter().all(|f| f["simulated"] == json!(true)));
    desk.assert_no_broker_write();
    drop_user(&mut conn, uid).await;
}

// --- test_fno_desk.py: TestRefusals ----------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn an_expired_plan_is_refused_410_and_nothing_is_sent() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    let sc = scenario("f1_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f1_entry(), 9, 25), |_| {}).await;
    desk.clock.set(at(f1_entry(), 9, 50)); // expires_at, exactly
    let (status, body) = desk.post(&plan_id, Some("true")).await;
    assert_eq!((status, &body["code"]), (410, &json!("PLAN_EXPIRED")));
    assert!(fills(&mut conn, &plan_id).await.is_empty());
    assert_eq!(
        plan_row(&mut conn, &plan_id).await["status"],
        json!("ISSUED")
    );
    desk.assert_no_broker_write();
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_missing_or_false_confirm_is_400_before_any_read() {
    let Some(_url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    // Python replaces `open_store` with one that raises: any read fails the test. Here the
    // desk's database is one the bed does not have, so a read would be a 500, not a 400.
    let desk = Desk::new(DEAD, 1, &json!({}), at(f1_entry(), 9, 25), |_| {}).await;
    for confirm in [None, Some("false"), Some(""), Some("True"), Some("1")] {
        let (status, body) = desk.post("F1N-20261103-NIFTY-u1", confirm).await;
        assert_eq!(
            (status, &body["code"]),
            (400, &json!("CONFIRM_REQUIRED")),
            "{confirm:?}"
        );
    }
    desk.assert_no_broker_write();
}

#[tokio::test(flavor = "multi_thread")]
async fn an_unknown_plan_is_404() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    let desk = Desk::new(&url, uid, &json!({}), at(f1_entry(), 9, 25), |_| {}).await;
    let (status, body) = desk.post("F1N-19990101-NOPE-u0", Some("true")).await;
    assert_eq!((status, &body["code"]), (404, &json!("UNKNOWN_PLAN")));
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_second_post_of_the_same_plan_sends_nothing() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    let sc = scenario("f1_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f1_entry(), 9, 25), |_| {}).await;
    assert_eq!(
        desk.post(&plan_id, Some("true")).await.1["outcome"],
        json!("OPEN")
    );
    let fill_sql = "SELECT count(*) AS n FROM public.fo_fill WHERE user_id = ?";
    let sent = count(&mut conn, fill_sql, uid).await;
    let (status, again) = desk.post(&plan_id, Some("true")).await;
    assert_eq!((status, &again["code"]), (409, &json!("NOT_ISSUED")));
    assert_eq!(count(&mut conn, fill_sql, uid).await, sent);
    assert_eq!(sent, 4);
    let positions = "SELECT count(*) AS n FROM public.fo_position WHERE user_id = ?";
    assert_eq!(count(&mut conn, positions, uid).await, 1);
    desk.assert_no_broker_write();
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_refused_leg_is_shown_by_its_name() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f2_raised", Skip::default()).await;
    let sc = scenario("f2_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f2_entry(), 9, 22), |_| {}).await;
    // Python swaps in a gateway over a halted `RiskManager()`; the desk's FO gateways share the
    // desk's one risk manager (one account, one loss cap), so the drill halts that one.
    let gw = desk
        .state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<baskfy_desk::lanes::exec_core::desk::DeskGateway>())
        .unwrap();
    gw.risk()
        .await
        .unwrap()
        .kill("FO8 drill halt")
        .await
        .unwrap();
    let (status, body) = desk.post(&plan_id, Some("true")).await;
    assert_eq!(status, 200, "{body}");
    assert_eq!(
        (&body["outcome"], &body["code"]),
        (&json!("ABANDONED_PARTIAL"), &json!("ABANDONED_PARTIAL"))
    );
    let detail = body["detail"].as_str().unwrap();
    assert!(
        detail.contains("RISK_BLOCKED") && detail.contains("KILL SWITCH"),
        "{detail}"
    );
    let plan = plan_row(&mut conn, &plan_id).await;
    assert_eq!(plan["status"], json!("ABANDONED_PARTIAL"));
    assert!(plan["reason"].as_str().unwrap().contains("RISK_BLOCKED"));
    let page = desk.page().await;
    assert!(page.contains("ABANDONED_PARTIAL") && page.contains("KILL SWITCH: FO8 drill halt"));
    assert!(fills(&mut conn, &plan_id).await.is_empty());
    desk.assert_no_broker_write();
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_live_sleeve_is_refused_before_any_order() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    let sc = scenario("f1_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f1_entry(), 9, 25), |st| {
        st.options_enabled = true;
        st.fno_carry_enabled = true;
        st.fno_f1_execution_enabled = true;
        st.dry_run = false;
    })
    .await;
    let (status, body) = desk.post(&plan_id, Some("true")).await;
    assert_eq!((status, &body["code"]), (409, &json!("LIVE_NOT_BUILT")));
    assert!(fills(&mut conn, &plan_id).await.is_empty());
    assert_eq!(
        plan_row(&mut conn, &plan_id).await["status"],
        json!("ISSUED")
    );
    desk.assert_no_broker_write();
    drop_user(&mut conn, uid).await;
}

// --- test_fno_desk.py: TestUnderTheConfirm ---------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn the_monitors_profit_take_under_the_click_is_on_the_page() {
    // Python ticks the monitor to the profit take; the monitor's watch is the sibling's and not
    // ported, so the rows it wrote (profit_take's golden: the EXIT plan and the closed position)
    // are seeded, and the page — this lane's half — must show them as Python's does.
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "profit_take", Skip::default()).await;
    let sc = scenario("profit_take");
    let entry: Value = serde_json::from_str(sc["rows"]["fo_plan"][0].as_str().unwrap()).unwrap();
    let plan_id = s.plan_id(entry["plan_id"].as_str().unwrap());
    let pid =
        s.position(
            serde_json::from_str::<Value>(sc["rows"]["fo_position"][0].as_str().unwrap()).unwrap()
                ["id"]
                .as_i64()
                .unwrap(),
        );
    assert_eq!(
        position(&mut conn, pid).await["closed_reason"],
        json!("PROFIT_TAKE")
    );
    let day = before(hard_exit(), 5);
    let desk = Desk::new(&url, uid, &sc["books"], at(day, 11, 2), |_| {}).await;
    let page = desk.page().await;
    assert!(page.contains(&format!("{plan_id}-X")) && page.contains("PROFIT_TAKE"));
    desk.assert_no_broker_write();
    drop_user(&mut conn, uid).await;
}

// --- test_fno_desk.py: TestThePage -----------------------------------------------------------

fn html_escape_quote(s: &str) -> String {
    s.replace('\'', "&#39;")
}

#[tokio::test(flavor = "multi_thread")]
async fn the_plan_card_the_sentence_the_button_and_paper() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    // `raise_f1(desk, credit="70.00")`: the scan's credit is 70.00 (93.50 on live mids).
    rows(
        &mut conn,
        "UPDATE public.fo_scan SET detail = detail || '{\"credit_points\": \"70.00\"}'::jsonb \
         WHERE user_id = ?",
        &[Param::Int(uid)],
    )
    .await;
    let sc = scenario("f1_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f1_entry(), 9, 25), |_| {}).await;
    let page = desk.page().await;
    assert!(page.contains(&plan_id));
    assert!(page.contains(r#"<button type="submit" class="btn-saffron">Confirm (paper)</button>"#));
    assert!(page.contains(&html_escape_quote(F1_SENTENCE)));
    for sleeve in ["F1N", "F1B", "F2"] {
        assert!(page.contains(&format!(r#"id="fnoMode{sleeve}">{sleeve} PAPER<"#)));
    }
    assert!(page.contains(r#"class="muted fno-countdown""#) && page.contains("25 min left"));
    assert!(page.contains(&format!(
        r#"<span class="pill warn" id="fnoDrift-{plan_id}">drift 33.57 %</span>"#
    )));
    assert!(page.contains("long put → long call → short put → short call (longs first)"));
    assert!(page.contains("within free"));
    let view = desk.data().await;
    let card = &view["plans"][0];
    assert_eq!(card["credit_drift_amber"], json!(true));
    assert_eq!(card["hard_exit_date"], json!("2026-11-23"));
    assert_eq!(card["legs"][0]["oi"], json!(1_000_000));
    assert!(!card["legs"][0]["spread_pct"].is_null());
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn an_expired_plan_has_no_button() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    let sc = scenario("f1_raised");
    let desk = Desk::new(&url, uid, &sc["books"], at(f1_entry(), 9, 50), |_| {}).await;
    let page = desk.page().await;
    assert!(
        !page.contains(r#"<button type="submit" class="btn-saffron">Confirm (paper)</button>"#)
    );
    assert!(!page.contains(r#"class="fno-confirm""#));
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn f2_sentence_the_gtt_and_the_red_flag() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f2_raised", Skip::default()).await;
    let sc = scenario("f2_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f2_entry(), 9, 22), |_| {}).await;
    assert!(desk.page().await.contains(&html_escape_quote(F2_SENTENCE)));
    let pid = desk.post(&plan_id, Some("true")).await.1["position_id"]
        .as_i64()
        .unwrap();
    let page = desk.page().await;
    assert!(page.contains(&format!("<code>PAPER-{pid}</code>")));
    assert!(!page.contains(&format!("fnoNaked-{pid}")));
    // `w.store.update_position(pid, gtt_id=None)`.
    rows(
        &mut conn,
        "UPDATE public.fo_position SET gtt_id = NULL WHERE id = ?",
        &[Param::Int(pid)],
    )
    .await;
    assert!(
        desk.page()
            .await
            .contains(&format!(r#"id="fnoNaked-{pid}""#))
    );
    desk.assert_no_broker_write();
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn the_nav_tab_and_its_badge() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    let sc = scenario("f1_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f1_entry(), 9, 25), |_| {}).await;
    let page = desk.page().await;
    assert!(page.contains(r#"href="/fno""#) && page.contains("F&amp;O Overnight"));
    assert!(page.contains(r#"href="/nifty-options""#));
    assert!(!page.contains(r#"id="navFnoBadge""#), "nothing open yet");
    desk.post(&plan_id, Some("true")).await;
    let page = desk.page().await;
    assert!(
        page.contains(r#"id="navFnoBadge">1 open · exit 2026-11-23</span>"#),
        "the badge"
    );
    assert!(page.contains(r#"title="F1N PAPER · F1B PAPER · F2 PAPER · F3N PAPER · F3B PAPER""#));
    drop_user(&mut conn, uid).await;
}

#[tokio::test(flavor = "multi_thread")]
async fn the_view_says_why_it_has_nothing() {
    let Some(_url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    // Python's `open_store` raises `RuntimeError("no database")`; here the database is one
    // the bed does not have, and the page says why in the same words' place.
    let desk = Desk::new(DEAD, 1, &json!({}), at(f1_entry(), 9, 25), |_| {}).await;
    let (status, text) = desk
        .send(Request::get("/fno").body(Body::empty()).unwrap())
        .await;
    assert_eq!(status, 200);
    assert!(text.contains(r#"<p class="muted" id="fnoUnavailable">Unavailable: "#));
}

// --- test_fno_f3.py through this lane's route: the entry, the click on the exit, the add -------

/// Seed `pre`, post `plan` (its golden's `raised[0]`) at `when`, and compare the outcome and every
/// `fo_` row with `post` — what Python's executor and ledger wrote on the same rows and books.
async fn route_matches(pre: &str, post: &str, when: DateTime<FixedOffset>) -> Option<Value> {
    let url = url()?;
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, pre, Skip::default()).await;
    let sc = scenario(pre);
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], when, |_| {}).await;
    let (status, body) = desk.post(&plan_id, Some("true")).await;
    assert_eq!(status, 200, "{body}");
    desk.assert_no_broker_write();
    let g = s.against(post);
    let mut want = scenario(post)["outcome"].clone();
    want["plan_id"] = json!(plan_id);
    want["position_id"] = body["position_id"].clone();
    assert_eq!(body, want, "{post}: the outcome is Python's");
    assert_world_is(&mut conn, &g, post).await;
    drop_user(&mut conn, uid).await;
    Some(body)
}

#[tokio::test(flavor = "multi_thread")]
async fn the_f3_confirm_fills_the_wing_first_and_opens_the_spread() {
    let entry = before(nov(), 12);
    if let Some(body) = route_matches("f3_raised", "f3_open", at(entry, 9, 25)).await {
        assert_eq!(body["outcome"], json!("OPEN"));
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn the_click_sends_the_held_exit_short_first_and_journals_it() {
    let when =
        DateTime::parse_from_rfc3339(scenario("f3_exit_issued")["at"].as_str().unwrap()).unwrap();
    if let Some(body) = route_matches("f3_exit_issued", "f3_exit_clicked", when).await {
        assert_eq!(
            (&body["outcome"], &body["detail"]["reason"]),
            (&json!("CLOSED"), &json!("LEVEL_BREAK"))
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn the_add_is_confirmed_by_the_click_onto_the_open_spread() {
    let when =
        DateTime::parse_from_rfc3339(scenario("f3_add_issued")["at"].as_str().unwrap()).unwrap();
    if let Some(body) = route_matches("f3_add_issued", "f3_add_confirmed", when).await {
        assert_eq!(
            (&body["outcome"], &body["detail"]["lots"]),
            (&json!("OPEN"), &json!(2))
        );
    }
}

// --- test_fno_ledger.py: a journal row at every close -----------------------------------------

/// `assert_in_rupees_and_r(w, pid)`: gross = the fills' signed cash, net = gross − costs, R =
/// net ÷ the position's planned max loss; `simulated`; `detail.costs.total` = `costs_inr`.
async fn assert_in_rupees_and_r(conn: &mut Conn, pid: i64) -> Value {
    let row = jrows(
        conn,
        "SELECT to_jsonb(j)::text AS j FROM public.fo_journal j WHERE position_id = ?",
        &[Param::Int(pid)],
    )
    .await;
    assert_eq!(row.len(), 1, "one journal row for position {pid}");
    let row = row.into_iter().next().unwrap();
    let fills = jrows(
        conn,
        "SELECT to_jsonb(f)::text AS j FROM public.fo_fill f WHERE position_id = ? ORDER BY id",
        &[Param::Int(pid)],
    )
    .await;
    let cash: Decimal = fills
        .iter()
        .map(|f| {
            let sign = if f["side"] == json!("SELL") {
                Decimal::ONE
            } else {
                -Decimal::ONE
            };
            d(&f["price"]) * Decimal::from(f["quantity"].as_i64().unwrap()) * sign
        })
        .sum();
    assert_eq!(d(&row["gross_pnl_inr"]), py::q2(cash));
    assert_eq!(
        d(&row["net_pnl_inr"]),
        d(&row["gross_pnl_inr"]) - d(&row["costs_inr"])
    );
    assert!(d(&row["costs_inr"]) > Decimal::ZERO);
    let r = d(&position(conn, pid).await["max_loss_inr"]);
    assert_eq!(d(&row["risk_budget_inr"]), r);
    assert_eq!(d(&row["r_multiple"]), py::q2(d(&row["net_pnl_inr"]) / r));
    assert_eq!(row["simulated"], json!(true));
    assert_eq!(d(&row["detail"]["costs"]["total"]), d(&row["costs_inr"]));
    row
}

fn quiet(_: &str) {}

/// Seed a close scenario without its journal, write the journal in Rust, and compare it with
/// the row Python wrote; then the Python test's own assertions.
async fn journal_of(name: &str) -> Option<(Conn, i64, Seeded, Value)> {
    let url = url()?;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(
        &mut conn,
        uid,
        name,
        Skip {
            journal: true,
            positions: false,
        },
    )
    .await;
    let sc = scenario(name);
    let golden_journal = sc["rows"]["fo_journal"][0].as_str().unwrap();
    let old_pid = serde_json::from_str::<Value>(golden_journal).unwrap()["position_id"]
        .as_i64()
        .unwrap();
    let pid = s.position(old_pid);
    {
        let mut store = FoStore::new(&mut conn, uid);
        let alert: &(dyn Fn(&str) + Send + Sync) = &quiet;
        let written = ledger::write_journal(&mut store, pid, alert, None, &FnoConfig::default())
            .await
            .unwrap();
        assert!(written.is_some(), "{name}: journalled");
        // Idempotent on position_id: a second write is nothing.
        let again = ledger::write_journal(&mut store, pid, alert, None, &FnoConfig::default())
            .await
            .unwrap();
        assert!(again.is_none(), "{name}: once");
    }
    let row = assert_in_rupees_and_r(&mut conn, pid).await;
    assert_eq!(
        strip(row.clone(), &["position_id"]),
        s.golden(golden_journal, &["position_id"]),
        "{name}: Rust's journal row is Python's"
    );
    Some((conn, uid, s, row))
}

fn costs(row: &Value) -> &Value {
    &row["detail"]["costs"]
}

#[tokio::test]
async fn journal_profit_take() {
    let Some((mut conn, uid, s, row)) = journal_of("profit_take").await else {
        return;
    };
    assert_eq!(
        (&row["closed_reason"], &row["structure"]),
        (&json!("PROFIT_TAKE"), &json!("IRON_CONDOR"))
    );
    assert_eq!(
        (
            &costs(&row)["entry"]["orders"],
            &costs(&row)["exit"]["orders"]
        ),
        (&json!(4), &json!(4))
    );
    assert!(d(&row["net_pnl_inr"]) > Decimal::ZERO && d(&row["r_multiple"]) > Decimal::ZERO);
    let sc = scenario("profit_take");
    let entry: Value = serde_json::from_str(sc["rows"]["fo_plan"][0].as_str().unwrap()).unwrap();
    let plan = plan_row(&mut conn, &s.plan_id(entry["plan_id"].as_str().unwrap())).await;
    assert_eq!(row["sizing_mode"], plan["sizing_mode"]);
    assert!(row["sessions_held"].as_i64().unwrap() >= 1 && row["rolls"] == json!(0));
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn journal_loss_close() {
    let Some((mut conn, uid, _s, row)) = journal_of("loss_close").await else {
        return;
    };
    assert_eq!(row["closed_reason"], json!("LOSS_CLOSE"));
    assert!(d(&row["r_multiple"]) < Decimal::ZERO);
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn journal_hard_exit() {
    let Some((mut conn, uid, _s, row)) = journal_of("hard_exit").await else {
        return;
    };
    assert_eq!(row["closed_reason"], json!("HARD_EXIT"));
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn journal_late_exit_carries_its_violation() {
    let Some((mut conn, uid, _s, row)) = journal_of("late_exit").await else {
        return;
    };
    assert_eq!(row["closed_reason"], json!("LATE_EXIT"));
    let codes: Vec<&Value> = row["detail"]["violations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| &v["code"])
        .collect();
    assert_eq!(codes, [&json!("LATE_EXIT")]);
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn journal_a_roll_then_the_time_exit_journals_the_roll_with_its_own_costs() {
    let Some((mut conn, uid, s, row)) = journal_of("roll_time_exit").await else {
        return;
    };
    assert_eq!(
        (&row["closed_reason"], &row["rolls"], &row["structure"]),
        (&json!("TIME_EXIT"), &json!(1), &json!("FUTURE"))
    );
    let c = costs(&row);
    let rolls = c["rolls"].as_array().unwrap();
    assert_eq!(rolls.len(), 1);
    let sc = scenario("roll_time_exit");
    let entry: Value = serde_json::from_str(sc["rows"]["fo_plan"][0].as_str().unwrap()).unwrap();
    let plan_id = s.plan_id(entry["plan_id"].as_str().unwrap());
    assert_eq!(
        (&rolls[0]["plan_id"], &rolls[0]["costs"]["orders"]),
        (&json!(format!("{plan_id}-R1")), &json!(2))
    );
    assert_eq!(
        d(&rolls[0]["sold_inr"]),
        Decimal::new(143_000, 2) * Decimal::from(500)
    );
    assert_eq!(
        d(&c["total"]),
        d(&c["entry"]["total"]) + d(&c["exit"]["total"]) + d(&rolls[0]["costs"]["total"])
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn journal_a_stop_out() {
    let Some((mut conn, uid, _s, row)) = journal_of("stop").await else {
        return;
    };
    assert_eq!(row["closed_reason"], json!("STOP"));
    assert!(d(&row["r_multiple"]) < Decimal::ZERO);
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn journal_a_naked_future() {
    let Some((mut conn, uid, _s, row)) = journal_of("naked_future").await else {
        return;
    };
    assert_eq!(row["closed_reason"], json!("NAKED_FUTURE"));
    let codes: Vec<&Value> = row["detail"]["violations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| &v["code"])
        .collect();
    assert_eq!(codes, [&json!("NAKED_FUTURE")]);
    drop_user(&mut conn, uid).await;
}

/// `test_an_abandoned_entry_is_journalled_and_carries_nothing`, end to end through the route:
/// the 09:20 plan, a thin long call, the confirm abandons — and FO10.4 journals the filled and
/// closed legs as one position, as Python's world did (abandoned's golden).
#[tokio::test(flavor = "multi_thread")]
async fn an_abandoned_entry_is_journalled_and_carries_nothing() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    let raised = scenario("f1_raised");
    let plan_id = s.plan_id(raised["raised"][0].as_str().unwrap());
    let ab = scenario("abandoned");
    // The thin book Python's world confirmed against (the long call's ask, 20 deep).
    let desk = Desk::new(&url, uid, &ab["books"], at(f1_entry(), 9, 21), |_| {}).await;
    let (status, body) = desk.post(&plan_id, Some("true")).await;
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["outcome"], json!("ABANDONED_PARTIAL"));
    let pos = jrows(
        &mut conn,
        "SELECT to_jsonb(p)::text AS j FROM public.fo_position p WHERE user_id = ?",
        &[Param::Int(uid)],
    )
    .await;
    assert_eq!(pos.len(), 1);
    assert!(!pos[0]["closed_at"].is_null());
    let pid = pos[0]["id"].as_i64().unwrap();
    let row = jrows(
        &mut conn,
        "SELECT to_jsonb(j)::text AS j FROM public.fo_journal j WHERE position_id = ?",
        &[Param::Int(pid)],
    )
    .await;
    assert_eq!(row.len(), 1);
    assert_eq!(row[0]["closed_reason"], json!("ABANDONED_PARTIAL"));
    assert!(
        d(&row[0]["net_pnl_inr"]) < Decimal::ZERO,
        "the round trip's spread and charges"
    );
    let mut store_conn = connect(&url).await;
    let mut store = FoStore::new(&mut store_conn, uid);
    assert!(store.open_positions().await.unwrap().is_empty());
    desk.assert_no_broker_write();

    // Python's rows for the same confirm.
    let g = Seeded {
        uid,
        old_uid: ab["uid"].as_i64().unwrap(),
        ..Seeded::default()
    };
    assert_eq!(
        strip(row[0].clone(), &["position_id"]),
        g.golden(
            ab["rows"]["fo_journal"][0].as_str().unwrap(),
            &["position_id"]
        )
    );
    assert_eq!(
        strip(pos[0].clone(), &[]),
        g.golden(ab["rows"]["fo_position"][0].as_str().unwrap(), &[])
    );
    assert_eq!(
        fills(&mut conn, &plan_id).await,
        golden_fills(&g, "abandoned").await
    );
    assert_eq!(body, {
        let mut o = ab["outcome"].clone();
        o["plan_id"] = json!(plan_id);
        o["code"] = json!("ABANDONED_PARTIAL");
        o["detail"] = body["detail"].clone();
        o
    });
    assert_world_is(&mut conn, &s.against("abandoned"), "abandoned").await;
    drop_user(&mut conn, uid).await;
}

/// The same abandonment, the ledger alone: the plan and its fills as the executor left them
/// (unattached), then `journal_abandoned` + `after_close` — Python's `_journal_abandoned`.
#[tokio::test]
async fn journal_abandoned_then_after_close_is_pythons() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    let s = seed(
        &mut conn,
        uid,
        "abandoned",
        Skip {
            journal: true,
            positions: true,
        },
    )
    .await;
    let ab = scenario("abandoned");
    let plan_id = s.plan_id(ab["raised"][0].as_str().unwrap());
    let when = DateTime::parse_from_rfc3339(ab["at"].as_str().unwrap()).unwrap();
    let alerts = Arc::new(Mutex::new(Vec::<String>::new()));
    let sink = alerts.clone();
    let alert = move |m: &str| sink.lock().unwrap().push(m.to_owned());
    let pid = {
        let mut store = FoStore::new(&mut conn, uid);
        let plan = store.plan(&plan_id).await.unwrap().unwrap();
        let pid = ledger::journal_abandoned(&mut store, &plan, when)
            .await
            .unwrap()
            .unwrap();
        let pauses = ledger::after_close(&mut store, pid, when, &alert).await;
        assert!(pauses.is_empty());
        pid
    };
    assert!(
        alerts.lock().unwrap().is_empty(),
        "{:?}",
        alerts.lock().unwrap()
    );
    let row = assert_in_rupees_and_r(&mut conn, pid).await;
    assert_eq!(
        strip(row, &["position_id"]),
        s.golden(
            ab["rows"]["fo_journal"][0].as_str().unwrap(),
            &["position_id"]
        )
    );
    assert_eq!(
        strip(position(&mut conn, pid).await, &[]),
        s.golden(ab["rows"]["fo_position"][0].as_str().unwrap(), &[])
    );
    drop_user(&mut conn, uid).await;
}

// --- test_fno_ledger.py: the pauses -----------------------------------------------------------

/// `closed(w, sleeve, r, inr=, simulated=, on=)`: one closed position and its journal row.
async fn closed(
    conn: &mut Conn,
    uid: i64,
    sleeve: &str,
    r: &str,
    inr: Option<&str>,
    simulated: bool,
    on: Option<NaiveDate>,
) -> i64 {
    let on = on.unwrap_or_else(|| before(f1_entry(), 3));
    let symbol = match sleeve {
        "F1N" => "NIFTY",
        "F1B" => "BANKNIFTY",
        _ => "RELIANCE",
    };
    let structure = if sleeve == "F2" {
        "FUTURE"
    } else {
        "IRON_CONDOR"
    };
    let at_close = at(on, 15, 0);
    let pid = py::int(py::col(
        &rows(
            conn,
            "INSERT INTO public.fo_position (user_id, sleeve, symbol, structure, entry_plan_id, \
             legs, lots, lot_size, max_loss_inr, opened_at, closed_at, closed_reason, simulated) \
             VALUES (?, ?, ?, ?, ?, '{}', 1, 65, 10000, ?, ?, 'LOSS_CLOSE', ?) RETURNING id",
            &[
                Param::Int(uid),
                Param::Text(sleeve.to_owned()),
                Param::Text(symbol.to_owned()),
                Param::Text(structure.to_owned()),
                Param::Text(format!("seed-{sleeve}-{r}-{on}")),
                Param::Aware(at_close),
                Param::Aware(at_close),
                Param::Bool(simulated),
            ],
        )
        .await[0],
        "id",
    ));
    let rr: Decimal = r.parse().unwrap();
    let net = inr.map_or_else(|| rr * Decimal::from(10_000), |x| x.parse().unwrap());
    rows(
        conn,
        "INSERT INTO public.fo_journal (position_id, user_id, sleeve, symbol, structure, \
         opened_on, closed_on, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, \
         risk_budget_inr, r_multiple, closed_reason, sessions_held, simulated, sizing_mode) \
         VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 10000, ?, 'LOSS_CLOSE', 1, ?, 'BUDGET')",
        &[
            Param::Int(pid),
            Param::Int(uid),
            Param::Text(sleeve.to_owned()),
            Param::Text(symbol.to_owned()),
            Param::Text(structure.to_owned()),
            Param::Date(on),
            Param::Date(on),
            Param::Decimal(net),
            Param::Decimal(net),
            Param::Decimal(rr),
            Param::Bool(simulated),
        ],
    )
    .await;
    pid
}

async fn audits(conn: &mut Conn, uid: i64, key: &str) -> Vec<Value> {
    jrows(
        conn,
        "SELECT to_jsonb(a)::text AS j FROM public.fo_config_audit a WHERE user_id = ? \
         AND key = ? ORDER BY id",
        &[Param::Int(uid), Param::Text(key.to_owned())],
    )
    .await
}

async fn apply(
    conn: &mut Conn,
    uid: i64,
    simulated: bool,
    today: NaiveDate,
) -> Vec<(String, String)> {
    let mut store = FoStore::new(conn, uid);
    ledger::apply_pauses(
        &mut store,
        simulated,
        today,
        &FnoConfig::default(),
        &FnoCeilings::default(),
    )
    .await
    .unwrap()
    .iter()
    .map(|p| (p.scope.clone(), p.reason()))
    .collect()
}

async fn block(
    conn: &mut Conn,
    uid: i64,
    sleeve: FoSleeve,
    day: NaiveDate,
    simulated: bool,
) -> Vec<String> {
    let mut store = FoStore::new(conn, uid);
    ledger::entry_block(
        &mut store,
        sleeve,
        day,
        simulated,
        &FnoConfig::default(),
        &FnoCeilings::default(),
    )
    .await
    .unwrap()
}

async fn column(conn: &mut Conn, uid: i64, sql: &str) -> Value {
    jrows(conn, sql, &[Param::Int(uid)]).await.remove(0)
}

#[tokio::test]
async fn three_f1_closes_at_minus_0_6r_refuse_that_underlyings_next_entry() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    for (i, r) in ["-0.60", "-0.73", "-0.60"].iter().enumerate() {
        closed(
            &mut conn,
            uid,
            "F1N",
            r,
            None,
            true,
            Some(before(f1_entry(), 5 - i)),
        )
        .await;
    }
    let found = apply(&mut conn, uid, true, f1_entry()).await;
    assert_eq!(found, [("F1N".to_owned(), "PAPER:F1_LOSS_RUN".to_owned())]);
    assert_eq!(audits(&mut conn, uid, "pause:F1N").await.len(), 1);
    apply(&mut conn, uid, true, f1_entry()).await;
    assert_eq!(
        audits(&mut conn, uid, "pause:F1N").await.len(),
        1,
        "once per run"
    );
    // Python then ticks the monitor: the 09:20 plan is REJECTED_PAUSED with this reason. The
    // monitor's raising is the sibling's (not ported); its input from this lane is the block.
    let reasons = block(&mut conn, uid, FoSleeve::F1N, f1_entry(), true).await;
    assert!(reasons.iter().any(|r| r.contains("-0.6R")), "{reasons:?}");
    assert!(
        block(&mut conn, uid, FoSleeve::F1B, f1_entry(), true)
            .await
            .is_empty()
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn minus_0_59r_is_not_the_loss_close() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    for (i, r) in ["-0.60", "-0.59", "-0.73"].iter().enumerate() {
        closed(
            &mut conn,
            uid,
            "F1N",
            r,
            None,
            true,
            Some(before(f1_entry(), 5 - i)),
        )
        .await;
    }
    assert!(
        block(&mut conn, uid, FoSleeve::F1N, f1_entry(), true)
            .await
            .is_empty()
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn a_pause_stops_entries_only() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    // `open_f1(world)`: Python's own open structure (f1_open's golden).
    let s = seed(&mut conn, uid, "f1_open", Skip::default()).await;
    let old_pid = serde_json::from_str::<Value>(
        scenario("f1_open")["rows"]["fo_position"][0]
            .as_str()
            .unwrap(),
    )
    .unwrap()["id"]
        .as_i64()
        .unwrap();
    let pid = s.position(old_pid);
    for i in 0..3 {
        closed(
            &mut conn,
            uid,
            "F1N",
            "-0.7",
            None,
            true,
            Some(before(f1_entry(), 3 - i)),
        )
        .await;
    }
    assert!(
        !block(&mut conn, uid, FoSleeve::F1N, hard_exit(), true)
            .await
            .is_empty()
    );
    // The pause closes nothing: the open structure is untouched (Python then ticks the monitor
    // to its own HARD_EXIT — the sibling's watch, not ported).
    apply(&mut conn, uid, true, hard_exit()).await;
    let pos = position(&mut conn, pid).await;
    assert!(pos["closed_at"].is_null() && pos["closed_reason"].is_null());
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn f2_at_minus_6r_pauses_to_the_month_end_and_is_audited() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    let today = day(2026, 11, 20);
    for (i, r) in ["-2", "-2", "-2"].iter().enumerate() {
        let on = day(2026, 11, 2 + u32::try_from(i).unwrap());
        closed(&mut conn, uid, "F2", r, None, true, Some(on)).await;
    }
    apply(&mut conn, uid, true, today).await;
    let row = column(
        &mut conn,
        uid,
        "SELECT jsonb_build_object('u', paused_until, 'r', paused_reason)::text AS j \
         FROM public.fo_sleeve_config WHERE user_id = ? AND sleeve = 'F2'",
    )
    .await;
    assert_eq!(row, json!({"u": "2026-11-30", "r": "PAPER:F2_MONTH_R"}));
    let a = audits(&mut conn, uid, "paused_until").await;
    assert_eq!(a.len(), 1);
    assert_eq!(
        (&a[0]["scope"], &a[0]["changed_by"], &a[0]["new_value"]),
        (&json!("F2"), &json!("ledger"), &json!("2026-11-30"))
    );
    apply(&mut conn, uid, true, today).await;
    assert_eq!(
        audits(&mut conn, uid, "paused_until").await.len(),
        1,
        "idempotent"
    );
    assert!(
        !block(&mut conn, uid, FoSleeve::F2, today, true)
            .await
            .is_empty()
    );
    assert!(
        block(&mut conn, uid, FoSleeve::F2, day(2026, 12, 1), true)
            .await
            .is_empty()
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn f2_at_minus_5_99r_does_not() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    for (i, r) in ["-2", "-2", "-1.99"].iter().enumerate() {
        let on = day(2026, 11, 2 + u32::try_from(i).unwrap());
        closed(&mut conn, uid, "F2", r, None, true, Some(on)).await;
    }
    assert!(
        apply(&mut conn, uid, true, day(2026, 11, 20))
            .await
            .is_empty()
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn the_book_at_the_75000_ceiling_when_the_amount_is_0() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    let today = day(2026, 11, 20);
    closed(
        &mut conn,
        uid,
        "F1N",
        "-0.5",
        Some("-74999.99"),
        true,
        Some(day(2026, 11, 2)),
    )
    .await;
    assert!(apply(&mut conn, uid, true, today).await.is_empty());
    closed(
        &mut conn,
        uid,
        "F2",
        "-0.01",
        Some("-0.01"),
        true,
        Some(day(2026, 11, 3)),
    )
    .await;
    let found = apply(&mut conn, uid, true, today).await;
    assert_eq!(
        found,
        [("BOOK".to_owned(), "PAPER:BOOK_MONTH_INR".to_owned())]
    );
    let row = column(
        &mut conn,
        uid,
        "SELECT jsonb_build_object('u', paused_until)::text AS j FROM public.fo_book_config \
         WHERE user_id = ?",
    )
    .await;
    assert_eq!(row, json!({"u": "2026-11-30"}));
    assert!(
        !block(&mut conn, uid, FoSleeve::F1B, today, true)
            .await
            .is_empty()
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn a_set_book_amount_is_the_limit() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    rows(
        &mut conn,
        "INSERT INTO public.fo_book_config (user_id, monthly_pause_inr) VALUES (?, 30000)",
        &[Param::Int(uid)],
    )
    .await;
    closed(
        &mut conn,
        uid,
        "F2",
        "-1",
        Some("-30000.00"),
        true,
        Some(day(2026, 11, 2)),
    )
    .await;
    let found = apply(&mut conn, uid, true, day(2026, 11, 20)).await;
    let scopes: Vec<&str> = found.iter().map(|(s, _)| s.as_str()).collect();
    assert_eq!(scopes, ["BOOK"]);
    drop_user(&mut conn, uid).await;
}

// --- test_fno_ledger.py: never pooled ---------------------------------------------------------

async fn nightly(conn: &mut Conn, uid: i64, today: NaiveDate) -> Vec<(String, bool, String)> {
    let mut store = FoStore::new(conn, uid);
    ledger::nightly_pauses(&mut store, today)
        .await
        .unwrap()
        .iter()
        .map(|p| (p.scope.clone(), p.simulated, p.reason()))
        .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn a_live_loss_never_pauses_paper() {
    let Some(url) = url() else { return };
    let _turn = DESK_LOCK.lock().await;
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, false).await;
    // `open_f1` at the end needs the 09:20 plan: Python's raised rows (and its sleeve rows).
    let s = seed(&mut conn, uid, "f1_raised", Skip::default()).await;
    for i in 0..3 {
        closed(
            &mut conn,
            uid,
            "F1N",
            "-0.9",
            Some("-3000"),
            false,
            Some(before(f1_entry(), 5 - i)),
        )
        .await;
    }
    closed(
        &mut conn,
        uid,
        "F2",
        "-9",
        Some("-90000"),
        false,
        Some(before(f1_entry(), 1)),
    )
    .await;
    let live = nightly(&mut conn, uid, f1_entry()).await;
    let mut got: Vec<(String, bool)> = live.iter().map(|(s, m, _)| (s.clone(), *m)).collect();
    got.sort();
    assert_eq!(
        got,
        [
            ("BOOK".to_owned(), false),
            ("F1N".to_owned(), false),
            ("F2".to_owned(), false)
        ]
    );
    let book = column(
        &mut conn,
        uid,
        "SELECT jsonb_build_object('r', paused_reason)::text AS j FROM public.fo_book_config \
         WHERE user_id = ?",
    )
    .await;
    assert_eq!(book["r"], json!("LIVE:BOOK_MONTH_INR"));
    for sleeve in [FoSleeve::F1N, FoSleeve::F2] {
        assert!(
            block(&mut conn, uid, sleeve, f1_entry(), true)
                .await
                .is_empty()
        );
        assert!(
            !block(&mut conn, uid, sleeve, f1_entry(), false)
                .await
                .is_empty()
        );
    }
    // Paper raises and opens all the same (`open_f1`: the 09:20 plan confirmed at 09:25).
    let sc = scenario("f1_raised");
    let plan_id = s.plan_id(sc["raised"][0].as_str().unwrap());
    let desk = Desk::new(&url, uid, &sc["books"], at(f1_entry(), 9, 25), |_| {}).await;
    let (_, body) = desk.post(&plan_id, Some("true")).await;
    assert_eq!(body["outcome"], json!("OPEN"), "{body}");
    let pos = position(&mut conn, body["position_id"].as_i64().unwrap()).await;
    assert_eq!(pos["simulated"], json!(true));
    desk.assert_no_broker_write();
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn a_paper_loss_never_pauses_live() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    for i in 0..3 {
        closed(
            &mut conn,
            uid,
            "F1N",
            "-0.9",
            Some("-30000"),
            true,
            Some(before(f1_entry(), 5 - i)),
        )
        .await;
    }
    nightly(&mut conn, uid, f1_entry()).await;
    assert!(
        !block(&mut conn, uid, FoSleeve::F1N, f1_entry(), true)
            .await
            .is_empty()
    );
    assert!(
        block(&mut conn, uid, FoSleeve::F1N, f1_entry(), false)
            .await
            .is_empty()
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn a_hand_set_pause_binds_both() {
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    rows(
        &mut conn,
        "UPDATE public.fo_sleeve_config SET paused_until = ?, paused_reason = 'Maulik' \
         WHERE user_id = ? AND sleeve = 'F1'",
        &[Param::Date(f1_entry()), Param::Int(uid)],
    )
    .await;
    for simulated in [true, false] {
        assert!(
            !block(&mut conn, uid, FoSleeve::F1B, f1_entry(), simulated)
                .await
                .is_empty()
        );
    }
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn the_night_records_the_pauses() {
    // Python's `world.night(HARD_EXIT).pauses == ["PAPER:F2_MONTH_R:F2"]`: the night marks (the
    // sibling's, not ported) and then records both modes' pauses — `nightly_pauses`, this lane's.
    let Some(url) = url() else { return };
    let mut conn = connect(&url).await;
    let uid = new_user(&mut conn, true).await;
    for (i, r) in ["-2", "-2", "-2"].iter().enumerate() {
        let on = day(2026, 11, 2 + u32::try_from(i).unwrap());
        closed(&mut conn, uid, "F2", r, None, true, Some(on)).await;
    }
    let found: Vec<String> = nightly(&mut conn, uid, hard_exit())
        .await
        .into_iter()
        .map(|(scope, _, reason)| format!("{reason}:{scope}"))
        .collect();
    assert_eq!(found, ["PAPER:F2_MONTH_R:F2"]);
    drop_user(&mut conn, uid).await;
}
