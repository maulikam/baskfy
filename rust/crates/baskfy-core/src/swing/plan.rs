//! Port of `baskfy_core.swing.plan` (`decile-blueprint/packages/core/src/baskfy_core/swing/plan.py`).
//!
//! A day's plan (docs/swing/04 §9): watch items and account to lines the desk can confirm. Advice with numbers only — law 2: nothing here sends an order.
//!
//! Goldens: `L1/swing/build_entries.*` (`baskfy_core.swing.plan.build_entries`).
//!
//! The plan is *advice with numbers*: for each watch item it says the trigger (exchange price),
//! the stop, the share count and the rupees at risk. The desk turns a confirmed line into an
//! order through the gateway; nothing here knows a broker exists. The plan also says what it
//! **refused** and why.
//!
//! Money and prices are `Decimal` (house rule 9). Where Python's `Decimal` arithmetic decides
//! the *digits* of a string the golden grades (`to_tick`'s exponent, `str(Decimal)` inside a
//! note, `repr(float)` inside a skip detail) the port reproduces Python's rule exactly.

use std::collections::{BTreeMap, BTreeSet};

use chrono::NaiveDate;
use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::swing::market::{ExposureTier, MarketGate};
use crate::swing::setups::{Setup, SwingConfig, TRADEABLE_SETUPS};
use crate::swing::sizing::{
    SizeCap, SizeRefusal, SizedPosition, SizingConfig, decimal_from_py_float, size_position,
};
use crate::swing::stops::{StopConfig, TrailMa};

const PCT: Decimal = Decimal::ONE_HUNDRED;

/// What a plan line asks the desk to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum LineKind {
    /// Buy when the trigger trades.
    BuyOnTrigger,
    /// Sell at the open.
    SellAtOpen,
    /// Raise the GTT stop.
    RaiseGttStop,
    /// A live gap found before the open (A7): information only, never executable; it reserves
    /// one of the session's new-entry slots.
    PendingRange,
}

/// The kinds the desk's `/swing/execute` may turn into an order. `PENDING_RANGE` is not here
/// and must never be: a line with no quantity and no stop is not a buy (A7).
pub const EXECUTABLE_KINDS: [LineKind; 3] = [
    LineKind::BuyOnTrigger,
    LineKind::SellAtOpen,
    LineKind::RaiseGttStop,
];

/// Why a watch item produced no line.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SkipReason {
    /// The setup is never a BUY (PARABOLIC_SHORT).
    NotTradeableSetup,
    /// The gate is RED or the tier allows no new entries.
    GateRed,
    /// The sleeve is in its drawdown lock-out.
    DrawdownLockout,
    /// The rung's position count is full.
    TierFull,
    /// The session's new-entry cap is reached.
    SessionCap,
    /// The rung's exposure is full.
    ExposureFull,
    /// The name is held (at the pyramiding cap, or at all when pyramiding is off).
    AlreadyHeld,
    /// Locked at the upper band: no fill.
    LockedUpperCircuit,
    /// `size_position` refused.
    SizeRefused,
}

/// One name on the watchlist, with its levels as **exchange** prices.
///
/// `stop_ref` is `None` for a live gap found before the open (A7): the stop is the opening
/// range's to set, and the plan shows the name as a `PENDING_RANGE` line.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WatchItem {
    /// The trading symbol.
    pub symbol: String,
    /// Which setup put it on the list.
    pub setup: Setup,
    /// The trigger price.
    pub trigger: Decimal,
    /// The stop reference, `None` for a live gap.
    pub stop_ref: Option<Decimal>,
    /// ADR%, percent.
    pub adr_pct: Decimal,
    /// Average daily turnover, rupees.
    pub avg_turnover_inr: Option<Decimal>,
    /// The detector's score.
    pub score: Decimal,
    /// Today's high touched the upper band.
    #[serde(default)]
    pub locked_upper_circuit: bool,
}

/// The sleeve the swing book trades from. Rupees.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SwingAccount {
    /// Sleeve equity.
    pub equity: Decimal,
    /// Cash on hand.
    pub cash_available: Decimal,
    /// The names held.
    pub open_symbols: BTreeSet<String>,
    /// Rupees deployed.
    pub open_exposure_inr: Decimal,
    /// LV10: open entries per held name (pyramiding). Empty means one per name in
    /// `open_symbols`.
    #[serde(default)]
    pub open_entry_counts: BTreeMap<String, i64>,
}

impl SwingAccount {
    /// Open entries in `symbol`.
    pub fn entries_in(&self, symbol: &str) -> i64 {
        if !self.open_entry_counts.is_empty() {
            return self.open_entry_counts.get(symbol).copied().unwrap_or(0);
        }
        i64::from(self.open_symbols.contains(symbol))
    }

    /// Open entries — a second entry in a name is a second position (LV10).
    pub fn open_count(&self) -> i64 {
        if !self.open_entry_counts.is_empty() {
            return self.open_entry_counts.values().sum();
        }
        i64::try_from(self.open_symbols.len()).unwrap_or(i64::MAX)
    }
}

/// One line of the plan.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlanLine {
    /// What to do.
    pub kind: LineKind,
    /// The trading symbol.
    pub symbol: String,
    /// The setup behind an entry; `None` on an exit.
    pub setup: Option<Setup>,
    /// Shares.
    pub quantity: i64,
    /// The trigger, snapped to the tick.
    pub trigger: Option<Decimal>,
    /// The stop, snapped to the tick.
    pub stop: Option<Decimal>,
    /// Rupees at risk.
    pub risk_inr: Decimal,
    /// Rupees deployed.
    pub position_value: Decimal,
    /// The MA the position trails.
    pub trail: Option<TrailMa>,
    /// Why, in words.
    pub note: String,
    /// LV10: the position an exit or a raise acts on, when the name holds more than one.
    /// Entry lines never carry one, and it is encoded as `null` then, exactly as Python's
    /// `dataclasses.asdict` writes it (goldens regenerated after LV10, DECISIONS-RS R1.1).
    #[serde(default)]
    pub position_id: Option<i64>,
}

/// A watch item the plan refused, and why.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Skipped {
    /// The trading symbol.
    pub symbol: String,
    /// The reason.
    pub reason: SkipReason,
    /// The reason's detail.
    pub detail: String,
}

impl Skipped {
    fn new(symbol: &str, reason: SkipReason, detail: impl Into<String>) -> Self {
        Self {
            symbol: symbol.to_owned(),
            reason,
            detail: detail.into(),
        }
    }
}

/// `build_entries`' result: the lines and the refusals (the golden's
/// `{"lines": [...], "skipped": [...]}`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Entries {
    /// BUY and PENDING_RANGE lines, best score first.
    pub lines: Vec<PlanLine>,
    /// What was refused, in the order it was considered.
    pub skipped: Vec<Skipped>,
}

// ---------------------------------------------------------------------------
// Python's digits
// ---------------------------------------------------------------------------

fn arith(value: Option<Decimal>, what: &str) -> CoreResult<Decimal> {
    value.ok_or_else(|| CoreError::Arithmetic(format!("{what} overflowed or divided by zero")))
}

/// `str(Decimal)`: plain digits unless the adjusted exponent is below -6 (scientific then).
/// A `rust_decimal` never has a positive exponent, so that branch of Python's rule is moot.
fn py_decimal_str(d: Decimal) -> String {
    let scale = i64::from(d.scale());
    let digits = d.mantissa().unsigned_abs().to_string();
    let len = i64::try_from(digits.len()).unwrap_or(i64::MAX);
    let adjusted = len - 1 - scale;
    if adjusted >= -6 {
        return d.to_string();
    }
    let sign = if d.is_sign_negative() { "-" } else { "" };
    let (head, rest) = digits.split_at(1);
    let body = if rest.is_empty() {
        head.to_owned()
    } else {
        format!("{head}.{rest}")
    };
    format!("{sign}{body}E{adjusted}")
}

use crate::py::float_repr as py_float_repr;

/// `d.quantize(Decimal("0.01"))` under the default context (ROUND_HALF_EVEN).
fn gate_value(gate: MarketGate) -> &'static str {
    match gate {
        MarketGate::Green => "GREEN",
        MarketGate::Amber => "AMBER",
        MarketGate::Red => "RED",
    }
}

fn cap_value(cap: SizeCap) -> &'static str {
    match cap {
        SizeCap::Risk => "RISK",
        SizeCap::PositionPct => "POSITION_PCT",
        SizeCap::Turnover => "TURNOVER",
        SizeCap::Cash => "CASH",
    }
}

fn refusal_value(refusal: SizeRefusal) -> &'static str {
    match refusal {
        SizeRefusal::StopNotBelowEntry => "STOP_NOT_BELOW_ENTRY",
        SizeRefusal::StopTooWide => "STOP_TOO_WIDE",
        SizeRefusal::BelowMinTradeValue => "BELOW_MIN_TRADE_VALUE",
        SizeRefusal::NoEquity => "NO_EQUITY",
    }
}

fn trail_value(trail: TrailMa) -> &'static str {
    match trail {
        TrailMa::Ma10 => "MA10",
        TrailMa::Ma20 => "MA20",
    }
}

// ---------------------------------------------------------------------------
// The helpers build_entries reads
// ---------------------------------------------------------------------------

/// Snap to the exchange tick (0.05) — `(price / tick).to_integral_value(ROUND_HALF_UP) * tick`.
///
/// The value is Python's; so are the digits. Python's `Decimal` division keeps the *ideal
/// exponent* (`exp(price) - exp(tick)`) when the quotient is exact, and `to_integral_value`
/// leaves a non-negative exponent alone, so `to_tick(96)` is `96.0`, `to_tick(100)` is `100`
/// and `to_tick(123.37)` is `123.35`. This reproduces that exponent.
///
/// # Errors
///
/// [`CoreError::Arithmetic`] when the price overflows.
pub fn to_tick(price: Decimal) -> CoreResult<Decimal> {
    // price / 0.05 == price * 20, exactly.
    let quotient = arith(price.checked_mul(Decimal::from(20)), "tick quotient")?;
    let mut rounded = quotient.round_dp_with_strategy(0, RoundingStrategy::MidpointAwayFromZero);
    rounded.rescale(0);
    let n = rounded.mantissa();
    let ideal = 2 - i64::from(price.scale());
    let exponent = if quotient.fract().is_zero() && ideal > 0 {
        let mut tz = 0_i64;
        let mut m = n;
        while m != 0 && m % 10 == 0 && tz < ideal {
            m /= 10;
            tz += 1;
        }
        if n == 0 { ideal } else { tz }
    } else {
        0
    };
    let divisor = 10_i128.pow(u32::try_from(exponent).unwrap_or(0));
    let coefficient = (n / divisor)
        .checked_mul(5)
        .ok_or_else(|| CoreError::Arithmetic("tick overflowed".to_owned()))?;
    let scale = u32::try_from(2 - exponent)
        .map_err(|_| CoreError::Arithmetic("tick exponent out of range".to_owned()))?;
    Decimal::try_from_i128_with_scale(coefficient, scale)
        .map_err(|e| CoreError::Arithmetic(format!("tick: {e}")))
}

/// How far below the entry a stop may sit for this name (`stops.widest_stop_pct`): one ADR,
/// never more than the absolute cap, to the cent.
///
/// # Errors
///
/// [`CoreError::Arithmetic`] on overflow; [`CoreError::InvalidInput`] for a non-finite config.
pub fn widest_stop_pct(adr_pct: Decimal, config: &StopConfig) -> CoreResult<Decimal> {
    let by_adr = arith(
        adr_pct.checked_mul(decimal_from_py_float(config.max_stop_adr_multiple)?),
        "widest stop",
    )?;
    let cap = decimal_from_py_float(config.max_stop_distance_pct)?;
    // Python's `min(by_adr, cap)` keeps the first on a tie; equal values quantize alike.
    Ok(crate::swing::sizing::quantize_cents(if cap < by_adr {
        cap
    } else {
        by_adr
    }))
}

/// Fast movers trail the 10-day, slower names the 20-day (`stops.choose_trail`).
///
/// # Errors
///
/// [`CoreError::InvalidInput`] for a non-finite config.
pub fn choose_trail(adr_pct: Decimal, config: &StopConfig) -> CoreResult<TrailMa> {
    if adr_pct >= decimal_from_py_float(config.fast_trail_min_adr_pct)? {
        return Ok(TrailMa::Ma10);
    }
    Ok(TrailMa::Ma20)
}

/// `02` §3.5 / STANDING-ANSWERS A9: the risk multiplier a plan is sized with —
/// `risk_multiplier_first_live` while the countdown runs **and** execution is enabled, 1
/// otherwise.
///
/// # Errors
///
/// [`CoreError::InvalidInput`] for a non-finite config.
pub fn first_live_multiplier(
    sessions_left: i64,
    execution_enabled: bool,
    config: &SizingConfig,
) -> CoreResult<Decimal> {
    if execution_enabled && sessions_left > 0 {
        return decimal_from_py_float(config.risk_multiplier_first_live);
    }
    Ok(Decimal::ONE)
}

/// `config` with `risk_per_trade_pct` scaled by `risk_multiplier` — the one place the
/// multiplier is applied, before `size_position`.
///
/// # Errors
///
/// [`CoreError::Arithmetic`] on overflow; [`CoreError::InvalidInput`] for a non-finite config.
pub fn sizing_at(config: &SizingConfig, risk_multiplier: Decimal) -> CoreResult<SizingConfig> {
    if risk_multiplier == Decimal::ONE {
        return Ok(config.clone());
    }
    let scaled = arith(
        decimal_from_py_float(config.risk_per_trade_pct)?.checked_mul(risk_multiplier),
        "scaled risk",
    )?;
    // Python's float(Decimal) is correctly rounded; so is parsing its digits.
    let as_float = scaled
        .to_string()
        .parse::<f64>()
        .map_err(|e| CoreError::InvalidInput(format!("scaled risk {scaled}: {e}")))?;
    let mut out = config.clone();
    out.risk_per_trade_pct = as_float;
    Ok(out)
}

// ---------------------------------------------------------------------------
// Lines
// ---------------------------------------------------------------------------

/// A7's information-only line: no quantity, no stop, a preview at a 1-ADR stop.
fn pending_line(
    item: &WatchItem,
    account: &SwingAccount,
    cash_left: Decimal,
    sizing: &SizingConfig,
    config: &SwingConfig,
) -> CoreResult<PlanLine> {
    let trail = choose_trail(item.adr_pct, &config.stops)?;
    let trigger = to_tick(item.trigger)?;
    let widest = widest_stop_pct(item.adr_pct, &config.stops)?;
    let preview = if item.locked_upper_circuit {
        "locked at the upper band: no fill until it unlocks".to_owned()
    } else if widest <= Decimal::ZERO {
        "ADR unknown: no preview".to_owned()
    } else {
        let fraction = arith(widest.checked_div(PCT), "preview stop")?;
        let below = arith(Decimal::ONE.checked_sub(fraction), "preview stop")?;
        let stop = to_tick(arith(trigger.checked_mul(below), "preview stop")?)?;
        let sized = size_position(
            account.equity,
            cash_left,
            trigger,
            stop,
            item.avg_turnover_inr,
            sizing,
            widest,
        )?;
        match sized.refusal {
            None => format!(
                "\u{2248} {} shares (\u{20b9}{} at risk) if the stop lands 1 ADR ({}%) below {}",
                sized.quantity,
                py_decimal_str(sized.risk_inr),
                py_decimal_str(widest),
                py_decimal_str(trigger),
            ),
            Some(refusal) => format!("no size at a 1-ADR stop: {}", refusal_value(refusal)),
        }
    };
    Ok(PlanLine {
        kind: LineKind::PendingRange,
        symbol: item.symbol.clone(),
        setup: Some(item.setup),
        quantity: 0,
        trigger: Some(trigger),
        stop: None,
        risk_inr: Decimal::ZERO,
        position_value: Decimal::ZERO,
        trail: Some(trail),
        note: format!(
            "{} score {}; live gap, stop set by the opening range at window close; slot \
             reserved; preview: {preview}",
            item.setup.as_str(),
            py_decimal_str(item.score),
        ),
        position_id: None,
    })
}

fn entry_line(
    item: &WatchItem,
    sized: &SizedPosition,
    config: &SwingConfig,
) -> CoreResult<PlanLine> {
    let trail = choose_trail(item.adr_pct, &config.stops)?;
    Ok(PlanLine {
        kind: LineKind::BuyOnTrigger,
        symbol: item.symbol.clone(),
        setup: Some(item.setup),
        quantity: sized.quantity,
        trigger: Some(to_tick(item.trigger)?),
        stop: Some(to_tick(sized.stop)?),
        risk_inr: sized.risk_inr,
        position_value: sized.position_value,
        trail: Some(trail),
        note: format!(
            "{} score {}; stop {}% below; size by {}; trail {}",
            item.setup.as_str(),
            py_decimal_str(item.score),
            py_decimal_str(sized.stop_distance_pct),
            sized.cap.map_or("RISK", cap_value),
            trail_value(trail),
        ),
        position_id: None,
    })
}

/// BUY lines for the watchlist, best score first, until the tier or the session is full.
///
/// The position count is the smaller of the ladder's rung and the trader's own cap; new
/// entries per session are capped at `max_new_entries_per_session`; each name's widest stop is
/// one ADR ([`widest_stop_pct`]). `entries_already_today` counts entries the session took
/// before this plan; `risk_multiplier` scales `risk_per_trade_pct` before sizing (A9).
///
/// A watch item with no `stop_ref` (a live gap, A7) becomes a `PENDING_RANGE` line after the
/// same refusals a buy faces (setup, lock-out, gate, held, session cap) and reserves one of the
/// session's new-entry slots; it neither counts against nor is refused by the rung's position
/// count.
///
/// Refusals are checked in Python's order: setup, drawdown lock-out, gate, held (LV10
/// pyramiding: refused only at `max_entries_per_name`, or at all when `pyramiding` is off),
/// locked band, session cap, pending, rung, size, exposure. Ties in score sort by symbol.
///
/// # Errors
///
/// [`CoreError::Arithmetic`] on a decimal overflow; [`CoreError::InvalidInput`] for a
/// non-finite config.
#[allow(clippy::too_many_arguments)]
pub fn build_entries(
    as_of: NaiveDate,
    watch: &[WatchItem],
    account: &SwingAccount,
    gate: MarketGate,
    tier: &ExposureTier,
    config: &SwingConfig,
    entries_already_today: i64,
    risk_multiplier: Decimal,
) -> CoreResult<Entries> {
    // `as_of` is part of Python's signature and of the plan's identity; the entries do not read it.
    let _ = as_of;
    let mut lines: Vec<PlanLine> = Vec::new();
    let mut skipped: Vec<Skipped> = Vec::new();
    let sizing = sizing_at(&config.sizing, risk_multiplier)?;
    let open_count = account.open_count();
    let mut exposure = account.open_exposure_inr;
    let max_exposure = arith(
        arith(
            account
                .equity
                .checked_mul(decimal_from_py_float(tier.max_exposure_pct)?),
            "max exposure",
        )?
        .checked_div(PCT),
        "max exposure",
    )?;
    let max_positions = tier
        .max_open_positions
        .min(config.sizing.max_open_positions);
    let session_cap = config.sizing.max_new_entries_per_session;

    // `sorted(watch, key=lambda w: (-w.score, w.symbol))` — stable, score descending.
    let mut ordered: Vec<&WatchItem> = watch.iter().collect();
    ordered.sort_by(|a, b| b.score.cmp(&a.score).then_with(|| a.symbol.cmp(&b.symbol)));

    for item in ordered {
        let buys = i64::try_from(
            lines
                .iter()
                .filter(|l| l.kind == LineKind::BuyOnTrigger)
                .count(),
        )
        .unwrap_or(i64::MAX);
        let symbol = item.symbol.as_str();
        if !TRADEABLE_SETUPS.contains(&item.setup) {
            skipped.push(Skipped::new(
                symbol,
                SkipReason::NotTradeableSetup,
                item.setup.as_str(),
            ));
            continue;
        }
        if tier.drawdown_locked {
            skipped.push(Skipped::new(
                symbol,
                SkipReason::DrawdownLockout,
                "sleeve in drawdown",
            ));
            continue;
        }
        if gate == MarketGate::Red || !tier.new_entries_allowed {
            skipped.push(Skipped::new(symbol, SkipReason::GateRed, gate_value(gate)));
            continue;
        }
        let held = account.entries_in(symbol);
        if held != 0 && !config.sizing.pyramiding {
            skipped.push(Skipped::new(symbol, SkipReason::AlreadyHeld, ""));
            continue;
        }
        if held >= config.sizing.max_entries_per_name {
            // LV10: his pyramiding — a fresh setup in a held name is a new entry, up to the cap.
            skipped.push(Skipped::new(
                symbol,
                SkipReason::AlreadyHeld,
                format!(
                    "{held} open entries already; {} is the cap",
                    config.sizing.max_entries_per_name
                ),
            ));
            continue;
        }
        if item.locked_upper_circuit && item.stop_ref.is_some() {
            skipped.push(Skipped::new(
                symbol,
                SkipReason::LockedUpperCircuit,
                "no fill at band",
            ));
            continue;
        }
        let taken = i64::try_from(lines.len()).unwrap_or(i64::MAX);
        if entries_already_today.saturating_add(taken) >= session_cap {
            skipped.push(Skipped::new(
                symbol,
                SkipReason::SessionCap,
                format!("{session_cap} new entries per session"),
            ));
            continue;
        }
        let mut spent = Decimal::ZERO;
        for line in &lines {
            spent = arith(spent.checked_add(line.position_value), "cash spent")?;
        }
        let cash_left = arith(account.cash_available.checked_sub(spent), "cash left")?;
        let Some(stop_ref) = item.stop_ref else {
            // A7: a slot in the session, not a seat in the rung.
            lines.push(pending_line(item, account, cash_left, &sizing, config)?);
            continue;
        };
        if open_count.saturating_add(buys) >= max_positions {
            skipped.push(Skipped::new(
                symbol,
                SkipReason::TierFull,
                format!("rung {} allows {max_positions} positions", tier.level),
            ));
            continue;
        }
        let sized = size_position(
            account.equity,
            cash_left,
            item.trigger,
            stop_ref,
            item.avg_turnover_inr,
            &sizing,
            widest_stop_pct(item.adr_pct, &config.stops)?,
        )?;
        if let Some(refusal) = sized.refusal {
            skipped.push(Skipped::new(
                symbol,
                SkipReason::SizeRefused,
                refusal_value(refusal),
            ));
            continue;
        }
        let after = arith(exposure.checked_add(sized.position_value), "exposure")?;
        if after > max_exposure {
            skipped.push(Skipped::new(
                symbol,
                SkipReason::ExposureFull,
                format!("{}% of sleeve", py_float_repr(tier.max_exposure_pct)),
            ));
            continue;
        }
        exposure = after;
        lines.push(entry_line(item, &sized, config)?);
    }
    Ok(Entries { lines, skipped })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    #[test]
    fn to_tick_keeps_pythons_exponent() {
        for (price, want) in [
            ("96", "96.0"),
            ("100", "100"),
            ("123.37", "123.35"),
            ("118.11", "118.10"),
            ("95.5", "95.5"),
            ("99", "99.0"),
            ("120", "120"),
            ("0", "0"),
            ("100.025", "100.05"),
        ] {
            assert_eq!(to_tick(d(price)).unwrap().to_string(), want, "{price}");
        }
    }

    #[test]
    fn python_reprs() {
        assert_eq!(py_float_repr(25.0), "25.0");
        assert_eq!(py_float_repr(0.5), "0.5");
        assert_eq!(py_float_repr(1e-05), "1e-05");
        assert_eq!(py_float_repr(1e16), "1e+16");
        assert_eq!(py_float_repr(123.456), "123.456");
        assert_eq!(py_decimal_str(d("4.00")), "4.00");
        assert_eq!(py_decimal_str(d("0.0000001")), "1E-7");
        assert_eq!(py_decimal_str(d("0.00000012")), "1.2E-7");
    }
}
