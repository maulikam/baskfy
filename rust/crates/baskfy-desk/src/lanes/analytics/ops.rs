//! `app/analytics/ops.py`: the operations registry — a FIXED ALLOWLIST of commands, each with a
//! hardcoded argv template, runnable one at a time from the operations page. Nothing here
//! places an order; nothing is interpolated into a shell.
//!
//! Each command keeps Python's argv (`-m scripts.daily …`, shown and stored as Python does) and
//! runs as this binary's `service <name>` with the same arguments. The options lab's five
//! strangle controls are not offered (the lab is frozen and off by default; R8.7).

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

use chrono::NaiveDate;

use crate::app::AppState;
use crate::storage::{Conn, DbValue, Param as SqlParam, StorageError};
use crate::wire::{PyDict, PyValue, dict};

use super::web::{row_dict, rows_py};

/// `data/uploads`, relative to the desk's working directory.
pub const UPLOADS: &str = "data/uploads";

/// `OpsError(ValueError)`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct OpsError(pub String);

/// One validated parameter value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    /// `None`.
    None,
    /// A flag.
    Bool(bool),
    /// A date, file or choice.
    Str(String),
}

impl Value {
    fn truthy(&self) -> bool {
        match self {
            Self::None => false,
            Self::Bool(b) => *b,
            Self::Str(s) => !s.is_empty(),
        }
    }

    fn or<'a>(&'a self, default: &'a str) -> &'a str {
        match self {
            Self::Str(s) if !s.is_empty() => s,
            _ => default,
        }
    }
}

/// A parameter: `flag | date | upload | text_choice`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OpParam {
    /// Form key.
    pub key: &'static str,
    /// Label.
    pub label: &'static str,
    /// Kind.
    pub kind: &'static str,
    /// Default.
    pub default: Option<&'static str>,
    /// Help.
    pub help: &'static str,
}

/// `date.fromisoformat(s)` (Python 3.12), `None` where Python raises `ValueError`.
pub(crate) fn parse_iso_date(s: &str) -> Option<NaiveDate> {
    // Python 3.12's date.fromisoformat: YYYY-MM-DD, YYYYMMDD, and the ISO week forms.
    if let Ok(d) = NaiveDate::parse_from_str(s, "%Y-%m-%d")
        && s.len() == 10
    {
        return Some(d);
    }
    if s.len() == 8 && s.bytes().all(|b| b.is_ascii_digit()) {
        return NaiveDate::parse_from_str(s, "%Y%m%d").ok();
    }
    let compact = s.replace('-', "");
    let (year, rest) = compact.split_at_checked(4)?;
    let rest = rest.strip_prefix('W')?;
    let year: i32 = year.parse().ok()?;
    let (week, day) = match rest.len() {
        2 => (rest.parse().ok()?, 1),
        3 => (rest.get(..2)?.parse().ok()?, rest.get(2..)?.parse().ok()?),
        _ => return None,
    };
    // `_parse_isoformat_date`: the dash after the year decides the form, and a mixed use of the
    // separator is refused ("Inconsistent use of dash separator").
    let has_sep = s.as_bytes().get(4) == Some(&b'-');
    let dashed_ok = match (has_sep, s.len()) {
        (true, 8) => true,
        (true, 10) => s.as_bytes().get(8) == Some(&b'-'),
        (false, 7 | 8) => !s.contains('-'),
        _ => false,
    };
    if !dashed_ok || !(1..=7).contains(&day) {
        return None;
    }
    let weekday = chrono::Weekday::try_from(u8::try_from(day - 1).ok()?).ok()?;
    NaiveDate::from_isoywd_opt(year, week, weekday)
}

impl OpParam {
    /// `Param.validate(raw)`.
    ///
    /// # Errors
    /// The refusal the page reports.
    pub fn validate(&self, raw: Option<&str>, root: &Path) -> Result<Value, OpsError> {
        if self.kind == "flag" {
            let text = raw.map_or_else(|| "none".to_owned(), str::to_lowercase);
            return Ok(Value::Bool(matches!(
                text.as_str(),
                "1" | "true" | "on" | "yes"
            )));
        }
        let Some(raw) = raw.filter(|r| !r.is_empty()) else {
            return Ok(Value::None);
        };
        let s = raw.trim();
        match self.kind {
            "date" => {
                if parse_iso_date(s).is_none() {
                    return Err(OpsError(format!(
                        "{}: {} is not a YYYY-MM-DD date",
                        self.label,
                        baskfy_providers::py::repr_str(s)
                    )));
                }
                Ok(Value::Str(s.to_owned()))
            }
            "upload" => {
                let uploads = resolve(&root.join(UPLOADS));
                let name = Path::new(s)
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                let target = resolve(&root.join(UPLOADS).join(&name));
                if name.is_empty()
                    || name == ".."
                    || !target.starts_with(&uploads)
                    || target == uploads
                {
                    return Err(OpsError(format!(
                        "{}: must be a file inside {UPLOADS}",
                        self.label
                    )));
                }
                if !target.exists() {
                    return Err(OpsError(format!(
                        "{}: {} does not exist",
                        self.label,
                        target
                            .file_name()
                            .map(|n| n.to_string_lossy().into_owned())
                            .unwrap_or_default()
                    )));
                }
                Ok(Value::Str(target.to_string_lossy().into_owned()))
            }
            other => Err(OpsError(format!(
                "unknown parameter kind {}",
                baskfy_providers::py::repr_str(other)
            ))),
        }
    }

    fn py(&self) -> PyValue {
        PyValue::Map(dict([
            ("key", PyValue::Str(self.key.to_owned())),
            ("label", PyValue::Str(self.label.to_owned())),
            ("kind", PyValue::Str(self.kind.to_owned())),
            (
                "default",
                self.default
                    .map_or(PyValue::None, |d| PyValue::Str(d.to_owned())),
            ),
            ("choices", PyValue::List(Vec::new())),
            ("help", PyValue::Str(self.help.to_owned())),
        ]))
    }
}

/// `Path.resolve()`: absolute, symlinks followed as far as the path exists.
fn resolve(p: &Path) -> PathBuf {
    if let Ok(c) = p.canonicalize() {
        return c;
    }
    match (p.parent(), p.file_name()) {
        (Some(parent), Some(name)) => resolve(parent).join(name),
        _ => p.to_path_buf(),
    }
}

/// How an operation builds its argv (after `python`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Build {
    Daily,
    DailyCheck,
    Snapshot,
    IndexUpdate,
    BenchmarkPri,
    BenchmarkList,
    BenchmarkShow,
    TradebookReconcile,
    Protection,
    TradebookCapture,
    TradebookSummary,
    Backtest,
    BacktestFull,
    BacktestFetch,
    DbInfo,
    ScoringSmoke,
    Autorun,
    AutorunCheck,
    Tests,
}

/// One operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Operation {
    /// Name.
    pub name: &'static str,
    /// Label.
    pub label: &'static str,
    /// Group.
    pub group: &'static str,
    /// Summary.
    pub summary: &'static str,
    build: Build,
    /// Seconds before it is stopped.
    pub timeout: u64,
    /// Parameters.
    pub params: &'static [OpParam],
    /// Needs a Kite login.
    pub needs_kite: bool,
    /// Writes.
    pub writes: bool,
    /// Slow.
    pub long_running: bool,
}

type Values = Vec<(&'static str, Value)>;

fn get<'a>(v: &'a Values, key: &str) -> &'a Value {
    v.iter()
        .find(|(k, _)| *k == key)
        .map_or(&Value::None, |(_, x)| x)
}

fn s(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|p| (*p).to_owned()).collect()
}

impl Operation {
    fn build_args(&self, v: &Values) -> Vec<String> {
        match self.build {
            Build::Daily => {
                let mut a = s(&["-m", "scripts.daily", "--source", "page"]);
                if get(v, "scan").truthy() {
                    a.push("--scan".to_owned());
                    a.push(get(v, "scan").or("").to_owned());
                }
                a
            }
            Build::DailyCheck => s(&["-m", "scripts.daily", "--check"]),
            Build::Snapshot => {
                let mut a = s(&["-m", "app.analytics.snapshot"]);
                if get(v, "force").truthy() {
                    a.push("--force".to_owned());
                }
                a
            }
            Build::IndexUpdate => s(&["-m", "scripts.daily", "--only", "index history"]),
            Build::BenchmarkPri => {
                let mut a = s(&[
                    "-m",
                    "app.analytics.benchmark",
                    "--pri",
                    "NIFTY 500",
                    "--from",
                ]);
                a.push(get(v, "start").or("2020-01-01").to_owned());
                a
            }
            Build::BenchmarkList => s(&["-m", "app.analytics.benchmark", "--list", "NIFTY"]),
            Build::BenchmarkShow => s(&["-m", "app.analytics.benchmark"]),
            Build::TradebookReconcile => s(&["-m", "app.analytics.tradebook", "--reconcile"]),
            Build::Protection => s(&["-m", "app.analytics.protection"]),
            Build::TradebookCapture => s(&["-m", "app.analytics.tradebook", "--capture"]),
            Build::TradebookSummary => s(&["-m", "app.analytics.tradebook", "--summary"]),
            Build::Backtest => {
                let mut a = s(&["-m", "scripts.backtest_regime", "--run", "--start"]);
                a.push(get(v, "start").or("2005-04-01").to_owned());
                a.push("--save".to_owned());
                a
            }
            Build::BacktestFull => {
                let mut a = s(&[
                    "-m",
                    "scripts.backtest_regime",
                    "--run",
                    "--robustness",
                    "--timelines",
                    "--start",
                ]);
                a.push(get(v, "start").or("2005-04-01").to_owned());
                a.push("--save".to_owned());
                a
            }
            Build::BacktestFetch => s(&[
                "-m",
                "scripts.backtest_regime",
                "--fetch",
                "--start",
                "2005-01-01",
            ]),
            Build::DbInfo => s(&["-m", "app.analytics.db", "--info"]),
            Build::ScoringSmoke => {
                let mut a = s(&["-m", "app.scoring"]);
                a.push(get(v, "scan").or("data/uploads/sample_scan.csv").to_owned());
                a
            }
            Build::Autorun => s(&["-m", "scripts.autorun"]),
            Build::AutorunCheck => s(&["-m", "scripts.autorun", "--check"]),
            Build::Tests => s(&["-m", "pytest", "tests/", "-q"]),
        }
    }

    /// `op.argv(values)[1:]`: the validated arguments after the interpreter.
    ///
    /// # Errors
    /// A parameter that does not validate.
    pub fn args(&self, values: &[(String, String)], root: &Path) -> Result<Vec<String>, OpsError> {
        let mut clean: Values = Vec::new();
        for p in self.params {
            let raw = values
                .iter()
                .rev()
                .find(|(k, _)| k == p.key)
                .map(|(_, v)| v.as_str());
            clean.push((p.key, p.validate(raw, root)?));
        }
        Ok(self.build_args(&clean))
    }

    /// `op.cli(values)`.
    #[must_use]
    pub fn cli(&self, values: &[(String, String)], root: &Path) -> String {
        let args = self.args(values, root).unwrap_or_else(|_| {
            let defaults: Values = self
                .params
                .iter()
                .map(|p| {
                    (
                        p.key,
                        p.default.map_or(Value::None, |d| Value::Str(d.to_owned())),
                    )
                })
                .collect();
            self.build_args(&defaults)
        });
        format!("python {}", args.join(" "))
    }

    fn py(&self) -> PyValue {
        PyValue::Map(dict([
            ("name", PyValue::Str(self.name.to_owned())),
            ("label", PyValue::Str(self.label.to_owned())),
            ("group", PyValue::Str(self.group.to_owned())),
            ("summary", PyValue::Str(self.summary.to_owned())),
            (
                "timeout",
                PyValue::Int(i64::try_from(self.timeout).unwrap_or(0)),
            ),
            (
                "params",
                PyValue::List(self.params.iter().map(OpParam::py).collect()),
            ),
            ("needs_kite", PyValue::Bool(self.needs_kite)),
            ("writes", PyValue::Bool(self.writes)),
            ("long_running", PyValue::Bool(self.long_running)),
        ]))
    }
}

const SCAN: OpParam = OpParam {
    key: "scan",
    label: "Scan CSV",
    kind: "upload",
    default: None,
    help: "A weekly momentum scan in data/uploads",
};

const fn date_param(default: &'static str) -> OpParam {
    OpParam {
        key: "start",
        label: "From date",
        kind: "date",
        default: Some(default),
        help: "",
    }
}

const FORCE: OpParam = OpParam {
    key: "force",
    label: "Overwrite today's row",
    kind: "flag",
    default: None,
    help: "Default keeps the stored values untouched",
};

#[allow(clippy::too_many_arguments)]
const fn op(
    name: &'static str,
    label: &'static str,
    group: &'static str,
    summary: &'static str,
    build: Build,
    timeout: u64,
    params: &'static [OpParam],
    flags: (bool, bool, bool),
) -> Operation {
    Operation {
        name,
        label,
        group,
        summary,
        build,
        timeout,
        params,
        needs_kite: flags.0,
        writes: flags.1,
        long_running: flags.2,
    }
}

/// `OPERATIONS`: the equity allowlist, in page order.
pub const OPERATIONS: &[Operation] = &[
    op(
        "daily",
        "Run the daily collection",
        "Daily",
        "Index history, benchmark PRI, EOD snapshot, breadth if a scan is given, and an observe-mode regime preview. Idempotent: re-running changes nothing.",
        Build::Daily,
        900,
        &[SCAN],
        (true, true, true),
    ),
    op(
        "daily_check",
        "Check collection state",
        "Daily",
        "Row counts, snapshot span and how many days since the last one. Reads only.",
        Build::DailyCheck,
        60,
        &[],
        (false, false, false),
    ),
    op(
        "snapshot",
        "Take an EOD snapshot",
        "Data",
        "NAV, holdings and cash into the TWR index chain. Run after 15:30 IST; a run before the close would record intraday prices as the close.",
        Build::Snapshot,
        180,
        &[FORCE],
        (true, true, false),
    ),
    op(
        "index_update",
        "Update index history",
        "Data",
        "Fetch missing daily candles for the structural indices and the momentum sentinel. Only missing dates are requested, and only this step of the daily run happens — no snapshot, no regime preview.",
        Build::IndexUpdate,
        600,
        &[],
        (true, true, true),
    ),
    op(
        "benchmark_pri",
        "Refresh benchmark PRI",
        "Data",
        "Price-return levels for NIFTY 500 from Kite. TRI remains authoritative for reported numbers; PRI understates the index by roughly its dividend yield.",
        Build::BenchmarkPri,
        600,
        &[date_param("2020-01-01")],
        (true, true, true),
    ),
    op(
        "benchmark_list",
        "List index symbols",
        "Data",
        "Every index instrument Kite exposes. Tradingsymbols drift, so verify one here before wiring it into a config.",
        Build::BenchmarkList,
        120,
        &[],
        (true, false, false),
    ),
    op(
        "benchmark_show",
        "Show stored benchmarks",
        "Data",
        "Coverage per index: how many PRI and TRI rows exist and over what span.",
        Build::BenchmarkShow,
        60,
        &[],
        (false, false, false),
    ),
    op(
        "tradebook_reconcile",
        "Reconcile tax lots",
        "Tax lots",
        "Compare reconstructed FIFO lots against live holdings. A quantity mismatch usually means a corporate action moved shares without a trade.",
        Build::TradebookReconcile,
        180,
        &[],
        (true, false, false),
    ),
    op(
        "protection",
        "Check stop coverage",
        "Diagnostics",
        "Compare live holdings against live GTT triggers. Every buy is supposed to get a stop, but one can fail to place, expire, be cancelled, or cover fewer shares than are now held — and each of those is silent. Reads only; never places or cancels a stop.",
        Build::Protection,
        90,
        &[],
        (true, false, false),
    ),
    op(
        "tradebook_capture",
        "Capture today's fills",
        "Tax lots",
        "Record today's executed trades and rebuild the lots they touch. Kite's trade book is same-day only, so a fill not captured on its own session survives nowhere but a Console export. Idempotent: repeating it changes nothing.",
        Build::TradebookCapture,
        120,
        &[],
        (true, true, false),
    ),
    op(
        "tradebook_summary",
        "Summarise tax lots",
        "Tax lots",
        "How many lots are stored and how many are still open. Reads only.",
        Build::TradebookSummary,
        60,
        &[],
        (false, false, false),
    ),
    op(
        "backtest",
        "Run the regime backtest",
        "Regime",
        "Every variant and ablation over the cached history, saved for the backtest page. Reports limitations before results; nothing here is endorsed.",
        Build::Backtest,
        1800,
        &[date_param("2005-04-01")],
        (false, true, true),
    ),
    op(
        "backtest_full",
        "Backtest with robustness",
        "Regime",
        "The above plus the parameter grid and event timelines. Slow.",
        Build::BacktestFull,
        3600,
        &[date_param("2005-04-01")],
        (false, true, true),
    ),
    op(
        "backtest_fetch",
        "Fetch maximum index history",
        "Regime",
        "Download every available daily candle for the regime indices. Long, and only needed once.",
        Build::BacktestFetch,
        1800,
        &[],
        (true, true, true),
    ),
    op(
        "db_info",
        "Database status",
        "Diagnostics",
        "Schema version, journal mode, row counts and the snapshot span.",
        Build::DbInfo,
        60,
        &[],
        (false, false, false),
    ),
    op(
        "scoring_smoke",
        "Score a scan",
        "Diagnostics",
        "Run the scoring engine over a scan and print the top ranks. Changes nothing.",
        Build::ScoringSmoke,
        120,
        &[SCAN],
        (false, false, false),
    ),
    op(
        "autorun",
        "Collect what today still needs",
        "Daily",
        "Runs whatever is outstanding for today and nothing else: the daily collection if no complete run has landed, and the ATM straddle observation if none is recorded. Fires automatically after a Kite login, because that is the moment the token that blocks both jobs becomes available.",
        Build::Autorun,
        1800,
        &[],
        (true, true, true),
    ),
    op(
        "autorun_check",
        "What does today still need?",
        "Daily",
        "Reports the outstanding collection and why, without running anything.",
        Build::AutorunCheck,
        180,
        &[],
        (true, false, false),
    ),
    op(
        "tests",
        "Run the test suite",
        "Diagnostics",
        "The full suite. Slow, but the fastest way to know the system is intact after a settings change or an upgrade.",
        Build::Tests,
        900,
        &[],
        (false, false, true),
    ),
];

/// `by_name()[name]`.
#[must_use]
pub fn by_name(name: &str) -> Option<&'static Operation> {
    OPERATIONS.iter().find(|o| o.name == name)
}

/// `groups()`.
#[must_use]
pub fn groups() -> Vec<&'static str> {
    let mut out: Vec<&'static str> = Vec::new();
    for o in OPERATIONS {
        if !out.contains(&o.group) {
            out.push(o.group);
        }
    }
    out
}

fn scan_choices(root: &Path) -> Vec<String> {
    let mut out: Vec<String> = std::fs::read_dir(root.join(UPLOADS))
        .map(|rd| {
            rd.filter_map(Result::ok)
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .filter(|n| n.ends_with(".csv"))
                .collect()
        })
        .unwrap_or_default();
    out.sort();
    out
}

// ---------------------------------------------------------------------------------------------
// running
// ---------------------------------------------------------------------------------------------

static LOCK: AtomicBool = AtomicBool::new(false);

fn live() -> &'static Mutex<HashSet<i64>> {
    static LIVE: OnceLock<Mutex<HashSet<i64>>> = OnceLock::new();
    LIVE.get_or_init(|| Mutex::new(HashSet::new()))
}

fn is_live(id: i64) -> bool {
    live().lock().map(|s| s.contains(&id)).unwrap_or(false)
}

fn set_live(id: i64, on: bool) {
    if let Ok(mut s) = live().lock() {
        if on {
            s.insert(id);
        } else {
            s.remove(&id);
        }
    }
}

const ORPHAN: &str = "the server stopped while this was running; nothing here is left half-written — every operation is idempotent, so just run it again.";

/// `reclaim_orphans(conn)`: close out `running` rows this process is not working on.
///
/// # Errors
/// Storage.
pub async fn reclaim_orphans(conn: &mut Conn) -> Result<usize, StorageError> {
    let cur = conn
        .execute("SELECT id FROM ops_jobs WHERE status='running'", &[])
        .await?;
    let orphans: Vec<i64> = cur
        .rows
        .iter()
        .filter_map(|r| r.at(0).and_then(DbValue::as_i64))
        .filter(|id| !is_live(*id))
        .collect();
    let n = orphans.len();
    if n > 0 {
        conn.transaction(move |c| {
            Box::pin(async move {
                let seq: Vec<Vec<SqlParam>> = orphans
                    .into_iter()
                    .map(|i| vec![SqlParam::Text(ORPHAN.to_owned()), SqlParam::Int(i)])
                    .collect();
                c.executemany(
                    "UPDATE ops_jobs SET status='interrupted', \
                     output=COALESCE(NULLIF(output,''),?) WHERE id=?",
                    &seq,
                )
                .await
                .map(|_| ())
            })
        })
        .await?;
    }
    Ok(n)
}

/// `running_job(conn)`.
///
/// # Errors
/// Storage.
pub async fn running_job(conn: &mut Conn) -> Result<Option<PyDict>, StorageError> {
    reclaim_orphans(conn).await?;
    let cur = conn
        .execute(
            "SELECT * FROM ops_jobs WHERE status='running' ORDER BY id DESC LIMIT 1",
            &[],
        )
        .await?;
    Ok(cur.fetchone().map(row_dict))
}

/// `history(conn, limit)`.
///
/// # Errors
/// Storage.
pub async fn history(conn: &mut Conn, limit: i64) -> Result<PyValue, StorageError> {
    let cur = conn
        .execute(
            "SELECT id, name, argv_json, status, exit_code, started_at, finished_at, \
             duration_s, output FROM ops_jobs ORDER BY id DESC LIMIT ?",
            &[SqlParam::Int(limit)],
        )
        .await?;
    Ok(rows_py(&cur.rows))
}

/// `last_run(conn)`: the most recent outcome per operation.
///
/// # Errors
/// Storage.
pub async fn last_run(conn: &mut Conn) -> Result<Vec<(String, PyDict)>, StorageError> {
    let cur = conn
        .execute(
            "SELECT id, name, status, started_at, duration_s, argv_json \
             FROM ops_jobs ORDER BY id DESC",
            &[],
        )
        .await?;
    let mut out: Vec<(String, PyDict)> = Vec::new();
    for r in &cur.rows {
        let name = r
            .get("name")
            .and_then(DbValue::as_str)
            .unwrap_or_default()
            .to_owned();
        if !out.iter().any(|(n, _)| *n == name) {
            out.push((name, row_dict(r)));
        }
    }
    Ok(out)
}

/// `view(conn)`: the operations page's context.
///
/// # Errors
/// Storage.
pub async fn view(conn: &mut Conn, state: &AppState) -> Result<PyDict, StorageError> {
    let root = &state.paths.root;
    let running = running_job(conn).await?;
    let last = last_run(conn).await?;
    let scans = scan_choices(root);
    let operations: Vec<PyValue> = OPERATIONS
        .iter()
        .map(|o| {
            let has_upload = o.params.iter().any(|p| p.kind == "upload");
            PyValue::Map(dict([
                ("op", o.py()),
                (
                    "scans",
                    PyValue::List(if has_upload {
                        scans.iter().map(|s| PyValue::Str(s.clone())).collect()
                    } else {
                        Vec::new()
                    }),
                ),
                (
                    "last",
                    last.iter()
                        .find(|(n, _)| n == o.name)
                        .map_or(PyValue::None, |(_, d)| PyValue::Map(d.clone())),
                ),
                ("cli", PyValue::Str(o.cli(&[], root))),
            ]))
        })
        .collect();
    Ok(dict([
        (
            "groups",
            PyValue::List(
                groups()
                    .into_iter()
                    .map(|g| PyValue::Str(g.to_owned()))
                    .collect(),
            ),
        ),
        ("operations", PyValue::List(operations)),
        ("running", running.map_or(PyValue::None, PyValue::Map)),
        ("history", history(conn, 25).await?),
        ("dry_run", PyValue::Bool(state.settings_snapshot().dry_run)),
        ("has_scan", PyValue::Bool(!scans.is_empty())),
    ]))
}

/// `/ops/data`'s `operations`.
#[must_use]
pub fn operations_listing(root: &Path) -> PyValue {
    PyValue::List(
        OPERATIONS
            .iter()
            .map(|o| {
                PyValue::Map(dict([
                    ("name", PyValue::Str(o.name.to_owned())),
                    ("label", PyValue::Str(o.label.to_owned())),
                    ("group", PyValue::Str(o.group.to_owned())),
                    ("cli", PyValue::Str(o.cli(&[], root))),
                ]))
            })
            .collect(),
    )
}

/// What `start` returns.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Started {
    /// The `ops_jobs` id.
    pub job_id: i64,
    /// The operation.
    pub name: String,
    /// The command as shown.
    pub cli: String,
}

/// Why `start` refused or failed.
#[derive(Debug, thiserror::Error)]
pub enum StartError {
    /// A refusal the page reports (`/ops?error=...`).
    #[error("{0}")]
    Refused(#[from] OpsError),
    /// The database.
    #[error("{0}")]
    Storage(#[from] StorageError),
}

/// The `baskfy-desk service` each allowlisted Python module runs as.
#[must_use]
pub fn service_for(module: &str) -> Option<&'static str> {
    Some(match module {
        "scripts.daily" => "daily",
        "scripts.autorun" => "autorun",
        "scripts.backtest_regime" => "backtest-regime",
        "app.analytics.snapshot" => "snapshot",
        "app.analytics.benchmark" => "benchmark",
        "app.analytics.tradebook" => "tradebook",
        "app.analytics.protection" => "protection",
        "app.analytics.db" => "db",
        "app.scoring" => "scoring",
        "pytest" => "tests",
        _ => return None,
    })
}

/// The child's argv for `op.argv(values)[1:]`: this binary, `service <name>`, the same arguments.
#[must_use]
pub fn exec_argv(args: &[String]) -> Vec<String> {
    let (name, rest) = match args {
        [m, module, rest @ ..] if m == "-m" => (service_for(module).unwrap_or("unknown"), rest),
        _ => ("unknown", args),
    };
    let rest: Vec<&str> = rest.iter().map(String::as_str).collect();
    crate::lanes::service_loop::child_argv(&crate::lanes::service_loop::program(), name, &rest)
}

/// `start(conn, name, values)`: validate, record and launch; refuses while another runs.
///
/// # Errors
/// [`StartError::Refused`] with Python's message; storage.
pub async fn start(
    conn: &mut Conn,
    state: &AppState,
    name: &str,
    values: &[(String, String)],
) -> Result<Started, StartError> {
    let Some(op) = by_name(name) else {
        return Err(OpsError(format!(
            "{} is not a known operation",
            baskfy_providers::py::repr_str(name)
        ))
        .into());
    };
    let root = state.paths.root.clone();
    let args = op.args(values, &root)?;
    if LOCK
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return Err(OpsError(
            "another operation is already running; they share the database and the broker session, so they are not run concurrently"
                .to_owned(),
        )
        .into());
    }
    let started = state.clock.now_ist().naive_local();
    let argv_json = match crate::wire::py_json_dumps(&PyValue::List(
        args.iter().map(|a| PyValue::Str(a.clone())).collect(),
    )) {
        Ok(j) => j,
        Err(e) => {
            LOCK.store(false, Ordering::SeqCst);
            return Err(StorageError::Other(e.to_string()).into());
        }
    };
    let started_iso = started.format("%Y-%m-%dT%H:%M:%S").to_string();
    let name_owned = name.to_owned();
    let inserted = conn
        .transaction(move |c| {
            Box::pin(async move {
                let cur = c
                    .execute(
                        "INSERT INTO ops_jobs(name, argv_json, status, started_at) \
                         VALUES(?,?,?,?) RETURNING id",
                        &[
                            SqlParam::Text(name_owned),
                            SqlParam::Text(argv_json),
                            SqlParam::Text("running".to_owned()),
                            SqlParam::Text(started_iso),
                        ],
                    )
                    .await?;
                Ok(cur
                    .fetchone()
                    .and_then(|r| r.at(0))
                    .and_then(DbValue::as_i64)
                    .unwrap_or(0))
            })
        })
        .await;
    let job_id = match inserted {
        Ok(id) => id,
        Err(e) => {
            LOCK.store(false, Ordering::SeqCst);
            return Err(e.into());
        }
    };
    set_live(job_id, true);
    let st = state.clone();
    let timeout = op.timeout;
    tokio::spawn(async move {
        execute(st, job_id, args, timeout, started).await;
    });
    Ok(Started {
        job_id,
        name: name.to_owned(),
        cli: op.cli(values, &root),
    })
}

async fn finish(
    state: &AppState,
    job_id: i64,
    status: &str,
    code: Option<i32>,
    output: &str,
    started: chrono::NaiveDateTime,
) {
    let now = state.clock.now_ist().naive_local();
    #[allow(clippy::cast_precision_loss)]
    let secs = (now - started).num_microseconds().unwrap_or(0) as f64 / 1e6;
    let duration = super::desk_settings::round2(secs);
    let chars: Vec<char> = output.chars().collect();
    let tail: String = chars[chars.len().saturating_sub(20_000)..].iter().collect();
    let params = vec![
        SqlParam::Text(status.to_owned()),
        code.map_or(SqlParam::Null, |c| SqlParam::Int(i64::from(c))),
        SqlParam::Text(now.format("%Y-%m-%dT%H:%M:%S").to_string()),
        SqlParam::Float(duration),
        SqlParam::Text(tail),
        SqlParam::Int(job_id),
    ];
    if let Ok(mut conn) = state.db.connect().await {
        let _ = conn
            .transaction(move |c| {
                Box::pin(async move {
                    c.execute(
                        "UPDATE ops_jobs SET status=?, exit_code=?, finished_at=?, duration_s=?, \
                         output=? WHERE id=?",
                        &params,
                    )
                    .await
                    .map(|_| ())
                })
            })
            .await;
    }
}

enum RunError {
    Start(String),
    Timeout,
}

/// `subprocess.run(argv, capture_output=True, text=True, timeout=..., shell=False)`.
fn run_with_timeout(
    args: &[String],
    root: &Path,
    timeout: u64,
) -> Result<std::process::Output, RunError> {
    use std::io::Read as _;
    use std::process::{Command, Stdio};
    let argv = exec_argv(args);
    let (program, rest) = argv.split_first().ok_or_else(|| RunError::Start("empty argv".to_owned()))?;
    let mut child = Command::new(program)
        .args(rest)
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| RunError::Start(e.to_string()))?;
    let drain = |pipe: Option<Box<dyn std::io::Read + Send>>| {
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            if let Some(mut p) = pipe {
                let _ = p.read_to_end(&mut buf);
            }
            buf
        })
    };
    let out = drain(
        child
            .stdout
            .take()
            .map(|p| Box::new(p) as Box<dyn std::io::Read + Send>),
    );
    let err = drain(
        child
            .stderr
            .take()
            .map(|p| Box::new(p) as Box<dyn std::io::Read + Send>),
    );
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if std::time::Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(RunError::Timeout);
            }
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(100)),
            Err(e) => return Err(RunError::Start(e.to_string())),
        }
    };
    Ok(std::process::Output {
        status,
        stdout: out.join().unwrap_or_default(),
        stderr: err.join().unwrap_or_default(),
    })
}

async fn execute(
    state: AppState,
    job_id: i64,
    args: Vec<String>,
    timeout: u64,
    started: chrono::NaiveDateTime,
) {
    let root = state.paths.root.clone();
    let run = tokio::task::spawn_blocking(move || run_with_timeout(&args, &root, timeout)).await;
    match run.unwrap_or(Err(RunError::Start("worker panicked".to_owned()))) {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout);
            let stderr = String::from_utf8_lossy(&out.stderr);
            let text = if stderr.is_empty() {
                stdout.into_owned()
            } else {
                format!("{stdout}\n{stderr}")
            };
            let status = if out.status.success() { "ok" } else { "failed" };
            finish(
                &state,
                job_id,
                status,
                out.status.code(),
                text.trim(),
                started,
            )
            .await;
        }
        Err(RunError::Start(e)) => {
            finish(
                &state,
                job_id,
                "failed",
                None,
                &format!("could not start: {e}"),
                started,
            )
            .await;
        }
        Err(RunError::Timeout) => {
            finish(
                &state,
                job_id,
                "timeout",
                None,
                &format!("exceeded the {timeout}s limit and was stopped"),
                started,
            )
            .await;
        }
    }
    set_live(job_id, false);
    LOCK.store(false, Ordering::SeqCst);
}

/// `SELECT * FROM ops_jobs WHERE id=?`.
///
/// # Errors
/// Storage.
pub async fn job(conn: &mut Conn, job_id: i64) -> Result<Option<PyDict>, StorageError> {
    let cur = conn
        .execute(
            "SELECT * FROM ops_jobs WHERE id=?",
            &[SqlParam::Int(job_id)],
        )
        .await?;
    Ok(cur.fetchone().map(row_dict))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn allowlist_is_fixed_and_never_orders() {
        for o in OPERATIONS {
            let args = o.cli(&[], Path::new("/nonexistent"));
            assert!(args.starts_with("python -m "), "{args}");
            for word in ["place_order", "execute", "gtt"] {
                assert!(!args.contains(word), "{} must not place orders", o.name);
            }
        }
        assert_eq!(OPERATIONS.len(), 19);
    }

    #[test]
    fn cli_defaults() {
        let root = Path::new("/nonexistent");
        assert_eq!(
            by_name("benchmark_pri").unwrap().cli(&[], root),
            "python -m app.analytics.benchmark --pri NIFTY 500 --from 2020-01-01"
        );
        assert_eq!(
            by_name("scoring_smoke").unwrap().cli(&[], root),
            "python -m app.scoring data/uploads/sample_scan.csv"
        );
        assert_eq!(
            by_name("snapshot")
                .unwrap()
                .cli(&[("force".into(), "on".into())], root),
            "python -m app.analytics.snapshot --force"
        );
    }

    #[test]
    fn params_validate() {
        let root = tempfile::tempdir().unwrap();
        let bt = by_name("backtest").unwrap();
        let bad = bt.args(&[("start".into(), "2024-13-01".into())], root.path());
        assert_eq!(
            bad.unwrap_err().0,
            "From date: '2024-13-01' is not a YYYY-MM-DD date"
        );
        assert!(
            bt.args(&[("start".into(), "20240105".into())], root.path())
                .is_ok()
        );
        let daily = by_name("daily").unwrap();
        assert_eq!(
            daily
                .args(&[("scan".into(), "../../etc/passwd".into())], root.path())
                .unwrap_err()
                .0,
            "Scan CSV: passwd does not exist"
        );
        std::fs::create_dir_all(root.path().join(UPLOADS)).unwrap();
        std::fs::write(root.path().join(UPLOADS).join("s.csv"), "x").unwrap();
        let ok = daily
            .args(&[("scan".into(), "nested/s.csv".into())], root.path())
            .unwrap();
        assert!(ok.last().unwrap().ends_with("data/uploads/s.csv"));
    }

    #[test]
    fn every_operation_runs_as_a_desk_service() {
        let root = Path::new("/nonexistent");
        for o in OPERATIONS {
            let args = o.args(&[], root).unwrap_or_else(|_| o.build_args(&Vec::new()));
            let argv = exec_argv(&args);
            let at = argv.iter().position(|a| a == "service").unwrap();
            assert_ne!(argv[at + 1], "unknown", "{}", o.name);
            assert_eq!(&argv[at + 2..], &args[2..], "{}", o.name);
        }
        let args = by_name("index_update").unwrap().args(&[], root).unwrap();
        let argv = exec_argv(&args);
        assert_eq!(
            &argv[argv.len() - 4..],
            ["service", "daily", "--only", "index history"]
        );
        assert_eq!(service_for("app.analytics.snapshot"), Some("snapshot"));
    }

    #[test]
    fn week_dates_parse_like_python() {
        assert!(parse_iso_date("2026-W44-5").is_some());
        assert!(parse_iso_date("2026W445").is_some());
        assert!(parse_iso_date("2026-1-5").is_none());
    }
}
