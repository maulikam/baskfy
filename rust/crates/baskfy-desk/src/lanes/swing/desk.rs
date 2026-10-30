//! Ports the page half of `app/swing_desk.py` (SW7, SW11B, SW15, A7–A9, A14): the view
//! (`build_view`, `unavailable_view` and their pieces) and the seven `/swing` routes.
//!
//! **What this module is, and is not.** It reads the `sw_` tables for one user through
//! [`PgSwingStore`] and renders them; it takes a form post and hands it to
//! [`super::execute::execute_line`], which is the only thing in the desk that knows what a
//! confirmed swing line does. Nothing here names a broker write: the routes' only Kite calls are
//! reads (`ltp`, `order_history`), and every order-shaped act goes through the swing book's
//! gateway inside `execute`. A Confirm is one line; there is no route that takes more than one.
//!
//! **Values.** The view is built as the twt lane's [`Rec`] (an ordered Python dict), key for key
//! in Python's order, so `/swing/data` serialises byte-equal and `swing.html` sees the same
//! names. `_jsonable` here is this module's own: a `Decimal` is `format(x, "f")`, not `str(x)`.

use std::sync::Arc;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{Response, Uri};
use axum::routing::{get, post};
use baskfy_core::swing::plan::first_live_multiplier;
use baskfy_core::swing::setups::SwingConfig;
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveTime, Timelike};
use rust_decimal::{Decimal, RoundingStrategy};

use super::execute::{
    ExecContext, ExecError, ExecOutcome, OrderReport, OrderSource, OrderUpdate, TokioTimer,
    cutoff_open_orders, execute_line, on_order_update, rearm_gtt, swing_gates, swing_stop_band,
};
use super::live_gate::{DeskLevel, LiveLevel};
use super::rules::{py_dec, quantize};
use super::store::{
    Catalyst, LineRow, MarketScan, PgSwingStore, PlanRow, PositionRow, ScanRun, SessionRow,
    SignalRow, SignalSkip, SkipRow, SleeveConfig, StoreError, SwingStore,
};
use crate::app::AppState;
use crate::clock::ist;
use crate::gateway::Gateway;
use crate::lanes::twt::common::{
    FormFields, HttpError, Rec, Val, json_response, py_isoformat, server_error,
};
use crate::settings::Settings;
use crate::storage::StorageError;
use crate::wire::{PyDict, PyValue};

/// `EXIT_KINDS`: the plan-line kinds that act on a held position, in the plan panel's order.
pub const EXIT_KINDS: [&str; 2] = ["SELL_AT_OPEN", "RAISE_GTT_STOP"];
/// `EXECUTABLE_KINDS` (A7): the kinds a Confirm may post.
pub const EXECUTABLE_KINDS: [&str; 3] = ["BUY_ON_TRIGGER", "SELL_AT_OPEN", "RAISE_GTT_STOP"];
/// `PENDING_KIND`.
pub const PENDING_KIND: &str = "PENDING_RANGE";
/// `PROPOSED`: the only line state a Confirm button may be attached to.
pub const PROPOSED: &str = "PROPOSED";
/// `MANAGE_ACTIONS_SHOWN`.
pub const MANAGE_ACTIONS_SHOWN: i64 = 5;
/// `POLL_MS`.
pub const POLL_MS: i64 = 5_000;

// =============================================================================================
// Rows as Python dicts
// =============================================================================================

fn dec(d: Option<Decimal>) -> Val {
    d.map_or(Val::None, Val::Dec)
}
fn date_v(d: Option<NaiveDate>) -> Val {
    d.map_or(Val::None, Val::Date)
}
fn stamp_v(t: Option<DateTime<FixedOffset>>) -> Val {
    t.map_or(Val::None, Val::Stamp)
}
fn str_v(s: Option<&str>) -> Val {
    s.map_or(Val::None, |v| Val::Str(v.to_owned()))
}
fn int_v(i: Option<i64>) -> Val {
    i.map_or(Val::None, Val::Int)
}

/// `_plan_row`.
#[must_use]
pub fn plan_rec(p: &PlanRow) -> Rec {
    Rec::of([
        ("id", Val::Int(p.id)),
        ("plan_id", Val::Str(p.plan_id.clone())),
        ("as_of", date_v(p.as_of)),
        ("source", Val::Str(p.source.clone())),
        ("built_at", stamp_v(p.built_at)),
        ("expires_at", stamp_v(p.expires_at)),
        ("gate", Val::Str(p.gate.clone())),
        ("exposure_level", Val::Int(p.exposure_level)),
        ("total_risk_inr", dec(p.total_risk_inr)),
        ("total_new_exposure_inr", dec(p.total_new_exposure_inr)),
    ])
}

/// `_line_row`.
#[must_use]
pub fn line_rec(l: &LineRow) -> Rec {
    Rec::of([
        ("id", Val::Int(l.id)),
        ("plan_pk", Val::Int(l.plan_pk)),
        ("plan_id", Val::Str(l.plan_id.clone())),
        ("kind", Val::Str(l.kind.clone())),
        ("instrument_id", Val::Int(l.instrument_id)),
        ("symbol", Val::Str(l.symbol.clone())),
        ("setup", str_v(l.setup.as_deref())),
        ("quantity", Val::Int(l.quantity)),
        ("trigger", dec(l.trigger)),
        ("stop", dec(l.stop)),
        ("risk_inr", dec(l.risk_inr)),
        ("position_value", dec(l.position_value)),
        ("trail", str_v(l.trail.as_deref())),
        ("note", str_v(l.note.as_deref())),
        ("state", Val::Str(l.state.clone())),
        ("client_id", Val::Str(l.client_id.clone())),
        ("journal_ref", str_v(l.journal_ref.as_deref())),
        ("position_id", int_v(l.position_id)),
        ("updated_at", stamp_v(l.updated_at)),
        ("as_of", date_v(l.as_of)),
        ("plan_source", Val::Str(l.plan_source.clone())),
    ])
}

/// `_position_row`.
#[must_use]
pub fn position_rec(p: &PositionRow) -> Rec {
    Rec::of([
        ("id", Val::Int(p.id)),
        ("instrument_id", Val::Int(p.instrument_id)),
        ("symbol", Val::Str(p.symbol.clone())),
        ("setup", Val::Str(p.setup.clone())),
        ("entry_date", date_v(p.entry_date)),
        ("entry_avg", dec(p.entry_avg)),
        ("quantity_entered", Val::Int(p.quantity_entered)),
        ("quantity_open", Val::Int(p.quantity_open)),
        ("initial_stop", dec(p.initial_stop)),
        ("stop", dec(p.stop)),
        ("gtt_id", str_v(p.gtt_id.as_deref())),
        ("gtt_trigger", dec(p.gtt_trigger)),
        ("gtt_armed_at", stamp_v(p.gtt_armed_at)),
        ("trail", Val::Str(p.trail.clone())),
        ("partial_done", Val::Bool(p.partial_done)),
        ("partial_date", date_v(p.partial_date)),
        ("state", Val::Str(p.state.clone())),
        ("closed_on", date_v(p.closed_on)),
        ("exit_avg", dec(p.exit_avg)),
        ("close_reason", str_v(p.close_reason.as_deref())),
        ("r_multiple", dec(p.r_multiple)),
        ("pnl_inr", dec(p.pnl_inr)),
        ("simulated", Val::Bool(p.simulated)),
        ("half_risk", Val::Bool(p.half_risk)),
    ])
}

/// `session(day)`'s dict.
#[must_use]
pub fn session_rec(s: &SessionRow) -> Rec {
    Rec::of([
        ("session_date", date_v(s.session_date)),
        ("mode", Val::Str(s.mode.clone())),
        ("monitor_ran", Val::Bool(s.monitor_ran)),
        ("signals", Val::Int(s.signals)),
        ("confirms", Val::Int(s.confirms)),
        ("fills", Val::Int(s.fills)),
        ("manage_actions", Val::Int(s.manage_actions)),
        ("notes", str_v(s.notes.as_deref())),
    ])
}

/// `signals_for(day)`'s dict.
#[must_use]
pub fn signal_rec(s: &SignalRow) -> Rec {
    Rec::of([
        ("id", Val::Int(s.id)),
        ("instrument_id", Val::Int(s.instrument_id)),
        ("symbol", Val::Str(s.symbol.clone())),
        ("setup", Val::Str(s.setup.clone())),
        ("session_date", date_v(s.session_date)),
        ("raised_at", stamp_v(s.raised_at)),
        ("state", Val::Str(s.state.clone())),
        ("or_window_minutes", int_v(s.or_window_minutes)),
        ("range_high", dec(s.range_high)),
        ("range_low", dec(s.range_low)),
        ("low_of_day", dec(s.low_of_day)),
        ("last_price", dec(s.last_price)),
        ("entry", dec(s.entry)),
        ("stop", dec(s.stop)),
        ("plan_line_id", int_v(s.plan_line_id)),
        ("focus", Val::Bool(s.focus)),
    ])
}

fn skip_rec(k: &SkipRow) -> Rec {
    Rec::of([
        ("id", Val::Int(k.id)),
        ("instrument_id", int_v(k.instrument_id)),
        ("symbol", Val::Str(k.symbol.clone())),
        ("reason", Val::Str(k.reason.clone())),
        ("detail", str_v(k.detail.as_deref())),
    ])
}

fn signal_skip_rec(k: &SignalSkip) -> Rec {
    Rec::of([
        ("reason", Val::Str(k.reason.clone())),
        ("detail", str_v(k.detail.as_deref())),
        ("plan_id", Val::Str(k.plan_id.clone())),
    ])
}

fn catalyst_rec(c: &Catalyst) -> Rec {
    Rec::of([
        ("headline", str_v(c.headline.as_deref())),
        ("published_at", stamp_v(c.published_at)),
        ("url", str_v(c.url.as_deref())),
        ("earnings_date", date_v(c.earnings_date)),
    ])
}

fn scan_run_rec(r: &ScanRun) -> Rec {
    Rec::of([
        ("id", Val::Int(r.id)),
        ("requested_at", stamp_v(r.requested_at)),
        ("started_at", stamp_v(r.started_at)),
        ("finished_at", stamp_v(r.finished_at)),
        ("session_date", date_v(r.session_date)),
        ("provisional", Val::Bool(r.provisional)),
        ("status", Val::Str(r.status.clone())),
        ("funnel", r.funnel.clone().map_or(Val::None, Val::Py)),
        ("detail", r.detail.clone().map_or(Val::None, Val::Py)),
        ("error", str_v(r.error.as_deref())),
        ("task_id", str_v(r.task_id.as_deref())),
    ])
}

// =============================================================================================
// `_jsonable` (Decimal → `format(x, "f")`) and Python's `str()` for the fingerprint
// =============================================================================================

/// `format(Decimal, "f")`.
#[must_use]
pub fn dec_f(d: Decimal) -> String {
    d.to_string()
}

/// `_jsonable(value)`.
#[must_use]
pub fn jsonable(v: &Val) -> PyValue {
    match v {
        Val::None => PyValue::None,
        Val::Bool(b) => PyValue::Bool(*b),
        Val::Int(i) => PyValue::Int(*i),
        Val::Float(f) => PyValue::Float(*f),
        Val::Dec(d) => PyValue::Str(dec_f(*d)),
        Val::Str(s) => PyValue::Str(s.clone()),
        Val::Date(d) => PyValue::Str(d.format("%Y-%m-%d").to_string()),
        Val::Stamp(t) => PyValue::Str(py_isoformat(t, 'T')),
        Val::Naive(t) => PyValue::Str(t.format("%Y-%m-%dT%H:%M:%S%.f").to_string()),
        Val::Py(p) => jsonable_py(p),
        Val::List(items) => PyValue::List(items.iter().map(jsonable).collect()),
        Val::Map(rec) => PyValue::Map(jsonable_rec(rec)),
    }
}

fn jsonable_py(p: &PyValue) -> PyValue {
    use baskfy_providers::records::PyDateTime;
    match p {
        PyValue::Date(d) => PyValue::Str(d.format("%Y-%m-%d").to_string()),
        PyValue::DateTime(PyDateTime::Aware(t)) => PyValue::Str(py_isoformat(t, 'T')),
        PyValue::List(items) => PyValue::List(items.iter().map(jsonable_py).collect()),
        PyValue::Map(m) => {
            let mut out = PyDict::new();
            for (k, v) in m.iter() {
                out.insert(k, jsonable_py(v));
            }
            PyValue::Map(out)
        }
        other => other.clone(),
    }
}

/// `_jsonable(dict)`.
#[must_use]
pub fn jsonable_rec(rec: &Rec) -> PyDict {
    let mut out = PyDict::new();
    for (k, v) in &rec.0 {
        out.insert(k.clone(), jsonable(v));
    }
    out
}

// =============================================================================================
// The view's pieces
// =============================================================================================

/// `_countdown(expires_at, now)`: `m:ss` until the plan expires; `0:00` once it has.
#[must_use]
pub fn countdown(expires_at: Option<DateTime<FixedOffset>>, now: DateTime<FixedOffset>) -> String {
    let Some(at) = expires_at else {
        return "0:00".to_owned();
    };
    // `int(timedelta.total_seconds())`: truncation toward zero.
    let micros = (at - now).num_microseconds().unwrap_or(i64::MIN);
    let left = micros / 1_000_000;
    if left <= 0 {
        return "0:00".to_owned();
    }
    format!("{}:{:02}", left / 60, left % 60)
}

/// `_pct_of_sleeve(value, capital)`.
#[must_use]
pub fn pct_of_sleeve(value: Option<Decimal>, capital: Option<Decimal>) -> Option<Decimal> {
    let value = value?;
    let capital = capital.filter(|c| !c.is_zero())?;
    let ratio = crate::lanes::twt::common::ddiv(value, capital).ok()?;
    let pct = ratio.checked_mul(Decimal::ONE_HUNDRED)?;
    Some(quantize(pct, 2, RoundingStrategy::MidpointAwayFromZero))
}

/// `_price(value)`: two places, `—` for none.
#[must_use]
pub fn price(value: Option<Decimal>) -> String {
    value.map_or_else(
        || "\u{2014}".to_owned(),
        |v| py_dec(quantize(v, 2, RoundingStrategy::MidpointNearestEven)),
    )
}

/// `_label(line)`: always led by SWING.
#[must_use]
pub fn label(
    kind: &str,
    symbol: &str,
    quantity: i64,
    trigger: Option<Decimal>,
    stop: Option<Decimal>,
) -> String {
    match kind {
        "BUY_ON_TRIGGER" => format!("SWING BUY {symbol} x{quantity} @ {}", price(trigger)),
        "SELL_AT_OPEN" => format!("SWING SELL {symbol} x{quantity} at open"),
        "RAISE_GTT_STOP" => format!("SWING RAISE GTT {symbol} to {}", price(stop)),
        PENDING_KIND => format!(
            "SWING PENDING {symbol} \u{2014} range at {}, no stop yet",
            price(trigger)
        ),
        other => format!("SWING {other} {symbol}"),
    }
}

fn line_label(l: &LineRow) -> String {
    label(&l.kind, &l.symbol, l.quantity, l.trigger, l.stop)
}

/// `_line_view(...)`'s refusal sentence: `""` when Confirm may appear.
#[must_use]
pub fn why_not(line: &LineRow, expired: bool, position: Option<&PositionRow>) -> String {
    if !EXECUTABLE_KINDS.contains(&line.kind.as_str()) {
        return if line.state == PROPOSED {
            "pending the opening range \u{2014} not confirmable; the SIGNAL plan at window close \
             is the line"
                .to_owned()
        } else {
            format!("{} (pending range)", line.state.to_lowercase())
        };
    }
    if line.state != PROPOSED {
        return line.state.to_lowercase();
    }
    if expired {
        return "plan expired".to_owned();
    }
    if line.kind == "BUY_ON_TRIGGER"
        && let Some(p) = position
    {
        return format!("already held ({} open)", p.quantity_open);
    }
    let Some(p) = position else {
        return if EXIT_KINDS.contains(&line.kind.as_str()) {
            "not a swing position".to_owned()
        } else {
            String::new()
        };
    };
    if line.kind == "SELL_AT_OPEN" && line.quantity > p.quantity_open {
        return format!("more than the {} open", p.quantity_open);
    }
    if line.kind == "RAISE_GTT_STOP"
        && let (Some(new), Some(old)) = (line.stop, p.stop)
        && new <= old
    {
        return format!("not above the resting stop {}", py_dec(old));
    }
    String::new()
}

fn plan_expired(plan: &PlanRow, now: DateTime<FixedOffset>) -> bool {
    plan.expires_at.is_none_or(|e| now > e)
}

/// `_line_view(line, plan, now=, config=, held=)`.
fn line_view(
    line: &LineRow,
    plan: &PlanRow,
    now: DateTime<FixedOffset>,
    config: &SleeveConfig,
    held: &[PositionRow],
) -> Rec {
    let expired = plan_expired(plan, now);
    // `held = {p["instrument_id"]: p for p in positions}`: a later row for the same instrument
    // replaces an earlier one (nothing in the schema forbids two open rows), so the LAST wins.
    let position = held.iter().rfind(|p| p.instrument_id == line.instrument_id);
    let why = why_not(line, expired, position);
    let mut r = line_rec(line);
    r.set("label", Val::Str(line_label(line)));
    r.set("plan_expires_at", stamp_v(plan.expires_at));
    r.set("countdown", Val::Str(countdown(plan.expires_at, now)));
    r.set("expired", Val::Bool(expired));
    r.set("held", Val::Bool(position.is_some()));
    r.set(
        "position",
        position.map_or(Val::None, |p| Val::Map(position_rec(p))),
    );
    r.set(
        "pct_of_sleeve",
        dec(pct_of_sleeve(
            line.position_value,
            Some(config.sleeve_capital_inr),
        )),
    );
    r.set("cap_pct", Val::Dec(config.max_position_pct));
    r.set("confirmable", Val::Bool(why.is_empty()));
    r.set("why_not", Val::Str(why));
    r
}

/// `_signal_text(signal)`.
#[must_use]
pub fn signal_text(s: &SignalRow) -> String {
    let window = s
        .or_window_minutes
        .filter(|w| *w != 0)
        .unwrap_or_else(|| SwingConfig::default().opening_range.default_window_minutes);
    let show = |d: Option<Decimal>| d.map_or_else(|| "None".to_owned(), py_dec);
    match s.state.as_str() {
        "TRIGGERED" => format!(
            "{window}-min ORH {} broken at {}",
            show(s.range_high),
            show(s.last_price)
        ),
        "LOCKED_UPPER_CIRCUIT" => format!(
            "locked at the upper circuit ({}) \u{2014} not enterable",
            show(s.last_price)
        ),
        "BELOW_PIVOT" => format!(
            "{window}-min ORH {} broken at {}, below the pivot",
            show(s.range_high),
            show(s.last_price)
        ),
        other => format!(
            "{} at {}",
            other.to_lowercase().replace('_', " "),
            show(s.last_price)
        ),
    }
}

/// `_todays_plan(store, today)`: the later of today's INTRADAY and MORNING plans.
async fn todays_plan(
    store: &mut PgSwingStore,
    today: NaiveDate,
) -> Result<Option<PlanRow>, StoreError> {
    let intraday = store.latest_plan("INTRADAY").await?;
    let morning = store.latest_plan("MORNING").await?;
    let mut best: Option<PlanRow> = None;
    for plan in [intraday, morning].into_iter().flatten() {
        if plan.as_of != Some(today) {
            continue;
        }
        // `max(..., key=built_at)` keeps the first of equal keys.
        let replace = match &best {
            None => true,
            Some(b) => built_key(&plan) > built_key(b),
        };
        if replace {
            best = Some(plan);
        }
    }
    Ok(best)
}

fn built_key(p: &PlanRow) -> Option<DateTime<FixedOffset>> {
    p.built_at
}

/// A plan view: the record plus its lines (the exits, buys and pending, with their views).
struct PlanView {
    rec: Rec,
    exits: Vec<(i64, Rec)>,
    buys: Vec<(i64, Rec)>,
    pending: Vec<(i64, Rec)>,
}

async fn plan_view(
    plan: Option<PlanRow>,
    store: &mut PgSwingStore,
    now: DateTime<FixedOffset>,
    config: &SleeveConfig,
    held: &[PositionRow],
) -> Result<Option<PlanView>, StoreError> {
    let Some(plan) = plan else { return Ok(None) };
    let lines = store.lines_for(plan.id).await?;
    let views: Vec<(&LineRow, Rec)> = lines
        .iter()
        .map(|l| (l, line_view(l, &plan, now, config, held)))
        .collect();
    let mut exits: Vec<(&LineRow, Rec)> = views
        .iter()
        .filter(|(l, _)| EXIT_KINDS.contains(&l.kind.as_str()))
        .cloned()
        .collect();
    exits.sort_by(|(a, _), (b, _)| {
        let ka = EXIT_KINDS.iter().position(|k| *k == a.kind);
        let kb = EXIT_KINDS.iter().position(|k| *k == b.kind);
        (ka, &a.symbol).cmp(&(kb, &b.symbol))
    });
    let buys: Vec<(i64, Rec)> = views
        .iter()
        .filter(|(l, _)| l.kind == "BUY_ON_TRIGGER")
        .map(|(l, r)| (l.instrument_id, r.clone()))
        .collect();
    let pending: Vec<(i64, Rec)> = views
        .iter()
        .filter(|(l, _)| l.kind == PENDING_KIND)
        .map(|(l, r)| (l.instrument_id, r.clone()))
        .collect();
    let skips = store.skips_for(plan.id).await?;
    let mut rec = plan_rec(&plan);
    rec.set("expired", Val::Bool(plan_expired(&plan, now)));
    rec.set("countdown", Val::Str(countdown(plan.expires_at, now)));
    // exits / buys / pending are set after the catalysts are attached (`assemble`).
    rec.set("exits", Val::None);
    rec.set("buys", Val::None);
    rec.set("pending", Val::None);
    rec.set(
        "skips",
        Val::List(skips.iter().map(|k| Val::Map(skip_rec(k))).collect()),
    );
    rec.set(
        "line_count",
        Val::Int(i64::try_from(lines.len()).unwrap_or(0)),
    );
    Ok(Some(PlanView {
        rec,
        exits: exits
            .into_iter()
            .map(|(l, r)| (l.instrument_id, r))
            .collect(),
        buys,
        pending,
    }))
}

impl PlanView {
    fn attach(&mut self, catalysts: &std::collections::BTreeMap<i64, Catalyst>) {
        for (id, r) in self
            .exits
            .iter_mut()
            .chain(self.buys.iter_mut())
            .chain(self.pending.iter_mut())
        {
            r.set(
                "catalyst",
                catalysts
                    .get(id)
                    .map_or(Val::None, |c| Val::Map(catalyst_rec(c))),
            );
        }
    }

    fn ids(&self) -> Vec<i64> {
        self.exits
            .iter()
            .chain(self.buys.iter())
            .chain(self.pending.iter())
            .map(|(id, _)| *id)
            .collect()
    }

    fn finish(mut self) -> Rec {
        let list =
            |v: Vec<(i64, Rec)>| Val::List(v.into_iter().map(|(_, r)| Val::Map(r)).collect());
        self.rec.set("exits", list(self.exits));
        self.rec.set("buys", list(self.buys));
        self.rec.set("pending", list(self.pending));
        self.rec
    }
}

fn hhmm(t: (i64, i64)) -> NaiveTime {
    let h = u32::try_from(t.0).unwrap_or(0);
    let m = u32::try_from(t.1).unwrap_or(0);
    NaiveTime::from_hms_opt(h, m, 0).unwrap_or_default()
}

/// `monitor_state(enabled=, monitor_ran=, now=)`.
#[must_use]
pub fn monitor_state(enabled: bool, monitor_ran: bool, now: DateTime<FixedOffset>) -> Rec {
    let window = SwingConfig::default().opening_range;
    let opens = hhmm(window.session_open);
    let closes = hhmm(window.monitor_close);
    let opens_text = opens.format("%H:%M").to_string();
    let closes_text = closes.format("%H:%M").to_string();
    let out = |state: &str, label: String| {
        Rec::of([("state", Val::from(state)), ("label", Val::Str(label))])
    };
    if !enabled {
        return out(
            "not enabled",
            "not enabled (BASKFY_SWING_MONITOR_ENABLED=false)".to_owned(),
        );
    }
    if monitor_ran {
        return out("stopped", format!("stopped at {closes_text}"));
    }
    let local = now.with_timezone(&ist());
    if local.weekday().num_days_from_monday() >= 5 {
        return out("idle", "idle (not a trading day)".to_owned());
    }
    let time = local.time();
    if time < opens {
        return out("idle", format!("idle \u{2014} starts at {opens_text}"));
    }
    if time <= closes {
        return out("running", format!("running since {opens_text}"));
    }
    out(
        "did not run",
        format!("did not run \u{2014} no monitor_ran mark after {closes_text}"),
    )
}

/// `token_age(now, token_file)`: how old the Kite access token is, read without touching Kite.
#[must_use]
pub fn token_age(now: DateTime<FixedOffset>, path: &std::path::Path) -> Rec {
    let absent = |label: &str| {
        Rec::of([
            ("present", Val::Bool(false)),
            ("label", Val::from(label)),
            ("age_minutes", Val::None),
            ("expired", Val::Bool(true)),
        ])
    };
    if !path.is_file() {
        return absent("no token \u{2014} log in");
    }
    let token = crate::lanes::exec_core::token_store::store_for(path, "")
        .ok()
        .and_then(|s| s.load().ok());
    let Some(token) = token else {
        return absent("token unreadable \u{2014} log in");
    };
    let issued = token.issued_at.with_timezone(&ist());
    let age_secs = (now - issued).num_microseconds().unwrap_or(0) as f64 / 1e6;
    #[allow(clippy::cast_possible_truncation)]
    let minutes = ((age_secs / 60.0).floor() as i64).max(0);
    let (hours, rest) = (minutes / 60, minutes % 60);
    let mut text = if hours > 0 {
        format!("{hours}h {rest:02}m old")
    } else {
        format!("{rest}m old")
    };
    let expired = token.is_expired(now);
    if expired {
        text = format!("expired (issued {})", issued.format("%d %b %H:%M"));
    }
    Rec::of([
        ("present", Val::Bool(true)),
        ("label", Val::Str(text)),
        ("age_minutes", Val::Int(minutes)),
        ("expired", Val::Bool(expired)),
    ])
}

fn ist_hm(t: &DateTime<FixedOffset>) -> String {
    t.with_timezone(&ist()).format("%H:%M IST").to_string()
}

fn py_int_of(v: &PyValue) -> i64 {
    match v {
        PyValue::Int(i) => *i,
        PyValue::Bool(b) => i64::from(*b),
        #[allow(clippy::cast_possible_truncation)]
        PyValue::Float(f) => f.trunc() as i64,
        PyValue::Str(s) => s.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

/// `scan_status(market, run)`: SW15's status-bar line.
#[must_use]
pub fn scan_status(market: Option<&MarketScan>, run: Option<&ScanRun>) -> Rec {
    let provisional = market.is_some_and(|m| m.provisional);
    let stamp = market.and_then(|m| m.scanned_at);
    let when = stamp.as_ref().map(ist_hm);
    let mut label = String::new();
    if provisional {
        label = when.as_ref().map_or_else(
            || "provisional \u{2014} from live quotes".to_owned(),
            |w| format!("provisional \u{2014} scanned {w} from live quotes"),
        );
    } else if let Some(w) = &when {
        label = format!("re-scanned {w} from published bars");
    }
    let mut run_label = String::new();
    let mut in_flight = false;
    if let Some(run) = run {
        let status = run.status.as_str();
        in_flight = matches!(status, "QUEUED" | "RUNNING");
        run_label = match status {
            "QUEUED" => "scan queued".to_owned(),
            "RUNNING" => "scanning\u{2026}".to_owned(),
            "FAILED" => format!(
                "last scan failed{}",
                run.error
                    .as_deref()
                    .filter(|e| !e.is_empty())
                    .map_or_else(String::new, |e| format!(": {e}"))
            ),
            _ => {
                let done = run.finished_at.or(run.requested_at);
                let done_at = done.as_ref().map_or_else(|| "done".to_owned(), ist_hm);
                let empty = PyDict::new();
                let funnel = match &run.funnel {
                    Some(PyValue::Map(m)) => m,
                    _ => &empty,
                };
                let liquid = funnel.get("liquid").filter(|v| !matches!(v, PyValue::None));
                let found: i64 = match funnel.get("candidates") {
                    Some(PyValue::Map(c)) => c.iter().map(|(_, n)| py_int_of(n)).sum(),
                    _ => 0,
                };
                let counts = liquid.map_or_else(String::new, |l| {
                    format!(" \u{b7} {} liquid, {found} flagged", l.py_str())
                });
                let what = if run.provisional {
                    "from live quotes"
                } else {
                    "from published bars"
                };
                format!("last scan {done_at} {what}{counts}")
            }
        };
    }
    Rec::of([
        ("date", date_v(market.and_then(|m| m.date))),
        ("provisional", Val::Bool(provisional)),
        ("scanned_at", stamp_v(stamp)),
        ("label", Val::Str(label)),
        ("run", run.map_or(Val::None, |r| Val::Map(scan_run_rec(r)))),
        ("run_label", Val::Str(run_label)),
        ("in_flight", Val::Bool(in_flight)),
    ])
}

/// `_refresh(now)`: (poll interval in ms, ms until the window opens today).
#[must_use]
pub fn refresh(now: DateTime<FixedOffset>) -> (i64, Option<i64>) {
    let window = SwingConfig::default().opening_range;
    let local = now.with_timezone(&ist());
    let at = |t: (i64, i64)| {
        local
            .with_hour(u32::try_from(t.0).unwrap_or(0))
            .and_then(|v| v.with_minute(u32::try_from(t.1).unwrap_or(0)))
            .and_then(|v| v.with_second(0))
            .and_then(|v| v.with_nanosecond(0))
            .unwrap_or(local)
    };
    let opens = at(window.session_open);
    let closes = at(window.monitor_close);
    if opens <= local && local <= closes {
        return (POLL_MS, None);
    }
    if local < opens {
        let micros = (opens - local).num_microseconds().unwrap_or(0);
        #[allow(clippy::cast_possible_truncation)]
        let ms = (micros as f64 / 1e6 * 1000.0) as i64;
        return (0, Some(ms));
    }
    (0, None)
}

/// SHA-1 (FIPS 180-4) — `hashlib.sha1` for the page fingerprint; no new dependency (R8.S1).
#[must_use]
pub fn sha1_hex(data: &[u8]) -> String {
    let mut h: [u32; 5] = [
        0x6745_2301,
        0xEFCD_AB89,
        0x98BA_DCFE,
        0x1032_5476,
        0xC3D2_E1F0,
    ];
    let mut msg = data.to_vec();
    let bit_len = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 80];
        for (i, word) in chunk.chunks(4).enumerate().take(16) {
            w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let [mut a, mut b, mut c, mut d, mut e] = h;
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A82_7999),
                20..=39 => (b ^ c ^ d, 0x6ED9_EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1B_BCDC),
                _ => (b ^ c ^ d, 0xCA62_C1D6),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    h.iter().map(|x| format!("{x:08x}")).collect()
}

fn rec_list<'a>(rec: &'a Rec, key: &str) -> Vec<&'a Rec> {
    match rec.get(key) {
        Some(Val::List(items)) => items
            .iter()
            .filter_map(|v| match v {
                Val::Map(r) => Some(r),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// `_fingerprint(signals, plans, positions, session, scan)`.
fn fingerprint(
    triggers: &[Rec],
    plans: &[Option<&Rec>],
    positions: &[PositionRow],
    session: Option<&SessionRow>,
    scan: &Rec,
) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(Val::Map(run)) = scan.get("run") {
        parts.push(format!(
            "c{}:{}",
            run.val("id").py_str(),
            run.val("status").py_str()
        ));
    }
    for s in triggers {
        let line_state = match s.get("line") {
            Some(Val::Map(l)) => l.val("state").py_str(),
            _ => "None".to_owned(),
        };
        parts.push(format!(
            "s{}:{}:{}:{line_state}",
            s.val("id").py_str(),
            s.val("state").py_str(),
            s.val("plan_line_id").py_str()
        ));
    }
    for p in plans.iter().flatten() {
        parts.push(format!(
            "p{}:{}",
            p.val("id").py_str(),
            p.val("expired").py_str()
        ));
        for key in ["exits", "buys", "pending"] {
            for ln in rec_list(p, key) {
                parts.push(format!(
                    "l{}:{}",
                    ln.val("id").py_str(),
                    ln.val("state").py_str()
                ));
            }
        }
    }
    for pos in positions {
        parts.push(format!(
            "o{}:{}:{}:{}",
            pos.id,
            pos.state,
            pos.gtt_id.as_deref().unwrap_or("None"),
            pos.quantity_open
        ));
    }
    if let Some(s) = session {
        let ran = if s.monitor_ran { "True" } else { "False" };
        parts.push(format!(
            "n{}/{}/{}/{}/{ran}",
            s.signals, s.confirms, s.fills, s.manage_actions
        ));
    }
    let digest = sha1_hex(parts.join("|").as_bytes());
    digest.chars().take(16).collect()
}

/// `EXIT_RULES["swing"]`.
#[must_use]
pub fn swing_exit_rule() -> String {
    let s = SwingConfig::default().stops;
    let pct = |x: f64| crate::lanes::twt::common::pct_text(&baskfy_core::py::float_repr(x));
    format!(
        "stop under the range low, at most {}% below entry; {}/{} sold between bar {} and {}, \
         then the stop to breakeven (or after {}R); the rest trails the 10-day MA (ADR at or \
         above {}%) or the 20-day MA; the GTT is the exit",
        pct(s.max_stop_distance_pct),
        s.partial_numerator,
        s.partial_denominator,
        s.partial_earliest_bar,
        s.partial_latest_bar,
        pct(s.breakeven_after_r),
        pct(s.fast_trail_min_adr_pct)
    )
}

/// The process's flags as `build_view` reads them (`app.config`).
#[derive(Debug, Clone)]
pub struct ViewFlags {
    /// `DRY_RUN`.
    pub dry_run: bool,
    /// `BASKFY_SWING_EXECUTION_ENABLED`.
    pub execution_enabled: bool,
    /// `BASKFY_SWING_MONITOR_ENABLED`.
    pub monitor_enabled: bool,
    /// The token's status-bar entry.
    pub token: Rec,
}

impl ViewFlags {
    /// From the settings and the token file.
    #[must_use]
    pub fn from_settings(
        s: &Settings,
        now: DateTime<FixedOffset>,
        token_path: &std::path::Path,
    ) -> Self {
        Self {
            dry_run: s.dry_run,
            execution_enabled: s.swing_execution_enabled,
            monitor_enabled: s.swing_monitor_enabled,
            token: token_age(now, token_path),
        }
    }
}

fn mode_label(dry_run: bool, simulated: bool) -> String {
    if simulated {
        format!(
            "SIMULATED \u{2014} every confirm journals simulated=true{}",
            if dry_run {
                " (DRY_RUN=true)"
            } else {
                " (BASKFY_SWING_EXECUTION_ENABLED=false)"
            }
        )
    } else {
        "LIVE \u{2014} confirms place real orders".to_owned()
    }
}

/// `build_view(store, now=, ...)`: the dict `swing.html` renders and `/swing/data` returns.
///
/// # Errors
/// The store's (the route turns it into [`unavailable_view`]).
pub async fn build_view(
    store: &mut PgSwingStore,
    now: DateTime<FixedOffset>,
    flags: &ViewFlags,
) -> Result<Rec, StoreError> {
    let now = now.with_timezone(&ist());
    let today = now.date_naive();
    let config = store.config().await?;
    let positions = store.open_positions().await?;
    let session = store.session(today).await?;

    // --- triggers --------------------------------------------------------------------------
    let mut triggers: Vec<(bool, i64, Rec)> = Vec::new();
    for signal in store.signals_for(today).await? {
        let line = match signal.plan_line_id {
            Some(id) if id != 0 => store.line(id).await?,
            _ => None,
        };
        let plan = match &line {
            Some(l) => store.plan(&l.plan_id).await?,
            None => None,
        };
        let lv = match (&line, &plan) {
            (Some(l), Some(p)) => Some(line_view(l, p, now, &config, &positions)),
            _ => None,
        };
        let skip = if lv.is_none() && signal.state == "TRIGGERED" {
            store.signal_skip_for(signal.instrument_id, today).await?
        } else {
            None
        };
        let confirmable = lv.as_ref().is_some_and(|r| r.truthy("confirmable"));
        let mut r = signal_rec(&signal);
        r.set("text", Val::Str(signal_text(&signal)));
        r.set("locked", Val::Bool(signal.state == "LOCKED_UPPER_CIRCUIT"));
        r.set("line", lv.map_or(Val::None, Val::Map));
        r.set(
            "plan",
            plan.as_ref().map_or(Val::None, |p| Val::Map(plan_rec(p))),
        );
        r.set(
            "skip",
            skip.as_ref()
                .map_or(Val::None, |k| Val::Map(signal_skip_rec(k))),
        );
        r.set("confirmable", Val::Bool(confirmable));
        triggers.push((signal.focus, signal.instrument_id, r));
    }
    // A14: the focus names first (a stable sort keeps newest-first inside each group).
    triggers.sort_by_key(|(focus, _, _)| i32::from(!*focus));

    // --- the plan --------------------------------------------------------------------------
    let morning = todays_plan(store, today).await?;
    let mut morning_view = plan_view(morning, store, now, &config, &positions).await?;
    let preview = store.latest_plan("EOD_PREVIEW").await?;
    let mut preview_view = plan_view(preview, store, now, &config, &positions).await?;

    // --- SW11B: the catalyst link on triggers and plan lines (A3) --------------------------
    let mut ids: Vec<i64> = triggers.iter().map(|(_, id, _)| *id).collect();
    for v in [&morning_view, &preview_view].into_iter().flatten() {
        ids.extend(v.ids());
    }
    let catalysts = store.catalysts_for(&ids).await?;
    for (_, id, r) in &mut triggers {
        r.set(
            "catalyst",
            catalysts
                .get(id)
                .map_or(Val::None, |c| Val::Map(catalyst_rec(c))),
        );
    }
    for v in [&mut morning_view, &mut preview_view].into_iter().flatten() {
        v.attach(&catalysts);
    }
    let morning_rec = morning_view.map(PlanView::finish);
    let preview_rec = preview_view.map(PlanView::finish);
    let triggers: Vec<Rec> = triggers.into_iter().map(|(_, _, r)| r).collect();

    // --- the book --------------------------------------------------------------------------
    let book: Vec<Rec> = positions
        .iter()
        .map(|p| {
            let mut r = position_rec(p);
            r.set("naked", Val::Bool(p.gtt_id.is_none()));
            let value = p.entry_avg.map(|e| {
                quantize(
                    e * Decimal::from(p.quantity_open),
                    2,
                    RoundingStrategy::MidpointNearestEven,
                )
            });
            r.set("value_inr", dec(value));
            r
        })
        .collect();
    let naked_count = positions.iter().filter(|p| p.gtt_id.is_none()).count();
    let manage: Vec<Val> = store
        .recent_manage_actions(MANAGE_ACTIONS_SHOWN)
        .await?
        .iter()
        .map(|l| {
            let mut r = line_rec(l);
            r.set("label", Val::Str(line_label(l)));
            Val::Map(r)
        })
        .collect();

    // --- the status bar --------------------------------------------------------------------
    let counters = session.as_ref().map_or_else(
        || {
            Rec::of([
                ("session_date", Val::Date(today)),
                ("mode", Val::None),
                ("monitor_ran", Val::Bool(false)),
                ("signals", Val::Int(0)),
                ("confirms", Val::Int(0)),
                ("fills", Val::Int(0)),
                ("manage_actions", Val::Int(0)),
                ("notes", Val::None),
            ])
        },
        session_rec,
    );
    let monitor_ran = session.as_ref().is_some_and(|s| s.monitor_ran);
    let simulated = flags.dry_run || !flags.execution_enabled;
    let left = config.first_live_sessions_left;
    let sizing = SwingConfig::default().sizing;
    let multiplier = first_live_multiplier(left, !simulated, &sizing).unwrap_or(Decimal::ONE);
    let risk_in_force = quantize(
        config.risk_per_trade_pct * multiplier,
        3,
        RoundingStrategy::MidpointNearestEven,
    );
    let first_live_header = if left > 0 {
        format!(
            "first live sessions: {left} left \u{b7} risk {}%",
            py_dec(risk_in_force)
        )
    } else {
        String::new()
    };
    let sent_lines: Vec<Val> = store
        .sent_buy_lines(today)
        .await?
        .iter()
        .map(|l| {
            Val::Map(Rec::of([
                ("id", Val::Int(l.id)),
                ("symbol", Val::Str(l.symbol.clone())),
                ("quantity", Val::Int(l.quantity)),
                ("journal_ref", str_v(l.journal_ref.as_deref())),
                ("position_id", int_v(l.position_id)),
            ]))
        })
        .collect();
    let (poll_ms, window_opens_in_ms) = refresh(now);
    let market = store.latest_market_scan().await?;
    let run = store.latest_scan_run().await?;
    let scan = scan_status(market.as_ref(), run.as_ref());
    let fp = fingerprint(
        &triggers,
        &[morning_rec.as_ref(), preview_rec.as_ref()],
        &positions,
        session.as_ref(),
        &scan,
    );
    let mut session_view = counters.clone();
    session_view.set("recorded", Val::Bool(session.is_some()));
    let status = Rec::of([
        ("dry_run", Val::Bool(flags.dry_run)),
        ("execution_enabled", Val::Bool(flags.execution_enabled)),
        ("monitor_enabled", Val::Bool(flags.monitor_enabled)),
        ("simulated", Val::Bool(simulated)),
        ("mode_label", Val::Str(mode_label(flags.dry_run, simulated))),
        (
            "monitor",
            Val::Map(monitor_state(flags.monitor_enabled, monitor_ran, now)),
        ),
        ("token", Val::Map(flags.token.clone())),
        ("session", Val::Map(session_view)),
        ("scan", Val::Map(scan)),
    ]);
    let sleeve = Rec::of([
        ("capital_inr", Val::Dec(config.sleeve_capital_inr)),
        ("risk_per_trade_pct", Val::Dec(config.risk_per_trade_pct)),
        ("max_position_pct", Val::Dec(config.max_position_pct)),
        ("max_open_positions", Val::Int(config.max_open_positions)),
        ("exposure_level", Val::Int(config.exposure_level)),
        (
            "first_live_sessions_left",
            Val::Int(config.first_live_sessions_left),
        ),
        ("risk_multiplier", Val::Str(py_dec(multiplier))),
        ("risk_pct_in_force", Val::Dec(risk_in_force)),
        ("first_live_header", Val::Str(first_live_header)),
        ("half_risk", Val::Bool(multiplier < Decimal::ONE)),
        ("configured", Val::Bool(config.present)),
    ]);
    Ok(Rec::of([
        ("available", Val::Bool(true)),
        ("reason", Val::from("")),
        ("now", Val::Stamp(now)),
        ("today", Val::Date(today)),
        ("status", Val::Map(status)),
        ("sleeve", Val::Map(sleeve)),
        ("sent_lines", Val::List(sent_lines)),
        (
            "triggers",
            Val::List(triggers.into_iter().map(Val::Map).collect()),
        ),
        (
            "plans",
            Val::Map(Rec::of([
                ("morning", morning_rec.map_or(Val::None, Val::Map)),
                ("preview", preview_rec.map_or(Val::None, Val::Map)),
            ])),
        ),
        ("exit_rule", Val::Str(swing_exit_rule())),
        (
            "book",
            Val::Map(Rec::of([
                (
                    "positions",
                    Val::List(book.into_iter().map(Val::Map).collect()),
                ),
                (
                    "naked_count",
                    Val::Int(i64::try_from(naked_count).unwrap_or(0)),
                ),
                ("manage", Val::List(manage)),
            ])),
        ),
        ("poll_ms", Val::Int(poll_ms)),
        ("window_opens_in_ms", int_v(window_opens_in_ms)),
        ("fingerprint", Val::Str(fp)),
    ]))
}

/// `unavailable_view(reason, now=)`: what the page shows when the store cannot be read.
#[must_use]
pub fn unavailable_view(reason: &str, now: DateTime<FixedOffset>, flags: &ViewFlags) -> Rec {
    let now = now.with_timezone(&ist());
    let simulated = flags.dry_run || !flags.execution_enabled;
    let status = Rec::of([
        ("dry_run", Val::Bool(flags.dry_run)),
        ("execution_enabled", Val::Bool(flags.execution_enabled)),
        ("monitor_enabled", Val::Bool(flags.monitor_enabled)),
        ("simulated", Val::Bool(simulated)),
        ("mode_label", Val::from("")),
        (
            "monitor",
            Val::Map(monitor_state(flags.monitor_enabled, false, now)),
        ),
        ("token", Val::Map(flags.token.clone())),
        (
            "session",
            Val::Map(Rec::of([
                ("recorded", Val::Bool(false)),
                ("signals", Val::Int(0)),
                ("confirms", Val::Int(0)),
                ("fills", Val::Int(0)),
                ("manage_actions", Val::Int(0)),
                ("monitor_ran", Val::Bool(false)),
                ("mode", Val::None),
            ])),
        ),
        ("scan", Val::Map(scan_status(None, None))),
    ]);
    Rec::of([
        ("available", Val::Bool(false)),
        ("reason", Val::from(reason)),
        ("now", Val::Stamp(now)),
        ("today", Val::Date(now.date_naive())),
        ("status", Val::Map(status)),
        ("sleeve", Val::None),
        ("triggers", Val::List(Vec::new())),
        (
            "plans",
            Val::Map(Rec::of([("morning", Val::None), ("preview", Val::None)])),
        ),
        ("exit_rule", Val::Str(swing_exit_rule())),
        (
            "book",
            Val::Map(Rec::of([
                ("positions", Val::List(Vec::new())),
                ("naked_count", Val::Int(0)),
                ("manage", Val::List(Vec::new())),
            ])),
        ),
        ("poll_ms", Val::Int(0)),
        ("window_opens_in_ms", Val::None),
        ("fingerprint", Val::from("")),
    ])
}

// =============================================================================================
// Wiring: the store, the gateway, the broker reads
// =============================================================================================

/// `open_store()`: the sole user's store over the desk's connection.
///
/// # Errors
/// The connection.
pub async fn open_store(state: &AppState) -> Result<PgSwingStore, StorageError> {
    let settings = state.settings_snapshot();
    let conn = state.db.connect().await?;
    let pg = state.db.config().is_postgres();
    let live_level: Option<Arc<dyn LiveLevel>> = if pg {
        Some(Arc::new(DeskLevel::new(
            desk_gateway(state).map(crate::lanes::exec_core::desk::DeskGateway::kite),
            state.settings.clone(),
        )))
    } else {
        None
    };
    Ok(PgSwingStore {
        conn,
        user_id: settings.sole_user_id,
        schema: if pg { "public" } else { "" }.to_owned(),
        broker_account_id: Some(settings.sole_broker_account_id),
        live_level,
        settings,
        clock: state.clock.clone(),
    })
}

/// `swing_gateway()`: the swing book's gateway, derived from the desk's (one risk manager).
///
/// Python builds it through `app.main.gateway()`, which builds the process's `_risk` as a side
/// effect (on Postgres: the day's `risk_ledger` row); the same happens here through the exec_core
/// lane's shared risk manager before the sleeve's gateway is derived.
///
/// `None` is Python's `RuntimeError("KITE_API_KEY missing …")`: `_main.gateway()` builds the
/// risk manager (the day's `risk_ledger` row) and then constructs `Kite()`, which refuses
/// without a key, so every route that builds the gateway answers 500 there.
pub async fn swing_gateway(state: &AppState) -> Option<Arc<dyn Gateway>> {
    // `_risk = RiskManager(cfg, store=_risk_store())` runs before `OrderGateway(kite().kc, …)`,
    // so the day's risk_ledger row is written even when the Kite constructor then refuses.
    if !kite_configured(state) {
        // `_gateway` stays None, so Python builds a NEW `_risk` on every call (each writing the
        // then-current day's row) before `Kite()` refuses; the shared cell builds only once.
        if desk_gateway(state).is_some() {
            let fresh = crate::lanes::exec_core::desk::SharedRisk::new(
                state.settings.clone(),
                state.clock.clone(),
                state.db.clone(),
            );
            let _ = fresh.get().await;
        }
        return None;
    }
    if let Some(gw) = desk_gateway(state) {
        let _ = gw.risk().await;
    }
    let settings = state.settings_snapshot();
    // An inverted band is Python's `StopBand` raising inside `build_swing_gateway`: a 500.
    let band = swing_stop_band(&settings).ok()?;
    Some(super::gateway::swing_sleeve_gateway(state, band))
}

/// `Kite()` can be constructed: `KITE_API_KEY` is set (`kite_client.Kite.__init__`).
fn kite_configured(state: &AppState) -> bool {
    !state.settings_snapshot().kite_api_key.is_empty()
}

fn desk_gateway(state: &AppState) -> Option<&crate::lanes::exec_core::desk::DeskGateway> {
    state
        .gateway
        .as_any()?
        .downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>()
}

fn py_float_value(v: &PyValue) -> Option<f64> {
    match v {
        PyValue::Float(f) => Some(*f),
        #[allow(clippy::cast_precision_loss)]
        PyValue::Int(i) => Some(*i as f64),
        _ => None,
    }
}

/// `last_price(symbol)`: the broker's last traded price (a READ through the desk's Kite wrapper,
/// `Kite.ltp`), `None` without a session or a price.
pub async fn last_price(state: &AppState, symbol: &str) -> Option<Decimal> {
    // `_main.kite()` raising (no KITE_API_KEY) is caught by Python's `except Exception`: None.
    if !kite_configured(state) {
        return None;
    }
    let value = if let Some(gw) = desk_gateway(state) {
        let prices = gw.kite().ltp(&[symbol.to_owned()], "NSE").await.ok()?;
        prices.get(symbol).and_then(py_float_value)
    } else {
        let key = format!("NSE:{symbol}");
        let answer = state.kite.ltp(std::slice::from_ref(&key)).await.ok()?;
        let PyValue::Map(m) = answer else { return None };
        match m.get(&key)? {
            PyValue::Map(row) => row.get("last_price").and_then(py_float_value),
            other => py_float_value(other),
        }
    }?;
    if value == 0.0 {
        return None;
    }
    Some(super::rules::dec_of_float(value))
}

/// `KiteOrders`: A8's `OrderSource` over the desk's Kite session — a READ of the history.
pub struct KiteOrders {
    state: AppState,
}

/// `order_source()`: `None` when `Kite()` cannot be constructed (no `KITE_API_KEY`), in which
/// case a live confirm answers `SENT` without polling and the reconcile skips every line.
fn order_source(state: &AppState) -> Option<KiteOrders> {
    kite_configured(state).then(|| KiteOrders {
        state: state.clone(),
    })
}

#[async_trait::async_trait]
impl OrderSource for KiteOrders {
    async fn order_status(&self, order_id: &str) -> OrderReport {
        let open = || OrderReport {
            status: "OPEN".to_owned(),
            filled_quantity: 0,
            average_price: Decimal::ZERO,
        };
        let Ok(PyValue::List(history)) = self.state.kite.order_history(order_id).await else {
            return open();
        };
        let Some(PyValue::Map(last)) = history.last() else {
            return open();
        };
        let status = match last.get("status") {
            Some(PyValue::Str(s)) if !s.is_empty() => s.clone(),
            _ => "OPEN".to_owned(),
        };
        let filled = last.get("filled_quantity").map_or(0, py_int_of);
        let avg = last
            .get("average_price")
            .and_then(py_float_value)
            .map_or(Decimal::ZERO, super::rules::dec_of_float);
        OrderReport {
            status,
            filled_quantity: filled,
            average_price: avg,
        }
    }
}

// =============================================================================================
// Routes
// =============================================================================================

fn store_error_text(e: &StoreError) -> String {
    let text = match e {
        StoreError::Storage(StorageError::Driver(sqlx::Error::Database(db))) => {
            db.message().to_owned()
        }
        other => other.to_string(),
    };
    text.chars().take(200).collect()
}

async fn view(state: &AppState) -> Rec {
    let now = state.clock.now_ist();
    let settings = state.settings_snapshot();
    let flags =
        ViewFlags::from_settings(&settings, now, &state.paths.resolve(&settings.token_file));
    let built = match open_store(state).await {
        Ok(mut store) => build_view(&mut store, now, &flags).await,
        Err(e) => Err(StoreError::Storage(e)),
    };
    built.unwrap_or_else(|e| unavailable_view(&store_error_text(&e), now, &flags))
}

/// A `date`/`datetime` for the template that is truthy, as Python's are (`{% if p.gtt_armed_at %}`,
/// `{% if v.status.scan.date %}`): the foundation's `PyDate` enumerates as empty, which
/// minijinja reads as false. Everything else is `PyDate`'s.
#[derive(Debug)]
struct TruthyDate(Arc<crate::templates::PyDate>);

impl minijinja::value::Object for TruthyDate {
    fn get_value(self: &Arc<Self>, key: &minijinja::Value) -> Option<minijinja::Value> {
        minijinja::value::Object::get_value(&self.0, key)
    }

    fn call_method(
        self: &Arc<Self>,
        state: &mut minijinja::State<'_, '_>,
        method: &str,
        args: &[minijinja::Value],
    ) -> Result<minijinja::Value, minijinja::Error> {
        minijinja::value::Object::call_method(&self.0, state, method, args)
    }

    fn render(self: &Arc<Self>, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0.isoformat(' '))
    }

    fn is_true(self: &Arc<Self>) -> bool {
        true
    }
}

/// The view as `swing.html` sees it (the twt lane's conversion, dates made truthy).
#[must_use]
pub fn template_value(v: &Val) -> minijinja::Value {
    use crate::templates::PyDate;
    let date = |d: PyDate| minijinja::Value::from_object(TruthyDate(Arc::new(d)));
    match v {
        Val::Date(d) => date(PyDate::Date(*d)),
        Val::Stamp(t) => date(PyDate::Aware(*t)),
        Val::Naive(t) => date(PyDate::Naive(*t)),
        Val::List(items) => {
            minijinja::Value::from(items.iter().map(template_value).collect::<Vec<_>>())
        }
        Val::Map(rec) => minijinja::Value::from_pairs(
            rec.0
                .iter()
                .map(|(k, v)| (k.clone(), template_value(v)))
                .collect::<Vec<_>>(),
        ),
        other => crate::lanes::twt::common::template_value(other),
    }
}

/// `templates.TemplateResponse(request, "swing.html", {"v": v})`.
fn render_page(state: &AppState, uri: &Uri, v: &Rec) -> Response<Body> {
    let request = minijinja::Value::from_pairs(vec![(
        "url".to_owned(),
        minijinja::Value::from_pairs(vec![(
            "path".to_owned(),
            minijinja::Value::from(uri.path().to_owned()),
        )]),
    )]);
    let ctx = minijinja::Value::from_pairs(vec![
        ("request".to_owned(), request),
        ("v".to_owned(), template_value(&Val::Map(v.clone()))),
    ]);
    match state.templates.render("swing.html", ctx) {
        Ok(text) => crate::lanes::twt::common::html_response(text),
        Err(_) => server_error(),
    }
}

async fn swing_page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let v = view(&state).await;
    render_page(&state, &uri, &v)
}

async fn swing_data(State(state): State<AppState>) -> Response<Body> {
    let v = view(&state).await;
    json_response(200, &PyValue::Map(jsonable_rec(&v)))
}

fn outcome_json(outcome: &ExecOutcome, extra: (&str, i64)) -> PyValue {
    let mut m = outcome.to_py();
    m.insert(extra.0, PyValue::Int(extra.1));
    PyValue::Map(m)
}

fn outcome_json_plain(outcome: &ExecOutcome) -> PyValue {
    PyValue::Map(outcome.to_py())
}

/// `_refused_json(exc, **extra)`: a guard's refusal as BLOCKED with the guard's words.
fn refused_json(message: &str, simulated: bool, extra: (&str, i64)) -> PyValue {
    let mut m = PyDict::new();
    m.insert("status", PyValue::Str("BLOCKED".to_owned()));
    m.insert("reason", PyValue::Str(message.to_owned()));
    m.insert("order", PyValue::None);
    m.insert("gtt", PyValue::None);
    m.insert("position_id", PyValue::None);
    m.insert("simulated", PyValue::Bool(simulated));
    m.insert(extra.0, PyValue::Int(extra.1));
    PyValue::Map(m)
}

fn exec_error_response(e: &ExecError, settings: &Settings, extra: (&str, i64)) -> Response<Body> {
    match e {
        ExecError::Http(h) => HttpError::new(h.status, h.detail.clone()).response(),
        ExecError::Gateway(g) if g.python_type == "UntouchableInstrumentError" => json_response(
            200,
            &refused_json(&g.message, swing_gates(settings).dry_run, extra),
        ),
        _ => server_error(),
    }
}

async fn swing_execute(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let plan_id = form.str("plan_id");
    let line_id = form.int("line_id");
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    let settings = state.settings_snapshot();
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let line = match store.line(line_id).await {
        Ok(l) => l,
        Err(_) => return server_error(),
    };
    if let Some(l) = &line
        && !EXECUTABLE_KINDS.contains(&l.kind.as_str())
    {
        return HttpError::new(
            400,
            format!(
                "A {} line cannot be executed \u{2014} it is information only; the SIGNAL plan \
                 at range close is the line.",
                l.kind
            ),
        )
        .response();
    }
    let price = match &line {
        Some(l) => last_price(&state, &l.symbol).await,
        None => None,
    };
    let Some(gw) = swing_gateway(&state).await else {
        return server_error();
    };
    let orders = order_source(&state);
    let timer = TokioTimer;
    let cx = ExecContext {
        settings: &settings,
        orders: orders.as_ref().map(|o| o as &dyn OrderSource),
        timer: &timer,
    };
    let now = state.clock.now_ist();
    match execute_line(
        &mut store,
        gw.as_ref(),
        &cx,
        &plan_id,
        line_id,
        &confirm,
        now,
        price,
    )
    .await
    {
        Ok(outcome) => json_response(200, &outcome_json(&outcome, ("line_id", line_id))),
        Err(e) => exec_error_response(&e, &settings, ("line_id", line_id)),
    }
}

async fn swing_scan(State(state): State<AppState>) -> Response<Body> {
    let now = state.clock.now_ist();
    let settings = state.settings_snapshot();
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    match store
        .request_scan(
            now,
            Duration::seconds(settings.swing_scan_min_interval_seconds),
            Duration::seconds(settings.swing_scan_stale_after_seconds),
            "desk",
        )
        .await
    {
        Ok(Ok(run_id)) => json_response(
            200,
            &PyValue::Map(jsonable_rec(&Rec::of([
                ("run_id", Val::Int(run_id)),
                ("status", Val::from("QUEUED")),
                ("requested_at", Val::Stamp(now)),
            ]))),
        ),
        Ok(Err(refused)) => {
            let mut err = HttpError::new(refused.status, refused.reason);
            if let Some(after) = refused.retry_after.filter(|a| *a != 0) {
                err.headers
                    .push(("Retry-After".to_owned(), after.to_string()));
            }
            err.response()
        }
        Err(_) => server_error(),
    }
}

async fn swing_reconcile(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    if confirm != "true" {
        return HttpError::new(400, "Reconciling fills requires explicit confirmation.").response();
    }
    let settings = state.settings_snapshot();
    let source = order_source(&state);
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let today = state.clock.now_ist().date_naive();
    let Ok(lines) = store.sent_buy_lines(today).await else {
        return server_error();
    };
    let mut applied: Vec<PyValue> = Vec::new();
    for line in lines {
        // `if source is None or not line["journal_ref"]: continue` — and Python builds the
        // gateway (with its risk_ledger row) only inside the loop, for a line it applies.
        let Some(source) = source.as_ref() else {
            continue;
        };
        let Some(order_id) = line.journal_ref.clone().filter(|r| !r.is_empty()) else {
            continue;
        };
        let seen = source.order_status(&order_id).await;
        let Some(gw) = swing_gateway(&state).await else {
            return server_error();
        };
        let update = OrderUpdate {
            order_id,
            status: seen.status,
            filled_quantity: seen.filled_quantity,
            average_price: seen.average_price,
        };
        let now = state.clock.now_ist();
        match on_order_update(&mut store, gw.as_ref(), &settings, &update, now).await {
            Ok(Some(outcome)) => applied.push(outcome_json(&outcome, ("line_id", line.id))),
            Ok(None) => {}
            // Python's reconcile catches nothing: an HTTPException keeps its status, anything
            // else (an untouchable instrument included) is FastAPI's 500.
            Err(ExecError::Http(h)) => return HttpError::new(h.status, h.detail).response(),
            Err(_) => return server_error(),
        }
    }
    let count = i64::try_from(applied.len()).unwrap_or(0);
    let mut m = PyDict::new();
    m.insert("reconciled", PyValue::List(applied));
    m.insert("count", PyValue::Int(count));
    json_response(200, &PyValue::Map(m))
}

async fn swing_cutoff(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    if confirm != "true" {
        return HttpError::new(400, "The 10:45 sweep requires explicit confirmation.").response();
    }
    let settings = state.settings_snapshot();
    let source = order_source(&state);
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let Some(gw) = swing_gateway(&state).await else {
        return server_error();
    };
    let now = state.clock.now_ist();
    match cutoff_open_orders(
        &mut store,
        gw.as_ref(),
        &settings,
        source.as_ref().map(|o| o as &dyn OrderSource),
        now,
    )
    .await
    {
        Ok(report) => {
            let mut m = PyDict::new();
            m.insert("reconciled", PyValue::Int(report.reconciled));
            m.insert("cancelled", PyValue::Int(report.cancelled));
            m.insert("cancel_failed", PyValue::Int(report.cancel_failed));
            m.insert("slots_freed", PyValue::Int(report.slots_freed));
            m.insert(
                "outcomes",
                PyValue::List(report.outcomes.iter().map(outcome_json_plain).collect()),
            );
            json_response(200, &PyValue::Map(m))
        }
        Err(ExecError::Http(h)) => HttpError::new(h.status, h.detail).response(),
        Err(_) => server_error(),
    }
}

async fn swing_rearm(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let position_id = form.int("position_id");
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    let settings = state.settings_snapshot();
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let pos = match store.position(position_id).await {
        Ok(p) => p,
        Err(_) => return server_error(),
    };
    let price = match &pos {
        Some(p) => last_price(&state, &p.symbol).await,
        None => None,
    };
    let Some(gw) = swing_gateway(&state).await else {
        return server_error();
    };
    let now = state.clock.now_ist();
    match rearm_gtt(
        &mut store,
        gw.as_ref(),
        &settings,
        position_id,
        &confirm,
        now,
        price,
    )
    .await
    {
        Ok(outcome) => json_response(200, &outcome_json(&outcome, ("position_id", position_id))),
        Err(e) => exec_error_response(&e, &settings, ("position_id", position_id)),
    }
}

/// The swing desk's routes (`app.swing_desk.router`).
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/swing", get(swing_page))
        .route("/swing/data", get(swing_data))
        .route("/swing/execute", post(swing_execute))
        .route("/swing/scan", post(swing_scan))
        .route("/swing/reconcile", post(swing_reconcile))
        .route("/swing/cutoff", post(swing_cutoff))
        .route("/swing/rearm", post(swing_rearm))
}

#[cfg(test)]
mod scenario;
#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_catalyst;
#[cfg(test)]
mod tests_routes;
#[cfg(test)]
mod tests_store;
