//! `eq_bars` (LV5): one-minute bars for the swing book's liquid universe into `eq_minute_bar`.

use std::collections::BTreeMap;
use std::future::Future;
use std::pin::Pin;
use std::time::Instant;

use baskfy_db::{OrderedJson, WriteDb};
use baskfy_providers::ProviderError;
use baskfy_providers::kite::{KiteProvider, MINUTE_MAX_DAYS_PER_REQUEST, MinuteBound};
use baskfy_providers::records::MinuteBarRecord;
use baskfy_providers::tokens::ist;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveTime, Utc};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Postgres, QueryBuilder};

use super::common::{cent, count, iso};
use crate::error::WorkerError;
use crate::pyjson::{object, s};
use crate::steps::log_warning;
use crate::tasks::swing_live::scan::{liquid_universe, load_swing_config};

const SOURCE_KITE: &str = "KITE_HIST";
const UPSERT_CHUNK: usize = 2000;
const CHECKPOINT_EVERY_NAMES: usize = 25;

/// One liquid name: our id, its symbol and Kite's token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EqName {
    /// `instrument.id`.
    pub instrument_id: i64,
    /// `instrument.symbol`.
    pub symbol: String,
    /// `instrument.kite_token`.
    pub kite_token: i64,
}

/// What one pass did (`BarsReport`).
#[derive(Debug, Clone, Default)]
pub struct BarsReport {
    /// Names asked for.
    pub names: usize,
    /// Kite calls made.
    pub calls: usize,
    /// Rows sent to the upsert.
    pub written: usize,
    /// Name (or name and window) to the error that skipped it.
    pub skipped: Vec<(String, String)>,
    /// Name to the date it resumed from.
    pub resumed_from: Vec<(String, String)>,
    /// Wall time.
    pub seconds: f64,
}

impl BarsReport {
    /// `as_dict()`, as pairs.
    #[must_use]
    pub fn pairs(&self) -> Vec<(String, OrderedJson)> {
        let map = |items: &[(String, String)]| {
            object(items.iter().map(|(k, v)| (k.clone(), s(v.clone()))))
        };
        vec![
            ("names".to_owned(), count(self.names)),
            ("calls".to_owned(), count(self.calls)),
            ("written".to_owned(), count(self.written)),
            ("skipped".to_owned(), map(&self.skipped)),
            ("resumed_from".to_owned(), map(&self.resumed_from)),
            (
                "seconds".to_owned(),
                OrderedJson::Float((self.seconds * 1000.0).round() / 1000.0),
            ),
        ]
    }
}

/// Python's `BarReader`: one-minute bars for a token over `[lo, hi]`.
pub trait MinuteReader: Sync {
    /// `minute_bars(token, start, end)`.
    fn minute_bars(
        &self,
        token: i64,
        lo: MinuteBound,
        hi: MinuteBound,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<MinuteBarRecord>, ProviderError>> + Send + '_>>;
}

impl MinuteReader for KiteProvider {
    fn minute_bars(
        &self,
        token: i64,
        lo: MinuteBound,
        hi: MinuteBound,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<MinuteBarRecord>, ProviderError>> + Send + '_>>
    {
        Box::pin(Self::minute_bars(self, token, lo, hi))
    }
}

fn ist_at(day: NaiveDate, at: NaiveTime) -> Result<DateTime<FixedOffset>, WorkerError> {
    day.and_time(at)
        .and_local_timezone(ist())
        .single()
        .ok_or_else(|| WorkerError::python("ValueError", "no such IST moment"))
}

fn session_bounds(
    day: NaiveDate,
) -> Result<(DateTime<FixedOffset>, DateTime<FixedOffset>), WorkerError> {
    let at = |h, m| NaiveTime::from_hms_opt(h, m, 0).unwrap_or(NaiveTime::MIN);
    Ok((ist_at(day, at(9, 15))?, ist_at(day, at(15, 30))?))
}

/// `closed_bars(bars, now)`: only the minutes finished by `now`.
#[must_use]
pub fn closed_bars(bars: Vec<MinuteBarRecord>, now: DateTime<FixedOffset>) -> Vec<MinuteBarRecord> {
    bars.into_iter()
        .filter(|b| {
            b.ts.aware()
                .is_some_and(|ts| ts + Duration::minutes(1) <= now)
        })
        .collect()
}

fn inverted(start: NaiveDate, end: NaiveDate) -> WorkerError {
    WorkerError::python(
        "ValueError",
        format!("end {} precedes start {}", iso(end), iso(start)),
    )
}

/// `day_windows(start, end)`: Kite's 60-day minute windows, inclusive both ends.
///
/// # Errors
/// `ValueError` when `end` precedes `start`.
pub fn day_windows(
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<(NaiveDate, NaiveDate)>, WorkerError> {
    if end < start {
        return Err(inverted(start, end));
    }
    let span = Duration::days(MINUTE_MAX_DAYS_PER_REQUEST - 1);
    let mut out = Vec::new();
    let mut cursor = start;
    while cursor <= end {
        let chunk_end = (cursor + span).min(end);
        out.push((cursor, chunk_end));
        cursor = chunk_end + Duration::days(1);
    }
    Ok(out)
}

/// `liquid_names(session, as_of=, user_id=)`: the swing book's liquid universe with Kite tokens.
///
/// # Errors
/// The database's and the indicator computation's.
pub async fn liquid_names(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    user_id: i64,
) -> Result<Vec<EqName>, WorkerError> {
    let config = load_swing_config(conn, user_id).await?;
    let universe = liquid_universe(conn, as_of, &config).await?;
    if universe.is_empty() {
        return Ok(Vec::new());
    }
    let ids: Vec<i64> = universe.iter().map(|n| n.instrument_id).collect();
    let rows: Vec<(i64, Option<i64>)> =
        sqlx::query_as("SELECT id, kite_token FROM instrument WHERE id = ANY($1)")
            .bind(&ids)
            .fetch_all(&mut *conn)
            .await?;
    let tokens: BTreeMap<i64, Option<i64>> = rows.into_iter().collect();
    Ok(universe
        .into_iter()
        .filter_map(|name| {
            tokens
                .get(&name.instrument_id)
                .copied()
                .flatten()
                .map(|kite_token| EqName {
                    instrument_id: name.instrument_id,
                    symbol: name.symbol,
                    kite_token,
                })
        })
        .collect())
}

/// `upsert_bars(session, instrument_id, bars)`: the rows sent; high below low is dropped.
///
/// # Errors
/// The database's.
pub async fn upsert_bars(
    conn: &mut PgConnection,
    instrument_id: i64,
    bars: Vec<MinuteBarRecord>,
) -> Result<usize, WorkerError> {
    let total = bars.len();
    let sane: Vec<MinuteBarRecord> = bars.into_iter().filter(|b| b.high >= b.low).collect();
    if sane.len() != total {
        log_warning(&format!(
            "dropped {} minute bars with high < low",
            total - sane.len()
        ));
    }
    let mut sent = 0;
    for batch in sane.chunks(UPSERT_CHUNK) {
        let rows: Vec<(DateTime<FixedOffset>, [Decimal; 4], i64)> = batch
            .iter()
            .filter_map(|b| {
                b.ts.aware().map(|ts| {
                    (
                        ts.with_timezone(&ist()),
                        [b.open, b.high, b.low, b.close].map(cent),
                        b.volume,
                    )
                })
            })
            .collect();
        if rows.is_empty() {
            continue;
        }
        let mut q: QueryBuilder<'_, Postgres> = QueryBuilder::new(
            "INSERT INTO eq_minute_bar (instrument_id, ts, open, high, low, close, volume, source) ",
        );
        q.push_values(rows.iter(), |mut b, (ts, [o, h, l, c], v)| {
            b.push_bind(instrument_id)
                .push_bind(*ts)
                .push_bind(*o)
                .push_bind(*h)
                .push_bind(*l)
                .push_bind(*c)
                .push_bind(*v)
                .push_bind(SOURCE_KITE);
        });
        q.push(
            " ON CONFLICT (instrument_id, ts) DO UPDATE SET open = excluded.open, \
             high = excluded.high, low = excluded.low, close = excluded.close, \
             volume = excluded.volume, source = excluded.source",
        );
        q.build().execute(&mut *conn).await?;
        sent += rows.len();
    }
    Ok(sent)
}

async fn latest_ts(
    conn: &mut PgConnection,
    instrument_id: i64,
    lo: DateTime<FixedOffset>,
    hi: DateTime<FixedOffset>,
) -> Result<Option<DateTime<FixedOffset>>, WorkerError> {
    let value: Option<DateTime<Utc>> = sqlx::query_scalar(
        "SELECT max(ts) FROM eq_minute_bar WHERE instrument_id = $1 AND ts >= $2 AND ts <= $3",
    )
    .bind(instrument_id)
    .bind(lo)
    .bind(hi)
    .fetch_one(&mut *conn)
    .await?;
    Ok(value.map(|v| v.with_timezone(&ist())))
}

fn skipped(e: &ProviderError) -> String {
    format!("{}: {e}", e.python_type())
}

/// `reconcile_session(session, reader, day, now=, names=, checkpoint=session.commit)` under
pub async fn reconcile_session(
    db: &WriteDb,
    reader: &dyn MinuteReader,
    day: NaiveDate,
    now: DateTime<FixedOffset>,
    names: &[EqName],
) -> Result<BarsReport, WorkerError> {
    let started = Instant::now();
    let mut report = BarsReport {
        names: names.len(),
        ..BarsReport::default()
    };
    let (open_at, close_at) = session_bounds(day)?;
    let mut tx = db.begin().await?;
    for (index, name) in names.iter().enumerate() {
        match reader
            .minute_bars(
                name.kite_token,
                MinuteBound::from(open_at),
                MinuteBound::from(close_at),
            )
            .await
        {
            Err(e) => report.skipped.push((name.symbol.clone(), skipped(&e))),
            Ok(bars) => {
                report.calls += 1;
                report.written +=
                    upsert_bars(&mut tx, name.instrument_id, closed_bars(bars, now)).await?;
                if (index + 1) % CHECKPOINT_EVERY_NAMES == 0 {
                    tx.commit().await?;
                    tx = db.begin().await?;
                }
            }
        }
    }
    tx.commit().await?;
    report.seconds = started.elapsed().as_secs_f64();
    Ok(report)
}

/// `backfill(session, reader, start, end, now=, names=, checkpoint=session.commit)`: per name,
pub async fn backfill(
    db: &WriteDb,
    reader: &dyn MinuteReader,
    start: NaiveDate,
    end: NaiveDate,
    now: DateTime<FixedOffset>,
    names: &[EqName],
) -> Result<BarsReport, WorkerError> {
    if end < start {
        return Err(inverted(start, end));
    }
    let started = Instant::now();
    let mut report = BarsReport {
        names: names.len(),
        ..BarsReport::default()
    };
    let lo = ist_at(start, NaiveTime::MIN)?;
    let hi = ist_at(
        end,
        NaiveTime::from_hms_opt(23, 59, 59).unwrap_or(NaiveTime::MIN),
    )?;
    for name in names {
        let mut tx = db.begin().await?;
        let last = latest_ts(&mut tx, name.instrument_id, lo, hi).await?;
        tx.rollback().await?;
        let resume = last.map_or(start, |t| start.max(t.date_naive()));
        if last.is_some() {
            report.resumed_from.push((name.symbol.clone(), iso(resume)));
        }
        for (w_lo, w_hi) in day_windows(resume, end)? {
            match reader
                .minute_bars(
                    name.kite_token,
                    MinuteBound::from(w_lo),
                    MinuteBound::from(w_hi),
                )
                .await
            {
                Err(e) => report.skipped.push((
                    format!("{} {}..{}", name.symbol, iso(w_lo), iso(w_hi)),
                    skipped(&e),
                )),
                Ok(bars) => {
                    report.calls += 1;
                    let mut tx = db.begin().await?;
                    report.written +=
                        upsert_bars(&mut tx, name.instrument_id, closed_bars(bars, now)).await?;
                    tx.commit().await?;
                }
            }
        }
    }
    report.seconds = started.elapsed().as_secs_f64();
    Ok(report)
}
