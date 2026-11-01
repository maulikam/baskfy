//! `app/analytics/performance_view.py` (the stored track record, every metric flagged against
//! its minimum history) and `main._holdings_block` (the live holdings table, best-effort).

use std::collections::HashMap;
use std::fmt::Write as _;

use chrono::NaiveDate;

use super::benchmark::{self as bm, TRI};
use super::metrics::{self as m, Series, Trade};
use crate::app::AppState;
use crate::lanes::weekly::weekly_books::holdings_view as hv;
use crate::lanes::weekly::weekly_books::snapshot_db;
use crate::storage::{Conn, DbValue};
use crate::wire::{PyDict, PyValue, dict};

/// Below these counts a metric is arithmetic rather than evidence.
pub const MIN_DAYS: [(&str, usize); 6] = [
    ("return", 5),
    ("volatility", 21),
    ("ratio", 63),
    ("drawdown", 21),
    ("monthly", 2),
    ("relative", 21),
];
/// The benchmarks drawn, in order (the first with data is the relative-metrics primary).
pub const BENCHMARKS: [&str; 3] = ["NIFTY 500", "NIFTY200MOMENTM50", "NIFTY200MOMENTM30"];

/// `_flag(n, kind)`.
#[must_use]
pub fn flag(n: usize, kind: &str) -> PyValue {
    let need = MIN_DAYS
        .iter()
        .find(|(k, _)| *k == kind)
        .map_or(0, |(_, v)| *v);
    PyValue::Map(dict([
        ("n", int(n)),
        ("need", int(need)),
        ("meaningful", PyValue::Bool(n >= need)),
    ]))
}

fn int(n: usize) -> PyValue {
    PyValue::Int(i64::try_from(n).unwrap_or(i64::MAX))
}

fn f(x: f64) -> PyValue {
    PyValue::Float(x)
}

fn s(x: &str) -> PyValue {
    PyValue::Str(x.to_owned())
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn fmt2(x: f64) -> String {
    crate::pyfmt::format_value(&crate::pyfmt::PyArg::Float(x), ".2f")
        .unwrap_or_else(|_| format!("{x:.2}"))
}

/// `_polyline(series, width, height, lo=, hi=)`: inline-SVG points.
#[must_use]
#[allow(clippy::cast_precision_loss, clippy::float_cmp)]
pub fn polyline(
    values: &[f64],
    width: f64,
    height: f64,
    lo: Option<f64>,
    hi: Option<f64>,
) -> String {
    if values.len() < 2 {
        return String::new();
    }
    let finite = || values.iter().copied().filter(|x| !x.is_nan());
    let lo = lo.unwrap_or_else(|| finite().reduce(f64::min).unwrap_or(f64::NAN));
    let hi = hi.unwrap_or_else(|| finite().reduce(f64::max).unwrap_or(f64::NAN));
    let raw = hi - lo;
    let span = if raw == 0.0 { 1.0 } else { raw };
    let n = (values.len() - 1) as f64;
    let mut out = String::new();
    for (i, v) in values.iter().enumerate() {
        if i > 0 {
            out.push(' ');
        }
        let _ = write!(
            out,
            "{},{}",
            fmt2(i as f64 / n * width),
            fmt2(height - (v - lo) / span * height)
        );
    }
    out
}

fn db_f64(v: Option<&DbValue>) -> f64 {
    match v {
        Some(DbValue::Text(t)) => crate::settings::py_float(t).unwrap_or(f64::NAN),
        Some(other) => other.as_f64().unwrap_or(f64::NAN),
        None => f64::NAN,
    }
}

fn db_opt(v: Option<&DbValue>) -> Option<f64> {
    match v {
        None | Some(DbValue::Null) => None,
        other => Some(db_f64(other)),
    }
}

fn db_text(v: Option<&DbValue>) -> Option<String> {
    match v {
        None | Some(DbValue::Null) => None,
        Some(DbValue::Text(t)) => Some(t.clone()),
        Some(other) => Some(crate::lanes::analytics::web::db_to_py(other).py_str()),
    }
}

fn db_date(v: Option<&DbValue>) -> Option<NaiveDate> {
    match v? {
        DbValue::Text(t) => NaiveDate::parse_from_str(t.get(..10).unwrap_or(t), "%Y-%m-%d").ok(),
        DbValue::Naive(t) => Some(t.date()),
        DbValue::Aware(t) => Some(t.date_naive()),
        _ => None,
    }
}

/// Why the view could not be built (Python raises; the routes answer 500).
#[derive(Debug, thiserror::Error)]
pub enum ViewError {
    /// The database.
    #[error(transparent)]
    Storage(#[from] crate::storage::StorageError),
    /// A Python exception (malformed stored data).
    #[error("{0}")]
    Python(String),
}

async fn cashflows(conn: &mut Conn) -> Result<Series, ViewError> {
    let rows = conn
        .execute(
            "SELECT date, SUM(amount) amt FROM cashflows GROUP BY date",
            &[],
        )
        .await?
        .rows;
    rows.iter()
        .map(|r| {
            let d = db_date(r.get("date"))
                .ok_or_else(|| ViewError::Python("ValueError: unparseable cashflow date".into()))?;
            Ok((d, db_f64(r.get("amt"))))
        })
        .collect()
}

async fn trades(conn: &mut Conn) -> Result<Vec<Trade>, ViewError> {
    let rows = conn
        .execute(
            "SELECT symbol, entry_ts, exit_ts, qty, entry_price, exit_price, entry_score, exit_reason, pnl, costs FROM trades",
            &[],
        )
        .await?
        .rows;
    Ok(rows
        .iter()
        .map(|r| Trade {
            symbol: db_text(r.get("symbol")).unwrap_or_default(),
            pnl: db_f64(r.get("pnl")),
            costs: db_opt(r.get("costs")),
            entry_ts: db_opt(r.get("entry_ts")),
            exit_ts: db_opt(r.get("exit_ts")).filter(|x| !x.is_nan()),
            qty: db_opt(r.get("qty")),
            entry_price: db_opt(r.get("entry_price")),
            exit_price: db_opt(r.get("exit_price")),
            entry_score: db_opt(r.get("entry_score")),
            entry_rank: None,
            exit_reason: db_text(r.get("exit_reason")),
            fwd_return_20d: None,
        })
        .collect())
}

fn metric(
    label: &str,
    value: Option<f64>,
    unit: &str,
    polarity: &str,
    fl: PyValue,
    note: String,
) -> PyValue {
    PyValue::Map(dict([
        ("label", s(label)),
        ("value", value.map_or(PyValue::None, f)),
        ("unit", s(unit)),
        ("polarity", s(polarity)),
        ("flag", fl),
        ("note", PyValue::Str(note)),
    ]))
}

fn trade_stats(closed: &[Trade]) -> PyValue {
    let by_symbol: Vec<PyValue> = m::per_symbol_contribution(closed)
        .into_iter()
        .take(12)
        .map(|c| {
            PyValue::Map(dict([
                ("symbol", PyValue::Str(c.key)),
                ("pnl", f(c.pnl)),
                ("trades", int(c.trades)),
                ("share_pct", f(c.share_pct)),
            ]))
        })
        .collect();
    let by_exit: Vec<PyValue> = m::hit_rate_by_exit_reason(closed)
        .into_iter()
        .map(|g| {
            PyValue::Map(dict([
                ("exit_reason", PyValue::Str(g.key)),
                ("trades", int(g.trades)),
                ("hit_rate", f(g.hit_rate)),
                ("avg_pnl", f(g.avg_pnl)),
                ("total_pnl", f(g.total_pnl)),
            ]))
        })
        .collect();
    PyValue::Map(dict([
        ("closed", int(closed.len())),
        ("win_rate", f(m::win_rate(closed) * 100.0)),
        ("avg_win", f(m::avg_win(closed))),
        ("avg_loss", f(m::avg_loss(closed))),
        ("payoff", f(m::payoff_ratio(closed))),
        ("profit_factor", f(m::profit_factor(closed))),
        ("expectancy", f(m::expectancy(closed))),
        ("avg_hold_days", f(m::avg_holding_period(closed))),
        ("by_symbol", PyValue::List(by_symbol)),
        ("by_exit", PyValue::List(by_exit)),
    ]))
}

fn date_or_none(d: Option<NaiveDate>) -> PyValue {
    d.map_or(PyValue::None, |d| PyValue::Str(iso(d)))
}

/// `build(conn)`: the performance page's view model.
///
/// # Errors
/// The database, or stored data Python would refuse.
#[allow(clippy::too_many_lines, clippy::float_cmp)]
pub async fn build(conn: &mut Conn) -> Result<PyDict, ViewError> {
    let rows = snapshot_db::snapshot_series(conn).await?;
    if rows.is_empty() {
        return Ok(dict([
            ("available", PyValue::Bool(false)),
            ("message", s("No EOD snapshots recorded yet.")),
            ("how_to", s("python -m app.analytics.snapshot")),
        ]));
    }
    let mut index: Series = Vec::new();
    let mut nav: Series = Vec::new();
    let mut cash: Series = Vec::new();
    for r in &rows {
        let d = db_date(r.get("date"))
            .ok_or_else(|| ViewError::Python("ValueError: unparseable snapshot date".into()))?;
        index.push((d, db_f64(r.get("index_value"))));
        nav.push((d, db_f64(r.get("nav"))));
        cash.push((d, db_f64(r.get("cash"))));
    }
    let flows = cashflows(conn).await?;
    let flows_opt = (!flows.is_empty()).then_some(flows.as_slice());
    let returns = m::daily_returns(&nav, flows_opt);
    let n = index.len();
    let first_day = index[0].0;
    let last_day = index[n - 1].0;

    let mut curves: Vec<(String, Series)> = Vec::new();
    let mut used: HashMap<String, String> = HashMap::new();
    let mut notes: Vec<PyValue> = Vec::new();
    for name in BENCHMARKS {
        let b = bm::benchmark_series(conn, name, TRI).await?;
        if b.series.is_empty() {
            continue;
        }
        let clipped: Series = b.series.into_iter().filter(|p| p.0 >= first_day).collect();
        if clipped.len() < 2 {
            notes.push(PyValue::Str(format!(
                "{name}: no overlap with the portfolio's life"
            )));
            continue;
        }
        curves.push((name.to_owned(), clipped));
        used.insert(
            name.to_owned(),
            b.series_type.unwrap_or("UNKNOWN").to_owned(),
        );
    }
    let combined = m::equity_curve_vs_benchmark(&index, &curves, 100.0, &used);
    let curve = &combined.curve;

    let dd = m::max_drawdown(&index);
    let underwater: Vec<f64> = m::drawdown_series(&index)
        .iter()
        .map(|p| p.1 * 100.0)
        .collect();

    let xirr = if flows.is_empty() {
        None
    } else {
        let mut amounts: Vec<f64> = flows.iter().map(|p| p.1).collect();
        let mut dates: Vec<NaiveDate> = flows.iter().map(|p| p.0).collect();
        amounts.push(nav[n - 1].1);
        dates.push(nav[n - 1].0);
        Some(m::xirr(&amounts, &dates)).filter(|v| !v.is_nan())
    };

    let all_trades = trades(conn).await?;
    let closed: Vec<Trade> = all_trades
        .into_iter()
        .filter(|t| t.exit_ts.is_some())
        .collect();
    let stats = if closed.is_empty() {
        PyValue::None
    } else {
        trade_stats(&closed)
    };

    let monthly = m::monthly_return_matrix(&index, true);
    let mut rel = PyDict::new();
    if let Some((primary, series)) = curves.first() {
        let br = m::pct_change_dropna(series);
        let mi = m::monthly_returns(&index);
        let mb = m::monthly_returns(series);
        rel = dict([
            ("name", PyValue::Str(primary.clone())),
            (
                "series_type",
                PyValue::Str(
                    used.get(primary)
                        .cloned()
                        .unwrap_or_else(|| "UNKNOWN".into()),
                ),
            ),
            ("beta", f(m::beta(&returns, &br))),
            ("alpha", f(m::alpha_jensen(&returns, &br, 0.0))),
            ("tracking_error", f(m::tracking_error(&returns, &br))),
            ("information_ratio", f(m::information_ratio(&returns, &br))),
            ("up_capture", f(m::up_capture(&mi, &mb))),
            ("down_capture", f(m::down_capture(&mi, &mb))),
        ]);
    }

    let latest = snapshot_db::get_snapshot(conn, &iso(last_day)).await?;
    let holdings_json = latest
        .as_ref()
        .and_then(|r| db_text(r.get("holdings_json")))
        .filter(|t| !t.is_empty())
        .unwrap_or_else(|| "{}".to_owned());
    let blob = PyValue::from_json(holdings_json.as_bytes())
        .map_err(|e| ViewError::Python(format!("JSONDecodeError: {e}")))?;
    let positions = match &blob {
        PyValue::Map(mm) => mm
            .get("positions")
            .cloned()
            .unwrap_or(PyValue::List(Vec::new())),
        _ => {
            return Err(ViewError::Python(
                "AttributeError: object has no attribute 'get'".into(),
            ));
        }
    };
    let tradeable = match &positions {
        PyValue::List(items) => items
            .iter()
            .filter(|p| match p {
                PyValue::Map(pm) => !pm.get("excluded").is_some_and(PyValue::truthy),
                _ => true,
            })
            .count(),
        _ => 0,
    };

    let (nav_last, cash_last, idx_last) = (nav[n - 1].1, cash[n - 1].1, index[n - 1].1);
    let nr = returns.len();
    let metrics = vec![
        metric(
            "Total return",
            Some(m::absolute(&index) * 100.0),
            "%",
            "up",
            flag(n, "return"),
            "Time-weighted, so deposits and withdrawals do not count as gains.".into(),
        ),
        metric(
            "CAGR",
            Some(m::cagr(&index) * 100.0),
            "%",
            "up",
            flag(n, "ratio"),
            "Annualising a few weeks extrapolates noise.".into(),
        ),
        metric(
            "XIRR",
            xirr.map(|x| x * 100.0),
            "%",
            "up",
            flag(n, "ratio"),
            if xirr.is_some() {
                "Money-weighted, from recorded cashflows."
            } else {
                "No cashflows recorded."
            }
            .into(),
        ),
        metric(
            "Volatility",
            Some(m::ann_volatility(&returns) * 100.0),
            "%",
            "down",
            flag(nr, "volatility"),
            "Annualised. Higher is not better.".into(),
        ),
        metric(
            "Sharpe",
            Some(m::sharpe(&returns, 0.0)),
            "",
            "up",
            flag(nr, "ratio"),
            "Excess return per unit of risk.".into(),
        ),
        metric(
            "Sortino",
            Some(m::sortino(&returns, 0.0)),
            "",
            "up",
            flag(nr, "ratio"),
            "Downside risk only.".into(),
        ),
        metric(
            "Calmar",
            Some(m::calmar(&index)),
            "",
            "up",
            flag(n, "drawdown"),
            "CAGR over the deepest fall.".into(),
        ),
        metric(
            "Max drawdown",
            Some(dd.depth * 100.0),
            "%",
            "loss",
            flag(n, "drawdown"),
            format!(
                "{}d{}",
                dd.duration_days,
                if dd.recovered {
                    ""
                } else {
                    ", still under water"
                }
            ),
        ),
        metric(
            "VaR 95%",
            Some(m::var_historical(&returns, 0.95) * 100.0),
            "%",
            "loss",
            flag(nr, "volatility"),
            "Worst daily loss at 95% confidence. A loss, not a gain.".into(),
        ),
    ];

    let (lo, hi) = (curve.min(), curve.max());
    let chart_series: Vec<PyValue> = curve
        .columns
        .iter()
        .filter(|(_, v)| v.iter().any(|x| !x.is_nan()))
        .map(|(name, v)| {
            let present: Vec<f64> = v.iter().copied().filter(|x| !x.is_nan()).collect();
            let kind = if name == "portfolio" {
                "NAV".to_owned()
            } else {
                used.get(name).cloned().unwrap_or_else(|| "UNKNOWN".into())
            };
            PyValue::Map(dict([
                ("name", PyValue::Str(name.clone())),
                (
                    "points",
                    PyValue::Str(polyline(&present, 1000.0, 260.0, Some(lo), Some(hi))),
                ),
                ("last", f(present[present.len() - 1])),
                ("type", PyValue::Str(kind)),
            ]))
        })
        .collect();
    let has_curve = !curve.index.is_empty();
    let chart = dict([
        ("series", PyValue::List(chart_series)),
        ("lo", f(if has_curve { lo } else { 100.0 })),
        ("hi", f(if has_curve { hi } else { 100.0 })),
        ("start", date_or_none(curve.index.first().copied())),
        ("end", date_or_none(curve.index.last().copied())),
    ]);
    let worst = underwater
        .iter()
        .copied()
        .filter(|x| !x.is_nan())
        .reduce(f64::min)
        .unwrap_or(f64::NAN);
    let uw_lo = if worst.is_nan() || -0.01 < worst {
        -0.01
    } else {
        worst
    };

    let monthly_py = if monthly.is_empty() {
        PyValue::None
    } else {
        PyValue::Map(dict([
            (
                "years",
                PyValue::List(
                    monthly
                        .iter()
                        .map(|(y, _)| PyValue::Int(i64::from(*y)))
                        .collect(),
                ),
            ),
            (
                "rows",
                PyValue::List(
                    monthly
                        .iter()
                        .map(|(_, row)| {
                            PyValue::List(
                                row.iter()
                                    .map(|v| {
                                        if v.is_nan() {
                                            PyValue::None
                                        } else {
                                            f(crate::lanes::weekly::weekly_books::round_to(*v, 2))
                                        }
                                    })
                                    .collect(),
                            )
                        })
                        .collect(),
                ),
            ),
        ]))
    };

    Ok(dict([
        ("available", PyValue::Bool(true)),
        ("inception", PyValue::Str(iso(first_day))),
        ("as_of", PyValue::Str(iso(last_day))),
        ("observations", int(n)),
        ("index_value", f(idx_last)),
        ("nav", f(nav_last)),
        ("cash", f(cash_last)),
        (
            "cash_pct",
            if nav_last == 0.0 {
                PyValue::None
            } else {
                f(cash_last / nav_last * 100.0)
            },
        ),
        ("positions", int(tradeable)),
        ("metrics", PyValue::List(metrics)),
        (
            "drawdown",
            PyValue::Map(dict([
                ("depth_pct", f(dd.depth * 100.0)),
                ("peak", date_or_none(dd.peak_date)),
                ("trough", date_or_none(dd.trough_date)),
                ("recovered", PyValue::Bool(dd.recovered)),
                ("recovery", date_or_none(dd.recovery_date)),
                ("duration_days", PyValue::Int(dd.duration_days)),
                ("current_pct", f(dd.current_dd * 100.0)),
            ])),
        ),
        ("chart", PyValue::Map(chart)),
        (
            "underwater",
            PyValue::Map(dict([
                (
                    "points",
                    PyValue::Str(polyline(&underwater, 1000.0, 90.0, Some(uw_lo), Some(0.0))),
                ),
                ("worst", f(worst)),
            ])),
        ),
        (
            "series_used",
            PyValue::Map(dict(
                combined
                    .series_used
                    .iter()
                    .map(|(k, v)| (k.clone(), PyValue::Str(v.clone()))),
            )),
        ),
        (
            "dropped_benchmarks",
            PyValue::List(combined.dropped.iter().map(|d| s(d)).collect()),
        ),
        ("benchmark_notes", PyValue::List(notes)),
        ("relative", PyValue::Map(rel)),
        ("relative_flag", flag(nr, "relative")),
        ("monthly", monthly_py),
        (
            "monthly_flag",
            flag(m::monthly_returns(&index).len(), "monthly"),
        ),
        ("trades", stats),
        ("has_cashflows", PyValue::Bool(!flows.is_empty())),
    ]))
}

// =============================================================================================
// the live holdings block
// =============================================================================================

fn unavailable(reason: &str) -> PyDict {
    dict([
        ("available", PyValue::Bool(false)),
        ("gaps", hv::gaps_py()),
        ("reason", PyValue::Str(reason.chars().take(160).collect())),
    ])
}

fn latest_scan(state: &AppState) -> Option<std::path::PathBuf> {
    let dir = state
        .paths
        .resolve(crate::lanes::weekly::analyze::UPLOAD_DIR);
    let mut files: Vec<(std::time::SystemTime, std::path::PathBuf)> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "csv"))
        .filter(|p| {
            !p.file_name()
                .is_some_and(|n| n.to_string_lossy().to_lowercase().contains("tradebook"))
        })
        .filter_map(|p| Some((std::fs::metadata(&p).ok()?.modified().ok()?, p)))
        .collect();
    files.sort_by_key(|a| a.0);
    files.pop().map(|x| x.1)
}

fn frame_records(frame: &crate::lanes::weekly::frame::Frame) -> HashMap<String, PyDict> {
    use crate::lanes::weekly::frame::Column;
    let mut out = HashMap::new();
    for i in 0..frame.nrows {
        let mut row = PyDict::new();
        for name in &frame.names {
            let v = match frame.col(name) {
                Some(Column::Int(v)) => PyValue::Int(v[i]),
                Some(Column::Float(v)) => PyValue::Float(v[i]),
                Some(Column::Bool(v)) => PyValue::Bool(v[i]),
                Some(Column::Str(v)) => v[i].clone().map_or(PyValue::Float(f64::NAN), PyValue::Str),
                None => PyValue::None,
            };
            row.insert(name.clone(), v);
        }
        let key = match row.get("symbol") {
            Some(PyValue::Str(sym)) => sym.clone(),
            Some(other) => other.py_str(),
            None => continue,
        };
        out.insert(key, row);
    }
    out
}

/// `main._holdings_block()`: the live holdings table. Best-effort: no session, or a failed
/// broker read, answers `available: False` with the reason; nothing here writes.
///
/// # Errors
/// What `holdings_view` raises on malformed data (the page 500s, as Python's does).
pub async fn holdings_block(state: &AppState) -> Result<PyDict, String> {
    let Some(k) = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
        .map(crate::lanes::exec_core::desk::DeskGateway::kite)
    else {
        return Ok(unavailable("not logged in"));
    };
    if !k.is_authed().await {
        return Ok(unavailable("not logged in"));
    }
    let held = match k.holdings().await {
        Ok(h) => h,
        Err(e) => return Ok(unavailable(&e.to_string())),
    };
    let raw: Vec<PyDict> = match state.kite.holdings().await {
        Ok(PyValue::List(items)) => items
            .into_iter()
            .filter_map(|v| match v {
                PyValue::Map(mm) => Some(mm),
                _ => None,
            })
            .collect(),
        Ok(_) => Vec::new(),
        Err(e) => return Ok(unavailable(&e.to_string())),
    };
    let cash = match k.available_cash().await {
        Ok(c) => c,
        Err(e) => return Ok(unavailable(&e.to_string())),
    };
    let (cfg, _) = crate::lanes::weekly::analyze::load_config(state).await;

    let mut protected: HashMap<String, String> = HashMap::new();
    if let Ok(gtts) = state.kite.get_gtts().await {
        let gtts = match gtts {
            PyValue::List(items) => items,
            _ => Vec::new(),
        };
        let band = crate::lanes::exec_core::stops::StopConfig {
            stop_min: cfg.scoring.stop_min,
            stop_max: cfg.scoring.stop_max,
            stop_vol_mult: cfg.scoring.stop_vol_mult,
        };
        let held_py: Vec<PyValue> = held.iter().cloned().map(PyValue::Map).collect();
        let rev = super::protection::review(&held_py, &gtts, band);
        for fnd in rev.findings {
            protected.insert(fnd.symbol, fnd.kind.to_owned());
        }
        for h in &held {
            if let Some(PyValue::Str(sym)) = h.get("symbol") {
                protected
                    .entry(sym.clone())
                    .or_insert_with(|| "ok".to_owned());
            }
        }
    }

    let scan = latest_scan(state)
        .and_then(|p| std::fs::read(p).ok())
        .and_then(|raw| crate::lanes::weekly::score::load_scan(&raw, &cfg.scoring).ok())
        .map(|f| frame_records(&f))
        .unwrap_or_default();

    let mut fills: Vec<hv::FillRef> = Vec::new();
    if let Ok(mut conn) = state.db.connect().await
        && let Ok(cur) = conn
            .execute("SELECT symbol, side, when_ts FROM fills", &[])
            .await
    {
        fills = cur
            .rows
            .iter()
            .map(|r| hv::FillRef {
                symbol: db_text(r.get("symbol")).unwrap_or_default(),
                side: db_text(r.get("side")).unwrap_or_default(),
                when_ts: db_opt(r.get("when_ts")),
            })
            .collect();
    }

    let now = state.clock.now_ist().naive_local();
    let ctx = hv::Context {
        raw: &raw,
        scan: (!scan.is_empty()).then_some(&scan),
        fills: &fills,
        protected: Some(&protected),
    };
    let rows = hv::rows(&held, &ctx, now).map_err(|e| e.0)?;
    let summary = hv::summary(&rows, cash).map_err(|e| e.0)?;
    let allocation = hv::allocation(&rows, cash).map_err(|e| e.0)?;
    let warnings = hv::warnings(&rows, &summary);
    Ok(dict([
        ("available", PyValue::Bool(true)),
        (
            "rows",
            PyValue::List(rows.iter().map(|r| PyValue::Map(r.to_py())).collect()),
        ),
        ("summary", PyValue::Map(summary.dict.clone())),
        ("allocation", PyValue::Map(allocation)),
        (
            "warnings",
            PyValue::List(warnings.into_iter().map(PyValue::Map).collect()),
        ),
        ("scan_context", PyValue::Bool(!scan.is_empty())),
        ("gaps", hv::gaps_py()),
        (
            "as_of",
            PyValue::Str(now.format("%Y-%m-%d %H:%M").to_string()),
        ),
    ]))
}

// =============================================================================================
// Starlette's JSONResponse
// =============================================================================================

/// `json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"))`.
///
/// # Errors
/// `ValueError: Out of range float values are not JSON compliant` (a NaN or infinity).
pub fn starlette_json(value: &PyValue) -> Result<String, String> {
    let mut out = String::new();
    json_into(value, &mut out)?;
    Ok(out)
}

fn json_into(value: &PyValue, out: &mut String) -> Result<(), String> {
    match value {
        PyValue::None => out.push_str("null"),
        PyValue::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        PyValue::Int(i) => {
            let _ = write!(out, "{i}");
        }
        PyValue::Float(x) => {
            if !x.is_finite() {
                return Err(format!(
                    "Out of range float values are not JSON compliant: {}",
                    baskfy_providers::py::repr_float(*x)
                ));
            }
            out.push_str(&baskfy_providers::py::repr_float(*x));
        }
        PyValue::Str(t) => json_str(t, out),
        PyValue::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                json_into(item, out)?;
            }
            out.push(']');
        }
        PyValue::Map(mm) => {
            out.push('{');
            for (i, (k, v)) in mm.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                json_str(k, out);
                out.push(':');
                json_into(v, out)?;
            }
            out.push('}');
        }
        other => {
            return Err(format!(
                "Object of type {} is not JSON serializable",
                other.type_name()
            ));
        }
    }
    Ok(())
}

fn json_str(t: &str, out: &mut String) {
    out.push('"');
    for c in t.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
