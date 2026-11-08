//! `app/analytics/holdings_view.py` — the holdings table, and what can honestly be said about it.
//!
//! Every figure comes from Kite's holdings payload, the fills table, the uploaded scan or the GTT
//! book; where a field cannot be sourced it is **absent**, and [`GAPS`] names why. Quantities
//! arrive already summed (`quantity + t1_quantity + collateral_quantity`, the desk's `Kite.holdings`
//! wrapper); `pledged_qty` is reported as information and gates nothing.

use std::collections::HashMap;

use chrono::NaiveDateTime;

use super::{delta_days, local_from_ts, pyf, pyi, round_int, round_to};
use crate::lanes::weekly::basket::tradeable;
use crate::wire::{PyDict, PyValue, dict};

/// Kite reports market cap in Rs crore; the planner's large-cap line.
pub const LARGE_CAP_CR: f64 = 100_000.0;
/// The planner's mid-cap line.
pub const MID_CAP_CR: f64 = 25_000.0;
/// Equity LTCG threshold, days.
pub const LONG_TERM_DAYS: i64 = 365;
/// `app.config.MAX_SINGLE_WEIGHT` (a constant there, not an environment variable).
pub const MAX_SINGLE_WEIGHT: f64 = 15.0;

/// `GAPS`: what the page cannot show, and why.
pub const GAPS: &[(&str, &str)] = &[
    (
        "Sector allocation",
        "the scan carries no sector column; inferring one from a symbol would make the concentration warning fiction",
    ),
    (
        "Dividends and yield",
        "no dividend feed is wired, and kc.holdings() does not carry one",
    ),
    (
        "XIRR",
        "needs recorded cashflows; the cashflows table is empty",
    ),
    (
        "Factor exposure and correlations",
        "none of the inputs exist in this account",
    ),
];

/// One `fills` row as the holdings block reads it (`SELECT symbol, side, when_ts FROM fills`).
#[derive(Debug, Clone, PartialEq)]
pub struct FillRef {
    /// `symbol`.
    pub symbol: String,
    /// `side`.
    pub side: String,
    /// `when_ts` (seconds; `None` sorts as 0).
    pub when_ts: Option<f64>,
}

/// Why a table could not be built (Python raises; the page 500s).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ViewError(pub String);

/// The holding period from the fill history.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Lots {
    /// `first_bought` (ISO date).
    pub first_bought: String,
    /// `held_days`.
    pub held_days: i64,
    /// `term`: `long` | `short`.
    pub term: &'static str,
    /// `buy_fills`.
    pub buy_fills: i64,
}

/// One row of the holdings table.
#[derive(Debug, Clone, PartialEq)]
pub struct HoldingRow {
    /// `symbol`.
    pub symbol: String,
    /// `isin` (Kite's; `None` when absent).
    pub isin: PyValue,
    /// `exchange`.
    pub exchange: PyValue,
    /// `quantity` (already the total held).
    pub quantity: i64,
    /// `pledged_qty` — information only.
    pub pledged_qty: i64,
    /// `t1_quantity`.
    pub t1_quantity: i64,
    /// `avg_price`.
    pub avg_price: f64,
    /// `last_price`.
    pub last_price: f64,
    /// `invested`.
    pub invested: i64,
    /// `value`.
    pub value: i64,
    /// `pnl`.
    pub pnl: i64,
    /// `pnl_pct`.
    pub pnl_pct: Option<f64>,
    /// `day_change_pct`.
    pub day_change_pct: Option<f64>,
    /// `day_pnl`.
    pub day_pnl: Option<i64>,
    /// `marketcap_cr`.
    pub marketcap_cr: Option<i64>,
    /// `cap_band`.
    pub cap_band: Option<&'static str>,
    /// `beta` (the scan's, as given).
    pub beta: PyValue,
    /// `from_high_pct` (the scan's `away_from_high_one_year`, as given).
    pub from_high_pct: PyValue,
    /// `stop`: the protection review's kind, `ok`, or `unknown`.
    pub stop: String,
    /// The fill-history fields, when there is a buy on record.
    pub lots: Option<Lots>,
    /// `weight_pct`.
    pub weight_pct: f64,
}

fn opt_f(v: Option<f64>) -> PyValue {
    v.map_or(PyValue::None, PyValue::Float)
}

fn opt_i(v: Option<i64>) -> PyValue {
    v.map_or(PyValue::None, PyValue::Int)
}

impl HoldingRow {
    /// The row dict, in Python's key order.
    #[must_use]
    pub fn to_py(&self) -> PyDict {
        let mut d = dict([
            ("symbol", PyValue::Str(self.symbol.clone())),
            ("isin", self.isin.clone()),
            ("exchange", self.exchange.clone()),
            ("quantity", PyValue::Int(self.quantity)),
            ("pledged_qty", PyValue::Int(self.pledged_qty)),
            ("t1_quantity", PyValue::Int(self.t1_quantity)),
            ("avg_price", PyValue::Float(self.avg_price)),
            ("last_price", PyValue::Float(self.last_price)),
            ("invested", PyValue::Int(self.invested)),
            ("value", PyValue::Int(self.value)),
            ("pnl", PyValue::Int(self.pnl)),
            ("pnl_pct", opt_f(self.pnl_pct)),
            ("day_change_pct", opt_f(self.day_change_pct)),
            ("day_pnl", opt_i(self.day_pnl)),
            ("marketcap_cr", opt_i(self.marketcap_cr)),
            (
                "cap_band",
                self.cap_band
                    .map_or(PyValue::None, |b| PyValue::Str(b.to_owned())),
            ),
            ("beta", self.beta.clone()),
            ("from_high_pct", self.from_high_pct.clone()),
            ("stop", PyValue::Str(self.stop.clone())),
        ]);
        if let Some(l) = &self.lots {
            d.insert("first_bought", PyValue::Str(l.first_bought.clone()));
            d.insert("held_days", PyValue::Int(l.held_days));
            d.insert("term", PyValue::Str(l.term.to_owned()));
            d.insert("buy_fills", PyValue::Int(l.buy_fills));
        }
        d.insert("weight_pct", PyValue::Float(self.weight_pct));
        d
    }
}

fn band(marketcap_cr: Option<f64>) -> Option<&'static str> {
    let m = marketcap_cr?;
    if m >= LARGE_CAP_CR {
        return Some("largecap");
    }
    Some(if m >= MID_CAP_CR {
        "midcap"
    } else {
        "smallcap"
    })
}

/// `_lots`: first purchase and holding period, from the captured fill history.
fn lots(fills: &[FillRef], symbol: &str, now: NaiveDateTime) -> Result<Option<Lots>, ViewError> {
    let mut buys: Vec<&FillRef> = fills
        .iter()
        .filter(|f| f.symbol == symbol && f.side.to_uppercase() == "BUY")
        .collect();
    buys.sort_by(|a, b| {
        a.when_ts
            .unwrap_or(0.0)
            .partial_cmp(&b.when_ts.unwrap_or(0.0))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let Some(first) = buys.first() else {
        return Ok(None);
    };
    let ts = first.when_ts.ok_or_else(|| {
        ViewError(
            "TypeError: float() argument must be a string or a real number, not 'NoneType'".into(),
        )
    })?;
    let first =
        local_from_ts(ts).ok_or_else(|| ViewError(format!("timestamp out of range: {ts}")))?;
    let held = delta_days(now - first);
    Ok(Some(Lots {
        first_bought: first.date().format("%Y-%m-%d").to_string(),
        held_days: held,
        term: if held >= LONG_TERM_DAYS {
            "long"
        } else {
            "short"
        },
        buy_fills: i64::try_from(buys.len()).unwrap_or(i64::MAX),
    }))
}

fn round_or_err(x: f64) -> Result<i64, ViewError> {
    round_int(x).ok_or_else(|| ViewError("ValueError: cannot convert float NaN to integer".into()))
}

/// Inputs to [`rows`] beyond the holdings themselves.
#[derive(Debug, Clone, Default)]
pub struct Context<'a> {
    /// `raw`: `kc.holdings()` rows (`tradingsymbol`, `isin`, `day_change`, …).
    pub raw: &'a [PyDict],
    /// `scan`: the latest uploaded scan's rows by symbol.
    pub scan: Option<&'a HashMap<String, PyDict>>,
    /// `fills`: the fills table's `symbol, side, when_ts`.
    pub fills: &'a [FillRef],
    /// `protected`: symbol → the stop-cover kind.
    pub protected: Option<&'a HashMap<String, String>>,
}

/// `rows(holdings, raw=, scan=, fills=, protected=)`: one row per holding, largest first.
/// `now` is `datetime.now()` (naive local).
///
/// # Errors
/// What Python raises on a malformed input (a NaN figure, a buy fill without a time).
pub fn rows(
    holdings: &[PyDict],
    ctx: &Context<'_>,
    now: NaiveDateTime,
) -> Result<Vec<HoldingRow>, ViewError> {
    let mut by_symbol: HashMap<String, &PyDict> = HashMap::new();
    for r in ctx.raw {
        let key = match r.get("tradingsymbol") {
            Some(PyValue::Str(s)) => s.clone(),
            _ => continue,
        };
        by_symbol.insert(key, r);
    }
    let empty = PyDict::new();
    let mut out = Vec::new();
    for h in holdings {
        let sym = match h.get("symbol") {
            Some(PyValue::Str(s)) => s.clone(),
            Some(other) => other.py_str(),
            None => return Err(ViewError("KeyError: 'symbol'".into())),
        };
        let qty = pyi(h.get("quantity"));
        let avg = pyf(h.get("average_price"));
        let ltp = pyf(h.get("last_price"));
        let k = by_symbol.get(&sym).copied().unwrap_or(&empty);
        let s = ctx.scan.and_then(|m| m.get(&sym)).unwrap_or(&empty);

        #[allow(clippy::cast_precision_loss)]
        let qf = qty as f64;
        let invested = qf * avg;
        let value = qf * ltp;
        let pnl = value - invested;
        let present = |v: Option<&PyValue>| v.filter(|x| !matches!(x, PyValue::None)).cloned();
        let day_pct = present(k.get("day_change_percentage"));
        let day_share = present(k.get("day_change"));
        let mcap = present(s.get("marketcap"));
        let mcap_f = mcap.as_ref().map(|m| pyf(Some(m)));
        let exchange = h
            .get("exchange")
            .unwrap_or(&PyValue::None)
            .or(k.get("exchange").unwrap_or(&PyValue::None))
            .clone();

        out.push(HoldingRow {
            symbol: sym.clone(),
            isin: k.get("isin").cloned().unwrap_or(PyValue::None),
            exchange,
            quantity: qty,
            pledged_qty: pyi(h.get("pledged_qty")),
            t1_quantity: pyi(k.get("t1_quantity")),
            avg_price: round_to(avg, 2),
            last_price: round_to(ltp, 2),
            invested: round_or_err(invested)?,
            value: round_or_err(value)?,
            pnl: round_or_err(pnl)?,
            #[allow(clippy::float_cmp)]
            pnl_pct: (invested != 0.0).then(|| round_to(pnl / invested * 100.0, 2)),
            day_change_pct: day_pct.as_ref().map(|d| round_to(pyf(Some(d)), 2)),
            day_pnl: match &day_share {
                None => None,
                Some(d) => Some(round_or_err(pyf(Some(d)) * qf)?),
            },
            marketcap_cr: match mcap_f {
                None => None,
                Some(m) => Some(round_or_err(m)?),
            },
            cap_band: band(mcap_f),
            beta: s.get("beta").cloned().unwrap_or(PyValue::None),
            from_high_pct: s
                .get("away_from_high_one_year")
                .cloned()
                .unwrap_or(PyValue::None),
            stop: ctx
                .protected
                .and_then(|p| p.get(&sym))
                .cloned()
                .unwrap_or_else(|| "unknown".to_owned()),
            lots: lots(ctx.fills, &sym, now)?,
            weight_pct: 0.0,
        });
    }
    let sum: i64 = out.iter().map(|r| r.value).sum();
    #[allow(clippy::cast_precision_loss)]
    let total = if sum == 0 { 1.0 } else { sum as f64 };
    for r in &mut out {
        #[allow(clippy::cast_precision_loss)]
        let v = r.value as f64;
        r.weight_pct = round_to(v / total * 100.0, 2);
    }
    out.sort_by_key(|r| std::cmp::Reverse(r.value));
    Ok(out)
}

/// The portfolio totals ([`summary`]).
#[derive(Debug, Clone, PartialEq)]
pub struct Summary {
    /// The dict, in Python's key order.
    pub dict: PyDict,
    /// `top5_weight`, for [`warnings`].
    pub top5_weight: f64,
}

/// `summary(rows, cash=)`: portfolio totals. Percentages are of the equity book unless named.
///
/// # Errors
/// `ZeroDivisionError` when every beta-carrying row weighs zero (Python raises).
#[allow(clippy::cast_precision_loss)]
pub fn summary(rows_: &[HoldingRow], cash: f64) -> Result<Summary, ViewError> {
    let invested: i64 = rows_.iter().map(|r| r.invested).sum();
    let value: i64 = rows_.iter().map(|r| r.value).sum();
    let day: Vec<i64> = rows_.iter().filter_map(|r| r.day_pnl).collect();
    let day_sum: i64 = day.iter().sum();
    let nav = value as f64 + cash;
    let mut by_weight: Vec<&HoldingRow> = rows_.iter().collect();
    by_weight.sort_by(|a, b| {
        b.weight_pct
            .partial_cmp(&a.weight_pct)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let top5: f64 = by_weight
        .iter()
        .take(5)
        .fold(0.0, |acc, r| acc + r.weight_pct);
    let betas: Vec<(f64, f64)> = rows_
        .iter()
        .filter(|r| !matches!(r.beta, PyValue::None))
        .map(|r| (pyf(Some(&r.beta)), r.weight_pct))
        .collect();
    let wb = if betas.is_empty() {
        None
    } else {
        let num = betas.iter().fold(0.0, |acc, (b, w)| acc + b * w);
        let den = betas.iter().fold(0.0, |acc, (_, w)| acc + w);
        if den == 0.0 {
            return Err(ViewError(
                "ZeroDivisionError: float division by zero".into(),
            ));
        }
        Some(num / den)
    };
    let largest = rows_
        .iter()
        .fold(None::<&HoldingRow>, |best, r| match best {
            Some(b) if b.weight_pct >= r.weight_pct => Some(b),
            _ => Some(r),
        });
    let pnl_pct =
        (invested != 0).then(|| round_to((value - invested) as f64 / invested as f64 * 100.0, 2));
    let day_pnl_pct = (!day.is_empty() && value - day_sum != 0)
        .then(|| round_to(day_sum as f64 / (value - day_sum) as f64 * 100.0, 2));
    let top5_weight = round_to(top5, 1);
    let dict_ = dict([
        (
            "positions",
            PyValue::Int(i64::try_from(rows_.len()).unwrap_or(i64::MAX)),
        ),
        ("invested", PyValue::Int(invested)),
        ("value", PyValue::Int(value)),
        ("pnl", PyValue::Int(value - invested)),
        ("pnl_pct", opt_f(pnl_pct)),
        (
            "day_pnl",
            if day.is_empty() {
                PyValue::None
            } else {
                PyValue::Int(day_sum)
            },
        ),
        ("day_pnl_pct", opt_f(day_pnl_pct)),
        ("cash", PyValue::Int(round_or_err(cash)?)),
        ("nav", PyValue::Int(round_or_err(nav)?)),
        (
            "cash_pct",
            opt_f((nav != 0.0).then(|| round_to(cash / nav * 100.0, 2))),
        ),
        // `sum([])` is the int `0` and `round(0, 1)` keeps it an int: an empty book prints `0`.
        (
            "top5_weight",
            if rows_.is_empty() {
                PyValue::Int(0)
            } else {
                PyValue::Float(top5_weight)
            },
        ),
        (
            "largest",
            largest.map_or(PyValue::None, |r| PyValue::Str(r.symbol.clone())),
        ),
        (
            "largest_weight",
            largest.map_or(PyValue::None, |r| PyValue::Float(round_to(r.weight_pct, 2))),
        ),
        ("weighted_beta", opt_f(wb.map(|w| round_to(w, 2)))),
        (
            "pledged_positions",
            PyValue::Int(
                i64::try_from(rows_.iter().filter(|r| r.pledged_qty > 0).count()).unwrap_or(0),
            ),
        ),
    ]);
    Ok(Summary {
        dict: dict_,
        top5_weight,
    })
}

/// `allocation(rows, cash=)`: by market-cap band, plus the cash sleeve. Unbanded names are
/// `unclassified`, never folded into a band they were not classified into.
///
/// # Errors
/// A non-finite total (Python's `round` raises).
#[allow(clippy::cast_precision_loss)]
pub fn allocation(rows_: &[HoldingRow], cash: f64) -> Result<PyDict, ViewError> {
    let mut buckets: Vec<(String, f64)> = Vec::new();
    for r in rows_ {
        let key = r.cap_band.unwrap_or("unclassified");
        if let Some(slot) = buckets.iter_mut().find(|(b, _)| b == key) {
            slot.1 += r.value as f64;
        } else {
            buckets.push((key.to_owned(), 0.0 + r.value as f64));
        }
    }
    let raw_total = buckets.iter().fold(0.0, |acc, (_, v)| acc + v) + cash;
    let total = if raw_total == 0.0 { 1.0 } else { raw_total };
    let mut sorted = buckets.clone();
    sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    let mut out = Vec::new();
    for (b, v) in sorted {
        out.push(PyValue::Map(dict([
            ("band", PyValue::Str(b)),
            ("value", PyValue::Int(round_or_err(v)?)),
            ("pct", PyValue::Float(round_to(v / total * 100.0, 1))),
        ])));
    }
    if cash != 0.0 {
        out.push(PyValue::Map(dict([
            ("band", PyValue::Str("cash".into())),
            ("value", PyValue::Int(round_or_err(cash)?)),
            ("pct", PyValue::Float(round_to(cash / total * 100.0, 1))),
        ])));
    }
    Ok(dict([
        ("rows", PyValue::List(out)),
        ("total", PyValue::Int(round_or_err(total)?)),
    ]))
}

fn warn(level: &str, symbol: Option<&str>, text: String) -> PyDict {
    dict([
        ("level", PyValue::Str(level.to_owned())),
        (
            "symbol",
            symbol.map_or(PyValue::None, |s| PyValue::Str(s.to_owned())),
        ),
        ("text", PyValue::Str(text)),
    ])
}

/// `warnings(rows, summary)`: only conditions the data can actually establish. An untouchable
/// instrument is held on purpose and is never measured against the position cap.
#[must_use]
pub fn warnings(rows_: &[HoldingRow], summary_: &Summary) -> Vec<PyDict> {
    let mut out = Vec::new();
    let cap = MAX_SINGLE_WEIGHT;
    for r in rows_ {
        if !tradeable(&r.symbol) {
            continue;
        }
        if r.weight_pct > cap {
            out.push(warn(
                "warn",
                Some(&r.symbol),
                format!(
                    "{:.1}% of the book, over the {cap:.0}% single-position cap",
                    r.weight_pct
                ),
            ));
        }
    }
    let unprotected: Vec<&str> = rows_
        .iter()
        .filter(|r| r.stop == "missing" || r.stop == "partial")
        .map(|r| r.symbol.as_str())
        .collect();
    if !unprotected.is_empty() {
        out.push(warn(
            "bad",
            None,
            format!(
                "{} position(s) without full stop cover: {}",
                unprotected.len(),
                unprotected
                    .iter()
                    .take(6)
                    .copied()
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        ));
    }
    for r in rows_ {
        let p = r.pnl_pct.unwrap_or(0.0);
        if p <= -20.0 && tradeable(&r.symbol) {
            out.push(warn(
                "warn",
                Some(&r.symbol),
                format!("down {p:.1}% on cost"),
            ));
        }
    }
    if summary_.top5_weight > 60.0 {
        out.push(warn(
            "warn",
            None,
            format!(
                "top five holdings are {:.0}% of the book",
                summary_.top5_weight
            ),
        ));
    }
    out
}

/// `GAPS` as the page's list of `(name, why)` pairs.
#[must_use]
pub fn gaps_py() -> PyValue {
    PyValue::List(
        GAPS.iter()
            .map(|(n, w)| PyValue::List(vec![PyValue::Str((*n).into()), PyValue::Str((*w).into())]))
            .collect(),
    )
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
mod tests {
    //! `tests/test_holdings_view.py`, ported (14 cases with the parametrisation).
    use super::*;
    use crate::lanes::weekly::weekly_books::local_ts;
    use chrono::{Duration, NaiveDate};

    fn now() -> NaiveDateTime {
        NaiveDate::from_ymd_opt(2026, 10, 30)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap()
    }

    fn held(sym: &str, qty: i64, avg: f64, ltp: f64) -> PyDict {
        dict([
            ("symbol", PyValue::Str(sym.into())),
            ("quantity", PyValue::Int(qty)),
            ("average_price", PyValue::Float(avg)),
            ("last_price", PyValue::Float(ltp)),
            ("pledged_qty", PyValue::Int(0)),
            ("exchange", PyValue::Str("NSE".into())),
        ])
    }

    fn kite_row(sym: &str, day_change: f64, day_pct: f64) -> PyDict {
        dict([
            ("tradingsymbol", PyValue::Str(sym.into())),
            ("day_change", PyValue::Float(day_change)),
            ("day_change_percentage", PyValue::Float(day_pct)),
            ("t1_quantity", PyValue::Int(0)),
            ("isin", PyValue::Str(format!("IN{sym}"))),
        ])
    }

    fn run(h: &[PyDict], raw: &[PyDict]) -> Vec<HoldingRow> {
        rows(
            h,
            &Context {
                raw,
                ..Context::default()
            },
            now(),
        )
        .unwrap()
    }

    #[test]
    fn a_row_carries_cost_value_and_the_gap_between_them() {
        let r = &run(
            &[held("AAA", 100, 90.0, 100.0)],
            &[kite_row("AAA", 0.0, 0.0)],
        )[0];
        assert_eq!((r.invested, r.value, r.pnl), (9000, 10000, 1000));
        assert!((r.pnl_pct.unwrap() - 11.11).abs() <= 0.01);
    }

    #[test]
    fn day_pnl_uses_kites_own_day_change_not_a_stored_close() {
        let r = &run(
            &[held("AAA", 100, 90.0, 100.0)],
            &[kite_row("AAA", -2.5, -2.44)],
        )[0];
        assert_eq!(r.day_pnl, Some(-250));
        assert_eq!(r.day_change_pct, Some(-2.44));
    }

    #[test]
    fn weights_are_of_the_equity_book_and_sum_to_a_hundred() {
        let rs = run(
            &[held("AAA", 100, 10.0, 10.0), held("BBB", 100, 10.0, 30.0)],
            &[kite_row("AAA", 0.0, 0.0), kite_row("BBB", 0.0, 0.0)],
        );
        let total: f64 = rs.iter().map(|r| r.weight_pct).sum();
        assert!((total - 100.0).abs() <= 0.05);
        assert_eq!(rs[0].symbol, "BBB", "rows lead with the largest position");
    }

    #[test]
    fn a_missing_day_figure_is_absent_not_zero() {
        let raw = [dict([("tradingsymbol", PyValue::Str("AAA".into()))])];
        let r = &run(&[held("AAA", 10, 10.0, 10.0)], &raw)[0];
        assert_eq!(r.day_pnl, None);
        assert_eq!(r.day_change_pct, None);
    }

    #[test]
    fn a_name_absent_from_the_scan_is_unclassified_not_guessed() {
        let scan = HashMap::new();
        let raw = [kite_row("AAA", 0.0, 0.0)];
        let ctx = Context {
            raw: &raw,
            scan: Some(&scan),
            ..Context::default()
        };
        let r = &rows(&[held("AAA", 10, 10.0, 10.0)], &ctx, now()).unwrap()[0];
        assert_eq!(r.cap_band, None);
        assert_eq!(r.marketcap_cr, None);
        assert_eq!(r.beta, PyValue::None);
    }

    #[test]
    fn bands_match_the_ones_the_planner_uses() {
        for (cr, want) in [
            (150_000, "largecap"),
            (40_000, "midcap"),
            (5_000, "smallcap"),
        ] {
            let mut scan = HashMap::new();
            scan.insert("AAA".to_owned(), dict([("marketcap", PyValue::Int(cr))]));
            let raw = [kite_row("AAA", 0.0, 0.0)];
            let ctx = Context {
                raw: &raw,
                scan: Some(&scan),
                ..Context::default()
            };
            let r = &rows(&[held("AAA", 10, 10.0, 10.0)], &ctx, now()).unwrap()[0];
            assert_eq!(r.cap_band, Some(want));
        }
    }

    fn with_fill(days_ago: i64) -> HoldingRow {
        let fills = [FillRef {
            symbol: "AAA".into(),
            side: "BUY".into(),
            when_ts: Some(local_ts(now() - Duration::days(days_ago))),
        }];
        let raw = [kite_row("AAA", 0.0, 0.0)];
        let ctx = Context {
            raw: &raw,
            fills: &fills,
            ..Context::default()
        };
        rows(&[held("AAA", 10, 10.0, 10.0)], &ctx, now())
            .unwrap()
            .remove(0)
    }

    #[test]
    fn holding_period_comes_from_the_fill_history() {
        let r = with_fill(400);
        let lots = r.lots.unwrap();
        assert_eq!(lots.term, "long");
        assert!(lots.held_days >= 400);
    }

    #[test]
    fn a_recent_buy_is_short_term() {
        assert_eq!(with_fill(30).lots.unwrap().term, "short");
    }

    #[test]
    fn no_fill_history_means_no_claim_about_the_holding_period() {
        let r = &run(&[held("AAA", 10, 10.0, 10.0)], &[kite_row("AAA", 0.0, 0.0)])[0];
        let d = r.to_py();
        assert!(d.get("term").is_none() && d.get("held_days").is_none());
    }

    #[test]
    fn a_position_over_the_cap_is_flagged() {
        let rs = run(
            &[held("AAA", 100, 10.0, 10.0), held("BBB", 10, 10.0, 10.0)],
            &[kite_row("AAA", 0.0, 0.0), kite_row("BBB", 0.0, 0.0)],
        );
        let w = warnings(&rs, &summary(&rs, 0.0).unwrap());
        assert!(
            w.iter()
                .any(|x| x.get("symbol") == Some(&PyValue::Str("AAA".into()))
                    && x.get("text").unwrap().py_str().contains("cap"))
        );
    }

    #[test]
    fn an_untouchable_instrument_is_not_measured_against_the_cap() {
        let rs = run(
            &[
                held("SGBDE31III-GB", 100, 10.0, 10.0),
                held("BBB", 1, 10.0, 10.0),
            ],
            &[
                kite_row("SGBDE31III-GB", 0.0, 0.0),
                kite_row("BBB", 0.0, 0.0),
            ],
        );
        assert!(rs[0].weight_pct > 90.0);
        let w = warnings(&rs, &summary(&rs, 0.0).unwrap());
        assert!(w.iter().all(|x| x.get("symbol") == Some(&PyValue::None)));
    }

    #[test]
    fn positions_without_full_stop_cover_are_named() {
        let mut protected = HashMap::new();
        protected.insert("AAA".to_owned(), "missing".to_owned());
        let raw = [kite_row("AAA", 0.0, 0.0)];
        let ctx = Context {
            raw: &raw,
            protected: Some(&protected),
            ..Context::default()
        };
        let rs = rows(&[held("AAA", 10, 10.0, 10.0)], &ctx, now()).unwrap();
        let w = warnings(&rs, &summary(&rs, 0.0).unwrap());
        assert!(w.iter().any(|x| {
            x.get("text")
                .unwrap()
                .py_str()
                .contains("without full stop cover")
        }));
    }

    #[test]
    fn the_summary_reports_concentration_and_weighted_beta() {
        let mut scan = HashMap::new();
        scan.insert("AAA".to_owned(), dict([("beta", PyValue::Float(1.0))]));
        scan.insert("BBB".to_owned(), dict([("beta", PyValue::Float(2.0))]));
        let raw = [kite_row("AAA", 0.0, 0.0), kite_row("BBB", 0.0, 0.0)];
        let ctx = Context {
            raw: &raw,
            scan: Some(&scan),
            ..Context::default()
        };
        let rs = rows(
            &[held("AAA", 100, 10.0, 10.0), held("BBB", 100, 10.0, 30.0)],
            &ctx,
            now(),
        )
        .unwrap();
        let s = summary(&rs, 1000.0).unwrap();
        assert!((s.top5_weight - 100.0).abs() <= 0.05);
        assert_eq!(s.dict.get("largest"), Some(&PyValue::Str("BBB".into())));
        assert!((pyf(s.dict.get("weighted_beta")) - 1.75).abs() <= 0.01);
        assert_eq!(s.dict.get("cash"), Some(&PyValue::Int(1000)));
    }

    #[test]
    fn every_gap_says_why_it_cannot_be_shown() {
        assert!(!GAPS.is_empty());
        for (name, why) in GAPS {
            assert!(!name.is_empty() && why.len() > 20, "{name} has no reason");
        }
        let named: Vec<&str> = GAPS.iter().map(|(n, _)| *n).collect();
        assert!(named.contains(&"Sector allocation") && named.contains(&"XIRR"));
    }

    #[test]
    fn pledged_shares_are_information_only() {
        // Non-negotiable 3: a pledged holding is counted, valued and weighted like any other;
        // the only trace is the info flag.
        let mut h = held("AAA", 100, 10.0, 10.0);
        h.insert("pledged_qty", PyValue::Int(100));
        let rs = run(&[h, held("BBB", 100, 10.0, 10.0)], &[]);
        assert_eq!(rs[0].quantity, 100);
        assert_eq!(rs[0].value, 1000);
        assert_eq!(rs[0].weight_pct, 50.0);
        assert_eq!(
            summary(&rs, 0.0).unwrap().dict.get("pledged_positions"),
            Some(&PyValue::Int(1))
        );
    }
}
