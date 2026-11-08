//! Port of `baskfy_core.twt.plan` — a session's plan: what the desk shows and a person confirms
//! (`docs/twt/04` §10).
//!
//! The plan is the only thing that becomes an order, and a plan is not honest without its skips:
//! every signal the rules passed over leaves a [`Skipped`] with the reason that stopped it, in
//! the order `04` §10.1 checks them.
//!
//! Pure. This module builds values; the desk gives the plan a `plan_id`, a 30-minute expiry and
//! a gateway, and **a person presses Confirm**. Money is `Decimal`; running totals (cash,
//! exposure) are kept in [`WideDecimal`] so they round where Python's `Decimal` does.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::WideDecimal;
use crate::error::{CoreError, CoreResult};
use crate::screener::sha256;
use crate::twt::config::{Gate, TICK_INR, TwtConfig};
use crate::twt::exits::{arith, initial_stop, stop_is_below_entry, wide};
use crate::twt::sizing::{SizeCap, SizeRefusal, SizedEntry, size_entry};

/// Ports `LineKind`: what a confirmed line does (`03` §7).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum LineKind {
    /// A signal from last night: buy at the next session's open, at market.
    #[serde(rename = "BUY_AT_OPEN")]
    BuyAtOpen,
    /// A filled position with no resting stop.
    #[serde(rename = "ARM_GTT")]
    ArmGtt,
    /// The ratchet: a higher trigger than the resting one.
    #[serde(rename = "RAISE_GTT_STOP")]
    RaiseGttStop,
    /// LV9: the partial into strength, or the remainder on a close below the trail MA.
    #[serde(rename = "SELL_AT_OPEN")]
    SellAtOpen,
}

impl LineKind {
    /// The enum's string value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::BuyAtOpen => "BUY_AT_OPEN",
            Self::ArmGtt => "ARM_GTT",
            Self::RaiseGttStop => "RAISE_GTT_STOP",
            Self::SellAtOpen => "SELL_AT_OPEN",
        }
    }
}

/// Ports `LINE_ORDER`: risk off before risk on (`04` §10.3).
pub const LINE_ORDER: [LineKind; 4] = [
    LineKind::SellAtOpen,
    LineKind::ArmGtt,
    LineKind::RaiseGttStop,
    LineKind::BuyAtOpen,
];

/// Ports `EXIT_LINE_KINDS`: the kinds `exit_lines` may produce.
pub const EXIT_LINE_KINDS: [LineKind; 3] = [
    LineKind::ArmGtt,
    LineKind::RaiseGttStop,
    LineKind::SellAtOpen,
];

/// Ports `SkipReason`: why a signal did not become a line, in `04` §10.1's order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SkipReason {
    /// Breadth at or below the gate.
    #[serde(rename = "GATE_SHUT")]
    GateShut,
    /// The name is held (or held to its pyramiding cap).
    #[serde(rename = "ALREADY_HELD")]
    AlreadyHeld,
    /// The 20-session average turnover is under the floor.
    #[serde(rename = "BELOW_LIQUIDITY_FLOOR")]
    BelowLiquidityFloor,
    /// No bar on the entry session.
    #[serde(rename = "NO_BAR")]
    NoBar,
    /// Limit-locked at the open.
    #[serde(rename = "LOCKED_UPPER_CIRCUIT")]
    LockedUpperCircuit,
    /// The session's entry cap.
    #[serde(rename = "SESSION_CAP")]
    SessionCap,
    /// Every slot held.
    #[serde(rename = "SLOTS_FULL")]
    SlotsFull,
    /// The sleeve has no capital.
    #[serde(rename = "NO_SLEEVE_CAPITAL")]
    NoSleeveCapital,
    /// The stop is not below the entry.
    #[serde(rename = "STOP_NOT_BELOW_ENTRY")]
    StopNotBelowEntry,
    /// The sized value is below the minimum trade.
    #[serde(rename = "BELOW_MIN_TRADE_VALUE")]
    BelowMinTradeValue,
    /// The sleeve's cash is fully committed.
    #[serde(rename = "EXPOSURE_FULL")]
    ExposureFull,
    /// In the schema, emitted by nothing (a person may record one by hand).
    #[serde(rename = "TURNOVER_CAP")]
    TurnoverCap,
}

fn refusal_to_skip(refusal: SizeRefusal) -> SkipReason {
    match refusal {
        SizeRefusal::NoSleeveCapital => SkipReason::NoSleeveCapital,
        SizeRefusal::StopNotBelowEntry => SkipReason::StopNotBelowEntry,
        SizeRefusal::BelowMinTradeValue => SkipReason::BelowMinTradeValue,
    }
}

/// Ports `EntryBar`: the session the order would fill in, when it is known.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EntryBar {
    /// The entry session.
    pub session: NaiveDate,
    /// `None`: the instrument printed no bar (`NO_BAR`).
    pub open: Option<Decimal>,
    /// `open == high == low`.
    #[serde(default)]
    pub limit_locked: bool,
}

/// Ports `Candidate`: one entry event, as the plan needs it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Candidate {
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// The signal session.
    pub signal_date: NaiveDate,
    /// The signal session's own rupee turnover (`04` §6.3, TW0.2).
    pub rank_key: Decimal,
    /// The 20-session average turnover; `None` fails the liquidity floor.
    pub turnover_avg_inr: Option<Decimal>,
    /// The price the line is sized against.
    pub entry_price: Decimal,
    /// The price `04` §7.1's stop is computed from.
    pub stop_reference_price: Decimal,
    /// The entry session's bar, when known.
    #[serde(default)]
    pub entry_bar: Option<EntryBar>,
}

/// Ports `Skipped`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Skipped {
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// Why.
    pub reason: SkipReason,
    /// A human detail.
    #[serde(default)]
    pub detail: String,
}

/// Ports `NakedPosition`: an open position with no resting stop.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NakedPosition {
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// Shares.
    pub quantity: i64,
    /// The stop to arm.
    pub stop_price: Decimal,
    /// The position, when the name holds more than one.
    #[serde(default)]
    pub position_id: Option<i64>,
}

/// Ports `RatchetDue`: a position whose evening arithmetic produced a higher trigger.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RatchetDue {
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// Shares.
    pub quantity: i64,
    /// The resting trigger.
    pub gtt_trigger: Decimal,
    /// The new trigger.
    pub next_trigger: Decimal,
    /// The session `next_trigger` was computed for.
    pub next_trigger_for: NaiveDate,
    /// The highest high since entry.
    pub high_since: Decimal,
    /// A human note (empty: the default note).
    #[serde(default)]
    pub note: String,
    /// The position, when the name holds more than one.
    #[serde(default)]
    pub position_id: Option<i64>,
}

/// Ports `SellDue`: LV9's sale for the next open (`PARTIAL` or `MA_TRAIL`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SellDue {
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// Shares.
    pub quantity: i64,
    /// `"PARTIAL"` or `"MA_TRAIL"`.
    pub reason: String,
    /// The session the decision was taken on.
    pub decided_for: NaiveDate,
    /// A human note (empty: the default note).
    #[serde(default)]
    pub note: String,
    /// The position, when the name holds more than one.
    #[serde(default)]
    pub position_id: Option<i64>,
}

/// Ports `PlanLine`: one confirmable line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanLine {
    /// What it does.
    pub kind: LineKind,
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// Shares.
    pub quantity: i64,
    /// The expected entry, for a buy.
    #[serde(default)]
    pub entry_price: Option<Decimal>,
    /// The stop (the new trigger, for a raise).
    #[serde(default)]
    pub stop_price: Option<Decimal>,
    /// The resting trigger, for a raise.
    #[serde(default)]
    pub previous_stop: Option<Decimal>,
    /// The highest high since entry, for a raise.
    #[serde(default)]
    pub high_since: Option<Decimal>,
    /// The line's value (`Decimal(0)` unless a buy).
    #[serde(default)]
    pub value_inr: Decimal,
    /// The cap that sized a buy.
    #[serde(default)]
    pub cap: Option<SizeCap>,
    /// A human note.
    #[serde(default)]
    pub note: String,
    /// LV10: the position an exit or a raise acts on.
    #[serde(default)]
    pub position_id: Option<i64>,
}

/// One value of [`PlanLine::canonical`]: Python's `str | int`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum CanonicalValue {
    /// An integer.
    Int(i64),
    /// A string.
    Str(String),
}

/// `str(d)` of a Python `Decimal`.
fn py_str(d: Decimal) -> String {
    WideDecimal::from(d).to_sci_string()
}

impl PlanLine {
    /// Ports `PlanLine.canonical`: the form `plan_hash` sees, prices as `str(Decimal)`.
    #[must_use]
    pub fn canonical(&self) -> BTreeMap<&'static str, CanonicalValue> {
        let price = |p: Option<Decimal>| CanonicalValue::Str(p.map(py_str).unwrap_or_default());
        BTreeMap::from([
            ("kind", CanonicalValue::Str(self.kind.value().to_owned())),
            ("instrument_id", CanonicalValue::Int(self.instrument_id)),
            ("symbol", CanonicalValue::Str(self.symbol.clone())),
            ("quantity", CanonicalValue::Int(self.quantity)),
            (
                "position_id",
                self.position_id
                    .map_or_else(|| CanonicalValue::Str(String::new()), CanonicalValue::Int),
            ),
            ("entry_price", price(self.entry_price)),
            ("stop_price", price(self.stop_price)),
        ])
    }
}

/// Ports `BookState`: what the sleeve holds and what it has already done this session.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct BookState {
    /// Held names.
    pub open_instrument_ids: BTreeSet<i64>,
    /// Open exposure.
    pub open_exposure_inr: Decimal,
    /// Cash available to the sleeve.
    pub cash_available_inr: Decimal,
    /// Entries already confirmed or sent this session.
    pub entries_already_this_session: i64,
    /// Open positions with no resting stop.
    pub positions_naked_of_gtt: Vec<NakedPosition>,
    /// Ratchets the evening computed.
    pub ratchets_due: Vec<RatchetDue>,
    /// LV9: sales the evening decided.
    pub sells_due: Vec<SellDue>,
    /// LV10: open entries per held name.
    pub open_entry_counts: BTreeMap<i64, i64>,
}

impl BookState {
    /// Ports `BookState.entries_in`: how many open entries the book holds in a name.
    #[must_use]
    pub fn entries_in(&self, instrument_id: i64) -> i64 {
        if !self.open_entry_counts.is_empty() {
            return self
                .open_entry_counts
                .get(&instrument_id)
                .copied()
                .unwrap_or(0);
        }
        i64::from(self.open_instrument_ids.contains(&instrument_id))
    }

    /// Ports `BookState.slots_taken`: open entries.
    #[must_use]
    pub fn slots_taken(&self) -> i64 {
        if !self.open_entry_counts.is_empty() {
            return self.open_entry_counts.values().sum();
        }
        i64::try_from(self.open_instrument_ids.len()).unwrap_or(i64::MAX)
    }
}

/// Ports `TwtPlan`: exits first, then entries (`04` §10.3).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TwtPlan {
    /// The session.
    pub session: NaiveDate,
    /// The gate.
    pub gate: Gate,
    /// The sleeve's equity.
    pub sleeve_equity_inr: Decimal,
    /// The lines, in `LINE_ORDER` then symbol.
    pub lines: Vec<PlanLine>,
    /// The skips.
    pub skips: Vec<Skipped>,
    /// `"EVENING"`, `"MORNING"`, ...
    pub generated_from: String,
}

impl TwtPlan {
    /// Ports `TwtPlan.entries`.
    #[must_use]
    pub fn entries(&self) -> Vec<PlanLine> {
        self.lines
            .iter()
            .filter(|l| l.kind == LineKind::BuyAtOpen)
            .cloned()
            .collect()
    }

    /// Ports `TwtPlan.total_new_exposure_inr`: `sum(value_inr of entries, Decimal(0))`.
    ///
    /// # Errors
    /// [`CoreError::Arithmetic`] when the total does not fit a `Decimal`.
    pub fn total_new_exposure_inr(&self) -> CoreResult<Decimal> {
        let total = self
            .lines
            .iter()
            .filter(|l| l.kind == LineKind::BuyAtOpen)
            .fold(WideDecimal::zero(), |acc, l| {
                acc.add(&WideDecimal::from(l.value_inr))
            });
        total.try_to_decimal().map_err(arith)
    }

    /// Ports `TwtPlan.plan_hash`: sha256 of `json.dumps({session, gate, lines: canonical},
    /// sort_keys=True, separators=(",", ":"))`.
    #[must_use]
    pub fn plan_hash(&self) -> String {
        let mut lines = String::from("[");
        for (i, line) in self.lines.iter().enumerate() {
            if i > 0 {
                lines.push(',');
            }
            lines.push('{');
            for (j, (key, value)) in line.canonical().iter().enumerate() {
                if j > 0 {
                    lines.push(',');
                }
                lines.push_str(&json_string(key));
                lines.push(':');
                match value {
                    CanonicalValue::Int(n) => {
                        let _ = write!(lines, "{n}");
                    }
                    CanonicalValue::Str(s) => lines.push_str(&json_string(s)),
                }
            }
            lines.push('}');
        }
        lines.push(']');
        let payload = format!(
            "{{\"gate\":{},\"lines\":{lines},\"session\":{}}}",
            json_string(self.gate.value()),
            json_string(&self.session.format("%Y-%m-%d").to_string()),
        );
        sha256::hex_digest(payload.as_bytes())
    }
}

/// `json.dumps(s)` with Python's default `ensure_ascii=True`.
fn json_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if c.is_ascii() && !c.is_ascii_control() => out.push(c),
            c => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
    out
}

/// Ports `client_id_for`: `plan_id:symbol` for an order, `plan_id:symbol:kind` for a GTT leg or
/// an exit (non-negotiable 6).
#[must_use]
pub fn client_id_for(plan_id: &str, symbol: &str, kind: LineKind) -> String {
    if EXIT_LINE_KINDS.contains(&kind) {
        return format!("{plan_id}:{symbol}:{}", kind.value());
    }
    format!("{plan_id}:{symbol}")
}

/// Ports `stop_for`: `04` §7.1's initial stop from the exchange's price.
///
/// # Errors
/// [`CoreError::Arithmetic`] on a zero tick.
pub fn stop_for(
    reference_price: Decimal,
    config: &TwtConfig,
    tick: Decimal,
) -> CoreResult<Decimal> {
    initial_stop(reference_price, &config.exits, tick)
}

fn default_tick() -> CoreResult<Decimal> {
    Decimal::from_str_exact(TICK_INR).map_err(arith)
}

const STOP_NOT_BELOW: &str = "the stop is not below the entry";

fn skip(candidate: &Candidate, reason: SkipReason, detail: impl Into<String>) -> Skipped {
    Skipped {
        instrument_id: candidate.instrument_id,
        symbol: candidate.symbol.clone(),
        reason,
        detail: detail.into(),
    }
}

/// `_before_sizing`: `04` §10.1's checks that depend on nothing the sizing produces, in order.
fn before_sizing(
    candidate: &Candidate,
    gate: Gate,
    equity: Decimal,
    book: &BookState,
    lined: i64,
    slot_ceiling: i64,
    config: &TwtConfig,
) -> Option<(SkipReason, String)> {
    let sizing = &config.sizing;
    if gate == Gate::Shut {
        return Some((
            SkipReason::GateShut,
            "breadth is at or below the gate".to_owned(),
        ));
    }
    let held = book.entries_in(candidate.instrument_id);
    if held != 0 && !sizing.pyramiding {
        return Some((
            SkipReason::AlreadyHeld,
            "one position per name; the book never averages down".to_owned(),
        ));
    }
    if held >= sizing.max_entries_per_name {
        return Some((
            SkipReason::AlreadyHeld,
            format!(
                "{held} open entries in this name already; {} is the cap",
                sizing.max_entries_per_name
            ),
        ));
    }
    let floor_inr = config.entry.min_turnover_inr;
    if candidate
        .turnover_avg_inr
        .is_none_or(|turnover| turnover < floor_inr)
    {
        return Some((
            SkipReason::BelowLiquidityFloor,
            format!(
                "20-session average turnover under the floor of {}",
                py_str(floor_inr)
            ),
        ));
    }
    if let Some(bar) = &candidate.entry_bar {
        if bar.open.is_none() {
            return Some((
                SkipReason::NoBar,
                "the instrument printed no bar on the entry session".to_owned(),
            ));
        }
        if bar.limit_locked {
            return Some((
                SkipReason::LockedUpperCircuit,
                "open == high == low: no fill is possible at a price the book would accept"
                    .to_owned(),
            ));
        }
    }
    if lined + book.entries_already_this_session >= sizing.max_new_entries_per_session {
        return Some((
            SkipReason::SessionCap,
            format!(
                "{} new entries a session",
                sizing.max_new_entries_per_session
            ),
        ));
    }
    if book.slots_taken() + lined >= slot_ceiling {
        return Some((
            SkipReason::SlotsFull,
            format!("{slot_ceiling} slots, all of them held"),
        ));
    }
    if equity <= Decimal::ZERO {
        return Some((
            SkipReason::NoSleeveCapital,
            "the sleeve has no capital set".to_owned(),
        ));
    }
    None
}

fn refusal_detail(sized: &SizedEntry) -> &'static str {
    match sized.refusal {
        Some(SizeRefusal::BelowMinTradeValue) => "the sized value is below the minimum trade",
        Some(SizeRefusal::StopNotBelowEntry) => "the stop is not below the entry",
        _ => "the sleeve has no capital set",
    }
}

fn cap_value(cap: SizeCap) -> &'static str {
    match cap {
        SizeCap::Slot => "SLOT",
        SizeCap::PositionPct => "POSITION_PCT",
        SizeCap::Turnover => "TURNOVER",
        SizeCap::Cash => "CASH",
    }
}

fn entry_line(candidate: &Candidate, sized: &SizedEntry) -> PlanLine {
    let bound = sized
        .cap
        .map_or_else(|| "nothing".to_owned(), |c| cap_value(c).to_lowercase());
    PlanLine {
        kind: LineKind::BuyAtOpen,
        instrument_id: candidate.instrument_id,
        symbol: candidate.symbol.clone(),
        quantity: sized.quantity,
        entry_price: Some(sized.entry_price),
        stop_price: Some(sized.stop_price),
        previous_stop: None,
        high_since: None,
        value_inr: sized.value_inr,
        cap: sized.cap,
        note: format!("buy at the next open, at market; {bound} bound the size"),
        position_id: None,
    }
}

/// Ports `build_entries`: `04` §10.1, in its order. Cash spent by earlier lines is not spent
/// twice. Signals are taken by `rank_key` descending then `symbol` ascending.
///
/// `slot_multiplier` is Python's default `Decimal(1)` when the caller has nothing else;
/// `max_open_positions` defaults to `sizing.max_slots`; `tick` to [`TICK_INR`].
///
/// # Errors
/// [`CoreError::Arithmetic`] where Python's `Decimal` would raise (a zero tick, a quantity
/// beyond 28 digits).
#[allow(clippy::too_many_arguments)]
pub fn build_entries(
    candidates: &[Candidate],
    gate: Gate,
    equity: Decimal,
    book: &BookState,
    config: &TwtConfig,
    slot_multiplier: Decimal,
    max_open_positions: Option<i64>,
    tick: Option<Decimal>,
) -> CoreResult<(Vec<PlanLine>, Vec<Skipped>)> {
    let sizing = &config.sizing;
    let tick_size = match tick {
        Some(t) => t,
        None => default_tick()?,
    };
    let slot_ceiling = max_open_positions.unwrap_or(sizing.max_slots);
    let mut lines: Vec<PlanLine> = Vec::new();
    let mut skips: Vec<Skipped> = Vec::new();
    let mut cash = WideDecimal::from(book.cash_available_inr);
    let mut exposure = WideDecimal::from(book.open_exposure_inr);
    let equity_w = WideDecimal::from(equity);
    let mut lined: i64 = 0;

    let mut ordered: Vec<&Candidate> = candidates.iter().collect();
    // `sorted(key=(-rank_key, symbol))`: stable, rank descending, then symbol ascending.
    ordered.sort_by(|a, b| {
        b.rank_key
            .cmp(&a.rank_key)
            .then_with(|| a.symbol.cmp(&b.symbol))
    });

    for candidate in ordered {
        if let Some((reason, detail)) =
            before_sizing(candidate, gate, equity, book, lined, slot_ceiling, config)
        {
            skips.push(skip(candidate, reason, detail));
            continue;
        }
        let stop = stop_for(candidate.stop_reference_price, config, tick_size)?;
        if !stop_is_below_entry(candidate.stop_reference_price, stop) {
            skips.push(skip(
                candidate,
                SkipReason::StopNotBelowEntry,
                STOP_NOT_BELOW,
            ));
            continue;
        }
        let sized = size_entry(
            equity,
            cash.try_to_decimal().map_err(arith)?,
            candidate.entry_price,
            stop,
            candidate.turnover_avg_inr,
            sizing,
            slot_multiplier,
        )?;
        if !sized.placed() {
            let Some(refusal) = sized.refusal else {
                return Err(CoreError::InvalidInput(
                    "a refused size carries no refusal".to_owned(),
                ));
            };
            skips.push(skip(
                candidate,
                refusal_to_skip(refusal),
                refusal_detail(&sized),
            ));
            continue;
        }
        let value = WideDecimal::from(sized.value_inr);
        if exposure.add(&value) > equity_w {
            skips.push(skip(
                candidate,
                SkipReason::ExposureFull,
                "the sleeve's cash is fully committed",
            ));
            continue;
        }
        lines.push(entry_line(candidate, &sized));
        cash = cash.sub(&value);
        exposure = exposure.add(&value);
        lined += 1;
    }
    Ok((lines, skips))
}

/// Ports `exit_lines`: `04` §10.2 plus LV9's sales — sells decided for `session`, then every
/// naked position's `ARM_GTT`, then every ratchet computed for `session` that is strictly above
/// its resting trigger. A stop never falls.
#[must_use]
pub fn exit_lines(book: &BookState, session: NaiveDate) -> Vec<PlanLine> {
    let iso = |d: NaiveDate| d.format("%Y-%m-%d").to_string();
    let mut lines: Vec<PlanLine> = book
        .sells_due
        .iter()
        .filter(|sale| sale.decided_for == session && sale.quantity > 0)
        .map(|sale| PlanLine {
            kind: LineKind::SellAtOpen,
            instrument_id: sale.instrument_id,
            symbol: sale.symbol.clone(),
            quantity: sale.quantity,
            entry_price: None,
            stop_price: None,
            previous_stop: None,
            high_since: None,
            value_inr: Decimal::ZERO,
            cap: None,
            note: if sale.note.is_empty() {
                format!(
                    "{} decided on {}",
                    sale.reason.to_lowercase(),
                    iso(sale.decided_for)
                )
            } else {
                sale.note.clone()
            },
            position_id: sale.position_id,
        })
        .collect();
    lines.extend(book.positions_naked_of_gtt.iter().map(|naked| PlanLine {
        kind: LineKind::ArmGtt,
        instrument_id: naked.instrument_id,
        symbol: naked.symbol.clone(),
        quantity: naked.quantity,
        entry_price: None,
        stop_price: Some(naked.stop_price),
        previous_stop: None,
        high_since: None,
        value_inr: Decimal::ZERO,
        cap: None,
        note: "a position without a resting stop is the one state the method forbids".to_owned(),
        position_id: naked.position_id,
    }));
    lines.extend(
        book.ratchets_due
            .iter()
            .filter(|due| due.next_trigger_for == session && due.next_trigger > due.gtt_trigger)
            .map(|due| PlanLine {
                kind: LineKind::RaiseGttStop,
                instrument_id: due.instrument_id,
                symbol: due.symbol.clone(),
                quantity: due.quantity,
                entry_price: None,
                stop_price: Some(due.next_trigger),
                previous_stop: Some(due.gtt_trigger),
                high_since: Some(due.high_since),
                value_inr: Decimal::ZERO,
                cap: None,
                note: if due.note.is_empty() {
                    format!("the stop moves on {}", iso(due.next_trigger_for))
                } else {
                    due.note.clone()
                },
                position_id: due.position_id,
            }),
    );
    lines
}

fn line_rank(kind: LineKind) -> usize {
    LINE_ORDER
        .iter()
        .position(|k| *k == kind)
        .unwrap_or(LINE_ORDER.len())
}

/// Ports `assemble`: exits first, then entries, sorted by (`LINE_ORDER`, symbol), stably.
#[must_use]
pub fn assemble(
    session: NaiveDate,
    gate: Gate,
    equity: Decimal,
    exits: &[PlanLine],
    entries: &[PlanLine],
    skips: &[Skipped],
    source: &str,
) -> TwtPlan {
    let mut lines: Vec<PlanLine> = exits.iter().chain(entries).cloned().collect();
    lines.sort_by(|a, b| {
        line_rank(a.kind)
            .cmp(&line_rank(b.kind))
            .then_with(|| a.symbol.cmp(&b.symbol))
    });
    TwtPlan {
        session,
        gate,
        sleeve_equity_inr: equity,
        lines,
        skips: skips.to_vec(),
        generated_from: source.to_owned(),
    }
}

/// Ports `gtt_limit_price`: the resting LIMIT a TWT GTT fires, `gtt_limit_fraction` of its
/// trigger, floored to the tick (`04` §10.6).
///
/// # Errors
/// [`CoreError::Arithmetic`] on a zero tick.
pub fn gtt_limit_price(
    trigger: Decimal,
    config: &TwtConfig,
    tick: Option<Decimal>,
) -> CoreResult<Decimal> {
    let tick_size = match tick {
        Some(t) => t,
        None => default_tick()?,
    };
    let limit = WideDecimal::from(trigger).mul(&WideDecimal::from(config.exits.gtt_limit_fraction));
    wide::tick_floor(&limit, &WideDecimal::from(tick_size))?
        .try_to_decimal()
        .map_err(arith)
}
