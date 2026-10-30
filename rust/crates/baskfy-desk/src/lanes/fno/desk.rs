//! Ports `app.fno_desk` (`docs/fno/05` §1, §4; `06` FO8): the desk's `/fno` page, its JSON twin,
//! its one POST and the nav's `fno_badge()`.
//!
//! **The confirm is the only field on this page that moves anything.** There is no close
//! button, no settings field and no second POST. `POST /fno/execute` refuses a missing or false
//! `confirm` (400) before any read and an unknown plan (404); a known plan goes to the executor
//! (`fno_exec::execute::execute_entry` / `execute_click_exit`, with `confirm=true`), whose
//! guards refuse an expired, unissued or LIVE plan before any order.

use std::collections::BTreeMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderMap, Response};
use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use baskfy_core::fno::condor::{CondorStrikes, LegRole};
use baskfy_core::fno::config::{FoSleeve, group_of};
use baskfy_core::fno::monitor::f1_close_cost;
use baskfy_core::options::gating::Mode;

use super::fno_exec::store::{FoPlanRow, FoQuote, FoStore, PositionRow};
use super::fno_exec::{
    self,
    execute::{ExecError, FoCtx, FoOutcome, FoQuoteSource},
};
use super::gates;
use crate::app::AppState;
use crate::lanes::options::py::{self, col, int, text};
use crate::lanes::options::store::StoreError;
use crate::lanes::options::web::{self, Form};
use crate::settings::Settings;
use crate::storage::Param;
use crate::wire::{PyDict, PyValue, dict};

/// `SLEEVES`.
pub const SLEEVES: [FoSleeve; 5] = [
    FoSleeve::F1N,
    FoSleeve::F1B,
    FoSleeve::F2,
    FoSleeve::F3N,
    FoSleeve::F3B,
];
/// `DRIFT_AMBER_PCT`: a credit drifted more than this from the scan's is shown in amber.
pub const DRIFT_AMBER_PCT: i64 = 20;

/// `CONFIRM_SENTENCES["F1"]` (`05` §4, verbatim).
pub const F1_SENTENCE: &str = "This confirm also authorises this structure's 50 % profit take, its loss close at 1.5\u{d7} the credit (shorts first), and its E\u{2212}1 15:00 exit. It places nothing else.";
/// `CONFIRM_SENTENCES["F2"]`.
pub const F2_SENTENCE: &str = "This confirm also authorises this position's trailing GTT stop, its E\u{2212}1 rolls and its 40-session time exit";
/// `CONFIRM_SENTENCES["F3"]`.
pub const F3_SENTENCE: &str = "This confirm sends the wing, then the short, and nothing else. The exit \u{2014} a level break, the cut at 2\u{d7} the credit, the 80 % decay target, or 15:00 on expiry day \u{2014} is raised by the monitor and sent only when BASKFY_FNO_F3_AUTO_EXIT is on; otherwise it waits for your click here. An add is always a click.";
/// `ACTION_SENTENCES["EXIT"]`.
pub const EXIT_SENTENCE: &str =
    "This confirm closes the spread now, short first, then the wing. Nothing else.";
/// `ACTION_SENTENCES["ADD"]`.
pub const ADD_SENTENCE: &str = "This confirm sends the same two legs again, wing first, onto the open spread. It opens no new position and moves the level nowhere.";

/// `sentence_for(sleeve)`.
#[must_use]
pub fn sentence_for(sleeve: FoSleeve) -> &'static str {
    match group_of(sleeve).value() {
        "F1" => F1_SENTENCE,
        "F2" => F2_SENTENCE,
        _ => F3_SENTENCE,
    }
}

fn action_sentence(kind: &str) -> Option<&'static str> {
    match kind {
        "EXIT" => Some(EXIT_SENTENCE),
        "ADD" => Some(ADD_SENTENCE),
        _ => None,
    }
}

/// The live books by tradingsymbol (`X.QuoteSource`); an error is the exception's text.
pub type Live = BTreeMap<String, FoQuote>;

/// The page's live sources: books for tradingsymbols and the index level for an F1 symbol.
pub trait LiveSources: Send + Sync {
    /// `quotes(symbols)`.
    fn quotes<'a>(
        &'a self,
        symbols: &'a [String],
    ) -> Pin<Box<dyn Future<Output = Result<Live, String>> + Send + 'a>>;
    /// `spot(symbol)`.
    fn spot<'a>(
        &'a self,
        symbol: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Option<Decimal>, String>> + Send + 'a>>;
}

fn mode_str(m: Mode) -> &'static str {
    match m {
        Mode::Paper => "PAPER",
        Mode::Live => "LIVE",
    }
}

fn modes(s: &Settings) -> PyDict {
    let mut out = PyDict::new();
    for sleeve in SLEEVES {
        out.insert(
            sleeve.value(),
            PyValue::Str(mode_str(gates::mode_of(s, sleeve)).to_owned()),
        );
    }
    out
}

// --- small formatting ------------------------------------------------------------------------

fn iso_day(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn sd(d: Option<Decimal>) -> PyValue {
    py::vdec(d)
}

fn sday(d: Option<NaiveDate>) -> PyValue {
    d.map_or(PyValue::None, |d| PyValue::Str(iso_day(d)))
}

/// `_s(value)` of a JSON value: `None` stays `None`, a string is itself, else `str(value)`.
fn s_py(v: Option<&PyValue>) -> PyValue {
    match v {
        None | Some(PyValue::None) => PyValue::None,
        Some(PyValue::Str(s)) => PyValue::Str(s.clone()),
        Some(other) => PyValue::Str(other.py_str()),
    }
}

fn raw(v: Option<&PyValue>) -> PyValue {
    v.cloned().unwrap_or(PyValue::None)
}

fn truthy(v: Option<&PyValue>) -> bool {
    v.is_some_and(PyValue::truthy)
}

fn pct(num: Decimal, den: Decimal) -> Option<Decimal> {
    use baskfy_core::decimal::{Rounding, WideDecimal};
    if den.is_zero() {
        return None;
    }
    baskfy_core::fno::config::py::div_wide(num, den)
        .ok()?
        .mul(&WideDecimal::from(Decimal::from(100)))
        .quantize(-2, Rounding::HalfEven)
        .ok()?
        .to_decimal()
}

fn spread_pct(bid: Option<Decimal>, ask: Option<Decimal>) -> Option<Decimal> {
    let (b, a) = (bid?, ask?);
    if b <= Decimal::ZERO || a <= Decimal::ZERO {
        return None;
    }
    pct(a - b, py::div(a + b, Decimal::from(2)).ok()?)
}

fn margin_message(reasons: Option<&PyValue>) -> PyValue {
    let Some(PyValue::List(items)) = reasons else {
        return PyValue::None;
    };
    items
        .iter()
        .map(PyValue::py_str)
        .rev()
        .find(|r| r.contains("free") || r.contains("margin"))
        .map_or(PyValue::None, PyValue::Str)
}

/// `int(dt.timestamp() * 1000)`, in Python's float arithmetic.
#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
fn epoch_ms(at: DateTime<FixedOffset>) -> i64 {
    let seconds = at.timestamp_micros() as f64 / 1e6;
    (seconds * 1000.0) as i64
}

#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
fn seconds_left(expires: DateTime<FixedOffset>, now: DateTime<FixedOffset>) -> i64 {
    let us = (expires - now).num_microseconds().unwrap_or(0);
    ((us as f64 / 1e6) as i64).max(0)
}

/// Python's `repr(float)` of a value already rounded to two places.
fn py_float_repr(x: f64) -> String {
    let s = format!("{x}");
    if s.contains('.') || s.contains('e') || s.contains("inf") || s.contains("NaN") {
        s
    } else {
        format!("{s}.0")
    }
}

/// `_sigma(strike, spot, iv, days)`: `ln(K/S) / (iv √T)` to two places, or `None`.
#[must_use]
pub fn sigma(strike: Decimal, spot: Decimal, iv: f64, days: i64) -> Option<String> {
    if spot <= Decimal::ZERO || strike <= Decimal::ZERO || iv <= 0.0 || days <= 0 {
        return None;
    }
    #[allow(clippy::cast_precision_loss)]
    let width = iv * (days as f64 / 365.0).sqrt();
    let ratio = baskfy_core::fno::config::py::div_wide(strike, spot).ok()?;
    let r: f64 = ratio.to_sci_string().parse().ok()?;
    let value = r.ln() / width;
    let rounded: f64 = format!("{value:.2}").parse().ok()?;
    py::dec_from_str(&py_float_repr(rounded)).map(py::dstr)
}

// --- the view --------------------------------------------------------------------------------

type R<T> = Result<T, StoreError>;

async fn scan_credit(store: &mut FoStore<'_>, detail: &PyDict) -> R<Option<Decimal>> {
    let Some(raw_id) = detail
        .get("scan_id")
        .filter(|v| !matches!(v, PyValue::None))
    else {
        return Ok(None);
    };
    let id = py::py_int(Some(raw_id), 0);
    let sql = format!(
        "SELECT credit, detail FROM {} WHERE id = ? AND user_id = ?",
        store.t("fo_scan")
    );
    let uid = store.user_id;
    let Some(row) = store.one(&sql, &[Param::Int(id), Param::Int(uid)]).await? else {
        return Ok(None);
    };
    Ok(py::px(col(&row, "credit"))
        .or_else(|| py::px_py(py::json_obj(col(&row, "detail")).get("credit_points"))))
}

async fn plan_head(store: &mut FoStore<'_>, plan_id: &str) -> R<crate::storage::Row> {
    let sql = format!(
        "SELECT * FROM {} WHERE user_id = ? AND plan_id = ?",
        store.t("fo_plan")
    );
    let uid = store.user_id;
    Ok(store
        .one(&sql, &[Param::Int(uid), Param::Text(plan_id.to_owned())])
        .await?
        .unwrap_or_else(|| crate::storage::Row(Vec::new())))
}

async fn leg_view(store: &mut FoStore<'_>, plan: &FoPlanRow, live: &Live) -> R<Vec<PyValue>> {
    let sql = format!(
        "SELECT id, bid, ask, reference_price, status, filled_qty, avg_price \
         FROM {} WHERE plan_id = ?",
        store.t("fo_leg")
    );
    let rows = store.rows(&sql, &[Param::Int(plan.pk)]).await?;
    let stored: BTreeMap<i64, crate::storage::Row> =
        rows.into_iter().map(|r| (int(col(&r, "id")), r)).collect();
    let empty = crate::storage::Row(Vec::new());
    let mut out = Vec::new();
    for lg in &plan.legs {
        let row = stored.get(&lg.id).unwrap_or(&empty);
        let q = live.get(&lg.tradingsymbol);
        let bid = q.map_or_else(|| py::px(col(row, "bid")), |q| q.bid);
        let ask = q.map_or_else(|| py::px(col(row, "ask")), |q| q.ask);
        out.push(PyValue::Map(dict([
            ("seq", PyValue::Int(lg.entry_seq)),
            ("role", PyValue::Str(lg.role.clone())),
            ("symbol", PyValue::Str(lg.tradingsymbol.clone())),
            ("side", PyValue::Str(lg.side.clone())),
            ("quantity", PyValue::Int(lg.quantity)),
            ("strike", sd(lg.strike)),
            ("option_type", PyValue::Str(lg.option_type.clone())),
            ("bid", sd(bid)),
            ("ask", sd(ask)),
            ("spread_pct", sd(spread_pct(bid, ask))),
            (
                "oi",
                q.and_then(|q| q.oi).map_or(PyValue::None, PyValue::Int),
            ),
            ("live", PyValue::Bool(q.is_some())),
            ("reference", sd(py::px(col(row, "reference_price")))),
            ("status", py::db_to_py(col(row, "status"))),
            ("filled", py::db_to_py(col(row, "filled_qty"))),
            ("avg_price", sd(py::px(col(row, "avg_price")))),
        ])));
    }
    Ok(out)
}

fn live_credit(plan: &FoPlanRow, live: &Live) -> Option<Decimal> {
    let mut total = Decimal::ZERO;
    for lg in &plan.legs {
        let mid = live.get(&lg.tradingsymbol)?.mid()?;
        total = if lg.side == "SELL" {
            total + mid
        } else {
            total - mid
        };
    }
    Some(py::q2(total))
}

async fn plan_view(
    store: &mut FoStore<'_>,
    settings: &Settings,
    plan: &FoPlanRow,
    now: DateTime<FixedOffset>,
    live: &Live,
) -> R<PyValue> {
    let head = plan_head(store, &plan.plan_id).await?;
    let d = &plan.detail;
    let is_f1 = plan.structure == "IRON_CONDOR";
    let credit = py::px(col(&head, "credit_points"));
    let scan = if is_f1 {
        scan_credit(store, d).await?
    } else {
        None
    };
    let drift = match (credit, scan) {
        (Some(c), Some(s)) if !s.is_zero() => pct(c - s, s),
        _ => None,
    };
    let max_loss = py::px(col(&head, "max_loss_inr"));
    let budget = py::px(col(&head, "risk_budget_inr"));
    let max_loss_r = match (max_loss, budget) {
        (Some(m), Some(b)) if b > Decimal::ZERO => baskfy_core::fno::config::py::div_quantize(
            m,
            b,
            -2,
            baskfy_core::decimal::Rounding::HalfEven,
        )
        .ok(),
        _ => None,
    };
    let expires = plan.expires_at;
    let confirmable = plan.status == "ISSUED" && now < expires;
    let sequence: Vec<String> = plan
        .legs
        .iter()
        .map(|lg| lg.role.replace('_', " ").to_lowercase())
        .collect();
    let legs = leg_view(store, plan, live).await?;
    Ok(PyValue::Map(dict([
        ("plan_id", PyValue::Str(plan.plan_id.clone())),
        ("sleeve", PyValue::Str(plan.sleeve.value().to_owned())),
        (
            "group",
            PyValue::Str(group_of(plan.sleeve).value().to_owned()),
        ),
        ("symbol", PyValue::Str(plan.symbol.clone())),
        ("structure", PyValue::Str(plan.structure.clone())),
        ("status", PyValue::Str(plan.status.clone())),
        ("reason", py::db_to_py(col(&head, "reason"))),
        ("issued_at", PyValue::Str(py::iso_aware(&plan.issued_at))),
        ("expires_at", PyValue::Str(py::iso_aware(&expires))),
        ("expires_epoch_ms", PyValue::Int(epoch_ms(expires))),
        ("seconds_left", PyValue::Int(seconds_left(expires, now))),
        ("confirmable", PyValue::Bool(confirmable)),
        (
            "paper",
            PyValue::Bool(gates::mode_of(settings, plan.sleeve) == Mode::Paper),
        ),
        (
            "sentence",
            PyValue::Str(sentence_for(plan.sleeve).to_owned()),
        ),
        ("legs", PyValue::List(legs)),
        ("entry_sequence", PyValue::Str(sequence.join(" \u{2192} "))),
        ("credit", sd(credit)),
        (
            "live_credit",
            if is_f1 {
                sd(live_credit(plan, live))
            } else {
                PyValue::None
            },
        ),
        ("scan_credit", sd(scan)),
        ("credit_drift_pct", sd(drift)),
        (
            "credit_drift_amber",
            PyValue::Bool(drift.is_some_and(|x| x.abs() > Decimal::from(DRIFT_AMBER_PCT))),
        ),
        ("width", sd(py::px(col(&head, "width_points")))),
        ("profit_take", s_py(d.get("profit_take_points"))),
        ("loss_close", s_py(d.get("loss_close_points"))),
        ("entry_reference", sd(py::px(col(&head, "debit_points")))),
        ("stop", s_py(d.get("stop"))),
        ("gtt_trigger", s_py(d.get("gtt_trigger"))),
        ("time_exit_date", s_py(d.get("time_exit_date"))),
        ("roll_date", s_py(d.get("roll_date"))),
        ("lots", PyValue::Int(plan.lots)),
        ("lots_sized", raw(d.get("lots_sized"))),
        ("lots_at_ceiling", raw(d.get("lots_at_ceiling"))),
        ("lot_size", PyValue::Int(plan.lot_size)),
        ("max_loss_inr", sd(max_loss)),
        ("risk_budget_inr", sd(budget)),
        ("max_loss_r", sd(max_loss_r)),
        (
            "margin_required_inr",
            sd(py::px(col(&head, "margin_required_inr"))),
        ),
        ("margin_message", margin_message(d.get("reasons"))),
        ("cost_inr", sd(py::px(col(&head, "expected_cost_inr")))),
        ("cost_share_pct", {
            let v = col(&head, "cost_share");
            if v.is_null() {
                PyValue::None
            } else {
                PyValue::Str(py::db_to_py(v).py_str())
            }
        }),
        ("hard_exit_date", sday(plan.hard_exit_date)),
        ("kind", PyValue::Str(plan.kind.clone())),
        ("direction", raw(d.get("direction"))),
        ("level", sd(py::dec_py(d.get("level")))),
        ("index_last", sd(py::dec_py(d.get("index_last")))),
        ("decay_target", sd(py::dec_py(d.get("decay_target_mark")))),
        ("loss_cut", sd(py::dec_py(d.get("loss_cut_mark")))),
        ("rule", raw(d.get("rule"))),
    ])))
}

fn strikes_of(carry: &PyDict) -> Option<CondorStrikes> {
    let Some(PyValue::Map(raw)) = carry.get("strikes") else {
        return None;
    };
    let get = |k: &str| raw.get(k).and_then(|v| py::dec_from_str(&v.py_str()));
    Some(CondorStrikes {
        long_call: get("LONG_CALL")?,
        short_call: get("SHORT_CALL")?,
        short_put: get("SHORT_PUT")?,
        long_put: get("LONG_PUT")?,
    })
}

fn leg_role(s: &str) -> Option<LegRole> {
    match s {
        "LONG_PUT" => Some(LegRole::LongPut),
        "LONG_CALL" => Some(LegRole::LongCall),
        "SHORT_PUT" => Some(LegRole::ShortPut),
        "SHORT_CALL" => Some(LegRole::ShortCall),
        _ => None,
    }
}

/// Marks by role, in the legs' order (Python's dict).
fn marks_of(legs: &[PyDict], live: &Live) -> Vec<(String, Decimal)> {
    let mut out: Vec<(String, Decimal)> = Vec::new();
    for lg in legs {
        let symbol = py::py_text(lg.get("tradingsymbol"));
        if let Some(mid) = live.get(&symbol).and_then(FoQuote::mid) {
            let role = py::py_text(lg.get("role"));
            if let Some(slot) = out.iter_mut().find(|(r, _)| *r == role) {
                slot.1 = mid;
            } else {
                out.push((role, mid));
            }
        }
    }
    out
}

fn mark_for(marks: &[(String, Decimal)], role: &str) -> Option<Decimal> {
    marks.iter().find(|(r, _)| r == role).map(|(_, m)| *m)
}

async fn position_targets(
    store: &mut FoStore<'_>,
    position_id: i64,
) -> R<(Option<Decimal>, Option<Decimal>)> {
    let sql = format!(
        "SELECT profit_take_points, loss_close_points FROM {} WHERE id = ? AND user_id = ?",
        store.t("fo_position")
    );
    let uid = store.user_id;
    Ok(
        match store
            .one(&sql, &[Param::Int(position_id), Param::Int(uid)])
            .await?
        {
            None => (None, None),
            Some(r) => (
                py::px(col(&r, "profit_take_points")),
                py::px(col(&r, "loss_close_points")),
            ),
        },
    )
}

async fn entry_iv(store: &mut FoStore<'_>, plan_id: &str) -> R<Option<f64>> {
    let Some(plan) = store.plan(plan_id).await? else {
        return Ok(None);
    };
    let mut found = plan.detail.get("iv").cloned().unwrap_or(PyValue::None);
    if matches!(found, PyValue::None) {
        let Some(scan) = plan
            .detail
            .get("scan_id")
            .filter(|v| !matches!(v, PyValue::None))
        else {
            return Ok(None);
        };
        let sql = format!("SELECT iv FROM {} WHERE id = ?", store.t("fo_scan"));
        let id = py::py_int(Some(scan), 0);
        found = store
            .one(&sql, &[Param::Int(id)])
            .await?
            .map_or(PyValue::None, |r| py::db_to_py(col(&r, "iv")));
    }
    Ok(match found {
        PyValue::Int(i) =>
        {
            #[allow(clippy::cast_precision_loss)]
            Some(i as f64)
        }
        PyValue::Float(f) => Some(f),
        PyValue::Str(s) => s.trim().parse().ok(),
        _ => None,
    })
}

async fn last_mark(store: &mut FoStore<'_>, position_id: i64) -> R<PyValue> {
    let sql = format!(
        "SELECT trade_date, mark_points, pnl_inr FROM {} \
         WHERE position_id = ? ORDER BY trade_date DESC LIMIT 1",
        store.t("fo_mark")
    );
    Ok(match store.one(&sql, &[Param::Int(position_id)]).await? {
        None => PyValue::None,
        Some(r) => PyValue::Map(dict([
            ("trade_date", sday(py::opt_day(col(&r, "trade_date")))),
            ("mark_points", sd(py::px(col(&r, "mark_points")))),
            ("pnl_inr", sd(py::px(col(&r, "pnl_inr")))),
        ])),
    })
}

async fn level_of(sources: Option<&dyn LiveSources>, symbol: &str) -> Option<Decimal> {
    // A missing level is a dash, never a failed page.
    match sources {
        None => None,
        Some(s) => s.spot(symbol).await.ok().flatten(),
    }
}

fn opt_sub(a: Option<Decimal>, b: Option<Decimal>) -> PyValue {
    match (a, b) {
        (Some(a), Some(b)) => sd(Some(a - b)),
        _ => PyValue::None,
    }
}

fn dash(v: &PyValue) -> String {
    match v {
        PyValue::Str(s) if !s.is_empty() => s.clone(),
        _ => "\u{2014}".to_owned(),
    }
}

async fn f1_view(
    store: &mut FoStore<'_>,
    pos: &PositionRow,
    now: DateTime<FixedOffset>,
    live: &Live,
    sources: Option<&dyn LiveSources>,
) -> R<PyValue> {
    let legs = pos.leg_list();
    let marks = marks_of(&legs, live);
    let close_cost = match strikes_of(&pos.carry()) {
        None => None,
        Some(strikes) => {
            let map = marks
                .iter()
                .filter_map(|(r, m)| leg_role(r).map(|role| (role, *m)))
                .collect();
            f1_close_cost(&strikes, &map)
                .map_err(|e| StoreError::Data(e.to_string()))?
                .map(py::q2)
        }
    };
    let (profit, loss) = position_targets(store, pos.id).await?;
    let level = level_of(sources, &pos.symbol).await;
    let iv = entry_iv(store, &pos.entry_plan_id).await?;
    let mut sigmas = PyDict::new();
    for lg in &legs {
        let role = py::py_text(lg.get("role"));
        let strike = py::dec_py(lg.get("strike"));
        let expiry = py::opt_day_py(lg.get("expiry"));
        if role.starts_with("SHORT")
            && let (Some(strike), Some(expiry)) = (strike, expiry)
        {
            let days = (expiry - now.date_naive()).num_days();
            let v = match (level, iv) {
                (Some(l), Some(iv)) => {
                    sigma(strike, l, iv, days).map_or(PyValue::None, PyValue::Str)
                }
                _ => PyValue::None,
            };
            sigmas.insert(role, v);
        }
    }
    let pnl = match (close_cost, pos.entry_credit) {
        (Some(c), Some(e)) => (e - c)
            .checked_mul(Decimal::from(pos.lots * pos.lot_size))
            .map(py::q2),
        _ => None,
    };
    let when = pos
        .hard_exit_date
        .map_or_else(|| "\u{2014}".to_owned(), iso_day);
    let next_rule = format!(
        "the E-1 exit at 15:00 on {when}, unless the profit take (close at <= {}) or the loss \
         close (>= {}) fires first",
        dash(&sd(profit)),
        dash(&sd(loss))
    );
    let lm = last_mark(store, pos.id).await?;
    let leg_rows: Vec<PyValue> = legs
        .iter()
        .map(|lg| {
            let role = py::py_text(lg.get("role"));
            PyValue::Map(dict([
                ("role", raw(lg.get("role"))),
                ("symbol", raw(lg.get("tradingsymbol"))),
                ("avg_price", s_py(lg.get("avg_price"))),
                ("mid", sd(mark_for(&marks, &role))),
            ]))
        })
        .collect();
    Ok(PyValue::Map(dict([
        ("id", PyValue::Int(pos.id)),
        ("sleeve", PyValue::Str(pos.sleeve.value().to_owned())),
        ("symbol", PyValue::Str(pos.symbol.clone())),
        ("entry_plan_id", PyValue::Str(pos.entry_plan_id.clone())),
        ("lots", PyValue::Int(pos.lots)),
        ("lot_size", PyValue::Int(pos.lot_size)),
        ("entry_credit", sd(pos.entry_credit)),
        ("live_close_cost", sd(close_cost)),
        ("live_pnl_inr", sd(pnl)),
        ("max_loss_inr", sd(pos.max_loss_inr)),
        ("profit_take", sd(profit)),
        ("loss_close", sd(loss)),
        ("to_profit_take", opt_sub(close_cost, profit)),
        ("to_loss_close", opt_sub(loss, close_cost)),
        ("spot", sd(level)),
        ("short_sigma", PyValue::Map(sigmas)),
        ("hard_exit_date", sday(pos.hard_exit_date)),
        ("next_rule", PyValue::Str(next_rule)),
        ("last_mark", lm),
        ("simulated", PyValue::Bool(pos.simulated)),
        ("legs", PyValue::List(leg_rows)),
    ])))
}

async fn f3_view(
    store: &mut FoStore<'_>,
    pos: &PositionRow,
    live: &Live,
    sources: Option<&dyn LiveSources>,
) -> R<PyValue> {
    let legs = pos.leg_list();
    let carry = pos.carry();
    let marks = marks_of(&legs, live);
    let short_mark = marks
        .iter()
        .find(|(r, _)| r.starts_with("SHORT"))
        .map(|(_, m)| *m);
    let wing_mark = marks
        .iter()
        .find(|(r, _)| r.starts_with("LONG"))
        .map(|(_, m)| *m);
    let wing = wing_mark.filter(|w| !w.is_zero()).unwrap_or(Decimal::ZERO);
    let mark = short_mark.map(|s| py::q2(s - wing));
    let level = level_of(sources, &pos.symbol).await;
    let (target, cut) = position_targets(store, pos.id).await?;
    let pnl = match (mark, pos.entry_credit) {
        (Some(m), Some(e)) => (e - m)
            .checked_mul(Decimal::from(pos.lots * pos.lot_size))
            .map(py::q2),
        _ => None,
    };
    let direction = match carry.get("direction") {
        Some(v) if v.truthy() => v.py_str(),
        _ => String::new(),
    };
    let beyond = if direction == "UP" { "below" } else { "above" };
    let when = pos
        .hard_exit_date
        .map_or_else(|| "\u{2014}".to_owned(), iso_day);
    let next_rule = format!(
        "out at once if {} trades {beyond} {}; the cut at a mark of {}; the target at {}; flat \
         at 15:00 on {when}",
        pos.symbol,
        dash(&sd(pos.stop_price)),
        dash(&sd(cut)),
        dash(&sd(target))
    );
    let lm = last_mark(store, pos.id).await?;
    let leg_rows: Vec<PyValue> = legs
        .iter()
        .map(|lg| {
            let role = py::py_text(lg.get("role"));
            PyValue::Map(dict([
                ("role", raw(lg.get("role"))),
                ("symbol", raw(lg.get("tradingsymbol"))),
                ("quantity", raw(lg.get("quantity"))),
                ("avg_price", s_py(lg.get("avg_price"))),
                ("mid", sd(mark_for(&marks, &role))),
            ]))
        })
        .collect();
    Ok(PyValue::Map(dict([
        ("id", PyValue::Int(pos.id)),
        ("sleeve", PyValue::Str(pos.sleeve.value().to_owned())),
        ("symbol", PyValue::Str(pos.symbol.clone())),
        ("entry_plan_id", PyValue::Str(pos.entry_plan_id.clone())),
        ("lots", PyValue::Int(pos.lots)),
        ("lot_size", PyValue::Int(pos.lot_size)),
        ("direction", PyValue::Str(direction)),
        ("level", sd(pos.stop_price)),
        ("spot", sd(level)),
        ("entry_credit", sd(pos.entry_credit)),
        ("mark", sd(mark)),
        ("live_pnl_inr", sd(pnl)),
        ("max_loss_inr", sd(pos.max_loss_inr)),
        ("decay_target", sd(target)),
        ("loss_cut", sd(cut)),
        ("expiry", PyValue::Str(when)),
        ("added", PyValue::Bool(truthy(carry.get("added")))),
        ("next_rule", PyValue::Str(next_rule)),
        ("last_mark", lm),
        ("simulated", PyValue::Bool(pos.simulated)),
        ("legs", PyValue::List(leg_rows)),
    ])))
}

async fn f2_view(store: &mut FoStore<'_>, pos: &PositionRow, live: &Live) -> R<PyValue> {
    let legs = pos.leg_list();
    let first = legs.first();
    let symbol = first.map_or_else(String::new, |l| py::py_text(l.get("tradingsymbol")));
    let q = live.get(&symbol);
    let last = q.and_then(|q| q.last.filter(|l| !l.is_zero()).or_else(|| q.mid()));
    let carry = pos.carry();
    let entry = first
        .and_then(|l| py::dec_py(l.get("avg_price")))
        .filter(|e| !e.is_zero())
        .or(pos.entry_price);
    let pnl = match (last, entry) {
        (Some(l), Some(e)) => {
            let qty = first.map_or(0, |l| py::py_int(l.get("quantity"), 0));
            (l - e).checked_mul(Decimal::from(qty)).map(py::q2)
        }
        _ => None,
    };
    let roll = sday(pos.next_roll_date);
    let time_exit = s_py(carry.get("time_exit_date"));
    let next_rule = format!(
        "the stop at {}; the E-1 roll at 15:00 on {}; the time exit on {}",
        dash(&sd(pos.stop_price)),
        dash(&roll),
        dash(&time_exit)
    );
    let lm = last_mark(store, pos.id).await?;
    Ok(PyValue::Map(dict([
        ("id", PyValue::Int(pos.id)),
        ("sleeve", PyValue::Str(pos.sleeve.value().to_owned())),
        ("symbol", PyValue::Str(pos.symbol.clone())),
        ("contract", PyValue::Str(symbol)),
        ("entry_plan_id", PyValue::Str(pos.entry_plan_id.clone())),
        ("lots", PyValue::Int(pos.lots)),
        ("lot_size", PyValue::Int(pos.lot_size)),
        ("entry_price", sd(entry)),
        ("last", sd(last)),
        ("live_pnl_inr", sd(pnl)),
        (
            "gtt_id",
            pos.gtt_id.clone().map_or(PyValue::None, PyValue::Str),
        ),
        ("gtt_trigger", sd(pos.stop_price)),
        (
            "gtt_resting",
            PyValue::Bool(pos.gtt_id.as_ref().is_some_and(|g| !g.is_empty())),
        ),
        ("trail_stop", s_py(carry.get("trail_stop"))),
        ("rolls", raw(carry.get("rolls"))),
        ("roll_date", roll),
        ("time_exit_date", time_exit),
        ("next_rule", PyValue::Str(next_rule)),
        ("last_mark", lm),
        ("simulated", PyValue::Bool(pos.simulated)),
    ])))
}

async fn actions(
    store: &mut FoStore<'_>,
    day: NaiveDate,
    now: DateTime<FixedOffset>,
) -> R<Vec<PyValue>> {
    let sql = format!(
        "SELECT plan_id, parent_plan_id, sleeve, symbol, kind, status, reason, issued_at, \
         expires_at, detail FROM {} WHERE user_id = ? AND kind IN \
         ('EXIT', 'ROLL', 'ADD') AND (trade_date = ? OR status IN ('ISSUED', 'CONFIRMED', \
         'FILLING')) ORDER BY id DESC",
        store.t("fo_plan")
    );
    let uid = store.user_id;
    let rows = store
        .rows(&sql, &[Param::Int(uid), Param::Date(day)])
        .await?;
    let mut out = Vec::new();
    for r in &rows {
        let detail = py::json_obj(col(r, "detail"));
        let expires = py::aware(col(r, "expires_at"))
            .ok_or_else(|| StoreError::Data("TypeError: expires_at is not a datetime".into()))?;
        let kind = text(col(r, "kind"));
        let confirmable =
            text(col(r, "status")) == "ISSUED" && action_sentence(&kind).is_some() && now < expires;
        out.push(PyValue::Map(dict([
            ("plan_id", py::db_to_py(col(r, "plan_id"))),
            ("parent_plan_id", py::db_to_py(col(r, "parent_plan_id"))),
            ("sleeve", py::db_to_py(col(r, "sleeve"))),
            ("symbol", py::db_to_py(col(r, "symbol"))),
            ("kind", py::db_to_py(col(r, "kind"))),
            ("status", py::db_to_py(col(r, "status"))),
            ("code", raw(detail.get("code"))),
            ("message", raw(detail.get("message"))),
            ("reason", py::db_to_py(col(r, "reason"))),
            (
                "issued_at",
                py::aware(col(r, "issued_at"))
                    .map_or(PyValue::None, |t| PyValue::Str(py::iso_aware(&t))),
            ),
            ("confirmable", PyValue::Bool(confirmable)),
            (
                "awaits_click",
                PyValue::Bool(truthy(detail.get("awaits_click"))),
            ),
            (
                "sentence",
                action_sentence(&kind).map_or(PyValue::None, |s| PyValue::Str(s.to_owned())),
            ),
            ("expires_epoch_ms", PyValue::Int(epoch_ms(expires))),
        ])));
    }
    Ok(out)
}

async fn violations(store: &mut FoStore<'_>, positions: &[PositionRow]) -> R<Vec<PyValue>> {
    let mut out = Vec::new();
    for pos in positions {
        let plan = store.plan(&pos.entry_plan_id).await?;
        let Some(PyValue::List(items)) = plan.as_ref().and_then(|p| p.detail.get("violations"))
        else {
            continue;
        };
        for v in items {
            if let PyValue::Map(m) = v {
                let mut row = dict([
                    ("position_id", PyValue::Int(pos.id)),
                    ("symbol", PyValue::Str(pos.symbol.clone())),
                ]);
                for (k, val) in m.iter() {
                    row.insert(k, val.clone());
                }
                out.push(PyValue::Map(row));
            }
        }
    }
    Ok(out)
}

/// `badge_of(positions)`: the open structures and their nearest hard-exit date (F1's `E - 1`;
/// F2's time exit).
#[must_use]
pub fn badge_of(positions: &[PositionRow]) -> PyDict {
    let mut dates: Vec<NaiveDate> = Vec::new();
    for pos in positions {
        if let Some(d) = pos.hard_exit_date {
            dates.push(d);
        }
        if let Some(d) = py::opt_day_py(pos.carry().get("time_exit_date")) {
            dates.push(d);
        }
    }
    dict([
        (
            "open",
            PyValue::Int(i64::try_from(positions.len()).unwrap_or(i64::MAX)),
        ),
        ("nearest", sday(dates.into_iter().min())),
    ])
}

/// `build_view(store, now=, quotes=, spot=, live_note=)`: today's entry plans, the open book and
/// the actions under confirms, as `/fno` shows them.
///
/// # Errors
/// The database, or a row Python would refuse.
pub async fn build_view(
    store: &mut FoStore<'_>,
    settings: &Settings,
    now: DateTime<FixedOffset>,
    sources: Option<&dyn LiveSources>,
    live_note: Option<String>,
) -> R<PyDict> {
    let day = now.with_timezone(&py::ist()).date_naive();
    let sql = format!(
        "SELECT plan_id FROM {} WHERE user_id = ? AND trade_date = ? \
         AND kind = 'ENTRY' ORDER BY issued_at, id",
        store.t("fo_plan")
    );
    let uid = store.user_id;
    let ids: Vec<String> = store
        .rows(&sql, &[Param::Int(uid), Param::Date(day)])
        .await?
        .iter()
        .map(|r| text(col(r, "plan_id")))
        .collect();
    let mut plans = Vec::new();
    for id in &ids {
        if let Some(p) = store.plan(id).await? {
            plans.push(p);
        }
    }
    let positions = store.open_positions().await?;
    let mut symbols: Vec<String> = plans
        .iter()
        .filter(|p| p.status == "ISSUED")
        .flat_map(|p| p.legs.iter().map(|l| l.tradingsymbol.clone()))
        .collect();
    symbols.extend(positions.iter().flat_map(|p| {
        p.leg_list()
            .into_iter()
            .map(|l| py::py_text(l.get("tradingsymbol")))
    }));
    let mut live = Live::new();
    let mut note = live_note;
    if let Some(src) = sources
        && !symbols.is_empty()
    {
        match src.quotes(&symbols).await {
            Ok(found) => live = found,
            Err(e) => note = Some(format!("live quotes unavailable ({e})")),
        }
    }
    let mut plan_views = Vec::new();
    for p in &plans {
        plan_views.push(plan_view(store, settings, p, now, &live).await?);
    }
    let mut structures = Vec::new();
    let mut futures = Vec::new();
    let mut spreads = Vec::new();
    for p in positions.iter().filter(|p| p.structure == "IRON_CONDOR") {
        structures.push(f1_view(store, p, now, &live, sources).await?);
    }
    for p in positions.iter().filter(|p| p.structure == "FUTURE") {
        futures.push(f2_view(store, p, &live).await?);
    }
    for p in positions.iter().filter(|p| p.structure == "CREDIT_SPREAD") {
        spreads.push(f3_view(store, p, &live, sources).await?);
    }
    let acts = actions(store, day, now).await?;
    let viols = violations(store, &positions).await?;
    Ok(dict([
        ("day", PyValue::Str(iso_day(day))),
        ("now", PyValue::Str(py::iso_aware(&now))),
        ("modes", PyValue::Map(modes(settings))),
        ("live_note", note.map_or(PyValue::None, PyValue::Str)),
        ("plans", PyValue::List(plan_views)),
        ("structures", PyValue::List(structures)),
        ("futures", PyValue::List(futures)),
        ("spreads", PyValue::List(spreads)),
        (
            "f3_auto_exit",
            PyValue::Bool(gates::f3_auto_exit_enabled(settings)),
        ),
        ("actions", PyValue::List(acts)),
        ("violations", PyValue::List(viols)),
        ("badge", PyValue::Map(badge_of(&positions))),
    ]))
}

// --- the live sources over the desk's Kite -----------------------------------------------------

struct KiteSources {
    kite: Arc<crate::lanes::exec_core::kite_client::DeskKite>,
}

fn levels(raw: Option<&PyValue>) -> Vec<baskfy_core::options::execution::Level> {
    let Some(PyValue::List(items)) = raw else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|it| match it {
            PyValue::Map(m) => {
                let price = m
                    .get("price")
                    .filter(|v| v.truthy())
                    .and_then(|v| py::dec_py(Some(v)))
                    .unwrap_or(Decimal::ZERO);
                let qty = py::py_int(m.get("quantity"), 0);
                (price > Decimal::ZERO && qty > 0).then_some(
                    baskfy_core::options::execution::Level {
                        price,
                        quantity: qty,
                    },
                )
            }
            _ => None,
        })
        .collect()
}

impl LiveSources for KiteSources {
    fn quotes<'a>(
        &'a self,
        symbols: &'a [String],
    ) -> Pin<Box<dyn Future<Output = Result<Live, String>> + Send + 'a>> {
        Box::pin(async move {
            let mut keys: Vec<String> = Vec::new();
            for s in symbols {
                let k = format!("NFO:{s}");
                if !keys.contains(&k) {
                    keys.push(k);
                }
            }
            let mut out = Live::new();
            for chunk in keys.chunks(500) {
                let payload = self
                    .kite
                    .quote_raw(chunk)
                    .await
                    .map_err(|e| format!("{}: {}", e.python_type, e.message))?;
                let PyValue::Map(m) = payload else { continue };
                for (key, q) in m.iter() {
                    let PyValue::Map(q) = q else { continue };
                    let depth = match q.get("depth") {
                        Some(PyValue::Map(d)) => d.clone(),
                        _ => PyDict::new(),
                    };
                    let bids = levels(depth.get("buy"));
                    let asks = levels(depth.get("sell"));
                    let name = key.strip_prefix("NFO:").unwrap_or(key).to_owned();
                    out.insert(
                        name,
                        FoQuote {
                            bid: bids.first().map(|l| l.price),
                            ask: asks.first().map(|l| l.price),
                            bids,
                            asks,
                            last: py::dec_py(q.get("last_price")),
                            oi: Some(py::py_int(q.get("oi"), 0)),
                        },
                    );
                }
            }
            Ok(out)
        })
    }

    fn spot<'a>(
        &'a self,
        symbol: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Option<Decimal>, String>> + Send + 'a>> {
        Box::pin(async move {
            let name = match symbol {
                "NIFTY" => "NIFTY 50",
                "BANKNIFTY" => "NIFTY BANK",
                _ => return Ok(None),
            };
            let found = self
                .kite
                .ltp(&[name.to_owned()], "NSE")
                .await
                .map_err(|e| format!("{}: {}", e.python_type, e.message))?;
            Ok(found.get(name).and_then(|v| py::dec_py(Some(v))))
        })
    }
}

async fn view_sources(state: &AppState) -> (Option<KiteSources>, Option<String>) {
    let desk = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>());
    let Some(desk) = desk else {
        return (
            None,
            Some("Kite unavailable (RuntimeError); legs show the 09:20 pricing".to_owned()),
        );
    };
    let kite = desk.kite();
    if !kite.is_authed().await {
        return (
            None,
            Some("no Kite session; legs show the 09:20 pricing".to_owned()),
        );
    }
    (Some(KiteSources { kite }), None)
}

async fn view(state: &AppState) -> PyDict {
    let settings = state.settings_snapshot();
    let (sources, note) = view_sources(state).await;
    let built: R<PyDict> = async {
        let mut conn = state.db.connect().await?;
        let mut store = FoStore::new(&mut conn, settings.sole_user_id);
        let now = state.clock.now_ist();
        build_view(
            &mut store,
            &settings,
            now,
            sources.as_ref().map(|s| s as &dyn LiveSources),
            note,
        )
        .await
    }
    .await;
    match built {
        Ok(v) => {
            let mut out = dict([("ok", PyValue::Bool(true))]);
            for (k, val) in v.iter() {
                out.insert(k, val.clone());
            }
            out
        }
        Err(e) => dict([
            ("ok", PyValue::Bool(false)),
            ("error", PyValue::Str(e.py_text())),
            ("plans", PyValue::List(Vec::new())),
            ("structures", PyValue::List(Vec::new())),
            ("futures", PyValue::List(Vec::new())),
            ("actions", PyValue::List(Vec::new())),
            ("violations", PyValue::List(Vec::new())),
            ("modes", PyValue::Map(modes(&settings))),
            (
                "badge",
                PyValue::Map(dict([("open", PyValue::None), ("nearest", PyValue::None)])),
            ),
        ]),
    }
}

// --- the nav badge (every desk page) ---------------------------------------------------------

const BADGE_TTL: Duration = Duration::from_secs(60);

type BadgeCache = Mutex<Option<(Instant, Option<PyDict>)>>;

fn badge_cache() -> &'static BadgeCache {
    static CACHE: OnceLock<BadgeCache> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(None))
}

/// `_badge_cache["at"] = -1e9`: the next nav reads the book again.
pub fn reset_badge() {
    if let Ok(mut c) = badge_cache().lock() {
        *c = None;
    }
}

async fn read_badge(state: &AppState) -> Option<PyDict> {
    let user = state.settings_snapshot().sole_user_id;
    let mut conn = state.db.connect().await.ok()?;
    let mut store = FoStore::new(&mut conn, user);
    store.open_positions().await.ok().map(|p| badge_of(&p))
}

/// `nav_badge()`: the `F&O Overnight` tab's badge and the sleeves' modes, cached a minute;
/// never fails — a nav that cannot read the book shows the modes alone. The book is read only
/// from a multi-threaded runtime (the served desk); elsewhere the badge shows the modes alone.
#[must_use]
pub fn nav_badge(state: Option<&AppState>) -> PyDict {
    let settings = state.map_or_else(
        || Settings::from_env().or_else(|_| Settings::defaults()).ok(),
        |s| Some(s.settings_snapshot()),
    );
    let modes = settings.as_ref().map_or_else(PyDict::new, modes);
    let cached = badge_cache().lock().ok().and_then(|c| c.clone());
    let value = match cached {
        Some((at, value)) if at.elapsed() < BADGE_TTL => value,
        _ => {
            let value = state.and_then(|s| {
                let handle = tokio::runtime::Handle::try_current().ok()?;
                if handle.runtime_flavor() != tokio::runtime::RuntimeFlavor::MultiThread {
                    return None;
                }
                tokio::task::block_in_place(|| handle.block_on(read_badge(s)))
            });
            if let Ok(mut c) = badge_cache().lock() {
                *c = Some((Instant::now(), value.clone()));
            }
            value
        }
    };
    let mut out =
        value.unwrap_or_else(|| dict([("open", PyValue::None), ("nearest", PyValue::None)]));
    out.insert("modes", PyValue::Map(modes));
    out
}

// --- the routes --------------------------------------------------------------------------------

/// `GET /fno`.
pub async fn page(State(state): State<AppState>) -> Response<Body> {
    super::install(&state);
    let v = view(&state).await;
    web::render(&state, "/fno", "fno.html", dict([("v", PyValue::Map(v))]))
}

/// `GET /fno/data`.
pub async fn data(State(state): State<AppState>) -> Response<Body> {
    super::install(&state);
    web::json(200, &PyValue::Map(view(&state).await))
}

struct ExecQuotes {
    kite: Option<Arc<crate::lanes::exec_core::kite_client::DeskKite>>,
}

#[async_trait::async_trait]
impl FoQuoteSource for ExecQuotes {
    async fn quotes(&self, symbols: &[String]) -> Result<Live, ExecError> {
        let Some(kite) = self.kite.clone() else {
            return Err(ExecError::Other(
                "RuntimeError: the desk's Kite client is not built".to_owned(),
            ));
        };
        KiteSources { kite }
            .quotes(symbols)
            .await
            .map_err(ExecError::Other)
    }
}

#[allow(clippy::print_stderr)]
fn alert_log(message: &str) {
    eprintln!("ERROR:app.fno_execute:{message}");
}

async fn confirm_plan(state: &AppState, plan: &FoPlanRow) -> Result<FoOutcome, ExecError> {
    let settings = state.settings_snapshot();
    // `gateway_for(sleeve)`: `app.main.gateway()` first (the shared risk manager, built as a
    // side effect), then the sleeve's paper-pinned FO gateway.
    let gateway = fno_exec::execute::fo_gateway_ready(state, plan.sleeve)
        .await
        .ok_or_else(|| {
            ExecError::Other("RuntimeError: the FO gateway could not be built".to_owned())
        })?;
    let kite = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
        .map(crate::lanes::exec_core::desk::DeskGateway::kite);
    let quotes = ExecQuotes { kite };
    let clock = Arc::clone(&state.clock);
    let now = move || clock.now_ist();
    let book = |filled: &[baskfy_core::fno::covered::OptionPosition]| {
        fno_exec::execute::paper_book(filled)
    };
    // `execute_entry(alert=log.error)`: the executor's default, on the `app.fno_execute` logger.
    let alert = |m: &str| alert_log(m);
    let ledger = super::ledger::DeskLedger;
    let ctx = FoCtx {
        quotes: &quotes,
        now: &now,
        broker_book: &book,
        alert: &alert,
        ledger: &ledger,
        tenant: fno_exec::execute::tenancy(&settings).ok(),
    };
    let mut conn = state
        .db
        .connect()
        .await
        .map_err(|e| ExecError::Other(e.to_string()))?;
    let mut store = FoStore::new(&mut conn, settings.sole_user_id);
    if plan.kind == "EXIT" {
        // An F3 exit the monitor raised for the click (M.5): confirmed and closed here.
        fno_exec::execute::execute_click_exit(&mut store, &gateway, &ctx, &plan.plan_id, true).await
    } else {
        let modes = move |s: FoSleeve| gates::mode_of(&settings, s);
        fno_exec::execute::execute_entry(&mut store, &gateway, &ctx, &plan.plan_id, true, &modes)
            .await
    }
}

/// `POST /fno/execute` (form `plan_id`, `confirm`, both defaulting to `""`): the one route that
/// moves anything. A missing or false `confirm` is refused by name before any read.
pub async fn execute(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response<Body> {
    super::install(&state);
    let form = Form::new(web::content_type(&headers).as_deref(), &body);
    let plan_id = form.optional("plan_id", "");
    let confirm = form.optional("confirm", "");
    if confirm != "true" {
        return web::refusal(400, "CONFIRM_REQUIRED", "confirm must be true");
    }
    let settings = state.settings_snapshot();
    let plan = async {
        let mut conn = state.db.connect().await?;
        let mut store = FoStore::new(&mut conn, settings.sole_user_id);
        store.plan(&plan_id).await
    }
    .await;
    let plan = match plan {
        Ok(Some(p)) => p,
        Ok(None) => return web::refusal(404, "UNKNOWN_PLAN", "no such plan"),
        Err(e) => return web::internal_error(&e.py_text()),
    };
    let outcome = match confirm_plan(&state, &plan).await {
        Ok(o) => o,
        Err(ExecError::Refused(r)) => return web::refusal(r.status, &r.code, &r.message),
        Err(ExecError::Other(m)) => return web::internal_error(&m),
    };
    reset_badge(); // the tab's count changes with this confirm
    let mut body = fno_exec::execute::outcome_json(&outcome);
    if outcome.outcome != "OPEN" && outcome.outcome != "CLOSED" {
        let refusals: Vec<String> = match outcome.detail.get("refusals") {
            Some(PyValue::List(items)) => items.iter().map(PyValue::py_str).collect(),
            _ => Vec::new(),
        };
        let detail = if refusals.is_empty() {
            outcome.outcome.clone()
        } else {
            refusals.join("; ")
        };
        body.insert("code", PyValue::Str(outcome.outcome.clone()));
        body.insert("detail", PyValue::Str(detail));
    }
    web::json(200, &PyValue::Map(body))
}

#[cfg(test)]
mod tests {
    //! The structural and pure halves of `tests/test_fno_desk.py`.
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use chrono::TimeZone;

    use super::*;

    #[test]
    fn the_sentences_are_05s_verbatim() {
        assert_eq!(sentence_for(FoSleeve::F1N), F1_SENTENCE);
        assert_eq!(sentence_for(FoSleeve::F1B), F1_SENTENCE);
        assert_eq!(sentence_for(FoSleeve::F2), F2_SENTENCE);
        assert_eq!(sentence_for(FoSleeve::F3N), F3_SENTENCE);
        assert!(F1_SENTENCE.contains("1.5\u{d7} the credit") && F1_SENTENCE.contains("E\u{2212}1"));
        assert!(F3_SENTENCE.contains("BASKFY_FNO_F3_AUTO_EXIT"));
    }

    #[test]
    fn one_post_and_no_get_executes() {
        let src = include_str!("../fno.rs");
        let posts: Vec<&str> = src.match_indices("post(").map(|(i, _)| &src[i..]).collect();
        assert_eq!(posts.len(), 1, "one POST route");
        assert!(src.contains("\"/fno/execute\", post(desk::execute)"));
        assert!(
            !regex::Regex::new(r#"get\([^)]*execute"#)
                .unwrap()
                .is_match(src)
        );
    }

    #[test]
    fn nothing_here_reaches_the_broker_around_the_executor() {
        let src = include_str!("desk.rs");
        let code = src.split("#[cfg(test)]").next().unwrap();
        assert!(code.contains("execute_entry(") && code.contains("execute_click_exit("));
        for word in [
            "place_gtt",
            ".place_order(",
            ".place(",
            concat!("Kite", "Writes"),
        ] {
            assert!(!code.contains(word), "{word}");
        }
    }

    #[test]
    fn the_page_offers_two_forms_and_both_are_the_confirm() {
        let html =
            include_str!("../../../../../../kite-momentum-rebalancer/app/templates/fno.html");
        let re = regex::Regex::new(r#"<form[^>]*action="([^"]+)""#).unwrap();
        let actions: Vec<&str> = re
            .captures_iter(html)
            .map(|c| c.get(1).unwrap().as_str())
            .collect();
        assert_eq!(actions, ["/fno/execute", "/fno/execute"]);
        let inputs = regex::Regex::new(r"<input[^>]*>").unwrap();
        assert!(
            inputs
                .find_iter(html)
                .all(|m| m.as_str().contains("hidden"))
        );
    }

    #[test]
    fn the_badge_is_the_count_and_the_nearest_exit() {
        let ist = py::ist();
        let pos = |id: i64, hard: Option<NaiveDate>, time_exit: Option<&str>| PositionRow {
            id,
            sleeve: FoSleeve::F1N,
            symbol: "NIFTY".into(),
            structure: "IRON_CONDOR".into(),
            entry_plan_id: "P".into(),
            legs: dict([(
                "carry",
                PyValue::Map(dict([(
                    "time_exit_date",
                    time_exit.map_or(PyValue::None, |s| PyValue::Str(s.into())),
                )])),
            )]),
            lots: 1,
            lot_size: 75,
            entry_price: None,
            entry_credit: None,
            max_loss_inr: None,
            stop_price: None,
            gtt_id: None,
            hard_exit_date: hard,
            next_roll_date: None,
            opened_at: ist.with_ymd_and_hms(2026, 11, 2, 9, 30, 0).unwrap(),
            simulated: true,
        };
        let d = |m, d| NaiveDate::from_ymd_opt(2026, m, d);
        let out = badge_of(&[pos(1, d(11, 24), None), pos(2, None, Some("2026-11-20"))]);
        assert_eq!(
            py::dumps(&PyValue::Map(out)),
            "{\"open\": 2, \"nearest\": \"2026-11-20\"}"
        );
        assert_eq!(
            py::dumps(&PyValue::Map(badge_of(&[]))),
            "{\"open\": 0, \"nearest\": null}"
        );
    }

    #[test]
    fn the_shipped_modes_are_paper_in_sleeve_order() {
        let s = Settings::defaults().unwrap();
        assert_eq!(
            py::dumps(&PyValue::Map(modes(&s))),
            "{\"F1N\": \"PAPER\", \"F1B\": \"PAPER\", \"F2\": \"PAPER\", \"F3N\": \"PAPER\", \
             \"F3B\": \"PAPER\"}"
        );
    }

    #[test]
    fn small_arithmetic_is_pythons() {
        assert_eq!(
            spread_pct(Some(Decimal::new(600, 1)), Some(Decimal::new(610, 1))),
            Some(Decimal::new(165, 2))
        );
        assert_eq!(spread_pct(Some(Decimal::ZERO), Some(Decimal::ONE)), None);
        assert_eq!(pct(Decimal::ONE, Decimal::ZERO), None);
        assert_eq!(
            sigma(Decimal::from(26000), Decimal::from(25000), 0.12, 0),
            None
        );
        assert_eq!(
            sigma(Decimal::from(26000), Decimal::from(25000), 0.12, 14).as_deref(),
            Some("1.67")
        );
        let at = py::ist().with_ymd_and_hms(2026, 11, 18, 9, 50, 0).unwrap();
        assert_eq!(epoch_ms(at), 1_794_975_600_000);
        assert_eq!(seconds_left(at, at + chrono::Duration::seconds(5)), 0);
        assert_eq!(
            margin_message(Some(&PyValue::List(vec![
                PyValue::Str("x".into()),
                PyValue::Str("margin \u{20b9}1 > free \u{20b9}0".into()),
            ]))),
            PyValue::Str("margin \u{20b9}1 > free \u{20b9}0".into())
        );
    }
}
