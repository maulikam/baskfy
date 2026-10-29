//! Ports the parts of `baskfy_core.allocation_ledger` (and `baskfy_core.gst.money`) the portfolio
//! read routes call: the enums, holdings and allocations, valuation, and the return figures.
//! Pure. A Python `ValueError`/`KeyError` is an `Err(String)` here; the routes turn it into the
//! 500 Python's catch-all gives it.

use std::collections::{BTreeMap, HashMap};

use baskfy_core::decimal::Rounding;
use chrono::NaiveDate;
use serde::Serialize;

use super::pydec::Dec;

/// A domain refusal (`ValueError` / `KeyError` in Python).
pub(crate) type DomainResult<T> = Result<T, String>;

/// `gst.money`: quantize to the paisa, half-up.
pub(crate) fn money(value: &Dec) -> Dec {
    value
        .quantize(-2, Rounding::HalfUp)
        .unwrap_or_else(|| value.clone())
}

/// `PortfolioKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) enum PortfolioKind {
    #[serde(rename = "CAPITAL")]
    Capital,
    #[serde(rename = "MONITORING")]
    Monitoring,
}

impl PortfolioKind {
    pub(crate) fn parse(s: &str) -> DomainResult<Self> {
        match s {
            "CAPITAL" => Ok(Self::Capital),
            "MONITORING" => Ok(Self::Monitoring),
            other => Err(format!("'{other}' is not a valid PortfolioKind")),
        }
    }
}

/// `PortfolioSource`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) enum PortfolioSource {
    #[serde(rename = "SUBSCRIBED")]
    Subscribed,
    #[serde(rename = "MY_SCREEN")]
    MyScreen,
    #[serde(rename = "MY_STRATEGY")]
    MyStrategy,
    #[serde(rename = "HOLDING_GROUP")]
    HoldingGroup,
}

impl PortfolioSource {
    pub(crate) fn parse(s: &str) -> DomainResult<Self> {
        match s {
            "SUBSCRIBED" => Ok(Self::Subscribed),
            "MY_SCREEN" => Ok(Self::MyScreen),
            "MY_STRATEGY" => Ok(Self::MyStrategy),
            "HOLDING_GROUP" => Ok(Self::HoldingGroup),
            other => Err(format!("'{other}' is not a valid PortfolioSource")),
        }
    }
}

/// `MetricKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) enum MetricKind {
    #[serde(rename = "TWR_SINCE_SUBSCRIBED")]
    TwrSinceSubscribed,
    #[serde(rename = "TWR_SINCE_GO_LIVE")]
    TwrSinceGoLive,
    #[serde(rename = "TWR_SINCE_CREATED")]
    TwrSinceCreated,
    #[serde(rename = "SINCE_GROUPED")]
    SinceGrouped,
}

impl MetricKind {
    /// `label`.
    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::TwrSinceSubscribed => "TWR since you subscribed",
            Self::TwrSinceGoLive => "TWR since go-live",
            Self::TwrSinceCreated => "TWR since created",
            Self::SinceGrouped => "Since grouped",
        }
    }
}

/// `ReconciliationReason`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ReconciliationReason {
    UnallocatedHolding,
    QuantityMismatch,
    UnknownInflow,
    SplitHolding,
}

impl ReconciliationReason {
    pub(crate) fn parse(s: &str) -> DomainResult<Self> {
        match s {
            "UNALLOCATED_HOLDING" => Ok(Self::UnallocatedHolding),
            "QUANTITY_MISMATCH" => Ok(Self::QuantityMismatch),
            "UNKNOWN_INFLOW" => Ok(Self::UnknownInflow),
            "SPLIT_HOLDING" => Ok(Self::SplitHolding),
            other => Err(format!("'{other}' is not a valid ReconciliationReason")),
        }
    }

    /// The enum value.
    pub(crate) fn value(self) -> &'static str {
        match self {
            Self::UnallocatedHolding => "UNALLOCATED_HOLDING",
            Self::QuantityMismatch => "QUANTITY_MISMATCH",
            Self::UnknownInflow => "UNKNOWN_INFLOW",
            Self::SplitHolding => "SPLIT_HOLDING",
        }
    }

    /// `question`.
    pub(crate) fn question(self) -> &'static str {
        match self {
            Self::UnallocatedHolding => {
                "This holding was not in any portfolio — which portfolio should it count against?"
            }
            Self::QuantityMismatch => {
                "The quantity sold is more than we had recorded — which portfolio?"
            }
            Self::UnknownInflow => {
                "We saw shares arrive that we cannot account for — which portfolio?"
            }
            Self::SplitHolding => {
                "You hold this name in more than one portfolio — which of them did you sell from?"
            }
        }
    }
}

/// `HoldingKey`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub(crate) struct HoldingKey {
    pub(crate) instrument_id: i64,
    pub(crate) broker_account_id: i64,
}

/// `Holding` (the ledger's).
#[derive(Debug, Clone)]
pub(crate) struct Holding {
    pub(crate) key: HoldingKey,
    pub(crate) quantity: Dec,
    pub(crate) avg_price: Option<Dec>,
}

impl Holding {
    /// `Holding(...)` with `__post_init__`'s checks.
    pub(crate) fn new(
        key: HoldingKey,
        quantity: Dec,
        avg_price: Option<Dec>,
    ) -> DomainResult<Self> {
        if quantity < Dec::zero() {
            return Err(format!("quantity cannot be negative; got {quantity}"));
        }
        if let Some(p) = &avg_price
            && *p < Dec::zero()
        {
            return Err(format!("avg_price cannot be negative; got {p}"));
        }
        Ok(Self {
            key,
            quantity,
            avg_price,
        })
    }

    /// `replace(holding, quantity=q)` (re-checked, as the frozen dataclass re-runs `__post_init__`).
    pub(crate) fn with_quantity(&self, quantity: Dec) -> DomainResult<Self> {
        Self::new(self.key, quantity, self.avg_price.clone())
    }
}

/// `Portfolio` (the ledger's).
#[derive(Debug, Clone)]
pub(crate) struct LedgerPortfolio {
    pub(crate) portfolio_id: i64,
    pub(crate) name: String,
    pub(crate) kind: PortfolioKind,
    pub(crate) source: PortfolioSource,
    pub(crate) started_on: NaiveDate,
}

/// `Allocation`.
#[derive(Debug, Clone)]
pub(crate) struct Allocation {
    pub(crate) key: HoldingKey,
    pub(crate) portfolio_id: i64,
    pub(crate) quantity: Dec,
}

/// `ReconciliationItem` (the ledger's question).
#[derive(Debug, Clone)]
pub(crate) struct LedgerQuestion {
    pub(crate) key: HoldingKey,
    pub(crate) quantity: Dec,
    pub(crate) reason: ReconciliationReason,
    pub(crate) suggested_portfolio_id: Option<i64>,
}

/// `ReturnFigure`.
#[derive(Debug, Clone)]
pub(crate) struct ReturnFigure {
    pub(crate) kind: MetricKind,
    pub(crate) since: NaiveDate,
    pub(crate) value: Option<Dec>,
    pub(crate) is_model: bool,
    pub(crate) unavailable_reason: Option<String>,
}

impl ReturnFigure {
    /// The constructor with `__post_init__`'s two refusals.
    pub(crate) fn new(
        kind: MetricKind,
        since: NaiveDate,
        value: Option<Dec>,
        is_model: bool,
        unavailable_reason: Option<String>,
    ) -> DomainResult<Self> {
        let has_reason = unavailable_reason.as_deref().is_some_and(|r| !r.is_empty());
        if value.is_none() && !has_reason {
            return Err("a figure with no value must say why".to_owned());
        }
        if value.is_some() && has_reason {
            return Err("a figure cannot both have a value and be unavailable".to_owned());
        }
        Ok(Self {
            kind,
            since,
            value,
            is_model,
            unavailable_reason,
        })
    }

    /// `label`.
    pub(crate) fn label(&self) -> String {
        if self.is_model {
            format!("Model {}", self.kind.label().to_lowercase())
        } else {
            self.kind.label().to_owned()
        }
    }
}

/// `_slice_index`: holding → (portfolio → quantity), refusing a second slice to one portfolio.
pub(crate) fn slice_index(
    allocations: &[Allocation],
) -> DomainResult<HashMap<HoldingKey, Vec<(i64, Dec)>>> {
    let mut index: HashMap<HoldingKey, Vec<(i64, Dec)>> = HashMap::new();
    for a in allocations {
        let slices = index.entry(a.key).or_default();
        if slices.iter().any(|(p, _)| *p == a.portfolio_id) {
            return Err(format!(
                "holding {:?} has two allocations to portfolio {}",
                a.key, a.portfolio_id
            ));
        }
        slices.push((a.portfolio_id, a.quantity.clone()));
    }
    Ok(index)
}

fn sum_slices(slices: Option<&Vec<(i64, Dec)>>) -> Dec {
    slices
        .map(|s| s.iter().fold(Dec::lit("0"), |acc, (_, q)| acc.add(q)))
        .unwrap_or_else(|| Dec::lit("0"))
}

/// `validate_allocations`.
pub(crate) fn validate_allocations(
    allocations: &[Allocation],
    portfolios: &BTreeMap<i64, LedgerPortfolio>,
) -> DomainResult<()> {
    for slices in slice_index(allocations)?.values() {
        for (portfolio_id, _) in slices {
            let Some(p) = portfolios.get(portfolio_id) else {
                return Err(format!(
                    "allocation names portfolio {portfolio_id}, which does not exist"
                ));
            };
            if p.kind == PortfolioKind::Monitoring {
                return Err(format!("{} is a monitoring view", p.name));
            }
        }
    }
    Ok(())
}

/// `validate_against_holdings`.
pub(crate) fn validate_against_holdings(
    holdings: &[Holding],
    allocations: &[Allocation],
) -> DomainResult<()> {
    let held: HashMap<HoldingKey, &Dec> = holdings.iter().map(|h| (h.key, &h.quantity)).collect();
    for (key, slices) in &slice_index(allocations)? {
        let Some(have) = held.get(key) else {
            return Err(format!(
                "allocations name holding {key:?}, which is not held at all"
            ));
        };
        let allocated = sum_slices(Some(slices));
        if allocated > **have {
            return Err(format!("holding {key:?} is over-allocated"));
        }
    }
    Ok(())
}

/// `unallocated_holdings`: each holding's remainder after its slices, where positive.
pub(crate) fn unallocated_holdings(
    holdings: &[Holding],
    allocations: &[Allocation],
) -> DomainResult<Vec<Holding>> {
    let index = slice_index(allocations)?;
    let mut out = Vec::new();
    for h in holdings {
        let remaining = h.quantity.sub(&sum_slices(index.get(&h.key)));
        if remaining > Dec::zero() {
            out.push(h.with_quantity(remaining)?);
        }
    }
    Ok(out)
}

/// `holding_value`: `money(quantity * price)`; a missing price is a `KeyError`.
pub(crate) fn holding_value(holding: &Holding, prices: &HashMap<i64, Dec>) -> DomainResult<Dec> {
    let price = prices
        .get(&holding.key.instrument_id)
        .ok_or_else(|| format!("no price for instrument {}", holding.key.instrument_id))?;
    Ok(money(&holding.quantity.mul(price)))
}

/// `portfolio_values`: every capital portfolio's value (and Unallocated's, under `None`).
pub(crate) fn portfolio_values(
    holdings: &[Holding],
    allocations: &[Allocation],
    portfolios: &BTreeMap<i64, LedgerPortfolio>,
    prices: &HashMap<i64, Dec>,
) -> DomainResult<HashMap<Option<i64>, Dec>> {
    validate_allocations(allocations, portfolios)?;
    validate_against_holdings(holdings, allocations)?;
    let index = slice_index(allocations)?;
    let mut values: HashMap<Option<i64>, Dec> = HashMap::new();
    values.insert(None, Dec::lit("0"));
    for p in portfolios.values() {
        if p.kind == PortfolioKind::Capital {
            values.insert(Some(p.portfolio_id), Dec::lit("0"));
        }
    }
    for h in holdings {
        let slices = index.get(&h.key);
        if let Some(slices) = slices {
            for (pid, q) in slices {
                let v = holding_value(&h.with_quantity(q.clone())?, prices)?;
                let prev = values
                    .get(&Some(*pid))
                    .cloned()
                    .unwrap_or_else(|| Dec::lit("0"));
                values.insert(Some(*pid), prev.add(&v));
            }
        }
        let remaining = h.quantity.sub(&sum_slices(slices));
        if remaining > Dec::zero() {
            let v = holding_value(&h.with_quantity(remaining)?, prices)?;
            let prev = values.get(&None).cloned().unwrap_or_else(|| Dec::lit("0"));
            values.insert(None, prev.add(&v));
        }
    }
    Ok(values.into_iter().map(|(k, v)| (k, money(&v))).collect())
}

/// `consolidated_value`: cash plus every holding, valued once.
pub(crate) fn consolidated_value(
    holdings: &[Holding],
    allocations: &[Allocation],
    portfolios: &BTreeMap<i64, LedgerPortfolio>,
    prices: &HashMap<i64, Dec>,
    cash: &Dec,
) -> DomainResult<Dec> {
    validate_allocations(allocations, portfolios)?;
    validate_against_holdings(holdings, allocations)?;
    let mut total = cash.clone();
    for h in holdings {
        total = total.add(&holding_value(h, prices)?);
    }
    Ok(money(&total))
}

/// `cost_basis`.
pub(crate) fn cost_basis(holding: &Holding) -> Option<Dec> {
    holding.avg_price.as_ref().map(|p| holding.quantity.mul(p))
}

/// `_HEADLINE_BY_SOURCE`.
pub(crate) fn headline_kind(source: PortfolioSource) -> MetricKind {
    match source {
        PortfolioSource::Subscribed => MetricKind::TwrSinceSubscribed,
        PortfolioSource::MyStrategy => MetricKind::TwrSinceGoLive,
        PortfolioSource::MyScreen => MetricKind::TwrSinceCreated,
        PortfolioSource::HoldingGroup => MetricKind::SinceGrouped,
    }
}

/// `model_figure`.
pub(crate) fn model_figure(
    portfolio: &LedgerPortfolio,
    value: Option<Dec>,
) -> DomainResult<ReturnFigure> {
    if portfolio.source != PortfolioSource::Subscribed {
        return Err(format!("{} has no publisher model", portfolio.name));
    }
    let reason = value
        .is_none()
        .then(|| "Publisher has not reported for this period".to_owned());
    ReturnFigure::new(
        MetricKind::TwrSinceSubscribed,
        portfolio.started_on,
        value,
        true,
        reason,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(i: i64, b: i64) -> HoldingKey {
        HoldingKey {
            instrument_id: i,
            broker_account_id: b,
        }
    }

    #[test]
    fn monitoring_views_never_enter_a_total() {
        // Ports test_allocation_ledger.py's criterion-2 assertions.
        let day = NaiveDate::from_ymd_opt(2026, 9, 1).unwrap_or_default();
        let mut portfolios = BTreeMap::new();
        portfolios.insert(
            1,
            LedgerPortfolio {
                portfolio_id: 1,
                name: "Long term".to_owned(),
                kind: PortfolioKind::Capital,
                source: PortfolioSource::HoldingGroup,
                started_on: day,
            },
        );
        portfolios.insert(
            2,
            LedgerPortfolio {
                portfolio_id: 2,
                name: "Watch".to_owned(),
                kind: PortfolioKind::Monitoring,
                source: PortfolioSource::MyScreen,
                started_on: day,
            },
        );
        let holdings = vec![
            Holding::new(key(1, 1), Dec::lit("100"), Some(Dec::lit("10")))
                .unwrap_or_else(|_| unreachable!()),
            Holding::new(key(2, 1), Dec::lit("10"), None).unwrap_or_else(|_| unreachable!()),
        ];
        let allocations = vec![Allocation {
            key: key(1, 1),
            portfolio_id: 1,
            quantity: Dec::lit("60"),
        }];
        let prices: HashMap<i64, Dec> = [(1, Dec::lit("12.50")), (2, Dec::lit("3"))].into();
        let values =
            portfolio_values(&holdings, &allocations, &portfolios, &prices).unwrap_or_default();
        assert_eq!(
            values.get(&Some(1)).map(Dec::to_sci_string),
            Some("750.00".to_owned())
        );
        assert_eq!(
            values.get(&None).map(Dec::to_sci_string),
            Some("530.00".to_owned())
        );
        assert!(!values.contains_key(&Some(2)));
        let total = consolidated_value(
            &holdings,
            &allocations,
            &portfolios,
            &prices,
            &Dec::lit("0"),
        );
        assert_eq!(total.map(|t| t.to_sci_string()), Ok("1280.00".to_owned()));
        // A monitoring view may not hold an allocation.
        let bad = vec![Allocation {
            key: key(2, 1),
            portfolio_id: 2,
            quantity: Dec::lit("1"),
        }];
        assert!(validate_allocations(&bad, &portfolios).is_err());
        // Over-allocation is refused.
        let over = vec![Allocation {
            key: key(1, 1),
            portfolio_id: 1,
            quantity: Dec::lit("101"),
        }];
        assert!(validate_against_holdings(&holdings, &over).is_err());
        let loose = unallocated_holdings(&holdings, &allocations).unwrap_or_default();
        assert_eq!(loose.len(), 2);
        assert_eq!(loose[0].quantity.to_sci_string(), "40");
        assert_eq!(money(&Dec::lit("2.675")).to_sci_string(), "2.68");
    }
}
