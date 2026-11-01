//! Typed provider failures. Ports `baskfy_providers.errors`.
//!
//! docs/09 §"Kite specifics": the daily access token expiring is "the #1 pipeline failure", and
//! it must "alert loudly". That only works if the failure is distinguishable — a pipeline that
//! catches one broad error cannot tell "renew the token" from "NSE published late" from "we are
//! being rate limited", and will retry all three identically.
//!
//! Python's class hierarchy becomes one [`ProviderError`] carrying an [`ErrorKind`]; the
//! `isinstance` questions are methods ([`ProviderError::is_transient`], ...):
//!
//! ```text
//! ProviderError
//! ├─ ProviderUnavailable        the provider cannot serve at all right now
//! │  ├─ CredentialsMissing      nothing configured; expected in local dev
//! │  ├─ AccessTokenExpired      configured but stale — LOUD, needs a human, never retried
//! │  └─ CircuitOpen             we stopped calling it on purpose
//! ├─ TransientProviderError     worth retrying
//! │  ├─ RateLimited             429, or our own limiter said no
//! │  └─ UpstreamUnavailable     5xx, timeout, connection reset
//! ├─ ProviderDataError          it answered, but the answer is unusable
//! │  ├─ UnexpectedPayload       shape we do not recognise
//! │  └─ ArchiveError            we could not archive a raw file, so parsing must not proceed
//! ├─ RetryBudgetExhausted       every retry used; carries the last error
//! └─ CapabilityNotAvailable     no configured provider offers the capability
//! ```
//!
//! [`ErrorKind::Foreign`] carries what Python raises that is **not** a `ProviderError` — a
//! `ValueError` from a config check, a `redis.ConnectionError` from the limiter. It exists so
//! those keep Python's semantics: retry does not retry them and the circuit breaker does not
//! count them, exactly as `except TransientProviderError` lets them through (DECISIONS-RS R4.3).

use std::borrow::Cow;
use std::fmt;

/// Python's exception class, for the `isinstance` checks and for messages that name it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ErrorKind {
    /// `ProviderError` itself.
    ProviderError,
    /// `ProviderUnavailable`.
    ProviderUnavailable,
    /// `CredentialsMissing` (a `ProviderUnavailable`).
    CredentialsMissing,
    /// `AccessTokenExpired` (a `ProviderUnavailable`).
    AccessTokenExpired,
    /// `CircuitOpen` (a `ProviderUnavailable`).
    CircuitOpen,
    /// `TransientProviderError`.
    TransientProviderError,
    /// `RateLimited` (a `TransientProviderError`).
    RateLimited,
    /// `UpstreamUnavailable` (a `TransientProviderError`).
    UpstreamUnavailable,
    /// `RetryBudgetExhausted`.
    RetryBudgetExhausted,
    /// `ProviderDataError`.
    ProviderDataError,
    /// `UnexpectedPayload` (a `ProviderDataError`).
    UnexpectedPayload,
    /// `ArchiveError` (a `ProviderDataError`).
    ArchiveError,
    /// `CapabilityNotAvailable`.
    CapabilityNotAvailable,
    /// A Python exception outside the `ProviderError` tree (its class is
    /// [`ProviderError::python_type`]).
    Foreign,
}

impl ErrorKind {
    /// The Python class name (`type(exc).__name__`); `None` for [`ErrorKind::Foreign`], whose
    /// name the error carries.
    #[must_use]
    pub const fn class_name(self) -> Option<&'static str> {
        Some(match self {
            Self::ProviderError => "ProviderError",
            Self::ProviderUnavailable => "ProviderUnavailable",
            Self::CredentialsMissing => "CredentialsMissing",
            Self::AccessTokenExpired => "AccessTokenExpired",
            Self::CircuitOpen => "CircuitOpen",
            Self::TransientProviderError => "TransientProviderError",
            Self::RateLimited => "RateLimited",
            Self::UpstreamUnavailable => "UpstreamUnavailable",
            Self::RetryBudgetExhausted => "RetryBudgetExhausted",
            Self::ProviderDataError => "ProviderDataError",
            Self::UnexpectedPayload => "UnexpectedPayload",
            Self::ArchiveError => "ArchiveError",
            Self::CapabilityNotAvailable => "CapabilityNotAvailable",
            Self::Foreign => return None,
        })
    }
}

/// Kind-specific payload (Python's extra attributes).
#[derive(Debug)]
pub enum Detail {
    /// No extra attributes.
    None,
    /// `CircuitOpen.failures` / `.retry_after_seconds`.
    CircuitOpen {
        /// Consecutive failures when the breaker refused.
        failures: u32,
        /// Seconds until the breaker admits a probe.
        retry_after_seconds: f64,
    },
    /// `RetryBudgetExhausted.attempts` / `.last_error`.
    RetryBudgetExhausted {
        /// Attempts made (the policy's `max_attempts`).
        attempts: u32,
        /// The final underlying error.
        last_error: Box<ProviderError>,
    },
    /// The class of a Python exception outside the provider tree (`ValueError`, ...).
    Foreign {
        /// `type(exc).__name__`.
        python_type: Cow<'static, str>,
    },
}

/// Ports `baskfy_providers.errors.ProviderError` and its subclasses.
#[derive(Debug)]
pub struct ProviderError {
    kind: ErrorKind,
    message: String,
    provider: Option<String>,
    detail: Detail,
    source: Option<Box<dyn std::error::Error + Send + Sync + 'static>>,
}

impl ProviderError {
    /// A provider error of `kind` (`Kind(message)` in Python, `provider=None`).
    #[must_use]
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            provider: None,
            detail: Detail::None,
            source: None,
        }
    }

    /// Sets `provider=` (the `[provider]` prefix in the message). An empty name is no prefix,
    /// as Python's truthiness test makes it.
    #[must_use]
    pub fn with_provider(mut self, provider: impl Into<String>) -> Self {
        self.provider = Some(provider.into());
        self
    }

    /// Python's `raise ... from exc`: keeps the cause for `source()`.
    #[must_use]
    pub fn with_source(mut self, source: impl std::error::Error + Send + Sync + 'static) -> Self {
        self.source = Some(Box::new(source));
        self
    }

    /// `CredentialsMissing(message, provider=...)`.
    #[must_use]
    pub fn credentials_missing(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::CredentialsMissing, message)
    }

    /// `ProviderUnavailable(message)`.
    #[must_use]
    pub fn provider_unavailable(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::ProviderUnavailable, message)
    }

    /// `AccessTokenExpired(message)`: Python's constructor appends the login-flow instruction
    /// and defaults `provider="kite"` (pass `None` for the default message).
    #[must_use]
    pub fn access_token_expired(message: Option<&str>) -> Self {
        let base = message.unwrap_or("Kite access token has expired");
        Self::new(
            ErrorKind::AccessTokenExpired,
            format!(
                "{base}. Regenerate it through the Kite login flow and store it with \
                 `AccessTokenStore.save()`; the nightly pipeline cannot fetch bars until then."
            ),
        )
        .with_provider("kite")
    }

    /// `CircuitOpen(provider, failures, retry_after_seconds)`.
    #[must_use]
    pub fn circuit_open(provider: &str, failures: u32, retry_after_seconds: f64) -> Self {
        let mut error = Self::new(
            ErrorKind::CircuitOpen,
            format!(
                "circuit is open after {failures} consecutive failures; retrying in {}s",
                crate::py::fixed(retry_after_seconds, 1)
            ),
        )
        .with_provider(provider);
        error.detail = Detail::CircuitOpen {
            failures,
            retry_after_seconds,
        };
        error
    }

    /// `TransientProviderError(message)`.
    #[must_use]
    pub fn transient(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::TransientProviderError, message)
    }

    /// `RateLimited(message)`.
    #[must_use]
    pub fn rate_limited(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::RateLimited, message)
    }

    /// `UpstreamUnavailable(message)`.
    #[must_use]
    pub fn upstream_unavailable(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::UpstreamUnavailable, message)
    }

    /// `RetryBudgetExhausted(attempts, last_error, provider=...)`.
    #[must_use]
    pub fn retry_budget_exhausted(attempts: u32, last_error: Self, provider: Option<&str>) -> Self {
        let message = format!(
            "gave up after {attempts} attempt(s); last error: {}: {last_error}",
            last_error.python_type()
        );
        let mut error = Self::new(ErrorKind::RetryBudgetExhausted, message);
        if let Some(p) = provider {
            error = error.with_provider(p);
        }
        error.detail = Detail::RetryBudgetExhausted {
            attempts,
            last_error: Box::new(last_error),
        };
        error
    }

    /// `ProviderDataError(message)`.
    #[must_use]
    pub fn data_error(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::ProviderDataError, message)
    }

    /// `UnexpectedPayload(message)`.
    #[must_use]
    pub fn unexpected_payload(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::UnexpectedPayload, message)
    }

    /// `ArchiveError(message)`.
    #[must_use]
    pub fn archive_error(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::ArchiveError, message)
    }

    /// `CapabilityNotAvailable(message)`.
    #[must_use]
    pub fn capability_not_available(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::CapabilityNotAvailable, message)
    }

    /// A Python exception outside the provider tree, e.g. `foreign("ValueError", ...)`.
    #[must_use]
    pub fn foreign(python_type: impl Into<Cow<'static, str>>, message: impl Into<String>) -> Self {
        let mut error = Self::new(ErrorKind::Foreign, message);
        error.detail = Detail::Foreign {
            python_type: python_type.into(),
        };
        error
    }

    /// Python's `ValueError(message)`.
    #[must_use]
    pub fn value_error(message: impl Into<String>) -> Self {
        Self::foreign("ValueError", message)
    }

    /// The kind (Python class).
    #[must_use]
    pub const fn kind(&self) -> ErrorKind {
        self.kind
    }

    /// The message without the `[provider]` prefix (`super().__str__()`).
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }

    /// `exc.provider`.
    #[must_use]
    pub fn provider(&self) -> Option<&str> {
        self.provider.as_deref()
    }

    /// Kind-specific attributes.
    #[must_use]
    pub const fn detail(&self) -> &Detail {
        &self.detail
    }

    /// `type(exc).__name__`.
    #[must_use]
    pub fn python_type(&self) -> &str {
        match (&self.detail, self.kind.class_name()) {
            (Detail::Foreign { python_type }, _) => python_type,
            (_, Some(name)) => name,
            (_, None) => "Exception",
        }
    }

    /// `RetryBudgetExhausted.last_error`.
    #[must_use]
    pub fn last_error(&self) -> Option<&Self> {
        match &self.detail {
            Detail::RetryBudgetExhausted { last_error, .. } => Some(last_error),
            _ => None,
        }
    }

    /// `RetryBudgetExhausted.attempts`.
    #[must_use]
    pub const fn attempts(&self) -> Option<u32> {
        match &self.detail {
            Detail::RetryBudgetExhausted { attempts, .. } => Some(*attempts),
            _ => None,
        }
    }

    /// `isinstance(exc, ProviderError)` — false only for [`ErrorKind::Foreign`].
    #[must_use]
    pub const fn is_provider_error(&self) -> bool {
        !matches!(self.kind, ErrorKind::Foreign)
    }

    /// `isinstance(exc, ProviderUnavailable)`.
    #[must_use]
    pub const fn is_unavailable(&self) -> bool {
        matches!(
            self.kind,
            ErrorKind::ProviderUnavailable
                | ErrorKind::CredentialsMissing
                | ErrorKind::AccessTokenExpired
                | ErrorKind::CircuitOpen
        )
    }

    /// `isinstance(exc, TransientProviderError)` — the only errors retry retries and the circuit
    /// breaker counts.
    #[must_use]
    pub const fn is_transient(&self) -> bool {
        matches!(
            self.kind,
            ErrorKind::TransientProviderError
                | ErrorKind::RateLimited
                | ErrorKind::UpstreamUnavailable
        )
    }

    /// `isinstance(exc, ProviderDataError)`.
    #[must_use]
    pub const fn is_data_error(&self) -> bool {
        matches!(
            self.kind,
            ErrorKind::ProviderDataError | ErrorKind::UnexpectedPayload | ErrorKind::ArchiveError
        )
    }
}

impl fmt::Display for ProviderError {
    /// `ProviderError.__str__`: `"[provider] message"` when a provider is named.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.provider.as_deref() {
            Some(p) if !p.is_empty() && self.is_provider_error() => {
                write!(f, "[{p}] {}", self.message)
            }
            _ => f.write_str(&self.message),
        }
    }
}

impl std::error::Error for ProviderError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match &self.detail {
            Detail::RetryBudgetExhausted { last_error, .. } => Some(last_error.as_ref()),
            _ => self
                .source
                .as_ref()
                .map(|s| s.as_ref() as &(dyn std::error::Error + 'static)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn str_carries_the_provider_prefix() {
        let e = ProviderError::upstream_unavailable("503").with_provider("kite");
        assert_eq!(e.to_string(), "[kite] 503");
        assert_eq!(
            ProviderError::upstream_unavailable("503").to_string(),
            "503"
        );
        assert_eq!(
            ProviderError::upstream_unavailable("503")
                .with_provider("")
                .to_string(),
            "503"
        );
    }

    #[test]
    fn the_hierarchy_answers_isinstance() {
        assert!(ProviderError::rate_limited("x").is_transient());
        assert!(ProviderError::access_token_expired(None).is_unavailable());
        assert!(!ProviderError::access_token_expired(None).is_transient());
        assert!(ProviderError::archive_error("x").is_data_error());
        assert!(!ProviderError::value_error("x").is_provider_error());
        assert_eq!(ProviderError::value_error("x").python_type(), "ValueError");
    }

    #[test]
    fn messages_match_python() {
        assert_eq!(
            ProviderError::circuit_open("nse", 5, 59.95).to_string(),
            "[nse] circuit is open after 5 consecutive failures; retrying in 60.0s"
        );
        let expired = ProviderError::access_token_expired(None);
        assert_eq!(
            expired.to_string(),
            "[kite] Kite access token has expired. Regenerate it through the Kite login flow and \
             store it with `AccessTokenStore.save()`; the nightly pipeline cannot fetch bars \
             until then."
        );
        let exhausted = ProviderError::retry_budget_exhausted(
            2,
            ProviderError::upstream_unavailable("503"),
            Some("kite"),
        );
        assert_eq!(
            exhausted.to_string(),
            "[kite] gave up after 2 attempt(s); last error: UpstreamUnavailable: 503"
        );
    }
}
