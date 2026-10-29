//! `NseProvider` — the [`ReferenceProvider`] port over NSE's public files (Prompt 2 deliverable
//! 3). Ports `baskfy_providers.nse`.
//!
//! docs/02 §"Why Kite ... and why it is not sufficient alone" defines the job: Kite has no index
//! constituents, no index PE/PB/DivYield, no corporate-action calendar, no series codes for the
//! full universe and no listings history. All of that comes from here.
//!
//! docs/09 §"NSE specifics" defines the discipline:
//!
//! * public files need a browser-like session, so cookies are primed against the site root
//!   before any archive request;
//! * they are rate-sensitive, so every request passes through a token bucket;
//! * **fetch once, archive the raw file, parse from the archive.** Every method below routes
//!   through [`fetch_and_archive`], so the bytes that get parsed are always the bytes on record.
//!
//! What this module does *not* do is decide whether a day is usable. docs/09: "Files occasionally
//! publish late or malformed -> the QA gate, not the parser, decides whether the day is
//! publishable." So parsers raise on structural damage, and stay silent about business-level
//! oddities.
//!
//! **How Python's dynamic values are kept.** Python reads every CSV/JSON with polars and walks
//! the rows as Python objects (`str(value)`, `value or ""`, `Decimal(str(float))`). Those
//! conversions are the behaviour: a CSV cell `800.00` is inferred as a float and becomes
//! `Decimal("800.0")`, `0` is falsy. [`PyValue`] carries a cell as Python would see it, and the
//! helpers below (`py_str`, truthiness, `_decimal`, `_int`, `_date`) are Python's, case for case
//! (DECISIONS-RS R4.11).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::io::{Cursor, Read};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, LazyLock, Mutex};

use async_trait::async_trait;
use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime};
use percent_encoding::utf8_percent_encode;
use polars::prelude::{
    AnyValue, Column, CsvReadOptions, DataFrame, DataType, Expr, Int64Chunked, IntoColumn,
    IntoLazy, IntoSeries, JsonReader, NewChunkedArray, PlSmallStr, RoundMode, SerReader,
    StringChunked, StrptimeOptions, col, lit, when,
};
use regex::Regex;
use rust_decimal::{Decimal, RoundingStrategy};

use crate::archive::{PY_QUOTE, RawArchive, archive_key, fetch_and_archive};
use crate::clock::{Clock, SystemClock};
use crate::composite::SupplementaryReads;
use crate::errors::{ErrorKind, ProviderError};
use crate::net::GuardedClient;
use crate::ports::{
    CapabilitySet, HealthReporting, ProviderHealth, ReferenceProvider, reference_capabilities,
};
use crate::py::{repr_float, repr_str};
use crate::ratelimit::RateLimiter;
use crate::records::{
    CatalystFields, CatalystRecord, CorporateAction, CorporateActionFields, CorporateActionType,
    EarningsDateFields, EarningsDateRecord, EquityFundamental, EquityFundamentalFields,
    IndexSnapshot, IndexSnapshotFields, ListingFields, ListingRecord, PyDateTime, RecordError,
    bhavcopy_schema, conform, fo_bhavcopy_schema,
};
use crate::retry::{RetryHooks, RetryPolicy, call_with_retry};
use crate::settings::ProviderSettings;

/// `PROVIDER_NAME`.
pub const PROVIDER_NAME: &str = "nse";

/// Archive kind of the cash bhavcopy — the `{kind}` in docs/09's `nse/{kind}/{date}.csv`.
pub const KIND_BHAVCOPY: &str = "bhavcopy";
/// Archive kind of the F&O bhavcopy.
pub const KIND_FO_BHAVCOPY: &str = "fo-bhavcopy";
/// NSE switched the F&O bhavcopy to the UDiFF layout on this session; earlier days exist only in
/// the legacy `fo{DD}{MON}{YYYY}bhav.csv.zip` file under `content/historical/DERIVATIVES`.
pub const FO_UDIFF_SINCE: NaiveDate = match NaiveDate::from_ymd_opt(2024, 7, 8) {
    Some(d) => d,
    None => NaiveDate::MIN,
};
/// UDiFF's `FinInstrmTp` mapped onto the legacy `INSTRUMENT` vocabulary (`_FO_INSTRUMENT_TYPES`).
pub const FO_INSTRUMENT_TYPES: [(&str, &str); 4] = [
    ("STF", "FUTSTK"),
    ("STO", "OPTSTK"),
    ("IDF", "FUTIDX"),
    ("IDO", "OPTIDX"),
];
/// FO2: the F&O ban list (Track C §9). NSE publishes one file, always at the same URL, naming
/// the session it applies to in its first line; the archive keys it by *that* session.
pub const KIND_FO_BAN_LIST: &str = "fo-ban-list";
/// Where the ban list lives on the archive host.
pub const FO_BAN_LIST_PATH: &str = "/content/fo/fo_secban.csv";
/// Archive kind of `ind_close_all_DDMMYYYY.csv`.
pub const KIND_INDEX_SNAPSHOT: &str = "index-snapshot";
/// The un-windowed corporate-actions payloads already on disk (never written any more).
pub const KIND_CORPORATE_ACTIONS: &str = "corporate-actions";
/// The windowed request is a *different question* from the un-ranged one, so it gets its own
/// archive kind. `fetch_and_archive` never re-fetches a key that exists (docs/09, "Never
/// re-fetch to re-parse"), so reusing the old kind would have kept re-parsing the truncated
/// 20-row payloads already on disk and the fix would have been a no-op.
pub const KIND_CORPORATE_ACTIONS_WINDOWED: &str = "corporate-actions-windowed";
/// Rows NSE returns when it ignores the window and serves its default first page.
///
/// This exact number is the incident: twelve consecutive nightly payloads, every one of them 20
/// rows, while NSE held 87 actions for 2026-09-11 alone. See `gates/ca-truncation.md`.
pub const NSE_DEFAULT_PAGE_ROWS: usize = 20;
/// Archive kind of the main-board listings register.
pub const KIND_LISTINGS: &str = "listings";
/// Archive kind prefix of the constituent files.
pub const KIND_CONSTITUENTS: &str = "constituents";
/// Archive kind prefix of the per-symbol quotes.
pub const KIND_EQUITY_FUNDAMENTALS: &str = "equity-fundamentals";
/// Archive kind prefix of the per-symbol series lookups.
pub const KIND_EQUITY_META: &str = "equity-meta";

/// NSE retired `/api/quote-equity` when the site moved to Next.js; the quote page calls this
/// proxy instead, with a `functionName` naming the operation (DECISIONS-MERGE §T3F.1).
const GET_QUOTE_API: &str = "/api/NextApi/apiClient/GetQuoteApi";

/// Rs crore. docs/13 §2 finding 7: marketcap is an integer in this unit.
const CRORE: Decimal = Decimal::from_parts(10_000_000, 0, 0, false, 0);

/// SW11B: archive kind of the per-symbol corporate announcements.
pub const KIND_ANNOUNCEMENTS: &str = "announcements";
/// SW11B: archive kind of the per-symbol event calendar.
pub const KIND_EVENT_CALENDAR: &str = "event-calendar";
const ANNOUNCEMENTS_API: &str = "/api/corporate-announcements?index=equities&symbol=";
const EVENT_CALENDAR_API: &str = "/api/event-calendar?index=equities&symbol=";
/// Where a row links out to when the calendar entry has no attachment of its own.
const EVENT_CALENDAR_PAGE: &str = "/companies-listing/corporate-filings-event-calendar?symbol=";
/// `desc` and NSE's one-line summary make a headline; anything longer is the filing, which the
/// feed does not carry (A3: "link out, do not reproduce text").
pub const HEADLINE_MAX_CHARS: usize = 160;
/// `sw_catalyst.source` of an announcement.
pub const CATALYST_SOURCE_ANNOUNCEMENT: &str = "NSE_ANNOUNCEMENT";
/// `sw_catalyst.source` of an event-calendar row.
pub const CATALYST_SOURCE_EVENT_CALENDAR: &str = "NSE_EVENT_CALENDAR";

/// NSE serves its public files only to something that looks like a browser that has already
/// visited the site. These headers plus a primed cookie jar are the minimum that works.
pub const BROWSER_HEADERS: [(&str, &str); 4] = [
    (
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 \
         (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    ),
    (
        "Accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    ),
    ("Accept-Language", "en-GB,en;q=0.9"),
    ("Connection", "keep-alive"),
];

/// What httpx 0.28 advertises with no brotli/zstandard installed (the Python image has neither),
/// sent first, exactly as httpx orders its default headers.
pub const ACCEPT_ENCODING: &str = "gzip, deflate";

/// `index_def.slug` -> the name NSE publishes the index under.
pub const INDEX_SLUG_TO_NSE_NAME: [(&str, &str); 11] = [
    ("nifty-50", "NIFTY 50"),
    ("nifty-next-50", "NIFTY NEXT 50"),
    ("nifty-100", "NIFTY 100"),
    ("nifty-200", "NIFTY 200"),
    ("nifty-500", "NIFTY 500"),
    ("nifty-total-market", "NIFTY TOTAL MARKET"),
    ("nifty-large-mid-250", "NIFTY LARGEMIDCAP 250"),
    ("nifty-midcap-150", "NIFTY MIDCAP 150"),
    ("nifty-smallcap-250", "NIFTY SMALLCAP 250"),
    ("nifty-microcap-250", "NIFTY MICROCAP 250"),
    ("nifty-mid-small-400", "NIFTY MIDSMALLCAP 400"),
];

/// Slugs that are not published as an NSE constituent file at all; docs/06 §"Step 2" defines
/// them by rule (`DERIVED_UNIVERSES`).
pub const DERIVED_UNIVERSES: [&str; 4] = ["nifty-allcap", "etf", "nifty-fno", "nse-sme-emerge"];

/// The NSE Emerge (SME platform) series, as the SME register itself spells them.
pub const SME_SERIES: [&str; 3] = ["SM", "ST", "SZ"];

/// The Emerge register. NOT `/content/equities/SME_EQUITY_L.csv`, which has been frozen at a
/// single stale row for years. Its columns are underscore-separated and carry no `MARKET_LOT`.
pub const SME_LISTINGS_PATH: &str = "/emerge/corporates/content/SME_EQUITY_L.csv";
/// Archive kind of the Emerge register.
pub const KIND_SME_LISTINGS: &str = "sme-listings";
/// Archive kind of NSE's ETF security list (9 Sep 2026).
pub const KIND_ETF_LIST: &str = "etf-list";
/// NSE's ETF security list — the source the `etf` universe never had.
pub const ETF_LIST_PATH: &str = "/content/equities/eq_etfseclist.csv";

/// The exchange stamps announcements in IST, without an offset (`IST`).
pub const IST_OFFSET_SECONDS: i32 = 5 * 3600 + 30 * 60;

/// `IST` — `+05:30` (no DST, so a fixed offset is exact).
#[must_use]
pub fn ist() -> FixedOffset {
    use chrono::Offset as _;
    FixedOffset::east_opt(IST_OFFSET_SECONDS).unwrap_or_else(|| chrono::Utc.fix())
}

// ---------------------------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------------------------

fn unexpected(message: impl Into<String>) -> ProviderError {
    ProviderError::unexpected_payload(message).with_provider(PROVIDER_NAME)
}

fn upstream(message: impl Into<String>) -> ProviderError {
    ProviderError::upstream_unavailable(message).with_provider(PROVIDER_NAME)
}

fn unavailable(message: impl Into<String>) -> ProviderError {
    ProviderError::provider_unavailable(message).with_provider(PROVIDER_NAME)
}

/// A pydantic `ValidationError` raised while building a record.
pub(crate) fn validation(error: RecordError) -> ProviderError {
    ProviderError::foreign("ValidationError", error.to_string()).with_source(error)
}

fn polars_error(class: &'static str, error: &polars::error::PolarsError) -> ProviderError {
    ProviderError::foreign(class, error.to_string())
}

// ---------------------------------------------------------------------------------------------
// Python values
// ---------------------------------------------------------------------------------------------

/// One cell as Python sees it after `iter_rows(named=True)` or `json.loads`.
#[derive(Debug, Clone, PartialEq)]
pub enum PyValue {
    /// `None`.
    None,
    /// `bool`.
    Bool(bool),
    /// `int`.
    Int(i128),
    /// `float`.
    Float(f64),
    /// `str`.
    Str(String),
    /// `datetime.date`.
    Date(NaiveDate),
    /// `decimal.Decimal`.
    Decimal(Decimal),
    /// `list`.
    List(Vec<PyValue>),
    /// `dict` (insertion order).
    Dict(Vec<(String, PyValue)>),
}

static NONE: PyValue = PyValue::None;

impl PyValue {
    /// `bool(value)`.
    #[must_use]
    pub fn truthy(&self) -> bool {
        match self {
            Self::None => false,
            Self::Bool(b) => *b,
            Self::Int(i) => *i != 0,
            Self::Float(f) => *f != 0.0,
            Self::Str(s) => !s.is_empty(),
            Self::Date(_) => true,
            Self::Decimal(d) => !d.is_zero(),
            Self::List(v) => !v.is_empty(),
            Self::Dict(v) => !v.is_empty(),
        }
    }

    /// `str(value)`.
    #[must_use]
    pub fn py_str(&self) -> String {
        match self {
            Self::Str(s) => s.clone(),
            other => other.py_repr(),
        }
    }

    /// `repr(value)`.
    #[must_use]
    pub fn py_repr(&self) -> String {
        match self {
            Self::None => "None".to_owned(),
            Self::Bool(true) => "True".to_owned(),
            Self::Bool(false) => "False".to_owned(),
            Self::Int(i) => i.to_string(),
            Self::Float(f) => repr_float(*f),
            Self::Str(s) => repr_str(s),
            Self::Date(d) => format!("datetime.date({}, {}, {})", d.year(), d.month(), d.day()),
            Self::Decimal(d) => format!("Decimal('{d}')"),
            Self::List(items) => {
                let inner: Vec<String> = items.iter().map(Self::py_repr).collect();
                format!("[{}]", inner.join(", "))
            }
            Self::Dict(items) => {
                let inner: Vec<String> = items
                    .iter()
                    .map(|(k, v)| format!("{}: {}", repr_str(k), v.py_repr()))
                    .collect();
                format!("{{{}}}", inner.join(", "))
            }
        }
    }

    /// `type(value).__name__`.
    #[must_use]
    pub const fn type_name(&self) -> &'static str {
        match self {
            Self::None => "NoneType",
            Self::Bool(_) => "bool",
            Self::Int(_) => "int",
            Self::Float(_) => "float",
            Self::Str(_) => "str",
            Self::Date(_) => "date",
            Self::Decimal(_) => "Decimal",
            Self::List(_) => "list",
            Self::Dict(_) => "dict",
        }
    }

    /// A JSON value as `json.loads` gives it.
    #[must_use]
    pub fn from_json(value: &serde_json::Value) -> Self {
        match value {
            serde_json::Value::Null => Self::None,
            serde_json::Value::Bool(b) => Self::Bool(*b),
            serde_json::Value::Number(n) => n
                .as_i64()
                .map(|i| Self::Int(i128::from(i)))
                .or_else(|| n.as_u64().map(|u| Self::Int(i128::from(u))))
                .unwrap_or_else(|| Self::Float(n.as_f64().unwrap_or(f64::NAN))),
            serde_json::Value::String(s) => Self::Str(s.clone()),
            serde_json::Value::Array(items) => {
                Self::List(items.iter().map(Self::from_json).collect())
            }
            serde_json::Value::Object(map) => Self::Dict(
                map.iter()
                    .map(|(k, v)| (k.clone(), Self::from_json(v)))
                    .collect(),
            ),
        }
    }

    /// The value as it would be kept in a `dict[str, Any]` and serialised as JSON.
    #[must_use]
    pub fn to_json(&self) -> serde_json::Value {
        match self {
            Self::None => serde_json::Value::Null,
            Self::Bool(b) => serde_json::Value::Bool(*b),
            Self::Int(i) => i64::try_from(*i).map_or_else(
                |_| {
                    u64::try_from(*i).map_or(serde_json::Value::Null, |u| {
                        serde_json::Value::Number(u.into())
                    })
                },
                |v| serde_json::Value::Number(v.into()),
            ),
            Self::Float(f) => serde_json::Number::from_f64(*f)
                .map_or(serde_json::Value::Null, serde_json::Value::Number),
            Self::Str(s) => serde_json::Value::String(s.clone()),
            Self::Date(d) => serde_json::Value::String(d.format("%Y-%m-%d").to_string()),
            Self::Decimal(d) => serde_json::Value::String(d.to_string()),
            Self::List(items) => {
                serde_json::Value::Array(items.iter().map(Self::to_json).collect())
            }
            Self::Dict(items) => serde_json::Value::Object(
                items
                    .iter()
                    .map(|(k, v)| (k.clone(), v.to_json()))
                    .collect(),
            ),
        }
    }

    /// A polars cell as `iter_rows` gives it.
    #[must_use]
    pub fn from_any(value: &AnyValue<'_>) -> Self {
        match value {
            AnyValue::Null => Self::None,
            AnyValue::Boolean(b) => Self::Bool(*b),
            AnyValue::Int8(v) => Self::Int(i128::from(*v)),
            AnyValue::Int16(v) => Self::Int(i128::from(*v)),
            AnyValue::Int32(v) => Self::Int(i128::from(*v)),
            AnyValue::Int64(v) => Self::Int(i128::from(*v)),
            AnyValue::UInt8(v) => Self::Int(i128::from(*v)),
            AnyValue::UInt16(v) => Self::Int(i128::from(*v)),
            AnyValue::UInt32(v) => Self::Int(i128::from(*v)),
            AnyValue::UInt64(v) => Self::Int(i128::from(*v)),
            AnyValue::Float32(v) => Self::Float(f64::from(*v)),
            AnyValue::Float64(v) => Self::Float(*v),
            AnyValue::String(s) => Self::Str((*s).to_owned()),
            AnyValue::StringOwned(s) => Self::Str(s.to_string()),
            AnyValue::Date(days) => {
                NaiveDate::from_num_days_from_ce_opt(days + 719_163).map_or(Self::None, Self::Date)
            }
            AnyValue::Decimal(v, _, scale) => u32::try_from(*scale)
                .ok()
                .and_then(|s| Decimal::try_from_i128_with_scale(*v, s).ok())
                .map_or_else(|| Self::Str(value.to_string()), Self::Decimal),
            AnyValue::List(series) => {
                Self::List(series.iter().map(|av| Self::from_any(&av)).collect())
            }
            AnyValue::StructOwned(payload) => {
                let (values, fields) = &**payload;
                Self::Dict(
                    fields
                        .iter()
                        .zip(values.iter())
                        .map(|(f, v)| (f.name().to_string(), Self::from_any(v)))
                        .collect(),
                )
            }
            AnyValue::Struct(..) => Self::from_any(&value.clone().into_static()),
            other => Self::Str(other.to_string()),
        }
    }
}

/// One row as a `dict` (`iter_rows(named=True)` or a JSON object).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Row {
    items: Vec<(String, PyValue)>,
}

impl Row {
    /// A row from its key/value pairs, in order.
    #[must_use]
    pub const fn new(items: Vec<(String, PyValue)>) -> Self {
        Self { items }
    }

    /// `key in row`.
    #[must_use]
    pub fn contains(&self, key: &str) -> bool {
        self.items.iter().any(|(k, _)| k == key)
    }

    /// `row.get(key)` (`None` when absent).
    #[must_use]
    pub fn get(&self, key: &str) -> &PyValue {
        self.items
            .iter()
            .find(|(k, _)| k == key)
            .map_or(&NONE, |(_, v)| v)
    }

    /// `dict(row)` as JSON.
    #[must_use]
    pub fn to_json_map(&self) -> serde_json::Map<String, serde_json::Value> {
        self.items
            .iter()
            .map(|(k, v)| (k.clone(), v.to_json()))
            .collect()
    }

    /// `_first`: the value of the first candidate key the row has, else `None`.
    #[must_use]
    pub fn first(&self, candidates: &[&str]) -> &PyValue {
        candidates
            .iter()
            .find(|c| self.contains(c))
            .map_or(&NONE, |c| self.get(c))
    }
}

/// `frame.iter_rows(named=True)`.
///
/// # Errors
/// polars' own error if a cell cannot be read.
pub fn frame_rows(frame: &DataFrame) -> Result<Vec<Row>, ProviderError> {
    let names: Vec<String> = frame
        .get_column_names()
        .iter()
        .map(|n| n.to_string())
        .collect();
    let columns = frame.columns();
    let mut rows = Vec::with_capacity(frame.height());
    for i in 0..frame.height() {
        let mut items = Vec::with_capacity(columns.len());
        for (name, column) in names.iter().zip(columns) {
            let value = column
                .get(i)
                .map_err(|e| polars_error("ComputeError", &e))?;
            items.push((name.clone(), PyValue::from_any(&value)));
        }
        rows.push(Row::new(items));
    }
    Ok(rows)
}

/// `str.isspace` for one character: Unicode White_Space plus the four separators
/// `\x1c`-`\x1f` Python also counts.
#[must_use]
pub fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()`.
#[must_use]
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// `str.split()` (no separator): runs of whitespace, empty pieces dropped.
#[must_use]
pub fn py_split_whitespace(s: &str) -> Vec<&str> {
    s.split(py_isspace).filter(|p| !p.is_empty()).collect()
}

/// `str.splitlines()`.
#[must_use]
pub fn py_splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let boundary = matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if boundary {
            out.push(&s[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r'
                && let Some(&(j, '\n')) = chars.peek()
            {
                chars.next();
                end = j + 1;
            }
            start = end;
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// `urllib.parse.quote(text)`.
#[must_use]
pub fn py_quote(text: &str) -> String {
    utf8_percent_encode(text, PY_QUOTE).to_string()
}

/// `json.loads(payload)` for bytes: a UTF-8 BOM is accepted, as Python's encoding detection does.
fn json_loads(payload: &[u8]) -> Result<serde_json::Value, serde_json::Error> {
    let body = payload.strip_prefix(b"\xef\xbb\xbf").unwrap_or(payload);
    serde_json::from_slice(body)
}

// ---------------------------------------------------------------------------------------------
// Python's conversions
// ---------------------------------------------------------------------------------------------

/// What `Decimal(text)` makes of a string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PyDecimal {
    /// A finite value.
    Finite(Decimal),
    /// `NaN`, `sNaN` or an infinity — valid to Python, refused by every consumer here.
    NonFinite,
    /// Finite, but beyond `rust_decimal`'s 28 digits (a DECISIONS-RS R1.3-class limit).
    TooWide,
    /// `InvalidOperation`.
    Invalid,
}

/// `Decimal(text)`: Python's numeric-string grammar — sign, digits, an optional fraction and
/// exponent, or `Inf`/`Infinity`/`NaN`/`sNaN`, with surrounding whitespace allowed. CPython's C
/// `decimal` drops every `_` before parsing (`Decimal("_1")` and `Decimal("1__0")` are valid),
/// so the port does too. A negative zero keeps its sign, as Python's `Decimal("-0")` does.
#[must_use]
pub fn py_decimal(text: &str) -> PyDecimal {
    let t: String = py_strip(text).chars().filter(|c| *c != '_').collect();
    let (negative, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t.as_str()),
    };
    let lower = body.to_ascii_lowercase();
    let payload_digits = |rest: &str| rest.bytes().all(|b| b.is_ascii_digit());
    if matches!(lower.as_str(), "inf" | "infinity")
        || lower.strip_prefix("nan").is_some_and(payload_digits)
        || lower.strip_prefix("snan").is_some_and(payload_digits)
    {
        return PyDecimal::NonFinite;
    }
    let (mantissa, exponent) = match body.find(['e', 'E']) {
        Some(i) => (&body[..i], Some(&body[i + 1..])),
        None => (body, None),
    };
    let (int_part, frac_part) = match mantissa.find('.') {
        Some(i) => (&mantissa[..i], &mantissa[i + 1..]),
        None => (mantissa, ""),
    };
    let ascii_digits = |s: &str| s.bytes().all(|b| b.is_ascii_digit());
    if !ascii_digits(int_part) || !ascii_digits(frac_part) {
        return PyDecimal::Invalid;
    }
    if int_part.is_empty() && frac_part.is_empty() {
        return PyDecimal::Invalid;
    }
    let exp: i64 = match exponent {
        None => 0,
        Some(e) => {
            let (eneg, edigits) = match e.as_bytes().first() {
                Some(b'-') => (true, &e[1..]),
                Some(b'+') => (false, &e[1..]),
                _ => (false, e),
            };
            if edigits.is_empty() || !ascii_digits(edigits) {
                return PyDecimal::Invalid;
            }
            let Ok(v) = edigits.parse::<i64>() else {
                return PyDecimal::TooWide;
            };
            if eneg { -v } else { v }
        }
    };
    let Ok(frac_len) = i64::try_from(frac_part.len()) else {
        return PyDecimal::TooWide;
    };
    // value = coefficient * 10^(exp - frac_len)
    let mut power = exp - frac_len;
    let coefficient = format!("{int_part}{frac_part}");
    let trimmed = coefficient.trim_start_matches('0');
    let mut digits = if trimmed.is_empty() {
        "0".to_owned()
    } else {
        trimmed.to_owned()
    };
    if power > 0 {
        if digits != "0" {
            let Ok(p) = usize::try_from(power) else {
                return PyDecimal::TooWide;
            };
            if digits.len() + p > 29 {
                return PyDecimal::TooWide;
            }
            digits.push_str(&"0".repeat(p));
        }
        power = 0;
    }
    let Ok(scale) = u32::try_from(-power) else {
        return PyDecimal::TooWide;
    };
    if scale > 28 || digits.len() > 29 {
        return PyDecimal::TooWide;
    }
    let Ok(m) = digits.parse::<i128>() else {
        return PyDecimal::TooWide;
    };
    match Decimal::try_from_i128_with_scale(if negative { -m } else { m }, scale) {
        Ok(mut d) => {
            if negative && m == 0 {
                d.set_sign_negative(true);
            }
            PyDecimal::Finite(d)
        }
        Err(_) => PyDecimal::TooWide,
    }
}

/// `_clean`: `str(value).strip()`, `None` for `None` or an empty result.
#[must_use]
pub fn clean(value: &PyValue) -> Option<String> {
    if matches!(value, PyValue::None) {
        return None;
    }
    let text = value.py_str();
    let stripped = py_strip(&text);
    if stripped.is_empty() {
        None
    } else {
        Some(stripped.to_owned())
    }
}

/// `_decimal`: `Decimal(text.replace(",", ""))`, `None` for blanks, `-`, `NA`, `N/A`, `nan`
/// and anything `Decimal` refuses.
///
/// # Errors
/// A non-finite value (`NaN`, `Infinity`): Python builds it, and every consumer here then
/// raises (pydantic refuses it, polars panics on it); the port raises at once. A finite value
/// beyond 28 digits is the R1.3-class limit.
pub fn decimal(value: &PyValue) -> Result<Option<Decimal>, ProviderError> {
    let Some(text) = clean(value) else {
        return Ok(None);
    };
    if matches!(text.as_str(), "-" | "NA" | "N/A" | "nan") {
        return Ok(None);
    }
    match py_decimal(&text.replace(',', "")) {
        PyDecimal::Finite(d) => Ok(Some(d)),
        PyDecimal::Invalid => Ok(None),
        PyDecimal::NonFinite => Err(ProviderError::foreign(
            "ValidationError",
            format!("Input should be a finite number: {}", repr_str(&text)),
        )),
        PyDecimal::TooWide => Err(ProviderError::foreign(
            "OverflowError",
            format!(
                "{} is beyond the 28 digits a rust_decimal holds (DECISIONS-RS R1.3)",
                repr_str(&text)
            ),
        )),
    }
}

/// Python's `float(text)`: `None` where `float()` raises `ValueError`.
fn py_float(text: &str) -> Option<f64> {
    let t = py_strip(text);
    let cleaned: String = if t.contains('_') {
        let bytes = t.as_bytes();
        let mut out = String::with_capacity(t.len());
        for (i, &b) in bytes.iter().enumerate() {
            if b == b'_' {
                let ok = i > 0
                    && bytes.get(i - 1).is_some_and(u8::is_ascii_digit)
                    && bytes.get(i + 1).is_some_and(u8::is_ascii_digit);
                if !ok {
                    return None;
                }
            } else {
                out.push(char::from(b));
            }
        }
        out
    } else {
        t.to_owned()
    };
    let lower = cleaned.to_ascii_lowercase();
    let unsigned = lower.trim_start_matches(['+', '-']);
    if unsigned.starts_with("nan") && unsigned != "nan" {
        return None;
    }
    if unsigned.starts_with("inf") && unsigned != "inf" && unsigned != "infinity" {
        return None;
    }
    cleaned.parse::<f64>().ok()
}

/// `_int`: `int(float(text.replace(",", "")))`, `None` where that raises `ValueError` (a NaN
/// included).
///
/// # Errors
/// `OverflowError` for an infinity (Python's `int(inf)`) or a value beyond `i64`.
pub fn int(value: &PyValue) -> Result<Option<i64>, ProviderError> {
    let Some(text) = clean(value) else {
        return Ok(None);
    };
    let Some(f) = py_float(&text.replace(',', "")) else {
        return Ok(None);
    };
    if f.is_nan() {
        return Ok(None);
    }
    if f.is_infinite() {
        return Err(ProviderError::foreign(
            "OverflowError",
            "cannot convert float infinity to integer",
        ));
    }
    let truncated = f.trunc();
    // i64::MAX as f64 rounds up to 2^63, which is out of range; the check is exclusive.
    #[allow(clippy::cast_precision_loss)]
    let limit = i64::MAX as f64;
    if truncated >= limit || truncated < -limit {
        return Err(ProviderError::foreign(
            "OverflowError",
            format!("{} does not fit a 64-bit integer", repr_float(f)),
        ));
    }
    #[allow(clippy::cast_possible_truncation)]
    Ok(Some(truncated as i64))
}

/// One `strptime` format, compiled the way `_strptime` compiles it.
struct Strptime {
    regex: Regex,
}

const MONTHS: [&str; 12] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
];

impl Strptime {
    fn compile(format: &str) -> Result<Self, regex::Error> {
        let mut pattern = String::from("(?i)^");
        let mut chars = format.chars();
        while let Some(c) = chars.next() {
            if c == '%' {
                let directive = chars.next().unwrap_or('%');
                pattern.push_str(match directive {
                    'd' => r"(?P<d>3[01]|[12]\d|0[1-9]|[1-9]| [1-9])",
                    'm' => r"(?P<m>1[0-2]|0[1-9]|[1-9])",
                    'Y' => r"(?P<Y>\d\d\d\d)",
                    'y' => r"(?P<y>\d\d)",
                    'b' => r"(?P<b>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                    'H' => r"(?P<H>2[0-3]|[0-1]\d|\d)",
                    'M' => r"(?P<M>[0-5]\d|\d)",
                    'S' => r"(?P<S>6[0-1]|[0-5]\d|\d)",
                    _ => "%",
                });
            } else if py_isspace(c) {
                pattern.push_str(r"[\s\x1c-\x1f]+");
            } else {
                pattern.push_str(&regex::escape(&c.to_string()));
            }
        }
        Ok(Self {
            regex: Regex::new(&pattern)?,
        })
    }

    /// `datetime.strptime(text, format)`, or `None` where it raises `ValueError`.
    fn parse(&self, text: &str) -> Option<NaiveDateTime> {
        let caps = self.regex.captures(text)?;
        if caps.get(0)?.end() != text.len() {
            return None; // "unconverted data remains"
        }
        let num = |name: &str| -> Option<Option<u32>> {
            match caps.name(name) {
                None => Some(None),
                Some(m) => {
                    let digits = m.as_str().trim_start();
                    if !digits.bytes().all(|b| b.is_ascii_digit()) {
                        return None;
                    }
                    digits.parse::<u32>().ok().map(Some)
                }
            }
        };
        let year = match (num("Y")?, num("y")?) {
            (Some(y), _) => i32::try_from(y).ok()?,
            (None, Some(y)) => {
                let y = i32::try_from(y).ok()?;
                if y <= 68 { 2000 + y } else { 1900 + y }
            }
            (None, None) => 1900,
        };
        let month = match (num("m")?, caps.name("b")) {
            (Some(m), _) => m,
            (None, Some(b)) => {
                let lower = b.as_str().to_lowercase();
                let index = MONTHS.iter().position(|m| *m == lower)?;
                u32::try_from(index).ok()? + 1
            }
            (None, None) => 1,
        };
        let day = num("d")?.unwrap_or(1);
        let date = NaiveDate::from_ymd_opt(year, month, day)?;
        // `%S` matches 60 and 61 (`_strptime`'s leap-second allowance), but `datetime(...)` then
        // raises "second must be in 0..59", so `datetime.strptime` refuses them; `from_hms_opt`
        // returns `None` for a second of 60 or more, which is the same refusal.
        let time = NaiveTime::from_hms_opt(
            num("H")?.unwrap_or(0),
            num("M")?.unwrap_or(0),
            num("S")?.unwrap_or(0),
        )?;
        Some(NaiveDateTime::new(date, time))
    }
}

type Compiled = LazyLock<Result<Vec<Strptime>, regex::Error>>;

/// `_DATE_FORMATS`.
static DATE_FORMATS: Compiled = LazyLock::new(|| {
    ["%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%y"]
        .iter()
        .map(|f| Strptime::compile(f))
        .collect()
});

/// `_STAMP_FORMATS`.
static STAMP_FORMATS: Compiled = LazyLock::new(|| {
    ["%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y"]
        .iter()
        .map(|f| Strptime::compile(f))
        .collect()
});

fn formats(cell: &'static Compiled) -> Result<&'static [Strptime], ProviderError> {
    cell.as_ref()
        .map(Vec::as_slice)
        .map_err(|e| ProviderError::foreign("re.error", e.to_string()))
}

/// `_date`: a date, from a date or the first of `_DATE_FORMATS` the text parses with.
///
/// # Errors
/// Only if a format fails to compile (never, for these literals).
pub fn date(value: &PyValue) -> Result<Option<NaiveDate>, ProviderError> {
    if let PyValue::Date(d) = value {
        return Ok(Some(*d));
    }
    let Some(text) = clean(value) else {
        return Ok(None);
    };
    Ok(formats(&DATE_FORMATS)?
        .iter()
        .find_map(|f| f.parse(&text))
        .map(|dt| dt.date()))
}

/// `_stamp`: an IST-stamped datetime from the first of `_STAMP_FORMATS` that parses.
///
/// # Errors
/// Only if a format fails to compile (never, for these literals).
pub fn stamp(value: &PyValue) -> Result<Option<DateTime<FixedOffset>>, ProviderError> {
    let Some(text) = clean(value) else {
        return Ok(None);
    };
    let offset = ist();
    Ok(formats(&STAMP_FORMATS)?
        .iter()
        .find_map(|f| f.parse(&text))
        .and_then(|naive| naive.and_local_timezone(offset).single()))
}

fn regex_of(
    cell: &'static LazyLock<Result<Regex, regex::Error>>,
) -> Result<&'static Regex, ProviderError> {
    cell.as_ref()
        .map_err(|e| ProviderError::foreign("re.error", e.to_string()))
}

/// Python's `\s` in a `str` pattern (Unicode White_Space plus `\x1c`-`\x1f`).
macro_rules! ws {
    () => {
        r"[\s\x1c-\x1f]"
    };
}

static FO_BAN_HEADER: LazyLock<Result<Regex, regex::Error>> = LazyLock::new(|| {
    Regex::new(concat!(
        r"(?i)^Securities",
        ws!(),
        r"+in",
        ws!(),
        r"+Ban",
        ws!(),
        r"+For",
        ws!(),
        r"+Trade",
        ws!(),
        r"+Date",
        ws!(),
        r"+(\d{1,2}-[A-Za-z]{3}-\d{4})",
        ws!(),
        r"*:?(.*)$"
    ))
});
static FO_BAN_ROW: LazyLock<Result<Regex, regex::Error>> = LazyLock::new(|| {
    Regex::new(concat!(
        r"^",
        ws!(),
        r"*\d+",
        ws!(),
        r"*,",
        ws!(),
        r"*([A-Za-z0-9&\-_.]+)",
        ws!(),
        r"*,?",
        ws!(),
        r"*$"
    ))
});
static RESULTS_PURPOSE: LazyLock<Result<Regex, regex::Error>> =
    LazyLock::new(|| Regex::new(r"(?i)\bresults?\b"));
static SPLIT_FACES: LazyLock<Result<Regex, regex::Error>> = LazyLock::new(|| {
    Regex::new(concat!(
        r"(?:RS\.?|RE\.?)",
        ws!(),
        r"*([\d.]+)\D+(?:RS\.?|RE\.?)",
        ws!(),
        r"*([\d.]+)"
    ))
});
static RUPEE_AMOUNT: LazyLock<Result<Regex, regex::Error>> =
    LazyLock::new(|| Regex::new(concat!(r"(?:RS\.?|RE\.?)", ws!(), r"*([\d.]+)")));
static RATIO_PAIR: LazyLock<Result<Regex, regex::Error>> =
    LazyLock::new(|| Regex::new(concat!(r"(\d+)", ws!(), r"*:", ws!(), r"*(\d+)")));
static LEG_SEPARATOR: LazyLock<Result<Regex, regex::Error>> =
    LazyLock::new(|| Regex::new(concat!(ws!(), r"+(?:AND|&)", ws!(), r"+")));

// ---------------------------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------------------------

/// What a GET answered (`HttpResponseLike`): the status and the decoded body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    /// `status_code`.
    pub status_code: u16,
    /// `content` — decompressed, as httpx's is.
    pub content: Vec<u8>,
}

/// A transport failure (connection reset, timeout, a refused address).
pub type HttpFailure = Box<dyn std::error::Error + Send + Sync + 'static>;

/// The slice of `httpx.Client` this adapter uses (`HttpClientLike`).
///
/// A trait, not the concrete client, so the tests drive every parser and the archive-first
/// ordering with zero network calls (Prompt 2 acceptance criterion 1).
#[async_trait]
pub trait HttpClientLike: Send + Sync {
    /// `client.get(url)`: one GET, cookies carried by the client.
    ///
    /// # Errors
    /// Any transport failure.
    async fn get(&self, url: &str) -> Result<HttpResponse, HttpFailure>;
}

/// The browser-shaped client [`build_http_client`] returns: httpx's request, byte for byte on
/// HTTP/1.1 (`Host`, then `Accept-Encoding`, the four [`BROWSER_HEADERS`] in order, then the
/// jar's `Cookie`), with a cookie jar that persists across requests and redirects followed.
#[derive(Debug, Clone)]
pub struct BrowserHttpClient {
    inner: GuardedClient,
}

#[async_trait]
impl HttpClientLike for BrowserHttpClient {
    async fn get(&self, url: &str) -> Result<HttpResponse, HttpFailure> {
        let parsed = reqwest::Url::parse(url)?;
        let host = match (parsed.host_str(), parsed.port()) {
            (Some(h), Some(p)) => format!("{h}:{p}"),
            (Some(h), None) => h.to_owned(),
            (None, _) => return Err(format!("no host in {url}").into()),
        };
        // Every header on the request itself, in httpx's order: reqwest fills its defaults (which
        // begin with its own `Accept`) only into vacant slots, and the jar appends `Cookie` last.
        let mut request = self
            .inner
            .get(url)?
            .header(reqwest::header::HOST, host)
            .header(reqwest::header::ACCEPT_ENCODING, ACCEPT_ENCODING);
        for (name, value) in BROWSER_HEADERS {
            request = request.header(name, value);
        }
        let response = request.send().await?;
        let status_code = response.status().as_u16();
        let content = response.bytes().await?.to_vec();
        Ok(HttpResponse {
            status_code,
            content,
        })
    }
}

/// The cookie jar, in `http.cookiejar`'s send order: longest cookie path first, then the order
/// the cookies were first set (a reset keeps its place, as a dict assignment does). reqwest's
/// own [`reqwest::cookie::Jar`] keeps the cookies — domain, path, expiry, `Secure` — and this
/// only reorders the `Cookie` header it produces.
#[derive(Debug, Default)]
pub struct CookieJar {
    jar: reqwest::cookie::Jar,
    /// `(name, path length)`, in first-set order.
    seen: Mutex<Vec<(String, usize)>>,
}

impl CookieJar {
    fn remember(&self, header: &str, url: &reqwest::Url) {
        let mut parts = header.split(';');
        let name = parts
            .next()
            .and_then(|pair| pair.split('=').next())
            .map(str::trim)
            .unwrap_or_default();
        if name.is_empty() {
            return;
        }
        let path = parts
            .filter_map(|attr| {
                let (k, v) = attr.split_once('=')?;
                k.trim()
                    .eq_ignore_ascii_case("path")
                    .then(|| v.trim().to_owned())
            })
            .next_back()
            .filter(|p| p.starts_with('/'))
            .unwrap_or_else(|| {
                // RFC 6265's default-path, as cookiejar computes it: the request path up to its
                // last `/`, or `/`.
                let request = url.path();
                match request.rfind('/') {
                    Some(0) | None => "/".to_owned(),
                    Some(i) => request[..i].to_owned(),
                }
            });
        let mut seen = self
            .seen
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        match seen.iter_mut().find(|(n, _)| n == name) {
            Some(entry) => entry.1 = path.len(),
            None => seen.push((name.to_owned(), path.len())),
        }
    }
}

impl reqwest::cookie::CookieStore for CookieJar {
    fn set_cookies(
        &self,
        cookie_headers: &mut dyn Iterator<Item = &reqwest::header::HeaderValue>,
        url: &reqwest::Url,
    ) {
        let headers: Vec<&reqwest::header::HeaderValue> = cookie_headers.collect();
        for header in &headers {
            if let Ok(text) = header.to_str() {
                self.remember(text, url);
            }
        }
        self.jar.set_cookies(&mut headers.into_iter(), url);
    }

    fn cookies(&self, url: &reqwest::Url) -> Option<reqwest::header::HeaderValue> {
        let value = self.jar.cookies(url)?;
        let text = value.to_str().ok()?;
        let seen = self
            .seen
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let rank = |pair: &str| -> (std::cmp::Reverse<usize>, usize) {
            let name = pair.split('=').next().unwrap_or_default();
            seen.iter()
                .position(|(n, _)| n == name)
                .and_then(|i| seen.get(i).map(|(_, len)| (std::cmp::Reverse(*len), i)))
                .unwrap_or((std::cmp::Reverse(0), usize::MAX))
        };
        let mut pairs: Vec<&str> = text.split("; ").collect();
        pairs.sort_by_key(|p| rank(p));
        reqwest::header::HeaderValue::from_str(&pairs.join("; ")).ok()
    }
}

/// `build_http_client`: a browser-shaped client with a cookie jar that persists across
/// requests — httpx's `Client(headers=BROWSER_HEADERS, timeout=..., follow_redirects=True)`.
///
/// httpx's timeout bounds each phase (connect, read, write, pool), not the whole request, so the
/// port sets the connect and read timeouts, not reqwest's total one. httpx speaks HTTP/1.1 only.
///
/// # Errors
/// `UpstreamUnavailable` when the TLS backend cannot initialise.
pub fn build_http_client(
    settings: &ProviderSettings,
) -> Result<Arc<dyn HttpClientLike>, ProviderError> {
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        reqwest::header::ACCEPT_ENCODING,
        reqwest::header::HeaderValue::from_static(ACCEPT_ENCODING),
    );
    for (name, value) in BROWSER_HEADERS {
        let name = reqwest::header::HeaderName::from_static(lower_static(name));
        headers.insert(name, reqwest::header::HeaderValue::from_static(value));
    }
    let timeout = crate::clock::duration(settings.nse_request_timeout_seconds);
    let builder = crate::net::client_builder()
        .default_headers(headers)
        .cookie_provider(Arc::new(CookieJar::default()))
        .connect_timeout(timeout)
        .read_timeout(timeout)
        .http1_only()
        .http1_title_case_headers();
    Ok(Arc::new(BrowserHttpClient {
        inner: GuardedClient::from_builder(builder)?,
    }))
}

/// The lower-case spelling `HeaderName::from_static` requires, for the four browser headers.
const fn lower_static(name: &str) -> &'static str {
    match name.as_bytes() {
        b"User-Agent" => "user-agent",
        b"Accept" => "accept",
        b"Accept-Language" => "accept-language",
        _ => "connection",
    }
}

/// `_get`: a transport failure becomes `UpstreamUnavailable`, so the retry policy sees it.
async fn http_get(client: &dyn HttpClientLike, url: &str) -> Result<HttpResponse, ProviderError> {
    client
        .get(url)
        .await
        .map_err(|e| upstream(format!("GET {url} failed: {e}")))
}

/// `_body`: 429 and 5xx are transient, any other 4xx is a payload error.
fn http_body(response: HttpResponse, url: &str) -> Result<Vec<u8>, ProviderError> {
    let status = response.status_code;
    if status == 429 {
        return Err(upstream(format!("NSE rate-limited GET {url}")));
    }
    if status >= 500 {
        return Err(upstream(format!("GET {url} returned {status}")));
    }
    if status >= 400 {
        return Err(unexpected(format!("GET {url} returned {status}")));
    }
    Ok(response.content)
}

// ---------------------------------------------------------------------------------------------
// The provider
// ---------------------------------------------------------------------------------------------

/// The collaborators [`NseProvider`] is wired with (`NSERuntime`).
#[derive(Clone, Default)]
pub struct NseRuntime {
    /// The HTTP client; `None` leaves the provider unavailable.
    pub client: Option<Arc<dyn HttpClientLike>>,
    /// The shared token bucket; `None` refuses every request.
    pub rate_limiter: Option<Arc<dyn RateLimiter>>,
    /// The retry policy; `None` builds one from the settings.
    pub retry_policy: Option<RetryPolicy>,
    /// The retry seams.
    pub retry_hooks: Option<RetryHooks>,
    /// The wall clock behind `date.today()` (the listings registers are archived by fetch day);
    /// `None` is the system clock.
    pub clock: Option<Arc<dyn Clock>>,
}

impl std::fmt::Debug for NseRuntime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NseRuntime")
            .field("client", &self.client.is_some())
            .field("rate_limiter", &self.rate_limiter.is_some())
            .field("retry_policy", &self.retry_policy)
            .finish_non_exhaustive()
    }
}

struct Inner {
    settings: ProviderSettings,
    archive: Option<Arc<dyn RawArchive>>,
    client: Option<Arc<dyn HttpClientLike>>,
    rate_limiter: Option<Arc<dyn RateLimiter>>,
    retry_policy: RetryPolicy,
    retry_hooks: Option<RetryHooks>,
    clock: Arc<dyn Clock>,
    cookies_primed: AtomicBool,
    /// The cookie jar is primed once and shared; with concurrent symbol reads neither the first
    /// prime nor the 401/403 re-prime may interleave.
    cookie_lock: tokio::sync::Mutex<()>,
}

/// [`ReferenceProvider`] backed by NSE's public files (`NSEProvider`). Cheap to clone: clones
/// share one cookie jar, one primed flag and one limiter.
#[derive(Clone)]
pub struct NseProvider {
    inner: Arc<Inner>,
}

impl std::fmt::Debug for NseProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NseProvider").finish_non_exhaustive()
    }
}

type ParsedAction = (
    CorporateActionType,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
);

impl NseProvider {
    /// `NSEProvider(settings, archive, runtime)`.
    ///
    /// # Errors
    /// `ValueError` when the settings' retry fields do not make a valid policy.
    pub fn new(
        settings: ProviderSettings,
        archive: Option<Arc<dyn RawArchive>>,
        runtime: NseRuntime,
    ) -> Result<Self, ProviderError> {
        let retry_policy = match runtime.retry_policy {
            Some(p) => p,
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
            inner: Arc::new(Inner {
                settings,
                archive,
                client: runtime.client,
                rate_limiter: runtime.rate_limiter,
                retry_policy,
                retry_hooks: runtime.retry_hooks,
                clock: runtime.clock.unwrap_or_else(|| Arc::new(SystemClock)),
                cookies_primed: AtomicBool::new(false),
                cookie_lock: tokio::sync::Mutex::new(()),
            }),
        })
    }

    /// The settings it was built with.
    #[must_use]
    pub fn settings(&self) -> &ProviderSettings {
        &self.inner.settings
    }

    /// `_throttle`: take one token from the limiter. AF 3.11: a missing limiter is a hard
    /// refuse, not a silent no-op — an unthrottled NSE client is how the §17 ~600-request stall
    /// happens.
    ///
    /// # Errors
    /// `ProviderUnavailable` with no limiter; the limiter's own failure.
    pub async fn throttle(&self) -> Result<(), ProviderError> {
        self.inner.throttle().await
    }

    /// Symbols in `index_slug` on `on`, from the archived constituent file.
    ///
    /// # Errors
    /// `UnexpectedPayload` for a derived, unknown or unmapped universe or a file without a
    /// symbol column; fetch and archive failures.
    pub async fn index_constituents(
        &self,
        index_slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        self.inner.index_constituents(index_slug, on).await
    }

    /// Index level, PE, PB and dividend yield for every published index (docs/01 §7).
    ///
    /// # Errors
    /// `UnexpectedPayload` for a file dated for another session or without a name column.
    pub async fn index_snapshots(
        &self,
        on: NaiveDate,
    ) -> Result<Vec<IndexSnapshot>, ProviderError> {
        self.inner.index_snapshots(on).await
    }

    /// Splits, bonuses, dividends and rights with an ex-date on or after `since`.
    ///
    /// **The window is not optional.** Asked without `from_date`/`to_date`, NSE answers with a
    /// default first page of 20 rows and no indication that it has done so. The window runs
    /// forward from `since` rather than stopping at today, because NSE announces an ex-date ahead
    /// of time; `apply_adjustments` bounds itself to `ex_date <= as_of` (docs/DECISIONS.md
    /// §21.9), so a future-dated row sits inert until its ex-date arrives. Deriving the far edge
    /// from `since` and not the clock keeps one archived payload answering exactly one question.
    ///
    /// # Errors
    /// `UnexpectedPayload` for a default page over a long window (`gates/ca-truncation.md`).
    pub async fn corporate_actions(
        &self,
        since: NaiveDate,
    ) -> Result<Vec<CorporateAction>, ProviderError> {
        self.inner.corporate_actions(since).await
    }

    /// The NSE main-board listings register (docs/01 §1). Main board only; the Emerge (SME)
    /// platform has a register of its own — [`NseProvider::sme_listings`].
    ///
    /// # Errors
    /// Fetch, archive and parse failures.
    pub async fn listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        self.inner
            .listings_from(KIND_LISTINGS, "/content/equities/EQUITY_L.csv", "listings")
            .await
    }

    /// The NSE Emerge (SME) register — the other half of the listing universe. Separate so an
    /// Emerge outage is a fact the pipeline step records, not an exception this layer swallows.
    ///
    /// # Errors
    /// Fetch, archive and parse failures.
    pub async fn sme_listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        self.inner
            .listings_from(KIND_SME_LISTINGS, SME_LISTINGS_PATH, "sme listings")
            .await
    }

    /// Every ETF listed on NSE — the `etf` universe's membership (9 Sep 2026), sorted and
    /// de-duplicated. Sourcing the membership leaves every other universe exactly as it is,
    /// where reclassifying the instruments would silently shrink `nifty-allcap`.
    ///
    /// # Errors
    /// `UnexpectedPayload` (no provider) when the list has no symbol column.
    pub async fn etf_symbols(&self) -> Result<Vec<String>, ProviderError> {
        self.inner.etf_symbols().await
    }

    /// One trading day's bhavcopy, including series and circuit bands (docs/09).
    ///
    /// # Errors
    /// `UnexpectedPayload` for an unreadable zip, CSV, or a file with no readable rows.
    pub async fn bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        self.inner.bhavcopy(on).await
    }

    /// One trading day's F&O bhavcopy: every NFO contract's OHLC, settle and OI (docs/fno/07).
    ///
    /// # Errors
    /// `UnexpectedPayload` for a missing column, no readable rows, or a file stamped for
    /// another session.
    pub async fn fo_bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        self.inner.fo_bhavcopy(on).await
    }

    /// The F&O ban list for `for_session`: symbols over 95 % of their market-wide limit,
    /// sorted and de-duplicated. Archive-then-parse, keyed by the session the file *says* it is
    /// for; a file stamped with another session is refused (the SW16 rule) and stays archived
    /// under its own date. An empty list is a real answer.
    ///
    /// # Errors
    /// `ProviderUnavailable` without an archive; `UnexpectedPayload` for a wrong-session or
    /// unreadable file.
    pub async fn fo_ban_list(&self, for_session: NaiveDate) -> Result<Vec<String>, ProviderError> {
        self.inner.fo_ban_list(for_session).await
    }

    /// Issued size × last price and the published P/E, archived per symbol. A name NSE does not
    /// quote is skipped. `series_by_symbol` is only a hint: a stale one costs a round trip,
    /// never a NULL row. Requests overlap up to `nse_fetch_concurrency` — overlapping the
    /// waiting only; every departure still takes a token from the one limiter. Results keep the
    /// caller's order (house rule 7).
    ///
    /// # Errors
    /// The first failure in the callers' symbol order.
    pub async fn equity_fundamentals(
        &self,
        on: NaiveDate,
        symbols: &[String],
        series_by_symbol: Option<&HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        let hints: HashMap<String, String> = series_by_symbol
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.to_uppercase(), v.clone()))
                    .collect()
            })
            .unwrap_or_default();
        let tokens: Vec<String> = symbols
            .iter()
            .map(|s| py_strip(s).to_uppercase())
            .filter(|t| !t.is_empty())
            .collect();
        if tokens.is_empty() {
            return Ok(Vec::new());
        }
        let concurrency = usize::try_from(self.inner.settings.nse_fetch_concurrency).unwrap_or(1);
        let workers = concurrency.min(tokens.len());
        if workers <= 1 {
            let mut found = Vec::new();
            for token in &tokens {
                if let Some(record) = self
                    .inner
                    .equity_fundamental(on, token, hints.get(token).map(String::as_str))
                    .await?
                {
                    found.push(record);
                }
            }
            return Ok(found);
        }
        let tokens = Arc::new(tokens);
        let hints = Arc::new(hints);
        let next = Arc::new(AtomicUsize::new(0));
        // The lowest index that failed so far. Python's `pool.map` is consumed in input order,
        // and the first failed result it reaches raises and cancels every future not yet
        // started, so a dead NSE costs one failure's retries, not one per remaining symbol. A
        // worker therefore never *starts* an index past a known failure; every index before it
        // still runs, so the first error in input order is the one raised, as in Python.
        let first_failure = Arc::new(AtomicUsize::new(usize::MAX));
        type Slot = Option<Result<Option<EquityFundamental>, ProviderError>>;
        let slots: Arc<Mutex<Vec<Slot>>> =
            Arc::new(Mutex::new((0..tokens.len()).map(|_| None).collect()));
        let mut pool = tokio::task::JoinSet::new();
        for _ in 0..workers {
            let inner = Arc::clone(&self.inner);
            let tokens = Arc::clone(&tokens);
            let hints = Arc::clone(&hints);
            let next = Arc::clone(&next);
            let slots = Arc::clone(&slots);
            let first_failure = Arc::clone(&first_failure);
            pool.spawn(async move {
                loop {
                    let i = next.fetch_add(1, Ordering::SeqCst);
                    if i > first_failure.load(Ordering::SeqCst) {
                        break;
                    }
                    let Some(token) = tokens.get(i) else {
                        break;
                    };
                    let result = inner
                        .equity_fundamental(on, token, hints.get(token).map(String::as_str))
                        .await;
                    if result.is_err() {
                        first_failure.fetch_min(i, Ordering::SeqCst);
                    }
                    let mut guard = slots
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner);
                    if let Some(slot) = guard.get_mut(i) {
                        *slot = Some(result);
                    }
                }
            });
        }
        while let Some(joined) = pool.join_next().await {
            joined.map_err(|e| ProviderError::foreign("RuntimeError", e.to_string()))?;
        }
        let collected = std::mem::take(
            &mut *slots
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
        );
        let mut found = Vec::new();
        for slot in collected {
            match slot {
                Some(Ok(Some(record))) => found.push(record),
                Some(Ok(None)) => {}
                Some(Err(e)) => return Err(e),
                None => {
                    return Err(ProviderError::foreign(
                        "RuntimeError",
                        "a fundamentals worker exited without answering",
                    ));
                }
            }
        }
        Ok(found)
    }

    /// SW11B: corporate announcements for `symbols`, newest first per symbol. One archived
    /// request per symbol; a symbol NSE answers with 404 yields no rows (absence); anything
    /// else that goes wrong raises the provider's own error.
    ///
    /// # Errors
    /// Every failure but a 404.
    pub async fn announcements(
        &self,
        symbols: &[String],
        on: NaiveDate,
    ) -> Result<Vec<CatalystRecord>, ProviderError> {
        let mut records = Vec::new();
        for symbol in symbols {
            let token = py_strip(symbol).to_uppercase();
            if token.is_empty() {
                continue;
            }
            if let Some(payload) = self
                .inner
                .filings_payload(KIND_ANNOUNCEMENTS, ANNOUNCEMENTS_API, &token, on)
                .await?
            {
                records.extend(parse_announcements(
                    &payload,
                    &token,
                    &self.inner.settings.nse_archive_url,
                )?);
            }
        }
        Ok(records)
    }

    /// SW11B: board meetings whose purpose is a financial result, from NSE's event calendar.
    /// Every listed meeting is returned, past and future. Same absence rule and the same error
    /// discipline as [`NseProvider::announcements`].
    ///
    /// # Errors
    /// Every failure but a 404.
    pub async fn results_calendar(
        &self,
        symbols: &[String],
        on: NaiveDate,
    ) -> Result<Vec<EarningsDateRecord>, ProviderError> {
        let mut records = Vec::new();
        let page = format!("{}{EVENT_CALENDAR_PAGE}", self.inner.settings.nse_base_url);
        for symbol in symbols {
            let token = py_strip(symbol).to_uppercase();
            if token.is_empty() {
                continue;
            }
            if let Some(payload) = self
                .inner
                .filings_payload(KIND_EVENT_CALENDAR, EVENT_CALENDAR_API, &token, on)
                .await?
            {
                records.extend(parse_results_calendar(&payload, &token, &page)?);
            }
        }
        Ok(records)
    }
}

impl Inner {
    fn archive(&self) -> Result<&Arc<dyn RawArchive>, ProviderError> {
        self.archive.as_ref().ok_or_else(|| {
            unavailable(
                "no raw-file archive configured. docs/09 requires every NSE file to be archived \
                 before it is parsed, so parsing is reproducible without refetching.",
            )
        })
    }

    fn today(&self) -> NaiveDate {
        self.clock.now().date_naive()
    }

    async fn index_constituents(
        &self,
        index_slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        if DERIVED_UNIVERSES.contains(&index_slug) {
            return Err(unexpected(format!(
                "{} is derived by rule, not published by NSE as a constituent file (docs/06 \
                 §'Step 2'); resolve it from instrument data instead",
                repr_str(index_slug)
            )));
        }
        if baskfy_core::screener::universe_by_slug(index_slug).is_err() {
            return Err(unexpected(format!(
                "unknown universe {}",
                repr_str(index_slug)
            )));
        }
        if !INDEX_SLUG_TO_NSE_NAME.iter().any(|(s, _)| *s == index_slug) {
            return Err(unexpected(format!(
                "no NSE constituent file is mapped for {}",
                repr_str(index_slug)
            )));
        }
        let payload = self
            .archived(
                &format!("{KIND_CONSTITUENTS}/{index_slug}"),
                on,
                format!(
                    "{}/content/indices/{}",
                    self.settings.nse_archive_url,
                    constituent_file(index_slug)
                ),
                "csv",
                "text/csv",
            )
            .await?;
        let context = format!("{index_slug} constituents");
        let frame = read_csv(&payload, &context)?;
        let column = require_column(&frame, &["Symbol", "SYMBOL", "symbol"], &context)?;
        let mut out = Vec::new();
        for row in frame_rows(&frame)? {
            let text = row.get(&column).py_str();
            if !py_strip(&text).is_empty() {
                out.push(text);
            }
        }
        Ok(out)
    }

    async fn index_snapshots(&self, on: NaiveDate) -> Result<Vec<IndexSnapshot>, ProviderError> {
        let payload = self
            .archived(
                KIND_INDEX_SNAPSHOT,
                on,
                format!(
                    "{}/content/indices/ind_close_all_{}.csv",
                    self.settings.nse_archive_url,
                    on.format("%d%m%Y")
                ),
                "csv",
                "text/csv",
            )
            .await?;
        let frame = read_csv(&payload, "index snapshots")?;
        let name_column = require_column(&frame, &["Index Name", "INDEX NAME"], "index snapshots")?;
        let mut snapshots = Vec::new();
        for row in frame_rows(&frame)? {
            let published = py_strip(&row.get(&name_column).py_str()).to_owned();
            require_index_date(&row, on, &published)?;
            let upper = published.to_uppercase();
            let slug = INDEX_SLUG_TO_NSE_NAME
                .iter()
                .find(|(_, name)| *name == upper)
                .map_or(published.as_str(), |(slug, _)| slug);
            let mut fields = IndexSnapshotFields::new(slug, on);
            fields.level = decimal(row.first(&["Closing Index Value", "CLOSING INDEX VALUE"]))?;
            fields.change_abs = decimal(row.first(&["Points Change", "POINTS CHANGE"]))?;
            fields.change_pct = decimal(row.first(&["Change(%)", "CHANGE(%)"]))?;
            fields.pe = decimal(row.first(&["P/E", "PE"]))?;
            fields.pb = decimal(row.first(&["P/B", "PB"]))?;
            fields.div_yield = decimal(row.first(&["Div Yield", "DIV YIELD"]))?;
            snapshots.push(IndexSnapshot::new(fields).map_err(validation)?);
        }
        Ok(snapshots)
    }

    async fn corporate_actions(
        &self,
        since: NaiveDate,
    ) -> Result<Vec<CorporateAction>, ProviderError> {
        let until = since
            .checked_add_signed(chrono::TimeDelta::days(
                self.settings.nse_corporate_action_window_days,
            ))
            .ok_or_else(|| ProviderError::foreign("OverflowError", "date value out of range"))?;
        let payload = self
            .archived(
                KIND_CORPORATE_ACTIONS_WINDOWED,
                since,
                format!(
                    "{}/api/corporates-corporateActions?index=equities&from_date={}&to_date={}",
                    self.settings.nse_base_url,
                    nse_day(since),
                    nse_day(until)
                ),
                "json",
                "application/json",
            )
            .await?;
        let frame = read_json(&payload, "corporate actions")?;
        refuse_default_page(frame.height(), since, until)?;
        let mut actions = Vec::new();
        for row in frame_rows(&frame)? {
            let symbol_value = row.first(&["symbol", "SYMBOL"]);
            let symbol = if symbol_value.truthy() {
                py_strip(&symbol_value.py_str()).to_owned()
            } else {
                String::new()
            };
            let purpose_value = row.first(&["subject", "purpose", "SUBJECT"]);
            let purpose = if purpose_value.truthy() {
                purpose_value.py_str()
            } else {
                String::new()
            };
            let ex_date = date(row.first(&["exDate", "EX_DATE", "ex_date"]))?;
            let Some(ex_date) = ex_date else {
                continue;
            };
            if symbol.is_empty() || ex_date < since {
                continue;
            }
            for (action_type, ratio_from, ratio_to, amount) in
                parse_corporate_action_purposes(&purpose)?
            {
                let mut fields = CorporateActionFields::new(&symbol, action_type, ex_date);
                fields.ratio_from = ratio_from;
                fields.ratio_to = ratio_to;
                fields.amount = amount;
                fields.raw = row.to_json_map();
                actions.push(CorporateAction::new(fields).map_err(validation)?);
            }
        }
        Ok(actions)
    }

    async fn etf_symbols(&self) -> Result<Vec<String>, ProviderError> {
        let payload = self
            .archived(
                KIND_ETF_LIST,
                self.today(),
                format!("{}{ETF_LIST_PATH}", self.settings.nse_archive_url),
                "csv",
                "text/csv",
            )
            .await?;
        let frame = read_csv(&payload, "etf list")?;
        let names: Vec<String> = frame
            .get_column_names()
            .iter()
            .map(|n| n.to_string())
            .collect();
        for column in ["Symbol", "SYMBOL", "symbol"] {
            if names.iter().any(|n| n == column) {
                let mut set = BTreeSet::new();
                for row in frame_rows(&frame)? {
                    let value = row.get(column);
                    if matches!(value, PyValue::None) {
                        continue;
                    }
                    let text = value.py_str();
                    let stripped = py_strip(&text);
                    if !stripped.is_empty() {
                        set.insert(stripped.to_uppercase());
                    }
                }
                return Ok(set.into_iter().collect());
            }
        }
        Err(ProviderError::unexpected_payload(format!(
            "the ETF list has no Symbol column: {}",
            py_list(&names)
        )))
    }

    /// One listings register. The two files spell their headers differently, so every lookup
    /// names both spellings.
    async fn listings_from(
        &self,
        kind: &str,
        path: &str,
        context: &str,
    ) -> Result<Vec<ListingRecord>, ProviderError> {
        let payload = self
            .archived(
                kind,
                self.today(),
                format!("{}{path}", self.settings.nse_archive_url),
                "csv",
                "text/csv",
            )
            .await?;
        let frame = read_csv(&payload, context)?;
        let mut records = Vec::new();
        for row in frame_rows(&frame)? {
            let symbol_value = row.first(&["SYMBOL", "Symbol"]);
            let symbol = if symbol_value.truthy() {
                py_strip(&symbol_value.py_str()).to_owned()
            } else {
                String::new()
            };
            if symbol.is_empty() {
                continue;
            }
            let name_value = row.first(&["NAME OF COMPANY", "NAME_OF_COMPANY"]);
            let name = if name_value.truthy() {
                py_strip(&name_value.py_str()).to_owned()
            } else {
                symbol.clone()
            };
            let mut fields = ListingFields::new(&symbol, &name);
            fields.series = clean(row.first(&[" SERIES", "SERIES"]));
            fields.isin = clean(row.first(&[" ISIN NUMBER", "ISIN NUMBER", "ISIN_NUMBER"]));
            fields.listed_on =
                date(row.first(&[" DATE OF LISTING", "DATE OF LISTING", "DATE_OF_LISTING"]))?;
            fields.face_value = decimal(row.first(&[" FACE VALUE", "FACE VALUE", "FACE_VALUE"]))?;
            fields.paid_up_value =
                decimal(row.first(&[" PAID UP VALUE", "PAID UP VALUE", "PAID_UP_VALUE"]))?;
            // Absent from the Emerge register entirely; None, never a guessed 1.
            fields.market_lot = int(row.first(&[" MARKET LOT", "MARKET LOT", "MARKET_LOT"]))?;
            records.push(ListingRecord::new(fields).map_err(validation)?);
        }
        Ok(records)
    }

    async fn bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        let payload = self
            .archived(
                KIND_BHAVCOPY,
                on,
                format!(
                    "{}/content/cm/BhavCopy_NSE_CM_0_0_0_{}_F_0000.csv.zip",
                    self.settings.nse_archive_url,
                    on.format("%Y%m%d")
                ),
                "zip",
                "application/zip",
            )
            .await?;
        off_runtime(move || {
            let frame = read_csv(
                &maybe_unzip(&payload)?,
                &format!("bhavcopy {}", on.format("%Y-%m-%d")),
            )?;
            bhavcopy_to_frame(&frame, on)
        })
        .await
    }

    async fn fo_bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        let base = &self.settings.nse_archive_url;
        let url = if on >= FO_UDIFF_SINCE {
            format!(
                "{base}/content/fo/BhavCopy_NSE_FO_0_0_0_{}_F_0000.csv.zip",
                on.format("%Y%m%d")
            )
        } else {
            let month = on.format("%b").to_string().to_uppercase();
            format!(
                "{base}/content/historical/DERIVATIVES/{}/{month}/fo{}{month}{}bhav.csv.zip",
                on.year(),
                on.format("%d"),
                on.year()
            )
        };
        let payload = self
            .archived(KIND_FO_BHAVCOPY, on, url, "zip", "application/zip")
            .await?;
        off_runtime(move || {
            let frame = read_csv(
                &maybe_unzip(&payload)?,
                &format!("F&O bhavcopy {}", on.format("%Y-%m-%d")),
            )?;
            fo_bhavcopy_to_frame(frame, on)
        })
        .await
    }

    async fn fo_ban_list(&self, for_session: NaiveDate) -> Result<Vec<String>, ProviderError> {
        let archive = Arc::clone(self.archive()?);
        let wanted_key = archive_key(KIND_FO_BAN_LIST, for_session, "csv")?;
        let (stamped, symbols) = if archive.exists(&wanted_key).await? {
            parse_fo_ban_list(&archive.get(&wanted_key).await?)?
        } else {
            let fetched = self
                .fetch(&format!(
                    "{}{FO_BAN_LIST_PATH}",
                    self.settings.nse_archive_url
                ))
                .await?;
            let (stamped, _) = parse_fo_ban_list(&fetched)?;
            let key = archive_key(KIND_FO_BAN_LIST, stamped, "csv")?;
            let read_back = fetch_and_archive(
                archive.as_ref(),
                &key,
                || async { Ok(fetched) },
                "text/csv",
                false,
            )
            .await?;
            parse_fo_ban_list(&read_back)?
        };
        if stamped != for_session {
            return Err(unexpected(format!(
                "F&O ban list for {} is stamped {}; wrong file for this session",
                for_session.format("%Y-%m-%d"),
                stamped.format("%Y-%m-%d")
            )));
        }
        Ok(symbols)
    }

    /// One symbol's quote: try the hinted series, then the series NSE itself reports. NSE
    /// answers a wrong series with `200` and an empty `equityResponse`, so the empty case
    /// escalates to `getMetaData` instead of being recorded as a miss.
    async fn equity_fundamental(
        &self,
        on: NaiveDate,
        token: &str,
        series_hint: Option<&str>,
    ) -> Result<Option<EquityFundamental>, ProviderError> {
        let mut tried: HashSet<String> = HashSet::new();
        if let Some(series) = series_hint.filter(|s| !s.is_empty())
            && let Some(record) = self.quote(on, token, series, &mut tried).await?
        {
            return Ok(Some(record));
        }
        if let Some(resolved) = self.resolve_series(on, token).await? {
            return self.quote(on, token, &resolved, &mut tried).await;
        }
        Ok(None)
    }

    async fn quote(
        &self,
        on: NaiveDate,
        token: &str,
        series: &str,
        tried: &mut HashSet<String>,
    ) -> Result<Option<EquityFundamental>, ProviderError> {
        if !tried.insert(series.to_owned()) {
            return Ok(None);
        }
        let result = self
            .archived(
                &format!("{KIND_EQUITY_FUNDAMENTALS}/{token}-{series}"),
                on,
                format!(
                    "{}{GET_QUOTE_API}?functionName=getSymbolData&marketType=N&series={}&symbol={}",
                    self.settings.nse_base_url,
                    py_quote(series),
                    py_quote(token)
                ),
                "json",
                "application/json",
            )
            .await;
        let payload = match result {
            Ok(p) => p,
            // 404 — NSE does not list this name. Recorded by absence, not by an invented row.
            Err(e) if e.kind() == ErrorKind::UnexpectedPayload => return Ok(None),
            Err(e) => return Err(e),
        };
        parse_equity_quote(&payload, on, token)
    }

    /// Ask NSE which series it actually quotes this symbol under today.
    async fn resolve_series(
        &self,
        on: NaiveDate,
        token: &str,
    ) -> Result<Option<String>, ProviderError> {
        let result = self
            .archived(
                &format!("{KIND_EQUITY_META}/{token}"),
                on,
                format!(
                    "{}{GET_QUOTE_API}?functionName=getMetaData&symbol={}",
                    self.settings.nse_base_url,
                    py_quote(token)
                ),
                "json",
                "application/json",
            )
            .await;
        let payload = match result {
            Ok(p) => p,
            Err(e) if e.kind() == ErrorKind::UnexpectedPayload => return Ok(None),
            Err(e) => return Err(e),
        };
        let Ok(data) = json_loads(&payload) else {
            return Ok(None);
        };
        let PyValue::Dict(items) = PyValue::from_json(&data) else {
            return Ok(None);
        };
        let data = Row::new(items);
        let PyValue::List(active) = data.get("activeSeries") else {
            return Ok(None);
        };
        let codes: Vec<String> = active
            .iter()
            .filter_map(|code| {
                let text = code.py_str();
                let stripped = py_strip(&text);
                (!stripped.is_empty()).then(|| stripped.to_uppercase())
            })
            .collect();
        if codes.is_empty() {
            return Ok(None);
        }
        if codes.iter().any(|c| c == "EQ") {
            return Ok(Some("EQ".to_owned()));
        }
        Ok(codes.into_iter().next())
    }

    async fn filings_payload(
        &self,
        kind: &str,
        api: &str,
        token: &str,
        on: NaiveDate,
    ) -> Result<Option<Vec<u8>>, ProviderError> {
        let result = self
            .archived(
                &format!("{kind}/{token}"),
                on,
                format!("{}{api}{}", self.settings.nse_base_url, py_quote(token)),
                "json",
                "application/json",
            )
            .await;
        match result {
            Ok(p) => Ok(Some(p)),
            // 404 — NSE does not know this name today. Absence, not an invented row. Every
            // other 4xx is a real refusal and stays loud.
            Err(e)
                if e.kind() == ErrorKind::UnexpectedPayload
                    && e.to_string().contains("returned 404") =>
            {
                Ok(None)
            }
            Err(e) => Err(e),
        }
    }

    /// `_archived`: archive-then-parse (docs/09). Returns the bytes read *back* from the archive.
    async fn archived(
        &self,
        kind: &str,
        on: NaiveDate,
        url: String,
        extension: &str,
        content_type: &str,
    ) -> Result<Vec<u8>, ProviderError> {
        let archive = self.archive()?;
        let key = archive_key(kind, on, extension)?;
        fetch_and_archive(
            archive.as_ref(),
            &key,
            || self.fetch(&url),
            content_type,
            false,
        )
        .await
    }

    /// `_fetch`: prime cookies, throttle, then fetch — under the retry policy.
    async fn fetch(&self, url: &str) -> Result<Vec<u8>, ProviderError> {
        call_with_retry(
            || self.request(url),
            &self.retry_policy,
            Some(PROVIDER_NAME),
            self.retry_hooks.as_ref(),
        )
        .await
    }

    fn require_client(&self) -> Result<&Arc<dyn HttpClientLike>, ProviderError> {
        self.client
            .as_ref()
            .ok_or_else(|| unavailable("no HTTP client configured"))
    }

    /// docs/09: NSE's public files need a browser-like session. One visit to the site root
    /// before the first archive request, primed once per provider, not per request — re-priming
    /// on every file is itself a traffic pattern that gets throttled. AF 3.11: a 401/403 clears
    /// the flag so the next call re-primes rather than looping on a stale jar.
    async fn prime_cookies(&self, client: &dyn HttpClientLike) -> Result<(), ProviderError> {
        let _guard = self.cookie_lock.lock().await;
        if self.cookies_primed.load(Ordering::SeqCst) {
            return Ok(());
        }
        self.throttle().await?;
        http_get(client, &self.settings.nse_base_url).await?;
        self.cookies_primed.store(true, Ordering::SeqCst);
        Ok(())
    }

    async fn throttle(&self) -> Result<(), ProviderError> {
        let Some(limiter) = &self.rate_limiter else {
            return Err(unavailable(
                "NSE rate limiter is required; refusing an unthrottled request",
            ));
        };
        limiter.acquire(1.0).await?;
        Ok(())
    }

    /// `_request`: a throttled GET with a one-shot cookie re-prime on 401/403 (AF 3.11).
    async fn request(&self, url: &str) -> Result<Vec<u8>, ProviderError> {
        let client = Arc::clone(self.require_client()?);
        self.prime_cookies(client.as_ref()).await?;
        self.throttle().await?;
        let mut response = http_get(client.as_ref(), url).await?;
        if matches!(response.status_code, 401 | 403) {
            self.cookies_primed.store(false, Ordering::SeqCst);
            self.prime_cookies(client.as_ref()).await?;
            self.throttle().await?;
            response = http_get(client.as_ref(), url).await?;
        }
        http_body(response, url)
    }
}

impl HealthReporting for NseProvider {
    fn name(&self) -> &str {
        PROVIDER_NAME
    }

    fn capabilities(&self) -> CapabilitySet {
        reference_capabilities()
    }

    fn check(&self) -> ProviderHealth {
        let inner = &self.inner;
        let mut problems: Vec<&str> = Vec::new();
        if inner.archive.is_none() {
            problems
                .push("no raw-file archive configured; docs/09 requires archiving before parsing");
        }
        if inner.client.is_none() {
            problems.push("no HTTP client configured");
        }
        if inner.rate_limiter.is_none() {
            problems.push("no shared rate limiter configured; NSE public files are rate-sensitive");
        }
        if !problems.is_empty() {
            return ProviderHealth {
                name: PROVIDER_NAME.to_owned(),
                available: false,
                capabilities: self.capabilities(),
                detail: problems.join("; "),
            };
        }
        let described = inner
            .archive
            .as_ref()
            .map_or_else(|| "unknown".to_owned(), |a| a.describe());
        ProviderHealth {
            name: PROVIDER_NAME.to_owned(),
            available: true,
            capabilities: self.capabilities(),
            detail: format!("archiving to {described}"),
        }
    }
}

#[async_trait]
impl ReferenceProvider for NseProvider {
    async fn index_constituents(
        &self,
        index_slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        Self::index_constituents(self, index_slug, on).await
    }

    async fn index_snapshots(&self, on: NaiveDate) -> Result<Vec<IndexSnapshot>, ProviderError> {
        Self::index_snapshots(self, on).await
    }

    async fn corporate_actions(
        &self,
        since: NaiveDate,
    ) -> Result<Vec<CorporateAction>, ProviderError> {
        Self::corporate_actions(self, since).await
    }

    async fn listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        Self::listings(self).await
    }

    async fn bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        Self::bhavcopy(self, on).await
    }

    async fn equity_fundamentals(
        &self,
        on: NaiveDate,
        symbols: &[String],
        series_by_symbol: Option<&HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        Self::equity_fundamentals(self, on, symbols, series_by_symbol).await
    }
}

#[async_trait]
impl SupplementaryReads for NseProvider {
    async fn sme_listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        Self::sme_listings(self).await
    }

    async fn etf_symbols(&self) -> Result<Vec<String>, ProviderError> {
        Self::etf_symbols(self).await
    }
}

// ---------------------------------------------------------------------------------------------
// Parsing helpers
// ---------------------------------------------------------------------------------------------

/// Runs CPU-bound frame work on tokio's blocking pool. polars' lazy engine calls
/// `block_in_place`, which panics on a current-thread runtime and stalls a worker on a
/// multi-threaded one; a 50,000-row F&O day should not hold the reactor either way.
///
/// # Errors
/// `work`'s own error; `RuntimeError` if it panicked.
pub(crate) async fn off_runtime<T, F>(work: F) -> Result<T, ProviderError>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T, ProviderError> + Send + 'static,
{
    tokio::task::spawn_blocking(work)
        .await
        .map_err(|e| ProviderError::foreign("RuntimeError", e.to_string()))?
}

/// A Python list of strings' `repr`.
fn py_list(items: &[String]) -> String {
    let inner: Vec<String> = items.iter().map(|s| repr_str(s)).collect();
    format!("[{}]", inner.join(", "))
}

/// `_maybe_unzip`: NSE ships the bhavcopy as a single-entry zip; older files are plain CSV.
///
/// # Errors
/// `UnexpectedPayload` for an empty or unreadable zip.
pub fn maybe_unzip(payload: &[u8]) -> Result<Vec<u8>, ProviderError> {
    if !payload.starts_with(b"PK") {
        return Ok(payload.to_vec());
    }
    let mut bundle = zip::ZipArchive::new(Cursor::new(payload))
        .map_err(|e| unexpected(format!("archived file is not a readable zip: {e}")))?;
    if bundle.is_empty() {
        return Err(unexpected("archived zip is empty"));
    }
    let mut entry = bundle
        .by_index(0)
        .map_err(|e| unexpected(format!("archived file is not a readable zip: {e}")))?;
    let mut out = Vec::new();
    entry
        .read_to_end(&mut out)
        .map_err(|e| unexpected(format!("archived file is not a readable zip: {e}")))?;
    Ok(out)
}

/// SW16: the file for `on` must say `on` in its own `Index Date` column. A file that carries
/// another session's date under this key is the wrong file — every level in it would land on
/// the wrong day and look plausible. A file with no date column is left to the columns there.
fn require_index_date(row: &Row, on: NaiveDate, published: &str) -> Result<(), ProviderError> {
    let raw = row.first(&["Index Date", "INDEX DATE"]);
    if matches!(raw, PyValue::None) {
        return Ok(());
    }
    if let Some(stamped) = date(raw)?
        && stamped != on
    {
        return Err(unexpected(format!(
            "index snapshots for {}: row {} is dated {}; wrong file for this date",
            on.format("%Y-%m-%d"),
            repr_str(published),
            stamped.format("%Y-%m-%d")
        )));
    }
    Ok(())
}

/// `_read_csv`: `pl.read_csv(payload, infer_schema_length=None, try_parse_dates=False)`.
///
/// # Errors
/// `UnexpectedPayload` naming `context` for anything polars refuses.
pub fn read_csv(payload: &[u8], context: &str) -> Result<DataFrame, ProviderError> {
    CsvReadOptions::default()
        .with_infer_schema_length(None)
        .with_has_header(true)
        .into_reader_with_file_handle(Cursor::new(payload.to_vec()))
        .finish()
        .map_err(|e| unexpected(format!("could not parse {context} as CSV: {e}")))
}

/// `_read_json`: `pl.read_json(payload)`.
///
/// # Errors
/// `UnexpectedPayload` naming `context` for anything polars refuses.
pub fn read_json(payload: &[u8], context: &str) -> Result<DataFrame, ProviderError> {
    JsonReader::new(Cursor::new(payload.to_vec()))
        .finish()
        .map_err(|e| unexpected(format!("could not parse {context} as JSON: {e}")))
}

fn require_column(
    frame: &DataFrame,
    candidates: &[&str],
    context: &str,
) -> Result<String, ProviderError> {
    let names: Vec<String> = frame
        .get_column_names()
        .iter()
        .map(|n| n.to_string())
        .collect();
    for candidate in candidates {
        if names.iter().any(|n| n == candidate) {
            return Ok((*candidate).to_owned());
        }
    }
    let wanted: Vec<String> = candidates.iter().map(|c| (*c).to_owned()).collect();
    Err(unexpected(format!(
        "{context}: none of the expected columns {} are present; got {}",
        py_list(&wanted),
        py_list(&names)
    )))
}

/// `_nse_day`: `DD-MM-YYYY` — the only date spelling `corporates-corporateActions` accepts. Give
/// it an ISO date and it silently ignores the range and serves the default page.
#[must_use]
pub fn nse_day(on: NaiveDate) -> String {
    on.format("%d-%m-%Y").to_string()
}

/// `_refuse_default_page`: refuse a payload shaped like NSE's default page rather than an
/// answer to our window. Only a window long enough to make 20 rows absurd is judged.
fn refuse_default_page(
    rows: usize,
    since: NaiveDate,
    until: NaiveDate,
) -> Result<(), ProviderError> {
    if rows != NSE_DEFAULT_PAGE_ROWS {
        return Ok(());
    }
    if (until - since).num_days() <= 20 {
        return Ok(());
    }
    Err(unexpected(format!(
        "corporate actions for {}..{} came back as exactly {NSE_DEFAULT_PAGE_ROWS} rows, which is \
         the page NSE serves when it ignores the date range. Refusing to treat it as the window's \
         full contents — see gates/ca-truncation.md.",
        since.format("%Y-%m-%d"),
        until.format("%Y-%m-%d")
    )))
}

/// Constituent files whose name is NOT `ind_{slug-without-hyphens}list.csv` — verified against
/// the live archive on 2026-08-22 (M8); every entry was a 404 under the rule.
const CONSTITUENT_FILE_OVERRIDES: [(&str, &str); 4] = [
    ("nifty-total-market", "ind_niftytotalmarket_list.csv"),
    ("nifty-large-mid-250", "ind_niftylargemidcap250list.csv"),
    ("nifty-microcap-250", "ind_niftymicrocap250_list.csv"),
    ("nifty-mid-small-400", "ind_niftymidsmallcap400list.csv"),
];

/// `_constituent_file`: the constituent CSV's filename for an index slug.
#[must_use]
pub fn constituent_file(index_slug: &str) -> String {
    CONSTITUENT_FILE_OVERRIDES
        .iter()
        .find(|(s, _)| *s == index_slug)
        .map_or_else(
            || format!("ind_{}list.csv", index_slug.replace('-', "")),
            |(_, f)| (*f).to_owned(),
        )
}

// --- the cash bhavcopy, as `pl.DataFrame(rows, strict=False)` builds it ------------------------

/// `pl.DataFrame(list_of_dicts)` infers each column from its first 100 rows.
const DICT_INFER_ROWS: usize = 100;

fn builder_refusal(value: &str, dtype: &str) -> ProviderError {
    ProviderError::foreign(
        "ComputeError",
        format!(
            "could not append value: {value} of type: {dtype} to the builder; make sure that all \
             rows have the same schema or consider increasing `infer_schema_length`\n\nit might \
             also be that a value overflows the data-type's capacity"
        ),
    )
}

/// A column whose first 100 rows are all `None` is inferred as `Null`, and polars refuses a
/// later value.
fn refuse_late_values<T>(
    values: &[Option<T>],
    show: impl Fn(&T) -> (String, &'static str),
) -> Result<(), ProviderError> {
    let window = values.len().min(DICT_INFER_ROWS);
    if values.iter().take(window).all(Option::is_none)
        && let Some(late) = values.iter().skip(window).flatten().next()
    {
        let (text, dtype) = show(late);
        return Err(builder_refusal(&text, dtype));
    }
    Ok(())
}

fn string_column(name: &str, values: &[Option<String>]) -> Result<Column, ProviderError> {
    refuse_late_values(values, |s| (format!("\"{s}\""), "str"))?;
    Ok(StringChunked::from_iter_options(
        PlSmallStr::from(name),
        values.iter().map(|v| v.as_deref()),
    )
    .into_column())
}

fn int_column(name: &str, values: &[Option<i64>]) -> Result<Column, ProviderError> {
    refuse_late_values(values, |v| (v.to_string(), "i64"))?;
    Ok(
        Int64Chunked::from_iter_options(PlSmallStr::from(name), values.iter().copied())
            .into_column(),
    )
}

/// A `Decimal` column: precision 38, scale the widest of the first 100 rows; a later value with
/// more places is rounded half to even onto that scale, as polars' builder does.
fn decimal_column(name: &str, values: &[Option<Decimal>]) -> Result<Column, ProviderError> {
    refuse_late_values(values, |d| (d.to_string(), "decimal"))?;
    let scale = values
        .iter()
        .take(DICT_INFER_ROWS)
        .flatten()
        .map(Decimal::scale)
        .max()
        .unwrap_or(0);
    let mut mantissas = Vec::with_capacity(values.len());
    for value in values {
        mantissas.push(match value {
            None => None,
            Some(d) => {
                let mut v = d.round_dp_with_strategy(scale, RoundingStrategy::MidpointNearestEven);
                v.rescale(scale);
                if v.scale() == scale {
                    Some(v.mantissa())
                } else {
                    return Err(builder_refusal(&d.to_string(), "decimal"));
                }
            }
        });
    }
    let scale = usize::try_from(scale).unwrap_or(0);
    let series = polars::prelude::Int128Chunked::from_iter_options(
        PlSmallStr::from(name),
        mantissas.into_iter(),
    )
    .into_decimal(38, scale)
    .map_err(|e| polars_error("ComputeError", &e))?
    .into_series();
    Ok(series.into_column())
}

/// `_bhavcopy_to_frame`: map NSE's bhavcopy columns onto `BHAVCOPY_SCHEMA`. NSE has published
/// this file under two column vocabularies; both are accepted so a backfill spanning the change
/// does not need two code paths.
///
/// # Errors
/// `UnexpectedPayload` when no row has a symbol; polars' refusals as Python raises them.
pub fn bhavcopy_to_frame(frame: &DataFrame, on: NaiveDate) -> Result<DataFrame, ProviderError> {
    const SYMBOL: &[&str] = &["TckrSymb", "SYMBOL"];
    const SERIES: &[&str] = &["SctySrs", "SERIES"];
    let decimals: [(&str, &[&str]); 9] = [
        ("open", &["OpnPric", "OPEN"]),
        ("high", &["HghPric", "HIGH"]),
        ("low", &["LwPric", "LOW"]),
        ("close", &["ClsPric", "CLOSE"]),
        ("prev_close", &["PrvsClsgPric", "PREVCLOSE"]),
        ("turnover", &["TtlTrfVal", "TOTTRDVAL"]),
        ("upper_circuit", &["UpprBookgPric", "UPPER_BAND"]),
        ("lower_circuit", &["LwrBookgPric", "LOWER_BAND"]),
        ("", &[]),
    ];
    let mut symbols: Vec<Option<String>> = Vec::new();
    let mut series: Vec<Option<String>> = Vec::new();
    let mut volume: Vec<Option<i64>> = Vec::new();
    let mut trades: Vec<Option<i64>> = Vec::new();
    let mut prices: Vec<Vec<Option<Decimal>>> = vec![Vec::new(); 8];
    for row in frame_rows(frame)? {
        let Some(symbol) = clean(row.first(SYMBOL)) else {
            continue;
        };
        symbols.push(Some(symbol));
        series.push(clean(row.first(SERIES)));
        for (i, (_, aliases)) in decimals.iter().take(8).enumerate() {
            if let Some(column) = prices.get_mut(i) {
                column.push(decimal(row.first(aliases))?);
            }
        }
        volume.push(int(row.first(&["TtlTradgVol", "TOTTRDQTY"]))?);
        trades.push(int(row.first(&["TtlNbOfTxsExctd", "TOTALTRADES"]))?);
    }
    if symbols.is_empty() {
        return Err(unexpected(format!(
            "bhavcopy for {} contained no readable rows",
            on.format("%Y-%m-%d")
        )));
    }
    let n = symbols.len();
    let price = |i: usize| -> Result<Column, ProviderError> {
        let (name, _) = decimals.get(i).copied().unwrap_or(("", &[]));
        decimal_column(name, prices.get(i).map_or(&[][..], Vec::as_slice))
    };
    let date_column = polars::prelude::DateChunked::from_naive_date(
        PlSmallStr::from("date"),
        std::iter::repeat_n(on, n),
    )
    .into_column();
    let columns = vec![
        string_column("symbol", &symbols)?,
        string_column("series", &series)?,
        date_column,
        price(0)?,
        price(1)?,
        price(2)?,
        price(3)?,
        price(4)?,
        int_column("volume", &volume)?,
        price(5)?,
        int_column("trades", &trades)?,
        price(6)?,
        price(7)?,
    ];
    let built = DataFrame::new(n, columns).map_err(|e| polars_error("ComputeError", &e))?;
    conform(&built, &bhavcopy_schema())
}

// --- the F&O bhavcopy ------------------------------------------------------------------------

fn strip_str(name: &str) -> Expr {
    col(name)
        .cast(DataType::String)
        .str()
        .strip_chars(lit(polars::prelude::NULL))
}

fn to_date(expr: Expr, format: &str) -> Expr {
    expr.str().to_date(StrptimeOptions {
        format: Some(PlSmallStr::from(format)),
        strict: false,
        exact: true,
        cache: true,
    })
}

/// One F&O layout's column vocabulary and parsing rules.
struct FoLayout {
    columns: [(&'static str, &'static str); 14],
    instrument: Expr,
    expiry_format: &'static str,
    stamp_format: &'static str,
    underlying: Expr,
    lot_size: Expr,
    turnover_scale: f64,
}

/// `_fo_bhavcopy_to_frame`: map either F&O bhavcopy layout onto `FO_BHAVCOPY_SCHEMA`,
/// vectorised. A file stamped with another session's date is the wrong file and is refused.
///
/// # Errors
/// `UnexpectedPayload` for a missing column, no rows, or a wrong stamp; polars' refusals.
pub fn fo_bhavcopy_to_frame(
    mut frame: DataFrame,
    on: NaiveDate,
) -> Result<DataFrame, ProviderError> {
    let stripped: Vec<PlSmallStr> = frame
        .get_column_names()
        .iter()
        .map(|n| PlSmallStr::from(py_strip(n)))
        .collect();
    frame
        .set_column_names(&stripped)
        .map_err(|e| polars_error("DuplicateError", &e))?;
    let names: Vec<String> = frame
        .get_column_names()
        .iter()
        .map(|n| n.to_string())
        .collect();
    let has = |c: &str| names.iter().any(|n| n == c);
    let udiff = has("TckrSymb");
    let FoLayout {
        columns,
        instrument,
        expiry_format,
        stamp_format,
        underlying,
        lot_size,
        turnover_scale,
    } = if udiff {
        require_column(&frame, &["FinInstrmTp"], "F&O bhavcopy")?;
        let raw = strip_str("FinInstrmTp");
        let [(c0, n0), (c1, n1), (c2, n2), (c3, n3)] = FO_INSTRUMENT_TYPES;
        let instrument = when(raw.clone().eq(lit(c0)))
            .then(lit(n0))
            .when(raw.clone().eq(lit(c1)))
            .then(lit(n1))
            .when(raw.clone().eq(lit(c2)))
            .then(lit(n2))
            .when(raw.eq(lit(c3)))
            .then(lit(n3))
            .otherwise(lit(polars::prelude::NULL).cast(DataType::String));
        let underlying = if has("UndrlygPric") {
            col("UndrlygPric").cast(DataType::Float64)
        } else {
            lit(polars::prelude::NULL).cast(DataType::Float64)
        };
        let lot_size = if has("NewBrdLotQty") {
            col("NewBrdLotQty")
                .cast(DataType::Float64)
                .cast(DataType::Int64)
        } else {
            lit(polars::prelude::NULL).cast(DataType::Int64)
        };
        FoLayout {
            columns: [
                ("stamp", "TradDt"),
                ("symbol", "TckrSymb"),
                ("expiry", "XpryDt"),
                ("strike", "StrkPric"),
                ("option_type", "OptnTp"),
                ("open", "OpnPric"),
                ("high", "HghPric"),
                ("low", "LwPric"),
                ("close", "ClsPric"),
                ("settle", "SttlmPric"),
                ("open_interest", "OpnIntrst"),
                ("oi_change", "ChngInOpnIntrst"),
                ("volume", "TtlTradgVol"),
                ("turnover", "TtlTrfVal"),
            ],
            instrument,
            expiry_format: "%Y-%m-%d",
            stamp_format: "%Y-%m-%d",
            underlying,
            lot_size,
            turnover_scale: 1.0,
        }
    } else {
        require_column(&frame, &["INSTRUMENT"], "F&O bhavcopy")?;
        FoLayout {
            columns: [
                ("stamp", "TIMESTAMP"),
                ("symbol", "SYMBOL"),
                ("expiry", "EXPIRY_DT"),
                ("strike", "STRIKE_PR"),
                ("option_type", "OPTION_TYP"),
                ("open", "OPEN"),
                ("high", "HIGH"),
                ("low", "LOW"),
                ("close", "CLOSE"),
                ("settle", "SETTLE_PR"),
                ("open_interest", "OPEN_INT"),
                ("oi_change", "CHG_IN_OI"),
                ("volume", "CONTRACTS"),
                ("turnover", "VAL_INLAKH"),
            ],
            instrument: strip_str("INSTRUMENT"),
            expiry_format: "%d-%b-%Y",
            stamp_format: "%d-%b-%Y",
            underlying: lit(polars::prelude::NULL).cast(DataType::Float64),
            lot_size: lit(polars::prelude::NULL).cast(DataType::Int64),
            // The legacy file prints turnover in lakh.
            turnover_scale: 100_000.0,
        }
    };
    for (_, name) in &columns {
        require_column(&frame, &[name], "F&O bhavcopy")?;
    }
    let source = |key: &str| -> &str {
        columns
            .iter()
            .find(|(k, _)| *k == key)
            .map_or("", |(_, v)| v)
    };
    let number = |key: &str| col(source(key)).cast(DataType::Float64);
    let count = |key: &str| number(key).cast(DataType::Int64);
    let option_type = strip_str(source("option_type"));
    let instruments: Vec<&str> = FO_INSTRUMENT_TYPES.iter().map(|(_, v)| *v).collect();
    let is_instrument = instruments.iter().skip(1).fold(
        col("instrument").eq(lit(instruments.first().copied().unwrap_or(""))),
        |acc, v| acc.or(col("instrument").eq(lit(*v))),
    );
    let parsed = frame
        .lazy()
        .select([
            to_date(strip_str(source("stamp")), stamp_format).alias("stamp"),
            lit(on).alias("date"),
            instrument.alias("instrument"),
            strip_str(source("symbol")).alias("symbol"),
            to_date(strip_str(source("expiry")), expiry_format).alias("expiry"),
            number("strike").alias("strike"),
            when(
                option_type
                    .clone()
                    .eq(lit("CE"))
                    .or(option_type.clone().eq(lit("PE"))),
            )
            .then(option_type)
            .otherwise(lit(polars::prelude::NULL).cast(DataType::String))
            .alias("option_type"),
            number("open").alias("open"),
            number("high").alias("high"),
            number("low").alias("low"),
            number("close").alias("close"),
            number("settle").alias("settle"),
            underlying.alias("underlying"),
            count("open_interest").alias("open_interest"),
            count("oi_change").alias("oi_change"),
            count("volume").alias("volume"),
            (number("turnover") * lit(turnover_scale))
                .round(2, RoundMode::HalfToEven)
                .alias("turnover"),
            lot_size.alias("lot_size"),
        ])
        .filter(is_instrument)
        .collect()
        .map_err(|e| polars_error("ComputeError", &e))?;
    if parsed.height() == 0 {
        return Err(unexpected(format!(
            "F&O bhavcopy for {} contained no readable rows",
            on.format("%Y-%m-%d")
        )));
    }
    let stamps = parsed
        .column("stamp")
        .map_err(|e| polars_error("ColumnNotFoundError", &e))?
        .as_materialized_series()
        .drop_nulls()
        .unique()
        .map_err(|e| polars_error("ComputeError", &e))?;
    let mut seen: Vec<NaiveDate> = Vec::new();
    for i in 0..stamps.len() {
        if let PyValue::Date(d) = PyValue::from_any(
            &stamps
                .get(i)
                .map_err(|e| polars_error("ComputeError", &e))?,
        ) {
            seen.push(d);
        }
    }
    if !seen.is_empty() && seen != [on] {
        seen.sort();
        let listed: Vec<String> = seen.iter().map(|d| PyValue::Date(*d).py_repr()).collect();
        return Err(unexpected(format!(
            "F&O bhavcopy for {} is stamped [{}]; wrong file for this date",
            on.format("%Y-%m-%d"),
            listed.join(", ")
        )));
    }
    let without_stamp = parsed
        .drop("stamp")
        .map_err(|e| polars_error("ComputeError", &e))?;
    conform(&without_stamp, &fo_bhavcopy_schema())
}

/// `parse_fo_ban_list`: `fo_secban.csv` as `(the session it is for, the symbols in ban)`.
///
/// A list that cannot be read in full is refused rather than read in part, because a missed
/// symbol is a trade in a banned stock. `NIL` after the header, or no rows, is an empty list.
///
/// # Errors
/// `UnexpectedPayload` for non-UTF-8 bytes, a missing or undatable header, or a line that is
/// neither blank, `NIL` nor `n,SYMBOL`.
pub fn parse_fo_ban_list(payload: &[u8]) -> Result<(NaiveDate, Vec<String>), ProviderError> {
    let body = payload.strip_prefix(b"\xef\xbb\xbf").unwrap_or(payload);
    let text = std::str::from_utf8(body).map_err(|e| {
        let position = e.valid_up_to();
        let byte = body.get(position).copied().unwrap_or(0);
        let reason = match e.error_len() {
            None => "unexpected end of data",
            Some(_) if (0x80..0xc2).contains(&byte) || byte >= 0xf5 => "invalid start byte",
            Some(_) => "invalid continuation byte",
        };
        unexpected(format!(
            "F&O ban list is not UTF-8 text: 'utf-8' codec can't decode byte 0x{byte:02x} in \
             position {position}: {reason}"
        ))
    })?;
    let lines: Vec<&str> = py_splitlines(text)
        .into_iter()
        .map(py_strip)
        .filter(|l| !l.is_empty())
        .collect();
    let header = match lines.first() {
        Some(first) => regex_of(&FO_BAN_HEADER)?.captures(first),
        None => None,
    };
    let stamped = match &header {
        Some(caps) => date(&PyValue::Str(
            caps.get(1).map_or("", |m| m.as_str()).to_owned(),
        ))?,
        None => None,
    };
    let Some(stamped) = stamped else {
        return Err(unexpected(
            "F&O ban list has no 'Securities in Ban For Trade Date' header; not the ban file",
        ));
    };
    let row_re = regex_of(&FO_BAN_ROW)?;
    let mut symbols = BTreeSet::new();
    for line in lines.iter().skip(1) {
        match row_re.captures(line) {
            Some(caps) => {
                symbols.insert(caps.get(1).map_or("", |m| m.as_str()).to_uppercase());
            }
            None => {
                if line.trim_matches([' ', ',']).to_uppercase() == "NIL" {
                    continue;
                }
                return Err(unexpected(format!(
                    "F&O ban list for {} has an unreadable line {}",
                    stamped.format("%Y-%m-%d"),
                    repr_str(line)
                )));
            }
        }
    }
    Ok((stamped, symbols.into_iter().collect()))
}

// --- corporate-action purposes ----------------------------------------------------------------

/// `_to_decimal`: `Decimal(text)`, `None` for `InvalidOperation`.
fn to_decimal(text: &str) -> Result<Option<Decimal>, ProviderError> {
    decimal(&PyValue::Str(text.to_owned())).map(|d| d.filter(|_| !text.trim().is_empty()))
}

/// `(ratio_from, ratio_to)`.
type Ratio = (Option<Decimal>, Option<Decimal>);

fn ratio_pair(text: &str) -> Result<Option<Ratio>, ProviderError> {
    let Some(caps) = regex_of(&RATIO_PAIR)?.captures(text) else {
        return Ok(None);
    };
    Ok(Some((
        to_decimal(caps.get(1).map_or("", |m| m.as_str()))?,
        to_decimal(caps.get(2).map_or("", |m| m.as_str()))?,
    )))
}

fn parse_split(text: &str) -> Result<Option<ParsedAction>, ProviderError> {
    if let Some(caps) = regex_of(&SPLIT_FACES)?.captures(text) {
        return Ok(Some((
            CorporateActionType::Split,
            to_decimal(caps.get(1).map_or("", |m| m.as_str()))?,
            to_decimal(caps.get(2).map_or("", |m| m.as_str()))?,
            None,
        )));
    }
    Ok(ratio_pair(text)?.map(|(a, b)| (CorporateActionType::Split, a, b, None)))
}

fn parse_bonus(text: &str) -> Result<Option<ParsedAction>, ProviderError> {
    Ok(ratio_pair(text)?.map(|(a, b)| (CorporateActionType::Bonus, a, b, None)))
}

fn parse_rights(text: &str) -> Result<Option<ParsedAction>, ProviderError> {
    Ok(Some(ratio_pair(text)?.map_or(
        (CorporateActionType::Rights, None, None, None),
        |(a, b)| (CorporateActionType::Rights, a, b, None),
    )))
}

/// `_parse_dividend`: sum every `RS x` / `RE x` amount in the purpose (AF 3.3). NSE writes
/// combined cash legs as `DIVIDEND - RS.2.50 + RS.1.00 PER SHARE`.
fn parse_dividend(text: &str) -> Result<Option<ParsedAction>, ProviderError> {
    let mut amounts = Vec::new();
    for caps in regex_of(&RUPEE_AMOUNT)?.captures_iter(text) {
        if let Some(value) = to_decimal(caps.get(1).map_or("", |m| m.as_str()))? {
            amounts.push(value);
        }
    }
    let Some((first, rest)) = amounts.split_first() else {
        return Ok(Some((CorporateActionType::Dividend, None, None, None)));
    };
    let mut total = *first;
    for extra in rest {
        total = total.checked_add(*extra).ok_or_else(|| {
            ProviderError::foreign("OverflowError", "dividend total beyond 28 digits")
        })?;
    }
    Ok(Some((
        CorporateActionType::Dividend,
        None,
        None,
        Some(total),
    )))
}

/// `parse_corporate_action_purpose`: the first modelled leg only, for callers that expect one.
///
/// # Errors
/// Only the R1.3-class overflow of an absurd amount.
pub fn parse_corporate_action_purpose(
    purpose: &str,
) -> Result<Option<ParsedAction>, ProviderError> {
    Ok(parse_corporate_action_purposes(purpose)?.into_iter().next())
}

/// `parse_corporate_action_purposes`: every modelled leg in an NSE purpose string (AF 3.2).
///
/// NSE publishes combined purposes such as `FACE VALUE SPLIT … AND BONUS 1:1`. Segments are
/// split on `AND` / `&`; each contributes at most one action, matched by the first keyword
/// (SPLIT, BONUS, RIGHTS, DEMERGER, DIVIDEND) it contains. Unmodelled text yields nothing
/// rather than a guess.
///
/// # Errors
/// Only the R1.3-class overflow of an absurd amount.
pub fn parse_corporate_action_purposes(purpose: &str) -> Result<Vec<ParsedAction>, ProviderError> {
    let upper = purpose.to_uppercase();
    let text = py_strip(&upper);
    if text.is_empty() {
        return Ok(Vec::new());
    }
    let mut segments: Vec<&str> = regex_of(&LEG_SEPARATOR)?
        .split(text)
        .map(py_strip)
        .filter(|p| !p.is_empty())
        .collect();
    if segments.len() == 1 {
        segments = vec![text];
    }
    let mut found = Vec::new();
    for segment in segments {
        let parsed = if segment.contains("SPLIT") {
            parse_split(segment)?
        } else if segment.contains("BONUS") {
            parse_bonus(segment)?
        } else if segment.contains("RIGHTS") {
            parse_rights(segment)?
        } else if segment.contains("DEMERGER") {
            Some((CorporateActionType::Demerger, None, None, None))
        } else if segment.contains("DIVIDEND") {
            parse_dividend(segment)?
        } else {
            None
        };
        if let Some(action) = parsed {
            found.push(action);
        }
    }
    Ok(found)
}

// --- equity quotes ----------------------------------------------------------------------------

fn quote_json(payload: &[u8], fallback_symbol: &str) -> Result<Row, ProviderError> {
    let data = json_loads(payload).map_err(|e| {
        unexpected(format!(
            "equity quote for {fallback_symbol} is not JSON: {e}"
        ))
    })?;
    match PyValue::from_json(&data) {
        PyValue::Dict(items) => Ok(Row::new(items)),
        _ => Err(unexpected(format!(
            "equity quote for {fallback_symbol} is not an object"
        ))),
    }
}

/// `_section`: one nested object, or an empty mapping. NSE omits whole sections for some names.
fn section(data: &Row, key: &str) -> Row {
    match data.get(key) {
        PyValue::Dict(items) => Row::new(items.clone()),
        _ => Row::default(),
    }
}

/// `a or b or ...` over `_decimal` results, evaluated lazily: a zero is falsy, so it falls
/// through.
fn first_truthy(
    candidates: &[&dyn Fn() -> Result<Option<Decimal>, ProviderError>],
) -> Result<Option<Decimal>, ProviderError> {
    let mut last = None;
    for candidate in candidates {
        let value = candidate()?;
        if value.is_some_and(|d| !d.is_zero()) {
            return Ok(value);
        }
        last = value;
    }
    Ok(last)
}

fn quantize_half_up(value: Decimal) -> Result<i64, ProviderError> {
    let rounded = value.round_dp_with_strategy(0, RoundingStrategy::MidpointAwayFromZero);
    i64::try_from(rounded.mantissa() / 10_i128.pow(rounded.scale()))
        .map_err(|_| ProviderError::foreign("OverflowError", "marketcap beyond a 64-bit integer"))
}

fn marketcap_from_shares(
    shares: Option<i64>,
    price: Option<Decimal>,
) -> Result<Option<i64>, ProviderError> {
    let (Some(shares), Some(price)) = (shares, price) else {
        return Ok(None);
    };
    if price <= Decimal::ZERO {
        return Ok(None);
    }
    let value = Decimal::from(shares)
        .checked_mul(price)
        .and_then(|v| v.checked_div(CRORE))
        .ok_or_else(|| {
            ProviderError::foreign(
                "OverflowError",
                "marketcap beyond 28 digits (DECISIONS-RS R1.3)",
            )
        })?;
    quantize_half_up(value).map(Some)
}

fn fundamental(
    fields: EquityFundamentalFields,
) -> Result<Option<EquityFundamental>, ProviderError> {
    EquityFundamental::new(fields).map(Some).map_err(validation)
}

/// `parse_equity_quote`: read whichever equity-quote shape NSE served into one record.
///
/// Two shapes exist on disk; the archive is permanent (docs/09), so payloads captured before
/// NSE's Next.js migration must keep parsing forever. Dispatching on the payload rather than on a
/// flag means a re-parse of the archive never has to know when the file was fetched.
///
/// # Errors
/// `UnexpectedPayload` when the payload is not a JSON object.
pub fn parse_equity_quote(
    payload: &[u8],
    on: NaiveDate,
    fallback_symbol: &str,
) -> Result<Option<EquityFundamental>, ProviderError> {
    let data = quote_json(payload, fallback_symbol)?;
    if data.contains("equityResponse") {
        return parse_get_symbol_data(payload, on, fallback_symbol);
    }
    parse_quote_equity(payload, on, fallback_symbol)
}

/// `parse_get_symbol_data`: `GetQuoteApi?functionName=getSymbolData` into a record. A wrong
/// series is answered with `200` and an **empty** list, so an empty response is `None`. `pb`
/// and `div_yield` are not in this payload at all and stay NULL.
///
/// # Errors
/// `UnexpectedPayload` when the payload is not a JSON object.
pub fn parse_get_symbol_data(
    payload: &[u8],
    on: NaiveDate,
    fallback_symbol: &str,
) -> Result<Option<EquityFundamental>, ProviderError> {
    let data = quote_json(payload, fallback_symbol)?;
    let PyValue::List(entries) = data.get("equityResponse") else {
        return Ok(None);
    };
    let Some(PyValue::Dict(entry)) = entries.first() else {
        return Ok(None);
    };
    let entry = Row::new(entry.clone());
    let trade = section(&entry, "tradeInfo");
    let security = section(&entry, "secInfo");
    let meta = section(&entry, "metaData");
    let order = section(&entry, "orderBook");

    let symbol = clean(meta.first(&["symbol"])).unwrap_or_else(|| fallback_symbol.to_owned());
    let shares = int(trade.first(&["issuedSize"]))?;
    let last = first_truthy(&[
        &|| decimal(trade.first(&["lastPrice"])),
        &|| decimal(meta.first(&["closePrice", "lastPrice"])),
        &|| decimal(order.first(&["lastPrice"])),
    ])?;
    let pe = decimal(security.first(&["pdSymbolPe"]))?;

    let mut marketcap_cr = marketcap_from_shares(shares, last)?;
    if marketcap_cr.is_none() {
        // NSE's own figure, already in rupees, when we cannot derive it ourselves.
        if let Some(total) = decimal(trade.first(&["totalMarketCap"]))?
            && total > Decimal::ZERO
        {
            let value = total.checked_div(CRORE).ok_or_else(|| {
                ProviderError::foreign("OverflowError", "marketcap beyond 28 digits")
            })?;
            marketcap_cr = Some(quantize_half_up(value)?);
        }
    }
    if marketcap_cr.is_none() && pe.is_none() && shares.is_none() {
        return Ok(None);
    }
    let mut fields = EquityFundamentalFields::new(&symbol, on);
    fields.shares_outstanding = shares;
    fields.last_price = last;
    fields.marketcap_cr = marketcap_cr;
    fields.pe = pe;
    fundamental(fields)
}

/// `parse_quote_equity`: NSE's retired `quote-equity` JSON. Kept for the archive, not the
/// network. A missing issued size or last price leaves `marketcap_cr` NULL rather than
/// inventing a zero cap.
///
/// # Errors
/// `UnexpectedPayload` when the payload is not a JSON object.
pub fn parse_quote_equity(
    payload: &[u8],
    on: NaiveDate,
    fallback_symbol: &str,
) -> Result<Option<EquityFundamental>, ProviderError> {
    let data = quote_json(payload, fallback_symbol)?;
    let info = section(&data, "info");
    let security = section(&data, "securityInfo");
    let price = section(&data, "priceInfo");
    let meta = section(&data, "metadata");
    let symbol = clean(info.first(&["symbol"])).unwrap_or_else(|| fallback_symbol.to_owned());
    let shares = int(security.first(&["issuedSize", "issued_size"]))?;
    let last = decimal(price.first(&["lastPrice", "close", "last_price"]))?;
    let pe = first_truthy(&[&|| decimal(price.first(&["pE", "pe"])), &|| {
        decimal(meta.first(&["pdSymbolPe"]))
    }])?;
    let pb = first_truthy(&[&|| decimal(price.first(&["pB", "pb"])), &|| {
        decimal(meta.first(&["pdSymbolPb", "pdSectorPb"]))
    }])?;
    let div_yield = decimal(price.first(&["yield", "divYield"]))?;
    let marketcap_cr = marketcap_from_shares(shares, last)?;
    if marketcap_cr.is_none() && pe.is_none() && shares.is_none() {
        return Ok(None);
    }
    let mut fields = EquityFundamentalFields::new(&symbol, on);
    fields.shares_outstanding = shares;
    fields.last_price = last;
    fields.marketcap_cr = marketcap_cr;
    fields.pe = pe;
    fields.pb = pb;
    fields.div_yield = div_yield;
    fundamental(fields)
}

// --- SW11B: the catalyst feed's two parsers (STANDING-ANSWERS A3) -----------------------------

/// `_first_stamp`: the first candidate that *parses* — NSE ships `an_dt` as `""` beside a filled
/// `sort_date` often enough that "first key present" would lose the stamp.
fn first_stamp(
    row: &Row,
    candidates: &[&str],
) -> Result<Option<DateTime<FixedOffset>>, ProviderError> {
    for candidate in candidates {
        if let Some(s) = stamp(row.get(candidate))? {
            return Ok(Some(s));
        }
    }
    Ok(None)
}

/// `_filings_rows`: the list NSE's filings endpoints answer with. A `{}` or a non-list is damage;
/// an empty list is an honest "nothing filed".
fn filings_rows(payload: &[u8], context: &str) -> Result<Vec<Row>, ProviderError> {
    let data = json_loads(payload)
        .map_err(|e| unexpected(format!("could not parse {context} as JSON: {e}")))?;
    let mut value = PyValue::from_json(&data);
    if let PyValue::Dict(items) = &value
        && let PyValue::List(inner) = Row::new(items.clone()).get("data")
    {
        value = PyValue::List(inner.clone());
    }
    let PyValue::List(items) = value else {
        return Err(unexpected(format!(
            "{context}: expected a JSON list, got {}",
            value.type_name()
        )));
    };
    Ok(items
        .into_iter()
        .filter_map(|item| match item {
            PyValue::Dict(pairs) => Some(Row::new(pairs)),
            _ => None,
        })
        .collect())
}

/// `_headline`: NSE's subject line plus its one-line summary, capped. Never the attachment body.
fn headline(subject: Option<&str>, summary: Option<&str>) -> Option<String> {
    let mut parts: Vec<&str> = Vec::new();
    match (subject, summary) {
        (Some(subj), Some(summ)) if summ.to_lowercase().starts_with(&subj.to_lowercase()) => {
            parts.push(summ); // NSE repeats the subject at the head of its summary
        }
        _ => parts.extend(
            [subject, summary]
                .into_iter()
                .flatten()
                .filter(|p| !p.is_empty()),
        ),
    }
    let joined: Vec<String> = parts
        .iter()
        .map(|p| py_split_whitespace(p).join(" "))
        .collect();
    let text = joined.join(" — ");
    if text.is_empty() {
        return None;
    }
    if text.chars().count() > HEADLINE_MAX_CHARS {
        let head: String = text.chars().take(HEADLINE_MAX_CHARS - 1).collect();
        let trimmed = head.trim_end_matches(py_isspace);
        return Some(format!("{trimmed}…"));
    }
    Some(text)
}

/// `_absolute`: NSE's attachment links are absolute; a relative one is joined, never guessed at.
fn absolute(url: &str, base: &str) -> String {
    if url.starts_with("http://") || url.starts_with("https://") {
        return url.to_owned();
    }
    format!("{base}/{}", url.trim_start_matches('/'))
}

/// `parse_announcements`: `/api/corporate-announcements` into records — headline, stamp and
/// link only. A row without an attachment URL is dropped and not invented; a row without a
/// stamp is kept and sorts last. Newest first.
///
/// # Errors
/// `UnexpectedPayload` for a body that is not JSON or not a list.
pub fn parse_announcements(
    payload: &[u8],
    fallback_symbol: &str,
    archive_url: &str,
) -> Result<Vec<CatalystRecord>, ProviderError> {
    let rows = filings_rows(payload, &format!("{fallback_symbol} announcements"))?;
    let mut records: Vec<CatalystRecord> = Vec::new();
    for row in rows {
        let Some(url) = clean(row.first(&["attchmntFile", "attachmentFile", "attchmntfile"]))
        else {
            continue;
        };
        let Some(head) = headline(
            clean(row.first(&["desc", "subject", "sm_desc"])).as_deref(),
            clean(row.first(&["attchmntText", "attachmentText"])).as_deref(),
        ) else {
            continue;
        };
        let published = first_stamp(&row, &["an_dt", "sort_date", "dt", "exchdisstime"])?;
        let fields = CatalystFields {
            symbol: clean(row.first(&["symbol", "SYMBOL"]))
                .unwrap_or_else(|| fallback_symbol.to_owned()),
            headline: head,
            published_at: published.map(PyDateTime::Aware),
            url: absolute(&url, archive_url),
            source: CATALYST_SOURCE_ANNOUNCEMENT.to_owned(),
        };
        records.push(CatalystRecord::new(fields).map_err(validation)?);
    }
    records.sort_by(|a, b| {
        let key = |r: &CatalystRecord| r.published_at.and_then(|p| p.aware());
        match (key(a), key(b)) {
            (Some(x), Some(y)) => y.cmp(&x),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        }
    });
    Ok(records)
}

/// `parse_results_calendar`: `/api/event-calendar` into the meetings whose purpose names a
/// result. AGMs, fund raising and the rest are dropped; a row with no readable date cannot be a
/// flag and is dropped too. Soonest first.
///
/// # Errors
/// `UnexpectedPayload` for a body that is not JSON or not a list.
pub fn parse_results_calendar(
    payload: &[u8],
    fallback_symbol: &str,
    page_url: &str,
) -> Result<Vec<EarningsDateRecord>, ProviderError> {
    let rows = filings_rows(payload, &format!("{fallback_symbol} event calendar"))?;
    let results = regex_of(&RESULTS_PURPOSE)?;
    let mut records: Vec<EarningsDateRecord> = Vec::new();
    for row in rows {
        let Some(purpose) = clean(row.first(&["purpose", "bm_desc", "PURPOSE"])) else {
            continue;
        };
        if !results.is_match(&purpose) {
            continue;
        }
        let Some(event_date) = date(row.first(&["bm_date", "date", "BM_DATE"]))? else {
            continue;
        };
        let symbol =
            clean(row.first(&["symbol", "SYMBOL"])).unwrap_or_else(|| fallback_symbol.to_owned());
        let fields = EarningsDateFields {
            url: format!("{page_url}{}", py_quote(&symbol)),
            symbol,
            event_date,
            purpose: py_split_whitespace(&purpose).join(" "),
        };
        records.push(EarningsDateRecord::new(fields).map_err(validation)?);
    }
    records.sort_by_key(|r| r.event_date);
    Ok(records)
}
