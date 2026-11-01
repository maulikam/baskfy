//! `KiteProvider` — the `BarsProvider` and `HoldingsProvider` ports over Zerodha Kite Connect.
//! Ports `baskfy_providers.kite` (and, because Kite Connect has no Rust SDK, the read-only REST
//! half of `kiteconnect` 5.2.1's `KiteConnect` that it drives).
//!
//! docs/09 §"Kite specifics" is the whole specification for this module:
//!
//! * the access token is daily, stored encrypted, and its expiry must alert loudly;
//! * the rate limit is ~3 req/s and the limiter is shared across workers via Redis;
//! * day-interval history is capped per request, so backfills chunk into <= 2000-day slices;
//! * **Kite returns unadjusted OHLC.** Everything from here is raw. The adjusted series is
//!   derived by the pipeline's own adjustment step, never by a provider.
//!
//! docs/02 adds the negative space: no index constituents, no PE/PB, no corporate-action
//! calendar. Those belong to the NSE provider, and this type does not pretend to offer them.
//!
//! # Layers
//!
//! * [`KiteValue`] — what the Python SDK hands back: JSON values in **document order**, plus
//!   the `date`/`datetime` objects the SDK parses out of strings (`dateutil`), plus the raw
//!   instrument dump's typed CSV cells. The mapping functions below read it exactly as the
//!   Python reads its dicts (`row.get(...)`, truthiness, `str(value)`).
//! * [`KiteClient`] — `KiteClientLike`, the slice of `KiteConnect` this adapter uses. Tests
//!   inject fakes through [`KiteRuntime::client_factory`], exactly as the Python tests do.
//! * [`KiteConnect`] — the production client: `kiteconnect.KiteConnect`'s read-only routes,
//!   with its URL, query encoding (`requests`' `quote_plus`), headers, timeout, response
//!   validation and error classes reproduced. **It has no order verb** (law 2: the order path is
//!   `baskfy-execution`, wave 4 step 6).
//! * [`KiteProvider`] — the adapter: rate limit, then retry, then translate, then map.
//!
//! # Exception translation
//!
//! kiteconnect raises its own hierarchy ([`KiteError`]). It is mapped onto ours at this
//! boundary so nothing downstream has to know a vendor error to decide whether to retry:
//!
//! ```text
//! TokenException      -> AccessTokenExpired    (loud; never retried)
//! NetworkException    -> UpstreamUnavailable   (retried; RateLimited when it says 429)
//! DataException       -> UpstreamUnavailable   (retried; it signals an upstream fault)
//! PermissionException -> ProviderUnavailable   (the API key lacks historical-data access)
//! InputException      -> UnexpectedPayload     (our request was wrong; retrying will not help)
//! ```
//!
//! **KNOWN BUG, reproduced (DECISIONS-RS R4.10):** the Python table's `TimeoutError` and
//! `ConnectionError` rows name Python's *builtin* classes, but `requests` raises its own
//! `requests.exceptions.ReadTimeout`/`ConnectionError`, which are `OSError`s and subclass
//! neither. So a Kite read timeout or refused connection is passed through untranslated, is
//! not transient, and is **not retried**. [`KiteConnect`] reproduces that: its transport
//! failures are [`KiteError::Other`] carrying `requests`' class name. The fix, later and in
//! both languages at once, is to translate the transport classes to `UpstreamUnavailable`.

use std::collections::BTreeMap;
use std::fmt;
use std::str::FromStr;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use chrono::{
    DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, Timelike,
};
use polars::prelude::{
    DataFrame, Int128Chunked, IntoColumn, IntoSeries, NamedFrom, NewChunkedArray, Series,
};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};

use crate::errors::{ErrorKind, ProviderError};
use crate::net::{GuardedClient, client_builder};
use crate::ports::{
    BarsProvider, CapabilitySet, HealthReporting, HoldingsProvider, ProviderHealth,
    bars_capabilities, holdings_capabilities,
};
use crate::py::{general, json_str, repr_float, repr_str};
use crate::ratelimit::RateLimiter;
use crate::records::{
    BasketMarginFields, BasketMarginRecord, BrokerAccountRef, BrokerHoldingFields,
    BrokerHoldingRecord, DepthLevelFields, DepthLevelRecord, InstrumentFields, InstrumentRecord,
    InstrumentType, MarginLegRecord, MinuteBarFields, MinuteBarRecord, OptionContractFields,
    OptionContractRecord, OptionQuoteFields, OptionQuoteRecord, OptionType, PyDateTime,
    QuoteFields, QuoteRecord, RecordError, daily_bars_schema, empty_frame,
};
use crate::retry::{RetryHooks, RetryPolicy, call_with_retry};
use crate::secret::Secret;
use crate::settings::ProviderSettings;
use crate::tokens::{AccessTokenStore, ist};

/// `PROVIDER_NAME`.
pub const PROVIDER_NAME: &str = "kite";

/// `DEFAULT_MAX_DAYS_PER_REQUEST`: Kite's `day` interval history cap. docs/09: "chunk
/// backfills into <= 2000-day slices".
pub const DEFAULT_MAX_DAYS_PER_REQUEST: i64 = 2000;

/// `QUOTE_BATCH_SIZE`: Kite's `GET /quote` accepts up to 500 instruments per call. A larger
/// batch is refused with a 400, which the retry policy would then repeat — so the cap is
/// enforced here, before the call.
pub const QUOTE_BATCH_SIZE: usize = 500;

/// `MINUTE_MAX_DAYS_PER_REQUEST`: Kite's `minute` interval history cap per request, in calendar
/// days. A wider window is refused with an `InputException`, which retrying would not cure.
pub const MINUTE_MAX_DAYS_PER_REQUEST: i64 = 60;

/// `KITE_BROKER_ID`: the catalog id this adapter is the adapter *for*. A ref naming any other
/// broker is refused rather than served.
pub const KITE_BROKER_ID: &str = "zerodha";

/// `_EQUITY_SEGMENT`: the margins segment whose `net` is the user's "cash".
const EQUITY_SEGMENT: &str = "equity";

/// `_INDEX_SEGMENT`: the instrument dump's segment for indices.
const INDEX_SEGMENT: &str = "INDICES";

/// `_SME_SUFFIXES`: Kite spells an NSE Emerge symbol with its series appended (`SHEETAL-SM`)
/// where NSE's register and the bhavcopy use the bare `SHEETAL` (M61). Only these three: `-RE`
/// (rights entitlement) and `-BE` are different instruments, not the share renamed.
const SME_SUFFIXES: [&str; 3] = ["-SM", "-ST", "-SZ"];

/// `_EQUITY_EXCHANGES`: the exchanges whose trades are cash equity.
const EQUITY_EXCHANGES: [&str; 2] = ["NSE", "BSE"];

/// `KiteProvider.INDEX_QUOTE_SYMBOL`: our universe slug -> Kite's own index tradingsymbol.
/// Kite abbreviates; nothing derives one name from the other, so the ones the swing gate can
/// ask about are written out.
pub const INDEX_QUOTE_SYMBOL: [(&str, &str); 3] = [
    ("nifty-smallcap-250", "NIFTY SMLCAP 250"),
    ("nifty-mid-small-400", "NIFTY MIDSML 400"),
    ("nifty-500", "NIFTY 500"),
];

// =============================================================================================
// KiteValue — the Python objects the SDK hands back
// =============================================================================================

/// One value as `kiteconnect` returns it to Python: a JSON value (objects keep document order,
/// as Python's `json` does), or one of the objects the SDK makes out of strings — a `date`
/// (the instrument dump's `expiry`) or a `datetime` (candle stamps, quote and trade timestamps).
#[derive(Debug, Clone, PartialEq)]
pub enum KiteValue {
    /// `None` / JSON `null`.
    None,
    /// `bool`.
    Bool(bool),
    /// `int` (JSON integers within `i64`).
    Int(i64),
    /// `float`.
    Float(f64),
    /// `str`.
    Str(String),
    /// `bytes` (a CSV body returned where JSON was expected).
    Bytes(Vec<u8>),
    /// `datetime.date`.
    Date(NaiveDate),
    /// `datetime.datetime`, naive or aware.
    DateTime(PyDateTime),
    /// `list`.
    List(Vec<KiteValue>),
    /// `dict`.
    Map(KiteMap),
}

/// A Python `dict` with string keys, in insertion order.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct KiteMap(Vec<(String, KiteValue)>);

static NONE: KiteValue = KiteValue::None;

impl KiteMap {
    /// An empty dict.
    #[must_use]
    pub const fn new() -> Self {
        Self(Vec::new())
    }

    /// `d.get(key)`.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&KiteValue> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    /// `d[key] = value`: replaces in place (keeping the first position), else appends.
    pub fn insert(&mut self, key: impl Into<String>, value: KiteValue) {
        let key = key.into();
        if let Some(slot) = self.0.iter_mut().find(|(k, _)| *k == key) {
            slot.1 = value;
        } else {
            self.0.push((key, value));
        }
    }

    /// `d.items()`, in order.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &KiteValue)> {
        self.0.iter().map(|(k, v)| (k.as_str(), v))
    }

    /// `len(d)`.
    #[must_use]
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// `not d`.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl<K: Into<String>> FromIterator<(K, KiteValue)> for KiteMap {
    fn from_iter<I: IntoIterator<Item = (K, KiteValue)>>(iter: I) -> Self {
        let mut map = Self::new();
        for (k, v) in iter {
            map.insert(k, v);
        }
        map
    }
}

impl From<&str> for KiteValue {
    fn from(value: &str) -> Self {
        Self::Str(value.to_owned())
    }
}

impl From<String> for KiteValue {
    fn from(value: String) -> Self {
        Self::Str(value)
    }
}

impl From<i64> for KiteValue {
    fn from(value: i64) -> Self {
        Self::Int(value)
    }
}

impl From<f64> for KiteValue {
    fn from(value: f64) -> Self {
        Self::Float(value)
    }
}

impl From<bool> for KiteValue {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}

impl From<NaiveDate> for KiteValue {
    fn from(value: NaiveDate) -> Self {
        Self::Date(value)
    }
}

impl From<NaiveDateTime> for KiteValue {
    fn from(value: NaiveDateTime) -> Self {
        Self::DateTime(PyDateTime::Naive(value))
    }
}

impl From<DateTime<FixedOffset>> for KiteValue {
    fn from(value: DateTime<FixedOffset>) -> Self {
        Self::DateTime(PyDateTime::Aware(value))
    }
}

impl From<Vec<Self>> for KiteValue {
    fn from(value: Vec<Self>) -> Self {
        Self::List(value)
    }
}

impl From<KiteMap> for KiteValue {
    fn from(value: KiteMap) -> Self {
        Self::Map(value)
    }
}

impl<T: Into<Self>> From<Option<T>> for KiteValue {
    fn from(value: Option<T>) -> Self {
        value.map_or(Self::None, Into::into)
    }
}

impl KiteValue {
    /// `type(value).__name__`.
    #[must_use]
    pub const fn type_name(&self) -> &'static str {
        match self {
            Self::None => "NoneType",
            Self::Bool(_) => "bool",
            Self::Int(_) => "int",
            Self::Float(_) => "float",
            Self::Str(_) => "str",
            Self::Bytes(_) => "bytes",
            Self::Date(_) => "date",
            Self::DateTime(_) => "datetime",
            Self::List(_) => "list",
            Self::Map(_) => "dict",
        }
    }

    /// `bool(value)`.
    #[must_use]
    pub fn truthy(&self) -> bool {
        match self {
            Self::None => false,
            Self::Bool(b) => *b,
            Self::Int(i) => *i != 0,
            Self::Float(f) => *f != 0.0,
            Self::Str(s) => !s.is_empty(),
            Self::Bytes(b) => !b.is_empty(),
            Self::Date(_) | Self::DateTime(_) => true,
            Self::List(items) => !items.is_empty(),
            Self::Map(map) => !map.is_empty(),
        }
    }

    /// `a or b`.
    #[must_use]
    pub fn or<'a>(&'a self, other: &'a Self) -> &'a Self {
        if self.truthy() { self } else { other }
    }

    /// `str(value)`.
    #[must_use]
    pub fn py_str(&self) -> String {
        match self {
            Self::Str(s) => s.clone(),
            Self::DateTime(dt) => py_datetime_str(dt),
            Self::Date(d) => d.format("%Y-%m-%d").to_string(),
            other => other.py_repr(),
        }
    }

    /// `repr(value)`. Aware datetimes print `dateutil`'s `tzoffset`, as the SDK makes them.
    #[must_use]
    pub fn py_repr(&self) -> String {
        match self {
            Self::None => "None".to_owned(),
            Self::Bool(true) => "True".to_owned(),
            Self::Bool(false) => "False".to_owned(),
            Self::Int(i) => i.to_string(),
            Self::Float(f) => repr_float(*f),
            Self::Str(s) => repr_str(s),
            Self::Bytes(b) => repr_bytes(b),
            Self::Date(d) => format!("datetime.date({}, {}, {})", d.year(), d.month(), d.day()),
            Self::DateTime(dt) => py_datetime_repr(dt),
            Self::List(items) => {
                let inner: Vec<String> = items.iter().map(Self::py_repr).collect();
                format!("[{}]", inner.join(", "))
            }
            Self::Map(map) => {
                let inner: Vec<String> = map
                    .iter()
                    .map(|(k, v)| format!("{}: {}", repr_str(k), v.py_repr()))
                    .collect();
                format!("{{{}}}", inner.join(", "))
            }
        }
    }

    /// `row.get(key)` — `AttributeError` when the row is not a dict, as Python's would be.
    ///
    /// # Errors
    /// `AttributeError` for a non-dict.
    pub fn get(&self, key: &str) -> Result<&Self, ProviderError> {
        match self {
            Self::Map(map) => Ok(map.get(key).unwrap_or(&NONE)),
            other => Err(no_attribute(other, "get")),
        }
    }

    /// `for item in value` — a dict iterates its keys, bytes their integers.
    ///
    /// # Errors
    /// `TypeError` for a value Python cannot iterate.
    pub fn py_iter(&self) -> Result<Vec<Self>, ProviderError> {
        match self {
            Self::List(items) => Ok(items.clone()),
            Self::Map(map) => Ok(map.iter().map(|(k, _)| Self::Str(k.to_owned())).collect()),
            Self::Str(s) => Ok(s.chars().map(|c| Self::Str(c.to_string())).collect()),
            Self::Bytes(b) => Ok(b.iter().map(|x| Self::Int(i64::from(*x))).collect()),
            other => Err(ProviderError::foreign(
                "TypeError",
                format!("'{}' object is not iterable", other.type_name()),
            )),
        }
    }

    /// Parses a JSON document as Python's `json.loads` would, keeping object order.
    ///
    /// # Errors
    /// The parser's message when the bytes are not JSON.
    pub fn from_json(bytes: &[u8]) -> Result<Self, String> {
        serde_json::from_slice(bytes).map_err(|e| e.to_string())
    }
}

fn no_attribute(value: &KiteValue, attribute: &str) -> ProviderError {
    ProviderError::foreign(
        "AttributeError",
        format!(
            "'{}' object has no attribute '{attribute}'",
            value.type_name()
        ),
    )
}

impl<'de> Deserialize<'de> for KiteValue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(KiteValueVisitor)
    }
}

struct KiteValueVisitor;

impl<'de> Visitor<'de> for KiteValueVisitor {
    type Value = KiteValue;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a JSON value")
    }

    fn visit_unit<E: de::Error>(self) -> Result<KiteValue, E> {
        Ok(KiteValue::None)
    }

    fn visit_none<E: de::Error>(self) -> Result<KiteValue, E> {
        Ok(KiteValue::None)
    }

    fn visit_bool<E: de::Error>(self, v: bool) -> Result<KiteValue, E> {
        Ok(KiteValue::Bool(v))
    }

    fn visit_i64<E: de::Error>(self, v: i64) -> Result<KiteValue, E> {
        Ok(KiteValue::Int(v))
    }

    #[allow(clippy::cast_precision_loss)]
    fn visit_u64<E: de::Error>(self, v: u64) -> Result<KiteValue, E> {
        // Python keeps a wider int exactly; Kite never sends one (DECISIONS-RS R4.10).
        Ok(i64::try_from(v).map_or(KiteValue::Float(v as f64), KiteValue::Int))
    }

    fn visit_f64<E: de::Error>(self, v: f64) -> Result<KiteValue, E> {
        Ok(KiteValue::Float(v))
    }

    fn visit_str<E: de::Error>(self, v: &str) -> Result<KiteValue, E> {
        Ok(KiteValue::Str(v.to_owned()))
    }

    fn visit_string<E: de::Error>(self, v: String) -> Result<KiteValue, E> {
        Ok(KiteValue::Str(v))
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<KiteValue, A::Error> {
        let mut items = Vec::new();
        while let Some(item) = seq.next_element::<KiteValue>()? {
            items.push(item);
        }
        Ok(KiteValue::List(items))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<KiteValue, A::Error> {
        let mut map = KiteMap::new();
        while let Some((k, v)) = access.next_entry::<String, KiteValue>()? {
            map.insert(k, v);
        }
        Ok(KiteValue::Map(map))
    }
}

/// `repr(b)` for Python `bytes`.
#[must_use]
pub fn repr_bytes(bytes: &[u8]) -> String {
    let quote = if bytes.contains(&b'\'') && !bytes.contains(&b'"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from("b");
    out.push(quote);
    for &b in bytes {
        match b {
            b'\\' => out.push_str("\\\\"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            x if char::from(x) == quote => {
                out.push('\\');
                out.push(quote);
            }
            0x20..=0x7e => out.push(char::from(b)),
            x => out.push_str(&format!("\\x{x:02x}")),
        }
    }
    out.push(quote);
    out
}

fn py_datetime_str(dt: &PyDateTime) -> String {
    // str(datetime) is isoformat(sep=" ").
    let iso = dt.isoformat();
    match iso.get(..10) {
        Some(head) if iso.len() > 10 => format!("{head} {}", &iso[11..]),
        _ => iso,
    }
}

fn py_datetime_repr(dt: &PyDateTime) -> String {
    let (naive, offset) = match dt {
        PyDateTime::Naive(n) => (*n, None),
        PyDateTime::Aware(a) => (a.naive_local(), Some(a.offset().local_minus_utc())),
    };
    let mut parts = vec![
        naive.year().to_string(),
        naive.month().to_string(),
        naive.day().to_string(),
        naive.hour().to_string(),
        naive.minute().to_string(),
    ];
    let micros = naive.nanosecond() / 1_000;
    if naive.second() != 0 || micros != 0 {
        parts.push(naive.second().to_string());
    }
    if micros != 0 {
        parts.push(micros.to_string());
    }
    let mut out = format!("datetime.datetime({}", parts.join(", "));
    if let Some(seconds) = offset {
        out.push_str(&format!(", tzinfo=tzoffset(None, {seconds})"));
    }
    out.push(')');
    out
}

/// `json.dumps(value)` with Python's defaults (`", "`/`": "` separators, `ensure_ascii`),
/// and `allow_nan=False` as `requests` passes it.
///
/// # Errors
/// `ValueError` for a NaN or infinite float; `TypeError` for a value JSON cannot hold.
pub fn py_json_dumps(value: &KiteValue) -> Result<String, ProviderError> {
    Ok(match value {
        KiteValue::None => "null".to_owned(),
        KiteValue::Bool(true) => "true".to_owned(),
        KiteValue::Bool(false) => "false".to_owned(),
        KiteValue::Int(i) => i.to_string(),
        KiteValue::Float(f) => {
            if !f.is_finite() {
                return Err(ProviderError::value_error(format!(
                    "Out of range float values are not JSON compliant: {}",
                    repr_float(*f)
                )));
            }
            repr_float(*f)
        }
        KiteValue::Str(s) => json_str(s),
        KiteValue::List(items) => {
            let inner: Result<Vec<String>, ProviderError> =
                items.iter().map(py_json_dumps).collect();
            format!("[{}]", inner?.join(", "))
        }
        KiteValue::Map(map) => {
            let mut inner = Vec::with_capacity(map.len());
            for (k, v) in map.iter() {
                inner.push(format!("{}: {}", json_str(k), py_json_dumps(v)?));
            }
            format!("{{{}}}", inner.join(", "))
        }
        other => {
            return Err(ProviderError::foreign(
                "TypeError",
                format!(
                    "Object of type {} is not JSON serializable",
                    other.type_name()
                ),
            ));
        }
    })
}

// =============================================================================================
// Errors — kiteconnect.exceptions
// =============================================================================================

/// The `kiteconnect.exceptions` classes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum KiteExceptionKind {
    /// `KiteException`, the base.
    Kite,
    /// `GeneralException` (500).
    General,
    /// `TokenException` (403).
    Token,
    /// `PermissionException` (403).
    Permission,
    /// `OrderException` (500).
    Order,
    /// `InputException` (400).
    Input,
    /// `DataException` (502).
    Data,
    /// `NetworkException` (503).
    Network,
}

impl KiteExceptionKind {
    /// The class name.
    #[must_use]
    pub const fn class_name(self) -> &'static str {
        match self {
            Self::Kite => "KiteException",
            Self::General => "GeneralException",
            Self::Token => "TokenException",
            Self::Permission => "PermissionException",
            Self::Order => "OrderException",
            Self::Input => "InputException",
            Self::Data => "DataException",
            Self::Network => "NetworkException",
        }
    }

    /// The class's default `code`.
    #[must_use]
    pub const fn default_code(self) -> u16 {
        match self {
            Self::Kite | Self::General | Self::Order => 500,
            Self::Token | Self::Permission => 403,
            Self::Input => 400,
            Self::Data => 502,
            Self::Network => 503,
        }
    }

    /// `getattr(kiteconnect.exceptions, name, GeneralException)` for an exception class name.
    #[must_use]
    pub fn from_class_name(name: &str) -> Self {
        match name {
            "KiteException" => Self::Kite,
            "TokenException" => Self::Token,
            "PermissionException" => Self::Permission,
            "OrderException" => Self::Order,
            "InputException" => Self::Input,
            "DataException" => Self::Data,
            "NetworkException" => Self::Network,
            _ => Self::General,
        }
    }
}

/// What a [`KiteClient`] call can raise.
#[derive(Debug, thiserror::Error)]
pub enum KiteError {
    /// A `kiteconnect.exceptions` exception.
    #[error("{message}")]
    Kite {
        /// Its class.
        kind: KiteExceptionKind,
        /// `str(exc)`.
        message: String,
        /// `exc.code`, the HTTP status.
        code: u16,
    },
    /// Python's builtin `TimeoutError`.
    #[error("{0}")]
    Timeout(String),
    /// Python's builtin `ConnectionError`.
    #[error("{0}")]
    Connection(String),
    /// Anything else — passed through `_translate` untouched (including `requests`' own
    /// transport exceptions; see the module's KNOWN BUG).
    #[error("{0}")]
    Other(ProviderError),
}

impl KiteError {
    /// `ExceptionClass(message)` with the class's default code.
    #[must_use]
    pub fn kite(kind: KiteExceptionKind, message: impl Into<String>) -> Self {
        Self::Kite {
            kind,
            message: message.into(),
            code: kind.default_code(),
        }
    }

    /// `type(exc).__name__`.
    #[must_use]
    pub fn python_type(&self) -> &str {
        match self {
            Self::Kite { kind, .. } => kind.class_name(),
            Self::Timeout(_) => "TimeoutError",
            Self::Connection(_) => "ConnectionError",
            Self::Other(e) => e.python_type(),
        }
    }
}

impl From<ProviderError> for KiteError {
    fn from(value: ProviderError) -> Self {
        Self::Other(value)
    }
}

/// `_rate_limited_or_unavailable`: Kite signals throttling through `NetworkException`.
fn rate_limited_or_unavailable(text: &str) -> ProviderError {
    let lowered = text.to_lowercase();
    if lowered.contains("too many") || lowered.contains("429") {
        return ProviderError::rate_limited(format!("Kite rate-limited the request: {text}"))
            .with_provider(PROVIDER_NAME);
    }
    ProviderError::upstream_unavailable(format!("Kite network failure: {text}"))
        .with_provider(PROVIDER_NAME)
}

/// `_translate`: map a kiteconnect exception onto ours; anything unrecognised passes through.
#[must_use]
pub fn translate(error: KiteError) -> ProviderError {
    let text = error.to_string();
    let translated = match &error {
        KiteError::Other(_) => None,
        KiteError::Kite { kind, .. } => Some(match kind {
            KiteExceptionKind::Token => ProviderError::access_token_expired(Some(&format!(
                "Kite rejected the access token: {text}"
            ))),
            KiteExceptionKind::Permission => ProviderError::provider_unavailable(format!(
                "the Kite API key lacks permission for this call: {text}"
            ))
            .with_provider(PROVIDER_NAME),
            KiteExceptionKind::Input => ProviderError::unexpected_payload(format!(
                "Kite rejected the request as malformed: {text}"
            ))
            .with_provider(PROVIDER_NAME),
            KiteExceptionKind::Network => rate_limited_or_unavailable(&text),
            KiteExceptionKind::Data => {
                ProviderError::upstream_unavailable(format!("Kite returned a bad response: {text}"))
                    .with_provider(PROVIDER_NAME)
            }
            KiteExceptionKind::Kite | KiteExceptionKind::General | KiteExceptionKind::Order => {
                ProviderError::upstream_unavailable(format!("Kite failed: {text}"))
                    .with_provider(PROVIDER_NAME)
            }
        }),
        KiteError::Timeout(_) | KiteError::Connection(_) => Some(
            ProviderError::upstream_unavailable(format!("connection to Kite failed: {text}"))
                .with_provider(PROVIDER_NAME),
        ),
    };
    match (translated, error) {
        (Some(ours), cause) => ours.with_source(cause),
        (None, KiteError::Other(inner)) => inner,
        (None, other) => ProviderError::foreign("Exception", other.to_string()),
    }
}

// =============================================================================================
// KiteClient — KiteClientLike
// =============================================================================================

/// A `historical_data` bound: a `date` (sent as `str(date)`) or a naive datetime (sent as
/// `strftime("%Y-%m-%d %H:%M:%S")`), as the SDK formats them.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum HistoricalBound {
    /// A calendar date.
    Date(NaiveDate),
    /// A naive IST wall-clock moment.
    DateTime(NaiveDateTime),
}

impl HistoricalBound {
    /// The query value the SDK sends.
    #[must_use]
    pub fn as_param(&self) -> String {
        match self {
            Self::Date(d) => d.format("%Y-%m-%d").to_string(),
            Self::DateTime(dt) => dt.format("%Y-%m-%d %H:%M:%S").to_string(),
        }
    }
}

/// `KiteClientLike`: the slice of `kiteconnect.KiteConnect` this adapter uses. Depending on a
/// trait rather than the HTTP client is what lets the tests exercise retry, chunking and error
/// translation with zero network calls. **It carries no order verb** (law 2).
#[async_trait]
pub trait KiteClient: Send + Sync {
    /// `set_access_token`.
    fn set_access_token(&self, access_token: &Secret);

    /// `instruments(exchange)`: the instrument dump's rows.
    async fn instruments(&self, exchange: Option<&str>) -> Result<KiteValue, KiteError>;

    /// `historical_data(token, from, to, interval)`: the candles.
    async fn historical_data(
        &self,
        instrument_token: i64,
        from_date: HistoricalBound,
        to_date: HistoricalBound,
        interval: &str,
    ) -> Result<KiteValue, KiteError>;

    /// `holdings()`.
    async fn holdings(&self) -> Result<KiteValue, KiteError>;

    /// `margins(segment)`.
    async fn margins(&self, segment: Option<&str>) -> Result<KiteValue, KiteError>;

    /// `quote(*instruments)`.
    async fn quote(&self, instruments: &[String]) -> Result<KiteValue, KiteError>;

    /// `trades()`.
    async fn trades(&self) -> Result<KiteValue, KiteError>;

    /// `isinstance(client, BasketMarginClient)`: does this client have Kite's margin
    /// calculator? Kept off the required surface so every existing test double stays valid.
    fn has_basket_order_margins(&self) -> bool {
        false
    }

    /// `basket_order_margins(params, consider_positions, mode)` — a calculation, never an
    /// order.
    async fn basket_order_margins(
        &self,
        params: &[KiteValue],
        consider_positions: bool,
        mode: Option<&str>,
    ) -> Result<KiteValue, KiteError> {
        let _ = (params, consider_positions, mode);
        Err(KiteError::Other(ProviderError::foreign(
            "AttributeError",
            "this Kite client has no attribute 'basket_order_margins'",
        )))
    }
}

// =============================================================================================
// KiteConnect — the read-only REST client (kiteconnect 5.2.1)
// =============================================================================================

/// `KiteConnect._default_root_uri`.
pub const DEFAULT_ROOT_URI: &str = "https://api.kite.trade";

/// `KiteConnect._default_timeout`, seconds.
pub const DEFAULT_TIMEOUT_SECONDS: f64 = 7.0;

/// `KiteConnect.kite_header_version`.
pub const KITE_HEADER_VERSION: &str = "3";

/// `KiteConnect._user_agent()`: `(__title__ + "-python/").capitalize() + __version__`.
pub const USER_AGENT: &str = "Kiteconnect-python/5.2.1";

/// `requests.utils.default_headers()["Accept-Encoding"]` without brotli/zstd installed.
pub const ACCEPT_ENCODING: &str = "gzip, deflate";

/// What a request returned once the SDK validated it.
#[derive(Debug, Clone, PartialEq)]
pub enum Body {
    /// The JSON envelope's `data`.
    Json(KiteValue),
    /// A CSV body's bytes.
    Csv(Vec<u8>),
}

/// The SDK's `requests` session: HTTP/1.1, no brotli, a cookie jar, and the connect/read
/// timeout (`timeout or 7`), returned with the effective timeout.
///
/// # Errors
/// `UpstreamUnavailable` when the TLS backend fails.
pub fn sdk_http_client(timeout: Option<f64>) -> Result<(GuardedClient, f64), ProviderError> {
    let timeout = match timeout {
        Some(t) if t != 0.0 && !t.is_nan() => t,
        _ => DEFAULT_TIMEOUT_SECONDS,
    };
    let wait = crate::clock::duration(timeout);
    let builder = client_builder()
        .http1_only()
        .no_brotli()
        .cookie_store(true)
        .connect_timeout(wait)
        .read_timeout(wait);
    Ok((GuardedClient::from_builder(crate::net::with_kite_proxy(builder)?)?, timeout))
}

/// The SDK's headers on `request`, with `Authorization: token key:token` (sensitive) when both
/// are set.
pub fn sdk_headers(
    request: reqwest::RequestBuilder,
    api_key: &Secret,
    access_token: &Secret,
) -> reqwest::RequestBuilder {
    let mut request = request
        .header("User-Agent", USER_AGENT)
        .header("Accept-Encoding", ACCEPT_ENCODING)
        .header("Accept", "*/*")
        .header("Connection", "keep-alive")
        .header("X-Kite-Version", KITE_HEADER_VERSION);
    if api_key.is_set() && access_token.is_set() {
        let header = format!("token {}:{}", api_key.expose(), access_token.expose());
        request = match reqwest::header::HeaderValue::from_str(&header) {
            Ok(mut value) => {
                value.set_sensitive(true);
                request.header(reqwest::header::AUTHORIZATION, value)
            }
            Err(_) => request.header("Authorization", header),
        };
    }
    request
}

/// `_request`'s validation of an answer: the JSON envelope (`status`/`error_type` raise the
/// named exception, else `data`), CSV bytes, or `DataException` for anything else.
///
/// # Errors
/// The SDK's exceptions ([`KiteError`]).
pub fn validate_response(
    status: u16,
    content_type: Option<String>,
    content: &[u8],
) -> Result<Body, KiteError> {
    let Some(content_type) = content_type else {
        return Err(ProviderError::foreign("KeyError", "'content-type'").into());
    };
    if content_type.contains("json") {
        let Ok(data) = KiteValue::from_json(content) else {
            return Err(KiteError::kite(
                KiteExceptionKind::Data,
                format!(
                    "Couldn't parse the JSON response received from the server: {}",
                    repr_bytes(content)
                ),
            ));
        };
        let KiteValue::Map(map) = &data else {
            return Err(no_attribute(&data, "get").into());
        };
        let status_field = map.get("status").unwrap_or(&NONE);
        let error_type = map.get("error_type").unwrap_or(&NONE);
        let is_error = matches!(status_field, KiteValue::Str(s) if s == "error");
        if is_error || error_type.truthy() {
            let KiteValue::Str(class) = error_type else {
                return Err(ProviderError::foreign(
                    "TypeError",
                    format!(
                        "attribute name must be string, not '{}'",
                        error_type.type_name()
                    ),
                )
                .into());
            };
            let Some(message) = map.get("message") else {
                return Err(ProviderError::foreign("KeyError", "'message'").into());
            };
            return Err(KiteError::Kite {
                kind: KiteExceptionKind::from_class_name(class),
                message: message.py_str(),
                code: status,
            });
        }
        return match map.get("data") {
            Some(inner) => Ok(Body::Json(inner.clone())),
            None => Err(ProviderError::foreign("KeyError", "'data'").into()),
        };
    }
    if content_type.contains("csv") {
        return Ok(Body::Csv(content.to_vec()));
    }
    Err(KiteError::kite(
        KiteExceptionKind::Data,
        format!(
            "Unknown Content-Type ({content_type}) with response: ({})",
            repr_bytes(content)
        ),
    ))
}

/// `quote(...)`'s `{key: _format_response(data[key]) for key in data}`.
///
/// # Errors
/// Python's `TypeError` for a non-dict answer; `_format_response`'s failures.
pub fn format_quote(data: &KiteValue) -> Result<KiteValue, KiteError> {
    let KiteValue::Map(map) = data else {
        let error = match data {
            KiteValue::List(items) if items.is_empty() => None,
            KiteValue::Str(text) if text.is_empty() => None,
            KiteValue::Bytes(bytes) if bytes.is_empty() => None,
            KiteValue::List(items) => Some(format!(
                "list indices must be integers or slices, not {}",
                items.first().map_or("str", KiteValue::type_name)
            )),
            KiteValue::Str(_) => Some("string indices must be integers, not 'str'".to_owned()),
            KiteValue::Bytes(_) => {
                Some("byte indices must be integers or slices, not int".to_owned())
            }
            other => Some(format!("'{}' object is not iterable", other.type_name())),
        };
        return match error {
            None => Ok(KiteValue::Map(KiteMap::new())),
            Some(message) => Err(KiteError::Other(ProviderError::foreign(
                "TypeError",
                message,
            ))),
        };
    };
    let mut out = KiteMap::new();
    for (key, value) in map.iter() {
        out.insert(key, format_response(value).map_err(KiteError::Other)?);
    }
    Ok(KiteValue::Map(out))
}

/// The production [`KiteClient`]: `kiteconnect.KiteConnect`'s read-only REST routes over HTTP/1.1
/// (as `requests` speaks), built from [`crate::net::client_builder`].
pub struct KiteConnect {
    api_key: Secret,
    access_token: Mutex<Secret>,
    root: reqwest::Url,
    timeout: f64,
    http: GuardedClient,
}

impl fmt::Debug for KiteConnect {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("KiteConnect")
            .field("api_key", &self.api_key)
            .field("root", &self.root.as_str())
            .field("timeout", &self.timeout)
            .finish_non_exhaustive()
    }
}

impl KiteConnect {
    /// `KiteConnect(api_key=..., timeout=...)` against Kite's own root.
    ///
    /// # Errors
    /// As [`KiteConnect::with_root`].
    pub fn new(api_key: &str, timeout: Option<f64>) -> Result<Self, ProviderError> {
        Self::with_root(api_key, DEFAULT_ROOT_URI, timeout)
    }

    /// `KiteConnect(api_key=..., root=..., timeout=...)`. A missing or zero timeout is the
    /// SDK's 7 s (`timeout or self._default_timeout`); it bounds the connect and each read, as
    /// `requests`' single-number timeout does.
    ///
    /// # Errors
    /// `ValueError` for an unparseable root; `UpstreamUnavailable` when the TLS backend fails.
    pub fn with_root(
        api_key: &str,
        root: &str,
        timeout: Option<f64>,
    ) -> Result<Self, ProviderError> {
        let root = reqwest::Url::parse(root).map_err(|e| {
            ProviderError::value_error(format!("invalid Kite root {}: {e}", repr_str(root)))
        })?;
        let (http, timeout) = sdk_http_client(timeout)?;
        Ok(Self {
            api_key: Secret::new(api_key),
            access_token: Mutex::new(Secret::default()),
            root,
            timeout,
            http,
        })
    }

    /// `self.timeout`.
    #[must_use]
    pub const fn timeout(&self) -> f64 {
        self.timeout
    }

    /// `self.root`.
    #[must_use]
    pub fn root(&self) -> &str {
        self.root.as_str()
    }

    /// `profile()`: `GET /user/profile`.
    ///
    /// # Errors
    /// The SDK's exceptions ([`KiteError`]).
    pub async fn profile(&self) -> Result<KiteValue, KiteError> {
        self.get_json("/user/profile", &[]).await
    }

    /// `ohlc(*instruments)`: `GET /quote/ohlc`.
    ///
    /// # Errors
    /// The SDK's exceptions ([`KiteError`]).
    pub async fn ohlc(&self, instruments: &[String]) -> Result<KiteValue, KiteError> {
        self.get_json("/quote/ohlc", &instrument_params(instruments))
            .await
    }

    /// `ltp(*instruments)`: `GET /quote/ltp`.
    ///
    /// # Errors
    /// The SDK's exceptions ([`KiteError`]).
    pub async fn ltp(&self, instruments: &[String]) -> Result<KiteValue, KiteError> {
        self.get_json("/quote/ltp", &instrument_params(instruments))
            .await
    }

    fn access_token(&self) -> Secret {
        self.access_token
            .lock()
            .map(|t| t.clone())
            .unwrap_or_default()
    }

    async fn get_json(
        &self,
        path: &str,
        query: &[(String, String)],
    ) -> Result<KiteValue, KiteError> {
        match self
            .request(reqwest::Method::GET, path, query, None)
            .await?
        {
            Body::Json(v) => Ok(v),
            Body::Csv(bytes) => Ok(KiteValue::Bytes(bytes)),
        }
    }

    /// `_request`: build the URL (`urljoin(root, uri)` plus `requests`' query encoding), send
    /// the SDK's headers, and validate the response exactly as the SDK does.
    async fn request(
        &self,
        method: reqwest::Method,
        path: &str,
        query: &[(String, String)],
        json: Option<&KiteValue>,
    ) -> Result<Body, KiteError> {
        let joined = self.root.join(path).map_err(|e| {
            ProviderError::value_error(format!("invalid Kite path {}: {e}", repr_str(path)))
        })?;
        let mut url = joined.to_string();
        if !query.is_empty() {
            url.push(if joined.query().is_some() { '&' } else { '?' });
            url.push_str(&encode_query(query));
        }
        let mut request = sdk_headers(
            self.http.request(method, &url)?,
            &self.api_key,
            &self.access_token(),
        );
        if let Some(body) = json {
            request = request
                .header("Content-Type", "application/json")
                .body(py_json_dumps(body)?.into_bytes());
        }
        let response = request.send().await.map_err(requests_error)?;
        let status = response.status().as_u16();
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned());
        let content = response.bytes().await.map_err(requests_error)?;
        validate_response(status, content_type, &content)
    }
}

/// `params={"i": ins}`.
fn instrument_params(instruments: &[String]) -> Vec<(String, String)> {
    instruments
        .iter()
        .map(|i| ("i".to_owned(), i.clone()))
        .collect()
}

/// `urllib.parse.quote_plus(text)` (what `requests` encodes params with): unreserved bytes
/// pass, a space is `+`, everything else is `%XX`.
#[must_use]
pub fn quote_plus(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for &b in text.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(char::from(b));
            }
            b' ' => out.push('+'),
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

/// `urlencode(params, doseq=True)` over ordered pairs.
#[must_use]
pub fn encode_query(pairs: &[(String, String)]) -> String {
    pairs
        .iter()
        .map(|(k, v)| format!("{}={}", quote_plus(k), quote_plus(v)))
        .collect::<Vec<_>>()
        .join("&")
}

/// A `reqwest` failure as the `requests` exception class Python would see.
#[must_use]
pub fn requests_error(error: reqwest::Error) -> KiteError {
    let mut source: Option<&(dyn std::error::Error + 'static)> = Some(&error);
    while let Some(cause) = source {
        if let Some(blocked) = cause.downcast_ref::<crate::net::NetworkAccessBlocked>() {
            return KiteError::Other(ProviderError::foreign(
                "NetworkAccessBlocked",
                blocked.to_string(),
            ));
        }
        if let Some(provider) = cause.downcast_ref::<ProviderError>()
            && provider.python_type() == "NetworkAccessBlocked"
        {
            return KiteError::Other(ProviderError::foreign(
                "NetworkAccessBlocked",
                provider.to_string(),
            ));
        }
        source = cause.source();
    }
    let class = if error.is_timeout() {
        if error.is_connect() {
            "ConnectTimeout"
        } else {
            "ReadTimeout"
        }
    } else if error.is_connect() {
        "ConnectionError"
    } else if error.is_redirect() {
        "TooManyRedirects"
    } else if error.is_decode() {
        "ContentDecodingError"
    } else if error.is_body() {
        "ChunkedEncodingError"
    } else {
        "RequestException"
    };
    KiteError::Other(ProviderError::foreign(class, error.to_string()))
}

#[async_trait]
impl KiteClient for KiteConnect {
    fn set_access_token(&self, access_token: &Secret) {
        if let Ok(mut slot) = self.access_token.lock() {
            *slot = access_token.clone();
        }
    }

    async fn instruments(&self, exchange: Option<&str>) -> Result<KiteValue, KiteError> {
        let path = match exchange {
            Some(e) if !e.is_empty() => format!("/instruments/{e}"),
            _ => "/instruments".to_owned(),
        };
        match self.request(reqwest::Method::GET, &path, &[], None).await? {
            Body::Csv(bytes) => parse_instruments(&bytes).map_err(KiteError::Other),
            Body::Json(data) => Err(KiteError::Other(ProviderError::foreign(
                "TypeError",
                format!(
                    "initial_value must be str or None, not {}",
                    data.type_name()
                ),
            ))),
        }
    }

    async fn historical_data(
        &self,
        instrument_token: i64,
        from_date: HistoricalBound,
        to_date: HistoricalBound,
        interval: &str,
    ) -> Result<KiteValue, KiteError> {
        let path = format!("/instruments/historical/{instrument_token}/{interval}");
        let query = vec![
            ("from".to_owned(), from_date.as_param()),
            ("to".to_owned(), to_date.as_param()),
            ("interval".to_owned(), interval.to_owned()),
            ("continuous".to_owned(), "0".to_owned()),
            ("oi".to_owned(), "0".to_owned()),
        ];
        let data = self.get_json(&path, &query).await?;
        format_historical(&data).map_err(KiteError::Other)
    }

    async fn holdings(&self) -> Result<KiteValue, KiteError> {
        self.get_json("/portfolio/holdings", &[]).await
    }

    async fn margins(&self, segment: Option<&str>) -> Result<KiteValue, KiteError> {
        match segment {
            Some(s) if !s.is_empty() => self.get_json(&format!("/user/margins/{s}"), &[]).await,
            _ => self.get_json("/user/margins", &[]).await,
        }
    }

    async fn quote(&self, instruments: &[String]) -> Result<KiteValue, KiteError> {
        let data = self
            .get_json("/quote", &instrument_params(instruments))
            .await?;
        format_quote(&data)
    }

    async fn trades(&self) -> Result<KiteValue, KiteError> {
        let data = self.get_json("/trades", &[]).await?;
        format_response(&data).map_err(KiteError::Other)
    }

    fn has_basket_order_margins(&self) -> bool {
        true
    }

    async fn basket_order_margins(
        &self,
        params: &[KiteValue],
        consider_positions: bool,
        mode: Option<&str>,
    ) -> Result<KiteValue, KiteError> {
        let mut query = vec![(
            "consider_positions".to_owned(),
            if consider_positions { "True" } else { "False" }.to_owned(),
        )];
        if let Some(m) = mode {
            query.push(("mode".to_owned(), m.to_owned()));
        }
        let body = KiteValue::List(params.to_vec());
        match self
            .request(
                reqwest::Method::POST,
                "/margins/basket",
                &query,
                Some(&body),
            )
            .await?
        {
            Body::Json(v) => Ok(v),
            Body::Csv(bytes) => Ok(KiteValue::Bytes(bytes)),
        }
    }
}

/// `_parse_instruments`: the CSV dump as dicts, with the SDK's typed columns.
pub fn parse_instruments(bytes: &[u8]) -> Result<KiteValue, ProviderError> {
    let text = std::str::from_utf8(bytes).map_err(|e| {
        ProviderError::foreign(
            "UnicodeDecodeError",
            format!("'utf-8' codec can't decode bytes: {e}"),
        )
    })?;
    let text = text.trim();
    // `csv.reader` over `StringIO(d)` sees lines split at "\n" only, so a bare "\r" outside
    // quotes is "new-line character seen in unquoted field"; the csv crate would take it as a
    // line end.
    let mut in_quotes = false;
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '"' => in_quotes = !in_quotes,
            '\r' if !in_quotes && chars.peek().is_some_and(|n| *n != '\n') => {
                return Err(ProviderError::foreign(
                    "Error",
                    "new-line character seen in unquoted field - do you need to open the file \
                     with newline=''?",
                ));
            }
            _ => {}
        }
    }
    let mut reader = csv::ReaderBuilder::new()
        .flexible(true)
        .has_headers(true)
        .from_reader(text.as_bytes());
    let headers: Vec<String> = reader
        .headers()
        .map_err(|e| ProviderError::foreign("Error", e.to_string()))?
        .iter()
        .map(str::to_owned)
        .collect();
    // The csv crate strips a UTF-8 BOM; Python's `str.strip()` and `DictReader` keep it, so the
    // first column is "\u{feff}instrument_token" and the SDK's lookup is a KeyError.
    let mut headers = headers;
    if text.starts_with('\u{feff}')
        && let Some(first) = headers.first_mut()
        && !first.starts_with('\u{feff}')
    {
        first.insert(0, '\u{feff}');
    }
    let mut rows = Vec::new();
    for record in reader.records() {
        let record = record.map_err(|e| ProviderError::foreign("Error", e.to_string()))?;
        if record.is_empty() {
            continue;
        }
        let mut row = KiteMap::new();
        for (i, name) in headers.iter().enumerate() {
            let cell = record
                .get(i)
                .map_or(KiteValue::None, |c| KiteValue::Str(c.to_owned()));
            row.insert(name.clone(), cell);
        }
        for (column, kind) in [
            ("instrument_token", Cell::Int),
            ("last_price", Cell::Float),
            ("strike", Cell::Float),
            ("tick_size", Cell::Float),
            ("lot_size", Cell::Int),
        ] {
            let raw = row
                .get(column)
                .ok_or_else(|| ProviderError::foreign("KeyError", repr_str(column)))?
                .clone();
            let typed = match (kind, &raw) {
                (Cell::Int, KiteValue::Str(s)) => KiteValue::Int(py_int_from_str(s)?),
                (Cell::Float, KiteValue::Str(s)) => KiteValue::Float(py_float_from_str(s)?),
                (Cell::Int, other) => {
                    return Err(ProviderError::foreign(
                        "TypeError",
                        format!(
                            "int() argument must be a string, a bytes-like object or a real \
                             number, not '{}'",
                            other.type_name()
                        ),
                    ));
                }
                (Cell::Float, other) => {
                    return Err(ProviderError::foreign(
                        "TypeError",
                        format!(
                            "float() argument must be a string or a real number, not '{}'",
                            other.type_name()
                        ),
                    ));
                }
            };
            row.insert(column, typed);
        }
        let expiry = row
            .get("expiry")
            .ok_or_else(|| ProviderError::foreign("KeyError", "'expiry'"))?
            .clone();
        match &expiry {
            KiteValue::Str(s) if s.chars().count() == 10 => {
                let parsed = dateutil_parse(s)?;
                row.insert("expiry", KiteValue::Date(naive_of(&parsed).date()));
            }
            KiteValue::Str(_) => {}
            other => {
                return Err(ProviderError::foreign(
                    "TypeError",
                    format!("object of type '{}' has no len()", other.type_name()),
                ));
            }
        }
        rows.push(KiteValue::Map(row));
    }
    Ok(KiteValue::List(rows))
}

#[derive(Clone, Copy)]
enum Cell {
    Int,
    Float,
}

/// `_format_historical`: `[[stamp, o, h, l, c, v(, oi)], ...]` as dicts.
pub fn format_historical(data: &KiteValue) -> Result<KiteValue, ProviderError> {
    let KiteValue::Map(map) = data else {
        return Err(ProviderError::foreign(
            "TypeError",
            format!(
                "{} indices must be integers or slices, not str",
                data.type_name()
            ),
        ));
    };
    let candles = map
        .get("candles")
        .ok_or_else(|| ProviderError::foreign("KeyError", "'candles'"))?;
    let mut records = Vec::new();
    for d in candles.py_iter()? {
        let KiteValue::List(cells) = &d else {
            return Err(ProviderError::foreign(
                "TypeError",
                format!("'{}' object is not subscriptable", d.type_name()),
            ));
        };
        let at = |i: usize| {
            cells
                .get(i)
                .cloned()
                .ok_or_else(|| ProviderError::foreign("IndexError", "list index out of range"))
        };
        let stamp = match at(0)? {
            KiteValue::Str(s) => KiteValue::DateTime(dateutil_parse(&s)?),
            other => {
                return Err(ProviderError::foreign(
                    "TypeError",
                    format!(
                        "Parser must be a string or character stream, not {}",
                        other.type_name()
                    ),
                ));
            }
        };
        let mut record = KiteMap::new();
        record.insert("date", stamp);
        record.insert("open", at(1)?);
        record.insert("high", at(2)?);
        record.insert("low", at(3)?);
        record.insert("close", at(4)?);
        record.insert("volume", at(5)?);
        if cells.len() == 7 {
            record.insert("oi", at(6)?);
        }
        records.push(KiteValue::Map(record));
    }
    Ok(KiteValue::List(records))
}

/// `_format_response`: the SDK's 19-character timestamp fields become datetimes.
pub fn format_response(data: &KiteValue) -> Result<KiteValue, ProviderError> {
    const FIELDS: [&str; 7] = [
        "order_timestamp",
        "exchange_timestamp",
        "created",
        "last_instalment",
        "fill_timestamp",
        "timestamp",
        "last_trade_time",
    ];
    let fix = |item: &KiteValue| -> Result<KiteValue, ProviderError> {
        let KiteValue::Map(map) = item else {
            return Err(no_attribute(item, "get"));
        };
        let mut out = map.clone();
        for field in FIELDS {
            let value = map.get(field).unwrap_or(&NONE);
            if !value.truthy() {
                continue;
            }
            let length = match value {
                KiteValue::Str(s) => s.chars().count(),
                KiteValue::List(items) => items.len(),
                KiteValue::Map(m) => m.len(),
                KiteValue::Bytes(b) => b.len(),
                other => {
                    return Err(ProviderError::foreign(
                        "TypeError",
                        format!("object of type '{}' has no len()", other.type_name()),
                    ));
                }
            };
            if length != 19 {
                continue;
            }
            let KiteValue::Str(text) = value else {
                return Err(ProviderError::foreign(
                    "TypeError",
                    format!(
                        "Parser must be a string or character stream, not {}",
                        value.type_name()
                    ),
                ));
            };
            out.insert(field, KiteValue::DateTime(dateutil_parse(text)?));
        }
        Ok(KiteValue::Map(out))
    };
    match data {
        KiteValue::List(items) => Ok(KiteValue::List(
            items.iter().map(fix).collect::<Result<_, _>>()?,
        )),
        KiteValue::Map(_) => fix(data),
        _ => Err(ProviderError::foreign(
            "UnboundLocalError",
            "cannot access local variable '_list' where it is not associated with a value",
        )),
    }
}

/// `dateutil.parser.parse` for the forms Kite sends: ISO dates and datetimes with a `T` or a
/// space, optional fraction, and an optional `Z`, `±HH:MM` or `±HHMM` offset.
/// `dateutil.parser.parse(text)` as the SDK's timestamps need it.
///
/// # Errors
/// `ParserError` for an unparseable string.
pub fn dateutil_parse(text: &str) -> Result<PyDateTime, ProviderError> {
    parse_iso(text).ok_or_else(|| {
        ProviderError::foreign("ParserError", format!("Unknown string format: {text}"))
    })
}

fn parse_iso(text: &str) -> Option<PyDateTime> {
    if let Ok(parsed) = PyDateTime::parse(text) {
        return Some(parsed);
    }
    let normalised = text.trim().replacen(' ', "T", 1);
    for fmt in ["%Y-%m-%dT%H:%M:%S%.f%z", "%Y-%m-%dT%H:%M%z"] {
        if let Ok(dt) = DateTime::parse_from_str(&normalised, fmt) {
            return Some(PyDateTime::Aware(dt));
        }
    }
    None
}

/// `datetime.datetime.fromisoformat(text)` as CPython 3.12's C implementation
/// (`_datetimemodule.c`) parses it — what `_date` and `_aware_ist` call. Faithful to its quirks:
/// no whitespace stripping, `YYYYMMDD` and ISO week dates, any one character as the date/time
/// separator, `HH`, `HHMM`, `HH:MM[:SS[.f+]]` (a fraction truncated to microseconds), and one
/// ignored character between the time and a `Z`/`±` offset. An offset with a sub-second part
/// cannot be held by a `FixedOffset` and is refused (DECISIONS-RS R4.10).
fn py_fromisoformat(text: &str) -> Result<PyDateTime, ProviderError> {
    let invalid =
        || ProviderError::value_error(format!("Invalid isoformat string: {}", repr_str(text)));
    if text.chars().count() < 7 {
        return Err(invalid());
    }
    // A NUL sentinel, as the C parser reads its UTF-8 buffer up to the terminator.
    let mut buf: Vec<u8> = text.as_bytes().to_vec();
    buf.push(0);
    let len = text.len();
    let at = |i: usize| buf.get(i).copied().unwrap_or(0);
    let separator = iso_separator(&buf, len).ok_or_else(invalid)?;
    let (year, month, day) = iso_date(&buf, separator).ok_or_else(invalid)?;
    let mut time = IsoTime::default();
    if len > separator {
        // Skip the separator, a whole UTF-8 character.
        let lead = at(separator);
        let width = match lead {
            b if b & 0x80 == 0 => 1,
            b if b & 0xE0 == 0xC0 => 2,
            b if b & 0xF0 == 0xE0 => 3,
            _ => 4,
        };
        let start = separator + width;
        time = iso_time(&buf, start, len).ok_or_else(invalid)?;
    }
    let date = NaiveDate::from_ymd_opt(year, 1, 1)
        .filter(|_| (1..=9999).contains(&year))
        .ok_or_else(|| ProviderError::value_error(format!("year {year} is out of range")))?;
    if !(1..=12).contains(&month) {
        return Err(ProviderError::value_error("month must be in 1..12"));
    }
    let date = date
        .with_month(month)
        .and_then(|d| d.with_day(day))
        .ok_or_else(|| ProviderError::value_error("day is out of range for month"))?;
    for (value, name, top) in [
        (time.hour, "hour", 23),
        (time.minute, "minute", 59),
        (time.second, "second", 59),
    ] {
        if value > top {
            return Err(ProviderError::value_error(format!(
                "{name} must be in 0..{top}"
            )));
        }
    }
    let naive = date
        .and_hms_micro_opt(time.hour, time.minute, time.second, time.micro)
        .ok_or_else(invalid)?;
    let Some((offset, offset_micro)) = time.offset else {
        return Ok(PyDateTime::Naive(naive));
    };
    if offset_micro != 0 {
        return Err(ProviderError::value_error(format!(
            "{}: a sub-second UTC offset is not representable (DECISIONS-RS R4.10)",
            repr_str(text)
        )));
    }
    let zone = FixedOffset::east_opt(offset).ok_or_else(|| {
        ProviderError::value_error(
            "offset must be a timedelta strictly between -timedelta(hours=24) and \
             timedelta(hours=24)",
        )
    })?;
    naive
        .and_local_timezone(zone)
        .single()
        .map(PyDateTime::Aware)
        .ok_or_else(invalid)
}

/// The parsed time half of an ISO string: wall clock plus an optional offset in seconds (and
/// its microseconds).
#[derive(Default)]
struct IsoTime {
    hour: u32,
    minute: u32,
    second: u32,
    micro: u32,
    offset: Option<(i32, i32)>,
}

/// `parse_digits`: exactly `n` ASCII digits at `pos`.
fn iso_digits(buf: &[u8], pos: usize, n: usize) -> Option<(u32, usize)> {
    let mut value: u32 = 0;
    for i in 0..n {
        let c = *buf.get(pos + i)?;
        if !c.is_ascii_digit() {
            return None;
        }
        value = value * 10 + u32::from(c - b'0');
    }
    Some((value, pos + n))
}

/// `_find_isoformat_datetime_separator`.
fn iso_separator(buf: &[u8], len: usize) -> Option<usize> {
    let at = |i: usize| buf.get(i).copied().unwrap_or(0);
    if len == 7 {
        return Some(7);
    }
    if at(4) == b'-' {
        if at(5) != b'W' {
            return Some(10);
        }
        if len < 8 {
            return None;
        }
        if len > 8 && at(8) == b'-' {
            if len == 9 {
                return None;
            }
            return Some(if len > 10 && at(10).is_ascii_digit() {
                8
            } else {
                10
            });
        }
        return Some(8);
    }
    if at(4) == b'W' {
        let mut idx = 7;
        while idx < len && at(idx).is_ascii_digit() {
            idx += 1;
        }
        if idx < 9 {
            return Some(idx);
        }
        return Some(if idx % 2 == 0 { 7 } else { 8 });
    }
    Some(8)
}

/// `parse_isoformat_date` (calendar or ISO-week form), read from the start of the buffer.
fn iso_date(buf: &[u8], len: usize) -> Option<(i32, u32, u32)> {
    let at = |i: usize| buf.get(i).copied().unwrap_or(0);
    let (year, mut p) = iso_digits(buf, 0, 4)?;
    let uses_separator = at(p) == b'-';
    if uses_separator {
        p += 1;
    }
    let year = i32::try_from(year).ok()?;
    if at(p) == b'W' {
        // `iso_to_ymd` refuses a year outside MINYEAR..=MAXYEAR before reading the week.
        if !(1..=9999).contains(&year) {
            return None;
        }
        let (week, after) = iso_digits(buf, p + 1, 2)?;
        p = after;
        let mut weekday = 1;
        if p < len {
            if uses_separator {
                if at(p) != b'-' {
                    return None;
                }
                p += 1;
            }
            weekday = iso_digits(buf, p, 1)?.0;
        }
        let weekday = match weekday {
            1 => chrono::Weekday::Mon,
            2 => chrono::Weekday::Tue,
            3 => chrono::Weekday::Wed,
            4 => chrono::Weekday::Thu,
            5 => chrono::Weekday::Fri,
            6 => chrono::Weekday::Sat,
            7 => chrono::Weekday::Sun,
            _ => return None,
        };
        let date = NaiveDate::from_isoywd_opt(year, week, weekday)?;
        return Some((date.year(), date.month(), date.day()));
    }
    let (month, mut p) = iso_digits(buf, p, 2)?;
    if uses_separator {
        if at(p) != b'-' {
            return None;
        }
        p += 1;
    }
    let (day, _) = iso_digits(buf, p, 2)?;
    Some((year, month, day))
}

/// `parse_hh_mm_ss_ff` over `[start, end)`: `Some((h, m, s, us, trailing))`, where `trailing`
/// is the C function's "not the end of the string" return of 1.
fn iso_hh_mm_ss_ff(buf: &[u8], start: usize, end: usize) -> Option<([u32; 4], bool)> {
    let at = |i: usize| buf.get(i).copied().unwrap_or(0);
    let mut vals = [0_u32; 4];
    let mut p = start;
    let mut has_separator = true;
    let mut fraction = false;
    for (i, slot) in vals.iter_mut().take(3).enumerate() {
        let (v, after) = iso_digits(buf, p, 2)?;
        *slot = v;
        let c = at(after);
        p = after + 1;
        if i == 0 {
            has_separator = c == b':';
        }
        if p >= end {
            return Some((vals, c != 0));
        } else if has_separator && c == b':' {
            continue;
        } else if c == b'.' || c == b',' {
            fraction = true;
            break;
        } else if !has_separator {
            p -= 1;
        } else {
            return None;
        }
    }
    // After three components the C loop falls through to the fraction parse unconditionally.
    let _ = fraction;
    let remains = end.checked_sub(p)?;
    let to_parse = remains.min(6);
    let (mut micro, mut q) = iso_digits(buf, p, to_parse)?;
    if to_parse == 0 {
        return None;
    }
    if to_parse < 6 {
        micro *= 10_u32.pow(u32::try_from(6 - to_parse).ok()?);
    }
    vals[3] = micro;
    while at(q).is_ascii_digit() {
        q += 1;
    }
    Some((vals, at(q) != 0))
}

/// `parse_isoformat_time` over `[start, len)`.
fn iso_time(buf: &[u8], start: usize, len: usize) -> Option<IsoTime> {
    let at = |i: usize| buf.get(i).copied().unwrap_or(0);
    let mut tz = start;
    loop {
        if matches!(at(tz), b'Z' | b'+' | b'-') {
            break;
        }
        tz += 1;
        if tz >= len {
            break;
        }
    }
    let (vals, trailing) = iso_hh_mm_ss_ff(buf, start, tz)?;
    let mut time = IsoTime {
        hour: vals[0],
        minute: vals[1],
        second: vals[2],
        micro: vals[3],
        offset: None,
    };
    if tz >= len {
        return (!trailing).then_some(time);
    }
    if at(tz) == b'Z' {
        return (at(tz + 1) == 0).then(|| {
            time.offset = Some((0, 0));
            time
        });
    }
    let sign: i32 = if at(tz) == b'-' { -1 } else { 1 };
    let (tz_vals, tz_trailing) = iso_hh_mm_ss_ff(buf, tz + 1, len)?;
    if tz_trailing {
        return None;
    }
    let seconds = i32::try_from(tz_vals[0] * 3600 + tz_vals[1] * 60 + tz_vals[2]).ok()?;
    let micro = i32::try_from(tz_vals[3]).ok()?;
    time.offset = Some((sign * seconds, sign * micro));
    Some(time)
}

fn naive_of(dt: &PyDateTime) -> NaiveDateTime {
    match dt {
        PyDateTime::Naive(n) => *n,
        PyDateTime::Aware(a) => a.naive_local(),
    }
}

// =============================================================================================
// TradeFill — baskfy_core.tradebook, privately (DECISIONS-RS R2.3 / R4.10)
// =============================================================================================

/// `baskfy_core.tradebook.TradeSide`. Lives here until `tradebook` is ported to the core.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TradeSide {
    /// `BUY`.
    Buy,
    /// `SELL`.
    Sell,
}

impl TradeSide {
    /// The `StrEnum` value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Buy => "BUY",
            Self::Sell => "SELL",
        }
    }
}

/// `baskfy_core.tradebook.TradeFill`: one execution, in the broker's own words. Quantities are
/// shares; prices are rupees. Lives here until `tradebook` is ported to the core.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TradeFill {
    /// Symbol, upper-cased.
    pub symbol: String,
    /// `NSE` or `BSE`.
    pub exchange: String,
    /// Buy or sell.
    pub side: TradeSide,
    /// Shares.
    pub quantity: Decimal,
    /// Rupees.
    pub price: Decimal,
    /// The fill's calendar date.
    pub trade_date: NaiveDate,
    /// Kite's trade id.
    pub trade_id: String,
    /// Kite's order id.
    pub order_id: Option<String>,
    /// ISIN (Kite's trades carry none).
    pub isin: Option<String>,
    /// Segment (Kite's trades carry none).
    pub segment: Option<String>,
    /// The fill's timestamp, as Kite sent it (naive IST).
    pub executed_at: Option<PyDateTime>,
}

// =============================================================================================
// KiteRuntime / KiteProvider
// =============================================================================================

/// Builds the client for an API key (`client_factory`).
pub type ClientFactory =
    Arc<dyn Fn(&str) -> Result<Arc<dyn KiteClient>, ProviderError> + Send + Sync>;

/// `_default_client_factory`: `KiteConnect(api_key=..., timeout=...)` — AF 3.11, the timeout is
/// passed explicitly because the SDK's 7 s default is not a product decision.
///
/// # Errors
/// As [`KiteConnect::new`].
pub fn default_client_factory(api_key: &str, timeout: f64) -> Result<KiteConnect, ProviderError> {
    KiteConnect::new(api_key, Some(timeout))
}

/// `KiteRuntime`: the collaborators [`KiteProvider`] is wired with. `rate_limiter` is
/// deliberately not defaulted: an unthrottled Kite client is a way to lose the API key.
#[derive(Default, Clone)]
pub struct KiteRuntime {
    /// The shared limiter every call waits on; `None` refuses every call.
    pub rate_limiter: Option<Arc<dyn RateLimiter>>,
    /// Builds the client; `None` is [`default_client_factory`] at the settings' timeout.
    pub client_factory: Option<ClientFactory>,
    /// The token store; `None` is one at the settings' path and key.
    pub token_store: Option<Arc<AccessTokenStore>>,
    /// Retry policy; `None` is the settings'.
    pub retry_policy: Option<RetryPolicy>,
    /// Retry seams.
    pub retry_hooks: Option<RetryHooks>,
    /// The `broker_account.id` the configured access token belongs to, when known: a holdings
    /// read for any other account is refused instead of answered with the wrong positions.
    pub holdings_account_id: Option<i64>,
}

impl fmt::Debug for KiteRuntime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("KiteRuntime")
            .field("rate_limiter", &self.rate_limiter.is_some())
            .field("client_factory", &self.client_factory.is_some())
            .field("token_store", &self.token_store)
            .field("retry_policy", &self.retry_policy)
            .field("holdings_account_id", &self.holdings_account_id)
            .finish_non_exhaustive()
    }
}

/// One call on the client, so retries can re-issue it.
#[derive(Clone, Copy)]
enum Call<'a> {
    Instruments(&'static str),
    Historical {
        token: i64,
        from: HistoricalBound,
        to: HistoricalBound,
        interval: &'static str,
    },
    Holdings,
    Margins(Option<&'static str>),
    Quote(&'a [String]),
    Trades,
    Basket(&'a [KiteValue], bool),
}

/// A `minute_bars` bound: a `date` (the whole day) or a `datetime` (read in IST).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MinuteBound {
    /// A whole day.
    Date(NaiveDate),
    /// A moment; a naive one is IST wall-clock.
    DateTime(PyDateTime),
}

impl From<NaiveDate> for MinuteBound {
    fn from(value: NaiveDate) -> Self {
        Self::Date(value)
    }
}

impl From<NaiveDateTime> for MinuteBound {
    fn from(value: NaiveDateTime) -> Self {
        Self::DateTime(PyDateTime::Naive(value))
    }
}

impl From<DateTime<FixedOffset>> for MinuteBound {
    fn from(value: DateTime<FixedOffset>) -> Self {
        Self::DateTime(PyDateTime::Aware(value))
    }
}

/// `KiteProvider`: `BarsProvider` and the read-only broker ledger, backed by Kite Connect.
pub struct KiteProvider {
    settings: ProviderSettings,
    client_factory: ClientFactory,
    token_store: Arc<AccessTokenStore>,
    rate_limiter: Option<Arc<dyn RateLimiter>>,
    retry_policy: RetryPolicy,
    retry_hooks: Option<RetryHooks>,
    holdings_account_id: Option<i64>,
    client: Mutex<Option<Arc<dyn KiteClient>>>,
}

impl fmt::Debug for KiteProvider {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("KiteProvider")
            .field("token_store", &self.token_store)
            .field("rate_limiter", &self.rate_limiter.is_some())
            .field("retry_policy", &self.retry_policy)
            .field("holdings_account_id", &self.holdings_account_id)
            .finish_non_exhaustive()
    }
}

fn validation(error: &RecordError) -> ProviderError {
    ProviderError::foreign("ValidationError", error.to_string())
}

fn naive_str(dt: &NaiveDateTime) -> String {
    py_datetime_str(&PyDateTime::Naive(*dt))
}

impl KiteProvider {
    /// `KiteProvider(settings, runtime)`.
    ///
    /// # Errors
    /// `ValueError` when the settings' retry fields do not make a valid `RetryPolicy`.
    pub fn new(settings: ProviderSettings, runtime: KiteRuntime) -> Result<Self, ProviderError> {
        let client_factory = runtime.client_factory.unwrap_or_else(|| {
            let timeout = settings.kite_request_timeout_seconds;
            let factory: ClientFactory = Arc::new(move |api_key: &str| {
                let client: Arc<dyn KiteClient> =
                    Arc::new(default_client_factory(api_key, timeout)?);
                Ok(client)
            });
            factory
        });
        let token_store = runtime.token_store.unwrap_or_else(|| {
            Arc::new(AccessTokenStore::new(
                settings.kite_token_path.clone(),
                settings.kite_token_encryption_key.clone(),
            ))
        });
        let retry_policy = match runtime.retry_policy {
            Some(policy) => policy,
            None => RetryPolicy::new(
                u32::try_from(settings.provider_max_attempts).map_err(|_| {
                    ProviderError::value_error(format!(
                        "max_attempts must be at least 1; got {}",
                        settings.provider_max_attempts
                    ))
                })?,
                settings.provider_backoff_base_seconds,
                settings.provider_backoff_max_seconds,
            )?,
        };
        Ok(Self {
            settings,
            client_factory,
            token_store,
            rate_limiter: runtime.rate_limiter,
            retry_policy,
            retry_hooks: runtime.retry_hooks,
            holdings_account_id: runtime.holdings_account_id,
            client: Mutex::new(None),
        })
    }

    /// `rate_limiter`: which clock this adapter's reads wait on, or `None` when none was
    /// configured. Read-only — nothing can swap a limiter after construction.
    #[must_use]
    pub const fn rate_limiter(&self) -> Option<&Arc<dyn RateLimiter>> {
        self.rate_limiter.as_ref()
    }

    /// The token store this adapter reads.
    #[must_use]
    pub fn token_store(&self) -> &AccessTokenStore {
        &self.token_store
    }

    // --- BarsProvider ---------------------------------------------------------------------

    /// `list_instruments`: the Kite instrument dump, narrowed to NSE and mapped onto
    /// [`InstrumentRecord`].
    ///
    /// # Errors
    /// As the call; a row that cannot be read raises as Python's does.
    pub async fn list_instruments(&self) -> Result<Vec<InstrumentRecord>, ProviderError> {
        let raw = self.call(Call::Instruments("NSE")).await?;
        let mut records = Vec::new();
        for row in raw.py_iter()? {
            if let Some(record) = to_instrument_record(&row)? {
                records.push(record);
            }
        }
        Ok(records)
    }

    /// `index_level`: today's live level for a benchmark index, or `None` when it cannot be
    /// quoted — an unmapped slug, a missing or malformed quote. Without a live level the gate
    /// reads published closes, which is never wrong, only late.
    ///
    /// # Errors
    /// As the call.
    pub async fn index_level(&self, slug: &str) -> Result<Option<Decimal>, ProviderError> {
        let Some((_, symbol)) = INDEX_QUOTE_SYMBOL.iter().find(|(s, _)| *s == slug) else {
            return Ok(None);
        };
        let keys = [format!("NSE:{symbol}")];
        let payload = self.call(Call::Quote(&keys)).await?;
        let KiteValue::Map(map) = &payload else {
            return Ok(None);
        };
        let Some(KiteValue::Map(row)) = map.get(&keys[0]) else {
            return Ok(None);
        };
        let last = row.get("last_price").unwrap_or(&NONE);
        if matches!(last, KiteValue::None) {
            return Ok(None);
        }
        let Ok(level) = py_decimal_from_str(&last.py_str()) else {
            return Ok(None);
        };
        Ok((level > Decimal::ZERO).then_some(level))
    }

    /// `fno_underlyings`: every equity that has a futures contract — the distinct `name`s of
    /// the NFO dump's `FUT` rows, upper-cased and sorted (house rule 7). One HTTP call.
    ///
    /// # Errors
    /// As the call.
    pub async fn fno_underlyings(&self) -> Result<Vec<String>, ProviderError> {
        let raw = self.call(Call::Instruments("NFO")).await?;
        let mut underlyings = std::collections::BTreeSet::new();
        for row in raw.py_iter()? {
            if text(row.get("instrument_type")?)
                .unwrap_or_default()
                .to_uppercase()
                != "FUT"
            {
                continue;
            }
            if let Some(name) = text(row.get("name")?) {
                underlyings.insert(name.to_uppercase());
            }
        }
        Ok(underlyings.into_iter().collect())
    }

    /// `option_contracts`: every listed CE/PE contract on `underlying` from the NFO dump,
    /// sorted by token. A row missing a sizing fact is skipped, never guessed.
    ///
    /// # Errors
    /// As the call.
    pub async fn option_contracts(
        &self,
        underlying: &str,
    ) -> Result<Vec<OptionContractRecord>, ProviderError> {
        let wanted = underlying.to_uppercase().trim().to_owned();
        let raw = self.call(Call::Instruments("NFO")).await?;
        let mut out = Vec::new();
        for row in raw.py_iter()? {
            if let Some(record) = to_option_contract(&row, &wanted)? {
                out.push(record);
            }
        }
        out.sort_by_key(|r| r.instrument_token);
        Ok(out)
    }

    /// `fno_option_contracts`: every CE/PE contract on **every** F&O underlying from one NFO
    /// dump (FO2), each row's underlying its own `name`, sorted by token.
    ///
    /// # Errors
    /// As the call.
    pub async fn fno_option_contracts(&self) -> Result<Vec<OptionContractRecord>, ProviderError> {
        let raw = self.call(Call::Instruments("NFO")).await?;
        let mut out = Vec::new();
        for row in raw.py_iter()? {
            let name = text(row.get("name")?).unwrap_or_default().to_uppercase();
            if name.is_empty() {
                continue;
            }
            if let Some(record) = to_option_contract(&row, &name)? {
                out.push(record);
            }
        }
        out.sort_by_key(|r| r.instrument_token);
        Ok(out)
    }

    /// `daily_bars`: raw daily candles for one instrument, chunked to respect the day-interval
    /// cap, conforming to [`daily_bars_schema`]. No history is an empty frame, not an error.
    ///
    /// # Errors
    /// `ValueError` when `end` precedes `start`; otherwise as the calls.
    pub async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        if end < start {
            return Err(ProviderError::value_error(format!(
                "end {end} precedes start {start}"
            )));
        }
        let symbol = token.to_string();
        let mut rows: Vec<DailyRow> = Vec::new();
        for (chunk_start, chunk_end) in self.chunk_windows(start, end)? {
            let candles = self
                .call(Call::Historical {
                    token,
                    from: HistoricalBound::Date(chunk_start),
                    to: HistoricalBound::Date(chunk_end),
                    interval: "day",
                })
                .await?;
            let chunk = candles_to_rows(&candles)?;
            // Python conforms each chunk's frame as it is fetched (a list comprehension), so an
            // unrepresentable price fails that chunk — its own inferred scale and counts in the
            // message — before the next request is made.
            for (name, pick) in PRICE_COLUMNS {
                drop(price_column(
                    name,
                    &chunk.iter().map(pick).collect::<Vec<_>>(),
                )?);
            }
            rows.extend(chunk);
        }
        if rows.is_empty() {
            return Ok(empty_frame(&daily_bars_schema()));
        }
        // Chunk boundaries are inclusive on both ends: dedupe on date, first wins, then sort.
        let mut by_date: BTreeMap<NaiveDate, DailyRow> = BTreeMap::new();
        for row in rows {
            by_date.entry(row.date).or_insert(row);
        }
        daily_frame(&symbol, by_date.into_values().collect())
    }

    /// `chunk_windows`: `[start, end]` in slices no longer than the day-interval cap,
    /// contiguous and covering the whole window. Public because the resumable backfill drives
    /// its cursor by the same boundaries.
    ///
    /// # Errors
    /// `OverflowError` past chrono's last date.
    pub fn chunk_windows(
        &self,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<Vec<(NaiveDate, NaiveDate)>, ProviderError> {
        windows(start, end, self.settings.kite_max_days_per_request)
    }

    // --- HoldingsProvider -----------------------------------------------------------------

    /// `broker_holdings`: `GET /portfolio/holdings` mapped onto [`BrokerHoldingRecord`]. A row
    /// that cannot be read is skipped (one malformed row must not cost the user the sync of
    /// everything else); everything else is strict.
    ///
    /// # Errors
    /// The account refusals, then as the call.
    pub async fn broker_holdings(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Vec<BrokerHoldingRecord>, ProviderError> {
        self.require_own_account(account)?;
        let raw = self.call(Call::Holdings).await?;
        let mut records = Vec::new();
        for row in raw.py_iter()? {
            if let Some(record) = to_holding_record(&row)? {
                records.push(record);
            }
        }
        Ok(records)
    }

    /// `broker_cash`: the equity segment's net balance; `None` (never zero) when Kite reports
    /// none.
    ///
    /// # Errors
    /// The account refusals, then as the call.
    pub async fn broker_cash(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Option<Decimal>, ProviderError> {
        self.require_own_account(account)?;
        let payload = self.call(Call::Margins(Some(EQUITY_SEGMENT))).await?;
        equity_net_cash(&payload)
    }

    /// `broker_trades`: `GET /trades`, today's executions. Strict: an equity row that cannot be
    /// read refuses the whole read; derivative and currency rows are left out.
    ///
    /// # Errors
    /// The account refusals, then as the call; `UnexpectedPayload` for an unreadable trade.
    pub async fn broker_trades(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Vec<TradeFill>, ProviderError> {
        self.require_own_account(account)?;
        let raw = self.call(Call::Trades).await?;
        let mut fills = Vec::new();
        for row in raw.py_iter()? {
            if let Some(fill) = to_trade_fill(&row)? {
                fills.push(fill);
            }
        }
        Ok(fills)
    }

    // --- Quotes (SW6) ---------------------------------------------------------------------

    /// `quotes(symbols, exchange=...)`: `GET /quote` in batches of at most
    /// [`QUOTE_BATCH_SIZE`], one throttled call each. A symbol Kite does not answer for is
    /// absent; a row without a price is skipped.
    ///
    /// # Errors
    /// As the calls; a row that cannot be read at all raises as Python's does.
    pub async fn quotes(
        &self,
        symbols: &[&str],
        exchange: &str,
    ) -> Result<Vec<QuoteRecord>, ProviderError> {
        let wanted: Vec<&str> = symbols.iter().copied().filter(|s| !s.is_empty()).collect();
        let mut records = Vec::new();
        for batch in wanted.chunks(QUOTE_BATCH_SIZE) {
            let keys: Vec<String> = batch.iter().map(|s| format!("{exchange}:{s}")).collect();
            let payload = self.call(Call::Quote(&keys)).await?;
            let KiteValue::Map(map) = &payload else {
                return Err(no_attribute(&payload, "items"));
            };
            for (key, row) in map.iter() {
                if let Some(record) = to_quote_record(key, row)? {
                    records.push(record);
                }
            }
        }
        Ok(records)
    }

    // --- Options reads (OP3) --------------------------------------------------------------

    /// `option_quotes`: `GET /quote` for whole `EXCHANGE:SYMBOL` keys, with depth, OI and
    /// timestamps; blanks dropped and duplicates sent once, in batches of
    /// [`QUOTE_BATCH_SIZE`]. An unreadable row is skipped.
    ///
    /// # Errors
    /// As the calls.
    pub async fn option_quotes(
        &self,
        keys: &[&str],
    ) -> Result<Vec<OptionQuoteRecord>, ProviderError> {
        let mut seen = std::collections::HashSet::new();
        let mut wanted: Vec<String> = Vec::new();
        for key in keys {
            if !key.is_empty() && seen.insert(*key) {
                wanted.push((*key).to_owned());
            }
        }
        let mut records = Vec::new();
        for batch in wanted.chunks(QUOTE_BATCH_SIZE) {
            let payload = self.call(Call::Quote(batch)).await?;
            let KiteValue::Map(map) = &payload else {
                return Err(no_attribute(&payload, "items"));
            };
            for (key, row) in map.iter() {
                if let Some(record) = to_option_quote(key, row)? {
                    records.push(record);
                }
            }
        }
        Ok(records)
    }

    /// `minute_windows`: `[start, end]` in slices of at most
    /// [`MINUTE_MAX_DAYS_PER_REQUEST`] days.
    ///
    /// # Errors
    /// `ValueError` when `end` precedes `start`.
    pub fn minute_windows(
        &self,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<Vec<(NaiveDate, NaiveDate)>, ProviderError> {
        if end < start {
            return Err(ProviderError::value_error(format!(
                "end {end} precedes start {start}"
            )));
        }
        windows(start, end, MINUTE_MAX_DAYS_PER_REQUEST)
    }

    /// `minute_bars`: one-minute candles for `token` over `[start, end]`, one throttled call
    /// per 60-day window; de-duplicated on the timestamp and in time order. An empty window is
    /// an empty list.
    ///
    /// # Errors
    /// `ValueError` for an inverted window; otherwise as the calls.
    pub async fn minute_bars(
        &self,
        token: i64,
        start: impl Into<MinuteBound> + Send,
        end: impl Into<MinuteBound> + Send,
    ) -> Result<Vec<MinuteBarRecord>, ProviderError> {
        let lo = ist_wall(start.into(), false);
        let hi = ist_wall(end.into(), true);
        if hi < lo {
            return Err(ProviderError::value_error(format!(
                "end {} precedes start {}",
                naive_str(&hi),
                naive_str(&lo)
            )));
        }
        let midnight = NaiveTime::MIN;
        let last_second = NaiveTime::from_hms_opt(23, 59, 59).unwrap_or(NaiveTime::MIN);
        let mut by_ts: BTreeMap<DateTime<chrono::Utc>, MinuteBarRecord> = BTreeMap::new();
        for (day_lo, day_hi) in self.minute_windows(lo.date(), hi.date())? {
            let chunk_lo = lo.max(day_lo.and_time(midnight));
            let chunk_hi = hi.min(day_hi.and_time(last_second));
            let candles = self
                .call(Call::Historical {
                    token,
                    from: HistoricalBound::DateTime(chunk_lo),
                    to: HistoricalBound::DateTime(chunk_hi),
                    interval: "minute",
                })
                .await?;
            for candle in candles.py_iter()? {
                if let Some(bar) = to_minute_bar(&candle)?
                    && let Some(at) = bar.ts.aware()
                {
                    by_ts.entry(at.to_utc()).or_insert(bar);
                }
            }
        }
        Ok(by_ts.into_values().collect())
    }

    /// `basket_order_margins`: Kite's `/margins/basket` for `legs` — **a calculation, never an
    /// order**. `consider_positions` is `false` unless asked, so the answer is the basket's
    /// own requirement.
    ///
    /// # Errors
    /// `ValueError` for no legs; `ProviderUnavailable` for a client without the verb; as the
    /// call.
    pub async fn basket_order_margins(
        &self,
        legs: &[MarginLegRecord],
        consider_positions: bool,
    ) -> Result<BasketMarginRecord, ProviderError> {
        if legs.is_empty() {
            return Err(ProviderError::value_error(
                "a basket margin needs at least one leg",
            ));
        }
        let mut params = Vec::with_capacity(legs.len());
        for leg in legs {
            let price = leg.price.to_string().parse::<f64>().map_err(|e| {
                ProviderError::value_error(format!("cannot read {} as a float: {e}", leg.price))
            })?;
            let fields: [(&str, KiteValue); 8] = [
                ("exchange", leg.exchange.as_str().into()),
                ("tradingsymbol", leg.tradingsymbol.as_str().into()),
                (
                    "transaction_type",
                    match leg.transaction_type {
                        crate::records::TransactionType::Buy => "BUY",
                        crate::records::TransactionType::Sell => "SELL",
                    }
                    .into(),
                ),
                ("variety", "regular".into()),
                ("product", "MIS".into()),
                (
                    "order_type",
                    match leg.order_type {
                        crate::records::MarginOrderType::Limit => "LIMIT",
                        crate::records::MarginOrderType::Market => "MARKET",
                    }
                    .into(),
                ),
                ("quantity", leg.quantity.into()),
                ("price", price.into()),
            ];
            params.push(KiteValue::Map(fields.into_iter().collect()));
        }
        let payload = self.call(Call::Basket(&params, consider_positions)).await?;
        to_basket_margin(&payload, i64::try_from(params.len()).unwrap_or(i64::MAX))
    }

    /// `margins_shape`: `margins()` reduced to segment and field names with their types — **no
    /// figures**, so an account's balances never reach a probe report or a log.
    ///
    /// # Errors
    /// As the call.
    pub async fn margins_shape(&self) -> Result<serde_json::Value, ProviderError> {
        let payload = self.call(Call::Margins(None)).await?;
        Ok(margins_shape(&payload))
    }

    fn require_own_account(&self, account: &BrokerAccountRef) -> Result<(), ProviderError> {
        if account.broker_id != KITE_BROKER_ID {
            return Err(ProviderError::provider_unavailable(format!(
                "this adapter serves {} and was asked for {}; a broker's holdings are not \
                 fetchable with another broker's session",
                repr_str(KITE_BROKER_ID),
                repr_str(&account.broker_id)
            ))
            .with_provider(PROVIDER_NAME));
        }
        if let Some(own) = self.holdings_account_id
            && account.broker_account_id != own
        {
            return Err(ProviderError::provider_unavailable(format!(
                "the configured Kite session belongs to broker account {own} and holdings were \
                 requested for {}; refusing rather than answering with another tenant's \
                 positions",
                account.broker_account_id
            ))
            .with_provider(PROVIDER_NAME));
        }
        Ok(())
    }

    // --- internals ------------------------------------------------------------------------

    /// `_call`: rate limit, then run the call under the retry policy, translating errors.
    async fn call(&self, call: Call<'_>) -> Result<KiteValue, ProviderError> {
        call_with_retry(
            || self.attempt(call),
            &self.retry_policy,
            Some(PROVIDER_NAME),
            self.retry_hooks.as_ref(),
        )
        .await
    }

    async fn attempt(&self, call: Call<'_>) -> Result<KiteValue, ProviderError> {
        let client = self.authenticated_client()?;
        self.throttle().await?;
        let result = match call {
            Call::Instruments(exchange) => client.instruments(Some(exchange)).await,
            Call::Historical {
                token,
                from,
                to,
                interval,
            } => client.historical_data(token, from, to, interval).await,
            Call::Holdings => client.holdings().await,
            Call::Margins(segment) => client.margins(segment).await,
            Call::Quote(keys) => client.quote(keys).await,
            Call::Trades => client.trades().await,
            Call::Basket(params, consider_positions) => {
                if client.has_basket_order_margins() {
                    client
                        .basket_order_margins(params, consider_positions, None)
                        .await
                } else {
                    Err(KiteError::Other(
                        ProviderError::provider_unavailable(
                            "this Kite client has no basket_order_margins",
                        )
                        .with_provider(PROVIDER_NAME),
                    ))
                }
            }
        };
        result.map_err(translate)
    }

    async fn throttle(&self) -> Result<(), ProviderError> {
        let Some(limiter) = &self.rate_limiter else {
            return Err(ProviderError::provider_unavailable(
                "no shared rate limiter configured. Kite allows ~3 req/s across all workers \
                 (docs/09); calling it unthrottled risks the API key.",
            )
            .with_provider(PROVIDER_NAME));
        };
        limiter.acquire(1.0).await?;
        Ok(())
    }

    /// `_authenticated_client`: build the client once, and re-assert token freshness on every
    /// call — a backfill can outlive the token and must fail loudly at that moment.
    fn authenticated_client(&self) -> Result<Arc<dyn KiteClient>, ProviderError> {
        if !self.settings.kite_api_key.is_set() {
            return Err(
                ProviderError::credentials_missing("BASKFY_KITE_API_KEY is not set")
                    .with_provider(PROVIDER_NAME),
            );
        }
        let token = self.token_store.require_fresh(None)?;
        let mut slot = self.client.lock().map_err(|_| {
            ProviderError::foreign("RuntimeError", "the Kite client lock was poisoned")
        })?;
        let client = match slot.as_ref() {
            Some(c) => Arc::clone(c),
            None => {
                let built = (self.client_factory)(self.settings.kite_api_key.expose())?;
                *slot = Some(Arc::clone(&built));
                built
            }
        };
        drop(slot);
        client.set_access_token(&token.value);
        Ok(client)
    }
}

impl HealthReporting for KiteProvider {
    fn name(&self) -> &str {
        PROVIDER_NAME
    }

    /// Bars, and the read-only broker ledger. Never an order verb — law 2.
    fn capabilities(&self) -> CapabilitySet {
        let mut set = bars_capabilities();
        set.extend(holdings_capabilities());
        set
    }

    /// Readiness from configuration alone. Never fails, never touches the network.
    fn check(&self) -> ProviderHealth {
        let mut problems: Vec<String> = Vec::new();
        if !self.settings.kite_api_key.is_set() {
            problems.push("BASKFY_KITE_API_KEY is not set".to_owned());
        }
        if !self.settings.kite_api_secret.is_set() {
            problems.push("BASKFY_KITE_API_SECRET is not set".to_owned());
        }
        if !self.settings.token_encryption_configured() {
            problems.push("BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set".to_owned());
        } else if !self.token_store.exists() {
            problems.push(format!(
                "no encrypted access token at {}",
                self.token_store.path().display()
            ));
        } else if let Err(error) = self.token_store.require_fresh(None) {
            problems.push(error.to_string());
        }
        if self.rate_limiter.is_none() {
            problems.push(
                "no shared rate limiter configured; refusing to call Kite unthrottled".to_owned(),
            );
        }
        if problems.is_empty() {
            return ProviderHealth {
                name: PROVIDER_NAME.to_owned(),
                available: true,
                capabilities: self.capabilities(),
                detail: format!(
                    "access token valid, {} req/s",
                    general(self.settings.kite_rate_limit_per_second)
                ),
            };
        }
        ProviderHealth {
            name: PROVIDER_NAME.to_owned(),
            available: false,
            capabilities: self.capabilities(),
            detail: problems.join("; "),
        }
    }
}

#[async_trait]
impl BarsProvider for KiteProvider {
    async fn list_instruments(&self) -> Result<Vec<InstrumentRecord>, ProviderError> {
        Self::list_instruments(self).await
    }

    async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        Self::daily_bars(self, token, start, end).await
    }
}

#[async_trait]
impl HoldingsProvider for KiteProvider {
    async fn broker_holdings(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Vec<BrokerHoldingRecord>, ProviderError> {
        Self::broker_holdings(self, account).await
    }

    async fn broker_cash(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Option<Decimal>, ProviderError> {
        Self::broker_cash(self, account).await
    }
}

/// The shared window walk behind `chunk_windows` and `minute_windows`.
fn windows(
    start: NaiveDate,
    end: NaiveDate,
    max_days: i64,
) -> Result<Vec<(NaiveDate, NaiveDate)>, ProviderError> {
    let overflow = || ProviderError::foreign("OverflowError", "date value out of range");
    // Settings refuse a cap below 1 (gt=0); a zero span still makes progress rather than loop.
    let span = Duration::try_days(max_days.max(1) - 1).ok_or_else(overflow)?;
    let mut out = Vec::new();
    let mut cursor = start;
    while cursor <= end {
        let chunk_end = cursor
            .checked_add_signed(span)
            .ok_or_else(overflow)?
            .min(end);
        out.push((cursor, chunk_end));
        match chunk_end.succ_opt() {
            Some(next) => cursor = next,
            None => break,
        }
    }
    Ok(out)
}

/// `_ist_wall`: a naive IST wall-clock datetime, as Kite's historical API reads its bounds.
fn ist_wall(value: MinuteBound, end_of_day: bool) -> NaiveDateTime {
    match value {
        MinuteBound::DateTime(PyDateTime::Naive(n)) => n,
        MinuteBound::DateTime(PyDateTime::Aware(a)) => a.with_timezone(&ist()).naive_local(),
        MinuteBound::Date(d) => {
            let time = if end_of_day {
                NaiveTime::from_hms_opt(23, 59, 59).unwrap_or(NaiveTime::MIN)
            } else {
                NaiveTime::MIN
            };
            d.and_time(time)
        }
    }
}

// =============================================================================================
// Mapping
// =============================================================================================

/// `_split_sme_symbol`: `("SHEETAL-SM", "NSE") -> ("SHEETAL", Some("SM"))`; anything else,
/// and anything off NSE, passes through.
#[must_use]
pub fn split_sme_symbol(symbol: &str, exchange: &str) -> (String, Option<String>) {
    if exchange.to_uppercase() != "NSE" {
        return (symbol.to_owned(), None);
    }
    for suffix in SME_SUFFIXES {
        if let Some(bare) = symbol.strip_suffix(suffix)
            && symbol.chars().count() > suffix.chars().count()
        {
            return (
                bare.to_owned(),
                Some(suffix.trim_start_matches('-').to_owned()),
            );
        }
    }
    (symbol.to_owned(), None)
}

fn to_instrument_record(row: &KiteValue) -> Result<Option<InstrumentRecord>, ProviderError> {
    let Some(symbol) = text(row.get("tradingsymbol")?) else {
        return Ok(None);
    };
    let segment = text(row.get("segment")?).unwrap_or_default();
    let instrument_type_raw = text(row.get("instrument_type")?).unwrap_or_default();
    let instrument_type = if segment.to_uppercase() == INDEX_SEGMENT {
        InstrumentType::Index
    } else if instrument_type_raw.to_uppercase() == "EQ" {
        InstrumentType::Eq
    } else {
        // Futures, options and everything else Kite carries are out of scope.
        return Ok(None);
    };
    let exchange = text(row.get("exchange")?).unwrap_or_else(|| "NSE".to_owned());
    let (bare, sme_series) = split_sme_symbol(&symbol, &exchange);
    let name = text(row.get("name")?).unwrap_or_else(|| bare.clone());
    let mut fields = InstrumentFields::new(&bare, &name, instrument_type);
    fields.series = sme_series;
    fields.kite_token = to_int(row.get("instrument_token")?)?;
    fields.lot_size = to_int(row.get("lot_size")?)?;
    fields.exchange = exchange;
    InstrumentRecord::new(fields)
        .map(Some)
        .map_err(|e| validation(&e))
}

fn to_option_contract(
    row: &KiteValue,
    underlying: &str,
) -> Result<Option<OptionContractRecord>, ProviderError> {
    if text(row.get("name")?).unwrap_or_default().to_uppercase() != underlying {
        return Ok(None);
    }
    let option_type = text(row.get("instrument_type")?)
        .unwrap_or_default()
        .to_uppercase();
    if option_type != "CE" && option_type != "PE" {
        return Ok(None);
    }
    let token = to_int(row.get("instrument_token")?)?;
    let symbol = text(row.get("tradingsymbol")?);
    let expiry_raw = row.get("expiry")?;
    let strike = to_decimal(row.get("strike")?)?;
    let lot_size = to_int(row.get("lot_size")?)?;
    let tick_size = to_decimal(row.get("tick_size")?)?;
    let (Some(token), Some(symbol), Some(strike), Some(lot_size), Some(tick_size)) =
        (token, symbol, strike, lot_size, tick_size)
    else {
        return Ok(None);
    };
    let expiry_missing = matches!(expiry_raw, KiteValue::None)
        || matches!(expiry_raw, KiteValue::Str(s) if s.is_empty());
    if token == 0 || expiry_missing || strike.is_zero() || lot_size == 0 || tick_size.is_zero() {
        return Ok(None);
    }
    OptionContractRecord::new(OptionContractFields {
        instrument_token: token,
        tradingsymbol: symbol,
        underlying: underlying.to_owned(),
        expiry: to_date(expiry_raw)?,
        strike,
        option_type: if option_type == "CE" {
            OptionType::Ce
        } else {
            OptionType::Pe
        },
        lot_size,
        tick_size,
    })
    .map(Some)
    .map_err(|e| validation(&e))
}

/// `except (UnexpectedPayload, ValueError)` — pydantic's `ValidationError`, `dateutil`'s
/// `ParserError` and `UnicodeDecodeError` are `ValueError`s; `decimal.InvalidOperation` is not.
fn is_unreadable(error: &ProviderError) -> bool {
    error.kind() == ErrorKind::UnexpectedPayload
        || matches!(
            error.python_type(),
            "ValueError" | "ValidationError" | "ParserError" | "UnicodeDecodeError"
        )
}

fn to_holding_record(row: &KiteValue) -> Result<Option<BrokerHoldingRecord>, ProviderError> {
    let Some(symbol) = text(row.get("tradingsymbol")?).or(text(row.get("symbol")?)) else {
        return Ok(None);
    };
    let build = || -> Result<BrokerHoldingRecord, ProviderError> {
        let mut fields = BrokerHoldingFields::new(&symbol);
        fields.exchange = text(row.get("exchange")?).unwrap_or_else(|| "NSE".to_owned());
        fields.isin = text(row.get("isin")?);
        fields.quantity = quantity(row.get("quantity")?)?;
        fields.t1_quantity = quantity(row.get("t1_quantity")?)?;
        fields.collateral_quantity = quantity(row.get("collateral_quantity")?)?;
        fields.average_price = to_decimal(row.get("average_price")?)?;
        fields.last_price = to_decimal(row.get("last_price")?)?;
        fields.product = text(row.get("product")?).unwrap_or_else(|| "CNC".to_owned());
        BrokerHoldingRecord::new(fields).map_err(|e| validation(&e))
    };
    match build() {
        Ok(record) => Ok(Some(record)),
        // Narrow on purpose (house rule 3): a payload we cannot read is skipped, a bug in our
        // own mapping still escapes.
        Err(error) if is_unreadable(&error) => Ok(None),
        Err(error) => Err(error),
    }
}

fn to_trade_fill(row: &KiteValue) -> Result<Option<TradeFill>, ProviderError> {
    let exchange = text(row.get("exchange")?)
        .unwrap_or_default()
        .to_uppercase();
    if !EQUITY_EXCHANGES.contains(&exchange.as_str()) {
        return Ok(None);
    }
    let symbol = text(row.get("tradingsymbol")?);
    let trade_id = text(row.get("trade_id")?);
    let side_raw = text(row.get("transaction_type")?)
        .unwrap_or_default()
        .to_uppercase();
    let quantity = to_decimal(row.get("quantity")?)?;
    let price = to_decimal(row.get("average_price")?)?;
    let when = row
        .get("fill_timestamp")?
        .or(row.get("exchange_timestamp")?)
        .or(row.get("order_timestamp")?);
    let side = match side_raw.as_str() {
        "BUY" => Some(TradeSide::Buy),
        "SELL" => Some(TradeSide::Sell),
        _ => None,
    };
    let unreadable = || {
        let show = |v: &Option<String>| v.as_deref().map_or_else(|| "None".to_owned(), repr_str);
        ProviderError::unexpected_payload(format!(
            "a trade row could not be read (trade_id={}, symbol={})",
            show(&trade_id),
            show(&symbol)
        ))
        .with_provider(PROVIDER_NAME)
    };
    let (Some(symbol_text), Some(trade_id_text), Some(side), Some(quantity), Some(price)) =
        (symbol.clone(), trade_id.clone(), side, quantity, price)
    else {
        return Err(unreadable());
    };
    let KiteValue::DateTime(when) = when else {
        return Err(unreadable());
    };
    if quantity <= Decimal::ZERO {
        return Err(unreadable());
    }
    Ok(Some(TradeFill {
        symbol: symbol_text.to_uppercase(),
        exchange,
        side,
        quantity,
        price,
        trade_date: naive_of(when).date(),
        trade_id: trade_id_text,
        order_id: text(row.get("order_id")?),
        isin: None,
        segment: None,
        executed_at: Some(*when),
    }))
}

/// `_aware_ist`: a Kite timestamp as an aware datetime; a naive one is IST.
fn aware_ist(value: &KiteValue) -> Option<DateTime<FixedOffset>> {
    let as_aware = |dt: PyDateTime| match dt {
        PyDateTime::Aware(a) => Some(a),
        PyDateTime::Naive(n) => n.and_local_timezone(ist()).single(),
    };
    match value {
        KiteValue::DateTime(dt) => as_aware(*dt),
        KiteValue::Str(s) if !s.is_empty() => py_fromisoformat(s).ok().and_then(as_aware),
        _ => None,
    }
}

fn to_minute_bar(candle: &KiteValue) -> Result<Option<MinuteBarRecord>, ProviderError> {
    let ts = aware_ist(candle.get("date")?);
    let mut prices = Vec::with_capacity(4);
    for key in ["open", "high", "low", "close"] {
        prices.push(to_decimal(candle.get(key)?)?);
    }
    let (Some(ts), [Some(open), Some(high), Some(low), Some(close)]) =
        (ts, [prices[0], prices[1], prices[2], prices[3]])
    else {
        return Ok(None);
    };
    let floored = ts
        .with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .unwrap_or(ts);
    MinuteBarRecord::new(MinuteBarFields {
        ts: PyDateTime::Aware(floored),
        open,
        high,
        low,
        close,
        volume: to_int(candle.get("volume")?)?.unwrap_or(0),
    })
    .map(Some)
    .map_err(|e| validation(&e))
}

/// `_depth_side`: one side of Kite's `depth`; zero-priced padding levels are dropped.
fn depth_side(raw: &KiteValue) -> Result<Vec<DepthLevelRecord>, ProviderError> {
    let KiteValue::List(levels) = raw else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    for level in levels {
        if !matches!(level, KiteValue::Map(_)) {
            continue;
        }
        let Some(price) = to_decimal(level.get("price")?)? else {
            continue;
        };
        if price <= Decimal::ZERO {
            continue;
        }
        out.push(
            DepthLevelRecord::new(DepthLevelFields {
                price,
                quantity: to_int(level.get("quantity")?)?.unwrap_or(0).max(0),
                orders: to_int(level.get("orders")?)?.unwrap_or(0).max(0),
            })
            .map_err(|e| validation(&e))?,
        );
    }
    Ok(out)
}

fn to_option_quote(key: &str, row: &KiteValue) -> Result<Option<OptionQuoteRecord>, ProviderError> {
    if !matches!(row, KiteValue::Map(_)) {
        return Ok(None);
    }
    let (exchange, symbol) = key.split_once(':').unwrap_or((key, ""));
    if symbol.is_empty() || exchange.is_empty() {
        return Ok(None);
    }
    let depth = row.get("depth")?;
    let empty = KiteValue::Map(KiteMap::new());
    let sides = if matches!(depth, KiteValue::Map(_)) {
        depth
    } else {
        &empty
    };
    let build = || -> Result<OptionQuoteRecord, ProviderError> {
        OptionQuoteRecord::new(OptionQuoteFields {
            symbol: symbol.to_owned(),
            exchange: exchange.to_owned(),
            instrument_token: to_int(row.get("instrument_token")?)?,
            last_price: to_decimal(row.get("last_price")?)?,
            volume: to_int(row.get("volume")?.or(row.get("volume_traded")?))?.unwrap_or(0),
            oi: to_int(row.get("oi")?)?,
            oi_day_high: to_int(row.get("oi_day_high")?)?,
            oi_day_low: to_int(row.get("oi_day_low")?)?,
            bids: depth_side(sides.get("buy")?)?,
            asks: depth_side(sides.get("sell")?)?,
            as_of: aware_ist(row.get("timestamp")?).map(PyDateTime::Aware),
            last_trade_time: aware_ist(row.get("last_trade_time")?).map(PyDateTime::Aware),
        })
        .map_err(|e| validation(&e))
    };
    match build() {
        Ok(record) => Ok(Some(record)),
        // Narrow on purpose (house rule 3): one unreadable quote must not cost the others.
        Err(error) if is_unreadable(&error) => Ok(None),
        Err(error) => Err(error),
    }
}

/// `_margins_shape`: segment -> field -> JSON type name (or a nested block's sorted keys).
fn margins_shape(payload: &KiteValue) -> serde_json::Value {
    use serde_json::Value;
    let KiteValue::Map(map) = payload else {
        return Value::Object(
            [(
                "type".to_owned(),
                Value::String(payload.type_name().to_owned()),
            )]
            .into_iter()
            .collect(),
        );
    };
    let mut out = serde_json::Map::new();
    for (segment, body) in map.iter() {
        let KiteValue::Map(fields) = body else {
            out.insert(
                segment.to_owned(),
                Value::String(body.type_name().to_owned()),
            );
            continue;
        };
        let mut block = serde_json::Map::new();
        for (key, value) in fields.iter() {
            let described = match value {
                KiteValue::Map(inner) => {
                    let mut keys: Vec<String> = inner.iter().map(|(k, _)| k.to_owned()).collect();
                    keys.sort();
                    Value::Array(keys.into_iter().map(Value::String).collect())
                }
                other => Value::String(other.type_name().to_owned()),
            };
            block.insert(key.to_owned(), described);
        }
        out.insert(segment.to_owned(), Value::Object(block));
    }
    Value::Object(out)
}

fn to_basket_margin(payload: &KiteValue, legs: i64) -> Result<BasketMarginRecord, ProviderError> {
    let KiteValue::Map(map) = payload else {
        return Err(
            ProviderError::unexpected_payload("basket margins returned no object")
                .with_provider(PROVIDER_NAME),
        );
    };
    let sorted_keys = |m: &KiteMap| {
        let mut keys: Vec<String> = m.iter().map(|(k, _)| k.to_owned()).collect();
        keys.sort();
        keys
    };
    let mut fields = BasketMarginFields {
        legs,
        ..BasketMarginFields::default()
    };
    fields.shape.insert("top".to_owned(), sorted_keys(map));
    for block in ["initial", "final"] {
        let total = match map.get(block) {
            Some(KiteValue::Map(raw)) => {
                fields.shape.insert(block.to_owned(), sorted_keys(raw));
                to_decimal(raw.get("total").unwrap_or(&NONE))?
            }
            _ => None,
        };
        if block == "initial" {
            fields.initial_total = total;
        } else {
            fields.final_total = total;
        }
    }
    BasketMarginRecord::new(fields).map_err(|e| validation(&e))
}

fn to_quote_record(key: &str, row: &KiteValue) -> Result<Option<QuoteRecord>, ProviderError> {
    if !matches!(row, KiteValue::Map(_)) {
        return Ok(None);
    }
    let (exchange, symbol) = key.split_once(':').unwrap_or((key, ""));
    if symbol.is_empty() {
        return Ok(None);
    }
    let Some(last) = to_decimal(row.get("last_price")?)? else {
        return Ok(None);
    };
    let ohlc = row.get("ohlc")?;
    let empty = KiteValue::Map(KiteMap::new());
    let bands = if matches!(ohlc, KiteValue::Map(_)) {
        ohlc
    } else {
        &empty
    };
    let stamp = row.get("timestamp")?.or(row.get("last_trade_time")?);
    let mut fields = QuoteFields::new(symbol, last);
    fields.exchange = if exchange.is_empty() { "NSE" } else { exchange }.to_owned();
    fields.instrument_token = to_int(row.get("instrument_token")?)?;
    fields.volume = to_int(row.get("volume")?.or(row.get("volume_traded")?))?.unwrap_or(0);
    fields.prev_close = to_decimal(bands.get("close")?)?;
    fields.open = to_decimal(bands.get("open")?)?;
    fields.high = to_decimal(bands.get("high")?)?;
    fields.low = to_decimal(bands.get("low")?)?;
    fields.upper_circuit = to_decimal(row.get("upper_circuit_limit")?)?;
    fields.lower_circuit = to_decimal(row.get("lower_circuit_limit")?)?;
    fields.as_of = match stamp {
        KiteValue::DateTime(dt) => Some(*dt),
        _ => None,
    };
    QuoteRecord::new(fields)
        .map(Some)
        .map_err(|e| validation(&e))
}

/// `_equity_net_cash`: the equity segment's `net`, from the segment or the whole envelope.
fn equity_net_cash(payload: &KiteValue) -> Result<Option<Decimal>, ProviderError> {
    let segment = payload.get(EQUITY_SEGMENT)?;
    let body = if matches!(segment, KiteValue::Map(_)) {
        segment
    } else {
        payload
    };
    let net = body.get("net")?;
    if matches!(net, KiteValue::None) {
        return Ok(None);
    }
    to_decimal(net)
}

/// `_quantity`: absent means zero — a segment the broker did not mention holds none.
fn quantity(value: &KiteValue) -> Result<Decimal, ProviderError> {
    Ok(to_decimal(value)?.unwrap_or(Decimal::ZERO))
}

/// One daily candle, read.
struct DailyRow {
    date: NaiveDate,
    open: Option<Decimal>,
    high: Option<Decimal>,
    low: Option<Decimal>,
    close: Option<Decimal>,
    volume: i64,
}

/// The rows of `_candles_to_frame`, read in Python's order (date, open, high, low, close,
/// volume) so the first unreadable cell raises the same error.
fn candles_to_rows(candles: &KiteValue) -> Result<Vec<DailyRow>, ProviderError> {
    let mut rows = Vec::new();
    for candle in candles.py_iter()? {
        rows.push(DailyRow {
            date: to_date(candle.get("date")?)?,
            open: to_decimal(candle.get("open")?)?,
            high: to_decimal(candle.get("high")?)?,
            low: to_decimal(candle.get("low")?)?,
            close: to_decimal(candle.get("close")?)?,
            volume: to_int(candle.get("volume")?)?.unwrap_or(0),
        });
    }
    Ok(rows)
}

type PricePick = fn(&DailyRow) -> Option<Decimal>;

/// The four price columns, in the schema's order.
const PRICE_COLUMNS: [(&str, PricePick); 4] = [
    ("open", |r| r.open),
    ("high", |r| r.high),
    ("low", |r| r.low),
    ("close", |r| r.close),
];

const PRICE_PRECISION: usize = 18;
const PRICE_SCALE: usize = 4;
const PRICE_SCALE_DP: u32 = 4;

/// One price column at `Decimal(18, 4)`: polars infers `Decimal(38, max scale)` from Python
/// Decimals and `conform`'s strict cast rounds half to even onto scale 4, refusing a value
/// beyond 18 digits (verified against polars 1.43.2; DECISIONS-RS R4.10).
fn price_column(name: &str, values: &[Option<Decimal>]) -> Result<Series, ProviderError> {
    let inferred_scale = values
        .iter()
        .flatten()
        .map(Decimal::scale)
        .max()
        .unwrap_or(0);
    let limit: i128 = 1_000_000_000_000_000_000;
    let mut mantissas = Vec::with_capacity(values.len());
    let mut failed: Vec<String> = Vec::new();
    for value in values {
        mantissas.push(match value {
            None => None,
            Some(v) => {
                let mut rounded =
                    v.round_dp_with_strategy(PRICE_SCALE_DP, RoundingStrategy::MidpointNearestEven);
                rounded.rescale(PRICE_SCALE_DP);
                let m = rounded.mantissa();
                if m.abs() >= limit {
                    failed.push(v.to_string());
                }
                Some(m)
            }
        });
    }
    if !failed.is_empty() {
        return Err(ProviderError::foreign(
            "InvalidOperationError",
            format!(
                "conversion from `decimal[38,{inferred_scale}]` to `decimal[18,4]` failed in \
                 column '{name}' for {} out of {} values: [{}]",
                failed.len(),
                values.len(),
                failed.join(", ")
            ),
        ));
    }
    let chunked = Int128Chunked::from_iter_options(name.into(), mantissas.into_iter());
    Ok(chunked
        .into_decimal_unchecked(PRICE_PRECISION, PRICE_SCALE)
        .into_series())
}

/// `_candles_to_frame` + `conform(..., DAILY_BARS_SCHEMA)` over the deduplicated rows.
///
/// Built eagerly, column by column, in the schema's own dtypes, so `conform`'s strict cast is
/// the identity; the schema is then checked rather than cast. `records::conform` runs polars'
/// lazy engine, whose `block_in_place` panics on a current-thread tokio runtime — an adapter
/// method must not (DECISIONS-RS R4.10).
fn daily_frame(symbol: &str, rows: Vec<DailyRow>) -> Result<DataFrame, ProviderError> {
    let height = rows.len();
    let dates: Vec<NaiveDate> = rows.iter().map(|r| r.date).collect();
    let pick = |f: fn(&DailyRow) -> Option<Decimal>| rows.iter().map(f).collect::<Vec<_>>();
    let columns = vec![
        Series::new("symbol".into(), vec![symbol; height]).into_column(),
        Series::new("date".into(), dates).into_column(),
        price_column("open", &pick(|r| r.open))?.into_column(),
        price_column("high", &pick(|r| r.high))?.into_column(),
        price_column("low", &pick(|r| r.low))?.into_column(),
        price_column("close", &pick(|r| r.close))?.into_column(),
        Series::new(
            "volume".into(),
            rows.iter().map(|r| r.volume).collect::<Vec<i64>>(),
        )
        .into_column(),
        Series::new("source".into(), vec![PROVIDER_NAME; height]).into_column(),
    ];
    let frame = DataFrame::new(height, columns)
        .map_err(|e| ProviderError::foreign("ShapeError", e.to_string()))?;
    if frame.schema().as_ref() != &daily_bars_schema() {
        return Err(ProviderError::foreign(
            "SchemaError",
            format!("daily bars built off-schema: {:?}", frame.schema()),
        ));
    }
    Ok(frame)
}

// =============================================================================================
// Python's coercions: _text, _int, _decimal, _date
// =============================================================================================

/// `_text`: `str(value).strip()`, `None` when that is empty or the value is `None`.
fn text(value: &KiteValue) -> Option<String> {
    if matches!(value, KiteValue::None) {
        return None;
    }
    let s = value.py_str();
    let trimmed = s.trim();
    (!trimmed.is_empty()).then(|| trimmed.to_owned())
}

/// `int(x)` for a float: truncation, with Python's errors.
fn float_to_int(f: f64) -> Result<i64, ProviderError> {
    if f.is_nan() {
        return Err(ProviderError::value_error(
            "cannot convert float NaN to integer",
        ));
    }
    if f.is_infinite() {
        return Err(ProviderError::foreign(
            "OverflowError",
            "cannot convert float infinity to integer",
        ));
    }
    let truncated = f.trunc();
    // i64's range as floats: [-2^63, 2^63).
    if (-9_223_372_036_854_775_808.0..9_223_372_036_854_775_808.0).contains(&truncated) {
        #[allow(clippy::cast_possible_truncation)]
        Ok(truncated as i64)
    } else {
        // Python's int is unbounded; an instrument field this wide is not Kite's.
        Err(ProviderError::foreign(
            "OverflowError",
            format!("{} does not fit a 64-bit integer", repr_float(f)),
        ))
    }
}

/// `_int`.
fn to_int(value: &KiteValue) -> Result<Option<i64>, ProviderError> {
    match value {
        KiteValue::None => Ok(None),
        KiteValue::Str(s) if s.is_empty() => Ok(None),
        KiteValue::Bool(b) => Ok(Some(i64::from(*b))),
        KiteValue::Int(i) => Ok(Some(*i)),
        KiteValue::Float(f) => float_to_int(*f).map(Some),
        KiteValue::Str(s) => float_to_int(py_float_from_str(s)?).map(Some),
        other => Err(ProviderError::unexpected_payload(format!(
            "cannot read {} as an integer",
            other.py_repr()
        ))
        .with_provider(PROVIDER_NAME)),
    }
}

/// `_decimal`: prices become Decimal at the boundary, never float — `Decimal(str(value))`.
fn to_decimal(value: &KiteValue) -> Result<Option<Decimal>, ProviderError> {
    match value {
        KiteValue::None => Ok(None),
        KiteValue::Str(s) if s.is_empty() => Ok(None),
        KiteValue::Int(i) => Ok(Some(Decimal::from(*i))),
        KiteValue::Float(_) | KiteValue::Str(_) | KiteValue::Bool(_) => {
            py_decimal_from_str(&value.py_str()).map(Some)
        }
        other => Err(ProviderError::unexpected_payload(format!(
            "cannot read {} as a price",
            other.py_repr()
        ))
        .with_provider(PROVIDER_NAME)),
    }
}

/// `_date`.
fn to_date(value: &KiteValue) -> Result<NaiveDate, ProviderError> {
    match value {
        KiteValue::DateTime(dt) => Ok(naive_of(dt).date()),
        KiteValue::Date(d) => Ok(*d),
        KiteValue::Str(s) => py_fromisoformat(s).map(|dt| naive_of(&dt).date()),
        other => Err(ProviderError::unexpected_payload(format!(
            "cannot read {} as a date",
            other.py_repr()
        ))
        .with_provider(PROVIDER_NAME)),
    }
}

/// PEP 515: an underscore is allowed only between two digits; returns the text without them.
fn strip_digit_underscores(text: &str) -> Option<String> {
    if !text.contains('_') {
        return Some(text.to_owned());
    }
    let chars: Vec<char> = text.chars().collect();
    for (i, c) in chars.iter().enumerate() {
        if *c == '_' {
            let before = i.checked_sub(1).and_then(|j| chars.get(j));
            let after = chars.get(i + 1);
            if !(before.is_some_and(char::is_ascii_digit)
                && after.is_some_and(char::is_ascii_digit))
            {
                return None;
            }
        }
    }
    Some(text.replace('_', ""))
}

/// Python's `float(text)`.
fn py_float_from_str(text: &str) -> Result<f64, ProviderError> {
    let invalid = || {
        ProviderError::value_error(format!(
            "could not convert string to float: {}",
            repr_str(text)
        ))
    };
    let cleaned = strip_digit_underscores(text.trim()).ok_or_else(invalid)?;
    f64::from_str(&cleaned).map_err(|_| invalid())
}

/// Python's `int(text)` (base 10).
fn py_int_from_str(text: &str) -> Result<i64, ProviderError> {
    let invalid = || {
        ProviderError::value_error(format!(
            "invalid literal for int() with base 10: {}",
            repr_str(text)
        ))
    };
    let cleaned = strip_digit_underscores(text.trim()).ok_or_else(invalid)?;
    if cleaned.is_empty()
        || !cleaned
            .trim_start_matches(['+', '-'])
            .chars()
            .all(|c| c.is_ascii_digit())
    {
        return Err(invalid());
    }
    i64::from_str(&cleaned).map_err(|_| invalid())
}

/// Python's `Decimal(text)`: whitespace stripped, digit underscores allowed, an optional
/// exponent. Scale is kept (`"25000.0"` stays one place). `NaN`/`Infinity` and values beyond
/// `rust_decimal`'s 28 digits are refused as `InvalidOperation` (Python would hold them —
/// DECISIONS-RS R4.10); a syntax error is Python's `[<class 'decimal.ConversionSyntax'>]`.
fn py_decimal_from_str(text: &str) -> Result<Decimal, ProviderError> {
    let syntax =
        || ProviderError::foreign("InvalidOperation", "[<class 'decimal.ConversionSyntax'>]");
    // CPython's `Decimal(str)` drops *every* underscore (`"1__0"`, `"_1"` and `"1_"` are all
    // accepted), unlike `int()`/`float()`, which allow one only between two digits.
    let cleaned = text.trim().replace('_', "");
    let lowered = cleaned.trim_start_matches(['+', '-']).to_lowercase();
    if matches!(lowered.as_str(), "nan" | "snan" | "inf" | "infinity") {
        return Err(ProviderError::foreign(
            "InvalidOperation",
            format!(
                "Decimal({}) is not representable without NaN/Infinity (DECISIONS-RS R4.10)",
                repr_str(text)
            ),
        ));
    }
    let (mantissa, exponent) = match cleaned.find(['e', 'E']) {
        Some(at) => (&cleaned[..at], Some(&cleaned[at + 1..])),
        None => (cleaned.as_str(), None),
    };
    let unsigned = mantissa.trim_start_matches(['+', '-']);
    if mantissa.len() - unsigned.len() > 1 {
        return Err(syntax());
    }
    let (int_part, frac_part) = unsigned.split_once('.').unwrap_or((unsigned, ""));
    let digits_ok = |s: &str| s.chars().all(|c| c.is_ascii_digit());
    if (int_part.is_empty() && frac_part.is_empty())
        || !digits_ok(int_part)
        || !digits_ok(frac_part)
    {
        return Err(syntax());
    }
    if let Some(exp) = exponent {
        let body = exp.trim_start_matches(['+', '-']);
        if body.is_empty() || exp.len() - body.len() > 1 || !digits_ok(body) {
            return Err(syntax());
        }
    }
    let out_of_range = || {
        ProviderError::foreign(
            "InvalidOperation",
            format!(
                "Decimal({}) is beyond rust_decimal's 28 digits (DECISIONS-RS R4.10)",
                repr_str(text)
            ),
        )
    };
    let normalised = format!(
        "{}{}.{}",
        if mantissa.starts_with('-') { "-" } else { "" },
        if int_part.is_empty() { "0" } else { int_part },
        if frac_part.is_empty() { "0" } else { frac_part }
    );
    let mut value = Decimal::from_str_exact(&normalised).map_err(|_| out_of_range())?;
    // "5" and "5." have scale 0 in Python; the ".0" above was only for the parser.
    let scale = u32::try_from(frac_part.len()).map_err(|_| out_of_range())?;
    value.rescale(scale);
    if let Some(exp) = exponent {
        let shift: i64 = exp.parse().map_err(|_| out_of_range())?;
        let new_scale = i64::from(scale) - shift;
        if new_scale >= 0 {
            let s = u32::try_from(new_scale).map_err(|_| out_of_range())?;
            if s > 28 {
                return Err(out_of_range());
            }
            value = Decimal::from_i128_with_scale(value.mantissa(), s);
        } else {
            let up = u32::try_from(-new_scale).map_err(|_| out_of_range())?;
            let factor = 10_i128.checked_pow(up).ok_or_else(out_of_range)?;
            let m = value
                .mantissa()
                .checked_mul(factor)
                .ok_or_else(out_of_range)?;
            value = Decimal::try_from_i128_with_scale(m, 0).map_err(|_| out_of_range())?;
        }
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decimals_parse_like_python() {
        let d = |s: &str| py_decimal_from_str(s).map(|v| v.to_string());
        assert_eq!(d("25000.0").ok().as_deref(), Some("25000.0"));
        assert_eq!(d(" 1.5 ").ok().as_deref(), Some("1.5"));
        assert_eq!(d("1_000").ok().as_deref(), Some("1000"));
        assert_eq!(d("1e-07").ok().as_deref(), Some("0.0000001"));
        assert_eq!(d("1.25e2").ok().as_deref(), Some("125"));
        assert_eq!(d("5.").ok().as_deref(), Some("5"));
        assert_eq!(d(".5").ok().as_deref(), Some("0.5"));
        assert_eq!(d("-0.05").ok().as_deref(), Some("-0.05"));
        assert!(d("True").is_err());
        assert!(d("abc").is_err());
        assert_eq!(d("1__0").ok().as_deref(), Some("10"));
        assert!(d("NaN").is_err());
    }

    /// `_decimal`, `_int`, `_text`, `_date` and `_aware_ist` against the live Python's answers
    /// (`tests/fixtures/kite_coercions.py`, run with the repo's interpreter, 29 Oct 2026). The
    /// documented R4.10 limits — Decimal NaN/Infinity, more than 28 digits, ints beyond i64 —
    /// must still be refused, never answered differently.
    #[test]
    fn coercions_match_the_live_python() {
        use serde_json::Value;
        let key: Value =
            serde_json::from_str(include_str!("../tests/fixtures/kite_coercions.json"))
                .unwrap_or(Value::Null);
        let input = |c: &Value| -> KiteValue {
            let v = &c["in"];
            match v["t"].as_str().unwrap_or_default() {
                "bool" => KiteValue::Bool(v["v"].as_bool().unwrap_or_default()),
                "int" => KiteValue::Int(v["v"].as_i64().unwrap_or_default()),
                "float" => KiteValue::Float(
                    v["v"]
                        .as_str()
                        .unwrap_or_default()
                        .parse()
                        .unwrap_or_default(),
                ),
                "str" => KiteValue::Str(v["v"].as_str().unwrap_or_default().to_owned()),
                _ => KiteValue::None,
            }
        };
        let limit = |e: &ProviderError| {
            e.to_string().contains("R4.10") || e.python_type() == "OverflowError"
        };
        let mut checked = 0;
        let mut failures: Vec<String> = Vec::new();
        for c in key["scalars"].as_array().into_iter().flatten() {
            let value = input(c);
            let expect = &c["decimal"];
            match (to_decimal(&value), expect.get("ok")) {
                (Ok(None), Some(Value::Null)) => {}
                (Ok(Some(d)), Some(Value::String(s))) if expect.get("special").is_none() => {
                    let scale = expect["scale"].as_u64().unwrap_or_default();
                    if d.to_string().trim_start_matches('-') != s.trim_start_matches('-')
                        || u64::from(d.scale()) != scale
                    {
                        failures.push(format!("decimal {value:?}: {d} vs {s}/{scale}"));
                    }
                }
                (Err(e), Some(_)) if limit(&e) => {}
                (Err(e), None) if e.python_type() == expect["err"].as_str().unwrap_or("?") => {}
                (got, _) => failures.push(format!("decimal {value:?}: {got:?} vs {expect}")),
            }
            let expect = &c["int"];
            match (to_int(&value), expect.get("ok")) {
                (Ok(None), Some(Value::Null)) => {}
                (Ok(Some(i)), Some(n)) if n.as_i64() == Some(i) => {}
                (Err(e), Some(n)) if n.as_i64().is_none() && limit(&e) => {}
                (Err(e), None) if e.python_type() == expect["err"].as_str().unwrap_or("?") => {}
                (got, _) => failures.push(format!("int {value:?}: {got:?} vs {expect}")),
            }
            let expect = &c["text"]["ok"];
            if text(&value).as_deref() != expect.as_str() {
                failures.push(format!("text {value:?}: {:?} vs {expect}", text(&value)));
            }
            checked += 1;
        }
        for c in key["dates"].as_array().into_iter().flatten() {
            let value = KiteValue::Str(c["in"].as_str().unwrap_or_default().to_owned());
            let expect = &c["date"];
            match (to_date(&value), expect.get("ok")) {
                (Ok(d), Some(Value::String(s))) if d.format("%Y-%m-%d").to_string() == *s => {}
                (Err(e), None) if e.python_type() == expect["err"].as_str().unwrap_or("?") => {}
                (got, _) => failures.push(format!("date {value:?}: {got:?} vs {expect}")),
            }
            let got = aware_ist(&value).map(|a| PyDateTime::Aware(a).isoformat());
            if got.as_deref() != c["aware"]["ok"].as_str() {
                failures.push(format!("aware {value:?}: {got:?} vs {}", c["aware"]));
            }
            checked += 1;
        }
        assert!(checked > 80, "the answer key did not load ({checked})");
        assert!(failures.is_empty(), "{}", failures.join("\n"));
    }

    /// `_parse_instruments` against kiteconnect 5.2.1's own answers
    /// (`tests/fixtures/kite_instruments_csv.py`): BOM, line endings, blank and short rows,
    /// quoting, bad numbers, a missing column, invalid UTF-8. `extra_field` is the one
    /// documented gap — `csv.DictReader` keys the surplus cells by `None`, which a `KiteMap`
    /// cannot hold, so it is dropped (DECISIONS-RS R4.10).
    #[test]
    fn instrument_dumps_parse_like_the_sdk() {
        use serde_json::Value;
        let key: Value =
            serde_json::from_str(include_str!("../tests/fixtures/kite_instruments_csv.json"))
                .unwrap_or(Value::Null);
        let mut checked = 0;
        let mut failures: Vec<String> = Vec::new();
        for (name, case) in key.as_object().into_iter().flatten() {
            if name == "extra_field" {
                continue;
            }
            let body: Vec<u8> = case["body"]
                .as_str()
                .unwrap_or_default()
                .chars()
                .map(|c| u8::try_from(u32::from(c)).unwrap_or(b'?'))
                .collect();
            match (parse_instruments(&body), case.get("ok"), case.get("err")) {
                (Ok(rows), Some(Value::String(repr)), _) if rows.py_repr() == *repr => {}
                (Err(e), _, Some(Value::String(class))) if e.python_type() == class => {}
                (got, ok, err) => failures.push(format!("{name}: {got:?} vs {ok:?}/{err:?}")),
            }
            checked += 1;
        }
        assert!(checked > 20, "the answer key did not load ({checked})");
        assert!(failures.is_empty(), "{}", failures.join("\n"));
    }

    #[test]
    fn quote_plus_matches_requests() {
        assert_eq!(quote_plus("NSE:NIFTY 50"), "NSE%3ANIFTY+50");
        assert_eq!(quote_plus("NFO:M&M26OCTFUT"), "NFO%3AM%26M26OCTFUT");
        assert_eq!(quote_plus("BSE:A~B*C"), "BSE%3AA~B%2AC");
        assert_eq!(quote_plus("2026-09-22 09:15:00"), "2026-09-22+09%3A15%3A00");
    }

    #[test]
    fn bytes_repr_matches_python() {
        assert_eq!(repr_bytes(b"<html>nope"), "b'<html>nope'");
        assert_eq!(repr_bytes(b"it's"), "b\"it's\"");
        assert_eq!(repr_bytes(b"a\nb\x00"), "b'a\\nb\\x00'");
    }

    #[test]
    fn windows_are_contiguous() {
        let d = |y, m, day| NaiveDate::from_ymd_opt(y, m, day).unwrap_or_default();
        let w = windows(d(2020, 1, 1), d(2020, 12, 31), 100).unwrap_or_default();
        assert_eq!(w.len(), 4);
        assert_eq!(w[0], (d(2020, 1, 1), d(2020, 4, 9)));
    }
}
