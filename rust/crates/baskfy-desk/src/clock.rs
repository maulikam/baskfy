//! The clock every lane reads (DECISIONS-RS R8.1): the desk's Python reads `time.time()`,
//! `time.monotonic()`, `datetime.now(IST)` and `date.today()` directly; here they are one trait,
//! so a plan's 30-minute expiry, a session window (09:15–15:30) and a Friday rebalance can be
//! driven by `desk_diff.py`'s injected clock (pre-open, 09:16, 11:00, 15:29, after close) and by
//! tests.
//!
//! Production is [`SystemClock`]. [`FixedClock`] is settable and only advances when told to: the
//! harness (`--harness`) installs one and moves it through `POST /__harness/clock`, exactly as
//! `tools/parity/desk_harness.py` moves Python's patched `time.time` / `datetime.now`.

use std::sync::RwLock;
use std::time::{SystemTime, UNIX_EPOCH};

use chrono::{DateTime, FixedOffset, NaiveDate, TimeZone, Utc};

/// India Standard Time, `ZoneInfo("Asia/Kolkata")` — fixed +05:30, no DST since 1945.
#[must_use]
pub fn ist() -> FixedOffset {
    baskfy_providers::tokens::ist()
}

/// What the desk reads the time from.
pub trait Clock: Send + Sync {
    /// `time.time()`: seconds since the epoch, as a float.
    fn time(&self) -> f64;

    /// `time.monotonic()`: never goes backwards (cache TTLs, rate limiters).
    fn monotonic(&self) -> f64;

    /// `datetime.now(timezone.utc)`.
    fn now_utc(&self) -> DateTime<Utc> {
        let t = self.time();
        let secs = t.floor();
        let nanos = ((t - secs) * 1e9).round().clamp(0.0, 999_999_999.0);
        // Seconds since 1970 fit an i64 for any clock this desk can read.
        #[allow(clippy::cast_possible_truncation)]
        let (secs, nanos) = (secs as i64, nanos as u32);
        Utc.timestamp_opt(secs, nanos).single().unwrap_or_default()
    }

    /// `datetime.now(IST)` — the desk's wall clock for session windows.
    fn now_ist(&self) -> DateTime<FixedOffset> {
        self.now_utc().with_timezone(&ist())
    }

    /// `date.today()` on a machine whose zone is IST (the box's `TZ=Asia/Kolkata`).
    fn today_ist(&self) -> NaiveDate {
        self.now_ist().date_naive()
    }
}

/// The real clock.
#[derive(Debug, Default, Clone, Copy)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn time(&self) -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
    }

    fn monotonic(&self) -> f64 {
        baskfy_providers::clock::Clock::monotonic(&baskfy_providers::clock::SystemClock)
    }
}

/// A clock that stands still until [`FixedClock::set`] or [`FixedClock::advance`] moves it.
/// `monotonic` moves with it (it starts at 1000 s so a TTL arithmetic never sees zero).
#[derive(Debug)]
pub struct FixedClock {
    state: RwLock<(f64, f64)>,
}

impl FixedClock {
    /// A clock showing `at`.
    #[must_use]
    pub fn new(at: DateTime<FixedOffset>) -> Self {
        Self {
            state: RwLock::new((epoch_seconds(&at), 1000.0)),
        }
    }

    /// Jump to `at` (monotonic advances by the same amount, never backwards).
    pub fn set(&self, at: DateTime<FixedOffset>) {
        if let Ok(mut s) = self.state.write() {
            let t = epoch_seconds(&at);
            let step = (t - s.0).max(0.0);
            *s = (t, s.1 + step);
        }
    }

    /// Move forward `seconds`.
    pub fn advance(&self, seconds: f64) {
        if let Ok(mut s) = self.state.write() {
            let step = seconds.max(0.0);
            *s = (s.0 + step, s.1 + step);
        }
    }
}

impl Clock for FixedClock {
    fn time(&self) -> f64 {
        self.state.read().map_or(0.0, |s| s.0)
    }

    fn monotonic(&self) -> f64 {
        self.state.read().map_or(0.0, |s| s.1)
    }
}

/// Seconds since the epoch, as `datetime.timestamp()` returns them.
#[must_use]
pub fn epoch_seconds<Tz: TimeZone>(at: &DateTime<Tz>) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let secs = at.timestamp() as f64;
    secs + f64::from(at.timestamp_subsec_nanos()) / 1e9
}
