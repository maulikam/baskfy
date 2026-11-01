//! Encrypted storage for the Kite access token. Ports `baskfy_providers.tokens`.
//!
//! docs/09: the token is *daily*, refreshed through the login flow, stored encrypted, and its
//! expiry is "the #1 pipeline failure" — so it alerts loudly. docs/11: "Kite access token
//! encrypted at rest."
//!
//! * **Expiry is computed, not trusted.** Kite invalidates at the next trading day's pre-open,
//!   so a token is stale once the IST calendar day has rolled over. A false "expired" costs one
//!   login; a false "valid" costs a night's pipeline.
//! * **A stale token is an error, never a return value.**
//!
//! The blob is Python's: a Fernet token (via the `fernet` crate's RustCrypto backend) over
//! `json.dumps({"version": 1, "access_token": ..., "issued_at": <isoformat>})`. Python and Rust
//! read each other's files (a test decrypts a blob Python wrote).

use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::{DateTime, FixedOffset, NaiveDate, Offset, Utc};

use crate::clock::{Clock, SystemClock};
use crate::errors::ProviderError;
use crate::py::json_str;
use crate::records::PyDateTime;
use crate::secret::Secret;

/// The payload version written into every blob.
pub const PAYLOAD_VERSION: i64 = 1;

/// `IST`: UTC+05:30, the timezone the token's lifetime is bounded by. India has no DST, so a
/// fixed offset is exact (no `chrono-tz`).
#[must_use]
pub fn ist() -> FixedOffset {
    // 19,800 s is always a valid offset; the fallback only keeps this total without a panic.
    FixedOffset::east_opt(19_800).unwrap_or_else(|| Utc.fix())
}

/// A Kite access token and the moment it was issued (`AccessToken`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccessToken {
    /// The token (never printed).
    pub value: Secret,
    /// When it was issued.
    pub issued_at: DateTime<FixedOffset>,
}

impl AccessToken {
    /// A token issued at `issued_at`.
    #[must_use]
    pub fn new(value: impl Into<Secret>, issued_at: DateTime<FixedOffset>) -> Self {
        Self {
            value: value.into(),
            issued_at,
        }
    }

    /// `is_expired(now=...)`: true once the IST calendar day of issue has passed.
    #[must_use]
    pub fn is_expired(&self, now: DateTime<FixedOffset>) -> bool {
        ist_date(now) > ist_date(self.issued_at)
    }

    /// The IST date it was issued on.
    #[must_use]
    pub fn issued_on(&self) -> NaiveDate {
        ist_date(self.issued_at)
    }
}

fn ist_date(moment: DateTime<FixedOffset>) -> NaiveDate {
    moment.with_timezone(&ist()).date_naive()
}

/// Reads and writes the Fernet-encrypted token blob (`AccessTokenStore`).
pub struct AccessTokenStore {
    path: PathBuf,
    encryption_key: Secret,
    clock: Arc<dyn Clock>,
}

impl std::fmt::Debug for AccessTokenStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AccessTokenStore")
            .field("path", &self.path)
            .field("encryption_key", &self.encryption_key)
            .finish_non_exhaustive()
    }
}

impl AccessTokenStore {
    /// A store at `path` keyed by `encryption_key`, on the system clock.
    #[must_use]
    pub fn new(path: impl Into<PathBuf>, encryption_key: Secret) -> Self {
        Self::with_clock(path, encryption_key, Arc::new(SystemClock))
    }

    /// A store with an injected clock (the default `issued_at` and `now`).
    #[must_use]
    pub fn with_clock(
        path: impl Into<PathBuf>,
        encryption_key: Secret,
        clock: Arc<dyn Clock>,
    ) -> Self {
        Self {
            path: path.into(),
            encryption_key,
            clock,
        }
    }

    /// Where the blob lives.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    fn now_ist(&self) -> DateTime<FixedOffset> {
        self.clock.now().with_timezone(&ist())
    }

    fn fernet(&self) -> Result<fernet::Fernet, ProviderError> {
        if !self.encryption_key.is_set() {
            return Err(ProviderError::credentials_missing(
                "BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set, so the access token cannot be \
                 decrypted. Generate one with `python -c \"from cryptography.fernet import \
                 Fernet; print(Fernet.generate_key().decode())\"` and store it as a secret.",
            )
            .with_provider("kite"));
        }
        fernet::Fernet::new(self.encryption_key.expose()).ok_or_else(|| {
            ProviderError::credentials_missing(
                "BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe \
                 base64-encoded bytes)",
            )
            .with_provider("kite")
        })
    }

    /// `exists`: is there a blob file?
    #[must_use]
    pub fn exists(&self) -> bool {
        self.path.is_file()
    }

    /// `save`: encrypt and persist a freshly obtained token, readable by its owner only.
    ///
    /// # Errors
    /// `ValueError` for an empty token; `CredentialsMissing` for a missing or invalid key;
    /// `OSError` when the file cannot be written.
    pub fn save(
        &self,
        token: &str,
        issued_at: Option<DateTime<FixedOffset>>,
    ) -> Result<AccessToken, ProviderError> {
        if token.is_empty() {
            return Err(ProviderError::value_error(
                "refusing to store an empty access token",
            ));
        }
        let record = AccessToken::new(token, issued_at.unwrap_or_else(|| self.now_ist()));
        let payload = format!(
            "{{\"version\": {PAYLOAD_VERSION}, \"access_token\": {}, \"issued_at\": {}}}",
            json_str(record.value.expose()),
            json_str(&PyDateTime::Aware(record.issued_at).isoformat())
        );
        if let Some(parent) = self.path.parent()
            && !parent.as_os_str().is_empty()
        {
            std::fs::create_dir_all(parent).map_err(|e| os_error(&e))?;
        }
        let blob = self.fernet()?.encrypt(payload.as_bytes());
        write_private(&self.path, blob.as_bytes()).map_err(|e| os_error(&e))?;
        Ok(record)
    }

    /// `load`: decrypt the stored token. Does not check expiry — see
    /// [`AccessTokenStore::require_fresh`].
    ///
    /// # Errors
    /// `CredentialsMissing` (no file, no key, rotated key); `UnexpectedPayload` (decrypts but is
    /// not a token record).
    pub fn load(&self) -> Result<AccessToken, ProviderError> {
        if !self.exists() {
            return Err(ProviderError::credentials_missing(format!(
                "no Kite access token at {}. Run the Kite login flow and store the result with \
                 `AccessTokenStore.save()`.",
                self.path.display()
            ))
            .with_provider("kite"));
        }
        let fernet = self.fernet()?;
        let blob = std::fs::read(&self.path).map_err(|e| os_error(&e))?;
        let rotated = || {
            ProviderError::credentials_missing(format!(
                "the token blob at {} could not be decrypted; the encryption key has probably \
                 been rotated. Re-run the login flow to write a fresh one.",
                self.path.display()
            ))
            .with_provider("kite")
        };
        let text = String::from_utf8(blob).map_err(|_| rotated())?;
        let decrypted = fernet.decrypt(text.trim_end()).map_err(|_| rotated())?;
        let not_a_record = || {
            ProviderError::unexpected_payload(format!(
                "the token blob at {} decrypted but is not a token record",
                self.path.display()
            ))
            .with_provider("kite")
        };
        let payload: serde_json::Value =
            serde_json::from_slice(&decrypted).map_err(|_| not_a_record())?;
        let serde_json::Value::Object(map) = payload else {
            return Err(ProviderError::foreign(
                "TypeError",
                "the decrypted token payload is not a JSON object",
            ));
        };
        let value = map.get("access_token").ok_or_else(not_a_record)?;
        let issued = map.get("issued_at").ok_or_else(not_a_record)?;
        let issued_at = match PyDateTime::parse(&py_str(issued)).map_err(|_| not_a_record())? {
            PyDateTime::Aware(dt) => dt,
            // Python's astimezone() reads a naive time as the box's local zone; both writers
            // always write an offset, so a naive one is read as IST (DECISIONS-RS R4.9).
            PyDateTime::Naive(naive) => naive
                .and_local_timezone(ist())
                .single()
                .ok_or_else(not_a_record)?,
        };
        Ok(AccessToken::new(py_str(value), issued_at))
    }

    /// `require_fresh`: a token known to be usable, or a loud `AccessTokenExpired`. The only
    /// accessor the Kite adapter uses. `now` defaults to the store's clock.
    ///
    /// # Errors
    /// As [`AccessTokenStore::load`], plus `AccessTokenExpired`.
    pub fn require_fresh(
        &self,
        now: Option<DateTime<FixedOffset>>,
    ) -> Result<AccessToken, ProviderError> {
        let token = self.load()?;
        let moment = now.unwrap_or_else(|| self.now_ist());
        if token.is_expired(moment) {
            return Err(ProviderError::access_token_expired(Some(&format!(
                "the Kite access token stored at {} was issued on {} and is no longer valid",
                self.path.display(),
                token.issued_on().format("%Y-%m-%d")
            ))));
        }
        Ok(token)
    }
}

/// Python's `str(value)` for a decoded JSON value.
fn py_str(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Null => "None".to_owned(),
        serde_json::Value::Bool(true) => "True".to_owned(),
        serde_json::Value::Bool(false) => "False".to_owned(),
        other => other.to_string(),
    }
}

fn os_error(e: &std::io::Error) -> ProviderError {
    ProviderError::foreign("OSError", e.to_string())
}

#[cfg(unix)]
fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(bytes)?;
    // An existing file keeps its old mode through open(); set it, as Python's chmod does.
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    std::fs::write(path, bytes)
}
