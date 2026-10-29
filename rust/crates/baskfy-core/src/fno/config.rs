//! Port of `baskfy_core.fno.config` (`decile-blueprint/packages/core/src/baskfy_core/fno/config.py`).
//!
//! Every number of the FO run, named once (`docs/fno/04-business-rules.md`). Each default is the
//! number printed there; each bound is the range printed beside it. Python validates a config in
//! `__post_init__`; Rust deserialises it field by field (a missing field takes its default, as
//! the dumper's `cfg` does) and [`validate`](F1Config::validate) raises the same `ValueError`.
//!
//! **What is deliberately absent**, exactly as in Python: an auto-execute field for any sleeve
//! (F3's `BASKFY_FNO_F3_AUTO_EXIT` is the desk's env, never a config field), lot sizes, strike
//! steps and expiry dates (the NFO master), and the env ceilings as config fields
//! ([`FnoCeilings`] carries `02`'s defaults so the pure core can be called with them).
//!
//! This file also carries the fno lane's error type ([`FnoError`]: one variant per Python
//! exception class, message verbatim) and the small pieces of Python's `decimal` printing and
//! comparison the fno ports share ([`py`]).
//!
//! Goldens: `L1/fno/config.{construct,group_of,f1_sleeve_for,f3_sleeve_for,
//! FnoConfig.risk_per_trade_pct,ceiling_violations}.case_NNN.json`.

use chrono::{NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::CoreError;

// =============================================================================================
// Errors
// =============================================================================================

/// What the fno core raises — Python's exception class and its message, verbatim.
#[derive(Debug, thiserror::Error)]
pub enum FnoError {
    /// `ValueError`.
    #[error("{0}")]
    Value(String),
    /// `KeyError` (the message is the missing key, as `exc.args[0]`).
    #[error("{0}")]
    Key(String),
    /// `TypeError`.
    #[error("{0}")]
    Type(String),
    /// `baskfy_core.fno.journal.PooledRows` (a `ValueError` subclass): rows of more than one
    /// pool handed to one figure.
    #[error("{0}")]
    PooledRows(String),
    /// A decimal operation Python's default context refuses (`decimal.InvalidOperation`,
    /// `DivisionByZero`), or a value `rust_decimal` cannot hold on the way out.
    #[error("{0}")]
    Decimal(String),
    /// Any other core error (polars, arithmetic overflow).
    #[error(transparent)]
    Core(#[from] CoreError),
}

impl From<polars::error::PolarsError> for FnoError {
    fn from(e: polars::error::PolarsError) -> Self {
        Self::Core(CoreError::Polars(e))
    }
}

impl FnoError {
    /// The Python exception class name (`"ValueError"`, `"KeyError"`, `"PooledRows"`, ...).
    #[must_use]
    pub fn python_type(&self) -> &'static str {
        match self {
            Self::Value(_) => "ValueError",
            Self::Key(_) => "KeyError",
            Self::Type(_) => "TypeError",
            Self::PooledRows(_) => "PooledRows",
            Self::Decimal(_) => "InvalidOperation",
            Self::Core(_) => "CoreError",
        }
    }

    /// The exception's message (`str(exc)`, or the key for a `KeyError`).
    #[must_use]
    pub fn message(&self) -> String {
        self.to_string()
    }
}

/// `Result` with [`FnoError`].
pub type FnoResult<T> = Result<T, FnoError>;

fn value_error<T>(message: impl Into<String>) -> FnoResult<T> {
    Err(FnoError::Value(message.into()))
}

/// Python `decimal` printing, division and comparison, as the fno ports need them.
pub mod py {
    use rust_decimal::Decimal;

    use super::{FnoError, FnoResult};
    use crate::decimal::{Rounding, WideDecimal};

    fn wide_error(e: &crate::decimal::DecimalError) -> FnoError {
        FnoError::Decimal(e.to_string())
    }

    /// `str(d)` — Python's to-scientific-string (`"1.0"`, `"-0.6"`, `"1E-7"`).
    #[must_use]
    pub fn str(d: Decimal) -> String {
        WideDecimal::from(d).to_sci_string()
    }

    /// `f"{d:.2f}"` — `Decimal.__format__` rounds with the context's `ROUND_HALF_EVEN`.
    #[must_use]
    pub fn fmt_2f(d: Decimal) -> String {
        WideDecimal::from(d)
            .quantize(-2, Rounding::HalfEven)
            .map_or_else(|_| d.to_string(), |q| q.to_string())
    }

    /// `a / b` in Python's default context (28 significant digits, half even, the ideal
    /// exponent when exact).
    ///
    /// # Errors
    /// Division by zero, or a quotient `rust_decimal` cannot hold (scale above 28).
    pub fn div(a: Decimal, b: Decimal) -> FnoResult<Decimal> {
        let q = WideDecimal::from(a)
            .div(&WideDecimal::from(b))
            .map_err(|e| wide_error(&e))?;
        q.try_to_decimal().map_err(|e| wide_error(&e))
    }

    /// `a / b` in Python's default context, kept exact (a quotient below 0.1 prints at scale
    /// 29+, which `rust_decimal` cannot hold; DECISIONS-RS R1.3).
    ///
    /// # Errors
    /// Division by zero.
    pub fn div_wide(a: Decimal, b: Decimal) -> FnoResult<WideDecimal> {
        WideDecimal::from(a)
            .div(&WideDecimal::from(b))
            .map_err(|e| wide_error(&e))
    }

    /// `(a / b).quantize(Decimal(10) ** exp, rounding=mode)` — the 28-digit quotient first,
    /// then the quantize, as Python computes it.
    ///
    /// # Errors
    /// Division by zero, or a result `rust_decimal` cannot hold.
    pub fn div_quantize(a: Decimal, b: Decimal, exp: i64, mode: Rounding) -> FnoResult<Decimal> {
        let q = div_wide(a, b)?
            .quantize(exp, mode)
            .map_err(|e| wide_error(&e))?;
        q.try_to_decimal().map_err(|e| wide_error(&e))
    }

    fn narrow(w: &WideDecimal) -> FnoResult<Decimal> {
        w.try_to_decimal().map_err(|e| wide_error(&e))
    }

    /// `a * b` in Python's default context: the exponent is the sum of the operands' (so
    /// `Decimal(0) * Decimal("1.0")` is `0.0`), a zero carries the XOR of the signs, and the
    /// product rounds to 28 significant digits.
    ///
    /// # Errors
    /// A product `rust_decimal` cannot hold.
    pub fn mul(a: Decimal, b: Decimal) -> FnoResult<Decimal> {
        narrow(&WideDecimal::from(a).mul(&WideDecimal::from(b)))
    }

    /// `a + b` in Python's default context (the smaller exponent; a zero sum is negative only
    /// when both operands are).
    ///
    /// # Errors
    /// A sum `rust_decimal` cannot hold.
    pub fn add(a: Decimal, b: Decimal) -> FnoResult<Decimal> {
        narrow(&WideDecimal::from(a).add(&WideDecimal::from(b)))
    }

    /// `a - b` in Python's default context.
    ///
    /// # Errors
    /// A difference `rust_decimal` cannot hold.
    pub fn sub(a: Decimal, b: Decimal) -> FnoResult<Decimal> {
        narrow(&WideDecimal::from(a).sub(&WideDecimal::from(b)))
    }

    /// `max(a, b)`: the first argument unless the second is strictly greater (Python keeps the
    /// first of equals, and with it its scale: `max(Decimal("0.05"), Decimal("0.050"))` is
    /// `0.05`).
    #[must_use]
    pub fn max(a: Decimal, b: Decimal) -> Decimal {
        if b > a { b } else { a }
    }

    /// `min(a, b)`: the first argument unless the second is strictly smaller.
    #[must_use]
    pub fn min(a: Decimal, b: Decimal) -> Decimal {
        if b < a { b } else { a }
    }

    /// `repr(s)` of a Python `str` (single quotes unless the text holds one and no double).
    #[must_use]
    pub fn repr_str(s: &str) -> String {
        let quote = if s.contains('\'') && !s.contains('"') {
            '"'
        } else {
            '\''
        };
        let mut out = String::with_capacity(s.len() + 2);
        out.push(quote);
        for c in s.chars() {
            match c {
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                c if c == quote => {
                    out.push('\\');
                    out.push(c);
                }
                c => out.push(c),
            }
        }
        out.push(quote);
        out
    }

    /// `repr(tuple_of_str)`: `('NIFTY', 'BANKNIFTY')`, `('BANKNIFTY',)`, `()`.
    #[must_use]
    pub fn repr_tuple(items: &[String]) -> String {
        match items {
            [] => "()".to_owned(),
            [one] => format!("({},)", repr_str(one)),
            many => format!(
                "({})",
                many.iter()
                    .map(|s| repr_str(s))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        }
    }
}

// =============================================================================================
// Seeds and enums
// =============================================================================================

/// F1's capital, ₹25 lakh (Maulik, M.2, 25 Sep 2026; `04` §3).
///
/// Ports `baskfy_core.fno.config.F1_SEED_CAPITAL_INR`.
#[must_use]
pub fn f1_seed_capital_inr() -> Decimal {
    Decimal::new(2_500_000, 0)
}

/// F2's capital, ₹0 (`01` §1b): paper runs one lot; live refuses.
///
/// Ports `baskfy_core.fno.config.F2_SEED_CAPITAL_INR`.
#[must_use]
pub fn f2_seed_capital_inr() -> Decimal {
    Decimal::ZERO
}

/// The first UDiFF bhavcopy session, 8 Jul 2024: the file carries the underlying's price from
/// here (`04` §4).
///
/// Ports `baskfy_core.fno.config.UDIFF_FIRST_SESSION`.
#[must_use]
pub fn udiff_first_session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2024, 7, 8).unwrap_or_default()
}

/// The `fo_sleeve` codes (`01` §4).
///
/// Ports `baskfy_core.fno.config.FoSleeve`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum FoSleeve {
    /// F1 on NIFTY.
    #[serde(rename = "F1N")]
    F1N,
    /// F1 on BANKNIFTY.
    #[serde(rename = "F1B")]
    F1B,
    /// F2, the stock-futures breakout long.
    #[serde(rename = "F2")]
    F2,
    /// F3 on NIFTY (weekly), the directional index credit spread (M.5).
    #[serde(rename = "F3N")]
    F3N,
    /// F3 on BANKNIFTY (monthly).
    #[serde(rename = "F3B")]
    F3B,
}

impl FoSleeve {
    /// Every sleeve, in Python's declaration order.
    pub const ALL: [Self; 5] = [Self::F1N, Self::F1B, Self::F2, Self::F3N, Self::F3B];

    /// The enum's value (`"F1N"`).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::F1N => "F1N",
            Self::F1B => "F1B",
            Self::F2 => "F2",
            Self::F3N => "F3N",
            Self::F3B => "F3B",
        }
    }
}

/// How flags group the sleeves (`01` §4): `F1N` and `F1B` are one flag, `F1`.
///
/// Ports `baskfy_core.fno.config.FoSleeveGroup`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum FoSleeveGroup {
    /// `F1`.
    #[serde(rename = "F1")]
    F1,
    /// `F2`.
    #[serde(rename = "F2")]
    F2,
    /// `F3`.
    #[serde(rename = "F3")]
    F3,
}

impl FoSleeveGroup {
    /// The enum's value (`"F1"`).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::F1 => "F1",
            Self::F2 => "F2",
            Self::F3 => "F3",
        }
    }
}

/// The flag group of a sleeve.
///
/// Ports `baskfy_core.fno.config.group_of`.
#[must_use]
pub fn group_of(sleeve: FoSleeve) -> FoSleeveGroup {
    match sleeve {
        FoSleeve::F2 => FoSleeveGroup::F2,
        FoSleeve::F3N | FoSleeve::F3B => FoSleeveGroup::F3,
        FoSleeve::F1N | FoSleeve::F1B => FoSleeveGroup::F1,
    }
}

/// `F1N` for NIFTY, `F1B` for BANKNIFTY; anything else is not an F1 underlying.
///
/// Ports `baskfy_core.fno.config.f1_sleeve_for`.
///
/// # Errors
/// `ValueError` for any other underlying (case-sensitive, as in Python).
pub fn f1_sleeve_for(underlying: &str) -> FnoResult<FoSleeve> {
    match underlying {
        "NIFTY" => Ok(FoSleeve::F1N),
        "BANKNIFTY" => Ok(FoSleeve::F1B),
        other => value_error(format!(
            "{} is not an F1 underlying (04 §1)",
            py::repr_str(other)
        )),
    }
}

/// `F3N` for NIFTY (weekly), `F3B` for BANKNIFTY (monthly); M.5's third answer.
///
/// Ports `baskfy_core.fno.config.f3_sleeve_for`.
///
/// # Errors
/// `ValueError` for any other underlying.
pub fn f3_sleeve_for(underlying: &str) -> FnoResult<FoSleeve> {
    match underlying {
        "NIFTY" => Ok(FoSleeve::F3N),
        "BANKNIFTY" => Ok(FoSleeve::F3B),
        other => value_error(format!(
            "{} is not an F3 underlying (04 §11)",
            py::repr_str(other)
        )),
    }
}

/// `fo_plan.structure` (`03` §4).
///
/// Ports `baskfy_core.fno.config.Structure`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Structure {
    /// F1's iron condor.
    IronCondor,
    /// F2's long future.
    Future,
    /// F3's short option with its far wing.
    CreditSpread,
}

/// `fo_plan.kind` (`03` §4).
///
/// Ports `baskfy_core.fno.config.PlanKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PlanKind {
    /// An entry.
    Entry,
    /// An exit.
    Exit,
    /// F2's calendar roll.
    Roll,
    /// F3's pyramid: more lots onto the open spread.
    Add,
}

/// `fo_scan.state` (`04` §8). The last four are F2's only.
///
/// Ports `baskfy_core.fno.config.ScanState`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ScanState {
    /// Not an entry day.
    NotEntryDay,
    /// A candidate.
    Candidate,
    /// Skipped for an event.
    SkippedEvent,
    /// Paused (`04` §7).
    Paused,
    /// A position is already open.
    OpenPosition,
    /// No data.
    NoData,
    /// No signal (F2).
    NoSignal,
    /// In the F&O ban list (F2).
    BlockedBan,
    /// Blocked by the regime (F2).
    BlockedRegime,
    /// Blocked by capacity (F2).
    BlockedCapacity,
}

/// `fo_plan.state` (`04` §8). Every non-happy state carries its reason in words.
///
/// Ports `baskfy_core.fno.config.PlanState`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PlanState {
    /// Issued.
    Issued,
    /// Confirmed.
    Confirmed,
    /// Filling.
    Filling,
    /// Open.
    Open,
    /// Exiting.
    Exiting,
    /// Closed.
    Closed,
    /// The window passed unconfirmed.
    Lapsed,
    /// Refused: the structure.
    RejectedStructure,
    /// Refused: liquidity.
    RejectedLiquidity,
    /// Refused: zero lots.
    RejectedSize,
    /// Refused: margin.
    RejectedMargin,
    /// Refused: costs above the cap.
    RejectedCost,
    /// Live at ₹0 sleeve capital.
    NoSleeveCapital,
    /// Refused: a pause stands (`04` §7, FO10.7).
    RejectedPaused,
    /// An entry abandoned before it was a structure.
    AbandonedPartial,
}

// =============================================================================================
// Configs
// =============================================================================================

/// `_between`'s operands: printed as Python prints an `int` or a `Decimal`.
trait Bound: PartialOrd + Copy {
    fn show(self) -> String;
}

impl Bound for i64 {
    fn show(self) -> String {
        self.to_string()
    }
}

impl Bound for Decimal {
    fn show(self) -> String {
        py::str(self)
    }
}

fn between<T: Bound>(name: &str, value: T, low: T, high: T) -> FnoResult<()> {
    if low <= value && value <= high {
        Ok(())
    } else {
        value_error(format!(
            "{name} = {} is outside its bounds {}-{} (04)",
            value.show(),
            low.show(),
            high.show()
        ))
    }
}

fn dec(units: i64, scale: u32) -> Decimal {
    Decimal::new(units, scale)
}

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap_or_default()
}

/// `02` Track B "Ceilings": the system-only env bounds, as the pure core receives them.
///
/// Ports `baskfy_core.fno.config.FnoCeilings`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct FnoCeilings {
    /// `BASKFY_FNO_RISK_PER_TRADE_INR_MAX`.
    pub risk_per_trade_inr_max: Decimal,
    /// `BASKFY_FNO_RISK_PCT_MAX`.
    pub risk_pct_max: Decimal,
    /// `BASKFY_FNO_MAX_OPEN_POSITIONS_MAX`.
    pub max_open_positions_max: i64,
    /// `BASKFY_FNO_MAX_PER_UNDERLYING_MAX`.
    pub max_per_underlying_max: i64,
    /// `BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX`.
    pub book_monthly_loss_inr_max: Decimal,
}

impl Default for FnoCeilings {
    fn default() -> Self {
        Self {
            risk_per_trade_inr_max: dec(25_000, 0),
            risk_pct_max: dec(10, 1),
            max_open_positions_max: 10,
            max_per_underlying_max: 1,
            book_monthly_loss_inr_max: dec(75_000, 0),
        }
    }
}

impl FnoCeilings {
    /// `__post_init__`'s checks.
    ///
    /// # Errors
    /// `ValueError` with Python's message.
    pub fn validate(&self) -> FnoResult<()> {
        if self.risk_per_trade_inr_max <= Decimal::ZERO
            || self.book_monthly_loss_inr_max <= Decimal::ZERO
        {
            return value_error("a rupee ceiling must be positive");
        }
        if self.risk_pct_max <= Decimal::ZERO || self.max_open_positions_max < 1 {
            return value_error("a ceiling of zero is a flag, not a ceiling");
        }
        if self.max_per_underlying_max < 1 {
            return value_error("max per underlying must be at least one");
        }
        Ok(())
    }
}

/// Fields every FO sleeve shares (`04` §1, §3).
///
/// Ports `baskfy_core.fno.config.CommonConfig`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CommonConfig {
    /// `fo_hard_exit_before_expiry`: flat by 15:00 on session `E - n`. Never zero.
    pub hard_exit_before_expiry: i64,
    /// The hard exit's time of day.
    pub hard_exit_time: NaiveTime,
    /// `fo_max_lots`: default 2.
    pub max_lots: i64,
    /// `fo_max_lots`' ceiling, 10.
    pub max_lots_ceiling: i64,
    /// The plan's lifetime after issue, minutes.
    pub plan_ttl_minutes: i64,
    /// The market's open.
    pub market_open: NaiveTime,
    /// The last moment an entry window may end.
    pub latest_window_end: NaiveTime,
}

impl Default for CommonConfig {
    fn default() -> Self {
        Self {
            hard_exit_before_expiry: 1,
            hard_exit_time: hm(15, 0),
            max_lots: 2,
            max_lots_ceiling: 10,
            plan_ttl_minutes: 30,
            market_open: hm(9, 15),
            latest_window_end: hm(15, 0),
        }
    }
}

impl CommonConfig {
    /// `__post_init__`'s checks.
    ///
    /// # Errors
    /// `ValueError` with Python's message.
    pub fn validate(&self) -> FnoResult<()> {
        between(
            "fo_hard_exit_before_expiry",
            self.hard_exit_before_expiry,
            1,
            5,
        )?;
        between("fo_max_lots", self.max_lots, 1, self.max_lots_ceiling)?;
        between("fo_max_lots_ceiling", self.max_lots_ceiling, 1, 10)?;
        if self.plan_ttl_minutes <= 0 {
            return value_error("a plan must live for a positive number of minutes");
        }
        Ok(())
    }
}

fn window_ok(plan_time: NaiveTime, window_end: NaiveTime) -> bool {
    hm(9, 15) <= plan_time && plan_time < window_end && window_end <= hm(15, 0)
}

/// F1, the index monthly condor (`04` §1-§3, §7).
///
/// Ports `baskfy_core.fno.config.F1Config`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct F1Config {
    /// The underlyings F1 trades.
    pub underlyings: Vec<String>,
    /// The index underlyings the pack admits.
    pub index_underlyings: Vec<String>,
    /// Sessions before the monthly that F1 enters.
    pub entry_sessions_before: i64,
    /// The plan's time.
    pub plan_time: NaiveTime,
    /// The entry window's end.
    pub entry_window_end: NaiveTime,
    /// Loss close at this multiple of the credit.
    pub loss_close_mult: Decimal,
    /// Profit take at this percent of the credit.
    pub profit_take_pct: Decimal,
    /// Short strikes at this many sigma.
    pub short_sigma: Decimal,
    /// Wings this many sigma beyond the shorts.
    pub wing_sigma: Decimal,
    /// Minimum open interest of a short leg, in lots.
    pub min_short_oi_lots: i64,
    /// Maximum bid-ask spread, percent of mid.
    pub max_spread_pct: Decimal,
    /// Open condors per underlying.
    pub max_open_per_underlying: i64,
    /// `f1_max_cost_share`: the round trip's cap as a share of the credit, percent.
    pub max_cost_share_pct: Decimal,
    /// Risk per trade, percent of sleeve capital.
    pub risk_per_trade_pct: Decimal,
    /// Research slippage per leg per crossing, percent of premium.
    pub slippage_pct: Decimal,
    /// Research slippage floor per leg per crossing, ₹ per unit.
    pub slippage_min_inr: Decimal,
    /// `04` §7: pause after this many consecutive trades at or below `pause_loss_r`.
    pub pause_consecutive: i64,
    /// The pause's R threshold.
    pub pause_loss_r: Decimal,
}

fn index_pair() -> Vec<String> {
    vec!["NIFTY".to_owned(), "BANKNIFTY".to_owned()]
}

impl Default for F1Config {
    fn default() -> Self {
        Self {
            underlyings: index_pair(),
            index_underlyings: index_pair(),
            entry_sessions_before: 15,
            plan_time: hm(9, 20),
            entry_window_end: hm(10, 30),
            loss_close_mult: dec(15, 1),
            profit_take_pct: dec(50, 0),
            short_sigma: dec(10, 1),
            wing_sigma: dec(5, 1),
            min_short_oi_lots: 500,
            max_spread_pct: dec(5, 0),
            max_open_per_underlying: 1,
            max_cost_share_pct: dec(25, 0),
            risk_per_trade_pct: dec(10, 1),
            slippage_pct: dec(5, 1),
            slippage_min_inr: dec(5, 2),
            pause_consecutive: 3,
            pause_loss_r: dec(-6, 1),
        }
    }
}

impl F1Config {
    /// `__post_init__`'s checks, in Python's order.
    ///
    /// # Errors
    /// `ValueError` with Python's message.
    pub fn validate(&self) -> FnoResult<()> {
        let subset = self
            .underlyings
            .iter()
            .all(|u| self.index_underlyings.contains(u));
        if self.underlyings.is_empty() || !subset {
            return value_error(format!(
                "f1_underlyings {} must be a non-empty subset of {} (04 §1)",
                py::repr_tuple(&self.underlyings),
                py::repr_tuple(&self.index_underlyings)
            ));
        }
        between(
            "f1_entry_sessions_before",
            self.entry_sessions_before,
            10,
            20,
        )?;
        if !window_ok(self.plan_time, self.entry_window_end) {
            return value_error("f1_entry_window must sit inside 09:15-15:00 (04 §1)");
        }
        between(
            "f1_loss_close_mult",
            self.loss_close_mult,
            dec(1, 0),
            dec(2, 0),
        )?;
        between(
            "f1_profit_take_pct",
            self.profit_take_pct,
            dec(30, 0),
            dec(80, 0),
        )?;
        between("f1_short_sigma", self.short_sigma, dec(75, 2), dec(15, 1))?;
        between("f1_wing_sigma", self.wing_sigma, dec(25, 2), dec(10, 1))?;
        if self.min_short_oi_lots < 0 {
            return value_error("f1_min_short_oi_lots cannot be negative");
        }
        if !(Decimal::ZERO < self.max_spread_pct && self.max_spread_pct <= dec(100, 0)) {
            return value_error("f1_max_spread_pct must be a positive percent");
        }
        between(
            "f1_max_open_per_underlying",
            self.max_open_per_underlying,
            1,
            1,
        )?;
        if !(Decimal::ZERO < self.max_cost_share_pct && self.max_cost_share_pct <= dec(100, 0)) {
            return value_error("f1_max_cost_share must be a positive percent");
        }
        if self.risk_per_trade_pct <= Decimal::ZERO {
            return value_error("risk_per_trade_pct must be positive");
        }
        if self.slippage_pct < Decimal::ZERO || self.slippage_min_inr < Decimal::ZERO {
            return value_error("slippage cannot be negative");
        }
        if self.pause_consecutive < 1 || self.pause_loss_r >= Decimal::ZERO {
            return value_error("the F1 pause needs a positive count and a negative R");
        }
        Ok(())
    }
}

/// F2, stock-futures breakout long (`04` §10, §7).
///
/// Ports `baskfy_core.fno.config.F2Config`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct F2Config {
    /// The universe: the top futures by turnover, percent.
    pub universe_turnover_pct: Decimal,
    /// Turnover window, sessions.
    pub turnover_sessions: i64,
    /// Breakout window, sessions.
    pub breakout_sessions: i64,
    /// Trend window, sessions.
    pub trend_sessions: i64,
    /// The initial stop, in ATRs.
    pub stop_atr: Decimal,
    /// ATR window, sessions.
    pub atr_sessions: i64,
    /// Whether the stop trails.
    pub trail: bool,
    /// The time exit, sessions.
    pub max_sessions: i64,
    /// Roll this many sessions before expiry.
    pub roll_before_expiry: i64,
    /// Open positions.
    pub max_open: i64,
    /// Open positions per industry.
    pub max_per_industry: i64,
    /// `01` §1b: buy the next month if the near one expires within this many sessions.
    pub near_month_min_sessions: i64,
    /// `04` §7: pause new entries for the rest of the month at this closed-R total.
    pub month_pause_r: Decimal,
    /// Risk per trade, percent of sleeve capital.
    pub risk_per_trade_pct: Decimal,
}

impl Default for F2Config {
    fn default() -> Self {
        Self {
            universe_turnover_pct: dec(60, 0),
            turnover_sessions: 20,
            breakout_sessions: 20,
            trend_sessions: 50,
            stop_atr: dec(30, 1),
            atr_sessions: 14,
            trail: true,
            max_sessions: 40,
            roll_before_expiry: 1,
            max_open: 5,
            max_per_industry: 2,
            near_month_min_sessions: 3,
            month_pause_r: dec(-6, 0),
            risk_per_trade_pct: dec(10, 1),
        }
    }
}

impl F2Config {
    /// `__post_init__`'s checks, in Python's order.
    ///
    /// # Errors
    /// `ValueError` with Python's message.
    pub fn validate(&self) -> FnoResult<()> {
        between(
            "f2_universe_turnover_pct",
            self.universe_turnover_pct,
            dec(20, 0),
            dec(100, 0),
        )?;
        between("f2_breakout_sessions", self.breakout_sessions, 10, 60)?;
        between("f2_trend_sessions", self.trend_sessions, 20, 200)?;
        between("f2_stop_atr", self.stop_atr, dec(15, 1), dec(50, 1))?;
        between("f2_max_sessions", self.max_sessions, 10, 60)?;
        between("f2_roll_before_expiry", self.roll_before_expiry, 1, 3)?;
        between("f2_max_open", self.max_open, 1, 10)?;
        if self.max_per_industry < 1 || self.atr_sessions < 1 || self.turnover_sessions < 1 {
            return value_error("window lengths and the industry cap must be positive");
        }
        if self.near_month_min_sessions < 0 {
            return value_error("near_month_min_sessions cannot be negative");
        }
        if self.month_pause_r >= Decimal::ZERO {
            return value_error("f2 month pause must be a negative R");
        }
        if self.risk_per_trade_pct <= Decimal::ZERO {
            return value_error("risk_per_trade_pct must be positive");
        }
        Ok(())
    }
}

/// F3, the directional index credit spread (`04` §11; Maulik, M.5, 28 Sep 2026).
///
/// The auto-exit switch is **not** a field here: `BASKFY_FNO_F3_AUTO_EXIT` is the desk's env.
///
/// Ports `baskfy_core.fno.config.F3Config`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct F3Config {
    /// The underlyings F3 trades.
    pub underlyings: Vec<String>,
    /// Pivot lookback, sessions.
    pub pivot_lookback: i64,
    /// Pivot width, sessions each side.
    pub pivot_width: i64,
    /// Trend window, sessions.
    pub trend_sessions: i64,
    /// The weekly window, sessions.
    pub weekly_sessions: i64,
    /// Confirmation bars.
    pub confirm_bars: i64,
    /// The short strike's distance, percent.
    pub distance_pct: Decimal,
    /// The wing's distance, percent.
    pub wing_pct: Decimal,
    /// The short premium's floor, ₹.
    pub short_premium_min_inr: Decimal,
    /// Minimum sessions to a weekly expiry.
    pub min_sessions_weekly: i64,
    /// Minimum sessions to a monthly expiry.
    pub min_sessions_monthly: i64,
    /// The entry's share of the full size, percent.
    pub entry_share_pct: Decimal,
    /// The full size's share, percent.
    pub full_share_pct: Decimal,
    /// An add needs the spread working by this percent.
    pub add_working_pct: Decimal,
    /// The decay target, percent of the credit.
    pub decay_target_pct: Decimal,
    /// The loss cut, multiple of the credit.
    pub loss_cut_mult: Decimal,
    /// The level's buffer, percent.
    pub level_buffer_pct: Decimal,
    /// Open spreads per underlying.
    pub max_open_per_underlying: i64,
    /// Risk per trade, percent of sleeve capital.
    pub risk_per_trade_pct: Decimal,
    /// F1's entry window, shared: plan at 09:20.
    pub plan_time: NaiveTime,
    /// Confirmable to 10:30.
    pub entry_window_end: NaiveTime,
    /// Research slippage per leg per crossing, percent of premium.
    pub slippage_pct: Decimal,
    /// Research slippage floor per leg per crossing, ₹ per unit.
    pub slippage_min_inr: Decimal,
}

impl Default for F3Config {
    fn default() -> Self {
        Self {
            underlyings: index_pair(),
            pivot_lookback: 60,
            pivot_width: 3,
            trend_sessions: 20,
            weekly_sessions: 5,
            confirm_bars: 10,
            distance_pct: dec(10, 1),
            wing_pct: dec(20, 1),
            short_premium_min_inr: dec(10, 0),
            min_sessions_weekly: 2,
            min_sessions_monthly: 5,
            entry_share_pct: dec(25, 0),
            full_share_pct: dec(50, 0),
            add_working_pct: dec(20, 0),
            decay_target_pct: dec(80, 0),
            loss_cut_mult: dec(20, 1),
            level_buffer_pct: dec(10, 2),
            max_open_per_underlying: 1,
            risk_per_trade_pct: dec(10, 1),
            plan_time: hm(9, 20),
            entry_window_end: hm(10, 30),
            slippage_pct: dec(5, 1),
            slippage_min_inr: dec(5, 2),
        }
    }
}

impl F3Config {
    /// `__post_init__`'s checks, in Python's order.
    ///
    /// # Errors
    /// `ValueError` with Python's message.
    pub fn validate(&self) -> FnoResult<()> {
        let allowed = ["NIFTY", "BANKNIFTY"];
        if self.underlyings.is_empty()
            || !self
                .underlyings
                .iter()
                .all(|u| allowed.contains(&u.as_str()))
        {
            return value_error(
                "f3_underlyings must be a non-empty subset of NIFTY, BANKNIFTY (04 §11)",
            );
        }
        between("f3_pivot_lookback", self.pivot_lookback, 20, 250)?;
        between("f3_pivot_width", self.pivot_width, 1, 10)?;
        between("f3_trend_sessions", self.trend_sessions, 5, 100)?;
        between("f3_weekly_sessions", self.weekly_sessions, 3, 10)?;
        between("f3_confirm_bars", self.confirm_bars, 3, 40)?;
        between("f3_distance_pct", self.distance_pct, dec(25, 2), dec(30, 1))?;
        between("f3_wing_pct", self.wing_pct, dec(5, 1), dec(50, 1))?;
        between(
            "f3_short_premium_min_inr",
            self.short_premium_min_inr,
            dec(1, 0),
            dec(100, 0),
        )?;
        between("f3_min_sessions_weekly", self.min_sessions_weekly, 1, 4)?;
        between("f3_min_sessions_monthly", self.min_sessions_monthly, 1, 15)?;
        between(
            "f3_entry_share_pct",
            self.entry_share_pct,
            dec(5, 0),
            dec(50, 0),
        )?;
        between(
            "f3_full_share_pct",
            self.full_share_pct,
            dec(10, 0),
            dec(100, 0),
        )?;
        if self.full_share_pct < self.entry_share_pct {
            return value_error("f3_full_share_pct cannot sit below f3_entry_share_pct");
        }
        between(
            "f3_add_working_pct",
            self.add_working_pct,
            dec(5, 0),
            dec(80, 0),
        )?;
        between(
            "f3_decay_target_pct",
            self.decay_target_pct,
            dec(50, 0),
            dec(95, 0),
        )?;
        between(
            "f3_loss_cut_mult",
            self.loss_cut_mult,
            dec(12, 1),
            dec(40, 1),
        )?;
        between(
            "f3_level_buffer_pct",
            self.level_buffer_pct,
            dec(0, 0),
            dec(10, 1),
        )?;
        between(
            "f3_max_open_per_underlying",
            self.max_open_per_underlying,
            1,
            1,
        )?;
        if self.risk_per_trade_pct <= Decimal::ZERO {
            return value_error("risk_per_trade_pct must be positive");
        }
        if !window_ok(self.plan_time, self.entry_window_end) {
            return value_error("f3's entry window must sit inside 09:15-15:00 (04 §1)");
        }
        if self.slippage_pct < Decimal::ZERO || self.slippage_min_inr < Decimal::ZERO {
            return value_error("slippage cannot be negative");
        }
        Ok(())
    }
}

/// `04` §10's futures costs per order, as dated percent fields.
///
/// Ports `baskfy_core.fno.config.FutureCostRates`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct FutureCostRates {
    /// ₹ per order.
    pub brokerage_per_order_inr: Decimal,
    /// STT on the sale, percent.
    pub stt_sell_pct: Decimal,
    /// Exchange transaction charge per side, percent.
    pub exchange_txn_pct: Decimal,
    /// Stamp duty on the buy, percent.
    pub stamp_buy_pct: Decimal,
    /// GST on brokerage and exchange charges, percent.
    pub gst_pct: Decimal,
    /// Slippage per side, percent (until FO3 measures it).
    pub slippage_pct: Decimal,
}

impl Default for FutureCostRates {
    fn default() -> Self {
        Self {
            brokerage_per_order_inr: dec(20, 0),
            stt_sell_pct: dec(5, 2),
            exchange_txn_pct: dec(173, 5),
            stamp_buy_pct: dec(2, 3),
            gst_pct: dec(18, 0),
            slippage_pct: dec(3, 2),
        }
    }
}

/// The desk's `stop_from_vol` band (non-negotiable 4's 8-12 %).
///
/// Ports `baskfy_core.fno.config.StopVolConfig`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct StopVolConfig {
    /// The vol multiple.
    pub vol_mult: f64,
    /// The band's floor.
    pub stop_min: f64,
    /// The band's ceiling.
    pub stop_max: f64,
}

impl Default for StopVolConfig {
    fn default() -> Self {
        Self {
            vol_mult: 2.2,
            stop_min: 0.08,
            stop_max: 0.12,
        }
    }
}

impl StopVolConfig {
    /// `__post_init__`'s check.
    ///
    /// # Errors
    /// `ValueError` with Python's message.
    pub fn validate(&self) -> FnoResult<()> {
        if !(0.0 < self.stop_min && self.stop_min <= self.stop_max && self.stop_max < 1.0) {
            return value_error("the stop band must satisfy 0 < min <= max < 1");
        }
        Ok(())
    }
}

/// `04` §4: the continuous series, the CA flag, ATR, RV, IV and basis.
///
/// Ports `baskfy_core.fno.config.SeriesConfig`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct SeriesConfig {
    /// A held-future move above this ratio is a corporate action.
    pub ca_up_ratio: f64,
    /// ... or below this one.
    pub ca_down_ratio: f64,
    /// Sessions a CA keeps a symbol out of signals, the flagged one included.
    pub ca_exclusion_sessions: i64,
    /// ATR window.
    pub atr_sessions: i64,
    /// RV window.
    pub rv_sessions: i64,
    /// Sessions a year.
    pub annualisation_sessions: i64,
    /// Turnover median window.
    pub turnover_sessions: i64,
    /// The IV monthly needs at least this many sessions left.
    pub iv_min_sessions_left: i64,
    /// Black-76's rate.
    pub iv_rate: f64,
    /// The solver's lower vol bound.
    pub iv_lower: f64,
    /// The solver's upper vol bound.
    pub iv_upper: f64,
    /// The solver's tolerance.
    pub iv_tolerance: f64,
    /// Basis is computed from this session (UDiFF).
    pub basis_from: NaiveDate,
}

impl Default for SeriesConfig {
    fn default() -> Self {
        Self {
            ca_up_ratio: 1.4,
            ca_down_ratio: 0.7,
            ca_exclusion_sessions: 5,
            atr_sessions: 14,
            rv_sessions: 20,
            annualisation_sessions: 252,
            turnover_sessions: 20,
            iv_min_sessions_left: 8,
            iv_rate: 0.0,
            iv_lower: 0.01,
            iv_upper: 5.0,
            iv_tolerance: 1e-6,
            basis_from: udiff_first_session(),
        }
    }
}

impl SeriesConfig {
    /// `__post_init__`'s check.
    ///
    /// # Errors
    /// `ValueError` with Python's message.
    pub fn validate(&self) -> FnoResult<()> {
        if !(0.0 < self.ca_down_ratio && self.ca_down_ratio < 1.0 && 1.0 < self.ca_up_ratio) {
            return value_error("the CA band must straddle 1");
        }
        Ok(())
    }
}

/// `fo_book_config` (`03` §6, `04` §7): the book's own limits, under the ceilings.
///
/// Ports `baskfy_core.fno.config.BookConfig`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct BookConfig {
    /// The monthly book pause, ₹.
    pub monthly_pause_inr: Decimal,
    /// Open positions across the book.
    pub max_open_positions: i64,
    /// Open positions per underlying.
    pub max_per_underlying: i64,
}

impl Default for BookConfig {
    fn default() -> Self {
        Self {
            monthly_pause_inr: dec(75_000, 0),
            max_open_positions: 10,
            max_per_underlying: 1,
        }
    }
}

impl BookConfig {
    /// `__post_init__`'s checks.
    ///
    /// # Errors
    /// `ValueError` with Python's message.
    pub fn validate(&self) -> FnoResult<()> {
        if self.monthly_pause_inr <= Decimal::ZERO || self.max_open_positions < 1 {
            return value_error("the book's limits must be positive");
        }
        if self.max_per_underlying < 1 {
            return value_error("max per underlying must be at least one");
        }
        Ok(())
    }
}

/// The whole contract of `04`.
///
/// Ports `baskfy_core.fno.config.FnoConfig`.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct FnoConfig {
    /// Shared fields.
    pub common: CommonConfig,
    /// F1.
    pub f1: F1Config,
    /// F2.
    pub f2: F2Config,
    /// F3.
    pub f3: F3Config,
    /// F2's futures costs.
    pub future_costs: FutureCostRates,
    /// The stop band.
    pub stop_vol: StopVolConfig,
    /// The series.
    pub series: SeriesConfig,
    /// The book.
    pub book: BookConfig,
}

impl FnoConfig {
    /// Every nested config's `__post_init__`, in the dataclass's field order (the order Python
    /// constructs them in, so the first violation raised is Python's).
    ///
    /// # Errors
    /// `ValueError` with Python's message.
    pub fn validate(&self) -> FnoResult<()> {
        self.common.validate()?;
        self.f1.validate()?;
        self.f2.validate()?;
        self.f3.validate()?;
        self.stop_vol.validate()?;
        self.series.validate()?;
        self.book.validate()
    }

    /// The sleeve's risk per trade, percent: F1's for `F1N`/`F1B`, F3's for `F3N`/`F3B`,
    /// F2's otherwise.
    ///
    /// Ports `baskfy_core.fno.config.FnoConfig.risk_per_trade_pct`.
    #[must_use]
    pub fn risk_per_trade_pct(&self, sleeve: FoSleeve) -> Decimal {
        match group_of(sleeve) {
            FoSleeveGroup::F1 => self.f1.risk_per_trade_pct,
            FoSleeveGroup::F3 => self.f3.risk_per_trade_pct,
            FoSleeveGroup::F2 => self.f2.risk_per_trade_pct,
        }
    }
}

/// Every field that sits above its env ceiling (`02` Track B). Empty means admissible.
///
/// Ports `baskfy_core.fno.config.ceiling_violations`.
#[must_use]
pub fn ceiling_violations(config: &FnoConfig, ceilings: &FnoCeilings) -> Vec<String> {
    let mut found = Vec::new();
    for (name, pct) in [
        ("f1", config.f1.risk_per_trade_pct),
        ("f2", config.f2.risk_per_trade_pct),
        ("f3", config.f3.risk_per_trade_pct),
    ] {
        if pct > ceilings.risk_pct_max {
            found.push(format!(
                "{name}.risk_per_trade_pct {} > BASKFY_FNO_RISK_PCT_MAX {}",
                py::str(pct),
                py::str(ceilings.risk_pct_max)
            ));
        }
    }
    if config.book.monthly_pause_inr > ceilings.book_monthly_loss_inr_max {
        found.push(format!(
            "book.monthly_pause_inr {} > BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX {}",
            py::str(config.book.monthly_pause_inr),
            py::str(ceilings.book_monthly_loss_inr_max)
        ));
    }
    if config.book.max_open_positions > ceilings.max_open_positions_max {
        found.push(format!(
            "book.max_open_positions {} > BASKFY_FNO_MAX_OPEN_POSITIONS_MAX {}",
            config.book.max_open_positions, ceilings.max_open_positions_max
        ));
    }
    if config.book.max_per_underlying > ceilings.max_per_underlying_max {
        found.push(format!(
            "book.max_per_underlying {} > BASKFY_FNO_MAX_PER_UNDERLYING_MAX {}",
            config.book.max_per_underlying, ceilings.max_per_underlying_max
        ));
    }
    if config.f2.max_open > ceilings.max_open_positions_max {
        found.push(format!(
            "f2.max_open {} > BASKFY_FNO_MAX_OPEN_POSITIONS_MAX {}",
            config.f2.max_open, ceilings.max_open_positions_max
        ));
    }
    found
}
