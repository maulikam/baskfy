//! What the backfills share: dates, frames, the provider seams and the Kite session check.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use baskfy_db::OrderedJson;
use baskfy_providers::ProviderError;
use baskfy_providers::composite::{CompositeProvider, Registered};
use baskfy_providers::factory::{KiteFamily, KiteLane, RedisSource, build_kite_family_limiter};
use baskfy_providers::kite::{
    ClientFactory, DEFAULT_ROOT_URI, KiteClient, KiteConnect, KiteProvider, KiteRuntime,
};
use baskfy_providers::ports::{
    Capability, CapabilitySet, HealthReporting, ProviderHealth, ReferenceProvider,
};
use baskfy_providers::ratelimit::RateLimiter;
use baskfy_providers::records::{CorporateAction, EquityFundamental, IndexSnapshot, ListingRecord};
use baskfy_providers::secret::Secret;
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::{AccessTokenStore, ist};
use chrono::{DateTime, FixedOffset, NaiveDate};
use polars::prelude::{AnyValue, Column, DataFrame};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::tasks::{BoundArgs, TaskContext};
use crate::window::DateWindow;

pub(super) fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

pub(super) fn count(n: usize) -> OrderedJson {
    OrderedJson::Int(i64::try_from(n).unwrap_or(i64::MAX))
}

pub(super) fn described(error: &WorkerError) -> String {
    format!("{}: {error}", error.python_type())
}

pub(super) fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

pub(super) fn trade_date_arg(
    args: &BoundArgs,
    name: &str,
) -> Result<Option<NaiveDate>, WorkerError> {
    match args.value(name) {
        None | Some(OrderedJson::Null | OrderedJson::Bool(false) | OrderedJson::Int(0)) => Ok(None),
        Some(OrderedJson::Str(text)) if text.is_empty() => Ok(None),
        Some(OrderedJson::Str(text)) => crate::tasks::args::fromisoformat_date(text).map(Some),
        Some(_) => Err(WorkerError::python(
            "TypeError",
            "fromisoformat: argument must be str",
        )),
    }
}

pub(super) async fn trading_days_in(
    conn: &mut PgConnection,
    window: DateWindow,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date >= $2 AND date <= $3 ORDER BY date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(window.start())
    .bind(window.end())
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(d,)| d).collect())
}

fn cell<'a>(frame: &'a DataFrame, name: &str, row: usize) -> Result<AnyValue<'a>, WorkerError> {
    let col: &Column = frame
        .column(name)
        .map_err(|e| WorkerError::python("ColumnNotFoundError", format!("{name}: {e}")))?;
    col.get(row)
        .map_err(|e| WorkerError::python("OutOfBoundsError", e.to_string()))
}

pub(super) fn text_at(
    frame: &DataFrame,
    name: &str,
    row: usize,
) -> Result<Option<String>, WorkerError> {
    match cell(frame, name, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::String(s) => Ok(Some(s.to_owned())),
        AnyValue::StringOwned(s) => Ok(Some(s.to_string())),
        other => Err(WorkerError::python(
            "TypeError",
            format!("{name}: expected a string, got {other:?}"),
        )),
    }
}

pub(super) fn date_at(
    frame: &DataFrame,
    name: &str,
    row: usize,
) -> Result<Option<NaiveDate>, WorkerError> {
    match cell(frame, name, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::Date(days) => NaiveDate::from_ymd_opt(1970, 1, 1)
            .and_then(|e| e.checked_add_signed(chrono::Duration::days(i64::from(days))))
            .map(Some)
            .ok_or_else(|| WorkerError::python("OverflowError", "date out of range")),
        AnyValue::Datetime(..) | AnyValue::DatetimeOwned(..) => Err(WorkerError::python(
            "TypeError",
            format!("{name}: a datetime where a date was expected"),
        )),
        other => Err(WorkerError::python(
            "TypeError",
            format!("{name}: expected a date, got {other:?}"),
        )),
    }
}

pub(super) fn decimal_at(
    frame: &DataFrame,
    name: &str,
    row: usize,
) -> Result<Option<Decimal>, WorkerError> {
    match cell(frame, name, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::Decimal(mantissa, _, scale) => {
            let scale = u32::try_from(scale)
                .map_err(|_| WorkerError::python("ValueError", "negative decimal scale"))?;
            Decimal::try_from_i128_with_scale(mantissa, scale)
                .map(Some)
                .map_err(|e| WorkerError::python("ValueError", e.to_string()))
        }
        AnyValue::Float64(v) => Ok(Decimal::from_f64_retain(v)),
        AnyValue::Int64(v) => Ok(Some(Decimal::from(v))),
        other => Err(WorkerError::python(
            "TypeError",
            format!("{name}: expected a number, got {other:?}"),
        )),
    }
}

pub(super) fn int_at(
    frame: &DataFrame,
    name: &str,
    row: usize,
) -> Result<Option<i64>, WorkerError> {
    match cell(frame, name, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::Int64(v) => Ok(Some(v)),
        AnyValue::Int32(v) => Ok(Some(i64::from(v))),
        AnyValue::UInt32(v) => Ok(Some(i64::from(v))),
        other => Err(WorkerError::python(
            "TypeError",
            format!("{name}: expected an integer, got {other:?}"),
        )),
    }
}

pub(super) fn float_of(d: Decimal) -> f64 {
    d.to_string().parse::<f64>().unwrap_or(f64::NAN)
}

pub(super) fn decimal4(x: f64) -> Result<Decimal, WorkerError> {
    let text = baskfy_core::py::float_repr(crate::steps::sleeves::for_swing_live::py_round(x, 4));
    let parsed = if text.contains('e') {
        Decimal::from_scientific(&text)
    } else {
        text.parse::<Decimal>()
    };
    parsed.map_err(|e| WorkerError::python("InvalidOperation", format!("{text}: {e}")))
}

pub(super) fn cent(d: Decimal) -> Decimal {
    let mut v = d.round_dp_with_strategy(2, rust_decimal::RoundingStrategy::MidpointAwayFromZero);
    v.rescale(2);
    v
}

pub(super) fn slugify_index(name: &str) -> String {
    let is_space = |c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c);
    let lowered = name.trim_matches(is_space).to_lowercase();
    let mut out = String::with_capacity(lowered.len());
    let mut in_run = false;
    for c in lowered.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            out.push(c);
            in_run = false;
        } else if !in_run {
            out.push('-');
            in_run = true;
        }
    }
    out.trim_matches('-').to_owned()
}

type Fut<'a, T> = Pin<Box<dyn Future<Output = Result<T, ProviderError>> + Send + 'a>>;

pub(super) struct Canned {
    pub(super) bhavcopy: Option<DataFrame>,
    pub(super) snapshots: Vec<IndexSnapshot>,
}

const CANNED: &str = "canned";

fn unserved(what: &str) -> ProviderError {
    ProviderError::provider_unavailable(format!("{CANNED} serves no {what}"))
}

impl HealthReporting for Canned {
    fn name(&self) -> &str {
        CANNED
    }

    fn capabilities(&self) -> CapabilitySet {
        [Capability::IndexSnapshots, Capability::Bhavcopy].into()
    }

    fn check(&self) -> ProviderHealth {
        ProviderHealth {
            name: CANNED.to_owned(),
            available: true,
            capabilities: self.capabilities(),
            detail: "pre-fetched".to_owned(),
        }
    }
}

impl ReferenceProvider for Canned {
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
        Box::pin(async { Err(unserved("index_constituents")) })
    }

    fn index_snapshots<'life0, 'async_trait>(
        &'life0 self,
        _on: NaiveDate,
    ) -> Fut<'async_trait, Vec<IndexSnapshot>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async move { Ok(self.snapshots.clone()) })
    }

    fn corporate_actions<'life0, 'async_trait>(
        &'life0 self,
        _since: NaiveDate,
    ) -> Fut<'async_trait, Vec<CorporateAction>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Err(unserved("corporate_actions")) })
    }

    fn listings<'life0, 'async_trait>(&'life0 self) -> Fut<'async_trait, Vec<ListingRecord>>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async { Err(unserved("listings")) })
    }

    fn bhavcopy<'life0, 'async_trait>(&'life0 self, _on: NaiveDate) -> Fut<'async_trait, DataFrame>
    where
        'life0: 'async_trait,
        Self: 'async_trait,
    {
        Box::pin(async move { self.bhavcopy.clone().ok_or_else(|| unserved("bhavcopy")) })
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
        Box::pin(async { Err(unserved("equity_fundamentals")) })
    }
}

pub(super) fn canned(canned: Canned) -> Result<CompositeProvider, WorkerError> {
    let shared = Arc::new(canned);
    Ok(CompositeProvider::with_defaults(vec![
        Registered::new(shared.clone()).with_reference(shared),
    ])?)
}

pub(super) fn now_ist(ctx: &TaskContext) -> DateTime<FixedOffset> {
    ctx.now_utc().with_timezone(&ist())
}

fn token_store_path() -> std::path::PathBuf {
    let read = |name: &str| std::env::var(name).unwrap_or_default().trim().to_owned();
    let broker = read("BASKFY_BROKER_TOKEN_PATH");
    let kite = read("BASKFY_KITE_TOKEN_PATH");
    std::path::PathBuf::from(if !broker.is_empty() {
        broker
    } else if !kite.is_empty() {
        kite
    } else {
        ".secrets/broker-token.enc".to_owned()
    })
}

pub(super) fn kite_session_usable(ctx: &TaskContext) -> bool {
    let key = std::env::var("BASKFY_KITE_TOKEN_ENCRYPTION_KEY")
        .unwrap_or_default()
        .trim()
        .to_owned();
    let store = AccessTokenStore::with_clock(token_store_path(), Secret::new(key), ctx.clock());
    if !store.exists() {
        return false;
    }
    store
        .load()
        .is_ok_and(|token| !token.is_expired(now_ist(ctx)))
}

pub(super) fn provider_settings() -> Result<ProviderSettings, WorkerError> {
    ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))
}

pub(super) async fn kite_bars(ctx: &TaskContext) -> Result<KiteProvider, WorkerError> {
    let settings = provider_settings()?;
    let limiter = build_kite_family_limiter(
        &RedisSource::from_settings(&settings),
        &settings,
        KiteFamily::Historical,
        KiteLane::Bulk,
    )
    .await?;
    let root = ctx.kite_root().unwrap_or(DEFAULT_ROOT_URI).to_owned();
    let timeout = settings.kite_request_timeout_seconds;
    let factory: ClientFactory = Arc::new(move |api_key: &str| {
        let client: Arc<dyn KiteClient> =
            Arc::new(KiteConnect::with_root(api_key, &root, Some(timeout))?);
        Ok(client)
    });
    let store = AccessTokenStore::with_clock(
        settings.kite_token_path.clone(),
        settings.kite_token_encryption_key.clone(),
        ctx.clock(),
    );
    KiteProvider::new(
        settings,
        KiteRuntime {
            rate_limiter: limiter.map(|l| l as Arc<dyn RateLimiter>),
            client_factory: Some(factory),
            token_store: Some(Arc::new(store)),
            ..KiteRuntime::default()
        },
    )
    .map_err(WorkerError::Provider)
}
