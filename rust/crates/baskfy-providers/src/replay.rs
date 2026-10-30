//! The replay provider: one recorded trading day, served from a bundle directory with **zero
//! network**. No Python twin in production — its twin is `tools/parity/replay_provider.py`, the
//! harness's adapter, and the two must answer every call identically (DECISIONS-RS R7.3).
//!
//! It exists so the worker's nightly can be graded differentially (`tools/parity/nightly_diff.py`):
//! the Python chain and the Rust chain read the same answers for the same calls, so any
//! difference in what they write is a difference in the code, never in the inputs.
//!
//! # The bundle (format 1)
//!
//! ```text
//! <bundle>/manifest.json   {"format": 1, "trade_date": "YYYY-MM-DD", "variant": "...",
//!                           "refuse": [{"method": "...", "args": {...}, "kind": "...",
//!                                       "message": "..."}], ...}
//! <bundle>/calls/list_instruments.json            [InstrumentRecord, ...]
//! <bundle>/calls/listings.json                    [ListingRecord, ...]
//! <bundle>/calls/sme_listings.json                [ListingRecord, ...]
//! <bundle>/calls/etf_symbols.json                 ["SYM", ...]
//! <bundle>/calls/fno_underlyings.json             ["SYM", ...]
//! <bundle>/calls/daily_bars.json                  {"start": D, "end": D,
//!                                                  "by_token": {"<token>": [bar, ...]}}
//! <bundle>/calls/bhavcopy/<date>.json             [bhavcopy row, ...]
//! <bundle>/calls/corporate_actions/<since>.json   [CorporateAction, ...]
//! <bundle>/calls/index_constituents/<slug>/<date>.json   ["SYM", ...]
//! <bundle>/calls/index_snapshots/<date>.json      [IndexSnapshot, ...]
//! <bundle>/calls/equity_fundamentals/<date>.json  [EquityFundamental, ...]
//! ```
//!
//! Records are pydantic's `model_dump(mode="json")` (decimals and dates as strings). A bar is
//! `{symbol, date, open, high, low, close, volume}`; a bhavcopy row carries `BHAVCOPY_SCHEMA`'s
//! thirteen columns. Frames are built at the schema's own `Decimal(18, 4)` / `Decimal(20, 2)`.
//!
//! # What a call the bundle cannot answer does
//!
//! It raises **`ProviderUnavailable`** (`[replay] no recorded answer for <call>`), and a call the
//! manifest's `refuse` list names raises the error kind it names. Both are explicit, identical in
//! both languages, and never an empty answer that could pass for data.
//!
//! * `daily_bars`: the token must be in `by_token` and the window inside `[start, end]`; rows are
//!   filtered to the window.
//! * `equity_fundamentals`: the recorded rows for the symbols asked, in the order asked (each
//!   symbol once); `series_by_symbol` is ignored (a hint, never an answer).
//! * `index_level`: never recorded — unavailable.
//!
//! Law 2: reads only. The adapter has no broker port at all.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};

use async_trait::async_trait;
use chrono::NaiveDate;
use polars::prelude::{
    DataFrame, DataType, Int64Chunked, Int128Chunked, IntoColumn, IntoSeries, NamedFrom,
    NewChunkedArray, PlSmallStr, Series, StringChunked,
};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::Deserialize;

use crate::composite::{Registered, SupplementaryReads};
use crate::errors::{ErrorKind, ProviderError};
use crate::ports::{
    BarsProvider, Capability, CapabilitySet, HealthReporting, ProviderHealth, ReferenceProvider,
};
use crate::records::{
    CorporateAction, EquityFundamental, IndexSnapshot, InstrumentRecord, ListingRecord,
    bhavcopy_schema, daily_bars_schema,
};

/// The adapter's name, as errors and the doctor print it.
pub const PROVIDER_NAME: &str = "replay";

/// The bundle format this adapter reads.
pub const BUNDLE_FORMAT: i64 = 1;

/// One entry of the manifest's `refuse` list.
#[derive(Debug, Clone, Deserialize)]
pub struct Refusal {
    /// The port method it applies to (`index_snapshots`, `daily_bars`, ...).
    pub method: String,
    /// The arguments that must match (each compared as its string form); absent: every call.
    #[serde(default)]
    pub args: BTreeMap<String, serde_json::Value>,
    /// The Python error class to raise (`ProviderUnavailable` when absent).
    #[serde(default)]
    pub kind: Option<String>,
    /// The message (provider prefix added on display).
    pub message: String,
}

/// `manifest.json`.
#[derive(Debug, Clone, Deserialize)]
pub struct Manifest {
    /// Must be [`BUNDLE_FORMAT`].
    pub format: i64,
    /// The recorded session.
    pub trade_date: NaiveDate,
    /// The variant label (`base`, `gate-refuse`, `provider-failure-midway`, ...).
    #[serde(default)]
    pub variant: Option<String>,
    /// Calls to refuse.
    #[serde(default)]
    pub refuse: Vec<Refusal>,
}

#[derive(Debug, Clone, Deserialize)]
struct Bar {
    symbol: String,
    date: NaiveDate,
    open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: i64,
}

#[derive(Debug, Clone, Deserialize)]
struct DailyBars {
    start: NaiveDate,
    end: NaiveDate,
    by_token: HashMap<String, Vec<Bar>>,
}

#[derive(Debug, Clone, Deserialize)]
struct BhavRow {
    symbol: Option<String>,
    series: Option<String>,
    date: Option<NaiveDate>,
    open: Option<Decimal>,
    high: Option<Decimal>,
    low: Option<Decimal>,
    close: Option<Decimal>,
    prev_close: Option<Decimal>,
    volume: Option<i64>,
    turnover: Option<Decimal>,
    trades: Option<i64>,
    upper_circuit: Option<Decimal>,
    lower_circuit: Option<Decimal>,
}

/// The replay adapter over one bundle directory.
pub struct ReplayProvider {
    root: PathBuf,
    manifest: Manifest,
    /// `Err(None)`: no `daily_bars.json`; `Err(Some(message))`: one that cannot be read.
    bars: OnceLock<Result<DailyBars, Option<String>>>,
}

impl std::fmt::Debug for ReplayProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ReplayProvider")
            .field("root", &self.root)
            .field("trade_date", &self.manifest.trade_date)
            .finish_non_exhaustive()
    }
}

fn unavailable(message: impl Into<String>) -> ProviderError {
    ProviderError::provider_unavailable(message).with_provider(PROVIDER_NAME)
}

fn kind_named(name: &str) -> ErrorKind {
    match name {
        "ProviderError" => ErrorKind::ProviderError,
        "CredentialsMissing" => ErrorKind::CredentialsMissing,
        "TransientProviderError" => ErrorKind::TransientProviderError,
        "RateLimited" => ErrorKind::RateLimited,
        "UpstreamUnavailable" => ErrorKind::UpstreamUnavailable,
        "ProviderDataError" => ErrorKind::ProviderDataError,
        "UnexpectedPayload" => ErrorKind::UnexpectedPayload,
        _ => ErrorKind::ProviderUnavailable,
    }
}

/// The string form an argument is matched and printed by: a string as itself, anything else as
/// its JSON text (`738561`, `true`).
fn arg_text(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// `method(k=v, ...)` — the call as the messages name it, arguments in the port's order.
fn call_text(method: &str, args: &[(&str, String)]) -> String {
    let inner: Vec<String> = args.iter().map(|(k, v)| format!("{k}={v}")).collect();
    format!("{method}({})", inner.join(", "))
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

impl ReplayProvider {
    /// Opens `root`, reading its manifest.
    ///
    /// # Errors
    /// `ProviderUnavailable` for a missing or unreadable manifest, or a format this adapter does
    /// not read.
    pub fn open(root: impl Into<PathBuf>) -> Result<Self, ProviderError> {
        let root = root.into();
        let path = root.join("manifest.json");
        let text = std::fs::read_to_string(&path).map_err(|e| {
            unavailable(format!(
                "cannot read replay manifest {}: {e}",
                path.display()
            ))
        })?;
        let manifest: Manifest = serde_json::from_str(&text).map_err(|e| {
            unavailable(format!(
                "replay manifest {} does not parse: {e}",
                path.display()
            ))
        })?;
        if manifest.format != BUNDLE_FORMAT {
            return Err(unavailable(format!(
                "replay bundle format {} is not {BUNDLE_FORMAT}",
                manifest.format
            )));
        }
        Ok(Self {
            root,
            manifest,
            bars: OnceLock::new(),
        })
    }

    /// The manifest.
    #[must_use]
    pub fn manifest(&self) -> &Manifest {
        &self.manifest
    }

    /// This adapter registered for a [`CompositeProvider`](crate::composite::CompositeProvider):
    /// bars, reference and the duck-typed reads.
    #[must_use]
    pub fn register(self: Arc<Self>) -> Registered {
        Registered::new(Arc::clone(&self) as Arc<dyn HealthReporting>)
            .with_bars(Arc::clone(&self) as Arc<dyn BarsProvider>)
            .with_reference(Arc::clone(&self) as Arc<dyn ReferenceProvider>)
            .with_supplementary(self as Arc<dyn SupplementaryReads>)
    }

    /// Raises the manifest's refusal for this call, if one names it.
    fn refused(&self, method: &str, args: &[(&str, String)]) -> Result<(), ProviderError> {
        for refusal in &self.manifest.refuse {
            if refusal.method != method {
                continue;
            }
            let all = refusal.args.iter().all(|(k, v)| {
                args.iter()
                    .any(|(name, given)| name == k && *given == arg_text(v))
            });
            if all {
                let kind = kind_named(refusal.kind.as_deref().unwrap_or("ProviderUnavailable"));
                return Err(
                    ProviderError::new(kind, refusal.message.clone()).with_provider(PROVIDER_NAME)
                );
            }
        }
        Ok(())
    }

    fn no_answer(method: &str, args: &[(&str, String)]) -> ProviderError {
        unavailable(format!(
            "no recorded answer for {}",
            call_text(method, args)
        ))
    }

    /// Reads `calls/<relative>` as JSON, or the "no recorded answer" refusal when absent.
    fn read<T: serde::de::DeserializeOwned>(
        &self,
        relative: &Path,
        method: &str,
        args: &[(&str, String)],
    ) -> Result<T, ProviderError> {
        let path = self.root.join("calls").join(relative);
        let text = match std::fs::read_to_string(&path) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(Self::no_answer(method, args));
            }
            Err(e) => {
                return Err(unavailable(format!(
                    "cannot read the recorded answer for {}: {e}",
                    call_text(method, args)
                )));
            }
        };
        serde_json::from_str(&text).map_err(|e| {
            unavailable(format!(
                "the recorded answer for {} does not parse: {e}",
                call_text(method, args)
            ))
        })
    }

    fn daily_bars_file(&self, args: &[(&str, String)]) -> Result<&DailyBars, ProviderError> {
        let loaded = self.bars.get_or_init(|| {
            let path = self.root.join("calls").join("daily_bars.json");
            match std::fs::read_to_string(&path) {
                Ok(text) => serde_json::from_str::<DailyBars>(&text)
                    .map_err(|e| Some(format!("the recorded daily bars do not parse: {e}"))),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(None),
                Err(e) => Err(Some(format!("cannot read the recorded daily bars: {e}"))),
            }
        });
        loaded.as_ref().map_err(|message| match message {
            None => Self::no_answer("daily_bars", args),
            Some(message) => unavailable(message.clone()),
        })
    }
}

impl HealthReporting for ReplayProvider {
    fn name(&self) -> &str {
        PROVIDER_NAME
    }

    fn capabilities(&self) -> CapabilitySet {
        [
            Capability::ListInstruments,
            Capability::DailyBars,
            Capability::IndexConstituents,
            Capability::IndexSnapshots,
            Capability::CorporateActions,
            Capability::Listings,
            Capability::Bhavcopy,
            Capability::EquityFundamentals,
        ]
        .into_iter()
        .collect()
    }

    fn check(&self) -> ProviderHealth {
        ProviderHealth {
            name: PROVIDER_NAME.to_owned(),
            available: true,
            capabilities: self.capabilities(),
            detail: format!(
                "replay bundle {} for {}",
                self.root.display(),
                iso(self.manifest.trade_date)
            ),
        }
    }
}

/// A `Decimal(precision, scale)` column; each value must already sit on `scale` places or
/// fewer (rescaled exactly — a bundle never carries more places than the schema).
fn decimal_column(
    name: &str,
    values: &[Option<Decimal>],
    precision: usize,
    scale: u32,
) -> Result<Series, ProviderError> {
    let mut mantissas = Vec::with_capacity(values.len());
    for value in values {
        mantissas.push(match value {
            None => None,
            Some(d) => {
                let mut v = d.round_dp_with_strategy(scale, RoundingStrategy::MidpointNearestEven);
                v.rescale(scale);
                if v != *d || v.scale() != scale {
                    return Err(unavailable(format!(
                        "recorded value {d} in column {name:?} has more than {scale} places"
                    )));
                }
                Some(v.mantissa())
            }
        });
    }
    let scale = usize::try_from(scale).unwrap_or(0);
    Ok(
        Int128Chunked::from_iter_options(PlSmallStr::from(name), mantissas.into_iter())
            .into_decimal_unchecked(precision, scale)
            .into_series(),
    )
}

fn dates_column(name: &str, values: &[Option<NaiveDate>]) -> Result<Series, ProviderError> {
    let series = Series::new(PlSmallStr::from(name), values.to_vec());
    if series.dtype() == &DataType::Date {
        Ok(series)
    } else {
        series
            .cast(&DataType::Date)
            .map_err(|e| unavailable(format!("cannot build column {name:?}: {e}")))
    }
}

fn frame_on_schema(
    height: usize,
    columns: Vec<Series>,
    schema: &polars::prelude::Schema,
    what: &str,
) -> Result<DataFrame, ProviderError> {
    let frame = DataFrame::new(
        height,
        columns.into_iter().map(IntoColumn::into_column).collect(),
    )
    .map_err(|e| unavailable(format!("cannot build the {what} frame: {e}")))?;
    if frame.schema().as_ref() != schema {
        return Err(unavailable(format!(
            "the {what} frame was built off-schema: {:?}",
            frame.schema()
        )));
    }
    Ok(frame)
}

fn bars_frame(rows: &[&Bar]) -> Result<DataFrame, ProviderError> {
    let height = rows.len();
    let dec = |f: fn(&Bar) -> Decimal| rows.iter().map(|r| Some(f(r))).collect::<Vec<_>>();
    let columns = vec![
        StringChunked::from_iter_options(
            PlSmallStr::from("symbol"),
            rows.iter().map(|r| Some(r.symbol.as_str())),
        )
        .into_series(),
        dates_column(
            "date",
            &rows.iter().map(|r| Some(r.date)).collect::<Vec<_>>(),
        )?,
        decimal_column("open", &dec(|r| r.open), 18, 4)?,
        decimal_column("high", &dec(|r| r.high), 18, 4)?,
        decimal_column("low", &dec(|r| r.low), 18, 4)?,
        decimal_column("close", &dec(|r| r.close), 18, 4)?,
        Int64Chunked::from_iter_options(
            PlSmallStr::from("volume"),
            rows.iter().map(|r| Some(r.volume)),
        )
        .into_series(),
        StringChunked::from_iter_options(
            PlSmallStr::from("source"),
            rows.iter().map(|_| Some("kite")),
        )
        .into_series(),
    ];
    frame_on_schema(height, columns, &daily_bars_schema(), "daily bars")
}

fn bhavcopy_frame(rows: &[BhavRow]) -> Result<DataFrame, ProviderError> {
    let height = rows.len();
    let text = |name: &str, f: fn(&BhavRow) -> Option<&str>| {
        StringChunked::from_iter_options(PlSmallStr::from(name), rows.iter().map(f)).into_series()
    };
    let int = |name: &str, f: fn(&BhavRow) -> Option<i64>| {
        Int64Chunked::from_iter_options(PlSmallStr::from(name), rows.iter().map(f)).into_series()
    };
    let dec = |f: fn(&BhavRow) -> Option<Decimal>| rows.iter().map(f).collect::<Vec<_>>();
    let columns = vec![
        text("symbol", |r| r.symbol.as_deref()),
        text("series", |r| r.series.as_deref()),
        dates_column("date", &rows.iter().map(|r| r.date).collect::<Vec<_>>())?,
        decimal_column("open", &dec(|r| r.open), 18, 4)?,
        decimal_column("high", &dec(|r| r.high), 18, 4)?,
        decimal_column("low", &dec(|r| r.low), 18, 4)?,
        decimal_column("close", &dec(|r| r.close), 18, 4)?,
        decimal_column("prev_close", &dec(|r| r.prev_close), 18, 4)?,
        int("volume", |r| r.volume),
        decimal_column("turnover", &dec(|r| r.turnover), 20, 2)?,
        int("trades", |r| r.trades),
        decimal_column("upper_circuit", &dec(|r| r.upper_circuit), 18, 4)?,
        decimal_column("lower_circuit", &dec(|r| r.lower_circuit), 18, 4)?,
    ];
    frame_on_schema(height, columns, &bhavcopy_schema(), "bhavcopy")
}

#[async_trait]
impl BarsProvider for ReplayProvider {
    async fn list_instruments(&self) -> Result<Vec<InstrumentRecord>, ProviderError> {
        self.refused("list_instruments", &[])?;
        self.read(Path::new("list_instruments.json"), "list_instruments", &[])
    }

    async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        let args = [
            ("token", token.to_string()),
            ("start", iso(start)),
            ("end", iso(end)),
        ];
        self.refused("daily_bars", &args)?;
        let file = self.daily_bars_file(&args)?;
        if start < file.start || end > file.end {
            return Err(unavailable(format!(
                "no recorded answer for {}: the bundle holds {}..{}",
                call_text("daily_bars", &args),
                iso(file.start),
                iso(file.end)
            )));
        }
        let Some(rows) = file.by_token.get(&token.to_string()) else {
            return Err(Self::no_answer("daily_bars", &args));
        };
        let selected: Vec<&Bar> = rows
            .iter()
            .filter(|r| r.date >= start && r.date <= end)
            .collect();
        bars_frame(&selected)
    }
}

#[async_trait]
impl ReferenceProvider for ReplayProvider {
    async fn index_constituents(
        &self,
        index_slug: &str,
        on: NaiveDate,
    ) -> Result<Vec<String>, ProviderError> {
        let args = [("index_slug", index_slug.to_owned()), ("on", iso(on))];
        self.refused("index_constituents", &args)?;
        if index_slug.contains(['/', '\\']) || index_slug.starts_with('.') {
            return Err(Self::no_answer("index_constituents", &args));
        }
        let relative = Path::new("index_constituents")
            .join(index_slug)
            .join(format!("{}.json", iso(on)));
        self.read(&relative, "index_constituents", &args)
    }

    async fn index_snapshots(&self, on: NaiveDate) -> Result<Vec<IndexSnapshot>, ProviderError> {
        let args = [("on", iso(on))];
        self.refused("index_snapshots", &args)?;
        let relative = Path::new("index_snapshots").join(format!("{}.json", iso(on)));
        self.read(&relative, "index_snapshots", &args)
    }

    async fn corporate_actions(
        &self,
        since: NaiveDate,
    ) -> Result<Vec<CorporateAction>, ProviderError> {
        let args = [("since", iso(since))];
        self.refused("corporate_actions", &args)?;
        let relative = Path::new("corporate_actions").join(format!("{}.json", iso(since)));
        self.read(&relative, "corporate_actions", &args)
    }

    async fn listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        self.refused("listings", &[])?;
        self.read(Path::new("listings.json"), "listings", &[])
    }

    async fn bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        let args = [("on", iso(on))];
        self.refused("bhavcopy", &args)?;
        let relative = Path::new("bhavcopy").join(format!("{}.json", iso(on)));
        let rows: Vec<BhavRow> = self.read(&relative, "bhavcopy", &args)?;
        bhavcopy_frame(&rows)
    }

    async fn equity_fundamentals(
        &self,
        on: NaiveDate,
        symbols: &[String],
        _series_by_symbol: Option<&HashMap<String, String>>,
    ) -> Result<Vec<EquityFundamental>, ProviderError> {
        let args = [("on", iso(on))];
        self.refused("equity_fundamentals", &args)?;
        let relative = Path::new("equity_fundamentals").join(format!("{}.json", iso(on)));
        let recorded: Vec<EquityFundamental> =
            self.read(&relative, "equity_fundamentals", &args)?;
        let mut by_symbol: HashMap<&str, &EquityFundamental> = HashMap::new();
        for record in &recorded {
            by_symbol.entry(record.symbol.as_str()).or_insert(record);
        }
        let mut seen: HashSet<&str> = HashSet::new();
        let mut out = Vec::new();
        for symbol in symbols {
            if seen.insert(symbol.as_str())
                && let Some(record) = by_symbol.get(symbol.as_str())
            {
                out.push((*record).clone());
            }
        }
        Ok(out)
    }
}

#[async_trait]
impl SupplementaryReads for ReplayProvider {
    async fn sme_listings(&self) -> Result<Vec<ListingRecord>, ProviderError> {
        self.refused("sme_listings", &[])?;
        self.read(Path::new("sme_listings.json"), "sme_listings", &[])
    }

    async fn etf_symbols(&self) -> Result<Vec<String>, ProviderError> {
        self.refused("etf_symbols", &[])?;
        self.read(Path::new("etf_symbols.json"), "etf_symbols", &[])
    }

    async fn index_level(&self, slug: &str) -> Result<Option<Decimal>, ProviderError> {
        let args = [("slug", slug.to_owned())];
        self.refused("index_level", &args)?;
        Err(Self::no_answer("index_level", &args))
    }

    async fn fno_underlyings(&self) -> Result<Vec<String>, ProviderError> {
        self.refused("fno_underlyings", &[])?;
        self.read(Path::new("fno_underlyings.json"), "fno_underlyings", &[])
    }
}
