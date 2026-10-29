//! Port of `baskfy_core.vbt.plan` (`decile-blueprint/packages/core/src/baskfy_core/vbt/plan.py`)
//! — a session's plan: what the desk shows and a person confirms (`docs/vbt/04` §9).
//!
//! The plan is the only thing that becomes an order, and **a plan is not honest without its
//! skips**: every signal the rules passed over leaves a [`Skipped`] with the reason that stopped
//! it, in the order `04` §9.1 checks them.
//!
//! Pure. This module builds values; the desk gives the plan a `plan_id`, a 30-minute expiry and
//! a gateway, and a person presses Confirm (there is no auto-execute flag for this sleeve, and
//! none is added). Levels are computed in Python's `Decimal` arithmetic (`vbt::exits::wide`).
//!
//! Goldens: `plan.to_tick`, `plan.BookState`, `plan.build_entries`, `plan.exit_lines`,
//! `plan.assemble`.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::str::FromStr;

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize, Serializer};

use crate::backtest::helpers::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::screener::sha256;
use crate::vbt::config::{Gate, TICK_INR, VbtConfig};
use crate::vbt::exits::{Action, ExitReason, ManageAction, initial_stop_wide, wide};
use crate::vbt::orders::WorkingOrder;
use crate::vbt::sizing::{SizeCap, SizeRefusal, SizedEntry, size_entry};

/// Ports `baskfy_core.vbt.plan.LineKind` — what a confirmed line does.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum LineKind {
    /// Place the working limit at the signal bar's close (`04` §7).
    #[serde(rename = "PLACE_LIMIT")]
    PlaceLimit,
    /// Sell at the next open: the whole position, or (`reason == PARTIAL`) a third.
    #[serde(rename = "SELL_AT_OPEN")]
    SellAtOpen,
    /// Cancel a working order at the end of its third session.
    #[serde(rename = "CANCEL_LIMIT")]
    CancelLimit,
    /// Arm a GTT for a filled position that has none (non-negotiable 4's backstop).
    #[serde(rename = "ARM_GTT")]
    ArmGtt,
    /// LV8: a live scan's entry — a MARKET buy confirmed by hand.
    #[serde(rename = "BUY_AT_MARKET")]
    BuyAtMarket,
    /// LV9: raise a position's resting GTT to a higher trigger (breakeven); never lowers.
    #[serde(rename = "RAISE_GTT_STOP")]
    RaiseGttStop,
}

impl LineKind {
    /// The enum's string value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::PlaceLimit => "PLACE_LIMIT",
            Self::SellAtOpen => "SELL_AT_OPEN",
            Self::CancelLimit => "CANCEL_LIMIT",
            Self::ArmGtt => "ARM_GTT",
            Self::BuyAtMarket => "BUY_AT_MARKET",
            Self::RaiseGttStop => "RAISE_GTT_STOP",
        }
    }

    /// True for `ENTRY_KINDS` — the kinds that commit new money.
    #[must_use]
    pub fn is_entry(self) -> bool {
        matches!(self, Self::PlaceLimit | Self::BuyAtMarket)
    }

    /// `LINE_ORDER.index(kind)`.
    #[must_use]
    pub fn order(self) -> usize {
        LINE_ORDER
            .iter()
            .position(|k| *k == self)
            .unwrap_or(LINE_ORDER.len())
    }
}

/// Ports `baskfy_core.vbt.plan.LINE_ORDER` — the order `04` §9.3 renders a plan in: the risk
/// coming off before the risk going on.
pub const LINE_ORDER: [LineKind; 6] = [
    LineKind::SellAtOpen,
    LineKind::CancelLimit,
    LineKind::ArmGtt,
    LineKind::RaiseGttStop,
    LineKind::PlaceLimit,
    LineKind::BuyAtMarket,
];

/// Ports `baskfy_core.vbt.plan.SkipReason` — why a signal did not become a line.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SkipReason {
    /// The sleeve has no capital.
    #[serde(rename = "NO_SLEEVE_CAPITAL")]
    NoSleeveCapital,
    /// The breadth gate is shut.
    #[serde(rename = "GATE_SHUT")]
    GateShut,
    /// The signal bar printed at the upper band.
    #[serde(rename = "LOCKED_UPPER_CIRCUIT")]
    LockedUpperCircuit,
    /// Already held (or at the pyramiding cap).
    #[serde(rename = "ALREADY_HELD")]
    AlreadyHeld,
    /// A limit is already resting.
    #[serde(rename = "ALREADY_WORKING")]
    AlreadyWorking,
    /// The session's new-entry cap.
    #[serde(rename = "SESSION_CAP")]
    SessionCap,
    /// Every slot is taken.
    #[serde(rename = "SLOTS_FULL")]
    SlotsFull,
    /// The stop is not below the limit.
    #[serde(rename = "STOP_NOT_BELOW_ENTRY")]
    StopNotBelowEntry,
    /// The sized value is below the minimum trade.
    #[serde(rename = "BELOW_MIN_TRADE_VALUE")]
    BelowMinTradeValue,
    /// The turnover cap cannot fund the minimum trade.
    #[serde(rename = "TURNOVER_CAP")]
    TurnoverCap,
    /// The sleeve's cash is fully committed.
    #[serde(rename = "EXPOSURE_FULL")]
    ExposureFull,
}

fn refusal_to_skip(refusal: SizeRefusal) -> SkipReason {
    match refusal {
        SizeRefusal::NoSleeveCapital => SkipReason::NoSleeveCapital,
        SizeRefusal::StopNotBelowEntry => SkipReason::StopNotBelowEntry,
        SizeRefusal::BelowMinTradeValue => SkipReason::BelowMinTradeValue,
        SizeRefusal::TurnoverCap => SkipReason::TurnoverCap,
    }
}

/// Ports `baskfy_core.vbt.plan.Candidate` — one `SIGNAL` row as the plan needs it. Levels are
/// exchange prices.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Candidate {
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// The signal session.
    pub signal_date: NaiveDate,
    /// The signal bar's exchange close.
    pub close_raw: Decimal,
    /// The 20-day average rupee turnover.
    pub turnover_avg_inr: Option<Decimal>,
    /// The rank (higher first).
    pub rank_key: i64,
    /// The bar printed at the upper band.
    #[serde(default)]
    pub locked_upper_circuit: bool,
}

/// Ports `baskfy_core.vbt.plan.Skipped`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Skipped {
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// Why.
    pub reason: SkipReason,
    /// A sentence.
    #[serde(default)]
    pub detail: String,
}

/// Ports `baskfy_core.vbt.plan.PlanLine` — one confirmable line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanLine {
    /// What the line does.
    pub kind: LineKind,
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// Shares.
    pub quantity: i64,
    /// The limit, if any.
    #[serde(default)]
    pub limit_price: Option<Decimal>,
    /// The stop, if any.
    #[serde(default)]
    pub stop_price: Option<Decimal>,
    /// The rupees the line commits (0 for exits).
    #[serde(default)]
    pub value_inr: Decimal,
    /// The budget that bound the size.
    #[serde(default)]
    pub cap: Option<SizeCap>,
    /// Why, for a sell.
    #[serde(default)]
    pub reason: Option<ExitReason>,
    /// A sentence.
    #[serde(default)]
    pub note: String,
    /// LV10: the position an exit or a raise acts on.
    #[serde(default)]
    pub position_id: Option<i64>,
}

/// `canonical()`'s `position_id`: the id, or `""` when there is none.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CanonicalId {
    /// A position id.
    Id(i64),
    /// No position id (`""`).
    Blank,
}

impl Serialize for CanonicalId {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Id(v) => serializer.serialize_i64(*v),
            Self::Blank => serializer.serialize_str(""),
        }
    }
}

/// Ports the dict `PlanLine.canonical()` returns — the form `plan_hash` sees.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CanonicalLine {
    /// `kind.value`.
    pub kind: String,
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// Shares.
    pub quantity: i64,
    /// The position id or `""`.
    pub position_id: CanonicalId,
    /// `str(limit_price)` or `""`.
    pub limit_price: String,
    /// `str(stop_price)` or `""`.
    pub stop_price: String,
}

impl PlanLine {
    /// Ports `PlanLine.canonical`: prices as strings of their exact decimal, never floats.
    #[must_use]
    pub fn canonical(&self) -> CanonicalLine {
        CanonicalLine {
            kind: self.kind.value().to_owned(),
            instrument_id: self.instrument_id,
            symbol: self.symbol.clone(),
            quantity: self.quantity,
            position_id: self.position_id.map_or(CanonicalId::Blank, CanonicalId::Id),
            limit_price: self.limit_price.map_or_else(String::new, wide::py_str),
            stop_price: self.stop_price.map_or_else(String::new, wide::py_str),
        }
    }
}

/// Ports `baskfy_core.vbt.plan.VbtPlan` — exits first, then cancels, then entries.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VbtPlan {
    /// The session.
    pub session: NaiveDate,
    /// The gate.
    pub gate: Gate,
    /// The sleeve's equity.
    pub sleeve_equity_inr: Decimal,
    /// The lines, in `LINE_ORDER`.
    pub lines: Vec<PlanLine>,
    /// The skips.
    pub skips: Vec<Skipped>,
    /// `"EVENING"` or the live scan's source.
    pub generated_from: String,
}

/// `json.dumps(text)` with the default `ensure_ascii=True`.
fn json_ascii_str(text: &str, out: &mut String) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(ch),
            c => {
                let mut units = [0_u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
}

impl VbtPlan {
    /// Ports the `total_new_exposure_inr` property: `sum(value_inr of entries, Decimal(0))`.
    ///
    /// # Errors
    /// A sum `rust_decimal` cannot hold.
    pub fn total_new_exposure_inr(&self) -> CoreResult<Decimal> {
        let mut total = WideDecimal::zero();
        for line in self.lines.iter().filter(|l| l.kind.is_entry()) {
            total = total.add(&WideDecimal::from(line.value_inr));
        }
        wide::to_dec(&total)
    }

    /// Ports the `entries` property: the lines that commit new money.
    #[must_use]
    pub fn entries(&self) -> Vec<PlanLine> {
        self.lines
            .iter()
            .filter(|l| l.kind.is_entry())
            .cloned()
            .collect()
    }

    /// Ports `VbtPlan.plan_hash`: sha256 of `json.dumps({session, gate, lines: [canonical]},
    /// sort_keys=True, separators=(",", ":"))`. **The same plan hashes the same.**
    #[must_use]
    pub fn plan_hash(&self) -> String {
        let mut out = String::new();
        out.push_str("{\"gate\":");
        json_ascii_str(self.gate.value(), &mut out);
        out.push_str(",\"lines\":[");
        for (i, line) in self.lines.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            let c = line.canonical();
            let _ = write!(out, "{{\"instrument_id\":{},\"kind\":", c.instrument_id);
            json_ascii_str(&c.kind, &mut out);
            out.push_str(",\"limit_price\":");
            json_ascii_str(&c.limit_price, &mut out);
            out.push_str(",\"position_id\":");
            match c.position_id {
                CanonicalId::Id(v) => {
                    let _ = write!(out, "{v}");
                }
                CanonicalId::Blank => json_ascii_str("", &mut out),
            }
            let _ = write!(out, ",\"quantity\":{},\"stop_price\":", c.quantity);
            json_ascii_str(&c.stop_price, &mut out);
            out.push_str(",\"symbol\":");
            json_ascii_str(&c.symbol, &mut out);
            out.push('}');
        }
        out.push_str("],\"session\":");
        json_ascii_str(&self.session.format("%Y-%m-%d").to_string(), &mut out);
        out.push('}');
        sha256::hex_digest(out.as_bytes())
    }
}

/// Ports `baskfy_core.vbt.plan.BookState` — what the sleeve holds and what it has already done
/// this session.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct BookState {
    /// Instruments with an open position.
    pub open_instrument_ids: BTreeSet<i64>,
    /// Instruments with a working limit.
    pub working_instrument_ids: BTreeSet<i64>,
    /// Rupees committed to open positions.
    pub open_exposure_inr: Decimal,
    /// Cash the sleeve can spend.
    pub cash_available_inr: Decimal,
    /// Entries already lined this session.
    pub entries_already_this_session: i64,
    /// `(instrument_id, symbol, quantity, stop)` for positions with no resting GTT.
    pub positions_naked_of_gtt: Vec<(i64, String, i64, Decimal)>,
    /// LV10: open entries per held name. Empty means one per name.
    pub open_entry_counts: BTreeMap<i64, i64>,
}

impl BookState {
    /// Ports `BookState.entries_in`: open entries in the name.
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

    /// Ports the `slots_taken` property: held entries plus working orders (a working order
    /// holds a slot; since LV10 a second entry in a name is a second slot).
    #[must_use]
    pub fn slots_taken(&self) -> i64 {
        let held = if self.open_entry_counts.is_empty() {
            i64::try_from(self.open_instrument_ids.len()).unwrap_or(i64::MAX)
        } else {
            self.open_entry_counts.values().sum()
        };
        held.saturating_add(i64::try_from(self.working_instrument_ids.len()).unwrap_or(i64::MAX))
    }
}

/// `tick or Decimal(TICK_INR)`: a missing **or zero** tick is the exchange's.
fn tick_or_default(tick: Option<Decimal>) -> CoreResult<WideDecimal> {
    match tick {
        Some(t) if !t.is_zero() => Ok(WideDecimal::from(t)),
        _ => WideDecimal::from_str(TICK_INR).map_err(wide::arith),
    }
}

fn to_tick_wide(price: &WideDecimal, tick: &WideDecimal) -> CoreResult<WideDecimal> {
    Ok(wide::div(price, tick)?
        .quantize(0, Rounding::HalfUp)
        .map_err(wide::arith)?
        .mul(tick))
}

/// Ports `baskfy_core.vbt.plan.to_tick`: the nearest tick, half-up (`tick` defaults to
/// `TICK_INR`, as does a zero tick).
///
/// # Errors
/// A quotient Python's `quantize` would refuse, or a result `rust_decimal` cannot hold.
pub fn to_tick(price: Decimal, tick: Option<Decimal>) -> CoreResult<Decimal> {
    let tick = tick_or_default(tick)?;
    wide::to_dec(&to_tick_wide(&WideDecimal::from(price), &tick)?)
}

fn refusal_detail(sized: &SizedEntry) -> &'static str {
    match sized.refusal {
        Some(SizeRefusal::TurnoverCap) => {
            "1% of the name's 20-day turnover cannot fund the minimum trade"
        }
        Some(SizeRefusal::BelowMinTradeValue) => "the sized value is below the minimum trade",
        Some(SizeRefusal::StopNotBelowEntry) => "the stop is not below the limit",
        _ => "the sleeve has no capital set",
    }
}

fn cap_lower(cap: Option<SizeCap>) -> &'static str {
    match cap {
        Some(SizeCap::Slot) => "slot",
        Some(SizeCap::PositionPct) => "position_pct",
        Some(SizeCap::Cash) => "cash",
        Some(SizeCap::Turnover) => "turnover",
        None => "nothing",
    }
}

fn entry_line(candidate: &Candidate, sized: &SizedEntry) -> PlanLine {
    PlanLine {
        kind: LineKind::PlaceLimit,
        instrument_id: candidate.instrument_id,
        symbol: candidate.symbol.clone(),
        quantity: sized.quantity,
        limit_price: Some(sized.limit_price),
        stop_price: Some(sized.stop_price),
        value_inr: sized.value_inr,
        cap: sized.cap,
        reason: None,
        note: format!(
            "limit at the signal close; {} bound the size",
            cap_lower(sized.cap)
        ),
        position_id: None,
    }
}

/// Ports `baskfy_core.vbt.plan.build_entries`: `04` §9.1, in its order. Cash spent by an
/// earlier line is not spent twice. Candidates go highest `rank_key` first, then by symbol.
///
/// `max_open_positions` is the trader's own cap; the plan takes the smaller of it and
/// `sizing.max_slots` (a missing or zero setting means `max_slots`).
///
/// # Errors
/// What `size_entry` refuses (Python's decimal failures), and levels `rust_decimal` cannot
/// hold.
#[allow(clippy::too_many_arguments)] // one keyword per gate the plan must consult, as Python
pub fn build_entries(
    candidates: &[Candidate],
    gate: Gate,
    equity: Decimal,
    book: &BookState,
    config: &VbtConfig,
    slot_multiplier: Decimal,
    max_open_positions: Option<i64>,
    tick: Option<Decimal>,
) -> CoreResult<(Vec<PlanLine>, Vec<Skipped>)> {
    let sizing = &config.sizing;
    let tick = tick_or_default(tick)?;
    let wanted = match max_open_positions {
        Some(v) if v != 0 => v,
        _ => sizing.max_slots,
    };
    let slot_ceiling = wanted.min(sizing.max_slots);
    let mut lines: Vec<PlanLine> = Vec::new();
    let mut skips: Vec<Skipped> = Vec::new();
    let mut cash = WideDecimal::from(book.cash_available_inr);
    let mut exposure = WideDecimal::from(book.open_exposure_inr);
    let wide_equity = WideDecimal::from(equity);
    let mut lined: i64 = 0;

    let mut ordered: Vec<&Candidate> = candidates.iter().collect();
    ordered.sort_by(|a, b| {
        b.rank_key
            .cmp(&a.rank_key)
            .then_with(|| a.symbol.cmp(&b.symbol))
    });

    for candidate in ordered {
        let mut skip = |reason: SkipReason, detail: String| {
            skips.push(Skipped {
                instrument_id: candidate.instrument_id,
                symbol: candidate.symbol.clone(),
                reason,
                detail,
            });
        };
        if equity <= Decimal::ZERO {
            skip(
                SkipReason::NoSleeveCapital,
                "the sleeve has no capital set".to_owned(),
            );
            continue;
        }
        if gate == Gate::Shut {
            skip(
                SkipReason::GateShut,
                "breadth is at or below the gate".to_owned(),
            );
            continue;
        }
        if candidate.locked_upper_circuit {
            skip(
                SkipReason::LockedUpperCircuit,
                "the bar printed at the upper band".to_owned(),
            );
            continue;
        }
        let held = book.entries_in(candidate.instrument_id);
        if held != 0 && !sizing.pyramiding {
            skip(
                SkipReason::AlreadyHeld,
                "one position per name; never averaged down".to_owned(),
            );
            continue;
        }
        if held >= sizing.max_entries_per_name {
            // LV10: his pyramiding — a fresh signal in a held name is a new entry, up to the cap.
            skip(
                SkipReason::AlreadyHeld,
                format!(
                    "{held} open entries in this name already; {} is the cap",
                    sizing.max_entries_per_name
                ),
            );
            continue;
        }
        if book
            .working_instrument_ids
            .contains(&candidate.instrument_id)
        {
            skip(
                SkipReason::AlreadyWorking,
                "a limit is already resting in this name".to_owned(),
            );
            continue;
        }
        if lined + book.entries_already_this_session >= sizing.max_new_entries_per_session {
            skip(
                SkipReason::SessionCap,
                format!(
                    "{} new entries a session",
                    sizing.max_new_entries_per_session
                ),
            );
            continue;
        }
        if book.slots_taken() + lined >= slot_ceiling {
            skip(
                SkipReason::SlotsFull,
                format!("{slot_ceiling} slots, positions and working orders"),
            );
            continue;
        }

        let limit = to_tick_wide(&WideDecimal::from(candidate.close_raw), &tick)?;
        let stop = initial_stop_wide(&limit, &config.exits, &tick)?;
        let sized = size_entry(
            equity,
            wide::to_dec(&cash)?,
            wide::to_dec(&limit)?,
            wide::to_dec(&stop)?,
            candidate.turnover_avg_inr,
            sizing,
            slot_multiplier,
        )?;
        if !sized.placed() {
            let Some(refusal) = sized.refusal else {
                return Err(CoreError::InvalidInput(
                    "size_entry placed nothing and gave no refusal".to_owned(),
                ));
            };
            skip(refusal_to_skip(refusal), refusal_detail(&sized).to_owned());
            continue;
        }
        let value = WideDecimal::from(sized.value_inr);
        if exposure.add(&value) > wide_equity {
            skip(
                SkipReason::ExposureFull,
                "the sleeve's cash is fully committed".to_owned(),
            );
            continue;
        }

        lines.push(entry_line(candidate, &sized));
        cash = cash.sub(&value);
        exposure = exposure.add(&value);
        lined += 1;
    }
    Ok((lines, skips))
}

/// Ports the `ManagedRow` tuple `exit_lines` reads: `(instrument_id, symbol, quantity_open,
/// action)` and, since LV10, an optional fifth element, the position id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedRow {
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: String,
    /// Shares open.
    pub quantity_open: i64,
    /// What `manage` said.
    pub action: ManageAction,
    /// The position id (the fifth element; `None` when absent or `None`).
    pub position_id: Option<i64>,
}

/// Ports `baskfy_core.vbt.plan.exit_lines`: sells, then cancels, then the GTT backstop — and,
/// since LV9, the partial sale and the breakeven raise.
#[must_use]
pub fn exit_lines(
    managed: &[ManagedRow],
    expired: &[(WorkingOrder, String)],
    book: &BookState,
) -> Vec<PlanLine> {
    let blank = |kind: LineKind, instrument_id: i64, symbol: &str, quantity: i64| PlanLine {
        kind,
        instrument_id,
        symbol: symbol.to_owned(),
        quantity,
        limit_price: None,
        stop_price: None,
        value_inr: Decimal::ZERO,
        cap: None,
        reason: None,
        note: String::new(),
        position_id: None,
    };
    let mut lines: Vec<PlanLine> = Vec::new();
    for row in managed {
        let action = &row.action;
        let base = |kind: LineKind, quantity: i64| {
            let mut line = blank(kind, row.instrument_id, &row.symbol, quantity);
            line.position_id = row.position_id;
            line
        };
        match action.action {
            Action::QueueSellAtOpen => {
                let mut line = base(LineKind::SellAtOpen, row.quantity_open);
                line.reason = action.reason;
                line.note.clone_from(&action.note);
                lines.push(line);
            }
            Action::QueuePartialAtOpen => {
                let Some(quantity) = action.quantity.filter(|q| *q != 0) else {
                    continue;
                };
                let mut line = base(LineKind::SellAtOpen, quantity);
                line.reason = Some(ExitReason::Partial);
                line.note.clone_from(&action.note);
                lines.push(line);
                if let Some(new_stop) = action.new_stop {
                    // The breakeven move rides with the partial: the stop for what stays open.
                    let mut raise = base(LineKind::RaiseGttStop, row.quantity_open - quantity);
                    raise.stop_price = Some(new_stop);
                    raise.note = "stop to breakeven after the partial".to_owned();
                    lines.push(raise);
                }
            }
            Action::RaiseStop => {
                if let Some(new_stop) = action.new_stop {
                    let mut line = base(LineKind::RaiseGttStop, row.quantity_open);
                    line.stop_price = Some(new_stop);
                    line.note.clone_from(&action.note);
                    lines.push(line);
                }
            }
            Action::Hold | Action::StoppedOut | Action::WriteOff => {}
        }
    }
    for (order, symbol) in expired {
        let mut line = blank(
            LineKind::CancelLimit,
            order.instrument_id,
            symbol,
            order.remaining(),
        );
        line.limit_price = Some(order.limit_price);
        line.note = "the third session has closed; the limit stops being an order".to_owned();
        lines.push(line);
    }
    for (instrument_id, symbol, quantity, stop) in &book.positions_naked_of_gtt {
        let mut line = blank(LineKind::ArmGtt, *instrument_id, symbol, *quantity);
        line.stop_price = Some(*stop);
        line.note =
            "a position without a resting stop is the one state the method forbids".to_owned();
        lines.push(line);
    }
    lines
}

/// Ports `baskfy_core.vbt.plan.assemble`: exits first, then entries, sorted (stably) by
/// `(LINE_ORDER.index(kind), symbol)`.
#[must_use]
pub fn assemble(
    session: NaiveDate,
    gate: Gate,
    equity: Decimal,
    exits: &[PlanLine],
    entries: &[PlanLine],
    skips: &[Skipped],
    source: &str,
) -> VbtPlan {
    let mut lines: Vec<PlanLine> = exits.iter().chain(entries).cloned().collect();
    lines.sort_by(|a, b| {
        a.kind
            .order()
            .cmp(&b.kind.order())
            .then_with(|| a.symbol.cmp(&b.symbol))
    });
    VbtPlan {
        session,
        gate,
        sleeve_equity_inr: equity,
        lines,
        skips: skips.to_vec(),
        generated_from: source.to_owned(),
    }
}
