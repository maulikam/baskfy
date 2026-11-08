//! 52-week-high breakout and Minervini VCP hits for the regime candidate job, computed from
//! `ohlcv_daily` with baskfy-core's opportunities scan (the API's opportunities glue, trimmed).
//! Read-only: no Kite call, no order.

use std::collections::{BTreeSet, HashMap};

use baskfy_core::error::CoreError;
use baskfy_core::opportunities::config::{DEFAULT_SWING_PARAMS, SetupType, SwingFilters};
use baskfy_core::opportunities::indicators::with_indicators;
use baskfy_core::opportunities::swing::scan;
use chrono::{Days, NaiveDate};
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use crate::error::WorkerError;

/// Same lookback as the API's scan: 252 sessions plus indicator warm-up.
const HISTORY_CALENDAR_DAYS: u64 = 420;
const BENCHMARK_SLUGS: [&str; 2] = ["nifty-500", "nifty-50"];

/// One pattern hit: symbol, scanner (`HIGH52W` / `VCP`), `SETUP:STATUS`, entry, stop, score 0..1.
pub(crate) type PatternHit = (
    String,
    &'static str,
    String,
    Option<Decimal>,
    Option<Decimal>,
    f64,
);

type BarRow = (
    i64,
    String,
    NaiveDate,
    f64,
    f64,
    f64,
    f64,
    f64,
    Option<f64>,
    f64,
);

fn core(e: &CoreError) -> WorkerError {
    WorkerError::python("ValueError", e.to_string())
}

fn polars_err(e: polars::prelude::PolarsError) -> WorkerError {
    core(&e.into())
}

async fn benchmark(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Option<DataFrame>, WorkerError> {
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
        .fetch_all(&mut *conn)
        .await?;
        if !rows.is_empty() {
            let (dates, levels): (Vec<NaiveDate>, Vec<f64>) = rows.into_iter().unzip();
            let frame = DataFrame::new(
                dates.len(),
                vec![
                    Series::new("date".into(), dates).into_column(),
                    Series::new("level".into(), levels).into_column(),
                ],
            )
            .map_err(polars_err)?;
            return Ok(Some(frame));
        }
    }
    Ok(None)
}

/// HIGH_52W_BREAKOUT and VCP, triggered or pending on `on`, for the given symbols only.
///
/// # Errors
/// The database's; a polars/core error building the indicator frame.
pub(crate) async fn pattern_hits(
    conn: &mut PgConnection,
    universe_symbols: &[String],
    on: NaiveDate,
) -> Result<Vec<PatternHit>, WorkerError> {
    if universe_symbols.is_empty() {
        return Ok(Vec::new());
    }
    let start = on - Days::new(HISTORY_CALENDAR_DAYS);
    // Bars <= on only: no look-ahead.
    let rows: Vec<BarRow> = sqlx::query_as(
        "SELECT o.instrument_id::bigint, i.symbol, o.date, o.open::float8, o.high::float8, \
         o.low::float8, o.close::float8, o.volume::float8, o.turnover::float8, \
         COALESCE(o.adj_factor::float8, 1.0::float8) \
         FROM ohlcv_daily o JOIN instrument i ON i.id = o.instrument_id \
         WHERE i.symbol = ANY($1) AND o.date >= $2 AND o.date <= $3 \
         AND i.instrument_type = 'EQ' AND i.series IN ('EQ', 'BE') \
         ORDER BY o.instrument_id, o.date",
    )
    .bind(universe_symbols)
    .bind(start)
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let bench = benchmark(conn, start, on).await?;
    let n = rows.len();
    let (mut ids, mut sym, mut date) = (
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
    );
    let (mut open, mut high, mut low, mut close, mut vol, mut turn, mut adj) = (
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
        Vec::with_capacity(n),
    );
    for (id, s, d, o, h, l, c, v, t, a) in rows {
        ids.push(id);
        sym.push(s);
        date.push(d);
        open.push(o);
        high.push(h);
        low.push(l);
        close.push(c);
        vol.push(v);
        turn.push(t);
        adj.push(a);
    }
    let col = |name: &str, v: Vec<f64>| Series::new(name.into(), v).into_column();
    let frame = DataFrame::new(
        n,
        vec![
            Series::new("instrument_id".into(), ids).into_column(),
            Series::new("symbol".into(), sym).into_column(),
            Series::new("date".into(), date).into_column(),
            col("open", open),
            col("high", high),
            col("low", low),
            col("close", close),
            col("volume", vol),
            Series::new("turnover".into(), turn).into_column(),
            col("adj_factor", adj),
        ],
    )
    .map_err(polars_err)?;
    let indicated =
        with_indicators(&frame, bench.as_ref(), &DEFAULT_SWING_PARAMS).map_err(|e| core(&e))?;
    // Default screen filters; each setup keeps its own horizon; no result cap.
    let filters = SwingFilters {
        setups: BTreeSet::from([SetupType::High52wBreakout, SetupType::Vcp]),
        horizon_min: 0,
        horizon_max: i64::MAX,
        limit: i64::MAX,
        ..SwingFilters::default()
    };
    let result = scan(
        &indicated,
        on,
        &HashMap::new(),
        &filters,
        &DEFAULT_SWING_PARAMS,
    )
    .map_err(|e| core(&e))?;
    Ok(result
        .opportunities
        .into_iter()
        .filter_map(|o| {
            let scanner = match o.evaluation.setup {
                SetupType::High52wBreakout => "HIGH52W",
                SetupType::Vcp => "VCP",
                _ => return None,
            };
            Some((
                o.symbol,
                scanner,
                format!(
                    "{}:{}",
                    o.evaluation.setup.value(),
                    o.evaluation.status.value()
                ),
                Some(o.entry),
                Some(o.invalidation),
                (o.score.total / 100.0).clamp(0.0, 1.0),
            ))
        })
        .collect())
}
