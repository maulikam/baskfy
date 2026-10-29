//! Port of `baskfy_core.screener`.
//!
//! The pure screener query builder (docs/06-screener-semantics.md). Goldens:
//! `go/testdata/golden/L1/ranking/screener.*`.
//!
//! A validated [`ScreenDefinition`] plus an as-of date becomes **one** parameterised SQL
//! statement, following docs/06 §"Reference SQL skeleton" step for step. Nothing here opens a
//! connection or reads a clock (law 1).
//!
//! The Python builds a SQLAlchemy `Select` and renders it with the `postgresql+asyncpg` dialect;
//! the goldens are that rendered text. This port carries a small statement tree that reproduces
//! the same text byte for byte: compile order (a select's columns, then its FROM — compiling a
//! CTE the first time it is reached — then WHERE, ORDER BY and LIMIT), anonymous bind names
//! (`<key>_<n>`, counted per key in compile order), `$n::TYPE` casts with SQLAlchemy's
//! compared-value type coercion, `__[POSTCOMPILE_<name>]` for expanding `IN` lists, labels on
//! every column inside a CTE or subquery, and a label's name in an ORDER BY when the label is
//! one of the select's own columns.
//!
//! The pieces of `baskfy_core.screen_definition` (the model, its pydantic validation and its
//! canonical JSON), `baskfy_core.universes`, `baskfy_core.models` (column types) and the
//! `ranking`/`ranking_engine` constants the builder reads live here as private modules, because
//! those Python modules have no Rust module of their own.
//!
//! The four departures from the letter of docs/06 the Python documents (no `QUALIFY`, `NULLS
//! LAST` on the marketcap bucket, an `instrument_id` tie-breaker in every `ROW_NUMBER`, only
//! active filters emitted) are reproduced, not re-decided.

use std::collections::HashSet;
use std::fmt;
use std::rc::Rc;

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::Serialize;
use thiserror::Error;

use crate::error::CoreError;
use crate::factor_registry::{
    self, COLUMN_PICKER_KEYS, CUSTOM_FILTER_OPERANDS, RegistryError, py_repr_str,
};

pub use definition::{
    AwayFromHighFilter, CircuitsFilter, CustomFilter, ExtraFactor, FactorRange, FamilyWeights,
    MovingAverageFilter, PeFilter, PositiveDaysFilter, RangeFilter, RankingTerm, ScreenDefinition,
    TopRiskFilter,
};
pub use payload::{DecimalValue, Payload, PyDecimal, py_float_repr};

/// docs/03 §"Request path": "Result rows (<= 4,000)".
pub const MAX_RESULT_ROWS: i64 = 4000;

/// docs/06 §Caching key prefix, shared with the worker's purge.
pub const CACHE_NAMESPACE: &str = "screen:";

/// docs/07 §"Running a screen" — the first three entries of the response's `columns`.
pub const IDENTITY_COLUMNS: [&str; 3] = ["symbol", "name", "sorting_factor"];

/// docs/01 §4 "Default columns", minus the identity ones.
pub const DEFAULT_RESULT_COLUMNS: [&str; 3] = ["ret_12m", "vol_12m", "close_raw"];

/// The four export columns that live on the daily bar rather than on the fact row.
pub const BAR_COLUMNS: [&str; 4] = ["open", "high", "low", "volume_shares"];

/// `baskfy_core.ranking.DESK_SCORE_KEY`.
pub const DESK_SCORE_KEY: &str = "desk_score";

/// `baskfy_core.ranking.DESK_SCORE_INPUT_COLUMNS` — what the desk scorer reads off a fact row.
pub const DESK_SCORE_INPUT_COLUMNS: [&str; 27] = [
    "series",
    "close",
    "marketcap_cr",
    "median_vol_12m",
    "rsi_1m",
    "vol_12m",
    "beta_12m",
    "circuits_3m",
    "circuits_12m",
    "pos_days_3m",
    "pos_days_6m",
    "ma_20",
    "ma_50",
    "ma_100",
    "ma_200",
    "away_high_1y",
    "universe_mask",
    "ret_1m",
    "ret_3m",
    "ret_6m",
    "ret_9m",
    "ret_12m",
    "sharpe_1m",
    "sharpe_3m",
    "sharpe_6m",
    "sharpe_9m",
    "sharpe_12m",
];

/// `baskfy_core.ranking_engine.FAIL_PREFIX`.
pub const FAIL_PREFIX: &str = "fail__";

/// `ranking_engine.NSE_POPULATION_COLUMNS`.
pub const NSE_POPULATION_COLUMNS: [&str; 4] = [
    "nse_mr6_mean",
    "nse_mr6_std",
    "nse_mr12_mean",
    "nse_mr12_std",
];

/// `DESK_FRAME_COLUMNS` — `desk_score_daily` column → the frame column the engine reads.
pub const DESK_FRAME_COLUMNS: [(&str, &str); 11] = [
    ("score", "desk_score"),
    ("score_rank", "desk_score_rank"),
    ("a_trend", "desk_a_trend"),
    ("b_momentum", "desk_b_momentum"),
    ("c_sharpe", "desk_c_sharpe"),
    ("d_consistency", "desk_d_consistency"),
    ("e_liquidity", "desk_e_liquidity"),
    ("f_penalty", "desk_f_penalty"),
    ("ext_over_20dma", "desk_ext_over_20dma"),
    ("reject", "desk_reject"),
    ("score_version", "desk_score_version"),
];

/// The universe whose point-in-time membership is NSE's F&O list (C4).
pub const FNO_UNIVERSE_SLUG: &str = "nifty-fno";

/// `baskfy_core.nse_momentum.NSE_MOMENTUM_INDEX`.
pub const NSE_MOMENTUM_INDEX: &str = "nifty-200";

/// `baskfy_core.universes.DECILE_RANK_KEY` — the bucket is cut by marketcap.
pub const DECILE_RANK_KEY: &str = "marketcap_cr";

/// `baskfy_core.universes.SECTOR_INDEX_SLUGS`.
pub const SECTOR_INDEX_SLUGS: [&str; 15] = [
    "nifty-auto",
    "nifty-bank",
    "nifty-chemicals",
    "nifty-consumer-durables",
    "nifty-financial-services",
    "nifty-fmcg",
    "nifty-healthcare-index",
    "nifty-it",
    "nifty-media",
    "nifty-metal",
    "nifty-oil-gas",
    "nifty-pharma",
    "nifty-private-bank",
    "nifty-psu-bank",
    "nifty-realty",
];

/// `baskfy_core.universes.UNIVERSES` — `(index_id, slug)`; the mask bit is `index_id - 1`.
const UNIVERSES: [(i64, &str); 15] = [
    (1, "nifty-50"),
    (2, "nifty-next-50"),
    (3, "nifty-100"),
    (4, "nifty-200"),
    (5, "nifty-500"),
    (6, "nifty-total-market"),
    (7, "nifty-large-mid-250"),
    (8, "nifty-midcap-150"),
    (9, "nifty-smallcap-250"),
    (10, "nifty-microcap-250"),
    (11, "nifty-mid-small-400"),
    (12, "nifty-allcap"),
    (13, "nifty-fno"),
    (14, "etf"),
    (15, "nse-sme-emerge"),
];

/// One selectable universe: its `index_def.id` and slug.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Universe {
    /// `index_def.id`.
    pub index_id: i64,
    /// The slug.
    pub slug: &'static str,
}

impl Universe {
    /// `1 << (index_id - 1)` — this universe's bit in the `factor_daily` masks.
    pub fn mask_value(&self) -> i64 {
        1_i64 << (self.index_id - 1)
    }
}

/// `UNIVERSE_BY_SLUG[slug]` — a miss is Python's `KeyError` (its message is the slug).
pub fn universe_by_slug(slug: &str) -> Result<Universe, ScreenError> {
    UNIVERSES
        .iter()
        .find(|(_, s)| *s == slug)
        .map(|&(index_id, slug)| Universe { index_id, slug })
        .ok_or_else(|| ScreenError::Key(slug.to_owned()))
}

/// docs/06 §step 3 — a decile bucket's fraction of the universe.
pub fn bucket_fraction(bucket: &str) -> Option<Decimal> {
    match bucket {
        "decile_1" => Some(Decimal::new(10, 2)),
        "decile_2" => Some(Decimal::new(20, 2)),
        "decile_3" => Some(Decimal::new(30, 2)),
        "decile_4" => Some(Decimal::new(40, 2)),
        "decile_5" => Some(Decimal::new(50, 2)),
        _ => None,
    }
}

/// docs/06 §step 3 — `top_50` / `top_100` are row counts.
pub fn bucket_row_limit(bucket: &str) -> Option<i64> {
    match bucket {
        "top_50" => Some(50),
        "top_100" => Some(100),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/// One pydantic error entry: `{"type", "loc", "msg"}` (the URL is left out, as the goldens do).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ErrorDetail {
    /// pydantic's error type (`extra_forbidden`, `value_error`, ...).
    #[serde(rename = "type")]
    pub kind: String,
    /// The location path, every part as a string.
    pub loc: Vec<String>,
    /// The message.
    pub msg: String,
}

/// pydantic's `ValidationError`: the model refused its input.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationError {
    /// Every error, in pydantic's order.
    pub errors: Vec<ErrorDetail>,
}

impl fmt::Display for ValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} validation error(s)", self.errors.len())?;
        for e in &self.errors {
            write!(f, "; {}: {} [{}]", e.loc.join("."), e.msg, e.kind)?;
        }
        Ok(())
    }
}

/// What the screener raises, by the Python exception class it stands for.
#[derive(Debug, Clone, PartialEq, Error)]
pub enum ScreenError {
    /// `ScreenQueryError` — a definition that cannot be turned into SQL.
    #[error("{0}")]
    Query(String),
    /// pydantic `ValidationError` from [`ScreenDefinition::model_validate`].
    #[error("{0}")]
    Validation(ValidationError),
    /// `ValueError`.
    #[error("{0}")]
    Value(String),
    /// `TypeError`.
    #[error("{0}")]
    Type(String),
    /// `KeyError` (the message is `exc.args[0]`).
    #[error("{0}")]
    Key(String),
}

impl ScreenError {
    /// The Python exception class name.
    pub fn python_type(&self) -> &'static str {
        match self {
            ScreenError::Query(_) => "ScreenQueryError",
            ScreenError::Validation(_) => "ValidationError",
            ScreenError::Value(_) => "ValueError",
            ScreenError::Type(_) => "TypeError",
            ScreenError::Key(_) => "KeyError",
        }
    }
}

impl From<RegistryError> for ScreenError {
    fn from(e: RegistryError) -> Self {
        match e {
            RegistryError::UnknownFactor(m) => ScreenError::Key(m),
            RegistryError::Computed(m) => ScreenError::Value(m),
        }
    }
}

impl From<ScreenError> for CoreError {
    fn from(e: ScreenError) -> Self {
        CoreError::InvalidInput(e.to_string())
    }
}

// ---------------------------------------------------------------------------
// Validation — the whitelist gate
// ---------------------------------------------------------------------------

fn require_factor(key: &str, place: &str) -> Result<&'static factor_registry::Factor, ScreenError> {
    factor_registry::get(key).map_err(|_| {
        ScreenError::Query(format!(
            "{place}: {} is not a factor-registry key, so no SQL will be built for it",
            py_repr_str(key)
        ))
    })
}

fn require_operand<'a>(key: &'a str, place: &str) -> Result<&'a str, ScreenError> {
    if !CUSTOM_FILTER_OPERANDS.contains(&key) {
        return Err(ScreenError::Query(format!(
            "{place}: {} is not a custom-filter operand (docs/01 §2.14 lists {})",
            py_repr_str(key),
            CUSTOM_FILTER_OPERANDS.len()
        )));
    }
    Ok(key)
}

/// Reject every user-supplied identifier that is not in the registry.
pub fn validate_definition(definition: &ScreenDefinition) -> Result<(), ScreenError> {
    require_factor(&definition.sort_by, "sort_by")?;
    for (name, extra) in [
        ("factor_two", &definition.factor_two),
        ("factor_three", &definition.factor_three),
    ] {
        if let (true, Some(key)) = (extra.is_active(), extra.sort_by.as_deref()) {
            require_factor(key, &format!("{name}.sort_by"))?;
        }
    }
    for (i, custom) in definition.custom_filters.iter().enumerate() {
        if !custom.is_active() {
            continue;
        }
        let position = i + 1;
        require_operand(&custom.left, &format!("custom_filters[{position}].left"))?;
        require_operand(&custom.right, &format!("custom_filters[{position}].right"))?;
    }
    for (i, term) in definition.ranking_terms.iter().enumerate() {
        require_factor(&term.factor, &format!("ranking_terms[{}].factor", i + 1))?;
    }
    for (i, range) in definition.factor_ranges.iter().enumerate() {
        if range.is_active() {
            require_factor(&range.factor, &format!("factor_ranges[{}].factor", i + 1))?;
        }
    }
    let bucket = definition.apply_filters_on.as_str();
    if bucket != "all" && bucket_fraction(bucket).is_none() && bucket_row_limit(bucket).is_none() {
        return Err(ScreenError::Query(format!(
            "apply_filters_on: unknown bucket {}",
            py_repr_str(bucket)
        )));
    }
    Ok(())
}

/// docs/06 §step 7: identity, then the defaults, then the saved extras in the user's order.
/// Duplicates collapse; an unknown key is refused rather than dropped.
pub fn resolve_columns<S: AsRef<str>>(saved: &[S]) -> Result<Vec<String>, ScreenError> {
    let mut ordered: Vec<String> = IDENTITY_COLUMNS
        .iter()
        .chain(DEFAULT_RESULT_COLUMNS.iter())
        .map(|s| (*s).to_owned())
        .collect();
    let mut seen: HashSet<String> = ordered.iter().cloned().collect();
    for key in saved {
        let key = key.as_ref();
        if seen.contains(key) {
            continue;
        }
        if !COLUMN_PICKER_KEYS.contains(&key) {
            return Err(ScreenError::Query(format!(
                "columns: {} is not one of the {} selectable result columns (docs/01 §4)",
                py_repr_str(key),
                COLUMN_PICKER_KEYS.len()
            )));
        }
        ordered.push(key.to_owned());
        seen.insert(key.to_owned());
    }
    Ok(ordered)
}

// ---------------------------------------------------------------------------
// Query results
// ---------------------------------------------------------------------------

/// One of the up-to-three factors whose ranks are summed (docs/06 §step 5-6).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RankingFactor {
    /// 1-based position.
    pub position: i64,
    /// Registry key.
    pub key: String,
    /// Registry label.
    pub label: String,
    /// `asc` or `desc`.
    pub direction: String,
}

/// A bound value, as SQLAlchemy's compiled `params` holds it.
#[derive(Debug, Clone, Serialize)]
#[serde(untagged)]
pub enum BindValue {
    /// A Python `int`.
    Int(i64),
    /// A Python `float`.
    Float(f64),
    /// A Python `Decimal` (serialised as `format(d, "f")`, scale kept).
    Decimal(DecimalValue),
    /// A date.
    Date(NaiveDate),
    /// An expanding `IN` list of strings.
    StrList(Vec<String>),
}

/// A statement rendered for PostgreSQL (asyncpg dialect): text, bound values, and the bind
/// name behind each position (`$1`, `$2`, ... and the post-compile lists).
#[derive(Debug, Clone, Serialize)]
pub struct CompiledStatement {
    /// The SQL text, values still bound.
    pub sql: String,
    /// Every bind by name, in compile order.
    pub params: Vec<(String, BindValue)>,
    /// `compiled.positiontup`: the bind names in order (post-compile lists included).
    pub param_order: Vec<String>,
}

/// One parameterised statement (opaque; [`Statement::compile`] renders it).
#[derive(Debug, Clone)]
pub struct Statement {
    select: sql::Select,
}

impl Statement {
    /// Render to PostgreSQL text with `$n` placeholders.
    pub fn compile(&self) -> CompiledStatement {
        sql::compile_statement(&self.select)
    }

    /// The rendered text (Python's `ScreenQuery.sql()`).
    pub fn sql(&self) -> String {
        self.compile().sql
    }
}

/// `ScreenQuery` — one statement plus what the caller needs to read its rows.
#[derive(Debug, Clone)]
pub struct ScreenQuery {
    /// The statement.
    pub statement: Statement,
    /// The resolved as-of date.
    pub as_of: NaiveDate,
    /// The universe's `index_def.id`.
    pub index_id: i64,
    /// The universe slug.
    pub universe_slug: String,
    /// Result column names in projection order.
    pub columns: Vec<String>,
    /// Factor one.
    pub sorting_factor: RankingFactor,
    /// Every ranking factor.
    pub ranking_factors: Vec<RankingFactor>,
    /// `ScreenDefinition::definition_hash`.
    pub definition_hash: String,
}

/// `SurvivorsQuery` — filtered survivors for in-process desk SCORE re-ranking.
#[derive(Debug, Clone)]
pub struct SurvivorsQuery {
    /// The statement.
    pub statement: Statement,
    /// The resolved as-of date.
    pub as_of: NaiveDate,
    /// The universe's `index_def.id`.
    pub index_id: i64,
    /// The universe slug.
    pub universe_slug: String,
    /// Result column names.
    pub columns: Vec<String>,
    /// Columns present on each SQL row.
    pub row_columns: Vec<String>,
    /// Factor one.
    pub sorting_factor: RankingFactor,
    /// Every ranking factor.
    pub ranking_factors: Vec<RankingFactor>,
    /// `ScreenDefinition::definition_hash`.
    pub definition_hash: String,
}

/// `RankingFrameQuery` — the ranking engine's input as one statement (C4).
#[derive(Debug, Clone)]
pub struct RankingFrameQuery {
    /// The statement.
    pub statement: Statement,
    /// The resolved as-of date.
    pub as_of: NaiveDate,
    /// The universe's `index_def.id`.
    pub index_id: i64,
    /// The universe slug.
    pub universe_slug: String,
    /// `fail__<name>` suffix → display text, in clause order.
    pub clause_details: Vec<(String, String)>,
    /// Ranking-factor keys projected as expressions because they are not stored columns.
    pub term_columns: Vec<String>,
    /// `ScreenDefinition::definition_hash`.
    pub definition_hash: String,
}

impl RankingFrameQuery {
    /// `fail__<name>` for every clause, in order.
    pub fn fail_columns(&self) -> Vec<String> {
        self.clause_details
            .iter()
            .map(|(name, _)| format!("{FAIL_PREFIX}{name}"))
            .collect()
    }
}

// ---------------------------------------------------------------------------
// Filter clauses
// ---------------------------------------------------------------------------

/// One AND-combined eligibility predicate, named for its `fail__<name>` flag.
struct FilterClause {
    name: String,
    detail: String,
    clause: sql::Expr,
}

fn clause(name: impl Into<String>, detail: impl Into<String>, expr: sql::Expr) -> FilterClause {
    FilterClause {
        name: name.into(),
        detail: detail.into(),
        clause: expr,
    }
}

fn label_of(key: &str) -> String {
    factor_registry::lookup(key)
        .map(|f| f.label.clone())
        .unwrap_or_else(|| key.to_owned())
}

fn scalar_clauses(d: &ScreenDefinition, cols: &sql::Cte) -> Result<Vec<FilterClause>, ScreenError> {
    use sql::{Expr, cmp};
    let mut out = Vec::new();
    if let Some(v) = &d.min_return_1y {
        out.push(clause(
            "min_return_1y",
            format!("{} >= {}", label_of("ret_12m"), v.py_str()),
            cmp(cols.col("ret_12m")?, ">=", BindValue::Decimal(v.clone())),
        ));
    }
    if let Some(v) = d.median_volume_1y {
        out.push(clause(
            "median_volume_1y",
            format!("{} >= {v}", label_of("median_vol_12m")),
            cmp(cols.col("median_vol_12m")?, ">=", BindValue::Int(v)),
        ));
    }
    if let Some(v) = &d.marketcap.from {
        out.push(clause(
            "marketcap_from",
            format!("{} >= {}", label_of("marketcap_cr"), v.py_str()),
            cmp(
                cols.col("marketcap_cr")?,
                ">=",
                BindValue::Decimal(v.clone()),
            ),
        ));
    }
    if let Some(v) = &d.marketcap.to {
        out.push(clause(
            "marketcap_to",
            format!("{} <= {}", label_of("marketcap_cr"), v.py_str()),
            cmp(
                cols.col("marketcap_cr")?,
                "<=",
                BindValue::Decimal(v.clone()),
            ),
        ));
    }
    if d.pe.is_active() {
        out.push(clause(
            "pe_defined",
            "P/E is defined",
            Expr::IsNotNull(Box::new(cols.col("pe")?)),
        ));
        if let Some(v) = &d.pe.from {
            out.push(clause(
                "pe_from",
                format!("P/E >= {}", v.py_str()),
                cmp(cols.col("pe")?, ">=", BindValue::Decimal(v.clone())),
            ));
        }
        if let Some(v) = &d.pe.to {
            out.push(clause(
                "pe_to",
                format!("P/E <= {}", v.py_str()),
                cmp(cols.col("pe")?, "<=", BindValue::Decimal(v.clone())),
            ));
        }
    }
    if !d.series.is_empty() {
        out.push(clause(
            "series",
            format!("series in {}", d.series.join(", ")),
            sql::in_list(cols.col("series")?, d.series.clone()),
        ));
    }
    if d.ignore_above_beta_is_active() {
        out.push(clause(
            "ignore_above_beta",
            format!("{} <= {}", label_of("beta_12m"), d.ignore_above_beta),
            cmp(
                cols.col("beta_12m")?,
                "<=",
                BindValue::Int(d.ignore_above_beta),
            ),
        ));
    }
    if let Some(v) = &d.price.from {
        out.push(clause(
            "price_from",
            format!("{} >= {}", label_of("close_raw"), v.py_str()),
            cmp(cols.col("close_raw")?, ">=", BindValue::Decimal(v.clone())),
        ));
    }
    if let Some(v) = &d.price.to {
        out.push(clause(
            "price_to",
            format!("{} <= {}", label_of("close_raw"), v.py_str()),
            cmp(cols.col("close_raw")?, "<=", BindValue::Decimal(v.clone())),
        ));
    }
    Ok(out)
}

fn moving_average_clauses(
    d: &ScreenDefinition,
    cols: &sql::Cte,
) -> Result<Vec<FilterClause>, ScreenError> {
    let ma = &d.moving_average;
    if !ma.is_active() {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    for length in [200, 100, 50, 20] {
        let (above, below) = ma.switches(length);
        if above {
            out.push(clause(
                format!("ma_above_{length}"),
                format!("close above the {length}-day moving average"),
                sql::cmp_expr(cols.col("close")?, ">", cols.col(&format!("ma_{length}"))?),
            ));
        }
        if below {
            out.push(clause(
                format!("ma_below_{length}"),
                format!("close below the {length}-day moving average"),
                sql::cmp_expr(cols.col("close")?, "<", cols.col(&format!("ma_{length}"))?),
            ));
        }
    }
    Ok(out)
}

fn window_months(window: &str) -> u32 {
    match window {
        "m12" => 12,
        "m9" => 9,
        "m6" => 6,
        "m3" => 3,
        _ => 1,
    }
}

fn windowed_clauses(
    d: &ScreenDefinition,
    cols: &sql::Cte,
) -> Result<Vec<FilterClause>, ScreenError> {
    use sql::{Expr, cmp};
    let mut out = Vec::new();
    let away = &d.away_from_high;
    if away.ath_is_active() {
        out.push(clause(
            "away_from_high_ath",
            format!("within {}% of the all-time high", away.ath),
            cmp(
                Expr::func("abs", vec![cols.col("away_high_ath")?]),
                "<=",
                BindValue::Int(away.ath),
            ),
        ));
    }
    if away.one_year_is_active() {
        out.push(clause(
            "away_from_high_1y",
            format!("within {}% of the 1-year high", away.one_year),
            cmp(
                Expr::func("abs", vec![cols.col("away_high_1y")?]),
                "<=",
                BindValue::Int(away.one_year),
            ),
        ));
    }
    for (window, threshold) in d.positive_days.active_windows() {
        let key = format!("pos_days_{}m", window_months(window));
        out.push(clause(
            format!("positive_days_{window}"),
            format!("{} >= {threshold}", label_of(&key)),
            cmp(cols.col(&key)?, ">=", BindValue::Int(threshold)),
        ));
    }
    for (window, ceiling) in d.circuits.active_windows() {
        let key = format!("circuits_{}m", window_months(window));
        out.push(clause(
            format!("circuits_{window}"),
            format!("{} <= {ceiling}", label_of(&key)),
            cmp(cols.col(&key)?, "<=", BindValue::Int(ceiling)),
        ));
    }
    Ok(out)
}

fn custom_filter_clauses(
    d: &ScreenDefinition,
    cols: &sql::Cte,
) -> Result<Vec<FilterClause>, ScreenError> {
    let mut out = Vec::new();
    for (i, custom) in d.custom_filters.iter().enumerate() {
        if !custom.is_active() {
            continue;
        }
        let position = i + 1;
        let left_key = require_operand(&custom.left, &format!("custom_filters[{position}].left"))?;
        let right_key =
            require_operand(&custom.right, &format!("custom_filters[{position}].right"))?;
        let op = match custom.op.as_str() {
            ">=" => ">=",
            "<=" => "<=",
            _ => "=",
        };
        out.push(clause(
            format!("custom_filter_{position}"),
            format!(
                "{} {} {}",
                label_of(left_key),
                custom.op,
                label_of(right_key)
            ),
            sql::cmp_expr(cols.col(left_key)?, op, cols.col(right_key)?),
        ));
    }
    Ok(out)
}

fn factor_range_clauses(d: &ScreenDefinition) -> Result<Vec<FilterClause>, ScreenError> {
    let mut out = Vec::new();
    for (i, range) in d.factor_ranges.iter().enumerate() {
        if !range.is_active() {
            continue;
        }
        let position = i + 1;
        let key = require_factor(&range.factor, &format!("factor_ranges[{position}].factor"))?
            .key
            .clone();
        let mut bounds = Vec::new();
        let mut text = Vec::new();
        if let Some(min) = range.min {
            bounds.push(sql::cmp(
                factor_expression(&key)?,
                ">=",
                BindValue::Float(min),
            ));
            text.push(format!(">= {}", py_float_repr(min)));
        }
        if let Some(max) = range.max {
            bounds.push(sql::cmp(
                factor_expression(&key)?,
                "<=",
                BindValue::Float(max),
            ));
            text.push(format!("<= {}", py_float_repr(max)));
        }
        let expr = if bounds.len() == 1 {
            bounds.remove(0)
        } else {
            sql::Expr::And(bounds)
        };
        out.push(clause(
            format!("factor_range_{position}_{key}"),
            format!("{} {}", label_of(&key), text.join(" and ")),
            expr,
        ));
    }
    Ok(out)
}

fn regime_clauses(d: &ScreenDefinition, cols: &sql::Cte) -> Result<Vec<FilterClause>, ScreenError> {
    let Some(regimes) = &d.regime_in else {
        return Ok(Vec::new());
    };
    Ok(vec![clause(
        "regime_in",
        format!("regime in {}", regimes.join(", ")),
        sql::in_list(cols.col("regime")?, regimes.clone()),
    )])
}

/// docs/06 §step 4's table in the document's order, then C3's factor ranges and regime.
fn named_filter_clauses(
    d: &ScreenDefinition,
    cols: &sql::Cte,
) -> Result<Vec<FilterClause>, ScreenError> {
    let mut out = scalar_clauses(d, cols)?;
    out.extend(moving_average_clauses(d, cols)?);
    out.extend(windowed_clauses(d, cols)?);
    out.extend(custom_filter_clauses(d, cols)?);
    out.extend(factor_range_clauses(d)?);
    out.extend(regime_clauses(d, cols)?);
    Ok(out)
}

fn filter_clauses(d: &ScreenDefinition, cols: &sql::Cte) -> Result<Vec<sql::Expr>, ScreenError> {
    Ok(named_filter_clauses(d, cols)?
        .into_iter()
        .map(|c| c.clause)
        .collect())
}

/// docs/06 §step 4 ⚠ — the precomputed per-universe top-beta / top-volatility flags.
fn named_risk_clauses(
    d: &ScreenDefinition,
    cols: &sql::Cte,
    mask_bit: i64,
) -> Result<Vec<FilterClause>, ScreenError> {
    let mut out = Vec::new();
    if d.ignore_top_beta.is_active() {
        out.push(clause(
            "ignore_top_beta",
            "not in the universe's top-beta cut",
            sql::cmp(
                sql::bit_and(cols.col("top_beta_mask")?, mask_bit),
                "=",
                BindValue::Int(0),
            ),
        ));
    }
    if d.ignore_top_volatility.is_active() {
        out.push(clause(
            "ignore_top_volatility",
            "not in the universe's top-volatility cut",
            sql::cmp(
                sql::bit_and(cols.col("top_volatility_mask")?, mask_bit),
                "=",
                BindValue::Int(0),
            ),
        ));
    }
    Ok(out)
}

fn risk_clauses(
    d: &ScreenDefinition,
    cols: &sql::Cte,
    mask_bit: i64,
) -> Result<Vec<sql::Expr>, ScreenError> {
    Ok(named_risk_clauses(d, cols, mask_bit)?
        .into_iter()
        .map(|c| c.clause)
        .collect())
}

/// The registry's hand-written SQL for `key`, as an unqualified expression.
fn factor_expression(key: &str) -> Result<sql::Expr, ScreenError> {
    if key == DESK_SCORE_KEY {
        return Ok(sql::Expr::Literal(DESK_SCORE_KEY.to_owned()));
    }
    Ok(sql::Expr::Literal(
        factor_registry::sql_for(key)?.to_owned(),
    ))
}

fn ranking_factors_of(d: &ScreenDefinition) -> Result<Vec<RankingFactor>, ScreenError> {
    d.ranking_factors()
        .into_iter()
        .enumerate()
        .map(|(i, (key, direction))| {
            Ok(RankingFactor {
                position: i64::try_from(i + 1).unwrap_or(i64::MAX),
                label: factor_registry::get(&key)?.label.clone(),
                key,
                direction,
            })
        })
        .collect()
}

// ---------------------------------------------------------------------------
// The pipeline
// ---------------------------------------------------------------------------

fn membership_cte(
    index_id: i64,
    as_of: NaiveDate,
    name: &str,
) -> Result<Rc<sql::Cte>, ScreenError> {
    let imd = &tables::INDEX_MEMBER_DAILY;
    let select = sql::Select::new(
        vec![imd.col("instrument_id")?],
        vec![sql::FromItem::Table(imd)],
    )
    .filter(vec![
        sql::cmp(imd.col("index_id")?, "=", BindValue::Int(index_id)),
        sql::cmp(imd.col("date")?, "=", BindValue::Date(as_of)),
    ]);
    Ok(sql::Cte::new(name, select))
}

fn universe_cte(universe: Universe, as_of: NaiveDate) -> Result<Rc<sql::Cte>, ScreenError> {
    membership_cte(universe.index_id, as_of, "universe")
}

fn desk_scored(selected: &Rc<sql::Cte>, as_of: NaiveDate) -> Result<Rc<sql::Cte>, ScreenError> {
    let desk = &tables::DESK_SCORE_DAILY;
    let mut items = selected.all();
    items.push(sql::Expr::label(DESK_SCORE_KEY, desk.col("score")?));
    let select = sql::Select::new(
        items,
        vec![sql::FromItem::join(
            sql::FromItem::Cte(Rc::clone(selected)),
            sql::FromItem::Table(desk),
            sql::Expr::And(vec![
                sql::cmp_expr(
                    desk.col("instrument_id")?,
                    "=",
                    selected.col("instrument_id")?,
                ),
                sql::cmp(desk.col("date")?, "=", BindValue::Date(as_of)),
            ]),
            false,
        )],
    )
    .filter(vec![sql::Expr::IsNotNull(Box::new(desk.col("score")?))]);
    Ok(sql::Cte::new("desk_scored", select))
}

fn bucketed_universe(
    d: &ScreenDefinition,
    universe: &Rc<sql::Cte>,
    as_of: NaiveDate,
) -> Result<Rc<sql::Cte>, ScreenError> {
    let facts = &tables::FACTOR_DAILY;
    let fraction = bucket_fraction(&d.apply_filters_on);
    let row_limit = bucket_row_limit(&d.apply_filters_on);
    let marketcap_order = sql::Order::desc_nulls_last(facts.col(DECILE_RANK_KEY)?);
    let mut items = facts.all();
    let window_name = if fraction.is_some() {
        items.push(sql::Expr::label(
            "bucket_percent_rank",
            sql::Expr::over("percent_rank", Vec::new(), vec![marketcap_order]),
        ));
        Some("bucket_percent_rank")
    } else if row_limit.is_some() {
        items.push(sql::Expr::label(
            "bucket_row_number",
            sql::Expr::over("row_number", Vec::new(), vec![marketcap_order]),
        ));
        Some("bucket_row_number")
    } else {
        None
    };
    let bucketed = sql::Cte::new(
        "bucketed",
        sql::Select::new(
            items,
            vec![sql::FromItem::join(
                sql::FromItem::Table(facts),
                sql::FromItem::Cte(Rc::clone(universe)),
                sql::cmp_expr(
                    facts.col("instrument_id")?,
                    "=",
                    universe.col("instrument_id")?,
                ),
                false,
            )],
        )
        .filter(vec![sql::cmp(
            facts.col("date")?,
            "=",
            BindValue::Date(as_of),
        )]),
    );
    let Some(window_name) = window_name else {
        return Ok(bucketed);
    };
    let kept = facts
        .columns
        .iter()
        .map(|(name, _)| bucketed.col(name))
        .collect::<Result<Vec<_>, _>>()?;
    let cut = match (fraction, row_limit) {
        (Some(f), _) => sql::cmp(
            bucketed.col(window_name)?,
            "<=",
            BindValue::Decimal(f.into()),
        ),
        (None, Some(n)) => sql::cmp(bucketed.col(window_name)?, "<=", BindValue::Int(n)),
        (None, None) => return Ok(bucketed),
    };
    Ok(sql::Cte::new(
        "selected",
        sql::Select::new(kept, vec![sql::FromItem::Cte(Rc::clone(&bucketed))]).filter(vec![cut]),
    ))
}

/// Steps 1-6 of docs/06, ready to be projected.
struct RankedPipeline {
    ranked: Rc<sql::Cte>,
    combined: sql::Expr,
    order_by: Vec<sql::Order>,
    ranking: Vec<RankingFactor>,
}

fn sequential_value_name(position: i64) -> String {
    format!("seq_value_{position}")
}

fn sequential_value_columns(ranking: &[RankingFactor]) -> Result<Vec<sql::Expr>, ScreenError> {
    ranking
        .iter()
        .map(|f| {
            Ok(sql::Expr::label(
                &sequential_value_name(f.position),
                factor_expression(&f.key)?,
            ))
        })
        .collect()
}

fn rank_columns(
    ranking: &[RankingFactor],
    source: &sql::Cte,
) -> Result<Vec<sql::Expr>, ScreenError> {
    let mut out = Vec::new();
    for slot in 1..=3_usize {
        let label = format!("r{slot}");
        let Some(factor) = ranking.get(slot - 1) else {
            out.push(sql::Expr::label(
                &label,
                sql::Expr::literal_bind(BindValue::Int(0)),
            ));
            continue;
        };
        let expression = factor_expression(&factor.key)?;
        let ordering = sql::Order {
            expr: expression,
            desc: factor.direction == "desc",
            nulls_last: true,
        };
        out.push(sql::Expr::label(
            &label,
            sql::Expr::over(
                "row_number",
                Vec::new(),
                vec![ordering, sql::Order::asc(source.col("instrument_id")?)],
            ),
        ));
    }
    Ok(out)
}

fn ranked_pipeline(d: &ScreenDefinition, as_of: NaiveDate) -> Result<RankedPipeline, ScreenError> {
    let universe = universe_by_slug(&d.index)?;
    let universe_cte = universe_cte(universe, as_of)?;
    let mut selected = bucketed_universe(d, &universe_cte, as_of)?;
    if d.sort_by == DESK_SCORE_KEY {
        selected = desk_scored(&selected, as_of)?;
    }
    let ranking = ranking_factors_of(d)?;
    let first = ranking
        .first()
        .ok_or_else(|| ScreenError::Value("no ranking factor".to_owned()))?;
    let value_columns = if d.ranking_mode == "sequential" {
        sequential_value_columns(&ranking)?
    } else {
        Vec::new()
    };

    let ranked = if d.ranking_scope == "fixed_universe" {
        let mut items = selected.all();
        items.push(sql::Expr::label(
            "sorting_factor",
            factor_expression(&first.key)?,
        ));
        items.extend(rank_columns(&ranking, &selected)?);
        items.extend(value_columns);
        let pre_ranked = sql::Cte::new(
            "pre_ranked",
            sql::Select::new(items, vec![sql::FromItem::Cte(Rc::clone(&selected))]),
        );
        let filtered = sql::Cte::new(
            "filtered",
            sql::Select::new(
                pre_ranked.all(),
                vec![sql::FromItem::Cte(Rc::clone(&pre_ranked))],
            )
            .filter(filter_clauses(d, &pre_ranked)?),
        );
        sql::Cte::new(
            "ranked",
            sql::Select::new(
                filtered.all(),
                vec![sql::FromItem::Cte(Rc::clone(&filtered))],
            )
            .filter(risk_clauses(d, &filtered, universe.mask_value())?),
        )
    } else {
        let filtered = sql::Cte::new(
            "filtered",
            sql::Select::new(
                selected.all(),
                vec![sql::FromItem::Cte(Rc::clone(&selected))],
            )
            .filter(filter_clauses(d, &selected)?),
        );
        let relative = sql::Cte::new(
            "relative",
            sql::Select::new(
                filtered.all(),
                vec![sql::FromItem::Cte(Rc::clone(&filtered))],
            )
            .filter(risk_clauses(d, &filtered, universe.mask_value())?),
        );
        let mut items = relative.all();
        items.push(sql::Expr::label(
            "sorting_factor",
            factor_expression(&first.key)?,
        ));
        items.extend(rank_columns(&ranking, &relative)?);
        items.extend(value_columns);
        sql::Cte::new(
            "ranked",
            sql::Select::new(items, vec![sql::FromItem::Cte(Rc::clone(&relative))]),
        )
    };

    let combined = sql::Expr::label(
        "combined_rank",
        sql::Expr::binary(
            sql::Expr::binary(ranked.col("r1")?, "+", ranked.col("r2")?),
            "+",
            ranked.col("r3")?,
        ),
    );
    let order_by = if d.ranking_mode == "sequential" {
        let mut order = ranking
            .iter()
            .map(|f| {
                Ok(sql::Order {
                    expr: ranked.col(&sequential_value_name(f.position))?,
                    desc: f.direction == "desc",
                    nulls_last: true,
                })
            })
            .collect::<Result<Vec<_>, ScreenError>>()?;
        order.push(sql::Order::asc(ranked.col("instrument_id")?));
        order
    } else {
        vec![
            sql::Order::asc(combined.clone()),
            sql::Order::asc(ranked.col("r1")?),
            sql::Order::asc(ranked.col("instrument_id")?),
        ]
    };
    Ok(RankedPipeline {
        ranked,
        combined,
        order_by,
        ranking,
    })
}

/// The legacy statement ranks docs/06's way; refuse what it cannot rank rather than misrank.
fn refuse_outside_legacy_sql(d: &ScreenDefinition, what: &str) -> Result<(), ScreenError> {
    if !d.ranking_terms.is_empty() {
        return Err(ScreenError::Query(format!(
            "{what}: ranking_terms are ranked by baskfy_core.ranking_engine over build_ranking_frame_query, not by the legacy SQL ranking"
        )));
    }
    for (key, _direction) in d.ranking_factors() {
        if factor_registry::get(&key)?.is_computed && key != DESK_SCORE_KEY {
            return Err(ScreenError::Query(format!(
                "{what}: {} is computed by the ranking engine and cannot be ranked in SQL; use ranking_terms",
                py_repr_str(&key)
            )));
        }
    }
    Ok(())
}

fn instrument_join(ranked: &Rc<sql::Cte>) -> Result<sql::FromItem, ScreenError> {
    let instrument = &tables::INSTRUMENT;
    Ok(sql::FromItem::join(
        sql::FromItem::Cte(Rc::clone(ranked)),
        sql::FromItem::Table(instrument),
        sql::cmp_expr(instrument.col("id")?, "=", ranked.col("instrument_id")?),
        false,
    ))
}

/// docs/06's seven-step pipeline as one statement. `as_of` must already be resolved.
pub fn build_screen_query<S: AsRef<str>>(
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    columns: &[S],
    limit: i64,
) -> Result<ScreenQuery, ScreenError> {
    validate_definition(definition)?;
    refuse_outside_legacy_sql(definition, "build_screen_query")?;
    let projection = resolve_columns(columns)?;
    let universe = universe_by_slug(&definition.index)?;
    let pipeline = ranked_pipeline(definition, as_of)?;
    let ranked = &pipeline.ranked;
    let instrument = &tables::INSTRUMENT;

    let mut items = vec![
        sql::Expr::label(
            "rank",
            sql::Expr::over("row_number", Vec::new(), pipeline.order_by.clone()),
        ),
        ranked.col("instrument_id")?,
        pipeline.combined.clone(),
        ranked.col("r1")?,
        ranked.col("r2")?,
        ranked.col("r3")?,
    ];
    for name in &projection {
        items.push(match name.as_str() {
            "symbol" => sql::Expr::label("symbol", instrument.col("symbol")?),
            "name" => sql::Expr::label("name", instrument.col("name")?),
            other => sql::Expr::label(other, ranked.col(other)?),
        });
    }
    let select = sql::Select::new(items, vec![instrument_join(ranked)?])
        .order(pipeline.order_by.clone())
        .limit(limit);

    let sorting_factor = pipeline
        .ranking
        .first()
        .cloned()
        .ok_or_else(|| ScreenError::Value("no ranking factor".to_owned()))?;
    Ok(ScreenQuery {
        statement: Statement { select },
        as_of,
        index_id: universe.index_id,
        universe_slug: universe.slug.to_owned(),
        columns: projection,
        sorting_factor,
        ranking_factors: pipeline.ranking,
        definition_hash: definition.definition_hash(),
    })
}

/// Filters + risk only — rows for the in-process desk SCORE re-rank, `instrument_id ASC`.
pub fn build_survivors_query<S: AsRef<str>>(
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    columns: &[S],
    limit: i64,
) -> Result<SurvivorsQuery, ScreenError> {
    validate_definition(definition)?;
    if definition.sort_by != DESK_SCORE_KEY {
        return Err(ScreenError::Query(format!(
            "build_survivors_query requires sort_by={}; got {}",
            py_repr_str(DESK_SCORE_KEY),
            py_repr_str(&definition.sort_by)
        )));
    }
    let projection = resolve_columns(columns)?;
    let universe = universe_by_slug(&definition.index)?;
    let universe_cte = universe_cte(universe, as_of)?;
    let selected = bucketed_universe(definition, &universe_cte, as_of)?;
    let filtered = sql::Cte::new(
        "filtered",
        sql::Select::new(
            selected.all(),
            vec![sql::FromItem::Cte(Rc::clone(&selected))],
        )
        .filter(filter_clauses(definition, &selected)?),
    );
    let survivors = sql::Cte::new(
        "survivors",
        sql::Select::new(
            filtered.all(),
            vec![sql::FromItem::Cte(Rc::clone(&filtered))],
        )
        .filter(risk_clauses(definition, &filtered, universe.mask_value())?),
    );

    let ranking = ranking_factors_of(definition)?;
    let mut needed: Vec<String> = Vec::new();
    let candidates = std::iter::once("instrument_id")
        .chain(DESK_SCORE_INPUT_COLUMNS.iter().copied())
        .chain(
            projection
                .iter()
                .map(String::as_str)
                .filter(|n| !["symbol", "name", "sorting_factor"].contains(n)),
        );
    for name in candidates {
        if !needed.iter().any(|n| n == name) {
            needed.push(name.to_owned());
        }
    }
    for name in &needed {
        if !survivors.has(name) {
            return Err(ScreenError::Query(format!(
                "survivors CTE is missing column {} required for desk_score",
                py_repr_str(name)
            )));
        }
    }

    let instrument = &tables::INSTRUMENT;
    let mut items = vec![
        survivors.col("instrument_id")?,
        sql::Expr::label("symbol", instrument.col("symbol")?),
        sql::Expr::label("name", instrument.col("name")?),
    ];
    for name in needed.iter().filter(|n| *n != "instrument_id") {
        items.push(sql::Expr::label(name, survivors.col(name)?));
    }
    let mut row_columns = vec![
        "instrument_id".to_owned(),
        "symbol".to_owned(),
        "name".to_owned(),
    ];
    row_columns.extend(needed.iter().filter(|n| *n != "instrument_id").cloned());

    let select = sql::Select::new(items, vec![instrument_join(&survivors)?])
        .order(vec![sql::Order::asc(survivors.col("instrument_id")?)])
        .limit(limit);
    let sorting_factor = ranking
        .first()
        .cloned()
        .ok_or_else(|| ScreenError::Value("no ranking factor".to_owned()))?;
    Ok(SurvivorsQuery {
        statement: Statement { select },
        as_of,
        index_id: universe.index_id,
        universe_slug: universe.slug.to_owned(),
        columns: projection,
        row_columns,
        sorting_factor,
        ranking_factors: ranking,
        definition_hash: definition.definition_hash(),
    })
}

fn sector_cte(as_of: NaiveDate) -> Result<Rc<sql::Cte>, ScreenError> {
    let imd = &tables::INDEX_MEMBER_DAILY;
    let index_def = &tables::INDEX_DEF;
    let members = sql::Cte::new(
        "sector_members",
        sql::Select::new(
            vec![
                imd.col("instrument_id")?,
                index_def.col("slug")?,
                sql::Expr::label(
                    "members",
                    sql::Expr::Over {
                        func: Box::new(sql::Expr::CountStar),
                        partition_by: vec![imd.col("index_id")?],
                        order_by: Vec::new(),
                    },
                ),
            ],
            vec![sql::FromItem::join(
                sql::FromItem::Table(imd),
                sql::FromItem::Table(index_def),
                sql::cmp_expr(index_def.col("id")?, "=", imd.col("index_id")?),
                false,
            )],
        )
        .filter(vec![
            sql::cmp(imd.col("date")?, "=", BindValue::Date(as_of)),
            sql::in_list(
                index_def.col("slug")?,
                SECTOR_INDEX_SLUGS.iter().map(|s| (*s).to_owned()).collect(),
            ),
        ]),
    );
    let ordered = sql::Cte::new(
        "sector_ordered",
        sql::Select::new(
            vec![
                members.col("instrument_id")?,
                members.col("slug")?,
                sql::Expr::label(
                    "sector_order",
                    sql::Expr::over(
                        "row_number",
                        vec![members.col("instrument_id")?],
                        vec![
                            sql::Order::asc(members.col("members")?),
                            sql::Order::asc(members.col("slug")?),
                        ],
                    ),
                ),
            ],
            vec![sql::FromItem::Cte(Rc::clone(&members))],
        ),
    );
    Ok(sql::Cte::new(
        "sector",
        sql::Select::new(
            vec![ordered.col("instrument_id")?, ordered.col("slug")?],
            vec![sql::FromItem::Cte(Rc::clone(&ordered))],
        )
        .filter(vec![sql::cmp(
            ordered.col("sector_order")?,
            "=",
            BindValue::Int(1),
        )]),
    ))
}

fn nse_population_cte(
    as_of: NaiveDate,
    nifty_200: &Rc<sql::Cte>,
    fno: &Rc<sql::Cte>,
) -> Result<Rc<sql::Cte>, ScreenError> {
    let facts = &tables::FACTOR_DAILY;
    let eligible = sql::Cte::new(
        "nse_eligible",
        sql::Select::new(
            vec![facts.col("nse_mr6")?, facts.col("nse_mr12")?],
            vec![sql::FromItem::join(
                sql::FromItem::join(
                    sql::FromItem::Table(facts),
                    sql::FromItem::Cte(Rc::clone(nifty_200)),
                    sql::cmp_expr(
                        nifty_200.col("instrument_id")?,
                        "=",
                        facts.col("instrument_id")?,
                    ),
                    false,
                ),
                sql::FromItem::Cte(Rc::clone(fno)),
                sql::cmp_expr(fno.col("instrument_id")?, "=", facts.col("instrument_id")?),
                false,
            )],
        )
        .filter(vec![
            sql::cmp(facts.col("date")?, "=", BindValue::Date(as_of)),
            sql::Expr::IsNotNull(Box::new(facts.col("nse_mr6")?)),
            sql::Expr::IsNotNull(Box::new(facts.col("nse_mr12")?)),
        ]),
    );
    let [six_mean, six_std, twelve_mean, twelve_std] = NSE_POPULATION_COLUMNS;
    Ok(sql::Cte::new(
        "nse_population",
        sql::Select::new(
            vec![
                sql::Expr::label(
                    six_mean,
                    sql::Expr::func("avg", vec![eligible.col("nse_mr6")?]),
                ),
                sql::Expr::label(
                    six_std,
                    sql::Expr::func("stddev_pop", vec![eligible.col("nse_mr6")?]),
                ),
                sql::Expr::label(
                    twelve_mean,
                    sql::Expr::func("avg", vec![eligible.col("nse_mr12")?]),
                ),
                sql::Expr::label(
                    twelve_std,
                    sql::Expr::func("stddev_pop", vec![eligible.col("nse_mr12")?]),
                ),
            ],
            vec![sql::FromItem::Cte(Rc::clone(&eligible))],
        ),
    ))
}

fn frame_term_keys(d: &ScreenDefinition) -> Vec<String> {
    let keys: Vec<String> = if d.ranking_terms.is_empty() {
        d.ranking_factors().into_iter().map(|(k, _)| k).collect()
    } else {
        d.ranking_terms.iter().map(|t| t.factor.clone()).collect()
    };
    let mut out: Vec<String> = Vec::new();
    for key in keys {
        if !out.contains(&key) {
            out.push(key);
        }
    }
    out
}

/// The pre-filter frame the ranking engine ranks (C4 "Input"): every `factor_daily` column,
/// the expression terms, one `fail__<clause>` flag per active clause, `passes_filters`, the
/// sector, the desk row, the NSE inputs and the last bar date. No LIMIT.
pub fn build_ranking_frame_query(
    definition: &ScreenDefinition,
    as_of: NaiveDate,
) -> Result<RankingFrameQuery, ScreenError> {
    validate_definition(definition)?;
    let universe = universe_by_slug(&definition.index)?;
    let universe_cte = universe_cte(universe, as_of)?;
    let selected = bucketed_universe(definition, &universe_cte, as_of)?;

    let mut clauses = named_filter_clauses(definition, &selected)?;
    clauses.extend(named_risk_clauses(
        definition,
        &selected,
        universe.mask_value(),
    )?);
    let mut term_columns = Vec::new();
    for key in frame_term_keys(definition) {
        if !selected.has(&key) && !factor_registry::get(&key)?.is_computed {
            term_columns.push(key);
        }
    }
    let mut items = selected.all();
    for key in &term_columns {
        items.push(sql::Expr::label(key, factor_expression(key)?));
    }
    let mut clause_details = Vec::new();
    for named in clauses {
        items.push(sql::Expr::label(
            &format!("{FAIL_PREFIX}{}", named.name),
            sql::Expr::case(named.clause, sql::Expr::Bool(false), sql::Expr::Bool(true)),
        ));
        clause_details.push((named.name, named.detail));
    }
    let flagged = sql::Cte::new(
        "flagged",
        sql::Select::new(items, vec![sql::FromItem::Cte(Rc::clone(&selected))]),
    );
    let fail_columns = clause_details
        .iter()
        .map(|(name, _)| flagged.col(&format!("{FAIL_PREFIX}{name}")))
        .collect::<Result<Vec<_>, _>>()?;
    let passes = match fail_columns.len() {
        0 => sql::Expr::Bool(true),
        1 => sql::Expr::Not(Box::new(
            fail_columns
                .into_iter()
                .next()
                .unwrap_or(sql::Expr::Bool(false)),
        )),
        _ => sql::Expr::Not(Box::new(sql::Expr::Group(Box::new(sql::Expr::Or(
            fail_columns,
        ))))),
    };

    let sector = sector_cte(as_of)?;
    let nifty_200 = membership_cte(
        universe_by_slug(NSE_MOMENTUM_INDEX)?.index_id,
        as_of,
        "nse_nifty_200",
    )?;
    let fno = membership_cte(
        universe_by_slug(FNO_UNIVERSE_SLUG)?.index_id,
        as_of,
        "nse_fno",
    )?;
    let population = nse_population_cte(as_of, &nifty_200, &fno)?;
    let desk = &tables::DESK_SCORE_DAILY;
    let bars = &tables::OHLCV_DAILY;
    let instrument = &tables::INSTRUMENT;
    let last_bar = sql::Expr::Scalar(Box::new(
        sql::Select::new(
            vec![sql::Expr::func("max", vec![bars.col("date")?])],
            vec![sql::FromItem::Table(bars)],
        )
        .filter(vec![
            sql::cmp_expr(
                bars.col("instrument_id")?,
                "=",
                flagged.col("instrument_id")?,
            ),
            sql::cmp(bars.col("date")?, "<=", BindValue::Date(as_of)),
        ]),
    ));

    let mut items = flagged.all();
    items.push(sql::Expr::label("symbol", instrument.col("symbol")?));
    items.push(sql::Expr::label("name", instrument.col("name")?));
    items.push(sql::Expr::label("passes_filters", passes));
    items.push(sql::Expr::label("sector", sector.col("slug")?));
    for (column, label) in DESK_FRAME_COLUMNS {
        items.push(sql::Expr::label(label, desk.col(column)?));
    }
    items.push(sql::Expr::label(
        "in_nifty_200",
        sql::Expr::IsNotNull(Box::new(nifty_200.col("instrument_id")?)),
    ));
    items.push(sql::Expr::label(
        "is_fno",
        sql::Expr::IsNotNull(Box::new(fno.col("instrument_id")?)),
    ));
    for name in NSE_POPULATION_COLUMNS {
        items.push(population.col(name)?);
    }
    items.push(sql::Expr::label("last_bar_date", last_bar));

    let flagged_id = flagged.col("instrument_id")?;
    let from = sql::FromItem::join(
        sql::FromItem::join(
            sql::FromItem::join(
                sql::FromItem::join(
                    sql::FromItem::join(
                        sql::FromItem::join(
                            sql::FromItem::Cte(Rc::clone(&flagged)),
                            sql::FromItem::Table(instrument),
                            sql::cmp_expr(instrument.col("id")?, "=", flagged_id.clone()),
                            false,
                        ),
                        sql::FromItem::Cte(Rc::clone(&sector)),
                        sql::cmp_expr(sector.col("instrument_id")?, "=", flagged_id.clone()),
                        true,
                    ),
                    sql::FromItem::Table(desk),
                    sql::Expr::And(vec![
                        sql::cmp_expr(desk.col("instrument_id")?, "=", flagged_id.clone()),
                        sql::cmp(desk.col("date")?, "=", BindValue::Date(as_of)),
                    ]),
                    true,
                ),
                sql::FromItem::Cte(Rc::clone(&nifty_200)),
                sql::cmp_expr(nifty_200.col("instrument_id")?, "=", flagged_id.clone()),
                true,
            ),
            sql::FromItem::Cte(Rc::clone(&fno)),
            sql::cmp_expr(fno.col("instrument_id")?, "=", flagged_id.clone()),
            true,
        ),
        sql::FromItem::Cte(Rc::clone(&population)),
        sql::Expr::Bool(true),
        false,
    );
    let select = sql::Select::new(items, vec![from]).order(vec![sql::Order::asc(flagged_id)]);
    Ok(RankingFrameQuery {
        statement: Statement { select },
        as_of,
        index_id: universe.index_id,
        universe_slug: universe.slug.to_owned(),
        clause_details,
        term_columns,
        definition_hash: definition.definition_hash(),
    })
}

/// The same rows as [`build_screen_query`], projected for docs/13's 93-column export.
pub fn build_export_query(
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    limit: i64,
) -> Result<Statement, ScreenError> {
    validate_definition(definition)?;
    refuse_outside_legacy_sql(definition, "build_export_query")?;
    let pipeline = ranked_pipeline(definition, as_of)?;
    let ranked = &pipeline.ranked;
    let bars = &tables::OHLCV_DAILY;
    let instrument = &tables::INSTRUMENT;
    let mut items = vec![
        sql::Expr::label(
            "rank",
            sql::Expr::over("row_number", Vec::new(), pipeline.order_by.clone()),
        ),
        sql::Expr::label("symbol", instrument.col("symbol")?),
        sql::Expr::label("name", instrument.col("name")?),
        sql::Expr::label("open", bars.col("open")?),
        sql::Expr::label("high", bars.col("high")?),
        sql::Expr::label("low", bars.col("low")?),
        sql::Expr::label("volume_shares", bars.col("volume_raw")?),
    ];
    for (name, _) in tables::FACTOR_DAILY.columns {
        items.push(ranked.col(name)?);
    }
    let from = sql::FromItem::join(
        instrument_join(ranked)?,
        sql::FromItem::Table(bars),
        sql::Expr::And(vec![
            sql::cmp_expr(
                bars.col("instrument_id")?,
                "=",
                ranked.col("instrument_id")?,
            ),
            sql::cmp(bars.col("date")?, "=", BindValue::Date(as_of)),
        ]),
        true,
    );
    let select = sql::Select::new(items, vec![from])
        .order(pipeline.order_by.clone())
        .limit(limit);
    Ok(Statement { select })
}

// ---------------------------------------------------------------------------
// Caching key and canonical serialisation
// ---------------------------------------------------------------------------

/// docs/06 §Caching: `screen:{sha256(definition_canonical_json \x1f columns)}:{as_of}:{version}`.
/// The digest covers the projection as well as the definition (docs/09a §3).
pub fn cache_key<S: AsRef<str>>(
    definition: &ScreenDefinition,
    as_of: NaiveDate,
    data_version: i64,
    columns: &[S],
) -> Result<String, ScreenError> {
    let projection = resolve_columns(columns)?.join(",");
    let payload = format!("{}\u{1f}{projection}", definition.canonical_json());
    let digest = sha256::hex_digest(payload.as_bytes());
    Ok(format!(
        "{CACHE_NAMESPACE}{digest}:{}:{data_version}",
        as_of.format("%Y-%m-%d")
    ))
}

/// A byte-stable JSON encoding that keeps `Decimal` exact: decimals unquoted at their own
/// precision (exponents expanded), mappings in insertion order, `ensure_ascii=False`.
pub fn canonical_json(payload: &Payload) -> Result<String, ScreenError> {
    let mut out = String::new();
    payload::encode(payload, &mut out)?;
    Ok(out)
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

/// One result row; `values` is keyed by [`ScreenResult::columns`], in insertion order.
#[derive(Debug, Clone)]
pub struct ScreenResultRow {
    /// The row's rank.
    pub rank: i64,
    /// The instrument.
    pub instrument_id: i64,
    /// Sum of the factor ranks.
    pub combined_rank: i64,
    /// `r1, r2, r3`.
    pub ranks: [i64; 3],
    /// Column values.
    pub values: Vec<(String, Payload)>,
}

impl ScreenResultRow {
    /// `str(values["symbol"])`.
    pub fn symbol(&self) -> Result<String, ScreenError> {
        self.values
            .iter()
            .find(|(k, _)| k == "symbol")
            .map(|(_, v)| payload::py_str(v))
            .ok_or_else(|| ScreenError::Key("symbol".to_owned()))
    }
}

/// The payload of docs/07 §"Running a screen".
#[derive(Debug, Clone)]
pub struct ScreenResult {
    /// The date actually used.
    pub as_of: NaiveDate,
    /// The published data version.
    pub data_version: i64,
    /// Factor one.
    pub sorting_factor: RankingFactor,
    /// Column names.
    pub columns: Vec<String>,
    /// Rows.
    pub rows: Vec<ScreenResultRow>,
    /// docs/06 §step 1: the date the client asked for.
    pub requested_as_of: Option<NaiveDate>,
    /// True when the row cap cut the result.
    pub truncated: bool,
    /// Every ranking factor.
    pub ranking_factors: Vec<RankingFactor>,
}

impl ScreenResult {
    /// `len(rows)`.
    pub fn result_count(&self) -> usize {
        self.rows.len()
    }

    /// Every row's symbol, in order.
    pub fn symbols(&self) -> Result<Vec<String>, ScreenError> {
        self.rows.iter().map(ScreenResultRow::symbol).collect()
    }

    /// The response body as a [`Payload`] (insertion order kept).
    pub fn payload(&self) -> Payload {
        let rows = self
            .rows
            .iter()
            .map(|row| {
                let mut entries = vec![("rank".to_owned(), Payload::Int(row.rank))];
                for (k, v) in &row.values {
                    match entries.iter_mut().find(|(key, _)| key == k) {
                        Some(slot) => slot.1 = v.clone(),
                        None => entries.push((k.clone(), v.clone())),
                    }
                }
                Payload::Map(entries)
            })
            .collect();
        Payload::Map(vec![
            (
                "as_of".to_owned(),
                Payload::Str(self.as_of.format("%Y-%m-%d").to_string()),
            ),
            ("data_version".to_owned(), Payload::Int(self.data_version)),
            (
                "result_count".to_owned(),
                Payload::Int(i64::try_from(self.result_count()).unwrap_or(i64::MAX)),
            ),
            (
                "sorting_factor".to_owned(),
                Payload::Map(vec![
                    (
                        "key".to_owned(),
                        Payload::Str(self.sorting_factor.key.clone()),
                    ),
                    (
                        "label".to_owned(),
                        Payload::Str(self.sorting_factor.label.clone()),
                    ),
                ]),
            ),
            (
                "columns".to_owned(),
                Payload::List(self.columns.iter().cloned().map(Payload::Str).collect()),
            ),
            ("rows".to_owned(), Payload::List(rows)),
        ])
    }

    /// The cached bytes, and the determinism guarantee's unit of comparison.
    pub fn to_json(&self) -> Result<String, ScreenError> {
        canonical_json(&self.payload())
    }
}

// ---------------------------------------------------------------------------
// Payload: the Python values canonical_json and the model accept
// ---------------------------------------------------------------------------

mod payload {
    use std::fmt::Write as _;

    use chrono::{Datelike, NaiveDate};
    use rust_decimal::Decimal;
    use serde::{Serialize, Serializer};

    use super::ScreenError;
    use crate::factor_registry::py_repr_str;

    /// A finite Python `Decimal`, held exactly as Python holds it: a sign, a coefficient of any
    /// length and an exponent (`(-1)**negative * coefficient * 10**exponent`). Nothing here does
    /// arithmetic — the screener only parses, compares and prints these values — so the triple is
    /// kept rather than a `rust_decimal::Decimal`, whose 28 digits would refuse inputs pydantic
    /// accepts (`Decimal("1E+300")`, `Decimal("5E-324")`) and drop the sign of a zero and a
    /// positive exponent (`Decimal("1E+2")` prints as `1E+2`). Serialises as `format(d, "f")`,
    /// golden.py's encoding.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct DecimalValue {
        negative: bool,
        /// ASCII digits without leading zeros; `"0"` for a zero.
        coefficient: String,
        exponent: i64,
    }

    impl From<Decimal> for DecimalValue {
        fn from(value: Decimal) -> Self {
            let mantissa = value.mantissa();
            DecimalValue {
                negative: mantissa < 0,
                coefficient: mantissa.unsigned_abs().to_string(),
                exponent: -i64::from(value.scale()),
            }
        }
    }

    /// libmpdec's `MAX_EMAX`: the largest adjusted exponent `Decimal(str)` accepts.
    const MAX_EMAX: i128 = 999_999_999_999_999_999;
    /// libmpdec's `MIN_ETINY`: the smallest exponent `Decimal(str)` accepts.
    const MIN_ETINY: i128 = -1_999_999_999_999_999_997;

    impl DecimalValue {
        fn is_zero(&self) -> bool {
            self.coefficient == "0"
        }

        fn digits(&self) -> i64 {
            i64::try_from(self.coefficient.len()).unwrap_or(i64::MAX)
        }

        /// `format(d, "f")` — the exponent expanded; a zero with a positive exponent is `0`.
        pub fn format_f(&self) -> String {
            let sign = if self.negative { "-" } else { "" };
            let exponent = if self.is_zero() && self.exponent > 0 {
                0
            } else {
                self.exponent
            };
            let len = self.digits();
            let dot = exponent.saturating_add(len);
            let zeros = |n: i64| "0".repeat(usize::try_from(n).unwrap_or(0));
            let body = if dot <= 0 {
                format!("0.{}{}", zeros(-dot), self.coefficient)
            } else if dot >= len {
                format!("{}{}", self.coefficient, zeros(dot - len))
            } else {
                let (int, frac) = self.coefficient.split_at(usize::try_from(dot).unwrap_or(0));
                format!("{int}.{frac}")
            };
            format!("{sign}{body}")
        }

        /// `str(d)` — Python's to-sci-string.
        pub fn py_str(&self) -> String {
            let sign = if self.negative { "-" } else { "" };
            let len = self.digits();
            let left = self.exponent.saturating_add(len);
            let dot = if self.exponent <= 0 && left > -6 {
                left
            } else {
                1
            };
            let zeros = |n: i64| "0".repeat(usize::try_from(n).unwrap_or(0));
            let (int, frac) = if dot <= 0 {
                (
                    "0".to_owned(),
                    format!(".{}{}", zeros(-dot), self.coefficient),
                )
            } else if dot >= len {
                (
                    format!("{}{}", self.coefficient, zeros(dot - len)),
                    String::new(),
                )
            } else {
                let (a, b) = self.coefficient.split_at(usize::try_from(dot).unwrap_or(0));
                (a.to_owned(), format!(".{b}"))
            };
            let exp = if left == dot {
                String::new()
            } else {
                format!("E{:+}", left - dot)
            };
            format!("{sign}{int}{frac}{exp}")
        }

        /// Python's numeric comparison of two finite decimals (`-0 == 0`).
        pub fn cmp_value(&self, other: &DecimalValue) -> std::cmp::Ordering {
            use std::cmp::Ordering;
            match (self.is_zero(), other.is_zero()) {
                (true, true) => return Ordering::Equal,
                (true, false) => {
                    return if other.negative {
                        Ordering::Greater
                    } else {
                        Ordering::Less
                    };
                }
                (false, true) => {
                    return if self.negative {
                        Ordering::Less
                    } else {
                        Ordering::Greater
                    };
                }
                (false, false) => {}
            }
            if self.negative != other.negative {
                return if self.negative {
                    Ordering::Less
                } else {
                    Ordering::Greater
                };
            }
            let magnitude = self.cmp_magnitude(other);
            if self.negative {
                magnitude.reverse()
            } else {
                magnitude
            }
        }

        /// Compare `|self|` with `|other|`, both non-zero.
        fn cmp_magnitude(&self, other: &DecimalValue) -> std::cmp::Ordering {
            let adjusted = |d: &DecimalValue| i128::from(d.exponent) + i128::from(d.digits());
            adjusted(self).cmp(&adjusted(other)).then_with(|| {
                // Same leading position: compare the digit strings, the shorter padded with zeros.
                let a = self.coefficient.as_bytes();
                let b = other.coefficient.as_bytes();
                (0..a.len().max(b.len()))
                    .map(|i| {
                        let x = a.get(i).copied().unwrap_or(b'0');
                        let y = b.get(i).copied().unwrap_or(b'0');
                        x.cmp(&y)
                    })
                    .find(|o| o.is_ne())
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
        }

        /// The value as an integer when it has no fractional part: `Ok(Some(i))` inside i64,
        /// `Ok(None)` when integral but wider than i64, `Err(())` when it has a fraction.
        pub(crate) fn integral_i64(&self) -> Result<Option<i64>, ()> {
            let digits = if self.exponent >= 0 {
                if self.is_zero() {
                    return Ok(Some(0));
                }
                let zeros = usize::try_from(self.exponent).unwrap_or(usize::MAX);
                if zeros > 40 {
                    return Ok(None);
                }
                format!("{}{}", self.coefficient, "0".repeat(zeros))
            } else {
                let cut = usize::try_from(-self.exponent).unwrap_or(usize::MAX);
                let len = self.coefficient.len();
                let (int, frac) = if cut >= len {
                    ("0", self.coefficient.as_str())
                } else {
                    self.coefficient.split_at(len - cut)
                };
                if frac.bytes().any(|b| b != b'0') {
                    return Err(());
                }
                int.to_owned()
            };
            let magnitude: i128 = match digits.parse() {
                Ok(m) => m,
                Err(_) => return Ok(None),
            };
            let value = if self.negative { -magnitude } else { magnitude };
            Ok(i64::try_from(value).ok())
        }
    }

    impl Serialize for DecimalValue {
        fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            serializer.serialize_str(&self.format_f())
        }
    }

    /// A Python `Decimal`: finite, or one of the non-finite values.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub enum PyDecimal {
        /// A finite value.
        Finite(DecimalValue),
        /// `NaN`, `sNaN` (with any diagnostic payload), `Infinity` or `-Infinity`, as Python's
        /// `str()` prints it.
        NonFinite(String),
    }

    /// Python's `str.isspace` for one character: Rust's `White_Space` plus the four ASCII
    /// information separators `\x1c`-`\x1f`.
    fn py_isspace(c: char) -> bool {
        c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
    }

    /// The Unicode decimal digit value of `c` (`Py_UNICODE_TODECIMAL`), from the zero of each
    /// block of ten Nd digits.
    fn unicode_decimal(c: char) -> Option<u8> {
        let code = c as u32;
        let index = DECIMAL_ZEROS.partition_point(|&zero| zero <= code);
        let zero = DECIMAL_ZEROS.get(index.checked_sub(1)?)?;
        u8::try_from(code - zero).ok().filter(|d| *d < 10)
    }

    /// The first code point of every block of ten Unicode decimal digits (category Nd), from
    /// CPython 3.12's `unicodedata` (Unicode 15.0.0).
    const DECIMAL_ZEROS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60,
        0x16ac0, 0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0,
        0x1e950, 0x1fbf0,
    ];

    impl PyDecimal {
        /// Python's `Decimal(text)`: surrounding whitespace (`str.isspace`) stripped, every `_`
        /// dropped, Unicode decimal digits read as ASCII, then sign, digits, point, exponent; or
        /// `Inf`/`Infinity`/`NaN`/`sNaN` (with an optional digit payload) in any case. `None`
        /// where Python raises (also for an exponent outside libmpdec's limits).
        pub fn parse(text: &str) -> Option<PyDecimal> {
            let trimmed = text.trim_matches(py_isspace);
            let mut ascii = String::with_capacity(trimmed.len());
            for c in trimmed.chars() {
                if c == '_' {
                    continue;
                }
                if c.is_ascii() && c != '\0' {
                    ascii.push(c);
                } else if py_isspace(c) {
                    ascii.push(' ');
                } else {
                    ascii.push(char::from(b'0' + unicode_decimal(c)?));
                }
            }
            let lower = ascii.to_ascii_lowercase();
            let (negative, body) = match lower.as_bytes().first() {
                Some(b'-') => (true, &lower[1..]),
                Some(b'+') => (false, &lower[1..]),
                _ => (false, lower.as_str()),
            };
            let sign = if negative { "-" } else { "" };
            if body == "inf" || body == "infinity" {
                return Some(PyDecimal::NonFinite(format!("{sign}Infinity")));
            }
            for (prefix, name) in [("snan", "sNaN"), ("nan", "NaN")] {
                if let Some(payload) = body.strip_prefix(prefix) {
                    if !payload.bytes().all(|b| b.is_ascii_digit()) {
                        return None;
                    }
                    let payload = payload.trim_start_matches('0');
                    return Some(PyDecimal::NonFinite(format!("{sign}{name}{payload}")));
                }
            }
            let (mantissa, exponent) = match body.find('e') {
                Some(i) => (&body[..i], parse_exponent(&body[i + 1..])?),
                None => (body, 0),
            };
            let (int_part, frac_part) = match mantissa.find('.') {
                Some(i) => (&mantissa[..i], &mantissa[i + 1..]),
                None => (mantissa, ""),
            };
            if int_part.is_empty() && frac_part.is_empty() {
                return None;
            }
            if !int_part
                .bytes()
                .chain(frac_part.bytes())
                .all(|b| b.is_ascii_digit())
            {
                return None;
            }
            let all = format!("{int_part}{frac_part}");
            let stripped = all.trim_start_matches('0');
            let coefficient = if stripped.is_empty() { "0" } else { stripped };
            let frac_len = i128::try_from(frac_part.len()).ok()?;
            let exp = exponent - frac_len;
            let len = i128::try_from(coefficient.len()).ok()?;
            if exp < MIN_ETINY || exp + len - 1 > MAX_EMAX {
                return None;
            }
            Some(PyDecimal::Finite(DecimalValue {
                negative,
                coefficient: coefficient.to_owned(),
                exponent: i64::try_from(exp).ok()?,
            }))
        }

        /// `format(d, "f")`, or the non-finite spelling.
        pub fn format_f(&self) -> String {
            match self {
                PyDecimal::Finite(d) => d.format_f(),
                PyDecimal::NonFinite(s) => s.clone(),
            }
        }
    }

    /// An exponent's digits after `e`: optional sign, at least one digit (any length; a value
    /// past `i128` saturates, and is refused by the caller's limits).
    fn parse_exponent(text: &str) -> Option<i128> {
        let (negative, digits) = match text.as_bytes().first() {
            Some(b'-') => (true, &text[1..]),
            Some(b'+') => (false, &text[1..]),
            _ => (false, text),
        };
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        let magnitude = digits.bytes().fold(0_i128, |acc, b| {
            acc.saturating_mul(10).saturating_add(i128::from(b - b'0'))
        });
        Some(if negative { -magnitude } else { magnitude })
    }

    /// A Python value as `canonical_json` and `ScreenDefinition.model_validate` receive it.
    #[derive(Debug, Clone)]
    pub enum Payload {
        /// `None`.
        Null,
        /// `bool`.
        Bool(bool),
        /// `int`.
        Int(i64),
        /// `float`.
        Float(f64),
        /// `str`.
        Str(String),
        /// `Decimal`.
        Decimal(PyDecimal),
        /// `datetime.date`.
        Date(NaiveDate),
        /// A list or tuple.
        List(Vec<Payload>),
        /// A mapping, in insertion order.
        Map(Vec<(String, Payload)>),
        /// An object of some other type (its type name) — `canonical_json` refuses it.
        Opaque(String),
    }

    /// Python's `repr(float)` (and `json.dumps` of a float): shortest round-trip digits, fixed
    /// notation for exponents in `[-4, 16)`, `1e+16` / `1.5e-07` outside it.
    pub fn py_float_repr(x: f64) -> String {
        if x.is_nan() {
            return "NaN".to_owned();
        }
        if x.is_infinite() {
            return if x > 0.0 { "Infinity" } else { "-Infinity" }.to_owned();
        }
        // Rust's shortest form gives the shortest round-trip *length*; among candidates of that
        // length Python takes the one closest to the exact value, which is the correctly
        // rounded print at that precision.
        let shortest = format!("{x:e}");
        let length = shortest
            .split('e')
            .next()
            .unwrap_or("")
            .chars()
            .filter(char::is_ascii_digit)
            .count();
        // At a power of two the rounding interval is lopsided, and the correctly rounded print
        // may fall outside it; then the shortest form (which round-trips by construction) is
        // the only candidate of that length, and Python prints it too.
        let rounded = format!("{x:.*e}", length.saturating_sub(1));
        let sci = if rounded
            .parse::<f64>()
            .is_ok_and(|y| y.to_bits() == x.to_bits())
        {
            rounded
        } else {
            shortest
        };
        let (mantissa, exp) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
        let exp: i64 = exp.parse().unwrap_or(0);
        let (sign, mantissa) = match mantissa.strip_prefix('-') {
            Some(m) => ("-", m),
            None => ("", mantissa),
        };
        let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
        let ndigits = i64::try_from(digits.len()).unwrap_or(1);
        if (-4..16).contains(&exp) {
            let point = exp + 1;
            let body = if point <= 0 {
                format!(
                    "0.{}{digits}",
                    "0".repeat(usize::try_from(-point).unwrap_or(0))
                )
            } else if point >= ndigits {
                format!(
                    "{digits}{}.0",
                    "0".repeat(usize::try_from(point - ndigits).unwrap_or(0))
                )
            } else {
                let (a, b) = digits.split_at(usize::try_from(point).unwrap_or(0));
                format!("{a}.{b}")
            };
            return format!("{sign}{body}");
        }
        let (head, tail) = digits.split_at(1);
        let point = if tail.is_empty() {
            String::new()
        } else {
            format!(".{tail}")
        };
        let exp_sign = if exp < 0 { "-" } else { "+" };
        format!("{sign}{head}{point}e{exp_sign}{:02}", exp.abs())
    }

    /// `json.dumps(text, ensure_ascii=False)`.
    pub fn json_str(text: &str, out: &mut String) {
        out.push('"');
        for ch in text.chars() {
            match ch {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\u{8}' => out.push_str("\\b"),
                '\u{c}' => out.push_str("\\f"),
                c if (c as u32) < 0x20 => {
                    let _ = write!(out, "\\u{:04x}", c as u32);
                }
                c => out.push(c),
            }
        }
        out.push('"');
    }

    /// `screener._encode` — insertion order kept, decimals unquoted.
    pub fn encode(value: &Payload, out: &mut String) -> Result<(), ScreenError> {
        match value {
            Payload::Decimal(d) => match d {
                PyDecimal::Finite(_) => {
                    let text = d.format_f();
                    out.push_str(if text == "-0" { "-0" } else { &text });
                }
                PyDecimal::NonFinite(s) => {
                    return Err(ScreenError::Value(format!(
                        "{s} is not a finite decimal and has no JSON representation"
                    )));
                }
            },
            Payload::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Payload::Null => out.push_str("null"),
            Payload::Int(i) => {
                let _ = write!(out, "{i}");
            }
            Payload::Float(f) => out.push_str(&py_float_repr(*f)),
            Payload::Str(s) => json_str(s, out),
            Payload::Date(d) => json_str(&d.format("%Y-%m-%d").to_string(), out),
            Payload::Map(entries) => {
                out.push('{');
                for (i, (k, v)) in entries.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    json_str(k, out);
                    out.push(':');
                    encode(v, out)?;
                }
                out.push('}');
            }
            Payload::List(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    encode(item, out)?;
                }
                out.push(']');
            }
            Payload::Opaque(type_name) => {
                return Err(ScreenError::Type(format!(
                    "cannot canonically encode {type_name}"
                )));
            }
        }
        Ok(())
    }

    /// `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
    pub fn dumps_sorted(value: &Payload, out: &mut String) {
        match value {
            Payload::Map(entries) => {
                let mut sorted: Vec<&(String, Payload)> = entries.iter().collect();
                sorted.sort_by(|a, b| a.0.cmp(&b.0));
                out.push('{');
                for (i, (k, v)) in sorted.into_iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    json_str(k, out);
                    out.push(':');
                    dumps_sorted(v, out);
                }
                out.push('}');
            }
            Payload::List(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    dumps_sorted(item, out);
                }
                out.push(']');
            }
            Payload::Decimal(d) => json_str(&d.format_f(), out),
            Payload::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Payload::Null | Payload::Opaque(_) => out.push_str("null"),
            Payload::Int(i) => {
                let _ = write!(out, "{i}");
            }
            Payload::Float(f) => out.push_str(&py_float_repr(*f)),
            Payload::Str(s) => json_str(s, out),
            Payload::Date(d) => json_str(&d.format("%Y-%m-%d").to_string(), out),
        }
    }

    /// Python's `str(value)`.
    pub fn py_str(value: &Payload) -> String {
        match value {
            Payload::Str(s) => s.clone(),
            Payload::Decimal(PyDecimal::Finite(d)) => d.py_str(),
            Payload::Decimal(PyDecimal::NonFinite(s)) => s.clone(),
            Payload::Date(d) => d.format("%Y-%m-%d").to_string(),
            other => py_repr(other),
        }
    }

    /// Python's `repr(value)` (a container's `str` is its `repr`). An opaque object's repr
    /// carries its memory address in Python, which no port can reproduce; it renders as
    /// `<type object>`.
    pub fn py_repr(value: &Payload) -> String {
        match value {
            Payload::Null => "None".to_owned(),
            Payload::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
            Payload::Int(i) => i.to_string(),
            Payload::Float(f) => match py_float_repr(*f).as_str() {
                "NaN" => "nan".to_owned(),
                "Infinity" => "inf".to_owned(),
                "-Infinity" => "-inf".to_owned(),
                s => s.to_owned(),
            },
            Payload::Str(s) => py_repr_str(s),
            Payload::Decimal(d) => {
                let text = match d {
                    PyDecimal::Finite(v) => v.py_str(),
                    PyDecimal::NonFinite(s) => s.clone(),
                };
                format!("Decimal('{text}')")
            }
            Payload::Date(d) => format!("datetime.date({}, {}, {})", d.year(), d.month(), d.day()),
            Payload::List(items) => {
                let parts: Vec<String> = items.iter().map(py_repr).collect();
                format!("[{}]", parts.join(", "))
            }
            Payload::Map(entries) => {
                let parts: Vec<String> = entries
                    .iter()
                    .map(|(k, v)| format!("{}: {}", py_repr_str(k), py_repr(v)))
                    .collect();
                format!("{{{}}}", parts.join(", "))
            }
            Payload::Opaque(type_name) => format!("<{type_name} object>"),
        }
    }
}

// ---------------------------------------------------------------------------
// SHA-256 (FIPS 180-4), for cache_key and definition_hash
// ---------------------------------------------------------------------------

pub(crate) mod sha256 {
    use std::fmt::Write as _;

    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];

    /// The lowercase hex SHA-256 digest of `data` (Python's `hashlib.sha256(data).hexdigest()`).
    pub fn hex_digest(data: &[u8]) -> String {
        let mut h: [u32; 8] = [
            0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
            0x5be0cd19,
        ];
        let mut message = data.to_vec();
        let bit_len = (data.len() as u64).wrapping_mul(8);
        message.push(0x80);
        while message.len() % 64 != 56 {
            message.push(0);
        }
        message.extend_from_slice(&bit_len.to_be_bytes());
        let (blocks, _) = message.as_chunks::<64>();
        for block in blocks {
            let mut w = [0u32; 64];
            let (words, _) = block.as_chunks::<4>();
            for (slot, word) in w.iter_mut().zip(words) {
                *slot = u32::from_be_bytes(*word);
            }
            for i in 16..64 {
                let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
                let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
                w[i] = w[i - 16]
                    .wrapping_add(s0)
                    .wrapping_add(w[i - 7])
                    .wrapping_add(s1);
            }
            let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
            for i in 0..64 {
                let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
                let ch = (e & f) ^ (!e & g);
                let t1 = hh
                    .wrapping_add(s1)
                    .wrapping_add(ch)
                    .wrapping_add(K[i])
                    .wrapping_add(w[i]);
                let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
                let maj = (a & b) ^ (a & c) ^ (b & c);
                let t2 = s0.wrapping_add(maj);
                hh = g;
                g = f;
                f = e;
                e = d.wrapping_add(t1);
                d = c;
                c = b;
                b = a;
                a = t1.wrapping_add(t2);
            }
            for (slot, v) in h.iter_mut().zip([a, b, c, d, e, f, g, hh]) {
                *slot = slot.wrapping_add(v);
            }
        }
        let mut out = String::with_capacity(64);
        for word in h {
            let _ = write!(out, "{word:08x}");
        }
        out
    }
}

// ---------------------------------------------------------------------------
// Table metadata (`baskfy_core.models`): column names and SQLAlchemy types
// ---------------------------------------------------------------------------

mod tables {
    use super::ScreenError;
    use super::sql::{Expr, SqlType};

    /// A table and its columns, in declaration order.
    #[derive(Debug)]
    pub struct TableDef {
        pub name: &'static str,
        pub columns: &'static [(&'static str, SqlType)],
    }

    impl TableDef {
        pub fn col(&self, name: &str) -> Result<Expr, ScreenError> {
            self.columns
                .iter()
                .find(|(n, _)| *n == name)
                .map(|&(n, ty)| Expr::Column {
                    qualifier: self.name.to_owned(),
                    name: n.to_owned(),
                    ty,
                })
                .ok_or_else(|| ScreenError::Key(name.to_owned()))
        }

        pub fn all(&self) -> Vec<Expr> {
            self.columns
                .iter()
                .map(|&(n, ty)| Expr::Column {
                    qualifier: self.name.to_owned(),
                    name: n.to_owned(),
                    ty,
                })
                .collect()
        }
    }

    const fn num(p: u8, s: u8) -> SqlType {
        SqlType::Numeric(Some((p, s)))
    }

    pub static FACTOR_DAILY: TableDef = TableDef {
        name: "factor_daily",
        columns: &[
            ("instrument_id", SqlType::BigInt),
            ("date", SqlType::Date),
            ("close", num(18, 2)),
            ("close_raw", num(18, 2)),
            ("ret_1m", num(14, 2)),
            ("ret_3m", num(14, 2)),
            ("ret_6m", num(14, 2)),
            ("ret_9m", num(14, 2)),
            ("ret_12m", num(14, 2)),
            ("ret_12m_minus_1m", num(14, 2)),
            ("ret_12m_minus_2m", num(14, 2)),
            ("vol_1m", num(18, 10)),
            ("vol_3m", num(18, 10)),
            ("vol_6m", num(18, 10)),
            ("vol_9m", num(18, 10)),
            ("vol_12m", num(18, 10)),
            ("sharpe_1m", num(14, 2)),
            ("sharpe_3m", num(14, 2)),
            ("sharpe_6m", num(14, 2)),
            ("sharpe_9m", num(14, 2)),
            ("sharpe_12m", num(14, 2)),
            ("rsi_1m", num(10, 4)),
            ("rsi_3m", num(10, 4)),
            ("rsi_6m", num(10, 4)),
            ("rsi_9m", num(10, 4)),
            ("rsi_12m", num(10, 4)),
            ("beta_12m", num(18, 10)),
            ("ma_20", num(18, 2)),
            ("ma_50", num(18, 2)),
            ("ma_100", num(18, 2)),
            ("ma_200", num(18, 2)),
            ("high_1y", num(18, 2)),
            ("high_ath", num(18, 2)),
            ("away_high_1y", num(10, 2)),
            ("away_high_ath", num(10, 2)),
            ("pos_days_1m", num(7, 2)),
            ("pos_days_3m", num(7, 2)),
            ("pos_days_6m", num(7, 2)),
            ("pos_days_9m", num(7, 2)),
            ("pos_days_12m", num(7, 2)),
            ("circuits_1m", SqlType::SmallInt),
            ("circuits_3m", SqlType::SmallInt),
            ("circuits_6m", SqlType::SmallInt),
            ("circuits_9m", SqlType::SmallInt),
            ("circuits_12m", SqlType::SmallInt),
            ("vol_day_val", SqlType::BigInt),
            ("vol_avg_1w", SqlType::BigInt),
            ("vol_avg_1m", SqlType::BigInt),
            ("vol_avg_3m", SqlType::BigInt),
            ("vol_avg_6m", SqlType::BigInt),
            ("vol_avg_9m", SqlType::BigInt),
            ("vol_avg_12m", SqlType::BigInt),
            ("median_vol_12m", SqlType::BigInt),
            ("marketcap_cr", SqlType::BigInt),
            ("pe", num(14, 4)),
            ("series", SqlType::Varchar(None)),
            ("regime", SqlType::Varchar(None)),
            ("atr_14", num(18, 4)),
            ("atr_ext_20", num(10, 4)),
            ("ma50_slope_20", num(14, 2)),
            ("eff_ratio_63", num(10, 4)),
            ("max_dd_6m", num(14, 2)),
            ("max_dd_12m", num(14, 2)),
            ("downside_vol_6m", num(18, 10)),
            ("downside_vol_12m", num(18, 10)),
            ("sortino_6m", num(14, 2)),
            ("sortino_12m", num(14, 2)),
            ("underwater_12m", num(7, 2)),
            ("ret_ex_top3_12m", num(14, 2)),
            ("accel_21_105", num(18, 10)),
            ("accel_21_105_vs", num(10, 4)),
            ("vol_exp_21_126", num(10, 4)),
            ("vol_persist_20", SqlType::SmallInt),
            ("excess_ret_3m", num(14, 2)),
            ("excess_ret_6m", num(14, 2)),
            ("excess_ret_12m", num(14, 2)),
            ("resid_ret_12m", num(14, 2)),
            ("rs_persist_126", num(7, 2)),
            ("mom_pctile", num(7, 2)),
            ("rank_persist_20", num(7, 2)),
            ("nse_mr6", num(18, 10)),
            ("nse_mr12", num(18, 10)),
            ("universe_mask", SqlType::Integer),
            ("top_beta_mask", SqlType::Integer),
            ("top_volatility_mask", SqlType::Integer),
        ],
    };

    pub static INDEX_MEMBER_DAILY: TableDef = TableDef {
        name: "index_member_daily",
        columns: &[
            ("index_id", SqlType::SmallInt),
            ("date", SqlType::Date),
            ("instrument_id", SqlType::BigInt),
            ("weight", num(10, 6)),
            ("source", SqlType::Varchar(None)),
        ],
    };

    pub static INSTRUMENT: TableDef = TableDef {
        name: "instrument",
        columns: &[
            ("id", SqlType::BigInt),
            ("exchange_id", SqlType::SmallInt),
            ("symbol", SqlType::Varchar(None)),
            ("name", SqlType::Varchar(None)),
            ("isin", SqlType::Varchar(None)),
            ("instrument_type", SqlType::Varchar(None)),
            ("series", SqlType::Varchar(None)),
            ("face_value", num(12, 4)),
            ("lot_size", SqlType::Integer),
            ("listed_on", SqlType::Date),
            ("delisted_on", SqlType::Date),
            ("kite_token", SqlType::BigInt),
            ("is_active", SqlType::Boolean),
            ("created_at", SqlType::TimestampTz),
            ("updated_at", SqlType::TimestampTz),
        ],
    };

    pub static DESK_SCORE_DAILY: TableDef = TableDef {
        name: "desk_score_daily",
        columns: &[
            ("instrument_id", SqlType::BigInt),
            ("date", SqlType::Date),
            ("score", num(6, 1)),
            ("score_rank", SqlType::Integer),
            ("a_trend", num(8, 4)),
            ("b_momentum", num(8, 4)),
            ("c_sharpe", num(8, 4)),
            ("d_consistency", num(8, 4)),
            ("e_liquidity", num(8, 4)),
            ("f_penalty", num(8, 4)),
            ("ext_over_20dma", num(10, 4)),
            ("reject", SqlType::Varchar(Some(200))),
            ("score_version", SqlType::Varchar(Some(32))),
        ],
    };

    pub static INDEX_DEF: TableDef = TableDef {
        name: "index_def",
        columns: &[
            ("id", SqlType::SmallInt),
            ("slug", SqlType::Varchar(None)),
            ("name", SqlType::Varchar(None)),
            ("is_universe", SqlType::Boolean),
            ("sort_order", SqlType::SmallInt),
        ],
    };

    pub static OHLCV_DAILY: TableDef = TableDef {
        name: "ohlcv_daily",
        columns: &[
            ("instrument_id", SqlType::BigInt),
            ("date", SqlType::Date),
            ("open", num(18, 4)),
            ("high", num(18, 4)),
            ("low", num(18, 4)),
            ("close", num(18, 4)),
            ("volume", SqlType::BigInt),
            ("close_raw", num(18, 4)),
            ("volume_raw", SqlType::BigInt),
            ("open_raw", num(18, 4)),
            ("high_raw", num(18, 4)),
            ("low_raw", num(18, 4)),
            ("turnover", num(20, 2)),
            ("adj_factor", num(18, 10)),
            ("upper_circuit", num(18, 4)),
            ("lower_circuit", num(18, 4)),
            ("source", SqlType::Varchar(None)),
        ],
    };
}

// ---------------------------------------------------------------------------
// The statement tree and its PostgreSQL (asyncpg) compiler
// ---------------------------------------------------------------------------

mod sql {
    use std::collections::{HashMap, HashSet};
    use std::rc::Rc;

    use super::{BindValue, CompiledStatement, ScreenError, tables::TableDef};

    /// The SQLAlchemy types the statements touch.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum SqlType {
        BigInt,
        SmallInt,
        Integer,
        Date,
        Numeric(Option<(u8, u8)>),
        Float,
        Varchar(Option<u16>),
        Boolean,
        TimestampTz,
        Null,
    }

    #[derive(PartialEq, Eq)]
    enum Affinity {
        Integer,
        Numeric,
        Date,
        String,
        Boolean,
        DateTime,
        Null,
    }

    impl SqlType {
        /// SQLAlchemy's `_type_affinity` (the generic class right under `TypeEngine`).
        fn affinity(self) -> Affinity {
            match self {
                SqlType::BigInt | SqlType::SmallInt | SqlType::Integer => Affinity::Integer,
                SqlType::Numeric(_) | SqlType::Float => Affinity::Numeric,
                SqlType::Date => Affinity::Date,
                SqlType::Varchar(_) => Affinity::String,
                SqlType::Boolean => Affinity::Boolean,
                SqlType::TimestampTz => Affinity::DateTime,
                SqlType::Null => Affinity::Null,
            }
        }

        /// The asyncpg dialect's `::TYPE` cast text.
        fn render(self) -> String {
            match self {
                SqlType::BigInt => "BIGINT".to_owned(),
                SqlType::SmallInt => "SMALLINT".to_owned(),
                SqlType::Integer => "INTEGER".to_owned(),
                SqlType::Date => "DATE".to_owned(),
                SqlType::Numeric(None) => "NUMERIC".to_owned(),
                SqlType::Numeric(Some((p, s))) => format!("NUMERIC({p}, {s})"),
                SqlType::Float => "FLOAT".to_owned(),
                SqlType::Varchar(None) => "VARCHAR".to_owned(),
                SqlType::Varchar(Some(n)) => format!("VARCHAR({n})"),
                SqlType::Boolean => "BOOLEAN".to_owned(),
                SqlType::TimestampTz => "TIMESTAMP WITH TIME ZONE".to_owned(),
                SqlType::Null => "NULL".to_owned(),
            }
        }
    }

    /// The type SQLAlchemy resolves a Python value to.
    fn value_type(value: &BindValue) -> SqlType {
        match value {
            // `Integer._resolve_for_literal`: `bit_length() >= 32` is a BigInteger.
            BindValue::Int(i) if i.unsigned_abs() >= 1 << 31 => SqlType::BigInt,
            BindValue::Int(_) => SqlType::Integer,
            BindValue::Float(_) => SqlType::Float,
            BindValue::Decimal(_) => SqlType::Numeric(None),
            BindValue::Date(_) => SqlType::Date,
            BindValue::StrList(_) => SqlType::Varchar(None),
        }
    }

    /// `TypeEngine.coerce_compared_value`: the column's type when the value has the same
    /// affinity, else the value's own type.
    fn coerce(compared: SqlType, value: &BindValue) -> SqlType {
        let resolved = value_type(value);
        if resolved.affinity() == compared.affinity() {
            compared
        } else {
            resolved
        }
    }

    /// `_anonymous_label.safe_construct(..., sanitize_key=True)`.
    fn sanitize(text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        let mut in_run = false;
        for ch in text.chars() {
            if matches!(ch, '%' | '(' | ')' | ' ' | '$') {
                if !in_run {
                    out.push('_');
                    in_run = true;
                }
            } else {
                out.push(ch);
                in_run = false;
            }
        }
        out.trim_matches('_').to_owned()
    }

    /// A bound parameter.
    #[derive(Debug, Clone)]
    pub struct Bind {
        base: String,
        value: BindValue,
        ty: SqlType,
        expanding: bool,
    }

    /// One ORDER BY element.
    #[derive(Debug, Clone)]
    pub struct Order {
        pub expr: Expr,
        pub desc: bool,
        pub nulls_last: bool,
    }

    impl Order {
        pub fn asc(expr: Expr) -> Order {
            Order {
                expr,
                desc: false,
                nulls_last: false,
            }
        }

        pub fn desc_nulls_last(expr: Expr) -> Order {
            Order {
                expr,
                desc: true,
                nulls_last: true,
            }
        }
    }

    /// An expression.
    #[derive(Debug, Clone)]
    pub enum Expr {
        Column {
            qualifier: String,
            name: String,
            ty: SqlType,
        },
        /// `literal_column(text)` — NullType, rendered verbatim.
        Literal(String),
        Bind(Bind),
        Binary {
            left: Box<Expr>,
            op: &'static str,
            right: Box<Expr>,
            ty: SqlType,
        },
        And(Vec<Expr>),
        Or(Vec<Expr>),
        Not(Box<Expr>),
        Group(Box<Expr>),
        IsNotNull(Box<Expr>),
        In(Box<Expr>, Bind),
        Func {
            name: &'static str,
            args: Vec<Expr>,
        },
        CountStar,
        Over {
            func: Box<Expr>,
            partition_by: Vec<Expr>,
            order_by: Vec<Order>,
        },
        Case {
            when: Box<Expr>,
            then: Box<Expr>,
            otherwise: Box<Expr>,
        },
        Bool(bool),
        Scalar(Box<Select>),
        Label(String, Box<Expr>),
    }

    impl Expr {
        pub fn label(name: &str, inner: Expr) -> Expr {
            Expr::Label(name.to_owned(), Box::new(inner))
        }

        pub fn func(name: &'static str, args: Vec<Expr>) -> Expr {
            Expr::Func { name, args }
        }

        pub fn over(name: &'static str, partition_by: Vec<Expr>, order_by: Vec<Order>) -> Expr {
            Expr::Over {
                func: Box::new(Expr::func(name, Vec::new())),
                partition_by,
                order_by,
            }
        }

        /// `left <op> right` arithmetic; the result has the left operand's type.
        pub fn binary(left: Expr, op: &'static str, right: Expr) -> Expr {
            let ty = left.ty();
            Expr::Binary {
                left: Box::new(left),
                op,
                right: Box::new(right),
                ty,
            }
        }

        /// `literal(value)` — an anonymous `param_<n>` bind of the value's own type.
        pub fn literal_bind(value: BindValue) -> Expr {
            let ty = value_type(&value);
            Expr::Bind(Bind {
                base: "param".to_owned(),
                value,
                ty,
                expanding: false,
            })
        }

        /// `case((when, then), else_=otherwise)` — the condition is grouped.
        pub fn case(when: Expr, then: Expr, otherwise: Expr) -> Expr {
            let when = match when {
                g @ (Expr::Group(_) | Expr::Column { .. } | Expr::Literal(_) | Expr::Bool(_)) => g,
                other => Expr::Group(Box::new(other)),
            };
            Expr::Case {
                when: Box::new(when),
                then: Box::new(then),
                otherwise: Box::new(otherwise),
            }
        }

        /// The SQLAlchemy type of the expression, for compared-value coercion.
        pub fn ty(&self) -> SqlType {
            match self {
                Expr::Column { ty, .. } | Expr::Binary { ty, .. } => *ty,
                Expr::Bind(b) => b.ty,
                Expr::Literal(_) | Expr::Case { .. } | Expr::Scalar(_) => SqlType::Null,
                Expr::And(_)
                | Expr::Or(_)
                | Expr::Not(_)
                | Expr::IsNotNull(_)
                | Expr::In(..)
                | Expr::Bool(_) => SqlType::Boolean,
                Expr::Group(inner) | Expr::Label(_, inner) => inner.ty(),
                Expr::Func { name, args } => match *name {
                    "percent_rank" => SqlType::Numeric(None),
                    "row_number" | "count" => SqlType::Integer,
                    "max" | "min" => args.first().map_or(SqlType::Null, Expr::ty),
                    _ => SqlType::Null,
                },
                Expr::CountStar => SqlType::Integer,
                Expr::Over { func, .. } => func.ty(),
            }
        }

        /// The key an anonymous bind compared to this expression is named after.
        fn bind_base(&self) -> String {
            match self {
                Expr::Column { name, .. } => name.clone(),
                Expr::Func { name, .. } => (*name).to_owned(),
                Expr::Literal(text) => sanitize(text),
                Expr::Label(_, inner) => inner.bind_base(),
                _ => "param".to_owned(),
            }
        }
    }

    /// `left <op> value` — the value bound, typed by compared-value coercion.
    pub fn cmp(left: Expr, op: &'static str, value: BindValue) -> Expr {
        let ty = coerce(left.ty(), &value);
        let bind = Bind {
            base: left.bind_base(),
            value,
            ty,
            expanding: false,
        };
        Expr::Binary {
            left: Box::new(left),
            op,
            right: Box::new(Expr::Bind(bind)),
            ty: SqlType::Boolean,
        }
    }

    /// `left <op> right` between two expressions.
    pub fn cmp_expr(left: Expr, op: &'static str, right: Expr) -> Expr {
        Expr::Binary {
            left: Box::new(left),
            op,
            right: Box::new(right),
            ty: SqlType::Boolean,
        }
    }

    /// `left.in_(values)` — an expanding (post-compile) bind.
    pub fn in_list(left: Expr, values: Vec<String>) -> Expr {
        let bind = Bind {
            base: left.bind_base(),
            value: BindValue::StrList(values),
            ty: left.ty(),
            expanding: true,
        };
        Expr::In(Box::new(left), bind)
    }

    /// `col.op("&")(mask)`, grouped as the left side of a comparison (custom-op precedence).
    pub fn bit_and(left: Expr, mask: i64) -> Expr {
        let value = BindValue::Int(mask);
        let ty = coerce(left.ty(), &value);
        let bind = Bind {
            base: left.bind_base(),
            value,
            ty,
            expanding: false,
        };
        let result_ty = left.ty();
        Expr::Group(Box::new(Expr::Binary {
            left: Box::new(left),
            op: "&",
            right: Box::new(Expr::Bind(bind)),
            ty: result_ty,
        }))
    }

    /// A FROM entry.
    #[derive(Debug, Clone)]
    pub enum FromItem {
        Table(&'static TableDef),
        Cte(Rc<Cte>),
        Join {
            left: Box<FromItem>,
            right: Box<FromItem>,
            on: Expr,
            outer: bool,
        },
    }

    impl FromItem {
        pub fn join(left: FromItem, right: FromItem, on: Expr, outer: bool) -> FromItem {
            FromItem::Join {
                left: Box::new(left),
                right: Box::new(right),
                on,
                outer,
            }
        }
    }

    /// A SELECT.
    #[derive(Debug, Clone)]
    pub struct Select {
        items: Vec<Expr>,
        from: Vec<FromItem>,
        filters: Vec<Expr>,
        order_by: Vec<Order>,
        limit: Option<i64>,
    }

    impl Select {
        pub fn new(items: Vec<Expr>, from: Vec<FromItem>) -> Select {
            Select {
                items,
                from,
                filters: Vec::new(),
                order_by: Vec::new(),
                limit: None,
            }
        }

        /// `.where(*clauses)` — AND-combined.
        pub fn filter(mut self, clauses: Vec<Expr>) -> Select {
            self.filters.extend(clauses);
            self
        }

        pub fn order(mut self, order_by: Vec<Order>) -> Select {
            self.order_by = order_by;
            self
        }

        pub fn limit(mut self, limit: i64) -> Select {
            self.limit = Some(limit);
            self
        }
    }

    /// A named CTE and the columns it exposes.
    #[derive(Debug)]
    pub struct Cte {
        name: String,
        select: Select,
        columns: Vec<(String, SqlType)>,
    }

    impl Cte {
        pub fn new(name: &str, select: Select) -> Rc<Cte> {
            let columns = select
                .items
                .iter()
                .filter_map(|item| match item {
                    Expr::Column { name, ty, .. } => Some((name.clone(), *ty)),
                    Expr::Label(name, inner) => Some((name.clone(), inner.ty())),
                    _ => None,
                })
                .collect();
            Rc::new(Cte {
                name: name.to_owned(),
                select,
                columns,
            })
        }

        pub fn has(&self, name: &str) -> bool {
            self.columns.iter().any(|(n, _)| n == name)
        }

        /// `cte.c[name]` — a miss is Python's `KeyError`.
        pub fn col(&self, name: &str) -> Result<Expr, ScreenError> {
            self.columns
                .iter()
                .find(|(n, _)| n == name)
                .map(|(n, ty)| Expr::Column {
                    qualifier: self.name.clone(),
                    name: n.clone(),
                    ty: *ty,
                })
                .ok_or_else(|| ScreenError::Key(name.to_owned()))
        }

        /// `select(cte)` — every column, in order.
        pub fn all(&self) -> Vec<Expr> {
            self.columns
                .iter()
                .map(|(n, ty)| Expr::Column {
                    qualifier: self.name.clone(),
                    name: n.clone(),
                    ty: *ty,
                })
                .collect()
        }
    }

    /// The PostgreSQL dialect's `max_identifier_length` (its `label_length` is unset).
    const LABEL_LENGTH: usize = 63;

    struct Compiler {
        counters: HashMap<String, usize>,
        truncated: usize,
        positional: usize,
        params: Vec<(String, BindValue)>,
        param_order: Vec<String>,
        ctes: Vec<String>,
        seen: HashSet<String>,
    }

    impl Compiler {
        fn anon(&mut self, base: &str) -> String {
            let n = self.counters.entry(base.to_owned()).or_insert(0);
            *n += 1;
            format!("{base}_{n}")
        }

        /// `_truncated_identifier("bindparam", ...)` then `bindparam_string`'s escaping: an
        /// anonymous name longer than `label_length - 6` (63 - 6 on PostgreSQL) is cut to 57
        /// characters plus `_<hex counter>`; `compiled.params` then keys it with `%():.[] `
        /// translated, while `positiontup` keeps the untranslated name. Returns both.
        fn bind_name(&mut self, base: &str) -> (String, String) {
            let anon = self.anon(base);
            let name = if anon.chars().count() > LABEL_LENGTH - 6 {
                let head: String = anon.chars().take(LABEL_LENGTH - 6).collect();
                let name = format!("{head}_{:x}", self.truncated);
                self.truncated += 1;
                name
            } else {
                anon
            };
            let escaped = name
                .chars()
                .map(|c| match c {
                    '%' => 'P',
                    '(' => 'A',
                    ')' => 'Z',
                    ':' => 'C',
                    '.' | '[' | ']' | ' ' => '_',
                    other => other,
                })
                .collect();
            (name, escaped)
        }

        fn bind(&mut self, b: &Bind) -> String {
            let (raw, name) = self.bind_name(&b.base);
            self.params.push((name.clone(), b.value.clone()));
            self.param_order.push(raw);
            if b.expanding {
                format!("(__[POSTCOMPILE_{name}])")
            } else {
                self.positional += 1;
                format!("${}::{}", self.positional, b.ty.render())
            }
        }

        fn order_item(&mut self, o: &Order, labels: &[&str]) -> String {
            let text = match &o.expr {
                Expr::Label(name, inner) => {
                    if labels.contains(&name.as_str()) {
                        name.clone()
                    } else {
                        self.expr(inner)
                    }
                }
                other => self.expr(other),
            };
            let dir = if o.desc { "DESC" } else { "ASC" };
            let nulls = if o.nulls_last { " NULLS LAST" } else { "" };
            format!("{text} {dir}{nulls}")
        }

        fn expr(&mut self, e: &Expr) -> String {
            match e {
                Expr::Column {
                    qualifier, name, ..
                } => format!("{qualifier}.{name}"),
                Expr::Literal(text) => text.clone(),
                Expr::Bind(b) => self.bind(b),
                Expr::Binary {
                    left, op, right, ..
                } => {
                    let l = self.expr(left);
                    let r = self.expr(right);
                    format!("{l} {op} {r}")
                }
                Expr::And(items) => self.joined(items, " AND "),
                Expr::Or(items) => self.joined(items, " OR "),
                Expr::Not(inner) => format!("NOT {}", self.expr(inner)),
                Expr::Group(inner) => format!("({})", self.expr(inner)),
                Expr::IsNotNull(inner) => format!("{} IS NOT NULL", self.expr(inner)),
                Expr::In(left, bind) => {
                    let l = self.expr(left);
                    let r = self.bind(bind);
                    format!("{l} IN {r}")
                }
                Expr::Func { name, args } => {
                    let args = self.joined(args, ", ");
                    format!("{name}({args})")
                }
                Expr::CountStar => "count(*)".to_owned(),
                Expr::Over {
                    func,
                    partition_by,
                    order_by,
                } => {
                    let f = self.expr(func);
                    let mut parts = Vec::new();
                    if !partition_by.is_empty() {
                        parts.push(format!("PARTITION BY {}", self.joined(partition_by, ", ")));
                    }
                    if !order_by.is_empty() {
                        let items: Vec<String> =
                            order_by.iter().map(|o| self.order_item(o, &[])).collect();
                        parts.push(format!("ORDER BY {}", items.join(", ")));
                    }
                    format!("{f} OVER ({})", parts.join(" "))
                }
                Expr::Case {
                    when,
                    then,
                    otherwise,
                } => {
                    let w = self.expr(when);
                    let t = self.expr(then);
                    let o = self.expr(otherwise);
                    format!("CASE WHEN {w} THEN {t} ELSE {o} END")
                }
                Expr::Bool(b) => if *b { "true" } else { "false" }.to_owned(),
                Expr::Scalar(select) => format!("({})", self.select(select, true)),
                Expr::Label(_, inner) => self.expr(inner),
            }
        }

        fn joined(&mut self, items: &[Expr], sep: &str) -> String {
            let parts: Vec<String> = items.iter().map(|e| self.expr(e)).collect();
            parts.join(sep)
        }

        fn item(&mut self, e: &Expr, nested: bool) -> String {
            match e {
                Expr::Column { name, .. } => {
                    let text = self.expr(e);
                    if nested {
                        format!("{text} AS {name}")
                    } else {
                        text
                    }
                }
                Expr::Label(name, inner) => format!("{} AS {name}", self.expr(inner)),
                Expr::Func { name, .. } if nested => {
                    let text = self.expr(e);
                    let label = self.anon(name);
                    format!("{text} AS {label}")
                }
                other => self.expr(other),
            }
        }

        fn from(&mut self, f: &FromItem) -> String {
            match f {
                FromItem::Table(t) => t.name.to_owned(),
                FromItem::Cte(cte) => {
                    if !self.seen.contains(&cte.name) {
                        let body = self.select(&cte.select, true);
                        self.seen.insert(cte.name.clone());
                        self.ctes.push(format!("{} AS \n({body})", cte.name));
                    }
                    cte.name.clone()
                }
                FromItem::Join {
                    left,
                    right,
                    on,
                    outer,
                } => {
                    let l = self.from(left);
                    let r = self.from(right);
                    let on = self.expr(on);
                    let kind = if *outer { "LEFT OUTER JOIN" } else { "JOIN" };
                    format!("{l} {kind} {r} ON {on}")
                }
            }
        }

        fn select(&mut self, s: &Select, nested: bool) -> String {
            let items: Vec<String> = s.items.iter().map(|e| self.item(e, nested)).collect();
            let froms: Vec<String> = s.from.iter().map(|f| self.from(f)).collect();
            let mut text = format!("SELECT {} \nFROM {}", items.join(", "), froms.join(", "));
            if !s.filters.is_empty() {
                let filters = self.joined(&s.filters, " AND ");
                text.push_str(&format!(" \nWHERE {filters}"));
            }
            if !s.order_by.is_empty() {
                let labels: Vec<&str> = s
                    .items
                    .iter()
                    .filter_map(|e| match e {
                        Expr::Label(name, _) => Some(name.as_str()),
                        _ => None,
                    })
                    .collect();
                let orders: Vec<String> = s
                    .order_by
                    .iter()
                    .map(|o| self.order_item(o, &labels))
                    .collect();
                text.push_str(&format!(" ORDER BY {}", orders.join(", ")));
            }
            if let Some(limit) = s.limit {
                // LIMIT binds like `literal(limit)`: BIGINT once the value needs 32 bits.
                let value = BindValue::Int(limit);
                let bind = Bind {
                    base: "param".to_owned(),
                    ty: value_type(&value),
                    value,
                    expanding: false,
                };
                let rendered = self.bind(&bind);
                text.push_str(&format!(" \n LIMIT {rendered}"));
            }
            text
        }
    }

    /// Render a top-level statement: `WITH <ctes in completion order>\n SELECT ...`.
    pub fn compile_statement(select: &Select) -> CompiledStatement {
        let mut c = Compiler {
            counters: HashMap::new(),
            truncated: 1,
            positional: 0,
            params: Vec::new(),
            param_order: Vec::new(),
            ctes: Vec::new(),
            seen: HashSet::new(),
        };
        let body = c.select(select, false);
        let sql = if c.ctes.is_empty() {
            body
        } else {
            format!("WITH {}\n {body}", c.ctes.join(", \n"))
        };
        CompiledStatement {
            sql,
            params: c.params,
            param_order: c.param_order,
        }
    }
}

// ---------------------------------------------------------------------------
// ScreenDefinition (`baskfy_core.screen_definition`)
// ---------------------------------------------------------------------------

mod definition {
    use chrono::NaiveDate;

    use super::payload::{DecimalValue, Payload, PyDecimal, dumps_sorted, py_float_repr};
    use super::{ErrorDetail, ScreenError, ValidationError, sha256};
    use crate::factor_registry::{self, CUSTOM_FILTER_OPERANDS, py_repr_str};

    const AWAY_FROM_HIGH_IGNORE: i64 = 100;
    const POSITIVE_DAYS_IGNORE: i64 = 0;
    const CIRCUITS_IGNORE_ABOVE: i64 = 250;
    const IGNORE_ABOVE_BETA_IGNORE: i64 = 100;
    const MAX_CUSTOM_FILTERS: usize = 3;
    const MAX_RANKING_TERMS: usize = 8;
    const MAX_FACTOR_RANGES: usize = 10;
    const PERCENT_MAX: i64 = 100;
    const SERIES_VALUES: [&str; 5] = ["EQ", "BE", "SM", "ST", "SZ"];
    const WINDOWS: [&str; 5] = ["m12", "m9", "m6", "m3", "m1"];
    const UNIVERSE_SLUGS: [&str; 15] = [
        "nifty-50",
        "nifty-next-50",
        "nifty-100",
        "nifty-200",
        "nifty-500",
        "nifty-total-market",
        "nifty-large-mid-250",
        "nifty-midcap-150",
        "nifty-smallcap-250",
        "nifty-microcap-250",
        "nifty-mid-small-400",
        "nifty-allcap",
        "nifty-fno",
        "etf",
        "nse-sme-emerge",
    ];
    const SORT_DIRECTIONS: [&str; 2] = ["asc", "desc"];
    const APPLY_FILTERS_ON: [&str; 8] = [
        "all", "decile_1", "decile_2", "decile_3", "decile_4", "decile_5", "top_50", "top_100",
    ];
    const RANKING_MODES: [&str; 3] = ["single", "sequential", "composite"];
    const RANKING_SCOPES: [&str; 3] = ["filtered_results", "fixed_universe", "within_sector"];
    const CUSTOM_FILTER_OPS: [&str; 3] = [">=", "<=", "="];
    const TERM_PREFERENCES: [&str; 3] = ["higher", "lower", "target_range"];
    const MISSING_DATA: [&str; 3] = ["penalize", "neutral", "exclude"];
    const REGIMES: [&str; 3] = ["BULL", "NEUTRAL", "BEAR"];

    /// Eight independent switches, AND-combined (docs/01 §2.3).
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct MovingAverageFilter {
        /// The group switch.
        pub enabled: bool,
        /// Close above MA 200.
        pub above_200: bool,
        /// Close above MA 100.
        pub above_100: bool,
        /// Close above MA 50.
        pub above_50: bool,
        /// Close above MA 20.
        pub above_20: bool,
        /// Close below MA 200.
        pub below_200: bool,
        /// Close below MA 100.
        pub below_100: bool,
        /// Close below MA 50.
        pub below_50: bool,
        /// Close below MA 20.
        pub below_20: bool,
    }

    impl MovingAverageFilter {
        /// `(above_K, below_K)` for K in 200/100/50/20 (false for any other K).
        pub fn switches(&self, length: u32) -> (bool, bool) {
            match length {
                200 => (self.above_200, self.below_200),
                100 => (self.above_100, self.below_100),
                50 => (self.above_50, self.below_50),
                20 => (self.above_20, self.below_20),
                _ => (false, false),
            }
        }

        /// On, with at least one switch set.
        pub fn is_active(&self) -> bool {
            self.enabled
                && [200, 100, 50, 20].iter().any(|&k| {
                    let (a, b) = self.switches(k);
                    a || b
                })
        }
    }

    /// "Within X% of high"; 100 means ignore (docs/01 §2.4).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct AwayFromHighFilter {
        /// All-time-high distance cap.
        pub ath: i64,
        /// 1-year-high distance cap.
        pub one_year: i64,
    }

    impl Default for AwayFromHighFilter {
        fn default() -> Self {
            AwayFromHighFilter {
                ath: AWAY_FROM_HIGH_IGNORE,
                one_year: AWAY_FROM_HIGH_IGNORE,
            }
        }
    }

    impl AwayFromHighFilter {
        /// `ath != 100`.
        pub fn ath_is_active(&self) -> bool {
            self.ath != AWAY_FROM_HIGH_IGNORE
        }

        /// `one_year != 100`.
        pub fn one_year_is_active(&self) -> bool {
            self.one_year != AWAY_FROM_HIGH_IGNORE
        }
    }

    /// Minimum % of up days per window; 0 means ignore (docs/01 §2.5).
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct PositiveDaysFilter {
        /// 12-month.
        pub m12: i64,
        /// 9-month.
        pub m9: i64,
        /// 6-month.
        pub m6: i64,
        /// 3-month.
        pub m3: i64,
        /// 1-month.
        pub m1: i64,
    }

    fn windows(values: [i64; 5]) -> impl Iterator<Item = (&'static str, i64)> {
        WINDOWS.into_iter().zip(values)
    }

    impl PositiveDaysFilter {
        fn values(&self) -> [i64; 5] {
            [self.m12, self.m9, self.m6, self.m3, self.m1]
        }

        /// The windows whose value is not the ignore sentinel, longest first, with the value.
        pub fn active_windows(&self) -> Vec<(&'static str, i64)> {
            windows(self.values())
                .filter(|&(_, v)| v != POSITIVE_DAYS_IGNORE)
                .collect()
        }
    }

    /// Maximum circuit-hit days per window; above 250 means ignore (docs/01 §2.6).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct CircuitsFilter {
        /// 12-month.
        pub m12: i64,
        /// 9-month.
        pub m9: i64,
        /// 6-month.
        pub m6: i64,
        /// 3-month.
        pub m3: i64,
        /// 1-month.
        pub m1: i64,
    }

    impl Default for CircuitsFilter {
        fn default() -> Self {
            CircuitsFilter {
                m12: 999,
                m9: 999,
                m6: 999,
                m3: 999,
                m1: 999,
            }
        }
    }

    impl CircuitsFilter {
        fn values(&self) -> [i64; 5] {
            [self.m12, self.m9, self.m6, self.m3, self.m1]
        }

        /// The windows whose cap can bind (`<= 250`), longest first, with the cap.
        pub fn active_windows(&self) -> Vec<(&'static str, i64)> {
            windows(self.values())
                .filter(|&(_, v)| v <= CIRCUITS_IGNORE_ABOVE)
                .collect()
        }
    }

    /// An inclusive `[from, to]` band; both `None` means ignore.
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct RangeFilter {
        /// Lower bound (JSON key `from`).
        pub from: Option<DecimalValue>,
        /// Upper bound.
        pub to: Option<DecimalValue>,
    }

    impl RangeFilter {
        /// Either bound set.
        pub fn is_active(&self) -> bool {
            self.from.is_some() || self.to.is_some()
        }
    }

    /// The P/E band behind its own switch (docs/01 §2.8).
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct PeFilter {
        /// Lower bound (JSON key `from`).
        pub from: Option<DecimalValue>,
        /// Upper bound.
        pub to: Option<DecimalValue>,
        /// The switch.
        pub enabled: bool,
    }

    impl PeFilter {
        /// Switched on with a bound.
        pub fn is_active(&self) -> bool {
            self.enabled && (self.from.is_some() || self.to.is_some())
        }
    }

    /// "Ignore Top Beta / Top Volatility" — a precomputed per-universe flag.
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct TopRiskFilter {
        /// The switch.
        pub enabled: bool,
        /// The count shown in the UI.
        pub count: i64,
    }

    impl TopRiskFilter {
        /// `enabled`.
        pub fn is_active(&self) -> bool {
            self.enabled
        }
    }

    /// Factor two / factor three of the combined ranking (docs/01 §2.12).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct ExtraFactor {
        /// The switch.
        pub enabled: bool,
        /// The registry key.
        pub sort_by: Option<String>,
        /// `asc` or `desc`.
        pub sort_direction: String,
    }

    impl Default for ExtraFactor {
        fn default() -> Self {
            ExtraFactor {
                enabled: false,
                sort_by: None,
                sort_direction: "desc".to_owned(),
            }
        }
    }

    impl ExtraFactor {
        /// Enabled with a key.
        pub fn is_active(&self) -> bool {
            self.enabled && self.sort_by.is_some()
        }
    }

    /// A field-to-field comparison (docs/01 §2.14).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct CustomFilter {
        /// The slot switch.
        pub enabled: bool,
        /// Left operand key.
        pub left: String,
        /// `>=`, `<=` or `=`.
        pub op: String,
        /// Right operand key.
        pub right: String,
    }

    impl CustomFilter {
        /// `enabled`.
        pub fn is_active(&self) -> bool {
            self.enabled
        }
    }

    /// One explicit ranking term (docs/ranking/PLAN.md C3).
    #[derive(Debug, Clone)]
    pub struct RankingTerm {
        /// Registry key.
        pub factor: String,
        /// `higher`, `lower` or `target_range`.
        pub preference: String,
        /// Weight in `(0, 100]`.
        pub weight: f64,
        /// Lower target.
        pub target_min: Option<f64>,
        /// Upper target.
        pub target_max: Option<f64>,
    }

    /// An inclusive `[min, max]` eligibility filter on one factor (C3).
    #[derive(Debug, Clone)]
    pub struct FactorRange {
        /// The switch.
        pub enabled: bool,
        /// Registry key.
        pub factor: String,
        /// Lower bound.
        pub min: Option<f64>,
        /// Upper bound.
        pub max: Option<f64>,
    }

    impl FactorRange {
        /// `enabled`.
        pub fn is_active(&self) -> bool {
            self.enabled
        }
    }

    /// Relative weight per weight family for `composite` (C3/C4).
    #[derive(Debug, Clone, Default)]
    pub struct FamilyWeights {
        /// momentum.
        pub momentum: Option<f64>,
        /// path_quality.
        pub path_quality: Option<f64>,
        /// trend_structure.
        pub trend_structure: Option<f64>,
        /// participation.
        pub participation: Option<f64>,
        /// risk_execution.
        pub risk_execution: Option<f64>,
    }

    impl FamilyWeights {
        /// `as_mapping()` in family order.
        pub fn as_mapping(&self) -> [(&'static str, Option<f64>); 5] {
            [
                ("momentum", self.momentum),
                ("path_quality", self.path_quality),
                ("trend_structure", self.trend_structure),
                ("participation", self.participation),
                ("risk_execution", self.risk_execution),
            ]
        }
    }

    /// The persisted screen configuration (docs/04 §Screens). Literal-typed fields are strings
    /// so that a value smuggled past the model (`model_copy`) reaches the builder's own gate.
    #[derive(Debug, Clone)]
    pub struct ScreenDefinition {
        /// Universe slug.
        pub index: String,
        /// Factor one.
        pub sort_by: String,
        /// `asc` / `desc`.
        pub sort_direction: String,
        /// The bucket.
        pub apply_filters_on: String,
        /// `single` / `sequential` / `composite`.
        pub ranking_mode: String,
        /// `filtered_results` / `fixed_universe` / `within_sector`.
        pub ranking_scope: String,
        /// Minimum 1-year return.
        pub min_return_1y: Option<DecimalValue>,
        /// Minimum median daily traded value over 1 year, in rupees.
        pub median_volume_1y: Option<i64>,
        /// MA switches.
        pub moving_average: MovingAverageFilter,
        /// Distance from high.
        pub away_from_high: AwayFromHighFilter,
        /// Positive days.
        pub positive_days: PositiveDaysFilter,
        /// Circuit caps.
        pub circuits: CircuitsFilter,
        /// Marketcap band.
        pub marketcap: RangeFilter,
        /// P/E band.
        pub pe: PeFilter,
        /// NSE series.
        pub series: Vec<String>,
        /// Ignore top beta.
        pub ignore_top_beta: TopRiskFilter,
        /// Ignore top volatility.
        pub ignore_top_volatility: TopRiskFilter,
        /// Beta ceiling; 100 means ignore.
        pub ignore_above_beta: i64,
        /// Price band.
        pub price: RangeFilter,
        /// Factor two.
        pub factor_two: ExtraFactor,
        /// Factor three.
        pub factor_three: ExtraFactor,
        /// Historical date.
        pub historical_date: Option<NaiveDate>,
        /// Custom filters.
        pub custom_filters: Vec<CustomFilter>,
        /// Explicit ranking terms (Phase 2).
        pub ranking_terms: Vec<RankingTerm>,
        /// Family weights (Phase 2).
        pub family_weights: Option<FamilyWeights>,
        /// Missing-data policy (Phase 2).
        pub missing_data: String,
        /// Factor ranges (Phase 2).
        pub factor_ranges: Vec<FactorRange>,
        /// Regime filter (Phase 2).
        pub regime_in: Option<Vec<String>>,
    }

    impl ScreenDefinition {
        /// A definition with every default, as `ScreenDefinition(index=..., sort_by=...)`.
        pub fn new(index: &str, sort_by: &str) -> ScreenDefinition {
            ScreenDefinition {
                index: index.to_owned(),
                sort_by: sort_by.to_owned(),
                sort_direction: "desc".to_owned(),
                apply_filters_on: "all".to_owned(),
                ranking_mode: "composite".to_owned(),
                ranking_scope: "filtered_results".to_owned(),
                min_return_1y: None,
                median_volume_1y: None,
                moving_average: MovingAverageFilter::default(),
                away_from_high: AwayFromHighFilter::default(),
                positive_days: PositiveDaysFilter::default(),
                circuits: CircuitsFilter::default(),
                marketcap: RangeFilter::default(),
                pe: PeFilter::default(),
                series: vec!["EQ".to_owned()],
                ignore_top_beta: TopRiskFilter::default(),
                ignore_top_volatility: TopRiskFilter::default(),
                ignore_above_beta: IGNORE_ABOVE_BETA_IGNORE,
                price: RangeFilter::default(),
                factor_two: ExtraFactor::default(),
                factor_three: ExtraFactor::default(),
                historical_date: None,
                custom_filters: Vec::new(),
                ranking_terms: Vec::new(),
                family_weights: None,
                missing_data: "penalize".to_owned(),
                factor_ranges: Vec::new(),
                regime_in: None,
            }
        }

        /// `ignore_above_beta != 100`.
        pub fn ignore_above_beta_is_active(&self) -> bool {
            self.ignore_above_beta != IGNORE_ABOVE_BETA_IGNORE
        }

        /// Factor one, plus any active extras, in the order their ranks are summed.
        pub fn ranking_factors(&self) -> Vec<(String, String)> {
            let mut out = vec![(self.sort_by.clone(), self.sort_direction.clone())];
            for extra in [&self.factor_two, &self.factor_three] {
                if let (true, Some(key)) = (extra.is_active(), extra.sort_by.as_ref()) {
                    out.push((key.clone(), extra.sort_direction.clone()));
                }
            }
            out
        }

        /// `model_dump(mode="json", by_alias=True)` minus the Phase-2 fields at their default.
        pub fn json_payload(&self) -> Payload {
            let dec = |d: &Option<DecimalValue>| match d {
                Some(v) => Payload::Str(v.py_str()),
                None => Payload::Null,
            };
            let float = |f: Option<f64>| f.map_or(Payload::Null, Payload::Float);
            let s = |t: &str| Payload::Str(t.to_owned());
            let b = Payload::Bool;
            let i = Payload::Int;
            let map = |entries: Vec<(&str, Payload)>| {
                Payload::Map(
                    entries
                        .into_iter()
                        .map(|(k, v)| (k.to_owned(), v))
                        .collect(),
                )
            };
            let ma = &self.moving_average;
            let range = |r: &RangeFilter| map(vec![("from", dec(&r.from)), ("to", dec(&r.to))]);
            let extra = |e: &ExtraFactor| {
                map(vec![
                    ("enabled", b(e.enabled)),
                    ("sort_by", e.sort_by.as_deref().map_or(Payload::Null, s)),
                    ("sort_direction", s(&e.sort_direction)),
                ])
            };
            let risk =
                |r: &TopRiskFilter| map(vec![("enabled", b(r.enabled)), ("count", i(r.count))]);
            let five =
                |v: [i64; 5]| map(WINDOWS.iter().copied().zip(v.into_iter().map(i)).collect());
            let mut entries: Vec<(&str, Payload)> = vec![
                ("index", s(&self.index)),
                ("sort_by", s(&self.sort_by)),
                ("sort_direction", s(&self.sort_direction)),
                ("apply_filters_on", s(&self.apply_filters_on)),
                ("ranking_mode", s(&self.ranking_mode)),
                ("ranking_scope", s(&self.ranking_scope)),
                ("min_return_1y", dec(&self.min_return_1y)),
                (
                    "median_volume_1y",
                    self.median_volume_1y.map_or(Payload::Null, i),
                ),
                (
                    "moving_average",
                    map(vec![
                        ("enabled", b(ma.enabled)),
                        ("above_200", b(ma.above_200)),
                        ("above_100", b(ma.above_100)),
                        ("above_50", b(ma.above_50)),
                        ("above_20", b(ma.above_20)),
                        ("below_200", b(ma.below_200)),
                        ("below_100", b(ma.below_100)),
                        ("below_50", b(ma.below_50)),
                        ("below_20", b(ma.below_20)),
                    ]),
                ),
                (
                    "away_from_high",
                    map(vec![
                        ("ath", i(self.away_from_high.ath)),
                        ("one_year", i(self.away_from_high.one_year)),
                    ]),
                ),
                ("positive_days", five(self.positive_days.values())),
                ("circuits", five(self.circuits.values())),
                ("marketcap", range(&self.marketcap)),
                (
                    "pe",
                    map(vec![
                        ("from", dec(&self.pe.from)),
                        ("to", dec(&self.pe.to)),
                        ("enabled", b(self.pe.enabled)),
                    ]),
                ),
                (
                    "series",
                    Payload::List(self.series.iter().map(|x| s(x)).collect()),
                ),
                ("ignore_top_beta", risk(&self.ignore_top_beta)),
                ("ignore_top_volatility", risk(&self.ignore_top_volatility)),
                ("ignore_above_beta", i(self.ignore_above_beta)),
                ("price", range(&self.price)),
                ("factor_two", extra(&self.factor_two)),
                ("factor_three", extra(&self.factor_three)),
                (
                    "historical_date",
                    self.historical_date
                        .map_or(Payload::Null, |d| s(&d.format("%Y-%m-%d").to_string())),
                ),
                (
                    "custom_filters",
                    Payload::List(
                        self.custom_filters
                            .iter()
                            .map(|c| {
                                map(vec![
                                    ("enabled", b(c.enabled)),
                                    ("left", s(&c.left)),
                                    ("op", s(&c.op)),
                                    ("right", s(&c.right)),
                                ])
                            })
                            .collect(),
                    ),
                ),
            ];
            // Phase-2 fields are dropped while they hold their default (PHASE_2_DEFAULTS), so
            // every definition saved before they existed keeps its hash.
            if !self.ranking_terms.is_empty() {
                entries.push((
                    "ranking_terms",
                    Payload::List(
                        self.ranking_terms
                            .iter()
                            .map(|t| {
                                map(vec![
                                    ("factor", s(&t.factor)),
                                    ("preference", s(&t.preference)),
                                    ("weight", Payload::Float(t.weight)),
                                    ("target_min", float(t.target_min)),
                                    ("target_max", float(t.target_max)),
                                ])
                            })
                            .collect(),
                    ),
                ));
            }
            if let Some(w) = &self.family_weights {
                entries.push((
                    "family_weights",
                    map(w
                        .as_mapping()
                        .into_iter()
                        .map(|(k, v)| (k, float(v)))
                        .collect()),
                ));
            }
            if self.missing_data != "penalize" {
                entries.push(("missing_data", s(&self.missing_data)));
            }
            if !self.factor_ranges.is_empty() {
                entries.push((
                    "factor_ranges",
                    Payload::List(
                        self.factor_ranges
                            .iter()
                            .map(|r| {
                                map(vec![
                                    ("enabled", b(r.enabled)),
                                    ("factor", s(&r.factor)),
                                    ("min", float(r.min)),
                                    ("max", float(r.max)),
                                ])
                            })
                            .collect(),
                    ),
                ));
            }
            if let Some(regimes) = &self.regime_in {
                entries.push((
                    "regime_in",
                    Payload::List(regimes.iter().map(|x| s(x)).collect()),
                ));
            }
            map(entries)
        }

        /// A byte-stable serialisation: JSON mode, aliases, defaults materialised (Phase-2
        /// fields dropped at their default), keys sorted recursively, no whitespace.
        pub fn canonical_json(&self) -> String {
            let mut out = String::new();
            dumps_sorted(&self.json_payload(), &mut out);
            out
        }

        /// sha256 of [`ScreenDefinition::canonical_json`].
        pub fn definition_hash(&self) -> String {
            sha256::hex_digest(self.canonical_json().as_bytes())
        }

        /// `ScreenDefinition.model_validate(payload)` — pydantic's lax Python-mode validation,
        /// field validators and model validators, with pydantic's error types and messages.
        pub fn model_validate(input: &Payload) -> Result<ScreenDefinition, ScreenError> {
            let mut v = V { errors: Vec::new() };
            let out = screen_definition(&mut v, input);
            match out {
                Some(d) if v.errors.is_empty() => Ok(d),
                _ => Err(ScreenError::Validation(ValidationError {
                    errors: v.errors,
                })),
            }
        }
    }

    // --- pydantic emulation ------------------------------------------------------

    struct V {
        errors: Vec<ErrorDetail>,
    }

    impl V {
        fn err(&mut self, kind: &str, loc: &[String], msg: impl Into<String>) {
            self.errors.push(ErrorDetail {
                kind: kind.to_owned(),
                loc: loc.to_vec(),
                msg: msg.into(),
            });
        }

        fn value_error(&mut self, loc: &[String], msg: &str) {
            self.err("value_error", loc, format!("Value error, {msg}"));
        }
    }

    fn at(loc: &[String], key: impl Into<String>) -> Vec<String> {
        let mut out = loc.to_vec();
        out.push(key.into());
        out
    }

    fn py_list_repr<S: AsRef<str>>(items: &[S]) -> String {
        let parts: Vec<String> = items.iter().map(|s| py_repr_str(s.as_ref())).collect();
        format!("[{}]", parts.join(", "))
    }

    /// A model's input dict: field lookup by alias/name and the extra-key check.
    struct Obj<'a> {
        entries: &'a [(String, Payload)],
        loc: Vec<String>,
        known: Vec<&'static str>,
    }

    impl<'a> Obj<'a> {
        fn open(v: &mut V, input: &'a Payload, loc: &[String], model: &str) -> Option<Obj<'a>> {
            match input {
                Payload::Map(entries) => Some(Obj {
                    entries,
                    loc: loc.to_vec(),
                    known: Vec::new(),
                }),
                _ => {
                    v.err(
                        "model_type",
                        loc,
                        format!("Input should be a valid dictionary or instance of {model}"),
                    );
                    None
                }
            }
        }

        /// The value under `name` (alias first, then the field name) and the key it was found
        /// under, or `None` when absent. As in pydantic, a field found by its alias leaves the
        /// field-name key unclaimed, so a second spelling of the same field is
        /// `extra_forbidden`; an error's loc names the key actually used.
        fn get(
            &mut self,
            name: &'static str,
            alias: Option<&'static str>,
        ) -> Option<(&'a Payload, &'static str)> {
            if let Some(a) = alias {
                self.known.push(a);
                if let Some((_, v)) = self.entries.iter().find(|(k, _)| k == a) {
                    return Some((v, a));
                }
            }
            self.known.push(name);
            self.entries
                .iter()
                .find(|(k, _)| k == name)
                .map(|(_, v)| (v, name))
        }

        fn field_loc(&self, name: &str) -> Vec<String> {
            at(&self.loc, name)
        }

        /// A required field: `missing` when absent.
        fn required(
            &mut self,
            v: &mut V,
            name: &'static str,
        ) -> Option<(&'a Payload, Vec<String>)> {
            let loc = self.field_loc(name);
            match self.get(name, None) {
                Some((p, _)) => Some((p, loc)),
                None => {
                    v.err("missing", &loc, "Field required");
                    None
                }
            }
        }

        fn finish(&self, v: &mut V) {
            for (k, _) in self.entries {
                if !self.known.iter().any(|n| n == k) {
                    v.err(
                        "extra_forbidden",
                        &at(&self.loc, k.clone()),
                        "Extra inputs are not permitted",
                    );
                }
            }
        }
    }

    fn literal_msg(allowed: &[&str]) -> String {
        let quoted: Vec<String> = allowed.iter().map(|a| format!("'{a}'")).collect();
        match quoted.split_last() {
            Some((last, rest)) if !rest.is_empty() => {
                format!("Input should be {} or {last}", rest.join(", "))
            }
            Some((last, _)) => format!("Input should be {last}"),
            None => "Input should be a valid literal".to_owned(),
        }
    }

    fn p_str(v: &mut V, p: &Payload, loc: &[String]) -> Option<String> {
        match p {
            Payload::Str(s) => Some(s.clone()),
            _ => {
                v.err("string_type", loc, "Input should be a valid string");
                None
            }
        }
    }

    fn p_literal(v: &mut V, p: &Payload, loc: &[String], allowed: &[&str]) -> Option<String> {
        match p {
            Payload::Str(s) if allowed.contains(&s.as_str()) => Some(s.clone()),
            _ => {
                v.err("literal_error", loc, literal_msg(allowed));
                None
            }
        }
    }

    /// `FactorKey`: a str of 1-64 chars matching `^[a-z][a-z0-9_]*$`.
    fn p_key(v: &mut V, p: &Payload, loc: &[String]) -> Option<String> {
        let s = p_str(v, p, loc)?;
        let n = s.chars().count();
        if n < 1 {
            v.err(
                "string_too_short",
                loc,
                "String should have at least 1 character",
            );
            return None;
        }
        if n > 64 {
            v.err(
                "string_too_long",
                loc,
                "String should have at most 64 characters",
            );
            return None;
        }
        let mut chars = s.chars();
        let ok = chars.next().is_some_and(|c| c.is_ascii_lowercase())
            && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_');
        if !ok {
            v.err(
                "string_pattern_mismatch",
                loc,
                "String should match pattern '^[a-z][a-z0-9_]*$'",
            );
            return None;
        }
        Some(s)
    }

    fn p_bool(v: &mut V, p: &Payload, loc: &[String]) -> Option<bool> {
        let parsing = "Input should be a valid boolean, unable to interpret input";
        match p {
            Payload::Bool(b) => Some(*b),
            Payload::Int(0) => Some(false),
            Payload::Int(1) => Some(true),
            Payload::Int(_) => {
                v.err("bool_parsing", loc, parsing);
                None
            }
            Payload::Float(f) if *f == 0.0 => Some(false),
            #[allow(clippy::float_cmp)] // pydantic accepts exactly 1.0
            Payload::Float(f) if *f == 1.0 => Some(true),
            // Another whole number inside i64 cannot be interpreted; a fraction, a number past
            // i64, nan or inf is no bool at all.
            Payload::Float(f)
                if f.is_finite() && f.fract() == 0.0 && f.abs() < 9_223_372_036_854_775_808.0 =>
            {
                v.err("bool_parsing", loc, parsing);
                None
            }
            Payload::Decimal(PyDecimal::Finite(d)) => match d.integral_i64() {
                Ok(Some(0)) => Some(false),
                Ok(Some(1)) => Some(true),
                Ok(_) => {
                    v.err("bool_parsing", loc, parsing);
                    None
                }
                Err(()) => {
                    v.err("bool_type", loc, "Input should be a valid boolean");
                    None
                }
            },
            Payload::Str(s) => match s.to_ascii_lowercase().as_str() {
                "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
                "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
                _ => {
                    v.err("bool_parsing", loc, parsing);
                    None
                }
            },
            _ => {
                v.err("bool_type", loc, "Input should be a valid boolean");
                None
            }
        }
    }

    /// pydantic's `int` from a float: integral and inside i64, else the matching error.
    fn int_from_float(v: &mut V, f: f64, loc: &[String]) -> Option<i64> {
        if !f.is_finite() {
            v.err("finite_number", loc, "Input should be a finite number");
            return None;
        }
        if f.fract() != 0.0 {
            v.err(
                "int_from_float",
                loc,
                "Input should be a valid integer, got a number with a fractional part",
            );
            return None;
        }
        if f.abs() >= 9_223_372_036_854_775_808.0 {
            v.err(
                "int_parsing_size",
                loc,
                "Unable to parse input string as an integer, exceeded maximum size",
            );
            return None;
        }
        // Integral and inside i64's range, so the cast is exact.
        #[allow(clippy::cast_possible_truncation)]
        Some(f as i64)
    }

    fn p_int(v: &mut V, p: &Payload, loc: &[String]) -> Option<i64> {
        match p {
            Payload::Int(i) => Some(*i),
            Payload::Bool(b) => Some(i64::from(*b)),
            Payload::Float(f) => int_from_float(v, *f, loc),
            Payload::Str(s) => match int_text(s) {
                Ok(i) => Some(i),
                Err(IntText::Invalid) => {
                    v.err(
                        "int_parsing",
                        loc,
                        "Input should be a valid integer, unable to parse string as an integer",
                    );
                    None
                }
                Err(IntText::TooWide) => {
                    v.err(
                        "int_parsing_size",
                        loc,
                        "Unable to parse input string as an integer, exceeded maximum size",
                    );
                    None
                }
            },
            Payload::Decimal(PyDecimal::Finite(d)) => match d.integral_i64() {
                Ok(Some(i)) => Some(i),
                Ok(None) => {
                    v.err(
                        "int_parsing_size",
                        loc,
                        "Unable to parse input string as an integer, exceeded maximum size",
                    );
                    None
                }
                Err(()) => {
                    v.err(
                        "int_from_float",
                        loc,
                        "Input should be a valid integer, got a number with a fractional part",
                    );
                    None
                }
            },
            Payload::Decimal(PyDecimal::NonFinite(_)) => {
                v.err("finite_number", loc, "Input should be a finite number");
                None
            }
            _ => {
                v.err("int_type", loc, "Input should be a valid integer");
                None
            }
        }
    }

    enum IntText {
        Invalid,
        TooWide,
    }

    /// pydantic-core's `int` from a string: trimmed; an optional `.000…` tail (at least one
    /// zero); a sign; digits with single underscores strictly between digits.
    fn int_text(s: &str) -> Result<i64, IntText> {
        let t = s.trim();
        let t = match t.split_once('.') {
            Some((head, tail)) if !tail.is_empty() && tail.bytes().all(|b| b == b'0') => head,
            Some(_) => return Err(IntText::Invalid),
            None => t,
        };
        let (negative, digits) = match t.as_bytes().first() {
            Some(b'-') => (true, &t[1..]),
            Some(b'+') => (false, &t[1..]),
            _ => (false, t),
        };
        let bytes = digits.as_bytes();
        let well_formed = bytes.first().is_some_and(u8::is_ascii_digit)
            && bytes.last().is_some_and(u8::is_ascii_digit)
            && bytes.iter().all(|b| b.is_ascii_digit() || *b == b'_')
            && !digits.contains("__");
        if !well_formed {
            return Err(IntText::Invalid);
        }
        let plain: String = digits.chars().filter(|c| *c != '_').collect();
        let magnitude: i128 = plain.parse().map_err(|_| IntText::TooWide)?;
        let value = if negative { -magnitude } else { magnitude };
        i64::try_from(value).map_err(|_| IntText::TooWide)
    }

    /// pydantic-core's `float` from a string: Rust's float grammar on the trimmed text, then
    /// once more on the *untrimmed* text with underscores removed when none leads, trails or
    /// doubles (so `" 1_0 "` is refused while `"1_0"` is 10.0).
    fn float_text(s: &str) -> Option<f64> {
        if let Ok(f) = s.trim().parse::<f64>() {
            return Some(f);
        }
        if s.starts_with('_') || s.ends_with('_') || s.contains("__") {
            return None;
        }
        s.replace('_', "").parse::<f64>().ok()
    }

    struct FloatBounds {
        gt: Option<f64>,
        ge: Option<f64>,
        le: Option<f64>,
    }

    const NO_BOUNDS: FloatBounds = FloatBounds {
        gt: None,
        ge: None,
        le: None,
    };

    fn p_float(v: &mut V, p: &Payload, loc: &[String], bounds: &FloatBounds) -> Option<f64> {
        let f = match p {
            Payload::Float(f) => *f,
            // Python's float(int) — exact up to 2**53, rounded beyond, as Python rounds.
            #[allow(clippy::cast_precision_loss)]
            Payload::Int(i) => *i as f64,
            Payload::Bool(b) => f64::from(u8::from(*b)),
            Payload::Decimal(d) => match d {
                PyDecimal::Finite(x) => x.py_str().parse::<f64>().unwrap_or(f64::NAN),
                PyDecimal::NonFinite(_) => f64::NAN,
            },
            Payload::Str(s) => {
                if let Some(f) = float_text(s) {
                    f
                } else {
                    v.err(
                        "float_parsing",
                        loc,
                        "Input should be a valid number, unable to parse string as a number",
                    );
                    return None;
                }
            }
            _ => {
                v.err("float_type", loc, "Input should be a valid number");
                return None;
            }
        };
        if !f.is_finite() {
            v.err("finite_number", loc, "Input should be a finite number");
            return None;
        }
        let show = |b: f64| {
            let r = py_float_repr(b);
            r.strip_suffix(".0").map(str::to_owned).unwrap_or(r)
        };
        if let Some(gt) = bounds.gt
            && f <= gt
        {
            v.err(
                "greater_than",
                loc,
                format!("Input should be greater than {}", show(gt)),
            );
            return None;
        }
        if let Some(ge) = bounds.ge
            && f < ge
        {
            v.err(
                "greater_than_equal",
                loc,
                format!("Input should be greater than or equal to {}", show(ge)),
            );
            return None;
        }
        if let Some(le) = bounds.le
            && f > le
        {
            v.err(
                "less_than_equal",
                loc,
                format!("Input should be less than or equal to {}", show(le)),
            );
            return None;
        }
        Some(f)
    }

    fn p_decimal(v: &mut V, p: &Payload, loc: &[String]) -> Option<DecimalValue> {
        let parsed = match p {
            Payload::Decimal(d) => Some(d.clone()),
            Payload::Int(i) => PyDecimal::parse(&i.to_string()),
            Payload::Float(f) => PyDecimal::parse(&py_float_repr(*f)),
            Payload::Str(s) => PyDecimal::parse(s),
            _ => {
                v.err(
                    "decimal_type",
                    loc,
                    "Decimal input should be an integer, float, string or Decimal object",
                );
                return None;
            }
        };
        match parsed {
            Some(PyDecimal::Finite(d)) => Some(d),
            Some(PyDecimal::NonFinite(_)) => {
                v.err("finite_number", loc, "Input should be a finite number");
                None
            }
            None => {
                v.err("decimal_parsing", loc, "Input should be a valid decimal");
                None
            }
        }
    }

    /// A date field in pydantic's lax mode (pydantic-core 2.46.4 over speedate 0.17.0): a
    /// `date` as is; a string as an exact `YYYY-MM-DD`, else an integer timestamp at midnight,
    /// else a datetime (RFC 3339 or a numeric timestamp) whose time is zero; an int or float as a
    /// timestamp (milliseconds above 2e10). Errors carry speedate's own messages.
    fn p_date(v: &mut V, p: &Payload, loc: &[String]) -> Option<NaiveDate> {
        let result = match p {
            Payload::Date(d) => return Some(*d),
            Payload::Str(s) => speedate::date_from_str(s.as_bytes()),
            Payload::Int(i) => speedate::date_from_datetime(speedate::from_timestamp(*i, 0)),
            Payload::Float(f) => speedate::date_from_datetime(speedate::from_float(*f)),
            // Python's `float(Decimal)`, as pydantic extracts it.
            Payload::Decimal(d) => {
                let f = match d {
                    PyDecimal::Finite(x) => x.py_str().parse::<f64>().unwrap_or(f64::NAN),
                    PyDecimal::NonFinite(t) if t.contains("NaN") => f64::NAN,
                    PyDecimal::NonFinite(t) if t.starts_with('-') => f64::NEG_INFINITY,
                    PyDecimal::NonFinite(_) => f64::INFINITY,
                };
                speedate::date_from_datetime(speedate::from_float(f))
            }
            _ => {
                v.err("date_type", loc, "Input should be a valid date");
                return None;
            }
        };
        match result {
            Ok(date) => Some(date),
            Err(speedate::DateError::Parsing(why)) => {
                v.err(
                    "date_from_datetime_parsing",
                    loc,
                    format!("Input should be a valid date or datetime, {why}"),
                );
                None
            }
            Err(speedate::DateError::Inexact) => {
                v.err(
                    "date_from_datetime_inexact",
                    loc,
                    "Datetimes provided to dates should have zero time - e.g. be exact dates",
                );
                None
            }
            Err(speedate::DateError::YearZero) => {
                v.err(
                    "date_parsing",
                    loc,
                    "Input should be a valid date in the format YYYY-MM-DD, year 0 is out of range",
                );
                None
            }
        }
    }

    /// The parts of speedate 0.17.0 a lax `date` field reaches, reproduced byte for byte.
    mod speedate {
        use chrono::NaiveDate;

        /// Why a date was not produced.
        pub(super) enum DateError {
            /// speedate's error text (`date_from_datetime_parsing`).
            Parsing(&'static str),
            /// A datetime whose time is not midnight.
            Inexact,
            /// speedate accepts year 0; Python's `date` does not.
            YearZero,
        }

        const MS_WATERSHED: i64 = 20_000_000_000;
        const UNIX_9999: i64 = 253_402_300_799;
        const UNIX_0000: i64 = -62_167_219_200;

        const TOO_SHORT: &str = "input is too short";
        const EXTRA: &str = "unexpected extra characters at the end of the input";
        const DATETIME_SEP: &str = "invalid datetime separator, expected `T`, `t`, `_` or space";
        const DATE_SEP: &str = "invalid date separator, expected `-`";
        const TOO_SMALL: &str = "dates before 0000 are not supported as unix timestamps";
        const TOO_LARGE: &str = "dates after 9999 are not supported as unix timestamps";
        const TIME_TOO_LARGE: &str = "numeric times may not exceed 86,399 seconds";

        /// speedate's `(year, month, day)` (year 0 allowed) and seconds into the day.
        type Ymd = (u16, u8, u8);

        fn digit(bytes: &[u8], i: usize, err: &'static str) -> Result<u8, &'static str> {
            match bytes.get(i) {
                Some(c) if c.is_ascii_digit() => Ok(c - b'0'),
                _ => Err(err),
            }
        }

        /// `Date::parse_bytes_partial`.
        fn date_partial(b: &[u8]) -> Result<Ymd, &'static str> {
            if b.len() < 10 {
                return Err(TOO_SHORT);
            }
            let year_err = "invalid character in year";
            let mut year: u16 = 0;
            for i in 0..4 {
                year = year * 10 + u16::from(digit(b, i, year_err)?);
            }
            if b[4] != b'-' {
                return Err(DATE_SEP);
            }
            let month_err = "invalid character in month";
            let month = digit(b, 5, month_err)? * 10 + digit(b, 6, month_err)?;
            if b[7] != b'-' {
                return Err(DATE_SEP);
            }
            let day_err = "invalid character in day";
            let day = digit(b, 8, day_err)? * 10 + digit(b, 9, day_err)?;
            let leap =
                year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
            let max_days = match month {
                1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
                4 | 6 | 9 | 11 => 30,
                2 if leap => 29,
                2 => 28,
                _ => return Err("month value is outside expected range of 1-12"),
            };
            if day < 1 || day > max_days {
                return Err("day value is outside expected range");
            }
            Ok((year, month, day))
        }

        /// `int_parse_bytes_internal`, wrapping arithmetic and its once-per-digit overflow
        /// check included. `Err(Some(byte))` names the byte that stopped it.
        fn int_parse(s: &[u8]) -> Result<i64, Option<u8>> {
            let (negative, first, rest) = match s {
                [b'-', first, rest @ ..] => (true, *first, rest),
                [b'+', first, rest @ ..] | [first, rest @ ..] => (false, *first, rest),
                [] => return Err(None),
            };
            let decode = |c: u8| {
                if c.is_ascii_digit() {
                    Ok(i64::from(c - b'0'))
                } else {
                    Err(Some(c))
                }
            };
            let mut value = decode(first)?;
            for &c in rest {
                value = value.wrapping_mul(10).wrapping_add(decode(c)?);
                if value < 0 {
                    return Err(Some(c));
                }
            }
            Ok(if negative {
                value.wrapping_neg()
            } else {
                value
            })
        }

        enum IntFloat {
            Int(i64),
            Float(f64),
            Err,
        }

        /// `float_parse_bytes`: an integer, else (only when the integer stopped at a `.`) a
        /// float in lexical's standard grammar.
        fn float_parse(s: &[u8]) -> IntFloat {
            match int_parse(s) {
                Ok(i) => IntFloat::Int(i),
                Err(Some(b'.')) => match std::str::from_utf8(s).ok().and_then(standard_float) {
                    Some(f) => IntFloat::Float(f),
                    None => IntFloat::Err,
                },
                Err(_) => IntFloat::Err,
            }
        }

        /// lexical's `STANDARD` float: sign, digits, point, digits (at least one in all), an
        /// optional exponent; no specials, no whitespace.
        fn standard_float(text: &str) -> Option<f64> {
            let body = text.strip_prefix(['+', '-']).unwrap_or(text);
            let (mantissa, exponent) = match body.find(['e', 'E']) {
                Some(i) => (&body[..i], Some(&body[i + 1..])),
                None => (body, None),
            };
            let digits_ok = mantissa.bytes().all(|b| b.is_ascii_digit() || b == b'.')
                && mantissa.bytes().filter(|b| *b == b'.').count() <= 1
                && mantissa.bytes().any(|b| b.is_ascii_digit());
            let exponent_ok = exponent.is_none_or(|e| {
                let e = e.strip_prefix(['+', '-']).unwrap_or(e);
                !e.is_empty() && e.bytes().all(|b| b.is_ascii_digit())
            });
            if !(digits_ok && exponent_ok) {
                return None;
            }
            text.parse::<f64>().ok()
        }

        /// `timestamp_watershed`: seconds, or milliseconds when |t| > 2e10.
        fn watershed(t: i64) -> Result<(i64, u32), &'static str> {
            let abs = t.checked_abs().ok_or(TOO_SMALL)?;
            if abs <= MS_WATERSHED {
                return Ok((t, 0));
            }
            let mut seconds = t / 1_000;
            let mut micros = (t % 1_000) * 1_000;
            if micros < 0 {
                seconds -= 1;
                micros += 1_000_000;
            }
            Ok((seconds, u32::try_from(micros).unwrap_or(0)))
        }

        /// `Date::from_timestamp_calc`: the date and the seconds into it.
        fn from_timestamp_calc(seconds: i64) -> Result<(Ymd, i64), &'static str> {
            if seconds < UNIX_0000 {
                return Err(TOO_SMALL);
            }
            if seconds > UNIX_9999 {
                return Err(TOO_LARGE);
            }
            let days = seconds.div_euclid(86_400);
            let date = NaiveDate::from_ymd_opt(1970, 1, 1)
                .and_then(|epoch| epoch.checked_add_signed(chrono::Duration::days(days)))
                .ok_or(TOO_LARGE)?;
            let ymd = (
                u16::try_from(chrono::Datelike::year(&date)).map_err(|_| TOO_SMALL)?,
                u8::try_from(chrono::Datelike::month(&date)).unwrap_or(0),
                u8::try_from(chrono::Datelike::day(&date)).unwrap_or(0),
            );
            Ok((ymd, seconds.rem_euclid(86_400)))
        }

        /// A parsed datetime reduced to what a date field needs: the date and whether the time
        /// is exactly midnight.
        pub(super) type Datetime = Result<(Ymd, bool), &'static str>;

        /// `DateTime::from_timestamp_with_config` (unit inferred).
        pub(super) fn from_timestamp(t: i64, micros: u32) -> Datetime {
            let (mut second, extra) = watershed(t)?;
            let mut total = micros.checked_add(extra).ok_or(TIME_TOO_LARGE)?;
            if total >= 1_000_000 {
                second = second
                    .checked_add(i64::from(total / 1_000_000))
                    .ok_or(TIME_TOO_LARGE)?;
                total %= 1_000_000;
            }
            let (date, in_day) = from_timestamp_calc(second)?;
            Ok((date, in_day == 0 && total == 0))
        }

        /// `float_as_datetime` → `DateTime::from_float_with_config`.
        pub(super) fn from_float(t: f64) -> Datetime {
            if t.is_nan() {
                return Err("NaN values not permitted");
            }
            let scale = if t.abs() <= 20_000_000_000.0 {
                1_000_000.0
            } else {
                1_000.0
            };
            let micros = t.fract().abs() * scale;
            // Rust's saturating float-to-int casts, exactly as speedate writes them.
            #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
            from_timestamp(t.floor() as i64, micros.round() as u32)
        }

        /// `Time::parse_bytes_offset` from byte 11, reduced to "is it midnight".
        fn time_is_midnight(b: &[u8]) -> Result<bool, &'static str> {
            let offset = 11;
            if b.len() < offset + 5 {
                return Err(TOO_SHORT);
            }
            let hour_err = "invalid character in hour";
            let hour = digit(b, offset, hour_err)? * 10 + digit(b, offset + 1, hour_err)?;
            if b[offset + 2] != b':' {
                return Err("invalid time separator, expected `:`");
            }
            let minute_err = "invalid character in minute";
            let minute = digit(b, offset + 3, minute_err)? * 10 + digit(b, offset + 4, minute_err)?;
            if hour > 23 {
                return Err("hour value is outside expected range of 0-23");
            }
            if minute > 59 {
                return Err("minute value is outside expected range of 0-59");
            }
            let mut position = offset + 5;
            let mut second = 0;
            let mut micros: u32 = 0;
            if b.get(position) == Some(&b':') {
                let second_err = "invalid character in second";
                second = digit(b, offset + 6, second_err)? * 10 + digit(b, offset + 7, second_err)?;
                if second > 59 {
                    return Err("second value is outside expected range of 0-59");
                }
                position = offset + 8;
                if matches!(b.get(position), Some(b'.' | b',')) {
                    position += 1;
                    let mut i = 0;
                    while let Some(c) = b.get(position + i).filter(|c| c.is_ascii_digit()) {
                        if i < 6 {
                            micros = micros * 10 + u32::from(c - b'0');
                        }
                        i += 1;
                    }
                    if i == 0 {
                        return Err("second fraction digits missing after `.`");
                    }
                    position += i;
                }
            }
            if let Some(&next) = b.get(position) {
                position += 1;
                if next != b'Z' && next != b'z' {
                    match next {
                        b'+' | b'-' => {}
                        226 => {
                            if b.get(position) != Some(&136) || b.get(position + 1) != Some(&146) {
                                return Err("invalid timezone sign");
                            }
                            position += 2;
                        }
                        _ => return Err("invalid timezone sign"),
                    }
                    let tz_hour_err = "invalid timezone hour";
                    let h = i32::from(digit(b, position, tz_hour_err)?) * 10
                        + i32::from(digit(b, position + 1, tz_hour_err)?);
                    let tz_minute_err = "invalid timezone minute";
                    let m1 = match b.get(position + 2) {
                        Some(b':') => {
                            position += 3;
                            i32::from(digit(b, position, tz_minute_err)?)
                        }
                        Some(c) if c.is_ascii_digit() => {
                            position += 2;
                            i32::from(c - b'0')
                        }
                        _ => return Err(tz_minute_err),
                    };
                    let m2 = i32::from(digit(b, position + 1, tz_minute_err)?);
                    let minute_seconds = m1 * 600 + m2 * 60;
                    if minute_seconds >= 3_600 {
                        return Err("timezone minute value is outside expected range of 0-59");
                    }
                    if h * 3_600 + minute_seconds >= 24 * 3_600 {
                        return Err("timezone offset must be less than 24 hours");
                    }
                    position += 2;
                }
            }
            if b.len() > position {
                return Err(EXTRA);
            }
            Ok(hour == 0 && minute == 0 && second == 0 && micros == 0)
        }

        /// `DateTime::parse_bytes_with_config` (microsecond overflow truncated).
        fn datetime_from_bytes(b: &[u8]) -> Datetime {
            let rfc = date_partial(b).and_then(|date| {
                if !matches!(b.get(10), Some(b'T' | b't' | b' ' | b'_')) {
                    return Err(DATETIME_SEP);
                }
                time_is_midnight(b).map(|midnight| (date, midnight))
            });
            let Err(why) = rfc else { return rfc };
            match float_parse(b) {
                IntFloat::Int(i) => from_timestamp(i, 0),
                IntFloat::Float(f) => {
                    let normalized = if f.abs() > 20_000_000_000.0 {
                        f / 1_000.0
                    } else {
                        f
                    };
                    // speedate's saturating casts.
                    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
                    let (seconds, micros) = {
                        let seconds = normalized.floor() as i64;
                        #[allow(clippy::cast_precision_loss)]
                        let micros = ((normalized - seconds as f64) * 1_000_000.0).round() as u32;
                        (seconds, micros)
                    };
                    from_timestamp(seconds, micros)
                }
                IntFloat::Err => Err(why),
            }
        }

        fn to_date((year, month, day): Ymd) -> Result<NaiveDate, DateError> {
            if year == 0 {
                return Err(DateError::YearZero);
            }
            NaiveDate::from_ymd_opt(i32::from(year), u32::from(month), u32::from(day))
                .ok_or(DateError::Parsing("day value is outside expected range"))
        }

        /// The datetime fallback of a date field.
        pub(super) fn date_from_datetime(dt: Datetime) -> Result<NaiveDate, DateError> {
            match dt {
                Ok((date, true)) => to_date(date),
                Ok((_, false)) => Err(DateError::Inexact),
                Err(why) => Err(DateError::Parsing(why)),
            }
        }

        /// A string into a date field.
        pub(super) fn date_from_str(b: &[u8]) -> Result<NaiveDate, DateError> {
            if b.len() <= 10
                && let Ok(date) = date_partial(b)
            {
                return to_date(date);
            }
            if let Ok(i) = int_parse(b)
                && let Ok((date, true)) = from_timestamp(i, 0)
            {
                return to_date(date);
            }
            date_from_datetime(datetime_from_bytes(b))
        }
    }

    fn p_list<'a>(v: &mut V, p: &'a Payload, loc: &[String]) -> Option<&'a [Payload]> {
        match p {
            Payload::List(items) => Some(items),
            _ => {
                v.err("list_type", loc, "Input should be a valid list");
                None
            }
        }
    }

    /// Validate every item; `None` if any item failed (errors recorded with the item index).
    fn p_items<T>(
        v: &mut V,
        p: &Payload,
        loc: &[String],
        mut each: impl FnMut(&mut V, &Payload, &[String]) -> Option<T>,
    ) -> Option<Vec<T>> {
        let items = p_list(v, p, loc)?;
        let mut out = Vec::with_capacity(items.len());
        let mut ok = true;
        for (i, item) in items.iter().enumerate() {
            match each(v, item, &at(loc, i.to_string())) {
                Some(x) => out.push(x),
                None => ok = false,
            }
        }
        ok.then_some(out)
    }

    /// `field(...)` with a default: `default` when absent, `None` when present and invalid.
    fn opt<T>(
        v: &mut V,
        obj: &mut Obj<'_>,
        name: &'static str,
        alias: Option<&'static str>,
        default: T,
        parse: impl FnOnce(&mut V, &Payload, &[String]) -> Option<T>,
    ) -> Option<T> {
        match obj.get(name, alias) {
            Some((p, key)) => {
                let loc = obj.field_loc(key);
                parse(v, p, &loc)
            }
            None => Some(default),
        }
    }

    /// `T | None` — `Null` is `None`.
    fn nullable<T>(
        v: &mut V,
        p: &Payload,
        loc: &[String],
        parse: impl FnOnce(&mut V, &Payload, &[String]) -> Option<T>,
    ) -> Option<Option<T>> {
        match p {
            Payload::Null => Some(None),
            other => parse(v, other, loc).map(Some),
        }
    }

    fn known_factor(v: &mut V, key: String, field: &str, loc: &[String]) -> Option<String> {
        if factor_registry::contains(&key) {
            Some(key)
        } else {
            v.value_error(
                loc,
                &format!(
                    "{field}: {} is not one of the {} factor-registry keys (docs/01 §3; see GET /meta/factors)",
                    py_repr_str(&key),
                    factor_registry::named_factor_count()
                ),
            );
            None
        }
    }

    fn moving_average(v: &mut V, p: &Payload, loc: &[String]) -> Option<MovingAverageFilter> {
        let mut obj = Obj::open(v, p, loc, "MovingAverageFilter")?;
        let before = v.errors.len();
        let mut flags = [false; 9];
        for (slot, name) in flags.iter_mut().zip([
            "enabled",
            "above_200",
            "above_100",
            "above_50",
            "above_20",
            "below_200",
            "below_100",
            "below_50",
            "below_20",
        ]) {
            *slot = opt(v, &mut obj, name, None, false, p_bool).unwrap_or(false);
        }
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        let [
            enabled,
            above_200,
            above_100,
            above_50,
            above_20,
            below_200,
            below_100,
            below_50,
            below_20,
        ] = flags;
        let m = MovingAverageFilter {
            enabled,
            above_200,
            above_100,
            above_50,
            above_20,
            below_200,
            below_100,
            below_50,
            below_20,
        };
        if m.enabled {
            let both: Vec<String> = [200, 100, 50, 20]
                .iter()
                .filter(|&&k| {
                    let (a, b) = m.switches(k);
                    a && b
                })
                .map(u32::to_string)
                .collect();
            if !both.is_empty() {
                v.value_error(
                    loc,
                    &format!(
                        "moving_average: above and below cannot both be set for MA {}; no stock can satisfy both.",
                        both.join(", ")
                    ),
                );
                return None;
            }
        }
        Some(m)
    }

    fn bounded_int(
        v: &mut V,
        p: &Payload,
        loc: &[String],
        check: impl FnOnce(i64) -> Option<String>,
    ) -> Option<i64> {
        let i = p_int(v, p, loc)?;
        match check(i) {
            Some(msg) => {
                v.value_error(loc, &msg);
                None
            }
            None => Some(i),
        }
    }

    fn away_from_high(v: &mut V, p: &Payload, loc: &[String]) -> Option<AwayFromHighFilter> {
        let mut obj = Obj::open(v, p, loc, "AwayFromHighFilter")?;
        let before = v.errors.len();
        let check = |x: i64| {
            (!(0..=AWAY_FROM_HIGH_IGNORE).contains(&x)).then(|| {
                format!(
                    "away_from_high must be between 0 and {AWAY_FROM_HIGH_IGNORE} ({AWAY_FROM_HIGH_IGNORE} = ignore); got {x}"
                )
            })
        };
        let ath = opt(
            v,
            &mut obj,
            "ath",
            None,
            AWAY_FROM_HIGH_IGNORE,
            |v, p, l| bounded_int(v, p, l, check),
        );
        let one_year = opt(
            v,
            &mut obj,
            "one_year",
            None,
            AWAY_FROM_HIGH_IGNORE,
            |v, p, l| bounded_int(v, p, l, check),
        );
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        Some(AwayFromHighFilter {
            ath: ath?,
            one_year: one_year?,
        })
    }

    fn five_windows(
        v: &mut V,
        p: &Payload,
        loc: &[String],
        model: &str,
        default: i64,
        check: impl Fn(i64) -> Option<String> + Copy,
    ) -> Option<[i64; 5]> {
        let mut obj = Obj::open(v, p, loc, model)?;
        let before = v.errors.len();
        let mut values = [default; 5];
        for (slot, name) in values.iter_mut().zip(WINDOWS) {
            *slot = opt(v, &mut obj, name, None, default, |v, p, l| {
                bounded_int(v, p, l, check)
            })
            .unwrap_or(default);
        }
        obj.finish(v);
        (v.errors.len() == before).then_some(values)
    }

    fn positive_days(v: &mut V, p: &Payload, loc: &[String]) -> Option<PositiveDaysFilter> {
        let [m12, m9, m6, m3, m1] = five_windows(v, p, loc, "PositiveDaysFilter", 0, |x| {
            (!(0..=PERCENT_MAX).contains(&x)).then(|| {
                format!(
                    "positive_days is a percentage between 0 and {PERCENT_MAX} ({POSITIVE_DAYS_IGNORE} = ignore); got {x}"
                )
            })
        })?;
        Some(PositiveDaysFilter {
            m12,
            m9,
            m6,
            m3,
            m1,
        })
    }

    fn circuits(v: &mut V, p: &Payload, loc: &[String]) -> Option<CircuitsFilter> {
        let [m12, m9, m6, m3, m1] = five_windows(v, p, loc, "CircuitsFilter", 999, |x| {
            (x < 0).then(|| {
                format!(
                    "circuits cap cannot be negative (> {CIRCUITS_IGNORE_ABOVE} = ignore); got {x}"
                )
            })
        })?;
        Some(CircuitsFilter {
            m12,
            m9,
            m6,
            m3,
            m1,
        })
    }

    fn opt_decimal(v: &mut V, p: &Payload, loc: &[String]) -> Option<Option<DecimalValue>> {
        nullable(v, p, loc, p_decimal)
    }

    fn range_bounds(
        v: &mut V,
        obj: &mut Obj<'_>,
    ) -> (Option<Option<DecimalValue>>, Option<Option<DecimalValue>>) {
        let from = opt(v, obj, "from_", Some("from"), None, opt_decimal);
        let to = opt(v, obj, "to", None, None, opt_decimal);
        (from, to)
    }

    fn check_range(
        v: &mut V,
        loc: &[String],
        from: Option<&DecimalValue>,
        to: Option<&DecimalValue>,
    ) -> bool {
        if let (Some(f), Some(t)) = (from, to)
            && f.cmp_value(t).is_gt()
        {
            v.value_error(
                loc,
                &format!("range is inverted: from={} > to={}", f.py_str(), t.py_str()),
            );
            return false;
        }
        true
    }

    fn range_filter(v: &mut V, p: &Payload, loc: &[String]) -> Option<RangeFilter> {
        let mut obj = Obj::open(v, p, loc, "RangeFilter")?;
        let before = v.errors.len();
        let (from, to) = range_bounds(v, &mut obj);
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        let (from, to) = (from?, to?);
        check_range(v, loc, from.as_ref(), to.as_ref()).then_some(RangeFilter { from, to })
    }

    fn pe_filter(v: &mut V, p: &Payload, loc: &[String]) -> Option<PeFilter> {
        let mut obj = Obj::open(v, p, loc, "PeFilter")?;
        let before = v.errors.len();
        let (from, to) = range_bounds(v, &mut obj);
        let enabled = opt(v, &mut obj, "enabled", None, false, p_bool);
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        let (from, to, enabled) = (from?, to?, enabled?);
        check_range(v, loc, from.as_ref(), to.as_ref()).then_some(PeFilter { from, to, enabled })
    }

    fn top_risk(v: &mut V, p: &Payload, loc: &[String]) -> Option<TopRiskFilter> {
        let mut obj = Obj::open(v, p, loc, "TopRiskFilter")?;
        let before = v.errors.len();
        let enabled = opt(v, &mut obj, "enabled", None, false, p_bool);
        let count = opt(v, &mut obj, "count", None, 0, |v, p, l| {
            bounded_int(v, p, l, |x| {
                (x < 0).then(|| format!("count cannot be negative; got {x}"))
            })
        });
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        Some(TopRiskFilter {
            enabled: enabled?,
            count: count?,
        })
    }

    fn extra_factor(v: &mut V, p: &Payload, loc: &[String]) -> Option<ExtraFactor> {
        let mut obj = Obj::open(v, p, loc, "ExtraFactor")?;
        let before = v.errors.len();
        let enabled = opt(v, &mut obj, "enabled", None, false, p_bool);
        let sort_by = opt(
            v,
            &mut obj,
            "sort_by",
            None,
            None,
            |v, p, l| match nullable(v, p, l, p_key)? {
                Some(key) => known_factor(v, key, "sort_by", l).map(Some),
                None => Some(None),
            },
        );
        let direction = opt(
            v,
            &mut obj,
            "sort_direction",
            None,
            "desc".to_owned(),
            |v, p, l| p_literal(v, p, l, &SORT_DIRECTIONS),
        );
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        let e = ExtraFactor {
            enabled: enabled?,
            sort_by: sort_by?,
            sort_direction: direction?,
        };
        if e.enabled && e.sort_by.is_none() {
            v.value_error(loc, "sort_by is required when the extra factor is enabled");
            return None;
        }
        Some(e)
    }

    fn operand(v: &mut V, p: &Payload, loc: &[String], field: &str) -> Option<String> {
        let key = p_key(v, p, loc)?;
        if CUSTOM_FILTER_OPERANDS.contains(&key.as_str()) {
            Some(key)
        } else {
            v.value_error(
                loc,
                &format!(
                    "{field}: {} is not one of the {} custom-filter operands (docs/01 §2.14)",
                    py_repr_str(&key),
                    CUSTOM_FILTER_OPERANDS.len()
                ),
            );
            None
        }
    }

    fn custom_filter(v: &mut V, p: &Payload, loc: &[String]) -> Option<CustomFilter> {
        let mut obj = Obj::open(v, p, loc, "CustomFilter")?;
        let before = v.errors.len();
        let enabled = opt(v, &mut obj, "enabled", None, true, p_bool);
        let left = obj
            .required(v, "left")
            .and_then(|(p, l)| operand(v, p, &l, "left"));
        let op = obj
            .required(v, "op")
            .and_then(|(p, l)| p_literal(v, p, &l, &CUSTOM_FILTER_OPS));
        let right = obj
            .required(v, "right")
            .and_then(|(p, l)| operand(v, p, &l, "right"));
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        Some(CustomFilter {
            enabled: enabled?,
            left: left?,
            op: op?,
            right: right?,
        })
    }

    fn ranking_term(v: &mut V, p: &Payload, loc: &[String]) -> Option<RankingTerm> {
        let mut obj = Obj::open(v, p, loc, "RankingTerm")?;
        let before = v.errors.len();
        let factor = obj.required(v, "factor").and_then(|(p, l)| {
            let key = p_key(v, p, &l)?;
            let key = known_factor(v, key, "ranking_terms.factor", &l)?;
            if factor_registry::lookup(&key).is_some_and(|f| !f.rankable) {
                v.value_error(
                    &l,
                    &format!(
                        "ranking_terms.factor: {} cannot rank a list; it is a filter-only factor. Use it in factor_ranges as an eligibility filter instead",
                        py_repr_str(&key)
                    ),
                );
                return None;
            }
            Some(key)
        });
        let preference = obj
            .required(v, "preference")
            .and_then(|(p, l)| p_literal(v, p, &l, &TERM_PREFERENCES));
        let weight = opt(v, &mut obj, "weight", None, 1.0, |v, p, l| {
            p_float(
                v,
                p,
                l,
                &FloatBounds {
                    gt: Some(0.0),
                    ge: None,
                    le: Some(100.0),
                },
            )
        });
        let finite_opt = |v: &mut V, p: &Payload, l: &[String]| {
            nullable(v, p, l, |v, p, l| p_float(v, p, l, &NO_BOUNDS))
        };
        let target_min = opt(v, &mut obj, "target_min", None, None, finite_opt);
        let target_max = opt(v, &mut obj, "target_max", None, None, finite_opt);
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        let t = RankingTerm {
            factor: factor?,
            preference: preference?,
            weight: weight?,
            target_min: target_min?,
            target_max: target_max?,
        };
        let has_bound = t.target_min.is_some() || t.target_max.is_some();
        if t.preference == "target_range" {
            if !has_bound {
                v.value_error(
                    loc,
                    &format!(
                        "ranking_terms[{}]: target_range needs target_min, target_max or both",
                        t.factor
                    ),
                );
                return None;
            }
            if let (Some(lo), Some(hi)) = (t.target_min, t.target_max)
                && lo > hi
            {
                v.value_error(
                        loc,
                        &format!(
                            "ranking_terms[{}]: target range is inverted: target_min={} > target_max={}",
                            t.factor,
                            py_float_repr(lo),
                            py_float_repr(hi)
                        ),
                    );
                return None;
            }
        } else if has_bound {
            v.value_error(
                loc,
                &format!(
                    "ranking_terms[{}]: target_min/target_max only apply to preference='target_range', not {}",
                    t.factor,
                    py_repr_str(&t.preference)
                ),
            );
            return None;
        }
        Some(t)
    }

    fn factor_range(v: &mut V, p: &Payload, loc: &[String]) -> Option<FactorRange> {
        let mut obj = Obj::open(v, p, loc, "FactorRange")?;
        let before = v.errors.len();
        let enabled = opt(v, &mut obj, "enabled", None, true, p_bool);
        let factor = obj.required(v, "factor").and_then(|(p, l)| {
            let key = p_key(v, p, &l)?;
            let key = known_factor(v, key, "factor_ranges.factor", &l)?;
            if factor_registry::lookup(&key).is_some_and(|f| f.is_computed) {
                v.value_error(
                    &l,
                    &format!(
                        "factor_ranges.factor: {} is computed in-process, not stored, so it cannot be a SQL eligibility filter",
                        py_repr_str(&key)
                    ),
                );
                return None;
            }
            Some(key)
        });
        let finite_opt = |v: &mut V, p: &Payload, l: &[String]| {
            nullable(v, p, l, |v, p, l| p_float(v, p, l, &NO_BOUNDS))
        };
        let min = opt(v, &mut obj, "min", None, None, finite_opt);
        let max = opt(v, &mut obj, "max", None, None, finite_opt);
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        let r = FactorRange {
            enabled: enabled?,
            factor: factor?,
            min: min?,
            max: max?,
        };
        match (r.min, r.max) {
            (None, None) => {
                v.value_error(
                    loc,
                    &format!("factor_ranges[{}]: set min, max or both", r.factor),
                );
                None
            }
            (Some(lo), Some(hi)) if lo > hi => {
                v.value_error(
                    loc,
                    &format!(
                        "factor_ranges[{}]: range is inverted: min={} > max={}",
                        r.factor,
                        py_float_repr(lo),
                        py_float_repr(hi)
                    ),
                );
                None
            }
            _ => Some(r),
        }
    }

    fn family_weights(v: &mut V, p: &Payload, loc: &[String]) -> Option<FamilyWeights> {
        let mut obj = Obj::open(v, p, loc, "FamilyWeights")?;
        let before = v.errors.len();
        let bounds = FloatBounds {
            gt: None,
            ge: Some(0.0),
            le: None,
        };
        let mut values: [Option<f64>; 5] = [None; 5];
        for (slot, name) in values.iter_mut().zip([
            "momentum",
            "path_quality",
            "trend_structure",
            "participation",
            "risk_execution",
        ]) {
            *slot = opt(v, &mut obj, name, None, None, |v, p, l| {
                nullable(v, p, l, |v, p, l| p_float(v, p, l, &bounds))
            })
            .flatten();
        }
        obj.finish(v);
        if v.errors.len() != before {
            return None;
        }
        let [
            momentum,
            path_quality,
            trend_structure,
            participation,
            risk_execution,
        ] = values;
        Some(FamilyWeights {
            momentum,
            path_quality,
            trend_structure,
            participation,
            risk_execution,
        })
    }

    fn duplicates(items: &[String]) -> bool {
        items
            .iter()
            .enumerate()
            .any(|(i, x)| items[..i].contains(x))
    }

    #[allow(clippy::too_many_lines)]
    fn screen_definition(v: &mut V, input: &Payload) -> Option<ScreenDefinition> {
        let root: Vec<String> = Vec::new();
        let mut obj = Obj::open(v, input, &root, "ScreenDefinition")?;
        let mut d = ScreenDefinition::new("", "");

        let index = obj
            .required(v, "index")
            .and_then(|(p, l)| p_literal(v, p, &l, &UNIVERSE_SLUGS));
        let sort_by = obj.required(v, "sort_by").and_then(|(p, l)| {
            let key = p_key(v, p, &l)?;
            known_factor(v, key, "sort_by", &l)
        });
        let sort_direction = opt(
            v,
            &mut obj,
            "sort_direction",
            None,
            d.sort_direction.clone(),
            |v, p, l| p_literal(v, p, l, &SORT_DIRECTIONS),
        );
        let apply_filters_on = opt(
            v,
            &mut obj,
            "apply_filters_on",
            None,
            d.apply_filters_on.clone(),
            |v, p, l| p_literal(v, p, l, &APPLY_FILTERS_ON),
        );
        let ranking_mode = opt(
            v,
            &mut obj,
            "ranking_mode",
            None,
            d.ranking_mode.clone(),
            |v, p, l| p_literal(v, p, l, &RANKING_MODES),
        );
        let ranking_scope = opt(
            v,
            &mut obj,
            "ranking_scope",
            None,
            d.ranking_scope.clone(),
            |v, p, l| p_literal(v, p, l, &RANKING_SCOPES),
        );
        let min_return_1y = opt(v, &mut obj, "min_return_1y", None, None, opt_decimal);
        let median_volume_1y = opt(v, &mut obj, "median_volume_1y", None, None, |v, p, l| {
            nullable(v, p, l, p_int)
        });
        let moving_average = opt(
            v,
            &mut obj,
            "moving_average",
            None,
            d.moving_average.clone(),
            moving_average,
        );
        let away = opt(
            v,
            &mut obj,
            "away_from_high",
            None,
            d.away_from_high.clone(),
            away_from_high,
        );
        let positive = opt(
            v,
            &mut obj,
            "positive_days",
            None,
            d.positive_days.clone(),
            positive_days,
        );
        let circuits_f = opt(v, &mut obj, "circuits", None, d.circuits.clone(), circuits);
        let marketcap = opt(
            v,
            &mut obj,
            "marketcap",
            None,
            RangeFilter::default(),
            range_filter,
        );
        let pe = opt(v, &mut obj, "pe", None, PeFilter::default(), pe_filter);
        let series = opt(v, &mut obj, "series", None, d.series.clone(), |v, p, l| {
            let items = p_items(v, p, l, p_str)?;
            let unknown: Vec<String> = items
                .iter()
                .filter(|s| !SERIES_VALUES.contains(&s.as_str()))
                .cloned()
                .collect();
            if !unknown.is_empty() {
                v.value_error(
                    l,
                    &format!(
                        "unknown series {}; expected any of {}",
                        py_list_repr(&unknown),
                        py_list_repr(&SERIES_VALUES)
                    ),
                );
                return None;
            }
            if duplicates(&items) {
                v.value_error(
                    l,
                    &format!("series contains duplicates: {}", py_list_repr(&items)),
                );
                return None;
            }
            Some(items)
        });
        let ignore_top_beta = opt(
            v,
            &mut obj,
            "ignore_top_beta",
            None,
            TopRiskFilter::default(),
            top_risk,
        );
        let ignore_top_volatility = opt(
            v,
            &mut obj,
            "ignore_top_volatility",
            None,
            TopRiskFilter::default(),
            top_risk,
        );
        let ignore_above_beta = opt(
            v,
            &mut obj,
            "ignore_above_beta",
            None,
            IGNORE_ABOVE_BETA_IGNORE,
            |v, p, l| {
                bounded_int(v, p, l, |x| {
                    (x < 0).then(|| {
                    format!(
                        "ignore_above_beta cannot be negative ({IGNORE_ABOVE_BETA_IGNORE} = ignore); got {x}"
                    )
                })
                })
            },
        );
        let price = opt(
            v,
            &mut obj,
            "price",
            None,
            RangeFilter::default(),
            range_filter,
        );
        let factor_two = opt(
            v,
            &mut obj,
            "factor_two",
            None,
            ExtraFactor::default(),
            extra_factor,
        );
        let factor_three = opt(
            v,
            &mut obj,
            "factor_three",
            None,
            ExtraFactor::default(),
            extra_factor,
        );
        let historical_date = opt(v, &mut obj, "historical_date", None, None, |v, p, l| {
            nullable(v, p, l, p_date)
        });
        let custom_filters = opt(
            v,
            &mut obj,
            "custom_filters",
            None,
            Vec::new(),
            |v, p, l| {
                let items = p_items(v, p, l, custom_filter)?;
                if items.len() > MAX_CUSTOM_FILTERS {
                    v.value_error(
                        l,
                        &format!(
                            "at most {MAX_CUSTOM_FILTERS} custom filters are supported; got {}",
                            items.len()
                        ),
                    );
                    return None;
                }
                Some(items)
            },
        );
        let ranking_terms = opt(v, &mut obj, "ranking_terms", None, Vec::new(), |v, p, l| {
            let items = p_items(v, p, l, ranking_term)?;
            if items.len() > MAX_RANKING_TERMS {
                v.value_error(
                    l,
                    &format!(
                        "at most {MAX_RANKING_TERMS} ranking terms are supported; got {}",
                        items.len()
                    ),
                );
                return None;
            }
            let factors: Vec<String> = items.iter().map(|t| t.factor.clone()).collect();
            let mut duplicated: Vec<String> = factors
                .iter()
                .filter(|f| factors.iter().filter(|g| g == f).count() > 1)
                .cloned()
                .collect();
            duplicated.sort();
            duplicated.dedup();
            if !duplicated.is_empty() {
                v.value_error(
                    l,
                    &format!(
                        "ranking_terms name {} more than once; use one term with a larger weight instead",
                        py_list_repr(&duplicated)
                    ),
                );
                return None;
            }
            Some(items)
        });
        let family_weights_f = opt(v, &mut obj, "family_weights", None, None, |v, p, l| {
            nullable(v, p, l, family_weights)
        });
        let missing_data = opt(
            v,
            &mut obj,
            "missing_data",
            None,
            d.missing_data.clone(),
            |v, p, l| p_literal(v, p, l, &MISSING_DATA),
        );
        let factor_ranges = opt(v, &mut obj, "factor_ranges", None, Vec::new(), |v, p, l| {
            let items = p_items(v, p, l, factor_range)?;
            if items.len() > MAX_FACTOR_RANGES {
                v.value_error(
                    l,
                    &format!(
                        "at most {MAX_FACTOR_RANGES} factor ranges are supported; got {}",
                        items.len()
                    ),
                );
                return None;
            }
            Some(items)
        });
        let regime_in = opt(v, &mut obj, "regime_in", None, None, |v, p, l| {
            let Some(items) = nullable(v, p, l, |v, p, l| {
                p_items(v, p, l, |v, p, l| p_literal(v, p, l, &REGIMES))
            })?
            else {
                return Some(None);
            };
            if items.is_empty() {
                v.value_error(
                    l,
                    "regime_in cannot be empty (no row could match); use null for no filter",
                );
                return None;
            }
            if duplicates(&items) {
                v.value_error(
                    l,
                    &format!("regime_in contains duplicates: {}", py_list_repr(&items)),
                );
                return None;
            }
            Some(Some(items))
        });
        obj.finish(v);
        if !v.errors.is_empty() {
            return None;
        }

        d.index = index?;
        d.sort_by = sort_by?;
        d.sort_direction = sort_direction?;
        d.apply_filters_on = apply_filters_on?;
        d.ranking_mode = ranking_mode?;
        d.ranking_scope = ranking_scope?;
        d.min_return_1y = min_return_1y?;
        d.median_volume_1y = median_volume_1y?;
        d.moving_average = moving_average?;
        d.away_from_high = away?;
        d.positive_days = positive?;
        d.circuits = circuits_f?;
        d.marketcap = marketcap?;
        d.pe = pe?;
        d.series = series?;
        d.ignore_top_beta = ignore_top_beta?;
        d.ignore_top_volatility = ignore_top_volatility?;
        d.ignore_above_beta = ignore_above_beta?;
        d.price = price?;
        d.factor_two = factor_two?;
        d.factor_three = factor_three?;
        d.historical_date = historical_date?;
        d.custom_filters = custom_filters?;
        d.ranking_terms = ranking_terms?;
        d.family_weights = family_weights_f?;
        d.missing_data = missing_data?;
        d.factor_ranges = factor_ranges?;
        d.regime_in = regime_in?;

        match model_rules(&d) {
            Ok(()) => Some(d),
            Err(msg) => {
                v.value_error(&root, &msg);
                None
            }
        }
    }

    /// `_factor_three_requires_two` and `_check_ranking_terms`.
    fn model_rules(d: &ScreenDefinition) -> Result<(), String> {
        if d.factor_three.enabled && !d.factor_two.enabled {
            return Err("factor_three cannot be enabled while factor_two is disabled".to_owned());
        }
        let extras_active = d.factor_two.is_active() || d.factor_three.is_active();
        if d.ranking_mode == "single" && extras_active {
            return Err(
                "ranking_mode='single' cannot combine factor_two or factor_three; use sequential or composite"
                    .to_owned(),
            );
        }
        if d.sort_by == "desk_score" && extras_active {
            return Err(
                "desk_score cannot combine with factor_two or factor_three; it is a single computed SCORE"
                    .to_owned(),
            );
        }
        if let Some(first) = d.ranking_terms.first() {
            if d.sort_by != first.factor {
                return Err(format!(
                    "sort_by must equal ranking_terms[0].factor ({}); got {}",
                    py_repr_str(&first.factor),
                    py_repr_str(&d.sort_by)
                ));
            }
            if extras_active {
                return Err(
                    "ranking_terms replace factor_two and factor_three; disable them".to_owned(),
                );
            }
            if d.ranking_mode == "single" && d.ranking_terms.len() != 1 {
                return Err(format!(
                    "ranking_mode='single' ranks by exactly one term; got {}",
                    d.ranking_terms.len()
                ));
            }
            if d.ranking_mode != "composite" {
                // Python compares the weight to 1.0 exactly.
                #[allow(clippy::float_cmp)]
                let weighted: Vec<String> = d
                    .ranking_terms
                    .iter()
                    .filter(|t| t.weight != 1.0)
                    .map(|t| t.factor.clone())
                    .collect();
                if !weighted.is_empty() {
                    return Err(format!(
                        "term weights only apply to ranking_mode='composite'; {} carry one under {}",
                        py_list_repr(&weighted),
                        py_repr_str(&d.ranking_mode)
                    ));
                }
                if d.family_weights.is_some() {
                    return Err("family_weights only apply to ranking_mode='composite'".to_owned());
                }
                if d.ranking_scope == "within_sector" {
                    return Err(format!(
                        "ranking_scope='within_sector' only changes a composite score; {} orders by raw values, so it would do nothing",
                        py_repr_str(&d.ranking_mode)
                    ));
                }
            }
        } else {
            if d.ranking_scope == "within_sector" {
                return Err(
                    "ranking_scope='within_sector' needs explicit ranking_terms; the legacy sort_by/factor_two/factor_three path ranks in SQL without sectors"
                        .to_owned(),
                );
            }
            if d.family_weights.is_some() {
                return Err("family_weights needs ranking_terms in composite mode".to_owned());
            }
            if d.missing_data != "penalize" {
                return Err(
                    "missing_data applies to ranking_terms; the legacy path always sorts missing values last"
                        .to_owned(),
                );
            }
        }
        Ok(())
    }
}
