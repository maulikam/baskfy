//! `app/analytics/nse_indices.py`: the NSE public-data adapter — the live index board, the
//! daily close report and each index's constituent list, each cached on disk.
//!
//! Two NSE surfaces with very different tolerances, and that difference is the design: the
//! board (`allIndices`) is cookie-gated but content to be polled; the archive host bans a burst
//! outright, so archive requests go **one at a time**, never closer than [`MIN_ARCHIVE_GAP`],
//! constituent lists are cached for a month, and a 403 silences the module for
//! [`ARCHIVE_BACKOFF`] (a ban costs every index at once). Nothing pre-warms the cache. No
//! function here raises: a stale or empty panel, never a 500.
//!
//! **The wire (DECISIONS-RS lane analytics_regime R8.AR6):** this binary has **no live NSE
//! client** — as it has no live Kite client (R8.0). Every request goes through [`NseHttp`]:
//! [`Offline`] (the production default: every request fails, so the page serves its disk cache
//! or says NSE is unreachable) or [`Replay`] (the parity harness: files from a fixture
//! directory, shared byte for byte with `tools/parity/desk_nse_replay.py`). A live client is a
//! cutover item.
//!
//! The process-wide state Python keeps in module globals (`_last_archive_at`, `_blocked_until`,
//! and `_lock`, which serialises the close report and the constituent fetch) lives in one
//! [`Nse`] value; the app holds one for its life ([`desk_nse`]).

use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};

use async_trait::async_trait;
use chrono::{Datelike, Duration, NaiveDate};

use crate::clock::Clock;
use crate::wire::{PyDict, PyValue, dict};

/// `BOARD_URL`.
pub const BOARD_URL: &str = "https://www.nseindia.com/api/allIndices";
/// `ARCHIVE_BASE`.
pub const ARCHIVE_BASE: &str = "https://nsearchives.nseindia.com/content/indices";
/// `NIFTYINDICES`.
pub const NIFTYINDICES: &str = "https://www.niftyindices.com/IndexConstituent";
/// `BOARD_CACHE`.
pub const BOARD_CACHE: &str = "data/.nse_board.json";
/// `CONSTITUENT_CACHE`.
pub const CONSTITUENT_CACHE: &str = "data/.nse_constituents.json";
/// `CLOSE_CACHE`.
pub const CLOSE_CACHE: &str = "data/.nse_close.json";
/// `BOARD_TTL` (seconds): the board is a quote feed.
pub const BOARD_TTL: f64 = 90.0;
/// `CLOSE_BACK_DAYS`: walk back over a weekend plus a holiday or two.
pub const CLOSE_BACK_DAYS: i64 = 6;
/// `CLOSE_TODAY_TTL` (seconds): today's report is still being written.
pub const CLOSE_TODAY_TTL: f64 = 1800.0;
/// `CONSTITUENT_TTL` (seconds): membership changes at a semi-annual review.
pub const CONSTITUENT_TTL: f64 = 30.0 * 86400.0;
/// `FAILURE_TTL` (seconds): how long a "no such list" answer is remembered.
pub const FAILURE_TTL: f64 = 6.0 * 3600.0;
/// `MIN_ARCHIVE_GAP` (seconds between two archive requests, process-wide).
pub const MIN_ARCHIVE_GAP: f64 = 3.0;
/// `ARCHIVE_BACKOFF` (seconds of silence after the host says no).
pub const ARCHIVE_BACKOFF: f64 = 3600.0;

/// `ACRONYMS`: kept whole in a niftyindices filename (`ind_niftyNBFC_list.csv`).
pub const ACRONYMS: [&str; 21] = [
    "IT", "NBFC", "FMCG", "PSU", "PSE", "CPSE", "ESG", "MNC", "EV", "IPO", "SME", "FPI", "USD",
    "TR", "PR", "MAATR", "REIT", "REITS", "INVIT", "INVITS", "GS",
];

// ---------------------------------------------------------------------------------------------
// the wire
// ---------------------------------------------------------------------------------------------

/// One HTTP answer: `r.status_code` and `r.text`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpReply {
    /// `status_code`.
    pub status: u16,
    /// `text`.
    pub text: String,
}

/// Every request this module makes. `Err` is a transport failure (`str(exc)`).
#[async_trait]
pub trait NseHttp: Send + Sync {
    /// The primed session's `GET BOARD_URL`.
    async fn board(&self) -> Result<HttpReply, String>;
    /// `requests.get(url)` on the archive hosts.
    async fn get(&self, url: &str) -> Result<HttpReply, String>;
}

/// No network: every request fails with `"{prefix} ({url})"`.
#[derive(Debug, Clone)]
pub struct Offline {
    /// The message's head.
    pub prefix: String,
}

impl Offline {
    /// The production default: no live NSE client in this binary.
    #[must_use]
    pub fn production() -> Self {
        Self {
            prefix:
                "NSE is not reachable from the Rust desk: no live NSE client (DECISIONS-RS R8.AR6)"
                    .to_owned(),
        }
    }

    /// The harness's: the same text `desk_nse_replay.py` raises.
    #[must_use]
    pub fn harness() -> Self {
        Self {
            prefix: "harness: no network".to_owned(),
        }
    }
}

#[async_trait]
impl NseHttp for Offline {
    async fn board(&self) -> Result<HttpReply, String> {
        Err(format!("{} ({BOARD_URL})", self.prefix))
    }

    async fn get(&self, url: &str) -> Result<HttpReply, String> {
        Err(format!("{} ({url})", self.prefix))
    }
}

/// The parity harness's NSE: `allIndices.json` is the board's body; an archive URL is answered
/// by the file named as its last path segment (200), else 404 with an empty body; an optional
/// `status.json` (`{"<file name>": 403}`) overrides a status. With no board file the board is a
/// transport failure, as when offline.
#[derive(Debug, Clone)]
pub struct Replay {
    /// The fixture directory.
    pub dir: PathBuf,
}

impl Replay {
    fn status_override(&self, name: &str) -> Option<u16> {
        let raw = std::fs::read(self.dir.join("status.json")).ok()?;
        match PyValue::from_json(&raw).ok()? {
            PyValue::Map(m) => match m.get(name) {
                Some(PyValue::Int(i)) => u16::try_from(*i).ok(),
                _ => None,
            },
            _ => None,
        }
    }
}

#[async_trait]
impl NseHttp for Replay {
    async fn board(&self) -> Result<HttpReply, String> {
        match std::fs::read_to_string(self.dir.join("allIndices.json")) {
            Ok(text) => Ok(HttpReply {
                status: self.status_override("allIndices.json").unwrap_or(200),
                text,
            }),
            Err(_) => Err(format!("harness: no network ({BOARD_URL})")),
        }
    }

    async fn get(&self, url: &str) -> Result<HttpReply, String> {
        let name = url.rsplit('/').next().unwrap_or(url);
        let text = std::fs::read_to_string(self.dir.join(name)).ok();
        let status = self
            .status_override(name)
            .unwrap_or(if text.is_some() { 200 } else { 404 });
        Ok(HttpReply {
            status,
            text: text.unwrap_or_default(),
        })
    }
}

// ---------------------------------------------------------------------------------------------
// the module's state
// ---------------------------------------------------------------------------------------------

#[derive(Debug, Default)]
struct Globals {
    last_archive_at: f64,
    blocked_until: f64,
}

/// The tunables tests move (`monkeypatch.setattr(NSE, ...)`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Tunables {
    /// `BOARD_TTL`.
    pub board_ttl: f64,
    /// `CLOSE_TODAY_TTL`.
    pub close_today_ttl: f64,
    /// `MIN_ARCHIVE_GAP`.
    pub min_archive_gap: f64,
}

impl Default for Tunables {
    fn default() -> Self {
        Self {
            board_ttl: BOARD_TTL,
            close_today_ttl: CLOSE_TODAY_TTL,
            min_archive_gap: MIN_ARCHIVE_GAP,
        }
    }
}

/// The module: its wire, its clock, its three caches and its process-wide state.
pub struct Nse {
    http: Arc<dyn NseHttp>,
    clock: Arc<dyn Clock>,
    board_cache: PathBuf,
    constituent_cache: PathBuf,
    close_cache: PathBuf,
    tunables: std::sync::Mutex<Tunables>,
    globals: tokio::sync::Mutex<Globals>,
    lock: tokio::sync::Mutex<()>,
}

impl std::fmt::Debug for Nse {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Nse")
            .field("board_cache", &self.board_cache)
            .finish_non_exhaustive()
    }
}

impl Nse {
    /// The module over `root` (the desk's working directory), its caches under `data/`.
    #[must_use]
    pub fn new(http: Arc<dyn NseHttp>, clock: Arc<dyn Clock>, root: &Path) -> Self {
        Self::with_caches(
            http,
            clock,
            root.join(BOARD_CACHE),
            root.join(CONSTITUENT_CACHE),
            root.join(CLOSE_CACHE),
        )
    }

    /// The module with its three cache files named.
    #[must_use]
    pub fn with_caches(
        http: Arc<dyn NseHttp>,
        clock: Arc<dyn Clock>,
        board_cache: PathBuf,
        constituent_cache: PathBuf,
        close_cache: PathBuf,
    ) -> Self {
        Self {
            http,
            clock,
            board_cache,
            constituent_cache,
            close_cache,
            tunables: std::sync::Mutex::new(Tunables::default()),
            globals: tokio::sync::Mutex::new(Globals::default()),
            lock: tokio::sync::Mutex::new(()),
        }
    }

    /// The tunables now.
    #[must_use]
    pub fn tunables(&self) -> Tunables {
        match self.tunables.lock() {
            Ok(t) => *t,
            Err(p) => *p.into_inner(),
        }
    }

    /// Move a tunable (tests).
    pub fn set_tunables(&self, t: Tunables) {
        match self.tunables.lock() {
            Ok(mut g) => *g = t,
            Err(p) => *p.into_inner() = t,
        }
    }

    /// `_blocked_until = 0.0` (tests).
    pub async fn unblock(&self) {
        self.globals.lock().await.blocked_until = 0.0;
    }

    fn now(&self) -> f64 {
        self.clock.time()
    }

    fn stamp(&self) -> String {
        self.clock
            .now_ist()
            .naive_local()
            .format("%d %b %Y %H:%M")
            .to_string()
    }
}

/// The desk's one module instance: the harness's replay (`$BASKFY_NSE_REPLAY_DIR`, else
/// `<--harness fixture>/nse`) or offline under the harness; offline otherwise (R8.AR6).
pub fn desk_nse(state: &crate::app::AppState) -> Arc<Nse> {
    static NSE: OnceLock<Arc<Nse>> = OnceLock::new();
    NSE.get_or_init(|| {
        let http: Arc<dyn NseHttp> = if state.harness.is_some() {
            match harness_replay_dir() {
                Some(dir) => Arc::new(Replay { dir }),
                None => Arc::new(Offline::harness()),
            }
        } else {
            Arc::new(Offline::production())
        };
        Arc::new(Nse::new(http, state.clock.clone(), &state.paths.root))
    })
    .clone()
}

fn harness_replay_dir() -> Option<PathBuf> {
    if let Ok(dir) = std::env::var("BASKFY_NSE_REPLAY_DIR")
        && !dir.is_empty()
    {
        return Some(PathBuf::from(dir));
    }
    let args: Vec<String> = std::env::args().collect();
    let fixture = args
        .iter()
        .position(|a| a == "--harness")
        .and_then(|i| args.get(i + 1))?;
    let dir = PathBuf::from(fixture).join("nse");
    dir.is_dir().then_some(dir)
}

// ---------------------------------------------------------------------------------------------
// disk cache
// ---------------------------------------------------------------------------------------------

fn read_cache(path: &Path) -> PyDict {
    match std::fs::read(path)
        .ok()
        .and_then(|raw| PyValue::from_json(&raw).ok())
    {
        Some(PyValue::Map(m)) => m,
        _ => PyDict::new(),
    }
}

/// `json.dump(payload, f)`: Python's default separators, `ensure_ascii`, `NaN` allowed.
#[must_use]
pub fn json_dump(value: &PyValue) -> String {
    fn s(text: &str) -> String {
        let mut out = String::with_capacity(text.len() + 2);
        out.push('"');
        for ch in text.chars() {
            match ch {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\u{8}' => out.push_str("\\b"),
                '\u{c}' => out.push_str("\\f"),
                c if (c as u32) < 0x20 || (c as u32) > 0x7f => {
                    let mut buf = [0_u16; 2];
                    for unit in c.encode_utf16(&mut buf) {
                        out.push_str(&format!("\\u{unit:04x}"));
                    }
                }
                c => out.push(c),
            }
        }
        out.push('"');
        out
    }
    match value {
        PyValue::None => "null".to_owned(),
        PyValue::Bool(true) => "true".to_owned(),
        PyValue::Bool(false) => "false".to_owned(),
        PyValue::Int(i) => i.to_string(),
        PyValue::Float(f) if f.is_nan() => "NaN".to_owned(),
        PyValue::Float(f) if f.is_infinite() => {
            if *f > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
        }
        PyValue::Float(f) => baskfy_core::py::float_repr(*f),
        PyValue::Str(t) => s(t),
        PyValue::List(items) => {
            let inner: Vec<String> = items.iter().map(json_dump).collect();
            format!("[{}]", inner.join(", "))
        }
        PyValue::Map(m) => {
            let inner: Vec<String> = m
                .iter()
                .map(|(k, v)| format!("{}: {}", s(k), json_dump(v)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
        other => s(&other.py_str()),
    }
}

/// `_write_cache(path, payload)`: through a temp file in the same directory, then a rename — a
/// half-written cache is worse than none.
fn write_cache(path: &Path, payload: &PyDict) -> Result<(), String> {
    if let Some(dir) = path.parent()
        && !dir.as_os_str().is_empty()
    {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let tmp = PathBuf::from(format!("{}.tmp", path.display()));
    std::fs::write(&tmp, json_dump(&PyValue::Map(payload.clone()))).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())
}

fn get(m: &PyDict, k: &str) -> PyValue {
    m.get(k).cloned().unwrap_or(PyValue::None)
}

/// `float(x or 0)` over a cached epoch.
fn epoch(m: &PyDict, k: &str) -> f64 {
    match m.get(k) {
        Some(PyValue::Float(f)) => *f,
        #[allow(clippy::cast_precision_loss)]
        Some(PyValue::Int(i)) => *i as f64,
        Some(PyValue::Str(s)) if !s.is_empty() => crate::settings::py_float(s).unwrap_or(0.0),
        _ => 0.0,
    }
}

/// `{**base, **extra}`.
fn merged(base: &PyDict, extra: &[(&str, PyValue)]) -> PyDict {
    let mut out = base.clone();
    for (k, v) in extra {
        out.insert(*k, v.clone());
    }
    out
}

fn truthy_key(m: &PyDict, k: &str) -> bool {
    m.get(k).is_some_and(PyValue::truthy)
}

// ---------------------------------------------------------------------------------------------
// the live board
// ---------------------------------------------------------------------------------------------

/// `_num(v)`: `None` for `None`/`""`/`"-"`/`"NA"` and anything `float()` refuses.
#[must_use]
pub fn num(v: &PyValue) -> Option<f64> {
    match v {
        PyValue::None => None,
        PyValue::Str(s) if s.is_empty() || s == "-" || s == "NA" => None,
        PyValue::Str(s) => crate::settings::py_float(s),
        PyValue::Float(f) => Some(*f),
        #[allow(clippy::cast_precision_loss)]
        PyValue::Int(i) => Some(*i as f64),
        PyValue::Bool(b) => Some(f64::from(u8::from(*b))),
        _ => None,
    }
}

fn text_strip(v: &PyValue) -> Result<String, String> {
    match v {
        PyValue::Str(s) => Ok(s.trim().to_owned()),
        other if !other.truthy() => Ok(String::new()),
        other => Err(format!(
            "'{}' object has no attribute 'strip'",
            other.type_name()
        )),
    }
}

fn opt_f(x: Option<f64>) -> PyValue {
    x.map_or(PyValue::None, PyValue::Float)
}

#[allow(clippy::cast_possible_truncation)]
fn opt_i(x: Option<f64>) -> Result<PyValue, String> {
    match x {
        None => Ok(PyValue::None),
        Some(f) if f.is_finite() => Ok(PyValue::Int(f.trunc() as i64)),
        Some(f) => Err(format!(
            "cannot convert float {} to integer",
            baskfy_core::py::float_repr(f)
        )),
    }
}

/// `_board_row(r)`: one index, reduced to what the desk shows (`index` is the full name, the one
/// an archive filename resolves).
///
/// # Errors
/// What Python raises on a malformed row.
pub fn board_row(r: &PyDict) -> Result<PyDict, String> {
    let n = |k: &str| num(&get(r, k));
    Ok(dict([
        ("name", PyValue::Str(text_strip(&get(r, "index"))?)),
        ("ticker", PyValue::Str(text_strip(&get(r, "indexSymbol"))?)),
        ("group", PyValue::Str(text_strip(&get(r, "key"))?)),
        ("last", opt_f(n("last"))),
        ("change", opt_f(n("variation"))),
        ("pct", opt_f(n("percentChange"))),
        ("open", opt_f(n("open"))),
        ("high", opt_f(n("high"))),
        ("low", opt_f(n("low"))),
        ("prev_close", opt_f(n("previousClose"))),
        ("year_high", opt_f(n("yearHigh"))),
        ("year_low", opt_f(n("yearLow"))),
        ("pe", opt_f(n("pe"))),
        ("pb", opt_f(n("pb"))),
        ("dy", opt_f(n("dy"))),
        ("ret_1y", opt_f(n("perChange365d"))),
        ("ret_30d", opt_f(n("perChange30d"))),
        ("advances", opt_i(n("advances"))?),
        ("declines", opt_i(n("declines"))?),
        ("unchanged", opt_i(n("unchanged"))?),
    ]))
}

fn parse_board(reply: &HttpReply) -> Result<Vec<PyDict>, String> {
    if reply.status >= 400 {
        return Err(format!("HTTP {} for {BOARD_URL}", reply.status));
    }
    let body = PyValue::from_json(reply.text.as_bytes())
        .map_err(|_| "Expecting value: line 1 column 1 (char 0)".to_owned())?;
    let PyValue::Map(body) = body else {
        return Err("'list' object has no attribute 'get'".to_owned());
    };
    let raw = match body.get("data") {
        Some(PyValue::List(items)) => items.clone(),
        Some(v) if v.truthy() => return Err("board data is not a list".to_owned()),
        _ => Vec::new(),
    };
    let mut rows = Vec::new();
    for x in raw {
        let PyValue::Map(x) = x else {
            return Err("'str' object has no attribute 'get'".to_owned());
        };
        if text_strip(&get(&x, "index"))?.is_empty() {
            continue;
        }
        rows.push(board_row(&x)?);
    }
    if rows.is_empty() {
        return Err("board returned no rows".to_owned());
    }
    Ok(rows)
}

impl Nse {
    /// `board(force=False)`: the live board, or the last one that worked. Never raises.
    pub async fn board(&self, force: bool) -> PyDict {
        let cached = read_cache(&self.board_cache);
        let age = self.now() - epoch(&cached, "fetched_at_epoch");
        if truthy_key(&cached, "rows") && !force && age < self.tunables().board_ttl {
            return merged(
                &cached,
                &[("stale", PyValue::Bool(false)), ("error", PyValue::None)],
            );
        }
        let fetched = match self.http.board().await {
            Ok(reply) => parse_board(&reply),
            Err(e) => Err(e),
        };
        let fetched = fetched.and_then(|rows| {
            let payload = dict([
                (
                    "rows",
                    PyValue::List(rows.into_iter().map(PyValue::Map).collect()),
                ),
                ("fetched_at", PyValue::Str(self.stamp())),
                ("fetched_at_epoch", PyValue::Float(self.now())),
            ]);
            write_cache(&self.board_cache, &payload)?;
            Ok(payload)
        });
        match fetched {
            Ok(payload) => merged(
                &payload,
                &[("stale", PyValue::Bool(false)), ("error", PyValue::None)],
            ),
            Err(e) => {
                if truthy_key(&cached, "rows") {
                    merged(
                        &cached,
                        &[("stale", PyValue::Bool(true)), ("error", PyValue::Str(e))],
                    )
                } else {
                    dict([
                        ("rows", PyValue::List(Vec::new())),
                        ("fetched_at", PyValue::None),
                        ("stale", PyValue::Bool(true)),
                        ("error", PyValue::Str(e)),
                    ])
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------
// CSV — Python's `csv.DictReader` (the excel dialect)
// ---------------------------------------------------------------------------------------------

/// `csv.reader(io.StringIO(text))`: the excel dialect (`,` `"` doubled quotes, `\r\n`/`\n`/`\r`).
#[must_use]
pub fn csv_rows(text: &str) -> Vec<Vec<String>> {
    let mut rows = Vec::new();
    let mut row: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut in_quotes = false;
    let mut quote_closed = false;
    let mut started = false;
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if in_quotes {
            if c == '"' {
                if chars.get(i + 1) == Some(&'"') {
                    field.push('"');
                    i += 2;
                    continue;
                }
                in_quotes = false;
                quote_closed = true;
            } else {
                field.push(c);
            }
            i += 1;
            continue;
        }
        match c {
            '"' if field.is_empty() && !quote_closed => {
                in_quotes = true;
                started = true;
            }
            ',' => {
                row.push(std::mem::take(&mut field));
                quote_closed = false;
                started = true;
            }
            '\r' | '\n' => {
                if started || !field.is_empty() {
                    row.push(std::mem::take(&mut field));
                }
                rows.push(std::mem::take(&mut row));
                quote_closed = false;
                started = false;
                if c == '\r' && chars.get(i + 1) == Some(&'\n') {
                    i += 1;
                }
            }
            other => {
                field.push(other);
                started = true;
            }
        }
        i += 1;
    }
    if started || !field.is_empty() || !row.is_empty() {
        row.push(field);
        rows.push(row);
    }
    rows
}

/// `csv.DictReader`: the first row names the fields; a short row's missing fields are `None`;
/// an empty row is skipped.
#[must_use]
pub fn csv_dicts(text: &str) -> Vec<Vec<(String, Option<String>)>> {
    let mut rows = csv_rows(text).into_iter();
    let Some(header) = rows.next() else {
        return Vec::new();
    };
    rows.filter(|r| !r.is_empty())
        .map(|r| {
            header
                .iter()
                .enumerate()
                .map(|(i, h)| (h.clone(), r.get(i).cloned()))
                .collect()
        })
        .collect()
}

fn field<'a>(row: &'a [(String, Option<String>)], key: &str) -> Option<&'a str> {
    row.iter()
        .rev()
        .find(|(k, _)| k == key)
        .and_then(|(_, v)| v.as_deref())
}

fn stripped(row: &[(String, Option<String>)], key: &str) -> String {
    field(row, key).unwrap_or_default().trim().to_owned()
}

// ---------------------------------------------------------------------------------------------
// the daily close report
// ---------------------------------------------------------------------------------------------

/// `_close_url(d)`.
#[must_use]
pub fn close_url(d: NaiveDate) -> String {
    format!("{ARCHIVE_BASE}/ind_close_all_{}.csv", d.format("%d%m%Y"))
}

/// `_close_num(v)`: `"-"` is absent, `".42"` is 0.42.
#[must_use]
pub fn close_num(v: Option<&str>) -> Option<f64> {
    let s = v.unwrap_or_default().trim();
    if s.is_empty() || s == "-" {
        return None;
    }
    crate::settings::py_float(s)
}

/// `_parse_close(text)`.
#[must_use]
pub fn parse_close(text: &str) -> Vec<PyDict> {
    let mut rows = Vec::new();
    for r in csv_dicts(text) {
        let name = stripped(&r, "Index Name");
        if name.is_empty() {
            continue;
        }
        let n = |k: &str| opt_f(close_num(field(&r, k)));
        rows.push(dict([
            ("name", PyValue::Str(name)),
            ("date", PyValue::Str(stripped(&r, "Index Date"))),
            ("open", n("Open Index Value")),
            ("high", n("High Index Value")),
            ("low", n("Low Index Value")),
            ("last", n("Closing Index Value")),
            ("change", n("Points Change")),
            ("pct", n("Change(%)")),
            ("volume", n("Volume")),
            ("turnover", n("Turnover (Rs. Cr.)")),
            ("pe", n("P/E")),
            ("pb", n("P/B")),
            ("dy", n("Div Yield")),
        ]));
    }
    rows
}

impl Nse {
    /// One archive request through the process-wide throttle and the ban backoff.
    async fn archive_get(&self, url: &str) -> Result<HttpReply, String> {
        let gap = {
            let g = self.globals.lock().await;
            self.tunables().min_archive_gap - (self.now() - g.last_archive_at)
        };
        if gap > 0.0 {
            tokio::time::sleep(std::time::Duration::from_secs_f64(gap)).await;
        }
        self.globals.lock().await.last_archive_at = self.now();
        self.http.get(url).await
    }

    async fn refuse_if_blocked(&self) -> Result<(), String> {
        if self.now() < self.globals.lock().await.blocked_until {
            return Err("NSE archive is refusing requests; backing off".to_owned());
        }
        Ok(())
    }

    async fn ban(&self) -> String {
        self.globals.lock().await.blocked_until = self.now() + ARCHIVE_BACKOFF;
        "NSE archive returned 403 — backing off for an hour".to_owned()
    }

    /// `_fetch_close(d)`: one day's report (`[]` on a 404 — a holiday, or today before the
    /// close).
    ///
    /// # Errors
    /// The backoff, a 403, a transport failure, a reply that is not the report.
    pub async fn fetch_close(&self, d: NaiveDate) -> Result<Vec<PyDict>, String> {
        self.refuse_if_blocked().await?;
        let r = self.archive_get(&close_url(d)).await?;
        if r.status == 403 {
            return Err(self.ban().await);
        }
        if r.status == 404 {
            return Ok(Vec::new());
        }
        if r.status != 200 || !r.text.trim_start().starts_with("Index Name") {
            return Err(format!(
                "close report {} -> HTTP {}",
                d.format("%Y-%m-%d"),
                r.status
            ));
        }
        Ok(parse_close(&r.text))
    }

    /// `close_report(today=None)`: the most recent daily close report, walking back over
    /// non-trading days. A past day's report is cached for good; today's is re-read until the
    /// day is over. Never raises.
    pub async fn close_report(&self, today: NaiveDate) -> PyDict {
        let _held = self.lock.lock().await;
        let mut cache = read_cache(&self.close_cache);
        for back in 0..=CLOSE_BACK_DAYS {
            let d = today - Duration::days(back);
            let key = d.format("%Y-%m-%d").to_string();
            let hit = match cache.get(&key) {
                Some(PyValue::Map(m)) if !m.is_empty() => Some(m.clone()),
                _ => None,
            };
            if let Some(h) = &hit {
                let settled = d != today
                    || self.now() - epoch(h, "fetched_at_epoch") < self.tunables().close_today_ttl;
                if settled {
                    return merged(
                        h,
                        &[("stale", PyValue::Bool(back > 0)), ("error", PyValue::None)],
                    );
                }
            } else if d.weekday().num_days_from_monday() >= 5 {
                continue;
            }
            let rows = match self.fetch_close(d).await {
                Ok(r) => r,
                Err(e) => {
                    if let Some(h) = &hit {
                        return merged(
                            h,
                            &[("stale", PyValue::Bool(true)), ("error", PyValue::Str(e))],
                        );
                    }
                    break;
                }
            };
            if !rows.is_empty() {
                let entry = dict([
                    (
                        "rows",
                        PyValue::List(rows.into_iter().map(PyValue::Map).collect()),
                    ),
                    ("as_of", PyValue::Str(key.clone())),
                    ("fetched_at", PyValue::Str(self.stamp())),
                    ("fetched_at_epoch", PyValue::Float(self.now())),
                ]);
                cache.insert(key, PyValue::Map(entry.clone()));
                let _ = write_cache(&self.close_cache, &cache);
                return merged(
                    &entry,
                    &[("stale", PyValue::Bool(back > 0)), ("error", PyValue::None)],
                );
            }
            if let Some(h) = &hit {
                return merged(
                    h,
                    &[("stale", PyValue::Bool(true)), ("error", PyValue::None)],
                );
            }
        }
        if !cache.is_empty() {
            let newest = cache
                .iter()
                .map(|(k, _)| k.to_owned())
                .max()
                .unwrap_or_default();
            let entry = match cache.get(&newest) {
                Some(PyValue::Map(m)) => m.clone(),
                _ => PyDict::new(),
            };
            return merged(
                &entry,
                &[
                    ("stale", PyValue::Bool(true)),
                    (
                        "error",
                        PyValue::Str("could not reach NSE's archive".to_owned()),
                    ),
                ],
            );
        }
        dict([
            ("rows", PyValue::List(Vec::new())),
            ("as_of", PyValue::None),
            ("fetched_at", PyValue::None),
            ("stale", PyValue::Bool(true)),
            (
                "error",
                PyValue::Str("no close report available".to_owned()),
            ),
        ])
    }
}

fn rows_of(m: &PyDict) -> Vec<PyDict> {
    match m.get("rows") {
        Some(PyValue::List(items)) => items
            .iter()
            .filter_map(|r| match r {
                PyValue::Map(m) => Some(m.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn name_of(r: &PyDict) -> String {
    match r.get("name") {
        Some(PyValue::Str(s)) => s.clone(),
        Some(PyValue::None) | None => String::new(),
        Some(other) => other.py_str(),
    }
}

impl Nse {
    /// `full_board()`: every published index — the close report as the spine, the live feed
    /// layered on top (live wins on level and change; volume, turnover and the report's casing
    /// are kept; report-only rows are labelled `source="eod"`).
    pub async fn full_board(&self, today: NaiveDate) -> PyDict {
        let rep = self.close_report(today).await;
        let live = self.board(false).await;
        let mut by_live: Vec<(String, PyDict)> = Vec::new();
        for r in rows_of(&live) {
            let k = slug(&name_of(&r));
            if let Some(slot) = by_live.iter_mut().find(|(kk, _)| *kk == k) {
                slot.1 = r;
            } else {
                by_live.push((k, r));
            }
        }
        let mut rows: Vec<PyDict> = Vec::new();
        let mut seen: Vec<String> = Vec::new();
        for r in rows_of(&rep) {
            let k = slug(&name_of(&r));
            seen.push(k.clone());
            if let Some((_, lv)) = by_live.iter().find(|(kk, _)| *kk == k) {
                rows.push(merged(
                    lv,
                    &[
                        ("name", get(&r, "name")),
                        ("volume", get(&r, "volume")),
                        ("turnover", get(&r, "turnover")),
                        ("as_of", PyValue::None),
                        ("source", PyValue::Str("live".to_owned())),
                    ],
                ));
            } else {
                let date = get(&r, "date");
                let as_of = if date.truthy() {
                    date
                } else {
                    get(&rep, "as_of")
                };
                rows.push(merged(
                    &r,
                    &[
                        ("ticker", PyValue::Str(String::new())),
                        ("group", PyValue::Str(String::new())),
                        ("prev_close", PyValue::None),
                        ("year_high", PyValue::None),
                        ("year_low", PyValue::None),
                        ("ret_1y", PyValue::None),
                        ("ret_30d", PyValue::None),
                        ("advances", PyValue::None),
                        ("declines", PyValue::None),
                        ("unchanged", PyValue::None),
                        ("as_of", as_of),
                        ("source", PyValue::Str("eod".to_owned())),
                    ],
                ));
            }
        }
        for (k, lv) in &by_live {
            if !seen.contains(k) {
                rows.push(merged(
                    lv,
                    &[
                        ("volume", PyValue::None),
                        ("turnover", PyValue::None),
                        ("as_of", PyValue::None),
                        ("source", PyValue::Str("live".to_owned())),
                    ],
                ));
            }
        }
        let eod_only = rows
            .iter()
            .filter(|r| get(r, "source") == PyValue::Str("eod".to_owned()))
            .count();
        let live_fetched = get(&live, "fetched_at");
        let live_stale = get(&live, "stale");
        let live_error = get(&live, "error");
        dict([
            (
                "rows",
                PyValue::List(rows.into_iter().map(PyValue::Map).collect()),
            ),
            (
                "fetched_at",
                if live_fetched.truthy() {
                    live_fetched
                } else {
                    get(&rep, "fetched_at")
                },
            ),
            (
                "live_count",
                PyValue::Int(i64::try_from(by_live.len()).unwrap_or(i64::MAX)),
            ),
            (
                "eod_only",
                PyValue::Int(i64::try_from(eod_only).unwrap_or(i64::MAX)),
            ),
            ("close_as_of", get(&rep, "as_of")),
            ("close_stale", get(&rep, "stale")),
            (
                "stale",
                if live_stale.truthy() {
                    get(&rep, "stale")
                } else {
                    live_stale
                },
            ),
            (
                "error",
                if live_error.truthy() {
                    live_error
                } else {
                    get(&rep, "error")
                },
            ),
        ])
    }
}

// ---------------------------------------------------------------------------------------------
// constituents
// ---------------------------------------------------------------------------------------------

/// `slug(name)`: the archive's filename stem — every separator stripped.
#[must_use]
pub fn slug(name: &str) -> String {
    name.to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        .collect()
}

/// Python's `str.title()`: a letter after a non-letter is upper-cased, every other lower-cased.
fn py_title(p: &str) -> String {
    let mut out = String::with_capacity(p.len());
    let mut prev_cased = false;
    for ch in p.chars() {
        if ch.is_alphabetic() {
            if prev_cased {
                out.extend(ch.to_lowercase());
            } else {
                out.extend(ch.to_uppercase());
            }
            prev_cased = true;
        } else {
            out.push(ch);
            prev_cased = false;
        }
    }
    out
}

/// Python's `str.isupper()`: at least one cased character, and every cased one upper.
fn py_isupper(p: &str) -> bool {
    let mut cased = false;
    for ch in p.chars() {
        if ch.is_lowercase() {
            return false;
        }
        if ch.is_uppercase() {
            cased = true;
        }
    }
    cased
}

/// `camel(name)`: `"Nifty Sugar & Ethanol"` → `"niftySugarEthanol"`; acronyms stay whole.
#[must_use]
pub fn camel(name: &str) -> String {
    let parts: Vec<&str> = name
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|p| !p.is_empty())
        .collect();
    let mut out = String::new();
    for (i, p) in parts.iter().enumerate() {
        if i == 0 {
            out.push_str(&p.to_lowercase());
        } else if py_isupper(p) && !ACRONYMS.contains(p) {
            out.push_str(&py_title(p));
        } else {
            let mut chars = p.chars();
            if let Some(first) = chars.next() {
                out.extend(first.to_uppercase());
                out.push_str(chars.as_str());
            }
        }
    }
    out
}

/// `_archive_candidates(name)`: the four spellings NSE uses across two hosts.
#[must_use]
pub fn archive_candidates(name: &str) -> Vec<String> {
    let (s, c) = (slug(name), camel(name));
    vec![
        format!("{ARCHIVE_BASE}/ind_{s}list.csv"),
        format!("{NIFTYINDICES}/ind_{c}_list.csv"),
        format!("{ARCHIVE_BASE}/ind_{s}_list.csv"),
        format!("{NIFTYINDICES}/ind_{c}list.csv"),
    ]
}

/// `_parse_constituents(text)`.
#[must_use]
pub fn parse_constituents(text: &str) -> Vec<PyDict> {
    let mut rows = Vec::new();
    for r in csv_dicts(text) {
        let sym = stripped(&r, "Symbol");
        if sym.is_empty() {
            continue;
        }
        rows.push(dict([
            ("symbol", PyValue::Str(sym)),
            ("company", PyValue::Str(stripped(&r, "Company Name"))),
            ("industry", PyValue::Str(stripped(&r, "Industry"))),
            ("series", PyValue::Str(stripped(&r, "Series"))),
            ("isin", PyValue::Str(stripped(&r, "ISIN Code"))),
        ]));
    }
    rows
}

impl Nse {
    /// `_fetch_constituents(name)`: one index's list, throttled and ban-aware. A wrong name on
    /// niftyindices answers 200 with an HTML page, so a reply is trusted only on its CSV header.
    ///
    /// # Errors
    /// The backoff, a 403, or no candidate answering a list.
    pub async fn fetch_constituents(&self, name: &str) -> Result<Vec<PyDict>, String> {
        self.refuse_if_blocked().await?;
        let mut last_err: Option<String> = None;
        for url in archive_candidates(name) {
            let r = match self.archive_get(&url).await {
                Ok(r) => r,
                Err(e) => {
                    last_err = Some(e);
                    continue;
                }
            };
            if r.status == 403 {
                return Err(self.ban().await);
            }
            if r.status == 200 && r.text.trim_start().starts_with("Company Name") {
                let rows = parse_constituents(&r.text);
                if !rows.is_empty() {
                    return Ok(rows);
                }
            }
            last_err = Some(format!(
                "{} -> HTTP {}",
                url.rsplit('/').next().unwrap_or(&url),
                r.status
            ));
        }
        Err(format!(
            "no constituent list for {} ({})",
            baskfy_providers::py::repr_str(name),
            last_err.unwrap_or_else(|| "None".to_owned())
        ))
    }

    /// `constituents(name)`: from cache when possible; a genuine "not found" is remembered for
    /// [`FAILURE_TTL`], a rate limit never is. Never raises.
    pub async fn constituents(&self, name: &str) -> PyDict {
        let key = slug(name);
        let _held = self.lock.lock().await;
        let mut cache = read_cache(&self.constituent_cache);
        let hit = match cache.get(&key) {
            Some(PyValue::Map(m)) => m.clone(),
            _ => PyDict::new(),
        };
        let age = self.now() - epoch(&hit, "fetched_at_epoch");
        if truthy_key(&hit, "rows") && age < CONSTITUENT_TTL {
            return merged(
                &hit,
                &[("stale", PyValue::Bool(false)), ("error", PyValue::None)],
            );
        }
        let failed_age = self.now() - epoch(&hit, "failed_at_epoch");
        if !truthy_key(&hit, "rows")
            && truthy_key(&hit, "failed_at_epoch")
            && failed_age < FAILURE_TTL
        {
            let err = get(&hit, "error");
            return dict([
                ("rows", PyValue::List(Vec::new())),
                ("fetched_at", PyValue::None),
                ("stale", PyValue::Bool(true)),
                (
                    "error",
                    if err.truthy() {
                        err
                    } else {
                        PyValue::Str("no constituent list published".to_owned())
                    },
                ),
            ]);
        }
        match self.fetch_constituents(name).await {
            Err(e) => {
                if truthy_key(&hit, "rows") {
                    return merged(
                        &hit,
                        &[("stale", PyValue::Bool(true)), ("error", PyValue::Str(e))],
                    );
                }
                if !e.contains("403") && !e.contains("backing off") {
                    cache.insert(
                        key,
                        PyValue::Map(dict([
                            ("rows", PyValue::List(Vec::new())),
                            ("failed_at_epoch", PyValue::Float(self.now())),
                            ("error", PyValue::Str(e.clone())),
                        ])),
                    );
                    let _ = write_cache(&self.constituent_cache, &cache);
                }
                dict([
                    ("rows", PyValue::List(Vec::new())),
                    ("fetched_at", PyValue::None),
                    ("stale", PyValue::Bool(true)),
                    ("error", PyValue::Str(e)),
                ])
            }
            Ok(rows) => {
                let entry = dict([
                    (
                        "rows",
                        PyValue::List(rows.into_iter().map(PyValue::Map).collect()),
                    ),
                    ("fetched_at", PyValue::Str(self.stamp())),
                    ("fetched_at_epoch", PyValue::Float(self.now())),
                ]);
                cache.insert(key, PyValue::Map(entry.clone()));
                let _ = write_cache(&self.constituent_cache, &cache);
                merged(
                    &entry,
                    &[("stale", PyValue::Bool(false)), ("error", PyValue::None)],
                )
            }
        }
    }

    /// `cached_index_names()`: which indices already have a constituent list on disk.
    #[must_use]
    pub fn cached_index_names(&self) -> Vec<String> {
        let mut names: Vec<String> = read_cache(&self.constituent_cache)
            .iter()
            .map(|(k, _)| k.to_owned())
            .collect();
        names.sort();
        names
    }
}
