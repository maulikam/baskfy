//! Ports `baskfy_worker.tasks.swing_backtest` (SW9): the runner around the swing backtest.
//!
//! It loads the bars the way the detectors are given them (the nightly's adjusted frame, the
//! circuit band moved into the adjusted space, the user's `sw_config` floors) with the one
//! survivorship difference `04` §11 names — every name listed inside the run, delisted or not —
//! supplies the calendar, the index (`nifty-500`, `nifty-50` when it has too few rows, none
//! when neither has) and the delisting dates, and stores one `sw_backtest_run` row: written on
//! the way in, committed so an operator sees the run in flight, finished on the way out, and
//! on a raise given its `error` and committed before the error leaves (`run_checkpointed`).

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::swing::setups::SwingConfig;
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Utc};
use polars::prelude::{DataFrame, DataType, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{dec_str, error_text, iso_date, iso_utc, jsonb_text};
use super::swing_core::{BacktestResult, Params, params_json, run_backtest};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::sleeves::for_backtests::{
    NSE_EXCHANGE_ID, config_row, lookback_start, pl_err, swing_config,
};
use crate::tasks::TaskContext;

/// `DEFAULT_START`: `04` §11's "for each session 2017→".
pub(super) fn default_start() -> NaiveDate {
    NaiveDate::from_ymd_opt(2017, 1, 1).unwrap_or_default()
}

/// `INDEX_SLUG` / `INDEX_FALLBACK_SLUG` — the backtest's own pair (the nightly gate has since
/// moved to `nifty-smallcap-250`, SW29; this module was not changed with it, and the port
/// reproduces the module as it stands).
const INDEX_SLUG: &str = "nifty-500";
const INDEX_FALLBACK_SLUG: &str = "nifty-50";

/// `tasks.swing.LOOKBACK_SESSIONS` and `CASH_SERIES`.
const LOOKBACK_SESSIONS: i64 = 200;
const CASH_SERIES: [&str; 2] = ["EQ", "BE"];

/// `_index_rows`: levels of `slug` in `[window_start, end]`.
async fn index_rows(
    conn: &mut PgConnection,
    slug: &str,
    window_start: NaiveDate,
    end: NaiveDate,
) -> Result<i64, WorkerError> {
    let (n,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM index_snapshot_daily s JOIN index_def d ON d.id = s.index_id \
         WHERE d.slug = $1 AND s.date >= $2 AND s.date <= $3 AND s.level IS NOT NULL",
    )
    .bind(slug)
    .bind(window_start)
    .bind(end)
    .fetch_one(&mut *conn)
    .await?;
    Ok(n)
}

/// `resolve_index_slug`: the first of the pair with `index_ma_slow` levels in the window.
async fn resolve_index_slug(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
    config: &SwingConfig,
) -> Result<Option<String>, WorkerError> {
    let window_start = lookback_start(conn, start, LOOKBACK_SESSIONS).await?;
    for candidate in [INDEX_SLUG, INDEX_FALLBACK_SLUG] {
        if index_rows(conn, candidate, window_start, end).await? >= config.market.index_ma_slow {
            return Ok(Some(candidate.to_owned()));
        }
    }
    Ok(None)
}

/// `params_for`: the user's liquidity floors over the pack's defaults, the index resolved.
async fn params_for(
    conn: &mut PgConnection,
    user_id: i64,
    start: NaiveDate,
    end: NaiveDate,
    sleeve_inr: Option<Decimal>,
    cost_pct_per_side: Option<Decimal>,
) -> Result<Params, WorkerError> {
    let config = swing_config(config_row(conn, user_id).await?.as_ref());
    let index_slug = resolve_index_slug(conn, start, end, &config).await?;
    Ok(Params {
        start,
        end,
        sleeve_inr: sleeve_inr.unwrap_or_else(|| Decimal::from(1_000_000_i64)),
        cost_pct_per_side: cost_pct_per_side.unwrap_or_else(|| Decimal::new(13, 2)),
        config,
        index_slug,
    })
}

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
    Option<f64>,
    Option<f64>,
);

/// `load_backtest_bars`: `_bar_query` with the survivorship clause, through `bars_frame`.
async fn load_bars(
    conn: &mut PgConnection,
    params: &Params,
    window_start: NaiveDate,
) -> Result<DataFrame, WorkerError> {
    let series: Vec<String> = CASH_SERIES.iter().map(|s| (*s).to_owned()).collect();
    let records: Vec<BarRow> = sqlx::query_as(
        "SELECT o.instrument_id, i.symbol, o.date, o.open::float8, o.high::float8, \
         o.low::float8, o.close::float8, o.volume::float8, o.turnover::float8, \
         o.upper_circuit::float8, o.adj_factor::float8 \
         FROM ohlcv_daily o JOIN instrument i ON i.id = o.instrument_id \
         WHERE o.date >= $1 AND o.date <= $2 \
         AND (i.delisted_on IS NULL OR i.delisted_on >= $3) \
         AND i.instrument_type = 'EQ' AND i.series = ANY($4) \
         ORDER BY o.instrument_id, o.date",
    )
    .bind(window_start)
    .bind(params.end)
    .bind(params.start)
    .bind(&series)
    .fetch_all(&mut *conn)
    .await?;
    let n = records.len();
    let mut ids = Vec::with_capacity(n);
    let mut symbols = Vec::with_capacity(n);
    let mut days = Vec::with_capacity(n);
    let mut cols: [Vec<Option<f64>>; 8] = Default::default();
    for r in records {
        // `float(adj_factor) if adj_factor is not None else 1.0`.
        let factor = r.10.unwrap_or(1.0);
        ids.push(r.0);
        symbols.push(r.1);
        days.push(Some(day_number(r.2)));
        cols[0].push(Some(r.3));
        cols[1].push(Some(r.4));
        cols[2].push(Some(r.5));
        cols[3].push(Some(r.6));
        cols[4].push(Some(r.7));
        cols[5].push(r.8);
        cols[6].push(r.9.map(|c| c * factor));
        cols[7].push(Some(factor));
    }
    let names = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "upper_circuit",
        "adj_factor",
    ];
    let mut columns = vec![
        Series::new("instrument_id".into(), ids).into_column(),
        Series::new("symbol".into(), symbols).into_column(),
        Series::new("date".into(), days)
            .cast(&DataType::Date)
            .map_err(|e| pl_err(&e))?
            .into_column(),
    ];
    for (name, values) in names.iter().zip(cols) {
        columns.push(Series::new((*name).into(), values).into_column());
    }
    DataFrame::new(n, columns).map_err(|e| pl_err(&e))
}

fn day_number(d: NaiveDate) -> i32 {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap_or_default();
    i32::try_from((d - epoch).num_days()).unwrap_or(i32::MAX)
}

/// `load_backtest_calendar`: every NSE trading day from the lookback start to `end`.
async fn load_calendar(
    conn: &mut PgConnection,
    window_start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date >= $2 AND date <= $3 ORDER BY date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(window_start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|r| r.0).collect())
}

/// `load_backtest_index`: the resolved slug's levels, oldest first.
async fn load_index(
    conn: &mut PgConnection,
    params: &Params,
    window_start: NaiveDate,
) -> Result<Option<Vec<(NaiveDate, f64)>>, WorkerError> {
    let Some(slug) = &params.index_slug else {
        return Ok(None);
    };
    let rows: Vec<(NaiveDate, f64)> = sqlx::query_as(
        "SELECT s.date, s.level::float8 FROM index_snapshot_daily s \
         JOIN index_def d ON d.id = s.index_id \
         WHERE d.slug = $1 AND s.date >= $2 AND s.date <= $3 AND s.level IS NOT NULL \
         ORDER BY s.date",
    )
    .bind(slug)
    .bind(window_start)
    .bind(params.end)
    .fetch_all(&mut *conn)
    .await?;
    Ok(Some(rows))
}

/// `load_delisted`: `instrument_id -> delisted_on` for every name in the frame that has one.
async fn load_delisted(
    conn: &mut PgConnection,
    bars: &DataFrame,
) -> Result<BTreeMap<i64, NaiveDate>, WorkerError> {
    if bars.height() == 0 {
        return Ok(BTreeMap::new());
    }
    let ids: BTreeSet<i64> = bars
        .column("instrument_id")
        .map_err(|e| pl_err(&e))?
        .as_materialized_series()
        .i64()
        .map_err(|e| pl_err(&e))?
        .iter()
        .flatten()
        .collect();
    let ids: Vec<i64> = ids.into_iter().collect();
    let rows: Vec<(i64, NaiveDate)> = sqlx::query_as(
        "SELECT id, delisted_on FROM instrument WHERE id = ANY($1) AND delisted_on IS NOT NULL",
    )
    .bind(&ids)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().collect())
}

/// `finish_run`'s body up to the result: load the inputs and run the engine.
async fn compute(conn: &mut PgConnection, params: &Params) -> Result<BacktestResult, WorkerError> {
    let window_start = lookback_start(conn, params.start, LOOKBACK_SESSIONS).await?;
    let bars = load_bars(conn, params, window_start).await?;
    let window_start = lookback_start(conn, params.start, LOOKBACK_SESSIONS).await?;
    let calendar = load_calendar(conn, window_start, params.end).await?;
    let window_start = lookback_start(conn, params.start, LOOKBACK_SESSIONS).await?;
    let index = load_index(conn, params, window_start).await?;
    let delisted = load_delisted(conn, &bars).await?;
    run_backtest(&bars, params, &calendar, index.as_deref(), &delisted)
}

/// `summary(row, result)`: what the task returns.
fn summary(
    run_id: i64,
    started_at: DateTime<Utc>,
    finished_at: DateTime<Utc>,
    result: &BacktestResult,
) -> OrderedJson {
    let st = &result.stats;
    object([
        ("run_id", int(run_id)),
        ("start", s(iso_date(result.params.start))),
        ("end", s(iso_date(result.params.end))),
        (
            "index_slug",
            result
                .params
                .index_slug
                .as_ref()
                .map_or(OrderedJson::Null, |t| s(t.clone())),
        ),
        (
            "sessions",
            int(i64::try_from(result.equity_curve.len()).unwrap_or(i64::MAX)),
        ),
        ("trades", int(st.trades)),
        ("win_rate_pct", s(dec_str(st.win_rate_pct))),
        ("expectancy_r", s(dec_str(st.expectancy_r))),
        ("net_r", s(dec_str(st.net_r))),
        (
            "profit_factor",
            st.profit_factor
                .map_or(OrderedJson::Null, |p| s(dec_str(p))),
        ),
        ("max_drawdown_pct", s(dec_str(result.drawdown.max_pct))),
        (
            "gate_comparison",
            object(result.comparison.overall.iter().map(|(mode, cell)| {
                (
                    mode.value(),
                    object([
                        ("entered", int(cell.entered)),
                        ("net_r", s(dec_str(cell.net_r))),
                    ]),
                )
            })),
        ),
        ("funnel", result.funnel.json()),
        ("started_at", s(iso_utc(started_at))),
        ("finished_at", s(iso_utc(finished_at))),
    ])
}

/// `run_and_commit(session, user_id=, start=, end=, sleeve_inr=, cost_pct_per_side=)` over a
/// checkpointed session: the row committed as soon as it is started, the result (or the error)
/// committed after.
pub(super) async fn run_and_commit(
    ctx: &TaskContext,
    user_id: i64,
    start: NaiveDate,
    end: NaiveDate,
    sleeve_inr: Option<Decimal>,
    cost_pct_per_side: Option<Decimal>,
) -> Result<OrderedJson, WorkerError> {
    // `params_for` and `start_run`, then the first commit.
    let mut tx = ctx.db.begin().await?;
    let params = params_for(&mut tx, user_id, start, end, sleeve_inr, cost_pct_per_side).await?;
    let started_at = ctx.now_utc();
    let (run_id,): (i64,) = sqlx::query_as(
        "INSERT INTO sw_backtest_run (user_id, params, started_at) VALUES ($1, $2::jsonb, $3) \
         RETURNING id",
    )
    .bind(user_id)
    .bind(jsonb_text(&params_json(&params)?)?)
    .bind(started_at)
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;

    let mut tx = ctx.db.begin().await?;
    let outcome = compute(&mut tx, &params).await;
    let result = match outcome {
        Ok(result) => result,
        Err(error) => {
            let text = error_text(&error, "\n\n", "tasks::sleeve_backtests::swing::compute");
            // A failed statement aborts the transaction; the row's error goes in on a fresh one.
            drop(tx);
            let mut tx = ctx.db.begin().await?;
            sqlx::query("UPDATE sw_backtest_run SET error = $1, finished_at = $2 WHERE id = $3")
                .bind(text)
                .bind(ctx.now_utc())
                .bind(run_id)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
            crate::steps::log_warning(&format!("swing backtest {run_id} failed: {error}"));
            return Err(error);
        }
    };
    let stats = jsonb_text(&result.to_json()?)?;
    let finished_at = ctx.now_utc();
    sqlx::query("UPDATE sw_backtest_run SET stats = $1::jsonb, finished_at = $2 WHERE id = $3")
        .bind(stats)
        .bind(finished_at)
        .bind(run_id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(summary(run_id, started_at, finished_at, &result))
}
