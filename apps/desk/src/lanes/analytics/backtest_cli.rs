//! `scripts/backtest_regime.py` (`baskfy-desk backtest-regime [args]`).

mod model;
mod table;

use chrono::NaiveDate;

use crate::app::AppState;
use crate::lanes::analytics::index_cache as ic;
use crate::lanes::analytics::regime::RegimeConfig;
use crate::lanes::exec_core::ratelimit::KiteLimits;
use crate::lanes::service_loop::say;
use crate::lanes::weekly::analyze::dumps_indent2;
use crate::pyfmt::{PyArg, format_value};
use crate::wire::{PyDict, PyValue, dict};

use model::{BtError, CostModel, Row, Sim, Store, VARIANTS, Variant};

const USAGE: &str = "usage: backtest_regime.py [-h] [--db DB] [--fetch] [--start START] [--end END]
                          [--run] [--list] [--robustness] [--timelines]
                          [--cash-rate CASH_RATE]
                          [--slippage {low,base,stressed_smallcap}]
                          [--reversal-windows REVERSAL_WINDOWS]
                          [--save [PATH]]
                          [--portfolio-series PORTFOLIO_SERIES]";

const HELP_BODY: &str = "
regime overlay backtest

options:
  -h, --help            show this help message and exit
  --db DB
  --fetch               download max index history first
  --start START
  --end END
  --run
  --list
  --robustness
  --timelines
  --cash-rate CASH_RATE
  --slippage {low,base,stressed_smallcap}
  --reversal-windows REVERSAL_WINDOWS
  --save [PATH]         write results as JSON for the /regime/backtest page
  --portfolio-series PORTFOLIO_SERIES
                        CSV of an existing historical portfolio return series";

const SAVE_CONST: &str = "data/outputs/regime_backtest.json";

/// The parsed command line.
#[derive(Debug, Clone, PartialEq)]
pub struct Args {
    /// `--fetch`.
    pub fetch: bool,
    /// `--start`.
    pub start: String,
    /// `--end`.
    pub end: Option<String>,
    /// `--run`.
    pub run: bool,
    /// `--list`.
    pub list: bool,
    /// `--robustness`.
    pub robustness: bool,
    /// `--timelines`.
    pub timelines: bool,
    /// `--cash-rate`.
    pub cash_rate: f64,
    /// `--slippage`.
    pub slippage: String,
    /// `--reversal-windows`.
    pub reversal_windows: String,
    /// `--save`.
    pub save: Option<String>,
}

impl Default for Args {
    fn default() -> Self {
        Self {
            fetch: false,
            start: "2020-01-01".to_owned(),
            end: None,
            run: false,
            list: false,
            robustness: false,
            timelines: false,
            cash_rate: 0.0,
            slippage: "base".to_owned(),
            reversal_windows: "2,4,8".to_owned(),
            save: None,
        }
    }
}

/// What `argparse` resolved to.
#[derive(Debug, Clone, PartialEq)]
pub enum Parsed {
    /// Parsed arguments.
    Args(Args),
    /// `-h` / `--help`.
    Help,
}

const LONG: [&str; 14] = [
    "--help",
    "--db",
    "--fetch",
    "--start",
    "--end",
    "--run",
    "--list",
    "--robustness",
    "--timelines",
    "--cash-rate",
    "--slippage",
    "--reversal-windows",
    "--save",
    "--portfolio-series",
];

fn looks_negative_number(s: &str) -> bool {
    let t = &s[1..];
    !t.is_empty() && t.parse::<f64>().is_ok() && t.chars().all(|c| c.is_ascii_digit() || c == '.')
}

fn is_optionish(s: &str) -> bool {
    s.starts_with('-') && s.len() > 1 && !looks_negative_number(s)
}

/// `argparse`; `Err` is the message after `error: `.
///
/// # Errors
/// An unknown flag, a missing value or a bad choice/float.
pub fn parse_args(argv: &[String]) -> Result<Parsed, String> {
    let mut a = Args::default();
    let mut extras: Vec<String> = Vec::new();
    let mut i = 0;
    while i < argv.len() {
        let tok = &argv[i];
        i += 1;
        if tok == "--" {
            extras.extend(argv[i..].iter().cloned());
            break;
        }
        if tok == "-h" {
            return Ok(Parsed::Help);
        }
        if !tok.starts_with("--") || tok.len() <= 2 {
            extras.push(tok.clone());
            continue;
        }
        let (name, explicit) = match tok.split_once('=') {
            Some((n, v)) => (n, Some(v.to_owned())),
            None => (tok.as_str(), None),
        };
        let opt = if LONG.contains(&name) {
            name
        } else {
            let hits: Vec<&str> = LONG
                .iter()
                .copied()
                .filter(|o| o.starts_with(name))
                .collect();
            match hits.len() {
                0 => {
                    extras.push(tok.clone());
                    continue;
                }
                1 => hits[0],
                _ => {
                    return Err(format!(
                        "ambiguous option: {tok} could match {}",
                        hits.join(", ")
                    ));
                }
            }
        };
        let flag = matches!(
            opt,
            "--help" | "--fetch" | "--run" | "--list" | "--robustness" | "--timelines"
        );
        if flag {
            if let Some(v) = explicit {
                return Err(format!("argument {opt}: ignored explicit argument '{v}'"));
            }
            match opt {
                "--help" => return Ok(Parsed::Help),
                "--fetch" => a.fetch = true,
                "--run" => a.run = true,
                "--list" => a.list = true,
                "--robustness" => a.robustness = true,
                _ => a.timelines = true,
            }
            continue;
        }
        let value = if let Some(v) = explicit {
            Some(v)
        } else if i < argv.len() && !is_optionish(&argv[i]) {
            i += 1;
            Some(argv[i - 1].clone())
        } else {
            None
        };
        if opt == "--save" {
            a.save = Some(value.unwrap_or_else(|| SAVE_CONST.to_owned()));
            continue;
        }
        let Some(v) = value else {
            return Err(format!("argument {opt}: expected one argument"));
        };
        match opt {
            "--start" => a.start = v,
            "--end" => a.end = Some(v),
            "--cash-rate" => {
                a.cash_rate = v
                    .trim()
                    .parse::<f64>()
                    .map_err(|_| format!("argument --cash-rate: invalid float value: '{v}'"))?;
            }
            "--slippage" => {
                if !model::SLIPPAGE_SCENARIOS.iter().any(|(k, _)| *k == v) {
                    return Err(format!(
                        "argument --slippage: invalid choice: '{v}' (choose from low, base, stressed_smallcap)"
                    ));
                }
                a.slippage = v;
            }
            "--reversal-windows" => a.reversal_windows = v,
            _ => {}
        }
    }
    if !extras.is_empty() {
        return Err(format!("unrecognized arguments: {}", extras.join(" ")));
    }
    Ok(Parsed::Args(a))
}

/// The `--list` table.
#[must_use]
pub fn list_lines() -> Vec<String> {
    let mut out = vec![format!("{:<22}{:<16}{}", "key", "status", "design")];
    for v in &VARIANTS {
        let status = if v.available { "ok" } else { "NOT AVAILABLE" };
        out.push(format!("  {:<20}{status:<16}{}", v.key, v.label));
    }
    out
}

fn fmt(v: f64, spec: &str) -> String {
    format_value(&PyArg::Float(v), spec).unwrap_or_else(|_| v.to_string())
}

fn fmt_i(v: i64, spec: &str) -> String {
    format_value(&PyArg::Int(v), spec).unwrap_or_else(|_| v.to_string())
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn eprint(msg: &str) {
    #[allow(clippy::print_stderr)]
    {
        eprintln!("{msg}");
    }
}

fn fail(e: &BtError) -> i32 {
    match e {
        BtError::Exit(m) => eprint(m),
        BtError::Py(m) => eprint(&format!("Traceback (most recent call last):\n{m}")),
    }
    1
}

fn parse_date(s: &str) -> Result<NaiveDate, BtError> {
    crate::lanes::analytics::ops::parse_iso_date(s)
        .ok_or_else(|| BtError::Py(format!("ValueError: Invalid isoformat string: '{s}'")))
}

fn parse_windows(s: &str) -> Result<Vec<usize>, BtError> {
    s.split(',')
        .map(|x| {
            x.trim().parse::<usize>().map_err(|_| {
                BtError::Py(format!(
                    "ValueError: invalid literal for int() with base 10: '{x}'"
                ))
            })
        })
        .collect()
}

/// `main()`'s exit code.
pub async fn main(state: &AppState, argv: &[String]) -> i32 {
    let a = match parse_args(argv) {
        Ok(Parsed::Args(a)) => a,
        Ok(Parsed::Help) => {
            say(&format!("{USAGE}\n{HELP_BODY}"));
            return 0;
        }
        Err(e) => {
            eprint(&format!("{USAGE}\nbacktest_regime.py: error: {e}"));
            return 2;
        }
    };
    let cfg = match RegimeConfig::from_settings(&state.settings_snapshot()) {
        Ok(c) => c,
        Err(e) => return fail(&BtError::from(e)),
    };
    let start = match parse_date(&a.start) {
        Ok(d) => d,
        Err(e) => return fail(&e),
    };
    let end = match a.end.as_deref().filter(|s| !s.is_empty()) {
        Some(s) => match parse_date(s) {
            Ok(d) => d,
            Err(e) => return fail(&e),
        },
        None => state.clock.now_ist().date_naive(),
    };
    let costs = CostModel::with_slippage(model::slippage_of(&a.slippage));

    if a.list {
        for line in list_lines() {
            say(&line);
        }
        return 0;
    }

    let mut conn = match state.db.connect().await {
        Ok(c) => c,
        Err(e) => return fail(&BtError::Py(e.to_string())),
    };
    if let Err(e) = conn.migrate().await {
        return fail(&BtError::Py(e.to_string()));
    }

    if a.fetch {
        if let Err(e) = fetch(state, &mut conn, &cfg, start).await {
            return fail(&e);
        }
        if !a.run {
            return 0;
        }
    }
    if !a.run {
        say(&format!("{USAGE}\n{HELP_BODY}"));
        return 0;
    }
    let store = match Store::load(&mut conn, &cfg).await {
        Ok(s) => s,
        Err(e) => return fail(&BtError::Py(e)),
    };
    match run(state, &a, &cfg, &store, start, end, &costs) {
        Ok(()) => 0,
        Err(e) => fail(&e),
    }
}

async fn fetch(
    state: &AppState,
    conn: &mut crate::storage::Conn,
    cfg: &RegimeConfig,
    start: NaiveDate,
) -> Result<(), BtError> {
    let kite = crate::lanes::exec_core::exec_kite::kite(state)
        .map_err(|e| BtError::Py(e.message.clone()))?;
    let limits = KiteLimits::default();
    let now_iso = state
        .clock
        .now_ist()
        .format("%Y-%m-%dT%H:%M:%S%:z")
        .to_string();
    let opts = ic::UpdateOptions {
        start: Some(start),
        end: None,
        clock: ic::SessionClock::of(&*state.clock),
        limits: &limits,
        full_refresh: true,
        attempts: 3,
        base_delay: 1.0,
        now: &now_iso,
    };
    let cache = state.paths.root.join(ic::INSTRUMENT_CACHE);
    let res = ic::update_all(&*kite as &dyn ic::IndexKite, conn, cfg, &cache, opts)
        .await
        .map_err(|e| BtError::Py(e.to_string()))?;
    for r in &res {
        let get = |k: &str| r.get(k).map_or_else(|| "None".to_owned(), PyValue::py_str);
        say(&format!(
            "  {:<26} {:>6} rows  {} -> {}",
            get("index_name"),
            get("written"),
            get("first"),
            get("last")
        ));
    }
    Ok(())
}

type Detail = Vec<(&'static str, Vec<Row>, Sim)>;

fn run_all(
    store: &Store,
    cfg: &RegimeConfig,
    start: NaiveDate,
    end: NaiveDate,
    costs: &CostModel,
    cash_rate: f64,
) -> Result<(Vec<(String, PyDict)>, Detail), BtError> {
    let sentinel = store.frame(&cfg.momentum_sentinel);
    if sentinel.is_empty() {
        return Err(BtError::Py(format!(
            "TypeError: cannot do slice indexing on RangeIndex with these indexers [{}] of type str",
            iso(start)
        )));
    }
    let proxy = store.window(&cfg.momentum_sentinel, start, end);
    if model::index_names(cfg)
        .iter()
        .any(|n| store.frame(n).is_empty())
    {
        return Err(BtError::Py("KeyError: 'close'".to_owned()));
    }
    let tapes = model::build_tapes(store, cfg)?;
    let has_breadth = store.breadth_covers_window(start, end);
    let mut rows = Vec::new();
    let mut detail = Vec::new();
    for v in &VARIANTS {
        if !v.available {
            rows.push((
                v.key.to_owned(),
                dict([
                    ("label", PyValue::Str(v.label.to_owned())),
                    ("status", PyValue::Str("NOT AVAILABLE".to_owned())),
                    ("reason", PyValue::Str(v.unavailable_reason.to_owned())),
                ]),
            ));
            continue;
        }
        let note = if v.use_breadth && !has_breadth && v.kind == model::Kind::Tiered {
            "breadth history unavailable — evaluated as a PRICE-ONLY PROXY with breadth rules disabled; this is NOT the full live regime"
        } else {
            ""
        };
        let vcfg = RegimeConfig {
            breadth_required: v.use_breadth && has_breadth,
            ..cfg.clone()
        };
        let sched = model::replay(store, &vcfg, v, start, end, Some(&tapes))?;
        let sim = model::simulate(&sched, &proxy, costs, cash_rate);
        let mut perf = model::performance(&sim, &sched, &proxy)?.to_dict();
        perf.insert("label", PyValue::Str(v.label.to_owned()));
        perf.insert("status", PyValue::Str("ok".to_owned()));
        perf.insert("note", PyValue::Str(note.to_owned()));
        rows.push((v.key.to_owned(), perf));
        detail.push((v.key, sched, sim));
    }
    Ok((rows, detail))
}

struct Ctx<'a> {
    store: &'a Store,
    bt_cfg: &'a RegimeConfig,
    start: NaiveDate,
    end: NaiveDate,
    proxy: &'a [(NaiveDate, f64)],
    costs: &'a CostModel,
    cash_rate: f64,
}

fn robustness_grid(c: &Ctx<'_>, variant: &Variant) -> Result<Vec<PyValue>, BtError> {
    let Ctx {
        store,
        bt_cfg,
        start,
        end,
        proxy,
        costs,
        cash_rate,
    } = *c;
    let mut out = Vec::new();
    for bps in [100_i64, 150, 200] {
        for cd in [2_i64, 3, 5] {
            for r4 in [0.0_f64, 10.0] {
                let g = RegimeConfig {
                    buffer_bps: bps,
                    confirm_days: cd,
                    tier_exposure_pct: [100.0, 70.0, 40.0, r4],
                    ..bt_cfg.clone()
                };
                let sch = model::replay(store, &g, variant, start, end, None)?;
                let sim = model::simulate(&sch, proxy, costs, cash_rate);
                let p = model::performance(&sim, &sch, proxy)?;
                out.push(PyValue::Map(dict([
                    ("buffer_bps", PyValue::Int(bps)),
                    ("confirm_days", PyValue::Int(cd)),
                    ("r4_exposure", PyValue::Float(r4)),
                    ("CAGR", PyValue::Float(p.cagr)),
                    ("max_drawdown_pct", PyValue::Float(p.max_drawdown_pct)),
                    ("annual_turnover_pct", PyValue::Float(p.annual_turnover_pct)),
                    ("whipsaws_per_year", PyValue::Float(p.whipsaws_per_year)),
                ])));
            }
        }
    }
    Ok(out)
}

const EVENT_WINDOWS: [(&str, &str, &str); 3] = [
    ("2018 smallcap bear", "2018-01-01", "2019-03-31"),
    ("Feb-Jun 2020", "2020-02-01", "2020-06-30"),
    ("2021-22 top/correction", "2021-10-01", "2022-07-31"),
];

const SUBPERIODS: [(&str, &str); 4] = [
    ("2007-2009 GFC", "2007-01-01"),
    ("2009 onward", "2009-01-01"),
    ("2015 onward", "2015-01-01"),
    ("2020 onward", "2020-01-01"),
];

fn ymd(s: &str) -> NaiveDate {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap_or_default()
}

fn event_timelines(sched: &[Row]) -> Vec<PyDict> {
    let mut out = Vec::new();
    for (label, w0, w1) in EVENT_WINDOWS {
        let (d0, d1) = (ymd(w0), ymd(w1));
        let rows: Vec<PyValue> = sched
            .iter()
            .filter(|r| r.session >= d0 && r.session <= d1)
            .map(|r| {
                PyValue::Map(dict([
                    ("date", PyValue::Str(iso(r.session))),
                    ("raw_tier", PyValue::Str(r.raw_tier.to_owned())),
                    ("tier", PyValue::Str(r.tier.to_owned())),
                    ("exposure", PyValue::Float(r.exposure)),
                    (
                        "breadth",
                        r.breadth
                            .filter(|b| !b.is_nan())
                            .map_or(PyValue::None, PyValue::Float),
                    ),
                ]))
            })
            .collect();
        let empty = rows.is_empty();
        let mut entry = dict([
            ("label", PyValue::Str(label.to_owned())),
            ("start", PyValue::Str(w0.to_owned())),
            ("end", PyValue::Str(w1.to_owned())),
            ("rows", PyValue::List(rows)),
        ]);
        if empty {
            entry.insert(
                "unavailable",
                PyValue::Str(
                    "index history does not reach this window; re-run with --fetch and an earlier --start"
                        .to_owned(),
                ),
            );
        }
        out.push(entry);
    }
    out
}

fn subperiod_table(c: &Ctx<'_>, variant: &Variant) -> Result<Vec<PyDict>, BtError> {
    let Ctx {
        store,
        bt_cfg,
        start,
        end,
        proxy,
        costs,
        cash_rate,
    } = *c;
    let base = model::variant("A");
    let mut out = Vec::new();
    for (label, s0) in SUBPERIODS {
        let s0d = ymd(s0).max(start);
        if s0d >= end {
            continue;
        }
        let sub = model::between(proxy, s0d, end);
        if sub.len() < 260 {
            out.push(dict([
                ("label", PyValue::Str(label.to_owned())),
                ("start", PyValue::Str(iso(s0d))),
                (
                    "unavailable",
                    PyValue::Str(format!("only {} sessions cached", sub.len())),
                ),
            ]));
            continue;
        }
        let mut variants = PyDict::new();
        for v in [&base, variant] {
            let sch = model::replay(store, bt_cfg, v, s0d, end, None)?;
            let sim = model::simulate(&sch, &sub, costs, cash_rate);
            let p = model::performance(&sim, &sch, &sub)?;
            variants.insert(
                v.key,
                PyValue::Map(dict([
                    ("CAGR", PyValue::Float(p.cagr)),
                    ("max_drawdown_pct", PyValue::Float(p.max_drawdown_pct)),
                ])),
            );
        }
        out.push(dict([
            ("label", PyValue::Str(label.to_owned())),
            ("start", PyValue::Str(iso(s0d))),
            ("variants", PyValue::Map(variants)),
        ]));
    }
    Ok(out)
}

const TABLE_COLS: [&str; 14] = [
    "label",
    "status",
    "CAGR",
    "max_drawdown_pct",
    "volatility_pct",
    "sharpe",
    "sortino",
    "calmar",
    "avg_equity_exposure",
    "pct_time_in_cash",
    "transitions",
    "whipsaws_per_year",
    "annual_turnover_pct",
    "total_cost_drag_pct_yr",
];

fn variants_json(rows: &[(String, PyDict)]) -> PyValue {
    let mut cols: Vec<String> = Vec::new();
    for (_, r) in rows {
        for (k, _) in r.iter() {
            if !cols.iter().any(|c| c == k) {
                cols.push(k.to_owned());
            }
        }
    }
    let mut out = PyDict::new();
    for (key, r) in rows {
        let mut m = PyDict::new();
        for c in &cols {
            let v = match r.get(c) {
                None => PyValue::None,
                Some(PyValue::Float(f)) if f.is_nan() => PyValue::None,
                Some(v) => v.clone(),
            };
            m.insert(c.clone(), v);
        }
        out.insert(key.clone(), PyValue::Map(m));
    }
    PyValue::Map(out)
}

fn truthy(v: Option<&PyValue>) -> bool {
    match v {
        None | Some(PyValue::None) => false,
        Some(PyValue::Str(s)) => !s.is_empty(),
        Some(PyValue::Bool(b)) => *b,
        Some(_) => true,
    }
}

#[allow(clippy::too_many_lines)]
fn run(
    state: &AppState,
    a: &Args,
    cfg: &RegimeConfig,
    store: &Store,
    start: NaiveDate,
    end: NaiveDate,
    costs: &CostModel,
) -> Result<(), BtError> {
    let bar = "=".repeat(92);
    let slip = model::slippage_of(&a.slippage);
    say(&bar);
    say("REGIME BACKTEST — LIMITATIONS");
    say(&bar);
    say("  portfolio series : NIFTY 500 MOMENTUM 50 index — PROXY, not the live book");
    say("  return type      : PRICE RETURN (Kite indices carry no TRI)");
    let covers = store.breadth_covers_window(start, end);
    let breadth_text = if covers {
        "sufficient coverage"
    } else {
        "INSUFFICIENT for this window; tiered variants run as a PRICE-ONLY PROXY with breadth rules disabled"
    };
    say(&format!(
        "  breadth history  : {} stored readings — {breadth_text}",
        store.breadth.len()
    ));
    say("  execution        : next session OPEN after the weekly close (no lookahead)");
    say("  book weights     : configured defaults (no historical full-risk snapshots)");
    say(&format!(
        "  window           : {} -> {}",
        iso(start),
        iso(end)
    ));
    let slip_text = format!("{} ({}%)", a.slippage, fmt(slip * 100.0, ".2f"));
    say(&format!("  slippage         : {slip_text}"));
    let cash_text = format!("{}%", fmt(a.cash_rate * 100.0, ".2f"));
    say(&format!("  cash return      : {cash_text}"));
    say("");

    let bt_cfg = RegimeConfig {
        breadth_required: covers,
        ..cfg.clone()
    };
    let (rows, detail) = run_all(store, cfg, start, end, costs, a.cash_rate)?;

    say(&bar);
    say("VARIANTS");
    say(&bar);
    let index: Vec<String> = rows.iter().map(|r| r.0.clone()).collect();
    let cells: Vec<Vec<PyValue>> = rows
        .iter()
        .map(|(_, r)| {
            TABLE_COLS
                .iter()
                .map(|c| r.get(c).cloned().unwrap_or(PyValue::Float(f64::NAN)))
                .collect()
        })
        .collect();
    say(&table::to_string(&index, &TABLE_COLS, &cells));
    say("");
    for (k, row) in &rows {
        let status = row.get("status").map(PyValue::py_str).unwrap_or_default();
        if status == "NOT AVAILABLE" {
            let reason = row
                .get("reason")
                .map_or_else(|| "None".to_owned(), PyValue::py_str);
            say(&format!("  {k}: NOT AVAILABLE — {reason}"));
        } else if truthy(row.get("note")) {
            let note = row.get("note").map(PyValue::py_str).unwrap_or_default();
            say(&format!("  {k}: {note}"));
        }
    }

    say("");
    say(&bar);
    say("CASH RETURN SENSITIVITY (variant F)");
    say(&bar);
    let vf = model::variant("F");
    let sched = model::replay(store, &bt_cfg, &vf, start, end, None)?;
    let proxy = store.window(&cfg.momentum_sentinel, start, end);
    let ctx = Ctx {
        store,
        bt_cfg: &bt_cfg,
        start,
        end,
        proxy: &proxy,
        costs,
        cash_rate: a.cash_rate,
    };
    for rate in [0.0_f64, 0.065] {
        let sim = model::simulate(&sched, &proxy, costs, rate);
        let p = model::performance(&sim, &sched, &proxy)?;
        say(&format!(
            "  cash {}%  CAGR {}%  maxDD {}%  Sharpe {}",
            fmt(rate * 100.0, ">5.2f"),
            fmt(p.cagr, ">7.2f"),
            fmt(p.max_drawdown_pct, ">7.2f"),
            fmt(p.sharpe, ">6.3f")
        ));
    }

    say("");
    say("SLIPPAGE SCENARIOS (variant F)");
    for (name, s) in model::SLIPPAGE_SCENARIOS {
        let c = costs.with_slippage_of(s);
        let sim = model::simulate(&sched, &proxy, &c, a.cash_rate);
        let p = model::performance(&sim, &sched, &proxy)?;
        say(&format!(
            "  {name:<20} CAGR {}%  cost drag {}%/yr",
            fmt(p.cagr, ">7.2f"),
            fmt(p.total_cost_drag_pct_yr, ">6.3f")
        ));
    }

    say("");
    say("COST BREAKDOWN — Rs 1 crore book, R1 -> R2 (30-point exposure change)");
    let notional = 10_000_000.0_f64 * 0.30;
    say(&format!(
        "  traded notional        Rs {}",
        fmt(notional, ">12,.0f")
    ));
    for (k, v) in costs.breakdown(notional, false) {
        say(&format!("    {k:<20} Rs {}", fmt(v, ">12,.0f")));
    }
    say(&format!(
        "  one-way total          Rs {}",
        fmt(costs.one_way(notional, false), ">12,.0f")
    ));
    say(&format!(
        "  exit + re-entry        Rs {}",
        fmt(costs.round_trip(notional), ">12,.0f")
    ));

    say("");
    say("WHIPSAW SENSITIVITY (variant F)");
    for w in parse_windows(&a.reversal_windows)? {
        let (count, per_year) = model::count_whipsaws(&sched, &proxy, w);
        say(&format!(
            "  reversal window {} evals: {count} whipsaws ({}/yr)",
            fmt_i(i64::try_from(w).unwrap_or(i64::MAX), ">2"),
            baskfy_core::py::float_repr(per_year)
        ));
    }

    let mut grid: Option<Vec<PyValue>> = None;
    let mut timelines: Option<Vec<PyDict>> = None;

    if a.robustness {
        let g = robustness_grid(&ctx, &vf)?;
        say("");
        say(&bar);
        say("ROBUSTNESS GRID (variant F)");
        say(&bar);
        say(&format!(
            "{:>8}{:>9}{:>8}{:>9}{:>9}{:>11}{:>9}",
            "buffer", "confirm", "R4 eq", "CAGR", "maxDD", "turnover", "whip/yr"
        ));
        for row in &g {
            let PyValue::Map(m) = row else { continue };
            let f = |k: &str| match m.get(k) {
                Some(PyValue::Float(x)) => *x,
                #[allow(clippy::cast_precision_loss)]
                Some(PyValue::Int(i)) => *i as f64,
                _ => f64::NAN,
            };
            let cd = match m.get("confirm_days") {
                Some(PyValue::Int(i)) => *i,
                _ => 0,
            };
            say(&format!(
                "{}%{}{}%{}{}{}{}",
                fmt(f("buffer_bps") / 100.0, ">7.1f"),
                fmt_i(cd, ">9"),
                fmt(f("r4_exposure"), ">7.0f"),
                fmt(f("CAGR"), ">9.2f"),
                fmt(f("max_drawdown_pct"), ">9.2f"),
                fmt(f("annual_turnover_pct"), ">11.1f"),
                fmt(f("whipsaws_per_year"), ">9.2f")
            ));
        }
        say(
            "\n  Reported, not ranked: a wider buffer is not automatically better because it lowers whipsaw count.",
        );
        grid = Some(g);
    }

    if a.timelines {
        let t = event_timelines(&sched);
        say("");
        say(&bar);
        say("EVENT TIMELINES (variant F)");
        say(&bar);
        for w in &t {
            let get = |k: &str| w.get(k).map(PyValue::py_str).unwrap_or_default();
            say(&format!(
                "\n-- {} ({} .. {}) --",
                get("label"),
                get("start"),
                get("end")
            ));
            if truthy(w.get("unavailable")) {
                say(&format!("   NO DATA: {}", get("unavailable")));
                continue;
            }
            say(&format!(
                "{:<12}{:>5}{:>6}{:>7}{:>9}",
                "session", "raw", "tier", "exp%", "breadth"
            ));
            let Some(PyValue::List(items)) = w.get("rows") else {
                continue;
            };
            for item in items {
                let PyValue::Map(r) = item else { continue };
                let s = |k: &str| r.get(k).map(PyValue::py_str).unwrap_or_default();
                let b = match r.get("breadth") {
                    Some(PyValue::Float(x)) => fmt(*x, ".1f"),
                    _ => String::new(),
                };
                let e = match r.get("exposure") {
                    Some(PyValue::Float(x)) => fmt(*x, ">7.0f"),
                    _ => String::new(),
                };
                say(&format!(
                    "{:<12}{:>5}{:>6}{e}{b:>9}",
                    s("date"),
                    s("raw_tier"),
                    s("tier")
                ));
            }
        }
        timelines = Some(t);
    }

    let subperiods = subperiod_table(&ctx, &vf)?;
    say("");
    say(&bar);
    say("SUBPERIODS (variant F vs A)");
    say(&bar);
    for row in &subperiods {
        let label = row.get("label").map(PyValue::py_str).unwrap_or_default();
        if truthy(row.get("unavailable")) {
            let u = row
                .get("unavailable")
                .map(PyValue::py_str)
                .unwrap_or_default();
            say(&format!("  {label:<18} INSUFFICIENT DATA ({u})"));
            continue;
        }
        let mut parts = Vec::new();
        if let Some(PyValue::Map(vs)) = row.get("variants") {
            for (k, v) in vs.iter() {
                let PyValue::Map(m) = v else { continue };
                let f = |key: &str| match m.get(key) {
                    Some(PyValue::Float(x)) => *x,
                    _ => f64::NAN,
                };
                parts.push(format!(
                    "{k}: CAGR {}% maxDD {}%",
                    fmt(f("CAGR"), ">6.2f"),
                    fmt(f("max_drawdown_pct"), ">7.2f")
                ));
            }
        }
        say(&format!("  {label:<18} {}", parts.join("   |   ")));
    }

    if let Some(save) = a.save.as_deref().filter(|s| !s.is_empty()) {
        let path = state.paths.resolve(save);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| BtError::Py(format!("OSError: {e}")))?;
        }
        let grid = match grid {
            Some(g) => g,
            None => {
                say("\n(--save: computing the robustness grid so the saved artifact is complete)");
                robustness_grid(&ctx, &vf)?
            }
        };
        let timelines = timelines.unwrap_or_else(|| event_timelines(&sched));
        let tier_distribution: PyDict = detail
            .iter()
            .map(|(k, sch, _)| {
                let m: PyDict = model::tier_counts(sch)
                    .into_iter()
                    .map(|(t, v)| (t, PyValue::Float(v)))
                    .collect();
                ((*k).to_owned(), PyValue::Map(m))
            })
            .collect();
        let mut whipsaw = PyDict::new();
        for w in parse_windows(&a.reversal_windows)? {
            whipsaw.insert(
                w.to_string(),
                PyValue::Float(model::count_whipsaws(&sched, &proxy, w).1),
            );
        }
        let breakdown: PyDict = costs
            .breakdown(3_000_000.0, false)
            .into_iter()
            .map(|(k, v)| (k, PyValue::Float(v)))
            .collect();
        let unavailable: PyDict = VARIANTS
            .iter()
            .filter(|v| !v.available)
            .map(|v| (v.key, PyValue::Str(v.unavailable_reason.to_owned())))
            .collect();
        let s = |x: &str| PyValue::Str(x.to_owned());
        let payload = dict([
            (
                "generated_for",
                PyValue::Map(dict([("start", s(&iso(start))), ("end", s(&iso(end)))])),
            ),
            (
                "limitations",
                PyValue::Map(dict([
                    (
                        "portfolio_series",
                        s(&format!(
                            "{} index — PROXY, not the live book",
                            cfg.momentum_sentinel
                        )),
                    ),
                    ("return_type", s("PRICE RETURN (Kite indices carry no TRI)")),
                    ("breadth", s(breadth_text)),
                    (
                        "execution",
                        s("next session OPEN after the weekly close (no lookahead)"),
                    ),
                    (
                        "book_weights",
                        s("configured defaults (no historical full-risk snapshots)"),
                    ),
                    ("slippage", s(&slip_text)),
                    ("cash_rate", s(&cash_text)),
                ])),
            ),
            ("variants", variants_json(&rows)),
            ("curves", model::curve_payload(&detail)),
            ("robustness", PyValue::List(grid)),
            (
                "timelines",
                PyValue::List(timelines.into_iter().map(PyValue::Map).collect()),
            ),
            (
                "subperiods",
                PyValue::List(subperiods.into_iter().map(PyValue::Map).collect()),
            ),
            ("tier_distribution", PyValue::Map(tier_distribution)),
            ("cost_breakdown", PyValue::Map(breakdown)),
            (
                "cost_one_way",
                PyValue::Float(costs.one_way(3_000_000.0, false)),
            ),
            (
                "cost_round_trip",
                PyValue::Float(costs.round_trip(3_000_000.0)),
            ),
            ("whipsaw_sensitivity", PyValue::Map(whipsaw)),
            ("unavailable", PyValue::Map(unavailable)),
        ]);
        std::fs::write(&path, dumps_indent2(&PyValue::Map(payload)))
            .map_err(|e| BtError::Py(format!("OSError: {e}")))?;
        say(&format!("\nsaved: {save}"));
    }

    say("");
    say(
        "Results are reported, not endorsed. Run this before enabling enforce mode and judge the thresholds yourself.",
    );
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| (*x).to_owned()).collect()
    }

    #[test]
    fn argv_and_list_match_python() {
        let want = Args {
            run: true,
            save: Some(SAVE_CONST.to_owned()),
            start: "2005-04-01".to_owned(),
            ..Args::default()
        };
        assert_eq!(
            parse_args(&s(&["--run", "--save", "--start=2005-04-01"])).unwrap(),
            Parsed::Args(want)
        );
        assert!(
            parse_args(&s(&["--r"]))
                .unwrap_err()
                .starts_with("ambiguous option")
        );
        assert_eq!(parse_args(&s(&["-h"])).unwrap(), Parsed::Help);
        let lines = list_lines();
        assert_eq!(lines[0], "key                   status          design");
        assert_eq!(
            lines[6],
            "  D                   NOT AVAILABLE   M50 50-DMA middle ground (skip entries, no liquidation)"
        );
    }
}
