//! `baskfy_worker.ranking_backfill` and `tasks.ranking.write_phase2_columns`.

use std::collections::HashSet;
use std::future::Future;
use std::pin::Pin;
use std::time::Instant;

use baskfy_core::factors::{FactorConfig, compute_factors};
use chrono::NaiveDate;
use polars::prelude::{BooleanChunked, DataFrame, NewChunkedArray as _};
use sqlx::{Connection as _, PgConnection};

use super::session::Session;
use crate::error::WorkerError;
use crate::steps::StepOutcome;
use crate::steps::compute::desk::{DESK_SCORE_VERSION, run as run_desk_score};
use crate::steps::compute::factors::{
    Bound, LoadedHistory, NSE_EXCHANGE_ID, apply_true_ath, bind_column, ints, load_history,
    writable_columns,
};
use crate::steps::compute::ranking::run_rank_columns;
use crate::steps::compute::{core_err, pl_err};

pub(crate) const COMPUTED: &str = "computed";
pub(crate) const SKIPPED_COMPLETE: &str = "skipped_complete";
pub(crate) const SKIPPED_NO_ROWS: &str = "skipped_no_factor_rows";
pub(crate) const FAILED: &str = "failed";

/// `RANKING_FACTOR_COLUMNS` (C1), in the core's order.
pub(crate) const PHASE2_COLUMNS: [&str; 25] = [
    "atr_14",
    "atr_ext_20",
    "ma50_slope_20",
    "eff_ratio_63",
    "max_dd_6m",
    "max_dd_12m",
    "downside_vol_6m",
    "downside_vol_12m",
    "sortino_6m",
    "sortino_12m",
    "underwater_12m",
    "ret_ex_top3_12m",
    "accel_21_105",
    "accel_21_105_vs",
    "vol_exp_21_126",
    "vol_persist_20",
    "excess_ret_3m",
    "excess_ret_6m",
    "excess_ret_12m",
    "resid_ret_12m",
    "rs_persist_126",
    "mom_pctile",
    "rank_persist_20",
    "nse_mr6",
    "nse_mr12",
];

/// `CROSS_SECTIONAL_COLUMNS`.
pub(crate) const CROSS_SECTIONAL_COLUMNS: [&str; 2] = ["mom_pctile", "rank_persist_20"];

/// `PER_INSTRUMENT_PHASE2_COLUMNS`: C1 minus the two cross-sectional columns.
pub(crate) fn per_instrument_phase2_columns() -> Vec<&'static str> {
    PHASE2_COLUMNS
        .iter()
        .copied()
        .filter(|c| !CROSS_SECTIONAL_COLUMNS.contains(c))
        .collect()
}

/// `DayResult`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct DayResult {
    pub day: NaiveDate,
    pub status: &'static str,
    pub factor_rows: i64,
    pub desk_rows: i64,
    pub seconds: f64,
    pub error: Option<String>,
}

impl DayResult {
    fn bare(day: NaiveDate, status: &'static str) -> Self {
        Self {
            day,
            status,
            factor_rows: 0,
            desk_rows: 0,
            seconds: 0.0,
            error: None,
        }
    }
}

/// `RankingBackfillReport`.
#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct Report {
    pub days: Vec<DayResult>,
}

impl Report {
    pub(crate) fn count(&self, status: &str) -> usize {
        self.days.iter().filter(|d| d.status == status).count()
    }

    pub(crate) fn errors(&self) -> Vec<String> {
        self.days
            .iter()
            .filter(|d| d.status == FAILED)
            .map(|d| {
                format!(
                    "{}: {}",
                    d.day.format("%Y-%m-%d"),
                    d.error.as_deref().unwrap_or("None")
                )
            })
            .collect()
    }
}

/// The desk scorer a day runs (`run_compute_desk_score`); a test swaps in a refusing one.
pub(crate) type DeskScorer =
    for<'c> fn(
        &'c mut PgConnection,
        NaiveDate,
    ) -> Pin<Box<dyn Future<Output = Result<i64, WorkerError>> + 'c>>;

/// `run_compute_desk_score(session, StepOutcome(), day)`.
pub(crate) fn desk_score<'c>(
    conn: &'c mut PgConnection,
    on: NaiveDate,
) -> Pin<Box<dyn Future<Output = Result<i64, WorkerError>> + 'c>> {
    Box::pin(async move {
        let mut outcome = StepOutcome::default();
        run_desk_score(conn, &mut outcome, on).await
    })
}

fn described(error: &WorkerError) -> String {
    format!("{}: {error}", error.python_type())
}

/// `trading_days_between(session, start, end)`: NSE trading days in `[start, end]`, oldest first.
pub(crate) async fn trading_days_between(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trading_day.date FROM trading_day WHERE trading_day.exchange_id = $1 \
         AND trading_day.is_trading_day IS true AND trading_day.date >= $2 \
         AND trading_day.date <= $3 ORDER BY trading_day.date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|r| r.0).collect())
}

/// `day_is_complete(session, day)`.
pub(crate) async fn day_is_complete(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<bool, WorkerError> {
    let nse_rows: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM factor_daily JOIN instrument ON instrument.id = \
         factor_daily.instrument_id WHERE factor_daily.date = $1 AND instrument.exchange_id = $2",
    )
    .bind(day)
    .bind(NSE_EXCHANGE_ID)
    .fetch_one(&mut *conn)
    .await?;
    if nse_rows == 0 {
        return Ok(false);
    }
    let desk_rows: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM desk_score_daily WHERE desk_score_daily.date = $1 \
         AND desk_score_daily.score_version = $2",
    )
    .bind(day)
    .bind(DESK_SCORE_VERSION)
    .fetch_one(&mut *conn)
    .await?;
    if desk_rows != nse_rows {
        return Ok(false);
    }
    let rankable_without_pctile: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM factor_daily WHERE factor_daily.date = $1 \
         AND factor_daily.universe_mask != 0 AND factor_daily.mom_pctile IS NULL \
         AND factor_daily.sharpe_12m IS NOT NULL AND factor_daily.sharpe_6m IS NOT NULL \
         AND factor_daily.sharpe_3m IS NOT NULL AND factor_daily.sharpe_1m IS NOT NULL",
    )
    .bind(day)
    .fetch_one(&mut *conn)
    .await?;
    Ok(rankable_without_pctile == 0)
}

/// `PolarsFactorEngine().run(history, on).frame`: the factor frame with the true ATH applied.
pub(crate) fn engine_frame(
    history: &LoadedHistory,
    on: NaiveDate,
) -> Result<(DataFrame, Vec<(i64, usize)>), WorkerError> {
    let result = compute_factors(
        &history.bars,
        on,
        &history.trading_days,
        Some(&history.benchmark),
        &FactorConfig::default(),
        Some(&history.market_benchmark),
    )
    .map_err(core_err)?;
    let windows = result.window_lengths();
    Ok((
        apply_true_ath(result.frame, &history.all_time_highs)?,
        windows,
    ))
}

fn py_list(names: &[&str]) -> String {
    let quoted: Vec<String> = names.iter().map(|n| format!("'{n}'")).collect();
    format!("[{}]", quoted.join(", "))
}

/// `write_phase2_columns(session, frame, on)`: UPDATE exactly the per-instrument C1 columns of
/// the rows that already exist for `on`; never inserts. Returns the rows updated.
pub(crate) async fn write_phase2_columns(
    conn: &mut PgConnection,
    frame: &DataFrame,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let existing: HashSet<i64> = sqlx::query_scalar::<_, i64>(
        "SELECT factor_daily.instrument_id FROM factor_daily WHERE factor_daily.date = $1",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await?
    .into_iter()
    .collect();
    let columns = per_instrument_phase2_columns();
    let missing: Vec<&str> = columns
        .iter()
        .copied()
        .filter(|c| frame.get_column_index(c).is_none())
        .collect();
    if !missing.is_empty() {
        return Err(WorkerError::python(
            "ValueError",
            format!(
                "the factor frame for {} carries no {}",
                on.format("%Y-%m-%d"),
                py_list(&missing)
            ),
        ));
    }
    let ids = ints(frame, "instrument_id")?;
    let mask: Vec<bool> = ids
        .iter()
        .map(|id| id.is_some_and(|i| existing.contains(&i)))
        .collect();
    let kept = frame
        .filter(&BooleanChunked::from_slice("keep".into(), &mask))
        .map_err(|e| pl_err(&e))?;
    let kept_ids: Vec<i64> = ints(&kept, "instrument_id")?
        .into_iter()
        .flatten()
        .collect();
    if kept_ids.is_empty() {
        return Ok(0);
    }
    let kinds = writable_columns(conn).await?;
    let mut casts: Vec<String> = vec!["$1::bigint[]".to_owned()];
    let mut sets: Vec<String> = Vec::new();
    let mut names: Vec<String> = vec!["instrument_id".to_owned()];
    let mut bound: Vec<Bound> = Vec::new();
    for column in &columns {
        let kind = kinds
            .iter()
            .find(|(n, _)| n == column)
            .map(|(_, k)| *k)
            .ok_or_else(|| {
                WorkerError::python(
                    "CompileError",
                    format!("factor_daily has no column {column}"),
                )
            })?;
        let value = bind_column(&kept, column, kind)?;
        let cast = match &value {
            Bound::Numeric(_) => "numeric[]",
            Bound::Int(_) => "bigint[]",
            Bound::Text(_) => "text[]",
        };
        casts.push(format!("${}::{cast}", casts.len() + 1));
        sets.push(format!("{column} = v.{column}"));
        names.push((*column).to_owned());
        bound.push(value);
    }
    let date_param = casts.len() + 1;
    let sql = format!(
        "UPDATE factor_daily AS f SET {} FROM UNNEST({}) AS v({}) \
         WHERE f.instrument_id = v.instrument_id AND f.date = ${date_param}",
        sets.join(", "),
        casts.join(", "),
        names.join(", ")
    );
    let mut query = sqlx::query(&sql).bind(kept_ids.clone());
    for value in bound {
        query = match value {
            Bound::Numeric(v) => query.bind(v),
            Bound::Int(v) => query.bind(v),
            Bound::Text(v) => query.bind(v),
        };
    }
    query.bind(on).execute(&mut *conn).await?;
    i64::try_from(kept_ids.len()).map_err(|_| WorkerError::python("OverflowError", "too many rows"))
}

async fn compute_day(
    conn: &mut PgConnection,
    day: NaiveDate,
    started: Instant,
    desk: DeskScorer,
) -> Result<DayResult, WorkerError> {
    let history = load_history(conn, day).await?;
    let (frame, _) = engine_frame(&history, day)?;
    drop(history);
    let factor_rows = write_phase2_columns(conn, &frame, day).await?;
    run_rank_columns(conn, day).await?;
    let mut savepoint = conn.begin().await?;
    match desk(&mut savepoint, day).await {
        Ok(desk_rows) => {
            savepoint.commit().await?;
            Ok(DayResult {
                factor_rows,
                desk_rows,
                seconds: started.elapsed().as_secs_f64(),
                ..DayResult::bare(day, COMPUTED)
            })
        }
        Err(e) => {
            savepoint.rollback().await?;
            Ok(DayResult {
                factor_rows,
                seconds: started.elapsed().as_secs_f64(),
                error: Some(format!("desk_score: {}", described(&e))),
                ..DayResult::bare(day, FAILED)
            })
        }
    }
}

/// `backfill_ranking_day(session, day, force=...)`, on `conn`, without committing.
pub(crate) async fn backfill_ranking_day(
    conn: &mut PgConnection,
    day: NaiveDate,
    force: bool,
    desk: DeskScorer,
) -> Result<DayResult, WorkerError> {
    let started = Instant::now();
    if !force && day_is_complete(conn, day).await? {
        return Ok(DayResult::bare(day, SKIPPED_COMPLETE));
    }
    let existing: i64 =
        sqlx::query_scalar("SELECT count(*) FROM factor_daily WHERE factor_daily.date = $1")
            .bind(day)
            .fetch_one(&mut *conn)
            .await?;
    if existing == 0 {
        return Ok(DayResult::bare(day, SKIPPED_NO_ROWS));
    }
    compute_day(conn, day, started, desk).await
}

fn log(text: &str) {
    let stamp = (chrono::Utc::now() + chrono::Duration::minutes(330))
        .format("%Y-%m-%d %H:%M:%S,%3f")
        .to_string();
    super::super::cli::warn(&format!("{stamp} {text}"));
}

/// `run_backfill_ranking(session, start, end, force=..., checkpoint=...)`: every trading day in
/// `[start, end]`, oldest first, each in its own savepoint, checkpointed after each day.
pub(crate) async fn run_backfill_ranking<S: Session>(
    session: &mut S,
    start: NaiveDate,
    end: NaiveDate,
    force: bool,
    desk: DeskScorer,
) -> Result<Report, WorkerError> {
    let mut report = Report::default();
    let days = trading_days_between(session.conn(), start, end).await?;
    log(&format!(
        "backfill-ranking {}..{}: {} trading days",
        start.format("%Y-%m-%d"),
        end.format("%Y-%m-%d"),
        days.len()
    ));
    for (index, day) in days.iter().copied().enumerate() {
        let mut savepoint = session.conn().begin().await?;
        let result = match backfill_ranking_day(&mut savepoint, day, force, desk).await {
            Ok(r) => {
                savepoint.commit().await?;
                r
            }
            Err(e) => {
                savepoint.rollback().await?;
                DayResult {
                    error: Some(described(&e)),
                    ..DayResult::bare(day, FAILED)
                }
            }
        };
        if result.status == FAILED {
            crate::steps::log_warning(&format!(
                "backfill-ranking {} failed: {}",
                day.format("%Y-%m-%d"),
                result.error.as_deref().unwrap_or("None")
            ));
        }
        session.checkpoint().await?;
        log(&format!(
            "backfill-ranking [{}/{}] {} {} factor_rows={} desk_rows={} {:.1}s",
            index + 1,
            days.len(),
            day.format("%Y-%m-%d"),
            result.status,
            result.factor_rows,
            result.desk_rows,
            result.seconds
        ));
        report.days.push(result);
    }
    Ok(report)
}

/// `render(report)`.
pub(crate) fn render(report: &Report) -> String {
    let computed: Vec<&DayResult> = report
        .days
        .iter()
        .filter(|d| d.status == COMPUTED)
        .collect();
    let seconds: f64 = computed.iter().map(|d| d.seconds).sum();
    #[allow(clippy::cast_precision_loss)]
    let per_day = if computed.is_empty() {
        0.0
    } else {
        seconds / computed.len() as f64
    };
    let mut lines = vec![format!(
        "BACKFILL-RANKING days={} computed={} skipped_complete={} skipped_no_factor_rows={} \
         failed={} seconds_per_computed_day={per_day:.2}",
        report.days.len(),
        computed.len(),
        report.count(SKIPPED_COMPLETE),
        report.count(SKIPPED_NO_ROWS),
        report.count(FAILED)
    )];
    lines.extend(
        report
            .errors()
            .into_iter()
            .take(20)
            .map(|e| format!("  {e}")),
    );
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    #[test]
    fn backfill_ranking_updates_no_column_outside_phase_2() {
        let mut all: Vec<&str> = per_instrument_phase2_columns();
        all.extend(CROSS_SECTIONAL_COLUMNS);
        all.sort_unstable();
        let mut phase2 = PHASE2_COLUMNS.to_vec();
        phase2.sort_unstable();
        assert_eq!(all, phase2);
        for legacy in [
            "close",
            "ret_12m",
            "sharpe_12m",
            "beta_12m",
            "universe_mask",
            "marketcap_cr",
        ] {
            assert!(!PHASE2_COLUMNS.contains(&legacy));
        }
    }

    #[test]
    fn render_counts_every_status_and_lists_errors() {
        let mut computed = DayResult::bare(d(2026, 9, 1), COMPUTED);
        computed.seconds = 3.0;
        let mut failed = DayResult::bare(d(2026, 9, 2), FAILED);
        failed.error = Some("desk_score: ZeroDivisionError: boom".to_owned());
        let report = Report {
            days: vec![
                computed,
                failed,
                DayResult::bare(d(2026, 9, 3), SKIPPED_COMPLETE),
                DayResult::bare(d(2026, 9, 4), SKIPPED_NO_ROWS),
            ],
        };
        assert_eq!(
            render(&report),
            "BACKFILL-RANKING days=4 computed=1 skipped_complete=1 skipped_no_factor_rows=1 \
             failed=1 seconds_per_computed_day=3.00\n  2026-09-02: desk_score: \
             ZeroDivisionError: boom"
        );
        assert_eq!(
            render(&Report::default()),
            "BACKFILL-RANKING days=0 computed=0 skipped_complete=0 skipped_no_factor_rows=0 \
             failed=0 seconds_per_computed_day=0.00"
        );
    }
}
