//! `baskfy_core.exposure.allocation` (re-exported by the desk as `app/core/regime_alloc.py`):
//! the sleeve-relative allocation solver and the regime's sell prioritisation. **Pure.**
//!
//! `MIN_POSITION_WEIGHT` and `MAX_SINGLE_WEIGHT` are percentages of the **active equity
//! sleeve**, not of NAV (in R3 the sleeve is 40% of NAV, so a 10% sleeve position is 4% of
//! NAV); both figures are returned. The solver produces **one** final target book and the
//! orders are its delta against the holdings, so a name the regime would trim and scoring would
//! re-buy nets to one order instead of a round trip. Infeasibility is an error, never a nudge.
//!
//! One deliberate difference, recorded in DECISIONS-RS (lane analytics_regime, R8.AR3): Python
//! iterates the water-fill's free names as a `set`, whose order is per-process (string hash
//! randomisation); Rust iterates them in candidate order. The solved weights agree to the
//! rounding the solver applies (6 decimals); only the order of equal-weight ties can differ, and
//! it differs between two Python processes too.

use std::collections::BTreeSet;

use super::engine::{NewBuyMode, RegimeTier, feq, py_sum};
use crate::lanes::weekly::pandas::{py_round, py_round_int};
use crate::pyfmt::{PyArg, format_value};

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

/// `AllocationError`: the constraints cannot all hold.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct AllocationError(pub String);

fn f1(x: f64) -> String {
    format_value(&PyArg::Float(x), ".1f").unwrap_or_default()
}

fn f0(x: f64) -> String {
    format_value(&PyArg::Float(x), ".0f").unwrap_or_default()
}

fn fs(x: f64) -> String {
    baskfy_core::py::float_repr(x)
}

/// `Candidate`: a line of the full-risk target book scoring produced, before regime limits.
#[derive(Debug, Clone, PartialEq)]
pub struct Candidate {
    /// `symbol`.
    pub symbol: String,
    /// `rank`.
    pub rank: i64,
    /// `score`.
    pub score: f64,
    /// `price`.
    pub price: f64,
    /// `cluster` (default `"other"`).
    pub cluster: String,
    /// `liquidity`: median daily traded value, for residual ordering.
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
            cluster: "other".to_owned(),
            liquidity: 0.0,
        }
    }
}

/// `Position`: a currently held position.
#[derive(Debug, Clone, PartialEq)]
pub struct Position {
    /// `symbol`.
    pub symbol: String,
    /// `quantity`.
    pub quantity: i64,
    /// `price`.
    pub price: f64,
    /// `average_price`.
    pub average_price: f64,
    /// `rank`.
    pub rank: Option<i64>,
    /// `cluster`.
    pub cluster: String,
    /// `guard_exit`: an existing hard-guard exit; always takes precedence.
    pub guard_exit: bool,
    /// `liquidity`.
    pub liquidity: f64,
}

impl Position {
    /// `Position(symbol, quantity, price, average_price, rank=)` with the defaults.
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
            cluster: "other".to_owned(),
            guard_exit: false,
            liquidity: 0.0,
        }
    }

    /// `value`.
    #[must_use]
    #[allow(clippy::cast_precision_loss)]
    pub fn value(&self) -> f64 {
        self.quantity as f64 * self.price
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
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Allocation {
    /// `weights_sleeve`: % of the equity sleeve, sums to ~100.
    pub weights_sleeve: Vec<(String, f64)>,
    /// `weights_nav`: % of NAV, sums to ~`sleeve_pct`.
    pub weights_nav: Vec<(String, f64)>,
    /// `half_sized`.
    pub half_sized: BTreeSet<String>,
    /// `sleeve_pct`.
    pub sleeve_pct: f64,
    /// `cluster_weights`.
    pub cluster_weights: Vec<(String, f64)>,
    /// `notes`.
    pub notes: Vec<String>,
}

impl Allocation {
    /// `Allocation({}, {}, frozenset(), sleeve)`.
    #[must_use]
    pub fn empty(sleeve_pct: f64) -> Self {
        Self {
            sleeve_pct,
            ..Self::default()
        }
    }

    /// `n_positions`.
    #[must_use]
    pub fn n_positions(&self) -> usize {
        self.weights_sleeve.len()
    }

    /// `weights_sleeve.get(symbol)`.
    #[must_use]
    pub fn sleeve(&self, symbol: &str) -> Option<f64> {
        lookup(&self.weights_sleeve, symbol)
    }

    /// `weights_nav.get(symbol)`.
    #[must_use]
    pub fn nav(&self, symbol: &str) -> Option<f64> {
        lookup(&self.weights_nav, symbol)
    }
}

fn lookup(v: &[(String, f64)], key: &str) -> Option<f64> {
    v.iter().find(|(k, _)| k == key).map(|(_, x)| *x)
}

fn put(v: &mut Vec<(String, f64)>, key: &str, value: f64) {
    if let Some(slot) = v.iter_mut().find(|(k, _)| k == key) {
        slot.1 = value;
    } else {
        v.push((key.to_owned(), value));
    }
}

/// `OrderLine`.
#[derive(Debug, Clone, PartialEq)]
pub struct OrderLine {
    /// `symbol`.
    pub symbol: String,
    /// `action`: BUY / ADD / TRIM / EXIT / HOLD.
    pub action: &'static str,
    /// `qty_now`.
    pub qty_now: i64,
    /// `delta`.
    pub delta: i64,
    /// `qty_final`.
    pub qty_final: i64,
    /// `ref_price`.
    pub ref_price: f64,
    /// `weight_nav`.
    pub weight_nav: f64,
    /// `weight_sleeve`.
    pub weight_sleeve: f64,
    /// `value`.
    pub value: f64,
    /// `rank`.
    pub rank: Option<i64>,
    /// `half_sized`.
    pub half_sized: bool,
    /// `tax_review`.
    pub tax_review: bool,
    /// `note`.
    pub note: String,
}

/// `RegimePlan`.
#[derive(Debug, Clone, PartialEq)]
pub struct RegimePlan {
    /// `tier`.
    pub tier: RegimeTier,
    /// `sleeve_pct`.
    pub sleeve_pct: f64,
    /// `allocation`.
    pub allocation: Allocation,
    /// `orders`.
    pub orders: Vec<OrderLine>,
    /// `projected_equity_pct`.
    pub projected_equity_pct: f64,
    /// `exposure_gap_after_plan_pct`.
    pub exposure_gap_after_plan_pct: f64,
    /// `manual_action_required`.
    pub manual_action_required: bool,
    /// `executable`.
    pub executable: bool,
    /// `reason_codes`.
    pub reason_codes: Vec<&'static str>,
    /// `tax_flagged`.
    pub tax_flagged: Vec<String>,
    /// `infeasible_reason`.
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

    /// `round_trips`: symbols both bought and sold — netting makes this empty.
    #[must_use]
    pub fn round_trips(&self) -> Vec<String> {
        let b: BTreeSet<&str> = self.buys().iter().map(|o| o.symbol.as_str()).collect();
        let s: BTreeSet<&str> = self.sells().iter().map(|o| o.symbol.as_str()).collect();
        b.intersection(&s).map(|x| (*x).to_owned()).collect()
    }
}

// =============================================================================================
// feasibility
// =============================================================================================

/// `feasible_position_range(min_sleeve_w, max_sleeve_w)`.
///
/// # Errors
/// [`AllocationError`] when a weight is not positive.
#[allow(clippy::cast_possible_truncation)]
pub fn feasible_position_range(
    min_sleeve_w: f64,
    max_sleeve_w: f64,
) -> Result<(i64, i64), AllocationError> {
    if min_sleeve_w <= 0.0 || max_sleeve_w <= 0.0 {
        return Err(AllocationError(
            "sleeve weights must be positive".to_owned(),
        ));
    }
    let lo = (100.0 / max_sleeve_w - 1e-9).ceil() as i64;
    let hi = (100.0 / min_sleeve_w + 1e-9).floor() as i64;
    Ok((lo.max(1), hi.max(1)))
}

/// `validate_allocation_request(n, min_sleeve_w=, max_sleeve_w=, n_half=)`.
///
/// # Errors
/// [`AllocationError`] when the request cannot be met without distortion.
#[allow(clippy::cast_precision_loss)]
pub fn validate_allocation_request(
    n: i64,
    min_sleeve_w: f64,
    max_sleeve_w: f64,
    n_half: i64,
) -> Result<(), AllocationError> {
    if n <= 0 {
        return Err(AllocationError(
            "no eligible constituents to allocate to".to_owned(),
        ));
    }
    if min_sleeve_w > max_sleeve_w {
        return Err(AllocationError(format!(
            "MIN_POSITION_WEIGHT ({}) exceeds MAX_SINGLE_WEIGHT ({}) — the sleeve constraints \
             contradict each other",
            fs(min_sleeve_w),
            fs(max_sleeve_w)
        )));
    }
    let (lo, hi) = feasible_position_range(min_sleeve_w, max_sleeve_w)?;
    let full = n - n_half;
    if full > hi {
        return Err(AllocationError(format!(
            "{full} full-sized positions at a {}% sleeve floor need {}% of the sleeve; only 100% \
             exists. Feasible full-size count is {lo}-{hi}.",
            fs(min_sleeve_w),
            f0(full as f64 * min_sleeve_w)
        )));
    }
    if n < lo {
        return Err(AllocationError(format!(
            "{n} positions cannot absorb the sleeve without breaching the {}% cap (need at least \
             {lo}).",
            fs(max_sleeve_w)
        )));
    }
    Ok(())
}

// =============================================================================================
// solver
// =============================================================================================

/// `_waterfill(raw, budget=, floors=, caps=, max_iter=64)`: proportional allocation honouring
/// per-name floors and caps, re-solved until stable.
///
/// # Errors
/// [`AllocationError`] when the floors alone exceed the budget.
#[allow(clippy::cast_precision_loss)]
pub fn waterfill(
    raw: &[(String, f64)],
    budget: f64,
    floors: &[(String, f64)],
    caps: &[(String, f64)],
) -> Result<Vec<(String, f64)>, AllocationError> {
    let floor = |s: &str| lookup(floors, s).unwrap_or(0.0);
    let cap = |s: &str| lookup(caps, s).unwrap_or(100.0);
    let names: Vec<&String> = raw.iter().map(|(k, _)| k).collect();
    let floor_total = py_sum(names.iter().map(|s| floor(s)));
    if floor_total > budget + 1e-9 {
        return Err(AllocationError(format!(
            "position floors total {}% of a {}% budget — infeasible",
            f1(floor_total),
            f1(budget)
        )));
    }
    let raw_of = |s: &str| lookup(raw, s).unwrap_or(0.0);
    let mut fixed: Vec<(String, f64)> = Vec::new();
    let mut free: Vec<String> = Vec::new();
    for n in names {
        if !free.contains(n) {
            free.push(n.clone());
        }
    }
    let mut w: Vec<(String, f64)> = Vec::new();
    for _ in 0..64 {
        let remaining = budget - py_sum(fixed.iter().map(|(_, v)| *v));
        let pool = py_sum(free.iter().map(|s| raw_of(s).max(0.0)));
        w = if pool <= 0.0 {
            let share = if free.is_empty() {
                0.0
            } else {
                remaining / free.len() as f64
            };
            free.iter().map(|s| (s.clone(), share)).collect()
        } else {
            free.iter()
                .map(|s| (s.clone(), raw_of(s).max(0.0) / pool * remaining))
                .collect()
        };
        let over: Vec<String> = w
            .iter()
            .filter(|(s, v)| *v > cap(s) + 1e-9)
            .map(|(s, _)| s.clone())
            .collect();
        if !over.is_empty() {
            for s in over {
                put(&mut fixed, &s, cap(&s));
                free.retain(|x| *x != s);
            }
            continue;
        }
        let under: Vec<String> = w
            .iter()
            .filter(|(s, v)| *v < floor(s) - 1e-9)
            .map(|(s, _)| s.clone())
            .collect();
        if !under.is_empty() {
            for s in under {
                put(&mut fixed, &s, floor(&s));
                free.retain(|x| *x != s);
            }
            continue;
        }
        let mut out = fixed;
        for (s, v) in w {
            put(&mut out, &s, v);
        }
        return Ok(out);
    }
    let mut out = fixed;
    for s in &free {
        put(&mut out, s, lookup(&w, s).unwrap_or(0.0));
    }
    Ok(out)
}

/// `solve_allocation(candidates, sleeve_pct=, min_sleeve_w=, max_sleeve_w=, cluster_cap=25.0,
/// half_sized=(), half_factor=0.5)`: score-proportional sleeve allocation under floors, caps and
/// the cluster cap (applied to the sleeve).
///
/// # Errors
/// [`AllocationError`] for an infeasible request, never a distorted book.
#[allow(clippy::too_many_lines)]
pub fn solve_allocation(
    candidates: &[Candidate],
    sleeve_pct: f64,
    min_sleeve_w: f64,
    max_sleeve_w: f64,
    cluster_cap: f64,
    half_sized: &BTreeSet<String>,
    half_factor: f64,
) -> Result<Allocation, AllocationError> {
    if candidates.is_empty() {
        return Err(AllocationError(
            "no eligible constituents to allocate to".to_owned(),
        ));
    }
    let n = i64::try_from(candidates.len()).unwrap_or(i64::MAX);
    let n_half = i64::try_from(half_sized.len()).unwrap_or(i64::MAX);
    validate_allocation_request(n, min_sleeve_w, max_sleeve_w, n_half)?;

    let mut raw: Vec<(String, f64)> = Vec::new();
    let mut floors: Vec<(String, f64)> = Vec::new();
    let mut caps: Vec<(String, f64)> = Vec::new();
    let mut clusters: Vec<(String, String)> = Vec::new();
    for c in candidates {
        let half = half_sized.contains(&c.symbol);
        put(&mut raw, &c.symbol, c.score.max(1e-9));
        put(&mut floors, &c.symbol, if half { 0.0 } else { min_sleeve_w });
        put(
            &mut caps,
            &c.symbol,
            if half {
                max_sleeve_w * half_factor
            } else {
                max_sleeve_w
            },
        );
        let cl = if c.cluster.is_empty() {
            "other".to_owned()
        } else {
            c.cluster.clone()
        };
        if let Some(slot) = clusters.iter_mut().find(|(k, _)| *k == c.symbol) {
            slot.1 = cl;
        } else {
            clusters.push((c.symbol.clone(), cl));
        }
    }
    let cluster_of = |s: &str| -> String {
        clusters
            .iter()
            .find(|(k, _)| k == s)
            .map(|(_, v)| v.clone())
            .unwrap_or_default()
    };
    let floor_of = |floors: &[(String, f64)], s: &str| lookup(floors, s).unwrap_or(0.0);

    let mut w = waterfill(&raw, 100.0, &floors, &caps)?;
    let mut notes: Vec<String> = Vec::new();
    for _ in 0..8 {
        let mut agg: Vec<(String, f64)> = Vec::new();
        for (s, v) in &w {
            let k = cluster_of(s);
            let cur = lookup(&agg, &k).unwrap_or(0.0);
            put(&mut agg, &k, cur + v);
        }
        let breach: Vec<String> = agg
            .iter()
            .filter(|(k, v)| k != "other" && *v > cluster_cap + 1e-9)
            .map(|(k, _)| k.clone())
            .collect();
        if breach.is_empty() {
            break;
        }
        for k in &breach {
            let members: Vec<String> = w
                .iter()
                .filter(|(s, _)| cluster_of(s) == *k)
                .map(|(s, _)| s.clone())
                .collect();
            let floor_sum = py_sum(members.iter().map(|s| floor_of(&floors, s)));
            if floor_sum > cluster_cap + 1e-9 {
                let min_floor = members
                    .iter()
                    .map(|s| floor_of(&floors, s))
                    .fold(f64::INFINITY, f64::min);
                return Err(AllocationError(format!(
                    "cluster {} holds {} names needing {}% of the sleeve at the {}% floor, but \
                     the cluster cap is {}%. Reduce the position count, lower the floor, or \
                     raise the cluster cap.",
                    baskfy_providers::py::repr_str(k),
                    members.len(),
                    f1(floor_sum),
                    f1(min_floor),
                    f1(cluster_cap)
                )));
            }
            let scale = cluster_cap / lookup(&agg, k).unwrap_or(1.0);
            for s in &members {
                let current_cap = lookup(&caps, s).unwrap_or(100.0);
                let ws = lookup(&w, s).unwrap_or(0.0);
                put(
                    &mut caps,
                    s,
                    floor_of(&floors, s).max(current_cap.min(ws * scale)),
                );
            }
        }
        let mut sorted = breach.clone();
        sorted.sort();
        notes.push(format!("cluster cap applied: {}", sorted.join(", ")));
        w = waterfill(&raw, 100.0, &floors, &caps)?;
    }

    for (s, v) in &mut w {
        if half_sized.contains(s) {
            *v *= half_factor;
        }
    }
    let mut breached: Vec<String> = w
        .iter()
        .filter(|(s, v)| !half_sized.contains(s) && *v < floor_of(&floors, s) - 1e-6)
        .map(|(s, _)| s.clone())
        .collect();
    if !breached.is_empty() {
        breached.sort();
        return Err(AllocationError(format!(
            "constraints could not be satisfied without distorting weights: {} fell below the \
             {}% sleeve floor",
            breached.join(", "),
            fs(min_sleeve_w)
        )));
    }
    let total = py_sum(w.iter().map(|(_, v)| *v));
    let nav: Vec<(String, f64)> = w
        .iter()
        .map(|(s, v)| (s.clone(), py_round(v * sleeve_pct / 100.0, 6)))
        .collect();
    let mut agg: Vec<(String, f64)> = Vec::new();
    for (s, v) in &w {
        let k = cluster_of(s);
        let cur = lookup(&agg, &k).unwrap_or(0.0);
        put(&mut agg, &k, py_round(cur + v, 6));
    }
    if total < 99.0 {
        notes.push(format!("sleeve utilisation {}%", f1(total)));
    }
    Ok(Allocation {
        weights_sleeve: w.iter().map(|(s, v)| (s.clone(), py_round(*v, 6))).collect(),
        weights_nav: nav,
        half_sized: half_sized.clone(),
        sleeve_pct,
        cluster_weights: agg,
        notes,
    })
}

// =============================================================================================
// sell prioritisation
// =============================================================================================

/// `sale_priority_key(p, worst_rank=, nearby_band=, tax_flagged=)`: sell first what sorts
/// smallest — guard exits ahead of every band, then the lowest rank, losers before winners
/// within a nearby band, profitable positions near the LTCG boundary last.
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

/// `order_sales(positions, nearby_band=3, tax_flagged=())`: positions in selling order.
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
    out.sort_by_key(|p| {
        sale_priority_key(p, worst, nearby_band, tax_flagged.contains(&p.symbol))
    });
    out
}

/// Python's `seq[:n]` (a negative `n` counts from the end).
fn head<T: Clone>(v: &[T], n: i64) -> Vec<T> {
    let len = i64::try_from(v.len()).unwrap_or(i64::MAX);
    let end = if n < 0 { (len + n).max(0) } else { n.min(len) };
    v[..usize::try_from(end).unwrap_or(0)].to_vec()
}

/// `select_survivors(...)`: the final constituent set and the held names dropped (in selling
/// order). New names are admitted only when the tier permits buying.
#[must_use]
#[allow(clippy::too_many_arguments)]
pub fn select_survivors(
    candidates: &[Candidate],
    positions: &[Position],
    max_positions: i64,
    new_buys: NewBuyMode,
    residual_max_names: Option<i64>,
    residual_ordering: &[String],
    nearby_band: i64,
    tax_flagged: &BTreeSet<String>,
) -> (Vec<Candidate>, Vec<String>) {
    let held: BTreeSet<&str> = positions.iter().map(|p| p.symbol.as_str()).collect();
    let survivors = if let Some(residual) = residual_max_names {
        let mut pool: Vec<Candidate> = candidates
            .iter()
            .filter(|c| held.contains(c.symbol.as_str()))
            .cloned()
            .collect();
        if pool.is_empty() {
            pool = candidates.to_vec();
        }
        for key in residual_ordering.iter().rev() {
            match key.as_str() {
                "rank" => pool.sort_by_key(|c| c.rank),
                "liquidity" => pool.sort_by(|a, b| {
                    (-a.liquidity)
                        .partial_cmp(&-b.liquidity)
                        .unwrap_or(std::cmp::Ordering::Equal)
                }),
                _ => {}
            }
        }
        head(&pool, residual.max(0))
    } else {
        let mut eligible: Vec<Candidate> = candidates
            .iter()
            .filter(|c| held.contains(c.symbol.as_str()) || new_buys != NewBuyMode::Blocked)
            .cloned()
            .collect();
        eligible.sort_by_key(|c| c.rank);
        head(&eligible, max_positions)
    };
    let keep: BTreeSet<&str> = survivors.iter().map(|c| c.symbol.as_str()).collect();
    let dropped = order_sales(positions, nearby_band, tax_flagged)
        .into_iter()
        .filter(|p| !keep.contains(p.symbol.as_str()))
        .map(|p| p.symbol)
        .collect();
    (survivors, dropped)
}

// =============================================================================================
// netting
// =============================================================================================

/// `build_orders(allocation, positions, candidates, capital=, tax_flagged=, guard_exits=)`: one
/// pass over the union of held and target names — netting is structural. A hard guard exit
/// always wins: the regime never re-establishes a position a guard said to leave.
///
/// # Errors
/// A target quantity Python's `int(round(...))` cannot make (a NaN or infinite size).
#[allow(clippy::cast_precision_loss)]
pub fn build_orders(
    allocation: &Allocation,
    positions: &[Position],
    candidates: &[Candidate],
    capital: f64,
    tax_flagged: &BTreeSet<String>,
    guard_exits: &BTreeSet<String>,
) -> Result<Vec<OrderLine>, AllocationError> {
    let held = |s: &str| positions.iter().rev().find(|p| p.symbol == s);
    let price_of = |s: &str| candidates.iter().rev().find(|c| c.symbol == s).map(|c| c.price);
    let rank_of = |s: &str| candidates.iter().rev().find(|c| c.symbol == s).map(|c| c.rank);
    let mut names: BTreeSet<String> = positions.iter().map(|p| p.symbol.clone()).collect();
    names.extend(allocation.weights_nav.iter().map(|(s, _)| s.clone()));
    let mut lines = Vec::new();
    for symbol in names {
        let pos = held(&symbol);
        // `prices.get(symbol) or (pos.price if pos else 0.0)`: a zero price is falsy.
        let px = match price_of(&symbol) {
            Some(p) if !feq(p, 0.0) => p,
            _ => pos.map_or(0.0, |p| p.price),
        };
        if px <= 0.0 {
            continue;
        }
        let mut w_nav = allocation.nav(&symbol).unwrap_or(0.0);
        let guarded = guard_exits.contains(&symbol);
        if guarded {
            w_nav = 0.0;
        }
        let target_qty = py_round_int(capital * w_nav / 100.0 / px).ok_or_else(|| {
            AllocationError("ValueError: cannot convert float NaN to integer".to_owned())
        })?;
        let now = pos.map_or(0, |p| p.quantity);
        let delta = target_qty - now;
        let action = if delta > 0 {
            if now == 0 { "BUY" } else { "ADD" }
        } else if delta < 0 {
            if target_qty == 0 { "EXIT" } else { "TRIM" }
        } else {
            "HOLD"
        };
        lines.push(OrderLine {
            action,
            qty_now: now,
            delta,
            qty_final: target_qty,
            ref_price: px,
            weight_nav: py_round(w_nav, 4),
            weight_sleeve: py_round(allocation.sleeve(&symbol).unwrap_or(0.0), 4),
            value: py_round(target_qty as f64 * px, 2),
            rank: rank_of(&symbol),
            half_sized: allocation.half_sized.contains(&symbol),
            tax_review: tax_flagged.contains(&symbol) && delta < 0,
            note: if guarded {
                "guard exit".to_owned()
            } else {
                String::new()
            },
            symbol,
        });
    }
    Ok(lines)
}

/// `projected_equity_pct(lines, capital)`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn projected_equity_pct(lines: &[OrderLine], capital: f64) -> f64 {
    if capital <= 0.0 {
        return 0.0;
    }
    py_round(
        py_sum(lines.iter().map(|o| o.qty_final as f64 * o.ref_price)) / capital * 100.0,
        6,
    )
}
