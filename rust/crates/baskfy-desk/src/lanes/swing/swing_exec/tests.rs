//! `tests/test_swing_execute.py` (and the live gate's session window), ported test-first: the
//! spec of the swing confirm path, asserted against an in-memory `SwingStore` (the Python
//! suite's `MemoryStore`, field for field) and a recording gateway that answers as the core
//! gateway's dry-run branch does (`DRY_RUN` / `DRY_RUN_GTT`, `DRY-<client_id>` handles,
//! per-instance `client_id` idempotency, untouchables raised) or, under the `live` gates, as
//! the broker would (`PLACED` `ORD<n>`, `GTT_PLACED` 4242…). No test here reaches a broker:
//! the dry-run tests assert the broker counter is zero, which is SW7's acceptance criterion.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp,
    clippy::too_many_lines,
    clippy::too_many_arguments,
    missing_docs
)]

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Mutex;

use async_trait::async_trait;
use baskfy_core::swing::market::{ExposureTier, MarketGate};
use baskfy_core::swing::plan::SwingAccount;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

use super::execute::{
    self as x, EXECUTABLE_KINDS, ExecContext, ExecError, ExecOutcome, OrderReport, OrderSource,
    OrderUpdate, PENDING_RANGE, PollTimer,
};
use super::live_gate::{in_session, quote_symbol};
use crate::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder, gtt_status,
};
use crate::lanes::swing::context::SignalContext;
use crate::lanes::swing::store::{
    Fields, LineRow, PlanRow, PositionRow, ProtectionIssue, SleeveConfig, StoreError, SwingStore,
};
use crate::lanes::swing::values::Val;
use crate::settings::Settings;
use crate::wire::{PyDict, PyValue};

fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).unwrap()
}

/// `NOW = dt.datetime(2026, 9, 2, 9, 31, tzinfo=IST)` — a Wednesday.
fn now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 2, 9, 31, 0).unwrap()
}

fn d(s: &str) -> Decimal {
    s.parse().unwrap()
}

/// The ladder's rungs, `04` §8.4 — (max open positions, max exposure %), lowest first.
const TIERS: [(i64, f64); 4] = [(2, 25.0), (4, 50.0), (6, 75.0), (10, 100.0)];

// --- the store -----------------------------------------------------------------------------

#[derive(Clone, Default)]
struct Book {
    plans: BTreeMap<String, PlanRow>,
    lines: BTreeMap<i64, LineRow>,
    positions: BTreeMap<i64, PositionRow>,
    fills: Vec<Fields>,
    sessions: BTreeMap<NaiveDate, (String, i64, i64, i64)>,
    config: Option<SleeveConfig>,
}

struct MemoryStore {
    book: Book,
    snapshot: Option<Book>,
    line_history: Vec<(i64, String)>,
    market_gate: MarketGate,
    rung: usize,
    new_entries_allowed: bool,
    drawdown_locked: bool,
    detected: BTreeMap<String, (Decimal, Option<Decimal>, Decimal)>,
    locks: Vec<NaiveDate>,
    resizes: Vec<(i64, i64)>,
    range_highs: BTreeMap<i64, Decimal>,
    issues: Vec<ProtectionIssue>,
}

impl MemoryStore {
    fn new() -> Self {
        Self::with(0, 3, MarketGate::Green, d("1000000"))
    }

    fn with(first_live: i64, rung: usize, gate: MarketGate, capital: Decimal) -> Self {
        let mut detected = BTreeMap::new();
        detected.insert(
            "ALPHA".to_owned(),
            (d("5.00"), Some(d("100000000")), d("72.00")),
        );
        Self {
            book: Book {
                config: Some(SleeveConfig {
                    sleeve_capital_inr: capital,
                    risk_per_trade_pct: d("0.500"),
                    max_position_pct: d("20.00"),
                    max_open_positions: 10,
                    first_live_sessions_left: first_live,
                    exposure_level: i64::try_from(rung).unwrap(),
                    present: true,
                }),
                ..Book::default()
            },
            snapshot: None,
            line_history: Vec::new(),
            market_gate: gate,
            rung,
            new_entries_allowed: gate != MarketGate::Red,
            drawdown_locked: false,
            detected,
            locks: Vec::new(),
            resizes: Vec::new(),
            range_highs: BTreeMap::new(),
            issues: Vec::new(),
        }
    }

    fn add_plan(&mut self) -> String {
        self.add_plan_built(now() - Duration::minutes(5))
    }

    fn add_plan_built(&mut self, built_at: DateTime<FixedOffset>) -> String {
        let n = self.book.plans.len() + 1;
        let plan_id = format!("00000000-0000-4000-8000-{n:012}");
        self.book.plans.insert(
            plan_id.clone(),
            PlanRow {
                id: i64::try_from(n).unwrap(),
                plan_id: plan_id.clone(),
                as_of: Some(built_at.date_naive()),
                source: "MORNING".to_owned(),
                built_at: Some(built_at),
                expires_at: Some(built_at + Duration::minutes(30)),
                gate: "GREEN".to_owned(),
                exposure_level: 1,
                total_risk_inr: None,
                total_new_exposure_inr: None,
            },
        );
        plan_id
    }

    fn base_line(&self, plan_id: &str) -> LineRow {
        let id = i64::try_from(self.book.lines.len() + 1).unwrap();
        let plan = &self.book.plans[plan_id];
        LineRow {
            id,
            plan_pk: plan.id,
            plan_id: plan_id.to_owned(),
            kind: "BUY_ON_TRIGGER".to_owned(),
            instrument_id: 11,
            symbol: "ALPHA".to_owned(),
            setup: Some("FLAG".to_owned()),
            quantity: 100,
            trigger: Some(d("100.00")),
            stop: Some(d("96.00")),
            risk_inr: Some(d("400.00")),
            position_value: Some(d("10000.00")),
            trail: Some("MA20".to_owned()),
            note: Some(String::new()),
            state: "PROPOSED".to_owned(),
            client_id: format!("{plan_id}:ALPHA:BUY_ON_TRIGGER"),
            journal_ref: None,
            position_id: None,
            updated_at: None,
            as_of: plan.as_of,
            plan_source: plan.source.clone(),
        }
    }

    fn add_line_with(&mut self, plan_id: &str, edit: impl FnOnce(&mut LineRow)) -> i64 {
        let mut line = self.base_line(plan_id);
        edit(&mut line);
        let id = line.id;
        self.detected.entry(line.symbol.clone()).or_insert((
            d("5.00"),
            Some(d("100000000")),
            d("72.00"),
        ));
        self.book.lines.insert(id, line);
        id
    }

    fn add_line(&mut self, plan_id: &str) -> i64 {
        self.add_line_with(plan_id, |_| {})
    }

    fn base_position(id: i64) -> PositionRow {
        PositionRow {
            id,
            instrument_id: 11,
            symbol: "ALPHA".to_owned(),
            setup: "FLAG".to_owned(),
            entry_date: Some(now().date_naive()),
            entry_avg: Some(d("100.00")),
            quantity_entered: 300,
            quantity_open: 300,
            initial_stop: Some(d("96.00")),
            stop: Some(d("96.00")),
            gtt_id: Some("DRY-x".to_owned()),
            gtt_trigger: Some(d("96.00")),
            gtt_armed_at: Some(now()),
            trail: "MA20".to_owned(),
            partial_done: false,
            partial_date: None,
            state: "OPEN".to_owned(),
            closed_on: None,
            exit_avg: None,
            close_reason: None,
            r_multiple: None,
            pnl_inr: None,
            simulated: true,
            half_risk: false,
        }
    }

    fn add_position_with(&mut self, edit: impl FnOnce(&mut PositionRow)) -> i64 {
        let id = i64::try_from(self.book.positions.len() + 1).unwrap();
        let mut row = Self::base_position(id);
        edit(&mut row);
        row.id = id;
        self.book.positions.insert(id, row);
        id
    }

    fn line_state(&self, id: i64) -> &str {
        &self.book.lines[&id].state
    }

    fn session(&self) -> (String, i64, i64, i64) {
        self.book.sessions[&now().date_naive()].clone()
    }
}

fn apply(row: &mut PositionRow, fields: &Fields) {
    for (k, v) in fields {
        let dec = || match v {
            Val::Dec(x) => Some(*x),
            Val::Int(i) => Some(Decimal::from(*i)),
            _ => None,
        };
        let int = || match v {
            Val::Int(i) => *i,
            _ => 0,
        };
        let text = || match v {
            Val::Text(s) => Some(s.clone()),
            _ => None,
        };
        let date = || match v {
            Val::Date(x) => Some(*x),
            _ => None,
        };
        match *k {
            "instrument_id" => row.instrument_id = int(),
            "symbol" => row.symbol = text().unwrap_or_default(),
            "setup" => row.setup = text().unwrap_or_default(),
            "entry_date" => row.entry_date = date(),
            "entry_avg" => row.entry_avg = dec(),
            "quantity_entered" => row.quantity_entered = int(),
            "quantity_open" => row.quantity_open = int(),
            "initial_stop" => row.initial_stop = dec(),
            "stop" => row.stop = dec(),
            "gtt_id" => row.gtt_id = text(),
            "gtt_trigger" => row.gtt_trigger = dec(),
            "gtt_armed_at" => {
                row.gtt_armed_at = match v {
                    Val::Stamp(t) => Some(*t),
                    _ => None,
                }
            }
            "trail" => row.trail = text().unwrap_or_default(),
            "partial_done" => row.partial_done = matches!(v, Val::Bool(true)),
            "partial_date" => row.partial_date = date(),
            "state" => row.state = text().unwrap_or_default(),
            "closed_on" => row.closed_on = date(),
            "exit_avg" => row.exit_avg = dec(),
            "close_reason" => row.close_reason = text(),
            "r_multiple" => row.r_multiple = dec(),
            "pnl_inr" => row.pnl_inr = dec(),
            "simulated" => row.simulated = matches!(v, Val::Bool(true)),
            "half_risk" => row.half_risk = matches!(v, Val::Bool(true)),
            other => panic!("unknown sw_position column {other}"),
        }
    }
}

#[async_trait]
impl SwingStore for MemoryStore {
    async fn plan(&mut self, plan_id: &str) -> Result<Option<PlanRow>, StoreError> {
        Ok(self.book.plans.get(plan_id).cloned())
    }

    async fn line(&mut self, line_id: i64) -> Result<Option<LineRow>, StoreError> {
        Ok(self.book.lines.get(&line_id).cloned())
    }

    async fn set_line(
        &mut self,
        line_id: i64,
        state: &str,
        journal_ref: Option<&str>,
        position_id: Option<i64>,
    ) -> Result<(), StoreError> {
        let row = self
            .book
            .lines
            .get_mut(&line_id)
            .ok_or_else(|| StoreError::Value(format!("no line {line_id}")))?;
        state.clone_into(&mut row.state);
        if let Some(r) = journal_ref {
            row.journal_ref = Some(r.to_owned());
        }
        if position_id.is_some() {
            row.position_id = position_id;
        }
        self.line_history.push((line_id, state.to_owned()));
        Ok(())
    }

    async fn open_position_for(
        &mut self,
        instrument_id: i64,
    ) -> Result<Option<PositionRow>, StoreError> {
        Ok(self
            .book
            .positions
            .values()
            .find(|r| {
                r.instrument_id == instrument_id && r.state != "CLOSED" && r.quantity_open > 0
            })
            .cloned())
    }

    async fn position(&mut self, position_id: i64) -> Result<Option<PositionRow>, StoreError> {
        Ok(self.book.positions.get(&position_id).cloned())
    }

    async fn create_position(&mut self, fields: Fields) -> Result<i64, StoreError> {
        let id = i64::try_from(self.book.positions.len() + 1).unwrap();
        let mut row = Self::base_position(id);
        apply(&mut row, &fields);
        self.book.positions.insert(id, row);
        Ok(id)
    }

    async fn update_position(
        &mut self,
        position_id: i64,
        fields: Fields,
    ) -> Result<(), StoreError> {
        if let Some(row) = self.book.positions.get_mut(&position_id) {
            apply(row, &fields);
        }
        Ok(())
    }

    async fn add_fill(&mut self, fields: Fields) -> Result<i64, StoreError> {
        self.book.fills.push(fields);
        Ok(i64::try_from(self.book.fills.len()).unwrap())
    }

    async fn bump_session(
        &mut self,
        day: NaiveDate,
        mode: &str,
        confirms: i64,
        fills: i64,
        manage_actions: i64,
    ) -> Result<(), StoreError> {
        let row = self
            .book
            .sessions
            .entry(day)
            .or_insert_with(|| (mode.to_owned(), 0, 0, 0));
        mode.clone_into(&mut row.0);
        row.1 += confirms;
        row.2 += fills;
        row.3 += manage_actions;
        Ok(())
    }

    async fn note_session(&mut self, _day: NaiveDate, _note: &str) -> Result<(), StoreError> {
        Ok(())
    }

    async fn config(&mut self) -> Result<SleeveConfig, StoreError> {
        self.book
            .config
            .clone()
            .ok_or_else(|| StoreError::Value("no config".to_owned()))
    }

    async fn open_positions(&mut self) -> Result<Vec<PositionRow>, StoreError> {
        Ok(self
            .book
            .positions
            .values()
            .filter(|p| p.state != "CLOSED" && p.quantity_open > 0)
            .cloned()
            .collect())
    }

    async fn open_protection_issues(&mut self) -> Result<Vec<ProtectionIssue>, StoreError> {
        Ok(self.issues.clone())
    }

    async fn lock_begin(&mut self, day: NaiveDate) -> Result<(), StoreError> {
        self.locks.push(day);
        self.snapshot = Some(self.book.clone());
        Ok(())
    }

    async fn lock_commit(&mut self) -> Result<(), StoreError> {
        self.snapshot = None;
        Ok(())
    }

    async fn lock_rollback(&mut self) -> Result<(), StoreError> {
        if let Some(book) = self.snapshot.take() {
            self.book = book;
        }
        Ok(())
    }

    async fn session_context(&mut self, day: NaiveDate) -> Result<SignalContext, StoreError> {
        let held: Vec<&PositionRow> = self
            .book
            .positions
            .values()
            .filter(|p| p.state != "CLOSED" && p.quantity_open > 0)
            .collect();
        let mut exposure: Decimal = held
            .iter()
            .map(|p| p.entry_avg.unwrap_or_default() * Decimal::from(p.quantity_open))
            .sum();
        let mut symbols: BTreeSet<String> = held.iter().map(|p| p.symbol.clone()).collect();
        let taken: Vec<&LineRow> = self
            .book
            .lines
            .values()
            .filter(|l| {
                l.kind == "BUY_ON_TRIGGER"
                    && matches!(l.state.as_str(), "CONFIRMED" | "SENT" | "FILLED")
                    && self.book.plans[&l.plan_id].as_of == Some(day)
            })
            .collect();
        for l in &taken {
            if l.position_id.is_none() && !symbols.contains(&l.symbol) {
                exposure += l.trigger.unwrap_or_default() * Decimal::from(l.quantity);
                symbols.insert(l.symbol.clone());
            }
        }
        let config = self.book.config.clone().unwrap();
        let capital = config.sleeve_capital_inr;
        let (count, pct) = TIERS[self.rung];
        Ok(SignalContext {
            gate: self.market_gate,
            tier: ExposureTier {
                level: i64::try_from(self.rung).unwrap(),
                max_open_positions: count,
                max_exposure_pct: pct,
                new_entries_allowed: self.new_entries_allowed,
                drawdown_locked: self.drawdown_locked,
            },
            account: SwingAccount {
                equity: capital,
                cash_available: (capital - exposure).max(Decimal::ZERO),
                open_symbols: symbols,
                open_exposure_inr: exposure,
                open_entry_counts: BTreeMap::new(),
            },
            detected: self.detected.clone(),
            entries_today: i64::try_from(taken.len()).unwrap(),
            pending: Vec::new(),
            reserved: Vec::new(),
            first_live_sessions_left: config.first_live_sessions_left,
            live: None,
        })
    }

    async fn resize_line(
        &mut self,
        line_id: i64,
        quantity: i64,
        risk_inr: Decimal,
        position_value: Decimal,
        note: &str,
    ) -> Result<(), StoreError> {
        if let Some(row) = self.book.lines.get_mut(&line_id) {
            row.quantity = quantity;
            row.risk_inr = Some(risk_inr);
            row.position_value = Some(position_value);
            row.note = Some(note.to_owned());
        }
        self.resizes.push((line_id, quantity));
        Ok(())
    }

    async fn range_high_for(&mut self, line_id: i64) -> Result<Option<Decimal>, StoreError> {
        Ok(self.range_highs.get(&line_id).copied())
    }

    async fn line_by_order(&mut self, order_id: &str) -> Result<Option<LineRow>, StoreError> {
        Ok(self
            .book
            .lines
            .values()
            .find(|l| {
                l.kind == "BUY_ON_TRIGGER"
                    && l.state == "SENT"
                    && l.journal_ref.as_deref() == Some(order_id)
            })
            .cloned())
    }

    async fn sent_buy_lines(&mut self, day: NaiveDate) -> Result<Vec<LineRow>, StoreError> {
        Ok(self
            .book
            .lines
            .values()
            .filter(|l| {
                l.kind == "BUY_ON_TRIGGER"
                    && l.state == "SENT"
                    && self.book.plans[&l.plan_id].as_of == Some(day)
            })
            .cloned()
            .collect())
    }

    async fn note_line(&mut self, line_id: i64, note: &str) -> Result<(), StoreError> {
        if let Some(row) = self.book.lines.get_mut(&line_id) {
            let joined = format!("{}; {note}", row.note.clone().unwrap_or_default());
            row.note = Some(joined.trim_matches([';', ' ']).to_owned());
        }
        Ok(())
    }

    async fn expire_pending(&mut self, day: NaiveDate) -> Result<i64, StoreError> {
        let mut freed = 0;
        let ids: Vec<i64> = self
            .book
            .lines
            .values()
            .filter(|l| {
                l.kind == "PENDING_RANGE"
                    && l.state == "PROPOSED"
                    && self.book.plans[&l.plan_id].as_of == Some(day)
            })
            .map(|l| l.id)
            .collect();
        for id in ids {
            if let Some(l) = self.book.lines.get_mut(&id) {
                "EXPIRED".clone_into(&mut l.state);
            }
            self.line_history.push((id, "EXPIRED".to_owned()));
            freed += 1;
        }
        Ok(freed)
    }
}

// --- the gateway ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
#[allow(dead_code)] // every call is recorded; not every test reads every kind
enum Call {
    Place(PlaceOrder),
    Gtt(GttStop),
    Delete(DeleteGtt),
    Modify(ModifyGtt),
    Cancel(CancelOrder),
}

/// The core gateway's observable contract for these tests: untouchables raise, the product
/// gates are read per call, `client_id` is idempotent per instance, and the dry-run branch
/// answers without a broker. Under `live` gates it answers as the broker would and counts
/// every such answer in `broker` — which every dry-run test asserts is zero.
struct FakeGateway {
    settings: Settings,
    calls: Mutex<Vec<Call>>,
    sent: Mutex<BTreeMap<String, String>>,
    gtt_sent: Mutex<BTreeMap<String, String>>,
    broker: Mutex<i64>,
    refuse_gtt: bool,
    refuse_delete: bool,
    explode_place: bool,
}

impl FakeGateway {
    fn new(settings: &Settings) -> Self {
        Self {
            settings: settings.clone(),
            calls: Mutex::new(Vec::new()),
            sent: Mutex::new(BTreeMap::new()),
            gtt_sent: Mutex::new(BTreeMap::new()),
            broker: Mutex::new(0),
            refuse_gtt: false,
            refuse_delete: false,
            explode_place: false,
        }
    }

    fn dry(&self) -> bool {
        x::swing_gates(&self.settings).dry_run
    }

    fn calls(&self) -> Vec<Call> {
        self.calls.lock().unwrap().clone()
    }

    fn places(&self) -> Vec<PlaceOrder> {
        self.calls()
            .into_iter()
            .filter_map(|c| match c {
                Call::Place(p) => Some(p),
                _ => None,
            })
            .collect()
    }

    fn gtts(&self) -> Vec<GttStop> {
        self.calls()
            .into_iter()
            .filter_map(|c| match c {
                Call::Gtt(g) => Some(g),
                _ => None,
            })
            .collect()
    }

    fn modifies(&self) -> Vec<ModifyGtt> {
        self.calls()
            .into_iter()
            .filter_map(|c| match c {
                Call::Modify(m) => Some(m),
                _ => None,
            })
            .collect()
    }

    fn broker(&self) -> i64 {
        *self.broker.lock().unwrap()
    }

    fn untouchable(symbol: &str) -> Result<(), GatewayError> {
        if symbol.starts_with("SGB") {
            return Err(GatewayError {
                python_type: "UntouchableInstrumentError".to_owned(),
                message: format!("{symbol} is untouchable"),
            });
        }
        Ok(())
    }
}

fn dict(pairs: Vec<(&str, PyValue)>) -> PyDict {
    let mut m = PyDict::new();
    for (k, v) in pairs {
        m.insert(k, v);
    }
    m
}

fn s(v: &str) -> PyValue {
    PyValue::Str(v.to_owned())
}

#[async_trait]
impl Gateway for FakeGateway {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        self.calls.lock().unwrap().push(Call::Place(order.clone()));
        Self::untouchable(&order.symbol)?;
        if order.product != "CNC" {
            return Ok(dict(vec![
                ("symbol", s(&order.symbol)),
                ("status", s("BLOCKED")),
                ("error", s("product gate")),
            ]));
        }
        if self.explode_place {
            return Err(GatewayError {
                python_type: "RuntimeError".to_owned(),
                message: "boom".to_owned(),
            });
        }
        let cid = order.client_id.clone().unwrap_or_default();
        let mut sent = self.sent.lock().unwrap();
        if let Some(prior) = sent.get(&cid) {
            return Ok(dict(vec![
                ("symbol", s(&order.symbol)),
                ("status", s("DUPLICATE")),
                ("order_id", s(prior)),
            ]));
        }
        if self.dry() {
            sent.insert(cid.clone(), format!("DRY-{cid}"));
            return Ok(dict(vec![
                ("symbol", s(&order.symbol)),
                ("status", s("DRY_RUN")),
                ("order_id", s(&format!("DRY-{cid}"))),
            ]));
        }
        *self.broker.lock().unwrap() += 1;
        let n = self.places().len();
        sent.insert(cid, format!("ORD{n}"));
        Ok(dict(vec![
            ("symbol", s(&order.symbol)),
            ("status", s("PLACED")),
            ("order_id", s(&format!("ORD{n}"))),
        ]))
    }

    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        self.calls.lock().unwrap().push(Call::Gtt(stop.clone()));
        Self::untouchable(&stop.symbol)?;
        if self.refuse_gtt {
            return Ok(dict(vec![
                ("symbol", s(&stop.symbol)),
                ("status", s("BLOCKED")),
                ("error", s("stop outside the band")),
            ]));
        }
        let cid = stop.client_id.clone().unwrap_or_default();
        let mut sent = self.gtt_sent.lock().unwrap();
        if let Some(prior) = sent.get(&cid) {
            return Ok(dict(vec![
                ("symbol", s(&stop.symbol)),
                ("status", s("DUPLICATE")),
                ("gtt_id", s(prior)),
            ]));
        }
        if self.dry() {
            sent.insert(cid.clone(), format!("DRY-{cid}"));
            return Ok(dict(vec![
                ("symbol", s(&stop.symbol)),
                ("status", s(gtt_status::DRY_RUN_GTT)),
                ("trigger", PyValue::Float(stop.trigger)),
                ("qty", PyValue::Int(stop.qty)),
            ]));
        }
        *self.broker.lock().unwrap() += 1;
        let id = 4242 + i64::try_from(self.gtts().len()).unwrap() - 1;
        sent.insert(cid, id.to_string());
        Ok(dict(vec![
            ("symbol", s(&stop.symbol)),
            ("status", s(gtt_status::GTT_PLACED)),
            ("gtt_id", PyValue::Int(id)),
            ("trigger", PyValue::Float(stop.trigger)),
        ]))
    }

    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        self.calls.lock().unwrap().push(Call::Delete(req.clone()));
        Self::untouchable(&req.symbol)?;
        let status = if self.refuse_delete {
            "BLOCKED"
        } else if self.dry() {
            gtt_status::DRY_RUN_GTT_DELETE
        } else {
            *self.broker.lock().unwrap() += 1;
            gtt_status::GTT_DELETED
        };
        Ok(dict(vec![
            ("symbol", s(&req.symbol)),
            ("gtt_id", PyValue::Int(req.gtt_id)),
            ("status", s(status)),
        ]))
    }

    async fn modify_gtt_quantity(&self, req: ModifyGtt) -> Result<PyDict, GatewayError> {
        self.calls.lock().unwrap().push(Call::Modify(req.clone()));
        Self::untouchable(&req.symbol)?;
        let status = if self.dry() {
            gtt_status::DRY_RUN_GTT_MODIFY
        } else {
            *self.broker.lock().unwrap() += 1;
            gtt_status::GTT_MODIFIED
        };
        Ok(dict(vec![
            ("symbol", s(&req.symbol)),
            ("gtt_id", PyValue::Int(req.gtt_id)),
            ("status", s(status)),
            ("trigger", PyValue::Float(req.trigger)),
            ("qty", PyValue::Int(req.qty)),
        ]))
    }

    async fn cancel_order(&self, req: CancelOrder) -> Result<PyDict, GatewayError> {
        self.calls.lock().unwrap().push(Call::Cancel(req.clone()));
        let status = if self.dry() {
            gtt_status::ORDER_CANCEL_DRY_RUN
        } else {
            *self.broker.lock().unwrap() += 1;
            gtt_status::ORDER_CANCELLED
        };
        Ok(dict(vec![
            ("symbol", s(&req.symbol)),
            ("order_id", s(&req.order_id)),
            ("status", s(status)),
        ]))
    }
}

// --- the clock and the order source --------------------------------------------------------

#[derive(Default)]
struct FakeClock {
    now: Mutex<f64>,
    slept: Mutex<Vec<f64>>,
}

#[async_trait]
impl PollTimer for FakeClock {
    fn now(&self) -> f64 {
        *self.now.lock().unwrap()
    }

    async fn sleep(&self, seconds: f64) {
        self.slept.lock().unwrap().push(seconds);
        *self.now.lock().unwrap() += seconds;
    }
}

struct ScriptedOrders {
    script: Vec<OrderReport>,
    polls: Mutex<Vec<String>>,
}

impl ScriptedOrders {
    fn new(reports: &[(&str, i64, &str)]) -> Self {
        Self {
            script: reports
                .iter()
                .map(|(st, f, p)| OrderReport {
                    status: (*st).to_owned(),
                    filled_quantity: *f,
                    average_price: d(p),
                })
                .collect(),
            polls: Mutex::new(Vec::new()),
        }
    }

    fn polls(&self) -> usize {
        self.polls.lock().unwrap().len()
    }
}

#[async_trait]
impl OrderSource for ScriptedOrders {
    async fn order_status(&self, order_id: &str) -> OrderReport {
        let mut polls = self.polls.lock().unwrap();
        polls.push(order_id.to_owned());
        let index = (polls.len() - 1).min(self.script.len() - 1);
        self.script[index].clone()
    }
}

// --- the drivers ---------------------------------------------------------------------------

/// The defaults this run ships with: `DRY_RUN` true, the swing execution flag false.
fn paper() -> Settings {
    let s = Settings::defaults().unwrap();
    assert!(s.dry_run && !s.swing_execution_enabled);
    s
}

/// The gates a real session would run under. Only the fake gateway ever sees them.
fn live() -> Settings {
    let mut s = Settings::defaults().unwrap();
    s.dry_run = false;
    s.swing_execution_enabled = true;
    assert!(!x::swing_gates(&s).dry_run);
    s
}

async fn execute_full(
    store: &mut MemoryStore,
    gw: &FakeGateway,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    at: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
    orders: Option<&dyn OrderSource>,
    clock: &FakeClock,
) -> Result<ExecOutcome, ExecError> {
    let settings = gw.settings.clone();
    let cx = ExecContext {
        settings: &settings,
        orders,
        timer: clock,
    };
    x::execute_line(store, gw, &cx, plan_id, line_id, confirm, at, last_price).await
}

async fn execute(
    store: &mut MemoryStore,
    gw: &FakeGateway,
    plan_id: &str,
    line_id: i64,
) -> Result<ExecOutcome, ExecError> {
    let clock = FakeClock::default();
    execute_full(
        store,
        gw,
        plan_id,
        line_id,
        "true",
        now(),
        None,
        None,
        &clock,
    )
    .await
}

/// A live confirm at the line's own trigger (below the entry cap), as the Python helper does.
async fn execute_live(
    store: &mut MemoryStore,
    gw: &FakeGateway,
    plan_id: &str,
    line_id: i64,
    orders: Option<&dyn OrderSource>,
    clock: &FakeClock,
) -> ExecOutcome {
    let price = store.book.lines[&line_id].trigger;
    execute_full(
        store,
        gw,
        plan_id,
        line_id,
        "true",
        now(),
        price,
        orders,
        clock,
    )
    .await
    .unwrap()
}

fn http_status(result: Result<ExecOutcome, ExecError>) -> u16 {
    match result {
        Err(ExecError::Http(r)) => r.status,
        other => panic!("expected a refusal, got {other:?}"),
    }
}

// --- G1: the surface and the gates ---------------------------------------------------------

#[test]
fn swing_gates_dry_run_unless_both_flags_allow() {
    let mut st = Settings::defaults().unwrap();
    for (dry, enabled, expect) in [
        (true, false, true),
        (true, true, true),
        (false, false, true),
        (false, true, false),
    ] {
        st.dry_run = dry;
        st.swing_execution_enabled = enabled;
        assert_eq!(x::swing_gates(&st).dry_run, expect, "{dry} {enabled}");
    }
}

#[test]
fn swing_gates_never_allow_intraday_or_options() {
    let mut st = live();
    st.intraday_enabled = true;
    st.options_enabled = true;
    let gates = x::swing_gates(&st);
    assert!(!gates.intraday_enabled && !gates.options_enabled && !gates.fno_carry_enabled);
}

#[test]
fn the_auto_execute_flag_defaults_false_and_execution_is_off_by_default() {
    let st = Settings::defaults().unwrap();
    assert!(
        !st.swing_auto_execute,
        "SW25: BASKFY_SWING_AUTO_EXECUTE defaults false"
    );
    assert!(!st.swing_execution_enabled);
    assert!(st.dry_run);
}

#[test]
fn the_swing_journal_sits_beside_the_desks() {
    assert_eq!(
        x::swing_journal_path(),
        "data/outputs/swing_orders_journal.jsonl"
    );
}

#[test]
fn pending_range_is_not_in_the_executable_kinds_at_source_level() {
    assert!(!EXECUTABLE_KINDS.contains(&PENDING_RANGE));
    assert_eq!(
        EXECUTABLE_KINDS,
        ["BUY_ON_TRIGGER", "SELL_AT_OPEN", "RAISE_GTT_STOP"]
    );
    assert_eq!(x::ENTRY_ORDER_TYPES, ["LIMIT", "MARKET"]);
}

#[test]
fn risk_multiplier_for_is_half_only_for_a_real_order_with_sessions_left() {
    let mut cfg = MemoryStore::new().book.config.unwrap();
    cfg.first_live_sessions_left = 5;
    assert_eq!(x::risk_multiplier_for(&cfg, false), d("0.5"));
    assert_eq!(x::risk_multiplier_for(&cfg, true), Decimal::ONE);
    cfg.first_live_sessions_left = 0;
    assert_eq!(x::risk_multiplier_for(&cfg, false), Decimal::ONE);
}

#[test]
fn is_simulated_gtt_reads_the_dry_prefix() {
    assert!(x::is_simulated_gtt(Some("DRY-p:ALPHA:GTT")));
    assert!(!x::is_simulated_gtt(Some("4242")));
    assert!(!x::is_simulated_gtt(None));
}

// --- G2: the caller's refusals (400 / 404 / 410 / 409) -------------------------------------

#[tokio::test]
async fn refuses_400_when_confirm_is_not_true() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let clock = FakeClock::default();
    for confirm in ["", "false", "True", "yes"] {
        let r = execute_full(
            &mut store,
            &gw,
            &plan,
            line,
            confirm,
            now(),
            None,
            None,
            &clock,
        )
        .await;
        assert_eq!(http_status(r), 400);
    }
    assert!(gw.calls().is_empty() && store.line_state(line) == "PROPOSED");
}

#[tokio::test]
async fn refuses_404_for_an_unknown_plan_and_a_line_not_in_the_plan() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let other = store.add_plan();
    let line = store.add_line(&other);
    assert_eq!(
        http_status(execute(&mut store, &gw, "nope", line).await),
        404
    );
    assert_eq!(
        http_status(execute(&mut store, &gw, &plan, line).await),
        404
    );
    assert_eq!(http_status(execute(&mut store, &gw, &plan, 99).await), 404);
    assert!(gw.calls().is_empty());
}

#[tokio::test]
async fn refuses_410_for_an_expired_plan_and_marks_the_line_expired() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan_built(now() - Duration::minutes(31));
    let line = store.add_line(&plan);
    assert_eq!(
        http_status(execute(&mut store, &gw, &plan, line).await),
        410
    );
    assert_eq!(store.line_state(line), "EXPIRED");
    assert!(gw.calls().is_empty());
}

#[tokio::test]
async fn the_expiry_boundary_is_thirty_minutes_exactly() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan_built(now() - Duration::minutes(30));
    let line = store.add_line(&plan);
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(
        out.status, "SIMULATED",
        "at exactly 30 minutes the plan still stands"
    );

    let mut store = MemoryStore::new();
    let plan = store.add_plan_built(now() - Duration::minutes(30) - Duration::seconds(1));
    let line = store.add_line(&plan);
    assert_eq!(
        http_status(execute(&mut store, &gw, &plan, line).await),
        410
    );
}

#[tokio::test]
async fn refuses_409_when_the_line_is_not_proposed_and_a_repost_cannot_send_twice() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let first = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(first.status, "SIMULATED");
    assert_eq!(
        http_status(execute(&mut store, &gw, &plan, line).await),
        409
    );
    assert_eq!(
        gw.places().len(),
        1,
        "one order, however often the form is posted"
    );
    assert_eq!(store.book.positions.len(), 1);
}

#[tokio::test]
async fn pending_range_is_refused_400_before_the_lock_and_left_untouched() {
    for settings in [paper(), live()] {
        let gw = FakeGateway::new(&settings);
        let mut store = MemoryStore::new();
        let plan = store.add_plan();
        let line = store.add_line_with(&plan, |l| {
            l.kind = "PENDING_RANGE".to_owned();
            l.quantity = 0;
            l.stop = None;
            l.symbol = "GAPCO".to_owned();
        });
        assert_eq!(
            http_status(execute(&mut store, &gw, &plan, line).await),
            400
        );
        assert!(store.locks.is_empty() && gw.calls().is_empty());
        assert_eq!(store.line_state(line), "PROPOSED");
    }
}

#[tokio::test]
async fn an_unknown_kind_is_refused_400_and_sends_nothing() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| "SHORT_AT_OPEN".clone_into(&mut l.kind));
    assert_eq!(
        http_status(execute(&mut store, &gw, &plan, line).await),
        400
    );
    assert!(gw.calls().is_empty());
}

// --- G3: the buy, dry run ------------------------------------------------------------------

#[tokio::test]
async fn a_dry_run_buy_fills_the_whole_line_and_arms_its_gtt_in_the_same_call() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert!(out.simulated && out.reason.is_empty());
    assert_eq!(out.filled_quantity, 100);
    let pid = out.position_id.unwrap();
    let pos = &store.book.positions[&pid];
    assert_eq!((pos.quantity_entered, pos.quantity_open), (100, 100));
    assert_eq!(pos.entry_avg, Some(d("100.0000")));
    assert_eq!(pos.stop, Some(d("96.00")));
    assert_eq!(
        pos.gtt_id.as_deref(),
        Some(format!("DRY-{plan}:ALPHA:GTT").as_str())
    );
    assert_eq!(pos.gtt_trigger, Some(d("96.00")));
    assert!(pos.simulated && !pos.half_risk);
    let gtts = gw.gtts();
    assert_eq!(
        gtts.len(),
        1,
        "every buy gets its GTT stop in the same call"
    );
    assert_eq!((gtts[0].qty, gtts[0].trigger), (100, 96.0));
    assert_eq!(
        gtts[0].limit_fraction,
        Some(gw.settings.swing_gtt_limit_fraction)
    );
    assert_eq!(store.line_state(line), "FILLED");
    assert_eq!(store.book.lines[&line].position_id, Some(pid));
    assert_eq!(store.session(), ("DRY_RUN".to_owned(), 1, 1, 0));
    assert_eq!(gw.broker(), 0, "0 orders reach a broker");
}

#[tokio::test]
async fn the_line_is_confirmed_before_the_gateway_is_called() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    execute(&mut store, &gw, &plan, line).await.unwrap();
    let states: Vec<&str> = store
        .line_history
        .iter()
        .map(|(_, st)| st.as_str())
        .collect();
    assert_eq!(states, ["CONFIRMED", "FILLED"]);
}

#[tokio::test]
async fn the_order_and_gtt_client_ids_are_plan_id_symbol_and_distinct() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    execute(&mut store, &gw, &plan, line).await.unwrap();
    let order = &gw.places()[0];
    assert_eq!(
        order.client_id.as_deref(),
        Some(format!("{plan}:ALPHA:BUY").as_str())
    );
    assert_eq!(
        gw.gtts()[0].client_id.as_deref(),
        Some(format!("{plan}:ALPHA:GTT").as_str())
    );
    assert_eq!(order.product, "CNC", "the swing book is CNC-only");
    assert_eq!(
        (order.side.as_str(), order.exchange.as_str()),
        ("BUY", "NSE")
    );
    assert_eq!(order.order_type, "MARKET");
    assert_eq!(order.price, None, "a MARKET order carries no price");
    assert_eq!(
        order.reference_price,
        Some(100.0),
        "the drill falls back to the trigger"
    );
    let tenant = order.tenant.unwrap();
    assert_eq!(order.plan_tenant, Some(tenant));
}

#[tokio::test]
async fn a_buy_with_zero_quantity_or_a_stop_not_below_trigger_is_blocked_before_the_gateway() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let zero = store.add_line_with(&plan, |l| l.quantity = 0);
    let out = execute(&mut store, &gw, &plan, zero).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(out.reason.contains("is not a buy"), "{}", out.reason);
    let high = store.add_line_with(&plan, |l| l.stop = Some(d("100.00")));
    let out = execute(&mut store, &gw, &plan, high).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(out.reason.contains("is not below entry"), "{}", out.reason);
    assert!(gw.calls().is_empty());
    assert_eq!(store.line_state(zero), "REJECTED");
}

#[tokio::test]
async fn an_untouchable_instrument_raises_and_marks_the_line_rejected() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.symbol = "SGBJUN31I".to_owned();
        l.instrument_id = 77;
    });
    let r = execute(&mut store, &gw, &plan, line).await;
    assert!(matches!(r, Err(ExecError::Gateway(_))), "{r:?}");
    assert_eq!(store.line_state(line), "REJECTED");
    assert!(store.book.positions.is_empty());
    assert_eq!(
        store.session().1,
        1,
        "the click is counted once: the rollback took the first"
    );
}

#[tokio::test]
async fn a_duplicate_from_the_gateway_is_blocked_with_no_second_position() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    gw.sent
        .lock()
        .unwrap()
        .insert(format!("{plan}:ALPHA:BUY"), "DRY-earlier".to_owned());
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(
        out.reason
            .contains("already sent for this plan (DUPLICATE: DRY-earlier)")
    );
    assert!(store.book.positions.is_empty() && gw.gtts().is_empty());
    assert_eq!(store.line_state(line), "REJECTED");
}

#[tokio::test]
async fn a_refused_gtt_leaves_a_naked_position_and_says_so() {
    let mut gw = FakeGateway::new(&paper());
    gw.refuse_gtt = true;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert!(
        out.reason.contains("the stop was not armed"),
        "{}",
        out.reason
    );
    assert!(out.reason.contains("is NAKED, re-arm it"));
    let pos = &store.book.positions[&out.position_id.unwrap()];
    assert_eq!(pos.gtt_id, None);
}

#[tokio::test]
async fn flag_off_with_dry_run_false_still_never_reaches_the_broker() {
    let mut st = paper();
    st.dry_run = false;
    assert!(
        x::swing_gates(&st).dry_run,
        "the flag is the swing book's own dry run"
    );
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(gw.broker(), 0);
}

#[tokio::test]
async fn protection_unresolved_refuses_every_buy() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    store.issues.push(ProtectionIssue {
        symbol: "BETA".to_owned(),
        kind: "MISSING".to_owned(),
        detail: "the broker lists no trigger".to_owned(),
        position_id: 9,
    });
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert_eq!(
        out.reason,
        "PROTECTION_UNRESOLVED: BETA: MISSING — the broker lists no trigger"
    );
    assert!(gw.calls().is_empty());
}

// --- G4: the confirm-time gate (A5) --------------------------------------------------------

#[tokio::test]
async fn session_cap_after_three_entries_today() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::with(0, 3, MarketGate::Green, d("1000000"));
    let plan = store.add_plan();
    let mut ids = Vec::new();
    for (sym, inst) in [("ALPHA", 11), ("BETA", 12), ("GAMMA", 13), ("DELTA", 14)] {
        ids.push(store.add_line_with(&plan, |l| {
            l.symbol = sym.to_owned();
            l.instrument_id = inst;
            l.quantity = 10;
        }));
    }
    let mut statuses = Vec::new();
    let mut last = None;
    for id in &ids {
        let out = execute(&mut store, &gw, &plan, *id).await.unwrap();
        statuses.push(out.status.clone());
        last = Some(out);
    }
    assert_eq!(statuses, ["SIMULATED", "SIMULATED", "SIMULATED", "BLOCKED"]);
    let last = last.unwrap();
    assert!(
        last.reason.starts_with("SESSION_CAP: DELTA — "),
        "{}",
        last.reason
    );
    assert_eq!(store.book.positions.len(), 3);
    assert_eq!(store.line_state(ids[3]), "REJECTED");
    assert_eq!(gw.places().len(), 3);
}

#[tokio::test]
async fn tier_full_when_the_rung_allows_no_more_positions() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::with(0, 0, MarketGate::Green, d("1000000"));
    for (i, sym) in ["BETA", "GAMMA"].iter().enumerate() {
        store.add_position_with(|p| {
            p.symbol = (*sym).to_owned();
            p.instrument_id = 20 + i64::try_from(i).unwrap();
            p.quantity_open = 10;
            p.quantity_entered = 10;
        });
    }
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.quantity = 10);
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(
        out.reason.starts_with("TIER_FULL: ALPHA — "),
        "{}",
        out.reason
    );
    assert!(gw.calls().is_empty());
}

#[tokio::test]
async fn a_red_gate_or_a_drawdown_lock_refuses_the_confirm() {
    for (red, locked, code) in [(true, false, "GATE_RED"), (false, true, "DRAWDOWN_LOCKOUT")] {
        let gw = FakeGateway::new(&paper());
        let gate = if red {
            MarketGate::Red
        } else {
            MarketGate::Green
        };
        let mut store = MemoryStore::with(0, 3, gate, d("1000000"));
        store.drawdown_locked = locked;
        let plan = store.add_plan();
        let line = store.add_line(&plan);
        let out = execute(&mut store, &gw, &plan, line).await.unwrap();
        assert_eq!(out.status, "BLOCKED");
        assert!(out.reason.starts_with(code), "{code}: {}", out.reason);
        assert!(gw.calls().is_empty());
    }
}

#[tokio::test]
async fn a_name_with_no_adr_on_record_is_refused() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    store.detected.remove("ALPHA");
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(
        out.reason
            .starts_with("SIZE_REFUSED: ALPHA — no detection row for ALPHA before 2026-09-02")
    );
}

#[tokio::test]
async fn a_setup_this_book_does_not_trade_is_refused() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.setup = Some("CUP".to_owned()));
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(
        out.reason
            .starts_with("NOT_TRADEABLE_SETUP: ALPHA — setup 'CUP'"),
        "{}",
        out.reason
    );
}

#[tokio::test]
async fn the_confirm_never_sends_more_than_the_line_said() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.quantity = 5);
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(gw.places()[0].qty, 5);
    assert!(store.resizes.is_empty());
}

#[tokio::test]
async fn the_lock_is_taken_for_every_kind_on_the_session_day() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    store.add_position_with(|_| {});
    let plan = store.add_plan();
    let sell = store.add_line_with(&plan, |l| {
        "SELL_AT_OPEN".clone_into(&mut l.kind);
        l.quantity = 100;
    });
    let raise = store.add_line_with(&plan, |l| {
        "RAISE_GTT_STOP".clone_into(&mut l.kind);
        l.stop = Some(d("98.00"));
    });
    let clock = FakeClock::default();
    for line in [sell, raise] {
        execute_full(
            &mut store,
            &gw,
            &plan,
            line,
            "true",
            now(),
            Some(d("104")),
            None,
            &clock,
        )
        .await
        .unwrap();
    }
    assert_eq!(store.locks, vec![now().date_naive(); 2]);
}

#[tokio::test]
async fn a_gateway_exception_rolls_the_lock_back_and_records_the_refusal_once() {
    let mut gw = FakeGateway::new(&paper());
    gw.explode_place = true;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let r = execute(&mut store, &gw, &plan, line).await;
    assert!(matches!(r, Err(ExecError::Gateway(_))));
    assert_eq!(store.line_state(line), "REJECTED");
    assert!(store.book.positions.is_empty() && store.book.fills.is_empty());
    assert_eq!(store.session(), ("DRY_RUN".to_owned(), 1, 0, 0));
}

// --- G5: the sell and the raise ------------------------------------------------------------

async fn sell_line(
    store: &mut MemoryStore,
    gw: &FakeGateway,
    quantity: i64,
    last_price: Option<Decimal>,
) -> ExecOutcome {
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        "SELL_AT_OPEN".clone_into(&mut l.kind);
        l.quantity = quantity;
    });
    let clock = FakeClock::default();
    execute_full(
        store,
        gw,
        &plan,
        line,
        "true",
        now(),
        last_price,
        None,
        &clock,
    )
    .await
    .unwrap()
}

#[tokio::test]
async fn a_sell_for_more_than_quantity_open_or_a_name_not_held_is_blocked() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    store.add_position_with(|_| {});
    let out = sell_line(&mut store, &gw, 301, Some(d("104"))).await;
    assert_eq!(out.status, "BLOCKED");
    let mut empty = MemoryStore::new();
    let out = sell_line(&mut empty, &gw, 10, Some(d("104"))).await;
    assert_eq!(out.status, "BLOCKED");
    let out = sell_line(&mut store, &gw, 0, Some(d("104"))).await;
    assert_eq!(out.status, "BLOCKED");
    assert!(
        gw.places().is_empty(),
        "the sleeve never sells what it did not buy"
    );
}

#[tokio::test]
async fn a_sell_without_a_last_price_is_blocked_not_guessed() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    store.add_position_with(|_| {});
    let out = sell_line(&mut store, &gw, 100, None).await;
    assert_eq!(out.status, "BLOCKED");
    assert!(gw.places().is_empty());
}

#[tokio::test]
async fn a_partial_sell_resizes_the_remainders_stop_and_a_close_out_books_the_trade() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|_| {});
    let out = sell_line(&mut store, &gw, 100, Some(d("110"))).await;
    assert_eq!(out.status, "SIMULATED", "{}", out.reason);
    let pos = store.book.positions[&pid].clone();
    assert_eq!(pos.quantity_open, 200);
    assert_ne!(pos.state, "CLOSED");
    assert!(pos.gtt_id.is_some(), "the remainder stays protected");
    let order = &gw.places()[0];
    assert_eq!(
        (order.side.as_str(), order.qty, order.product.as_str()),
        ("SELL", 100, "CNC")
    );
    let out = sell_line(&mut store, &gw, 200, Some(d("108"))).await;
    assert_eq!(out.status, "SIMULATED", "{}", out.reason);
    let pos = &store.book.positions[&pid];
    assert_eq!((pos.quantity_open, pos.state.as_str()), (0, "CLOSED"));
    assert!(pos.r_multiple.is_some() && pos.pnl_inr.is_some() && pos.exit_avg.is_some());
    assert_eq!(gw.broker(), 0);
}

#[tokio::test]
async fn a_raise_below_or_equal_to_the_resting_stop_is_blocked() {
    for stop in ["95.00", "96.00"] {
        let gw = FakeGateway::new(&paper());
        let mut store = MemoryStore::new();
        store.add_position_with(|_| {});
        let plan = store.add_plan();
        let line = store.add_line_with(&plan, |l| {
            "RAISE_GTT_STOP".clone_into(&mut l.kind);
            l.stop = Some(d(stop));
        });
        let clock = FakeClock::default();
        let out = execute_full(
            &mut store,
            &gw,
            &plan,
            line,
            "true",
            now(),
            Some(d("104")),
            None,
            &clock,
        )
        .await
        .unwrap();
        assert_eq!(out.status, "BLOCKED", "a stop never falls ({stop})");
        assert!(gw.calls().is_empty());
    }
}

#[tokio::test]
async fn a_raise_replaces_the_trigger_and_updates_the_position() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|_| {});
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        "RAISE_GTT_STOP".clone_into(&mut l.kind);
        l.stop = Some(d("99.00"));
    });
    let clock = FakeClock::default();
    let out = execute_full(
        &mut store,
        &gw,
        &plan,
        line,
        "true",
        now(),
        Some(d("104")),
        None,
        &clock,
    )
    .await
    .unwrap();
    assert_eq!(out.status, "SIMULATED", "{}", out.reason);
    let pos = &store.book.positions[&pid];
    assert_eq!(pos.stop, Some(d("99.00")));
    assert_eq!(pos.gtt_trigger, Some(d("99.00")));
    assert_eq!(gw.gtts().len(), 1);
    assert_eq!(gw.gtts()[0].qty, 300);
    assert_eq!(
        store.session().3,
        1,
        "a raise is a manage action, not a fill"
    );
}

#[tokio::test]
async fn a_raise_at_or_above_the_last_price_is_blocked_before_cancelling() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    store.add_position_with(|p| p.gtt_id = Some("777".to_owned()));
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        "RAISE_GTT_STOP".clone_into(&mut l.kind);
        l.stop = Some(d("104.00"));
    });
    let clock = FakeClock::default();
    let out = execute_full(
        &mut store,
        &gw,
        &plan,
        line,
        "true",
        now(),
        Some(d("104")),
        None,
        &clock,
    )
    .await
    .unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(
        gw.calls().is_empty(),
        "the resting stop is never pulled for a refused raise"
    );
}

// --- G6: re-arming a naked position ---------------------------------------------------------

#[tokio::test]
async fn rearm_refuses_without_confirm_and_for_an_unknown_position() {
    let st = paper();
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    let r = x::rearm_gtt(&mut store, &gw, &st, 1, "false", now(), None).await;
    assert_eq!(http_status(r), 400);
    let r = x::rearm_gtt(&mut store, &gw, &st, 1, "true", now(), None).await;
    assert_eq!(http_status(r), 404);
}

#[tokio::test]
async fn rearm_arms_a_naked_position_for_quantity_open_once_a_day() {
    let st = paper();
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = None;
        p.quantity_open = 120;
    });
    let out = x::rearm_gtt(&mut store, &gw, &st, pid, "true", now(), Some(d("103")))
        .await
        .unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(gw.gtts()[0].qty, 120);
    assert_eq!(
        gw.gtts()[0].client_id.as_deref(),
        Some("rearm:1:ALPHA:GTT:2026-09-02")
    );
    assert!(store.book.positions[&pid].gtt_id.is_some());
    // A second re-arm the same day is a duplicate at the gateway (after the row is cleared).
    store.book.positions.get_mut(&pid).unwrap().gtt_id = None;
    let again = x::rearm_gtt(&mut store, &gw, &st, pid, "true", now(), Some(d("103")))
        .await
        .unwrap();
    assert_eq!(again.status, "BLOCKED");
    assert!(again.reason.contains("DUPLICATE"), "{}", again.reason);
}

#[tokio::test]
async fn rearm_on_a_covered_or_closed_position_is_blocked() {
    let st = paper();
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    let covered = store.add_position_with(|_| {});
    let closed = store.add_position_with(|p| {
        p.gtt_id = None;
        p.quantity_open = 0;
        "CLOSED".clone_into(&mut p.state);
    });
    for pid in [covered, closed] {
        let out = x::rearm_gtt(&mut store, &gw, &st, pid, "true", now(), Some(d("103")))
            .await
            .unwrap();
        assert_eq!(out.status, "BLOCKED");
    }
    assert!(gw.calls().is_empty());
}

#[tokio::test]
async fn the_eod_sweep_rearms_naked_positions_given_a_price() {
    let st = paper();
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    store.add_position_with(|p| p.gtt_id = None);
    let mut prices = BTreeMap::new();
    prices.insert("ALPHA".to_owned(), d("103"));
    let outs = x::eod_gtt_sweep(&mut store, &gw, &st, now(), &prices)
        .await
        .unwrap();
    assert_eq!(outs.len(), 1);
    assert_eq!(outs[0].status, "SIMULATED");
}

// --- G7: the first live sessions, the entry cap and the fill poll (A8, A9) ------------------

#[tokio::test]
async fn first_live_halves_the_risk_at_plan_time_once_and_tags_the_position() {
    let gw = FakeGateway::new(&live());
    let mut store = MemoryStore::with(5, 3, MarketGate::Green, d("1000000"));
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.quantity = 10_000);
    let orders = ScriptedOrders::new(&[("COMPLETE", 10_000, "100.00")]);
    let clock = FakeClock::default();
    let out = execute_live(&mut store, &gw, &plan, line, Some(&orders), &clock).await;
    let sent = gw.places()[0].qty;
    let mut full = MemoryStore::with(0, 3, MarketGate::Green, d("1000000"));
    let gw_full = FakeGateway::new(&live());
    let plan_f = full.add_plan();
    let line_f = full.add_line_with(&plan_f, |l| l.quantity = 10_000);
    let orders_f = ScriptedOrders::new(&[("COMPLETE", 10_000, "100.00")]);
    execute_live(
        &mut full,
        &gw_full,
        &plan_f,
        line_f,
        Some(&orders_f),
        &clock,
    )
    .await;
    let full_qty = gw_full.places()[0].qty;
    assert_eq!(
        sent * 2,
        full_qty,
        "half risk, applied once ({sent} vs {full_qty})"
    );
    let pos = &store.book.positions[&out.position_id.unwrap()];
    assert!(pos.half_risk);
    assert_eq!(
        store.book.config.as_ref().unwrap().first_live_sessions_left,
        5,
        "the countdown is never moved by a request"
    );
}

#[tokio::test]
async fn first_live_never_halves_a_simulated_confirm() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::with(5, 3, MarketGate::Green, d("1000000"));
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(gw.places()[0].qty, 100);
    assert!(!store.book.positions[&out.position_id.unwrap()].half_risk);
}

#[tokio::test]
async fn the_entry_cap_is_a8s_and_is_sent_as_market_protection() {
    let gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.trigger = Some(d("100.80"));
        l.stop = Some(d("97.80"));
    });
    store.range_highs.insert(line, d("100.00"));
    let orders = ScriptedOrders::new(&[("COMPLETE", 100, "100.9")]);
    let clock = FakeClock::default();
    execute_full(
        &mut store,
        &gw,
        &plan,
        line,
        "true",
        now(),
        Some(d("100.80")),
        Some(&orders),
        &clock,
    )
    .await
    .unwrap();
    let order = &gw.places()[0];
    assert_eq!(order.order_type, "MARKET");
    assert_eq!(order.price, None);
    assert_eq!(order.market_protection, Some(0.44));
}

#[tokio::test]
async fn a_price_past_the_cap_or_no_price_at_all_is_refused_live() {
    let gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.trigger = Some(d("100.80"));
        l.stop = Some(d("97.80"));
    });
    store.range_highs.insert(line, d("100.00"));
    let clock = FakeClock::default();
    let out = execute_full(
        &mut store,
        &gw,
        &plan,
        line,
        "true",
        now(),
        Some(d("101.25")),
        None,
        &clock,
    )
    .await
    .unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(out.reason.contains("run past the entry cap"));
    let line2 = store.add_line_with(&plan, |l| {
        l.symbol = "BETA".to_owned();
        l.instrument_id = 12;
    });
    let out = execute_full(
        &mut store,
        &gw,
        &plan,
        line2,
        "true",
        now(),
        None,
        None,
        &clock,
    )
    .await
    .unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(out.reason.contains("no live price"));
    assert!(gw.places().is_empty(), "nothing reached the broker");
}

#[tokio::test]
async fn the_request_polls_at_most_ten_seconds_at_two_a_second() {
    let gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 0, "0")]);
    let clock = FakeClock::default();
    let out = execute_live(&mut store, &gw, &plan, line, Some(&orders), &clock).await;
    assert_eq!(out.status, "SENT");
    assert_eq!((out.position_id, out.filled_quantity), (None, 0));
    assert!((19..=21).contains(&orders.polls()), "{}", orders.polls());
    assert!(clock.slept.lock().unwrap().iter().all(|s| *s == 0.5));
    assert!(clock.now() <= 10.0);
    assert_eq!(store.line_state(line), "SENT");
    assert!(gw.gtts().is_empty(), "no stop for shares not held");
}

#[tokio::test]
async fn the_poll_stops_early_on_complete() {
    let gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[
        ("OPEN", 0, "0"),
        ("OPEN", 40, "100.10"),
        ("COMPLETE", 100, "100.15"),
    ]);
    let clock = FakeClock::default();
    let out = execute_live(&mut store, &gw, &plan, line, Some(&orders), &clock).await;
    assert_eq!(out.status, "FILLED");
    assert_eq!(orders.polls(), 3);
    assert_eq!(clock.now(), 1.0);
}

#[tokio::test]
async fn complete_writes_the_position_the_fill_and_the_gtt_in_the_same_request() {
    let gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("COMPLETE", 100, "100.15")]);
    let clock = FakeClock::default();
    let out = execute_live(&mut store, &gw, &plan, line, Some(&orders), &clock).await;
    assert_eq!(out.status, "FILLED");
    assert!(!out.simulated);
    let pid = out.position_id.unwrap();
    let pos = &store.book.positions[&pid];
    assert_eq!((pos.quantity_entered, pos.quantity_open), (100, 100));
    assert_eq!(pos.entry_avg, Some(d("100.1500")));
    assert_eq!(pos.gtt_id.as_deref(), Some("4242"));
    assert_eq!(pos.gtt_trigger, Some(d("96.00")));
    assert!(!pos.simulated);
    let fill = &store.book.fills[0];
    assert!(fill.contains(&("journal_ref", Val::Text("ORD1".to_owned()))));
    assert!(fill.contains(&("price", Val::Dec(d("100.15")))));
    assert_eq!((gw.gtts()[0].qty, gw.gtts()[0].last_price), (100, 100.15));
    assert_eq!(store.line_state(line), "FILLED");
    assert_eq!(store.session().2, 1);
}

#[tokio::test]
async fn a_partial_fill_is_sent_with_a_gtt_for_exactly_the_filled_quantity() {
    let gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 40, "100.10")]);
    let clock = FakeClock::default();
    let out = execute_live(&mut store, &gw, &plan, line, Some(&orders), &clock).await;
    assert_eq!((out.status.as_str(), out.filled_quantity), ("SENT", 40));
    let pos = &store.book.positions[&out.position_id.unwrap()];
    assert_eq!((pos.quantity_entered, pos.quantity_open), (40, 40));
    assert_eq!(gw.gtts()[0].qty, 40, "never the 100 the line asked for");
    assert_eq!(store.line_state(line), "SENT");
    assert_eq!(store.book.lines[&line].journal_ref.as_deref(), Some("ORD1"));
    assert_eq!(store.session().2, 1, "a partial is a fill");
}

#[tokio::test]
async fn the_postback_modifies_the_gtt_and_never_places_a_second_one() {
    let st = live();
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 10, "100.00")]);
    let clock = FakeClock::default();
    execute_live(&mut store, &gw, &plan, line, Some(&orders), &clock).await;
    for filled in [20, 30, 100] {
        let update = OrderUpdate {
            order_id: "ORD1".to_owned(),
            status: if filled < 100 { "OPEN" } else { "COMPLETE" }.to_owned(),
            filled_quantity: filled,
            average_price: d("100.00"),
        };
        x::on_order_update(&mut store, &gw, &st, &update, now())
            .await
            .unwrap();
    }
    assert_eq!(gw.gtts().len(), 1);
    let qtys: Vec<i64> = gw.modifies().iter().map(|m| m.qty).collect();
    assert_eq!(qtys, [20, 30, 100]);
    assert!(gw.modifies().iter().all(|m| m.gtt_id == 4242));
    assert_eq!(store.book.positions[&1].quantity_open, 100);
    // A repeated postback changes nothing (the line is FILLED now).
    let again = OrderUpdate {
        order_id: "ORD1".to_owned(),
        status: "COMPLETE".to_owned(),
        filled_quantity: 100,
        average_price: d("100.00"),
    };
    let none = x::on_order_update(&mut store, &gw, &st, &again, now())
        .await
        .unwrap();
    assert!(none.is_none());
    assert_eq!(gw.modifies().len(), 3);
}

#[tokio::test]
async fn a_postback_for_an_unknown_order_is_ignored() {
    let st = live();
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    let update = OrderUpdate {
        order_id: "WEEKLY-9".to_owned(),
        status: "COMPLETE".to_owned(),
        filled_quantity: 5,
        average_price: d("10"),
    };
    let r = x::on_order_update(&mut store, &gw, &st, &update, now())
        .await
        .unwrap();
    assert!(r.is_none() && gw.calls().is_empty() && store.locks.is_empty());
}

#[tokio::test]
async fn the_cutoff_cancels_the_open_remainder_and_frees_unclaimed_slots() {
    let st = live();
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let pending = store.add_line_with(&plan, |l| {
        "PENDING_RANGE".clone_into(&mut l.kind);
        l.quantity = 0;
        l.stop = None;
        l.symbol = "GAPCO".to_owned();
        l.instrument_id = 19;
    });
    let orders = ScriptedOrders::new(&[("OPEN", 40, "100.00")]);
    let clock = FakeClock::default();
    execute_live(&mut store, &gw, &plan, line, Some(&orders), &clock).await;
    let report = x::cutoff_open_orders(&mut store, &gw, &st, Some(&orders), now())
        .await
        .unwrap();
    assert_eq!(report.cancelled, 1);
    assert_eq!(report.slots_freed, 1);
    assert_eq!(store.line_state(pending), "EXPIRED");
    let cancels = gw
        .calls()
        .into_iter()
        .filter(|c| matches!(c, Call::Cancel(_)))
        .count();
    assert_eq!(cancels, 1);
    assert_eq!(
        gw.gtts().len(),
        1,
        "the GTT covering what filled is untouched"
    );
    assert!(gw.modifies().is_empty());
}

#[tokio::test]
async fn a_rejected_order_with_nothing_filled_writes_no_book() {
    let gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("REJECTED", 0, "0")]);
    let clock = FakeClock::default();
    let out = execute_live(&mut store, &gw, &plan, line, Some(&orders), &clock).await;
    assert_eq!(out.status, "REJECTED");
    assert!(store.book.positions.is_empty() && gw.gtts().is_empty());
}

#[tokio::test]
async fn prices_are_float_at_the_gateway_boundary_and_decimal_in_the_store() {
    let gw = FakeGateway::new(&paper());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = execute(&mut store, &gw, &plan, line).await.unwrap();
    assert_eq!(gw.gtts()[0].trigger, 96.0);
    let pos = &store.book.positions[&out.position_id.unwrap()];
    assert_eq!(pos.entry_avg, Some(d("100.0000")));
}

#[test]
fn the_session_day_is_the_ist_date() {
    // 20:00 UTC on 1 Sep is 01:30 IST on 2 Sep.
    let utc = FixedOffset::east_opt(0).unwrap();
    let late = utc.with_ymd_and_hms(2026, 9, 1, 20, 0, 0).unwrap();
    assert_eq!(
        late.with_timezone(&ist()).date_naive(),
        NaiveDate::from_ymd_opt(2026, 9, 2).unwrap()
    );
}

// --- the live gate (SW30) ------------------------------------------------------------------

#[test]
fn the_live_gate_session_is_0915_to_1530_on_a_weekday_both_ends_included() {
    let at = |h: u32, m: u32, day: u32| ist().with_ymd_and_hms(2026, 9, day, h, m, 0).unwrap();
    assert!(!in_session(&at(9, 14, 2)));
    assert!(in_session(&at(9, 15, 2)));
    assert!(in_session(&at(15, 30, 2)));
    assert!(!in_session(&at(15, 31, 2)));
    assert!(!in_session(&at(11, 0, 5)), "Saturday has no session");
    assert!(!in_session(&at(11, 0, 6)), "nor Sunday");
}

#[test]
fn the_gate_index_quote_symbols_are_the_workers_table() {
    assert_eq!(quote_symbol("nifty-smallcap-250"), Some("NIFTY SMLCAP 250"));
    assert_eq!(
        quote_symbol("nifty-mid-small-400"),
        Some("NIFTY MIDSML 400")
    );
    assert_eq!(quote_symbol("nifty-500"), Some("NIFTY 500"));
    assert_eq!(quote_symbol("nifty-50"), None);
}

#[test]
fn the_gate_index_follows_the_setting_sw29() {
    let st = Settings::defaults().unwrap();
    assert_eq!(st.swing_index_slug, "nifty-smallcap-250");
}

// --- review additions (adversarial review of lane swing_exec, 30 Oct 2026) -----------------

/// Python's `test_swing_execute.py` AST test: the module names no broker method. Every order,
/// GTT, delete, modify and cancel goes through the [`Gateway`] trait it is handed.
#[test]
fn the_module_names_no_broker_method_and_holds_no_kite_handle() {
    let src = include_str!("execute.rs");
    for banned in [
        "place_order(",
        concat!("Kite", "Writes"),
        "crate::kite",
        "kc.",
        "Broker",
        "unsafe",
    ] {
        assert!(!src.contains(banned), "execute.rs names {banned:?}");
    }
    let gate = include_str!("live_gate.rs");
    for banned in [
        ".place(",
        "place_gtt",
        "delete_gtt",
        "modify_gtt",
        "cancel_order",
        concat!("Kite", "Writes"),
        "Gateway",
        "unsafe",
    ] {
        assert!(!gate.contains(banned), "live_gate.rs names {banned:?}");
    }
}

/// `_sole_tenant()` raises before any gateway call on a non-positive id (law 2's tenant
/// clause): the GTT paths, which the shim does not stamp, refuse too — nothing reaches the
/// gateway, and the confirm is recorded REJECTED as any raise is.
#[tokio::test]
async fn a_non_positive_tenant_refuses_before_any_gateway_call() {
    let mut st = paper();
    st.sole_user_id = 0;
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let r = execute(&mut store, &gw, &plan, line).await;
    assert!(matches!(r, Err(ExecError::Gateway(_))), "{r:?}");
    assert!(gw.calls().is_empty(), "no call reaches the gateway");
    assert_eq!(store.line_state(line), "REJECTED");
    let pid = store.add_position_with(|p| p.gtt_id = None);
    let r = x::rearm_gtt(&mut store, &gw, &st, pid, "true", now(), Some(d("103"))).await;
    assert!(matches!(r, Err(ExecError::Gateway(_))), "{r:?}");
    assert!(
        gw.calls().is_empty(),
        "the re-arm's GTT is refused before the gateway"
    );
}

/// `_price(None)` raises in Python; a missing stop must never become a trigger at 0, and a
/// missing resting stop must never let a RAISE pass "a stop never falls".
#[tokio::test]
async fn a_missing_stop_is_an_error_never_a_trigger_at_zero() {
    let st = paper();
    let gw = FakeGateway::new(&st);
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = None;
        p.stop = None;
    });
    let r = x::rearm_gtt(&mut store, &gw, &st, pid, "true", now(), Some(d("103"))).await;
    assert!(matches!(r, Err(ExecError::Store(_))), "{r:?}");
    assert!(gw.gtts().is_empty(), "no GTT at 0");
}
