//! The records the provider ports exchange. Ports `baskfy_providers.records`.
//!
//! Python uses frozen pydantic models with `extra="forbid"` because these cross a trust
//! boundary: every one is built from a broker response or a scraped exchange file. The Rust
//! shape keeps both properties (DECISIONS-RS R4.6):
//!
//! * **validated at construction** — each record `X` is built from its plain `XFields` by
//!   [`X::new`](InstrumentRecord::new) (or deserialised, which runs the same checks) and an
//!   invalid one cannot exist;
//! * **frozen** — `X` derefs to `&XFields`, so `record.symbol` reads, but there is no
//!   `DerefMut` and no setter: a record is a reading of the world at a moment;
//! * **unknown keys refused** — `XFields` deserialises with `deny_unknown_fields`.
//!
//! Prices and quantities are `Decimal` exactly where Python's are (house rule 9). Frames keep
//! polars: [`daily_bars_schema`], [`bhavcopy_schema`] and [`fo_bhavcopy_schema`] are the fixed
//! schemas every adapter conforms to, with `Decimal(18, 4)` money columns.

use std::collections::BTreeMap;
use std::fmt;
use std::ops::Deref;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime};
use polars::prelude::{DataFrame, DataType, Field, IntoLazy, PlSmallStr, Schema, col};
use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::errors::ProviderError;
use crate::py::repr_str;

/// A record failed validation (pydantic's `ValidationError`, one field per error).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("1 validation error for {record}\n{field}\n  {message}")]
pub struct RecordError {
    /// The record's Python class name.
    pub record: &'static str,
    /// The offending field.
    pub field: &'static str,
    /// What was wrong with it.
    pub message: String,
}

fn invalid(record: &'static str, field: &'static str, message: impl Into<String>) -> RecordError {
    RecordError {
        record,
        field,
        message: message.into(),
    }
}

fn min_length_1(record: &'static str, field: &'static str, value: &str) -> Result<(), RecordError> {
    if value.chars().count() < 1 {
        return Err(invalid(
            record,
            field,
            "String should have at least 1 character",
        ));
    }
    Ok(())
}

fn gt_zero_int(record: &'static str, field: &'static str, value: i64) -> Result<(), RecordError> {
    if value <= 0 {
        return Err(invalid(record, field, "Input should be greater than 0"));
    }
    Ok(())
}

fn ge_zero_int(record: &'static str, field: &'static str, value: i64) -> Result<(), RecordError> {
    if value < 0 {
        return Err(invalid(
            record,
            field,
            "Input should be greater than or equal to 0",
        ));
    }
    Ok(())
}

fn gt_zero_dec(
    record: &'static str,
    field: &'static str,
    value: Decimal,
) -> Result<(), RecordError> {
    if value <= Decimal::ZERO {
        return Err(invalid(record, field, "Input should be greater than 0"));
    }
    Ok(())
}

/// Generates the frozen record `$name` over its plain fields `$fields`, validated by `$check`.
macro_rules! record {
    ($(#[$meta:meta])* $name:ident, $fields:ident, $fields_lit:literal, $check:path) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
        #[serde(try_from = $fields_lit, into = $fields_lit)]
        pub struct $name($fields);

        impl $name {
            #[doc = concat!("Validates `fields` into a `", stringify!($name), "` (pydantic's constructor).")]
            ///
            /// # Errors
            /// [`RecordError`] naming the first field that fails.
            pub fn new(fields: $fields) -> Result<Self, RecordError> {
                $check(&fields)?;
                Ok(Self(fields))
            }

            /// The validated fields, by value.
            #[must_use]
            pub fn into_fields(self) -> $fields {
                self.0
            }
        }

        impl Deref for $name {
            type Target = $fields;
            fn deref(&self) -> &$fields {
                &self.0
            }
        }

        impl TryFrom<$fields> for $name {
            type Error = RecordError;
            fn try_from(fields: $fields) -> Result<Self, RecordError> {
                Self::new(fields)
            }
        }

        impl From<$name> for $fields {
            fn from(record: $name) -> $fields {
                record.0
            }
        }
    };
}

// ---------------------------------------------------------------------------------------------
// Literals
// ---------------------------------------------------------------------------------------------

/// `InstrumentType = Literal["EQ", "ETF", "INDEX"]` (`baskfy_core.models.reference.INSTRUMENT_TYPES`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum InstrumentType {
    /// `EQ`.
    #[serde(rename = "EQ")]
    Eq,
    /// `ETF`.
    #[serde(rename = "ETF")]
    Etf,
    /// `INDEX`.
    #[serde(rename = "INDEX")]
    Index,
}

impl InstrumentType {
    /// The stored string.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Eq => "EQ",
            Self::Etf => "ETF",
            Self::Index => "INDEX",
        }
    }
}

/// `CorporateActionType` (`baskfy_core.models.market.CORPORATE_ACTION_TYPES`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CorporateActionType {
    /// `dividend`.
    Dividend,
    /// `split`.
    Split,
    /// `bonus`.
    Bonus,
    /// `rights`.
    Rights,
    /// `demerger`.
    Demerger,
}

impl CorporateActionType {
    /// The stored string.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Dividend => "dividend",
            Self::Split => "split",
            Self::Bonus => "bonus",
            Self::Rights => "rights",
            Self::Demerger => "demerger",
        }
    }
}

/// `BarSource = Literal["kite", "nse"]` — what a provider stamps on a raw bar.
/// (`baskfy_core.models.market.BAR_SOURCES` adds `kite_adjusted`, which no provider writes.)
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BarSource {
    /// `kite`.
    Kite,
    /// `nse`.
    Nse,
}

impl BarSource {
    /// The stored string.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Kite => "kite",
            Self::Nse => "nse",
        }
    }
}

/// `Literal["CE", "PE"]`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OptionType {
    /// Call.
    #[serde(rename = "CE")]
    Ce,
    /// Put.
    #[serde(rename = "PE")]
    Pe,
}

/// `Literal["BUY", "SELL"]`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum TransactionType {
    /// `BUY`.
    #[serde(rename = "BUY")]
    Buy,
    /// `SELL`.
    #[serde(rename = "SELL")]
    Sell,
}

/// `Literal["MIS"]` — the only product a margin question is asked for.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum MarginProduct {
    /// `MIS`.
    #[default]
    #[serde(rename = "MIS")]
    Mis,
}

/// `Literal["LIMIT", "MARKET"]`, default `MARKET`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum MarginOrderType {
    /// `LIMIT`.
    #[serde(rename = "LIMIT")]
    Limit,
    /// `MARKET`.
    #[default]
    #[serde(rename = "MARKET")]
    Market,
}

/// `Literal["regular"]`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum MarginVariety {
    /// `regular`.
    #[default]
    #[serde(rename = "regular")]
    Regular,
}

/// A Python `datetime`: naive or timezone-aware, as the source gave it. Kite's quote timestamps
/// are naive (the kiteconnect client parses `"2026-08-20 15:29:59"`); NSE's are stamped IST.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PyDateTime {
    /// `tzinfo is None`.
    Naive(NaiveDateTime),
    /// Timezone-aware.
    Aware(DateTime<FixedOffset>),
}

impl PyDateTime {
    /// `datetime.fromisoformat` for the forms the providers meet (`T` or space separator,
    /// optional fraction, optional `±HH:MM`/`Z` offset).
    ///
    /// # Errors
    /// `ValueError` (Python's message) when the text is not one of them.
    pub fn parse(text: &str) -> Result<Self, ProviderError> {
        let normalised = text.trim().replacen(' ', "T", 1);
        let with_z = normalised.replace('Z', "+00:00");
        for fmt in ["%Y-%m-%dT%H:%M:%S%.f%:z", "%Y-%m-%dT%H:%M%:z"] {
            if let Ok(dt) = DateTime::parse_from_str(&with_z, fmt) {
                return Ok(Self::Aware(dt));
            }
        }
        for fmt in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M"] {
            if let Ok(dt) = NaiveDateTime::parse_from_str(&normalised, fmt) {
                return Ok(Self::Naive(dt));
            }
        }
        if let Ok(d) = NaiveDate::parse_from_str(&normalised, "%Y-%m-%d")
            && let Some(dt) = d.and_hms_opt(0, 0, 0)
        {
            return Ok(Self::Naive(dt));
        }
        Err(ProviderError::value_error(format!(
            "Invalid isoformat string: {}",
            repr_str(text)
        )))
    }

    /// `datetime.isoformat()`: microseconds only when non-zero, offset as `+HH:MM`.
    #[must_use]
    pub fn isoformat(&self) -> String {
        match self {
            Self::Naive(dt) => iso_naive(dt),
            Self::Aware(dt) => {
                let mut s = iso_naive(&dt.naive_local());
                s.push_str(&dt.format("%:z").to_string());
                s
            }
        }
    }

    /// The aware value, if it is one.
    #[must_use]
    pub const fn aware(&self) -> Option<DateTime<FixedOffset>> {
        match self {
            Self::Aware(dt) => Some(*dt),
            Self::Naive(_) => None,
        }
    }
}

fn iso_naive(dt: &NaiveDateTime) -> String {
    use chrono::Timelike;
    let micros = dt.nanosecond() / 1_000;
    if micros == 0 {
        dt.format("%Y-%m-%dT%H:%M:%S").to_string()
    } else {
        format!("{}.{micros:06}", dt.format("%Y-%m-%dT%H:%M:%S"))
    }
}

impl fmt::Display for PyDateTime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.isoformat())
    }
}

impl Serialize for PyDateTime {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.isoformat())
    }
}

impl<'de> Deserialize<'de> for PyDateTime {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text = String::deserialize(deserializer)?;
        Self::parse(&text).map_err(serde::de::Error::custom)
    }
}

// ---------------------------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------------------------

fn nse() -> String {
    "NSE".to_owned()
}

/// Fields of [`InstrumentRecord`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentFields {
    /// Exchange symbol (min length 1).
    pub symbol: String,
    /// Company or index name (min length 1).
    pub name: String,
    /// EQ, ETF or INDEX.
    pub instrument_type: InstrumentType,
    /// Exchange series.
    #[serde(default)]
    pub series: Option<String>,
    /// ISIN.
    #[serde(default)]
    pub isin: Option<String>,
    /// Exchange, default `NSE`.
    #[serde(default = "nse")]
    pub exchange: String,
    /// Kite's instrument token — absent for instruments Kite does not carry.
    #[serde(default)]
    pub kite_token: Option<i64>,
    /// Lot size.
    #[serde(default)]
    pub lot_size: Option<i64>,
    /// Face value.
    #[serde(default)]
    pub face_value: Option<Decimal>,
    /// Listing date.
    #[serde(default)]
    pub listed_on: Option<NaiveDate>,
    /// Delisting date.
    #[serde(default)]
    pub delisted_on: Option<NaiveDate>,
}

impl InstrumentFields {
    /// The required fields; everything else at Python's default.
    #[must_use]
    pub fn new(symbol: &str, name: &str, instrument_type: InstrumentType) -> Self {
        Self {
            symbol: symbol.to_owned(),
            name: name.to_owned(),
            instrument_type,
            series: None,
            isin: None,
            exchange: nse(),
            kite_token: None,
            lot_size: None,
            face_value: None,
            listed_on: None,
            delisted_on: None,
        }
    }
}

fn check_instrument(f: &InstrumentFields) -> Result<(), RecordError> {
    min_length_1("InstrumentRecord", "symbol", &f.symbol)?;
    min_length_1("InstrumentRecord", "name", &f.name)
}

record!(
    /// One tradable instrument, as a provider sees it (docs/09 `list_instruments`). Ports
    /// `records.InstrumentRecord`.
    InstrumentRecord, InstrumentFields, "InstrumentFields", check_instrument
);

/// Fields of [`IndexSnapshot`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndexSnapshotFields {
    /// Index slug (min length 1).
    pub index_slug: String,
    /// Session date.
    pub date: NaiveDate,
    /// Level.
    #[serde(default)]
    pub level: Option<Decimal>,
    /// Change, absolute.
    #[serde(default)]
    pub change_abs: Option<Decimal>,
    /// Change, percent.
    #[serde(default)]
    pub change_pct: Option<Decimal>,
    /// P/E — `None` for indices that publish none (a zero would be a lie).
    #[serde(default)]
    pub pe: Option<Decimal>,
    /// P/B.
    #[serde(default)]
    pub pb: Option<Decimal>,
    /// Dividend yield.
    #[serde(default)]
    pub div_yield: Option<Decimal>,
}

impl IndexSnapshotFields {
    /// The required fields; everything else `None`.
    #[must_use]
    pub fn new(index_slug: &str, date: NaiveDate) -> Self {
        Self {
            index_slug: index_slug.to_owned(),
            date,
            level: None,
            change_abs: None,
            change_pct: None,
            pe: None,
            pb: None,
            div_yield: None,
        }
    }
}

fn check_index_snapshot(f: &IndexSnapshotFields) -> Result<(), RecordError> {
    min_length_1("IndexSnapshot", "index_slug", &f.index_slug)
}

record!(
    /// A published index level with its fundamentals (docs/09 `index_snapshots`). Ports
    /// `records.IndexSnapshot`.
    IndexSnapshot, IndexSnapshotFields, "IndexSnapshotFields", check_index_snapshot
);

/// Fields of [`CorporateAction`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CorporateActionFields {
    /// Symbol (min length 1).
    pub symbol: String,
    /// Action type.
    pub action_type: CorporateActionType,
    /// Ex-date.
    pub ex_date: NaiveDate,
    /// Ratio leg "from" (split 10:1 → 10); positive when present.
    #[serde(default)]
    pub ratio_from: Option<Decimal>,
    /// Ratio leg "to"; positive when present.
    #[serde(default)]
    pub ratio_to: Option<Decimal>,
    /// Dividend amount.
    #[serde(default)]
    pub amount: Option<Decimal>,
    /// The source payload, kept so adjustments can be re-derived.
    #[serde(default)]
    pub raw: serde_json::Map<String, serde_json::Value>,
}

impl CorporateActionFields {
    /// The required fields; everything else at Python's default.
    #[must_use]
    pub fn new(symbol: &str, action_type: CorporateActionType, ex_date: NaiveDate) -> Self {
        Self {
            symbol: symbol.to_owned(),
            action_type,
            ex_date,
            ratio_from: None,
            ratio_to: None,
            amount: None,
            raw: serde_json::Map::new(),
        }
    }
}

fn check_corporate_action(f: &CorporateActionFields) -> Result<(), RecordError> {
    min_length_1("CorporateAction", "symbol", &f.symbol)?;
    for (field, value) in [("ratio_from", f.ratio_from), ("ratio_to", f.ratio_to)] {
        if let Some(v) = value
            && v <= Decimal::ZERO
        {
            return Err(invalid(
                "CorporateAction",
                field,
                format!("Value error, corporate-action ratio legs must be positive; got {v}"),
            ));
        }
    }
    Ok(())
}

record!(
    /// A split, bonus, dividend, rights issue or demerger (docs/09 `corporate_actions`). Ports
    /// `records.CorporateAction`.
    CorporateAction, CorporateActionFields, "CorporateActionFields", check_corporate_action
);

/// Fields of [`ListingRecord`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ListingFields {
    /// Symbol (min length 1).
    pub symbol: String,
    /// Name (min length 1).
    pub name: String,
    /// Series.
    #[serde(default)]
    pub series: Option<String>,
    /// ISIN.
    #[serde(default)]
    pub isin: Option<String>,
    /// Listing date.
    #[serde(default)]
    pub listed_on: Option<NaiveDate>,
    /// Face value.
    #[serde(default)]
    pub face_value: Option<Decimal>,
    /// Paid-up value.
    #[serde(default)]
    pub paid_up_value: Option<Decimal>,
    /// Market lot.
    #[serde(default)]
    pub market_lot: Option<i64>,
}

impl ListingFields {
    /// The required fields; everything else `None`.
    #[must_use]
    pub fn new(symbol: &str, name: &str) -> Self {
        Self {
            symbol: symbol.to_owned(),
            name: name.to_owned(),
            series: None,
            isin: None,
            listed_on: None,
            face_value: None,
            paid_up_value: None,
            market_lot: None,
        }
    }
}

fn check_listing(f: &ListingFields) -> Result<(), RecordError> {
    min_length_1("ListingRecord", "symbol", &f.symbol)?;
    min_length_1("ListingRecord", "name", &f.name)
}

record!(
    /// One row of the NSE listings register. Ports `records.ListingRecord`.
    ListingRecord, ListingFields, "ListingFields", check_listing
);

/// Fields of [`EquityFundamental`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EquityFundamentalFields {
    /// Symbol (min length 1).
    pub symbol: String,
    /// Session date.
    pub date: NaiveDate,
    /// Issued shares.
    #[serde(default)]
    pub shares_outstanding: Option<i64>,
    /// Last price.
    #[serde(default)]
    pub last_price: Option<Decimal>,
    /// Market cap in rupee crore, rounded to an integer.
    #[serde(default)]
    pub marketcap_cr: Option<i64>,
    /// P/E — `None` for names without earnings, never zero.
    #[serde(default)]
    pub pe: Option<Decimal>,
    /// P/B.
    #[serde(default)]
    pub pb: Option<Decimal>,
    /// Dividend yield.
    #[serde(default)]
    pub div_yield: Option<Decimal>,
}

impl EquityFundamentalFields {
    /// The required fields; everything else `None`.
    #[must_use]
    pub fn new(symbol: &str, date: NaiveDate) -> Self {
        Self {
            symbol: symbol.to_owned(),
            date,
            shares_outstanding: None,
            last_price: None,
            marketcap_cr: None,
            pe: None,
            pb: None,
            div_yield: None,
        }
    }
}

fn check_equity_fundamental(f: &EquityFundamentalFields) -> Result<(), RecordError> {
    min_length_1("EquityFundamental", "symbol", &f.symbol)
}

record!(
    /// One NSE cash-equity quote distilled onto `fundamental_daily`. Ports
    /// `records.EquityFundamental`.
    EquityFundamental, EquityFundamentalFields, "EquityFundamentalFields", check_equity_fundamental
);

/// Fields of [`BrokerAccountRef`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BrokerAccountFields {
    /// The tenant's broker account id (> 0).
    pub broker_account_id: i64,
    /// Catalog id (`zerodha`, `upstox`, ...; min length 1).
    pub broker_id: String,
    /// The broker's own client id, when known.
    #[serde(default)]
    pub kite_user_id: Option<String>,
}

fn check_broker_account(f: &BrokerAccountFields) -> Result<(), RecordError> {
    gt_zero_int("BrokerAccountRef", "broker_account_id", f.broker_account_id)?;
    min_length_1("BrokerAccountRef", "broker_id", &f.broker_id)
}

record!(
    /// Which tenant's broker account a read is for. Ports `records.BrokerAccountRef`.
    BrokerAccountRef, BrokerAccountFields, "BrokerAccountFields", check_broker_account
);

/// Fields of [`QuoteRecord`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuoteFields {
    /// Symbol (min length 1).
    pub symbol: String,
    /// Exchange, default `NSE`.
    #[serde(default = "nse")]
    pub exchange: String,
    /// Kite's instrument token.
    #[serde(default)]
    pub instrument_token: Option<i64>,
    /// The print (pre-open: the indicative equilibrium price).
    pub last_price: Decimal,
    /// The session's volume so far.
    #[serde(default)]
    pub volume: i64,
    /// The exchange's own previous close.
    #[serde(default)]
    pub prev_close: Option<Decimal>,
    /// Open.
    #[serde(default)]
    pub open: Option<Decimal>,
    /// High.
    #[serde(default)]
    pub high: Option<Decimal>,
    /// Low.
    #[serde(default)]
    pub low: Option<Decimal>,
    /// Upper circuit.
    #[serde(default)]
    pub upper_circuit: Option<Decimal>,
    /// Lower circuit.
    #[serde(default)]
    pub lower_circuit: Option<Decimal>,
    /// The exchange's timestamp for the print.
    #[serde(default)]
    pub as_of: Option<PyDateTime>,
}

impl QuoteFields {
    /// The required fields; everything else at Python's default.
    #[must_use]
    pub fn new(symbol: &str, last_price: Decimal) -> Self {
        Self {
            symbol: symbol.to_owned(),
            exchange: nse(),
            instrument_token: None,
            last_price,
            volume: 0,
            prev_close: None,
            open: None,
            high: None,
            low: None,
            upper_circuit: None,
            lower_circuit: None,
            as_of: None,
        }
    }
}

fn check_quote(f: &QuoteFields) -> Result<(), RecordError> {
    min_length_1("QuoteRecord", "symbol", &f.symbol)
}

record!(
    /// One instrument's live quote (docs/swing/06 SW6). Ports `records.QuoteRecord`.
    QuoteRecord, QuoteFields, "QuoteFields", check_quote
);

fn nse_announcement() -> String {
    "NSE_ANNOUNCEMENT".to_owned()
}

/// Fields of [`CatalystRecord`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalystFields {
    /// Symbol (min length 1).
    pub symbol: String,
    /// NSE's subject line (min length 1) — never the filing's text.
    pub headline: String,
    /// IST-stamped publication time, when NSE stamps it.
    #[serde(default)]
    pub published_at: Option<PyDateTime>,
    /// The filing attachment, verbatim (min length 1) — the uniqueness key.
    pub url: String,
    /// Source, default `NSE_ANNOUNCEMENT`.
    #[serde(default = "nse_announcement")]
    pub source: String,
}

fn check_catalyst(f: &CatalystFields) -> Result<(), RecordError> {
    min_length_1("CatalystRecord", "symbol", &f.symbol)?;
    min_length_1("CatalystRecord", "headline", &f.headline)?;
    min_length_1("CatalystRecord", "url", &f.url)
}

record!(
    /// One NSE corporate announcement: a headline, a timestamp and a link (SW11B, A3). Ports
    /// `records.CatalystRecord`.
    CatalystRecord, CatalystFields, "CatalystFields", check_catalyst
);

/// Fields of [`EarningsDateRecord`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EarningsDateFields {
    /// Symbol (min length 1).
    pub symbol: String,
    /// The board-meeting date NSE lists.
    pub event_date: NaiveDate,
    /// NSE's own wording (min length 1).
    pub purpose: String,
    /// The calendar page (min length 1).
    pub url: String,
}

fn check_earnings_date(f: &EarningsDateFields) -> Result<(), RecordError> {
    min_length_1("EarningsDateRecord", "symbol", &f.symbol)?;
    min_length_1("EarningsDateRecord", "purpose", &f.purpose)?;
    min_length_1("EarningsDateRecord", "url", &f.url)
}

record!(
    /// One board-meeting entry whose purpose is a financial result. Ports
    /// `records.EarningsDateRecord`.
    EarningsDateRecord, EarningsDateFields, "EarningsDateFields", check_earnings_date
);

fn cnc() -> String {
    "CNC".to_owned()
}

/// Fields of [`BrokerHoldingRecord`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BrokerHoldingFields {
    /// Symbol (min length 1).
    pub symbol: String,
    /// Exchange, default `NSE`.
    #[serde(default = "nse")]
    pub exchange: String,
    /// ISIN.
    #[serde(default)]
    pub isin: Option<String>,
    /// The settled, freely sellable quantity (non-negative).
    #[serde(default)]
    pub quantity: Decimal,
    /// Bought yesterday, not yet settled (non-negative).
    #[serde(default)]
    pub t1_quantity: Decimal,
    /// Pledged for margin (non-negative; sells directly — non-negotiable #3).
    #[serde(default)]
    pub collateral_quantity: Decimal,
    /// Cost basis — `None` when the broker publishes none, never a plausible zero.
    #[serde(default)]
    pub average_price: Option<Decimal>,
    /// Last price.
    #[serde(default)]
    pub last_price: Option<Decimal>,
    /// Product, default `CNC`.
    #[serde(default = "cnc")]
    pub product: String,
}

impl BrokerHoldingFields {
    /// The required field; everything else at Python's default.
    #[must_use]
    pub fn new(symbol: &str) -> Self {
        Self {
            symbol: symbol.to_owned(),
            exchange: nse(),
            isin: None,
            quantity: Decimal::ZERO,
            t1_quantity: Decimal::ZERO,
            collateral_quantity: Decimal::ZERO,
            average_price: None,
            last_price: None,
            product: cnc(),
        }
    }
}

fn check_broker_holding(f: &BrokerHoldingFields) -> Result<(), RecordError> {
    min_length_1("BrokerHoldingRecord", "symbol", &f.symbol)?;
    for (field, v) in [
        ("quantity", f.quantity),
        ("t1_quantity", f.t1_quantity),
        ("collateral_quantity", f.collateral_quantity),
    ] {
        if v < Decimal::ZERO {
            return Err(invalid(
                "BrokerHoldingRecord",
                field,
                format!("Value error, a holding quantity cannot be negative; got {v}"),
            ));
        }
    }
    for (field, v) in [
        ("average_price", f.average_price),
        ("last_price", f.last_price),
    ] {
        if let Some(v) = v
            && v < Decimal::ZERO
        {
            return Err(invalid(
                "BrokerHoldingRecord",
                field,
                format!("Value error, a price cannot be negative; got {v}"),
            ));
        }
    }
    Ok(())
}

record!(
    /// One equity position as a broker reports it. Ports `records.BrokerHoldingRecord`.
    ///
    /// **Quantity is three numbers, never one** (desk non-negotiable #2); the sum is
    /// [`BrokerHoldingRecord::total_quantity`] and it is the only number the ledger is shown.
    BrokerHoldingRecord, BrokerHoldingFields, "BrokerHoldingFields", check_broker_holding
);

impl BrokerHoldingRecord {
    /// `total_quantity`: `quantity + t1_quantity + collateral_quantity`.
    #[must_use]
    pub fn total_quantity(&self) -> Decimal {
        self.quantity + self.t1_quantity + self.collateral_quantity
    }
}

/// Fields of [`OptionContractRecord`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OptionContractFields {
    /// Kite's instrument token (> 0).
    pub instrument_token: i64,
    /// Trading symbol (min length 1).
    pub tradingsymbol: String,
    /// Underlying (min length 1).
    pub underlying: String,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Strike (> 0).
    pub strike: Decimal,
    /// CE or PE.
    pub option_type: OptionType,
    /// Lot size (> 0).
    pub lot_size: i64,
    /// Tick size (> 0).
    pub tick_size: Decimal,
}

fn check_option_contract(f: &OptionContractFields) -> Result<(), RecordError> {
    const R: &str = "OptionContractRecord";
    gt_zero_int(R, "instrument_token", f.instrument_token)?;
    min_length_1(R, "tradingsymbol", &f.tradingsymbol)?;
    min_length_1(R, "underlying", &f.underlying)?;
    gt_zero_dec(R, "strike", f.strike)?;
    gt_zero_int(R, "lot_size", f.lot_size)?;
    gt_zero_dec(R, "tick_size", f.tick_size)
}

record!(
    /// One NFO option contract, as Kite's instrument dump states it (OP2). Ports
    /// `records.OptionContractRecord`.
    OptionContractRecord, OptionContractFields, "OptionContractFields", check_option_contract
);

/// Fields of [`DepthLevelRecord`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DepthLevelFields {
    /// Price (> 0; Kite's zero padding is dropped at the boundary).
    pub price: Decimal,
    /// Quantity in units (>= 0).
    pub quantity: i64,
    /// Orders resting (>= 0).
    #[serde(default)]
    pub orders: i64,
}

fn check_depth_level(f: &DepthLevelFields) -> Result<(), RecordError> {
    gt_zero_dec("DepthLevelRecord", "price", f.price)?;
    ge_zero_int("DepthLevelRecord", "quantity", f.quantity)?;
    ge_zero_int("DepthLevelRecord", "orders", f.orders)
}

record!(
    /// One level of Kite's five-a-side depth (OP3). Ports `records.DepthLevelRecord`.
    DepthLevelRecord, DepthLevelFields, "DepthLevelFields", check_depth_level
);

/// Fields of [`OptionQuoteRecord`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OptionQuoteFields {
    /// Symbol (min length 1).
    pub symbol: String,
    /// Exchange (min length 1).
    pub exchange: String,
    /// Kite's instrument token.
    #[serde(default)]
    pub instrument_token: Option<i64>,
    /// Last price.
    #[serde(default)]
    pub last_price: Option<Decimal>,
    /// Volume.
    #[serde(default)]
    pub volume: i64,
    /// Open interest, as Kite reports it.
    #[serde(default)]
    pub oi: Option<i64>,
    /// OI day high.
    #[serde(default)]
    pub oi_day_high: Option<i64>,
    /// OI day low.
    #[serde(default)]
    pub oi_day_low: Option<i64>,
    /// Bid levels.
    #[serde(default)]
    pub bids: Vec<DepthLevelRecord>,
    /// Ask levels.
    #[serde(default)]
    pub asks: Vec<DepthLevelRecord>,
    /// Kite's `timestamp`.
    #[serde(default)]
    pub as_of: Option<PyDateTime>,
    /// Kite's `last_trade_time`.
    #[serde(default)]
    pub last_trade_time: Option<PyDateTime>,
}

fn check_option_quote(f: &OptionQuoteFields) -> Result<(), RecordError> {
    min_length_1("OptionQuoteRecord", "symbol", &f.symbol)?;
    min_length_1("OptionQuoteRecord", "exchange", &f.exchange)
}

record!(
    /// One `/quote` entry with depth, OI and both timestamps (OP3). Ports
    /// `records.OptionQuoteRecord`.
    OptionQuoteRecord, OptionQuoteFields, "OptionQuoteFields", check_option_quote
);

impl OptionQuoteRecord {
    /// `EXCHANGE:SYMBOL`, the form Kite keys a quote by.
    #[must_use]
    pub fn key(&self) -> String {
        format!("{}:{}", self.exchange, self.symbol)
    }
}

/// Fields of [`MinuteBarRecord`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MinuteBarFields {
    /// The minute's start — must be timezone-aware.
    pub ts: PyDateTime,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
    /// Volume.
    #[serde(default)]
    pub volume: i64,
}

fn check_minute_bar(f: &MinuteBarFields) -> Result<(), RecordError> {
    if f.ts.aware().is_none() {
        return Err(invalid(
            "MinuteBarRecord",
            "ts",
            "Value error, a minute bar's timestamp must be timezone-aware",
        ));
    }
    Ok(())
}

record!(
    /// One one-minute candle (OP3). Ports `records.MinuteBarRecord`.
    MinuteBarRecord, MinuteBarFields, "MinuteBarFields", check_minute_bar
);

/// Fields of [`MarginLegRecord`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarginLegFields {
    /// Exchange (min length 1).
    pub exchange: String,
    /// Trading symbol (min length 1).
    pub tradingsymbol: String,
    /// BUY or SELL.
    pub transaction_type: TransactionType,
    /// Quantity (> 0).
    pub quantity: i64,
    /// Always MIS.
    #[serde(default)]
    pub product: MarginProduct,
    /// LIMIT or MARKET.
    #[serde(default)]
    pub order_type: MarginOrderType,
    /// Always `regular`.
    #[serde(default)]
    pub variety: MarginVariety,
    /// Price, default 0.
    #[serde(default)]
    pub price: Decimal,
}

fn check_margin_leg(f: &MarginLegFields) -> Result<(), RecordError> {
    min_length_1("MarginLegRecord", "exchange", &f.exchange)?;
    min_length_1("MarginLegRecord", "tradingsymbol", &f.tradingsymbol)?;
    gt_zero_int("MarginLegRecord", "quantity", f.quantity)
}

record!(
    /// One leg of a basket whose margin is *asked about* — never an order (law 2). Ports
    /// `records.MarginLegRecord`.
    MarginLegRecord, MarginLegFields, "MarginLegFields", check_margin_leg
);

/// Fields of [`BasketMarginRecord`].
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BasketMarginFields {
    /// Sum of each leg alone.
    #[serde(default)]
    pub initial_total: Option<Decimal>,
    /// What the basket needs once Kite nets it.
    #[serde(default)]
    pub final_total: Option<Decimal>,
    /// Leg count.
    #[serde(default)]
    pub legs: i64,
    /// The response's top-level keys and each total block's keys (sorted by key; Python's
    /// dict keeps insertion order — DECISIONS-RS R4.6).
    #[serde(default)]
    pub shape: BTreeMap<String, Vec<String>>,
}

#[allow(clippy::unnecessary_wraps)]
const fn check_basket_margin(_: &BasketMarginFields) -> Result<(), RecordError> {
    Ok(())
}

record!(
    /// Kite's answer for a basket's margin. Ports `records.BasketMarginRecord`.
    BasketMarginRecord, BasketMarginFields, "BasketMarginFields", check_basket_margin
);

// ---------------------------------------------------------------------------------------------
// Frame schemas
// ---------------------------------------------------------------------------------------------

/// `pl.Decimal(18, 4)`.
#[must_use]
pub const fn price_dtype() -> DataType {
    DataType::Decimal(18, 4)
}

fn schema(columns: &[(&str, DataType)]) -> Schema {
    columns
        .iter()
        .map(|(name, dtype)| Field::new(PlSmallStr::from(*name), dtype.clone()))
        .collect()
}

/// `DAILY_BARS_SCHEMA`: raw daily candles — Kite's prints are unadjusted (docs/09).
#[must_use]
pub fn daily_bars_schema() -> Schema {
    schema(&[
        ("symbol", DataType::String),
        ("date", DataType::Date),
        ("open", price_dtype()),
        ("high", price_dtype()),
        ("low", price_dtype()),
        ("close", price_dtype()),
        ("volume", DataType::Int64),
        ("source", DataType::String),
    ])
}

/// `BHAVCOPY_SCHEMA`: one day's cash bhavcopy, incl. series and circuit bands.
#[must_use]
pub fn bhavcopy_schema() -> Schema {
    schema(&[
        ("symbol", DataType::String),
        ("series", DataType::String),
        ("date", DataType::Date),
        ("open", price_dtype()),
        ("high", price_dtype()),
        ("low", price_dtype()),
        ("close", price_dtype()),
        ("prev_close", price_dtype()),
        ("volume", DataType::Int64),
        ("turnover", DataType::Decimal(20, 2)),
        ("trades", DataType::Int64),
        ("upper_circuit", price_dtype()),
        ("lower_circuit", price_dtype()),
    ])
}

/// `FO_BHAVCOPY_SCHEMA`: one day's F&O bhavcopy, one row per contract.
#[must_use]
pub fn fo_bhavcopy_schema() -> Schema {
    schema(&[
        ("date", DataType::Date),
        ("instrument", DataType::String),
        ("symbol", DataType::String),
        ("expiry", DataType::Date),
        ("strike", price_dtype()),
        ("option_type", DataType::String),
        ("open", price_dtype()),
        ("high", price_dtype()),
        ("low", price_dtype()),
        ("close", price_dtype()),
        ("settle", price_dtype()),
        ("underlying", price_dtype()),
        ("open_interest", DataType::Int64),
        ("oi_change", DataType::Int64),
        ("volume", DataType::Int64),
        ("turnover", DataType::Decimal(20, 2)),
        ("lot_size", DataType::Int64),
    ])
}

/// `empty_frame`: an empty frame that still satisfies `schema` — "no data" is never a schema
/// change.
#[must_use]
pub fn empty_frame(schema: &Schema) -> DataFrame {
    DataFrame::empty_with_schema(schema)
}

fn py_list(items: &[String]) -> String {
    let inner: Vec<String> = items.iter().map(|s| repr_str(s)).collect();
    format!("[{}]", inner.join(", "))
}

/// `conform`: project `frame` onto `schema` with a strict cast of every column, refusing a
/// missing one — a provider that starts sending floats for prices cannot poison `numeric`.
///
/// # Errors
/// `ValueError` (Python's message) for missing columns; polars' `InvalidOperationError` when a
/// strict cast fails.
pub fn conform(frame: &DataFrame, schema: &Schema) -> Result<DataFrame, ProviderError> {
    let present: Vec<String> = frame
        .get_column_names()
        .iter()
        .map(|n| n.to_string())
        .collect();
    let missing: Vec<String> = schema
        .iter_names()
        .map(ToString::to_string)
        .filter(|n| !present.contains(n))
        .collect();
    if !missing.is_empty() {
        return Err(ProviderError::value_error(format!(
            "frame is missing required columns {}; got {}",
            py_list(&missing),
            py_list(&present)
        )));
    }
    let exprs: Vec<_> = schema
        .iter()
        .map(|(name, dtype)| {
            col(name.clone())
                .strict_cast(dtype.clone())
                .alias(name.clone())
        })
        .collect();
    frame
        .clone()
        .lazy()
        .select(exprs)
        .collect()
        .map_err(|e| ProviderError::foreign("InvalidOperationError", e.to_string()))
}
