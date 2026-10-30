//! `app/analytics/snapshot.py` — the daily EOD snapshot's **collection** (`SnapshotJob.collect`).
//!
//! Reads only; places no orders. Holdings arrive from the desk's `Kite.holdings` wrapper, which
//! already sums `quantity + t1_quantity + collateral_quantity` — pledged shares count as held and
//! `pledged_qty` is carried as information. Untouchable instruments (SGB/G-sec) are classified by
//! the one guard rule, recorded in `holdings_json` and excluded from NAV unless
//! `INCLUDE_UNTOUCHABLE_IN_NAV`.
//!
//! **Not here (loud):** `run()` persists through `analytics/db.py`'s `save_snapshot` (upsert +
//! `rechain_index`), which lane `analytics` owns; the CLI's cashflow bookkeeping likewise. The rate
//! limiter slot Python takes before each call is the [`SnapshotKite`] implementor's (the desk's
//! Kite wrapper, lane `exec_core`).

use std::path::Path;

use chrono::NaiveDate;

use super::{pyf, pyi, round_to};
use crate::lanes::weekly::basket::tradeable;
use crate::wire::{PyDict, PyValue, dict};

/// `INSTRUMENT_CACHE`.
pub const INSTRUMENT_CACHE: &str = "data/.instruments_{exchange}.json";

/// The reads the job makes (`Kite.is_authed`, `holdings`, `available_cash`, `quote_raw`,
/// `instruments`, `historical`).
#[async_trait::async_trait]
pub trait SnapshotKite: Send + Sync {
    /// `is_authed()`.
    async fn is_authed(&self) -> bool;
    /// `holdings()` — the desk's summed rows (`symbol`, `exchange`, `quantity`, `pledged_qty`,
    /// `average_price`, `last_price`).
    async fn holdings(&self) -> Result<Vec<PyDict>, String>;
    /// `available_cash()`.
    async fn available_cash(&self) -> Result<f64, String>;
    /// `quote_raw(keys)`.
    async fn quote_raw(&self, keys: &[String]) -> Result<PyDict, String>;
    /// `instruments(exchange)`.
    async fn instruments(&self, exchange: &str) -> Result<Vec<PyValue>, String>;
    /// `historical(token, day, day, "day")`.
    async fn historical(&self, token: i64, day: NaiveDate) -> Result<Vec<PyValue>, String>;
}

/// Why a snapshot was not taken.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SnapshotError {
    /// Python's `ValueError` (nothing is written).
    #[error("{0}")]
    Value(String),
    /// Python's `RuntimeError` (no session).
    #[error("{0}")]
    Runtime(String),
    /// A Kite read failed.
    #[error("{0}")]
    Kite(String),
}

/// `is_untouchable(symbol)`: the guard's rule, not a second list.
#[must_use]
pub fn is_untouchable(symbol: &str) -> bool {
    !tradeable(symbol)
}

/// The collected snapshot (`collect()`'s dict).
#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    /// `date`.
    pub date: String,
    /// `nav`.
    pub nav: f64,
    /// `invested`.
    pub invested: f64,
    /// `cash`.
    pub cash: f64,
    /// `holdings_json` (compact `json.dumps`).
    pub holdings_json: String,
}

/// Options for [`collect`].
#[derive(Debug, Clone)]
pub struct CollectOptions<'a> {
    /// `on_date` (default today).
    pub on_date: Option<&'a str>,
    /// `holdings` | `quote` | `historical`.
    pub price_source: &'a str,
    /// The cash balance on `on_date`, when backfilling.
    pub cash: Option<f64>,
    /// Accept today's cash for a past date.
    pub allow_stale_cash: bool,
    /// `date.today()`.
    pub today: NaiveDate,
    /// `config.INCLUDE_UNTOUCHABLE_IN_NAV`.
    pub include_untouchable_in_nav: bool,
    /// The desk's run directory (`data/` lives under it).
    pub root: &'a Path,
}

fn num_or(v: Option<&PyValue>) -> f64 {
    pyf(v)
}

/// Compact `json.dumps(value, separators=(",", ":"))` (Python's defaults otherwise).
fn json_compact(v: &PyValue, out: &mut String) -> Result<(), SnapshotError> {
    use baskfy_providers::py::{json_str, repr_float};
    match v {
        PyValue::None => out.push_str("null"),
        PyValue::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        PyValue::Int(i) => out.push_str(&i.to_string()),
        PyValue::Float(f) => {
            if !f.is_finite() {
                return Err(SnapshotError::Value(
                    "Out of range float values are not JSON compliant".into(),
                ));
            }
            out.push_str(&repr_float(*f));
        }
        PyValue::Str(s) => out.push_str(&json_str(s)),
        PyValue::List(items) => {
            out.push('[');
            for (i, x) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                json_compact(x, out)?;
            }
            out.push(']');
        }
        PyValue::Map(m) => {
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&json_str(k));
                out.push(':');
                json_compact(x, out)?;
            }
            out.push('}');
        }
        other => {
            return Err(SnapshotError::Value(format!(
                "Object of type {} is not JSON serializable",
                other.py_repr()
            )));
        }
    }
    Ok(())
}

async fn instrument_tokens(
    kite: &dyn SnapshotKite,
    symbols: &[String],
    root: &Path,
    today: NaiveDate,
) -> Result<Vec<(String, i64)>, SnapshotError> {
    let path = root.join(INSTRUMENT_CACHE.replace("{exchange}", "NSE"));
    let today_iso = today.format("%Y-%m-%d").to_string();
    let mut rows: Option<Vec<PyValue>> = None;
    if let Ok(bytes) = std::fs::read(&path)
        && let Ok(PyValue::Map(blob)) = PyValue::from_json(&bytes)
        && blob.get("fetched") == Some(&PyValue::Str(today_iso.clone()))
        && let Some(PyValue::List(r)) = blob.get("rows")
    {
        rows = Some(r.clone());
    }
    let rows = match rows {
        Some(r) => r,
        None => {
            let dump = kite.instruments("NSE").await.map_err(SnapshotError::Kite)?;
            let rows: Vec<PyValue> = dump
                .iter()
                .filter_map(|r| match r {
                    PyValue::Map(m) => Some(PyValue::Map(dict([
                        (
                            "tradingsymbol",
                            m.get("tradingsymbol").cloned().unwrap_or(PyValue::None),
                        ),
                        (
                            "instrument_token",
                            m.get("instrument_token").cloned().unwrap_or(PyValue::None),
                        ),
                        (
                            "segment",
                            m.get("segment")
                                .cloned()
                                .unwrap_or(PyValue::Str(String::new())),
                        ),
                        (
                            "name",
                            m.get("name")
                                .cloned()
                                .unwrap_or(PyValue::Str(String::new())),
                        ),
                    ]))),
                    _ => None,
                })
                .collect();
            let blob = PyValue::Map(dict([
                ("fetched", PyValue::Str(today_iso)),
                ("rows", PyValue::List(rows.clone())),
            ]));
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).map_err(|e| SnapshotError::Kite(e.to_string()))?;
            }
            let text = baskfy_providers::kite::py_json_dumps(&blob)
                .map_err(|e| SnapshotError::Value(e.to_string()))?;
            std::fs::write(&path, text).map_err(|e| SnapshotError::Kite(e.to_string()))?;
            rows
        }
    };
    let mut out = Vec::new();
    for r in rows {
        let PyValue::Map(m) = r else { continue };
        let sym = m
            .get("tradingsymbol")
            .map_or_else(String::new, PyValue::py_str);
        if symbols.contains(&sym) {
            let tok = pyi(m.get("instrument_token"));
            if let Some(slot) = out.iter_mut().find(|(s, _): &&mut (String, i64)| *s == sym) {
                slot.1 = tok;
            } else {
                out.push((sym, tok));
            }
        }
    }
    Ok(out)
}

async fn prices(
    kite: &dyn SnapshotKite,
    holdings: &[PyDict],
    on_date: &str,
    source: &str,
    opts: &CollectOptions<'_>,
) -> Result<Vec<(String, f64)>, SnapshotError> {
    let sym = |h: &PyDict| h.get("symbol").map_or_else(String::new, PyValue::py_str);
    match source {
        "holdings" => Ok(holdings
            .iter()
            .map(|h| (sym(h), num_or(h.get("last_price"))))
            .collect()),
        "quote" => {
            let keys: Vec<String> = holdings
                .iter()
                .map(|h| {
                    format!(
                        "{}:{}",
                        h.get("exchange").map_or_else(String::new, PyValue::py_str),
                        sym(h)
                    )
                })
                .collect();
            let mut out = Vec::new();
            for chunk in keys.chunks(200) {
                let got = kite.quote_raw(chunk).await.map_err(SnapshotError::Kite)?;
                for (k, v) in got.iter() {
                    let PyValue::Map(v) = v else { continue };
                    let close = match v.get("ohlc") {
                        Some(PyValue::Map(o)) => o.get("close").cloned(),
                        _ => None,
                    };
                    let px = match close {
                        Some(c) if c.truthy() => pyf(Some(&c)),
                        _ => pyf(v.get("last_price")),
                    };
                    let name = k.split_once(':').map_or(k, |(_, s)| s).to_owned();
                    out.push((name, px));
                }
            }
            Ok(out)
        }
        "historical" => {
            let symbols: Vec<String> = holdings.iter().map(sym).collect();
            let tokens = instrument_tokens(kite, &symbols, opts.root, opts.today).await?;
            let day = NaiveDate::parse_from_str(on_date, "%Y-%m-%d").map_err(|_| {
                SnapshotError::Value(format!("Invalid isoformat string: {on_date:?}"))
            })?;
            let mut out: Vec<(String, f64)> = Vec::new();
            let mut missing = Vec::new();
            for s in &symbols {
                let tok = tokens.iter().find(|(t, _)| t == s).map_or(0, |(_, k)| *k);
                if tok == 0 {
                    missing.push(s.clone());
                    continue;
                }
                let candles = kite
                    .historical(tok, day)
                    .await
                    .map_err(SnapshotError::Kite)?;
                match candles.last() {
                    Some(PyValue::Map(c)) => out.push((s.clone(), pyf(c.get("close")))),
                    _ => missing.push(s.clone()),
                }
            }
            if out.is_empty() {
                return Err(SnapshotError::Value(format!(
                    "no daily candles for any holding on {on_date} — market holiday or weekend? \
                     Nothing was written."
                )));
            }
            if !missing.is_empty() {
                for h in holdings {
                    let s = sym(h);
                    if !out.iter().any(|(k, _)| *k == s) {
                        out.push((s, num_or(h.get("last_price"))));
                    }
                }
            }
            Ok(out)
        }
        other => Err(SnapshotError::Value(format!(
            "unknown price source: {}",
            baskfy_providers::py::repr_str(other)
        ))),
    }
}

/// `SnapshotJob.collect(on_date, price_source, cash=, allow_stale_cash=)`.
///
/// # Errors
/// No session (`Runtime`), a backfill without cash, a holiday, an unknown source (`Value`), or a
/// failed read (`Kite`).
#[allow(clippy::cast_precision_loss)]
pub async fn collect(
    kite: &dyn SnapshotKite,
    opts: &CollectOptions<'_>,
) -> Result<Snapshot, SnapshotError> {
    if !kite.is_authed().await {
        return Err(SnapshotError::Runtime(
            "Kite session expired — log in at / first (token is daily).".into(),
        ));
    }
    let today = opts.today.format("%Y-%m-%d").to_string();
    let date = opts.on_date.map_or_else(|| today.clone(), str::to_owned);
    let holdings = kite.holdings().await.map_err(SnapshotError::Kite)?;
    let cash = match opts.cash {
        Some(c) => c,
        None => {
            if date < today && !opts.allow_stale_cash {
                return Err(SnapshotError::Value(format!(
                    "backfilling {date}: kc.margins() only knows today's cash. Pass cash=<balance \
                     on {date}> (CLI: --cash), or allow_stale_cash=True (CLI: \
                     --assume-current-cash) if you accept today's balance."
                )));
            }
            kite.available_cash().await.map_err(SnapshotError::Kite)?
        }
    };
    let backfilled = date < today;
    let px = prices(kite, &holdings, &date, opts.price_source, opts).await?;

    let mut positions: Vec<(f64, PyValue)> = Vec::new();
    let (mut invested, mut excluded_value) = (0.0, 0.0);
    for h in &holdings {
        let sym = h.get("symbol").map_or_else(String::new, PyValue::py_str);
        let price = px
            .iter()
            .rev()
            .find(|(s, _)| *s == sym)
            .map_or_else(|| num_or(h.get("last_price")), |(_, p)| *p);
        let qty = pyi(h.get("quantity"));
        let value = qty as f64 * price;
        let excluded = is_untouchable(&sym);
        positions.push((
            round_to(value, 2),
            PyValue::Map(dict([
                ("symbol", PyValue::Str(sym)),
                (
                    "exchange",
                    h.get("exchange").cloned().unwrap_or(PyValue::None),
                ),
                (
                    "quantity",
                    h.get("quantity").cloned().unwrap_or(PyValue::None),
                ),
                (
                    "pledged_qty",
                    h.get("pledged_qty").cloned().unwrap_or(PyValue::None),
                ),
                (
                    "average_price",
                    PyValue::Float(round_to(num_or(h.get("average_price")), 4)),
                ),
                ("price", PyValue::Float(round_to(price, 4))),
                ("value", PyValue::Float(round_to(value, 2))),
                ("excluded", PyValue::Bool(excluded)),
            ])),
        ));
        if excluded && !opts.include_untouchable_in_nav {
            excluded_value += value;
        } else {
            invested += value;
        }
    }
    positions.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    let holdings_json = PyValue::Map(dict([
        ("as_of", PyValue::Str(date.clone())),
        ("price_source", PyValue::Str(opts.price_source.to_owned())),
        (
            "include_untouchable_in_nav",
            PyValue::Bool(opts.include_untouchable_in_nav),
        ),
        (
            "excluded_value",
            PyValue::Float(round_to(excluded_value, 2)),
        ),
        ("backfilled", PyValue::Bool(backfilled)),
        (
            "positions",
            PyValue::List(positions.into_iter().map(|(_, p)| p).collect()),
        ),
    ]));
    let mut text = String::new();
    json_compact(&holdings_json, &mut text)?;
    Ok(Snapshot {
        date,
        nav: round_to(invested + cash, 2),
        invested: round_to(invested, 2),
        cash: round_to(cash, 2),
        holdings_json: text,
    })
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
mod tests {
    use super::*;

    struct Fake {
        authed: bool,
        holdings: Vec<PyDict>,
    }

    #[async_trait::async_trait]
    impl SnapshotKite for Fake {
        async fn is_authed(&self) -> bool {
            self.authed
        }
        async fn holdings(&self) -> Result<Vec<PyDict>, String> {
            Ok(self.holdings.clone())
        }
        async fn available_cash(&self) -> Result<f64, String> {
            Ok(1000.0)
        }
        async fn quote_raw(&self, _keys: &[String]) -> Result<PyDict, String> {
            Ok(PyDict::new())
        }
        async fn instruments(&self, _exchange: &str) -> Result<Vec<PyValue>, String> {
            Ok(Vec::new())
        }
        async fn historical(&self, _token: i64, _day: NaiveDate) -> Result<Vec<PyValue>, String> {
            Ok(Vec::new())
        }
    }

    fn h(sym: &str, qty: i64, ltp: f64, pledged: i64) -> PyDict {
        dict([
            ("symbol", PyValue::Str(sym.into())),
            ("exchange", PyValue::Str("NSE".into())),
            ("quantity", PyValue::Int(qty)),
            ("pledged_qty", PyValue::Int(pledged)),
            ("average_price", PyValue::Float(10.0)),
            ("last_price", PyValue::Float(ltp)),
        ])
    }

    fn opts(root: &Path) -> CollectOptions<'_> {
        CollectOptions {
            on_date: None,
            price_source: "holdings",
            cash: None,
            allow_stale_cash: false,
            today: NaiveDate::from_ymd_opt(2026, 8, 14).unwrap(),
            include_untouchable_in_nav: false,
            root,
        }
    }

    #[tokio::test]
    async fn untouchables_are_recorded_but_kept_out_of_nav_and_pledged_shares_count() {
        let dir = tempfile::tempdir().unwrap();
        let kite = Fake {
            authed: true,
            holdings: vec![h("AAA", 10, 100.0, 10), h("SGBDE31III", 5, 6000.0, 0)],
        };
        let s = collect(&kite, &opts(dir.path())).await.unwrap();
        assert_eq!(s.invested, 1000.0);
        assert_eq!(s.nav, 2000.0);
        assert!(s.holdings_json.starts_with("{\"as_of\":\"2026-08-14\",\"price_source\":\"holdings\",\"include_untouchable_in_nav\":false,\"excluded_value\":30000.0,\"backfilled\":false,\"positions\":[{\"symbol\":\"SGBDE31III\""));
        assert!(s.holdings_json.contains("\"pledged_qty\":10"));
    }

    #[tokio::test]
    async fn a_backfill_without_cash_is_refused_and_no_session_is_a_runtime_error() {
        let dir = tempfile::tempdir().unwrap();
        let kite = Fake {
            authed: true,
            holdings: vec![],
        };
        let mut o = opts(dir.path());
        o.on_date = Some("2026-08-11");
        assert!(
            matches!(collect(&kite, &o).await, Err(SnapshotError::Value(m)) if m.contains("only knows today's cash"))
        );
        let off = Fake {
            authed: false,
            holdings: vec![],
        };
        assert!(matches!(
            collect(&off, &opts(dir.path())).await,
            Err(SnapshotError::Runtime(_))
        ));
    }

    #[tokio::test]
    async fn a_holiday_writes_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let kite = Fake {
            authed: true,
            holdings: vec![h("AAA", 10, 100.0, 0)],
        };
        let mut o = opts(dir.path());
        o.price_source = "historical";
        o.cash = Some(0.0);
        assert!(
            matches!(collect(&kite, &o).await, Err(SnapshotError::Value(m)) if m.contains("market holiday"))
        );
    }
}
