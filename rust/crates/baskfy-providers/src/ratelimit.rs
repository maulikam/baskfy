//! Rate limits held in Redis, shared across processes. Ports `baskfy_providers.ratelimit`.
//!
//! docs/09 §"Kite specifics": "Rate limit ~ 3 req/s -> a token-bucket limiter shared across
//! workers (Redis)." Shared is the whole point: during the strangler a Python worker, the Python
//! desk and a Rust service all wait on the **same Redis keys**, so this module runs Python's
//! own Lua scripts, byte for byte ([`TAKE_SCRIPT`], [`SPACING_SCRIPT`]; a test holds them equal
//! to the Python source), with the same arguments in the same encoding. The rates themselves —
//! Kite's 3 req/s ceiling, the 2 req/s bulk lane, NSE's 1 req/s — are
//! [`ProviderSettings`](crate::settings::ProviderSettings)' defaults and the factory's keys.
//!
//! Redis is reached through [`ScriptBackend`]. [`RedisScriptBackend`] is production;
//! [`InMemoryScriptBackend`] executes the two scripts' semantics exactly (Lua 5.1 arithmetic,
//! `tostring` as `%.14g`, Redis's exact number arguments, `PEXPIRE`/`PX` expiry, `TIME` from an
//! injected clock) so the limiter's tests are deterministic and need no Redis
//! (DECISIONS-RS R4.8). The atomicity a real Redis gives by running a script alone is given here
//! by one lock held for the whole script.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;

use crate::clock::{Clock, Random, Sleeper, SystemClock, ThreadRandom, TokioSleeper};
use crate::errors::ProviderError;
use crate::py::{fixed, lua_number, repr_float, repr_str};
use crate::secret::Secret;

/// One value of a Redis reply, as the scripts can return it.
#[derive(Debug, Clone, PartialEq)]
pub enum ScriptValue {
    /// An integer reply (a Lua number).
    Int(i64),
    /// A bulk string (a Lua string).
    Bytes(Vec<u8>),
    /// A multi-bulk reply (a Lua table).
    Array(Vec<ScriptValue>),
    /// A nil reply.
    Nil,
    /// Anything else, described.
    Other(String),
}

/// The slice of Redis this module uses (`RedisLike`): evaluate a script atomically.
#[async_trait]
pub trait ScriptBackend: Send + Sync {
    /// Runs `script` with `KEYS = keys`, `ARGV = args`, atomically.
    ///
    /// # Errors
    /// Redis's failure as a non-provider error (Python's `redis` exceptions are not
    /// `ProviderError`s: retry does not retry them, the breaker does not count them).
    async fn eval(
        &self,
        script: &'static str,
        keys: &[String],
        args: &[String],
    ) -> Result<ScriptValue, ProviderError>;
}

/// What a provider needs from a limiter: permission to make one request (`RateLimiter`).
#[async_trait]
pub trait RateLimiter: Send + Sync {
    /// Wait until `tokens` are available; returns seconds waited.
    ///
    /// # Errors
    /// `RateLimited` when the wait would exceed the budget; backend failures as they are.
    async fn acquire(&self, tokens: f64) -> Result<f64, ProviderError>;
}

/// A [`RateLimiter`] that can say which clock it is and how fast it runs (`SpacedLimiter`).
pub trait SpacedLimiter: RateLimiter {
    /// The Redis key of its departure clock.
    fn key(&self) -> &str;

    /// Its rate and wait budget.
    fn config(&self) -> CallSpacingConfig;
}

// ---------------------------------------------------------------------------------------------
// Token bucket
// ---------------------------------------------------------------------------------------------

/// Ports `TokenBucketConfig`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TokenBucketConfig {
    /// Sustained rate (docs/09: ~3 req/s for Kite).
    pub rate_per_second: f64,
    /// Burst size; the factory uses one second's worth (no burst beyond the rate).
    pub capacity: f64,
    /// How long `acquire` waits before giving up.
    pub max_wait_seconds: f64,
}

impl TokenBucketConfig {
    /// Validated config with Python's default 30 s budget.
    ///
    /// # Errors
    /// `ValueError` for a non-positive rate or capacity (NaN passes, as in Python).
    pub fn new(rate_per_second: f64, capacity: f64) -> Result<Self, ProviderError> {
        Self::with_max_wait(rate_per_second, capacity, 30.0)
    }

    /// Validated config with an explicit budget.
    ///
    /// # Errors
    /// As [`TokenBucketConfig::new`].
    pub fn with_max_wait(
        rate_per_second: f64,
        capacity: f64,
        max_wait_seconds: f64,
    ) -> Result<Self, ProviderError> {
        if rate_per_second <= 0.0 {
            return Err(ProviderError::value_error(format!(
                "rate_per_second must be positive; got {}",
                repr_float(rate_per_second)
            )));
        }
        if capacity <= 0.0 {
            return Err(ProviderError::value_error(format!(
                "capacity must be positive; got {}",
                repr_float(capacity)
            )));
        }
        Ok(Self {
            rate_per_second,
            capacity,
            max_wait_seconds,
        })
    }
}

/// A continuous-refill token bucket several processes share (`RedisTokenBucket`).
pub struct RedisTokenBucket {
    backend: Arc<dyn ScriptBackend>,
    key: String,
    config: TokenBucketConfig,
    clock: Arc<dyn Clock>,
    sleeper: Arc<dyn Sleeper>,
    random: Arc<dyn Random>,
}

impl RedisTokenBucket {
    /// A bucket on the production clock, sleeper and jitter.
    #[must_use]
    pub fn new(backend: Arc<dyn ScriptBackend>, key: &str, config: TokenBucketConfig) -> Self {
        Self::with_seams(
            backend,
            key,
            config,
            Arc::new(SystemClock),
            Arc::new(TokioSleeper),
            Arc::new(ThreadRandom),
        )
    }

    /// A bucket with injected seams (Python's `clock=` and `sleeper=`, plus the jitter source).
    #[must_use]
    pub fn with_seams(
        backend: Arc<dyn ScriptBackend>,
        key: &str,
        config: TokenBucketConfig,
        clock: Arc<dyn Clock>,
        sleeper: Arc<dyn Sleeper>,
        random: Arc<dyn Random>,
    ) -> Self {
        Self {
            backend,
            key: key.to_owned(),
            config,
            clock,
            sleeper,
            random,
        }
    }

    /// The Redis key.
    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }

    /// The config.
    #[must_use]
    pub const fn config(&self) -> TokenBucketConfig {
        self.config
    }

    /// `try_acquire`: take `tokens` if available — `0.0` when granted, else the seconds until
    /// enough will have accrued. Never sleeps. `now` overrides the clock.
    ///
    /// # Errors
    /// Backend failures; `ValueError` on a malformed reply.
    pub async fn try_acquire(&self, tokens: f64, now: Option<f64>) -> Result<f64, ProviderError> {
        let moment = now.unwrap_or_else(|| self.clock.monotonic());
        let raw = self
            .backend
            .eval(
                TAKE_SCRIPT,
                std::slice::from_ref(&self.key),
                &[
                    repr_float(self.config.capacity),
                    repr_float(self.config.rate_per_second),
                    repr_float(moment),
                    repr_float(tokens),
                ],
            )
            .await?;
        let (granted, wait) = decode(&raw)?;
        Ok(if granted { 0.0 } else { wait })
    }
}

#[async_trait]
impl RateLimiter for RedisTokenBucket {
    /// Python's `acquire`: loop on `try_acquire`, sleeping the reported wait plus a jitter of
    /// up to 50 ms, until granted or the budget would be exceeded.
    async fn acquire(&self, tokens: f64) -> Result<f64, ProviderError> {
        let mut waited = 0.0_f64;
        loop {
            let wait = self.try_acquire(tokens, None).await?;
            if is_zero(wait) {
                return Ok(waited);
            }
            if waited + wait > self.config.max_wait_seconds {
                return Err(ProviderError::rate_limited(format!(
                    "waited {}s for a rate-limit token on {} and the next one is {}s away, \
                     exceeding the {}s budget",
                    fixed(waited, 2),
                    repr_str(&self.key),
                    fixed(wait, 2),
                    fixed(self.config.max_wait_seconds, 0)
                )));
            }
            // `wait or 0.05`: wait is non-zero here, so this is min(0.05, wait).
            let pause = wait + self.random.uniform(0.0, 0.05_f64.min(wait));
            self.sleeper.sleep(pause).await;
            waited += pause;
        }
    }
}

/// Python's `x == 0.0` (true for `-0.0`, false for NaN).
fn is_zero(x: f64) -> bool {
    x.abs() <= 0.0
}

// ---------------------------------------------------------------------------------------------
// Call spacing
// ---------------------------------------------------------------------------------------------

/// Ports `CallSpacingConfig`: no-burst spacing shared by every process on the key.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CallSpacingConfig {
    /// Departures per second.
    pub rate_per_second: f64,
    /// The longest acceptable wait for a slot.
    pub max_wait_seconds: f64,
}

impl CallSpacingConfig {
    /// Validated config with Python's default 30 s budget.
    ///
    /// # Errors
    /// `ValueError` for a non-positive rate.
    pub fn new(rate_per_second: f64) -> Result<Self, ProviderError> {
        Self::with_max_wait(rate_per_second, 30.0)
    }

    /// Validated config with an explicit budget.
    ///
    /// # Errors
    /// `ValueError` for a non-positive rate or a negative budget.
    pub fn with_max_wait(
        rate_per_second: f64,
        max_wait_seconds: f64,
    ) -> Result<Self, ProviderError> {
        if rate_per_second <= 0.0 {
            return Err(ProviderError::value_error(format!(
                "rate_per_second must be positive; got {}",
                repr_float(rate_per_second)
            )));
        }
        if max_wait_seconds < 0.0 {
            return Err(ProviderError::value_error(format!(
                "max_wait_seconds must be non-negative; got {}",
                repr_float(max_wait_seconds)
            )));
        }
        Ok(Self {
            rate_per_second,
            max_wait_seconds,
        })
    }
}

/// Reserves evenly spaced departures in Redis; a cold clock grants no burst (`RedisCallSpacer`).
pub struct RedisCallSpacer {
    backend: Arc<dyn ScriptBackend>,
    key: String,
    config: CallSpacingConfig,
    sleeper: Arc<dyn Sleeper>,
}

impl RedisCallSpacer {
    /// A spacer on the production sleeper.
    #[must_use]
    pub fn new(backend: Arc<dyn ScriptBackend>, key: &str, config: CallSpacingConfig) -> Self {
        Self::with_sleeper(backend, key, config, Arc::new(TokioSleeper))
    }

    /// A spacer with an injected sleeper.
    #[must_use]
    pub fn with_sleeper(
        backend: Arc<dyn ScriptBackend>,
        key: &str,
        config: CallSpacingConfig,
        sleeper: Arc<dyn Sleeper>,
    ) -> Self {
        Self {
            backend,
            key: key.to_owned(),
            config,
            sleeper,
        }
    }
}

#[async_trait]
impl RateLimiter for RedisCallSpacer {
    /// Reserve one departure, sleep until it, return seconds waited.
    async fn acquire(&self, tokens: f64) -> Result<f64, ProviderError> {
        if (tokens - 1.0).abs() > 0.0 || tokens.is_nan() {
            return Err(ProviderError::value_error(
                "RedisCallSpacer grants exactly one HTTP request per acquire",
            ));
        }
        let interval = 1.0 / self.config.rate_per_second;
        let raw = self
            .backend
            .eval(
                SPACING_SCRIPT,
                std::slice::from_ref(&self.key),
                &[
                    repr_float(interval),
                    repr_float(self.config.max_wait_seconds),
                ],
            )
            .await?;
        let (claimed, wait) = decode(&raw)?;
        if !claimed {
            return Err(ProviderError::rate_limited(format!(
                "the next rate-limit slot on {} is {}s away, exceeding the {}s budget",
                repr_str(&self.key),
                fixed(wait, 2),
                fixed(self.config.max_wait_seconds, 0)
            )));
        }
        if wait > 0.0 {
            self.sleeper.sleep(wait).await;
        }
        // Python's max(wait, 0.0): the first argument unless the second is greater.
        Ok(if 0.0 > wait { 0.0 } else { wait })
    }
}

impl SpacedLimiter for RedisCallSpacer {
    fn key(&self) -> &str {
        &self.key
    }

    fn config(&self) -> CallSpacingConfig {
        self.config
    }
}

/// Takes a slot from every clock in order, so one caller is held to two rates at once
/// (`LayeredCallSpacer`, M85). The lane clock comes first and the box-wide ceiling last.
pub struct LayeredCallSpacer {
    spacers: Vec<Arc<dyn SpacedLimiter>>,
}

impl LayeredCallSpacer {
    /// Layers `spacers`, taken in this order.
    ///
    /// # Errors
    /// `ValueError` when there are none.
    pub fn new(spacers: Vec<Arc<dyn SpacedLimiter>>) -> Result<Self, ProviderError> {
        if spacers.is_empty() {
            return Err(ProviderError::value_error(
                "a layered spacer needs at least one clock",
            ));
        }
        Ok(Self { spacers })
    }

    /// The clocks, in order.
    #[must_use]
    pub fn spacers(&self) -> &[Arc<dyn SpacedLimiter>] {
        &self.spacers
    }

    /// The ceiling's key — the last one taken, the one the whole box shares.
    #[must_use]
    pub fn key(&self) -> &str {
        self.spacers.last().map_or("", |s| s.key())
    }
}

#[async_trait]
impl RateLimiter for LayeredCallSpacer {
    async fn acquire(&self, tokens: f64) -> Result<f64, ProviderError> {
        let mut total = 0.0;
        for spacer in &self.spacers {
            total += spacer.acquire(tokens).await?;
        }
        Ok(total)
    }
}

// ---------------------------------------------------------------------------------------------
// Reply decoding
// ---------------------------------------------------------------------------------------------

/// `_decode`: the scripts return exactly `{granted, wait_seconds}`.
fn decode(raw: &ScriptValue) -> Result<(bool, f64), ProviderError> {
    let ScriptValue::Array(items) = raw else {
        return Err(bad_reply(raw));
    };
    let [granted_raw, wait_raw] = items.as_slice() else {
        return Err(bad_reply(raw));
    };
    let granted_text = as_scalar(granted_raw)?;
    let granted: i64 = granted_text.trim().parse().map_err(|_| {
        ProviderError::value_error(format!(
            "invalid literal for int() with base 10: {}",
            repr_str(&granted_text)
        ))
    })?;
    let wait_text = as_scalar(wait_raw)?;
    let wait: f64 = wait_text.trim().parse().map_err(|_| {
        ProviderError::value_error(format!(
            "could not convert string to float: {}",
            repr_str(&wait_text)
        ))
    })?;
    Ok((granted != 0, wait))
}

fn bad_reply(raw: &ScriptValue) -> ProviderError {
    ProviderError::value_error(format!(
        "unexpected reply from the rate-limit script: {raw:?}"
    ))
}

fn as_scalar(value: &ScriptValue) -> Result<String, ProviderError> {
    match value {
        ScriptValue::Int(i) => Ok(i.to_string()),
        ScriptValue::Bytes(b) => String::from_utf8(b.clone()).map_err(|e| {
            ProviderError::foreign(
                "UnicodeDecodeError",
                format!("'utf-8' codec can't decode: {e}"),
            )
        }),
        other => Err(ProviderError::value_error(format!(
            "unexpected scalar in the rate-limit reply: {other:?}"
        ))),
    }
}

// ---------------------------------------------------------------------------------------------
// Backends
// ---------------------------------------------------------------------------------------------

/// The production backend: a real Redis, through a reconnecting connection manager. Scripts go
/// by `EVALSHA` with a `SCRIPT LOAD` fallback, as redis-py's `register_script` does.
#[derive(Clone)]
pub struct RedisScriptBackend {
    connection: redis::aio::ConnectionManager,
}

impl RedisScriptBackend {
    /// Connects to `url` (the network block applies: only loopback while tests run).
    ///
    /// # Errors
    /// `NetworkAccessBlocked`, or Redis's `ConnectionError`. The URL is never in the message.
    pub async fn connect(url: &Secret) -> Result<Self, ProviderError> {
        // A URL that does not parse would be quoted in `guard_url`'s message, and a Redis URL
        // can carry a password, so that one message is replaced.
        crate::net::guard_url("redis", url.expose()).map_err(|e| {
            if e.python_type() == "ValueError" {
                ProviderError::value_error("invalid Redis URL (not shown: it may carry a password)")
            } else {
                e
            }
        })?;
        let client = redis::Client::open(url.expose()).map_err(redis_error)?;
        let connection = redis::aio::ConnectionManager::new(client)
            .await
            .map_err(redis_error)?;
        Ok(Self { connection })
    }
}

impl std::fmt::Debug for RedisScriptBackend {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("RedisScriptBackend")
    }
}

fn redis_error(e: redis::RedisError) -> ProviderError {
    let kind = if e.is_timeout() {
        "TimeoutError"
    } else if e.is_io_error() || e.is_connection_dropped() || e.is_connection_refusal() {
        "ConnectionError"
    } else {
        "ResponseError"
    };
    ProviderError::foreign(kind, e.to_string())
}

fn from_redis(value: redis::Value) -> ScriptValue {
    match value {
        redis::Value::Int(i) => ScriptValue::Int(i),
        redis::Value::BulkString(b) => ScriptValue::Bytes(b),
        redis::Value::SimpleString(s) => ScriptValue::Bytes(s.into_bytes()),
        redis::Value::Okay => ScriptValue::Bytes(b"OK".to_vec()),
        redis::Value::Nil => ScriptValue::Nil,
        redis::Value::Array(items) => {
            ScriptValue::Array(items.into_iter().map(from_redis).collect())
        }
        other => ScriptValue::Other(format!("{other:?}")),
    }
}

#[async_trait]
impl ScriptBackend for RedisScriptBackend {
    async fn eval(
        &self,
        script: &'static str,
        keys: &[String],
        args: &[String],
    ) -> Result<ScriptValue, ProviderError> {
        let script = redis::Script::new(script);
        let mut invocation = script.prepare_invoke();
        for k in keys {
            invocation.key(k);
        }
        for a in args {
            invocation.arg(a);
        }
        let mut connection = self.connection.clone();
        let value: redis::Value = invocation
            .invoke_async(&mut connection)
            .await
            .map_err(redis_error)?;
        Ok(from_redis(value))
    }
}

#[derive(Debug, Clone)]
enum Stored {
    Hash(HashMap<String, String>),
    Text(String),
}

#[derive(Debug, Clone)]
struct Entry {
    value: Stored,
    /// Expiry in clock milliseconds.
    expires_at_ms: Option<f64>,
}

/// A single-process Redis that runs exactly the two rate-limit scripts, for deterministic tests
/// and for a box with no Redis where one process is the only caller.
///
/// Fidelity to Redis 7 + Lua 5.1: arguments arrive as strings and are read with `tonumber`;
/// a Lua number written by `redis.call` is stored exactly (Redis 7 converts with a round-trip
/// `dtoa`); `tostring(wait)` is `%.14g`; `math.min` and `math.ceil` are Lua's; `TIME` is the
/// injected clock's whole seconds and microseconds; a key past its `PEXPIRE`/`PX` is absent.
pub struct InMemoryScriptBackend {
    clock: Arc<dyn Clock>,
    state: Mutex<HashMap<String, Entry>>,
}

impl InMemoryScriptBackend {
    /// An empty server whose `TIME` and expiry read `clock`.
    #[must_use]
    pub fn new(clock: Arc<dyn Clock>) -> Self {
        Self {
            clock,
            state: Mutex::new(HashMap::new()),
        }
    }

    fn now_ms(&self) -> f64 {
        self.clock.monotonic() * 1000.0
    }

    fn run_take(
        &self,
        state: &mut HashMap<String, Entry>,
        key: &str,
        args: &[String],
    ) -> Result<ScriptValue, ProviderError> {
        let capacity = tonumber(args, 0)?;
        let rate = tonumber(args, 1)?;
        let now = tonumber(args, 2)?;
        let requested = tonumber(args, 3)?;
        let now_ms = self.now_ms();
        let live = state
            .get(key)
            .filter(|e| e.expires_at_ms.is_none_or(|t| t > now_ms));
        let (stored_tokens, stored_at) = match live.map(|e| &e.value) {
            Some(Stored::Hash(h)) => (
                h.get("tokens").and_then(|v| v.trim().parse::<f64>().ok()),
                h.get("at").and_then(|v| v.trim().parse::<f64>().ok()),
            ),
            Some(Stored::Text(_)) => {
                return Err(ProviderError::foreign(
                    "ResponseError",
                    "WRONGTYPE Operation against a key holding the wrong kind of value",
                ));
            }
            None => (None, None),
        };
        let (mut tokens, at) = match (stored_tokens, stored_at) {
            (Some(t), Some(a)) => (t, a),
            _ => (capacity, now),
        };
        let mut elapsed = now - at;
        if elapsed < 0.0 {
            elapsed = 0.0;
        }
        tokens = lua_min(capacity, tokens + elapsed * rate);
        let mut granted = 0;
        let mut wait = 0.0;
        if tokens >= requested {
            tokens -= requested;
            granted = 1;
        } else {
            wait = (requested - tokens) / rate;
        }
        let mut hash = HashMap::new();
        hash.insert("tokens".to_owned(), repr_float(tokens));
        hash.insert("at".to_owned(), repr_float(now));
        let ttl_ms = redis_integer(((capacity / rate) + 1.0) * 1000.0)?;
        state.insert(
            key.to_owned(),
            Entry {
                value: Stored::Hash(hash),
                expires_at_ms: Some(now_ms + ttl_ms),
            },
        );
        Ok(ScriptValue::Array(vec![
            ScriptValue::Int(granted),
            ScriptValue::Bytes(lua_number(wait).into_bytes()),
        ]))
    }

    fn run_spacing(
        &self,
        state: &mut HashMap<String, Entry>,
        key: &str,
        args: &[String],
    ) -> Result<ScriptValue, ProviderError> {
        let interval = tonumber(args, 0)?;
        let max_wait = tonumber(args, 1)?;
        // TIME: whole seconds and microseconds.
        let t = self.clock.monotonic();
        let secs = t.floor();
        let micros = ((t - secs) * 1_000_000.0).floor();
        let now = secs + micros / 1_000_000.0;
        let now_ms = self.now_ms();
        let stored = match state
            .get(key)
            .filter(|e| e.expires_at_ms.is_none_or(|x| x > now_ms))
            .map(|e| &e.value)
        {
            Some(Stored::Text(v)) => v.trim().parse::<f64>().ok(),
            Some(Stored::Hash(_)) => {
                return Err(ProviderError::foreign(
                    "ResponseError",
                    "WRONGTYPE Operation against a key holding the wrong kind of value",
                ));
            }
            None => None,
        };
        let next_at = match stored {
            Some(n) if n >= now || n.is_nan() => n,
            _ => now,
        };
        let wait = next_at - now;
        if wait > max_wait {
            return Ok(ScriptValue::Array(vec![
                ScriptValue::Int(0),
                ScriptValue::Bytes(lua_number(wait).into_bytes()),
            ]));
        }
        let px = redis_integer(((wait + interval) * 1000.0).ceil() + 1000.0)?;
        state.insert(
            key.to_owned(),
            Entry {
                value: Stored::Text(repr_float(next_at + interval)),
                expires_at_ms: Some(now_ms + px),
            },
        );
        Ok(ScriptValue::Array(vec![
            ScriptValue::Int(1),
            ScriptValue::Bytes(lua_number(wait).into_bytes()),
        ]))
    }
}

/// Lua 5.1 `math.min(a, b)`: `b` only when `b < a` (so NaN in `b` yields `a`).
fn lua_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// `tonumber(ARGV[i])`; a missing or non-numeric argument fails the script, as Lua arithmetic
/// on `nil` does.
fn tonumber(args: &[String], index: usize) -> Result<f64, ProviderError> {
    args.get(index)
        .and_then(|a| a.trim().parse::<f64>().ok())
        .ok_or_else(|| {
            ProviderError::foreign(
                "ResponseError",
                format!(
                    "ERR user_script: attempt to perform arithmetic on a nil value (ARGV[{}])",
                    index + 1
                ),
            )
        })
}

/// A Lua number handed to `PEXPIRE`/`PX`: `math.ceil`'d, then parsed by Redis as an integer.
#[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
fn redis_integer(x: f64) -> Result<f64, ProviderError> {
    let c = x.ceil();
    if !c.is_finite() || c.abs() >= 9.2e18 {
        return Err(ProviderError::foreign(
            "ResponseError",
            "ERR value is not an integer or out of range",
        ));
    }
    Ok(c as i64 as f64)
}

#[async_trait]
impl ScriptBackend for InMemoryScriptBackend {
    async fn eval(
        &self,
        script: &'static str,
        keys: &[String],
        args: &[String],
    ) -> Result<ScriptValue, ProviderError> {
        let key = keys.first().ok_or_else(|| {
            ProviderError::foreign("ResponseError", "ERR Number of keys can't be negative")
        })?;
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if script == TAKE_SCRIPT {
            self.run_take(&mut state, key, args)
        } else if script == SPACING_SCRIPT {
            self.run_spacing(&mut state, key, args)
        } else {
            Err(ProviderError::foreign(
                "ResponseError",
                "NOSCRIPT the in-memory backend runs only TAKE_SCRIPT and SPACING_SCRIPT",
            ))
        }
    }
}

// ---------------------------------------------------------------------------------------------
// The scripts — byte-identical to Python's (tests/ratelimit.rs holds them equal).
// ---------------------------------------------------------------------------------------------

/// The atomic take (`TAKE_SCRIPT`). KEYS[1] bucket key; ARGV capacity, refill rate, now
/// (seconds), tokens requested; returns `{granted (0|1), wait_seconds}`.
pub const TAKE_SCRIPT: &str = r#"
local key       = KEYS[1]
local capacity  = tonumber(ARGV[1])
local rate      = tonumber(ARGV[2])
local now       = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'at')
local tokens = tonumber(state[1])
local at     = tonumber(state[2])

if tokens == nil or at == nil then
  tokens = capacity
  at = now
end

-- Refill for the time elapsed since the stored reading, capped at capacity.
local elapsed = now - at
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)

local granted = 0
local wait = 0.0
if tokens >= requested then
  tokens = tokens - requested
  granted = 1
else
  wait = (requested - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'at', now)
redis.call('PEXPIRE', key, math.ceil(((capacity / rate) + 1) * 1000))

return {granted, tostring(wait)}
"#;

/// The distributed no-burst departure clock (`SPACING_SCRIPT`). KEYS[1] clock key; ARGV
/// interval, maximum acceptable wait; returns `{claimed (0|1), wait_seconds}`. Uses Redis's
/// `TIME`, so processes cannot disagree about the time.
pub const SPACING_SCRIPT: &str = r#"
local key      = KEYS[1]
local interval = tonumber(ARGV[1])
local max_wait = tonumber(ARGV[2])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local next_at = tonumber(redis.call('GET', key))
if next_at == nil or next_at < now then
  next_at = now
end

local wait = next_at - now
if wait > max_wait then
  return {0, tostring(wait)}
end

redis.call('SET', key, next_at + interval, 'PX', math.ceil((wait + interval) * 1000) + 1000)
return {1, tostring(wait)}
"#;
