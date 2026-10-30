//! Rebalance plan construction — `baskfy_core.basket.build_plan` as `app/rebalance.py` binds it
//! (the desk's config, `data/sectors.csv`, and the **real** untouchable guard). Every rule is the
//! arithmetic the live account was rebalanced on, in Python's evaluation order; graded on
//! `fixtures/weekly/plans.json` byte for byte (orjson text, types kept).

use std::collections::HashMap;

use chrono::{Datelike, NaiveDate};

use super::costs::{CostLine, cost_pct, order_cost, plan_cost};
use super::pandas::{Num, nargsort, np_clip, py_int, py_round, py_round_int, py_sum, quantile};
use super::score::{Scored, ScoringConfig, stop_from_vol};
use crate::pyfmt::{PyArg, format_value, str_format};
use crate::wire::{PyDict, PyValue};

/// The constants plan construction reads (`BasketConfig`), with the desk's values as defaults.
#[derive(Debug, Clone, PartialEq)]
pub struct BasketConfig {
    /// `CASH_BANDS`: `(min_breadth, cash_pct)`, highest first.
    pub cash_bands: Vec<(f64, f64)>,
    /// `CLUSTER_CAP`.
    pub cluster_cap: f64,
    /// `FULLY_INVESTED`.
    pub fully_invested: bool,
    /// `HALF_SIZE_WEIGHT`.
    pub half_size_weight: f64,
    /// `MAX_POS_VS_DAY_VALUE`.
    pub max_pos_vs_day_value: f64,
    /// `MAX_SINGLE_WEIGHT`.
    pub max_single_weight: f64,
    /// `MAX_TRADE_COST_PCT`.
    pub max_trade_cost_pct: f64,
    /// `MIN_POSITION_WEIGHT`.
    pub min_position_weight: f64,
    /// `MIN_TRADE_PCT`.
    pub min_trade_pct: f64,
    /// `MIN_TRADE_VALUE`.
    pub min_trade_value: f64,
    /// `PARABOLIC_RSI`.
    pub parabolic_rsi: f64,
    /// `REPLACEMENT_EDGE`.
    pub replacement_edge: f64,
    /// `RETENTION_BUFFER`.
    pub retention_buffer: i64,
    /// `RUNNER_MAX_VALUE` (an `int` in `app.config`; a stored setting makes it a float).
    pub runner_max_value: Num,
    /// `TARGET_POSITIONS`.
    pub target_positions: (i64, i64),
    /// `TRIM_TO_WEIGHT`.
    pub trim_to_weight: f64,
    /// The scoring constants (the stops read `STOP_*`).
    pub scoring: ScoringConfig,
}

impl Default for BasketConfig {
    fn default() -> Self {
        Self {
            cash_bands: vec![(65.0, 5.0), (45.0, 15.0), (0.0, 35.0)],
            cluster_cap: 25.0,
            fully_invested: false,
            half_size_weight: 3.0,
            max_pos_vs_day_value: 0.01,
            max_single_weight: 15.0,
            max_trade_cost_pct: 0.20,
            min_position_weight: 6.0,
            min_trade_pct: 2.0,
            min_trade_value: 10_000.0,
            parabolic_rsi: 82.0,
            replacement_edge: 8.0,
            retention_buffer: 5,
            runner_max_value: Num::Int(300_000),
            target_positions: (12, 15),
            trim_to_weight: 2.0,
            scoring: ScoringConfig::default(),
        }
    }
}

/// `cash_pct_for(breadth20, cfg)`: percent of the sleeve held back.
#[must_use]
pub fn cash_pct_for(breadth20: f64, cfg: &BasketConfig) -> f64 {
    if cfg.fully_invested {
        return 0.0;
    }
    for (min_b, cash) in &cfg.cash_bands {
        if breadth20 >= *min_b {
            return *cash;
        }
    }
    cfg.cash_bands.last().map_or(0.0, |b| b.1)
}

/// `rank_buffer.inside_hold_band(rank, top_n, hold_buffer)`.
#[must_use]
pub const fn inside_hold_band(rank: i64, top_n: i64, hold_buffer: i64) -> bool {
    rank <= top_n + hold_buffer
}

/// `windows.subtract_months(day, months)` (clamped to the target month's last day).
#[must_use]
pub fn subtract_months(day: NaiveDate, months: i32) -> Option<NaiveDate> {
    let total = day.year() * 12 + i32::try_from(day.month()).ok()? - 1 - months;
    let year = total.div_euclid(12);
    let month = u32::try_from(total.rem_euclid(12) + 1).ok()?;
    let next = if month == 12 {
        NaiveDate::from_ymd_opt(year + 1, 1, 1)?
    } else {
        NaiveDate::from_ymd_opt(year, month + 1, 1)?
    };
    let last = next.pred_opt()?.day();
    NaiveDate::from_ymd_opt(year, month, day.day().min(last))
}

/// `app.rebalance._tradeable`: **the real guard**, exactly as the gateway applies it
/// (`baskfy_execution.guards.assert_tradeable`, lane `exec_core`) — the only implementation the
/// desk ever passes to plan construction (non-negotiables 6 and 7).
#[must_use]
pub fn tradeable(symbol: &str) -> bool {
    crate::lanes::exec_core::guards::assert_tradeable(symbol, None).is_ok()
}

/// Why a plan could not be built (Python raises; `/analyze` answers 500).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct PlanError(pub String);

/// A number from a holding dict, as Python arithmetic sees it.
fn num(v: Option<&PyValue>) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    match v {
        Some(PyValue::Int(i)) => *i as f64,
        Some(PyValue::Float(f)) => *f,
        Some(PyValue::Bool(b)) => f64::from(u8::from(*b)),
        _ => f64::NAN,
    }
}

/// A number from a dict, keeping Python's int/float distinction (for `sum()`).
fn pnum(v: Option<&PyValue>) -> Num {
    match v {
        Some(PyValue::Int(i)) => Num::Int(*i),
        Some(PyValue::Bool(b)) => Num::Int(i64::from(*b)),
        other => Num::Float(num(other)),
    }
}

fn truthy(v: Option<&PyValue>) -> bool {
    match v {
        None | Some(PyValue::None) => false,
        Some(PyValue::Int(i)) => *i != 0,
        Some(PyValue::Float(f)) => *f != 0.0,
        Some(PyValue::Bool(b)) => *b,
        Some(PyValue::Str(s)) => !s.is_empty(),
        Some(PyValue::List(l)) => !l.is_empty(),
        Some(PyValue::Map(m)) => !m.is_empty(),
        Some(_) => true,
    }
}

fn qty(h: &PyDict) -> i64 {
    match h.get("quantity") {
        Some(PyValue::Int(i)) => *i,
        other => py_int(num(other)).unwrap_or(0),
    }
}

fn sym(h: &PyDict) -> String {
    match h.get("symbol") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

fn pledged(h: Option<&PyDict>) -> PyValue {
    h.and_then(|h| h.get("pledged_qty").cloned())
        .unwrap_or(PyValue::Int(0))
}

fn fmt(value: f64, spec: &str) -> String {
    format_value(&PyArg::Float(value), spec).unwrap_or_default()
}

/// `pd.Timestamp(raw).date()` for the ISO dates a scan carries.
fn parse_date(raw: &str) -> Result<NaiveDate, PlanError> {
    let head: String = raw.trim().chars().take(10).collect();
    NaiveDate::parse_from_str(&head, "%Y-%m-%d").map_err(|_| {
        PlanError(format!(
            "ValueError: could not convert string to Timestamp: {raw:?}"
        ))
    })
}

/// What `build_plan` needs besides the scored scan.
#[derive(Debug, Clone, Default)]
pub struct PlanInputs {
    /// `holdings` (the dicts `Kite.holdings()` returns), in order.
    pub holdings: Vec<PyDict>,
    /// `cash`.
    pub cash: f64,
    /// `cash`'s Python type was `int` (it changes nothing but `round(cash)`'s input).
    pub live_prices: Vec<(String, f64)>,
    /// `breadth_override`.
    pub breadth_override: Option<f64>,
    /// `clusters` (`data/sectors.csv`).
    pub clusters: HashMap<String, String>,
    /// The new plan's id (`uuid.uuid4().hex[:12]`).
    pub plan_id: String,
}

struct Ctx {
    live: HashMap<String, f64>,
    hold: Vec<(String, PyDict)>,
}

impl Ctx {
    fn held(&self, s: &str) -> Option<&PyDict> {
        self.hold.iter().find(|(k, _)| k == s).map(|(_, h)| h)
    }

    fn price_of(&self, s: &str) -> Option<f64> {
        if let Some(p) = self.live.get(s)
            && *p != 0.0
        {
            return Some(*p);
        }
        let h = self.held(s)?;
        truthy(h.get("last_price")).then(|| num(h.get("last_price")))
    }
}

fn order_row(pairs: Vec<(&str, PyValue)>) -> PyDict {
    let mut d = PyDict::new();
    for (k, v) in pairs {
        d.insert(k, v);
    }
    d
}

fn int_or_none(x: Option<i64>) -> PyValue {
    x.map_or(PyValue::None, PyValue::Int)
}

/// `build_plan(scored, holdings, cash, cfg=C, tradeable=_tradeable, clusters=..., live_prices=...,
/// breadth_override=...)`.
///
/// # Errors
/// What Python would raise (a zero price or capital, an unparseable scan date).
#[allow(clippy::too_many_lines, clippy::cast_precision_loss)]
pub fn build_plan(
    scored: &Scored,
    inputs: &PlanInputs,
    cfg: &BasketConfig,
) -> Result<PyDict, PlanError> {
    let zero_div = || PlanError("ZeroDivisionError: float division by zero".to_owned());
    let live: HashMap<String, f64> = inputs
        .live_prices
        .iter()
        .filter(|(_, v)| *v != 0.0 && *v > 0.0)
        .cloned()
        .collect();
    let mut hold: Vec<(String, PyDict)> = Vec::new();
    let mut excluded: Vec<PyValue> = Vec::new();
    for h in &inputs.holdings {
        let s = sym(h);
        if tradeable(&s) {
            if let Some(slot) = hold.iter_mut().find(|(k, _)| *k == s) {
                slot.1 = h.clone();
            } else {
                hold.push((s, h.clone()));
            }
        } else {
            excluded.push(PyValue::Str(s));
        }
    }
    let ctx = Ctx { live, hold };
    let n_rows = scored.len();

    let mut unpriced: Vec<String> = Vec::new();
    for i in 0..n_rows {
        if !scored.reject[i].is_empty() {
            continue;
        }
        let s = scored.symbol(i);
        if ctx.held(&s).is_none() && ctx.price_of(&s).is_none() && !unpriced.contains(&s) {
            unpriced.push(s);
        }
    }
    unpriced.sort();

    let book_val: f64 = py_sum(
        ctx.hold
            .iter()
            .map(|(_, h)| Num::Int(qty(h)).mul(pnum(h.get("last_price")))),
    )
    .as_f64();
    let capital = book_val + inputs.cash;
    let breadth = match inputs.breadth_override {
        Some(b) => b,
        None => {
            let close: Vec<f64> = (0..n_rows).map(|i| scored.num("close", i)).collect();
            let ma20: Vec<f64> = (0..n_rows).map(|i| scored.num("ma_20", i)).collect();
            if n_rows == 0 {
                f64::NAN
            } else {
                let hits = (0..n_rows).filter(|&i| close[i] > ma20[i]).count();
                hits as f64 / n_rows as f64 * 100.0
            }
        }
    };
    let cash_pct = cash_pct_for(breadth, cfg);
    let invest = capital * (1.0 - cash_pct / 100.0);

    // --- classify holdings ---
    let mut runners: Vec<(String, String, Num)> = Vec::new();
    let mut exits: Vec<(String, String)> = Vec::new();
    let mut keepers: Vec<(String, f64)> = Vec::new();
    for (s, h) in &ctx.hold {
        let Some(i) = scored.row_of(s) else {
            exits.push((s.clone(), "not in scan universe".to_owned()));
            continue;
        };
        let value = Num::Int(qty(h)).mul(pnum(h.get("last_price")));
        if !scored.reject[i].is_empty() {
            if value.as_f64() > cfg.runner_max_value.as_f64() {
                runners.push((
                    s.clone(),
                    "filter-rejected \u{2192} capped runner".to_owned(),
                    cfg.runner_max_value,
                ));
            } else {
                runners.push((
                    s.clone(),
                    "filter-rejected \u{2192} hold as runner".to_owned(),
                    value,
                ));
            }
        } else if scored.num("rsi_one_month", i) >= cfg.parabolic_rsi {
            runners.push((
                s.clone(),
                format!(
                    "parabolic RSI {} \u{2192} trim to runner",
                    fmt(scored.num("rsi_one_month", i), ".0f")
                ),
                Num::Float(capital * cfg.trim_to_weight / 100.0),
            ));
        } else {
            keepers.push((s.clone(), scored.score[i]));
        }
    }
    let is_runner = |s: &str| runners.iter().any(|(k, _, _)| k == s);

    // --- selection ---
    let elig_rows: Vec<usize> = (0..n_rows)
        .filter(|&i| scored.reject[i].is_empty() && !unpriced.contains(&scored.symbol(i)))
        .collect();
    let elig_ranks: Vec<f64> = elig_rows.iter().map(|&i| scored.rank[i]).collect();
    let elig: Vec<usize> = nargsort(&elig_ranks, true)
        .into_iter()
        .map(|k| elig_rows[k])
        .collect();
    let elig_scores: Vec<f64> = elig.iter().map(|&i| scored.score[i]).collect();
    let q85 = quantile(&elig_scores, 0.85);
    let above = elig_scores.iter().filter(|&&x| q85 <= x).count();
    let (n_lo, n_hi) = cfg.target_positions;
    #[allow(clippy::cast_possible_truncation)]
    let n = np_clip(
        (i64::try_from(above).unwrap_or(0) + i64::try_from(keepers.len()).unwrap_or(0)) as f64,
        n_lo as f64,
        n_hi as f64,
    ) as i64;
    let mut selected: Vec<(String, f64)> = Vec::new();
    for (s, sc) in &keepers {
        let r = scored
            .row_of(s)
            .and_then(|i| py_int(scored.rank[i]))
            .ok_or_else(|| PlanError("ValueError: cannot convert float NaN to integer".into()))?;
        if inside_hold_band(r, n, cfg.retention_buffer) {
            selected.push((s.clone(), *sc));
        } else {
            let challenger = elig.get(selected.len()).map(|&i| scored.score[i]);
            match challenger {
                Some(ch) if ch - sc >= cfg.replacement_edge => exits.push((
                    s.clone(),
                    format!(
                        "rank {r} beyond N+{}, replaced (+{} pts)",
                        cfg.retention_buffer,
                        fmt(ch - sc, ".0f")
                    ),
                )),
                _ => selected.push((s.clone(), *sc)),
            }
        }
    }
    for &i in &elig {
        if i64::try_from(selected.len()).unwrap_or(i64::MAX) >= n {
            break;
        }
        let s = scored.symbol(i);
        if !selected.iter().any(|(k, _)| *k == s) && !is_runner(&s) {
            selected.push((s, scored.score[i]));
        }
    }

    // --- weights ---
    let runner_val = py_sum(runners.iter().map(|r| r.2)).as_f64();
    let pool = invest - runner_val;
    let tot = py_sum(selected.iter().map(|x| Num::Float(x.1))).as_f64();
    let mut w: Vec<(String, f64)> = Vec::new();
    if tot > 0.0 {
        if capital == 0.0 || invest == 0.0 {
            return Err(zero_div());
        }
        for (s, sc) in &selected {
            let x = sc / tot * 100.0 * (invest / capital) * (pool / invest) / (invest / capital);
            let m = if cfg.max_single_weight < x {
                cfg.max_single_weight
            } else {
                x
            };
            w.push((s.clone(), m));
        }
    }
    let rescale = |w: &[(String, f64)]| -> Result<f64, PlanError> {
        if w.is_empty() {
            return Ok(0.0);
        }
        let sum = py_sum(w.iter().map(|x| Num::Float(x.1))).as_f64();
        if sum == 0.0 || capital == 0.0 {
            return Err(zero_div());
        }
        Ok((pool / capital * 100.0) / sum)
    };
    let scale = rescale(&w)?;
    for x in &mut w {
        x.1 *= scale;
    }
    for x in &mut w {
        if x.1 > cfg.min_position_weight / 2.0 && cfg.min_position_weight > x.1 {
            x.1 = cfg.min_position_weight;
        }
    }
    let scale = rescale(&w)?;
    for x in &mut w {
        x.1 = py_round(x.1 * scale, 2);
    }
    // half-size short-history listings
    let has_listed = scored.frame.has("listed_on");
    let half_cutoff = if n_rows > 0 && scored.frame.has("date") {
        let raw = scored
            .frame
            .col("date")
            .map(|c| c.py_str(0))
            .unwrap_or_default();
        Some(
            subtract_months(parse_date(&raw)?, 18)
                .ok_or_else(|| PlanError("ValueError: date out of range".into()))?,
        )
    } else {
        None
    };
    if let (Some(cutoff), true) = (half_cutoff, has_listed) {
        let col = scored.frame.col("listed_on");
        for x in &mut w {
            let Some(i) = scored.row_of(&x.0) else {
                continue;
            };
            let Some(c) = col else { continue };
            if c.is_na(i) {
                continue;
            }
            let listed = parse_date(&c.py_str(i))?;
            if listed > cutoff && cfg.half_size_weight < x.1 {
                x.1 = cfg.half_size_weight;
            }
        }
    }

    let mut cluster_warn: Vec<PyValue> = Vec::new();
    if !inputs.clusters.is_empty() {
        let mut agg: Vec<(String, f64)> = Vec::new();
        for (s, v) in &w {
            let k = inputs
                .clusters
                .get(s)
                .cloned()
                .unwrap_or_else(|| "other".to_owned());
            if let Some(slot) = agg.iter_mut().find(|(a, _)| *a == k) {
                slot.1 += v;
            } else {
                agg.push((k, 0.0 + v));
            }
        }
        for (k, v) in agg {
            if v > cfg.cluster_cap && k != "other" {
                cluster_warn.push(PyValue::Str(format!(
                    "{k} {}% > cap {}%",
                    fmt(v, ".1f"),
                    baskfy_core::py::float_repr(cfg.cluster_cap)
                )));
            }
        }
    }

    // --- orders ---
    let mut orders: Vec<PyDict> = Vec::new();
    let mut by_weight = w.clone();
    by_weight.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    for (s, weight) in &by_weight {
        let Some(px) = ctx.price_of(s) else { continue };
        let row = scored.row_of(s);
        let vol = row.map_or(0.36, |i| scored.num("volatility_one_year", i));
        let mut tgt_qty = py_round_int(capital * weight / 100.0 / px).ok_or_else(zero_div)?;
        let held = ctx.held(s);
        let cur_qty = held.map_or(0, qty);
        let mut dq = tgt_qty - cur_qty;
        let day_val = row.map_or(f64::INFINITY, |i| scored.num("median_volume_one_year", i));
        if tgt_qty as f64 * px > day_val * cfg.max_pos_vs_day_value {
            tgt_qty = py_int(day_val * cfg.max_pos_vs_day_value / px).ok_or_else(zero_div)?;
            dq = tgt_qty - cur_qty;
        }
        let action = if cur_qty == 0 {
            "BUY"
        } else if dq > 0 {
            "ADD"
        } else if dq == 0 {
            "HOLD"
        } else {
            "TRIM"
        };
        orders.push(order_row(vec![
            ("symbol", PyValue::Str(s.clone())),
            ("action", PyValue::Str(action.to_owned())),
            ("qty_now", PyValue::Int(cur_qty)),
            ("delta", PyValue::Int(dq)),
            ("qty_final", PyValue::Int(tgt_qty)),
            ("ref_price", PyValue::Float(px)),
            ("weight", PyValue::Float(*weight)),
            ("value", int_or_none(py_round_int(tgt_qty as f64 * px))),
            ("stop", PyValue::Float(stop_from_vol(px, vol, &cfg.scoring))),
            ("pledged", pledged(held)),
            (
                "rank",
                row.map_or(PyValue::None, |i| int_or_none(py_int(scored.rank[i]))),
            ),
            (
                "score",
                row.map_or(PyValue::None, |i| PyValue::Float(scored.score[i])),
            ),
        ]));
    }
    for (s, reason, val) in &runners {
        let Some(h) = ctx.held(s) else { continue };
        let px_v = h.get("last_price").cloned().unwrap_or(PyValue::None);
        let px = num(Some(&px_v));
        if px == 0.0 {
            return Err(zero_div());
        }
        let q = qty(h);
        let fit = py_int(val.as_f64() / px).ok_or_else(zero_div)?;
        let tgt_qty = if fit < q { fit } else { q };
        let row = scored.row_of(s);
        let vol = row.map_or(0.4, |i| scored.num("volatility_one_year", i));
        if capital == 0.0 {
            return Err(zero_div());
        }
        orders.push(order_row(vec![
            ("symbol", PyValue::Str(s.clone())),
            (
                "action",
                PyValue::Str(if tgt_qty < q { "TRIM" } else { "HOLD" }.to_owned()),
            ),
            ("qty_now", PyValue::Int(q)),
            ("delta", PyValue::Int(tgt_qty - q)),
            ("qty_final", PyValue::Int(tgt_qty)),
            ("ref_price", px_v),
            (
                "weight",
                PyValue::Float(py_round(tgt_qty as f64 * px / capital * 100.0, 2)),
            ),
            ("value", int_or_none(py_round_int(tgt_qty as f64 * px))),
            ("stop", PyValue::Float(stop_from_vol(px, vol, &cfg.scoring))),
            ("pledged", pledged(Some(h))),
            ("rank", PyValue::None),
            ("score", PyValue::None),
            ("note", PyValue::Str(reason.clone())),
        ]));
    }
    for (s, reason) in &exits {
        let Some(h) = ctx.held(s) else { continue };
        let q = qty(h);
        orders.push(order_row(vec![
            ("symbol", PyValue::Str(s.clone())),
            ("action", PyValue::Str("EXIT".to_owned())),
            ("qty_now", PyValue::Int(q)),
            ("delta", PyValue::Int(-q)),
            ("qty_final", PyValue::Int(0)),
            (
                "ref_price",
                h.get("last_price").cloned().unwrap_or(PyValue::None),
            ),
            ("weight", PyValue::Int(0)),
            ("value", PyValue::Int(0)),
            ("stop", PyValue::None),
            ("pledged", pledged(Some(h))),
            ("rank", PyValue::None),
            ("score", PyValue::None),
            ("note", PyValue::Str(reason.clone())),
        ]));
    }

    // --- no-trade band ---
    for o in &mut orders {
        let delta = match o.get("delta") {
            Some(PyValue::Int(d)) => *d,
            _ => 0,
        };
        let qty_final = match o.get("qty_final") {
            Some(PyValue::Int(d)) => *d,
            _ => 0,
        };
        if delta == 0 || qty_final == 0 {
            continue;
        }
        let qty_now = match o.get("qty_now") {
            Some(PyValue::Int(d)) => *d,
            _ => 0,
        };
        let ref_price = num(o.get("ref_price"));
        let val = delta.unsigned_abs() as f64 * ref_price;
        let pct = if qty_now == 0 {
            100.0
        } else {
            delta.unsigned_abs() as f64 / qty_now as f64 * 100.0
        };
        let side = if delta > 0 { "BUY" } else { "SELL" };
        let cpct = cost_pct(val, side);
        let est_cost = py_round(order_cost(val, side).total(), 2);
        o.insert("est_cost", PyValue::Float(est_cost));
        o.insert("est_cost_pct", PyValue::Float(py_round(cpct, 3)));
        if val < cfg.min_trade_value || pct < cfg.min_trade_pct || cpct > cfg.max_trade_cost_pct {
            let skipped = str_format(
                "would move {:.2f}% of the position for Rs {:,.0f} at a cost of Rs {:,.0f} \
                 ({:.2f}% of the trade); below the no-trade band ({:.0f}% / Rs {:,.0f} / max \
                 {:.2f}% cost)",
                &[
                    PyArg::Float(pct),
                    PyArg::Float(val),
                    PyArg::Float(est_cost),
                    PyArg::Float(cpct),
                    PyArg::Float(cfg.min_trade_pct),
                    PyArg::Float(cfg.min_trade_value),
                    PyArg::Float(cfg.max_trade_cost_pct),
                ],
            )
            .map_err(|e| PlanError(e.to_string()))?;
            let note = match o.get("note") {
                Some(PyValue::Str(n)) if !n.is_empty() => format!("{n} \u{b7} {skipped}"),
                _ => skipped,
            };
            o.insert("note", PyValue::Str(note));
            o.insert("skipped_delta", PyValue::Int(delta));
            o.insert("action", PyValue::Str("HOLD".to_owned()));
            o.insert("delta", PyValue::Int(0));
            o.insert("qty_final", PyValue::Int(qty_now));
            o.insert(
                "value",
                int_or_none(py_round_int(qty_now as f64 * ref_price)),
            );
            o.insert("est_cost", PyValue::Float(0.0));
            o.insert("est_cost_pct", PyValue::Float(0.0));
        }
    }

    let delta_of = |o: &PyDict| match o.get("delta") {
        Some(PyValue::Int(d)) => *d,
        _ => 0,
    };
    let buys = py_sum(
        orders
            .iter()
            .filter(|o| delta_of(o) > 0)
            .map(|o| Num::Int(delta_of(o)).mul(pnum(o.get("ref_price")))),
    )
    .as_f64();
    let sells = py_sum(
        orders
            .iter()
            .filter(|o| delta_of(o) < 0)
            .map(|o| Num::Int(-delta_of(o)).mul(pnum(o.get("ref_price")))),
    )
    .as_f64();
    let pledged_sells: Vec<PyValue> = orders
        .iter()
        .filter(|o| delta_of(o) < 0 && num(o.get("pledged")) > 0.0)
        .filter_map(|o| o.get("symbol").cloned())
        .collect();
    let cost_lines: Vec<CostLine> = orders
        .iter()
        .map(|o| CostLine {
            delta: delta_of(o),
            ref_price: if truthy(o.get("ref_price")) {
                num(o.get("ref_price"))
            } else {
                0.0
            },
        })
        .collect();

    let mut plan = PyDict::new();
    plan.insert("plan_id", PyValue::Str(inputs.plan_id.clone()));
    plan.insert("capital", int_or_none(py_round_int(capital)));
    plan.insert("book_value", int_or_none(py_round_int(book_val)));
    plan.insert("cash", int_or_none(py_round_int(inputs.cash)));
    plan.insert("breadth_above_20dma", PyValue::Float(py_round(breadth, 1)));
    plan.insert("cash_target_pct", PyValue::Float(cash_pct));
    plan.insert("buys_value", int_or_none(py_round_int(buys)));
    plan.insert("sells_value", int_or_none(py_round_int(sells)));
    plan.insert("net_cash_use", int_or_none(py_round_int(buys - sells)));
    plan.insert("cluster_warnings", PyValue::List(cluster_warn));
    plan.insert("pledged_sells_margin_note", PyValue::List(pledged_sells));
    plan.insert("excluded", PyValue::List(excluded));
    plan.insert(
        "unpriced",
        PyValue::List(unpriced.into_iter().map(PyValue::Str).collect()),
    );
    plan.insert("est_costs", PyValue::Map(plan_cost(&cost_lines)));
    plan.insert(
        "orders",
        PyValue::List(orders.into_iter().map(PyValue::Map).collect()),
    );
    Ok(plan)
}
