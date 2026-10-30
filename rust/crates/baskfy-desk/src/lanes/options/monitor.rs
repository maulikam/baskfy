//! Ports `app/strategies/nifty_options.py` (`NiftyOptionsMonitor`, OP9) and the runner half of
//! `app/options_monitor.py` (`build_monitor`, the `PositionStore` over the `op_` tables).
//!
//! **What it reads.** The index's ticks (the spot, the last-tick clock for `04` §8.5's feed
//! rule, and minute bars built from the ticks), each open leg's depth ticks (the conservative
//! mark: bid for a long, ask for a short), and the store's `op_position` rows. Minute bars are
//! reconciled against `op_index_minute` — the collector's minutes win where they exist (A4).
//!
//! **What it does.** On every tick, and on every idle pass, it calls the pure
//! [`baskfy_core::options::exits::evaluate`] for each open position without an exit. A verdict
//! becomes one exit plan through the store (idempotent: a position gets one exit plan, ever),
//! and the position is no longer evaluated. Every `mark_every` it records each position's mark.
//!
//! **What it does not do.** Hold a gateway — the struct has no field for one, so "never an
//! order" is the type's, not a convention (Python passes `gateway=None` to `BaseStrategy`). Raise
//! entry plans. Read the wall clock, except `_tick_time`'s last fallback (a tick with no time at
//! all), as Python's does.
//!
//! **Resume.** `on_start` loads the day's open positions, so a restart at 11:30 carries on; the
//! first reconcile brings back the morning's bars (OP9 AC).

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;

use async_trait::async_trait;
use baskfy_core::options::bars::{Bar, ist};
use baskfy_core::options::config::OptionsConfig;
use baskfy_core::options::exits::{
    ExitVerdict, IndexState, LegMark, Marks, OpenLeg, OpenPosition, evaluate, mark,
};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeZone};
use rust_decimal::Decimal;

use super::py;
use super::store::{PgOptionsStore, Tracked};
use crate::storage::Conn;
use crate::wire::{PyDict, PyValue};
use baskfy_providers::records::PyDateTime;

/// NIFTY 50's Kite instrument token (the index the whole book reads).
pub const NIFTY_50_TOKEN: i64 = 256_265;

/// `NiftyOptionsMonitor.name`.
pub const NAME: &str = "nifty_options_monitor";
/// `NiftyOptionsMonitor.products`.
pub const PRODUCTS: [&str; 1] = ["MIS"];

/// One line on the desk's log (stderr): composed here from ids, codes and error words, never a
/// secret.
#[allow(clippy::print_stderr)]
pub fn emit(line: &str) {
    eprintln!("{line}");
}

/// The session's last moment: nothing is watched after the close.
#[must_use]
pub fn session_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// An `op_position` the monitor watches: its session, the pure view the rules read, and the exit
/// plan already raised for it (`None` while it is still being evaluated).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrackedPosition {
    /// `op_session.id`.
    pub session_id: i64,
    /// What `exits.evaluate` reads.
    pub position: OpenPosition,
    /// The exit plan already raised.
    pub exit_plan_id: Option<String>,
}

impl TrackedPosition {
    /// `tokens`: every leg's instrument token, in leg order.
    #[must_use]
    pub fn tokens(&self) -> Vec<i64> {
        self.position
            .legs
            .iter()
            .map(|l| l.instrument_token)
            .collect()
    }

    /// The store's row view (`position_from_rows`) as the pure view.
    #[must_use]
    pub fn from_tracked(t: &Tracked) -> Self {
        Self {
            session_id: t.session_id,
            position: OpenPosition {
                sleeve: t.sleeve,
                structure: t.structure,
                legs: t
                    .legs
                    .iter()
                    .map(|l| OpenLeg {
                        role: l.role,
                        strike: l.strike,
                        quantity: l.quantity,
                        fill_price: l.fill_price,
                        instrument_token: l.instrument_token,
                    })
                    .collect(),
                entry_points: t.entry_points,
                opened_at: ist(&t.opened_at),
                risk_budget_inr: t.risk_budget_inr,
                direction: t.direction,
                range_high: t.range_high,
                range_low: t.range_low,
                half_gap: t.half_gap,
                width_points: t.width_points,
            },
            exit_plan_id: t.exit_plan_id.clone(),
        }
    }
}

/// What the monitor asks of the database — [`PgPositionStore`] on the desk, a list in tests.
/// An `Err` is the exception Python's store raises (the monitor logs it and carries on).
#[async_trait]
pub trait PositionStore: Send {
    /// `open_positions(day)`.
    async fn open_positions(&mut self, day: NaiveDate) -> Result<Vec<TrackedPosition>, String>;
    /// `raise_exit(tracked, verdict, at)`: the exit plan's id.
    async fn raise_exit(
        &mut self,
        tracked: &TrackedPosition,
        verdict: &ExitVerdict,
        at: NaiveDateTime,
    ) -> Result<Option<String>, String>;
    /// `record_mark(tracked, value, at)`.
    async fn record_mark(
        &mut self,
        tracked: &TrackedPosition,
        value: Decimal,
        at: NaiveDateTime,
    ) -> Result<(), String>;
    /// `index_minutes(day, until)`.
    async fn index_minutes(
        &mut self,
        day: NaiveDate,
        until: NaiveDateTime,
    ) -> Result<Vec<Bar>, String>;
}

/// `_aware(at)` for a naive IST time.
fn aware(at: NaiveDateTime) -> Result<DateTime<FixedOffset>, String> {
    py::ist()
        .from_local_datetime(&at)
        .single()
        .ok_or_else(|| "ValueError: not an IST time".to_owned())
}

/// `options_monitor.PgPositionStore` over one connection of its own.
pub struct PgPositionStore {
    /// The connection.
    pub conn: Conn,
    /// `BASKFY_SOLE_USER_ID`.
    pub user_id: i64,
}

#[async_trait]
impl PositionStore for PgPositionStore {
    async fn open_positions(&mut self, day: NaiveDate) -> Result<Vec<TrackedPosition>, String> {
        let mut store = PgOptionsStore::new(&mut self.conn, self.user_id);
        let rows = store.open_positions(day).await.map_err(|e| e.py_text())?;
        Ok(rows.iter().map(TrackedPosition::from_tracked).collect())
    }

    async fn raise_exit(
        &mut self,
        tracked: &TrackedPosition,
        verdict: &ExitVerdict,
        at: NaiveDateTime,
    ) -> Result<Option<String>, String> {
        let at = aware(at)?;
        let mut store = PgOptionsStore::new(&mut self.conn, self.user_id);
        let written = super::store::ExitVerdict {
            code: verdict.code.clone(),
            reason: verdict.reason.clone(),
            value_points: verdict.value_points,
            marked_loss_inr: verdict.marked_loss_inr,
            stale: verdict.stale,
        };
        let id = store
            .raise_exit(tracked.session_id, &written, at)
            .await
            .map_err(|e| e.py_text())?;
        if id.is_none() {
            emit(&format!(
                "ERROR desk.options_monitor options exit: session {} has no entry plan",
                tracked.session_id
            ));
        }
        Ok(id)
    }

    async fn record_mark(
        &mut self,
        tracked: &TrackedPosition,
        value: Decimal,
        at: NaiveDateTime,
    ) -> Result<(), String> {
        let at = aware(at)?;
        let mut store = PgOptionsStore::new(&mut self.conn, self.user_id);
        store
            .record_mark_of(
                tracked.session_id,
                tracked.position.structure,
                tracked.position.entry_points,
                value,
                at,
            )
            .await
            .map_err(|e| e.py_text())
    }

    async fn index_minutes(
        &mut self,
        day: NaiveDate,
        until: NaiveDateTime,
    ) -> Result<Vec<Bar>, String> {
        let until = aware(until)?;
        let mut store = PgOptionsStore::new(&mut self.conn, self.user_id);
        store
            .index_minutes(day, until)
            .await
            .map_err(|e| e.py_text())
    }
}

#[derive(Debug, Clone)]
struct Minute {
    open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
}

/// The monitor's memory for the day (`_State`). Everything here is rebuilt by a restart.
#[derive(Debug, Clone, Default)]
pub struct MonitorState {
    /// `positions`, in insertion order (a Python dict).
    pub positions: Vec<(i64, TrackedPosition)>,
    marks: HashMap<i64, LegMark>,
    ticked: BTreeMap<NaiveDateTime, Minute>,
    stored: BTreeMap<NaiveDateTime, Bar>,
    /// The last index price.
    pub spot: Option<Decimal>,
    /// The last index tick's time.
    pub last_index_tick: Option<NaiveDateTime>,
    refreshed_at: Option<NaiveDateTime>,
    reconciled_at: Option<NaiveDateTime>,
    marked_at: HashMap<i64, NaiveDateTime>,
}

impl MonitorState {
    /// The tracked position of a session.
    #[must_use]
    pub fn position(&self, session_id: i64) -> Option<&TrackedPosition> {
        self.positions
            .iter()
            .find(|(s, _)| *s == session_id)
            .map(|(_, t)| t)
    }

    fn set(&mut self, session_id: i64, tracked: TrackedPosition) {
        if let Some(slot) = self.positions.iter_mut().find(|(s, _)| *s == session_id) {
            slot.1 = tracked;
        } else {
            self.positions.push((session_id, tracked));
        }
    }
}

/// `_tick_time`'s last fallback: `dt.datetime.now().time()`, the process's local wall clock.
pub type WallTime = Arc<dyn Fn() -> NaiveTime + Send + Sync>;

/// One verdict raised this session: `(session_id, code, reason, at)`.
pub type Raised = (i64, String, String, NaiveDateTime);

/// `NiftyOptionsMonitor`: marks every open options position and raises its exit — the desk's
/// half of OP9. It holds no gateway.
pub struct NiftyOptionsMonitor<S: PositionStore> {
    /// The store.
    pub store: S,
    /// The trading day.
    pub day: NaiveDate,
    /// The rules' config.
    pub options: OptionsConfig,
    /// The index's token.
    pub index_token: i64,
    refresh_every: Duration,
    reconcile_every: Duration,
    mark_every: Duration,
    /// The day's memory.
    pub state: MonitorState,
    /// Every verdict raised this session, in order.
    pub exits: Vec<Raised>,
    wall_time: WallTime,
}

/// `NiftyOptionsMonitor(gateway=None, store=, day=, refresh_seconds=, reconcile_seconds=,
/// mark_every_seconds=)`'s cadences.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cadence {
    /// `refresh_seconds` (30).
    pub refresh_seconds: i64,
    /// `reconcile_seconds` (60).
    pub reconcile_seconds: i64,
    /// `mark_every_seconds` (30).
    pub mark_every_seconds: i64,
}

impl Default for Cadence {
    fn default() -> Self {
        Self {
            refresh_seconds: 30,
            reconcile_seconds: 60,
            mark_every_seconds: 30,
        }
    }
}

/// The process's local wall-clock time (Python's naive `datetime.now()`).
#[must_use]
pub fn local_wall_time() -> WallTime {
    Arc::new(|| chrono::Local::now().naive_local().time())
}

impl<S: PositionStore> NiftyOptionsMonitor<S> {
    /// `NiftyOptionsMonitor(None, store=store, day=day)` with the default config and cadences.
    #[must_use]
    pub fn new(store: S, day: NaiveDate) -> Self {
        Self::with(store, day, Cadence::default(), local_wall_time())
    }

    /// With explicit cadences and the wall-clock seam.
    #[must_use]
    pub fn with(store: S, day: NaiveDate, cadence: Cadence, wall_time: WallTime) -> Self {
        Self {
            store,
            day,
            options: OptionsConfig::default(),
            index_token: NIFTY_50_TOKEN,
            refresh_every: Duration::seconds(cadence.refresh_seconds),
            reconcile_every: Duration::seconds(cadence.reconcile_seconds),
            mark_every: Duration::seconds(cadence.mark_every_seconds),
            state: MonitorState::default(),
            exits: Vec::new(),
            wall_time,
        }
    }

    /// `tokens`: the index, then every leg of every tracked position (sorted, distinct).
    #[must_use]
    pub fn tokens(&self) -> Vec<i64> {
        let mut legs: Vec<i64> = self
            .state
            .positions
            .iter()
            .flat_map(|(_, p)| p.tokens())
            .collect();
        legs.sort_unstable();
        legs.dedup();
        std::iter::once(self.index_token).chain(legs).collect()
    }

    /// `session_over(at)`.
    #[must_use]
    pub fn session_over(&self, at: NaiveDateTime) -> bool {
        at.time() >= session_close()
    }

    /// `bars()`: the day's minute bars, the store's where it has them, the ticks' otherwise.
    #[must_use]
    pub fn bars(&self) -> Vec<Bar> {
        let mut merged: BTreeMap<NaiveDateTime, Bar> = self
            .state
            .ticked
            .iter()
            .map(|(minute, m)| {
                (
                    *minute,
                    Bar {
                        ts: *minute,
                        open: m.open,
                        high: m.high,
                        low: m.low,
                        close: m.close,
                    },
                )
            })
            .collect();
        for (k, b) in &self.state.stored {
            merged.insert(*k, b.clone());
        }
        merged.into_values().collect()
    }

    /// `generate_targets(context)`: nothing, ever — the monitor decides exits, the executor
    /// sends them.
    #[must_use]
    pub const fn generate_targets(&self) -> Vec<PyDict> {
        Vec::new()
    }

    /// `on_start()`: load the day's positions (`_refresh(None)`).
    pub async fn start(&mut self) {
        self.refresh(None).await;
    }

    /// `on_tick(tick)`.
    ///
    /// # Errors
    /// `check`'s.
    pub async fn tick(&mut self, tick: &PyDict) -> Result<Vec<(i64, ExitVerdict)>, String> {
        let at = tick_time(tick, self.day, &self.wall_time);
        let token = tick.get("instrument_token");
        let is_index = match token {
            Some(PyValue::Int(i)) => *i == self.index_token,
            #[allow(clippy::cast_precision_loss)]
            Some(PyValue::Float(f)) => (*f - self.index_token as f64).abs() == 0.0,
            Some(PyValue::Bool(b)) => i64::from(*b) == self.index_token,
            _ => false,
        };
        if is_index {
            if let Some(price) = decimal_of(tick.get("last_price")) {
                self.index(at, price);
            }
        } else if let Some(PyValue::Int(t)) = token {
            self.leg(*t, tick, at);
        }
        self.check_at(at).await
    }

    /// `check(now)`: one evaluation pass — refresh, reconcile, then every open position's exit.
    /// Returns the verdicts raised by this pass.
    ///
    /// # Errors
    /// `exits.evaluate`'s `ValueError` (a malformed position), as Python raises it.
    pub async fn check_at(&mut self, now: NaiveDateTime) -> Result<Vec<(i64, ExitVerdict)>, String> {
        if due(self.state.refreshed_at, self.refresh_every, now) {
            self.refresh(Some(now)).await;
        }
        if due(self.state.reconciled_at, self.reconcile_every, now) {
            self.reconcile(now).await;
        }
        let index = IndexState {
            spot: self.state.spot,
            last_tick_at: self.state.last_index_tick,
            bars: self.bars(),
        };
        let mut raised = Vec::new();
        for (session_id, tracked) in self.state.positions.clone() {
            if tracked.exit_plan_id.is_some() {
                continue;
            }
            let mut marks = Marks::new();
            for lg in &tracked.position.legs {
                if let Some(m) = self.state.marks.get(&lg.instrument_token) {
                    marks.insert(lg.role, m.clone());
                }
            }
            self.record_mark(&tracked, &marks, now).await?;
            let verdict = evaluate(&tracked.position, &marks, &index, now, &self.options, false)
                .map_err(|e| format!("ValueError: {e}"))?;
            let Some(verdict) = verdict else {
                continue;
            };
            self.raise_one(session_id, &tracked, verdict, now, &mut raised)
                .await;
        }
        Ok(raised)
    }

    /// `token_lost(now)`: the Kite token was refused with positions open — `HARD_EXIT /
    /// FEED_LOST` for each, now (OP14).
    pub async fn token_lost_at(&mut self, now: NaiveDateTime) -> Vec<(i64, ExitVerdict)> {
        let mut raised = Vec::new();
        for (session_id, tracked) in self.state.positions.clone() {
            if tracked.exit_plan_id.is_some() {
                continue;
            }
            self.raise_one(session_id, &tracked, ExitVerdict::feed_lost(), now, &mut raised)
                .await;
        }
        raised
    }

    async fn raise_one(
        &mut self,
        session_id: i64,
        tracked: &TrackedPosition,
        verdict: ExitVerdict,
        now: NaiveDateTime,
        raised: &mut Vec<(i64, ExitVerdict)>,
    ) {
        let plan_id = self.raise(tracked, &verdict, now).await;
        let mut marked = tracked.clone();
        marked.exit_plan_id = Some(plan_id.unwrap_or_else(|| "RAISED".to_owned()));
        self.state.set(session_id, marked);
        self.exits.push((
            session_id,
            verdict.code.clone(),
            verdict.reason.clone(),
            now,
        ));
        raised.push((session_id, verdict));
    }

    fn index(&mut self, at: NaiveDateTime, price: Decimal) {
        self.state.spot = Some(price);
        self.state.last_index_tick = Some(at);
        let minute = floor_minute(at);
        match self.state.ticked.get_mut(&minute) {
            None => {
                self.state.ticked.insert(
                    minute,
                    Minute {
                        open: price,
                        high: price,
                        low: price,
                        close: price,
                    },
                );
            }
            Some(bar) => {
                bar.high = bar.high.max(price);
                bar.low = bar.low.min(price);
                bar.close = price;
            }
        }
    }

    fn leg(&mut self, token: i64, tick: &PyDict, at: NaiveDateTime) {
        let depth = match tick.get("depth") {
            Some(PyValue::Map(m)) => m.clone(),
            _ => PyDict::new(),
        };
        let bid = best(depth.get("buy"));
        let ask = best(depth.get("sell"));
        if bid.is_none() && ask.is_none() {
            return;
        }
        self.state.marks.insert(token, LegMark { bid, ask, at });
    }

    async fn refresh(&mut self, now: Option<NaiveDateTime>) {
        let fresh = match self.store.open_positions(self.day).await {
            Ok(f) => f,
            Err(e) => {
                emit(&format!(
                    "ERROR desk.options_monitor options monitor: could not read open positions: {e}"
                ));
                return;
            }
        };
        // Python rebuilds the dict from `fresh`: a position already carrying an exit plan keeps it
        // (never a second exit); a closed one drops out.
        let known = std::mem::take(&mut self.state.positions);
        let mut next = MonitorState {
            positions: Vec::new(),
            ..std::mem::take(&mut self.state)
        };
        for t in fresh {
            let before = known
                .iter()
                .find(|(s, _)| *s == t.session_id)
                .map(|(_, k)| k.exit_plan_id.clone());
            let kept = match before {
                Some(plan) if t.exit_plan_id.is_none() => TrackedPosition {
                    exit_plan_id: plan,
                    ..t
                },
                _ => t,
            };
            next.set(kept.session_id, kept);
        }
        next.refreshed_at = now;
        self.state = next;
    }

    async fn reconcile(&mut self, now: NaiveDateTime) {
        let stored = match self.store.index_minutes(self.day, now).await {
            Ok(s) => s,
            Err(e) => {
                emit(&format!(
                    "ERROR desk.options_monitor options monitor: could not read op_index_minute: {e}"
                ));
                return;
            }
        };
        self.state.stored = stored
            .into_iter()
            .map(|b| (floor_minute(b.ts), b))
            .collect();
        self.state.reconciled_at = Some(now);
    }

    async fn record_mark(
        &mut self,
        tracked: &TrackedPosition,
        marks: &Marks,
        now: NaiveDateTime,
    ) -> Result<(), String> {
        if let Some(last) = self.state.marked_at.get(&tracked.session_id)
            && now - *last < self.mark_every
        {
            return Ok(());
        }
        let current = mark(&tracked.position, marks, now, &self.options)
            .map_err(|e| format!("decimal.InvalidOperation: {e}"))?;
        let Some(value) = current.value_points else {
            return Ok(());
        };
        if let Err(e) = self.store.record_mark(tracked, value, now).await {
            emit(&format!(
                "ERROR desk.options_monitor options monitor: could not record a mark: {e}"
            ));
            return Ok(());
        }
        self.state.marked_at.insert(tracked.session_id, now);
        Ok(())
    }

    async fn raise(
        &mut self,
        tracked: &TrackedPosition,
        verdict: &ExitVerdict,
        now: NaiveDateTime,
    ) -> Option<String> {
        emit(&format!(
            "WARNING desk.options_monitor options exit: session {} {} {} ({}) value={} loss={}",
            tracked.session_id,
            sleeve_value(tracked.position.sleeve),
            verdict.code,
            verdict.reason,
            verdict.value_points.map_or_else(|| "None".to_owned(), py::dstr),
            verdict
                .marked_loss_inr
                .map_or_else(|| "None".to_owned(), py::dstr),
        ));
        match self.store.raise_exit(tracked, verdict, now).await {
            Ok(id) => id,
            Err(e) => {
                emit(&format!(
                    "ERROR desk.options_monitor options monitor: could not write the exit plan: {e}"
                ));
                None
            }
        }
    }
}

#[async_trait]
impl<S: PositionStore> super::clock::Monitor for NiftyOptionsMonitor<S> {
    fn tokens(&self) -> Vec<i64> {
        Self::tokens(self)
    }

    fn exits(&self) -> usize {
        self.exits.len()
    }

    async fn on_start(&mut self) {
        self.start().await;
    }

    async fn on_stop(&mut self) {}

    async fn on_tick(&mut self, tick: &PyDict) -> Result<(), String> {
        self.tick(tick).await.map(|_| ())
    }

    async fn check(&mut self, now: NaiveDateTime) -> Result<(), String> {
        self.check_at(now).await.map(|_| ())
    }

    fn session_over(&self, at: NaiveDateTime) -> bool {
        Self::session_over(self, at)
    }

    async fn token_lost(&mut self, now: NaiveDateTime) -> Result<(), String> {
        self.token_lost_at(now).await;
        Ok(())
    }
}

/// `build_monitor(enabled=, gateway=None, store=, day=)`: the strategy, or `None` — and with
/// the flag off, not even constructed (OP9 AC). There is no gateway argument: the monitor
/// never holds one.
#[must_use]
pub fn build_monitor<S: PositionStore>(
    enabled: bool,
    store: S,
    day: NaiveDate,
) -> Option<NiftyOptionsMonitor<S>> {
    if !enabled {
        emit("INFO desk.options_monitor BASKFY_OPTIONS_MONITOR_ENABLED is false; the options monitor is not built");
        return None;
    }
    Some(NiftyOptionsMonitor::new(store, day))
}

fn due(last: Option<NaiveDateTime>, every: Duration, now: NaiveDateTime) -> bool {
    last.is_none_or(|l| now - l >= every)
}

fn floor_minute(at: NaiveDateTime) -> NaiveDateTime {
    use chrono::Timelike as _;
    at.with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .unwrap_or(at)
}

const fn sleeve_value(sleeve: baskfy_core::options::gating::Sleeve) -> &'static str {
    super::core::sleeve_str(sleeve)
}

/// `_naive(at)`: naive IST.
#[must_use]
pub fn naive(at: &PyDateTime) -> NaiveDateTime {
    match at {
        PyDateTime::Naive(n) => *n,
        PyDateTime::Aware(a) => ist(a),
    }
}

/// `_best(levels)`: the top of one side of Kite's depth; a zero price is an empty level.
fn best(levels: Option<&PyValue>) -> Option<Decimal> {
    let Some(PyValue::List(items)) = levels else {
        return None;
    };
    let top = items.first()?;
    let price = match top {
        PyValue::Map(m) => decimal_of(m.get("price")),
        _ => None,
    }?;
    (price > Decimal::ZERO).then_some(price)
}

/// `_decimal(value)`: `Decimal(str(value))`, `None` for `None` or what `Decimal` refuses.
#[must_use]
pub fn decimal_of(value: Option<&PyValue>) -> Option<Decimal> {
    match value? {
        PyValue::None | PyValue::Bool(_) => None,
        PyValue::Int(i) => Some(Decimal::from(*i)),
        PyValue::Float(f) => py::dec_from_float(*f),
        PyValue::Str(s) => py::dec_from_str(s),
        other => py::dec_from_str(&other.py_str()),
    }
}

/// `_tick_time(tick, day)`: the tick's exchange timestamp, or its last-trade time, or its
/// `timestamp`, or the day at the process's wall-clock time.
#[must_use]
pub fn tick_time(tick: &PyDict, day: NaiveDate, wall_time: &WallTime) -> NaiveDateTime {
    for key in ["exchange_timestamp", "last_trade_time", "timestamp"] {
        match tick.get(key) {
            Some(PyValue::DateTime(at)) => return naive(at),
            Some(PyValue::Str(s)) => {
                if let Some((n, off)) = py::parse_iso(s) {
                    return match off {
                        Some(o) => o
                            .from_local_datetime(&n)
                            .single()
                            .map_or(n, |a| ist(&a)),
                        None => n,
                    };
                }
            }
            _ => {}
        }
    }
    day.and_time(wall_time())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    #[test]
    fn the_monitor_has_no_gateway_field_and_targets_nothing() {
        struct Nothing;
        #[async_trait]
        impl PositionStore for Nothing {
            async fn open_positions(&mut self, _: NaiveDate) -> Result<Vec<TrackedPosition>, String> {
                Ok(Vec::new())
            }
            async fn raise_exit(
                &mut self,
                _: &TrackedPosition,
                _: &ExitVerdict,
                _: NaiveDateTime,
            ) -> Result<Option<String>, String> {
                Ok(None)
            }
            async fn record_mark(
                &mut self,
                _: &TrackedPosition,
                _: Decimal,
                _: NaiveDateTime,
            ) -> Result<(), String> {
                Ok(())
            }
            async fn index_minutes(
                &mut self,
                _: NaiveDate,
                _: NaiveDateTime,
            ) -> Result<Vec<Bar>, String> {
                Ok(Vec::new())
            }
        }
        let m = NiftyOptionsMonitor::new(Nothing, NaiveDate::from_ymd_opt(2026, 10, 27).unwrap());
        assert!(m.generate_targets().is_empty());
        assert_eq!((NAME, PRODUCTS), ("nifty_options_monitor", ["MIS"]));
    }
}
