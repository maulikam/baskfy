//! Ports the parts of `baskfy_core.reconciliation` the read routes call: the inbox entry, the
//! freeze report (§4.3) and the attention ribbon (§6.4).

use std::collections::{BTreeSet, HashSet};

use chrono::NaiveDate;
use serde::Serialize;

use super::ledger::{
    Allocation, DomainResult, Holding, HoldingKey, LedgerQuestion, slice_index,
    unallocated_holdings,
};
use super::pydec::Dec;

/// `DEFAULT_STALE_AFTER_DAYS`.
const DEFAULT_STALE_AFTER_DAYS: i64 = 4;

/// `ReconciliationState`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) enum ReconciliationState {
    #[serde(rename = "OPEN")]
    Open,
    #[serde(rename = "RESOLVED")]
    Resolved,
    #[serde(rename = "DISMISSED")]
    Dismissed,
}

impl ReconciliationState {
    pub(crate) fn parse(s: &str) -> DomainResult<Self> {
        match s {
            "OPEN" => Ok(Self::Open),
            "RESOLVED" => Ok(Self::Resolved),
            "DISMISSED" => Ok(Self::Dismissed),
            other => Err(format!("'{other}' is not a valid ReconciliationState")),
        }
    }

    /// `freezes`.
    pub(crate) fn freezes(self) -> bool {
        self == Self::Open
    }
}

/// `InboxEntry`.
#[derive(Debug, Clone)]
pub(crate) struct InboxEntry {
    pub(crate) item_id: i64,
    pub(crate) item: LedgerQuestion,
    pub(crate) detected_on: NaiveDate,
    pub(crate) state: ReconciliationState,
    pub(crate) resolved_portfolio_id: Option<i64>,
    pub(crate) resolved_on: Option<NaiveDate>,
}

impl InboxEntry {
    /// With `__post_init__`'s two refusals.
    pub(crate) fn new(
        item_id: i64,
        item: LedgerQuestion,
        detected_on: NaiveDate,
        state: ReconciliationState,
        resolved_portfolio_id: Option<i64>,
        resolved_on: Option<NaiveDate>,
    ) -> DomainResult<Self> {
        if (state == ReconciliationState::Resolved) != resolved_portfolio_id.is_some() {
            return Err(format!(
                "reconciliation item {item_id} is {state:?} and its portfolio disagrees"
            ));
        }
        if state == ReconciliationState::Open && resolved_on.is_some() {
            return Err(format!(
                "reconciliation item {item_id} is OPEN but carries a resolution date"
            ));
        }
        Ok(Self {
            item_id,
            item,
            detected_on,
            state,
            resolved_portfolio_id,
            resolved_on,
        })
    }

    /// `freezes`.
    pub(crate) fn freezes(&self) -> bool {
        self.state.freezes()
    }

    /// `key`.
    pub(crate) fn key(&self) -> HoldingKey {
        self.item.key
    }
}

/// `LedgerPosition`.
#[derive(Debug, Clone)]
pub(crate) struct LedgerPosition {
    pub(crate) portfolio_id: Option<i64>,
    pub(crate) holding: Holding,
}

/// `FreezeReport`.
#[derive(Debug, Clone, Default)]
pub(crate) struct FreezeReport {
    pub(crate) frozen: HashSet<HoldingKey>,
    pub(crate) pending_portfolio_ids: BTreeSet<i64>,
    pub(crate) unallocated_pending: bool,
}

impl FreezeReport {
    /// `consolidated_pending`.
    pub(crate) fn consolidated_pending(&self) -> bool {
        !self.frozen.is_empty()
    }

    /// `is_frozen`.
    pub(crate) fn is_frozen(&self, key: &HoldingKey) -> bool {
        self.frozen.contains(key)
    }

    /// `is_pending(portfolio_id)` for a real portfolio.
    pub(crate) fn is_pending(&self, portfolio_id: i64) -> bool {
        self.pending_portfolio_ids.contains(&portfolio_id)
    }
}

/// `freeze_report`.
pub(crate) fn freeze_report(entries: &[InboxEntry], positions: &[LedgerPosition]) -> FreezeReport {
    let frozen: HashSet<HoldingKey> = entries
        .iter()
        .filter(|e| e.freezes())
        .map(InboxEntry::key)
        .collect();
    let mut pending = BTreeSet::new();
    let mut unallocated_pending = false;
    let mut seen = HashSet::new();
    for position in positions {
        if !frozen.contains(&position.holding.key) {
            continue;
        }
        seen.insert(position.holding.key);
        match position.portfolio_id {
            None => unallocated_pending = true,
            Some(p) => {
                pending.insert(p);
            }
        }
    }
    if frozen.iter().any(|k| !seen.contains(k)) {
        unallocated_pending = true;
    }
    FreezeReport {
        frozen,
        pending_portfolio_ids: pending,
        unallocated_pending,
    }
}

/// `positions_from_allocations(holdings, allocations)` (no lenses — the ribbon passes none).
fn positions_from_allocations(
    holdings: &[Holding],
    allocations: &[Allocation],
) -> DomainResult<Vec<LedgerPosition>> {
    let index = slice_index(allocations)?;
    let mut out = Vec::new();
    for h in holdings {
        let mut slices = index.get(&h.key).cloned().unwrap_or_default();
        slices.sort_by_key(|(p, _)| *p);
        let mut filed = Dec::lit("0");
        for (pid, q) in &slices {
            out.push(LedgerPosition {
                portfolio_id: Some(*pid),
                holding: h.with_quantity(q.clone())?,
            });
            filed = filed.add(q);
        }
        let remaining = h.quantity.sub(&filed);
        if remaining > Dec::zero() || slices.is_empty() {
            out.push(LedgerPosition {
                portfolio_id: None,
                holding: h.with_quantity(remaining)?,
            });
        }
    }
    Ok(out)
}

/// `AttentionKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AttentionKind {
    ReconciliationPending,
    StalePriceData,
    HoldingsUnallocated,
    RebalanceAvailable,
}

impl AttentionKind {
    /// The enum value.
    pub(crate) fn value(self) -> &'static str {
        match self {
            Self::ReconciliationPending => "RECONCILIATION_PENDING",
            Self::StalePriceData => "STALE_PRICE_DATA",
            Self::HoldingsUnallocated => "HOLDINGS_UNALLOCATED",
            Self::RebalanceAvailable => "REBALANCE_AVAILABLE",
        }
    }
}

/// `AttentionItem`.
#[derive(Debug, Clone)]
pub(crate) struct AttentionItem {
    pub(crate) kind: AttentionKind,
    pub(crate) count: i64,
    pub(crate) subject_ids: Vec<i64>,
    pub(crate) since: Option<NaiveDate>,
}

fn plural(count: i64, singular: &str, plural: &str) -> String {
    format!("{count} {}", if count == 1 { singular } else { plural })
}

impl AttentionItem {
    /// `message`.
    pub(crate) fn message(&self) -> String {
        match self.kind {
            AttentionKind::ReconciliationPending => format!(
                "{} pending reconciliation — returns are on hold until you answer",
                plural(self.count, "holding is", "holdings are")
            ),
            AttentionKind::StalePriceData => match self.since {
                None => "No closing prices yet — values cannot be shown".to_owned(),
                Some(d) => format!(
                    "Prices are still at the close of {}",
                    crate::wire::py_date(d)
                ),
            },
            AttentionKind::HoldingsUnallocated => format!(
                "{} not in any portfolio yet",
                plural(self.count, "holding is", "holdings are")
            ),
            AttentionKind::RebalanceAvailable => format!(
                "Rebalance available on {}",
                plural(self.count, "portfolio", "portfolios")
            ),
        }
    }
}

/// `AttentionInputs` (no expired brokers: the router never passes any).
pub(crate) struct AttentionInputs<'a> {
    pub(crate) as_of: NaiveDate,
    pub(crate) holdings: &'a [Holding],
    pub(crate) allocations: &'a [Allocation],
    pub(crate) entries: &'a [InboxEntry],
    pub(crate) rebalance_due_portfolio_ids: Vec<i64>,
    pub(crate) prices_as_of: Option<NaiveDate>,
    pub(crate) check_prices: bool,
}

/// `attention_items`, in `_ATTENTION_ORDER`.
pub(crate) fn attention_items(inputs: &AttentionInputs<'_>) -> DomainResult<Vec<AttentionItem>> {
    let mut found = Vec::new();
    let frozen = freeze_report(
        inputs.entries,
        &positions_from_allocations(inputs.holdings, inputs.allocations)?,
    );
    if !frozen.frozen.is_empty() {
        found.push(AttentionItem {
            kind: AttentionKind::ReconciliationPending,
            count: i64::try_from(frozen.frozen.len()).unwrap_or(i64::MAX),
            subject_ids: Vec::new(),
            since: None,
        });
    }
    if inputs.check_prices {
        match inputs.prices_as_of {
            None => found.push(AttentionItem {
                kind: AttentionKind::StalePriceData,
                count: 0,
                subject_ids: Vec::new(),
                since: None,
            }),
            Some(p) => {
                let days_behind = (inputs.as_of - p).num_days();
                if days_behind > DEFAULT_STALE_AFTER_DAYS {
                    found.push(AttentionItem {
                        kind: AttentionKind::StalePriceData,
                        count: days_behind,
                        subject_ids: Vec::new(),
                        since: Some(p),
                    });
                }
            }
        }
    }
    let loose = unallocated_holdings(inputs.holdings, inputs.allocations)?;
    if !loose.is_empty() {
        found.push(AttentionItem {
            kind: AttentionKind::HoldingsUnallocated,
            count: i64::try_from(loose.len()).unwrap_or(i64::MAX),
            subject_ids: Vec::new(),
            since: None,
        });
    }
    if !inputs.rebalance_due_portfolio_ids.is_empty() {
        found.push(AttentionItem {
            kind: AttentionKind::RebalanceAvailable,
            count: i64::try_from(inputs.rebalance_due_portfolio_ids.len()).unwrap_or(i64::MAX),
            subject_ids: inputs.rebalance_due_portfolio_ids.clone(),
            since: None,
        });
    }
    Ok(found)
}
