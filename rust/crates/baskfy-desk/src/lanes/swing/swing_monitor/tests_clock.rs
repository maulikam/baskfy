//! `tests/test_swing_clock.py`, whole, plus the R8.SM2 regression: the 15:15 sweep re-arms every
//! naked position in DRY_RUN and reports what is still naked, survives a sweep that raises; the
//! 10:45 cutoff frees pending slots and notes the session, survives a cutoff that raises;
//! `run_after_close` runs the cutoff now and sleeps exactly to 15:15, sweeps at once after it,
//! and a gateway that cannot be built at 10:45 does not cost the 15:15 sweep; the sweep time is
//! the config's; the clock names no placing verb; telemetry never stops a chore; the store's
//! note appends; the cutoff precedes the sweep and the watch covers the session.
//!
//! Python's `NotedStore` + `build_swing_gateway(ExplodingKC())` is, here, the desk's own
//! `PgSwingStore` over a SQLite file built from the swing DDL and the desk's real swing gateway
//! in DRY_RUN over the replay Kite — asserted to have recorded no order or GTT call.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_core::swing::setups::SwingConfig;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

use super::clock::{
    ChoreSeams, PriceOf, SweepCounter, run_after_close, run_chore, run_cutoff, run_gtt_sweep,
    sweep_time,
};
use super::service::DeskChoreSeams;
use crate::app::{AppState, DeskPaths, StateParts, build_state};
use crate::clock::{FixedClock, ist};
use crate::fake_kite::ReplayKite;
use crate::gateway::Gateway;
use crate::ids::HarnessIds;
use crate::lanes::swing::desk::{open_store, swing_gateway};
use crate::lanes::swing::execute::OrderSource;
use crate::lanes::swing::store::SwingStore;
use crate::settings::Settings;
use crate::storage::{Db, DbConfig, DbValue, Param, Row};

/// `DAY`: 2 Sep 2026, a Wednesday.
pub(super) fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 2).unwrap()
}

pub(super) fn ist_on(h: u32, m: u32, s: u32) -> DateTime<FixedOffset> {
    ist()
        .from_local_datetime(&day().and_hms_opt(h, m, s).unwrap())
        .single()
        .unwrap()
}

fn at_1045() -> DateTime<FixedOffset> {
    ist_on(10, 45, 0)
}

fn at_1515() -> DateTime<FixedOffset> {
    ist_on(15, 15, 0)
}

fn d(s: &str) -> Decimal {
    s.parse().unwrap()
}

const SW_DDL: &str = include_str!("../desk/sw_ddl.sql");

/// The desk over a SQLite file with the swing tables, in DRY_RUN with every swing flag off, the
/// replay Kite (no quote, no order, no GTT recorded) and a placeholder key so `Kite()` builds.
pub(super) struct Desk {
    pub(super) state: AppState,
    pub(super) kite: Arc<ReplayKite>,
    pub(super) clock: Arc<FixedClock>,
    pub(super) db_path: std::path::PathBuf,
    _dir: tempfile::TempDir,
    _kite_dir: tempfile::TempDir,
}

impl Desk {
    pub(super) async fn new(at: DateTime<FixedOffset>, edit: impl FnOnce(&mut Settings)) -> Self {
        Self::over(at, serde_json::json!({}), true, edit).await
    }

    /// The same, the replay Kite answering `routes` and `session.authed`.
    pub(super) async fn over(
        at: DateTime<FixedOffset>,
        routes: serde_json::Value,
        authed: bool,
        edit: impl FnOnce(&mut Settings),
    ) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let mut settings = Settings::defaults().unwrap();
        settings.dry_run = true;
        settings.swing_execution_enabled = false;
        settings.swing_monitor_enabled = false;
        settings.swing_auto_execute = false;
        settings.sole_user_id = 1;
        settings.sole_broker_account_id = 1;
        settings.kite_api_key = "replay-test-key".to_owned();
        settings.db_backend = "sqlite".to_owned();
        settings.db_path = dir.path().join("desk.db").display().to_string();
        settings.token_file = dir.path().join("no-token.json").display().to_string();
        edit(&mut settings);
        let kite_dir = tempfile::tempdir().unwrap();
        std::fs::write(
            kite_dir.path().join("kite.json"),
            serde_json::json!({
                "session": {"authed": authed},
                "routes": routes,
                "quotes": {
                    "NSE:AAA": {"instrument_token": 1, "last_price": 100.8,
                                "upper_circuit_limit": 120.95, "ohlc": {"low": 99.0}}
                }
            })
            .to_string(),
        )
        .unwrap();
        let kite =
            Arc::new(ReplayKite::load(kite_dir.path(), None, &settings.kite_api_key).unwrap());
        let clock = Arc::new(FixedClock::new(at));
        let db_path = std::path::PathBuf::from(&settings.db_path);
        let db = Db::open(DbConfig::from_settings(&settings, None))
            .await
            .unwrap();
        let state = build_state(StateParts {
            settings,
            clock: clock.clone(),
            ids: Arc::new(HarnessIds::new("swing-monitor-tests")),
            db,
            kite: kite.clone(),
            paths: DeskPaths::under(dir.path()),
            harness: None,
        });
        Self {
            state,
            kite,
            clock,
            db_path,
            _dir: dir,
            _kite_dir: kite_dir,
        }
    }

    /// The swing tables (the DDL the desk tests share).
    pub(super) async fn with_tables(self) -> Self {
        let mut conn = self.state.db.connect().await.unwrap();
        for stmt in SW_DDL.split(';').map(str::trim).filter(|s| !s.is_empty()) {
            conn.execute(stmt, &[]).await.unwrap();
        }
        self
    }

    pub(super) async fn exec(&self, sql: &str, params: &[Param]) {
        let mut conn = self.state.db.connect().await.unwrap();
        conn.execute(sql, params).await.unwrap();
    }

    pub(super) async fn rows(&self, sql: &str) -> Vec<Row> {
        let mut conn = self.state.db.connect().await.unwrap();
        conn.execute(sql, &[]).await.unwrap().rows
    }

    /// `store.notes`: the day's `sw_session.notes`, line by line.
    pub(super) async fn notes(&self) -> Vec<String> {
        let rows = self
            .rows("SELECT notes FROM sw_session ORDER BY session_date")
            .await;
        rows.iter()
            .filter_map(|r| match r.get("notes") {
                // The row's creation marker is the store's, not a chore's note.
                Some(DbValue::Text(t)) => Some(
                    t.lines()
                        .filter(|l| *l != "swing-desk")
                        .map(str::to_owned)
                        .collect::<Vec<_>>(),
                ),
                _ => None,
            })
            .flatten()
            .collect()
    }

    /// `store.add_position(...)`: an OPEN position, naked unless `gtt_id` is given.
    pub(super) async fn add_position(
        &self,
        id: i64,
        symbol: &str,
        (entry, stop, qty): (&str, &str, i64),
        gtt_id: Option<&str>,
    ) {
        self.exec(
            "INSERT OR IGNORE INTO instrument (id, symbol, kite_token) VALUES (?, ?, ?)",
            &[
                Param::Int(id),
                Param::Text(symbol.to_owned()),
                Param::Int(id * 100),
            ],
        )
        .await;
        self.exec(
            "INSERT INTO sw_position (id, user_id, broker_account_id, instrument_id, setup, \
             entry_date, entry_avg, quantity_entered, initial_stop, stop, gtt_id, gtt_trigger, \
             trail, quantity_open, state, simulated) \
             VALUES (?, 1, 1, ?, 'FLAG', '2026-09-01', ?, ?, ?, ?, ?, ?, 'MA10', ?, 'OPEN', 1)",
            &[
                Param::Int(id),
                Param::Int(id),
                Param::Text(entry.to_owned()),
                Param::Int(qty),
                // `initial_stop` 96.00 unless the stop is lower (Python's default row).
                Param::Text(if d(stop) < d("96.00") { stop } else { "96.00" }.to_owned()),
                Param::Text(stop.to_owned()),
                gtt_id.map_or(Param::Null, |g| Param::Text(g.to_owned())),
                gtt_id.map_or(Param::Null, |_| Param::Text(stop.to_owned())),
                Param::Int(qty),
            ],
        )
        .await;
    }

    pub(super) async fn position(&self, id: i64) -> Row {
        self.rows(&format!("SELECT * FROM sw_position WHERE id = {id}"))
            .await
            .remove(0)
    }

    pub(super) async fn gateway(&self) -> Arc<dyn Gateway> {
        swing_gateway(&self.state).await.unwrap()
    }

    pub(super) async fn store(&self) -> Box<dyn SwingStore> {
        Box::new(open_store(&self.state).await.unwrap())
    }

    pub(super) fn no_writes(&self) {
        assert!(
            self.kite.write_calls().is_empty(),
            "an order or GTT reached the broker: {:?}",
            self.kite.write_calls()
        );
    }
}

fn text(r: &Row, key: &str) -> Option<String> {
    match r.get(key) {
        Some(DbValue::Text(t)) => Some(t.clone()),
        Some(DbValue::Float(f)) => Some(f.to_string()),
        Some(DbValue::Int(i)) => Some(i.to_string()),
        _ => None,
    }
}

/// `price_of` from a table; a symbol not in it raises (`no session`).
struct Prices(Vec<(&'static str, Decimal)>);

#[async_trait]
impl PriceOf for Prices {
    async fn price_of(&self, symbol: &str) -> Result<Option<Decimal>, String> {
        self.0
            .iter()
            .find(|(s, _)| *s == symbol)
            .map(|(_, p)| Some(*p))
            .ok_or_else(|| "RuntimeError: no session".to_owned())
    }
}

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

// =============================================================================================
// TestTheSweepAt1515
// =============================================================================================

#[tokio::test]
async fn test_the_1515_sweep_rearms_every_naked_position_in_dry_run() {
    let desk = Desk::new(at_1515(), |_| {}).await.with_tables().await;
    desk.add_position(1, "ALPHA", ("100.00", "96.00", 100), None)
        .await;
    desk.add_position(2, "BETA", ("210.0000", "200.00", 50), None)
        .await;
    desk.add_position(3, "GAMMA", ("100.00", "96.00", 10), Some("123456"))
        .await;
    let covered_before = desk.position(3).await;
    let mut store = desk.store().await;
    let gw = desk.gateway().await;
    let settings = desk.state.settings_snapshot();
    let prices = Prices(vec![("ALPHA", d("101")), ("BETA", d("215"))]);
    let report = run_gtt_sweep(
        store.as_mut(),
        gw.as_ref(),
        &settings,
        at_1515(),
        &prices,
        &Counter::default(),
    )
    .await
    .unwrap();
    assert_eq!(
        (report.naked_before, report.armed, report.still_naked),
        (2, 2, 0)
    );
    for o in &report.outcomes {
        assert_eq!(o.status, "SIMULATED");
        let status = o.gtt.as_ref().unwrap().get("status").cloned();
        assert_eq!(
            status,
            Some(crate::wire::PyValue::Str("DRY_RUN_GTT".to_owned())),
            "{o:?}"
        );
    }
    let a = desk.position(1).await;
    assert!(text(&a, "gtt_id").unwrap().starts_with("DRY-"));
    assert_eq!(d(&text(&a, "gtt_trigger").unwrap()), d("96.00"));
    let b = desk.position(2).await;
    assert!(text(&b, "gtt_id").unwrap().starts_with("DRY-"));
    assert_eq!(d(&text(&b, "gtt_trigger").unwrap()), d("200.00"));
    assert_eq!(desk.position(3).await, covered_before);
    assert_eq!(
        desk.notes().await,
        vec!["gtt-sweep 15:15: 2 naked, 2 armed, 0 still naked"]
    );
    desk.no_writes();
}

#[tokio::test]
async fn test_the_1515_sweep_reports_what_is_still_naked_when_no_price_can_be_read() {
    let desk = Desk::new(at_1515(), |_| {}).await.with_tables().await;
    desk.add_position(1, "ALPHA", ("100.00", "104.00", 100), None)
        .await;
    let mut store = desk.store().await;
    let gw = desk.gateway().await;
    let report = run_gtt_sweep(
        store.as_mut(),
        gw.as_ref(),
        &desk.state.settings_snapshot(),
        at_1515(),
        &Prices(vec![]),
        &Counter::default(),
    )
    .await
    .unwrap();
    assert_eq!(
        (report.naked_before, report.armed, report.still_naked),
        (1, 0, 1)
    );
    assert_eq!(report.outcomes[0].status, "BLOCKED");
    assert!(
        desk.notes()
            .await
            .last()
            .unwrap()
            .ends_with("1 naked, 0 armed, 1 still naked")
    );
    desk.no_writes();
}

/// A store whose re-arm cannot read a position: `eod_gtt_sweep` raises mid-sweep (Python's
/// monkeypatched `boom`), or whose session's SENT lines cannot be read (the cutoff's).
struct Faulty {
    inner: Box<dyn SwingStore>,
    position_fails: bool,
    sent_fails: bool,
}

fn boom() -> crate::lanes::swing::store::StoreError {
    crate::lanes::swing::store::StoreError::Value("RuntimeError: gateway gone".to_owned())
}

#[async_trait]
impl SwingStore for Faulty {
    async fn plan(
        &mut self,
        plan_id: &str,
    ) -> Result<Option<crate::lanes::swing::store::PlanRow>, crate::lanes::swing::store::StoreError>
    {
        self.inner.plan(plan_id).await
    }
    async fn line(
        &mut self,
        line_id: i64,
    ) -> Result<Option<crate::lanes::swing::store::LineRow>, crate::lanes::swing::store::StoreError>
    {
        self.inner.line(line_id).await
    }
    async fn set_line(
        &mut self,
        line_id: i64,
        state: &str,
        journal_ref: Option<&str>,
        position_id: Option<i64>,
    ) -> Result<(), crate::lanes::swing::store::StoreError> {
        self.inner
            .set_line(line_id, state, journal_ref, position_id)
            .await
    }
    async fn open_position_for(
        &mut self,
        instrument_id: i64,
    ) -> Result<
        Option<crate::lanes::swing::store::PositionRow>,
        crate::lanes::swing::store::StoreError,
    > {
        self.inner.open_position_for(instrument_id).await
    }
    async fn position(
        &mut self,
        position_id: i64,
    ) -> Result<
        Option<crate::lanes::swing::store::PositionRow>,
        crate::lanes::swing::store::StoreError,
    > {
        if self.position_fails {
            return Err(boom());
        }
        self.inner.position(position_id).await
    }
    async fn create_position(
        &mut self,
        fields: crate::lanes::swing::store::Fields,
    ) -> Result<i64, crate::lanes::swing::store::StoreError> {
        self.inner.create_position(fields).await
    }
    async fn update_position(
        &mut self,
        position_id: i64,
        fields: crate::lanes::swing::store::Fields,
    ) -> Result<(), crate::lanes::swing::store::StoreError> {
        if self.position_fails {
            return Err(boom());
        }
        self.inner.update_position(position_id, fields).await
    }
    async fn add_fill(
        &mut self,
        fields: crate::lanes::swing::store::Fields,
    ) -> Result<i64, crate::lanes::swing::store::StoreError> {
        self.inner.add_fill(fields).await
    }
    async fn bump_session(
        &mut self,
        day: NaiveDate,
        mode: &str,
        confirms: i64,
        fills: i64,
        manage_actions: i64,
    ) -> Result<(), crate::lanes::swing::store::StoreError> {
        self.inner
            .bump_session(day, mode, confirms, fills, manage_actions)
            .await
    }
    async fn note_session(
        &mut self,
        day: NaiveDate,
        note: &str,
    ) -> Result<(), crate::lanes::swing::store::StoreError> {
        self.inner.note_session(day, note).await
    }
    async fn config(
        &mut self,
    ) -> Result<crate::lanes::swing::store::SleeveConfig, crate::lanes::swing::store::StoreError>
    {
        self.inner.config().await
    }
    async fn open_positions(
        &mut self,
    ) -> Result<Vec<crate::lanes::swing::store::PositionRow>, crate::lanes::swing::store::StoreError>
    {
        self.inner.open_positions().await
    }
    async fn open_protection_issues(
        &mut self,
    ) -> Result<
        Vec<crate::lanes::swing::store::ProtectionIssue>,
        crate::lanes::swing::store::StoreError,
    > {
        self.inner.open_protection_issues().await
    }
    async fn lock_begin(
        &mut self,
        day: NaiveDate,
    ) -> Result<(), crate::lanes::swing::store::StoreError> {
        self.inner.lock_begin(day).await
    }
    async fn lock_commit(&mut self) -> Result<(), crate::lanes::swing::store::StoreError> {
        self.inner.lock_commit().await
    }
    async fn lock_rollback(&mut self) -> Result<(), crate::lanes::swing::store::StoreError> {
        self.inner.lock_rollback().await
    }
    async fn session_context(
        &mut self,
        day: NaiveDate,
    ) -> Result<crate::lanes::swing::context::SignalContext, crate::lanes::swing::store::StoreError>
    {
        self.inner.session_context(day).await
    }
    async fn resize_line(
        &mut self,
        line_id: i64,
        quantity: i64,
        risk_inr: Decimal,
        position_value: Decimal,
        note: &str,
    ) -> Result<(), crate::lanes::swing::store::StoreError> {
        self.inner
            .resize_line(line_id, quantity, risk_inr, position_value, note)
            .await
    }
    async fn range_high_for(
        &mut self,
        line_id: i64,
    ) -> Result<Option<Decimal>, crate::lanes::swing::store::StoreError> {
        self.inner.range_high_for(line_id).await
    }
    async fn line_by_order(
        &mut self,
        order_id: &str,
    ) -> Result<Option<crate::lanes::swing::store::LineRow>, crate::lanes::swing::store::StoreError>
    {
        self.inner.line_by_order(order_id).await
    }
    async fn sent_buy_lines(
        &mut self,
        day: NaiveDate,
    ) -> Result<Vec<crate::lanes::swing::store::LineRow>, crate::lanes::swing::store::StoreError>
    {
        if self.sent_fails {
            return Err(crate::lanes::swing::store::StoreError::Value(
                "RuntimeError: store gone".to_owned(),
            ));
        }
        self.inner.sent_buy_lines(day).await
    }
    async fn note_line(
        &mut self,
        line_id: i64,
        note: &str,
    ) -> Result<(), crate::lanes::swing::store::StoreError> {
        self.inner.note_line(line_id, note).await
    }
    async fn expire_pending(
        &mut self,
        day: NaiveDate,
    ) -> Result<i64, crate::lanes::swing::store::StoreError> {
        self.inner.expire_pending(day).await
    }
}

#[tokio::test]
async fn test_the_1515_sweep_survives_a_sweep_that_raises() {
    let desk = Desk::new(at_1515(), |_| {}).await.with_tables().await;
    desk.add_position(1, "ALPHA", ("100.00", "96.00", 5), None)
        .await;
    let mut store = Faulty {
        inner: desk.store().await,
        position_fails: true,
        sent_fails: false,
    };
    let gw = desk.gateway().await;
    let counter = Counter::default();
    let report = run_gtt_sweep(
        &mut store,
        gw.as_ref(),
        &desk.state.settings_snapshot(),
        at_1515(),
        &Prices(vec![("ALPHA", d("101"))]),
        &counter,
    )
    .await
    .unwrap();
    assert_eq!((report.armed, report.still_naked), (0, 1));
    assert!(
        counter
            .0
            .lock()
            .unwrap()
            .contains(&("gtt".to_owned(), "failed".to_owned()))
    );
    desk.no_writes();
}

// =============================================================================================
// TestTheCutoffAt1045
// =============================================================================================

async fn add_pending(desk: &Desk) {
    desk.exec(
        "INSERT INTO instrument (id, symbol, kite_token) VALUES (19, 'GAPCO', 1900)",
        &[],
    )
    .await;
    desk.exec(
        "INSERT INTO sw_plan (id, plan_id, user_id, as_of, source, built_at, expires_at, \
         plan_hash, gate, exposure_level) VALUES (1, 'morning', 1, '2026-09-02', 'MORNING', \
         '2026-09-02T09:09:00+05:30', '2026-09-02T09:39:00+05:30', 'h', 'GREEN', 3)",
        &[],
    )
    .await;
    desk.exec(
        "INSERT INTO sw_plan_line (plan_id, user_id, kind, instrument_id, setup, quantity, \
         trigger, note, state, client_id) VALUES (1, 1, 'PENDING_RANGE', 19, 'EP', 0, '112.50', \
         'EP score 46.20; live gap; slot reserved', 'PROPOSED', 'morning:GAPCO:PENDING_RANGE')",
        &[],
    )
    .await;
}

#[tokio::test]
async fn test_the_cutoff_frees_pending_slots_and_notes_the_session() {
    let desk = Desk::new(at_1045(), |_| {}).await.with_tables().await;
    add_pending(&desk).await;
    let mut store = desk.store().await;
    let gw = desk.gateway().await;
    let report = run_cutoff(
        store.as_mut(),
        gw.as_ref(),
        &desk.state.settings_snapshot(),
        None,
        at_1045(),
        &Counter::default(),
    )
    .await
    .unwrap();
    assert_eq!((report.slots_freed, report.cancelled), (1, 0));
    assert_eq!(
        desk.notes().await,
        vec!["cutoff 10:45: 0 reconciled, 0 cancelled, 0 refused, 1 slots freed"]
    );
    desk.no_writes();
}

#[tokio::test]
async fn test_a_cutoff_that_raises_is_logged_and_the_clock_goes_on() {
    let desk = Desk::new(at_1045(), |_| {}).await.with_tables().await;
    let mut store = Faulty {
        inner: desk.store().await,
        position_fails: false,
        sent_fails: true,
    };
    let gw = desk.gateway().await;
    let counter = Counter::default();
    let report = run_cutoff(
        &mut store,
        gw.as_ref(),
        &desk.state.settings_snapshot(),
        None,
        at_1045(),
        &counter,
    )
    .await;
    assert!(report.is_none());
    assert_eq!(
        *counter.0.lock().unwrap(),
        vec![("cutoff".to_owned(), "failed".to_owned())]
    );
}

// =============================================================================================
// TestTheSchedule
// =============================================================================================

/// The desk's seams, counted: stores opened, gateways built (the first build may fail).
struct Counted {
    inner: DeskChoreSeams,
    opened: Mutex<usize>,
    built: Mutex<usize>,
    first_build_fails: bool,
    price: Option<Decimal>,
}

impl Counted {
    fn new(desk: &Desk, first_build_fails: bool, price: Option<Decimal>) -> Self {
        Self {
            inner: DeskChoreSeams::new(desk.state.clone()),
            opened: Mutex::new(0),
            built: Mutex::new(0),
            first_build_fails,
            price,
        }
    }
}

struct Fixed(Option<Decimal>);

#[async_trait]
impl PriceOf for Fixed {
    async fn price_of(&self, _symbol: &str) -> Result<Option<Decimal>, String> {
        Ok(self.0)
    }
}

#[async_trait]
impl ChoreSeams for Counted {
    async fn open_store(&self) -> Result<Box<dyn SwingStore>, String> {
        *self.opened.lock().unwrap() += 1;
        self.inner.open_store().await
    }
    async fn gateway(&self) -> Result<Arc<dyn Gateway>, String> {
        let n = {
            let mut b = self.built.lock().unwrap();
            *b += 1;
            *b
        };
        if self.first_build_fails && n == 1 {
            return Err("RuntimeError: not authed".to_owned());
        }
        self.inner.gateway().await
    }
    async fn orders(&self) -> Option<Arc<dyn OrderSource>> {
        None
    }
    fn prices(&self) -> Arc<dyn PriceOf> {
        Arc::new(Fixed(self.price))
    }
}

#[tokio::test]
async fn test_run_after_close_runs_the_cutoff_now_and_sleeps_to_1515_for_the_sweep() {
    let desk = Desk::new(at_1045(), |_| {}).await.with_tables().await;
    desk.add_position(1, "ALPHA", ("100.00", "96.00", 100), None)
        .await;
    let seams = Counted::new(&desk, false, Some(d("101")));
    let clock = Mutex::new(at_1045() + Duration::seconds(3));
    let slept = Mutex::new(Vec::new());
    let code = run_after_close(
        day(),
        &seams,
        &desk.state.settings_snapshot(),
        || *clock.lock().unwrap(),
        |secs| {
            slept.lock().unwrap().push(secs);
            #[allow(clippy::cast_possible_truncation)]
            let next = *clock.lock().unwrap() + Duration::milliseconds((secs * 1000.0) as i64);
            *clock.lock().unwrap() = next;
            async {}
        },
        &Counter::default(),
    )
    .await;
    assert_eq!(code, 0);
    let expected = (at_1515() - at_1045()).num_seconds() - 3;
    #[allow(clippy::cast_precision_loss)]
    let expected = expected as f64;
    assert_eq!(*slept.lock().unwrap(), vec![expected]);
    assert_eq!(
        (*seams.opened.lock().unwrap(), *seams.built.lock().unwrap()),
        (2, 2)
    );
    assert_eq!(
        desk.notes().await,
        vec![
            "cutoff 10:45: 0 reconciled, 0 cancelled, 0 refused, 0 slots freed",
            "gtt-sweep 15:15: 1 naked, 1 armed, 0 still naked",
        ]
    );
    desk.no_writes();
}

#[tokio::test]
async fn test_a_clock_started_after_1515_sweeps_at_once() {
    let desk = Desk::new(at_1515(), |_| {}).await.with_tables().await;
    let seams = Counted::new(&desk, false, None);
    let late = at_1515() + Duration::minutes(20);
    let slept = Mutex::new(Vec::<f64>::new());
    run_after_close(
        day(),
        &seams,
        &desk.state.settings_snapshot(),
        || late,
        |s| {
            slept.lock().unwrap().push(s);
            async {}
        },
        &Counter::default(),
    )
    .await;
    assert!(slept.lock().unwrap().is_empty());
}

#[tokio::test]
async fn test_a_gateway_that_cannot_be_built_at_1045_does_not_cost_the_1515_sweep() {
    let desk = Desk::new(at_1045(), |_| {}).await.with_tables().await;
    desk.add_position(1, "ALPHA", ("100.00", "96.00", 100), None)
        .await;
    let seams = Counted::new(&desk, true, Some(d("101")));
    let clock = Mutex::new(at_1045());
    let counter = Counter::default();
    let code = run_after_close(
        day(),
        &seams,
        &desk.state.settings_snapshot(),
        || *clock.lock().unwrap(),
        |secs| {
            #[allow(clippy::cast_possible_truncation)]
            let next = *clock.lock().unwrap() + Duration::milliseconds((secs * 1000.0) as i64);
            *clock.lock().unwrap() = next;
            async {}
        },
        &counter,
    )
    .await;
    assert_eq!(code, 0);
    assert_eq!(*seams.built.lock().unwrap(), 2);
    assert_eq!(
        desk.notes().await,
        vec!["gtt-sweep 15:15: 1 naked, 1 armed, 0 still naked"]
    );
    assert!(
        counter
            .0
            .lock()
            .unwrap()
            .contains(&("cutoff".to_owned(), "unavailable".to_owned()))
    );
}

#[test]
fn test_the_sweep_time_is_the_config_field_not_a_literal() {
    assert_eq!(sweep_time(day()), at_1515());
    let code = super::tests::code_only(include_str!("clock.rs"));
    let re = regex::Regex::new(r"\b15\s*,\s*15\b").unwrap();
    assert!(
        !re.is_match(&code),
        "the sweep time is typed into the clock"
    );
}

#[test]
fn test_the_clock_names_no_placing_verb() {
    let code = super::tests::code_only(include_str!("clock.rs"));
    for pattern in [
        r"\bplace\b",
        r"place_order",
        r"place_gtt",
        r"\.place\(",
        r"delete_gtt",
        r"OrderGateway",
        r"\bkc\b",
        r"kiteconnect",
    ] {
        let re = regex::Regex::new(pattern).unwrap();
        assert!(!re.is_match(&code), "clock.rs names {pattern}");
    }
}

// =============================================================================================
// TestTelemetryInTheClock / TestTheStoresNote
// =============================================================================================

#[tokio::test]
async fn test_sweep_telemetry_never_raises_into_the_sweep() {
    // A counter cannot fail a chore in Rust (`count` returns nothing); the sweep still arms and
    // the cutoff still reports while every count lands in a sink.
    let desk = Desk::new(at_1515(), |_| {}).await.with_tables().await;
    desk.add_position(1, "ALPHA", ("100.00", "96.00", 100), None)
        .await;
    let mut store = desk.store().await;
    let gw = desk.gateway().await;
    let counter = Counter::default();
    let settings = desk.state.settings_snapshot();
    let report = run_gtt_sweep(
        store.as_mut(),
        gw.as_ref(),
        &settings,
        at_1515(),
        &Prices(vec![("ALPHA", d("101"))]),
        &counter,
    )
    .await
    .unwrap();
    assert_eq!(report.armed, 1);
    assert!(
        text(&desk.position(1).await, "gtt_id")
            .unwrap()
            .starts_with("DRY-")
    );
    assert!(
        run_cutoff(
            store.as_mut(),
            gw.as_ref(),
            &settings,
            None,
            at_1045(),
            &counter
        )
        .await
        .is_some()
    );
    assert!(!counter.0.lock().unwrap().is_empty());
}

#[tokio::test]
async fn test_note_session_appends_to_the_days_row_on_the_sqlite_twin() {
    let desk = Desk::new(at_1045(), |_| {}).await.with_tables().await;
    let mut store = desk.store().await;
    store
        .note_session(day(), "cutoff 10:45: 0 reconciled")
        .await
        .unwrap();
    store.bump_session(day(), "DRY_RUN", 1, 0, 0).await.unwrap();
    store
        .note_session(day(), "gtt-sweep 15:15: 1 naked, 1 armed, 0 still naked")
        .await
        .unwrap();
    let row = desk
        .rows("SELECT confirms, notes FROM sw_session")
        .await
        .remove(0);
    assert_eq!(row.get("confirms"), Some(&DbValue::Int(1)));
    assert_eq!(
        text(&row, "notes").unwrap(),
        "cutoff 10:45: 0 reconciled\ngtt-sweep 15:15: 1 naked, 1 armed, 0 still naked"
    );
}

// =============================================================================================
// TestTheChoresKeepTheirOwnHours
// =============================================================================================

#[test]
fn test_the_cutoff_still_precedes_the_sweep() {
    let window = SwingConfig::default().opening_range;
    let hm = super::tests::hm;
    assert!(
        hm(window.pending_cutoff_at) < hm(window.gtt_sweep_at),
        "the cutoff would run after the GTT sweep"
    );
    assert!(
        hm(window.gtt_sweep_at) <= hm(window.monitor_close),
        "the sweep would fall outside the watch that runs it"
    );
}

#[test]
fn test_the_watch_now_covers_the_cash_session() {
    let window = SwingConfig::default().opening_range;
    assert_eq!(window.session_open, (9, 15));
    assert_eq!(window.monitor_close, (15, 30));
    assert_eq!(window.pending_cutoff_at, (10, 45));
}

#[test]
fn test_run_chore_is_the_single_entry_point() {
    // Both callers — the watch (`DeskHooks::run_chore`) and the backstop (`run_after_close`) —
    // reach the same chore body.
    let service = super::tests::code_only(include_str!("service.rs"));
    let hook = &service[service.find("async fn run_chore(").unwrap()..];
    let hook = &hook[..hook.find("async fn on_first_tick").unwrap()];
    assert!(
        hook.contains("run_chore(") && hook.contains("DeskChoreSeams::new"),
        "{hook}"
    );
    let clock = super::tests::code_only(include_str!("clock.rs"));
    let backstop = &clock[clock.find("pub async fn run_after_close").unwrap()..];
    assert!(backstop.matches("chore_body(").count() == 2);
    let single = &clock[clock.find("pub async fn run_chore(").unwrap()..];
    assert!(single[..single.find("pub async fn run_after_close").unwrap()].contains("chore_body("));
}

/// R8.SM2: the watch's chore reaches the desk's own store and gateway — Python's `run_chore`
/// named `swing_desk` without importing it, so every in-watch chore died on a `NameError`.
#[tokio::test]
async fn test_run_chore_with_no_seams_reaches_the_desks_own() {
    let desk = Desk::new(at_1045(), |_| {}).await.with_tables().await;
    add_pending(&desk).await;
    let counter = Counter::default();
    run_chore(
        "cutoff",
        &DeskChoreSeams::new(desk.state.clone()),
        &desk.state.settings_snapshot(),
        at_1045(),
        &counter,
    )
    .await;
    assert_eq!(
        *counter.0.lock().unwrap(),
        vec![("cutoff".to_owned(), "ok".to_owned())]
    );
    assert_eq!(
        desk.notes().await,
        vec!["cutoff 10:45: 0 reconciled, 0 cancelled, 0 refused, 1 slots freed"]
    );
    // An unknown chore is Python's ValueError, counted unavailable — never a crash.
    run_chore(
        "gtt",
        &DeskChoreSeams::new(desk.state.clone()),
        &desk.state.settings_snapshot(),
        at_1515(),
        &counter,
    )
    .await;
    assert_eq!(
        counter.0.lock().unwrap().last(),
        Some(&("gtt".to_owned(), "unavailable".to_owned()))
    );
    desk.no_writes();
    let _ = ist_on(0, 0, 0);
}
