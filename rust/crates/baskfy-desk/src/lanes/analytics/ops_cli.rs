//! The operations page's Python CLIs, as `baskfy-desk service <name> [args]`: `benchmark`,
//! `tradebook`, `protection`, `db`, `scoring`, `tests`. Reads and desk-table writes only.

use std::collections::BTreeMap;

use chrono::NaiveDate;

use crate::app::AppState;
use crate::lanes::analytics::analytics_views::benchmark as bm;
use crate::lanes::analytics::analytics_views::protection;
use crate::lanes::analytics::regime::RegimeConfig;
use crate::lanes::exec_core::exec_kite;
use crate::lanes::exec_core::kite_client::DeskKite;
use crate::lanes::exec_core::stops::StopConfig;
use crate::lanes::service_loop::say;
use crate::lanes::weekly::frame::Column;
use crate::lanes::weekly::weekly_books::tradebook as tb;
use crate::pyfmt::{PyArg, format_value};
use crate::storage::{Conn, Db, DbConfig, DbValue, Row};
use crate::wire::{PyDict, PyValue};

#[allow(clippy::print_stderr)]
fn err(line: &str) {
    eprintln!("{line}");
}

fn fmt(v: &PyArg, spec: &str) -> String {
    format_value(v, spec).unwrap_or_default()
}

fn fmt_f(v: f64, spec: &str) -> String {
    fmt(&PyArg::Float(v), spec)
}

fn fmt_i(v: i64, spec: &str) -> String {
    fmt(&PyArg::Int(v), spec)
}

fn fmt_s(v: &str, spec: &str) -> String {
    fmt(&PyArg::Str(v.to_owned()), spec)
}

// ---------------------------------------------------------------------------------------------
// argparse
// ---------------------------------------------------------------------------------------------

/// How many values a flag takes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Arity {
    /// `store_true`.
    Flag,
    /// One value.
    One,
    /// `nargs="?"` with this `const`.
    Opt(&'static str),
}

/// Why parsing stopped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Stop {
    /// `-h`: the usage, exit 0.
    Help(String),
    /// The usage and error, exit 2.
    Error(String),
}

/// `ArgumentParser(prog).parse_args(argv)` over `flags`; the map holds each given flag's value.
///
/// # Errors
/// [`Stop`] for `-h` or a bad command line.
pub fn parse(
    prog: &str,
    usage: &str,
    flags: &[(&'static str, Arity)],
    argv: &[String],
) -> Result<BTreeMap<&'static str, String>, Stop> {
    let usage_line = format!("usage: {prog} {usage}");
    let error = |m: String| Stop::Error(format!("{usage_line}\n{prog}: error: {m}"));
    let mut out = BTreeMap::new();
    let mut i = 0;
    while let Some(raw) = argv.get(i) {
        i += 1;
        if raw == "-h" || raw == "--help" {
            return Err(Stop::Help(usage_line));
        }
        let (name, inline) = match raw.split_once('=') {
            Some((f, v)) if f.starts_with("--") => (f, Some(v.to_owned())),
            _ => (raw.as_str(), None),
        };
        let Some(&(flag, arity)) = flags.iter().find(|(f, _)| *f == name) else {
            let rest: Vec<&str> = argv[i - 1..].iter().map(String::as_str).collect();
            return Err(error(format!("unrecognized arguments: {}", rest.join(" "))));
        };
        let value = match arity {
            Arity::Flag => {
                if inline.is_some() {
                    return Err(error(format!("argument {flag}: ignored explicit argument")));
                }
                String::new()
            }
            Arity::One => match inline {
                Some(v) => v,
                None => match argv.get(i) {
                    Some(v) if !v.starts_with('-') || v.len() == 1 => {
                        i += 1;
                        v.clone()
                    }
                    _ => return Err(error(format!("argument {flag}: expected one argument"))),
                },
            },
            Arity::Opt(konst) => match inline {
                Some(v) => v,
                None => match argv.get(i) {
                    Some(v) if !v.starts_with('-') => {
                        i += 1;
                        v.clone()
                    }
                    _ => konst.to_owned(),
                },
            },
        };
        out.insert(flag, value);
    }
    Ok(out)
}

fn stop_code(stop: Stop) -> i32 {
    match stop {
        Stop::Help(u) => {
            say(&u);
            0
        }
        Stop::Error(e) => {
            err(&e);
            2
        }
    }
}

/// `db.connect(path)`: an explicit path always means that SQLite file.
///
/// # Errors
/// The file cannot be opened.
pub async fn with_db(state: &AppState, path: Option<&str>) -> Result<AppState, String> {
    let Some(p) = path else {
        return Ok(state.clone());
    };
    let settings = state.settings_snapshot();
    let cfg = DbConfig {
        backend: "sqlite".to_owned(),
        schema: settings.db_schema,
        database_url: String::new(),
        db_path: state.paths.resolve(p),
    };
    let db = Db::open(cfg).await.map_err(|e| e.to_string())?;
    let mut out = state.clone();
    out.db = db;
    Ok(out)
}

async fn migrated(state: &AppState) -> Result<Conn, String> {
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    conn.migrate().await.map_err(|e| e.to_string())?;
    Ok(conn)
}

/// An uncaught exception: its last traceback line, exit 1.
fn raised(e: &str) -> i32 {
    err("Traceback (most recent call last):");
    err(e);
    1
}

/// `raise SystemExit(msg)`.
fn system_exit(msg: &str) -> i32 {
    err(msg);
    1
}

async fn authed_kite(state: &AppState) -> Result<std::sync::Arc<DeskKite>, i32> {
    let k = exec_kite::kite(state).map_err(|e| raised(&format!("{}: {}", e.python_type, e.message)))?;
    if !k.is_authed().await {
        return Err(system_exit("Kite session expired — log in at / first"));
    }
    Ok(k)
}

// ---------------------------------------------------------------------------------------------
// DataFrame.to_string(index=False)
// ---------------------------------------------------------------------------------------------

/// A frame column as pandas would type it.
#[derive(Debug, Clone, PartialEq)]
pub enum Col {
    /// `int64`.
    Int(Vec<i64>),
    /// `float64`.
    Float(Vec<f64>),
    /// `bool`.
    Bool(Vec<bool>),
    /// `object` (`None` prints `NaN`).
    Obj(Vec<Option<String>>),
}

impl Col {
    fn len(&self) -> usize {
        match self {
            Self::Int(v) => v.len(),
            Self::Float(v) => v.len(),
            Self::Bool(v) => v.len(),
            Self::Obj(v) => v.len(),
        }
    }

    fn take(&self, rows: &[usize]) -> Self {
        match self {
            Self::Int(v) => Self::Int(rows.iter().map(|&i| v[i]).collect()),
            Self::Float(v) => Self::Float(rows.iter().map(|&i| v[i]).collect()),
            Self::Bool(v) => Self::Bool(rows.iter().map(|&i| v[i]).collect()),
            Self::Obj(v) => Self::Obj(rows.iter().map(|&i| v[i].clone()).collect()),
        }
    }

    fn from_column(c: &Column) -> Self {
        match c {
            Column::Int(v) => Self::Int(v.clone()),
            Column::Float(v) => Self::Float(v.clone()),
            Column::Bool(v) => Self::Bool(v.clone()),
            Column::Str(v) => Self::Obj(v.clone()),
        }
    }

    /// The column of `pd.DataFrame([dict(r) for r in rows])`.
    #[must_use]
    pub fn from_db(values: &[Option<&DbValue>]) -> Self {
        let num = values.iter().all(|v| {
            matches!(
                v,
                None | Some(DbValue::Null | DbValue::Int(_) | DbValue::Float(_))
            )
        });
        let has_null = values.iter().any(|v| matches!(v, None | Some(DbValue::Null)));
        let has_float = values.iter().any(|v| matches!(v, Some(DbValue::Float(_))));
        if num && !has_null && !has_float {
            return Self::Int(
                values
                    .iter()
                    .map(|v| v.and_then(DbValue::as_i64).unwrap_or(0))
                    .collect(),
            );
        }
        if num {
            return Self::Float(
                values
                    .iter()
                    .map(|v| v.and_then(DbValue::as_f64).unwrap_or(f64::NAN))
                    .collect(),
            );
        }
        Self::Obj(
            values
                .iter()
                .map(|v| match v {
                    None | Some(DbValue::Null) => None,
                    Some(DbValue::Text(s) | DbValue::Json(s)) => Some(s.clone()),
                    Some(DbValue::Int(i)) => Some(i.to_string()),
                    Some(DbValue::Float(f)) => Some(baskfy_core::py::float_repr(*f)),
                    Some(DbValue::Bool(b)) => Some(if *b { "True" } else { "False" }.to_owned()),
                    Some(DbValue::Naive(t)) => Some(t.to_string()),
                    Some(DbValue::Aware(t)) => Some(t.to_string()),
                    Some(DbValue::Bytes(b)) => Some(format!("{b:?}")),
                })
                .collect(),
        )
    }

    const fn numeric(&self) -> bool {
        !matches!(self, Self::Obj(_))
    }

    fn cells(&self) -> Vec<String> {
        match self {
            Self::Int(v) => v.iter().map(ToString::to_string).collect(),
            Self::Bool(v) => v
                .iter()
                .map(|b| if *b { "True" } else { "False" }.to_owned())
                .collect(),
            Self::Obj(v) => v
                .iter()
                .map(|s| s.clone().unwrap_or_else(|| "NaN".to_owned()))
                .collect(),
            Self::Float(v) => float_cells(v),
        }
    }
}

fn is_decimal(s: &str) -> bool {
    let body = s.trim_start().trim_start_matches(['+', '-']);
    body.split_once('.').is_some_and(|(a, b)| {
        !a.is_empty() && a.bytes().all(|c| c.is_ascii_digit()) && b.bytes().all(|c| c.is_ascii_digit())
    })
}

fn sci(x: f64) -> String {
    let s = format!("{x:.6e}");
    let (m, e) = s.split_once('e').unwrap_or((s.as_str(), "0"));
    let e: i32 = e.parse().unwrap_or(0);
    format!("{m}e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs())
}

fn special(x: f64) -> Option<String> {
    if x.is_nan() {
        Some("NaN".to_owned())
    } else if x.is_infinite() {
        Some(if x > 0.0 { "inf" } else { "-inf" }.to_owned())
    } else {
        None
    }
}

/// pandas' `FloatArrayFormatter` without a leading space (`index=False`).
fn float_cells(v: &[f64]) -> Vec<String> {
    let mut cells: Vec<String> = v
        .iter()
        .map(|x| special(*x).unwrap_or_else(|| format!("{x:.6}")))
        .collect();
    loop {
        let nums: Vec<&String> = cells.iter().filter(|s| is_decimal(s)).collect();
        if nums.is_empty() || !nums.iter().all(|s| s.ends_with('0')) {
            break;
        }
        for c in &mut cells {
            if is_decimal(c) {
                c.pop();
            }
        }
    }
    for c in &mut cells {
        if is_decimal(c) && c.ends_with('.') {
            c.push('0');
        }
    }
    let finite: Vec<f64> = v.iter().copied().filter(|x| x.is_finite()).collect();
    let small = finite.iter().any(|x| *x != 0.0 && x.abs() < 1e-6);
    let large = finite.iter().any(|x| x.abs() > 1e6);
    let too_long = cells.iter().map(String::len).max().unwrap_or(0) > 12;
    if small || (too_long && large) {
        return v
            .iter()
            .map(|x| special(*x).unwrap_or_else(|| sci(*x)))
            .collect();
    }
    cells
}

/// `DataFrame(cols).to_string(index=False)`.
#[must_use]
pub fn frame_string(cols: &[(&str, Col)]) -> String {
    let rows = cols.first().map_or(0, |(_, c)| c.len());
    if rows == 0 {
        let names: Vec<&str> = cols.iter().map(|(n, _)| *n).collect();
        return format!("Empty DataFrame\nColumns: [{}]\nIndex: []", names.join(", "));
    }
    let mut rendered: Vec<Vec<String>> = Vec::new();
    for (name, col) in cols {
        let header = if col.numeric() {
            format!(" {name}")
        } else {
            (*name).to_owned()
        };
        let cells = col.cells();
        let width = cells
            .iter()
            .map(|c| c.chars().count())
            .chain(std::iter::once(header.chars().count()))
            .max()
            .unwrap_or(0);
        let mut lines = vec![format!("{header:>width$}")];
        lines.extend(cells.iter().map(|c| format!("{c:>width$}")));
        rendered.push(lines);
    }
    (0..=rows)
        .map(|r| {
            rendered
                .iter()
                .map(|c| c[r].as_str())
                .collect::<Vec<_>>()
                .join(" ")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// The frame of a query's rows, in its column order.
#[must_use]
pub fn rows_frame(rows: &[Row]) -> Vec<(String, Col)> {
    let Some(first) = rows.first() else {
        return Vec::new();
    };
    first
        .0
        .iter()
        .map(|(name, _)| {
            let vals: Vec<Option<&DbValue>> = rows.iter().map(|r| r.get(name)).collect();
            (name.clone(), Col::from_db(&vals))
        })
        .collect()
}

fn owned_frame_string(cols: &[(String, Col)]) -> String {
    let view: Vec<(&str, Col)> = cols.iter().map(|(n, c)| (n.as_str(), c.clone())).collect();
    frame_string(&view)
}

// ---------------------------------------------------------------------------------------------
// app/analytics/benchmark.py
// ---------------------------------------------------------------------------------------------

const BENCH_USAGE: &str = "[-h] [--db DB] [--list [SUBSTR]] [--pri INDEX] [--from START]\n                    [--to END] [--tri CSV] [--name INDEX] [--show INDEX]";

/// `benchmark.py`'s flags.
pub const BENCH_FLAGS: [(&str, Arity); 8] = [
    ("--db", Arity::One),
    ("--list", Arity::Opt("")),
    ("--pri", Arity::One),
    ("--from", Arity::One),
    ("--to", Arity::One),
    ("--tri", Arity::One),
    ("--name", Arity::One),
    ("--show", Arity::One),
];

fn iso(s: &str) -> Result<NaiveDate, String> {
    crate::lanes::analytics::ops::parse_iso_date(s)
        .ok_or_else(|| format!("ValueError: Invalid isoformat string: '{s}'"))
}

fn opt_str(v: Option<&String>) -> String {
    v.cloned().unwrap_or_else(|| "None".to_owned())
}

/// `python -m app.analytics.benchmark [args]`.
pub async fn benchmark(state: &AppState, argv: &[String]) -> i32 {
    let a = match parse("benchmark.py", BENCH_USAGE, &BENCH_FLAGS, argv) {
        Ok(a) => a,
        Err(s) => return stop_code(s),
    };
    let today = state.clock.now_ist().date_naive();
    let root = state.paths.root.clone();
    let state = match with_db(state, a.get("--db").map(String::as_str)).await {
        Ok(s) => s,
        Err(e) => return raised(&e),
    };

    if let Some(contains) = a.get("--list") {
        let k = match exec_kite::kite(&state) {
            Ok(k) => k,
            Err(e) => return raised(&format!("{}: {}", e.python_type, e.message)),
        };
        let rows = match bm::list_indices(&*k as &dyn bm::IndexKite, &root, today, contains).await {
            Ok(r) => r,
            Err(e) => return raised(&e.to_string()),
        };
        let suffix = if contains.is_empty() {
            String::new()
        } else {
            format!(" matching {}", PyValue::Str(contains.clone()).py_repr())
        };
        say(&format!("{} index instruments{suffix}", rows.len()));
        for r in rows.iter().take(80) {
            say(&format!(
                "  {} token={} {}",
                fmt_s(&r.tradingsymbol, "<32"),
                fmt_i(r.instrument_token, "<12"),
                r.segment
            ));
        }
        return 0;
    }

    let given = |f: &str| a.get(f).filter(|v| !v.is_empty());
    if let Some(path) = given("--tri") {
        let Some(name) = a.get("--name") else {
            return system_exit("--tri requires --name (the index_name to store it under)");
        };
        let res = async {
            let rows = bm::tri_csv_loader(&state.paths.resolve(path)).map_err(|e| e.to_string())?;
            let mut conn = migrated(&state).await?;
            bm::upsert_benchmark(&mut conn, name, &bm::tri_pairs(&rows), bm::TRI)
                .await
                .map_err(|e| e.to_string())
        }
        .await;
        match res {
            Ok(r) => say(&format!(
                "TRI  {}: {} rows  {} -> {}",
                r.index_name,
                r.rows,
                opt_str(r.first.as_ref()),
                opt_str(r.last.as_ref())
            )),
            Err(e) => return raised(&e),
        }
    }

    if let Some(index) = given("--pri") {
        let res = async {
            let start = iso(a.get("--from").map_or("2020-01-01", String::as_str))?;
            let end = a.get("--to").map(|s| iso(s)).transpose()?;
            let cfg = RegimeConfig::from_settings(&state.settings_snapshot()).map_err(|e| e.to_string())?;
            let aliases = cfg
                .index_aliases
                .iter()
                .find(|(k, _)| k == index)
                .map(|(_, v)| v.clone())
                .unwrap_or_default();
            let k = exec_kite::kite(&state).map_err(|e| format!("{}: {}", e.python_type, e.message))?;
            let s = bm::kite_index_series(&*k as &dyn bm::IndexKite, &root, today, index, &aliases, start, end)
                .await
                .map_err(|e| e.to_string())?;
            let mut conn = migrated(&state).await?;
            bm::upsert_benchmark(&mut conn, &s.index_name, &bm::series_pairs(&s.series), bm::PRI)
                .await
                .map_err(|e| e.to_string())
        }
        .await;
        match res {
            Ok(r) => say(&format!(
                "PRI  {}: {} rows  {} -> {}   (price-return only — TRI is authoritative)",
                r.index_name,
                r.rows,
                opt_str(r.first.as_ref()),
                opt_str(r.last.as_ref())
            )),
            Err(e) => return raised(&e),
        }
    }

    if let Some(index) = given("--show") {
        let res = async {
            let mut conn = migrated(&state).await?;
            bm::benchmark_series(&mut conn, index, bm::TRI)
                .await
                .map_err(|e| e.to_string())
        }
        .await;
        match res {
            Ok(s) => {
                let (Some(first), Some(last)) = (s.series.first(), s.series.last()) else {
                    say(&format!("no rows for {}", PyValue::Str(index.clone()).py_repr()));
                    return 0;
                };
                say(&format!(
                    "{index}: {} rows using {}  {} -> {}  level {} -> {}",
                    s.series.len(),
                    s.series_type.unwrap_or("None"),
                    first.0.format("%Y-%m-%d"),
                    last.0.format("%Y-%m-%d"),
                    fmt_f(first.1, ".2f"),
                    fmt_f(last.1, ".2f")
                ));
            }
            Err(e) => return raised(&e),
        }
    }

    if !["--pri", "--tri", "--show"].iter().any(|f| given(f).is_some()) {
        let res = async {
            let mut conn = migrated(&state).await?;
            bm::available_benchmarks(&mut conn)
                .await
                .map_err(|e| e.to_string())
        }
        .await;
        match res {
            Ok(rows) => say(&owned_frame_string(&rows_frame(&rows))),
            Err(e) => return raised(&e),
        }
    }
    0
}

// ---------------------------------------------------------------------------------------------
// app/analytics/tradebook.py
// ---------------------------------------------------------------------------------------------

const TB_USAGE: &str = "[-h] [--file FILE] [--db DB] [--reconcile] [--summary]\n                   [--capture]";

/// `tradebook.py`'s flags.
pub const TB_FLAGS: [(&str, Arity); 5] = [
    ("--file", Arity::One),
    ("--db", Arity::One),
    ("--reconcile", Arity::Flag),
    ("--summary", Arity::Flag),
    ("--capture", Arity::Flag),
];

fn g<'a>(d: &'a PyDict, k: &str) -> Option<&'a PyValue> {
    d.get(k)
}

fn gs(d: &PyDict, k: &str) -> String {
    g(d, k).map_or_else(|| "None".to_owned(), PyValue::py_str)
}

fn gi(d: &PyDict, k: &str) -> i64 {
    match g(d, k) {
        Some(PyValue::Int(i)) => *i,
        #[allow(clippy::cast_possible_truncation)]
        Some(PyValue::Float(f)) => *f as i64,
        Some(PyValue::Bool(b)) => i64::from(*b),
        _ => 0,
    }
}

fn gf(d: &PyDict, k: &str) -> f64 {
    match g(d, k) {
        Some(PyValue::Float(f)) => *f,
        #[allow(clippy::cast_precision_loss)]
        Some(PyValue::Int(i)) => *i as f64,
        _ => 0.0,
    }
}

fn glist<'a>(d: &'a PyDict, k: &str) -> &'a [PyValue] {
    match g(d, k) {
        Some(PyValue::List(l)) => l,
        _ => &[],
    }
}

fn truthy(v: Option<&PyValue>) -> bool {
    match v {
        None | Some(PyValue::None) => false,
        Some(PyValue::Bool(b)) => *b,
        Some(PyValue::Int(i)) => *i != 0,
        Some(PyValue::Float(f)) => *f != 0.0,
        Some(PyValue::Str(s)) => !s.is_empty(),
        Some(PyValue::List(l)) => !l.is_empty(),
        Some(PyValue::Map(m)) => !m.is_empty(),
        Some(_) => true,
    }
}

/// `python -m app.analytics.tradebook [args]`.
#[allow(clippy::too_many_lines)]
pub async fn tradebook(state: &AppState, argv: &[String]) -> i32 {
    let a = match parse("tradebook.py", TB_USAGE, &TB_FLAGS, argv) {
        Ok(a) => a,
        Err(s) => return stop_code(s),
    };
    let state = match with_db(state, a.get("--db").map(String::as_str)).await {
        Ok(s) => s,
        Err(e) => return raised(&e),
    };
    let mut conn = match migrated(&state).await {
        Ok(c) => c,
        Err(e) => return raised(&e),
    };
    let now = || state.clock.now_ist().naive_local();

    if let Some(file) = a.get("--file") {
        let res = match tb::import_tradebook(&mut conn, &state.paths.resolve(file), true, now()).await {
            Ok(r) => r,
            Err(tb::ImportError::Tradebook(e)) => return system_exit(&format!("import failed: {}", e.0)),
            Err(e) => return raised(&e.to_string()),
        };
        say(&format!(
            "imported {} fills across {} symbols ({} -> {})",
            gs(&res, "fills"),
            gs(&res, "symbols"),
            gs(&res, "first_trade"),
            gs(&res, "last_trade")
        ));
        say(&format!("  open lots      {}", gs(&res, "open_lots")));
        say(&format!(
            "  closed trades  {}  realised Rs {}",
            gs(&res, "closed_trades"),
            fmt_f(gf(&res, "realised_pnl"), ",.0f")
        ));
        let unmatched = glist(&res, "unmatched_sells");
        if !unmatched.is_empty() {
            say(&format!(
                "  UNMATCHED SELLS: {} — the export does not cover the full history for these",
                unmatched.len()
            ));
            for u in unmatched.iter().take(10) {
                let PyValue::Map(u) = u else { continue };
                say(&format!(
                    "    {}  {}{} @ {}",
                    gs(u, "date"),
                    fmt_s(&gs(u, "symbol"), "<14"),
                    fmt_i(gi(u, "quantity"), ">7"),
                    gs(u, "price")
                ));
            }
        }
    }

    if a.contains_key("--capture") {
        let k = match authed_kite(&state).await {
            Ok(k) => k,
            Err(code) => return code,
        };
        let raw = match k.trades().await {
            Ok(r) => r,
            Err(e) => return raised(&format!("{}: {}", e.python_type, e.message)),
        };
        let res = match tb::capture_live_trades(&mut conn, &raw, now()).await {
            Ok(r) => r,
            Err(e) => return raised(&e.to_string()),
        };
        if truthy(g(&res, "captured")) {
            let syms: Vec<String> = glist(&res, "symbols").iter().map(PyValue::py_str).collect();
            say(&format!(
                "captured {} new of {} fills: {}",
                gs(&res, "new"),
                gs(&res, "captured"),
                syms.join(", ")
            ));
            if truthy(g(&res, "closed_trades")) {
                say(&format!(
                    "  closed {} lots, realised Rs {}",
                    gs(&res, "closed_trades"),
                    fmt_f(gf(&res, "realised_pnl"), ",.0f")
                ));
            }
        } else {
            say(&format!("no fills today ({} raw rows from Kite)", gs(&res, "raw")));
        }
    }

    if a.contains_key("--summary") || a.contains_key("--file") || a.contains_key("--capture") {
        let cur = match conn
            .execute("SELECT COUNT(*) n, SUM(exit_ts IS NULL) open FROM trades", &[])
            .await
        {
            Ok(c) => c,
            Err(e) => return raised(&e.to_string()),
        };
        let row = cur.rows.first();
        let n = row.and_then(|r| r.get("n")).and_then(DbValue::as_i64).unwrap_or(0);
        let open = row.and_then(|r| r.get("open")).and_then(DbValue::as_i64).unwrap_or(0);
        say(&format!("\ntrades table: {n} rows, {open} open lots"));
    }

    if a.contains_key("--reconcile") {
        let k = match authed_kite(&state).await {
            Ok(k) => k,
            Err(code) => return code,
        };
        let holdings: Vec<PyDict> = match k.holdings().await {
            Ok(h) => h
                .into_iter()
                .filter(|h| !gs(h, "symbol").to_uppercase().starts_with("SGB"))
                .collect(),
            Err(e) => return raised(&format!("{}: {}", e.python_type, e.message)),
        };
        let cov = match tb::coverage(&mut conn, &holdings).await {
            Ok(c) => c,
            Err(e) => return raised(&e.to_string()),
        };
        say(&format!(
            "\nlot coverage: {}/{} holdings ({}%)",
            gs(&cov, "with_lots"),
            gs(&cov, "holdings"),
            gs(&cov, "coverage_pct")
        ));
        let missing: Vec<String> = glist(&cov, "missing").iter().map(PyValue::py_str).collect();
        if !missing.is_empty() {
            say(&format!("  no lots for: {}", missing.join(", ")));
        }
        let problems = match tb::reconcile_with_holdings(&mut conn, &holdings).await {
            Ok(p) => p,
            Err(e) => return raised(&e.to_string()),
        };
        if problems.is_empty() {
            say("  every holding reconciles against its lots");
        } else {
            say(&format!("\n{} quantity mismatches:", problems.len()));
            say(&format!(
                "  {}{}{}{}  likely cause",
                fmt_s("symbol", "<16"),
                fmt_s("holding", ">9"),
                fmt_s("lots", ">9"),
                fmt_s("diff", ">9")
            ));
            for p in &problems {
                say(&format!(
                    "  {}{}{}{}  {}",
                    fmt_s(&gs(p, "symbol"), "<16"),
                    fmt_i(gi(p, "holding_qty"), ">9"),
                    fmt_i(gi(p, "lot_qty"), ">9"),
                    fmt_i(gi(p, "difference"), ">+9"),
                    gs(p, "likely_cause")
                ));
            }
        }
    }
    0
}

// ---------------------------------------------------------------------------------------------
// app/analytics/protection.py
// ---------------------------------------------------------------------------------------------

/// `python -m app.analytics.protection`: two reads, never a stop placed or cancelled.
pub async fn protection_check(state: &AppState, argv: &[String]) -> i32 {
    if let Err(s) = parse("protection.py", "[-h]", &[], argv) {
        return stop_code(s);
    }
    let k = match authed_kite(state).await {
        Ok(k) => k,
        Err(code) => return code,
    };
    let (holdings, gtts) = match (k.holdings().await, k.get_gtts().await) {
        (Ok(h), Ok(g)) => (h, g),
        (Err(e), _) | (_, Err(e)) => return raised(&format!("{}: {}", e.python_type, e.message)),
    };
    let held: Vec<PyValue> = holdings.into_iter().map(PyValue::Map).collect();
    let rev = protection::review(&held, &gtts, StopConfig::default());
    say(&protection::summary_line(&rev));
    let cov = rev
        .coverage_pct
        .map_or_else(|| "None".to_owned(), baskfy_core::py::float_repr);
    say(&format!(
        "\nchecked {} tradeable positions · {} active GTTs · coverage {cov}%",
        rev.checked, rev.active_gtts
    ));
    if !rev.excluded.is_empty() {
        say(&format!(
            "excluded from the check (never traded): {}",
            rev.excluded.join(", ")
        ));
    }
    if !rev.findings.is_empty() {
        say(&format!(
            "\n{}{}{}  detail",
            fmt_s("issue", "<10"),
            fmt_s("symbol", "<16"),
            fmt_s("value", ">13")
        ));
        for f in &rev.findings {
            say(&format!(
                "{}{}{}  {}",
                fmt_s(f.kind, "<10"),
                fmt_s(&f.symbol, "<16"),
                fmt_i(f.value, ">13,"),
                f.detail
            ));
        }
    }
    say("\nThis check never places or cancels a stop. Arming one is a decision with money behind it, not something a status check should do.");
    0
}

// ---------------------------------------------------------------------------------------------
// app/analytics/db.py
// ---------------------------------------------------------------------------------------------

/// `python -m app.analytics.db [--db DB] [--init] [--info]` (an explicit path: SQLite).
pub async fn db_cli(state: &AppState, argv: &[String]) -> i32 {
    let flags = [("--db", Arity::One), ("--init", Arity::Flag), ("--info", Arity::Flag)];
    let a = match parse("db.py", "[-h] [--db DB] [--init] [--info]", &flags, argv) {
        Ok(a) => a,
        Err(s) => return stop_code(s),
    };
    let path = a
        .get("--db")
        .cloned()
        .unwrap_or_else(|| state.settings_snapshot().db_path);
    let st = match with_db(state, Some(&path)).await {
        Ok(s) => s,
        Err(e) => return raised(&e),
    };
    let mut conn = match migrated(&st).await {
        Ok(c) => c,
        Err(e) => return raised(&e),
    };
    if a.contains_key("--init") || !a.contains_key("--info") {
        say(&format!("initialised: {path}"));
    }
    if !a.contains_key("--info") {
        return 0;
    }
    match db_info(&mut conn, &path).await {
        Ok(lines) => {
            for l in lines {
                say(&l);
            }
            0
        }
        Err(e) => raised(&e),
    }
}

async fn db_info(conn: &mut Conn, path: &str) -> Result<Vec<String>, String> {
    let first = |rows: &[Row]| -> String {
        match rows.first().and_then(|r| r.at(0)) {
            Some(DbValue::Int(i)) => i.to_string(),
            Some(DbValue::Text(s)) => s.clone(),
            _ => "None".to_owned(),
        }
    };
    let e = |x: crate::storage::StorageError| x.to_string();
    let ver = first(&conn.execute("PRAGMA user_version", &[]).await.map_err(e)?.rows);
    let mode = first(&conn.execute("PRAGMA journal_mode", &[]).await.map_err(e)?.rows);
    let mut out = vec![
        format!("db            : {path}"),
        format!("schema version: {ver}   journal_mode: {mode}"),
    ];
    let tables: Vec<String> = conn
        .execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
            &[],
        )
        .await
        .map_err(e)?
        .rows
        .iter()
        .filter_map(|r| r.get("name").and_then(DbValue::as_str).map(str::to_owned))
        .collect();
    for t in &tables {
        let n = conn
            .execute(&format!("SELECT COUNT(*) AS c FROM {t}"), &[])
            .await
            .map_err(e)?
            .rows
            .first()
            .and_then(|r| r.get("c"))
            .and_then(DbValue::as_i64)
            .unwrap_or(0);
        out.push(format!("  {} {} rows", fmt_s(t, "<20"), fmt_i(n, ">7")));
    }
    let rows = crate::lanes::weekly::weekly_books::snapshot_db::snapshot_series(conn)
        .await
        .map_err(|x| x.to_string())?;
    if let (Some(a), Some(b)) = (rows.first(), rows.last()) {
        let d = |r: &Row| match r.get("date") {
            Some(DbValue::Text(s)) => s.clone(),
            Some(other) => format!("{other:?}"),
            None => "None".to_owned(),
        };
        let iv = |r: &Row| r.get("index_value").and_then(DbValue::as_f64).unwrap_or(f64::NAN);
        out.push(format!("snapshots span: {} -> {}", d(a), d(b)));
        out.push(format!(
            "index          : {} -> {}",
            fmt_f(iv(a), ".4f"),
            fmt_f(iv(b), ".4f")
        ));
    }
    Ok(out)
}

// ---------------------------------------------------------------------------------------------
// app/scoring.py
// ---------------------------------------------------------------------------------------------

/// `scoring.py`'s printed columns.
pub const SCORING_COLS: [&str; 8] = [
    "rank",
    "symbol",
    "SCORE",
    "close",
    "rsi_one_month",
    "away_from_high_one_year",
    "ext_over_20dma",
    "reject",
];

/// `python -m app.scoring <scan.csv>`: the top 30 ranks.
pub async fn scoring(state: &AppState, argv: &[String]) -> i32 {
    let Some(path) = argv.first() else {
        return raised("IndexError: list index out of range");
    };
    let full = state.paths.resolve(path);
    let raw = match std::fs::read(&full) {
        Ok(r) => r,
        Err(_) => {
            return raised(&format!(
                "FileNotFoundError: [Errno 2] No such file or directory: '{path}'"
            ));
        }
    };
    let (basket, _) = crate::lanes::weekly::analyze::load_config(state).await;
    let cfg = crate::lanes::weekly::analyze::scoring_config(&basket);
    let scored = match crate::lanes::weekly::score::load_scan(&raw, &cfg)
        .and_then(|f| crate::lanes::weekly::score::score(&f, &cfg))
    {
        Ok(s) => s,
        Err(e) => return raised(&e.0),
    };
    match scoring_table(&scored) {
        Ok(t) => {
            say(&t);
            0
        }
        Err(e) => raised(&e),
    }
}

/// `df.sort_values("rank").head(30)[cols].to_string(index=False)`.
///
/// # Errors
/// `KeyError` for a column the frame lacks.
pub fn scoring_table(s: &crate::lanes::weekly::score::Scored) -> Result<String, String> {
    let order = crate::lanes::weekly::pandas::nargsort(&s.rank, true);
    let rows: Vec<usize> = order.into_iter().take(30).collect();
    let mut cols: Vec<(&str, Col)> = Vec::new();
    for name in SCORING_COLS {
        let col = match name {
            "rank" if s.rank_is_int => {
                #[allow(clippy::cast_possible_truncation)]
                let v = s.rank.iter().map(|x| *x as i64).collect();
                Col::Int(v)
            }
            "rank" => Col::Float(s.rank.clone()),
            "SCORE" => Col::Float(s.score.clone()),
            "ext_over_20dma" => Col::Float(s.ext_over_20dma.clone()),
            "reject" => Col::Obj(s.reject.iter().cloned().map(Some).collect()),
            other => Col::from_column(
                s.frame
                    .col(other)
                    .ok_or_else(|| format!("KeyError: \"['{other}'] not in index\""))?,
            ),
        };
        cols.push((name, col.take(&rows)));
    }
    Ok(frame_string(&cols))
}

/// `python -m pytest tests/ -q`: the suite is not in the desk image.
#[must_use]
pub fn tests_cli() -> i32 {
    err("the test suite is not part of the desk binary; run `cargo test -p baskfy-desk` in a source checkout");
    5
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn v(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn argparse_like_python() {
        let a = parse("benchmark.py", BENCH_USAGE, &BENCH_FLAGS, &v(&["--list", "NIFTY"])).unwrap();
        assert_eq!(a["--list"], "NIFTY");
        let a = parse("benchmark.py", BENCH_USAGE, &BENCH_FLAGS, &v(&["--list"])).unwrap();
        assert_eq!(a["--list"], "");
        let a = parse("benchmark.py", BENCH_USAGE, &BENCH_FLAGS, &v(&["--pri", "NIFTY 500", "--from", "2020-01-01"])).unwrap();
        assert_eq!((a["--pri"].as_str(), a["--from"].as_str()), ("NIFTY 500", "2020-01-01"));
        assert!(matches!(parse("tradebook.py", TB_USAGE, &TB_FLAGS, &v(&["--nope"])), Err(Stop::Error(e)) if e.ends_with("tradebook.py: error: unrecognized arguments: --nope")));
        assert!(matches!(parse("x.py", "", &TB_FLAGS, &v(&["--file"])), Err(Stop::Error(e)) if e.ends_with("argument --file: expected one argument")));
    }

    #[test]
    fn frames_print_as_pandas() {
        let nan = f64::NAN;
        assert_eq!(
            frame_string(&[
                ("rank", Col::Float(vec![1.0, 2.0, nan])),
                ("symbol", Col::Obj(vec![Some("AB".into()), Some("LONGSYM".into()), Some("C".into())])),
                ("SCORE", Col::Float(vec![81.234_567, -5.5, nan])),
                ("close", Col::Float(vec![1234.5, 12.25, 3.0])),
                ("x", Col::Float(vec![0.000_012_34, 1.0, 2.0])),
                ("big", Col::Float(vec![1e17, 1.0, 2.0])),
                ("reject", Col::Obj(vec![Some(String::new()), Some("price<20".into()), Some(String::new())])),
            ]),
            " rank  symbol     SCORE   close        x          big   reject\n  1.0      AB 81.234567 1234.50 0.000012 1.000000e+17         \n  2.0 LONGSYM -5.500000   12.25 1.000000 1.000000e+00 price<20\n  NaN       C       NaN    3.00 2.000000 2.000000e+00         "
        );
        assert_eq!(frame_string(&[("a", Col::Int(vec![-5, 20]))]), " a\n-5\n20");
        assert_eq!(frame_string(&[("a", Col::Float(vec![123_456.789, 2.0]))]), "         a\n123456.789\n     2.000");
        assert_eq!(frame_string(&[("a", Col::Float(vec![123_456_789.123, 2.0]))]), "           a\n1.234568e+08\n2.000000e+00");
        assert_eq!(
            frame_string(&[("a", Col::Bool(vec![true, false])), ("b", Col::Obj(vec![None, Some("x".into())]))]),
            "    a   b\n True NaN\nFalse   x"
        );
        assert_eq!(frame_string(&[]), "Empty DataFrame\nColumns: []\nIndex: []");
    }
}
