//! Ports the parts of `baskfy_core.cash_ledger` the read routes call, and
//! `baskfy_core.curated_accounting.xirr` — the money-weighted return solver, `Decimal` for
//! `Decimal` (its non-integral powers through [`Dec::pow`]).

use chrono::NaiveDate;

use super::ledger::{DomainResult, money};
use super::pydec::Dec;

/// `CashFlowKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CashFlowKind {
    ExternalDeposit,
    ExternalWithdrawal,
    Assign,
    Release,
    Buy,
    Sell,
    Dividend,
}

impl CashFlowKind {
    pub(crate) fn parse(s: &str) -> DomainResult<Self> {
        Ok(match s {
            "EXTERNAL_DEPOSIT" => Self::ExternalDeposit,
            "EXTERNAL_WITHDRAWAL" => Self::ExternalWithdrawal,
            "ASSIGN" => Self::Assign,
            "RELEASE" => Self::Release,
            "BUY" => Self::Buy,
            "SELL" => Self::Sell,
            "DIVIDEND" => Self::Dividend,
            other => return Err(format!("'{other}' is not a valid CashFlowKind")),
        })
    }

    /// The enum value.
    pub(crate) fn value(self) -> &'static str {
        match self {
            Self::ExternalDeposit => "EXTERNAL_DEPOSIT",
            Self::ExternalWithdrawal => "EXTERNAL_WITHDRAWAL",
            Self::Assign => "ASSIGN",
            Self::Release => "RELEASE",
            Self::Buy => "BUY",
            Self::Sell => "SELL",
            Self::Dividend => "DIVIDEND",
        }
    }

    /// `is_external`.
    pub(crate) fn is_external(self) -> bool {
        matches!(self, Self::ExternalDeposit | Self::ExternalWithdrawal)
    }

    /// `is_xirr_event`.
    pub(crate) fn is_xirr_event(self) -> bool {
        matches!(self, Self::Assign | Self::Release)
    }

    fn is_stock(self) -> bool {
        matches!(self, Self::Buy | Self::Sell)
    }

    fn is_cash_only(self) -> bool {
        self.is_external() || self.is_xirr_event()
    }

    /// `_PORTFOLIO_SIGN`.
    fn portfolio_sign(self) -> i64 {
        match self {
            Self::ExternalDeposit | Self::ExternalWithdrawal => 0,
            Self::Assign | Self::Sell | Self::Dividend => 1,
            Self::Release | Self::Buy => -1,
        }
    }
}

/// `PortfolioCashFlow` (the ledger's).
#[derive(Debug, Clone)]
pub(crate) struct LedgerCashFlow {
    pub(crate) kind: CashFlowKind,
    pub(crate) amount: Dec,
    pub(crate) occurred_on: NaiveDate,
    pub(crate) portfolio_id: Option<i64>,
}

impl LedgerCashFlow {
    /// The constructor with `__post_init__`'s §4.4 checks.
    pub(crate) fn new(
        broker_account_id: i64,
        kind: CashFlowKind,
        amount: Dec,
        occurred_on: NaiveDate,
        portfolio_id: Option<i64>,
        instrument_id: Option<i64>,
        quantity: Option<&Dec>,
    ) -> DomainResult<Self> {
        if amount <= Dec::zero() {
            return Err(format!("a cash flow moves a positive amount; got {amount}"));
        }
        if money(&amount) != amount {
            return Err(format!("{amount} is finer than a paisa"));
        }
        if kind.is_external() && portfolio_id.is_some() {
            return Err(format!("{} belongs to no portfolio", kind.value()));
        }
        if !kind.is_external() && portfolio_id.is_none() {
            return Err(format!("{} must name the portfolio", kind.value()));
        }
        if kind.is_stock() {
            if instrument_id.is_none() {
                return Err(format!("{} must name the stock", kind.value()));
            }
            match quantity {
                Some(q) if *q > Dec::zero() => {
                    let rounded = q
                        .quantize(-4, baskfy_core::decimal::Rounding::HalfUp)
                        .unwrap_or_else(|| q.clone());
                    if rounded != *q {
                        return Err(format!("{q} is finer than four decimals"));
                    }
                }
                _ => return Err(format!("{} must carry the quantity traded", kind.value())),
            }
        } else {
            if kind.is_cash_only() && instrument_id.is_some() {
                return Err(format!("{} names an instrument", kind.value()));
            }
            if quantity.is_some() {
                return Err(format!("{} moves no shares", kind.value()));
            }
        }
        let _ = broker_account_id;
        Ok(Self {
            kind,
            amount,
            occurred_on,
            portfolio_id,
        })
    }
}

/// `portfolio_cash(flows, portfolio_id)`.
pub(crate) fn portfolio_cash(flows: &[LedgerCashFlow], portfolio_id: i64) -> Dec {
    let mut total = Dec::lit("0");
    for f in flows {
        if f.portfolio_id == Some(portfolio_id) {
            total = total.add(&Dec::int(f.kind.portfolio_sign()).mul(&f.amount));
        }
    }
    money(&total)
}

/// `CashFlow` (`curated_accounting`).
#[derive(Debug, Clone)]
pub(crate) struct CashFlow {
    pub(crate) on: NaiveDate,
    pub(crate) amount: Dec,
}

/// `xirr_events`.
fn xirr_events(flows: &[LedgerCashFlow], portfolio_id: i64) -> Vec<CashFlow> {
    flows
        .iter()
        .filter(|f| f.portfolio_id == Some(portfolio_id) && f.kind.is_xirr_event())
        .map(|f| CashFlow {
            on: f.occurred_on,
            amount: money(&Dec::int(-f.kind.portfolio_sign()).mul(&f.amount)),
        })
        .collect()
}

/// `portfolio_xirr`.
pub(crate) fn portfolio_xirr(
    flows: &[LedgerCashFlow],
    portfolio_id: i64,
    as_of: NaiveDate,
    closing_value: &Dec,
) -> DomainResult<Option<Dec>> {
    if *closing_value < Dec::zero() {
        return Err(format!(
            "closing value cannot be negative; got {closing_value}"
        ));
    }
    let mut events = xirr_events(flows, portfolio_id);
    let Some(last) = events.iter().map(|e| e.on).max() else {
        return Ok(None);
    };
    if as_of < last {
        return Err(format!(
            "as_of {as_of} precedes the last cash-flow event {last}"
        ));
    }
    events.push(CashFlow {
        on: as_of,
        amount: money(closing_value),
    });
    xirr(&events)
}

/// `curated_accounting.xirr` with its default guess of 0.1: Newton, then a scanned bisection.
pub(crate) fn xirr(flows: &[CashFlow]) -> DomainResult<Option<Dec>> {
    if flows.len() < 2 {
        return Ok(None);
    }
    let mut ordered = flows.to_vec();
    ordered.sort_by(|a, b| a.on.cmp(&b.on).then_with(|| a.amount.cmp(&b.amount)));
    let amounts: Vec<Dec> = ordered.iter().map(|f| f.amount.clone()).collect();
    let zero = Dec::lit("0");
    if !(amounts.iter().any(|a| *a > zero) && amounts.iter().any(|a| *a < zero)) {
        return Ok(None);
    }
    let t0 = ordered[0].on;
    let days_per_year = Dec::lit("365");
    let mut years = Vec::with_capacity(ordered.len());
    for f in &ordered {
        years.push(
            Dec::int((f.on - t0).num_days())
                .div(&days_per_year)
                .ok_or("division by zero")?,
        );
    }
    let one_d = Dec::lit("1");
    let npv = |rate: &Dec| -> DomainResult<Dec> {
        let mut total = Dec::lit("0");
        let one = Dec::lit("1").add(rate);
        if one <= zero {
            return Ok(if amounts[0] > zero {
                Dec::lit("1")
            } else {
                Dec::lit("-1")
            });
        }
        let ln = one.pow_ln().ok_or("pow")?;
        for (amount, year) in amounts.iter().zip(&years) {
            let p = Dec::pow_from_ln(&ln, year).ok_or("Overflow")?;
            total = total.add(&amount.div(&p).ok_or("division by zero")?);
        }
        Ok(total)
    };
    let d_npv = |rate: &Dec| -> DomainResult<Dec> {
        let mut total = Dec::lit("0");
        let one = Dec::lit("1").add(rate);
        if one <= zero {
            return Ok(Dec::lit("0"));
        }
        let ln = one.pow_ln().ok_or("pow")?;
        for (amount, year) in amounts.iter().zip(&years) {
            let p = Dec::pow_from_ln(&ln, &year.add(&one_d)).ok_or("Overflow")?;
            let num = amount.neg().mul(year);
            total = total.add(&num.div(&p).ok_or("division by zero")?);
        }
        Ok(total)
    };
    let quant = |d: &Dec| -> Dec {
        d.quantize(-4, baskfy_core::decimal::Rounding::HalfEven)
            .unwrap_or_else(|| d.clone())
    };
    let minus_one = Dec::lit("-1");
    let tol = Dec::lit("1e-12");
    let mut rate = Dec::lit("0.1");
    let mut completed = true;
    for _ in 0..100 {
        if rate <= minus_one {
            completed = false;
            break;
        }
        let f = npv(&rate)?;
        if f.abs() < tol {
            return Ok(Some(quant(&rate)));
        }
        let d = d_npv(&rate)?;
        if d.is_zero() {
            completed = false;
            break;
        }
        let nxt = rate.sub(&f.div(&d).ok_or("division by zero")?);
        if nxt <= minus_one {
            completed = false;
            break;
        }
        if nxt.sub(&rate).abs() < tol {
            return Ok(Some(quant(&nxt)));
        }
        rate = nxt;
    }
    if completed && npv(&rate)?.abs() < Dec::lit("1e-6") {
        return Ok(Some(quant(&rate)));
    }
    // Bisection fallback over a scanned bracket.
    let mut lo = Dec::lit("-0.9999");
    let mut f_lo = npv(&lo)?;
    let mut grid = Vec::new();
    let mut cursor = Dec::lit("-0.99");
    while cursor <= Dec::lit("1") {
        grid.push(cursor.clone());
        cursor = cursor.add(&Dec::lit("0.01"));
    }
    let mut cursor = Dec::lit("1");
    while cursor <= Dec::lit("100") {
        grid.push(cursor.clone());
        cursor = cursor.add(&Dec::lit("0.5"));
    }
    let mut bracket = None;
    for hi in grid {
        if hi <= lo {
            continue;
        }
        let f_hi = npv(&hi)?;
        if f_lo.mul(&f_hi) < zero {
            bracket = Some((lo.clone(), hi));
            break;
        }
        lo = hi;
        f_lo = f_hi;
    }
    let Some((mut lo, mut hi)) = bracket else {
        return Ok(None);
    };
    let two = Dec::lit("2");
    for _ in 0..200 {
        let mid = lo.add(&hi).div(&two).ok_or("division by zero")?;
        let f_mid = npv(&mid)?;
        if f_mid.abs() < tol || hi.sub(&lo) < tol {
            return Ok(Some(quant(&mid)));
        }
        if npv(&lo)?.mul(&f_mid) < zero {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    Ok(Some(quant(
        &lo.add(&hi).div(&two).ok_or("division by zero")?,
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn day(y: i32, m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, d).unwrap_or_default()
    }

    #[test]
    fn xirr_matches_curated_accounting() {
        // CPython: xirr([CashFlow(2025-01-01, -1000), CashFlow(2026-01-01, 1100)]) == 0.1000
        let flows = [
            CashFlow {
                on: day(2025, 1, 1),
                amount: Dec::lit("-1000"),
            },
            CashFlow {
                on: day(2026, 1, 1),
                amount: Dec::lit("1100"),
            },
        ];
        assert_eq!(
            xirr(&flows).map(|r| r.map(|d| d.to_sci_string())),
            Ok(Some("0.1000".to_owned()))
        );
        // All one sign: no root.
        let same = [
            CashFlow {
                on: day(2025, 1, 1),
                amount: Dec::lit("10"),
            },
            CashFlow {
                on: day(2025, 6, 1),
                amount: Dec::lit("10"),
            },
        ];
        assert_eq!(xirr(&same), Ok(None));
    }

    #[test]
    fn flows_obey_section_4_4() {
        let d = day(2026, 9, 1);
        assert!(
            LedgerCashFlow::new(
                1,
                CashFlowKind::Assign,
                Dec::lit("10.00"),
                d,
                None,
                None,
                None
            )
            .is_err()
        );
        assert!(
            LedgerCashFlow::new(
                1,
                CashFlowKind::ExternalDeposit,
                Dec::lit("10.001"),
                d,
                None,
                None,
                None
            )
            .is_err()
        );
        let ok = LedgerCashFlow::new(
            1,
            CashFlowKind::Assign,
            Dec::lit("10.00"),
            d,
            Some(3),
            None,
            None,
        );
        assert!(ok.is_ok());
        let flows: Vec<LedgerCashFlow> = ok.into_iter().collect();
        assert_eq!(portfolio_cash(&flows, 3).to_sci_string(), "10.00");
    }
}
