//! `app/analytics/index_cache.py`: the index OHLC adapter and repository for the regime
//! overlay — the strict instrument resolver, the injected session clock, the `index_series`
//! repository (idempotent transactional upsert, final-only reads, warm-up, session alignment)
//! and the chunked, retried Kite fetch that brings an index up to date.
//!
//! Safety properties kept: instrument resolution **fails** on zero or several matches (never the
//! first row); a session that has not closed is stored `is_final=0` and never returned to the
//! signal path; re-running a day's update changes nothing; a daily update fetches only from the
//! last cached session; the clock is injected. Kite is reached only through [`IndexKite`]
//! (the desk client's limited `instruments`/`historical` wrappers, SW20) — reads, never an order.

use std::collections::BTreeSet;
use std::path::Path;

use async_trait::async_trait;
use chrono::{Duration, NaiveDate, NaiveDateTime, NaiveTime};

use crate::lanes::exec_core::ratelimit::GatewayLimits;
use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::{PyDict, PyValue, dict};

use super::regime::RegimeConfig;
use super::regime::engine::{IndexSignals, build_index_signals};

/// `INSTRUMENT_CACHE`: every instrument (benchmark.py keeps a separate, index-only cache).
pub const INSTRUMENT_CACHE: &str = "data/.instruments_all.json";
/// `CHUNK_DAYS`: Kite caps a daily-candle request; long histories are walked in chunks.
pub const CHUNK_DAYS: i64 = 1800;

/// `core.regime.Candle`.
#[derive(Debug, Clone, PartialEq)]
pub struct Candle {
    /// `date`.
    pub date: NaiveDate,
    /// `open`.
    pub open: Option<f64>,
    /// `high`.
    pub high: Option<f64>,
    /// `low`.
    pub low: Option<f64>,
    /// `close` (`None` only where the stored value is NULL).
    pub close: Option<f64>,
    /// `is_final`.
    pub is_final: bool,
}

/// `MARKET_CLOSE`.
#[must_use]
pub fn market_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default()
}

/// `Clock.session_is_closed(session)` for an IST wall time `now`.
#[must_use]
pub fn session_is_closed(now: NaiveDateTime, session: NaiveDate) -> bool {
    let today = now.date();
    if session < today {
        return true;
    }
    if session > today {
        return false;
    }
    now.time() >= market_close()
}

/// `InstrumentNotFoundError` / `AmbiguousInstrumentError`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ResolveError {
    /// No instrument matched.
    #[error("{0}")]
    NotFound(String),
    /// More than one matched; refusing to choose.
    #[error("{0}")]
    Ambiguous(String),
}

fn field(row: &crate::wire::PyDict, key: &str) -> String {
    match row.get(key) {
        Some(PyValue::Str(s)) => s.clone(),
        Some(PyValue::Int(i)) => i.to_string(),
        _ => String::new(),
    }
}

/// `resolve_index_instrument(instruments, index_name, cfg)`: exactly one instrument or an
/// error naming what was accepted.
///
/// # Errors
/// [`ResolveError`] on zero or several matches.
pub fn resolve_index_instrument<'a>(
    instruments: &'a [crate::wire::PyDict],
    index_name: &str,
    cfg: &RegimeConfig,
) -> Result<&'a crate::wire::PyDict, ResolveError> {
    let mut accepted = vec![index_name.trim().to_uppercase()];
    if let Some((_, aliases)) = cfg.index_aliases.iter().find(|(k, _)| k == index_name) {
        accepted.extend(aliases.iter().map(|a| a.trim().to_uppercase()));
    }
    let hits: Vec<&crate::wire::PyDict> = instruments
        .iter()
        .filter(|r| {
            let exchange = match r.get("exchange") {
                Some(PyValue::Str(s)) => s.to_uppercase(),
                _ => cfg.exchange.to_uppercase(),
            };
            field(r, "segment").to_uppercase() == cfg.segment.to_uppercase()
                && exchange == cfg.exchange.to_uppercase()
                && accepted.contains(&field(r, "tradingsymbol").trim().to_uppercase())
        })
        .collect();
    let mut sorted_accepted = accepted.clone();
    sorted_accepted.sort();
    sorted_accepted.dedup();
    let repr_list = |v: &[String]| {
        format!(
            "[{}]",
            v.iter()
                .map(|s| format!("'{s}'"))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };
    match hits.len() {
        0 => {
            let first = index_name
                .split_whitespace()
                .next()
                .unwrap_or("")
                .to_uppercase();
            let mut near: Vec<String> = instruments
                .iter()
                .map(|r| field(r, "tradingsymbol"))
                .filter(|s| s.to_uppercase().contains(&first))
                .collect();
            near.sort();
            near.dedup();
            near.truncate(10);
            let near_text = if near.is_empty() {
                "none".to_owned()
            } else {
                repr_list(&near)
            };
            Err(ResolveError::NotFound(format!(
                "index '{index_name}' not found on {}/{}. Accepted symbols/aliases: {}. Nearby: {near_text}",
                cfg.exchange,
                cfg.segment,
                repr_list(&sorted_accepted)
            )))
        }
        1 => Ok(hits[0]),
        n => Err(ResolveError::Ambiguous(format!(
            "index '{index_name}' matched {n} instruments: [{}]. Narrow the configured symbol/aliases — refusing to choose.",
            hits.iter()
                .map(|h| format!(
                    "('{}', {})",
                    field(h, "tradingsymbol"),
                    field(h, "instrument_token")
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ))),
    }
}

/// `IndexRepository.load(index_name, start=, end=, final_only=)`.
///
/// # Errors
/// The driver's.
pub async fn load(
    conn: &mut Conn,
    index_name: &str,
    start: Option<NaiveDate>,
    end: Option<NaiveDate>,
    final_only: bool,
) -> Result<Vec<Candle>, StorageError> {
    let mut sql =
        "SELECT date, open, high, low, close, is_final FROM index_series WHERE index_name=?"
            .to_owned();
    let mut args: Vec<Param> = vec![index_name.into()];
    if final_only {
        sql.push_str(" AND is_final=1");
    }
    if let Some(s) = start {
        sql.push_str(" AND date >= ?");
        args.push(s.format("%Y-%m-%d").to_string().into());
    }
    if let Some(e) = end {
        sql.push_str(" AND date <= ?");
        args.push(e.format("%Y-%m-%d").to_string().into());
    }
    sql.push_str(" ORDER BY date");
    let cur = conn.execute(&sql, &args).await?;
    let mut out = Vec::with_capacity(cur.rows.len());
    for r in &cur.rows {
        let date = r
            .get("date")
            .and_then(DbValue::as_str)
            .and_then(super::ops::parse_iso_date)
            .ok_or_else(|| {
                StorageError::Other("ValueError: Invalid isoformat string".to_owned())
            })?;
        out.push(Candle {
            date,
            open: r.get("open").and_then(DbValue::as_f64),
            high: r.get("high").and_then(DbValue::as_f64),
            low: r.get("low").and_then(DbValue::as_f64),
            close: r.get("close").and_then(DbValue::as_f64),
            is_final: super::regime_store::db_truthy(r.get("is_final")),
        });
    }
    Ok(out)
}

/// `IndexRepository.last_date(index_name, final_only=)`.
///
/// # Errors
/// The driver's.
pub async fn last_date(
    conn: &mut Conn,
    index_name: &str,
    final_only: bool,
) -> Result<Option<NaiveDate>, StorageError> {
    let mut sql = "SELECT MAX(date) d FROM index_series WHERE index_name=?".to_owned();
    if final_only {
        sql.push_str(" AND is_final=1");
    }
    let cur = conn.execute(&sql, &[index_name.into()]).await?;
    // `dt.date.fromisoformat(row["d"]) if row and row["d"] else None`: a stored value that is
    // not an ISO date raises, as in Python; only an absent or empty one is `None`.
    match cur
        .fetchone()
        .and_then(|r| r.get("d"))
        .and_then(DbValue::as_str)
    {
        None | Some("") => Ok(None),
        Some(s) => super::ops::parse_iso_date(s).map(Some).ok_or_else(|| {
            StorageError::Other(format!("ValueError: Invalid isoformat string: '{s}'"))
        }),
    }
}

/// `Clock`: the IST wall clock the fetch judges a session's finality by (tests fix it).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SessionClock {
    /// `now()`, as an IST wall time.
    pub now: NaiveDateTime,
}

impl SessionClock {
    /// `Clock(fixed=...)`.
    #[must_use]
    pub const fn fixed(now: NaiveDateTime) -> Self {
        Self { now }
    }

    /// `Clock()`: the desk's clock, read now.
    #[must_use]
    pub fn of(clock: &dyn crate::clock::Clock) -> Self {
        Self {
            now: clock.now_ist().naive_local(),
        }
    }

    /// `today()`.
    #[must_use]
    pub fn today(&self) -> NaiveDate {
        self.now.date()
    }

    /// `session_is_closed(session)`.
    #[must_use]
    pub fn session_is_closed(&self, session: NaiveDate) -> bool {
        session_is_closed(self.now, session)
    }
}

/// `normalise_session_date(value)`: Kite returns tz-aware datetimes; the trading date is the IST
/// calendar date of an aware one, the date of a naive one, else `date.fromisoformat(str(v)[:10])`.
///
/// # Errors
/// What `fromisoformat` raises on.
pub fn normalise_session_date(value: &PyValue) -> Result<NaiveDate, String> {
    use baskfy_providers::records::PyDateTime;
    match value {
        PyValue::DateTime(PyDateTime::Aware(t)) => {
            Ok(t.with_timezone(&crate::clock::ist()).date_naive())
        }
        PyValue::DateTime(PyDateTime::Naive(t)) => Ok(t.date()),
        PyValue::Date(d) => Ok(*d),
        other => {
            let text: String = other.py_str().chars().take(10).collect();
            super::ops::parse_iso_date(&text)
                .ok_or_else(|| format!("ValueError: Invalid isoformat string: '{text}'"))
        }
    }
}

/// The two Kite reads the cache makes — the desk client's own limited wrappers (`Kite.instruments`
/// and `Kite.historical`). Never an order verb.
#[async_trait]
pub trait IndexKite: Send + Sync {
    /// `kite.instruments()` (exchange `NSE`).
    async fn instruments(&self) -> Result<Vec<PyValue>, String>;

    /// `kite.historical(token, start, end, interval)`.
    async fn historical(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
        interval: &str,
    ) -> Result<Vec<PyValue>, String>;
}

#[async_trait]
impl IndexKite for crate::lanes::exec_core::kite_client::DeskKite {
    async fn instruments(&self) -> Result<Vec<PyValue>, String> {
        Self::instruments(self, "NSE").await.map_err(|e| e.to_string())
    }

    async fn historical(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
        interval: &str,
    ) -> Result<Vec<PyValue>, String> {
        Self::historical(
            self,
            token,
            &start.format("%Y-%m-%d").to_string(),
            &end.format("%Y-%m-%d").to_string(),
            interval,
        )
        .await
        .map_err(|e| e.to_string())
    }
}

/// What a fetch or an update refuses.
#[derive(Debug, thiserror::Error)]
pub enum CacheError {
    /// `InstrumentNotFoundError` / `AmbiguousInstrumentError`.
    #[error(transparent)]
    Resolve(#[from] ResolveError),
    /// `RuntimeError: historical_data failed after N attempts: ...`, or a malformed candle.
    #[error("{0}")]
    Runtime(String),
    /// The driver's.
    #[error(transparent)]
    Storage(#[from] StorageError),
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn opt_text(v: Option<&str>) -> PyValue {
    v.map_or(PyValue::None, |s| PyValue::Str(s.to_owned()))
}

/// `IndexRepository.upsert(index_name, candles, instrument_token=)`: transactional and
/// idempotent; a provisional candle is updated in place when it becomes final. `now` is
/// `datetime.now(IST).isoformat(timespec="seconds")`.
///
/// # Errors
/// The driver's; a candle without a close (`float(None)` raises in Python).
pub async fn upsert(
    conn: &mut Conn,
    index_name: &str,
    candles: &[Candle],
    instrument_token: Option<i64>,
    now: &str,
) -> Result<PyDict, CacheError> {
    let mut rows: Vec<Vec<Param>> = Vec::with_capacity(candles.len());
    for c in candles {
        let close = c.close.ok_or_else(|| {
            CacheError::Runtime(
                "TypeError: float() argument must be a string or a real number, not 'NoneType'"
                    .to_owned(),
            )
        })?;
        rows.push(vec![
            index_name.into(),
            iso(c.date).into(),
            Param::from(c.open),
            Param::from(c.high),
            Param::from(c.low),
            Param::from(close),
            Param::from(instrument_token),
            Param::from(i64::from(c.is_final)),
            now.into(),
        ]);
    }
    let (Some(first), Some(last)) = (candles.first(), candles.last()) else {
        return Ok(dict([
            ("index_name", PyValue::Str(index_name.to_owned())),
            ("written", PyValue::Int(0)),
            ("first", PyValue::None),
            ("last", PyValue::None),
        ]));
    };
    let written = i64::try_from(rows.len()).unwrap_or(i64::MAX);
    conn.transaction(move |c| {
        Box::pin(async move {
            c.executemany(
                "INSERT INTO index_series(index_name, date, open, high, low, close, \
                 instrument_token, is_final, updated_at) VALUES(?,?,?,?,?,?,?,?,?) \
                 ON CONFLICT(index_name, date) DO UPDATE SET open=excluded.open, \
                 high=excluded.high, low=excluded.low, close=excluded.close, \
                 instrument_token=COALESCE(excluded.instrument_token, \
                 index_series.instrument_token), is_final=excluded.is_final, \
                 updated_at=excluded.updated_at",
                &rows,
            )
            .await?;
            Ok(())
        })
    })
    .await?;
    Ok(dict([
        ("index_name", PyValue::Str(index_name.to_owned())),
        ("written", PyValue::Int(written)),
        ("first", opt_text(Some(&iso(first.date)))),
        ("last", opt_text(Some(&iso(last.date)))),
    ]))
}

/// `IndexRepository.session_dates(index_name)`: the final sessions.
///
/// # Errors
/// The driver's; a stored date Python could not parse.
pub async fn session_dates(
    conn: &mut Conn,
    index_name: &str,
) -> Result<BTreeSet<NaiveDate>, StorageError> {
    let cur = conn
        .execute(
            "SELECT date FROM index_series WHERE index_name=? AND is_final=1",
            &[index_name.into()],
        )
        .await?;
    cur.rows
        .iter()
        .map(|r| {
            let text = r.get("date").and_then(DbValue::as_str).unwrap_or_default();
            super::ops::parse_iso_date(text).ok_or_else(|| {
                StorageError::Other(format!("ValueError: Invalid isoformat string: '{text}'"))
            })
        })
        .collect()
}

/// `IndexRepository.coverage(index_name)`.
///
/// # Errors
/// The driver's.
pub async fn coverage(conn: &mut Conn, index_name: &str) -> Result<PyDict, StorageError> {
    let cur = conn
        .execute(
            "SELECT COUNT(*) n, SUM(is_final) f, MIN(date) first, MAX(date) last \
             FROM index_series WHERE index_name=?",
            &[index_name.into()],
        )
        .await?;
    let row = cur.fetchone();
    let get = |k: &str| row.and_then(|r| r.get(k)).cloned().unwrap_or(DbValue::Null);
    let or0 = |v: DbValue| match v {
        DbValue::Null => PyValue::Int(0),
        other => super::web::db_to_py(&other),
    };
    Ok(dict([
        ("index_name", PyValue::Str(index_name.to_owned())),
        ("rows", or0(get("n"))),
        ("final_rows", or0(get("f"))),
        ("first", super::web::db_to_py(&get("first"))),
        ("last", super::web::db_to_py(&get("last"))),
    ]))
}

/// `IndexRepository.has_warmup(index_name, cfg)`: `max(MA lengths) + confirm_days` final
/// sessions must exist. `(ok, have, need)`.
///
/// # Errors
/// The driver's.
pub async fn has_warmup(
    conn: &mut Conn,
    index_name: &str,
    cfg: &RegimeConfig,
) -> Result<(bool, i64, i64), StorageError> {
    let need = cfg.ma_lengths.iter().copied().max().unwrap_or(0) + cfg.confirm_days;
    let cur = conn
        .execute(
            "SELECT COUNT(*) n FROM index_series WHERE index_name=? AND is_final=1",
            &[index_name.into()],
        )
        .await?;
    let have = cur
        .fetchone()
        .and_then(|r| r.get("n"))
        .and_then(DbValue::as_i64)
        .unwrap_or(0);
    Ok((have >= need, have, need))
}

/// `IndexRepository.aligned_sessions(index_names)`: sessions where **every** index has a final
/// candle — an intersection, never a union, never forward-filled.
///
/// # Errors
/// The driver's.
pub async fn aligned_sessions(
    conn: &mut Conn,
    index_names: &[String],
) -> Result<Vec<NaiveDate>, StorageError> {
    let mut common: Option<BTreeSet<NaiveDate>> = None;
    for n in index_names {
        let set = session_dates(conn, n).await?;
        common = Some(match common {
            None => set,
            Some(c) => c.intersection(&set).copied().collect(),
        });
    }
    Ok(common.unwrap_or_default().into_iter().collect())
}

/// `IndexRepository.signal_session_for_week(index_names, week_end, max_lookback_days=7)`: the
/// latest aligned session at or before the week end (a holiday Friday falls back to Thursday).
///
/// # Errors
/// The driver's.
pub async fn signal_session_for_week(
    conn: &mut Conn,
    index_names: &[String],
    week_end: NaiveDate,
    max_lookback_days: i64,
) -> Result<Option<NaiveDate>, StorageError> {
    let floor = week_end - Duration::days(max_lookback_days);
    Ok(aligned_sessions(conn, index_names)
        .await?
        .into_iter()
        .rfind(|d| floor <= *d && *d <= week_end))
}

fn row_get<'a>(row: &'a PyValue, key: &str) -> Option<&'a PyValue> {
    match row {
        PyValue::Map(m) => m.get(key),
        _ => None,
    }
}

fn opt_num(v: Option<&PyValue>) -> Option<f64> {
    match v {
        Some(PyValue::Float(f)) => Some(*f),
        #[allow(clippy::cast_precision_loss)]
        Some(PyValue::Int(i)) => Some(*i as f64),
        _ => None,
    }
}

/// `_with_retry(kite.historical, ...)`: transient broker errors retried with exponential backoff
/// (`base_delay * 2**attempt` seconds); the last error is reported after `attempts`.
async fn historical_with_retry(
    kite: &dyn IndexKite,
    token: i64,
    start: NaiveDate,
    end: NaiveDate,
    attempts: u32,
    base_delay: f64,
) -> Result<Vec<PyValue>, CacheError> {
    let mut last = String::new();
    for attempt in 0..attempts {
        match kite.historical(token, start, end, "day").await {
            Ok(rows) => return Ok(rows),
            Err(e) => {
                last = e;
                if attempt + 1 == attempts {
                    break;
                }
                let delay = base_delay * f64::from(2_u32.pow(attempt));
                tokio::time::sleep(std::time::Duration::from_secs_f64(delay.max(0.0))).await;
            }
        }
    }
    Err(CacheError::Runtime(format!(
        "historical_data failed after {attempts} attempts: {last}"
    )))
}

/// `fetch_candles(kite, token, start, end, limits=, clock=, attempts=3)`: chunked daily candles;
/// a session that has not closed is marked `is_final=false`.
///
/// # Errors
/// [`CacheError::Runtime`] when a chunk fails past the retry budget or a row is malformed.
#[allow(clippy::too_many_arguments)]
pub async fn fetch_candles(
    kite: &dyn IndexKite,
    token: i64,
    start: NaiveDate,
    end: NaiveDate,
    limits: &dyn GatewayLimits,
    clock: SessionClock,
    attempts: u32,
    base_delay: f64,
) -> Result<Vec<Candle>, CacheError> {
    let mut out = Vec::new();
    let mut cursor = start;
    while cursor <= end {
        let chunk_end = (cursor + Duration::days(CHUNK_DAYS)).min(end);
        limits.api_slot().await;
        let rows = historical_with_retry(kite, token, cursor, chunk_end, attempts, base_delay).await?;
        for r in &rows {
            let d = normalise_session_date(row_get(r, "date").unwrap_or(&PyValue::None))
                .map_err(CacheError::Runtime)?;
            let close = opt_num(row_get(r, "close")).ok_or_else(|| {
                CacheError::Runtime("TypeError: float() argument must be a real number".to_owned())
            })?;
            out.push(Candle {
                date: d,
                open: opt_num(row_get(r, "open")),
                high: opt_num(row_get(r, "high")),
                low: opt_num(row_get(r, "low")),
                close: Some(close),
                is_final: clock.session_is_closed(d),
            });
        }
        cursor = chunk_end + Duration::days(1);
    }
    Ok(out)
}

/// `load_instruments(kite, limits, clock=, refresh=False, cache_path=INSTRUMENT_CACHE)`: the
/// instrument dump, cached on disk once per day (`{"fetched": today, "rows": [...]}`).
///
/// # Errors
/// The dump read, or writing the cache.
pub async fn load_instruments(
    kite: &dyn IndexKite,
    limits: &dyn GatewayLimits,
    clock: SessionClock,
    refresh: bool,
    cache_path: &Path,
) -> Result<Vec<PyDict>, CacheError> {
    let today = iso(clock.today());
    if !refresh
        && let Ok(raw) = std::fs::read(cache_path)
        && let Ok(PyValue::Map(blob)) = PyValue::from_json(&raw)
        && blob.get("fetched") == Some(&PyValue::Str(today.clone()))
        && let Some(PyValue::List(rows)) = blob.get("rows")
    {
        return Ok(rows
            .iter()
            .filter_map(|r| match r {
                PyValue::Map(m) => Some(m.clone()),
                _ => None,
            })
            .collect());
    }
    limits.api_slot().await;
    let dump = kite.instruments().await.map_err(CacheError::Runtime)?;
    let field = |r: &PyValue, k: &str| row_get(r, k).cloned();
    let mut rows = Vec::with_capacity(dump.len());
    for r in &dump {
        let need = |k: &str| {
            field(r, k).ok_or_else(|| CacheError::Runtime(format!("KeyError: '{k}'")))
        };
        rows.push(dict([
            ("tradingsymbol", need("tradingsymbol")?),
            ("instrument_token", need("instrument_token")?),
            (
                "segment",
                field(r, "segment").unwrap_or_else(|| PyValue::Str(String::new())),
            ),
            (
                "exchange",
                field(r, "exchange").unwrap_or_else(|| PyValue::Str(String::new())),
            ),
            (
                "name",
                field(r, "name").unwrap_or_else(|| PyValue::Str(String::new())),
            ),
        ]));
    }
    if let Some(dir) = cache_path.parent()
        && !dir.as_os_str().is_empty()
    {
        std::fs::create_dir_all(dir).map_err(|e| CacheError::Runtime(e.to_string()))?;
    }
    let blob = PyValue::Map(dict([
        ("fetched", PyValue::Str(today)),
        (
            "rows",
            PyValue::List(rows.iter().cloned().map(PyValue::Map).collect()),
        ),
    ]));
    let text = crate::wire::py_json_dumps(&blob).map_err(|e| CacheError::Runtime(e.to_string()))?;
    std::fs::write(cache_path, text).map_err(|e| CacheError::Runtime(e.to_string()))?;
    Ok(rows)
}

/// `update_index_history(...)`'s knobs.
#[derive(Clone, Copy)]
pub struct UpdateOptions<'a> {
    /// `start`.
    pub start: Option<NaiveDate>,
    /// `end`.
    pub end: Option<NaiveDate>,
    /// `clock`.
    pub clock: SessionClock,
    /// `limits`.
    pub limits: &'a dyn GatewayLimits,
    /// `full_refresh`.
    pub full_refresh: bool,
    /// `_with_retry`'s `attempts`.
    pub attempts: u32,
    /// `_with_retry`'s `base_delay` (seconds).
    pub base_delay: f64,
    /// `datetime.now(IST).isoformat(timespec="seconds")` for `updated_at`.
    pub now: &'a str,
}

/// `update_index_history(kite, conn, index_name, cfg, ...)`: bring one index up to date,
/// fetching **only** what the cache lacks (from the last cached session, which may have been
/// provisional). Stored under the configured name, not the broker's.
///
/// # Errors
/// Resolution, the fetch, the store.
pub async fn update_index_history(
    kite: &dyn IndexKite,
    conn: &mut Conn,
    index_name: &str,
    cfg: &RegimeConfig,
    instruments: &[PyDict],
    opts: UpdateOptions<'_>,
) -> Result<PyDict, CacheError> {
    let inst = resolve_index_instrument(instruments, index_name, cfg)?;
    let token_v = inst
        .get("instrument_token")
        .cloned()
        .unwrap_or(PyValue::None);
    let token = match &token_v {
        PyValue::Int(i) => *i,
        other => {
            return Err(CacheError::Runtime(format!(
                "instrument_token is not an int: {}",
                other.py_repr()
            )));
        }
    };
    let to = opts.end.unwrap_or_else(|| opts.clock.today());
    let frm = if opts.full_refresh || opts.start.is_some() {
        opts.start.unwrap_or_else(|| NaiveDate::from_ymd_opt(2005, 1, 1).unwrap_or_default())
    } else {
        match last_date(conn, index_name, true).await? {
            Some(d) => d,
            None => NaiveDate::from_ymd_opt(2005, 1, 1).unwrap_or_default(),
        }
    };
    if frm > to {
        return Ok(dict([
            ("index_name", PyValue::Str(index_name.to_owned())),
            ("written", PyValue::Int(0)),
            ("skipped", PyValue::Str("already current".to_owned())),
            ("instrument_token", token_v),
        ]));
    }
    let candles = fetch_candles(
        kite,
        token,
        frm,
        to,
        opts.limits,
        opts.clock,
        opts.attempts,
        opts.base_delay,
    )
    .await?;
    let mut res = upsert(conn, index_name, &candles, Some(token), opts.now).await?;
    res.insert("instrument_token", token_v);
    res.insert(
        "resolved_symbol",
        inst.get("tradingsymbol").cloned().unwrap_or(PyValue::None),
    );
    res.insert("fetched_from", PyValue::Str(iso(frm)));
    Ok(res)
}

/// `required_index_names(cfg)`: every structural index plus the momentum sentinel.
#[must_use]
pub fn required_index_names(cfg: &RegimeConfig) -> Vec<String> {
    cfg.structural_indices
        .iter()
        .map(|(_, v)| v.clone())
        .chain(std::iter::once(cfg.momentum_sentinel.clone()))
        .collect()
}

/// `update_all(kite, conn, cfg, ...)`: update every structural index and the sentinel, over one
/// instrument dump (cached at `cache_path`).
///
/// # Errors
/// As [`update_index_history`], or the dump.
pub async fn update_all(
    kite: &dyn IndexKite,
    conn: &mut Conn,
    cfg: &RegimeConfig,
    cache_path: &Path,
    opts: UpdateOptions<'_>,
) -> Result<Vec<PyDict>, CacheError> {
    let dump = load_instruments(kite, opts.limits, opts.clock, false, cache_path).await?;
    let mut out = Vec::new();
    for name in required_index_names(cfg) {
        out.push(update_index_history(kite, conn, &name, cfg, &dump, opts).await?);
    }
    Ok(out)
}

/// `build_all_signals(conn, cfg, as_of=, end=None)`: cache → `IndexSignals` for every required
/// index, through the pure engine.
///
/// # Errors
/// The driver's; a stored close the engine cannot use.
pub async fn build_all_signals(
    conn: &mut Conn,
    cfg: &RegimeConfig,
    as_of: NaiveDate,
    end: Option<NaiveDate>,
) -> Result<Vec<(String, IndexSignals)>, CacheError> {
    let mut out = Vec::new();
    for name in required_index_names(cfg) {
        let candles = load(conn, &name, None, Some(end.unwrap_or(as_of)), true).await?;
        let sig = build_index_signals(&name, &candles, cfg, Some(as_of), None)
            .map_err(|e| CacheError::Runtime(e.to_string()))?;
        if let Some(slot) = out.iter_mut().find(|(k, _): &&mut (String, IndexSignals)| *k == name) {
            slot.1 = sig;
        } else {
            out.push((name, sig));
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wire::{PyDict, dict};

    fn inst(sym: &str, token: i64) -> PyDict {
        dict([
            ("tradingsymbol", PyValue::Str(sym.to_owned())),
            ("segment", PyValue::Str("INDICES".to_owned())),
            ("exchange", PyValue::Str("NSE".to_owned())),
            ("instrument_token", PyValue::Int(token)),
        ])
    }

    #[test]
    fn resolves_exactly_one_through_an_alias() {
        let cfg = RegimeConfig::default();
        let rows = vec![
            inst("NIFTY 50", 1),
            inst("NIFTY SMALLCAP 250", 2),
            inst("NIFTY BANK", 3),
        ];
        let hit = resolve_index_instrument(&rows, "NIFTY SMLCAP 250", &cfg);
        assert_eq!(
            hit.ok().and_then(|h| h.get("instrument_token").cloned()),
            Some(PyValue::Int(2))
        );
    }

    #[test]
    fn refuses_to_guess() {
        let cfg = RegimeConfig::default();
        let bank = [inst("NIFTY BANK", 3)];
        let none = resolve_index_instrument(&bank, "NIFTY 50", &cfg);
        assert!(matches!(none, Err(ResolveError::NotFound(_))));
        let two = vec![inst("NIFTY 50", 1), inst("nifty 50 ", 9)];
        assert!(matches!(
            resolve_index_instrument(&two, "NIFTY 50", &cfg),
            Err(ResolveError::Ambiguous(_))
        ));
    }

    #[test]
    fn a_session_is_final_only_after_the_close() {
        let d = |s: &str| NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap_or_default();
        let at = |s: &str| NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M").unwrap_or_default();
        assert!(session_is_closed(at("2026-10-30 09:00"), d("2026-10-29")));
        assert!(!session_is_closed(at("2026-10-30 15:29"), d("2026-10-30")));
        assert!(session_is_closed(at("2026-10-30 15:30"), d("2026-10-30")));
        assert!(!session_is_closed(at("2026-10-30 18:00"), d("2026-10-31")));
    }

    #[tokio::test]
    async fn load_returns_only_final_sessions_in_date_order() {
        let Ok(dir) = tempfile::tempdir() else { return };
        let Ok(mut conn) = crate::storage::Db::connect_sqlite_path(&dir.path().join("t.db")).await
        else {
            return;
        };
        assert!(conn.migrate().await.is_ok());
        for (date, close, fin) in [
            ("2026-10-29", 101.5, 1),
            ("2026-10-27", 100.0, 1),
            ("2026-10-30", 102.0, 0),
        ] {
            let ok = conn
                .execute(
                    "INSERT INTO index_series(index_name, date, open, high, low, close, instrument_token, is_final, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    &["NIFTY 50".into(), date.into(), Param::from(None::<f64>), Param::from(None::<f64>), Param::from(None::<f64>), close.into(), Param::from(None::<i64>), Param::from(fin), "x".into()],
                )
                .await;
            assert!(ok.is_ok());
        }
        let got = load(&mut conn, "NIFTY 50", None, None, true)
            .await
            .unwrap_or_default();
        assert_eq!(
            got.iter().map(|c| c.close).collect::<Vec<_>>(),
            vec![Some(100.0), Some(101.5)]
        );
        let all = load(&mut conn, "NIFTY 50", None, None, false)
            .await
            .unwrap_or_default();
        assert_eq!(all.len(), 3);
        assert_eq!(
            last_date(&mut conn, "NIFTY 50", true).await.ok().flatten(),
            NaiveDate::from_ymd_opt(2026, 10, 29)
        );
    }
}
