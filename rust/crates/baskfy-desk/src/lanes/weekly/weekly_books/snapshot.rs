//! `app/analytics/snapshot.py` — the daily EOD snapshot's **collection** (`SnapshotJob.collect`).
//!
//! Reads only; places no orders. Holdings arrive from the desk's `Kite.holdings` wrapper, which
//! already sums `quantity + t1_quantity + collateral_quantity` — pledged shares count as held and
//! `pledged_qty` is carried as information. Untouchable instruments (SGB/G-sec) are classified by
//! the one guard rule, recorded in `holdings_json` and excluded from NAV unless
//! `INCLUDE_UNTOUCHABLE_IN_NAV`.
//!
//! [`run`] collects and persists (`db.save_snapshot`: upsert + `rechain_index`, through
//! [`super::snapshot_db`]); [`cli`] is `python -m app.analytics.snapshot` — the cashflow
//! bookkeeping, the unrecorded-flow check and one snapshot — returning its exit code and the lines
//! it prints. The rate-limiter slot Python takes before each call is the [`SnapshotKite`]
//! implementor's: the desk's Kite wrapper ([`crate::lanes::exec_core::kite_client::DeskKite`])
//! takes its own per-family slot on every read.

use std::path::Path;

use chrono::NaiveDate;

use super::{pyf, pyi, round_to};
use crate::lanes::weekly::basket::tradeable;
use crate::wire::{PyDict, PyValue, dict};

/// `INSTRUMENT_CACHE`.
pub const INSTRUMENT_CACHE: &str = "data/.instruments_{exchange}.json";

/// The reads the job makes (`Kite.is_authed`, `holdings`, `available_cash`, `quote_raw`,
/// `instruments`, `historical`).
#[async_trait::async_trait]
pub trait SnapshotKite: Send + Sync {
    /// `is_authed()`.
    async fn is_authed(&self) -> bool;
    /// `holdings()` — the desk's summed rows (`symbol`, `exchange`, `quantity`, `pledged_qty`,
    /// `average_price`, `last_price`).
    async fn holdings(&self) -> Result<Vec<PyDict>, String>;
    /// `available_cash()`.
    async fn available_cash(&self) -> Result<f64, String>;
    /// `quote_raw(keys)`.
    async fn quote_raw(&self, keys: &[String]) -> Result<PyDict, String>;
    /// `instruments(exchange)`.
    async fn instruments(&self, exchange: &str) -> Result<Vec<PyValue>, String>;
    /// `historical(token, day, day, "day")`.
    async fn historical(&self, token: i64, day: NaiveDate) -> Result<Vec<PyValue>, String>;
}

/// Why a snapshot was not taken.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SnapshotError {
    /// Python's `ValueError` (nothing is written).
    #[error("{0}")]
    Value(String),
    /// Python's `RuntimeError` (no session).
    #[error("{0}")]
    Runtime(String),
    /// A Kite read failed.
    #[error("{0}")]
    Kite(String),
}

/// `is_untouchable(symbol)`: the guard's rule, not a second list.
#[must_use]
pub fn is_untouchable(symbol: &str) -> bool {
    !tradeable(symbol)
}

/// The collected snapshot (`collect()`'s dict).
#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    /// `date`.
    pub date: String,
    /// `nav`.
    pub nav: f64,
    /// `invested`.
    pub invested: f64,
    /// `cash`.
    pub cash: f64,
    /// `holdings_json` (compact `json.dumps`).
    pub holdings_json: String,
}

/// Options for [`collect`].
#[derive(Debug, Clone)]
pub struct CollectOptions<'a> {
    /// `on_date` (default today).
    pub on_date: Option<&'a str>,
    /// `holdings` | `quote` | `historical`.
    pub price_source: &'a str,
    /// The cash balance on `on_date`, when backfilling.
    pub cash: Option<f64>,
    /// Accept today's cash for a past date.
    pub allow_stale_cash: bool,
    /// `date.today()`.
    pub today: NaiveDate,
    /// `config.INCLUDE_UNTOUCHABLE_IN_NAV`.
    pub include_untouchable_in_nav: bool,
    /// The desk's run directory (`data/` lives under it).
    pub root: &'a Path,
}

fn num_or(v: Option<&PyValue>) -> f64 {
    pyf(v)
}

/// Compact `json.dumps(value, separators=(",", ":"))` (Python's defaults otherwise).
fn json_compact(v: &PyValue, out: &mut String) -> Result<(), SnapshotError> {
    use baskfy_providers::py::{json_str, repr_float};
    match v {
        PyValue::None => out.push_str("null"),
        PyValue::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        PyValue::Int(i) => out.push_str(&i.to_string()),
        PyValue::Float(f) => {
            if !f.is_finite() {
                return Err(SnapshotError::Value(
                    "Out of range float values are not JSON compliant".into(),
                ));
            }
            out.push_str(&repr_float(*f));
        }
        PyValue::Str(s) => out.push_str(&json_str(s)),
        PyValue::List(items) => {
            out.push('[');
            for (i, x) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                json_compact(x, out)?;
            }
            out.push(']');
        }
        PyValue::Map(m) => {
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&json_str(k));
                out.push(':');
                json_compact(x, out)?;
            }
            out.push('}');
        }
        other => {
            return Err(SnapshotError::Value(format!(
                "Object of type {} is not JSON serializable",
                other.py_repr()
            )));
        }
    }
    Ok(())
}

async fn instrument_tokens(
    kite: &dyn SnapshotKite,
    symbols: &[String],
    root: &Path,
    today: NaiveDate,
) -> Result<Vec<(String, i64)>, SnapshotError> {
    let path = root.join(INSTRUMENT_CACHE.replace("{exchange}", "NSE"));
    let today_iso = today.format("%Y-%m-%d").to_string();
    let mut rows: Option<Vec<PyValue>> = None;
    if let Ok(bytes) = std::fs::read(&path)
        && let Ok(PyValue::Map(blob)) = PyValue::from_json(&bytes)
        && blob.get("fetched") == Some(&PyValue::Str(today_iso.clone()))
        && let Some(PyValue::List(r)) = blob.get("rows")
    {
        rows = Some(r.clone());
    }
    let rows = match rows {
        Some(r) => r,
        None => {
            let dump = kite.instruments("NSE").await.map_err(SnapshotError::Kite)?;
            let rows: Vec<PyValue> = dump
                .iter()
                .filter_map(|r| match r {
                    PyValue::Map(m) => Some(PyValue::Map(dict([
                        (
                            "tradingsymbol",
                            m.get("tradingsymbol").cloned().unwrap_or(PyValue::None),
                        ),
                        (
                            "instrument_token",
                            m.get("instrument_token").cloned().unwrap_or(PyValue::None),
                        ),
                        (
                            "segment",
                            m.get("segment")
                                .cloned()
                                .unwrap_or(PyValue::Str(String::new())),
                        ),
                        (
                            "name",
                            m.get("name")
                                .cloned()
                                .unwrap_or(PyValue::Str(String::new())),
                        ),
                    ]))),
                    _ => None,
                })
                .collect();
            let blob = PyValue::Map(dict([
                ("fetched", PyValue::Str(today_iso)),
                ("rows", PyValue::List(rows.clone())),
            ]));
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).map_err(|e| SnapshotError::Kite(e.to_string()))?;
            }
            let text = baskfy_providers::kite::py_json_dumps(&blob)
                .map_err(|e| SnapshotError::Value(e.to_string()))?;
            std::fs::write(&path, text).map_err(|e| SnapshotError::Kite(e.to_string()))?;
            rows
        }
    };
    let mut out = Vec::new();
    for r in rows {
        let PyValue::Map(m) = r else { continue };
        let sym = m
            .get("tradingsymbol")
            .map_or_else(String::new, PyValue::py_str);
        if symbols.contains(&sym) {
            let tok = pyi(m.get("instrument_token"));
            if let Some(slot) = out.iter_mut().find(|(s, _): &&mut (String, i64)| *s == sym) {
                slot.1 = tok;
            } else {
                out.push((sym, tok));
            }
        }
    }
    Ok(out)
}

async fn prices(
    kite: &dyn SnapshotKite,
    holdings: &[PyDict],
    on_date: &str,
    source: &str,
    opts: &CollectOptions<'_>,
) -> Result<Vec<(String, f64)>, SnapshotError> {
    let sym = |h: &PyDict| h.get("symbol").map_or_else(String::new, PyValue::py_str);
    match source {
        "holdings" => Ok(holdings
            .iter()
            .map(|h| (sym(h), num_or(h.get("last_price"))))
            .collect()),
        "quote" => {
            let keys: Vec<String> = holdings
                .iter()
                .map(|h| {
                    format!(
                        "{}:{}",
                        h.get("exchange").map_or_else(String::new, PyValue::py_str),
                        sym(h)
                    )
                })
                .collect();
            let mut out = Vec::new();
            for chunk in keys.chunks(200) {
                let got = kite.quote_raw(chunk).await.map_err(SnapshotError::Kite)?;
                for (k, v) in got.iter() {
                    let PyValue::Map(v) = v else { continue };
                    let close = match v.get("ohlc") {
                        Some(PyValue::Map(o)) => o.get("close").cloned(),
                        _ => None,
                    };
                    let px = match close {
                        Some(c) if c.truthy() => pyf(Some(&c)),
                        _ => pyf(v.get("last_price")),
                    };
                    let name = k.split_once(':').map_or(k, |(_, s)| s).to_owned();
                    out.push((name, px));
                }
            }
            Ok(out)
        }
        "historical" => {
            let symbols: Vec<String> = holdings.iter().map(sym).collect();
            let tokens = instrument_tokens(kite, &symbols, opts.root, opts.today).await?;
            let day = NaiveDate::parse_from_str(on_date, "%Y-%m-%d").map_err(|_| {
                SnapshotError::Value(format!("Invalid isoformat string: {on_date:?}"))
            })?;
            let mut out: Vec<(String, f64)> = Vec::new();
            let mut missing = Vec::new();
            for s in &symbols {
                let tok = tokens.iter().find(|(t, _)| t == s).map_or(0, |(_, k)| *k);
                if tok == 0 {
                    missing.push(s.clone());
                    continue;
                }
                let candles = kite
                    .historical(tok, day)
                    .await
                    .map_err(SnapshotError::Kite)?;
                match candles.last() {
                    Some(PyValue::Map(c)) => out.push((s.clone(), pyf(c.get("close")))),
                    _ => missing.push(s.clone()),
                }
            }
            if out.is_empty() {
                return Err(SnapshotError::Value(format!(
                    "no daily candles for any holding on {on_date} — market holiday or weekend? \
                     Nothing was written."
                )));
            }
            if !missing.is_empty() {
                for h in holdings {
                    let s = sym(h);
                    if !out.iter().any(|(k, _)| *k == s) {
                        out.push((s, num_or(h.get("last_price"))));
                    }
                }
            }
            Ok(out)
        }
        other => Err(SnapshotError::Value(format!(
            "unknown price source: {}",
            baskfy_providers::py::repr_str(other)
        ))),
    }
}

/// `SnapshotJob.collect(on_date, price_source, cash=, allow_stale_cash=)`.
///
/// # Errors
/// No session (`Runtime`), a backfill without cash, a holiday, an unknown source (`Value`), or a
/// failed read (`Kite`).
#[allow(clippy::cast_precision_loss)]
pub async fn collect(
    kite: &dyn SnapshotKite,
    opts: &CollectOptions<'_>,
) -> Result<Snapshot, SnapshotError> {
    if !kite.is_authed().await {
        return Err(SnapshotError::Runtime(
            "Kite session expired — log in at / first (token is daily).".into(),
        ));
    }
    let today = opts.today.format("%Y-%m-%d").to_string();
    let date = opts.on_date.map_or_else(|| today.clone(), str::to_owned);
    let holdings = kite.holdings().await.map_err(SnapshotError::Kite)?;
    let cash = match opts.cash {
        Some(c) => c,
        None => {
            if date < today && !opts.allow_stale_cash {
                return Err(SnapshotError::Value(format!(
                    "backfilling {date}: kc.margins() only knows today's cash. Pass cash=<balance \
                     on {date}> (CLI: --cash), or allow_stale_cash=True (CLI: \
                     --assume-current-cash) if you accept today's balance."
                )));
            }
            kite.available_cash().await.map_err(SnapshotError::Kite)?
        }
    };
    let backfilled = date < today;
    let px = prices(kite, &holdings, &date, opts.price_source, opts).await?;

    let mut positions: Vec<(f64, PyValue)> = Vec::new();
    let (mut invested, mut excluded_value) = (0.0, 0.0);
    for h in &holdings {
        let sym = h.get("symbol").map_or_else(String::new, PyValue::py_str);
        let price = px
            .iter()
            .rev()
            .find(|(s, _)| *s == sym)
            .map_or_else(|| num_or(h.get("last_price")), |(_, p)| *p);
        let qty = pyi(h.get("quantity"));
        let value = qty as f64 * price;
        let excluded = is_untouchable(&sym);
        positions.push((
            round_to(value, 2),
            PyValue::Map(dict([
                ("symbol", PyValue::Str(sym)),
                (
                    "exchange",
                    h.get("exchange").cloned().unwrap_or(PyValue::None),
                ),
                (
                    "quantity",
                    h.get("quantity").cloned().unwrap_or(PyValue::None),
                ),
                (
                    "pledged_qty",
                    h.get("pledged_qty").cloned().unwrap_or(PyValue::None),
                ),
                (
                    "average_price",
                    PyValue::Float(round_to(num_or(h.get("average_price")), 4)),
                ),
                ("price", PyValue::Float(round_to(price, 4))),
                ("value", PyValue::Float(round_to(value, 2))),
                ("excluded", PyValue::Bool(excluded)),
            ])),
        ));
        if excluded && !opts.include_untouchable_in_nav {
            excluded_value += value;
        } else {
            invested += value;
        }
    }
    positions.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    let holdings_json = PyValue::Map(dict([
        ("as_of", PyValue::Str(date.clone())),
        ("price_source", PyValue::Str(opts.price_source.to_owned())),
        (
            "include_untouchable_in_nav",
            PyValue::Bool(opts.include_untouchable_in_nav),
        ),
        (
            "excluded_value",
            PyValue::Float(round_to(excluded_value, 2)),
        ),
        ("backfilled", PyValue::Bool(backfilled)),
        (
            "positions",
            PyValue::List(positions.into_iter().map(|(_, p)| p).collect()),
        ),
    ]));
    let mut text = String::new();
    json_compact(&holdings_json, &mut text)?;
    Ok(Snapshot {
        date,
        nav: round_to(invested + cash, 2),
        invested: round_to(invested, 2),
        cash: round_to(cash, 2),
        holdings_json: text,
    })
}

// =============================================================================================
// run() and the CLI
// =============================================================================================

#[async_trait::async_trait]
impl SnapshotKite for crate::lanes::exec_core::kite_client::DeskKite {
    async fn is_authed(&self) -> bool {
        Self::is_authed(self).await
    }
    async fn holdings(&self) -> Result<Vec<PyDict>, String> {
        Self::holdings(self).await.map_err(|e| e.message)
    }
    async fn available_cash(&self) -> Result<f64, String> {
        Self::available_cash(self).await.map_err(|e| e.message)
    }
    async fn quote_raw(&self, keys: &[String]) -> Result<PyDict, String> {
        match Self::quote_raw(self, keys).await.map_err(|e| e.message)? {
            PyValue::Map(m) => Ok(m),
            _ => Ok(PyDict::new()),
        }
    }
    async fn instruments(&self, exchange: &str) -> Result<Vec<PyValue>, String> {
        Self::instruments(self, exchange)
            .await
            .map_err(|e| e.message)
    }
    async fn historical(&self, token: i64, day: NaiveDate) -> Result<Vec<PyValue>, String> {
        let d = day.format("%Y-%m-%d").to_string();
        Self::historical(self, token, &d, &d, "day")
            .await
            .map_err(|e| e.message)
    }
}

/// What [`run`] wrote: the snapshot, the stored index and the write's outcome.
#[derive(Debug, Clone, PartialEq)]
pub struct RunResult {
    /// The collected snapshot.
    pub snapshot: Snapshot,
    /// `index_value` as stored after the rechain.
    pub index_value: f64,
    /// `inserted` | `updated` | `unchanged`.
    pub outcome: &'static str,
}

/// Why `run` stopped.
#[derive(Debug, thiserror::Error)]
pub enum RunError {
    /// The collection (nothing was written).
    #[error(transparent)]
    Snapshot(#[from] SnapshotError),
    /// The database.
    #[error(transparent)]
    Db(#[from] crate::storage::StorageError),
}

/// `run(on_date, price_source=, force=, kite=, cash=, allow_stale_cash=)`: collect, migrate, save
/// (upsert + rechain in one transaction), read the stored index back.
///
/// # Errors
/// The collection's refusals (nothing is written), or the database.
pub async fn run(
    kite: &dyn SnapshotKite,
    opts: &CollectOptions<'_>,
    conn: &mut crate::storage::Conn,
    force: bool,
) -> Result<RunResult, RunError> {
    use super::snapshot_db as db;
    let snap = collect(kite, opts).await?;
    conn.migrate().await?;
    let row = db::SnapRow {
        date: snap.date.clone(),
        nav: snap.nav,
        invested: snap.invested,
        cash: snap.cash,
        holdings_json: snap.holdings_json.clone(),
    };
    let outcome = db::save_snapshot(conn, &row, force).await?;
    let index_value = db::get_snapshot(conn, &snap.date)
        .await?
        .and_then(|r| {
            r.get("index_value")
                .and_then(crate::storage::DbValue::as_f64)
        })
        .unwrap_or(f64::NAN);
    Ok(RunResult {
        snapshot: snap,
        index_value,
        outcome,
    })
}

/// The CLI's arguments (`argparse` in Python).
#[derive(Debug, Clone, PartialEq)]
pub struct CliArgs {
    /// `--date` (default today).
    pub date: Option<String>,
    /// `--prices` (`holdings` | `quote` | `historical`).
    pub prices: String,
    /// `--force`.
    pub force: bool,
    /// `--cash`.
    pub cash: Option<f64>,
    /// `--assume-current-cash`.
    pub assume_current_cash: bool,
    /// `--cashflow`.
    pub cashflow: Option<f64>,
    /// `--cashflow-type`.
    pub cashflow_type: String,
    /// `--import-cashflows`.
    pub import_cashflows: Option<String>,
    /// `--check-flows`.
    pub check_flows: bool,
}

impl Default for CliArgs {
    fn default() -> Self {
        Self {
            date: None,
            prices: "holdings".to_owned(),
            force: false,
            cash: None,
            assume_current_cash: false,
            cashflow: None,
            cashflow_type: "invest".to_owned(),
            import_cashflows: None,
            check_flows: false,
        }
    }
}

const USAGE: &str = "usage: snapshot.py [-h] [--date DATE] [--db DB] [--prices {holdings,quote,historical}] \
[--force] [--cash CASH] [--assume-current-cash] [--cashflow CASHFLOW] [--cashflow-type CASHFLOW_TYPE] \
[--import-cashflows CSV] [--check-flows]";

/// `ap.parse_args(argv)`. `--db` is accepted and ignored: the desk's connection is the caller's.
///
/// # Errors
/// argparse's message (exit status 2).
pub fn parse_cli(argv: &[String]) -> Result<CliArgs, String> {
    let mut a = CliArgs::default();
    let mut it = argv.iter();
    let float = |flag: &str, v: Option<&String>| -> Result<f64, String> {
        let v = v.ok_or_else(|| format!("argument {flag}: expected one argument"))?;
        super::snapshot_db::py_float(v).map_err(|_| {
            format!(
                "argument {flag}: invalid float value: {}",
                PyValue::Str(v.clone()).py_repr()
            )
        })
    };
    while let Some(arg) = it.next() {
        let (flag, inline) = match arg.split_once('=') {
            Some((f, v)) if f.starts_with("--") => (f, Some(v.to_owned())),
            _ => (arg.as_str(), None),
        };
        let mut value = |name: &str| -> Result<String, String> {
            inline
                .clone()
                .or_else(|| it.next().cloned())
                .ok_or_else(|| format!("argument {name}: expected one argument"))
        };
        match flag {
            "--date" => a.date = Some(value("--date")?),
            "--db" => {
                value("--db")?;
            }
            "--prices" => {
                let v = value("--prices")?;
                if !["holdings", "quote", "historical"].contains(&v.as_str()) {
                    return Err(format!(
                        "argument --prices: invalid choice: {} (choose from 'holdings', 'quote', 'historical')",
                        PyValue::Str(v).py_repr()
                    ));
                }
                a.prices = v;
            }
            "--force" => a.force = true,
            "--cash" => a.cash = Some(float("--cash", Some(&value("--cash")?))?),
            "--assume-current-cash" => a.assume_current_cash = true,
            "--cashflow" => a.cashflow = Some(float("--cashflow", Some(&value("--cashflow")?))?),
            "--cashflow-type" => a.cashflow_type = value("--cashflow-type")?,
            "--import-cashflows" => a.import_cashflows = Some(value("--import-cashflows")?),
            "--check-flows" => a.check_flows = true,
            other => return Err(format!("unrecognized arguments: {other}")),
        }
    }
    Ok(a)
}

fn money(x: f64, spec: &str) -> String {
    crate::pyfmt::format_value(&crate::pyfmt::PyArg::Float(x), spec)
        .unwrap_or_else(|_| x.to_string())
}

/// What the CLI did: its exit status, what it printed to stdout, and to stderr.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CliOutcome {
    /// The process exit status.
    pub code: i32,
    /// stdout, one entry per `print`.
    pub stdout: Vec<String>,
    /// stderr (`SystemExit(message)`, argparse's error).
    pub stderr: Vec<String>,
}

/// `python -m app.analytics.snapshot [args]` (`_main`): cashflow bookkeeping first (so the index
/// chain sees it), then `--check-flows`, else one snapshot. `today` is `date.today()`; `dry_run`
/// is printed as Python prints `C.DRY_RUN` (snapshots never place an order either way).
pub async fn cli(
    argv: &[String],
    kite: &dyn SnapshotKite,
    conn: &mut crate::storage::Conn,
    base: CollectOptions<'_>,
    dry_run: bool,
) -> CliOutcome {
    use super::snapshot_db as db;
    let mut out = CliOutcome::default();
    let a = match parse_cli(argv) {
        Ok(a) => a,
        Err(e) => {
            out.code = 2;
            out.stderr.push(USAGE.to_owned());
            out.stderr.push(format!("snapshot.py: error: {e}"));
            return out;
        }
    };
    let date = a
        .date
        .clone()
        .unwrap_or_else(|| base.today.format("%Y-%m-%d").to_string());
    let fail = |mut out: CliOutcome, e: String| {
        out.code = 1;
        out.stderr.push(e);
        out
    };

    if a.cashflow.is_some() || a.import_cashflows.is_some() {
        if let Err(e) = conn.migrate().await {
            return fail(out, e.to_string());
        }
        let mut lines = Vec::new();
        let (cashflow, kind, import, day) = (
            a.cashflow,
            a.cashflow_type.clone(),
            a.import_cashflows.clone(),
            date.clone(),
        );
        let body = conn
            .transaction(move |c| {
                Box::pin(async move {
                    let mut printed = Vec::new();
                    if let Some(amount) = cashflow {
                        let added = db::record_cashflow(c, &day, amount, &kind).await?;
                        printed.push(format!(
                            "cashflow {} ({kind}) on {day}: {}",
                            money(amount, "+,.2f"),
                            if added { "recorded" } else { "already present" }
                        ));
                    }
                    if let Some(path) = import {
                        let res = db::import_cashflows_csv(c, std::path::Path::new(&path))
                            .await
                            .map_err(|e| match e {
                                db::DbError::Storage(s) => s,
                                db::DbError::Python(p) => crate::storage::StorageError::Other(p),
                            })?;
                        printed.push(format!("cashflow import: {}", PyValue::Map(res).py_repr()));
                    }
                    db::rechain_index(c, None).await?;
                    Ok(printed)
                })
            })
            .await;
        match body {
            Ok(p) => lines.extend(p),
            Err(e) => return fail(out, e.to_string()),
        }
        out.stdout.extend(lines);
    }

    if a.check_flows {
        if let Err(e) = conn.migrate().await {
            return fail(out, e.to_string());
        }
        let hits = match db::suggest_unrecorded_cashflows(conn, 0.02).await {
            Ok(h) => h,
            Err(e) => return fail(out, e.to_string()),
        };
        out.stdout
            .push(format!("possible unrecorded cashflows: {}", hits.len()));
        for h in &hits {
            out.stdout.push(format!(
                "  {}  unexplained {} (cash {}, invested {})",
                h.date,
                money(h.unexplained, "+,.0f"),
                money(h.cash_delta, "+,.0f"),
                money(h.invested_delta, "+,.0f")
            ));
        }
        if !(a.cashflow.is_none() && a.import_cashflows.is_none()) {
            return out;
        }
        if !hits.is_empty() {
            out.stdout
                .push("  -> record real ones with --cashflow / --import-cashflows".to_owned());
        }
        return out;
    }

    let opts = CollectOptions {
        on_date: a.date.as_deref(),
        price_source: &a.prices,
        cash: a.cash,
        allow_stale_cash: a.assume_current_cash,
        ..base
    };
    let res = match run(kite, &opts, conn, a.force).await {
        Ok(r) => r,
        Err(RunError::Snapshot(SnapshotError::Value(e))) => {
            return fail(out, format!("snapshot aborted: {e}"));
        }
        Err(e) => return fail(out, e.to_string()),
    };
    let s = &res.snapshot;
    out.stdout.push(format!(
        "DRY_RUN={} (snapshots are read-only — no orders are ever placed)",
        if dry_run { "True" } else { "False" }
    ));
    out.stdout.push(format!(
        "snapshot {}  [{}]  prices={}",
        s.date, res.outcome, a.prices
    ));
    out.stdout
        .push(format!("  nav        Rs {}", money(s.nav, ">16,.2f")));
    out.stdout
        .push(format!("  invested   Rs {}", money(s.invested, ">16,.2f")));
    out.stdout
        .push(format!("  cash       Rs {}", money(s.cash, ">16,.2f")));
    out.stdout
        .push(format!("  index      {}", money(res.index_value, ">19.4f")));
    if res.outcome == "unchanged" {
        out.stdout.push(
            "  (row already existed — values left untouched; pass --force to overwrite)".to_owned(),
        );
    }
    out
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
mod tests {
    use super::*;

    struct Fake {
        authed: bool,
        holdings: Vec<PyDict>,
    }

    #[async_trait::async_trait]
    impl SnapshotKite for Fake {
        async fn is_authed(&self) -> bool {
            self.authed
        }
        async fn holdings(&self) -> Result<Vec<PyDict>, String> {
            Ok(self.holdings.clone())
        }
        async fn available_cash(&self) -> Result<f64, String> {
            Ok(1000.0)
        }
        async fn quote_raw(&self, _keys: &[String]) -> Result<PyDict, String> {
            Ok(PyDict::new())
        }
        async fn instruments(&self, _exchange: &str) -> Result<Vec<PyValue>, String> {
            Ok(Vec::new())
        }
        async fn historical(&self, _token: i64, _day: NaiveDate) -> Result<Vec<PyValue>, String> {
            Ok(Vec::new())
        }
    }

    fn h(sym: &str, qty: i64, ltp: f64, pledged: i64) -> PyDict {
        dict([
            ("symbol", PyValue::Str(sym.into())),
            ("exchange", PyValue::Str("NSE".into())),
            ("quantity", PyValue::Int(qty)),
            ("pledged_qty", PyValue::Int(pledged)),
            ("average_price", PyValue::Float(10.0)),
            ("last_price", PyValue::Float(ltp)),
        ])
    }

    fn opts(root: &Path) -> CollectOptions<'_> {
        CollectOptions {
            on_date: None,
            price_source: "holdings",
            cash: None,
            allow_stale_cash: false,
            today: NaiveDate::from_ymd_opt(2026, 8, 14).unwrap(),
            include_untouchable_in_nav: false,
            root,
        }
    }

    #[tokio::test]
    async fn untouchables_are_recorded_but_kept_out_of_nav_and_pledged_shares_count() {
        let dir = tempfile::tempdir().unwrap();
        let kite = Fake {
            authed: true,
            holdings: vec![h("AAA", 10, 100.0, 10), h("SGBDE31III", 5, 6000.0, 0)],
        };
        let s = collect(&kite, &opts(dir.path())).await.unwrap();
        assert_eq!(s.invested, 1000.0);
        assert_eq!(s.nav, 2000.0);
        assert!(s.holdings_json.starts_with("{\"as_of\":\"2026-08-14\",\"price_source\":\"holdings\",\"include_untouchable_in_nav\":false,\"excluded_value\":30000.0,\"backfilled\":false,\"positions\":[{\"symbol\":\"SGBDE31III\""));
        assert!(s.holdings_json.contains("\"pledged_qty\":10"));
    }

    #[tokio::test]
    async fn a_backfill_without_cash_is_refused_and_no_session_is_a_runtime_error() {
        let dir = tempfile::tempdir().unwrap();
        let kite = Fake {
            authed: true,
            holdings: vec![],
        };
        let mut o = opts(dir.path());
        o.on_date = Some("2026-08-11");
        assert!(
            matches!(collect(&kite, &o).await, Err(SnapshotError::Value(m)) if m.contains("only knows today's cash"))
        );
        let off = Fake {
            authed: false,
            holdings: vec![],
        };
        assert!(matches!(
            collect(&off, &opts(dir.path())).await,
            Err(SnapshotError::Runtime(_))
        ));
    }

    #[tokio::test]
    async fn a_holiday_writes_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let kite = Fake {
            authed: true,
            holdings: vec![h("AAA", 10, 100.0, 0)],
        };
        let mut o = opts(dir.path());
        o.price_source = "historical";
        o.cash = Some(0.0);
        assert!(
            matches!(collect(&kite, &o).await, Err(SnapshotError::Value(m)) if m.contains("market holiday"))
        );
    }
}
