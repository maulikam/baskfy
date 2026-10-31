//! `scripts/daily.py`: the daily data run (`baskfy-desk service daily [args]`). Reads from
//! Kite and writes the desk's own tables; it has no path to an order.

use std::path::Path;
use std::sync::Arc;

use chrono::{NaiveDate, NaiveDateTime, NaiveTime};

use crate::app::AppState;
use crate::lanes::analytics::analytics_views::benchmark as bm;
use crate::lanes::analytics::regime::RegimeConfig;
use crate::lanes::analytics::{breadth, daily_runs as dr, index_cache as ic};
use crate::lanes::exec_core::kite_client::DeskKite;
use crate::lanes::exec_core::ratelimit::KiteLimits;
use crate::lanes::weekly::weekly_books::{snapshot as snap, snapshot_db, tradebook as tb};
use crate::pyfmt::{PyArg, format_value};
use crate::storage::{Conn, DbValue};
use crate::wire::PyValue;

/// `OK`, `SKIP`, `FAIL`.
pub use dr::{FAIL, OK, SKIP};

/// `STEPS`.
pub const STEPS: [&str; 7] = [
    "index history",
    "benchmark PRI",
    "EOD snapshot",
    "trade capture",
    "stale orders",
    "breadth",
    "regime preview",
];

const SOURCES: [&str; 3] = ["schedule", "cli", "page"];

/// The parsed command line.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Args {
    /// `--scan`.
    pub scan: Option<String>,
    /// `--check`.
    pub check: bool,
    /// `--quiet`.
    pub quiet: bool,
    /// `--force-snapshot`.
    pub force_snapshot: bool,
    /// `--only` (repeatable).
    pub only: Vec<String>,
    /// `--source`.
    pub source: String,
}

const USAGE: &str = "usage: daily.py [-h] [--scan SCAN] [--db DB] [--check] [--quiet] [--force-snapshot] [--only STEP] [--source {schedule,cli,page}]";

/// `argparse`; `Err` is the message for exit code 2.
///
/// # Errors
/// An unknown flag, a missing value or a bad choice.
pub fn parse_args(argv: &[String]) -> Result<Args, String> {
    let mut a = Args {
        source: "cli".to_owned(),
        ..Args::default()
    };
    let err = |m: String| format!("{USAGE}\ndaily.py: error: {m}");
    let mut it = argv.iter();
    while let Some(flag) = it.next() {
        let (flag, inline) = match flag.split_once('=') {
            Some((f, v)) if f.starts_with("--") => (f.to_owned(), Some(v.to_owned())),
            _ => (flag.clone(), None),
        };
        let mut value = |name: &str| {
            inline
                .clone()
                .or_else(|| it.next().cloned())
                .ok_or_else(|| err(format!("argument {name}: expected one argument")))
        };
        match flag.as_str() {
            "--scan" => a.scan = Some(value("--scan")?),
            "--db" => {
                let _ = value("--db")?;
                return Err(err(
                    "argument --db: the Rust desk reads DESK_DATABASE_URL only".to_owned(),
                ));
            }
            "--check" => a.check = true,
            "--quiet" => a.quiet = true,
            "--force-snapshot" => a.force_snapshot = true,
            "--only" => {
                let v = value("--only")?;
                if !STEPS.contains(&v.as_str()) {
                    return Err(err(format!("argument --only: invalid choice: '{v}'")));
                }
                a.only.push(v);
            }
            "--source" => {
                let v = value("--source")?;
                if !SOURCES.contains(&v.as_str()) {
                    return Err(err(format!(
                        "argument --source: invalid choice: '{v}' (choose from 'schedule', 'cli', 'page')"
                    )));
                }
                a.source = v;
            }
            other => return Err(err(format!("unrecognized arguments: {other}"))),
        }
    }
    Ok(a)
}

/// `_before_close(now)`: a weekday before 15:30.
#[must_use]
pub fn before_close(now: NaiveDateTime) -> bool {
    use chrono::Datelike;
    now.weekday().num_days_from_monday() < 5
        && now.time() < NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// `Runner`: step outcomes, so one failure never hides the rest.
#[derive(Debug, Default)]
pub struct Runner {
    /// `(name, status, detail)`.
    pub rows: Vec<(String, String, String)>,
    verbose: bool,
    only: Option<Vec<String>>,
}

impl Runner {
    /// `Runner(verbose, only)`.
    #[must_use]
    pub const fn new(verbose: bool, only: Option<Vec<String>>) -> Self {
        Self {
            rows: Vec::new(),
            verbose,
            only,
        }
    }

    /// The gate half of `step`: `true` when the step should run (else its skip row is written).
    pub fn gate(&mut self, name: &str, skip_reason: Option<String>) -> bool {
        if let Some(only) = &self.only
            && !only.iter().any(|o| o == name)
        {
            self.push(name, SKIP, "not selected by --only".to_owned());
            return false;
        }
        if let Some(why) = skip_reason {
            self.push(name, SKIP, why);
            return false;
        }
        true
    }

    /// The outcome half of `step`.
    #[allow(clippy::print_stderr)]
    pub fn done(&mut self, name: &str, result: Result<String, String>) {
        match result {
            Ok(detail) => self.push(name, OK, detail),
            Err(e) => {
                let first: String = e.lines().next().unwrap_or("").chars().take(110).collect();
                self.push(name, FAIL, first);
                if self.verbose {
                    eprintln!("{name}: {e}");
                }
            }
        }
    }

    fn push(&mut self, name: &str, status: &str, detail: String) {
        self.rows.push((name.to_owned(), status.to_owned(), detail));
    }

    /// `failed`.
    #[must_use]
    pub fn failed(&self) -> bool {
        self.rows.iter().any(|r| r.1 == FAIL)
    }

    /// `report()`'s lines.
    #[must_use]
    pub fn report(&self) -> Vec<String> {
        let bar = "=".repeat(78);
        let mut out = vec![
            String::new(),
            bar.clone(),
            format!("{:<22}{:<10}detail", "step", "result"),
            bar.clone(),
        ];
        for (n, s, d) in &self.rows {
            out.push(format!("{n:<22}{s:<10}{d}"));
        }
        out.push(bar);
        out.push(if self.failed() {
            "some steps failed; re-run once the cause is fixed, nothing is duplicated".to_owned()
        } else {
            "every step is idempotent — re-running this changes nothing".to_owned()
        });
        out
    }
}

fn say(line: &str) {
    crate::lanes::service_loop::say(line);
}

fn fmt(v: f64, spec: &str) -> String {
    format_value(&PyArg::Float(v), spec).unwrap_or_else(|_| v.to_string())
}

fn db_text(v: Option<&DbValue>) -> String {
    match v {
        None | Some(DbValue::Null) => "None".to_owned(),
        Some(DbValue::Text(s) | DbValue::Json(s)) => s.clone(),
        Some(DbValue::Int(i)) => i.to_string(),
        Some(DbValue::Float(f)) => baskfy_core::py::float_repr(*f),
        Some(DbValue::Bool(b)) => if *b { "True" } else { "False" }.to_owned(),
        Some(DbValue::Aware(t)) => t.to_rfc3339(),
        Some(DbValue::Naive(t)) => t.to_string(),
        Some(DbValue::Bytes(b)) => format!("{b:?}"),
    }
}

/// `_state(conn)`: row counts and the snapshot span, in Python's key order.
///
/// # Errors
/// The driver's.
pub async fn state_counts(conn: &mut Conn) -> Result<Vec<(String, String)>, String> {
    let mut out = Vec::new();
    for t in [
        "snapshots",
        "breadth_readings",
        "trades",
        "index_series",
        "regime_evaluations",
        "benchmark",
    ] {
        let cur = conn
            .execute(&format!("SELECT COUNT(*) c FROM {t}"), &[])
            .await
            .map_err(|e| e.to_string())?;
        let n = cur
            .rows
            .first()
            .and_then(|r| r.get("c"))
            .and_then(DbValue::as_i64)
            .unwrap_or(0);
        out.push((t.to_owned(), n.to_string()));
    }
    let rows = snapshot_db::snapshot_series(conn)
        .await
        .map_err(|e| e.to_string())?;
    out.push((
        "first_snapshot".to_owned(),
        db_text(rows.first().and_then(|r| r.get("date"))),
    ));
    out.push((
        "last_snapshot".to_owned(),
        db_text(rows.last().and_then(|r| r.get("date"))),
    ));
    Ok(out)
}

fn count(st: &[(String, String)], k: &str) -> String {
    st.iter()
        .find(|(n, _)| n == k)
        .map(|(_, v)| v.clone())
        .unwrap_or_default()
}

fn get_int(d: &crate::wire::PyDict, k: &str) -> i64 {
    match d.get(k) {
        Some(PyValue::Int(i)) => *i,
        _ => 0,
    }
}

/// `baskfy-desk service daily [args]`: `main()`'s exit code.
pub async fn main(state: &AppState, argv: &[String]) -> i32 {
    let a = match parse_args(argv) {
        Ok(a) => a,
        Err(e) => {
            #[allow(clippy::print_stderr)]
            {
                eprintln!("{e}");
            }
            return 2;
        }
    };
    let settings = state.settings_snapshot();
    let now_naive = || state.clock.now_ist().naive_local();
    let today = now_naive().date();
    let cfg = match RegimeConfig::from_settings(&settings) {
        Ok(c) => c,
        Err(e) => {
            say(&format!("regime_config: {e}"));
            return 1;
        }
    };
    let mut run = Runner::new(
        !a.quiet,
        if a.only.is_empty() {
            None
        } else {
            Some(a.only.clone())
        },
    );
    let started = now_naive();
    let record_run = a.only.is_empty();

    let mut conn = match state.db.connect().await {
        Ok(c) => c,
        Err(e) => {
            say(&e.to_string());
            return 1;
        }
    };
    if let Err(e) = conn.migrate().await {
        say(&e.to_string());
        return 1;
    }

    if a.check {
        let st = match state_counts(&mut conn).await {
            Ok(s) => s,
            Err(e) => {
                say(&e);
                return 1;
            }
        };
        say(&format!("as of {}", today.format("%Y-%m-%d")));
        for (k, v) in &st {
            say(&format!("  {k:<22}{v}"));
        }
        let last = count(&st, "last_snapshot");
        if let Ok(d) = NaiveDate::parse_from_str(&last, "%Y-%m-%d") {
            let gap = (today - d).num_days();
            say(&format!("\n  last snapshot was {gap} day(s) ago"));
            if gap > 1 {
                say("  those sessions cannot be recovered: margins() has no history");
            }
        }
        return 0;
    }

    let stamp_record = || {
        let duration = (now_naive() - started).num_milliseconds();
        #[allow(clippy::cast_precision_loss)]
        let secs = crate::lanes::weekly::pandas::py_round(duration as f64 / 1000.0, 2);
        (secs, now_naive().format("%Y-%m-%dT%H:%M:%S").to_string())
    };

    // --- auth ---------------------------------------------------------------------------
    let kite: Arc<DeskKite> = match crate::lanes::exec_core::exec_kite::kite(state) {
        Ok(k) => k,
        Err(e) => {
            say(&format!("cannot construct the Kite client: {}", e.message));
            return 2;
        }
    };
    let authed = match kite.try_is_authed().await {
        Ok(b) => b,
        Err(e) => {
            say(&format!("cannot construct the Kite client: {}", e.message));
            return 2;
        }
    };
    if !authed {
        say("Kite session expired. Tokens last one trading day and the standard login flow has no refresh token.");
        say(&format!("  log in:  {}", kite.login_url().unwrap_or_default()));
        say("  then re-run this command.");
        if record_run {
            let steps: Vec<(String, String, String)> = STEPS
                .iter()
                .map(|s| {
                    (
                        (*s).to_owned(),
                        FAIL.to_owned(),
                        "not reached: no Kite session".to_owned(),
                    )
                })
                .collect();
            let (secs, at) = stamp_record();
            safe_record(&mut conn, &steps, &a.source, today, Some(dr::AUTH), secs, &at).await;
        }
        return 2;
    }

    let root = state.paths.root.clone();
    let limits = KiteLimits::default();

    // --- 1. index history ---------------------------------------------------------------
    if run.gate("index history", None) {
        let now_iso = state
            .clock
            .now_ist()
            .format("%Y-%m-%dT%H:%M:%S%:z")
            .to_string();
        let opts = ic::UpdateOptions {
            start: None,
            end: None,
            clock: ic::SessionClock::of(&*state.clock),
            limits: &limits,
            full_refresh: false,
            attempts: 3,
            base_delay: 1.0,
            now: &now_iso,
        };
        let cache = root.join(ic::INSTRUMENT_CACHE);
        let r = ic::update_all(&*kite as &dyn ic::IndexKite, &mut conn, &cfg, &cache, opts)
            .await
            .map(|res| {
                res.iter()
                    .map(|r| {
                        let name = r.get("index_name").map(PyValue::py_str).unwrap_or_default();
                        let last = name.split_whitespace().last().unwrap_or("").to_owned();
                        format!("{last}+{}", get_int(r, "written"))
                    })
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .map_err(|e| e.to_string());
        run.done("index history", r);
    }

    // --- 2. benchmark PRI ---------------------------------------------------------------
    if run.gate("benchmark PRI", None) {
        let r = benchmarks(&kite, &mut conn, &cfg, &root, today).await;
        run.done("benchmark PRI", r);
    }

    // --- 3. EOD snapshot ----------------------------------------------------------------
    let now = now_naive();
    let too_early = before_close(now) && !a.force_snapshot;
    let skip = too_early.then(|| {
        format!(
            "it is {}, before the 15:30 close — a snapshot now would store an intraday NAV as the day's final value and the evening run would not correct it. Pass --force-snapshot to override.",
            now.format("%H:%M")
        )
    });
    if run.gate("EOD snapshot", skip) {
        let opts = snap::CollectOptions {
            on_date: None,
            price_source: "holdings",
            cash: None,
            allow_stale_cash: false,
            today: state.clock.now_ist().date_naive(),
            include_untouchable_in_nav: settings.include_untouchable_in_nav,
            root: &root,
        };
        let r = snap::run(&*kite as &dyn snap::SnapshotKite, &opts, &mut conn, false)
            .await
            .map(|s| {
                format!(
                    "{} nav Rs {} index {} [{}]",
                    s.snapshot.date,
                    fmt(s.snapshot.nav, ",.0f"),
                    fmt(s.index_value, ".4f"),
                    s.outcome
                )
            })
            .map_err(|e| e.to_string());
        run.done("EOD snapshot", r);
    }

    // --- 3b. trade capture --------------------------------------------------------------
    if run.gate("trade capture", None) {
        let r = async {
            let raw = kite.trades().await.map_err(|e| e.message)?;
            let res = tb::capture_live_trades(&mut conn, &raw, now_naive())
                .await
                .map_err(|e| e.to_string())?;
            if get_int(&res, "captured") == 0 {
                return Ok("no fills today".to_owned());
            }
            let syms = match res.get("symbols") {
                Some(PyValue::List(l)) => l.iter().map(PyValue::py_str).collect::<Vec<_>>(),
                _ => vec![],
            };
            Ok(format!(
                "{} new of {} fills, {}",
                get_int(&res, "new"),
                get_int(&res, "captured"),
                syms.join(", ")
            ))
        }
        .await;
        run.done("trade capture", r);
    }

    // --- 3b. orders left working by a previous session ----------------------------------
    if run.gate("stale orders", None) {
        let stamp = now_naive().format("%Y-%m-%dT%H:%M:%S").to_string();
        let r = crate::lanes::weekly::plan_store::lapse_stale_orders(&mut conn, today, &stamp)
            .await
            .map(|out| {
                if get_int(&out, "n") == 0 {
                    return "nothing left working from an earlier session".to_owned();
                }
                let lapsed = match out.get("lapsed") {
                    Some(PyValue::List(l)) => l.clone(),
                    _ => vec![],
                };
                lapsed
                    .iter()
                    .filter_map(|r| match r {
                        PyValue::Map(m) => Some(m),
                        _ => None,
                    })
                    .map(|m| {
                        let g = |k: &str| m.get(k).map(PyValue::py_str).unwrap_or_default();
                        format!(
                            "{} {}->{} ({}/{})",
                            g("symbol"),
                            g("was"),
                            g("now"),
                            g("filled"),
                            g("planned")
                        )
                    })
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .map_err(|e| e.to_string());
        run.done("stale orders", r);
    }

    // --- 4. breadth ---------------------------------------------------------------------
    let skip = a
        .scan
        .is_none()
        .then(|| "no --scan given; breadth is never invented from a stale reading".to_owned());
    if run.gate("breadth", skip) {
        let path = a.scan.clone().unwrap_or_default();
        let r = breadth_step(state, &mut conn, &root, &path, today).await;
        run.done("breadth", r);
    }

    // --- 5. regime preview --------------------------------------------------------------
    if run.gate("regime preview", None) {
        let stamp = now_naive().format("%Y-%m-%dT%H:%M:%S").to_string();
        let hex = state.ids.uuid4_hex();
        let r = crate::lanes::analytics::regime::run::regime_preview(
            &mut conn,
            &cfg,
            settings.dry_run,
            today,
            &stamp,
            &hex,
        )
        .await
        .map(|o| o.summary)
        .map_err(|e| e.to_string());
        run.done("regime preview", r);
    }

    for line in run.report() {
        say(&line);
    }
    if record_run {
        let (secs, at) = stamp_record();
        safe_record(&mut conn, &run.rows, &a.source, today, None, secs, &at).await;
    }
    if let Ok(st) = state_counts(&mut conn).await {
        say(&format!(
            "\nsnapshots {} ({} -> {}) · breadth {} · trades {}",
            count(&st, "snapshots"),
            count(&st, "first_snapshot"),
            count(&st, "last_snapshot"),
            count(&st, "breadth_readings"),
            count(&st, "trades")
        ));
        if count(&st, "breadth_readings").parse::<i64>().unwrap_or(0) < 26 {
            say("  breadth history is still too thin for the tiered regime backtest; pass --scan on the day you upload one");
        }
        if count(&st, "trades") == "0" {
            say("  no tax lots: import a Console tradebook at /tradebook");
        }
    }
    i32::from(run.failed())
}

/// `_safe_record`: bookkeeping that never masks the real exit code.
async fn safe_record(
    conn: &mut Conn,
    steps: &[(String, String, String)],
    trigger: &str,
    today: NaiveDate,
    outcome: Option<&str>,
    secs: f64,
    at: &str,
) {
    if let Err(e) = dr::record(conn, steps, trigger, today, outcome, Some(secs), at).await {
        say(&format!("  (could not record this run: {e})"));
    }
}

async fn benchmarks(
    kite: &DeskKite,
    conn: &mut Conn,
    cfg: &RegimeConfig,
    root: &Path,
    today: NaiveDate,
) -> Result<String, String> {
    let start = NaiveDate::from_ymd_opt(2020, 1, 1).unwrap_or_default();
    let mut out = Vec::new();
    for name in ["NIFTY 500".to_owned(), cfg.momentum_sentinel.clone()] {
        let aliases = cfg
            .index_aliases
            .iter()
            .find(|(k, _)| *k == name)
            .map(|(_, v)| v.clone())
            .unwrap_or_default();
        let s = bm::kite_index_series(
            kite as &dyn bm::IndexKite,
            root,
            today,
            &name,
            &aliases,
            start,
            None,
        )
        .await
        .map_err(|e| e.to_string())?;
        let pairs: Vec<(String, f64)> = s
            .series
            .iter()
            .map(|(d, v)| (d.format("%Y-%m-%d").to_string(), *v))
            .collect();
        let r = bm::upsert_benchmark(conn, &s.index_name, &pairs, bm::PRI)
            .await
            .map_err(|e| e.to_string())?;
        out.push(format!("{}+{}", r.index_name, r.rows));
    }
    Ok(format!("{}  (PRI — TRI is authoritative)", out.join(", ")))
}

async fn breadth_step(
    state: &AppState,
    conn: &mut Conn,
    root: &Path,
    scan: &str,
    today: NaiveDate,
) -> Result<String, String> {
    let p = Path::new(scan);
    let path = if p.is_absolute() {
        p.to_path_buf()
    } else {
        root.join(p)
    };
    let raw = std::fs::read(&path).map_err(|e| format!("FileNotFoundError: {e}"))?;
    let (basket, _) = crate::lanes::weekly::analyze::load_config(state).await;
    let scoring = crate::lanes::weekly::analyze::scoring_config(&basket);
    let frame = crate::lanes::weekly::score::load_scan(&raw, &scoring).map_err(|e| e.0)?;
    let audit = crate::lanes::weekly::score::audit(&frame, &scoring).map_err(|e| e.0)?;
    let opt = |v: f64| (!v.is_nan()).then_some(v);
    let close_ma20 = frame
        .f64s("close")
        .zip(frame.f64s("ma_20"))
        .map(|(c, m)| c.into_iter().zip(m).map(|(c, m)| (opt(c), opt(m))).collect());
    let cols = breadth::ScanColumns {
        symbol: frame.strs("symbol").map(|v| {
            v.into_iter()
                .map(|s| s.unwrap_or_else(|| "nan".to_owned()))
                .collect()
        }),
        close_ma20,
        rows: frame.nrows,
    };
    let reading = breadth::reading_from_audit_dict(&audit, &cols, "scan", None, today)?;
    let stamp = state
        .clock
        .now_ist()
        .naive_local()
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let outcome = breadth::save_reading(conn, &reading, breadth::MISSING_POLICY, &stamp)
        .await
        .map_err(|e| e.to_string())?;
    Ok(format!(
        "{}% above 20-DMA, {}% coverage [{outcome}]",
        fmt(reading.pct_above_20dma, ".1f"),
        fmt(reading.coverage_pct, ".0f")
    ))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| (*x).to_owned()).collect()
    }

    #[test]
    fn the_schedule_argv_parses() {
        let a = parse_args(&s(&["--quiet", "--source", "schedule"])).unwrap();
        assert!(a.quiet && !a.check && a.only.is_empty());
        assert_eq!(a.source, "schedule");
        assert_eq!(parse_args(&[]).unwrap().source, "cli");
        assert!(parse_args(&s(&["--only", "nope"])).is_err());
        assert!(parse_args(&s(&["--source", "x"])).is_err());
        assert_eq!(
            parse_args(&s(&["--only", "index history"])).unwrap().only,
            ["index history"]
        );
    }

    #[test]
    fn the_snapshot_is_refused_before_a_weekday_close() {
        let at = |d: u32, h: u32, m: u32| {
            NaiveDate::from_ymd_opt(2026, 9, d)
                .unwrap()
                .and_hms_opt(h, m, 0)
                .unwrap()
        };
        assert!(before_close(at(25, 15, 29)));
        assert!(!before_close(at(25, 15, 30)));
        assert!(!before_close(at(26, 10, 0)));
    }

    #[test]
    fn only_skips_unselected_and_a_failure_keeps_the_first_line() {
        let mut r = Runner::new(false, Some(s(&["breadth"])));
        assert!(!r.gate("index history", None));
        assert!(r.gate("breadth", None));
        r.done("breadth", Err(format!("{}\nsecond", "x".repeat(200))));
        assert_eq!(r.rows[0].2, "not selected by --only");
        assert_eq!(r.rows[1].1, FAIL);
        assert_eq!(r.rows[1].2.len(), 110);
        assert!(r.failed());
    }
}
