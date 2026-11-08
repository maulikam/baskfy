//! `baskfy_core.curated_plans`, `curated_metrics.shares_at_amount` and `market_hours_cb`, ported
//! for the plan-preview routes (DECISIONS-RS R2.3: Python core with no Rust home lives privately
//! beside its one caller). Pure: `now` and the trading dates are parameters (law 1's spirit).

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::decimal::WideDecimal;
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta, TimeZone};
use num_bigint::{BigInt, Sign};

/// `SESSION_OPEN` (09:15 IST).
fn session_open() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 0).unwrap_or(NaiveTime::MIN)
}

/// `SESSION_CLOSE` (15:30 IST).
fn session_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// `is_nse_session_open(now, trading_dates)`: a trading day, inside [09:15, 15:30] IST inclusive.
#[must_use]
pub fn is_nse_session_open(now: DateTime<FixedOffset>, dates: &BTreeSet<NaiveDate>) -> bool {
    if !dates.contains(&now.date_naive()) {
        return false;
    }
    let clock = now.time();
    session_open() <= clock && clock <= session_close()
}

/// `next_session_open(now, trading_dates)`: today's 09:15 if it is a trading day not yet open,
/// else 09:15 on the first trading date after today.
///
/// # Errors
/// The `ValueError`'s message (an empty calendar, nothing after today).
pub fn next_session_open(
    now: DateTime<FixedOffset>,
    dates: &BTreeSet<NaiveDate>,
) -> Result<DateTime<FixedOffset>, String> {
    if dates.is_empty() {
        return Err("trading_dates cannot be empty".to_owned());
    }
    let today = now.date_naive();
    let at_open = |d: NaiveDate| {
        now.offset()
            .from_local_datetime(&d.and_time(session_open()))
            .single()
            .ok_or_else(|| "invalid open".to_owned())
    };
    if dates.contains(&today) && now.time() < session_open() {
        return at_open(today);
    }
    match dates.range(today.succ_opt().unwrap_or(today)..).next() {
        Some(d) if *d > today => at_open(*d),
        _ => Err(format!(
            "no trading day after {} in the provided calendar",
            crate::wire::py_date(today)
        )),
    }
}

/// `PlanLeg`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Leg {
    /// `symbol`.
    pub symbol: String,
    /// `side` (`BUY`/`SELL`).
    pub side: &'static str,
    /// `quantity` (a Python int — unbounded, as Python's is: DECISIONS-RS R8.ar1; whole shares).
    pub quantity: BigInt,
    /// `ref_price` (the posted `Decimal`).
    pub ref_price: WideDecimal,
}

/// `DeskPlan`.
#[derive(Debug, Clone)]
pub struct Plan {
    /// `kind`.
    pub kind: &'static str,
    /// `legs`.
    pub legs: Vec<Leg>,
    /// `requested_amount`.
    pub requested_amount: WideDecimal,
    /// `expires_at_hint`: `now + PLAN_TTL` (30 minutes).
    pub expires_at_hint: DateTime<FixedOffset>,
}

/// `PLAN_TTL`.
fn expires(now: DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    now + TimeDelta::minutes(30)
}

/// `n > 0` for a Python `int`.
fn is_positive(n: &BigInt) -> bool {
    n.sign() == Sign::Plus
}

fn positive(d: &WideDecimal) -> bool {
    !d.is_zero() && !d.is_sign_negative()
}

/// `int(d.to_integral_value(rounding=ROUND_DOWN))`.
///
/// Not `quantize(0)`: `to_integral_value` has no 28-digit limit, so a quotient such as
/// `1.5E+29` is the integer `150000000000000000000000000000` in Python, where `quantize` would
/// raise `InvalidOperation` (found in review). Read from the value's `str()`: sign, coefficient
/// and exponent, then truncated toward zero. Unbounded, as Python's `int` is (R8.ar1).
fn to_int(d: &WideDecimal) -> Result<BigInt, String> {
    let text = d.to_sci_string();
    let (negative, body) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.as_str()),
    };
    let (mantissa, exp_text) = body.split_once(['E', 'e']).unwrap_or((body, "0"));
    let mut exp: i64 = exp_text
        .parse()
        .map_err(|_| format!("bad exponent in {text}"))?;
    let (whole, frac) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    exp -= i64::try_from(frac.len()).map_err(|e| e.to_string())?;
    let mut digits = format!("{whole}{frac}");
    if exp >= 0 {
        let zeros = usize::try_from(exp).map_err(|e| e.to_string())?;
        digits.push_str(&"0".repeat(zeros));
    } else {
        let cut = usize::try_from(exp.unsigned_abs()).map_err(|e| e.to_string())?;
        digits.truncate(digits.len().saturating_sub(cut));
    }
    if digits.is_empty() {
        return Ok(BigInt::default());
    }
    let magnitude = digits
        .parse::<BigInt>()
        .map_err(|_| format!("bad integer part in {text}"))?;
    Ok(if negative { -magnitude } else { magnitude })
}

/// `shares_at_amount(amount, prices, weights)` with `lot_size=1`.
///
/// # Errors
/// The `ValueError`'s message.
pub fn shares_at_amount(
    amount: &WideDecimal,
    prices: &[WideDecimal],
    weights: &[WideDecimal],
) -> Result<Vec<BigInt>, String> {
    if amount.is_sign_negative() && !amount.is_zero() {
        return Err("amount cannot be negative".to_owned());
    }
    if prices.len() != weights.len() {
        return Err("prices and weights must have the same length".to_owned());
    }
    let lot = WideDecimal::from(1_i64);
    let mut out = Vec::with_capacity(prices.len());
    for (price, weight) in prices.iter().zip(weights) {
        if !positive(price) || !positive(weight) {
            return Err("price and weight must be positive".to_owned());
        }
        let raw = amount.mul(weight).div(price).map_err(|e| e.to_string())?;
        let lots = raw.div(&lot).map_err(|e| e.to_string())?;
        out.push(to_int(&lots)?);
    }
    Ok(out)
}

fn missing_prices(
    symbols: &[String],
    prices: &BTreeMap<String, WideDecimal>,
) -> Result<(), String> {
    let missing: Vec<&str> = symbols
        .iter()
        .filter(|s| !prices.contains_key(*s))
        .map(String::as_str)
        .collect();
    if missing.is_empty() {
        Ok(())
    } else {
        Err(format!("missing prices for: {}", missing.join(", ")))
    }
}

fn price_of(prices: &BTreeMap<String, WideDecimal>, s: &str) -> WideDecimal {
    prices.get(s).cloned().unwrap_or_default()
}

/// `build_invest_plan(target_weights=, prices=, amount=, now=)` (kind BUY).
///
/// # Errors
/// The `ValueError`'s message.
pub fn build_invest_plan(
    target_weights: &BTreeMap<String, WideDecimal>,
    prices: &BTreeMap<String, WideDecimal>,
    amount: &WideDecimal,
    now: DateTime<FixedOffset>,
) -> Result<Plan, String> {
    if !positive(amount) {
        return Err("amount must be positive".to_owned());
    }
    if target_weights.is_empty() {
        return Err("target_weights cannot be empty".to_owned());
    }
    let symbols: Vec<String> = target_weights.keys().cloned().collect();
    missing_prices(&symbols, prices)?;
    let weights: Vec<WideDecimal> = symbols.iter().map(|s| target_weights[s].clone()).collect();
    let price_list: Vec<WideDecimal> = symbols.iter().map(|s| price_of(prices, s)).collect();
    let qtys = shares_at_amount(amount, &price_list, &weights)?;
    let legs = symbols
        .iter()
        .zip(qtys)
        .filter(|(_, q)| is_positive(q))
        .map(|(s, q)| Leg {
            symbol: s.clone(),
            side: "BUY",
            quantity: q,
            ref_price: price_of(prices, s),
        })
        .collect();
    Ok(Plan {
        kind: "BUY",
        legs,
        requested_amount: amount.clone(),
        expires_at_hint: expires(now),
    })
}

/// `build_apply_plan(holdings=, target_weights=, prices=, amount=, now=)` (kind REBALANCE).
///
/// # Errors
/// The `ValueError`'s message.
pub fn build_apply_plan(
    holdings: &BTreeMap<String, BigInt>,
    target_weights: &BTreeMap<String, WideDecimal>,
    prices: &BTreeMap<String, WideDecimal>,
    amount: &WideDecimal,
    now: DateTime<FixedOffset>,
) -> Result<Plan, String> {
    if !positive(amount) {
        return Err("amount must be positive".to_owned());
    }
    if target_weights.is_empty() {
        return Err("target_weights cannot be empty".to_owned());
    }
    let symbols: Vec<String> = holdings
        .keys()
        .chain(target_weights.keys())
        .cloned()
        .collect::<BTreeSet<String>>()
        .into_iter()
        .collect();
    missing_prices(&symbols, prices)?;
    let positive_symbols: Vec<String> = symbols
        .iter()
        .filter(|s| target_weights.get(*s).is_some_and(positive))
        .cloned()
        .collect();
    if positive_symbols.is_empty() {
        return Err("target_weights must include at least one positive weight".to_owned());
    }
    let weights: Vec<WideDecimal> = positive_symbols
        .iter()
        .map(|s| target_weights[s].clone())
        .collect();
    let price_list: Vec<WideDecimal> = positive_symbols
        .iter()
        .map(|s| price_of(prices, s))
        .collect();
    let targets: BTreeMap<String, BigInt> = positive_symbols
        .into_iter()
        .zip(shares_at_amount(amount, &price_list, &weights)?)
        .collect();
    let mut legs = Vec::new();
    for s in &symbols {
        let current = holdings.get(s).cloned().unwrap_or_default();
        let target = targets.get(s).cloned().unwrap_or_default();
        let delta = target - current;
        if is_positive(&delta) {
            legs.push(Leg {
                symbol: s.clone(),
                side: "BUY",
                quantity: delta,
                ref_price: price_of(prices, s),
            });
        } else if delta.sign() == Sign::Minus {
            legs.push(Leg {
                symbol: s.clone(),
                side: "SELL",
                quantity: -delta,
                ref_price: price_of(prices, s),
            });
        }
    }
    Ok(Plan {
        kind: "REBALANCE",
        legs,
        requested_amount: amount.clone(),
        expires_at_hint: expires(now),
    })
}

/// `build_customize_plan(...)`: `build_apply_plan`'s legs, labelled CUSTOMIZE.
///
/// # Errors
/// The `ValueError`'s message.
pub fn build_customize_plan(
    holdings: &BTreeMap<String, BigInt>,
    target_weights: &BTreeMap<String, WideDecimal>,
    prices: &BTreeMap<String, WideDecimal>,
    amount: &WideDecimal,
    now: DateTime<FixedOffset>,
) -> Result<Plan, String> {
    let mut plan = build_apply_plan(holdings, target_weights, prices, amount, now)?;
    plan.kind = "CUSTOMIZE";
    Ok(plan)
}

/// `build_exit_plan(holdings=, prices=, now=, requested_amount=)` (kind EXIT).
///
/// # Errors
/// The `ValueError`'s message.
pub fn build_exit_plan(
    holdings: &BTreeMap<String, BigInt>,
    prices: &BTreeMap<String, WideDecimal>,
    now: DateTime<FixedOffset>,
    requested_amount: Option<WideDecimal>,
) -> Result<Plan, String> {
    if holdings.is_empty() {
        return Err("holdings cannot be empty".to_owned());
    }
    let symbols: Vec<String> = holdings
        .iter()
        .filter(|(_, q)| is_positive(q))
        .map(|(s, _)| s.clone())
        .collect();
    if symbols.is_empty() {
        return Err("holdings must include at least one positive quantity".to_owned());
    }
    missing_prices(&symbols, prices)?;
    let requested = match requested_amount {
        Some(a) => a,
        None => {
            let mut total: WideDecimal = "0".parse().map_err(|_| "0".to_owned())?;
            for s in &symbols {
                let qty: WideDecimal = holdings[s]
                    .to_string()
                    .parse()
                    .map_err(|_| "qty".to_owned())?;
                total = total.add(&qty.mul(&price_of(prices, s)));
            }
            total
        }
    };
    let legs = symbols
        .iter()
        .map(|s| Leg {
            symbol: s.clone(),
            side: "SELL",
            quantity: holdings[s].clone(),
            ref_price: price_of(prices, s),
        })
        .collect();
    Ok(Plan {
        kind: "EXIT",
        legs,
        requested_amount: requested,
        expires_at_hint: expires(now),
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(s: &str) -> WideDecimal {
        s.parse().unwrap()
    }

    fn ist(y: i32, m: u32, day: u32, h: u32, min: u32) -> DateTime<FixedOffset> {
        FixedOffset::east_opt(19_800)
            .unwrap()
            .with_ymd_and_hms(y, m, day, h, min, 0)
            .unwrap()
    }

    fn map(pairs: &[(&str, &str)]) -> BTreeMap<String, WideDecimal> {
        pairs.iter().map(|(k, v)| ((*k).to_owned(), d(v))).collect()
    }

    /// Answers dumped from the live Python (`baskfy_core.curated_plans` / `market_hours_cb`,
    /// 30 Oct 2026) for the inputs below.
    #[test]
    fn quantities_beyond_28_digits_are_pythons_ints() {
        // `build_invest_plan` in the live Python (review): `to_integral_value` has no
        // precision limit, so these are integers, not `InvalidOperation`.
        let now = ist(2026, 10, 30, 10, 0);
        let qty = |amount: &str, w: &str, p: &str| {
            build_invest_plan(&map(&[("A", w)]), &map(&[("A", p)]), &d(amount), now).map(|plan| {
                plan.legs
                    .iter()
                    .map(|l| l.quantity.to_string())
                    .collect::<Vec<_>>()
            })
        };
        assert_eq!(
            qty("1.5E+29", "1", "1").unwrap(),
            vec!["150000000000000000000000000000"]
        );
        assert_eq!(
            qty("1E+30", "0.5", "3").unwrap(),
            vec!["166666666666666666666666666700"]
        );
        assert_eq!(qty("12345.6789", "0.3333", "7.77").unwrap(), vec!["529"]);
        assert_eq!(
            qty("99999999999999999999999999999999", "1", "0.01").unwrap(),
            vec!["10000000000000000000000000000000000"]
        );
        // Beyond 128 bits Python's int is still exact (R8.ar1): `int(Decimal("1E+45"))`.
        assert_eq!(
            qty("1E+45", "1", "1").unwrap(),
            vec!["1000000000000000000000000000000000000000000000"]
        );
        assert_eq!(to_int(&d("-7.9")).unwrap().to_string(), "-7");
        assert_eq!(to_int(&d("0E-7")).unwrap().to_string(), "0");
        assert_eq!(to_int(&d("0.999")).unwrap().to_string(), "0");
    }

    #[test]
    fn plans_match_pythons_answers() {
        let now = ist(2026, 10, 30, 10, 0);
        let plan = build_invest_plan(
            &map(&[("TCS", "0.6"), ("INFY", "0.4")]),
            &map(&[("TCS", "3412.55"), ("INFY", "1500")]),
            &d("100000"),
            now,
        )
        .unwrap();
        let legs: Vec<(String, &str, i128)> = plan
            .legs
            .iter()
            .map(|l| {
                (
                    l.symbol.clone(),
                    l.side,
                    l.quantity.to_string().parse().unwrap(),
                )
            })
            .collect();
        assert_eq!(
            legs,
            vec![
                ("INFY".to_owned(), "BUY", 26),
                ("TCS".to_owned(), "BUY", 17)
            ]
        );
        assert_eq!(
            crate::wire::py_datetime_fixed(plan.expires_at_hint),
            "2026-10-30T10:30:00+05:30"
        );
        let holdings: BTreeMap<String, BigInt> = [
            ("TCS".to_owned(), BigInt::from(30)),
            ("WIPRO".to_owned(), BigInt::from(10)),
        ]
        .into();
        let apply = build_apply_plan(
            &holdings,
            &map(&[("TCS", "0.5"), ("INFY", "0.5")]),
            &map(&[("TCS", "3000"), ("INFY", "1500"), ("WIPRO", "250")]),
            &d("120000"),
            now,
        )
        .unwrap();
        let legs: Vec<(String, &str, i128)> = apply
            .legs
            .iter()
            .map(|l| {
                (
                    l.symbol.clone(),
                    l.side,
                    l.quantity.to_string().parse().unwrap(),
                )
            })
            .collect();
        assert_eq!(
            legs,
            vec![
                ("INFY".to_owned(), "BUY", 40),
                ("TCS".to_owned(), "SELL", 10),
                ("WIPRO".to_owned(), "SELL", 10)
            ]
        );
        let exit = build_exit_plan(
            &holdings,
            &map(&[("TCS", "3000.50"), ("WIPRO", "250")]),
            now,
            None,
        )
        .unwrap();
        assert_eq!(exit.requested_amount.to_sci_string(), "92515.00");
        assert_eq!(
            build_invest_plan(&map(&[("A", "0")]), &map(&[("A", "1")]), &d("1"), now).unwrap_err(),
            "price and weight must be positive"
        );
        assert_eq!(
            build_invest_plan(&map(&[("A", "1"), ("B", "1")]), &map(&[]), &d("1"), now)
                .unwrap_err(),
            "missing prices for: A, B"
        );
    }

    /// R7.15 / R8.ar1: `probe-exit-huge-int` in the session. Python's answer
    /// (`build_exit_plan(holdings={"A": 123456789012345678901234567890123456789012, "B": -5},
    /// prices={"A": Decimal(1), "B": Decimal("1E+5")}, ...)`): one SELL leg of the exact 42-digit
    /// quantity, and `requested_amount` rounded to the context's 28 digits.
    #[test]
    fn an_exit_beyond_128_bits_is_pythons_int() {
        let now = ist(2026, 10, 30, 12, 36);
        let huge: BigInt = "123456789012345678901234567890123456789012"
            .parse()
            .unwrap();
        let holdings: BTreeMap<String, BigInt> = [
            ("A".to_owned(), huge.clone()),
            ("B".to_owned(), BigInt::from(-5)),
        ]
        .into();
        let exit =
            build_exit_plan(&holdings, &map(&[("A", "1"), ("B", "1E+5")]), now, None).unwrap();
        assert_eq!(exit.legs.len(), 1);
        assert_eq!(exit.legs[0].quantity, huge);
        assert_eq!(
            exit.requested_amount.to_sci_string(),
            "1.234567890123456789012345679E+41"
        );
        // And a SELL delta beyond 128 bits in an apply plan.
        let apply = build_apply_plan(
            &holdings,
            &map(&[("C", "1")]),
            &map(&[("A", "1"), ("B", "1"), ("C", "1")]),
            &d("10"),
            now,
        )
        .unwrap();
        let legs: Vec<(String, &str, String)> = apply
            .legs
            .iter()
            .map(|l| (l.symbol.clone(), l.side, l.quantity.to_string()))
            .collect();
        assert_eq!(
            legs,
            vec![
                ("A".to_owned(), "SELL", huge.to_string()),
                ("B".to_owned(), "BUY", "5".to_owned()),
                ("C".to_owned(), "BUY", "10".to_owned()),
            ]
        );
    }

    #[test]
    fn the_session_gate_is_inclusive_and_the_next_open_is_a_trading_day() {
        let dates: BTreeSet<NaiveDate> = [
            NaiveDate::from_ymd_opt(2026, 10, 30).unwrap(),
            NaiveDate::from_ymd_opt(2026, 11, 2).unwrap(),
        ]
        .into();
        assert!(is_nse_session_open(ist(2026, 10, 30, 9, 15), &dates));
        assert!(is_nse_session_open(ist(2026, 10, 30, 15, 30), &dates));
        assert!(!is_nse_session_open(ist(2026, 10, 30, 15, 31), &dates));
        assert_eq!(
            crate::wire::py_datetime_fixed(
                next_session_open(ist(2026, 10, 30, 2, 44), &dates).unwrap()
            ),
            "2026-10-30T09:15:00+05:30"
        );
        assert_eq!(
            crate::wire::py_datetime_fixed(
                next_session_open(ist(2026, 10, 30, 16, 0), &dates).unwrap()
            ),
            "2026-11-02T09:15:00+05:30"
        );
        assert_eq!(
            next_session_open(ist(2026, 11, 2, 16, 0), &dates).unwrap_err(),
            "no trading day after 2026-11-02 in the provided calendar"
        );
        assert_eq!(
            next_session_open(ist(2026, 11, 2, 16, 0), &BTreeSet::new()).unwrap_err(),
            "trading_dates cannot be empty"
        );
    }
}
