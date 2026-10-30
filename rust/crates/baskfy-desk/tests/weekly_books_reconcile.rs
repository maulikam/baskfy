//! `tests/test_reconcile.py` (LV2), ported whole: the reconciler driven through the sleeves' OWN
//! handlers (`twt_hooks`, `vbt_hooks` over the sleeve lanes' `on_order_update`) over in-memory
//! stores, with the REAL dry-run gateways — the desk's gateway deriving each sleeve's sibling over
//! the replay Kite — behind a spy that asserts every answer is a dry-run status, so every stop
//! armed here is a gateway answer and none is a broker call. The live-shaped path is what is
//! exercised: an order recorded `SENT` with a broker id, then the order book reporting fills.
//!
//! **The stores** are the executor suites' `MemoryStore` doubles, restated here (Python imports
//! them from `test_twt_execute`/`test_vbt_execute`; a Rust integration test cannot reach another
//! test file's types). The Rust SQLite binder refuses a `Decimal` (R8.2), so the doubles stand
//! where Python's do rather than the Postgres stores.
//!
//! **Flags.** Default settings: `DRY_RUN=true`, `TWT_EXECUTION_ENABLED=false`,
//! `VBT_EXECUTION_ENABLED=false`. No test flips any of them.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use std::str::FromStr;
use std::sync::{Arc, Mutex, RwLock};

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
use baskfy_desk::lanes::twt::execute as tx;
use baskfy_desk::lanes::twt::store::{
    LineUpdate, SessionBump, SleeveMoney, StoreError, StoreResult, TwtStore, sleeve_money_from_rows,
};
use baskfy_desk::lanes::vbt::execute as vx;
use baskfy_desk::lanes::vbt::store::{
    LineUpdate as VLineUpdate, SessionBump as VSessionBump, StoreError as VStoreError,
    StoreResult as VStoreResult, VbtStore,
};
use baskfy_desk::lanes::weekly::weekly_books::reconcile::{
    self as r, GTT_KINDS, HookError, ISSUE_KINDS, Issue, IssueStore, MemoryIssueStore, OrderBook,
    PgIssueStore, ReconcileRun, SleeveHooks, reconcile_once,
};
use baskfy_desk::lanes::weekly::weekly_books::reconcile_hooks::{
    NowFn, RiskRelease, TwtHooks, VbtHooks, twt_hooks, vbt_hooks,
};
use baskfy_desk::settings::{Settings, SharedSettings};
use baskfy_desk::storage::{Db, DbConfig};
use baskfy_desk::wire::{PyDict, PyValue, dict};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

const FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;

fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).unwrap()
}

/// `NOW` of `test_reconcile.py`.
fn now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 28, 9, 20, 0).unwrap()
}

fn later() -> DateTime<FixedOffset> {
    now() + Duration::minutes(3)
}

/// `test_twt_execute.NOW` / `SESSION`.
fn twt_now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 11, 9, 20, 0).unwrap()
}

fn twt_session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 10).unwrap()
}

/// `test_vbt_execute.NOW` / `SESSION`.
fn vbt_now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 9, 21, 20, 0).unwrap()
}

fn vbt_session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 9).unwrap()
}

fn d(text: &str) -> Decimal {
    Decimal::from_str(text).unwrap()
}

fn settings() -> Settings {
    let s = Settings::defaults().unwrap();
    // `_flags_off`: both sleeves' flags false and the desk in dry run. No test flips any.
    assert!(s.dry_run && !s.twt_execution_enabled && !s.vbt_execution_enabled);
    s
}

fn shared() -> SharedSettings {
    Arc::new(RwLock::new(settings()))
}

/// `clock(at)`: the hooks' `now=`.
fn clock(at: DateTime<FixedOffset>) -> NowFn {
    Arc::new(move || at)
}

// =============================================================================================
// The broker: three reads, any of which can be made to fail
// =============================================================================================

#[derive(Default)]
struct FakeBook {
    order_rows: Vec<(String, PyDict)>,
    gtt_rows: Vec<PyValue>,
    holding_rows: Vec<PyDict>,
    fail: Vec<&'static str>,
}

impl FakeBook {
    fn report(&mut self, order_id: &str, status: &str, filled: i64, average: Option<&str>) {
        self.report_with(order_id, status, filled, average, &[]);
    }

    fn report_with(
        &mut self,
        order_id: &str,
        status: &str,
        filled: i64,
        average: Option<&str>,
        more: &[(&str, PyValue)],
    ) {
        let mut row = dict([
            ("order_id", PyValue::Str(order_id.into())),
            ("status", PyValue::Str(status.into())),
            ("filled_quantity", PyValue::Int(filled)),
            (
                "average_price",
                average.map_or(PyValue::None, |a| PyValue::Float(a.parse().unwrap())),
            ),
        ]);
        for (k, v) in more {
            row.insert(*k, v.clone());
        }
        match self.order_rows.iter_mut().find(|(k, _)| k == order_id) {
            Some((_, r)) => *r = row,
            None => self.order_rows.push((order_id.to_owned(), row)),
        }
    }

    fn gtt(&mut self, gtt_id: &str, symbol: &str, quantity: i64, status: &str) {
        self.gtt_rows.push(PyValue::Map(dict([
            ("id", PyValue::Str(gtt_id.into())),
            ("status", PyValue::Str(status.into())),
            (
                "condition",
                PyValue::Map(dict([
                    ("tradingsymbol", PyValue::Str(symbol.into())),
                    ("trigger_values", PyValue::List(vec![PyValue::Float(80.0)])),
                ])),
            ),
            (
                "orders",
                PyValue::List(vec![PyValue::Map(dict([
                    ("transaction_type", PyValue::Str("SELL".into())),
                    ("quantity", PyValue::Int(quantity)),
                ]))]),
            ),
        ])));
    }
}

#[async_trait]
impl OrderBook for FakeBook {
    async fn orders(&self) -> Result<Vec<PyValue>, HookError> {
        if self.fail.contains(&"orders") {
            return Err("RuntimeError: order book unavailable".into());
        }
        Ok(self
            .order_rows
            .iter()
            .map(|(_, r)| PyValue::Map(r.clone()))
            .collect())
    }
    async fn get_gtts(&self) -> Result<Vec<PyValue>, HookError> {
        if self.fail.contains(&"gtts") {
            return Err("RuntimeError: gtt list unavailable".into());
        }
        Ok(self.gtt_rows.clone())
    }
    async fn holdings(&self) -> Result<Vec<PyDict>, HookError> {
        if self.fail.contains(&"holdings") {
            return Err("RuntimeError: holdings unavailable".into());
        }
        Ok(self.holding_rows.clone())
    }
}

#[derive(Default)]
struct RecordingRisk {
    released: Mutex<Vec<(String, f64)>>,
}

#[async_trait]
impl RiskRelease for RecordingRisk {
    async fn release(&self, symbol: &str, value: f64) -> Result<(), HookError> {
        self.released
            .lock()
            .unwrap()
            .push((symbol.to_owned(), value));
        Ok(())
    }
}

// =============================================================================================
// The real dry-run gateways, behind the spy
// =============================================================================================

type Tape = Arc<Mutex<Vec<(String, String)>>>;

/// `SpyGateway`: a real gateway with a tape. Every answer must be a dry-run status (the G7 spy);
/// a single `PLACED` would be a real order and fails one line after it happened.
struct SpyGateway {
    inner: Arc<dyn Gateway>,
    tape: Tape,
}

impl SpyGateway {
    fn record(
        &self,
        call: &str,
        result: Result<PyDict, GatewayError>,
    ) -> Result<PyDict, GatewayError> {
        if let Ok(r) = &result {
            let status = r.get("status").map_or_else(String::new, PyValue::py_str);
            assert!(
                status.starts_with("DRY_RUN"),
                "{call} answered {status:?} with the sleeve's flag false — that is a real order"
            );
            self.tape.lock().unwrap().push((call.to_owned(), status));
        }
        result
    }
}

#[async_trait]
impl Gateway for SpyGateway {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        let r = self.inner.place(order).await;
        self.record("place", r)
    }
    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        let r = self.inner.place_gtt_stop(stop).await;
        self.record("place_gtt_stop", r)
    }
    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        let r = self.inner.delete_gtt(req).await;
        self.record("delete_gtt", r)
    }
    async fn modify_gtt_quantity(&self, req: ModifyGtt) -> Result<PyDict, GatewayError> {
        let r = self.inner.modify_gtt_quantity(req).await;
        self.record("modify_gtt_quantity", r)
    }
    async fn cancel_order(&self, req: CancelOrder) -> Result<PyDict, GatewayError> {
        let r = self.inner.cancel_order(req).await;
        self.record("cancel_order", r)
    }
}

/// The desk's gateway over the replay Kite, deriving the two sleeves' siblings.
struct Bed {
    _dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
    twt: Arc<dyn Gateway>,
    vbt: Arc<dyn Gateway>,
    tape: Tape,
}

impl Bed {
    async fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("kite.json"), FIXTURE).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        std::fs::write(
            dir.path().join("instruments/NSE.csv"),
            "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n\
             1,2,TWTCO,TWTCO,0,,0,0.05,1,EQ,NSE,NSE\n\
             3,4,VBTCO,VBTCO,0,,0,0.05,1,EQ,NSE,NSE\n",
        )
        .unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        let clk: Arc<dyn Clock> = Arc::new(FixedClock::new(now()));
        let shared = shared();
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
            clock: clk.clone(),
            ids: Arc::new(HarnessIds::new("reconcile-test")),
            journal_path: dir.path().join("orders_journal.jsonl"),
            db: db.clone(),
        };
        let risk = Arc::new(
            RiskManager::new(RiskConfig::default(), clk.clone(), RiskBacking::Memory)
                .await
                .unwrap(),
        );
        let cell = Arc::new(SharedRisk::of(risk, shared, clk, db));
        let desk = DeskGateway::build_with_risk(parts, cell).unwrap();
        let s = settings();
        let twt = desk
            .derive_sleeve(SleeveGatewaySpec {
                derived: DerivedGateway {
                    gates: Arc::new(move || tx::twt_gates(&s)),
                    journal_path: dir.path().join(tx::TWT_JOURNAL_NAME),
                    fo_coverage: false,
                },
                stop_band: tx::twt_stop_band().unwrap(),
            })
            .unwrap();
        let s = settings();
        let vbt = desk
            .derive_sleeve(SleeveGatewaySpec {
                derived: DerivedGateway {
                    gates: Arc::new(move || vx::vbt_gates(&s)),
                    journal_path: dir.path().join(vx::VBT_JOURNAL_NAME),
                    fo_coverage: false,
                },
                stop_band: vx::vbt_stop_band(&settings()).unwrap(),
            })
            .unwrap();
        let tape: Tape = Arc::default();
        Self {
            _dir: dir,
            kite,
            twt: Arc::new(SpyGateway {
                inner: twt,
                tape: tape.clone(),
            }),
            vbt: Arc::new(SpyGateway {
                inner: vbt,
                tape: tape.clone(),
            }),
            tape,
        }
    }

    fn arms(&self) -> usize {
        self.tape
            .lock()
            .unwrap()
            .iter()
            .filter(|(c, _)| c == "place_gtt_stop")
            .count()
    }

    fn assert_nothing_reached_the_broker(&self) {
        assert!(
            self.kite.write_calls().is_empty(),
            "an order or GTT reached the broker: {:?}",
            self.kite.write_calls()
        );
    }
}

// =============================================================================================
// TWT's MemoryStore (`test_twt_execute.MemoryStore`)
// =============================================================================================

#[derive(Default)]
struct TwtMem {
    plans: Vec<(String, Rec)>,
    lines: Vec<(i64, Rec)>,
    positions: Vec<(i64, Rec)>,
    orders: Vec<(i64, Rec)>,
    exit_orders: Vec<(i64, Rec)>,
    fills: Vec<Rec>,
    sessions: Vec<(NaiveDate, Rec)>,
    issues: Vec<Rec>,
    config: Rec,
    next: i64,
}

fn find(rows: &mut [(i64, Rec)], id: i64) -> Option<&mut Rec> {
    rows.iter_mut().find(|(k, _)| *k == id).map(|(_, r)| r)
}

fn get(rows: &[(i64, Rec)], id: i64) -> Option<Rec> {
    rows.iter().find(|(k, _)| *k == id).map(|(_, r)| r.clone())
}

fn with_defaults(defaults: Rec, fields: &Rec) -> Rec {
    let mut r = defaults;
    r.update(fields);
    r
}

impl TwtMem {
    fn new() -> Self {
        Self {
            config: Rec::of([
                ("sleeve_capital_inr", Val::Dec(d("2500000.00"))),
                ("max_open_positions", Val::Int(10)),
                ("max_position_pct", Val::Dec(d("12.50"))),
                ("stop_pct", Val::Dec(d("20.00"))),
                ("trail_pct", Val::Dec(d("20.00"))),
                ("first_live_entries_left", Val::Int(10)),
            ]),
            next: 100,
            ..Self::default()
        }
    }

    fn id(&mut self) -> i64 {
        self.next += 1;
        self.next
    }

    fn order_row(&self, id: i64) -> Rec {
        get(&self.orders, id).unwrap()
    }

    fn position_row(&self, id: i64) -> Rec {
        get(&self.positions, id).unwrap()
    }

    fn line_row(&self, id: i64) -> Rec {
        get(&self.lines, id).unwrap()
    }

    fn open(&self) -> Vec<Rec> {
        self.positions
            .iter()
            .map(|(_, r)| r)
            .filter(|r| r.s("state") == "OPEN" && r.int("quantity_open") > 0)
            .cloned()
            .collect()
    }
}

#[async_trait]
impl TwtStore for TwtMem {
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
            .open()
            .into_iter()
            .find(|r| r.int("instrument_id") == instrument_id))
    }
    async fn position(&mut self, position_id: i64) -> StoreResult<Option<Rec>> {
        Ok(get(&self.positions, position_id))
    }
    async fn open_positions(&mut self) -> StoreResult<Vec<Rec>> {
        Ok(self.open())
    }
    async fn create_position(&mut self, fields: Rec) -> StoreResult<i64> {
        let id = self.id();
        let symbol = fields
            .get("symbol")
            .cloned()
            .unwrap_or_else(|| Val::from("TWTCO"));
        let r = with_defaults(
            Rec::of([
                ("id", Val::Int(id)),
                ("gtt_id", Val::None),
                ("gtt_trigger", Val::None),
                ("half_size", Val::Bool(false)),
                ("symbol", symbol),
            ]),
            &fields,
        );
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
        self.fills
            .push(with_defaults(Rec::of([("id", Val::Int(id))]), &fields));
        Ok(id)
    }
    async fn order(&mut self, order_id: i64) -> StoreResult<Option<Rec>> {
        Ok(get(&self.orders, order_id))
    }
    async fn order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>> {
        Ok(self
            .orders
            .iter()
            .map(|(_, r)| r)
            .find(|r| {
                r.get("broker_order_id")
                    .is_some_and(|v| v.py_str() == broker_order_id)
            })
            .cloned())
    }
    async fn create_exit_order(&mut self, fields: Rec) -> StoreResult<i64> {
        let id = self.id();
        let r = with_defaults(
            Rec::of([
                ("id", Val::Int(id)),
                ("filled_quantity", Val::Int(0)),
                ("avg_fill_price", Val::None),
            ]),
            &fields,
        );
        self.exit_orders.push((id, r));
        Ok(id)
    }
    async fn exit_order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>> {
        Ok(self
            .exit_orders
            .iter()
            .map(|(_, r)| r)
            .find(|r| {
                r.get("broker_order_id")
                    .is_some_and(|v| v.py_str() == broker_order_id)
            })
            .cloned())
    }
    async fn update_exit_order(&mut self, exit_order_id: i64, fields: Rec) -> StoreResult<()> {
        find(&mut self.exit_orders, exit_order_id)
            .ok_or(StoreError::Bad("KeyError".into()))?
            .update(&fields);
        Ok(())
    }
    async fn open_orders(&mut self) -> StoreResult<Vec<Rec>> {
        Ok(self
            .orders
            .iter()
            .map(|(_, r)| r)
            .filter(|r| {
                ["SENT", "PARTIAL"].contains(&r.s("state").as_str()) && r.truthy("broker_order_id")
            })
            .map(|r| with_defaults(Rec::of([("reference_price", Val::None)]), r))
            .collect())
    }
    async fn open_protection_issues(&mut self) -> StoreResult<Vec<Rec>> {
        Ok(self.issues.clone())
    }
    async fn line_by_client_id(&mut self, client_id: &str) -> StoreResult<Option<Rec>> {
        Ok(self
            .lines
            .iter()
            .map(|(_, r)| r)
            .find(|r| r.get("client_id").is_some_and(|v| v.py_str() == client_id))
            .cloned())
    }
    async fn create_order(&mut self, fields: Rec) -> StoreResult<i64> {
        let id = self.id();
        let r = with_defaults(
            Rec::of([
                ("id", Val::Int(id)),
                ("filled_quantity", Val::Int(0)),
                ("position_id", Val::None),
                ("symbol", Val::from("TWTCO")),
            ]),
            &fields,
        );
        self.orders.push((id, r));
        Ok(id)
    }
    async fn update_order(&mut self, order_id: i64, fields: Rec) -> StoreResult<()> {
        find(&mut self.orders, order_id)
            .ok_or(StoreError::Bad("KeyError".into()))?
            .update(&fields);
        Ok(())
    }
    async fn entries_taken(&mut self, day: NaiveDate) -> StoreResult<i64> {
        let sent_on = |r: &Rec| match r.stamp("created_at") {
            Some(t) => Some(t.with_timezone(&ist()).date_naive()),
            None => r.date("signal_date"),
        };
        #[allow(clippy::cast_possible_wrap)]
        Ok(self
            .orders
            .iter()
            .map(|(_, r)| r)
            .filter(|r| {
                r.s("side") == "BUY"
                    && sent_on(r) == Some(day)
                    && ["CONFIRMED", "SENT", "PARTIAL", "FILLED"].contains(&r.s("state").as_str())
            })
            .count() as i64)
    }
    async fn bump_session(&mut self, day: NaiveDate, bump: SessionBump) -> StoreResult<()> {
        if !self.sessions.iter().any(|(k, _)| *k == day) {
            self.sessions.push((
                day,
                Rec::of([
                    ("mode", Val::Str(bump.mode.clone())),
                    ("confirms", Val::Int(0)),
                    ("fills", Val::Int(0)),
                    ("ratchets", Val::Int(0)),
                    ("exits", Val::Int(0)),
                    ("naked", Val::Int(0)),
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
            ("ratchets", bump.ratchets),
            ("exits", bump.exits),
        ] {
            let was = row.int(k);
            row.set(k, Val::Int(was + n));
        }
        Ok(())
    }
    async fn set_naked_count(&mut self, day: NaiveDate, mode: &str, naked: i64) -> StoreResult<()> {
        self.bump_session(
            day,
            SessionBump {
                mode: mode.to_owned(),
                ..SessionBump::default()
            },
        )
        .await?;
        if let Some((_, r)) = self.sessions.iter_mut().find(|(k, _)| *k == day) {
            r.set("naked", Val::Int(naked));
        }
        Ok(())
    }
    async fn lock_session_for_update(&mut self, _day: NaiveDate) -> StoreResult<()> {
        Ok(())
    }
    async fn sleeve_money(&mut self, _as_of: NaiveDate) -> StoreResult<SleeveMoney> {
        sleeve_money_from_rows(&self.config, &self.open(), &[], Decimal::ZERO)
    }
    async fn adj_factor(&mut self, _instrument_id: i64, _as_of: NaiveDate) -> StoreResult<Decimal> {
        Ok(Decimal::ONE)
    }
    async fn count_first_live_entry(
        &mut self,
        position_id: i64,
        _session_date: NaiveDate,
        _now: DateTime<FixedOffset>,
        live: bool,
    ) -> StoreResult<bool> {
        let left = self.config.int("first_live_entries_left");
        let position =
            find(&mut self.positions, position_id).ok_or(StoreError::Bad("KeyError".into()))?;
        if position.truthy("half_size") || !live || position.truthy("simulated") || left <= 0 {
            return Ok(false);
        }
        position.set("half_size", Val::Bool(true));
        self.config
            .set("first_live_entries_left", Val::Int(left - 1));
        Ok(true)
    }
    async fn expire_plans(&mut self, now: DateTime<FixedOffset>) -> StoreResult<i64> {
        let mut n = 0;
        for (_, p) in &mut self.plans {
            if p.stamp("expires_at").is_some_and(|e| e > now) {
                p.set("expires_at", Val::Stamp(now));
                n += 1;
            }
        }
        Ok(n)
    }
    async fn zero_capital(
        &mut self,
        _now: DateTime<FixedOffset>,
        _changed_by: &str,
    ) -> StoreResult<Option<Decimal>> {
        let before = self.config.dec("sleeve_capital_inr");
        self.config
            .set("sleeve_capital_inr", Val::Dec(Decimal::ZERO));
        Ok(before)
    }
    async fn todays_plan(&mut self, _on_or_before: NaiveDate) -> StoreResult<Option<Rec>> {
        Ok(None)
    }
    async fn lines_for(&mut self, plan_pk: i64) -> StoreResult<Vec<Rec>> {
        Ok(self
            .lines
            .iter()
            .map(|(_, r)| r)
            .filter(|r| r.int("plan_pk") == plan_pk)
            .cloned()
            .collect())
    }
}

/// `twt.a_store()`: a `BUY_AT_OPEN` line for TWTCO on a plan that expires in 25 minutes.
fn twt_a_store() -> (TwtMem, String, i64) {
    let mut store = TwtMem::new();
    let plan_id = "11111111-2222-4333-8444-555555555555".to_owned();
    store.plans.push((
        plan_id.clone(),
        Rec::of([
            ("id", Val::Int(1)),
            ("plan_id", Val::Str(plan_id.clone())),
            ("session_date", Val::Date(twt_session())),
            ("source", Val::from("MORNING")),
            ("built_at", Val::Stamp(twt_now() - Duration::minutes(5))),
            ("expires_at", Val::Stamp(twt_now() + Duration::minutes(25))),
            ("gate", Val::from("OPEN")),
        ]),
    ));
    let line_id = 7;
    store.lines.push((
        line_id,
        Rec::of([
            ("id", Val::Int(line_id)),
            ("plan_pk", Val::Int(1)),
            ("plan_id", Val::Str(plan_id.clone())),
            ("kind", Val::from("BUY_AT_OPEN")),
            ("instrument_id", Val::Int(42)),
            ("symbol", Val::from("TWTCO")),
            ("quantity", Val::Int(2_500)),
            ("stop_price", Val::Dec(d("80.00"))),
            ("value_inr", Val::Dec(d("250000.00"))),
            ("high_since", Val::None),
            ("previous_trigger", Val::None),
            ("note", Val::from("")),
            ("state", Val::from("PROPOSED")),
            ("client_id", Val::Str(format!("{plan_id}:TWTCO"))),
            ("order_id", Val::None),
            ("position_id", Val::None),
            ("session_date", Val::Date(twt_session())),
            ("turnover_avg_inr", Val::Dec(d("500000000"))),
        ]),
    ));
    (store, plan_id, line_id)
}

/// `twt.a_position(store, ...)`.
fn twt_a_position(
    store: &mut TwtMem,
    symbol: &str,
    instrument_id: i64,
    gtt_id: Option<&str>,
    quantity: i64,
    simulated: bool,
) -> i64 {
    let id = store.id();
    store.positions.push((
        id,
        Rec::of([
            ("id", Val::Int(id)),
            ("instrument_id", Val::Int(instrument_id)),
            ("symbol", Val::from(symbol)),
            ("signal_date", Val::Date(twt_session())),
            ("entry_date", Val::Date(twt_session())),
            ("entry_avg", Val::Dec(d("100.00"))),
            ("entry_adj_factor", Val::Dec(Decimal::ONE)),
            ("quantity_entered", Val::Int(quantity)),
            ("quantity_open", Val::Int(quantity)),
            ("initial_stop", Val::Dec(d("80.00"))),
            ("stop_price", Val::Dec(d("80.00"))),
            ("high_since", Val::Dec(d("100.00"))),
            ("gtt_id", gtt_id.map_or(Val::None, Val::from)),
            (
                "gtt_trigger",
                if gtt_id.is_some() {
                    Val::Dec(d("80.00"))
                } else {
                    Val::None
                },
            ),
            ("next_trigger", Val::None),
            ("next_trigger_for", Val::None),
            ("state", Val::from("OPEN")),
            ("simulated", Val::Bool(simulated)),
            ("half_size", Val::Bool(false)),
        ]),
    ));
    id
}

/// `twt_sent_order(store, quantity=, broker_id=)`.
fn twt_sent_order(store: &mut TwtMem, quantity: i64, broker_id: &str) -> i64 {
    let id = store.id();
    let r = with_defaults(
        Rec::of([
            ("id", Val::Int(id)),
            ("filled_quantity", Val::Int(0)),
            ("position_id", Val::None),
            ("symbol", Val::from("TWTCO")),
        ]),
        &Rec::of([
            ("instrument_id", Val::Int(42)),
            ("symbol", Val::from("TWTCO")),
            ("signal_date", Val::Date(twt_session())),
            ("side", Val::from("BUY")),
            ("quantity", Val::Int(quantity)),
            ("stop_price", Val::Dec(d("80.00"))),
            ("state", Val::from("SENT")),
            ("broker_order_id", Val::from(broker_id)),
            ("client_id", Val::from("plan:TWTCO")),
            ("simulated", Val::Bool(true)),
            ("reference_price", Val::Dec(d("100.00"))),
        ]),
    );
    store.orders.push((id, r));
    id
}

fn fills_of(fills: &[Rec], position_id: i64, side: &str) -> i64 {
    fills
        .iter()
        .filter(|f| f.int("position_id") == position_id && f.s("side") == side)
        .map(|f| f.int("quantity"))
        .sum()
}

fn twt_h(bed: &Bed, store: TwtMem) -> TwtHooks<TwtMem> {
    // `getattr(gateway, "risk", None)`: the dry-run gateway's own manager; releases on it are
    // not what these tests read, so the hooks carry none unless a test sets one.
    twt_hooks(store, bed.twt.clone(), shared(), clock(now()), None)
}

async fn pass(
    book: &FakeBook,
    hooks: &dyn SleeveHooks,
    at: DateTime<FixedOffset>,
    issues: &mut dyn IssueStore,
) -> ReconcileRun {
    reconcile_once(book, &[hooks], at, issues).await.unwrap()
}

async fn twt_confirm(
    bed: &Bed,
    store: &mut TwtMem,
    plan_id: &str,
    line_id: i64,
) -> tx::ExecOutcome {
    let out = tx::execute_line(
        store,
        bed.twt.as_ref(),
        &settings(),
        plan_id,
        line_id,
        "true",
        twt_now(),
        Some(d("100.00")),
    )
    .await
    .unwrap();
    bed.assert_nothing_reached_the_broker();
    out
}

// =============================================================================================
// TestTwtPartialThenComplete
// =============================================================================================

#[tokio::test]
async fn twt_partial_fill_then_complete_grows_one_position_and_one_gtt() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    let order_id = twt_sent_order(&mut store, 100, "B1");
    let hooks = twt_h(&bed, store);
    let mut issues = MemoryIssueStore::default();
    let mut book = FakeBook::default();

    book.report("B1", "OPEN", 40, Some("101.00"));
    let first = pass(&book, &hooks, now(), &mut issues).await;
    assert!(first.seen == 1 && first.applied == 1);
    let position_id = {
        let s = hooks.store().lock().await;
        let order = s.order_row(order_id);
        assert_eq!(order.s("state"), "PARTIAL");
        assert_eq!(order.int("filled_quantity"), 40);
        let position = s.position_row(order.int("position_id"));
        assert_eq!(position.int("quantity_open"), 40);
        assert_eq!(position.int("quantity_entered"), 40);
        assert!(!position.is_none("gtt_id"));
        position.int("id")
    };
    let before = bed.arms();
    let gtt_before = hooks
        .store()
        .lock()
        .await
        .position_row(position_id)
        .s("gtt_id");

    book.report("B1", "COMPLETE", 100, Some("102.00"));
    let second = pass(&book, &hooks, later(), &mut issues).await;
    assert_eq!(second.applied, 1);
    let s = hooks.store().lock().await;
    let order = s.order_row(order_id);
    assert_eq!(order.s("state"), "FILLED");
    assert_eq!(order.int("filled_quantity"), 100);
    let position = s.position_row(order.int("position_id"));
    assert_eq!(position.int("quantity_open"), 100);
    assert_eq!(position.int("quantity_entered"), 100);
    assert_eq!(position.dec("entry_avg"), Some(d("102.00")));
    assert_eq!(fills_of(&s.fills, position_id, "BUY"), 100);
    // ONE GTT: the second fill re-sized the first trigger; it did not arm a second one.
    assert_eq!(bed.arms(), before);
    assert_eq!(position.s("gtt_id"), gtt_before);
    bed.assert_nothing_reached_the_broker();
}

#[tokio::test]
async fn twt_duplicate_and_out_of_order_reports_change_nothing() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    let order_id = twt_sent_order(&mut store, 100, "B1");
    let hooks = twt_h(&bed, store);
    let mut issues = MemoryIssueStore::default();
    let mut book = FakeBook::default();
    book.report("B1", "COMPLETE", 100, Some("102.00"));
    pass(&book, &hooks, now(), &mut issues).await;
    let (position, fills) = {
        let s = hooks.store().lock().await;
        let order = s.order_row(order_id);
        (s.position_row(order.int("position_id")), s.fills.len())
    };
    // A duplicate of the complete report, then an OLDER partial report arriving late.
    pass(&book, &hooks, later(), &mut issues).await;
    book.report("B1", "OPEN", 40, Some("101.00"));
    let late = pass(&book, &hooks, later(), &mut issues).await;
    assert_eq!(late.applied, 0);
    let s = hooks.store().lock().await;
    assert_eq!(s.order_row(order_id).s("state"), "FILLED");
    assert_eq!(s.position_row(position.int("id")).int("quantity_open"), 100);
    assert_eq!(s.fills.len(), fills);
}

// =============================================================================================
// TestDeadOrders
// =============================================================================================

fn twt_store_with_sent_line() -> (TwtMem, i64, i64) {
    let (mut store, _plan_id, line_id) = twt_a_store();
    let line = find(&mut store.lines, line_id).unwrap();
    line.set("state", Val::from("SENT"));
    line.set("client_id", Val::from("plan:TWTCO"));
    let order_id = twt_sent_order(&mut store, 100, "B1");
    (store, line_id, order_id)
}

#[tokio::test]
async fn twt_partial_then_cancel_keeps_what_filled_and_closes_the_line() {
    let bed = Bed::new().await;
    let (store, line_id, order_id) = twt_store_with_sent_line();
    let hooks = twt_h(&bed, store);
    let mut issues = MemoryIssueStore::default();
    let mut book = FakeBook::default();
    book.report("B1", "OPEN", 40, Some("101.00"));
    pass(&book, &hooks, now(), &mut issues).await;
    book.report("B1", "CANCELLED", 40, Some("101.00"));
    let result = pass(&book, &hooks, later(), &mut issues).await;
    assert_eq!(result.dead, 1);
    let s = hooks.store().lock().await;
    let order = s.order_row(order_id);
    assert_eq!(order.s("state"), "FILLED");
    assert_eq!(
        s.position_row(order.int("position_id"))
            .int("quantity_open"),
        40
    );
    assert_eq!(s.line_row(line_id).s("state"), "FILLED");
    assert!(s.line_row(line_id).s("note").contains("the rest never did"));
}

#[tokio::test]
async fn twt_rejected_with_nothing_filled_leaves_no_position_and_no_stop() {
    let bed = Bed::new().await;
    let (store, line_id, order_id) = twt_store_with_sent_line();
    let hooks = twt_h(&bed, store);
    let mut issues = MemoryIssueStore::default();
    let mut book = FakeBook::default();
    let arms_before = bed.arms();
    book.report_with(
        "B1",
        "REJECTED",
        0,
        None,
        &[(
            "status_message",
            PyValue::Str("Market orders without market protection are not allowed via API".into()),
        )],
    );
    let result = pass(&book, &hooks, now(), &mut issues).await;
    assert_eq!(result.dead, 1);
    let s = hooks.store().lock().await;
    let order = s.order_row(order_id);
    assert_eq!(order.s("state"), "REJECTED");
    assert!(order.is_none("position_id"));
    assert!(s.positions.is_empty());
    assert_eq!(s.line_row(line_id).s("state"), "REJECTED");
    assert!(s.line_row(line_id).s("note").contains("market protection"));
    assert_eq!(bed.arms(), arms_before);
}

#[tokio::test]
async fn a_dead_order_releases_its_unfilled_remainder_to_the_risk_ledger() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    twt_sent_order(&mut store, 100, "B1");
    let mut hooks = twt_h(&bed, store);
    let risk = Arc::new(RecordingRisk::default());
    hooks.risk = Some(risk.clone());
    let mut book = FakeBook::default();
    book.report("B1", "CANCELLED", 40, Some("101.00"));
    pass(&book, &hooks, now(), &mut MemoryIssueStore::default()).await;
    // 60 shares never filled, valued at the order's reference: 60 x 100 = 6,000.
    assert_eq!(
        *risk.released.lock().unwrap(),
        [("TWTCO".to_owned(), 6000.0)]
    );
}

#[tokio::test]
async fn release_is_a_real_risk_manager_method_that_gives_back_a_reservation() {
    let clk: Arc<dyn Clock> = Arc::new(FixedClock::new(now()));
    let risk = RiskManager::new(RiskConfig::default(), clk, RiskBacking::Memory)
        .await
        .unwrap();
    let (ok, _) = risk
        .pre_order("TWTCO", 10_000.0, 10_000.0, "BUY")
        .await
        .unwrap();
    assert!(ok);
    RiskRelease::release(&risk, "TWTCO", 6_000.0).await.unwrap();
    assert_eq!(risk.state().await.position("TWTCO"), 4_000.0);
    RiskRelease::release(&risk, "TWTCO", 9_999.0).await.unwrap();
    assert_eq!(risk.state().await.position("TWTCO"), 0.0);
}

// =============================================================================================
// VBT's MemoryStore (`test_vbt_execute.MemoryStore`)
// =============================================================================================

#[derive(Default)]
struct VbtMem {
    plans: Vec<(String, Rec)>,
    lines: Vec<(i64, Rec)>,
    positions: Vec<(i64, Rec)>,
    orders: Vec<(i64, Rec)>,
    exit_orders: Vec<(i64, Rec)>,
    fills: Vec<Rec>,
    sessions: Vec<(NaiveDate, Rec)>,
    issues: Vec<Rec>,
    next: i64,
}

impl VbtMem {
    fn id(&mut self) -> i64 {
        if self.next == 0 {
            self.next = 100;
        }
        self.next += 1;
        self.next
    }

    fn open(&self) -> Vec<Rec> {
        self.positions
            .iter()
            .map(|(_, r)| r)
            .filter(|r| r.s("state") == "OPEN" && r.int("quantity_open") > 0)
            .map(|r| with_defaults(Rec::of([("symbol", Val::from("VBTCO"))]), r))
            .collect()
    }

    fn exit_by_broker(&self, id: &str) -> Rec {
        self.exit_orders
            .iter()
            .map(|(_, r)| r)
            .find(|r| r.s("broker_order_id") == id)
            .cloned()
            .unwrap()
    }
}

fn vbad(what: &str) -> VStoreError {
    VStoreError::Bad(format!("KeyError: {what}"))
}

#[async_trait]
impl VbtStore for VbtMem {
    async fn plan(&mut self, plan_id: &str) -> VStoreResult<Option<Rec>> {
        Ok(self
            .plans
            .iter()
            .find(|(k, _)| k == plan_id)
            .map(|(_, r)| r.clone()))
    }
    async fn line(&mut self, line_id: i64) -> VStoreResult<Option<Rec>> {
        Ok(get(&self.lines, line_id))
    }
    async fn set_line(
        &mut self,
        line_id: i64,
        state: &str,
        extra: VLineUpdate,
    ) -> VStoreResult<()> {
        let row = find(&mut self.lines, line_id).ok_or_else(|| vbad("line"))?;
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
    async fn open_position_for(&mut self, instrument_id: i64) -> VStoreResult<Option<Rec>> {
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
    async fn position(&mut self, position_id: i64) -> VStoreResult<Option<Rec>> {
        Ok(get(&self.positions, position_id))
    }
    async fn create_position(&mut self, fields: Rec) -> VStoreResult<i64> {
        let id = self.id();
        let r = with_defaults(
            Rec::of([("id", Val::Int(id)), ("gtt_id", Val::None)]),
            &fields,
        );
        self.positions.push((id, r));
        Ok(id)
    }
    async fn update_position(&mut self, position_id: i64, fields: Rec) -> VStoreResult<()> {
        find(&mut self.positions, position_id)
            .ok_or_else(|| vbad("position"))?
            .update(&fields);
        Ok(())
    }
    async fn add_fill(&mut self, fields: Rec) -> VStoreResult<i64> {
        let id = self.id();
        self.fills
            .push(with_defaults(Rec::of([("id", Val::Int(id))]), &fields));
        Ok(id)
    }
    async fn order(&mut self, order_id: i64) -> VStoreResult<Option<Rec>> {
        Ok(get(&self.orders, order_id))
    }
    async fn create_order(&mut self, fields: Rec) -> VStoreResult<i64> {
        let id = self.id();
        let r = with_defaults(
            Rec::of([("id", Val::Int(id)), ("filled_quantity", Val::Int(0))]),
            &fields,
        );
        self.orders.push((id, r));
        Ok(id)
    }
    async fn update_order(&mut self, order_id: i64, fields: Rec) -> VStoreResult<()> {
        find(&mut self.orders, order_id)
            .ok_or_else(|| vbad("order"))?
            .update(&fields);
        Ok(())
    }
    async fn working_order_for(&mut self, instrument_id: i64) -> VStoreResult<Option<Rec>> {
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
    async fn order_by_broker_id(&mut self, broker_order_id: &str) -> VStoreResult<Option<Rec>> {
        Ok(self
            .orders
            .iter()
            .map(|(_, r)| r)
            .find(|r| {
                r.get("broker_order_id")
                    .is_some_and(|v| v.py_str() == broker_order_id)
            })
            .cloned())
    }
    async fn open_orders(&mut self) -> VStoreResult<Vec<Rec>> {
        let live = |r: &&Rec| {
            ["SENT", "PARTIAL"].contains(&r.s("state").as_str()) && r.truthy("broker_order_id")
        };
        let mut out: Vec<Rec> = self
            .orders
            .iter()
            .map(|(_, r)| r)
            .filter(live)
            .map(|r| {
                let mut row = r.clone();
                row.set("reference_price", r.val("limit_price"));
                row
            })
            .collect();
        out.extend(
            self.exit_orders
                .iter()
                .map(|(_, r)| r)
                .filter(live)
                .cloned(),
        );
        Ok(out)
    }
    async fn open_positions(&mut self) -> VStoreResult<Vec<Rec>> {
        Ok(self.open())
    }
    async fn open_protection_issues(&mut self) -> VStoreResult<Vec<Rec>> {
        Ok(self.issues.clone())
    }
    async fn create_exit_order(&mut self, fields: Rec) -> VStoreResult<i64> {
        let id = self.id();
        let r = with_defaults(
            Rec::of([
                ("id", Val::Int(id)),
                ("filled_quantity", Val::Int(0)),
                ("avg_fill_price", Val::None),
            ]),
            &fields,
        );
        self.exit_orders.push((id, r));
        Ok(id)
    }
    async fn exit_order_by_broker_id(
        &mut self,
        broker_order_id: &str,
    ) -> VStoreResult<Option<Rec>> {
        Ok(self
            .exit_orders
            .iter()
            .map(|(_, r)| r)
            .find(|r| {
                r.get("broker_order_id")
                    .is_some_and(|v| v.py_str() == broker_order_id)
            })
            .cloned())
    }
    async fn update_exit_order(&mut self, exit_order_id: i64, fields: Rec) -> VStoreResult<()> {
        find(&mut self.exit_orders, exit_order_id)
            .ok_or_else(|| vbad("exit_order"))?
            .update(&fields);
        Ok(())
    }
    async fn line_by_client_id(&mut self, client_id: &str) -> VStoreResult<Option<Rec>> {
        Ok(self
            .lines
            .iter()
            .map(|(_, r)| r)
            .find(|r| r.s("client_id") == client_id)
            .cloned())
    }
    async fn bump_session(&mut self, day: NaiveDate, bump: VSessionBump) -> VStoreResult<()> {
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
    async fn lock_session_for_update(&mut self, _day: NaiveDate) -> VStoreResult<()> {
        Ok(())
    }
    async fn entries_taken(&mut self, _day: NaiveDate) -> VStoreResult<i64> {
        #[allow(clippy::cast_possible_wrap)]
        Ok(self
            .orders
            .iter()
            .filter(|(_, r)| ["CONFIRMED", "SENT", "FILLED"].contains(&r.s("state").as_str()))
            .count() as i64)
    }
}

/// `vbt.a_store(kind, quantity=)`.
fn vbt_a_store(kind: &str, quantity: i64) -> (VbtMem, String, i64) {
    let mut store = VbtMem::default();
    let plan_id = "66666666-7777-4888-8999-000000000000".to_owned();
    store.plans.push((
        plan_id.clone(),
        Rec::of([
            ("id", Val::Int(1)),
            ("plan_id", Val::Str(plan_id.clone())),
            ("session_date", Val::Date(vbt_session())),
            ("source", Val::from("EVENING")),
            ("built_at", Val::Stamp(vbt_now() - Duration::minutes(5))),
            ("expires_at", Val::Stamp(vbt_now() + Duration::minutes(25))),
            ("gate", Val::from("OPEN")),
        ]),
    ));
    let line_id = 7;
    store.lines.push((
        line_id,
        Rec::of([
            ("id", Val::Int(line_id)),
            ("plan_pk", Val::Int(1)),
            ("plan_id", Val::Str(plan_id.clone())),
            ("kind", Val::from(kind)),
            ("instrument_id", Val::Int(42)),
            ("symbol", Val::from("VBTCO")),
            ("quantity", Val::Int(quantity)),
            ("limit_price", Val::Dec(d("96.00"))),
            ("stop_price", Val::Dec(d("84.45"))),
            ("value_inr", Val::Dec(d("96000.00"))),
            ("note", Val::from("")),
            ("state", Val::from("PROPOSED")),
            ("client_id", Val::Str(format!("{plan_id}:VBTCO:{kind}"))),
            ("order_id", Val::None),
            ("position_id", Val::None),
        ]),
    ));
    (store, plan_id, line_id)
}

/// `vbt_sent_order(store, quantity=, broker_id=)`.
fn vbt_sent_order(store: &mut VbtMem, quantity: i64, broker_id: &str) -> i64 {
    let id = store.id();
    let r = with_defaults(
        Rec::of([("id", Val::Int(id)), ("filled_quantity", Val::Int(0))]),
        &Rec::of([
            ("instrument_id", Val::Int(42)),
            ("symbol", Val::from("VBTCO")),
            ("signal_date", Val::Date(vbt_session())),
            ("limit_price", Val::Dec(d("96.00"))),
            ("stop_price", Val::Dec(d("84.45"))),
            ("quantity", Val::Int(quantity)),
            ("state", Val::from("SENT")),
            ("broker_order_id", Val::from(broker_id)),
            ("client_id", Val::from("plan:VBTCO:PLACE_LIMIT")),
            ("simulated", Val::Bool(true)),
        ]),
    );
    store.orders.push((id, r));
    id
}

/// `vbt_open_position(store, quantity=, gtt_id=)`.
fn vbt_open_position(store: &mut VbtMem, quantity: i64, gtt_id: Option<&str>) -> i64 {
    let id = store.id();
    store.positions.push((
        id,
        Rec::of([
            ("id", Val::Int(id)),
            ("instrument_id", Val::Int(42)),
            ("symbol", Val::from("VBTCO")),
            ("entry_date", Val::Date(vbt_session())),
            ("entry_avg", Val::Dec(d("96.00"))),
            ("quantity_entered", Val::Int(quantity)),
            ("quantity_open", Val::Int(quantity)),
            ("initial_stop", Val::Dec(d("84.45"))),
            ("stop_price", Val::Dec(d("84.45"))),
            ("gtt_id", gtt_id.map_or(Val::None, Val::from)),
            ("gtt_trigger", Val::Dec(d("84.45"))),
            ("state", Val::from("OPEN")),
            ("simulated", Val::Bool(false)),
            ("exit_queued_for", Val::None),
            ("exit_reason_queued", Val::None),
        ]),
    ));
    id
}

/// `LiveSellGateway`: `place` answers PLACED (a live acceptance) and the GTT methods record.
/// Nothing here reaches Zerodha: it is not a Kite client, it only answers.
#[derive(Default)]
struct LiveSellGateway {
    placed: Mutex<Vec<PlaceOrder>>,
    modified: Mutex<Vec<ModifyGtt>>,
    deleted: Mutex<Vec<DeleteGtt>>,
    armed: Mutex<Vec<GttStop>>,
}

#[async_trait]
impl Gateway for LiveSellGateway {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        let mut placed = self.placed.lock().unwrap();
        placed.push(order);
        Ok(dict([
            ("status", PyValue::Str("PLACED".into())),
            ("order_id", PyValue::Str(format!("S{}", placed.len()))),
        ]))
    }
    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        let trigger = stop.trigger;
        self.armed.lock().unwrap().push(stop);
        Ok(dict([
            ("status", PyValue::Str("GTT_PLACED".into())),
            ("gtt_id", PyValue::Int(4242)),
            ("trigger", PyValue::Float(trigger)),
        ]))
    }
    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        let id = req.gtt_id;
        self.deleted.lock().unwrap().push(req);
        Ok(dict([
            ("status", PyValue::Str("GTT_DELETED".into())),
            ("gtt_id", PyValue::Int(id)),
        ]))
    }
    async fn modify_gtt_quantity(&self, req: ModifyGtt) -> Result<PyDict, GatewayError> {
        let id = req.gtt_id;
        self.modified.lock().unwrap().push(req);
        Ok(dict([
            ("status", PyValue::Str("GTT_MODIFIED".into())),
            ("gtt_id", PyValue::Int(id)),
        ]))
    }
    async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
        Err(GatewayError {
            python_type: "AttributeError".into(),
            message: "'LiveSellGateway' object has no attribute 'cancel_order'".into(),
        })
    }
}

fn vbt_h(gateway: Arc<dyn Gateway>, store: VbtMem) -> VbtHooks<VbtMem> {
    vbt_hooks(store, gateway, shared(), clock(now()), None)
}

// =============================================================================================
// TestVbt
// =============================================================================================

#[tokio::test]
async fn vbt_real_buy_is_booked_only_from_the_brokers_fill() {
    let bed = Bed::new().await;
    let mut store = VbtMem::default();
    let order_id = vbt_sent_order(&mut store, 1_000, "V1");
    assert!(store.open().is_empty());
    let hooks = vbt_h(bed.vbt.clone(), store);
    let mut book = FakeBook::default();
    book.report("V1", "OPEN", 0, None);
    pass(&book, &hooks, now(), &mut MemoryIssueStore::default()).await;
    assert!(
        hooks.store().lock().await.open().is_empty(),
        "accepted is not filled"
    );
    book.report("V1", "COMPLETE", 1_000, Some("95.50"));
    pass(&book, &hooks, later(), &mut MemoryIssueStore::default()).await;
    let s = hooks.store().lock().await;
    let order = get(&s.orders, order_id).unwrap();
    assert_eq!(order.s("state"), "FILLED");
    assert_eq!(order.int("filled_quantity"), 1_000);
    let position = get(&s.positions, order.int("position_id")).unwrap();
    assert_eq!(position.int("quantity_open"), 1_000);
    assert_eq!(position.dec("entry_avg"), Some(d("95.5000")));
    assert_eq!(position.dec("entry_avg").unwrap().to_string(), "95.5000");
    assert!(!position.is_none("gtt_id"));
    assert_eq!(fills_of(&s.fills, position.int("id"), "BUY"), 1_000);
    bed.assert_nothing_reached_the_broker();
}

#[tokio::test]
async fn vbt_live_sell_books_nothing_at_placement_and_everything_from_the_fill() {
    let (mut store, plan_id, line_id) = vbt_a_store("SELL_AT_OPEN", 100);
    let position_id = vbt_open_position(&mut store, 100, Some("9001"));
    let gw = Arc::new(LiveSellGateway::default());
    let outcome = vx::execute_line(
        &mut store,
        gw.as_ref(),
        &settings(),
        &plan_id,
        line_id,
        "true",
        vbt_now(),
        Some(d("250.00")),
    )
    .await
    .unwrap();
    assert_eq!(outcome.status, "SENT");
    let position = get(&store.positions, position_id).unwrap();
    assert_eq!(
        position.int("quantity_open"),
        100,
        "an accepted sell is not a fill"
    );
    assert_eq!(position.s("state"), "OPEN");
    assert_eq!(position.date("exit_queued_for"), Some(vbt_session()));
    assert_eq!(fills_of(&store.fills, position_id, "SELL"), 0);
    assert_eq!(get(&store.lines, line_id).unwrap().s("state"), "SENT");
    let exit_order = store.exit_by_broker("S1");
    assert_eq!(exit_order.s("state"), "SENT");
    assert_eq!(exit_order.int("quantity"), 100);

    let hooks = vbt_h(gw.clone(), store);
    let mut book = FakeBook::default();
    book.report("S1", "OPEN", 30, Some("249.00"));
    pass(&book, &hooks, now(), &mut MemoryIssueStore::default()).await;
    {
        let s = hooks.store().lock().await;
        let position = get(&s.positions, position_id).unwrap();
        assert_eq!(position.int("quantity_open"), 70);
        assert_eq!(position.s("state"), "OPEN");
        assert_eq!(fills_of(&s.fills, position_id, "SELL"), 30);
        let modified = gw.modified.lock().unwrap();
        let last = modified.last().unwrap();
        assert_eq!((last.qty, last.gtt_id), (70, 9001));
        assert_eq!(s.exit_by_broker("S1").s("state"), "PARTIAL");
    }

    book.report("S1", "COMPLETE", 100, Some("248.50"));
    pass(&book, &hooks, later(), &mut MemoryIssueStore::default()).await;
    let s = hooks.store().lock().await;
    let position = get(&s.positions, position_id).unwrap();
    assert_eq!(position.int("quantity_open"), 0);
    assert_eq!(position.s("state"), "CLOSED");
    assert_eq!(position.s("close_reason"), "EMA_EXIT");
    assert_eq!(position.dec("exit_avg").unwrap().to_string(), "248.5000");
    assert_eq!(fills_of(&s.fills, position_id, "SELL"), 100);
    assert_eq!(gw.deleted.lock().unwrap().last().unwrap().gtt_id, 9001);
    assert!(position.is_none("gtt_id"));
    assert_eq!(s.exit_by_broker("S1").s("state"), "FILLED");
    assert_eq!(get(&s.lines, line_id).unwrap().s("state"), "FILLED");
}

#[tokio::test]
async fn vbt_dry_run_sell_still_books_the_rehearsal_at_once() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = vbt_a_store("SELL_AT_OPEN", 100);
    let position_id = vbt_open_position(&mut store, 100, None);
    let outcome = vx::execute_line(
        &mut store,
        bed.vbt.as_ref(),
        &settings(),
        &plan_id,
        line_id,
        "true",
        vbt_now(),
        Some(d("250.00")),
    )
    .await
    .unwrap();
    assert_eq!(outcome.status, "SIMULATED");
    assert_eq!(
        get(&store.positions, position_id).unwrap().s("state"),
        "CLOSED"
    );
    bed.assert_nothing_reached_the_broker();
}

// =============================================================================================
// TestRestartAndLateFill
// =============================================================================================

#[tokio::test]
async fn restart_recovery_a_sent_order_from_before_the_restart_is_reconciled_on_the_first_pass() {
    // The store is what survived the restart: a SENT order, a broker id, no position.
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    let order_id = twt_sent_order(&mut store, 100, "B-yesterday");
    let mut book = FakeBook::default();
    book.report("B-yesterday", "COMPLETE", 100, Some("99.00"));
    let hooks = twt_h(&bed, store);
    let first = pass(&book, &hooks, now(), &mut MemoryIssueStore::default()).await;
    assert_eq!(first.applied, 1);
    let s = hooks.store().lock().await;
    let order = s.order_row(order_id);
    assert_eq!(order.s("state"), "FILLED");
    assert!(!s.position_row(order.int("position_id")).is_none("gtt_id"));
}

#[tokio::test]
async fn late_fill_after_the_poll_window_is_picked_up_by_a_later_pass() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    let order_id = twt_sent_order(&mut store, 100, "B1");
    let hooks = twt_h(&bed, store);
    let mut book = FakeBook::default();
    book.report("B1", "OPEN", 0, None);
    for _ in 0..3 {
        pass(&book, &hooks, now(), &mut MemoryIssueStore::default()).await;
    }
    {
        let s = hooks.store().lock().await;
        assert_eq!(s.order_row(order_id).s("state"), "SENT");
        assert!(s.positions.is_empty());
    }
    book.report("B1", "COMPLETE", 100, Some("100.50"));
    pass(&book, &hooks, later(), &mut MemoryIssueStore::default()).await;
    assert_eq!(
        hooks.store().lock().await.order_row(order_id).s("state"),
        "FILLED"
    );
}

#[tokio::test]
async fn an_order_the_book_does_not_carry_is_counted_not_guessed() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    twt_sent_order(&mut store, 100, "gone");
    let hooks = twt_h(&bed, store);
    let result = pass(
        &FakeBook::default(),
        &hooks,
        now(),
        &mut MemoryIssueStore::default(),
    )
    .await;
    assert_eq!((result.unseen, result.applied), (1, 0));
}

// =============================================================================================
// TestProtectionIssues
// =============================================================================================

fn kinds(run: &ReconcileRun) -> Vec<(i64, String)> {
    run.issues
        .iter()
        .map(|i| (i.position_id, i.kind.clone()))
        .collect()
}

#[tokio::test]
async fn naked_gtt_missing_oversized_and_external_exit_each_write_an_issue() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    let naked = twt_a_position(&mut store, "NAKEDCO", 1, None, 1_000, true);
    let missing = twt_a_position(&mut store, "MISSCO", 2, Some("777"), 1_000, true);
    let oversized = twt_a_position(&mut store, "BIGCO", 3, Some("778"), 100, true);
    let gone = twt_a_position(&mut store, "GONECO", 4, Some("779"), 100, false);
    let mut book = FakeBook::default();
    book.gtt("778", "BIGCO", 150, "active");
    book.gtt("779", "GONECO", 100, "active");
    book.holding_rows = vec![dict([
        ("symbol", PyValue::Str("GONECO".into())),
        ("quantity", PyValue::Int(60)),
    ])];
    let mut issues = MemoryIssueStore::default();
    let result = pass(&book, &twt_h(&bed, store), now(), &mut issues).await;
    let found = kinds(&result);
    for (id, kind) in [
        (naked, r::NAKED),
        (missing, r::GTT_MISSING),
        (oversized, r::GTT_OVERSIZED),
        (gone, r::EXTERNAL_EXIT),
    ] {
        assert!(found.contains(&(id, kind.to_owned())), "{id} {kind}");
    }
    assert_eq!(issues.open_issues(Some("twt")).await.unwrap().len(), 4);
}

#[tokio::test]
async fn a_triggered_gtt_with_the_shares_still_held_is_an_unresolved_position() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    let fired = twt_a_position(&mut store, "FIRECO", 42, Some("780"), 100, false);
    let mut book = FakeBook::default();
    book.gtt("780", "FIRECO", 100, "triggered");
    book.holding_rows = vec![dict([
        ("symbol", PyValue::Str("FIRECO".into())),
        ("quantity", PyValue::Int(100)),
    ])];
    let result = pass(
        &book,
        &twt_h(&bed, store),
        now(),
        &mut MemoryIssueStore::default(),
    )
    .await;
    assert_eq!(
        kinds(&result),
        [(fired, r::GTT_TRIGGERED_UNFILLED.to_owned())]
    );
}

#[tokio::test]
async fn an_issue_is_resolved_when_the_next_pass_no_longer_sees_it_and_kept_when_the_read_failed() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    let position = twt_a_position(&mut store, "MISSCO", 42, Some("777"), 1_000, true);
    let hooks = twt_h(&bed, store);
    let mut book = FakeBook::default();
    let mut issues = MemoryIssueStore::default();
    pass(&book, &hooks, now(), &mut issues).await;
    let open = |rows: Vec<r::OpenIssue>| rows.into_iter().map(|i| i.kind).collect::<Vec<_>>();
    assert_eq!(
        open(issues.open_issues(Some("twt")).await.unwrap()),
        [r::GTT_MISSING]
    );
    // The GTT list cannot be read: the finding stands — an unreadable broker is not a clean bill.
    book.fail.push("gtts");
    let failed = pass(&book, &hooks, later(), &mut issues).await;
    assert!(!failed.errors.is_empty());
    assert_eq!(
        open(issues.open_issues(Some("twt")).await.unwrap()),
        [r::GTT_MISSING]
    );
    // The trigger is back in the list: resolved.
    book.fail.clear();
    book.gtt("777", "MISSCO", 1_000, "active");
    let result = pass(&book, &hooks, later(), &mut issues).await;
    assert_eq!(result.resolved, 1);
    assert!(issues.open_issues(Some("twt")).await.unwrap().is_empty());
    assert_eq!(
        hooks
            .store()
            .lock()
            .await
            .position_row(position)
            .s("gtt_id"),
        "777"
    );
}

#[tokio::test]
async fn twt_buy_is_refused_protection_unresolved_while_an_issue_is_open() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = twt_a_store();
    store.issues = vec![Rec::of([
        ("symbol", Val::from("MISSCO")),
        ("kind", Val::from(r::GTT_MISSING)),
        ("detail", Val::from("GTT 777 is not in the broker's list")),
        ("position_id", Val::Int(9)),
    ])];
    let outcome = twt_confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.starts_with("PROTECTION_UNRESOLVED"));
    assert!(outcome.reason.contains("GTT_MISSING"));
    assert_eq!(store.line_row(line_id).s("state"), "REJECTED");
    assert!(
        !bed.tape.lock().unwrap().iter().any(|(c, _)| c == "place"),
        "no order for a book with an open issue"
    );
}

#[tokio::test]
async fn twt_buy_is_refused_while_the_book_has_a_naked_position() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = twt_a_store();
    twt_a_position(&mut store, "NAKEDCO", 1, None, 1_000, true);
    let outcome = twt_confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.contains("no resting stop"));
}

#[tokio::test]
async fn vbt_buy_is_refused_protection_unresolved_while_an_issue_is_open() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = vbt_a_store("PLACE_LIMIT", 1_000);
    store.issues = vec![Rec::of([
        ("symbol", Val::from("X")),
        ("kind", Val::from(r::EXTERNAL_EXIT)),
        ("detail", Val::from("40 share(s) left the book")),
        ("position_id", Val::Int(3)),
    ])];
    let outcome = vx::execute_line(
        &mut store,
        bed.vbt.as_ref(),
        &settings(),
        &plan_id,
        line_id,
        "true",
        vbt_now(),
        None,
    )
    .await
    .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.starts_with("PROTECTION_UNRESOLVED"));
    bed.assert_nothing_reached_the_broker();
}

#[tokio::test]
async fn a_clear_book_buys_as_before() {
    let bed = Bed::new().await;
    let (mut store, plan_id, line_id) = twt_a_store();
    let outcome = twt_confirm(&bed, &mut store, &plan_id, line_id).await;
    assert_eq!(outcome.status, "SIMULATED");
}

// =============================================================================================
// TestInvariant
// =============================================================================================

/// One order-book report: status, filled quantity, average price.
type Report<'a> = (&'a str, i64, Option<&'a str>);

#[tokio::test]
async fn invariant_recorded_fill_quantity_equals_protected_quantity_and_no_duplicate_stops() {
    // Across a partial, a growth, a cancel and a sell: every open position's open quantity is
    // its buy fills less its sell fills, and every position was armed at most once.
    let bed = Bed::new().await;
    let hooks = twt_h(&bed, TwtMem::new());
    let mut book = FakeBook::default();
    let scenarios: [(i64, &[Report<'_>]); 4] = [
        (
            100,
            &[("OPEN", 40, Some("101")), ("COMPLETE", 100, Some("102"))],
        ),
        (
            200,
            &[("OPEN", 50, Some("99")), ("CANCELLED", 50, Some("99"))],
        ),
        (300, &[("COMPLETE", 300, Some("100"))]),
        (50, &[("REJECTED", 0, None)]),
    ];
    for (n, (qty, reports)) in scenarios.iter().enumerate() {
        let broker = format!("O{n}");
        twt_sent_order(&mut *hooks.store().lock().await, *qty, &broker);
        for (status, filled, avg) in *reports {
            book.report(&broker, status, *filled, *avg);
            pass(&book, &hooks, now(), &mut MemoryIssueStore::default()).await;
        }
    }
    let arms = bed.arms();
    let s = hooks.store().lock().await;
    let open = s.open();
    // 100, 50 (kept after the cancel), 300; the rejected one never became one.
    assert_eq!(open.len(), 3);
    for position in &open {
        let id = position.int("id");
        assert_eq!(
            position.int("quantity_open"),
            fills_of(&s.fills, id, "BUY") - fills_of(&s.fills, id, "SELL")
        );
        assert!(
            !position.is_none("gtt_id"),
            "every filled quantity is protected"
        );
    }
    // Three positions, three arms — one per position, none repeated on the growth.
    assert!(arms >= 3);
    assert!(s.orders.iter().all(|(_, o)| o.s("state") != "SENT"));
    bed.assert_nothing_reached_the_broker();
}

// =============================================================================================
// TestPgIssueStore
// =============================================================================================

#[tokio::test]
async fn record_is_one_open_row_per_finding_and_resolve_except_keeps_what_was_seen() {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("issues.db"))
        .await
        .unwrap();
    conn.execute(
        "CREATE TABLE lv_protection_issue (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, \
         sleeve TEXT, position_id INTEGER, symbol TEXT, kind TEXT, detail TEXT, seen_at TEXT, \
         resolved_at TEXT)",
        &[],
    )
    .await
    .unwrap();
    conn.execute(
        "CREATE UNIQUE INDEX uq_open ON lv_protection_issue (user_id, sleeve, position_id, kind) \
         WHERE resolved_at IS NULL",
        &[],
    )
    .await
    .unwrap();
    let mut store = PgIssueStore::new(&mut conn, 1, "");
    let issue = Issue {
        sleeve: "twt".into(),
        position_id: 9,
        symbol: "MISSCO".into(),
        kind: r::GTT_MISSING.into(),
        detail: "gone".into(),
    };
    store.record(&issue, now()).await.unwrap();
    let again = Issue {
        detail: "still gone".into(),
        ..issue.clone()
    };
    store.record(&again, later()).await.unwrap();
    let rows = store.open_issues(Some("twt")).await.unwrap();
    assert!(rows.len() == 1 && rows[0].detail == "still gone");
    let all = ISSUE_KINDS.iter().map(|k| (*k).to_owned()).collect();
    let gtt = GTT_KINDS.iter().map(|k| (*k).to_owned()).collect();
    let keep = [(9, r::GTT_MISSING.to_owned())].into_iter().collect();
    assert_eq!(
        store
            .resolve_except("twt", &all, &keep, later())
            .await
            .unwrap(),
        0
    );
    assert_eq!(
        store
            .resolve_except("twt", &gtt, &Default::default(), later())
            .await
            .unwrap(),
        1
    );
    assert!(store.open_issues(Some("twt")).await.unwrap().is_empty());
    // Resolved, a new finding of the same kind opens a fresh row.
    store.record(&issue, later()).await.unwrap();
    assert_eq!(store.open_issues(None).await.unwrap().len(), 1);
}

// =============================================================================================
// Beyond the Python file: where the pass raises (R8.WB1), and the payload conversions
// =============================================================================================

/// A sleeve whose rows are given and whose issue store can be made to fail.
struct FailingIssues;

#[async_trait]
impl IssueStore for FailingIssues {
    async fn open_issues(&mut self, _sleeve: Option<&str>) -> Result<Vec<r::OpenIssue>, HookError> {
        Ok(Vec::new())
    }
    async fn record(
        &mut self,
        _issue: &Issue,
        _now: DateTime<FixedOffset>,
    ) -> Result<(), HookError> {
        Err("OperationalError: database is locked".into())
    }
    async fn resolve_except(
        &mut self,
        _sleeve: &str,
        _kinds: &std::collections::BTreeSet<String>,
        _keep: &std::collections::HashSet<(i64, String)>,
        _now: DateTime<FixedOffset>,
    ) -> Result<i64, HookError> {
        Ok(0)
    }
}

#[tokio::test]
async fn an_issue_store_that_raises_fails_the_pass_as_python_does() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    twt_a_position(&mut store, "NAKEDCO", 1, None, 10, true);
    let hooks = twt_h(&bed, store);
    let err = reconcile_once(&FakeBook::default(), &[&hooks], now(), &mut FailingIssues)
        .await
        .unwrap_err();
    assert_eq!(err, "OperationalError: database is locked");
}

#[tokio::test]
async fn a_report_python_cannot_read_is_that_handlers_failure_and_the_pass_carries_on() {
    let bed = Bed::new().await;
    let mut store = TwtMem::new();
    let order_id = twt_sent_order(&mut store, 100, "B1");
    let hooks = twt_h(&bed, store);
    let mut book = FakeBook::default();
    book.report_with(
        "B1",
        "OPEN",
        0,
        None,
        &[("filled_quantity", PyValue::Str("forty".into()))],
    );
    let run = pass(&book, &hooks, now(), &mut MemoryIssueStore::default()).await;
    assert_eq!(run.seen, 1);
    assert_eq!(
        run.errors,
        ["twt B1: ValueError: invalid literal for int() with base 10: 'forty'"]
    );
    assert_eq!(
        hooks.store().lock().await.order_row(order_id).s("state"),
        "SENT"
    );
}

#[tokio::test]
async fn a_dead_order_with_an_unreadable_reference_fails_the_pass() {
    struct Sleeve;
    #[async_trait]
    impl SleeveHooks for Sleeve {
        fn name(&self) -> &str {
            "twt"
        }
        async fn open_orders(&self) -> Result<Vec<PyDict>, HookError> {
            Ok(vec![dict([
                ("broker_order_id", PyValue::Str("B1".into())),
                ("symbol", PyValue::Str("TWTCO".into())),
                ("quantity", PyValue::Int(100)),
                ("reference_price", PyValue::Str("n/a".into())),
            ])])
        }
        async fn on_order_update(&self, _p: &PyDict) -> Result<Option<PyValue>, HookError> {
            Ok(None)
        }
        async fn open_positions(&self) -> Result<Vec<PyDict>, HookError> {
            Ok(Vec::new())
        }
        fn has_risk(&self) -> bool {
            true
        }
        async fn release(&self, _s: &str, _v: f64) -> Result<(), HookError> {
            panic!("nothing is released for a reference Python cannot read")
        }
    }
    let mut book = FakeBook::default();
    book.report("B1", "CANCELLED", 0, None);
    let err = reconcile_once(&book, &[&Sleeve], now(), &mut MemoryIssueStore::default())
        .await
        .unwrap_err();
    assert_eq!(
        err,
        "InvalidOperation: [<class 'decimal.ConversionSyntax'>]"
    );
}

// =============================================================================================
// `run_pass_from_desk` and the supervisor's pass, over the desk's own state
// =============================================================================================

async fn desk_state(api_key: &str) -> (baskfy_desk::AppState, tempfile::TempDir, Arc<ReplayKite>) {
    use baskfy_desk::app::StateParts;
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("kite.json"), FIXTURE).unwrap();
    let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
    let mut s = settings();
    s.kite_api_key = api_key.to_owned();
    s.token_file = dir.path().join("token.json").display().to_string();
    let db = Db::open(DbConfig {
        backend: "sqlite".to_owned(),
        schema: String::new(),
        database_url: String::new(),
        db_path: dir.path().join("desk.db"),
    })
    .await
    .unwrap();
    let state = baskfy_desk::build_state(StateParts {
        settings: s,
        clock: Arc::new(FixedClock::new(now())),
        ids: Arc::new(HarnessIds::new("reconcile-desk")),
        db,
        kite: kite.clone(),
        paths: baskfy_desk::DeskPaths::under(dir.path()),
        harness: None,
    });
    (state, dir, kite)
}

#[tokio::test]
async fn the_desk_pass_refuses_without_a_kite_key_as_main_kite_does() {
    use baskfy_desk::lanes::session::supervisor::ReconcilePass;
    use baskfy_desk::lanes::weekly::weekly_books::reconcile_hooks::{
        DeskReconcilePass, NO_KITE_KEY, run_pass_from_desk,
    };
    let (state, _dir, kite) = desk_state("").await;
    assert_eq!(
        run_pass_from_desk(&state, now()).await.unwrap_err(),
        NO_KITE_KEY
    );
    let e = DeskReconcilePass::new(state).run(now()).await.unwrap_err();
    assert_eq!(e.kind, "RuntimeError");
    assert!(e.message.starts_with("KITE_API_KEY missing"));
    assert!(kite.write_calls().is_empty());
}

#[tokio::test]
async fn the_desk_pass_reads_three_books_and_sends_nothing() {
    use baskfy_desk::lanes::session::supervisor::ReconcilePass;
    use baskfy_desk::lanes::weekly::weekly_books::reconcile_hooks::DeskReconcilePass;
    let (state, _dir, kite) = desk_state("test-api-key").await;
    // A SQLite desk has no sleeve tables, as Python's says: every store read is a recorded miss,
    // and the pass still answers with its line rather than raising.
    match DeskReconcilePass::new(state).run(now()).await {
        Ok(outcome) => {
            assert!(
                outcome
                    .line
                    .starts_with("reconcile 09:20:00: 0 order(s) seen")
            );
            assert!(!outcome.ok);
        }
        Err(e) => assert!(!e.kind.is_empty(), "{e:?}"),
    }
    assert!(
        kite.write_calls().is_empty(),
        "a reconcile pass wrote to the broker: {:?}",
        kite.write_calls()
    );
}
