//! The weekly book's two routes — `app/main.py`'s `POST /analyze` (score a scan, read the
//! account, build the plan) and `POST /execute` (the confirm-gated send) — with
//! `_funding_check`, `_uploaded_scan`, `_generated_scan`'s refusals, `_resolve_breadth` and
//! `app/breadth_source.py`.
//!
//! **Non-negotiable 1, as Python has it.** `/execute` fires only with `confirm == "true"`, a
//! `plan_id` this process issued at `/analyze`, and a plan no older than 1,800 seconds
//! ([`crate::plan::check_execute`], in that order). The weekly rebalancer has **no**
//! auto-execute flag and never gains one: nothing in this module runs an order except the
//! handler of `POST /execute`. Every order goes through the gateway (`Gateway::place`, law 2),
//! sells first, with `client_id = plan_id:symbol`. Stops are **not** armed here (they are
//! sized from fills at `/stops`, which is the stops lane's).
//!
//! The generated-scan path (`generate_for=…`, or `SCAN_SOURCE_DEFAULT=generated`) builds the
//! scan from the screener's bars through [`super::scan_source`] (DECISIONS-RS R8.WP1). Telemetry (`_tel`) is a no-op in the
//! harness and is not reproduced.

use std::collections::BTreeSet;
use std::sync::Arc;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderMap, Response, StatusCode, header};
use axum::routing::post;
use chrono::NaiveDate;

use super::basket::{BasketConfig, PlanInputs, build_plan};
use super::frame::{Frame, read_csv};
use super::pandas::{Num, py_round_int};
use super::plan_store;
use super::scan_source;
use super::score::{ScoringConfig, audit, load_scan, score};
use crate::app::AppState;
use crate::gateway::{FAILED_STATUSES, PlaceOrder};
use crate::lanes::analytics::desk_settings::{SVal, config_value, stored_overrides};
use crate::lanes::analytics::web::{self, MultiDict, detail, internal_error};
use crate::lanes::exec_core::desk::{DeskGateway, latest_nav};
use crate::lanes::exec_core::kite_client::DeskKite;
use crate::plan::{Plan, PlanLine, check_execute};
use crate::settings::Settings;
use crate::wire::{PyDict, PyValue, dict};

/// `UPLOAD_DIR`.
pub const UPLOAD_DIR: &str = "data/uploads";
/// `CARRIED_COLUMNS` (`baskfy_core.momentum_scan`).
pub use super::scan_source::CARRIED_COLUMNS;
/// `/execute`'s circuit breaker: identical failures in a row that stop the batch.
pub const BREAKER_TRIP: usize = 3;

/// The two routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/analyze", post(analyze))
        .route("/execute", post(execute))
}

// =============================================================================================
// config
// =============================================================================================

fn f_of(v: &SVal) -> Option<f64> {
    #[allow(clippy::cast_precision_loss)]
    match v {
        SVal::Float(f) => Some(*f),
        SVal::Int(i) => Some(*i as f64),
        _ => None,
    }
}

/// The weekly book's constants as `app.config` holds them at run time: the code's values, the
/// environment's `FULLY_INVESTED`, then any override stored from the settings page.
#[must_use]
pub fn weekly_config(
    settings: &Settings,
    overrides: &std::collections::BTreeMap<String, SVal>,
) -> BasketConfig {
    let mut cfg = BasketConfig {
        fully_invested: settings.fully_invested,
        ..BasketConfig::default()
    };
    let val = |k: &str| config_value(overrides, settings, k);
    let set_f = |slot: &mut f64, k: &str| {
        if let Some(x) = val(k).as_ref().and_then(f_of) {
            *slot = x;
        }
    };
    set_f(&mut cfg.cluster_cap, "CLUSTER_CAP");
    set_f(&mut cfg.max_pos_vs_day_value, "MAX_POS_VS_DAY_VALUE");
    set_f(&mut cfg.max_single_weight, "MAX_SINGLE_WEIGHT");
    set_f(&mut cfg.min_position_weight, "MIN_POSITION_WEIGHT");
    set_f(&mut cfg.parabolic_rsi, "PARABOLIC_RSI");
    set_f(&mut cfg.replacement_edge, "REPLACEMENT_EDGE");
    set_f(&mut cfg.trim_to_weight, "TRIM_TO_WEIGHT");
    set_f(&mut cfg.scoring.stop_min, "STOP_MIN");
    set_f(&mut cfg.scoring.stop_max, "STOP_MAX");
    set_f(&mut cfg.scoring.stop_vol_mult, "STOP_VOL_MULT");
    if let Some(SVal::Bool(b)) = val("FULLY_INVESTED") {
        cfg.fully_invested = b;
    }
    if let Some(SVal::Int(i)) = val("RETENTION_BUFFER") {
        cfg.retention_buffer = i;
    }
    match val("RUNNER_MAX_VALUE") {
        Some(SVal::Int(i)) => cfg.runner_max_value = Num::Int(i),
        Some(SVal::Float(f)) => cfg.runner_max_value = Num::Float(f),
        _ => {}
    }
    if let Some(SVal::Pair(a, b)) = val("TARGET_POSITIONS") {
        cfg.target_positions = (a, b);
    }
    cfg
}

/// The scoring constants (`EXCLUDED_SYMBOLS`, the filters, the blends, the stop band).
#[must_use]
pub fn scoring_config(cfg: &BasketConfig) -> ScoringConfig {
    cfg.scoring.clone()
}

/// The weekly config with stored overrides, and whether the regime gate is on.
pub async fn load_config(state: &AppState) -> (BasketConfig, bool) {
    let settings = state.settings_snapshot();
    let overrides = match state.db.connect().await {
        Ok(mut conn) => {
            if conn.migrate().await.is_ok() {
                stored_overrides(&mut conn).await.unwrap_or_default()
            } else {
                std::collections::BTreeMap::new()
            }
        }
        Err(_) => std::collections::BTreeMap::new(),
    };
    let regime = match config_value(&overrides, &settings, "REGIME_ENABLED") {
        Some(SVal::Bool(b)) => b,
        _ => settings.regime_enabled,
    };
    (weekly_config(&settings, &overrides), regime)
}

// =============================================================================================
// the account
// =============================================================================================

/// `app.main.kite()` and `gateway()`: the exec_core lane's wrapper and gateway, when the app's
/// gateway is the desk's.
fn desk(state: &AppState) -> Option<(&DeskGateway, Arc<DeskKite>)> {
    let any = state.gateway.as_any()?;
    let gw = any.downcast_ref::<DeskGateway>()?;
    Some((gw, gw.kite()))
}

fn no_desk() -> Response<Body> {
    detail(
        StatusCode::SERVICE_UNAVAILABLE,
        "the desk's Kite client and gateway are not ported yet (lane exec_core); nothing was read or sent",
    )
}

fn get<'a>(d: &'a PyDict, k: &str) -> &'a PyValue {
    static NONE: PyValue = PyValue::None;
    d.get(k).unwrap_or(&NONE)
}

#[allow(clippy::cast_precision_loss)]
fn f64_of(v: &PyValue) -> Option<f64> {
    match v {
        PyValue::Float(f) => Some(*f),
        PyValue::Int(i) => Some(*i as f64),
        PyValue::Bool(b) => Some(f64::from(u8::from(*b))),
        _ => None,
    }
}

fn orders_of(plan: &PyDict) -> Vec<PyDict> {
    match get(plan, "orders") {
        PyValue::List(rows) => rows
            .iter()
            .filter_map(|o| match o {
                PyValue::Map(m) => Some(m.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// `_funding_check(k, orders)`: the whole plan's margin against what is available — a
/// calculation, never an order; a failure is reported, never fatal.
pub async fn funding_check(state: &AppState, k: &DeskKite, orders: &[PyDict]) -> PyDict {
    let mut basket: Vec<PyValue> = Vec::new();
    for o in orders {
        let delta = match get(o, "delta") {
            PyValue::Int(i) => *i,
            #[allow(clippy::cast_possible_truncation)]
            PyValue::Float(f) => f.trunc() as i64,
            _ => 0,
        };
        if delta == 0 {
            continue;
        }
        basket.push(PyValue::Map(dict([
            ("exchange", PyValue::Str("NSE".to_owned())),
            ("tradingsymbol", get(o, "symbol").clone()),
            (
                "transaction_type",
                PyValue::Str(if delta < 0 { "SELL" } else { "BUY" }.to_owned()),
            ),
            ("variety", PyValue::Str("regular".to_owned())),
            ("product", PyValue::Str("CNC".to_owned())),
            ("order_type", PyValue::Str("MARKET".to_owned())),
            ("quantity", PyValue::Int(delta.abs())),
        ])));
    }
    if basket.is_empty() {
        return dict([
            ("checked", PyValue::Bool(false)),
            ("reason", PyValue::Str("nothing to fund".to_owned())),
        ]);
    }
    let error = |message: String| {
        let short: String = message.chars().take(200).collect();
        dict([
            ("checked", PyValue::Bool(false)),
            ("error", PyValue::Str(short)),
        ])
    };
    let resp = match state.kite.basket_order_margins(&basket, true, None).await {
        Ok(r) => r,
        Err(e) => return error(e.to_string()),
    };
    let fin = match &resp {
        PyValue::Map(m) => m.get("final").cloned().unwrap_or(PyValue::None),
        _ => PyValue::None,
    };
    let total = match &fin {
        PyValue::Map(m) if !m.is_empty() => m.get("total").cloned().unwrap_or(PyValue::None),
        _ => PyValue::None,
    };
    let need = if total.truthy() {
        f64_of(&total).unwrap_or(0.0)
    } else {
        0.0
    };
    let have = match k.available_cash().await {
        Ok(v) => v,
        Err(e) => return error(e.message),
    };
    let r = |x: f64| PyValue::Int(py_round_int(x).unwrap_or(0));
    dict([
        ("checked", PyValue::Bool(true)),
        ("required", r(need)),
        ("available", r(have)),
        ("shortfall", r((need - have).max(0.0))),
    ])
}

// =============================================================================================
// the scan
// =============================================================================================

/// One part of a `multipart/form-data` body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Part {
    /// `name=`.
    pub name: String,
    /// `filename=`, when the part is a file.
    pub filename: Option<String>,
    /// The bytes.
    pub data: Vec<u8>,
}

fn find(hay: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    (from..=hay.len() - needle.len()).find(|&i| hay.get(i..i + needle.len()) == Some(needle))
}

fn disposition_param(line: &str, key: &str) -> Option<String> {
    let lower = line.to_ascii_lowercase();
    let pat = format!("{key}=\"");
    let mut start = 0;
    while let Some(pos) = lower.get(start..)?.find(&pat) {
        let at = start + pos;
        let ok = at == 0 || matches!(lower.as_bytes().get(at - 1), Some(b' ' | b';'));
        let value_start = at + pat.len();
        if ok {
            let rest = line.get(value_start..)?;
            let end = rest.find('"')?;
            return rest.get(..end).map(ToOwned::to_owned);
        }
        start = value_start;
    }
    None
}

/// Parse a `multipart/form-data` body (what Starlette's form parser accepts from a browser or
/// `requests`). `None` when the body is not one.
#[must_use]
pub fn parse_multipart(content_type: &str, body: &[u8]) -> Option<Vec<Part>> {
    let boundary = content_type
        .split(';')
        .map(str::trim)
        .find_map(|p| p.strip_prefix("boundary="))?
        .trim_matches('"')
        .to_owned();
    let delim = format!("--{boundary}").into_bytes();
    let mut parts = Vec::new();
    let mut pos = find(body, &delim, 0)? + delim.len();
    loop {
        if body.get(pos..pos + 2) == Some(b"--") {
            break;
        }
        if body.get(pos..pos + 2) == Some(b"\r\n") {
            pos += 2;
        }
        let head_end = find(body, b"\r\n\r\n", pos)?;
        let head = String::from_utf8_lossy(body.get(pos..head_end)?).into_owned();
        let data_start = head_end + 4;
        let mut next_delim = b"\r\n".to_vec();
        next_delim.extend_from_slice(&delim);
        let data_end = find(body, &next_delim, data_start)?;
        let mut name = None;
        let mut filename = None;
        for line in head.split("\r\n") {
            if line
                .to_ascii_lowercase()
                .starts_with("content-disposition:")
            {
                name = disposition_param(line, "name");
                filename = disposition_param(line, "filename");
            }
        }
        if let Some(name) = name {
            parts.push(Part {
                name,
                filename,
                data: body.get(data_start..data_end)?.to_vec(),
            });
        }
        pos = data_end + next_delim.len();
    }
    Some(parts)
}

/// `repr(str)`.
#[must_use]
pub fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `dt.date.fromisoformat(text)` (Python 3.12): `YYYY-MM-DD`, `YYYYMMDD`, and the ISO week
/// forms `YYYY-Www-D`, `YYYYWwwD`, `YYYY-Www`, `YYYYWww` (day 1 when absent). ASCII digits
/// only, every field at its exact width, year 1..=9999; anything else is `None` (Python's
/// `ValueError`).
#[must_use]
pub fn date_fromisoformat(text: &str) -> Option<NaiveDate> {
    let b = text.as_bytes();
    let num = |r: std::ops::Range<usize>| -> Option<u32> {
        let part = b.get(r)?;
        if part.is_empty() || !part.iter().all(u8::is_ascii_digit) {
            return None;
        }
        std::str::from_utf8(part).ok()?.parse().ok()
    };
    let year = num(0..4)?;
    if year == 0 {
        return None;
    }
    let y = i32::try_from(year).ok()?;
    let week = |w: u32, d: u32| -> Option<NaiveDate> {
        if !(1..=53).contains(&w) || !(1..=7).contains(&d) {
            return None;
        }
        let wd = chrono::Weekday::try_from(u8::try_from(d - 1).ok()?).ok()?;
        NaiveDate::from_isoywd_opt(y, w, wd)
    };
    match (b.len(), b.get(4), b.get(5)) {
        (10, Some(b'-'), Some(b'W')) if b.get(8) == Some(&b'-') => week(num(6..8)?, num(9..10)?),
        (8, Some(b'-'), Some(b'W')) => week(num(6..8)?, 1),
        (8, Some(b'W'), _) => week(num(5..7)?, num(7..8)?),
        (7, Some(b'W'), _) => week(num(5..7)?, 1),
        (10, Some(b'-'), _) if b.get(7) == Some(&b'-') => {
            NaiveDate::from_ymd_opt(y, num(5..7)?, num(8..10)?)
        }
        (8, _, _) => NaiveDate::from_ymd_opt(y, num(4..6)?, num(6..8)?),
        _ => None,
    }
}

/// `re.sub(r"[^A-Za-z0-9._-]", "_", name)`.
#[must_use]
pub fn safe_filename(name: &str) -> String {
    name.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
                c
            } else {
                '_'
            }
        })
        .collect()
}

fn sha256_hex(raw: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    use std::fmt::Write as _;
    Sha256::digest(raw)
        .iter()
        .fold(String::with_capacity(64), |mut s, b| {
            let _ = write!(s, "{b:02x}");
            s
        })
}

/// `_uploaded_scan(scan, raw)`: retain the file, describe it by its digest.
fn uploaded_scan(
    state: &AppState,
    cfg: &ScoringConfig,
    filename: Option<&str>,
    raw: &[u8],
) -> Result<(Frame, PyDict, String), Box<Response<Body>>> {
    let df = load_scan(raw, cfg).map_err(|e| Box::new(internal_error(&e.0)))?;
    let safe = safe_filename(filename.filter(|f| !f.is_empty()).unwrap_or("scan.csv"));
    #[allow(clippy::cast_possible_truncation)]
    let stamp = state.clock.time().floor() as i64;
    let dest = state
        .paths
        .resolve(&format!("{UPLOAD_DIR}/scan_{stamp}_{safe}"));
    if let Some(parent) = dest.parent()
        && std::fs::create_dir_all(parent).is_ok()
    {
        let _ = std::fs::write(&dest, raw);
    }
    let digest = sha256_hex(raw);
    let fname = filename.map_or(PyValue::None, |f| PyValue::Str(f.to_owned()));
    let note = format!(
        "scan {}",
        filename.map_or_else(|| "None".to_owned(), ToOwned::to_owned)
    );
    let provenance = dict([
        ("source", PyValue::Str("upload".to_owned())),
        (
            "screen_run_id",
            PyValue::Str(format!("upload:{}", digest.get(..16).unwrap_or(""))),
        ),
        ("filename", fname),
        (
            "rows",
            PyValue::Int(i64::try_from(df.nrows).unwrap_or(i64::MAX)),
        ),
        ("warnings", PyValue::List(Vec::new())),
    ]);
    Ok((df, provenance, note))
}

/// `_generated_scan(generate_for)`: the same columns, from the screener's bars instead of a
/// website (`scan_source.generate`, ported in [`super::scan_source`]).
async fn generated_scan(
    state: &AppState,
    cfg: &ScoringConfig,
    generate_for: &str,
) -> Result<(Frame, PyDict, String), Box<Response<Body>>> {
    let Some(as_of) = date_fromisoformat(generate_for) else {
        return Err(Box::new(detail(
            StatusCode::BAD_REQUEST,
            &format!(
                "generate_for must be YYYY-MM-DD, got {}",
                py_repr_str(generate_for)
            ),
        )));
    };
    let carried = carried_columns(state)?;
    let dsn = state.settings_snapshot().screener_dsn;
    let unavailable = |exc: &str| {
        Box::new(detail(
            StatusCode::SERVICE_UNAVAILABLE,
            &format!("could not generate a scan for {as_of}: {exc}"),
        ))
    };
    let (bars, days) = scan_source::fetch(&dsn, as_of)
        .await
        .map_err(|e| unavailable(&e))?;
    let generated = scan_source::generate_from(&bars, &days, as_of, cfg, &carried)
        .map_err(|e| unavailable(&e.0))?;
    drop(bars);
    let mut provenance = generated.provenance();
    provenance.insert("source", PyValue::Str("generated".to_owned()));
    provenance.insert(
        "warnings",
        PyValue::List(
            scan_source::scan_warnings(&generated)
                .into_iter()
                .map(PyValue::Str)
                .collect(),
        ),
    );
    // `as_desk_frame` is outside Python's try: its ValueError is a 500.
    let df =
        scan_source::as_desk_frame(&generated, cfg).map_err(|e| Box::new(internal_error(&e.0)))?;
    Ok((df, provenance, format!("generated scan {as_of}")))
}

/// `_carried_columns()`: the newest retained scan supplies what no bar series can.
fn carried_columns(state: &AppState) -> Result<Frame, Box<Response<Body>>> {
    let dir = state.paths.resolve(UPLOAD_DIR);
    let mut scans: Vec<(std::time::SystemTime, std::path::PathBuf)> = std::fs::read_dir(&dir)
        .map(|rd| {
            rd.filter_map(Result::ok)
                .map(|e| e.path())
                .filter(|p| {
                    p.file_name()
                        .and_then(|n| n.to_str())
                        .is_some_and(|n| n.starts_with("scan_") && n.ends_with(".csv"))
                })
                .filter_map(|p| {
                    let m = std::fs::metadata(&p).and_then(|m| m.modified()).ok()?;
                    Some((m, p))
                })
                .collect()
        })
        .unwrap_or_default();
    scans.sort_by_key(|s| std::cmp::Reverse(s.0));
    let Some((_, newest)) = scans.first() else {
        return Err(Box::new(detail(
            StatusCode::SERVICE_UNAVAILABLE,
            "a generated scan still needs marketcap, beta, circuits and F&O membership, and no uploaded scan is available to take them from. Upload one scan first.",
        )));
    };
    let frame = match std::fs::read(newest).map(|raw| read_csv(&raw)) {
        Ok(Ok(f)) => f,
        _ => return Err(Box::new(internal_error("unreadable retained scan"))),
    };
    let missing: Vec<String> = ["symbol", "series"]
        .iter()
        .chain(CARRIED_COLUMNS.iter())
        .filter(|c| !frame.has(c))
        .map(|c| py_repr_str(c))
        .collect();
    if !missing.is_empty() {
        let name = newest
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or_default();
        return Err(Box::new(detail(
            StatusCode::SERVICE_UNAVAILABLE,
            &format!("{name} cannot supply [{}]", missing.join(", ")),
        )));
    }
    Ok(frame)
}

/// `_latest_pipeline_date()`: the most recent date the pipeline has bars for (the M14 flag).
async fn latest_pipeline_date(state: &AppState) -> Result<NaiveDate, Box<Response<Body>>> {
    let dsn = state.settings_snapshot().screener_dsn;
    match scan_source::latest_pipeline_date(&dsn).await {
        Err(exc) => Err(Box::new(detail(
            StatusCode::SERVICE_UNAVAILABLE,
            &format!("SCAN_SOURCE_DEFAULT is 'generated' but the screener is unreachable: {exc}"),
        ))),
        Ok(None) => Err(Box::new(detail(
            StatusCode::SERVICE_UNAVAILABLE,
            "SCAN_SOURCE_DEFAULT is 'generated' but the screener has no bars",
        ))),
        Ok(Some(d)) => Ok(d),
    }
}

// =============================================================================================
// breadth
// =============================================================================================

/// `breadth_source.pipeline_breadth(on, slug)`: the pipeline's 20-DMA breadth for one date and
/// the membership it was measured over, read (only) from the screener's database.
///
/// # Errors
/// The exception class Python's psycopg would raise.
pub async fn pipeline_breadth(
    dsn: &str,
    on: NaiveDate,
    slug: &str,
) -> Result<(Option<f64>, BTreeSet<String>), String> {
    use sqlx::Connection as _;
    use sqlx::Row as _;
    let mut conn = sqlx::postgres::PgConnection::connect(dsn)
        .await
        .map_err(|_| "OperationalError".to_owned())?;
    let class = |e: &sqlx::Error| -> String {
        match e {
            sqlx::Error::Database(d) => match d.code().as_deref() {
                Some("42P01") => "UndefinedTable".to_owned(),
                Some("42703") => "UndefinedColumn".to_owned(),
                _ => "DatabaseError".to_owned(),
            },
            _ => "OperationalError".to_owned(),
        }
    };
    let day = on.format("%Y-%m-%d").to_string();
    let row = sqlx::query(
        "select m.pct_above_20dma::text as pct, m.constituent_count \
           from market_health_daily m join index_def d on d.id = m.index_id \
          where d.slug = $1 and m.date = $2::date",
    )
    .bind(slug)
    .bind(&day)
    .fetch_optional(&mut conn)
    .await
    .map_err(|e| class(&e))?;
    let members_rows = sqlx::query(
        "select i.symbol from index_member_daily m \
           join instrument i on i.id = m.instrument_id \
           join index_def d on d.id = m.index_id \
          where d.slug = $1 and m.date = $2::date",
    )
    .bind(slug)
    .bind(&day)
    .fetch_all(&mut conn)
    .await
    .map_err(|e| class(&e))?;
    let _ = conn.close().await;
    let members: BTreeSet<String> = members_rows
        .iter()
        .filter_map(|r| r.try_get::<String, _>(0).ok())
        .collect();
    let value = row
        .and_then(|r| r.try_get::<Option<String>, _>(0).ok().flatten())
        .and_then(|t| t.trim().parse::<f64>().ok());
    Ok((value, members))
}

/// `breadth_source.breadth_for_plan(scan_breadth, symbols, on)`.
pub async fn breadth_for_plan(
    settings: &Settings,
    scan_breadth: f64,
    symbols: &BTreeSet<String>,
    on: NaiveDate,
) -> PyDict {
    let slug = settings.desk_universe_slug.clone();
    let pipeline = pipeline_breadth(&settings.screener_dsn, on, &slug).await;
    breadth_decision(scan_breadth, symbols, on, &slug, pipeline)
}

/// `breadth_for_plan`'s decision, given what `pipeline_breadth` returned (or the class of the
/// exception it raised): the pipeline's number only when it measured the same population.
#[must_use]
pub fn breadth_decision(
    scan_breadth: f64,
    symbols: &BTreeSet<String>,
    on: NaiveDate,
    slug: &str,
    pipeline: Result<(Option<f64>, BTreeSet<String>), String>,
) -> PyDict {
    let mut result = dict([
        ("value", PyValue::Float(scan_breadth)),
        ("source", PyValue::Str("scan".to_owned())),
        ("scan_value", PyValue::Float(scan_breadth)),
        ("pipeline_value", PyValue::None),
        ("universe", PyValue::Str(slug.to_owned())),
        ("reason", PyValue::Str(String::new())),
    ]);
    let (value, members) = match pipeline {
        Ok(v) => v,
        Err(class) => {
            result.insert(
                "reason",
                PyValue::Str(format!(
                    "pipeline unreachable ({class}); used the scan's own"
                )),
            );
            return result;
        }
    };
    result.insert(
        "pipeline_value",
        value.map_or(PyValue::None, PyValue::Float),
    );
    match value {
        None => {
            result.insert(
                "reason",
                PyValue::Str(format!(
                    "no market_health_daily row for {on}; used the scan's own"
                )),
            );
        }
        Some(_) if members != *symbols => {
            let only_scan = symbols.difference(&members).count();
            let only_index = members.difference(symbols).count();
            result.insert(
                "reason",
                PyValue::Str(format!(
                    "scan universe differs from {slug} on {on} ({only_scan} only in the scan, {only_index} only in the index); used the scan's own"
                )),
            );
        }
        Some(v) => {
            result.insert("value", PyValue::Float(v));
            result.insert("source", PyValue::Str("pipeline".to_owned()));
            result.insert(
                "reason",
                PyValue::Str(format!("same {} symbols as {slug} on {on}", members.len())),
            );
        }
    }
    result
}

/// `_resolve_breadth(df, scan_audit, provenance)`.
async fn resolve_breadth(
    settings: &Settings,
    df: &Frame,
    scan_audit: &PyDict,
    provenance: &PyDict,
) -> PyDict {
    let scan_value = f64_of(get(scan_audit, "breadth_above_20dma")).unwrap_or(f64::NAN);
    let as_of_v = get(provenance, "as_of");
    let raw_date = if as_of_v.truthy() {
        super::plan_store::py_str(as_of_v)
    } else if df.nrows > 0 {
        df.col("date").map(|c| c.py_str(0)).unwrap_or_default()
    } else {
        String::new()
    };
    let head: String = raw_date.chars().take(10).collect();
    let Some(as_of) = date_fromisoformat(&head) else {
        return dict([
            ("value", PyValue::Float(scan_value)),
            ("source", PyValue::Str("scan".to_owned())),
            ("scan_value", PyValue::Float(scan_value)),
            ("pipeline_value", PyValue::None),
            (
                "universe",
                PyValue::Str(settings.desk_universe_slug.clone()),
            ),
            (
                "reason",
                PyValue::Str(format!(
                    "scan carries no usable date ({}); used the scan's own",
                    py_repr_str(&raw_date)
                )),
            ),
        ]);
    };
    let symbols: BTreeSet<String> = df
        .col("symbol")
        .map(|c| (0..df.nrows).map(|i| c.py_str(i)).collect())
        .unwrap_or_default();
    breadth_for_plan(settings, scan_value, &symbols, as_of).await
}

// =============================================================================================
// POST /analyze
// =============================================================================================

/// `POST /analyze`: score a scan, read the account, return the full plan.
async fn analyze(State(state): State<AppState>, headers: HeaderMap, body: Bytes) -> Response<Body> {
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_owned();
    let mut scan: Option<(Option<String>, Vec<u8>)> = None;
    let mut generate_for = String::new();
    if content_type
        .to_ascii_lowercase()
        .starts_with("multipart/form-data")
    {
        for part in parse_multipart(&content_type, &body).unwrap_or_default() {
            if part.name == "scan" && part.filename.is_some() {
                scan = Some((part.filename, part.data));
            } else if part.name == "generate_for" {
                generate_for = String::from_utf8_lossy(&part.data).into_owned();
            }
        }
    } else if content_type
        .to_ascii_lowercase()
        .starts_with("application/x-www-form-urlencoded")
    {
        generate_for = MultiDict::from_form(&body).str_or_empty("generate_for");
    }
    let (cfg, regime_enabled) = load_config(&state).await;
    let scoring = scoring_config(&cfg);
    let settings = state.settings_snapshot();
    let (df, provenance, note) = if !generate_for.is_empty() {
        match generated_scan(&state, &scoring, &generate_for).await {
            Ok(v) => v,
            Err(r) => return *r,
        }
    } else if let Some((filename, raw)) = scan {
        match uploaded_scan(&state, &scoring, filename.as_deref(), &raw) {
            Ok(v) => v,
            Err(r) => return *r,
        }
    } else if settings.scan_source_default == "generated" {
        // M14 §3's flag, off by default: "no file and no date" means "build it".
        let latest = match latest_pipeline_date(&state).await {
            Ok(d) => d,
            Err(r) => return *r,
        };
        match generated_scan(&state, &scoring, &latest.format("%Y-%m-%d").to_string()).await {
            Ok(v) => v,
            Err(r) => return *r,
        }
    } else {
        return detail(
            StatusCode::BAD_REQUEST,
            "send a scan file, or generate_for=YYYY-MM-DD",
        );
    };
    plan_from_scan(&state, &cfg, regime_enabled, &df, provenance, &note).await
}

/// `_plan_from_scan(df, provenance, note)`: everything after the scan arrives, one path.
async fn plan_from_scan(
    state: &AppState,
    cfg: &BasketConfig,
    regime_enabled: bool,
    df: &Frame,
    provenance: PyDict,
    note: &str,
) -> Response<Body> {
    let scan_audit = match audit(df, &cfg.scoring) {
        Ok(a) => a,
        Err(e) => return internal_error(&e.0),
    };
    let scored = match score(df, &cfg.scoring) {
        Ok(s) => s,
        Err(e) => return internal_error(&e.0),
    };
    let Some((_, k)) = desk(state) else {
        return no_desk();
    };
    if !k.is_authed().await {
        return detail(
            StatusCode::UNAUTHORIZED,
            "Kite session expired \u{2014} click Login first.",
        );
    }
    let mut holdings = match k.holdings().await {
        Ok(h) => h,
        Err(e) => return internal_error(&e.to_string()),
    };
    let cash = match k.available_cash().await {
        Ok(c) => c,
        Err(e) => return internal_error(&e.to_string()),
    };
    let mut wanted: BTreeSet<String> = BTreeSet::new();
    for i in 0..scored.len() {
        if scored.reject.get(i).is_some_and(String::is_empty) {
            wanted.insert(scored.symbol(i));
        }
    }
    for h in &holdings {
        wanted.insert(super::plan_store::py_str(get(h, "symbol")));
    }
    let wanted: Vec<String> = wanted.into_iter().collect();
    let live = k.ltp(&wanted, "NSE").await.unwrap_or_default();
    for h in &mut holdings {
        let sym = super::plan_store::py_str(get(h, "symbol"));
        if let Some(p) = live.get(&sym) {
            h.insert("last_price", p.clone());
        }
    }
    let settings = state.settings_snapshot();
    let breadth = resolve_breadth(&settings, df, &scan_audit, &provenance).await;
    let live_prices: Vec<(String, f64)> = live
        .iter()
        .filter_map(|(s, v)| f64_of(v).map(|p| (s.to_owned(), p)))
        .collect();
    let clusters = load_clusters(state);
    let plan_id: String = state.ids.uuid4_hex().chars().take(12).collect();
    let inputs = PlanInputs {
        holdings,
        cash,
        live_prices,
        breadth_override: f64_of(get(&breadth, "value")),
        clusters,
        plan_id,
    };
    let mut plan = match build_plan(&scored, &inputs, cfg) {
        Ok(p) => p,
        Err(e) => return internal_error(&e.0),
    };
    plan.insert("audit", PyValue::Map(scan_audit));
    plan.insert("breadth", PyValue::Map(breadth));
    let screen_run_id = super::plan_store::py_str(get(&provenance, "screen_run_id"));
    plan.insert("scan", PyValue::Map(provenance));
    let orders = orders_of(&plan);
    let funding = funding_check(state, &k, &orders).await;
    plan.insert("funding", PyValue::Map(funding));
    let created_at = state.clock.time();
    plan.insert("created_at", PyValue::Float(created_at));
    let plan_id = super::plan_store::py_str(get(&plan, "plan_id"));

    // Persist and stamp with the regime evaluation current at build; never blocks the desk.
    if let Ok(mut conn) = state.db.connect().await
        && conn.migrate().await.is_ok()
    {
        // `_rs.latest_evaluation(conn) if C.REGIME_ENABLED else None`: a failing read raises
        // inside Python's try, so the plan is neither stamped nor saved (`Err(())` here).
        let ev: Result<PyValue, ()> = if regime_enabled {
            match conn
                .execute(
                    "SELECT * FROM regime_evaluations ORDER BY scheduled_week_end DESC, id DESC LIMIT 1",
                    &[],
                )
                .await
            {
                Ok(c) => Ok(c
                    .fetchone()
                    .and_then(|r| r.get("evaluation_id").map(web::db_to_py))
                    .unwrap_or(PyValue::None)),
                Err(_) => Err(()),
            }
        } else {
            Ok(PyValue::None)
        };
        if let Ok(ev) = ev {
            plan.insert("evaluation_id", ev.clone());
            let ev_id = match &ev {
                PyValue::Str(s) => Some(s.clone()),
                PyValue::None => None,
                other => Some(super::plan_store::py_str(other)),
            };
            let _ = plan_store::save_plan(
                &mut conn,
                &plan,
                ev_id.as_deref(),
                &format!("{note} [{screen_run_id}]"),
                state.clock.time(),
            )
            .await;
        }
    }
    state.plans.insert(Plan {
        plan_id,
        created_at,
        body: plan.clone(),
    });
    web::json(StatusCode::OK, &PyValue::Map(plan))
}

/// `_load_clusters()`: `data/sectors.csv` (`symbol,cluster`), or empty.
pub fn load_clusters(state: &AppState) -> std::collections::HashMap<String, String> {
    let mut out = std::collections::HashMap::new();
    let Ok(raw) = std::fs::read(state.paths.resolve("data/sectors.csv")) else {
        return out;
    };
    let Ok(frame) = read_csv(&raw) else {
        return out;
    };
    if let (Some(s), Some(c)) = (frame.col("symbol"), frame.col("cluster")) {
        for i in 0..frame.nrows {
            out.insert(s.py_str(i), c.py_str(i));
        }
    }
    out
}

// =============================================================================================
// POST /execute
// =============================================================================================

/// `POST /execute`: execute a reviewed plan. Hard gates first: `confirm == "true"`, a plan
/// this process issued, younger than 30 minutes.
async fn execute(State(state): State<AppState>, headers: HeaderMap, body: Bytes) -> Response<Body> {
    let form = form_fields(&headers, &body);
    let (Some(plan_id), Some(confirm)) = (form.get("plan_id"), form.get("confirm")) else {
        let missing: Vec<&str> = ["plan_id", "confirm"]
            .into_iter()
            .filter(|n| form.get(n).is_none())
            .collect();
        return missing_fields("body", &missing);
    };
    let plan_id = plan_id.to_owned();
    let stored = state.plans.get(&plan_id);
    let plan = match check_execute(confirm, stored.as_ref(), state.clock.time()) {
        Ok(p) => p.clone(),
        Err(refusal) => {
            let status = StatusCode::from_u16(refusal.status).unwrap_or(StatusCode::BAD_REQUEST);
            return detail(status, refusal.detail);
        }
    };
    let Some((gw, k)) = desk(&state) else {
        return no_desk();
    };
    // `gateway()` builds the risk manager on first use (limits from the latest NAV).
    let risk = gw.risk().await.ok();
    let body_plan = plan.body.clone();

    // The daily-loss cap, measured before any order.
    let nav_prev = latest_nav(&state.db).await;
    if nav_prev > 0.0
        && let Some(risk) = &risk
        && let Ok(mut conn) = state.db.connect().await
        && let Ok(cur) = conn
            .execute(
                "SELECT date, nav, invested, cash, index_value FROM snapshots ORDER BY date",
                &[],
            )
            .await
    {
        let prev_invested = cur
            .rows
            .last()
            .and_then(|r| r.get("invested"))
            .and_then(crate::storage::DbValue::as_f64)
            .unwrap_or(0.0);
        if prev_invested > 0.0 {
            let book = get(&body_plan, "book_value");
            let book = if book.truthy() {
                f64_of(book).unwrap_or(0.0)
            } else {
                0.0
            };
            let _ = risk.on_pnl(book - prev_invested).await;
        }
    }

    let orders = orders_of(&body_plan);
    let preflight = funding_check(&state, &k, &orders).await;

    let mut lines = match plan_lines(&orders) {
        Ok(l) => l,
        Err(e) => return internal_error(&e),
    };
    // Sells first (frees cash), then buys, largest first: `(delta > 0, -|delta·ref_price|)`.
    #[allow(clippy::cast_precision_loss)]
    lines.sort_by(|a, b| {
        let ka = (a.delta > 0, -((a.delta as f64) * a.ref_price).abs());
        let kb = (b.delta > 0, -((b.delta as f64) * b.ref_price).abs());
        ka.0.cmp(&kb.0)
            .then(ka.1.partial_cmp(&kb.1).unwrap_or(std::cmp::Ordering::Equal))
    });
    let gross = {
        let b = get(&body_plan, "book_value");
        if b.truthy() {
            f64_of(b).unwrap_or(0.0)
        } else {
            0.0
        }
    };
    let mut results: Vec<PyDict> = Vec::new();
    let mut recent_errors: Vec<String> = Vec::new();
    let mut aborted_for = String::new();
    for (idx, o) in lines.iter().enumerate() {
        let order = PlaceOrder {
            symbol: o.symbol.clone(),
            qty: o.delta.abs(),
            side: o.side().to_owned(),
            product: "CNC".to_owned(),
            order_type: "LIMIT".to_owned(),
            price: Some(o.ref_price),
            exchange: "NSE".to_owned(),
            client_id: Some(format!("{plan_id}:{}", o.symbol)),
            gross_exposure: Some(gross),
            ..PlaceOrder::default()
        };
        let mut res = match state.gateway.place(order).await {
            Ok(r) => r,
            Err(e) if e.python_type == "UntouchableInstrumentError" => dict([
                ("symbol", PyValue::Str(o.symbol.clone())),
                ("status", PyValue::Str("BLOCKED".to_owned())),
                ("error", PyValue::Str(e.message.clone())),
            ]),
            Err(e) => return internal_error(&e.to_string()),
        };
        res.insert("action", PyValue::Str(o.action.clone()));
        let status = super::plan_store::py_str(get(&res, "status"));
        let err = if FAILED_STATUSES.contains(&status.as_str()) {
            let e = get(&res, "error");
            let text = if e.truthy() {
                super::plan_store::py_str(e)
            } else {
                String::new()
            };
            text.chars().take(80).collect::<String>()
        } else {
            String::new()
        };
        results.push(res);
        if err.is_empty() {
            recent_errors.clear();
        } else {
            recent_errors.push(err);
            if recent_errors.len() > BREAKER_TRIP {
                recent_errors.remove(0);
            }
        }
        if recent_errors.len() == BREAKER_TRIP
            && recent_errors.iter().all(|e| *e == recent_errors[0])
        {
            aborted_for = recent_errors[0].clone();
            for rest in lines.iter().skip(idx + 1) {
                results.push(dict([
                    ("symbol", PyValue::Str(rest.symbol.clone())),
                    ("status", PyValue::Str("ABORTED".to_owned())),
                    ("action", PyValue::Str(rest.action.clone())),
                    (
                        "error",
                        PyValue::Str(format!(
                            "batch stopped after {BREAKER_TRIP} identical failures: {aborted_for}"
                        )),
                    ),
                ]));
            }
            break;
        }
    }

    let stops_note = "stops are armed from /stops once fills are known; sizing them here would use planned quantities, not filled ones";
    let log_path = format!("data/outputs/execution_{plan_id}.json");
    let results_py = PyValue::List(results.iter().cloned().map(PyValue::Map).collect());
    let log = PyValue::Map(dict([
        ("plan", PyValue::Map(body_plan.clone())),
        ("orders", results_py.clone()),
        ("gtt", PyValue::List(Vec::new())),
    ]));
    if std::fs::write(state.paths.resolve(&log_path), dumps_indent2(&log)).is_err() {
        return internal_error("could not write the execution log");
    }

    let mut recon = PyDict::new();
    if let Ok(mut conn) = state.db.connect().await
        && conn.migrate().await.is_ok()
        && plan_store::record_execution(&mut conn, &plan_id, &results)
            .await
            .is_ok()
        && let Ok(r) = plan_store::reconciliation(&mut conn, &plan_id).await
    {
        recon = r;
    }

    let settings = state.settings_snapshot();
    web::json(
        StatusCode::OK,
        &PyValue::Map(dict([
            ("dry_run", PyValue::Bool(settings.dry_run)),
            ("orders", results_py),
            ("gtt", PyValue::List(Vec::new())),
            ("stops_note", PyValue::Str(stops_note.to_owned())),
            ("stops_pending", PyValue::Bool(true)),
            ("preflight", PyValue::Map(preflight)),
            ("aborted_for", PyValue::Str(aborted_for)),
            ("log", PyValue::Str(log_path)),
            ("reconciliation", PyValue::Map(recon)),
        ])),
    )
}

/// The lines `/execute` sends: `[o for o in plan["orders"] if o["delta"] != 0]`, every row
/// read **before any order is sent**.
///
/// Python evaluates `o["delta"]` on every row and the sort key `delta * ref_price` on every
/// non-zero row before its loop, so a row missing either (or holding a non-number) is a
/// `KeyError`/`TypeError` — a 500 with nothing sent — and so it is here. Two rows Python would
/// treat differently are refused the same way here, before anything is sent (DECISIONS-RS
/// R8.WP2, stricter): a non-zero row missing `symbol` or `action` (Python sends the rows sorted
/// before it, then raises mid-batch), and a `delta` that is not a whole number (Python would
/// hand the gateway a float quantity). A whole-number float delta is sent as that integer.
/// No plan `build_plan` makes has any of these: every delta is an `int`.
///
/// # Errors
/// The exception Python would raise.
pub fn plan_lines(orders: &[PyDict]) -> Result<Vec<PlanLine>, String> {
    let mut out = Vec::new();
    for row in orders {
        #[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
        let delta = match row.get("delta") {
            None => return Err("KeyError: 'delta'".to_owned()),
            Some(PyValue::Int(i)) => *i,
            Some(PyValue::Bool(b)) => i64::from(*b),
            Some(PyValue::Float(f)) if f.is_finite() && f.fract() == 0.0 && f.abs() < 9.0e15 => {
                *f as i64
            }
            Some(other) => {
                return Err(format!(
                    "TypeError: an order delta of {other:?} cannot be sent as a share quantity"
                ));
            }
        };
        if delta == 0 {
            continue;
        }
        #[allow(clippy::cast_precision_loss)]
        let ref_price = match row.get("ref_price") {
            None => return Err("KeyError: 'ref_price'".to_owned()),
            Some(PyValue::Float(f)) => *f,
            Some(PyValue::Int(i)) => *i as f64,
            Some(PyValue::Bool(b)) => f64::from(u8::from(*b)),
            Some(_) => return Err("TypeError: bad operand type for abs()".to_owned()),
        };
        let symbol = match row.get("symbol") {
            Some(PyValue::Str(s)) => s.clone(),
            _ => return Err("KeyError: 'symbol'".to_owned()),
        };
        let action = match row.get("action") {
            Some(PyValue::Str(s)) => s.clone(),
            _ => return Err("KeyError: 'action'".to_owned()),
        };
        out.push(PlanLine {
            symbol,
            action,
            delta,
            ref_price,
            row: row.clone(),
        });
    }
    Ok(out)
}

/// The request's form fields as Starlette's `request.form()` gives them to `Form(...)`
/// parameters: an `application/x-www-form-urlencoded` body, or the non-file parts of a
/// `multipart/form-data` one — which is what the desk's own page sends (`new FormData()` in
/// `index.html`'s `execute()`).
fn form_fields(headers: &HeaderMap, body: &[u8]) -> MultiDict {
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if content_type
        .to_ascii_lowercase()
        .starts_with("multipart/form-data")
    {
        return MultiDict(
            parse_multipart(content_type, body)
                .unwrap_or_default()
                .into_iter()
                .filter(|p| p.filename.is_none())
                .map(|p| (p.name, String::from_utf8_lossy(&p.data).into_owned()))
                .collect(),
        );
    }
    if content_type
        .to_ascii_lowercase()
        .starts_with("application/x-www-form-urlencoded")
    {
        return MultiDict::from_form(body);
    }
    MultiDict::default()
}

/// FastAPI's 422 when required `Form(...)` fields are absent: one `missing` item per field,
/// in the signature's order.
fn missing_fields(location: &str, names: &[&str]) -> Response<Body> {
    let items: Vec<PyValue> = names
        .iter()
        .map(|name| {
            PyValue::Map(dict([
                ("type", PyValue::Str("missing".to_owned())),
                (
                    "loc",
                    PyValue::List(vec![
                        PyValue::Str(location.to_owned()),
                        PyValue::Str((*name).to_owned()),
                    ]),
                ),
                ("msg", PyValue::Str("Field required".to_owned())),
                ("input", PyValue::None),
            ]))
        })
        .collect();
    web::json(
        StatusCode::UNPROCESSABLE_ENTITY,
        &PyValue::Map(dict([("detail", PyValue::List(items))])),
    )
}

// =============================================================================================
// json.dump(obj, f, indent=2, default=str)
// =============================================================================================

/// `json.dumps(value, indent=2, default=str)`: Python's pretty JSON (`ensure_ascii`, `NaN`,
/// `", "`-free separators `","` / `": "`), what the execution log is written with.
#[must_use]
pub fn dumps_indent2(value: &PyValue) -> String {
    let mut out = String::new();
    write_indent(value, 0, &mut out);
    out
}

fn write_indent(value: &PyValue, level: usize, out: &mut String) {
    let pad = |n: usize| " ".repeat(n * 2);
    match value {
        PyValue::List(items) if !items.is_empty() => {
            out.push_str("[\n");
            for (i, v) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                out.push_str(&pad(level + 1));
                write_indent(v, level + 1, out);
            }
            out.push('\n');
            out.push_str(&pad(level));
            out.push(']');
        }
        PyValue::Map(m) if !m.is_empty() => {
            out.push_str("{\n");
            for (i, (k, v)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                out.push_str(&pad(level + 1));
                out.push_str(&baskfy_providers::py::json_str(k));
                out.push_str(": ");
                write_indent(v, level + 1, out);
            }
            out.push('\n');
            out.push_str(&pad(level));
            out.push('}');
        }
        PyValue::Date(d) => out.push_str(&baskfy_providers::py::json_str(
            &d.format("%Y-%m-%d").to_string(),
        )),
        PyValue::DateTime(_) | PyValue::Bytes(_) => {
            out.push_str(&crate::lanes::exec_core::journal::dumps_allow_nan(value));
        }
        other => out.push_str(&crate::lanes::exec_core::journal::dumps_allow_nan(other)),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn multipart_parses_a_file_and_a_field() {
        let body = b"--XyZ\r\nContent-Disposition: form-data; name=\"generate_for\"\r\n\r\n2026-08-18\r\n--XyZ\r\nContent-Disposition: form-data; name=\"scan\"; filename=\"a b.csv\"\r\nContent-Type: text/csv\r\n\r\nsymbol\r\nAAA\r\n--XyZ--\r\n";
        let parts = parse_multipart("multipart/form-data; boundary=XyZ", body).unwrap();
        assert_eq!(parts.len(), 2);
        assert_eq!(parts[0].name, "generate_for");
        assert_eq!(parts[0].data, b"2026-08-18");
        assert_eq!(parts[1].filename.as_deref(), Some("a b.csv"));
        assert_eq!(parts[1].data, b"symbol\r\nAAA");
    }

    #[test]
    fn filenames_are_sanitised_per_character() {
        assert_eq!(safe_filename("a b/é.csv"), "a_b__.csv");
    }

    #[test]
    fn indent_matches_python() {
        let v = PyValue::Map(dict([
            (
                "a",
                PyValue::List(vec![PyValue::Int(1), PyValue::Float(2.5)]),
            ),
            ("b", PyValue::Map(PyDict::new())),
            ("c", PyValue::List(Vec::new())),
            ("d", PyValue::Str("\u{2014}".to_owned())),
        ]));
        assert_eq!(
            dumps_indent2(&v),
            "{\n  \"a\": [\n    1,\n    2.5\n  ],\n  \"b\": {},\n  \"c\": [],\n  \"d\": \"\\u2014\"\n}"
        );
    }

    /// The desk's page posts `/execute` as `new FormData()` (multipart); FastAPI's `Form(...)`
    /// reads it the same as a urlencoded body. A desk that read only urlencoded would refuse
    /// every real click with a 422.
    #[test]
    fn execute_reads_its_form_from_multipart_as_the_page_sends_it() {
        let mut h = HeaderMap::new();
        h.insert(
            header::CONTENT_TYPE,
            "multipart/form-data; boundary=B".parse().unwrap(),
        );
        let body = b"--B\r\nContent-Disposition: form-data; name=\"plan_id\"\r\n\r\nabc123\r\n--B\r\nContent-Disposition: form-data; name=\"confirm\"\r\n\r\ntrue\r\n--B\r\nContent-Disposition: form-data; name=\"place_stops\"\r\n\r\ntrue\r\n--B--\r\n";
        let f = form_fields(&h, body);
        assert_eq!(f.get("plan_id"), Some("abc123"));
        assert_eq!(f.get("confirm"), Some("true"));
        let mut u = HeaderMap::new();
        u.insert(
            header::CONTENT_TYPE,
            "application/x-www-form-urlencoded".parse().unwrap(),
        );
        assert_eq!(
            form_fields(&u, b"plan_id=x&confirm=true").get("plan_id"),
            Some("x")
        );
        // Neither form type: Starlette's request.form() is empty, so both fields are missing.
        assert_eq!(
            form_fields(&HeaderMap::new(), b"plan_id=x").get("plan_id"),
            None
        );
    }

    #[test]
    fn every_missing_form_field_is_listed_like_fastapi() {
        let r = missing_fields("body", &["plan_id", "confirm"]);
        assert_eq!(r.status(), StatusCode::UNPROCESSABLE_ENTITY);
    }

    /// `STOP_MAX` is a settings-page key (`app/analytics/settings.py`); Python applies it to
    /// `app.config`, so the plan's stops read it.
    #[test]
    fn a_stored_stop_max_override_reaches_the_plan_config() {
        let mut o = std::collections::BTreeMap::new();
        o.insert("STOP_MAX".to_owned(), SVal::Float(0.10));
        o.insert("STOP_MIN".to_owned(), SVal::Float(0.07));
        let cfg = weekly_config(&Settings::defaults().unwrap(), &o);
        assert!((cfg.scoring.stop_max - 0.10).abs() < 1e-12);
        assert!((cfg.scoring.stop_min - 0.07).abs() < 1e-12);
    }

    #[test]
    fn isoformat_dates() {
        assert!(date_fromisoformat("2026-08-18").is_some());
        assert!(date_fromisoformat("20260818").is_some());
        assert!(date_fromisoformat("18-08-2026").is_none());
        // Python 3.12's other accepted forms and its refusals (probed in the desk's venv).
        let d = |s: &str| date_fromisoformat(s).map(|d| d.to_string());
        assert_eq!(d("2026-W34-2").as_deref(), Some("2026-08-18"));
        assert_eq!(d("2026W342").as_deref(), Some("2026-08-18"));
        assert_eq!(d("2026-W34").as_deref(), Some("2026-08-17"));
        assert_eq!(d("2026W34").as_deref(), Some("2026-08-17"));
        assert_eq!(d("2026-W53-1").as_deref(), Some("2026-12-28"));
        for bad in [
            "2026-230",
            "2026230",
            "2026-0818",
            "202608-18",
            "2026-02-30",
            "2026-W00-1",
            "2026-W34-8",
            "2026-08-18 ",
            "0000-01-01",
            "2026-w34-2",
            "+2026-08-1",
            "2026-8-180",
            " 2026-08-1",
        ] {
            assert_eq!(d(bad), None, "{bad}");
        }
        assert_eq!(py_repr_str("it's"), "\"it's\"");
    }
}
