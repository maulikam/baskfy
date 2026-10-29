//! Provider configuration, env prefix `BASKFY_`. Ports `baskfy_providers.settings`.
//!
//! Python's `ProviderSettings` is a pydantic-settings model; this is the same field set, the
//! same defaults, the same constraints and the same sources in the same precedence:
//!
//! 1. explicit values (struct-update syntax over [`ProviderSettings::default`]),
//! 2. process environment variables `BASKFY_<FIELD>` — **case-insensitive**, as
//!    pydantic-settings' default `case_sensitive=False` makes them,
//! 3. dotenv files `.env` then `../../.env` relative to the working directory (a later file
//!    wins), UTF-8,
//! 4. the defaults.
//!
//! Unknown `BASKFY_*` names are ignored (`extra="ignore"`). Every credential defaults to empty
//! so an unconfigured environment yields `CredentialsMissing` — a reported condition — rather
//! than a crash. Credentials are [`Secret`]s: `{:?}` of the settings never shows one.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use crate::py::general;
use crate::secret::Secret;

/// The env prefix.
pub const ENV_PREFIX: &str = "BASKFY_";

/// The dotenv files pydantic-settings reads, relative to the working directory.
pub const ENV_FILES: [&str; 2] = [".env", "../../.env"];

/// Ports `ProviderSettings`: everything the adapters need, and nothing they do not.
#[derive(Debug, Clone, PartialEq)]
pub struct ProviderSettings {
    // --- Kite ---
    /// `kite_api_key`.
    pub kite_api_key: Secret,
    /// `kite_api_secret`.
    pub kite_api_secret: Secret,
    /// Fernet key that encrypts the daily access token at rest.
    pub kite_token_encryption_key: Secret,
    /// Where the encrypted token blob lives.
    pub kite_token_path: String,
    /// Explicit HTTP timeout for Kite (> 0).
    pub kite_request_timeout_seconds: f64,
    /// docs/09: "Rate limit ~ 3 req/s" — the combined ceiling every Kite read waits on (> 0).
    pub kite_rate_limit_per_second: f64,
    /// What a bulk caller may spend of that ceiling (> 0, strictly below it — M85).
    pub kite_bulk_rate_limit_per_second: f64,
    /// docs/09: chunk backfills into <= 2000-day slices (> 0).
    pub kite_max_days_per_request: i64,
    /// docs/09: bounded concurrency <= 3 (> 0).
    pub kite_max_concurrency: i64,

    // --- Kite session bridge ---
    /// `user@host` of the desk; empty disables the pull.
    pub kite_desk_ssh_target: String,
    /// Private key authorised on the desk against a forced command.
    pub kite_desk_ssh_key_path: String,
    /// The desk's pinned host key.
    pub kite_desk_known_hosts_path: String,
    /// Seconds to wait for the desk (> 0).
    pub kite_desk_ssh_timeout_seconds: f64,

    // --- Retry ---
    /// Attempts (>= 1).
    pub provider_max_attempts: i64,
    /// Backoff base (> 0).
    pub provider_backoff_base_seconds: f64,
    /// Backoff cap (> 0).
    pub provider_backoff_max_seconds: f64,

    // --- Circuit breaker ---
    /// Consecutive transient failures that open the circuit (>= 1).
    pub provider_circuit_failure_threshold: i64,
    /// Seconds before a probe is admitted (> 0).
    pub provider_circuit_reset_seconds: f64,

    // --- Redis ---
    /// `redis_url` (a secret: it can carry a password).
    pub redis_url: Secret,

    // --- NSE ---
    /// `nse_base_url`.
    pub nse_base_url: String,
    /// `nse_archive_url`.
    pub nse_archive_url: String,
    /// NSE HTTP timeout (> 0).
    pub nse_request_timeout_seconds: f64,
    /// NSE's public files are rate-sensitive (> 0).
    pub nse_rate_limit_per_second: f64,
    /// Per-symbol NSE reads in flight at once (1..=16); does not raise the request rate.
    pub nse_fetch_concurrency: i64,
    /// How far past `since` the corporate-action window reaches, in days (> 0).
    pub nse_corporate_action_window_days: i64,

    // --- Object storage ---
    /// `s3_endpoint_url`.
    pub s3_endpoint_url: String,
    /// `s3_region`.
    pub s3_region: String,
    /// `s3_bucket`.
    pub s3_bucket: String,
    /// Local raw-archive directory when no bucket is configured (M56).
    pub raw_archive_dir: String,
    /// `s3_access_key_id`.
    pub s3_access_key_id: Secret,
    /// `s3_secret_access_key`.
    pub s3_secret_access_key: Secret,

    // --- Fixtures ---
    /// Overridable so tests can point at a temporary directory.
    pub fixture_dir: String,
}

impl Default for ProviderSettings {
    /// Python's field defaults, unvalidated against any source.
    fn default() -> Self {
        Self {
            kite_api_key: Secret::default(),
            kite_api_secret: Secret::default(),
            kite_token_encryption_key: Secret::default(),
            kite_token_path: ".secrets/kite-token.enc".to_owned(),
            kite_request_timeout_seconds: 30.0,
            kite_rate_limit_per_second: 3.0,
            kite_bulk_rate_limit_per_second: 2.0,
            kite_max_days_per_request: 2000,
            kite_max_concurrency: 3,
            kite_desk_ssh_target: String::new(),
            kite_desk_ssh_key_path: "/var/lib/baskfy/.ssh/kite-session".to_owned(),
            kite_desk_known_hosts_path: "/var/lib/baskfy/.ssh/known_hosts".to_owned(),
            kite_desk_ssh_timeout_seconds: 20.0,
            provider_max_attempts: 5,
            provider_backoff_base_seconds: 0.5,
            provider_backoff_max_seconds: 30.0,
            provider_circuit_failure_threshold: 5,
            provider_circuit_reset_seconds: 60.0,
            redis_url: Secret::new("redis://localhost:6380/0"),
            nse_base_url: "https://www.nseindia.com".to_owned(),
            nse_archive_url: "https://nsearchives.nseindia.com".to_owned(),
            nse_request_timeout_seconds: 30.0,
            nse_rate_limit_per_second: 1.0,
            nse_fetch_concurrency: 4,
            nse_corporate_action_window_days: 120,
            s3_endpoint_url: String::new(),
            s3_region: "auto".to_owned(),
            s3_bucket: String::new(),
            raw_archive_dir: String::new(),
            s3_access_key_id: Secret::default(),
            s3_secret_access_key: Secret::default(),
            fixture_dir: String::new(),
        }
    }
}

/// The settings did not validate (pydantic's `ValidationError`, every field error at once).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{} validation error(s) for ProviderSettings\n{}", .errors.len(), .errors.join("\n"))]
pub struct SettingsError {
    /// One line per failing field: `field: message`. Never contains a credential's value.
    pub errors: Vec<String>,
}

#[derive(Clone, Copy)]
enum Bound {
    Gt0,
    Ge1,
    Ge1Le16,
}

impl ProviderSettings {
    /// Every field name, in Python's declaration order.
    pub const FIELDS: [&'static str; 32] = [
        "kite_api_key",
        "kite_api_secret",
        "kite_token_encryption_key",
        "kite_token_path",
        "kite_request_timeout_seconds",
        "kite_rate_limit_per_second",
        "kite_bulk_rate_limit_per_second",
        "kite_max_days_per_request",
        "kite_max_concurrency",
        "kite_desk_ssh_target",
        "kite_desk_ssh_key_path",
        "kite_desk_known_hosts_path",
        "kite_desk_ssh_timeout_seconds",
        "provider_max_attempts",
        "provider_backoff_base_seconds",
        "provider_backoff_max_seconds",
        "provider_circuit_failure_threshold",
        "provider_circuit_reset_seconds",
        "redis_url",
        "nse_base_url",
        "nse_archive_url",
        "nse_request_timeout_seconds",
        "nse_rate_limit_per_second",
        "nse_fetch_concurrency",
        "nse_corporate_action_window_days",
        "s3_endpoint_url",
        "s3_region",
        "s3_bucket",
        "raw_archive_dir",
        "s3_access_key_id",
        "s3_secret_access_key",
        "fixture_dir",
    ];

    /// `ProviderSettings()`: the process environment over the dotenv files over the defaults.
    ///
    /// # Errors
    /// [`SettingsError`] when a value does not parse or violates its constraint.
    pub fn from_env() -> Result<Self, SettingsError> {
        let files: Vec<PathBuf> = ENV_FILES.iter().map(PathBuf::from).collect();
        Self::from_sources(
            std::env::vars_os()
                .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?))),
            &files,
        )
    }

    /// The settings from an explicit environment and dotenv files — what `from_env` does, with
    /// both sources injected (`ProviderSettings(_env_file=None)` is `env_files = &[]`).
    ///
    /// # Errors
    /// [`SettingsError`] when a value does not parse or violates its constraint.
    pub fn from_sources(
        env: impl IntoIterator<Item = (String, String)>,
        env_files: &[PathBuf],
    ) -> Result<Self, SettingsError> {
        let mut values: HashMap<String, String> = HashMap::new();
        for file in env_files {
            for (k, v) in read_dotenv(file) {
                values.insert(k.to_lowercase(), v);
            }
        }
        for (k, v) in env {
            values.insert(k.to_lowercase(), v);
        }
        let mut settings = Self::default();
        let mut errors = Vec::new();
        let prefix = ENV_PREFIX.to_lowercase();
        for field in Self::FIELDS {
            if let Some(raw) = values.get(&format!("{prefix}{field}"))
                && let Err(e) = settings.set(field, raw)
            {
                errors.push(format!("{field}: {e}"));
            }
        }
        if !errors.is_empty() {
            return Err(SettingsError { errors });
        }
        settings.validate()?;
        Ok(settings)
    }

    /// Sets one field from its environment string (pydantic's lax str coercion).
    fn set(&mut self, field: &str, raw: &str) -> Result<(), String> {
        macro_rules! float {
            ($f:ident) => {
                self.$f = parse_float(raw)?
            };
        }
        macro_rules! int {
            ($f:ident) => {
                self.$f = parse_int(raw)?
            };
        }
        match field {
            "kite_api_key" => self.kite_api_key = Secret::new(raw),
            "kite_api_secret" => self.kite_api_secret = Secret::new(raw),
            "kite_token_encryption_key" => self.kite_token_encryption_key = Secret::new(raw),
            "kite_token_path" => raw.clone_into(&mut self.kite_token_path),
            "kite_request_timeout_seconds" => float!(kite_request_timeout_seconds),
            "kite_rate_limit_per_second" => float!(kite_rate_limit_per_second),
            "kite_bulk_rate_limit_per_second" => float!(kite_bulk_rate_limit_per_second),
            "kite_max_days_per_request" => int!(kite_max_days_per_request),
            "kite_max_concurrency" => int!(kite_max_concurrency),
            "kite_desk_ssh_target" => raw.clone_into(&mut self.kite_desk_ssh_target),
            "kite_desk_ssh_key_path" => raw.clone_into(&mut self.kite_desk_ssh_key_path),
            "kite_desk_known_hosts_path" => raw.clone_into(&mut self.kite_desk_known_hosts_path),
            "kite_desk_ssh_timeout_seconds" => float!(kite_desk_ssh_timeout_seconds),
            "provider_max_attempts" => int!(provider_max_attempts),
            "provider_backoff_base_seconds" => float!(provider_backoff_base_seconds),
            "provider_backoff_max_seconds" => float!(provider_backoff_max_seconds),
            "provider_circuit_failure_threshold" => int!(provider_circuit_failure_threshold),
            "provider_circuit_reset_seconds" => float!(provider_circuit_reset_seconds),
            "redis_url" => self.redis_url = Secret::new(raw),
            "nse_base_url" => raw.clone_into(&mut self.nse_base_url),
            "nse_archive_url" => raw.clone_into(&mut self.nse_archive_url),
            "nse_request_timeout_seconds" => float!(nse_request_timeout_seconds),
            "nse_rate_limit_per_second" => float!(nse_rate_limit_per_second),
            "nse_fetch_concurrency" => int!(nse_fetch_concurrency),
            "nse_corporate_action_window_days" => int!(nse_corporate_action_window_days),
            "s3_endpoint_url" => raw.clone_into(&mut self.s3_endpoint_url),
            "s3_region" => raw.clone_into(&mut self.s3_region),
            "s3_bucket" => raw.clone_into(&mut self.s3_bucket),
            "raw_archive_dir" => raw.clone_into(&mut self.raw_archive_dir),
            "s3_access_key_id" => self.s3_access_key_id = Secret::new(raw),
            "s3_secret_access_key" => self.s3_secret_access_key = Secret::new(raw),
            "fixture_dir" => raw.clone_into(&mut self.fixture_dir),
            _ => {}
        }
        Ok(())
    }

    /// The field constraints, then `_bulk_leaves_headroom` (run only when the fields pass, as
    /// pydantic's after-validator is).
    ///
    /// # Errors
    /// [`SettingsError`] listing every violated constraint.
    pub fn validate(&self) -> Result<(), SettingsError> {
        let floats: [(&str, f64); 9] = [
            (
                "kite_request_timeout_seconds",
                self.kite_request_timeout_seconds,
            ),
            (
                "kite_rate_limit_per_second",
                self.kite_rate_limit_per_second,
            ),
            (
                "kite_bulk_rate_limit_per_second",
                self.kite_bulk_rate_limit_per_second,
            ),
            (
                "kite_desk_ssh_timeout_seconds",
                self.kite_desk_ssh_timeout_seconds,
            ),
            (
                "provider_backoff_base_seconds",
                self.provider_backoff_base_seconds,
            ),
            (
                "provider_backoff_max_seconds",
                self.provider_backoff_max_seconds,
            ),
            (
                "provider_circuit_reset_seconds",
                self.provider_circuit_reset_seconds,
            ),
            (
                "nse_request_timeout_seconds",
                self.nse_request_timeout_seconds,
            ),
            ("nse_rate_limit_per_second", self.nse_rate_limit_per_second),
        ];
        let ints: [(&str, i64, Bound); 6] = [
            (
                "kite_max_days_per_request",
                self.kite_max_days_per_request,
                Bound::Gt0,
            ),
            (
                "kite_max_concurrency",
                self.kite_max_concurrency,
                Bound::Gt0,
            ),
            (
                "provider_max_attempts",
                self.provider_max_attempts,
                Bound::Ge1,
            ),
            (
                "provider_circuit_failure_threshold",
                self.provider_circuit_failure_threshold,
                Bound::Ge1,
            ),
            (
                "nse_fetch_concurrency",
                self.nse_fetch_concurrency,
                Bound::Ge1Le16,
            ),
            (
                "nse_corporate_action_window_days",
                self.nse_corporate_action_window_days,
                Bound::Gt0,
            ),
        ];
        let mut errors = Vec::new();
        for (name, value) in floats {
            // pydantic's `gt=0`: NaN fails too.
            if value <= 0.0 || value.is_nan() {
                errors.push(format!("{name}: Input should be greater than 0"));
            }
        }
        for (name, value, bound) in ints {
            match bound {
                Bound::Gt0 if value <= 0 => {
                    errors.push(format!("{name}: Input should be greater than 0"));
                }
                Bound::Ge1 | Bound::Ge1Le16 if value < 1 => {
                    errors.push(format!(
                        "{name}: Input should be greater than or equal to 1"
                    ));
                }
                Bound::Ge1Le16 if value > 16 => {
                    errors.push(format!("{name}: Input should be less than or equal to 16"));
                }
                _ => {}
            }
        }
        if !errors.is_empty() {
            return Err(SettingsError { errors });
        }
        if self.kite_bulk_rate_limit_per_second >= self.kite_rate_limit_per_second {
            return Err(SettingsError {
                errors: vec![format!(
                    "Value error, BASKFY_KITE_BULK_RATE_LIMIT_PER_SECOND ({}) must be strictly \
                     below BASKFY_KITE_RATE_LIMIT_PER_SECOND ({}); the difference is the \
                     headroom a login-time read depends on",
                    general(self.kite_bulk_rate_limit_per_second),
                    general(self.kite_rate_limit_per_second)
                )],
            });
        }
        Ok(())
    }

    /// `kite_configured`: both the API key and secret are set.
    #[must_use]
    pub fn kite_configured(&self) -> bool {
        self.kite_api_key.is_set() && self.kite_api_secret.is_set()
    }

    /// `s3_configured`: bucket, key id and secret are all set.
    #[must_use]
    pub fn s3_configured(&self) -> bool {
        !self.s3_bucket.is_empty()
            && self.s3_access_key_id.is_set()
            && self.s3_secret_access_key.is_set()
    }

    /// `token_encryption_configured`.
    #[must_use]
    pub fn token_encryption_configured(&self) -> bool {
        self.kite_token_encryption_key.is_set()
    }

    /// `desk_session_pull_configured`: target and key both set — a target with no key would
    /// hang on a password prompt, and a nightly job that hangs is worse than one that skips.
    #[must_use]
    pub fn desk_session_pull_configured(&self) -> bool {
        !self.kite_desk_ssh_target.is_empty() && !self.kite_desk_ssh_key_path.is_empty()
    }
}

/// pydantic's lax `str` → `float`: surrounding whitespace ignored, Python's `float()` spellings
/// (`3`, `3.0`, `1e3`, `inf`, `nan`), digit-separating underscores.
fn parse_float(raw: &str) -> Result<f64, String> {
    let text = raw.trim();
    let cleaned = strip_underscores(text).ok_or_else(float_error)?;
    let lower = cleaned.to_ascii_lowercase();
    let body = lower.trim_start_matches(['+', '-']);
    if matches!(body, "inf" | "infinity" | "nan") {
        return cleaned.parse::<f64>().map_err(|_| float_error());
    }
    if body.is_empty()
        || !body
            .chars()
            .all(|c| c.is_ascii_digit() || matches!(c, '.' | 'e' | '+' | '-'))
    {
        return Err(float_error());
    }
    cleaned.parse::<f64>().map_err(|_| float_error())
}

fn float_error() -> String {
    "Input should be a valid number, unable to parse string as a number".to_owned()
}

/// pydantic's lax `str` → `int`: whitespace ignored, an optional sign, digits (underscores
/// between them), and a `.0…` tail.
fn parse_int(raw: &str) -> Result<i64, String> {
    let error =
        || "Input should be a valid integer, unable to parse string as an integer".to_owned();
    let text = raw.trim();
    let cleaned = strip_underscores(text).ok_or_else(error)?;
    let (whole, frac) = cleaned.split_once('.').unwrap_or((&cleaned, ""));
    if !frac.chars().all(|c| c == '0') {
        return Err(
            "Input should be a valid integer, got a number with a fractional part".to_owned(),
        );
    }
    let digits = whole.trim_start_matches(['+', '-']);
    if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
        return Err(error());
    }
    whole.parse::<i64>().map_err(|_| error())
}

/// Removes underscores that sit between two digits; any other underscore is a parse error.
fn strip_underscores(text: &str) -> Option<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    for (i, c) in chars.iter().enumerate() {
        if *c == '_' {
            let before = i.checked_sub(1).and_then(|j| chars.get(j));
            let after = chars.get(i + 1);
            if !(before.is_some_and(char::is_ascii_digit)
                && after.is_some_and(char::is_ascii_digit))
            {
                return None;
            }
            continue;
        }
        out.push(*c);
    }
    Some(out)
}

/// python-dotenv's `dotenv_values` for the forms a `.env` here uses: `KEY=VALUE`, an optional
/// `export ` prefix, `#` comments (whole-line, or after whitespace on an unquoted value),
/// single- or double-quoted values (double quotes honour `\n`, `\"`, `\\`). A missing or
/// unreadable file contributes nothing, as pydantic-settings skips it. `${VAR}` interpolation is
/// not performed (DECISIONS-RS R4.7).
#[must_use]
pub fn read_dotenv(path: &Path) -> Vec<(String, String)> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    parse_dotenv(&text)
}

/// The parser behind [`read_dotenv`].
#[must_use]
pub fn parse_dotenv(text: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim_start_matches('\u{feff}').trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line).trim_start();
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        out.push((key.to_owned(), dotenv_value(value.trim())));
    }
    out
}

fn dotenv_value(value: &str) -> String {
    if let Some(rest) = value.strip_prefix('\'')
        && let Some(end) = rest.find('\'')
    {
        return rest[..end].to_owned();
    }
    if let Some(rest) = value.strip_prefix('"') {
        let mut out = String::new();
        let mut chars = rest.chars();
        while let Some(c) = chars.next() {
            match c {
                '"' => return out,
                '\\' => match chars.next() {
                    Some('n') => out.push('\n'),
                    Some('t') => out.push('\t'),
                    Some('r') => out.push('\r'),
                    Some(other) => out.push(other),
                    None => out.push('\\'),
                },
                other => out.push(other),
            }
        }
        return format!("\"{rest}");
    }
    match value.find(" #") {
        Some(i) => value[..i].trim_end().to_owned(),
        None => value.to_owned(),
    }
}

/// `get_provider_settings()`: the process's settings, read once. A failure is not cached (as
/// `lru_cache` does not cache an exception), so a fixed environment is picked up on retry.
///
/// # Errors
/// [`SettingsError`] from [`ProviderSettings::from_env`].
pub fn get_provider_settings() -> Result<&'static ProviderSettings, SettingsError> {
    static CELL: OnceLock<ProviderSettings> = OnceLock::new();
    if let Some(s) = CELL.get() {
        return Ok(s);
    }
    let loaded = ProviderSettings::from_env()?;
    Ok(CELL.get_or_init(|| loaded))
}
