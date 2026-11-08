//! Runtime settings, env prefix `BASKFY_`. Ports `baskfy_api.settings`.
//!
//! The same 160 fields as Python's `Settings`, with the same names, defaults and `Field`
//! constraints, read from the same sources in the same precedence as pydantic-settings
//! (DECISIONS-RS R4.7, reused here):
//!
//! 1. explicit values (struct-update syntax over [`Settings::default`]),
//! 2. process environment `BASKFY_<FIELD>`, matched case-insensitively,
//! 3. the dotenv files `.env` then `../../.env` relative to the working directory (the later
//!    file wins),
//! 4. the defaults.
//!
//! Values are coerced as pydantic's lax mode coerces an environment string: booleans from
//! `true/false/1/0/yes/no/on/off/t/f/y/n`, integers allowing `_` separators and a `.0` tail,
//! `Decimal`s with their scale kept, a `tuple[str, ...]` from a JSON array (pydantic-settings
//! decodes complex fields with `json.loads`), a `Literal` only from one of its values.
//!
//! Credentials (and the two URLs, which carry passwords) are [`Secret`]s: `{:?}` of the settings
//! never shows one. The field table below was generated once from `Settings.model_fields`;
//! `tests/settings.rs` holds the field list equal to Python's so a new Python setting is noticed.

use std::collections::HashMap;
use std::path::PathBuf;

use baskfy_providers::secret::Secret;
use baskfy_providers::settings::read_dotenv;
use chrono::NaiveTime;
use rust_decimal::Decimal;

/// docs/07: "Base: `/api/v1`". Ports `API_PREFIX`.
pub const API_PREFIX: &str = "/api/v1";

/// docs/11 §Security: "JWT: HS256". Ports `JWT_ALGORITHM`.
pub const JWT_ALGORITHM: &str = "HS256";

/// RFC 7518 §3.2's minimum HS256 key length in bytes. Ports `MIN_JWT_SECRET_BYTES`.
pub const MIN_JWT_SECRET_BYTES: usize = 32;

/// The env prefix.
pub const ENV_PREFIX: &str = "BASKFY_";

/// The dotenv files pydantic-settings reads, relative to the working directory.
pub const ENV_FILES: [&str; 2] = [".env", "../../.env"];

/// The settings did not validate: every field error at once, then the model validators.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{} validation error(s) for Settings\n{}", .errors.len(), .errors.join("\n"))]
pub struct SettingsError {
    /// One line per failing field (`field: message`) or validator. Never a credential's value.
    pub errors: Vec<String>,
}

fn dec(text: &str) -> Decimal {
    Decimal::from_str_exact(text).unwrap_or_default()
}

/// A comparison a `Field` constraint makes against a `Decimal` bound.
#[derive(Debug, Clone, Copy)]
enum Cmp {
    Gt,
    Ge,
    Le,
}

/// A `Field` constraint on an `int` (`Gt`, `Ge`, `Le`) or a `float` (`GtF`, `GeF`, `LeF`).
#[derive(Debug, Clone, Copy)]
enum Bound {
    Gt(i64),
    Ge(i64),
    Le(i64),
    GtF(f64),
    GeF(f64),
    LeF(f64),
}

mod bound {
    //! pydantic's constraint messages (`Input should be greater than 0`, ...).
    use super::{Bound, Cmp, Decimal, dec};

    pub(super) fn int(errors: &mut Vec<String>, field: &str, value: i64, bounds: &[Bound]) {
        for b in bounds {
            let failed = match *b {
                Bound::Gt(x) => (value <= x).then(|| format!("greater than {x}")),
                Bound::Ge(x) => (value < x).then(|| format!("greater than or equal to {x}")),
                Bound::Le(x) => (value > x).then(|| format!("less than or equal to {x}")),
                _ => None,
            };
            if let Some(what) = failed {
                errors.push(format!("{field}: Input should be {what}"));
            }
        }
    }

    pub(super) fn float(errors: &mut Vec<String>, field: &str, value: f64, bounds: &[Bound]) {
        for b in bounds {
            let r = baskfy_core::py::float_repr;
            let failed = match *b {
                Bound::GtF(x) => {
                    (value.is_nan() || value <= x).then(|| format!("greater than {}", r(x)))
                }
                Bound::GeF(x) => (value.is_nan() || value < x)
                    .then(|| format!("greater than or equal to {}", r(x))),
                Bound::LeF(x) => {
                    (value.is_nan() || value > x).then(|| format!("less than or equal to {}", r(x)))
                }
                _ => None,
            };
            if let Some(what) = failed {
                errors.push(format!("{field}: Input should be {what}"));
            }
        }
    }

    pub(super) fn decimal(
        errors: &mut Vec<String>,
        field: &str,
        value: Decimal,
        bounds: &[(Cmp, &str)],
    ) {
        for (cmp, text) in bounds {
            let x = dec(text);
            let failed = match cmp {
                Cmp::Gt => (value <= x).then(|| format!("greater than {text}")),
                Cmp::Ge => (value < x).then(|| format!("greater than or equal to {text}")),
                Cmp::Le => (value > x).then(|| format!("less than or equal to {text}")),
            };
            if let Some(what) = failed {
                errors.push(format!("{field}: Input should be {what}"));
            }
        }
    }
}

mod parse {
    //! pydantic's lax coercion of one environment string.
    use super::{Decimal, NaiveTime};

    pub(super) fn boolean(raw: &str) -> Result<bool, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "1" | "on" | "t" | "true" | "y" | "yes" => Ok(true),
            "0" | "off" | "f" | "false" | "n" | "no" => Ok(false),
            _ => Err("Input should be a valid boolean, unable to interpret input".to_owned()),
        }
    }

    /// Python's `int()` digit grouping: `_` only between digits.
    fn digits(text: &str) -> Option<String> {
        let body = text.strip_prefix(['+', '-']).unwrap_or(text);
        if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
            return None;
        }
        if !body.chars().all(|c| c.is_ascii_digit() || c == '_') {
            return None;
        }
        Some(text.replace('_', ""))
    }

    pub(super) fn integer(raw: &str) -> Result<i64, String> {
        let text = raw.trim();
        let whole = match text.split_once('.') {
            Some((w, frac)) if !frac.is_empty() && frac.chars().all(|c| c == '0') => w,
            Some(_) => {
                return Err(
                    "Input should be a valid integer, unable to parse string as an integer"
                        .to_owned(),
                );
            }
            None => text,
        };
        digits(whole)
            .and_then(|d| d.parse::<i64>().ok())
            .ok_or_else(|| {
                "Input should be a valid integer, unable to parse string as an integer".to_owned()
            })
    }

    pub(super) fn float(raw: &str) -> Result<f64, String> {
        let text = raw.trim().replace('_', "");
        let lowered = text.to_ascii_lowercase();
        let parsed = match lowered.trim_start_matches(['+', '-']) {
            "inf" | "infinity" | "nan" => {
                let v = if lowered.contains("nan") {
                    f64::NAN
                } else {
                    f64::INFINITY
                };
                Some(if lowered.starts_with('-') { -v } else { v })
            }
            _ => text.parse::<f64>().ok(),
        };
        parsed.ok_or_else(|| {
            "Input should be a valid number, unable to parse string as a number".to_owned()
        })
    }

    pub(super) fn decimal(raw: &str) -> Result<Decimal, String> {
        let text = raw.trim();
        Decimal::from_str_exact(text)
            .or_else(|_| Decimal::from_scientific(text))
            .map_err(|_| "Input should be a valid decimal".to_owned())
    }

    pub(super) fn time(raw: &str) -> Result<NaiveTime, String> {
        let text = raw.trim();
        ["%H:%M:%S%.f", "%H:%M:%S", "%H:%M"]
            .iter()
            .find_map(|fmt| NaiveTime::parse_from_str(text, fmt).ok())
            .ok_or_else(|| "Input should be in a valid time format".to_owned())
    }

    pub(super) fn str_tuple(raw: &str) -> Result<Vec<String>, String> {
        let value: serde_json::Value =
            serde_json::from_str(raw).map_err(|_| "error parsing value (not JSON)".to_owned())?;
        let serde_json::Value::Array(items) = value else {
            return Err("Input should be a valid tuple".to_owned());
        };
        items
            .into_iter()
            .map(|v| match v {
                serde_json::Value::String(s) => Ok(s),
                _ => Err("Input should be a valid string".to_owned()),
            })
            .collect()
    }

    pub(super) fn literal(raw: &str, allowed: &[&str]) -> Result<String, String> {
        if allowed.contains(&raw) {
            Ok(raw.to_owned())
        } else {
            let quoted: Vec<String> = allowed.iter().map(|a| format!("'{a}'")).collect();
            let (head, last) = quoted.split_at(quoted.len().saturating_sub(1));
            Err(format!(
                "Input should be {} or {}",
                head.join(", "),
                last.join("")
            ))
        }
    }
}

impl Settings {
    /// `Settings()`: the process environment over the dotenv files over the defaults, then
    /// validated.
    ///
    /// # Errors
    /// [`SettingsError`] for a value that does not parse, a violated constraint, or a model
    /// validator (`_smtp_credentials_are_coherent`).
    pub fn from_env() -> Result<Self, SettingsError> {
        let files: Vec<PathBuf> = ENV_FILES.iter().map(PathBuf::from).collect();
        Self::from_sources(
            std::env::vars_os()
                .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?))),
            &files,
        )
    }

    /// The settings from an explicit environment and dotenv files.
    ///
    /// # Errors
    /// As [`Settings::from_env`].
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

    /// The field constraints, then `_smtp_credentials_are_coherent` (run only when the fields
    /// pass, as pydantic's after-validator is).
    ///
    /// # Errors
    /// [`SettingsError`].
    pub fn validate(&self) -> Result<(), SettingsError> {
        let errors = self.field_errors();
        if !errors.is_empty() {
            return Err(SettingsError { errors });
        }
        if self.email_transport == "smtp" {
            if self.smtp_username.is_empty() != !self.smtp_password.is_set() {
                return Err(SettingsError {
                    errors: vec![
                        "BASKFY_SMTP_USERNAME and BASKFY_SMTP_PASSWORD must be set together"
                            .to_owned(),
                    ],
                });
            }
            if !self.smtp_username.is_empty() && !self.smtp_starttls {
                return Err(SettingsError {
                    errors: vec![
                        "BASKFY_SMTP_STARTTLS is false while SMTP credentials are set; that \
                         would send the relay password in the clear"
                            .to_owned(),
                    ],
                });
            }
        }
        Ok(())
    }

    /// Ports `require_configured`: refuse to serve production with development defaults.
    ///
    /// # Errors
    /// The first unmet production requirement, in Python's order and wording.
    pub fn require_configured(&self) -> Result<(), String> {
        if self.environment != "production" {
            return Ok(());
        }
        let fail = |m: &str| Err(m.to_owned());
        if !self.jwt_secret.is_set() {
            return fail(
                "BASKFY_JWT_SECRET is empty in production; the API could not verify any token",
            );
        }
        if self.jwt_secret.expose().len() < MIN_JWT_SECRET_BYTES {
            return Err(format!(
                "BASKFY_JWT_SECRET is shorter than the {MIN_JWT_SECRET_BYTES} bytes RFC 7518 §3.2 \
                 requires for HS256"
            ));
        }
        if !self.cookie_secure {
            return fail("BASKFY_COOKIE_SECURE is false in production");
        }
        if self.email_transport == "console" {
            return fail(
                "BASKFY_EMAIL_TRANSPORT is 'console' in production; no mail would be delivered",
            );
        }
        if self.email_transport == "resend" && !self.resend_api_key.is_set() {
            return fail("BASKFY_RESEND_API_KEY is empty but the transport is 'resend'");
        }
        if self.google_client_id.is_empty() {
            return fail("BASKFY_GOOGLE_CLIENT_ID is empty in production; nobody could sign in");
        }
        if self.allowed_logins().is_empty() {
            return fail(
                "BASKFY_LOGIN_ALLOWLIST is empty in production; nobody outside an explicit list \
                 may hold a session on this deployment",
            );
        }
        if !self.razorpay_webhook_secret.is_set() {
            return fail(
                "BASKFY_RAZORPAY_WEBHOOK_SECRET is empty in production; no webhook could be verified",
            );
        }
        if self.razorpay_key_id.is_empty() || !self.razorpay_key_secret.is_set() {
            return fail("BASKFY_RAZORPAY_KEY_ID/SECRET are empty in production");
        }
        if self.supplier_gstin.is_empty() {
            return fail("BASKFY_SUPPLIER_GSTIN is empty in production; invoices need it");
        }
        if self.supplier_state.is_empty() {
            return fail("BASKFY_SUPPLIER_STATE is empty in production; place of supply needs it");
        }
        Ok(())
    }

    fn csv_set(text: &str) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for part in text.split(',') {
            let p = part.trim().to_lowercase();
            if !p.is_empty() && !out.contains(&p) {
                out.push(p);
            }
        }
        out
    }

    /// Ports `allowed_logins`: the login allowlist, lower-cased and trimmed (empty = no limit).
    #[must_use]
    pub fn allowed_logins(&self) -> Vec<String> {
        Self::csv_set(&self.login_allowlist)
    }

    /// Ports `login_permitted`.
    #[must_use]
    pub fn login_permitted(&self, email: &str) -> bool {
        let allowed = self.allowed_logins();
        allowed.is_empty() || allowed.contains(&email.trim().to_lowercase())
    }

    /// Ports `allowed_staff`: the staff allowlist (empty = nobody auto-promoted).
    #[must_use]
    pub fn allowed_staff(&self) -> Vec<String> {
        Self::csv_set(&self.staff_allowlist)
    }

    /// Ports `is_staff_email`.
    #[must_use]
    pub fn is_staff_email(&self, email: &str) -> bool {
        self.allowed_staff().contains(&email.trim().to_lowercase())
    }

    /// Ports `payments_configured`.
    #[must_use]
    pub fn payments_configured(&self) -> bool {
        !self.razorpay_key_id.is_empty() && self.razorpay_key_secret.is_set()
    }

    /// The pool settings `create_engine` reads.
    #[must_use]
    pub fn db_settings(&self) -> baskfy_db::DbSettings {
        baskfy_db::DbSettings {
            url: self.database_url.clone(),
            pool_size: u32::try_from(self.db_pool_size).unwrap_or(1),
            max_overflow: u32::try_from(self.db_max_overflow).unwrap_or(0),
            pool_timeout_seconds: self.db_pool_timeout_seconds,
            pool_recycle_seconds: u64::try_from(self.db_pool_recycle_seconds).unwrap_or(1800),
            statement_cache_size: usize::try_from(self.db_statement_cache_size).unwrap_or(0),
        }
    }
}

// @generated-from: baskfy_api.settings.Settings.model_fields (see the module docs). Edit by hand
// when settings.py changes; tests/settings.rs holds the field list equal to Python's.

/// Ports `baskfy_api.settings.Settings`: every field, its default and its constraint.
#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    /// `BASKFY_DATABASE_URL` (Python default `'postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy'`).
    ///
    /// SQLAlchemy async URL, e.g. postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy
    pub database_url: Secret,
    /// `BASKFY_REDIS_URL` (Python default `'redis://localhost:6380/0'`).
    pub redis_url: Secret,
    /// `BASKFY_BACKTEST_EXECUTOR` (Python default `'inline'`).
    ///
    /// Where a backtest actually runs (M42).  ``inline`` runs it in this process, in a bounded pool, and is the default because it is what the work is worth: a nine-year monthly run measures about two seconds, nearly all of it waiting on Postgres. It is also the only mode with no way to leave a run queued for ever, which is the failure this default exists to remove — see `backtest_runner`.  ``celery`` publishes to the broker instead, for a deployment that wants the simulation off the web process. Choosing it means running a worker on the ``backtest`` queue.
    pub backtest_executor: String,
    /// `BASKFY_BACKTEST_CONCURRENCY` (Python default `2`).
    ///
    /// How many backtests one API process runs at once. See `backtest_runner.DEFAULT_CONCURRENCY`.
    pub backtest_concurrency: i64,
    /// `BASKFY_TEST_DATABASE_URL` (Python default `None`).
    ///
    /// Used only by the `db`-marked test suite; unset means those tests skip.
    pub test_database_url: Option<Secret>,
    /// `BASKFY_ENVIRONMENT` (Python default `'local'`).
    pub environment: String,
    /// `BASKFY_JWT_SECRET` (Python default `''`).
    ///
    /// Shared with the Next.js app, which mints the access tokens (docs/07). Empty in `local` means "no token can be verified", so every request is anonymous — which is the honest behaviour for a developer who has not configured auth, and is refused outright in production by :meth:`require_configured`.
    pub jwt_secret: Secret,
    /// `BASKFY_JWT_ISSUER` (Python default `None`).
    pub jwt_issuer: Option<String>,
    /// `BASKFY_JWT_AUDIENCE` (Python default `None`).
    pub jwt_audience: Option<String>,
    /// `BASKFY_JWT_MAX_LIFETIME_SECONDS` (Python default `900`).
    ///
    /// docs/11: "15-min access". Tokens are verified, not issued, here; this is the ceiling we accept, so a web app that mints a year-long token is still refused.
    pub jwt_max_lifetime_seconds: i64,
    /// `BASKFY_JWT_LEEWAY_SECONDS` (Python default `30`).
    ///
    /// Clock skew allowed on `exp`/`nbf`, in seconds.
    pub jwt_leeway_seconds: i64,
    /// `BASKFY_ACCESS_TOKEN_TTL_SECONDS` (Python default `900`).
    ///
    /// docs/11: "15-min access". The API now *issues* as well as verifies, so this is the lifetime it mints — kept below `jwt_max_lifetime_seconds`, which is the ceiling it accepts.
    pub access_token_ttl_seconds: i64,
    /// `BASKFY_REFRESH_TOKEN_TTL_DAYS` (Python default `30`).
    ///
    /// How long a refresh-token family lives before a fresh login is required.
    pub refresh_token_ttl_days: i64,
    /// `BASKFY_GOOGLE_CLIENT_ID` (Python default `''`).
    ///
    /// The OAuth client the web app authenticates against, and therefore the **only** `aud` this API accepts on a Google ID token. Empty means Google sign-in is off: `POST /auth/google` answers 401 rather than verifying against a wildcard, because an ID token checked without an audience is a token minted for somebody else's application that we accepted as ours.
    pub google_client_id: String,
    /// `BASKFY_GOOGLE_CLIENT_SECRET` (Python default `''`).
    ///
    /// Held by the web app for the code exchange. The API never uses it — it verifies an ID token signature against Google's public JWKS and needs no secret to do so. It lives here only so one env file describes the whole client.
    pub google_client_secret: Secret,
    /// `BASKFY_LOGIN_ALLOWLIST` (Python default `''`).
    ///
    /// Comma-separated addresses allowed to hold a session. **Empty means open** — every verified Google account may sign in, which is the behaviour before M60 and the right default for a deployment that has decided to be public.  Set it and the deployment becomes single-tenant by configuration rather than by code: only these addresses can sign in *or* rotate a refresh token, so an existing session belonging to someone since removed dies at its next rotation rather than living out its full length. Google has already proved the address by the time this is consulted, so this is authorisation, never authentication.  A settings value and not a hard-coded constant on purpose: who may use a deployment is a property of the deployment, and changing it must not need a rebuild.
    pub login_allowlist: String,
    /// `BASKFY_STAFF_ALLOWLIST`.
    ///
    /// Comma-separated addresses granted ``app_user.is_staff`` on Google sign-in and on ``GET /me``. Unlike :attr:`login_allowlist`, **empty means nobody is auto-promoted** — the list grants, it does not revoke, and it does not mean "everyone is staff".  The default is the founder's own addresses so a rebuilt database does not need a one-off ``UPDATE app_user SET is_staff``. An address already marked staff stays staff even if it is not on this list. Extra operators are added the same way as the login allowlist: comma-separated, case-insensitive, a setting rather than a rebuild.
    pub staff_allowlist: String,
    /// `BASKFY_KITE_PUBLISHER_API_KEY` (Python default `''`).
    ///
    /// The **Publisher** app's api key — not Kite Connect's. It is not a secret: it travels in the form the user's browser posts to `kite.zerodha.com/connect/basket`, and Zerodha's own embed snippets put it in page source. Empty means the hand-off is off and the button is not rendered — never "post without a key", which would send the user to an error page in Kite.  This buys no API access of any kind. Publisher hands a basket to the user's Kite session for them to review and confirm; there is no session token, no holdings read, and no order this system can place. Kite **Connect** is the paid product that does those, and its credentials are `BASKFY_KITE_API_KEY` / `BASKFY_KITE_API_SECRET`, which are a different app and a different thing entirely.
    pub kite_publisher_api_key: String,
    /// `BASKFY_OTP_LENGTH` (Python default `6`).
    ///
    /// docs/11: "OTP login as the default path". Six digits, ten minutes, five guesses.
    pub otp_length: i64,
    /// `BASKFY_OTP_TTL_SECONDS` (Python default `600`).
    pub otp_ttl_seconds: i64,
    /// `BASKFY_OTP_MAX_ATTEMPTS` (Python default `5`).
    pub otp_max_attempts: i64,
    /// `BASKFY_PASSWORD_RESET_TTL_SECONDS` (Python default `3600`).
    pub password_reset_ttl_seconds: i64,
    /// `BASKFY_EMAIL_VERIFICATION_TTL_SECONDS` (Python default `86400`).
    pub email_verification_ttl_seconds: i64,
    /// `BASKFY_AUTH_MAX_FAILURES` (Python default `10`).
    ///
    /// docs/11: "account lockout after 10 failures with email notification".
    pub auth_max_failures: i64,
    /// `BASKFY_AUTH_LOCKOUT_MINUTES` (Python default `15`).
    pub auth_lockout_minutes: i64,
    /// `BASKFY_AUTH_FAILURE_WINDOW_MINUTES` (Python default `60`).
    ///
    /// The window failures are counted over. Older failures are forgiven rather than accumulated forever, so a wrong password last month plus nine today is not a lockout.
    pub auth_failure_window_minutes: i64,
    /// `BASKFY_ARGON2_TIME_COST` (Python default `2`).
    ///
    /// Argon2id parameters. OWASP's 2024 baseline for interactive logins: 19 MiB, 2 passes, 1 lane. Configurable because the right memory cost depends on the box, not on the code.
    pub argon2_time_cost: i64,
    /// `BASKFY_ARGON2_MEMORY_KIB` (Python default `19456`).
    pub argon2_memory_kib: i64,
    /// `BASKFY_ARGON2_PARALLELISM` (Python default `1`).
    pub argon2_parallelism: i64,
    /// `BASKFY_COOKIE_SECURE` (Python default `True`).
    ///
    /// docs/11: "httpOnly, `SameSite=Lax`, **Secure** cookie". False only for plain-HTTP local development, where a Secure cookie is never sent back at all.
    pub cookie_secure: bool,
    /// `BASKFY_COOKIE_DOMAIN` (Python default `None`).
    pub cookie_domain: Option<String>,
    /// `BASKFY_ACCOUNT_PURGE_AFTER_DAYS` (Python default `7`).
    ///
    /// Prompt 12 §5: "a 7-day soft-delete window".
    pub account_purge_after_days: i64,
    /// `BASKFY_EMAIL_TRANSPORT` (Python default `'console'`).
    ///
    /// "resend" in a deployment, "smtp" for mailpit, "console" when neither is configured.
    pub email_transport: String,
    /// `BASKFY_RESEND_API_KEY` (Python default `''`).
    pub resend_api_key: Secret,
    /// `BASKFY_EMAIL_FROM` (Python default `'Baskfy <no-reply@baskfy.com>'`).
    pub email_from: String,
    /// `BASKFY_EMAIL_REPLY_TO` (Python default `None`).
    pub email_reply_to: Option<String>,
    /// `BASKFY_SMTP_HOST` (Python default `'localhost'`).
    pub smtp_host: String,
    /// `BASKFY_SMTP_PORT` (Python default `1025`).
    pub smtp_port: i64,
    /// `BASKFY_SMTP_USERNAME` (Python default `''`).
    pub smtp_username: String,
    /// `BASKFY_SMTP_PASSWORD` (Python default `''`).
    pub smtp_password: Secret,
    /// `BASKFY_SMTP_STARTTLS` (Python default `False`).
    ///
    /// STARTTLS on the existing port (587 for SES). Not implicit TLS — SES's 465 is a different endpoint mode, and 587 + STARTTLS is what AWS documents first.
    pub smtp_starttls: bool,
    /// `BASKFY_WEB_ORIGIN` (Python default `'http://localhost:3000'`).
    ///
    /// The web app's origin, for the links in emails.
    pub web_origin: String,
    /// `BASKFY_SUPPORT_EMAIL` (Python default `'support@baskfy.com'`).
    ///
    /// Where `POST /support` delivers (Prompt 18 §2). A setting, not a constant, so a deployment can route it at a real inbox while every test and every local run sends it to the console transport or to mailpit.
    pub support_email: String,
    /// `BASKFY_RAZORPAY_API_BASE` (Python default `'https://api.razorpay.com/v1'`).
    ///
    /// Razorpay's REST base. A setting so a test can point it at a local stub and so nothing in this repository can accidentally reach the live gateway (`network_guard.py` blocks it outright in the suite).
    pub razorpay_api_base: String,
    /// `BASKFY_RAZORPAY_KEY_ID` (Python default `''`).
    pub razorpay_key_id: String,
    /// `BASKFY_RAZORPAY_KEY_SECRET` (Python default `''`).
    pub razorpay_key_secret: Secret,
    /// `BASKFY_RAZORPAY_WEBHOOK_SECRET` (Python default `''`).
    ///
    /// docs/11 §Security: "Razorpay webhooks: verify signature". A *different* secret from the API key — Razorpay signs webhooks with the value configured on the webhook, not the key.
    pub razorpay_webhook_secret: Secret,
    /// `BASKFY_RAZORPAY_PLAN_ID_MONTHLY` (Python default `''`).
    ///
    /// Razorpay subscriptions bill against a plan created in *their* dashboard, which has its own id. It is environment-specific (test vs live), so it is configuration, not seed data.
    pub razorpay_plan_id_monthly: String,
    /// `BASKFY_RAZORPAY_PLAN_ID_YEARLY` (Python default `''`).
    pub razorpay_plan_id_yearly: String,
    /// `BASKFY_RAZORPAY_SUBSCRIPTION_CYCLES_MONTHLY` (Python default `120`).
    ///
    /// How many billing cycles a subscription is created for. Razorpay requires a finite count; ten years of cycles is "until cancelled" in practice.
    pub razorpay_subscription_cycles_monthly: i64,
    /// `BASKFY_RAZORPAY_SUBSCRIPTION_CYCLES_YEARLY` (Python default `10`).
    pub razorpay_subscription_cycles_yearly: i64,
    /// `BASKFY_RAZORPAY_TIMEOUT_SECONDS` (Python default `10.0`).
    ///
    /// Seconds a call to the gateway may take before it is abandoned.
    pub razorpay_timeout_seconds: f64,
    /// `BASKFY_FREE_TIER_ENABLED` (Python default `False`).
    ///
    /// PROMPTS.md Prompt 13 §5: "A ₹0 free tier with a limited universe is optional — implement it behind a feature flag." docs/11 §Reliability: "Feature flags for anything touching money".
    pub free_tier_enabled: bool,
    /// `BASKFY_SUBSCRIPTIONS_ENABLED` (Python default `False`).
    ///
    /// Subscription plans, entitlement locks, paywall. Default OFF — every basket is Free Access.
    pub subscriptions_enabled: bool,
    /// `BASKFY_FEE_COLLECTION_ENABLED` (Python default `False`).
    ///
    /// Fee *collection* (vs ledger computation). Default OFF; no payment call sites while false.
    pub fee_collection_enabled: bool,
    /// `BASKFY_PUBLIC_SIGNUP_ENABLED` (Python default `False`).
    ///
    /// Public sign-up / onboarding. Default OFF — signup-shaped routes 404 while false.
    pub public_signup_enabled: bool,
    /// `BASKFY_GST_RATE_PERCENT` (Python default `Decimal('18')`).
    ///
    /// 18% for an online information service. A setting because the rate is Parliament's to change, and an issued invoice keeps the rate it was raised at (`payment.gst_rate`).
    pub gst_rate_percent: Decimal,
    /// `BASKFY_GST_SAC_CODE` (Python default `'998439'`).
    ///
    /// SAC 998439, "Other on-line contents n.e.c." NOT CONFIRMED BY A CHARTERED ACCOUNTANT.
    pub gst_sac_code: String,
    /// `BASKFY_SUPPLIER_LEGAL_NAME` (Python default `'Baskfy'`).
    ///
    /// The supplier's own particulars — whoever operates the service, not the code.
    pub supplier_legal_name: String,
    /// `BASKFY_SUPPLIER_ADDRESS_LINES` (Python default `()`).
    pub supplier_address_lines: Vec<String>,
    /// `BASKFY_SUPPLIER_GSTIN` (Python default `''`).
    pub supplier_gstin: String,
    /// `BASKFY_SUPPLIER_STATE` (Python default `''`).
    ///
    /// Spelled as the GST return expects it, e.g. "Karnataka (29)". Used as the default place of supply for a customer who has told us nothing (IGST Act §12(2)(b)).
    pub supplier_state: String,
    /// `BASKFY_INVOICE_SERIES_PREFIX` (Python default `'DCL'`).
    ///
    /// Prefix of the invoice series: ``DCL/2026-27/000001``.
    pub invoice_series_prefix: String,
    /// `BASKFY_INVOICE_NUMBER_WIDTH` (Python default `6`).
    ///
    /// Zero-padding of the running number inside a series.
    pub invoice_number_width: i64,
    /// `BASKFY_INVOICE_OBJECT_PREFIX` (Python default `'invoices'`).
    ///
    /// Where invoice PDFs are written. ``payment.invoice_pdf_key`` stores the full key.
    pub invoice_object_prefix: String,
    /// `BASKFY_INVOICE_LOCAL_DIR` (Python default `'.archive'`).
    ///
    /// The directory invoice PDFs land in when no S3/R2 bucket is configured. Shares the raw-file archive's default so a laptop has one place to look.
    pub invoice_local_dir: String,
    /// `BASKFY_BACKTEST_USER_CONCURRENCY` (Python default `1`).
    ///
    /// PROMPTS.md Prompt 15 §4: "a per-user concurrency cap of 1, and a global cap." The per-user number is the document's; the global one is not in the bundle at all. Both are settings so a deployment with a bigger backtest pool can raise them without a code change.
    pub backtest_user_concurrency: i64,
    /// `BASKFY_BACKTEST_GLOBAL_CONCURRENCY` (Python default `8`).
    pub backtest_global_concurrency: i64,
    /// `BASKFY_PUBLIC_API_ENABLED` (Python default `False`).
    ///
    /// PROMPTS.md Prompt 20 §2: "Gate the entire feature behind a flag that stays OFF until the data-redistribution review in docs/11 is signed off." **This default is asserted by ``services/api/tests/test_public_api.py`` and by a scan over every environment file in the repository** — Prompt 20's third acceptance criterion. Turning it on is not sufficient on its own: ``baskfy_core.public_api.DATA_REDISTRIBUTION_REVIEW`` is a source constant that a human must edit, and the router refuses to mount while it says the review is outstanding.
    pub public_api_enabled: bool,
    /// `BASKFY_PUBLIC_API_CONTACT_EMAIL` (Python default `'api@baskfy.com'`).
    ///
    /// Where a caller of the public API is told to write. Appears in the terms-of-use document.
    pub public_api_contact_email: String,
    /// `BASKFY_PUBLIC_API_BASE_URL` (Python default `'http://localhost:8000'`).
    ///
    /// The base URL printed in the copy-paste examples on the interactive reference. A setting so a staging deployment's examples point at staging rather than at production.
    pub public_api_base_url: String,
    /// `BASKFY_REDOC_SCRIPT_URL` (Python default `'https://cdn.redoc.ly/redoc/v2.5.0/bundles/redoc.standalone.js'`).
    ///
    /// Redoc is loaded from a CDN — see ``baskfy_api.routers.public``. Pinned by version, and a setting so an air-gapped deployment can point it at a self-hosted copy.
    pub redoc_script_url: String,
    /// `BASKFY_API_KEY_MAX_RATE_LIMIT_PER_MINUTE` (Python default `600`).
    ///
    /// Prompt 20 §1: "per-key rate limits". The ceiling an owner may set for one of their own keys, so a key cannot be given a quota larger than the tier docs/07 fixes at 600/min.
    pub api_key_max_rate_limit_per_minute: i64,
    /// `BASKFY_API_KEY_DEFAULT_TTL_DAYS` (Python default `None`).
    ///
    /// How long a key lives when the caller does not say. ``None`` means "until revoked", which is what an integration credential usually is; a default expiry that silently breaks a production integration at 3am is worse than a key an operator has to remember to rotate.
    pub api_key_default_ttl_days: Option<i64>,
    /// `BASKFY_ALERT_LOOKBACK_DAYS` (Python default `14`).
    ///
    /// PROMPTS.md Prompt 20 §3. The number of days of screen runs an alert will look back over for its "previous" side. Beyond this the diff is against nothing and the alert is skipped rather than reporting every constituent as an entry.
    pub alert_lookback_days: i64,
    /// `BASKFY_ALERT_MIN_MOVE` (Python default `1`).
    ///
    /// The smallest rank move an alert reports by default (``baskfy_core.screen_diff``).
    pub alert_min_move: i64,
    /// `BASKFY_ALERT_MAX_ROWS` (Python default `25`).
    ///
    /// How many entries/exits/movers one email lists before it says "and N more". An email with four thousand rows in it is not an alert.
    pub alert_max_rows: i64,
    /// `BASKFY_WEBHOOK_SIGNING_SECRET` (Python default `''`).
    ///
    /// Prompt 20 §4: "HMAC signing". The master secret every endpoint's signing key is derived from — see ``baskfy_api.webhooks``. Empty falls back to ``jwt_secret``, which production already requires; a deployment with neither cannot create a webhook at all.
    pub webhook_signing_secret: Secret,
    /// `BASKFY_WEBHOOK_TIMEOUT_SECONDS` (Python default `10.0`).
    ///
    /// Seconds one delivery attempt may take.
    pub webhook_timeout_seconds: f64,
    /// `BASKFY_WEBHOOK_MAX_ATTEMPTS` (Python default `5`).
    ///
    /// "retry with backoff". Attempts, and the base of the exponential schedule in seconds: 30s, 2m, 8m, 32m, 2h8m — five attempts spanning about three hours.
    pub webhook_max_attempts: i64,
    /// `BASKFY_WEBHOOK_BACKOFF_BASE_SECONDS` (Python default `30`).
    pub webhook_backoff_base_seconds: i64,
    /// `BASKFY_WEBHOOK_BACKOFF_FACTOR` (Python default `4`).
    pub webhook_backoff_factor: i64,
    /// `BASKFY_WEBHOOK_FAILURE_THRESHOLD` (Python default `20`).
    ///
    /// Consecutive failed deliveries before an endpoint is switched off and its owner told.
    pub webhook_failure_threshold: i64,
    /// `BASKFY_SWING_EXECUTION_ENABLED` (Python default `False`).
    pub swing_execution_enabled: bool,
    /// `BASKFY_SWING_MONITOR_ENABLED` (Python default `False`).
    pub swing_monitor_enabled: bool,
    /// `BASKFY_SWING_EP_PREMARKET_ENABLED` (Python default `False`).
    pub swing_ep_premarket_enabled: bool,
    /// `BASKFY_SWING_RISK_PER_TRADE_PCT_MAX` (Python default `Decimal('1.0')`).
    ///
    /// ``sw_config.risk_per_trade_pct`` may not exceed this. His own range is 0.25-1%; 1.0 is the top of it, and the pack's default setting is half that.
    pub swing_risk_per_trade_pct_max: Decimal,
    /// `BASKFY_SWING_MAX_POSITION_PCT_MAX` (Python default `Decimal('30.0')`).
    ///
    /// ``sw_config.max_position_pct`` may not exceed this. His own line (`07`, SW9.5): "I don't believe you should ever have more than 30% of your account over night in any stock"; the default setting stays 20%.
    pub swing_max_position_pct_max: Decimal,
    /// `BASKFY_SWING_MAX_OPEN_POSITIONS_MAX` (Python default `20`).
    ///
    /// ``sw_config.max_open_positions`` may not exceed this. His "15-20 positions in a good market" (`07`, SW9.5); the ladder's top rung is his typical 10, and the plan takes the smaller of the rung and the setting.
    pub swing_max_open_positions_max: i64,
    /// `BASKFY_SWING_INDEX_SLUG` (Python default `'nifty-smallcap-250'`).
    ///
    /// The benchmark the market gate reads (``docs/swing/04`` §8.2). Falls back to ``nifty-50`` when the named index has no snapshot; a missing benchmark is not a bear market.
    pub swing_index_slug: String,
    /// `BASKFY_SWING_SCAN_MIN_INTERVAL_SECONDS` (Python default `60`).
    ///
    /// SW15 "Scan now": the least time between two requests from the one tenant, in seconds. A scan is a few minutes of Polars over the liquid universe and, during the session, one Kite quote call per 500 names under the shared limiter (STANDING-ANSWERS B10) — a second press inside a minute buys nothing the first will not deliver. A threshold, so a setting (B13), never a literal in the route.
    pub swing_scan_min_interval_seconds: i64,
    /// `BASKFY_SWING_SCAN_STALE_AFTER_SECONDS` (Python default `600`).
    ///
    /// A `QUEUED` / `RUNNING` scan older than this no longer counts as "in flight": a worker that died mid-scan must not lock the button forever. Ten minutes is three times the detect budget (B9: < 3 min for 2,500 names).
    pub swing_scan_stale_after_seconds: i64,
    /// `BASKFY_VBT_EXECUTION_ENABLED` (Python default `False`).
    pub vbt_execution_enabled: bool,
    /// `BASKFY_VBT_NIGHTLY_ENABLED` (Python default `True`).
    pub vbt_nightly_enabled: bool,
    /// `BASKFY_VBT_MAX_OPEN_POSITIONS_MAX` (Python default `15`).
    ///
    /// ``vb_config.max_open_positions`` may not exceed this. The strategy's own ten slots are the measured optimum (eight cost 2.7 CAGR points, fifteen cost 5.3); the ceiling leaves room to lower the book, never to widen it past what ``SizingConfig.max_slots`` allows.
    pub vbt_max_open_positions_max: i64,
    /// `BASKFY_VBT_MAX_POSITION_PCT_MAX` (Python default `Decimal('15.0')`).
    ///
    /// ``vb_config.max_position_pct`` may not exceed this. The slot is 10% and the setting 12.5%.
    pub vbt_max_position_pct_max: Decimal,
    /// `BASKFY_VBT_STOP_PCT_MAX` (Python default `Decimal('15.0')`).
    ///
    /// ``vb_config.stop_pct`` may not exceed this. STRATEGY §4 measured 10% and 15% and found the stop to be insurance either way; wider than 15% is a different instrument.
    pub vbt_stop_pct_max: Decimal,
    /// `BASKFY_VBT_SCAN_MIN_INTERVAL_SECONDS` (Python default `60`).
    ///
    /// VB12 "Scan now": the least time between two requests from the one tenant, in seconds. The same two numbers the swing book's scan carries and, deliberately, the same values the worker's `baskfy_worker.tasks.vbt_rescan` and the desk's `app/vbt_desk.py` already hold — a re-detect is a couple of minutes of Polars over the published universe, and a second press inside a minute buys nothing the first will not deliver.
    pub vbt_scan_min_interval_seconds: i64,
    /// `BASKFY_VBT_SCAN_STALE_AFTER_SECONDS` (Python default `600`).
    ///
    /// A `QUEUED` / `RUNNING` re-detect older than this no longer counts as "in flight": a worker that died mid-scan must not lock the button for an afternoon.
    pub vbt_scan_stale_after_seconds: i64,
    /// `BASKFY_TWT_EXECUTION_ENABLED` (Python default `False`).
    pub twt_execution_enabled: bool,
    /// `BASKFY_TWT_NIGHTLY_ENABLED` (Python default `True`).
    pub twt_nightly_enabled: bool,
    /// `BASKFY_TWT_MAX_OPEN_POSITIONS_MAX` (Python default `15`).
    ///
    /// ``tw_config.max_open_positions`` may not exceed this. ``SizingConfig.max_slots`` [10] is the strategy's own measured optimum (eight returns 26.3% CAGR, fifteen returns 14.7%); the ceiling leaves room to lower the book, never to widen it into a different instrument.
    pub twt_max_open_positions_max: i64,
    /// `BASKFY_TWT_MAX_POSITION_PCT_MAX` (Python default `Decimal('15.00')`).
    ///
    /// ``tw_config.max_position_pct`` may not exceed this. The slot is 10% and the setting 12.5%.
    pub twt_max_position_pct_max: Decimal,
    /// `BASKFY_TWT_STOP_PCT_MAX` (Python default `Decimal('25.00')`).
    ///
    /// ``tw_config.stop_pct`` may not exceed this. ``docs/twt/01`` §7 measured 15-30% and found the band flat, which is why the stop is a setting at all; past 25% it stops being a disaster stop.
    pub twt_stop_pct_max: Decimal,
    /// `BASKFY_TWT_TRAIL_PCT_MIN` (Python default `Decimal('18.00')`).
    ///
    /// ``tw_config.trail_pct`` may not fall **below** this — a floor, not a ceiling, and the one bound in this file that is easy to implement backwards. The trail is TWT-1's only exit (137 of the research's 164 exits) and the measured cliff is in the tightening direction: 20% -> 15% took the CAGR from 20.9% to 9.6% and the drawdown from -24.7% to -43%. Widening it is merely unprofitable; tightening it is the failure mode. DECISIONS-TW **TW0.5**.
    pub twt_trail_pct_min: Decimal,
    /// `BASKFY_TWT_SCAN_MIN_INTERVAL_SECONDS` (Python default `60`).
    ///
    /// TW12 "Scan now": the least time between two requests from the one tenant, in seconds. A scan re-detects the whole cash universe — a couple of minutes of Polars over closed bars — and a second press inside a minute buys nothing the first will not deliver. A threshold, so a setting (STANDING-ANSWERS B13), never a literal in the route.
    pub twt_scan_min_interval_seconds: i64,
    /// `BASKFY_TWT_SCAN_STALE_AFTER_SECONDS` (Python default `600`).
    ///
    /// A ``QUEUED`` / ``RUNNING`` scan older than this no longer counts as "in flight": a worker that died mid-scan must not lock the button forever. Ten minutes, the same window the swing book and VBT-1 use, and the desk copies it (`app/twt_desk.py`) because it cannot import the worker.
    pub twt_scan_stale_after_seconds: i64,
    /// `BASKFY_OPTIONS_O1M_EXECUTION_ENABLED` (Python default `False`).
    pub options_o1m_execution_enabled: bool,
    /// `BASKFY_OPTIONS_O1W_EXECUTION_ENABLED` (Python default `False`).
    pub options_o1w_execution_enabled: bool,
    /// `BASKFY_OPTIONS_O2_EXECUTION_ENABLED` (Python default `False`).
    pub options_o2_execution_enabled: bool,
    /// `BASKFY_OPTIONS_O3_EXECUTION_ENABLED` (Python default `False`).
    pub options_o3_execution_enabled: bool,
    /// `BASKFY_OPTIONS_MONITOR_ENABLED` (Python default `False`).
    pub options_monitor_enabled: bool,
    /// `BASKFY_OPTIONS_COLLECT_ENABLED` (Python default `False`).
    pub options_collect_enabled: bool,
    /// `BASKFY_OPTIONS_SCAN_ENABLED` (Python default `False`).
    pub options_scan_enabled: bool,
    /// `BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX` (Python default `Decimal('25000')`).
    ///
    /// ``op_sleeve_config`` — any sleeve's per-trade risk in ₹ may not exceed this.
    pub options_risk_per_trade_inr_max: Decimal,
    /// `BASKFY_OPTIONS_RISK_PCT_MAX` (Python default `Decimal('1.0')`).
    ///
    /// ``op_sleeve_config.risk_per_trade_pct`` may not exceed this.
    pub options_risk_pct_max: Decimal,
    /// `BASKFY_OPTIONS_MAX_LOTS_MAX` (Python default `10`).
    ///
    /// ``op_sleeve_config.max_lots`` may not exceed this.
    pub options_max_lots_max: i64,
    /// `BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX` (Python default `Decimal('30000')`).
    ///
    /// ``op_book_config.daily_loss_limit_inr`` may not exceed this.
    pub options_book_daily_loss_inr_max: Decimal,
    /// `BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX` (Python default `Decimal('75000')`).
    ///
    /// ``op_book_config.monthly_pause_inr`` may not exceed this.
    pub options_book_monthly_loss_inr_max: Decimal,
    /// `BASKFY_OPTIONS_HARD_EXIT_LATEST` (Python default `datetime.time(15, 0)`).
    ///
    /// ``op_sleeve_config.hard_exit_time`` may not be later than this (``02`` Track C §1).
    pub options_hard_exit_latest: NaiveTime,
    /// `BASKFY_FNO_F1_EXECUTION_ENABLED` (Python default `False`).
    pub fno_f1_execution_enabled: bool,
    /// `BASKFY_FNO_F2_EXECUTION_ENABLED` (Python default `False`).
    pub fno_f2_execution_enabled: bool,
    /// `BASKFY_FNO_F3_EXECUTION_ENABLED` (Python default `False`).
    ///
    /// F3 (M.5). Reported only. Its exit flag is the desk's and is not read here.
    pub fno_f3_execution_enabled: bool,
    /// `BASKFY_FNO_CARRY_ENABLED` (Python default `False`).
    pub fno_carry_enabled: bool,
    /// `BASKFY_FNO_SCAN_ENABLED` (Python default `False`).
    pub fno_scan_enabled: bool,
    /// `BASKFY_FNO_MONITOR_ENABLED` (Python default `False`).
    pub fno_monitor_enabled: bool,
    /// `BASKFY_FNO_RISK_PER_TRADE_INR_MAX` (Python default `Decimal('25000')`).
    ///
    /// Every FO plan's max loss in ₹; a sleeve's capital x risk % may not exceed it.
    pub fno_risk_per_trade_inr_max: Decimal,
    /// `BASKFY_FNO_RISK_PCT_MAX` (Python default `Decimal('1.0')`).
    ///
    /// ``fo_sleeve_config.risk_per_trade_pct`` may not exceed this.
    pub fno_risk_pct_max: Decimal,
    /// `BASKFY_FNO_MAX_OPEN_POSITIONS_MAX` (Python default `10`).
    ///
    /// ``fo_sleeve_config.max_open_positions`` may not exceed this.
    pub fno_max_open_positions_max: i64,
    /// `BASKFY_FNO_MAX_PER_UNDERLYING_MAX` (Python default `1`).
    ///
    /// Open FO positions on one underlying across sleeves (reported; no setting reaches it).
    pub fno_max_per_underlying_max: i64,
    /// `BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX` (Python default `Decimal('75000')`).
    ///
    /// ``fo_book_config.monthly_pause_inr`` may not exceed this.
    pub fno_book_monthly_loss_inr_max: Decimal,
    /// `BASKFY_RATE_LIMIT_ANONYMOUS_PER_MINUTE` (Python default `10`).
    pub rate_limit_anonymous_per_minute: i64,
    /// `BASKFY_RATE_LIMIT_AUTHENTICATED_PER_MINUTE` (Python default `60`).
    pub rate_limit_authenticated_per_minute: i64,
    /// `BASKFY_RATE_LIMIT_API_KEY_PER_MINUTE` (Python default `600`).
    pub rate_limit_api_key_per_minute: i64,
    /// `BASKFY_RATE_LIMIT_AUTH_PER_MINUTE` (Python default `10`).
    ///
    /// docs/11: "exponential backoff on auth endpoints". Per IP, far below the anonymous tier, because an auth endpoint is the one place a stranger's request costs a password guess.
    pub rate_limit_auth_per_minute: i64,
    /// `BASKFY_RATE_LIMIT_WEBHOOK_PER_MINUTE` (Python default `600`).
    ///
    /// Gateway webhooks are not anonymous browser traffic. Razorpay retries a failed delivery, and dropping one at ten a minute would lose a payment; the bucket exists so the endpoint is still metered rather than open. Keyed per IP, like every other anonymous caller.
    pub rate_limit_webhook_per_minute: i64,
    /// `BASKFY_RATE_LIMIT_ENABLED` (Python default `True`).
    ///
    /// Turned off in tests that are not about rate limiting, and in `local` by choice.
    pub rate_limit_enabled: bool,
    /// `BASKFY_DB_POOL_SIZE` (Python default `10`).
    ///
    /// SQLAlchemy's ``QueuePool`` in front of asyncpg. docs/11 §"Cost envelope" sizes the box at 8 vCPU / 32 GB and docs/03 §"Scaling plan" step 1 says "vertical: bigger box" before anything else, so the pool is sized against one API process on that box rather than against a fleet. See ``baskfy_api.db`` for the arithmetic.
    pub db_pool_size: i64,
    /// `BASKFY_DB_MAX_OVERFLOW` (Python default `10`).
    pub db_max_overflow: i64,
    /// `BASKFY_DB_POOL_TIMEOUT_SECONDS` (Python default `5.0`).
    ///
    /// Seconds a request waits for a connection before it is refused. Shorter than the screen-run budget in docs/11 (800 ms cold) times a small factor: a request that has already queued this long has missed its budget, and holding it open only deepens the queue.
    pub db_pool_timeout_seconds: f64,
    /// `BASKFY_DB_POOL_RECYCLE_SECONDS` (Python default `1800`).
    ///
    /// Recycle a connection after this long. Below any sensible proxy or firewall idle timeout, so the pool never hands out a socket the other end has already dropped.
    pub db_pool_recycle_seconds: i64,
    /// `BASKFY_DB_STATEMENT_CACHE_SIZE` (Python default `100`).
    ///
    /// asyncpg prepares every statement server-side and caches the handle per connection. Behind pgbouncer in *transaction* pooling mode that cache is wrong — the next transaction may land on a different server connection — so a deployment that puts pgbouncer in front must set this to 0. Direct connections keep it, because the screen query is the same statement every time and re-planning it per request is pure waste.
    pub db_statement_cache_size: i64,
    /// `BASKFY_HTTP_STALE_WHILE_REVALIDATE_SECONDS` (Python default `60`).
    ///
    /// ``stale-while-revalidate`` on the published analytics reads. One minute: long enough that a burst of RSC renders is served from the Next data cache while one of them revalidates, short enough that a publish is visible within a minute even if `POST /api/revalidate` never arrives (``docs/11a`` §6).
    pub http_stale_while_revalidate_seconds: i64,
    /// `BASKFY_OTEL_ENABLED` (Python default `False`).
    pub otel_enabled: bool,
    /// `BASKFY_OTEL_SERVICE_NAME` (Python default `'baskfy-api'`).
    pub otel_service_name: String,
    /// `BASKFY_OTEL_EXPORTER_OTLP_ENDPOINT` (Python default `None`).
    pub otel_exporter_otlp_endpoint: Option<String>,
    /// `BASKFY_LOG_LEVEL` (Python default `'INFO'`).
    pub log_level: String,
    /// `BASKFY_LOG_JSON` (Python default `True`).
    ///
    /// JSON lines in every environment but `local`, where a human is reading them.
    pub log_json: bool,
    /// `BASKFY_SENTRY_DSN` (Python default `''`).
    ///
    /// docs/02 §Observability: "Sentry for errors". Empty means the SDK is never initialised at all — see `baskfy_api.sentry` for why that is the honest default rather than a no-op init.
    pub sentry_dsn: Secret,
    /// `BASKFY_SENTRY_SAMPLE_RATE` (Python default `1.0`).
    ///
    /// Fraction of *error* events sent. 1.0 because an exception this service raises is rare and sampling them away is how a rare bug stays invisible; the knob exists for an incident where one endpoint is throwing thousands a minute.
    pub sentry_sample_rate: f64,
    /// `BASKFY_RELEASE` (Python default `''`).
    ///
    /// The build this process is running, e.g. a git sha. Tags Sentry events and OTel resources so "when did this start" has an answer. Set by the deploy; empty on a laptop.
    pub release: String,
    /// `BASKFY_METRICS_ENABLED` (Python default `True`).
    ///
    /// PROMPTS.md Prompt 17 §2. `/metrics` is unauthenticated by design and must therefore not be exposed publicly — it is a private-network scrape target (see `docs/runbooks/`). Off in `local` costs nothing; on everywhere a Prometheus exists.
    pub metrics_enabled: bool,
    /// `BASKFY_METRICS_TOKEN` (Python default `''`).
    ///
    /// When set, `/metrics` requires `Authorization: Bearer <this>`. A shared secret rather than a JWT because Prometheus has no account and cannot refresh one. Empty = no check, which is correct behind a private network and wrong on the public internet.
    pub metrics_token: Secret,
    /// `BASKFY_OPS_ALERT_EMAIL` (Python default `''`).
    ///
    /// Where operational alerts are emailed. Empty means alerts are logged (and sent to Sentry if it is configured) but nobody is woken up.
    pub ops_alert_email: String,
    /// `BASKFY_OPS_ALERT_WEBHOOK_URL` (Python default `''`).
    ///
    /// An optional generic webhook (Alertmanager, Slack's incoming webhook, whatever the operator has). Posted as JSON. Empty disables it.
    pub ops_alert_webhook_url: String,
    /// `BASKFY_PUBLISH_DEADLINE_IST` (Python default `'20:15'`).
    ///
    /// docs/11 §Reliability: "data published by 20:15 IST on >=95% of trading days." The deadline the publish-late alert fires on, as an IST wall-clock time in `HH:MM`.
    pub publish_deadline_ist: String,
    /// `BASKFY_PIPELINE_STALE_AFTER_MINUTES` (Python default `90`).
    ///
    /// A run still `running` this long after it started is treated as abandoned — the shape a worker killed mid-pipeline leaves behind. Above the 45-minute end-to-end budget in docs/11 with room for a slow night, so a healthy long run is never reaped.
    pub pipeline_stale_after_minutes: i64,
    /// `BASKFY_QUEUE_BACKLOG_THRESHOLD` (Python default `100`).
    ///
    /// Queue depth that counts as a backlog worth an alert.
    pub queue_backlog_threshold: i64,
    /// `BASKFY_KITE_TOKEN_WARNING_HOURS` (Python default `6`).
    ///
    /// How long before a Kite access token expires the warning fires, in hours.
    pub kite_token_warning_hours: i64,
    /// `BASKFY_CORS_ORIGINS` (Python default `('http://localhost:3000',)`).
    pub cors_origins: Vec<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            database_url: Secret::new("postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy"),
            redis_url: Secret::new("redis://localhost:6380/0"),
            backtest_executor: "inline".to_owned(),
            backtest_concurrency: 2,
            test_database_url: None,
            environment: "local".to_owned(),
            jwt_secret: Secret::new(""),
            jwt_issuer: None,
            jwt_audience: None,
            jwt_max_lifetime_seconds: 900,
            jwt_leeway_seconds: 30,
            access_token_ttl_seconds: 900,
            refresh_token_ttl_days: 30,
            google_client_id: "".to_owned(),
            google_client_secret: Secret::new(""),
            login_allowlist: "".to_owned(),
            staff_allowlist:
                "mdave.5191@gmail.com,learnwithalacrity@gmail.com,maulikdave05@gmail.com".to_owned(),
            kite_publisher_api_key: "".to_owned(),
            otp_length: 6,
            otp_ttl_seconds: 600,
            otp_max_attempts: 5,
            password_reset_ttl_seconds: 3600,
            email_verification_ttl_seconds: 86400,
            auth_max_failures: 10,
            auth_lockout_minutes: 15,
            auth_failure_window_minutes: 60,
            argon2_time_cost: 2,
            argon2_memory_kib: 19456,
            argon2_parallelism: 1,
            cookie_secure: true,
            cookie_domain: None,
            account_purge_after_days: 7,
            email_transport: "console".to_owned(),
            resend_api_key: Secret::new(""),
            email_from: "Baskfy <no-reply@baskfy.com>".to_owned(),
            email_reply_to: None,
            smtp_host: "localhost".to_owned(),
            smtp_port: 1025,
            smtp_username: "".to_owned(),
            smtp_password: Secret::new(""),
            smtp_starttls: false,
            web_origin: "http://localhost:3000".to_owned(),
            support_email: "support@baskfy.com".to_owned(),
            razorpay_api_base: "https://api.razorpay.com/v1".to_owned(),
            razorpay_key_id: "".to_owned(),
            razorpay_key_secret: Secret::new(""),
            razorpay_webhook_secret: Secret::new(""),
            razorpay_plan_id_monthly: "".to_owned(),
            razorpay_plan_id_yearly: "".to_owned(),
            razorpay_subscription_cycles_monthly: 120,
            razorpay_subscription_cycles_yearly: 10,
            razorpay_timeout_seconds: 10.0,
            free_tier_enabled: false,
            subscriptions_enabled: false,
            fee_collection_enabled: false,
            public_signup_enabled: false,
            gst_rate_percent: dec("18"),
            gst_sac_code: "998439".to_owned(),
            supplier_legal_name: "Baskfy".to_owned(),
            supplier_address_lines: vec![],
            supplier_gstin: "".to_owned(),
            supplier_state: "".to_owned(),
            invoice_series_prefix: "DCL".to_owned(),
            invoice_number_width: 6,
            invoice_object_prefix: "invoices".to_owned(),
            invoice_local_dir: ".archive".to_owned(),
            backtest_user_concurrency: 1,
            backtest_global_concurrency: 8,
            public_api_enabled: false,
            public_api_contact_email: "api@baskfy.com".to_owned(),
            public_api_base_url: "http://localhost:8000".to_owned(),
            redoc_script_url: "https://cdn.redoc.ly/redoc/v2.5.0/bundles/redoc.standalone.js"
                .to_owned(),
            api_key_max_rate_limit_per_minute: 600,
            api_key_default_ttl_days: None,
            alert_lookback_days: 14,
            alert_min_move: 1,
            alert_max_rows: 25,
            webhook_signing_secret: Secret::new(""),
            webhook_timeout_seconds: 10.0,
            webhook_max_attempts: 5,
            webhook_backoff_base_seconds: 30,
            webhook_backoff_factor: 4,
            webhook_failure_threshold: 20,
            swing_execution_enabled: false,
            swing_monitor_enabled: false,
            swing_ep_premarket_enabled: false,
            swing_risk_per_trade_pct_max: dec("1.0"),
            swing_max_position_pct_max: dec("30.0"),
            swing_max_open_positions_max: 20,
            swing_index_slug: "nifty-smallcap-250".to_owned(),
            swing_scan_min_interval_seconds: 60,
            swing_scan_stale_after_seconds: 600,
            vbt_execution_enabled: false,
            vbt_nightly_enabled: true,
            vbt_max_open_positions_max: 15,
            vbt_max_position_pct_max: dec("15.0"),
            vbt_stop_pct_max: dec("15.0"),
            vbt_scan_min_interval_seconds: 60,
            vbt_scan_stale_after_seconds: 600,
            twt_execution_enabled: false,
            twt_nightly_enabled: true,
            twt_max_open_positions_max: 15,
            twt_max_position_pct_max: dec("15.00"),
            twt_stop_pct_max: dec("25.00"),
            twt_trail_pct_min: dec("18.00"),
            twt_scan_min_interval_seconds: 60,
            twt_scan_stale_after_seconds: 600,
            options_o1m_execution_enabled: false,
            options_o1w_execution_enabled: false,
            options_o2_execution_enabled: false,
            options_o3_execution_enabled: false,
            options_monitor_enabled: false,
            options_collect_enabled: false,
            options_scan_enabled: false,
            options_risk_per_trade_inr_max: dec("25000"),
            options_risk_pct_max: dec("1.0"),
            options_max_lots_max: 10,
            options_book_daily_loss_inr_max: dec("30000"),
            options_book_monthly_loss_inr_max: dec("75000"),
            options_hard_exit_latest: NaiveTime::from_hms_opt(15, 0, 0).unwrap_or_default(),
            fno_f1_execution_enabled: false,
            fno_f2_execution_enabled: false,
            fno_f3_execution_enabled: false,
            fno_carry_enabled: false,
            fno_scan_enabled: false,
            fno_monitor_enabled: false,
            fno_risk_per_trade_inr_max: dec("25000"),
            fno_risk_pct_max: dec("1.0"),
            fno_max_open_positions_max: 10,
            fno_max_per_underlying_max: 1,
            fno_book_monthly_loss_inr_max: dec("75000"),
            rate_limit_anonymous_per_minute: 10,
            rate_limit_authenticated_per_minute: 60,
            rate_limit_api_key_per_minute: 600,
            rate_limit_auth_per_minute: 10,
            rate_limit_webhook_per_minute: 600,
            rate_limit_enabled: true,
            db_pool_size: 10,
            db_max_overflow: 10,
            db_pool_timeout_seconds: 5.0,
            db_pool_recycle_seconds: 1800,
            db_statement_cache_size: 100,
            http_stale_while_revalidate_seconds: 60,
            otel_enabled: false,
            otel_service_name: "baskfy-api".to_owned(),
            otel_exporter_otlp_endpoint: None,
            log_level: "INFO".to_owned(),
            log_json: true,
            sentry_dsn: Secret::new(""),
            sentry_sample_rate: 1.0,
            release: "".to_owned(),
            metrics_enabled: true,
            metrics_token: Secret::new(""),
            ops_alert_email: "".to_owned(),
            ops_alert_webhook_url: "".to_owned(),
            publish_deadline_ist: "20:15".to_owned(),
            pipeline_stale_after_minutes: 90,
            queue_backlog_threshold: 100,
            kite_token_warning_hours: 6,
            cors_origins: vec!["http://localhost:3000".to_owned()],
        }
    }
}

impl Settings {
    /// Every field name, in Python's declaration order.
    pub const FIELDS: [&'static str; 160] = [
        "database_url",
        "redis_url",
        "backtest_executor",
        "backtest_concurrency",
        "test_database_url",
        "environment",
        "jwt_secret",
        "jwt_issuer",
        "jwt_audience",
        "jwt_max_lifetime_seconds",
        "jwt_leeway_seconds",
        "access_token_ttl_seconds",
        "refresh_token_ttl_days",
        "google_client_id",
        "google_client_secret",
        "login_allowlist",
        "staff_allowlist",
        "kite_publisher_api_key",
        "otp_length",
        "otp_ttl_seconds",
        "otp_max_attempts",
        "password_reset_ttl_seconds",
        "email_verification_ttl_seconds",
        "auth_max_failures",
        "auth_lockout_minutes",
        "auth_failure_window_minutes",
        "argon2_time_cost",
        "argon2_memory_kib",
        "argon2_parallelism",
        "cookie_secure",
        "cookie_domain",
        "account_purge_after_days",
        "email_transport",
        "resend_api_key",
        "email_from",
        "email_reply_to",
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "smtp_password",
        "smtp_starttls",
        "web_origin",
        "support_email",
        "razorpay_api_base",
        "razorpay_key_id",
        "razorpay_key_secret",
        "razorpay_webhook_secret",
        "razorpay_plan_id_monthly",
        "razorpay_plan_id_yearly",
        "razorpay_subscription_cycles_monthly",
        "razorpay_subscription_cycles_yearly",
        "razorpay_timeout_seconds",
        "free_tier_enabled",
        "subscriptions_enabled",
        "fee_collection_enabled",
        "public_signup_enabled",
        "gst_rate_percent",
        "gst_sac_code",
        "supplier_legal_name",
        "supplier_address_lines",
        "supplier_gstin",
        "supplier_state",
        "invoice_series_prefix",
        "invoice_number_width",
        "invoice_object_prefix",
        "invoice_local_dir",
        "backtest_user_concurrency",
        "backtest_global_concurrency",
        "public_api_enabled",
        "public_api_contact_email",
        "public_api_base_url",
        "redoc_script_url",
        "api_key_max_rate_limit_per_minute",
        "api_key_default_ttl_days",
        "alert_lookback_days",
        "alert_min_move",
        "alert_max_rows",
        "webhook_signing_secret",
        "webhook_timeout_seconds",
        "webhook_max_attempts",
        "webhook_backoff_base_seconds",
        "webhook_backoff_factor",
        "webhook_failure_threshold",
        "swing_execution_enabled",
        "swing_monitor_enabled",
        "swing_ep_premarket_enabled",
        "swing_risk_per_trade_pct_max",
        "swing_max_position_pct_max",
        "swing_max_open_positions_max",
        "swing_index_slug",
        "swing_scan_min_interval_seconds",
        "swing_scan_stale_after_seconds",
        "vbt_execution_enabled",
        "vbt_nightly_enabled",
        "vbt_max_open_positions_max",
        "vbt_max_position_pct_max",
        "vbt_stop_pct_max",
        "vbt_scan_min_interval_seconds",
        "vbt_scan_stale_after_seconds",
        "twt_execution_enabled",
        "twt_nightly_enabled",
        "twt_max_open_positions_max",
        "twt_max_position_pct_max",
        "twt_stop_pct_max",
        "twt_trail_pct_min",
        "twt_scan_min_interval_seconds",
        "twt_scan_stale_after_seconds",
        "options_o1m_execution_enabled",
        "options_o1w_execution_enabled",
        "options_o2_execution_enabled",
        "options_o3_execution_enabled",
        "options_monitor_enabled",
        "options_collect_enabled",
        "options_scan_enabled",
        "options_risk_per_trade_inr_max",
        "options_risk_pct_max",
        "options_max_lots_max",
        "options_book_daily_loss_inr_max",
        "options_book_monthly_loss_inr_max",
        "options_hard_exit_latest",
        "fno_f1_execution_enabled",
        "fno_f2_execution_enabled",
        "fno_f3_execution_enabled",
        "fno_carry_enabled",
        "fno_scan_enabled",
        "fno_monitor_enabled",
        "fno_risk_per_trade_inr_max",
        "fno_risk_pct_max",
        "fno_max_open_positions_max",
        "fno_max_per_underlying_max",
        "fno_book_monthly_loss_inr_max",
        "rate_limit_anonymous_per_minute",
        "rate_limit_authenticated_per_minute",
        "rate_limit_api_key_per_minute",
        "rate_limit_auth_per_minute",
        "rate_limit_webhook_per_minute",
        "rate_limit_enabled",
        "db_pool_size",
        "db_max_overflow",
        "db_pool_timeout_seconds",
        "db_pool_recycle_seconds",
        "db_statement_cache_size",
        "http_stale_while_revalidate_seconds",
        "otel_enabled",
        "otel_service_name",
        "otel_exporter_otlp_endpoint",
        "log_level",
        "log_json",
        "sentry_dsn",
        "sentry_sample_rate",
        "release",
        "metrics_enabled",
        "metrics_token",
        "ops_alert_email",
        "ops_alert_webhook_url",
        "publish_deadline_ist",
        "pipeline_stale_after_minutes",
        "queue_backlog_threshold",
        "kite_token_warning_hours",
        "cors_origins",
    ];

    /// Sets one field from its environment string (pydantic's lax str coercion).
    fn set(&mut self, field: &str, raw: &str) -> Result<(), String> {
        match field {
            "database_url" => self.database_url = Secret::new(raw),
            "redis_url" => self.redis_url = Secret::new(raw),
            "backtest_executor" => {
                self.backtest_executor = parse::literal(raw, &["inline", "celery"])?
            }
            "backtest_concurrency" => self.backtest_concurrency = parse::integer(raw)?,
            "test_database_url" => self.test_database_url = Some(Secret::new(raw)),
            "environment" => {
                self.environment = parse::literal(raw, &["local", "test", "staging", "production"])?
            }
            "jwt_secret" => self.jwt_secret = Secret::new(raw),
            "jwt_issuer" => self.jwt_issuer = Some(raw.to_owned()),
            "jwt_audience" => self.jwt_audience = Some(raw.to_owned()),
            "jwt_max_lifetime_seconds" => self.jwt_max_lifetime_seconds = parse::integer(raw)?,
            "jwt_leeway_seconds" => self.jwt_leeway_seconds = parse::integer(raw)?,
            "access_token_ttl_seconds" => self.access_token_ttl_seconds = parse::integer(raw)?,
            "refresh_token_ttl_days" => self.refresh_token_ttl_days = parse::integer(raw)?,
            "google_client_id" => raw.clone_into(&mut self.google_client_id),
            "google_client_secret" => self.google_client_secret = Secret::new(raw),
            "login_allowlist" => raw.clone_into(&mut self.login_allowlist),
            "staff_allowlist" => raw.clone_into(&mut self.staff_allowlist),
            "kite_publisher_api_key" => raw.clone_into(&mut self.kite_publisher_api_key),
            "otp_length" => self.otp_length = parse::integer(raw)?,
            "otp_ttl_seconds" => self.otp_ttl_seconds = parse::integer(raw)?,
            "otp_max_attempts" => self.otp_max_attempts = parse::integer(raw)?,
            "password_reset_ttl_seconds" => self.password_reset_ttl_seconds = parse::integer(raw)?,
            "email_verification_ttl_seconds" => {
                self.email_verification_ttl_seconds = parse::integer(raw)?
            }
            "auth_max_failures" => self.auth_max_failures = parse::integer(raw)?,
            "auth_lockout_minutes" => self.auth_lockout_minutes = parse::integer(raw)?,
            "auth_failure_window_minutes" => {
                self.auth_failure_window_minutes = parse::integer(raw)?
            }
            "argon2_time_cost" => self.argon2_time_cost = parse::integer(raw)?,
            "argon2_memory_kib" => self.argon2_memory_kib = parse::integer(raw)?,
            "argon2_parallelism" => self.argon2_parallelism = parse::integer(raw)?,
            "cookie_secure" => self.cookie_secure = parse::boolean(raw)?,
            "cookie_domain" => self.cookie_domain = Some(raw.to_owned()),
            "account_purge_after_days" => self.account_purge_after_days = parse::integer(raw)?,
            "email_transport" => {
                self.email_transport = parse::literal(raw, &["resend", "smtp", "console"])?
            }
            "resend_api_key" => self.resend_api_key = Secret::new(raw),
            "email_from" => raw.clone_into(&mut self.email_from),
            "email_reply_to" => self.email_reply_to = Some(raw.to_owned()),
            "smtp_host" => raw.clone_into(&mut self.smtp_host),
            "smtp_port" => self.smtp_port = parse::integer(raw)?,
            "smtp_username" => raw.clone_into(&mut self.smtp_username),
            "smtp_password" => self.smtp_password = Secret::new(raw),
            "smtp_starttls" => self.smtp_starttls = parse::boolean(raw)?,
            "web_origin" => raw.clone_into(&mut self.web_origin),
            "support_email" => raw.clone_into(&mut self.support_email),
            "razorpay_api_base" => raw.clone_into(&mut self.razorpay_api_base),
            "razorpay_key_id" => raw.clone_into(&mut self.razorpay_key_id),
            "razorpay_key_secret" => self.razorpay_key_secret = Secret::new(raw),
            "razorpay_webhook_secret" => self.razorpay_webhook_secret = Secret::new(raw),
            "razorpay_plan_id_monthly" => raw.clone_into(&mut self.razorpay_plan_id_monthly),
            "razorpay_plan_id_yearly" => raw.clone_into(&mut self.razorpay_plan_id_yearly),
            "razorpay_subscription_cycles_monthly" => {
                self.razorpay_subscription_cycles_monthly = parse::integer(raw)?
            }
            "razorpay_subscription_cycles_yearly" => {
                self.razorpay_subscription_cycles_yearly = parse::integer(raw)?
            }
            "razorpay_timeout_seconds" => self.razorpay_timeout_seconds = parse::float(raw)?,
            "free_tier_enabled" => self.free_tier_enabled = parse::boolean(raw)?,
            "subscriptions_enabled" => self.subscriptions_enabled = parse::boolean(raw)?,
            "fee_collection_enabled" => self.fee_collection_enabled = parse::boolean(raw)?,
            "public_signup_enabled" => self.public_signup_enabled = parse::boolean(raw)?,
            "gst_rate_percent" => self.gst_rate_percent = parse::decimal(raw)?,
            "gst_sac_code" => raw.clone_into(&mut self.gst_sac_code),
            "supplier_legal_name" => raw.clone_into(&mut self.supplier_legal_name),
            "supplier_address_lines" => self.supplier_address_lines = parse::str_tuple(raw)?,
            "supplier_gstin" => raw.clone_into(&mut self.supplier_gstin),
            "supplier_state" => raw.clone_into(&mut self.supplier_state),
            "invoice_series_prefix" => raw.clone_into(&mut self.invoice_series_prefix),
            "invoice_number_width" => self.invoice_number_width = parse::integer(raw)?,
            "invoice_object_prefix" => raw.clone_into(&mut self.invoice_object_prefix),
            "invoice_local_dir" => raw.clone_into(&mut self.invoice_local_dir),
            "backtest_user_concurrency" => self.backtest_user_concurrency = parse::integer(raw)?,
            "backtest_global_concurrency" => {
                self.backtest_global_concurrency = parse::integer(raw)?
            }
            "public_api_enabled" => self.public_api_enabled = parse::boolean(raw)?,
            "public_api_contact_email" => raw.clone_into(&mut self.public_api_contact_email),
            "public_api_base_url" => raw.clone_into(&mut self.public_api_base_url),
            "redoc_script_url" => raw.clone_into(&mut self.redoc_script_url),
            "api_key_max_rate_limit_per_minute" => {
                self.api_key_max_rate_limit_per_minute = parse::integer(raw)?
            }
            "api_key_default_ttl_days" => {
                self.api_key_default_ttl_days = Some(parse::integer(raw)?)
            }
            "alert_lookback_days" => self.alert_lookback_days = parse::integer(raw)?,
            "alert_min_move" => self.alert_min_move = parse::integer(raw)?,
            "alert_max_rows" => self.alert_max_rows = parse::integer(raw)?,
            "webhook_signing_secret" => self.webhook_signing_secret = Secret::new(raw),
            "webhook_timeout_seconds" => self.webhook_timeout_seconds = parse::float(raw)?,
            "webhook_max_attempts" => self.webhook_max_attempts = parse::integer(raw)?,
            "webhook_backoff_base_seconds" => {
                self.webhook_backoff_base_seconds = parse::integer(raw)?
            }
            "webhook_backoff_factor" => self.webhook_backoff_factor = parse::integer(raw)?,
            "webhook_failure_threshold" => self.webhook_failure_threshold = parse::integer(raw)?,
            "swing_execution_enabled" => self.swing_execution_enabled = parse::boolean(raw)?,
            "swing_monitor_enabled" => self.swing_monitor_enabled = parse::boolean(raw)?,
            "swing_ep_premarket_enabled" => self.swing_ep_premarket_enabled = parse::boolean(raw)?,
            "swing_risk_per_trade_pct_max" => {
                self.swing_risk_per_trade_pct_max = parse::decimal(raw)?
            }
            "swing_max_position_pct_max" => self.swing_max_position_pct_max = parse::decimal(raw)?,
            "swing_max_open_positions_max" => {
                self.swing_max_open_positions_max = parse::integer(raw)?
            }
            "swing_index_slug" => raw.clone_into(&mut self.swing_index_slug),
            "swing_scan_min_interval_seconds" => {
                self.swing_scan_min_interval_seconds = parse::integer(raw)?
            }
            "swing_scan_stale_after_seconds" => {
                self.swing_scan_stale_after_seconds = parse::integer(raw)?
            }
            "vbt_execution_enabled" => self.vbt_execution_enabled = parse::boolean(raw)?,
            "vbt_nightly_enabled" => self.vbt_nightly_enabled = parse::boolean(raw)?,
            "vbt_max_open_positions_max" => self.vbt_max_open_positions_max = parse::integer(raw)?,
            "vbt_max_position_pct_max" => self.vbt_max_position_pct_max = parse::decimal(raw)?,
            "vbt_stop_pct_max" => self.vbt_stop_pct_max = parse::decimal(raw)?,
            "vbt_scan_min_interval_seconds" => {
                self.vbt_scan_min_interval_seconds = parse::integer(raw)?
            }
            "vbt_scan_stale_after_seconds" => {
                self.vbt_scan_stale_after_seconds = parse::integer(raw)?
            }
            "twt_execution_enabled" => self.twt_execution_enabled = parse::boolean(raw)?,
            "twt_nightly_enabled" => self.twt_nightly_enabled = parse::boolean(raw)?,
            "twt_max_open_positions_max" => self.twt_max_open_positions_max = parse::integer(raw)?,
            "twt_max_position_pct_max" => self.twt_max_position_pct_max = parse::decimal(raw)?,
            "twt_stop_pct_max" => self.twt_stop_pct_max = parse::decimal(raw)?,
            "twt_trail_pct_min" => self.twt_trail_pct_min = parse::decimal(raw)?,
            "twt_scan_min_interval_seconds" => {
                self.twt_scan_min_interval_seconds = parse::integer(raw)?
            }
            "twt_scan_stale_after_seconds" => {
                self.twt_scan_stale_after_seconds = parse::integer(raw)?
            }
            "options_o1m_execution_enabled" => {
                self.options_o1m_execution_enabled = parse::boolean(raw)?
            }
            "options_o1w_execution_enabled" => {
                self.options_o1w_execution_enabled = parse::boolean(raw)?
            }
            "options_o2_execution_enabled" => {
                self.options_o2_execution_enabled = parse::boolean(raw)?
            }
            "options_o3_execution_enabled" => {
                self.options_o3_execution_enabled = parse::boolean(raw)?
            }
            "options_monitor_enabled" => self.options_monitor_enabled = parse::boolean(raw)?,
            "options_collect_enabled" => self.options_collect_enabled = parse::boolean(raw)?,
            "options_scan_enabled" => self.options_scan_enabled = parse::boolean(raw)?,
            "options_risk_per_trade_inr_max" => {
                self.options_risk_per_trade_inr_max = parse::decimal(raw)?
            }
            "options_risk_pct_max" => self.options_risk_pct_max = parse::decimal(raw)?,
            "options_max_lots_max" => self.options_max_lots_max = parse::integer(raw)?,
            "options_book_daily_loss_inr_max" => {
                self.options_book_daily_loss_inr_max = parse::decimal(raw)?
            }
            "options_book_monthly_loss_inr_max" => {
                self.options_book_monthly_loss_inr_max = parse::decimal(raw)?
            }
            "options_hard_exit_latest" => self.options_hard_exit_latest = parse::time(raw)?,
            "fno_f1_execution_enabled" => self.fno_f1_execution_enabled = parse::boolean(raw)?,
            "fno_f2_execution_enabled" => self.fno_f2_execution_enabled = parse::boolean(raw)?,
            "fno_f3_execution_enabled" => self.fno_f3_execution_enabled = parse::boolean(raw)?,
            "fno_carry_enabled" => self.fno_carry_enabled = parse::boolean(raw)?,
            "fno_scan_enabled" => self.fno_scan_enabled = parse::boolean(raw)?,
            "fno_monitor_enabled" => self.fno_monitor_enabled = parse::boolean(raw)?,
            "fno_risk_per_trade_inr_max" => self.fno_risk_per_trade_inr_max = parse::decimal(raw)?,
            "fno_risk_pct_max" => self.fno_risk_pct_max = parse::decimal(raw)?,
            "fno_max_open_positions_max" => self.fno_max_open_positions_max = parse::integer(raw)?,
            "fno_max_per_underlying_max" => self.fno_max_per_underlying_max = parse::integer(raw)?,
            "fno_book_monthly_loss_inr_max" => {
                self.fno_book_monthly_loss_inr_max = parse::decimal(raw)?
            }
            "rate_limit_anonymous_per_minute" => {
                self.rate_limit_anonymous_per_minute = parse::integer(raw)?
            }
            "rate_limit_authenticated_per_minute" => {
                self.rate_limit_authenticated_per_minute = parse::integer(raw)?
            }
            "rate_limit_api_key_per_minute" => {
                self.rate_limit_api_key_per_minute = parse::integer(raw)?
            }
            "rate_limit_auth_per_minute" => self.rate_limit_auth_per_minute = parse::integer(raw)?,
            "rate_limit_webhook_per_minute" => {
                self.rate_limit_webhook_per_minute = parse::integer(raw)?
            }
            "rate_limit_enabled" => self.rate_limit_enabled = parse::boolean(raw)?,
            "db_pool_size" => self.db_pool_size = parse::integer(raw)?,
            "db_max_overflow" => self.db_max_overflow = parse::integer(raw)?,
            "db_pool_timeout_seconds" => self.db_pool_timeout_seconds = parse::float(raw)?,
            "db_pool_recycle_seconds" => self.db_pool_recycle_seconds = parse::integer(raw)?,
            "db_statement_cache_size" => self.db_statement_cache_size = parse::integer(raw)?,
            "http_stale_while_revalidate_seconds" => {
                self.http_stale_while_revalidate_seconds = parse::integer(raw)?
            }
            "otel_enabled" => self.otel_enabled = parse::boolean(raw)?,
            "otel_service_name" => raw.clone_into(&mut self.otel_service_name),
            "otel_exporter_otlp_endpoint" => {
                self.otel_exporter_otlp_endpoint = Some(raw.to_owned())
            }
            "log_level" => raw.clone_into(&mut self.log_level),
            "log_json" => self.log_json = parse::boolean(raw)?,
            "sentry_dsn" => self.sentry_dsn = Secret::new(raw),
            "sentry_sample_rate" => self.sentry_sample_rate = parse::float(raw)?,
            "release" => raw.clone_into(&mut self.release),
            "metrics_enabled" => self.metrics_enabled = parse::boolean(raw)?,
            "metrics_token" => self.metrics_token = Secret::new(raw),
            "ops_alert_email" => raw.clone_into(&mut self.ops_alert_email),
            "ops_alert_webhook_url" => raw.clone_into(&mut self.ops_alert_webhook_url),
            "publish_deadline_ist" => raw.clone_into(&mut self.publish_deadline_ist),
            "pipeline_stale_after_minutes" => {
                self.pipeline_stale_after_minutes = parse::integer(raw)?
            }
            "queue_backlog_threshold" => self.queue_backlog_threshold = parse::integer(raw)?,
            "kite_token_warning_hours" => self.kite_token_warning_hours = parse::integer(raw)?,
            "cors_origins" => self.cors_origins = parse::str_tuple(raw)?,
            _ => {}
        }
        Ok(())
    }

    /// The `Field(gt=..., ge=..., le=...)` constraints, every violation reported together.
    fn field_errors(&self) -> Vec<String> {
        let mut errors = Vec::new();
        bound::int(
            &mut errors,
            "backtest_concurrency",
            self.backtest_concurrency,
            &[Bound::Ge(1), Bound::Le(8)],
        );
        bound::int(
            &mut errors,
            "jwt_max_lifetime_seconds",
            self.jwt_max_lifetime_seconds,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "jwt_leeway_seconds",
            self.jwt_leeway_seconds,
            &[Bound::Ge(0)],
        );
        bound::int(
            &mut errors,
            "access_token_ttl_seconds",
            self.access_token_ttl_seconds,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "refresh_token_ttl_days",
            self.refresh_token_ttl_days,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "otp_length",
            self.otp_length,
            &[Bound::Ge(6), Bound::Le(10)],
        );
        bound::int(
            &mut errors,
            "otp_ttl_seconds",
            self.otp_ttl_seconds,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "otp_max_attempts",
            self.otp_max_attempts,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "password_reset_ttl_seconds",
            self.password_reset_ttl_seconds,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "email_verification_ttl_seconds",
            self.email_verification_ttl_seconds,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "auth_max_failures",
            self.auth_max_failures,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "auth_lockout_minutes",
            self.auth_lockout_minutes,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "auth_failure_window_minutes",
            self.auth_failure_window_minutes,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "argon2_time_cost",
            self.argon2_time_cost,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "argon2_memory_kib",
            self.argon2_memory_kib,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "argon2_parallelism",
            self.argon2_parallelism,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "account_purge_after_days",
            self.account_purge_after_days,
            &[Bound::Gt(0)],
        );
        bound::int(&mut errors, "smtp_port", self.smtp_port, &[Bound::Gt(0)]);
        bound::int(
            &mut errors,
            "razorpay_subscription_cycles_monthly",
            self.razorpay_subscription_cycles_monthly,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "razorpay_subscription_cycles_yearly",
            self.razorpay_subscription_cycles_yearly,
            &[Bound::Gt(0)],
        );
        bound::float(
            &mut errors,
            "razorpay_timeout_seconds",
            self.razorpay_timeout_seconds,
            &[Bound::GtF(0.0)],
        );
        bound::decimal(
            &mut errors,
            "gst_rate_percent",
            self.gst_rate_percent,
            &[(Cmp::Ge, "0")],
        );
        bound::int(
            &mut errors,
            "invoice_number_width",
            self.invoice_number_width,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "backtest_user_concurrency",
            self.backtest_user_concurrency,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "backtest_global_concurrency",
            self.backtest_global_concurrency,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "api_key_max_rate_limit_per_minute",
            self.api_key_max_rate_limit_per_minute,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "alert_lookback_days",
            self.alert_lookback_days,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "alert_min_move",
            self.alert_min_move,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "alert_max_rows",
            self.alert_max_rows,
            &[Bound::Gt(0)],
        );
        bound::float(
            &mut errors,
            "webhook_timeout_seconds",
            self.webhook_timeout_seconds,
            &[Bound::GtF(0.0)],
        );
        bound::int(
            &mut errors,
            "webhook_max_attempts",
            self.webhook_max_attempts,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "webhook_backoff_base_seconds",
            self.webhook_backoff_base_seconds,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "webhook_backoff_factor",
            self.webhook_backoff_factor,
            &[Bound::Gt(1)],
        );
        bound::int(
            &mut errors,
            "webhook_failure_threshold",
            self.webhook_failure_threshold,
            &[Bound::Gt(0)],
        );
        bound::decimal(
            &mut errors,
            "swing_risk_per_trade_pct_max",
            self.swing_risk_per_trade_pct_max,
            &[(Cmp::Gt, "0"), (Cmp::Le, "5")],
        );
        bound::decimal(
            &mut errors,
            "swing_max_position_pct_max",
            self.swing_max_position_pct_max,
            &[(Cmp::Gt, "0"), (Cmp::Le, "100")],
        );
        bound::int(
            &mut errors,
            "swing_max_open_positions_max",
            self.swing_max_open_positions_max,
            &[Bound::Gt(0), Bound::Le(50)],
        );
        bound::int(
            &mut errors,
            "swing_scan_min_interval_seconds",
            self.swing_scan_min_interval_seconds,
            &[Bound::Ge(0)],
        );
        bound::int(
            &mut errors,
            "swing_scan_stale_after_seconds",
            self.swing_scan_stale_after_seconds,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "vbt_max_open_positions_max",
            self.vbt_max_open_positions_max,
            &[Bound::Gt(0), Bound::Le(30)],
        );
        bound::decimal(
            &mut errors,
            "vbt_max_position_pct_max",
            self.vbt_max_position_pct_max,
            &[(Cmp::Gt, "0"), (Cmp::Le, "100")],
        );
        bound::decimal(
            &mut errors,
            "vbt_stop_pct_max",
            self.vbt_stop_pct_max,
            &[(Cmp::Gt, "0"), (Cmp::Le, "50")],
        );
        bound::int(
            &mut errors,
            "vbt_scan_min_interval_seconds",
            self.vbt_scan_min_interval_seconds,
            &[Bound::Ge(0)],
        );
        bound::int(
            &mut errors,
            "vbt_scan_stale_after_seconds",
            self.vbt_scan_stale_after_seconds,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "twt_max_open_positions_max",
            self.twt_max_open_positions_max,
            &[Bound::Gt(0), Bound::Le(30)],
        );
        bound::decimal(
            &mut errors,
            "twt_max_position_pct_max",
            self.twt_max_position_pct_max,
            &[(Cmp::Gt, "0"), (Cmp::Le, "100")],
        );
        bound::decimal(
            &mut errors,
            "twt_stop_pct_max",
            self.twt_stop_pct_max,
            &[(Cmp::Gt, "0"), (Cmp::Le, "50")],
        );
        bound::decimal(
            &mut errors,
            "twt_trail_pct_min",
            self.twt_trail_pct_min,
            &[(Cmp::Gt, "0"), (Cmp::Le, "100")],
        );
        bound::int(
            &mut errors,
            "twt_scan_min_interval_seconds",
            self.twt_scan_min_interval_seconds,
            &[Bound::Ge(0)],
        );
        bound::int(
            &mut errors,
            "twt_scan_stale_after_seconds",
            self.twt_scan_stale_after_seconds,
            &[Bound::Gt(0)],
        );
        bound::decimal(
            &mut errors,
            "options_risk_per_trade_inr_max",
            self.options_risk_per_trade_inr_max,
            &[(Cmp::Gt, "0")],
        );
        bound::decimal(
            &mut errors,
            "options_risk_pct_max",
            self.options_risk_pct_max,
            &[(Cmp::Gt, "0"), (Cmp::Le, "100")],
        );
        bound::int(
            &mut errors,
            "options_max_lots_max",
            self.options_max_lots_max,
            &[Bound::Gt(0), Bound::Le(100)],
        );
        bound::decimal(
            &mut errors,
            "options_book_daily_loss_inr_max",
            self.options_book_daily_loss_inr_max,
            &[(Cmp::Gt, "0")],
        );
        bound::decimal(
            &mut errors,
            "options_book_monthly_loss_inr_max",
            self.options_book_monthly_loss_inr_max,
            &[(Cmp::Gt, "0")],
        );
        bound::decimal(
            &mut errors,
            "fno_risk_per_trade_inr_max",
            self.fno_risk_per_trade_inr_max,
            &[(Cmp::Gt, "0")],
        );
        bound::decimal(
            &mut errors,
            "fno_risk_pct_max",
            self.fno_risk_pct_max,
            &[(Cmp::Gt, "0"), (Cmp::Le, "100")],
        );
        bound::int(
            &mut errors,
            "fno_max_open_positions_max",
            self.fno_max_open_positions_max,
            &[Bound::Gt(0), Bound::Le(100)],
        );
        bound::int(
            &mut errors,
            "fno_max_per_underlying_max",
            self.fno_max_per_underlying_max,
            &[Bound::Gt(0), Bound::Le(10)],
        );
        bound::decimal(
            &mut errors,
            "fno_book_monthly_loss_inr_max",
            self.fno_book_monthly_loss_inr_max,
            &[(Cmp::Gt, "0")],
        );
        bound::int(
            &mut errors,
            "rate_limit_anonymous_per_minute",
            self.rate_limit_anonymous_per_minute,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "rate_limit_authenticated_per_minute",
            self.rate_limit_authenticated_per_minute,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "rate_limit_api_key_per_minute",
            self.rate_limit_api_key_per_minute,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "rate_limit_auth_per_minute",
            self.rate_limit_auth_per_minute,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "rate_limit_webhook_per_minute",
            self.rate_limit_webhook_per_minute,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "db_pool_size",
            self.db_pool_size,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "db_max_overflow",
            self.db_max_overflow,
            &[Bound::Ge(0)],
        );
        bound::float(
            &mut errors,
            "db_pool_timeout_seconds",
            self.db_pool_timeout_seconds,
            &[Bound::GtF(0.0)],
        );
        bound::int(
            &mut errors,
            "db_pool_recycle_seconds",
            self.db_pool_recycle_seconds,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "db_statement_cache_size",
            self.db_statement_cache_size,
            &[Bound::Ge(0)],
        );
        bound::int(
            &mut errors,
            "http_stale_while_revalidate_seconds",
            self.http_stale_while_revalidate_seconds,
            &[Bound::Ge(0)],
        );
        bound::float(
            &mut errors,
            "sentry_sample_rate",
            self.sentry_sample_rate,
            &[Bound::GeF(0.0), Bound::LeF(1.0)],
        );
        bound::int(
            &mut errors,
            "pipeline_stale_after_minutes",
            self.pipeline_stale_after_minutes,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "queue_backlog_threshold",
            self.queue_backlog_threshold,
            &[Bound::Gt(0)],
        );
        bound::int(
            &mut errors,
            "kite_token_warning_hours",
            self.kite_token_warning_hours,
            &[Bound::Gt(0)],
        );
        errors
    }
}
