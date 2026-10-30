//! `app/analytics/tradebook.py` — the Zerodha Console tradebook importer: rebuilds FIFO lots into
//! `trades` from the durable `fills` table, idempotently (re-importing the same file changes
//! nothing; strategy annotations survive a rebuild). Also the two `corporate_actions` reads the
//! rebuild needs (`load`, `apply_to_book`), private until lane `analytics` ports that module.
//!
//! **Narrowed on purpose (DECISIONS-RS entry pending, recorded in the lane report):** Python's last
//! resort for an execution time it cannot read with its seven fixed formats is
//! `pandas.to_datetime(raw, dayfirst=True)`. Here that fallback is a fixed, day-first table
//! ([`parse_when`]): a date in any of the tradebook's date formats, then `T` or a space, then
//! `HH:MM[:SS[.ffffff]]`, or the date alone (midnight, as pandas gives). A string pandas would
//! read and this table does not falls through to the trade date at 09:15, as Python does when
//! pandas fails; a time zone suffix is not read.

use std::collections::{BTreeMap, HashMap};
use std::path::Path;

use chrono::{NaiveDate, NaiveDateTime, NaiveTime};

use super::{iso_naive, local_from_ts, local_ts, pyf, pyi, round_int, round_to, sha1};
use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::{PyDict, PyValue, dict};

const BUY_WORDS: &[&str] = &["buy", "b", "bought", "purchase"];
const SELL_WORDS: &[&str] = &["sell", "s", "sold", "sale"];

const DATE_FORMATS: &[&str] = &[
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d", "%m/%d/%Y",
];
const DATETIME_FORMATS: &[&str] = &[
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%.f",
];
/// The day-first dates the pandas fallback stands in for. A year-first date is read **year-day-month
/// first** when that is a valid date, as `pd.to_datetime(dayfirst=True)` (dateutil) reads it:
/// `2025-07-02` is 7 Feb, `2025-13-01` is 13 Jan, and only `2025-07-31` falls through to ISO.
const FALLBACK_DATES: &[&str] = &[
    "%Y-%d-%m", "%Y/%d/%m", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y",
    "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
];
const FALLBACK_TIMES: &[&str] = &["%H:%M:%S%.f", "%H:%M:%S", "%H:%M"];

/// `TradebookError` (a `ValueError`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct TradebookError(pub String);

/// Everything that can stop an import: the file, or the database.
#[derive(Debug, thiserror::Error)]
pub enum ImportError {
    /// The tradebook itself.
    #[error(transparent)]
    Tradebook(#[from] TradebookError),
    /// The database.
    #[error(transparent)]
    Storage(#[from] StorageError),
}

/// One executed trade.
#[derive(Debug, Clone, PartialEq)]
pub struct Fill {
    /// `symbol`.
    pub symbol: String,
    /// `when` (naive local).
    pub when: NaiveDateTime,
    /// `BUY` | `SELL`.
    pub side: String,
    /// `quantity`.
    pub quantity: i64,
    /// `price`.
    pub price: f64,
    /// `exchange`.
    pub exchange: String,
    /// `trade_id` (may be empty).
    pub trade_id: String,
    /// `charges`.
    pub charges: f64,
}

impl Fill {
    /// A fill with the dataclass defaults (`NSE`, no trade id, no charges).
    #[must_use]
    pub fn new(symbol: &str, when: NaiveDateTime, side: &str, quantity: i64, price: f64) -> Self {
        Self {
            symbol: symbol.to_owned(),
            when,
            side: side.to_owned(),
            quantity,
            price,
            exchange: "NSE".to_owned(),
            trade_id: String::new(),
            charges: 0.0,
        }
    }

    /// `ts`: `when.timestamp()`.
    #[must_use]
    pub fn ts(&self) -> f64 {
        local_ts(self.when)
    }
}

/// An open lot during reconstruction.
#[derive(Debug, Clone, PartialEq)]
pub struct Lot {
    /// `symbol`.
    pub symbol: String,
    /// `when`.
    pub when: NaiveDateTime,
    /// `quantity`.
    pub quantity: i64,
    /// `price`.
    pub price: f64,
    /// `charges` (the unconsumed share).
    pub charges: f64,
}

/// A consumed slice: one closed `trades` row.
#[derive(Debug, Clone, PartialEq)]
pub struct Closed {
    /// `symbol`.
    pub symbol: String,
    /// `entry_ts`.
    pub entry_ts: f64,
    /// `exit_ts`.
    pub exit_ts: f64,
    /// `qty`.
    pub qty: i64,
    /// `entry_price`.
    pub entry_price: f64,
    /// `exit_price`.
    pub exit_price: f64,
    /// `pnl`.
    pub pnl: f64,
    /// `costs`.
    pub costs: f64,
}

/// A sell with no inventory to consume.
#[derive(Debug, Clone, PartialEq)]
pub struct Unmatched {
    /// `symbol`.
    pub symbol: String,
    /// `date` (ISO).
    pub date: String,
    /// `quantity`.
    pub quantity: i64,
    /// `price`.
    pub price: f64,
}

impl Unmatched {
    fn to_py(&self) -> PyValue {
        PyValue::Map(dict([
            ("symbol", PyValue::Str(self.symbol.clone())),
            ("date", PyValue::Str(self.date.clone())),
            ("quantity", PyValue::Int(self.quantity)),
            ("price", PyValue::Float(self.price)),
        ]))
    }
}

/// `Rebuild`.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Rebuild {
    /// `open_lots`.
    pub open_lots: Vec<Lot>,
    /// `closed`.
    pub closed: Vec<Closed>,
    /// `unmatched_sells`.
    pub unmatched_sells: Vec<Unmatched>,
}

/// A recorded corporate action (`corporate_actions.Action`).
#[derive(Debug, Clone, PartialEq)]
pub struct Action {
    /// `symbol`.
    pub symbol: String,
    /// `bonus` | `split`.
    pub kind: String,
    /// `ex_date`.
    pub ex_date: NaiveDate,
    /// `ratio_new`.
    pub ratio_new: f64,
    /// `ratio_old`.
    pub ratio_old: f64,
    /// `note`.
    pub note: String,
}

impl Action {
    /// `multiple`.
    #[must_use]
    pub fn multiple(&self) -> f64 {
        self.ratio_new / self.ratio_old
    }

    /// `when`: 00:01 on the ex-date — the adjustment precedes any trade that session.
    #[must_use]
    pub fn when(&self) -> NaiveDateTime {
        self.ex_date
            .and_time(NaiveTime::from_hms_opt(0, 1, 0).unwrap_or(NaiveTime::MIN))
    }
}

/// `corporate_actions.apply_to_book(book, action)`: a bonus appends a nil-cost lot dated at the
/// ex-date; a split rewrites the existing lots (quantity × m, price ÷ m to 4 places).
#[allow(clippy::cast_precision_loss)]
pub fn apply_to_book(book: &mut Vec<Lot>, action: &Action) {
    if !book.iter().any(|l| l.quantity > 0) {
        return;
    }
    let m = action.multiple();
    if action.kind == "bonus" {
        let extra: i64 = book
            .iter()
            .filter(|l| l.quantity > 0)
            .map(|l| l.quantity)
            .sum();
        let whole = round_int(extra as f64 * m).unwrap_or(0);
        if whole > 0 {
            book.push(Lot {
                symbol: action.symbol.clone(),
                when: action.when(),
                quantity: whole,
                price: 0.0,
                charges: 0.0,
            });
        }
        return;
    }
    for l in book.iter_mut().filter(|l| l.quantity > 0) {
        l.quantity = round_int(l.quantity as f64 * m).unwrap_or(0);
        l.price = round_to(l.price / m, 4);
    }
}

fn in_list(n: usize) -> String {
    vec!["?"; n].join(",")
}

fn text_params(symbols: &[String]) -> Vec<Param> {
    symbols.iter().map(|s| Param::Text(s.clone())).collect()
}

fn as_text(v: Option<&DbValue>) -> String {
    match v {
        Some(DbValue::Text(s)) => s.clone(),
        Some(DbValue::Int(i)) => i.to_string(),
        Some(DbValue::Float(f)) => baskfy_providers::py::repr_float(*f),
        _ => String::new(),
    }
}

fn as_i(v: Option<&DbValue>) -> i64 {
    #[allow(clippy::cast_possible_truncation)]
    match v {
        Some(DbValue::Int(i)) => *i,
        Some(DbValue::Float(f)) if f.is_finite() => f.trunc() as i64,
        _ => 0,
    }
}

/// `corporate_actions.load(conn, symbols)`.
///
/// # Errors
/// The driver's, or an `ex_date` that is not ISO.
pub async fn load_actions(
    conn: &mut Conn,
    symbols: Option<&[String]>,
) -> Result<Vec<Action>, StorageError> {
    let mut sql = "SELECT * FROM corporate_actions".to_owned();
    let mut args = Vec::new();
    if let Some(syms) = symbols {
        if syms.is_empty() {
            return Ok(Vec::new());
        }
        sql.push_str(&format!(" WHERE symbol IN ({})", in_list(syms.len())));
        args = text_params(syms);
    }
    sql.push_str(" ORDER BY ex_date, id");
    let cur = conn.execute(&sql, &args).await?;
    let mut out = Vec::new();
    for r in &cur.rows {
        let ex = as_text(r.get("ex_date"));
        out.push(Action {
            symbol: as_text(r.get("symbol")),
            kind: as_text(r.get("kind")),
            ex_date: NaiveDate::parse_from_str(&ex, "%Y-%m-%d")
                .map_err(|_| StorageError::Other(format!("Invalid isoformat string: {ex:?}")))?,
            ratio_new: r.get("ratio_new").and_then(DbValue::as_f64).unwrap_or(0.0),
            ratio_old: r.get("ratio_old").and_then(DbValue::as_f64).unwrap_or(0.0),
            note: as_text(r.get("note")),
        });
    }
    Ok(out)
}

// =====================================================================================
// parsing
// =====================================================================================

fn strptime(raw: &str, fmt: &str) -> Option<NaiveDateTime> {
    NaiveDateTime::parse_from_str(raw, fmt).ok()
}

fn strpdate(raw: &str, fmt: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(raw, fmt).ok()
}

/// The stand-in for `pd.to_datetime(raw, dayfirst=True)` (see the module docs).
fn pandas_fallback(raw: &str) -> Option<NaiveDateTime> {
    let date_only = |s: &str| FALLBACK_DATES.iter().find_map(|f| strpdate(s, f));
    if let Some(d) = date_only(raw) {
        return Some(d.and_time(NaiveTime::MIN));
    }
    let splits: Vec<usize> = raw
        .char_indices()
        .filter(|(_, c)| *c == 'T' || *c == ' ')
        .map(|(i, _)| i)
        .collect();
    for i in splits.into_iter().rev() {
        let (d, t) = (&raw[..i], &raw[i + 1..]);
        if let Some(day) = date_only(d)
            && let Some(time) = FALLBACK_TIMES
                .iter()
                .find_map(|f| NaiveTime::parse_from_str(t, f).ok())
        {
            return Some(day.and_time(time));
        }
    }
    None
}

/// `_parse_when(date_raw, time_raw)`: the execution timestamp when readable, else the trade date
/// at 09:15.
#[must_use]
pub fn parse_when(date_raw: &str, time_raw: &str) -> Option<NaiveDateTime> {
    let raw = time_raw.trim();
    if !raw.is_empty() {
        if let Some(at) = DATETIME_FORMATS.iter().find_map(|f| strptime(raw, f)) {
            return Some(at);
        }
        if let Some(at) = pandas_fallback(raw) {
            return Some(at);
        }
    }
    let d = date_raw.trim();
    if d.is_empty() {
        return None;
    }
    let nine_fifteen = NaiveTime::from_hms_opt(9, 15, 0)?;
    DATE_FORMATS
        .iter()
        .find_map(|f| strpdate(d, f))
        .map(|day| day.and_time(nine_fifteen))
}

/// `_num(raw)`: thousands separators and the rupee sign dropped; blank or a dash is 0; anything
/// unreadable is 0.
#[must_use]
pub fn num(raw: &str) -> f64 {
    let s = raw.replace([',', '₹'], "");
    let s = s.trim();
    if s.is_empty() || s == "-" || s == "—" {
        return 0.0;
    }
    s.parse::<f64>().unwrap_or(0.0)
}

/// Python's `csv` reader (the `excel` dialect) over the whole text: records of fields.
fn csv_records(text: &str) -> Vec<Vec<String>> {
    let mut records = Vec::new();
    let mut record: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut in_quotes = false;
    let mut at_start = true;
    let mut any = false;
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if in_quotes {
            if c == '"' {
                if chars.peek() == Some(&'"') {
                    field.push('"');
                    chars.next();
                } else {
                    in_quotes = false;
                }
            } else {
                field.push(c);
            }
            continue;
        }
        match c {
            '"' if at_start => {
                in_quotes = true;
                at_start = false;
                any = true;
            }
            ',' => {
                record.push(std::mem::take(&mut field));
                at_start = true;
                any = true;
            }
            '\r' | '\n' => {
                if c == '\r' && chars.peek() == Some(&'\n') {
                    chars.next();
                }
                if any || !field.is_empty() {
                    record.push(std::mem::take(&mut field));
                }
                records.push(std::mem::take(&mut record));
                at_start = true;
                any = false;
            }
            _ => {
                field.push(c);
                at_start = false;
                any = true;
            }
        }
    }
    if any || !field.is_empty() || !record.is_empty() {
        record.push(field);
        records.push(record);
    }
    records
}

fn py_list_repr(items: &[String]) -> String {
    PyValue::List(items.iter().map(|s| PyValue::Str(s.clone())).collect()).py_repr()
}

/// `parse_tradebook(path)`: read a Console tradebook export, columns matched leniently.
///
/// # Errors
/// [`TradebookError`] with Python's messages: unreadable file, missing columns, a row with more
/// fields than the header, an unrecognised trade type, no parseable date, no usable trades.
#[allow(clippy::too_many_lines)]
pub fn parse_tradebook(path: &Path) -> Result<Vec<Fill>, TradebookError> {
    let shown = path.display().to_string();
    let bytes = std::fs::read(path).map_err(|e| TradebookError(format!("{shown}: {e}")))?;
    let text = String::from_utf8(bytes).map_err(|e| TradebookError(format!("{shown}: {e}")))?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    let mut records = csv_records(text).into_iter();
    let header = loop {
        match records.next() {
            None => return Err(TradebookError(format!("{shown}: empty or unreadable CSV"))),
            Some(r) if r.is_empty() => {}
            Some(r) => break r,
        }
    };
    // `{(c or "").strip().lower(): c}` — a later duplicate keeps the first one's position.
    let mut cols: Vec<(String, String)> = Vec::new();
    for c in &header {
        let key = c.trim().to_lowercase();
        if let Some(slot) = cols.iter_mut().find(|(k, _)| *k == key) {
            slot.1.clone_from(c);
        } else {
            cols.push((key, c.clone()));
        }
    }
    let find = |needles: &[&str], exclude: &[&str]| -> Option<String> {
        cols.iter()
            .find(|(k, _)| {
                needles.iter().all(|n| k.contains(n)) && !exclude.iter().any(|x| k.contains(x))
            })
            .map(|(_, c)| c.clone())
    };
    let c_sym = find(&["symbol"], &[])
        .or_else(|| find(&["tradingsymbol"], &[]))
        .or_else(|| find(&["instrument"], &[]));
    let c_side = find(&["trade", "type"], &[])
        .or_else(|| find(&["transaction"], &[]))
        .or_else(|| find(&["buy"], &[]));
    let c_qty = find(&["quantity"], &[]).or_else(|| find(&["qty"], &[]));
    let c_price = find(&["price"], &["average"]).or_else(|| find(&["price"], &[]));
    let c_date = find(&["trade", "date"], &[]).or_else(|| find(&["date"], &["time"]));
    let c_time = find(&["execution", "time"], &[])
        .or_else(|| find(&["order", "time"], &[]))
        .or_else(|| find(&["timestamp"], &[]));
    let c_exch = find(&["exchange"], &[]);
    let c_tid = find(&["trade", "id"], &[]);
    let c_chg = find(&["charge"], &[]).or_else(|| find(&["brokerage"], &[]));

    let missing: Vec<String> = [
        ("symbol", &c_sym),
        ("trade_type", &c_side),
        ("quantity", &c_qty),
        ("price", &c_price),
    ]
    .iter()
    .filter(|(_, c)| c.is_none())
    .map(|(n, _)| (*n).to_owned())
    .collect();
    if !missing.is_empty() {
        return Err(TradebookError(format!(
            "{shown}: missing required columns {}. Export console.zerodha.com -> Reports -> \
             Tradebook. Found: {}",
            py_list_repr(&missing),
            py_list_repr(&header)
        )));
    }
    let (Some(c_sym), Some(c_side), Some(c_qty), Some(c_price)) = (c_sym, c_side, c_qty, c_price)
    else {
        return Err(TradebookError(format!("{shown}: missing required columns")));
    };

    let mut fills = Vec::new();
    let mut line_no = 1;
    for rec in records {
        if rec.is_empty() {
            continue;
        }
        line_no += 1;
        if rec.len() > header.len() && rec[header.len()..].iter().any(|v| !v.trim().is_empty()) {
            return Err(TradebookError(format!(
                "{shown}: line {line_no} has more fields than the header — an unquoted comma \
                 inside a number would silently mis-read quantities."
            )));
        }
        // DictReader: a later duplicate header wins; a short row reads as missing.
        let get = |name: &str| -> Option<&str> {
            header
                .iter()
                .enumerate()
                .rev()
                .find(|(_, h)| h.as_str() == name)
                .and_then(|(i, _)| rec.get(i))
                .map(String::as_str)
        };
        let sym = get(&c_sym).unwrap_or("").trim().to_uppercase();
        if sym.is_empty() {
            continue;
        }
        let side_raw = get(&c_side).unwrap_or("").trim().to_lowercase();
        let side = if BUY_WORDS.contains(&side_raw.as_str()) {
            "BUY"
        } else if SELL_WORDS.contains(&side_raw.as_str()) {
            "SELL"
        } else {
            return Err(TradebookError(format!(
                "{shown}: line {line_no} has an unrecognised trade type {}; refusing to guess buy vs sell",
                baskfy_providers::py::repr_str(&side_raw)
            )));
        };
        let date_raw = c_date.as_deref().and_then(get).unwrap_or("");
        let time_raw = c_time.as_deref().and_then(get).unwrap_or("");
        let Some(when) = parse_when(date_raw, time_raw) else {
            return Err(TradebookError(format!(
                "{shown}: line {line_no} ({sym}) has no parseable date. An acquisition date \
                 cannot be inferred, and a lot without one is never assumed tax-safe."
            )));
        };
        #[allow(clippy::cast_possible_truncation)]
        let qty = num(get(&c_qty).unwrap_or("")).abs().trunc() as i64;
        if qty <= 0 {
            continue;
        }
        let exchange = match c_exch.as_deref().and_then(get) {
            Some(v) if !v.is_empty() => v.trim().to_uppercase(),
            _ => "NSE".to_owned(),
        };
        fills.push(Fill {
            symbol: sym,
            when,
            side: side.to_owned(),
            quantity: qty,
            price: num(get(&c_price).unwrap_or("")),
            exchange,
            trade_id: c_tid
                .as_deref()
                .and_then(get)
                .unwrap_or("")
                .trim()
                .to_owned(),
            charges: c_chg.as_deref().and_then(get).map_or(0.0, num),
        });
    }
    if fills.is_empty() {
        return Err(TradebookError(format!("{shown}: no usable trades found")));
    }
    fills.sort_by(|a, b| (&a.symbol, a.when).cmp(&(&b.symbol, b.when)));
    Ok(fills)
}

// =====================================================================================
// FIFO reconstruction
// =====================================================================================

enum Event<'a> {
    Fill(&'a Fill),
    Action(&'a Action),
}

/// `build_lots(fills, actions)`: buys open lots; sells consume them oldest-first; corporate
/// actions interleave by date (00:01 on the ex-date, ahead of that session's fills).
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn build_lots(fills: &[Fill], actions: &[Action]) -> Rebuild {
    let mut out = Rebuild::default();
    let mut books: Vec<(String, Vec<Lot>)> = Vec::new();
    let mut events: Vec<(NaiveDateTime, &str, u8, Event<'_>)> = fills
        .iter()
        .map(|f| (f.when, f.symbol.as_str(), 0, Event::Fill(f)))
        .collect();
    events.extend(
        actions
            .iter()
            .map(|a| (a.when(), a.symbol.as_str(), 1, Event::Action(a))),
    );
    events.sort_by(|a, b| (a.0, a.1, a.2).cmp(&(b.0, b.1, b.2)));

    fn book_of<'b>(books: &'b mut Vec<(String, Vec<Lot>)>, symbol: &str) -> &'b mut Vec<Lot> {
        let idx = match books.iter().position(|(s, _)| s == symbol) {
            Some(i) => i,
            None => {
                books.push((symbol.to_owned(), Vec::new()));
                books.len() - 1
            }
        };
        &mut books[idx].1
    }

    for (_, _, _, ev) in events {
        let f = match ev {
            Event::Action(a) => {
                apply_to_book(book_of(&mut books, &a.symbol), a);
                continue;
            }
            Event::Fill(f) => f,
        };
        let book = book_of(&mut books, &f.symbol);
        if f.side == "BUY" {
            book.push(Lot {
                symbol: f.symbol.clone(),
                when: f.when,
                quantity: f.quantity,
                price: f.price,
                charges: f.charges,
            });
            continue;
        }
        let mut remaining = f.quantity;
        while remaining > 0 && !book.is_empty() {
            let lot = &mut book[0];
            let take = remaining.min(lot.quantity);
            let share = if f.quantity == 0 {
                0.0
            } else {
                f.charges * take as f64 / f.quantity as f64
            };
            let cost_share = if lot.quantity == 0 {
                0.0
            } else {
                lot.charges * take as f64 / lot.quantity as f64
            };
            out.closed.push(Closed {
                symbol: f.symbol.clone(),
                entry_ts: local_ts(lot.when),
                exit_ts: local_ts(f.when),
                qty: take,
                entry_price: lot.price,
                exit_price: f.price,
                pnl: round_to((f.price - lot.price) * take as f64, 2),
                costs: round_to(share + cost_share, 2),
            });
            lot.quantity -= take;
            lot.charges -= cost_share;
            remaining -= take;
            if lot.quantity <= 0 {
                book.remove(0);
            }
        }
        if remaining > 0 {
            out.unmatched_sells.push(Unmatched {
                symbol: f.symbol.clone(),
                date: f.when.date().format("%Y-%m-%d").to_string(),
                quantity: remaining,
                price: f.price,
            });
        }
    }
    for (_, book) in books {
        out.open_lots
            .extend(book.into_iter().filter(|l| l.quantity > 0));
    }
    out.open_lots
        .sort_by(|a, b| (&a.symbol, a.when).cmp(&(&b.symbol, b.when)));
    out
}

// =====================================================================================
// fills — the durable source of truth
// =====================================================================================

/// `fill_key(f)`: the broker's trade id, else a deterministic stand-in
/// (`syn:` + the first 20 hex of SHA-1 over `symbol|when.isoformat()|side|quantity|price:.4f`).
#[must_use]
pub fn fill_key(f: &Fill) -> String {
    if !f.trade_id.is_empty() {
        return f.trade_id.clone();
    }
    let raw = format!(
        "{}|{}|{}|{}|{:.4}",
        f.symbol,
        iso_naive(f.when),
        f.side,
        f.quantity,
        f.price
    );
    let digest = sha1::hex(raw.as_bytes());
    format!("syn:{}", &digest[..20])
}

async fn count_fills(conn: &mut Conn) -> Result<i64, StorageError> {
    let cur = conn.execute("SELECT COUNT(*) c FROM fills", &[]).await?;
    Ok(as_i(cur.fetchone().and_then(|r| r.get("c"))))
}

/// What [`store_fills`] changed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stored {
    /// `seen`.
    pub seen: i64,
    /// `new`.
    pub new: i64,
    /// `symbols` (sorted, distinct).
    pub symbols: Vec<String>,
}

/// `store_fills(conn, fills, source=)`: insert, ignoring fills already recorded. `now` is
/// `datetime.now()` (naive local) for `captured_at`.
///
/// # Errors
/// The driver's.
pub async fn store_fills(
    conn: &mut Conn,
    fills: &[Fill],
    source: &str,
    now: NaiveDateTime,
) -> Result<Stored, StorageError> {
    let captured = now.format("%Y-%m-%dT%H:%M:%S").to_string();
    let rows: Vec<Vec<Param>> = fills
        .iter()
        .map(|f| {
            vec![
                Param::Text(fill_key(f)),
                Param::Text(f.symbol.clone()),
                Param::Float(f.ts()),
                Param::Text(f.side.clone()),
                Param::Int(f.quantity),
                Param::Float(f.price),
                Param::Text(f.exchange.clone()),
                Param::Float(f.charges),
                Param::Text(source.to_owned()),
                Param::Text(captured.clone()),
            ]
        })
        .collect();
    let before = count_fills(conn).await?;
    conn.transaction(|c| {
        Box::pin(async move {
            c.executemany(
                "INSERT INTO fills(trade_id, symbol, when_ts, side, quantity, price, exchange, \
                 charges, source, captured_at) VALUES(?,?,?,?,?,?,?,?,?,?) \
                 ON CONFLICT(trade_id) DO NOTHING",
                &rows,
            )
            .await
        })
    })
    .await?;
    let after = count_fills(conn).await?;
    let mut symbols: Vec<String> = fills.iter().map(|f| f.symbol.clone()).collect();
    symbols.sort();
    symbols.dedup();
    Ok(Stored {
        seen: i64::try_from(fills.len()).unwrap_or(i64::MAX),
        new: after - before,
        symbols,
    })
}

/// `load_fills(conn, symbols)`: every stored fill for these symbols (all when `None`), by time.
///
/// # Errors
/// The driver's.
pub async fn load_fills(
    conn: &mut Conn,
    symbols: Option<&[String]>,
) -> Result<Vec<Fill>, StorageError> {
    let mut sql = "SELECT * FROM fills".to_owned();
    let mut args = Vec::new();
    if let Some(syms) = symbols {
        if syms.is_empty() {
            return Ok(Vec::new());
        }
        sql.push_str(&format!(" WHERE symbol IN ({})", in_list(syms.len())));
        args = text_params(syms);
    }
    sql.push_str(" ORDER BY when_ts");
    let cur = conn.execute(&sql, &args).await?;
    let mut out = Vec::new();
    for r in &cur.rows {
        let ts = r.get("when_ts").and_then(DbValue::as_f64).unwrap_or(0.0);
        let exchange = as_text(r.get("exchange"));
        out.push(Fill {
            symbol: as_text(r.get("symbol")),
            when: local_from_ts(ts)
                .ok_or_else(|| StorageError::Other(format!("when_ts out of range: {ts}")))?,
            side: as_text(r.get("side")),
            quantity: as_i(r.get("quantity")),
            price: r.get("price").and_then(DbValue::as_f64).unwrap_or(0.0),
            exchange: if exchange.is_empty() {
                "NSE".to_owned()
            } else {
                exchange
            },
            trade_id: as_text(r.get("trade_id")),
            charges: r.get("charges").and_then(DbValue::as_f64).unwrap_or(0.0),
        });
    }
    Ok(out)
}

// =====================================================================================
// persistence
// =====================================================================================

/// What [`rebuild_symbols`] did.
#[derive(Debug, Clone, PartialEq)]
pub struct Rebuilt {
    /// `symbols`.
    pub symbols: Vec<String>,
    /// `fills`.
    pub fills: i64,
    /// `open_lots`.
    pub open_lots: i64,
    /// `closed_trades`.
    pub closed_trades: i64,
    /// `unmatched_sells`.
    pub unmatched_sells: Vec<Unmatched>,
    /// `realised_pnl`.
    pub realised_pnl: f64,
}

fn len_i(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn realised(closed: &[Closed]) -> f64 {
    round_to(closed.iter().fold(0.0, |acc, c| acc + c.pnl), 2)
}

/// `rebuild_symbols(conn, symbols)`: recompute FIFO lots for these symbols from every fill on
/// record (and their corporate actions).
///
/// # Errors
/// The driver's.
pub async fn rebuild_symbols(conn: &mut Conn, symbols: &[String]) -> Result<Rebuilt, StorageError> {
    let mut symbols = symbols.to_vec();
    symbols.sort();
    symbols.dedup();
    let fills = load_fills(conn, Some(&symbols)).await?;
    let actions = load_actions(conn, Some(&symbols)).await?;
    let rebuild = build_lots(&fills, &actions);
    write_lots(conn, &rebuild, &symbols, true).await?;
    Ok(Rebuilt {
        fills: len_i(fills.len()),
        open_lots: len_i(rebuild.open_lots.len()),
        closed_trades: len_i(rebuild.closed.len()),
        realised_pnl: realised(&rebuild.closed),
        unmatched_sells: rebuild.unmatched_sells,
        symbols,
    })
}

/// `import_tradebook(conn, path, replace=)`: parse, store the fills, rebuild lots. Re-importing the
/// same file is a no-op. Returns Python's result dict.
///
/// # Errors
/// The file's [`TradebookError`], or the driver's.
pub async fn import_tradebook(
    conn: &mut Conn,
    path: &Path,
    replace: bool,
    now: NaiveDateTime,
) -> Result<PyDict, ImportError> {
    let fills = parse_tradebook(path)?;
    let stored = store_fills(conn, &fills, "console_csv", now).await?;
    let (open_lots, closed_trades, unmatched, pnl) = if replace {
        let r = rebuild_symbols(conn, &stored.symbols).await?;
        (
            r.open_lots,
            r.closed_trades,
            r.unmatched_sells,
            r.realised_pnl,
        )
    } else {
        let rebuild = build_lots(&fills, &[]);
        write_lots(conn, &rebuild, &stored.symbols, false).await?;
        (
            len_i(rebuild.open_lots.len()),
            len_i(rebuild.closed.len()),
            rebuild.unmatched_sells.clone(),
            realised(&rebuild.closed),
        )
    };
    let first = fills.iter().map(|f| f.when).min().unwrap_or_default();
    let last = fills.iter().map(|f| f.when).max().unwrap_or_default();
    Ok(dict([
        ("file", PyValue::Str(path.display().to_string())),
        ("fills", PyValue::Int(len_i(fills.len()))),
        ("symbols", PyValue::Int(len_i(stored.symbols.len()))),
        ("new_fills", PyValue::Int(stored.new)),
        ("open_lots", PyValue::Int(open_lots)),
        ("closed_trades", PyValue::Int(closed_trades)),
        (
            "unmatched_sells",
            PyValue::List(unmatched.iter().map(Unmatched::to_py).collect()),
        ),
        ("realised_pnl", PyValue::Float(pnl)),
        (
            "first_trade",
            PyValue::Str(first.date().format("%Y-%m-%d").to_string()),
        ),
        (
            "last_trade",
            PyValue::Str(last.date().format("%Y-%m-%d").to_string()),
        ),
    ]))
}

fn opt_bits(v: Option<f64>) -> Option<u64> {
    v.map(f64::to_bits)
}

/// `_write_lots`: replace the stored lots for these symbols, carrying `entry_score` /
/// `exit_reason` across by `(symbol, entry_ts, qty)`.
async fn write_lots(
    conn: &mut Conn,
    rebuild: &Rebuild,
    symbols: &[String],
    replace: bool,
) -> Result<(), StorageError> {
    type Key = (String, Option<u64>, Option<i64>);
    let mut kept: HashMap<Key, (Param, Param)> = HashMap::new();
    if replace {
        let sql = format!(
            "SELECT symbol, entry_ts, qty, entry_score, exit_reason FROM trades WHERE symbol IN ({})",
            in_list(symbols.len())
        );
        let cur = conn.execute(&sql, &text_params(symbols)).await?;
        for r in &cur.rows {
            let score = r.get("entry_score").cloned().unwrap_or(DbValue::Null);
            let reason = r.get("exit_reason").cloned().unwrap_or(DbValue::Null);
            if score.is_null() && reason.is_null() {
                continue;
            }
            let key = (
                as_text(r.get("symbol")),
                opt_bits(r.get("entry_ts").and_then(DbValue::as_f64)),
                r.get("qty").and_then(DbValue::as_i64),
            );
            let to_param = |v: &DbValue| match v {
                DbValue::Float(f) => Param::Float(*f),
                DbValue::Int(i) => Param::Int(*i),
                DbValue::Text(s) => Param::Text(s.clone()),
                _ => Param::Null,
            };
            kept.insert(key, (to_param(&score), to_param(&reason)));
        }
    }
    let none = (Param::Null, Param::Null);
    let mut rows: Vec<Vec<Param>> = Vec::new();
    for c in &rebuild.closed {
        let ann = kept
            .get(&(c.symbol.clone(), Some(c.entry_ts.to_bits()), Some(c.qty)))
            .unwrap_or(&none);
        rows.push(vec![
            Param::Text(c.symbol.clone()),
            Param::Float(c.entry_ts),
            Param::Float(c.exit_ts),
            Param::Int(c.qty),
            Param::Float(c.entry_price),
            Param::Float(c.exit_price),
            ann.0.clone(),
            ann.1.clone(),
            Param::Float(c.pnl),
            Param::Float(c.costs),
        ]);
    }
    for l in &rebuild.open_lots {
        let ts = local_ts(l.when);
        let ann = kept
            .get(&(l.symbol.clone(), Some(ts.to_bits()), Some(l.quantity)))
            .unwrap_or(&none);
        rows.push(vec![
            Param::Text(l.symbol.clone()),
            Param::Float(ts),
            Param::Null,
            Param::Int(l.quantity),
            Param::Float(l.price),
            Param::Null,
            ann.0.clone(),
            ann.1.clone(),
            Param::Null,
            Param::Float(round_to(l.charges, 2)),
        ]);
    }
    let symbols = symbols.to_vec();
    conn.transaction(|c| {
        Box::pin(async move {
            if replace && !symbols.is_empty() {
                let sql = format!(
                    "DELETE FROM trades WHERE symbol IN ({})",
                    in_list(symbols.len())
                );
                c.execute(&sql, &text_params(&symbols)).await?;
            }
            c.executemany(
                "INSERT INTO trades(symbol, entry_ts, exit_ts, qty, entry_price, exit_price, \
                 entry_score, exit_reason, pnl, costs) VALUES(?,?,?,?,?,?,?,?,?,?)",
                &rows,
            )
            .await?;
            Ok(())
        })
    })
    .await
}

// =====================================================================================
// live capture
// =====================================================================================

/// `fills_from_kite(rows)`: Kite trade dicts onto fills — CNC only; an unknown side or a trade
/// without a usable timestamp is refused, a zero quantity skipped.
///
/// # Errors
/// [`TradebookError`] with Python's messages.
pub fn fills_from_kite(rows: &[PyValue]) -> Result<Vec<Fill>, TradebookError> {
    let empty = PyDict::new();
    let mut out = Vec::new();
    for row in rows {
        let r = match row {
            PyValue::Map(m) => m,
            _ => &empty,
        };
        let tt = r
            .get("transaction_type")
            .cloned()
            .unwrap_or(PyValue::Str(String::new()));
        let side = tt.py_str().trim().to_uppercase();
        if side != "BUY" && side != "SELL" {
            return Err(TradebookError(format!(
                "unrecognised transaction_type {}",
                r.get("transaction_type")
                    .unwrap_or(&PyValue::None)
                    .py_repr()
            )));
        }
        let product = r
            .get("product")
            .cloned()
            .unwrap_or(PyValue::Str("CNC".into()));
        if product.py_str().to_uppercase() != "CNC" {
            continue;
        }
        let qty = pyi(r.get("quantity"));
        if qty <= 0 {
            continue;
        }
        let none = PyValue::None;
        let when_v = r
            .get("fill_timestamp")
            .unwrap_or(&none)
            .or(r.get("exchange_timestamp").unwrap_or(&none))
            .or(r.get("order_timestamp").unwrap_or(&none));
        let when = match when_v {
            PyValue::Str(s) => parse_when("", s),
            PyValue::DateTime(baskfy_providers::records::PyDateTime::Naive(n)) => Some(*n),
            PyValue::DateTime(baskfy_providers::records::PyDateTime::Aware(a)) => {
                Some(a.naive_local())
            }
            _ => None,
        };
        let Some(when) = when else {
            return Err(TradebookError(format!(
                "trade {} for {} has no usable timestamp; it would land in the wrong FIFO position",
                r.get("trade_id").unwrap_or(&none).py_str(),
                r.get("tradingsymbol").unwrap_or(&none).py_str()
            )));
        };
        let symbol = r
            .get("tradingsymbol")
            .ok_or_else(|| TradebookError("KeyError: 'tradingsymbol'".into()))?
            .py_str();
        let exchange = r
            .get("exchange")
            .filter(|v| v.truthy())
            .map_or_else(|| "NSE".to_owned(), PyValue::py_str);
        out.push(Fill {
            symbol,
            when,
            side,
            quantity: qty,
            price: pyf(r.get("average_price")),
            exchange,
            trade_id: r
                .get("trade_id")
                .filter(|v| v.truthy())
                .map_or_else(String::new, PyValue::py_str),
            charges: 0.0,
        });
    }
    Ok(out)
}

/// `capture_live_trades(conn, kite)`: record today's fills (`raw` is `kite.trades()`) and rebuild
/// the lots they touch. Idempotent.
///
/// # Errors
/// A refused trade row, or the driver's.
pub async fn capture_live_trades(
    conn: &mut Conn,
    raw: &[PyValue],
    now: NaiveDateTime,
) -> Result<PyDict, ImportError> {
    let fills = fills_from_kite(raw)?;
    if fills.is_empty() {
        return Ok(dict([
            ("raw", PyValue::Int(len_i(raw.len()))),
            ("captured", PyValue::Int(0)),
            ("new", PyValue::Int(0)),
            ("symbols", PyValue::List(Vec::new())),
            ("note", PyValue::Str("no fills today".into())),
        ]));
    }
    let stored = store_fills(conn, &fills, "kite_api", now).await?;
    let rebuilt = if stored.new != 0 {
        Some(rebuild_symbols(conn, &stored.symbols).await?)
    } else {
        None
    };
    Ok(dict([
        ("raw", PyValue::Int(len_i(raw.len()))),
        ("captured", PyValue::Int(len_i(fills.len()))),
        ("new", PyValue::Int(stored.new)),
        (
            "symbols",
            PyValue::List(
                stored
                    .symbols
                    .iter()
                    .map(|s| PyValue::Str(s.clone()))
                    .collect(),
            ),
        ),
        (
            "open_lots",
            rebuilt
                .as_ref()
                .map_or(PyValue::None, |r| PyValue::Int(r.open_lots)),
        ),
        (
            "closed_trades",
            rebuilt
                .as_ref()
                .map_or(PyValue::None, |r| PyValue::Int(r.closed_trades)),
        ),
        (
            "realised_pnl",
            rebuilt
                .as_ref()
                .map_or(PyValue::None, |r| PyValue::Float(r.realised_pnl)),
        ),
    ]))
}

fn sym_of(h: &PyDict) -> String {
    h.get("symbol").map_or_else(String::new, PyValue::py_str)
}

/// `reconcile_with_holdings(conn, holdings)`: reconstructed open quantity against the real
/// holding, per symbol — a corporate action or a short export surfaces here, never silently.
///
/// # Errors
/// The driver's.
pub async fn reconcile_with_holdings(
    conn: &mut Conn,
    holdings: &[PyDict],
) -> Result<Vec<PyDict>, StorageError> {
    let cur = conn
        .execute(
            "SELECT symbol, SUM(qty) qty FROM trades WHERE exit_ts IS NULL GROUP BY symbol",
            &[],
        )
        .await?;
    let recon: HashMap<String, i64> = cur
        .rows
        .iter()
        .map(|r| (as_text(r.get("symbol")), as_i(r.get("qty"))))
        .collect();
    let mut out = Vec::new();
    for h in holdings {
        let sym = sym_of(h);
        let actual = pyi(h.get("quantity"));
        let lots = recon.get(&sym).copied().unwrap_or(0);
        if actual == lots {
            continue;
        }
        let cause = if lots != 0 && actual > lots {
            "corporate action or export starts mid-position"
        } else if lots == 0 {
            "no tradebook history for this symbol"
        } else {
            "lots exceed the holding — check for a missing sell"
        };
        out.push(dict([
            ("symbol", PyValue::Str(sym)),
            ("holding_qty", PyValue::Int(actual)),
            ("lot_qty", PyValue::Int(lots)),
            ("difference", PyValue::Int(actual - lots)),
            ("likely_cause", PyValue::Str(cause.to_owned())),
        ]));
    }
    Ok(out)
}

/// `coverage(conn, holdings)`: how much of the book the tax module can reason about.
///
/// # Errors
/// The driver's.
#[allow(clippy::cast_precision_loss)]
pub async fn coverage(conn: &mut Conn, holdings: &[PyDict]) -> Result<PyDict, StorageError> {
    let cur = conn
        .execute(
            "SELECT DISTINCT symbol FROM trades WHERE exit_ts IS NULL",
            &[],
        )
        .await?;
    let recon: std::collections::HashSet<String> =
        cur.rows.iter().map(|r| as_text(r.get("symbol"))).collect();
    let held: Vec<String> = holdings.iter().map(sym_of).collect();
    let covered = held.iter().filter(|s| recon.contains(*s)).count();
    let mut missing: Vec<String> = held
        .iter()
        .filter(|s| !recon.contains(*s))
        .cloned()
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect();
    missing.sort();
    Ok(dict([
        ("holdings", PyValue::Int(len_i(held.len()))),
        ("with_lots", PyValue::Int(len_i(covered))),
        (
            "coverage_pct",
            PyValue::Float(if held.is_empty() {
                0.0
            } else {
                round_to(covered as f64 / held.len() as f64 * 100.0, 1)
            }),
        ),
        (
            "missing",
            PyValue::List(missing.into_iter().map(PyValue::Str).collect()),
        ),
    ]))
}

fn date_of_ts(ts: f64) -> Result<String, StorageError> {
    local_from_ts(ts)
        .map(|d| d.date().format("%Y-%m-%d").to_string())
        .ok_or_else(|| StorageError::Other(format!("timestamp out of range: {ts}")))
}

/// `fills_status(conn)`: where the stored fills came from, and whether today's session was
/// captured. `today` is `date.today()`.
///
/// # Errors
/// The driver's.
pub async fn fills_status(conn: &mut Conn, today: NaiveDate) -> Result<PyDict, StorageError> {
    let cur = conn
        .execute(
            "SELECT source, COUNT(*) n, MIN(when_ts) first_ts, MAX(when_ts) last_ts,\
                    MAX(captured_at) last_run FROM fills GROUP BY source",
            &[],
        )
        .await?;
    let by_source: BTreeMap<String, &crate::storage::Row> = cur
        .rows
        .iter()
        .map(|r| (as_text(r.get("source")), r))
        .collect();
    let total: i64 = by_source.values().map(|r| as_i(r.get("n"))).sum();
    let last_ts = by_source
        .values()
        .filter_map(|r| r.get("last_ts").and_then(DbValue::as_f64))
        .fold(None::<f64>, |m, x| Some(m.map_or(x, |m| m.max(x))));
    let api = by_source.get("kite_api");
    let today_iso = today.format("%Y-%m-%d").to_string();
    let captured_today = api.is_some_and(|a| {
        let run = as_text(a.get("last_run"));
        !run.is_empty() && run.get(..10).unwrap_or(&run) == today_iso
    });
    let mut sources = Vec::new();
    for (k, v) in &by_source {
        let label = match k.as_str() {
            "console_csv" => "Console export",
            "kite_api" => "Daily capture",
            other => other,
        };
        let last_run = v.get("last_run").map_or(PyValue::None, super::db_to_py);
        sources.push(PyValue::Map(dict([
            ("source", PyValue::Str(k.clone())),
            ("label", PyValue::Str(label.to_owned())),
            ("count", PyValue::Int(as_i(v.get("n")))),
            (
                "first",
                PyValue::Str(date_of_ts(
                    v.get("first_ts").and_then(DbValue::as_f64).unwrap_or(0.0),
                )?),
            ),
            (
                "last",
                PyValue::Str(date_of_ts(
                    v.get("last_ts").and_then(DbValue::as_f64).unwrap_or(0.0),
                )?),
            ),
            ("last_run", last_run),
        ])));
    }
    Ok(dict([
        ("total", PyValue::Int(total)),
        ("sources", PyValue::List(sources)),
        (
            "latest_fill",
            match last_ts {
                Some(ts) if ts != 0.0 => PyValue::Str(date_of_ts(ts)?),
                _ => PyValue::None,
            },
        ),
        ("captured_today", PyValue::Bool(captured_today)),
        ("capture_ever_run", PyValue::Bool(api.is_some())),
    ]))
}

/// `status(conn, positions)`: everything the tradebook page shows, from stored rows only.
/// `positions` are the latest snapshot's (never a live Kite call).
///
/// # Errors
/// The driver's.
#[allow(clippy::too_many_lines)]
pub async fn status(
    conn: &mut Conn,
    positions: &[PyDict],
    today: NaiveDate,
) -> Result<PyDict, StorageError> {
    let open = conn
        .execute(
            "SELECT symbol, COUNT(*) lots, SUM(qty) qty, MIN(entry_ts) first_ts, \
                    MAX(entry_ts) last_ts, SUM(entry_price * qty) cost \
             FROM trades WHERE exit_ts IS NULL GROUP BY symbol",
            &[],
        )
        .await?;
    let open_by_symbol: HashMap<String, &crate::storage::Row> = open
        .rows
        .iter()
        .map(|r| (as_text(r.get("symbol")), r))
        .collect();
    let closed_cur = conn
        .execute(
            "SELECT COUNT(*) n, SUM(pnl) pnl, MIN(exit_ts) first_ts, MAX(exit_ts) last_ts \
             FROM trades WHERE exit_ts IS NOT NULL",
            &[],
        )
        .await?;
    let closed = closed_cur.fetchone().cloned().unwrap_or_default();
    let total_cur = conn.execute("SELECT COUNT(*) n FROM trades", &[]).await?;
    let total = as_i(total_cur.fetchone().and_then(|r| r.get("n")));

    let cov = coverage(conn, positions).await?;
    let problems = reconcile_with_holdings(conn, positions).await?;

    let mut ordered: Vec<&PyDict> = positions.iter().collect();
    ordered.sort_by(|a, b| {
        pyf(b.get("value"))
            .partial_cmp(&pyf(a.get("value")))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut detail = Vec::new();
    for p in ordered {
        let sym = sym_of(p);
        let o = open_by_symbol.get(&sym);
        let mismatch = problems
            .iter()
            .find(|m| m.get("symbol") == Some(&PyValue::Str(sym.clone())));
        let qty = o.map(|r| as_i(r.get("qty")));
        let cost = o.and_then(|r| r.get("cost").and_then(DbValue::as_f64));
        let first_ts = o.and_then(|r| r.get("first_ts").and_then(DbValue::as_f64));
        #[allow(clippy::cast_precision_loss)]
        let avg_cost = match (qty, cost) {
            (Some(q), Some(c)) if q != 0 => PyValue::Float(round_to(c / q as f64, 2)),
            _ => PyValue::None,
        };
        let status_ = match (o.is_some(), mismatch.is_some()) {
            (true, false) => "ok",
            (true, true) => "mismatch",
            _ => "missing",
        };
        detail.push(PyValue::Map(dict([
            ("symbol", PyValue::Str(sym.clone())),
            ("holding_qty", PyValue::Int(pyi(p.get("quantity")))),
            ("lot_qty", PyValue::Int(qty.unwrap_or(0))),
            ("lots", PyValue::Int(o.map_or(0, |r| as_i(r.get("lots"))))),
            (
                "first_acquired",
                match first_ts {
                    Some(ts) if ts != 0.0 => PyValue::Str(date_of_ts(ts)?),
                    _ => PyValue::None,
                },
            ),
            ("avg_cost", avg_cost),
            (
                "broker_avg",
                PyValue::Float(round_to(pyf(p.get("average_price")), 2)),
            ),
            ("status", PyValue::Str(status_.to_owned())),
            (
                "note",
                mismatch
                    .and_then(|m| m.get("likely_cause").cloned())
                    .unwrap_or(PyValue::Str(String::new())),
            ),
        ])));
    }
    let n_closed = as_i(closed.get("n"));
    let history_from = open
        .rows
        .iter()
        .filter_map(|r| r.get("first_ts").and_then(DbValue::as_f64))
        .fold(None::<f64>, |m, x| Some(m.map_or(x, |m| m.min(x))));
    Ok(dict([
        ("has_lots", PyValue::Bool(total > 0)),
        ("total_rows", PyValue::Int(total)),
        (
            "open_lots",
            PyValue::Int(open.rows.iter().map(|r| as_i(r.get("lots"))).sum()),
        ),
        ("closed_trades", PyValue::Int(n_closed)),
        (
            "realised_pnl",
            if n_closed != 0 {
                PyValue::Float(round_to(
                    closed.get("pnl").and_then(DbValue::as_f64).unwrap_or(0.0),
                    2,
                ))
            } else {
                PyValue::None
            },
        ),
        (
            "history_from",
            match history_from {
                Some(ts) if !open.rows.is_empty() => PyValue::Str(date_of_ts(ts)?),
                _ => PyValue::None,
            },
        ),
        ("coverage", PyValue::Map(cov)),
        (
            "problems",
            PyValue::List(problems.into_iter().map(PyValue::Map).collect()),
        ),
        ("detail", PyValue::List(detail)),
        ("fills", PyValue::Map(fills_status(conn, today).await?)),
    ]))
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
mod tests {
    //! `tests/test_tradebook.py`, ported (the parse, FIFO, persistence, reconciliation, capture
    //! and provenance cases; the page-rendering case belongs to the lane that serves `/tradebook`).
    use super::super::tax_lots as tl;
    use super::*;
    use crate::storage::Db;

    const HEADER: &str = "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,\
                          quantity,price,trade_id,order_id,order_execution_time";

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    fn dt(y: i32, m: u32, day: u32) -> NaiveDateTime {
        d(y, m, day).and_hms_opt(0, 0, 0).unwrap()
    }

    fn now() -> NaiveDateTime {
        d(2026, 8, 15).and_hms_opt(18, 0, 0).unwrap()
    }

    fn row(
        symbol: &str,
        date: &str,
        side: &str,
        qty: i64,
        price: f64,
        time: Option<&str>,
        tid: &str,
    ) -> String {
        let ts = match time {
            Some(t) if !t.is_empty() => t.to_owned(),
            _ => format!("{date}T09:30:00"),
        };
        let price = baskfy_providers::py::repr_float(price);
        format!(
            "{symbol},INE000A01001,{date},NSE,EQ,EQ,{side},false,{qty},{price},{tid},O{tid},{ts}"
        )
    }

    fn r(symbol: &str, date: &str, side: &str, qty: i64, price: f64) -> String {
        row(symbol, date, side, qty, price, None, "T1")
    }

    fn write(
        dir: &tempfile::TempDir,
        rows: &[String],
        header: &str,
        name: &str,
    ) -> std::path::PathBuf {
        let p = dir.path().join(name);
        let mut text = vec![header.to_owned()];
        text.extend(rows.iter().cloned());
        std::fs::write(&p, text.join("\n") + "\n").unwrap();
        p
    }

    fn tb(dir: &tempfile::TempDir, rows: &[String]) -> std::path::PathBuf {
        write(dir, rows, HEADER, "tradebook.csv")
    }

    async fn conn(dir: &tempfile::TempDir) -> Conn {
        let mut c = Db::connect_sqlite_path(&dir.path().join("p.db"))
            .await
            .unwrap();
        c.migrate().await.unwrap();
        c
    }

    async fn import(c: &mut Conn, p: &Path) -> PyDict {
        import_tradebook(c, p, true, now()).await.unwrap()
    }

    fn int(d: &PyDict, k: &str) -> i64 {
        pyi(d.get(k))
    }

    async fn scalar(c: &mut Conn, sql: &str) -> DbValue {
        c.execute(sql, &[]).await.unwrap().rows[0]
            .at(0)
            .cloned()
            .unwrap()
    }

    // --- parsing --------------------------------------------------------------------------

    #[test]
    fn parses_the_console_export_shape() {
        let dir = tempfile::tempdir().unwrap();
        let f = tb(
            &dir,
            &[
                r("RRKABEL", "2025-07-01", "buy", 100, 1900.0),
                row("RRKABEL", "2026-01-15", "sell", 40, 2500.0, None, "T2"),
            ],
        );
        let fills = parse_tradebook(&f).unwrap();
        assert_eq!(
            fills.iter().map(|x| x.side.as_str()).collect::<Vec<_>>(),
            ["BUY", "SELL"]
        );
        assert_eq!(
            (fills[0].symbol.as_str(), fills[0].quantity),
            ("RRKABEL", 100)
        );
        assert_eq!(fills[0].when.date(), d(2025, 7, 1));
    }

    #[test]
    fn buy_sell_notation_variants() {
        for (word, want) in [
            ("buy", "BUY"),
            ("BUY", "BUY"),
            ("B", "BUY"),
            ("bought", "BUY"),
            ("sell", "SELL"),
            ("SELL", "SELL"),
            ("S", "SELL"),
            ("sold", "SELL"),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let f = tb(&dir, &[r("X", "2025-07-01", word, 10, 100.0)]);
            assert_eq!(parse_tradebook(&f).unwrap()[0].side, want, "{word}");
        }
    }

    #[test]
    fn unrecognised_trade_type_is_refused_not_guessed() {
        let dir = tempfile::tempdir().unwrap();
        let f = tb(&dir, &[r("X", "2025-07-01", "transfer", 10, 100.0)]);
        assert!(
            parse_tradebook(&f)
                .unwrap_err()
                .0
                .contains("refusing to guess")
        );
    }

    #[test]
    fn a_row_without_a_parseable_date_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let f = tb(
            &dir,
            &[row("X", "not-a-date", "buy", 10, 100.0, Some(""), "T1")],
        );
        assert!(
            parse_tradebook(&f)
                .unwrap_err()
                .0
                .contains("acquisition date")
        );
    }

    #[test]
    fn date_formats() {
        for s in [
            "2025-07-01",
            "01-07-2025",
            "01/07/2025",
            "01-Jul-2025",
            "01 Jul 2025",
        ] {
            let dir = tempfile::tempdir().unwrap();
            let f = tb(&dir, &[row("X", s, "buy", 10, 100.0, Some(""), "T1")]);
            assert_eq!(
                parse_tradebook(&f).unwrap()[0].when.date(),
                d(2025, 7, 1),
                "{s}"
            );
        }
    }

    #[test]
    fn execution_time_is_preferred_over_the_date() {
        let dir = tempfile::tempdir().unwrap();
        let f = tb(
            &dir,
            &[row(
                "X",
                "2025-07-01",
                "buy",
                10,
                100.0,
                Some("2025-07-01T14:22:05"),
                "T1",
            )],
        );
        assert_eq!(
            chrono::Timelike::hour(&parse_tradebook(&f).unwrap()[0].when),
            14
        );
    }

    #[test]
    fn thousands_separators_are_handled() {
        let dir = tempfile::tempdir().unwrap();
        let f = tb(
            &dir,
            &["X,INE1,2025-07-01,NSE,EQ,EQ,buy,false,\"1,500\",\"2,150.75\",T1,O1,".to_owned()],
        );
        let fills = parse_tradebook(&f).unwrap();
        assert_eq!(fills[0].quantity, 1500);
        assert_eq!(fills[0].price, 2150.75);
    }

    #[test]
    fn unquoted_comma_is_refused_rather_than_misread() {
        let dir = tempfile::tempdir().unwrap();
        let f = tb(
            &dir,
            &[
                "X,INE1,2025-07-01,NSE,EQ,EQ,buy,false,1,500,2150.75,T1,O1,2025-07-01T09:30:00"
                    .to_owned(),
            ],
        );
        assert!(
            parse_tradebook(&f)
                .unwrap_err()
                .0
                .contains("more fields than the header")
        );
    }

    #[test]
    fn missing_required_columns_names_them() {
        let dir = tempfile::tempdir().unwrap();
        let f = write(
            &dir,
            &["X,2025-07-01".to_owned()],
            "symbol,trade_date",
            "t.csv",
        );
        let e = parse_tradebook(&f).unwrap_err().0;
        assert!(
            e.contains("missing required columns ['trade_type', 'quantity', 'price']"),
            "{e}"
        );
    }

    #[test]
    fn empty_file_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        assert!(parse_tradebook(&tb(&dir, &[])).is_err());
    }

    // --- FIFO -----------------------------------------------------------------------------

    #[test]
    fn partial_sell_consumes_the_oldest_lot_first() {
        let fills = [
            Fill::new("X", dt(2025, 1, 1), "BUY", 100, 50.0),
            Fill::new("X", dt(2025, 6, 1), "BUY", 100, 80.0),
            Fill::new("X", dt(2026, 1, 1), "SELL", 150, 100.0),
        ];
        let r = build_lots(&fills, &[]);
        assert_eq!(
            r.closed.iter().map(|c| c.qty).collect::<Vec<_>>(),
            [100, 50]
        );
        assert_eq!(
            r.closed.iter().map(|c| c.entry_price).collect::<Vec<_>>(),
            [50.0, 80.0]
        );
        assert_eq!(r.open_lots.len(), 1);
        assert_eq!((r.open_lots[0].quantity, r.open_lots[0].price), (50, 80.0));
    }

    #[test]
    fn realised_pnl_uses_the_consumed_lots() {
        let r = build_lots(
            &[
                Fill::new("X", dt(2025, 1, 1), "BUY", 100, 50.0),
                Fill::new("X", dt(2026, 1, 1), "SELL", 100, 90.0),
            ],
            &[],
        );
        assert!((r.closed[0].pnl - 4000.0).abs() < 1e-9);
        assert!(r.open_lots.is_empty());
    }

    #[test]
    fn a_pure_buy_history_leaves_only_open_lots() {
        let r = build_lots(
            &[
                Fill::new("X", dt(2025, 1, 1), "BUY", 10, 50.0),
                Fill::new("X", dt(2025, 3, 1), "BUY", 20, 60.0),
            ],
            &[],
        );
        assert!(r.closed.is_empty());
        assert_eq!(r.open_lots.iter().map(|l| l.quantity).sum::<i64>(), 30);
    }

    #[test]
    fn sell_without_inventory_is_recorded_not_dropped() {
        let r = build_lots(&[Fill::new("X", dt(2025, 1, 1), "SELL", 50, 90.0)], &[]);
        assert_eq!(r.unmatched_sells[0].quantity, 50);
        assert!(r.closed.is_empty());
    }

    #[test]
    fn symbols_are_kept_independent() {
        let r = build_lots(
            &[
                Fill::new("A", dt(2025, 1, 1), "BUY", 10, 50.0),
                Fill::new("B", dt(2025, 2, 1), "BUY", 20, 60.0),
                Fill::new("A", dt(2025, 3, 1), "SELL", 10, 70.0),
            ],
            &[],
        );
        assert_eq!(
            r.closed
                .iter()
                .map(|c| c.symbol.as_str())
                .collect::<Vec<_>>(),
            ["A"]
        );
        assert_eq!(
            r.open_lots
                .iter()
                .map(|l| l.symbol.as_str())
                .collect::<Vec<_>>(),
            ["B"]
        );
    }

    #[test]
    fn charges_are_apportioned_across_partial_fills() {
        let mut buy = Fill::new("X", dt(2025, 1, 1), "BUY", 100, 50.0);
        buy.charges = 100.0;
        let mut sell = Fill::new("X", dt(2026, 1, 1), "SELL", 50, 90.0);
        sell.charges = 60.0;
        let r = build_lots(&[buy, sell], &[]);
        assert!((r.closed[0].costs - 110.0).abs() < 1e-9);
        assert!((r.open_lots[0].charges - 50.0).abs() < 1e-9);
    }

    #[test]
    fn a_bonus_adds_a_nil_cost_lot_and_a_split_rewrites() {
        let fills = [Fill::new("X", dt(2025, 1, 1), "BUY", 100, 50.0)];
        let bonus = Action {
            symbol: "X".into(),
            kind: "bonus".into(),
            ex_date: d(2025, 6, 1),
            ratio_new: 1.0,
            ratio_old: 1.0,
            note: String::new(),
        };
        let r = build_lots(&fills, &[bonus]);
        assert_eq!(
            r.open_lots
                .iter()
                .map(|l| (l.quantity, l.price))
                .collect::<Vec<_>>(),
            [(100, 50.0), (100, 0.0)]
        );
        let split = Action {
            symbol: "X".into(),
            kind: "split".into(),
            ex_date: d(2025, 6, 1),
            ratio_new: 5.0,
            ratio_old: 1.0,
            note: String::new(),
        };
        let r = build_lots(&fills, &[split]);
        assert_eq!((r.open_lots[0].quantity, r.open_lots[0].price), (500, 10.0));
    }

    #[test]
    fn a_synthetic_key_matches_pythons_sha1_scheme() {
        // hashlib.sha1(b"X|2025-01-01T09:15:00|BUY|100|50.0000").hexdigest()[:20]
        let f = Fill::new(
            "X",
            d(2025, 1, 1).and_hms_opt(9, 15, 0).unwrap(),
            "BUY",
            100,
            50.0,
        );
        let want = format!(
            "syn:{}",
            &sha1::hex(b"X|2025-01-01T09:15:00|BUY|100|50.0000")[..20]
        );
        assert_eq!(fill_key(&f), want);
    }

    // --- persistence + idempotency -------------------------------------------------------

    #[tokio::test]
    async fn import_writes_lots_the_tax_module_can_read() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        let res = import(
            &mut c,
            &tb(&dir, &[r("RRKABEL", "2025-09-19", "buy", 172, 2121.25)]),
        )
        .await;
        assert_eq!((int(&res, "open_lots"), int(&res, "closed_trades")), (1, 0));
        let lots = tl::open_lots(&mut c, "RRKABEL").await.unwrap();
        assert_eq!(lots.len(), 1);
        assert_eq!(lots[0].quantity, 172);
        assert!((lots[0].price.unwrap() - 2121.25).abs() < 1e-9);
        assert_eq!(lots[0].acquired_on, Some(d(2025, 9, 19)));
        assert!(lots[0].complete());
    }

    #[tokio::test]
    async fn imported_lots_produce_a_real_tax_review() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        import(&mut c, &tb(&dir, &[r("X", "2025-09-19", "buy", 100, 50.0)])).await;
        let lots = tl::open_lots(&mut c, "X").await.unwrap();
        let rev = tl::review_sale(
            "X",
            100,
            100.0,
            &lots,
            d(2026, 8, 14),
            tl::DEFAULT_REVIEW_DAYS,
        );
        assert!(!rev.data_unknown);
        assert_eq!(rev.estimated_gain, Some(5000.0));
        assert!(rev.codes.contains(&tl::TAX_LOT_NEAR_LTCG) && rev.flagged);
    }

    #[tokio::test]
    async fn an_older_lot_is_recognised_as_long_term() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        import(&mut c, &tb(&dir, &[r("X", "2024-01-05", "buy", 100, 50.0)])).await;
        let lots = tl::open_lots(&mut c, "X").await.unwrap();
        let rev = tl::review_sale(
            "X",
            100,
            100.0,
            &lots,
            d(2026, 8, 14),
            tl::DEFAULT_REVIEW_DAYS,
        );
        assert!(rev.codes.contains(&tl::TAX_LOT_LONG_TERM) && !rev.flagged);
    }

    async fn trades(c: &mut Conn) -> Vec<(String, i64, Option<f64>)> {
        c.execute("SELECT * FROM trades ORDER BY id", &[])
            .await
            .unwrap()
            .rows
            .iter()
            .map(|r| {
                (
                    as_text(r.get("symbol")),
                    as_i(r.get("qty")),
                    r.get("entry_ts").and_then(DbValue::as_f64),
                )
            })
            .collect()
    }

    #[tokio::test]
    async fn reimporting_the_same_file_changes_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        let f = tb(
            &dir,
            &[
                r("X", "2025-01-01", "buy", 100, 50.0),
                row("X", "2026-01-01", "sell", 40, 90.0, None, "T2"),
            ],
        );
        let first = import(&mut c, &f).await;
        let before = trades(&mut c).await;
        let second = import(&mut c, &f).await;
        let after = trades(&mut c).await;
        assert_eq!(first.get("open_lots"), second.get("open_lots"));
        assert_eq!(before, after);
    }

    #[tokio::test]
    async fn strategy_annotations_survive_a_rebuild() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        let f = tb(&dir, &[r("X", "2025-01-01", "buy", 100, 50.0)]);
        import(&mut c, &f).await;
        c.execute(
            "UPDATE trades SET entry_score=84.3, exit_reason='rank_drop' WHERE symbol='X'",
            &[],
        )
        .await
        .unwrap();
        import(&mut c, &f).await;
        let cur = c
            .execute(
                "SELECT entry_score, exit_reason FROM trades WHERE symbol='X'",
                &[],
            )
            .await
            .unwrap();
        let row = cur.fetchone().unwrap();
        assert_eq!(row.get("entry_score"), Some(&DbValue::Float(84.3)));
        assert_eq!(
            row.get("exit_reason"),
            Some(&DbValue::Text("rank_drop".into()))
        );
    }

    #[tokio::test]
    async fn import_only_rebuilds_the_symbols_in_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        c.execute("INSERT INTO trades(symbol, qty, entry_ts, entry_price) VALUES('OTHER', 10, 1000.0, 5.0)", &[]).await.unwrap();
        import(&mut c, &tb(&dir, &[r("X", "2025-01-01", "buy", 100, 50.0)])).await;
        assert_eq!(
            scalar(&mut c, "SELECT COUNT(*) c FROM trades WHERE symbol='OTHER'").await,
            DbValue::Int(1)
        );
    }

    // --- reconciliation ------------------------------------------------------------------

    fn holding(sym: &str, qty: i64) -> PyDict {
        dict([
            ("symbol", PyValue::Str(sym.into())),
            ("quantity", PyValue::Int(qty)),
        ])
    }

    #[tokio::test]
    async fn quantity_mismatch_is_reported() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        import(&mut c, &tb(&dir, &[r("X", "2025-01-01", "buy", 100, 50.0)])).await;
        let p = reconcile_with_holdings(&mut c, &[holding("X", 200)])
            .await
            .unwrap();
        assert_eq!(p.len(), 1);
        assert_eq!(int(&p[0], "difference"), 100);
        assert!(
            p[0].get("likely_cause")
                .unwrap()
                .py_str()
                .contains("corporate action")
        );
    }

    #[tokio::test]
    async fn a_holding_with_no_history_is_reported() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        let p = reconcile_with_holdings(&mut c, &[holding("NEW", 50)])
            .await
            .unwrap();
        assert_eq!(
            p[0].get("likely_cause").unwrap().py_str(),
            "no tradebook history for this symbol"
        );
    }

    #[tokio::test]
    async fn matching_quantities_reconcile_clean() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        import(&mut c, &tb(&dir, &[r("X", "2025-01-01", "buy", 100, 50.0)])).await;
        assert!(
            reconcile_with_holdings(&mut c, &[holding("X", 100)])
                .await
                .unwrap()
                .is_empty()
        );
    }

    #[tokio::test]
    async fn coverage_reports_what_tax_can_reason_about() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        import(&mut c, &tb(&dir, &[r("A", "2025-01-01", "buy", 10, 50.0)])).await;
        let cov = coverage(&mut c, &[holding("A", 0), holding("B", 0), holding("C", 0)])
            .await
            .unwrap();
        assert_eq!((int(&cov, "with_lots"), int(&cov, "holdings")), (1, 3));
        assert!((pyf(cov.get("coverage_pct")) - 33.3).abs() < 1e-9);
        assert_eq!(cov.get("missing").unwrap().py_repr(), "['B', 'C']");
    }

    // --- live capture --------------------------------------------------------------------

    fn ktrade(
        symbol: &str,
        side: &str,
        qty: i64,
        price: f64,
        when: &str,
        tid: &str,
        product: &str,
    ) -> PyValue {
        PyValue::Map(dict([
            ("trade_id", PyValue::Str(tid.into())),
            ("tradingsymbol", PyValue::Str(symbol.into())),
            ("transaction_type", PyValue::Str(side.into())),
            ("quantity", PyValue::Int(qty)),
            ("average_price", PyValue::Float(price)),
            ("product", PyValue::Str(product.into())),
            ("exchange", PyValue::Str("NSE".into())),
            ("fill_timestamp", PyValue::Str(when.into())),
        ]))
    }

    fn k(symbol: &str, side: &str, qty: i64, price: f64, when: &str) -> PyValue {
        ktrade(symbol, side, qty, price, when, "K1", "CNC")
    }

    #[tokio::test]
    async fn a_captured_sell_consumes_a_lot_bought_months_earlier() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        import(&mut c, &tb(&dir, &[r("X", "2025-01-01", "buy", 100, 50.0)])).await;
        let res = capture_live_trades(
            &mut c,
            &[k("X", "SELL", 40, 90.0, "2026-08-15 10:00:00")],
            now(),
        )
        .await
        .unwrap();
        assert_eq!((int(&res, "new"), int(&res, "closed_trades")), (1, 1));
        let cur = c
            .execute("SELECT * FROM trades WHERE exit_ts IS NOT NULL", &[])
            .await
            .unwrap();
        let closed = cur.fetchone().unwrap();
        assert_eq!(closed.get("qty"), Some(&DbValue::Int(40)));
        assert_eq!(closed.get("entry_price"), Some(&DbValue::Float(50.0)));
        assert!((closed.get("pnl").and_then(DbValue::as_f64).unwrap() - 1600.0).abs() < 1e-9);
        assert_eq!(
            scalar(&mut c, "SELECT qty FROM trades WHERE exit_ts IS NULL").await,
            DbValue::Int(60)
        );
    }

    #[tokio::test]
    async fn capturing_the_same_session_twice_changes_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        import(&mut c, &tb(&dir, &[r("X", "2025-01-01", "buy", 100, 50.0)])).await;
        let trades_ = [k("X", "SELL", 40, 90.0, "2026-08-15 10:00:00")];
        capture_live_trades(&mut c, &trades_, now()).await.unwrap();
        let before = c
            .execute("SELECT * FROM trades ORDER BY id", &[])
            .await
            .unwrap()
            .rows;
        let second = capture_live_trades(&mut c, &trades_, now()).await.unwrap();
        let after = c
            .execute("SELECT * FROM trades ORDER BY id", &[])
            .await
            .unwrap()
            .rows;
        assert_eq!(int(&second, "new"), 0);
        assert_eq!(before, after);
    }

    #[tokio::test]
    async fn a_quiet_session_captures_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        let res = capture_live_trades(&mut c, &[], now()).await.unwrap();
        assert_eq!(int(&res, "captured"), 0);
        assert_eq!(res.get("symbols"), Some(&PyValue::List(Vec::new())));
    }

    #[tokio::test]
    async fn non_cnc_legs_are_excluded() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        capture_live_trades(
            &mut c,
            &[
                ktrade("X", "BUY", 10, 5.0, "2026-08-15 10:00:00", "A", "MIS"),
                ktrade("Y", "BUY", 10, 5.0, "2026-08-15 10:00:00", "B", "CNC"),
            ],
            now(),
        )
        .await
        .unwrap();
        let syms: Vec<String> = c
            .execute("SELECT symbol FROM fills", &[])
            .await
            .unwrap()
            .rows
            .iter()
            .map(|r| as_text(r.get("symbol")))
            .collect();
        assert_eq!(syms, ["Y"]);
    }

    #[test]
    fn a_trade_without_a_usable_timestamp_is_refused() {
        let row = PyValue::Map(dict([
            ("trade_id", PyValue::Str("Z".into())),
            ("tradingsymbol", PyValue::Str("X".into())),
            ("transaction_type", PyValue::Str("BUY".into())),
            ("quantity", PyValue::Int(1)),
            ("average_price", PyValue::Float(5.0)),
            ("product", PyValue::Str("CNC".into())),
        ]));
        assert!(fills_from_kite(&[row]).unwrap_err().0.contains("timestamp"));
    }

    #[test]
    fn an_unrecognised_side_is_refused_not_guessed() {
        let e = fills_from_kite(&[k("X", "TRANSFER", 1, 5.0, "2026-08-15 10:00:00")]).unwrap_err();
        assert!(e.0.contains("transaction_type"));
    }

    #[tokio::test]
    async fn capture_and_csv_coexist_without_duplicating() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        capture_live_trades(
            &mut c,
            &[ktrade(
                "X",
                "BUY",
                100,
                50.0,
                "2026-08-15 09:30:00",
                "T1",
                "CNC",
            )],
            now(),
        )
        .await
        .unwrap();
        import(&mut c, &tb(&dir, &[r("X", "2026-08-15", "buy", 100, 50.0)])).await;
        assert_eq!(
            scalar(&mut c, "SELECT COUNT(*) c FROM fills").await,
            DbValue::Int(1)
        );
        assert_eq!(
            scalar(&mut c, "SELECT COUNT(*) c FROM trades").await,
            DbValue::Int(1)
        );
    }

    #[tokio::test]
    async fn a_csv_row_without_a_trade_id_still_dedupes() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        let f = write(
            &dir,
            &["X,2025-01-01,buy,100,50.0".to_owned()],
            "symbol,trade_date,trade_type,quantity,price",
            "no_id.csv",
        );
        import(&mut c, &f).await;
        import(&mut c, &f).await;
        assert_eq!(
            scalar(&mut c, "SELECT COUNT(*) c FROM fills").await,
            DbValue::Int(1)
        );
    }

    #[tokio::test]
    async fn capture_leaves_other_symbols_alone() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        import(
            &mut c,
            &tb(&dir, &[r("OTHER", "2025-01-01", "buy", 7, 3.0)]),
        )
        .await;
        capture_live_trades(
            &mut c,
            &[k("X", "BUY", 1, 5.0, "2026-08-15 10:00:00")],
            now(),
        )
        .await
        .unwrap();
        assert_eq!(
            scalar(&mut c, "SELECT qty FROM trades WHERE symbol='OTHER'").await,
            DbValue::Int(7)
        );
    }

    // --- provenance ----------------------------------------------------------------------

    #[tokio::test]
    async fn fills_status_is_empty_before_anything_is_imported() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        let f = fills_status(&mut c, now().date()).await.unwrap();
        assert_eq!(int(&f, "total"), 0);
        assert_eq!(f.get("sources"), Some(&PyValue::List(Vec::new())));
        assert_eq!(f.get("capture_ever_run"), Some(&PyValue::Bool(false)));
        assert_eq!(f.get("captured_today"), Some(&PyValue::Bool(false)));
    }

    #[tokio::test]
    async fn fills_status_separates_the_two_sources() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        import(&mut c, &tb(&dir, &[r("X", "2025-01-01", "buy", 100, 50.0)])).await;
        capture_live_trades(
            &mut c,
            &[k("X", "SELL", 10, 90.0, "2026-08-15 10:00:00")],
            now(),
        )
        .await
        .unwrap();
        let f = fills_status(&mut c, now().date()).await.unwrap();
        assert_eq!(int(&f, "total"), 2);
        let PyValue::List(sources) = f.get("sources").unwrap() else {
            panic!()
        };
        let labels: Vec<(String, i64)> = sources
            .iter()
            .map(|s| {
                let PyValue::Map(m) = s else { panic!() };
                (m.get("label").unwrap().py_str(), int(m, "count"))
            })
            .collect();
        assert_eq!(
            labels,
            [
                ("Console export".to_owned(), 1),
                ("Daily capture".to_owned(), 1)
            ]
        );
        assert_eq!(f.get("capture_ever_run"), Some(&PyValue::Bool(true)));
        assert_eq!(f.get("captured_today"), Some(&PyValue::Bool(true)));
    }

    #[tokio::test]
    async fn capture_run_on_an_earlier_day_is_not_reported_as_today() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        capture_live_trades(
            &mut c,
            &[k("X", "BUY", 1, 5.0, "2026-08-15 10:00:00")],
            now(),
        )
        .await
        .unwrap();
        c.execute("UPDATE fills SET captured_at='2020-01-01T18:30:00'", &[])
            .await
            .unwrap();
        let f = fills_status(&mut c, now().date()).await.unwrap();
        assert_eq!(f.get("capture_ever_run"), Some(&PyValue::Bool(true)));
        assert_eq!(f.get("captured_today"), Some(&PyValue::Bool(false)));
    }

    #[tokio::test]
    async fn status_carries_fill_provenance_and_the_empty_state_still_names_problems() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        let pos = [dict([
            ("symbol", PyValue::Str("X".into())),
            ("quantity", PyValue::Int(100)),
            ("average_price", PyValue::Float(50.0)),
        ])];
        let empty = status(&mut c, &pos, now().date()).await.unwrap();
        assert_eq!(empty.get("has_lots"), Some(&PyValue::Bool(false)));
        assert!(empty.get("problems").unwrap().truthy());
        import(&mut c, &tb(&dir, &[r("X", "2025-01-01", "buy", 100, 50.0)])).await;
        let st = status(&mut c, &pos, now().date()).await.unwrap();
        let PyValue::Map(fills) = st.get("fills").unwrap() else {
            panic!()
        };
        assert_eq!(int(fills, "total"), 1);
    }
}
