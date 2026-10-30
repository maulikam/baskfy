//! `app/analytics/corporate_actions.py` — bonus issues and splits, which move quantity
//! without a trade.
//!
//! Bonus: nil cost (s.55), holding period starts at allotment — a new lot. Split: nothing is
//! acquired; quantity multiplies, cost per share divides, holding period inherited — the
//! existing lots are rewritten. Ratios are stored new:old.
//!
//! [`apply_to_book`] is generic over [`BookLot`] so the tradebook lane's `Lot` adopts it
//! without this module owning that type.

use chrono::{NaiveDate, NaiveDateTime, NaiveTime};

use crate::lanes::exec_core::gtt::{py_round, py_round0};
use crate::pyfmt::{PyArg, format_value};
use crate::storage::{Conn, DbValue, Param, Row, StorageError};
use crate::wire::{PyDict, PyValue};

/// `BONUS`.
pub const BONUS: &str = "bonus";
/// `SPLIT`.
pub const SPLIT: &str = "split";
/// `KINDS`.
pub const KINDS: [&str; 2] = [BONUS, SPLIT];

/// `CorporateActionError`: the message Python raises (and the page redirects with).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct CorporateActionError(pub String);

/// `Action`.
#[derive(Debug, Clone, PartialEq)]
pub struct Action {
    /// `symbol`.
    pub symbol: String,
    /// `kind`: [`BONUS`] or [`SPLIT`].
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

fn g(x: f64) -> String {
    format_value(&PyArg::Float(x), "g").unwrap_or_else(|_| x.to_string())
}

impl Action {
    /// `multiple`: new / old.
    #[must_use]
    pub fn multiple(&self) -> f64 {
        self.ratio_new / self.ratio_old
    }

    /// `when`: 00:01 on the ex-date, so the adjustment precedes any trade that session.
    #[must_use]
    pub fn when(&self) -> NaiveDateTime {
        self.ex_date
            .and_time(NaiveTime::from_hms_opt(0, 1, 0).unwrap_or(NaiveTime::MIN))
    }

    /// `describe()`.
    #[must_use]
    pub fn describe(&self) -> String {
        let r = format!("{}:{}", g(self.ratio_new), g(self.ratio_old));
        if self.kind == BONUS {
            return format!(
                "{r} bonus — {} new share(s) per share held, nil cost, holding period starts {}",
                g(self.multiple()),
                self.ex_date.format("%Y-%m-%d")
            );
        }
        format!(
            "{r} split — each share becomes {}, cost per share divided, holding period inherited",
            g(self.multiple())
        )
    }
}

/// Python's `repr(str)` for the messages that quote user input.
fn py_repr(s: &str) -> String {
    PyValue::Str(s.to_owned()).py_repr()
}

/// `float(x)` on a form string, as Python parses it.
fn py_float(raw: &str) -> Option<f64> {
    crate::settings::py_float(raw)
}

/// `dt.date.fromisoformat(s)` as Python 3.12 parses a date: `YYYY-MM-DD`, `YYYYMMDD`, and
/// the ISO week forms `YYYY-Www-D`, `YYYYWwwD`, `YYYY-Www`, `YYYYWww` (day defaults to 1;
/// week 53 only in a year that has one). ASCII digits only, as CPython's parser reads them.
fn fromisoformat(s: &str) -> Option<NaiveDate> {
    if !s.is_ascii() {
        return None;
    }
    let b = s.as_bytes();
    let digits = |r: std::ops::Range<usize>| -> Option<u32> {
        let part = s.get(r)?;
        if part.is_empty() || !part.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        part.parse().ok()
    };
    let year = i32::try_from(digits(0..4)?).ok()?;
    // MINYEAR is 1: `0000-01-01` is refused.
    if year < 1 {
        return None;
    }
    // Week dates: the fifth character (or the sixth after a dash) is 'W'.
    let (dash, w_at) = match (b.get(4), b.get(5)) {
        (Some(b'-'), Some(b'W')) => (true, 5),
        (Some(b'W'), _) => (false, 4),
        _ => (false, usize::MAX),
    };
    if w_at != usize::MAX {
        let week = digits(w_at + 1..w_at + 3)?;
        let day = match (dash, s.len()) {
            (true, 8) | (false, 7) => 1,
            (true, 10) if b.get(8) == Some(&b'-') => digits(9..10)?,
            (false, 8) => digits(7..8)?,
            _ => return None,
        };
        let weekday = match day {
            1 => chrono::Weekday::Mon,
            2 => chrono::Weekday::Tue,
            3 => chrono::Weekday::Wed,
            4 => chrono::Weekday::Thu,
            5 => chrono::Weekday::Fri,
            6 => chrono::Weekday::Sat,
            7 => chrono::Weekday::Sun,
            _ => return None,
        };
        return NaiveDate::from_isoywd_opt(year, week, weekday);
    }
    match s.len() {
        10 if b[4] == b'-' && b[7] == b'-' => {
            NaiveDate::from_ymd_opt(year, digits(5..7)?, digits(8..10)?)
        }
        8 => NaiveDate::from_ymd_opt(year, digits(4..6)?, digits(6..8)?),
        _ => None,
    }
}

/// `parse(symbol, kind, ex_date, ratio_new, ratio_old, note)`: validate before storing.
///
/// # Errors
/// The [`CorporateActionError`] Python raises, with its message.
pub fn parse(
    symbol: &str,
    kind: &str,
    ex_date: &str,
    ratio_new: &str,
    ratio_old: &str,
    note: &str,
    today: NaiveDate,
) -> Result<Action, CorporateActionError> {
    let kind = kind.trim().to_lowercase();
    if !KINDS.contains(&kind.as_str()) {
        return Err(CorporateActionError(format!(
            "kind must be one of {}, got {}",
            KINDS.join(", "),
            py_repr(&kind)
        )));
    }
    let symbol = symbol.trim().to_uppercase();
    if symbol.is_empty() {
        return Err(CorporateActionError("symbol is required".to_owned()));
    }
    let Some(d) = fromisoformat(ex_date.trim()) else {
        return Err(CorporateActionError(format!(
            "ex_date {} is not a YYYY-MM-DD date",
            py_repr(ex_date)
        )));
    };
    let (Some(new), Some(old)) = (py_float(ratio_new), py_float(ratio_old)) else {
        return Err(CorporateActionError("ratio must be numeric".to_owned()));
    };
    if new <= 0.0 || old <= 0.0 {
        return Err(CorporateActionError(
            "ratio parts must both be positive".to_owned(),
        ));
    }
    if kind == SPLIT && new / old <= 1.0 {
        return Err(CorporateActionError(
            "a split must increase the share count; use ratio 2:1 for a 1-into-2 split".to_owned(),
        ));
    }
    if d > today {
        return Err(CorporateActionError(format!(
            "ex_date {} is in the future",
            d.format("%Y-%m-%d")
        )));
    }
    Ok(Action {
        symbol,
        kind,
        ex_date: d,
        ratio_new: new,
        ratio_old: old,
        note: note.trim().to_owned(),
    })
}

/// `record(conn, action)`: upsert on `(symbol, kind, ex_date)`, returning the row id.
/// `now_iso` is `datetime.now().isoformat(timespec="seconds")`.
///
/// # Errors
/// The driver's.
pub async fn record(conn: &mut Conn, action: &Action, now_iso: &str) -> Result<i64, StorageError> {
    let params = vec![
        Param::Text(action.symbol.clone()),
        Param::Text(action.kind.clone()),
        Param::Text(action.ex_date.format("%Y-%m-%d").to_string()),
        Param::Float(action.ratio_new),
        Param::Float(action.ratio_old),
        Param::Text(action.note.clone()),
        Param::Text(now_iso.to_owned()),
    ];
    conn.transaction(move |c| {
        Box::pin(async move {
            let cur = c
                .execute(
                    "INSERT INTO corporate_actions(symbol, kind, ex_date, ratio_new, ratio_old, note, created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol, kind, ex_date) DO UPDATE SET ratio_new=excluded.ratio_new, ratio_old=excluded.ratio_old, note=excluded.note, created_at=excluded.created_at RETURNING id",
                    &params,
                )
                .await?;
            cur.rows
                .first()
                .and_then(|r| r.at(0))
                .and_then(db_int)
                .ok_or_else(|| StorageError::Other("RETURNING id gave no row".to_owned()))
        })
    })
    .await
}

fn db_int(v: &DbValue) -> Option<i64> {
    match v {
        DbValue::Int(i) => Some(*i),
        // int(float) truncates.
        #[allow(clippy::cast_possible_truncation)]
        DbValue::Float(f) => Some(f.trunc() as i64),
        DbValue::Text(s) => crate::settings::py_int(s),
        _ => None,
    }
}

fn db_float(v: Option<&DbValue>) -> f64 {
    match v {
        Some(DbValue::Float(f)) => *f,
        #[allow(clippy::cast_precision_loss)]
        Some(DbValue::Int(i)) => *i as f64,
        Some(DbValue::Text(s)) => py_float(s).unwrap_or(f64::NAN),
        _ => f64::NAN,
    }
}

fn db_text(v: Option<&DbValue>) -> String {
    match v {
        Some(DbValue::Text(s)) => s.clone(),
        Some(DbValue::Null) | None => String::new(),
        Some(other) => crate::lanes::analytics::web::db_to_py(other).py_str(),
    }
}

fn action_of(r: &Row, with_note: bool) -> Result<Action, StorageError> {
    let ex = db_text(r.get("ex_date"));
    let ex_date = fromisoformat(&ex)
        .ok_or_else(|| StorageError::Other(format!("Invalid isoformat string: {ex:?}")))?;
    Ok(Action {
        symbol: db_text(r.get("symbol")),
        kind: db_text(r.get("kind")),
        ex_date,
        ratio_new: db_float(r.get("ratio_new")),
        ratio_old: db_float(r.get("ratio_old")),
        note: if with_note {
            db_text(r.get("note"))
        } else {
            String::new()
        },
    })
}

/// `load(conn, symbols)`, ordered by ex-date then id.
///
/// # Errors
/// The driver's, or an unparseable stored date.
pub async fn load(
    conn: &mut Conn,
    symbols: Option<&[String]>,
) -> Result<Vec<Action>, StorageError> {
    let mut sql = "SELECT * FROM corporate_actions".to_owned();
    let mut args = Vec::new();
    if let Some(symbols) = symbols {
        if symbols.is_empty() {
            return Ok(vec![]);
        }
        sql.push_str(&format!(
            " WHERE symbol IN ({})",
            vec!["?"; symbols.len()].join(",")
        ));
        args = symbols.iter().map(|s| Param::Text(s.clone())).collect();
    }
    sql.push_str(" ORDER BY ex_date, id");
    let cur = conn.execute(&sql, &args).await?;
    cur.rows.iter().map(|r| action_of(r, true)).collect()
}

/// `listing(conn)`: every stored row as a dict plus `describe`, newest first.
///
/// # Errors
/// The driver's, or an unparseable stored date.
pub async fn listing(conn: &mut Conn) -> Result<Vec<PyDict>, StorageError> {
    let cur = conn
        .execute(
            "SELECT * FROM corporate_actions ORDER BY ex_date DESC, id DESC",
            &[],
        )
        .await?;
    let mut out = Vec::new();
    for r in &cur.rows {
        let mut d = crate::lanes::analytics::web::row_dict(r);
        d.insert("describe", PyValue::Str(action_of(r, false)?.describe()));
        out.push(d);
    }
    Ok(out)
}

/// `remove(conn, action_id)`: whether a row was deleted.
///
/// # Errors
/// The driver's.
pub async fn remove(conn: &mut Conn, action_id: i64) -> Result<bool, StorageError> {
    let n = conn
        .transaction(move |c| {
            Box::pin(async move {
                let cur = c
                    .execute(
                        "DELETE FROM corporate_actions WHERE id=?",
                        &[Param::Int(action_id)],
                    )
                    .await?;
                Ok(cur.rowcount)
            })
        })
        .await?;
    Ok(n > 0)
}

/// What [`apply_to_book`] needs of the tradebook's `Lot`.
pub trait BookLot: Sized {
    /// `Lot(symbol, when, quantity, price, cost)` — a bonus lot (price and cost nil).
    fn bonus_lot(symbol: &str, when: NaiveDateTime, quantity: i64) -> Self;
    /// `lot.quantity`.
    fn quantity(&self) -> i64;
    /// `lot.quantity = ...`.
    fn set_quantity(&mut self, quantity: i64);
    /// `lot.price`.
    fn price(&self) -> f64;
    /// `lot.price = ...`.
    fn set_price(&mut self, price: f64);
}

#[allow(clippy::cast_precision_loss)]
fn as_f(i: i64) -> f64 {
    i as f64
}

#[allow(clippy::cast_possible_truncation)]
fn round_int(x: f64) -> i64 {
    py_round0(x) as i64
}

/// `apply_to_book(book, action)`: adjust one symbol's open lots in place.
pub fn apply_to_book<L: BookLot>(book: &mut Vec<L>, action: &Action) {
    if !book.iter().any(|l| l.quantity() > 0) {
        return;
    }
    let m = action.multiple();
    if action.kind == BONUS {
        let held: i64 = book
            .iter()
            .filter(|l| l.quantity() > 0)
            .map(BookLot::quantity)
            .sum();
        let whole = round_int(as_f(held) * m);
        if whole > 0 {
            book.push(L::bonus_lot(&action.symbol, action.when(), whole));
        }
        return;
    }
    for l in book.iter_mut().filter(|l| l.quantity() > 0) {
        l.set_quantity(round_int(as_f(l.quantity()) * m));
        l.set_price(py_round(l.price() / m, 4));
    }
}

#[cfg(test)]
mod tests;
