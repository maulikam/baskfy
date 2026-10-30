//! The pure pieces of `baskfy_core.swing` the desk reads and `baskfy-core` has not ported yet
//! (DECISIONS-RS R8.7): the gate from breadth and the index (`market.market_gate`,
//! `index_reading`, `ema`, `intraday_tier`), the A8 entry cap (`plan.marketable_limit`,
//! `market_protection_pct`), the plan's identity (`plan.assemble`, `SwingPlan.plan_hash`), the
//! journal's two numbers (`journal.exit_average`, `ClosedTrade.pnl_inr`, `sizing.r_multiple`)
//! and the candle range (`opening_range.opening_range`).
//!
//! Law 1 holds here as in core: no clock, no database, no broker. They live in the lane, not in
//! `baskfy-core`, so this lane edits no shared crate; moving them into core (beside their
//! goldens) is a mechanical follow-up.
//!
//! Python's `Decimal` digits are kept where a sentence shows them: [`py_dec`] is `str(Decimal)`,
//! [`quantize`] pads to the exponent as `Decimal.quantize` does, [`money`] is `f"{x:,.2f}"`.

use std::str::FromStr;

use baskfy_core::swing::market::{ExposureTier, MarketConfig, MarketGate};
use baskfy_core::swing::opening_range::{OpeningRange, OpeningRangeConfig};
use baskfy_core::swing::plan::{LineKind, PlanLine, Skipped};
use baskfy_core::swing::stops::TrailMa;
use chrono::{NaiveDate, NaiveDateTime};
use rust_decimal::{Decimal, RoundingStrategy};
use sha2::{Digest, Sha256};

/// A rule refused its input (Python's `ValueError`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("ValueError: {0}")]
pub struct RuleError(pub String);

/// `PLAN_TTL_MINUTES` (`baskfy_core.models.swing`): a plan is confirmable for thirty minutes.
pub const PLAN_TTL_MINUTES: i64 = 30;

/// `INDEX_HISTORY_SESSIONS`: how many published closes an index reading is taken over.
pub const INDEX_HISTORY_SESSIONS: i64 = 250;

const TICK: Decimal = Decimal::from_parts(5, 0, 0, false, 2);

/// `Decimal(str(x))` for a Python float.
#[must_use]
pub fn dec_of_float(x: f64) -> Decimal {
    baskfy_core::py::decimal_from_float_str(x).unwrap_or_default()
}

/// `d.quantize(Decimal("1e-<dp>"), rounding)`, padded to exactly `dp` places as Python keeps
/// the exponent.
#[must_use]
pub fn quantize(d: Decimal, dp: u32, rounding: RoundingStrategy) -> Decimal {
    let mut out = d.round_dp_with_strategy(dp, rounding);
    out.rescale(dp);
    out
}

/// `quantize` under Python's default context (`ROUND_HALF_EVEN`).
#[must_use]
pub fn q2(d: Decimal) -> Decimal {
    quantize(d, 2, RoundingStrategy::MidpointNearestEven)
}

/// `str(Decimal)`: positional unless the adjusted exponent is below -6.
#[must_use]
pub fn py_dec(d: Decimal) -> String {
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

/// `format(d, "f")`: always positional, the digits Python keeps.
#[must_use]
pub fn dec_f(d: Decimal) -> String {
    d.to_string()
}

fn group_thousands(int_part: &str) -> String {
    let bytes: Vec<char> = int_part.chars().collect();
    let mut out = String::new();
    for (i, c) in bytes.iter().enumerate() {
        if i > 0 && (bytes.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(*c);
    }
    out
}

/// `f"{d:,.2f}"` for a `Decimal` (rounded half to even, as `Decimal.__format__` does).
#[must_use]
pub fn money(d: Decimal) -> String {
    let v = q2(d);
    let text = v.abs().to_string();
    let (int_part, frac) = text.split_once('.').unwrap_or((text.as_str(), "00"));
    let sign = if v.is_sign_negative() && !v.is_zero() {
        "-"
    } else {
        ""
    };
    format!("{sign}{}.{frac}", group_thousands(int_part))
}

/// `f"{d:.2f}"` for a `Decimal`.
#[must_use]
pub fn fixed2(d: Decimal) -> String {
    q2(d).to_string()
}

/// `f"{x:g}"` for a float.
#[must_use]
pub fn float_g(x: f64) -> String {
    baskfy_providers::py::general(x)
}

// ---------------------------------------------------------------------------------------------
// market.py
// ---------------------------------------------------------------------------------------------

/// `BreadthSnapshot`: one day's breadth over the swing universe.
#[derive(Debug, Clone, PartialEq)]
pub struct BreadthSnapshot {
    /// `constituent_count`.
    pub constituent_count: i64,
    /// `pct_up_strong_1m`.
    pub pct_up_strong_1m: f64,
    /// `pct_new_52w_high`.
    pub pct_new_52w_high: f64,
    /// `pct_above_ma_slow`.
    pub pct_above_ma_slow: f64,
}

/// `IndexReading`: the benchmark's level and its two EMAs (SW31).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct IndexReading {
    /// The newest level.
    pub close: f64,
    /// EMA(10).
    pub ma_fast: f64,
    /// EMA(20).
    pub ma_slow: f64,
}

impl IndexReading {
    /// His filter: the 10-day above the 20-day.
    #[must_use]
    pub fn long_bias(&self) -> bool {
        self.ma_fast > self.ma_slow
    }

    /// The 10-day below the 20-day.
    #[must_use]
    pub fn bearish(&self) -> bool {
        self.ma_fast < self.ma_slow
    }
}

/// `market_gate`: breadth decides; the index can only make it worse.
#[must_use]
pub fn market_gate(
    breadth: &BreadthSnapshot,
    index: Option<&IndexReading>,
    config: &MarketConfig,
) -> MarketGate {
    if breadth.constituent_count == 0 {
        return MarketGate::Red;
    }
    if index.is_some_and(IndexReading::bearish) {
        return MarketGate::Red;
    }
    if breadth.pct_up_strong_1m <= config.red_max_pct_up {
        return MarketGate::Red;
    }
    if breadth.pct_up_strong_1m >= config.green_min_pct_up
        && index.is_none_or(IndexReading::long_bias)
    {
        return MarketGate::Green;
    }
    MarketGate::Amber
}

/// `ema`: TradingView's `ta.ema`, seeded with the simple average of the first `length` values;
/// with no more than `length` values, their simple average.
///
/// # Errors
/// No values (Python's `ValueError`).
#[allow(clippy::cast_precision_loss)]
pub fn ema(values: &[f64], length: i64) -> Result<f64, RuleError> {
    if values.is_empty() {
        return Err(RuleError("an EMA of no values".to_owned()));
    }
    let n = usize::try_from(length.max(0)).unwrap_or(0);
    if values.len() <= n {
        return Ok(py_sum(values) / values.len() as f64);
    }
    let alpha = 2.0 / (length as f64 + 1.0);
    let mut value = py_sum(&values[..n]) / n as f64;
    for x in &values[n..] {
        value = alpha * x + (1.0 - alpha) * value;
    }
    Ok(value)
}

/// Python's `sum()` over floats: left to right from `0`.
fn py_sum(values: &[f64]) -> f64 {
    values.iter().fold(0.0, |acc, x| acc + x)
}

/// `index_reading`: the newest level and its two EMAs, `None` with fewer than `index_ma_slow`.
#[must_use]
pub fn index_reading(levels: &[f64], config: &MarketConfig) -> Option<IndexReading> {
    let slow = usize::try_from(config.index_ma_slow).unwrap_or(usize::MAX);
    if levels.len() < slow {
        return None;
    }
    let close = *levels.last()?;
    Some(IndexReading {
        close,
        ma_fast: ema(levels, config.index_ma_fast).ok()?,
        ma_slow: ema(levels, config.index_ma_slow).ok()?,
    })
}

/// `intraday_tier`: the ladder does not move on a quote; only the gate's sides change what the
/// rung allows.
#[must_use]
pub fn intraday_tier(
    close: &ExposureTier,
    gate: MarketGate,
    config: &MarketConfig,
) -> ExposureTier {
    if close.drawdown_locked {
        return close.clone();
    }
    let rung = |level: i64| -> (i64, f64) {
        usize::try_from(level)
            .ok()
            .and_then(|i| config.tiers.get(i).copied())
            .unwrap_or((0, 0.0))
    };
    if gate == MarketGate::Red {
        let (positions, exposure) = rung(0);
        return ExposureTier {
            level: 0,
            max_open_positions: positions,
            max_exposure_pct: exposure,
            new_entries_allowed: false,
            drawdown_locked: false,
        };
    }
    let top = i64::try_from(config.tiers.len()).unwrap_or(1) - 1;
    let level = close.level.max(0).min(top);
    let (positions, exposure) = rung(level);
    ExposureTier {
        level,
        max_open_positions: positions,
        max_exposure_pct: exposure,
        new_entries_allowed: true,
        drawdown_locked: false,
    }
}

/// `MarketGate.value`.
#[must_use]
pub const fn gate_str(gate: MarketGate) -> &'static str {
    match gate {
        MarketGate::Green => "GREEN",
        MarketGate::Amber => "AMBER",
        MarketGate::Red => "RED",
    }
}

/// `MarketGate(value)`.
#[must_use]
pub fn gate_from(value: &str) -> Option<MarketGate> {
    match value {
        "GREEN" => Some(MarketGate::Green),
        "AMBER" => Some(MarketGate::Amber),
        "RED" => Some(MarketGate::Red),
        _ => None,
    }
}

// ---------------------------------------------------------------------------------------------
// plan.py: the A8 cap, the plan's identity
// ---------------------------------------------------------------------------------------------

/// `marketable_limit`: `min(trigger × (1 + buffer/100), range_high + max_adr × ADR% / 100 ×
/// trigger)`, snapped **down** to the tick.
#[must_use]
pub fn marketable_limit(
    trigger: Decimal,
    range_high: Option<Decimal>,
    adr_pct: Decimal,
    config: &OpeningRangeConfig,
) -> Decimal {
    let pct = Decimal::ONE_HUNDRED;
    let chase = trigger * (Decimal::ONE + dec_of_float(config.entry_limit_buffer_pct) / pct);
    let base = range_high.unwrap_or(trigger);
    let reach = base + dec_of_float(config.entry_limit_max_adr) * adr_pct / pct * trigger;
    let limit = chase.min(reach);
    // (limit / tick).to_integral_value(ROUND_DOWN) * tick: the tick's exponent is kept.
    let steps = (limit / TICK).round_dp_with_strategy(0, RoundingStrategy::ToZero);
    steps.trunc() * TICK
}

/// `market_protection_pct`: how far past the live price Kite may fill a MARKET entry, or `None`
/// to refuse — the price at or above the cap is not chased.
#[must_use]
pub fn market_protection_pct(
    cap: Decimal,
    last_price: Decimal,
    config: &OpeningRangeConfig,
) -> Option<Decimal> {
    if last_price <= Decimal::ZERO || last_price >= cap {
        return None;
    }
    let room = (cap / last_price - Decimal::ONE) * Decimal::ONE_HUNDRED;
    let floor = dec_of_float(config.market_protection_floor_pct);
    let ceiling = dec_of_float(config.market_protection_max_pct);
    Some(quantize(
        room.max(floor).min(ceiling),
        2,
        RoundingStrategy::ToZero,
    ))
}

/// `SwingPlan`: exits first, then entries; the totals are the entries'.
#[derive(Debug, Clone, PartialEq)]
pub struct SwingPlan {
    /// `as_of`.
    pub as_of: NaiveDate,
    /// `gate`.
    pub gate: MarketGate,
    /// `tier`.
    pub tier: ExposureTier,
    /// `lines`.
    pub lines: Vec<PlanLine>,
    /// `skipped`.
    pub skipped: Vec<Skipped>,
    /// `total_risk_inr`.
    pub total_risk_inr: Decimal,
    /// `total_new_exposure_inr`.
    pub total_new_exposure_inr: Decimal,
}

/// `assemble`.
#[must_use]
pub fn assemble(
    as_of: NaiveDate,
    gate: MarketGate,
    tier: ExposureTier,
    entries: Vec<PlanLine>,
    exits: Vec<PlanLine>,
    skipped: Vec<Skipped>,
) -> SwingPlan {
    let total_risk_inr = entries.iter().fold(Decimal::ZERO, |a, l| a + l.risk_inr);
    let total_new_exposure_inr = entries
        .iter()
        .fold(Decimal::ZERO, |a, l| a + l.position_value);
    let mut lines = exits;
    lines.extend(entries);
    SwingPlan {
        as_of,
        gate,
        tier,
        lines,
        skipped,
        total_risk_inr,
        total_new_exposure_inr,
    }
}

/// `LineKind.value`.
#[must_use]
pub const fn kind_str(kind: LineKind) -> &'static str {
    match kind {
        LineKind::BuyOnTrigger => "BUY_ON_TRIGGER",
        LineKind::SellAtOpen => "SELL_AT_OPEN",
        LineKind::RaiseGttStop => "RAISE_GTT_STOP",
        LineKind::PendingRange => "PENDING_RANGE",
    }
}

/// `TrailMa.value`.
#[must_use]
pub const fn trail_str(trail: TrailMa) -> &'static str {
    match trail {
        TrailMa::Ma10 => "MA10",
        TrailMa::Ma20 => "MA20",
    }
}

/// `SkipReason.value` (serde's SCREAMING_SNAKE_CASE is Python's).
#[must_use]
pub fn skip_str(reason: baskfy_core::swing::plan::SkipReason) -> String {
    serde_json::to_value(reason)
        .ok()
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_default()
}

fn json_str(s: &str) -> String {
    baskfy_providers::py::json_str(s)
}

impl SwingPlan {
    /// `plan_hash`: SHA-256 of `json.dumps({"as_of", "gate", "tier", "lines"}, sort_keys=True,
    /// separators=(",", ":"))`, each line `asdict` with `Decimal` → `str`.
    #[must_use]
    pub fn plan_hash(&self) -> String {
        let opt_dec =
            |d: Option<Decimal>| d.map_or_else(|| "null".to_owned(), |v| json_str(&py_dec(v)));
        let lines: Vec<String> = self
            .lines
            .iter()
            .map(|l| {
                // sort_keys: kind, note, position_id, position_value, quantity, risk_inr,
                // setup, stop, symbol, trail, trigger.
                format!(
                    "{{\"kind\":{},\"note\":{},\"position_id\":{},\"position_value\":{},\
                     \"quantity\":{},\"risk_inr\":{},\"setup\":{},\"stop\":{},\"symbol\":{},\
                     \"trail\":{},\"trigger\":{}}}",
                    json_str(kind_str(l.kind)),
                    json_str(&l.note),
                    l.position_id
                        .map_or_else(|| "null".to_owned(), |p| p.to_string()),
                    json_str(&py_dec(l.position_value)),
                    l.quantity,
                    json_str(&py_dec(l.risk_inr)),
                    l.setup
                        .map_or_else(|| "null".to_owned(), |s| json_str(s.as_str())),
                    opt_dec(l.stop),
                    json_str(&l.symbol),
                    l.trail
                        .map_or_else(|| "null".to_owned(), |t| json_str(trail_str(t))),
                    opt_dec(l.trigger),
                )
            })
            .collect();
        let payload = format!(
            "{{\"as_of\":{},\"gate\":{},\"lines\":[{}],\"tier\":{}}}",
            json_str(&self.as_of.format("%Y-%m-%d").to_string()),
            json_str(gate_str(self.gate)),
            lines.join(","),
            self.tier.level,
        );
        hex(&Sha256::digest(payload.as_bytes()))
    }
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(out, "{b:02x}");
    }
    out
}

// ---------------------------------------------------------------------------------------------
// journal.py, sizing.r_multiple
// ---------------------------------------------------------------------------------------------

/// `exit_average`: the share-weighted average of `(quantity, price)` exit fills, to 2 places.
///
/// # Errors
/// No exit quantity.
pub fn exit_average(fills: &[(i64, Decimal)]) -> Result<Decimal, RuleError> {
    let total: i64 = fills.iter().map(|(q, _)| q).sum();
    if total <= 0 {
        return Err(RuleError("no exit quantity".to_owned()));
    }
    let sum = fills
        .iter()
        .fold(Decimal::ZERO, |a, (q, p)| a + Decimal::from(*q) * p);
    Ok(q2(sum / Decimal::from(total)))
}

/// `ClosedTrade.pnl_inr`: `((exit_avg - entry) × quantity).quantize(0.01)`.
#[must_use]
pub fn closed_pnl(entry: Decimal, exit_avg: Decimal, quantity: i64) -> Decimal {
    q2((exit_avg - entry) * Decimal::from(quantity))
}

/// `r_multiple`: how many initial risks the exit realised.
///
/// # Errors
/// A stop not below the entry.
pub fn r_multiple(
    entry: Decimal,
    stop: Decimal,
    exit_price: Decimal,
) -> Result<Decimal, RuleError> {
    let distance = entry - stop;
    if distance <= Decimal::ZERO {
        return Err(RuleError(format!(
            "stop {} must be below entry {}",
            py_dec(stop),
            py_dec(entry)
        )));
    }
    Ok(q2((exit_price - entry) / distance))
}

// ---------------------------------------------------------------------------------------------
// opening_range.py
// ---------------------------------------------------------------------------------------------

/// `Candle`: one intraday bar, `start` exchange-local and naive.
#[derive(Debug, Clone, PartialEq)]
pub struct Candle {
    /// The bar's opening time.
    pub start: NaiveDateTime,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
    /// Volume.
    pub volume: i64,
}

/// `opening_range`: the high/low of `[open, open + window)`; complete once a candle at or after
/// the window's end exists.
///
/// # Errors
/// A window the config does not name.
pub fn opening_range(
    candles: &[Candle],
    day: NaiveDate,
    window_minutes: i64,
    config: &OpeningRangeConfig,
) -> Result<OpeningRange, RuleError> {
    if !config.windows_minutes.contains(&window_minutes) {
        return Err(RuleError(format!(
            "window {window_minutes} is not one of {} (docs/swing/04 §7)",
            py_int_list(&config.windows_minutes)
        )));
    }
    let start = session_open(day, config);
    let end = start + chrono::Duration::minutes(window_minutes);
    let inside: Vec<&Candle> = candles
        .iter()
        .filter(|c| start <= c.start && c.start < end)
        .collect();
    let complete = candles.iter().any(|c| c.start >= end);
    let (Some(first), true) = (inside.first(), !inside.is_empty()) else {
        return Ok(OpeningRange {
            high: Decimal::ZERO,
            low: Decimal::ZERO,
            window_minutes,
            complete: false,
            candles: 0,
        });
    };
    // Python's max()/min(): the first of equal values wins.
    let mut high = first.high;
    let mut low = first.low;
    for c in &inside {
        if c.high > high {
            high = c.high;
        }
        if c.low < low {
            low = c.low;
        }
    }
    Ok(OpeningRange {
        high,
        low,
        window_minutes,
        complete,
        candles: i64::try_from(inside.len()).unwrap_or(i64::MAX),
    })
}

/// The session open on `day` (naive, exchange-local).
#[must_use]
pub fn session_open(day: NaiveDate, config: &OpeningRangeConfig) -> NaiveDateTime {
    let (hour, minute) = config.session_open;
    day.and_hms_opt(
        u32::try_from(hour).unwrap_or(9),
        u32::try_from(minute).unwrap_or(15),
        0,
    )
    .unwrap_or_default()
}

/// Python's `repr` of a tuple of ints (`(1, 5, 60)`) — what the window refusal prints.
#[must_use]
pub fn py_int_list(values: &[i64]) -> String {
    let parts: Vec<String> = values.iter().map(i64::to_string).collect();
    if parts.len() == 1 {
        format!("({},)", parts.join(""))
    } else {
        format!("({})", parts.join(", "))
    }
}

/// `Decimal(str(value))` for a decimal string.
#[must_use]
pub fn dec(text: &str) -> Option<Decimal> {
    Decimal::from_str(text)
        .or_else(|_| Decimal::from_scientific(text))
        .ok()
}
