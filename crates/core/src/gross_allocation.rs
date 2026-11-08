//! Equal gross-exposure basket allocation.
//!
//! `B` is the investor's cash, not buying power. `R` is an explicit reserve and `F` an explicit
//! cost estimate. Cash for positions is `C = B − R − F`. With flat margin the one position value
//! shared by every name is `G = C / Σ(1/Lᵢ)`. Quantity is `floor(G / price)` in lots. Leverage
//! changes the cash each name needs. It is never applied to the share count a second time.
//!
//! This module is the only allocation. Preview and order submission both consume its result.

use crate::decimal::{Rounding, WideDecimal};

/// Algorithm id stored on a screen-invest plan.
pub const ALLOCATION_VERSION: &str = "gross-v1";

/// One name to size. `leverage` is 1 for cash. `lot_size` is the board lot, at least 1.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AllocName {
    /// Symbol.
    pub symbol: String,
    /// Estimated executable price, per share.
    pub price: WideDecimal,
    /// Verified MTF leverage. 1 means the whole position is the investor's cash.
    pub leverage: WideDecimal,
    /// Shares per lot. NSE cash is 1.
    pub lot_size: i64,
    /// Cap on this name's position value, when a risk limit says so.
    pub max_gross: Option<WideDecimal>,
}

/// Cash budget and the names that share it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AllocInput {
    /// Investor cash for this basket (`B`).
    pub budget: WideDecimal,
    /// Explicit reserve (`R`). Zero when none was configured.
    pub reserve: WideDecimal,
    /// Explicit estimated costs (`F`). Zero when none were supplied.
    pub costs: WideDecimal,
    /// Selected names, in display order.
    pub names: Vec<AllocName>,
}

/// One sized line. `contribution + funding == gross` at paise.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AllocLine {
    /// Symbol.
    pub symbol: String,
    /// Price used to size.
    pub price: WideDecimal,
    /// Leverage used to size.
    pub leverage: WideDecimal,
    /// Board lot.
    pub lot_size: i64,
    /// Whole shares. Zero when the basket could not be filled.
    pub quantity: i64,
    /// `quantity * price`.
    pub gross: WideDecimal,
    /// Cash this name needs from the investor.
    pub contribution: WideDecimal,
    /// `gross - contribution`. Zero when leverage is 1.
    pub funding: WideDecimal,
    /// `gross - target`. Zero or negative after flooring to a lot.
    pub deviation: WideDecimal,
}

/// The basket, or a shortfall that bought nothing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AllocOutput {
    /// Common position value before lot flooring.
    pub target: WideDecimal,
    /// `B - R - F`.
    pub cash_available: WideDecimal,
    /// Sized names. Present, at zero quantity, when the budget cannot fill every name.
    pub lines: Vec<AllocLine>,
    /// Sum of position values.
    pub gross_total: WideDecimal,
    /// Sum of investor cash.
    pub contribution_total: WideDecimal,
    /// Sum of broker funding.
    pub funding_total: WideDecimal,
    /// `C` not used after lot flooring. Not pushed onto a leftover name.
    pub unallocated: WideDecimal,
    /// Smallest budget that buys one lot of every name at this leverage mix.
    pub minimum_budget: WideDecimal,
    /// Why the common target was lowered. Names stay equal.
    pub constraint: Option<String>,
    /// Why nothing was bought. Names are not dropped to spend the rest.
    pub shortfall: Option<String>,
}

fn err(e: crate::decimal::DecimalError) -> String {
    e.to_string()
}

fn n(units: i64) -> WideDecimal {
    WideDecimal::from(units)
}

fn paise(value: &WideDecimal) -> Result<WideDecimal, String> {
    value.quantize(-2, Rounding::HalfEven).map_err(err)
}

fn paise_down(value: &WideDecimal) -> Result<WideDecimal, String> {
    value.quantize(-2, Rounding::Down).map_err(err)
}

fn paise_up(value: &WideDecimal) -> Result<WideDecimal, String> {
    let down = paise_down(value)?;
    if &down < value {
        let tick = n(1).div(&n(100)).map_err(err)?;
        Ok(down.add(&tick))
    } else {
        Ok(down)
    }
}

fn whole_down(value: &WideDecimal) -> Result<i64, String> {
    let q = value.quantize(0, Rounding::Down).map_err(err)?;
    q.to_sci_string()
        .parse::<i64>()
        .map_err(|_| "quantity overflow".to_owned())
}

fn rupee_text(value: &WideDecimal) -> String {
    paise(value).map_or_else(|_| value.to_sci_string(), |v| v.to_sci_string())
}

fn cash_available(input: &AllocInput) -> Result<WideDecimal, String> {
    if input.budget.is_sign_negative() || input.budget.is_zero() {
        return Err(format!(
            "an investment amount must be positive; got {}",
            input.budget.to_sci_string()
        ));
    }
    if input.reserve.is_sign_negative() {
        return Err("reserve cannot be negative".to_owned());
    }
    if input.costs.is_sign_negative() {
        return Err("costs cannot be negative".to_owned());
    }
    let raw = input.budget.sub(&input.reserve).sub(&input.costs);
    if raw.is_sign_negative() {
        return Ok(WideDecimal::zero());
    }
    paise(&raw)
}

fn sum_inv(names: &[AllocName]) -> Result<WideDecimal, String> {
    let mut total = WideDecimal::zero();
    for name in names {
        if name.leverage.is_sign_negative() || name.leverage.is_zero() || name.leverage < n(1) {
            return Err(format!(
                "{} leverage must be at least 1; got {}",
                name.symbol,
                name.leverage.to_sci_string()
            ));
        }
        if name.price.is_sign_negative() || name.price.is_zero() {
            return Err(format!("{} needs a positive price", name.symbol));
        }
        if name.lot_size < 1 {
            return Err(format!("{} lot size must be at least 1", name.symbol));
        }
        if name
            .max_gross
            .as_ref()
            .is_some_and(|cap| cap.is_sign_negative() || cap.is_zero())
        {
            return Err(format!("{} position cap must be positive", name.symbol));
        }
        total = total.add(&n(1).div(&name.leverage).map_err(err)?);
    }
    Ok(total)
}

fn lot_cost(name: &AllocName) -> Result<WideDecimal, String> {
    Ok(paise(&name.price)?.mul(&n(name.lot_size)))
}

fn tightest_cap(names: &[AllocName]) -> Option<(String, WideDecimal)> {
    let mut cap: Option<(String, WideDecimal)> = None;
    for name in names {
        let Some(max) = name.max_gross.clone() else {
            continue;
        };
        cap = Some(match cap {
            Some((symbol, current)) if current <= max => (symbol, current),
            _ => (name.symbol.clone(), max),
        });
    }
    cap
}

fn minimum_budget(
    names: &[AllocName],
    sum_inverse: &WideDecimal,
    input: &AllocInput,
) -> Result<WideDecimal, String> {
    let mut highest = lot_cost(&names[0])?;
    for name in names.iter().skip(1) {
        let cost = lot_cost(name)?;
        if cost > highest {
            highest = cost;
        }
    }
    let cash = paise(&highest.mul(sum_inverse))?;
    paise(&cash.add(&input.reserve).add(&input.costs))
}

fn zero_lines(names: &[AllocName], target: &WideDecimal) -> Result<Vec<AllocLine>, String> {
    let mut lines = Vec::with_capacity(names.len());
    for name in names {
        lines.push(AllocLine {
            symbol: name.symbol.clone(),
            price: paise(&name.price)?,
            leverage: name.leverage.clone(),
            lot_size: name.lot_size,
            quantity: 0,
            gross: paise(&WideDecimal::zero())?,
            contribution: paise(&WideDecimal::zero())?,
            funding: paise(&WideDecimal::zero())?,
            deviation: paise(&WideDecimal::zero().sub(target))?,
        });
    }
    Ok(lines)
}

fn empty_output(
    cash: &WideDecimal,
    target: &WideDecimal,
    minimum: &WideDecimal,
    lines: Vec<AllocLine>,
    constraint: Option<String>,
    shortfall: String,
) -> AllocOutput {
    AllocOutput {
        target: target.clone(),
        cash_available: cash.clone(),
        lines,
        gross_total: WideDecimal::zero(),
        contribution_total: WideDecimal::zero(),
        funding_total: WideDecimal::zero(),
        unallocated: cash.clone(),
        minimum_budget: minimum.clone(),
        constraint,
        shortfall: Some(shortfall),
    }
}

struct TargetChoice {
    target: WideDecimal,
    constraint: Option<String>,
}

fn choose_target(
    cash: &WideDecimal,
    sum_inverse: &WideDecimal,
    names: &[AllocName],
) -> Result<TargetChoice, String> {
    let mut target = cash.div(sum_inverse).map_err(err)?;
    let Some((symbol, cap)) = tightest_cap(names) else {
        return Ok(TargetChoice {
            target,
            constraint: None,
        });
    };
    if cap >= target {
        return Ok(TargetChoice {
            target,
            constraint: None,
        });
    }
    target = cap.clone();
    Ok(TargetChoice {
        target,
        constraint: Some(format!(
            "{symbol} is capped at ₹{}, so every name uses that same position value",
            rupee_text(&cap)
        )),
    })
}

fn line_at(name: &AllocName, target: &WideDecimal) -> Result<AllocLine, String> {
    let price = paise(&name.price)?;
    let lots = whole_down(&target.div(&lot_cost(name)?).map_err(err)?)?;
    let quantity = lots
        .checked_mul(name.lot_size)
        .ok_or_else(|| format!("{} quantity overflow", name.symbol))?;
    let gross = paise_down(&price.mul(&n(quantity)))?;
    let contribution = paise_down(&gross.div(&name.leverage).map_err(err)?)?;
    let funding = gross.sub(&contribution);
    Ok(AllocLine {
        symbol: name.symbol.clone(),
        price,
        leverage: name.leverage.clone(),
        lot_size: name.lot_size,
        quantity,
        gross: gross.clone(),
        contribution,
        funding,
        deviation: gross.sub(target),
    })
}

fn finish(
    cash: &WideDecimal,
    target: &WideDecimal,
    minimum: &WideDecimal,
    lines: Vec<AllocLine>,
    constraint: Option<String>,
) -> Result<AllocOutput, String> {
    let mut gross_total = WideDecimal::zero();
    let mut contribution_total = WideDecimal::zero();
    let mut funding_total = WideDecimal::zero();
    for line in &lines {
        gross_total = gross_total.add(&line.gross);
        contribution_total = contribution_total.add(&line.contribution);
        funding_total = funding_total.add(&line.funding);
    }
    let gross_total = paise(&gross_total)?;
    let contribution_total = paise(&contribution_total)?;
    let funding_total = paise(&funding_total)?;
    if contribution_total > *cash {
        return Err("sized contributions exceed the cash set aside for positions".to_owned());
    }
    Ok(AllocOutput {
        target: target.clone(),
        cash_available: cash.clone(),
        lines,
        gross_total,
        contribution_total: contribution_total.clone(),
        funding_total,
        unallocated: cash.sub(&contribution_total),
        minimum_budget: minimum.clone(),
        constraint,
        shortfall: None,
    })
}

/// Size every name to one gross target. Flat margin: contribution is `gross / leverage`.
///
/// # Errors
/// A non-positive budget, price, leverage, or lot, or a negative reserve or cost.
pub fn allocate_equal_gross(input: &AllocInput) -> Result<AllocOutput, String> {
    if input.names.is_empty() {
        return Err("a basket needs at least one name".to_owned());
    }
    let cash = cash_available(input)?;
    let inverse = sum_inv(&input.names)?;
    let minimum = minimum_budget(&input.names, &inverse, input)?;
    if cash.is_zero() {
        let lines = zero_lines(&input.names, &WideDecimal::zero())?;
        return Ok(empty_output(
            &cash,
            &WideDecimal::zero(),
            &minimum,
            lines,
            None,
            format!(
                "Reserve ₹{} and estimated costs ₹{} leave no cash for positions.",
                rupee_text(&input.reserve),
                rupee_text(&input.costs)
            ),
        ));
    }
    let choice = choose_target(&cash, &inverse, &input.names)?;
    let mut blocking: Option<&AllocName> = None;
    for name in &input.names {
        if lot_cost(name)? > choice.target {
            blocking = Some(name);
            break;
        }
    }
    if let Some(name) = blocking {
        let lines = zero_lines(&input.names, &choice.target)?;
        let because_cap = choice
            .constraint
            .as_ref()
            .map_or(String::new(), |note| format!(" {note}."));
        return Ok(empty_output(
            &cash,
            &choice.target,
            &minimum,
            lines,
            choice.constraint,
            format!(
                "This budget cannot buy one lot of every selected name at the same position value.{because_cap} One lot of {} is ₹{} and the equal target is ₹{}. ₹{} is the least that fills every name. No name was dropped to spend the rest.",
                name.symbol,
                rupee_text(&lot_cost(name)?),
                rupee_text(&choice.target),
                rupee_text(&minimum)
            ),
        ));
    }
    let mut lines = Vec::with_capacity(input.names.len());
    for name in &input.names {
        let line = line_at(name, &choice.target)?;
        if line.quantity < name.lot_size {
            let lines = zero_lines(&input.names, &choice.target)?;
            return Ok(empty_output(
                &cash,
                &choice.target,
                &minimum,
                lines,
                choice.constraint,
                format!(
                    "This budget cannot buy one lot of every selected name at the same position value. ₹{} is the least that fills every name.",
                    rupee_text(&minimum)
                ),
            ));
        }
        lines.push(line);
    }
    finish(&cash, &choice.target, &minimum, lines, choice.constraint)
}

fn apply_required<F>(
    output: &AllocOutput,
    input: &AllocInput,
    mut required_cash: F,
) -> Result<AllocOutput, String>
where
    F: FnMut(&str, i64, &WideDecimal) -> Result<WideDecimal, String>,
{
    let mut lines = Vec::with_capacity(output.lines.len());
    let mut contribution_total = WideDecimal::zero();
    let mut funding_total = WideDecimal::zero();
    for line in &output.lines {
        let required = paise_up(&required_cash(&line.symbol, line.quantity, &line.price)?)?;
        if required.is_sign_negative() {
            return Err(format!("{} broker margin cannot be negative", line.symbol));
        }
        let funding = line.gross.sub(&required);
        contribution_total = contribution_total.add(&required);
        funding_total = funding_total.add(&funding);
        let mut next = line.clone();
        next.contribution = required;
        next.funding = funding;
        lines.push(next);
    }
    let contribution_total = paise(&contribution_total)?;
    let funding_total = paise(&funding_total)?;
    let spend = contribution_total.add(&input.reserve).add(&input.costs);
    if spend > input.budget {
        return Err("broker margin exceeds the budget".to_owned());
    }
    Ok(AllocOutput {
        contribution_total: contribution_total.clone(),
        funding_total,
        unallocated: output.cash_available.sub(&contribution_total),
        lines,
        ..output.clone()
    })
}

fn size_at(input: &AllocInput, target: &WideDecimal) -> Result<Option<AllocOutput>, String> {
    for name in &input.names {
        if lot_cost(name)? > *target {
            return Ok(None);
        }
    }
    let cash = cash_available(input)?;
    let inverse = sum_inv(&input.names)?;
    let minimum = minimum_budget(&input.names, &inverse, input)?;
    let mut lines = Vec::with_capacity(input.names.len());
    for name in &input.names {
        let line = line_at(name, target)?;
        if line.quantity < 1 {
            return Ok(None);
        }
        lines.push(line);
    }
    Ok(Some(finish(&cash, target, &minimum, lines, None)?))
}

fn to_paise(value: &WideDecimal) -> Result<i64, String> {
    whole_down(&value.mul(&n(100)))
}

fn from_paise(paise_units: i64) -> Result<WideDecimal, String> {
    WideDecimal::from(paise_units).div(&n(100)).map_err(err)
}

/// Flat sizing, then broker cash for those quantities.
///
/// `required_cash(symbol, quantity, price)` is the broker's own-funds figure for that order.
/// When it fits, quantities stay. When it does not, the common target is lowered until the
/// broker's cash, costs, and reserve fit in the budget, or the basket reports a shortfall.
/// Names are not resized independently.
///
/// # Errors
/// The same input errors as [`allocate_equal_gross`], or a negative broker figure.
pub fn allocate_with_required<F>(
    input: &AllocInput,
    mut required_cash: F,
) -> Result<AllocOutput, String>
where
    F: FnMut(&str, i64, &WideDecimal) -> Result<WideDecimal, String>,
{
    let flat = allocate_equal_gross(input)?;
    if flat.shortfall.is_some() {
        return Ok(flat);
    }
    if let Ok(fitted) = apply_required(&flat, input, &mut required_cash) {
        return Ok(fitted);
    }
    let mut lo = 0_i64;
    for name in &input.names {
        let floor = to_paise(&lot_cost(name)?)?;
        if floor > lo {
            lo = floor;
        }
    }
    let mut hi = to_paise(&flat.target)?;
    let mut best: Option<AllocOutput> = None;
    while lo <= hi {
        let mid = lo + (hi - lo) / 2;
        let target = from_paise(mid)?;
        let Some(trial) = size_at(input, &target)? else {
            lo = mid.saturating_add(1);
            continue;
        };
        if let Ok(fitted) = apply_required(&trial, input, &mut required_cash) {
            best = Some(fitted);
            lo = mid.saturating_add(1);
        } else if mid == 0 {
            break;
        } else {
            hi = mid - 1;
        }
    }
    let Some(mut fitted) = best else {
        let cash = cash_available(input)?;
        let inverse = sum_inv(&input.names)?;
        let minimum = minimum_budget(&input.names, &inverse, input)?;
        let lines = zero_lines(&input.names, &flat.target)?;
        return Ok(empty_output(
            &cash,
            &flat.target,
            &minimum,
            lines,
            flat.constraint.clone(),
            format!(
                "Broker margin on one lot of every name does not fit in ₹{}. No name was dropped to spend the rest.",
                rupee_text(&input.budget)
            ),
        ));
    };
    if flat.constraint.is_some() {
        fitted.constraint = flat.constraint;
    }
    Ok(fitted)
}

/// Same symbols, share counts, and leverage. Price noise that does not change a lot is not a new plan.
#[must_use]
pub fn same_executed_quantities(left: &AllocOutput, right: &AllocOutput) -> bool {
    if left.shortfall.is_some()
        || right.shortfall.is_some()
        || left.lines.len() != right.lines.len()
    {
        return false;
    }
    left.lines
        .iter()
        .zip(&right.lines)
        .all(|(a, b)| a.symbol == b.symbol && a.quantity == b.quantity && a.leverage == b.leverage)
}

/// Cash that remains affordable when broker margin came in higher than the flat estimate.
///
/// # Errors
/// A required figure that is not positive.
pub fn scaled_cash(available: &WideDecimal, required: &WideDecimal) -> Result<WideDecimal, String> {
    if required.is_sign_negative() || required.is_zero() {
        return Err("required cash must be positive".to_owned());
    }
    available.mul(available).div(required).map_err(err)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    fn d(units: i64) -> WideDecimal {
        WideDecimal::from(units)
    }

    fn name(symbol: &str, price: i64, leverage: i64, lot: i64) -> AllocName {
        AllocName {
            symbol: symbol.to_owned(),
            price: d(price),
            leverage: d(leverage),
            lot_size: lot,
            max_gross: None,
        }
    }

    fn book(budget: i64, names: Vec<AllocName>) -> AllocInput {
        AllocInput {
            budget: d(budget),
            reserve: d(0),
            costs: d(0),
            names,
        }
    }

    fn line<'a>(out: &'a AllocOutput, symbol: &str) -> &'a AllocLine {
        out.lines.iter().find(|row| row.symbol == symbol).unwrap()
    }

    fn assert_flat_invariants(input: &AllocInput, out: &AllocOutput) {
        assert!(out.shortfall.is_none(), "{:?}", out.shortfall);
        let mut gross = WideDecimal::zero();
        let mut contribution = WideDecimal::zero();
        let mut funding = WideDecimal::zero();
        for row in &out.lines {
            assert_eq!(row.contribution.add(&row.funding), row.gross);
            assert_eq!(row.price.mul(&d(row.quantity)), row.gross);
            if row.leverage == d(1) {
                assert!(row.funding.is_zero());
            }
            let gap = out.target.sub(&row.gross);
            let one_lot = row.price.mul(&d(row.lot_size));
            assert!(
                gap >= WideDecimal::zero() && gap < one_lot,
                "{gap} vs {one_lot}"
            );
            if row.leverage > d(1) {
                let shares = whole_down(&out.target.div(&row.price).unwrap()).unwrap();
                let doubled = shares.saturating_mul(whole_down(&row.leverage).unwrap().max(1));
                assert_ne!(row.quantity, doubled);
            }
            gross = gross.add(&row.gross);
            contribution = contribution.add(&row.contribution);
            funding = funding.add(&row.funding);
        }
        assert_eq!(gross, out.gross_total);
        assert_eq!(contribution, out.contribution_total);
        assert_eq!(funding, out.funding_total);
        assert_eq!(
            out.contribution_total.add(&out.funding_total),
            out.gross_total
        );
        let spend = out.contribution_total.add(&input.reserve).add(&input.costs);
        assert!(spend <= input.budget);
        assert_eq!(input.budget.sub(&spend), out.unallocated);
        if out.gross_total > d(0) {
            let mut weight = WideDecimal::zero();
            for row in &out.lines {
                weight = weight.add(&row.gross.div(&out.gross_total).unwrap());
            }
            let drift = weight.sub(&d(1)).abs();
            assert!(drift < w("0.0001"));
        }
    }

    fn w(text: &str) -> WideDecimal {
        text.parse().unwrap()
    }

    #[test]
    fn three_names_share_one_position_value() {
        let input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 2, 1),
                name("C", 500, 4, 1),
            ],
        );
        let out = allocate_equal_gross(&input).unwrap();
        assert_flat_invariants(&input, &out);
        assert_eq!(line(&out, "A").quantity, 400);
        assert_eq!(line(&out, "B").quantity, 400);
        assert_eq!(line(&out, "C").quantity, 400);
        assert_eq!(line(&out, "A").gross, d(200_000));
        assert_eq!(line(&out, "B").gross, d(200_000));
        assert_eq!(line(&out, "C").gross, d(200_000));
        assert_eq!(line(&out, "A").contribution, d(200_000));
        assert_eq!(line(&out, "B").contribution, d(100_000));
        assert_eq!(line(&out, "C").contribution, d(50_000));
        assert!(line(&out, "A").funding.is_zero());
        assert_eq!(line(&out, "B").funding, d(100_000));
        assert_eq!(line(&out, "C").funding, d(150_000));
        assert_eq!(out.gross_total, d(600_000));
        assert_eq!(out.contribution_total, d(350_000));
        assert_eq!(out.funding_total, d(250_000));
        assert!(out.unallocated.is_zero());
        let equal_cash_qty = |lev: i64| (116_666 / 500) * lev;
        assert_ne!(line(&out, "B").quantity, equal_cash_qty(2));
        assert_ne!(line(&out, "C").quantity, equal_cash_qty(4));
        assert_ne!(line(&out, "C").quantity, 400 * 4);
    }

    #[test]
    fn twenty_mixed_names_stay_near_five_percent() {
        let levers = [1_i64, 2, 4, 5];
        let names = (0..20)
            .map(|i| name(&format!("S{i:02}"), 80, levers[i % 4], 1))
            .collect();
        let input = book(1_000_000, names);
        let out = allocate_equal_gross(&input).unwrap();
        assert_flat_invariants(&input, &out);
        assert_eq!(out.lines.len(), 20);
        let gross = out.lines[0].gross.clone();
        for row in &out.lines {
            assert_eq!(row.gross, gross);
            let weight = row.gross.div(&out.gross_total).unwrap();
            assert_eq!(weight, w("0.05"));
            if row.leverage == d(1) {
                assert!(row.funding.is_zero());
            }
        }
    }

    #[test]
    fn cash_only_names_split_the_budget_and_take_no_funding() {
        let input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 1, 1),
                name("C", 500, 1, 1),
            ],
        );
        let out = allocate_equal_gross(&input).unwrap();
        assert_flat_invariants(&input, &out);
        assert!(out.funding_total.is_zero());
        assert_eq!(line(&out, "A").quantity, line(&out, "B").quantity);
        assert_eq!(line(&out, "B").quantity, line(&out, "C").quantity);
        assert_eq!(line(&out, "A").contribution, line(&out, "A").gross);
    }

    #[test]
    fn different_prices_and_lot_sizes_stay_within_one_lot_of_the_target() {
        let input = book(
            350_000,
            vec![
                name("A", 500, 1, 5),
                name("B", 125, 2, 4),
                name("C", 40, 4, 10),
            ],
        );
        let out = allocate_equal_gross(&input).unwrap();
        assert_flat_invariants(&input, &out);
        assert_eq!(line(&out, "A").quantity % 5, 0);
        assert_eq!(line(&out, "B").quantity % 4, 0);
        assert_eq!(line(&out, "C").quantity % 10, 0);
        assert_eq!(line(&out, "A").quantity, 400);
    }

    #[test]
    fn reserve_and_costs_come_out_of_the_budget_before_the_target() {
        let mut input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 2, 1),
                name("C", 500, 4, 1),
            ],
        );
        input.reserve = d(10_000);
        input.costs = d(5_000);
        let out = allocate_equal_gross(&input).unwrap();
        assert_flat_invariants(&input, &out);
        assert_eq!(out.cash_available, d(335_000));
        assert_eq!(line(&out, "A").quantity, 382);
        assert_eq!(line(&out, "B").quantity, 382);
        assert_eq!(line(&out, "C").quantity, 382);
        assert_eq!(out.contribution_total, d(334_250));
        assert_eq!(out.unallocated, d(750));
    }

    #[test]
    fn a_short_budget_buys_nothing_instead_of_dropping_the_expensive_name() {
        let input = book(
            5_000,
            vec![name("CHEAP", 100, 1, 1), name("DEAR", 10_000, 4, 1)],
        );
        let out = allocate_equal_gross(&input).unwrap();
        let reason = out.shortfall.unwrap();
        assert!(reason.contains("DEAR"));
        assert!(reason.contains("No name was dropped"));
        assert!(out.lines.iter().all(|row| row.quantity == 0));
        assert!(out.gross_total.is_zero());
    }

    #[test]
    fn a_position_cap_lowers_every_name_together() {
        let mut input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 2, 1),
                name("C", 500, 4, 1),
            ],
        );
        input.names[0].max_gross = Some(d(100_000));
        let out = allocate_equal_gross(&input).unwrap();
        assert_flat_invariants(&input, &out);
        assert!(out.constraint.as_deref().unwrap().contains('A'));
        assert_eq!(line(&out, "A").gross, d(100_000));
        assert_eq!(line(&out, "B").gross, d(100_000));
        assert_eq!(line(&out, "C").gross, d(100_000));
        assert_eq!(out.contribution_total, d(175_000));
        assert_eq!(out.unallocated, d(175_000));
    }

    #[test]
    fn a_cap_below_one_lot_is_a_shortfall() {
        let mut input = book(350_000, vec![name("A", 500, 1, 1), name("B", 500, 2, 1)]);
        input.names[0].max_gross = Some(d(100));
        let out = allocate_equal_gross(&input).unwrap();
        assert!(out.shortfall.is_some());
        assert!(out.lines.iter().all(|row| row.quantity == 0));
    }

    #[test]
    fn the_same_inputs_produce_the_same_quantities() {
        let input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 2, 1),
                name("C", 500, 4, 1),
            ],
        );
        let first = allocate_equal_gross(&input).unwrap();
        let second = allocate_equal_gross(&input).unwrap();
        assert!(same_executed_quantities(&first, &second));
    }

    #[test]
    fn a_price_move_that_changes_a_lot_is_a_different_plan() {
        let input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 2, 1),
                name("C", 500, 4, 1),
            ],
        );
        let priced = allocate_equal_gross(&input).unwrap();
        let mut moved = input.clone();
        moved.names[2].price = d(620);
        let next = allocate_equal_gross(&moved).unwrap();
        assert!(!same_executed_quantities(&priced, &next));
    }

    #[test]
    fn a_leverage_change_is_a_different_plan() {
        let input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 2, 1),
                name("C", 500, 4, 1),
            ],
        );
        let priced = allocate_equal_gross(&input).unwrap();
        let mut moved = input.clone();
        moved.names[2].leverage = d(2);
        let next = allocate_equal_gross(&moved).unwrap();
        assert!(!same_executed_quantities(&priced, &next));
        assert_ne!(
            line(&next, "C").contribution,
            line(&priced, "C").contribution
        );
    }

    #[test]
    fn broker_margin_above_the_flat_model_lowers_the_common_target() {
        let input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 2, 1),
                name("C", 500, 4, 1),
            ],
        );
        let out =
            allocate_with_required(&input, |_symbol, qty, price| Ok(price.mul(&d(qty)))).unwrap();
        assert!(out.shortfall.is_none());
        assert_eq!(line(&out, "A").quantity, 233);
        assert_eq!(line(&out, "B").quantity, 233);
        assert_eq!(line(&out, "C").quantity, 233);
        assert!(out.contribution_total.add(&out.unallocated) <= input.budget);
        assert_ne!(line(&out, "C").quantity, 400 * 4);
    }

    #[test]
    fn nonlinear_margin_keeps_quantities_equal() {
        let input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 2, 1),
                name("C", 500, 4, 1),
            ],
        );
        let out = allocate_with_required(&input, |_symbol, qty, price| {
            Ok(price.mul(&d(qty)).mul(&d(3)).div(&d(2)).unwrap())
        })
        .unwrap();
        assert!(out.shortfall.is_none());
        assert_eq!(line(&out, "A").quantity, line(&out, "B").quantity);
        assert_eq!(line(&out, "B").quantity, line(&out, "C").quantity);
        assert!(line(&out, "A").quantity < 400);
        assert!(out.contribution_total <= input.budget);
    }

    #[test]
    fn flat_broker_margin_does_not_change_the_acceptance_quantities() {
        let input = book(
            350_000,
            vec![
                name("A", 500, 1, 1),
                name("B", 500, 2, 1),
                name("C", 500, 4, 1),
            ],
        );
        let flat = allocate_equal_gross(&input).unwrap();
        let checked = allocate_with_required(&input, |symbol, qty, price| {
            let lev = match symbol {
                "A" => d(1),
                "B" => d(2),
                _ => d(4),
            };
            Ok(price.mul(&d(qty)).div(&lev).unwrap())
        })
        .unwrap();
        assert!(same_executed_quantities(&flat, &checked));
        assert_eq!(line(&checked, "A").quantity, 400);
    }
}
