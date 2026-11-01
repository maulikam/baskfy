//! Port of `baskfy_core.rank_buffer`: the rank-buffer rebalancing rule.

use std::collections::{BTreeMap, BTreeSet};

use rust_decimal::{Decimal, RoundingStrategy};

/// `WEIGHT_STEP`: weights at `numeric(10,6)`.
pub const WEIGHT_STEP_DP: u32 = 6;

/// `ExitReason`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExitReason {
    /// `rank_outside_buffer`.
    RankOutsideBuffer,
    /// `not_in_screen`.
    NotInScreen,
    /// `delisted`.
    Delisted,
}

impl ExitReason {
    /// The enum's string value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::RankOutsideBuffer => "rank_outside_buffer",
            Self::NotInScreen => "not_in_screen",
            Self::Delisted => "delisted",
        }
    }
}

/// `Action`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    /// `enter`.
    Enter,
    /// `hold`.
    Hold,
}

impl Action {
    /// The enum's string value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Enter => "enter",
            Self::Hold => "hold",
        }
    }
}

/// `ScreenRank`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScreenRank {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `rank`.
    pub rank: i64,
}

/// `HeldName`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HeldName {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `quantity`.
    pub quantity: Option<Decimal>,
    /// `delisted`.
    pub delisted: bool,
}

/// `RebalanceRow`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RebalanceRow {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `rank`.
    pub rank: Option<i64>,
    /// `reason`.
    pub reason: Option<ExitReason>,
}

/// `TargetWeight`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TargetWeight {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `rank`.
    pub rank: i64,
    /// `weight`.
    pub weight: Decimal,
    /// `action`.
    pub action: Action,
}

/// `RebalancePlan`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RebalancePlan {
    /// `top_n`.
    pub top_n: i64,
    /// `hold_buffer`.
    pub hold_buffer: i64,
    /// `exits`.
    pub exits: Vec<RebalanceRow>,
    /// `inside_wrh`.
    pub inside_wrh: Vec<RebalanceRow>,
    /// `entries`.
    pub entries: Vec<RebalanceRow>,
    /// `holds`.
    pub holds: Vec<RebalanceRow>,
    /// `target_weights`.
    pub target_weights: Vec<TargetWeight>,
}

/// `inside_hold_band(rank, top_n, hold_buffer)`.
#[must_use]
pub fn inside_hold_band(rank: i64, top_n: i64, hold_buffer: i64) -> bool {
    rank <= top_n + hold_buffer
}

fn row(rank: &ScreenRank) -> RebalanceRow {
    RebalanceRow {
        instrument_id: rank.instrument_id,
        symbol: rank.symbol.clone(),
        name: rank.name.clone(),
        rank: Some(rank.rank),
        reason: None,
    }
}

/// `plan_rebalance(ranked, held, top_n=, hold_buffer=)`.
///
/// # Errors
/// Python's `ValueError` message for a bad `top_n` or `hold_buffer`.
pub fn plan_rebalance(
    ranked: &[ScreenRank],
    held: &[HeldName],
    top_n: i64,
    hold_buffer: i64,
) -> Result<RebalancePlan, String> {
    if top_n < 1 {
        return Err(format!("top_n must be at least 1, got {top_n}"));
    }
    if hold_buffer < 0 {
        return Err(format!(
            "hold_buffer must not be negative, got {hold_buffer}"
        ));
    }
    // A dict keyed by id: a repeated id keeps its first position and its last row.
    let mut order: Vec<i64> = Vec::new();
    let mut by_id: BTreeMap<i64, &ScreenRank> = BTreeMap::new();
    for r in ranked {
        if by_id.insert(r.instrument_id, r).is_none() {
            order.push(r.instrument_id);
        }
    }
    let held_ids: BTreeSet<i64> = held.iter().map(|h| h.instrument_id).collect();
    let limit = top_n + hold_buffer;
    let mut exits = Vec::new();
    let mut inside = Vec::new();
    let mut holds = Vec::new();
    for holding in held {
        let r = by_id.get(&holding.instrument_id).copied();
        let reason = if holding.delisted {
            Some(ExitReason::Delisted)
        } else {
            match r {
                None => Some(ExitReason::NotInScreen),
                Some(r) if r.rank > limit => Some(ExitReason::RankOutsideBuffer),
                Some(_) => None,
            }
        };
        if let Some(reason) = reason {
            exits.push(RebalanceRow {
                instrument_id: holding.instrument_id,
                symbol: holding.symbol.clone(),
                name: holding.name.clone(),
                rank: r.map(|r| r.rank),
                reason: Some(reason),
            });
        } else if let Some(r) = r {
            if r.rank > top_n {
                inside.push(row(r));
            } else {
                holds.push(row(r));
            }
        }
    }
    let mut values: Vec<&ScreenRank> = order
        .iter()
        .filter_map(|id| by_id.get(id).copied())
        .collect();
    values.sort_by_key(|r| r.rank);
    let entries: Vec<RebalanceRow> = values
        .into_iter()
        .filter(|r| r.rank <= top_n && !held_ids.contains(&r.instrument_id))
        .map(row)
        .collect();
    let targets = target_weights(&entries, &holds, &inside, &by_id);
    exits.sort_by_key(exit_key);
    inside.sort_by_key(|r| r.rank.unwrap_or(0));
    holds.sort_by_key(|r| r.rank.unwrap_or(0));
    Ok(RebalancePlan {
        top_n,
        hold_buffer,
        exits,
        inside_wrh: inside,
        entries,
        holds,
        target_weights: targets,
    })
}

fn exit_key(r: &RebalanceRow) -> (i64, i64, String) {
    match r.rank {
        None => (1, 0, r.symbol.clone()),
        Some(rank) => (0, rank, r.symbol.clone()),
    }
}

fn target_weights(
    entries: &[RebalanceRow],
    holds: &[RebalanceRow],
    inside: &[RebalanceRow],
    by_id: &BTreeMap<i64, &ScreenRank>,
) -> Vec<TargetWeight> {
    let members: Vec<&RebalanceRow> = entries.iter().chain(holds).chain(inside).collect();
    if members.is_empty() {
        return Vec::new();
    }
    let rank_of = |id: i64| by_id.get(&id).map_or(0, |r| r.rank);
    let mut share = (Decimal::ONE / Decimal::from(members.len()))
        .round_dp_with_strategy(WEIGHT_STEP_DP, RoundingStrategy::ToZero);
    share.rescale(WEIGHT_STEP_DP);
    let mut ordered = members;
    ordered.sort_by(|a, b| {
        (rank_of(a.instrument_id), &a.symbol).cmp(&(rank_of(b.instrument_id), &b.symbol))
    });
    let entering: BTreeSet<i64> = entries.iter().map(|r| r.instrument_id).collect();
    let mut allocated = Decimal::ZERO;
    let last = ordered.len() - 1;
    let mut out = Vec::with_capacity(ordered.len());
    for (position, r) in ordered.into_iter().enumerate() {
        let weight = if position == last {
            Decimal::ONE - allocated
        } else {
            share
        };
        allocated += weight;
        out.push(TargetWeight {
            instrument_id: r.instrument_id,
            symbol: r.symbol.clone(),
            name: r.name.clone(),
            rank: rank_of(r.instrument_id),
            weight,
            action: if entering.contains(&r.instrument_id) {
                Action::Enter
            } else {
                Action::Hold
            },
        });
    }
    out
}
