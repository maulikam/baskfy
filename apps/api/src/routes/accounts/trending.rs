//! `baskfy_core.curated_trending` — the ranked lists the home surface shows (SC9), ported as a
//! private module of the accounts lane (it has no goldens and only `/cb/trending` uses it; the
//! module is pure — candidates in, lists out — so it moves into `baskfy-core` unchanged when a
//! dumper grades it).
//!
//! A ranking that cannot be computed is **withheld, not faked**: fewer than [`MIN_ENTRIES`]
//! eligible baskets, or a popularity list over fewer than [`MIN_POPULATION`] people, comes back
//! with its reason and a sentence instead of entries. Absent is excluded, not last.

use chrono::{Datelike, NaiveDate};
use rust_decimal::{Decimal, RoundingStrategy};

/// `MIN_ENTRIES`.
pub const MIN_ENTRIES: usize = 3;
/// `MIN_POPULATION`.
pub const MIN_POPULATION: i64 = 5;
/// `LIST_LENGTH`.
pub const LIST_LENGTH: usize = 5;

/// `MetricKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MetricKind {
    /// `PCT`.
    Pct,
    /// `MONEY`.
    Money,
    /// `DATE`.
    Date,
    /// `COUNT`.
    Count,
}

impl MetricKind {
    /// The Literal's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Pct => "PCT",
            Self::Money => "MONEY",
            Self::Date => "DATE",
            Self::Count => "COUNT",
        }
    }
}

/// Which candidate field a list ranks on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Field {
    /// `ret_1m`.
    Ret1m,
    /// `ret_1y`.
    Ret1y,
    /// `cagr_5y`.
    Cagr5y,
    /// `min_amount`.
    MinAmount,
    /// `launched_at`.
    LaunchedAt,
    /// `last_rebalanced_on`.
    LastRebalancedOn,
    /// `watchers`.
    Watchers,
    /// `investors`.
    Investors,
    /// `inflow_amount`.
    InflowAmount,
}

/// `TrendingListDef`.
#[derive(Debug, Clone, Copy)]
pub struct ListDef {
    /// `key`.
    pub key: &'static str,
    /// `title`.
    pub title: &'static str,
    /// `ranks_by`.
    pub ranks_by: &'static str,
    /// `metric_label`.
    pub metric_label: &'static str,
    /// `metric_kind`.
    pub metric_kind: MetricKind,
    /// `field`.
    pub field: Field,
    /// `direction == "desc"`.
    pub descending: bool,
    /// `population_based`.
    pub population_based: bool,
}

/// `TRENDING_LISTS`, in declaration order.
pub const TRENDING_LISTS: [ListDef; 9] = [
    ListDef {
        key: "TOP_1M",
        title: "Moved most this month",
        ranks_by: "Ranked by 1-month price return, highest first.",
        metric_label: "1M return",
        metric_kind: MetricKind::Pct,
        field: Field::Ret1m,
        descending: true,
        population_based: false,
    },
    ListDef {
        key: "TOP_1Y",
        title: "Moved most this year",
        ranks_by: "Ranked by 1-year price return, highest first.",
        metric_label: "1Y return",
        metric_kind: MetricKind::Pct,
        field: Field::Ret1y,
        descending: true,
        population_based: false,
    },
    ListDef {
        key: "TOP_CAGR_5Y",
        title: "Longest run behind them",
        ranks_by: "Ranked by 5-year CAGR, highest first. Only baskets with five years of history.",
        metric_label: "5Y CAGR",
        metric_kind: MetricKind::Pct,
        field: Field::Cagr5y,
        descending: true,
        population_based: false,
    },
    ListDef {
        key: "BUDGET_FRIENDLY",
        title: "Cheapest to start",
        ranks_by: "Ranked by the smallest amount that buys one whole share of every constituent.",
        metric_label: "Min. amount",
        metric_kind: MetricKind::Money,
        field: Field::MinAmount,
        descending: false,
        population_based: false,
    },
    ListDef {
        key: "RECENTLY_LAUNCHED",
        title: "New here",
        ranks_by: "Ranked by launch date, newest first. New is not the same as proven.",
        metric_label: "Launched",
        metric_kind: MetricKind::Date,
        field: Field::LaunchedAt,
        descending: true,
        population_based: false,
    },
    ListDef {
        key: "RECENTLY_REBALANCED",
        title: "Just reviewed",
        ranks_by: "Ranked by the effective date of the newest published version, newest first.",
        metric_label: "Last review",
        metric_kind: MetricKind::Date,
        field: Field::LastRebalancedOn,
        descending: true,
        population_based: false,
    },
    ListDef {
        key: "MOST_WATCHED",
        title: "Most watchlisted",
        ranks_by: "Ranked by how many people have this on a watchlist.",
        metric_label: "Watchers",
        metric_kind: MetricKind::Count,
        field: Field::Watchers,
        descending: true,
        population_based: true,
    },
    ListDef {
        key: "MOST_INVESTED",
        title: "Most invested in",
        ranks_by: "Ranked by how many people currently hold it.",
        metric_label: "Investors",
        metric_kind: MetricKind::Count,
        field: Field::Investors,
        descending: true,
        population_based: true,
    },
    ListDef {
        key: "MOST_INFLOWS",
        title: "Biggest inflows",
        ranks_by: "Ranked by rupees committed through recorded buy batches.",
        metric_label: "Recorded inflow",
        metric_kind: MetricKind::Money,
        field: Field::InflowAmount,
        descending: true,
        population_based: true,
    },
];

/// `TrendingCandidate`: `None` means *not known*, never zero.
#[derive(Debug, Clone, Default)]
pub struct Candidate {
    /// `slug`.
    pub slug: String,
    /// `name`.
    pub name: String,
    /// `ret_1m`.
    pub ret_1m: Option<Decimal>,
    /// `ret_1y`.
    pub ret_1y: Option<Decimal>,
    /// `cagr_5y`.
    pub cagr_5y: Option<Decimal>,
    /// `min_amount`.
    pub min_amount: Option<Decimal>,
    /// `launched_at`.
    pub launched_at: Option<NaiveDate>,
    /// `last_rebalanced_on`.
    pub last_rebalanced_on: Option<NaiveDate>,
    /// `watchers`.
    pub watchers: Option<i64>,
    /// `investors`.
    pub investors: Option<i64>,
    /// `inflow_amount`.
    pub inflow_amount: Option<Decimal>,
}

/// `TrendingPopulation`.
#[derive(Debug, Clone, Copy, Default)]
pub struct Population {
    /// `watchers`.
    pub watchers: i64,
    /// `investors`.
    pub investors: i64,
    /// `contributors`.
    pub contributors: i64,
}

/// One metric value of a candidate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Value {
    /// A `Decimal`.
    Dec(Decimal),
    /// A `date`.
    Date(NaiveDate),
    /// An `int`.
    Int(i64),
}

/// `TrendingEntry`.
#[derive(Debug, Clone)]
pub struct Entry {
    /// `rank`.
    pub rank: i64,
    /// `slug`.
    pub slug: String,
    /// `name`.
    pub name: String,
    /// `metric_value`.
    pub metric_value: Option<Decimal>,
    /// `metric_date`.
    pub metric_date: Option<NaiveDate>,
    /// `metric_display`.
    pub metric_display: String,
}

/// `TrendingList`.
#[derive(Debug, Clone)]
pub struct TrendingList {
    /// The definition it was ranked from.
    pub def: ListDef,
    /// `population`.
    pub population: Option<i64>,
    /// `eligible`.
    pub eligible: i64,
    /// `entries`.
    pub entries: Vec<Entry>,
    /// `withheld_reason`.
    pub withheld_reason: Option<&'static str>,
    /// `withheld_note`.
    pub withheld_note: Option<String>,
    /// `price_return_caveat`: the list ranks a price return (`RETURN_RANKED_KEYS`).
    pub price_return_caveat: bool,
}

fn value_of(c: &Candidate, field: Field) -> Option<Value> {
    match field {
        Field::Ret1m => c.ret_1m.map(Value::Dec),
        Field::Ret1y => c.ret_1y.map(Value::Dec),
        Field::Cagr5y => c.cagr_5y.map(Value::Dec),
        Field::MinAmount => c.min_amount.map(Value::Dec),
        Field::LaunchedAt => c.launched_at.map(Value::Date),
        Field::LastRebalancedOn => c.last_rebalanced_on.map(Value::Date),
        Field::Watchers => c.watchers.map(Value::Int),
        Field::Investors => c.investors.map(Value::Int),
        Field::InflowAmount => c.inflow_amount.map(Value::Dec),
    }
}

/// `_eligible`: counts and money must also be positive.
fn eligible(c: &Candidate, def: &ListDef) -> Option<Value> {
    let value = value_of(c, def.field)?;
    if matches!(def.metric_kind, MetricKind::Count | MetricKind::Money) {
        let positive = match value {
            Value::Dec(d) => d > Decimal::ZERO,
            Value::Int(i) => i > 0,
            Value::Date(_) => false,
        };
        if !positive {
            return None;
        }
    }
    Some(value)
}

/// `_sort_key`: one comparable scale (dates by ordinal).
fn sort_key(value: Value) -> Decimal {
    match value {
        Value::Dec(d) => d,
        Value::Int(i) => Decimal::from(i),
        Value::Date(d) => Decimal::from(i64::from(d.num_days_from_ce())),
    }
}

/// `f"{n:,}"` over the integer digits of a non-negative-exponent Decimal string.
fn group_thousands(digits: &str) -> String {
    let (sign, body) = digits.strip_prefix('-').map_or(("", digits), |b| ("-", b));
    let mut out = String::new();
    for (i, c) in body.chars().enumerate() {
        if i > 0 && (body.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    format!("{sign}{out}")
}

/// `quantize(q, rounding=ROUND_HALF_UP)` to `dp` places, always exactly `dp`.
fn quantize_half_up(d: Decimal, dp: u32) -> Decimal {
    let mut q = d.round_dp_with_strategy(dp, RoundingStrategy::MidpointAwayFromZero);
    q.rescale(dp);
    q
}

/// `_display`: the rounded string every surface must use.
fn display(value: Value, def: &ListDef) -> String {
    match (def.metric_kind, value) {
        (MetricKind::Pct, Value::Dec(d)) => {
            format!("{}%", crate::wire::py_decimal(quantize_half_up(d, 2)))
        }
        (MetricKind::Money, Value::Dec(d)) => {
            format!(
                "₹{}",
                group_thousands(&crate::wire::py_decimal(quantize_half_up(d, 0)))
            )
        }
        (_, Value::Date(d)) => crate::wire::py_date(d),
        (_, Value::Int(i)) => i.to_string(),
        (_, Value::Dec(d)) => crate::wire::py_decimal(d),
    }
}

/// `_withheld_note`.
fn withheld_note(reason: &str, def: &ListDef, count: i64) -> String {
    if reason == "TOO_FEW_BASKETS" {
        return format!(
            "Not enough baskets to rank yet — {count} {} a {}, and a ranking needs {MIN_ENTRIES}.",
            if count == 1 { "has" } else { "have" },
            def.metric_label.to_lowercase()
        );
    }
    format!(
        "Not enough people yet — this ranks what investors did, and {count} {} done it. It stays \
         hidden until {MIN_POPULATION}.",
        if count == 1 {
            "person has"
        } else {
            "people have"
        }
    )
}

fn population_for(def: &ListDef, population: &Population) -> Option<i64> {
    if !def.population_based {
        return None;
    }
    Some(match def.field {
        Field::Watchers => population.watchers,
        Field::Investors => population.investors,
        _ => population.contributors,
    })
}

fn rank_one(def: &ListDef, candidates: &[Candidate], population: &Population) -> TrendingList {
    let people = population_for(def, population);
    let scored: Vec<(&Candidate, Value)> = candidates
        .iter()
        .filter_map(|c| eligible(c, def).map(|v| (c, v)))
        .collect();
    let eligible_count = i64::try_from(scored.len()).unwrap_or(i64::MAX);
    let (reason, note) = match people {
        Some(p) if def.population_based && p < MIN_POPULATION => (
            Some("TOO_FEW_PEOPLE"),
            Some(withheld_note("TOO_FEW_PEOPLE", def, p)),
        ),
        _ if scored.len() < MIN_ENTRIES => (
            Some("TOO_FEW_BASKETS"),
            Some(withheld_note("TOO_FEW_BASKETS", def, eligible_count)),
        ),
        _ => (None, None),
    };
    let mut entries = Vec::new();
    if reason.is_none() {
        let mut ordered = scored;
        ordered.sort_by(|(a, va), (b, vb)| {
            let (ka, kb) = (sort_key(*va), sort_key(*vb));
            let (ka, kb) = if def.descending { (-ka, -kb) } else { (ka, kb) };
            ka.cmp(&kb).then_with(|| a.slug.cmp(&b.slug))
        });
        for (index, (c, value)) in ordered.iter().take(LIST_LENGTH).enumerate() {
            entries.push(Entry {
                rank: i64::try_from(index).unwrap_or(0) + 1,
                slug: c.slug.clone(),
                name: c.name.clone(),
                metric_value: match value {
                    Value::Dec(d) => Some(*d),
                    _ => None,
                },
                metric_date: match value {
                    Value::Date(d) => Some(*d),
                    _ => None,
                },
                metric_display: display(*value, def),
            });
        }
    }
    TrendingList {
        def: *def,
        population: people,
        eligible: eligible_count,
        entries,
        withheld_reason: reason,
        withheld_note: note,
        price_return_caveat: def.metric_kind == MetricKind::Pct,
    }
}

/// `rank_lists(candidates, population=)`: all nine lists, ranked or withheld, in order.
#[must_use]
pub fn rank_lists(candidates: &[Candidate], population: &Population) -> Vec<TrendingList> {
    TRENDING_LISTS
        .iter()
        .map(|def| rank_one(def, candidates, population))
        .collect()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use std::str::FromStr;

    fn c(slug: &str, ret: &str, amount: &str) -> Candidate {
        Candidate {
            slug: slug.into(),
            name: slug.to_uppercase(),
            ret_1m: Some(Decimal::from_str(ret).unwrap()),
            min_amount: Some(Decimal::from_str(amount).unwrap()),
            watchers: Some(1),
            ..Candidate::default()
        }
    }

    #[test]
    fn lists_rank_withhold_and_round_as_python_does() {
        let lists = rank_lists(
            &[
                c("b", "1.005", "12345.5"),
                c("a", "1.005", "999.49"),
                c("c", "-2.5", "0"),
            ],
            &Population {
                watchers: 4,
                investors: 9,
                contributors: 0,
            },
        );
        assert_eq!(lists.len(), 9);
        let top = &lists[0];
        assert_eq!(top.withheld_reason, None);
        let slugs: Vec<&str> = top.entries.iter().map(|e| e.slug.as_str()).collect();
        assert_eq!(slugs, ["a", "b", "c"], "ties break by slug");
        assert_eq!(top.entries[0].metric_display, "1.01%");
        assert_eq!(top.entries[2].metric_display, "-2.50%");
        // BUDGET_FRIENDLY: a zero amount is not eligible, so two baskets is too few.
        let budget = &lists[3];
        assert_eq!(budget.withheld_reason, Some("TOO_FEW_BASKETS"));
        assert_eq!(
            budget.withheld_note.as_deref(),
            Some("Not enough baskets to rank yet — 2 have a min. amount, and a ranking needs 3.")
        );
        let watched = &lists[6];
        assert_eq!(watched.withheld_reason, Some("TOO_FEW_PEOPLE"));
        assert_eq!(watched.population, Some(4));
        assert_eq!(
            display(
                Value::Dec(Decimal::from_str("1234567.5").unwrap()),
                &lists[3].def
            ),
            "₹1,234,568"
        );
    }
}
