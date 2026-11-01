//! Ports `baskfy_core.grouping_suggestions.suggest_groupings` (§6.6's first-run helper) and the
//! three bases it ranks: sector, purchase era, basket overlap.

use std::cmp::Ordering;
use std::collections::{BTreeSet, HashMap};

use baskfy_core::decimal::Rounding;
use chrono::{Datelike, NaiveDate};
use serde::Serialize;

use super::ledger::{DomainResult, Holding, HoldingKey, holding_value, money};
use super::pydec::Dec;

const MIN_HOLDINGS_PER_SUGGESTION: usize = 2;
const DEFAULT_SUGGESTION_LIMIT: usize = 6;
const ERA_HORIZON_YEARS: i32 = 5;
const FINANCIAL_YEAR_START_MONTH: u32 = 4;

/// `SuggestionBasis`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) enum SuggestionBasis {
    #[serde(rename = "SECTOR")]
    Sector,
    #[serde(rename = "PURCHASE_ERA")]
    PurchaseEra,
    #[serde(rename = "BASKET_OVERLAP")]
    BasketOverlap,
}

impl SuggestionBasis {
    fn confidence_tier(self) -> i64 {
        match self {
            Self::BasketOverlap => 0,
            Self::Sector => 1,
            Self::PurchaseEra => 2,
        }
    }
}

/// `SubscribedBasket`.
pub(crate) struct SubscribedBasket {
    pub(crate) basket_id: i64,
    pub(crate) name: String,
    pub(crate) constituent_instrument_ids: BTreeSet<i64>,
}

/// `GroupingSuggestion`.
#[derive(Debug, Clone)]
pub(crate) struct GroupingSuggestion {
    pub(crate) basis: SuggestionBasis,
    pub(crate) proposed_name: String,
    pub(crate) keys: Vec<HoldingKey>,
    pub(crate) value: Dec,
    pub(crate) rationale: String,
    pub(crate) basket_id: Option<i64>,
    pub(crate) basket_coverage: Option<Dec>,
    pub(crate) missing_instrument_ids: Vec<i64>,
}

/// `SuggestionInputs`.
pub(crate) struct SuggestionInputs {
    pub(crate) sectors: HashMap<i64, String>,
    /// `first_bought`, in the caller's insertion order.
    pub(crate) first_bought: Vec<(HoldingKey, NaiveDate)>,
    pub(crate) as_of: NaiveDate,
    pub(crate) baskets: Vec<SubscribedBasket>,
}

fn sorted_keys(keys: &[HoldingKey]) -> Vec<HoldingKey> {
    let mut out = keys.to_vec();
    out.sort();
    out
}

fn group_value(keys: &[HoldingKey], values: &HashMap<HoldingKey, Dec>) -> Dec {
    let total = keys.iter().fold(Dec::lit("0"), |acc, k| {
        acc.add(values.get(k).unwrap_or(&Dec::zero()))
    });
    money(&total)
}

fn holding_values(
    holdings: &[Holding],
    prices: &HashMap<i64, Dec>,
) -> DomainResult<HashMap<HoldingKey, Dec>> {
    holdings
        .iter()
        .map(|h| Ok((h.key, holding_value(h, prices)?)))
        .collect()
}

fn rank_key_cmp(a: &GroupingSuggestion, b: &GroupingSuggestion) -> Ordering {
    b.value
        .cmp(&a.value)
        .then_with(|| b.keys.len().cmp(&a.keys.len()))
        .then_with(|| a.basis.confidence_tier().cmp(&b.basis.confidence_tier()))
        .then_with(|| a.proposed_name.cmp(&b.proposed_name))
}

/// Groups `keys` by a label, in first-seen label order (a Python `dict`'s).
fn grouped(pairs: impl Iterator<Item = (String, HoldingKey)>) -> Vec<(String, Vec<HoldingKey>)> {
    let mut out: Vec<(String, Vec<HoldingKey>)> = Vec::new();
    for (label, key) in pairs {
        match out.iter_mut().find(|(l, _)| *l == label) {
            Some((_, keys)) => keys.push(key),
            None => out.push((label, vec![key])),
        }
    }
    out
}

fn suggest_by_sector(
    unallocated: &[Holding],
    prices: &HashMap<i64, Dec>,
    sectors: &HashMap<i64, String>,
) -> DomainResult<Vec<GroupingSuggestion>> {
    let values = holding_values(unallocated, prices)?;
    let groups = grouped(unallocated.iter().filter_map(|h| {
        sectors
            .get(&h.key.instrument_id)
            .map(|s| (s.clone(), h.key))
    }));
    let mut out = Vec::new();
    for (sector, keys) in groups {
        if keys.len() < MIN_HOLDINGS_PER_SUGGESTION {
            continue;
        }
        let ordered = sorted_keys(&keys);
        out.push(GroupingSuggestion {
            basis: SuggestionBasis::Sector,
            rationale: format!("These {} holdings are all {sector}.", ordered.len()),
            value: group_value(&ordered, &values),
            proposed_name: sector,
            keys: ordered,
            basket_id: None,
            basket_coverage: None,
            missing_instrument_ids: Vec::new(),
        });
    }
    out.sort_by(rank_key_cmp);
    Ok(out)
}

fn fy_start(day: NaiveDate) -> i32 {
    if day.month() >= FINANCIAL_YEAR_START_MONTH {
        day.year()
    } else {
        day.year() - 1
    }
}

/// `gst.financial_year(start, 4)`: `2026-27`.
fn financial_year(start: i32) -> String {
    format!("{start}-{:02}", (start + 1).rem_euclid(100))
}

/// `purchase_era`.
fn purchase_era(bought_on: NaiveDate, as_of: NaiveDate) -> DomainResult<String> {
    if bought_on > as_of {
        return Err(format!(
            "a purchase dated {bought_on} is after the as-of date {as_of}"
        ));
    }
    let start = fy_start(bought_on);
    let cutoff = fy_start(as_of) - ERA_HORIZON_YEARS;
    Ok(if start <= cutoff {
        format!("Bought in FY {} or earlier", financial_year(cutoff))
    } else {
        format!("Bought in FY {}", financial_year(start))
    })
}

fn suggest_by_purchase_era(
    unallocated: &[Holding],
    prices: &HashMap<i64, Dec>,
    first_bought: &[(HoldingKey, NaiveDate)],
    as_of: NaiveDate,
) -> DomainResult<Vec<GroupingSuggestion>> {
    let values = holding_values(unallocated, prices)?;
    let mut labelled = Vec::new();
    for h in unallocated {
        if let Some((_, on)) = first_bought.iter().find(|(k, _)| *k == h.key) {
            labelled.push((purchase_era(*on, as_of)?, h.key));
        }
    }
    let mut out = Vec::new();
    for (era, keys) in grouped(labelled.into_iter()) {
        if keys.len() < MIN_HOLDINGS_PER_SUGGESTION {
            continue;
        }
        let ordered = sorted_keys(&keys);
        out.push(GroupingSuggestion {
            basis: SuggestionBasis::PurchaseEra,
            rationale: format!(
                "You first bought these {} holdings in the same year.",
                ordered.len()
            ),
            value: group_value(&ordered, &values),
            proposed_name: era,
            keys: ordered,
            basket_id: None,
            basket_coverage: None,
            missing_instrument_ids: Vec::new(),
        });
    }
    out.sort_by(rank_key_cmp);
    Ok(out)
}

fn suggest_by_basket_overlap(
    unallocated: &[Holding],
    prices: &HashMap<i64, Dec>,
    basket: &SubscribedBasket,
) -> DomainResult<Option<GroupingSuggestion>> {
    let values = holding_values(unallocated, prices)?;
    let matched: Vec<HoldingKey> = unallocated
        .iter()
        .filter(|h| {
            basket
                .constituent_instrument_ids
                .contains(&h.key.instrument_id)
        })
        .map(|h| h.key)
        .collect();
    if matched.len() < MIN_HOLDINGS_PER_SUGGESTION {
        return Ok(None);
    }
    let held: BTreeSet<i64> = matched.iter().map(|k| k.instrument_id).collect();
    let n_held = i64::try_from(held.len()).unwrap_or(i64::MAX);
    let n_all = i64::try_from(basket.constituent_instrument_ids.len()).unwrap_or(i64::MAX);
    let coverage = Dec::int(n_held)
        .div(&Dec::int(n_all))
        .and_then(|c| c.quantize(-4, Rounding::HalfUp))
        .ok_or("coverage")?;
    let missing: Vec<i64> = basket
        .constituent_instrument_ids
        .difference(&held)
        .copied()
        .collect();
    let ordered = sorted_keys(&matched);
    Ok(Some(GroupingSuggestion {
        basis: SuggestionBasis::BasketOverlap,
        proposed_name: basket.name.clone(),
        value: group_value(&ordered, &values),
        keys: ordered,
        rationale: format!(
            "You subscribe to {}, and you already hold {} of its {} stocks.",
            basket.name, n_held, n_all
        ),
        basket_id: Some(basket.basket_id),
        basket_coverage: Some(coverage),
        missing_instrument_ids: missing,
    }))
}

/// `suggest_groupings(unallocated, prices, inputs)` with the default limit of six.
pub(crate) fn suggest_groupings(
    unallocated: &[Holding],
    prices: &HashMap<i64, Dec>,
    inputs: &SuggestionInputs,
) -> DomainResult<Vec<GroupingSuggestion>> {
    let mut all = Vec::new();
    if !inputs.sectors.is_empty() {
        all.extend(suggest_by_sector(unallocated, prices, &inputs.sectors)?);
    }
    if !inputs.first_bought.is_empty() {
        all.extend(suggest_by_purchase_era(
            unallocated,
            prices,
            &inputs.first_bought,
            inputs.as_of,
        )?);
    }
    for basket in &inputs.baskets {
        if let Some(s) = suggest_by_basket_overlap(unallocated, prices, basket)? {
            all.push(s);
        }
    }
    all.sort_by(rank_key_cmp);
    all.truncate(DEFAULT_SUGGESTION_LIMIT);
    Ok(all)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn eras_and_names() {
        let as_of = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap_or_default();
        let d = |y, m| NaiveDate::from_ymd_opt(y, m, 1).unwrap_or_default();
        assert_eq!(
            purchase_era(d(2026, 3), as_of),
            Ok("Bought in FY 2025-26".to_owned())
        );
        assert_eq!(
            purchase_era(d(2026, 4), as_of),
            Ok("Bought in FY 2026-27".to_owned())
        );
        assert_eq!(
            purchase_era(d(2019, 4), as_of),
            Ok("Bought in FY 2021-22 or earlier".to_owned())
        );
        assert!(purchase_era(d(2027, 1), as_of).is_err());
        assert_eq!(financial_year(1999), "1999-00");
    }
}
