//! Port of `baskfy_core.ranking_selection`: portfolio-aware selection over a quality rank
//! (contract C5). Informational only; never mutates a rank or a score.

use std::collections::{BTreeMap, BTreeSet};

use crate::decimal::{Rounding, WideDecimal};
use crate::rank_buffer::{HeldName, RebalancePlan, ScreenRank, inside_hold_band, plan_rebalance};

/// `SELECTION_VERSION`.
pub const SELECTION_VERSION: &str = "selection-2.0.0";
/// `MIN_CORRELATION_OBSERVATIONS`.
pub const MIN_CORRELATION_OBSERVATIONS: usize = 60;
/// `UNCLASSIFIED_SECTOR`.
pub const UNCLASSIFIED_SECTOR: &str = "unclassified";

/// `QualityRow`.
#[derive(Debug, Clone, PartialEq)]
pub struct QualityRow {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `rank`.
    pub rank: i64,
}

/// `select_from_ranks(SelectionInput(...))`.
///
/// # Errors
/// [`plan_rebalance`]'s.
pub fn select_from_ranks(
    ranked: &[QualityRow],
    holdings: &[HeldName],
    top_n: i64,
    hold_buffer: i64,
) -> Result<RebalancePlan, String> {
    let screen: Vec<ScreenRank> = ranked
        .iter()
        .map(|r| ScreenRank {
            instrument_id: r.instrument_id,
            symbol: r.symbol.clone(),
            name: r.name.clone(),
            rank: r.rank,
        })
        .collect();
    plan_rebalance(&screen, holdings, top_n, hold_buffer)
}

/// `SelectionAction`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    /// `hold`.
    Hold,
    /// `exit`.
    Exit,
    /// `enter`.
    Enter,
    /// `skip`.
    Skip,
}

impl Action {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Hold => "hold",
            Self::Exit => "exit",
            Self::Enter => "enter",
            Self::Skip => "skip",
        }
    }
}

/// `SelectionReason`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Reason {
    /// `RANK_WITHIN_RETENTION`.
    RankWithinRetention,
    /// `RANK_OUTSIDE_RETENTION`.
    RankOutsideRetention,
    /// `NOT_IN_RESULTS`.
    NotInResults,
    /// `RETAINED_BY_TURNOVER_BUDGET`.
    RetainedByTurnoverBudget,
    /// `RANK_WITHIN_ENTRY`.
    RankWithinEntry,
    /// `SECTOR_CAP`.
    SectorCap,
    /// `CAPACITY`.
    Capacity,
    /// `CORRELATION`.
    Correlation,
    /// `TURNOVER_BUDGET`.
    TurnoverBudget,
    /// `FULL`.
    Full,
}

impl Reason {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::RankWithinRetention => "RANK_WITHIN_RETENTION",
            Self::RankOutsideRetention => "RANK_OUTSIDE_RETENTION",
            Self::NotInResults => "NOT_IN_RESULTS",
            Self::RetainedByTurnoverBudget => "RETAINED_BY_TURNOVER_BUDGET",
            Self::RankWithinEntry => "RANK_WITHIN_ENTRY",
            Self::SectorCap => "SECTOR_CAP",
            Self::Capacity => "CAPACITY",
            Self::Correlation => "CORRELATION",
            Self::TurnoverBudget => "TURNOVER_BUDGET",
            Self::Full => "FULL",
        }
    }
}

/// `SelectionFlag`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Flag {
    /// `SECTOR_UNCLASSIFIED`.
    SectorUnclassified,
    /// `CAPACITY_UNKNOWN`.
    CapacityUnknown,
    /// `HOLD_ABOVE_CAPACITY`.
    HoldAboveCapacity,
    /// `PRICE_UNKNOWN`.
    PriceUnknown,
    /// `PRICE_ABOVE_SLOT_VALUE`.
    PriceAboveSlotValue,
    /// `CORRELATION_HISTORY_SHORT`.
    CorrelationHistoryShort,
    /// `CORRELATION_UNDEFINED`.
    CorrelationUndefined,
}

impl Flag {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::SectorUnclassified => "SECTOR_UNCLASSIFIED",
            Self::CapacityUnknown => "CAPACITY_UNKNOWN",
            Self::HoldAboveCapacity => "HOLD_ABOVE_CAPACITY",
            Self::PriceUnknown => "PRICE_UNKNOWN",
            Self::PriceAboveSlotValue => "PRICE_ABOVE_SLOT_VALUE",
            Self::CorrelationHistoryShort => "CORRELATION_HISTORY_SHORT",
            Self::CorrelationUndefined => "CORRELATION_UNDEFINED",
        }
    }

    fn sentence(self) -> Option<&'static str> {
        match self {
            Self::SectorUnclassified => Some("it has no sector, so the sector cap cannot apply"),
            Self::CapacityUnknown => Some("no median daily traded value, so capacity is unknown"),
            Self::PriceUnknown => Some("no price, so no proposed quantity"),
            Self::PriceAboveSlotValue => Some("one share costs more than the slot's value"),
            _ => None,
        }
    }
}

/// `SelectionWarning`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Warning {
    /// `HOLDINGS_EXCEED_MAX_NAMES`.
    HoldingsExceedMaxNames,
    /// `SECTOR_OVER_CAP_FROM_HOLDINGS`.
    SectorOverCapFromHoldings,
    /// `CAPACITY_NOT_CHECKED`.
    CapacityNotChecked,
}

impl Warning {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::HoldingsExceedMaxNames => "HOLDINGS_EXCEED_MAX_NAMES",
            Self::SectorOverCapFromHoldings => "SECTOR_OVER_CAP_FROM_HOLDINGS",
            Self::CapacityNotChecked => "CAPACITY_NOT_CHECKED",
        }
    }
}

const REFUSAL_ORDER: [Reason; 5] = [
    Reason::SectorCap,
    Reason::Capacity,
    Reason::Correlation,
    Reason::TurnoverBudget,
    Reason::Full,
];

/// `Candidate`.
#[derive(Debug, Clone, PartialEq)]
pub struct Candidate {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `quality_rank`.
    pub quality_rank: i64,
    /// `score`.
    pub score: Option<f64>,
    /// `sector`.
    pub sector: Option<String>,
    /// `adv_value_inr`.
    pub adv_value_inr: Option<WideDecimal>,
    /// `close_raw`.
    pub close_raw: Option<WideDecimal>,
}

impl Candidate {
    /// `__post_init__`.
    ///
    /// # Errors
    /// Python's `ValueError` message.
    pub fn validate(&self) -> Result<(), String> {
        if self.quality_rank < 1 {
            return Err(format!("{}: quality_rank must be at least 1", self.symbol));
        }
        if self.score.is_some_and(|s| !s.is_finite()) {
            return Err(format!(
                "{}: score must be finite; pass None for a missing score",
                self.symbol
            ));
        }
        if self
            .adv_value_inr
            .as_ref()
            .is_some_and(|a| a.is_sign_negative() && !a.is_zero())
        {
            return Err(format!(
                "{}: adv_value_inr must be a finite value >= 0",
                self.symbol
            ));
        }
        if self
            .close_raw
            .as_ref()
            .is_some_and(|c| c.is_zero() || c.is_sign_negative())
        {
            return Err(format!(
                "{}: close_raw must be a finite price > 0",
                self.symbol
            ));
        }
        Ok(())
    }
}

/// `Holding`.
#[derive(Debug, Clone, PartialEq)]
pub struct Holding {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `quantity`.
    pub quantity: WideDecimal,
    /// `sector`.
    pub sector: Option<String>,
}

impl Holding {
    /// `__post_init__`.
    ///
    /// # Errors
    /// Python's `ValueError` message.
    pub fn validate(&self) -> Result<(), String> {
        if self.quantity.is_zero() || self.quantity.is_sign_negative() {
            return Err(format!("{}: a holding's quantity must be > 0", self.symbol));
        }
        Ok(())
    }
}

/// `SelectionConstraints`.
#[derive(Debug, Clone, PartialEq)]
pub struct Constraints {
    /// `max_names`.
    pub max_names: i64,
    /// `entry_rank`.
    pub entry_rank: i64,
    /// `retention_rank`.
    pub retention_rank: i64,
    /// `max_per_sector`.
    pub max_per_sector: Option<i64>,
    /// `capital_inr`.
    pub capital_inr: Option<WideDecimal>,
    /// `max_adv_participation_pct`.
    pub max_adv_participation_pct: Option<WideDecimal>,
    /// `turnover_budget_names`.
    pub turnover_budget_names: Option<i64>,
    /// `max_correlation`.
    pub max_correlation: Option<f64>,
    /// `correlation_window`.
    pub correlation_window: i64,
}

impl Constraints {
    /// `__post_init__`.
    ///
    /// # Errors
    /// Python's `ValueError` message.
    pub fn validate(&self) -> Result<(), String> {
        if self.max_names < 1 {
            return Err("max_names must be at least 1".to_owned());
        }
        if self.entry_rank < 1 {
            return Err("entry_rank must be at least 1".to_owned());
        }
        if self.retention_rank < self.entry_rank {
            return Err("retention_rank must be >= entry_rank".to_owned());
        }
        if self.max_per_sector.is_some_and(|m| m < 1) {
            return Err("max_per_sector must be at least 1 (or None for no cap)".to_owned());
        }
        if self
            .capital_inr
            .as_ref()
            .is_some_and(|c| c.is_zero() || c.is_sign_negative())
        {
            return Err("capital_inr must be > 0 (or None)".to_owned());
        }
        if self
            .max_adv_participation_pct
            .as_ref()
            .is_some_and(|c| c.is_zero() || c.is_sign_negative())
        {
            return Err("max_adv_participation_pct must be > 0 (or None for no cap)".to_owned());
        }
        if self.turnover_budget_names.is_some_and(|t| t < 0) {
            return Err("turnover_budget_names must be >= 0 (or None for no budget)".to_owned());
        }
        if self
            .max_correlation
            .is_some_and(|m| !(m.is_finite() && (-1.0..=1.0).contains(&m)))
        {
            return Err("max_correlation must be within [-1, 1] (or None)".to_owned());
        }
        if self.correlation_window < i64::try_from(MIN_CORRELATION_OBSERVATIONS).unwrap_or(60) {
            return Err(format!(
                "correlation_window must be at least {MIN_CORRELATION_OBSERVATIONS} sessions"
            ));
        }
        Ok(())
    }
}

/// `SelectionRow`.
#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `action`.
    pub action: Action,
    /// `reasons`.
    pub reasons: Vec<Reason>,
    /// `flags`.
    pub flags: Vec<Flag>,
    /// `explanation`.
    pub explanation: String,
    /// `quality_rank`.
    pub quality_rank: Option<i64>,
    /// `score`.
    pub score: Option<f64>,
    /// `sector`.
    pub sector: Option<String>,
    /// `current_quantity`.
    pub current_quantity: Option<WideDecimal>,
    /// `proposed_value_inr`.
    pub proposed_value_inr: Option<WideDecimal>,
    /// `proposed_qty`.
    pub proposed_qty: Option<i64>,
    /// `adv_participation_pct`.
    pub adv_participation_pct: Option<WideDecimal>,
    /// `max_correlation`.
    pub max_correlation: Option<f64>,
    /// `correlation_peer`.
    pub correlation_peer: Option<String>,
}

/// `SelectionNote`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Note {
    /// `code`.
    pub code: Warning,
    /// `message`.
    pub message: String,
}

/// `SelectionResult`.
#[derive(Debug, Clone, PartialEq)]
pub struct SelectionResult {
    /// `constraints`.
    pub constraints: Constraints,
    /// `rows`.
    pub rows: Vec<Row>,
    /// `kept_count`.
    pub kept_count: i64,
    /// `entry_count`.
    pub entry_count: i64,
    /// `exit_count`.
    pub exit_count: i64,
    /// `skip_count`.
    pub skip_count: i64,
    /// `turnover_used`.
    pub turnover_used: i64,
    /// `unfilled_slots`.
    pub unfilled_slots: i64,
    /// `sector_counts`, sorted by sector.
    pub sector_counts: Vec<(String, i64)>,
    /// `notes`.
    pub notes: Vec<Note>,
    /// `version`.
    pub version: &'static str,
}

/// C5's `returns`: daily returns already sorted by date, one series per instrument id.
#[derive(Debug, Clone, Default)]
pub struct Returns {
    /// Instrument id → its returns, oldest first (NaN for a missing session).
    pub columns: Vec<(i64, Vec<f64>)>,
}

struct Sizing {
    value: Option<WideDecimal>,
    qty: Option<i64>,
    participation: Option<WideDecimal>,
    over_cap: bool,
    flags: Vec<Flag>,
}

struct Correlation {
    value: Option<f64>,
    peer: Option<String>,
    short: i64,
    undefined: i64,
}

struct Kept<'a> {
    holding: &'a Holding,
    candidate: Option<&'a Candidate>,
    retained_from: Option<Reason>,
}

type WouldExit<'a> = (&'a Holding, Option<&'a Candidate>, Reason);

fn classified(sector: Option<&str>) -> Option<String> {
    let s = sector?;
    let text = s.trim();
    if text.is_empty() || text == UNCLASSIFIED_SECTOR {
        return None;
    }
    Some(s.to_owned())
}

fn holding_sector(holding: &Holding, candidate: Option<&Candidate>) -> Option<String> {
    let own = classified(holding.sector.as_deref());
    match (own, candidate) {
        (Some(o), _) => Some(o),
        (None, None) => None,
        (None, Some(c)) => classified(c.sector.as_deref()),
    }
}

fn refuse_duplicates(candidates: &[Candidate], holdings: &[Holding]) -> Result<(), String> {
    for (label, ids) in [
        (
            "candidates",
            candidates
                .iter()
                .map(|c| c.instrument_id)
                .collect::<Vec<_>>(),
        ),
        (
            "holdings",
            holdings.iter().map(|h| h.instrument_id).collect::<Vec<_>>(),
        ),
    ] {
        let mut seen = BTreeSet::new();
        for id in ids {
            if !seen.insert(id) {
                return Err(format!("duplicate instrument_id {id} in {label}"));
            }
        }
    }
    Ok(())
}

fn worst_first(item: &WouldExit<'_>) -> (i64, i64, i64) {
    let (holding, candidate, _) = item;
    match candidate {
        None => (0, 0, -holding.instrument_id),
        Some(c) => (1, -c.quality_rank, -holding.instrument_id),
    }
}

fn size(
    candidate: &Candidate,
    slot_value: Option<&WideDecimal>,
    cap: Option<&WideDecimal>,
) -> Sizing {
    let Some(slot) = slot_value else {
        return Sizing {
            value: None,
            qty: None,
            participation: None,
            over_cap: false,
            flags: Vec::new(),
        };
    };
    let mut flags = Vec::new();
    let mut qty = None;
    match &candidate.close_raw {
        None => flags.push(Flag::PriceUnknown),
        Some(close) => {
            let q = slot
                .div(close)
                .ok()
                .and_then(|r| r.quantize(0, Rounding::Down).ok())
                .and_then(|r| r.to_string().parse::<i64>().ok())
                .unwrap_or(0);
            if q == 0 {
                flags.push(Flag::PriceAboveSlotValue);
            }
            qty = Some(q);
        }
    }
    let mut participation = None;
    let mut over_cap = false;
    match &candidate.adv_value_inr {
        None => flags.push(Flag::CapacityUnknown),
        Some(adv) if adv.is_zero() => over_cap = cap.is_some(),
        Some(adv) => {
            let p = slot
                .div(adv)
                .ok()
                .map(|r| r.mul(&WideDecimal::from(100_i64)))
                .and_then(|r| r.quantize(-4, Rounding::HalfUp).ok());
            over_cap = match (cap, &p) {
                (Some(c), Some(p)) => p > c,
                _ => false,
            };
            participation = p;
        }
    }
    Sizing {
        value: Some(slot.clone()),
        qty,
        participation,
        over_cap,
        flags,
    }
}

fn pairwise_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0;
        for x in a {
            res += x;
        }
        res
    } else if n <= 128 {
        let mut r = [0.0_f64; 8];
        r.copy_from_slice(&a[..8]);
        let mut i = 8;
        while i < n - (n % 8) {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
    }
}

#[allow(clippy::cast_precision_loss)]
fn mean(x: &[f64]) -> f64 {
    pairwise_sum(x) / x.len() as f64
}

#[allow(clippy::cast_precision_loss)]
fn np_std(x: &[f64]) -> f64 {
    let m = mean(x);
    let sq: Vec<f64> = x.iter().map(|v| (v - m) * (v - m)).collect();
    (pairwise_sum(&sq) / x.len() as f64).sqrt()
}

#[allow(clippy::cast_precision_loss)]
fn corrcoef(x: &[f64], y: &[f64]) -> f64 {
    let fact = 1.0 / (x.len() as f64 - 1.0);
    let (mx, my) = (mean(x), mean(y));
    let xs: Vec<f64> = x.iter().map(|v| v - mx).collect();
    let ys: Vec<f64> = y.iter().map(|v| v - my).collect();
    let dot = |a: &[f64], b: &[f64]| a.iter().zip(b).map(|(p, q)| p * q).sum::<f64>();
    let cxy = dot(&xs, &ys) * fact;
    let sx = (dot(&xs, &xs) * fact).sqrt();
    let sy = (dot(&ys, &ys) * fact).sqrt();
    (cxy / sx / sy).clamp(-1.0, 1.0)
}

fn round4(x: f64) -> f64 {
    format!("{x:.4}").parse().unwrap_or(x)
}

type Window = BTreeMap<i64, Vec<f64>>;

fn returns_window(returns: &Returns, rows: i64) -> Result<Window, String> {
    let mut seen = BTreeSet::new();
    for (id, _) in &returns.columns {
        if !seen.insert(*id) {
            return Err("returns has duplicate instrument columns".to_owned());
        }
    }
    let mut out = BTreeMap::new();
    let keep = usize::try_from(rows).unwrap_or(0);
    for (id, values) in &returns.columns {
        let start = values.len().saturating_sub(keep);
        let tail: Vec<f64> = values[start..]
            .iter()
            .map(|v| if v.is_finite() { *v } else { f64::NAN })
            .collect();
        out.insert(*id, tail);
    }
    Ok(out)
}

enum Pair {
    Ok(f64),
    Short,
    Undefined,
}

fn pair_correlation(window: &Window, left: i64, right: i64) -> Pair {
    let (Some(xa), Some(ya)) = (window.get(&left), window.get(&right)) else {
        return Pair::Short;
    };
    let mut x = Vec::new();
    let mut y = Vec::new();
    for (a, b) in xa.iter().zip(ya) {
        if !a.is_nan() && !b.is_nan() {
            x.push(*a);
            y.push(*b);
        }
    }
    if x.len() < MIN_CORRELATION_OBSERVATIONS {
        return Pair::Short;
    }
    if np_std(&x) == 0.0 || np_std(&y) == 0.0 {
        return Pair::Undefined;
    }
    Pair::Ok(round4(corrcoef(&x, &y)))
}

fn max_correlation(
    window: Option<&Window>,
    candidate: &Candidate,
    peers: &[(i64, String)],
) -> Correlation {
    let Some(window) = window else {
        return Correlation {
            value: None,
            peer: None,
            short: 0,
            undefined: 0,
        };
    };
    let mut best: Option<f64> = None;
    let mut best_peer = None;
    let (mut short, mut undefined) = (0, 0);
    for (peer_id, peer_symbol) in peers {
        match pair_correlation(window, candidate.instrument_id, *peer_id) {
            Pair::Short => short += 1,
            Pair::Undefined => undefined += 1,
            Pair::Ok(v) => {
                if best.is_none_or(|b| v > b) {
                    best = Some(v);
                    best_peer = Some(peer_symbol.clone());
                }
            }
        }
    }
    Correlation {
        value: best,
        peer: best_peer,
        short,
        undefined,
    }
}

fn names(count: Option<i64>) -> String {
    match count {
        Some(1) => "1 name".to_owned(),
        Some(n) => format!("{n} names"),
        None => "None names".to_owned(),
    }
}

/// `f"₹{value:,.2f}"`.
#[must_use]
pub fn inr(value: &WideDecimal) -> String {
    let rounded = value
        .quantize(-2, Rounding::HalfEven)
        .unwrap_or_else(|_| value.clone())
        .to_string();
    let (sign, body) = match rounded.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", rounded.as_str()),
    };
    let (int, frac) = body.split_once('.').unwrap_or((body, "00"));
    let mut grouped = String::new();
    for (i, ch) in int.chars().enumerate() {
        if i > 0 && (int.len() - i) % 3 == 0 {
            grouped.push(',');
        }
        grouped.push(ch);
    }
    format!("₹{sign}{grouped}.{frac}")
}

/// `f"{value.normalize():f}%"`.
#[must_use]
pub fn pct(value: &WideDecimal) -> String {
    let text = value.to_string();
    let trimmed = if text.contains('.') {
        text.trim_end_matches('0').trim_end_matches('.').to_owned()
    } else {
        text
    };
    format!("{trimmed}%")
}

struct Book {
    kept_total: i64,
    entry_allowance: Option<i64>,
    sector_counts: BTreeMap<String, i64>,
    unclassified: i64,
    peers: Vec<(i64, String)>,
    accepted: i64,
}

impl Book {
    fn add(&mut self, instrument_id: i64, symbol: &str, sector: Option<&str>) {
        match sector {
            None => self.unclassified += 1,
            Some(s) => *self.sector_counts.entry(s.to_owned()).or_insert(0) += 1,
        }
        self.peers.push((instrument_id, symbol.to_owned()));
    }
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `select_portfolio(candidates, holdings, constraints, returns)`.
///
/// # Errors
/// Python's `ValueError` message (duplicates, a correlation cap without returns, a bad input).
pub fn select_portfolio(
    candidates: &[Candidate],
    holdings: &[Holding],
    constraints: &Constraints,
    returns: Option<&Returns>,
) -> Result<SelectionResult, String> {
    refuse_duplicates(candidates, holdings)?;
    let c = constraints;
    if c.max_correlation.is_some() && returns.is_none() {
        return Err("max_correlation is set but no returns frame was given".to_owned());
    }
    let by_id: BTreeMap<i64, &Candidate> =
        candidates.iter().map(|x| (x.instrument_id, x)).collect();
    let (kept, exiting, entry_allowance) = split_holdings(holdings, &by_id, c);

    let slot_value = match &c.capital_inr {
        Some(capital) => Some(
            capital
                .div(&WideDecimal::from(c.max_names))
                .and_then(|v| v.quantize(-2, Rounding::Down))
                .map_err(|e| e.to_string())?,
        ),
        None => None,
    };
    let window = match (c.max_correlation, returns) {
        (Some(_), Some(r)) => Some(returns_window(r, c.correlation_window)?),
        _ => None,
    };
    let mut book = Book {
        kept_total: len_i64(kept.len()),
        entry_allowance,
        sector_counts: BTreeMap::new(),
        unclassified: 0,
        peers: Vec::new(),
        accepted: 0,
    };
    let mut rows = Vec::new();
    for item in &kept {
        let sector = holding_sector(item.holding, item.candidate);
        book.add(
            item.holding.instrument_id,
            &item.holding.symbol,
            sector.as_deref(),
        );
        rows.push(hold_row(item, sector, c, slot_value.as_ref())?);
    }
    for (holding, candidate, reason) in &exiting {
        rows.push(exit_row(holding, *candidate, *reason, c));
    }
    let notes = notes(&book, c, slot_value.as_ref());

    let held_ids: BTreeSet<i64> = holdings.iter().map(|h| h.instrument_id).collect();
    let mut entrants: Vec<&Candidate> = candidates
        .iter()
        .filter(|x| !held_ids.contains(&x.instrument_id) && x.quality_rank <= c.entry_rank)
        .collect();
    entrants.sort_by_key(|x| (x.quality_rank, x.instrument_id));
    for candidate in entrants {
        rows.push(entrant_row(
            candidate,
            &mut book,
            c,
            slot_value.as_ref(),
            window.as_ref(),
        ));
    }

    let mut counts = book.sector_counts.clone();
    if book.unclassified > 0 {
        *counts.entry(UNCLASSIFIED_SECTOR.to_owned()).or_insert(0) += book.unclassified;
    }
    rows.sort_by_key(row_order);
    let skip_count = len_i64(rows.iter().filter(|r| r.action == Action::Skip).count());
    let exit_count = len_i64(exiting.len());
    Ok(SelectionResult {
        constraints: c.clone(),
        kept_count: book.kept_total,
        entry_count: book.accepted,
        exit_count,
        skip_count,
        turnover_used: book.accepted + exit_count,
        unfilled_slots: (c.max_names - book.kept_total - book.accepted).max(0),
        sector_counts: counts.into_iter().collect(),
        notes,
        rows,
        version: SELECTION_VERSION,
    })
}

fn row_order(row: &Row) -> (i64, i64, String, i64) {
    match row.quality_rank {
        None => (1, 0, row.symbol.clone(), row.instrument_id),
        Some(r) => (0, r, String::new(), row.instrument_id),
    }
}

fn split_holdings<'a>(
    held: &'a [Holding],
    by_id: &BTreeMap<i64, &'a Candidate>,
    c: &Constraints,
) -> (Vec<Kept<'a>>, Vec<WouldExit<'a>>, Option<i64>) {
    let band = c.retention_rank - c.entry_rank;
    let mut kept = Vec::new();
    let mut would_exit: Vec<WouldExit<'a>> = Vec::new();
    for holding in held {
        match by_id.get(&holding.instrument_id).copied() {
            None => would_exit.push((holding, None, Reason::NotInResults)),
            Some(cand) if inside_hold_band(cand.quality_rank, c.entry_rank, band) => {
                kept.push(Kept {
                    holding,
                    candidate: Some(cand),
                    retained_from: None,
                });
            }
            Some(cand) => would_exit.push((holding, Some(cand), Reason::RankOutsideRetention)),
        }
    }
    would_exit.sort_by_key(worst_first);
    let Some(budget) = c.turnover_budget_names else {
        return (kept, would_exit, None);
    };
    let cut = usize::try_from(budget).unwrap_or(0).min(would_exit.len());
    let retained = would_exit.split_off(cut);
    for (holding, candidate, reason) in retained {
        kept.push(Kept {
            holding,
            candidate,
            retained_from: Some(reason),
        });
    }
    let left = budget - len_i64(would_exit.len());
    (kept, would_exit, Some(left))
}

fn notes(book: &Book, c: &Constraints, slot_value: Option<&WideDecimal>) -> Vec<Note> {
    let mut out = Vec::new();
    if book.kept_total > c.max_names {
        out.push(Note {
            code: Warning::HoldingsExceedMaxNames,
            message: format!(
                "{} holdings are kept, more than max_names ({}); kept holdings are never \
                 trimmed, so no entries fit.",
                book.kept_total, c.max_names
            ),
        });
    }
    if let Some(cap) = c.max_per_sector {
        let over: Vec<&str> = book
            .sector_counts
            .iter()
            .filter(|(_, n)| **n > cap)
            .map(|(s, _)| s.as_str())
            .collect();
        if !over.is_empty() {
            out.push(Note {
                code: Warning::SectorOverCapFromHoldings,
                message: format!(
                    "Kept holdings alone exceed the cap of {cap} per sector in: {}. Holdings \
                     are never exited for a cap.",
                    over.join(", ")
                ),
            });
        }
    }
    if slot_value.is_none() {
        out.push(Note {
            code: Warning::CapacityNotChecked,
            message: "Capacity was not checked and no sizes are proposed: capital_inr was not \
                      given."
                .to_owned(),
        });
    } else if c.max_adv_participation_pct.is_none() {
        out.push(Note {
            code: Warning::CapacityNotChecked,
            message: "Capacity was not checked: max_adv_participation_pct was not given."
                .to_owned(),
        });
    }
    out
}

fn entrant_row(
    candidate: &Candidate,
    book: &mut Book,
    c: &Constraints,
    slot_value: Option<&WideDecimal>,
    window: Option<&Window>,
) -> Row {
    let mut clauses: BTreeMap<Reason, String> = BTreeMap::new();
    let mut flags = Vec::new();
    let sector = classified(candidate.sector.as_deref());
    if let Some(cap) = c.max_per_sector {
        match &sector {
            None => flags.push(Flag::SectorUnclassified),
            Some(s) => {
                let in_sector = book.sector_counts.get(s).copied().unwrap_or(0);
                if in_sector >= cap {
                    clauses.insert(
                        Reason::SectorCap,
                        format!(
                            "{s} already has {}, the cap is {cap}",
                            names(Some(in_sector))
                        ),
                    );
                }
            }
        }
    }
    let sizing = size(candidate, slot_value, c.max_adv_participation_pct.as_ref());
    flags.extend(sizing.flags.iter().copied());
    if let Some(text) = capacity_clause(&sizing, c.max_adv_participation_pct.as_ref()) {
        clauses.insert(Reason::Capacity, text);
    }
    let corr = max_correlation(window, candidate, &book.peers);
    if corr.short > 0 {
        flags.push(Flag::CorrelationHistoryShort);
    }
    if corr.undefined > 0 {
        flags.push(Flag::CorrelationUndefined);
    }
    if let (Some(limit), Some(value)) = (c.max_correlation, corr.value)
        && value > limit
    {
        clauses.insert(
            Reason::Correlation,
            format!(
                "its correlation with {} is {value:.4} over the last {} sessions, above the {} \
                 limit",
                corr.peer.as_deref().unwrap_or("None"),
                c.correlation_window,
                crate::ranking_engine::py_format_g(limit, 6)
            ),
        );
    }
    if book.entry_allowance.is_some_and(|a| book.accepted >= a) {
        clauses.insert(
            Reason::TurnoverBudget,
            format!(
                "the turnover budget of {} is used up",
                names(c.turnover_budget_names)
            ),
        );
    }
    if book.kept_total + book.accepted >= c.max_names {
        clauses.insert(
            Reason::Full,
            format!("the book is full at {}", names(Some(c.max_names))),
        );
    }
    let refusals: Vec<Reason> = REFUSAL_ORDER
        .iter()
        .copied()
        .filter(|r| clauses.contains_key(r))
        .collect();
    let head = format!(
        "ranked {}, inside the entry limit of {}",
        candidate.quality_rank, c.entry_rank
    );
    let (action, reasons, explanation) = if refusals.is_empty() {
        book.accepted += 1;
        book.add(
            candidate.instrument_id,
            &candidate.symbol,
            sector.as_deref(),
        );
        (
            Action::Enter,
            vec![Reason::RankWithinEntry],
            format!("Enter: {head}.{}", sizing_sentence(&sizing)),
        )
    } else {
        let parts: Vec<&str> = refusals
            .iter()
            .filter_map(|r| clauses.get(r).map(String::as_str))
            .collect();
        (
            Action::Skip,
            refusals.clone(),
            format!("Skip: {head}, but {}.", parts.join("; and ")),
        )
    };
    Row {
        instrument_id: candidate.instrument_id,
        symbol: candidate.symbol.clone(),
        action,
        reasons,
        explanation: explanation + &flag_sentence(&flags, &corr),
        flags,
        quality_rank: Some(candidate.quality_rank),
        score: candidate.score,
        sector: candidate.sector.clone(),
        current_quantity: None,
        proposed_value_inr: sizing.value,
        proposed_qty: sizing.qty,
        adv_participation_pct: sizing.participation,
        max_correlation: corr.value,
        correlation_peer: corr.peer,
    }
}

fn capacity_clause(sizing: &Sizing, cap: Option<&WideDecimal>) -> Option<String> {
    let cap = cap?;
    let value = sizing.value.as_ref()?;
    if !sizing.over_cap {
        return None;
    }
    Some(match &sizing.participation {
        None => format!(
            "its median daily traded value is zero, so any position breaks the {} \
             participation cap",
            pct(cap)
        ),
        Some(p) => format!(
            "{} would be {} of median daily traded value, above the {} cap",
            inr(value),
            pct(p),
            pct(cap)
        ),
    })
}

fn hold_row(
    item: &Kept<'_>,
    sector: Option<String>,
    c: &Constraints,
    slot_value: Option<&WideDecimal>,
) -> Result<Row, String> {
    let (holding, candidate) = (item.holding, item.candidate);
    let cap_pct = c.max_adv_participation_pct.as_ref();
    let sizing = candidate.map(|cand| size(cand, slot_value, cap_pct));
    let mut flags: Vec<Flag> = sizing.as_ref().map(|s| s.flags.clone()).unwrap_or_default();
    let over = sizing.as_ref().is_some_and(|s| s.over_cap);
    if over {
        flags.push(Flag::HoldAboveCapacity);
    }
    let (reasons, mut explanation) = if let Some(from) = item.retained_from {
        (
            vec![Reason::RetainedByTurnoverBudget, from],
            format!(
                "Hold: {}, so it would exit, but the turnover budget of {} is spent on \
                 worse-ranked exits.",
                would_exit_clause(candidate, c),
                names(c.turnover_budget_names)
            ),
        )
    } else if let Some(cand) = candidate {
        (
            vec![Reason::RankWithinRetention],
            format!(
                "Hold: ranked {}, inside the retention limit of {}.",
                cand.quality_rank, c.retention_rank
            ),
        )
    } else {
        return Err(format!(
            "{}: a hold with no rank must be a budget retention",
            holding.symbol
        ));
    };
    if over && let Some(cap) = cap_pct {
        explanation.push_str(&format!(
            " Its equal-weight target is above the {} participation cap; holdings are never \
             refused for capacity.",
            pct(cap)
        ));
    }
    Ok(Row {
        instrument_id: holding.instrument_id,
        symbol: holding.symbol.clone(),
        action: Action::Hold,
        reasons,
        flags,
        explanation,
        quality_rank: candidate.map(|x| x.quality_rank),
        score: candidate.and_then(|x| x.score),
        sector,
        current_quantity: Some(holding.quantity.clone()),
        proposed_value_inr: sizing.as_ref().and_then(|s| s.value.clone()),
        proposed_qty: sizing.as_ref().and_then(|s| s.qty),
        adv_participation_pct: sizing.as_ref().and_then(|s| s.participation.clone()),
        max_correlation: None,
        correlation_peer: None,
    })
}

fn exit_row(
    holding: &Holding,
    candidate: Option<&Candidate>,
    reason: Reason,
    c: &Constraints,
) -> Row {
    Row {
        instrument_id: holding.instrument_id,
        symbol: holding.symbol.clone(),
        action: Action::Exit,
        reasons: vec![reason],
        flags: Vec::new(),
        explanation: format!("Exit: {}.", would_exit_clause(candidate, c)),
        quality_rank: candidate.map(|x| x.quality_rank),
        score: candidate.and_then(|x| x.score),
        sector: holding_sector(holding, candidate),
        current_quantity: Some(holding.quantity.clone()),
        proposed_value_inr: None,
        proposed_qty: None,
        adv_participation_pct: None,
        max_correlation: None,
        correlation_peer: None,
    }
}

fn would_exit_clause(candidate: Option<&Candidate>, c: &Constraints) -> String {
    match candidate {
        None => "not in this screen's results (filtered out, or outside its universe)".to_owned(),
        Some(x) => format!(
            "ranked {}, outside the retention limit of {}",
            x.quality_rank, c.retention_rank
        ),
    }
}

fn sizing_sentence(sizing: &Sizing) -> String {
    let Some(value) = &sizing.value else {
        return String::new();
    };
    let mut out = format!(" Proposed {}", inr(value));
    let mut detail = Vec::new();
    if let Some(q) = sizing.qty {
        detail.push(format!("{q} shares"));
    }
    if let Some(p) = &sizing.participation {
        detail.push(format!("{} of median daily traded value", pct(p)));
    }
    if !detail.is_empty() {
        out.push_str(&format!(" ({})", detail.join(", ")));
    }
    out.push('.');
    out
}

fn flag_sentence(flags: &[Flag], corr: &Correlation) -> String {
    let mut notes: Vec<String> = flags
        .iter()
        .filter_map(|f| f.sentence().map(str::to_owned))
        .collect();
    if flags.contains(&Flag::CorrelationHistoryShort) {
        notes.push(format!(
            "correlation not checked against {} name(s) with fewer than \
             {MIN_CORRELATION_OBSERVATIONS} shared sessions of returns",
            corr.short
        ));
    }
    if flags.contains(&Flag::CorrelationUndefined) {
        notes.push(format!(
            "correlation undefined against {} name(s) with a constant return series",
            corr.undefined
        ));
    }
    if notes.is_empty() {
        return String::new();
    }
    format!(" Note: {}.", notes.join("; "))
}
