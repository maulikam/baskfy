//! The corporate-action adjustment algorithm (docs/09 §"Adjustment algorithm"). Ports
//! `baskfy_core.adjustments`, which had no Rust home before this lane (DECISIONS-RS R7.11).
//!
//! Pure: rows in, rows out. It lives in the worker rather than `baskfy-core` only because the
//! lane owns `steps/ingest/`; nothing here touches I/O, so it can move to the core unchanged.
//!
//! # Two kinds of decimal arithmetic, both Python's
//!
//! * The **factors** (`split_factor`, `dividend_factor`, ...) are Python `Decimal` arithmetic in
//!   the default context (28 significant digits, half-even): [`WideDecimal`]'s `add`/`sub`/
//!   `mul`/`div`.
//! * The **cumulative product and the adjusted prices** are polars 1.43 `Decimal` columns:
//!   every literal is `Decimal(38, 10)` (rounded half-even to 10 places), and a product of two
//!   decimal columns is the exact product rounded **once**, half-even, to the larger of the two
//!   scales (an `Int64` column has scale 0). Both rules were measured against the installed
//!   polars (`2.5e-10 -> 2e-10`, `3.5e-10 -> 4e-10`; `Decimal(38,25) x Decimal(38,10)` keeps
//!   scale 25) and are reproduced by [`lit`] and [`pl_mul`].

use std::cmp::Ordering;

use baskfy_core::decimal::{Rounding, WideDecimal};
use chrono::NaiveDate;

use crate::error::WorkerError;

/// polars' widest decimal precision.
pub(super) const POLARS_MAX_PRECISION: usize = 38;
/// `Decimal(38, 10)`: the scale of every factor literal.
pub(super) const FACTOR_SCALE: i64 = 10;

fn one() -> WideDecimal {
    WideDecimal::from(1i64)
}

/// `pl.lit(value, dtype=pl.Decimal(38, 10))`.
pub(super) fn lit(value: &WideDecimal) -> WideDecimal {
    value.rescale_exact(-FACTOR_SCALE, Rounding::HalfEven)
}

/// The scale a polars decimal value sits at (`max(0, -exponent)`).
pub(super) fn scale_of(value: &WideDecimal) -> i64 {
    (-value.exponent()).max(0)
}

/// `a * b` for two polars decimal columns at scales `scale_a`/`scale_b`: the exact product,
/// rounded half-even once to the larger scale.
///
/// # Errors
/// `ComputeError` when the result needs more than 38 digits (polars refuses it).
pub(super) fn pl_mul(
    a: &WideDecimal,
    scale_a: i64,
    b: &WideDecimal,
    scale_b: i64,
) -> Result<WideDecimal, WorkerError> {
    let scale = scale_a.max(scale_b);
    let out = a.mul_exact(b).rescale_exact(-scale, Rounding::HalfEven);
    if out.coefficient_len() > POLARS_MAX_PRECISION {
        return Err(WorkerError::python(
            "ComputeError",
            format!("decimal multiplication overflowed precision 38: {a} * {b}"),
        ));
    }
    Ok(out)
}

/// `AdjustmentOutcome`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum AdjustmentOutcome {
    /// `applied`.
    Applied,
    /// `not_price_affecting` (a demerger; an unknown type).
    NotPriceAffecting,
    /// `insufficient_data`: recognised, but an input is missing; factor left at 1.
    InsufficientData,
    /// `invalid`: the row's ratio or amount cannot produce a sane factor.
    Invalid,
}

/// `CorporateActionInput`.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct ActionInput {
    pub(super) action_type: String,
    pub(super) ex_date: NaiveDate,
    pub(super) ratio_from: Option<WideDecimal>,
    pub(super) ratio_to: Option<WideDecimal>,
    pub(super) amount: Option<WideDecimal>,
    pub(super) issue_price: Option<WideDecimal>,
}

#[cfg(test)]
impl ActionInput {
    /// An action with no inputs beyond its type and date.
    pub(super) fn new(action_type: &str, ex_date: NaiveDate) -> Self {
        Self {
            action_type: action_type.to_owned(),
            ex_date,
            ratio_from: None,
            ratio_to: None,
            amount: None,
            issue_price: None,
        }
    }
}

/// `AppliedAction`.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct AppliedAction {
    pub(super) action: ActionInput,
    pub(super) price_factor: WideDecimal,
    pub(super) volume_factor: WideDecimal,
    pub(super) outcome: AdjustmentOutcome,
    pub(super) detail: String,
}

impl AppliedAction {
    /// `is_effective`: either factor differs from 1.
    pub(super) fn is_effective(&self) -> bool {
        self.price_factor != one() || self.volume_factor != one()
    }

    fn unit(action: &ActionInput, outcome: AdjustmentOutcome, detail: String) -> Self {
        Self {
            action: action.clone(),
            price_factor: one(),
            volume_factor: one(),
            outcome,
            detail,
        }
    }
}

/// A `ValueError` (or a decimal signal) inside a resolver: the action is `INVALID`.
#[derive(Debug)]
struct Refused(String);

fn require_positive(values: &[&WideDecimal]) -> Result<(), Refused> {
    for value in values {
        if (*value).cmp(&WideDecimal::zero()) != Ordering::Greater {
            return Err(Refused(format!(
                "ratio legs must be positive; got {}",
                value.to_sci_string()
            )));
        }
    }
    Ok(())
}

fn div(a: &WideDecimal, b: &WideDecimal) -> Result<WideDecimal, Refused> {
    a.div(b).map_err(|e| Refused(e.0))
}

/// `split_factor(a, b)`: price `b/a`, volume `a/b`.
fn split_factor(
    ratio_from: &WideDecimal,
    ratio_to: &WideDecimal,
) -> Result<(WideDecimal, WideDecimal), Refused> {
    require_positive(&[ratio_from, ratio_to])?;
    Ok((div(ratio_to, ratio_from)?, div(ratio_from, ratio_to)?))
}

/// `bonus_factor(a, b)`: price `b/(a+b)`, volume `(a+b)/b`.
fn bonus_factor(
    ratio_from: &WideDecimal,
    ratio_to: &WideDecimal,
) -> Result<(WideDecimal, WideDecimal), Refused> {
    require_positive(&[ratio_from, ratio_to])?;
    let total = ratio_from.add(ratio_to);
    Ok((div(ratio_to, &total)?, div(&total, ratio_to)?))
}

/// `dividend_factor(P_cum, D)`: `(P_cum - D) / P_cum`.
fn dividend_factor(
    previous_close: &WideDecimal,
    amount: &WideDecimal,
) -> Result<WideDecimal, Refused> {
    let zero = WideDecimal::zero();
    if *previous_close <= zero {
        return Err(Refused(format!(
            "cum price must be positive; got {}",
            previous_close.to_sci_string()
        )));
    }
    if *amount < zero {
        return Err(Refused(format!(
            "dividend amount cannot be negative; got {}",
            amount.to_sci_string()
        )));
    }
    let factor = div(&previous_close.sub(amount), previous_close)?;
    if factor <= zero {
        return Err(Refused(format!(
            "dividend {} is not less than the cum price {}; the resulting factor would be zero \
             or negative",
            amount.to_sci_string(),
            previous_close.to_sci_string()
        )));
    }
    Ok(factor)
}

/// `rights_factor(P_cum, a, b, S)`: `TERP / P_cum`, clamped at 1 for a worthless right.
fn rights_factor(
    previous_close: &WideDecimal,
    ratio_from: &WideDecimal,
    ratio_to: &WideDecimal,
    issue_price: &WideDecimal,
) -> Result<WideDecimal, Refused> {
    require_positive(&[ratio_from, ratio_to])?;
    let zero = WideDecimal::zero();
    if *previous_close <= zero {
        return Err(Refused(format!(
            "cum price must be positive; got {}",
            previous_close.to_sci_string()
        )));
    }
    if *issue_price < zero {
        return Err(Refused(format!(
            "issue price cannot be negative; got {}",
            issue_price.to_sci_string()
        )));
    }
    // `(P_cum - S) * a / (a + b)`, evaluated left to right as Python does.
    let right_value = div(
        &previous_close.sub(issue_price).mul(ratio_from),
        &ratio_from.add(ratio_to),
    )?;
    if right_value <= zero {
        return Ok(one());
    }
    div(&previous_close.sub(&right_value), previous_close)
}

fn insufficient(action: &ActionInput, reason: &str) -> AppliedAction {
    AppliedAction::unit(
        action,
        AdjustmentOutcome::InsufficientData,
        reason.to_owned(),
    )
}

fn resolve(
    action: &ActionInput,
    previous_close: Option<&WideDecimal>,
) -> Result<AppliedAction, Refused> {
    let applied = |price: WideDecimal, volume: WideDecimal| AppliedAction {
        action: action.clone(),
        price_factor: price,
        volume_factor: volume,
        outcome: AdjustmentOutcome::Applied,
        detail: String::new(),
    };
    Ok(match action.action_type.as_str() {
        "split" => match (&action.ratio_from, &action.ratio_to) {
            (Some(a), Some(b)) => {
                let (p, v) = split_factor(a, b)?;
                applied(p, v)
            }
            _ => insufficient(action, "split has no ratio"),
        },
        "bonus" => match (&action.ratio_from, &action.ratio_to) {
            (Some(a), Some(b)) => {
                let (p, v) = bonus_factor(a, b)?;
                applied(p, v)
            }
            _ => insufficient(action, "bonus has no ratio"),
        },
        "dividend" => match (&action.amount, previous_close) {
            (None, _) => insufficient(action, "dividend has no amount"),
            (Some(_), None) => insufficient(action, "no cum-dividend close before the ex-date"),
            (Some(d), Some(p)) => applied(dividend_factor(p, d)?, one()),
        },
        "rights" => match (&action.ratio_from, &action.ratio_to) {
            (Some(a), Some(b)) => match (&action.issue_price, previous_close) {
                (None, _) => insufficient(action, "rights issue has no subscription price"),
                (Some(_), None) => insufficient(action, "no cum-rights close before the ex-date"),
                (Some(s), Some(p)) => applied(rights_factor(p, a, b, s)?, one()),
            },
            _ => insufficient(action, "rights issue has no ratio"),
        },
        "demerger" => AppliedAction::unit(
            action,
            AdjustmentOutcome::NotPriceAffecting,
            "demerger adjustment needs a per-case entitlement ratio".to_owned(),
        ),
        other => AppliedAction::unit(
            action,
            AdjustmentOutcome::NotPriceAffecting,
            format!("unrecognised action type {}", py_repr_str(other)),
        ),
    })
}

/// Python's `repr(str)` for the plain strings an action type can be.
fn py_repr_str(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{s}\"")
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// `resolve_action`: never guesses — a missing input is `INSUFFICIENT_DATA` at factor 1, an
/// impossible one `INVALID` with the refusal's message.
pub(super) fn resolve_action(
    action: &ActionInput,
    previous_close: Option<&WideDecimal>,
) -> AppliedAction {
    match resolve(action, previous_close) {
        Ok(applied) => applied,
        Err(Refused(message)) => AppliedAction::unit(action, AdjustmentOutcome::Invalid, message),
    }
}

/// One raw bar, every decimal already at its polars column's scale (see
/// `adjustments::frame_column`).
#[derive(Debug, Clone, PartialEq)]
pub(super) struct RawBar {
    pub(super) date: NaiveDate,
    pub(super) open_raw: Option<WideDecimal>,
    pub(super) high_raw: Option<WideDecimal>,
    pub(super) low_raw: Option<WideDecimal>,
    pub(super) close_raw: Option<WideDecimal>,
    pub(super) volume_raw: Option<i64>,
}

/// The four price columns' polars scales.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub(super) struct Scales {
    pub(super) open: i64,
    pub(super) high: i64,
    pub(super) low: i64,
    pub(super) close: i64,
}

/// One adjusted bar (`adjust_bars`' added columns).
#[derive(Debug, Clone, PartialEq)]
pub(super) struct AdjustedBar {
    pub(super) date: NaiveDate,
    pub(super) adj_factor: WideDecimal,
    pub(super) volume_factor: WideDecimal,
    pub(super) open: Option<WideDecimal>,
    pub(super) high: Option<WideDecimal>,
    pub(super) low: Option<WideDecimal>,
    pub(super) close: Option<WideDecimal>,
    pub(super) volume: Option<WideDecimal>,
}

/// `AdjustmentResult`.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct AdjustmentResult {
    pub(super) bars: Vec<AdjustedBar>,
    pub(super) applied: Vec<AppliedAction>,
}

impl AdjustmentResult {
    /// `effective_actions`.
    pub(super) fn effective_actions(&self) -> impl Iterator<Item = &AppliedAction> {
        self.applied.iter().filter(|a| a.is_effective())
    }

    /// `unquantified_actions`: recognised but not turned into a factor — never dropped.
    pub(super) fn unquantified_actions(&self) -> impl Iterator<Item = &AppliedAction> {
        self.applied.iter().filter(|a| {
            matches!(
                a.outcome,
                AdjustmentOutcome::InsufficientData | AdjustmentOutcome::Invalid
            )
        })
    }
}

/// `_cum_close`: the last close strictly before `ex_date`.
fn cum_close(closes: &[(NaiveDate, WideDecimal)], ex_date: NaiveDate) -> Option<&WideDecimal> {
    let mut candidate = None;
    for (day, close) in closes {
        if *day >= ex_date {
            break;
        }
        candidate = Some(close);
    }
    candidate
}

fn times(
    value: Option<&WideDecimal>,
    scale: i64,
    factor: &WideDecimal,
) -> Result<Option<WideDecimal>, WorkerError> {
    value
        .map(|v| pl_mul(v, scale, factor, FACTOR_SCALE))
        .transpose()
}

/// `adjust_bars(bars, actions)`: one instrument's raw history, adjusted. `bars` are sorted by
/// date (`bars.sort("date")`, which the loader's `ORDER BY date` already gives).
///
/// # Errors
/// A decimal polars would refuse (wider than 38 digits).
pub(super) fn adjust_bars(
    bars: &[RawBar],
    scales: Scales,
    actions: &[ActionInput],
) -> Result<AdjustmentResult, WorkerError> {
    if bars.is_empty() {
        return Ok(AdjustmentResult {
            bars: Vec::new(),
            applied: Vec::new(),
        });
    }
    let mut ordered: Vec<&RawBar> = bars.iter().collect();
    ordered.sort_by_key(|b| b.date);
    let closes: Vec<(NaiveDate, WideDecimal)> = ordered
        .iter()
        .filter_map(|b| b.close_raw.clone().map(|c| (b.date, c)))
        .collect();

    // "Process corporate actions in reverse-chronological order" — a stable sort, so actions
    // sharing an ex-date keep the order they were loaded in, as Python's `sorted` does.
    let mut by_date: Vec<&ActionInput> = actions.iter().collect();
    by_date.sort_by_key(|a| std::cmp::Reverse(a.ex_date));
    let applied: Vec<AppliedAction> = by_date
        .into_iter()
        .map(|a| resolve_action(a, cum_close(&closes, a.ex_date)))
        .collect();
    let effective: Vec<(NaiveDate, WideDecimal, WideDecimal)> = applied
        .iter()
        .filter(|a| a.is_effective())
        .map(|a| {
            (
                a.action.ex_date,
                lit(&a.price_factor),
                lit(&a.volume_factor),
            )
        })
        .collect();

    let unit = lit(&one());
    let mut out = Vec::with_capacity(ordered.len());
    for bar in ordered {
        // `price_expr = lit(1) * when(date < e1).then(f1).otherwise(1) * ...`, in `applied`
        // order, each product rounded to scale 10.
        let mut price = unit.clone();
        let mut volume = unit.clone();
        for (ex_date, price_factor, volume_factor) in &effective {
            let before_ex = bar.date < *ex_date;
            let pf = if before_ex { price_factor } else { &unit };
            let vf = if before_ex { volume_factor } else { &unit };
            price = pl_mul(&price, FACTOR_SCALE, pf, FACTOR_SCALE)?;
            volume = pl_mul(&volume, FACTOR_SCALE, vf, FACTOR_SCALE)?;
        }
        let adjusted_volume = bar
            .volume_raw
            .map(|v| pl_mul(&WideDecimal::from(v), 0, &volume, FACTOR_SCALE))
            .transpose()?;
        out.push(AdjustedBar {
            date: bar.date,
            open: times(bar.open_raw.as_ref(), scales.open, &price)?,
            high: times(bar.high_raw.as_ref(), scales.high, &price)?,
            low: times(bar.low_raw.as_ref(), scales.low, &price)?,
            close: times(bar.close_raw.as_ref(), scales.close, &price)?,
            volume: adjusted_volume,
            adj_factor: price,
            volume_factor: volume,
        });
    }
    Ok(AdjustmentResult { bars: out, applied })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    fn d(s: &str) -> WideDecimal {
        s.parse().unwrap()
    }

    fn day(y: i32, m: u32, dd: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, dd).unwrap()
    }

    const EX: (i32, u32, u32) = (2026, 3, 9);

    fn ex() -> NaiveDate {
        day(EX.0, EX.1, EX.2)
    }

    /// The Python tests' `bars()` helper: `Decimal(value)` with its own exponent, so a column
    /// of integers is polars scale 0.
    fn bars(closes: &[(NaiveDate, &str)], volume: i64) -> (Vec<RawBar>, Scales) {
        let rows: Vec<RawBar> = closes
            .iter()
            .map(|(date, v)| RawBar {
                date: *date,
                open_raw: Some(d(v)),
                high_raw: Some(d(v)),
                low_raw: Some(d(v)),
                close_raw: Some(d(v)),
                volume_raw: Some(volume),
            })
            .collect();
        let scale = rows
            .iter()
            .filter_map(|r| r.close_raw.as_ref().map(scale_of))
            .max()
            .unwrap_or(0);
        (
            rows,
            Scales {
                open: scale,
                high: scale,
                low: scale,
                close: scale,
            },
        )
    }

    fn split(a: &str, b: &str, on: NaiveDate) -> ActionInput {
        let mut x = ActionInput::new("split", on);
        x.ratio_from = Some(d(a));
        x.ratio_to = Some(d(b));
        x
    }

    #[test]
    fn split_ten_to_one() {
        let (p, v) = split_factor(&d("10"), &d("1")).unwrap();
        assert_eq!(p, d("0.1"));
        assert_eq!(v, d("10"));
    }

    #[test]
    fn bonus_four_to_one_and_one_to_one() {
        let (p, v) = bonus_factor(&d("4"), &d("1")).unwrap();
        assert_eq!((p, v), (d("0.2"), d("5")));
        let (p, v) = bonus_factor(&d("1"), &d("1")).unwrap();
        assert_eq!((p, v), (d("0.5"), d("2")));
    }

    #[test]
    fn dividend_and_its_refusal() {
        assert_eq!(dividend_factor(&d("100"), &d("10")).unwrap(), d("0.9"));
        let err = dividend_factor(&d("10"), &d("10")).unwrap_err();
        assert!(err.0.contains("not less than"));
    }

    #[test]
    fn rights_below_and_above_market() {
        assert_eq!(
            rights_factor(&d("100"), &d("1"), &d("1"), &d("50")).unwrap(),
            d("0.75")
        );
        assert_eq!(
            rights_factor(&d("100"), &d("1"), &d("1"), &d("120")).unwrap(),
            d("1")
        );
    }

    #[test]
    fn non_positive_ratio_legs_are_refused() {
        for leg in ["0", "-1"] {
            let err = split_factor(&d(leg), &d("1")).unwrap_err();
            assert!(err.0.contains("positive"));
        }
    }

    #[test]
    fn refusal_to_guess() {
        let r = resolve_action(&ActionInput::new("split", ex()), Some(&d("100")));
        assert_eq!(r.outcome, AdjustmentOutcome::InsufficientData);
        assert_eq!(r.price_factor, d("1"));
        let r = resolve_action(&ActionInput::new("dividend", ex()), Some(&d("100")));
        assert_eq!(r.outcome, AdjustmentOutcome::InsufficientData);
        let mut div5 = ActionInput::new("dividend", ex());
        div5.amount = Some(d("5"));
        assert_eq!(
            resolve_action(&div5, None).outcome,
            AdjustmentOutcome::InsufficientData
        );
        let mut rights = ActionInput::new("rights", ex());
        rights.ratio_from = Some(d("1"));
        rights.ratio_to = Some(d("1"));
        let r = resolve_action(&rights, Some(&d("100")));
        assert_eq!(r.outcome, AdjustmentOutcome::InsufficientData);
        assert!(r.detail.contains("subscription price"));
        let r = resolve_action(&ActionInput::new("demerger", ex()), Some(&d("100")));
        assert_eq!(r.outcome, AdjustmentOutcome::NotPriceAffecting);
        let r = resolve_action(&ActionInput::new("buyback", ex()), Some(&d("100")));
        assert_eq!(r.outcome, AdjustmentOutcome::NotPriceAffecting);
        assert_eq!(r.detail, "unrecognised action type 'buyback'");
        let r = resolve_action(&split("0", "1", ex()), Some(&d("100")));
        assert_eq!(r.outcome, AdjustmentOutcome::Invalid);
        assert_eq!(r.detail, "ratio legs must be positive; got 0");
    }

    #[test]
    fn a_two_for_one_split_leaves_the_close_continuous() {
        let (b, s) = bars(
            &[
                (day(2026, 3, 6), "100"),
                (day(2026, 3, 9), "50"),
                (day(2026, 3, 10), "51"),
            ],
            1000,
        );
        let r = adjust_bars(&b, s, &[split("2", "1", ex())]).unwrap();
        assert_eq!(r.bars[0].close, Some(d("50")));
        assert_eq!(r.bars[1].close, Some(d("50")));
        // The ex-date bar and after are untouched; the newest equals its print.
        assert_eq!(r.bars[1].adj_factor, d("1"));
        assert_eq!(r.bars[2].close, b[2].close_raw);
        // Volume moves the other way; OHL with the close.
        assert_eq!(r.bars[0].volume, Some(d("2000")));
        assert_eq!(r.bars[0].open, Some(d("50")));
        assert_eq!(r.bars[0].high, Some(d("50")));
        assert_eq!(r.bars[0].low, Some(d("50")));
    }

    #[test]
    fn two_actions_compound_and_later_actions_reach_earlier_bars() {
        let (b, s) = bars(
            &[
                (day(2024, 4, 12), "1000"),
                (day(2024, 4, 15), "50"),
                (day(2024, 4, 16), "52"),
            ],
            1000,
        );
        let mut bonus = ActionInput::new("bonus", day(2024, 4, 15));
        bonus.ratio_from = Some(d("1"));
        bonus.ratio_to = Some(d("1"));
        let r = adjust_bars(&b, s, &[split("10", "1", day(2024, 4, 15)), bonus]).unwrap();
        assert_eq!(r.bars[0].adj_factor, d("0.05"));
        assert_eq!(r.bars[0].close, Some(d("50")));

        let (b, s) = bars(
            &[
                (day(2024, 1, 2), "400"),
                (day(2025, 1, 2), "200"),
                (day(2026, 1, 2), "100"),
            ],
            1000,
        );
        let r = adjust_bars(
            &b,
            s,
            &[
                split("2", "1", day(2025, 1, 2)),
                split("2", "1", day(2026, 1, 2)),
            ],
        )
        .unwrap();
        let factors: Vec<WideDecimal> = r.bars.iter().map(|x| x.adj_factor.clone()).collect();
        assert_eq!(factors, [d("0.25"), d("0.5"), d("1")]);
    }

    #[test]
    fn an_unquantified_action_is_surfaced_not_dropped() {
        let (b, s) = bars(&[(day(2026, 3, 6), "100"), (day(2026, 3, 9), "95")], 1000);
        let mut rights = ActionInput::new("rights", ex());
        rights.ratio_from = Some(d("1"));
        rights.ratio_to = Some(d("1"));
        let r = adjust_bars(&b, s, &[rights]).unwrap();
        assert_eq!(r.unquantified_actions().count(), 1);
        assert!(r.bars.iter().all(|x| x.adj_factor == d("1")));
    }

    #[test]
    fn adjusting_twice_gives_the_same_answer_and_no_actions_is_unity() {
        let (b, s) = bars(&[(day(2026, 3, 6), "100"), (day(2026, 3, 9), "50")], 1000);
        let actions = [split("2", "1", ex())];
        assert_eq!(
            adjust_bars(&b, s, &actions).unwrap(),
            adjust_bars(&b, s, &actions).unwrap()
        );
        let r = adjust_bars(&b, s, &[]).unwrap();
        assert!(r.bars.iter().all(|x| x.adj_factor == d("1")));
        assert_eq!(r.bars[0].close, Some(d("100")));
        assert!(adjust_bars(&[], s, &actions).unwrap().bars.is_empty());
    }

    #[test]
    fn polars_rounds_half_even_once_at_the_wider_scale() {
        // Measured against polars 1.43.2 (see the module doc).
        assert_eq!(lit(&d("0.33333333345")).to_string(), "0.3333333334");
        assert_eq!(lit(&d("0.33333333335")).to_string(), "0.3333333334");
        assert_eq!(lit(&d("-0.66666666666666")).to_string(), "-0.6666666667");
        let half = d("0.5000000000");
        for (p, want) in [
            ("0.0000000001", "0E-10"),
            ("0.0000000003", "2E-10"),
            ("0.0000000005", "2E-10"),
            ("0.0000000007", "4E-10"),
            ("0.0000000009", "4E-10"),
        ] {
            let got = pl_mul(&d(p), 10, &half, 10).unwrap();
            assert_eq!(got.to_sci_string(), want, "{p} * 0.5");
        }
        let wide = pl_mul(
            &d("12345.8938683933698068462403670"),
            25,
            &d("0.9860465070"),
            10,
        )
        .unwrap();
        assert_eq!(wide.to_string(), "12173.6255247220000000000000000");
        let narrow = pl_mul(&d("100.1235"), 4, &d("0.5000000000"), 10).unwrap();
        assert_eq!(narrow.to_string(), "50.0617500000");
    }

    #[test]
    fn a_three_action_history_matches_polars() {
        // The same inputs run through Python's adjust_bars (polars 1.43.2) printed
        // high 33.1284872352, close 32.7476891460, adj applied factors
        // 0.9860465069921950140182770825 / 0.3333333333333333333333333333 /
        // 0.9930232534960975070091385412 (see this module's doc).
        let rows: Vec<RawBar> = (1..=7)
            .map(|dd| RawBar {
                date: day(2026, 1, dd),
                open_raw: Some(d("100.1234")),
                high_raw: Some(d("101.5")),
                low_raw: Some(d("99.0000")),
                close_raw: Some(d("100.3333")),
                volume_raw: Some(1000),
            })
            .collect();
        let scales = Scales {
            open: 4,
            high: 1,
            low: 4,
            close: 4,
        };
        let mut div1 = ActionInput::new("dividend", day(2026, 1, 5));
        div1.amount = Some(d("1.4000"));
        let mut div2 = ActionInput::new("dividend", day(2026, 1, 2));
        div2.amount = Some(d("0.7"));
        let r = adjust_bars(
            &rows,
            scales,
            &[div1, split("3", "1", day(2026, 1, 3)), div2],
        )
        .unwrap();
        let factors: Vec<String> = r
            .applied
            .iter()
            .map(|a| a.price_factor.to_string())
            .collect();
        assert_eq!(
            factors,
            [
                "0.9860465069921950140182770825",
                "0.3333333333333333333333333333",
                "0.9930232534960975070091385412"
            ]
        );
        let first = &r.bars[0];
        assert_eq!(first.high.as_ref().unwrap().to_string(), "33.1284872352");
        assert_eq!(first.low.as_ref().unwrap().to_string(), "32.3125146432");
        assert_eq!(first.close.as_ref().unwrap().to_string(), "32.7476891460");
        assert_eq!(
            first.volume.as_ref().unwrap().to_string(),
            "3000.0000000000"
        );
        let third = &r.bars[2];
        assert_eq!(third.close.as_ref().unwrap().to_string(), "98.9333000008");
        assert_eq!(
            r.bars[4].close.as_ref().unwrap().to_string(),
            "100.3333000000"
        );
    }
}
