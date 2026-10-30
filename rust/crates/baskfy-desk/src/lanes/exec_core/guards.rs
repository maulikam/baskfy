//! Ports `baskfy_execution.guards` (re-exported by the desk's `app/core/guards.py`): the HARD
//! instrument guards, the lowest layer of the system.
//!
//! Every order, GTT and cancel the gateway handles passes [`assert_tradeable`] before any
//! network call (non-negotiable 7: SGB*/G-sec untouchable, prefix- and series-aware). Option
//! carry is refused by [`assert_not_overnight_option`] (16 Aug 2026: no option is held past the
//! close), and an order carrying a `fo_plan` reference meets the narrower covered-overnight
//! guard [`assert_overnight_option_is_covered`] instead (`docs/fno/02` §1, FO6). The product
//! allow-list [`product_exchange_refusal`] is non-negotiable 5: CNC on NSE/BSE; MIS needs
//! `INTRADAY_ENABLED`; a derivative venue needs `OPTIONS_ENABLED` and admits MIS only — plus
//! the one FO branch (NRML on NFO with `BASKFY_FNO_CARRY_ENABLED` and a `fo_plan`).
//!
//! Every message is Python's, character for character: the desk's tests and the sleeves'
//! reports match on them.

use std::sync::{Arc, LazyLock};

use chrono::NaiveDate;
use regex::Regex;
use rust_decimal::Decimal;

use crate::gateway::FoPlanRef;
use crate::wire::PyValue;

/// Symbols that must NEVER be traded by this system, in any product, any direction.
pub const UNTOUCHABLE_SYMBOLS: [&str; 1] = ["SGBDE31III"];
/// Every Sovereign Gold Bond trades as `SGB*`.
pub const UNTOUCHABLE_PREFIXES: [&str; 1] = ["SGB"];
/// `GB`/`GS` series are G-secs.
pub const UNTOUCHABLE_SERIES: [&str; 2] = ["GB", "GS"];

/// Products that carry a position past the close.
pub const CARRY_PRODUCTS: [&str; 2] = ["NRML", "CNC"];
/// Derivative venues (need `OPTIONS_ENABLED`).
pub const DERIVATIVE_EXCHANGES: [&str; 5] = ["NFO", "BFO", "CDS", "BCD", "MCX"];
/// Cash-equity venues.
pub const CASH_EXCHANGES: [&str; 2] = ["NSE", "BSE"];
/// The one product the FO run may carry past the close (`docs/fno/02` §1).
pub const FO_CARRY_PRODUCT: &str = "NRML";
/// The one venue the FO run may carry on.
pub const FO_CARRY_EXCHANGE: &str = "NFO";
/// `fo_sleeve` codes that trade option structures (F3N/F3B since M.5).
pub const FO_OPTION_SLEEVES: [&str; 4] = ["F1N", "F1B", "F3N", "F3B"];
/// `fo_sleeve` codes that trade stock futures.
pub const FO_FUTURE_SLEEVES: [&str; 1] = ["F2"];
/// Index underlyings: a GTT is admitted for a STOCK future only.
pub const INDEX_UNDERLYINGS: [&str; 8] = [
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "SENSEX",
    "BANKEX",
    "SENSEX50",
];

/// Which guard refused (the Python exception class).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardKind {
    /// `UntouchableInstrumentError`.
    Untouchable,
    /// `OvernightOptionError`.
    OvernightOption,
    /// `UncoveredOptionError` (a subclass of `OvernightOptionError`).
    UncoveredOption,
}

impl GuardKind {
    /// `type(exc).__name__`.
    #[must_use]
    pub const fn python_type(self) -> &'static str {
        match self {
            Self::Untouchable => "UntouchableInstrumentError",
            Self::OvernightOption => "OvernightOptionError",
            Self::UncoveredOption => "UncoveredOptionError",
        }
    }
}

/// A guard's refusal: `str(exc)` is [`GuardError::message`].
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct GuardError {
    /// The exception class.
    pub kind: GuardKind,
    /// `str(exc)`.
    pub message: String,
}

impl GuardError {
    fn new(kind: GuardKind, message: String) -> Self {
        Self { kind, message }
    }

    /// Is this an `OvernightOptionError` (or its `UncoveredOptionError` subclass)?
    #[must_use]
    pub const fn is_overnight_option(&self) -> bool {
        matches!(
            self.kind,
            GuardKind::OvernightOption | GuardKind::UncoveredOption
        )
    }
}

/// `str.isspace()` — Unicode whitespace plus the four ASCII separators Python counts.
#[must_use]
pub fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `s.strip()`.
#[must_use]
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_space)
}

/// `(s or "").upper().strip()`.
fn norm(s: &str) -> String {
    py_strip(&s.to_uppercase()).to_owned()
}

static OPTION_RE: LazyLock<Option<Regex>> = LazyLock::new(|| Regex::new(r"\d(CE|PE)$").ok());
static EXPIRY_CODE: LazyLock<Option<Regex>> =
    LazyLock::new(|| Regex::new(r"^\d{2}(?:[A-Z]{3}|[1-9OND]\d{2})$").ok());
static FUTURE_RE: LazyLock<Option<Regex>> =
    LazyLock::new(|| Regex::new(r"^([A-Z0-9&\-]+?)(\d{2}[A-Z]{3})FUT$").ok());

fn matches(re: &LazyLock<Option<Regex>>, text: &str) -> bool {
    re.as_ref().is_some_and(|r| r.is_match(text))
}

/// `is_option`: a tradingsymbol ending in a strike digit then `CE`/`PE` (`RELIANCE` is not a
/// call).
#[must_use]
pub fn is_option(symbol: &str) -> bool {
    matches(&OPTION_RE, &norm(symbol))
}

/// `assert_tradeable(symbol, series)`: SGB/G-sec hard block.
///
/// # Errors
/// `UntouchableInstrumentError` for an untouchable symbol, prefix or series.
pub fn assert_tradeable(symbol: &str, series: Option<&str>) -> Result<(), GuardError> {
    let s = norm(symbol);
    let series_u = series.unwrap_or("").to_uppercase();
    if UNTOUCHABLE_SYMBOLS.contains(&s.as_str())
        || UNTOUCHABLE_PREFIXES.iter().any(|p| s.starts_with(p))
        || UNTOUCHABLE_SERIES.contains(&series_u.as_str())
    {
        return Err(GuardError::new(
            GuardKind::Untouchable,
            format!("{symbol}: protected instrument (SGB/G-sec). This system will never trade it."),
        ));
    }
    Ok(())
}

/// `filter_tradeable(symbols)`: drops only the untouchables, order kept.
#[must_use]
pub fn filter_tradeable(symbols: &[String]) -> Vec<String> {
    symbols
        .iter()
        .filter(|s| assert_tradeable(s, None).is_ok())
        .cloned()
        .collect()
}

/// `assert_not_overnight_option(symbol, exchange, product)`.
///
/// # Errors
/// `OvernightOptionError` for an option on a derivative venue under a carry product.
pub fn assert_not_overnight_option(
    symbol: &str,
    exchange: &str,
    product: &str,
) -> Result<(), GuardError> {
    if DERIVATIVE_EXCHANGES.contains(&exchange.to_uppercase().as_str())
        && is_option(symbol)
        && CARRY_PRODUCTS.contains(&product.to_uppercase().as_str())
    {
        return Err(GuardError::new(
            GuardKind::OvernightOption,
            format!(
                "{symbol}: {product} would carry this option past the close. This system never \
                 holds an option overnight — option orders are MIS only."
            ),
        ));
    }
    Ok(())
}

/// `future_underlying("RELIANCE26SEPFUT")` → `RELIANCE`; `None` for anything else.
#[must_use]
pub fn future_underlying(symbol: &str) -> Option<String> {
    let s = norm(symbol);
    FUTURE_RE
        .as_ref()
        .and_then(|r| r.captures(&s))
        .and_then(|c| c.get(1).map(|m| m.as_str().to_owned()))
}

/// `product_exchange_refusal(...)`: why this product/venue pair is refused, or `""`.
#[must_use]
pub fn product_exchange_refusal(
    product: &str,
    exchange: &str,
    intraday_enabled: bool,
    options_enabled: bool,
    fno_carry_enabled: bool,
    fo_plan: bool,
) -> String {
    let product_u = norm(product);
    let exchange_u = norm(exchange);
    let or_repr = |u: &str, raw: &str| {
        if u.is_empty() {
            baskfy_providers::py::repr_str(raw)
        } else {
            u.to_owned()
        }
    };
    if DERIVATIVE_EXCHANGES.contains(&exchange_u.as_str()) {
        if !options_enabled {
            return "F&O/derivatives disabled (config.OPTIONS_ENABLED)".to_owned();
        }
        if product_u == FO_CARRY_PRODUCT && exchange_u == FO_CARRY_EXCHANGE && fo_plan {
            if !fno_carry_enabled {
                return format!(
                    "{product_u}: only MIS is allowed on a derivative venue ({exchange_u}); an \
                     fo_plan order may carry NRML only with BASKFY_FNO_CARRY_ENABLED"
                );
            }
            return String::new();
        }
        if product_u != "MIS" {
            return format!(
                "{}: only MIS is allowed on a derivative venue ({exchange_u}); nothing is carried \
                 past the close",
                or_repr(&product_u, product)
            );
        }
        if !intraday_enabled {
            return "MIS/intraday disabled (config.INTRADAY_ENABLED)".to_owned();
        }
        return String::new();
    }
    if !CASH_EXCHANGES.contains(&exchange_u.as_str()) {
        return format!(
            "{}: exchange is neither cash equity (NSE/BSE) nor an enabled derivative venue",
            or_repr(&exchange_u, exchange)
        );
    }
    if product_u == "MIS" {
        if !intraday_enabled {
            return "MIS/intraday disabled (config.INTRADAY_ENABLED)".to_owned();
        }
        return String::new();
    }
    if product_u != "CNC" {
        return format!(
            "{}: only CNC is allowed on cash equity unless INTRADAY_ENABLED (MIS)",
            or_repr(&product_u, product)
        );
    }
    String::new()
}

/// One option position in units (positive long, negative short) — the shape
/// `baskfy_core.fno.covered.OptionPosition` has and `guards.SignedOptionLeg` declares.
///
/// On the wire (a [`FoPlanRef`]'s `step`, `broker_positions`, `plan_filled`) it is a dict
/// `{"underlying": str, "expiry": date | "YYYY-MM-DD", "option_type": "CE" | "PE",
/// "strike": str | int | float, "quantity": int}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SignedLeg {
    /// `underlying`.
    pub underlying: String,
    /// `expiry`.
    pub expiry: NaiveDate,
    /// `option_type` (`CE`/`PE`).
    pub option_type: String,
    /// `strike`.
    pub strike: Decimal,
    /// `quantity`, signed.
    pub quantity: i64,
}

impl SignedLeg {
    /// Read a leg off its dict form.
    ///
    /// # Errors
    /// A missing or mistyped field, named.
    pub fn from_value(value: &PyValue) -> Result<Self, String> {
        let PyValue::Map(m) = value else {
            return Err("an option leg is a dict".to_owned());
        };
        let text = |k: &str| match m.get(k) {
            Some(PyValue::Str(s)) => Ok(s.clone()),
            _ => Err(format!("an option leg's {k} is a str")),
        };
        let expiry = match m.get("expiry") {
            Some(PyValue::Date(d)) => *d,
            Some(PyValue::Str(s)) => NaiveDate::parse_from_str(s, "%Y-%m-%d")
                .map_err(|_| format!("an option leg's expiry {s:?} is not a date"))?,
            _ => return Err("an option leg's expiry is a date".to_owned()),
        };
        let strike = match m.get("strike") {
            Some(PyValue::Str(s)) => s
                .parse::<Decimal>()
                .map_err(|_| format!("an option leg's strike {s:?} is not a number"))?,
            Some(PyValue::Int(i)) => Decimal::from(*i),
            Some(PyValue::Float(f)) => baskfy_core::py::decimal_from_float_str(*f)
                .map_err(|e| format!("an option leg's strike: {e}"))?,
            _ => return Err("an option leg's strike is a number".to_owned()),
        };
        let quantity = match m.get("quantity") {
            Some(PyValue::Int(i)) => *i,
            _ => return Err("an option leg's quantity is an int".to_owned()),
        };
        Ok(Self {
            underlying: text("underlying")?,
            expiry,
            option_type: text("option_type")?,
            strike,
            quantity,
        })
    }

    /// The dict form, for a [`FoPlanRef`].
    #[must_use]
    pub fn to_value(&self) -> PyValue {
        PyValue::Map(crate::wire::dict([
            ("underlying", PyValue::Str(self.underlying.clone())),
            ("expiry", PyValue::Date(self.expiry)),
            ("option_type", PyValue::Str(self.option_type.clone())),
            ("strike", PyValue::Str(self.strike.to_string())),
            ("quantity", PyValue::Int(self.quantity)),
        ]))
    }
}

/// `CoverageRule`: `book_after -> reasons`; empty means covered. Injected into the gateway
/// (execution does not import core); [`core_coverage`] wires `baskfy_core::fno::covered`.
pub type CoverageRule = Arc<dyn Fn(&[SignedLeg]) -> Vec<String> + Send + Sync>;

/// `baskfy_core.fno.covered.uncovered`, as a [`CoverageRule`]. A leg whose type is neither
/// `CE` nor `PE` cannot be assessed and is reported (refused), never skipped.
#[must_use]
pub fn core_coverage() -> CoverageRule {
    Arc::new(|legs: &[SignedLeg]| {
        use baskfy_core::fno::covered::{OptionPosition, uncovered};
        use baskfy_core::fno::vol::OptionType;
        let mut book = Vec::with_capacity(legs.len());
        let mut bad = Vec::new();
        for leg in legs {
            let kind = match leg.option_type.to_uppercase().as_str() {
                "CE" => OptionType::Ce,
                "PE" => OptionType::Pe,
                other => {
                    bad.push(format!(
                        "{}: {other:?} is not an option type",
                        leg.underlying
                    ));
                    continue;
                }
            };
            book.push(OptionPosition {
                underlying: leg.underlying.clone(),
                expiry: leg.expiry,
                option_type: kind,
                strike: leg.strike,
                quantity: leg.quantity,
            });
        }
        if !bad.is_empty() {
            return bad;
        }
        uncovered(book.iter())
    })
}

fn sleeve_of(fo_plan: &FoPlanRef) -> String {
    norm(&fo_plan.sleeve)
}

/// `fo_order_refusal(symbol, exchange, fo_plan)`: why this reference cannot ride on this
/// order, or `""`.
#[must_use]
pub fn fo_order_refusal(symbol: &str, exchange: &str, fo_plan: &FoPlanRef) -> String {
    let plan_id = &fo_plan.plan_id;
    if py_strip(plan_id).is_empty() {
        return "fo_plan reference carries no plan_id".to_owned();
    }
    let sleeve = sleeve_of(fo_plan);
    let option_sleeve = FO_OPTION_SLEEVES.contains(&sleeve.as_str());
    let future_sleeve = FO_FUTURE_SLEEVES.contains(&sleeve.as_str());
    if !option_sleeve && !future_sleeve {
        return format!(
            "fo_plan {plan_id}: {} is not an FO sleeve",
            baskfy_providers::py::repr_str(&fo_plan.sleeve)
        );
    }
    if norm(exchange) != FO_CARRY_EXCHANGE {
        return format!(
            "fo_plan {plan_id}: FO orders are on NFO, not {}",
            baskfy_providers::py::repr_str(exchange)
        );
    }
    if option_sleeve && !is_option(symbol) {
        return format!("fo_plan {plan_id}: {sleeve} trades options; {symbol} is not one");
    }
    if future_sleeve && future_underlying(symbol).is_none() {
        return format!("fo_plan {plan_id}: {sleeve} trades futures; {symbol} is not one");
    }
    String::new()
}

/// `_strike_text`: `format(Decimal(strike).normalize(), "f")`.
fn strike_text(strike: Decimal) -> String {
    strike.normalize().to_string()
}

/// `str[a:b]` on characters, empty when the slice is inverted.
fn char_slice(s: &str, start: usize, end: usize) -> String {
    if start >= end {
        return String::new();
    }
    s.chars().skip(start).take(end - start).collect()
}

/// `_leg_mismatch`: the order must BE the step the guard assesses.
fn leg_mismatch(symbol: &str, qty: i64, side: &str, step: &SignedLeg) -> String {
    let sym = norm(symbol);
    let side_u = norm(side);
    if side_u != "BUY" && side_u != "SELL" {
        return format!(
            "side {} is neither BUY nor SELL",
            baskfy_providers::py::repr_str(side)
        );
    }
    // Python computes `abs(int(qty))` first; `i64::abs` overflows on `i64::MIN`, so the
    // non-positive refusal (which Python reaches with the same message) is tested first.
    let signed = if side_u == "BUY" {
        qty
    } else {
        qty.saturating_neg()
    };
    if qty <= 0 || step.quantity != signed {
        return format!(
            "{symbol}: the order is {side_u} {qty} but the plan step is {}",
            step.quantity
        );
    }
    let underlying = step.underlying.to_uppercase();
    let tail = format!(
        "{}{}",
        strike_text(step.strike),
        step.option_type.to_uppercase()
    );
    if !sym.starts_with(&underlying) {
        return format!("{symbol}: the plan step is on {}", step.underlying);
    }
    if !sym.ends_with(&tail) {
        return format!("{symbol}: the plan step is the {tail} leg");
    }
    let len = sym.chars().count();
    let middle = char_slice(
        &sym,
        underlying.chars().count(),
        len.saturating_sub(tail.chars().count()),
    );
    if !matches(&EXPIRY_CODE, &middle) {
        return format!("{symbol}: does not read as {underlying} <expiry> {tail}");
    }
    String::new()
}

fn legs(values: &[PyValue]) -> Result<Vec<SignedLeg>, String> {
    values.iter().map(SignedLeg::from_value).collect()
}

/// `assert_overnight_option_is_covered(symbol, qty, side, fo_plan=, coverage=)`.
///
/// # Errors
/// `UncoveredOptionError`: no step, a step that is not the order, no rule wired, or a book
/// after the order with an uncovered short.
pub fn assert_overnight_option_is_covered(
    symbol: &str,
    qty: i64,
    side: &str,
    fo_plan: &FoPlanRef,
    coverage: Option<&CoverageRule>,
) -> Result<(), GuardError> {
    let uncovered = |m: String| Err(GuardError::new(GuardKind::UncoveredOption, m));
    if !is_option(symbol) {
        return Ok(());
    }
    let plan_id = &fo_plan.plan_id;
    let Some(step_value) = &fo_plan.step else {
        return uncovered(format!(
            "{symbol}: fo_plan {plan_id} names no step for this option order, so its cover cannot \
             be assessed"
        ));
    };
    let step = match SignedLeg::from_value(step_value) {
        Ok(s) => s,
        Err(why) => return uncovered(format!("fo_plan {plan_id}: {why}")),
    };
    let mismatch = leg_mismatch(symbol, qty, side, &step);
    if !mismatch.is_empty() {
        return uncovered(format!("fo_plan {plan_id}: {mismatch}"));
    }
    let Some(rule) = coverage else {
        return uncovered(format!(
            "{symbol}: no covered-overnight rule is wired into this gateway; an FO option order \
             is refused until one is"
        ));
    };
    let mut book = match legs(&fo_plan.broker_positions) {
        Ok(b) => b,
        Err(why) => return uncovered(format!("fo_plan {plan_id}: {why}")),
    };
    match legs(&fo_plan.plan_filled) {
        Ok(f) => book.extend(f),
        Err(why) => return uncovered(format!("fo_plan {plan_id}: {why}")),
    }
    book.push(step);
    let reasons = rule(&book);
    if !reasons.is_empty() {
        return uncovered(format!(
            "{symbol}: the book after this order would hold an uncovered short option — {}",
            reasons.join("; ")
        ));
    }
    Ok(())
}

/// `fo_gtt_refusal(...)`: the GTT branch of `docs/fno/02` §1 — an F2 stock future on
/// NFO/NRML with both flags on. An option GTT is refused whatever the switches.
#[must_use]
pub fn fo_gtt_refusal(
    symbol: &str,
    exchange: &str,
    fo_plan: &FoPlanRef,
    options_enabled: bool,
    fno_carry_enabled: bool,
) -> String {
    if is_option(symbol) {
        return format!(
            "{symbol}: an option GTT is refused whatever the switches; an option structure's stop \
             is its structure (docs/fno/02 §2.4)"
        );
    }
    let why = fo_order_refusal(symbol, exchange, fo_plan);
    if !why.is_empty() {
        return why;
    }
    if !FO_FUTURE_SLEEVES.contains(&sleeve_of(fo_plan).as_str()) {
        return format!(
            "fo_plan {}: only an F2 future may rest a GTT on NFO",
            fo_plan.plan_id
        );
    }
    match future_underlying(symbol) {
        Some(u) if !INDEX_UNDERLYINGS.contains(&u.as_str()) => {}
        _ => return format!("{symbol}: a GTT on NFO is admitted for a stock future only"),
    }
    product_exchange_refusal(
        FO_CARRY_PRODUCT,
        exchange,
        false,
        options_enabled,
        fno_carry_enabled,
        true,
    )
}
