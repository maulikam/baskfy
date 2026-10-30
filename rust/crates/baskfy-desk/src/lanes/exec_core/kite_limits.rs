//! Ports `app/core/kite_limits.py` (SW20/SW21/M85): one limit in front of every Kite READ the
//! desk makes, per endpoint family, shared across processes through Redis when it answers.
//!
//! | family | cap |
//! |---|---|
//! | `read` | 3 req/s — Kite's combined ceiling, taken FIRST on every call (M85) |
//! | `quote` | 1 req/s (`quote`, `ltp`, `ohlc`) |
//! | `historical` | 3 req/s |
//! | `general` | 9 req/s (everything else; one in hand) |
//!
//! Orders are NOT here: they keep the gateway's own limiter ([`super::ratelimit`]).
//!
//! Spacing, never a token bucket (a bucket of C grants 2C in its first second). The shared
//! clock is the providers' `SPACING_SCRIPT` at `baskfy:ratelimit:kite:<family>`; Redis is a
//! nicety, never a dependency — no URL, a dead server, a dropped connection or a queue past
//! [`MAX_SHARED_WAIT_SECONDS`] all degrade to this process's own spacers, which are always
//! taken as well.
//!
//! The clock and the sleep are injected ([`LimitClock`]) so a burst test proves the floor
//! without spending the seconds.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_providers::ratelimit::{SPACING_SCRIPT, ScriptBackend, ScriptValue};

/// `QUOTE_PER_SECOND`.
pub const QUOTE_PER_SECOND: f64 = 1.0;
/// `HISTORICAL_PER_SECOND`.
pub const HISTORICAL_PER_SECOND: f64 = 3.0;
/// `GENERAL_PER_SECOND`.
pub const GENERAL_PER_SECOND: f64 = 9.0;
/// `READ_PER_SECOND` — Kite's combined ceiling.
pub const READ_PER_SECOND: f64 = 3.0;
/// `SHARED_KEY_PREFIX`.
pub const SHARED_KEY_PREFIX: &str = "baskfy:ratelimit:kite";
/// `MAX_SHARED_WAIT_SECONDS`.
pub const MAX_SHARED_WAIT_SECONDS: f64 = 10.0;
/// `SHARED_RETRY_AFTER_SECONDS`.
pub const SHARED_RETRY_AFTER_SECONDS: f64 = 30.0;
/// `SHARED_REBUILD_AFTER_SECONDS`.
pub const SHARED_REBUILD_AFTER_SECONDS: f64 = 60.0;
/// `SHARED_SOCKET_TIMEOUT_SECONDS`: connect AND per-call timeout for the limiter's own Redis
/// calls (redis-py's `socket_timeout`/`socket_connect_timeout`). A sick Redis is not waited on.
pub const SHARED_SOCKET_TIMEOUT_SECONDS: f64 = 1.5;
/// `TAKE_SCRIPT` — literally the providers' spacing script (M85: one clock, both trees).
pub const TAKE_SCRIPT: &str = SPACING_SCRIPT;

/// `RATE_FOR_FAMILY`, in Python's order.
pub const RATE_FOR_FAMILY: [(&str, f64); 4] = [
    ("read", READ_PER_SECOND),
    ("quote", QUOTE_PER_SECOND),
    ("historical", HISTORICAL_PER_SECOND),
    ("general", GENERAL_PER_SECOND),
];

/// `FAMILY_FOR_CALL`: which family each Kite method spends.
pub const FAMILY_FOR_CALL: [(&str, &str); 13] = [
    ("ltp", "quote"),
    ("quote", "quote"),
    ("ohlc", "quote"),
    ("historical_data", "historical"),
    ("holdings", "general"),
    ("positions", "general"),
    ("orders", "general"),
    ("order_history", "general"),
    ("trades", "general"),
    ("margins", "general"),
    ("instruments", "general"),
    ("profile", "general"),
    ("get_gtts", "general"),
];

/// `FAMILY_FOR_CALL[call]`.
#[must_use]
pub fn family_for_call(call: &str) -> Option<&'static str> {
    FAMILY_FOR_CALL
        .iter()
        .find(|(c, _)| *c == call)
        .map(|(_, f)| *f)
}

/// A limiter error (`ValueError`/`KeyError` in Python).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{python_type}: {message}")]
pub struct LimitError {
    /// The Python class.
    pub python_type: &'static str,
    /// The message.
    pub message: String,
}

/// The clock and sleep a limiter uses: `time.monotonic` / `time.sleep` in production.
#[async_trait]
pub trait LimitClock: Send + Sync {
    /// Seconds, monotonic.
    fn now(&self) -> f64;
    /// Sleep that long.
    async fn sleep(&self, seconds: f64);
}

/// The real clock: `tokio::time`.
#[derive(Debug, Clone, Copy)]
pub struct TokioClock {
    origin: tokio::time::Instant,
}

impl Default for TokioClock {
    fn default() -> Self {
        Self {
            origin: tokio::time::Instant::now(),
        }
    }
}

#[async_trait]
impl LimitClock for TokioClock {
    fn now(&self) -> f64 {
        // Offset so the first departure (next_at = 0) is always in the past.
        1_000.0 + self.origin.elapsed().as_secs_f64()
    }
    async fn sleep(&self, seconds: f64) {
        // `try_`: an infinite wait (a script reply of "inf") must not panic the read path.
        let d = std::time::Duration::try_from_secs_f64(seconds.max(0.0))
            .unwrap_or(std::time::Duration::MAX);
        tokio::time::sleep(d).await;
    }
}

/// `Spacer`: at most one call every `1/rate` seconds in this process. The slot is claimed
/// under the lock; the wait happens outside it.
pub struct Spacer {
    interval: f64,
    clock: Arc<dyn LimitClock>,
    next_at: Mutex<f64>,
}

impl std::fmt::Debug for Spacer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Spacer")
            .field("interval", &self.interval)
            .finish_non_exhaustive()
    }
}

impl Spacer {
    /// `Spacer(rate, clock=, sleep=)`.
    ///
    /// # Errors
    /// `ValueError: rate must be positive`.
    pub fn new(rate: f64, clock: Arc<dyn LimitClock>) -> Result<Self, LimitError> {
        if rate.is_nan() || rate <= 0.0 {
            return Err(LimitError {
                python_type: "ValueError",
                message: format!(
                    "rate must be positive, got {}",
                    baskfy_core::py::float_repr(rate)
                ),
            });
        }
        Ok(Self {
            interval: 1.0 / rate,
            clock,
            next_at: Mutex::new(0.0),
        })
    }

    /// `interval`.
    #[must_use]
    pub const fn interval(&self) -> f64 {
        self.interval
    }

    /// `take()`: claim the next slot; returns how long the caller was made to wait.
    pub async fn take(&self) -> f64 {
        let departure = {
            // A poisoned lock still holds a valid departure: take it rather than skip the
            // spacing (a limiter that fails open is the 18 Aug burst again).
            let mut next = self
                .next_at
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            let now = self.clock.now();
            let departure = now.max(*next);
            *next = departure + self.interval;
            departure
        };
        let wait = departure - self.clock.now();
        if wait > 0.0 {
            self.clock.sleep(wait).await;
        }
        wait.max(0.0)
    }
}

/// `SharedSpacer`: the departure clock in Redis. `None` from [`SharedSpacer::take`] means
/// "space this call locally", never "go ahead unthrottled".
pub struct SharedSpacer {
    /// The family's key.
    pub key: String,
    interval: f64,
    script: Arc<dyn ScriptBackend>,
    max_wait: f64,
    retry_after: f64,
    clock: Arc<dyn LimitClock>,
    down_until: Mutex<f64>,
}

impl std::fmt::Debug for SharedSpacer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SharedSpacer")
            .field("key", &self.key)
            .field("interval", &self.interval)
            .finish_non_exhaustive()
    }
}

/// `_claim(raw)`: the Lua reply, a two-element table (flag, wait as a string).
///
/// # Errors
/// Any other shape (a wrong shape is a bug in the script, never permission).
pub fn claim(raw: &ScriptValue) -> Result<(bool, f64), LimitError> {
    let bad = || LimitError {
        python_type: "ValueError",
        message: format!("unexpected reply from the Kite limiter script: {raw:?}"),
    };
    let ScriptValue::Array(items) = raw else {
        return Err(bad());
    };
    let [flag, wait] = items.as_slice() else {
        return Err(bad());
    };
    let scalar = |v: &ScriptValue| -> Result<String, LimitError> {
        match v {
            ScriptValue::Int(i) => Ok(i.to_string()),
            ScriptValue::Bytes(b) => Ok(String::from_utf8_lossy(b).into_owned()),
            other => Err(LimitError {
                python_type: "ValueError",
                message: format!("unexpected scalar in the Kite limiter reply: {other:?}"),
            }),
        }
    };
    let flag: i64 = scalar(flag)?.trim().parse().map_err(|_| bad())?;
    let wait: f64 = scalar(wait)?.trim().parse().map_err(|_| bad())?;
    Ok((flag != 0, wait))
}

impl SharedSpacer {
    /// `SharedSpacer(script, key, rate, max_wait=, retry_after=, clock=, sleep=)`.
    ///
    /// # Errors
    /// `ValueError: rate must be positive`.
    pub fn new(
        script: Arc<dyn ScriptBackend>,
        key: &str,
        rate: f64,
        max_wait: f64,
        retry_after: f64,
        clock: Arc<dyn LimitClock>,
    ) -> Result<Self, LimitError> {
        if rate.is_nan() || rate <= 0.0 {
            return Err(LimitError {
                python_type: "ValueError",
                message: format!(
                    "rate must be positive, got {}",
                    baskfy_core::py::float_repr(rate)
                ),
            });
        }
        Ok(Self {
            key: key.to_owned(),
            interval: 1.0 / rate,
            script,
            max_wait,
            retry_after,
            clock,
            down_until: Mutex::new(0.0),
        })
    }

    /// `available`: false while the last failure's back-off runs.
    #[must_use]
    pub fn available(&self) -> bool {
        let until = self.down_until.lock().map_or(0.0, |d| *d);
        self.clock.now() >= until
    }

    /// `take()`: claim the box's next slot, or `None` if the clock could not say.
    pub async fn take(&self) -> Option<f64> {
        if !self.available() {
            return None;
        }
        let args = [
            baskfy_providers::py::repr_float(self.interval),
            baskfy_providers::py::repr_float(self.max_wait),
        ];
        // redis-py's `socket_timeout=1.5`: a call that hangs is a failure like any other, so
        // the read degrades to the local spacer instead of hanging behind a sick Redis.
        let call = self
            .script
            .eval(TAKE_SCRIPT, std::slice::from_ref(&self.key), &args);
        let timeout = std::time::Duration::from_millis(1_500);
        let raw = match tokio::time::timeout(timeout, call).await {
            Ok(Ok(raw)) => raw,
            Ok(Err(_)) | Err(_) => {
                if let Ok(mut d) = self.down_until.lock() {
                    *d = self.clock.now() + self.retry_after;
                }
                return None;
            }
        };
        let Ok((claimed, wait)) = claim(&raw) else {
            if let Ok(mut d) = self.down_until.lock() {
                *d = self.clock.now() + self.retry_after;
            }
            return None;
        };
        if !claimed {
            return None;
        }
        if wait > 0.0 {
            self.clock.sleep(wait).await;
        }
        Some(wait.max(0.0))
    }
}

/// `DeskLimits`: one spacer per family, and the box's beside it.
pub struct DeskLimits {
    spacers: HashMap<&'static str, Spacer>,
    shared: HashMap<String, Arc<SharedSpacer>>,
    waits: Mutex<HashMap<String, f64>>,
    local_only: Mutex<HashMap<String, u64>>,
}

impl std::fmt::Debug for DeskLimits {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DeskLimits")
            .field("shared", &self.shared.keys().collect::<Vec<_>>())
            .finish_non_exhaustive()
    }
}

impl DeskLimits {
    /// `DeskLimits(clock=, sleep=, shared=)`. `shared` empty is a limiter deliberately alone.
    #[must_use]
    pub fn new(clock: Arc<dyn LimitClock>, shared: HashMap<String, Arc<SharedSpacer>>) -> Self {
        let mut spacers = HashMap::new();
        for (family, rate) in RATE_FOR_FAMILY {
            if let Ok(s) = Spacer::new(rate, clock.clone()) {
                spacers.insert(family, s);
            }
        }
        let zero = |v: f64| {
            RATE_FOR_FAMILY
                .iter()
                .map(|(f, _)| ((*f).to_owned(), v))
                .collect::<HashMap<_, _>>()
        };
        Self {
            spacers,
            shared,
            waits: Mutex::new(zero(0.0)),
            local_only: Mutex::new(
                RATE_FOR_FAMILY
                    .iter()
                    .map(|(f, _)| ((*f).to_owned(), 0))
                    .collect(),
            ),
        }
    }

    /// `is_shared`.
    #[must_use]
    pub fn is_shared(&self) -> bool {
        !self.shared.is_empty()
    }

    /// `spacer(family)`.
    #[must_use]
    pub fn spacer(&self, family: &str) -> Option<&Spacer> {
        self.spacers.get(family)
    }

    /// `waits[family]`: total seconds waited.
    #[must_use]
    pub fn waits(&self, family: &str) -> f64 {
        self.waits
            .lock()
            .map_or(0.0, |w| w.get(family).copied().unwrap_or(0.0))
    }

    /// `local_only[family]`: how often the shared clock could not answer.
    #[must_use]
    pub fn local_only(&self, family: &str) -> u64 {
        self.local_only
            .lock()
            .map_or(0, |w| w.get(family).copied().unwrap_or(0))
    }

    async fn take_one(&self, family: &str) -> f64 {
        let mut waited = 0.0;
        if let Some(shared) = self.shared.get(family) {
            match shared.take().await {
                None => {
                    if let Ok(mut l) = self.local_only.lock() {
                        *l.entry(family.to_owned()).or_insert(0) += 1;
                    }
                }
                Some(w) => waited += w,
            }
        }
        if let Some(spacer) = self.spacers.get(family) {
            waited += spacer.take().await;
        }
        if let Ok(mut w) = self.waits.lock() {
            *w.entry(family.to_owned()).or_insert(0.0) += waited;
        }
        waited
    }

    /// `slot(family)`: the combined ceiling first, then the family — shared then local, both
    /// always.
    ///
    /// # Errors
    /// `KeyError: no such Kite family`.
    pub async fn slot(&self, family: &str) -> Result<f64, LimitError> {
        if !FAMILY_FOR_CALL.iter().any(|(_, f)| *f == family) {
            return Err(LimitError {
                python_type: "KeyError",
                message: format!("no such Kite family: {family}"),
            });
        }
        Ok(self.take_one("read").await + self.take_one(family).await)
    }

    /// `slot_for_call(call)`: an unknown method lands in `general`, never outside every pool.
    pub async fn slot_for_call(&self, call: &str) -> f64 {
        self.slot(family_for_call(call).unwrap_or("general"))
            .await
            .unwrap_or(0.0)
    }
}

/// The built spacers (or none yet) and when to try building them again.
type SharedState = (Option<HashMap<String, Arc<SharedSpacer>>>, Option<f64>);

/// The box's shared spacers, built lazily, rebuilt a minute after a failed connection
/// (`shared_spacers()` / `reset_shared_spacers()` / `shared_mode()`).
pub struct SharedSpacers {
    state: tokio::sync::Mutex<SharedState>,
    enabled: bool,
    url: String,
    clock: Arc<dyn LimitClock>,
}

impl std::fmt::Debug for SharedSpacers {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SharedSpacers")
            .field("enabled", &self.enabled)
            .finish_non_exhaustive()
    }
}

impl SharedSpacers {
    /// From `DESK_SHARED_READ_LIMITS` and `BASKFY_REDIS_URL`.
    #[must_use]
    pub fn new(enabled: bool, url: &str, clock: Arc<dyn LimitClock>) -> Self {
        Self {
            state: tokio::sync::Mutex::new((None, None)),
            enabled,
            url: url.to_owned(),
            clock,
        }
    }

    async fn build(&self) -> HashMap<String, Arc<SharedSpacer>> {
        if !self.enabled || self.url.is_empty() {
            return HashMap::new();
        }
        let secret = baskfy_providers::secret::Secret::new(self.url.clone());
        let connect = baskfy_providers::ratelimit::RedisScriptBackend::connect(&secret);
        let Ok(Ok(backend)) =
            tokio::time::timeout(std::time::Duration::from_millis(1_500), connect).await
        else {
            return HashMap::new();
        };
        let backend: Arc<dyn ScriptBackend> = Arc::new(backend);
        RATE_FOR_FAMILY
            .iter()
            .filter_map(|(family, rate)| {
                SharedSpacer::new(
                    backend.clone(),
                    &format!("{SHARED_KEY_PREFIX}:{family}"),
                    *rate,
                    MAX_SHARED_WAIT_SECONDS,
                    SHARED_RETRY_AFTER_SECONDS,
                    self.clock.clone(),
                )
                .ok()
                .map(|s| ((*family).to_owned(), Arc::new(s)))
            })
            .collect()
    }

    /// `shared_spacers()`.
    pub async fn get(&self) -> HashMap<String, Arc<SharedSpacer>> {
        let mut state = self.state.lock().await;
        let due = state.1.is_some_and(|at| self.clock.now() >= at);
        if state.0.is_none() || due {
            let built = self.build().await;
            state.1 = (built.is_empty() && self.enabled && !self.url.is_empty())
                .then(|| self.clock.now() + SHARED_REBUILD_AFTER_SECONDS);
            state.0 = Some(built);
        }
        state.0.clone().unwrap_or_default()
    }

    /// `reset_shared_spacers()`.
    pub async fn reset(&self) {
        *self.state.lock().await = (None, None);
    }

    /// `shared_mode()`: `"shared"` or `"per-process"`.
    pub async fn mode(&self) -> &'static str {
        if self.get().await.is_empty() {
            "per-process"
        } else {
            "shared"
        }
    }
}
