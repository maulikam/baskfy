//! `FixtureProvider` — both ports, served from the fixture frames, zero network (Prompt 2
//! deliverable 5). Ports `baskfy_providers.fixtures`.
//!
//! docs/09's table: "`FixtureProvider` — local dev/tests; reads Parquet fixtures, zero network."
//! docs/03 §Environments: the `local` environment is a sample universe "seeded from fixtures",
//! with "no Kite calls, provider stubbed". It implements [`BarsProvider`] *and*
//! [`ReferenceProvider`], so a local stack or a test can wire one object where production wires a
//! composite over two vendors.
//!
//! **Parquet.** The fixture files are Parquet, and this crate's polars does not declare the
//! `parquet` feature (DECISIONS-RS R4.2 names that as a later, separate entry). Until it does, a
//! fixture *file* is reported unreadable — `check()` says so and every read is
//! `ProviderUnavailable` — and the provider serves frames handed to it in memory
//! ([`FixtureProvider::from_frames`]). Everything above the file read is Python's, and is tested.
//!
//! [`FixtureHoldingsProvider`] is the holdings double: a separate object that the production
//! factory deliberately does **not** register, because invented positions served as real ones
//! are the worst failure this repository can have.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use async_trait::async_trait;
use chrono::NaiveDate;
use polars::prelude::{DataFrame, Expr, IntoLazy, SortMultipleOptions, col, lit};
use rust_decimal::Decimal;

use crate::composite::SupplementaryReads;
use crate::errors::ProviderError;
use crate::nse::{PyValue, Row, frame_rows, off_runtime, validation};
use crate::ports::{
    BarsProvider, CapabilitySet, HealthReporting, HoldingsProvider, ProviderHealth,
    ReferenceProvider, bars_capabilities, holdings_capabilities, reference_capabilities,
};
use crate::py::repr_str;
use crate::records::{
    BrokerAccountRef, BrokerHoldingRecord, CorporateAction, CorporateActionFields,
    CorporateActionType, EquityFundamental, IndexSnapshot, IndexSnapshotFields, InstrumentFields,
    InstrumentRecord, InstrumentType, ListingFields, ListingRecord, bhavcopy_schema, conform,
    daily_bars_schema, empty_frame,
};

/// `PROVIDER_NAME`.
pub const PROVIDER_NAME: &str = "fixture";

/// Deliberately a *different* name from [`FixtureProvider`]'s: the composite refuses two adapters
/// with one name, and every line that mentions invented holdings should say so in its own words.
pub const HOLDINGS_PROVIDER_NAME: &str = "fixture-holdings";

/// `_FILES`: frame name -> fixture file, in Python's order.
pub const FILES: [(&str, &str); 7] = [
    ("instruments", "instruments.parquet"),
    ("daily_bars", "daily_bars.parquet"),
    ("corporate_actions", "corporate_actions.parquet"),
    ("index_members", "index_members.parquet"),
    ("index_snapshots", "index_snapshots.parquet"),
    ("listings", "listings.parquet"),
    ("bhavcopy", "bhavcopy.parquet"),
];

const REGENERATE: &str = "regenerate with `python -m baskfy_providers.fixture_builder`";

/// `default_fixture_dir`: `tests/fixtures/providers`, located by walking up from this crate (the
/// Python walks up from its own file; the fixtures live in `decile-blueprint/tests`).
///
/// # Errors
/// `FileNotFoundError` when no ancestor holds the directory.
pub fn default_fixture_dir() -> Result<PathBuf, ProviderError> {
    let here = Path::new(env!("CARGO_MANIFEST_DIR"));
    for parent in here.ancestors() {
        for candidate in [
            parent.join("tests").join("fixtures").join("providers"),
            parent
                .join("decile-blueprint")
                .join("tests")
                .join("fixtures")
                .join("providers"),
        ] {
            if candidate.is_dir() {
                return Ok(candidate);
            }
        }
    }
    Err(ProviderError::foreign(
        "FileNotFoundError",
        "no tests/fixtures/providers directory found above this file",
    ))
}

/// Serves both provider ports from the fixture frames (`FixtureProvider`).
pub struct FixtureProvider {
    dir: PathBuf,
    frames: HashMap<String, DataFrame>,
    token_map: Mutex<Option<Vec<(i64, String)>>>,
}

impl std::fmt::Debug for FixtureProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FixtureProvider")
            .field("dir", &self.dir)
            .finish_non_exhaustive()
    }
}

fn file_for(name: &str) -> &'static str {
    FILES
        .iter()
        .find(|(n, _)| *n == name)
        .map_or("", |(_, f)| f)
}

impl FixtureProvider {
    /// `FixtureProvider(fixture_dir)`: the files in `dir`.
    #[must_use]
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self {
            dir: dir.into(),
            frames: HashMap::new(),
            token_map: Mutex::new(None),
        }
    }

    /// The provider over frames already in memory, keyed by [`FILES`]' names; a name with no
    /// frame falls back to its file in `dir`.
    #[must_use]
    pub fn from_frames(dir: impl Into<PathBuf>, frames: HashMap<String, DataFrame>) -> Self {
        Self {
            dir: dir.into(),
            frames,
            token_map: Mutex::new(None),
        }
    }

    /// `directory`.
    #[must_use]
    pub fn directory(&self) -> &Path {
        &self.dir
    }

    fn present(&self, name: &str) -> bool {
        self.frames.contains_key(name) || self.dir.join(file_for(name)).is_file()
    }

    /// `_frame`: one fixture frame.
    fn frame(&self, name: &str) -> Result<DataFrame, ProviderError> {
        if let Some(frame) = self.frames.get(name) {
            return Ok(frame.clone());
        }
        let path = self.dir.join(file_for(name));
        if !path.is_file() {
            return Err(ProviderError::provider_unavailable(format!(
                "fixture {} is missing; {REGENERATE}",
                path.display()
            ))
            .with_provider(PROVIDER_NAME));
        }
        Err(ProviderError::provider_unavailable(format!(
            "fixture {} is Parquet, and baskfy-providers is built without polars' `parquet` \
             feature (DECISIONS-RS R4.12); serve the frames in memory until it is declared",
            path.display()
        ))
        .with_provider(PROVIDER_NAME))
    }

    fn rows(&self, frame: &DataFrame) -> Result<Vec<Row>, ProviderError> {
        frame_rows(frame)
    }

    /// `daily_bars_for_symbol`: convenience for seeding and tests, which think in symbols.
    ///
    /// # Errors
    /// `UnexpectedPayload` for a symbol no instrument carries; as [`BarsProvider::daily_bars`].
    pub async fn daily_bars_for_symbol(
        &self,
        symbol: &str,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        let token = self.token_for_symbol(symbol)?;
        self.bars(token, start, end).await
    }

    async fn bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        if end < start {
            return Err(ProviderError::value_error(format!(
                "end {} precedes start {}",
                end.format("%Y-%m-%d"),
                start.format("%Y-%m-%d")
            )));
        }
        let symbol = self.symbol_for_token(token)?;
        let bars = self.frame("daily_bars")?;
        off_runtime(move || {
            let frame = bars
                .lazy()
                .filter(
                    col("symbol")
                        .eq(lit(symbol))
                        .and(col("date").gt_eq(lit(start)))
                        .and(col("date").lt_eq(lit(end))),
                )
                .sort(["date"], SortMultipleOptions::default())
                .collect()
                .map_err(|e| ProviderError::foreign("ComputeError", e.to_string()))?;
            if frame.height() == 0 {
                return Ok(empty_frame(&daily_bars_schema()));
            }
            conform(&frame, &daily_bars_schema())
        })
        .await
    }

    /// `_token_map` (a cached property): `kite_token -> symbol`, later rows overwriting earlier
    /// ones in place, as a dict comprehension does.
    fn token_map(&self) -> Result<Vec<(i64, String)>, ProviderError> {
        let mut cached = self
            .token_map
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Some(map) = cached.as_ref() {
            return Ok(map.clone());
        }
        let mut map: Vec<(i64, String)> = Vec::new();
        for row in self.rows(&self.frame("instruments")?)? {
            let token = row.get("kite_token");
            if matches!(token, PyValue::None) {
                continue;
            }
            let Some(token) = py_int(token)? else {
                continue;
            };
            let symbol = row.get("symbol").py_str();
            match map.iter_mut().find(|(t, _)| *t == token) {
                Some(entry) => entry.1 = symbol,
                None => map.push((token, symbol)),
            }
        }
        *cached = Some(map.clone());
        Ok(map)
    }

    fn symbol_for_token(&self, token: i64) -> Result<String, ProviderError> {
        self.token_map()?
            .into_iter()
            .find(|(t, _)| *t == token)
            .map(|(_, s)| s)
            .ok_or_else(|| {
                ProviderError::unexpected_payload(format!(
                    "no fixture instrument carries kite_token {token}"
                ))
                .with_provider(PROVIDER_NAME)
            })
    }

    fn token_for_symbol(&self, symbol: &str) -> Result<i64, ProviderError> {
        self.token_map()?
            .into_iter()
            .find(|(_, s)| s == symbol)
            .map(|(t, _)| t)
            .ok_or_else(|| {
                ProviderError::unexpected_payload(format!(
                    "no fixture instrument named {}",
                    repr_str(symbol)
                ))
                .with_provider(PROVIDER_NAME)
            })
    }

    async fn query(
        &self,
        name: &str,
        predicate: Expr,
        sort: &[&str],
    ) -> Result<DataFrame, ProviderError> {
        let frame = self.frame(name)?;
        let sort: Vec<String> = sort.iter().map(|s| (*s).to_owned()).collect();
        off_runtime(move || {
            let mut lazy = frame.lazy().filter(predicate);
            if !sort.is_empty() {
                lazy = lazy.sort(sort, SortMultipleOptions::default());
            }
            lazy.collect()
                .map_err(|e| ProviderError::foreign("ComputeError", e.to_string()))
        })
        .await
    }
}

/// `_text`.
fn text(value: &PyValue) -> Option<String> {
    if matches!(value, PyValue::None) {
        return None;
    }
    let s = value.py_str();
    let t = crate::nse::py_strip(&s);
    (!t.is_empty()).then(|| t.to_owned())
}

/// `_int`: `int(value)` for numbers, `int(float(value))` for text.
fn py_int(value: &PyValue) -> Result<Option<i64>, ProviderError> {
    match value {
        PyValue::None => Ok(None),
        PyValue::Bool(b) => Ok(Some(i64::from(*b))),
        PyValue::Int(i) => i64::try_from(*i)
            .map(Some)
            .map_err(|_| ProviderError::foreign("OverflowError", format!("{i} beyond i64"))),
        PyValue::Float(f) => float_to_int(*f).map(Some),
        PyValue::Decimal(d) => i64::try_from(d.trunc())
            .map(Some)
            .map_err(|_| ProviderError::foreign("OverflowError", format!("{d} beyond i64"))),
        PyValue::Str(s) => match s.trim().parse::<f64>() {
            Ok(f) => float_to_int(f).map(Some),
            Err(_) => Err(ProviderError::value_error(format!(
                "could not convert string to float: {}",
                repr_str(s)
            ))),
        },
        _ => Ok(None),
    }
}

fn float_to_int(f: f64) -> Result<i64, ProviderError> {
    if f.is_nan() {
        return Err(ProviderError::value_error(
            "cannot convert float NaN to integer",
        ));
    }
    #[allow(clippy::cast_precision_loss)]
    let limit = i64::MAX as f64;
    if f.is_infinite() || f.trunc() >= limit || f.trunc() < -limit {
        return Err(ProviderError::foreign(
            "OverflowError",
            "cannot convert float infinity to integer",
        ));
    }
    #[allow(clippy::cast_possible_truncation)]
    Ok(f.trunc() as i64)
}

/// `_decimal`: a `Decimal` as it is; a number or text through `Decimal(str(value))`.
fn py_decimal(value: &PyValue) -> Result<Option<Decimal>, ProviderError> {
    match value {
        PyValue::None => Ok(None),
        PyValue::Decimal(d) => Ok(Some(*d)),
        PyValue::Int(_) | PyValue::Float(_) | PyValue::Str(_) | PyValue::Bool(_) => {
            match crate::nse::py_decimal(&value.py_str()) {
                crate::nse::PyDecimal::Finite(d) => Ok(Some(d)),
                crate::nse::PyDecimal::Invalid => Err(ProviderError::foreign(
                    "InvalidOperation",
                    "[<class 'decimal.ConversionSyntax'>]",
                )),
                crate::nse::PyDecimal::NonFinite => Err(ProviderError::foreign(
                    "ValidationError",
                    "Input should be a finite number",
                )),
                crate::nse::PyDecimal::TooWide => Err(ProviderError::foreign(
                    "OverflowError",
                    "beyond the 28 digits a rust_decimal holds (DECISIONS-RS R1.3)",
                )),
            }
        }
        _ => Ok(None),
    }
}

/// `_date`: a date as it is; ISO text through `date.fromisoformat`.
fn py_date(value: &PyValue) -> Result<Option<NaiveDate>, ProviderError> {
    match value {
        PyValue::Date(d) => Ok(Some(*d)),
        PyValue::Str(s) => NaiveDate::parse_from_str(s, "%Y-%m-%d")
            .map(Some)
            .map_err(|_| {
                ProviderError::value_error(format!("Invalid isoformat string: {}", repr_str(s)))
            }),
        _ => Ok(None),
    }
}

/// A `Literal` field: pydantic's check, as `ValidationError`.
fn literal<T: serde::de::DeserializeOwned>(
    value: &PyValue,
    field: &str,
) -> Result<T, ProviderError> {
    serde_json::from_value(serde_json::Value::String(value.py_str()))
        .map_err(|e| ProviderError::foreign("ValidationError", format!("{field}: {e}")))
}

impl HealthReporting for FixtureProvider {
    fn name(&self) -> &str {
        PROVIDER_NAME
    }

    /// Everything — which is what makes it a drop-in for the whole composite locally.
    fn capabilities(&self) -> CapabilitySet {
        bars_capabilities()
            .into_iter()
            .chain(reference_capabilities())
            .collect()
    }

    fn check(&self) -> ProviderHealth {
        let missing: Vec<String> = FILES
            .iter()
            .filter(|(name, _)| !self.present(name))
            .map(|(name, _)| repr_str(name))
            .collect();
        if !missing.is_empty() {
            return ProviderHealth {
                name: PROVIDER_NAME.to_owned(),
                available: false,
                capabilities: self.capabilities(),
                detail: format!(
                    "missing fixture files [{}] in {}; {REGENERATE}",
                    missing.join(", "),
                    self.dir.display()
                ),
            };
        }
        let counted = (|| -> Result<(usize, usize), ProviderError> {
            let instruments = self.frame("instruments")?.height();
            let days = self
                .frame("daily_bars")?
                .column("date")
                .and_then(|c| c.n_unique())
                .map_err(|e| ProviderError::foreign("ComputeError", e.to_string()))?;
            Ok((instruments, days))
        })();
        match counted {
            Ok((instruments, days)) => ProviderHealth {
                name: PROVIDER_NAME.to_owned(),
                available: true,
                capabilities: self.capabilities(),
                detail: format!(
                    "{instruments} instruments over {days} trading days from {}",
                    self.dir.display()
                ),
            },
            Err(e) => ProviderHealth {
                name: PROVIDER_NAME.to_owned(),
                available: false,
                capabilities: self.capabilities(),
                detail: e.to_string(),
            },
        }
    }
}

#[async_trait]
impl BarsProvider for FixtureProvider {
    async fn list_instruments(&self) -> Result<Vec<InstrumentRecord>, ProviderError> {
        let mut out = Vec::new();
        for row in self.rows(&self.frame("instruments")?)? {
            let instrument_type: InstrumentType =
                literal(row.get("instrument_type"), "instrument_type")?;
            let symbol = row.get("symbol").py_str();
            let name = row.get("name").py_str();
            let mut fields = InstrumentFields::new(&symbol, &name, instrument_type);
            fields.series = text(row.get("series"));
            fields.isin = text(row.get("isin"));
            let exchange = row.get("exchange");
            fields.exchange = if exchange.truthy() {
                exchange.py_str()
            } else {
                "NSE".to_owned()
            };
            fields.kite_token = py_int(row.get("kite_token"))?;
            fields.lot_size = py_int(row.get("lot_size"))?;
            fields.listed_on = py_date(row.get("listed_on"))?;
            out.push(InstrumentRecord::new(fields).map_err(validation)?);
        }
        Ok(out)
    }

    /// Bars for the instrument carrying `token`, addressed exactly as Kite would be.
    async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        self.bars(token, start, end).await
    }
}

#[async_trait]
impl ReferenceProvider for FixtureProvider {
    async fn index_constituents(
        &self,
        index_slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        let frame = self
            .query(
                "index_members",
                col("index_slug")
                    .eq(lit(index_slug))
                    .and(col("date").eq(lit(on))),
                &[],
            )
            .await?;
        let mut symbols: Vec<String> = self
            .rows(&frame)?
            .iter()
            .map(|r| r.get("symbol").py_str())
            .collect();
        symbols.sort();
        Ok(symbols)
    }

    async fn index_snapshots(&self, on: NaiveDate) -> Result<Vec<IndexSnapshot>, ProviderError> {
        let frame = self
            .query("index_snapshots", col("date").eq(lit(on)), &[])
            .await?;
        let mut out = Vec::new();
        for row in self.rows(&frame)? {
            let mut fields = IndexSnapshotFields::new(&row.get("index_slug").py_str(), on);
            fields.level = py_decimal(row.get("level"))?;
            fields.change_abs = py_decimal(row.get("change_abs"))?;
            fields.change_pct = py_decimal(row.get("change_pct"))?;
            fields.pe = py_decimal(row.get("pe"))?;
            fields.pb = py_decimal(row.get("pb"))?;
            fields.div_yield = py_decimal(row.get("div_yield"))?;
            out.push(IndexSnapshot::new(fields).map_err(validation)?);
        }
        Ok(out)
    }

    async fn corporate_actions(
        &self,
        since: NaiveDate,
    ) -> Result<Vec<CorporateAction>, ProviderError> {
        let frame = self
            .query(
                "corporate_actions",
                col("ex_date").gt_eq(lit(since)),
                &["symbol", "ex_date"],
            )
            .await?;
        let mut out = Vec::new();
        for row in self.rows(&frame)? {
            let action_type: CorporateActionType = literal(row.get("action_type"), "action_type")?;
            let Some(ex_date) = py_date(row.get("ex_date"))? else {
                return Err(
                    ProviderError::unexpected_payload("fixture row has no ex_date")
                        .with_provider(PROVIDER_NAME),
                );
            };
            let mut fields =
                CorporateActionFields::new(&row.get("symbol").py_str(), action_type, ex_date);
            fields.ratio_from = py_decimal(row.get("ratio_from"))?;
            fields.ratio_to = py_decimal(row.get("ratio_to"))?;
            fields.amount = py_decimal(row.get("amount"))?;
            let or_empty = |key: &str| {
                let v = row.get(key);
                if v.truthy() {
                    v.py_str()
                } else {
                    String::new()
                }
            };
            fields.raw = serde_json::Map::from_iter([
                (
                    "purpose".to_owned(),
                    serde_json::Value::String(or_empty("purpose")),
                ),
                (
                    "provenance".to_owned(),
                    serde_json::Value::String(or_empty("provenance")),
                ),
            ]);
            out.push(CorporateAction::new(fields).map_err(validation)?);
        }
        Ok(out)
    }

    async fn listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        let mut out = Vec::new();
        for row in self.rows(&self.frame("listings")?)? {
            let mut fields =
                ListingFields::new(&row.get("symbol").py_str(), &row.get("name").py_str());
            fields.series = text(row.get("series"));
            fields.isin = text(row.get("isin"));
            fields.listed_on = py_date(row.get("listed_on"))?;
            fields.face_value = py_decimal(row.get("face_value"))?;
            fields.paid_up_value = py_decimal(row.get("paid_up_value"))?;
            fields.market_lot = py_int(row.get("market_lot"))?;
            out.push(ListingRecord::new(fields).map_err(validation)?);
        }
        Ok(out)
    }

    async fn bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        let frame = self
            .query("bhavcopy", col("date").eq(lit(on)), &["symbol"])
            .await?;
        if frame.height() == 0 {
            return Ok(empty_frame(&bhavcopy_schema()));
        }
        off_runtime(move || conform(&frame, &bhavcopy_schema())).await
    }

    /// Fixtures carry no issued-capital series. NULL is the honest local-dev answer.
    async fn equity_fundamentals(
        &self,
        _on: NaiveDate,
        _symbols: &[String],
        _series_by_symbol: Option<&HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        Ok(Vec::new())
    }
}

#[async_trait]
impl SupplementaryReads for FixtureProvider {
    /// The local fixture set is a main-board sample, so it carries no Emerge register. Empty,
    /// not absent: the pipeline step must see "no SME rows today".
    async fn sme_listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        Ok(Vec::new())
    }
}

/// [`HoldingsProvider`] served from rows the caller supplies. Zero network, zero disk
/// (`FixtureHoldingsProvider`).
///
/// A separate class and not a method on [`FixtureProvider`]: that one sits at the back of the
/// production stack, and a registered holdings fallback would serve invented positions as if they
/// were real. This one the factory deliberately does **not** register. Rows are keyed by
/// `broker_account_id`, so per-tenant isolation tests have two different answers.
#[derive(Debug, Clone, Default)]
pub struct FixtureHoldingsProvider {
    holdings: HashMap<i64, Vec<BrokerHoldingRecord>>,
    cash: HashMap<i64, Decimal>,
}

impl FixtureHoldingsProvider {
    /// `FixtureHoldingsProvider(holdings, cash)`.
    #[must_use]
    pub const fn new(
        holdings: HashMap<i64, Vec<BrokerHoldingRecord>>,
        cash: HashMap<i64, Decimal>,
    ) -> Self {
        Self { holdings, cash }
    }

    fn rows_for(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<&[BrokerHoldingRecord], ProviderError> {
        self.holdings
            .get(&account.broker_account_id)
            .map(Vec::as_slice)
            .ok_or_else(|| {
                ProviderError::provider_unavailable(format!(
                    "this fixture holds no answer for broker account {}; configure it explicitly \
                     (an empty list means the account holds nothing, and must be said rather than \
                     inferred)",
                    account.broker_account_id
                ))
                .with_provider(HOLDINGS_PROVIDER_NAME)
            })
    }
}

impl HealthReporting for FixtureHoldingsProvider {
    fn name(&self) -> &str {
        HOLDINGS_PROVIDER_NAME
    }

    fn capabilities(&self) -> CapabilitySet {
        holdings_capabilities()
    }

    /// Always available, and always says the numbers are invented.
    fn check(&self) -> ProviderHealth {
        let positions: usize = self.holdings.values().map(Vec::len).sum();
        ProviderHealth {
            name: HOLDINGS_PROVIDER_NAME.to_owned(),
            available: true,
            capabilities: self.capabilities(),
            detail: format!(
                "{positions} fabricated positions across {} broker accounts; these are not \
                 anyone's real holdings",
                self.holdings.len()
            ),
        }
    }
}

#[async_trait]
impl HoldingsProvider for FixtureHoldingsProvider {
    /// The configured rows for `account`. An account this fixture was not given raises rather
    /// than returning an empty list — the empty list is a meaningful answer here.
    async fn broker_holdings(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Vec<BrokerHoldingRecord>, ProviderError> {
        Ok(self.rows_for(account)?.to_vec())
    }

    /// The configured balance, or `None` for an account given no cash figure. `None` is not
    /// zero: the sync must leave it alone rather than overwrite it with a zero.
    async fn broker_cash(
        &self,
        account: &BrokerAccountRef,
    ) -> Result<Option<Decimal>, ProviderError> {
        self.rows_for(account)?;
        Ok(self.cash.get(&account.broker_account_id).copied())
    }
}
