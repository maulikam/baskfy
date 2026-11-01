//! The curated-basket arithmetic the read routes serve: `baskfy_core.curated_metrics`
//! (`headline_return`, `whole_months_between`, `return_convention_fields`, `version_aware_nav`,
//! `nav_for_storage`, `shares_at_amount`), `baskfy_core.curated_plans.build_invest_plan`, and
//! `baskfy_api.kite_basket.build_basket` (the Publisher hand-off — **never an order**).
//!
//! Decimal arithmetic runs in [`WideDecimal`], Python's default context (28 significant digits,
//! half-even), so a chained NAV or a share count rounds exactly where Python's does.

use std::collections::BTreeMap;

use baskfy_core::decimal::{Rounding, WideDecimal};
use chrono::{Datelike, NaiveDate};
use rust_decimal::Decimal;

// ---------------------------------------------------------------------------------------------
// curated_metrics
// ---------------------------------------------------------------------------------------------

/// `RETURN_CONVENTION`.
pub(crate) const RETURN_CONVENTION: &str = "PRICE_RETURN";
/// `DIVIDENDS_INCLUDED`.
pub(crate) const DIVIDENDS_INCLUDED: bool = false;
/// `RETURN_CONVENTION_NOTE`.
pub(crate) const RETURN_CONVENTION_NOTE: &str = "Returns are price returns computed from split- \
    and bonus-adjusted closes. Cash dividends are excluded (docs/DECISIONS-MERGE.md M39.3), so a \
    total-return series would be higher by roughly the basket's dividend yield each year - about \
    1.2% for the broad Indian market - compounding.";
/// `NO_HEADLINE_LABEL`.
pub(crate) const NO_HEADLINE_LABEL: &str = "Not enough history";

/// `whole_months_between(start, end)`.
pub(crate) fn whole_months_between(start: NaiveDate, end: NaiveDate) -> i64 {
    if end < start {
        return 0;
    }
    let mut months = i64::from(end.year() - start.year()) * 12 + i64::from(end.month())
        - i64::from(start.month());
    if end.day() < start.day() {
        months -= 1;
    }
    months.max(0)
}

/// `headline_return(...)`: `(label, value_pct)`.
pub(crate) fn headline_return(
    months_available: i64,
    ret_1m: Option<Decimal>,
    ret_6m: Option<Decimal>,
    ret_1y: Option<Decimal>,
    cagr_3y: Option<Decimal>,
    cagr_5y: Option<Decimal>,
    since_inception_pct: Option<Decimal>,
) -> (String, Option<Decimal>) {
    if (1..12).contains(&months_available) && since_inception_pct.is_some() {
        return (format!("{months_available}M returns"), since_inception_pct);
    }
    for (required, label, value) in [
        (60, "5Y CAGR", cagr_5y),
        (36, "3Y CAGR", cagr_3y),
        (12, "1Y returns", ret_1y),
        (6, "6M returns", ret_6m),
        (1, "1M returns", ret_1m),
    ] {
        if months_available >= required && value.is_some() {
            return (label.to_owned(), value);
        }
    }
    (NO_HEADLINE_LABEL.to_owned(), None)
}

fn wide(d: Decimal) -> WideDecimal {
    WideDecimal::from(d)
}

/// `basket_day_return(weights, prices_prev, prices_today)` in Python's decimal context.
fn basket_day_return(
    weights: &[(String, WideDecimal)],
    prev: &BTreeMap<String, WideDecimal>,
    today: &BTreeMap<String, WideDecimal>,
) -> Result<WideDecimal, String> {
    let zero = WideDecimal::zero();
    let one = wide(Decimal::ONE);
    let mut total_w = WideDecimal::zero();
    let mut contrib = WideDecimal::zero();
    for (symbol, weight) in weights {
        let (Some(p), Some(t)) = (prev.get(symbol), today.get(symbol)) else {
            continue;
        };
        if *p <= zero {
            continue;
        }
        total_w = total_w.add(weight);
        let ratio = t.div(p).map_err(|e| e.to_string())?;
        contrib = contrib.add(&weight.mul(&ratio.sub(&one)));
    }
    if total_w <= zero {
        return Ok(zero);
    }
    contrib.div(&total_w).map_err(|e| e.to_string())
}

/// One `BasketVersion`: its effective date and weights, in the version's insertion order.
#[derive(Debug, Clone)]
pub(crate) struct BasketVersion {
    /// `effective_date`.
    pub effective_date: NaiveDate,
    /// `weights` (symbol, weight).
    pub weights: Vec<(String, Decimal)>,
}

/// `bisect.bisect_right(dates, day) - 1`, floored at 0.
pub(crate) fn version_index(effective_dates: &[NaiveDate], day: NaiveDate) -> usize {
    effective_dates
        .partition_point(|d| *d <= day)
        .saturating_sub(1)
}

/// `version_aware_nav(prices_by_date, versions).points`, base 100, unrounded.
pub(crate) fn version_aware_nav(
    prices_by_date: &BTreeMap<NaiveDate, BTreeMap<String, Decimal>>,
    versions: &[BasketVersion],
) -> Result<Vec<(NaiveDate, WideDecimal)>, String> {
    if versions.is_empty() || prices_by_date.is_empty() {
        return Ok(Vec::new());
    }
    let mut ordered: Vec<&BasketVersion> = versions.iter().collect();
    ordered.sort_by_key(|v| v.effective_date);
    let effective: Vec<NaiveDate> = ordered.iter().map(|v| v.effective_date).collect();
    let inception = effective[0];
    let dates: Vec<NaiveDate> = prices_by_date
        .keys()
        .copied()
        .filter(|d| *d >= inception)
        .collect();
    let Some(first) = dates.first() else {
        return Ok(Vec::new());
    };
    let widen = |m: &BTreeMap<String, Decimal>| -> BTreeMap<String, WideDecimal> {
        m.iter().map(|(k, v)| (k.clone(), wide(*v))).collect()
    };
    let base = wide(Decimal::ONE_HUNDRED);
    let one = wide(Decimal::ONE);
    let mut points = vec![(*first, base.clone())];
    let mut nav = base;
    for pair in dates.windows(2) {
        let (prev_day, day) = (pair[0], pair[1]);
        let weights: Vec<(String, WideDecimal)> = ordered[version_index(&effective, day)]
            .weights
            .iter()
            .map(|(s, w)| (s.clone(), wide(*w)))
            .collect();
        let empty = BTreeMap::new();
        let prev = prices_by_date.get(&prev_day).unwrap_or(&empty);
        let today = prices_by_date.get(&day).unwrap_or(&empty);
        let ret = basket_day_return(&weights, &widen(prev), &widen(today))?;
        nav = nav.mul(&one.add(&ret));
        points.push((day, nav.clone()));
    }
    Ok(points)
}

/// `nav_for_storage(nav)`: `money()`, paise half-up.
pub(crate) fn nav_for_storage(nav: &WideDecimal) -> Result<WideDecimal, String> {
    nav.quantize(-2, Rounding::HalfUp)
        .map_err(|e| e.to_string())
}

/// Python's default-context `Emax`: an operation whose result's adjusted exponent exceeds it
/// raises `decimal.Overflow` (trapped by default), which no route catches — a 500.
const EMAX: i64 = 999_999;

/// Why [`build_invest_plan`] refused: Python's `ValueError` (the route's 400) or an arithmetic
/// trap nothing catches (the route's 500).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum PlanError {
    /// `ValueError(message)`.
    Value(String),
    /// `decimal.Overflow` / `InvalidOperation`.
    Arithmetic(String),
}

/// `d.adjusted() > Emax` — only reachable with a positive exponent (a coefficient is at most 28
/// digits after any context operation), where `str(d)` always carries the `E+n` suffix.
fn overflows(d: &WideDecimal) -> bool {
    if d.exponent() <= 0 {
        return false;
    }
    let sci = d.to_sci_string();
    sci.rsplit_once('E')
        .and_then(|(_, e)| e.parse::<i64>().ok())
        .is_none_or(|adjusted| adjusted > EMAX)
}

/// `shares_at_amount(amount, prices, weights)` (lot size 1): `int(floor(amount * w / p))` each,
/// as Python's unbounded `int` spelled in decimal digits.
fn shares_at_amount(
    amount: &WideDecimal,
    prices: &[WideDecimal],
    weights: &[WideDecimal],
) -> Result<Vec<String>, PlanError> {
    let zero = WideDecimal::zero();
    let trap = |e: baskfy_core::decimal::DecimalError| PlanError::Arithmetic(e.to_string());
    let overflow = || PlanError::Arithmetic("[<class 'decimal.Overflow'>]".to_owned());
    let mut out = Vec::new();
    for (price, weight) in prices.iter().zip(weights) {
        if *price <= zero || *weight <= zero {
            return Err(PlanError::Value(
                "price and weight must be positive".to_owned(),
            ));
        }
        let product = amount.mul(weight);
        if overflows(&product) {
            return Err(overflow());
        }
        let raw = product.div(price).map_err(trap)?;
        if overflows(&raw) {
            return Err(overflow());
        }
        // `to_integral_value(ROUND_DOWN)`: unchanged at a non-negative exponent (it never checks
        // the precision), otherwise rescaled to exponent 0 — at most 28 digits, since a negative
        // exponent means the value is below 10**28. `int(...)` is then `format(d, "f")`.
        let lots = if raw.exponent() >= 0 {
            raw
        } else {
            raw.quantize(0, Rounding::Down).map_err(trap)?
        };
        out.push(lots.to_string());
    }
    Ok(out)
}

/// Whether a Python `int` spelled in decimal digits is `> 0`.
fn int_text_positive(text: &str) -> bool {
    !text.starts_with('-') && text.bytes().any(|b| b != b'0')
}

/// One `PlanLeg` (BUY only, for an invest plan).
#[derive(Debug, Clone)]
pub(crate) struct Leg {
    /// `symbol`.
    pub symbol: String,
    /// `quantity`: a Python `int` (unbounded) in decimal digits.
    pub quantity: String,
}

/// `build_invest_plan(target_weights=, prices=, amount=, now=)["legs"]`.
///
/// # Errors
/// [`PlanError::Value`] with the `ValueError` message; [`PlanError::Arithmetic`] for a trapped
/// decimal signal (an `amount` so large the product overflows `Emax`).
pub(crate) fn build_invest_plan(
    target_weights: &BTreeMap<String, Decimal>,
    prices: &BTreeMap<String, Decimal>,
    amount: &WideDecimal,
) -> Result<Vec<Leg>, PlanError> {
    if *amount <= WideDecimal::zero() {
        return Err(PlanError::Value("amount must be positive".to_owned()));
    }
    if target_weights.is_empty() {
        return Err(PlanError::Value(
            "target_weights cannot be empty".to_owned(),
        ));
    }
    // `sorted(target_weights)`: Python sorts str by code point, as BTreeMap's keys are.
    let symbols: Vec<&String> = target_weights.keys().collect();
    let missing: Vec<&str> = symbols
        .iter()
        .filter(|s| !prices.contains_key(s.as_str()))
        .map(|s| s.as_str())
        .collect();
    if !missing.is_empty() {
        return Err(PlanError::Value(format!(
            "missing prices for: {}",
            missing.join(", ")
        )));
    }
    let weights: Vec<WideDecimal> = symbols
        .iter()
        .map(|s| wide(target_weights.get(s.as_str()).copied().unwrap_or_default()))
        .collect();
    let price_list: Vec<WideDecimal> = symbols
        .iter()
        .map(|s| wide(prices.get(s.as_str()).copied().unwrap_or_default()))
        .collect();
    let qtys = shares_at_amount(amount, &price_list, &weights)?;
    Ok(symbols
        .into_iter()
        .zip(qtys)
        .filter(|(_, q)| int_text_positive(q))
        .map(|(s, q)| Leg {
            symbol: s.clone(),
            quantity: q,
        })
        .collect())
}

// ---------------------------------------------------------------------------------------------
// kite_basket
// ---------------------------------------------------------------------------------------------

/// `KITE_BASKET_URL`.
pub(crate) const KITE_BASKET_URL: &str = "https://kite.zerodha.com/connect/basket";
/// `MAX_BASKET_ITEMS`.
pub(crate) const MAX_BASKET_ITEMS: usize = 10;

/// One `BasketItem` (Kite's field names).
#[derive(Debug, Clone, serde::Serialize)]
pub(crate) struct BasketItem {
    /// `variety`.
    pub variety: &'static str,
    /// `tradingsymbol`.
    pub tradingsymbol: String,
    /// `exchange`.
    pub exchange: &'static str,
    /// `transaction_type`.
    pub transaction_type: &'static str,
    /// `order_type`.
    pub order_type: &'static str,
    /// `quantity`: a Python `int` in decimal digits, written as a bare JSON number.
    #[serde(serialize_with = "int_text")]
    pub quantity: String,
    /// `product`.
    pub product: &'static str,
    /// `readonly`.
    pub readonly: bool,
}

/// `KiteBasketOut`.
#[derive(Debug, Clone, serde::Serialize)]
pub(crate) struct KiteBasketOut {
    /// `url`.
    pub url: &'static str,
    /// `api_key`.
    pub api_key: String,
    /// `configured`.
    pub configured: bool,
    /// `items`.
    pub items: Vec<BasketItem>,
    /// `batches`.
    pub batches: Vec<Vec<BasketItem>>,
    /// `excluded`.
    pub excluded: Vec<String>,
}

/// A Python `int` held as its decimal digits, serialised as the bare JSON number Python writes.
fn int_text<S: serde::Serializer>(text: &str, s: S) -> Result<S::Ok, S::Error> {
    let raw = serde_json::value::RawValue::from_string(text.to_owned())
        .map_err(serde::ser::Error::custom)?;
    serde::Serialize::serialize(&raw, s)
}

/// `build_basket(orders, api_key=)` as the route's `KiteBasketOut`: malformed rows (an unknown
/// side, a quantity ≤ 0) are dropped and named; nothing is filtered on policy grounds. Each
/// quantity is a Python `int` in decimal digits (`-?[0-9]+`).
pub(crate) fn build_basket(orders: &[(String, String, String)], api_key: &str) -> KiteBasketOut {
    let mut items = Vec::new();
    let mut excluded = Vec::new();
    for (symbol, side, quantity) in orders {
        let side = match super::py_strip(side).to_uppercase().as_str() {
            "BUY" => Some("BUY"),
            "SELL" => Some("SELL"),
            _ => None,
        };
        let Some(side) = side.filter(|_| int_text_positive(quantity)) else {
            excluded.push(symbol.clone());
            continue;
        };
        items.push(BasketItem {
            variety: "regular",
            tradingsymbol: symbol.clone(),
            exchange: "NSE",
            transaction_type: side,
            order_type: "MARKET",
            quantity: quantity.clone(),
            product: "CNC",
            readonly: false,
        });
    }
    let batches = items
        .chunks(MAX_BASKET_ITEMS)
        .map(<[BasketItem]>::to_vec)
        .collect();
    KiteBasketOut {
        url: KITE_BASKET_URL,
        api_key: api_key.to_owned(),
        configured: !api_key.is_empty(),
        items,
        batches,
        excluded,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap_or_default()
    }

    #[test]
    fn months_and_headline() {
        assert_eq!(whole_months_between(d(2026, 1, 31), d(2026, 2, 28)), 0);
        assert_eq!(whole_months_between(d(2026, 1, 15), d(2026, 3, 15)), 2);
        assert_eq!(whole_months_between(d(2026, 3, 1), d(2026, 1, 1)), 0);
        let (label, value) =
            headline_return(7, None, None, None, None, None, Some(Decimal::new(123, 1)));
        assert_eq!(label, "7M returns");
        assert_eq!(value, Some(Decimal::new(123, 1)));
        let (label, value) = headline_return(0, None, None, None, None, None, None);
        assert_eq!((label.as_str(), value), (NO_HEADLINE_LABEL, None));
    }

    #[test]
    fn a_basket_batches_by_ten_and_names_what_it_drops() {
        let mut orders: Vec<(String, String, String)> = (0..12)
            .map(|i| (format!("S{i}"), "buy".to_owned(), "1".to_owned()))
            .collect();
        orders.push(("BAD".to_owned(), "HOLD".to_owned(), "5".to_owned()));
        orders.push(("ZERO".to_owned(), "SELL".to_owned(), "0".to_owned()));
        orders.push(("NEG".to_owned(), "SELL".to_owned(), "-3".to_owned()));
        let out = build_basket(&orders, "");
        assert_eq!(out.items.len(), 12);
        assert_eq!(out.batches.len(), 2);
        assert_eq!(out.excluded, vec!["BAD", "ZERO", "NEG"]);
        assert!(!out.configured);
    }

    #[test]
    fn whole_shares_round_down() {
        let mut w = BTreeMap::new();
        w.insert("A".to_owned(), Decimal::new(5, 1));
        w.insert("B".to_owned(), Decimal::new(5, 1));
        let mut p = BTreeMap::new();
        p.insert("A".to_owned(), Decimal::new(300, 0));
        p.insert("B".to_owned(), Decimal::new(7000, 0));
        let legs = build_invest_plan(&w, &p, &WideDecimal::from(Decimal::new(10_000, 0)));
        let legs = legs.unwrap_or_default();
        assert_eq!(legs.len(), 1);
        assert_eq!(
            (legs[0].symbol.as_str(), legs[0].quantity.as_str()),
            ("A", "16")
        );
    }

    fn one_leg(amount: &str) -> Result<Vec<Leg>, PlanError> {
        let mut w = BTreeMap::new();
        w.insert("A".to_owned(), Decimal::new(7, 1));
        let mut p = BTreeMap::new();
        p.insert("A".to_owned(), Decimal::new(12_345, 2));
        let amount: WideDecimal = amount.parse().unwrap_or_default();
        build_invest_plan(&w, &p, &amount)
    }

    #[test]
    fn quantities_are_unbounded_ints_as_python_computes_them() {
        // Each checked against `int((Decimal(a) * Decimal("0.7") / Decimal("123.45"))
        // .to_integral_value(rounding=ROUND_DOWN))` under the default context.
        let q = |a: &str| one_leg(a).map(|legs| legs.into_iter().map(|l| l.quantity).collect());
        assert_eq!(
            q("1e30"),
            Ok(vec!["5670311867152693398136897529".to_owned()])
        );
        assert_eq!(
            q("1e40"),
            Ok(vec![format!(
                "5670311867152693398136897529{}",
                "0".repeat(10)
            )])
        );
        assert_eq!(q("1e-50"), Ok(Vec::<String>::new()));
        assert!(q("1e1000000").is_ok());
        assert!(matches!(q("1e1000001"), Err(PlanError::Arithmetic(_))));
    }
}
