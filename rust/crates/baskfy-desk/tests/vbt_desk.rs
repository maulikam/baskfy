//! The volume-breakout sleeve, ported test-first from `tests/test_vbt_execute.py`,
//! `test_vbt_safety.py`, `test_vbt_scan_safety.py`, `test_vbt_rescan_desk.py` and
//! `test_vbt_desk.py`.
//!
//! **The execute tests** drive the REAL gateway — the desk's gateway (`DeskGateway`) deriving
//! this sleeve's own sibling exactly as `vbt_gateway()` does, with `vbt_gates` read off default
//! settings (`DRY_RUN=true`, `VBT_EXECUTION_ENABLED=false`) — over the replay Kite, which
//! records every call and sends nothing. Every test asserts the replay Kite saw **no order and
//! no GTT write** (`docs/vbt/06` VB6: 0 orders reach a broker). The store is an in-memory
//! implementation of the `VbtStore` protocol, as Python's `MemoryStore` is, so these assert the
//! law and the numbers rather than any SQL.
//!
//! **The store/scan tests** run the real `PgVbtStore` over SQLite with the `vb_` tables built
//! from a DDL mirroring the columns the store reads (money written as text — the SQLite binder
//! refuses a `Decimal`, R8.2, as Python's sqlite3 does without its adapter).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::str::FromStr;
use std::sync::{Arc, RwLock};

use async_trait::async_trait;
use baskfy_desk::clock::{Clock, FixedClock};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, DerivedGateway, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
    SleeveGatewaySpec,
};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::kite::KiteWrites;
use baskfy_desk::lanes::exec_core::desk::{DeskGateway, DeskParts, SharedRisk};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskManager};
use baskfy_desk::lanes::twt::common::{Rec, Val};
use baskfy_desk::lanes::vbt::execute::{self as x, ExecError, ExecOutcome};
use baskfy_desk::lanes::vbt::store::{
    LineUpdate, PgVbtStore, SessionBump, StoreError, StoreResult, VbtStore,
};
use baskfy_desk::lanes::vbt::{self as v};
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Db, DbConfig, Param};
use baskfy_desk::wire::{PyDict, PyValue};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

const FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;

fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).unwrap()
}

fn now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 9, 21, 20, 0).unwrap()
}

fn session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 9).unwrap()
}

fn d(text: &str) -> Decimal {
    Decimal::from_str(text).unwrap()
}

fn settings() -> Settings {
    Settings::defaults().unwrap()
}

// =============================================================================================
// MemoryStore: the `VbtStore` protocol over dicts (Python's double, deliberately dumb)
// =============================================================================================

#[derive(Default)]
struct MemoryStore {
    plans: Vec<(String, Rec)>,
    lines: Vec<(i64, Rec)>,
    positions: Vec<(i64, Rec)>,
    orders: Vec<(i64, Rec)>,
    exit_orders: Vec<(i64, Rec)>,
    fills: Vec<Rec>,
    sessions: Vec<(NaiveDate, Rec)>,
    issues: Vec<Rec>,
    locked: Vec<NaiveDate>,
    unlocked: usize,
    busy: bool,
    next: i64,
}

fn find(rows: &mut [(i64, Rec)], id: i64) -> Option<&mut Rec> {
    rows.iter_mut().find(|(k, _)| *k == id).map(|(_, r)| r)
}

fn get(rows: &[(i64, Rec)], id: i64) -> Option<Rec> {
    rows.iter().find(|(k, _)| *k == id).map(|(_, r)| r.clone())
}

impl MemoryStore {
    fn id(&mut self) -> i64 {
        if self.next == 0 {
            self.next = 100;
        }
        self.next += 1;
        self.next
    }

    fn position(&self, id: i64) -> Rec {
        get(&self.positions, id).unwrap()
    }

    fn line_state(&self, id: i64) -> String {
        get(&self.lines, id).unwrap().s("state")
    }

    fn insert_position(&mut self, id: i64, rec: Rec) {
        let mut r = Rec::of([("id", Val::Int(id))]);
        r.update(&rec);
        self.positions.push((id, r));
    }

    fn insert_order(&mut self, id: i64, rec: Rec) {
        let mut r = Rec::of([("id", Val::Int(id))]);
        r.update(&rec);
        self.orders.push((id, r));
    }
}

#[async_trait]
impl VbtStore for MemoryStore {
    async fn plan(&mut self, plan_id: &str) -> StoreResult<Option<Rec>> {
        Ok(self
            .plans
            .iter()
            .find(|(k, _)| k == plan_id)
            .map(|(_, r)| r.clone()))
    }
    async fn line(&mut self, line_id: i64) -> StoreResult<Option<Rec>> {
        Ok(get(&self.lines, line_id))
    }
    async fn set_line(&mut self, line_id: i64, state: &str, extra: LineUpdate) -> StoreResult<()> {
        let row = find(&mut self.lines, line_id).ok_or(StoreError::Bad("KeyError".into()))?;
        row.set("state", Val::from(state));
        if let Some(j) = extra.journal_ref {
            row.set("journal_ref", Val::Str(j));
        }
        if let Some(o) = extra.order_id {
            row.set("order_id", Val::Int(o));
        }
        if let Some(p) = extra.position_id {
            row.set("position_id", Val::Int(p));
        }
        if let Some(n) = extra.note {
            row.set("note", Val::Str(n));
        }
        Ok(())
    }
    async fn open_position_for(&mut self, instrument_id: i64) -> StoreResult<Option<Rec>> {
        Ok(self
            .positions
            .iter()
            .map(|(_, r)| r)
            .find(|r| {
                r.int("instrument_id") == instrument_id
                    && r.s("state") == "OPEN"
                    && r.int("quantity_open") > 0
            })
            .cloned())
    }
    async fn position(&mut self, position_id: i64) -> StoreResult<Option<Rec>> {
        Ok(get(&self.positions, position_id))
    }
    async fn create_position(&mut self, fields: Rec) -> StoreResult<i64> {
        let id = self.id();
        let mut r = Rec::of([("id", Val::Int(id)), ("gtt_id", Val::None)]);
        r.update(&fields);
        self.positions.push((id, r));
        Ok(id)
    }
    async fn update_position(&mut self, position_id: i64, fields: Rec) -> StoreResult<()> {
        find(&mut self.positions, position_id)
            .ok_or(StoreError::Bad("KeyError".into()))?
            .update(&fields);
        Ok(())
    }
    async fn add_fill(&mut self, fields: Rec) -> StoreResult<i64> {
        let id = self.id();
        let mut r = Rec::of([("id", Val::Int(id))]);
        r.update(&fields);
        self.fills.push(r);
        Ok(id)
    }
    async fn order(&mut self, order_id: i64) -> StoreResult<Option<Rec>> {
        Ok(get(&self.orders, order_id))
    }
    async fn create_order(&mut self, fields: Rec) -> StoreResult<i64> {
        let id = self.id();
        let mut r = Rec::of([("id", Val::Int(id)), ("filled_quantity", Val::Int(0))]);
        r.update(&fields);
        self.orders.push((id, r));
        Ok(id)
    }
    async fn update_order(&mut self, order_id: i64, fields: Rec) -> StoreResult<()> {
        find(&mut self.orders, order_id)
            .ok_or(StoreError::Bad("KeyError".into()))?
            .update(&fields);
        Ok(())
    }
    async fn working_order_for(&mut self, instrument_id: i64) -> StoreResult<Option<Rec>> {
        Ok(self
            .orders
            .iter()
            .map(|(_, r)| r)
            .find(|r| {
                r.int("instrument_id") == instrument_id
                    && ["PROPOSED", "CONFIRMED", "SENT", "PARTIAL"].contains(&r.s("state").as_str())
            })
            .cloned())
    }
    async fn order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>> {
        Ok(self
            .orders
            .iter()
            .map(|(_, r)| r)
            .find(|r| r.s("broker_order_id") == broker_order_id)
            .cloned())
    }
    async fn open_orders(&mut self) -> StoreResult<Vec<Rec>> {
        Ok(Vec::new())
    }
    async fn open_positions(&mut self) -> StoreResult<Vec<Rec>> {
        Ok(self
            .positions
            .iter()
            .map(|(_, r)| r)
            .filter(|r| r.s("state") == "OPEN" && r.int("quantity_open") > 0)
            .map(|r| {
                let mut out = Rec::of([("symbol", Val::from("VBTCO"))]);
                out.update(r);
                out
            })
            .collect())
    }
    async fn open_protection_issues(&mut self) -> StoreResult<Vec<Rec>> {
        Ok(self.issues.clone())
    }
    async fn create_exit_order(&mut self, fields: Rec) -> StoreResult<i64> {
        let id = self.id();
        let mut r = Rec::of([
            ("id", Val::Int(id)),
            ("filled_quantity", Val::Int(0)),
            ("avg_fill_price", Val::None),
        ]);
        r.update(&fields);
        self.exit_orders.push((id, r));
        Ok(id)
    }
    async fn exit_order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>> {
        Ok(self
            .exit_orders
            .iter()
            .map(|(_, r)| r)
            .find(|r| r.s("broker_order_id") == broker_order_id)
            .cloned())
    }
    async fn update_exit_order(&mut self, exit_order_id: i64, fields: Rec) -> StoreResult<()> {
        find(&mut self.exit_orders, exit_order_id)
            .ok_or(StoreError::Bad("KeyError".into()))?
            .update(&fields);
        Ok(())
    }
    async fn line_by_client_id(&mut self, client_id: &str) -> StoreResult<Option<Rec>> {
        Ok(self
            .lines
            .iter()
            .map(|(_, r)| r)
            .find(|r| r.s("client_id") == client_id)
            .cloned())
    }
    async fn bump_session(&mut self, day: NaiveDate, bump: SessionBump) -> StoreResult<()> {
        if !self.sessions.iter().any(|(k, _)| *k == day) {
            self.sessions.push((
                day,
                Rec::of([
                    ("mode", Val::Str(bump.mode.clone())),
                    ("confirms", Val::Int(0)),
                    ("fills", Val::Int(0)),
                    ("exits", Val::Int(0)),
                ]),
            ));
        }
        let row = self
            .sessions
            .iter_mut()
            .find(|(k, _)| *k == day)
            .map(|(_, r)| r)
            .unwrap();
        row.set("mode", Val::Str(bump.mode));
        for (k, n) in [
            ("confirms", bump.confirms),
            ("fills", bump.fills),
            ("exits", bump.exits),
        ] {
            let was = row.int(k);
            row.set(k, Val::Int(was + n));
        }
        Ok(())
    }
    async fn lock_session_for_update(&mut self, day: NaiveDate) -> StoreResult<()> {
        if self.busy {
            return Err(StoreError::SessionBusy);
        }
        self.locked.push(day);
        Ok(())
    }
    async fn unlock_session(&mut self) -> StoreResult<()> {
        self.unlocked += 1;
        Ok(())
    }
    async fn order_for_signal(
        &mut self,
        instrument_id: i64,
        signal_date: NaiveDate,
    ) -> StoreResult<Option<i64>> {
        Ok(self
            .orders
            .iter()
            .find(|(_, r)| {
                r.int("instrument_id") == instrument_id
                    && r.date("signal_date") == Some(signal_date)
            })
            .map(|(id, _)| *id))
    }
    async fn entries_taken(&mut self, _day: NaiveDate) -> StoreResult<i64> {
        #[allow(clippy::cast_possible_wrap)]
        Ok(self
            .orders
            .iter()
            .filter(|(_, r)| ["CONFIRMED", "SENT", "FILLED"].contains(&r.s("state").as_str()))
            .count() as i64)
    }
}

fn a_store(kind: &str, overrides: &[(&str, Val)]) -> (MemoryStore, String, i64) {
    let mut store = MemoryStore::default();
    let plan_id = "6f1c2a9e-1d2b-4c3d-8e4f-0a1b2c3d4e5f".to_owned();
    store.plans.push((
        plan_id.clone(),
        Rec::of([
            ("id", Val::Int(1)),
            ("plan_id", Val::Str(plan_id.clone())),
            ("session_date", Val::Date(session())),
            ("source", Val::from("EVENING")),
            ("built_at", Val::Stamp(now() - Duration::minutes(5))),
            ("expires_at", Val::Stamp(now() + Duration::minutes(25))),
            ("gate", Val::from("OPEN")),
        ]),
    ));
    let line_id = 7;
    let mut line = Rec::of([
        ("id", Val::Int(line_id)),
        ("plan_pk", Val::Int(1)),
        ("plan_id", Val::Str(plan_id.clone())),
        ("kind", Val::from(kind)),
        ("instrument_id", Val::Int(42)),
        ("symbol", Val::from("VBTCO")),
        ("quantity", Val::Int(1_000)),
        ("limit_price", Val::Dec(d("96.00"))),
        ("stop_price", Val::Dec(d("84.45"))),
        ("value_inr", Val::Dec(d("96000.00"))),
        ("note", Val::from("")),
        ("state", Val::from("PROPOSED")),
        ("client_id", Val::Str(format!("{plan_id}:VBTCO:{kind}"))),
        ("order_id", Val::None),
        ("position_id", Val::None),
    ]);
    for (k, val) in overrides {
        line.set(*k, val.clone());
    }
    store.lines.push((line_id, line));
    (store, plan_id, line_id)
}

/// The real path: the desk's gateway deriving this sleeve's sibling, over the replay Kite.
struct Bed {
    _dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
    gateway: Arc<dyn Gateway>,
}

impl Bed {
    async fn new() -> Self {
        Self::with(settings()).await
    }

    /// The bed over the given settings (`monkeypatch.setattr(C, "DRY_RUN", ...)`).
    async fn with(base: Settings) -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("kite.json"), FIXTURE).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        std::fs::write(
            dir.path().join("instruments/NSE.csv"),
            "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n1,2,VBTCO,VBTCO,0,,0,0.05,1,EQ,NSE,NSE\n",
        )
        .unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        let clock: Arc<dyn Clock> = Arc::new(FixedClock::new(now()));
        let shared = Arc::new(RwLock::new(base.clone()));
        let db = Db::open(DbConfig {
            backend: "sqlite".to_owned(),
            schema: String::new(),
            database_url: String::new(),
            db_path: dir.path().join("desk.db"),
        })
        .await
        .unwrap();
        let writes: Arc<dyn KiteWrites> = kite.clone();
        let parts = DeskParts {
            kite: writes,
            settings: shared.clone(),
            clock: clock.clone(),
            ids: Arc::new(HarnessIds::new("vbt-test")),
            journal_path: dir.path().join("orders_journal.jsonl"),
            db: db.clone(),
        };
        let risk = Arc::new(
            RiskManager::new(RiskConfig::default(), clock.clone(), RiskBacking::Memory)
                .await
                .unwrap(),
        );
        let cell = Arc::new(SharedRisk::of(risk, shared.clone(), clock, db));
        let desk = DeskGateway::build_with_risk(parts, cell).unwrap();
        let s = base.clone();
        let gw = desk
            .derive_sleeve(SleeveGatewaySpec {
                derived: DerivedGateway {
                    gates: Arc::new(move || x::vbt_gates(&s)),
                    journal_path: dir.path().join(x::VBT_JOURNAL_NAME),
                    fo_coverage: false,
                },
                stop_band: x::vbt_stop_band(&base).unwrap(),
            })
            .expect("the desk gateway derives a sleeve");
        Self {
            _dir: dir,
            kite,
            gateway: gw,
        }
    }

    fn assert_nothing_reached_the_broker(&self) {
        assert!(
            self.kite.write_calls().is_empty(),
            "an order or GTT reached the broker: {:?}",
            self.kite.write_calls()
        );
    }
}

async fn confirm_with(
    bed: &Bed,
    store: &mut MemoryStore,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    at: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> Result<ExecOutcome, ExecError> {
    let out = x::execute_line(
        store,
        bed.gateway.as_ref(),
        &settings(),
        plan_id,
        line_id,
        confirm,
        at,
        last_price,
    )
    .await;
    bed.assert_nothing_reached_the_broker();
    out
}

async fn confirm(bed: &Bed, store: &mut MemoryStore, plan_id: &str, line_id: i64) -> ExecOutcome {
    confirm_with(bed, store, plan_id, line_id, "true", now(), None)
        .await
        .unwrap()
}

fn status_of(e: ExecError) -> (u16, String) {
    match e {
        ExecError::Http(h) => (h.status, h.detail),
        other => panic!("not an HTTPException: {other}"),
    }
}

fn held(instrument_id: i64) -> Rec {
    Rec::of([
        ("instrument_id", Val::Int(instrument_id)),
        ("state", Val::from("OPEN")),
        ("quantity_open", Val::Int(100)),
        ("stop_price", Val::Dec(d("80.00"))),
        ("entry_avg", Val::Dec(d("90.00"))),
        ("gtt_id", Val::from("g")),
    ])
}

// --- TestNothingFiresWithoutTheClick ---------------------------------------------------------

#[tokio::test]
async fn a_missing_confirm_is_a_four_hundred() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    let e = confirm_with(&bed, &mut store, &plan_id, line_id, "false", now(), None)
        .await
        .unwrap_err();
    assert_eq!(status_of(e).0, 400);
    assert!(store.orders.is_empty());
}

#[tokio::test]
async fn an_unknown_plan_is_a_four_oh_four() {
    let bed = Bed::new().await;
    let (mut store, _plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    let e = confirm_with(
        &bed,
        &mut store,
        "00000000-0000-4000-8000-000000000000",
        line_id,
        "true",
        now(),
        None,
    )
    .await
    .unwrap_err();
    assert_eq!(status_of(e).0, 404);
}

#[tokio::test]
async fn an_expired_plan_is_a_four_ten() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    let e = confirm_with(
        &bed,
        &mut store,
        &plan_id,
        line_id,
        "true",
        now() + Duration::minutes(31),
        None,
    )
    .await
    .unwrap_err();
    let (status, detail) = status_of(e);
    assert_eq!(status, 410);
    assert!(detail.contains("expired"));
}

#[tokio::test]
async fn a_line_confirmed_twice_is_a_four_oh_nine() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    confirm(&bed, &mut store, &plan_id, line_id).await;
    let e = confirm_with(&bed, &mut store, &plan_id, line_id, "true", now(), None)
        .await
        .unwrap_err();
    assert_eq!(status_of(e).0, 409);
}

#[tokio::test]
async fn the_whole_confirm_runs_under_the_session_lock() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(store.locked, vec![session()]);
}

// --- TestThePlaceLimit -----------------------------------------------------------------------

#[tokio::test]
async fn it_is_a_limit_buy_at_the_signal_close_and_rehearses_position_fill_and_stop() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    assert!(outcome.simulated);
    let order = store.orders[0].1.clone();
    assert_eq!(order.dec("limit_price"), Some(d("96.00")));
    assert_eq!(order.int("quantity"), 1_000);
    assert_eq!(order.val("simulated"), Val::Bool(true));
    let position = store.position(outcome.position_id.unwrap());
    assert_eq!(position.int("quantity_open"), 1_000);
    assert_eq!(position.dec("entry_avg"), Some(d("96.00")));
    assert_eq!(position.dec("initial_stop"), Some(d("84.45")));
    assert_eq!(position.val("simulated"), Val::Bool(true));
    let sides: Vec<String> = store.fills.iter().map(|f| f.s("side")).collect();
    assert_eq!(sides, vec!["BUY"]);
    // Non-negotiable 4: the stop is armed in the same request as the fill.
    assert!(outcome.gtt.is_some());
    assert_eq!(position.dec("gtt_trigger"), Some(d("84.45")));
    assert!(!position.is_none("gtt_armed_at"));
}

/// Non-negotiables 6 and 7 through THIS sleeve's own gateway (adversarial review): an SGB line
/// (the listed one and any `SGB*`) is refused at the lowest layer even in the dry-run rehearsal — nothing is
/// simulated, no `vb_order`/position is written, the line is rejected, and no broker call is made.
#[tokio::test]
async fn an_sgb_line_is_refused_by_the_sleeves_gateway_even_in_dry_run() {
    for symbol in ["SGBDE31III", "SGBJUN31I"] {
        let bed = Bed::new().await;
        let (mut store, plan_id, line_id) =
            a_store("PLACE_LIMIT", &[("symbol", Val::from(symbol))]);
        let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
        assert_eq!(outcome.status, "BLOCKED", "{symbol}: {}", outcome.reason);
        assert!(
            store.orders.is_empty(),
            "{symbol}: an order row was written"
        );
        assert!(
            store.positions.is_empty(),
            "{symbol}: a position was written"
        );
        assert_eq!(get(&store.lines, line_id).unwrap().s("state"), "REJECTED");
    }
}

#[tokio::test]
async fn a_fourth_entry_in_one_session_is_refused() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    for i in 0..3 {
        store.insert_order(
            900 + i,
            Rec::of([
                ("instrument_id", Val::Int(500 + i)),
                ("state", Val::from("SENT")),
            ]),
        );
    }
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.starts_with("SESSION_CAP"));
    assert_eq!(store.line_state(line_id), "REJECTED");
}

#[tokio::test]
async fn a_second_entry_in_a_held_name_is_allowed_and_a_third_is_refused() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    store.insert_position(1, held(42));
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);

    let (mut store2, plan_id2, line_id2) = a_store("PLACE_LIMIT", &[]);
    store2.insert_position(1, held(42));
    store2.insert_position(2, held(42));
    let outcome = confirm(&bed, &mut store2, &plan_id2, line_id2).await;
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.starts_with("ALREADY_HELD") && outcome.reason.contains("2 open entries")
    );
}

#[tokio::test]
async fn a_name_already_bid_for_is_refused() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    store.insert_order(
        1,
        Rec::of([
            ("instrument_id", Val::Int(42)),
            ("state", Val::from("SENT")),
        ]),
    );
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.starts_with("ALREADY_WORKING"));
}

#[tokio::test]
async fn protection_first_entries_second() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    let mut naked = held(77);
    naked.set("gtt_id", Val::None);
    store.insert_position(5, naked);
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.starts_with("PROTECTION_UNRESOLVED"),
        "{}",
        outcome.reason
    );
    assert!(store.orders.is_empty());
}

// --- TestTheBuyAtMarket ----------------------------------------------------------------------

#[tokio::test]
async fn a_market_buy_values_at_the_live_price_and_rehearses_the_stop() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("BUY_AT_MARKET", &[]);
    let outcome = confirm_with(
        &bed,
        &mut store,
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("97.50")),
    )
    .await
    .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    let order = store.orders[0].1.clone();
    assert_eq!(order.dec("limit_price"), Some(d("97.50")));
    assert_eq!(order.int("quantity"), 1_000);
    assert!((x::VBT_MARKET_PROTECTION - -1.0).abs() < f64::EPSILON);
    let position = store.position(outcome.position_id.unwrap());
    assert_eq!(position.dec("entry_avg"), Some(d("97.50")));
    assert_eq!(position.dec("gtt_trigger"), Some(d("84.45")));
    assert!(outcome.gtt.is_some());
}

#[tokio::test]
async fn without_a_live_price_a_market_buy_values_against_the_plan_preview() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("BUY_AT_MARKET", &[]);
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "SIMULATED");
    assert_eq!(store.orders[0].1.dec("limit_price"), Some(d("96.00")));
}

#[tokio::test]
async fn a_market_buy_respects_the_session_cap_and_the_held_cap() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("BUY_AT_MARKET", &[]);
    for i in 0..3 {
        store.insert_order(
            900 + i,
            Rec::of([
                ("instrument_id", Val::Int(500 + i)),
                ("state", Val::from("SENT")),
            ]),
        );
    }
    let outcome = confirm_with(
        &bed,
        &mut store,
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("97.50")),
    )
    .await
    .unwrap();
    assert!(outcome.status == "BLOCKED" && outcome.reason.contains("SESSION_CAP"));
    assert_eq!(store.orders.len(), 3);

    let (mut store, plan_id, line_id) = a_store("BUY_AT_MARKET", &[]);
    store.insert_position(1, held(42));
    store.insert_position(2, held(42));
    let outcome = confirm_with(
        &bed,
        &mut store,
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("97.50")),
    )
    .await
    .unwrap();
    assert!(outcome.status == "BLOCKED" && outcome.reason.contains("ALREADY_HELD"));
}

/// A gateway that records what it was asked and answers as the dry-run branch does.
#[derive(Default)]
struct Recording {
    placed: std::sync::Mutex<Vec<PlaceOrder>>,
    accept: bool,
}

#[async_trait]
impl Gateway for Recording {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        self.placed.lock().unwrap().push(order);
        let mut d = PyDict::new();
        if self.accept {
            d.insert("status", PyValue::Str("PLACED".into()));
            d.insert("order_id", PyValue::Str("BRK-VB-SELL".into()));
        } else {
            d.insert("status", PyValue::Str("DRY_RUN".into()));
            d.insert("order_id", PyValue::Str("dry".into()));
        }
        Ok(d)
    }
    async fn place_gtt_stop(&self, _stop: GttStop) -> Result<PyDict, GatewayError> {
        let mut d = PyDict::new();
        d.insert("status", PyValue::Str("DRY_RUN_GTT".into()));
        d.insert("gtt_id", PyValue::Str("DRY-2".into()));
        Ok(d)
    }
    async fn delete_gtt(&self, _req: DeleteGtt) -> Result<PyDict, GatewayError> {
        panic!("no GTT delete is expected here")
    }
    async fn modify_gtt_quantity(&self, _req: ModifyGtt) -> Result<PyDict, GatewayError> {
        let mut d = PyDict::new();
        d.insert("status", PyValue::Str("DRY_RUN_GTT_MODIFY".into()));
        Ok(d)
    }
    async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
        panic!("no cancel is expected here")
    }
}

#[tokio::test]
async fn the_gateway_is_asked_for_a_cnc_market_buy_with_kite_market_protection() {
    let (mut store, plan_id, line_id) = a_store("BUY_AT_MARKET", &[]);
    let gw = Recording::default();
    x::execute_line(
        &mut store,
        &gw,
        &settings(),
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("97.50")),
    )
    .await
    .unwrap();
    let seen = gw.placed.lock().unwrap()[0].clone();
    assert_eq!(seen.order_type, "MARKET");
    assert_eq!(seen.market_protection, Some(-1.0));
    assert_eq!((seen.side.as_str(), seen.product.as_str()), ("BUY", "CNC"));
    assert_eq!(seen.reference_price, Some(97.5));
}

#[tokio::test]
async fn a_place_limit_is_a_limit_at_the_signal_close_with_the_lines_client_id() {
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    let gw = Recording::default();
    x::execute_line(
        &mut store,
        &gw,
        &settings(),
        &plan_id,
        line_id,
        "true",
        now(),
        None,
    )
    .await
    .unwrap();
    let seen = gw.placed.lock().unwrap()[0].clone();
    assert_eq!(seen.order_type, "LIMIT");
    assert_eq!(seen.price, Some(96.0));
    assert_eq!(seen.product, "CNC");
    assert_eq!(seen.client_id, Some(format!("{plan_id}:VBTCO:PLACE_LIMIT")));
    assert!(seen.tenant.is_some() && seen.tenant == seen.plan_tenant);
}

// --- TestTheSell -----------------------------------------------------------------------------

fn holding(quantity: i64) -> Rec {
    Rec::of([
        ("instrument_id", Val::Int(42)),
        ("symbol", Val::from("VBTCO")),
        ("state", Val::from("OPEN")),
        ("quantity_open", Val::Int(quantity)),
        ("quantity_entered", Val::Int(quantity)),
        ("entry_avg", Val::Dec(d("96.00"))),
        ("initial_stop", Val::Dec(d("84.45"))),
        ("stop_price", Val::Dec(d("84.45"))),
        ("gtt_id", Val::from("gtt-1")),
    ])
}

#[tokio::test]
async fn a_sell_closes_the_position() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("SELL_AT_OPEN", &[]);
    store.insert_position(1, holding(1_000));
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    let p = store.position(1);
    assert_eq!(p.int("quantity_open"), 0);
    assert_eq!(p.s("state"), "CLOSED");
    assert_eq!(p.s("close_reason"), "EMA_EXIT");
    let sides: Vec<String> = store.fills.iter().map(|f| f.s("side")).collect();
    assert_eq!(sides, vec!["SELL"]);
}

#[tokio::test]
async fn a_sell_for_a_name_the_sleeve_does_not_own_is_blocked() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("SELL_AT_OPEN", &[]);
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.contains("did not buy"));
    assert!(store.fills.is_empty());
}

#[tokio::test]
async fn a_sell_for_more_than_it_owns_is_blocked() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("SELL_AT_OPEN", &[]);
    store.insert_position(1, holding(400));
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.contains("sells 1000"));
    assert_eq!(store.position(1).int("quantity_open"), 400);
}

// --- TestTheCancel / TestTheReArm ------------------------------------------------------------

#[tokio::test]
async fn a_cancel_is_the_only_way_a_working_order_leaves_the_book() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("CANCEL_LIMIT", &[]);
    store.insert_order(
        1,
        Rec::of([
            ("instrument_id", Val::Int(42)),
            ("state", Val::from("SENT")),
            ("broker_order_id", Val::None),
        ]),
    );
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "SIMULATED");
    let o = get(&store.orders, 1).unwrap();
    assert_eq!(o.s("state"), "CANCELLED");
    assert_eq!(o.s("cancel_reason"), "EXPIRY_SWEEP");
}

#[tokio::test]
async fn nothing_to_cancel_is_blocked_rather_than_silent() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("CANCEL_LIMIT", &[]);
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.contains("no working order"));
}

#[tokio::test]
async fn a_naked_position_gets_its_stop_back() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("ARM_GTT", &[]);
    let mut p = holding(500);
    p.set("gtt_id", Val::None);
    store.insert_position(1, p);
    let outcome = confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "SIMULATED");
    assert!(outcome.gtt.is_some());
    assert_eq!(store.position(1).dec("gtt_trigger"), Some(d("84.45")));
}

// --- TestTheLaw ------------------------------------------------------------------------------

#[test]
fn the_flag_is_false_and_a_confirm_is_simulated() {
    let s = settings();
    assert!(!s.vbt_execution_enabled);
    assert!(x::vbt_gates(&s).dry_run);
    assert!(x::is_simulated(&s));
    // Out of DRY_RUN, the sleeve's flag alone still keeps it simulated (Track B).
    let mut live = settings();
    live.dry_run = false;
    assert!(x::vbt_gates(&live).dry_run);
    live.vbt_execution_enabled = true;
    assert!(!x::vbt_gates(&live).dry_run);
}

#[test]
fn this_sleeve_can_never_place_an_intraday_or_option_order() {
    let mut s = settings();
    s.intraday_enabled = true;
    s.options_enabled = true;
    let gates = x::vbt_gates(&s);
    assert!(!gates.intraday_enabled && !gates.options_enabled && !gates.fno_carry_enabled);
}

#[test]
fn there_are_six_executable_kinds_and_none_of_them_shorts() {
    let mut kinds = x::EXECUTABLE_KINDS.to_vec();
    kinds.sort_unstable();
    assert_eq!(
        kinds,
        vec![
            "ARM_GTT",
            "BUY_AT_MARKET",
            "CANCEL_LIMIT",
            "PLACE_LIMIT",
            "RAISE_GTT_STOP",
            "SELL_AT_OPEN"
        ]
    );
}

fn lane_source() -> String {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes");
    let mut text = std::fs::read_to_string(root.join("vbt.rs")).unwrap();
    for f in ["execute.rs", "store.rs"] {
        text += &std::fs::read_to_string(root.join("vbt").join(f)).unwrap();
    }
    text
}

#[test]
fn the_lane_names_no_broker_method() {
    // Law 2: every order goes through the gateway the lane is handed.
    let source = lane_source();
    for forbidden in [
        "KiteWrites",
        "place_order",
        "kc.",
        ".place_gtt(",
        "fake_kite",
    ] {
        assert!(!source.contains(forbidden), "{forbidden}");
    }
}

#[test]
fn no_auto_execute_setting_is_referenced_anywhere() {
    // DECISIONS-VB PACK.2 — non-negotiable 1's exceptions are the swing, TWT and F3 books'.
    let source = lane_source().to_uppercase();
    assert!(!source.contains("AUTO_EXECUTE"));
}

// --- TestTheRaise ----------------------------------------------------------------------------

fn raisable(gtt_id: Option<&str>) -> Rec {
    Rec::of([
        ("instrument_id", Val::Int(42)),
        ("symbol", Val::from("VBTCO")),
        ("state", Val::from("OPEN")),
        ("quantity_open", Val::Int(500)),
        ("entry_avg", Val::Dec(d("96.00"))),
        ("initial_stop", Val::Dec(d("84.45"))),
        ("stop_price", Val::Dec(d("84.45"))),
        ("gtt_id", gtt_id.map_or(Val::None, Val::from)),
        (
            "gtt_trigger",
            if gtt_id.is_some() {
                Val::Dec(d("84.45"))
            } else {
                Val::None
            },
        ),
        ("partial_done", Val::Bool(false)),
        ("trail", Val::from("MA20")),
    ])
}

#[tokio::test]
async fn a_raise_moves_the_resting_stop_to_the_lines_trigger() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) =
        a_store("RAISE_GTT_STOP", &[("stop_price", Val::Dec(d("96.00")))]);
    store.insert_position(1, raisable(Some("DRY-1")));
    let outcome = confirm_with(
        &bed,
        &mut store,
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("110.00")),
    )
    .await
    .unwrap();
    assert!(
        outcome.status == "SIMULATED" && outcome.gtt.is_some(),
        "{}",
        outcome.reason
    );
    assert_eq!(store.position(1).dec("stop_price"), Some(d("96.00")));
    assert_eq!(store.position(1).dec("gtt_trigger"), Some(d("96.00")));
    assert_eq!(store.line_state(line_id), "FILLED");
}

#[tokio::test]
async fn a_stop_never_falls() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) =
        a_store("RAISE_GTT_STOP", &[("stop_price", Val::Dec(d("80.00")))]);
    store.insert_position(1, raisable(Some("DRY-1")));
    let outcome = confirm_with(
        &bed,
        &mut store,
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("110.00")),
    )
    .await
    .unwrap();
    assert!(outcome.status == "BLOCKED" && outcome.reason.contains("never falls"));
    assert_eq!(store.position(1).dec("gtt_trigger"), Some(d("84.45")));
}

#[tokio::test]
async fn a_trigger_at_or_above_the_market_is_refused() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) =
        a_store("RAISE_GTT_STOP", &[("stop_price", Val::Dec(d("96.00")))]);
    store.insert_position(1, raisable(Some("DRY-1")));
    let outcome = confirm_with(
        &bed,
        &mut store,
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("95.00")),
    )
    .await
    .unwrap();
    assert!(outcome.status == "BLOCKED" && outcome.reason.contains("fire at once"));
}

#[tokio::test]
async fn the_raise_acts_on_the_position_it_names() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store(
        "RAISE_GTT_STOP",
        &[
            ("stop_price", Val::Dec(d("96.00"))),
            ("position_id", Val::Int(2)),
        ],
    );
    store.insert_position(1, raisable(Some("DRY-1")));
    let mut second = raisable(Some("DRY-1"));
    second.set("stop_price", Val::Dec(d("90.00")));
    second.set("gtt_trigger", Val::Dec(d("90.00")));
    store.insert_position(2, second);
    confirm_with(
        &bed,
        &mut store,
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("110.00")),
    )
    .await
    .unwrap();
    assert_eq!(store.position(2).dec("stop_price"), Some(d("96.00")));
    assert_eq!(store.position(1).dec("stop_price"), Some(d("84.45")));
}

// --- TestThePartialSale ----------------------------------------------------------------------

#[tokio::test]
async fn a_partial_fill_from_the_broker_marks_the_partial_done_and_keeps_the_position() {
    let (mut store, plan_id, line_id) = a_store(
        "SELL_AT_OPEN",
        &[
            ("quantity", Val::Int(300)),
            ("reason", Val::from("PARTIAL")),
        ],
    );
    let mut p = raisable(Some("DRY-1"));
    p.set("quantity_open", Val::Int(900));
    p.set("exit_queued_for", Val::None);
    p.set("exit_reason_queued", Val::None);
    store.insert_position(1, p);
    let gw = Recording {
        accept: true,
        ..Recording::default()
    };
    let outcome = x::execute_line(
        &mut store,
        &gw,
        &settings(),
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("110.00")),
    )
    .await
    .unwrap();
    assert_eq!(outcome.status, "SENT");
    assert_eq!(store.exit_orders[0].1.s("reason"), "PARTIAL");
    assert!(
        store.position(1).is_none("exit_queued_for"),
        "a partial is not a full exit"
    );

    let mut payload = PyDict::new();
    payload.insert("order_id", PyValue::Str("BRK-VB-SELL".into()));
    payload.insert("status", PyValue::Str("COMPLETE".into()));
    payload.insert("filled_quantity", PyValue::Int(300));
    payload.insert("average_price", PyValue::Float(111.0));
    x::on_order_update(&mut store, &gw, &settings(), &payload, now())
        .await
        .unwrap();
    let position = store.position(1);
    assert_eq!(position.int("quantity_open"), 600);
    assert_eq!(position.s("state"), "OPEN");
    assert_eq!(position.val("partial_done"), Val::Bool(true));
}

// --- the page's pure pieces ------------------------------------------------------------------

#[test]
fn the_price_text_is_rupees_grouped_to_two_places() {
    assert_eq!(v::price_text(None), "\u{2014}");
    assert_eq!(v::price_text(Some(d("96000"))), "\u{20b9}96,000.00");
    assert_eq!(
        v::price_text(Some(d("1234567.125"))),
        "\u{20b9}1,234,567.12"
    );
    assert_eq!(v::price_text(Some(d("84.45"))), "\u{20b9}84.45");
    assert_eq!(v::price_text(Some(d("-1500.5"))), "\u{20b9}-1,500.50");
}

#[test]
fn the_countdown_says_when_a_plan_cannot_be_confirmed() {
    assert_eq!(v::countdown(None, &now()), "no expiry");
    assert_eq!(
        v::countdown(Some(now() + Duration::seconds(125)), &now()),
        "2m 5s left"
    );
    assert_eq!(
        v::countdown(Some(now() - Duration::seconds(1)), &now()),
        "expired \u{2014} rebuild the plan"
    );
}

#[test]
fn an_expired_line_is_not_confirmable() {
    let line = Rec::of([
        ("kind", Val::from("PLACE_LIMIT")),
        ("state", Val::from("PROPOSED")),
        ("expires_at", Val::Stamp(now() - Duration::minutes(1))),
        ("limit_price", Val::Dec(d("96.00"))),
        ("stop_price", Val::None),
        ("value_inr", Val::None),
    ]);
    let view = v::line_view(&line, &now());
    assert_eq!(view.val("confirmable"), Val::Bool(false));
    assert_eq!(view.val("expired"), Val::Bool(true));
    assert_eq!(view.s("label"), "VBT BUY LIMIT");
    assert_eq!(view.s("stop_text"), "\u{2014}");
}

#[test]
fn a_position_with_no_gtt_is_naked_in_red() {
    let mut p = holding(10);
    p.set("gtt_id", Val::None);
    p.set("exit_queued_for", Val::None);
    let view = v::position_view(&p).unwrap();
    assert_eq!(view.val("naked"), Val::Bool(true));
    assert_eq!(view.s("risk_text"), "\u{20b9}115.50");
    assert_eq!(view.val("sells_tomorrow"), Val::Bool(false));
}

#[test]
fn a_working_order_on_its_last_session_expires_tonight() {
    let o = Rec::of([
        ("sessions_worked", Val::Int(2)),
        ("quantity", Val::Int(100)),
        ("filled_quantity", Val::Int(40)),
        ("limit_price", Val::Dec(d("10"))),
    ]);
    let view = v::working_view(&o, 3);
    assert_eq!(view.s("sessions_text"), "2 of 3");
    assert_eq!(view.val("expires_tonight"), Val::Bool(true));
    assert_eq!(view.int("remaining"), 60);
}

// =============================================================================================
// PgVbtStore over SQLite: the scan request and the store's reads
// =============================================================================================

const DDL: &[&str] = &[
    "CREATE TABLE instrument (id INTEGER PRIMARY KEY, symbol TEXT)",
    "CREATE TABLE vb_scan_run (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, requested_at TEXT, started_at TEXT, finished_at TEXT, session_date TEXT, status TEXT, source TEXT, detail TEXT, error TEXT, task_id TEXT)",
    "CREATE TABLE vb_plan (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, user_id INTEGER, session_date TEXT, source TEXT, built_at TEXT, expires_at TEXT, gate TEXT, sleeve_equity_inr NUMERIC, total_new_exposure_inr NUMERIC)",
    "CREATE TABLE vb_plan_line (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER, kind TEXT, state TEXT, quantity INTEGER, limit_price NUMERIC, stop_price NUMERIC, value_inr NUMERIC, size_cap TEXT, reason TEXT, note TEXT, client_id TEXT, journal_ref TEXT, order_id INTEGER, position_id INTEGER)",
    "CREATE TABLE vb_session (user_id INTEGER, session_date TEXT, mode TEXT DEFAULT 'DRY_RUN', gate TEXT DEFAULT 'SHUT', signals INTEGER DEFAULT 0, confirms INTEGER DEFAULT 0, fills INTEGER DEFAULT 0, exits INTEGER DEFAULT 0, PRIMARY KEY (user_id, session_date))",
    "CREATE TABLE vb_order (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER, instrument_id INTEGER, signal_date TEXT, limit_price NUMERIC, stop_price NUMERIC, quantity INTEGER, state TEXT, working_from TEXT, expires_after_session TEXT, sessions_worked INTEGER DEFAULT 0, broker_order_id TEXT, client_id TEXT, filled_quantity INTEGER DEFAULT 0, avg_fill_price NUMERIC, position_id INTEGER, cancelled_on TEXT, cancel_reason TEXT, simulated INTEGER DEFAULT 1, created_at TEXT)",
];

async fn sqlite_store() -> (tempfile::TempDir, PgVbtStore) {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("vbt.db"))
        .await
        .unwrap();
    for stmt in DDL {
        conn.execute(stmt, &[]).await.unwrap();
    }
    conn.execute(
        "INSERT INTO instrument (id, symbol) VALUES (42, 'VBTCO')",
        &[],
    )
    .await
    .unwrap();
    (dir, PgVbtStore::new(conn, 1, "", Some(1)))
}

fn stamp(t: DateTime<FixedOffset>) -> String {
    t.format("%Y-%m-%d %H:%M:%S%.f%:z").to_string()
}

#[tokio::test]
async fn a_rescan_writes_one_queued_row_from_the_desk() {
    let (_d, mut store) = sqlite_store().await;
    let id = store.request_scan(now(), None, None).await.unwrap();
    let rows = store
        .conn
        .execute("SELECT user_id, status, source FROM vb_scan_run", &[])
        .await
        .unwrap()
        .rows;
    assert_eq!(rows.len(), 1);
    assert_eq!(
        rows[0].get("status").and_then(|v| v.as_str()),
        Some("QUEUED")
    );
    assert_eq!(rows[0].get("source").and_then(|v| v.as_str()), Some("desk"));
    let run = store.scan_run(id).await.unwrap().unwrap();
    assert_eq!(run.s("status"), "QUEUED");
}

#[tokio::test]
async fn one_in_flight_is_a_four_oh_nine_and_one_a_minute_a_four_two_nine() {
    let (_d, mut store) = sqlite_store().await;
    let first = store.request_scan(now(), None, None).await.unwrap();
    match store
        .request_scan(now() + Duration::seconds(5), None, None)
        .await
    {
        Err(StoreError::Scan(r)) => {
            assert_eq!(
                (r.status, r.run_id, r.run_status.as_str()),
                (409, first, "QUEUED")
            );
            assert_eq!(
                r.reason,
                format!("Scan {first} is queued; its result is on its way.")
            );
        }
        other => panic!("{other:?}"),
    }
    store
        .conn
        .execute("UPDATE vb_scan_run SET status = 'DONE'", &[])
        .await
        .unwrap();
    match store
        .request_scan(now() + Duration::seconds(20), None, None)
        .await
    {
        Err(StoreError::Scan(r)) => {
            assert_eq!(r.status, 429);
            assert_eq!(r.retry_after, Some(40));
            assert_eq!(
                r.reason,
                "A scan was requested 20 s ago; one a minute is the limit. Try again in 40 s."
            );
        }
        other => panic!("{other:?}"),
    }
    assert!(
        store
            .request_scan(now() + Duration::seconds(61), None, None)
            .await
            .is_ok()
    );
}

#[tokio::test]
async fn a_stale_in_flight_run_does_not_block_a_new_one() {
    let (_d, mut store) = sqlite_store().await;
    store.request_scan(now(), None, None).await.unwrap();
    assert!(
        store
            .request_scan(now() + Duration::seconds(601), None, None)
            .await
            .is_ok()
    );
}

#[test]
fn the_desk_and_the_worker_agree_on_the_two_numbers() {
    use baskfy_desk::lanes::vbt::store as st;
    assert_eq!(st::RESCAN_STALE_AFTER_SECONDS, 600);
    assert_eq!(st::RESCAN_MIN_INTERVAL_SECONDS, 60);
    assert_eq!(st::RESCAN_IN_FLIGHT, ["QUEUED", "RUNNING"]);
}

#[tokio::test]
async fn a_malformed_plan_id_is_none_not_a_driver_error() {
    let (_d, mut store) = sqlite_store().await;
    assert!(store.plan("not-a-uuid").await.unwrap().is_none());
}

#[tokio::test]
async fn the_store_reads_lines_in_kind_order_with_decimals() {
    let (_d, mut store) = sqlite_store().await;
    let plan_id = "00000000-0000-4000-8000-000000000001";
    store
        .conn
        .execute(
            "INSERT INTO vb_plan (plan_id, user_id, session_date, source, built_at, expires_at, gate, sleeve_equity_inr, total_new_exposure_inr) VALUES (?, 1, '2026-09-09', 'EVENING', ?, ?, 'OPEN', '1000000.00', '96000.00')",
            &[
                Param::Text(plan_id.into()),
                Param::Text(stamp(now())),
                Param::Text(stamp(now() + Duration::minutes(30))),
            ],
        )
        .await
        .unwrap();
    for kind in ["PLACE_LIMIT", "SELL_AT_OPEN", "ARM_GTT"] {
        store
            .conn
            .execute(
                "INSERT INTO vb_plan_line (plan_id, user_id, instrument_id, kind, state, quantity, limit_price, stop_price, value_inr, note, client_id) VALUES (1, 1, 42, ?, 'PROPOSED', 10, '96.00', '84.45', '960.00', '', ?)",
                &[Param::Text(kind.into()), Param::Text(format!("{plan_id}:VBTCO:{kind}"))],
            )
            .await
            .unwrap();
    }
    let plan = store.plan(plan_id).await.unwrap().unwrap();
    assert_eq!(plan.date("session_date"), Some(session()));
    let lines = store.lines_for(plan.int("id")).await.unwrap();
    let kinds: Vec<String> = lines.iter().map(|l| l.s("kind")).collect();
    assert_eq!(kinds, vec!["SELL_AT_OPEN", "ARM_GTT", "PLACE_LIMIT"]);
    assert_eq!(lines[0].dec("limit_price"), Some(d("96.00")));
    assert_eq!(
        store
            .todays_plan(session())
            .await
            .unwrap()
            .unwrap()
            .s("plan_id"),
        plan_id
    );
    // The lock inserts the day's session row.
    store.lock_session_for_update(session()).await.unwrap();
    assert!(store.session(session()).await.unwrap().is_some());
}

/// `line_id: int = Form(...)` is unbounded in Python: a well-formed id past `i64` is printed as
/// `str(int(raw))` in the 404, and anything that is not an integer stays a 422.
#[test]
fn a_line_id_past_i64_is_python_int_text_and_garbage_is_not() {
    assert_eq!(
        v::unbounded_int_text("99999999999999999999").as_deref(),
        Some("99999999999999999999")
    );
    assert_eq!(
        v::unbounded_int_text(" -000_99999999999999999999 ").as_deref(),
        Some("-99999999999999999999")
    );
    assert_eq!(v::unbounded_int_text("+0132.00").as_deref(), Some("132"));
    assert_eq!(v::unbounded_int_text("-0").as_deref(), Some("0"));
    for bad in ["1.5", "abc", "", "1__0", "_1", "1_", "1e3", "--1"] {
        assert_eq!(v::unbounded_int_text(bad), None, "{bad:?}");
    }
}

// =============================================================================================
// R8.VB2 — one entry per name per signal, refused before the gateway (`TestOneEntryPerSignal`)
// =============================================================================================

/// A gateway that counts every call and answers as the dry-run branch would.
#[derive(Default)]
struct Counting {
    calls: std::sync::Mutex<Vec<String>>,
}

impl Counting {
    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }
}

#[async_trait]
impl Gateway for Counting {
    async fn place(&self, _order: PlaceOrder) -> Result<PyDict, GatewayError> {
        self.calls.lock().unwrap().push("place".into());
        Ok(baskfy_desk::wire::dict([
            ("status", PyValue::Str("DRY_RUN".into())),
            ("order_id", PyValue::Str("dry".into())),
        ]))
    }
    async fn place_gtt_stop(&self, _stop: GttStop) -> Result<PyDict, GatewayError> {
        self.calls.lock().unwrap().push("place_gtt_stop".into());
        Ok(baskfy_desk::wire::dict([
            ("status", PyValue::Str("DRY_RUN_GTT".into())),
            ("trigger_id", PyValue::Str("g".into())),
        ]))
    }
    async fn delete_gtt(&self, _req: DeleteGtt) -> Result<PyDict, GatewayError> {
        self.calls.lock().unwrap().push("delete_gtt".into());
        Ok(PyDict::default())
    }
    async fn modify_gtt_quantity(&self, _req: ModifyGtt) -> Result<PyDict, GatewayError> {
        self.calls
            .lock()
            .unwrap()
            .push("modify_gtt_quantity".into());
        Ok(PyDict::default())
    }
    async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
        self.calls.lock().unwrap().push("cancel_order".into());
        Ok(PyDict::default())
    }
}

fn taken_on(store: &mut MemoryStore, state: &str, signal: NaiveDate) {
    store.insert_order(
        900,
        Rec::of([
            ("instrument_id", Val::Int(42)),
            ("signal_date", Val::Date(signal)),
            ("state", Val::from(state)),
        ]),
    );
}

#[tokio::test]
async fn a_second_order_on_the_same_signal_is_refused_before_the_gateway() {
    for kind in ["PLACE_LIMIT", "BUY_AT_MARKET"] {
        for state in ["FILLED", "CANCELLED", "EXPIRED", "REJECTED"] {
            let (mut store, plan_id, line_id) = a_store(kind, &[]);
            taken_on(&mut store, state, session());
            let gw = Counting::default();
            let outcome = x::execute_line(
                &mut store,
                &gw,
                &settings(),
                &plan_id,
                line_id,
                "true",
                now(),
                Some(d("97.50")),
            )
            .await
            .unwrap();
            assert_eq!(outcome.status, "BLOCKED", "{kind} {state}");
            assert!(
                outcome.reason.starts_with("ALREADY_SENT") && outcome.reason.contains("order 900"),
                "{}",
                outcome.reason
            );
            assert_eq!(
                outcome.reason,
                "ALREADY_SENT: order 900 already took this name on the 2026-09-09 signal; \
                 one entry per name per signal"
            );
            assert!(gw.calls().is_empty(), "nothing may reach the gateway");
            assert_eq!(store.line_state(line_id), "REJECTED");
            assert_eq!(store.orders.len(), 1);
        }
    }
}

#[tokio::test]
async fn a_fresh_signal_in_the_same_name_is_a_fresh_entry() {
    let bed = Bed::new().await;
    for kind in ["PLACE_LIMIT", "BUY_AT_MARKET"] {
        let (mut store, plan_id, line_id) = a_store(kind, &[]);
        taken_on(&mut store, "CANCELLED", session() - Duration::days(1));
        let outcome = confirm_with(
            &bed,
            &mut store,
            &plan_id,
            line_id,
            "true",
            now(),
            Some(d("97.50")),
        )
        .await
        .unwrap();
        assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    }
}

#[tokio::test]
async fn a_store_that_cannot_answer_the_signal_question_refuses_the_entry() {
    // The protocol's default fails closed: an entry is never sent unasked.
    struct Silent(MemoryStore);
    #[async_trait]
    impl VbtStore for Silent {
        async fn plan(&mut self, p: &str) -> StoreResult<Option<Rec>> {
            self.0.plan(p).await
        }
        async fn line(&mut self, l: i64) -> StoreResult<Option<Rec>> {
            self.0.line(l).await
        }
        async fn set_line(&mut self, l: i64, s: &str, e: LineUpdate) -> StoreResult<()> {
            self.0.set_line(l, s, e).await
        }
        async fn open_position_for(&mut self, i: i64) -> StoreResult<Option<Rec>> {
            self.0.open_position_for(i).await
        }
        async fn position(&mut self, p: i64) -> StoreResult<Option<Rec>> {
            VbtStore::position(&mut self.0, p).await
        }
        async fn create_position(&mut self, f: Rec) -> StoreResult<i64> {
            self.0.create_position(f).await
        }
        async fn update_position(&mut self, p: i64, f: Rec) -> StoreResult<()> {
            self.0.update_position(p, f).await
        }
        async fn add_fill(&mut self, f: Rec) -> StoreResult<i64> {
            self.0.add_fill(f).await
        }
        async fn order(&mut self, o: i64) -> StoreResult<Option<Rec>> {
            self.0.order(o).await
        }
        async fn create_order(&mut self, f: Rec) -> StoreResult<i64> {
            self.0.create_order(f).await
        }
        async fn update_order(&mut self, o: i64, f: Rec) -> StoreResult<()> {
            self.0.update_order(o, f).await
        }
        async fn working_order_for(&mut self, i: i64) -> StoreResult<Option<Rec>> {
            self.0.working_order_for(i).await
        }
        async fn order_by_broker_id(&mut self, b: &str) -> StoreResult<Option<Rec>> {
            self.0.order_by_broker_id(b).await
        }
        async fn open_orders(&mut self) -> StoreResult<Vec<Rec>> {
            self.0.open_orders().await
        }
        async fn open_positions(&mut self) -> StoreResult<Vec<Rec>> {
            self.0.open_positions().await
        }
        async fn open_protection_issues(&mut self) -> StoreResult<Vec<Rec>> {
            self.0.open_protection_issues().await
        }
        async fn create_exit_order(&mut self, f: Rec) -> StoreResult<i64> {
            self.0.create_exit_order(f).await
        }
        async fn exit_order_by_broker_id(&mut self, b: &str) -> StoreResult<Option<Rec>> {
            self.0.exit_order_by_broker_id(b).await
        }
        async fn update_exit_order(&mut self, e: i64, f: Rec) -> StoreResult<()> {
            self.0.update_exit_order(e, f).await
        }
        async fn line_by_client_id(&mut self, c: &str) -> StoreResult<Option<Rec>> {
            self.0.line_by_client_id(c).await
        }
        async fn bump_session(&mut self, d: NaiveDate, b: SessionBump) -> StoreResult<()> {
            self.0.bump_session(d, b).await
        }
        async fn lock_session_for_update(&mut self, d: NaiveDate) -> StoreResult<()> {
            self.0.lock_session_for_update(d).await
        }
        async fn entries_taken(&mut self, d: NaiveDate) -> StoreResult<i64> {
            self.0.entries_taken(d).await
        }
    }
    let (inner, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    let mut store = Silent(inner);
    let gw = Counting::default();
    let out = x::execute_line(
        &mut store,
        &gw,
        &settings(),
        &plan_id,
        line_id,
        "true",
        now(),
        None,
    )
    .await;
    assert!(out.is_err(), "{out:?}");
    assert!(gw.calls().is_empty());
}

// =============================================================================================
// R8.VB1 — the lock's exit, and a held lock is a 409 before anything is read or sent
// =============================================================================================

#[tokio::test]
async fn every_confirm_leaves_the_lock_on_the_way_out_even_a_refused_one() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(store.unlocked, 1);
    // A refusal inside the block (the line is no longer PROPOSED) is a 409 before the lock,
    // and the exit still runs exactly once per call.
    let _ = confirm_with(&bed, &mut store, &plan_id, line_id, "true", now(), None).await;
    assert_eq!(store.unlocked, 2);
}

#[tokio::test]
async fn a_held_session_lock_is_a_409_and_nothing_is_sent_or_written() {
    let (mut store, plan_id, line_id) = a_store("PLACE_LIMIT", &[]);
    store.busy = true;
    let gw = Counting::default();
    let e = x::execute_line(
        &mut store,
        &gw,
        &settings(),
        &plan_id,
        line_id,
        "true",
        now(),
        None,
    )
    .await
    .unwrap_err();
    let (status, detail) = status_of(e);
    assert_eq!(status, 409);
    assert_eq!(detail, baskfy_desk::lanes::vbt::store::SESSION_BUSY);
    assert!(detail.starts_with("SESSION_BUSY"));
    assert!(gw.calls().is_empty());
    assert_eq!(
        store.line_state(line_id),
        "PROPOSED",
        "it can be confirmed again"
    );
    assert!(store.orders.is_empty());
}

#[tokio::test]
async fn a_broker_report_meeting_a_held_lock_is_a_409_too() {
    let (mut store, _plan_id, _line_id) = a_store("PLACE_LIMIT", &[]);
    store.insert_order(
        900,
        Rec::of([
            ("instrument_id", Val::Int(42)),
            ("state", Val::from("SENT")),
            ("broker_order_id", Val::from("B1")),
            ("quantity", Val::Int(10)),
        ]),
    );
    store.busy = true;
    let gw = Counting::default();
    let payload = baskfy_desk::wire::dict([
        ("order_id", PyValue::Str("B1".into())),
        ("status", PyValue::Str("COMPLETE".into())),
        ("filled_quantity", PyValue::Int(10)),
        ("average_price", PyValue::Float(96.0)),
    ]);
    let e = x::on_order_update(&mut store, &gw, &settings(), &payload, now())
        .await
        .unwrap_err();
    assert_eq!(status_of(e).0, 409);
    assert_eq!(store.unlocked, 1);
    assert!(gw.calls().is_empty());
}

// =============================================================================================
// The exact-expiry instant: Python's clock is microseconds, half-even
// =============================================================================================

#[test]
fn the_desk_clock_rounds_to_microseconds_half_even_as_cpython_does() {
    // 2026-10-21 09:30:00.355993 IST, the case that went red at nanosecond precision.
    let expires = ist()
        .with_ymd_and_hms(2026, 10, 21, 9, 30, 0)
        .unwrap()
        .checked_add_signed(Duration::microseconds(355_993))
        .unwrap();
    let t = baskfy_desk::clock::epoch_seconds(&expires);
    let now = v::py_fromtimestamp_ist(t);
    assert_eq!(
        now, expires,
        "at the stored microsecond the plan is still live"
    );
    assert_eq!(now.timestamp_subsec_nanos() % 1_000, 0);
    // CPython's own answers (`datetime.fromtimestamp(t, timezone.utc).microsecond`), which
    // round the float's fraction half-even: 2.5e-6 → 2 and 1.25e-5 → 12, never up.
    let at = |s: f64| v::py_fromtimestamp_ist(s).timestamp_subsec_micros();
    assert_eq!(at(1.000_000_5), 1);
    assert_eq!(at(1.000_001_5), 1);
    assert_eq!(at(2.5e-6), 2);
    assert_eq!(at(1.25e-5), 12);
    assert_eq!(at(1.999_999_9), 0, "carries into the next second");
    assert_eq!(v::py_fromtimestamp_ist(1.999_999_9).timestamp(), 2);
    assert_eq!(
        v::py_fromtimestamp_ist(-0.25).timestamp_subsec_micros(),
        750_000
    );
    assert_eq!(v::py_fromtimestamp_ist(-0.25).timestamp(), -1);
}

// =============================================================================================
// test_vbt_safety.py — TestNothingReachesABroker, TestTheTwoWaysThisLosesMoneyQuietly
// =============================================================================================

/// `_confirm_every_kind(spy)`: one line of every executable kind through the real gateway.
async fn confirm_every_kind(bed: &Bed, s: &Settings) -> Vec<String> {
    let mut kinds = x::EXECUTABLE_KINDS.to_vec();
    kinds.sort_unstable();
    let mut outcomes = Vec::new();
    for kind in kinds {
        let (mut store, plan_id, line_id) = a_store(kind, &[]);
        if kind == "SELL_AT_OPEN" || kind == "ARM_GTT" {
            let mut p = holding(1_000);
            p.set("gtt_id", Val::None);
            store.insert_position(1, p);
            find(&mut store.lines, line_id)
                .unwrap()
                .set("position_id", Val::Int(1));
        }
        if kind == "CANCEL_LIMIT" {
            store.insert_order(
                1,
                Rec::of([
                    ("instrument_id", Val::Int(42)),
                    ("symbol", Val::from("VBTCO")),
                    ("state", Val::from("SENT")),
                    ("quantity", Val::Int(1_000)),
                    ("filled_quantity", Val::Int(0)),
                    ("broker_order_id", Val::None),
                    ("limit_price", Val::Dec(d("96.00"))),
                ]),
            );
            find(&mut store.lines, line_id)
                .unwrap()
                .set("order_id", Val::Int(1));
        }
        let outcome = x::execute_line(
            &mut store,
            bed.gateway.as_ref(),
            s,
            &plan_id,
            line_id,
            "true",
            now(),
            Some(d("96.00")),
        )
        .await
        .unwrap();
        outcomes.push(format!("{kind}:{}", outcome.status));
    }
    outcomes
}

#[tokio::test]
async fn no_line_kind_reaches_the_adapter_with_the_flag_false() {
    // `02` Track B: the execution flag decides, not DRY_RUN — so both values are driven.
    for dry_run in [true, false] {
        let mut s = settings();
        s.dry_run = dry_run;
        s.vbt_execution_enabled = false;
        let bed = Bed::with(s.clone()).await;
        let outcomes = confirm_every_kind(&bed, &s).await;
        assert_eq!(
            outcomes.len(),
            6,
            "no executable kinds were driven, so this would prove nothing"
        );
        assert!(
            bed.kite.write_calls().is_empty(),
            "DRY_RUN={dry_run}: the adapter was called: {:?} ({outcomes:?})",
            bed.kite.write_calls()
        );
    }
}

#[test]
fn the_flag_defaults_to_false_in_the_desk_and_the_simulated_marker_is_on() {
    let s = settings();
    assert!(
        !s.vbt_execution_enabled,
        "a safety flag whose default is on is not one"
    );
    assert!(x::is_simulated(&s));
}

#[tokio::test]
async fn a_sell_larger_than_the_position_is_blocked_with_both_numbers() {
    let bed = Bed::new().await;
    for (held, asked) in [(1_000, 1_001), (1_000, 5_000), (1, 2), (100, 101)] {
        let (mut store, plan_id, line_id) =
            a_store("SELL_AT_OPEN", &[("quantity", Val::Int(asked))]);
        let mut p = holding(held);
        p.set("gtt_id", Val::from("SIM-1"));
        store.insert_position(1, p);
        find(&mut store.lines, line_id)
            .unwrap()
            .set("position_id", Val::Int(1));
        let outcome = confirm_with(
            &bed,
            &mut store,
            &plan_id,
            line_id,
            "true",
            now(),
            Some(d("96.00")),
        )
        .await
        .unwrap();
        assert_eq!(outcome.status, "BLOCKED", "{held}/{asked}");
        assert!(
            outcome.reason.contains(&held.to_string())
                && outcome.reason.contains(&asked.to_string()),
            "{}",
            outcome.reason
        );
    }
}

#[tokio::test]
async fn a_sell_for_a_name_the_sleeve_does_not_hold_is_blocked_whatever_the_price() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = a_store("SELL_AT_OPEN", &[]);
    let outcome = confirm_with(
        &bed,
        &mut store,
        &plan_id,
        line_id,
        "true",
        now(),
        Some(d("96.00")),
    )
    .await
    .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.to_lowercase().contains("did not buy"));
}
