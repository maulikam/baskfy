//! Ports `baskfy_worker.fno.index_daily`: `fo_index_daily`, the two indices' own daily OHLC
//! from Kite's history (`docs/fno/03` §9, F3) — one throttled `historical_data(interval="day")`
//! call per index, stored rounded to the paisa, idempotent on `(underlying, trade_date)`.
//! Reads only; places nothing.

use std::collections::BTreeMap;

use baskfy_core::fno::directional::DailyBar;
use baskfy_db::OrderedJson;
use baskfy_providers::errors::ProviderError;
use baskfy_providers::kite::KiteProvider;
use chrono::NaiveDate;
use polars::prelude::DataFrame;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{cell_date, cell_decimal, column, iso, quantize_half_up};
use super::underlying::minus;
use crate::error::WorkerError;
use crate::pyjson::{object, s};

/// `INDEX_SYMBOL_FOR`, in Python's dict order.
pub(crate) const INDEX_SYMBOL_FOR: [(&str, &str); 2] =
    [("NIFTY", "NIFTY 50"), ("BANKNIFTY", "NIFTY BANK")];
/// `SOURCE_KITE`.
pub(crate) const SOURCE_KITE: &str = "KITE_HIST";
/// The evening re-reads this many calendar days back.
pub(crate) const EXTEND_OVERLAP_DAYS: u64 = 7;
const UPSERT_CHUNK: usize = 1000;

/// `DEFAULT_BACKFILL_START`.
pub(crate) fn default_backfill_start() -> NaiveDate {
    NaiveDate::from_ymd_opt(2024, 1, 1).unwrap_or_default()
}

/// The index's symbol in Kite's `INDICES` segment.
pub(crate) fn index_symbol(underlying: &str) -> Option<&'static str> {
    INDEX_SYMBOL_FOR
        .iter()
        .find(|(u, _)| *u == underlying)
        .map(|(_, s)| *s)
}

/// `DailyBarReader`: `KiteProvider.daily_bars`.
#[allow(async_fn_in_trait)]
pub(crate) trait DailyBarReader {
    /// `historical_data(interval="day")` as a `DAILY_BARS_SCHEMA` frame.
    async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError>;
}

impl DailyBarReader for KiteProvider {
    async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        Self::daily_bars(self, token, start, end).await
    }
}

/// `IndexDailyReport`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct IndexDailyReport {
    pub(crate) calls: i64,
    pub(crate) written: Vec<(String, i64)>,
    pub(crate) latest: Vec<(String, Option<NaiveDate>)>,
    pub(crate) skipped: Vec<String>,
}

impl IndexDailyReport {
    /// `as_dict()` entries.
    pub(crate) fn entries(&self) -> Vec<(String, OrderedJson)> {
        vec![
            ("calls".to_owned(), OrderedJson::Int(self.calls)),
            (
                "written".to_owned(),
                object(
                    self.written
                        .iter()
                        .map(|(k, v)| (k.clone(), OrderedJson::Int(*v))),
                ),
            ),
            (
                "latest".to_owned(),
                object(
                    self.latest
                        .iter()
                        .map(|(k, v)| (k.clone(), v.map_or(OrderedJson::Null, |d| s(iso(d))))),
                ),
            ),
            (
                "skipped".to_owned(),
                OrderedJson::Array(self.skipped.iter().map(|x| s(x.clone())).collect()),
            ),
        ]
    }

    fn add_written(&mut self, underlying: &str, n: i64) {
        match self.written.iter_mut().find(|(k, _)| k == underlying) {
            Some(slot) => slot.1 += n,
            None => self.written.push((underlying.to_owned(), n)),
        }
    }

    fn set_latest(&mut self, underlying: &str, day: Option<NaiveDate>) {
        match self.latest.iter_mut().find(|(k, _)| k == underlying) {
            Some(slot) => slot.1 = day,
            None => self.latest.push((underlying.to_owned(), day)),
        }
    }
}

/// `index_tokens`: each underlying's index as `instrument` knows it, with a Kite token.
pub(crate) async fn index_tokens(
    conn: &mut PgConnection,
) -> Result<BTreeMap<String, i64>, WorkerError> {
    let symbols: Vec<String> = INDEX_SYMBOL_FOR
        .iter()
        .map(|(_, s)| (*s).to_owned())
        .collect();
    let rows: Vec<(String, Option<i64>)> = sqlx::query_as(
        "SELECT symbol, kite_token FROM instrument WHERE symbol = ANY($1) \
         AND instrument_type = 'INDEX' AND kite_token IS NOT NULL",
    )
    .bind(&symbols)
    .fetch_all(&mut *conn)
    .await?;
    let mut by_symbol = BTreeMap::new();
    for (symbol, token) in rows {
        if let Some(token) = token {
            by_symbol.insert(symbol, token);
        }
    }
    Ok(INDEX_SYMBOL_FOR
        .iter()
        .filter_map(|(u, sym)| by_symbol.get(*sym).map(|t| ((*u).to_owned(), *t)))
        .collect())
}

/// `upsert_daily`: the frame's candles for `underlying`, overwriting a stored day. A candle
/// missing a price, or with a high below its low, is left out. Returns rows sent.
pub(crate) async fn upsert_daily(
    conn: &mut PgConnection,
    underlying: &str,
    bars: &DataFrame,
) -> Result<i64, WorkerError> {
    if index_symbol(underlying).is_none() {
        return Err(WorkerError::python(
            "ValueError",
            format!(
                "{} is not an fo_index_daily underlying (03 §9)",
                baskfy_providers::py::repr_str(underlying)
            ),
        ));
    }
    let (date, open, high, low, close) = (
        column(bars, "date")?,
        column(bars, "open")?,
        column(bars, "high")?,
        column(bars, "low")?,
        column(bars, "close")?,
    );
    let mut values: Vec<(NaiveDate, [Decimal; 4])> = Vec::new();
    for i in 0..bars.height() {
        let Some(day) = cell_date(date, i)? else {
            continue;
        };
        let prices = [
            cell_decimal(open, i)?,
            cell_decimal(high, i)?,
            cell_decimal(low, i)?,
            cell_decimal(close, i)?,
        ];
        let [Some(o), Some(h), Some(l), Some(c)] = prices else {
            continue;
        };
        if h < l {
            continue;
        }
        let cent = |d: Decimal| quantize_half_up(d, 2);
        values.push((day, [cent(o), cent(h), cent(l), cent(c)]));
    }
    for chunk in values.chunks(UPSERT_CHUNK) {
        let col = |k: usize| -> Vec<Decimal> { chunk.iter().map(|(_, p)| p[k]).collect() };
        sqlx::query(
            "INSERT INTO fo_index_daily (underlying, trade_date, open, high, low, close, source) \
             SELECT $1, t.*, $7 FROM UNNEST($2::date[], $3::numeric[], $4::numeric[], \
             $5::numeric[], $6::numeric[]) AS t \
             ON CONFLICT (underlying, trade_date) DO UPDATE SET open = excluded.open, \
             high = excluded.high, low = excluded.low, close = excluded.close, \
             source = excluded.source, updated_at = now()",
        )
        .bind(underlying)
        .bind(chunk.iter().map(|(d, _)| *d).collect::<Vec<_>>())
        .bind(col(0))
        .bind(col(1))
        .bind(col(2))
        .bind(col(3))
        .bind(SOURCE_KITE)
        .execute(&mut *conn)
        .await?;
    }
    Ok(i64::try_from(values.len()).unwrap_or(i64::MAX))
}

/// `latest_date`.
pub(crate) async fn latest_date(
    conn: &mut PgConnection,
    underlying: &str,
) -> Result<Option<NaiveDate>, WorkerError> {
    let row: (Option<NaiveDate>,) =
        sqlx::query_as("SELECT max(trade_date) FROM fo_index_daily WHERE underlying = $1")
            .bind(underlying)
            .fetch_one(&mut *conn)
            .await?;
    Ok(row.0)
}

/// `daily_bars`: the last `limit` sessions of `underlying` at or before `end`, oldest first.
pub(crate) async fn daily_bars(
    conn: &mut PgConnection,
    underlying: &str,
    end: NaiveDate,
    limit: i64,
) -> Result<Vec<DailyBar>, WorkerError> {
    let rows: Vec<(NaiveDate, Decimal, Decimal, Decimal, Decimal)> = sqlx::query_as(
        "SELECT trade_date, open, high, low, close FROM fo_index_daily \
         WHERE underlying = $1 AND trade_date <= $2 ORDER BY trade_date DESC LIMIT $3",
    )
    .bind(underlying)
    .bind(end)
    .bind(limit)
    .fetch_all(&mut *conn)
    .await?;
    let mut bars: Vec<DailyBar> = rows
        .into_iter()
        .map(|(date, open, high, low, close)| DailyBar {
            date,
            open,
            high,
            low,
            close,
        })
        .collect();
    bars.reverse();
    Ok(bars)
}

async fn pull<R: DailyBarReader>(
    conn: &mut PgConnection,
    reader: &R,
    underlying: &str,
    token: i64,
    window: (NaiveDate, NaiveDate),
    report: &mut IndexDailyReport,
) -> Result<(), WorkerError> {
    let bars = reader.daily_bars(token, window.0, window.1).await?;
    report.calls += 1;
    let written = upsert_daily(conn, underlying, &bars).await?;
    report.add_written(underlying, written);
    let latest = latest_date(conn, underlying).await?;
    report.set_latest(underlying, latest);
    Ok(())
}

/// `backfill`: every session in `[start, end]` for both indices.
pub(crate) async fn backfill<R: DailyBarReader>(
    conn: &mut PgConnection,
    reader: &R,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<IndexDailyReport, WorkerError> {
    let mut report = IndexDailyReport::default();
    let tokens = index_tokens(conn).await?;
    for (underlying, _) in INDEX_SYMBOL_FOR {
        let Some(token) = tokens.get(underlying).copied() else {
            report.skipped.push(underlying.to_owned());
            continue;
        };
        pull(conn, reader, underlying, token, (start, end), &mut report).await?;
    }
    Ok(report)
}

/// `extend`: from a week before the latest stored day (or the default start) to `today`.
pub(crate) async fn extend<R: DailyBarReader>(
    conn: &mut PgConnection,
    reader: &R,
    today: NaiveDate,
) -> Result<IndexDailyReport, WorkerError> {
    let mut report = IndexDailyReport::default();
    let tokens = index_tokens(conn).await?;
    for (underlying, _) in INDEX_SYMBOL_FOR {
        let Some(token) = tokens.get(underlying).copied() else {
            report.skipped.push(underlying.to_owned());
            continue;
        };
        let latest = latest_date(conn, underlying).await?;
        let start = latest.map_or_else(default_backfill_start, |d| minus(d, EXTEND_OVERLAP_DAYS));
        pull(
            conn,
            reader,
            underlying,
            token,
            (start.min(today), today),
            &mut report,
        )
        .await?;
    }
    Ok(report)
}
