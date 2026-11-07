//! Regime A/B for the TWT and VBT backtests: `legacy` is the sleeve's own run (breadth gate,
//! whole universe); `index_led` adds the leading-index gate per session and keeps only signals
//! in that session's universe. Pure reads — no `*_backtest_run` row, no plan, no order.
//! Constituents are today's lists, so the index-led book carries survivorship bias and says so.

use std::collections::{BTreeSet, HashMap};

use baskfy_core::decimal::WideDecimal;
use baskfy_core::regime::{RegimeConfig, scan, universe};
use chrono::{Days, NaiveDate};
use polars::prelude::IntoColumn;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::regime;
use crate::steps::sleeves::for_backtests::{
    UniverseRule, core_err, load_raw_bars, load_universe, lookback_start, pl_err, value_err,
};

/// The label the index-led rows carry.
pub const SURVIVORSHIP: &str = "constituents=current (survivorship bias)";

/// One line of the table.
#[derive(Debug, Clone)]
pub struct RegimeRow {
    /// `TWT` / `VBT`.
    pub strategy: &'static str,
    /// `legacy` / `index_led`.
    pub mode: &'static str,
    /// Closed trades.
    pub trades: i64,
    /// Winners, percent.
    pub win_pct: f64,
    /// Mean R over trades with one.
    pub avg_r: Option<f64>,
    /// CAGR, percent.
    pub cagr_pct: f64,
    /// Max drawdown, percent.
    pub max_dd_pct: f64,
}

type Sessions = HashMap<NaiveDate, (bool, BTreeSet<String>)>;

fn mean_r<'a>(rs: impl Iterator<Item = Option<&'a WideDecimal>>) -> Option<f64> {
    let v: Vec<f64> = rs
        .flatten()
        .filter_map(|d| d.to_string().parse::<f64>().ok())
        .collect();
    if v.is_empty() {
        return None;
    }
    #[allow(clippy::cast_precision_loss)]
    let n = v.len() as f64;
    Some(v.iter().sum::<f64>() / n)
}

/// Per session: is the index-led gate open, and which symbols are in its universe.
async fn sessions_map(
    conn: &mut PgConnection,
    cfg: &RegimeConfig,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<(Sessions, usize), WorkerError> {
    if !regime::index_tables_ready(conn).await? {
        return Err(value_err("index_bar_daily (0060) is not migrated"));
    }
    let from = start
        .checked_sub_days(Days::new(90))
        .ok_or_else(|| value_err("date out of range"))?;
    let (series, bench) = regime::load_series(conn, cfg, from, end).await?;
    let names: Vec<String> = series.iter().map(|s| s.name.clone()).collect();
    let constituents = regime::load_constituents(conn, &names).await?;
    let mut days: BTreeSet<NaiveDate> = BTreeSet::new();
    for s in &series {
        days.extend(s.closes.iter().map(|(d, _)| *d).filter(|d| *d >= start));
    }
    let mut out = HashMap::new();
    for d in &days {
        let v = scan(&series, bench.as_ref(), Some(*d), cfg.min_leading);
        let members: BTreeSet<String> = universe(&v.leading, &constituents).into_keys().collect();
        out.insert(*d, (v.gate_open, members));
    }
    let covered = days.len();
    Ok((out, covered))
}

/// `signal[r][c] &= symbol ∈ universe(c)`; `gate[c] &= open(c)`. A session the index bars do
/// not cover is shut (no look-ahead, no guess).
fn apply(
    symbols: &[String],
    sessions: &[NaiveDate],
    signal: &mut [Vec<bool>],
    gate: &mut [bool],
    map: &Sessions,
) {
    for (c, d) in sessions.iter().enumerate() {
        let (open, members) = map.get(d).map_or((false, None), |(o, m)| (*o, Some(m)));
        if let Some(g) = gate.get_mut(c) {
            *g = *g && open;
        }
        for (r, sym) in symbols.iter().enumerate() {
            if let Some(cell) = signal.get_mut(r).and_then(|row| row.get_mut(c)) {
                *cell = *cell && members.is_some_and(|m| m.contains(sym));
            }
        }
    }
}

async fn twt_rows(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
    map: &Sessions,
) -> Result<Vec<RegimeRow>, WorkerError> {
    use baskfy_core::twt::backtest::{
        BacktestParams, BacktestResult, gate_vector, panel_from_frame, run_backtest, summarise,
    };
    use baskfy_core::twt::config::TwtConfig;
    let config = TwtConfig::default();
    let universe = load_universe(conn, &super::twt::universe_rule(&config)).await?;
    let warmup = lookback_start(conn, start, config.data.bars_required).await?;
    let bars = load_raw_bars(conn, warmup, end, &universe, true).await?;
    if bars.height() == 0 {
        return Err(value_err("no bars for the TWT universe in this window"));
    }
    let (clean, calendar) =
        baskfy_core::twt::calendar::drop_thin_sessions(&bars, &config).map_err(core_err)?;
    let detected = baskfy_core::twt::signals::with_twt_columns(&clean, &calendar, &config)
        .map_err(core_err)?;
    let mask = baskfy_core::twt::signals::signal_mask(&detected, &config, None).map_err(core_err)?;
    let mut tagged = detected.clone();
    tagged
        .with_column(mask.with_name("signal".into()).into_column())
        .map_err(|e| pl_err(&e))?;
    let breadth =
        baskfy_core::twt::breadth::breadth_series(&detected, &config).map_err(core_err)?;
    let panel = panel_from_frame(&tagged, "signal").map_err(core_err)?;
    let gate = gate_vector(&breadth, &panel.sessions).map_err(core_err)?;
    let params = BacktestParams {
        sleeve_inr: config.backtest.initial_capital_inr,
        start: Some(start),
        end: Some(end),
        config: config.clone(),
        ..BacktestParams::default()
    };
    let row = |mode: &'static str, r: &BacktestResult| -> Result<RegimeRow, WorkerError> {
        let s = summarise(r).map_err(core_err)?;
        Ok(RegimeRow {
            strategy: "TWT",
            mode,
            trades: s.as_ref().map_or(0, |s| s.trades),
            win_pct: s.as_ref().map_or(0.0, |s| s.win_rate_pct),
            avg_r: mean_r(r.trades.iter().map(|t| t.r_multiple.as_ref())),
            cagr_pct: s.as_ref().map_or(0.0, |s| s.cagr_pct),
            max_dd_pct: s.as_ref().map_or(0.0, |s| s.max_drawdown_pct),
        })
    };
    let legacy = run_backtest(&panel, &gate, &params).map_err(core_err)?;
    let mut led_panel = panel.clone();
    let mut led_gate = gate.clone();
    apply(
        &led_panel.symbols.clone(),
        &led_panel.sessions.clone(),
        &mut led_panel.signal,
        &mut led_gate,
        map,
    );
    let led = run_backtest(&led_panel, &led_gate, &params).map_err(core_err)?;
    Ok(vec![row("legacy", &legacy)?, row("index_led", &led)?])
}

async fn vbt_rows(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
    map: &Sessions,
) -> Result<Vec<RegimeRow>, WorkerError> {
    use baskfy_core::vbt::backtest::{
        BacktestParams, BacktestResult, gate_vector, panel_from_frame, run_backtest, summarise,
    };
    use baskfy_core::vbt::config::VbtConfig;
    let config = VbtConfig::default();
    let universe = load_universe(
        conn,
        &UniverseRule {
            instrument_type: &config.data.instrument_type,
            series_allowed: &config.data.series_allowed,
            keep_null_series: false,
            etf_universe_slug: &config.data.etf_universe_slug,
            etf_name_pattern: &config.data.etf_name_pattern,
            etf_symbol_pattern: &config.data.etf_symbol_pattern,
        },
    )
    .await?;
    let first = start
        .checked_sub_days(Days::new(super::vbt::WARMUP_DAYS))
        .ok_or_else(|| value_err("date out of range"))?;
    let bars = load_raw_bars(conn, first, end, &universe, false).await?;
    if bars.height() == 0 {
        return Err(value_err("no bars for the VBT universe in this window"));
    }
    let (clean, calendar) =
        baskfy_core::vbt::calendar::drop_thin_sessions(&bars, &config).map_err(core_err)?;
    let indicated = baskfy_core::vbt::indicators::with_vbt_indicators(&clean, &calendar, &config)
        .map_err(core_err)?;
    let tagged =
        baskfy_core::vbt::signals::with_signal_columns(&indicated, &config).map_err(core_err)?;
    let breadth = baskfy_core::vbt::breadth::breadth_series(&tagged, &config).map_err(core_err)?;
    let panel = panel_from_frame(&tagged, "state").map_err(core_err)?;
    let gate = gate_vector(&breadth, &panel.sessions).map_err(core_err)?;
    let params = BacktestParams {
        sleeve_inr: WideDecimal::from(1_000_000_i64),
        start: Some(start),
        end: Some(end),
        ..BacktestParams::default()
    };
    let row = |mode: &'static str, r: &BacktestResult| -> Result<RegimeRow, WorkerError> {
        let s = summarise(r).map_err(core_err)?;
        Ok(RegimeRow {
            strategy: "VBT",
            mode,
            trades: s.as_ref().map_or(0, |s| s.trades),
            win_pct: s.as_ref().map_or(0.0, |s| s.win_rate_pct),
            avg_r: mean_r(r.trades.iter().map(|t| t.r_multiple.as_ref())),
            cagr_pct: s.as_ref().map_or(0.0, |s| s.cagr_pct),
            max_dd_pct: s.as_ref().map_or(0.0, |s| s.max_drawdown_pct),
        })
    };
    let legacy = run_backtest(&panel, &gate, &params).map_err(core_err)?;
    let mut led_panel = panel.clone();
    let mut led_gate = gate.clone();
    apply(
        &led_panel.symbols.clone(),
        &led_panel.sessions.clone(),
        &mut led_panel.signal,
        &mut led_gate,
        map,
    );
    let led = run_backtest(&led_panel, &led_gate, &params).map_err(core_err)?;
    Ok(vec![row("legacy", &legacy)?, row("index_led", &led)?])
}

/// The A/B over `[start, end]`, and how many sessions the index bars covered.
///
/// # Errors
/// No index tables, no bars, the core's, the database's.
pub async fn regime_backtest(
    conn: &mut PgConnection,
    cfg: &RegimeConfig,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<(Vec<RegimeRow>, usize), WorkerError> {
    let (map, covered) = sessions_map(conn, cfg, start, end).await?;
    let mut rows = twt_rows(conn, start, end, &map).await?;
    rows.extend(vbt_rows(conn, start, end, &map).await?);
    Ok((rows, covered))
}

/// The table as text.
#[must_use]
pub fn render(
    rows: &[RegimeRow],
    start: NaiveDate,
    end: NaiveDate,
    covered: usize,
    cfg: &RegimeConfig,
) -> String {
    let mut out = format!(
        "regime backtest {start} .. {end}  (index sessions covered: {covered}; min_leading={}; \
         groups={}; index_led: {SURVIVORSHIP})\n",
        cfg.min_leading,
        cfg.groups.join("|")
    );
    out.push_str(&format!(
        "{:<8} {:<10} {:>7} {:>7} {:>7} {:>8} {:>8}\n",
        "strategy", "mode", "trades", "win%", "avg R", "CAGR%", "maxDD%"
    ));
    for r in rows {
        out.push_str(&format!(
            "{:<8} {:<10} {:>7} {:>7.1} {:>7} {:>8.2} {:>8.2}\n",
            r.strategy,
            r.mode,
            r.trades,
            r.win_pct,
            r.avg_r.map_or_else(|| "-".to_owned(), |x| format!("{x:.2}")),
            r.cagr_pct,
            r.max_dd_pct
        ));
    }
    out
}
