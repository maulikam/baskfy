//! `app/analytics/settings.py`: runtime-editable settings — database overrides that beat the
//! environment without a restart (resolution order: `settings` table → environment → code
//! default), every change audited, the safety switches and risk ceilings locked to `.env`.
//!
//! **Where Python's `setattr(config, key, value)` lands here.** Keys the foundation's
//! [`Settings`] carries (the regime knobs, `FULLY_INVESTED`, `INCLUDE_UNTOUCHABLE_IN_NAV`) are
//! written through [`Settings::set`] on the shared settings, so every lane sees them; the
//! code-only constants (`TARGET_POSITIONS`, `MAX_SINGLE_WEIGHT`, the stop band, …) have no
//! field there, so a reader resolves them with [`config_value`] over [`stored_overrides`] —
//! what `C.<NAME>` is in Python after `apply_to_config` (DECISIONS-RS R8.7).

use std::collections::BTreeMap;

use crate::pyfmt::{PyArg, format_value};
use crate::settings::{ConfigValue, ENV_VARS, Settings, py_float};
use crate::storage::{Conn, Param, StorageError};
use crate::wire::{PyDict, PyValue, dict};

/// `KITE_API_KEY`, `KITE_API_SECRET`: never stored, never shown.
pub const SECRET_KEYS: [&str; 2] = ["KITE_API_KEY", "KITE_API_SECRET"];

/// Env-only: the safety switches, the risk ceilings and the sleeves' system flags.
pub const LOCKED_KEYS: &[&str] = &[
    "DRY_RUN",
    "INTRADAY_ENABLED",
    "OPTIONS_ENABLED",
    "PORT",
    "DB_PATH",
    "TOKEN_FILE",
    "RISK_MAX_DAILY_LOSS_PCT",
    "RISK_POSITION_HEADROOM",
    "RISK_GROSS_MULTIPLE",
    "RISK_MAX_ORDERS_PER_DAY",
    "RISK_MAX_POSITION_VALUE",
    "RISK_MAX_GROSS_EXPOSURE",
    "BASKFY_SWING_EXECUTION_ENABLED",
    "BASKFY_SWING_MONITOR_ENABLED",
    "BASKFY_SWING_EP_PREMARKET_ENABLED",
    "BASKFY_SWING_RISK_PER_TRADE_PCT_MAX",
    "BASKFY_SWING_MAX_POSITION_PCT_MAX",
    "BASKFY_SWING_MAX_OPEN_POSITIONS_MAX",
    "BASKFY_OPTIONS_O1M_EXECUTION_ENABLED",
    "BASKFY_OPTIONS_O1W_EXECUTION_ENABLED",
    "BASKFY_OPTIONS_O2_EXECUTION_ENABLED",
    "BASKFY_OPTIONS_O3_EXECUTION_ENABLED",
    "BASKFY_OPTIONS_MONITOR_ENABLED",
    "BASKFY_OPTIONS_COLLECT_ENABLED",
    "BASKFY_OPTIONS_SCAN_ENABLED",
    "BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX",
    "BASKFY_OPTIONS_RISK_PCT_MAX",
    "BASKFY_OPTIONS_MAX_LOTS_MAX",
    "BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX",
    "BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX",
    "BASKFY_OPTIONS_HARD_EXIT_LATEST",
];

/// `SettingsError(ValueError)`: the message the page shows.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct SettingsError(pub String);

/// What can go wrong around a settings call.
#[derive(Debug, thiserror::Error)]
pub enum Failure {
    /// A refusal the page reports (`/settings?error=...`).
    #[error("{0}")]
    Refused(#[from] SettingsError),
    /// An exception Python does not catch (a 500).
    #[error("{0}")]
    Internal(String),
    /// The database.
    #[error("{0}")]
    Storage(#[from] StorageError),
}

/// A number as written in the spec table (`minimum=1` prints `1`, `minimum=1.0` prints `1.0`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Num {
    /// An `int` literal.
    I(i64),
    /// A `float` literal.
    F(f64),
}

impl Num {
    #[allow(clippy::cast_precision_loss)]
    fn as_f64(self) -> f64 {
        match self {
            Self::I(i) => i as f64,
            Self::F(f) => f,
        }
    }

    fn py(self) -> PyValue {
        match self {
            Self::I(i) => PyValue::Int(i),
            Self::F(f) => PyValue::Float(f),
        }
    }

    fn text(self) -> String {
        match self {
            Self::I(i) => i.to_string(),
            Self::F(f) => baskfy_core::py::float_repr(f),
        }
    }
}

/// A typed setting value, as `Spec.parse` returns it.
#[derive(Debug, Clone, PartialEq)]
pub enum SVal {
    /// `int`.
    Int(i64),
    /// `float`.
    Float(f64),
    /// `bool`.
    Bool(bool),
    /// `str` (a choice).
    Str(String),
    /// `tuple[int, int]`.
    Pair(i64, i64),
}

impl SVal {
    /// `str(value)`.
    #[must_use]
    pub fn py_str(&self) -> String {
        match self {
            Self::Int(i) => i.to_string(),
            Self::Float(f) => baskfy_core::py::float_repr(*f),
            Self::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
            Self::Str(s) => s.clone(),
            Self::Pair(a, b) => format!("({a}, {b})"),
        }
    }

    /// As a Python value (a pair becomes a two-item list: templates only test its truth).
    #[must_use]
    pub fn py(&self) -> PyValue {
        match self {
            Self::Int(i) => PyValue::Int(*i),
            Self::Float(f) => PyValue::Float(*f),
            Self::Bool(b) => PyValue::Bool(*b),
            Self::Str(s) => PyValue::Str(s.clone()),
            Self::Pair(a, b) => PyValue::List(vec![PyValue::Int(*a), PyValue::Int(*b)]),
        }
    }

    /// `float(value)` (a choice's text parsed as Python would).
    #[allow(clippy::cast_precision_loss)]
    fn as_f64(&self) -> Option<f64> {
        match self {
            Self::Int(i) => Some(*i as f64),
            Self::Float(f) => Some(*f),
            Self::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
            Self::Str(s) => py_float(s),
            Self::Pair(..) => None,
        }
    }

    /// `int(value)`.
    #[allow(clippy::cast_possible_truncation)]
    fn as_i64(&self) -> Option<i64> {
        match self {
            Self::Int(i) => Some(*i),
            Self::Float(f) if f.is_finite() => Some(f.trunc() as i64),
            Self::Bool(b) => Some(i64::from(*b)),
            Self::Str(s) => crate::settings::py_int(s),
            _ => None,
        }
    }
}

/// One editable setting.
#[derive(Debug, Clone, PartialEq)]
pub struct Spec {
    /// The config attribute.
    pub key: &'static str,
    /// The label.
    pub label: &'static str,
    /// The page section.
    pub group: &'static str,
    /// `int | float | bool | choice | intpair`.
    pub kind: &'static str,
    /// The help line.
    pub help: &'static str,
    /// The lowest legal value.
    pub minimum: Option<Num>,
    /// The highest legal value.
    pub maximum: Option<Num>,
    /// A choice's options.
    pub choices: &'static [&'static str],
    /// The unit label.
    pub unit: &'static str,
}

fn invalid(label: &str, s: &str, kind: &str) -> SettingsError {
    SettingsError(format!(
        "{label}: {} is not a valid {kind}",
        baskfy_providers::py::repr_str(s)
    ))
}

/// Python's `int(float(text))`.
#[allow(clippy::cast_possible_truncation)]
fn int_of_float(text: &str) -> Result<i64, Option<String>> {
    match py_float(text) {
        None => Err(None),
        Some(f) if f.is_nan() => Err(None),
        // `int(float("inf"))` raises OverflowError, which `Spec.parse` does not catch.
        Some(f) if f.is_infinite() => Err(Some(
            "OverflowError: cannot convert float infinity to integer".to_owned(),
        )),
        Some(f) => Ok(f.trunc() as i64),
    }
}

impl Spec {
    /// `Spec.parse`: text from a form → a typed value, or the error naming the problem.
    ///
    /// # Errors
    /// [`Failure::Refused`] for what Python's `SettingsError` reports; [`Failure::Internal`]
    /// where Python raises something it does not catch.
    pub fn parse(&self, raw: &str) -> Result<SVal, Failure> {
        let s = raw.trim();
        let v = match self.kind {
            "int" => match int_of_float(s) {
                Ok(i) => SVal::Int(i),
                Err(None) => return Err(invalid(self.label, s, self.kind).into()),
                Err(Some(e)) => return Err(Failure::Internal(e)),
            },
            "float" => match py_float(s) {
                Some(f) => SVal::Float(f),
                None => return Err(invalid(self.label, s, self.kind).into()),
            },
            "bool" => SVal::Bool(matches!(
                s.to_lowercase().as_str(),
                "1" | "true" | "on" | "yes"
            )),
            "choice" => {
                if !self.choices.contains(&s) {
                    return Err(SettingsError(format!(
                        "{}: {} is not one of {}",
                        self.label,
                        baskfy_providers::py::repr_str(s),
                        self.choices.join(", ")
                    ))
                    .into());
                }
                SVal::Str(s.to_owned())
            }
            "intpair" => {
                let joined = s.replace(' ', "");
                let mut parts = Vec::new();
                for p in joined.split(',').filter(|p| !p.is_empty()) {
                    match int_of_float(p) {
                        Ok(i) => parts.push(i),
                        Err(None) => return Err(invalid(self.label, s, self.kind).into()),
                        Err(Some(e)) => return Err(Failure::Internal(e)),
                    }
                }
                let [lo, hi] = parts[..] else {
                    return Err(SettingsError(format!(
                        "{}: expected two numbers, got {}",
                        self.label,
                        baskfy_providers::py::repr_str(s)
                    ))
                    .into());
                };
                if lo > hi {
                    return Err(SettingsError(format!(
                        "{}: minimum {lo} exceeds maximum {hi}",
                        self.label
                    ))
                    .into());
                }
                SVal::Pair(lo, hi)
            }
            _ => SVal::Str(s.to_owned()),
        };
        if matches!(self.kind, "int" | "float" | "intpair") {
            #[allow(clippy::cast_precision_loss)]
            let (check, hi) = match &v {
                SVal::Pair(a, b) => (*a as f64, *b as f64),
                other => {
                    let x = other.as_f64().unwrap_or(f64::NAN);
                    (x, x)
                }
            };
            if let Some(min) = self.minimum
                && check < min.as_f64()
            {
                return Err(SettingsError(format!(
                    "{}: {} is below the minimum {}",
                    self.label,
                    v.py_str(),
                    min.text()
                ))
                .into());
            }
            if let Some(max) = self.maximum
                && hi > max.as_f64()
            {
                return Err(SettingsError(format!(
                    "{}: {} is above the maximum {}",
                    self.label,
                    v.py_str(),
                    max.text()
                ))
                .into());
            }
        }
        Ok(v)
    }

    /// `Spec.to_text`.
    #[must_use]
    pub fn to_text(&self, value: &SVal) -> String {
        match (self.kind, value) {
            ("intpair", SVal::Pair(a, b)) => format!("{a},{b}"),
            ("bool", v) => if truthy(v) { "true" } else { "false" }.to_owned(),
            (_, v) => v.py_str(),
        }
    }

    /// The spec as the template reads it (`e.spec.kind`, `e.spec.minimum`, …).
    #[must_use]
    pub fn py(&self) -> PyValue {
        let opt = |n: Option<Num>| n.map_or(PyValue::None, Num::py);
        PyValue::Map(dict([
            ("key", PyValue::Str(self.key.to_owned())),
            ("label", PyValue::Str(self.label.to_owned())),
            ("group", PyValue::Str(self.group.to_owned())),
            ("kind", PyValue::Str(self.kind.to_owned())),
            ("help", PyValue::Str(self.help.to_owned())),
            ("minimum", opt(self.minimum)),
            ("maximum", opt(self.maximum)),
            (
                "choices",
                PyValue::List(
                    self.choices
                        .iter()
                        .map(|c| PyValue::Str((*c).to_owned()))
                        .collect(),
                ),
            ),
            ("unit", PyValue::Str(self.unit.to_owned())),
        ]))
    }
}

fn truthy(v: &SVal) -> bool {
    match v {
        SVal::Int(i) => *i != 0,
        SVal::Float(f) => *f != 0.0,
        SVal::Bool(b) => *b,
        SVal::Str(s) => !s.is_empty(),
        SVal::Pair(..) => true,
    }
}

const fn spec(
    key: &'static str,
    label: &'static str,
    group: &'static str,
    kind: &'static str,
    help: &'static str,
) -> Spec {
    Spec {
        key,
        label,
        group,
        kind,
        help,
        minimum: None,
        maximum: None,
        choices: &[],
        unit: "",
    }
}

const fn ranged(s: Spec, minimum: Num, maximum: Num, unit: &'static str) -> Spec {
    Spec {
        minimum: Some(minimum),
        maximum: Some(maximum),
        unit,
        ..s
    }
}

const fn choice(s: Spec, choices: &'static [&'static str]) -> Spec {
    Spec { choices, ..s }
}

use Num::{F, I};

const PS: &str = "Position sizing";
const RD: &str = "Rebalance discipline";
const ST: &str = "Stops";
const RO: &str = "Regime overlay";

/// `SPECS`: what can be edited, in page order.
pub const SPECS: &[Spec] = &[
    ranged(
        spec(
            "TARGET_POSITIONS",
            "Target positions",
            PS,
            "intpair",
            "Soft range, resolved by score dispersion. Half-size provisional entries count toward the ceiling.",
        ),
        I(1),
        I(60),
        "",
    ),
    ranged(
        spec(
            "MAX_SINGLE_WEIGHT",
            "Max single weight",
            PS,
            "float",
            "Percent of the ACTIVE EQUITY SLEEVE, not of NAV. In R3 the sleeve is 40% of NAV, so 15% here is 6% of NAV.",
        ),
        F(1.0),
        F(100.0),
        "%",
    ),
    ranged(
        spec(
            "MIN_POSITION_WEIGHT",
            "Min position weight",
            PS,
            "float",
            "Also sleeve-relative. Half-size entries may sit below it deliberately.",
        ),
        F(0.1),
        F(100.0),
        "%",
    ),
    ranged(
        spec(
            "CLUSTER_CAP",
            "Cluster cap",
            PS,
            "float",
            "Max weight per sector cluster, applied on the sleeve.",
        ),
        F(1.0),
        F(100.0),
        "%",
    ),
    ranged(
        spec(
            "MAX_POS_VS_DAY_VALUE",
            "Liquidity cap",
            PS,
            "float",
            "Position must stay under this fraction of median daily traded value.",
        ),
        F(0.0001),
        F(1.0),
        "",
    ),
    spec(
        "FULLY_INVESTED",
        "Ignore cash bands",
        PS,
        "bool",
        "True holds no strategic cash regardless of breadth.",
    ),
    ranged(
        spec(
            "RETENTION_BUFFER",
            "Retention buffer",
            RD,
            "int",
            "Keep holdings ranked within N + buffer of the cutoff.",
        ),
        I(0),
        I(50),
        "",
    ),
    ranged(
        spec(
            "REPLACEMENT_EDGE",
            "Replacement edge",
            RD,
            "float",
            "A challenger must beat the incumbent's score by this to force a swap.",
        ),
        F(0.0),
        F(50.0),
        "pts",
    ),
    ranged(
        spec(
            "RUNNER_MAX_VALUE",
            "Runner cap",
            RD,
            "float",
            "Rupee ceiling for a filter-rejected name kept as a runner.",
        ),
        F(0.0),
        F(100_000_000.0),
        "Rs",
    ),
    ranged(
        spec(
            "PARABOLIC_RSI",
            "Parabolic RSI",
            RD,
            "float",
            "Trim winners above this RSI to the runner weight.",
        ),
        F(50.0),
        F(100.0),
        "",
    ),
    ranged(
        spec(
            "TRIM_TO_WEIGHT",
            "Trim-to weight",
            RD,
            "float",
            "Weight retained as a trailed runner after a parabolic trim.",
        ),
        F(0.0),
        F(100.0),
        "%",
    ),
    ranged(
        spec(
            "STOP_MIN",
            "Stop floor",
            ST,
            "float",
            "Minimum GTT stop distance below the reference price.",
        ),
        F(0.01),
        F(0.5),
        "",
    ),
    ranged(
        spec(
            "STOP_MAX",
            "Stop ceiling",
            ST,
            "float",
            "Maximum stop distance.",
        ),
        F(0.01),
        F(0.9),
        "",
    ),
    ranged(
        spec(
            "STOP_VOL_MULT",
            "Stop volatility multiple",
            ST,
            "float",
            "Weekly volatility multiple before clamping to the floor and ceiling.",
        ),
        F(0.1),
        F(10.0),
        "",
    ),
    spec(
        "REGIME_ENABLED",
        "Overlay enabled",
        RO,
        "bool",
        "Master switch. Off means the overlay displays a view and changes nothing.",
    ),
    choice(
        spec(
            "REGIME_MODE",
            "Rollout mode",
            RO,
            "choice",
            "observe displays only; propose builds a plan for review; enforce lets an approved plan reach the gateway. DRY_RUN still overrides all three.",
        ),
        &["observe", "propose", "enforce"],
    ),
    ranged(
        spec(
            "REGIME_BUFFER_BPS",
            "Signal buffer",
            RO,
            "int",
            "Schmitt-trigger band around each moving average. 150 = 1.50%.",
        ),
        I(0),
        I(1000),
        "bps",
    ),
    ranged(
        spec(
            "REGIME_CONFIRM_DAYS",
            "Confirmation days",
            RO,
            "int",
            "Consecutive closes needed to confirm a side. Each is compared against that day's own moving average.",
        ),
        I(1),
        I(20),
        "",
    ),
    choice(
        spec(
            "REGIME_WEEKLY_EVAL_WEEKDAY",
            "Evaluation day",
            RO,
            "choice",
            "Weekly evaluation day. A holiday falls back to the last completed session.",
        ),
        &["0", "1", "2", "3", "4", "5", "6"],
    ),
    ranged(
        spec(
            "REGIME_INDEX_STALE_DAYS",
            "Index stale after",
            RO,
            "int",
            "Older index candles block new buys but never force selling.",
        ),
        I(1),
        I(90),
        "days",
    ),
    ranged(
        spec(
            "REGIME_BREADTH_STALE_DAYS",
            "Breadth stale after",
            RO,
            "int",
            "",
        ),
        I(1),
        I(180),
        "days",
    ),
    ranged(
        spec(
            "REGIME_MIN_BREADTH_COVERAGE_PCT",
            "Min breadth coverage",
            RO,
            "float",
            "Below this, breadth can neither force selling nor permit Risk-On.",
        ),
        F(0.0),
        F(100.0),
        "%",
    ),
    ranged(
        spec(
            "REGIME_R4_EQUITY_PCT",
            "R4 equity cap",
            RO,
            "float",
            "Set 0 for genuine full cash rather than calling a 10% residual a full exit.",
        ),
        F(0.0),
        F(100.0),
        "%",
    ),
    ranged(
        spec(
            "REGIME_R4_MAX_RESIDUAL_NAMES",
            "R4 residual names",
            RO,
            "int",
            "0 forces R4 to full cash rather than inventing names to retain.",
        ),
        I(0),
        I(20),
        "",
    ),
    ranged(
        spec(
            "REGIME_HALF_SIZE_FACTOR",
            "Half-size factor",
            RO,
            "float",
            "Fraction of full size for provisional R2 entries.",
        ),
        F(0.05),
        F(1.0),
        "",
    ),
    ranged(
        spec(
            "REGIME_NEARBY_RANK_BAND",
            "Nearby rank band",
            RO,
            "int",
            "Within this band, unrealised losers are sold before winners.",
        ),
        I(1),
        I(30),
        "",
    ),
    choice(
        spec(
            "REGIME_BOOTSTRAP_POLICY",
            "Bootstrap policy",
            RO,
            "choice",
            "observe_only never forces sells merely because no prior state exists.",
        ),
        &["observe_only", "adopt_candidate"],
    ),
    ranged(
        spec(
            "REGIME_LTCG_REVIEW_DAYS",
            "LTCG review window",
            RO,
            "int",
            "Flag a profitable lot this many days short of long-term treatment.",
        ),
        I(0),
        I(365),
        "days",
    ),
    spec(
        "INCLUDE_UNTOUCHABLE_IN_NAV",
        "Count untouchables in NAV",
        "Analytics",
        "bool",
        "Off keeps SGB/G-sec out of NAV so their P&L is not attributed to momentum.",
    ),
];

/// `BY_KEY[key]`.
#[must_use]
pub fn by_key(key: &str) -> Option<&'static Spec> {
    SPECS.iter().find(|s| s.key == key)
}

/// `GROUPS`: the sections, in first-seen order.
#[must_use]
pub fn groups() -> Vec<&'static str> {
    let mut out: Vec<&'static str> = Vec::new();
    for s in SPECS {
        if !out.contains(&s.group) {
            out.push(s.group);
        }
    }
    out
}

fn from_config(v: Option<ConfigValue>) -> Option<SVal> {
    Some(match v? {
        ConfigValue::Bool(b) => SVal::Bool(b),
        ConfigValue::Int(i) => SVal::Int(i),
        ConfigValue::Float(f) => SVal::Float(f),
        ConfigValue::Str(s) => SVal::Str(s),
        ConfigValue::Tuple(t) => SVal::Str(t.join(",")),
    })
}

/// `getattr(config, key)` before any override: the module's code default (or its env value,
/// for the keys `app.config` reads from the environment). `None` for an unknown key.
#[must_use]
pub fn code_default(settings: &Settings, key: &str) -> Option<SVal> {
    Some(match key {
        "TARGET_POSITIONS" => SVal::Pair(12, 15),
        "MAX_SINGLE_WEIGHT" => SVal::Float(15.0),
        "MIN_POSITION_WEIGHT" => SVal::Float(6.0),
        "CLUSTER_CAP" => SVal::Float(25.0),
        "MAX_POS_VS_DAY_VALUE" => SVal::Float(0.01),
        "RETENTION_BUFFER" => SVal::Int(5),
        "REPLACEMENT_EDGE" => SVal::Float(8.0),
        "RUNNER_MAX_VALUE" => SVal::Int(300_000),
        "PARABOLIC_RSI" => SVal::Float(82.0),
        "TRIM_TO_WEIGHT" => SVal::Float(2.0),
        "STOP_MIN" => SVal::Float(0.08),
        "STOP_MAX" => SVal::Float(0.12),
        "STOP_VOL_MULT" => SVal::Float(2.2),
        other => return from_config(settings.values().remove(other)),
    })
}

/// `stored(conn)`: the raw override rows.
///
/// # Errors
/// The database.
pub async fn stored(conn: &mut Conn) -> Result<Vec<(String, String)>, StorageError> {
    let cur = conn.execute("SELECT key, value FROM settings", &[]).await?;
    Ok(cur
        .rows
        .iter()
        .map(|r| {
            let k = r.at(0).and_then(|v| v.as_str()).unwrap_or_default();
            let v = r.at(1).and_then(|v| v.as_str()).unwrap_or_default();
            (k.to_owned(), v.to_owned())
        })
        .collect())
}

/// The overrides `apply_to_config` would set: every stored row whose key is still exposed,
/// parsed.
///
/// # Errors
/// The database, or a stored value that no longer parses (Python raises there too).
pub async fn stored_overrides(conn: &mut Conn) -> Result<BTreeMap<String, SVal>, Failure> {
    let mut out = BTreeMap::new();
    for (key, text) in stored(conn).await? {
        if let Some(spec) = by_key(&key) {
            out.insert(key, spec.parse(&text)?);
        }
    }
    Ok(out)
}

/// `config.<KEY>` as Python sees it at run time: an applied override, else the code default.
#[must_use]
pub fn config_value(
    overrides: &BTreeMap<String, SVal>,
    settings: &Settings,
    key: &str,
) -> Option<SVal> {
    overrides
        .get(key)
        .cloned()
        .or_else(|| code_default(settings, key))
}

/// The environment the settings page reads (`os.getenv`).
pub type EnvLookup<'a> = &'a (dyn Fn(&str) -> Option<String> + Sync);

/// The process environment.
#[must_use]
pub fn process_env(key: &str) -> Option<String> {
    std::env::var(key).ok()
}

/// One row of `effective()`.
#[derive(Debug, Clone, PartialEq)]
pub struct Effective {
    /// The spec.
    pub spec: &'static Spec,
    /// The value in force.
    pub value: SVal,
    /// `spec.to_text(value)`.
    pub text: String,
    /// `database | environment | code default`.
    pub source: &'static str,
}

/// `effective(conn)`: every editable setting with its value and where it came from.
///
/// # Errors
/// The database; a stored or environment value that does not parse.
pub async fn effective(
    conn: &mut Conn,
    settings: &Settings,
    env: EnvLookup<'_>,
) -> Result<Vec<Effective>, Failure> {
    let db_values = stored(conn).await?;
    let mut out = Vec::with_capacity(SPECS.len());
    for spec in SPECS {
        let from_db = db_values
            .iter()
            .rev()
            .find(|(k, _)| k == spec.key)
            .map(|(_, v)| v.as_str())
            .filter(|v| !v.is_empty());
        let (value, source) = if let Some(text) = from_db {
            (spec.parse(text)?, "database")
        } else if let Some(text) = env(spec.key) {
            (spec.parse(&text)?, "environment")
        } else {
            let v = code_default(settings, spec.key)
                .ok_or_else(|| Failure::Internal(format!("no default for {}", spec.key)))?;
            (v, "code default")
        };
        out.push(Effective {
            spec,
            text: spec.to_text(&value),
            value,
            source,
        });
    }
    Ok(out)
}

fn to_config_value(key: &str, value: &SVal) -> Option<ConfigValue> {
    Some(match value {
        SVal::Bool(b) => ConfigValue::Bool(*b),
        SVal::Int(i) => ConfigValue::Int(*i),
        SVal::Float(f) => ConfigValue::Float(*f),
        // `REGIME_WEEKLY_EVAL_WEEKDAY` is a choice ("4") but an int field: every Python
        // reader calls int() on it.
        SVal::Str(s) if key == "REGIME_WEEKLY_EVAL_WEEKDAY" => {
            ConfigValue::Int(crate::settings::py_int(s)?)
        }
        SVal::Str(s) => ConfigValue::Str(s.clone()),
        SVal::Pair(..) => return None,
    })
}

/// `apply_to_config(conn)`: push every stored override onto the shared settings (the keys the
/// foundation's settings carry; the rest resolve through [`config_value`]).
///
/// # Errors
/// As [`stored_overrides`].
pub async fn apply_to_config(
    conn: &mut Conn,
    settings: &crate::settings::SharedSettings,
) -> Result<BTreeMap<String, SVal>, Failure> {
    let applied = stored_overrides(conn).await?;
    let mut guard = match settings.write() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };
    for (key, value) in &applied {
        if let Some(cv) = to_config_value(key, value) {
            // A key the settings struct does not carry is resolved by config_value instead.
            let _ = guard.set(key, cv);
        }
    }
    Ok(applied)
}

/// `importlib.reload(config)` for the keys this page can change: each goes back to what the
/// environment (or the code) says.
fn reload_keys(settings: &crate::settings::SharedSettings, env: EnvLookup<'_>) {
    let mut guard = match settings.write() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };
    for spec in SPECS {
        let Some(var) = ENV_VARS.iter().find(|v| v.attrs.contains(&spec.key)) else {
            continue;
        };
        if let Ok(values) = var.parse(env(var.key).as_deref()) {
            for (attr, value) in values {
                let _ = guard.set(attr, value);
            }
        }
    }
}

#[allow(clippy::cast_precision_loss)]
fn feasible_position_range(min_w: f64, max_w: f64) -> (i64, i64) {
    #[allow(clippy::cast_possible_truncation)]
    let lo = (100.0 / max_w - 1e-9).ceil() as i64;
    #[allow(clippy::cast_possible_truncation)]
    let hi = (100.0 / min_w + 1e-9).floor() as i64;
    (lo.max(1), hi.max(1))
}

fn list_repr(xs: &[f64]) -> String {
    let parts: Vec<String> = xs.iter().map(|x| baskfy_core::py::float_repr(*x)).collect();
    format!("[{}]", parts.join(", "))
}

/// `RegimeConfig(...).validate()` over the knobs this page edits (every other field is its
/// fixed default and passes): the error lines after the first, as `validate()` reports them.
fn regime_problems(
    buffer_bps: i64,
    confirm_days: i64,
    weekday: i64,
    min_breadth_coverage_pct: f64,
    r4: f64,
) -> Vec<String> {
    let mut errs = Vec::new();
    let caps = [100.0, 70.0, 40.0, r4];
    if caps.windows(2).any(|w| w[0] < w[1]) {
        errs.push(format!(
            "tier exposure caps must be non-increasing R1->R4: {}",
            list_repr(&caps)
        ));
    }
    if caps.iter().any(|c| !(0.0 <= *c && *c <= 100.0)) {
        errs.push(format!(
            "tier exposure caps must be within 0-100: {}",
            list_repr(&caps)
        ));
    }
    if !(0.0..=100.0).contains(&min_breadth_coverage_pct) {
        errs.push(format!(
            "min_breadth_coverage_pct must be a percentage within 0-100, got {}",
            baskfy_core::py::float_repr(min_breadth_coverage_pct)
        ));
    }
    if confirm_days < 1 {
        errs.push(format!(
            "confirm_days must be at least 1, got {confirm_days}"
        ));
    }
    if !(0..=1000).contains(&buffer_bps) {
        errs.push(format!(
            "buffer_bps must be within 0-1000 (0-10%), got {buffer_bps}"
        ));
    }
    if !(0..=6).contains(&weekday) {
        errs.push("weekly_evaluation_weekday must be 0-6 (Mon-Sun)".to_owned());
    }
    errs.into_iter().map(|e| format!("  - {e}")).collect()
}

fn fmt(value: PyArg, spec: &str) -> String {
    format_value(&value, spec).unwrap_or_default()
}

/// `validate(candidate)`: cross-field checks — a value can be individually legal and jointly
/// impossible.
///
/// # Errors
/// [`Failure::Internal`] where Python would raise past the check (a value that is not a
/// number where one is needed).
pub fn validate(
    candidate: &BTreeMap<String, SVal>,
    settings: &Settings,
) -> Result<Vec<String>, Failure> {
    let g = |k: &str| -> Result<SVal, Failure> {
        candidate
            .get(k)
            .cloned()
            .or_else(|| code_default(settings, k))
            .ok_or_else(|| Failure::Internal(format!("AttributeError: {k}")))
    };
    let num = |k: &str| -> Result<f64, Failure> {
        g(k)?
            .as_f64()
            .ok_or_else(|| Failure::Internal(format!("TypeError: {k}")))
    };
    let int = |k: &str| -> Result<i64, Failure> {
        g(k)?
            .as_i64()
            .ok_or_else(|| Failure::Internal(format!("ValueError: {k}")))
    };
    let mut problems = Vec::new();
    let (lo_v, hi_v) = (g("MIN_POSITION_WEIGHT")?, g("MAX_SINGLE_WEIGHT")?);
    let (lo_w, hi_w) = (num("MIN_POSITION_WEIGHT")?, num("MAX_SINGLE_WEIGHT")?);
    if lo_w > hi_w {
        problems.push(format!(
            "Min position weight ({}%) exceeds max single weight ({}%)",
            lo_v.py_str(),
            hi_v.py_str()
        ));
    } else {
        if lo_w <= 0.0 || hi_w <= 0.0 {
            return Err(Failure::Internal(
                "AllocationError: sleeve weights must be positive".to_owned(),
            ));
        }
        let (lo_n, hi_n) = feasible_position_range(lo_w, hi_w);
        let SVal::Pair(want_lo, want_hi) = g("TARGET_POSITIONS")? else {
            return Err(Failure::Internal(
                "TARGET_POSITIONS is not a pair".to_owned(),
            ));
        };
        if want_hi > hi_n {
            #[allow(clippy::cast_precision_loss)]
            let need = want_hi as f64 * lo_w;
            problems.push(format!(
                "{want_hi} positions at a {}% floor need {}% of the sleeve; only 100% exists. Feasible range is {lo_n}-{hi_n}.",
                lo_v.py_str(),
                fmt(PyArg::Float(need), ".0f")
            ));
        }
        if want_lo < lo_n {
            problems.push(format!(
                "{want_lo} positions cannot absorb the sleeve without breaching the {}% cap; at least {lo_n} are needed.",
                hi_v.py_str()
            ));
        }
    }
    if num("STOP_MIN")? > num("STOP_MAX")? {
        problems.push(format!(
            "Stop floor ({}) exceeds stop ceiling ({})",
            g("STOP_MIN")?.py_str(),
            g("STOP_MAX")?.py_str()
        ));
    }
    let r4 = num("REGIME_R4_EQUITY_PCT")?;
    if r4 > 40.0 {
        problems.push("R4 equity cap must stay at or below the R3 cap of 40%".to_owned());
    }
    if r4 > 0.0 && int("REGIME_R4_MAX_RESIDUAL_NAMES")? == 0 {
        problems.push(
            "R4 permits equity but names no residual positions. Set the residual name count above zero, or set the R4 cap to 0 for genuine full cash."
                .to_owned(),
        );
    }
    problems.extend(regime_problems(
        int("REGIME_BUFFER_BPS")?,
        int("REGIME_CONFIRM_DAYS")?,
        int("REGIME_WEEKLY_EVAL_WEEKDAY")?,
        num("REGIME_MIN_BREADTH_COVERAGE_PCT")?,
        r4,
    ));
    Ok(problems)
}

/// What `save` returns.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Saved {
    /// How many values were typed.
    pub saved: usize,
    /// The keys whose value in force changed, sorted.
    pub changed: Vec<String>,
    /// When.
    pub at: String,
}

/// `save(conn, updates, note=...)`: validate, persist, audit and apply. Nothing is written
/// unless everything passes.
///
/// # Errors
/// [`Failure::Refused`] with Python's message; storage.
pub async fn save(
    conn: &mut Conn,
    shared: &crate::settings::SharedSettings,
    env: EnvLookup<'_>,
    updates: &[(String, String)],
    note: &str,
    now: &str,
) -> Result<Saved, Failure> {
    let mut typed: Vec<(String, SVal)> = Vec::new();
    let mut errors: Vec<String> = Vec::new();
    for (key, raw) in updates {
        if SECRET_KEYS.contains(&key.as_str()) || LOCKED_KEYS.contains(&key.as_str()) {
            errors.push(format!("{key} is not editable from the interface"));
            continue;
        }
        let Some(spec) = by_key(key) else {
            errors.push(format!("{key} is not a known setting"));
            continue;
        };
        match spec.parse(raw) {
            Ok(v) => {
                if let Some(slot) = typed.iter_mut().find(|(k, _)| k == key) {
                    slot.1 = v;
                } else {
                    typed.push((key.clone(), v));
                }
            }
            Err(Failure::Refused(e)) => errors.push(e.0),
            Err(other) => return Err(other),
        }
    }
    if !errors.is_empty() {
        return Err(SettingsError(errors.join("; ")).into());
    }
    let settings = snapshot(shared);
    let eff = effective(conn, &settings, env).await?;
    let current: BTreeMap<String, SVal> = eff
        .iter()
        .map(|e| (e.spec.key.to_owned(), e.value.clone()))
        .collect();
    let mut candidate = current.clone();
    for (k, v) in &typed {
        candidate.insert(k.clone(), v.clone());
    }
    let problems = validate(&candidate, &settings)?;
    if !problems.is_empty() {
        return Err(SettingsError(problems.join("; ")).into());
    }
    let existing: Vec<String> = stored(conn).await?.into_iter().map(|(k, _)| k).collect();
    let in_force = |key: &str| -> String {
        match (by_key(key), current.get(key)) {
            (Some(spec), Some(v)) => spec.to_text(v),
            _ => String::new(),
        }
    };
    let mut writes: Vec<(String, String, String, bool)> = Vec::new();
    let mut changed: Vec<String> = Vec::new();
    for (key, value) in &typed {
        let Some(spec) = by_key(key) else { continue };
        let text = spec.to_text(value);
        let old = in_force(key);
        let is_changed = text != old;
        if is_changed {
            changed.push(key.clone());
        }
        if text == old && !existing.contains(key) {
            continue;
        }
        writes.push((key.clone(), text, old, is_changed));
    }
    let note_param = if note.is_empty() {
        Param::Null
    } else {
        Param::Text(note.to_owned())
    };
    let now_owned = now.to_owned();
    conn.transaction(move |c| {
        Box::pin(async move {
            for (key, text, old, is_changed) in writes {
                c.execute(
                    "INSERT INTO settings(key, value, updated_at, note) VALUES(?,?,?,?) \
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value, \
                     updated_at=excluded.updated_at, note=excluded.note",
                    &[
                        Param::Text(key.clone()),
                        Param::Text(text.clone()),
                        Param::Text(now_owned.clone()),
                        note_param.clone(),
                    ],
                )
                .await?;
                if is_changed {
                    c.execute(
                        "INSERT INTO settings_audit(key, old_value, new_value, changed_at, note) \
                         VALUES(?,?,?,?,?)",
                        &[
                            Param::Text(key),
                            Param::Text(old),
                            Param::Text(text),
                            Param::Text(now_owned.clone()),
                            note_param.clone(),
                        ],
                    )
                    .await?;
                }
            }
            Ok(())
        })
    })
    .await?;
    apply_to_config(conn, shared).await?;
    changed.sort();
    Ok(Saved {
        saved: typed.len(),
        changed,
        at: now.to_owned(),
    })
}

/// `reset(conn, keys)`: drop overrides so the environment or code default takes over again.
/// Returns the keys reset (a key named twice is reported twice, as Python's comprehension does).
///
/// # Errors
/// Storage; a remaining override that no longer parses.
pub async fn reset(
    conn: &mut Conn,
    shared: &crate::settings::SharedSettings,
    env: EnvLookup<'_>,
    keys: Option<Vec<String>>,
    now: &str,
) -> Result<Vec<String>, Failure> {
    let existing = stored(conn).await?;
    let targets: Vec<String> =
        keys.unwrap_or_else(|| existing.iter().map(|(k, _)| k.clone()).collect());
    let lookup = |k: &str| {
        existing
            .iter()
            .rev()
            .find(|(kk, _)| kk == k)
            .map(|(_, v)| v.clone())
    };
    let mut writes: Vec<(String, String)> = Vec::new();
    for key in &targets {
        if let Some(old) = lookup(key) {
            writes.push((key.clone(), old));
        }
    }
    let now_owned = now.to_owned();
    conn.transaction(move |c| {
        Box::pin(async move {
            for (key, old) in writes {
                c.execute(
                    "INSERT INTO settings_audit(key, old_value, new_value, changed_at, note) \
                     VALUES(?,?,?,?,?)",
                    &[
                        Param::Text(key.clone()),
                        Param::Text(old),
                        Param::Text("<reset>".to_owned()),
                        Param::Text(now_owned.clone()),
                        Param::Text("reset".to_owned()),
                    ],
                )
                .await?;
                c.execute("DELETE FROM settings WHERE key=?", &[Param::Text(key)])
                    .await?;
            }
            Ok(())
        })
    })
    .await?;
    reload_keys(shared, env);
    apply_to_config(conn, shared).await?;
    Ok(targets
        .into_iter()
        .filter(|k| lookup(k).is_some())
        .collect())
}

/// `history(conn, limit)`.
///
/// # Errors
/// Storage.
pub async fn history(conn: &mut Conn, limit: i64) -> Result<PyValue, StorageError> {
    let cur = conn
        .execute(
            "SELECT key, old_value, new_value, changed_at, note FROM settings_audit \
             ORDER BY id DESC LIMIT ?",
            &[Param::Int(limit)],
        )
        .await?;
    Ok(super::web::rows_py(&cur.rows))
}

/// `locked_view()`: the env-only settings, shown read-only.
#[must_use]
pub fn locked_view(settings: &Settings) -> Vec<(&'static str, PyValue, &'static str)> {
    vec![
        (
            "DRY_RUN",
            PyValue::Bool(settings.dry_run),
            "Global no-order guarantee. Kept in .env so enabling live trading is a deliberate act on the machine, not a click in a browser.",
        ),
        (
            "INTRADAY_ENABLED",
            PyValue::Bool(settings.intraday_enabled),
            "Product gate. Same reasoning.",
        ),
        (
            "OPTIONS_ENABLED",
            PyValue::Bool(settings.options_enabled),
            "Product gate. Same reasoning.",
        ),
        ("PORT", PyValue::Int(settings.port), "Read once at startup."),
        (
            "DB_PATH",
            PyValue::Str(settings.db_path.clone()),
            "Changing it mid-run would split the book across two databases.",
        ),
        (
            "TOKEN_FILE",
            PyValue::Str(settings.token_file.clone()),
            "Holds the Kite access token.",
        ),
    ]
}

/// `inr_short(v)`: the lakh/crore gloss.
#[must_use]
pub fn inr_short(v: f64) -> String {
    let a = v.abs();
    if a >= 1e7 {
        format!("{} cr", fmt(PyArg::Float(v / 1e7), ",.2f"))
    } else if a >= 1e5 {
        format!("{} L", fmt(PyArg::Float(v / 1e5), ",.2f"))
    } else {
        fmt(PyArg::Float(v), ",.0f")
    }
}

/// `config.risk_config(nav)` — the limits the gateway enforces.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RiskLimits {
    /// `max_daily_loss`.
    pub max_daily_loss: f64,
    /// `max_orders_per_day`.
    pub max_orders_per_day: i64,
    /// `max_position_value`.
    pub max_position_value: f64,
    /// `max_gross_exposure`.
    pub max_gross_exposure: f64,
}

/// Python's `round(x, 2)` (correctly rounded, ties to even on the exact binary value).
#[must_use]
pub fn round2(x: f64) -> f64 {
    format!("{x:.2}").parse().unwrap_or(x)
}

/// `config.risk_config(nav)`.
///
/// # Errors
/// A `RISK_MAX_*` override that `float()` refuses (Python raises `ValueError`).
pub fn risk_config(
    settings: &Settings,
    max_single_weight: f64,
    nav: f64,
) -> Result<RiskLimits, Failure> {
    let base = if nav == 0.0 { 0.0 } else { nav };
    let parse = |raw: &str| {
        py_float(raw).ok_or_else(|| {
            Failure::Internal(format!(
                "ValueError: could not convert string to float: {}",
                baskfy_providers::py::repr_str(raw)
            ))
        })
    };
    let pos_cap = if !settings.risk_max_position_value.is_empty() {
        parse(&settings.risk_max_position_value)?
    } else if base > 0.0 {
        base * max_single_weight / 100.0 * settings.risk_position_headroom
    } else {
        1_500_000.0
    };
    let gross_cap = if !settings.risk_max_gross_exposure.is_empty() {
        parse(&settings.risk_max_gross_exposure)?
    } else if base > 0.0 {
        base * settings.risk_gross_multiple
    } else {
        12_000_000.0
    };
    let daily_loss = if base > 0.0 {
        base * settings.risk_max_daily_loss_pct / 100.0
    } else {
        100_000.0
    };
    Ok(RiskLimits {
        max_daily_loss: round2(daily_loss),
        max_orders_per_day: settings.risk_max_orders_per_day,
        max_position_value: round2(pos_cap),
        max_gross_exposure: round2(gross_cap),
    })
}

/// `config.risk_coherence(cfg, nav)`.
#[must_use]
pub fn risk_coherence(
    cfg: &RiskLimits,
    nav: f64,
    max_single_weight: &SVal,
    target_lo: i64,
) -> Vec<String> {
    let mut problems = Vec::new();
    if nav > 0.0 {
        let msw = max_single_weight.as_f64().unwrap_or(f64::NAN);
        let legal_max = nav * msw / 100.0;
        if cfg.max_position_value < legal_max {
            problems.push(format!(
                "max_position_value Rs {} is below the largest position the strategy permits ({}% of NAV = Rs {}) — it would block a legal position",
                fmt(PyArg::Float(cfg.max_position_value), ",.0f"),
                max_single_weight.py_str(),
                fmt(PyArg::Float(legal_max), ",.0f")
            ));
        }
        if cfg.max_gross_exposure < nav {
            problems.push(format!(
                "max_gross_exposure Rs {} is below current NAV Rs {} — every order would be refused",
                fmt(PyArg::Float(cfg.max_gross_exposure), ",.0f"),
                fmt(PyArg::Float(nav), ",.0f")
            ));
        }
        if cfg.max_orders_per_day < target_lo * 2 {
            problems.push(format!(
                "max_orders_per_day {} cannot cover a {target_lo}-position rebalance",
                cfg.max_orders_per_day
            ));
        }
    }
    problems
}

/// `risk_preview(nav)`: what the risk limits mean, in rupees, at the current NAV.
///
/// # Errors
/// As [`risk_config`].
pub fn risk_preview(
    settings: &Settings,
    overrides: &BTreeMap<String, SVal>,
    nav: f64,
) -> Result<PyValue, Failure> {
    let msw = config_value(overrides, settings, "MAX_SINGLE_WEIGHT").unwrap_or(SVal::Float(15.0));
    let msw_f = msw.as_f64().unwrap_or(f64::NAN);
    let (t_lo, t_hi) = match config_value(overrides, settings, "TARGET_POSITIONS") {
        Some(SVal::Pair(a, b)) => (a, b),
        _ => (12, 15),
    };
    let cfg = risk_config(settings, msw_f, nav)?;
    let derived =
        settings.risk_max_position_value.is_empty() && settings.risk_max_gross_exposure.is_empty();
    let legal_max = if nav > 0.0 { nav * msw_f / 100.0 } else { 0.0 };
    let rupees = |x: f64| format!("₹{}", fmt(PyArg::Float(x), ",.0f"));
    let row = |key: &str, value: String, short: String, means: String| {
        PyValue::Map(dict([
            ("key", PyValue::Str(key.to_owned())),
            ("value", PyValue::Str(value)),
            ("short", PyValue::Str(short)),
            ("means", PyValue::Str(means)),
        ]))
    };
    let rows = vec![
        row(
            "RISK_MAX_DAILY_LOSS_PCT",
            rupees(cfg.max_daily_loss),
            inr_short(cfg.max_daily_loss),
            "Trading halts for the rest of the day once losses reach this.".to_owned(),
        ),
        row(
            "RISK_POSITION_HEADROOM",
            rupees(cfg.max_position_value),
            inr_short(cfg.max_position_value),
            format!(
                "Largest single position the gateway will allow. The strategy's own cap is {}% of NAV = {}.",
                msw.py_str(),
                rupees(legal_max)
            ),
        ),
        row(
            "RISK_GROSS_MULTIPLE",
            rupees(cfg.max_gross_exposure),
            inr_short(cfg.max_gross_exposure),
            "Total exposure ceiling across all positions.".to_owned(),
        ),
        row(
            "RISK_MAX_ORDERS_PER_DAY",
            fmt(PyArg::Int(cfg.max_orders_per_day), ","),
            String::new(),
            format!(
                "A full rebalance of {t_hi} positions is at most {} orders plus stops.",
                t_hi * 2
            ),
        ),
    ];
    let problems = if nav > 0.0 {
        risk_coherence(&cfg, nav, &msw, t_lo)
    } else {
        Vec::new()
    };
    Ok(PyValue::Map(dict([
        ("nav", PyValue::Float(nav)),
        ("nav_short", PyValue::Str(inr_short(nav))),
        ("have_nav", PyValue::Bool(nav > 0.0)),
        ("derived", PyValue::Bool(derived)),
        ("rows", PyValue::List(rows)),
        (
            "problems",
            PyValue::List(problems.into_iter().map(PyValue::Str).collect()),
        ),
    ])))
}

/// A copy of the shared settings.
#[must_use]
pub fn snapshot(shared: &crate::settings::SharedSettings) -> Settings {
    match shared.read() {
        Ok(s) => s.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    }
}

/// `eff` as the template reads it.
#[must_use]
pub fn effective_py(eff: &[Effective]) -> PyDict {
    let mut out = PyDict::new();
    for e in eff {
        out.insert(
            e.spec.key,
            PyValue::Map(dict([
                ("spec", e.spec.py()),
                ("value", e.value.py()),
                ("text", PyValue::Str(e.text.clone())),
                ("source", PyValue::Str(e.source.to_owned())),
            ])),
        );
    }
    out
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::*;

    fn refused(r: Result<SVal, Failure>) -> String {
        match r {
            Err(Failure::Refused(e)) => e.0,
            other => panic!("expected a refusal, got {other:?}"),
        }
    }

    #[test]
    fn parse_matches_spec_messages() {
        let tp = by_key("TARGET_POSITIONS").unwrap();
        assert_eq!(tp.parse(" 10, 14 ").unwrap(), SVal::Pair(10, 14));
        assert_eq!(
            refused(tp.parse("14,10")),
            "Target positions: minimum 14 exceeds maximum 10"
        );
        assert_eq!(
            refused(tp.parse("5")),
            "Target positions: expected two numbers, got '5'"
        );
        assert_eq!(
            refused(tp.parse("0,10")),
            "Target positions: (0, 10) is below the minimum 1"
        );
        let msw = by_key("MAX_SINGLE_WEIGHT").unwrap();
        assert_eq!(
            refused(msw.parse("abc")),
            "Max single weight: 'abc' is not a valid float"
        );
        assert_eq!(
            refused(msw.parse("101")),
            "Max single weight: 101.0 is above the maximum 100.0"
        );
        let mode = by_key("REGIME_MODE").unwrap();
        assert_eq!(
            refused(mode.parse("yolo")),
            "Rollout mode: 'yolo' is not one of observe, propose, enforce"
        );
        let rb = by_key("RETENTION_BUFFER").unwrap();
        assert_eq!(rb.parse("7.9").unwrap(), SVal::Int(7));
        assert!(matches!(rb.parse("inf"), Err(Failure::Internal(_))));
        let b = by_key("FULLY_INVESTED").unwrap();
        assert_eq!(b.parse("ON").unwrap(), SVal::Bool(true));
        assert_eq!(b.parse("nope").unwrap(), SVal::Bool(false));
    }

    #[test]
    fn locked_and_secret_keys_are_not_editable() {
        for k in [
            "DRY_RUN",
            "INTRADAY_ENABLED",
            "OPTIONS_ENABLED",
            "RISK_MAX_DAILY_LOSS_PCT",
        ] {
            assert!(LOCKED_KEYS.contains(&k));
            assert!(by_key(k).is_none(), "{k} must not be a spec");
        }
        for k in SECRET_KEYS {
            assert!(by_key(k).is_none());
        }
        for flag in [
            "BASKFY_SWING_AUTO_EXECUTE",
            "BASKFY_TWT_AUTO_EXECUTE",
            "BASKFY_FNO_F3_AUTO_EXIT",
        ] {
            assert!(
                by_key(flag).is_none(),
                "{flag} is never editable from a browser"
            );
        }
    }

    #[test]
    fn validate_cross_field() {
        let s = Settings::defaults().unwrap();
        let mut c: BTreeMap<String, SVal> = SPECS
            .iter()
            .map(|sp| (sp.key.to_owned(), code_default(&s, sp.key).unwrap()))
            .collect();
        assert!(validate(&c, &s).unwrap().is_empty());
        c.insert("MIN_POSITION_WEIGHT".into(), SVal::Float(20.0));
        assert_eq!(
            validate(&c, &s).unwrap(),
            vec!["Min position weight (20.0%) exceeds max single weight (15.0%)".to_owned()]
        );
        c.insert("MIN_POSITION_WEIGHT".into(), SVal::Float(6.0));
        c.insert("TARGET_POSITIONS".into(), SVal::Pair(5, 20));
        assert_eq!(
            validate(&c, &s).unwrap(),
            vec![
                "20 positions at a 6.0% floor need 120% of the sleeve; only 100% exists. Feasible range is 7-16.".to_owned(),
                "5 positions cannot absorb the sleeve without breaching the 15.0% cap; at least 7 are needed.".to_owned()
            ]
        );
        c.insert("TARGET_POSITIONS".into(), SVal::Pair(12, 15));
        c.insert("REGIME_R4_EQUITY_PCT".into(), SVal::Float(45.0));
        assert_eq!(
            validate(&c, &s).unwrap(),
            vec![
                "R4 equity cap must stay at or below the R3 cap of 40%".to_owned(),
                "  - tier exposure caps must be non-increasing R1->R4: [100.0, 70.0, 40.0, 45.0]"
                    .to_owned()
            ]
        );
    }

    #[test]
    fn inr_short_gloss() {
        assert_eq!(inr_short(15_750_000.0), "1.57 cr");
        assert_eq!(inr_short(525_000.0), "5.25 L");
        assert_eq!(inr_short(99_999.0), "99,999");
    }
}
