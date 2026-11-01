//! Port of `baskfy_api.opportunities`: the loaders, the scans, the options read. Read-only: the
//! one Kite path here is `option_quotes` / `option_contracts` (no order, no GTT).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

use baskfy_core::opportunities::config::{
    DEFAULT_OPTIONS_PARAMS, DEFAULT_SWING_PARAMS, DataStatus, SetupType, SwingFilters,
};
use baskfy_core::opportunities::indicators::with_indicators;
use baskfy_core::opportunities::options::{ContractQuote, OptionsRequest, OptionsResult, analyze};
use baskfy_core::opportunities::swing::{
    InstrumentContext, ScanCounts, SwingScanResult, missing_sessions, scan,
};
use baskfy_core::opportunities::validation::{ValidationResult, validate_setup};
use baskfy_core::options::config::{CostRates, OptionType};
use baskfy_core::screener::SECTOR_INDEX_SLUGS;
use baskfy_providers::kite::KiteProvider;
use baskfy_providers::records::{OptionType as KiteOptionType, PyDateTime};
use chrono::{DateTime, Duration as Days, FixedOffset, NaiveDate, NaiveTime, Utc};
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::PgPool;
use tokio::sync::{Semaphore, mpsc};

use crate::problems::{Problem, internal_error};
use crate::routes::sleeves::common::db;

/// `BENCHMARK_SLUGS`.
const BENCHMARK_SLUGS: [&str; 2] = ["nifty-500", "nifty-50"];
const SESSION_DATA_READY_HOUR: u32 = 18;
const HISTORY_CALENDAR_DAYS: i64 = 420;
/// Safety ceiling on names per swing scan; `BASKFY_OPPS_MAX_UNIVERSE` overrides the default,
/// which covers the whole NSE equity universe.
const DEFAULT_MAX_UNIVERSE: usize = 6000;
static MAX_UNIVERSE_CELL: LazyLock<usize> = LazyLock::new(|| {
    std::env::var("BASKFY_OPPS_MAX_UNIVERSE")
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(DEFAULT_MAX_UNIVERSE)
});

/// `MAX_UNIVERSE`.
pub(crate) fn max_universe() -> usize {
    *MAX_UNIVERSE_CELL
}
/// `MAX_RESULTS`.
pub(crate) const MAX_RESULTS: i64 = 100;
/// `MAX_VALIDATION_DAYS`.
pub(crate) const MAX_VALIDATION_DAYS: i64 = 3 * 366;
const VALIDATION_MAX_UNIVERSE: usize = 500;
const CACHE_TTL: Duration = Duration::from_secs(300);
const CONTRACT_CACHE_TTL: Duration = Duration::from_secs(6 * 3600);
const UPCOMING_ACTION_DAYS: i64 = 30;
const NSE_EXCHANGE_ID: i16 = 1;
const INDEX_SPOT_KEYS: [(&str, &str); 4] = [
    ("NIFTY", "NSE:NIFTY 50"),
    ("BANKNIFTY", "NSE:NIFTY BANK"),
    ("FINNIFTY", "NSE:NIFTY FIN SERVICE"),
    ("MIDCPNIFTY", "NSE:NIFTY MID SELECT"),
];
/// `KITE_DEPENDENCY`.
pub(crate) const KITE_DEPENDENCY: &str = "Kite Connect read session: BASKFY_KITE_API_KEY set, BASKFY_LIVE_QUOTES on, and a broker login completed today (quote + NFO instrument master).";

static SCAN_SLOTS: Semaphore = Semaphore::const_new(2);

/// Why a scan did not produce a result.
#[derive(Debug)]
pub(crate) enum ScanError {
    /// `ScanCancelled`: the client went away.
    Cancelled,
    /// `UniverseUnavailable(message)`.
    Unavailable(String),
    /// Anything else, as the response.
    Failed(Problem),
}

impl From<Problem> for ScanError {
    fn from(p: Problem) -> Self {
        Self::Failed(p)
    }
}

fn core_err(e: &baskfy_core::error::CoreError) -> ScanError {
    crate::logging::error(
        "baskfy_api.opportunities",
        "opportunities computation failed",
        &[("error", &e.to_string())],
    );
    ScanError::Failed(internal_error())
}

/// `Freshness`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Freshness {
    pub(crate) data_as_of: Option<NaiveDate>,
    pub(crate) expected: Option<NaiveDate>,
    pub(crate) status: DataStatus,
    pub(crate) reason: Option<String>,
}

/// `SwingScan`.
#[derive(Debug, Clone)]
pub(crate) struct SwingScan {
    pub(crate) scan_id: String,
    pub(crate) scanned_at: DateTime<Utc>,
    pub(crate) universe: String,
    pub(crate) freshness: Freshness,
    pub(crate) result: SwingScanResult,
    pub(crate) warnings: Vec<String>,
}

struct TtlCache<V> {
    ttl: Duration,
    size: usize,
    items: HashMap<String, (Instant, V)>,
}

impl<V: Clone> TtlCache<V> {
    fn new(ttl: Duration, size: usize) -> Self {
        Self {
            ttl,
            size,
            items: HashMap::new(),
        }
    }

    fn get(&mut self, key: &str) -> Option<V> {
        match self.items.get(key) {
            Some((at, v)) if at.elapsed() <= self.ttl => Some(v.clone()),
            Some(_) => {
                self.items.remove(key);
                None
            }
            None => None,
        }
    }

    fn put(&mut self, key: String, value: V) {
        if self.items.len() >= self.size
            && let Some(oldest) = self
                .items
                .iter()
                .min_by_key(|(_, (at, _))| *at)
                .map(|(k, _)| k.clone())
        {
            self.items.remove(&oldest);
        }
        self.items.insert(key, (Instant::now(), value));
    }
}

static SCAN_CACHE: LazyLock<Mutex<TtlCache<Arc<SwingScan>>>> =
    LazyLock::new(|| Mutex::new(TtlCache::new(CACHE_TTL, 32)));
static CONTRACT_CACHE: LazyLock<Mutex<TtlCache<Arc<Vec<ContractQuote>>>>> =
    LazyLock::new(|| Mutex::new(TtlCache::new(CONTRACT_CACHE_TTL, 64)));

fn cache_get<V: Clone>(cache: &Mutex<TtlCache<V>>, key: &str) -> Option<V> {
    cache.lock().ok().and_then(|mut c| c.get(key))
}

fn cache_put<V: Clone>(cache: &Mutex<TtlCache<V>>, key: String, value: V) {
    if let Ok(mut c) = cache.lock() {
        c.put(key, value);
    }
}

fn ist() -> FixedOffset {
    baskfy_providers::tokens::ist()
}

/// `expected_session(now, trading_days)`.
pub(crate) fn expected_session(now: DateTime<Utc>, trading_days: &[NaiveDate]) -> Option<NaiveDate> {
    let local = now.with_timezone(&ist());
    let mut cutoff = local.date_naive();
    if local.time() < NaiveTime::from_hms_opt(SESSION_DATA_READY_HOUR, 0, 0).unwrap_or_default() {
        cutoff -= Days::days(1);
    }
    trading_days.iter().copied().filter(|d| *d <= cutoff).max()
}

/// `freshness(data_as_of, expected)`.
pub(crate) fn freshness(data_as_of: Option<NaiveDate>, expected: Option<NaiveDate>) -> Freshness {
    let Some(as_of) = data_as_of else {
        return Freshness {
            data_as_of: None,
            expected,
            status: DataStatus::Unavailable,
            reason: Some("No daily bars are stored.".to_owned()),
        };
    };
    if let Some(exp) = expected
        && as_of < exp
    {
        return Freshness {
            data_as_of: Some(as_of),
            expected,
            status: DataStatus::Stale,
            reason: Some(format!(
                "Latest stored session is {as_of}; {exp} has closed and its bars are not loaded yet."
            )),
        };
    }
    Freshness {
        data_as_of: Some(as_of),
        expected,
        status: DataStatus::Fresh,
        reason: None,
    }
}

/// `universes(session)`.
pub(crate) async fn universes(pool: &PgPool) -> Result<Vec<(String, String)>, Problem> {
    sqlx::query_as(
        "SELECT slug, name FROM index_def WHERE is_universe IS true ORDER BY sort_order, slug",
    )
    .fetch_all(pool)
    .await
    .map_err(db)
}

/// `sectors(session)`.
pub(crate) async fn sectors(pool: &PgPool) -> Result<Vec<(String, String)>, Problem> {
    let slugs: Vec<String> = SECTOR_INDEX_SLUGS.iter().map(|s| (*s).to_owned()).collect();
    sqlx::query_as("SELECT slug, name FROM index_def WHERE slug = ANY($1) ORDER BY slug")
        .bind(slugs)
        .fetch_all(pool)
        .await
        .map_err(db)
}

async fn trading_days(pool: &PgPool, start: NaiveDate, end: NaiveDate) -> Result<Vec<NaiveDate>, Problem> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM trading_day WHERE exchange_id = $1::SMALLINT AND is_trading_day IS true \
         AND date >= $2 AND date <= $3 ORDER BY date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    Ok(rows.into_iter().map(|r| r.0).collect())
}

async fn latest_bar_date(pool: &PgPool, on_or_before: NaiveDate) -> Result<Option<NaiveDate>, Problem> {
    let (v,): (Option<NaiveDate>,) =
        sqlx::query_as("SELECT max(date) FROM ohlcv_daily WHERE date <= $1")
            .bind(on_or_before)
            .fetch_one(pool)
            .await
            .map_err(db)?;
    Ok(v)
}

/// `current_freshness(session, now)`.
pub(crate) async fn current_freshness(pool: &PgPool, now: DateTime<Utc>) -> Result<Freshness, Problem> {
    let today = now.with_timezone(&ist()).date_naive();
    let days = trading_days(pool, today - Days::days(15), today).await?;
    let as_of = latest_bar_date(pool, today).await?;
    Ok(freshness(as_of, expected_session(now, &days)))
}

fn repr(s: &str) -> String {
    baskfy_providers::py::repr_str(s)
}

async fn index_id(pool: &PgPool, slug: &str) -> Result<Option<i64>, Problem> {
    let row: Option<(i64,)> = sqlx::query_as("SELECT id::bigint FROM index_def WHERE slug = $1")
        .bind(slug)
        .fetch_optional(pool)
        .await
        .map_err(db)?;
    Ok(row.map(|r| r.0))
}

/// `members_on(session, slug, on)`.
async fn members_on(pool: &PgPool, slug: &str, on: NaiveDate) -> Result<BTreeSet<i64>, ScanError> {
    let Some(id) = index_id(pool, slug).await? else {
        return Err(ScanError::Unavailable(format!("Universe {} is not defined.", repr(slug))));
    };
    let (latest,): (Option<NaiveDate>,) = sqlx::query_as(
        "SELECT max(date) FROM index_member_daily WHERE index_id = $1 AND date <= $2",
    )
    .bind(id)
    .bind(on)
    .fetch_one(pool)
    .await
    .map_err(db)?;
    let Some(latest) = latest else {
        return Err(ScanError::Unavailable(format!(
            "Universe {} has no membership rows on or before {on}.",
            repr(slug)
        )));
    };
    let rows: Vec<(i64,)> = sqlx::query_as(
        "SELECT instrument_id::bigint FROM index_member_daily WHERE index_id = $1 AND date = $2",
    )
    .bind(id)
    .bind(latest)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    Ok(rows.into_iter().map(|r| r.0).collect())
}

/// `membership_history(session, slug, start, end)`.
async fn membership_history(
    pool: &PgPool,
    slug: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<BTreeMap<NaiveDate, BTreeSet<i64>>, ScanError> {
    let Some(id) = index_id(pool, slug).await? else {
        return Err(ScanError::Unavailable(format!("Universe {} is not defined.", repr(slug))));
    };
    let (first,): (Option<NaiveDate>,) = sqlx::query_as(
        "SELECT max(date) FROM index_member_daily WHERE index_id = $1 AND date <= $2",
    )
    .bind(id)
    .bind(start)
    .fetch_one(pool)
    .await
    .map_err(db)?;
    let rows: Vec<(NaiveDate, i64)> = sqlx::query_as(
        "SELECT date, instrument_id::bigint FROM index_member_daily WHERE index_id = $1 \
         AND date >= $2 AND date <= $3",
    )
    .bind(id)
    .bind(first.unwrap_or(start))
    .bind(end)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let mut grouped: BTreeMap<NaiveDate, BTreeSet<i64>> = BTreeMap::new();
    for (day, instrument_id) in rows {
        grouped.entry(day).or_default().insert(instrument_id);
    }
    Ok(grouped)
}

/// `load_bars` as columns (the frame is built from them, the contexts read them).
#[derive(Debug, Default)]
struct Bars {
    instrument_id: Vec<i64>,
    symbol: Vec<String>,
    date: Vec<NaiveDate>,
    open: Vec<f64>,
    high: Vec<f64>,
    low: Vec<f64>,
    close: Vec<f64>,
    volume: Vec<f64>,
    turnover: Vec<Option<f64>>,
    adj_factor: Vec<f64>,
}

type BarRow = (i64, String, NaiveDate, f64, f64, f64, f64, f64, Option<f64>, f64);

async fn load_bars(
    pool: &PgPool,
    ids: &BTreeSet<i64>,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Bars, Problem> {
    let mut bars = Bars::default();
    if ids.is_empty() {
        return Ok(bars);
    }
    let ids: Vec<i64> = ids.iter().copied().collect();
    let rows: Vec<BarRow> = sqlx::query_as(
        "SELECT o.instrument_id::bigint, i.symbol, o.date, o.open::float8, o.high::float8, \
         o.low::float8, o.close::float8, o.volume::float8, o.turnover::float8, \
         COALESCE(o.adj_factor::float8, 1.0::float8) \
         FROM ohlcv_daily o JOIN instrument i ON i.id = o.instrument_id \
         WHERE o.instrument_id = ANY($1) AND o.date >= $2 AND o.date <= $3 \
         AND i.instrument_type = 'EQ' AND i.series IN ('EQ', 'BE') \
         ORDER BY o.instrument_id, o.date",
    )
    .bind(ids)
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    for (id, symbol, date, open, high, low, close, volume, turnover, adj) in rows {
        bars.instrument_id.push(id);
        bars.symbol.push(symbol);
        bars.date.push(date);
        bars.open.push(open);
        bars.high.push(high);
        bars.low.push(low);
        bars.close.push(close);
        bars.volume.push(volume);
        bars.turnover.push(turnover);
        bars.adj_factor.push(adj);
    }
    Ok(bars)
}

fn bar_frame(b: Bars) -> Result<DataFrame, ScanError> {
    let col = |name: &str, v: Vec<f64>| Series::new(name.into(), v).into_column();
    DataFrame::new(
        b.instrument_id.len(),
        vec![
            Series::new("instrument_id".into(), b.instrument_id).into_column(),
            Series::new("symbol".into(), b.symbol).into_column(),
            Series::new("date".into(), b.date).into_column(),
            col("open", b.open),
            col("high", b.high),
            col("low", b.low),
            col("close", b.close),
            col("volume", b.volume),
            Series::new("turnover".into(), b.turnover).into_column(),
            col("adj_factor", b.adj_factor),
        ],
    )
    .map_err(|e| core_err(&e.into()))
}

/// `load_benchmark`: the slug used and its `(date, level)` frame.
async fn load_benchmark(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Option<(&'static str, DataFrame)>, ScanError> {
    for slug in BENCHMARK_SLUGS {
        let rows: Vec<(NaiveDate, f64)> = sqlx::query_as(
            "SELECT s.date, s.level::float8 FROM index_snapshot_daily s \
             JOIN index_def d ON d.id = s.index_id \
             WHERE d.slug = $1 AND s.date >= $2 AND s.date <= $3 AND s.level IS NOT NULL \
             ORDER BY s.date",
        )
        .bind(slug)
        .bind(start)
        .bind(end)
        .fetch_all(pool)
        .await
        .map_err(db)?;
        if !rows.is_empty() {
            let (dates, levels): (Vec<NaiveDate>, Vec<f64>) = rows.into_iter().unzip();
            let frame = DataFrame::new(
                dates.len(),
                vec![
                    Series::new("date".into(), dates).into_column(),
                    Series::new("level".into(), levels).into_column(),
                ],
            )
            .map_err(|e| core_err(&e.into()))?;
            return Ok(Some((slug, frame)));
        }
    }
    Ok(None)
}

async fn sector_map(pool: &PgPool, on: NaiveDate) -> Result<HashMap<i64, String>, Problem> {
    let slugs: Vec<String> = SECTOR_INDEX_SLUGS.iter().map(|s| (*s).to_owned()).collect();
    let (latest,): (Option<NaiveDate>,) = sqlx::query_as(
        "SELECT max(m.date) FROM index_member_daily m JOIN index_def d ON d.id = m.index_id \
         WHERE d.slug = ANY($1) AND m.date <= $2",
    )
    .bind(&slugs)
    .bind(on)
    .fetch_one(pool)
    .await
    .map_err(db)?;
    let Some(latest) = latest else {
        return Ok(HashMap::new());
    };
    let rows: Vec<(i64, String, i64)> = sqlx::query_as(
        "SELECT m.instrument_id::bigint, d.slug, count(*) OVER (PARTITION BY m.index_id) \
         FROM index_member_daily m JOIN index_def d ON d.id = m.index_id \
         WHERE m.date = $1 AND d.slug = ANY($2)",
    )
    .bind(latest)
    .bind(&slugs)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let mut best: HashMap<i64, (i64, String)> = HashMap::new();
    for (id, slug, members) in rows {
        match best.get(&id) {
            Some((n, _)) if members >= *n => {}
            _ => {
                best.insert(id, (members, slug));
            }
        }
    }
    Ok(best.into_iter().map(|(k, (_, s))| (k, s)).collect())
}

async fn fno_bans(pool: &PgPool, as_of: NaiveDate) -> Result<HashSet<String>, Problem> {
    let (latest,): (Option<NaiveDate>,) =
        sqlx::query_as("SELECT max(trade_date) FROM fo_underlying_daily WHERE trade_date <= $1")
            .bind(as_of)
            .fetch_one(pool)
            .await
            .map_err(db)?;
    let Some(latest) = latest else {
        return Ok(HashSet::new());
    };
    let rows: Vec<(String,)> = sqlx::query_as(
        "SELECT symbol FROM fo_underlying_daily WHERE trade_date = $1 AND in_ban IS true",
    )
    .bind(latest)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    Ok(rows.into_iter().map(|r| r.0).collect())
}

async fn contexts(
    pool: &PgPool,
    ids: &BTreeSet<i64>,
    as_of: NaiveDate,
    bars: &Bars,
    calendar: &[NaiveDate],
) -> Result<HashMap<i64, InstrumentContext>, Problem> {
    let rules = DEFAULT_SWING_PARAMS.data;
    let id_list: Vec<i64> = ids.iter().copied().collect();
    let names: HashMap<i64, String> = sqlx::query_as::<_, (i64, String)>(
        "SELECT id::bigint, name FROM instrument WHERE id = ANY($1)",
    )
    .bind(&id_list)
    .fetch_all(pool)
    .await
    .map_err(db)?
    .into_iter()
    .collect();
    let sectors_by_id = sector_map(pool, as_of).await?;
    let mut sorted = calendar.to_vec();
    sorted.sort();
    let keep = usize::try_from(rules.window_sessions).unwrap_or(0);
    let window: Vec<NaiveDate> = sorted[sorted.len().saturating_sub(keep)..].to_vec();
    let window_start = window.first().copied().unwrap_or(as_of);
    let actions: Vec<(i64, NaiveDate, String)> = sqlx::query_as(
        "SELECT instrument_id::bigint, ex_date, action_type::text FROM corporate_action \
         WHERE instrument_id = ANY($1) AND ex_date >= $2 AND ex_date <= $3 ORDER BY ex_date",
    )
    .bind(&id_list)
    .bind(window_start)
    .bind(as_of + Days::days(UPCOMING_ACTION_DAYS))
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let mut recent: HashMap<i64, Vec<(NaiveDate, String)>> = HashMap::new();
    let mut upcoming: HashMap<i64, Vec<(NaiveDate, String)>> = HashMap::new();
    for (id, ex_date, kind) in actions {
        let target = if ex_date > as_of { &mut upcoming } else { &mut recent };
        target.entry(id).or_default().push((ex_date, kind));
    }
    let bans = fno_bans(pool, as_of).await?;
    let mut present: HashMap<i64, Vec<NaiveDate>> = HashMap::new();
    let mut symbols: HashMap<i64, &str> = HashMap::new();
    for i in 0..bars.instrument_id.len() {
        let id = bars.instrument_id[i];
        symbols.insert(id, &bars.symbol[i]);
        if bars.date[i] >= window_start {
            present.entry(id).or_default().push(bars.date[i]);
        }
    }
    Ok(ids
        .iter()
        .map(|i| {
            let ctx = InstrumentContext {
                name: names.get(i).cloned().unwrap_or_default(),
                sector: sectors_by_id.get(i).cloned(),
                missing_sessions: missing_sessions(
                    present.get(i).map_or(&[][..], Vec::as_slice),
                    &window,
                    rules.window_sessions,
                ),
                recent_actions: recent.remove(i).unwrap_or_default(),
                upcoming_actions: upcoming.remove(i).unwrap_or_default(),
                in_fno_ban: bans.contains(symbols.get(i).copied().unwrap_or("")),
            };
            (*i, ctx)
        })
        .collect())
}

/// `cache_key(universe, filters, as_of)`.
fn cache_key(universe: &str, f: &SwingFilters, as_of: Option<NaiveDate>) -> String {
    let setups: Vec<&str> = f.setups.iter().map(|s| s.value()).collect();
    let sectors: Vec<&str> = f.sectors.iter().map(String::as_str).collect();
    format!(
        "{universe}|{as_of:?}|{setups:?}|{sectors:?}|{}|{}|{}|{}|{}|{}|{}|{}",
        f.min_turnover_cr,
        f.min_price,
        f.horizon_min,
        f.horizon_max,
        f.atr_pct_min,
        f.atr_pct_max,
        f.min_rr,
        f.limit
    )
}

/// Where progress frames go: the stream's sender, or nowhere.
pub(crate) type Progress<'a> = Option<&'a mpsc::Sender<String>>;

async fn report(progress: Progress<'_>, stage: &str, done: i64, total: i64) {
    if let Some(tx) = progress {
        let _ = tx.send(super::views::progress_frame(stage, done, total)).await;
    }
}

async fn step(progress: Progress<'_>, stage: &str, done: i64, total: i64) -> Result<(), ScanError> {
    if progress.is_some_and(mpsc::Sender::is_closed) {
        return Err(ScanError::Cancelled);
    }
    report(progress, stage, done, total).await;
    Ok(())
}

async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> Result<T, baskfy_core::error::CoreError> + Send + 'static,
) -> Result<T, ScanError> {
    match tokio::task::spawn_blocking(f).await {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Err(core_err(&e)),
        Err(_) => Err(ScanError::Failed(internal_error())),
    }
}

/// `run_swing_scan(session, universe=, filters=, now=, progress=, cancelled=)`.
pub(crate) async fn run_swing_scan(
    pool: &PgPool,
    universe: &str,
    filters: &SwingFilters,
    now: DateTime<Utc>,
    progress: Progress<'_>,
) -> Result<Arc<SwingScan>, ScanError> {
    let total = 5;
    step(progress, "Checking data freshness", 0, total).await?;
    let fresh = current_freshness(pool, now).await?;
    let key = cache_key(universe, filters, fresh.data_as_of);
    if let Some(cached) = cache_get(&SCAN_CACHE, &key) {
        report(progress, "Served from cache", total, total).await;
        return Ok(cached);
    }
    let scan_id = crate::logging::new_request_id();
    let Some(as_of) = fresh.data_as_of else {
        return Ok(Arc::new(SwingScan {
            scan_id,
            scanned_at: now,
            universe: universe.to_owned(),
            freshness: fresh,
            result: SwingScanResult {
                as_of: None,
                counts: ScanCounts::default(),
                opportunities: Vec::new(),
                warnings: Vec::new(),
            },
            warnings: Vec::new(),
        }));
    };
    let permit = SCAN_SLOTS.acquire().await.map_err(|_| ScanError::Failed(internal_error()))?;
    let started = Instant::now();
    step(progress, "Resolving universe", 1, total).await?;
    let mut ids = members_on(pool, universe, as_of).await?;
    let mut warnings: Vec<String> = Vec::new();
    let cap = max_universe();
    if ids.len() > cap {
        warnings.push(format!("Universe trimmed to {cap} names for this scan."));
        ids = ids.into_iter().take(cap).collect();
    }
    let names = ids.len();
    let start = as_of - Days::days(HISTORY_CALENDAR_DAYS);
    step(progress, "Loading daily bars", 2, total).await?;
    let bars = load_bars(pool, &ids, start, as_of).await?;
    let bench = load_benchmark(pool, start, as_of).await?;
    match &bench {
        None => warnings.push("No benchmark series stored; relative-strength setup unavailable.".to_owned()),
        Some((slug, _)) if *slug != BENCHMARK_SLUGS[0] => {
            warnings.push(format!("Benchmark fell back to {slug}."));
        }
        Some(_) => {}
    }
    let calendar = trading_days(pool, start, as_of).await?;
    let ctx = contexts(pool, &ids, as_of, &bars, &calendar).await?;
    step(progress, "Computing indicators", 3, total).await?;
    let frame = bar_frame(bars)?;
    let bench_frame = bench.map(|(_, f)| f);
    let indicated = blocking(move || {
        with_indicators(&frame, bench_frame.as_ref(), &DEFAULT_SWING_PARAMS)
    })
    .await?;
    step(progress, "Evaluating setups", 4, total).await?;
    let filters_owned = filters.clone();
    let result = blocking(move || {
        scan(&indicated, as_of, &ctx, &filters_owned, &DEFAULT_SWING_PARAMS)
    })
    .await?;
    drop(permit);
    crate::logging::info(
        "baskfy_api.opportunities",
        "swing scan finished",
        &[
            ("universe", universe),
            ("names", &names.to_string()),
            ("elapsed_ms", &started.elapsed().as_millis().to_string()),
        ],
    );
    warnings.extend(result.warnings.iter().cloned());
    let out = Arc::new(SwingScan {
        scan_id,
        scanned_at: now,
        universe: universe.to_owned(),
        freshness: fresh,
        result,
        warnings,
    });
    cache_put(&SCAN_CACHE, key, Arc::clone(&out));
    report(progress, "Done", total, total).await;
    Ok(out)
}

/// `run_validation(session, setup=, universe=, start=, end=, filters=, slippage_bps=)`.
pub(crate) async fn run_validation(
    pool: &PgPool,
    setup: SetupType,
    universe: &str,
    start: NaiveDate,
    end: NaiveDate,
    filters: SwingFilters,
    slippage_bps: f64,
) -> Result<ValidationResult, ScanError> {
    let _permit = SCAN_SLOTS.acquire().await.map_err(|_| ScanError::Failed(internal_error()))?;
    let history = membership_history(pool, universe, start, end).await?;
    let mut ids: BTreeSet<i64> = history.values().flatten().copied().collect();
    if ids.is_empty() {
        return Err(ScanError::Unavailable(format!(
            "Universe {} has no membership rows between {start} and {end}.",
            repr(universe)
        )));
    }
    if ids.len() > VALIDATION_MAX_UNIVERSE {
        ids = ids.into_iter().take(VALIDATION_MAX_UNIVERSE).collect();
    }
    let warm = start - Days::days(HISTORY_CALENDAR_DAYS);
    let tail = end + Days::days(DEFAULT_SWING_PARAMS.relative_strength.horizon_max * 2);
    let bars = load_bars(pool, &ids, warm, tail).await?;
    let bench = load_benchmark(pool, warm, tail).await?.map(|(_, f)| f);
    let frame = bar_frame(bars)?;
    blocking(move || {
        let indicated = with_indicators(&frame, bench.as_ref(), &DEFAULT_SWING_PARAMS)?;
        validate_setup(
            &indicated,
            setup,
            start,
            end,
            Some(&history),
            &filters,
            slippage_bps,
            &DEFAULT_SWING_PARAMS,
        )
    })
    .await
}

/// `ProviderState`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ProviderState {
    pub(crate) available: bool,
    pub(crate) reason: Option<String>,
    pub(crate) dependency: Option<String>,
}

/// `OptionsAnalysis`.
#[derive(Debug, Clone)]
pub(crate) struct OptionsAnalysis {
    pub(crate) provider: ProviderState,
    pub(crate) spot: Option<Decimal>,
    pub(crate) spot_as_of: Option<DateTime<FixedOffset>>,
    pub(crate) analyzed_at: DateTime<Utc>,
    pub(crate) status: DataStatus,
    pub(crate) result: Option<OptionsResult>,
    pub(crate) warnings: Vec<String>,
}

/// `provider_state()`.
pub(crate) fn provider_state() -> Result<ProviderState, Problem> {
    match crate::live_prices::quotes_permitted() {
        Ok(true) => Ok(ProviderState {
            available: true,
            reason: None,
            dependency: None,
        }),
        Ok(false) => Ok(ProviderState {
            available: false,
            reason: Some(
                "No live Kite session is available to this API, so option quotes cannot be read."
                    .to_owned(),
            ),
            dependency: Some(KITE_DEPENDENCY.to_owned()),
        }),
        Err(e) => {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", e.python_type())],
            );
            Err(internal_error())
        }
    }
}

fn aware(t: &PyDateTime) -> Option<DateTime<FixedOffset>> {
    match t {
        PyDateTime::Aware(a) => Some(*a),
        PyDateTime::Naive(n) => n.and_local_timezone(ist()).single(),
    }
}

async fn read_provider() -> Result<KiteProvider, String> {
    let key = std::env::var("BASKFY_KITE_API_KEY").unwrap_or_default();
    let key = key.trim();
    if key.is_empty() {
        return Err("BASKFY_KITE_API_KEY is not set".to_owned());
    }
    crate::live_prices::kite_provider(key).await
}

async fn contracts_for(provider: &KiteProvider, underlying: &str) -> Result<Arc<Vec<ContractQuote>>, String> {
    if let Some(hit) = cache_get(&CONTRACT_CACHE, underlying) {
        return Ok(hit);
    }
    let records = provider
        .option_contracts(underlying)
        .await
        .map_err(|e| e.to_string())?;
    let contracts: Arc<Vec<ContractQuote>> = Arc::new(
        records
            .iter()
            .map(|c| ContractQuote {
                tradingsymbol: c.tradingsymbol.clone(),
                underlying: c.underlying.clone(),
                expiry: c.expiry,
                strike: c.strike,
                option_type: match c.option_type {
                    KiteOptionType::Ce => OptionType::Ce,
                    KiteOptionType::Pe => OptionType::Pe,
                },
                lot_size: c.lot_size,
                bid: None,
                ask: None,
                volume: None,
                oi: None,
                quote_ts: None,
            })
            .collect(),
    );
    cache_put(&CONTRACT_CACHE, underlying.to_owned(), Arc::clone(&contracts));
    Ok(contracts)
}

type Quoted = (Option<Decimal>, Option<DateTime<FixedOffset>>, Vec<ContractQuote>);

/// `_quote_contracts(request, now)`; `Err` is the text of the failure.
async fn quote_contracts(request: &OptionsRequest, now: DateTime<Utc>) -> Result<Quoted, String> {
    let provider = read_provider().await?;
    let underlying = request.underlying.to_uppercase();
    let spot_key = INDEX_SPOT_KEYS
        .iter()
        .find(|(k, _)| *k == underlying)
        .map_or_else(|| format!("NSE:{underlying}"), |(_, v)| (*v).to_owned());
    let spot_rows = provider
        .option_quotes(&[spot_key.as_str()])
        .await
        .map_err(|e| e.to_string())?;
    let Some(spot_row) = spot_rows.iter().find(|r| r.key() == spot_key) else {
        return Ok((None, None, Vec::new()));
    };
    let spot = match spot_row.last_price {
        Some(p) if p > Decimal::ZERO => p,
        _ => return Ok((None, None, Vec::new())),
    };
    let today = now.with_timezone(&ist()).date_naive();
    let all = contracts_for(&provider, &underlying).await?;
    let contracts: Vec<&ContractQuote> = all
        .iter()
        .filter(|c| {
            let dte = (c.expiry - today).num_days();
            request.min_dte <= dte && dte <= request.max_dte
        })
        .collect();
    let expiries: BTreeSet<NaiveDate> = contracts.iter().map(|c| c.expiry).collect();
    let max_expiries = usize::try_from(DEFAULT_OPTIONS_PARAMS.max_expiries).unwrap_or(0);
    let each_side = usize::try_from(DEFAULT_OPTIONS_PARAMS.strikes_each_side + 1).unwrap_or(0);
    let mut wanted: Vec<&ContractQuote> = Vec::new();
    for expiry in expiries.into_iter().take(max_expiries) {
        let pool: Vec<&ContractQuote> = contracts.iter().copied().filter(|c| c.expiry == expiry).collect();
        let mut strikes: Vec<Decimal> = pool.iter().map(|c| c.strike).collect::<BTreeSet<_>>().into_iter().collect();
        strikes.sort_by_key(|a| ((*a - spot).abs(), *a));
        let keep: HashSet<Decimal> = strikes.into_iter().take(2 * each_side + 1).collect();
        wanted.extend(pool.into_iter().filter(|c| keep.contains(&c.strike)));
    }
    let keys: Vec<String> = wanted.iter().map(|c| format!("NFO:{}", c.tradingsymbol)).collect();
    let key_refs: Vec<&str> = keys.iter().map(String::as_str).collect();
    let quotes: HashMap<String, _> = provider
        .option_quotes(&key_refs)
        .await
        .map_err(|e| e.to_string())?
        .into_iter()
        .map(|r| (r.symbol.clone(), r))
        .collect();
    let out = wanted
        .into_iter()
        .map(|c| {
            let q = quotes.get(&c.tradingsymbol);
            ContractQuote {
                bid: q.and_then(|q| q.bids.first().map(|l| l.price)),
                ask: q.and_then(|q| q.asks.first().map(|l| l.price)),
                volume: q.map(|q| q.volume),
                oi: q.and_then(|q| q.oi),
                quote_ts: q.and_then(|q| q.as_of.as_ref().and_then(aware)),
                ..c.clone()
            }
        })
        .collect();
    Ok((Some(spot), spot_row.as_of.as_ref().and_then(aware), out))
}

/// `run_options_analysis(request, now=, max_quote_age_s=)`.
pub(crate) async fn run_options_analysis(
    request: &OptionsRequest,
    now: DateTime<Utc>,
    max_quote_age_s: i64,
) -> Result<OptionsAnalysis, Problem> {
    let state = provider_state()?;
    let empty = |provider: ProviderState, warnings: Vec<String>| OptionsAnalysis {
        provider,
        spot: None,
        spot_as_of: None,
        analyzed_at: now,
        status: DataStatus::Unavailable,
        result: None,
        warnings,
    };
    if !state.available {
        return Ok(empty(state, Vec::new()));
    }
    let (spot, spot_ts, quotes) = match quote_contracts(request, now).await {
        Ok(v) => v,
        Err(text) => {
            let failed = ProviderState {
                available: false,
                reason: Some(format!("Kite read failed: {text}")),
                dependency: Some(KITE_DEPENDENCY.to_owned()),
            };
            return Ok(empty(failed, Vec::new()));
        }
    };
    let Some(spot) = spot else {
        let warning = format!("No live quote for underlying {}.", request.underlying);
        return Ok(empty(state, vec![warning]));
    };
    let mut warnings = Vec::new();
    let mut status = DataStatus::Fresh;
    let stale = spot_ts.is_none_or(|ts| {
        (now - ts.with_timezone(&Utc)).num_microseconds().unwrap_or(i64::MAX)
            > max_quote_age_s.saturating_mul(1_000_000)
    });
    if stale {
        status = DataStatus::Stale;
        warnings.push(
            "Underlying quote is older than the quote-age limit (market may be shut).".to_owned(),
        );
    }
    if quotes.is_empty() {
        warnings.push(format!(
            "{} has no listed options in the requested window.",
            request.underlying
        ));
    }
    let mut result = analyze(
        request,
        &quotes,
        spot,
        &now.fixed_offset(),
        &CostRates::default(),
        &DEFAULT_OPTIONS_PARAMS,
    )
    .map_err(|e| match core_err(&e) {
        ScanError::Failed(p) => p,
        _ => internal_error(),
    })?;
    if status == DataStatus::Stale {
        for c in &mut result.candidates {
            c.actionable = false;
        }
    }
    Ok(OptionsAnalysis {
        provider: state,
        spot: Some(spot),
        spot_as_of: spot_ts,
        analyzed_at: now,
        status,
        result: Some(result),
        warnings,
    })
}
