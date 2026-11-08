//! Step 8 — `compute_market_health`. Ports `baskfy_worker.tasks.market_health` and the query
//! `baskfy_core.breadth.breadth_query` builds (its SQL, compiled by SQLAlchemy, with the bound
//! literals inlined: `1`, `100`, `0`, `10` and `round(..., 4)`).
//!
//! One `market_health_daily` row per market-health universe (docs/01 §6 lists twelve), from the
//! **point-in-time** `index_member_daily` rows of the date joined to the same date's
//! `factor_daily` rows (house rule 5). A gauge whose input column is entirely NULL is NULL, never
//! 0%: "0% of the market is above its 200-day average" is a claim about the market, not about
//! the pipeline.

use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

/// `baskfy_core.universes.MARKET_HEALTH_SLUGS` with their `index_id`s, in `UNIVERSES` order
/// (every universe with `market_health=True`: ids 1-12).
pub(crate) const MARKET_HEALTH_UNIVERSES: [(i16, &str); 12] = [
    (1, "nifty-50"),
    (2, "nifty-next-50"),
    (3, "nifty-100"),
    (4, "nifty-200"),
    (5, "nifty-500"),
    (6, "nifty-total-market"),
    (7, "nifty-large-mid-250"),
    (8, "nifty-midcap-150"),
    (9, "nifty-smallcap-250"),
    (10, "nifty-microcap-250"),
    (11, "nifty-mid-small-400"),
    (12, "nifty-allcap"),
];

/// `breadth_query(index_id, on)`, as SQLAlchemy compiles it.
const BREADTH_SQL: &str = "SELECT count(*) AS constituents, \
     round((CAST(count(CASE WHEN (factor_daily.close > factor_daily.ma_200) THEN 1 END) AS NUMERIC(20, 10)) * 100) \
       / CAST(CAST(nullif(count(factor_daily.ma_200), 0) AS NUMERIC(20, 10)) AS NUMERIC(20, 10)), 4), \
     round((CAST(count(CASE WHEN (factor_daily.close > factor_daily.ma_50) THEN 1 END) AS NUMERIC(20, 10)) * 100) \
       / CAST(CAST(nullif(count(factor_daily.ma_50), 0) AS NUMERIC(20, 10)) AS NUMERIC(20, 10)), 4), \
     round((CAST(count(CASE WHEN (abs(factor_daily.away_high_ath) <= 10) THEN 1 END) AS NUMERIC(20, 10)) * 100) \
       / CAST(CAST(nullif(count(factor_daily.away_high_ath), 0) AS NUMERIC(20, 10)) AS NUMERIC(20, 10)), 4), \
     round((CAST(count(CASE WHEN (factor_daily.ret_12m > 0) THEN 1 END) AS NUMERIC(20, 10)) * 100) \
       / CAST(CAST(nullif(count(factor_daily.ret_12m), 0) AS NUMERIC(20, 10)) AS NUMERIC(20, 10)), 4), \
     round((CAST(count(CASE WHEN (factor_daily.close > factor_daily.ma_20) THEN 1 END) AS NUMERIC(20, 10)) * 100) \
       / CAST(CAST(nullif(count(factor_daily.ma_20), 0) AS NUMERIC(20, 10)) AS NUMERIC(20, 10)), 4) \
     FROM index_member_daily JOIN factor_daily ON factor_daily.instrument_id = index_member_daily.instrument_id \
       AND factor_daily.date = index_member_daily.date \
     WHERE index_member_daily.index_id = $1 AND index_member_daily.date = $2";

/// The breadth row: `(count, above_200dma, above_50dma, within_10pct_ath, ret_1y_positive,
/// above_20dma)`.
type BreadthRow = (
    i64,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
);

/// `run_compute_market_health(session, outcome, on)`; returns the rows written.
pub(crate) async fn run(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let mut written = 0_i64;
    let mut incomplete: Vec<&str> = Vec::new();
    for (index_id, slug) in MARKET_HEALTH_UNIVERSES {
        let row: BreadthRow = sqlx::query_as(BREADTH_SQL)
            .bind(i64::from(index_id))
            .bind(on)
            .fetch_one(&mut *conn)
            .await?;
        let (constituents, above_200, above_50, near_ath, positive_1y, above_20) = row;
        if constituents != 0 && above_200.is_none() {
            incomplete.push(slug);
        }
        let constituents = i32::try_from(constituents).map_err(|_| {
            WorkerError::python("DataError", "value out of int32 range: constituent_count")
        })?;
        sqlx::query(
            "INSERT INTO market_health_daily (index_id, date, pct_above_200dma, pct_above_50dma, \
             pct_within_10pct_ath, pct_ret_1y_positive, pct_above_20dma, constituent_count) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8) \
             ON CONFLICT (index_id, date) DO UPDATE SET \
             pct_above_200dma = excluded.pct_above_200dma, \
             pct_above_50dma = excluded.pct_above_50dma, \
             pct_within_10pct_ath = excluded.pct_within_10pct_ath, \
             pct_above_20dma = excluded.pct_above_20dma, \
             pct_ret_1y_positive = excluded.pct_ret_1y_positive, \
             constituent_count = excluded.constituent_count",
        )
        .bind(index_id)
        .bind(on)
        .bind(above_200)
        .bind(above_50)
        .bind(near_ath)
        .bind(positive_1y)
        .bind(above_20)
        .bind(constituents)
        .execute(&mut *conn)
        .await?;
        written += 1;
    }
    outcome.rows_in = i64::try_from(MARKET_HEALTH_UNIVERSES.len()).unwrap_or(12);
    outcome.rows_out = written;
    outcome.note("date", pyjson::s(on.format("%Y-%m-%d").to_string()));
    outcome.note(
        "breadth_unavailable",
        if incomplete.is_empty() {
            baskfy_db::OrderedJson::Null
        } else {
            pyjson::strings(incomplete)
        },
    );
    Ok(written)
}
