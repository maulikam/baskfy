//! The seams Python injects as callables — `clock=time.monotonic`, `sleeper=time.sleep`,
//! `random.uniform` — as traits, so the rate limiter, retry loop, circuit breaker and token
//! store can be driven deterministically in tests. No Python twin.

use std::sync::OnceLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use chrono::{DateTime, Utc};

/// What a limiter or breaker reads the time from.
pub trait Clock: Send + Sync {
    /// `time.monotonic()`: seconds on a clock that never goes backwards.
    fn monotonic(&self) -> f64;

    /// `datetime.now(tz=UTC)`: the wall clock (token issue times, archive keys).
    fn now(&self) -> DateTime<Utc>;
}

/// How the code waits (`time.sleep`).
#[async_trait]
pub trait Sleeper: Send + Sync {
    /// Sleep `seconds`; a negative or NaN value sleeps not at all.
    async fn sleep(&self, seconds: f64);
}

/// Where jitter comes from (`random.random()`).
pub trait Random: Send + Sync {
    /// A float in `[0, 1)`.
    fn random(&self) -> f64;

    /// `random.uniform(a, b)`: `a + (b - a) * random()`, Python's formula.
    fn uniform(&self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.random()
    }
}

/// The production clock.
///
/// `monotonic` is `CLOCK_MONOTONIC`, the clock Python's `time.monotonic()` reads on Linux, so a
/// Rust and a Python process on one kernel (one container host) can share the Redis token bucket
/// that is stamped with it. On macOS Python reads `mach_absolute_time` instead, which stops
/// while the machine sleeps; a native-Mac Rust process and a native-Mac Python process would
/// disagree after a sleep (DECISIONS-RS R4.4).
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn monotonic(&self) -> f64 {
        system_monotonic()
    }

    fn now(&self) -> DateTime<Utc> {
        let since = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(Duration::ZERO);
        let secs = i64::try_from(since.as_secs()).unwrap_or(i64::MAX);
        DateTime::from_timestamp(secs, since.subsec_nanos()).unwrap_or(DateTime::UNIX_EPOCH)
    }
}

fn system_monotonic() -> f64 {
    timespec_seconds(rustix::time::clock_gettime(
        rustix::time::ClockId::Monotonic,
    ))
}

#[allow(clippy::cast_precision_loss)]
fn timespec_seconds(ts: rustix::time::Timespec) -> f64 {
    ts.tv_sec as f64 + ts.tv_nsec as f64 * 1e-9
}

/// The production sleeper: `tokio::time::sleep`.
#[derive(Debug, Clone, Copy, Default)]
pub struct TokioSleeper;

#[async_trait]
impl Sleeper for TokioSleeper {
    async fn sleep(&self, seconds: f64) {
        tokio::time::sleep(duration(seconds)).await;
    }
}

/// `seconds` as a `Duration`: negative and NaN are zero, beyond range is the maximum.
#[must_use]
pub fn duration(seconds: f64) -> Duration {
    if seconds.is_nan() || seconds <= 0.0 {
        return Duration::ZERO;
    }
    Duration::try_from_secs_f64(seconds).unwrap_or(Duration::MAX)
}

/// A clock on tokio's time: with a paused runtime (`start_paused`) it moves only when every
/// task sleeps, so concurrent limiter tests are exact and instant. Its wall clock is the Unix
/// epoch plus its monotonic reading.
#[derive(Debug, Clone, Copy)]
pub struct TokioClock {
    origin: tokio::time::Instant,
}

impl TokioClock {
    /// A clock reading zero now.
    #[must_use]
    pub fn new() -> Self {
        Self {
            origin: tokio::time::Instant::now(),
        }
    }
}

impl Default for TokioClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for TokioClock {
    fn monotonic(&self) -> f64 {
        tokio::time::Instant::now()
            .duration_since(self.origin)
            .as_secs_f64()
    }

    fn now(&self) -> DateTime<Utc> {
        let d = tokio::time::Instant::now().duration_since(self.origin);
        let secs = i64::try_from(d.as_secs()).unwrap_or(i64::MAX);
        DateTime::from_timestamp(secs, d.subsec_nanos()).unwrap_or(DateTime::UNIX_EPOCH)
    }
}

/// The production randomness: the thread-local generator.
#[derive(Debug, Clone, Copy, Default)]
pub struct ThreadRandom;

impl Random for ThreadRandom {
    fn random(&self) -> f64 {
        rand::random::<f64>()
    }
}

/// The process's single production clock instance.
#[must_use]
pub fn system_clock() -> &'static SystemClock {
    static CLOCK: OnceLock<SystemClock> = OnceLock::new();
    CLOCK.get_or_init(|| SystemClock)
}
