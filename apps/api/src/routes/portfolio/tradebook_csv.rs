//! Ports `baskfy_core.tradebook.parse_tradebook_csv` — a Zerodha Console tradebook export read
//! strictly (an unreadable number, date or side refuses the whole file with its line) — with the
//! `csv.DictReader` and `datetime.strptime` behaviour it relies on.

use std::sync::LazyLock;

use baskfy_core::decimal::WideDecimal;
use chrono::{NaiveDate, NaiveDateTime, NaiveTime};
use regex::Regex;

use super::model::{PyDecimal, py_decimal, py_strip};
use super::pycsv::{CsvError, Reader};

/// `MAX_TRADEBOOK_ROWS`.
const MAX_TRADEBOOK_ROWS: usize = 200_000;

/// Why the file was refused: `TradebookError` (a 400) or `csv.Error` (Python's 500).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum TradebookFailure {
    /// `TradebookError(message)`.
    Refused(String),
    /// `csv.Error`.
    Csv(CsvError),
}

/// `TradeFill`, as the importer stores it.
#[derive(Debug, Clone)]
pub(crate) struct ParsedFill {
    pub(crate) symbol: String,
    pub(crate) exchange: String,
    pub(crate) is_buy: bool,
    pub(crate) quantity: WideDecimal,
    pub(crate) price: WideDecimal,
    pub(crate) trade_date: NaiveDate,
    pub(crate) trade_id: String,
    pub(crate) order_id: Option<String>,
    pub(crate) isin: Option<String>,
    /// Naive: Console prints IST wall time.
    pub(crate) executed_at: Option<NaiveDateTime>,
}

/// `ParsedTradebook`.
#[derive(Debug, Clone, Default)]
pub(crate) struct ParsedTradebook {
    pub(crate) fills: Vec<ParsedFill>,
    pub(crate) skipped_non_equity: usize,
}

fn refused(message: String) -> TradebookFailure {
    TradebookFailure::Refused(message)
}

fn repr_opt(raw: Option<&str>) -> String {
    raw.map_or_else(|| "None".to_owned(), baskfy_providers::py::repr_str)
}

/// `_decimal(raw, field=, line=)`.
fn decimal(raw: Option<&str>, field: &str, line: usize) -> Result<WideDecimal, TradebookFailure> {
    let text = raw.unwrap_or("").replace([',', '₹'], "");
    match py_decimal(py_strip(&text)) {
        PyDecimal::Finite(d) => Ok(d),
        _ => Err(refused(format!(
            "line {line}: {field} {} is not a number",
            repr_opt(raw)
        ))),
    }
}

const MONTHS: [&str; 12] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
];

/// One `strptime` format, as `_strptime` compiles it (case-insensitive; a space is `\s+`).
fn compile(format: &str) -> Option<Regex> {
    let mut pattern = String::from("(?i)^");
    let mut chars = format.chars();
    while let Some(c) = chars.next() {
        if c == '%' {
            let part = match chars.next()? {
                'Y' => r"(?P<Y>[0-9][0-9][0-9][0-9])",
                'm' => r"(?P<m>1[0-2]|0[1-9]|[1-9])",
                'd' => r"(?P<d>3[01]|[12][0-9]|0[1-9]|[1-9]| [1-9])",
                'b' => r"(?P<b>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                'H' => r"(?P<H>2[0-3]|[0-1][0-9]|[0-9])",
                'M' => r"(?P<M>[0-5][0-9]|[0-9])",
                'S' => r"(?P<S>6[0-1]|[0-5][0-9]|[0-9])",
                'f' => r"(?P<f>[0-9]{1,6})",
                _ => return None,
            };
            pattern.push_str(part);
        } else if c.is_whitespace() {
            pattern.push_str(r"\s+");
        } else {
            pattern.push_str(&regex::escape(&c.to_string()));
        }
    }
    Regex::new(&pattern).ok()
}

const DATE_FORMATS: [&str; 6] = [
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d",
];
const DATETIME_FORMATS: [&str; 5] = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%d/%m/%Y %H:%M:%S",
];

static DATE_RES: LazyLock<Vec<Regex>> =
    LazyLock::new(|| DATE_FORMATS.iter().filter_map(|f| compile(f)).collect());
static DATETIME_RES: LazyLock<Vec<Regex>> =
    LazyLock::new(|| DATETIME_FORMATS.iter().filter_map(|f| compile(f)).collect());

/// `datetime.strptime(text, fmt)` for one compiled format: `None` is its `ValueError`.
fn strptime(re: &Regex, text: &str) -> Option<NaiveDateTime> {
    let caps = re.captures(text)?;
    // `match()` anchors the start only; anything left over is "unconverted data remains".
    if caps.get(0)?.end() != text.len() {
        return None;
    }
    let num = |name: &str| -> Option<u32> {
        caps.name(name)
            .map(|m| m.as_str().trim().parse::<u32>().ok())
            .unwrap_or(Some(0))
    };
    let year = i32::try_from(num("Y")?).ok()?;
    let month = match caps.name("b") {
        Some(b) => {
            let lower = b.as_str().to_ascii_lowercase();
            u32::try_from(MONTHS.iter().position(|m| *m == lower)? + 1).ok()?
        }
        None => num("m")?,
    };
    let day = num("d")?;
    let date = NaiveDate::from_ymd_opt(year, month, day)?;
    let micros = match caps.name("f") {
        Some(f) => format!("{:0<6}", f.as_str()).parse::<u32>().ok()?,
        None => 0,
    };
    let time = NaiveTime::from_hms_micro_opt(num("H")?, num("M")?, num("S")?, micros)?;
    Some(NaiveDateTime::new(date, time))
}

/// `_date(raw, line=)`.
fn trade_date(raw: Option<&str>, line: usize) -> Result<NaiveDate, TradebookFailure> {
    let text = py_strip(raw.unwrap_or(""));
    for re in DATE_RES.iter() {
        if let Some(dt) = strptime(re, text) {
            return Ok(dt.date());
        }
    }
    Err(refused(format!(
        "line {line}: trade date {} is not a date this reader knows",
        repr_opt(raw)
    )))
}

/// `_datetime(raw)`.
fn executed_at(raw: Option<&str>) -> Option<NaiveDateTime> {
    let text = py_strip(raw.unwrap_or(""));
    DATETIME_RES.iter().find_map(|re| strptime(re, text))
}

/// `{key.strip().lower(): original}` — a later duplicate key keeps the first key's place.
struct Columns(Vec<(String, String)>);

impl Columns {
    /// `_column(columns, *needles, exclude=...)`.
    fn find(&self, needles: &[&str], exclude: &[&str]) -> Option<String> {
        self.0
            .iter()
            .find(|(key, _)| {
                needles.iter().all(|n| key.contains(n)) && !exclude.iter().any(|x| key.contains(x))
            })
            .map(|(_, original)| original.clone())
    }
}

/// `x or y` over two optional column names: an empty name is falsy.
fn or(a: Option<String>, b: impl FnOnce() -> Option<String>) -> Option<String> {
    match a {
        Some(s) if !s.is_empty() => Some(s),
        _ => b(),
    }
}

/// Python's `repr` of a `list[str]`.
fn list_repr<S: AsRef<str>>(items: &[S]) -> String {
    let parts: Vec<String> = items
        .iter()
        .map(|s| baskfy_providers::py::repr_str(s.as_ref()))
        .collect();
    format!("[{}]", parts.join(", "))
}

/// `parse_tradebook_csv(text)`.
///
/// # Errors
/// `TradebookError` with Python's sentence; `csv.Error` as Python raises it.
#[allow(clippy::too_many_lines)] // one pass, one refusal each, as the Python is
pub(crate) fn parse_tradebook_csv(text: &str) -> Result<ParsedTradebook, TradebookFailure> {
    let mut reader = Reader::new(text.trim_start_matches('\u{feff}'));
    let fieldnames = reader
        .next_row()
        .map_err(TradebookFailure::Csv)?
        .unwrap_or_default();
    if fieldnames.is_empty() {
        return Err(refused("the file is empty or is not a CSV".to_owned()));
    }
    let mut columns = Columns(Vec::new());
    for name in &fieldnames {
        let key = py_strip(name).to_lowercase();
        match columns.0.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 = name.clone(),
            None => columns.0.push((key, name.clone())),
        }
    }
    let c_symbol = or(columns.find(&["symbol"], &[]), || {
        columns.find(&["tradingsymbol"], &[])
    });
    let c_side = or(columns.find(&["trade", "type"], &[]), || {
        columns.find(&["transaction"], &[])
    });
    let c_qty = or(columns.find(&["quantity"], &[]), || {
        columns.find(&["qty"], &[])
    });
    let c_price = columns.find(&["price"], &["average"]);
    let c_date = or(columns.find(&["trade", "date"], &[]), || {
        columns.find(&["date"], &["time"])
    });
    let c_trade_id = columns.find(&["trade", "id"], &[]);
    let c_order_id = columns.find(&["order", "id"], &[]);
    let c_time = or(columns.find(&["execution", "time"], &[]), || {
        columns.find(&["order", "time"], &[])
    });
    let c_exchange = columns.find(&["exchange"], &[]);
    let c_segment = columns.find(&["segment"], &[]);
    let c_isin = columns.find(&["isin"], &[]);
    let required = [
        ("symbol", &c_symbol),
        ("trade_date", &c_date),
        ("trade_type", &c_side),
        ("quantity", &c_qty),
        ("price", &c_price),
        ("trade_id", &c_trade_id),
    ];
    let missing: Vec<&str> = required
        .iter()
        .filter(|(_, c)| c.is_none())
        .map(|(n, _)| *n)
        .collect();
    let (Some(c_symbol), Some(c_date), Some(c_side), Some(c_qty), Some(c_price), Some(c_trade_id)) =
        (c_symbol, c_date, c_side, c_qty, c_price, c_trade_id)
    else {
        return Err(refused(format!(
            "missing columns {}. Export console.zerodha.com → Reports → Tradebook as CSV. \
             Found: {}",
            list_repr(&missing),
            list_repr(&fieldnames)
        )));
    };

    let mut out = ParsedTradebook::default();
    let mut line = 1usize;
    while let Some(row) = reader.next_row().map_err(TradebookFailure::Csv)? {
        if row.is_empty() {
            continue;
        }
        line += 1;
        if line > MAX_TRADEBOOK_ROWS + 1 {
            return Err(refused(format!(
                "more than {MAX_TRADEBOOK_ROWS} rows; split the export by year"
            )));
        }
        if row.len() > fieldnames.len() {
            return Err(refused(format!(
                "line {line}: more fields than the header — an unquoted comma inside a number \
                 would mis-read a quantity"
            )));
        }
        // `dict(zip(fieldnames, row))`: a later duplicate name wins; a short row reads `None`.
        let get = |name: &str| -> Option<&str> {
            fieldnames
                .iter()
                .enumerate()
                .rev()
                .find(|(_, f)| *f == name)
                .and_then(|(i, _)| row.get(i).map(String::as_str))
        };
        let symbol = py_strip(get(&c_symbol).unwrap_or("")).to_uppercase();
        if symbol.is_empty() {
            continue;
        }
        let exchange = match &c_exchange {
            Some(c) => py_strip(get(c).filter(|s| !s.is_empty()).unwrap_or("NSE")).to_uppercase(),
            None => "NSE".to_owned(),
        };
        let segment = match &c_segment {
            Some(c) => py_strip(get(c).unwrap_or("")).to_owned(),
            None => String::new(),
        };
        let equity_segment =
            ["eq", "equity", "cash", ""].contains(&segment.to_lowercase().as_str());
        if !["NSE", "BSE"].contains(&exchange.as_str()) || !equity_segment {
            out.skipped_non_equity += 1;
            continue;
        }
        let side_raw = py_strip(get(&c_side).unwrap_or("")).to_lowercase();
        let is_buy = if ["buy", "b", "bought", "purchase"].contains(&side_raw.as_str()) {
            true
        } else if ["sell", "s", "sold", "sale"].contains(&side_raw.as_str()) {
            false
        } else {
            return Err(refused(format!(
                "line {line} ({symbol}): trade type {} is not buy/sell",
                baskfy_providers::py::repr_str(&side_raw)
            )));
        };
        let quantity = decimal(get(&c_qty), "quantity", line)?.abs();
        if quantity.is_zero() {
            return Err(refused(format!("line {line} ({symbol}): quantity is zero")));
        }
        let price = decimal(get(&c_price), "price", line)?;
        if price < WideDecimal::zero() {
            return Err(refused(format!(
                "line {line} ({symbol}): price is negative"
            )));
        }
        let trade_id = py_strip(get(&c_trade_id).unwrap_or("")).to_owned();
        if trade_id.is_empty() {
            return Err(refused(format!("line {line} ({symbol}): no trade id")));
        }
        let order_id = c_order_id
            .as_ref()
            .map(|c| py_strip(get(c).unwrap_or("")).to_owned())
            .filter(|s| !s.is_empty());
        let isin = c_isin
            .as_ref()
            .map(|c| py_strip(get(c).unwrap_or("")).to_owned())
            .filter(|s| !s.is_empty());
        let date = trade_date(get(&c_date), line)?;
        out.fills.push(ParsedFill {
            symbol,
            exchange,
            is_buy,
            quantity,
            price,
            trade_date: date,
            trade_id,
            order_id,
            isin,
            executed_at: c_time.as_ref().and_then(|c| executed_at(get(c))),
        });
    }
    if out.fills.is_empty() && out.skipped_non_equity == 0 {
        return Err(refused("no trades in the file".to_owned()));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    const HEADER: &str = "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,\
                          quantity,price,trade_id,order_id,order_execution_time\n";

    #[test]
    fn a_console_export_reads_strictly() {
        // Ports test_tradebook.py's parser assertions.
        let text = format!(
            "{HEADER}INFY,INE009A01021,2024-01-15,NSE,EQ,EQ,buy,false,10.000000,1500.50,T1,O1,\
             2024-01-15T09:30:00\nNIFTY24JANFUT,,2024-01-15,NFO,FO,,buy,false,50,1,T2,O2,\n\
             tcs,,15-01-2024,BSE,EQ,EQ,SELL,false,-3,3000,T3,,\n"
        );
        let parsed = parse_tradebook_csv(&text).unwrap();
        assert_eq!(parsed.skipped_non_equity, 1);
        assert_eq!(parsed.fills.len(), 2);
        assert_eq!(parsed.fills[0].quantity.to_sci_string(), "10.000000");
        assert!(parsed.fills[0].executed_at.is_some());
        assert_eq!(parsed.fills[1].symbol, "TCS");
        assert_eq!(parsed.fills[1].quantity.to_sci_string(), "3");
        assert!(!parsed.fills[1].is_buy);
        let bad = format!("{HEADER}INFY,,2024-01-15,NSE,EQ,EQ,hold,false,1,1,T1,,\n");
        assert_eq!(
            parse_tradebook_csv(&bad).unwrap_err(),
            TradebookFailure::Refused(
                "line 2 (INFY): trade type 'hold' is not buy/sell".to_owned()
            )
        );
        assert_eq!(
            parse_tradebook_csv("a,b\n1,2\n").unwrap_err(),
            TradebookFailure::Refused(
                "missing columns ['symbol', 'trade_date', 'trade_type', 'quantity', 'price', \
                 'trade_id']. Export console.zerodha.com → Reports → Tradebook as CSV. Found: \
                 ['a', 'b']"
                    .to_owned()
            )
        );
        assert!(trade_date(Some("1-2-2024"), 2).is_ok());
        assert!(trade_date(Some("15 jan 2024"), 2).is_ok());
        assert!(trade_date(Some("31-02-2024"), 2).is_err());
        assert!(executed_at(Some("2024-01-15T09:30:00.5")).is_some());
    }
}
