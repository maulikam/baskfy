//! Ports `baskfy_worker.tasks.twt_backtest` (TW9): the study, re-run from the plant's own bars,
//! two books over one detection pass, the ETF-denominator counterfactual measured, one appended
//! `tw_backtest_run` row.
//!
//! Python has **no Celery task** for it: `tools/twt/backtest.py` (`make twt-backtest`) calls
//! `run_twt_backtest` inside one `session.begin()`. The Rust body is [`run_twt_backtest`]; the
//! caller owns the transaction exactly as that script does. Nothing here reads `tw_config` — not
//! the capital, not the thresholds (`03` §9): a run that passes nothing is the shipped sleeve.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::py::{float_repr, wide_from_float_str};
use baskfy_core::twt::backtest::{
    BacktestParams, BacktestResult, BacktestStats, gate_vector, panel_from_frame, run_backtest,
    summarise, yearly,
};
use baskfy_core::twt::breadth::breadth_series;
use baskfy_core::twt::calendar::drop_thin_sessions;
use baskfy_core::twt::config::TwtConfig;
use baskfy_core::twt::drift::{Points, compare, drift_cagr_points};
use baskfy_core::twt::indicators::with_twt_indicators;
use baskfy_core::twt::published::published;
use baskfy_core::twt::signals::{signal_mask, with_twt_columns};
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Utc};
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{dec_str, error_text, iso_date, wide_str};
use super::vbt::qulla_fields;
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::sleeves::for_backtests::{
    UniverseRule, core_err, load_raw_bars, load_universe, lookback_start, pl_err, value_err,
};

/// `SOURCE_PLANT`.
pub const SOURCE_PLANT: &str = "PLANT";

/// What a caller may pass `run_twt_backtest` (Python's keywords; `None` takes the default).
#[derive(Debug, Clone, Default)]
pub struct TwtBacktestArgs {
    /// `start` (default `04` §12's `BacktestConfig.start`).
    pub start: Option<NaiveDate>,
    /// `end` (default: the last published session).
    pub end: Option<NaiveDate>,
    /// `sleeve_inr` (default `BacktestConfig.initial_capital_inr`). Python's `sleeve_inr or
    /// default`: a zero sleeve takes the default too.
    pub sleeve_inr: Option<Decimal>,
    /// `measure_etf_denominator` (Python's default `True`).
    pub skip_etf_denominator: bool,
}

/// The appended row, as `tools/twt/backtest.py` reports it.
#[derive(Debug, Clone)]
pub struct TwtRun {
    /// `row.id`.
    pub id: i64,
    /// `row.started_at`.
    pub started_at: DateTime<Utc>,
    /// `row.finished_at`.
    pub finished_at: Option<DateTime<Utc>>,
    /// `row.params`.
    pub params: OrderedJson,
    /// `row.stats`.
    pub stats: Option<OrderedJson>,
    /// `row.drift`.
    pub drift: Option<OrderedJson>,
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `_pct` of a float: `Decimal(str(value))` to two places half-up, `None` when not finite.
fn pct_f(value: f64) -> Result<Option<String>, WorkerError> {
    if !value.is_finite() {
        return Ok(None);
    }
    let exact = wide_from_float_str(value).map_err(|e| value_err(e.0))?;
    pct_w(&exact).map(Some)
}

/// `_pct` of a `Decimal`.
fn pct_w(value: &WideDecimal) -> Result<String, WorkerError> {
    value
        .quantize(-2, Rounding::HalfUp)
        .map(|q| q.to_sci_string())
        .map_err(|e| WorkerError::python("InvalidOperation", e.0))
}

fn put(out: &mut Vec<(String, OrderedJson)>, key: &str, value: Option<String>) {
    out.push((key.to_owned(), value.map_or(OrderedJson::Null, s)));
}

/// `float(decimal)`: the double nearest the decimal's exact value.
fn to_f64(d: &WideDecimal) -> Result<f64, WorkerError> {
    d.to_sci_string()
        .parse::<f64>()
        .map_err(|e| value_err(e.to_string()))
}

/// `_half`: the annualised return of one slice of the stored equity curve.
fn half(
    result: &BacktestResult,
    before: Option<NaiveDate>,
    on_or_after: Option<NaiveDate>,
) -> Result<Option<String>, WorkerError> {
    let zero = WideDecimal::zero();
    let points: Vec<(NaiveDate, &WideDecimal)> = result
        .sessions
        .iter()
        .zip(&result.equity)
        .filter(|(day, value)| {
            **value > zero
                && before.is_none_or(|b| **day < b)
                && on_or_after.is_none_or(|a| **day >= a)
        })
        .map(|(day, value)| (*day, value))
        .collect();
    let (Some(&(first_day, first_value)), Some(&(last_day, last_value))) =
        (points.first(), points.last())
    else {
        return Ok(None);
    };
    if points.len() < 2 {
        return Ok(None);
    }
    let days = (last_day - first_day).num_days();
    if days <= 0 || *first_value <= zero {
        return Ok(None);
    }
    let arith =
        |e: baskfy_core::decimal::DecimalError| WorkerError::python("InvalidOperation", e.0);
    let quarter_days: WideDecimal = "365.25"
        .parse()
        .map_err(|e: baskfy_core::decimal::DecimalError| value_err(e.0))?;
    let years = WideDecimal::from(days).div(&quarter_days).map_err(arith)?;
    let ratio = to_f64(&last_value.div(first_value).map_err(arith)?)?;
    let grown = ratio.powf(1.0 / to_f64(&years)?);
    if !grown.is_finite() {
        // `Decimal(str(inf))` is `Infinity`, which `_pct` answers with `None`.
        return Ok(None);
    }
    let growth = wide_from_float_str(grown)
        .map_err(|e| value_err(e.0))?
        .sub(&WideDecimal::from(1_i64))
        .mul(&WideDecimal::from(100_i64));
    pct_w(&growth).map(Some)
}

/// `_window`: `"2017-10-16 → 2022-12-30"`, or `None` when the run does not reach that half.
fn window(
    sessions: &[NaiveDate],
    before: Option<NaiveDate>,
    on_or_after: Option<NaiveDate>,
) -> Option<String> {
    let inside: Vec<&NaiveDate> = sessions
        .iter()
        .filter(|day| before.is_none_or(|b| **day < b) && on_or_after.is_none_or(|a| **day >= a))
        .collect();
    let (first, last) = (inside.first()?, inside.last()?);
    Some(format!("{} → {}", iso_date(**first), iso_date(**last)))
}

/// `skipped`'s dict literal (`twt.backtest.run_backtest`).
const SKIPPED_ORDER: [&str; 7] = [
    "gate_shut",
    "session_cap",
    "slots_full",
    "no_bar",
    "locked",
    "cash",
    "turnover",
];

/// `ExitReason`'s members, in declaration order (`by_reason` iterates the enum).
const REASON_ORDER: [&str; 6] = [
    "STOP_HIT",
    "STOP_GAP",
    "STOP_DAY0",
    "NO_BAR",
    "END_OF_RUN",
    "MANUAL",
];

/// `two_books`: as specified, and with the gate forced open.
fn two_books(
    tagged: &DataFrame,
    breadth: &DataFrame,
    params: &BacktestParams,
) -> Result<(BacktestResult, BacktestResult), WorkerError> {
    let panel = panel_from_frame(tagged, "signal").map_err(core_err)?;
    let gate = gate_vector(breadth, &panel.sessions).map_err(core_err)?;
    let always_open = vec![true; panel.sessions_count()];
    let full = BacktestParams {
        label: "full".to_owned(),
        ..params.clone()
    };
    let off = BacktestParams {
        label: "gate_off".to_owned(),
        ..params.clone()
    };
    Ok((
        run_backtest(&panel, &gate, &full).map_err(core_err)?,
        run_backtest(&panel, &always_open, &off).map_err(core_err)?,
    ))
}

/// `stats_payload`: every key `05` §3's card reads; a `None` is dropped, never stored as null;
/// `extra` is merged last.
fn stats_payload(
    full: &BacktestResult,
    gate_off: &BacktestResult,
    primary: &BacktestStats,
    config: &TwtConfig,
    extra: Vec<(String, OrderedJson)>,
) -> Result<OrderedJson, WorkerError> {
    let split = Some(config.backtest.is_oos_split);
    let off = summarise(gate_off).map_err(core_err)?;
    let mut p: Vec<(String, OrderedJson)> = Vec::new();
    put(&mut p, "cagr_pct", pct_f(primary.cagr_pct)?);
    put(&mut p, "max_drawdown_pct", pct_f(primary.max_drawdown_pct)?);
    put(
        &mut p,
        "calmar",
        primary.calmar.as_ref().map(pct_w).transpose()?,
    );
    put(&mut p, "sharpe", pct_f(primary.sharpe_ratio)?);
    p.push(("trades".to_owned(), int(primary.trades)));
    put(&mut p, "win_rate_pct", pct_f(primary.win_rate_pct)?);
    put(
        &mut p,
        "profit_factor",
        primary.profit_factor.as_ref().map(pct_w).transpose()?,
    );
    put(
        &mut p,
        "avg_hold_sessions",
        Some(pct_w(&primary.avg_hold_sessions)?),
    );
    put(&mut p, "exposure_pct", pct_f(primary.exposure_pct)?);
    put(&mut p, "in_sample_cagr_pct", half(full, split, None)?);
    put(&mut p, "out_of_sample_cagr_pct", half(full, None, split)?);
    put(
        &mut p,
        "in_sample_window",
        window(&full.sessions, split, None),
    );
    put(
        &mut p,
        "out_of_sample_window",
        window(&full.sessions, None, split),
    );
    match &off {
        None => {
            put(&mut p, "gate_off_cagr_pct", None);
            put(&mut p, "gate_off_max_drawdown_pct", None);
            p.push(("gate_off_trades".to_owned(), int(0)));
        }
        Some(o) => {
            put(&mut p, "gate_off_cagr_pct", pct_f(o.cagr_pct)?);
            put(
                &mut p,
                "gate_off_max_drawdown_pct",
                pct_f(o.max_drawdown_pct)?,
            );
            p.push(("gate_off_trades".to_owned(), int(o.trades)));
        }
    }
    let mut years = Vec::new();
    for row in yearly(full).map_err(core_err)? {
        let mut y = vec![("year".to_owned(), int(i64::from(row.year)))];
        put(&mut y, "return_pct", pct_f(row.return_pct)?);
        y.push(("trades".to_owned(), int(row.trades)));
        put(&mut y, "win_rate_pct", pct_f(row.win_rate_pct)?);
        years.push(OrderedJson::Object(y));
    }
    p.push(("yearly".to_owned(), OrderedJson::Array(years)));
    if full.sessions.len() != full.equity.len() {
        return Err(value_err("zip() argument 2 is shorter than argument 1"));
    }
    p.push((
        "equity_curve".to_owned(),
        OrderedJson::Array(
            full.sessions
                .iter()
                .zip(&full.equity)
                .map(|(day, value)| {
                    object([
                        ("date", s(iso_date(*day))),
                        ("equity_inr", s(wide_str(value))),
                    ])
                })
                .collect(),
        ),
    ));
    put(&mut p, "start", Some(iso_date(primary.start)));
    put(&mut p, "end", Some(iso_date(primary.end)));
    put(&mut p, "years", Some(wide_str(&primary.years)));
    put(
        &mut p,
        "final_equity_inr",
        Some(wide_str(&primary.final_equity_inr)),
    );
    put(
        &mut p,
        "drawdown_peak_on",
        Some(iso_date(primary.drawdown_peak_on)),
    );
    put(
        &mut p,
        "drawdown_trough_on",
        Some(iso_date(primary.drawdown_trough_on)),
    );
    put(&mut p, "avg_win_pct", pct_f(primary.avg_win_pct)?);
    put(&mut p, "avg_loss_pct", pct_f(primary.avg_loss_pct)?);
    put(&mut p, "avg_trade_pct", pct_f(primary.avg_trade_pct)?);
    put(
        &mut p,
        "avg_open_positions",
        Some(wide_str(&primary.avg_open_positions)),
    );
    p.push((
        "by_reason".to_owned(),
        super::common::ordered_counts(&primary.by_reason, &REASON_ORDER),
    ));
    p.push((
        "skipped".to_owned(),
        super::common::ordered_counts(&full.skipped, &SKIPPED_ORDER),
    ));
    p.push((
        "clamped_below_stop".to_owned(),
        int(full.clamped_below_stop),
    ));
    p.retain(|(_, v)| *v != OrderedJson::Null);
    for (key, value) in extra {
        crate::pyjson::update(&mut p, &key, value);
    }
    Ok(OrderedJson::Object(p))
}

fn universe_rule(config: &TwtConfig) -> UniverseRule<'_> {
    UniverseRule {
        instrument_type: &config.data.instrument_type,
        series_allowed: &config.data.series_allowed,
        keep_null_series: config.data.keep_null_series,
        etf_universe_slug: &config.data.etf_universe_slug,
        etf_name_pattern: &config.data.etf_name_pattern,
        etf_symbol_pattern: &config.data.etf_symbol_pattern,
    }
}

/// `excluded_etf_ids`: every instrument `_universe_query` admits that `load_universe` refused.
async fn excluded_etf_ids(
    conn: &mut PgConnection,
    config: &TwtConfig,
    universe: &BTreeSet<i64>,
) -> Result<BTreeSet<i64>, WorkerError> {
    let null_arm = if config.data.keep_null_series {
        " OR series IS NULL"
    } else {
        ""
    };
    let sql = format!(
        "SELECT id FROM instrument WHERE instrument_type = $1 AND (series = ANY($2){null_arm})"
    );
    let rows: Vec<(i64,)> = sqlx::query_as(&sql)
        .bind(&config.data.instrument_type)
        .bind(&config.data.series_allowed)
        .fetch_all(&mut *conn)
        .await?;
    Ok(rows
        .into_iter()
        .map(|r| r.0)
        .filter(|id| !universe.contains(id))
        .collect())
}

fn etf_absent(etfs: usize, bars: i64, delta: OrderedJson, flipped: OrderedJson) -> OrderedJson {
    object([
        ("etf_instruments", int(len_i64(etfs))),
        ("etf_bars", int(bars)),
        ("sessions_compared", int(0)),
        ("max_pct_above_dma_delta", delta),
        ("gate_verdicts_changed", flipped),
    ])
}

/// `etf_denominator_delta`: what counting ETFs in the breadth denominator would do to this run.
async fn etf_denominator_delta(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
    universe: &BTreeSet<i64>,
    config: &TwtConfig,
    baseline: &DataFrame,
    baseline_breadth: &DataFrame,
) -> Result<OrderedJson, WorkerError> {
    let etfs = excluded_etf_ids(conn, config, universe).await?;
    if etfs.is_empty() {
        return Ok(etf_absent(0, 0, OrderedJson::Null, OrderedJson::Null));
    }
    let mut etf_bars = load_raw_bars(conn, start, end, &etfs, true).await?;
    if etf_bars.height() == 0 {
        return Ok(etf_absent(etfs.len(), 0, s("0.0000"), int(0)));
    }
    let etf_height = len_i64(etf_bars.height());
    etf_bars
        .with_column(
            Series::new("is_etf".into(), vec![Some(true); etf_bars.height()]).into_column(),
        )
        .map_err(|e| pl_err(&e))?;
    let names: Vec<String> = baseline
        .get_column_names()
        .iter()
        .map(|n| n.to_string())
        .collect();
    let etf_bars = etf_bars.select(names).map_err(|e| pl_err(&e))?;
    let combined = baseline.vstack(&etf_bars).map_err(|e| pl_err(&e))?;
    let (clean, calendar) = drop_thin_sessions(&combined, config).map_err(core_err)?;
    let indicated = with_twt_indicators(&clean, &calendar, config).map_err(core_err)?;
    let with_etfs = breadth_series(&indicated, config).map_err(core_err)?;
    // `baseline_breadth.join(with_etfs, on="date", how="inner", suffix="_etf")`: one row per
    // session on each side, so the inner join pairs equal dates.
    let base = breadth_by_date(baseline_breadth)?;
    let other = breadth_by_date(&with_etfs)?;
    let mut compared = 0_i64;
    let mut flipped = 0_i64;
    let mut max_delta: Option<f64> = None;
    for (day, rows) in &base {
        let Some(theirs) = other.get(day) else {
            continue;
        };
        for (pct, gate) in rows {
            for (pct_etf, gate_etf) in theirs {
                compared += 1;
                if let (Some(a), Some(b)) = (pct, pct_etf) {
                    let d = (b - a).abs();
                    // polars' max skips NaN unless every value is NaN.
                    max_delta = Some(match max_delta {
                        None => d,
                        Some(m) if m.is_nan() => d,
                        Some(m) if d > m => d,
                        Some(m) => m,
                    });
                }
                if let (Some(g), Some(ge)) = (gate, gate_etf)
                    && g != ge
                {
                    flipped += 1;
                }
            }
        }
    }
    if compared == 0 {
        return Ok(etf_absent(
            etfs.len(),
            etf_height,
            OrderedJson::Null,
            OrderedJson::Null,
        ));
    }
    Ok(object([
        ("etf_instruments", int(len_i64(etfs.len()))),
        ("etf_bars", int(etf_height)),
        ("universe_instruments", int(len_i64(universe.len()))),
        ("sessions_compared", int(compared)),
        (
            "max_pct_above_dma_delta",
            // `str(deltas.max())`: a float's repr, or `"None"` when every pair had a null.
            s(max_delta.map_or_else(|| "None".to_owned(), float_repr)),
        ),
        ("gate_verdicts_changed", int(flipped)),
    ]))
}

type BreadthRows = BTreeMap<Option<i32>, Vec<(Option<f64>, Option<String>)>>;

fn breadth_by_date(frame: &DataFrame) -> Result<BreadthRows, WorkerError> {
    let col = |name: &str| frame.column(name).map_err(|e| pl_err(&e));
    let dates: Vec<Option<i32>> = col("date")?
        .as_materialized_series()
        .date()
        .map_err(|e| pl_err(&e))?
        .physical()
        .iter()
        .collect();
    let pct: Vec<Option<f64>> = col("pct_above_dma")?
        .as_materialized_series()
        .f64()
        .map_err(|e| pl_err(&e))?
        .iter()
        .collect();
    let gate: Vec<Option<String>> = col("gate")?
        .as_materialized_series()
        .str()
        .map_err(|e| pl_err(&e))?
        .iter()
        .map(|g| g.map(str::to_owned))
        .collect();
    let mut out: BreadthRows = BTreeMap::new();
    for ((d, p), g) in dates.into_iter().zip(pct).zip(gate) {
        // A null key never joins in polars' inner join.
        if d.is_some() {
            out.entry(d).or_default().push((p, g));
        }
    }
    Ok(out)
}

/// `_params_json`: `start`, `end`, the money, the costs and the whole config, on the way in.
fn params_json(params: &BacktestParams, start: NaiveDate, end: Option<NaiveDate>) -> OrderedJson {
    object([
        ("start", s(iso_date(start))),
        ("end", end.map_or(OrderedJson::Null, |e| s(iso_date(e)))),
        ("sleeve_inr", s(dec_str(params.sleeve_inr))),
        (
            "cost_bps_per_side",
            s(dec_str(params.config.costs.cost_bps_per_side)),
        ),
        ("cost_pct_per_side", s(dec_str(params.cost_pct_per_side))),
        ("tick", s(dec_str(params.tick))),
        (
            "min_turnover_inr",
            s(dec_str(params.config.entry.min_turnover_inr)),
        ),
        ("source", s(SOURCE_PLANT)),
        ("config", config_json(&params.config)),
    ])
}

/// A `StrEnum` field's `.value`, as serde spells it.
fn enum_value<T: serde::Serialize>(value: &T) -> OrderedJson {
    match serde_json::to_value(value) {
        Ok(serde_json::Value::String(text)) => s(text),
        _ => OrderedJson::Null,
    }
}

/// The config of `_params_json`, field by field in `TwtConfig`'s declaration order.
fn config_json(c: &TwtConfig) -> OrderedJson {
    let d = |x: Decimal| s(dec_str(x));
    let mut o: Vec<(String, OrderedJson)> = Vec::new();
    let mut add = |k: &str, v: OrderedJson| o.push((k.to_owned(), v));
    let a = &c.data;
    add("data.instrument_type", s(&a.instrument_type));
    add(
        "data.series_allowed",
        OrderedJson::Array(a.series_allowed.iter().map(s).collect()),
    );
    add(
        "data.keep_null_series",
        OrderedJson::Bool(a.keep_null_series),
    );
    add("data.etf_universe_slug", s(&a.etf_universe_slug));
    add("data.etf_name_pattern", s(&a.etf_name_pattern));
    add("data.etf_symbol_pattern", s(&a.etf_symbol_pattern));
    add(
        "data.thin_session_min_share",
        OrderedJson::Float(a.thin_session_min_share),
    );
    add(
        "data.thin_session_window_bars",
        int(a.thin_session_window_bars),
    );
    add(
        "data.thin_session_min_periods",
        int(a.thin_session_min_periods),
    );
    add(
        "data.rolling_min_share",
        OrderedJson::Float(a.rolling_min_share),
    );
    add("data.bars_required", int(a.bars_required));
    let sc = &c.scan;
    add("scan.min_close_raw", d(sc.min_close_raw));
    add("scan.month_low_multiple", d(sc.month_low_multiple));
    add("scan.month_low_months_back", int(sc.month_low_months_back));
    add("scan.tight_band_pct", d(sc.tight_band_pct));
    add("scan.tight_weeks", int(sc.tight_weeks));
    add("scan.min_vol_sma", d(sc.min_vol_sma));
    add("scan.vol_sma_bars", int(sc.vol_sma_bars));
    add("breadth.dma_bars", int(c.breadth.dma_bars));
    add(
        "breadth.min_pct_above_dma",
        OrderedJson::Float(c.breadth.min_pct_above_dma),
    );
    add("breadth.pct_decimals", int(c.breadth.pct_decimals));
    let e = &c.entry;
    add("entry.entry", enum_value(&e.entry));
    add(
        "entry.entry_min_sessions_out",
        int(e.entry_min_sessions_out),
    );
    add("entry.min_turnover_inr", d(e.min_turnover_inr));
    add(
        "entry.research_min_turnover_inr",
        d(e.research_min_turnover_inr),
    );
    add("entry.turnover_avg_bars", int(e.turnover_avg_bars));
    add("entry.rank_key", enum_value(&e.rank_key));
    let z = &c.sizing;
    add("sizing.max_slots", int(z.max_slots));
    add("sizing.pyramiding", OrderedJson::Bool(z.pyramiding));
    add("sizing.max_entries_per_name", int(z.max_entries_per_name));
    add("sizing.max_position_pct", d(z.max_position_pct));
    add(
        "sizing.max_position_vs_turnover",
        d(z.max_position_vs_turnover),
    );
    add("sizing.min_trade_value_inr", d(z.min_trade_value_inr));
    add(
        "sizing.max_new_entries_per_session",
        int(z.max_new_entries_per_session),
    );
    add(
        "sizing.risk_multiplier_first_live",
        d(z.risk_multiplier_first_live),
    );
    add("sizing.first_live_entries", int(z.first_live_entries));
    let x = &c.exits;
    add("exits.stop_pct", d(x.stop_pct));
    add("exits.trail_pct", d(x.trail_pct));
    add("exits.close_clamp_fraction", d(x.close_clamp_fraction));
    add("exits.close_clamp_fallback", d(x.close_clamp_fallback));
    add("exits.no_bar_sessions", int(x.no_bar_sessions));
    add("exits.gtt_limit_fraction", d(x.gtt_limit_fraction));
    add("exits.gtt_band_min_pct", d(x.gtt_band_min_pct));
    add("exits.gtt_band_max_pct", d(x.gtt_band_max_pct));
    add("exits.qulla_exits", OrderedJson::Bool(x.qulla_exits));
    for (k, v) in qulla_fields("exits.qulla", &x.qulla) {
        add(&k, v);
    }
    add("costs.cost_bps_per_side", d(c.costs.cost_bps_per_side));
    add(
        "backtest.initial_capital_inr",
        d(c.backtest.initial_capital_inr),
    );
    add("backtest.start", s(iso_date(c.backtest.start)));
    add(
        "backtest.is_oos_split",
        s(iso_date(c.backtest.is_oos_split)),
    );
    OrderedJson::Object(o)
}

/// `Drift.to_json()` (every number a decimal string).
fn drift_json(primary: &BacktestStats) -> Result<OrderedJson, WorkerError> {
    let drift = compare(
        Points::Float(primary.cagr_pct),
        Points::Float(primary.max_drawdown_pct),
        primary.trades,
        &published(),
        drift_cagr_points(),
    )
    .map_err(core_err)?
    .to_json();
    Ok(object([
        ("cagr_pct_delta", s(drift.cagr_pct_delta)),
        ("max_dd_pct_delta", s(drift.max_dd_pct_delta)),
        ("trades_delta", int(drift.trades_delta)),
        ("flagged", OrderedJson::Bool(drift.flagged)),
        ("published_cagr_pct", s(drift.published_cagr_pct)),
        ("run_cagr_pct", s(drift.run_cagr_pct)),
        ("threshold_cagr_points", s(drift.threshold_cagr_points)),
    ]))
}

fn dumps(value: &OrderedJson) -> Result<String, WorkerError> {
    super::common::jsonb_text(value)
}

/// `last_published_session`: the newest date `ohlcv_daily` holds.
async fn last_published_session(conn: &mut PgConnection) -> Result<Option<NaiveDate>, WorkerError> {
    let row: Option<(NaiveDate,)> =
        sqlx::query_as("SELECT date FROM ohlcv_daily ORDER BY date DESC LIMIT 1")
            .fetch_optional(&mut *conn)
            .await?;
    Ok(row.map(|r| r.0))
}

async fn compute(
    conn: &mut PgConnection,
    params: &BacktestParams,
    first: NaiveDate,
    end: Option<NaiveDate>,
    measure_etf: bool,
) -> Result<(OrderedJson, OrderedJson), WorkerError> {
    let config = &params.config;
    let last = match end {
        Some(day) => day,
        None => last_published_session(conn).await?.ok_or_else(|| {
            value_err(
                "the plant holds no bars at all, so there is no last published session to run \
                 to; run the backfill before asking for a backtest",
            )
        })?,
    };
    let universe = load_universe(conn, &universe_rule(config)).await?;
    // `LOOKBACK_SESSIONS = DEFAULT_TWT_CONFIG.data.bars_required`.
    let warmup = lookback_start(conn, first, TwtConfig::default().data.bars_required).await?;
    let bars = load_raw_bars(conn, warmup, last, &universe, true).await?;
    if bars.height() == 0 {
        return Err(value_err(
            "the plant holds no bars for the three-weeks-tight universe in this window; \
             run the backfill before asking for a backtest",
        ));
    }
    let (clean, calendar) = drop_thin_sessions(&bars, config).map_err(core_err)?;
    let detected = with_twt_columns(&clean, &calendar, config).map_err(core_err)?;
    let mask = signal_mask(&detected, config, None).map_err(core_err)?;
    let mut tagged = detected.clone();
    tagged
        .with_column(mask.with_name("signal".into()).into_column())
        .map_err(|e| pl_err(&e))?;
    let breadth = breadth_series(&detected, config).map_err(core_err)?;
    let (full, gate_off) = two_books(&tagged, &breadth, params)?;
    let Some(primary) = summarise(&full).map_err(core_err)? else {
        return Err(value_err(
            "the run produced no equity curve, so there is no result to compare — usually \
             too little history for the 260-session window `04` §2.3 asks for",
        ));
    };
    let etf = if measure_etf {
        etf_denominator_delta(conn, warmup, last, &universe, config, &bars, &breadth).await?
    } else {
        OrderedJson::Object(Vec::new())
    };
    let stats = stats_payload(
        &full,
        &gate_off,
        &primary,
        config,
        vec![
            ("universe".to_owned(), int(len_i64(universe.len()))),
            ("sessions".to_owned(), int(len_i64(calendar.sessions.len()))),
            (
                "thin_sessions_dropped".to_owned(),
                OrderedJson::Array(calendar.dropped.iter().map(|d| s(iso_date(*d))).collect()),
            ),
            ("etf_denominator".to_owned(), etf),
        ],
    )?;
    Ok((stats, drift_json(&primary)?))
}

/// Ports `run_twt_backtest(session, user_id=, start=, end=, sleeve_inr=, now=,
/// measure_etf_denominator=)` with `DEFAULT_TWT_CONFIG`. Inserts the row (`params`,
/// `started_at = now`), runs, and finishes it (`stats`, `drift`, `finished_at`); on an error the
/// row gets `error` and `finished_at` and the error is returned — the caller's transaction
/// decides whether that survives (`tools/twt/backtest.py` rolls it back).
///
/// # Errors
/// Python's: `ValueError` with no bars or no equity curve, the core's, the database's.
pub async fn run_twt_backtest(
    conn: &mut PgConnection,
    clock: &dyn Fn() -> DateTime<Utc>,
    user_id: i64,
    args: &TwtBacktestArgs,
) -> Result<TwtRun, WorkerError> {
    let stamp = clock();
    let config = TwtConfig::default();
    let first = args.start.unwrap_or(config.backtest.start);
    let sleeve = args
        .sleeve_inr
        .filter(|v| !v.is_zero())
        .unwrap_or(config.backtest.initial_capital_inr);
    let params = BacktestParams {
        sleeve_inr: sleeve,
        start: Some(first),
        end: args.end,
        config,
        ..BacktestParams::default()
    };
    let params_value = params_json(&params, first, args.end);
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO tw_backtest_run (user_id, source, params, started_at) \
         VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
    )
    .bind(user_id)
    .bind(SOURCE_PLANT)
    .bind(dumps(&params_value)?)
    .bind(stamp)
    .fetch_one(&mut *conn)
    .await?;
    match compute(conn, &params, first, args.end, !args.skip_etf_denominator).await {
        Ok((stats, drift)) => {
            let finished = clock();
            sqlx::query(
                "UPDATE tw_backtest_run SET stats = $1::jsonb, drift = $2::jsonb, \
                 finished_at = $3 WHERE id = $4",
            )
            .bind(dumps(&stats)?)
            .bind(dumps(&drift)?)
            .bind(finished)
            .bind(id)
            .execute(&mut *conn)
            .await?;
            Ok(TwtRun {
                id,
                started_at: stamp,
                finished_at: Some(finished),
                params: params_value,
                stats: Some(stats),
                drift: Some(drift),
            })
        }
        Err(error) => {
            let text = error_text(&error, "\n", "tasks::sleeve_backtests::twt::compute");
            sqlx::query("UPDATE tw_backtest_run SET error = $1, finished_at = $2 WHERE id = $3")
                .bind(text)
                .bind(clock())
                .bind(id)
                .execute(&mut *conn)
                .await?;
            crate::steps::log_warning(&format!("twt backtest failed (run {id}): {error}"));
            Err(error)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `_params_json` for the shipped sleeve with nothing passed, as Python writes it.
    const PYTHON_PARAMS: &str = r#"{"start": "2017-10-16", "end": null, "sleeve_inr": "1000000", "cost_bps_per_side": "25.0", "cost_pct_per_side": "0.25", "tick": "0.05", "min_turnover_inr": "20000000", "source": "PLANT", "config": {"data.instrument_type": "EQ", "data.series_allowed": ["EQ", "BE", "BZ"], "data.keep_null_series": true, "data.etf_universe_slug": "etf", "data.etf_name_pattern": "\\bETF\\b", "data.etf_symbol_pattern": "(BEES|ETF|IETF)$", "data.thin_session_min_share": 0.25, "data.thin_session_window_bars": 41, "data.thin_session_min_periods": 5, "data.rolling_min_share": 0.9, "data.bars_required": 260, "scan.min_close_raw": "30.0", "scan.month_low_multiple": "1.3", "scan.month_low_months_back": 3, "scan.tight_band_pct": "3.01", "scan.tight_weeks": 3, "scan.min_vol_sma": "10000.0", "scan.vol_sma_bars": 50, "breadth.dma_bars": 200, "breadth.min_pct_above_dma": 40.0, "breadth.pct_decimals": 4, "entry.entry": "NEXT_OPEN", "entry.entry_min_sessions_out": 5, "entry.min_turnover_inr": "20000000", "entry.research_min_turnover_inr": "20000000", "entry.turnover_avg_bars": 20, "entry.rank_key": "SIGNAL_TURNOVER", "sizing.max_slots": 10, "sizing.pyramiding": true, "sizing.max_entries_per_name": 2, "sizing.max_position_pct": "12.5", "sizing.max_position_vs_turnover": "0.01", "sizing.min_trade_value_inr": "10000", "sizing.max_new_entries_per_session": 3, "sizing.risk_multiplier_first_live": "0.5", "sizing.first_live_entries": 10, "exits.stop_pct": "20.0", "exits.trail_pct": "20.0", "exits.close_clamp_fraction": "0.9999", "exits.close_clamp_fallback": "0.999", "exits.no_bar_sessions": 5, "exits.gtt_limit_fraction": "0.97", "exits.gtt_band_min_pct": "0.005", "exits.gtt_band_max_pct": "0.30", "exits.qulla_exits": true, "exits.qulla.partial_earliest_bar": 3, "exits.qulla.partial_latest_bar": 5, "exits.qulla.partial_numerator": 1, "exits.qulla.partial_denominator": 3, "exits.qulla.breakeven_after_r": 1.0, "exits.qulla.fast_trail_min_adr_pct": 6.0, "exits.qulla.max_stop_distance_pct": 10.0, "exits.qulla.max_stop_adr_multiple": 1.0, "costs.cost_bps_per_side": "25.0", "backtest.initial_capital_inr": "1000000", "backtest.start": "2017-10-16", "backtest.is_oos_split": "2023-01-01"}}"#;

    #[test]
    fn params_are_written_exactly_as_python_writes_them() {
        let config = TwtConfig::default();
        let first = config.backtest.start;
        let params = BacktestParams {
            sleeve_inr: config.backtest.initial_capital_inr,
            start: Some(first),
            config,
            ..BacktestParams::default()
        };
        let ours = crate::pyjson::dumps(&params_json(&params, first, None));
        assert_eq!(ours.as_deref(), Ok(PYTHON_PARAMS));
    }

    #[test]
    fn pct_is_half_up_on_the_float_repr() {
        assert_eq!(pct_f(12.345).ok().flatten().as_deref(), Some("12.35"));
        assert_eq!(pct_f(-0.005).ok().flatten().as_deref(), Some("-0.01"));
        assert_eq!(pct_f(f64::NAN).ok().flatten(), None);
        assert_eq!(pct_f(100.0).ok().flatten().as_deref(), Some("100.00"));
    }

    /// `test_the_drift_json_is_the_shape_the_card_reads` (and the threshold, both ways).
    #[test]
    fn the_drift_is_the_shape_the_card_reads_and_flags_both_ways() {
        let primary = |cagr: f64| BacktestStats {
            start: NaiveDate::default(),
            end: NaiveDate::default(),
            years: WideDecimal::from(1_i64),
            final_equity_inr: WideDecimal::from(1_i64),
            cagr_pct: cagr,
            max_drawdown_pct: -30.0,
            drawdown_peak_on: NaiveDate::default(),
            drawdown_trough_on: NaiveDate::default(),
            calmar: None,
            sharpe_ratio: 0.0,
            trades: 150,
            win_rate_pct: 0.0,
            profit_factor: None,
            avg_win_pct: 0.0,
            avg_loss_pct: 0.0,
            avg_trade_pct: 0.0,
            avg_hold_sessions: WideDecimal::zero(),
            avg_open_positions: WideDecimal::zero(),
            exposure_pct: 0.0,
            by_reason: BTreeMap::new(),
        };
        let text = drift_json(&primary(16.6))
            .ok()
            .and_then(|j| crate::pyjson::dumps(&j).ok())
            .unwrap_or_default();
        assert!(text.contains("\"run_cagr_pct\": \"16.60\""), "{text}");
        assert!(text.contains("\"published_cagr_pct\": \"20.92\""), "{text}");
        assert!(text.contains("\"cagr_pct_delta\": \"-4.32\""), "{text}");
        assert!(text.contains("\"flagged\": true"), "{text}");
        let published = published().cagr_pct;
        for (offset, flagged) in [(1.0, false), (-1.0, false), (1.5, true), (-1.5, true)] {
            let json = drift_json(&primary(published + offset)).ok();
            assert_eq!(
                json.as_ref().and_then(|j| j.get("flagged")).cloned(),
                Some(OrderedJson::Bool(flagged)),
                "{offset}"
            );
        }
    }

    /// `test_the_etf_counterfactual_is_on_by_default` and
    /// `test_the_sleeve_inr_default_is_the_studys_capital_and_not_a_live_one`.
    #[test]
    fn the_defaults_are_the_studys() {
        let args = TwtBacktestArgs::default();
        assert!(!args.skip_etf_denominator);
        assert_eq!(args.sleeve_inr, None);
        assert_eq!(
            TwtConfig::default().backtest.initial_capital_inr,
            Decimal::from(1_000_000_i64)
        );
    }

    /// `test_no_statement_of_the_run_touches_tw_config_at_all`, as a scan of this module's SQL:
    /// the live sleeve's capital and thresholds never reach a backtest.
    #[test]
    fn no_statement_reads_tw_config() {
        let source = include_str!("twt.rs");
        let sql: String = source
            .lines()
            .filter(|l| l.contains("SELECT") || l.contains("FROM") || l.contains("JOIN"))
            .collect();
        assert!(!sql.contains("tw_config"));
    }

    /// `test_a_missing_number_is_an_absent_key_and_never_a_json_null`: a book with no losing
    /// trade, no calmar and no in-sample half stores those keys absent.
    #[test]
    fn a_missing_number_is_an_absent_key() {
        let day = |d: u32| NaiveDate::from_ymd_opt(2024, 3, d).unwrap_or_default();
        let config = TwtConfig::default();
        let result = BacktestResult {
            params: BacktestParams::default(),
            trades: Vec::new(),
            sessions: vec![day(1), day(4), day(5)],
            equity: vec![WideDecimal::from(1_000_000_i64); 3],
            open_positions: vec![0; 3],
            skipped: BTreeMap::new(),
            clamped_below_stop: 0,
        };
        let primary = summarise(&result).ok().flatten();
        let Some(primary) = primary else {
            return;
        };
        let payload = stats_payload(&result, &result, &primary, &config, Vec::new()).ok();
        assert!(matches!(payload, Some(OrderedJson::Object(_))));
        let Some(OrderedJson::Object(pairs)) = payload else {
            return;
        };
        assert!(pairs.iter().all(|(_, v)| *v != OrderedJson::Null));
        assert!(!pairs.iter().any(|(k, _)| k == "in_sample_cagr_pct"));
        assert!(pairs.iter().any(|(k, _)| k == "out_of_sample_window"));
    }

    #[test]
    fn window_is_none_outside_the_run() {
        let days: Vec<NaiveDate> = [(2022, 12, 30), (2023, 1, 2)]
            .iter()
            .filter_map(|&(y, m, d)| NaiveDate::from_ymd_opt(y, m, d))
            .collect();
        let split = NaiveDate::from_ymd_opt(2023, 1, 1);
        assert_eq!(
            window(&days, split, None).as_deref(),
            Some("2022-12-30 → 2022-12-30")
        );
        assert_eq!(window(&days[..1], None, split), None);
    }
}
