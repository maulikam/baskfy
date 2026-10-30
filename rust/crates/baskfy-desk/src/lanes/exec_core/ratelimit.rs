//! Ports `baskfy_execution.ratelimit` (the desk's `app/core/ratelimit.py`): the ORDER path's
//! limiter, tuned to Kite's caps — 10 req/s API, 10 orders/s, 400/min, 3,000/day (planned to
//! the safer 3,000).
//!
//! **The per-second cap is spacing, not a token bucket** (18 Aug 2026: a bucket of 9 starting
//! full granted 18 in the first second; eleven of twenty-one orders came back "Maximum allowed
//! order requests per second exceeded"). Consecutive grants are at least `1/rate` apart, so no
//! one-second window can hold more than nine. The minute and day windows stay buckets, where a
//! burst is harmless.
//!
//! Time is `tokio::time` (Python's `time.monotonic` + `asyncio.sleep`), so a test on a paused
//! runtime measures the spacing without spending the seconds.

use async_trait::async_trait;
use tokio::sync::Mutex;
use tokio::time::{Duration, Instant};

/// `asyncio.sleep(secs)`. `Duration::from_secs_f64` panics on an infinite or overflowing
/// value (a `Spacer(0)` interval, a zero-capacity bucket); such a wait sleeps "forever" — the
/// call is never granted, which is what the arithmetic says — instead of panicking.
fn sleep_secs(secs: f64) -> tokio::time::Sleep {
    let d = Duration::try_from_secs_f64(secs.max(0.0)).unwrap_or(Duration::MAX);
    tokio::time::sleep(d)
}

/// `Bucket`: a token bucket for the LONG windows only (per minute, per day).
#[derive(Debug)]
pub struct Bucket {
    capacity: f64,
    per: f64,
    state: Mutex<(f64, Instant)>,
}

impl Bucket {
    /// `Bucket(capacity, per_seconds)`, starting full.
    #[must_use]
    pub fn new(capacity: u32, per_seconds: f64) -> Self {
        let capacity = f64::from(capacity);
        Self {
            capacity,
            per: per_seconds,
            state: Mutex::new((capacity, Instant::now())),
        }
    }

    /// `take()`: one token, waiting for the refill when empty.
    pub async fn take(&self) {
        loop {
            let wait = {
                let mut s = self.state.lock().await;
                let now = Instant::now();
                let elapsed = now.duration_since(s.1).as_secs_f64();
                s.0 = self.capacity.min(s.0 + elapsed * self.capacity / self.per);
                s.1 = now;
                if s.0 >= 1.0 {
                    s.0 -= 1.0;
                    return;
                }
                ((1.0 - s.0) * self.per / self.capacity).max(0.005)
            };
            sleep_secs(wait).await;
        }
    }
}

/// `Spacer`: a minimum interval between grants. Cannot burst. The lock serialises waiters so
/// two tasks cannot both observe the same free slot.
#[derive(Debug)]
pub struct Spacer {
    interval: f64,
    last: Mutex<Option<Instant>>,
}

impl Spacer {
    /// `Spacer(rate)`: `rate` grants per second.
    #[must_use]
    pub fn new(rate: f64) -> Self {
        Self {
            interval: 1.0 / rate,
            last: Mutex::new(None),
        }
    }

    /// `interval`.
    #[must_use]
    pub const fn interval(&self) -> f64 {
        self.interval
    }

    /// `take()`.
    pub async fn take(&self) {
        let mut last = self.last.lock().await;
        let mut now = Instant::now();
        if let Some(prev) = *last {
            let wait = self.interval - now.duration_since(prev).as_secs_f64();
            if wait > 0.0 {
                sleep_secs(wait).await;
                now = Instant::now();
            }
        }
        *last = Some(now);
    }
}

/// The two slots the gateway takes. A trait so a test can count or slow them (Python
/// monkeypatches `gw.limits.order_slot`).
#[async_trait]
pub trait GatewayLimits: Send + Sync {
    /// `order_slot()`: every order.
    async fn order_slot(&self);
    /// `api_slot()`: every GTT call, cancel and instrument dump.
    async fn api_slot(&self);
}

/// `KiteLimits`: 9/s spaced for the API and for orders (one in hand for clock skew, and under
/// SEBI's 10-OPS retail-algo threshold), 380/min and 2,900/day buckets.
#[derive(Debug)]
pub struct KiteLimits {
    /// `api`.
    pub api: Spacer,
    /// `orders_sec`.
    pub orders_sec: Spacer,
    /// `orders_min`.
    pub orders_min: Bucket,
    /// `orders_day`.
    pub orders_day: Bucket,
}

impl Default for KiteLimits {
    fn default() -> Self {
        Self {
            api: Spacer::new(9.0),
            orders_sec: Spacer::new(9.0),
            orders_min: Bucket::new(380, 60.0),
            orders_day: Bucket::new(2900, 86_400.0),
        }
    }
}

#[async_trait]
impl GatewayLimits for KiteLimits {
    async fn order_slot(&self) {
        self.api.take().await;
        self.orders_sec.take().await;
        self.orders_min.take().await;
        self.orders_day.take().await;
    }

    async fn api_slot(&self) {
        self.api.take().await;
    }
}
