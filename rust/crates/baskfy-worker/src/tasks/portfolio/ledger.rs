//! The parts of `baskfy_core.allocation_ledger` and `baskfy_core.cash_ledger` the NAV job calls,
//! with Python's refusals and their sentences.
//!
//! The API holds a port of the same modules for its read routes (`routes/portfolio/ledger.rs`,
//! `cash.rs`), crate-private and on its own `Dec`, and without the three cash totals this job
//! needs; DECISIONS-RS R8.pf2 records why this lane carries the job's subset here instead of
//! changing that crate's visibility. Pure: no I/O, no clock.
//!
//! Arithmetic is `rust_decimal`: every operand is a stored `numeric` (four places for quantities
//! and prices, two for money), so each product and sum is exact — as Python's 28-digit context
//! is for the same operands — and `checked_*` turns an impossible overflow into an error rather
//! than a wrong number.

use std::collections::HashMap;

use chrono::NaiveDate;
use rust_decimal::{Decimal, RoundingStrategy};

use crate::error::WorkerError;

/// A Python `ValueError`.
pub fn value_error(message: impl Into<String>) -> WorkerError {
    WorkerError::python("ValueError", message)
}

fn overflow() -> WorkerError {
    WorkerError::python("ArithmeticError", "decimal overflow")
}

/// `a + b`.
///
/// # Errors
/// An overflow (never on stored values).
pub fn add(a: Decimal, b: Decimal) -> Result<Decimal, WorkerError> {
    a.checked_add(b).ok_or_else(overflow)
}

/// `a - b`.
///
/// # Errors
/// An overflow (never on stored values).
pub fn sub(a: Decimal, b: Decimal) -> Result<Decimal, WorkerError> {
    a.checked_sub(b).ok_or_else(overflow)
}

/// `a * b`.
///
/// # Errors
/// An overflow (never on stored values).
pub fn mul(a: Decimal, b: Decimal) -> Result<Decimal, WorkerError> {
    a.checked_mul(b).ok_or_else(overflow)
}

/// `baskfy_core.gst.money`: quantise to paise, half-up (the result always has two places).
#[must_use]
pub fn money(value: Decimal) -> Decimal {
    let mut out = value.round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero);
    out.rescale(2);
    out
}

/// `PortfolioKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortfolioKind {
    /// `CAPITAL`.
    Capital,
    /// `MONITORING`.
    Monitoring,
}

impl PortfolioKind {
    /// `PortfolioKind(value)`.
    ///
    /// # Errors
    /// Python's `ValueError` for any other value.
    pub fn parse(value: &str) -> Result<Self, WorkerError> {
        match value {
            "CAPITAL" => Ok(Self::Capital),
            "MONITORING" => Ok(Self::Monitoring),
            other => Err(not_valid(other, "PortfolioKind")),
        }
    }
}

fn not_valid(value: &str, enum_name: &str) -> WorkerError {
    value_error(format!(
        "{} is not a valid {enum_name}",
        baskfy_providers::py::repr_str(value)
    ))
}

/// `PortfolioSource(value)`: the job reads it only to build the domain object.
///
/// # Errors
/// Python's `ValueError` for a value outside the enum.
pub fn check_portfolio_source(value: &str) -> Result<(), WorkerError> {
    match value {
        "SUBSCRIBED" | "MY_SCREEN" | "MY_STRATEGY" | "HOLDING_GROUP" => Ok(()),
        other => Err(not_valid(other, "PortfolioSource")),
    }
}

/// `HoldingKey`: one instrument in one broker account.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct HoldingKey {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `broker_account_id`.
    pub broker_account_id: i64,
}

impl HoldingKey {
    /// The dataclass `repr`.
    #[must_use]
    pub fn repr(&self) -> String {
        format!(
            "HoldingKey(instrument_id={}, broker_account_id={})",
            self.instrument_id, self.broker_account_id
        )
    }
}

/// `Holding` (the job never knows an average price).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Holding {
    /// `key`.
    pub key: HoldingKey,
    /// `quantity`.
    pub quantity: Decimal,
}

impl Holding {
    /// With `__post_init__`'s refusal of a negative quantity.
    ///
    /// # Errors
    /// `ValueError` for a negative quantity.
    pub fn new(key: HoldingKey, quantity: Decimal) -> Result<Self, WorkerError> {
        if quantity < Decimal::ZERO {
            return Err(value_error(format!(
                "quantity cannot be negative; got {quantity}"
            )));
        }
        Ok(Self { key, quantity })
    }
}

/// `Portfolio` (the fields the job reads).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LedgerPortfolio {
    /// `portfolio_id`.
    pub portfolio_id: i64,
    /// `name`.
    pub name: String,
    /// `kind`.
    pub kind: PortfolioKind,
    /// `started_on`.
    pub started_on: NaiveDate,
}

/// `Allocation`: a positive quantity of one holding counted against one capital portfolio.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Allocation {
    /// `key`.
    pub key: HoldingKey,
    /// `portfolio_id`.
    pub portfolio_id: i64,
    /// `quantity`.
    pub quantity: Decimal,
}

impl Allocation {
    /// With `__post_init__`'s refusals.
    ///
    /// # Errors
    /// `ValueError` for no portfolio or a non-positive quantity.
    pub fn new(
        key: HoldingKey,
        portfolio_id: Option<i64>,
        quantity: Decimal,
    ) -> Result<Self, WorkerError> {
        let Some(portfolio_id) = portfolio_id else {
            return Err(value_error(
                "an allocation names a capital portfolio; Unallocated is the remainder, not a \
                 row — ask unallocated_quantity() for it",
            ));
        };
        if quantity <= Decimal::ZERO {
            return Err(value_error(format!(
                "an allocation must be a positive quantity; got {quantity}. Remove the row \
                 instead of writing a zero one"
            )));
        }
        Ok(Self {
            key,
            portfolio_id,
            quantity,
        })
    }
}

/// `_slice_index`: `{holding -> {portfolio -> quantity}}` in insertion order, refusing a repeated
/// `(holding, portfolio)` pair.
type SliceIndex = Vec<(HoldingKey, Vec<(i64, Decimal)>)>;

fn slice_index(allocations: &[Allocation]) -> Result<SliceIndex, WorkerError> {
    let mut index: SliceIndex = Vec::new();
    for allocation in allocations {
        let position = match index.iter().position(|(k, _)| *k == allocation.key) {
            Some(p) => p,
            None => {
                index.push((allocation.key, Vec::new()));
                index.len() - 1
            }
        };
        let Some((_, slices)) = index.get_mut(position) else {
            continue;
        };
        if let Some((_, existing)) = slices.iter().find(|(id, _)| *id == allocation.portfolio_id) {
            return Err(value_error(format!(
                "holding {} has two allocations to portfolio {} ({existing} and {}); a holding \
                 has at most one slice per portfolio, and summing them would credit shares \
                 nobody owns",
                allocation.key.repr(),
                allocation.portfolio_id,
                allocation.quantity
            )));
        }
        slices.push((allocation.portfolio_id, allocation.quantity));
    }
    Ok(index)
}

fn slices_sum(slices: &[(i64, Decimal)]) -> Result<Decimal, WorkerError> {
    let mut total = Decimal::ZERO;
    for (_, quantity) in slices {
        total = add(total, *quantity)?;
    }
    Ok(total)
}

/// `validate_allocations`: a slice in a missing portfolio or a monitoring view is refused.
///
/// # Errors
/// `ValueError`, Python's sentences.
pub fn validate_allocations(
    allocations: &[Allocation],
    portfolios: &[LedgerPortfolio],
) -> Result<(), WorkerError> {
    for (_, slices) in slice_index(allocations)? {
        for (portfolio_id, _) in slices {
            let Some(portfolio) = portfolios.iter().find(|p| p.portfolio_id == portfolio_id) else {
                return Err(value_error(format!(
                    "allocation names portfolio {portfolio_id}, which does not exist"
                )));
            };
            if portfolio.kind == PortfolioKind::Monitoring {
                return Err(value_error(format!(
                    "{} is a monitoring view, and a monitoring view holds no allocation: it \
                     overlaps other portfolios by design and is excluded from every total (spec \
                     section 4.1)",
                    baskfy_providers::py::repr_str(&portfolio.name)
                )));
            }
        }
    }
    Ok(())
}

/// `validate_against_holdings`: no holding's slices may exceed what is held. `held` is a dict
/// over the holdings, so a key listed twice keeps its last quantity — as Python's does.
///
/// # Errors
/// `ValueError`, Python's sentences.
pub fn validate_against_holdings(
    holdings: &[Holding],
    allocations: &[Allocation],
) -> Result<(), WorkerError> {
    let mut held: HashMap<HoldingKey, Decimal> = HashMap::new();
    for holding in holdings {
        held.insert(holding.key, holding.quantity);
    }
    for (key, slices) in slice_index(allocations)? {
        let Some(have) = held.get(&key).copied() else {
            return Err(value_error(format!(
                "allocations name holding {}, which is not held at all; a slice of a position \
                 that no longer exists cannot be valued",
                key.repr()
            )));
        };
        let allocated = slices_sum(&slices)?;
        if allocated > have {
            return Err(value_error(format!(
                "holding {} is over-allocated: {allocated} shares are filed across {} \
                 portfolio(s) but only {have} are held. Free {} before this can be valued",
                key.repr(),
                slices.len(),
                sub(allocated, have)?
            )));
        }
    }
    Ok(())
}

/// `holding_value`: quantity times price, quantised to paise. A missing price is `KeyError`.
///
/// # Errors
/// `KeyError` for a missing price.
pub fn holding_value(
    key: HoldingKey,
    quantity: Decimal,
    prices: &HashMap<i64, Decimal>,
) -> Result<Decimal, WorkerError> {
    let Some(price) = prices.get(&key.instrument_id) else {
        return Err(WorkerError::python(
            "KeyError",
            format!(
                "'no price for instrument {}; a missing price must be resolved by the caller, \
                 because valuing it at zero produces a total that is wrong and still adds up'",
                key.instrument_id
            ),
        ));
    };
    Ok(money(mul(quantity, *price)?))
}

/// What `portfolio_values` returns: `{None (Unallocated) | portfolio id -> value}` in insertion
/// order.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Values(pub Vec<(Option<i64>, Decimal)>);

impl Values {
    /// `values.get(key)`.
    #[must_use]
    pub fn get(&self, key: Option<i64>) -> Option<Decimal> {
        self.0.iter().find(|(k, _)| *k == key).map(|(_, v)| *v)
    }

    fn bump(&mut self, key: Option<i64>, amount: Decimal) -> Result<(), WorkerError> {
        if let Some((_, value)) = self.0.iter_mut().find(|(k, _)| *k == key) {
            *value = add(*value, amount)?;
        } else {
            self.0.push((key, amount));
        }
        Ok(())
    }
}

/// `portfolio_values`: every capital portfolio's value plus Unallocated's, validated first.
///
/// # Errors
/// The two validations' `ValueError`s; `KeyError` for a missing price.
pub fn portfolio_values(
    holdings: &[Holding],
    allocations: &[Allocation],
    portfolios: &[LedgerPortfolio],
    prices: &HashMap<i64, Decimal>,
) -> Result<Values, WorkerError> {
    validate_allocations(allocations, portfolios)?;
    validate_against_holdings(holdings, allocations)?;
    let index = slice_index(allocations)?;
    let mut values = Values(vec![(None, Decimal::ZERO)]);
    for portfolio in portfolios {
        if portfolio.kind == PortfolioKind::Capital {
            if let Some((_, v)) = values
                .0
                .iter_mut()
                .find(|(k, _)| *k == Some(portfolio.portfolio_id))
            {
                *v = Decimal::ZERO;
            } else {
                values.0.push((Some(portfolio.portfolio_id), Decimal::ZERO));
            }
        }
    }
    let empty: Vec<(i64, Decimal)> = Vec::new();
    for holding in holdings {
        let slices = index
            .iter()
            .find(|(k, _)| *k == holding.key)
            .map_or(&empty, |(_, s)| s);
        for (portfolio_id, quantity) in slices {
            let value = holding_value(holding.key, *quantity, prices)?;
            values.bump(Some(*portfolio_id), value)?;
        }
        let remaining = sub(holding.quantity, slices_sum(slices)?)?;
        if remaining > Decimal::ZERO {
            let value = holding_value(holding.key, remaining, prices)?;
            values.bump(None, value)?;
        }
    }
    Ok(Values(
        values.0.into_iter().map(|(k, v)| (k, money(v))).collect(),
    ))
}

/// `consolidated_value(..., cash=0)`: every holding whole, validated first.
///
/// # Errors
/// As [`portfolio_values`].
pub fn consolidated_value(
    holdings: &[Holding],
    allocations: &[Allocation],
    portfolios: &[LedgerPortfolio],
    prices: &HashMap<i64, Decimal>,
) -> Result<Decimal, WorkerError> {
    validate_allocations(allocations, portfolios)?;
    validate_against_holdings(holdings, allocations)?;
    let mut total = Decimal::ZERO;
    for holding in holdings {
        total = add(total, holding_value(holding.key, holding.quantity, prices)?)?;
    }
    Ok(money(total))
}

// ---------------------------------------------------------------------------------------------
// cash_ledger
// ---------------------------------------------------------------------------------------------

/// `CashFlowKind`, migration 0022's seven.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CashFlowKind {
    /// `EXTERNAL_DEPOSIT`.
    ExternalDeposit,
    /// `EXTERNAL_WITHDRAWAL`.
    ExternalWithdrawal,
    /// `ASSIGN`.
    Assign,
    /// `RELEASE`.
    Release,
    /// `BUY`.
    Buy,
    /// `SELL`.
    Sell,
    /// `DIVIDEND`.
    Dividend,
}

impl CashFlowKind {
    /// `CashFlowKind(value)`.
    ///
    /// # Errors
    /// Python's `ValueError` for any other value.
    pub fn parse(value: &str) -> Result<Self, WorkerError> {
        Ok(match value {
            "EXTERNAL_DEPOSIT" => Self::ExternalDeposit,
            "EXTERNAL_WITHDRAWAL" => Self::ExternalWithdrawal,
            "ASSIGN" => Self::Assign,
            "RELEASE" => Self::Release,
            "BUY" => Self::Buy,
            "SELL" => Self::Sell,
            "DIVIDEND" => Self::Dividend,
            other => return Err(not_valid(other, "CashFlowKind")),
        })
    }

    /// The stored value (`str(kind)`).
    #[must_use]
    pub const fn value(self) -> &'static str {
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

    const fn is_external(self) -> bool {
        matches!(self, Self::ExternalDeposit | Self::ExternalWithdrawal)
    }

    const fn is_stock(self) -> bool {
        matches!(self, Self::Buy | Self::Sell)
    }

    const fn is_cash_only(self) -> bool {
        matches!(
            self,
            Self::ExternalDeposit | Self::ExternalWithdrawal | Self::Assign | Self::Release
        )
    }

    /// `_UNALLOCATED_SIGN`.
    const fn unallocated_sign(self) -> i64 {
        match self {
            Self::ExternalDeposit | Self::Release => 1,
            Self::ExternalWithdrawal | Self::Assign => -1,
            Self::Buy | Self::Sell | Self::Dividend => 0,
        }
    }

    /// `_PORTFOLIO_SIGN`.
    const fn portfolio_sign(self) -> i64 {
        match self {
            Self::ExternalDeposit | Self::ExternalWithdrawal => 0,
            Self::Assign | Self::Sell | Self::Dividend => 1,
            Self::Release | Self::Buy => -1,
        }
    }
}

/// `PortfolioCashFlow` (the fields the job reads).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LedgerCashFlow {
    /// `broker_account_id`.
    pub broker_account_id: i64,
    /// `kind`.
    pub kind: CashFlowKind,
    /// `amount`, always positive.
    pub amount: Decimal,
    /// `occurred_on`.
    pub occurred_on: NaiveDate,
    /// `portfolio_id`; `None` for the external kinds.
    pub portfolio_id: Option<i64>,
}

fn opt_str(value: Option<Decimal>) -> String {
    value.map_or_else(|| "None".to_owned(), |v| v.to_string())
}

impl LedgerCashFlow {
    /// The constructor with `__post_init__`'s §4.4 checks and sentences.
    ///
    /// # Errors
    /// `ValueError` for a row that breaks a rule.
    pub fn new(
        broker_account_id: i64,
        kind: CashFlowKind,
        amount: Decimal,
        occurred_on: NaiveDate,
        portfolio_id: Option<i64>,
        instrument_id: Option<i64>,
        quantity: Option<Decimal>,
    ) -> Result<Self, WorkerError> {
        let k = kind.value();
        if amount <= Decimal::ZERO {
            return Err(value_error(format!(
                "a cash flow moves a positive amount and its direction comes from its kind; got \
                 {amount} for {k}"
            )));
        }
        if money(amount) != amount {
            return Err(value_error(format!(
                "{amount} is finer than a paisa, and portfolio_cash_flow.amount stores two \
                 decimal places; round it at write time rather than letting a balance exist that \
                 the ledger cannot reproduce"
            )));
        }
        if let (true, Some(id)) = (kind.is_external(), portfolio_id) {
            return Err(value_error(format!(
                "{k} crosses the boundary of the whole broker account and belongs to no \
                 portfolio, but it names portfolio {id}; money arriving from outside lands in \
                 Unallocated, and assigning it is a separate event (spec section 4.4)"
            )));
        }
        if !kind.is_external() && portfolio_id.is_none() {
            return Err(value_error(format!(
                "{k} is an internal flow and must name the portfolio it happened in; without one \
                 it can be neither valued nor attributed, and a per-portfolio XIRR computed \
                 around it would silently omit a contribution (spec section 4.4)"
            )));
        }
        if kind.is_stock() {
            if instrument_id.is_none() {
                return Err(value_error(format!(
                    "{k} moves cash against a stock and must name it"
                )));
            }
            match quantity {
                Some(q) if q > Decimal::ZERO => {
                    let mut rounded =
                        q.round_dp_with_strategy(4, RoundingStrategy::MidpointAwayFromZero);
                    rounded.rescale(4);
                    if rounded != q {
                        return Err(value_error(format!(
                            "{q} is finer than the four decimals a quantity stores \
                             (allocation_ledger.QUANTITY_PRECISION)"
                        )));
                    }
                }
                other => {
                    return Err(value_error(format!(
                        "{k} must carry the quantity traded; got {}",
                        opt_str(other)
                    )));
                }
            }
        } else {
            if let (true, Some(id)) = (kind.is_cash_only(), instrument_id) {
                return Err(value_error(format!(
                    "{k} moves cash and nothing else, but it names instrument {id}; an activity \
                     feed would read that as a trade that never happened"
                )));
            }
            if let Some(q) = quantity {
                return Err(value_error(format!(
                    "{k} moves no shares, so it carries no quantity; got {q}"
                )));
            }
        }
        Ok(Self {
            broker_account_id,
            kind,
            amount,
            occurred_on,
            portfolio_id,
        })
    }
}

fn signed(sign: i64, amount: Decimal) -> Result<Decimal, WorkerError> {
    mul(Decimal::from(sign), amount)
}

/// `unallocated_cash_by_broker`: every broker account's Unallocated bucket, in first-seen order.
///
/// # Errors
/// An overflow (never on stored values).
pub fn unallocated_cash_by_broker(
    flows: &[LedgerCashFlow],
) -> Result<Vec<(i64, Decimal)>, WorkerError> {
    let mut totals: Vec<(i64, Decimal)> = Vec::new();
    for flow in flows {
        let delta = signed(flow.kind.unallocated_sign(), flow.amount)?;
        if let Some((_, v)) = totals
            .iter_mut()
            .find(|(id, _)| *id == flow.broker_account_id)
        {
            *v = add(*v, delta)?;
        } else {
            totals.push((flow.broker_account_id, add(Decimal::ZERO, delta)?));
        }
    }
    Ok(totals.into_iter().map(|(k, v)| (k, money(v))).collect())
}

/// `portfolio_cash_by_portfolio`: every portfolio's internal cash, in first-seen order.
///
/// # Errors
/// An overflow (never on stored values).
pub fn portfolio_cash_by_portfolio(
    flows: &[LedgerCashFlow],
) -> Result<Vec<(i64, Decimal)>, WorkerError> {
    let mut totals: Vec<(i64, Decimal)> = Vec::new();
    for flow in flows {
        let Some(portfolio_id) = flow.portfolio_id else {
            continue;
        };
        let delta = signed(flow.kind.portfolio_sign(), flow.amount)?;
        if let Some((_, v)) = totals.iter_mut().find(|(id, _)| *id == portfolio_id) {
            *v = add(*v, delta)?;
        } else {
            totals.push((portfolio_id, add(Decimal::ZERO, delta)?));
        }
    }
    Ok(totals.into_iter().map(|(k, v)| (k, money(v))).collect())
}

/// `total_cash`: the two sign tables added, so `ASSIGN`/`RELEASE` cancel.
///
/// # Errors
/// An overflow (never on stored values).
pub fn total_cash(flows: &[LedgerCashFlow]) -> Result<Decimal, WorkerError> {
    let mut total = Decimal::ZERO;
    for flow in flows {
        let sign = flow.kind.unallocated_sign() + flow.kind.portfolio_sign();
        total = add(total, signed(sign, flow.amount)?)?;
    }
    Ok(money(total))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    fn dec(s: &str) -> Decimal {
        s.parse().unwrap()
    }

    fn key(i: i64) -> HoldingKey {
        HoldingKey {
            instrument_id: i,
            broker_account_id: 1,
        }
    }

    fn day() -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 1, 5).unwrap()
    }

    fn capital(id: i64) -> LedgerPortfolio {
        LedgerPortfolio {
            portfolio_id: id,
            name: format!("P{id}"),
            kind: PortfolioKind::Capital,
            started_on: day(),
        }
    }

    #[test]
    fn money_is_half_up_to_paise_with_two_places() {
        assert_eq!(money(dec("0.005")).to_string(), "0.01");
        assert_eq!(money(dec("-0.005")).to_string(), "-0.01");
        assert_eq!(money(dec("0.0049")).to_string(), "0.00");
        assert_eq!(money(dec("5")).to_string(), "5.00");
        assert_eq!(money(Decimal::ZERO).to_string(), "0.00");
    }

    #[test]
    fn each_holding_is_quantised_before_it_is_summed() {
        // Prices chosen so a float, or quantising the sum, would be a paisa off (Python's
        // `test_consolidated_equals_the_sum_of_its_parts_to_the_paisa`).
        let prices: HashMap<i64, Decimal> = [
            (1, dec("333.3333")),
            (2, dec("0.0050")),
            (3, dec("10.0050")),
        ]
        .into();
        let holdings = vec![
            Holding::new(key(1), dec("3")).unwrap(),
            Holding::new(key(2), dec("1")).unwrap(),
            Holding::new(key(3), dec("1")).unwrap(),
        ];
        let allocations = vec![
            Allocation::new(key(1), Some(10), dec("3")).unwrap(),
            Allocation::new(key(2), Some(11), dec("1")).unwrap(),
        ];
        let portfolios = vec![capital(10), capital(11)];
        let values = portfolio_values(&holdings, &allocations, &portfolios, &prices).unwrap();
        let whole = consolidated_value(&holdings, &allocations, &portfolios, &prices).unwrap();
        assert_eq!(values.get(Some(10)).unwrap().to_string(), "1000.00");
        assert_eq!(values.get(Some(11)).unwrap().to_string(), "0.01");
        assert_eq!(values.get(None).unwrap().to_string(), "10.01");
        let parts = values.0.iter().fold(Decimal::ZERO, |a, (_, v)| a + v);
        assert_eq!(parts, whole);
    }

    #[test]
    fn a_holding_listed_twice_is_over_allocated_as_python_says() {
        let prices: HashMap<i64, Decimal> = [(1, dec("10"))].into();
        let holdings = vec![
            Holding::new(key(1), dec("10.0000")).unwrap(),
            Holding::new(key(1), dec("20.0000")).unwrap(),
        ];
        let allocations = vec![
            Allocation::new(key(1), Some(10), dec("10.0000")).unwrap(),
            Allocation::new(key(1), Some(11), dec("20.0000")).unwrap(),
        ];
        let err = portfolio_values(
            &holdings,
            &allocations,
            &[capital(10), capital(11)],
            &prices,
        )
        .unwrap_err();
        assert_eq!(err.python_type(), "ValueError");
        assert_eq!(
            err.to_string(),
            "holding HoldingKey(instrument_id=1, broker_account_id=1) is over-allocated: \
             30.0000 shares are filed across 2 portfolio(s) but only 20.0000 are held. Free \
             10.0000 before this can be valued"
        );
    }

    #[test]
    fn a_slice_in_a_monitoring_view_is_refused() {
        let lens = LedgerPortfolio {
            kind: PortfolioKind::Monitoring,
            ..capital(10)
        };
        let allocations = vec![Allocation::new(key(1), Some(10), dec("1")).unwrap()];
        let err = validate_allocations(&allocations, &[lens]).unwrap_err();
        assert!(err.to_string().starts_with("'P10' is a monitoring view"));
    }

    #[test]
    fn assign_cancels_in_the_total_and_moves_between_pockets() {
        let flows = vec![
            LedgerCashFlow::new(
                1,
                CashFlowKind::ExternalDeposit,
                dec("1000.00"),
                day(),
                None,
                None,
                None,
            )
            .unwrap(),
            LedgerCashFlow::new(
                1,
                CashFlowKind::Assign,
                dec("400.00"),
                day(),
                Some(10),
                None,
                None,
            )
            .unwrap(),
            LedgerCashFlow::new(
                1,
                CashFlowKind::Buy,
                dec("150.00"),
                day(),
                Some(10),
                Some(5),
                Some(dec("3.0000")),
            )
            .unwrap(),
        ];
        assert_eq!(
            unallocated_cash_by_broker(&flows).unwrap(),
            vec![(1, dec("600.00"))]
        );
        assert_eq!(
            portfolio_cash_by_portfolio(&flows).unwrap(),
            vec![(10, dec("250.00"))]
        );
        assert_eq!(total_cash(&flows).unwrap().to_string(), "850.00");
    }

    #[test]
    fn the_constructor_refuses_what_section_4_4_forbids() {
        let err = LedgerCashFlow::new(
            1,
            CashFlowKind::ExternalDeposit,
            dec("5.00"),
            day(),
            Some(3),
            None,
            None,
        )
        .unwrap_err();
        assert!(err.to_string().starts_with(
            "EXTERNAL_DEPOSIT crosses the boundary of the whole broker account and belongs to no \
             portfolio, but it names portfolio 3;"
        ));
        let err = LedgerCashFlow::new(
            1,
            CashFlowKind::Buy,
            dec("5.00"),
            day(),
            Some(3),
            Some(1),
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.to_string(),
            "BUY must carry the quantity traded; got None"
        );
        assert_eq!(
            CashFlowKind::parse("GIFT").unwrap_err().to_string(),
            "'GIFT' is not a valid CashFlowKind"
        );
    }
}
