//! Port of `baskfy_core.ranking_engine`.
//!
//! ranking, explanation and target scores. Goldens: go/testdata/golden/L1/ranking.
//!
//! The ranking engine — docs/ranking/PLAN.md contract C4. Input is one frame: the selected
//! universe for `as_of` **after** the `apply_filters_on` bucket and **before** any filter,
//! carrying `passes_filters` and the per-clause `fail__*` flags, the point-in-time sector, every
//! term's raw value, the book's desk score columns and the NSE momentum inputs. Output is the
//! ranked rows plus everything [`explain`] needs to say why a row sits where it does.
//!
//! The six steps:
//!
//! 1. **Computed factors** — `desk_score` arrives joined; `nse_momentum_score` is computed by
//!    [`add_nse_momentum_score`] (NSE's NIFTY200 Momentum 30 normalised score).
//! 2. **Scope set** — `fixed_universe`: every row; `filtered_results`: the rows that will be
//!    results; `within_sector`: every row, grouped by sector, NULL sector → `"unclassified"`.
//! 3. **Transform** each term inside its scope set (per group for `within_sector`) to `[0, 1]`.
//! 4. **Combine** — composite: family-weighted mean of term scores x 100, rounded half-up to
//!    2 dp through `Decimal(repr(x))`; sequential / single: raw values in term order.
//! 5. **Output** — rows that pass the filters and are not excluded, ranked `1..n`, ties broken
//!    by `instrument_id`.
//! 6. **Explain** one row as a [`RankExplanation`].
//!
//! Python's pandas `float64` is `f64` here, and pandas' missing marker (`NaN`) is `None` at
//! every public boundary. Frames are polars `DataFrame`s with the same columns, in the same
//! order, as the pandas frames the Python returns. Every numeric reduction reproduces the
//! kernel pandas runs: average-tie ranks as `sum of positions / ties`, numpy's pairwise sum
//! for means, and the two-pass variance for the NSE Z-score's population standard deviation.

use std::collections::{BTreeMap, HashSet};

use chrono::NaiveDate;
use polars::prelude::{
    AnyValue, Column, DataFrame, DataType, IdxCa, IdxSize, IntoColumn, NamedFrom, Series,
};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};

/// Bumped whenever the engine's behaviour changes (`RANKING_ENGINE_VERSION`).
pub const RANKING_ENGINE_VERSION: &str = "ranking-2.0.0";

/// `baskfy_core.nse_momentum.NSE_MOMENTUM_VERSION`, cited by every explanation's provenance.
pub const NSE_MOMENTUM_VERSION: &str = "nifty200-momentum30-2026.09";

/// C1's weight families, in the order the UI lists them.
pub const WEIGHT_FAMILIES: [&str; 5] = [
    "momentum",
    "path_quality",
    "trend_structure",
    "participation",
    "risk_execution",
];

/// Frame column: the instrument id (unique per row).
pub const INSTRUMENT_ID: &str = "instrument_id";
/// Frame column: the symbol.
pub const SYMBOL: &str = "symbol";
/// Frame column: whether the row passes every filter clause.
pub const PASSES_FILTERS: &str = "passes_filters";
/// Frame column: the point-in-time sector.
pub const SECTOR: &str = "sector";
/// Prefix of one boolean per filter clause; `true` means the row fails that clause.
pub const FAIL_PREFIX: &str = "fail__";
/// The group a NULL sector ranks in under `within_sector`.
pub const UNCLASSIFIED_SECTOR: &str = "unclassified";

/// The desk score's factor key.
pub const DESK_SCORE_KEY: &str = "desk_score";
/// Frame column: the desk score's rank in the book.
pub const DESK_SCORE_RANK: &str = "desk_score_rank";
/// Frame column: the book's `;`-separated reject tokens.
pub const DESK_REJECT: &str = "desk_reject";
/// Desk component column → (display name, the component's maximum in `score.score`).
pub const DESK_COMPONENTS: [(&str, &str, f64); 5] = [
    ("desk_a_trend", "A trend", 25.0),
    ("desk_b_momentum", "B momentum", 25.0),
    ("desk_c_sharpe", "C Sharpe", 20.0),
    ("desk_d_consistency", "D consistency", 10.0),
    ("desk_e_liquidity", "E liquidity", 10.0),
];
/// Frame column: the desk's F penalty.
pub const DESK_F_PENALTY: &str = "desk_f_penalty";
/// Frame column: `desk_score_daily.ext_over_20dma`.
pub const DESK_EXT_OVER_20DMA: &str = "desk_ext_over_20dma";
/// Frame column: `desk_score_daily.score_version` on the row itself.
pub const DESK_ROW_SCORE_VERSION: &str = "desk_score_version";
/// F's floor in `score.score`.
pub const DESK_F_FLOOR: f64 = -10.0;

/// The NSE momentum score's factor key.
pub const NSE_MOMENTUM_KEY: &str = "nse_momentum_score";
/// Frame column: NSE's 6-month momentum ratio.
pub const NSE_MR6: &str = "nse_mr6";
/// Frame column: NSE's 12-month momentum ratio.
pub const NSE_MR12: &str = "nse_mr12";
/// Frame column: point-in-time NIFTY 200 membership.
pub const IN_NIFTY_200: &str = "in_nifty_200";
/// Frame column: F&O availability.
pub const IS_FNO: &str = "is_fno";
/// Optional constant columns: Z statistics over the whole eligible universe.
pub const NSE_POPULATION_COLUMNS: [&str; 4] = [
    "nse_mr6_mean",
    "nse_mr6_std",
    "nse_mr12_mean",
    "nse_mr12_std",
];

/// Frame column: the instrument's last bar date.
pub const LAST_BAR_DATE: &str = "last_bar_date";
/// Frame column: a recent corporate action flag.
pub const RECENT_CORPORATE_ACTION: &str = "recent_corporate_action";

/// Output column: the 1-based rank (nullable).
pub const RANK: &str = "rank";
/// Output column: the composite score.
pub const COMPOSITE_SCORE: &str = "composite_score";
/// Output column prefix: a term's transformed score.
pub const TERM_SCORE_PREFIX: &str = "term_score__";
/// Output column prefix: a term's contribution to the composite.
pub const TERM_CONTRIB_PREFIX: &str = "term_contrib__";
/// Output column prefix: a `target_range` term's distance to the range.
pub const TERM_DISTANCE_PREFIX: &str = "term_distance__";
/// Output column: whether the row is in the scope set.
pub const IN_SCOPE: &str = "in_scope";
/// Output column: whether the row is dropped from the results regardless of filters.
pub const EXCLUDED: &str = "excluded";
/// Output column: why (`""` when not excluded).
pub const EXCLUDED_REASON: &str = "excluded_reason";

/// C4 step 6: a transformed score at or above this is a positive.
pub const POSITIVE_AT: f64 = 0.8;
/// C4 step 6: a transformed score at or below this is a deduction.
pub const DEDUCTION_AT: f64 = 0.2;

const NULL_POLICY_INSUFFICIENT_HISTORY: &str = "insufficient_history";

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/// What the Python raises, by exception type, with its message.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RankingError {
    /// `ValueError`.
    #[error("{0}")]
    Value(String),
    /// `KeyError` (the message is `args[0]`, unquoted).
    #[error("{0}")]
    Key(String),
    /// `TypeError`.
    #[error("{0}")]
    Type(String),
    /// `IndexError` (a row position outside the frame).
    #[error("{0}")]
    Index(String),
}

impl RankingError {
    /// The Python exception class name.
    pub fn python_type(&self) -> &'static str {
        match self {
            Self::Value(_) => "ValueError",
            Self::Key(_) => "KeyError",
            Self::Type(_) => "TypeError",
            Self::Index(_) => "IndexError",
        }
    }

    /// The exception's message.
    pub fn message(&self) -> &str {
        match self {
            Self::Value(m) | Self::Key(m) | Self::Type(m) | Self::Index(m) => m,
        }
    }
}

impl From<polars::error::PolarsError> for RankingError {
    fn from(e: polars::error::PolarsError) -> Self {
        Self::Value(e.to_string())
    }
}

type RResult<T> = Result<T, RankingError>;

// ---------------------------------------------------------------------------
// Specification
// ---------------------------------------------------------------------------

/// `ModeName`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    /// One term, raw values.
    Single,
    /// Raw values in term order.
    Sequential,
    /// Family-weighted mean of term scores.
    Composite,
}

impl Mode {
    /// The Python literal.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Single => "single",
            Self::Sequential => "sequential",
            Self::Composite => "composite",
        }
    }
}

/// `ScopeName`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    /// Rank among the results.
    FilteredResults,
    /// Rank among the whole universe.
    FixedUniverse,
    /// Rank among the universe, per sector.
    WithinSector,
}

impl Scope {
    /// The Python literal.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::FilteredResults => "filtered_results",
            Self::FixedUniverse => "fixed_universe",
            Self::WithinSector => "within_sector",
        }
    }
}

/// `PreferenceName`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Preference {
    /// Higher raw values are better.
    Higher,
    /// Lower raw values are better.
    Lower,
    /// Values inside `[target_min, target_max]` are best.
    TargetRange,
}

impl Preference {
    /// The Python literal.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Higher => "higher",
            Self::Lower => "lower",
            Self::TargetRange => "target_range",
        }
    }
}

/// `MissingPolicy`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MissingPolicy {
    /// A missing raw value scores 0.
    Penalize,
    /// A missing raw value scores 0.5.
    Neutral,
    /// A row missing any term is dropped from the results.
    Exclude,
}

impl MissingPolicy {
    /// The Python literal.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Penalize => "penalize",
            Self::Neutral => "neutral",
            Self::Exclude => "exclude",
        }
    }

    /// `MISSING_SCORE`: the score a missing raw value receives, when the policy gives one.
    pub fn missing_score(self) -> Option<f64> {
        match self {
            Self::Penalize => Some(0.0),
            Self::Neutral => Some(0.5),
            Self::Exclude => None,
        }
    }
}

fn default_weight() -> f64 {
    1.0
}

fn default_missing() -> MissingPolicy {
    MissingPolicy::Penalize
}

/// One ranking term, resolved against the registry (label, family, null policy).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TermSpec {
    /// The factor key (a frame column).
    pub key: String,
    /// Display label.
    pub label: String,
    /// One of [`WEIGHT_FAMILIES`].
    pub weight_family: String,
    /// Which direction is better.
    pub preference: Preference,
    /// The term's weight inside its family (default 1.0).
    #[serde(default = "default_weight")]
    pub weight: f64,
    /// `target_range` lower bound.
    #[serde(default)]
    pub target_min: Option<f64>,
    /// `target_range` upper bound.
    #[serde(default)]
    pub target_max: Option<f64>,
    /// The registry's null policy for the factor.
    #[serde(default)]
    pub null_policy: Option<String>,
}

impl TermSpec {
    /// `TermSpec.__post_init__`: the checks the Python dataclass runs on construction.
    pub fn validate(&self) -> RResult<()> {
        if !WEIGHT_FAMILIES.contains(&self.weight_family.as_str()) {
            return Err(RankingError::Value(format!(
                "{}: unknown weight family {}",
                self.key,
                py_str_repr(&self.weight_family)
            )));
        }
        if !(self.weight.is_finite() && self.weight > 0.0) {
            return Err(RankingError::Value(format!(
                "{}: weight must be a positive finite number",
                self.key
            )));
        }
        if self.preference == Preference::TargetRange {
            if self.target_min.is_none() && self.target_max.is_none() {
                return Err(RankingError::Value(format!(
                    "{}: target_range needs at least one bound",
                    self.key
                )));
            }
            if let (Some(lo), Some(hi)) = (self.target_min, self.target_max)
                && lo > hi
            {
                return Err(RankingError::Value(format!(
                    "{}: target range is inverted",
                    self.key
                )));
            }
        }
        Ok(())
    }
}

/// Everything the engine needs from a `ScreenDefinition`, registry already resolved.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RankingSpec {
    /// The terms, in order.
    pub terms: Vec<TermSpec>,
    /// Combination mode.
    pub mode: Mode,
    /// Scope set.
    pub scope: Scope,
    /// Missing-data policy.
    #[serde(default = "default_missing")]
    pub missing_data: MissingPolicy,
    /// Family → weight, `None` = no opinion. `None` overall = equal.
    #[serde(default)]
    pub family_weights: Option<BTreeMap<String, Option<f64>>>,
}

impl RankingSpec {
    /// `RankingSpec.__post_init__` (each term is validated first, as the Python builds the
    /// `TermSpec`s before the spec).
    pub fn validate(&self) -> RResult<()> {
        for term in &self.terms {
            term.validate()?;
        }
        if self.terms.is_empty() {
            return Err(RankingError::Value(
                "a ranking needs at least one term".to_owned(),
            ));
        }
        let keys: Vec<String> = self.terms.iter().map(|t| t.key.clone()).collect();
        let unique: HashSet<&String> = keys.iter().collect();
        if unique.len() != keys.len() {
            return Err(RankingError::Value(format!(
                "duplicate ranking terms: {}",
                py_list_repr(&keys)
            )));
        }
        if self.mode == Mode::Single && self.terms.len() != 1 {
            return Err(RankingError::Value(
                "single mode ranks by exactly one term".to_owned(),
            ));
        }
        if let Some(weights) = &self.family_weights {
            let unknown: Vec<String> = weights
                .keys()
                .filter(|f| !WEIGHT_FAMILIES.contains(&f.as_str()))
                .cloned()
                .collect();
            if !unknown.is_empty() {
                return Err(RankingError::Value(format!(
                    "unknown weight families: {}",
                    py_list_repr(&unknown)
                )));
            }
            if weights
                .values()
                .any(|w| w.is_some_and(|w| !(w.is_finite() && w >= 0.0)))
            {
                return Err(RankingError::Value(
                    "family weights must be finite and >= 0".to_owned(),
                ));
            }
        }
        Ok(())
    }

    fn family_weight(&self, family: &str) -> Option<f64> {
        self.family_weights
            .as_ref()
            .and_then(|m| m.get(family).copied().flatten())
    }
}

/// Python 3.12's built-in `sum()` over floats: Neumaier's compensated summation, the
/// compensation added at the end only when it is non-zero and finite (`builtin_sum_impl`). A
/// naive left fold differs in the last bit, e.g. `sum([0.1, 0.2, 0.3])` is `0.6`, not
/// `0.6000000000000001`.
fn py_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    let mut total = 0.0_f64;
    let mut c = 0.0_f64;
    for x in values {
        let t = total + x;
        if total.abs() >= x.abs() {
            c += (total - t) + x;
        } else {
            c += (x - t) + total;
        }
        total = t;
    }
    if c != 0.0 && c.is_finite() {
        total += c;
    }
    total
}

/// Each present family's share of the composite, summing to 1 (C4 step 4), in the order the
/// families first appear among the terms.
///
/// A family with no explicit weight gets the mean of the explicit weights of the families
/// present (1.0 when none is explicit). Families with no term get no share at all.
pub fn family_shares(spec: &RankingSpec) -> RResult<Vec<(String, f64)>> {
    let mut present: Vec<&str> = Vec::new();
    for t in &spec.terms {
        if !present.contains(&t.weight_family.as_str()) {
            present.push(&t.weight_family);
        }
    }
    let explicit: Vec<f64> = present
        .iter()
        .filter_map(|f| spec.family_weight(f))
        .collect();
    let default = if explicit.is_empty() {
        1.0
    } else {
        py_sum(explicit.iter().copied()) / as_f64(explicit.len())
    };
    let raw: Vec<(&str, f64)> = present
        .iter()
        .map(|f| (*f, spec.family_weight(f).unwrap_or(default)))
        .collect();
    let total = py_sum(raw.iter().map(|(_, w)| *w));
    if total.is_nan() || total <= 0.0 {
        return Err(RankingError::Value(
            "family_weights give every family used by the terms a zero share; nothing would rank"
                .to_owned(),
        ));
    }
    Ok(raw
        .into_iter()
        .map(|(f, w)| (f.to_owned(), w / total))
        .collect())
}

/// Term key → effective weight = family share x term weight / sum term weights in the family,
/// in term order.
pub fn effective_weights(spec: &RankingSpec) -> RResult<Vec<(String, f64)>> {
    let shares = family_shares(spec)?;
    let mut totals: Vec<(&str, f64)> = Vec::new();
    for term in &spec.terms {
        match totals.iter_mut().find(|(f, _)| *f == term.weight_family) {
            Some((_, w)) => *w += term.weight,
            None => totals.push((&term.weight_family, 0.0 + term.weight)),
        }
    }
    let lookup = |pairs: &[(&str, f64)], f: &str| {
        pairs
            .iter()
            .find(|(k, _)| *k == f)
            .map_or(f64::NAN, |(_, w)| *w)
    };
    let shares_ref: Vec<(&str, f64)> = shares.iter().map(|(k, v)| (k.as_str(), *v)).collect();
    Ok(spec
        .terms
        .iter()
        .map(|t| {
            let share = lookup(&shares_ref, &t.weight_family);
            let total = lookup(&totals, &t.weight_family);
            (t.key.clone(), share * t.weight / total)
        })
        .collect())
}

// ---------------------------------------------------------------------------
// Step 3 — transforms
// ---------------------------------------------------------------------------

fn as_f64(n: usize) -> f64 {
    // Counts and positions are far below 2^53: the conversion is exact.
    #[allow(clippy::cast_precision_loss)]
    let x = n as f64;
    x
}

fn to_nan(values: &[Option<f64>]) -> Vec<f64> {
    values.iter().map(|v| v.unwrap_or(f64::NAN)).collect()
}

fn from_nan(values: &[f64]) -> Vec<Option<f64>> {
    values
        .iter()
        .map(|v| if v.is_nan() { None } else { Some(*v) })
        .collect()
}

fn cmp_f64(a: f64, b: f64) -> std::cmp::Ordering {
    a.partial_cmp(&b).unwrap_or(std::cmp::Ordering::Equal)
}

fn pct(values: &[f64]) -> Vec<f64> {
    let present: Vec<usize> = (0..values.len()).filter(|&i| !values[i].is_nan()).collect();
    let n = present.len();
    let mut out = vec![f64::NAN; values.len()];
    if n == 0 {
        return out;
    }
    if n == 1 {
        for &i in &present {
            out[i] = 1.0;
        }
        return out;
    }
    let mut order = present;
    order.sort_by(|&a, &b| cmp_f64(values[a], values[b]));
    let mut ranks = vec![f64::NAN; values.len()];
    let mut start = 0;
    while start < order.len() {
        let mut end = start;
        let mut sum_ranks = 0.0;
        // `==` on the sorted values, as pandas' rank_1d detects ties (-0.0 == 0.0).
        while end < order.len() && cmp_f64(values[order[end]], values[order[start]]).is_eq() {
            sum_ranks += as_f64(end + 1);
            end += 1;
        }
        let average = sum_ranks / as_f64(end - start);
        for &i in &order[start..end] {
            ranks[i] = average;
        }
        start = end;
    }
    let denominator = as_f64(n - 1);
    for (o, r) in out.iter_mut().zip(&ranks) {
        if !r.is_nan() {
            *o = (r - 1.0) / denominator;
        }
    }
    out
}

/// `(average-tie rank - 1) / (n - 1)` ascending over the non-missing values; `n = 1` ⇒ 1.
///
/// Missing values stay missing and do not count toward `n`.
pub fn percentile(values: &[Option<f64>]) -> Vec<Option<f64>> {
    from_nan(&pct(&to_nan(values)))
}

/// pandas `clip(lower=0.0)`: NaN stays NaN, anything below zero becomes 0.0.
fn clip0(v: f64) -> f64 {
    if v.is_nan() || v >= 0.0 { v } else { 0.0 }
}

fn distance(values: &[f64], lower: Option<f64>, upper: Option<f64>) -> Vec<f64> {
    values
        .iter()
        .map(|&x| {
            if x.is_nan() {
                return f64::NAN;
            }
            let below = lower.map_or(0.0, |lo| clip0(lo - x));
            let above = upper.map_or(0.0, |hi| clip0(x - hi));
            below + above
        })
        .collect()
}

/// 0 inside `[lower, upper]` (either bound optional), else the gap to the nearest bound.
pub fn target_distance(
    values: &[Option<f64>],
    lower: Option<f64>,
    upper: Option<f64>,
) -> Vec<Option<f64>> {
    from_nan(&distance(&to_nan(values), lower, upper))
}

fn tscore(distances: &[f64]) -> Vec<f64> {
    pct(distances)
        .into_iter()
        .zip(distances)
        .map(|(p, &d)| {
            // `distances == 0.0` masks to 1.0 (a NaN distance is not equal to 0).
            if d.is_finite() && d.abs() <= 0.0 {
                1.0
            } else {
                1.0 - p
            }
        })
        .collect()
}

/// Inside the range ⇒ 1.0; outside ⇒ `1 - percentile` of the distance among the group.
pub fn target_score(distances: &[Option<f64>]) -> Vec<Option<f64>> {
    from_nan(&tscore(&to_nan(distances)))
}

fn transform_nan(values: &[f64], term: &TermSpec) -> Vec<f64> {
    match term.preference {
        Preference::Higher => pct(values),
        Preference::Lower => pct(&values.iter().map(|v| -v).collect::<Vec<_>>()),
        Preference::TargetRange => tscore(&distance(values, term.target_min, term.target_max)),
    }
}

/// One term's raw values → scores in `[0, 1]` over exactly the rows passed in.
pub fn transform(values: &[Option<f64>], term: &TermSpec) -> Vec<Option<f64>> {
    from_nan(&transform_nan(&to_nan(values), term))
}

// ---------------------------------------------------------------------------
// Frame access
// ---------------------------------------------------------------------------

/// One cell of a pandas object row, as the Python sees it (`NaN` is null).
#[derive(Debug, Clone, PartialEq)]
enum Cell {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Date(NaiveDate),
}

impl Cell {
    fn from_any(av: AnyValue<'_>) -> RResult<Self> {
        Ok(match av {
            AnyValue::Null => Self::Null,
            AnyValue::Boolean(b) => Self::Bool(b),
            AnyValue::String(s) => Self::Str(s.to_owned()),
            AnyValue::StringOwned(s) => Self::Str(s.to_string()),
            AnyValue::Int8(i) => Self::Int(i64::from(i)),
            AnyValue::Int16(i) => Self::Int(i64::from(i)),
            AnyValue::Int32(i) => Self::Int(i64::from(i)),
            AnyValue::Int64(i) => Self::Int(i),
            AnyValue::UInt8(i) => Self::Int(i64::from(i)),
            AnyValue::UInt16(i) => Self::Int(i64::from(i)),
            AnyValue::UInt32(i) => Self::Int(i64::from(i)),
            AnyValue::UInt64(i) => {
                Self::Int(i64::try_from(i).map_err(|e| RankingError::Value(e.to_string()))?)
            }
            AnyValue::Float32(f) if f.is_nan() => Self::Null,
            AnyValue::Float32(f) => Self::Float(f64::from(f)),
            AnyValue::Float64(f) if f.is_nan() => Self::Null,
            AnyValue::Float64(f) => Self::Float(f),
            AnyValue::Date(days) => Self::Date(
                epoch()
                    .checked_add_signed(chrono::TimeDelta::days(i64::from(days)))
                    .ok_or_else(|| RankingError::Value("date out of range".to_owned()))?,
            ),
            other => {
                return Err(RankingError::Type(format!(
                    "unsupported cell value {other:?}"
                )));
            }
        })
    }

    fn is_null(&self) -> bool {
        matches!(self, Self::Null)
    }

    /// Python `bool(value)`.
    fn truthy(&self) -> bool {
        match self {
            Self::Null => false,
            Self::Bool(b) => *b,
            Self::Int(i) => *i != 0,
            Self::Float(f) => f.abs() > 0.0,
            Self::Str(s) => !s.is_empty(),
            Self::Date(_) => true,
        }
    }

    fn type_name(&self) -> &'static str {
        match self {
            Self::Null => "NoneType",
            Self::Bool(_) => "bool",
            Self::Int(_) => "int",
            Self::Float(_) => "float",
            Self::Str(_) => "str",
            Self::Date(_) => "date",
        }
    }
}

fn epoch() -> NaiveDate {
    NaiveDate::default()
}

fn has(df: &DataFrame, name: &str) -> bool {
    df.get_column_index(name).is_some()
}

fn require(df: &DataFrame, columns: &[&str], why: &str) -> RResult<()> {
    let missing: Vec<String> = columns
        .iter()
        .filter(|c| !has(df, c))
        .map(|c| (*c).to_owned())
        .collect();
    if missing.is_empty() {
        Ok(())
    } else {
        Err(RankingError::Value(format!(
            "ranking frame lacks {}, needed for {why}",
            py_list_repr(&missing)
        )))
    }
}

fn cells(df: &DataFrame, name: &str) -> RResult<Vec<Cell>> {
    let column = df.column(name)?;
    (0..df.height())
        .map(|i| Cell::from_any(column.get(i)?))
        .collect()
}

/// `_numeric`: a column as float64 (`NaN` for missing); anything non-numeric raises.
fn numeric(df: &DataFrame, name: &str) -> RResult<Vec<f64>> {
    let column = df.column(name)?;
    match column.dtype() {
        DataType::String | DataType::Date => {
            let first = cells(df, name)?
                .into_iter()
                .enumerate()
                .find(|(_, c)| !c.is_null());
            match first {
                Some((i, Cell::Str(s))) => Err(RankingError::Value(format!(
                    "Unable to parse string \"{s}\" at position {i}"
                ))),
                Some((_, c)) => Err(RankingError::Type(format!(
                    "Invalid object type at position 0: {}",
                    c.type_name()
                ))),
                None => Ok(vec![f64::NAN; df.height()]),
            }
        }
        _ => {
            let s = column.as_materialized_series().cast(&DataType::Float64)?;
            Ok(s.f64()?.iter().map(|v| v.unwrap_or(f64::NAN)).collect())
        }
    }
}

/// `frame[col].astype("object").fillna(False).astype(bool)`.
fn truth_column(df: &DataFrame, name: &str) -> RResult<Vec<bool>> {
    Ok(cells(df, name)?.iter().map(Cell::truthy).collect())
}

fn put(df: &mut DataFrame, series: Series) -> RResult<()> {
    df.with_column(series.into_column())?;
    Ok(())
}

fn float_series(name: &str, values: &[f64]) -> Series {
    Series::new(name.into(), from_nan(values))
}

// ---------------------------------------------------------------------------
// NSE momentum (baskfy_core.nse_momentum: steps 1, 3, 4, 5)
// ---------------------------------------------------------------------------

mod nse {
    //! The part of `baskfy_core.nse_momentum` the ranking engine calls: eligibility,
    //! population statistics (mean and `ddof=0` standard deviation) and the normalised score.

    use super::as_f64;

    /// Mean and population standard deviation of one horizon's MR.
    #[derive(Debug, Clone, Copy)]
    pub(super) struct HorizonStats {
        pub(super) mean: f64,
        pub(super) std: f64,
    }

    /// Statistics for both horizons.
    #[derive(Debug, Clone, Copy)]
    pub(super) struct Population {
        pub(super) mr6: HorizonStats,
        pub(super) mr12: HorizonStats,
    }

    /// numpy's pairwise summation (`np.add.reduce` on a contiguous float64 array), which is
    /// what pandas' `Series.sum`/`mean`/`var` run when bottleneck is absent.
    pub(super) fn pairwise_sum(a: &[f64]) -> f64 {
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

    /// `horizon_stats`: pandas `mean()` and `std(ddof=0)` (two-pass) of the clean values.
    fn horizon_stats(values: &[f64]) -> Option<HorizonStats> {
        if values.is_empty() {
            return None;
        }
        let count = as_f64(values.len());
        let mean = pairwise_sum(values) / count;
        let sqr: Vec<f64> = values.iter().map(|v| (mean - v).powi(2)).collect();
        let var = pairwise_sum(&sqr) / count;
        Some(HorizonStats {
            mean,
            std: var.sqrt(),
        })
    }

    /// `normalise`: `1 + Z` for `Z ≥ 0`, else `1 / (1 - Z)`.
    fn normalise(z: f64) -> f64 {
        if z >= 0.0 { 1.0 + z } else { 1.0 / (1.0 - z) }
    }

    /// `scores`: the normalised score per row, NaN for every ineligible row.
    pub(super) fn scores(
        mr6: &[f64],
        mr12: &[f64],
        in_nifty_200: &[bool],
        is_fno: &[bool],
        population: Option<Population>,
    ) -> Vec<f64> {
        let eligible: Vec<bool> = (0..mr6.len())
            .map(|i| in_nifty_200[i] && is_fno[i] && !mr6[i].is_nan() && !mr12[i].is_nan())
            .collect();
        let stats = population.or_else(|| {
            let pick = |v: &[f64]| -> Vec<f64> {
                v.iter()
                    .zip(&eligible)
                    .filter(|(x, e)| **e && !x.is_nan())
                    .map(|(x, _)| *x)
                    .collect()
            };
            let six = horizon_stats(&pick(mr6))?;
            let twelve = horizon_stats(&pick(mr12))?;
            Some(Population {
                mr6: six,
                mr12: twelve,
            })
        });
        let mut out = vec![f64::NAN; mr6.len()];
        let Some(stats) = stats else {
            return out;
        };
        if !(stats.mr6.std > 0.0 && stats.mr12.std > 0.0) {
            return out;
        }
        for (i, slot) in out.iter_mut().enumerate() {
            if eligible[i] {
                let z6 = (mr6[i] - stats.mr6.mean) / stats.mr6.std;
                let z12 = (mr12[i] - stats.mr12.mean) / stats.mr12.std;
                *slot = normalise(0.5 * z12 + 0.5 * z6);
            }
        }
        out
    }
}

/// Step 1 for `nse_momentum_score`: NSE's normalised score, NaN for ineligible rows, appended
/// to a copy of `frame`.
///
/// If the frame carries [`NSE_POPULATION_COLUMNS`] (statistics over the whole eligible
/// universe on the date), Z is taken against those (the first row's values); otherwise against
/// the eligible rows present.
pub fn add_nse_momentum_score(frame: &DataFrame) -> RResult<DataFrame> {
    require(
        frame,
        &[NSE_MR6, NSE_MR12, IN_NIFTY_200, IS_FNO],
        NSE_MOMENTUM_KEY,
    )?;
    let mut population = None;
    if NSE_POPULATION_COLUMNS.iter().all(|c| has(frame, c)) && frame.height() > 0 {
        let mut firsts = [f64::NAN; 4];
        for (slot, c) in firsts.iter_mut().zip(NSE_POPULATION_COLUMNS) {
            *slot = numeric(frame, c)?.first().copied().unwrap_or(f64::NAN);
        }
        if !firsts.iter().any(|v| v.is_nan()) {
            population = Some(nse::Population {
                mr6: nse::HorizonStats {
                    mean: firsts[0],
                    std: firsts[1],
                },
                mr12: nse::HorizonStats {
                    mean: firsts[2],
                    std: firsts[3],
                },
            });
        }
    }
    let scores = nse::scores(
        &numeric(frame, NSE_MR6)?,
        &numeric(frame, NSE_MR12)?,
        &truth_column(frame, IN_NIFTY_200)?,
        &truth_column(frame, IS_FNO)?,
        population,
    );
    let mut out = frame.clone();
    put(&mut out, float_series(NSE_MOMENTUM_KEY, &scores))?;
    Ok(out)
}

// ---------------------------------------------------------------------------
// The engine
// ---------------------------------------------------------------------------

/// `ranked` is the output (C4 step 5); `scored` is every input row, for explain.
#[derive(Debug, Clone)]
pub struct RankingResult {
    /// Output rows, best first, `rank` as the first column.
    pub ranked: DataFrame,
    /// Every input row with the engine's columns appended.
    pub scored: DataFrame,
    /// The spec that ranked them.
    pub spec: RankingSpec,
    /// Term key → effective weight, in term order; empty unless the mode is composite.
    pub weights: Vec<(String, f64)>,
}

impl RankingResult {
    fn weight(&self, key: &str) -> Option<f64> {
        self.weights.iter().find(|(k, _)| k == key).map(|(_, w)| *w)
    }
}

/// An ascending sort key that orders raw `values` best-first with missing values last.
fn sort_key(values: &[f64], term: &TermSpec) -> Vec<f64> {
    let key: Vec<f64> = match term.preference {
        Preference::Higher => values.iter().map(|v| -v).collect(),
        Preference::Lower => values.to_vec(),
        Preference::TargetRange => distance(values, term.target_min, term.target_max),
    };
    key.into_iter()
        .map(|k| if k.is_nan() { f64::INFINITY } else { k })
        .collect()
}

/// `float(Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))`.
fn round2(value: f64) -> RResult<f64> {
    // Rust's `Display` for f64 is the shortest round-trip digits, as Python's `repr`.
    let text = format!("{value}");
    let d: Decimal = text
        .parse()
        .map_err(|e| RankingError::Value(format!("cannot quantize {text}: {e}")))?;
    let q = d.round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero);
    q.to_string()
        .parse::<f64>()
        .map_err(|e| RankingError::Value(e.to_string()))
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum IdKey {
    Null,
    Int(i64),
    Float(u64),
    Bool(bool),
    Str(String),
    Date(NaiveDate),
}

fn id_key(c: &Cell) -> IdKey {
    match c {
        Cell::Null => IdKey::Null,
        Cell::Int(i) => IdKey::Int(*i),
        Cell::Float(f) => {
            // An integral float equals the int with that value, as pandas' hashing has it.
            #[allow(clippy::cast_possible_truncation)]
            let i = *f as i64;
            #[allow(clippy::cast_precision_loss)]
            let back = i as f64;
            if back.to_bits() == f.to_bits() {
                IdKey::Int(i)
            } else {
                IdKey::Float(f.to_bits())
            }
        }
        Cell::Bool(b) => IdKey::Bool(*b),
        Cell::Str(s) => IdKey::Str(s.clone()),
        Cell::Date(d) => IdKey::Date(*d),
    }
}

fn cmp_cells(a: &Cell, b: &Cell) -> std::cmp::Ordering {
    use std::cmp::Ordering;
    let num = |c: &Cell| match c {
        Cell::Int(i) => {
            #[allow(clippy::cast_precision_loss)]
            let f = *i as f64;
            Some(f)
        }
        Cell::Float(f) => Some(*f),
        Cell::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    };
    match (a, b) {
        (Cell::Null, Cell::Null) => Ordering::Equal,
        (Cell::Null, _) => Ordering::Greater,
        (_, Cell::Null) => Ordering::Less,
        (Cell::Int(x), Cell::Int(y)) => x.cmp(y),
        (Cell::Str(x), Cell::Str(y)) => x.cmp(y),
        (Cell::Date(x), Cell::Date(y)) => x.cmp(y),
        _ => match (num(a), num(b)) {
            (Some(x), Some(y)) => cmp_f64(x, y),
            _ => Ordering::Equal,
        },
    }
}

fn exclusions(raw: &[(String, Vec<f64>)], spec: &RankingSpec, height: usize) -> Vec<&'static str> {
    let mut reasons = vec![""; height];
    if let Some((_, desk)) = raw.iter().find(|(k, _)| k == DESK_SCORE_KEY) {
        for (r, v) in reasons.iter_mut().zip(desk) {
            if v.is_nan() {
                *r = "desk_score_ineligible";
            }
        }
    }
    if spec.missing_data == MissingPolicy::Exclude {
        for (i, r) in reasons.iter_mut().enumerate() {
            if r.is_empty() && raw.iter().any(|(_, v)| v[i].is_nan()) {
                *r = "missing_data";
            }
        }
    }
    reasons
}

fn groups(scored: &DataFrame, spec: &RankingSpec) -> RResult<Vec<String>> {
    let h = scored.height();
    if spec.scope != Scope::WithinSector {
        return Ok(vec!["all".to_owned(); h]);
    }
    if !has(scored, SECTOR) {
        return Ok(vec![UNCLASSIFIED_SECTOR.to_owned(); h]);
    }
    cells(scored, SECTOR)?
        .into_iter()
        .map(|c| {
            Ok(match c {
                Cell::Null => UNCLASSIFIED_SECTOR.to_owned(),
                Cell::Str(s) => s,
                Cell::Int(i) => i.to_string(),
                Cell::Float(f) => f.to_string(),
                Cell::Bool(b) => if b { "True" } else { "False" }.to_owned(),
                Cell::Date(d) => d.to_string(),
            })
        })
        .collect()
}

/// Run C4 steps 1-5 over `frame` (one row per instrument).
///
/// `spec` must have been validated ([`RankingSpec::validate`]), as the Python dataclass is on
/// construction.
pub fn rank_frame(frame: &DataFrame, spec: &RankingSpec) -> RResult<RankingResult> {
    require(frame, &[INSTRUMENT_ID, PASSES_FILTERS], "ranking")?;
    let ids = cells(frame, INSTRUMENT_ID)?;
    let mut seen = HashSet::new();
    if !ids.iter().all(|c| seen.insert(id_key(c))) {
        return Err(RankingError::Value(
            "ranking frame has duplicate instrument_id rows".to_owned(),
        ));
    }
    let keys: Vec<&str> = spec.terms.iter().map(|t| t.key.as_str()).collect();

    // Step 1: computed factors.
    let mut scored = frame.clone();
    if keys.contains(&NSE_MOMENTUM_KEY) && !has(&scored, NSE_MOMENTUM_KEY) {
        scored = add_nse_momentum_score(&scored)?;
    }
    require(&scored, &keys, "the ranking terms")?;
    let raw: Vec<(String, Vec<f64>)> = keys
        .iter()
        .map(|k| Ok(((*k).to_owned(), numeric(&scored, k)?)))
        .collect::<RResult<_>>()?;
    let passes = truth_column(&scored, PASSES_FILTERS)?;
    let h = scored.height();

    let reasons = exclusions(&raw, spec, h);
    let excluded: Vec<bool> = reasons.iter().map(|r| !r.is_empty()).collect();
    put(&mut scored, Series::new(EXCLUDED.into(), excluded.clone()))?;
    put(
        &mut scored,
        Series::new(
            EXCLUDED_REASON.into(),
            reasons.iter().map(|r| (*r).to_owned()).collect::<Vec<_>>(),
        ),
    )?;

    // Step 2: scope set.
    let in_scope: Vec<bool> = match spec.scope {
        Scope::FilteredResults => (0..h).map(|i| passes[i] && !excluded[i]).collect(),
        _ => vec![true; h],
    };
    put(&mut scored, Series::new(IN_SCOPE.into(), in_scope.clone()))?;
    let group_of = groups(&scored, spec)?;
    if spec.scope == Scope::WithinSector {
        put(&mut scored, Series::new(SECTOR.into(), group_of.clone()))?;
    }

    // Step 3: transforms.
    let mut members: BTreeMap<&str, Vec<usize>> = BTreeMap::new();
    for (i, g) in group_of.iter().enumerate() {
        if in_scope[i] {
            members.entry(g.as_str()).or_default().push(i);
        }
    }
    let mut term_scores: Vec<Vec<f64>> = Vec::with_capacity(spec.terms.len());
    for (term, (_, values)) in spec.terms.iter().zip(&raw) {
        let mut transformed = vec![f64::NAN; h];
        for rows in members.values() {
            let subset: Vec<f64> = rows.iter().map(|&i| values[i]).collect();
            for (&i, v) in rows.iter().zip(transform_nan(&subset, term)) {
                transformed[i] = v;
            }
        }
        if let Some(missing) = spec.missing_data.missing_score() {
            for i in 0..h {
                if in_scope[i] && values[i].is_nan() {
                    transformed[i] = missing;
                }
            }
        }
        put(
            &mut scored,
            float_series(&format!("{TERM_SCORE_PREFIX}{}", term.key), &transformed),
        )?;
        if term.preference == Preference::TargetRange {
            put(
                &mut scored,
                float_series(
                    &format!("{TERM_DISTANCE_PREFIX}{}", term.key),
                    &distance(values, term.target_min, term.target_max),
                ),
            )?;
        }
        term_scores.push(transformed);
    }

    // Step 4: combine.
    let mut sort_keys: Vec<Vec<f64>> = Vec::new();
    let mut weights = Vec::new();
    if spec.mode == Mode::Composite {
        weights = effective_weights(spec)?;
        let mut total = vec![0.0; h];
        for ((term, scores), (_, w)) in spec.terms.iter().zip(&term_scores).zip(&weights) {
            let contribution: Vec<f64> = scores.iter().map(|s| 100.0 * w * s).collect();
            for (t, c) in total.iter_mut().zip(&contribution) {
                *t += c;
            }
            put(
                &mut scored,
                float_series(&format!("{TERM_CONTRIB_PREFIX}{}", term.key), &contribution),
            )?;
        }
        let composite: Vec<f64> = total
            .iter()
            .map(|&v| if v.is_nan() { Ok(f64::NAN) } else { round2(v) })
            .collect::<RResult<_>>()?;
        put(&mut scored, float_series(COMPOSITE_SCORE, &composite))?;
        sort_keys.push(
            composite
                .iter()
                .map(|v| if v.is_nan() { f64::INFINITY } else { -v })
                .collect(),
        );
        if let (Some(first), Some((_, values))) = (spec.terms.first(), raw.first()) {
            sort_keys.push(sort_key(values, first));
        }
    } else {
        for (term, (_, values)) in spec.terms.iter().zip(&raw) {
            sort_keys.push(sort_key(values, term));
        }
    }

    // Step 5: output rows, best first, ties broken by instrument_id (a stable sort).
    let mut order: Vec<usize> = (0..h).filter(|&i| passes[i] && !excluded[i]).collect();
    order.sort_by(|&a, &b| {
        sort_keys
            .iter()
            .map(|k| cmp_f64(k[a], k[b]))
            .find(|o| o.is_ne())
            .unwrap_or_else(|| cmp_cells(&ids[a], &ids[b]))
    });
    let mut rank: Vec<Option<i64>> = vec![None; h];
    for (position, &i) in order.iter().enumerate() {
        rank[i] =
            Some(i64::try_from(position + 1).map_err(|e| RankingError::Value(e.to_string()))?);
    }
    put(&mut scored, Series::new(RANK.into(), rank))?;

    let idx = order
        .iter()
        .map(|&r| IdxSize::try_from(r).map_err(|e| RankingError::Value(e.to_string())))
        .collect::<RResult<Vec<IdxSize>>>()?;
    let taken = scored.take(&IdxCa::from_vec("".into(), idx))?;
    let mut columns: Vec<Column> = Vec::with_capacity(taken.width());
    columns.push(taken.column(RANK)?.clone());
    for c in taken.columns() {
        if c.name().as_str() != RANK {
            columns.push(c.clone());
        }
    }
    let ranked = DataFrame::new(taken.height(), columns)?;
    Ok(RankingResult {
        ranked,
        scored,
        spec: spec.clone(),
        weights,
    })
}

// ---------------------------------------------------------------------------
// Step 6 — explain
// ---------------------------------------------------------------------------

/// One term's part in a row's rank.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TermExplanation {
    /// The factor key.
    pub factor: String,
    /// Display label.
    pub label: String,
    /// Weight family.
    pub weight_family: String,
    /// Preference literal.
    pub preference: String,
    /// The raw value (`None` when missing).
    pub raw: Option<f64>,
    /// The transformed score, when the row is in scope.
    pub transformed: Option<f64>,
    /// The effective weight (composite only).
    pub effective_weight: Option<f64>,
    /// The contribution to the composite, when in scope.
    pub contribution: Option<f64>,
    /// Whether the raw value is missing.
    pub missing: bool,
}

/// One failed eligibility clause.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EligibilityFailure {
    /// The clause (or `desk_score` / `missing_data`).
    pub filter: String,
    /// Human-readable detail.
    pub detail: String,
}

/// Whether a row is a result, and why not.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Eligibility {
    /// Explicit pass and no failure.
    pub passed: bool,
    /// Every failure, in column order then the exclusion.
    pub failures: Vec<EligibilityFailure>,
}

/// Data-quality flags for a row.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DataQuality {
    /// Term keys whose raw value is missing.
    pub missing_factors: Vec<String>,
    /// A missing factor's null policy is `insufficient_history`.
    pub insufficient_history: bool,
    /// The last bar is before `as_of` (or there is none).
    pub stale_price: bool,
    /// A recent corporate action.
    pub recent_corporate_action: bool,
}

/// One stored raw value a desk grade was computed from.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DeskInput {
    /// Input name.
    pub name: String,
    /// Display label.
    pub label: String,
    /// The stored value.
    pub value: Option<f64>,
}

/// One A-F grade: its points, the range `score.score` clips it to, and its stored inputs.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DeskComponent {
    /// Grade letter.
    pub grade: String,
    /// Component key (`a_trend`, …).
    pub key: String,
    /// Display label.
    pub label: String,
    /// Points scored.
    pub points: Option<f64>,
    /// Lowest possible points.
    pub min_points: f64,
    /// Highest possible points.
    pub max_points: f64,
    /// Stored inputs (only `ext_over_20dma`, for A and F).
    pub inputs: Vec<DeskInput>,
}

/// The book's desk score block for a row.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DeskBlock {
    /// The desk score.
    pub score: Option<f64>,
    /// Its rank in the book.
    pub rank: Option<i64>,
    /// A.
    pub a_trend: Option<f64>,
    /// B.
    pub b_momentum: Option<f64>,
    /// C.
    pub c_sharpe: Option<f64>,
    /// D.
    pub d_consistency: Option<f64>,
    /// E.
    pub e_liquidity: Option<f64>,
    /// F.
    pub f_penalty: Option<f64>,
    /// Reject tokens, `;`-separated (`""` when none).
    pub reject: String,
    /// No reject and a score.
    pub eligible: bool,
    /// `ext_over_20dma`.
    pub ext_over_20dma: Option<f64>,
    /// The row's score version.
    pub score_version: Option<String>,
    /// A-F.
    pub components: Vec<DeskComponent>,
}

/// Today's rank beside the previous session's rank for the same definition.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RankHistory {
    /// Today's rank.
    pub today: Option<i64>,
    /// The previous session's rank.
    pub previous: Option<i64>,
    /// The previous session (ISO).
    pub previous_as_of: Option<String>,
    /// `previous - today` when both exist (positive = places gained).
    pub change: Option<i64>,
}

/// The run-level facts an explanation cites.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Provenance {
    /// Universe slug.
    pub universe: String,
    /// `as_of` (ISO).
    pub as_of: String,
    /// Data version.
    pub data_version: Option<i64>,
    /// [`RANKING_ENGINE_VERSION`].
    pub ranking_engine_version: String,
    /// Desk score version.
    pub desk_score_version: Option<String>,
    /// [`NSE_MOMENTUM_VERSION`].
    pub nse_momentum_version: String,
    /// Scope literal.
    pub scope: String,
    /// Mode literal.
    pub mode: String,
}

/// Why one row ranks where it does (C4 step 6).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RankExplanation {
    /// The instrument.
    pub instrument_id: i64,
    /// Its symbol.
    pub symbol: Option<String>,
    /// Its rank.
    pub rank: Option<i64>,
    /// Composite score (composite mode only).
    pub total: Option<f64>,
    /// Per-term detail.
    pub terms: Vec<TermExplanation>,
    /// Positive notes.
    pub positives: Vec<String>,
    /// Deduction notes.
    pub deductions: Vec<String>,
    /// Eligibility.
    pub eligibility: Eligibility,
    /// Data quality.
    pub data_quality: DataQuality,
    /// The desk block, when the book has a row.
    pub desk: Option<DeskBlock>,
    /// Provenance.
    pub provenance: Provenance,
    /// Rank history.
    pub rank_history: RankHistory,
}

/// The run-level facts an explanation cites.
#[derive(Debug, Clone)]
pub struct ExplainContext<'a> {
    /// The ranking run.
    pub result: &'a RankingResult,
    /// Universe slug.
    pub universe: String,
    /// The run's date.
    pub as_of: NaiveDate,
    /// Data version.
    pub data_version: Option<i64>,
    /// Desk score version.
    pub desk_score_version: Option<String>,
    /// `fail__<clause>` suffix → human-readable filter text.
    pub clause_details: BTreeMap<String, String>,
    /// The trading day before `as_of`.
    pub previous_as_of: Option<NaiveDate>,
    /// The row's rank in that day's run.
    pub previous_rank: Option<i64>,
}

struct Row<'a> {
    df: &'a DataFrame,
    i: usize,
}

impl Row<'_> {
    fn has(&self, name: &str) -> bool {
        has(self.df, name)
    }

    /// `row.get(name)`: the cell, or null when the column is absent.
    fn get(&self, name: &str) -> RResult<Cell> {
        if !self.has(name) {
            return Ok(Cell::Null);
        }
        Cell::from_any(self.df.column(name)?.get(self.i)?)
    }

    fn opt_float(&self, name: &str) -> RResult<Option<f64>> {
        opt_float(&self.get(name)?)
    }
}

fn opt_float(c: &Cell) -> RResult<Option<f64>> {
    match c {
        Cell::Null => Ok(None),
        Cell::Bool(b) => Ok(Some(if *b { 1.0 } else { 0.0 })),
        Cell::Int(i) => {
            #[allow(clippy::cast_precision_loss)]
            let f = *i as f64;
            Ok(Some(f))
        }
        Cell::Float(f) => Ok(Some(*f)),
        other => Err(RankingError::Type(format!(
            "expected a number, got {}",
            other.type_name()
        ))),
    }
}

fn opt_int(c: &Cell) -> RResult<Option<i64>> {
    if let Cell::Int(i) = c {
        return Ok(Some(*i));
    }
    Ok(opt_float(c)?.map(|f| {
        // Python `int(float)`: truncation toward zero.
        #[allow(clippy::cast_possible_truncation)]
        let i = f.trunc() as i64;
        i
    }))
}

fn scope_phrase(context: &ExplainContext<'_>, row: &Row<'_>) -> RResult<String> {
    Ok(match context.result.spec.scope {
        Scope::FilteredResults => "the filtered results".to_owned(),
        Scope::FixedUniverse => context.universe.clone(),
        Scope::WithinSector => {
            let sector = if row.has(SECTOR) {
                match row.get(SECTOR)? {
                    Cell::Str(s) => s,
                    Cell::Null => "None".to_owned(),
                    Cell::Int(i) => i.to_string(),
                    Cell::Float(f) => f.to_string(),
                    Cell::Bool(b) => if b { "True" } else { "False" }.to_owned(),
                    Cell::Date(d) => d.to_string(),
                }
            } else {
                UNCLASSIFIED_SECTOR.to_owned()
            };
            format!("sector {sector}")
        }
    })
}

fn explain_terms(row: &Row<'_>, context: &ExplainContext<'_>) -> RResult<Vec<TermExplanation>> {
    let result = context.result;
    let mut out = Vec::with_capacity(result.spec.terms.len());
    for term in &result.spec.terms {
        let raw = row.opt_float(&term.key)?;
        let in_scope = row.get(IN_SCOPE)?.truthy();
        let transformed = if in_scope {
            row.opt_float(&format!("{TERM_SCORE_PREFIX}{}", term.key))?
        } else {
            None
        };
        let contribution = if in_scope {
            row.opt_float(&format!("{TERM_CONTRIB_PREFIX}{}", term.key))?
        } else {
            None
        };
        out.push(TermExplanation {
            factor: term.key.clone(),
            label: term.label.clone(),
            weight_family: term.weight_family.clone(),
            preference: term.preference.as_str().to_owned(),
            raw,
            transformed,
            effective_weight: result.weight(&term.key),
            contribution,
            missing: raw.is_none(),
        });
    }
    Ok(out)
}

fn term_notes(
    row: &Row<'_>,
    context: &ExplainContext<'_>,
    terms: &[TermExplanation],
) -> RResult<(Vec<String>, Vec<String>)> {
    let mut positives = Vec::new();
    let mut deductions = Vec::new();
    let place = scope_phrase(context, row)?;
    let policy = context.result.spec.missing_data.as_str();
    for (explained, spec) in terms.iter().zip(&context.result.spec.terms) {
        if explained.missing {
            deductions.push(format!("Missing {} (missing_data={policy})", spec.label));
            continue;
        }
        if spec.preference == Preference::TargetRange {
            let gap = row.opt_float(&format!("{TERM_DISTANCE_PREFIX}{}", spec.key))?;
            match gap {
                Some(d) if d.abs() <= 0.0 => {
                    positives.push(format!("Inside the target range on {}", spec.label));
                }
                Some(d) => deductions.push(format!(
                    "Outside the target range on {} by {}",
                    spec.label,
                    py_format_g(d, 4)
                )),
                None => {}
            }
            continue;
        }
        let Some(t) = explained.transformed else {
            continue;
        };
        if t >= POSITIVE_AT {
            positives.push(format!("Top 20% on {} within {place}", spec.label));
        } else if t <= DEDUCTION_AT {
            deductions.push(format!("Bottom 20% on {} within {place}", spec.label));
        }
    }
    Ok((positives, deductions))
}

/// Grade letter, frame column and display label, in A-F order.
const DESK_GRADES: [(&str, &str, &str); 6] = [
    ("A", "desk_a_trend", "Trend"),
    ("B", "desk_b_momentum", "Momentum"),
    ("C", "desk_c_sharpe", "Sharpe"),
    ("D", "desk_d_consistency", "Consistency"),
    ("E", "desk_e_liquidity", "Liquidity"),
    ("F", DESK_F_PENALTY, "Penalty"),
];

fn desk_components(row: &Row<'_>) -> RResult<Vec<DeskComponent>> {
    let extension = DeskInput {
        name: "ext_over_20dma".to_owned(),
        label: "Extension over the 20-day moving average, %".to_owned(),
        value: row.opt_float(DESK_EXT_OVER_20DMA)?,
    };
    let mut out = Vec::with_capacity(DESK_GRADES.len());
    for (grade, column, label) in DESK_GRADES {
        let (low, high) = if column == DESK_F_PENALTY {
            (DESK_F_FLOOR, 0.0)
        } else {
            let maximum = DESK_COMPONENTS
                .iter()
                .find(|(c, _, _)| *c == column)
                .map_or(0.0, |(_, _, m)| *m);
            (0.0, maximum)
        };
        out.push(DeskComponent {
            grade: grade.to_owned(),
            key: column.strip_prefix("desk_").unwrap_or(column).to_owned(),
            label: label.to_owned(),
            points: row.opt_float(column)?,
            min_points: low,
            max_points: high,
            inputs: if grade == "A" || grade == "F" {
                vec![extension.clone()]
            } else {
                Vec::new()
            },
        });
    }
    Ok(out)
}

/// `RankHistory` from two ranks; `change` only when both exist.
pub fn rank_history(
    today: Option<i64>,
    previous: Option<i64>,
    previous_as_of: Option<NaiveDate>,
) -> RankHistory {
    let change = match (today, previous) {
        (Some(t), Some(p)) => Some(p - t),
        _ => None,
    };
    RankHistory {
        today,
        previous,
        previous_as_of: previous_as_of.map(|d| d.format("%Y-%m-%d").to_string()),
        change,
    }
}

fn desk_block(row: &Row<'_>) -> RResult<Option<DeskBlock>> {
    if !row.has(DESK_REJECT) {
        return Ok(None);
    }
    let reject_cell = row.get(DESK_REJECT)?;
    let score = row.opt_float(DESK_SCORE_KEY)?;
    if reject_cell.is_null() && score.is_none() {
        // No desk_score_daily row for this instrument on the date.
        return Ok(None);
    }
    let reject = match reject_cell {
        Cell::Str(s) => s,
        _ => String::new(),
    };
    let score_version = match row.get(DESK_ROW_SCORE_VERSION)? {
        Cell::Str(s) => Some(s),
        _ => None,
    };
    Ok(Some(DeskBlock {
        score,
        rank: opt_int(&row.get(DESK_SCORE_RANK)?)?,
        a_trend: row.opt_float("desk_a_trend")?,
        b_momentum: row.opt_float("desk_b_momentum")?,
        c_sharpe: row.opt_float("desk_c_sharpe")?,
        d_consistency: row.opt_float("desk_d_consistency")?,
        e_liquidity: row.opt_float("desk_e_liquidity")?,
        f_penalty: row.opt_float(DESK_F_PENALTY)?,
        eligible: reject.is_empty() && score.is_some(),
        reject,
        ext_over_20dma: row.opt_float(DESK_EXT_OVER_20DMA)?,
        score_version,
        components: desk_components(row)?,
    }))
}

fn desk_notes(row: &Row<'_>, desk: Option<&DeskBlock>) -> RResult<(Vec<String>, Vec<String>)> {
    let Some(desk) = desk else {
        return Ok((Vec::new(), Vec::new()));
    };
    let mut positives = Vec::new();
    let mut deductions = Vec::new();
    for (column, name, maximum) in DESK_COMPONENTS {
        if let Some(value) = row.opt_float(column)?
            && value >= POSITIVE_AT * maximum
        {
            positives.push(format!(
                "Desk {name} {value:.1} of {}",
                py_format_g(maximum, 6)
            ));
        }
    }
    if let Some(f) = desk.f_penalty
        && f < 0.0
    {
        deductions.push(format!("Desk F penalty {f:+.1}"));
    }
    for token in desk.reject.split(';').filter(|t| !t.is_empty()) {
        deductions.push(format!("Desk reject: {token}"));
    }
    Ok((positives, deductions))
}

fn eligibility(row: &Row<'_>, context: &ExplainContext<'_>) -> RResult<Eligibility> {
    let mut failures = Vec::new();
    for column in row.df.columns() {
        let name = column.name().as_str();
        let Some(clause) = name.strip_prefix(FAIL_PREFIX) else {
            continue;
        };
        let cell = row.get(name)?;
        // NULL counts as failed (docs/06: NULL never satisfies a predicate).
        if cell.is_null() || cell.truthy() {
            failures.push(EligibilityFailure {
                filter: clause.to_owned(),
                detail: context
                    .clause_details
                    .get(clause)
                    .cloned()
                    .unwrap_or_else(|| clause.to_owned()),
            });
        }
    }
    let reason = match row.get(EXCLUDED_REASON)? {
        Cell::Str(s) => s,
        _ => String::new(),
    };
    if reason == "desk_score_ineligible" {
        failures.push(EligibilityFailure {
            filter: "desk_score".to_owned(),
            detail: "the book rejected this name or has no score for it".to_owned(),
        });
    } else if reason == "missing_data" {
        let mut missing = Vec::new();
        for t in &context.result.spec.terms {
            if row.get(&t.key)?.is_null() {
                missing.push(t.label.clone());
            }
        }
        failures.push(EligibilityFailure {
            filter: "missing_data".to_owned(),
            detail: format!(
                "missing_data=exclude drops rows missing {}",
                missing.join(", ")
            ),
        });
    }
    // A NULL passes_filters is not a pass: only an explicit true passes.
    let pass_cell = row.get(PASSES_FILTERS)?;
    let passes = !pass_cell.is_null() && pass_cell.truthy();
    Ok(Eligibility {
        passed: passes && failures.is_empty(),
        failures,
    })
}

fn data_quality(
    row: &Row<'_>,
    context: &ExplainContext<'_>,
    terms: &[TermExplanation],
) -> RResult<DataQuality> {
    let missing: Vec<String> = terms
        .iter()
        .filter(|t| t.missing)
        .map(|t| t.factor.clone())
        .collect();
    let insufficient = missing.iter().any(|key| {
        context.result.spec.terms.iter().any(|t| {
            t.key == *key && t.null_policy.as_deref() == Some(NULL_POLICY_INSUFFICIENT_HISTORY)
        })
    });
    let mut stale = false;
    if row.has(LAST_BAR_DATE) {
        // No bar at all is the stalest price there is.
        stale = match row.get(LAST_BAR_DATE)? {
            Cell::Null => true,
            Cell::Date(d) => d < context.as_of,
            Cell::Str(s) => {
                let day = s.get(..10).unwrap_or(&s);
                let parsed = NaiveDate::parse_from_str(day, "%Y-%m-%d")
                    .map_err(|e| RankingError::Value(format!("{s}: {e}")))?;
                parsed < context.as_of
            }
            other => {
                return Err(RankingError::Type(format!(
                    "cannot read a date from {}",
                    other.type_name()
                )));
            }
        };
    }
    let action = row.get(RECENT_CORPORATE_ACTION)?;
    Ok(DataQuality {
        missing_factors: missing,
        insufficient_history: insufficient,
        stale_price: stale,
        recent_corporate_action: !action.is_null() && action.truthy(),
    })
}

/// C4 step 6 for the row at position `row` of `context.result.scored` (ranked or not).
pub fn explain(row: usize, context: &ExplainContext<'_>) -> RResult<RankExplanation> {
    let scored = &context.result.scored;
    if row >= scored.height() {
        return Err(RankingError::Index(
            "single positional indexer is out-of-bounds".to_owned(),
        ));
    }
    let r = Row { df: scored, i: row };
    let spec = &context.result.spec;
    let terms = explain_terms(&r, context)?;
    let (mut positives, mut deductions) = term_notes(&r, context, &terms)?;
    let desk = desk_block(&r)?;
    let (desk_positives, desk_deductions) = desk_notes(&r, desk.as_ref())?;
    positives.extend(desk_positives);
    deductions.extend(desk_deductions);
    // `int(row[INSTRUMENT_ID])`: a missing id in a float column is NaN (ValueError); any other
    // missing id is None (TypeError, Python 3.12's wording).
    let instrument_id = match r.get(INSTRUMENT_ID)? {
        Cell::Int(i) => i,
        Cell::Null if r.df.column(INSTRUMENT_ID)?.dtype().is_float() => {
            return Err(RankingError::Value(
                "cannot convert float NaN to integer".to_owned(),
            ));
        }
        other => opt_int(&other)?.ok_or_else(|| {
            RankingError::Type(
                "int() argument must be a string, a bytes-like object or a real number, \
                 not 'NoneType'"
                    .to_owned(),
            )
        })?,
    };
    let symbol = match r.get(SYMBOL)? {
        Cell::Str(s) => Some(s),
        _ => None,
    };
    let rank = opt_int(&r.get(RANK)?)?;
    let total = if spec.mode == Mode::Composite {
        r.opt_float(COMPOSITE_SCORE)?
    } else {
        None
    };
    Ok(RankExplanation {
        instrument_id,
        symbol,
        rank,
        total,
        eligibility: eligibility(&r, context)?,
        data_quality: data_quality(&r, context, &terms)?,
        terms,
        positives,
        deductions,
        desk,
        provenance: Provenance {
            universe: context.universe.clone(),
            as_of: context.as_of.format("%Y-%m-%d").to_string(),
            data_version: context.data_version,
            ranking_engine_version: RANKING_ENGINE_VERSION.to_owned(),
            desk_score_version: context.desk_score_version.clone(),
            nse_momentum_version: NSE_MOMENTUM_VERSION.to_owned(),
            scope: spec.scope.as_str().to_owned(),
            mode: spec.mode.as_str().to_owned(),
        },
        rank_history: rank_history(rank, context.previous_rank, context.previous_as_of),
    })
}

/// [`explain`] for one `instrument_id` (the first matching row); `KeyError` if it is not in
/// the frame.
pub fn explain_instrument(
    instrument_id: i64,
    context: &ExplainContext<'_>,
) -> RResult<RankExplanation> {
    let ids = cells(&context.result.scored, INSTRUMENT_ID)?;
    #[allow(clippy::cast_precision_loss)]
    let target = instrument_id as f64;
    let position = ids.iter().position(|c| match c {
        Cell::Int(i) => *i == instrument_id,
        Cell::Float(f) => cmp_f64(*f, target).is_eq(),
        _ => false,
    });
    match position {
        Some(p) => explain(p, context),
        None => Err(RankingError::Key(format!(
            "instrument {instrument_id} is not in the ranking frame"
        ))),
    }
}

// ---------------------------------------------------------------------------
// Python text helpers
// ---------------------------------------------------------------------------

/// Python `repr(str)`.
fn py_str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python `repr(list_of_str)`.
fn py_list_repr(items: &[String]) -> String {
    let inner: Vec<String> = items.iter().map(|s| py_str_repr(s)).collect();
    format!("[{}]", inner.join(", "))
}

/// Python `format(x, ".{precision}g")`.
pub fn py_format_g(x: f64, precision: usize) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    let p = precision.max(1);
    if x.abs() <= 0.0 {
        return if x.is_sign_negative() { "-0" } else { "0" }.to_owned();
    }
    let sci = format!("{:.*e}", p - 1, x);
    let (mantissa, exponent) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
    let exp: i64 = exponent.parse().unwrap_or(0);
    let p_i = i64::try_from(p).unwrap_or(i64::MAX);
    if exp >= -4 && exp < p_i {
        let decimals = usize::try_from(p_i - 1 - exp).unwrap_or(0);
        strip_zeros(&format!("{x:.decimals$}"))
    } else {
        let m = strip_zeros(mantissa);
        let sign = if exp < 0 { '-' } else { '+' };
        format!("{m}e{sign}{:02}", exp.abs())
    }
}

fn strip_zeros(s: &str) -> String {
    if s.contains('.') {
        s.trim_end_matches('0').trim_end_matches('.').to_owned()
    } else {
        s.to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::{py_format_g, py_list_repr, py_str_repr, round2};

    #[test]
    fn g_format_matches_python() {
        assert_eq!(py_format_g(6.0, 4), "6");
        assert_eq!(py_format_g(12.5, 4), "12.5");
        assert_eq!(py_format_g(12345.0, 4), "1.234e+04");
        assert_eq!(py_format_g(0.0001234, 4), "0.0001234");
        assert_eq!(py_format_g(0.00001234, 4), "1.234e-05");
        assert_eq!(py_format_g(25.0, 6), "25");
        assert_eq!(py_format_g(1.0 / 3.0, 4), "0.3333");
    }

    #[test]
    fn reprs_match_python() {
        assert_eq!(py_str_repr("vibes"), "'vibes'");
        assert_eq!(py_str_repr("it's"), "\"it's\"");
        assert_eq!(
            py_list_repr(&["a".to_owned(), "b".to_owned()]),
            "['a', 'b']"
        );
    }

    #[test]
    fn round2_is_half_up_on_the_repr() -> Result<(), super::RankingError> {
        assert!((round2(8.333_333_333_333_334)? - 8.33).abs() < 1e-12);
        assert!((round2(0.125)? - 0.13).abs() < 1e-12);
        assert!((round2(2.675)? - 2.68).abs() < 1e-12);
        Ok(())
    }
}
