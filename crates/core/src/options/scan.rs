//! Port of `baskfy_core.options.scan`: each sleeve's scan state and candidates at one minute (`04` §10).

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta};
use rust_decimal::Decimal;
use serde::ser::SerializeMap;
use serde::{Deserialize, Serialize, Serializer};

use crate::decimal::Rounding;
use crate::error::CoreResult;
use crate::options::bars::{Bar, closed, ist, minute_after};
use crate::options::calendar::{
    Contract, expiry_for_o1_o3, expiry_for_o2, lot_size_for, next_session, role, tick_size_for,
};
use crate::options::chain::{OptionQuote, as_f64};
use crate::options::condor::{self, CondorBuild};
use crate::options::config::{CondorConfig, OptionsCeilings, OptionsConfig, Sleeve};
use crate::options::directional::{self, LongBuild, Range};
use crate::options::expiry_setups::{self, SetupClock, SpreadBuild};
use crate::options::num::{self, at_ist, hhmm, py_str};
use crate::options::session::SessionState;
use crate::options::structures::{
    Candidate, CandidateJson, ChainView, Direction, Rejection, SleeveBook, ViewInputs, chain_view,
    to_json,
};

/// `04` §10's scan states.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ScanState {
    /// Not this sleeve's day.
    NotToday,
    /// Watching.
    Observing,
    /// The gate passed with a viable candidate.
    WouldTrade,
    /// The gate or the candidate refused.
    WouldSkip,
    /// Another sleeve holds the slot.
    SlotTaken,
    /// A plan is live or taken.
    Planned,
    /// The day is over.
    Done,
    /// The range is still building.
    BuildingRange,
    /// The day filters refused.
    DaySkipped,
    /// Waiting for a trigger.
    Armed,
    /// Triggered.
    Triggered,
    /// The window closed with no trigger.
    WindowClosed,
    /// Paused.
    Paused,
}

impl ScanState {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            ScanState::NotToday => "NOT_TODAY",
            ScanState::Observing => "OBSERVING",
            ScanState::WouldTrade => "WOULD_TRADE",
            ScanState::WouldSkip => "WOULD_SKIP",
            ScanState::SlotTaken => "SLOT_TAKEN",
            ScanState::Planned => "PLANNED",
            ScanState::Done => "DONE",
            ScanState::BuildingRange => "BUILDING_RANGE",
            ScanState::DaySkipped => "DAY_SKIPPED",
            ScanState::Armed => "ARMED",
            ScanState::Triggered => "TRIGGERED",
            ScanState::WindowClosed => "WINDOW_CLOSED",
            ScanState::Paused => "PAUSED",
        }
    }
}

/// The order `scan_all` evaluates: O3-B before O3-A.
pub const SCAN_ORDER: [Sleeve; 5] = [
    Sleeve::O1m,
    Sleeve::O1w,
    Sleeve::O2,
    Sleeve::O3b,
    Sleeve::O3a,
];
/// No previous NIFTY 50 close.
pub const NO_PREV_CLOSE: &str = "NO_PREV_CLOSE";
/// O3-B holds the day's one O3 trade.
pub const O3B_HOLDS: &str = "O3B_HOLDS";
/// The setup is switched off.
pub const SETUP_DISABLED: &str = "SETUP_DISABLED";

const LIVE_SESSION: [SessionState; 3] = [
    SessionState::Planned,
    SessionState::Confirmed,
    SessionState::Open,
];
const OVER_SESSION: [SessionState; 3] = [
    SessionState::Closed,
    SessionState::Lapsed,
    SessionState::Skipped,
];

/// One minute of the chain snapshot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Snapshot {
    /// The minute.
    pub ts: DateTime<FixedOffset>,
    /// The spot quoted with it.
    pub spot: Option<Decimal>,
    /// Every contract's quote.
    pub quotes: Vec<OptionQuote>,
}

/// The market facts of `trade_date`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MarketDay {
    /// The trade date.
    pub trade_date: NaiveDate,
    /// An NSE trading day.
    pub trading_day: bool,
    /// The NFO master.
    pub contracts: Vec<Contract>,
    /// NIFTY 50's one-minute bars of the day.
    pub bars: Vec<Bar>,
    /// NIFTY 50's daily closes through the previous session, oldest first.
    pub daily_closes: Vec<Decimal>,
    /// India VIX's previous close.
    pub vix_prev_close: Option<Decimal>,
}

/// One sleeve's book and today's session.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct SleeveContext {
    /// The book.
    pub book: SleeveBook,
    /// Paused.
    pub paused: bool,
    /// Today's session state.
    pub session_state: Option<SessionState>,
}

/// The user's side of the day.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct DayContext {
    /// Event days.
    pub event_days: Vec<NaiveDate>,
    /// The expiry-day slot's holder.
    pub slot_holder: Option<Sleeve>,
    /// Each sleeve's context.
    pub sleeves: BTreeMap<Sleeve, SleeveContext>,
}

impl DayContext {
    /// The sleeve's context, or the default one.
    pub fn of(&self, sleeve: Sleeve) -> SleeveContext {
        self.sleeves.get(&sleeve).cloned().unwrap_or_default()
    }

    fn is_event(&self, day: NaiveDate) -> bool {
        self.event_days.contains(&day)
    }
}

/// A minute asked for; `None` is the latest.
pub type MinuteKey = Option<DateTime<FixedOffset>>;

/// The snapshots the caller loaded, keyed by the minute asked for; records every request.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SnapshotBook {
    snapshots: BTreeMap<MinuteKey, Option<Snapshot>>,
    /// Every minute asked for.
    pub asked: BTreeSet<MinuteKey>,
}

impl SnapshotBook {
    /// A book over `snapshots`.
    pub fn new(snapshots: BTreeMap<MinuteKey, Option<Snapshot>>) -> Self {
        Self {
            snapshots,
            asked: BTreeSet::new(),
        }
    }

    /// The snapshot for `at`, recording the request.
    pub fn get(&mut self, at: MinuteKey) -> Option<Snapshot> {
        self.asked.insert(at);
        self.snapshots.get(&at).cloned().flatten()
    }
}

/// A JSON value in a row's `numbers`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum Value {
    /// `null`.
    Null,
    /// A bool.
    Bool(bool),
    /// An int.
    Int(i64),
    /// A string.
    Str(String),
    /// A list of objects.
    List(Vec<Numbers>),
}

impl From<Option<String>> for Value {
    fn from(v: Option<String>) -> Self {
        v.map_or(Value::Null, Value::Str)
    }
}

impl From<Option<bool>> for Value {
    fn from(v: Option<bool>) -> Self {
        v.map_or(Value::Null, Value::Bool)
    }
}

impl From<String> for Value {
    fn from(v: String) -> Self {
        Value::Str(v)
    }
}

impl From<&str> for Value {
    fn from(v: &str) -> Self {
        Value::Str(v.to_owned())
    }
}

impl From<i64> for Value {
    fn from(v: i64) -> Self {
        Value::Int(v)
    }
}

impl From<bool> for Value {
    fn from(v: bool) -> Self {
        Value::Bool(v)
    }
}

/// An insertion-ordered JSON object (Python's `dict[str, object]`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Numbers(pub Vec<(String, Value)>);

impl Numbers {
    /// `d[key] = value`: replaced in place, or appended.
    pub fn set(&mut self, key: &str, value: impl Into<Value>) {
        let value = value.into();
        match self.0.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1 = value,
            None => self.0.push((key.to_owned(), value)),
        }
    }

    /// `d |= other`.
    pub fn extend(&mut self, other: Numbers) {
        for (k, v) in other.0 {
            self.set(&k, v);
        }
    }

    /// `d.get(key)`.
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    /// Builds from pairs.
    pub fn of(pairs: Vec<(&str, Value)>) -> Self {
        let mut out = Numbers::default();
        for (k, v) in pairs {
            out.set(k, v);
        }
        out
    }
}

impl Serialize for Numbers {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(Some(self.0.len()))?;
        for (k, v) in &self.0 {
            map.serialize_entry(k, v)?;
        }
        map.end()
    }
}

/// One `op_scan` row.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ScanResult {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// The minute.
    pub ts: DateTime<FixedOffset>,
    /// The state.
    pub state: ScanState,
    /// Every reason.
    pub reasons: Vec<String>,
    /// The numbers.
    pub numbers: Numbers,
    /// The priced candidates.
    pub candidates: Vec<Candidate>,
    /// The snapshot's minute.
    pub as_of_minute: Option<DateTime<FixedOffset>>,
    /// `04` §2.5's stale flag.
    pub stale: bool,
}

impl ScanResult {
    /// The candidates as `op_scan.candidates` stores them.
    pub fn candidates_json(&self) -> CoreResult<Vec<CandidateJson>> {
        self.candidates.iter().map(to_json).collect()
    }
}

/// `str(value.quantize(places, ROUND_HALF_UP))`, `None` passing through.
pub(crate) fn q_up(value: Option<Decimal>, places: i64) -> CoreResult<Value> {
    num::qstr(value, places, Rounding::HalfUp).map(Value::from)
}

/// `value.strftime("%H:%M")`.
pub(crate) fn t(value: Option<NaiveTime>) -> Value {
    value.map_or(Value::Null, |v| Value::Str(hhmm(v)))
}

/// The row's `ts`: `now` floored to the minute, aware IST.
pub fn scan_minute(now: &DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    let local = ist(now);
    let floored = local.date().and_time(
        NaiveTime::from_hms_opt(
            chrono::Timelike::hour(&local),
            chrono::Timelike::minute(&local),
            0,
        )
        .unwrap_or(NaiveTime::MIN),
    );
    at_ist(floored.date(), floored.time())
}

fn last_close_by(bars: &[Bar], at: &DateTime<FixedOffset>) -> Option<Decimal> {
    closed(bars, &(*at + TimeDelta::minutes(1)))
        .last()
        .map(|b| b.close)
}

/// The snapshot's chain for `expiry`, priced at the snapshot's minute.
pub fn priced_view(
    snapshot: Option<&Snapshot>,
    expiry: Option<NaiveDate>,
    market: &MarketDay,
    options: &OptionsConfig,
) -> CoreResult<Option<ChainView>> {
    let (Some(snapshot), Some(expiry)) = (snapshot, expiry) else {
        return Ok(None);
    };
    let underlying = options.calendar.underlying.as_str();
    let tick = tick_size_for(&market.contracts, expiry, underlying);
    let spot = match snapshot.spot {
        Some(s) if !s.is_zero() => Some(s),
        _ => last_close_by(&market.bars, &snapshot.ts),
    };
    let (Some(tick), Some(spot)) = (tick, spot) else {
        return Ok(None);
    };
    if spot <= Decimal::ZERO {
        return Ok(None);
    }
    let listed: Vec<Decimal> = market
        .contracts
        .iter()
        .filter(|c| c.expiry == expiry && c.underlying == underlying)
        .map(|c| c.strike)
        .collect();
    chain_view(
        &snapshot.quotes,
        &ViewInputs {
            expiry,
            spot,
            now: snapshot.ts,
            tick,
            config: &options.chain,
            settle: options.calendar.market_close,
            listed_strikes: if listed.is_empty() {
                None
            } else {
                Some(&listed)
            },
        },
    )
}

fn stale(
    snapshot: Option<&Snapshot>,
    reference: &DateTime<FixedOffset>,
    options: &OptionsConfig,
) -> bool {
    let Some(snapshot) = snapshot else {
        return true;
    };
    let gap = (ist(&snapshot.ts) - ist(reference))
        .num_microseconds()
        .unwrap_or(i64::MAX);
    (as_f64(gap) / 1_000_000.0).abs() > as_f64(options.chain.stale_scan_seconds)
}

fn slot_free(sleeve: Sleeve, context: &DayContext) -> bool {
    context.slot_holder.is_none_or(|h| h == sleeve)
}

fn condor_config(sleeve: Sleeve, options: &OptionsConfig) -> &CondorConfig {
    if sleeve == Sleeve::O1m {
        &options.condor_monthly
    } else {
        &options.condor_weekly
    }
}

#[derive(Debug, Clone)]
struct Row {
    state: ScanState,
    reasons: Vec<String>,
    numbers: Numbers,
    candidates: Vec<Candidate>,
    snapshot: Option<Snapshot>,
    reference: Option<DateTime<FixedOffset>>,
    priced: bool,
}

impl Row {
    fn new(state: ScanState) -> Self {
        Row {
            state,
            reasons: Vec::new(),
            numbers: Numbers::default(),
            candidates: Vec::new(),
            snapshot: None,
            reference: None,
            priced: false,
        }
    }

    fn with_reasons(state: ScanState, reasons: Vec<String>) -> Self {
        Row {
            reasons,
            ..Row::new(state)
        }
    }
}

fn finish(
    sleeve: Sleeve,
    row: Row,
    market: &MarketDay,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
) -> ScanResult {
    let is_stale = row.priced
        && stale(
            row.snapshot.as_ref(),
            &row.reference.unwrap_or(*now),
            options,
        );
    ScanResult {
        sleeve,
        trade_date: market.trade_date,
        ts: scan_minute(now),
        state: row.state,
        reasons: row.reasons,
        numbers: row.numbers,
        candidates: row.candidates,
        as_of_minute: row.snapshot.as_ref().map(|s| s.ts),
        stale: is_stale,
    }
}

fn not_today(
    sleeve: Sleeve,
    market: &MarketDay,
    context: &DayContext,
    reason: &str,
) -> CoreResult<Row> {
    let mut row = Row::with_reasons(ScanState::NotToday, vec![reason.to_owned()]);
    if sleeve != Sleeve::O2 {
        let upcoming = next_session(
            market.trade_date,
            sleeve,
            &market.contracts,
            &context.event_days,
            "NIFTY",
        )?;
        row.numbers
            .set("next_date", upcoming.map(|d| d.to_string()));
    }
    Ok(row)
}

fn paused(mut row: Row, is_paused: bool) -> Row {
    if is_paused && row.state != ScanState::NotToday {
        row.numbers.set("state_before_pause", row.state.value());
        row.state = ScanState::Paused;
        let code = Rejection::RejectedPaused.value();
        if !row.reasons.iter().any(|r| r == code) {
            row.reasons.push(code.to_owned());
        }
    }
    row
}

fn candidate_state(
    candidate: &Candidate,
    ok: ScanState,
    skip: ScanState,
) -> (ScanState, Vec<String>) {
    match candidate.rejection {
        None => (ok, Vec::new()),
        Some(Rejection::RejectedSlotTaken) => (
            ScanState::SlotTaken,
            vec![Rejection::RejectedSlotTaken.value().to_owned()],
        ),
        Some(code) => (skip, vec![code.value().to_owned()]),
    }
}

/// The inputs every per-sleeve scan shares.
struct Scan<'a> {
    market: &'a MarketDay,
    context: &'a DayContext,
    now: &'a DateTime<FixedOffset>,
    options: &'a OptionsConfig,
    ceilings: &'a OptionsCeilings,
}

impl Scan<'_> {
    fn role_of(&self, sleeve: Sleeve) -> CoreResult<crate::options::calendar::DayRole> {
        role(
            self.market.trade_date,
            sleeve,
            &self.market.contracts,
            &self.context.event_days,
            self.market.trading_day,
            "NIFTY",
        )
    }

    fn clock(&self) -> SetupClock {
        SetupClock {
            now: *self.now,
            event_day: self.context.is_event(self.market.trade_date),
            session_open: self.options.calendar.market_open,
            grace_seconds: self.options.chain.stale_scan_seconds,
        }
    }

    fn o1(&self, sleeve: Sleeve, snapshots: &mut SnapshotBook) -> CoreResult<Row> {
        let (market, context, options) = (self.market, self.context, self.options);
        let cfg = condor_config(sleeve, options);
        let mine = context.of(sleeve);
        let verdict_role = self.role_of(sleeve)?;
        if !verdict_role.trades {
            return not_today(sleeve, market, context, verdict_role.reason.value());
        }
        let Some(prev_close) = market.daily_closes.last().copied() else {
            let state = if ist(self.now).time() < cfg.plan_time {
                ScanState::Observing
            } else {
                ScanState::WouldSkip
            };
            return Ok(paused(
                Row::with_reasons(state, vec![NO_PREV_CLOSE.to_owned()]),
                mine.paused,
            ));
        };
        let verdict = condor::observe(
            &market.bars,
            prev_close,
            self.now,
            context.is_event(market.trade_date),
            cfg,
            options.chain.stale_scan_seconds,
        )?;
        let n = &verdict.numbers;
        let mut row = Row::new(ScanState::Observing);
        row.numbers = Numbers::of(vec![
            ("variant", cfg.variant.value().into()),
            ("prev_close", q_up(Some(n.prev_close), 2)?),
            ("bars", n.bars.into()),
            ("open_0915", q_up(n.open_0915, 2)?),
            ("gap_pct", q_up(n.gap_pct, 4)?),
            ("gap_max_pct", py_str(cfg.gap_max_pct).into()),
            ("obs_high", q_up(n.obs_high, 2)?),
            ("obs_low", q_up(n.obs_low, 2)?),
            ("range_pct", q_up(n.range_pct, 4)?),
            ("range_max_pct", py_str(cfg.range_max_pct).into()),
            ("or_high", q_up(n.or_high, 2)?),
            ("or_low", q_up(n.or_low, 2)?),
            ("last_close", q_up(n.last_close, 2)?),
            ("contained", n.contained.into()),
            ("er", q_up(n.er, 4)?),
            ("er_max", py_str(cfg.er_max).into()),
            ("min_bars", cfg.min_bars.into()),
            ("plan_time", t(Some(cfg.plan_time))),
            ("entry_window_end", t(Some(cfg.entry_window_end))),
        ]);
        row.reasons = verdict
            .reasons
            .iter()
            .map(|r| r.value().to_owned())
            .collect();
        let clock = ist(self.now).time();
        if !verdict.final_ || clock < cfg.plan_time {
            return Ok(paused(row, mine.paused));
        }
        if verdict.reasons.is_empty() {
            let decision = at_ist(market.trade_date, cfg.plan_time);
            let snapshot = snapshots.get(Some(decision));
            let expiry = expiry_for_o1_o3(market.trade_date);
            let view = priced_view(snapshot.as_ref(), Some(expiry), market, options)?;
            let candidate = condor::build(
                view.as_ref(),
                &CondorBuild {
                    or_high: n.or_high.unwrap_or(Decimal::ZERO),
                    or_low: n.or_low.unwrap_or(Decimal::ZERO),
                    lot_size: lot_size_for(&market.contracts, expiry, &options.calendar.underlying),
                    book: mine.book.clone(),
                    slot_free: slot_free(sleeve, context),
                    paused: mine.paused,
                    expiry,
                },
                cfg,
                options,
                self.ceilings,
            )?;
            let (state, reasons) =
                candidate_state(&candidate, ScanState::WouldTrade, ScanState::WouldSkip);
            row.candidates = vec![candidate];
            row.snapshot = snapshot;
            row.reference = Some(decision);
            row.priced = true;
            row.state = state;
            row.reasons = reasons;
        } else {
            row.state = ScanState::WouldSkip;
        }
        row.numbers.set("verdict", row.state.value());
        if mine
            .session_state
            .is_some_and(|s| LIVE_SESSION.contains(&s))
        {
            row.state = ScanState::Planned;
        } else if mine
            .session_state
            .is_some_and(|s| OVER_SESSION.contains(&s))
            || clock >= cfg.entry_window_end
        {
            row.state = ScanState::Done;
        }
        Ok(paused(row, mine.paused))
    }

    fn o2(&self, snapshots: &mut SnapshotBook) -> CoreResult<Row> {
        let (market, context, options) = (self.market, self.context, self.options);
        let cfg = &options.directional;
        let mine = context.of(Sleeve::O2);
        let verdict_role = self.role_of(Sleeve::O2)?;
        if !verdict_role.trades {
            return not_today(Sleeve::O2, market, context, verdict_role.reason.value());
        }
        let filters = directional::day_filters(
            &market.bars,
            &market.daily_closes,
            market.vix_prev_close,
            self.now,
            context.is_event(market.trade_date),
            cfg,
            options.chain.stale_scan_seconds,
        )?;
        let expiry = expiry_for_o2(
            market.trade_date,
            &market.contracts,
            &options.calendar.underlying,
        );
        let mut row = Row::new(ScanState::BuildingRange);
        row.numbers = day_numbers_up(&filters, cfg, expiry)?;
        row.reasons = filters
            .reasons
            .iter()
            .map(|r| r.value().to_owned())
            .collect();
        if !filters.final_ {
            return Ok(paused(row, mine.paused));
        }
        if !filters.reasons.is_empty() {
            row.state = ScanState::DaySkipped;
            return Ok(paused(row, mine.paused));
        }
        let direction = filters.trend.unwrap_or(Direction::Up);
        let trig = directional::find_trigger(
            &market.bars,
            Range {
                or_high: filters.or_high.unwrap_or(Decimal::ZERO),
                or_low: filters.or_low.unwrap_or(Decimal::ZERO),
                direction,
            },
            self.now,
            cfg,
            options.calendar.market_open,
            options.chain.stale_scan_seconds,
        )?;
        let level = match direction {
            Direction::Up => trig.level_up,
            Direction::Down => trig.level_down,
        };
        let breaks = trig
            .counter_breaks
            .iter()
            .map(|b| {
                Ok(Numbers::of(vec![
                    ("close_time", t(Some(b.close_time))),
                    ("close", q_up(Some(b.close), 2)?),
                    ("direction", b.direction.value().into()),
                ]))
            })
            .collect::<CoreResult<Vec<_>>>()?;
        row.numbers.extend(Numbers::of(vec![
            ("direction", direction.value().into()),
            ("trigger_level", q_up(Some(level), 2)?),
            ("level_up", q_up(Some(trig.level_up), 2)?),
            ("level_down", q_up(Some(trig.level_down), 2)?),
            ("last_close", q_up(trig.last_close, 2)?),
            ("counter_trend_breaks", Value::List(breaks)),
        ]));
        if let Some(trigger) = &trig.trigger {
            let decision = at_ist(market.trade_date, minute_after(trigger.close_time));
            row.state = ScanState::Triggered;
            row.numbers.extend(Numbers::of(vec![
                ("trigger_time", t(Some(trigger.close_time))),
                ("trigger_close", q_up(Some(trigger.close), 2)?),
            ]));
            row.snapshot = snapshots.get(Some(decision));
            row.reference = Some(decision);
        } else if trig.window_closed {
            row.state = ScanState::WindowClosed;
            return Ok(paused(row, mine.paused));
        } else {
            row.state = ScanState::Armed;
            if let Some(last) = trig.last_close {
                let distance = match direction {
                    Direction::Up => num::sub(level, last)?,
                    Direction::Down => num::sub(last, level)?,
                };
                let distance_pct = num::mul(num::div(distance, last)?, Decimal::ONE_HUNDRED)?;
                row.numbers.extend(Numbers::of(vec![
                    ("distance_points", q_up(Some(distance), 2)?),
                    ("distance_pct", q_up(Some(distance_pct), 4)?),
                ]));
            }
            row.snapshot = snapshots.get(None);
            row.reference = Some(*self.now);
        }
        let view = priced_view(row.snapshot.as_ref(), expiry, market, options)?;
        let candidate = directional::build(
            view.as_ref(),
            direction,
            &LongBuild {
                lot_size: expiry
                    .and_then(|e| lot_size_for(&market.contracts, e, &options.calendar.underlying)),
                book: mine.book.clone(),
                expiry: expiry.unwrap_or(market.trade_date),
                paused: mine.paused,
            },
            cfg,
            options,
            self.ceilings,
        )?;
        if let Some(code) = candidate.rejection {
            row.reasons.push(code.value().to_owned());
        }
        row.candidates = vec![candidate];
        row.priced = true;
        Ok(paused(row, mine.paused))
    }

    fn o3_role(&self, sleeve: Sleeve) -> CoreResult<Option<Row>> {
        let verdict_role = self.role_of(sleeve)?;
        if !verdict_role.trades {
            return not_today(
                sleeve,
                self.market,
                self.context,
                verdict_role.reason.value(),
            )
            .map(Some);
        }
        let cfg = &self.options.expiry_setups;
        let enabled = if sleeve == Sleeve::O3a {
            cfg.o3a_enabled
        } else {
            cfg.o3b_enabled
        };
        if !enabled {
            return Ok(Some(Row::with_reasons(
                ScanState::NotToday,
                vec![SETUP_DISABLED.to_owned()],
            )));
        }
        Ok(None)
    }

    fn spread(
        &self,
        snapshot: Option<&Snapshot>,
        direction: Direction,
        mine: &SleeveContext,
        slot_free: bool,
    ) -> CoreResult<Candidate> {
        let (market, options) = (self.market, self.options);
        let expiry = expiry_for_o1_o3(market.trade_date);
        let view = priced_view(snapshot, Some(expiry), market, options)?;
        expiry_setups::build(
            view.as_ref(),
            direction,
            &SpreadBuild {
                lot_size: lot_size_for(&market.contracts, expiry, &options.calendar.underlying),
                book: mine.book.clone(),
                expiry,
                slot_free,
                paused: mine.paused,
            },
            &options.expiry_setups,
            options,
            self.ceilings,
        )
    }

    fn o3b(&self, snapshots: &mut SnapshotBook) -> CoreResult<Row> {
        let (market, context, options) = (self.market, self.context, self.options);
        let cfg = &options.expiry_setups;
        let mine = context.of(Sleeve::O3b);
        if let Some(early) = self.o3_role(Sleeve::O3b)? {
            return Ok(early);
        }
        let Some(prev_close) = market.daily_closes.last().copied() else {
            return Ok(paused(
                Row::with_reasons(ScanState::BuildingRange, vec![NO_PREV_CLOSE.to_owned()]),
                mine.paused,
            ));
        };
        let gh = expiry_setups::gap_hold(&market.bars, prev_close, &self.clock(), cfg)?;
        let mut row = Row::new(ScanState::BuildingRange);
        row.numbers = Numbers::of(vec![
            ("prev_close", q_up(Some(prev_close), 2)?),
            ("open_0915", q_up(gh.open_0915, 2)?),
            ("gap_points", q_up(gh.gap_points, 2)?),
            ("gap_pct", q_up(gh.gap_pct, 4)?),
            ("gap_min_pct", py_str(cfg.o3b_gap_min_pct).into()),
            ("gap_max_pct", py_str(cfg.o3b_gap_max_pct).into()),
            ("half_gap", q_up(gh.half_gap, 2)?),
            (
                "direction",
                gh.direction.map(|d| d.value().to_owned()).into(),
            ),
            ("held_so_far", gh.held_so_far.into()),
            ("bars", gh.bars.into()),
            ("hold_end", t(Some(cfg.o3b_hold_end))),
            ("plan_time", t(Some(cfg.o3b_plan_time))),
            ("window_end", t(Some(cfg.o3b_window_end))),
        ]);
        row.reasons = gh.reasons.iter().map(|r| r.value().to_owned()).collect();
        let free = slot_free(Sleeve::O3b, context);
        if gh.final_ && !gh.reasons.is_empty() {
            row.state = ScanState::DaySkipped;
        } else if let (true, Some(direction)) = (gh.triggered, gh.direction) {
            let decision = at_ist(market.trade_date, cfg.o3b_plan_time);
            let snapshot = snapshots.get(Some(decision));
            let candidate = self.spread(snapshot.as_ref(), direction, &mine, free)?;
            let (state, extra) =
                candidate_state(&candidate, ScanState::Triggered, ScanState::Triggered);
            row.candidates = vec![candidate];
            row.snapshot = snapshot;
            row.reference = Some(decision);
            row.priced = true;
            row.state = state;
            row.reasons.extend(extra);
        } else if gh.window_closed {
            row.state = ScanState::WindowClosed;
        } else if let (Some(direction), true) = (gh.direction, gh.reasons.is_empty()) {
            let snapshot = snapshots.get(None);
            row.candidates = vec![self.spread(snapshot.as_ref(), direction, &mine, free)?];
            row.snapshot = snapshot;
            row.reference = Some(*self.now);
            row.priced = true;
        }
        Ok(paused(row, mine.paused))
    }

    fn o3b_holds(&self, o3b: Option<&Row>) -> bool {
        let Some(o3b) = o3b else {
            return false;
        };
        if o3b.state != ScanState::Triggered || !o3b.candidates.iter().any(Candidate::viable) {
            return false;
        }
        !matches!(
            self.context.of(Sleeve::O3b).session_state,
            Some(SessionState::Lapsed | SessionState::Skipped)
        )
    }

    fn o3a(&self, snapshots: &mut SnapshotBook, o3b: Option<&Row>) -> CoreResult<Row> {
        let (market, context, options) = (self.market, self.context, self.options);
        let cfg = &options.expiry_setups;
        let mine = context.of(Sleeve::O3a);
        if let Some(early) = self.o3_role(Sleeve::O3a)? {
            return Ok(early);
        }
        let Some(prev_close) = market.daily_closes.last().copied() else {
            return Ok(paused(
                Row::with_reasons(ScanState::BuildingRange, vec![NO_PREV_CLOSE.to_owned()]),
                mine.paused,
            ));
        };
        let rb = expiry_setups::range_break(&market.bars, prev_close, &self.clock(), cfg)?;
        let low_er = rb
            .low_er_breaks
            .iter()
            .map(|(time, close, er)| {
                Ok(Numbers::of(vec![
                    ("close_time", t(Some(*time))),
                    ("close", q_up(Some(*close), 2)?),
                    ("er", q_up(Some(*er), 4)?),
                ]))
            })
            .collect::<CoreResult<Vec<_>>>()?;
        let mut row = Row::new(ScanState::BuildingRange);
        row.numbers = Numbers::of(vec![
            ("prev_close", q_up(Some(prev_close), 2)?),
            ("range_high", q_up(rb.range_high, 2)?),
            ("range_low", q_up(rb.range_low, 2)?),
            ("range_pct", q_up(rb.range_pct, 4)?),
            ("range_max_pct", py_str(cfg.o3a_range_max_pct).into()),
            ("bars", rb.bars.into()),
            ("level_up", q_up(rb.level_up, 2)?),
            ("level_down", q_up(rb.level_down, 2)?),
            ("er_min", py_str(cfg.o3a_er_min).into()),
            ("last_close", q_up(rb.last_close, 2)?),
            ("window_start", t(Some(cfg.o3a_window_start))),
            ("window_end", t(Some(cfg.o3a_window_end))),
            ("low_er_breaks", Value::List(low_er)),
        ]);
        row.reasons = rb.reasons.iter().map(|r| r.value().to_owned()).collect();
        let holds = self.o3b_holds(o3b);
        let free = slot_free(Sleeve::O3a, context) && !holds;
        if !rb.final_ {
            return Ok(paused(row, mine.paused));
        }
        if !rb.reasons.is_empty() {
            row.state = ScanState::DaySkipped;
        } else if let (Some(direction), Some(trigger_time)) = (rb.direction, rb.trigger_time) {
            let decision = at_ist(market.trade_date, minute_after(trigger_time));
            row.numbers.extend(Numbers::of(vec![
                ("trigger_time", t(Some(trigger_time))),
                ("trigger_close", q_up(rb.trigger_close, 2)?),
                ("trigger_er", q_up(rb.trigger_er, 4)?),
                ("direction", direction.value().into()),
            ]));
            let snapshot = snapshots.get(Some(decision));
            let candidate = self.spread(snapshot.as_ref(), direction, &mine, free)?;
            let (state, extra) =
                candidate_state(&candidate, ScanState::Triggered, ScanState::Triggered);
            row.candidates = vec![candidate];
            row.snapshot = snapshot;
            row.reference = Some(decision);
            row.priced = true;
            row.state = state;
            row.reasons.extend(extra);
            if holds {
                row.reasons.push(O3B_HOLDS.to_owned());
            }
        } else if rb.window_closed {
            row.state = ScanState::WindowClosed;
        } else {
            row.state = if free {
                ScanState::Armed
            } else {
                ScanState::SlotTaken
            };
            if holds {
                row.reasons.push(O3B_HOLDS.to_owned());
            }
            let snapshot = snapshots.get(None);
            row.candidates = vec![
                self.spread(snapshot.as_ref(), Direction::Up, &mine, free)?,
                self.spread(snapshot.as_ref(), Direction::Down, &mine, free)?,
            ];
            row.snapshot = snapshot;
            row.reference = Some(*self.now);
            row.priced = true;
        }
        Ok(paused(row, mine.paused))
    }
}

fn day_numbers_up(
    filters: &directional::DayFilters,
    cfg: &crate::options::config::DirectionalConfig,
    expiry: Option<NaiveDate>,
) -> CoreResult<Numbers> {
    Ok(Numbers::of(vec![
        ("trend", filters.trend.map(|d| d.value().to_owned()).into()),
        ("ema", q_up(filters.ema, 2)?),
        ("trend_ema_days", cfg.trend_ema_days.into()),
        ("index", "NIFTY 50".into()),
        ("prev_close", q_up(filters.prev_close, 2)?),
        ("open_0915", q_up(filters.open_0915, 2)?),
        ("gap_pct", q_up(filters.gap_pct, 4)?),
        ("gap_max_pct", py_str(cfg.gap_max_pct).into()),
        ("or_high", q_up(filters.or_high, 2)?),
        ("or_low", q_up(filters.or_low, 2)?),
        ("or_pct", q_up(filters.or_pct, 4)?),
        ("or_max_pct", py_str(cfg.or_max_pct).into()),
        ("vix", q_up(filters.vix, 2)?),
        ("vix_max", py_str(cfg.vix_max).into()),
        ("bars", filters.bars.into()),
        ("expiry", expiry.map(|e| e.to_string()).into()),
    ]))
}

/// Every sleeve's row for the minute of `now`, in [`SCAN_ORDER`].
pub fn scan_all(
    market: &MarketDay,
    context: &DayContext,
    now: &DateTime<FixedOffset>,
    snapshots: &mut SnapshotBook,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<Vec<ScanResult>> {
    let scan = Scan {
        market,
        context,
        now,
        options,
        ceilings,
    };
    let mut rows: Vec<(Sleeve, Row)> = Vec::with_capacity(SCAN_ORDER.len());
    for sleeve in SCAN_ORDER {
        let row = match sleeve {
            Sleeve::O1m | Sleeve::O1w => scan.o1(sleeve, snapshots)?,
            Sleeve::O2 => scan.o2(snapshots)?,
            Sleeve::O3b => scan.o3b(snapshots)?,
            Sleeve::O3a => {
                let o3b = rows.iter().find(|(s, _)| *s == Sleeve::O3b).map(|(_, r)| r);
                scan.o3a(snapshots, o3b)?
            }
        };
        rows.push((sleeve, row));
    }
    Ok(rows
        .into_iter()
        .map(|(s, r)| finish(s, r, market, now, options))
        .collect())
}

/// The snapshot minutes [`scan_all`] will ask for at `now` (`None` = the latest).
pub fn wanted_minutes(
    market: &MarketDay,
    context: &DayContext,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<BTreeSet<MinuteKey>> {
    let mut book = SnapshotBook::default();
    scan_all(market, context, now, &mut book, options, ceilings)?;
    Ok(book.asked)
}
