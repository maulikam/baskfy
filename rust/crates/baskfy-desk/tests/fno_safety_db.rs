//! Lane **fno_exec** (wave D2): the Postgres halves of `tests/test_fno_safety.py` (FO11, the
//! desk's half of the FO safety proof), ported as spec:
//!
//! * `TestTheFlagMatrixThroughTheDesk` — per sleeve (F1N, F1B, F2) and per row of the four
//!   switches (`DRY_RUN` off, `OPTIONS_ENABLED`, `BASKFY_FNO_CARRY_ENABLED`,
//!   `BASKFY_FNO_<F1|F2>_EXECUTION_ENABLED`, with `INTRADAY_ENABLED` on): a plan raised by the
//!   monitor, `POST /fno/execute` through the real route, executor and FO gateway, then the
//!   carry (a night's mark, F2's trail) and the exit (F1's `E - 1` 15:00; F2's roll and stop)
//!   under that row's switches. The LIVE row is `409 LIVE_NOT_BUILT`; a half-flipped row is
//!   refused leg by leg by the gateway; every other row is simulated end to end. The replay
//!   Kite records **no** write and no gateway journal records a broker event.
//! * `TestTheExecuteRouteFuzz` — Hypothesis's 100 examples become 100 draws of a seeded
//!   generator over the same space (plan ids own/foreign/exit/roll/garbage/prefix, confirm
//!   spellings, clocks across the 30-minute window, tampered form fields, a re-post): the
//!   answer is 200 exactly when the spec says the confirm is valid, no order is formed toward
//!   the broker, and a tampered field changes nothing the gateway was asked (NRML on NFO, the
//!   plan's own legs). No new dependency: the generator is a xorshift (DECISIONS-RS R8.FX3).
//!
//! Skipped without `BASKFY_RS_FNO_TEST_PG`.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines,
    clippy::print_stderr
)]

mod fno_world;

use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::Arc;

use axum::body::{Body, to_bytes};
use axum::http::{Request, header};
use baskfy_core::fno::config::{FoSleeve, FoSleeveGroup, group_of};
use baskfy_desk::app::StateParts;
use baskfy_desk::clock::FixedClock;
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::fno::fno_exec::store::FoQuote;
use baskfy_desk::lanes::options::py;
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Db, Param};
use baskfy_desk::{AppState, DeskPaths, build_state};
use chrono::{DateTime, Duration, FixedOffset};
use fno_world::*;
use serde_json::{Value, json};
use tower::ServiceExt as _;

const SLEEVES: [FoSleeve; 3] = [FoSleeve::F1N, FoSleeve::F1B, FoSleeve::F2];

fn underlying(s: FoSleeve) -> &'static str {
    match s {
        FoSleeve::F1B => "BANKNIFTY",
        FoSleeve::F2 => "RELIANCE",
        _ => "NIFTY",
    }
}

/// Journal events that mean the broker was called (or answered). None may appear.
const BROKER_EVENTS: [&str; 11] = [
    "placed",
    "rejected",
    "error",
    "gtt_placed",
    "gtt_modified",
    "gtt_deleted",
    "gtt_error",
    "gtt_modify_error",
    "gtt_delete_error",
    "order_cancelled",
    "order_cancel_error",
];

fn rows4() -> Vec<(bool, bool, bool, bool)> {
    let mut out = Vec::new();
    for a in [false, true] {
        for b in [false, true] {
            for c in [false, true] {
                for d_ in [false, true] {
                    out.push((a, b, c, d_));
                }
            }
        }
    }
    out
}

/// `set_flags(...)` on one settings value: true when the row is LIVE.
fn set_flags(
    s: &mut Settings,
    sleeve: FoSleeve,
    dry_off: bool,
    options: bool,
    carry: bool,
    execution: bool,
    intraday: bool,
) -> bool {
    s.dry_run = !dry_off;
    s.options_enabled = options;
    s.fno_carry_enabled = carry;
    s.intraday_enabled = intraday;
    s.fno_f1_execution_enabled = false;
    s.fno_f2_execution_enabled = false;
    s.fno_f3_execution_enabled = false;
    match group_of(sleeve) {
        FoSleeveGroup::F1 => s.fno_f1_execution_enabled = execution,
        FoSleeveGroup::F2 => s.fno_f2_execution_enabled = execution,
        FoSleeveGroup::F3 => s.fno_f3_execution_enabled = execution,
    }
    dry_off && options && carry && execution
}

fn all_off(s: &mut Settings, dry_run: bool, intraday: bool) {
    set_flags(s, FoSleeve::F1N, !dry_run, false, false, false, intraday);
}

/// The world's books as Kite `quote` entries (`NFO:<symbol>`, depth, last, OI).
fn kite_fixture(books: &[(String, FoQuote)]) -> Value {
    let mut quotes = serde_json::Map::new();
    let f = |d: rust_decimal::Decimal| py::dstr(d).parse::<f64>().unwrap();
    for (sym, q) in books {
        let level = |l: &Vec<baskfy_core::options::execution::Level>| {
            Value::Array(
                l.iter()
                    .map(|x| json!({"price": f(x.price), "quantity": x.quantity}))
                    .collect(),
            )
        };
        quotes.insert(
            format!("NFO:{sym}"),
            json!({
                "last_price": f(q.last.unwrap()), "oi": q.oi.unwrap_or(0),
                "depth": {"buy": level(&q.bids), "sell": level(&q.asks)},
            }),
        );
    }
    json!({"session": {"authed": true}, "routes": {}, "quotes": quotes, "historical": {}})
}

static MIGRATED: tokio::sync::OnceCell<()> = tokio::sync::OnceCell::const_new();
static TURN: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// The desk itself (`Desk(w, monkeypatch)`): its own `AppState` over the bed clone, the sole
/// user the world's, the replay Kite answering the world's books, the clock settable.
struct Desk {
    state: AppState,
    clock: Arc<FixedClock>,
    kite: Arc<ReplayKite>,
    dir: tempfile::TempDir,
}

fn desk_app() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer/app")
}

impl Desk {
    async fn new(
        db: &Db,
        uid: i64,
        books: &[(String, FoQuote)],
        now: DateTime<FixedOffset>,
        settings: Settings,
    ) -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink(desk_app(), dir.path().join("app")).unwrap();
        std::fs::create_dir(dir.path().join("data")).unwrap();
        let fixture = dir.path().join("kite");
        std::fs::create_dir_all(fixture.join("instruments")).unwrap();
        std::fs::write(fixture.join("kite.json"), kite_fixture(books).to_string()).unwrap();
        let kite = Arc::new(ReplayKite::load(&fixture, None, "test-api-key").unwrap());
        let mut settings = settings;
        settings.sole_user_id = uid;
        settings.sole_broker_account_id = 1;
        settings.kite_api_key = "test-api-key".to_owned();
        let clock = Arc::new(FixedClock::new(now));
        let state = build_state(StateParts {
            settings,
            clock: clock.clone(),
            ids: Arc::new(HarnessIds::new("fno-safety-db")),
            db: db.clone(),
            kite: kite.clone(),
            paths: DeskPaths::under(dir.path()),
            harness: None,
        });
        MIGRATED
            .get_or_init(|| async {
                state.db.connect().await.unwrap().migrate().await.unwrap();
            })
            .await;
        Self {
            state,
            clock,
            kite,
            dir,
        }
    }

    /// `client.post("/fno/execute", data=form)`.
    async fn post_form(&self, form: &[(String, String)]) -> (u16, String) {
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
        let body = form
            .iter()
            .map(|(k, v)| format!("{}={}", enc(k), enc(v)))
            .collect::<Vec<_>>()
            .join("&");
        let req = Request::post("/fno/execute")
            .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
            .body(Body::from(body))
            .unwrap();
        let resp = baskfy_desk::lanes::fno::routes()
            .with_state(self.state.clone())
            .oneshot(req)
            .await
            .unwrap();
        let status = resp.status().as_u16();
        let text = String::from_utf8(
            to_bytes(resp.into_body(), usize::MAX)
                .await
                .unwrap()
                .to_vec(),
        )
        .unwrap();
        (status, text)
    }

    async fn post(&self, plan_id: &str) -> (u16, Value) {
        let (s, t) = self
            .post_form(&[
                ("plan_id".into(), plan_id.into()),
                ("confirm".into(), "true".into()),
            ])
            .await;
        (
            s,
            serde_json::from_str(&t).unwrap_or_else(|e| panic!("{s} {t}: {e}")),
        )
    }

    /// Every journal line the desk's own FO gateways wrote.
    fn journal_events(&self) -> Vec<Value> {
        let mut out = Vec::new();
        let mut stack = vec![self.dir.path().join("data")];
        while let Some(d) = stack.pop() {
            for e in std::fs::read_dir(&d).into_iter().flatten().flatten() {
                let p = e.path();
                if p.is_dir() {
                    stack.push(p);
                } else if p.extension().is_some_and(|x| x == "jsonl") {
                    for line in std::fs::read_to_string(&p).unwrap_or_default().lines() {
                        if !line.trim().is_empty() {
                            out.push(serde_json::from_str(line).unwrap());
                        }
                    }
                }
            }
        }
        out
    }
}

/// `raise_plan(w, sleeve)`: the monitor's 09:20 plan for `sleeve`, raised on paper.
async fn raise_plan(w: &mut World, sleeve: FoSleeve) -> (String, DateTime<FixedOffset>) {
    let issued = if group_of(sleeve) == FoSleeveGroup::F2 {
        f2_scan(w).await;
        w.books(vec![(
            FUT_NOV.to_owned(),
            book_last("1399.90", "1400.00", "1400.00"),
        )]);
        at(f2_entry(), 9, 20)
    } else {
        f1_scan_on(w, sleeve.value(), underlying(sleeve)).await;
        w.books(neutral(underlying(sleeve)));
        at(f1_entry(), 9, 20)
    };
    let raised = w.tick(issued).await.raised;
    assert_eq!(raised.len(), 1, "{raised:?}");
    assert_eq!(tcol(&w.plan_row(&raised[0]).await, "status"), "ISSUED");
    (raised[0].clone(), issued)
}

fn all_books(w: &World) -> Vec<(String, FoQuote)> {
    w.quotes
        .0
        .lock()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// `assert_no_broker(w)` over the world's and the desk's journals.
fn assert_no_broker(w: &World, desk: &Desk) -> Vec<Value> {
    assert_eq!(w.broker_calls(), 0);
    assert_eq!(
        desk.kite.write_calls().len(),
        0,
        "{:?}",
        desk.kite.write_calls()
    );
    let mut events = w.journal_events();
    events.extend(desk.journal_events());
    for e in &events {
        let ev = e["event"].as_str().unwrap_or_default();
        assert!(!BROKER_EVENTS.contains(&ev), "{e}");
        if ev == "dry_run" && !e["fo_plan"].is_null() {
            assert_eq!(e["product"], "NRML", "{e}");
        }
    }
    events
}

/// `carry_and_exit(w, sleeve, plan_id, position_id)`: the position's closing reason.
async fn carry_and_exit(w: &mut World, sleeve: FoSleeve, plan_id: &str, pid: i64) -> String {
    if group_of(sleeve) == FoSleeveGroup::F2 {
        land_future(&w.market, f2_entry(), nov(), "1450", "1449");
        let night = w.night(f2_entry()).await;
        assert_eq!(night.marked, 1);
        assert_eq!(
            night.trailed,
            vec!["RELIANCE:1340.00->1390.00:DRY_RUN_GTT_MODIFY".to_owned()]
        );
        w.books(vec![
            (
                FUT_NOV.to_owned(),
                book_last("1430.00", "1430.10", "1430.05"),
            ),
            (
                FUT_DEC.to_owned(),
                book_last("1438.00", "1438.10", "1438.05"),
            ),
        ]);
        assert_eq!(
            w.tick(at(hard_exit(), 15, 0)).await.actions,
            vec![format!("{plan_id}-R1")]
        );
        assert!(is_null(&w.position(pid).await, "closed_at")); // rolled, not closed
        let day = before(dec_(), 10);
        w.books(vec![(
            FUT_DEC.to_owned(),
            book_last("1380.00", "1380.10", "1380.00"),
        )]);
        assert_eq!(
            w.tick(at(day, 10, 0)).await.actions,
            vec![format!("{plan_id}-X")]
        );
    } else {
        let u = underlying(sleeve);
        land_condor(
            &w.market,
            u,
            f1_entry(),
            &[
                ("SHORT_CALL", "58"),
                ("SHORT_PUT", "54"),
                ("LONG_CALL", "12"),
                ("LONG_PUT", "10"),
            ],
        );
        assert_eq!(w.night(f1_entry()).await.marked, 1);
        assert!(w.tick(at(hard_exit(), 14, 59)).await.actions.is_empty());
        assert_eq!(
            w.tick(at(hard_exit(), 15, 0)).await.actions,
            vec![format!("{plan_id}-X")]
        );
    }
    let pos = w.position(pid).await;
    assert!(!is_null(&pos, "closed_at"));
    tcol(&pos, "closed_reason")
}

async fn fresh(db: &Db, prefix: &str) -> Option<World> {
    let mut conn = db.connect().await.ok()?;
    let uid = new_user(&mut conn, prefix).await;
    Some(World::new(conn, uid).await)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_flag_matrix_through_the_desk_reaches_no_broker() {
    if std::env::var(ENV).is_err() {
        eprintln!("{ENV} is not set: skipped");
        return;
    }
    let _turn = TURN.lock().await;
    let worlds = open_db("public").await.unwrap();
    let desks = open_db("desk").await.unwrap();
    for sleeve in SLEEVES {
        for (dry_off, options, carry, execution) in rows4() {
            let row = (sleeve, dry_off, options, carry, execution);
            let mut w = fresh(&worlds, "fo11").await.unwrap();
            let uid = w.uid;
            let task_desks = desks.clone();
            let outcome = tokio::spawn(async move {
                w.set(|s| all_off(s, true, false));
                let (plan_id, issued) = raise_plan(&mut w, sleeve).await;
                let mut s = Settings::defaults().unwrap();
                let live = set_flags(&mut s, sleeve, dry_off, options, carry, execution, true);
                w.set(|ws| {
                    set_flags(ws, sleeve, dry_off, options, carry, execution, true);
                });
                let desk = Desk::new(
                    &task_desks,
                    uid,
                    &all_books(&w),
                    issued + Duration::minutes(2),
                    s,
                )
                .await;
                let (status, body) = desk.post(&plan_id).await;
                if live {
                    assert_eq!(
                        (status, body["code"].as_str().unwrap()),
                        (409, "LIVE_NOT_BUILT"),
                        "{row:?}"
                    );
                    assert!(w.fills(&plan_id).await.is_empty());
                    assert_eq!(tcol(&w.plan_row(&plan_id).await, "status"), "ISSUED");
                } else if execution && !(options && carry) {
                    assert_eq!(status, 200, "{row:?} {body}");
                    assert_eq!(body["outcome"], "ABANDONED_PARTIAL", "{row:?} {body}");
                    let detail = body["detail"].as_str().unwrap_or_default();
                    assert!(
                        detail.contains("OPTIONS_ENABLED") || detail.contains("CARRY"),
                        "{row:?} {body}"
                    );
                    assert!(w.fills(&plan_id).await.is_empty());
                } else {
                    assert_eq!(status, 200, "{row:?} {body}");
                    assert_eq!(body["outcome"], "OPEN", "{row:?} {body}");
                    assert_eq!(body["simulated"], true);
                    let fills = w.fills(&plan_id).await;
                    assert!(!fills.is_empty());
                    assert!(fills.iter().all(|f| py::truthy(col(f, "simulated"))));
                    let ids: BTreeSet<String> = fills
                        .iter()
                        .map(|f| tcol(f, "client_id").split(':').next().unwrap().to_owned())
                        .collect();
                    assert_eq!(ids, BTreeSet::from([plan_id.clone()]));
                    let pid = body["position_id"].as_i64().unwrap();
                    let reason = carry_and_exit(&mut w, sleeve, &plan_id, pid).await;
                    assert_eq!(
                        reason,
                        if sleeve == FoSleeve::F2 {
                            "STOP"
                        } else {
                            "HARD_EXIT"
                        },
                        "{row:?}"
                    );
                    let every = w
                        .rows(
                            "SELECT bool_and(simulated) AS s FROM fo_fill WHERE user_id = ?",
                            &[Param::Int(uid)],
                        )
                        .await;
                    assert!(py::truthy(col(&every[0], "s")));
                }
                assert_no_broker(&w, &desk);
                let _ = desk.clock.clone();
                w
            })
            .await;
            let mut conn = worlds.connect().await.unwrap();
            drop_user(&mut conn, uid).await;
            if let Err(e) = outcome {
                std::panic::resume_unwind(e.into_panic());
            }
        }
    }
}

// --- the fuzz -----------------------------------------------------------------------------------

/// A xorshift64* generator: Hypothesis's strategies as seeded draws (no new dependency).
struct Draw(u64);

impl Draw {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: usize) -> usize {
        usize::try_from(self.next() % (n as u64)).unwrap()
    }
    fn bool(&mut self) -> bool {
        self.next() & 1 == 1
    }
    fn pick<'a>(&mut self, items: &'a [&'a str]) -> &'a str {
        items[self.below(items.len())]
    }
    /// `st.text(max_size=n)`: any code point but a surrogate.
    fn text(&mut self, max: usize) -> String {
        let len = self.below(max + 1);
        let pool: [char; 16] = [
            'a',
            'Z',
            '0',
            '-',
            '_',
            ' ',
            '&',
            '=',
            '%',
            '+',
            '\0',
            'é',
            '€',
            '\u{1F600}',
            '\n',
            ':',
        ];
        (0..len)
            .map(|_| {
                if self.bool() {
                    pool[self.below(pool.len())]
                } else {
                    char::from_u32(0x20 + u32::try_from(self.below(0x2000)).unwrap()).unwrap_or('x')
                }
            })
            .collect()
    }
}

const CONFIRMS: [&str; 12] = [
    "True", "TRUE", "1", "yes", "on", "", "false", " true", "true ", "t", "null", "true\0",
];
const TAMPER_FIELDS: [&str; 14] = [
    "product",
    "exchange",
    "qty",
    "quantity",
    "side",
    "tradingsymbol",
    "sleeve",
    "mode",
    "dry_run",
    "order_type",
    "price",
    "variety",
    "fo_plan",
    "structure",
];
const TAMPER_VALUES: [&str; 23] = [
    "MIS",
    "CNC",
    "NRML",
    "BO",
    "CO",
    "NSE",
    "BFO",
    "MCX",
    "CDS",
    "NFO",
    "999999",
    "-1",
    "SELL",
    "BUY",
    "SGBMAR31",
    "LIVE",
    "false",
    "0",
    "MARKET",
    "F2",
    "F1N",
    "NAKED_SHORT",
    "AMO",
];
const PLAN_CHOICES: [&str; 8] = [
    "own", "own", "own", "foreign", "exit", "roll", "garbage", "prefix",
];

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_execute_route_fuzz_forms_no_order_toward_the_broker() {
    if std::env::var(ENV).is_err() {
        eprintln!("{ENV} is not set: skipped");
        return;
    }
    let _turn = TURN.lock().await;
    let worlds = open_db("public").await.unwrap();
    let desks = open_db("desk").await.unwrap();
    let mut g = Draw(0x00F0_11F0_5AFE_2026);
    for example in 0..100 {
        let sleeve = SLEEVES[g.below(3)];
        let dry_run = g.bool();
        let intraday = g.bool();
        let choice = g.pick(&PLAN_CHOICES).to_owned();
        let garbage = g.text(40);
        let confirm = match g.below(4) {
            0 | 1 => "true".to_owned(),
            2 => g.pick(&CONFIRMS).to_owned(),
            _ => g.text(6),
        };
        let minutes = if g.bool() {
            i64::try_from(g.below(30)).unwrap()
        } else {
            i64::try_from(g.below(121)).unwrap()
        };
        let mut tamper: Vec<(String, String)> = Vec::new();
        for _ in 0..g.below(6) {
            let k = g.pick(&TAMPER_FIELDS).to_owned();
            let v = g.pick(&TAMPER_VALUES).to_owned();
            if let Some(slot) = tamper.iter_mut().find(|(x, _)| *x == k) {
                slot.1 = v;
            } else {
                tamper.push((k, v));
            }
        }
        let twice = g.bool();
        let case = format!(
            "#{example} {sleeve:?} dry={dry_run} intraday={intraday} {choice} confirm={confirm:?} +{minutes}m tamper={tamper:?} twice={twice} garbage={garbage:?}"
        );
        let mut other = fresh(&worlds, "fo11").await.unwrap();
        let mut w = fresh(&worlds, "fo11").await.unwrap();
        let (ouid, uid) = (other.uid, w.uid);
        let task_desks = desks.clone();
        let outcome = tokio::spawn(async move {
            for world in [&other, &w] {
                world.set(|s| all_off(s, dry_run, intraday));
            }
            let (foreign_id, _) = raise_plan(&mut other, sleeve).await;
            let (own_id, issued) = raise_plan(&mut w, sleeve).await;
            let plan_id = match choice.as_str() {
                "own" => own_id.clone(),
                "foreign" => foreign_id.clone(),
                "exit" => format!("{own_id}-X"),
                "roll" => format!("{own_id}-R1"),
                "garbage" => garbage.clone(),
                _ => own_id[..own_id.len() - 1].to_owned(),
            };
            let mut s = Settings::defaults().unwrap();
            all_off(&mut s, dry_run, intraday);
            let now = issued + Duration::minutes(minutes);
            let desk = Desk::new(&task_desks, uid, &all_books(&w), now, s).await;
            let expires = py::aware(col(&w.plan_row(&own_id).await, "expires_at")).unwrap();
            let mut form = tamper.clone();
            form.retain(|(k, _)| k != "plan_id" && k != "confirm");
            form.push(("plan_id".into(), plan_id.clone()));
            form.push(("confirm".into(), confirm.clone()));
            let mut answers = Vec::new();
            for _ in 0..(if twice { 2 } else { 1 }) {
                answers.push(desk.post_form(&form).await);
            }
            let valid = confirm == "true" && plan_id == own_id && now < expires;
            let (status, text) = &answers[0];
            assert!([200, 400, 404, 409, 410].contains(status), "{case}: {status} {text}");
            assert_eq!(*status == 200, valid, "{case}: {status} {text}");
            if valid {
                let body: Value = serde_json::from_str(text).unwrap();
                assert_eq!(body["outcome"], "OPEN", "{case}");
                assert_eq!(body["simulated"], true);
                let fills = w.fills(&own_id).await;
                assert!(!fills.is_empty() && fills.iter().all(|f| py::truthy(col(f, "simulated"))));
                let symbols: BTreeSet<String> = w
                    .rows("SELECT l.tradingsymbol FROM fo_leg l JOIN fo_plan p ON p.id = l.plan_id WHERE p.plan_id = ?", &[Param::Text(own_id.clone())])
                    .await
                    .iter()
                    .map(|r| tcol(r, "tradingsymbol"))
                    .collect();
                let orders: Vec<Value> = desk.journal_events().into_iter().filter(|e| e["event"] == "dry_run").collect();
                assert!(!orders.is_empty(), "{case}");
                assert!(orders.iter().all(|e| symbols.contains(e["symbol"].as_str().unwrap_or_default())), "{case}");
                assert!(orders.iter().all(|e| e["product"] == "NRML"), "{case}: the tamper moved nothing");
            } else {
                assert!(w.fills(&own_id).await.is_empty(), "{case}");
            }
            if twice && valid {
                let (s2, t2) = &answers[1];
                let b2: Value = serde_json::from_str(t2).unwrap();
                assert_eq!((*s2, b2["code"].as_str().unwrap_or_default()), (409, "NOT_ISSUED"), "{case}");
            }
            assert!(other.fills(&foreign_id).await.is_empty());
            assert_eq!(tcol(&other.plan_row(&foreign_id).await, "status"), "ISSUED");
            assert_no_broker(&w, &desk);
            assert_eq!(other.broker_calls(), 0);
            (other, w)
        })
        .await;
        let mut conn = worlds.connect().await.unwrap();
        drop_user(&mut conn, uid).await;
        drop_user(&mut conn, ouid).await;
        if let Err(e) = outcome {
            std::panic::resume_unwind(e.into_panic());
        }
    }
}
