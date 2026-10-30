//! `app/analytics/regime_view.py`: the read-only view model for `/regime` and `/regime/data`.
//!
//! Reads **precomputed** rows only — no Kite call, no signal recomputation, no write (beyond what
//! `db.migrate` and `schema_version` do on connect, as in Python). Fields with no data are
//! `None`, never `0`. The derivation lives here; the template renders.
//!
//! A decision that carries **proposed orders** is joined to what is known about each position
//! ([`enrich_orders`]: the weight it moves from and to, its P&L, the FIFO tax-lot review, the
//! liquidity check and the order's state once submitted) and every proposed sale gets its lot
//! breakdown ([`tax_detail`]). A tax review that fails leaves `tax_flagged` **unknown** (`None`),
//! never `False`: a silent failure must not invite an unassessed sale. `dt.date.today()` in
//! Python is the injected clock's IST date here (the harness's `FakeDate.today` is the same
//! clock; DECISIONS-RS lane analytics_regime R8.AR5).

use std::collections::BTreeMap;

use chrono::NaiveDate;

use crate::lanes::analytics::desk_settings::{SVal, config_value};
use crate::lanes::weekly::pandas::{Num, py_round, py_sum};
use crate::lanes::weekly::plan_store;
use crate::pyfmt::{PyArg, format_value};
use crate::settings::Settings;
use crate::storage::{Conn, DbValue, StorageError};
use crate::wire::{PyDict, PyValue, dict};

use super::regime::{ConfigError, RegimeConfig, display_reason, is_affirmative_risk_off};
use super::{breadth, daily_runs, index_cache, regime_store};

type V = PyValue;

/// Why the view could not be built (an unhandled exception in Python: a 500).
#[derive(Debug, thiserror::Error)]
pub enum BuildError {
    /// The database.
    #[error(transparent)]
    Storage(#[from] StorageError),
    /// `regime_config()` raised.
    #[error(transparent)]
    Config(#[from] ConfigError),
    /// A stored value Python would raise on (a tier that is not a tier, a bad date).
    #[error("ValueError: {0}")]
    Value(String),
}

/// `TIMELINE_MIN_WEEKS`.
pub const TIMELINE_MIN_WEEKS: i64 = 3;
/// `SPARK_SESSIONS`.
pub const SPARK_SESSIONS: usize = 130;
const SPARK_W: i64 = 168;
const SPARK_H: i64 = 34;

const TIER_META: [(&str, &str, &str, &str); 4] = [
    (
        "R1",
        "Risk-On",
        "on",
        "Full exposure permitted, full-size entries",
    ),
    (
        "R2",
        "Caution",
        "caution",
        "Reduced exposure, half-size entries",
    ),
    (
        "R3",
        "Risk-Off",
        "off",
        "Materially reduced exposure, no new entries",
    ),
    (
        "R4",
        "Crash",
        "crash",
        "Minimum residual exposure, no new entries",
    ),
];

fn tier_meta(tier: &str) -> Option<(&'static str, &'static str, &'static str)> {
    TIER_META
        .iter()
        .find(|(t, ..)| *t == tier)
        .map(|(_, a, b, c)| (*a, *b, *c))
}

/// `TIER_LADDER`: (tier, name, key, buys, when, does).
const TIER_LADDER: [(&str, &str, &str, &str, &str, &str); 4] = [
    (
        "R1",
        "Risk-On",
        "on",
        "Full size",
        "Health strong on both the 50 and 200-DMA, breadth healthy, and the momentum sentinel above its 50-DMA.",
        "Hold the full book. New positions enter at full weight.",
    ),
    (
        "R2",
        "Caution",
        "caution",
        "Half size",
        "Nothing risk-off has fired, but the conditions for Risk-On are not all met — or the momentum sentinel has broken its 50-DMA.",
        "Trim the weakest names back to the cap. New positions enter at half weight, or not at all while the sentinel veto is active.",
    ),
    (
        "R3",
        "Risk-Off",
        "off",
        "Blocked",
        "Long-term health at or below its threshold, or breadth below its threshold, or the momentum sentinel below its 200-DMA.",
        "Sell down until actual exposure reaches the cap. No new positions.",
    ),
    (
        "R4",
        "Crash",
        "crash",
        "Blocked",
        "Most of the book in full bearish alignment with very weak breadth, or the sentinel below its 200-DMA with weak long-term health and breadth.",
        "Reduce to a minimum residual, keeping only the names the residual policy names. No new positions.",
    ),
];

const BREADTH_BANDS: [(f64, f64, &str, &str); 4] = [
    (0.0, 30.0, "Crash breadth", "crash"),
    (30.0, 40.0, "Risk-off", "off"),
    (40.0, 55.0, "Caution", "caution"),
    (55.0, 101.0, "Recovery", "on"),
];

fn new_buy_label(v: &V) -> &'static str {
    match v {
        V::Str(s) if s == "full" => "Full size",
        V::Str(s) if s == "half" => "Half size",
        V::Str(s) if s == "blocked" => "Blocked",
        _ => "—",
    }
}

fn forced_label(v: &V) -> &'static str {
    match v {
        V::Str(s) => match s.as_str() {
            "none" => "None",
            "trim_to_cap" => "Trim to cap",
            "reduce_to_cap" => "Reduce to cap",
            "crash_reduce" => "Crash reduction",
            "manual_review" => "Manual review",
            _ => "—",
        },
        _ => "—",
    }
}

// ---------------------------------------------------------------------------------------------
// small Python helpers
// ---------------------------------------------------------------------------------------------

fn s(x: &str) -> V {
    V::Str(x.to_owned())
}

fn get(m: &PyDict, k: &str) -> V {
    m.get(k).cloned().unwrap_or(V::None)
}

/// `d.get(k, default)`: the default only when the key is absent.
fn get_or(m: &PyDict, k: &str, default: V) -> V {
    m.get(k).cloned().unwrap_or(default)
}

#[allow(clippy::cast_precision_loss)]
fn num(v: &V) -> Option<f64> {
    match v {
        V::Float(f) => Some(*f),
        V::Int(i) => Some(*i as f64),
        V::Bool(b) => Some(f64::from(u8::from(*b))),
        _ => None,
    }
}

fn truthy(v: &V) -> bool {
    match v {
        V::None => false,
        V::Bool(b) => *b,
        V::Int(i) => *i != 0,
        V::Float(f) => *f != 0.0,
        V::Str(x) => !x.is_empty(),
        V::List(l) => !l.is_empty(),
        V::Map(m) => !m.is_empty(),
        _ => true,
    }
}

fn opt_f(x: Option<f64>) -> V {
    x.map_or(V::None, V::Float)
}

fn arg(v: &V) -> PyArg {
    match v {
        V::Int(i) => PyArg::Int(*i),
        V::Float(f) => PyArg::Float(*f),
        V::Bool(b) => PyArg::Bool(*b),
        V::Str(x) => PyArg::Str(x.clone()),
        _ => PyArg::None,
    }
}

/// `format(x, spec)`.
fn pf(x: f64, spec: &str) -> String {
    format_value(&PyArg::Float(x), spec).unwrap_or_default()
}

fn pv(v: &V, spec: &str) -> String {
    format_value(&arg(v), spec).unwrap_or_default()
}

fn as_map(v: &V) -> PyDict {
    match v {
        V::Map(m) => m.clone(),
        _ => PyDict::new(),
    }
}

fn as_list(v: &V) -> Vec<V> {
    match v {
        V::List(l) => l.clone(),
        _ => Vec::new(),
    }
}

fn as_str(v: &V) -> Option<&str> {
    match v {
        V::Str(x) => Some(x),
        _ => None,
    }
}

/// `json.loads(text or default)`.
fn loads(v: &V, default: &str) -> V {
    let text = match v {
        V::Str(x) if !x.is_empty() => x.as_str(),
        _ => default,
    };
    V::from_json(text.as_bytes()).unwrap_or(V::None)
}

fn date_of(v: &V) -> Result<Option<NaiveDate>, BuildError> {
    match v {
        V::Str(x) if !x.is_empty() => super::ops::parse_iso_date(x)
            .map(Some)
            .ok_or_else(|| BuildError::Value(format!("Invalid isoformat string: '{x}'"))),
        _ => Ok(None),
    }
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn ilen(n: usize) -> V {
    V::Int(i64::try_from(n).unwrap_or(i64::MAX))
}

fn list_of_maps(v: &V) -> Vec<PyDict> {
    as_list(v).iter().map(as_map).collect()
}

/// `_pct(v, 1)`.
fn pct1(v: &V) -> V {
    if matches!(v, V::None) {
        V::None
    } else {
        V::Str(format!("{}%", pv(v, ".1f")))
    }
}

/// `x < y` for two Python numbers (None never compares here).
fn lt(a: &V, b: f64) -> bool {
    num(a).is_some_and(|x| x < b)
}

/// `py_float(row value)` for the numbers a stored row carries (`float(x or 0.0)`).
fn float_or0(v: &V) -> f64 {
    match v {
        V::Str(x) => crate::settings::py_float(x).unwrap_or(0.0),
        other => num(other).unwrap_or(0.0),
    }
}

fn to_num(v: &V) -> Num {
    match v {
        V::Int(i) => Num::Int(*i),
        other => Num::Float(num(other).unwrap_or(0.0)),
    }
}

fn num_v(n: Num) -> V {
    match n {
        Num::Int(i) => V::Int(i),
        Num::Float(f) => V::Float(f),
    }
}

/// Python's `max(iterable)` over numbers: the first maximal item.
fn py_max(items: &[V]) -> Option<V> {
    let mut best: Option<&V> = None;
    for it in items {
        match best {
            None => best = Some(it),
            Some(b) if num(it).unwrap_or(f64::NAN) > num(b).unwrap_or(f64::NAN) => best = Some(it),
            _ => {}
        }
    }
    best.cloned()
}

fn abs_v(v: &V) -> V {
    match v {
        V::Int(i) => V::Int(i.abs()),
        V::Float(f) => V::Float(f.abs()),
        other => other.clone(),
    }
}

/// `x or 0`.
fn or0(v: &V) -> V {
    if truthy(v) { v.clone() } else { V::Int(0) }
}

/// `str.title()` for the ASCII words this page passes it.
fn title(x: &str) -> String {
    let mut out = String::with_capacity(x.len());
    let mut prev_alpha = false;
    for c in x.chars() {
        if c.is_alphabetic() {
            if prev_alpha {
                out.extend(c.to_lowercase());
            } else {
                out.extend(c.to_uppercase());
            }
            prev_alpha = true;
        } else {
            out.push(c);
            prev_alpha = false;
        }
    }
    out
}

// ---------------------------------------------------------------------------------------------
// constants the settings page can move
// ---------------------------------------------------------------------------------------------

/// The `app.config` constants `build` reads, as they stand at run time.
#[derive(Debug, Clone)]
pub struct DeskConstants {
    /// `TARGET_POSITIONS`.
    pub target_positions: (i64, i64),
    /// `CLUSTER_CAP`.
    pub cluster_cap: V,
    /// `MIN_POSITION_WEIGHT`.
    pub min_position_weight: f64,
    /// `HALF_SIZE_WEIGHT`.
    pub half_size_weight: f64,
    /// `MAX_POS_VS_DAY_VALUE`.
    pub max_pos_vs_day_value: f64,
    /// A stored override holds `REGIME_WEEKLY_EVAL_WEEKDAY`. The settings page types that key
    /// as a `choice`, so `apply_to_config` leaves `C.REGIME_WEEKLY_EVAL_WEEKDAY` a **str**
    /// ("2"), and `regime_config()`'s `validate` then raises `TypeError` on `0 <= "2" <= 6`:
    /// every `/regime` and `/regime/data` answers 500 until the override is reset. Reproduced,
    /// not repaired (DECISIONS-RS lane analytics_regime R-ar.1); the Python defect is the fix.
    pub weekday_override_is_text: bool,
}

impl DeskConstants {
    /// From the settings and the stored overrides.
    #[must_use]
    pub fn resolve(settings: &Settings, overrides: &BTreeMap<String, SVal>) -> Self {
        let val = |k: &str| config_value(overrides, settings, k);
        #[allow(clippy::cast_precision_loss)]
        let f = |k: &str, d: f64| match val(k) {
            Some(SVal::Float(x)) => x,
            Some(SVal::Int(i)) => i as f64,
            _ => d,
        };
        let cluster_cap = match val("CLUSTER_CAP") {
            Some(SVal::Int(i)) => V::Int(i),
            Some(SVal::Float(x)) => V::Float(x),
            _ => V::Float(25.0),
        };
        let target_positions = match val("TARGET_POSITIONS") {
            Some(SVal::Pair(a, b)) => (a, b),
            _ => (12, 15),
        };
        Self {
            target_positions,
            cluster_cap,
            min_position_weight: f("MIN_POSITION_WEIGHT", 6.0),
            half_size_weight: f("HALF_SIZE_WEIGHT", 3.0),
            max_pos_vs_day_value: f("MAX_POS_VS_DAY_VALUE", 0.01),
            weekday_override_is_text: matches!(
                overrides.get("REGIME_WEEKLY_EVAL_WEEKDAY"),
                Some(SVal::Str(_))
            ),
        }
    }
}

// ---------------------------------------------------------------------------------------------
// sources
// ---------------------------------------------------------------------------------------------

/// `latest_evaluation(conn)`: the canonical committed row if one exists, else the newest preview.
///
/// # Errors
/// The driver's.
pub async fn latest_evaluation(conn: &mut Conn) -> Result<(PyDict, bool), StorageError> {
    if let Some(row) = regime_store::latest_committed(conn).await? {
        return Ok((row, true));
    }
    let cur = conn
        .execute(
            "SELECT * FROM regime_evaluations ORDER BY created_at DESC, id DESC LIMIT 1",
            &[],
        )
        .await?;
    Ok((
        cur.fetchone().map(super::web::row_dict).unwrap_or_default(),
        false,
    ))
}

/// `index_status(conn, names, as_of)`.
///
/// # Errors
/// The driver's.
pub async fn index_status(
    conn: &mut Conn,
    names: &[String],
    as_of: Option<NaiveDate>,
    stale_days: i64,
) -> Result<Vec<PyDict>, BuildError> {
    let mut out = Vec::new();
    for name in names {
        let cur = conn
            .execute(
                "SELECT MAX(date) d, COUNT(*) n FROM index_series WHERE index_name=? AND is_final=1",
                &[name.as_str().into()],
            )
            .await?;
        let row = cur.fetchone();
        let last = match row.and_then(|r| r.get("d")) {
            Some(DbValue::Text(x)) if !x.is_empty() => date_of(&V::Str(x.clone()))?,
            _ => None,
        };
        let n = row
            .and_then(|r| r.get("n"))
            .map_or(V::Int(0), super::web::db_to_py);
        let age = match (last, as_of) {
            (Some(l), Some(a)) => Some((a - l).num_days()),
            _ => None,
        };
        let dates_cur = conn
            .execute(
                "SELECT date FROM index_series WHERE index_name=? AND is_final=1 ORDER BY date",
                &[name.as_str().into()],
            )
            .await?;
        let mut days = Vec::with_capacity(dates_cur.rows.len());
        for r in &dates_cur.rows {
            let text = r.get("date").and_then(DbValue::as_str).unwrap_or_default();
            match date_of(&V::Str(text.to_owned()))? {
                Some(d) => days.push(d),
                None => return Err(BuildError::Value("Invalid isoformat string: ''".to_owned())),
            }
        }
        let gaps: Vec<V> = days
            .windows(2)
            .filter(|w| (w[1] - w[0]).num_days() > 5)
            .map(|w| {
                V::Map(dict([
                    ("from", V::Str(iso(w[0]))),
                    ("to", V::Str(iso(w[1]))),
                    ("days", V::Int((w[1] - w[0]).num_days())),
                ]))
            })
            .collect();
        let tail = gaps[gaps.len().saturating_sub(3)..].to_vec();
        out.push(dict([
            ("index_name", V::Str(name.clone())),
            (
                "last_final_session",
                last.map_or(V::None, |d| V::Str(iso(d))),
            ),
            ("sessions", if row.is_some() { n } else { V::Int(0) }),
            ("age_days", age.map_or(V::None, V::Int)),
            ("stale", V::Bool(age.is_some_and(|a| a > stale_days))),
            ("gaps", V::List(tail)),
            ("gap_count", ilen(gaps.len())),
            (
                "first_session",
                days.first().map_or(V::None, |d| V::Str(iso(*d))),
            ),
            ("has_data", V::Bool(last.is_some())),
        ]));
    }
    Ok(out)
}

// ---------------------------------------------------------------------------------------------
// derivation
// ---------------------------------------------------------------------------------------------

/// `_confirmation(ma, cfg)`: "3/3 below".
fn confirmation(ma: &PyDict, confirm_days: i64) -> String {
    let state = match ma.get("state") {
        Some(V::Str(x)) => x.clone(),
        Some(other) => crate::templates::py_str_value(&crate::templates::py_value(other)),
        None => "unknown".to_owned(),
    };
    if state == "unknown" {
        return format!("0/{confirm_days} unconfirmed");
    }
    let raw = or0(&get(ma, "confirming_closes"));
    #[allow(clippy::cast_possible_truncation)]
    let n = match raw {
        V::Int(i) => i,
        V::Float(f) => f.trunc() as i64,
        V::Bool(b) => i64::from(b),
        _ => 0,
    };
    format!("{}/{confirm_days} {state}", n.min(confirm_days))
}

/// `_sparkline(closes, ma_len)`: close against the deciding moving average, ~6 months.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn sparkline(closes: &[f64], ma_len: usize) -> Option<PyDict> {
    if closes.len() < ma_len + 2 {
        return None;
    }
    let ma: Vec<Option<f64>> = (0..closes.len())
        .map(|i| {
            (i + 1 >= ma_len).then(|| {
                let window = &closes[i + 1 - ma_len..=i];
                let total = py_sum(window.iter().map(|x| Num::Float(*x))).as_f64();
                total / ma_len as f64
            })
        })
        .collect();
    let start = (ma_len - 1).max(closes.len().saturating_sub(SPARK_SESSIONS));
    let px_v = &closes[start..];
    let ma_v = &ma[start..];
    if px_v.len() < 2 {
        return None;
    }
    let pts: Vec<f64> = px_v
        .iter()
        .copied()
        .chain(ma_v.iter().filter_map(|m| *m))
        .collect();
    let mut lo = pts[0];
    let mut hi = pts[0];
    for p in &pts[1..] {
        if *p < lo {
            lo = *p;
        }
        if *p > hi {
            hi = *p;
        }
    }
    let span = {
        let d = hi - lo;
        if d != 0.0 {
            d
        } else {
            let base = if hi != 0.0 { hi } else { 1.0 };
            base * 0.01
        }
    };
    let n = (px_v.len() - 1) as f64;
    let x = |i: usize| i as f64 / n * (SPARK_W - 2) as f64 + 1.0;
    let y = |v: f64| (SPARK_H - 2) as f64 - (v - lo) / span * (SPARK_H - 4) as f64;
    let path = |vals: &mut dyn Iterator<Item = Option<f64>>| {
        let mut out = Vec::new();
        let mut pen = "M";
        for (i, v) in vals.enumerate() {
            match v {
                None => pen = "M",
                Some(v) => {
                    out.push(format!("{pen}{},{}", pf(x(i), ".1f"), pf(y(v), ".1f")));
                    pen = "L";
                }
            }
        }
        out.join(" ")
    };
    let last_px = px_v[px_v.len() - 1];
    let last_ma = ma_v[ma_v.len() - 1].unwrap_or(last_px);
    Some(dict([
        ("w", V::Int(SPARK_W)),
        ("h", V::Int(SPARK_H)),
        ("ma_len", ilen(ma_len)),
        ("price", V::Str(path(&mut px_v.iter().map(|p| Some(*p))))),
        ("ma", V::Str(path(&mut ma_v.iter().copied()))),
        ("last_x", V::Float(py_round(x(px_v.len() - 1), 1))),
        ("last_y", V::Float(py_round(y(last_px), 1))),
        ("above", V::Bool(last_px >= last_ma)),
        ("sessions", ilen(px_v.len())),
    ]))
}

async fn spark_series(conn: &mut Conn, order: &[String]) -> BTreeMap<String, Vec<f64>> {
    let mut out = BTreeMap::new();
    for name in order {
        let Ok(candles) = index_cache::load(conn, name, None, None, true).await else {
            return BTreeMap::new();
        };
        let need = SPARK_SESSIONS + 200 + 5;
        let tail = &candles[candles.len().saturating_sub(need)..];
        out.insert(
            name.clone(),
            tail.iter()
                .filter_map(|c| c.close.filter(|x| *x != 0.0))
                .collect(),
        );
    }
    out
}

fn index_cards(
    diagnostics: &PyDict,
    cfg: &RegimeConfig,
    status_rows: &[PyDict],
    spark: &BTreeMap<String, Vec<f64>>,
) -> Vec<PyDict> {
    let order: Vec<String> = cfg
        .structural_indices
        .iter()
        .map(|(_, v)| v.clone())
        .chain(std::iter::once(cfg.momentum_sentinel.clone()))
        .collect();
    let mut cards = Vec::new();
    for name in &order {
        let d = as_map(&get(diagnostics, name));
        if d.is_empty() {
            continue;
        }
        let sigs = as_map(&get(&d, "signals"));
        let mas: Vec<V> = [20_i64, 50, 200]
            .iter()
            .map(|n| {
                let sig = {
                    let raw = get(&sigs, &n.to_string());
                    if truthy(&raw) {
                        as_map(&raw)
                    } else {
                        PyDict::new()
                    }
                };
                V::Map(dict([
                    ("length", V::Int(*n)),
                    ("ma", get(&sig, "ma")),
                    ("distance_pct", get(&sig, "distance_pct")),
                    ("state", get_or(&sig, "state", s("unknown"))),
                    (
                        "confirming_closes",
                        get_or(&sig, "confirming_closes", V::Int(0)),
                    ),
                    ("confirmation", V::Str(confirmation(&sig, cfg.confirm_days))),
                ]))
            })
            .collect();
        let st = status_rows
            .iter()
            .find(|r| as_str(&get(r, "index_name")) == Some(name.as_str()))
            .cloned()
            .unwrap_or_default();
        let is_sentinel = *name == cfg.momentum_sentinel;
        let closes = spark.get(name).cloned().unwrap_or_default();
        let sp = sparkline(&closes, if is_sentinel { 50 } else { 200 });
        cards.push(dict([
            ("index_name", V::Str(name.clone())),
            ("is_sentinel", V::Bool(is_sentinel)),
            ("spark", sp.map_or(V::None, V::Map)),
            ("close", get(&d, "close")),
            ("mas", V::List(mas)),
            ("bearish_stack", V::Bool(truthy(&get(&d, "bearish_stack")))),
            ("data_date", get(&d, "as_of_date")),
            (
                "stale",
                V::Bool(truthy(&get(&d, "is_stale")) || truthy(&get(&st, "stale"))),
            ),
            ("sessions", get(&st, "sessions")),
        ]));
    }
    cards
}

fn ma_of(card: Option<&PyDict>, length: i64) -> PyDict {
    card.map(|c| list_of_maps(&get(c, "mas")))
        .unwrap_or_default()
        .into_iter()
        .find(|m| matches!(m.get("length"), Some(V::Int(n)) if *n == length))
        .unwrap_or_default()
}

fn num_s(x: &V, spec: &str) -> V {
    if matches!(x, V::None) {
        V::None
    } else {
        V::Str(pv(x, spec))
    }
}

#[allow(clippy::too_many_lines, clippy::too_many_arguments)]
fn gates(
    health: &PyDict,
    breadth_pct: &V,
    breadth_usable: bool,
    sentinel: Option<&PyDict>,
    stack_weight: &V,
    cfg: &RegimeConfig,
    codes: &[String],
) -> Vec<V> {
    let h50 = get(health, "50");
    let h200 = get(health, "200");
    let s50 = ma_of(sentinel, 50);
    let s200 = ma_of(sentinel, 200);
    #[allow(clippy::cast_precision_loss)]
    let buf = cfg.buffer_bps as f64 / 100.0;
    let breadth_ev = breadth_usable && !matches!(breadth_pct, V::None);
    let cur_breadth = if breadth_usable {
        pct1(breadth_pct)
    } else {
        V::None
    };
    let sentinel_status = |m: &PyDict| {
        if m.is_empty() {
            "Not evaluable"
        } else if as_str(&get(m, "state")) == Some("below") {
            "Triggered"
        } else {
            "Clear"
        }
    };
    let rows: Vec<(String, String, V, String, String, V)> = vec![
        (
            "R3_H200_WEAK".into(),
            "Long-term structural health (H200)".into(),
            num_s(&h200, ".2f"),
            format!("≤ {}", pf(cfg.r3_h200_max, ".2f")),
            (if matches!(h200, V::None) {
                "Not evaluable"
            } else if num(&h200).is_some_and(|h| h <= cfg.r3_h200_max) {
                "Triggered"
            } else {
                "Clear"
            })
            .into(),
            s("Weighted share of the book above its 200-DMA"),
        ),
        (
            "R3_BREADTH_WEAK".into(),
            "Breadth".into(),
            cur_breadth.clone(),
            format!("< {}%", pf(cfg.r3_breadth_max, ".0f")),
            (if !breadth_ev {
                "Not evaluable"
            } else if lt(breadth_pct, cfg.r3_breadth_max) {
                "Triggered"
            } else {
                "Clear"
            })
            .into(),
            s("Universe above its 20-DMA"),
        ),
        (
            "R3_SENTINEL_BELOW_200DMA".into(),
            "Momentum 50 vs 200-DMA".into(),
            if matches!(get(&s200, "distance_pct"), V::None) {
                V::None
            } else {
                V::Str(format!("{}%", pv(&get(&s200, "distance_pct"), "+.2f")))
            },
            format!("below −{}%", pf(buf, ".2f")),
            sentinel_status(&s200).into(),
            get_or(&s200, "confirmation", s("")),
        ),
        (
            "SENTINEL_BELOW_50DMA_VETO".into(),
            "Momentum 50 vs 50-DMA".into(),
            if matches!(get(&s50, "distance_pct"), V::None) {
                V::None
            } else {
                V::Str(format!("{}%", pv(&get(&s50, "distance_pct"), "+.2f")))
            },
            format!("below −{}%", pf(buf, ".2f")),
            sentinel_status(&s50).into(),
            get_or(&s50, "confirmation", s("")),
        ),
        (
            "R4_BEARISH_STACK_AND_WEAK_BREADTH".into(),
            "Bearish MA stack weight".into(),
            num_s(stack_weight, ".2f"),
            format!(
                "≥ {} with breadth < {}%",
                pf(cfg.r4_bearish_stack_min, ".2f"),
                pf(cfg.r4_breadth_max, ".0f")
            ),
            (if matches!(stack_weight, V::None) {
                "Not evaluable"
            } else if num(stack_weight).is_some_and(|w| w >= cfg.r4_bearish_stack_min)
                && breadth_ev
                && lt(breadth_pct, cfg.r4_breadth_max)
            {
                "Triggered"
            } else {
                "Clear"
            })
            .into(),
            s("Share of the book in full bearish alignment"),
        ),
        (
            "R1_ALL_CONDITIONS_MET".into(),
            "Risk-On health requirement".into(),
            V::Str(format!(
                "H50 {} · H200 {}",
                as_str(&num_s(&h50, ".2f")).unwrap_or("—"),
                as_str(&num_s(&h200, ".2f")).unwrap_or("—")
            )),
            format!(
                "≥ {} and ≥ {}",
                pf(cfg.r1_h50_min, ".2f"),
                pf(cfg.r1_h200_min, ".2f")
            ),
            (if num(&h50).is_some_and(|h| h >= cfg.r1_h50_min)
                && num(&h200).is_some_and(|h| h >= cfg.r1_h200_min)
            {
                "Met"
            } else {
                "Not met"
            })
            .into(),
            s("Both must hold for Risk-On"),
        ),
        (
            "R1_BREADTH".into(),
            "Risk-On breadth requirement".into(),
            cur_breadth,
            if cfg.breadth_required {
                format!("≥ {}%", pf(cfg.r1_breadth_min, ".0f"))
            } else {
                "not required".to_owned()
            },
            (if !cfg.breadth_required {
                "Not required"
            } else if !breadth_ev {
                "Not evaluable"
            } else if num(breadth_pct).is_some_and(|b| b >= cfg.r1_breadth_min) {
                "Met"
            } else {
                "Not met"
            })
            .into(),
            s("Breadth gates Risk-On when it is part of the model"),
        ),
    ];
    rows.into_iter()
        .map(|(code, label, current, threshold, status, note)| {
            let fired = codes.contains(&code);
            V::Map(dict([
                ("code", V::Str(code)),
                ("label", V::Str(label)),
                ("current", current),
                ("threshold", V::Str(threshold)),
                ("status", V::Str(status)),
                ("note", note),
                ("fired", V::Bool(fired)),
            ]))
        })
        .collect()
}

fn narrative(
    cards: &[PyDict],
    health: &PyDict,
    breadth_pct: &V,
    breadth_usable: bool,
    new_buys: &V,
    cfg: &RegimeConfig,
    codes: &[String],
) -> Vec<String> {
    let mut out = Vec::new();
    for c in cards {
        if truthy(&get(c, "is_sentinel")) {
            continue;
        }
        let below: Vec<i64> = list_of_maps(&get(c, "mas"))
            .iter()
            .filter(|m| as_str(&get(m, "state")) == Some("below"))
            .filter_map(|m| match m.get("length") {
                Some(V::Int(n)) => Some(*n),
                _ => None,
            })
            .collect();
        let name = as_str(&get(c, "index_name")).unwrap_or_default().to_owned();
        if below.contains(&200) {
            out.push(format!("{name} is confirmed below its 200-DMA."));
        } else if below.contains(&50) {
            out.push(format!("{name} is confirmed below its 50-DMA."));
        }
    }
    let h200 = get(health, "200");
    if let Some(h) = num(&h200).filter(|h| *h <= cfg.r3_h200_max) {
        out.push(format!(
            "Weighted 200-DMA health is {}, at or below the {} risk-off threshold.",
            pv(&h200, ".2f"),
            pf(cfg.r3_h200_max, ".2f")
        ));
        let _ = h;
    }
    if breadth_usable && !matches!(breadth_pct, V::None) && lt(breadth_pct, cfg.r3_breadth_max) {
        out.push(format!(
            "Breadth is {}%, below the {}% risk-off threshold.",
            pv(breadth_pct, ".0f"),
            pf(cfg.r3_breadth_max, ".0f")
        ));
    } else if codes.iter().any(|c| c == "BREADTH_NOT_USED") {
        out.push(
            "Breadth is not part of this configuration, so it neither forces risk-off nor gates Risk-On."
                .to_owned(),
        );
    } else if !breadth_usable {
        out.push(
            "There is no usable breadth reading, so breadth can neither force selling nor permit Risk-On."
                .to_owned(),
        );
    }
    if let Some(sent) = cards.iter().find(|c| truthy(&get(c, "is_sentinel"))) {
        let s50 = ma_of(Some(sent), 50);
        if as_str(&get(&s50, "state")) == Some("below") {
            out.push(format!(
                "{} remains below its 50-DMA ({}%).",
                as_str(&get(sent, "index_name")).unwrap_or_default(),
                pv(&or0(&get(&s50, "distance_pct")), "+.1f")
            ));
        }
    }
    let last = match as_str(new_buys) {
        Some("blocked") => "New purchases are therefore blocked.",
        Some("half") => "New purchases are permitted at half size.",
        Some("full") => "Full-size new purchases are permitted.",
        _ => "",
    };
    out.push(last.to_owned());
    out.into_iter().filter(|x| !x.is_empty()).collect()
}

fn banners(v: &PyDict) -> Vec<V> {
    let mut out = Vec::new();
    let b = |level: &str, t: &str, d: String| {
        V::Map(dict([
            ("level", s(level)),
            ("title", s(t)),
            ("detail", V::Str(d)),
        ]))
    };
    let tier = crate::templates::py_str_value(&crate::templates::py_value(&get(v, "policy_tier")));
    if truthy(&get(v, "data_stale")) {
        out.push(b(
            "warn",
            "Index data is stale",
            format!("Policy tier {tier} retained, new purchases blocked, and no forced regime sales generated."),
        ));
    }
    if !truthy(&get(v, "breadth_usable")) {
        out.push(b(
            "warn",
            "Breadth coverage insufficient",
            "Breadth cannot trigger forced selling and cannot permit Risk-On.".to_owned(),
        ));
    }
    if truthy(&get(v, "tax_data_incomplete")) {
        out.push(b(
            "warn",
            "Tax lot data incomplete",
            "Affected sales need manual review. No sale is assumed tax-safe.".to_owned(),
        ));
    }
    if truthy(&get(v, "manual_action_required")) {
        out.push(b(
            "warn",
            "Manual action required",
            "The exposure target cannot be reached automatically.".to_owned(),
        ));
    }
    if truthy(&get(v, "dry_run")) {
        out.push(b(
            "hold",
            "DRY_RUN is on",
            "No order can be submitted, no policy tier committed, and no rebalance marked executed."
                .to_owned(),
        ));
    }
    if as_str(&get(v, "mode")) == Some("observe") {
        out.push(b(
            "hold",
            "Observe mode",
            "Decisions are calculated and displayed only. No executable regime plan is produced."
                .to_owned(),
        ));
    }
    out
}

fn safe_state(v: &PyDict) -> V {
    let tier = crate::templates::py_str_value(&crate::templates::py_value(&get(v, "policy_tier")));
    if truthy(&get(v, "data_stale")) {
        return V::Str(format!(
            "Index data is stale. Policy tier {tier} retained, new purchases blocked, and no forced regime sales generated."
        ));
    }
    if !truthy(&get(v, "breadth_usable")) {
        return s(
            "Breadth is unusable, so it can neither trigger forced selling nor permit Risk-On. The tier is decided by structure and the momentum sentinel.",
        );
    }
    if !truthy(&get(v, "forced_actions_permitted")) {
        return s(
            "No prior policy state exists, so forced selling is suppressed. Proposed actions are advisory until a tier is committed.",
        );
    }
    V::None
}

fn mode_explainer(regime_enabled: bool, mode: &V, dry_run: bool) -> Vec<V> {
    let m = as_str(mode).unwrap_or_default();
    let mode_text = if truthy(mode) { m } else { "observe" };
    let state = match m {
        "propose" => "caution",
        "enforce" => "on",
        _ => "off",
    };
    let means = match m {
        "observe" => {
            "Watch only. Decisions are calculated and shown, but no executable plan is created."
        }
        "propose" => {
            "A plan is built and shown for your review. Nothing runs until you approve it."
        }
        "enforce" => "An approved plan may be sent to the broker through the normal order path.",
        _ => "Watch only.",
    };
    vec![
        V::Map(dict([
            ("label", s("Feature")),
            (
                "value",
                s(if regime_enabled {
                    "Enabled"
                } else {
                    "Disabled"
                }),
            ),
            ("state", s(if regime_enabled { "on" } else { "off" })),
            (
                "means",
                s(if regime_enabled {
                    "The regime overlay is switched on and may influence the rebalancer."
                } else {
                    "The regime overlay is switched off entirely. It calculates and displays a view, but changes nothing about how the portfolio is run."
                }),
            ),
        ])),
        V::Map(dict([
            ("label", s("Mode")),
            ("value", V::Str(title(mode_text))),
            ("state", s(state)),
            ("means", s(means)),
        ])),
        V::Map(dict([
            ("label", s("Orders")),
            ("value", s(if dry_run { "Dry run" } else { "Live" })),
            ("state", s(if dry_run { "off" } else { "on" })),
            (
                "means",
                s(if dry_run {
                    "Simulation. No order can reach the broker, no policy tier can be committed, and no rebalance can be marked executed."
                } else {
                    "Real orders can be placed when a plan is approved in enforce mode."
                }),
            ),
        ])),
    ]
}

/// `portfolio_block(conn)`: the actual book, from the most recent stored EOD snapshot.
///
/// # Errors
/// The driver's.
#[allow(clippy::too_many_lines)]
pub async fn portfolio_block(conn: &mut Conn) -> Result<PyDict, BuildError> {
    let series = conn
        .execute(
            "SELECT date, nav, invested, cash, index_value FROM snapshots ORDER BY date",
            &[],
        )
        .await?;
    let Some(last) = series.rows.last() else {
        return Ok(dict([("available", V::Bool(false))]));
    };
    let date = last.get("date").map_or(V::None, super::web::db_to_py);
    let date_text = crate::templates::py_str_value(&crate::templates::py_value(&date));
    let cur = conn
        .execute("SELECT * FROM snapshots WHERE date=?", &[date_text.into()])
        .await?;
    let latest = cur
        .fetchone()
        .map(super::web::row_dict)
        .ok_or_else(|| BuildError::Value("'NoneType' object is not subscriptable".to_owned()))?;
    let blob = loads(&get(&latest, "holdings_json"), "{}");
    let positions = list_of_maps(&get_or(&as_map(&blob), "positions", V::List(Vec::new())));
    let nav = float_or0(&get(&latest, "nav"));

    let mut out: Vec<PyDict> = Vec::new();
    for p in &positions {
        let value = float_or0(&get(p, "value"));
        let avg = float_or0(&get(p, "average_price"));
        let px = float_or0(&get(p, "price"));
        let excluded = truthy(&get(p, "excluded"));
        let pnl_pct = (avg > 0.0).then(|| (px / avg - 1.0) * 100.0);
        out.push(dict([
            ("symbol", get(p, "symbol")),
            ("quantity", get(p, "quantity")),
            ("average_price", V::Float(avg)),
            ("price", V::Float(px)),
            ("value", V::Float(value)),
            ("pledged_qty", get_or(p, "pledged_qty", V::Int(0))),
            ("excluded", V::Bool(excluded)),
            (
                "weight_pct",
                if excluded || nav == 0.0 {
                    V::None
                } else {
                    V::Float(value / nav * 100.0)
                },
            ),
            ("pnl_pct", opt_f(pnl_pct)),
            (
                "pnl_value",
                if avg > 0.0 {
                    V::Float((px - avg) * float_or0(&get(p, "quantity")))
                } else {
                    V::None
                },
            ),
        ]));
    }
    let value_of = |p: &PyDict| num(&get(p, "value")).unwrap_or(0.0);
    let tradeable: Vec<PyDict> = out
        .iter()
        .filter(|p| !truthy(&get(p, "excluded")))
        .cloned()
        .collect();
    let winners = tradeable
        .iter()
        .filter(|p| num(&or0(&get(p, "pnl_pct"))).is_some_and(|x| x > 0.0))
        .count();
    let sorted_by_value = |rows: &[PyDict]| {
        let mut v = rows.to_vec();
        v.sort_by(|a, b| {
            (-value_of(a))
                .partial_cmp(&-value_of(b))
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        V::List(v.into_iter().map(V::Map).collect())
    };
    let excluded_value = py_sum(
        out.iter()
            .filter(|p| truthy(&get(p, "excluded")))
            .map(|p| to_num(&get(p, "value"))),
    );
    let max_weight = if tradeable.is_empty() {
        V::Int(0)
    } else {
        py_max(
            &tradeable
                .iter()
                .map(|p| or0(&get(p, "weight_pct")))
                .collect::<Vec<_>>(),
        )
        .unwrap_or(V::Int(0))
    };
    let max_abs_pnl = py_max(
        &tradeable
            .iter()
            .map(|p| abs_v(&or0(&get(p, "pnl_pct"))))
            .collect::<Vec<_>>(),
    )
    .unwrap_or(V::Int(0));
    let total_pnl = py_sum(tradeable.iter().map(|p| to_num(&or0(&get(p, "pnl_value")))));
    let cash = float_or0(&get(&latest, "cash"));
    Ok(dict([
        ("available", V::Bool(true)),
        ("as_of", get(&latest, "date")),
        ("nav", V::Float(nav)),
        ("invested", V::Float(float_or0(&get(&latest, "invested")))),
        ("cash", V::Float(cash)),
        (
            "cash_pct",
            if nav == 0.0 {
                V::None
            } else {
                V::Float(cash / nav * 100.0)
            },
        ),
        ("count", ilen(tradeable.len())),
        ("excluded_count", ilen(out.len() - tradeable.len())),
        ("excluded_value", num_v(excluded_value)),
        ("positions", sorted_by_value(&out)),
        ("tradeable", sorted_by_value(&tradeable)),
        ("max_weight", max_weight),
        ("max_abs_pnl", max_abs_pnl),
        ("winners", ilen(winners)),
        ("losers", ilen(tradeable.len() - winners)),
        ("total_pnl", num_v(total_pnl)),
        (
            "pledged_count",
            ilen(
                tradeable
                    .iter()
                    .filter(|p| truthy(&get(p, "pledged_qty")))
                    .count(),
            ),
        ),
    ]))
}

/// `transition_history(conn, cfg, limit)`: one row per week, oldest first.
///
/// # Errors
/// The driver's, or a stored tier that is not a tier.
pub async fn transition_history(
    conn: &mut Conn,
    cfg: &RegimeConfig,
    limit: usize,
) -> Result<Vec<PyDict>, BuildError> {
    let cur = conn
        .execute(
            "SELECT e.*, x.actual_equity_pct FROM regime_evaluations e \
             LEFT JOIN (SELECT evaluation_id, actual_equity_pct FROM (\
                          SELECT evaluation_id, actual_equity_pct, ROW_NUMBER() OVER (\
                            PARTITION BY evaluation_id ORDER BY observed_at DESC) rn \
                          FROM regime_exposure) ranked WHERE rn = 1) x \
               ON x.evaluation_id = e.evaluation_id \
             ORDER BY e.scheduled_week_end DESC, (e.run_id = ?) DESC, e.id DESC",
            &[regime_store::CANONICAL.into()],
        )
        .await?;
    let mut seen: Vec<V> = Vec::new();
    let mut rows: Vec<PyDict> = Vec::new();
    for r in &cur.rows {
        let d = super::web::row_dict(r);
        let week = get(&d, "scheduled_week_end");
        if seen.contains(&week) {
            continue;
        }
        seen.push(week);
        rows.push(d);
        if rows.len() >= limit {
            break;
        }
    }
    let mut out = Vec::new();
    for r in &rows {
        let codes: Vec<String> = as_list(&loads(&get(r, "reason_codes_json"), "[]"))
            .iter()
            .filter_map(|c| as_str(c).map(str::to_owned))
            .collect();
        let trigger = codes
            .iter()
            .find(|c| is_affirmative_risk_off(c))
            .or_else(|| {
                codes
                    .iter()
                    .find(|c| c.starts_with("R1_") || *c == "R2_DEFAULT")
            })
            .cloned()
            .unwrap_or_else(|| "—".to_owned());
        let tier = get(r, "policy_tier");
        let cap = as_str(&tier)
            .and_then(|t| cfg.cap_for(t))
            .ok_or_else(|| BuildError::Value(format!("{tier:?} is not a valid RegimeTier")))?;
        out.push(dict([
            ("week", get(r, "scheduled_week_end")),
            ("raw", get(r, "raw_candidate_tier")),
            ("tier", tier),
            ("cap", V::Float(cap)),
            ("actual", get(r, "actual_equity_pct")),
            ("trigger", V::Str(trigger)),
            (
                "committed",
                V::Bool(as_str(&get(r, "run_id")) == Some(regime_store::CANONICAL)),
            ),
            ("breadth", get(r, "breadth_pct")),
            (
                "transition_limited",
                V::Bool(truthy(&get(r, "transition_limited"))),
            ),
        ]));
    }
    out.reverse();
    Ok(out)
}

/// `timeline_chart(history)` with its default geometry: cap, actual exposure and breadth on one
/// 0–100% axis, week by week; `None` below [`TIMELINE_MIN_WEEKS`].
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn timeline_chart(history: &[PyDict]) -> Option<PyDict> {
    let (width, height, pad_l, pad_r, pad_t, pad_b) =
        (700_i64, 170_i64, 40_i64, 46_i64, 12_i64, 26_i64);
    let rows: Vec<&PyDict> = history.iter().filter(|h| truthy(&get(h, "week"))).collect();
    let mut weeks: Vec<V> = Vec::new();
    for r in &rows {
        let w = get(r, "week");
        if !weeks.contains(&w) {
            weeks.push(w);
        }
    }
    if (weeks.len() as i64) < TIMELINE_MIN_WEEKS {
        return None;
    }
    let n = (rows.len().saturating_sub(1)).max(1) as f64;
    let (pw, ph) = (
        (width - pad_l - pad_r) as f64,
        (height - pad_t - pad_b) as f64,
    );
    let px = |i: usize| pad_l as f64 + i as f64 / n * pw;
    let py = |v: f64| {
        let m = if v < 100.0 { v } else { 100.0 };
        let c = if m > 0.0 { m } else { 0.0 };
        pad_t as f64 + (1.0 - c / 100.0) * ph
    };
    let line = |key: &str, step: bool| {
        let mut pts: Vec<String> = Vec::new();
        let mut pen = "M";
        for (i, h) in rows.iter().enumerate() {
            let v = get(h, key);
            let Some(x) = num(&v).or_else(|| match &v {
                V::Str(t) => crate::settings::py_float(t),
                _ => None,
            }) else {
                pen = "M";
                continue;
            };
            if step && !pts.is_empty() && pen == "L" {
                let prev = num(&get(rows[i - 1], key)).unwrap_or(0.0);
                pts.push(format!("L{},{}", pf(px(i), ".1f"), pf(py(prev), ".1f")));
            }
            pts.push(format!("{pen}{},{}", pf(px(i), ".1f"), pf(py(x), ".1f")));
            pen = "L";
        }
        pts.join(" ")
    };
    let week_text = |h: &PyDict| as_str(&get(h, "week")).unwrap_or_default().to_owned();
    let stride = (rows.len() / 6).max(1);
    Some(dict([
        ("width", V::Int(width)),
        ("height", V::Int(height)),
        ("pad_l", V::Int(pad_l)),
        ("pad_t", V::Int(pad_t)),
        ("weeks", ilen(rows.len())),
        ("cap", V::Str(line("cap", true))),
        ("actual", V::Str(line("actual", false))),
        ("breadth", V::Str(line("breadth", false))),
        (
            "ticks",
            V::List(
                [0_i64, 50, 100]
                    .iter()
                    .map(|v| {
                        V::Map(dict([
                            ("v", V::Int(*v)),
                            ("px", V::Float(py_round(py(*v as f64), 1))),
                        ]))
                    })
                    .collect(),
            ),
        ),
        (
            "xticks",
            V::List(
                rows.iter()
                    .enumerate()
                    .filter(|(i, _)| rows.len() <= 8 || i % stride == 0)
                    .map(|(i, h)| {
                        let w = week_text(h);
                        V::Map(dict([
                            ("px", V::Float(py_round(px(i), 1))),
                            ("label", V::Str(w.chars().skip(5).collect())),
                        ]))
                    })
                    .collect(),
            ),
        ),
        ("first", get(rows[0], "week")),
        ("last", get(rows[rows.len() - 1], "week")),
        (
            "omitted",
            s("Momentum 50 distance is not stored per evaluation"),
        ),
    ]))
}

/// `headline_reason(reason_pairs, narrative)`.
#[must_use]
pub fn headline_reason(reason_pairs: &[PyDict], narrative: &[String]) -> String {
    if let Some(p) = reason_pairs.iter().find(|p| truthy(&get(p, "risk_off"))) {
        return as_str(&get(p, "text")).unwrap_or_default().to_owned();
    }
    for prefix in ["R4_", "R3_", "R2_", "R1_ALL_CONDITIONS_MET", "R2_DEFAULT"] {
        for p in reason_pairs {
            if as_str(&get(p, "code"))
                .unwrap_or_default()
                .starts_with(prefix)
            {
                return as_str(&get(p, "text")).unwrap_or_default().to_owned();
            }
        }
    }
    narrative.first().cloned().unwrap_or_else(|| "—".to_owned())
}

fn holding_counts(
    portfolio: &PyDict,
    tier_cap: Option<f64>,
    proposed: &[PyDict],
    k: &DeskConstants,
) -> PyDict {
    let rows: Vec<PyDict> = list_of_maps(&get(portfolio, "tradeable"))
        .into_iter()
        .filter(|p| !truthy(&get(p, "excluded")))
        .collect();
    if rows.is_empty() {
        let n = proposed
            .iter()
            .filter(|o| num(&get_or(o, "qty_now", V::Int(0))).is_some_and(|q| q > 0.0))
            .count();
        return dict([
            ("holdings_now", if n == 0 { V::None } else { ilen(n) }),
            ("full_sized", V::None),
            ("half_sized", V::List(Vec::new())),
            ("largest_position", V::None),
            ("holdings_source", if n == 0 { V::None } else { s("plan") }),
        ]);
    }
    let cap = tier_cap.filter(|c| *c != 0.0).unwrap_or(100.0);
    let sleeve = cap / 100.0;
    let (mut full, mut half, mut thin) = (0_i64, Vec::new(), Vec::new());
    for p in &rows {
        let w = float_or0(&get(p, "weight_pct"));
        let sleeve_w = if sleeve == 0.0 { w } else { w / sleeve };
        let entry = V::Map(dict([
            ("symbol", get(p, "symbol")),
            ("weight_pct", V::Float(py_round(w, 2))),
            ("sleeve_pct", V::Float(py_round(sleeve_w, 2))),
        ]));
        if sleeve_w + 1e-9 >= k.min_position_weight {
            full += 1;
        } else if sleeve_w + 1e-9 >= k.half_size_weight {
            half.push(entry);
        } else {
            thin.push(entry);
        }
    }
    let mut top = &rows[0];
    for p in &rows[1..] {
        if float_or0(&get(p, "value")) > float_or0(&get(top, "value")) {
            top = p;
        }
    }
    let top_w = float_or0(&get(top, "weight_pct"));
    dict([
        ("holdings_now", ilen(rows.len())),
        ("full_sized", V::Int(full)),
        ("half_sized", V::List(half)),
        ("below_half", V::List(thin)),
        (
            "largest_position",
            V::Map(dict([
                ("symbol", get(top, "symbol")),
                ("weight_pct", V::Float(py_round(top_w, 2))),
                (
                    "weight_sleeve",
                    V::Float(py_round(
                        if sleeve == 0.0 { top_w } else { top_w / sleeve },
                        2,
                    )),
                ),
                (
                    "value",
                    crate::lanes::weekly::pandas::py_round_int(float_or0(&get(top, "value")))
                        .map_or(V::None, V::Int),
                ),
            ])),
        ),
        ("holdings_source", s("portfolio")),
    ])
}

async fn store_status(conn: &mut Conn, schema: &str, today: NaiveDate) -> PyDict {
    let mut out = dict([
        ("schema_version", V::None),
        ("journal_mode", V::None),
        ("db_bytes", V::None),
        ("last_collection", V::None),
        ("last_collection_outcome", V::None),
        ("consecutive_failures", V::Int(0)),
        ("last_index_write", V::None),
    ]);
    out.insert(
        "schema_version",
        conn.schema_version().await.map_or(V::None, V::Int),
    );
    if conn.is_postgres() {
        out.insert("journal_mode", s("postgres/wal"));
        let bytes = conn
            .execute(
                "SELECT sum(pg_total_relation_size(c.oid)) FROM pg_class c \
                 JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = ?",
                &[schema.into()],
            )
            .await
            .ok()
            .and_then(|c| c.fetchone().and_then(|r| r.at(0)).cloned());
        #[allow(clippy::cast_possible_truncation)]
        let v = match bytes {
            Some(DbValue::Int(i)) => V::Int(i),
            Some(DbValue::Float(f)) => V::Int(f.trunc() as i64),
            Some(DbValue::Null) => V::Int(0),
            _ => V::None,
        };
        out.insert("db_bytes", v);
    } else {
        let mode = conn
            .execute("PRAGMA journal_mode", &[])
            .await
            .ok()
            .and_then(|c| c.fetchone().and_then(|r| r.at(0)).map(super::web::db_to_py));
        out.insert("journal_mode", mode.unwrap_or(V::None));
        let mut size = None;
        if let Ok(a) = conn.execute("PRAGMA page_size", &[]).await
            && let Some(ps) = a.fetchone().and_then(|r| r.at(0)).and_then(DbValue::as_i64)
            && let Ok(b) = conn.execute("PRAGMA page_count", &[]).await
            && let Some(pc) = b.fetchone().and_then(|r| r.at(0)).and_then(DbValue::as_i64)
        {
            size = Some(ps * pc);
        }
        out.insert("db_bytes", size.map_or(V::None, V::Int));
    }
    let last_write = conn
        .execute("SELECT MAX(updated_at) m FROM index_series", &[])
        .await
        .ok()
        .and_then(|c| {
            c.fetchone()
                .and_then(|r| r.get("m"))
                .map(super::web::db_to_py)
        });
    out.insert("last_index_write", last_write.unwrap_or(V::None));
    match daily_runs::status(conn, today).await {
        Ok(st) => {
            out.insert("last_collection", get(&st, "ran_at"));
            out.insert("last_collection_outcome", get(&st, "outcome"));
            out.insert(
                "consecutive_failures",
                get_or(&st, "consecutive_failures", V::Int(0)),
            );
            out.insert("collection_healthy", get_or(&st, "healthy", V::Bool(false)));
        }
        Err(_) => out.insert("collection_healthy", V::None),
    }
    out
}

async fn plan_link(conn: &mut Conn, evaluation_id: &V) -> V {
    let eid = as_str(evaluation_id).filter(|e| !e.is_empty());
    let mut linked = false;
    let mut row = None;
    if let Some(e) = eid {
        match plan_store::latest(conn, Some(e)).await {
            Ok(r) => {
                linked = r.is_some();
                row = r;
            }
            Err(_) => return V::None,
        }
    }
    if row.is_none() {
        match plan_store::latest(conn, None).await {
            Ok(r) => row = r,
            Err(_) => return V::None,
        }
    }
    let Some(row) = row else { return V::None };
    let plan_id = as_str(&get(&row, "version_id"))
        .unwrap_or_default()
        .to_owned();
    let Ok(recon) = plan_store::reconciliation(conn, &plan_id).await else {
        return V::None;
    };
    let mut out = recon;
    out.insert("plan_id", get(&row, "version_id"));
    out.insert("linked", V::Bool(linked));
    out.insert("created_at", get(&row, "created_ts"));
    out.insert("note", get(&row, "note"));
    out.insert("evaluation_id", get(&row, "evaluation_id"));
    V::Map(out)
}

// ---------------------------------------------------------------------------------------------
// enriched action rows and per-lot tax review
// ---------------------------------------------------------------------------------------------

/// `int(x)` for `o.get(k) or 0`: falsy is 0; a float truncates; a string parses; else raises.
fn py_int_or0(v: &V) -> Result<i64, BuildError> {
    if !truthy(v) {
        return Ok(0);
    }
    match v {
        V::Int(i) => Ok(*i),
        V::Bool(b) => Ok(i64::from(*b)),
        V::Float(f) if f.is_finite() => {
            #[allow(clippy::cast_possible_truncation)]
            Ok(f.trunc() as i64)
        }
        V::Float(f) => Err(BuildError::Value(format!(
            "cannot convert float {} to integer",
            baskfy_core::py::float_repr(*f)
        ))),
        V::Str(x) => crate::settings::py_int(x).ok_or_else(|| {
            BuildError::Value(format!("invalid literal for int() with base 10: '{x}'"))
        }),
        other => Err(BuildError::Value(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            other.type_name()
        ))),
    }
}

/// `float(x)` for `o.get(k) or 0.0`.
fn py_float_or0(v: &V) -> Result<f64, BuildError> {
    if !truthy(v) {
        return Ok(0.0);
    }
    match v {
        V::Float(f) => Ok(*f),
        #[allow(clippy::cast_precision_loss)]
        V::Int(i) => Ok(*i as f64),
        V::Bool(b) => Ok(f64::from(u8::from(*b))),
        V::Str(x) => crate::settings::py_float(x).ok_or_else(|| {
            BuildError::Value(format!("could not convert string to float: '{x}'"))
        }),
        other => Err(BuildError::Value(format!(
            "float() argument must be a string or a real number, not '{}'",
            other.type_name()
        ))),
    }
}

/// `(o.get("delta") or 0) < 0` (and `> 0`): numbers compare, a string raises.
fn delta_sign(o: &PyDict) -> Result<f64, BuildError> {
    let d = get(o, "delta");
    if !truthy(&d) {
        return Ok(0.0);
    }
    num(&d).ok_or_else(|| {
        BuildError::Value(format!(
            "'<' not supported between instances of '{}' and 'int'",
            d.type_name()
        ))
    })
}

fn symbol_of(o: &PyDict) -> Result<String, BuildError> {
    match o.get("symbol") {
        Some(V::Str(x)) => Ok(x.clone()),
        Some(other) => Ok(other.py_str()),
        None => Err(BuildError::Value("KeyError: 'symbol'".to_owned())),
    }
}

/// `PS.orders_frame(conn, plan_id).to_dict("records")` for the two columns the page reads:
/// pandas' dtype inference included — an integer column with a missing value becomes `float64`
/// (`5.0`, `NaN`), an all-missing one stays `None`.
async fn order_states(conn: &mut Conn, plan_id: &str) -> Vec<(String, PyDict)> {
    let Ok(rows) = plan_store::orders_rows(conn, Some(plan_id)).await else {
        return Vec::new();
    };
    let column = |k: &str| -> Vec<V> { rows.iter().map(|r| get(r, k)).collect() };
    let pandas = |vals: Vec<V>| -> Vec<V> {
        let any_none = vals.iter().any(|v| matches!(v, V::None));
        let all_num = vals
            .iter()
            .all(|v| matches!(v, V::None | V::Int(_) | V::Float(_)));
        let any_num = vals.iter().any(|v| matches!(v, V::Int(_) | V::Float(_)));
        let any_float = vals.iter().any(|v| matches!(v, V::Float(_)));
        if all_num && any_num && (any_none || any_float) {
            vals.into_iter()
                .map(|v| match v {
                    #[allow(clippy::cast_precision_loss)]
                    V::Int(i) => V::Float(i as f64),
                    V::None => V::Float(f64::NAN),
                    other => other,
                })
                .collect()
        } else {
            vals
        }
    };
    let statuses = pandas(column("status"));
    let filled = pandas(column("filled_qty"));
    let mut out: Vec<(String, PyDict)> = Vec::new();
    for ((r, st), fq) in rows.iter().zip(statuses).zip(filled) {
        let sym = get(r, "symbol").py_str();
        let rec = dict([("status", st), ("filled_qty", fq)]);
        if let Some(slot) = out.iter_mut().find(|(k, _)| *k == sym) {
            slot.1 = rec;
        } else {
            out.push((sym, rec));
        }
    }
    out
}

/// `enrich_orders(conn, orders, portfolio, cfg, plan_id=None)`: each proposed order joined to the
/// position it touches. Every source is optional — a missing one leaves its fields `None` rather
/// than removing the row, because an order you cannot fully assess still has to be visible.
///
/// # Errors
/// [`BuildError::Value`] where Python raises on a malformed stored order.
#[allow(clippy::too_many_arguments)]
pub async fn enrich_orders(
    conn: &mut Conn,
    orders: &[PyDict],
    portfolio: &PyDict,
    max_pos_vs_day_value: f64,
    review_days: i64,
    today: NaiveDate,
    plan_id: Option<&str>,
) -> Result<Vec<PyDict>, BuildError> {
    use crate::lanes::weekly::weekly_books::tax_lots;
    let tradeable = list_of_maps(&get(portfolio, "tradeable"));
    let mut held: Vec<(String, PyDict)> = Vec::new();
    for p in tradeable {
        let sym = symbol_of(&p)?;
        if let Some(slot) = held.iter_mut().find(|(k, _)| *k == sym) {
            slot.1 = p;
        } else {
            held.push((sym, p));
        }
    }
    let mut sells: Vec<(String, (i64, f64))> = Vec::new();
    for o in orders {
        if delta_sign(o)? < 0.0 {
            let sym = symbol_of(o)?;
            let leg = (
                py_int_or0(&get(o, "delta"))?.abs(),
                py_float_or0(&get(o, "ref_price"))?,
            );
            if let Some(slot) = sells.iter_mut().find(|(k, _)| *k == sym) {
                slot.1 = leg;
            } else {
                sells.push((sym, leg));
            }
        }
    }
    let mut reviews: Vec<(String, tax_lots::TaxReview)> = Vec::new();
    if !sells.is_empty() {
        // Deliberately not a bare except: a review that fails leaves every sale `None`, never
        // tax-clear.
        match tax_lots::review_sales(conn, &sells, today, review_days).await {
            Ok(r) => reviews = r,
            Err(e) => {
                let mut names: Vec<&String> = sells.iter().map(|(k, _)| k).collect();
                names.sort();
                #[allow(clippy::print_stderr)]
                {
                    eprintln!("WARNING:root:tax review failed for {names:?}: {e}");
                }
            }
        }
    }
    let states = match plan_id.filter(|p| !p.is_empty()) {
        Some(p) => order_states(conn, p).await,
        None => Vec::new(),
    };
    let mut out = Vec::with_capacity(orders.len());
    for o in orders {
        let sym = symbol_of(o)?;
        let p = held
            .iter()
            .find(|(k, _)| *k == sym)
            .map(|(_, v)| v.clone())
            .unwrap_or_default();
        let rv = reviews.iter().find(|(k, _)| *k == sym).map(|(_, r)| r);
        let st = states
            .iter()
            .find(|(k, _)| *k == sym)
            .map(|(_, v)| v.clone())
            .unwrap_or_default();
        let soonest = rv.and_then(|r| {
            r.consumptions
                .iter()
                .filter_map(|c| c.days_to_ltcg)
                .filter(|d| *d > 0)
                .min()
        });
        let value = py_float_or0(&get(o, "delta"))?.abs() * py_float_or0(&get(o, "ref_price"))?;
        let day_value = py_float_or0(&get(&p, "median_day_value"))?;
        let mut row = o.clone();
        row.insert("weight_now", get(&p, "weight_pct"));
        row.insert("weight_target", get(o, "weight_nav"));
        row.insert("pnl_pct", get(&p, "pnl_pct"));
        row.insert("pnl_value", get(&p, "pnl_value"));
        row.insert("days_to_ltcg", soonest.map_or(V::None, V::Int));
        row.insert(
            "tax_flagged",
            rv.map_or(V::None, |r| V::Bool(r.manual_review())),
        );
        row.insert(
            "tax_codes",
            V::List(
                rv.map(|r| {
                    r.codes
                        .iter()
                        .map(|c| V::Str((*c).to_owned()))
                        .collect()
                })
                .unwrap_or_default(),
            ),
        );
        row.insert(
            "tax_gain",
            rv.and_then(|r| r.estimated_gain).map_or(V::None, V::Float),
        );
        row.insert(
            "liquidity_warning",
            V::Bool(!super::regime::engine::feq(day_value, 0.0) && value > day_value * max_pos_vs_day_value),
        );
        row.insert("order_status", get(&st, "status"));
        row.insert("filled_qty", get(&st, "filled_qty"));
        out.push(row);
    }
    Ok(out)
}

/// `tax_detail(conn, orders)`: the FIFO lots each proposed sale would consume, with what each
/// realises — for every sale, not only the flagged ones. A sale whose lots cannot be read is
/// skipped (logged), as in Python.
///
/// # Errors
/// [`BuildError::Value`] where Python raises on a malformed stored order.
pub async fn tax_detail(
    conn: &mut Conn,
    orders: &[PyDict],
    review_days: i64,
    today: NaiveDate,
) -> Result<Vec<PyDict>, BuildError> {
    use crate::lanes::weekly::weekly_books::tax_lots;
    let mut out = Vec::new();
    for o in orders {
        let qty = -py_int_or0(&get(o, "delta"))?;
        if qty <= 0 {
            continue;
        }
        let sym = symbol_of(o)?;
        let px = py_float_or0(&get(o, "ref_price"))?;
        let lots = match tax_lots::open_lots(conn, &sym).await {
            Ok(l) => l,
            Err(e) => {
                #[allow(clippy::print_stderr)]
                {
                    eprintln!("WARNING:root:lot detail failed for {sym}: {e}");
                }
                continue;
            }
        };
        let rv = tax_lots::review_sale(&sym, qty, px, &lots, today, review_days);
        let lot_rows: Vec<PyDict> = rv
            .consumptions
            .iter()
            .map(|c| {
                dict([
                    ("quantity", V::Int(c.quantity)),
                    (
                        "acquired_on",
                        c.lot.acquired_on.map_or(V::None, |d| V::Str(iso(d))),
                    ),
                    ("acquired_at", c.lot.price.map_or(V::None, V::Float)),
                    ("days_held", c.days_held.map_or(V::None, V::Int)),
                    ("days_to_ltcg", c.days_to_ltcg.map_or(V::None, V::Int)),
                    ("gain", c.gain.map_or(V::None, V::Float)),
                    ("long_term", c.long_term.map_or(V::None, V::Bool)),
                    (
                        "codes",
                        V::List(c.codes.iter().map(|x| V::Str((*x).to_owned())).collect()),
                    ),
                ])
            })
            .collect();
        let short_term: i64 = rv
            .consumptions
            .iter()
            .filter(|c| c.long_term == Some(false))
            .map(|c| c.quantity)
            .sum();
        let long_term: i64 = rv
            .consumptions
            .iter()
            .filter(|c| c.long_term == Some(true))
            .map(|c| c.quantity)
            .sum();
        out.push(dict([
            ("symbol", V::Str(sym.clone())),
            ("requested_qty", V::Int(rv.requested_qty)),
            ("covered_qty", V::Int(rv.covered_qty)),
            (
                "uncovered_qty",
                V::Int((rv.requested_qty - rv.covered_qty).max(0)),
            ),
            ("estimated_gain", rv.estimated_gain.map_or(V::None, V::Float)),
            ("flagged", V::Bool(rv.flagged)),
            ("data_unknown", V::Bool(rv.data_unknown)),
            (
                "codes",
                V::List(rv.codes.iter().map(|x| V::Str((*x).to_owned())).collect()),
            ),
            ("manual_review", V::Bool(rv.manual_review())),
            ("lots", V::List(lot_rows.into_iter().map(V::Map).collect())),
            ("short_term_qty", V::Int(short_term)),
            ("long_term_qty", V::Int(long_term)),
        ]));
    }
    Ok(out)
}

// ---------------------------------------------------------------------------------------------
// build
// ---------------------------------------------------------------------------------------------

/// `build(conn)`: everything `/regime` renders and `/regime/data` returns.
///
/// # Errors
/// [`BuildError`] where Python raises (a 500).
#[allow(clippy::too_many_lines)]
pub async fn build(
    conn: &mut Conn,
    settings: &Settings,
    k: &DeskConstants,
    schema: &str,
    today: NaiveDate,
) -> Result<PyDict, BuildError> {
    // `cfg = C.regime_config()` is build's first line, before the empty-store answer.
    if k.weekday_override_is_text {
        return Err(BuildError::Value(
            "TypeError: '<=' not supported between instances of 'int' and 'str'".to_owned(),
        ));
    }
    let cfg = RegimeConfig::from_settings(settings)?;
    let (ev, committed) = latest_evaluation(conn).await?;
    if ev.is_empty() {
        return Ok(dict([
            ("available", V::Bool(false)),
            ("message", s("No regime evaluation has been recorded yet.")),
            ("dry_run", V::Bool(settings.dry_run)),
            ("mode", V::Str(settings.regime_mode.clone())),
            ("regime_enabled", V::Bool(settings.regime_enabled)),
            (
                "how_to",
                s("python -m app.analytics.snapshot   then run an evaluation"),
            ),
        ]));
    }

    let snapshot = as_map(&loads(&get(&ev, "input_snapshot_json"), "{}"));
    let diagnostics = as_map(&get_or(
        &snapshot,
        "index_diagnostics",
        V::Map(PyDict::new()),
    ));
    let health = as_map(&get_or(&snapshot, "health", V::Map(PyDict::new())));
    let codes: Vec<String> = as_list(&loads(&get(&ev, "reason_codes_json"), "[]"))
        .iter()
        .filter_map(|c| as_str(c).map(str::to_owned))
        .collect();
    let reasons: Vec<V> = as_list(&loads(&get(&ev, "reasons_json"), "[]"));
    let weights = loads(&get(&ev, "book_weights_json"), "{}");
    let plan_v = get_or(&snapshot, "plan", V::Map(PyDict::new()));
    let plan = as_map(&plan_v);
    let proposed = list_of_maps(&get_or(&snapshot, "proposed_orders", V::List(Vec::new())));
    let manual_items = list_of_maps(&get_or(
        &snapshot,
        "manual_review_items",
        V::List(Vec::new()),
    ));
    let no_plan_reason = get(&snapshot, "no_plan_reason");

    let as_of = date_of(&get(&ev, "data_as_of"))?;
    let evaluation_id = get(&ev, "evaluation_id");
    let exposure = regime_store::latest_exposure(conn, as_str(&evaluation_id))
        .await?
        .unwrap_or_default();
    let breadth_row = breadth::latest_reading(conn, "scan", None).await?;
    let names: Vec<String> = cfg
        .structural_indices
        .iter()
        .map(|(_, v)| v.clone())
        .chain(std::iter::once(cfg.momentum_sentinel.clone()))
        .collect();
    let status_rows = index_status(conn, &names, as_of, settings.regime_index_stale_days).await?;

    let tier = get(&ev, "policy_tier");
    let tier_cap = if truthy(&tier) {
        Some(
            as_str(&tier)
                .and_then(|t| cfg.cap_for(t))
                .ok_or_else(|| BuildError::Value(format!("{tier:?} is not a valid RegimeTier")))?,
        )
    } else {
        None
    };
    let stored_cap = num(&get(&exposure, "target_equity_cap_pct"));
    let cap_mismatch = matches!((stored_cap, tier_cap), (Some(a), Some(b)) if (a - b).abs() > 1e-6);
    let actual_v = get(&exposure, "actual_equity_pct");
    let actual = num(&actual_v);
    let gap = match (actual, tier_cap) {
        (Some(a), Some(c)) => Some(py_round(a - c, 4)),
        _ => None,
    };
    let reduction = gap.map(|g| if 0.0 > g { 0.0 } else { g });

    let breadth_pct = get(&ev, "breadth_pct");
    let breadth_cov = get(&ev, "breadth_coverage_pct");
    let breadth_usable = !matches!(breadth_pct, V::None)
        && num(&breadth_cov).is_some_and(|c| c >= cfg.min_breadth_coverage_pct);
    let band = num(&breadth_pct).and_then(|b| {
        BREADTH_BANDS
            .iter()
            .find(|(lo, hi, ..)| *lo <= b && b < *hi)
    });

    let spark = spark_series(conn, &names).await;
    let cards = index_cards(&diagnostics, &cfg, &status_rows, &spark);
    let sentinel_card = cards
        .iter()
        .find(|c| truthy(&get(c, "is_sentinel")))
        .cloned();
    let s50 = ma_of(sentinel_card.as_ref(), 50);
    let s200 = ma_of(sentinel_card.as_ref(), 200);
    let veto_active = as_str(&get(&s50, "state")) == Some("below");

    let any_stale =
        truthy(&get(&ev, "data_stale")) || status_rows.iter().any(|r| truthy(&get(r, "stale")));
    let tax_incomplete = manual_items.iter().any(|m| {
        as_list(&or_list(&get(m, "codes")))
            .iter()
            .any(|c| as_str(c) == Some("TAX_DATA_UNKNOWN"))
    });
    let tier_text = as_str(&tier).unwrap_or_default();
    let (human, key, meaning) = tier_meta(tier_text).map_or_else(
        || {
            (
                if truthy(&tier) {
                    crate::templates::py_str_value(&crate::templates::py_value(&tier))
                } else {
                    "—".to_owned()
                },
                "caution".to_owned(),
                String::new(),
            )
        },
        |(a, b, c)| (a.to_owned(), b.to_owned(), c.to_owned()),
    );
    let raw_tier = get(&ev, "raw_candidate_tier");
    let dry_run = ev.get("dry_run").map_or(settings.dry_run, truthy);
    let mode = get_or(&ev, "mode", V::Str(settings.regime_mode.clone()));
    let new_buys = get(&ev, "new_buys");

    let reason_pairs: Vec<PyDict> = codes
        .iter()
        .enumerate()
        .filter_map(|(i, c)| {
            let t = if reasons.is_empty() {
                Some(V::Str(c.clone()))
            } else {
                reasons.get(i).cloned()
            }?;
            Some(dict([
                ("code", V::Str(c.clone())),
                ("text", display_reason(c).map_or(t, s)),
                ("risk_off", V::Bool(is_affirmative_risk_off(c))),
            ]))
        })
        .collect();
    let narrative_lines = narrative(
        &cards,
        &health,
        &breadth_pct,
        breadth_usable,
        &new_buys,
        &cfg,
        &codes,
    );
    let sum_orders = |sign: i64| {
        let picked: Vec<Num> = proposed
            .iter()
            .filter(|o| {
                num(&get_or(o, "delta", V::Int(0)))
                    .is_some_and(|d| d * f64::from(i32::try_from(sign).unwrap_or(1)) > 0.0)
            })
            .map(|o| to_num(&abs_v(&get(o, "delta"))).mul(to_num(&get(o, "ref_price"))))
            .collect();
        let total = py_sum(picked);
        if num(&num_v(total)).is_some_and(|x| x != 0.0) {
            num_v(total)
        } else {
            V::None
        }
    };
    let cfg_hash = cfg.config_hash();
    let structural_cards: Vec<V> = cards
        .iter()
        .filter(|c| !truthy(&get(c, "is_sentinel")))
        .cloned()
        .map(V::Map)
        .collect();
    let rebalance_plan = plan_link(conn, &evaluation_id).await;
    let store = store_status(conn, schema, today).await;
    let delta_of = |o: &PyDict| num(&get_or(o, "delta", V::Int(0))).unwrap_or(0.0);

    let mut v = dict([
        ("available", V::Bool(true)),
        ("committed", V::Bool(committed)),
        ("run_id", get(&ev, "run_id")),
        ("dry_run", V::Bool(dry_run)),
        ("mode", mode.clone()),
        ("regime_enabled", V::Bool(settings.regime_enabled)),
        ("signal_session_date", get(&ev, "signal_session_date")),
        ("scheduled_week_end", get(&ev, "scheduled_week_end")),
        ("data_as_of", get(&ev, "data_as_of")),
        ("next_evaluation_date", get(&ev, "next_evaluation_date")),
        ("last_transition_date", get(&ev, "last_transition_date")),
        ("algorithm_version", get(&ev, "algorithm_version")),
        ("config_hash", get(&ev, "config_hash")),
        ("policy_tier", tier.clone()),
        ("tier_name", V::Str(human)),
        ("tier_key", V::Str(key)),
        ("tier_meaning", V::Str(meaning)),
        ("raw_candidate_tier", raw_tier.clone()),
        (
            "raw_candidate_name",
            s(as_str(&raw_tier).and_then(tier_meta).map_or("—", |m| m.0)),
        ),
        ("previous_policy_tier", get(&ev, "previous_policy_tier")),
        (
            "transition_limited",
            V::Bool(truthy(&get(&ev, "transition_limited"))),
        ),
        ("new_buys", new_buys.clone()),
        ("new_buys_label", s(new_buy_label(&new_buys))),
        ("forced_action", get(&ev, "forced_action")),
        (
            "forced_action_label",
            s(forced_label(&get(&ev, "forced_action"))),
        ),
        (
            "override_active",
            V::Bool(truthy(&get(&ev, "override_active"))),
        ),
        ("sentinel_veto", V::Bool(veto_active)),
        (
            "manual_action_required",
            V::Bool(truthy(&get(&ev, "manual_action_required"))),
        ),
        ("target_equity_cap_pct", opt_f(tier_cap)),
        ("actual_equity_pct", actual_v.clone()),
        ("exposure_gap_pct", opt_f(gap)),
        ("required_reduction_pct", opt_f(reduction)),
        (
            "current_cash_pct",
            opt_f(actual.map(|a| py_round(100.0 - a, 4))),
        ),
        ("pending_buy_pct", get(&exposure, "pending_buy_pct")),
        ("pending_sell_pct", get(&exposure, "pending_sell_pct")),
        (
            "execution_status",
            get_or(&exposure, "execution_status", s("not_planned")),
        ),
        ("exposure_observed_at", get(&exposure, "observed_at")),
        ("exposure_cap_mismatch", V::Bool(cap_mismatch)),
        (
            "bar",
            V::Map(dict([
                (
                    "within_cap",
                    match (actual, tier_cap) {
                        (Some(a), Some(c)) => {
                            if c < a {
                                V::Float(c)
                            } else {
                                actual_v.clone()
                            }
                        }
                        _ => V::None,
                    },
                ),
                ("excess", opt_f(reduction)),
                ("cash", opt_f(actual.map(|a| py_round(100.0 - a, 4)))),
                ("cap_marker", opt_f(tier_cap)),
            ])),
        ),
        ("positions_target", get(&plan, "positions")),
        (
            "target_position_range",
            V::List(vec![
                V::Int(k.target_positions.0),
                V::Int(k.target_positions.1),
            ]),
        ),
        (
            "cluster_weights",
            get_or(&plan, "cluster_weights", V::Map(PyDict::new())),
        ),
        ("cluster_cap", k.cluster_cap.clone()),
        ("largest_position", get(&plan, "largest_position")),
        (
            "allocation_feasible",
            if truthy(&plan_v) {
                V::Bool(matches!(get(&plan, "infeasible_reason"), V::None))
            } else {
                V::None
            },
        ),
        (
            "allocation_notes",
            get_or(&plan, "allocation_notes", V::List(Vec::new())),
        ),
        ("plan_summary", plan_v.clone()),
        (
            "gates",
            V::List(gates(
                &health,
                &breadth_pct,
                breadth_usable,
                sentinel_card.as_ref(),
                &get(&snapshot, "bearish_stack_weight"),
                &cfg,
                &codes,
            )),
        ),
        (
            "narrative",
            V::List(narrative_lines.iter().map(|x| V::Str(x.clone())).collect()),
        ),
        (
            "reason_pairs",
            V::List(reason_pairs.iter().cloned().map(V::Map).collect()),
        ),
        (
            "reason_codes",
            V::List(codes.iter().map(|c| V::Str(c.clone())).collect()),
        ),
        (
            "reasons",
            if reasons.is_empty() {
                V::List(
                    codes
                        .iter()
                        .map(|c| s(display_reason(c).unwrap_or(c)))
                        .collect(),
                )
            } else {
                V::List(reasons.clone())
            },
        ),
        (
            "index_cards",
            V::List(cards.iter().cloned().map(V::Map).collect()),
        ),
        ("structural_cards", V::List(structural_cards)),
        (
            "sentinel_card",
            sentinel_card.clone().map_or(V::None, V::Map),
        ),
        (
            "health",
            V::Map(dict(["20", "50", "200"].map(|k2| (k2, get(&health, k2))))),
        ),
        (
            "bearish_stack_weight",
            get(&snapshot, "bearish_stack_weight"),
        ),
        ("book_weights", weights),
        ("book_weight_source", get(&ev, "book_weight_source")),
        (
            "book_weights_are_defaults",
            V::Bool(as_str(&get(&ev, "book_weight_source")) == Some("config_default")),
        ),
        ("breadth_pct", breadth_pct.clone()),
        ("breadth_coverage_pct", breadth_cov.clone()),
        ("breadth_usable", V::Bool(breadth_usable)),
        ("breadth_band", band.map_or(V::None, |b| s(b.2))),
        ("breadth_band_key", band.map_or(V::None, |b| s(b.3))),
        (
            "breadth_bands",
            V::List(
                BREADTH_BANDS
                    .iter()
                    .map(|b| {
                        V::Map(dict([
                            ("lo", V::Float(b.0)),
                            ("hi", V::Float(b.1)),
                            ("label", s(b.2)),
                            ("key", s(b.3)),
                        ]))
                    })
                    .collect(),
            ),
        ),
        (
            "breadth_observed",
            breadth_row
                .as_ref()
                .map_or(V::None, |b| V::Int(b.observed_count)),
        ),
        (
            "breadth_eligible",
            breadth_row
                .as_ref()
                .map_or(V::None, |b| V::Int(b.eligible_count)),
        ),
        (
            "breadth_as_of",
            breadth_row
                .as_ref()
                .map_or(V::None, |b| V::Str(iso(b.as_of_date))),
        ),
        (
            "breadth_universe",
            breadth_row
                .as_ref()
                .map_or(V::None, |b| V::Str(b.universe_id.clone())),
        ),
        (
            "breadth_universe_hash",
            breadth_row
                .as_ref()
                .map_or(V::None, |b| V::Str(b.universe_hash.clone())),
        ),
        (
            "breadth_version",
            breadth_row
                .as_ref()
                .map_or(V::None, |b| V::Str(b.calculation_version.clone())),
        ),
        (
            "min_breadth_coverage_pct",
            V::Float(cfg.min_breadth_coverage_pct),
        ),
        (
            "veto",
            V::Map(dict([
                ("active", V::Bool(veto_active)),
                ("dist_50", get(&s50, "distance_pct")),
                ("state_50", get(&s50, "state")),
                ("conf_50", get(&s50, "confirmation")),
                ("dist_200", get(&s200, "distance_pct")),
                ("state_200", get(&s200, "state")),
                ("conf_200", get(&s200, "confirmation")),
                (
                    "below_200",
                    V::Bool(as_str(&get(&s200, "state")) == Some("below")),
                ),
                (
                    "effect",
                    s(if veto_active {
                        "New purchases blocked; candidate tier floored at R2"
                    } else {
                        "No restriction from the sentinel"
                    }),
                ),
                (
                    "release",
                    #[allow(clippy::cast_precision_loss)]
                    V::Str(format!(
                        "{} consecutive closes above the 50-DMA plus the {}% buffer",
                        cfg.confirm_days,
                        pf(cfg.buffer_bps as f64 / 100.0, ".2f")
                    )),
                ),
            ])),
        ),
        (
            "proposed_orders",
            V::List(proposed.iter().cloned().map(V::Map).collect()),
        ),
        (
            "proposed_sells",
            V::List(
                proposed
                    .iter()
                    .filter(|o| delta_of(o) < 0.0)
                    .cloned()
                    .map(V::Map)
                    .collect(),
            ),
        ),
        (
            "proposed_buys",
            V::List(
                proposed
                    .iter()
                    .filter(|o| delta_of(o) > 0.0)
                    .cloned()
                    .map(V::Map)
                    .collect(),
            ),
        ),
        (
            "guard_orders",
            V::List(
                proposed
                    .iter()
                    .filter(|o| as_str(&get(o, "note")) == Some("guard exit"))
                    .cloned()
                    .map(V::Map)
                    .collect(),
            ),
        ),
        ("no_plan_reason", no_plan_reason),
        (
            "manual_review_items",
            V::List(manual_items.iter().cloned().map(V::Map).collect()),
        ),
        ("manual_review_count", ilen(manual_items.len())),
        ("tax_data_incomplete", V::Bool(tax_incomplete)),
        ("evaluation_id", evaluation_id.clone()),
        ("planned_sell_value", sum_orders(-1)),
        ("planned_buy_value", sum_orders(1)),
        ("rebalance_plan", rebalance_plan),
        (
            "index_status",
            V::List(status_rows.iter().cloned().map(V::Map).collect()),
        ),
        ("data_stale", V::Bool(any_stale)),
        ("store_status", V::Map(store)),
        (
            "forced_actions_permitted",
            V::Bool(!any_stale && !codes.iter().any(|c| c == "BOOTSTRAP_OBSERVE_ONLY")),
        ),
        ("config", V::Map(config_block(&cfg, settings, &cfg_hash))),
    ]);

    let portfolio = portfolio_block(conn).await?;
    v.insert("portfolio", V::Map(portfolio.clone()));
    for (key2, val) in holding_counts(&portfolio, tier_cap, &proposed, k).iter() {
        v.insert(key2, val.clone());
    }
    let reason_pairs_now = list_of_maps(&get(&v, "reason_pairs"));
    v.insert(
        "headline_reason",
        V::Str(headline_reason(&reason_pairs_now, &narrative_lines)),
    );
    // Enriched here because it needs the portfolio and the plan link, both resolved above.
    let plan_id = match get(&v, "rebalance_plan") {
        V::Map(m) => get(&m, "plan_id"),
        _ => V::None,
    };
    let plan_id = if truthy(&plan_id) {
        Some(plan_id.py_str())
    } else {
        None
    };
    let enriched = enrich_orders(
        conn,
        &proposed,
        &portfolio,
        k.max_pos_vs_day_value,
        settings.regime_ltcg_review_days,
        today,
        plan_id.as_deref(),
    )
    .await?;
    let detail = tax_detail(conn, &enriched, settings.regime_ltcg_review_days, today).await?;
    let mut sells = Vec::new();
    let mut buys = Vec::new();
    for o in &enriched {
        let d = delta_sign(o)?;
        if d < 0.0 {
            sells.push(V::Map(o.clone()));
        } else if d > 0.0 {
            buys.push(V::Map(o.clone()));
        }
    }
    v.insert(
        "proposed_orders",
        V::List(enriched.into_iter().map(V::Map).collect()),
    );
    v.insert("tax_detail", V::List(detail.into_iter().map(V::Map).collect()));
    v.insert("proposed_sells", V::List(sells));
    v.insert("proposed_buys", V::List(buys));
    match regime_store::count_recovery_confirmations(conn).await {
        Ok(n) => {
            v.insert("recovery_confirmations", V::Int(n));
            v.insert(
                "recovery_required",
                V::Int(cfg.recovery_confirm_evaluations),
            );
        }
        Err(_) => {
            v.insert("recovery_confirmations", V::None);
            v.insert("recovery_required", V::None);
        }
    }
    v.insert(
        "tier_ladder",
        V::List(
            TIER_LADDER
                .iter()
                .map(|(t, name, key3, buys, when, does)| {
                    V::Map(dict([
                        ("tier", s(t)),
                        ("name", s(name)),
                        ("key", s(key3)),
                        ("buys", s(buys)),
                        ("when", s(when)),
                        ("does", s(does)),
                        ("current", V::Bool(as_str(&tier) == Some(*t))),
                        ("cap", opt_f(cfg.cap_for(t))),
                    ]))
                })
                .collect(),
        ),
    );
    v.insert(
        "mode_explainer",
        V::List(mode_explainer(settings.regime_enabled, &mode, dry_run)),
    );
    v.insert(
        "data_health",
        s(if any_stale {
            "Stale"
        } else if !breadth_usable {
            "Incomplete"
        } else {
            "Current"
        }),
    );
    let history = transition_history(conn, &cfg, 12).await?;
    let timeline = timeline_chart(&history);
    v.insert(
        "history",
        V::List(history.into_iter().map(V::Map).collect()),
    );
    v.insert("timeline", timeline.map_or(V::None, V::Map));
    v.insert("timeline_min_weeks", V::Int(TIMELINE_MIN_WEEKS));
    let b = banners(&v);
    v.insert("banners", V::List(b));
    let ss = safe_state(&v);
    v.insert("safe_state", ss);
    Ok(v)
}

fn or_list(v: &V) -> V {
    if truthy(v) {
        v.clone()
    } else {
        V::List(Vec::new())
    }
}

fn config_block(cfg: &RegimeConfig, settings: &Settings, cfg_hash: &str) -> PyDict {
    let th = |pairs: Vec<(&str, String)>| {
        V::Map(pairs.into_iter().map(|(a, b)| (a, V::Str(b))).collect())
    };
    dict([
        (
            "ma_lengths",
            V::List(cfg.ma_lengths.iter().map(|n| V::Int(*n)).collect()),
        ),
        ("buffer_bps", V::Int(cfg.buffer_bps)),
        ("confirm_days", V::Int(cfg.confirm_days)),
        (
            "breadth_thresholds",
            th(vec![
                ("R4 crash", format!("< {}%", pf(cfg.r4_breadth_max, ".0f"))),
                (
                    "R3 risk-off",
                    format!("< {}%", pf(cfg.r3_breadth_max, ".0f")),
                ),
                (
                    "R1 risk-on",
                    format!("≥ {}%", pf(cfg.r1_breadth_min, ".0f")),
                ),
                (
                    "recovery override",
                    format!("≥ {}%", pf(cfg.recovery_breadth_min, ".0f")),
                ),
            ]),
        ),
        (
            "health_thresholds",
            th(vec![
                ("R3 H200", format!("≤ {}", pf(cfg.r3_h200_max, ".2f"))),
                ("R1 H50", format!("≥ {}", pf(cfg.r1_h50_min, ".2f"))),
                ("R1 H200", format!("≥ {}", pf(cfg.r1_h200_min, ".2f"))),
                ("R4 H200", format!("< {}", pf(cfg.r4_h200_max, ".2f"))),
                (
                    "R4 stack",
                    format!("≥ {}", pf(cfg.r4_bearish_stack_min, ".2f")),
                ),
            ]),
        ),
        (
            "exposure_map",
            V::Map(
                super::regime::TIERS
                    .iter()
                    .zip(cfg.tier_exposure_pct)
                    .map(|(t, c)| (*t, V::Float(c)))
                    .collect(),
            ),
        ),
        (
            "default_weights",
            V::Map(
                cfg.default_book_weights
                    .iter()
                    .map(|(a, b)| (a.clone(), V::Float(*b)))
                    .collect(),
            ),
        ),
        ("recovery_breadth_min", V::Float(cfg.recovery_breadth_min)),
        ("recovery_h20_min", V::Float(cfg.recovery_h20_min)),
        (
            "recovery_confirm_evaluations",
            V::Int(cfg.recovery_confirm_evaluations),
        ),
        ("ltcg_review_days", V::Int(settings.regime_ltcg_review_days)),
        (
            "weekly_evaluation_weekday",
            V::Int(cfg.weekly_evaluation_weekday),
        ),
        (
            "r4_residual",
            V::Str(format!(
                "max {} names, ordered by {}",
                settings.regime_r4_max_residual_names,
                settings.regime_r4_residual_ordering.join(", ")
            )),
        ),
        ("breadth_required", V::Bool(cfg.breadth_required)),
        (
            "min_breadth_coverage_pct",
            V::Float(cfg.min_breadth_coverage_pct),
        ),
        ("index_stale_days", V::Int(cfg.index_stale_days)),
        (
            "whipsaw",
            s("≥20pt reduction, ≥80% restored within 4 evaluations, benchmark drawdown < 5%"),
        ),
        ("algorithm_version", V::Str(cfg.algorithm_version.clone())),
        ("config_hash", V::Str(cfg_hash.to_owned())),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn m(pairs: &[(&str, V)]) -> PyDict {
        pairs.iter().map(|(a, b)| (*a, b.clone())).collect()
    }

    #[test]
    fn sparkline_needs_a_real_average_at_the_first_point() {
        let closes: Vec<f64> = (0..51).map(f64::from).collect();
        assert!(sparkline(&closes, 50).is_none());
        let closes: Vec<f64> = (0..52).map(|i| 100.0 + f64::from(i)).collect();
        let sp = sparkline(&closes, 50);
        assert!(sp.is_some());
        let sp = sp.unwrap_or_default();
        assert_eq!(get(&sp, "sessions"), V::Int(3));
        assert_eq!(get(&sp, "above"), V::Bool(true));
        assert_eq!(get(&sp, "last_x"), V::Float(167.0));
    }

    #[test]
    fn sparkline_paths_match_python() {
        // _sparkline([10, 11, 12, 11, 13, 14], 3) printed by the Python desk.
        let sp = sparkline(&[10.0, 11.0, 12.0, 11.0, 13.0, 14.0], 3).unwrap_or_default();
        assert_eq!(get(&sp, "price"), s(PY_SPARK_PRICE));
        assert_eq!(get(&sp, "ma"), s(PY_SPARK_MA));
        assert_eq!(get(&sp, "last_y"), V::Float(PY_SPARK_LAST_Y));
    }

    #[test]
    fn timeline_refuses_fewer_than_three_weeks() {
        let h = |w: &str, cap: f64| {
            m(&[
                ("week", s(w)),
                ("cap", V::Float(cap)),
                ("actual", V::None),
                ("breadth", V::Float(50.0)),
            ])
        };
        assert!(timeline_chart(&[h("2026-10-16", 40.0), h("2026-10-23", 40.0)]).is_none());
        let t = timeline_chart(&[
            h("2026-10-09", 70.0),
            h("2026-10-16", 40.0),
            h("2026-10-23", 40.0),
        ]);
        let t = t.unwrap_or_default();
        assert_eq!(get(&t, "cap"), s(PY_TIMELINE_CAP));
        assert_eq!(get(&t, "actual"), s(""));
        assert_eq!(get(&t, "first"), s("2026-10-09"));
    }

    #[test]
    fn the_headline_is_the_risk_off_code_that_fired_not_the_first_observation() {
        let pairs = vec![
            m(&[
                ("code", s("R2_DEFAULT")),
                ("text", s("default")),
                ("risk_off", V::Bool(false)),
            ]),
            m(&[
                ("code", s("R3_H200_WEAK")),
                ("text", s("weak")),
                ("risk_off", V::Bool(true)),
            ]),
        ];
        assert_eq!(
            headline_reason(&pairs, &["NIFTY 50 is below".to_owned()]),
            "weak"
        );
        let pairs = vec![
            m(&[
                ("code", s("NEW_BUYS_HALF")),
                ("text", s("half")),
                ("risk_off", V::Bool(false)),
            ]),
            m(&[
                ("code", s("R2_DEFAULT")),
                ("text", s("default")),
                ("risk_off", V::Bool(false)),
            ]),
        ];
        assert_eq!(headline_reason(&pairs, &[]), "default");
        assert_eq!(headline_reason(&[], &[]), "—");
    }

    #[test]
    fn confirmation_counts_are_capped_at_the_requirement() {
        assert_eq!(confirmation(&PyDict::new(), 3), "0/3 unconfirmed");
        assert_eq!(
            confirmation(
                &m(&[("state", s("below")), ("confirming_closes", V::Int(44))]),
                3
            ),
            "3/3 below"
        );
        assert_eq!(
            confirmation(
                &m(&[("state", s("above")), ("confirming_closes", V::None)]),
                3
            ),
            "0/3 above"
        );
    }

    #[test]
    fn gates_show_every_rule_with_its_threshold() {
        let cfg = RegimeConfig::default();
        let health = m(&[
            ("20", V::Float(0.0)),
            ("50", V::Float(0.0)),
            ("200", V::Float(0.5)),
        ]);
        let g = gates(
            &health,
            &V::Float(83.9),
            true,
            None,
            &V::Float(0.2),
            &cfg,
            &["R3_H200_WEAK".to_owned()],
        );
        let rows: Vec<PyDict> = g.iter().map(as_map).collect();
        assert_eq!(rows.len(), 7);
        assert_eq!(get(&rows[0], "current"), s("0.50"));
        assert_eq!(get(&rows[0], "status"), s("Triggered"));
        assert_eq!(get(&rows[0], "fired"), V::Bool(true));
        assert_eq!(get(&rows[1], "current"), s("83.9%"));
        assert_eq!(get(&rows[1], "status"), s("Clear"));
        assert_eq!(get(&rows[2], "status"), s("Not evaluable"));
        assert_eq!(get(&rows[2], "threshold"), s("below −1.50%"));
        assert_eq!(get(&rows[5], "current"), s("H50 0.00 · H200 0.50"));
        assert_eq!(get(&rows[6], "status"), s("Met"));
    }

    #[test]
    fn unusable_breadth_never_reads_as_a_measurement() {
        let cfg = RegimeConfig::default();
        let g = gates(
            &PyDict::new(),
            &V::Float(20.0),
            false,
            None,
            &V::None,
            &cfg,
            &[],
        );
        let rows: Vec<PyDict> = g.iter().map(as_map).collect();
        assert_eq!(get(&rows[1], "current"), V::None);
        assert_eq!(get(&rows[1], "status"), s("Not evaluable"));
        assert_eq!(get(&rows[0], "status"), s("Not evaluable"));
        let n = narrative(
            &[],
            &PyDict::new(),
            &V::Float(20.0),
            false,
            &s("blocked"),
            &cfg,
            &[],
        );
        assert_eq!(
            n,
            vec![
                "There is no usable breadth reading, so breadth can neither force selling nor permit Risk-On.".to_owned(),
                "New purchases are therefore blocked.".to_owned(),
            ]
        );
    }

    #[test]
    fn dry_run_and_observe_banners_say_what_cannot_happen() {
        let v = m(&[
            ("data_stale", V::Bool(false)),
            ("breadth_usable", V::Bool(true)),
            ("tax_data_incomplete", V::Bool(false)),
            ("manual_action_required", V::Bool(false)),
            ("dry_run", V::Bool(true)),
            ("mode", s("observe")),
            ("policy_tier", s("R3")),
            ("forced_actions_permitted", V::Bool(false)),
        ]);
        let titles: Vec<V> = banners(&v)
            .iter()
            .map(|b| get(&as_map(b), "title"))
            .collect();
        assert_eq!(titles, vec![s("DRY_RUN is on"), s("Observe mode")]);
        assert_eq!(
            safe_state(&v),
            s(
                "No prior policy state exists, so forced selling is suppressed. Proposed actions are advisory until a tier is committed."
            )
        );
    }

    #[test]
    fn half_size_is_judged_against_the_sleeve_not_nav() {
        let k = DeskConstants {
            target_positions: (12, 15),
            cluster_cap: V::Float(25.0),
            min_position_weight: 6.0,
            half_size_weight: 3.0,
            max_pos_vs_day_value: 0.01,
            weekday_override_is_text: false,
        };
        let p = |sym: &str, w: f64, value: f64| {
            V::Map(m(&[
                ("symbol", s(sym)),
                ("weight_pct", V::Float(w)),
                ("value", V::Float(value)),
                ("excluded", V::Bool(false)),
            ]))
        };
        let portfolio = m(&[(
            "tradeable",
            V::List(vec![
                p("A", 2.4, 100.0),
                p("B", 1.6, 300.0),
                p("C", 0.8, 50.0),
            ]),
        )]);
        // R3: the sleeve is 40% of NAV, so 2.4% of NAV is 6% of the sleeve — full size.
        let c = holding_counts(&portfolio, Some(40.0), &[], &k);
        assert_eq!(get(&c, "full_sized"), V::Int(1));
        assert_eq!(as_list(&get(&c, "half_sized")).len(), 1);
        assert_eq!(as_list(&get(&c, "below_half")).len(), 1);
        let top = as_map(&get(&c, "largest_position"));
        assert_eq!(get(&top, "symbol"), s("B"));
        assert_eq!(get(&top, "weight_sleeve"), V::Float(4.0));
        assert_eq!(get(&top, "value"), V::Int(300));
    }

    pub(super) const PY_SPARK_PRICE: &str = "M1.0,22.0 L56.3,32.0 L111.7,12.0 L167.0,2.0";
    pub(super) const PY_SPARK_MA: &str = "M1.0,32.0 L56.3,28.7 L111.7,22.0 L167.0,15.3";
    pub(super) const PY_SPARK_LAST_Y: f64 = 2.0;
    pub(super) const PY_TIMELINE_CAP: &str =
        "M40.0,51.6 L347.0,51.6 L347.0,91.2 L654.0,91.2 L654.0,91.2";
}
