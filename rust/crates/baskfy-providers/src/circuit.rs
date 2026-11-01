//! A per-provider circuit breaker. Ports `baskfy_providers.circuit`.
//!
//! The point is not to make a failing provider work; it is to stop one from consuming the
//! nightly window — 2,300 instruments × 5 retries × backoff against a dead upstream would still
//! be running when the market reopens.
//!
//! ```text
//! CLOSED     calls pass through; consecutive failures are counted
//! OPEN       calls are refused with CircuitOpen until the reset interval elapses
//! HALF_OPEN  one probe is allowed; success closes the circuit, failure re-opens it
//! ```
//!
//! Only transient errors trip it: a malformed payload or a missing credential is a fact about
//! the request, not evidence the provider is down.

use std::future::Future;
use std::sync::{Arc, Mutex};

use crate::clock::{Clock, SystemClock};
use crate::errors::ProviderError;
use crate::py::repr_float;

/// `CircuitState` (a `StrEnum`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum CircuitState {
    /// `closed`.
    Closed,
    /// `open`.
    Open,
    /// `half_open`.
    HalfOpen,
}

impl CircuitState {
    /// The enum's string value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Closed => "closed",
            Self::Open => "open",
            Self::HalfOpen => "half_open",
        }
    }
}

#[derive(Debug, Default)]
struct Inner {
    failures: u32,
    opened_at: Option<f64>,
}

/// Trips after `failure_threshold` consecutive transient failures (`CircuitBreaker`).
pub struct CircuitBreaker {
    provider: String,
    failure_threshold: u32,
    reset_seconds: f64,
    clock: Arc<dyn Clock>,
    inner: Mutex<Inner>,
}

impl std::fmt::Debug for CircuitBreaker {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CircuitBreaker")
            .field("provider", &self.provider)
            .field("failure_threshold", &self.failure_threshold)
            .field("reset_seconds", &self.reset_seconds)
            .finish_non_exhaustive()
    }
}

impl CircuitBreaker {
    /// A breaker on the system monotonic clock, with Python's defaults (5 failures, 60 s).
    ///
    /// # Errors
    /// As [`CircuitBreaker::with_clock`].
    pub fn new(
        provider: &str,
        failure_threshold: u32,
        reset_seconds: f64,
    ) -> Result<Self, ProviderError> {
        Self::with_clock(
            provider,
            failure_threshold,
            reset_seconds,
            Arc::new(SystemClock),
        )
    }

    /// A breaker on an injected clock.
    ///
    /// # Errors
    /// `ValueError` for a threshold below 1 or a non-positive reset interval.
    pub fn with_clock(
        provider: &str,
        failure_threshold: u32,
        reset_seconds: f64,
        clock: Arc<dyn Clock>,
    ) -> Result<Self, ProviderError> {
        if failure_threshold < 1 {
            return Err(ProviderError::value_error(format!(
                "failure_threshold must be at least 1; got {failure_threshold}"
            )));
        }
        if reset_seconds <= 0.0 {
            return Err(ProviderError::value_error(format!(
                "reset_seconds must be positive; got {}",
                repr_float(reset_seconds)
            )));
        }
        Ok(Self {
            provider: provider.to_owned(),
            failure_threshold,
            reset_seconds,
            clock,
            inner: Mutex::new(Inner::default()),
        })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// The provider this breaker guards.
    #[must_use]
    pub fn provider(&self) -> &str {
        &self.provider
    }

    /// Consecutive transient failures so far.
    #[must_use]
    pub fn failures(&self) -> u32 {
        self.lock().failures
    }

    /// The state now.
    #[must_use]
    pub fn state(&self) -> CircuitState {
        let opened_at = self.lock().opened_at;
        match opened_at {
            None => CircuitState::Closed,
            Some(at) if self.clock.monotonic() - at >= self.reset_seconds => CircuitState::HalfOpen,
            Some(_) => CircuitState::Open,
        }
    }

    /// `retry_after`: seconds until a probe is admitted; `0.0` when it already would be.
    #[must_use]
    pub fn retry_after(&self) -> f64 {
        let opened_at = self.lock().opened_at;
        match opened_at {
            None => 0.0,
            Some(at) => {
                let remaining = self.reset_seconds - (self.clock.monotonic() - at);
                if remaining > 0.0 { remaining } else { 0.0 }
            }
        }
    }

    /// `call`: run `operation` under the breaker.
    ///
    /// # Errors
    /// `CircuitOpen` without running it while open; otherwise the operation's own error
    /// (a transient one counted first).
    pub async fn call<T, Fut>(&self, operation: Fut) -> Result<T, ProviderError>
    where
        Fut: Future<Output = Result<T, ProviderError>>,
    {
        if self.state() == CircuitState::Open {
            let failures = self.failures();
            return Err(ProviderError::circuit_open(
                &self.provider,
                failures,
                self.retry_after(),
            ));
        }
        match operation.await {
            Ok(value) => {
                self.record_success();
                Ok(value)
            }
            Err(error) => {
                if error.is_transient() {
                    self.record_failure();
                }
                Err(error)
            }
        }
    }

    fn record_failure(&self) {
        let now = self.clock.monotonic();
        let mut inner = self.lock();
        inner.failures += 1;
        if inner.failures >= self.failure_threshold {
            // Re-stamp on every failure so a failing half-open probe restarts the interval.
            inner.opened_at = Some(now);
        }
    }

    fn record_success(&self) {
        let mut inner = self.lock();
        inner.failures = 0;
        inner.opened_at = None;
    }
}
