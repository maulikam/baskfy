//! Shared seams for the backfill tests: a stub provider, frames, and a throwaway clone of the bed.
#![allow(dead_code, clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use baskfy_db::{DbSettings, WriteDb};
use baskfy_providers::ProviderError;
use baskfy_providers::composite::{CompositeProvider, Registered};
use baskfy_providers::ports::{
    BarsProvider, Capability, CapabilitySet, HealthReporting, ProviderHealth, ReferenceProvider,
};
use baskfy_providers::records::{
    CorporateAction, EquityFundamental, IndexSnapshot, InstrumentRecord, ListingRecord,
};
use baskfy_providers::secret::Secret;
use chrono::{Datelike, NaiveDate, Weekday};
use polars::prelude::{
    Column, DataFrame, Int128Chunked, IntoColumn, IntoSeries, NamedFrom, NewChunkedArray, Series,
};
use rust_decimal::Decimal;

pub const ENV: &str = "BASKFY_RS_BACKFILLS_DB_URL";
pub const IGNORE: &str = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored";

/// One test at a time on the clone: the backfills read global tables.
pub static LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

pub fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

pub fn dec(text: &str) -> Decimal {
    text.parse().unwrap()
}

/// The clone's read-write pool; refuses anything but an `rst_*`/`rsn_*` database.
pub fn db() -> WriteDb {
    let url = std::env::var(ENV).unwrap_or_else(|_| panic!("{IGNORE}"));
    let name = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        name.starts_with("rst_") || name.starts_with("rsn_"),
        "refusing {name:?}: these tests write, so they need a throwaway clone of the bed"
    );
    WriteDb::connect_lazy(&DbSettings::with_url(Secret::new(url))).unwrap()
}

/// A short tag unique to this process and call, for symbols and slugs no other row has.
pub fn tag() -> String {
    static N: AtomicUsize = AtomicUsize::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let n = N.fetch_add(1, Ordering::SeqCst);
    let raw = format!("{:x}{n}", nanos % 0xff_ffff);
    format!("W6{}", raw.to_uppercase())
}

pub async fn exec(db: &WriteDb, sql: &str) {
    let mut tx = db.begin().await.unwrap();
    sqlx::raw_sql(sql).execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
}

/// `make_instrument`: an NSE listing.
pub async fn make_instrument(db: &WriteDb, symbol: &str, series: &str, token: Option<i64>) -> i64 {
    let mut tx = db.begin().await.unwrap();
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO instrument (exchange_id, symbol, name, instrument_type, series, kite_token, \
         is_active) VALUES (1, $1, $1, 'EQ', $2, $3, true) RETURNING id",
    )
    .bind(symbol)
    .bind(series)
    .bind(token)
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
    id
}

/// One stored bar, as the tests compare them.
pub type Bar = (
    NaiveDate,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<i64>,
    Option<String>,
);

pub async fn bars_of(db: &WriteDb, instrument_id: i64) -> Vec<Bar> {
    let mut tx = db.begin().await.unwrap();
    let rows = sqlx::query_as(
        "SELECT date, open, close, close_raw, adj_factor, volume, source FROM ohlcv_daily \
         WHERE instrument_id = $1 ORDER BY date",
    )
    .bind(instrument_id)
    .fetch_all(&mut *tx)
    .await
    .unwrap();
    tx.rollback().await.unwrap();
    rows
}

pub fn dec_col(name: &str, values: &[Option<&str>], precision: usize, scale: u32) -> Column {
    Int128Chunked::from_iter_options(
        name.into(),
        values.iter().map(|v| {
            v.map(|t| {
                let mut x = dec(t);
                x.rescale(scale);
                x.mantissa()
            })
        }),
    )
    .into_decimal_unchecked(precision, scale as usize)
    .into_series()
    .into_column()
}

/// One bhavcopy row: symbol, series, OHLC, volume, turnover, upper and lower bands.
pub struct BhavRow<'a> {
    pub symbol: &'a str,
    pub series: Option<&'a str>,
    pub ohlc: [&'a str; 4],
    pub volume: i64,
    pub turnover: Option<&'a str>,
    pub bands: [Option<&'a str>; 2],
}

pub fn bhavcopy(on: NaiveDate, rows: &[BhavRow<'_>]) -> DataFrame {
    let n = rows.len();
    let price = |k: usize| -> Vec<Option<&str>> { rows.iter().map(|r| Some(r.ohlc[k])).collect() };
    DataFrame::new(
        n,
        vec![
            Series::new(
                "symbol".into(),
                rows.iter().map(|r| r.symbol).collect::<Vec<_>>(),
            )
            .into_column(),
            Series::new(
                "series".into(),
                rows.iter().map(|r| r.series).collect::<Vec<_>>(),
            )
            .into_column(),
            Series::new("date".into(), vec![on; n]).into_column(),
            dec_col("open", &price(0), 18, 4),
            dec_col("high", &price(1), 18, 4),
            dec_col("low", &price(2), 18, 4),
            dec_col("close", &price(3), 18, 4),
            dec_col("prev_close", &price(3), 18, 4),
            Series::new(
                "volume".into(),
                rows.iter().map(|r| r.volume).collect::<Vec<_>>(),
            )
            .into_column(),
            dec_col(
                "turnover",
                &rows.iter().map(|r| r.turnover).collect::<Vec<_>>(),
                20,
                2,
            ),
            Series::new("trades".into(), vec![1i64; n]).into_column(),
            dec_col(
                "upper_circuit",
                &rows.iter().map(|r| r.bands[0]).collect::<Vec<_>>(),
                18,
                4,
            ),
            dec_col(
                "lower_circuit",
                &rows.iter().map(|r| r.bands[1]).collect::<Vec<_>>(),
                18,
                4,
            ),
        ],
    )
    .unwrap()
}

/// A daily candle: date, open, high, low, close, volume.
pub type Candle = (
    NaiveDate,
    &'static str,
    &'static str,
    &'static str,
    &'static str,
    i64,
);

pub fn daily_frame(symbol: &str, candles: &[Candle]) -> DataFrame {
    let n = candles.len();
    let col = |k: usize| -> Vec<Option<&str>> {
        candles
            .iter()
            .map(|c| Some([c.1, c.2, c.3, c.4][k]))
            .collect()
    };
    DataFrame::new(
        n,
        vec![
            Series::new("symbol".into(), vec![symbol; n]).into_column(),
            Series::new(
                "date".into(),
                candles.iter().map(|c| c.0).collect::<Vec<_>>(),
            )
            .into_column(),
            dec_col("open", &col(0), 18, 4),
            dec_col("high", &col(1), 18, 4),
            dec_col("low", &col(2), 18, 4),
            dec_col("close", &col(3), 18, 4),
            Series::new(
                "volume".into(),
                candles.iter().map(|c| c.5).collect::<Vec<_>>(),
            )
            .into_column(),
            Series::new("source".into(), vec!["kite"; n]).into_column(),
        ],
    )
    .unwrap()
}

/// Every weekday in `[start, end]`.
pub fn weekdays(start: NaiveDate, end: NaiveDate) -> Vec<NaiveDate> {
    start
        .iter_days()
        .take_while(|x| *x <= end)
        .filter(|x| !matches!(x.weekday(), Weekday::Sat | Weekday::Sun))
        .collect()
}

type Fut<'a, T> = Pin<Box<dyn Future<Output = Result<T, ProviderError>> + Send + 'a>>;

/// Answers from what it was given; counts the `daily_bars` calls; fails where told.
#[derive(Default)]
pub struct Stub {
    /// Token to its whole history (filtered to the asked window).
    pub bars: HashMap<i64, DataFrame>,
    /// Tokens whose `daily_bars` fails.
    pub failing_tokens: Vec<i64>,
    /// Day to its bhavcopy, or the error message for that day.
    pub bhavcopy: BTreeMap<NaiveDate, Result<DataFrame, String>>,
    pub fetches: AtomicUsize,
    pub asked: Mutex<Vec<(i64, NaiveDate, NaiveDate)>>,
}

impl Stub {
    pub fn composite(self: &Arc<Self>) -> CompositeProvider {
        CompositeProvider::with_defaults(vec![
            Registered::new(self.clone())
                .with_bars(self.clone())
                .with_reference(self.clone()),
        ])
        .unwrap()
    }

    pub fn fetches(&self) -> usize {
        self.fetches.load(Ordering::SeqCst)
    }
}

fn none(what: &str) -> ProviderError {
    ProviderError::provider_unavailable(format!("stub serves no {what}"))
}

impl HealthReporting for Stub {
    fn name(&self) -> &str {
        "stub"
    }

    fn capabilities(&self) -> CapabilitySet {
        [Capability::DailyBars, Capability::Bhavcopy].into()
    }

    fn check(&self) -> ProviderHealth {
        ProviderHealth {
            name: "stub".to_owned(),
            available: true,
            capabilities: self.capabilities(),
            detail: "test".to_owned(),
        }
    }
}

impl BarsProvider for Stub {
    fn list_instruments<'life0, 'async_trait>(
        &'life0 self,
    ) -> Fut<'async_trait, Vec<InstrumentRecord>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Err(none("list_instruments")) })
    }

    fn daily_bars<'life0, 'async_trait>(
        &'life0 self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Fut<'async_trait, DataFrame>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async move {
            self.fetches.fetch_add(1, Ordering::SeqCst);
            self.asked.lock().unwrap().push((token, start, end));
            if self.failing_tokens.contains(&token) {
                return Err(ProviderError::upstream_unavailable("503 from the stub"));
            }
            let Some(frame) = self.bars.get(&token) else {
                return Ok(daily_frame("", &[]));
            };
            let dates = frame.column("date").unwrap().date().unwrap().clone();
            let lo = (start - d(1970, 1, 1)).num_days();
            let hi = (end - d(1970, 1, 1)).num_days();
            let mask = dates
                .physical()
                .iter()
                .map(|v| v.is_some_and(|x| i64::from(x) >= lo && i64::from(x) <= hi))
                .collect::<polars::prelude::BooleanChunked>();
            Ok(frame.filter(&mask).unwrap())
        })
    }
}

impl ReferenceProvider for Stub {
    fn index_constituents<'life0, 'life1, 'async_trait>(
        &'life0 self,
        _index_slug: &'life1 str,
        _on: NaiveDate,
    ) -> Fut<'async_trait, Vec<String>>
    where
        'life0: 'async_trait,
        'life1: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Err(none("index_constituents")) })
    }

    fn index_snapshots<'life0, 'async_trait>(
        &'life0 self,
        _on: NaiveDate,
    ) -> Fut<'async_trait, Vec<IndexSnapshot>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Err(none("index_snapshots")) })
    }

    fn corporate_actions<'life0, 'async_trait>(
        &'life0 self,
        _since: NaiveDate,
    ) -> Fut<'async_trait, Vec<CorporateAction>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Err(none("corporate_actions")) })
    }

    fn listings<'life0, 'async_trait>(&'life0 self) -> Fut<'async_trait, Vec<ListingRecord>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Err(none("listings")) })
    }

    fn bhavcopy<'life0, 'async_trait>(&'life0 self, on: NaiveDate) -> Fut<'async_trait, DataFrame>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async move {
            match self.bhavcopy.get(&on) {
                Some(Ok(frame)) => Ok(frame.clone()),
                Some(Err(message)) => Err(ProviderError::upstream_unavailable(message.clone())),
                None => Err(ProviderError::upstream_unavailable(format!(
                    "GET bhavcopy {on} returned 404"
                ))),
            }
        })
    }

    fn equity_fundamentals<'life0, 'life1, 'life2, 'async_trait>(
        &'life0 self,
        _on: NaiveDate,
        _symbols: &'life1 [String],
        _series_by_symbol: Option<&'life2 HashMap<String, String>>,
    ) -> Fut<'async_trait, Vec<EquityFundamental>>
    where
        'life0: 'async_trait,
        'life1: 'async_trait,
        'life2: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Err(none("equity_fundamentals")) })
    }
}
