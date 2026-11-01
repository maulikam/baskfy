//! FO7's replay world (`tests/test_fno_monitor.py`'s `World`), shared by the fno_exec lane's
//! DB-backed tests: the real engine ([`FnoMonitor`]), the real executor and the real FO
//! gateway (paper-pinned, the covered-overnight rule wired) over a real PostgreSQL with the
//! `fo_` schema, a fixed clock per call, a fixture book, a fixture F&O bhavcopy, and a
//! **recording broker that must see nothing** (the replay Kite: every call recorded, nothing
//! sent).
//!
//! Like Python's `requires_db`, the database is `BASKFY_RS_FNO_TEST_PG` (a bed clone `rsd_*`,
//! never the template); without it each test returns early and says so. Every row a test writes
//! hangs off its own `app_user`, deleted at the end (`ON DELETE CASCADE`).
#![allow(
    dead_code,
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::print_stderr,
    clippy::missing_panics_doc
)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};

use baskfy_core::fno::calendar::sessions_before;
use baskfy_core::fno::config::FoSleeve;
use baskfy_core::options::execution::Level;
use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::exec_core::desk::ShimGateway;
use baskfy_desk::lanes::exec_core::guards::core_coverage;
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskLayer, RiskManager};
use baskfy_desk::lanes::fno::fno_exec::execute::{
    ExecError, FoCtx, FoOutcome, execute_click_exit, execute_entry, paper_book, tenancy,
};
use baskfy_desk::lanes::fno::fno_exec::monitor::seams::{
    Contract, IndexQuote, MarginQuote, Print, PrintKey, SharedBooks, StaticIndex,
    StaticInstruments, StaticMargin, StaticMarket,
};
use baskfy_desk::lanes::fno::fno_exec::monitor::{FnoMonitor, NightReport, TickReport};
use baskfy_desk::lanes::fno::fno_exec::store::{FoQuote, FoStore};
use baskfy_desk::lanes::fno::gates::{mode_of, paper_gates};
use baskfy_desk::lanes::fno::ledger::DeskLedger;
use baskfy_desk::lanes::options::py;
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Conn, Db, DbConfig, DbValue, Param, Row};
use baskfy_desk::wire::{PyDict, PyValue};
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

pub const ENV: &str = "BASKFY_RS_FNO_TEST_PG";

// --- the fixture calendar -----------------------------------------------------------------------

pub fn date(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).unwrap()
}

/// `SESSIONS`: the weekdays of the 130 days from 1 Oct 2026.
pub fn sessions() -> Vec<NaiveDate> {
    (0..130)
        .map(|i| date(2026, 10, 1) + Duration::days(i))
        .filter(|d| d.weekday().num_days_from_monday() < 5)
        .collect()
}

pub fn before(anchor: NaiveDate, n: i64) -> NaiveDate {
    sessions_before(&sessions(), anchor, n).unwrap().unwrap()
}

pub fn nov() -> NaiveDate {
    date(2026, 11, 24)
}
pub fn dec_() -> NaiveDate {
    date(2026, 12, 29)
}
pub fn f1_entry() -> NaiveDate {
    before(nov(), 15)
}
pub fn hard_exit() -> NaiveDate {
    before(nov(), 1)
}
pub fn f2_entry() -> NaiveDate {
    date(2026, 11, 2)
}
pub const LOT: i64 = 65;
pub const F2_LOT: i64 = 500;
pub const FUT_NOV: &str = "RELIANCE26NOVFUT";
pub const FUT_DEC: &str = "RELIANCE26DECFUT";
pub const ROLES: [(&str, i64, &str); 4] = [
    ("LONG_CALL", 25400, "CE"),
    ("SHORT_CALL", 25000, "CE"),
    ("SHORT_PUT", 23000, "PE"),
    ("LONG_PUT", 22600, "PE"),
];

pub fn strike(role: &str) -> i64 {
    ROLES.iter().find(|(r, _, _)| *r == role).unwrap().1
}
pub fn kind(role: &str) -> &'static str {
    ROLES.iter().find(|(r, _, _)| *r == role).unwrap().2
}

pub fn at(day: NaiveDate, hh: u32, mm: u32) -> DateTime<FixedOffset> {
    ist()
        .from_local_datetime(&day.and_hms_opt(hh, mm, 0).unwrap())
        .single()
        .unwrap()
}

pub fn d(s: &str) -> Decimal {
    py::dec_from_str(s).unwrap()
}

pub fn opt_symbol(underlying: &str, role: &str) -> String {
    format!("{underlying}26NOV{}{}", strike(role), kind(role))
}

/// `book(bid, ask, last=, depth=)`.
pub fn book_full(bid: &str, ask: &str, last: Option<&str>, depth: i64) -> FoQuote {
    let (b, a) = (d(bid), d(ask));
    FoQuote {
        bid: Some(b),
        ask: Some(a),
        bids: vec![Level {
            price: b,
            quantity: depth,
        }],
        asks: vec![Level {
            price: a,
            quantity: depth,
        }],
        last: Some(last.map_or_else(|| py::div(b + a, Decimal::from(2)).unwrap(), d)),
        oi: Some(1_000_000),
    }
}

pub fn book(bid: &str, ask: &str) -> FoQuote {
    book_full(bid, ask, None, 100_000)
}

pub fn book_last(bid: &str, ask: &str, last: &str) -> FoQuote {
    book_full(bid, ask, Some(last), 100_000)
}

/// `condor_book(sc, sp, lc, lp)`: a one-rupee book around each short's mid, half a rupee
/// around each wing's.
pub fn condor_book(
    underlying: &str,
    sc: &str,
    sp: &str,
    lc: &str,
    lp: &str,
) -> Vec<(String, FoQuote)> {
    let mut out = Vec::new();
    for (role, mid, half) in [
        ("SHORT_CALL", sc, "0.5"),
        ("SHORT_PUT", sp, "0.5"),
        ("LONG_CALL", lc, "0.25"),
        ("LONG_PUT", lp, "0.25"),
    ] {
        let (m, h) = (d(mid), d(half));
        out.push((
            opt_symbol(underlying, role),
            book(&py::dstr(m - h), &py::dstr(m + h)),
        ));
    }
    out
}

/// `NEUTRAL`: credit on mids 93.50.
pub fn neutral(underlying: &str) -> Vec<(String, FoQuote)> {
    condor_book(underlying, "60.5", "55.5", "12.25", "10.25")
}

/// `FakeInstruments` (and `BothIndices`): NIFTY's (and BANKNIFTY's) four strikes on the November
/// monthly, RELIANCE's two futures.
pub fn instruments() -> StaticInstruments {
    let mut out = StaticInstruments::default();
    for (underlying, base) in [("NIFTY", 7_000_000), ("BANKNIFTY", 7_500_000)] {
        for (role, k, t) in ROLES {
            out.options.push((
                underlying.to_owned(),
                nov(),
                Decimal::from(k),
                t.to_owned(),
                Contract {
                    tradingsymbol: opt_symbol(underlying, role),
                    instrument_token: base + k,
                    lot_size: LOT,
                    expiry: nov(),
                },
            ));
        }
    }
    for (sym, token, expiry) in [(FUT_NOV, 8_000_001, nov()), (FUT_DEC, 8_000_002, dec_())] {
        out.futures.push((
            "RELIANCE".to_owned(),
            Contract {
                tradingsymbol: sym.to_owned(),
                instrument_token: token,
                lot_size: F2_LOT,
                expiry,
            },
        ));
    }
    out
}

/// `land_condor(day, settles)`.
pub fn land_condor(m: &StaticMarket, underlying: &str, day: NaiveDate, settles: &[(&str, &str)]) {
    m.land(
        day,
        settles.iter().map(|(r, s)| {
            (
                (
                    underlying.to_owned(),
                    nov(),
                    Some(Decimal::from(strike(r))),
                    kind(r).to_owned(),
                ),
                Print {
                    close: d(s),
                    settle: d(s),
                },
            )
        }),
    );
}

/// `land_future(day, expiry, close, settle)`.
pub fn land_future(m: &StaticMarket, day: NaiveDate, expiry: NaiveDate, close: &str, settle: &str) {
    let key: PrintKey = ("RELIANCE".to_owned(), expiry, None, "XX".to_owned());
    m.land(
        day,
        [(
            key,
            Print {
                close: d(close),
                settle: d(settle),
            },
        )],
    );
}

// --- the database -------------------------------------------------------------------------------

/// One pool on the bed clone under `schema`, for a test to share across its worlds.
pub async fn open_db(schema: &str) -> Option<Db> {
    let Ok(url) = std::env::var(ENV) else {
        eprintln!("{ENV} is not set: skipped (Python's requires_db)");
        return None;
    };
    Some(
        Db::open(DbConfig {
            backend: "postgres".to_owned(),
            schema: schema.to_owned(),
            database_url: url,
            db_path: PathBuf::from("unused.db"),
        })
        .await
        .unwrap(),
    )
}

pub async fn connect() -> Option<Conn> {
    let db = open_db("public").await?;
    Some(db.connect().await.unwrap())
}

pub async fn rows(conn: &mut Conn, sql: &str, params: &[Param]) -> Vec<Row> {
    conn.execute(sql, params).await.unwrap().rows
}

pub fn unique_tag() -> String {
    use std::sync::atomic::AtomicU64;
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

/// The `user` fixture: a fresh `app_user` with F1 ₹25,00,000 and F2 ₹0 configured.
pub async fn new_user(conn: &mut Conn, prefix: &str) -> i64 {
    let tag = unique_tag();
    let r = rows(
        conn,
        "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
        &[
            Param::Text(format!("{prefix}-{tag}")),
            Param::Text(format!("{prefix}-{tag}@x.test")),
        ],
    )
    .await;
    let uid = py::int(py::col(&r[0], "id"));
    rows(
        conn,
        "INSERT INTO fo_sleeve_config (user_id, sleeve, capital_inr) VALUES (?, 'F1', 2500000), \
         (?, 'F2', 0)",
        &[Param::Int(uid), Param::Int(uid)],
    )
    .await;
    uid
}

pub async fn drop_user(conn: &mut Conn, uid: i64) {
    rows(
        conn,
        "DELETE FROM app_user WHERE id = ?",
        &[Param::Int(uid)],
    )
    .await;
}

pub fn col<'a>(row: &'a Row, name: &str) -> &'a DbValue {
    py::col(row, name)
}
pub fn dcol(row: &Row, name: &str) -> Decimal {
    py::dec(col(row, name))
        .unwrap_or_else(|| panic!("{name} is not a number: {:?}", col(row, name)))
}
pub fn tcol(row: &Row, name: &str) -> String {
    py::text(col(row, name))
}
pub fn icol(row: &Row, name: &str) -> i64 {
    py::int(col(row, name))
}
pub fn jcol(row: &Row, name: &str) -> PyDict {
    py::json_obj(col(row, name))
}
pub fn is_null(row: &Row, name: &str) -> bool {
    col(row, name).is_null()
}

pub fn jtext(m: &PyDict, key: &str) -> String {
    m.get(key)
        .map_or_else(|| "None".to_owned(), PyValue::py_str)
}

// --- a gateway whose GTT placement the broker refuses -------------------------------------------

/// `gw.place_gtt_stop = refuse`: everything else is the wrapped gateway.
pub struct RefuseGtt(pub Arc<dyn Gateway>);

#[async_trait::async_trait]
impl Gateway for RefuseGtt {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        self.0.place(order).await
    }
    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        let _ = stop;
        let mut out = PyDict::new();
        out.insert("status", PyValue::Str("GTT_ERROR".to_owned()));
        out.insert(
            "error",
            PyValue::Str("fixture: the broker refused the trigger".to_owned()),
        );
        Ok(out)
    }
    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        self.0.delete_gtt(req).await
    }
    async fn modify_gtt_quantity(&self, req: ModifyGtt) -> Result<PyDict, GatewayError> {
        self.0.modify_gtt_quantity(req).await
    }
    async fn cancel_order(&self, req: CancelOrder) -> Result<PyDict, GatewayError> {
        self.0.cancel_order(req).await
    }
}

// --- the world ----------------------------------------------------------------------------------

pub const FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;

pub struct World {
    pub conn: Conn,
    pub uid: i64,
    pub settings: Arc<RwLock<Settings>>,
    pub quotes: Arc<SharedBooks>,
    pub market: Arc<StaticMarket>,
    pub margin: Arc<StaticMargin>,
    pub index: Arc<StaticIndex>,
    pub auto_exit: Arc<AtomicBool>,
    pub alerts: Arc<Mutex<Vec<String>>>,
    pub kite: Arc<ReplayKite>,
    pub gateways: Arc<Mutex<BTreeMap<FoSleeve, Arc<dyn Gateway>>>>,
    pub monitor: FnoMonitor,
    pub dir: tempfile::TempDir,
}

fn snapshot(s: &RwLock<Settings>) -> Settings {
    match s.read() {
        Ok(s) => s.clone(),
        Err(p) => p.into_inner().clone(),
    }
}

impl World {
    /// `World(conn, uid, tmp)`, every flag at its shipped default (DRY_RUN on, FO flags off).
    pub async fn new(conn: Conn, uid: i64) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let kdir = dir.path().join("kite");
        std::fs::create_dir(&kdir).unwrap();
        std::fs::write(kdir.join("kite.json"), FIXTURE).unwrap();
        std::fs::create_dir(kdir.join("instruments")).unwrap();
        let kite = Arc::new(ReplayKite::load(&kdir, None, "test-api-key").unwrap());
        let settings = Arc::new(RwLock::new(Settings::defaults().unwrap()));
        let gateways: Arc<Mutex<BTreeMap<FoSleeve, Arc<dyn Gateway>>>> = Arc::default();
        for sleeve in FoSleeve::ALL {
            let g = Self::build_gateway(
                &kite,
                &settings,
                sleeve,
                &dir.path().join(format!("fo-{}.jsonl", sleeve.value())),
            )
            .await;
            gateways.lock().unwrap().insert(sleeve, g);
        }
        let quotes = Arc::new(SharedBooks::default());
        let market = Arc::new(StaticMarket::new(sessions()));
        let margin = Arc::new(StaticMargin::new(Some(MarginQuote {
            required_inr: Decimal::from(150_000),
            free_inr: Decimal::from(1_000_000),
        })));
        let index = Arc::new(StaticIndex::default());
        let auto_exit = Arc::new(AtomicBool::new(false));
        let alerts: Arc<Mutex<Vec<String>>> = Arc::default();
        let g = gateways.clone();
        let mut monitor = FnoMonitor::new(
            &snapshot(&settings),
            Arc::new(move |sleeve| g.lock().unwrap().get(&sleeve).cloned()),
            quotes.clone(),
            Arc::new(instruments()),
            margin.clone(),
            market.clone(),
        );
        let s = settings.clone();
        monitor.mode_of = Arc::new(move |sleeve| mode_of(&snapshot(&s), sleeve));
        let sink = alerts.clone();
        monitor.alert = Arc::new(move |m: &str| sink.lock().unwrap().push(m.to_owned()));
        monitor.index_quotes = index.clone();
        let flag = auto_exit.clone();
        monitor.exit_by_monitor = Arc::new(move |_| flag.load(Ordering::SeqCst));
        Self {
            conn,
            uid,
            settings,
            quotes,
            market,
            margin,
            index,
            auto_exit,
            alerts,
            kite,
            gateways,
            monitor,
            dir,
        }
    }

    /// `X.build_fo_gateway(sleeve, kc, RiskManager(), journal)`: the sleeve's paper-pinned
    /// switches read per order and the covered-overnight rule, behind the desk's tenant shim.
    pub async fn build_gateway(
        kite: &Arc<ReplayKite>,
        settings: &Arc<RwLock<Settings>>,
        sleeve: FoSleeve,
        journal: &std::path::Path,
    ) -> Arc<dyn Gateway> {
        let clock: Arc<dyn Clock> = Arc::new(FixedClock::new(at(date(2026, 11, 3), 9, 20)));
        let risk: Arc<dyn RiskLayer> = Arc::new(
            RiskManager::new(RiskConfig::default(), clock.clone(), RiskBacking::Memory)
                .await
                .unwrap(),
        );
        let s = settings.clone();
        let core = OrderGateway::new(
            kite.clone(),
            risk,
            GatewayOptions {
                gates: Arc::new(move || paper_gates(&snapshot(&s), sleeve)),
                journal_path: journal.to_path_buf(),
                stop_band: None,
                coverage: Some(core_coverage()),
                clock,
                ids: Arc::new(HarnessIds::new("fno-monitor-db")),
                limits: None,
            },
        )
        .unwrap();
        Arc::new(ShimGateway::new(Arc::new(core), settings.clone()))
    }

    pub fn set(&self, f: impl FnOnce(&mut Settings)) {
        f(&mut self.settings.write().unwrap());
    }

    pub fn books(&self, books: Vec<(String, FoQuote)>) {
        self.quotes.update(books);
    }

    pub fn index_at(&self, last: &str, open: &str) {
        self.index.set_all(Some(IndexQuote {
            last: d(last),
            open: Some(d(open)),
        }));
    }

    pub fn gateway_for(&self, sleeve: FoSleeve) -> Arc<dyn Gateway> {
        self.gateways.lock().unwrap().get(&sleeve).cloned().unwrap()
    }

    pub fn broker_calls(&self) -> usize {
        self.kite.calls().len()
    }

    pub async fn tick(&mut self, now: DateTime<FixedOffset>) -> TickReport {
        let mut store = FoStore::new(&mut self.conn, self.uid);
        self.monitor.tick(&mut store, now).await.unwrap()
    }

    pub async fn night(&mut self, day: NaiveDate) -> NightReport {
        let mut store = FoStore::new(&mut self.conn, self.uid);
        self.monitor.nightly(&mut store, day).await.unwrap()
    }

    async fn send(
        &mut self,
        plan_id: &str,
        now: DateTime<FixedOffset>,
        confirm: bool,
        click: bool,
    ) -> Result<FoOutcome, ExecError> {
        let sleeve =
            baskfy_desk::lanes::fno::fno_exec::store::sleeve_of(plan_id.split('-').next().unwrap())
                .unwrap();
        let gateway = self.gateway_for(sleeve);
        let settings = snapshot(&self.settings);
        let now_fn = move || now;
        let book = |f: &[baskfy_core::fno::covered::OptionPosition]| paper_book(f);
        let sink = self.alerts.clone();
        let alert = move |m: &str| sink.lock().unwrap().push(m.to_owned());
        let ledger = DeskLedger;
        let ctx = FoCtx {
            quotes: &*self.quotes,
            now: &now_fn,
            broker_book: &book,
            alert: &alert,
            ledger: &ledger,
            tenant: tenancy(&settings).ok(),
        };
        let s = settings.clone();
        let modes = move |sl| mode_of(&s, sl);
        let mut store = FoStore::new(&mut self.conn, self.uid);
        if click {
            execute_click_exit(&mut store, &gateway, &ctx, plan_id, confirm).await
        } else {
            execute_entry(&mut store, &gateway, &ctx, plan_id, confirm, &modes).await
        }
    }

    /// `confirm(plan_id, now, confirm=)`.
    pub async fn confirm(
        &mut self,
        plan_id: &str,
        now: DateTime<FixedOffset>,
    ) -> Result<FoOutcome, ExecError> {
        self.send(plan_id, now, true, false).await
    }

    pub async fn confirm_with(
        &mut self,
        plan_id: &str,
        now: DateTime<FixedOffset>,
        confirm: bool,
    ) -> Result<FoOutcome, ExecError> {
        self.send(plan_id, now, confirm, false).await
    }

    /// `click_exit(plan_id, now)` (F3World).
    pub async fn click_exit(
        &mut self,
        plan_id: &str,
        now: DateTime<FixedOffset>,
    ) -> Result<FoOutcome, ExecError> {
        self.send(plan_id, now, true, true).await
    }

    /// `scan(sleeve, trade_date, symbol, detail, rv20=)`.
    pub async fn scan(
        &mut self,
        sleeve: &str,
        trade_date: NaiveDate,
        symbol: &str,
        detail: serde_json::Value,
        rv20: Option<&str>,
        state: &str,
    ) {
        let uid = self.uid;
        rows(
            &mut self.conn,
            "INSERT INTO fo_scan (user_id, sleeve, trade_date, symbol, state, reasons, detail, rv20) \
             VALUES (?, ?, ?, ?, ?, ARRAY['candidate'], ?, ?)",
            &[
                Param::Int(uid),
                Param::Text(sleeve.to_owned()),
                Param::Date(trade_date),
                Param::Text(symbol.to_owned()),
                Param::Text(state.to_owned()),
                Param::Jsonb(detail.to_string()),
                py::pdec(rv20.map(d)),
            ],
        )
        .await;
    }

    pub async fn rows(&mut self, sql: &str, params: &[Param]) -> Vec<Row> {
        rows(&mut self.conn, sql, params).await
    }

    pub async fn fills(&mut self, plan_id: &str) -> Vec<Row> {
        self.rows(
            "SELECT l.role, f.side, f.quantity, f.price, f.simulated, f.client_id FROM fo_fill f \
             JOIN fo_leg l ON l.id = f.leg_id JOIN fo_plan p ON p.id = l.plan_id \
             WHERE p.plan_id = ? ORDER BY f.id",
            &[Param::Text(plan_id.to_owned())],
        )
        .await
    }

    pub async fn fill_roles(&mut self, plan_id: &str) -> Vec<String> {
        self.fills(plan_id)
            .await
            .iter()
            .map(|r| tcol(r, "role"))
            .collect()
    }

    pub async fn position(&mut self, pid: i64) -> Row {
        self.rows("SELECT * FROM fo_position WHERE id = ?", &[Param::Int(pid)])
            .await
            .remove(0)
    }

    pub async fn plan_row(&mut self, plan_id: &str) -> Row {
        self.rows(
            "SELECT * FROM fo_plan WHERE plan_id = ?",
            &[Param::Text(plan_id.to_owned())],
        )
        .await
        .remove(0)
    }

    pub async fn plan_exists(&mut self, plan_id: &str) -> bool {
        !self
            .rows(
                "SELECT id FROM fo_plan WHERE plan_id = ?",
                &[Param::Text(plan_id.to_owned())],
            )
            .await
            .is_empty()
    }

    pub fn alerts(&self) -> Vec<String> {
        self.alerts.lock().unwrap().clone()
    }

    /// Every journal line under the world's directory.
    pub fn journal_events(&self) -> Vec<serde_json::Value> {
        let mut out = Vec::new();
        let mut paths: Vec<PathBuf> = std::fs::read_dir(self.dir.path())
            .unwrap()
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
            .collect();
        paths.sort();
        for p in paths {
            for line in std::fs::read_to_string(&p).unwrap_or_default().lines() {
                if !line.trim().is_empty() {
                    out.push(serde_json::from_str(line).unwrap());
                }
            }
        }
        out
    }
}

// --- the scans ----------------------------------------------------------------------------------

pub fn f1_legs(expiry: NaiveDate) -> serde_json::Value {
    serde_json::Value::Array(
        ["LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"]
            .iter()
            .enumerate()
            .map(|(i, r)| {
                serde_json::json!({
                    "entry_seq": i + 1, "role": r, "strike": strike(r).to_string(),
                    "option_type": kind(r), "expiry": expiry.to_string(),
                    "qty_sign": if r.starts_with("LONG") { 1 } else { -1 },
                })
            })
            .collect(),
    )
}

/// `f1_scan(w)` for `sleeve` on `underlying`.
pub async fn f1_scan_on(w: &mut World, sleeve: &str, underlying: &str) {
    let detail = serde_json::json!({
        "underlying": underlying, "entry_session": f1_entry().to_string(),
        "expiry": nov().to_string(), "hard_exit_date": hard_exit().to_string(),
        "legs": f1_legs(nov()), "lot_size": LOT,
    });
    w.scan(
        sleeve,
        before(f1_entry(), 1),
        underlying,
        detail,
        None,
        "CANDIDATE",
    )
    .await;
}

pub async fn f1_scan(w: &mut World) {
    f1_scan_on(w, "F1N", "NIFTY").await;
}

/// `open_f1(w)`.
pub async fn open_f1(w: &mut World) -> (String, i64) {
    f1_scan(w).await;
    w.books(neutral("NIFTY"));
    let raised = w.tick(at(f1_entry(), 9, 20)).await.raised;
    assert_eq!(raised.len(), 1, "{raised:?}");
    let out = w.confirm(&raised[0], at(f1_entry(), 9, 25)).await.unwrap();
    assert_eq!(out.outcome, "OPEN", "{out:?}");
    (raised[0].clone(), out.position_id.unwrap())
}

pub async fn f2_scan(w: &mut World) {
    let detail = serde_json::json!({
        "contract_expiry": nov().to_string(), "atr14": "20.00", "lot_size": F2_LOT,
        "industry": "Oil", "stop": "1340.00",
    });
    w.scan(
        "F2",
        before(f2_entry(), 1),
        "RELIANCE",
        detail,
        Some("0.250000"),
        "CANDIDATE",
    )
    .await;
}

/// `open_f2(w)`.
pub async fn open_f2(w: &mut World) -> (String, i64) {
    f2_scan(w).await;
    w.books(vec![(
        FUT_NOV.to_owned(),
        book_last("1399.90", "1400.00", "1400.00"),
    )]);
    let raised = w.tick(at(f2_entry(), 9, 20)).await.raised;
    assert_eq!(raised.len(), 1, "{raised:?}");
    let out = w.confirm(&raised[0], at(f2_entry(), 9, 22)).await.unwrap();
    assert_eq!(out.outcome, "OPEN", "{out:?}");
    (raised[0].clone(), out.position_id.unwrap())
}

/// A test body over a fresh user, its world dropped with the user afterwards (also when the
/// body panics: it runs on its own task, and the panic is re-raised after the cleanup).
#[macro_export]
macro_rules! with_world {
    (|$w:ident| $body:block) => {{
        let Some(mut conn) = fno_world::connect().await else {
            return;
        };
        let uid = fno_world::new_user(&mut conn, "fo7").await;
        let conn2 = fno_world::connect().await.unwrap();
        let task = tokio::spawn(async move {
            #[allow(unused_mut)]
            let mut $w = fno_world::World::new(conn2, uid).await;
            $body
        });
        let outcome = task.await;
        fno_world::drop_user(&mut conn, uid).await;
        if let Err(e) = outcome {
            if e.is_panic() {
                std::panic::resume_unwind(e.into_panic());
            }
            panic!("{e}");
        }
    }};
}
