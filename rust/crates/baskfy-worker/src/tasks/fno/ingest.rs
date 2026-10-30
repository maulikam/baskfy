//! Ports `baskfy_worker.fno.ingest` and `baskfy_worker.fno.partitions`: one session's F&O
//! bhavcopy into `fo_contract_daily` (`docs/fno/03` §1, retention; house rules 7 and 8), every
//! attempt into `fo_ingest_day` (`PENDING` / `INGESTED` / `MISSING`, never interpolated), and the
//! ban list NSE published for the next session onto tonight's rows (`03` §2 `in_ban`).
//!
//! Reads NSE only (through the provider, archive first); moves no money.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_db::OrderedJson;
use baskfy_providers::archive::archive_key;
use baskfy_providers::errors::ProviderError;
use baskfy_providers::nse::{KIND_FO_BAN_LIST, KIND_FO_BHAVCOPY, NseProvider};
use chrono::{Datelike, NaiveDate, NaiveDateTime, NaiveTime};
use polars::prelude::DataFrame;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{
    cell_date, cell_decimal, cell_int, cell_string, column, iso, opt_int, quantize_half_up,
};
use crate::error::WorkerError;
use crate::pyjson::{object, opt_s, s, strings};
use crate::steps::log_warning;

/// `03` §1: the two underlyings whose weeklies are kept as well as their monthlies.
pub(crate) const WEEKLY_UNDERLYINGS: [&str; 2] = ["BANKNIFTY", "NIFTY"];
/// `03` §1: the two nearest monthly expiries of each underlying.
pub(crate) const NEAREST_MONTHLIES: usize = 2;
/// Rows per INSERT statement.
pub(crate) const BATCH: usize = 1_000;
/// `FO_FUTURE_INSTRUMENTS`.
pub(crate) const FUTURE_INSTRUMENTS: [&str; 2] = ["FUTSTK", "FUTIDX"];
/// `FUTURE_TYPE`: a future's option type in the key.
pub(crate) const FUTURE_TYPE: &str = "XX";

/// `STATUS_PENDING`.
pub(crate) const STATUS_PENDING: &str = "PENDING";
/// `STATUS_INGESTED`.
pub(crate) const STATUS_INGESTED: &str = "INGESTED";
/// `STATUS_MISSING`.
pub(crate) const STATUS_MISSING: &str = "MISSING";

/// `FoBhavcopyReader`: the two NSE reads the ingest needs (`NseProvider` satisfies it).
#[allow(async_fn_in_trait)]
pub(crate) trait FoBhavcopyReader {
    /// One session's F&O bhavcopy (`FO_BHAVCOPY_SCHEMA`).
    async fn fo_bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError>;
    /// The ban list for `for_session`.
    async fn fo_ban_list(&self, for_session: NaiveDate) -> Result<Vec<String>, ProviderError>;
}

impl FoBhavcopyReader for NseProvider {
    async fn fo_bhavcopy(&self, on: NaiveDate) -> Result<DataFrame, ProviderError> {
        Self::fo_bhavcopy(self, on).await
    }

    async fn fo_ban_list(&self, for_session: NaiveDate) -> Result<Vec<String>, ProviderError> {
        Self::fo_ban_list(self, for_session).await
    }
}

/// `IngestReport`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct IngestReport {
    pub(crate) trade_date: NaiveDate,
    pub(crate) status: &'static str,
    pub(crate) rows_in_file: Option<i64>,
    pub(crate) rows_kept: Option<i64>,
    pub(crate) source_key: Option<String>,
    pub(crate) error: Option<String>,
    pub(crate) skipped: Option<String>,
}

impl IngestReport {
    fn new(trade_date: NaiveDate, status: &'static str) -> Self {
        Self {
            trade_date,
            status,
            rows_in_file: None,
            rows_kept: None,
            source_key: None,
            error: None,
            skipped: None,
        }
    }

    /// `as_dict()`.
    pub(crate) fn as_dict(&self) -> OrderedJson {
        object([
            ("trade_date", s(iso(self.trade_date))),
            ("status", s(self.status)),
            ("rows_in_file", opt_int(self.rows_in_file)),
            ("rows_kept", opt_int(self.rows_kept)),
            ("source_key", opt_s(self.source_key.clone())),
            ("error", opt_s(self.error.clone())),
            ("skipped", opt_s(self.skipped.clone())),
        ])
    }
}

/// `BanReport`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct BanReport {
    pub(crate) for_session: NaiveDate,
    pub(crate) symbols: Option<Vec<String>>,
    pub(crate) source_key: Option<String>,
    pub(crate) error: Option<String>,
    pub(crate) skipped: Option<String>,
}

impl BanReport {
    /// `as_dict()`.
    pub(crate) fn as_dict(&self) -> OrderedJson {
        object([
            ("for_session", s(iso(self.for_session))),
            (
                "symbols",
                self.symbols
                    .as_ref()
                    .map_or(OrderedJson::Null, |v| strings(v.iter().cloned())),
            ),
            ("source_key", opt_s(self.source_key.clone())),
            ("error", opt_s(self.error.clone())),
            ("skipped", opt_s(self.skipped.clone())),
        ])
    }
}

/// `f"{type(exc).__name__}: {exc}"`.
pub(crate) fn describe(e: &ProviderError) -> String {
    format!("{}: {e}", e.python_type())
}

// ---------------------------------------------------------------------------------------------
// Pure: retention and the row shape
// ---------------------------------------------------------------------------------------------

/// One row of `FO_BHAVCOPY_SCHEMA`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct FoRow {
    pub(crate) date: Option<NaiveDate>,
    pub(crate) instrument: Option<String>,
    pub(crate) symbol: Option<String>,
    pub(crate) expiry: Option<NaiveDate>,
    pub(crate) strike: Option<Decimal>,
    pub(crate) option_type: Option<String>,
    pub(crate) open: Option<Decimal>,
    pub(crate) high: Option<Decimal>,
    pub(crate) low: Option<Decimal>,
    pub(crate) close: Option<Decimal>,
    pub(crate) settle: Option<Decimal>,
    pub(crate) underlying: Option<Decimal>,
    pub(crate) open_interest: Option<i64>,
    pub(crate) oi_change: Option<i64>,
    pub(crate) volume: Option<i64>,
    pub(crate) turnover: Option<Decimal>,
    pub(crate) lot_size: Option<i64>,
}

/// `frame.iter_rows(named=True)` over `FO_BHAVCOPY_SCHEMA`.
pub(crate) fn read_rows(frame: &DataFrame) -> Result<Vec<FoRow>, WorkerError> {
    let c = |name: &str| column(frame, name);
    let (date, instrument, symbol, expiry, strike, option_type) = (
        c("date")?,
        c("instrument")?,
        c("symbol")?,
        c("expiry")?,
        c("strike")?,
        c("option_type")?,
    );
    let (open, high, low, close, settle, underlying) = (
        c("open")?,
        c("high")?,
        c("low")?,
        c("close")?,
        c("settle")?,
        c("underlying")?,
    );
    let (oi, oi_change, volume, turnover, lot) = (
        c("open_interest")?,
        c("oi_change")?,
        c("volume")?,
        c("turnover")?,
        c("lot_size")?,
    );
    (0..frame.height())
        .map(|i| {
            Ok(FoRow {
                date: cell_date(date, i)?,
                instrument: cell_string(instrument, i)?,
                symbol: cell_string(symbol, i)?,
                expiry: cell_date(expiry, i)?,
                strike: cell_decimal(strike, i)?,
                option_type: cell_string(option_type, i)?,
                open: cell_decimal(open, i)?,
                high: cell_decimal(high, i)?,
                low: cell_decimal(low, i)?,
                close: cell_decimal(close, i)?,
                settle: cell_decimal(settle, i)?,
                underlying: cell_decimal(underlying, i)?,
                open_interest: cell_int(oi, i)?,
                oi_change: cell_int(oi_change, i)?,
                volume: cell_int(volume, i)?,
                turnover: cell_decimal(turnover, i)?,
                lot_size: cell_int(lot, i)?,
            })
        })
        .collect()
}

fn is_future(row: &FoRow) -> bool {
    row.instrument
        .as_deref()
        .is_some_and(|i| FUTURE_INSTRUMENTS.contains(&i))
}

/// `monthly_expiries(frame)`: `(symbol, expiry)` for every monthly in the file — the last listed
/// expiry of its calendar month for that underlying.
pub(crate) fn monthly_pairs(rows: &[FoRow]) -> BTreeSet<(String, NaiveDate)> {
    let mut last: BTreeMap<(String, i32), NaiveDate> = BTreeMap::new();
    for r in rows {
        let (Some(symbol), Some(expiry)) = (&r.symbol, r.expiry) else {
            continue;
        };
        let month = expiry.year() * 100 + i32::try_from(expiry.month()).unwrap_or(0);
        let slot = last.entry((symbol.clone(), month)).or_insert(expiry);
        if expiry > *slot {
            *slot = expiry;
        }
    }
    last.into_iter().map(|((s, _), e)| (s, e)).collect()
}

/// `retain(frame)`: `03` §1's retention over one session's rows, sorted by
/// `symbol, expiry, instrument, strike, option_type` (nulls last).
pub(crate) fn retain(rows: &[FoRow]) -> Vec<FoRow> {
    if rows.is_empty() {
        return Vec::new();
    }
    let monthly = monthly_pairs(rows);
    let dates: BTreeMap<&str, BTreeSet<NaiveDate>> =
        rows.iter().fold(BTreeMap::new(), |mut acc, r| {
            if let (Some(sym), Some(d)) = (r.symbol.as_deref(), r.date) {
                acc.entry(sym).or_insert_with(BTreeSet::new).insert(d);
            }
            acc
        });
    // `nearest`: per symbol, the first two monthly expiries on or after the file's date(s).
    let mut nearest: BTreeSet<(String, NaiveDate)> = BTreeSet::new();
    let mut by_symbol: BTreeMap<&str, Vec<(NaiveDate, NaiveDate)>> = BTreeMap::new();
    for (symbol, expiry) in &monthly {
        for d in dates.get(symbol.as_str()).into_iter().flatten() {
            if expiry >= d {
                by_symbol
                    .entry(symbol.as_str())
                    .or_default()
                    .push((*expiry, *d));
            }
        }
    }
    for (symbol, mut pairs) in by_symbol {
        pairs.sort();
        for (expiry, _) in pairs.into_iter().take(NEAREST_MONTHLIES) {
            nearest.insert((symbol.to_owned(), expiry));
        }
    }
    let mut kept: Vec<FoRow> = rows.iter().filter(|r| is_future(r)).cloned().collect();
    for r in rows {
        let Some(instrument) = r.instrument.as_deref() else {
            continue;
        };
        if FUTURE_INSTRUMENTS.contains(&instrument) {
            continue;
        }
        let traded = r.open_interest.unwrap_or(0) > 0 || r.volume.unwrap_or(0) > 0;
        if !traded {
            continue;
        }
        let key = match (&r.symbol, r.expiry) {
            (Some(s), Some(e)) => Some((s.clone(), e)),
            _ => None,
        };
        let near = key.as_ref().is_some_and(|k| nearest.contains(k));
        let is_monthly = key.as_ref().is_some_and(|k| monthly.contains(k));
        let weekly = r
            .symbol
            .as_deref()
            .is_some_and(|s| WEEKLY_UNDERLYINGS.contains(&s))
            && !is_monthly;
        if near || weekly {
            kept.push(r.clone());
        }
    }
    kept.sort_by(|a, b| {
        nulls_last(&a.symbol, &b.symbol)
            .then_with(|| nulls_last(&a.expiry, &b.expiry))
            .then_with(|| nulls_last(&a.instrument, &b.instrument))
            .then_with(|| nulls_last(&a.strike, &b.strike))
            .then_with(|| nulls_last(&a.option_type, &b.option_type))
    });
    kept
}

fn nulls_last<T: Ord>(a: &Option<T>, b: &Option<T>) -> std::cmp::Ordering {
    match (a, b) {
        (Some(x), Some(y)) => x.cmp(y),
        (Some(_), None) => std::cmp::Ordering::Less,
        (None, Some(_)) => std::cmp::Ordering::Greater,
        (None, None) => std::cmp::Ordering::Equal,
    }
}

/// One `fo_contract_daily` row, rounded at write time (house rule 8).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ContractRow {
    pub(crate) trade_date: Option<NaiveDate>,
    pub(crate) instrument: Option<String>,
    pub(crate) symbol: Option<String>,
    pub(crate) expiry: Option<NaiveDate>,
    pub(crate) strike: Decimal,
    pub(crate) option_type: String,
    pub(crate) prices: [Option<Decimal>; 6],
    pub(crate) open_interest: Option<i64>,
    pub(crate) oi_change: Option<i64>,
    pub(crate) volume: Option<i64>,
    pub(crate) turnover: Option<Decimal>,
    pub(crate) lot_size: Option<i64>,
}

fn price(v: Option<Decimal>) -> Option<Decimal> {
    v.map(|d| quantize_half_up(d, 2))
}

/// `contract_rows(frame, source_key)`: a future's missing strike becomes 0 and its option type
/// `XX`, so the key has no null.
pub(crate) fn contract_rows(rows: &[FoRow]) -> Vec<ContractRow> {
    rows.iter()
        .map(|r| {
            let strike = r.strike.filter(|k| !k.is_zero()).unwrap_or(Decimal::ZERO);
            let option_type = if is_future(r) {
                FUTURE_TYPE.to_owned()
            } else {
                r.option_type
                    .clone()
                    .unwrap_or_else(|| FUTURE_TYPE.to_owned())
            };
            ContractRow {
                trade_date: r.date,
                instrument: r.instrument.clone(),
                symbol: r.symbol.clone(),
                expiry: r.expiry,
                strike: quantize_half_up(strike, 2),
                option_type,
                prices: [
                    price(r.open),
                    price(r.high),
                    price(r.low),
                    price(r.close),
                    price(r.settle),
                    price(r.underlying),
                ],
                open_interest: r.open_interest,
                oi_change: r.oi_change,
                volume: r.volume,
                turnover: price(r.turnover),
                lot_size: r.lot_size,
            }
        })
        .collect()
}

/// `is_final_attempt`: whether 23:30 IST on `trade_date` has passed (the wall clock compared
/// as written, its zone dropped). A past day's attempt is always final.
pub(crate) fn is_final_attempt(trade_date: NaiveDate, now_wall: NaiveDateTime) -> bool {
    let last = NaiveTime::from_hms_opt(23, 30, 0).unwrap_or(NaiveTime::MIN);
    now_wall >= trade_date.and_time(last)
}

// ---------------------------------------------------------------------------------------------
// Partitions
// ---------------------------------------------------------------------------------------------

/// `options.partitions.month_bounds`.
pub(crate) fn month_bounds(day: NaiveDate) -> (NaiveDate, NaiveDate) {
    let start = day.with_day(1).unwrap_or(day);
    let end = if start.month() == 12 {
        NaiveDate::from_ymd_opt(start.year() + 1, 1, 1)
    } else {
        NaiveDate::from_ymd_opt(start.year(), start.month() + 1, 1)
    }
    .unwrap_or(start);
    (start, end)
}

/// `partitions.partition_name`.
pub(crate) fn partition_name(day: NaiveDate) -> String {
    let (start, _) = month_bounds(day);
    format!("fo_contract_daily_{:04}{:02}", start.year(), start.month())
}

/// `partitions.ensure_contract_partition`: the month's partition, created if absent. The name
/// and bounds come from a date, never from input text.
pub(crate) async fn ensure_contract_partition(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<String, WorkerError> {
    let (start, end) = month_bounds(day);
    let name = partition_name(day);
    sqlx::query(&format!(
        "CREATE TABLE IF NOT EXISTS {name} PARTITION OF fo_contract_daily \
         FOR VALUES FROM ('{}') TO ('{}')",
        iso(start),
        iso(end)
    ))
    .execute(&mut *conn)
    .await?;
    Ok(name)
}

// ---------------------------------------------------------------------------------------------
// Database
// ---------------------------------------------------------------------------------------------

/// `ingested`.
pub(crate) async fn ingested(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<bool, WorkerError> {
    let status: Option<(Option<String>,)> =
        sqlx::query_as("SELECT status FROM fo_ingest_day WHERE trade_date = $1")
            .bind(trade_date)
            .fetch_optional(&mut *conn)
            .await?;
    Ok(status.and_then(|(s,)| s).as_deref() == Some(STATUS_INGESTED))
}

/// `ingested_days`.
pub(crate) async fn ingested_days(
    conn: &mut PgConnection,
    days: &[NaiveDate],
) -> Result<BTreeSet<NaiveDate>, WorkerError> {
    if days.is_empty() {
        return Ok(BTreeSet::new());
    }
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trade_date FROM fo_ingest_day WHERE trade_date = ANY($1) AND status = $2",
    )
    .bind(days)
    .bind(STATUS_INGESTED)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(d,)| d).collect())
}

/// `upsert_contracts`: idempotent on `(trade_date, symbol, expiry, strike, option_type)`.
pub(crate) async fn upsert_contracts(
    conn: &mut PgConnection,
    rows: &[ContractRow],
    source_key: &str,
) -> Result<(), WorkerError> {
    for chunk in rows.chunks(BATCH) {
        let col =
            |k: usize| -> Vec<Option<Decimal>> { chunk.iter().map(|r| r.prices[k]).collect() };
        sqlx::query(
            "INSERT INTO fo_contract_daily (trade_date, instrument, symbol, expiry, strike, \
             option_type, open, high, low, close, settle, underlying, open_interest, oi_change, \
             volume, turnover, lot_size, source_key) \
             SELECT t.*, $17::text FROM UNNEST($1::date[], $2::varchar[], $3::varchar[], \
             $4::date[], $5::numeric[], $6::varchar[], $7::numeric[], $8::numeric[], \
             $9::numeric[], $10::numeric[], $11::numeric[], $12::numeric[], $13::int8[], \
             $14::int8[], $15::int8[], $16::numeric[], $18::int4[]) AS t \
             ON CONFLICT (trade_date, symbol, expiry, strike, option_type) DO UPDATE SET \
             instrument = excluded.instrument, open = excluded.open, high = excluded.high, \
             low = excluded.low, close = excluded.close, settle = excluded.settle, \
             underlying = excluded.underlying, open_interest = excluded.open_interest, \
             oi_change = excluded.oi_change, volume = excluded.volume, \
             turnover = excluded.turnover, lot_size = excluded.lot_size, \
             source_key = excluded.source_key",
        )
        .bind(chunk.iter().map(|r| r.trade_date).collect::<Vec<_>>())
        .bind(
            chunk
                .iter()
                .map(|r| r.instrument.clone())
                .collect::<Vec<_>>(),
        )
        .bind(chunk.iter().map(|r| r.symbol.clone()).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.expiry).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.strike).collect::<Vec<_>>())
        .bind(
            chunk
                .iter()
                .map(|r| r.option_type.clone())
                .collect::<Vec<_>>(),
        )
        .bind(col(0))
        .bind(col(1))
        .bind(col(2))
        .bind(col(3))
        .bind(col(4))
        .bind(col(5))
        .bind(chunk.iter().map(|r| r.open_interest).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.oi_change).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.volume).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.turnover).collect::<Vec<_>>())
        .bind(source_key)
        .bind(
            chunk
                .iter()
                .map(|r| r.lot_size.and_then(|l| i32::try_from(l).ok()))
                .collect::<Vec<_>>(),
        )
        .execute(&mut *conn)
        .await?;
    }
    Ok(())
}

/// `_record`: one attempt into `fo_ingest_day` (`attempts` grows by one per attempt).
async fn record(
    conn: &mut PgConnection,
    report: &IngestReport,
    attempted: bool,
) -> Result<(), WorkerError> {
    sqlx::query(
        "INSERT INTO fo_ingest_day (trade_date, status, rows_in_file, rows_kept, source_key, \
         attempts, last_error) VALUES ($1, $2, $3, $4, $5, $6, $7) \
         ON CONFLICT (trade_date) DO UPDATE SET status = excluded.status, \
         rows_in_file = excluded.rows_in_file, rows_kept = excluded.rows_kept, \
         source_key = excluded.source_key, \
         attempts = fo_ingest_day.attempts + excluded.attempts, \
         last_error = excluded.last_error, updated_at = now()",
    )
    .bind(report.trade_date)
    .bind(report.status)
    .bind(report.rows_in_file.and_then(|v| i32::try_from(v).ok()))
    .bind(report.rows_kept.and_then(|v| i32::try_from(v).ok()))
    .bind(report.source_key.as_deref())
    .bind(i16::from(attempted))
    .bind(report.error.as_deref())
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// `ingest_day`: one session's bhavcopy into `fo_contract_daily`, the attempt into
/// `fo_ingest_day`. A day already `INGESTED` is answered without a read; a provider refusal is
/// recorded (`PENDING`, or `MISSING` on the final attempt) and returned, not raised.
pub(crate) async fn ingest_day<R: FoBhavcopyReader>(
    conn: &mut PgConnection,
    reader: &R,
    trade_date: NaiveDate,
    final_attempt: bool,
) -> Result<IngestReport, WorkerError> {
    if ingested(conn, trade_date).await? {
        let mut report = IngestReport::new(trade_date, STATUS_INGESTED);
        report.skipped = Some("already ingested".to_owned());
        return Ok(report);
    }
    let frame = match reader.fo_bhavcopy(trade_date).await {
        Ok(frame) => frame,
        Err(e) if e.is_provider_error() => {
            let status = if final_attempt {
                STATUS_MISSING
            } else {
                STATUS_PENDING
            };
            let mut report = IngestReport::new(trade_date, status);
            report.error = Some(describe(&e));
            log_warning(&format!(
                "fno bhavcopy {}: {status} ({})",
                iso(trade_date),
                describe(&e)
            ));
            record(conn, &report, true).await?;
            return Ok(report);
        }
        Err(e) => return Err(e.into()),
    };
    let rows = read_rows(&frame)?;
    let kept = retain(&rows);
    let key = archive_key(KIND_FO_BHAVCOPY, trade_date, "zip")?;
    ensure_contract_partition(conn, trade_date).await?;
    upsert_contracts(conn, &contract_rows(&kept), &key).await?;
    let mut report = IngestReport::new(trade_date, STATUS_INGESTED);
    report.rows_in_file = i64::try_from(frame.height()).ok();
    report.rows_kept = i64::try_from(kept.len()).ok();
    report.source_key = Some(key);
    record(conn, &report, true).await?;
    Ok(report)
}

/// `store_ban_list`: the ban list for `for_session` onto `trade_date`'s rows. Answered without
/// a read when tonight's row already holds that session's list; a refusal is returned.
pub(crate) async fn store_ban_list<R: FoBhavcopyReader>(
    conn: &mut PgConnection,
    reader: &R,
    trade_date: NaiveDate,
    for_session: NaiveDate,
) -> Result<BanReport, WorkerError> {
    let stored: Option<(Option<NaiveDate>, Option<Vec<String>>)> = sqlx::query_as(
        "SELECT ban_for_session, ban_symbols FROM fo_ingest_day WHERE trade_date = $1",
    )
    .bind(trade_date)
    .fetch_optional(&mut *conn)
    .await?;
    let mut report = BanReport {
        for_session,
        symbols: None,
        source_key: None,
        error: None,
        skipped: None,
    };
    if let Some((Some(session), Some(symbols))) = stored
        && session == for_session
    {
        report.symbols = Some(symbols);
        report.skipped = Some("already stored".to_owned());
        return Ok(report);
    }
    let symbols = match reader.fo_ban_list(for_session).await {
        Ok(symbols) => symbols,
        Err(e) if e.is_provider_error() => {
            report.error = Some(describe(&e));
            return Ok(report);
        }
        Err(e) => return Err(e.into()),
    };
    let key = archive_key(KIND_FO_BAN_LIST, for_session, "csv")?;
    sqlx::query(
        "INSERT INTO fo_ingest_day (trade_date, status, ban_for_session, ban_symbols, \
         ban_source_key) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (trade_date) DO UPDATE SET \
         ban_for_session = excluded.ban_for_session, ban_symbols = excluded.ban_symbols, \
         ban_source_key = excluded.ban_source_key, updated_at = now()",
    )
    .bind(trade_date)
    .bind(STATUS_PENDING)
    .bind(for_session)
    .bind(&symbols)
    .bind(&key)
    .execute(&mut *conn)
    .await?;
    // SQLAlchemy's `update()` applies the model's `onupdate=now()` to `updated_at`.
    if symbols.is_empty() {
        sqlx::query(
            "UPDATE fo_underlying_daily SET in_ban = false, updated_at = now() \
             WHERE trade_date = $1",
        )
        .bind(trade_date)
        .execute(&mut *conn)
        .await?;
    } else {
        sqlx::query(
            "UPDATE fo_underlying_daily SET in_ban = (symbol = ANY($2)), updated_at = now() \
             WHERE trade_date = $1",
        )
        .bind(trade_date)
        .bind(&symbols)
        .execute(&mut *conn)
        .await?;
        sqlx::query(
            "INSERT INTO fo_underlying_daily (trade_date, symbol, in_ban) \
             SELECT $1, s, true FROM UNNEST($2::varchar[]) AS s \
             ON CONFLICT (trade_date, symbol) DO UPDATE SET in_ban = true",
        )
        .bind(trade_date)
        .bind(&symbols)
        .execute(&mut *conn)
        .await?;
    }
    report.symbols = Some(symbols);
    report.source_key = Some(key);
    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    fn row(
        instrument: &str,
        symbol: &str,
        expiry: NaiveDate,
        strike: i64,
        kind: Option<&str>,
        oi: i64,
        vol: i64,
    ) -> FoRow {
        FoRow {
            date: Some(d(2026, 10, 28)),
            instrument: Some(instrument.to_owned()),
            symbol: Some(symbol.to_owned()),
            expiry: Some(expiry),
            strike: (strike != 0).then(|| Decimal::from(strike)),
            option_type: kind.map(str::to_owned),
            open: Some("1.005".parse().unwrap()),
            high: None,
            low: None,
            close: None,
            settle: Some(Decimal::from(5)),
            underlying: None,
            open_interest: Some(oi),
            oi_change: None,
            volume: Some(vol),
            turnover: None,
            lot_size: Some(75),
        }
    }

    /// `test_fno_ingest.py`: every future, the two nearest monthlies with OI or volume, NIFTY's
    /// weeklies; a third monthly and an untraded strike are dropped.
    #[test]
    fn retention_keeps_futures_two_monthlies_and_index_weeklies() {
        let rows = vec![
            row("FUTSTK", "ACC", d(2026, 12, 29), 0, None, 0, 0),
            row("OPTSTK", "ACC", d(2026, 10, 28), 100, Some("CE"), 5, 0),
            row("OPTSTK", "ACC", d(2026, 11, 25), 100, Some("CE"), 0, 3),
            row("OPTSTK", "ACC", d(2026, 12, 29), 100, Some("CE"), 5, 5),
            row("OPTSTK", "ACC", d(2026, 11, 25), 110, Some("PE"), 0, 0),
            row("OPTIDX", "NIFTY", d(2026, 11, 4), 25000, Some("CE"), 9, 9),
            row("OPTIDX", "NIFTY", d(2026, 11, 25), 25000, Some("CE"), 9, 9),
            row("OPTIDX", "NIFTY", d(2026, 12, 29), 25000, Some("CE"), 9, 9),
            row("OPTIDX", "NIFTY", d(2027, 1, 27), 25000, Some("CE"), 9, 9),
        ];
        let kept = retain(&rows);
        let keys: Vec<(String, NaiveDate)> = kept
            .iter()
            .map(|r| (r.symbol.clone().unwrap(), r.expiry.unwrap()))
            .collect();
        assert_eq!(
            keys,
            vec![
                ("ACC".to_owned(), d(2026, 10, 28)),
                ("ACC".to_owned(), d(2026, 11, 25)),
                ("ACC".to_owned(), d(2026, 12, 29)),
                ("NIFTY".to_owned(), d(2026, 11, 4)),
                ("NIFTY".to_owned(), d(2026, 11, 25)),
                ("NIFTY".to_owned(), d(2026, 12, 29)),
            ]
        );
        assert_eq!(kept[2].instrument.as_deref(), Some("FUTSTK"));
    }

    #[test]
    fn a_future_row_has_strike_zero_and_type_xx_and_prices_round_half_up() {
        let rows = vec![row("FUTIDX", "NIFTY", d(2026, 11, 25), 0, None, 1, 1)];
        let out = contract_rows(&rows);
        assert_eq!(out[0].strike.to_string(), "0.00");
        assert_eq!(out[0].option_type, "XX");
        assert_eq!(out[0].prices[0].unwrap().to_string(), "1.01");
        assert_eq!(out[0].prices[4].unwrap().to_string(), "5.00");
    }

    #[test]
    fn the_final_attempt_is_2330_on_the_day_or_any_later_day() {
        let day = d(2026, 10, 29);
        assert!(!is_final_attempt(day, day.and_hms_opt(23, 29, 59).unwrap()));
        assert!(is_final_attempt(day, day.and_hms_opt(23, 30, 0).unwrap()));
        assert!(is_final_attempt(
            day,
            d(2026, 10, 30).and_hms_opt(9, 0, 0).unwrap()
        ));
    }

    #[test]
    fn partitions_are_named_and_bounded_by_month() {
        assert_eq!(partition_name(d(2026, 12, 9)), "fo_contract_daily_202612");
        assert_eq!(
            month_bounds(d(2026, 12, 9)),
            (d(2026, 12, 1), d(2027, 1, 1))
        );
        assert_eq!(month_bounds(d(2026, 2, 28)), (d(2026, 2, 1), d(2026, 3, 1)));
    }
}
