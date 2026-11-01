//! Ports `app.config` and every other module-level environment read in the desk
//! (`app.analytics.db`, `app.scan_source`, `app.breadth_source`, `app.telemetry`) — DECISIONS-RS
//! R8.1.
//!
//! **Table-driven, and the table is the answer key's.** [`ENV_VARS`] lists every variable with
//! the Python parse it gets (`os.getenv(K, d).lower() == "true"`, `!= "false"`, `int(...)`,
//! `int(... or n)`, `float(...)`, `tuple(....split(","))`). `fixtures/settings_defaults.json`
//! (written by `tools/parity/desk_settings_dump.py` from the live Python) records, for each one,
//! the default and the value after a reload with each of 18 probe strings; `tests/settings.rs`
//! requires this module to agree on all of them and to know exactly the same set of variables.
//!
//! **The safety defaults are the point.** `DRY_RUN` is `true` unless the variable says exactly
//! `false` (any case); every execution, auto-execute and product switch is `false` unless it
//! says exactly `true`. The three auto-execute exceptions (non-negotiable 1) are
//! [`Settings::swing_auto_execute`], [`Settings::twt_auto_execute`] and
//! [`Settings::fno_f3_auto_exit`] — there is no fourth, and `tests/settings.rs` fails if one
//! appears.
//!
//! Function-level reads (Python reads them inside a function, so they are not module state):
//! [`FUNCTION_LEVEL`]. Note `BASKFY_SOLE_USER_ID` defaults to `"0"` in the three monitors and
//! to `"1"` in `app.config` — reproduced, not unified.
//!
//! `.env`: Python's `load_dotenv()` reads the desk root's `.env` without overriding the process
//! environment. [`Settings::from_env`] reads only the process environment; [`dotenv_lookup`]
//! layers a `.env` file under it for the binary (`--env-file`), with the same no-override rule.
//!
//! The settings page's stored overrides (`app.analytics.settings.apply_to_config`, which also
//! reads `os.getenv(spec.key)` for its own keys) belong to the analytics lane; it writes through
//! [`Settings::set`] on the shared copy (`AppState::settings`).

use std::collections::BTreeMap;
use std::sync::{Arc, RwLock};

/// A Python config value, as `app.config` holds it.
#[derive(Debug, Clone, PartialEq)]
pub enum ConfigValue {
    /// `bool`.
    Bool(bool),
    /// `int`.
    Int(i64),
    /// `float`.
    Float(f64),
    /// `str`.
    Str(String),
    /// `tuple[str, ...]`.
    Tuple(Vec<String>),
}

/// How Python turns the raw string into the attribute.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// `os.getenv(K, d)` — the string itself.
    Str,
    /// `os.getenv(K, d).lower() == "true"` — off unless exactly `true`.
    BoolEqTrue,
    /// `os.getenv(K, d).lower() != "false"` — on unless exactly `false` (`DRY_RUN`).
    BoolNeFalse,
    /// `int(os.getenv(K, d))`.
    Int,
    /// `int(os.getenv(K, d) or n)` — an empty string falls back to `n`.
    IntOr(i64),
    /// `float(os.getenv(K, d))`.
    Float,
    /// `tuple(os.getenv(K, d).split(","))`.
    CsvTuple,
    /// `_RAW = os.getenv(K, d)` then `NAME = _RAW.lower() == "true"` (two attributes).
    StrThenBoolEqTrue,
}

/// One environment variable and the module-level attribute(s) it sets.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EnvVar {
    /// The environment variable.
    pub key: &'static str,
    /// The Python module whose import reads it.
    pub module: &'static str,
    /// The attribute(s) it sets, in Python's order.
    pub attrs: &'static [&'static str],
    /// The parse.
    pub kind: Kind,
    /// The default string passed to `os.getenv`.
    pub default: &'static str,
}

/// A variable read inside a function rather than at import.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FunctionLevel {
    /// The environment variable.
    pub key: &'static str,
    /// Where Python reads it.
    pub module: &'static str,
    /// The literal default at that call.
    pub default: &'static str,
}

/// Function-level reads, each ported where its function is (the owning lane).
pub const FUNCTION_LEVEL: &[FunctionLevel] = &[
    FunctionLevel {
        key: "DESK_DATABASE_URL",
        module: "app.analytics.db",
        default: "postgresql://baskfy:baskfy@localhost:5433/baskfy",
    },
    FunctionLevel {
        key: "BASKFY_SOLE_USER_ID",
        module: "app.fno_monitor",
        default: "0",
    },
    FunctionLevel {
        key: "BASKFY_SOLE_USER_ID",
        module: "app.options_monitor",
        default: "0",
    },
    FunctionLevel {
        key: "BASKFY_SOLE_USER_ID",
        module: "app.swing_monitor",
        default: "0",
    },
    FunctionLevel {
        key: "DESK_MONITOR_METRICS_PORT",
        module: "app.swing_monitor",
        default: "",
    },
    FunctionLevel {
        key: "KITE_TOKEN_ENCRYPTION_KEY",
        module: "app.token_store",
        default: "",
    },
];

/// A value Python would refuse at import (`int("yes")` raises `ValueError`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{key}={raw:?}: {python_error} (as app.config's import would raise)")]
pub struct SettingsError {
    /// The variable.
    pub key: String,
    /// What it held.
    pub raw: String,
    /// The Python exception type (`ValueError`).
    pub python_error: String,
}

/// Python's `str.strip()` whitespace: Rust's `char::is_whitespace` plus the four ASCII
/// separators (`\x1c`-`\x1f`) Python's `str.isspace` also counts. (Python's `int()` also
/// accepts non-ASCII decimal digits; this does not — R8.1.)
fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Remove `_` digit separators the way Python's numeric literals allow them: only single
/// underscores between two digits. `None` when the placement is illegal.
fn strip_underscores(s: &str) -> Option<String> {
    if !s.contains('_') {
        return Some(s.to_owned());
    }
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    for (i, c) in chars.iter().enumerate() {
        if *c == '_' {
            let before = i.checked_sub(1).and_then(|j| chars.get(j));
            let after = chars.get(i + 1);
            if !(before.is_some_and(char::is_ascii_digit)
                && after.is_some_and(char::is_ascii_digit))
            {
                return None;
            }
        } else {
            out.push(*c);
        }
    }
    Some(out)
}

/// Python's `int(str)`: surrounding whitespace, an optional sign, decimal digits with `_`
/// separators. `None` where Python raises `ValueError`.
#[must_use]
pub fn py_int(raw: &str) -> Option<i64> {
    let s = py_strip(raw);
    let (sign, body) = match s.strip_prefix('-') {
        Some(rest) => (-1, rest),
        None => (1, s.strip_prefix('+').unwrap_or(s)),
    };
    if body.is_empty() || body.starts_with('_') {
        return None;
    }
    let digits = strip_underscores(body)?;
    if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    digits.parse::<i64>().ok().map(|v| v * sign)
}

/// Python's `float(str)`: whitespace, sign, `inf`/`infinity`/`nan` in any case, decimals and
/// exponents with `_` separators. `None` where Python raises `ValueError`.
#[must_use]
pub fn py_float(raw: &str) -> Option<f64> {
    let s = py_strip(raw);
    let (neg, body) = match s.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, s.strip_prefix('+').unwrap_or(s)),
    };
    let lower = body.to_ascii_lowercase();
    let magnitude = match lower.as_str() {
        "inf" | "infinity" => f64::INFINITY,
        "nan" => f64::NAN,
        _ => {
            if body.is_empty()
                || !body
                    .chars()
                    .all(|c| c.is_ascii_digit() || matches!(c, '.' | 'e' | 'E' | '+' | '-' | '_'))
            {
                return None;
            }
            let cleaned = strip_underscores(body)?;
            if !cleaned.chars().any(|c| c.is_ascii_digit()) {
                return None;
            }
            cleaned.parse::<f64>().ok()?
        }
    };
    Some(if neg { -magnitude } else { magnitude })
}

fn parse_error(key: &str, raw: &str) -> SettingsError {
    SettingsError {
        key: key.to_owned(),
        raw: raw.to_owned(),
        python_error: "ValueError".to_owned(),
    }
}

impl EnvVar {
    /// The attribute values Python computes from `raw` (`None` = unset, the default applies).
    ///
    /// # Errors
    /// Where Python's import would raise.
    pub fn parse(
        &self,
        raw: Option<&str>,
    ) -> Result<Vec<(&'static str, ConfigValue)>, SettingsError> {
        let text = raw.unwrap_or(self.default);
        let first = self.attrs.first().copied().unwrap_or(self.key);
        let one = |v: ConfigValue| Ok(vec![(first, v)]);
        match self.kind {
            Kind::Str => one(ConfigValue::Str(text.to_owned())),
            Kind::BoolEqTrue => one(ConfigValue::Bool(text.to_lowercase() == "true")),
            Kind::BoolNeFalse => one(ConfigValue::Bool(text.to_lowercase() != "false")),
            Kind::Int => py_int(text)
                .map(ConfigValue::Int)
                .map_or_else(|| Err(parse_error(self.key, text)), one),
            Kind::IntOr(fallback) => {
                if text.is_empty() {
                    one(ConfigValue::Int(fallback))
                } else {
                    py_int(text)
                        .map(ConfigValue::Int)
                        .map_or_else(|| Err(parse_error(self.key, text)), one)
                }
            }
            Kind::Float => py_float(text)
                .map(ConfigValue::Float)
                .map_or_else(|| Err(parse_error(self.key, text)), one),
            Kind::CsvTuple => one(ConfigValue::Tuple(
                text.split(',').map(str::to_owned).collect(),
            )),
            Kind::StrThenBoolEqTrue => {
                let second = self.attrs.get(1).copied().unwrap_or(first);
                Ok(vec![
                    (first, ConfigValue::Str(text.to_owned())),
                    (second, ConfigValue::Bool(text.to_lowercase() == "true")),
                ])
            }
        }
    }
}

fn take<'a>(
    values: &'a BTreeMap<&'static str, ConfigValue>,
    attr: &str,
) -> Result<&'a ConfigValue, SettingsError> {
    values.get(attr).ok_or_else(|| SettingsError {
        key: attr.to_owned(),
        raw: String::new(),
        python_error: "AttributeError".to_owned(),
    })
}

fn wrong_type(attr: &str) -> SettingsError {
    SettingsError {
        key: attr.to_owned(),
        raw: String::new(),
        python_error: "TypeError".to_owned(),
    }
}

fn take_bool(
    values: &BTreeMap<&'static str, ConfigValue>,
    attr: &str,
) -> Result<bool, SettingsError> {
    match take(values, attr)? {
        ConfigValue::Bool(b) => Ok(*b),
        _ => Err(wrong_type(attr)),
    }
}

fn take_int(
    values: &BTreeMap<&'static str, ConfigValue>,
    attr: &str,
) -> Result<i64, SettingsError> {
    match take(values, attr)? {
        ConfigValue::Int(i) => Ok(*i),
        _ => Err(wrong_type(attr)),
    }
}

fn take_float(
    values: &BTreeMap<&'static str, ConfigValue>,
    attr: &str,
) -> Result<f64, SettingsError> {
    match take(values, attr)? {
        ConfigValue::Float(f) => Ok(*f),
        _ => Err(wrong_type(attr)),
    }
}

fn take_str(
    values: &BTreeMap<&'static str, ConfigValue>,
    attr: &str,
) -> Result<String, SettingsError> {
    match take(values, attr)? {
        ConfigValue::Str(s) => Ok(s.clone()),
        _ => Err(wrong_type(attr)),
    }
}

fn take_tuple(
    values: &BTreeMap<&'static str, ConfigValue>,
    attr: &str,
) -> Result<Vec<String>, SettingsError> {
    match take(values, attr)? {
        ConfigValue::Tuple(t) => Ok(t.clone()),
        _ => Err(wrong_type(attr)),
    }
}

impl Settings {
    /// Build from a lookup (`std::env::var` in production, a map in tests).
    ///
    /// # Errors
    /// The first variable Python's import would refuse, in [`ENV_VARS`] order.
    pub fn from_lookup(lookup: &dyn Fn(&str) -> Option<String>) -> Result<Self, SettingsError> {
        let mut values = BTreeMap::new();
        for var in ENV_VARS {
            let raw = lookup(var.key);
            for (attr, value) in var.parse(raw.as_deref())? {
                values.insert(attr, value);
            }
        }
        Self::from_values(&values)
    }

    /// The process environment, as the Python desk sees it after `load_dotenv()`.
    ///
    /// # Errors
    /// As [`Settings::from_lookup`].
    pub fn from_env() -> Result<Self, SettingsError> {
        Self::from_lookup(&|k| std::env::var(k).ok())
    }

    /// Every default: the Python desk started with an empty environment.
    ///
    /// # Errors
    /// Never in practice (every default parses); kept fallible rather than unwrapped.
    pub fn defaults() -> Result<Self, SettingsError> {
        Self::from_lookup(&|_| None)
    }

    /// Set one attribute by its Python name (`app.analytics.settings.apply_to_config`'s
    /// `setattr(C, key, value)`).
    ///
    /// # Errors
    /// An unknown name or a value of the wrong type.
    pub fn set(&mut self, attr: &str, value: ConfigValue) -> Result<(), SettingsError> {
        let mut values = self.values();
        let Some(slot) = values.iter_mut().find(|(k, _)| **k == attr) else {
            return Err(SettingsError {
                key: attr.to_owned(),
                raw: String::new(),
                python_error: "AttributeError".to_owned(),
            });
        };
        *slot.1 = value;
        *self = Self::from_values(&values)?;
        Ok(())
    }

    /// The three named auto-execute exceptions (non-negotiable 1), by their Python names.
    /// Exactly three; an agent may not add a fourth.
    #[must_use]
    pub fn auto_execute_flags(&self) -> [(&'static str, bool); 3] {
        [
            ("SWING_AUTO_EXECUTE", self.swing_auto_execute),
            ("TWT_AUTO_EXECUTE", self.twt_auto_execute),
            ("FNO_F3_AUTO_EXIT", self.fno_f3_auto_exit),
        ]
    }
}

/// The settings every request reads, shared and mutable at run time (the settings page writes
/// through it, and the gateway reads the product gates at the moment of each order, as Python's
/// `_gates_from_config` does).
pub type SharedSettings = Arc<RwLock<Settings>>;

/// A `.env` file under the process environment, never overriding it (`load_dotenv()`'s rule).
/// Lines are `KEY=VALUE`, optionally `export KEY=VALUE`, with `#` comments and one level of
/// matching quotes stripped — the subset the desk's `.env.example` uses.
#[must_use]
pub fn dotenv_lookup(contents: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for line in contents.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line);
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let mut value = value.trim();
        if value.len() >= 2
            && ((value.starts_with('"') && value.ends_with('"'))
                || (value.starts_with('\'') && value.ends_with('\'')))
        {
            value = &value[1..value.len() - 1];
        } else if let Some((before, _)) = value.split_once(" #") {
            value = before.trim_end();
        }
        out.insert(key.trim().to_owned(), value.to_owned());
    }
    out
}

/// Every module-level environment variable, generated from the answer key (R8.1).
#[rustfmt::skip]
pub const ENV_VARS: &[EnvVar] = &[
    EnvVar { key: "BASKFY_FNO_CARRY_ENABLED", module: "app.config", attrs: &["FNO_CARRY_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_FNO_F1_EXECUTION_ENABLED", module: "app.config", attrs: &["FNO_F1_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_FNO_F2_EXECUTION_ENABLED", module: "app.config", attrs: &["FNO_F2_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_FNO_F3_AUTO_EXIT", module: "app.config", attrs: &["FNO_F3_AUTO_EXIT"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_FNO_F3_EXECUTION_ENABLED", module: "app.config", attrs: &["FNO_F3_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_FNO_MONITOR_ENABLED", module: "app.config", attrs: &["FNO_MONITOR_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX", module: "app.config", attrs: &["OPTIONS_BOOK_DAILY_LOSS_INR_MAX"], kind: Kind::Str, default: "30000" },
    EnvVar { key: "BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX", module: "app.config", attrs: &["OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX"], kind: Kind::Str, default: "75000" },
    EnvVar { key: "BASKFY_OPTIONS_COLLECT_ENABLED", module: "app.config", attrs: &["OPTIONS_COLLECT_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_OPTIONS_HARD_EXIT_LATEST", module: "app.config", attrs: &["OPTIONS_HARD_EXIT_LATEST"], kind: Kind::Str, default: "15:00" },
    EnvVar { key: "BASKFY_OPTIONS_MAX_LOTS_MAX", module: "app.config", attrs: &["OPTIONS_MAX_LOTS_MAX"], kind: Kind::IntOr(10), default: "10" },
    EnvVar { key: "BASKFY_OPTIONS_MONITOR_ENABLED", module: "app.config", attrs: &["OPTIONS_MONITOR_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_OPTIONS_O1M_EXECUTION_ENABLED", module: "app.config", attrs: &["OPTIONS_O1M_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_OPTIONS_O1W_EXECUTION_ENABLED", module: "app.config", attrs: &["OPTIONS_O1W_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_OPTIONS_O2_EXECUTION_ENABLED", module: "app.config", attrs: &["OPTIONS_O2_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_OPTIONS_O3_EXECUTION_ENABLED", module: "app.config", attrs: &["OPTIONS_O3_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_OPTIONS_RISK_PCT_MAX", module: "app.config", attrs: &["OPTIONS_RISK_PCT_MAX"], kind: Kind::Str, default: "1.0" },
    EnvVar { key: "BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX", module: "app.config", attrs: &["OPTIONS_RISK_PER_TRADE_INR_MAX"], kind: Kind::Str, default: "25000" },
    EnvVar { key: "BASKFY_OPTIONS_SCAN_ENABLED", module: "app.config", attrs: &["OPTIONS_SCAN_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_REDIS_URL", module: "app.config", attrs: &["SHARED_READ_LIMITS_REDIS_URL"], kind: Kind::Str, default: "" },
    EnvVar { key: "BASKFY_SOLE_BROKER_ACCOUNT_ID", module: "app.config", attrs: &["SOLE_BROKER_ACCOUNT_ID"], kind: Kind::Int, default: "1" },
    EnvVar { key: "BASKFY_SOLE_USER_ID", module: "app.config", attrs: &["SOLE_USER_ID"], kind: Kind::Int, default: "1" },
    EnvVar { key: "BASKFY_SWING_AUTO_EXECUTE", module: "app.config", attrs: &["SWING_AUTO_EXECUTE"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_SWING_EP_PREMARKET_ENABLED", module: "app.config", attrs: &["_EP_PREMARKET", "SWING_EP_PREMARKET_ENABLED"], kind: Kind::StrThenBoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_SWING_EXECUTION_ENABLED", module: "app.config", attrs: &["SWING_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_SWING_GTT_LIMIT_FRACTION", module: "app.config", attrs: &["SWING_GTT_LIMIT_FRACTION"], kind: Kind::Float, default: "0.97" },
    EnvVar { key: "BASKFY_SWING_INDEX_SLUG", module: "app.config", attrs: &["SWING_INDEX_SLUG"], kind: Kind::Str, default: "nifty-smallcap-250" },
    EnvVar { key: "BASKFY_SWING_MONITOR_ENABLED", module: "app.config", attrs: &["SWING_MONITOR_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_SWING_SCAN_MIN_INTERVAL_SECONDS", module: "app.config", attrs: &["SWING_SCAN_MIN_INTERVAL_SECONDS"], kind: Kind::Int, default: "60" },
    EnvVar { key: "BASKFY_SWING_SCAN_STALE_AFTER_SECONDS", module: "app.config", attrs: &["SWING_SCAN_STALE_AFTER_SECONDS"], kind: Kind::Int, default: "600" },
    EnvVar { key: "BASKFY_SWING_STOP_BAND_MAX", module: "app.config", attrs: &["SWING_STOP_BAND_MAX"], kind: Kind::Float, default: "0.10" },
    EnvVar { key: "BASKFY_SWING_STOP_BAND_MIN", module: "app.config", attrs: &["SWING_STOP_BAND_MIN"], kind: Kind::Float, default: "0.005" },
    EnvVar { key: "BASKFY_SWING_TELEGRAM_BOT_TOKEN", module: "app.config", attrs: &["SWING_TELEGRAM_BOT_TOKEN"], kind: Kind::Str, default: "" },
    EnvVar { key: "BASKFY_SWING_TELEGRAM_CHAT_ID", module: "app.config", attrs: &["SWING_TELEGRAM_CHAT_ID"], kind: Kind::Str, default: "" },
    EnvVar { key: "BASKFY_TWT_AUTO_EXECUTE", module: "app.config", attrs: &["TWT_AUTO_EXECUTE"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_TWT_EXECUTION_ENABLED", module: "app.config", attrs: &["TWT_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_VBT_EXECUTION_ENABLED", module: "app.config", attrs: &["VBT_EXECUTION_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "BASKFY_VBT_GTT_LIMIT_FRACTION", module: "app.config", attrs: &["VBT_GTT_LIMIT_FRACTION"], kind: Kind::Float, default: "0.97" },
    EnvVar { key: "BASKFY_VBT_STOP_BAND_MAX", module: "app.config", attrs: &["VBT_STOP_BAND_MAX"], kind: Kind::Float, default: "0.15" },
    EnvVar { key: "BASKFY_VBT_STOP_BAND_MIN", module: "app.config", attrs: &["VBT_STOP_BAND_MIN"], kind: Kind::Float, default: "0.005" },
    EnvVar { key: "DB_PATH", module: "app.config", attrs: &["DB_PATH"], kind: Kind::Str, default: "data/portfolio.db" },
    EnvVar { key: "DESK_ALLOWED_HOSTS", module: "app.config", attrs: &["DESK_ALLOWED_HOSTS"], kind: Kind::Str, default: "127.0.0.1,localhost,testserver" },
    EnvVar { key: "DESK_DOCS", module: "app.config", attrs: &["DESK_DOCS"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "DESK_NOTIFY_FROM", module: "app.config", attrs: &["DESK_NOTIFY_FROM"], kind: Kind::Str, default: "" },
    EnvVar { key: "DESK_NOTIFY_TO", module: "app.config", attrs: &["DESK_NOTIFY_TO"], kind: Kind::Str, default: "" },
    EnvVar { key: "DESK_PASSWORD", module: "app.config", attrs: &["DESK_PASSWORD"], kind: Kind::Str, default: "" },
    EnvVar { key: "DESK_PATH_PREFIX", module: "app.config", attrs: &["DESK_PATH_PREFIX"], kind: Kind::Str, default: "" },
    EnvVar { key: "DESK_PORT", module: "app.config", attrs: &["DESK_PORT"], kind: Kind::Int, default: "8420" },
    EnvVar { key: "DESK_SHARED_READ_LIMITS", module: "app.config", attrs: &["SHARED_READ_LIMITS"], kind: Kind::BoolNeFalse, default: "true" },
    EnvVar { key: "DESK_SMTP_HOST", module: "app.config", attrs: &["DESK_SMTP_HOST"], kind: Kind::Str, default: "" },
    EnvVar { key: "DESK_SMTP_PASSWORD", module: "app.config", attrs: &["DESK_SMTP_PASSWORD"], kind: Kind::Str, default: "" },
    EnvVar { key: "DESK_SMTP_PORT", module: "app.config", attrs: &["DESK_SMTP_PORT"], kind: Kind::IntOr(587), default: "587" },
    EnvVar { key: "DESK_SMTP_STARTTLS", module: "app.config", attrs: &["DESK_SMTP_STARTTLS"], kind: Kind::BoolNeFalse, default: "true" },
    EnvVar { key: "DESK_SMTP_USER", module: "app.config", attrs: &["DESK_SMTP_USER"], kind: Kind::Str, default: "" },
    EnvVar { key: "DRY_RUN", module: "app.config", attrs: &["DRY_RUN"], kind: Kind::BoolNeFalse, default: "true" },
    EnvVar { key: "FORCE_IPV4", module: "app.config", attrs: &["FORCE_IPV4"], kind: Kind::BoolNeFalse, default: "true" },
    EnvVar { key: "FULLY_INVESTED", module: "app.config", attrs: &["FULLY_INVESTED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "INCLUDE_UNTOUCHABLE_IN_NAV", module: "app.config", attrs: &["INCLUDE_UNTOUCHABLE_IN_NAV"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "INTRADAY_ENABLED", module: "app.config", attrs: &["INTRADAY_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "KITE_API_KEY", module: "app.config", attrs: &["KITE_API_KEY"], kind: Kind::Str, default: "" },
    EnvVar { key: "KITE_API_SECRET", module: "app.config", attrs: &["KITE_API_SECRET"], kind: Kind::Str, default: "" },
    EnvVar { key: "OPTIONS_ENABLED", module: "app.config", attrs: &["OPTIONS_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "OPTION_BUY_DELTA", module: "app.config", attrs: &["OPTION_BUY_DELTA"], kind: Kind::Float, default: "0.60" },
    EnvVar { key: "OPTION_BUY_ENTRY_END", module: "app.config", attrs: &["OPTION_BUY_ENTRY_END"], kind: Kind::Str, default: "14:30" },
    EnvVar { key: "OPTION_BUY_ENTRY_START", module: "app.config", attrs: &["OPTION_BUY_ENTRY_START"], kind: Kind::Str, default: "09:35" },
    EnvVar { key: "OPTION_BUY_PREMIUM_STOP_PCT", module: "app.config", attrs: &["OPTION_BUY_PREMIUM_STOP_PCT"], kind: Kind::Float, default: "25" },
    EnvVar { key: "OPTION_BUY_RISK_PER_TRADE_PCT", module: "app.config", attrs: &["OPTION_BUY_RISK_PER_TRADE_PCT"], kind: Kind::Float, default: "0.50" },
    EnvVar { key: "OPTION_BUY_TRADE_BENCHMARK_PCT", module: "app.config", attrs: &["OPTION_BUY_TRADE_BENCHMARK_PCT"], kind: Kind::Float, default: "15.0" },
    EnvVar { key: "OPTION_BUY_TRAIL_ACTIVATION_PCT", module: "app.config", attrs: &["OPTION_BUY_TRAIL_ACTIVATION_PCT"], kind: Kind::Float, default: "15" },
    EnvVar { key: "OPTION_BUY_TRAIL_DRAWDOWN_PCT", module: "app.config", attrs: &["OPTION_BUY_TRAIL_DRAWDOWN_PCT"], kind: Kind::Float, default: "8" },
    EnvVar { key: "OPTION_INTRADAY_SQUARE_OFF", module: "app.config", attrs: &["OPTION_INTRADAY_SQUARE_OFF"], kind: Kind::Str, default: "15:12" },
    EnvVar { key: "OPTION_SELL_ENTRY_END", module: "app.config", attrs: &["OPTION_SELL_ENTRY_END"], kind: Kind::Str, default: "13:30" },
    EnvVar { key: "OPTION_SELL_ENTRY_START", module: "app.config", attrs: &["OPTION_SELL_ENTRY_START"], kind: Kind::Str, default: "09:45" },
    EnvVar { key: "OPTION_SELL_MONTHLY_BENCHMARK_PCT", module: "app.config", attrs: &["OPTION_SELL_MONTHLY_BENCHMARK_PCT"], kind: Kind::Float, default: "5.0" },
    EnvVar { key: "OPTION_SELL_RISK_PER_TRADE_PCT", module: "app.config", attrs: &["OPTION_SELL_RISK_PER_TRADE_PCT"], kind: Kind::Float, default: "0.75" },
    EnvVar { key: "OPTION_SELL_SHORT_DELTA", module: "app.config", attrs: &["OPTION_SELL_SHORT_DELTA"], kind: Kind::Float, default: "0.16" },
    EnvVar { key: "OPTION_SELL_STOP_CREDIT_MULTIPLE", module: "app.config", attrs: &["OPTION_SELL_STOP_CREDIT_MULTIPLE"], kind: Kind::Float, default: "1.5" },
    EnvVar { key: "OPTION_SELL_TARGET_CAPTURE_PCT", module: "app.config", attrs: &["OPTION_SELL_TARGET_CAPTURE_PCT"], kind: Kind::Float, default: "35" },
    EnvVar { key: "OPTION_SELL_WING_DELTA", module: "app.config", attrs: &["OPTION_SELL_WING_DELTA"], kind: Kind::Float, default: "0.05" },
    EnvVar { key: "OPTION_UNDERLYING", module: "app.config", attrs: &["OPTION_UNDERLYING"], kind: Kind::Str, default: "NIFTY" },
    EnvVar { key: "PORT", module: "app.config", attrs: &["PORT"], kind: Kind::Int, default: "8420" },
    EnvVar { key: "REGIME_BOOTSTRAP_POLICY", module: "app.config", attrs: &["REGIME_BOOTSTRAP_POLICY"], kind: Kind::Str, default: "observe_only" },
    EnvVar { key: "REGIME_BREADTH_STALE_DAYS", module: "app.config", attrs: &["REGIME_BREADTH_STALE_DAYS"], kind: Kind::Int, default: "10" },
    EnvVar { key: "REGIME_BUFFER_BPS", module: "app.config", attrs: &["REGIME_BUFFER_BPS"], kind: Kind::Int, default: "150" },
    EnvVar { key: "REGIME_CONFIRM_DAYS", module: "app.config", attrs: &["REGIME_CONFIRM_DAYS"], kind: Kind::Int, default: "3" },
    EnvVar { key: "REGIME_ENABLED", module: "app.config", attrs: &["REGIME_ENABLED"], kind: Kind::BoolEqTrue, default: "false" },
    EnvVar { key: "REGIME_HALF_SIZE_FACTOR", module: "app.config", attrs: &["REGIME_HALF_SIZE_FACTOR"], kind: Kind::Float, default: "0.5" },
    EnvVar { key: "REGIME_INDEX_STALE_DAYS", module: "app.config", attrs: &["REGIME_INDEX_STALE_DAYS"], kind: Kind::Int, default: "5" },
    EnvVar { key: "REGIME_LTCG_REVIEW_DAYS", module: "app.config", attrs: &["REGIME_LTCG_REVIEW_DAYS"], kind: Kind::Int, default: "45" },
    EnvVar { key: "REGIME_MIN_BREADTH_COVERAGE_PCT", module: "app.config", attrs: &["REGIME_MIN_BREADTH_COVERAGE_PCT"], kind: Kind::Float, default: "90" },
    EnvVar { key: "REGIME_MODE", module: "app.config", attrs: &["REGIME_MODE"], kind: Kind::Str, default: "observe" },
    EnvVar { key: "REGIME_NEARBY_RANK_BAND", module: "app.config", attrs: &["REGIME_NEARBY_RANK_BAND"], kind: Kind::Int, default: "3" },
    EnvVar { key: "REGIME_R4_EQUITY_PCT", module: "app.config", attrs: &["REGIME_R4_EQUITY_PCT"], kind: Kind::Float, default: "10" },
    EnvVar { key: "REGIME_R4_MAX_RESIDUAL_NAMES", module: "app.config", attrs: &["REGIME_R4_MAX_RESIDUAL_NAMES"], kind: Kind::Int, default: "2" },
    EnvVar { key: "REGIME_R4_RESIDUAL_ORDERING", module: "app.config", attrs: &["REGIME_R4_RESIDUAL_ORDERING"], kind: Kind::CsvTuple, default: "rank,liquidity" },
    EnvVar { key: "REGIME_WEEKLY_EVAL_WEEKDAY", module: "app.config", attrs: &["REGIME_WEEKLY_EVAL_WEEKDAY"], kind: Kind::Int, default: "4" },
    EnvVar { key: "RISK_GROSS_MULTIPLE", module: "app.config", attrs: &["RISK_GROSS_MULTIPLE"], kind: Kind::Float, default: "1.5" },
    EnvVar { key: "RISK_MAX_DAILY_LOSS_PCT", module: "app.config", attrs: &["RISK_MAX_DAILY_LOSS_PCT"], kind: Kind::Float, default: "5.0" },
    EnvVar { key: "RISK_MAX_GROSS_EXPOSURE", module: "app.config", attrs: &["RISK_MAX_GROSS_EXPOSURE"], kind: Kind::Str, default: "" },
    EnvVar { key: "RISK_MAX_ORDERS_PER_DAY", module: "app.config", attrs: &["RISK_MAX_ORDERS_PER_DAY"], kind: Kind::Int, default: "500" },
    EnvVar { key: "RISK_MAX_POSITION_VALUE", module: "app.config", attrs: &["RISK_MAX_POSITION_VALUE"], kind: Kind::Str, default: "" },
    EnvVar { key: "RISK_POSITION_HEADROOM", module: "app.config", attrs: &["RISK_POSITION_HEADROOM"], kind: Kind::Float, default: "1.25" },
    EnvVar { key: "SCAN_SOURCE_DEFAULT", module: "app.config", attrs: &["SCAN_SOURCE_DEFAULT"], kind: Kind::Str, default: "upload" },
    EnvVar { key: "TOKEN_FILE", module: "app.config", attrs: &["TOKEN_FILE"], kind: Kind::Str, default: "data/.kite_token.json" },
    EnvVar { key: "DESK_DB_BACKEND", module: "app.analytics.db", attrs: &["DB_BACKEND"], kind: Kind::Str, default: "sqlite" },
    EnvVar { key: "DESK_DB_SCHEMA", module: "app.analytics.db", attrs: &["DB_SCHEMA"], kind: Kind::Str, default: "desk" },
    EnvVar { key: "SCREENER_DATABASE_URL", module: "app.scan_source", attrs: &["SCREENER_DSN"], kind: Kind::Str, default: "postgresql://baskfy:baskfy@localhost:5433/baskfy" },
    EnvVar { key: "DESK_UNIVERSE_SLUG", module: "app.breadth_source", attrs: &["DESK_UNIVERSE_SLUG"], kind: Kind::Str, default: "nifty-total-market" },
    EnvVar { key: "SCREENER_DATABASE_URL", module: "app.breadth_source", attrs: &["SCREENER_DSN"], kind: Kind::Str, default: "postgresql://baskfy:baskfy@localhost:5433/baskfy" },
    EnvVar { key: "METRICS_PORT", module: "app.telemetry", attrs: &["METRICS_PORT"], kind: Kind::Str, default: "" },
    EnvVar { key: "OTEL_EXPORTER_OTLP_ENDPOINT", module: "app.telemetry", attrs: &["OTLP_ENDPOINT"], kind: Kind::Str, default: "" },
    EnvVar { key: "OTEL_SERVICE_NAME", module: "app.telemetry", attrs: &["SERVICE_NAME"], kind: Kind::Str, default: "momentum-desk" },
    EnvVar { key: "SENTRY_DSN", module: "app.telemetry", attrs: &["SENTRY_DSN"], kind: Kind::Str, default: "" },
];

/// Every module-level setting the Python desk reads from the environment, typed.
///
/// One field per Python attribute (`app.config.DRY_RUN` → `dry_run`); generated from
/// `fixtures/settings_defaults.json` and held to it by `tests/settings.rs`.
#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    /// `app.config.FNO_CARRY_ENABLED` (env `BASKFY_FNO_CARRY_ENABLED`).
    pub fno_carry_enabled: bool,
    /// `app.config.FNO_F1_EXECUTION_ENABLED` (env `BASKFY_FNO_F1_EXECUTION_ENABLED`).
    pub fno_f1_execution_enabled: bool,
    /// `app.config.FNO_F2_EXECUTION_ENABLED` (env `BASKFY_FNO_F2_EXECUTION_ENABLED`).
    pub fno_f2_execution_enabled: bool,
    /// `app.config.FNO_F3_AUTO_EXIT` (env `BASKFY_FNO_F3_AUTO_EXIT`).
    pub fno_f3_auto_exit: bool,
    /// `app.config.FNO_F3_EXECUTION_ENABLED` (env `BASKFY_FNO_F3_EXECUTION_ENABLED`).
    pub fno_f3_execution_enabled: bool,
    /// `app.config.FNO_MONITOR_ENABLED` (env `BASKFY_FNO_MONITOR_ENABLED`).
    pub fno_monitor_enabled: bool,
    /// `app.config.OPTIONS_BOOK_DAILY_LOSS_INR_MAX` (env `BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX`).
    pub options_book_daily_loss_inr_max: String,
    /// `app.config.OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX` (env `BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX`).
    pub options_book_monthly_loss_inr_max: String,
    /// `app.config.OPTIONS_COLLECT_ENABLED` (env `BASKFY_OPTIONS_COLLECT_ENABLED`).
    pub options_collect_enabled: bool,
    /// `app.config.OPTIONS_HARD_EXIT_LATEST` (env `BASKFY_OPTIONS_HARD_EXIT_LATEST`).
    pub options_hard_exit_latest: String,
    /// `app.config.OPTIONS_MAX_LOTS_MAX` (env `BASKFY_OPTIONS_MAX_LOTS_MAX`).
    pub options_max_lots_max: i64,
    /// `app.config.OPTIONS_MONITOR_ENABLED` (env `BASKFY_OPTIONS_MONITOR_ENABLED`).
    pub options_monitor_enabled: bool,
    /// `app.config.OPTIONS_O1M_EXECUTION_ENABLED` (env `BASKFY_OPTIONS_O1M_EXECUTION_ENABLED`).
    pub options_o1m_execution_enabled: bool,
    /// `app.config.OPTIONS_O1W_EXECUTION_ENABLED` (env `BASKFY_OPTIONS_O1W_EXECUTION_ENABLED`).
    pub options_o1w_execution_enabled: bool,
    /// `app.config.OPTIONS_O2_EXECUTION_ENABLED` (env `BASKFY_OPTIONS_O2_EXECUTION_ENABLED`).
    pub options_o2_execution_enabled: bool,
    /// `app.config.OPTIONS_O3_EXECUTION_ENABLED` (env `BASKFY_OPTIONS_O3_EXECUTION_ENABLED`).
    pub options_o3_execution_enabled: bool,
    /// `app.config.OPTIONS_RISK_PCT_MAX` (env `BASKFY_OPTIONS_RISK_PCT_MAX`).
    pub options_risk_pct_max: String,
    /// `app.config.OPTIONS_RISK_PER_TRADE_INR_MAX` (env `BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX`).
    pub options_risk_per_trade_inr_max: String,
    /// `app.config.OPTIONS_SCAN_ENABLED` (env `BASKFY_OPTIONS_SCAN_ENABLED`).
    pub options_scan_enabled: bool,
    /// `app.config.SHARED_READ_LIMITS_REDIS_URL` (env `BASKFY_REDIS_URL`).
    pub shared_read_limits_redis_url: String,
    /// `app.config.SOLE_BROKER_ACCOUNT_ID` (env `BASKFY_SOLE_BROKER_ACCOUNT_ID`).
    pub sole_broker_account_id: i64,
    /// `app.config.SOLE_USER_ID` (env `BASKFY_SOLE_USER_ID`).
    pub sole_user_id: i64,
    /// `app.config.SWING_AUTO_EXECUTE` (env `BASKFY_SWING_AUTO_EXECUTE`).
    pub swing_auto_execute: bool,
    /// `app.config._EP_PREMARKET` (env `BASKFY_SWING_EP_PREMARKET_ENABLED`).
    pub ep_premarket_raw: String,
    /// `app.config.SWING_EP_PREMARKET_ENABLED` (env `BASKFY_SWING_EP_PREMARKET_ENABLED`).
    pub swing_ep_premarket_enabled: bool,
    /// `app.config.SWING_EXECUTION_ENABLED` (env `BASKFY_SWING_EXECUTION_ENABLED`).
    pub swing_execution_enabled: bool,
    /// `app.config.SWING_GTT_LIMIT_FRACTION` (env `BASKFY_SWING_GTT_LIMIT_FRACTION`).
    pub swing_gtt_limit_fraction: f64,
    /// `app.config.SWING_INDEX_SLUG` (env `BASKFY_SWING_INDEX_SLUG`).
    pub swing_index_slug: String,
    /// `app.config.SWING_MONITOR_ENABLED` (env `BASKFY_SWING_MONITOR_ENABLED`).
    pub swing_monitor_enabled: bool,
    /// `app.config.SWING_SCAN_MIN_INTERVAL_SECONDS` (env `BASKFY_SWING_SCAN_MIN_INTERVAL_SECONDS`).
    pub swing_scan_min_interval_seconds: i64,
    /// `app.config.SWING_SCAN_STALE_AFTER_SECONDS` (env `BASKFY_SWING_SCAN_STALE_AFTER_SECONDS`).
    pub swing_scan_stale_after_seconds: i64,
    /// `app.config.SWING_STOP_BAND_MAX` (env `BASKFY_SWING_STOP_BAND_MAX`).
    pub swing_stop_band_max: f64,
    /// `app.config.SWING_STOP_BAND_MIN` (env `BASKFY_SWING_STOP_BAND_MIN`).
    pub swing_stop_band_min: f64,
    /// `app.config.SWING_TELEGRAM_BOT_TOKEN` (env `BASKFY_SWING_TELEGRAM_BOT_TOKEN`).
    pub swing_telegram_bot_token: String,
    /// `app.config.SWING_TELEGRAM_CHAT_ID` (env `BASKFY_SWING_TELEGRAM_CHAT_ID`).
    pub swing_telegram_chat_id: String,
    /// `app.config.TWT_AUTO_EXECUTE` (env `BASKFY_TWT_AUTO_EXECUTE`).
    pub twt_auto_execute: bool,
    /// `app.config.TWT_EXECUTION_ENABLED` (env `BASKFY_TWT_EXECUTION_ENABLED`).
    pub twt_execution_enabled: bool,
    /// `app.config.VBT_EXECUTION_ENABLED` (env `BASKFY_VBT_EXECUTION_ENABLED`).
    pub vbt_execution_enabled: bool,
    /// `app.config.VBT_GTT_LIMIT_FRACTION` (env `BASKFY_VBT_GTT_LIMIT_FRACTION`).
    pub vbt_gtt_limit_fraction: f64,
    /// `app.config.VBT_STOP_BAND_MAX` (env `BASKFY_VBT_STOP_BAND_MAX`).
    pub vbt_stop_band_max: f64,
    /// `app.config.VBT_STOP_BAND_MIN` (env `BASKFY_VBT_STOP_BAND_MIN`).
    pub vbt_stop_band_min: f64,
    /// `app.config.DB_PATH` (env `DB_PATH`).
    pub db_path: String,
    /// `app.config.DESK_ALLOWED_HOSTS` (env `DESK_ALLOWED_HOSTS`).
    pub desk_allowed_hosts: String,
    /// `app.config.DESK_DOCS` (env `DESK_DOCS`).
    pub desk_docs: bool,
    /// `app.config.DESK_NOTIFY_FROM` (env `DESK_NOTIFY_FROM`).
    pub desk_notify_from: String,
    /// `app.config.DESK_NOTIFY_TO` (env `DESK_NOTIFY_TO`).
    pub desk_notify_to: String,
    /// `app.config.DESK_PASSWORD` (env `DESK_PASSWORD`).
    pub desk_password: String,
    /// `app.config.DESK_PATH_PREFIX` (env `DESK_PATH_PREFIX`).
    pub desk_path_prefix: String,
    /// `app.config.DESK_PORT` (env `DESK_PORT`).
    pub desk_port: i64,
    /// `app.config.SHARED_READ_LIMITS` (env `DESK_SHARED_READ_LIMITS`).
    pub shared_read_limits: bool,
    /// `app.config.DESK_SMTP_HOST` (env `DESK_SMTP_HOST`).
    pub desk_smtp_host: String,
    /// `app.config.DESK_SMTP_PASSWORD` (env `DESK_SMTP_PASSWORD`).
    pub desk_smtp_password: String,
    /// `app.config.DESK_SMTP_PORT` (env `DESK_SMTP_PORT`).
    pub desk_smtp_port: i64,
    /// `app.config.DESK_SMTP_STARTTLS` (env `DESK_SMTP_STARTTLS`).
    pub desk_smtp_starttls: bool,
    /// `app.config.DESK_SMTP_USER` (env `DESK_SMTP_USER`).
    pub desk_smtp_user: String,
    /// `app.config.DRY_RUN` (env `DRY_RUN`).
    pub dry_run: bool,
    /// `app.config.FORCE_IPV4` (env `FORCE_IPV4`).
    pub force_ipv4: bool,
    /// `app.config.FULLY_INVESTED` (env `FULLY_INVESTED`).
    pub fully_invested: bool,
    /// `app.config.INCLUDE_UNTOUCHABLE_IN_NAV` (env `INCLUDE_UNTOUCHABLE_IN_NAV`).
    pub include_untouchable_in_nav: bool,
    /// `app.config.INTRADAY_ENABLED` (env `INTRADAY_ENABLED`).
    pub intraday_enabled: bool,
    /// `app.config.KITE_API_KEY` (env `KITE_API_KEY`).
    pub kite_api_key: String,
    /// `app.config.KITE_API_SECRET` (env `KITE_API_SECRET`).
    pub kite_api_secret: String,
    /// `app.config.OPTIONS_ENABLED` (env `OPTIONS_ENABLED`).
    pub options_enabled: bool,
    /// `app.config.OPTION_BUY_DELTA` (env `OPTION_BUY_DELTA`).
    pub option_buy_delta: f64,
    /// `app.config.OPTION_BUY_ENTRY_END` (env `OPTION_BUY_ENTRY_END`).
    pub option_buy_entry_end: String,
    /// `app.config.OPTION_BUY_ENTRY_START` (env `OPTION_BUY_ENTRY_START`).
    pub option_buy_entry_start: String,
    /// `app.config.OPTION_BUY_PREMIUM_STOP_PCT` (env `OPTION_BUY_PREMIUM_STOP_PCT`).
    pub option_buy_premium_stop_pct: f64,
    /// `app.config.OPTION_BUY_RISK_PER_TRADE_PCT` (env `OPTION_BUY_RISK_PER_TRADE_PCT`).
    pub option_buy_risk_per_trade_pct: f64,
    /// `app.config.OPTION_BUY_TRADE_BENCHMARK_PCT` (env `OPTION_BUY_TRADE_BENCHMARK_PCT`).
    pub option_buy_trade_benchmark_pct: f64,
    /// `app.config.OPTION_BUY_TRAIL_ACTIVATION_PCT` (env `OPTION_BUY_TRAIL_ACTIVATION_PCT`).
    pub option_buy_trail_activation_pct: f64,
    /// `app.config.OPTION_BUY_TRAIL_DRAWDOWN_PCT` (env `OPTION_BUY_TRAIL_DRAWDOWN_PCT`).
    pub option_buy_trail_drawdown_pct: f64,
    /// `app.config.OPTION_INTRADAY_SQUARE_OFF` (env `OPTION_INTRADAY_SQUARE_OFF`).
    pub option_intraday_square_off: String,
    /// `app.config.OPTION_SELL_ENTRY_END` (env `OPTION_SELL_ENTRY_END`).
    pub option_sell_entry_end: String,
    /// `app.config.OPTION_SELL_ENTRY_START` (env `OPTION_SELL_ENTRY_START`).
    pub option_sell_entry_start: String,
    /// `app.config.OPTION_SELL_MONTHLY_BENCHMARK_PCT` (env `OPTION_SELL_MONTHLY_BENCHMARK_PCT`).
    pub option_sell_monthly_benchmark_pct: f64,
    /// `app.config.OPTION_SELL_RISK_PER_TRADE_PCT` (env `OPTION_SELL_RISK_PER_TRADE_PCT`).
    pub option_sell_risk_per_trade_pct: f64,
    /// `app.config.OPTION_SELL_SHORT_DELTA` (env `OPTION_SELL_SHORT_DELTA`).
    pub option_sell_short_delta: f64,
    /// `app.config.OPTION_SELL_STOP_CREDIT_MULTIPLE` (env `OPTION_SELL_STOP_CREDIT_MULTIPLE`).
    pub option_sell_stop_credit_multiple: f64,
    /// `app.config.OPTION_SELL_TARGET_CAPTURE_PCT` (env `OPTION_SELL_TARGET_CAPTURE_PCT`).
    pub option_sell_target_capture_pct: f64,
    /// `app.config.OPTION_SELL_WING_DELTA` (env `OPTION_SELL_WING_DELTA`).
    pub option_sell_wing_delta: f64,
    /// `app.config.OPTION_UNDERLYING` (env `OPTION_UNDERLYING`).
    pub option_underlying: String,
    /// `app.config.PORT` (env `PORT`).
    pub port: i64,
    /// `app.config.REGIME_BOOTSTRAP_POLICY` (env `REGIME_BOOTSTRAP_POLICY`).
    pub regime_bootstrap_policy: String,
    /// `app.config.REGIME_BREADTH_STALE_DAYS` (env `REGIME_BREADTH_STALE_DAYS`).
    pub regime_breadth_stale_days: i64,
    /// `app.config.REGIME_BUFFER_BPS` (env `REGIME_BUFFER_BPS`).
    pub regime_buffer_bps: i64,
    /// `app.config.REGIME_CONFIRM_DAYS` (env `REGIME_CONFIRM_DAYS`).
    pub regime_confirm_days: i64,
    /// `app.config.REGIME_ENABLED` (env `REGIME_ENABLED`).
    pub regime_enabled: bool,
    /// `app.config.REGIME_HALF_SIZE_FACTOR` (env `REGIME_HALF_SIZE_FACTOR`).
    pub regime_half_size_factor: f64,
    /// `app.config.REGIME_INDEX_STALE_DAYS` (env `REGIME_INDEX_STALE_DAYS`).
    pub regime_index_stale_days: i64,
    /// `app.config.REGIME_LTCG_REVIEW_DAYS` (env `REGIME_LTCG_REVIEW_DAYS`).
    pub regime_ltcg_review_days: i64,
    /// `app.config.REGIME_MIN_BREADTH_COVERAGE_PCT` (env `REGIME_MIN_BREADTH_COVERAGE_PCT`).
    pub regime_min_breadth_coverage_pct: f64,
    /// `app.config.REGIME_MODE` (env `REGIME_MODE`).
    pub regime_mode: String,
    /// `app.config.REGIME_NEARBY_RANK_BAND` (env `REGIME_NEARBY_RANK_BAND`).
    pub regime_nearby_rank_band: i64,
    /// `app.config.REGIME_R4_EQUITY_PCT` (env `REGIME_R4_EQUITY_PCT`).
    pub regime_r4_equity_pct: f64,
    /// `app.config.REGIME_R4_MAX_RESIDUAL_NAMES` (env `REGIME_R4_MAX_RESIDUAL_NAMES`).
    pub regime_r4_max_residual_names: i64,
    /// `app.config.REGIME_R4_RESIDUAL_ORDERING` (env `REGIME_R4_RESIDUAL_ORDERING`).
    pub regime_r4_residual_ordering: Vec<String>,
    /// `app.config.REGIME_WEEKLY_EVAL_WEEKDAY` (env `REGIME_WEEKLY_EVAL_WEEKDAY`).
    pub regime_weekly_eval_weekday: i64,
    /// `app.config.RISK_GROSS_MULTIPLE` (env `RISK_GROSS_MULTIPLE`).
    pub risk_gross_multiple: f64,
    /// `app.config.RISK_MAX_DAILY_LOSS_PCT` (env `RISK_MAX_DAILY_LOSS_PCT`).
    pub risk_max_daily_loss_pct: f64,
    /// `app.config.RISK_MAX_GROSS_EXPOSURE` (env `RISK_MAX_GROSS_EXPOSURE`).
    pub risk_max_gross_exposure: String,
    /// `app.config.RISK_MAX_ORDERS_PER_DAY` (env `RISK_MAX_ORDERS_PER_DAY`).
    pub risk_max_orders_per_day: i64,
    /// `app.config.RISK_MAX_POSITION_VALUE` (env `RISK_MAX_POSITION_VALUE`).
    pub risk_max_position_value: String,
    /// `app.config.RISK_POSITION_HEADROOM` (env `RISK_POSITION_HEADROOM`).
    pub risk_position_headroom: f64,
    /// `app.config.SCAN_SOURCE_DEFAULT` (env `SCAN_SOURCE_DEFAULT`).
    pub scan_source_default: String,
    /// `app.config.TOKEN_FILE` (env `TOKEN_FILE`).
    pub token_file: String,
    /// `app.analytics.db.DB_BACKEND` (env `DESK_DB_BACKEND`).
    pub db_backend: String,
    /// `app.analytics.db.DB_SCHEMA` (env `DESK_DB_SCHEMA`).
    pub db_schema: String,
    /// `app.scan_source.SCREENER_DSN` (env `SCREENER_DATABASE_URL`).
    pub screener_dsn: String,
    /// `app.breadth_source.DESK_UNIVERSE_SLUG` (env `DESK_UNIVERSE_SLUG`).
    pub desk_universe_slug: String,
    /// `app.telemetry.METRICS_PORT` (env `METRICS_PORT`).
    pub metrics_port: String,
    /// `app.telemetry.OTLP_ENDPOINT` (env `OTEL_EXPORTER_OTLP_ENDPOINT`).
    pub otlp_endpoint: String,
    /// `app.telemetry.SERVICE_NAME` (env `OTEL_SERVICE_NAME`).
    pub service_name: String,
    /// `app.telemetry.SENTRY_DSN` (env `SENTRY_DSN`).
    pub sentry_dsn: String,
}

impl Settings {
    fn from_values(values: &BTreeMap<&'static str, ConfigValue>) -> Result<Self, SettingsError> {
        Ok(Self {
            fno_carry_enabled: take_bool(values, "FNO_CARRY_ENABLED")?,
            fno_f1_execution_enabled: take_bool(values, "FNO_F1_EXECUTION_ENABLED")?,
            fno_f2_execution_enabled: take_bool(values, "FNO_F2_EXECUTION_ENABLED")?,
            fno_f3_auto_exit: take_bool(values, "FNO_F3_AUTO_EXIT")?,
            fno_f3_execution_enabled: take_bool(values, "FNO_F3_EXECUTION_ENABLED")?,
            fno_monitor_enabled: take_bool(values, "FNO_MONITOR_ENABLED")?,
            options_book_daily_loss_inr_max: take_str(values, "OPTIONS_BOOK_DAILY_LOSS_INR_MAX")?,
            options_book_monthly_loss_inr_max: take_str(
                values,
                "OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX",
            )?,
            options_collect_enabled: take_bool(values, "OPTIONS_COLLECT_ENABLED")?,
            options_hard_exit_latest: take_str(values, "OPTIONS_HARD_EXIT_LATEST")?,
            options_max_lots_max: take_int(values, "OPTIONS_MAX_LOTS_MAX")?,
            options_monitor_enabled: take_bool(values, "OPTIONS_MONITOR_ENABLED")?,
            options_o1m_execution_enabled: take_bool(values, "OPTIONS_O1M_EXECUTION_ENABLED")?,
            options_o1w_execution_enabled: take_bool(values, "OPTIONS_O1W_EXECUTION_ENABLED")?,
            options_o2_execution_enabled: take_bool(values, "OPTIONS_O2_EXECUTION_ENABLED")?,
            options_o3_execution_enabled: take_bool(values, "OPTIONS_O3_EXECUTION_ENABLED")?,
            options_risk_pct_max: take_str(values, "OPTIONS_RISK_PCT_MAX")?,
            options_risk_per_trade_inr_max: take_str(values, "OPTIONS_RISK_PER_TRADE_INR_MAX")?,
            options_scan_enabled: take_bool(values, "OPTIONS_SCAN_ENABLED")?,
            shared_read_limits_redis_url: take_str(values, "SHARED_READ_LIMITS_REDIS_URL")?,
            sole_broker_account_id: take_int(values, "SOLE_BROKER_ACCOUNT_ID")?,
            sole_user_id: take_int(values, "SOLE_USER_ID")?,
            swing_auto_execute: take_bool(values, "SWING_AUTO_EXECUTE")?,
            ep_premarket_raw: take_str(values, "_EP_PREMARKET")?,
            swing_ep_premarket_enabled: take_bool(values, "SWING_EP_PREMARKET_ENABLED")?,
            swing_execution_enabled: take_bool(values, "SWING_EXECUTION_ENABLED")?,
            swing_gtt_limit_fraction: take_float(values, "SWING_GTT_LIMIT_FRACTION")?,
            swing_index_slug: take_str(values, "SWING_INDEX_SLUG")?,
            swing_monitor_enabled: take_bool(values, "SWING_MONITOR_ENABLED")?,
            swing_scan_min_interval_seconds: take_int(values, "SWING_SCAN_MIN_INTERVAL_SECONDS")?,
            swing_scan_stale_after_seconds: take_int(values, "SWING_SCAN_STALE_AFTER_SECONDS")?,
            swing_stop_band_max: take_float(values, "SWING_STOP_BAND_MAX")?,
            swing_stop_band_min: take_float(values, "SWING_STOP_BAND_MIN")?,
            swing_telegram_bot_token: take_str(values, "SWING_TELEGRAM_BOT_TOKEN")?,
            swing_telegram_chat_id: take_str(values, "SWING_TELEGRAM_CHAT_ID")?,
            twt_auto_execute: take_bool(values, "TWT_AUTO_EXECUTE")?,
            twt_execution_enabled: take_bool(values, "TWT_EXECUTION_ENABLED")?,
            vbt_execution_enabled: take_bool(values, "VBT_EXECUTION_ENABLED")?,
            vbt_gtt_limit_fraction: take_float(values, "VBT_GTT_LIMIT_FRACTION")?,
            vbt_stop_band_max: take_float(values, "VBT_STOP_BAND_MAX")?,
            vbt_stop_band_min: take_float(values, "VBT_STOP_BAND_MIN")?,
            db_path: take_str(values, "DB_PATH")?,
            desk_allowed_hosts: take_str(values, "DESK_ALLOWED_HOSTS")?,
            desk_docs: take_bool(values, "DESK_DOCS")?,
            desk_notify_from: take_str(values, "DESK_NOTIFY_FROM")?,
            desk_notify_to: take_str(values, "DESK_NOTIFY_TO")?,
            desk_password: take_str(values, "DESK_PASSWORD")?,
            desk_path_prefix: take_str(values, "DESK_PATH_PREFIX")?,
            desk_port: take_int(values, "DESK_PORT")?,
            shared_read_limits: take_bool(values, "SHARED_READ_LIMITS")?,
            desk_smtp_host: take_str(values, "DESK_SMTP_HOST")?,
            desk_smtp_password: take_str(values, "DESK_SMTP_PASSWORD")?,
            desk_smtp_port: take_int(values, "DESK_SMTP_PORT")?,
            desk_smtp_starttls: take_bool(values, "DESK_SMTP_STARTTLS")?,
            desk_smtp_user: take_str(values, "DESK_SMTP_USER")?,
            dry_run: take_bool(values, "DRY_RUN")?,
            force_ipv4: take_bool(values, "FORCE_IPV4")?,
            fully_invested: take_bool(values, "FULLY_INVESTED")?,
            include_untouchable_in_nav: take_bool(values, "INCLUDE_UNTOUCHABLE_IN_NAV")?,
            intraday_enabled: take_bool(values, "INTRADAY_ENABLED")?,
            kite_api_key: take_str(values, "KITE_API_KEY")?,
            kite_api_secret: take_str(values, "KITE_API_SECRET")?,
            options_enabled: take_bool(values, "OPTIONS_ENABLED")?,
            option_buy_delta: take_float(values, "OPTION_BUY_DELTA")?,
            option_buy_entry_end: take_str(values, "OPTION_BUY_ENTRY_END")?,
            option_buy_entry_start: take_str(values, "OPTION_BUY_ENTRY_START")?,
            option_buy_premium_stop_pct: take_float(values, "OPTION_BUY_PREMIUM_STOP_PCT")?,
            option_buy_risk_per_trade_pct: take_float(values, "OPTION_BUY_RISK_PER_TRADE_PCT")?,
            option_buy_trade_benchmark_pct: take_float(values, "OPTION_BUY_TRADE_BENCHMARK_PCT")?,
            option_buy_trail_activation_pct: take_float(values, "OPTION_BUY_TRAIL_ACTIVATION_PCT")?,
            option_buy_trail_drawdown_pct: take_float(values, "OPTION_BUY_TRAIL_DRAWDOWN_PCT")?,
            option_intraday_square_off: take_str(values, "OPTION_INTRADAY_SQUARE_OFF")?,
            option_sell_entry_end: take_str(values, "OPTION_SELL_ENTRY_END")?,
            option_sell_entry_start: take_str(values, "OPTION_SELL_ENTRY_START")?,
            option_sell_monthly_benchmark_pct: take_float(
                values,
                "OPTION_SELL_MONTHLY_BENCHMARK_PCT",
            )?,
            option_sell_risk_per_trade_pct: take_float(values, "OPTION_SELL_RISK_PER_TRADE_PCT")?,
            option_sell_short_delta: take_float(values, "OPTION_SELL_SHORT_DELTA")?,
            option_sell_stop_credit_multiple: take_float(
                values,
                "OPTION_SELL_STOP_CREDIT_MULTIPLE",
            )?,
            option_sell_target_capture_pct: take_float(values, "OPTION_SELL_TARGET_CAPTURE_PCT")?,
            option_sell_wing_delta: take_float(values, "OPTION_SELL_WING_DELTA")?,
            option_underlying: take_str(values, "OPTION_UNDERLYING")?,
            port: take_int(values, "PORT")?,
            regime_bootstrap_policy: take_str(values, "REGIME_BOOTSTRAP_POLICY")?,
            regime_breadth_stale_days: take_int(values, "REGIME_BREADTH_STALE_DAYS")?,
            regime_buffer_bps: take_int(values, "REGIME_BUFFER_BPS")?,
            regime_confirm_days: take_int(values, "REGIME_CONFIRM_DAYS")?,
            regime_enabled: take_bool(values, "REGIME_ENABLED")?,
            regime_half_size_factor: take_float(values, "REGIME_HALF_SIZE_FACTOR")?,
            regime_index_stale_days: take_int(values, "REGIME_INDEX_STALE_DAYS")?,
            regime_ltcg_review_days: take_int(values, "REGIME_LTCG_REVIEW_DAYS")?,
            regime_min_breadth_coverage_pct: take_float(values, "REGIME_MIN_BREADTH_COVERAGE_PCT")?,
            regime_mode: take_str(values, "REGIME_MODE")?,
            regime_nearby_rank_band: take_int(values, "REGIME_NEARBY_RANK_BAND")?,
            regime_r4_equity_pct: take_float(values, "REGIME_R4_EQUITY_PCT")?,
            regime_r4_max_residual_names: take_int(values, "REGIME_R4_MAX_RESIDUAL_NAMES")?,
            regime_r4_residual_ordering: take_tuple(values, "REGIME_R4_RESIDUAL_ORDERING")?,
            regime_weekly_eval_weekday: take_int(values, "REGIME_WEEKLY_EVAL_WEEKDAY")?,
            risk_gross_multiple: take_float(values, "RISK_GROSS_MULTIPLE")?,
            risk_max_daily_loss_pct: take_float(values, "RISK_MAX_DAILY_LOSS_PCT")?,
            risk_max_gross_exposure: take_str(values, "RISK_MAX_GROSS_EXPOSURE")?,
            risk_max_orders_per_day: take_int(values, "RISK_MAX_ORDERS_PER_DAY")?,
            risk_max_position_value: take_str(values, "RISK_MAX_POSITION_VALUE")?,
            risk_position_headroom: take_float(values, "RISK_POSITION_HEADROOM")?,
            scan_source_default: take_str(values, "SCAN_SOURCE_DEFAULT")?,
            token_file: take_str(values, "TOKEN_FILE")?,
            db_backend: take_str(values, "DB_BACKEND")?,
            db_schema: take_str(values, "DB_SCHEMA")?,
            screener_dsn: take_str(values, "SCREENER_DSN")?,
            desk_universe_slug: take_str(values, "DESK_UNIVERSE_SLUG")?,
            metrics_port: take_str(values, "METRICS_PORT")?,
            otlp_endpoint: take_str(values, "OTLP_ENDPOINT")?,
            service_name: take_str(values, "SERVICE_NAME")?,
            sentry_dsn: take_str(values, "SENTRY_DSN")?,
        })
    }

    /// Every attribute as a [`ConfigValue`], keyed by its Python name.
    #[must_use]
    pub fn values(&self) -> BTreeMap<&'static str, ConfigValue> {
        let mut out = BTreeMap::new();
        out.insert(
            "FNO_CARRY_ENABLED",
            ConfigValue::Bool(self.fno_carry_enabled),
        );
        out.insert(
            "FNO_F1_EXECUTION_ENABLED",
            ConfigValue::Bool(self.fno_f1_execution_enabled),
        );
        out.insert(
            "FNO_F2_EXECUTION_ENABLED",
            ConfigValue::Bool(self.fno_f2_execution_enabled),
        );
        out.insert("FNO_F3_AUTO_EXIT", ConfigValue::Bool(self.fno_f3_auto_exit));
        out.insert(
            "FNO_F3_EXECUTION_ENABLED",
            ConfigValue::Bool(self.fno_f3_execution_enabled),
        );
        out.insert(
            "FNO_MONITOR_ENABLED",
            ConfigValue::Bool(self.fno_monitor_enabled),
        );
        out.insert(
            "OPTIONS_BOOK_DAILY_LOSS_INR_MAX",
            ConfigValue::Str(self.options_book_daily_loss_inr_max.clone()),
        );
        out.insert(
            "OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX",
            ConfigValue::Str(self.options_book_monthly_loss_inr_max.clone()),
        );
        out.insert(
            "OPTIONS_COLLECT_ENABLED",
            ConfigValue::Bool(self.options_collect_enabled),
        );
        out.insert(
            "OPTIONS_HARD_EXIT_LATEST",
            ConfigValue::Str(self.options_hard_exit_latest.clone()),
        );
        out.insert(
            "OPTIONS_MAX_LOTS_MAX",
            ConfigValue::Int(self.options_max_lots_max),
        );
        out.insert(
            "OPTIONS_MONITOR_ENABLED",
            ConfigValue::Bool(self.options_monitor_enabled),
        );
        out.insert(
            "OPTIONS_O1M_EXECUTION_ENABLED",
            ConfigValue::Bool(self.options_o1m_execution_enabled),
        );
        out.insert(
            "OPTIONS_O1W_EXECUTION_ENABLED",
            ConfigValue::Bool(self.options_o1w_execution_enabled),
        );
        out.insert(
            "OPTIONS_O2_EXECUTION_ENABLED",
            ConfigValue::Bool(self.options_o2_execution_enabled),
        );
        out.insert(
            "OPTIONS_O3_EXECUTION_ENABLED",
            ConfigValue::Bool(self.options_o3_execution_enabled),
        );
        out.insert(
            "OPTIONS_RISK_PCT_MAX",
            ConfigValue::Str(self.options_risk_pct_max.clone()),
        );
        out.insert(
            "OPTIONS_RISK_PER_TRADE_INR_MAX",
            ConfigValue::Str(self.options_risk_per_trade_inr_max.clone()),
        );
        out.insert(
            "OPTIONS_SCAN_ENABLED",
            ConfigValue::Bool(self.options_scan_enabled),
        );
        out.insert(
            "SHARED_READ_LIMITS_REDIS_URL",
            ConfigValue::Str(self.shared_read_limits_redis_url.clone()),
        );
        out.insert(
            "SOLE_BROKER_ACCOUNT_ID",
            ConfigValue::Int(self.sole_broker_account_id),
        );
        out.insert("SOLE_USER_ID", ConfigValue::Int(self.sole_user_id));
        out.insert(
            "SWING_AUTO_EXECUTE",
            ConfigValue::Bool(self.swing_auto_execute),
        );
        out.insert(
            "_EP_PREMARKET",
            ConfigValue::Str(self.ep_premarket_raw.clone()),
        );
        out.insert(
            "SWING_EP_PREMARKET_ENABLED",
            ConfigValue::Bool(self.swing_ep_premarket_enabled),
        );
        out.insert(
            "SWING_EXECUTION_ENABLED",
            ConfigValue::Bool(self.swing_execution_enabled),
        );
        out.insert(
            "SWING_GTT_LIMIT_FRACTION",
            ConfigValue::Float(self.swing_gtt_limit_fraction),
        );
        out.insert(
            "SWING_INDEX_SLUG",
            ConfigValue::Str(self.swing_index_slug.clone()),
        );
        out.insert(
            "SWING_MONITOR_ENABLED",
            ConfigValue::Bool(self.swing_monitor_enabled),
        );
        out.insert(
            "SWING_SCAN_MIN_INTERVAL_SECONDS",
            ConfigValue::Int(self.swing_scan_min_interval_seconds),
        );
        out.insert(
            "SWING_SCAN_STALE_AFTER_SECONDS",
            ConfigValue::Int(self.swing_scan_stale_after_seconds),
        );
        out.insert(
            "SWING_STOP_BAND_MAX",
            ConfigValue::Float(self.swing_stop_band_max),
        );
        out.insert(
            "SWING_STOP_BAND_MIN",
            ConfigValue::Float(self.swing_stop_band_min),
        );
        out.insert(
            "SWING_TELEGRAM_BOT_TOKEN",
            ConfigValue::Str(self.swing_telegram_bot_token.clone()),
        );
        out.insert(
            "SWING_TELEGRAM_CHAT_ID",
            ConfigValue::Str(self.swing_telegram_chat_id.clone()),
        );
        out.insert("TWT_AUTO_EXECUTE", ConfigValue::Bool(self.twt_auto_execute));
        out.insert(
            "TWT_EXECUTION_ENABLED",
            ConfigValue::Bool(self.twt_execution_enabled),
        );
        out.insert(
            "VBT_EXECUTION_ENABLED",
            ConfigValue::Bool(self.vbt_execution_enabled),
        );
        out.insert(
            "VBT_GTT_LIMIT_FRACTION",
            ConfigValue::Float(self.vbt_gtt_limit_fraction),
        );
        out.insert(
            "VBT_STOP_BAND_MAX",
            ConfigValue::Float(self.vbt_stop_band_max),
        );
        out.insert(
            "VBT_STOP_BAND_MIN",
            ConfigValue::Float(self.vbt_stop_band_min),
        );
        out.insert("DB_PATH", ConfigValue::Str(self.db_path.clone()));
        out.insert(
            "DESK_ALLOWED_HOSTS",
            ConfigValue::Str(self.desk_allowed_hosts.clone()),
        );
        out.insert("DESK_DOCS", ConfigValue::Bool(self.desk_docs));
        out.insert(
            "DESK_NOTIFY_FROM",
            ConfigValue::Str(self.desk_notify_from.clone()),
        );
        out.insert(
            "DESK_NOTIFY_TO",
            ConfigValue::Str(self.desk_notify_to.clone()),
        );
        out.insert(
            "DESK_PASSWORD",
            ConfigValue::Str(self.desk_password.clone()),
        );
        out.insert(
            "DESK_PATH_PREFIX",
            ConfigValue::Str(self.desk_path_prefix.clone()),
        );
        out.insert("DESK_PORT", ConfigValue::Int(self.desk_port));
        out.insert(
            "SHARED_READ_LIMITS",
            ConfigValue::Bool(self.shared_read_limits),
        );
        out.insert(
            "DESK_SMTP_HOST",
            ConfigValue::Str(self.desk_smtp_host.clone()),
        );
        out.insert(
            "DESK_SMTP_PASSWORD",
            ConfigValue::Str(self.desk_smtp_password.clone()),
        );
        out.insert("DESK_SMTP_PORT", ConfigValue::Int(self.desk_smtp_port));
        out.insert(
            "DESK_SMTP_STARTTLS",
            ConfigValue::Bool(self.desk_smtp_starttls),
        );
        out.insert(
            "DESK_SMTP_USER",
            ConfigValue::Str(self.desk_smtp_user.clone()),
        );
        out.insert("DRY_RUN", ConfigValue::Bool(self.dry_run));
        out.insert("FORCE_IPV4", ConfigValue::Bool(self.force_ipv4));
        out.insert("FULLY_INVESTED", ConfigValue::Bool(self.fully_invested));
        out.insert(
            "INCLUDE_UNTOUCHABLE_IN_NAV",
            ConfigValue::Bool(self.include_untouchable_in_nav),
        );
        out.insert("INTRADAY_ENABLED", ConfigValue::Bool(self.intraday_enabled));
        out.insert("KITE_API_KEY", ConfigValue::Str(self.kite_api_key.clone()));
        out.insert(
            "KITE_API_SECRET",
            ConfigValue::Str(self.kite_api_secret.clone()),
        );
        out.insert("OPTIONS_ENABLED", ConfigValue::Bool(self.options_enabled));
        out.insert(
            "OPTION_BUY_DELTA",
            ConfigValue::Float(self.option_buy_delta),
        );
        out.insert(
            "OPTION_BUY_ENTRY_END",
            ConfigValue::Str(self.option_buy_entry_end.clone()),
        );
        out.insert(
            "OPTION_BUY_ENTRY_START",
            ConfigValue::Str(self.option_buy_entry_start.clone()),
        );
        out.insert(
            "OPTION_BUY_PREMIUM_STOP_PCT",
            ConfigValue::Float(self.option_buy_premium_stop_pct),
        );
        out.insert(
            "OPTION_BUY_RISK_PER_TRADE_PCT",
            ConfigValue::Float(self.option_buy_risk_per_trade_pct),
        );
        out.insert(
            "OPTION_BUY_TRADE_BENCHMARK_PCT",
            ConfigValue::Float(self.option_buy_trade_benchmark_pct),
        );
        out.insert(
            "OPTION_BUY_TRAIL_ACTIVATION_PCT",
            ConfigValue::Float(self.option_buy_trail_activation_pct),
        );
        out.insert(
            "OPTION_BUY_TRAIL_DRAWDOWN_PCT",
            ConfigValue::Float(self.option_buy_trail_drawdown_pct),
        );
        out.insert(
            "OPTION_INTRADAY_SQUARE_OFF",
            ConfigValue::Str(self.option_intraday_square_off.clone()),
        );
        out.insert(
            "OPTION_SELL_ENTRY_END",
            ConfigValue::Str(self.option_sell_entry_end.clone()),
        );
        out.insert(
            "OPTION_SELL_ENTRY_START",
            ConfigValue::Str(self.option_sell_entry_start.clone()),
        );
        out.insert(
            "OPTION_SELL_MONTHLY_BENCHMARK_PCT",
            ConfigValue::Float(self.option_sell_monthly_benchmark_pct),
        );
        out.insert(
            "OPTION_SELL_RISK_PER_TRADE_PCT",
            ConfigValue::Float(self.option_sell_risk_per_trade_pct),
        );
        out.insert(
            "OPTION_SELL_SHORT_DELTA",
            ConfigValue::Float(self.option_sell_short_delta),
        );
        out.insert(
            "OPTION_SELL_STOP_CREDIT_MULTIPLE",
            ConfigValue::Float(self.option_sell_stop_credit_multiple),
        );
        out.insert(
            "OPTION_SELL_TARGET_CAPTURE_PCT",
            ConfigValue::Float(self.option_sell_target_capture_pct),
        );
        out.insert(
            "OPTION_SELL_WING_DELTA",
            ConfigValue::Float(self.option_sell_wing_delta),
        );
        out.insert(
            "OPTION_UNDERLYING",
            ConfigValue::Str(self.option_underlying.clone()),
        );
        out.insert("PORT", ConfigValue::Int(self.port));
        out.insert(
            "REGIME_BOOTSTRAP_POLICY",
            ConfigValue::Str(self.regime_bootstrap_policy.clone()),
        );
        out.insert(
            "REGIME_BREADTH_STALE_DAYS",
            ConfigValue::Int(self.regime_breadth_stale_days),
        );
        out.insert(
            "REGIME_BUFFER_BPS",
            ConfigValue::Int(self.regime_buffer_bps),
        );
        out.insert(
            "REGIME_CONFIRM_DAYS",
            ConfigValue::Int(self.regime_confirm_days),
        );
        out.insert("REGIME_ENABLED", ConfigValue::Bool(self.regime_enabled));
        out.insert(
            "REGIME_HALF_SIZE_FACTOR",
            ConfigValue::Float(self.regime_half_size_factor),
        );
        out.insert(
            "REGIME_INDEX_STALE_DAYS",
            ConfigValue::Int(self.regime_index_stale_days),
        );
        out.insert(
            "REGIME_LTCG_REVIEW_DAYS",
            ConfigValue::Int(self.regime_ltcg_review_days),
        );
        out.insert(
            "REGIME_MIN_BREADTH_COVERAGE_PCT",
            ConfigValue::Float(self.regime_min_breadth_coverage_pct),
        );
        out.insert("REGIME_MODE", ConfigValue::Str(self.regime_mode.clone()));
        out.insert(
            "REGIME_NEARBY_RANK_BAND",
            ConfigValue::Int(self.regime_nearby_rank_band),
        );
        out.insert(
            "REGIME_R4_EQUITY_PCT",
            ConfigValue::Float(self.regime_r4_equity_pct),
        );
        out.insert(
            "REGIME_R4_MAX_RESIDUAL_NAMES",
            ConfigValue::Int(self.regime_r4_max_residual_names),
        );
        out.insert(
            "REGIME_R4_RESIDUAL_ORDERING",
            ConfigValue::Tuple(self.regime_r4_residual_ordering.clone()),
        );
        out.insert(
            "REGIME_WEEKLY_EVAL_WEEKDAY",
            ConfigValue::Int(self.regime_weekly_eval_weekday),
        );
        out.insert(
            "RISK_GROSS_MULTIPLE",
            ConfigValue::Float(self.risk_gross_multiple),
        );
        out.insert(
            "RISK_MAX_DAILY_LOSS_PCT",
            ConfigValue::Float(self.risk_max_daily_loss_pct),
        );
        out.insert(
            "RISK_MAX_GROSS_EXPOSURE",
            ConfigValue::Str(self.risk_max_gross_exposure.clone()),
        );
        out.insert(
            "RISK_MAX_ORDERS_PER_DAY",
            ConfigValue::Int(self.risk_max_orders_per_day),
        );
        out.insert(
            "RISK_MAX_POSITION_VALUE",
            ConfigValue::Str(self.risk_max_position_value.clone()),
        );
        out.insert(
            "RISK_POSITION_HEADROOM",
            ConfigValue::Float(self.risk_position_headroom),
        );
        out.insert(
            "SCAN_SOURCE_DEFAULT",
            ConfigValue::Str(self.scan_source_default.clone()),
        );
        out.insert("TOKEN_FILE", ConfigValue::Str(self.token_file.clone()));
        out.insert("DB_BACKEND", ConfigValue::Str(self.db_backend.clone()));
        out.insert("DB_SCHEMA", ConfigValue::Str(self.db_schema.clone()));
        out.insert("SCREENER_DSN", ConfigValue::Str(self.screener_dsn.clone()));
        out.insert(
            "DESK_UNIVERSE_SLUG",
            ConfigValue::Str(self.desk_universe_slug.clone()),
        );
        out.insert("METRICS_PORT", ConfigValue::Str(self.metrics_port.clone()));
        out.insert(
            "OTLP_ENDPOINT",
            ConfigValue::Str(self.otlp_endpoint.clone()),
        );
        out.insert("SERVICE_NAME", ConfigValue::Str(self.service_name.clone()));
        out.insert("SENTRY_DSN", ConfigValue::Str(self.sentry_dsn.clone()));
        out
    }
}
