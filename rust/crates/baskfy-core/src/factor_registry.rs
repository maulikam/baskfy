//! Port of `baskfy_core.factor_registry`.
//!
//! The factor catalogue and its SQL fragments (pure text). Goldens:
//! `go/testdata/golden/L1/ranking/factor_registry.*`.
//!
//! docs/06 §"The factor registry": one table carrying, per factor, `key`, `label`, `family`,
//! `sql_expr`, `unit`, `higher_is_better` and `null_policy`. It is the single source of truth for
//! the `sort_by` dropdown, the custom-filter operand list, the column picker, the API enum and
//! the backtest signal list.
//!
//! `sql_expr` is a string written in this file, never off the wire: a user supplies a *key*, the
//! key is looked up here, and a key that is not here has no SQL (docs/06 §"Reference SQL
//! skeleton"). The registry is built exactly as the Python builds it — the same loops over the
//! same window and blend shapes, in the same order — so [`factors`] iterates in the Python
//! dict's insertion order.
//!
//! The count note of the Python module stands: docs/01 §3 says 62, its families enumerate 64,
//! and Phase 1/2 brought the registry to [`named_factor_count`] (109).
//!
//! The pieces of `baskfy_core.blends` (the blend shapes and `blend_sql`), `baskfy_core.windows`
//! (`WINDOW_MONTHS`) and `baskfy_core.ranking` (`FactorPreference`) the registry needs live here
//! too, because those Python modules have no Rust module of their own yet.

use std::collections::HashMap;
use std::sync::OnceLock;

use serde::Serialize;
use thiserror::Error;

use crate::error::CoreError;

/// `baskfy_core.windows.WINDOW_MONTHS` — the five windows every factor family is computed over.
pub const WINDOW_MONTHS: [u32; 5] = [1, 3, 6, 9, 12];

/// docs/07 and docs/01 render the 12-month window as "1 YEAR", not "12 MONTHS".
const MONTHS_IN_A_YEAR: u32 = 12;

/// `baskfy_core.blends.BLEND_SHAPES` — the eleven blend shapes used by all three families.
pub const BLEND_SHAPES: [&[u32]; 11] = [
    &[12, 9, 6, 3, 1],
    &[12, 9, 6, 3],
    &[12, 9, 6],
    &[12, 9],
    &[12, 6, 3, 1],
    &[12, 6, 3],
    &[12, 6],
    &[12, 3, 1],
    &[12, 3],
    &[12, 9, 3, 1],
    &[12, 9, 3],
];

/// `baskfy_core.blends.SHARPE_ONLY_SHAPES` — `6·3`, and `3·1` / `6·1` since M56.
pub const SHARPE_ONLY_SHAPES: [&[u32]; 3] = [&[6, 3], &[3, 1], &[6, 1]];

/// `baskfy_core.blends.BETA_BLEND_SHAPES` — the blends applied to sharpe-divided-by-beta.
pub const BETA_BLEND_SHAPES: [&[u32]; 3] = [&[12, 9, 6, 3], &[12, 6, 3], &[12, 6]];

/// docs/01 §3's headline. Recorded next to the real count so the gap cannot be forgotten.
pub const DOCUMENTED_FACTOR_COUNT: usize = 62;

/// docs/01 §4's headline column count (it enumerates thirty-six; all are offered).
pub const DOCUMENTED_COLUMN_COUNT: usize = 34;

/// SQL for `regime_priority`. C1: "the Wasserstein labels get an order, never a distance".
pub const REGIME_PRIORITY_SQL: &str =
    "(CASE regime WHEN 'BULL' THEN 2 WHEN 'NEUTRAL' THEN 1 WHEN 'BEAR' THEN 0 END)";

/// The sentinel `sql_expr` of a computed factor; [`sql_for`] refuses those keys.
pub const COMPUTED_SQL_SENTINEL: &str = "__computed_not_sql__";

/// docs/01 §2.14 — the custom-filter operand list, in the reference product's dropdown order
/// (longest window first; the reverse of [`WINDOW_MONTHS`]).
pub const CUSTOM_FILTER_OPERANDS: [&str; 22] = [
    "ret_12m",
    "vol_12m",
    "vol_9m",
    "vol_6m",
    "vol_3m",
    "vol_1m",
    "beta_12m",
    "close",
    "close_raw",
    "away_high_ath",
    "away_high_1y",
    "ma_200",
    "ma_100",
    "ma_50",
    "ma_20",
    "vol_day_val",
    "vol_avg_12m",
    "vol_avg_9m",
    "vol_avg_6m",
    "vol_avg_3m",
    "vol_avg_1m",
    "vol_avg_1w",
];

/// docs/01 §4 — the column picker, in the document's order (thirty-six keys).
pub const COLUMN_PICKER_KEYS: [&str; 36] = [
    "series",
    "marketcap_cr",
    "pe",
    "ret_12m",
    "ret_9m",
    "ret_6m",
    "ret_3m",
    "ret_1m",
    "sharpe_12m",
    "sharpe_9m",
    "sharpe_6m",
    "sharpe_3m",
    "sharpe_1m",
    "rsi_12m",
    "rsi_9m",
    "rsi_6m",
    "rsi_3m",
    "rsi_1m",
    "beta_12m",
    "vol_12m",
    "high_1y",
    "high_ath",
    "away_high_1y",
    "away_high_ath",
    "ma_200",
    "ma_100",
    "ma_50",
    "ma_20",
    "median_vol_12m",
    "close",
    "close_raw",
    "circuits_12m",
    "circuits_9m",
    "circuits_6m",
    "circuits_3m",
    "circuits_1m",
];

/// docs/01 §3's own grouping, plus the Phase-1/2 ranking families (docs/ranking/PLAN.md).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FactorFamily {
    /// `absolute_return`.
    AbsoluteReturn,
    /// `sharpe_return`.
    SharpeReturn,
    /// `rsi`.
    Rsi,
    /// `risk_adjusted`.
    RiskAdjusted,
    /// `skip_month`.
    SkipMonth,
    /// `non_momentum`.
    NonMomentum,
    /// `path_quality`.
    PathQuality,
    /// `trend_structure`.
    TrendStructure,
    /// `participation`.
    Participation,
    /// `momentum_quality` — momentum measured as something other than a raw window return.
    MomentumQuality,
}

impl FactorFamily {
    /// Every family, in the Python `StrEnum`'s declaration order.
    pub const ALL: [FactorFamily; 10] = [
        FactorFamily::AbsoluteReturn,
        FactorFamily::SharpeReturn,
        FactorFamily::Rsi,
        FactorFamily::RiskAdjusted,
        FactorFamily::SkipMonth,
        FactorFamily::NonMomentum,
        FactorFamily::PathQuality,
        FactorFamily::TrendStructure,
        FactorFamily::Participation,
        FactorFamily::MomentumQuality,
    ];

    /// The enum's string value (`"absolute_return"`).
    pub fn value(self) -> &'static str {
        match self {
            FactorFamily::AbsoluteReturn => "absolute_return",
            FactorFamily::SharpeReturn => "sharpe_return",
            FactorFamily::Rsi => "rsi",
            FactorFamily::RiskAdjusted => "risk_adjusted",
            FactorFamily::SkipMonth => "skip_month",
            FactorFamily::NonMomentum => "non_momentum",
            FactorFamily::PathQuality => "path_quality",
            FactorFamily::TrendStructure => "trend_structure",
            FactorFamily::Participation => "participation",
            FactorFamily::MomentumQuality => "momentum_quality",
        }
    }
}

/// docs/ranking/PLAN.md C1 — the five families a composite ranking weights.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WeightFamily {
    /// `momentum`.
    Momentum,
    /// `path_quality`.
    PathQuality,
    /// `trend_structure`.
    TrendStructure,
    /// `participation`.
    Participation,
    /// `risk_execution`.
    RiskExecution,
}

/// docs/ranking/PLAN.md C1 — what evidence stands behind a factor as a ranking input.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationStatus {
    /// Pre-Phase-2 factors.
    Legacy,
    /// Phase-2 factors awaiting hold-out evidence.
    Research,
    /// Promoted by C8's evidence.
    Validated,
    /// Refused by C8's evidence.
    Rejected,
}

/// The display unit of a factor or column.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FactorUnit {
    /// A percentage.
    Percent,
    /// A decimal fraction rendered as a percentage (volatility).
    Fraction,
    /// A ratio.
    Ratio,
    /// An index value.
    Index,
    /// Rupees.
    Rupees,
    /// Rupees crore.
    Crore,
    /// A price.
    Price,
    /// A plain integer count (circuit-hit days).
    Count,
    /// Not a number at all (`series`).
    Text,
}

/// What a NULL means for a factor, which decides how it sorts and filters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum NullPolicy {
    /// NULL because the instrument lacks a full window (docs/05 §Notation).
    InsufficientHistory,
    /// NULL because a component was NULL (docs/05 §4).
    NullIfAnyComponentNull,
    /// NULL because the denominator was zero or non-positive (docs/05 §3, §7).
    NullOnUndefinedRatio,
    /// NULL because the upstream source does not publish it (docs/05 §14).
    NullIfUnpublished,
}

/// `baskfy_core.ranking.FactorPreference` — how a factor is used when ranking or filtering.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FactorPreference {
    /// Monotonic, larger is better.
    Higher,
    /// Monotonic, smaller is better.
    Lower,
    /// Larger is not better; prefer a band.
    TargetRange,
    /// Gate only; never a sort key by itself.
    Eligibility,
}

/// One registry entry, carrying every field docs/06 §"The factor registry" lists plus C1's.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Factor {
    /// The key a definition names.
    pub key: String,
    /// The reference product's own wording.
    pub label: String,
    /// The Sort By dropdown's grouping.
    pub family: FactorFamily,
    /// The whitelisted SQL expression (unqualified column names).
    pub sql_expr: String,
    /// Display unit.
    pub unit: FactorUnit,
    /// The default sort direction's meaning.
    pub higher_is_better: bool,
    /// What a NULL means.
    pub null_policy: NullPolicy,
    /// The stored `factor_daily` columns this reads; empty for a stored column itself.
    pub components: Vec<String>,
    /// True when the factor is a stored column rather than an expression.
    pub is_stored: bool,
    /// True when ranking is computed in-process, not by SQL `ROW_NUMBER`.
    pub is_computed: bool,
    /// How the ranking engine treats this key.
    pub preference: FactorPreference,
    /// C1: which composite weight family this factor contributes to.
    pub weight_family: WeightFamily,
    /// C1: the evidence behind it as a ranking input.
    pub validation_status: ValidationStatus,
    /// C1: one sentence saying what the number is.
    pub definition: String,
    /// C1: false for filter/column-only factors a ranking term must refuse.
    pub rankable: bool,
}

impl Factor {
    /// True for a blend (more than one component).
    pub fn is_blend(&self) -> bool {
        self.components.len() > 1
    }
}

/// One entry of docs/01 §4's column picker, as `GET /meta/columns` returns it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ResultColumn {
    /// The column key.
    pub key: String,
    /// Its display label.
    pub label: String,
    /// Its unit.
    pub unit: FactorUnit,
    /// True when the column is also a ranking factor.
    pub is_factor: bool,
}

/// What the registry raises. Python raises `KeyError` for an unknown key and `ValueError` for a
/// computed key asked for SQL.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum RegistryError {
    /// Python's `KeyError`: the key is not in the registry.
    #[error("{0}")]
    UnknownFactor(String),
    /// Python's `ValueError`: a computed factor has no SQL.
    #[error("{0}")]
    Computed(String),
}

impl RegistryError {
    /// The Python exception class this error stands for.
    pub fn python_type(&self) -> &'static str {
        match self {
            RegistryError::UnknownFactor(_) => "KeyError",
            RegistryError::Computed(_) => "ValueError",
        }
    }

    /// The Python exception's message (`exc.args[0]`).
    pub fn message(&self) -> &str {
        match self {
            RegistryError::UnknownFactor(m) | RegistryError::Computed(m) => m,
        }
    }
}

impl From<RegistryError> for CoreError {
    fn from(e: RegistryError) -> Self {
        CoreError::InvalidInput(e.message().to_owned())
    }
}

/// Python's `repr()` of a `str`: single quotes unless the text holds a single quote and no
/// double quote; backslash, the quote and every character `str.isprintable` rejects escaped
/// (`\xNN`, `\uNNNN`, `\UNNNNNNNN`).
pub fn py_repr_str(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if !py_isprintable(c) => {
                let code = c as u32;
                if code < 0x100 {
                    out.push_str(&format!("\\x{code:02x}"));
                } else if code < 0x1_0000 {
                    out.push_str(&format!("\\u{code:04x}"));
                } else {
                    out.push_str(&format!("\\U{code:08x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python's `str.isprintable` (CPython 3.12, Unicode 15.0.0): false for categories Cc, Cf, Cs,
/// Co, Cn, Zl, Zp and Zs other than the space. `repr` escapes exactly these.
fn py_isprintable(c: char) -> bool {
    let code = c as u32;
    NON_PRINTABLE
        .binary_search_by(|&(lo, hi)| {
            if hi < code {
                std::cmp::Ordering::Less
            } else if lo > code {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_err()
}

/// The code-point ranges (inclusive) CPython 3.12's `str.isprintable` rejects, generated from
/// its `unicodedata` (Unicode 15.0.0).
const NON_PRINTABLE: &[(u32, u32)] = &[
    (0x0, 0x1f),
    (0x7f, 0xa0),
    (0xad, 0xad),
    (0x378, 0x379),
    (0x380, 0x383),
    (0x38b, 0x38b),
    (0x38d, 0x38d),
    (0x3a2, 0x3a2),
    (0x530, 0x530),
    (0x557, 0x558),
    (0x58b, 0x58c),
    (0x590, 0x590),
    (0x5c8, 0x5cf),
    (0x5eb, 0x5ee),
    (0x5f5, 0x605),
    (0x61c, 0x61c),
    (0x6dd, 0x6dd),
    (0x70e, 0x70f),
    (0x74b, 0x74c),
    (0x7b2, 0x7bf),
    (0x7fb, 0x7fc),
    (0x82e, 0x82f),
    (0x83f, 0x83f),
    (0x85c, 0x85d),
    (0x85f, 0x85f),
    (0x86b, 0x86f),
    (0x88f, 0x897),
    (0x8e2, 0x8e2),
    (0x984, 0x984),
    (0x98d, 0x98e),
    (0x991, 0x992),
    (0x9a9, 0x9a9),
    (0x9b1, 0x9b1),
    (0x9b3, 0x9b5),
    (0x9ba, 0x9bb),
    (0x9c5, 0x9c6),
    (0x9c9, 0x9ca),
    (0x9cf, 0x9d6),
    (0x9d8, 0x9db),
    (0x9de, 0x9de),
    (0x9e4, 0x9e5),
    (0x9ff, 0xa00),
    (0xa04, 0xa04),
    (0xa0b, 0xa0e),
    (0xa11, 0xa12),
    (0xa29, 0xa29),
    (0xa31, 0xa31),
    (0xa34, 0xa34),
    (0xa37, 0xa37),
    (0xa3a, 0xa3b),
    (0xa3d, 0xa3d),
    (0xa43, 0xa46),
    (0xa49, 0xa4a),
    (0xa4e, 0xa50),
    (0xa52, 0xa58),
    (0xa5d, 0xa5d),
    (0xa5f, 0xa65),
    (0xa77, 0xa80),
    (0xa84, 0xa84),
    (0xa8e, 0xa8e),
    (0xa92, 0xa92),
    (0xaa9, 0xaa9),
    (0xab1, 0xab1),
    (0xab4, 0xab4),
    (0xaba, 0xabb),
    (0xac6, 0xac6),
    (0xaca, 0xaca),
    (0xace, 0xacf),
    (0xad1, 0xadf),
    (0xae4, 0xae5),
    (0xaf2, 0xaf8),
    (0xb00, 0xb00),
    (0xb04, 0xb04),
    (0xb0d, 0xb0e),
    (0xb11, 0xb12),
    (0xb29, 0xb29),
    (0xb31, 0xb31),
    (0xb34, 0xb34),
    (0xb3a, 0xb3b),
    (0xb45, 0xb46),
    (0xb49, 0xb4a),
    (0xb4e, 0xb54),
    (0xb58, 0xb5b),
    (0xb5e, 0xb5e),
    (0xb64, 0xb65),
    (0xb78, 0xb81),
    (0xb84, 0xb84),
    (0xb8b, 0xb8d),
    (0xb91, 0xb91),
    (0xb96, 0xb98),
    (0xb9b, 0xb9b),
    (0xb9d, 0xb9d),
    (0xba0, 0xba2),
    (0xba5, 0xba7),
    (0xbab, 0xbad),
    (0xbba, 0xbbd),
    (0xbc3, 0xbc5),
    (0xbc9, 0xbc9),
    (0xbce, 0xbcf),
    (0xbd1, 0xbd6),
    (0xbd8, 0xbe5),
    (0xbfb, 0xbff),
    (0xc0d, 0xc0d),
    (0xc11, 0xc11),
    (0xc29, 0xc29),
    (0xc3a, 0xc3b),
    (0xc45, 0xc45),
    (0xc49, 0xc49),
    (0xc4e, 0xc54),
    (0xc57, 0xc57),
    (0xc5b, 0xc5c),
    (0xc5e, 0xc5f),
    (0xc64, 0xc65),
    (0xc70, 0xc76),
    (0xc8d, 0xc8d),
    (0xc91, 0xc91),
    (0xca9, 0xca9),
    (0xcb4, 0xcb4),
    (0xcba, 0xcbb),
    (0xcc5, 0xcc5),
    (0xcc9, 0xcc9),
    (0xcce, 0xcd4),
    (0xcd7, 0xcdc),
    (0xcdf, 0xcdf),
    (0xce4, 0xce5),
    (0xcf0, 0xcf0),
    (0xcf4, 0xcff),
    (0xd0d, 0xd0d),
    (0xd11, 0xd11),
    (0xd45, 0xd45),
    (0xd49, 0xd49),
    (0xd50, 0xd53),
    (0xd64, 0xd65),
    (0xd80, 0xd80),
    (0xd84, 0xd84),
    (0xd97, 0xd99),
    (0xdb2, 0xdb2),
    (0xdbc, 0xdbc),
    (0xdbe, 0xdbf),
    (0xdc7, 0xdc9),
    (0xdcb, 0xdce),
    (0xdd5, 0xdd5),
    (0xdd7, 0xdd7),
    (0xde0, 0xde5),
    (0xdf0, 0xdf1),
    (0xdf5, 0xe00),
    (0xe3b, 0xe3e),
    (0xe5c, 0xe80),
    (0xe83, 0xe83),
    (0xe85, 0xe85),
    (0xe8b, 0xe8b),
    (0xea4, 0xea4),
    (0xea6, 0xea6),
    (0xebe, 0xebf),
    (0xec5, 0xec5),
    (0xec7, 0xec7),
    (0xecf, 0xecf),
    (0xeda, 0xedb),
    (0xee0, 0xeff),
    (0xf48, 0xf48),
    (0xf6d, 0xf70),
    (0xf98, 0xf98),
    (0xfbd, 0xfbd),
    (0xfcd, 0xfcd),
    (0xfdb, 0xfff),
    (0x10c6, 0x10c6),
    (0x10c8, 0x10cc),
    (0x10ce, 0x10cf),
    (0x1249, 0x1249),
    (0x124e, 0x124f),
    (0x1257, 0x1257),
    (0x1259, 0x1259),
    (0x125e, 0x125f),
    (0x1289, 0x1289),
    (0x128e, 0x128f),
    (0x12b1, 0x12b1),
    (0x12b6, 0x12b7),
    (0x12bf, 0x12bf),
    (0x12c1, 0x12c1),
    (0x12c6, 0x12c7),
    (0x12d7, 0x12d7),
    (0x1311, 0x1311),
    (0x1316, 0x1317),
    (0x135b, 0x135c),
    (0x137d, 0x137f),
    (0x139a, 0x139f),
    (0x13f6, 0x13f7),
    (0x13fe, 0x13ff),
    (0x1680, 0x1680),
    (0x169d, 0x169f),
    (0x16f9, 0x16ff),
    (0x1716, 0x171e),
    (0x1737, 0x173f),
    (0x1754, 0x175f),
    (0x176d, 0x176d),
    (0x1771, 0x1771),
    (0x1774, 0x177f),
    (0x17de, 0x17df),
    (0x17ea, 0x17ef),
    (0x17fa, 0x17ff),
    (0x180e, 0x180e),
    (0x181a, 0x181f),
    (0x1879, 0x187f),
    (0x18ab, 0x18af),
    (0x18f6, 0x18ff),
    (0x191f, 0x191f),
    (0x192c, 0x192f),
    (0x193c, 0x193f),
    (0x1941, 0x1943),
    (0x196e, 0x196f),
    (0x1975, 0x197f),
    (0x19ac, 0x19af),
    (0x19ca, 0x19cf),
    (0x19db, 0x19dd),
    (0x1a1c, 0x1a1d),
    (0x1a5f, 0x1a5f),
    (0x1a7d, 0x1a7e),
    (0x1a8a, 0x1a8f),
    (0x1a9a, 0x1a9f),
    (0x1aae, 0x1aaf),
    (0x1acf, 0x1aff),
    (0x1b4d, 0x1b4f),
    (0x1b7f, 0x1b7f),
    (0x1bf4, 0x1bfb),
    (0x1c38, 0x1c3a),
    (0x1c4a, 0x1c4c),
    (0x1c89, 0x1c8f),
    (0x1cbb, 0x1cbc),
    (0x1cc8, 0x1ccf),
    (0x1cfb, 0x1cff),
    (0x1f16, 0x1f17),
    (0x1f1e, 0x1f1f),
    (0x1f46, 0x1f47),
    (0x1f4e, 0x1f4f),
    (0x1f58, 0x1f58),
    (0x1f5a, 0x1f5a),
    (0x1f5c, 0x1f5c),
    (0x1f5e, 0x1f5e),
    (0x1f7e, 0x1f7f),
    (0x1fb5, 0x1fb5),
    (0x1fc5, 0x1fc5),
    (0x1fd4, 0x1fd5),
    (0x1fdc, 0x1fdc),
    (0x1ff0, 0x1ff1),
    (0x1ff5, 0x1ff5),
    (0x1fff, 0x200f),
    (0x2028, 0x202f),
    (0x205f, 0x206f),
    (0x2072, 0x2073),
    (0x208f, 0x208f),
    (0x209d, 0x209f),
    (0x20c1, 0x20cf),
    (0x20f1, 0x20ff),
    (0x218c, 0x218f),
    (0x2427, 0x243f),
    (0x244b, 0x245f),
    (0x2b74, 0x2b75),
    (0x2b96, 0x2b96),
    (0x2cf4, 0x2cf8),
    (0x2d26, 0x2d26),
    (0x2d28, 0x2d2c),
    (0x2d2e, 0x2d2f),
    (0x2d68, 0x2d6e),
    (0x2d71, 0x2d7e),
    (0x2d97, 0x2d9f),
    (0x2da7, 0x2da7),
    (0x2daf, 0x2daf),
    (0x2db7, 0x2db7),
    (0x2dbf, 0x2dbf),
    (0x2dc7, 0x2dc7),
    (0x2dcf, 0x2dcf),
    (0x2dd7, 0x2dd7),
    (0x2ddf, 0x2ddf),
    (0x2e5e, 0x2e7f),
    (0x2e9a, 0x2e9a),
    (0x2ef4, 0x2eff),
    (0x2fd6, 0x2fef),
    (0x2ffc, 0x3000),
    (0x3040, 0x3040),
    (0x3097, 0x3098),
    (0x3100, 0x3104),
    (0x3130, 0x3130),
    (0x318f, 0x318f),
    (0x31e4, 0x31ef),
    (0x321f, 0x321f),
    (0xa48d, 0xa48f),
    (0xa4c7, 0xa4cf),
    (0xa62c, 0xa63f),
    (0xa6f8, 0xa6ff),
    (0xa7cb, 0xa7cf),
    (0xa7d2, 0xa7d2),
    (0xa7d4, 0xa7d4),
    (0xa7da, 0xa7f1),
    (0xa82d, 0xa82f),
    (0xa83a, 0xa83f),
    (0xa878, 0xa87f),
    (0xa8c6, 0xa8cd),
    (0xa8da, 0xa8df),
    (0xa954, 0xa95e),
    (0xa97d, 0xa97f),
    (0xa9ce, 0xa9ce),
    (0xa9da, 0xa9dd),
    (0xa9ff, 0xa9ff),
    (0xaa37, 0xaa3f),
    (0xaa4e, 0xaa4f),
    (0xaa5a, 0xaa5b),
    (0xaac3, 0xaada),
    (0xaaf7, 0xab00),
    (0xab07, 0xab08),
    (0xab0f, 0xab10),
    (0xab17, 0xab1f),
    (0xab27, 0xab27),
    (0xab2f, 0xab2f),
    (0xab6c, 0xab6f),
    (0xabee, 0xabef),
    (0xabfa, 0xabff),
    (0xd7a4, 0xd7af),
    (0xd7c7, 0xd7ca),
    (0xd7fc, 0xf8ff),
    (0xfa6e, 0xfa6f),
    (0xfada, 0xfaff),
    (0xfb07, 0xfb12),
    (0xfb18, 0xfb1c),
    (0xfb37, 0xfb37),
    (0xfb3d, 0xfb3d),
    (0xfb3f, 0xfb3f),
    (0xfb42, 0xfb42),
    (0xfb45, 0xfb45),
    (0xfbc3, 0xfbd2),
    (0xfd90, 0xfd91),
    (0xfdc8, 0xfdce),
    (0xfdd0, 0xfdef),
    (0xfe1a, 0xfe1f),
    (0xfe53, 0xfe53),
    (0xfe67, 0xfe67),
    (0xfe6c, 0xfe6f),
    (0xfe75, 0xfe75),
    (0xfefd, 0xff00),
    (0xffbf, 0xffc1),
    (0xffc8, 0xffc9),
    (0xffd0, 0xffd1),
    (0xffd8, 0xffd9),
    (0xffdd, 0xffdf),
    (0xffe7, 0xffe7),
    (0xffef, 0xfffb),
    (0xfffe, 0xffff),
    (0x1000c, 0x1000c),
    (0x10027, 0x10027),
    (0x1003b, 0x1003b),
    (0x1003e, 0x1003e),
    (0x1004e, 0x1004f),
    (0x1005e, 0x1007f),
    (0x100fb, 0x100ff),
    (0x10103, 0x10106),
    (0x10134, 0x10136),
    (0x1018f, 0x1018f),
    (0x1019d, 0x1019f),
    (0x101a1, 0x101cf),
    (0x101fe, 0x1027f),
    (0x1029d, 0x1029f),
    (0x102d1, 0x102df),
    (0x102fc, 0x102ff),
    (0x10324, 0x1032c),
    (0x1034b, 0x1034f),
    (0x1037b, 0x1037f),
    (0x1039e, 0x1039e),
    (0x103c4, 0x103c7),
    (0x103d6, 0x103ff),
    (0x1049e, 0x1049f),
    (0x104aa, 0x104af),
    (0x104d4, 0x104d7),
    (0x104fc, 0x104ff),
    (0x10528, 0x1052f),
    (0x10564, 0x1056e),
    (0x1057b, 0x1057b),
    (0x1058b, 0x1058b),
    (0x10593, 0x10593),
    (0x10596, 0x10596),
    (0x105a2, 0x105a2),
    (0x105b2, 0x105b2),
    (0x105ba, 0x105ba),
    (0x105bd, 0x105ff),
    (0x10737, 0x1073f),
    (0x10756, 0x1075f),
    (0x10768, 0x1077f),
    (0x10786, 0x10786),
    (0x107b1, 0x107b1),
    (0x107bb, 0x107ff),
    (0x10806, 0x10807),
    (0x10809, 0x10809),
    (0x10836, 0x10836),
    (0x10839, 0x1083b),
    (0x1083d, 0x1083e),
    (0x10856, 0x10856),
    (0x1089f, 0x108a6),
    (0x108b0, 0x108df),
    (0x108f3, 0x108f3),
    (0x108f6, 0x108fa),
    (0x1091c, 0x1091e),
    (0x1093a, 0x1093e),
    (0x10940, 0x1097f),
    (0x109b8, 0x109bb),
    (0x109d0, 0x109d1),
    (0x10a04, 0x10a04),
    (0x10a07, 0x10a0b),
    (0x10a14, 0x10a14),
    (0x10a18, 0x10a18),
    (0x10a36, 0x10a37),
    (0x10a3b, 0x10a3e),
    (0x10a49, 0x10a4f),
    (0x10a59, 0x10a5f),
    (0x10aa0, 0x10abf),
    (0x10ae7, 0x10aea),
    (0x10af7, 0x10aff),
    (0x10b36, 0x10b38),
    (0x10b56, 0x10b57),
    (0x10b73, 0x10b77),
    (0x10b92, 0x10b98),
    (0x10b9d, 0x10ba8),
    (0x10bb0, 0x10bff),
    (0x10c49, 0x10c7f),
    (0x10cb3, 0x10cbf),
    (0x10cf3, 0x10cf9),
    (0x10d28, 0x10d2f),
    (0x10d3a, 0x10e5f),
    (0x10e7f, 0x10e7f),
    (0x10eaa, 0x10eaa),
    (0x10eae, 0x10eaf),
    (0x10eb2, 0x10efc),
    (0x10f28, 0x10f2f),
    (0x10f5a, 0x10f6f),
    (0x10f8a, 0x10faf),
    (0x10fcc, 0x10fdf),
    (0x10ff7, 0x10fff),
    (0x1104e, 0x11051),
    (0x11076, 0x1107e),
    (0x110bd, 0x110bd),
    (0x110c3, 0x110cf),
    (0x110e9, 0x110ef),
    (0x110fa, 0x110ff),
    (0x11135, 0x11135),
    (0x11148, 0x1114f),
    (0x11177, 0x1117f),
    (0x111e0, 0x111e0),
    (0x111f5, 0x111ff),
    (0x11212, 0x11212),
    (0x11242, 0x1127f),
    (0x11287, 0x11287),
    (0x11289, 0x11289),
    (0x1128e, 0x1128e),
    (0x1129e, 0x1129e),
    (0x112aa, 0x112af),
    (0x112eb, 0x112ef),
    (0x112fa, 0x112ff),
    (0x11304, 0x11304),
    (0x1130d, 0x1130e),
    (0x11311, 0x11312),
    (0x11329, 0x11329),
    (0x11331, 0x11331),
    (0x11334, 0x11334),
    (0x1133a, 0x1133a),
    (0x11345, 0x11346),
    (0x11349, 0x1134a),
    (0x1134e, 0x1134f),
    (0x11351, 0x11356),
    (0x11358, 0x1135c),
    (0x11364, 0x11365),
    (0x1136d, 0x1136f),
    (0x11375, 0x113ff),
    (0x1145c, 0x1145c),
    (0x11462, 0x1147f),
    (0x114c8, 0x114cf),
    (0x114da, 0x1157f),
    (0x115b6, 0x115b7),
    (0x115de, 0x115ff),
    (0x11645, 0x1164f),
    (0x1165a, 0x1165f),
    (0x1166d, 0x1167f),
    (0x116ba, 0x116bf),
    (0x116ca, 0x116ff),
    (0x1171b, 0x1171c),
    (0x1172c, 0x1172f),
    (0x11747, 0x117ff),
    (0x1183c, 0x1189f),
    (0x118f3, 0x118fe),
    (0x11907, 0x11908),
    (0x1190a, 0x1190b),
    (0x11914, 0x11914),
    (0x11917, 0x11917),
    (0x11936, 0x11936),
    (0x11939, 0x1193a),
    (0x11947, 0x1194f),
    (0x1195a, 0x1199f),
    (0x119a8, 0x119a9),
    (0x119d8, 0x119d9),
    (0x119e5, 0x119ff),
    (0x11a48, 0x11a4f),
    (0x11aa3, 0x11aaf),
    (0x11af9, 0x11aff),
    (0x11b0a, 0x11bff),
    (0x11c09, 0x11c09),
    (0x11c37, 0x11c37),
    (0x11c46, 0x11c4f),
    (0x11c6d, 0x11c6f),
    (0x11c90, 0x11c91),
    (0x11ca8, 0x11ca8),
    (0x11cb7, 0x11cff),
    (0x11d07, 0x11d07),
    (0x11d0a, 0x11d0a),
    (0x11d37, 0x11d39),
    (0x11d3b, 0x11d3b),
    (0x11d3e, 0x11d3e),
    (0x11d48, 0x11d4f),
    (0x11d5a, 0x11d5f),
    (0x11d66, 0x11d66),
    (0x11d69, 0x11d69),
    (0x11d8f, 0x11d8f),
    (0x11d92, 0x11d92),
    (0x11d99, 0x11d9f),
    (0x11daa, 0x11edf),
    (0x11ef9, 0x11eff),
    (0x11f11, 0x11f11),
    (0x11f3b, 0x11f3d),
    (0x11f5a, 0x11faf),
    (0x11fb1, 0x11fbf),
    (0x11ff2, 0x11ffe),
    (0x1239a, 0x123ff),
    (0x1246f, 0x1246f),
    (0x12475, 0x1247f),
    (0x12544, 0x12f8f),
    (0x12ff3, 0x12fff),
    (0x13430, 0x1343f),
    (0x13456, 0x143ff),
    (0x14647, 0x167ff),
    (0x16a39, 0x16a3f),
    (0x16a5f, 0x16a5f),
    (0x16a6a, 0x16a6d),
    (0x16abf, 0x16abf),
    (0x16aca, 0x16acf),
    (0x16aee, 0x16aef),
    (0x16af6, 0x16aff),
    (0x16b46, 0x16b4f),
    (0x16b5a, 0x16b5a),
    (0x16b62, 0x16b62),
    (0x16b78, 0x16b7c),
    (0x16b90, 0x16e3f),
    (0x16e9b, 0x16eff),
    (0x16f4b, 0x16f4e),
    (0x16f88, 0x16f8e),
    (0x16fa0, 0x16fdf),
    (0x16fe5, 0x16fef),
    (0x16ff2, 0x16fff),
    (0x187f8, 0x187ff),
    (0x18cd6, 0x18cff),
    (0x18d09, 0x1afef),
    (0x1aff4, 0x1aff4),
    (0x1affc, 0x1affc),
    (0x1afff, 0x1afff),
    (0x1b123, 0x1b131),
    (0x1b133, 0x1b14f),
    (0x1b153, 0x1b154),
    (0x1b156, 0x1b163),
    (0x1b168, 0x1b16f),
    (0x1b2fc, 0x1bbff),
    (0x1bc6b, 0x1bc6f),
    (0x1bc7d, 0x1bc7f),
    (0x1bc89, 0x1bc8f),
    (0x1bc9a, 0x1bc9b),
    (0x1bca0, 0x1ceff),
    (0x1cf2e, 0x1cf2f),
    (0x1cf47, 0x1cf4f),
    (0x1cfc4, 0x1cfff),
    (0x1d0f6, 0x1d0ff),
    (0x1d127, 0x1d128),
    (0x1d173, 0x1d17a),
    (0x1d1eb, 0x1d1ff),
    (0x1d246, 0x1d2bf),
    (0x1d2d4, 0x1d2df),
    (0x1d2f4, 0x1d2ff),
    (0x1d357, 0x1d35f),
    (0x1d379, 0x1d3ff),
    (0x1d455, 0x1d455),
    (0x1d49d, 0x1d49d),
    (0x1d4a0, 0x1d4a1),
    (0x1d4a3, 0x1d4a4),
    (0x1d4a7, 0x1d4a8),
    (0x1d4ad, 0x1d4ad),
    (0x1d4ba, 0x1d4ba),
    (0x1d4bc, 0x1d4bc),
    (0x1d4c4, 0x1d4c4),
    (0x1d506, 0x1d506),
    (0x1d50b, 0x1d50c),
    (0x1d515, 0x1d515),
    (0x1d51d, 0x1d51d),
    (0x1d53a, 0x1d53a),
    (0x1d53f, 0x1d53f),
    (0x1d545, 0x1d545),
    (0x1d547, 0x1d549),
    (0x1d551, 0x1d551),
    (0x1d6a6, 0x1d6a7),
    (0x1d7cc, 0x1d7cd),
    (0x1da8c, 0x1da9a),
    (0x1daa0, 0x1daa0),
    (0x1dab0, 0x1deff),
    (0x1df1f, 0x1df24),
    (0x1df2b, 0x1dfff),
    (0x1e007, 0x1e007),
    (0x1e019, 0x1e01a),
    (0x1e022, 0x1e022),
    (0x1e025, 0x1e025),
    (0x1e02b, 0x1e02f),
    (0x1e06e, 0x1e08e),
    (0x1e090, 0x1e0ff),
    (0x1e12d, 0x1e12f),
    (0x1e13e, 0x1e13f),
    (0x1e14a, 0x1e14d),
    (0x1e150, 0x1e28f),
    (0x1e2af, 0x1e2bf),
    (0x1e2fa, 0x1e2fe),
    (0x1e300, 0x1e4cf),
    (0x1e4fa, 0x1e7df),
    (0x1e7e7, 0x1e7e7),
    (0x1e7ec, 0x1e7ec),
    (0x1e7ef, 0x1e7ef),
    (0x1e7ff, 0x1e7ff),
    (0x1e8c5, 0x1e8c6),
    (0x1e8d7, 0x1e8ff),
    (0x1e94c, 0x1e94f),
    (0x1e95a, 0x1e95d),
    (0x1e960, 0x1ec70),
    (0x1ecb5, 0x1ed00),
    (0x1ed3e, 0x1edff),
    (0x1ee04, 0x1ee04),
    (0x1ee20, 0x1ee20),
    (0x1ee23, 0x1ee23),
    (0x1ee25, 0x1ee26),
    (0x1ee28, 0x1ee28),
    (0x1ee33, 0x1ee33),
    (0x1ee38, 0x1ee38),
    (0x1ee3a, 0x1ee3a),
    (0x1ee3c, 0x1ee41),
    (0x1ee43, 0x1ee46),
    (0x1ee48, 0x1ee48),
    (0x1ee4a, 0x1ee4a),
    (0x1ee4c, 0x1ee4c),
    (0x1ee50, 0x1ee50),
    (0x1ee53, 0x1ee53),
    (0x1ee55, 0x1ee56),
    (0x1ee58, 0x1ee58),
    (0x1ee5a, 0x1ee5a),
    (0x1ee5c, 0x1ee5c),
    (0x1ee5e, 0x1ee5e),
    (0x1ee60, 0x1ee60),
    (0x1ee63, 0x1ee63),
    (0x1ee65, 0x1ee66),
    (0x1ee6b, 0x1ee6b),
    (0x1ee73, 0x1ee73),
    (0x1ee78, 0x1ee78),
    (0x1ee7d, 0x1ee7d),
    (0x1ee7f, 0x1ee7f),
    (0x1ee8a, 0x1ee8a),
    (0x1ee9c, 0x1eea0),
    (0x1eea4, 0x1eea4),
    (0x1eeaa, 0x1eeaa),
    (0x1eebc, 0x1eeef),
    (0x1eef2, 0x1efff),
    (0x1f02c, 0x1f02f),
    (0x1f094, 0x1f09f),
    (0x1f0af, 0x1f0b0),
    (0x1f0c0, 0x1f0c0),
    (0x1f0d0, 0x1f0d0),
    (0x1f0f6, 0x1f0ff),
    (0x1f1ae, 0x1f1e5),
    (0x1f203, 0x1f20f),
    (0x1f23c, 0x1f23f),
    (0x1f249, 0x1f24f),
    (0x1f252, 0x1f25f),
    (0x1f266, 0x1f2ff),
    (0x1f6d8, 0x1f6db),
    (0x1f6ed, 0x1f6ef),
    (0x1f6fd, 0x1f6ff),
    (0x1f777, 0x1f77a),
    (0x1f7da, 0x1f7df),
    (0x1f7ec, 0x1f7ef),
    (0x1f7f1, 0x1f7ff),
    (0x1f80c, 0x1f80f),
    (0x1f848, 0x1f84f),
    (0x1f85a, 0x1f85f),
    (0x1f888, 0x1f88f),
    (0x1f8ae, 0x1f8af),
    (0x1f8b2, 0x1f8ff),
    (0x1fa54, 0x1fa5f),
    (0x1fa6e, 0x1fa6f),
    (0x1fa7d, 0x1fa7f),
    (0x1fa89, 0x1fa8f),
    (0x1fabe, 0x1fabe),
    (0x1fac6, 0x1facd),
    (0x1fadc, 0x1fadf),
    (0x1fae9, 0x1faef),
    (0x1faf9, 0x1faff),
    (0x1fb93, 0x1fb93),
    (0x1fbcb, 0x1fbef),
    (0x1fbfa, 0x1ffff),
    (0x2a6e0, 0x2a6ff),
    (0x2b73a, 0x2b73f),
    (0x2b81e, 0x2b81f),
    (0x2cea2, 0x2ceaf),
    (0x2ebe1, 0x2f7ff),
    (0x2fa1e, 0x2ffff),
    (0x3134b, 0x3134f),
    (0x323b0, 0xe00ff),
    (0xe01f0, 0x10ffff),
];

/// `baskfy_core.blends.shape_suffix`: `(12, 6, 3, 1)` -> `12_6_3_1`.
pub fn shape_suffix(shape: &[u32]) -> String {
    shape
        .iter()
        .map(u32::to_string)
        .collect::<Vec<_>>()
        .join("_")
}

/// `baskfy_core.blends.shape_label`: `(12, 6, 3, 1)` -> `12 6 3 1`.
pub fn shape_label(shape: &[u32]) -> String {
    shape
        .iter()
        .map(u32::to_string)
        .collect::<Vec<_>>()
        .join(" ")
}

/// `baskfy_core.blends.blend_sql`: the mean of the stored component columns, NULL if any is
/// NULL by PostgreSQL's own propagation (no COALESCE, deliberately).
pub fn blend_sql(components: &[String]) -> Result<String, CoreError> {
    if components.is_empty() {
        return Err(CoreError::InvalidInput(
            "a blend needs at least one component".to_owned(),
        ));
    }
    Ok(format!(
        "(({}) / {}.0)",
        components.join(" + "),
        components.len()
    ))
}

/// docs/07's own wording: "1 YEAR", "6 MONTHS", "1 MONTH".
fn window_label(months: u32) -> String {
    if months == MONTHS_IN_A_YEAR {
        return "1 YEAR".to_owned();
    }
    if months > 1 {
        format!("{months} MONTHS")
    } else {
        "1 MONTH".to_owned()
    }
}

/// The registry: factors in insertion order plus a key index.
struct Registry {
    factors: Vec<Factor>,
    index: HashMap<String, usize>,
    duplicates: Vec<String>,
}

impl Registry {
    fn add(&mut self, factor: Factor) {
        // Python raises ValueError on a duplicate key; the table is static, so the check is
        // kept as a recorded list that a test asserts empty instead of a runtime failure.
        if self.index.contains_key(&factor.key) {
            self.duplicates.push(factor.key);
            return;
        }
        self.index.insert(factor.key.clone(), self.factors.len());
        self.factors.push(factor);
    }
}

/// Fields every builder call shares; the rest default as in the Python dataclass.
struct Spec<'a> {
    key: String,
    label: String,
    family: FactorFamily,
    sql_expr: String,
    unit: FactorUnit,
    higher_is_better: bool,
    null_policy: NullPolicy,
    weight_family: WeightFamily,
    validation_status: ValidationStatus,
    definition: String,
    components: &'a [String],
}

fn factor(spec: Spec<'_>) -> Factor {
    Factor {
        key: spec.key,
        label: spec.label,
        family: spec.family,
        sql_expr: spec.sql_expr,
        unit: spec.unit,
        higher_is_better: spec.higher_is_better,
        null_policy: spec.null_policy,
        components: spec.components.to_vec(),
        is_stored: false,
        is_computed: false,
        preference: FactorPreference::Higher,
        weight_family: spec.weight_family,
        validation_status: spec.validation_status,
        definition: spec.definition,
        rankable: true,
    }
}

fn stored(spec: Spec<'_>) -> Factor {
    Factor {
        is_stored: true,
        ..factor(spec)
    }
}

/// `blend_sql` over shapes that are never empty; an empty shape cannot occur in the tables
/// above, so the fallback is unreachable and keeps the builder panic-free.
fn blend(components: &[String]) -> String {
    blend_sql(components).unwrap_or_default()
}

fn build() -> Registry {
    let mut r = Registry {
        factors: Vec::new(),
        index: HashMap::new(),
        duplicates: Vec::new(),
    };
    let none: &[String] = &[];

    // --- Absolute return: 5 singles + 11 blends = 16 (docs/01 §3) ---------------
    for months in WINDOW_MONTHS {
        let column = format!("ret_{months}m");
        r.add(stored(Spec {
            key: column.clone(),
            label: format!("ABSOLUTE RETURN {}", window_label(months)),
            family: FactorFamily::AbsoluteReturn,
            sql_expr: column,
            unit: FactorUnit::Percent,
            higher_is_better: true,
            null_policy: NullPolicy::InsufficientHistory,
            weight_family: WeightFamily::Momentum,
            validation_status: ValidationStatus::Legacy,
            definition: format!(
                "Percent change of the adjusted close over the {months}-month calendar window (docs/05 §1)."
            ),
            components: none,
        }));
    }
    for shape in BLEND_SHAPES {
        let components: Vec<String> = shape.iter().map(|m| format!("ret_{m}m")).collect();
        r.add(factor(Spec {
            key: format!("avg_return_{}", shape_suffix(shape)),
            label: format!("AVERAGE ABSOLUTE RETURN {} MONTHS", shape_label(shape)),
            family: FactorFamily::AbsoluteReturn,
            sql_expr: blend(&components),
            unit: FactorUnit::Percent,
            higher_is_better: true,
            null_policy: NullPolicy::NullIfAnyComponentNull,
            weight_family: WeightFamily::Momentum,
            validation_status: ValidationStatus::Legacy,
            definition: format!(
                "Mean of the {}-month absolute returns, NULL if any is NULL (docs/05 §4).",
                shape_label(shape)
            ),
            components: &components,
        }));
    }

    // --- Sharpe return: 5 singles + 11 blends + 6·3, 3·1, 6·1 ---------------------
    for months in WINDOW_MONTHS {
        let column = format!("sharpe_{months}m");
        r.add(stored(Spec {
            key: column.clone(),
            label: format!("SHARPE RETURN {}", window_label(months)),
            family: FactorFamily::SharpeReturn,
            sql_expr: column,
            unit: FactorUnit::Ratio,
            higher_is_better: true,
            null_policy: NullPolicy::NullOnUndefinedRatio,
            weight_family: WeightFamily::Momentum,
            validation_status: ValidationStatus::Legacy,
            definition: format!(
                "The {months}-month return divided by 100 x its annualised volatility (docs/05 §3)."
            ),
            components: none,
        }));
    }
    for shape in BLEND_SHAPES.iter().chain(SHARPE_ONLY_SHAPES.iter()) {
        let components: Vec<String> = shape.iter().map(|m| format!("sharpe_{m}m")).collect();
        r.add(factor(Spec {
            key: format!("avg_sharpe_{}", shape_suffix(shape)),
            label: format!("AVERAGE SHARPE RETURN {} MONTHS", shape_label(shape)),
            family: FactorFamily::SharpeReturn,
            sql_expr: blend(&components),
            unit: FactorUnit::Ratio,
            higher_is_better: true,
            null_policy: NullPolicy::NullIfAnyComponentNull,
            weight_family: WeightFamily::Momentum,
            validation_status: ValidationStatus::Legacy,
            definition: format!(
                "Mean of the {}-month sharpe returns, NULL if any is NULL (docs/05 §4).",
                shape_label(shape)
            ),
            components: &components,
        }));
    }

    // --- RSI: 5 singles + 11 blends = 16 ------------------------------------------
    for months in WINDOW_MONTHS {
        let column = format!("rsi_{months}m");
        r.add(stored(Spec {
            key: column.clone(),
            label: format!("RSI {}", window_label(months)),
            family: FactorFamily::Rsi,
            sql_expr: column,
            unit: FactorUnit::Index,
            higher_is_better: true,
            null_policy: NullPolicy::InsufficientHistory,
            weight_family: WeightFamily::Momentum,
            validation_status: ValidationStatus::Legacy,
            definition: format!(
                "Cutler's RSI with a period of the {months}-month window's daily changes (docs/05 §5)."
            ),
            components: none,
        }));
    }
    for shape in BLEND_SHAPES {
        let components: Vec<String> = shape.iter().map(|m| format!("rsi_{m}m")).collect();
        r.add(factor(Spec {
            key: format!("avg_rsi_{}", shape_suffix(shape)),
            label: format!("AVERAGE RSI {} MONTHS", shape_label(shape)),
            family: FactorFamily::Rsi,
            sql_expr: blend(&components),
            unit: FactorUnit::Index,
            higher_is_better: true,
            null_policy: NullPolicy::NullIfAnyComponentNull,
            weight_family: WeightFamily::Momentum,
            validation_status: ValidationStatus::Legacy,
            definition: format!(
                "Mean of the {}-month RSIs, NULL if any is NULL (docs/05 §4).",
                shape_label(shape)
            ),
            components: &components,
        }));
    }

    // --- Risk-adjusted by beta: 5 (docs/05 §7) ------------------------------------
    // "beta <= 0 -> NULL (the ratio is meaningless and would invert the ranking)."
    let beta_guard = |expr: &str| format!("CASE WHEN beta_12m > 0 THEN {expr} / beta_12m END");
    let ret_beta = ["ret_12m".to_owned(), "beta_12m".to_owned()];
    r.add(factor(Spec {
        key: "abs_div_beta_12m".to_owned(),
        label: "ABSOLUTE RETURN 1 YEAR / BETA".to_owned(),
        family: FactorFamily::RiskAdjusted,
        sql_expr: beta_guard("ret_12m"),
        unit: FactorUnit::Ratio,
        higher_is_better: true,
        null_policy: NullPolicy::NullOnUndefinedRatio,
        weight_family: WeightFamily::Momentum,
        validation_status: ValidationStatus::Legacy,
        definition: "The 1-year return divided by beta, NULL when beta <= 0 (docs/05 §7)."
            .to_owned(),
        components: &ret_beta,
    }));
    let sharpe_beta = ["sharpe_12m".to_owned(), "beta_12m".to_owned()];
    r.add(factor(Spec {
        key: "sharpe_div_beta_12m".to_owned(),
        label: "SHARPE RETURN 1 YEAR / BETA".to_owned(),
        family: FactorFamily::RiskAdjusted,
        sql_expr: beta_guard("sharpe_12m"),
        unit: FactorUnit::Ratio,
        higher_is_better: true,
        null_policy: NullPolicy::NullOnUndefinedRatio,
        weight_family: WeightFamily::Momentum,
        validation_status: ValidationStatus::Legacy,
        definition: "The 1-year sharpe return divided by beta, NULL when beta <= 0 (docs/05 §7)."
            .to_owned(),
        components: &sharpe_beta,
    }));
    for shape in BETA_BLEND_SHAPES {
        let components: Vec<String> = shape.iter().map(|m| format!("sharpe_{m}m")).collect();
        let mut with_beta = components.clone();
        with_beta.push("beta_12m".to_owned());
        r.add(factor(Spec {
            key: format!("avg_sharpe_div_beta_{}", shape_suffix(shape)),
            label: format!("AVERAGE SHARPE RETURN {} MONTHS / BETA", shape_label(shape)),
            family: FactorFamily::RiskAdjusted,
            sql_expr: beta_guard(&blend(&components)),
            unit: FactorUnit::Ratio,
            higher_is_better: true,
            null_policy: NullPolicy::NullOnUndefinedRatio,
            weight_family: WeightFamily::Momentum,
            validation_status: ValidationStatus::Legacy,
            definition: format!(
                "Mean of the {}-month sharpe returns divided by beta, NULL when beta <= 0 (docs/05 §7).",
                shape_label(shape)
            ),
            components: &with_beta,
        }));
    }

    // --- Skip-month momentum: 2 (docs/05 §8, INFERRED) ----------------------------
    for (key, label, definition) in [
        (
            "ret_12m_minus_1m",
            "RETURN 12 MINUS 1 MONTHS",
            "Twelve-month return excluding the most recent month (docs/05 §8).",
        ),
        (
            "ret_12m_minus_2m",
            "RETURN 12 MINUS TWO MONTHS",
            "Twelve-month return excluding the most recent two months (docs/05 §8).",
        ),
    ] {
        r.add(stored(Spec {
            key: key.to_owned(),
            label: label.to_owned(),
            family: FactorFamily::SkipMonth,
            sql_expr: key.to_owned(),
            unit: FactorUnit::Percent,
            higher_is_better: true,
            null_policy: NullPolicy::InsufficientHistory,
            weight_family: WeightFamily::Momentum,
            validation_status: ValidationStatus::Legacy,
            definition: definition.to_owned(),
            components: none,
        }));
    }

    // --- Non-momentum sort keys: docs/01 §3 names eight ---------------------------
    let non_momentum: [(&str, &str, FactorUnit, bool, NullPolicy, WeightFamily, &str); 8] = [
        (
            "vol_12m",
            "VOLATILITY 1 YEAR",
            FactorUnit::Fraction,
            false,
            NullPolicy::InsufficientHistory,
            WeightFamily::RiskExecution,
            "Annualised population standard deviation of the 1-year window's daily returns, as a fraction (docs/05 §2).",
        ),
        (
            "beta_12m",
            "BETA",
            FactorUnit::Ratio,
            false,
            NullPolicy::InsufficientHistory,
            WeightFamily::RiskExecution,
            "Covariance of 252 daily returns with NIFTY 50 over its variance (docs/05 §6).",
        ),
        (
            "pe",
            "PRICE TO EARNINGS",
            FactorUnit::Ratio,
            false,
            NullPolicy::NullIfUnpublished,
            WeightFamily::RiskExecution,
            "Price to earnings as NSE publishes it (docs/05 §14).",
        ),
        (
            "marketcap_cr",
            "MARKETCAP",
            FactorUnit::Crore,
            true,
            NullPolicy::NullIfUnpublished,
            WeightFamily::RiskExecution,
            "Market capitalisation in ₹ crore (docs/05 §14).",
        ),
        (
            "close",
            "CLOSE",
            FactorUnit::Price,
            true,
            NullPolicy::InsufficientHistory,
            WeightFamily::RiskExecution,
            "The corporate-action-adjusted closing price.",
        ),
        (
            "close_raw",
            "CLOSE RAW",
            FactorUnit::Price,
            true,
            NullPolicy::InsufficientHistory,
            WeightFamily::RiskExecution,
            "The exchange's own closing print, unadjusted.",
        ),
        (
            "away_high_ath",
            "AWAY FROM HIGH ALL TIME",
            FactorUnit::Percent,
            true,
            NullPolicy::InsufficientHistory,
            WeightFamily::TrendStructure,
            "Percent distance of the close below the all-time intraday high (docs/05 §10).",
        ),
        (
            "away_high_1y",
            "AWAY FROM HIGH 1 YEAR",
            FactorUnit::Percent,
            true,
            NullPolicy::InsufficientHistory,
            WeightFamily::TrendStructure,
            "Percent distance of the close below the 252-bar intraday high (docs/05 §10).",
        ),
    ];
    for (key, label, unit, higher, policy, weight_family, definition) in non_momentum {
        r.add(Factor {
            preference: if higher {
                FactorPreference::Higher
            } else {
                FactorPreference::Lower
            },
            ..stored(Spec {
                key: key.to_owned(),
                label: label.to_owned(),
                family: FactorFamily::NonMomentum,
                sql_expr: key.to_owned(),
                unit,
                higher_is_better: higher,
                null_policy: policy,
                weight_family,
                validation_status: ValidationStatus::Legacy,
                definition: definition.to_owned(),
                components: none,
            })
        });
    }

    // --- Path / trend / participation (ranking engine Phase 1, 13 Sep 2026) -------
    for months in WINDOW_MONTHS {
        let column = format!("pos_days_{months}m");
        r.add(stored(Spec {
            key: column.clone(),
            label: format!("POSITIVE DAYS {}", window_label(months)),
            family: FactorFamily::PathQuality,
            sql_expr: column,
            unit: FactorUnit::Percent,
            higher_is_better: true,
            null_policy: NullPolicy::InsufficientHistory,
            weight_family: WeightFamily::PathQuality,
            validation_status: ValidationStatus::Legacy,
            definition: format!(
                "Percent of the {months}-month window's daily returns that were positive (docs/05 §11)."
            ),
            components: none,
        }));
    }
    for months in WINDOW_MONTHS {
        if months == MONTHS_IN_A_YEAR {
            continue; // vol_12m already registered under non_momentum
        }
        let column = format!("vol_{months}m");
        r.add(Factor {
            preference: FactorPreference::Lower,
            ..stored(Spec {
                key: column.clone(),
                label: format!("VOLATILITY {}", window_label(months)),
                family: FactorFamily::NonMomentum,
                sql_expr: column,
                unit: FactorUnit::Fraction,
                higher_is_better: false,
                null_policy: NullPolicy::InsufficientHistory,
                weight_family: WeightFamily::RiskExecution,
                validation_status: ValidationStatus::Legacy,
                definition: format!(
                    "Annualised population standard deviation of the {months}-month window's daily returns, as a fraction (docs/05 §2)."
                ),
                components: none,
            })
        });
    }
    let vol_avg = ["vol_avg_1w".to_owned(), "vol_avg_12m".to_owned()];
    r.add(factor(Spec {
        key: "vol_expansion_1w_12m".to_owned(),
        label: "VOLUME EXPANSION 1 WEEK / 1 YEAR".to_owned(),
        family: FactorFamily::Participation,
        sql_expr: "CASE WHEN vol_avg_12m > 0 THEN CAST(vol_avg_1w AS numeric) / CAST(vol_avg_12m AS numeric) END".to_owned(),
        unit: FactorUnit::Ratio,
        higher_is_better: true,
        null_policy: NullPolicy::NullOnUndefinedRatio,
        weight_family: WeightFamily::Participation,
        validation_status: ValidationStatus::Legacy,
        definition: "Mean traded value of the last week over that of the last 252 sessions."
            .to_owned(),
        components: &vol_avg,
    }));
    for window in [20, 50, 100, 200] {
        let components = ["close".to_owned(), format!("ma_{window}")];
        r.add(Factor {
            preference: FactorPreference::TargetRange,
            ..factor(Spec {
                key: format!("ma_dist_{window}"),
                label: format!("DISTANCE FROM MA {window}"),
                family: FactorFamily::TrendStructure,
                sql_expr: format!(
                    "CASE WHEN ma_{window} > 0 THEN (close / ma_{window} - 1) * 100 END"
                ),
                unit: FactorUnit::Percent,
                // Not "higher is better": overextension is a risk. Preference is target_range.
                higher_is_better: false,
                null_policy: NullPolicy::NullOnUndefinedRatio,
                weight_family: WeightFamily::TrendStructure,
                validation_status: ValidationStatus::Legacy,
                definition: format!(
                    "Percent distance of the close above its {window}-session average."
                ),
                components: &components,
            })
        });
    }
    let stack = [
        "close".to_owned(),
        "ma_20".to_owned(),
        "ma_50".to_owned(),
        "ma_100".to_owned(),
        "ma_200".to_owned(),
    ];
    r.add(factor(Spec {
        key: "ma_stack_score".to_owned(),
        label: "MA STACK SCORE".to_owned(),
        family: FactorFamily::TrendStructure,
        sql_expr: concat!(
            "CASE WHEN close IS NOT NULL AND ma_20 IS NOT NULL AND ma_50 IS NOT NULL ",
            "AND ma_100 IS NOT NULL AND ma_200 IS NOT NULL THEN ",
            "(CASE WHEN close > ma_20 THEN 4 ELSE 0 END)",
            " + (CASE WHEN close > ma_50 THEN 4 ELSE 0 END)",
            " + (CASE WHEN close > ma_100 THEN 4 ELSE 0 END)",
            " + (CASE WHEN close > ma_200 THEN 4 ELSE 0 END)",
            " + (CASE WHEN close > ma_20 AND ma_20 > ma_50 AND ma_50 > ma_100 ",
            "AND ma_100 > ma_200 THEN 5 ELSE 0 END) END"
        )
        .to_owned(),
        unit: FactorUnit::Ratio,
        higher_is_better: true,
        null_policy: NullPolicy::InsufficientHistory,
        weight_family: WeightFamily::TrendStructure,
        validation_status: ValidationStatus::Legacy,
        definition: "0-21 points: 4 for each moving average the close is above, 5 more for a full 20 > 50 > 100 > 200 stack.".to_owned(),
        components: &stack,
    }));

    // --- Desk SCORE (ranking engine Phase 1.2) — computed, not SQL ----------------
    r.add(Factor {
        is_computed: true,
        ..factor(Spec {
            key: "desk_score".to_owned(),
            label: "DESK MOMENTUM QUALITY SCORE".to_owned(),
            family: FactorFamily::NonMomentum,
            sql_expr: COMPUTED_SQL_SENTINEL.to_owned(),
            unit: FactorUnit::Ratio,
            higher_is_better: true,
            null_policy: NullPolicy::InsufficientHistory,
            weight_family: WeightFamily::Momentum,
            validation_status: ValidationStatus::Legacy,
            definition: "The momentum book's own 0-100 SCORE for the name, from desk_score_daily (docs/ranking/PLAN.md C2).".to_owned(),
            components: none,
        })
    });

    add_phase_two_factors(&mut r);
    r
}

/// docs/ranking/VALIDATION.md §5 — the C8 promotion rule's outcome per Phase-2 factor. Every
/// other Phase-2 key stays `research`.
pub fn c8_validation_status(key: &str) -> ValidationStatus {
    const REJECTED: [&str; 17] = [
        "atr_ext_20",
        "ma50_slope_20",
        "eff_ratio_63",
        "max_dd_6m",
        "max_dd_12m",
        "downside_vol_6m",
        "downside_vol_12m",
        "sortino_6m",
        "sortino_12m",
        "underwater_12m",
        "ret_ex_top3_12m",
        "accel_21_105",
        "accel_21_105_vs",
        "vol_exp_21_126",
        "vol_persist_20",
        "resid_ret_12m",
        "regime_priority",
    ];
    if key == "rank_persist_20" {
        ValidationStatus::Validated
    } else if REJECTED.contains(&key) {
        ValidationStatus::Rejected
    } else {
        ValidationStatus::Research
    }
}

type PhaseTwoRow = (
    &'static str,
    &'static str,
    FactorFamily,
    FactorUnit,
    FactorPreference,
    WeightFamily,
    bool,
    NullPolicy,
    &'static str,
);

/// docs/ranking/PLAN.md C1 — every stored ranking factor, as `(key, label, display family, unit,
/// preference, weight family, rankable, null policy, definition)`.
const PHASE_TWO_STORED: [PhaseTwoRow; 25] = [
    (
        "atr_14",
        "ATR 14",
        FactorFamily::NonMomentum,
        FactorUnit::Price,
        FactorPreference::Eligibility,
        WeightFamily::RiskExecution,
        false,
        NullPolicy::InsufficientHistory,
        "Wilder's 14-session average true range of the adjusted bars, seeded by a simple mean.",
    ),
    (
        "atr_ext_20",
        "ATR EXTENSION FROM MA 20",
        FactorFamily::TrendStructure,
        FactorUnit::Ratio,
        FactorPreference::TargetRange,
        WeightFamily::TrendStructure,
        true,
        NullPolicy::NullOnUndefinedRatio,
        "How many 14-session ATRs the close sits above its 20-session moving average.",
    ),
    (
        "ma50_slope_20",
        "MA 50 SLOPE 20 SESSIONS",
        FactorFamily::TrendStructure,
        FactorUnit::Percent,
        FactorPreference::Higher,
        WeightFamily::TrendStructure,
        true,
        NullPolicy::InsufficientHistory,
        "Percent change of the 50-session moving average over the last 20 sessions.",
    ),
    (
        "eff_ratio_63",
        "EFFICIENCY RATIO 63 SESSIONS",
        FactorFamily::PathQuality,
        FactorUnit::Ratio,
        FactorPreference::Higher,
        WeightFamily::PathQuality,
        true,
        NullPolicy::NullOnUndefinedRatio,
        "Net 63-session price move over the sum of its 63 absolute daily moves (1 = straight).",
    ),
    (
        "max_dd_6m",
        "MAX DRAWDOWN 6 MONTHS",
        FactorFamily::PathQuality,
        FactorUnit::Percent,
        FactorPreference::Lower,
        WeightFamily::PathQuality,
        true,
        NullPolicy::InsufficientHistory,
        "Deepest percent fall of the close from its running high inside the 6-month window.",
    ),
    (
        "max_dd_12m",
        "MAX DRAWDOWN 1 YEAR",
        FactorFamily::PathQuality,
        FactorUnit::Percent,
        FactorPreference::Lower,
        WeightFamily::PathQuality,
        true,
        NullPolicy::InsufficientHistory,
        "Deepest percent fall of the close from its running high inside the 1-year window.",
    ),
    (
        "downside_vol_6m",
        "DOWNSIDE VOLATILITY 6 MONTHS",
        FactorFamily::NonMomentum,
        FactorUnit::Fraction,
        FactorPreference::Lower,
        WeightFamily::RiskExecution,
        true,
        NullPolicy::InsufficientHistory,
        "Annualised root mean square of the 6-month window's negative daily returns, over N.",
    ),
    (
        "downside_vol_12m",
        "DOWNSIDE VOLATILITY 1 YEAR",
        FactorFamily::NonMomentum,
        FactorUnit::Fraction,
        FactorPreference::Lower,
        WeightFamily::RiskExecution,
        true,
        NullPolicy::InsufficientHistory,
        "Annualised root mean square of the 1-year window's negative daily returns, over N.",
    ),
    (
        "sortino_6m",
        "SORTINO RETURN 6 MONTHS",
        FactorFamily::NonMomentum,
        FactorUnit::Ratio,
        FactorPreference::Higher,
        WeightFamily::RiskExecution,
        true,
        NullPolicy::NullOnUndefinedRatio,
        "The 6-month return divided by 100 x its downside volatility.",
    ),
    (
        "sortino_12m",
        "SORTINO RETURN 1 YEAR",
        FactorFamily::NonMomentum,
        FactorUnit::Ratio,
        FactorPreference::Higher,
        WeightFamily::RiskExecution,
        true,
        NullPolicy::NullOnUndefinedRatio,
        "The 1-year return divided by 100 x its downside volatility.",
    ),
    (
        "underwater_12m",
        "UNDERWATER DAYS 1 YEAR",
        FactorFamily::PathQuality,
        FactorUnit::Percent,
        FactorPreference::Lower,
        WeightFamily::PathQuality,
        true,
        NullPolicy::InsufficientHistory,
        "Percent of the 1-year window's sessions that closed below the window's running high.",
    ),
    (
        "ret_ex_top3_12m",
        "RETURN 1 YEAR EXCLUDING TOP 3 DAYS",
        FactorFamily::PathQuality,
        FactorUnit::Percent,
        FactorPreference::Higher,
        WeightFamily::PathQuality,
        true,
        NullPolicy::InsufficientHistory,
        "The 1-year return with its three largest daily log returns removed.",
    ),
    (
        "accel_21_105",
        "MOMENTUM ACCELERATION 21 / 105",
        FactorFamily::MomentumQuality,
        FactorUnit::Ratio,
        FactorPreference::Higher,
        WeightFamily::Momentum,
        true,
        NullPolicy::InsufficientHistory,
        "Mean daily log return of the last 21 sessions minus that of the 105 sessions before.",
    ),
    (
        "accel_21_105_vs",
        "MOMENTUM ACCELERATION 21 / 105 VOL-SCALED",
        FactorFamily::MomentumQuality,
        FactorUnit::Ratio,
        FactorPreference::Higher,
        WeightFamily::Momentum,
        true,
        NullPolicy::NullOnUndefinedRatio,
        "Momentum acceleration 21/105 divided by the sample std of the same 126 log returns.",
    ),
    (
        "vol_exp_21_126",
        "VOLUME EXPANSION 21 / 126",
        FactorFamily::Participation,
        FactorUnit::Ratio,
        FactorPreference::Higher,
        WeightFamily::Participation,
        true,
        NullPolicy::NullOnUndefinedRatio,
        "Mean traded value of the last 21 sessions over that of the 126 sessions before them.",
    ),
    (
        "vol_persist_20",
        "VOLUME PERSISTENCE 20 SESSIONS",
        FactorFamily::Participation,
        FactorUnit::Count,
        FactorPreference::Higher,
        WeightFamily::Participation,
        true,
        NullPolicy::InsufficientHistory,
        "How many of the last 20 sessions traded above the mean of the 126 sessions before.",
    ),
    (
        "excess_ret_3m",
        "EXCESS RETURN 3 MONTHS VS NIFTY 500",
        FactorFamily::MomentumQuality,
        FactorUnit::Percent,
        FactorPreference::Eligibility,
        WeightFamily::Momentum,
        false,
        NullPolicy::NullIfAnyComponentNull,
        "The 3-month return minus NIFTY 500's over the same window; a filter, not a rank key.",
    ),
    (
        "excess_ret_6m",
        "EXCESS RETURN 6 MONTHS VS NIFTY 500",
        FactorFamily::MomentumQuality,
        FactorUnit::Percent,
        FactorPreference::Eligibility,
        WeightFamily::Momentum,
        false,
        NullPolicy::NullIfAnyComponentNull,
        "The 6-month return minus NIFTY 500's over the same window; a filter, not a rank key.",
    ),
    (
        "excess_ret_12m",
        "EXCESS RETURN 1 YEAR VS NIFTY 500",
        FactorFamily::MomentumQuality,
        FactorUnit::Percent,
        FactorPreference::Eligibility,
        WeightFamily::Momentum,
        false,
        NullPolicy::NullIfAnyComponentNull,
        "The 1-year return minus NIFTY 500's over the same window; a filter, not a rank key.",
    ),
    (
        "resid_ret_12m",
        "RESIDUAL RETURN 1 YEAR",
        FactorFamily::MomentumQuality,
        FactorUnit::Percent,
        FactorPreference::Higher,
        WeightFamily::Momentum,
        true,
        NullPolicy::NullIfAnyComponentNull,
        "The 1-year return minus beta times NIFTY 50's 1-year return.",
    ),
    (
        "rs_persist_126",
        "RELATIVE STRENGTH PERSISTENCE 126 SESSIONS",
        FactorFamily::MomentumQuality,
        FactorUnit::Percent,
        FactorPreference::Higher,
        WeightFamily::Momentum,
        true,
        NullPolicy::InsufficientHistory,
        "Percent of the last 126 sessions whose 20-session return beat NIFTY 500's.",
    ),
    (
        "mom_pctile",
        "MOMENTUM PERCENTILE",
        FactorFamily::MomentumQuality,
        FactorUnit::Percent,
        FactorPreference::Eligibility,
        WeightFamily::Momentum,
        false,
        NullPolicy::InsufficientHistory,
        "Percentile (0-100) of AVERAGE SHARPE RETURN 12 6 3 1 among the day's universe rows.",
    ),
    (
        "rank_persist_20",
        "RANK PERSISTENCE 20 DAYS",
        FactorFamily::MomentumQuality,
        FactorUnit::Percent,
        FactorPreference::Higher,
        WeightFamily::Momentum,
        true,
        NullPolicy::InsufficientHistory,
        "Percent of the last 20 dates on which the momentum percentile was at least 80.",
    ),
    (
        "nse_mr6",
        "NSE MOMENTUM RATIO 6 MONTHS",
        FactorFamily::MomentumQuality,
        FactorUnit::Ratio,
        FactorPreference::Eligibility,
        WeightFamily::Momentum,
        false,
        NullPolicy::NullOnUndefinedRatio,
        "NSE's 6-month momentum ratio: month-end price return over annualised 1-year sigma.",
    ),
    (
        "nse_mr12",
        "NSE MOMENTUM RATIO 12 MONTHS",
        FactorFamily::MomentumQuality,
        FactorUnit::Ratio,
        FactorPreference::Eligibility,
        WeightFamily::Momentum,
        false,
        NullPolicy::NullOnUndefinedRatio,
        "NSE's 12-month momentum ratio: month-end price return over annualised 1-year sigma.",
    ),
];

/// docs/ranking/PLAN.md C1 — the stored ranking factors, `regime_priority` and the NSE score.
fn add_phase_two_factors(r: &mut Registry) {
    let none: &[String] = &[];
    for (key, label, family, unit, preference, weight, rankable, policy, meaning) in
        PHASE_TWO_STORED
    {
        r.add(Factor {
            preference,
            rankable,
            ..stored(Spec {
                key: key.to_owned(),
                label: label.to_owned(),
                family,
                sql_expr: key.to_owned(),
                unit,
                // As ma_dist_* already does: a target range is not "higher is better".
                higher_is_better: !matches!(
                    preference,
                    FactorPreference::Lower | FactorPreference::TargetRange
                ),
                null_policy: policy,
                weight_family: weight,
                validation_status: c8_validation_status(key),
                definition: meaning.to_owned(),
                components: none,
            })
        });
    }
    let regime = ["regime".to_owned()];
    r.add(factor(Spec {
        key: "regime_priority".to_owned(),
        label: "REGIME PRIORITY".to_owned(),
        family: FactorFamily::TrendStructure,
        sql_expr: REGIME_PRIORITY_SQL.to_owned(),
        unit: FactorUnit::Index,
        higher_is_better: true,
        null_policy: NullPolicy::InsufficientHistory,
        weight_family: WeightFamily::TrendStructure,
        validation_status: c8_validation_status("regime_priority"),
        definition: "The Wasserstein regime as an explicit order: BULL 2, NEUTRAL 1, BEAR 0 — an order, never a distance.".to_owned(),
        components: &regime,
    }));
    let ratios = ["nse_mr6".to_owned(), "nse_mr12".to_owned()];
    r.add(Factor {
        is_computed: true,
        ..factor(Spec {
            key: "nse_momentum_score".to_owned(),
            label: "NIFTY200 MOMENTUM 30 SCORE (NSE METHODOLOGY)".to_owned(),
            family: FactorFamily::MomentumQuality,
            sql_expr: COMPUTED_SQL_SENTINEL.to_owned(),
            unit: FactorUnit::Ratio,
            higher_is_better: true,
            null_policy: NullPolicy::InsufficientHistory,
            weight_family: WeightFamily::Momentum,
            validation_status: c8_validation_status("nse_momentum_score"),
            definition: "NSE's normalised momentum score: 1 + Z (or 1/(1 - Z)) of 0.5 Z12 + 0.5 Z6 over the Nifty 200 F&O-eligible set.".to_owned(),
            components: &ratios,
        })
    });
}

fn registry() -> &'static Registry {
    static REGISTRY: OnceLock<Registry> = OnceLock::new();
    REGISTRY.get_or_init(build)
}

/// `FACTORS` — every factor, in the Python dict's insertion order.
pub fn factors() -> &'static [Factor] {
    &registry().factors
}

/// Keys the builder tried to add twice (Python raises `ValueError`); always empty.
pub fn duplicate_keys() -> &'static [String] {
    &registry().duplicates
}

/// `key in FACTORS`.
pub fn contains(key: &str) -> bool {
    registry().index.contains_key(key)
}

/// `FACTORS.get(key)` — `None` for an unknown key.
pub fn lookup(key: &str) -> Option<&'static Factor> {
    let r = registry();
    r.index.get(key).and_then(|&i| r.factors.get(i))
}

/// `NAMED_FACTOR_COUNT` — what the registry actually holds.
pub fn named_factor_count() -> usize {
    factors().len()
}

/// `SORT_FACTOR_KEYS` — the registry's keys in order (the `sort_by` enum).
pub fn sort_factor_keys() -> Vec<&'static str> {
    factors().iter().map(|f| f.key.as_str()).collect()
}

/// `BACKTEST_SIGNAL_KEYS` — every registry key is a candidate backtest signal (docs/10).
pub fn backtest_signal_keys() -> Vec<&'static str> {
    sort_factor_keys()
}

/// `_NON_FACTOR_COLUMNS` — labels and units for the picker columns that are not factors.
fn non_factor_column(key: &str) -> Option<(String, FactorUnit)> {
    let fixed = match key {
        "series" => Some(("SERIES", FactorUnit::Text)),
        "high_1y" => Some(("HIGH ONE YEAR", FactorUnit::Price)),
        "high_ath" => Some(("HIGH ALL TIME", FactorUnit::Price)),
        "ma_200" => Some(("MA 200", FactorUnit::Price)),
        "ma_100" => Some(("MA 100", FactorUnit::Price)),
        "ma_50" => Some(("MA 50", FactorUnit::Price)),
        "ma_20" => Some(("MA 20", FactorUnit::Price)),
        "median_vol_12m" => Some(("MEDIAN VOLUME ONE YEAR", FactorUnit::Rupees)),
        _ => None,
    };
    if let Some((label, unit)) = fixed {
        return Some((label.to_owned(), unit));
    }
    WINDOW_MONTHS
        .iter()
        .find(|m| key == format!("circuits_{m}m"))
        .map(|&m| (format!("CIRCUITS {}", window_label(m)), FactorUnit::Count))
}

/// Every selectable result column, in docs/01 §4's order.
pub fn columns() -> Vec<ResultColumn> {
    COLUMN_PICKER_KEYS
        .iter()
        .filter_map(|&key| {
            if let Some(f) = lookup(key) {
                return Some(ResultColumn {
                    key: key.to_owned(),
                    label: f.label.clone(),
                    unit: f.unit,
                    is_factor: true,
                });
            }
            non_factor_column(key).map(|(label, unit)| ResultColumn {
                key: key.to_owned(),
                label,
                unit,
                is_factor: false,
            })
        })
        .collect()
}

/// Look a factor up — the whitelist docs/06 requires before any SQL is built.
pub fn get(key: &str) -> Result<&'static Factor, RegistryError> {
    lookup(key).ok_or_else(|| {
        RegistryError::UnknownFactor(format!(
            "unknown factor {}; it is not in the registry, so no SQL will be built for it",
            py_repr_str(key)
        ))
    })
}

/// The factor's whitelisted SQL; a computed factor has none.
pub fn sql_for(key: &str) -> Result<&'static str, RegistryError> {
    let factor = get(key)?;
    if factor.is_computed {
        return Err(RegistryError::Computed(format!(
            "{} is a computed ranking factor and has no SQL expression; it is ranked in-process (desk_score from desk_score_daily, nse_momentum_score by the ranking engine), never by SQL ROW_NUMBER",
            py_repr_str(key)
        )));
    }
    Ok(&factor.sql_expr)
}

/// docs/08 §"Sort By": the factors grouped by family, every family present, in the enum's order.
pub fn by_family() -> Vec<(FactorFamily, Vec<&'static Factor>)> {
    FactorFamily::ALL
        .iter()
        .map(|&family| {
            (
                family,
                factors().iter().filter(|f| f.family == family).collect(),
            )
        })
        .collect()
}

/// Factor count per family, keyed by the family's value, in the enum's order.
pub fn family_counts() -> Vec<(&'static str, usize)> {
    by_family()
        .into_iter()
        .map(|(family, items)| (family.value(), items.len()))
        .collect()
}
