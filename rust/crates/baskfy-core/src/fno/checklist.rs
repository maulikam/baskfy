//! Port of `baskfy_core.fno.checklist` (`decile-blueprint/packages/core/src/baskfy_core/fno/checklist.py`).
//!
//! The paper checklist, `04` §9, as a tally — pure (`docs/fno/06` FO10).
//!
//! **F1**: 6 consecutive monthly cycles per underlying (12 structures in all), with ≥ 4 actually
//! opened. **F2**: 60 consecutive trading sessions with ≥ 15 positions closed, including ≥ 3
//! rolls. **Zero rule violations**: no uncovered short at any step, no position into expiry day,
//! no `LATE_EXIT`, no journal gap, no `NAKED_FUTURE`, and a cycle missed for want of a Kite
//! login counts as a violation, not a skip.
//!
//! The period counts **paper only** and restarts after a violation: a sleeve's cycles and
//! sessions are counted from the day after its latest violation (FO10.6).
//!
//! Goldens: `L1/fno/checklist.<function>.case_NNN.json`.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::NaiveDate;
use serde::{Deserialize, Serialize};

use super::config::{FoSleeve, FoSleeveGroup, group_of};
use crate::options::costs::Side;

/// `04` §9: consecutive monthly cycles per F1 underlying.
pub const F1_CYCLES: i64 = 6;
/// `04` §9: F1 structures actually opened.
pub const F1_MIN_OPENED: i64 = 4;
/// `04` §9: consecutive F2 sessions.
pub const F2_SESSIONS: i64 = 60;
/// `04` §9: F2 positions closed.
pub const F2_MIN_CLOSED: i64 = 15;
/// `04` §9: F2 rolls among them.
pub const F2_MIN_ROLLS: i64 = 3;

/// A rule the paper period must never break.
///
/// Ports `baskfy_core.fno.checklist.ViolationKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ViolationKind {
    /// A short open without its long.
    UncoveredShort,
    /// A position held on its contract's expiry day.
    IntoExpiry,
    /// The hard exit taken late.
    LateExit,
    /// A missing journal row or nightly mark.
    JournalGap,
    /// An F2 future without its resting stop.
    NakedFuture,
    /// A candidate cycle with no plan raised.
    MissedCycle,
}

impl ViolationKind {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::UncoveredShort => "UNCOVERED_SHORT",
            Self::IntoExpiry => "INTO_EXPIRY",
            Self::LateExit => "LATE_EXIT",
            Self::JournalGap => "JOURNAL_GAP",
            Self::NakedFuture => "NAKED_FUTURE",
            Self::MissedCycle => "MISSED_CYCLE",
        }
    }
}

/// One violation.
///
/// Ports `baskfy_core.fno.checklist.Violation`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Violation {
    /// What rule.
    pub kind: ViolationKind,
    /// Which sleeve.
    pub sleeve: FoSleeve,
    /// The day it happened.
    pub on: NaiveDate,
    /// The plan or position it names.
    #[serde(rename = "ref")]
    pub reference: String,
    /// The violation in words.
    pub message: String,
}

/// How an F1 cycle ended.
///
/// Ports `baskfy_core.fno.checklist.CycleOutcome`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CycleOutcome {
    /// A structure was opened in the cycle.
    Opened,
    /// The rules skipped it: a cycle, not a violation.
    Skipped,
    /// A candidate with no plan raised: a violation.
    Missed,
}

/// One F1 cycle.
///
/// Ports `baskfy_core.fno.checklist.F1Cycle`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct F1Cycle {
    /// The sleeve.
    pub sleeve: FoSleeve,
    /// The cycle's entry session.
    pub entry_session: NaiveDate,
    /// How it ended.
    pub outcome: CycleOutcome,
    /// A note (`""` by default).
    #[serde(default)]
    pub note: String,
}

/// One fill of an option position, in time order, for the covered check.
///
/// Ports `baskfy_core.fno.checklist.LegFill`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegFill {
    /// The leg role (`"SHORT_CALL"`, ...).
    pub role: String,
    /// Buy or sell.
    pub side: Side,
    /// Units.
    pub quantity: i64,
    /// The plan.
    #[serde(rename = "ref")]
    pub reference: String,
    /// The fill's day.
    pub on: NaiveDate,
}

fn side_value(side: Side) -> &'static str {
    match side {
        Side::Buy => "BUY",
        Side::Sell => "SELL",
    }
}

/// Replay a condor's fills in order: after every fill each short side must be covered by its
/// long. One violation per step that leaves a short uncovered.
///
/// Ports `baskfy_core.fno.checklist.uncovered_steps`.
#[must_use]
pub fn uncovered_steps(sleeve: FoSleeve, fills: &[LegFill]) -> Vec<Violation> {
    let mut held: HashMap<String, i64> = HashMap::new();
    let mut found = Vec::new();
    for fill in fills {
        let sign = if fill.side == Side::Buy { 1 } else { -1 };
        let delta = if fill.role.starts_with("SHORT") {
            -sign * fill.quantity
        } else {
            sign * fill.quantity
        };
        *held.entry(fill.role.clone()).or_insert(0) += delta;
        for (short, long) in [("SHORT_CALL", "LONG_CALL"), ("SHORT_PUT", "LONG_PUT")] {
            let s = held.get(short).copied().unwrap_or(0);
            let l = held.get(long).copied().unwrap_or(0).max(0);
            if s > l {
                found.push(Violation {
                    kind: ViolationKind::UncoveredShort,
                    sleeve,
                    on: fill.on,
                    reference: fill.reference.clone(),
                    message: format!(
                        "{s} {short} units against {l} {long} after a {} of {}",
                        side_value(fill.side),
                        fill.role
                    ),
                });
            }
        }
    }
    found
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// A position still open on its contract's expiry day (`PACK.4`: never).
///
/// Ports `baskfy_core.fno.checklist.into_expiry`.
#[must_use]
pub fn into_expiry(
    sleeve: FoSleeve,
    reference: &str,
    expiry: Option<NaiveDate>,
    closed_on: Option<NaiveDate>,
    as_of: NaiveDate,
) -> Option<Violation> {
    let expiry = expiry?;
    let last_held = closed_on.unwrap_or(as_of);
    (last_held >= expiry).then(|| Violation {
        kind: ViolationKind::IntoExpiry,
        sleeve,
        on: expiry,
        reference: reference.to_owned(),
        message: format!(
            "held on {}, its contract's expiry is {}",
            iso(last_held),
            iso(expiry)
        ),
    })
}

/// A closed position without its `fo_journal` row.
///
/// Ports `baskfy_core.fno.checklist.journal_gap`.
#[must_use]
pub fn journal_gap(
    sleeve: FoSleeve,
    reference: &str,
    closed_on: NaiveDate,
    journalled: bool,
) -> Option<Violation> {
    (!journalled).then(|| Violation {
        kind: ViolationKind::JournalGap,
        sleeve,
        on: closed_on,
        reference: reference.to_owned(),
        message: "closed with no journal row".to_owned(),
    })
}

/// Sessions an open position was held without its `fo_mark`: a missing night is a gap.
///
/// Ports `baskfy_core.fno.checklist.mark_gaps`.
#[must_use]
pub fn mark_gaps(
    sleeve: FoSleeve,
    reference: &str,
    held_sessions: &[NaiveDate],
    marked: &[NaiveDate],
) -> Vec<Violation> {
    let have: BTreeSet<NaiveDate> = marked.iter().copied().collect();
    let mut held = held_sessions.to_vec();
    held.sort_unstable();
    held.into_iter()
        .filter(|d| !have.contains(d))
        .map(|day| Violation {
            kind: ViolationKind::JournalGap,
            sleeve,
            on: day,
            reference: reference.to_owned(),
            message: format!("no mark for {}", iso(day)),
        })
        .collect()
}

/// The `MISSED` cycles as violations.
///
/// Ports `baskfy_core.fno.checklist.missed_cycles`.
#[must_use]
pub fn missed_cycles(cycles: &[F1Cycle]) -> Vec<Violation> {
    cycles
        .iter()
        .filter(|c| c.outcome == CycleOutcome::Missed)
        .map(|c| Violation {
            kind: ViolationKind::MissedCycle,
            sleeve: c.sleeve,
            on: c.entry_session,
            reference: format!("{}@{}", c.sleeve.value(), iso(c.entry_session)),
            message: if c.note.is_empty() {
                "a candidate cycle with no plan raised: the desk was not there".to_owned()
            } else {
                c.note.clone()
            },
        })
        .collect()
}

/// One F1 sleeve's tally.
///
/// Ports `baskfy_core.fno.checklist.F1Tally`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct F1Tally {
    /// The sleeve.
    pub sleeve: FoSleeve,
    /// The latest F1 violation (counting restarts the day after), if any.
    pub since: Option<NaiveDate>,
    /// Cycles counted.
    pub cycles: i64,
    /// Cycles with a structure opened.
    pub opened: i64,
}

/// F2's tally.
///
/// Ports `baskfy_core.fno.checklist.F2Tally`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct F2Tally {
    /// The first session counted.
    pub since: Option<NaiveDate>,
    /// Sessions counted.
    pub sessions: i64,
    /// Positions closed.
    pub closed: i64,
    /// Rolls among them.
    pub rolls: i64,
}

/// `04` §9's tally at `as_of`.
///
/// Ports `baskfy_core.fno.checklist.Checklist`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Checklist {
    /// The day of the tally.
    pub as_of: NaiveDate,
    /// F1N and F1B.
    pub f1: Vec<F1Tally>,
    /// Structures opened across both.
    pub f1_opened: i64,
    /// F1's bar is met.
    pub f1_met: bool,
    /// F2.
    pub f2: F2Tally,
    /// F2's bar is met.
    pub f2_met: bool,
    /// Every violation up to `as_of`, sorted by (day, kind, ref).
    pub violations: Vec<Violation>,
}

impl Checklist {
    /// Violations per kind, optionally for one sleeve group, sorted by kind.
    ///
    /// Ports `baskfy_core.fno.checklist.Checklist.violations_by_kind`.
    #[must_use]
    pub fn violations_by_kind(&self, group: Option<FoSleeveGroup>) -> BTreeMap<String, i64> {
        let mut counts: BTreeMap<String, i64> = BTreeMap::new();
        for v in &self.violations {
            if group.is_none_or(|g| group_of(v.sleeve) == g) {
                *counts.entry(v.kind.value().to_owned()).or_insert(0) += 1;
            }
        }
        counts
    }
}

/// A closed paper F2 position: when, and how many rolls it carried.
///
/// Ports `baskfy_core.fno.checklist.F2Close`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct F2Close {
    /// The close day.
    pub closed_on: NaiveDate,
    /// Rolls carried.
    pub rolls: i64,
}

fn since(violations: &[Violation], group: FoSleeveGroup) -> Option<NaiveDate> {
    violations
        .iter()
        .filter(|v| group_of(v.sleeve) == group)
        .map(|v| v.on)
        .max()
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `04` §9's tally at `as_of`. `sessions` is the exchange calendar; `f2_start` the first paper
/// F2 session, `None` before there is one.
///
/// Ports `baskfy_core.fno.checklist.paper_checklist`.
#[must_use]
pub fn paper_checklist(
    as_of: NaiveDate,
    f1_cycles: &[F1Cycle],
    f2_start: Option<NaiveDate>,
    f2_closes: &[F2Close],
    sessions: &[NaiveDate],
    violations: &[Violation],
) -> Checklist {
    let mut all_v = violations.to_vec();
    all_v.sort_by(|a, b| {
        (a.on, a.kind.value(), &a.reference).cmp(&(b.on, b.kind.value(), &b.reference))
    });
    all_v.retain(|v| v.on <= as_of);
    let f1_since = since(&all_v, FoSleeveGroup::F1);
    let f2_since = since(&all_v, FoSleeveGroup::F2);
    let cycles: Vec<&F1Cycle> = f1_cycles
        .iter()
        .filter(|c| c.entry_session <= as_of && f1_since.is_none_or(|s| c.entry_session > s))
        .collect();
    let tallies: Vec<F1Tally> = [FoSleeve::F1N, FoSleeve::F1B]
        .into_iter()
        .map(|sleeve| F1Tally {
            sleeve,
            since: f1_since,
            cycles: count(cycles.iter().filter(|c| c.sleeve == sleeve).count()),
            opened: count(
                cycles
                    .iter()
                    .filter(|c| c.sleeve == sleeve && c.outcome == CycleOutcome::Opened)
                    .count(),
            ),
        })
        .collect();
    let f1_opened: i64 = tallies.iter().map(|t| t.opened).sum();
    let f1_met = tallies.iter().all(|t| t.cycles >= F1_CYCLES) && f1_opened >= F1_MIN_OPENED;
    let mut start = f2_start;
    if let (Some(s), Some(v)) = (start, f2_since)
        && v >= s
    {
        start = sessions.iter().copied().find(|d| *d > v);
    }
    let counted = start.map_or(0, |s| {
        sessions.iter().filter(|d| s <= **d && **d <= as_of).count()
    });
    let closes: Vec<&F2Close> = f2_closes
        .iter()
        .filter(|c| start.is_some_and(|s| s <= c.closed_on && c.closed_on <= as_of))
        .collect();
    let f2 = F2Tally {
        since: start,
        sessions: count(counted),
        closed: count(closes.len()),
        rolls: closes.iter().map(|c| c.rolls).sum(),
    };
    let f2_met =
        f2.sessions >= F2_SESSIONS && f2.closed >= F2_MIN_CLOSED && f2.rolls >= F2_MIN_ROLLS;
    Checklist {
        as_of,
        f1: tallies,
        f1_opened,
        f1_met,
        f2,
        f2_met,
        violations: all_v,
    }
}
