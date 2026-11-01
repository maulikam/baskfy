//! Exponential backoff with full jitter. Ports `baskfy_providers.retry`.
//!
//! Retries only transient errors ([`ProviderError::is_transient`]) — a 5xx, a timeout, a 429.
//! Everything else is returned at once: an expired token fails identically five times while
//! delaying the alert a human needs; a missing credential cannot fix itself; a malformed payload
//! stays malformed. Jitter is *full* — a uniform draw over `[0, backoff]` — so N workers that
//! failed together do not retry together.

use std::future::Future;
use std::sync::Arc;

use crate::clock::{Random, Sleeper, ThreadRandom, TokioSleeper};
use crate::errors::ProviderError;
use crate::py::repr_float;

/// Draws the delay from `[0, ceiling]` (`RetryHooks.jitter`).
pub type Jitter = Arc<dyn Fn(f64) -> f64 + Send + Sync>;

/// Observes each retry with `(attempt, delay, error)` before the sleep (`RetryHooks.on_retry`).
pub type OnRetry = Arc<dyn Fn(u32, f64, &ProviderError) + Send + Sync>;

/// The seams the retry loop is driven and observed through (`RetryHooks`).
#[derive(Clone)]
pub struct RetryHooks {
    /// How the loop sleeps.
    pub sleeper: Arc<dyn Sleeper>,
    /// Draws the delay; `None` is full jitter from [`RetryHooks::random`].
    pub jitter: Option<Jitter>,
    /// Called before each sleep.
    pub on_retry: Option<OnRetry>,
    /// The randomness behind the default jitter.
    pub random: Arc<dyn Random>,
}

impl Default for RetryHooks {
    fn default() -> Self {
        Self {
            sleeper: Arc::new(TokioSleeper),
            jitter: None,
            on_retry: None,
            random: Arc::new(ThreadRandom),
        }
    }
}

impl std::fmt::Debug for RetryHooks {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RetryHooks")
            .field("jitter", &self.jitter.is_some())
            .field("on_retry", &self.on_retry.is_some())
            .finish_non_exhaustive()
    }
}

impl RetryHooks {
    /// `draw`: the injected jitter, else `random.uniform(0.0, ceiling)`.
    #[must_use]
    pub fn draw(&self, ceiling: f64) -> f64 {
        match &self.jitter {
            Some(jitter) => jitter(ceiling),
            None => self.random.uniform(0.0, ceiling),
        }
    }
}

/// How many times, and how long between (`RetryPolicy`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RetryPolicy {
    /// Attempts in all, including the first (>= 1).
    pub max_attempts: u32,
    /// The first backoff ceiling (> 0).
    pub base_seconds: f64,
    /// The cap on any ceiling (>= base).
    pub max_seconds: f64,
}

impl Default for RetryPolicy {
    /// Python's defaults: 5 attempts, 0.5 s base, 30 s cap.
    fn default() -> Self {
        Self {
            max_attempts: 5,
            base_seconds: 0.5,
            max_seconds: 30.0,
        }
    }
}

impl RetryPolicy {
    /// A validated policy (`__post_init__`).
    ///
    /// # Errors
    /// `ValueError` with Python's messages.
    pub fn new(
        max_attempts: u32,
        base_seconds: f64,
        max_seconds: f64,
    ) -> Result<Self, ProviderError> {
        if max_attempts < 1 {
            return Err(ProviderError::value_error(format!(
                "max_attempts must be at least 1; got {max_attempts}"
            )));
        }
        if base_seconds <= 0.0 {
            return Err(ProviderError::value_error(format!(
                "base_seconds must be positive; got {}",
                repr_float(base_seconds)
            )));
        }
        if max_seconds < base_seconds {
            return Err(ProviderError::value_error(format!(
                "max_seconds ({}) cannot be below base_seconds ({})",
                repr_float(max_seconds),
                repr_float(base_seconds)
            )));
        }
        Ok(Self {
            max_attempts,
            base_seconds,
            max_seconds,
        })
    }

    /// `backoff_ceiling`: the upper bound of the jitter window before attempt `attempt`
    /// (1-based): `min(max_seconds, base_seconds * 2**(attempt-1))`.
    ///
    /// # Errors
    /// `ValueError` for attempt 0; `OverflowError` where Python's `float(2**n)` overflows.
    pub fn backoff_ceiling(&self, attempt: u32) -> Result<f64, ProviderError> {
        if attempt < 1 {
            return Err(ProviderError::value_error(format!(
                "attempt is 1-based; got {attempt}"
            )));
        }
        let exponent = attempt - 1;
        if exponent >= 1024 {
            return Err(ProviderError::foreign(
                "OverflowError",
                "int too large to convert to float",
            ));
        }
        let growth = 2.0_f64.powi(i32::try_from(exponent).unwrap_or(i32::MAX));
        Ok(self.max_seconds.min(self.base_seconds * growth))
    }
}

/// `call_with_retry`: run `operation`, retrying transient failures per `policy`.
///
/// # Errors
/// A non-transient error at once, unchanged; once the budget is spent,
/// `RetryBudgetExhausted` carrying the final underlying error (and `provider`).
pub async fn call_with_retry<T, F, Fut>(
    mut operation: F,
    policy: &RetryPolicy,
    provider: Option<&str>,
    hooks: Option<&RetryHooks>,
) -> Result<T, ProviderError>
where
    F: FnMut() -> Fut + Send,
    Fut: Future<Output = Result<T, ProviderError>> + Send,
{
    let default_hooks;
    let seams = match hooks {
        Some(h) => h,
        None => {
            default_hooks = RetryHooks::default();
            &default_hooks
        }
    };
    let mut last_error: Option<ProviderError> = None;
    for attempt in 1..=policy.max_attempts {
        match operation().await {
            Ok(value) => return Ok(value),
            Err(error) if error.is_transient() => {
                if attempt == policy.max_attempts {
                    last_error = Some(error);
                    break;
                }
                let delay = seams.draw(policy.backoff_ceiling(attempt)?);
                if let Some(on_retry) = &seams.on_retry {
                    on_retry(attempt, delay, &error);
                }
                last_error = Some(error);
                seams.sleeper.sleep(delay).await;
            }
            Err(error) => return Err(error),
        }
    }
    match last_error {
        Some(error) => Err(ProviderError::retry_budget_exhausted(
            policy.max_attempts,
            error,
            provider,
        )),
        None => Err(ProviderError::foreign(
            "RuntimeError",
            "retry loop exited without running the operation",
        )),
    }
}
