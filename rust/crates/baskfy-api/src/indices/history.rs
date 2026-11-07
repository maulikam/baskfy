//! `index_bar_daily` from Kite's daily history and `index_stats_daily` computed from it, with
//! `index_snapshot_daily`'s NSE level filling sessions (or indices) Kite does not carry.

use std::collections::BTreeMap;

use baskfy_providers::kite::KiteProvider;
use chrono::{Duration, NaiveDate};
use polars::prelude::{AnyValue, Column, DataFrame};
use rust_decimal::Decimal;
use sqlx::PgConnection;

/// Fewer stored bars than this triggers a backfill.
pub const MIN_BARS: i64 = 300;
/// The backfill window, in calendar days (~300 sessions).
pub const BACKFILL_DAYS: i64 = 450;
/// An incremental pull re-reads this many days.
pub const OVERLAP_DAYS: i64 = 7;
/// Stats rows kept fresh per index on each recompute (breadth needs 30).
pub const STATS_SESSIONS: usize = 60;

/// One daily bar.
#[derive(Debug, Clone, Copy)]
pub struct Bar {
    /// Session.
    pub date: NaiveDate,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
}

fn cell_decimal(col: &Column, row: usize) -> Option<Decimal> {
    match col.get(row).ok()? {
        AnyValue::Decimal(mantissa, _, scale) => {
            Decimal::try_from_i128_with_scale(mantissa, u32::try_from(scale).ok()?).ok()
        }
        AnyValue::Float64(f) => Decimal::try_from(f).ok(),
        AnyValue::Int64(v) => Some(Decimal::from(v)),
        _ => None,
    }
}

fn cell_date(col: &Column, row: usize) -> Option<NaiveDate> {
    match col.get(row).ok()? {
        AnyValue::Date(days) => baskfy_core::fno::series::cols::from_days(days),
        _ => None,
    }
}

fn frame_bars(df: &DataFrame) -> Vec<Bar> {
    let cols = ["date", "open", "high", "low", "close"].map(|c| df.column(c).ok());
    let [Some(d), Some(o), Some(h), Some(l), Some(c)] = cols else {
        return Vec::new();
    };
    (0..df.height())
        .filter_map(|i| {
            let bar = Bar {
                date: cell_date(d, i)?,
                open: cell_decimal(o, i)?.round_dp(2),
                high: cell_decimal(h, i)?.round_dp(2),
                low: cell_decimal(l, i)?.round_dp(2),
                close: cell_decimal(c, i)?.round_dp(2),
            };
            (bar.high >= bar.low).then_some(bar)
        })
        .collect()
}

/// Kite `historical_data(interval="day")` for one index token.
///
/// # Errors
/// The provider's, as text.
pub async fn fetch_bars(
    kite: &KiteProvider,
    token: i64,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<Bar>, String> {
    let df = kite
        .daily_bars(token, start, end)
        .await
        .map_err(|e| e.to_string())?;
    Ok(frame_bars(&df))
}

/// The window each live index needs: `(index_id, token, start, end)`.
///
/// # Errors
/// The database's, as text.
pub async fn plan(
    conn: &mut PgConnection,
    today: NaiveDate,
    only: Option<i16>,
) -> Result<Vec<(i16, i64, NaiveDate, NaiveDate)>, String> {
    let rows: Vec<(i16, i64, i64, Option<NaiveDate>)> = sqlx::query_as(
        "SELECT m.id, m.kite_token, count(b.date), max(b.date) FROM index_master m \
         LEFT JOIN index_bar_daily b ON b.index_id = m.id \
         WHERE m.live AND m.kite_token IS NOT NULL AND ($1::smallint IS NULL OR m.id = $1) \
         GROUP BY m.id, m.kite_token ORDER BY m.id",
    )
    .bind(only)
    .fetch_all(&mut *conn)
    .await
    .map_err(|e| e.to_string())?;
    Ok(rows
        .into_iter()
        .filter_map(|(id, token, n, last)| {
            let start = match last {
                Some(d) if n >= MIN_BARS => d - Duration::days(OVERLAP_DAYS),
                _ => today - Duration::days(BACKFILL_DAYS),
            };
            (start <= today).then_some((id, token, start, today))
        })
        .collect())
}

/// Upsert one index's bars.
///
/// # Errors
/// The database's, as text.
pub async fn write_bars(conn: &mut PgConnection, index_id: i16, bars: &[Bar]) -> Result<(), String> {
    if bars.is_empty() {
        return Ok(());
    }
    let col = |f: fn(&Bar) -> Decimal| bars.iter().map(f).collect::<Vec<_>>();
    sqlx::query(
        "INSERT INTO index_bar_daily (index_id, date, open, high, low, close) \
         SELECT $1, t.* FROM UNNEST($2::date[], $3::numeric[], $4::numeric[], $5::numeric[], \
         $6::numeric[]) AS t ON CONFLICT (index_id, date) DO UPDATE SET open = excluded.open, \
         high = excluded.high, low = excluded.low, close = excluded.close",
    )
    .bind(index_id)
    .bind(bars.iter().map(|b| b.date).collect::<Vec<_>>())
    .bind(col(|b| b.open))
    .bind(col(|b| b.high))
    .bind(col(|b| b.low))
    .bind(col(|b| b.close))
    .execute(&mut *conn)
    .await
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// One session of the merged close series.
#[derive(Debug, Clone, Copy)]
pub struct Point {
    /// Session.
    pub date: NaiveDate,
    /// Open (absent for an NSE-level-only day).
    pub open: Option<Decimal>,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
}

/// Kite bars where stored, the NSE level elsewhere; oldest first.
///
/// # Errors
/// The database's, as text.
pub async fn series(
    conn: &mut PgConnection,
    index_id: i16,
    def_id: Option<i16>,
    from: NaiveDate,
) -> Result<Vec<Point>, String> {
    let mut by_date: BTreeMap<NaiveDate, Point> = BTreeMap::new();
    if let Some(def) = def_id {
        let rows: Vec<(NaiveDate, Decimal)> = sqlx::query_as(
            "SELECT date, level FROM index_snapshot_daily \
             WHERE index_id = $1 AND level IS NOT NULL AND date >= $2",
        )
        .bind(def)
        .bind(from)
        .fetch_all(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
        for (date, level) in rows {
            let c = level.round_dp(2);
            by_date.insert(
                date,
                Point {
                    date,
                    open: None,
                    high: c,
                    low: c,
                    close: c,
                },
            );
        }
    }
    let rows: Vec<(NaiveDate, Decimal, Decimal, Decimal, Decimal)> = sqlx::query_as(
        "SELECT date, open, high, low, close FROM index_bar_daily \
         WHERE index_id = $1 AND date >= $2",
    )
    .bind(index_id)
    .bind(from)
    .fetch_all(&mut *conn)
    .await
    .map_err(|e| e.to_string())?;
    for (date, open, high, low, close) in rows {
        by_date.insert(
            date,
            Point {
                date,
                open: Some(open),
                high,
                low,
                close,
            },
        );
    }
    Ok(by_date.into_values().collect())
}

/// Mean of the `k` closes ending at `i` (inclusive), rounded to the paisa.
#[must_use]
pub fn sma(points: &[Point], i: usize, k: usize) -> Option<Decimal> {
    if k == 0 || i + 1 < k {
        return None;
    }
    let sum: Decimal = points[i + 1 - k..=i].iter().map(|p| p.close).sum();
    Some((sum / Decimal::from(k)).round_dp(2))
}

fn pct(a: Decimal, b: Decimal) -> Option<Decimal> {
    (!b.is_zero()).then(|| ((a / b - Decimal::ONE) * Decimal::ONE_HUNDRED).round_dp(2))
}

/// The close on or before `day`.
fn close_on_or_before(points: &[Point], i: usize, day: NaiveDate) -> Option<Decimal> {
    points[..=i].iter().rev().find(|p| p.date <= day).map(|p| p.close)
}

/// One stats row.
#[derive(Debug, Clone, Default)]
pub struct Stats {
    /// Session.
    pub date: Option<NaiveDate>,
    /// Close.
    pub close: Decimal,
    /// % vs the previous session.
    pub chg_pct: Option<Decimal>,
    /// DMA 10/20/50/200.
    pub dma: [Option<Decimal>; 4],
    /// % above DMA20.
    pub vs20_pct: Option<Decimal>,
    /// DMA10 above DMA20.
    pub cross_10_20: Option<bool>,
    /// 5-session, 30-day, 1-year returns (%).
    pub ret: [Option<Decimal>; 3],
    /// 52-week high and low.
    pub hi_52w: Option<Decimal>,
    /// 52-week low.
    pub lo_52w: Option<Decimal>,
    /// % below the 52-week high.
    pub dist_52w_hi_pct: Option<Decimal>,
    /// The swing gate's EMA(10) / EMA(20), paisa.
    pub ema: Option<(Decimal, Decimal)>,
    /// EMA(10) > EMA(20) (unrounded, as the gate compares).
    pub ema_cross: Option<bool>,
}

/// The swing gate's EMA(10)/EMA(20) at session `i` over the last 250 closes.
#[must_use]
pub fn ema_at(points: &[Point], i: usize) -> Option<(f64, f64)> {
    use baskfy_core::swing::market::{INDEX_HISTORY_SESSIONS, gate_ema_pair};
    use rust_decimal::prelude::ToPrimitive;
    let from = (i + 1).saturating_sub(INDEX_HISTORY_SESSIONS);
    let closes: Vec<f64> = points
        .get(from..=i)?
        .iter()
        .map(|p| p.close.to_f64())
        .collect::<Option<_>>()?;
    gate_ema_pair(&closes, 10, 20)
}

/// Stats at session `i`.
#[must_use]
pub fn stats_at(points: &[Point], i: usize) -> Stats {
    let p = points[i];
    let dma = [10, 20, 50, 200].map(|k| sma(points, i, k));
    let year_ago = p.date - Duration::days(365);
    let window = points[..=i].iter().filter(|q| q.date > year_ago);
    let (mut hi, mut lo): (Option<Decimal>, Option<Decimal>) = (None, None);
    for q in window {
        hi = Some(hi.map_or(q.high, |h| h.max(q.high)));
        lo = Some(lo.map_or(q.low, |l| l.min(q.low)));
    }
    let has_year = points.first().is_some_and(|f| f.date <= year_ago);
    let ema = ema_at(points, i);
    Stats {
        date: Some(p.date),
        close: p.close,
        chg_pct: i.checked_sub(1).and_then(|j| pct(p.close, points[j].close)),
        vs20_pct: dma[1].and_then(|d| pct(p.close, d)),
        cross_10_20: dma[0].zip(dma[1]).map(|(a, b)| a > b),
        ret: [
            i.checked_sub(5).and_then(|j| pct(p.close, points[j].close)),
            close_on_or_before(points, i, p.date - Duration::days(30))
                .filter(|_| points[0].date <= p.date - Duration::days(30))
                .and_then(|c| pct(p.close, c)),
            if has_year {
                close_on_or_before(points, i, year_ago).and_then(|c| pct(p.close, c))
            } else {
                None
            },
        ],
        hi_52w: hi,
        lo_52w: lo,
        dist_52w_hi_pct: hi.and_then(|h| pct(p.close, h)),
        dma,
        ema: ema.and_then(|(a, b)| {
            use rust_decimal::prelude::FromPrimitive;
            Some((Decimal::from_f64(a)?.round_dp(2), Decimal::from_f64(b)?.round_dp(2)))
        }),
        ema_cross: ema.map(|(a, b)| a > b),
    }
}

/// Recompute the last [`STATS_SESSIONS`] stats rows of one index.
///
/// # Errors
/// The database's, as text.
pub async fn recompute_stats(
    conn: &mut PgConnection,
    index_id: i16,
    def_id: Option<i16>,
    today: NaiveDate,
) -> Result<i64, String> {
    let from = today - Duration::days(BACKFILL_DAYS + 120);
    write_stats(conn, index_id, def_id, from, Some(STATS_SESSIONS)).await
}

/// Stats rows from the series since `from`: the last `sessions`, or every session (`None`).
///
/// # Errors
/// The database's, as text.
pub async fn write_stats(
    conn: &mut PgConnection,
    index_id: i16,
    def_id: Option<i16>,
    from: NaiveDate,
    sessions: Option<usize>,
) -> Result<i64, String> {
    let pts = series(conn, index_id, def_id, from).await?;
    if pts.is_empty() {
        return Ok(0);
    }
    let from = sessions.map_or(0, |n| pts.len().saturating_sub(n));
    let rows: Vec<Stats> = (from..pts.len()).map(|i| stats_at(&pts, i)).collect();
    let dec = |f: &dyn Fn(&Stats) -> Option<Decimal>| rows.iter().map(f).collect::<Vec<_>>();
    sqlx::query(
        "INSERT INTO index_stats_daily (index_id, date, close, chg_pct, dma10, dma20, dma50, \
         dma200, vs20_pct, cross_10_20, ret_5d, ret_30d, ret_1y, hi_52w, lo_52w, dist_52w_hi_pct, \
         ema10, ema20, ema_cross) \
         SELECT $1, t.* FROM UNNEST($2::date[], $3::numeric[], $4::numeric[], $5::numeric[], \
         $6::numeric[], $7::numeric[], $8::numeric[], $9::numeric[], $10::bool[], $11::numeric[], \
         $12::numeric[], $13::numeric[], $14::numeric[], $15::numeric[], $16::numeric[], \
         $17::numeric[], $18::numeric[], $19::bool[]) AS t \
         ON CONFLICT (index_id, date) DO UPDATE SET close = excluded.close, \
         chg_pct = excluded.chg_pct, dma10 = excluded.dma10, dma20 = excluded.dma20, \
         dma50 = excluded.dma50, dma200 = excluded.dma200, vs20_pct = excluded.vs20_pct, \
         cross_10_20 = excluded.cross_10_20, ret_5d = excluded.ret_5d, \
         ret_30d = excluded.ret_30d, ret_1y = excluded.ret_1y, hi_52w = excluded.hi_52w, \
         lo_52w = excluded.lo_52w, dist_52w_hi_pct = excluded.dist_52w_hi_pct, \
         ema10 = excluded.ema10, ema20 = excluded.ema20, ema_cross = excluded.ema_cross",
    )
    .bind(index_id)
    .bind(rows.iter().filter_map(|r| r.date).collect::<Vec<_>>())
    .bind(rows.iter().map(|r| r.close).collect::<Vec<_>>())
    .bind(dec(&|r| r.chg_pct))
    .bind(dec(&|r| r.dma[0]))
    .bind(dec(&|r| r.dma[1]))
    .bind(dec(&|r| r.dma[2]))
    .bind(dec(&|r| r.dma[3]))
    .bind(dec(&|r| r.vs20_pct))
    .bind(rows.iter().map(|r| r.cross_10_20).collect::<Vec<_>>())
    .bind(dec(&|r| r.ret[0]))
    .bind(dec(&|r| r.ret[1]))
    .bind(dec(&|r| r.ret[2]))
    .bind(dec(&|r| r.hi_52w))
    .bind(dec(&|r| r.lo_52w))
    .bind(dec(&|r| r.dist_52w_hi_pct))
    .bind(dec(&|r| r.ema.map(|e| e.0)))
    .bind(dec(&|r| r.ema.map(|e| e.1)))
    .bind(rows.iter().map(|r| r.ema_cross).collect::<Vec<_>>())
    .execute(&mut *conn)
    .await
    .map_err(|e| e.to_string())?;
    Ok(i64::try_from(rows.len()).unwrap_or(i64::MAX))
}

/// Recompute stats for every master index. Returns rows written.
///
/// # Errors
/// The database's, as text.
pub async fn recompute_all_stats(conn: &mut PgConnection) -> Result<i64, String> {
    let today = super::now_ist().date_naive();
    let defs = super::master::index_def_map(conn).await?;
    let ids: Vec<(i16,)> = sqlx::query_as("SELECT id FROM index_master ORDER BY id")
        .fetch_all(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
    let mut n = 0;
    for (id,) in ids {
        n += recompute_stats(conn, id, defs.get(&id).copied(), today).await?;
    }
    Ok(n)
}

/// Every session's stats (EMA columns included) for every master index, from all stored history.
/// Returns rows written.
///
/// # Errors
/// The database's, as text.
pub async fn backfill_all_stats(conn: &mut PgConnection) -> Result<i64, String> {
    let defs = super::master::index_def_map(conn).await?;
    let ids: Vec<(i16,)> = sqlx::query_as("SELECT id FROM index_master ORDER BY id")
        .fetch_all(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
    let from = NaiveDate::from_ymd_opt(2000, 1, 1).unwrap_or_default();
    let mut n = 0;
    for (id,) in ids {
        n += write_stats(conn, id, defs.get(&id).copied(), from, None).await?;
    }
    Ok(n)
}
