//! Ports `app/kite_client.py`'s `Kite`: the desk's broker wrapper — reads (each taking its
//! family's slot, [`super::kite_limits`]), the LTP cache, holdings that count T1, collateral
//! and today's CNC buys (non-negotiables 2 and 3), the token at rest, and the **documented GTT
//! exception** of non-negotiable 6 (`place_gtt_stop` / `delete_gtt` with their own guard, used
//! by the weekly `/stops/arm`).
//!
//! **`Kite()` and `app.main.kite()`** ([`DeskKite::ready`]). The gateway builds this handle at
//! boot; Python builds its `Kite` on the first `kite()` call, and that constructor is where
//! `KITE_API_KEY missing` raises and where the stored token is loaded (which, with no
//! `KITE_TOKEN_ENCRYPTION_KEY`, writes the fallback key beside it). So the handle runs `Kite()`'s
//! load at boot only when it is configured (timing only: a token written later is picked up by
//! `refresh_token_if_changed`), and every method first runs [`DeskKite::ready`]: unconfigured,
//! each one raises Python's `RuntimeError` (a GTT write before its guard — Python's `kite()`
//! raises before `place_gtt_stop` is reached), nothing having been loaded or written; a store
//! that could not be built raises its `OSError` on every call until it can. `FORCE_IPV4` pins in
//! the constructor, before anything else — slightly earlier than Python's (which pins only after
//! the key check), in the safe direction: an order is never sent from an unpinned family.
//!
//! **Malformed broker replies raise what Python raises** (wave D2): `TypeError`, `KeyError`,
//! `AttributeError`, `IndexError`, `ValueError` with Python's own messages, pinned by
//! `fixtures/exec_kite/goldens.json` (written by `tools/parity/exec_kite_goldens.py` from the
//! live `Kite`). `list(x or [])` reads a dict as its keys and a string as its characters, and
//! `dict(...)` takes pairs, exactly as Python does.
//!
//! **Not ported, deliberately:** `place_cnc_order`. It places an order around the gateway (no
//! risk, no rate limit, no journal), and nothing in the desk calls it; the stricter boundary
//! wins (law 2; DECISIONS-RS R8.9). Every order goes through [`super::order_gateway`].

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use baskfy_providers::kite::repr_bytes;
use baskfy_providers::py::repr_str;
use baskfy_providers::secret::Secret;
use tokio::sync::OnceCell;

use super::Broker;
use super::gtt::to_tick;
use super::guards::{assert_not_overnight_option, assert_tradeable};
use super::kite_limits::{DeskLimits, LimitClock, SharedSpacers, TokioClock};
use crate::clock::Clock;
use crate::gateway::DEFAULT_TICK;
use crate::kite::KiteError;
use crate::settings::{Settings, SharedSettings};
use crate::wire::{PyDict, PyValue, dict};

/// `LTP_CACHE_SECONDS`.
pub const LTP_CACHE_SECONDS: f64 = 5.0;
/// `QUOTE_BATCH_SIZE`.
pub const QUOTE_BATCH_SIZE: usize = 500;
/// `LTP_BATCH_SIZE`.
pub const LTP_BATCH_SIZE: usize = 1000;

/// `RuntimeError("KITE_API_KEY missing — …")`, `Kite()`'s refusal.
pub const NO_API_KEY: &str = "KITE_API_KEY missing — copy .env.example to .env and fill it.";

/// What a wrapper call raised (`type(exc).__name__`, `str(exc)`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{python_type}: {message}")]
pub struct ClientError {
    /// The class.
    pub python_type: String,
    /// The message (`str(exc)`).
    pub message: String,
}

impl From<KiteError> for ClientError {
    fn from(e: KiteError) -> Self {
        Self {
            python_type: e.python_type().to_owned(),
            message: e.to_string(),
        }
    }
}

impl From<super::guards::GuardError> for ClientError {
    fn from(e: super::guards::GuardError) -> Self {
        Self {
            python_type: e.kind.python_type().to_owned(),
            message: e.message,
        }
    }
}

type R<T> = Result<T, ClientError>;

fn raise(python_type: &str, message: impl Into<String>) -> ClientError {
    ClientError {
        python_type: python_type.to_owned(),
        message: message.into(),
    }
}

fn runtime(message: impl Into<String>) -> ClientError {
    raise("RuntimeError", message)
}

fn s(v: &str) -> PyValue {
    PyValue::Str(v.to_owned())
}

/// `logging.getLogger("kite")`, on stderr (no secret ever reaches it: messages name files and
/// exceptions, never a token).
#[allow(clippy::print_stderr)]
fn log(level: &str, message: &str) {
    eprintln!("{level}:kite:{message}");
}

// ---------------------------------------------------------------------------------------------
// Python's object model, as far as a broker reply reaches it
// ---------------------------------------------------------------------------------------------

static NONE: PyValue = PyValue::None;
static ZERO: PyValue = PyValue::Int(0);

/// `v[key]` with a str key.
fn subscript<'a>(v: &'a PyValue, key: &str) -> R<&'a PyValue> {
    match v {
        PyValue::Map(m) => m.get(key).ok_or_else(|| raise("KeyError", repr_str(key))),
        PyValue::Str(_) => Err(raise(
            "TypeError",
            "string indices must be integers, not 'str'",
        )),
        PyValue::List(_) => Err(raise(
            "TypeError",
            "list indices must be integers or slices, not str",
        )),
        PyValue::Bytes(_) => Err(raise(
            "TypeError",
            "byte indices must be integers or slices, not str",
        )),
        other => Err(raise(
            "TypeError",
            format!("'{}' object is not subscriptable", other.type_name()),
        )),
    }
}

fn no_attribute(v: &PyValue, attribute: &str) -> ClientError {
    raise(
        "AttributeError",
        format!("'{}' object has no attribute '{attribute}'", v.type_name()),
    )
}

/// `v.get(key)` (`None` when absent): `AttributeError` on a value that is not a dict.
fn attr_get<'a>(v: &'a PyValue, key: &str) -> R<&'a PyValue> {
    match v {
        PyValue::Map(m) => Ok(m.get(key).unwrap_or(&NONE)),
        other => Err(no_attribute(other, "get")),
    }
}

/// `v.items()`.
fn items(v: &PyValue) -> R<&PyDict> {
    match v {
        PyValue::Map(m) => Ok(m),
        other => Err(no_attribute(other, "items")),
    }
}

/// `for item in v`.
fn py_iter(v: &PyValue) -> R<Vec<PyValue>> {
    v.py_iter()
        .map_err(|e| raise(e.python_type(), e.message().to_owned()))
}

/// `list(v or [])`.
fn list_or_empty(v: &PyValue) -> R<Vec<PyValue>> {
    if v.truthy() {
        py_iter(v)
    } else {
        Ok(Vec::new())
    }
}

/// `float(v)`.
fn to_float(v: &PyValue) -> R<f64> {
    #[allow(clippy::cast_precision_loss)]
    match v {
        PyValue::Float(f) => Ok(*f),
        PyValue::Int(i) => Ok(*i as f64),
        PyValue::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        PyValue::Str(text) => crate::settings::py_float(text).ok_or_else(|| {
            raise(
                "ValueError",
                format!("could not convert string to float: {}", repr_str(text)),
            )
        }),
        PyValue::Bytes(b) => std::str::from_utf8(b)
            .ok()
            .and_then(crate::settings::py_float)
            .ok_or_else(|| {
                raise(
                    "ValueError",
                    format!("could not convert string to float: {}", repr_bytes(b)),
                )
            }),
        other => Err(raise(
            "TypeError",
            format!(
                "float() argument must be a string or a real number, not '{}'",
                other.type_name()
            ),
        )),
    }
}

/// `int(v)`. An integer past `i64` (Python's are unbounded) is refused, never wrapped.
fn to_int(v: &PyValue) -> R<i64> {
    match v {
        PyValue::Int(i) => Ok(*i),
        PyValue::Bool(b) => Ok(i64::from(*b)),
        PyValue::Float(f) => {
            if f.is_nan() {
                return Err(raise("ValueError", "cannot convert float NaN to integer"));
            }
            if f.is_infinite() {
                return Err(raise(
                    "OverflowError",
                    "cannot convert float infinity to integer",
                ));
            }
            let t = f.trunc();
            #[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
            if t >= i64::MIN as f64 && t < i64::MAX as f64 {
                Ok(t as i64)
            } else {
                Err(raise("OverflowError", "int too large to convert to i64"))
            }
        }
        PyValue::Str(text) => crate::settings::py_int(text).ok_or_else(|| {
            raise(
                "ValueError",
                format!("invalid literal for int() with base 10: {}", repr_str(text)),
            )
        }),
        other => Err(raise(
            "TypeError",
            format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                other.type_name()
            ),
        )),
    }
}

/// `a + b`.
fn add(a: &PyValue, b: &PyValue) -> R<PyValue> {
    let int_of = |v: &PyValue| match v {
        PyValue::Int(i) => Some(*i),
        PyValue::Bool(x) => Some(i64::from(*x)),
        _ => None,
    };
    #[allow(clippy::cast_precision_loss)]
    let float_of = |v: &PyValue| match v {
        PyValue::Float(f) => Some(*f),
        PyValue::Int(i) => Some(*i as f64),
        PyValue::Bool(x) => Some(f64::from(u8::from(*x))),
        _ => None,
    };
    if let (Some(x), Some(y)) = (int_of(a), int_of(b)) {
        return x
            .checked_add(y)
            .map(PyValue::Int)
            .ok_or_else(|| raise("OverflowError", "int too large to convert to i64"));
    }
    if let (Some(x), Some(y)) = (float_of(a), float_of(b)) {
        return Ok(PyValue::Float(x + y));
    }
    match (a, b) {
        (PyValue::Str(x), PyValue::Str(y)) => Ok(PyValue::Str(format!("{x}{y}"))),
        (PyValue::List(x), PyValue::List(y)) => {
            let mut out = x.clone();
            out.extend(y.iter().cloned());
            Ok(PyValue::List(out))
        }
        (PyValue::Str(_), other) => Err(raise(
            "TypeError",
            format!(
                "can only concatenate str (not \"{}\") to str",
                other.type_name()
            ),
        )),
        (PyValue::List(_), other) => Err(raise(
            "TypeError",
            format!(
                "can only concatenate list (not \"{}\") to list",
                other.type_name()
            ),
        )),
        (x, y) => Err(raise(
            "TypeError",
            format!(
                "unsupported operand type(s) for +: '{}' and '{}'",
                x.type_name(),
                y.type_name()
            ),
        )),
    }
}

/// `v <= 0`.
fn le_zero(v: &PyValue) -> R<bool> {
    match v {
        PyValue::Int(i) => Ok(*i <= 0),
        PyValue::Float(f) => Ok(*f <= 0.0),
        PyValue::Bool(b) => Ok(!*b),
        other => Err(raise(
            "TypeError",
            format!(
                "'<=' not supported between instances of '{}' and 'int'",
                other.type_name()
            ),
        )),
    }
}

/// `hash(v)`: a list or a dict cannot be a dict key.
fn hashable(v: &PyValue) -> R<()> {
    match v {
        PyValue::List(_) | PyValue::Map(_) => Err(raise(
            "TypeError",
            format!("unhashable type: '{}'", v.type_name()),
        )),
        _ => Ok(()),
    }
}

/// Dict-key equality: `1 == 1.0 == True`.
fn key_eq(a: &PyValue, b: &PyValue) -> bool {
    #[allow(clippy::cast_precision_loss)]
    let num = |v: &PyValue| match v {
        PyValue::Int(i) => Some(*i as f64),
        PyValue::Float(f) => Some(*f),
        PyValue::Bool(x) => Some(f64::from(u8::from(*x))),
        _ => None,
    };
    match (num(a), num(b)) {
        (Some(x), Some(y)) => x == y,
        _ => a == b,
    }
}

/// `key.split(":", 1)[1]`.
fn after_colon(key: &str) -> R<&str> {
    key.split_once(':')
        .map(|(_, rest)| rest)
        .ok_or_else(|| raise("IndexError", "list index out of range"))
}

/// `dict(v)`: a dict as is, else a sequence of pairs.
fn py_dict(v: &PyValue) -> R<PyDict> {
    match v {
        PyValue::Map(m) => Ok(m.clone()),
        PyValue::Str(_) | PyValue::List(_) | PyValue::Bytes(_) => {
            let mut out = PyDict::new();
            for (i, element) in py_iter(v)?.iter().enumerate() {
                let parts = match element {
                    PyValue::Str(_) | PyValue::List(_) | PyValue::Map(_) | PyValue::Bytes(_) => {
                        py_iter(element)?
                    }
                    _ => {
                        return Err(raise(
                            "TypeError",
                            format!(
                                "cannot convert dictionary update sequence element #{i} to a sequence"
                            ),
                        ));
                    }
                };
                let [key, value] = parts.as_slice() else {
                    return Err(raise(
                        "ValueError",
                        format!(
                            "dictionary update sequence element #{i} has length {}; 2 is required",
                            parts.len()
                        ),
                    ));
                };
                hashable(key)?;
                let PyValue::Str(key) = key else {
                    // A non-str key is legal Python; the desk's dict holds str keys only, and no
                    // Kite payload has one. Refused, never silently re-keyed.
                    return Err(raise(
                        "TypeError",
                        format!("a {} dict key is not representable here", key.type_name()),
                    ));
                };
                out.insert(key.clone(), value.clone());
            }
            Ok(out)
        }
        other => Err(raise(
            "TypeError",
            format!("'{}' object is not iterable", other.type_name()),
        )),
    }
}

fn snapshot(settings: &SharedSettings) -> Settings {
    match settings.read() {
        Ok(s) => s.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    }
}

/// `app.kite_client.Kite`.
pub struct DeskKite {
    kc: Arc<Broker>,
    settings: SharedSettings,
    limit_clock: Arc<dyn LimitClock>,
    cache_clock: Arc<dyn Clock>,
    limits: OnceCell<Arc<DeskLimits>>,
    shared: Arc<SharedSpacers>,
    ltp_cache: Mutex<HashMap<String, (f64, f64)>>,
    /// `_ticks`: exchange → (str symbol → tick). A non-str symbol is a legal key in Python's
    /// dict, but no `&str` lookup can ever match it, so it is validated and not kept.
    ticks: Mutex<HashMap<String, HashMap<String, f64>>>,
    token_mtime: Mutex<Option<std::time::SystemTime>>,
    slots_taken: Mutex<Vec<&'static str>>,
    /// `app.main._kite is not None`: `Kite()` has run to completion.
    constructed: Mutex<bool>,
}

impl std::fmt::Debug for DeskKite {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DeskKite").finish_non_exhaustive()
    }
}

impl DeskKite {
    /// The handle the gateway holds, built at boot: `FORCE_IPV4` pins first (before any session
    /// exists), then — configured — `Kite()`'s token load. Unconfigured, nothing is loaded or
    /// written and every method raises `KITE_API_KEY missing` ([`DeskKite::ready`]). The LTP
    /// cache ages on `clock.monotonic()` (`app.kite_client`'s `time.monotonic`); the limiter
    /// keeps its own real clock.
    #[must_use]
    pub fn new(kc: Arc<Broker>, settings: SharedSettings, clock: Arc<dyn Clock>) -> Self {
        Self::with_limit_clock(kc, settings, clock, Arc::new(TokioClock::default()))
    }

    /// As [`DeskKite::new`] with the limiter's clock injected (tests).
    #[must_use]
    pub fn with_limit_clock(
        kc: Arc<Broker>,
        settings: SharedSettings,
        clock: Arc<dyn Clock>,
        limit_clock: Arc<dyn LimitClock>,
    ) -> Self {
        let s = snapshot(&settings);
        if s.force_ipv4 {
            super::net::force_ipv4();
        }
        let shared = Arc::new(SharedSpacers::new(
            s.shared_read_limits,
            &s.shared_read_limits_redis_url,
            limit_clock.clone(),
        ));
        let me = Self {
            kc,
            settings,
            limit_clock,
            cache_clock: clock,
            limits: OnceCell::new(),
            shared,
            ltp_cache: Mutex::new(HashMap::new()),
            ticks: Mutex::new(HashMap::new()),
            token_mtime: Mutex::new(None),
            slots_taken: Mutex::new(Vec::new()),
            constructed: Mutex::new(false),
        };
        // `Kite()` runs here when it can: configured, and the token store builds. Otherwise
        // nothing is loaded and nothing is written, and [`DeskKite::ready`] raises Python's error
        // on the first call (and builds it once the key arrives).
        if !s.kite_api_key.is_empty()
            && me.load_token().is_ok()
            && let Ok(mut constructed) = me.constructed.lock()
        {
            *constructed = true;
        }
        me
    }

    /// `KITE_API_KEY missing` is Python's constructor error.
    ///
    /// # Errors
    /// `RuntimeError` when `KITE_API_KEY` is empty.
    pub fn ensure_configured(&self) -> Result<(), ClientError> {
        if snapshot(&self.settings).kite_api_key.is_empty() {
            return Err(runtime(NO_API_KEY));
        }
        Ok(())
    }

    /// `app.main.kite()`: run `Kite()` once — refuse without `KITE_API_KEY`, pin IPv4 when
    /// `FORCE_IPV4` (before the session exists), load the stored token. A constructor that
    /// raised leaves nothing built, so the next call tries again (`_kite` stays `None`).
    ///
    /// # Errors
    /// `RuntimeError` without `KITE_API_KEY`; the token store's `OSError` (its fallback key
    /// cannot be read or written).
    pub fn ready(&self) -> Result<(), ClientError> {
        let mut constructed = self
            .constructed
            .lock()
            .map_err(|_| runtime("the Kite wrapper's state is poisoned"))?;
        if *constructed {
            return Ok(());
        }
        self.ensure_configured()?;
        if snapshot(&self.settings).force_ipv4 {
            super::net::force_ipv4();
        }
        self.load_token()?;
        *constructed = true;
        Ok(())
    }

    /// The read limiter, built on first use with the box's shared spacers.
    pub async fn limits(&self) -> Arc<DeskLimits> {
        self.limits
            .get_or_init(|| async {
                Arc::new(DeskLimits::new(
                    self.limit_clock.clone(),
                    self.shared.get().await,
                ))
            })
            .await
            .clone()
    }

    /// Use a given limiter (tests; `DeskLimits(shared={})`).
    ///
    /// # Errors
    /// A limiter is already in place.
    pub fn set_limits(&self, limits: Arc<DeskLimits>) -> Result<(), ClientError> {
        self.limits
            .set(limits)
            .map_err(|_| runtime("the read limiter is already built"))
    }

    /// `shared_mode()` for `/status`.
    pub async fn shared_mode(&self) -> &'static str {
        self.shared.mode().await
    }

    async fn slot(&self, family: &'static str) {
        if let Ok(mut log) = self.slots_taken.lock() {
            log.push(family);
        }
        let _ = self.limits().await.slot(family).await;
    }

    /// Take one `family` slot from this client's limiter (the lanes' raw reads share it).
    pub async fn take_read_slot(&self, family: &'static str) {
        self.slot(family).await;
    }

    /// Every read-limiter family this client has taken a slot from, in order (SW20's "no read
    /// leaves without a slot", observable in tests).
    #[must_use]
    pub fn slots_taken(&self) -> Vec<&'static str> {
        self.slots_taken
            .lock()
            .map(|l| l.clone())
            .unwrap_or_default()
    }

    fn token_path(&self) -> PathBuf {
        PathBuf::from(snapshot(&self.settings).token_file)
    }

    /// `store_for(C.TOKEN_FILE, getattr(C, "KITE_TOKEN_ENCRYPTION_KEY", ""))` — `app.config`
    /// has no such attribute, so the configured key is always `""` and the environment (then
    /// `.env`) decides.
    fn store(&self) -> R<baskfy_providers::tokens::AccessTokenStore> {
        let path = self.token_path();
        super::token_store::store_for(&path, "").map_err(|e| {
            let (python_type, message) =
                super::token_store::os_error(&e, &super::token_store::key_path(&path));
            raise(&python_type, message)
        })
    }

    fn mtime(path: &std::path::Path) -> Option<std::time::SystemTime> {
        std::fs::metadata(path).and_then(|m| m.modified()).ok()
    }

    /// `_load_token()`: a token that cannot be read is the same as none (log in again); a
    /// store that cannot be built raises.
    fn load_token(&self) -> R<()> {
        let store = self.store()?;
        let seen = if store.exists() {
            match store.load() {
                Ok(token) => {
                    self.kc.set_access_token(&token.value);
                    Self::mtime(store.path())
                }
                Err(exc) => {
                    log(
                        "WARNING",
                        &format!(
                            "could not read the stored access token ({}); log in again",
                            exc.message()
                        ),
                    );
                    None
                }
            }
        } else {
            None
        };
        if let Ok(mut m) = self.token_mtime.lock() {
            *m = seen;
        }
        Ok(())
    }

    /// `refresh_token_if_changed()` on a built `Kite`.
    fn refresh(&self) -> R<bool> {
        // `store_for` first, as Python does: it is what bootstraps the fallback key file.
        let store = self.store()?;
        let seen = Self::mtime(store.path());
        let known = self.token_mtime.lock().map(|m| *m).unwrap_or(None);
        if seen.is_none() || seen == known {
            return Ok(false);
        }
        log(
            "INFO",
            "the stored Kite token changed on disk; re-reading it",
        );
        self.load_token()?;
        Ok(true)
    }

    /// `refresh_token_if_changed()`: re-read the blob when somebody else wrote a newer token.
    ///
    /// # Errors
    /// As [`DeskKite::ready`], or the store cannot be built.
    pub fn refresh_token_if_changed(&self) -> Result<bool, ClientError> {
        self.ready()?;
        self.refresh()
    }

    /// `login_url()`.
    ///
    /// # Errors
    /// As [`DeskKite::ready`].
    pub fn login_url(&self) -> Result<String, ClientError> {
        self.ready()?;
        Ok(self.kc.login_url())
    }

    /// `exchange_token(request_token)`: the login exchange, then the token encrypted at rest.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the exchange; a reply without `access_token`; the store.
    pub async fn exchange_token(&self, request_token: &str) -> Result<(), ClientError> {
        self.ready()?;
        let secret = Secret::new(snapshot(&self.settings).kite_api_secret);
        let data = self.kc.generate_session(request_token, &secret).await?;
        let token = match subscript(&data, "access_token")? {
            PyValue::Str(t) => t.clone(),
            other => {
                return Err(raise(
                    "TypeError",
                    format!("the session's access_token is a {}", other.type_name()),
                ));
            }
        };
        self.kc.set_access_token(&Secret::new(token.clone()));
        let store = self.store()?;
        store
            .save(&token, None)
            .map_err(|e| raise(e.python_type(), e.message().to_owned()))?;
        Ok(())
    }

    /// `is_authed()`, raising where Python's does (`kite()` unconfigured, the store unbuilt);
    /// a refused session or a limiter error is `false`.
    ///
    /// # Errors
    /// As [`DeskKite::ready`], or the token store.
    pub async fn try_is_authed(&self) -> Result<bool, ClientError> {
        self.ready()?;
        self.refresh()?;
        self.slot("general").await;
        Ok(self.kc.profile().await.is_ok())
    }

    /// `kite().is_authed()` for a caller that treats any failure as "not authed" (an
    /// unconfigured desk is not authed). [`DeskKite::try_is_authed`] raises instead.
    pub async fn is_authed(&self) -> bool {
        self.try_is_authed().await.unwrap_or(false)
    }

    /// `holdings()`: what is owned right now — `quantity + t1_quantity + collateral_quantity`
    /// (non-negotiable 2; pledged shares are counted and surfaced as `pledged_qty`, never a
    /// gate — non-negotiable 3), plus today's POSITIVE CNC day buys (net of overnight carry),
    /// which Kite moves into holdings only at T+1.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the holdings read or a malformed reply (Python's exception);
    /// `RuntimeError` when positions cannot be read (refuse rather than under-report).
    pub async fn holdings(&self) -> Result<Vec<PyDict>, ClientError> {
        self.ready()?;
        self.refresh()?;
        let mut out: Vec<(PyValue, PyDict)> = Vec::new();
        self.slot("general").await;
        let reply = self.kc.holdings().await?;
        for h in py_iter(&reply)? {
            let quantity = subscript(&h, "quantity")?;
            let PyValue::Map(hm) = &h else { continue };
            let t1 = hm.get("t1_quantity").unwrap_or(&ZERO);
            let collateral = hm.get("collateral_quantity").unwrap_or(&ZERO);
            let total = add(&add(quantity, t1)?, collateral)?;
            if le_zero(&total)? {
                continue;
            }
            let row = dict([
                ("symbol", subscript(&h, "tradingsymbol")?.clone()),
                ("exchange", subscript(&h, "exchange")?.clone()),
                ("quantity", total),
                ("pledged_qty", collateral.clone()),
                ("average_price", subscript(&h, "average_price")?.clone()),
                ("last_price", subscript(&h, "last_price")?.clone()),
            ]);
            let key = subscript(&h, "tradingsymbol")?.clone();
            hashable(&key)?;
            match out.iter_mut().find(|(k, _)| key_eq(k, &key)) {
                Some((_, slot)) => *slot = row,
                None => out.push((key, row)),
            }
        }

        self.slot("general").await;
        let positions = match self.kc.positions().await {
            Ok(PyValue::Map(m)) => match m.get("net") {
                Some(net) if net.truthy() => net.clone(),
                Some(_) => PyValue::List(Vec::new()),
                None => PyValue::List(Vec::new()),
            },
            Ok(other) => {
                return Err(runtime(format!(
                    "could not read positions, so today's purchases cannot be counted: {}",
                    no_attribute(&other, "get").message
                )));
            }
            Err(exc) => {
                return Err(runtime(format!(
                    "could not read positions, so today's purchases cannot be counted: {exc}"
                )));
            }
        };
        for p in py_iter(&positions)? {
            let product = attr_get(&p, "product")?;
            let product = if product.truthy() {
                product.py_str()
            } else {
                String::new()
            };
            if product != "CNC" {
                continue;
            }
            let or_zero = |v: &PyValue| if v.truthy() { v.clone() } else { ZERO.clone() };
            let quantity = to_int(&or_zero(attr_get(&p, "quantity")?))?;
            let overnight = to_int(&or_zero(attr_get(&p, "overnight_quantity")?))?;
            let qty = quantity
                .checked_sub(overnight)
                .ok_or_else(|| raise("OverflowError", "int too large to convert to i64"))?;
            if qty <= 0 {
                continue;
            }
            let sym = subscript(&p, "tradingsymbol")?.clone();
            hashable(&sym)?;
            if let Some((_, row)) = out.iter_mut().find(|(k, _)| key_eq(k, &sym)) {
                let q = add(row.get("quantity").unwrap_or(&NONE), &PyValue::Int(qty))?;
                row.insert("quantity", q);
            } else {
                let PyValue::Map(pm) = &p else { continue };
                let or_float = |k: &str| {
                    let v = pm.get(k).unwrap_or(&NONE);
                    if v.truthy() {
                        v.clone()
                    } else {
                        PyValue::Float(0.0)
                    }
                };
                let row = dict([
                    ("symbol", sym.clone()),
                    (
                        "exchange",
                        pm.get("exchange").cloned().unwrap_or_else(|| s("NSE")),
                    ),
                    ("quantity", PyValue::Int(qty)),
                    ("pledged_qty", PyValue::Int(0)),
                    ("average_price", or_float("average_price")),
                    ("last_price", or_float("last_price")),
                ]);
                out.push((sym, row));
            }
        }
        Ok(out.into_iter().map(|(_, r)| r).collect())
    }

    /// `trades()`: today's executed trades (the book is same-day only). No token re-read, as
    /// Python's has none.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read; a reply `list()` refuses.
    pub async fn trades(&self) -> Result<Vec<PyValue>, ClientError> {
        self.ready()?;
        self.slot("general").await;
        list_or_empty(&self.kc.trades().await?)
    }

    /// `available_cash()`: `float(margins()["equity"]["available"]["live_balance"])`.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read; Python's exception on a malformed payload.
    pub async fn available_cash(&self) -> Result<f64, ClientError> {
        self.ready()?;
        self.slot("general").await;
        let m = self.kc.margins(None).await?;
        let equity = subscript(&m, "equity")?;
        let available = subscript(equity, "available")?;
        to_float(subscript(available, "live_balance")?)
    }

    /// `ltp(symbols, exchange)`: last prices, a `LTP_CACHE_SECONDS` cache first (on
    /// `time.monotonic()`); only the names with no fresh answer cost a (quote-family) call.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read; Python's exception on a malformed entry (the entries
    /// before it are cached, as Python's are).
    pub async fn ltp(&self, symbols: &[String], exchange: &str) -> Result<PyDict, ClientError> {
        self.ready()?;
        self.refresh()?;
        let mut wanted: Vec<&String> = Vec::new();
        for sym in symbols {
            if !sym.is_empty() && !wanted.contains(&sym) {
                wanted.push(sym);
            }
        }
        let now = self.cache_clock.monotonic();
        let mut out = PyDict::new();
        let mut missing: Vec<String> = Vec::new();
        {
            let cache = self
                .ltp_cache
                .lock()
                .map_err(|_| runtime("ltp cache poisoned"))?;
            for sym in wanted {
                match cache.get(&format!("{exchange}:{sym}")) {
                    Some((at, price)) if now - at < LTP_CACHE_SECONDS => {
                        out.insert(sym.clone(), PyValue::Float(*price));
                    }
                    _ => missing.push(sym.clone()),
                }
            }
        }
        for chunk in missing.chunks(LTP_BATCH_SIZE) {
            let keys: Vec<String> = chunk
                .iter()
                .map(|sym| format!("{exchange}:{sym}"))
                .collect();
            self.slot("quote").await;
            let data = self.kc.ltp(&keys).await?;
            let fetched = self.cache_clock.monotonic();
            for (key, value) in items(&data)?.iter() {
                let price = to_float(subscript(value, "last_price")?)?;
                if let Ok(mut cache) = self.ltp_cache.lock() {
                    cache.insert(key.to_owned(), (fetched, price));
                }
                out.insert(after_colon(key)?, PyValue::Float(price));
            }
        }
        Ok(out)
    }

    /// `quotes(symbols, exchange)`: the desk's shape — chunked under 500, symbol-keyed; a batch
    /// whose call fails is skipped (logged), a reply that is not a dict raises, as Python's does.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; Python's exception on a malformed reply.
    pub async fn quotes(&self, symbols: &[String], exchange: &str) -> Result<PyDict, ClientError> {
        self.ready()?;
        self.refresh()?;
        let mut clean: Vec<&String> = Vec::new();
        for sym in symbols {
            if !sym.is_empty() && !clean.contains(&sym) {
                clean.push(sym);
            }
        }
        let mut out = PyDict::new();
        for chunk in clean.chunks(QUOTE_BATCH_SIZE) {
            let keys: Vec<String> = chunk
                .iter()
                .map(|sym| format!("{exchange}:{sym}"))
                .collect();
            self.slot("quote").await;
            let data = match self.kc.quote(&keys).await {
                Ok(d) => d,
                Err(e) => {
                    log(
                        "WARNING",
                        &format!("quote batch failed ({} symbols): {e}", keys.len()),
                    );
                    continue;
                }
            };
            for (k, v) in items(&data)?.iter() {
                let ohlc = attr_get(v, "ohlc")?;
                let empty = PyValue::Map(PyDict::new());
                let ohlc = if ohlc.truthy() { ohlc } else { &empty };
                let volume = attr_get(v, "volume")?;
                let volume = if volume.truthy() {
                    volume
                } else {
                    attr_get(v, "volume_traded")?
                };
                let row = dict([
                    ("last_price", attr_get(v, "last_price")?.clone()),
                    ("net_change", attr_get(v, "net_change")?.clone()),
                    ("open", attr_get(ohlc, "open")?.clone()),
                    ("high", attr_get(ohlc, "high")?.clone()),
                    ("low", attr_get(ohlc, "low")?.clone()),
                    ("prev_close", attr_get(ohlc, "close")?.clone()),
                    ("volume", volume.clone()),
                ]);
                out.insert(after_colon(k)?, PyValue::Map(row));
            }
        }
        Ok(out)
    }

    /// `instruments(exchange)`: one general slot.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read.
    pub async fn instruments(&self, exchange: &str) -> Result<Vec<PyValue>, ClientError> {
        self.ready()?;
        self.slot("general").await;
        list_or_empty(&self.kc.instruments(Some(exchange)).await?)
    }

    /// `historical(token, start, end, interval)`: the 3 req/s family.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read.
    pub async fn historical(
        &self,
        token: i64,
        start: &str,
        end: &str,
        interval: &str,
    ) -> Result<Vec<PyValue>, ClientError> {
        self.ready()?;
        self.refresh()?;
        self.slot("historical").await;
        list_or_empty(
            &self
                .kc
                .historical_data(token, start, end, interval, false, false)
                .await?,
        )
    }

    /// `margins(segment)`: `dict(...)` of the raw payload.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read; a payload `dict()` refuses.
    pub async fn margins(&self, segment: Option<&str>) -> Result<PyValue, ClientError> {
        self.ready()?;
        self.refresh()?;
        self.slot("general").await;
        let raw = self.kc.margins(segment.filter(|s| !s.is_empty())).await?;
        Ok(PyValue::Map(py_dict(&raw)?))
    }

    /// `orders()`.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read.
    pub async fn orders(&self) -> Result<Vec<PyValue>, ClientError> {
        self.ready()?;
        self.refresh()?;
        self.slot("general").await;
        list_or_empty(&self.kc.orders().await?)
    }

    /// `order_history(order_id)`.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read.
    pub async fn order_history(&self, order_id: &str) -> Result<Vec<PyValue>, ClientError> {
        self.ready()?;
        self.refresh()?;
        self.slot("general").await;
        list_or_empty(&self.kc.order_history(order_id).await?)
    }

    /// `quote_raw(keys)`: `dict(quote(keys) or {})`, one quote slot.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read; a payload `dict()` refuses.
    pub async fn quote_raw(&self, keys: &[String]) -> Result<PyValue, ClientError> {
        self.ready()?;
        self.refresh()?;
        self.slot("quote").await;
        let raw = self.kc.quote(keys).await?;
        if !raw.truthy() {
            return Ok(PyValue::Map(PyDict::new()));
        }
        Ok(PyValue::Map(py_dict(&raw)?))
    }

    /// `get_gtts()`: every resting GTT (a read).
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the read.
    pub async fn get_gtts(&self) -> Result<Vec<PyValue>, ClientError> {
        self.ready()?;
        self.refresh()?;
        self.slot("general").await;
        list_or_empty(&self.kc.get_gtts().await?)
    }

    /// `tick_size(symbol, exchange)`: `{i["tradingsymbol"]: float(i.get("tick_size") or 0.05)}`
    /// from the dump, cached per exchange for the process.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; the instrument read; Python's exception on a malformed row.
    pub async fn tick_size(&self, symbol: &str, exchange: &str) -> Result<f64, ClientError> {
        self.ready()?;
        let cached = self.ticks.lock().is_ok_and(|t| t.contains_key(exchange));
        if !cached {
            let dump = self.instruments(exchange).await?;
            let mut table = HashMap::new();
            for i in &dump {
                let key = subscript(i, "tradingsymbol")?;
                let tick = attr_get(i, "tick_size")?;
                let tick = if tick.truthy() {
                    to_float(tick)?
                } else {
                    DEFAULT_TICK
                };
                hashable(key)?;
                if let PyValue::Str(sym) = key {
                    table.insert(sym.clone(), tick);
                }
            }
            if let Ok(mut t) = self.ticks.lock() {
                t.insert(exchange.to_owned(), table);
            }
        }
        Ok(self
            .ticks
            .lock()
            .ok()
            .and_then(|t| t.get(exchange).and_then(|m| m.get(symbol)).copied())
            .unwrap_or(DEFAULT_TICK))
    }

    /// `delete_gtt(gtt_id, symbol)`: the documented exception — its own untouchable guard (when
    /// a symbol is named), DRY_RUN simulated.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; `UntouchableInstrumentError`.
    pub async fn delete_gtt(&self, gtt_id: i64, symbol: &str) -> Result<PyDict, ClientError> {
        self.ready()?;
        if !symbol.is_empty() {
            assert_tradeable(symbol, None)?;
        }
        let out = |status: &str| {
            dict([
                ("symbol", s(symbol)),
                ("gtt_id", PyValue::Int(gtt_id)),
                ("status", s(status)),
            ])
        };
        if snapshot(&self.settings).dry_run {
            return Ok(out("DRY_RUN_GTT_DELETE"));
        }
        match self.kc.delete_gtt(gtt_id).await {
            Ok(_) => Ok(out("GTT_DELETED")),
            Err(exc) => {
                let mut d = out("GTT_DELETE_ERROR");
                d.insert("error", s(&exc.to_string()));
                Ok(d)
            }
        }
    }

    /// `place_gtt_stop(symbol, qty, trigger, last_price, exchange)`: the documented exception
    /// of non-negotiable 6 — the untouchable and overnight-option guards first, DRY_RUN
    /// simulated (the unsnapped trigger), else snapped to the tick with the limit at 99.5%.
    ///
    /// # Errors
    /// As [`DeskKite::ready`]; `UntouchableInstrumentError` / `OvernightOptionError`.
    pub async fn place_gtt_stop(
        &self,
        symbol: &str,
        qty: i64,
        trigger: f64,
        last_price: f64,
        exchange: &str,
    ) -> Result<PyDict, ClientError> {
        self.ready()?;
        assert_tradeable(symbol, None)?;
        assert_not_overnight_option(symbol, exchange, "CNC")?;
        if snapshot(&self.settings).dry_run {
            return Ok(dict([
                ("symbol", s(symbol)),
                ("status", s("DRY_RUN_GTT")),
                ("trigger", PyValue::Float(trigger)),
                ("qty", PyValue::Int(qty)),
            ]));
        }
        let error = |text: String| {
            dict([
                ("symbol", s(symbol)),
                ("status", s("GTT_ERROR")),
                ("error", s(&text)),
            ])
        };
        let tick = match self.tick_size(symbol, exchange).await {
            Ok(t) => t,
            Err(e) => return Ok(error(e.message)),
        };
        let trig = to_tick(trigger, tick);
        let limit = to_tick(trig * 0.995, tick);
        let params = super::gtt::gtt_params(symbol, exchange, qty, trig, limit, last_price, None);
        let gid = match self.kc.place_gtt(&params).await {
            Ok(gid) => gid,
            Err(exc) => return Ok(error(exc.to_string())),
        };
        match subscript(&gid, "trigger_id") {
            Ok(id) => Ok(dict([
                ("symbol", s(symbol)),
                ("status", s("GTT_PLACED")),
                ("gtt_id", id.clone()),
                ("trigger", PyValue::Float(trig)),
                ("limit", PyValue::Float(limit)),
            ])),
            Err(e) => Ok(error(e.message)),
        }
    }
}
