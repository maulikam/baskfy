//! Lane **options**, the DB-backed halves of the desk's `tests/test_options_execute.py`
//! (`TestTheConfirm`, `TestAbandonment`, `TestTheExit`, `TestNoBrokerCall`), ported as spec:
//! a confirm through the real gateway on paper, its refusals before any order, abandoning a thin
//! entry without ever sending a short, and the monitor's exit closing shorts first.
//!
//! Like Python's `requires_db`, these need a Postgres carrying the screener's `op_` schema:
//! `BASKFY_RS_OPTIONS_TEST_PG` (a bed clone `rsd_*`, never the template). Without it each test
//! returns early and says so. Every row a test writes hangs off its own `app_user`, deleted at
//! the end (`ON DELETE CASCADE`, as the Python fixture relies on).
//!
//! Every order goes to the replay Kite, which records each call and sends nothing.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::print_stderr
)]

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, RwLock};

use baskfy_core::options::gating::{Mode, Sleeve};
use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{GatesFn, Gateway};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::exec_core::desk::ShimGateway;
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskLayer, RiskManager};
use baskfy_desk::lanes::options::core::{Level, VenueError};
use baskfy_desk::lanes::options::execute::{
    self, ExecError, ExecOutcome, Quote, QuoteSource, execute_entry,
};
use baskfy_desk::lanes::options::gates;
use baskfy_desk::lanes::options::py;
use baskfy_desk::lanes::options::store::{ExitVerdict, PgOptionsStore};
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Conn, Db, DbConfig, Param, Row};
use chrono::{DateTime, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

const FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;

type Legs = &'static [(&'static str, i64, &'static str, &'static str, i64)];

const CONDOR: Legs = &[
    ("LONG_PUT", 24750, "PE", "BUY", 9301),
    ("LONG_CALL", 25250, "CE", "BUY", 9302),
    ("SHORT_PUT", 24850, "PE", "SELL", 9303),
    ("SHORT_CALL", 25150, "CE", "SELL", 9304),
];
const SPREAD: Legs = &[
    ("LONG_CALL", 25200, "CE", "BUY", 9311),
    ("SHORT_CALL", 25300, "CE", "SELL", 9312),
];

fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, 27).unwrap()
}

fn now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 10, 27, 10, 5, 0).unwrap()
}

fn d(s: &str) -> Decimal {
    s.parse().unwrap()
}

fn book(bid: &str, ask: &str, depth: i64) -> Quote {
    let (b, a) = (d(bid), d(ask));
    let level = |price| {
        if depth > 0 {
            vec![Level {
                price,
                quantity: depth,
            }]
        } else {
            Vec::new()
        }
    };
    Quote {
        bid: b,
        ask: a,
        bids: level(b),
        asks: level(a),
    }
}

fn deep() -> HashMap<i64, Quote> {
    HashMap::from([
        (9301, book("5.00", "5.10", 1300)),
        (9302, book("5.00", "5.10", 1300)),
        (9303, book("20.00", "20.20", 1300)),
        (9304, book("20.00", "20.20", 1300)),
        (9311, book("35.00", "35.20", 1300)),
        (9312, book("5.00", "5.10", 1300)),
    ])
}

struct Quotes(HashMap<i64, Quote>);

#[async_trait::async_trait]
impl QuoteSource for Quotes {
    async fn quote(&self, token: i64) -> Result<Quote, VenueError> {
        self.0
            .get(&token)
            .cloned()
            .ok_or_else(|| VenueError::Other(format!("KeyError: {token}")))
    }
}

fn paper_defaults() -> Settings {
    // Every flag at its shipped default: DRY_RUN on, the options flags off (PAPER).
    Settings::defaults().unwrap()
}

struct Bed {
    _dir: tempfile::TempDir,
    journal: PathBuf,
    kite: Arc<ReplayKite>,
}

impl Bed {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("kite.json"), FIXTURE).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        let journal = dir.path().join("options.jsonl");
        Self {
            _dir: dir,
            journal,
            kite,
        }
    }

    /// `OrderGateway(broker, RiskManager(), gates=lambda: product_gates(sleeve))` — the desk's
    /// shim over the core gateway, so the operator tenant is stamped as on the desk.
    async fn gateway(&self, settings: Settings, sleeve: Sleeve) -> Arc<dyn Gateway> {
        let clock: Arc<dyn Clock> = Arc::new(FixedClock::new(now()));
        let risk: Arc<dyn RiskLayer> = Arc::new(
            RiskManager::new(RiskConfig::default(), clock.clone(), RiskBacking::Memory)
                .await
                .unwrap(),
        );
        let gate_settings = settings.clone();
        let gates: GatesFn = Arc::new(move || gates::product_gates(&gate_settings, sleeve));
        let core = OrderGateway::new(
            self.kite.clone(),
            risk,
            GatewayOptions {
                gates,
                journal_path: self.journal.clone(),
                stop_band: None,
                coverage: None,
                clock,
                ids: Arc::new(HarnessIds::new("options-confirm-db")),
                limits: None,
            },
        )
        .unwrap();
        Arc::new(ShimGateway::new(
            Arc::new(core),
            Arc::new(RwLock::new(settings)),
        ))
    }

    fn broker_calls(&self) -> usize {
        self.kite.write_calls().len()
    }
}

/// A 32-hex tag unique to this process and call (Python's `uuid4().hex`; no new dependency).
fn unique_tag() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let n = N.fetch_add(1, Ordering::Relaxed);
    format!(
        "{:016x}{:08x}{:08x}",
        nanos as u64 ^ n.rotate_left(40),
        std::process::id(),
        n
    )
}

async fn connect() -> Option<Conn> {
    let Ok(url) = std::env::var("BASKFY_RS_OPTIONS_TEST_PG") else {
        eprintln!("BASKFY_RS_OPTIONS_TEST_PG is not set: skipped (Python's requires_db)");
        return None;
    };
    let db = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "public".to_owned(),
        database_url: url,
        db_path: PathBuf::from("unused.db"),
    })
    .await
    .unwrap();
    Some(db.connect().await.unwrap())
}

async fn rows(conn: &mut Conn, sql: &str, params: &[Param]) -> Vec<Row> {
    conn.execute(sql, params).await.unwrap().rows
}

async fn new_user(conn: &mut Conn) -> i64 {
    let tag = unique_tag();
    let r = rows(
        conn,
        "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
        &[
            Param::Text(format!("op10-{}", &tag[..8])),
            Param::Text(format!("op10-{}@x.test", &tag[8..16])),
        ],
    )
    .await;
    py::int(py::col(&r[0], "id"))
}

async fn drop_user(conn: &mut Conn, uid: i64) {
    rows(
        conn,
        "DELETE FROM app_user WHERE id = ?",
        &[Param::Int(uid)],
    )
    .await;
}

/// `seed(conn, uid, sleeve, structure, legs, slot=, expires=)`: one PLANNED session, its ISSUED
/// entry plan and its legs — as the worker writes them.
async fn seed(
    conn: &mut Conn,
    uid: i64,
    sleeve: &str,
    structure: &str,
    legs: &[(&str, i64, &str, &str, i64)],
    slot: Option<&str>,
    expires: Option<DateTime<FixedOffset>>,
) -> String {
    for (_role, strike, kind, _side, token) in legs {
        rows(
            conn,
            "INSERT INTO op_contract (instrument_token, tradingsymbol, underlying, expiry, strike, \
             option_type, lot_size, tick_size, first_seen, last_seen, expired) VALUES \
             (?, ?, 'NIFTY', ?, ?, ?, 65, 0.05, ?, ?, false) ON CONFLICT (instrument_token) DO NOTHING",
            &[
                Param::Int(*token),
                Param::Text(format!("OP10{token}{kind}")),
                Param::Date(day()),
                Param::Int(*strike),
                Param::Text((*kind).to_owned()),
                Param::Date(day()),
                Param::Date(day()),
            ],
        )
        .await;
    }
    let tag = unique_tag();
    let plan_id = format!("{sleeve}-20261027-{}", &tag[..12]);
    let sid = py::int(py::col(
        &rows(
            conn,
            "INSERT INTO op_session (user_id, sleeve, trade_date, expiry_used, mode, state, plan_id) \
             VALUES (?, ?, ?, ?, 'PAPER', 'PLANNED', ?) RETURNING id",
            &[
                Param::Int(uid),
                Param::Text(sleeve.to_owned()),
                Param::Date(day()),
                Param::Date(day()),
                Param::Text(plan_id.clone()),
            ],
        )
        .await[0],
        "id",
    ));
    if let Some(holder) = slot {
        rows(
            conn,
            "INSERT INTO op_session (user_id, sleeve, trade_date, mode, state, slot_holder) \
             VALUES (?, ?, ?, 'PAPER', 'CONFIRMED', ?)",
            &[
                Param::Int(uid),
                Param::Text(holder.to_owned()),
                Param::Date(day()),
                Param::Text(holder.to_owned()),
            ],
        )
        .await;
    }
    let expires_at = expires.unwrap_or_else(|| now() + chrono::Duration::minutes(25));
    let pk = py::int(py::col(
        &rows(
            conn,
            "INSERT INTO op_plan (user_id, plan_id, session_id, sleeve, structure, kind, sizing_mode, \
             issued_at, expires_at, lots, lot_size, risk_budget_inr, status, detail) VALUES \
             (?, ?, ?, ?, ?, 'ENTRY', 'PAPER_ONE_LOT', ?, ?, 1, 65, 2500, 'ISSUED', ?) RETURNING id",
            &[
                Param::Int(uid),
                Param::Text(plan_id.clone()),
                Param::Int(sid),
                Param::Text(sleeve.to_owned()),
                Param::Text(structure.to_owned()),
                Param::Aware(now() - chrono::Duration::minutes(1)),
                Param::Aware(expires_at),
                Param::Jsonb(
                    r#"{"direction": "UP", "gate": {"range_high": "25182", "range_low": "24998"}}"#
                        .to_owned(),
                ),
            ],
        )
        .await[0],
        "id",
    ));
    for (seq, (role, strike, kind, side, token)) in legs.iter().enumerate() {
        rows(
            conn,
            "INSERT INTO op_leg (user_id, plan_id, seq, role, tradingsymbol, instrument_token, strike, \
             option_type, side, quantity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 65)",
            &[
                Param::Int(uid),
                Param::Int(pk),
                Param::Int(i64::try_from(seq).unwrap() + 1),
                Param::Text((*role).to_owned()),
                Param::Text(format!("OP10{token}{kind}")),
                Param::Int(*token),
                Param::Int(*strike),
                Param::Text((*kind).to_owned()),
                Param::Text((*side).to_owned()),
            ],
        )
        .await;
    }
    plan_id
}

/// `orders(conn, plan_id)`: (role, side, client_id, gateway_status, simulated).
async fn orders(conn: &mut Conn, plan_id: &str) -> Vec<(String, String, String, String, bool)> {
    rows(
        conn,
        "SELECT l.role, o.side, o.client_id, o.gateway_status, o.simulated, o.quantity \
         FROM op_order o JOIN op_leg l ON l.id = o.leg_id JOIN op_plan p ON p.id = l.plan_id \
         WHERE p.plan_id = ? ORDER BY o.id",
        &[Param::Text(plan_id.to_owned())],
    )
    .await
    .iter()
    .map(|r| {
        (
            py::text(py::col(r, "role")),
            py::text(py::col(r, "side")),
            py::text(py::col(r, "client_id")),
            py::text(py::col(r, "gateway_status")),
            py::truthy(py::col(r, "simulated")),
        )
    })
    .collect()
}

fn paper_mode(sleeve: Sleeve) -> Mode {
    gates::mode_of(&paper_defaults(), sleeve)
}

async fn confirm(
    conn: &mut Conn,
    uid: i64,
    gw: Arc<dyn Gateway>,
    plan_id: &str,
    quotes: HashMap<i64, Quote>,
) -> Result<ExecOutcome, ExecError> {
    let mut store = PgOptionsStore::new(conn, uid);
    execute_entry(
        &mut store,
        gw,
        &Quotes(quotes),
        plan_id,
        true,
        &paper_mode,
        &now,
    )
    .await
}

fn refused(r: Result<ExecOutcome, ExecError>) -> (u16, String) {
    match r {
        Err(ExecError::Refused(r)) => (r.status, r.code),
        other => panic!("expected a refusal, got {other:?}"),
    }
}

// --- TestTheConfirm -----------------------------------------------------------------------------

#[tokio::test]
async fn an_o1_confirm_is_four_simulated_fills_wings_first_and_open() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(&mut conn, uid, "O1M", "IRON_CONDOR", CONDOR, None, None).await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O1m).await;
    let out = confirm(&mut conn, uid, gw, &plan_id, deep()).await.unwrap();
    assert_eq!(
        (
            out.outcome.as_str(),
            out.session_state.as_str(),
            out.simulated
        ),
        ("OPEN", "OPEN", true)
    );
    let sent = orders(&mut conn, &plan_id).await;
    let roles: Vec<&str> = sent.iter().map(|o| o.0.as_str()).collect();
    assert_eq!(&roles[..2], ["LONG_PUT", "LONG_CALL"], "wings first");
    let mut shorts: Vec<&str> = roles[2..].to_vec();
    shorts.sort_unstable();
    assert_eq!(shorts, ["SHORT_CALL", "SHORT_PUT"]);
    assert!(sent.iter().all(|o| o.3 == "DRY_RUN" && o.4), "{sent:?}");
    assert_eq!(sent[0].2, format!("{plan_id}:OP109301PE"));
    let fills = rows(
        &mut conn,
        "SELECT count(*) AS n, bool_and(f.simulated) AS sim FROM op_fill f JOIN op_leg l \
         ON l.id = f.leg_id JOIN op_plan p ON p.id = l.plan_id WHERE p.plan_id = ?",
        &[Param::Text(plan_id.clone())],
    )
    .await;
    assert_eq!(py::int(py::col(&fills[0], "n")), 4);
    assert!(py::truthy(py::col(&fills[0], "sim")));
    let pos = rows(
        &mut conn,
        "SELECT p.entry_points, p.lots, p.simulated, s.state, s.slot_holder FROM op_position p \
         JOIN op_session s ON s.id = p.session_id WHERE s.plan_id = ?",
        &[Param::Text(plan_id.clone())],
    )
    .await;
    // 2 x 20.00 - 2 x 5.10: the shorts at the bid, the wings at the ask.
    assert_eq!(d(&py::text(py::col(&pos[0], "entry_points"))), d("29.80"));
    assert_eq!(py::text(py::col(&pos[0], "state")), "OPEN");
    assert_eq!(py::text(py::col(&pos[0], "slot_holder")), "O1M");
    assert!(py::truthy(py::col(&pos[0], "simulated")));
    assert_eq!(bed.broker_calls(), 0);
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn an_expired_plan_is_refused_with_410() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(
        &mut conn,
        uid,
        "O2",
        "LONG_OPTION",
        &SPREAD[..1],
        None,
        Some(now()),
    )
    .await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O2).await;
    let r = confirm(&mut conn, uid, gw, &plan_id, deep()).await;
    assert_eq!(refused(r), (410, "PLAN_EXPIRED".to_owned()));
    assert!(orders(&mut conn, &plan_id).await.is_empty());
    assert_eq!(bed.broker_calls(), 0);
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn a_taken_slot_is_refused_with_409() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(
        &mut conn,
        uid,
        "O3A",
        "DEBIT_SPREAD",
        SPREAD,
        Some("O1W"),
        None,
    )
    .await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O3a).await;
    let r = confirm(&mut conn, uid, gw, &plan_id, deep()).await;
    assert_eq!(refused(r), (409, "SLOT_TAKEN".to_owned()));
    assert!(orders(&mut conn, &plan_id).await.is_empty());
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn a_second_confirm_is_refused() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(&mut conn, uid, "O3A", "DEBIT_SPREAD", SPREAD, None, None).await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O3a).await;
    confirm(&mut conn, uid, gw.clone(), &plan_id, deep())
        .await
        .unwrap();
    let sent = orders(&mut conn, &plan_id).await.len();
    let r = confirm(&mut conn, uid, gw, &plan_id, deep()).await;
    assert_eq!(refused(r).0, 409);
    assert_eq!(
        orders(&mut conn, &plan_id).await.len(),
        sent,
        "nothing re-sent"
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn live_is_refused_before_any_order() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(
        &mut conn,
        uid,
        "O2",
        "LONG_OPTION",
        &SPREAD[..1],
        None,
        None,
    )
    .await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O2).await;
    let mut store = PgOptionsStore::new(&mut conn, uid);
    let live = |_s: Sleeve| Mode::Live;
    let r = execute_entry(&mut store, gw, &Quotes(deep()), &plan_id, true, &live, &now).await;
    assert_eq!(refused(r).1, "LIVE_NOT_BUILT");
    assert!(orders(&mut conn, &plan_id).await.is_empty());
    assert_eq!(bed.broker_calls(), 0);
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn confirm_false_is_refused_before_any_read() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(
        &mut conn,
        uid,
        "O2",
        "LONG_OPTION",
        &SPREAD[..1],
        None,
        None,
    )
    .await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O2).await;
    let mut store = PgOptionsStore::new(&mut conn, uid);
    let r = execute_entry(
        &mut store,
        gw,
        &Quotes(deep()),
        &plan_id,
        false,
        &paper_mode,
        &now,
    )
    .await;
    assert_eq!(refused(r), (400, "CONFIRM_REQUIRED".to_owned()));
    assert!(orders(&mut conn, &plan_id).await.is_empty());
    drop_user(&mut conn, uid).await;
}

// --- TestAbandonment ----------------------------------------------------------------------------

#[tokio::test]
async fn thin_long_call_depth_abandons_and_no_short_is_ever_sent() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(&mut conn, uid, "O1W", "IRON_CONDOR", CONDOR, None, None).await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O1w).await;
    let mut thin = deep();
    thin.insert(9302, book("5.00", "5.10", 0));
    let out = confirm(&mut conn, uid, gw, &plan_id, thin).await.unwrap();
    assert_eq!(
        (out.outcome.as_str(), out.session_state.as_str()),
        ("ABANDONED_ENTRY", "CLOSED")
    );
    let sent = orders(&mut conn, &plan_id).await;
    assert!(!sent.iter().any(|o| o.0.starts_with("SHORT")), "{sent:?}");
    let sells: Vec<(&str, &str)> = sent
        .iter()
        .filter(|o| o.1 == "SELL")
        .map(|o| (o.0.as_str(), o.1.as_str()))
        .collect();
    assert_eq!(sells, [("LONG_PUT", "SELL")]);
    let session = rows(
        &mut conn,
        "SELECT state, closed_reason, slot_holder FROM op_session WHERE plan_id = ?",
        &[Param::Text(plan_id.clone())],
    )
    .await;
    assert_eq!(py::text(py::col(&session[0], "state")), "CLOSED");
    assert_eq!(
        py::text(py::col(&session[0], "closed_reason")),
        "ABANDONED_ENTRY"
    );
    assert!(!py::truthy(py::col(&session[0], "slot_holder")));
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn an_o3_partial_long_is_abandoned_and_no_short_sent() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(&mut conn, uid, "O3B", "DEBIT_SPREAD", SPREAD, None, None).await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O3b).await;
    let mut partial = deep();
    partial.insert(9311, book("35.00", "35.20", 20));
    let out = confirm(&mut conn, uid, gw, &plan_id, partial)
        .await
        .unwrap();
    assert_eq!(out.outcome, "ABANDONED_ENTRY");
    assert!(
        !orders(&mut conn, &plan_id)
            .await
            .iter()
            .any(|o| o.0 == "SHORT_CALL")
    );
    let sid = py::int(py::col(
        &rows(
            &mut conn,
            "SELECT id FROM op_session WHERE plan_id = ?",
            &[Param::Text(plan_id.clone())],
        )
        .await[0],
        "id",
    ));
    let mut store = PgOptionsStore::new(&mut conn, uid);
    let mut open = store.net_open(sid).await.unwrap();
    open.sort_unstable();
    assert!(open.iter().all(|(_, q)| *q == 0), "{open:?}");
    assert_eq!(open.len(), 2);
    drop_user(&mut conn, uid).await;
}

// --- TestTheExit --------------------------------------------------------------------------------

#[tokio::test]
async fn the_monitors_exit_closes_shorts_first_and_the_position() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(&mut conn, uid, "O1M", "IRON_CONDOR", CONDOR, None, None).await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O1m).await;
    confirm(&mut conn, uid, gw.clone(), &plan_id, deep())
        .await
        .unwrap();
    let exit_id = {
        let mut store = PgOptionsStore::new(&mut conn, uid);
        let open = store.open_positions(day()).await.unwrap();
        assert_eq!(open.len(), 1);
        let verdict = ExitVerdict {
            code: "PROFIT".to_owned(),
            reason: "RULE".to_owned(),
            value_points: Some(d("14.80")),
            marked_loss_inr: Some(d("-962")),
            stale: false,
        };
        let at = ist().with_ymd_and_hms(2026, 10, 27, 11, 14, 50).unwrap();
        store
            .raise_exit(open[0].session_id, &verdict, at)
            .await
            .unwrap()
            .unwrap()
    };
    let gw_for = {
        let gw = gw.clone();
        move |_s: Sleeve| Some(gw.clone())
    };
    let done = {
        let mut store = PgOptionsStore::new(&mut conn, uid);
        execute::run_pending_exits(&mut store, &gw_for, &Quotes(deep()), &paper_mode, &now)
            .await
            .unwrap()
    };
    assert_eq!(done.len(), 1);
    assert_eq!(
        (done[0].outcome.as_str(), done[0].session_state.as_str()),
        ("FLAT", "CLOSED")
    );
    let closes: Vec<String> = orders(&mut conn, &plan_id)
        .await
        .into_iter()
        .filter(|o| o.2.split(':').nth(2) == Some("CLOSE"))
        .map(|o| o.0)
        .collect();
    assert_eq!(&closes[..2], ["SHORT_CALL", "SHORT_PUT"], "shorts first");
    let mut longs = closes[2..].to_vec();
    longs.sort_unstable();
    assert_eq!(longs, ["LONG_CALL", "LONG_PUT"]);
    let state = rows(
        &mut conn,
        "SELECT s.state, s.closed_reason, p.closed_at IS NOT NULL AS closed, x.status \
         FROM op_session s JOIN op_position p ON p.session_id = s.id \
         JOIN op_plan x ON x.plan_id = ? WHERE s.plan_id = ?",
        &[Param::Text(exit_id), Param::Text(plan_id.clone())],
    )
    .await;
    assert_eq!(py::text(py::col(&state[0], "state")), "CLOSED");
    assert_eq!(py::text(py::col(&state[0], "closed_reason")), "PROFIT");
    assert!(py::truthy(py::col(&state[0], "closed")));
    assert_eq!(py::text(py::col(&state[0], "status")), "CONFIRMED");
    let again = {
        let mut store = PgOptionsStore::new(&mut conn, uid);
        execute::run_pending_exits(&mut store, &gw_for, &Quotes(deep()), &paper_mode, &now)
            .await
            .unwrap()
    };
    assert!(again.is_empty(), "a second sweep finds nothing to do");
    assert_eq!(bed.broker_calls(), 0);
    drop_user(&mut conn, uid).await;
}

// --- TestNoBrokerCall ---------------------------------------------------------------------------

#[tokio::test]
async fn zero_broker_calls_under_both_dry_run_values() {
    let Some(mut conn) = connect().await else {
        return;
    };
    for dry_run in [true, false] {
        let mut s = paper_defaults();
        s.dry_run = dry_run;
        assert!(!s.options_o3_execution_enabled);
        let uid = new_user(&mut conn).await;
        let plan_id = seed(&mut conn, uid, "O3A", "DEBIT_SPREAD", SPREAD, None, None).await;
        let bed = Bed::new();
        let gw = bed.gateway(s.clone(), Sleeve::O3a).await;
        let mut store = PgOptionsStore::new(&mut conn, uid);
        let modes = |sl: Sleeve| gates::mode_of(&s, sl);
        let out = execute_entry(
            &mut store,
            gw,
            &Quotes(deep()),
            &plan_id,
            true,
            &modes,
            &now,
        )
        .await
        .unwrap();
        assert_eq!(out.outcome, "OPEN");
        assert_eq!(bed.broker_calls(), 0, "dry_run={dry_run}");
        assert!(
            orders(&mut conn, &plan_id)
                .await
                .iter()
                .all(|o| o.3 == "DRY_RUN")
        );
        drop_user(&mut conn, uid).await;
    }
}

// --- test_options_ledger.py ---------------------------------------------------------------------

fn later() -> DateTime<FixedOffset> {
    now() + chrono::Duration::hours(1)
}

async fn raise_exits(conn: &mut Conn, uid: i64, sleeve: Sleeve, code: &str) {
    let mut store = PgOptionsStore::new(conn, uid);
    for tracked in store.open_positions(day()).await.unwrap() {
        if tracked.sleeve == sleeve {
            store
                .raise_exit(
                    tracked.session_id,
                    &ExitVerdict::rule(code, "RULE"),
                    later(),
                )
                .await
                .unwrap();
        }
    }
}

async fn close_all(
    conn: &mut Conn,
    uid: i64,
    gw: Arc<dyn Gateway>,
    quotes: HashMap<i64, Quote>,
) -> Vec<ExecOutcome> {
    let gw_for = move |_s: Sleeve| Some(gw.clone());
    let mut store = PgOptionsStore::new(conn, uid);
    execute::run_pending_exits(&mut store, &gw_for, &Quotes(quotes), &paper_mode, &later)
        .await
        .unwrap()
}

async fn session_of(conn: &mut Conn, plan_id: &str) -> Row {
    rows(
        conn,
        "SELECT * FROM op_session WHERE plan_id = ?",
        &[Param::Text(plan_id.to_owned())],
    )
    .await
    .remove(0)
}

fn pauses(out: &ExecOutcome) -> Vec<String> {
    match out.detail.get("pauses") {
        Some(baskfy_desk::wire::PyValue::List(items)) => items
            .iter()
            .map(baskfy_desk::wire::PyValue::py_str)
            .collect(),
        other => panic!("no pauses: {other:?}"),
    }
}

#[tokio::test]
async fn a_close_writes_the_journal_row_with_r_and_mae_mfe() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(&mut conn, uid, "O1M", "IRON_CONDOR", CONDOR, None, None).await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O1m).await;
    confirm(&mut conn, uid, gw.clone(), &plan_id, deep())
        .await
        .unwrap();
    {
        let mut store = PgOptionsStore::new(&mut conn, uid);
        let tracked = store.open_positions(day()).await.unwrap().remove(0);
        // Credit 29.80; marks at a 35.00 cost (−5.20) and a 10.00 cost (+19.80): the extremes.
        store
            .record_mark(&tracked, d("35.00"), now() + chrono::Duration::minutes(5))
            .await
            .unwrap();
        store
            .record_mark(&tracked, d("10.00"), now() + chrono::Duration::minutes(30))
            .await
            .unwrap();
    }
    raise_exits(&mut conn, uid, Sleeve::O1m, "PROFIT").await;
    let mut exit_book = deep();
    exit_book.insert(9303, book("4.90", "5.00", 1300));
    exit_book.insert(9304, book("4.90", "5.00", 1300));
    exit_book.insert(9301, book("0.50", "0.60", 1300));
    exit_book.insert(9302, book("0.50", "0.60", 1300));
    let out = close_all(&mut conn, uid, gw, exit_book).await;
    assert_eq!(out.len(), 1);
    assert_eq!(out[0].outcome, "FLAT");
    let sid = py::int(py::col(&session_of(&mut conn, &plan_id).await, "id"));
    let row = rows(
        &mut conn,
        "SELECT * FROM op_journal WHERE session_id = ?",
        &[Param::Int(sid)],
    )
    .await
    .remove(0);
    assert_eq!(py::text(py::col(&row, "closed_reason")), "PROFIT");
    assert!(py::truthy(py::col(&row, "simulated")));
    let dec = |c: &str| d(&py::text(py::col(&row, c)));
    let gross = (d("29.80") - d("9.00")) * d("65");
    assert_eq!(dec("gross_pnl_inr"), gross);
    let costs = dec("costs_inr");
    assert!(costs > Decimal::ZERO);
    assert_eq!(dec("net_pnl_inr"), gross - costs);
    assert_eq!(dec("risk_budget_inr"), d("2500.00"));
    assert_eq!(
        dec("r_multiple"),
        ((gross - costs) / d("2500"))
            .round_dp_with_strategy(2, rust_decimal::RoundingStrategy::MidpointNearestEven)
    );
    assert_eq!(dec("mfe_r"), (d("19.80") * d("65") / d("2500")).round_dp(2));
    assert_eq!(dec("mae_r"), (d("-5.20") * d("65") / d("2500")).round_dp(2));
    let detail = py::text(py::col(&row, "detail"));
    assert!(detail.contains("\"orders\": 8"), "{detail}");
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn minus_2r_on_o2_pauses_the_sleeve_today() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(
        &mut conn,
        uid,
        "O2",
        "LONG_OPTION",
        &SPREAD[..1],
        None,
        None,
    )
    .await;
    rows(
        &mut conn,
        "UPDATE op_plan SET risk_budget_inr = 500, sizing_mode = 'BUDGET' WHERE plan_id = ?",
        &[Param::Text(plan_id.clone())],
    )
    .await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O2).await;
    confirm(&mut conn, uid, gw.clone(), &plan_id, deep())
        .await
        .unwrap(); // bought at 35.20
    raise_exits(&mut conn, uid, Sleeve::O2, "STOP").await;
    let mut q = deep();
    q.insert(9311, book("20.00", "20.20", 1300)); // sold at 20.00
    let out = close_all(&mut conn, uid, gw, q).await;
    assert_eq!(out.len(), 1);
    assert_eq!(out[0].outcome, "FLAT");
    assert!(pauses(&out[0]).contains(&"DAILY_R".to_owned()), "{out:?}");
    let sid = py::int(py::col(&session_of(&mut conn, &plan_id).await, "id"));
    let j = rows(
        &mut conn,
        "SELECT r_multiple FROM op_journal WHERE session_id = ?",
        &[Param::Int(sid)],
    )
    .await;
    assert!(d(&py::text(py::col(&j[0], "r_multiple"))) <= d("-2"));
    let cfg = rows(
        &mut conn,
        "SELECT paused_until, paused_reason FROM op_sleeve_config WHERE user_id = ? AND sleeve = 'O2'",
        &[Param::Int(uid)],
    )
    .await;
    assert_eq!(
        &py::text(py::col(&cfg[0], "paused_until"))[..10],
        "2026-10-27"
    );
    assert_eq!(py::text(py::col(&cfg[0], "paused_reason")), "DAILY_R");
    let audit = rows(
        &mut conn,
        "SELECT changed_by, new_value FROM op_config_audit WHERE user_id = ? AND scope = 'O2'",
        &[Param::Int(uid)],
    )
    .await;
    let audit: Vec<(String, String)> = audit
        .iter()
        .map(|a| {
            (
                py::text(py::col(a, "changed_by")),
                py::text(py::col(a, "new_value")),
            )
        })
        .collect();
    assert_eq!(audit, [("ledger".to_owned(), "2026-10-27".to_owned())]);
    assert_eq!(
        py::text(py::col(&session_of(&mut conn, &plan_id).await, "state")),
        "CLOSED"
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn four_minus_2r_o1w_weeks_pause_to_month_end_not_by_week() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    for dd in [6, 13, 20, 27] {
        let dday = NaiveDate::from_ymd_opt(2026, 10, dd).unwrap();
        let sid = py::int(py::col(
            &rows(
                &mut conn,
                "INSERT INTO op_session (user_id, sleeve, trade_date, mode, state) \
                 VALUES (?, 'O1W', ?, 'PAPER', 'CLOSED') RETURNING id",
                &[Param::Int(uid), Param::Date(dday)],
            )
            .await[0],
            "id",
        ));
        rows(
            &mut conn,
            "INSERT INTO op_journal (session_id, user_id, sleeve, trade_date, expiry_used, \
             structure, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, \
             risk_budget_inr, r_multiple, closed_reason, minutes_held, simulated, sizing_mode) \
             VALUES (?, ?, 'O1W', ?, ?, 'IRON_CONDOR', 2500, 7500, -5000, 0, -5000, 2500, -2, \
             'STOP', 60, true, 'PAPER_ONE_LOT')",
            &[
                Param::Int(sid),
                Param::Int(uid),
                Param::Date(dday),
                Param::Date(dday),
            ],
        )
        .await;
    }
    let applied = {
        let mut store = PgOptionsStore::new(&mut conn, uid);
        baskfy_desk::lanes::options::ledger::apply_ledger(
            &mut store,
            Sleeve::O1w,
            day(),
            now(),
            d("2500"),
            Mode::Paper,
        )
        .await
        .unwrap()
    };
    assert!(applied.contains(&"MONTHLY_R".to_owned()), "{applied:?}");
    assert!(!applied.contains(&"WEEKLY_R".to_owned()), "{applied:?}");
    let cfg = rows(
        &mut conn,
        "SELECT paused_until FROM op_sleeve_config WHERE user_id = ? AND sleeve = 'O1W'",
        &[Param::Int(uid)],
    )
    .await;
    assert_eq!(
        &py::text(py::col(&cfg[0], "paused_until"))[..10],
        "2026-10-31"
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn a_book_rupee_breach_closes_every_open_position() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    rows(
        &mut conn,
        "INSERT INTO op_sleeve_config (user_id, sleeve, sleeve_capital_inr) VALUES (?, 'O2', 50000)",
        &[Param::Int(uid)],
    )
    .await;
    let condor = seed(&mut conn, uid, "O1M", "IRON_CONDOR", CONDOR, None, None).await;
    let o2 = seed(
        &mut conn,
        uid,
        "O2",
        "LONG_OPTION",
        &SPREAD[..1],
        None,
        None,
    )
    .await;
    rows(
        &mut conn,
        "UPDATE op_plan SET risk_budget_inr = 5000, sizing_mode = 'BUDGET' WHERE plan_id = ?",
        &[Param::Text(o2.clone())],
    )
    .await;
    let bed = Bed::new();
    let gw1 = bed.gateway(paper_defaults(), Sleeve::O1m).await;
    let gw2 = bed.gateway(paper_defaults(), Sleeve::O2).await;
    confirm(&mut conn, uid, gw1.clone(), &condor, deep())
        .await
        .unwrap();
    confirm(&mut conn, uid, gw2.clone(), &o2, deep())
        .await
        .unwrap();
    raise_exits(&mut conn, uid, Sleeve::O2, "STOP").await;
    let mut q = deep();
    q.insert(9311, book("20.00", "20.20", 1300));
    let out = close_all(&mut conn, uid, gw2, q).await;
    assert_eq!(out.len(), 1);
    assert!(
        pauses(&out[0]).contains(&"BOOK_DAILY_INR".to_owned()),
        "{out:?}"
    );
    let csid = py::int(py::col(&session_of(&mut conn, &condor).await, "id"));
    let exit_plan = rows(
        &mut conn,
        "SELECT p.exit_plan_id, x.detail FROM op_position p JOIN op_plan x \
         ON x.plan_id = p.exit_plan_id WHERE p.session_id = ?",
        &[Param::Int(csid)],
    )
    .await;
    assert_eq!(exit_plan.len(), 1);
    assert!(
        py::text(py::col(&exit_plan[0], "detail")).contains("BOOK_DAILY_INR"),
        "{exit_plan:?}"
    );
    let closed = close_all(&mut conn, uid, gw1, deep()).await;
    assert_eq!(closed.len(), 1);
    assert_eq!(closed[0].outcome, "FLAT");
    assert_eq!(
        py::text(py::col(&session_of(&mut conn, &condor).await, "state")),
        "CLOSED"
    );
    let book_row = rows(
        &mut conn,
        "SELECT paused_until, paused_reason FROM op_book_config WHERE user_id = ?",
        &[Param::Int(uid)],
    )
    .await;
    assert!(py::text(py::col(&book_row[0], "paused_reason")).starts_with("BOOK_DAILY_INR"));
    assert_eq!(bed.broker_calls(), 0);
    drop_user(&mut conn, uid).await;
}

// --- test_options_monitor.py::TestTheStoreOnADatabase --------------------------------------------

/// The monitor's store on a real Postgres with the `op_` schema: `open_positions`, `raise_exit`
/// (one EXIT plan, `<entry>-X`, idempotent), `record_mark` — Python's seed, verbatim.
#[tokio::test]
async fn the_position_store_raises_one_exit_and_marks() {
    use baskfy_desk::lanes::options::monitor::{PgPositionStore, PositionStore};
    let Some(mut conn) = connect().await else {
        return;
    };
    let tag = unique_tag();
    let uid = py::int(py::col(
        &rows(
            &mut conn,
            "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
            &[
                Param::Text(format!("op9-{}", &tag[..8])),
                Param::Text(format!("op9-{}@x.test", &tag[8..16])),
            ],
        )
        .await[0],
        "id",
    ));
    let day = NaiveDate::from_ymd_opt(2026, 10, 20).unwrap();
    for (token, strike) in [(9201_i64, 25200_i64), (9202, 25300)] {
        rows(
            &mut conn,
            "INSERT INTO op_contract (instrument_token, tradingsymbol, underlying, expiry, \
             strike, option_type, lot_size, tick_size, first_seen, last_seen, expired) \
             VALUES (?, ?, 'NIFTY', ?, ?, 'CE', 65, 0.05, ?, ?, false) \
             ON CONFLICT (instrument_token) DO NOTHING",
            &[
                Param::Int(token),
                Param::Text(format!("NIFTYOP9{token}")),
                Param::Date(day),
                Param::Int(strike),
                Param::Date(day),
                Param::Date(day),
            ],
        )
        .await;
    }
    let plan_id = format!("O3A-OP9-{uid}");
    let sid = py::int(py::col(
        &rows(
            &mut conn,
            "INSERT INTO op_session (user_id, sleeve, trade_date, expiry_used, mode, state, plan_id) \
             VALUES (?, 'O3A', ?, ?, 'PAPER', 'OPEN', ?) RETURNING id",
            &[
                Param::Int(uid),
                Param::Date(day),
                Param::Date(day),
                Param::Text(plan_id.clone()),
            ],
        )
        .await[0],
        "id",
    ));
    let at = |h, m| ist().with_ymd_and_hms(2026, 10, 20, h, m, 0).unwrap();
    let detail =
        r#"{"direction": "UP", "gate": {"range_high": "25182.00", "range_low": "24998.00"}}"#;
    let plan_pk = py::int(py::col(
        &rows(
            &mut conn,
            "INSERT INTO op_plan (user_id, plan_id, session_id, sleeve, structure, kind, sizing_mode, \
             issued_at, expires_at, lots, lot_size, width_points, risk_budget_inr, status, detail) \
             VALUES (?, ?, ?, 'O3A', 'DEBIT_SPREAD', 'ENTRY', 'PAPER_ONE_LOT', ?, ?, 1, 65, 100, 2500, \
             'CONFIRMED', ?) RETURNING id",
            &[
                Param::Int(uid),
                Param::Text(plan_id.clone()),
                Param::Int(sid),
                Param::Aware(at(10, 21)),
                Param::Aware(at(10, 51)),
                Param::Text(detail.to_owned()),
            ],
        )
        .await[0],
        "id",
    ));
    let mut leg_ids = Vec::new();
    for (seq, (role, token, strike, side, price)) in [
        ("LONG_CALL", 9201_i64, 25200_i64, "BUY", "35.05"),
        ("SHORT_CALL", 9202, 25300, "SELL", "5.00"),
    ]
    .into_iter()
    .enumerate()
    {
        leg_ids.push(py::int(py::col(
            &rows(
                &mut conn,
                "INSERT INTO op_leg (user_id, plan_id, seq, role, tradingsymbol, instrument_token, \
                 strike, option_type, side, quantity, status, filled_qty, avg_price) \
                 VALUES (?, ?, ?, ?, ?, ?, ?, 'CE', ?, 65, 'FILLED', 65, ?) RETURNING id",
                &[
                    Param::Int(uid),
                    Param::Int(plan_pk),
                    Param::Int(i64::try_from(seq).unwrap() + 1),
                    Param::Text(role.to_owned()),
                    Param::Text(format!("NIFTYOP9{token}")),
                    Param::Int(token),
                    Param::Int(strike),
                    Param::Text(side.to_owned()),
                    Param::Decimal(d(price)),
                ],
            )
            .await[0],
            "id",
        )));
    }
    rows(
        &mut conn,
        &format!(
            "INSERT INTO op_position (session_id, user_id, leg_ids, entry_points, entry_inr, lots, \
             opened_at, hard_exit_at, simulated) VALUES (?, ?, {}, 30.05, 1953.25, 1, ?, ?, true)",
            py::int_array_sql(&leg_ids)
        ),
        &[
            Param::Int(sid),
            Param::Int(uid),
            Param::Aware(at(10, 22)),
            Param::Aware(at(14, 45)),
        ],
    )
    .await;

    let url = std::env::var("BASKFY_RS_OPTIONS_TEST_PG").unwrap();
    let store_conn = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "public".to_owned(),
        database_url: url,
        db_path: PathBuf::from("unused.db"),
    })
    .await
    .unwrap()
    .connect()
    .await
    .unwrap();
    let mut store = PgPositionStore {
        conn: store_conn,
        user_id: uid,
    };
    let open = store.open_positions(day).await.unwrap();
    assert_eq!(open.len(), 1);
    let tracked = &open[0];
    assert!(tracked.session_id == sid && tracked.exit_plan_id.is_none());
    assert_eq!(tracked.position.entry_points, d("30.05"));
    assert_eq!(tracked.position.range_high, Some(d("25182.00")));
    assert_eq!(tracked.tokens(), vec![9201, 9202]);
    let verdict = baskfy_core::options::exits::ExitVerdict {
        code: "TARGET".to_owned(),
        reason: "RULE".to_owned(),
        value_points: Some(d("81.50")),
        marked_loss_inr: Some(d("-3315")),
        stale: false,
    };
    let when = at(10, 40).naive_local() + chrono::Duration::seconds(50);
    let first = store.raise_exit(tracked, &verdict, when).await.unwrap();
    let again = store.raise_exit(tracked, &verdict, when).await.unwrap();
    assert_eq!(first, again);
    let first = first.unwrap();
    assert_eq!(first, format!("{plan_id}-X"));
    let plans = rows(
        &mut conn,
        "SELECT kind, status, detail->>'code' AS code FROM op_plan WHERE plan_id = ?",
        &[Param::Text(first.clone())],
    )
    .await;
    assert_eq!(plans.len(), 1);
    assert_eq!(py::text(py::col(&plans[0], "kind")), "EXIT");
    assert_eq!(py::text(py::col(&plans[0], "status")), "ISSUED");
    assert_eq!(py::text(py::col(&plans[0], "code")), "TARGET");
    let reread = store.open_positions(day).await.unwrap();
    assert_eq!(reread[0].exit_plan_id.as_deref(), Some(first.as_str()));
    store.record_mark(tracked, d("81.5"), when).await.unwrap();
    let mark = rows(
        &mut conn,
        "SELECT last_mark_points FROM op_position WHERE session_id = ?",
        &[Param::Int(sid)],
    )
    .await;
    assert_eq!(
        py::dec(py::col(&mark[0], "last_mark_points")),
        Some(d("81.50"))
    );
    drop_user(&mut conn, uid).await;
}

// --- test_options_safety.py::TestLiveRefused / TestNoSecondEntry -------------------------------

#[tokio::test]
async fn all_four_flags_on_is_refused_before_any_order() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let mut s = paper_defaults();
    s.options_enabled = true;
    s.intraday_enabled = true;
    s.options_o2_execution_enabled = true;
    s.dry_run = false;
    assert_eq!(gates::mode_of(&s, Sleeve::O2), Mode::Live);
    let uid = new_user(&mut conn).await;
    let plan_id = seed(
        &mut conn,
        uid,
        "O2",
        "LONG_OPTION",
        &SPREAD[..1],
        None,
        None,
    )
    .await;
    let bed = Bed::new();
    let gw = bed.gateway(s.clone(), Sleeve::O2).await;
    let modes = |sl: Sleeve| gates::mode_of(&s, sl);
    let mut store = PgOptionsStore::new(&mut conn, uid);
    let r = execute_entry(
        &mut store,
        gw,
        &Quotes(deep()),
        &plan_id,
        true,
        &modes,
        &now,
    )
    .await;
    assert_eq!(refused(r).1, "LIVE_NOT_BUILT");
    assert!(orders(&mut conn, &plan_id).await.is_empty());
    assert_eq!(bed.broker_calls(), 0);
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn a_second_entry_plan_in_a_confirmed_session_is_refused() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(&mut conn, uid, "O3B", "DEBIT_SPREAD", SPREAD, None, None).await;
    let bed = Bed::new();
    let gw = bed.gateway(paper_defaults(), Sleeve::O3b).await;
    let first = confirm(&mut conn, uid, gw.clone(), &plan_id, deep())
        .await
        .unwrap();
    assert_eq!(first.outcome, "OPEN");
    let session = py::int(py::col(
        &rows(
            &mut conn,
            "SELECT id FROM op_session WHERE plan_id = ?",
            &[Param::Text(plan_id.clone())],
        )
        .await[0],
        "id",
    ));
    // A second ISSUED entry plan hung on the same (now OPEN) session.
    let again = format!("{plan_id}-again");
    rows(
        &mut conn,
        "INSERT INTO op_plan (user_id, plan_id, session_id, sleeve, structure, kind, \
         sizing_mode, issued_at, expires_at, lots, lot_size, risk_budget_inr, status, detail) \
         SELECT user_id, ?, session_id, sleeve, structure, kind, sizing_mode, issued_at, \
         expires_at, lots, lot_size, risk_budget_inr, 'ISSUED', detail FROM op_plan \
         WHERE plan_id = ?",
        &[Param::Text(again.clone()), Param::Text(plan_id.clone())],
    )
    .await;
    let r = confirm(&mut conn, uid, gw, &again, deep()).await;
    assert_eq!(refused(r), (409, "NOT_ISSUED".to_owned()));
    let n = rows(
        &mut conn,
        "SELECT count(*) AS n FROM op_order o JOIN op_leg l ON l.id = o.leg_id \
         JOIN op_plan p ON p.id = l.plan_id WHERE p.session_id = ?",
        &[Param::Int(session)],
    )
    .await;
    assert_eq!(
        py::int(py::col(&n[0], "n")),
        2,
        "the first entry's two legs, nothing more"
    );
    drop_user(&mut conn, uid).await;
}

#[tokio::test]
async fn a_second_session_for_the_sleeve_and_day_cannot_exist() {
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    seed(
        &mut conn,
        uid,
        "O2",
        "LONG_OPTION",
        &SPREAD[..1],
        None,
        None,
    )
    .await;
    let err = conn
        .execute(
            "INSERT INTO op_session (user_id, sleeve, trade_date, mode, state) \
             VALUES (?, 'O2', ?, 'PAPER', 'PLANNED')",
            &[Param::Int(uid), Param::Date(day())],
        )
        .await
        .unwrap_err();
    assert!(
        err.to_string().contains("uq_op_session_user_sleeve_date"),
        "{err}"
    );
    drop_user(&mut conn, uid).await;
}

// --- test_options_ledger.py::TestMonthlyR::test_a_pause_is_never_shortened ---------------------

#[tokio::test]
async fn a_pause_is_never_shortened() {
    use baskfy_desk::lanes::options::core::{Pause, PauseReason};
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    rows(
        &mut conn,
        "INSERT INTO op_sleeve_config (user_id, sleeve, paused_until, paused_reason) \
         VALUES (?, 'O2', '2026-11-30', 'MANUAL')",
        &[Param::Int(uid)],
    )
    .await;
    {
        let mut store = PgOptionsStore::new(&mut conn, uid);
        baskfy_desk::lanes::options::ledger::write_pause(
            &mut store,
            "O2",
            &Pause {
                paused_until: day(),
                reasons: vec![PauseReason::DailyR],
                close_now: true,
            },
        )
        .await
        .unwrap();
    }
    let config = rows(
        &mut conn,
        "SELECT paused_until FROM op_sleeve_config WHERE user_id = ? AND sleeve = 'O2'",
        &[Param::Int(uid)],
    )
    .await;
    assert_eq!(
        py::opt_day(py::col(&config[0], "paused_until")),
        NaiveDate::from_ymd_opt(2026, 11, 30)
    );
    drop_user(&mut conn, uid).await;
}

// --- test_options_execute.py::TestThePage -------------------------------------------------------

#[tokio::test]
async fn the_page_shows_the_plan_the_button_and_the_sentence() {
    use baskfy_desk::app::StateParts;
    use baskfy_desk::{DeskPaths, build_state};
    use tower::ServiceExt as _;
    let Some(mut conn) = connect().await else {
        return;
    };
    let uid = new_user(&mut conn).await;
    let plan_id = seed(&mut conn, uid, "O3A", "DEBIT_SPREAD", SPREAD, None, None).await;
    let dir = tempfile::tempdir().unwrap();
    std::os::unix::fs::symlink(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../kite-momentum-rebalancer/app"),
        dir.path().join("app"),
    )
    .unwrap();
    std::fs::create_dir(dir.path().join("data")).unwrap();
    let kite_dir = dir.path().join("kite");
    std::fs::create_dir_all(kite_dir.join("instruments")).unwrap();
    std::fs::write(kite_dir.join("kite.json"), FIXTURE).unwrap();
    let kite = Arc::new(ReplayKite::load(&kite_dir, None, "test-api-key").unwrap());
    let mut settings = paper_defaults();
    settings.sole_user_id = uid;
    let db = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "public".to_owned(),
        database_url: std::env::var("BASKFY_RS_OPTIONS_TEST_PG").unwrap(),
        db_path: dir.path().join("unused.db"),
    })
    .await
    .unwrap();
    let state = build_state(StateParts {
        settings,
        clock: Arc::new(FixedClock::new(now())),
        ids: Arc::new(HarnessIds::new("options-page")),
        db,
        kite,
        paths: DeskPaths::under(dir.path()),
        harness: None,
    });
    let app = baskfy_desk::lanes::options::routes().with_state(state);
    let get = |path: &str| {
        axum::http::Request::builder()
            .uri(path)
            .body(axum::body::Body::empty())
            .unwrap()
    };
    let page = app.clone().oneshot(get("/nifty-options")).await.unwrap();
    assert_eq!(page.status(), 200);
    let text = String::from_utf8(
        axum::body::to_bytes(page.into_body(), usize::MAX)
            .await
            .unwrap()
            .to_vec(),
    )
    .unwrap();
    assert!(text.contains(&plan_id));
    assert!(text.contains("Confirm — simulated"));
    assert!(text.contains("80 % of width, half the debit lost"));
    let data = app.oneshot(get("/nifty-options/data")).await.unwrap();
    let body: serde_json::Value = serde_json::from_slice(
        &axum::body::to_bytes(data.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["ok"], true);
    assert_eq!(body["plans"][0]["legs"][0]["role"], "LONG_CALL");
    drop_user(&mut conn, uid).await;
}
