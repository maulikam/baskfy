//! `app/analytics/protection.py` — does every position actually have a working stop?
//!
//! READ-ONLY, as the Python is: it compares holdings against GTTs and proposes a plan. It
//! never places, modifies or cancels a trigger (the module holds no gateway and no Kite
//! write; `tests::the_module_never_places_or_cancels_a_stop` scans this source the way
//! `test_protection.py` scans the Python).
//!
//! Inputs are the Kite-shaped values Python passes (`kite_client.holdings()` rows, raw GTT
//! dicts), so the dict shapes and key orders of the output are Python's.

use crate::lanes::exec_core::gtt::{py_round, py_round0};
use crate::lanes::exec_core::guards::assert_tradeable;
use crate::lanes::exec_core::stops::{StopConfig, stop_from_vol};
use crate::wire::{PyDict, PyValue, dict};

/// No active stop at all.
pub const MISSING: &str = "missing";
/// A stop, but for fewer shares than are held.
pub const PARTIAL: &str = "partial";
/// Further below than the configured band allows.
pub const TOO_FAR: &str = "too_far";
/// Tight enough to be noise-triggered.
pub const TOO_CLOSE: &str = "too_close";
/// A trigger with no matching holding.
pub const ORPHAN: &str = "orphan";
/// Triggers covering MORE shares than are held.
pub const EXCESS: &str = "excess";

/// `SEVERITY`, worst first, in Python's dict order.
pub const SEVERITY: [(&str, i64); 6] = [
    (MISSING, 0),
    (EXCESS, 1),
    (PARTIAL, 2),
    (ORPHAN, 3),
    (TOO_FAR, 4),
    (TOO_CLOSE, 5),
];

/// `DEFAULT_VOL`: the fallback annual volatility a stop is sized from.
pub const DEFAULT_VOL: f64 = 0.36;

const ACTIVE: &str = "active";

fn severity(kind: &str) -> i64 {
    SEVERITY
        .iter()
        .find(|(k, _)| *k == kind)
        .map_or(9, |(_, s)| *s)
}

fn field<'a>(m: &'a PyValue, key: &str) -> Option<&'a PyValue> {
    match m {
        PyValue::Map(map) => map.get(key),
        _ => None,
    }
}

/// `float(x or 0.0)` for the numeric shapes Kite returns.
fn as_float(v: Option<&PyValue>) -> f64 {
    match v {
        Some(PyValue::Float(f)) => *f,
        // i64 → f64 is Python's `float(int)` for every quantity a book holds.
        #[allow(clippy::cast_precision_loss)]
        Some(PyValue::Int(i)) => *i as f64,
        Some(PyValue::Bool(b)) => f64::from(u8::from(*b)),
        Some(PyValue::Str(s)) => crate::settings::py_float(s).unwrap_or(0.0),
        _ => 0.0,
    }
}

/// `int(x or 0)`.
fn as_int(v: Option<&PyValue>) -> i64 {
    match v {
        Some(PyValue::Int(i)) => *i,
        // `int(float)` truncates toward zero.
        #[allow(clippy::cast_possible_truncation)]
        Some(PyValue::Float(f)) => f.trunc() as i64,
        Some(PyValue::Bool(b)) => i64::from(*b),
        Some(PyValue::Str(s)) => crate::settings::py_int(s).unwrap_or(0),
        _ => 0,
    }
}

fn as_text(v: Option<&PyValue>) -> String {
    match v {
        Some(x) if x.truthy() => x.py_str(),
        _ => String::new(),
    }
}

fn items(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(xs) => xs,
        _ => &[],
    }
}

/// `_sym(gtt)`.
fn gtt_symbol(g: &PyValue) -> String {
    as_text(field(g, "condition").and_then(|c| field(c, "tradingsymbol")))
}

/// `_trigger(gtt)`: the first trigger value, if any.
fn gtt_trigger(g: &PyValue) -> Option<f64> {
    let vals = field(g, "condition").and_then(|c| field(c, "trigger_values"))?;
    items(vals).first().map(|v| as_float(Some(v)))
}

/// `_qty(gtt)`: total quantity the trigger would sell.
fn gtt_qty(g: &PyValue) -> i64 {
    field(g, "orders")
        .map(items)
        .unwrap_or_default()
        .iter()
        .map(|o| as_int(field(o, "quantity")))
        .sum()
}

/// `_protected(symbol)`: an untouchable instrument is deliberately never traded.
fn tradeable(symbol: &str) -> bool {
    assert_tradeable(symbol, None).is_ok()
}

/// `round(x)` → `int`, ties to even.
fn round_int(x: f64) -> i64 {
    // Book values are far inside i64.
    #[allow(clippy::cast_possible_truncation)]
    let out = py_round0(x) as i64;
    out
}

#[allow(clippy::cast_precision_loss)]
fn f(i: i64) -> f64 {
    i as f64
}

/// One finding, typed; serialised in Python's key order by [`Finding::to_py`].
#[derive(Debug, Clone, PartialEq)]
pub struct Finding {
    /// `kind`.
    pub kind: &'static str,
    /// `symbol`.
    pub symbol: String,
    /// `qty`.
    pub qty: i64,
    /// `covered` (PARTIAL, EXCESS).
    pub covered: Option<i64>,
    /// `gtt_ids` (EXCESS, ORPHAN).
    pub gtt_ids: Option<Vec<PyValue>>,
    /// `trigger` (TOO_FAR, TOO_CLOSE).
    pub trigger: Option<f64>,
    /// `drop_pct` (TOO_FAR, TOO_CLOSE).
    pub drop_pct: Option<f64>,
    /// `value`.
    pub value: i64,
    /// `detail`.
    pub detail: String,
}

impl Finding {
    /// The finding as Python's dict, keys in the order Python builds them.
    #[must_use]
    pub fn to_py(&self) -> PyDict {
        let mut d = PyDict::new();
        d.insert("kind", PyValue::Str(self.kind.to_owned()));
        d.insert("symbol", PyValue::Str(self.symbol.clone()));
        d.insert("qty", PyValue::Int(self.qty));
        if let Some(c) = self.covered {
            d.insert("covered", PyValue::Int(c));
        }
        if self.kind == EXCESS
            && let Some(ids) = &self.gtt_ids
        {
            d.insert("gtt_ids", PyValue::List(ids.clone()));
        }
        if let Some(t) = self.trigger {
            d.insert("trigger", PyValue::Float(t));
        }
        if let Some(p) = self.drop_pct {
            d.insert("drop_pct", PyValue::Float(p));
        }
        if self.kind == ORPHAN
            && let Some(ids) = &self.gtt_ids
        {
            d.insert("gtt_ids", PyValue::List(ids.clone()));
        }
        d.insert("value", PyValue::Int(self.value));
        d.insert("detail", PyValue::Str(self.detail.clone()));
        d
    }
}

/// `review(holdings, gtts)`, typed.
#[derive(Debug, Clone, PartialEq)]
pub struct Review {
    /// `checked`.
    pub checked: usize,
    /// `excluded`, sorted.
    pub excluded: Vec<String>,
    /// `active_gtts`.
    pub active_gtts: usize,
    /// `protected`.
    pub protected: usize,
    /// `findings`, worst first then by value.
    pub findings: Vec<Finding>,
    /// `unprotected_value`.
    pub unprotected_value: i64,
    /// `tradeable_value`.
    pub tradeable_value: i64,
    /// `coverage_pct`.
    pub coverage_pct: Option<f64>,
    /// `healthy`.
    pub healthy: bool,
}

impl Review {
    /// `counts`: one entry per severity, Python's dict order.
    #[must_use]
    pub fn counts(&self) -> Vec<(&'static str, usize)> {
        SEVERITY
            .iter()
            .map(|(k, _)| (*k, self.findings.iter().filter(|x| x.kind == *k).count()))
            .collect()
    }

    /// The review as Python's dict.
    #[must_use]
    pub fn to_py(&self) -> PyDict {
        let counts = self
            .counts()
            .into_iter()
            .map(|(k, n)| (k, PyValue::Int(i64::try_from(n).unwrap_or(i64::MAX))))
            .collect::<Vec<_>>();
        dict([
            ("checked", PyValue::Int(len_i(self.checked))),
            (
                "excluded",
                PyValue::List(
                    self.excluded
                        .iter()
                        .map(|s| PyValue::Str(s.clone()))
                        .collect(),
                ),
            ),
            ("active_gtts", PyValue::Int(len_i(self.active_gtts))),
            ("protected", PyValue::Int(len_i(self.protected))),
            (
                "findings",
                PyValue::List(
                    self.findings
                        .iter()
                        .map(|x| PyValue::Map(x.to_py()))
                        .collect(),
                ),
            ),
            ("counts", PyValue::Map(dict(counts))),
            ("unprotected_value", PyValue::Int(self.unprotected_value)),
            ("tradeable_value", PyValue::Int(self.tradeable_value)),
            (
                "coverage_pct",
                self.coverage_pct.map_or(PyValue::None, PyValue::Float),
            ),
            ("healthy", PyValue::Bool(self.healthy)),
        ])
    }
}

fn len_i(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// A dict keyed in first-seen order, as Python's `dict` keeps it.
fn ordered_insert<T>(map: &mut Vec<(String, T)>, key: &str, value: T) {
    if let Some(slot) = map.iter_mut().find(|(k, _)| k == key) {
        slot.1 = value;
    } else {
        map.push((key.to_owned(), value));
    }
}

/// `review(holdings, gtts)`: compare what is held against what is actually triggered.
///
/// `holdings`: `[{symbol, quantity, last_price, ...}]` as `kite_client.holdings()` returns;
/// `gtts`: raw GTT dicts. `band` is `STOP_MIN`/`STOP_MAX` as configured.
#[must_use]
pub fn review(holdings: &[PyValue], gtts: &[PyValue], band: StopConfig) -> Review {
    // held = {h["symbol"]: h for h in holdings} — a later duplicate replaces the value
    // but keeps the first key's position.
    let mut held: Vec<(String, &PyValue)> = Vec::new();
    for h in holdings {
        ordered_insert(&mut held, &as_text(field(h, "symbol")), h);
    }
    let tradeable_rows: Vec<(String, &PyValue)> = held
        .iter()
        .filter(|(s, _)| tradeable(s))
        .map(|(s, h)| (s.clone(), *h))
        .collect();

    let mut live: Vec<(String, Vec<&PyValue>)> = Vec::new();
    for g in gtts {
        if as_text(field(g, "status")).to_lowercase() != ACTIVE {
            continue;
        }
        let sym = gtt_symbol(g);
        if let Some(slot) = live.iter_mut().find(|(k, _)| *k == sym) {
            slot.1.push(g);
        } else {
            live.push((sym, vec![g]));
        }
    }

    let mut findings: Vec<Finding> = Vec::new();
    let blank = |kind: &'static str, symbol: &str, qty: i64| Finding {
        kind,
        symbol: symbol.to_owned(),
        qty,
        covered: None,
        gtt_ids: None,
        trigger: None,
        drop_pct: None,
        value: 0,
        detail: String::new(),
    };

    for (sym, h) in &tradeable_rows {
        let qty = as_int(field(h, "quantity"));
        let px = as_float(field(h, "last_price"));
        let rows: &[&PyValue] = live
            .iter()
            .find(|(k, _)| k == sym)
            .map_or(&[], |(_, v)| v.as_slice());
        if rows.is_empty() {
            findings.push(Finding {
                value: round_int(f(qty) * px),
                detail: "no active GTT stop".to_owned(),
                ..blank(MISSING, sym, qty)
            });
            continue;
        }
        let covered: i64 = rows.iter().map(|g| gtt_qty(g)).sum();
        let triggers: Vec<f64> = rows
            .iter()
            .filter_map(|g| gtt_trigger(g))
            .filter(|t| *t != 0.0)
            .collect();
        let best = triggers.iter().copied().reduce(f64::max);

        if covered < qty {
            findings.push(Finding {
                covered: Some(covered),
                value: round_int(f(qty - covered) * px),
                detail: format!("stop covers {covered} of {qty} shares"),
                ..blank(PARTIAL, sym, qty)
            });
        } else if covered > qty {
            findings.push(Finding {
                covered: Some(covered),
                gtt_ids: Some(
                    rows.iter()
                        .map(|g| field(g, "id").cloned().unwrap_or(PyValue::None))
                        .collect(),
                ),
                value: round_int(f(covered - qty) * px),
                detail: format!(
                    "stops cover {covered} shares but only {qty} are held; firing would sell {} you do not own",
                    covered - qty
                ),
                ..blank(EXCESS, sym, qty)
            });
        }

        if let Some(best) = best
            && best != 0.0
            && px > 0.0
        {
            let drop = 1.0 - best / px;
            let band_text = format!(
                "band allows {:.0}-{:.0}%",
                band.stop_min * 100.0,
                band.stop_max * 100.0
            );
            let kind = if drop > band.stop_max + 1e-9 {
                Some(TOO_FAR)
            } else if drop < band.stop_min - 1e-9 {
                Some(TOO_CLOSE)
            } else {
                None
            };
            if let Some(kind) = kind {
                findings.push(Finding {
                    trigger: Some(best),
                    drop_pct: Some(py_round(drop * 100.0, 2)),
                    value: round_int(f(qty) * px),
                    detail: format!("{:.1}% below, {band_text}", drop * 100.0),
                    ..blank(kind, sym, qty)
                });
            }
        }
    }

    for (sym, rows) in &live {
        if !held.iter().any(|(k, _)| k == sym) {
            findings.push(Finding {
                qty: rows.iter().map(|g| gtt_qty(g)).sum(),
                gtt_ids: Some(
                    rows.iter()
                        .map(|g| field(g, "id").cloned().unwrap_or(PyValue::None))
                        .collect(),
                ),
                detail: "active trigger with no matching holding".to_owned(),
                ..blank(ORPHAN, sym, 0)
            });
        }
    }

    // list.sort is stable; key (severity, -value).
    findings.sort_by_key(|x| (severity(x.kind), -x.value));

    let price_of = |sym: &str| {
        held.iter()
            .find(|(k, _)| k == sym)
            .map_or(0.0, |(_, h)| as_float(field(h, "last_price")))
    };
    let mut unprotected_raw = 0.0;
    for x in &findings {
        if x.kind == MISSING {
            unprotected_raw += f(x.qty) * price_of(&x.symbol);
        } else if x.kind == PARTIAL {
            unprotected_raw += f(x.qty - x.covered.unwrap_or(0)) * price_of(&x.symbol);
        }
    }
    // sum(...) over a generator starts at int 0, then adds floats in order.
    let mut tradeable_raw = 0.0;
    for (_, h) in &tradeable_rows {
        tradeable_raw += f(as_int(field(h, "quantity"))) * as_float(field(h, "last_price"));
    }
    let unprotected_value = unprotected_raw.min(tradeable_raw);
    let tradeable_value = tradeable_raw;
    let protected = tradeable_rows
        .iter()
        .filter(|(s, _)| {
            !findings
                .iter()
                .any(|x| &x.symbol == s && (x.kind == MISSING || x.kind == PARTIAL))
        })
        .count();

    let mut excluded: Vec<String> = held
        .iter()
        .filter(|(s, _)| !tradeable_rows.iter().any(|(t, _)| t == s))
        .map(|(s, _)| s.clone())
        .collect();
    excluded.sort();
    excluded.dedup();

    let healthy = !findings
        .iter()
        .any(|x| [MISSING, PARTIAL, EXCESS, ORPHAN].contains(&x.kind));
    Review {
        checked: tradeable_rows.len(),
        excluded,
        active_gtts: live.iter().map(|(_, v)| v.len()).sum(),
        protected,
        findings,
        unprotected_value: round_int(unprotected_value),
        tradeable_value: round_int(tradeable_value),
        coverage_pct: if tradeable_value == 0.0 {
            None
        } else {
            Some(py_round(
                ((tradeable_value - unprotected_value) / tradeable_value * 100.0).max(0.0),
                1,
            ))
        },
        healthy,
    }
}

/// `build_stop_plan(holdings, gtts, vol_by_symbol=..., plan_id=...)`: propose stops to arm
/// and triggers to cancel. Pure: it cannot place anything.
///
/// `plan_id` is the caller's (`uuid.uuid4().hex[:12]` in Python, from the desk's id source).
#[must_use]
pub fn build_stop_plan(
    holdings: &[PyValue],
    gtts: &[PyValue],
    vol_by_symbol: &[(String, f64)],
    plan_id: &str,
    band: StopConfig,
) -> PyDict {
    let rev = review(holdings, gtts, band);
    let mut held: Vec<(String, &PyValue)> = Vec::new();
    for h in holdings {
        ordered_insert(&mut held, &as_text(field(h, "symbol")), h);
    }

    let mut cancels = Vec::new();
    for x in &rev.findings {
        if x.kind != EXCESS && x.kind != ORPHAN {
            continue;
        }
        for gid in x.gtt_ids.as_deref().unwrap_or_default() {
            cancels.push(PyValue::Map(dict([
                ("symbol", PyValue::Str(x.symbol.clone())),
                ("gtt_id", gid.clone()),
                ("reason", PyValue::Str(x.kind.to_owned())),
                ("detail", PyValue::Str(x.detail.clone())),
            ])));
        }
    }

    struct Row {
        value: i64,
        at_risk: i64,
        default_vol: bool,
        symbol: String,
        dict: PyDict,
    }
    let mut rows: Vec<Row> = Vec::new();
    for x in &rev.findings {
        if ![MISSING, PARTIAL, EXCESS].contains(&x.kind) {
            continue;
        }
        let Some((_, h)) = held.iter().find(|(k, _)| *k == x.symbol) else {
            continue;
        };
        let px = as_float(field(h, "last_price"));
        let qty = if x.kind == EXCESS {
            x.qty
        } else {
            x.qty - x.covered.unwrap_or(0)
        };
        if qty <= 0 || px <= 0.0 {
            continue;
        }
        // `vols.get(sym)` then `vol if vol else DEFAULT_VOL`: a 0.0 vol is the default.
        let vol = vol_by_symbol
            .iter()
            .rev()
            .find(|(s, _)| *s == x.symbol)
            .map(|(_, v)| *v)
            .filter(|v| *v != 0.0);
        let trigger = stop_from_vol(px, vol.unwrap_or(DEFAULT_VOL), band);
        let value = round_int(f(qty) * px);
        let at_risk = round_int(f(qty) * (px - trigger));
        rows.push(Row {
            value,
            at_risk,
            default_vol: vol.is_none(),
            symbol: x.symbol.clone(),
            dict: dict([
                ("symbol", PyValue::Str(x.symbol.clone())),
                ("qty", PyValue::Int(qty)),
                ("last_price", PyValue::Float(py_round(px, 2))),
                ("trigger", PyValue::Float(trigger)),
                (
                    "drop_pct",
                    PyValue::Float(py_round((1.0 - trigger / px) * 100.0, 2)),
                ),
                ("value", PyValue::Int(value)),
                ("at_risk", PyValue::Int(at_risk)),
                (
                    "vol",
                    vol.map_or(PyValue::None, |v| PyValue::Float(py_round(v, 4))),
                ),
                (
                    "vol_source",
                    PyValue::Str(if vol.is_some() { "scan" } else { "default" }.to_owned()),
                ),
                ("reason", PyValue::Str(x.kind.to_owned())),
            ]),
        });
    }
    rows.sort_by_key(|r| -r.value);

    let count = len_i(rows.len());
    let cancel_count = len_i(cancels.len());
    let value: i64 = rows.iter().map(|r| r.value).sum();
    let at_risk: i64 = rows.iter().map(|r| r.at_risk).sum();
    let using_default: Vec<PyValue> = rows
        .iter()
        .filter(|r| r.default_vol)
        .map(|r| PyValue::Str(r.symbol.clone()))
        .collect();
    dict([
        ("plan_id", PyValue::Str(plan_id.to_owned())),
        (
            "rows",
            PyValue::List(rows.into_iter().map(|r| PyValue::Map(r.dict)).collect()),
        ),
        ("cancels", PyValue::List(cancels)),
        ("count", PyValue::Int(count)),
        ("cancel_count", PyValue::Int(cancel_count)),
        ("value", PyValue::Int(value)),
        ("at_risk", PyValue::Int(at_risk)),
        ("using_default_vol", PyValue::List(using_default)),
        ("review", PyValue::Map(rev.to_py())),
    ])
}

/// `f"{n:,.0f}"` for an integer amount.
fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(ch);
    }
    if n < 0 { format!("-{out}") } else { out }
}

/// `summary_line(rev)`.
#[must_use]
pub fn summary_line(rev: &Review) -> String {
    if rev.checked == 0 {
        return "No tradeable positions to protect.".to_owned();
    }
    if rev.healthy {
        return format!(
            "All {} tradeable positions carry a stop covering their full quantity.",
            rev.checked
        );
    }
    let counts = rev.counts();
    let count = |k: &str| counts.iter().find(|(c, _)| *c == k).map_or(0, |(_, n)| *n);
    let mut bits = Vec::new();
    if count(MISSING) > 0 {
        bits.push(format!("{} with no stop at all", count(MISSING)));
    }
    if count(PARTIAL) > 0 {
        bits.push(format!("{} only partly covered", count(PARTIAL)));
    }
    format!(
        "₹{} of ₹{} is unprotected: {}.",
        thousands(rev.unprotected_value),
        thousands(rev.tradeable_value),
        bits.join(", ")
    )
}

#[cfg(test)]
mod tests;
