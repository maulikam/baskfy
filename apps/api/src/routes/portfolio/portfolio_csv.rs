//! Ports `baskfy_core.portfolio_csv` — the portfolio CSV parser (bytes in, normalised rows and a
//! report of what could not be used out) and the vocabulary the import report is written in.
//! Pure; resolving a symbol to an instrument is `super::symbols`.

use std::sync::LazyLock;

use baskfy_core::decimal::WideDecimal;
use regex::Regex;
use serde::Serialize;

use super::model::{PyDecimal, py_decimal, py_isspace, py_strip};
use super::pycsv::{CsvError, read_all};

/// `MAX_IMPORT_ROWS`.
pub(crate) const MAX_IMPORT_ROWS: usize = 500;

const SYMBOL_HEADERS: [&str; 8] = [
    "symbol",
    "symbols",
    "ticker",
    "tradingsymbol",
    "scrip",
    "scripcode",
    "instrument",
    "stock",
];
const QUANTITY_HEADERS: [&str; 5] = ["quantity", "qty", "shares", "units", "holdingqty"];
const AVG_PRICE_HEADERS: [&str; 7] = [
    "avgprice",
    "averageprice",
    "avgcost",
    "averagecost",
    "buyprice",
    "price",
    "avgtradedprice",
];

static SYMBOL_RE: LazyLock<Option<Regex>> =
    LazyLock::new(|| Regex::new(r"^[A-Z0-9][A-Z0-9&.\-]{0,29}$").ok());
static BSE_CODE_RE: LazyLock<Option<Regex>> = LazyLock::new(|| Regex::new(r"^\d{5,6}$").ok());

/// Why a parse refused the whole file: `CsvParseError` (a 400) or the `csv.Error` nothing catches.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ParseFailure {
    /// `CsvParseError(message)`.
    Refused(String),
    /// `csv.Error` — Python's 500 catch-all.
    Csv(CsvError),
}

/// `SkipReason`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum SkipReason {
    Blank,
    NoSymbol,
    Duplicate,
    Truncated,
}

/// `RowIssue`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RowIssue {
    UnreadableQuantity,
    UnreadableAvgPrice,
    SuffixStripped,
}

/// `SymbolShape`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SymbolShape {
    Symbol,
    BseCode,
    Invalid,
}

/// `MatchStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum MatchStatus {
    Matched,
    Ambiguous,
    Unmatched,
}

/// `UnmatchedReason`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum UnmatchedReason {
    UnknownSymbol,
    BseCode,
    Invalid,
}

/// `ParsedHolding`.
#[derive(Debug, Clone)]
pub(crate) struct ParsedHolding {
    pub(crate) line_number: usize,
    pub(crate) raw_symbol: String,
    pub(crate) symbol: String,
    pub(crate) shape: SymbolShape,
    pub(crate) quantity: Option<WideDecimal>,
    pub(crate) avg_price: Option<WideDecimal>,
    pub(crate) issues: Vec<RowIssue>,
}

/// `SkippedRow`.
#[derive(Debug, Clone)]
pub(crate) struct SkippedRow {
    pub(crate) line_number: usize,
    pub(crate) reason: SkipReason,
    pub(crate) raw: String,
}

/// `ParsedCsv`.
#[derive(Debug, Clone, Default)]
pub(crate) struct ParsedCsv {
    pub(crate) rows: Vec<ParsedHolding>,
    pub(crate) skipped: Vec<SkippedRow>,
    pub(crate) ignored_columns: Vec<String>,
}

impl ParsedCsv {
    /// `total_lines`.
    pub(crate) fn total_lines(&self) -> usize {
        self.rows.len() + self.skipped.len()
    }
}

/// Python's `str.upper()`.
fn py_upper(s: &str) -> String {
    s.to_uppercase()
}

/// `normalise_symbol(raw)`: trim, upper-case, drop a trailing `.NS`.
pub(crate) fn normalise_symbol(raw: &str) -> (String, Vec<RowIssue>) {
    let token = py_upper(py_strip(py_strip(raw).trim_matches('"').trim_matches('\'')));
    let mut issues = Vec::new();
    if token.ends_with(".NS") && token.chars().count() > 3 {
        let cut = token.len() - 3;
        issues.push(RowIssue::SuffixStripped);
        return (token[..cut].to_owned(), issues);
    }
    (token, issues)
}

/// `classify_symbol(token)`.
pub(crate) fn classify_symbol(token: &str) -> SymbolShape {
    if BSE_CODE_RE.as_ref().is_some_and(|re| re.is_match(token)) {
        SymbolShape::BseCode
    } else if SYMBOL_RE.as_ref().is_some_and(|re| re.is_match(token)) {
        SymbolShape::Symbol
    } else {
        SymbolShape::Invalid
    }
}

/// `_canonical(name)`: lower-cased, letters and digits only.
fn canonical(name: &str) -> String {
    py_strip(name)
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        .collect()
}

fn known(c: &str) -> bool {
    SYMBOL_HEADERS.contains(&c) || QUANTITY_HEADERS.contains(&c) || AVG_PRICE_HEADERS.contains(&c)
}

/// `_number(cells, index)`: `(value, unreadable)`. Python's non-finite `Decimal`s (`NaN`,
/// `Infinity`) are read as unreadable here (DECISIONS-RS R6.21).
fn number(cells: &[String], index: Option<usize>) -> (Option<WideDecimal>, bool) {
    let Some(cell) = index.and_then(|i| cells.get(i)) else {
        return (None, false);
    };
    let raw = py_strip(cell);
    if raw.is_empty() {
        return (None, false);
    }
    let cleaned: String = raw
        .chars()
        .filter(|c| *c != ',' && *c != '₹' && !py_isspace(*c))
        .collect();
    match py_decimal(&cleaned) {
        PyDecimal::Finite(d) => (Some(d), false),
        _ => (None, true),
    }
}

/// `parse_portfolio_csv(text)`.
///
/// # Errors
/// `CsvParseError` for an empty file or one with no symbol column; `csv.Error` as Python raises it.
pub(crate) fn parse_portfolio_csv(text: &str) -> Result<ParsedCsv, ParseFailure> {
    let body = text.trim_start_matches('\u{feff}');
    if py_strip(body).is_empty() {
        return Err(ParseFailure::Refused("The file is empty.".to_owned()));
    }
    let lines = read_all(body).map_err(ParseFailure::Csv)?;
    let (header, start): (Vec<String>, usize) = match lines.first() {
        Some(first) if first.iter().any(|c| known(&canonical(c))) => (first.clone(), 1),
        _ => (Vec::new(), 0),
    };
    let (symbol_col, quantity_col, price_col) = if header.is_empty() {
        (Some(0), None, None)
    } else {
        let (mut s, mut q, mut p) = (None, None, None);
        for (i, cell) in header.iter().enumerate() {
            let c = canonical(cell);
            if s.is_none() && SYMBOL_HEADERS.contains(&c.as_str()) {
                s = Some(i);
            } else if q.is_none() && QUANTITY_HEADERS.contains(&c.as_str()) {
                q = Some(i);
            } else if p.is_none() && AVG_PRICE_HEADERS.contains(&c.as_str()) {
                p = Some(i);
            }
        }
        (s, q, p)
    };
    if !header.is_empty() && symbol_col.is_none() {
        let mut names = SYMBOL_HEADERS.to_vec();
        names.sort_unstable();
        return Err(ParseFailure::Refused(format!(
            "No symbol column. Expected a header naming one of: {}.",
            names.join(", ")
        )));
    }
    let symbol_at = symbol_col.unwrap_or(0);
    let mut parsed = ParsedCsv::default();
    let mut seen: Vec<String> = Vec::new();
    for (offset, cells) in lines.iter().enumerate().skip(start) {
        let line_number = offset + 1;
        if !cells.iter().any(|c| !py_strip(c).is_empty()) {
            parsed.skipped.push(SkippedRow {
                line_number,
                reason: SkipReason::Blank,
                raw: String::new(),
            });
            continue;
        }
        let raw_symbol = cells.get(symbol_at).cloned().unwrap_or_default();
        let (token, issues) = normalise_symbol(&raw_symbol);
        if token.is_empty() {
            let joined: Vec<&str> = cells.iter().map(|c| py_strip(c)).collect();
            parsed.skipped.push(SkippedRow {
                line_number,
                reason: SkipReason::NoSymbol,
                raw: joined.join(",").chars().take(120).collect(),
            });
            continue;
        }
        if seen.contains(&token) {
            parsed.skipped.push(SkippedRow {
                line_number,
                reason: SkipReason::Duplicate,
                raw: token,
            });
            continue;
        }
        if parsed.rows.len() >= MAX_IMPORT_ROWS {
            parsed.skipped.push(SkippedRow {
                line_number,
                reason: SkipReason::Truncated,
                raw: token,
            });
            continue;
        }
        let (quantity, quantity_issue) = number(cells, quantity_col);
        let (avg_price, price_issue) = number(cells, price_col);
        let mut row_issues = issues;
        if quantity_issue {
            row_issues.push(RowIssue::UnreadableQuantity);
        }
        if price_issue {
            row_issues.push(RowIssue::UnreadableAvgPrice);
        }
        seen.push(token.clone());
        parsed.rows.push(ParsedHolding {
            line_number,
            raw_symbol: py_strip(&raw_symbol).to_owned(),
            shape: classify_symbol(&token),
            symbol: token,
            quantity,
            avg_price,
            issues: row_issues,
        });
    }
    let used = [symbol_col, quantity_col, price_col];
    parsed.ignored_columns = header
        .iter()
        .enumerate()
        .filter(|(i, c)| !used.contains(&Some(*i)) && !py_strip(c).is_empty())
        .map(|(_, c)| py_strip(c).to_owned())
        .collect();
    Ok(parsed)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn messy_files_parse_as_python_does() {
        // Ports test_portfolio_csv.py's acceptance case: extra columns, whitespace, lowercase,
        // a BSE code, a blank row, a duplicate and a `.NS` suffix.
        let text = "Symbol, Qty ,Avg. Price,Sector\n infy ,10,\"1,500.50\",IT\n\n532540,5,,\n\
                    tcs.ns,x,3000,IT\nINFY,1,1,IT\n,3,4,\n";
        let p = parse_portfolio_csv(text).unwrap();
        assert_eq!(p.ignored_columns, vec!["Sector"]);
        assert_eq!(p.rows.len(), 3);
        assert_eq!(p.rows[0].symbol, "INFY");
        assert_eq!(p.rows[0].raw_symbol, "infy");
        assert_eq!(
            p.rows[0].avg_price.as_ref().map(WideDecimal::to_sci_string),
            Some("1500.50".to_owned())
        );
        assert_eq!(p.rows[1].shape, SymbolShape::BseCode);
        assert_eq!(p.rows[2].symbol, "TCS");
        assert_eq!(
            p.rows[2].issues,
            vec![RowIssue::SuffixStripped, RowIssue::UnreadableQuantity]
        );
        let reasons: Vec<SkipReason> = p.skipped.iter().map(|s| s.reason).collect();
        assert_eq!(
            reasons,
            vec![
                SkipReason::Blank,
                SkipReason::Duplicate,
                SkipReason::NoSymbol
            ]
        );
        assert_eq!(p.skipped[2].raw, ",3,4,");
        assert_eq!(p.total_lines(), 6);
        assert!(matches!(
            parse_portfolio_csv("quantity,avg_price\n1,2\n"),
            Err(ParseFailure::Refused(m)) if m.starts_with("No symbol column")
        ));
        assert!(matches!(
            parse_portfolio_csv(" \n\t"),
            Err(ParseFailure::Refused(m)) if m == "The file is empty."
        ));
        // A headerless list of symbols.
        let bare = parse_portfolio_csv("INFY\nTCS\n").unwrap();
        assert_eq!(bare.rows.len(), 2);
    }
}
