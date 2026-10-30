//! `app/analytics/index_view.py`: what `/indices` renders and its drill-down returns — the
//! desk's read-only view pointed outward. Three sources meet: NSE's board (with the exchange's
//! own advance/decline counts), each index's constituents, and the newest scan CSV run through
//! the desk's scoring. The join is deliberately lopsided: most constituents have no score, and
//! the page says so rather than implying a blank means zero. Per-stock quotes need a Kite
//! session; without one every quote field is `None` and the panel keeps working.

use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::SystemTime;

use super::nse_indices::{Nse, slug};
use crate::lanes::weekly::score::{ScoringConfig, load_scan, score};
use crate::lanes::weekly::pandas::py_round;
use crate::wire::{PyDict, PyValue, dict};

/// `SCAN_GLOB`'s directory and pattern: `data/uploads/scan_*.csv`.
pub const SCAN_DIR: &str = "data/uploads";

/// `NO_CONSTITUENTS`: rows that render without a drill-down control.
pub const NO_CONSTITUENTS: [&str; 9] = [
    "VIX",
    "G-SEC",
    "GS ",
    "BHARATBOND",
    "BHARAT BOND",
    "LEVERAGE",
    "INVERSE",
    "DIVIDEND POINTS",
    "USD",
];

/// `BROAD`.
pub const BROAD: [&str; 25] = [
    "NIFTY 50",
    "NIFTY NEXT 50",
    "NIFTY 100",
    "NIFTY 200",
    "NIFTY 500",
    "NIFTY MIDCAP 50",
    "NIFTY MIDCAP 100",
    "NIFTY MIDCAP 150",
    "NIFTY MIDCAP SELECT",
    "NIFTY MIDCAP LIQUID 15",
    "NIFTY100 LIQUID 15",
    "NIFTY SMALLCAP 50",
    "NIFTY SMALLCAP 100",
    "NIFTY SMALLCAP 250",
    "NIFTY SMALLCAP 500",
    "NIFTY MICROCAP 250",
    "NIFTY TOTAL MARKET",
    "NIFTY LARGEMIDCAP 250",
    "NIFTY MIDSMALLCAP 400",
    "NIFTY MIDSMALLCAP400 50:50",
    "NIFTY SME EMERGE",
    "NIFTY IPO",
    "NIFTY500 MULTICAP 50:25:25",
    "NIFTY INDIA FPI 150",
    "NIFTY500 LARGEMIDSMALL EQUAL-CAP WEIGHTED",
];

const THEME_WORDS: [&str; 12] = [
    "DEFENCE",
    "RAILWAY",
    "TOURISM",
    "INTERNET",
    "DIGITAL",
    "AUTOMOTIVE",
    "CORPORATE GROUP",
    "MAATR",
    "CONGLOMERATE",
    "ESG",
    "SHARIAH",
    "AHIMSA",
];

const FACTOR_WORDS: [&str; 11] = [
    "MOMENTUM",
    "QUALITY",
    "VALUE",
    "ALPHA",
    "VOLATILITY",
    "HIGH BETA",
    "MULTIFACTOR",
    "MQVLV",
    "EQUAL WEIGHT",
    "EQUAL-CAP",
    "DIVIDEND OPPORTUNIT",
];

const SECTOR_WORDS: &[&str] = &[
    "BANK",
    "NBFC",
    "INSURANCE",
    "FINANCIAL SERVICES",
    "CAPITAL MARKETS",
    "HOUSING FINANCE",
    "IT",
    "PHARMA",
    "HEALTHCARE",
    "HOSPITALS",
    "AUTO",
    "FMCG",
    "METAL",
    "REALTY",
    "REITS",
    "MEDIA",
    "ENERGY",
    "OIL & GAS",
    "POWER",
    "TELECOM",
    "CEMENT",
    "CHEMICALS",
    "CAPITAL GOODS",
    "CONSUMER DURABLES",
    "RETAIL",
    "CONSTRUCTION",
    "COMMODITIES",
    "SUGAR",
];

/// `_drillable(name)`.
#[must_use]
pub fn drillable(name: &str) -> bool {
    let up = name.to_uppercase();
    !NO_CONSTITUENTS.iter().any(|t| up.contains(t))
}

/// `family(name)`: Broad / Sector / Factor / Theme / Derived. Theme words are tested before
/// factor words ("NIFTY INDIA DEFENCE EQUAL WEIGHT" is a theme).
#[must_use]
pub fn family(name: &str) -> &'static str {
    let up = name.to_uppercase();
    if NO_CONSTITUENTS.iter().any(|t| up.contains(t)) {
        return "Derived";
    }
    if BROAD.contains(&up.as_str()) {
        return "Broad";
    }
    if THEME_WORDS.iter().any(|t| up.contains(t)) {
        return "Theme";
    }
    if FACTOR_WORDS.iter().any(|t| up.contains(t)) {
        return "Factor";
    }
    if SECTOR_WORDS.iter().any(|t| up.contains(t)) {
        return "Sector";
    }
    "Theme"
}

// ---------------------------------------------------------------------------------------------
// the screen
// ---------------------------------------------------------------------------------------------

/// `scan_scores()`'s answer.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ScanScores {
    /// `by_symbol`, in the scored frame's order.
    pub by_symbol: Vec<(String, PyDict)>,
    /// `as_of`.
    pub as_of: Option<String>,
    /// `path`.
    pub path: Option<PathBuf>,
}

impl ScanScores {
    /// `by_symbol.get(symbol)`.
    #[must_use]
    pub fn get(&self, symbol: &str) -> Option<&PyDict> {
        self.by_symbol
            .iter()
            .find(|(k, _)| k == symbol)
            .map(|(_, v)| v)
    }

    /// `count`.
    #[must_use]
    pub fn count(&self) -> usize {
        self.by_symbol.len()
    }
}

/// `latest_scan_path()`: the newest `data/uploads/scan_*.csv` by modification time.
#[must_use]
pub fn latest_scan_path(root: &Path) -> Option<(PathBuf, SystemTime)> {
    let dir = root.join(SCAN_DIR);
    let mut found: Vec<(PathBuf, SystemTime)> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(Result::ok)
        .filter(|e| {
            let n = e.file_name().to_string_lossy().into_owned();
            n.starts_with("scan_") && n.ends_with(".csv")
        })
        .filter_map(|e| {
            let m = e.metadata().ok()?.modified().ok()?;
            Some((e.path(), m))
        })
        .collect();
    found.sort_by_key(|a| std::cmp::Reverse(a.1));
    found.into_iter().next()
}

fn opt_f(x: f64) -> PyValue {
    if x.is_nan() {
        PyValue::None
    } else {
        PyValue::Float(x)
    }
}

/// Score a scan file: `{symbol: {...}}` and the newest `date`.
type Scores = (Vec<(String, PyDict)>, Option<String>);

fn score_file(path: &Path, cfg: &ScoringConfig) -> Result<Scores, String> {
    let raw = std::fs::read(path).map_err(|e| e.to_string())?;
    let u = load_scan(&raw, cfg).map_err(|e| e.to_string())?;
    let scored = score(&u, cfg).map_err(|e| e.to_string())?;
    let as_of = if scored.frame.has("date") && !scored.is_empty() {
        scored
            .frame
            .strs("date")
            .and_then(|v| v.into_iter().flatten().max())
            .map(|d| d.chars().take(10).collect())
    } else {
        None
    };
    let mut out: Vec<(String, PyDict)> = Vec::new();
    for i in 0..scored.len() {
        let sym = scored.symbol(i).trim().to_owned();
        if sym.is_empty() {
            continue;
        }
        let n = |k: &str| opt_f(scored.num(k, i));
        let rank = scored.rank.get(i).copied().unwrap_or(f64::NAN);
        let reject = scored.reject.get(i).cloned().unwrap_or_default();
        #[allow(clippy::cast_possible_truncation)]
        let row = dict([
            (
                "score",
                opt_f(scored.score.get(i).copied().unwrap_or(f64::NAN)),
            ),
            (
                "rank",
                if rank.is_nan() {
                    PyValue::None
                } else {
                    PyValue::Int(rank.trunc() as i64)
                },
            ),
            (
                "reject",
                if reject.is_empty() {
                    PyValue::None
                } else {
                    PyValue::Str(reject)
                },
            ),
            ("close", n("close")),
            ("ma_50", n("ma_50")),
            ("ma_200", n("ma_200")),
            ("rsi", n("rsi_one_year")),
            ("ret_1y", n("absolute_return_one_year")),
            ("ret_3m", n("absolute_return_three_months")),
            ("ret_1m", n("absolute_return_one_month")),
            ("away_ath", n("away_from_high_all_time")),
            ("vol_1y", n("volatility_one_year")),
            ("beta", n("beta")),
            ("trend", opt_f(scored.parts[0].get(i).copied().unwrap_or(f64::NAN))),
            (
                "momentum",
                opt_f(scored.parts[1].get(i).copied().unwrap_or(f64::NAN)),
            ),
        ]);
        if let Some(slot) = out.iter_mut().find(|(k, _)| *k == sym) {
            slot.1 = row;
        } else {
            out.push((sym, row));
        }
    }
    Ok((out, as_of))
}

/// `scan_scores()`: the newest scan, scored, keyed by symbol — memoised on `(path, mtime)`. A
/// failure degrades to an empty map (the board is useful with no scan at all).
#[must_use]
pub fn scan_scores(root: &Path, cfg: &ScoringConfig) -> ScanScores {
    type Memo = Mutex<Option<(PathBuf, SystemTime, ScanScores)>>;
    static MEMO: OnceLock<Memo> = OnceLock::new();
    let Some((path, mtime)) = latest_scan_path(root) else {
        return ScanScores::default();
    };
    let memo = MEMO.get_or_init(|| Mutex::new(None));
    let mut guard = match memo.lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    if let Some((p, m, cached)) = guard.as_ref()
        && *p == path
        && *m == mtime
    {
        return cached.clone();
    }
    let (by_symbol, as_of) = match score_file(&path, cfg) {
        Ok(x) => x,
        Err(e) => {
            #[allow(clippy::print_stderr)]
            {
                eprintln!("WARNING:index_view:scan scoring failed for {}: {e}", path.display());
            }
            (Vec::new(), None)
        }
    };
    let out = ScanScores {
        by_symbol,
        as_of,
        path: Some(path.clone()),
    };
    *guard = Some((path, mtime, out.clone()));
    out
}

// ---------------------------------------------------------------------------------------------
// board
// ---------------------------------------------------------------------------------------------

fn f(m: &PyDict, k: &str) -> Option<f64> {
    match m.get(k) {
        Some(PyValue::Float(x)) => Some(*x),
        #[allow(clippy::cast_precision_loss)]
        Some(PyValue::Int(i)) => Some(*i as f64),
        _ => None,
    }
}

fn i(m: &PyDict, k: &str) -> Option<i64> {
    match m.get(k) {
        Some(PyValue::Int(i)) => Some(*i),
        _ => None,
    }
}

fn pos(lo: Option<f64>, hi: Option<f64>, last: Option<f64>) -> PyValue {
    let (Some(lo), Some(hi), Some(last)) = (lo, hi, last) else {
        return PyValue::None;
    };
    if hi <= lo {
        return PyValue::None;
    }
    PyValue::Float(py_round(((last - lo) / (hi - lo)).clamp(0.0, 1.0) * 100.0, 1))
}

/// `_range_pos(r)`: where the close sits inside the day's range, 0–100.
#[must_use]
pub fn range_pos(r: &PyDict) -> PyValue {
    pos(f(r, "low"), f(r, "high"), f(r, "last"))
}

/// `_year_pos(r)`.
#[must_use]
pub fn year_pos(r: &PyDict) -> PyValue {
    pos(f(r, "year_low"), f(r, "year_high"), f(r, "last"))
}

fn name_of(r: &PyDict) -> String {
    match r.get("name") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

/// `_pick(rows, selected)`.
#[must_use]
pub fn pick(rows: &[PyDict], selected: &str) -> PyValue {
    if rows.is_empty() {
        return PyValue::None;
    }
    if !selected.is_empty() {
        let want = slug(selected);
        if let Some(r) = rows.iter().find(|r| slug(&name_of(r)) == want) {
            return PyValue::Map(r.clone());
        }
    }
    if let Some(r) = rows.iter().find(|r| name_of(r).to_uppercase() == "NIFTY 50") {
        return PyValue::Map(r.clone());
    }
    PyValue::Map(rows[0].clone())
}

fn get(m: &PyDict, k: &str) -> PyValue {
    m.get(k).cloned().unwrap_or(PyValue::None)
}

/// `build(selected)`: everything `/indices` needs for its first paint.
pub async fn build(nse: &Nse, scan: &ScanScores, today: chrono::NaiveDate, selected: &str) -> PyDict {
    let b = nse.full_board(today).await;
    build_from_board(&b, scan, selected)
}

/// `build(selected)` over an already-fetched `full_board()`.
#[must_use]
pub fn build_from_board(b: &PyDict, scan: &ScanScores, selected: &str) -> PyDict {
    let board_rows: Vec<PyDict> = match b.get("rows") {
        Some(PyValue::List(items)) => items
            .iter()
            .filter_map(|r| match r {
                PyValue::Map(m) => Some(m.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    };
    let mut rows: Vec<PyDict> = Vec::with_capacity(board_rows.len());
    for r in board_rows {
        let adv = i(&r, "advances");
        let total: Option<i64> = {
            let t: i64 = [adv, i(&r, "declines"), i(&r, "unchanged")]
                .into_iter()
                .flatten()
                .sum();
            (t != 0).then_some(t)
        };
        #[allow(clippy::cast_precision_loss)]
        let adv_pct = match (adv, total) {
            (Some(a), Some(t)) => PyValue::Float(py_round(a as f64 / t as f64 * 100.0, 1)),
            _ => PyValue::None,
        };
        let name = name_of(&r);
        let mut row = r.clone();
        row.insert("family", PyValue::Str(family(&name).to_owned()));
        row.insert("drillable", PyValue::Bool(drillable(&name)));
        row.insert("members", total.map_or(PyValue::None, PyValue::Int));
        row.insert("adv_pct", adv_pct);
        row.insert("range_pos", range_pos(&r));
        row.insert("year_pos", year_pos(&r));
        rows.push(row);
    }
    rows.sort_by(|a, b| {
        let ka = (f(a, "pct").is_none(), -f(a, "pct").unwrap_or(0.0));
        let kb = (f(b, "pct").is_none(), -f(b, "pct").unwrap_or(0.0));
        ka.0.cmp(&kb.0)
            .then(ka.1.partial_cmp(&kb.1).unwrap_or(std::cmp::Ordering::Equal))
    });
    let chosen = pick(&rows, selected);
    let count = |pred: &dyn Fn(f64) -> bool| -> i64 {
        i64::try_from(
            rows.iter()
                .filter(|r| pred(f(r, "pct").unwrap_or(0.0)))
                .count(),
        )
        .unwrap_or(i64::MAX)
    };
    let advancing = count(&|p| p > 0.0);
    let declining = count(&|p| p < 0.0);
    let unchanged = i64::try_from(
        rows.iter()
            .filter(|r| f(r, "pct").is_some_and(|p| super::super::regime::engine::feq(p, 0.0)))
            .count(),
    )
    .unwrap_or(i64::MAX);
    dict([
        (
            "rows",
            PyValue::List(rows.into_iter().map(PyValue::Map).collect()),
        ),
        ("selected", chosen),
        ("fetched_at", get(b, "fetched_at")),
        ("stale", get(b, "stale")),
        ("error", get(b, "error")),
        ("live_count", get(b, "live_count")),
        ("eod_only", get(b, "eod_only")),
        ("close_as_of", get(b, "close_as_of")),
        ("close_stale", get(b, "close_stale")),
        (
            "scan",
            PyValue::Map(dict([
                (
                    "as_of",
                    scan.as_of.clone().map_or(PyValue::None, PyValue::Str),
                ),
                (
                    "count",
                    PyValue::Int(i64::try_from(scan.count()).unwrap_or(i64::MAX)),
                ),
                (
                    "path",
                    scan.path
                        .as_ref()
                        .and_then(|p| p.file_name())
                        .map_or(PyValue::None, |n| {
                            PyValue::Str(n.to_string_lossy().into_owned())
                        }),
                ),
            ])),
        ),
        (
            "families",
            PyValue::List(
                ["All", "Broad", "Sector", "Factor", "Theme", "Derived"]
                    .iter()
                    .map(|s| PyValue::Str((*s).to_owned()))
                    .collect(),
            ),
        ),
        ("advancing", PyValue::Int(advancing)),
        ("declining", PyValue::Int(declining)),
        ("unchanged_count", PyValue::Int(unchanged)),
    ])
}

// ---------------------------------------------------------------------------------------------
// drill-down
// ---------------------------------------------------------------------------------------------

/// `_above(s, key)`.
fn above(s: Option<&PyDict>, key: &str) -> PyValue {
    let Some(s) = s else {
        return PyValue::None;
    };
    match (f(s, "close"), f(s, key)) {
        (Some(c), Some(m)) => PyValue::Bool(c > m),
        _ => PyValue::None,
    }
}

/// `_breadth(scored)`: over the scanned subset only — an unscored name is unknown, not below.
#[must_use]
pub fn breadth(scored: &[PyDict]) -> PyDict {
    let n = scored.len();
    if n == 0 {
        return dict([("n", PyValue::Int(0))]);
    }
    #[allow(clippy::cast_precision_loss)]
    let pct = |pred: &dyn Fn(&PyDict) -> bool| -> PyValue {
        let c = scored.iter().filter(|r| pred(r)).count();
        PyValue::Float(py_round((c * 100) as f64 / n as f64, 1))
    };
    dict([
        ("n", PyValue::Int(i64::try_from(n).unwrap_or(i64::MAX))),
        (
            "above_200dma",
            pct(&|r| r.get("above_200dma") == Some(&PyValue::Bool(true))),
        ),
        (
            "above_50dma",
            pct(&|r| r.get("above_50dma") == Some(&PyValue::Bool(true))),
        ),
        (
            "near_ath",
            pct(&|r| f(r, "away_ath").is_some_and(|a| a > -10.0)),
        ),
        (
            "positive_1y",
            pct(&|r| f(r, "ret_1y").unwrap_or(0.0) > 0.0),
        ),
    ])
}

/// What the drill-down's quotes came back as: `quotes` or the error `str`.
pub type Quotes = Option<Result<PyDict, String>>;

fn num_of(v: &PyValue) -> Option<f64> {
    match v {
        PyValue::Float(x) => Some(*x),
        #[allow(clippy::cast_precision_loss)]
        PyValue::Int(i) => Some(*i as f64),
        PyValue::Bool(b) => Some(f64::from(u8::from(*b))),
        _ => None,
    }
}

/// `drilldown(name, kite)`: one index's constituents with the live quote (`quotes`: `None` when
/// there is no client, the read's result otherwise) and the desk's own score; scored names sort
/// first.
#[must_use]
pub fn drilldown(name: &str, constituents: &PyDict, scan: &ScanScores, quotes: Quotes) -> PyDict {
    let rows: Vec<PyDict> = match constituents.get("rows") {
        Some(PyValue::List(items)) => items
            .iter()
            .filter_map(|r| match r {
                PyValue::Map(m) => Some(m.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    };
    let (quotes, quote_error) = match quotes {
        Some(Ok(q)) if !rows.is_empty() => (q, PyValue::None),
        Some(Err(e)) if !rows.is_empty() => (PyDict::new(), PyValue::Str(e)),
        _ => (PyDict::new(), PyValue::None),
    };
    let mut out: Vec<PyDict> = Vec::with_capacity(rows.len());
    for r in &rows {
        let sym = get(r, "symbol").py_str();
        let q = match quotes.get(&sym) {
            Some(PyValue::Map(m)) if !m.is_empty() => m.clone(),
            _ => PyDict::new(),
        };
        let s = scan.get(&sym);
        let last = get(&q, "last_price");
        let prev = get(&q, "prev_close");
        let pct = match (num_of(&last), num_of(&prev)) {
            (Some(l), Some(p)) if last.truthy() && prev.truthy() => {
                PyValue::Float(py_round((l - p) / p * 100.0, 2))
            }
            _ => PyValue::None,
        };
        let sv = |k: &str| s.map_or(PyValue::None, |m| get(m, k));
        let mut row = r.clone();
        row.insert("last", last.clone());
        row.insert("change", get(&q, "net_change"));
        row.insert("pct", pct);
        row.insert("day_low", get(&q, "low"));
        row.insert("day_high", get(&q, "high"));
        row.insert("volume", get(&q, "volume"));
        row.insert("in_scan", PyValue::Bool(s.is_some()));
        row.insert("score", sv("score"));
        row.insert("rank", sv("rank"));
        row.insert("reject", sv("reject"));
        row.insert("rsi", sv("rsi"));
        row.insert("ret_1y", sv("ret_1y"));
        row.insert("ret_3m", sv("ret_3m"));
        row.insert("away_ath", sv("away_ath"));
        row.insert("above_50dma", above(s, "ma_50"));
        row.insert("above_200dma", above(s, "ma_200"));
        out.push(row);
    }
    out.sort_by(|a, b| {
        let key = |r: &PyDict| {
            let sc = f(r, "score");
            (sc.is_none(), -sc.unwrap_or(0.0), get(r, "symbol").py_str())
        };
        let (ka, kb) = (key(a), key(b));
        ka.0.cmp(&kb.0)
            .then(ka.1.partial_cmp(&kb.1).unwrap_or(std::cmp::Ordering::Equal))
            .then(ka.2.cmp(&kb.2))
    });
    let scored: Vec<PyDict> = out
        .iter()
        .filter(|r| r.get("in_scan") == Some(&PyValue::Bool(true)))
        .cloned()
        .collect();
    let mut industries: Vec<String> = out
        .iter()
        .filter_map(|r| match r.get("industry") {
            Some(PyValue::Str(s)) if !s.is_empty() => Some(s.clone()),
            _ => None,
        })
        .collect();
    industries.sort();
    industries.dedup();
    let count = i64::try_from(out.len()).unwrap_or(i64::MAX);
    dict([
        ("index", PyValue::Str(name.to_owned())),
        (
            "rows",
            PyValue::List(out.into_iter().map(PyValue::Map).collect()),
        ),
        ("count", PyValue::Int(count)),
        (
            "scored_count",
            PyValue::Int(i64::try_from(scored.len()).unwrap_or(i64::MAX)),
        ),
        ("fetched_at", get(constituents, "fetched_at")),
        ("stale", get(constituents, "stale")),
        ("error", get(constituents, "error")),
        ("quote_error", quote_error),
        (
            "industries",
            PyValue::List(industries.into_iter().map(PyValue::Str).collect()),
        ),
        ("breadth", PyValue::Map(breadth(&scored))),
    ])
}
