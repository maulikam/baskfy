//! `tests/test_twt_execute.py::MemoryStore` and `tests/test_twt_auto.py::AutoStore`: the
//! `TwtStore` protocol over in-memory rows, **deliberately dumb** — the rules are in the module
//! under test, not here. Every method is the Python one line for line (insertion order kept, as a
//! Python `dict` keeps it), plus the two page reads and the calendar read the auto runner uses.
//!
//! The rows live behind an `Arc<Mutex<..>>` so a test can hand one clone to the code under test
//! (`execute_line`, or the runner's `open_store`) and read the same rows back afterwards, as
//! Python's tests read `store.positions[...]` after a confirm.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex, MutexGuard};

use async_trait::async_trait;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

use crate::lanes::twt::common::{Rec, Val, to_ist};
use crate::lanes::twt::store::{
    KIND_ORDER, LineUpdate, SessionBump, SleeveMoney, StoreError, StoreResult, TwtStore,
    sleeve_money_from_rows,
};

pub fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).unwrap()
}

/// `NOW = dt.datetime(2026, 9, 11, 9, 20, tzinfo=IST)`.
pub fn now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 11, 9, 20, 0).unwrap()
}

/// `SESSION = dt.date(2026, 9, 10)`.
pub fn session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 10).unwrap()
}

/// `D(text)`.
pub fn d(text: &str) -> Decimal {
    text.parse().unwrap()
}

/// `TWENTY_FIVE_LAKH`: ₹25 lakh over ten slots is a ₹2.5 lakh line — `04` §3.5's example.
pub fn twenty_five_lakh() -> Decimal {
    d("2500000.00")
}

/// `FIFTY_CRORE`: a name where the 1 %-of-turnover cap does not bind.
pub fn fifty_crore() -> Decimal {
    d("500000000")
}

/// One session row (`{"mode", "confirms", "fills", "ratchets", "exits", "naked"}`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SessionRow {
    pub mode: String,
    pub confirms: i64,
    pub fills: i64,
    pub ratchets: i64,
    pub exits: i64,
    pub naked: i64,
}

/// The rows.
#[derive(Debug, Default)]
pub struct Mem {
    /// `issues` (LV2): the reconciler's open findings. Nothing wrong, by default.
    pub issues: Vec<Rec>,
    pub plans: Vec<Rec>,
    pub lines: Vec<Rec>,
    pub positions: Vec<Rec>,
    pub orders: Vec<Rec>,
    pub exit_orders: Vec<Rec>,
    pub fills: Vec<Rec>,
    pub sessions: Vec<(NaiveDate, SessionRow)>,
    pub audit: Vec<Rec>,
    pub locked: Vec<NaiveDate>,
    pub unlocked: usize,
    pub counted: Vec<i64>,
    pub config: Rec,
    /// `AutoStore.conn = _Calendar(closed)`.
    pub closed: Vec<NaiveDate>,
    next: i64,
}

impl Mem {
    /// `_id()`.
    pub fn id(&mut self) -> i64 {
        self.next += 1;
        self.next
    }

    fn find(rows: &mut [Rec], id: i64) -> Option<&mut Rec> {
        rows.iter_mut().find(|r| r.int("id") == id)
    }

    pub fn line(&mut self, id: i64) -> &mut Rec {
        Self::find(&mut self.lines, id).unwrap()
    }

    pub fn position(&mut self, id: i64) -> &mut Rec {
        Self::find(&mut self.positions, id).unwrap()
    }

    pub fn order(&mut self, id: i64) -> &mut Rec {
        Self::find(&mut self.orders, id).unwrap()
    }

    pub fn plan(&mut self, plan_id: &str) -> &mut Rec {
        self.plans
            .iter_mut()
            .find(|p| p.s("plan_id") == plan_id)
            .unwrap()
    }

    pub fn session(&self, day: NaiveDate) -> SessionRow {
        self.sessions
            .iter()
            .find(|(d, _)| *d == day)
            .map(|(_, r)| r.clone())
            .unwrap()
    }

    fn session_mut(&mut self, day: NaiveDate, mode: &str) -> &mut SessionRow {
        if !self.sessions.iter().any(|(d, _)| *d == day) {
            self.sessions.push((
                day,
                SessionRow {
                    mode: mode.to_owned(),
                    ..SessionRow::default()
                },
            ));
        }
        &mut self.sessions.iter_mut().find(|(d, _)| *d == day).unwrap().1
    }

    /// `open_positions()`.
    pub fn open_positions(&self) -> Vec<Rec> {
        self.positions
            .iter()
            .filter(|r| r.s("state") == "OPEN" && r.int("quantity_open") > 0)
            .cloned()
            .collect()
    }

    /// `entries_taken(day)` (LV10.2): by the IST day the order was sent; a hand-made row
    /// without `created_at` counts on its signal day.
    pub fn entries_taken(&self, day: NaiveDate) -> i64 {
        let sent_on = |row: &Rec| -> Option<NaiveDate> {
            match row.get("created_at") {
                None | Some(Val::None) => row.date("signal_date"),
                Some(Val::Stamp(t)) => Some(to_ist(t).date_naive()),
                Some(_) => panic!("created_at must be an aware datetime"),
            }
        };
        let n = self
            .orders
            .iter()
            .filter(|row| {
                row.s("side") == "BUY"
                    && sent_on(row) == Some(day)
                    && ["CONFIRMED", "SENT", "PARTIAL", "FILLED"].contains(&row.s("state").as_str())
            })
            .count();
        i64::try_from(n).unwrap()
    }
}

/// `MemoryStore(capital=..., first_live=...)` (and `AutoStore`, which adds the page reads).
#[derive(Debug, Clone)]
pub struct MemoryStore(pub Arc<Mutex<Mem>>);

impl MemoryStore {
    pub fn new(capital: Decimal, first_live: i64) -> Self {
        let config = Rec::of([
            ("sleeve_capital_inr", Val::Dec(capital)),
            ("max_open_positions", Val::Int(10)),
            ("max_position_pct", Val::Dec(d("12.50"))),
            ("stop_pct", Val::Dec(d("20.00"))),
            ("trail_pct", Val::Dec(d("20.00"))),
            ("first_live_entries_left", Val::Int(first_live)),
        ]);
        Self(Arc::new(Mutex::new(Mem {
            config,
            next: 100,
            ..Mem::default()
        })))
    }

    /// `MemoryStore()`.
    pub fn default_store() -> Self {
        Self::new(twenty_five_lakh(), 10)
    }

    /// The rows, for a test to read or arrange.
    pub fn m(&self) -> MutexGuard<'_, Mem> {
        self.0.lock().unwrap()
    }
}

fn fields_into(base: Rec, fields: &Rec) -> Rec {
    let mut row = base;
    row.update(fields);
    row
}

#[async_trait]
impl TwtStore for MemoryStore {
    async fn plan(&mut self, plan_id: &str) -> StoreResult<Option<Rec>> {
        let m = self.m();
        Ok(m.plans.iter().find(|p| p.s("plan_id") == plan_id).cloned())
    }

    async fn line(&mut self, line_id: i64) -> StoreResult<Option<Rec>> {
        let m = self.m();
        Ok(m.lines.iter().find(|l| l.int("id") == line_id).cloned())
    }

    async fn set_line(&mut self, line_id: i64, state: &str, extra: LineUpdate) -> StoreResult<()> {
        let mut m = self.m();
        let row = Mem::find(&mut m.lines, line_id)
            .ok_or_else(|| StoreError::Bad(format!("KeyError: {line_id}")))?;
        row.set("state", Val::from(state));
        if let Some(v) = extra.journal_ref {
            row.set("journal_ref", Val::Str(v));
        }
        if let Some(v) = extra.order_id {
            row.set("order_id", Val::Int(v));
        }
        if let Some(v) = extra.position_id {
            row.set("position_id", Val::Int(v));
        }
        if let Some(v) = extra.note {
            row.set("note", Val::Str(v));
        }
        Ok(())
    }

    async fn open_position_for(&mut self, instrument_id: i64) -> StoreResult<Option<Rec>> {
        let m = self.m();
        Ok(m.positions
            .iter()
            .find(|r| {
                r.int("instrument_id") == instrument_id
                    && r.s("state") == "OPEN"
                    && r.int("quantity_open") > 0
            })
            .cloned())
    }

    async fn position(&mut self, position_id: i64) -> StoreResult<Option<Rec>> {
        let m = self.m();
        Ok(m.positions
            .iter()
            .find(|r| r.int("id") == position_id)
            .cloned())
    }

    async fn open_positions(&mut self) -> StoreResult<Vec<Rec>> {
        Ok(self.m().open_positions())
    }

    async fn create_position(&mut self, fields: Rec) -> StoreResult<i64> {
        let mut m = self.m();
        let id = m.id();
        let symbol = fields.get("symbol").cloned().unwrap_or(Val::from("TWTCO"));
        let row = fields_into(
            Rec::of([
                ("id", Val::Int(id)),
                ("gtt_id", Val::None),
                ("gtt_trigger", Val::None),
                ("half_size", Val::Bool(false)),
                ("symbol", symbol),
            ]),
            &fields,
        );
        m.positions.push(row);
        Ok(id)
    }

    async fn update_position(&mut self, position_id: i64, fields: Rec) -> StoreResult<()> {
        let mut m = self.m();
        let row = Mem::find(&mut m.positions, position_id)
            .ok_or_else(|| StoreError::Bad(format!("KeyError: {position_id}")))?;
        row.update(&fields);
        Ok(())
    }

    async fn add_fill(&mut self, fields: Rec) -> StoreResult<i64> {
        let mut m = self.m();
        let id = m.id();
        let row = fields_into(Rec::of([("id", Val::Int(id))]), &fields);
        m.fills.push(row);
        Ok(id)
    }

    async fn order(&mut self, order_id: i64) -> StoreResult<Option<Rec>> {
        let m = self.m();
        Ok(m.orders.iter().find(|r| r.int("id") == order_id).cloned())
    }

    async fn order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>> {
        let m = self.m();
        Ok(m.orders
            .iter()
            .find(|r| r.s("broker_order_id") == broker_order_id)
            .cloned())
    }

    async fn create_exit_order(&mut self, fields: Rec) -> StoreResult<i64> {
        let mut m = self.m();
        let id = m.id();
        let row = fields_into(
            Rec::of([
                ("id", Val::Int(id)),
                ("filled_quantity", Val::Int(0)),
                ("avg_fill_price", Val::None),
            ]),
            &fields,
        );
        m.exit_orders.push(row);
        Ok(id)
    }

    async fn exit_order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>> {
        let m = self.m();
        Ok(m.exit_orders
            .iter()
            .find(|r| r.s("broker_order_id") == broker_order_id)
            .cloned())
    }

    async fn update_exit_order(&mut self, exit_order_id: i64, fields: Rec) -> StoreResult<()> {
        let mut m = self.m();
        let row = Mem::find(&mut m.exit_orders, exit_order_id)
            .ok_or_else(|| StoreError::Bad(format!("KeyError: {exit_order_id}")))?;
        row.update(&fields);
        Ok(())
    }

    async fn open_orders(&mut self) -> StoreResult<Vec<Rec>> {
        let m = self.m();
        Ok(m.orders
            .iter()
            .filter(|r| {
                ["SENT", "PARTIAL"].contains(&r.s("state").as_str()) && r.truthy("broker_order_id")
            })
            .map(|r| fields_into(Rec::of([("reference_price", Val::None)]), r))
            .collect())
    }

    async fn open_protection_issues(&mut self) -> StoreResult<Vec<Rec>> {
        Ok(self.m().issues.clone())
    }

    async fn line_by_client_id(&mut self, client_id: &str) -> StoreResult<Option<Rec>> {
        let m = self.m();
        Ok(m.lines
            .iter()
            .find(|r| r.s("client_id") == client_id)
            .cloned())
    }

    async fn create_order(&mut self, fields: Rec) -> StoreResult<i64> {
        let mut m = self.m();
        let id = m.id();
        let row = fields_into(
            Rec::of([
                ("id", Val::Int(id)),
                ("filled_quantity", Val::Int(0)),
                ("position_id", Val::None),
                ("symbol", Val::from("TWTCO")),
            ]),
            &fields,
        );
        m.orders.push(row);
        Ok(id)
    }

    async fn update_order(&mut self, order_id: i64, fields: Rec) -> StoreResult<()> {
        let mut m = self.m();
        let row = Mem::find(&mut m.orders, order_id)
            .ok_or_else(|| StoreError::Bad(format!("KeyError: {order_id}")))?;
        row.update(&fields);
        Ok(())
    }

    async fn entries_taken(&mut self, day: NaiveDate) -> StoreResult<i64> {
        Ok(self.m().entries_taken(day))
    }

    async fn bump_session(&mut self, day: NaiveDate, bump: SessionBump) -> StoreResult<()> {
        let mut m = self.m();
        let row = m.session_mut(day, &bump.mode);
        row.mode = bump.mode.clone();
        row.confirms += bump.confirms;
        row.fills += bump.fills;
        row.ratchets += bump.ratchets;
        row.exits += bump.exits;
        Ok(())
    }

    async fn set_naked_count(&mut self, day: NaiveDate, mode: &str, naked: i64) -> StoreResult<()> {
        self.m().session_mut(day, mode).naked = naked;
        Ok(())
    }

    async fn lock_session_for_update(&mut self, day: NaiveDate) -> StoreResult<()> {
        self.m().locked.push(day);
        Ok(())
    }

    async fn unlock_session(&mut self) -> StoreResult<()> {
        self.m().unlocked += 1;
        Ok(())
    }

    async fn sleeve_money(&mut self, _as_of: NaiveDate) -> StoreResult<SleeveMoney> {
        let m = self.m();
        sleeve_money_from_rows(&m.config, &m.open_positions(), &[], Decimal::ZERO)
    }

    async fn adj_factor(&mut self, _instrument_id: i64, _as_of: NaiveDate) -> StoreResult<Decimal> {
        Ok(Decimal::ONE)
    }

    async fn count_first_live_entry(
        &mut self,
        position_id: i64,
        session_date: NaiveDate,
        now: DateTime<FixedOffset>,
        live: bool,
    ) -> StoreResult<bool> {
        let mut m = self.m();
        let position = m.position(position_id).clone();
        if position.truthy("half_size") || !live || position.truthy("simulated") {
            return Ok(false);
        }
        let left = m.config.int("first_live_entries_left");
        if left <= 0 {
            return Ok(false);
        }
        m.config.set("first_live_entries_left", Val::Int(left - 1));
        m.position(position_id).set("half_size", Val::Bool(true));
        m.counted.push(position_id);
        m.audit.push(Rec::of([
            ("key", Val::from("first_live_entries_left")),
            ("at", Val::Stamp(now)),
            ("session_date", Val::Date(session_date)),
        ]));
        Ok(true)
    }

    async fn expire_plans(&mut self, now: DateTime<FixedOffset>) -> StoreResult<i64> {
        let mut m = self.m();
        let mut expired = 0;
        for plan in &mut m.plans {
            if plan.stamp("expires_at").is_some_and(|t| t > now) {
                plan.set("expires_at", Val::Stamp(now));
                expired += 1;
            }
        }
        Ok(expired)
    }

    async fn zero_capital(
        &mut self,
        now: DateTime<FixedOffset>,
        changed_by: &str,
    ) -> StoreResult<Option<Decimal>> {
        let mut m = self.m();
        let before = m.config.dec("sleeve_capital_inr");
        let old = m.config.s("sleeve_capital_inr");
        m.audit.push(Rec::of([
            ("key", Val::from("sleeve_capital_inr")),
            ("old_value", Val::Str(old)),
            ("new_value", Val::from("0")),
            ("at", Val::Stamp(now)),
            ("changed_by", Val::from(changed_by)),
        ]));
        m.config.set("sleeve_capital_inr", Val::Dec(Decimal::ZERO));
        Ok(before)
    }

    /// `AutoStore.todays_plan`.
    async fn todays_plan(&mut self, on_or_before: NaiveDate) -> StoreResult<Option<Rec>> {
        let m = self.m();
        // `sort(key=(session_date, built_at), reverse=True)[0]`: Python's sort is stable under
        // `reverse=True` too, so of equal keys the first inserted wins.
        let mut best: Option<Rec> = None;
        for p in &m.plans {
            if !p.date("session_date").is_some_and(|d| d <= on_or_before) {
                continue;
            }
            let key = (p.date("session_date"), p.stamp("built_at"));
            if best
                .as_ref()
                .is_none_or(|b| key > (b.date("session_date"), b.stamp("built_at")))
            {
                best = Some(p.clone());
            }
        }
        Ok(best)
    }

    /// `AutoStore.lines_for`.
    async fn lines_for(&mut self, plan_pk: i64) -> StoreResult<Vec<Rec>> {
        let m = self.m();
        let mut rows: Vec<Rec> = m
            .lines
            .iter()
            .filter(|l| l.int("plan_pk") == plan_pk)
            .cloned()
            .collect();
        rows.sort_by_key(|l| {
            let k = l.s("kind");
            (
                KIND_ORDER.iter().position(|o| *o == k).unwrap(),
                l.s("symbol"),
            )
        });
        Ok(rows)
    }

    /// `_Calendar`: `{"is_trading_day": day not in closed}`.
    async fn is_trading_day(&mut self, day: NaiveDate) -> StoreResult<Option<bool>> {
        Ok(Some(!self.m().closed.contains(&day)))
    }
}

/// `a_store(kind, capital=..., **line_overrides)`: one MORNING plan built five minutes before
/// `NOW`, expiring 25 minutes after it, and line 7.
pub fn a_store_with(
    kind: &str,
    capital: Decimal,
    overrides: &[(&str, Val)],
) -> (MemoryStore, String, i64) {
    let store = MemoryStore::new(capital, 10);
    let plan_id = "11111111-2222-4333-8444-555555555555".to_owned();
    let line_id = 7;
    {
        let mut m = store.m();
        m.plans.push(Rec::of([
            ("id", Val::Int(1)),
            ("plan_id", Val::Str(plan_id.clone())),
            ("session_date", Val::Date(session())),
            ("source", Val::from("MORNING")),
            ("built_at", Val::Stamp(now() - Duration::minutes(5))),
            ("expires_at", Val::Stamp(now() + Duration::minutes(25))),
            ("gate", Val::from("OPEN")),
        ]));
        let suffix = if kind == "BUY_AT_OPEN" {
            String::new()
        } else {
            format!(":{kind}")
        };
        let mut line = Rec::of([
            ("id", Val::Int(line_id)),
            ("plan_pk", Val::Int(1)),
            ("plan_id", Val::Str(plan_id.clone())),
            ("kind", Val::from(kind)),
            ("instrument_id", Val::Int(42)),
            ("symbol", Val::from("TWTCO")),
            ("quantity", Val::Int(2_500)),
            ("stop_price", Val::Dec(d("80.00"))),
            ("value_inr", Val::Dec(d("250000.00"))),
            ("high_since", Val::None),
            ("previous_trigger", Val::None),
            ("note", Val::from("")),
            ("state", Val::from("PROPOSED")),
            ("client_id", Val::Str(format!("{plan_id}:TWTCO{suffix}"))),
            ("order_id", Val::None),
            ("position_id", Val::None),
            ("session_date", Val::Date(session())),
            ("turnover_avg_inr", Val::Dec(fifty_crore())),
        ]);
        for (k, v) in overrides {
            line.set(*k, v.clone());
        }
        m.lines.push(line);
    }
    (store, plan_id, line_id)
}

/// `a_store()` / `a_store(kind)`.
pub fn a_store(kind: &str) -> (MemoryStore, String, i64) {
    a_store_with(kind, twenty_five_lakh(), &[])
}

/// `a_position(store, ...)`'s keyword arguments.
#[derive(Debug, Clone)]
pub struct Pos {
    pub instrument_id: i64,
    pub symbol: &'static str,
    pub quantity: i64,
    pub entry: &'static str,
    pub stop: &'static str,
    pub gtt_id: Option<&'static str>,
    pub gtt_trigger: Option<&'static str>,
    pub simulated: bool,
}

impl Default for Pos {
    fn default() -> Self {
        Self {
            instrument_id: 42,
            symbol: "TWTCO",
            quantity: 1_000,
            entry: "100.00",
            stop: "80.00",
            gtt_id: Some("551234"),
            gtt_trigger: Some("80.00"),
            simulated: true,
        }
    }
}

impl Pos {
    /// `gtt_id=None, gtt_trigger=None`.
    pub fn naked() -> Self {
        Self {
            gtt_id: None,
            gtt_trigger: None,
            ..Self::default()
        }
    }
}

/// `a_position(store, ...)`.
pub fn a_position(store: &MemoryStore, p: &Pos) -> i64 {
    let mut m = store.m();
    let id = m.id();
    m.positions.push(Rec::of([
        ("id", Val::Int(id)),
        ("instrument_id", Val::Int(p.instrument_id)),
        ("symbol", Val::from(p.symbol)),
        ("signal_date", Val::Date(session())),
        ("entry_date", Val::Date(session())),
        ("entry_avg", Val::Dec(d(p.entry))),
        ("entry_adj_factor", Val::Dec(Decimal::ONE)),
        ("quantity_entered", Val::Int(p.quantity)),
        ("quantity_open", Val::Int(p.quantity)),
        ("initial_stop", Val::Dec(d(p.stop))),
        ("stop_price", Val::Dec(d(p.stop))),
        ("high_since", Val::Dec(d(p.entry))),
        ("gtt_id", Val::opt(p.gtt_id, Val::from)),
        ("gtt_trigger", Val::opt(p.gtt_trigger, |t| Val::Dec(d(t)))),
        ("next_trigger", Val::None),
        ("next_trigger_for", Val::None),
        ("state", Val::from("OPEN")),
        ("simulated", Val::Bool(p.simulated)),
        ("half_size", Val::Bool(false)),
    ]));
    id
}
