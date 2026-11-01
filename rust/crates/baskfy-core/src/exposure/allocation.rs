//! Port of `baskfy_core.exposure.allocation`: the sleeve allocation solver and sell priority.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use super::regime::{NewBuyMode, RegimeTier};
use super::{
    ExposureError, ExposureResult, StrMap, fr, py_max, py_min, py_round, py_str_repr, py_sum,
};

/// `PRIORITY_GUARD_EXIT`.
pub const PRIORITY_GUARD_EXIT: i64 = 0;
/// `PRIORITY_UNRANKED`.
pub const PRIORITY_UNRANKED: i64 = 1;
/// `PRIORITY_LOSER`.
pub const PRIORITY_LOSER: i64 = 2;
/// `PRIORITY_PROFIT_CLEAR`.
pub const PRIORITY_PROFIT_CLEAR: i64 = 3;
/// `PRIORITY_PROFIT_NEAR_LTCG`.
pub const PRIORITY_PROFIT_NEAR_LTCG: i64 = 4;

fn other() -> String {
    "other".to_owned()
}

/// `Candidate`: a line of the full-risk target book.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Candidate {
    /// Symbol.
    pub symbol: String,
    /// Momentum rank.
    pub rank: i64,
    /// Score.
    pub score: f64,
    /// Reference price.
    pub price: f64,
    /// Sector cluster.
    #[serde(default = "other")]
    pub cluster: String,
    /// Median daily traded value.
    #[serde(default)]
    pub liquidity: f64,
}

impl Candidate {
    /// `Candidate(symbol, rank, score, price)` with the defaults.
    #[must_use]
    pub fn new(symbol: &str, rank: i64, score: f64, price: f64) -> Self {
        Self {
            symbol: symbol.to_owned(),
            rank,
            score,
            price,
            cluster: other(),
            liquidity: 0.0,
        }
    }
}

/// `Position`: a held position.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Position {
    /// Symbol.
    pub symbol: String,
    /// Quantity.
    pub quantity: i64,
    /// Price.
    pub price: f64,
    /// Average cost.
    #[serde(default)]
    pub average_price: f64,
    /// Momentum rank, if ranked.
    #[serde(default)]
    pub rank: Option<i64>,
    /// Sector cluster.
    #[serde(default = "other")]
    pub cluster: String,
    /// An existing hard-guard exit.
    #[serde(default)]
    pub guard_exit: bool,
    /// Median daily traded value.
    #[serde(default)]
    pub liquidity: f64,
}

impl Position {
    /// `Position(symbol, quantity, price, average_price, rank=...)` with the defaults.
    #[must_use]
    pub fn new(
        symbol: &str,
        quantity: i64,
        price: f64,
        average_price: f64,
        rank: Option<i64>,
    ) -> Self {
        Self {
            symbol: symbol.to_owned(),
            quantity,
            price,
            average_price,
            rank,
            cluster: other(),
            guard_exit: false,
            liquidity: 0.0,
        }
    }

    /// `value`.
    #[must_use]
    pub fn value(&self) -> f64 {
        i64_f64(self.quantity) * self.price
    }

    /// `unrealised_pct`.
    #[must_use]
    pub fn unrealised_pct(&self) -> f64 {
        if self.average_price <= 0.0 {
            return 0.0;
        }
        (self.price / self.average_price - 1.0) * 100.0
    }

    /// `is_loser`.
    #[must_use]
    pub fn is_loser(&self) -> bool {
        self.average_price > 0.0 && self.price < self.average_price
    }
}

/// `Allocation`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Allocation {
    /// Percent of the equity sleeve.
    pub weights_sleeve: StrMap<f64>,
    /// Percent of total NAV.
    pub weights_nav: StrMap<f64>,
    /// Half-sized symbols.
    pub half_sized: BTreeSet<String>,
    /// Sleeve, percent of NAV.
    pub sleeve_pct: f64,
    /// Cluster weights on the sleeve.
    #[serde(default)]
    pub cluster_weights: StrMap<f64>,
    /// Solver notes.
    #[serde(default)]
    pub notes: Vec<String>,
}

impl Allocation {
    /// `Allocation({}, {}, frozenset(), sleeve_pct)`.
    #[must_use]
    pub fn empty(sleeve_pct: f64) -> Self {
        Self {
            weights_sleeve: StrMap::new(),
            weights_nav: StrMap::new(),
            half_sized: BTreeSet::new(),
            sleeve_pct,
            cluster_weights: StrMap::new(),
            notes: Vec::new(),
        }
    }

    /// `n_positions`.
    #[must_use]
    pub fn n_positions(&self) -> usize {
        self.weights_sleeve.len()
    }
}

str_enum!(
    /// `OrderLine.action`.
    OrderAction { Buy => "BUY", Add => "ADD", Trim => "TRIM", Exit => "EXIT", Hold => "HOLD" }
);

/// `OrderLine`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderLine {
    /// Symbol.
    pub symbol: String,
    /// BUY / ADD / TRIM / EXIT / HOLD.
    pub action: OrderAction,
    /// Quantity held now.
    pub qty_now: i64,
    /// Final minus now.
    pub delta: i64,
    /// Target quantity.
    pub qty_final: i64,
    /// Reference price.
    pub ref_price: f64,
    /// Percent of NAV.
    pub weight_nav: f64,
    /// Percent of the sleeve.
    pub weight_sleeve: f64,
    /// Target value.
    pub value: f64,
    /// Rank, if a candidate.
    #[serde(default)]
    pub rank: Option<i64>,
    /// Half-sized entry.
    #[serde(default)]
    pub half_sized: bool,
    /// Sale needs tax review.
    #[serde(default)]
    pub tax_review: bool,
    /// Note.
    #[serde(default)]
    pub note: String,
}

/// `RegimePlan`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RegimePlan {
    /// Policy tier.
    pub tier: RegimeTier,
    /// Sleeve, percent of NAV.
    pub sleeve_pct: f64,
    /// Solved allocation.
    pub allocation: Allocation,
    /// Netted orders.
    pub orders: Vec<OrderLine>,
    /// Projected equity, percent.
    pub projected_equity_pct: f64,
    /// Projected minus cap.
    pub exposure_gap_after_plan_pct: f64,
    /// Manual action required.
    pub manual_action_required: bool,
    /// Executable.
    pub executable: bool,
    /// Reason codes.
    #[serde(default)]
    pub reason_codes: Vec<String>,
    /// Tax-flagged symbols.
    #[serde(default)]
    pub tax_flagged: Vec<String>,
    /// Why the allocation was infeasible.
    #[serde(default)]
    pub infeasible_reason: Option<String>,
}

impl RegimePlan {
    /// `sells`.
    #[must_use]
    pub fn sells(&self) -> Vec<&OrderLine> {
        self.orders.iter().filter(|o| o.delta < 0).collect()
    }

    /// `buys`.
    #[must_use]
    pub fn buys(&self) -> Vec<&OrderLine> {
        self.orders.iter().filter(|o| o.delta > 0).collect()
    }

    /// `round_trips`: symbols both bought and sold, sorted.
    #[must_use]
    pub fn round_trips(&self) -> Vec<String> {
        let b: BTreeSet<&str> = self.buys().iter().map(|o| o.symbol.as_str()).collect();
        let s: BTreeSet<&str> = self.sells().iter().map(|o| o.symbol.as_str()).collect();
        b.intersection(&s).map(|x| (*x).to_owned()).collect()
    }
}

fn i64_f64(n: i64) -> f64 {
    n as f64
}

fn usize_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn f64_i64(x: f64) -> i64 {
    x as i64
}

fn alloc_err(msg: String) -> ExposureError {
    ExposureError::Allocation(msg)
}

/// `feasible_position_range`: counts for which floors and caps both hold on a full sleeve.
pub fn feasible_position_range(min_sleeve_w: f64, max_sleeve_w: f64) -> ExposureResult<(i64, i64)> {
    if min_sleeve_w <= 0.0 || max_sleeve_w <= 0.0 {
        return Err(alloc_err("sleeve weights must be positive".into()));
    }
    let lo = f64_i64((100.0 / max_sleeve_w - 1e-9).ceil());
    let hi = f64_i64((100.0 / min_sleeve_w + 1e-9).floor());
    Ok((lo.max(1), hi.max(1)))
}

/// `validate_allocation_request`.
pub fn validate_allocation_request(
    n: i64,
    min_sleeve_w: f64,
    max_sleeve_w: f64,
    n_half: i64,
) -> ExposureResult<()> {
    if n <= 0 {
        return Err(alloc_err("no eligible constituents to allocate to".into()));
    }
    if min_sleeve_w > max_sleeve_w {
        return Err(alloc_err(format!(
            "MIN_POSITION_WEIGHT ({}) exceeds MAX_SINGLE_WEIGHT ({}) — the sleeve constraints contradict each other",
            fr(min_sleeve_w),
            fr(max_sleeve_w)
        )));
    }
    let (lo, hi) = feasible_position_range(min_sleeve_w, max_sleeve_w)?;
    let full = n - n_half;
    if full > hi {
        return Err(alloc_err(format!(
            "{full} full-sized positions at a {}% sleeve floor need {:.0}% of the sleeve; only 100% exists. Feasible full-size count is {lo}-{hi}.",
            fr(min_sleeve_w),
            i64_f64(full) * min_sleeve_w
        )));
    }
    if n < lo {
        return Err(alloc_err(format!(
            "{n} positions cannot absorb the sleeve without breaching the {}% cap (need at least {lo}).",
            fr(max_sleeve_w)
        )));
    }
    Ok(())
}

fn waterfill(
    raw: &StrMap<f64>,
    budget: f64,
    floors: &StrMap<f64>,
    caps: &StrMap<f64>,
) -> ExposureResult<StrMap<f64>> {
    let floor_of = |s: &str| floors.get(s).copied().unwrap_or(0.0);
    let cap_of = |s: &str| caps.get(s).copied().unwrap_or(100.0);
    let names: Vec<&str> = raw.keys().collect();
    let floor_total = py_sum(names.iter().map(|s| floor_of(s)));
    if floor_total > budget + 1e-9 {
        return Err(alloc_err(format!(
            "position floors total {floor_total:.1}% of a {budget:.1}% budget — infeasible"
        )));
    }

    let mut fixed: StrMap<f64> = StrMap::new();
    let mut free: Vec<&str> = names.clone();
    let mut w: StrMap<f64> = StrMap::new();
    for _ in 0..64 {
        let remaining = budget - py_sum(fixed.values().copied());
        let pos = |s: &str| py_max(raw.get(s).copied().unwrap_or(0.0), 0.0);
        let pool = py_sum(free.iter().map(|s| pos(s)));
        w = if pool <= 0.0 {
            let share = if free.is_empty() {
                0.0
            } else {
                remaining / usize_f64(free.len())
            };
            free.iter().map(|s| (*s, share)).collect()
        } else {
            free.iter()
                .map(|s| (*s, pos(s) / pool * remaining))
                .collect()
        };

        let wv = |s: &str| w.get(s).copied().unwrap_or(0.0);
        let over: Vec<&str> = free
            .iter()
            .copied()
            .filter(|s| wv(s) > cap_of(s) + 1e-9)
            .collect();
        if !over.is_empty() {
            for s in &over {
                fixed.insert(*s, cap_of(s));
            }
            free.retain(|s| !over.contains(s));
            continue;
        }
        let under: Vec<&str> = free
            .iter()
            .copied()
            .filter(|s| wv(s) < floor_of(s) - 1e-9)
            .collect();
        if !under.is_empty() {
            for s in &under {
                fixed.insert(*s, floor_of(s));
            }
            free.retain(|s| !under.contains(s));
            continue;
        }
        let mut out = fixed;
        for (k, v) in w.iter() {
            out.insert(k, *v);
        }
        return Ok(out);
    }
    let mut out = fixed;
    for s in free {
        out.insert(s, w.get(s).copied().unwrap_or(0.0));
    }
    Ok(out)
}

fn usize_f64(n: usize) -> f64 {
    n as f64
}

/// `solve_allocation`: score-proportional sleeve allocation under floors, caps and the cluster cap.
pub fn solve_allocation(
    candidates: &[Candidate],
    sleeve_pct: f64,
    min_sleeve_w: f64,
    max_sleeve_w: f64,
    cluster_cap: f64,
    half_sized: &BTreeSet<String>,
    half_factor: f64,
) -> ExposureResult<Allocation> {
    if candidates.is_empty() {
        return Err(alloc_err("no eligible constituents to allocate to".into()));
    }
    let half = half_sized;
    validate_allocation_request(
        usize_i64(candidates.len()),
        min_sleeve_w,
        max_sleeve_w,
        usize_i64(half.len()),
    )?;

    let raw: StrMap<f64> = candidates
        .iter()
        .map(|c| (c.symbol.clone(), py_max(c.score, 1e-9)))
        .collect();
    let floors: StrMap<f64> = candidates
        .iter()
        .map(|c| {
            let f = if half.contains(&c.symbol) {
                0.0
            } else {
                min_sleeve_w
            };
            (c.symbol.clone(), f)
        })
        .collect();
    let mut caps: StrMap<f64> = candidates
        .iter()
        .map(|c| {
            let cap = if half.contains(&c.symbol) {
                max_sleeve_w * half_factor
            } else {
                max_sleeve_w
            };
            (c.symbol.clone(), cap)
        })
        .collect();

    let mut w = waterfill(&raw, 100.0, &floors, &caps)?;

    let clusters: StrMap<String> = candidates
        .iter()
        .map(|c| {
            let k = if c.cluster.is_empty() {
                other()
            } else {
                c.cluster.clone()
            };
            (c.symbol.clone(), k)
        })
        .collect();
    let cluster_of = |s: &str| clusters.get(s).cloned().unwrap_or_else(other);
    let floor_of = |s: &str| floors.get(s).copied().unwrap_or(0.0);
    let mut notes: Vec<String> = Vec::new();
    for _ in 0..8 {
        let mut agg: StrMap<f64> = StrMap::new();
        for (s, v) in w.iter() {
            let k = cluster_of(s);
            let prev = agg.get(&k).copied().unwrap_or(0.0);
            agg.insert(k, prev + v);
        }
        let breach: Vec<String> = agg
            .iter()
            .filter(|(k, v)| *k != "other" && **v > cluster_cap + 1e-9)
            .map(|(k, _)| k.to_owned())
            .collect();
        if breach.is_empty() {
            break;
        }
        for k in &breach {
            let members: Vec<String> = w
                .keys()
                .filter(|s| cluster_of(s) == *k)
                .map(str::to_owned)
                .collect();
            let floor_sum = py_sum(members.iter().map(|s| floor_of(s)));
            if floor_sum > cluster_cap + 1e-9 {
                let min_floor = members
                    .iter()
                    .map(|s| floor_of(s))
                    .reduce(py_min)
                    .unwrap_or(0.0);
                return Err(alloc_err(format!(
                    "cluster {} holds {} names needing {floor_sum:.1}% of the sleeve at the {min_floor:.1}% floor, but the cluster cap is {cluster_cap:.1}%. Reduce the position count, lower the floor, or raise the cluster cap.",
                    py_str_repr(k),
                    members.len()
                )));
            }
            let scale = cluster_cap / agg.get(k).copied().unwrap_or(0.0);
            for s in &members {
                let ws = w.get(s).copied().unwrap_or(0.0);
                let cs = caps.get(s).copied().unwrap_or(100.0);
                caps.insert(s.clone(), py_max(floor_of(s), py_min(cs, ws * scale)));
            }
        }
        let mut sorted_breach = breach.clone();
        sorted_breach.sort();
        notes.push(format!("cluster cap applied: {}", sorted_breach.join(", ")));
        w = waterfill(&raw, 100.0, &floors, &caps)?;
    }

    for s in half {
        if let Some(v) = w.get_mut(s) {
            *v *= half_factor;
        }
    }

    let mut breached: Vec<&str> = w
        .iter()
        .filter(|(s, v)| !half.contains(*s) && **v < floor_of(s) - 1e-6)
        .map(|(s, _)| s)
        .collect();
    if !breached.is_empty() {
        breached.sort_unstable();
        return Err(alloc_err(format!(
            "constraints could not be satisfied without distorting weights: {} fell below the {}% sleeve floor",
            breached.join(", "),
            fr(min_sleeve_w)
        )));
    }

    let total = py_sum(w.values().copied());
    let nav: StrMap<f64> = w
        .iter()
        .map(|(s, v)| (s, py_round(v * sleeve_pct / 100.0, 6)))
        .collect();
    let mut agg: StrMap<f64> = StrMap::new();
    for (s, v) in w.iter() {
        let k = cluster_of(s);
        let prev = agg.get(&k).copied().unwrap_or(0.0);
        agg.insert(k, py_round(prev + v, 6));
    }
    if total < 99.0 {
        notes.push(format!("sleeve utilisation {total:.1}%"));
    }
    Ok(Allocation {
        weights_sleeve: w.iter().map(|(s, v)| (s, py_round(*v, 6))).collect(),
        weights_nav: nav,
        half_sized: half.clone(),
        sleeve_pct,
        cluster_weights: agg,
        notes,
    })
}

/// `sale_priority_key`: sell first what sorts smallest.
#[must_use]
pub fn sale_priority_key(
    p: &Position,
    worst_rank: i64,
    nearby_band: i64,
    tax_flagged: bool,
) -> (i64, i64, i64, i64) {
    if p.guard_exit {
        return (0, 0, PRIORITY_GUARD_EXIT, 0);
    }
    let rank = p.rank.unwrap_or(worst_rank + 1);
    let band = if nearby_band > 0 {
        -rank.div_euclid(nearby_band)
    } else {
        -rank
    };
    let bucket = if p.rank.is_none() {
        PRIORITY_UNRANKED
    } else if tax_flagged {
        PRIORITY_PROFIT_NEAR_LTCG
    } else if p.is_loser() {
        PRIORITY_LOSER
    } else {
        PRIORITY_PROFIT_CLEAR
    };
    (1, band, bucket, -rank)
}

/// `order_sales`: positions in the order the regime should sell them.
#[must_use]
pub fn order_sales(
    positions: &[Position],
    nearby_band: i64,
    tax_flagged: &BTreeSet<String>,
) -> Vec<Position> {
    let worst = positions
        .iter()
        .map(|p| p.rank.unwrap_or(0))
        .max()
        .unwrap_or(0);
    let mut out = positions.to_vec();
    out.sort_by_key(|p| sale_priority_key(p, worst, nearby_band, tax_flagged.contains(&p.symbol)));
    out
}

/// The keyword arguments of `select_survivors` after `candidates` and `positions`.
#[derive(Debug, Clone, PartialEq)]
pub struct SurvivorParams {
    /// Position ceiling.
    pub max_positions: i64,
    /// New-buy mode.
    pub new_buys: NewBuyMode,
    /// R4 residual name cap, when a residual policy applies.
    pub residual_max_names: Option<i64>,
    /// Residual ordering keys, most significant first.
    pub residual_ordering: Vec<String>,
    /// Nearby-rank band.
    pub nearby_band: i64,
    /// Tax-flagged symbols.
    pub tax_flagged: BTreeSet<String>,
}

impl SurvivorParams {
    /// The Python defaults for everything but `max_positions` and `new_buys`.
    #[must_use]
    pub fn new(max_positions: i64, new_buys: NewBuyMode) -> Self {
        Self {
            max_positions,
            new_buys,
            residual_max_names: None,
            residual_ordering: vec!["rank".into(), "liquidity".into()],
            nearby_band: 3,
            tax_flagged: BTreeSet::new(),
        }
    }
}

fn py_head<T>(mut v: Vec<T>, n: i64) -> Vec<T> {
    let len = usize_i64(v.len());
    let keep = if n >= 0 { n.min(len) } else { (len + n).max(0) };
    v.truncate(usize::try_from(keep).unwrap_or(0));
    v
}

/// `select_survivors`: the final constituent set and the dropped held symbols, in sale order.
#[must_use]
pub fn select_survivors(
    candidates: &[Candidate],
    positions: &[Position],
    p: &SurvivorParams,
) -> (Vec<Candidate>, Vec<String>) {
    let held: BTreeSet<&str> = positions.iter().map(|x| x.symbol.as_str()).collect();

    let survivors = if let Some(residual) = p.residual_max_names {
        let mut pool: Vec<Candidate> = candidates
            .iter()
            .filter(|c| held.contains(c.symbol.as_str()))
            .cloned()
            .collect();
        if pool.is_empty() {
            pool = candidates.to_vec();
        }
        for key in p.residual_ordering.iter().rev() {
            match key.as_str() {
                "rank" => pool.sort_by_key(|c| c.rank),
                "liquidity" => pool.sort_by(|a, b| (-a.liquidity).total_cmp(&(-b.liquidity))),
                _ => {}
            }
        }
        py_head(pool, residual.max(0))
    } else {
        let mut eligible: Vec<Candidate> = candidates
            .iter()
            .filter(|c| held.contains(c.symbol.as_str()) || p.new_buys != NewBuyMode::Blocked)
            .cloned()
            .collect();
        eligible.sort_by_key(|c| c.rank);
        py_head(eligible, p.max_positions)
    };

    let keep: BTreeSet<&str> = survivors.iter().map(|c| c.symbol.as_str()).collect();
    let dropped = order_sales(positions, p.nearby_band, &p.tax_flagged)
        .into_iter()
        .filter(|x| !keep.contains(x.symbol.as_str()))
        .map(|x| x.symbol)
        .collect();
    (survivors, dropped)
}

fn py_round_int(x: f64) -> i64 {
    f64_i64(x.round_ties_even())
}

/// `build_orders`: one netted pass over held and target names.
#[must_use]
pub fn build_orders(
    allocation: &Allocation,
    positions: &[Position],
    candidates: &[Candidate],
    capital: f64,
    tax_flagged: &BTreeSet<String>,
    guard_exits: &BTreeSet<String>,
) -> Vec<OrderLine> {
    let held: BTreeMap<&str, &Position> =
        positions.iter().map(|p| (p.symbol.as_str(), p)).collect();
    let prices: BTreeMap<&str, f64> = candidates
        .iter()
        .map(|c| (c.symbol.as_str(), c.price))
        .collect();
    let ranks: BTreeMap<&str, i64> = candidates
        .iter()
        .map(|c| (c.symbol.as_str(), c.rank))
        .collect();

    let mut symbols: BTreeSet<&str> = held.keys().copied().collect();
    symbols.extend(allocation.weights_nav.keys());

    let mut lines = Vec::new();
    for symbol in symbols {
        let pos = held.get(symbol).copied();
        let px = match prices.get(symbol) {
            Some(x) if *x != 0.0 => *x,
            _ => pos.map_or(0.0, |p| p.price),
        };
        if px <= 0.0 || px.is_nan() {
            continue;
        }
        let mut w_nav = allocation.weights_nav.get(symbol).copied().unwrap_or(0.0);
        let guarded = guard_exits.contains(symbol);
        if guarded {
            w_nav = 0.0;
        }
        let target_qty = py_round_int(capital * w_nav / 100.0 / px);
        let now = pos.map_or(0, |p| p.quantity);
        let delta = target_qty - now;
        let action = match delta.cmp(&0) {
            std::cmp::Ordering::Greater if now == 0 => OrderAction::Buy,
            std::cmp::Ordering::Greater => OrderAction::Add,
            std::cmp::Ordering::Less if target_qty == 0 => OrderAction::Exit,
            std::cmp::Ordering::Less => OrderAction::Trim,
            std::cmp::Ordering::Equal => OrderAction::Hold,
        };
        lines.push(OrderLine {
            symbol: symbol.to_owned(),
            action,
            qty_now: now,
            delta,
            qty_final: target_qty,
            ref_price: px,
            weight_nav: py_round(w_nav, 4),
            weight_sleeve: py_round(
                allocation
                    .weights_sleeve
                    .get(symbol)
                    .copied()
                    .unwrap_or(0.0),
                4,
            ),
            value: py_round(i64_f64(target_qty) * px, 2),
            rank: ranks.get(symbol).copied(),
            half_sized: allocation.half_sized.contains(symbol),
            tax_review: tax_flagged.contains(symbol) && delta < 0,
            note: if guarded {
                "guard exit".into()
            } else {
                String::new()
            },
        });
    }
    lines
}

/// `projected_equity_pct`.
#[must_use]
pub fn projected_equity_pct(lines: &[OrderLine], capital: f64) -> f64 {
    if capital <= 0.0 {
        return 0.0;
    }
    let total = py_sum(lines.iter().map(|o| i64_f64(o.qty_final) * o.ref_price));
    py_round(total / capital * 100.0, 6)
}
