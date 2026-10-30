//! Ports `app/lifecycle.py` (LV6 — review P1.4, P2.3): one visible lifecycle per trade across
//! the swing, TWT and VBT books, and the explicit adoption of a holding bought by hand.
//!
//! **What the page answers.** For every open position the three books hold: how many shares
//! filled and are still open, the entry, the initial and current stop, the rupees at risk
//! between entry and stop, what the **broker** says about the stop (`ARMED`, `NAKED`,
//! `GTT_MISSING`, `GTT_OVERSIZED`/`GTT_UNDERSIZED`, `TRIGGERED_UNFILLED`, or `UNVERIFIED` without
//! a Kite session), the exact exit rule the sleeve runs, the next action, whether it is overdue,
//! and every open finding the reconciler (LV2) has recorded against it.
//!
//! A GTT that fired is not a fill (Kite's GTT places a LIMIT order); the row says so and stays
//! unresolved until the position closes.
//!
//! **Adoption.** `POST /lifecycle/adopt` with `confirm=true` puts a holding Maulik bought in the
//! Kite app into a sleeve's book **on his stated cost and quantity**, links the GTT he armed by
//! hand or arms one through that sleeve's own gateway (non-negotiable 4), and records the act in
//! `lv_adoption`. It never claims a holding silently: a name the sleeve already holds is
//! refused, an unknown symbol is refused, a missing `confirm` writes nothing.
//!
//! The page is read-only apart from that one route. Nothing here places a buy, sells, or
//! cancels a stop; the only broker write it can cause is a GTT armed — **through the sleeve's
//! gateway** (`Gateway::place_gtt_stop`, guards → risk → idempotency → rate limit → journal,
//! DRY_RUN-aware) — for shares the person says he holds. No order verb is named here.
//!
//! The pieces of `app/reconcile.py` the page reads (`GttRow`, `gtt_index`, the GTT status sets,
//! `SIMULATED_GTT_PREFIX`, `PgIssueStore.open_issues`) are ported beside it, privately: the
//! reconciler itself is the weekly lane's.

use std::collections::HashMap;
use std::str::FromStr as _;
use std::sync::Arc;

use async_trait::async_trait;
use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{Response, Uri};
use axum::routing::{get, post};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use super::exit_rules::exit_rules;
use crate::app::AppState;
use crate::gateway::{Gateway, GatewayError, GttStop, TenantIds};
use crate::lanes::twt::common::{
    HttpError, Rec, Val, db_int, db_stamp, db_text, dstr, ist, json_response, jsonable_rec,
    parse_form, py_decimal, quantize, server_error,
};
use crate::settings::Settings;
use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::{PyDict, PyValue};

/// `SLEEVES`, in the page's order.
pub const SLEEVES: [&str; 3] = ["swing", "twt", "vbt"];

/// The broker rests the stop for the open quantity.
pub const ARMED: &str = "ARMED";
/// No stop at all.
pub const NAKED: &str = "NAKED";
/// The broker no longer lists the stop.
pub const GTT_MISSING: &str = "GTT_MISSING";
/// The stop rests for more shares than are held.
pub const GTT_OVERSIZED: &str = "GTT_OVERSIZED";
/// The stop rests for fewer shares than are held.
pub const GTT_UNDERSIZED: &str = "GTT_UNDERSIZED";
/// The stop fired and the shares are still in the book.
pub const TRIGGERED_UNFILLED: &str = "TRIGGERED_UNFILLED";
/// No Kite session: the stop cannot be checked.
pub const UNVERIFIED: &str = "UNVERIFIED";

/// `OVERDUE_AFTER`: a finding open this long is overdue.
pub const OVERDUE_AFTER_MINUTES: i64 = 30;

/// `reconcile.GTT_TRIGGERED`.
pub const GTT_TRIGGERED: &str = "triggered";
/// `reconcile.GTT_GONE_STATUSES`.
pub const GTT_GONE_STATUSES: [&str; 5] =
    ["cancelled", "deleted", "expired", "disabled", "rejected"];
/// `reconcile.SIMULATED_GTT_PREFIX`: a simulated GTT id, as the gateway mints it.
pub const SIMULATED_GTT_PREFIX: &str = "DRY-";

/// `NEXT_ACTION[state]`.
#[must_use]
pub fn next_action(state: &str) -> &'static str {
    match state {
        ARMED => "hold; the sleeve manages the exit",
        NAKED => "arm the stop (Re-arm on the sleeve page)",
        GTT_MISSING => "the broker no longer lists the stop — re-arm it, or record why it went",
        GTT_OVERSIZED => "re-size the GTT down to the open quantity, or it sells what is not there",
        GTT_UNDERSIZED => {
            "re-size the GTT up to the open quantity; part of the position is unprotected"
        }
        TRIGGERED_UNFILLED => {
            "the stop fired and the shares are still here — check the LIMIT order it placed, sell by hand if it lapsed"
        }
        _ => "log in to Kite so the stop can be checked against the broker",
    }
}

// =============================================================================================
// The broker's GTT list (`reconcile.GttRow`, `reconcile.gtt_index`)
// =============================================================================================

/// `reconcile.GttRow`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GttRow {
    /// `gtt_id`.
    pub gtt_id: String,
    /// `status`, lower-cased.
    pub status: String,
    /// `symbol`.
    pub symbol: String,
    /// The sell leg's quantity.
    pub quantity: Option<i64>,
    /// The trigger, `Decimal(str(value))`.
    pub trigger: Option<Decimal>,
}

/// The GTT rows by id.
pub type GttIndex = HashMap<String, GttRow>;

fn py_get<'a>(m: &'a PyDict, k: &str) -> Option<&'a PyValue> {
    m.get(k).filter(|v| v.truthy())
}

fn py_int(v: &PyValue) -> i64 {
    match v {
        PyValue::Int(i) => *i,
        PyValue::Bool(b) => i64::from(*b),
        #[allow(clippy::cast_possible_truncation)]
        PyValue::Float(f) => f.trunc() as i64,
        PyValue::Str(s) => s.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

/// `Decimal(str(value))`.
fn py_dec(v: &PyValue) -> Option<Decimal> {
    match v {
        PyValue::Int(i) => Some(Decimal::from(*i)),
        PyValue::Float(f) => baskfy_core::py::decimal_from_float_str(*f).ok(),
        PyValue::Str(s) => py_decimal(s),
        _ => None,
    }
}

/// `reconcile.gtt_index(raw)`: Kite's GTT rows by id — status, symbol, the sell leg's quantity
/// and the trigger.
#[must_use]
pub fn gtt_index(raw: &[PyValue]) -> GttIndex {
    let mut out = GttIndex::new();
    let empty = PyDict::new();
    for trigger in raw {
        let PyValue::Map(t) = trigger else { continue };
        let Some(id) = t.get("id").filter(|v| **v != PyValue::None) else {
            continue;
        };
        let condition = match py_get(t, "condition") {
            Some(PyValue::Map(c)) => c,
            _ => &empty,
        };
        let values = match py_get(condition, "trigger_values") {
            Some(PyValue::List(v)) => v.as_slice(),
            _ => &[],
        };
        let legs = match py_get(t, "orders") {
            Some(PyValue::List(v)) => v.as_slice(),
            _ => &[],
        };
        let mut quantity = None;
        for leg in legs {
            let PyValue::Map(l) = leg else { continue };
            let side =
                py_get(l, "transaction_type").map_or_else(|| "SELL".to_owned(), PyValue::py_str);
            if side.to_uppercase() == "SELL" {
                quantity = Some(py_get(l, "quantity").map_or(0, py_int));
                break;
            }
        }
        let id = id.py_str();
        out.insert(
            id.clone(),
            GttRow {
                gtt_id: id,
                status: py_get(t, "status")
                    .map_or_else(String::new, PyValue::py_str)
                    .to_lowercase(),
                symbol: py_get(condition, "tradingsymbol")
                    .map_or_else(String::new, PyValue::py_str),
                quantity,
                trigger: values.first().and_then(py_dec),
            },
        );
    }
    out
}

// =============================================================================================
// One row per trade
// =============================================================================================

/// `TradeLifecycle`.
#[derive(Debug, Clone, PartialEq)]
pub struct TradeLifecycle {
    /// `sleeve`.
    pub sleeve: String,
    /// `position_id`.
    pub position_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `entry_date`.
    pub entry_date: Val,
    /// `entry_avg`.
    pub entry_avg: Decimal,
    /// `filled_qty`.
    pub filled_qty: i64,
    /// `open_qty`.
    pub open_qty: i64,
    /// `initial_stop`.
    pub initial_stop: Option<Decimal>,
    /// `stop`.
    pub stop: Option<Decimal>,
    /// `rupee_risk`.
    pub rupee_risk: Decimal,
    /// `stop_state`.
    pub stop_state: &'static str,
    /// `stop_qty`.
    pub stop_qty: Option<i64>,
    /// `gtt_id`.
    pub gtt_id: Option<String>,
    /// `exit_rule`.
    pub exit_rule: String,
    /// `next_action`.
    pub next_action: String,
    /// `overdue`.
    pub overdue: bool,
    /// `simulated`.
    pub simulated: bool,
    /// `issues`: `"<kind>: <detail>"` per open finding.
    pub issues: Vec<String>,
}

impl TradeLifecycle {
    /// `resolved`: armed and nothing open against it.
    #[must_use]
    pub fn resolved(&self) -> bool {
        self.stop_state == ARMED && self.issues.is_empty()
    }

    /// `r.__dict__`, in the dataclass's field order.
    #[must_use]
    pub fn as_rec(&self) -> Rec {
        Rec::of([
            ("sleeve", Val::Str(self.sleeve.clone())),
            ("position_id", Val::Int(self.position_id)),
            ("symbol", Val::Str(self.symbol.clone())),
            ("entry_date", self.entry_date.clone()),
            ("entry_avg", Val::Dec(self.entry_avg)),
            ("filled_qty", Val::Int(self.filled_qty)),
            ("open_qty", Val::Int(self.open_qty)),
            ("initial_stop", Val::opt(self.initial_stop, Val::Dec)),
            ("stop", Val::opt(self.stop, Val::Dec)),
            ("rupee_risk", Val::Dec(self.rupee_risk)),
            ("stop_state", Val::Str(self.stop_state.to_owned())),
            ("stop_qty", Val::opt(self.stop_qty, Val::Int)),
            ("gtt_id", Val::opt(self.gtt_id.clone(), Val::Str)),
            ("exit_rule", Val::Str(self.exit_rule.clone())),
            ("next_action", Val::Str(self.next_action.clone())),
            ("overdue", Val::Bool(self.overdue)),
            ("simulated", Val::Bool(self.simulated)),
            (
                "issues",
                Val::List(self.issues.iter().cloned().map(Val::Str).collect()),
            ),
        ])
    }
}

/// `_dec(value)`.
fn rec_dec(p: &Rec, key: &str) -> Option<Decimal> {
    p.get(key).filter(|v| !v.is_none()).and_then(|_| p.dec(key))
}

/// `_stop_of(position)`: TWT and VBT say `stop_price`; the swing book says `stop`.
#[must_use]
pub fn stop_of(p: &Rec) -> Option<Decimal> {
    if p.contains("stop_price") {
        rec_dec(p, "stop_price")
    } else {
        rec_dec(p, "stop")
    }
}

/// `judge_stop(position, gtts)`: the broker's word on one position's stop, and the quantity it
/// rests for.
#[must_use]
pub fn judge_stop(position: &Rec, gtts: Option<&GttIndex>) -> (&'static str, Option<i64>) {
    let open_qty = position.int("quantity_open");
    let Some(gtt_id) = position.opt_str("gtt_id") else {
        return (NAKED, None);
    };
    if gtt_id.starts_with(SIMULATED_GTT_PREFIX) {
        return (ARMED, Some(open_qty)); // a rehearsal's trigger: nothing rests, nothing is missing
    }
    let Some(gtts) = gtts else {
        return (UNVERIFIED, None);
    };
    let Some(row) = gtts.get(&gtt_id) else {
        return (GTT_MISSING, None);
    };
    if GTT_GONE_STATUSES.contains(&row.status.as_str()) {
        return (GTT_MISSING, None);
    }
    if row.status == GTT_TRIGGERED {
        return (TRIGGERED_UNFILLED, row.quantity);
    }
    match row.quantity {
        Some(q) if q > open_qty => (GTT_OVERSIZED, Some(q)),
        Some(q) if q < open_qty => (GTT_UNDERSIZED, Some(q)),
        Some(q) => (ARMED, Some(q)),
        None => (ARMED, Some(open_qty)),
    }
}

/// `build_rows(positions, gtts=, issues=, now=)`: every open position across the sleeves as one
/// row, unresolved ones first. `positions` is `(sleeve, rows)` in any order; the sleeves are
/// walked in [`SLEEVES`] order.
#[must_use]
pub fn build_rows(
    positions: &[(&str, Vec<Rec>)],
    gtts: Option<&GttIndex>,
    issues: &[Rec],
    now: DateTime<FixedOffset>,
) -> Vec<TradeLifecycle> {
    let rules = exit_rules();
    let mut rows = Vec::new();
    for sleeve in SLEEVES {
        let rule = rules
            .iter()
            .find(|(k, _)| *k == sleeve)
            .map(|(_, v)| v.clone())
            .unwrap_or_default();
        let Some((_, list)) = positions.iter().find(|(s, _)| *s == sleeve) else {
            continue;
        };
        for p in list {
            let open_qty = p.int("quantity_open");
            if open_qty <= 0 {
                continue;
            }
            // `_dec(p.get("entry_avg")) or _ZERO`: a zero entry is the integer zero.
            let entry = rec_dec(p, "entry_avg")
                .filter(|d| !d.is_zero())
                .unwrap_or(Decimal::ZERO);
            let stop = stop_of(p);
            let initial = rec_dec(p, "initial_stop");
            let risk = match stop {
                Some(s) if s < entry => (entry - s) * Decimal::from(open_qty),
                _ => Decimal::ZERO,
            };
            let (state, stop_qty) = judge_stop(p, gtts);
            let pid = p.int("id");
            let mine: Vec<&Rec> = issues
                .iter()
                .filter(|i| i.s("sleeve") == sleeve && i.int("position_id") == pid)
                .collect();
            let oldest = mine.iter().filter_map(|i| i.stamp("seen_at")).min();
            let overdue = state != ARMED
                && oldest.is_some_and(|o| now - o >= Duration::minutes(OVERDUE_AFTER_MINUTES));
            let mut action = next_action(state).to_owned();
            if !mine.is_empty() && state == ARMED {
                action = "resolve the reconciler's finding(s) — see the issue column".to_owned();
            }
            let symbol = match p.get("symbol") {
                Some(v) if v.truthy() => v.py_str(),
                _ => format!("instrument:{}", p.val("instrument_id").py_str()),
            };
            let entered = p.int("quantity_entered");
            rows.push(TradeLifecycle {
                sleeve: sleeve.to_owned(),
                position_id: pid,
                symbol,
                entry_date: p.val("entry_date"),
                entry_avg: entry,
                filled_qty: if entered == 0 { open_qty } else { entered },
                open_qty,
                initial_stop: initial,
                stop,
                rupee_risk: quantize(risk, -2),
                stop_state: state,
                stop_qty,
                gtt_id: p.opt_str("gtt_id"),
                exit_rule: rule.clone(),
                next_action: action,
                overdue,
                simulated: p.truthy("simulated"),
                issues: mine
                    .iter()
                    .map(|i| format!("{}: {}", i.s("kind"), i.s("detail")))
                    .collect(),
            });
        }
    }
    rows.sort_by(|a, b| {
        (a.resolved(), !a.overdue, &a.sleeve, &a.symbol).cmp(&(
            b.resolved(),
            !b.overdue,
            &b.sleeve,
            &b.symbol,
        ))
    });
    rows
}

// =============================================================================================
// Adoption
// =============================================================================================

/// `Adoption`: the person's numbers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Adoption {
    /// `sleeve`.
    pub sleeve: String,
    /// `symbol`.
    pub symbol: String,
    /// `quantity`.
    pub quantity: i64,
    /// `avg_cost`.
    pub avg_cost: Decimal,
    /// `gtt_id` (a GTT armed by hand, to link).
    pub gtt_id: Option<String>,
    /// `setup` (swing: `FLAG` or `EP`).
    pub setup: Option<String>,
    /// `note`.
    pub note: Option<String>,
}

/// What an adoption needs of a sleeve's store: Python's `store.conn` reads, the store's own
/// writes, and `adj_factor` where the store has one.
#[async_trait]
pub trait AdoptBook: Send {
    /// `_instrument_id(store, symbol)`.
    async fn instrument_id(&mut self, symbol: &str) -> Result<Option<i64>, String>;
    /// `store.open_position_for(instrument_id) is not None`.
    async fn holds(&mut self, instrument_id: i64) -> Result<bool, String>;
    /// `store.adj_factor(...) if hasattr(store, "adj_factor") else Decimal(1)`.
    async fn adj_factor(&mut self, instrument_id: i64, day: NaiveDate) -> Result<Decimal, String>;
    /// `store.create_position(fields)`.
    async fn create_position(&mut self, fields: Rec) -> Result<i64, String>;
    /// `store.add_fill(fields)`.
    async fn add_fill(&mut self, fields: Rec) -> Result<(), String>;
    /// `store.update_position(position_id, fields)`.
    async fn update_position(&mut self, position_id: i64, fields: Rec) -> Result<(), String>;
    /// `_record_adoption(store, ...)`.
    async fn record_adoption(
        &mut self,
        position_id: i64,
        adoption: &Adoption,
        gtt_id: Option<&str>,
    ) -> Result<(), String>;
}

/// Why an adoption did not complete.
#[derive(Debug)]
pub enum AdoptError {
    /// `HTTPException(status, detail)`.
    Http(HttpError),
    /// A store failure (a 500 at the route).
    Store(String),
    /// The gateway raised (a 500 at the route).
    Gateway(GatewayError),
}

fn http(status: u16, detail: impl Into<String>) -> AdoptError {
    AdoptError::Http(HttpError::new(status, detail))
}

fn f64_dec(v: f64) -> Decimal {
    Decimal::from_str(&baskfy_core::py::float_repr(v)).unwrap_or(Decimal::ZERO)
}

/// `_stop_pct(sleeve)`: `Decimal(str(...))` of the sleeve's own stop distance.
#[must_use]
pub fn stop_pct(sleeve: &str) -> Decimal {
    match sleeve {
        "twt" => {
            baskfy_core::twt::config::TwtConfig::default()
                .exits
                .stop_pct
        }
        "vbt" => f64_dec(
            baskfy_core::vbt::config::VbtConfig::default()
                .exits
                .stop_pct,
        ),
        _ => f64_dec(
            baskfy_core::swing::setups::SwingConfig::default()
                .stops
                .max_stop_distance_pct,
        ),
    }
}

/// `_position_fields(sleeve, adoption, ...)`.
fn position_fields(
    sleeve: &str,
    a: &Adoption,
    instrument_id: i64,
    stop: Decimal,
    day: NaiveDate,
    adj: Decimal,
) -> Rec {
    let mut r = Rec::of([
        ("instrument_id", Val::Int(instrument_id)),
        ("entry_date", Val::Date(day)),
        ("entry_avg", Val::Dec(a.avg_cost)),
        ("quantity_entered", Val::Int(a.quantity)),
        ("quantity_open", Val::Int(a.quantity)),
        ("initial_stop", Val::Dec(stop)),
        ("state", Val::Str("OPEN".to_owned())),
        ("simulated", Val::Bool(false)),
    ]);
    match sleeve {
        "twt" => {
            r.set("signal_date", Val::Date(day));
            r.set("entry_adj_factor", Val::Dec(adj));
            r.set("stop_price", Val::Dec(stop));
            r.set("high_since", Val::Dec(a.avg_cost));
            r.set("high_since_date", Val::Date(day));
        }
        "vbt" => r.set("stop_price", Val::Dec(stop)),
        _ => {
            r.set("symbol", Val::Str(a.symbol.clone()));
            r.set(
                "setup",
                Val::Str(a.setup.clone().unwrap_or_else(|| "FLAG".to_owned())),
            );
            r.set("stop", Val::Dec(stop));
            r.set("trail", Val::Str("MA20".to_owned()));
            r.set("partial_done", Val::Bool(false));
            r.set("half_risk", Val::Bool(false));
        }
    }
    r
}

fn dfloat(d: Decimal) -> f64 {
    d.to_string().parse().unwrap_or(0.0)
}

/// The sleeves' arm helpers, one signature (`ARM[sleeve]`): `twt_execute._arm`,
/// `gateway.place_gtt_stop` for VBT, `swing_execute._arm` — each through the sleeve's gateway.
#[allow(clippy::too_many_arguments)]
async fn arm(
    sleeve: &str,
    gateway: &dyn Gateway,
    settings: &Settings,
    symbol: &str,
    qty: i64,
    stop: Decimal,
    last_price: Decimal,
    position_id: i64,
) -> Result<PyDict, GatewayError> {
    let tenant =
        TenantIds::new(settings.sole_user_id, settings.sole_broker_account_id).map_err(|e| {
            GatewayError {
                python_type: "ValueError".to_owned(),
                message: e.0,
            }
        })?;
    let (client_id, limit_fraction) = match sleeve {
        "twt" => (
            Some(format!("adopt:{position_id}:{symbol}:ARM_GTT")),
            crate::lanes::twt::execute::twt_limit_fraction(),
        ),
        "vbt" => (
            None,
            baskfy_core::vbt::config::VbtConfig::default()
                .exits
                .gtt_limit_fraction,
        ),
        _ => (
            Some(format!("adopt:{position_id}:{symbol}:GTT")),
            settings.swing_gtt_limit_fraction,
        ),
    };
    let mut result = gateway
        .place_gtt_stop(GttStop {
            symbol: symbol.to_owned(),
            qty,
            trigger: dfloat(stop),
            last_price: dfloat(last_price),
            exchange: "NSE".to_owned(),
            series: None,
            client_id: client_id.clone(),
            tenant: Some(tenant),
            plan_tenant: Some(tenant),
            limit_fraction: Some(limit_fraction),
            fo_plan: None,
        })
        .await?;
    if let Some(cid) = client_id
        && result.get("client_id").is_none()
    {
        result.insert("client_id", PyValue::Str(cid));
    }
    Ok(result)
}

/// `adopt(store, gateway, adoption, confirm=, now=, gtts=, arm=)`: a hand-bought holding
/// becomes a position in `adoption.sleeve`'s book, protected. With a `gtt_id` the resting
/// trigger is linked as it is; without one the sleeve's helper arms one through the gateway.
///
/// # Errors
/// The refusals as [`AdoptError::Http`]; a store or gateway failure.
#[allow(clippy::too_many_arguments)]
pub async fn adopt(
    book: &mut dyn AdoptBook,
    gateway: &dyn Gateway,
    settings: &Settings,
    adoption: &Adoption,
    confirm: &str,
    now: DateTime<FixedOffset>,
    gtts: Option<&GttIndex>,
) -> Result<Rec, AdoptError> {
    if confirm.to_lowercase() != "true" {
        return Err(http(
            400,
            "Adopting a holding requires explicit confirmation.",
        ));
    }
    let sleeve = adoption.sleeve.as_str();
    if !SLEEVES.contains(&sleeve) {
        return Err(http(
            400,
            format!("unknown sleeve {}", baskfy_providers::py::repr_str(sleeve)),
        ));
    }
    if adoption.quantity <= 0 || adoption.avg_cost <= Decimal::ZERO {
        return Err(http(400, "quantity and average cost must be positive"));
    }
    if sleeve == "swing" && !matches!(adoption.setup.as_deref(), Some("FLAG" | "EP")) {
        return Err(http(400, "a swing adoption names its setup: FLAG or EP"));
    }
    let Some(instrument_id) = book
        .instrument_id(&adoption.symbol)
        .await
        .map_err(AdoptError::Store)?
    else {
        return Err(http(
            404,
            format!("{} is not an instrument this desk knows", adoption.symbol),
        ));
    };
    if book.holds(instrument_id).await.map_err(AdoptError::Store)? {
        return Err(http(
            409,
            format!("the {sleeve} book already holds {}", adoption.symbol),
        ));
    }
    let day = now.with_timezone(&ist()).date_naive();
    let pct = stop_pct(sleeve);
    let hundred = Decimal::from(100);
    let mut stop = quantize(adoption.avg_cost * (hundred - pct) / hundred, -2);
    let mut linked: Option<String> = None;
    if let Some(g) = adoption.gtt_id.as_deref().filter(|g| !g.is_empty()) {
        if let Some(trigger) = gtts.and_then(|m| m.get(g)).and_then(|r| r.trigger) {
            stop = trigger;
        }
        linked = Some(g.to_owned());
    }
    let adj = book
        .adj_factor(instrument_id, day)
        .await
        .map_err(AdoptError::Store)?;
    let fields = position_fields(sleeve, adoption, instrument_id, stop, day, adj);
    let position_id = book
        .create_position(fields)
        .await
        .map_err(AdoptError::Store)?;
    let mut fill = Rec::of([("position_id", Val::Int(position_id))]);
    if sleeve != "swing" {
        fill.set("order_id", Val::None);
    }
    fill.set("side", Val::Str("BUY".to_owned()));
    fill.set("quantity", Val::Int(adoption.quantity));
    fill.set("price", Val::Dec(adoption.avg_cost));
    fill.set("filled_at", Val::Stamp(now));
    fill.set("simulated", Val::Bool(false));
    if sleeve == "swing" {
        fill.set("journal_ref", Val::Str(format!("adopted:{position_id}")));
    }
    book.add_fill(fill).await.map_err(AdoptError::Store)?;
    let mut outcome = Rec::of([
        ("position_id", Val::Int(position_id)),
        ("sleeve", Val::Str(sleeve.to_owned())),
        ("stop", Val::Str(dstr(stop))),
    ]);
    let armed_fields = |gtt_id: &str| {
        Rec::of([
            ("gtt_id", Val::Str(gtt_id.to_owned())),
            ("gtt_trigger", Val::Dec(stop)),
            ("gtt_armed_at", Val::Stamp(now)),
        ])
    };
    if let Some(g) = linked.clone() {
        book.update_position(position_id, armed_fields(&g))
            .await
            .map_err(AdoptError::Store)?;
        outcome.set("gtt", Val::Map(Rec::of([("linked", Val::Str(g))])));
    } else {
        let gtt = arm(
            sleeve,
            gateway,
            settings,
            &adoption.symbol,
            adoption.quantity,
            stop,
            adoption.avg_cost,
            position_id,
        )
        .await
        .map_err(AdoptError::Gateway)?;
        let placed = gtt
            .get("status")
            .filter(|v| v.truthy())
            .map_or_else(String::new, PyValue::py_str);
        outcome.set("gtt", Val::Py(PyValue::Map(gtt.clone())));
        if placed.starts_with("GTT_PLACED") || placed.starts_with("DRY_RUN") {
            let gtt_id = gtt.get("gtt_id").filter(|v| v.truthy()).map_or_else(
                || format!("{SIMULATED_GTT_PREFIX}adopt:{position_id}"),
                PyValue::py_str,
            );
            book.update_position(position_id, armed_fields(&gtt_id))
                .await
                .map_err(AdoptError::Store)?;
            linked = Some(gtt_id);
        } else {
            outcome.set("naked", Val::Bool(true));
        }
    }
    book.record_adoption(position_id, adoption, linked.as_deref())
        .await
        .map_err(AdoptError::Store)?;
    Ok(outcome)
}

// =============================================================================================
// The three books, as adoption needs them
// =============================================================================================

async fn instrument_by_symbol(
    conn: &mut Conn,
    table: &str,
    symbol: &str,
) -> Result<Option<i64>, String> {
    let cursor = conn
        .execute(
            &format!("SELECT id FROM {table} WHERE symbol = ? LIMIT 1"),
            &[Param::Text(symbol.to_owned())],
        )
        .await
        .map_err(|e| e.to_string())?;
    Ok(cursor.fetchone().map(|r| db_int(r.get("id"))))
}

async fn insert_adoption(
    conn: &mut Conn,
    table: &str,
    user_id: i64,
    position_id: i64,
    a: &Adoption,
    gtt_id: Option<&str>,
) -> Result<(), String> {
    let opt = |v: Option<&str>| v.map_or(Param::Null, |s| Param::Text(s.to_owned()));
    conn.execute(
        &format!(
            "INSERT INTO {table} (user_id, sleeve, position_id, symbol, quantity, avg_cost, \
             gtt_id, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        ),
        &[
            Param::Int(user_id),
            Param::Text(a.sleeve.clone()),
            Param::Int(position_id),
            Param::Text(a.symbol.clone()),
            Param::Int(a.quantity),
            Param::Text(dstr(a.avg_cost)),
            opt(gtt_id),
            opt(a.note.as_deref()),
        ],
    )
    .await
    .map(|_| ())
    .map_err(|e| e.to_string())
}

/// The TWT book.
pub struct TwtBook(pub crate::lanes::twt::store::PgTwtStore);

#[async_trait]
impl AdoptBook for TwtBook {
    async fn instrument_id(&mut self, symbol: &str) -> Result<Option<i64>, String> {
        let table = self.0.t("instrument");
        instrument_by_symbol(&mut self.0.conn, &table, symbol).await
    }
    async fn holds(&mut self, instrument_id: i64) -> Result<bool, String> {
        use crate::lanes::twt::store::TwtStore as _;
        self.0
            .open_position_for(instrument_id)
            .await
            .map(|p| p.is_some())
            .map_err(|e| e.to_string())
    }
    async fn adj_factor(&mut self, instrument_id: i64, day: NaiveDate) -> Result<Decimal, String> {
        use crate::lanes::twt::store::TwtStore as _;
        self.0
            .adj_factor(instrument_id, day)
            .await
            .map_err(|e| e.to_string())
    }
    async fn create_position(&mut self, fields: Rec) -> Result<i64, String> {
        use crate::lanes::twt::store::TwtStore as _;
        self.0
            .create_position(fields)
            .await
            .map_err(|e| e.to_string())
    }
    async fn add_fill(&mut self, fields: Rec) -> Result<(), String> {
        use crate::lanes::twt::store::TwtStore as _;
        self.0
            .add_fill(fields)
            .await
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    async fn update_position(&mut self, position_id: i64, fields: Rec) -> Result<(), String> {
        use crate::lanes::twt::store::TwtStore as _;
        self.0
            .update_position(position_id, fields)
            .await
            .map_err(|e| e.to_string())
    }
    async fn record_adoption(
        &mut self,
        position_id: i64,
        adoption: &Adoption,
        gtt_id: Option<&str>,
    ) -> Result<(), String> {
        let table = self.0.t("lv_adoption");
        let user = self.0.user_id;
        insert_adoption(
            &mut self.0.conn,
            &table,
            user,
            position_id,
            adoption,
            gtt_id,
        )
        .await
    }
}

/// The VBT book (no `adj_factor`: `Decimal(1)`).
pub struct VbtBook(pub crate::lanes::vbt::store::PgVbtStore);

#[async_trait]
impl AdoptBook for VbtBook {
    async fn instrument_id(&mut self, symbol: &str) -> Result<Option<i64>, String> {
        let table = self.0.t("instrument");
        instrument_by_symbol(&mut self.0.conn, &table, symbol).await
    }
    async fn holds(&mut self, instrument_id: i64) -> Result<bool, String> {
        use crate::lanes::vbt::store::VbtStore as _;
        self.0
            .open_position_for(instrument_id)
            .await
            .map(|p| p.is_some())
            .map_err(|e| e.to_string())
    }
    async fn adj_factor(
        &mut self,
        _instrument_id: i64,
        _day: NaiveDate,
    ) -> Result<Decimal, String> {
        Ok(Decimal::ONE)
    }
    async fn create_position(&mut self, fields: Rec) -> Result<i64, String> {
        use crate::lanes::vbt::store::VbtStore as _;
        self.0
            .create_position(fields)
            .await
            .map_err(|e| e.to_string())
    }
    async fn add_fill(&mut self, fields: Rec) -> Result<(), String> {
        use crate::lanes::vbt::store::VbtStore as _;
        self.0
            .add_fill(fields)
            .await
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    async fn update_position(&mut self, position_id: i64, fields: Rec) -> Result<(), String> {
        use crate::lanes::vbt::store::VbtStore as _;
        self.0
            .update_position(position_id, fields)
            .await
            .map_err(|e| e.to_string())
    }
    async fn record_adoption(
        &mut self,
        position_id: i64,
        adoption: &Adoption,
        gtt_id: Option<&str>,
    ) -> Result<(), String> {
        let table = self.0.t("lv_adoption");
        let user = self.0.user_id;
        insert_adoption(
            &mut self.0.conn,
            &table,
            user,
            position_id,
            adoption,
            gtt_id,
        )
        .await
    }
}

/// The swing book (no `adj_factor`: `Decimal(1)`).
pub struct SwingBook(pub crate::lanes::swing::store::PgSwingStore);

/// The swing store's columns, as `'static` names (its `Fields` are keyed by them).
const SWING_KEYS: [&str; 28] = [
    "broker_account_id",
    "instrument_id",
    "symbol",
    "setup",
    "entry_date",
    "entry_avg",
    "quantity_entered",
    "quantity_open",
    "initial_stop",
    "stop",
    "gtt_id",
    "gtt_trigger",
    "gtt_armed_at",
    "trail",
    "partial_done",
    "state",
    "simulated",
    "half_risk",
    "position_id",
    "side",
    "quantity",
    "price",
    "filled_at",
    "journal_ref",
    "order_id",
    "partial_date",
    "closed_on",
    "exit_avg",
];

fn swing_fields(rec: &Rec) -> Result<crate::lanes::swing::store::Fields, String> {
    use crate::lanes::swing::values::Val as S;
    let mut out = Vec::with_capacity(rec.0.len());
    for (k, v) in &rec.0 {
        let Some(key) = SWING_KEYS.iter().find(|c| **c == k.as_str()) else {
            return Err(format!("ValueError: sw_position has no column ['{k}']"));
        };
        let value = match v {
            Val::None => S::Null,
            Val::Bool(b) => S::Bool(*b),
            Val::Int(i) => S::Int(*i),
            Val::Dec(d) => S::Dec(*d),
            Val::Str(s) => S::Text(s.clone()),
            Val::Date(d) => S::Date(*d),
            Val::Stamp(t) => S::Stamp(*t),
            other => S::Text(other.py_str()),
        };
        out.push((*key, value));
    }
    Ok(out)
}

#[async_trait]
impl AdoptBook for SwingBook {
    async fn instrument_id(&mut self, symbol: &str) -> Result<Option<i64>, String> {
        let table = self.0.t("instrument");
        instrument_by_symbol(&mut self.0.conn, &table, symbol).await
    }
    async fn holds(&mut self, instrument_id: i64) -> Result<bool, String> {
        use crate::lanes::swing::store::SwingStore as _;
        self.0
            .open_position_for(instrument_id)
            .await
            .map(|p| p.is_some())
            .map_err(|e| e.to_string())
    }
    async fn adj_factor(
        &mut self,
        _instrument_id: i64,
        _day: NaiveDate,
    ) -> Result<Decimal, String> {
        Ok(Decimal::ONE)
    }
    async fn create_position(&mut self, fields: Rec) -> Result<i64, String> {
        use crate::lanes::swing::store::SwingStore as _;
        let f = swing_fields(&fields)?;
        self.0.create_position(f).await.map_err(|e| e.to_string())
    }
    async fn add_fill(&mut self, fields: Rec) -> Result<(), String> {
        use crate::lanes::swing::store::SwingStore as _;
        let f = swing_fields(&fields)?;
        self.0
            .add_fill(f)
            .await
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    async fn update_position(&mut self, position_id: i64, fields: Rec) -> Result<(), String> {
        use crate::lanes::swing::store::SwingStore as _;
        let f = swing_fields(&fields)?;
        self.0
            .update_position(position_id, f)
            .await
            .map_err(|e| e.to_string())
    }
    async fn record_adoption(
        &mut self,
        position_id: i64,
        adoption: &Adoption,
        gtt_id: Option<&str>,
    ) -> Result<(), String> {
        let table = self.0.t("lv_adoption");
        let user = self.0.user_id;
        insert_adoption(
            &mut self.0.conn,
            &table,
            user,
            position_id,
            adoption,
            gtt_id,
        )
        .await
    }
}

/// The swing book's position as Python's `_position_row` dict (the keys the page reads).
fn swing_rec(p: &crate::lanes::swing::store::PositionRow) -> Rec {
    Rec::of([
        ("id", Val::Int(p.id)),
        ("instrument_id", Val::Int(p.instrument_id)),
        ("symbol", Val::Str(p.symbol.clone())),
        ("setup", Val::Str(p.setup.clone())),
        ("entry_date", Val::opt(p.entry_date, Val::Date)),
        ("entry_avg", Val::opt(p.entry_avg, Val::Dec)),
        ("quantity_entered", Val::Int(p.quantity_entered)),
        ("quantity_open", Val::Int(p.quantity_open)),
        ("initial_stop", Val::opt(p.initial_stop, Val::Dec)),
        ("stop", Val::opt(p.stop, Val::Dec)),
        ("gtt_id", Val::opt(p.gtt_id.clone(), Val::Str)),
        ("simulated", Val::Bool(p.simulated)),
    ])
}

// =============================================================================================
// The desk's wiring
// =============================================================================================

/// `str(exc)` of a driver error, as the page prints it: the database's own message (sqlite3's
/// `no such table: ...`), not the driver's wrapper.
fn storage_text(e: &StorageError) -> String {
    match e {
        StorageError::Driver(sqlx::Error::Database(db)) => db.message().to_owned(),
        other => other.to_string(),
    }
}

fn twt_text(e: &crate::lanes::twt::store::StoreError) -> String {
    match e {
        crate::lanes::twt::store::StoreError::Storage(s) => storage_text(s),
        other => other.to_string(),
    }
}

fn vbt_text(e: &crate::lanes::vbt::store::StoreError) -> String {
    match e {
        crate::lanes::vbt::store::StoreError::Storage(s) => storage_text(s),
        other => other.to_string(),
    }
}

fn swing_text(e: &crate::lanes::swing::store::StoreError) -> String {
    match e {
        crate::lanes::swing::store::StoreError::Storage(s) => storage_text(s),
        other => other.to_string(),
    }
}

/// `PgIssueStore(conn, user_id, schema).open_issues()`.
async fn open_issues(conn: &mut Conn, schema: &str, user_id: i64) -> Result<Vec<Rec>, String> {
    let table = if schema.is_empty() {
        "lv_protection_issue".to_owned()
    } else {
        format!("{schema}.lv_protection_issue")
    };
    let cursor = conn
        .execute(
            &format!(
                "SELECT id, sleeve, position_id, symbol, kind, detail, seen_at FROM {table} \
                 WHERE user_id = ? AND resolved_at IS NULL ORDER BY seen_at, id"
            ),
            &[Param::Int(user_id)],
        )
        .await
        .map_err(|e| storage_text(&e))?;
    let text = |v: Option<&DbValue>| v.map_or_else(|| "None".to_owned(), db_text);
    let mut out = Vec::new();
    for r in &cursor.rows {
        let seen = db_stamp(r.get("seen_at")).map_err(|e| storage_text(&e))?;
        out.push(Rec::of([
            ("id", Val::Int(db_int(r.get("id")))),
            ("sleeve", Val::Str(text(r.get("sleeve")))),
            ("position_id", Val::Int(db_int(r.get("position_id")))),
            ("symbol", Val::Str(text(r.get("symbol")))),
            ("kind", Val::Str(text(r.get("kind")))),
            ("detail", Val::Str(text(r.get("detail")))),
            ("seen_at", Val::opt(seen, Val::Stamp)),
        ]));
    }
    Ok(out)
}

/// `_main.kite().get_gtts()` → `gtt_index`, or `None` (no key, no session): the stops are then
/// UNVERIFIED, and the page says so.
async fn broker_gtts(state: &AppState) -> Option<GttIndex> {
    let desk = state
        .gateway
        .as_any()?
        .downcast_ref::<super::desk::DeskGateway>()?;
    let kite = desk.kite();
    kite.ensure_configured().ok()?;
    kite.get_gtts().await.ok().map(|raw| gtt_index(&raw))
}

/// The three sleeves' stores (`desk_sources()`'s `stores`).
struct Stores {
    swing: crate::lanes::swing::store::PgSwingStore,
    twt: crate::lanes::twt::store::PgTwtStore,
    vbt: crate::lanes::vbt::store::PgVbtStore,
}

async fn open_stores(state: &AppState) -> Result<(Stores, Conn), String> {
    let swing = crate::lanes::swing::desk::open_store(state)
        .await
        .map_err(|e| storage_text(&e))?;
    let twt = crate::lanes::twt::open_store(state)
        .await
        .map_err(|e| storage_text(&e))?;
    let vbt = crate::lanes::vbt::open_store(state)
        .await
        .map_err(|e| storage_text(&e))?;
    let conn = state.db.connect().await.map_err(|e| storage_text(&e))?;
    Ok((Stores { swing, twt, vbt }, conn))
}

fn schema(state: &AppState) -> &'static str {
    if state.db.config().is_postgres() {
        "public"
    } else {
        ""
    }
}

fn rules_rec() -> Rec {
    Rec::of(exit_rules().into_iter().map(|(k, v)| (k, Val::Str(v))))
}

/// `build_view()`'s answer: the rows and the rest, or the error text.
#[derive(Debug, Clone)]
pub struct View {
    /// `rows`.
    pub rows: Vec<TradeLifecycle>,
    /// `gtt_checked`: the broker's GTT list was read.
    pub gtt_checked: bool,
    /// `issues`: the reconciler's open findings.
    pub issues: Vec<Rec>,
    /// `now`.
    pub now: DateTime<FixedOffset>,
    /// `error`: the page could not read the books.
    pub error: Option<String>,
}

async fn read_view(state: &AppState, now: DateTime<FixedOffset>) -> Result<View, String> {
    use crate::lanes::swing::store::SwingStore as _;
    use crate::lanes::twt::store::TwtStore as _;
    use crate::lanes::vbt::store::VbtStore as _;
    let (mut stores, mut conn) = open_stores(state).await?;
    let gtts = broker_gtts(state).await;
    let user = state.settings_snapshot().sole_user_id;
    let issues = open_issues(&mut conn, schema(state), user).await?;
    let swing: Vec<Rec> = stores
        .swing
        .open_positions()
        .await
        .map_err(|e| swing_text(&e))?
        .iter()
        .map(swing_rec)
        .collect();
    let twt = stores
        .twt
        .open_positions()
        .await
        .map_err(|e| twt_text(&e))?;
    let vbt = stores
        .vbt
        .open_positions()
        .await
        .map_err(|e| vbt_text(&e))?;
    let positions = [("swing", swing), ("twt", twt), ("vbt", vbt)];
    let rows = build_rows(&positions, gtts.as_ref(), &issues, now);
    Ok(View {
        rows,
        gtt_checked: gtts.is_some(),
        issues,
        now,
        error: None,
    })
}

/// `build_view()`: the page reports a failure, it does not 500.
async fn build_view(state: &AppState) -> View {
    let now = state.clock.now_ist();
    match read_view(state, now).await {
        Ok(v) => v,
        Err(e) => View {
            rows: Vec::new(),
            gtt_checked: false,
            issues: Vec::new(),
            now,
            error: Some(e),
        },
    }
}

/// The view as the page's `v` (`for_page`: each row also carries `resolved`, the property the
/// template reads) or as `/lifecycle/data`'s body (`r.__dict__`: fields only).
#[must_use]
pub fn view_rec(v: &View, for_page: bool) -> Rec {
    let rows = v
        .rows
        .iter()
        .map(|r| {
            let mut rec = r.as_rec();
            if for_page {
                rec.set("resolved", Val::Bool(r.resolved()));
            }
            Val::Map(rec)
        })
        .collect();
    let unresolved = v.rows.iter().filter(|r| !r.resolved()).count();
    Rec::of([
        ("rows", Val::List(rows)),
        (
            "unresolved",
            Val::Int(i64::try_from(unresolved).unwrap_or(i64::MAX)),
        ),
        ("gtt_checked", Val::Bool(v.gtt_checked)),
        (
            "issues",
            Val::List(v.issues.iter().cloned().map(Val::Map).collect()),
        ),
        ("rules", Val::Map(rules_rec())),
        ("now", Val::Stamp(v.now)),
        ("error", Val::opt(v.error.clone(), Val::Str)),
    ])
}

async fn lifecycle_page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let v = build_view(&state).await;
    crate::lanes::twt::render_page(&state, &uri, "lifecycle.html", &view_rec(&v, true))
}

async fn lifecycle_data(State(state): State<AppState>) -> Response<Body> {
    let v = build_view(&state).await;
    json_response(200, &PyValue::Map(jsonable_rec(&view_rec(&v, false))))
}

// --- the form ---------------------------------------------------------------------------------

struct Form {
    pairs: Vec<(String, String)>,
    errors: Vec<PyValue>,
}

impl Form {
    fn raw(&self, name: &str) -> Option<String> {
        self.pairs
            .iter()
            .rev()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.clone())
            .filter(|v| !v.is_empty())
    }

    fn error(&mut self, kind: &str, name: &str, msg: &str, input: PyValue) {
        let mut e = PyDict::new();
        e.insert("type", PyValue::Str(kind.to_owned()));
        e.insert(
            "loc",
            PyValue::List(vec![
                PyValue::Str("body".to_owned()),
                PyValue::Str(name.to_owned()),
            ]),
        );
        e.insert("msg", PyValue::Str(msg.to_owned()));
        e.insert("input", input);
        self.errors.push(PyValue::Map(e));
    }

    fn required(&mut self, name: &str) -> Option<String> {
        let v = self.raw(name);
        if v.is_none() {
            self.error("missing", name, "Field required", PyValue::None);
        }
        v
    }

    fn optional(&self, name: &str) -> String {
        self.raw(name).unwrap_or_default()
    }

    fn int(&mut self, name: &str) -> i64 {
        let Some(v) = self.required(name) else {
            return 0;
        };
        crate::lanes::twt::common::lax_int(&v).unwrap_or_else(|| {
            self.error(
                "int_parsing",
                name,
                "Input should be a valid integer, unable to parse string as an integer",
                PyValue::Str(v.clone()),
            );
            0
        })
    }

    fn decimal(&mut self, name: &str) -> Decimal {
        let Some(v) = self.required(name) else {
            return Decimal::ZERO;
        };
        py_decimal(v.trim()).unwrap_or_else(|| {
            self.error(
                "decimal_parsing",
                name,
                "Input should be a valid decimal",
                PyValue::Str(v.clone()),
            );
            Decimal::ZERO
        })
    }
}

/// `_main.gateway()` then the sleeve's own gateway. `None` is Python's 500: the Kite client
/// cannot be built without `KITE_API_KEY` (after the day's `risk_ledger` row is written, as
/// `_main.gateway()` writes it before `Kite()` refuses), or the sleeve's band is out of range.
async fn sleeve_gateway(state: &AppState, sleeve: &str) -> Option<Arc<dyn Gateway>> {
    if sleeve == "swing" {
        return crate::lanes::swing::desk::swing_gateway(state).await;
    }
    if state.settings_snapshot().kite_api_key.is_empty() {
        if state
            .gateway
            .as_any()
            .and_then(|a| a.downcast_ref::<super::desk::DeskGateway>())
            .is_some()
        {
            let fresh = super::desk::SharedRisk::new(
                state.settings.clone(),
                state.clock.clone(),
                state.db.clone(),
            );
            let _ = fresh.get().await;
        }
        return None;
    }
    if sleeve == "twt" {
        Some(crate::lanes::twt::twt_gateway_ready(state).await)
    } else {
        crate::lanes::vbt::vbt_gateway_ready(state).await
    }
}

fn adopt_failed(e: &AdoptError) -> Response<Body> {
    match e {
        AdoptError::Http(h) => h.response(),
        AdoptError::Store(_) | AdoptError::Gateway(_) => server_error(),
    }
}

async fn lifecycle_adopt(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = Form {
        pairs: parse_form(&body),
        errors: Vec::new(),
    };
    let sleeve = form.required("sleeve").unwrap_or_default();
    let symbol = form.required("symbol").unwrap_or_default();
    let quantity = form.int("quantity");
    let avg_cost = form.decimal("avg_cost");
    if !form.errors.is_empty() {
        let mut body = PyDict::new();
        body.insert("detail", PyValue::List(form.errors));
        return json_response(422, &PyValue::Map(body));
    }
    let confirm = form.optional("confirm");
    let gtt_id = form.optional("gtt_id");
    let setup = form.optional("setup");
    let note = form.optional("note");
    let some = |s: &str| {
        let t = s.trim();
        (!t.is_empty()).then(|| t.to_owned())
    };
    let adoption = Adoption {
        sleeve: sleeve.trim().to_lowercase(),
        symbol: symbol.trim().to_uppercase(),
        quantity,
        avg_cost,
        gtt_id: some(&gtt_id),
        setup: some(&setup).map(|s| s.to_uppercase()),
        note: some(&note),
    };
    let Ok((stores, mut conn)) = open_stores(&state).await else {
        return server_error();
    };
    let gtts = broker_gtts(&state).await;
    // `PgIssueStore(...).open_issues()`: `desk_sources()` reads it whatever the route does.
    let user = state.settings_snapshot().sole_user_id;
    if open_issues(&mut conn, schema(&state), user).await.is_err() {
        return server_error();
    }
    if !SLEEVES.contains(&adoption.sleeve.as_str()) {
        return HttpError::new(
            400,
            format!(
                "unknown sleeve {}",
                baskfy_providers::py::repr_str(&adoption.sleeve)
            ),
        )
        .response();
    }
    let Some(gateway) = sleeve_gateway(&state, &adoption.sleeve).await else {
        return server_error();
    };
    let settings = state.settings_snapshot();
    let now = state.clock.now_ist();
    let Stores { swing, twt, vbt } = stores;
    let mut book: Box<dyn AdoptBook> = match adoption.sleeve.as_str() {
        "twt" => Box::new(TwtBook(twt)),
        "vbt" => Box::new(VbtBook(vbt)),
        _ => Box::new(SwingBook(swing)),
    };
    match adopt(
        book.as_mut(),
        gateway.as_ref(),
        &settings,
        &adoption,
        &confirm,
        now,
        gtts.as_ref(),
    )
    .await
    {
        Ok(_) => crate::lanes::analytics::web::redirect_303("/lifecycle"),
        Err(e) => adopt_failed(&e),
    }
}

/// The page's routes: two reads and the one write, `/lifecycle/adopt`.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/lifecycle", get(lifecycle_page))
        .route("/lifecycle/data", get(lifecycle_data))
        .route("/lifecycle/adopt", post(lifecycle_adopt))
}
