//! Ports `baskfy_worker.tasks.vbt_backtest` (VB9): the study, re-run from the plant's own bars,
//! three books over one detection pass, one appended `vb_backtest_run` row.
//!
//! The arithmetic is `baskfy_core::vbt` (golden-graded); the universe and the bar loader are the
//! nightly detector's own (`steps::sleeves::for_backtests`, reused, never forked). Python runs
//! the body in `run_in_session` — one transaction — so a run that raises records its `error` and
//! is then **rolled back** with everything else: only the identity sequence remembers it. The
//! same here.

use std::collections::BTreeMap;

use baskfy_core::decimal::WideDecimal;
use baskfy_core::vbt::backtest::{
    BacktestParams, BacktestResult, BacktestStats, gate_vector, panel_from_frame, run_backtest,
    summarise, yearly,
};
use baskfy_core::vbt::breadth::breadth_series;
use baskfy_core::vbt::calendar::drop_thin_sessions;
use baskfy_core::vbt::config::VbtConfig;
use baskfy_core::vbt::drift::{Drift, compare};
use baskfy_core::vbt::indicators::with_vbt_indicators;
use baskfy_core::vbt::signals::with_signal_columns;
use baskfy_db::OrderedJson;
use chrono::{DateTime, Days, NaiveDate, Utc};
use sqlx::PgConnection;

use super::common::{error_text, iso_date, jf, opt_jf, today, wide_str};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::sleeves::for_backtests::{
    UniverseRule, core_err, load_raw_bars, load_universe, value_err,
};
use crate::tasks::TaskContext;

/// `SOURCE_PLANT`.
pub(super) const SOURCE_PLANT: &str = "PLANT";

/// `DEFAULT_START`: the study's window opens 2017-10-16 (`01` §4).
pub(super) fn default_start() -> NaiveDate {
    NaiveDate::from_ymd_opt(2017, 10, 16).unwrap_or_default()
}

/// `WARMUP_DAYS`: history loaded before `start` so the 200-session window is full on day one.
const WARMUP_DAYS: u64 = 400;

/// The finished row, as the task reads it back.
pub(super) struct VbRun {
    pub id: i64,
    pub finished_at: Option<DateTime<Utc>>,
    pub drift: Option<OrderedJson>,
}

/// `BacktestBooks`: `04` §11's three, over one detection pass.
struct Books {
    full: BacktestResult,
    gate_off: BacktestResult,
    raw_scan: BacktestResult,
}

fn labelled(params: &BacktestParams, label: &str) -> BacktestParams {
    BacktestParams {
        label: label.to_owned(),
        ..params.clone()
    }
}

/// `three_books`: as specified, without the gate, and on the raw scan.
fn three_books(
    tagged: &polars::prelude::DataFrame,
    breadth: &polars::prelude::DataFrame,
    params: &BacktestParams,
) -> Result<Books, WorkerError> {
    let panel = panel_from_frame(tagged, "state").map_err(core_err)?;
    let gate = gate_vector(breadth, &panel.sessions).map_err(core_err)?;
    let always_open = vec![true; panel.sessions_count()];
    let scan_panel = panel_from_frame(tagged, "scan_hit").map_err(core_err)?;
    let scan_gate = gate_vector(breadth, &scan_panel.sessions).map_err(core_err)?;
    Ok(Books {
        full: run_backtest(&panel, &gate, &labelled(params, "full")).map_err(core_err)?,
        gate_off: run_backtest(&panel, &always_open, &labelled(params, "gate_off"))
            .map_err(core_err)?,
        raw_scan: run_backtest(&scan_panel, &scan_gate, &labelled(params, "raw_scan"))
            .map_err(core_err)?,
    })
}

/// `skipped`'s dict literal (`vbt.backtest.run_backtest`).
const SKIPPED_ORDER: [&str; 8] = [
    "expired",
    "session_cap",
    "slots_full",
    "cash",
    "turnover",
    "gate_shut",
    "no_bar",
    "locked",
];

/// `ExitReason`'s members, in declaration order (`by_reason` iterates the enum).
const REASON_ORDER: [&str; 8] = [
    "EMA_EXIT",
    "STOP_HIT",
    "STOP_GAP",
    "NO_BAR",
    "END_OF_RUN",
    "MANUAL",
    "MA_TRAIL",
    "PARTIAL",
];

fn skipped(map: &BTreeMap<String, i64>) -> OrderedJson {
    super::common::ordered_counts(map, &SKIPPED_ORDER)
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `BacktestStats.to_json()`.
fn stats_json(stats: &BacktestStats) -> Vec<(String, OrderedJson)> {
    vec![
        ("start".to_owned(), s(iso_date(stats.start))),
        ("end".to_owned(), s(iso_date(stats.end))),
        ("years".to_owned(), jf(stats.years)),
        (
            "final_equity_inr".to_owned(),
            s(wide_str(&stats.final_equity_inr)),
        ),
        ("cagr_pct".to_owned(), jf(stats.cagr_pct)),
        ("max_drawdown_pct".to_owned(), jf(stats.max_drawdown_pct)),
        (
            "drawdown_peak_on".to_owned(),
            s(iso_date(stats.drawdown_peak_on)),
        ),
        (
            "drawdown_trough_on".to_owned(),
            s(iso_date(stats.drawdown_trough_on)),
        ),
        ("calmar".to_owned(), opt_jf(stats.calmar)),
        ("sharpe".to_owned(), jf(stats.sharpe)),
        ("trades".to_owned(), int(stats.trades)),
        ("win_rate_pct".to_owned(), jf(stats.win_rate_pct)),
        ("profit_factor".to_owned(), opt_jf(stats.profit_factor)),
        ("avg_win_pct".to_owned(), jf(stats.avg_win_pct)),
        ("avg_loss_pct".to_owned(), jf(stats.avg_loss_pct)),
        ("avg_trade_pct".to_owned(), jf(stats.avg_trade_pct)),
        ("avg_hold_sessions".to_owned(), jf(stats.avg_hold_sessions)),
        (
            "avg_open_positions".to_owned(),
            jf(stats.avg_open_positions),
        ),
        ("exposure_pct".to_owned(), jf(stats.exposure_pct)),
        (
            "by_reason".to_owned(),
            super::common::ordered_counts(&stats.by_reason, &REASON_ORDER),
        ),
    ]
}

/// `book_stats`: one book, in the JSONB shape `vb_backtest_run.stats` stores.
fn book_stats(result: &BacktestResult) -> Result<OrderedJson, WorkerError> {
    let Some(stats) = summarise(result).map_err(core_err)? else {
        return Ok(object([
            ("trades", int(0)),
            ("sessions", int(len_i64(result.sessions.len()))),
            ("skipped", skipped(&result.skipped)),
        ]));
    };
    let mut payload = stats_json(&stats);
    let years = yearly(result)
        .map_err(core_err)?
        .into_iter()
        .map(|row| {
            object([
                ("year", int(i64::from(row.year))),
                ("return_pct", jf(row.return_pct)),
                ("trades", int(row.trades)),
                ("win_rate_pct", jf(row.win_rate_pct)),
            ])
        })
        .collect();
    payload.push(("yearly".to_owned(), OrderedJson::Array(years)));
    payload.push(("equity_curve".to_owned(), equity_curve(result)?));
    payload.push(("skipped".to_owned(), skipped(&result.skipped)));
    payload.push(("orders_offered".to_owned(), int(result.orders_offered)));
    payload.push((
        "fill_rate_pct".to_owned(),
        opt_jf(result.fill_rate_pct().map_err(core_err)?),
    ));
    Ok(OrderedJson::Object(payload))
}

/// `zip(result.sessions, result.equity, strict=True)` as `{date, equity_inr}` rows.
fn equity_curve(result: &BacktestResult) -> Result<OrderedJson, WorkerError> {
    if result.sessions.len() != result.equity.len() {
        return Err(value_err(
            "zip() argument 2 is shorter than argument 1".to_owned(),
        ));
    }
    Ok(OrderedJson::Array(
        result
            .sessions
            .iter()
            .zip(&result.equity)
            .map(|(day, value)| {
                object([
                    ("date", s(iso_date(*day))),
                    ("equity_inr", s(wide_str(value))),
                ])
            })
            .collect(),
    ))
}

/// `_flat` over a float field.
fn f(name: &str, x: f64) -> (String, OrderedJson) {
    (name.to_owned(), jf(x))
}

fn i(name: &str, x: i64) -> (String, OrderedJson) {
    (name.to_owned(), int(x))
}

fn b(name: &str, x: bool) -> (String, OrderedJson) {
    (name.to_owned(), OrderedJson::Bool(x))
}

fn t(name: &str, x: &str) -> (String, OrderedJson) {
    (name.to_owned(), s(x))
}

/// `exits.qulla`, the swing book's `StopConfig`, flattened one level down (LV9).
pub(super) fn qulla_fields(
    prefix: &str,
    q: &baskfy_core::swing::stops::StopConfig,
) -> Vec<(String, OrderedJson)> {
    let k = |name: &str| format!("{prefix}.{name}");
    vec![
        (k("partial_earliest_bar"), int(q.partial_earliest_bar)),
        (k("partial_latest_bar"), int(q.partial_latest_bar)),
        (k("partial_numerator"), int(q.partial_numerator)),
        (k("partial_denominator"), int(q.partial_denominator)),
        (k("breakeven_after_r"), jf(q.breakeven_after_r)),
        (k("fast_trail_min_adr_pct"), jf(q.fast_trail_min_adr_pct)),
        (k("max_stop_distance_pct"), jf(q.max_stop_distance_pct)),
        (k("max_stop_adr_multiple"), jf(q.max_stop_adr_multiple)),
    ]
}

/// The config of `_params_json`, field by field in `VbtConfig`'s declaration order.
fn config_json(c: &VbtConfig) -> OrderedJson {
    let d = &c.data;
    let mut out = vec![
        t("data.instrument_type", &d.instrument_type),
        (
            "data.series_allowed".to_owned(),
            OrderedJson::Array(d.series_allowed.iter().map(s).collect()),
        ),
        b("data.keep_null_series", d.keep_null_series),
        t("data.etf_universe_slug", &d.etf_universe_slug),
        t("data.etf_name_pattern", &d.etf_name_pattern),
        t("data.etf_symbol_pattern", &d.etf_symbol_pattern),
        f("data.thin_session_min_share", d.thin_session_min_share),
        i("data.thin_session_window_bars", d.thin_session_window_bars),
        i("data.thin_session_min_periods", d.thin_session_min_periods),
        f("data.rolling_min_share", d.rolling_min_share),
    ];
    let sc = &c.scan;
    out.extend([
        f("scan.vol_mult", sc.vol_mult),
        i("scan.vol_sma_bars", sc.vol_sma_bars),
        f("scan.min_close_raw_inr", sc.min_close_raw_inr),
        f("scan.min_change_pct", sc.min_change_pct),
        f("scan.min_vol_sma", sc.min_vol_sma),
        f("scan.min_volume", sc.min_volume),
    ]);
    let tr = &c.trend;
    out.extend([
        i("trend.dma_bars", tr.dma_bars),
        i("trend.breakout_high_bars", tr.breakout_high_bars),
        i("trend.ret_bars", tr.ret_bars),
        f("trend.max_ret_20_pct", tr.max_ret_20_pct),
        f("trend.min_close_position", tr.min_close_position),
        f("trend.max_change_pct", tr.max_change_pct),
        i("trend.turnover_bars", tr.turnover_bars),
        f("trend.min_turnover_avg_inr", tr.min_turnover_avg_inr),
        i("breadth.dma_bars", c.breadth.dma_bars),
        f("breadth.min_pct_above_dma", c.breadth.min_pct_above_dma),
        t("entry.limit_at", &c.entry.limit_at),
        i("entry.valid_sessions", c.entry.valid_sessions),
        f("entry.fill_through_pct", c.entry.fill_through_pct),
    ]);
    let z = &c.sizing;
    out.extend([
        i("sizing.max_slots", z.max_slots),
        b("sizing.pyramiding", z.pyramiding),
        i("sizing.max_entries_per_name", z.max_entries_per_name),
        f("sizing.max_position_pct", z.max_position_pct),
        i(
            "sizing.max_new_entries_per_session",
            z.max_new_entries_per_session,
        ),
        f(
            "sizing.max_position_vs_turnover",
            z.max_position_vs_turnover,
        ),
        (
            "sizing.min_trade_value_inr".to_owned(),
            s(super::common::dec_str(z.min_trade_value_inr)),
        ),
        f(
            "sizing.risk_multiplier_first_live",
            z.risk_multiplier_first_live,
        ),
        i("sizing.first_live_sessions", z.first_live_sessions),
    ]);
    let e = &c.exits;
    out.extend([
        f("exits.stop_pct", e.stop_pct),
        f("exits.gtt_limit_fraction", e.gtt_limit_fraction),
        f("exits.gtt_band_min_pct", e.gtt_band_min_pct),
        f("exits.gtt_band_max_pct", e.gtt_band_max_pct),
        i("exits.trail_ema_bars", e.trail_ema_bars),
        i(
            "exits.no_bar_tolerance_sessions",
            e.no_bar_tolerance_sessions,
        ),
        b("exits.qulla_exits", e.qulla_exits),
    ]);
    out.extend(qulla_fields("exits.qulla", &e.qulla));
    out.push(f("costs.cost_pct_per_side", c.costs.cost_pct_per_side));
    OrderedJson::Object(out)
}

/// `_params_json`: `start`, `end`, the money, the cost and the whole config, on the way in.
pub(super) fn params_json(
    params: &BacktestParams,
    start: NaiveDate,
    end: Option<NaiveDate>,
) -> OrderedJson {
    object([
        ("start", s(iso_date(start))),
        ("end", end.map_or(OrderedJson::Null, |e| s(iso_date(e)))),
        ("sleeve_inr", s(wide_str(&params.sleeve_inr))),
        ("cost_pct_per_side", s(wide_str(&params.cost_pct_per_side))),
        ("tick", s(wide_str(&params.tick))),
        ("source", s(SOURCE_PLANT)),
        ("config", config_json(&params.config)),
    ])
}

/// `Drift.to_json()`.
pub(super) fn drift_json(d: &Drift) -> OrderedJson {
    object([
        ("cagr_pct_delta", jf(d.cagr_pct_delta)),
        ("max_dd_pct_delta", jf(d.max_dd_pct_delta)),
        ("trades_delta", int(d.trades_delta)),
        ("flagged", OrderedJson::Bool(d.flagged)),
        ("threshold_cagr_points", jf(d.threshold_cagr_points)),
    ])
}

fn dumps(value: &OrderedJson) -> Result<String, WorkerError> {
    super::common::jsonb_text(value)
}

/// The body between `_open_run` and the stats: everything that can raise.
async fn compute(
    conn: &mut PgConnection,
    ctx: &TaskContext,
    params: &BacktestParams,
    start: NaiveDate,
    end: Option<NaiveDate>,
) -> Result<(OrderedJson, OrderedJson), WorkerError> {
    let config = &params.config;
    let universe = load_universe(
        conn,
        &UniverseRule {
            instrument_type: &config.data.instrument_type,
            series_allowed: &config.data.series_allowed,
            // `tasks/vbt._universe_query` carries no null-series arm.
            keep_null_series: false,
            etf_universe_slug: &config.data.etf_universe_slug,
            etf_name_pattern: &config.data.etf_name_pattern,
            etf_symbol_pattern: &config.data.etf_symbol_pattern,
        },
    )
    .await?;
    let first = start
        .checked_sub_days(Days::new(WARMUP_DAYS))
        .ok_or_else(|| WorkerError::python("OverflowError", "date value out of range"))?;
    let last = end.unwrap_or_else(|| today(ctx));
    let bars = load_raw_bars(conn, first, last, &universe, false).await?;
    if bars.height() == 0 {
        return Err(value_err(
            "the plant holds no bars for the volume-breakout universe in this window; \
             run the backfill before asking for a backtest",
        ));
    }
    let (clean, calendar) = drop_thin_sessions(&bars, config).map_err(core_err)?;
    let indicated = with_vbt_indicators(&clean, &calendar, config).map_err(core_err)?;
    let tagged = with_signal_columns(&indicated, config).map_err(core_err)?;
    let breadth = breadth_series(&tagged, config).map_err(core_err)?;
    let books = three_books(&tagged, &breadth, params)?;
    let Some(primary) = summarise(&books.full).map_err(core_err)? else {
        return Err(value_err(
            "the run produced no closed trades, so there is no result to compare — \
             usually too little history for the 200-session window",
        ));
    };
    let stats = object([
        ("full", book_stats(&books.full)?),
        ("gate_off", book_stats(&books.gate_off)?),
        ("raw_scan", book_stats(&books.raw_scan)?),
        ("universe", int(len_i64(universe.len()))),
        ("sessions", int(len_i64(calendar.sessions.len()))),
        (
            "thin_sessions_dropped",
            OrderedJson::Array(calendar.dropped.iter().map(|d| s(iso_date(*d))).collect()),
        ),
    ]);
    let drift = drift_json(&compare(
        primary.cagr_pct,
        primary.max_drawdown_pct,
        primary.trades,
        None,
        None,
    ));
    Ok((stats, drift))
}

/// `run_vbt_backtest(session, user_id=, start=, end=)` with the defaults the task passes
/// (`sleeve_inr` ₹10 lakh, `DEFAULT_VBT_CONFIG`, `now` the clock). The caller owns the
/// transaction; on an error the row's `error` and `finished_at` are written and the error
/// returned (the caller rolls back, as `run_in_session` does).
pub(super) async fn run_vbt_backtest(
    conn: &mut PgConnection,
    ctx: &TaskContext,
    user_id: i64,
    start: NaiveDate,
    end: Option<NaiveDate>,
) -> Result<VbRun, WorkerError> {
    let stamp = ctx.now_utc();
    let params = BacktestParams {
        sleeve_inr: WideDecimal::from(1_000_000_i64),
        start: Some(start),
        end,
        ..BacktestParams::default()
    };
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO vb_backtest_run (user_id, source, params, started_at) \
         VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
    )
    .bind(user_id)
    .bind(SOURCE_PLANT)
    .bind(dumps(&params_json(&params, start, end))?)
    .bind(stamp)
    .fetch_one(&mut *conn)
    .await?;
    match compute(conn, ctx, &params, start, end).await {
        Ok((stats, drift)) => {
            let finished = ctx.now_utc();
            sqlx::query(
                "UPDATE vb_backtest_run SET stats = $1::jsonb, drift = $2::jsonb, \
                 finished_at = $3 WHERE id = $4",
            )
            .bind(dumps(&stats)?)
            .bind(dumps(&drift)?)
            .bind(finished)
            .bind(id)
            .execute(&mut *conn)
            .await?;
            Ok(VbRun {
                id,
                finished_at: Some(finished),
                drift: Some(drift),
            })
        }
        Err(error) => {
            let text = error_text(&error, "\n", "tasks::sleeve_backtests::vbt::compute");
            sqlx::query("UPDATE vb_backtest_run SET error = $1, finished_at = $2 WHERE id = $3")
                .bind(text)
                .bind(ctx.now_utc())
                .bind(id)
                .execute(&mut *conn)
                .await?;
            crate::steps::log_warning(&format!("vbt backtest failed (run {id}): {error}"));
            Err(error)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `_params_json(BacktestParams(start=2017-10-16), 2017-10-16, None)` as Python writes it
    /// (`json.dumps`), captured from the Python worker.
    const PYTHON_PARAMS: &str = r#"{"start": "2017-10-16", "end": null, "sleeve_inr": "1000000", "cost_pct_per_side": "0.25", "tick": "0.01", "source": "PLANT", "config": {"data.instrument_type": "EQ", "data.series_allowed": ["EQ", "BE", "BZ"], "data.keep_null_series": true, "data.etf_universe_slug": "etf", "data.etf_name_pattern": "\\bETF\\b", "data.etf_symbol_pattern": "(BEES|ETF|IETF)$", "data.thin_session_min_share": 0.25, "data.thin_session_window_bars": 41, "data.thin_session_min_periods": 5, "data.rolling_min_share": 0.9, "scan.vol_mult": 3.0, "scan.vol_sma_bars": 50, "scan.min_close_raw_inr": 30.0, "scan.min_change_pct": 6.5, "scan.min_vol_sma": 25000.0, "scan.min_volume": 50000.0, "trend.dma_bars": 200, "trend.breakout_high_bars": 20, "trend.ret_bars": 20, "trend.max_ret_20_pct": 25.0, "trend.min_close_position": 0.6, "trend.max_change_pct": 15.0, "trend.turnover_bars": 20, "trend.min_turnover_avg_inr": 20000000.0, "breadth.dma_bars": 200, "breadth.min_pct_above_dma": 40.0, "entry.limit_at": "SIGNAL_CLOSE", "entry.valid_sessions": 3, "entry.fill_through_pct": 0.0, "sizing.max_slots": 10, "sizing.pyramiding": true, "sizing.max_entries_per_name": 2, "sizing.max_position_pct": 12.5, "sizing.max_new_entries_per_session": 3, "sizing.max_position_vs_turnover": 0.01, "sizing.min_trade_value_inr": "10000", "sizing.risk_multiplier_first_live": 0.5, "sizing.first_live_sessions": 5, "exits.stop_pct": 12.0, "exits.gtt_limit_fraction": 0.97, "exits.gtt_band_min_pct": 0.005, "exits.gtt_band_max_pct": 0.15, "exits.trail_ema_bars": 21, "exits.no_bar_tolerance_sessions": 5, "exits.qulla_exits": true, "exits.qulla.partial_earliest_bar": 3, "exits.qulla.partial_latest_bar": 5, "exits.qulla.partial_numerator": 1, "exits.qulla.partial_denominator": 3, "exits.qulla.breakeven_after_r": 1.0, "exits.qulla.fast_trail_min_adr_pct": 6.0, "exits.qulla.max_stop_distance_pct": 10.0, "exits.qulla.max_stop_adr_multiple": 1.0, "costs.cost_pct_per_side": 0.25}}"#;

    #[test]
    fn params_are_written_exactly_as_python_writes_them() {
        let params = BacktestParams {
            start: Some(default_start()),
            ..BacktestParams::default()
        };
        let ours = crate::pyjson::dumps(&params_json(&params, default_start(), None));
        assert_eq!(ours.as_deref(), Ok(PYTHON_PARAMS));
    }

    #[test]
    fn the_drift_keeps_pythons_key_order() {
        let d = compare(18.0, -27.0, 100, None, None);
        let text = crate::pyjson::dumps(&drift_json(&d)).unwrap_or_default();
        assert!(text.starts_with("{\"cagr_pct_delta\": "));
        assert!(text.ends_with("\"threshold_cagr_points\": 1.0}"));
    }
}
