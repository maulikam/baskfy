//! `app/twt_execute.py` — one confirmed TWT line becomes an order, a stop, a raised stop, a sale
//! or a refusal with its reason. Written against [`TwtStore`] and **handed** its gateway, so the
//! same code runs over the desk's Postgres and over a test's memory store, and a test can prove
//! the path with a gateway that refuses on purpose.
//!
//! The rules it enforces are the module docstring's: never auto-execute (400/404/410/409 before
//! anything is read from a broker; `twt-auto` is the second named exception and calls this same
//! function); every buy gets its GTT in the same request as the fill (non-negotiable 4); a stop
//! never falls; CNC only ([`twt_gates`] hands the gateway intraday and options **off**,
//! whatever the weekly desk may do); everything through the gateway with
//! `client_id = plan_id:symbol` (the line's own) and `…:kind` for GTT legs; it never sells what
//! it did not buy (every position is a `tw_position` row); Track B — with
//! `BASKFY_TWT_EXECUTION_ENABLED=false` the same path runs through the gateway's dry-run branch.

use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use super::common::{
    HttpError, Rec, Val, ddiv, dfloat, dmul, dstr, protection_unresolved, py_isoformat, py_map,
    quantize, refusal, res_get, res_str, res_str_none, tenant, to_ist,
};
use super::store::{LineUpdate, SessionBump, SleeveMoney, StoreError, TwtStore, mode};
use crate::gateway::{
    DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder, ProductGates, StopBand,
    TenantIds,
};
use crate::settings::Settings;
use crate::wire::{PyDict, PyValue};

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::twt::config::{SizingConfig, TICK_INR, TwtConfig};
use baskfy_core::twt::sizing::SizeRefusal;

/// `LineKind.BUY_AT_OPEN`.
pub const BUY_AT_OPEN: &str = "BUY_AT_OPEN";
/// `LineKind.ARM_GTT`.
pub const ARM_GTT: &str = "ARM_GTT";
/// `LineKind.RAISE_GTT_STOP`.
pub const RAISE_GTT_STOP: &str = "RAISE_GTT_STOP";
/// `LineKind.SELL_AT_OPEN`.
pub const SELL_AT_OPEN: &str = "SELL_AT_OPEN";

/// `EXECUTABLE_KINDS` — four since LV9 (TW20 added `SELL_AT_OPEN`).
pub const EXECUTABLE_KINDS: [&str; 4] = [BUY_AT_OPEN, ARM_GTT, RAISE_GTT_STOP, SELL_AT_OPEN];

/// `PLACED_STATUSES`.
pub const PLACED_STATUSES: [&str; 2] = ["PLACED", "DUPLICATE"];
/// `DRY_RUN_STATUSES`.
pub const DRY_RUN_STATUSES: [&str; 1] = ["DRY_RUN"];
/// `ORDER_COMPLETE`.
pub const ORDER_COMPLETE: &str = "COMPLETE";
/// `ORDER_DEAD_STATUSES`.
pub const ORDER_DEAD_STATUSES: [&str; 3] = ["REJECTED", "CANCELLED", "CANCELLED AMO"];

/// `TWT_MARKET_PROTECTION`: Kite's own auto band (LV2.3).
pub const TWT_MARKET_PROTECTION: f64 = -1.0;

/// `TWT_JOURNAL_NAME`: this sleeve's own journal, beside the desk's.
pub const TWT_JOURNAL_NAME: &str = "twt_orders_journal.jsonl";

/// `SIMULATED_GTT_PREFIX`.
pub const SIMULATED_GTT_PREFIX: &str = "DRY-";

/// `HALT_CHANGED_BY`.
pub const HALT_CHANGED_BY: &str = "twt-halt";

/// `GTT_PLACED_STATUSES`.
pub const GTT_PLACED_STATUSES: [&str; 2] = ["GTT_PLACED", "DRY_RUN_GTT"];
/// `GTT_DELETED_STATUSES`.
pub const GTT_DELETED_STATUSES: [&str; 2] = ["GTT_DELETED", "DRY_RUN_GTT_DELETE"];
/// `GTT_MODIFIED_STATUSES`.
pub const GTT_MODIFIED_STATUSES: [&str; 2] = ["GTT_MODIFIED", "DRY_RUN_GTT_MODIFY"];

/// `twt_gates()`: real only when the desk is out of `DRY_RUN` **and** the sleeve's flag is on;
/// intraday and options **false unconditionally** (CNC only, non-negotiable 5).
#[must_use]
pub const fn twt_gates(s: &Settings) -> ProductGates {
    ProductGates {
        dry_run: s.dry_run || !s.twt_execution_enabled,
        intraday_enabled: false,
        options_enabled: false,
        fno_carry_enabled: false,
    }
}

/// `twt_stop_band()`: `04` §10.7, 0.5–30 % below the last price, off the engine's config.
///
/// # Errors
/// A config whose band is out of range (it is not).
pub fn twt_stop_band() -> Result<StopBand, crate::gateway::ClientIdError> {
    let e = TwtConfig::default().exits;
    StopBand::new(dfloat(e.gtt_band_min_pct), dfloat(e.gtt_band_max_pct))
}

/// `twt_limit_fraction()`: where the GTT rests its LIMIT leg (0.97).
#[must_use]
pub fn twt_limit_fraction() -> f64 {
    dfloat(TwtConfig::default().exits.gtt_limit_fraction)
}

/// `is_simulated()`.
#[must_use]
pub const fn is_simulated(s: &Settings) -> bool {
    twt_gates(s).dry_run
}

/// What one confirmed line came to (`ExecOutcome`).
#[derive(Debug, Clone, PartialEq)]
pub struct ExecOutcome {
    /// `SIMULATED` / `SENT` / `FILLED` / `BLOCKED` / `REJECTED`.
    pub status: String,
    /// The words.
    pub reason: String,
    /// The gateway's order result.
    pub order: Option<PyDict>,
    /// The gateway's GTT result.
    pub gtt: Option<PyDict>,
    /// `tw_order.id`.
    pub order_row_id: Option<i64>,
    /// `tw_position.id`.
    pub position_id: Option<i64>,
    /// Journalled `simulated`.
    pub simulated: bool,
    /// The position named in `reason` holds shares with no resting stop.
    pub naked: bool,
}

impl ExecOutcome {
    #[allow(clippy::too_many_arguments)]
    fn new(
        status: &str,
        reason: impl Into<String>,
        order: Option<PyDict>,
        gtt: Option<PyDict>,
        order_row_id: Option<i64>,
        position_id: Option<i64>,
        simulated: bool,
        naked: bool,
    ) -> Self {
        Self {
            status: status.to_owned(),
            reason: reason.into(),
            order,
            gtt,
            order_row_id,
            position_id,
            simulated,
            naked,
        }
    }

    /// `dataclasses.asdict(outcome)`.
    #[must_use]
    pub fn as_rec(&self) -> Rec {
        Rec::of([
            ("status", Val::Str(self.status.clone())),
            ("reason", Val::Str(self.reason.clone())),
            ("order", self.order.as_ref().map_or(Val::None, py_map)),
            ("gtt", self.gtt.as_ref().map_or(Val::None, py_map)),
            ("order_row_id", Val::from(self.order_row_id)),
            ("position_id", Val::from(self.position_id)),
            ("simulated", Val::Bool(self.simulated)),
            ("naked", Val::Bool(self.naked)),
        ])
    }
}

fn blocked(
    reason: impl Into<String>,
    simulated: bool,
    position_id: Option<i64>,
    naked: bool,
) -> ExecOutcome {
    ExecOutcome::new(
        "BLOCKED",
        reason,
        None,
        None,
        None,
        position_id,
        simulated,
        naked,
    )
}

/// Why a confirm did not produce an outcome: an `HTTPException`, an exception the gateway
/// raised (`UntouchableInstrumentError` above all), or the store.
#[derive(Debug, thiserror::Error)]
pub enum ExecError {
    /// `HTTPException(status, detail)`.
    #[error("{} {}", .0.status, .0.detail)]
    Http(HttpError),
    /// The gateway raised.
    #[error(transparent)]
    Gateway(GatewayError),
    /// The store (or a value Python would raise on).
    #[error(transparent)]
    Store(#[from] StoreError),
}

impl ExecError {
    /// Did a guard refuse an untouchable instrument (non-negotiable 7)?
    #[must_use]
    pub fn is_untouchable(&self) -> bool {
        matches!(self, Self::Gateway(e) if e.python_type == "UntouchableInstrumentError")
    }
}

impl From<GatewayError> for ExecError {
    fn from(e: GatewayError) -> Self {
        Self::Gateway(e)
    }
}

/// What the confirm functions return.
pub type ExecResult<T> = Result<T, ExecError>;

fn http(status: u16, detail: impl Into<String>) -> ExecError {
    ExecError::Http(HttpError::new(status, detail))
}

fn bad(what: impl Into<String>) -> ExecError {
    ExecError::Store(StoreError::Bad(what.into()))
}

fn tenant_of(settings: &Settings) -> ExecResult<TenantIds> {
    tenant(settings).map_err(|e| {
        ExecError::Gateway(GatewayError {
            python_type: "ValueError".to_owned(),
            message: e.0,
        })
    })
}

/// `_price(value)`.
fn price(row: &Rec, key: &str) -> ExecResult<Decimal> {
    row.dec(key).ok_or_else(|| {
        bad(format!(
            "decimal.InvalidOperation: [<class 'decimal.ConversionSyntax'>] ({key} is {})",
            row.s(key)
        ))
    })
}

/// `_blocked_reason(result, what=...)`.
#[must_use]
pub fn blocked_reason(result: &PyDict, what: &str) -> String {
    let status = result
        .get("status")
        .map_or_else(|| "?".to_owned(), PyValue::py_str);
    if status == "DUPLICATE" {
        let reference = match result.get("order_id") {
            Some(v) => v.py_str(),
            None => res_str_none(result, "gtt_id"),
        };
        return format!("{what} already sent for this plan ({status}: {reference})");
    }
    let error = match result.get("error") {
        Some(v) if v.truthy() => v.py_str(),
        _ => status.clone(),
    };
    format!("{what} refused by the gateway ({status}): {error}")
}

/// `is_simulated_gtt(gtt_id)`.
#[must_use]
pub fn is_simulated_gtt(gtt_id: &Val) -> bool {
    gtt_id.py_str().starts_with(SIMULATED_GTT_PREFIX)
}

/// `_broker_gtt_id(gtt_id)`: the exchange's integer id, or `None` for a simulated/absent one.
#[must_use]
pub fn broker_gtt_id(gtt_id: &Val) -> Option<i64> {
    if gtt_id.is_none() || is_simulated_gtt(gtt_id) {
        return None;
    }
    match gtt_id {
        Val::Int(i) => Some(*i),
        Val::Str(s) => s.trim().parse().ok(),
        other => other.py_str().trim().parse().ok(),
    }
}

/// `session_day(now)`.
#[must_use]
pub fn session_day(now: &DateTime<FixedOffset>) -> NaiveDate {
    to_ist(now).date_naive()
}

fn tick() -> Decimal {
    super::common::py_decimal(TICK_INR).unwrap_or_default()
}

/// `resting_trigger(position)`: the greater of `stop_price` and the resting `gtt_trigger`.
///
/// # Errors
/// A position with no stop.
pub fn resting_trigger(position: &Rec) -> ExecResult<Decimal> {
    let stop = price(position, "stop_price")?;
    if position.is_none("gtt_trigger") {
        return Ok(stop);
    }
    let trigger = price(position, "gtt_trigger")?;
    Ok(if trigger > stop { trigger } else { stop })
}

fn status_of(result: &PyDict) -> String {
    res_str(result, "status")
}

fn in_set(status: &str, set: &[&str]) -> bool {
    set.contains(&status)
}

// --- the gateway calls ----------------------------------------------------------------------

/// The parts of one GTT arm (`_arm`'s keyword arguments).
struct Arm<'a> {
    symbol: &'a str,
    qty: i64,
    trigger: Decimal,
    last_price: Decimal,
    client_id: String,
}

async fn arm(gateway: &dyn Gateway, settings: &Settings, a: Arm<'_>) -> ExecResult<PyDict> {
    let t = tenant_of(settings)?;
    let mut result = gateway
        .place_gtt_stop(GttStop {
            symbol: a.symbol.to_owned(),
            qty: a.qty,
            trigger: dfloat(a.trigger),
            last_price: dfloat(a.last_price),
            exchange: "NSE".to_owned(),
            series: None,
            client_id: Some(a.client_id.clone()),
            tenant: Some(t),
            plan_tenant: Some(t),
            limit_fraction: Some(twt_limit_fraction()),
            fo_plan: None,
        })
        .await?;
    if result.get("client_id").is_none() {
        result.insert("client_id", PyValue::Str(a.client_id));
    }
    Ok(result)
}

async fn cancel(
    gateway: &dyn Gateway,
    settings: &Settings,
    gtt_id: &Val,
    symbol: &str,
    client_id: String,
) -> ExecResult<Option<PyDict>> {
    if gtt_id.is_none() {
        return Ok(None);
    }
    let Some(broker_id) = broker_gtt_id(gtt_id) else {
        let mut d = PyDict::new();
        d.insert("symbol", PyValue::Str(symbol.to_owned()));
        d.insert("gtt_id", val_py(gtt_id));
        d.insert("status", PyValue::Str("DRY_RUN_GTT_DELETE".to_owned()));
        d.insert("simulated", PyValue::Bool(true));
        return Ok(Some(d));
    };
    let t = tenant_of(settings)?;
    Ok(Some(
        gateway
            .delete_gtt(DeleteGtt {
                gtt_id: broker_id,
                symbol: symbol.to_owned(),
                exchange: "NSE".to_owned(),
                series: None,
                client_id: Some(client_id),
                tenant: Some(t),
                plan_tenant: Some(t),
            })
            .await?,
    ))
}

fn val_py(v: &Val) -> PyValue {
    super::common::jsonable(v)
}

/// `_gtt_fields(gtt, trigger=..., now=...)`.
fn gtt_fields(gtt: &PyDict, trigger: Decimal, now: &DateTime<FixedOffset>) -> Rec {
    let status = res_str_none(gtt, "status");
    let gtt_id = match gtt.get("gtt_id") {
        Some(id) if status == "GTT_PLACED" && !matches!(id, PyValue::None) => id.py_str(),
        _ => {
            let who = match gtt.get("client_id") {
                Some(v) if v.truthy() => v.py_str(),
                _ => res_str_none(gtt, "symbol"),
            };
            format!("{SIMULATED_GTT_PREFIX}{who}")
        }
    };
    Rec::of([
        ("gtt_id", Val::Str(gtt_id)),
        ("gtt_trigger", Val::Dec(trigger)),
        ("gtt_armed_at", Val::Stamp(*now)),
    ])
}

async fn resize_gtt(
    gateway: &dyn Gateway,
    settings: &Settings,
    gtt_id: &Val,
    symbol: &str,
    qty: i64,
    trigger: Decimal,
    last_price: Decimal,
) -> ExecResult<PyDict> {
    let Some(broker_id) = broker_gtt_id(gtt_id) else {
        let mut d = PyDict::new();
        d.insert("status", PyValue::Str("DRY_RUN_GTT_MODIFY".to_owned()));
        d.insert("gtt_id", val_py(gtt_id));
        d.insert("symbol", PyValue::Str(symbol.to_owned()));
        d.insert("qty", PyValue::Int(qty));
        return Ok(d);
    };
    let t = tenant_of(settings)?;
    Ok(gateway
        .modify_gtt_quantity(ModifyGtt {
            gtt_id: broker_id,
            symbol: symbol.to_owned(),
            qty,
            trigger: dfloat(trigger),
            last_price: dfloat(last_price),
            exchange: "NSE".to_owned(),
            series: None,
            client_id: Some(format!("resize:{}:{symbol}:{qty}", gtt_id.py_str())),
            tenant: Some(t),
            plan_tenant: Some(t),
            limit_fraction: Some(twt_limit_fraction()),
            fo_plan: None,
            floor_trigger: None,
        })
        .await?)
}

// --- validation -------------------------------------------------------------------------------

async fn validate(
    store: &mut dyn TwtStore,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    now: &DateTime<FixedOffset>,
) -> ExecResult<(Rec, Rec)> {
    if confirm.to_lowercase() != "true" {
        return Err(http(
            400,
            "confirm=true is required \u{2014} nothing fires without it",
        ));
    }
    let Some(plan) = store.plan(plan_id).await? else {
        return Err(http(404, format!("no plan {plan_id}")));
    };
    if let Some(expires) = plan.stamp("expires_at")
        && *now >= expires
    {
        return Err(http(
            410,
            format!(
                "plan {plan_id} expired at {}; rebuild it and confirm again \u{2014} a plan is \
                 good for thirty minutes and no way round that is a feature",
                py_isoformat(&expires, 'T')
            ),
        ));
    }
    let line = store.line(line_id).await?;
    let Some(line) = line.filter(|l| l.s("plan_id") == plan_id) else {
        return Err(http(404, format!("no line {line_id} on plan {plan_id}")));
    };
    let kind = line.s("kind");
    if !EXECUTABLE_KINDS.contains(&kind.as_str()) {
        return Err(http(
            400,
            format!("a {kind} line cannot be executed on this sleeve"),
        ));
    }
    let state = line.s("state");
    if state != "PROPOSED" {
        return Err(http(
            409,
            format!(
                "line {line_id} is already {state} \u{2014} a line is confirmed once, and \
                 client_id {} would be the gateway's second refusal",
                line.s("client_id")
            ),
        ));
    }
    Ok((line, plan))
}

// --- BUY_AT_OPEN ------------------------------------------------------------------------------

fn refusal_code(r: SizeRefusal) -> &'static str {
    match r {
        SizeRefusal::NoSleeveCapital => "NO_SLEEVE_CAPITAL",
        SizeRefusal::StopNotBelowEntry => "STOP_NOT_BELOW_ENTRY",
        SizeRefusal::BelowMinTradeValue => "BELOW_MIN_TRADE_VALUE",
    }
}

/// `_resize`: re-derive the line's size under the lock — qty, value, stop, note — or the refusal.
fn resize(
    line: &Rec,
    money: &SleeveMoney,
    entry_price: Decimal,
    turnover_avg_inr: Option<Decimal>,
    gates: &ProductGates,
) -> ExecResult<Result<(i64, Decimal, Decimal, String), String>> {
    let config = TwtConfig::default();
    let mut exits = config.exits.clone();
    exits.stop_pct = money.stop_pct;
    exits.trail_pct = money.trail_pct;
    let sizing = SizingConfig {
        max_position_pct: money.max_position_pct,
        ..config.sizing
    };
    let core = |e: baskfy_core::CoreError| bad(e.to_string());
    let stop = baskfy_core::twt::exits::initial_stop(entry_price, &exits, tick()).map_err(core)?;
    if stop >= entry_price {
        return Ok(Err(
            "STOP_NOT_BELOW_ENTRY: the stop is not below the entry".to_owned()
        ));
    }
    let multiplier = baskfy_core::twt::sizing::first_live_multiplier(
        money.first_live_entries_left,
        !gates.dry_run,
        &sizing,
    );
    let sized = baskfy_core::twt::sizing::size_entry(
        money.equity_inr,
        money.cash_available_inr,
        entry_price,
        stop,
        turnover_avg_inr,
        &sizing,
        multiplier,
    )
    .map_err(core)?;
    if !sized.placed() {
        let code = refusal_code(sized.refusal.unwrap_or(SizeRefusal::BelowMinTradeValue));
        return Ok(Err(format!(
            "{code}: the rules cannot line this name at the confirm"
        )));
    }
    let planned = line.int("quantity");
    let mut quantity = sized.quantity.min(planned);
    let mut note = if quantity == planned {
        String::new()
    } else {
        "re-sized down at the confirm".to_owned()
    };
    let headroom = money.equity_inr - money.open_exposure_inr;
    if dmul(entry_price, Decimal::from(quantity)) > headroom {
        quantity = if entry_price > Decimal::ZERO {
            WideDecimal::from(headroom)
                .div(&WideDecimal::from(entry_price))
                .and_then(|q| q.quantize(0, Rounding::Down))
                .and_then(|q| q.try_to_decimal())
                .ok()
                .and_then(|q| i64::try_from(q).ok())
                .unwrap_or(0)
        } else {
            0
        };
        note = "shrunk to the exposure ceiling's headroom".to_owned();
    }
    if quantity <= 0 {
        return Ok(Err(
            "EXPOSURE_FULL: the sleeve's own cash is fully committed".to_owned(),
        ));
    }
    let value = quantize(dmul(entry_price, Decimal::from(quantity)), -2);
    if value < sizing.min_trade_value_inr {
        return Ok(Err(
            "BELOW_MIN_TRADE_VALUE: what is left is below the minimum trade".to_owned(),
        ));
    }
    Ok(Ok((quantity, value, stop, note)))
}

/// `_held_refusal`: LV10 — a fresh signal in a held name is a new entry up to the cap.
fn held_refusal(open: &[Rec], instrument_id: i64, sizing: &SizingConfig) -> Option<String> {
    let held = i64::try_from(
        open.iter()
            .filter(|r| r.int("instrument_id") == instrument_id && r.int("quantity_open") > 0)
            .count(),
    )
    .unwrap_or(0);
    if held > 0 && !sizing.pyramiding {
        return Some(
            "ALREADY_HELD: the sleeve holds this name; the book never averages down".to_owned(),
        );
    }
    let cap = sizing.max_entries_per_name;
    if held >= cap {
        return Some(format!(
            "ALREADY_HELD: {held} open entries in this name already; {cap} is the cap"
        ));
    }
    None
}

/// `naked_positions(store)`: every OPEN position with shares and no resting `gtt_id`.
///
/// # Errors
/// The store.
pub async fn naked_positions(store: &mut dyn TwtStore) -> ExecResult<Vec<Rec>> {
    Ok(store
        .open_positions()
        .await?
        .into_iter()
        .filter(|r| r.is_none("gtt_id") && r.int("quantity_open") > 0)
        .collect())
}

async fn reject(store: &mut dyn TwtStore, line: &Rec, reason: &str) -> ExecResult<()> {
    store
        .set_line(line.int("id"), "REJECTED", LineUpdate::note(reason))
        .await?;
    Ok(())
}

/// The context every handler needs.
struct Ctx<'a> {
    gateway: &'a dyn Gateway,
    settings: &'a Settings,
    gates: ProductGates,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
}

async fn buy_at_open(
    store: &mut dyn TwtStore,
    c: &Ctx<'_>,
    line: &Rec,
    plan: &Rec,
) -> ExecResult<ExecOutcome> {
    let sim = c.gates.dry_run;
    let day = plan
        .date("session_date")
        .ok_or_else(|| bad("plan has no session_date"))?;
    let cap = TwtConfig::default().sizing.max_new_entries_per_session;
    let taken = store.entries_taken(session_day(&c.now)).await?;
    if taken >= cap {
        let reason =
            format!("SESSION_CAP: {taken} entries already taken this session; {cap} is the cap");
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, None, false));
    }
    let open = store.open_positions().await?;
    if let Some(held) = held_refusal(
        &open,
        line.int("instrument_id"),
        &TwtConfig::default().sizing,
    ) {
        reject(store, line, &held).await?;
        return Ok(blocked(held, sim, None, false));
    }
    let naked = naked_positions(store).await?;
    let issues = store.open_protection_issues().await?;
    let unresolved = protection_unresolved(&naked, &issues);
    if !unresolved.is_empty() {
        let reason = refusal(&unresolved);
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, None, false));
    }
    let money = store.sleeve_money(day).await?;
    if money.equity_inr <= Decimal::ZERO {
        let reason = "NO_SLEEVE_CAPITAL: the sleeve has no capital set";
        reject(store, line, reason).await?;
        return Ok(blocked(reason, sim, None, false));
    }
    let planned = line.int("quantity");
    let preview = if planned != 0 {
        ddiv(price(line, "value_inr")?, Decimal::from(planned)).map_err(bad)?
    } else {
        Decimal::ZERO
    };
    let reference = c.last_price.unwrap_or(preview);
    if reference <= Decimal::ZERO {
        let reason = "no price to size or value this market buy against";
        reject(store, line, reason).await?;
        return Ok(blocked(reason, sim, None, false));
    }
    let (quantity, value, stop, note) = match resize(
        line,
        &money,
        reference,
        line.dec("turnover_avg_inr"),
        &c.gates,
    )? {
        Ok(sized) => sized,
        Err(refused) => {
            reject(store, line, &refused).await?;
            return Ok(blocked(refused, sim, None, false));
        }
    };
    let t = tenant_of(c.settings)?;
    let client_id = line.opt_str("client_id");
    let result = c
        .gateway
        .place(PlaceOrder {
            symbol: line.s("symbol"),
            qty: quantity,
            side: "BUY".to_owned(),
            product: "CNC".to_owned(),
            order_type: "MARKET".to_owned(),
            market_protection: Some(TWT_MARKET_PROTECTION),
            client_id: client_id.clone(),
            reference_price: Some(dfloat(reference)),
            gross_exposure: Some(dfloat(value)),
            tenant: Some(t),
            plan_tenant: Some(t),
            ..PlaceOrder::default()
        })
        .await?;
    let status = status_of(&result);
    if !in_set(&status, &PLACED_STATUSES) && !in_set(&status, &DRY_RUN_STATUSES) {
        let reason = match result.get("error") {
            Some(e) if e.truthy() => e.py_str(),
            _ => format!("the gateway answered {status}"),
        };
        reject(store, line, &reason).await?;
        let kind = if status.ends_with("BLOCKED") || status == "DUPLICATE" {
            "BLOCKED"
        } else {
            "REJECTED"
        };
        return Ok(ExecOutcome::new(
            kind,
            reason,
            Some(result),
            None,
            None,
            None,
            sim,
            false,
        ));
    }
    let order_row = store
        .create_order(Rec::of([
            ("instrument_id", Val::Int(line.int("instrument_id"))),
            ("signal_date", Val::Date(day)),
            ("side", Val::from("BUY")),
            ("quantity", Val::Int(quantity)),
            ("stop_price", Val::Dec(stop)),
            ("state", Val::from("SENT")),
            ("broker_order_id", res_get(&result, "order_id")),
            ("client_id", Val::from(client_id)),
            ("simulated", Val::Bool(sim)),
            ("created_at", Val::Stamp(c.now)),
        ]))
        .await?;
    store
        .set_line(
            line.int("id"),
            "SENT",
            LineUpdate {
                journal_ref: Some(res_str(&result, "order_id")),
                order_id: Some(order_row),
                position_id: None,
                note: (!note.is_empty()).then_some(note),
            },
        )
        .await?;
    store
        .bump_session(
            day,
            SessionBump {
                mode: mode(sim),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    if in_set(&status, &DRY_RUN_STATUSES) {
        return apply_fill(
            store,
            c.gateway,
            c.settings,
            Fill {
                line: Some(line),
                plan_day: Some(day),
                order_row,
                quantity,
                fill_price: reference,
                gates: c.gates,
                now: c.now,
                complete: true,
            },
        )
        .await;
    }
    Ok(ExecOutcome::new(
        "SENT",
        "",
        Some(result),
        None,
        Some(order_row),
        None,
        sim,
        false,
    ))
}

/// `_apply_fill`'s arguments.
struct Fill<'a> {
    line: Option<&'a Rec>,
    plan_day: Option<NaiveDate>,
    order_row: i64,
    quantity: i64,
    fill_price: Decimal,
    gates: ProductGates,
    now: DateTime<FixedOffset>,
    complete: bool,
}

/// `_apply_fill`: a filled entry becomes a `tw_position`, a `tw_fill` **and a resting GTT** —
/// one request, deliberately (`04` §7.4).
async fn apply_fill(
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    f: Fill<'_>,
) -> ExecResult<ExecOutcome> {
    let sim = f.gates.dry_run;
    let order = store
        .order(f.order_row)
        .await?
        .ok_or_else(|| bad("AssertionError: the order row is gone"))?;
    let day = f.plan_day.unwrap_or_else(|| session_day(&f.now));
    let symbol = f.line.map_or_else(|| order.s("symbol"), |l| l.s("symbol"));
    let instrument_id = order.int("instrument_id");
    let money = store.sleeve_money(day).await?;
    let mut exits = TwtConfig::default().exits;
    exits.stop_pct = money.stop_pct;
    exits.trail_pct = money.trail_pct;
    let stop = baskfy_core::twt::exits::initial_stop(f.fill_price, &exits, tick())
        .map_err(|e| bad(e.to_string()))?;
    let adj = store.adj_factor(instrument_id, day).await?;
    let position_id = store
        .create_position(Rec::of([
            ("instrument_id", Val::Int(instrument_id)),
            ("order_id", Val::Int(f.order_row)),
            ("signal_date", order.val("signal_date")),
            ("entry_date", Val::Date(day)),
            ("entry_avg", Val::Dec(f.fill_price)),
            ("entry_adj_factor", Val::Dec(adj)),
            ("quantity_entered", Val::Int(f.quantity)),
            ("quantity_open", Val::Int(f.quantity)),
            ("initial_stop", Val::Dec(stop)),
            ("stop_price", Val::Dec(stop)),
            ("high_since", Val::Dec(f.fill_price)),
            ("high_since_date", Val::Date(day)),
            ("state", Val::from("OPEN")),
            ("simulated", Val::Bool(sim)),
        ]))
        .await?;
    store
        .add_fill(Rec::of([
            ("position_id", Val::Int(position_id)),
            ("order_id", Val::Int(f.order_row)),
            ("side", Val::from("BUY")),
            ("quantity", Val::Int(f.quantity)),
            ("price", Val::Dec(f.fill_price)),
            ("filled_at", Val::Stamp(f.now)),
            ("simulated", Val::Bool(sim)),
        ]))
        .await?;
    store
        .update_order(
            f.order_row,
            Rec::of([
                (
                    "state",
                    Val::from(if f.complete { "FILLED" } else { "PARTIAL" }),
                ),
                ("filled_quantity", Val::Int(f.quantity)),
                ("avg_fill_price", Val::Dec(f.fill_price)),
                ("position_id", Val::Int(position_id)),
            ]),
        )
        .await?;
    if let Some(line) = f.line {
        store
            .set_line(
                line.int("id"),
                if f.complete { "FILLED" } else { "SENT" },
                LineUpdate::position(position_id),
            )
            .await?;
    }
    store
        .bump_session(
            day,
            SessionBump {
                mode: mode(sim),
                fills: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    store
        .count_first_live_entry(position_id, day, f.now, !sim)
        .await?;
    let client_id = f.line.map_or_else(
        || format!("fill:{}:{symbol}:{ARM_GTT}", f.order_row),
        |l| format!("{}:{ARM_GTT}", l.s("client_id")),
    );
    let gtt = arm(
        gateway,
        settings,
        Arm {
            symbol: &symbol,
            qty: f.quantity,
            trigger: stop,
            last_price: f.fill_price,
            client_id,
        },
    )
    .await?;
    if in_set(&res_str_none(&gtt, "status"), &GTT_PLACED_STATUSES) {
        store
            .update_position(position_id, gtt_fields(&gtt, stop, &f.now))
            .await?;
        return Ok(ExecOutcome::new(
            if sim { "SIMULATED" } else { "FILLED" },
            "",
            None,
            Some(gtt),
            Some(f.order_row),
            Some(position_id),
            sim,
            false,
        ));
    }
    let reason = format!(
        "{symbol}: the buy filled and the stop was not armed \u{2014} {}; position {position_id} \
         is NAKED, re-arm it",
        blocked_reason(&gtt, "the GTT")
    );
    Ok(ExecOutcome::new(
        "BLOCKED",
        reason,
        None,
        Some(gtt),
        Some(f.order_row),
        Some(position_id),
        sim,
        true,
    ))
}

// --- the position a line acts on ------------------------------------------------------------

async fn position_for_line(store: &mut dyn TwtStore, line: &Rec) -> ExecResult<Option<Rec>> {
    if let Some(position_id) = line.opt_int("position_id")
        && let Some(found) = store.position(position_id).await?
        && found.s("state") == "OPEN"
        && found.int("quantity_open") > 0
    {
        return Ok(Some(found));
    }
    Ok(store.open_position_for(line.int("instrument_id")).await?)
}

/// `_sell_reason(position, quantity)`.
fn sell_reason(position: &Rec, quantity: i64) -> String {
    let open = position.int("quantity_open");
    if !position.is_none("exit_queued_for")
        && position.truthy("exit_reason_queued")
        && quantity >= open
    {
        return position.s("exit_reason_queued");
    }
    if position.truthy("partial_quantity") && position.int("partial_quantity") == quantity {
        return "PARTIAL".to_owned();
    }
    if quantity < open {
        return "PARTIAL".to_owned();
    }
    "MANUAL".to_owned()
}

async fn sell_at_open(
    store: &mut dyn TwtStore,
    c: &Ctx<'_>,
    line: &Rec,
    plan: &Rec,
) -> ExecResult<ExecOutcome> {
    let sim = c.gates.dry_run;
    let Some(position) = position_for_line(store, line).await? else {
        let reason = format!(
            "{} is not in this sleeve's book; this sleeve never sells a holding it did not buy",
            line.s("symbol")
        );
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, None, false));
    };
    let position_id = position.int("id");
    let owned = position.int("quantity_open");
    let quantity = line.int("quantity");
    if quantity <= 0 || quantity > owned {
        let reason = format!("the line sells {quantity} and the sleeve holds {owned}");
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, Some(position_id), false));
    }
    let reason_code = sell_reason(&position, quantity);
    let reference = match c.last_price {
        Some(p) => p,
        None => price(&position, "entry_avg")?,
    };
    let t = tenant_of(c.settings)?;
    let result = c
        .gateway
        .place(PlaceOrder {
            symbol: line.s("symbol"),
            qty: quantity,
            side: "SELL".to_owned(),
            product: "CNC".to_owned(),
            order_type: "MARKET".to_owned(),
            market_protection: Some(TWT_MARKET_PROTECTION),
            client_id: line.opt_str("client_id"),
            reference_price: Some(dfloat(reference)),
            gross_exposure: Some(dfloat(dmul(reference, Decimal::from(quantity)))),
            tenant: Some(t),
            plan_tenant: Some(t),
            ..PlaceOrder::default()
        })
        .await?;
    let status = status_of(&result);
    if !in_set(&status, &PLACED_STATUSES) && !in_set(&status, &DRY_RUN_STATUSES) {
        let reason = match result.get("error") {
            Some(e) if e.truthy() => e.py_str(),
            _ => format!("the gateway answered {status}"),
        };
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, Some(position_id), false));
    }
    let day = plan
        .date("session_date")
        .ok_or_else(|| bad("plan has no session_date"))?;
    store
        .set_line(
            line.int("id"),
            "SENT",
            LineUpdate {
                journal_ref: Some(res_str(&result, "order_id")),
                order_id: None,
                position_id: Some(position_id),
                note: Some(format!(
                    "{}: sell {quantity} of {owned}",
                    reason_code.to_lowercase()
                )),
            },
        )
        .await?;
    store
        .bump_session(
            day,
            SessionBump {
                mode: mode(sim),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    if in_set(&status, &DRY_RUN_STATUSES) {
        return apply_exit_fill(
            store,
            c.gateway,
            c.settings,
            ExitFill {
                position: &position,
                line_id: Some(line.int("id")),
                quantity,
                average: reference,
                reason: &reason_code,
                day,
                simulated: true,
                now: c.now,
            },
        )
        .await;
    }
    store
        .create_exit_order(Rec::of([
            ("sleeve", Val::from("twt")),
            ("position_id", Val::Int(position_id)),
            ("line_id", Val::Int(line.int("id"))),
            ("symbol", Val::Str(line.s("symbol"))),
            ("broker_order_id", res_get(&result, "order_id")),
            ("client_id", line.val("client_id")),
            ("quantity", Val::Int(quantity)),
            ("reference_price", Val::Dec(reference)),
            ("state", Val::from("SENT")),
            ("filled_quantity", Val::Int(0)),
            ("reason", Val::Str(reason_code)),
            ("simulated", Val::Bool(false)),
        ]))
        .await?;
    Ok(ExecOutcome::new(
        "SENT",
        "",
        Some(result),
        None,
        None,
        Some(position_id),
        sim,
        false,
    ))
}

/// `_apply_exit_fill`'s arguments.
struct ExitFill<'a> {
    position: &'a Rec,
    line_id: Option<i64>,
    quantity: i64,
    average: Decimal,
    reason: &'a str,
    day: NaiveDate,
    simulated: bool,
    now: DateTime<FixedOffset>,
}

async fn apply_exit_fill(
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    f: ExitFill<'_>,
) -> ExecResult<ExecOutcome> {
    let position_id = f.position.int("id");
    let symbol = f.position.s("symbol");
    let remaining = (f.position.int("quantity_open") - f.quantity).max(0);
    store
        .add_fill(Rec::of([
            ("position_id", Val::Int(position_id)),
            ("order_id", Val::None),
            ("side", Val::from("SELL")),
            ("quantity", Val::Int(f.quantity)),
            ("price", Val::Dec(f.average)),
            ("filled_at", Val::Stamp(f.now)),
            ("simulated", Val::Bool(f.simulated)),
        ]))
        .await?;
    let mut fields = Rec::of([("quantity_open", Val::Int(remaining))]);
    if f.reason == "PARTIAL" {
        fields.set("partial_done", Val::Bool(true));
        fields.set("partial_queued_for", Val::None);
        fields.set("partial_quantity", Val::None);
    }
    if remaining == 0 {
        let entry = price(f.position, "entry_avg")?;
        let entered = match f.position.int("quantity_entered") {
            0 => f.quantity,
            n => n,
        };
        let pnl = quantize(dmul(f.average - entry, Decimal::from(entered)), -2);
        let ret = if entry > Decimal::ZERO {
            let ratio = ddiv(f.average, entry).map_err(bad)?;
            Val::Dec(quantize(dmul(ratio - Decimal::ONE, Decimal::from(100)), -2))
        } else {
            Val::None
        };
        fields.set("state", Val::from("CLOSED"));
        fields.set("closed_on", Val::Date(f.day));
        fields.set(
            "close_reason",
            Val::from(if f.reason == "MA_TRAIL" || f.reason == "MANUAL" {
                f.reason
            } else {
                "MANUAL"
            }),
        );
        fields.set("exit_avg", Val::Dec(f.average));
        fields.set("pnl_inr", Val::Dec(pnl));
        fields.set("return_pct", ret);
        fields.set("exit_queued_for", Val::None);
        fields.set("exit_reason_queued", Val::None);
    }
    store.update_position(position_id, fields).await?;
    let gtt_id = f.position.val("gtt_id");
    if remaining == 0 {
        let cancelled = cancel(
            gateway,
            settings,
            &gtt_id,
            &symbol,
            format!("exit:{position_id}:{symbol}:cancel"),
        )
        .await?;
        if let Some(c) = &cancelled
            && in_set(&res_str_none(c, "status"), &GTT_DELETED_STATUSES)
        {
            store
                .update_position(
                    position_id,
                    Rec::of([("gtt_id", Val::None), ("gtt_trigger", Val::None)]),
                )
                .await?;
        }
        store
            .bump_session(
                f.day,
                SessionBump {
                    mode: mode(f.simulated),
                    exits: 1,
                    ..SessionBump::default()
                },
            )
            .await?;
    } else if !gtt_id.is_none() {
        let trigger = resting_trigger(f.position)?;
        let _ = resize_gtt(
            gateway, settings, &gtt_id, &symbol, remaining, trigger, f.average,
        )
        .await?;
    }
    if let Some(line_id) = f.line_id {
        store
            .set_line(line_id, "FILLED", LineUpdate::position(position_id))
            .await?;
    }
    Ok(ExecOutcome::new(
        if f.simulated { "SIMULATED" } else { "FILLED" },
        format!(
            "{symbol}: sold {} at {} ({}); {remaining} left",
            f.quantity,
            dstr(f.average),
            f.reason.to_lowercase()
        ),
        None,
        None,
        None,
        Some(position_id),
        f.simulated,
        false,
    ))
}

// --- ARM_GTT --------------------------------------------------------------------------------

async fn arm_gtt_line(
    store: &mut dyn TwtStore,
    c: &Ctx<'_>,
    line: &Rec,
    plan: &Rec,
) -> ExecResult<ExecOutcome> {
    let sim = c.gates.dry_run;
    let symbol = line.s("symbol");
    let Some(position) = store.open_position_for(line.int("instrument_id")).await? else {
        let reason = format!("{symbol} is not in this sleeve's book");
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, None, false));
    };
    let position_id = position.int("id");
    if !position.is_none("gtt_id") {
        let reason = format!(
            "{symbol}: a GTT is already resting ({}); arming a second one would sell twice what \
             is held when they fire",
            position.s("gtt_id")
        );
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, Some(position_id), false));
    }
    let stop = if line.is_none("stop_price") {
        price(&position, "stop_price")?
    } else {
        price(line, "stop_price")?
    };
    let reference = match c.last_price {
        Some(p) => p,
        None => price(&position, "entry_avg")?,
    };
    if stop >= reference {
        let reason = format!(
            "{symbol}: the stop {} is at or above the {} {} \u{2014} it would fire the moment it \
             is armed",
            dstr(stop),
            if c.last_price.is_some() {
                "last price"
            } else {
                "entry"
            },
            dstr(reference)
        );
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, Some(position_id), false));
    }
    let gtt = arm(
        c.gateway,
        c.settings,
        Arm {
            symbol: &symbol,
            qty: position.int("quantity_open"),
            trigger: stop,
            last_price: reference,
            client_id: line.s("client_id"),
        },
    )
    .await?;
    if !in_set(&res_str_none(&gtt, "status"), &GTT_PLACED_STATUSES) {
        let reason = blocked_reason(&gtt, "the GTT");
        reject(store, line, &reason).await?;
        return Ok(ExecOutcome::new(
            "BLOCKED",
            format!("{symbol}: {reason}; position {position_id} is NAKED, re-arm it"),
            None,
            Some(gtt),
            None,
            Some(position_id),
            sim,
            true,
        ));
    }
    store
        .update_position(position_id, gtt_fields(&gtt, stop, &c.now))
        .await?;
    store
        .set_line(line.int("id"), "FILLED", LineUpdate::position(position_id))
        .await?;
    let day = plan
        .date("session_date")
        .ok_or_else(|| bad("plan has no session_date"))?;
    store
        .bump_session(
            day,
            SessionBump {
                mode: mode(sim),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    Ok(ExecOutcome::new(
        if sim { "SIMULATED" } else { "FILLED" },
        "",
        None,
        Some(gtt),
        None,
        Some(position_id),
        sim,
        false,
    ))
}

// --- RAISE_GTT_STOP: the ratchet ------------------------------------------------------------

async fn raise_gtt_stop(
    store: &mut dyn TwtStore,
    c: &Ctx<'_>,
    line: &Rec,
    plan: &Rec,
) -> ExecResult<ExecOutcome> {
    let sim = c.gates.dry_run;
    let symbol = line.s("symbol");
    let Some(position) = position_for_line(store, line).await? else {
        let reason = format!("{symbol} is not in this sleeve's book");
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, None, false));
    };
    let position_id = position.int("id");
    if line.is_none("stop_price") {
        let reason = format!("{symbol}: a RAISE line needs a new trigger");
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, Some(position_id), false));
    }
    let new_trigger = price(line, "stop_price")?;
    let resting = resting_trigger(&position)?;
    if new_trigger <= resting {
        let reason = format!(
            "{symbol}: the new trigger {} is not above the resting stop {} \u{2014} a stop never \
             falls",
            dstr(new_trigger),
            dstr(resting)
        );
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, Some(position_id), false));
    }
    let open_qty = position.int("quantity_open");
    if open_qty <= 0 {
        let reason = format!("{symbol}: nothing is open to protect");
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, Some(position_id), false));
    }
    let Some(last) = c.last_price else {
        let reason = format!(
            "{symbol}: no last price to check the new trigger against \u{2014} a stop armed above \
             the market fires at the next tick"
        );
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, Some(position_id), false));
    };
    if new_trigger >= last {
        let reason = format!(
            "{symbol}: the new trigger {} is at or above the last price {} \u{2014} that would \
             fire at once",
            dstr(new_trigger),
            dstr(last)
        );
        reject(store, line, &reason).await?;
        return Ok(blocked(reason, sim, Some(position_id), false));
    }
    let client_id = line.s("client_id");
    let cancelled = cancel(
        c.gateway,
        c.settings,
        &position.val("gtt_id"),
        &symbol,
        format!("{client_id}:cancel"),
    )
    .await?;
    if let Some(cancel_result) = &cancelled
        && !in_set(
            &res_str_none(cancel_result, "status"),
            &GTT_DELETED_STATUSES,
        )
    {
        let reason = format!(
            "{symbol}: {}; the old stop at {} is still resting and nothing was placed",
            blocked_reason(cancel_result, "cancelling the old GTT"),
            dstr(resting)
        );
        reject(store, line, &reason).await?;
        return Ok(ExecOutcome::new(
            "BLOCKED",
            reason,
            None,
            cancelled,
            None,
            Some(position_id),
            sim,
            false,
        ));
    }
    let gtt = arm(
        c.gateway,
        c.settings,
        Arm {
            symbol: &symbol,
            qty: open_qty,
            trigger: new_trigger,
            last_price: last,
            client_id: client_id.clone(),
        },
    )
    .await?;
    if !in_set(&res_str_none(&gtt, "status"), &GTT_PLACED_STATUSES) {
        store
            .update_position(
                position_id,
                Rec::of([
                    ("stop_price", Val::Dec(new_trigger)),
                    ("gtt_id", Val::None),
                    ("gtt_trigger", Val::None),
                    ("gtt_armed_at", Val::None),
                ]),
            )
            .await?;
        let reason = format!(
            "{symbol}: the old stop was cancelled and the new one was not armed \u{2014} {}; \
             position {position_id} is NAKED, re-arm it",
            blocked_reason(&gtt, "the GTT")
        );
        reject(store, line, &reason).await?;
        return Ok(ExecOutcome::new(
            "BLOCKED",
            reason,
            None,
            Some(gtt),
            None,
            Some(position_id),
            sim,
            true,
        ));
    }
    let mut fields = gtt_fields(&gtt, new_trigger, &c.now);
    fields.set("stop_price", Val::Dec(new_trigger));
    fields.set("next_trigger", Val::None);
    fields.set("next_trigger_for", Val::None);
    store.update_position(position_id, fields).await?;
    store
        .set_line(line.int("id"), "FILLED", LineUpdate::position(position_id))
        .await?;
    let day = plan
        .date("session_date")
        .ok_or_else(|| bad("plan has no session_date"))?;
    store
        .bump_session(
            day,
            SessionBump {
                mode: mode(sim),
                confirms: 1,
                ratchets: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    Ok(ExecOutcome::new(
        if sim { "SIMULATED" } else { "FILLED" },
        "",
        None,
        Some(gtt),
        None,
        Some(position_id),
        sim,
        false,
    ))
}

// --- the entry point --------------------------------------------------------------------------

/// `execute_line(store, gateway, plan_id=..., line_id=..., confirm=..., now=..., last_price=...)`.
///
/// One confirmed line becomes an order, a stop, a raised stop, a sale, or a refusal with its
/// reason. The whole of it runs under the day's `tw_session` row lock (`04` §10.5).
///
/// # Errors
/// [`ExecError::Http`] for 400/404/410/409, the gateway's raise, or the store.
#[allow(clippy::too_many_arguments)]
pub async fn execute_line(
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> ExecResult<ExecOutcome> {
    let gates = twt_gates(settings);
    let (line, _) = validate(store, plan_id, line_id, confirm, &now).await?;
    let plan = store
        .plan(plan_id)
        .await?
        .ok_or_else(|| bad("AssertionError: `_validate` has already refused a missing plan"))?;
    let day = plan
        .date("session_date")
        .ok_or_else(|| bad("plan has no session_date"))?;
    store.lock_session_for_update(day).await?;
    let c = Ctx {
        gateway,
        settings,
        gates,
        now,
        last_price,
    };
    match line.s("kind").as_str() {
        BUY_AT_OPEN => buy_at_open(store, &c, &line, &plan).await,
        ARM_GTT => arm_gtt_line(store, &c, &line, &plan).await,
        SELL_AT_OPEN => sell_at_open(store, &c, &line, &plan).await,
        _ => raise_gtt_stop(store, &c, &line, &plan).await,
    }
}

// --- the four chores the runbook needs --------------------------------------------------------

/// `rearm_gtt(store, gateway, position_id=..., confirm=..., now=..., last_price=...)`: arm a stop
/// for a **naked** position, nothing else. It places no buy and can only add protection.
///
/// # Errors
/// 400 without `confirm`, 404 for an unknown position, the gateway's raise, or the store.
pub async fn rearm_gtt(
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    position_id: i64,
    confirm: &str,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> ExecResult<ExecOutcome> {
    if confirm.to_lowercase() != "true" {
        return Err(http(400, "Arming a stop requires explicit confirmation."));
    }
    let Some(position) = store.position(position_id).await? else {
        return Err(http(404, "Unknown position_id."));
    };
    let sim = twt_gates(settings).dry_run;
    let symbol = position.s("symbol");
    if !position.is_none("gtt_id") {
        return Ok(blocked(
            format!(
                "{symbol}: a GTT is already resting ({}); re-arming would leave two",
                position.s("gtt_id")
            ),
            sim,
            Some(position_id),
            false,
        ));
    }
    let open_qty = position.int("quantity_open");
    if open_qty <= 0 || position.s("state") == "CLOSED" {
        return Ok(blocked(
            format!("{symbol}: nothing is open to protect"),
            sim,
            Some(position_id),
            false,
        ));
    }
    let trigger = resting_trigger(&position)?;
    let reference = match last_price {
        Some(p) => p,
        None => price(&position, "entry_avg")?,
    };
    if trigger >= reference {
        return Ok(blocked(
            format!(
                "{symbol}: the stop {} is at or above the {} {} \u{2014} a GTT that would fire on \
                 the next tick is not protection; decide whether to sell, by hand",
                dstr(trigger),
                if last_price.is_some() {
                    "last price"
                } else {
                    "entry"
                },
                dstr(reference)
            ),
            sim,
            Some(position_id),
            false,
        ));
    }
    let gtt = arm(
        gateway,
        settings,
        Arm {
            symbol: &symbol,
            qty: open_qty,
            trigger,
            last_price: reference,
            client_id: format!(
                "rearm:{position_id}:{symbol}:{ARM_GTT}:{}",
                session_day(&now).format("%Y-%m-%d")
            ),
        },
    )
    .await?;
    if !in_set(&res_str_none(&gtt, "status"), &GTT_PLACED_STATUSES) {
        return Ok(ExecOutcome::new(
            "BLOCKED",
            format!(
                "{symbol}: {}; position {position_id} is still NAKED",
                blocked_reason(&gtt, "the GTT")
            ),
            None,
            Some(gtt),
            None,
            Some(position_id),
            sim,
            true,
        ));
    }
    store
        .update_position(position_id, gtt_fields(&gtt, trigger, &now))
        .await?;
    Ok(ExecOutcome::new(
        if sim { "SIMULATED" } else { "FILLED" },
        "",
        None,
        Some(gtt),
        None,
        Some(position_id),
        sim,
        false,
    ))
}

/// `rearm_callable(...)`'s answer for one position: `RearmOutcome`'s four fields.
///
/// # Errors
/// As [`rearm_gtt`].
pub async fn rearm_one(
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    position_id: i64,
    now: DateTime<FixedOffset>,
    price: Option<Decimal>,
) -> ExecResult<Rec> {
    let outcome = rearm_gtt(store, gateway, settings, position_id, "true", now, price).await?;
    let after = store.position(position_id).await?;
    let gtt_id = after
        .as_ref()
        .and_then(|a| a.opt_str("gtt_id"))
        .map_or(Val::None, Val::Str);
    Ok(Rec::of([
        ("position_id", Val::Int(position_id)),
        (
            "armed",
            Val::Bool(outcome.status == "FILLED" || outcome.status == "SIMULATED"),
        ),
        ("gtt_id", gtt_id),
        ("reason", Val::Str(outcome.reason)),
    ]))
}

/// What the 15:15 chore found (`SweepReport`).
#[derive(Debug, Clone, PartialEq)]
pub struct SweepReport {
    /// `naked_before`.
    pub naked_before: i64,
    /// `rearmed`.
    pub rearmed: i64,
    /// `naked`.
    pub naked: i64,
    /// `outcomes`.
    pub outcomes: Vec<ExecOutcome>,
}

/// `sweep_naked(store, gateway, now=..., prices=...)`: re-arm everything naked and say what is
/// still naked. Idempotent and keyed on the day; places no buy, cancels nothing.
///
/// # Errors
/// The gateway's raise, or the store.
pub async fn sweep_naked(
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    now: DateTime<FixedOffset>,
    prices: &[(String, Decimal)],
) -> ExecResult<SweepReport> {
    let day = session_day(&now);
    let before = naked_positions(store).await?;
    let mut outcomes = Vec::with_capacity(before.len());
    for position in &before {
        let symbol = position.s("symbol");
        let p = prices.iter().find(|(s, _)| *s == symbol).map(|(_, p)| *p);
        outcomes
            .push(rearm_gtt(store, gateway, settings, position.int("id"), "true", now, p).await?);
    }
    let still = naked_positions(store).await?;
    let still_n = i64::try_from(still.len()).unwrap_or(0);
    let before_n = i64::try_from(before.len()).unwrap_or(0);
    store
        .set_naked_count(day, &mode(twt_gates(settings).dry_run), still_n)
        .await?;
    Ok(SweepReport {
        naked_before: before_n,
        rearmed: before_n - still_n,
        naked: still_n,
        outcomes,
    })
}

/// What `/twt/reconcile` attached (`ReconcileReport`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ReconcileReport {
    /// `attached`.
    pub attached: i64,
    /// `unmatched`: resting triggers in names this sleeve holds that landed on no position.
    pub unmatched: i64,
    /// `positions`.
    pub positions: Vec<i64>,
}

/// `reconcile_gtts(store, source, now=...)`: attach a **hand-armed** GTT to the naked position
/// it protects. A read of the broker and a write of our own book; places nothing, cancels
/// nothing, moves no stop. `gtts` is `None` without a source (or when the broker would not
/// answer), each entry `{gtt_id, symbol, trigger, status}`.
///
/// # Errors
/// The store.
pub async fn reconcile_gtts(
    store: &mut dyn TwtStore,
    gtts: Option<Vec<Rec>>,
    now: DateTime<FixedOffset>,
) -> ExecResult<ReconcileReport> {
    let Some(resting) = gtts else {
        return Ok(ReconcileReport::default());
    };
    let mut by_symbol: Vec<(String, Rec)> = Vec::new();
    for trigger in resting {
        let symbol = if trigger.truthy("symbol") {
            trigger.s("symbol")
        } else {
            String::new()
        };
        let status = trigger.val("status");
        let active = status.is_none() || matches!(status.py_str().as_str(), "active" | "ACTIVE");
        if !symbol.is_empty() && active && !by_symbol.iter().any(|(s, _)| *s == symbol) {
            by_symbol.push((symbol, trigger));
        }
    }
    let mut attached = Vec::new();
    for position in naked_positions(store).await? {
        let symbol = position.s("symbol");
        let Some(at) = by_symbol.iter().position(|(s, _)| *s == symbol) else {
            continue;
        };
        let (_, found) = by_symbol.remove(at);
        if found.is_none("gtt_id") {
            continue;
        }
        let level = if found.is_none("trigger") {
            resting_trigger(&position)?
        } else {
            found
                .dec("trigger")
                .ok_or_else(|| bad("trigger is not a number"))?
        };
        store
            .update_position(
                position.int("id"),
                Rec::of([
                    ("gtt_id", Val::Str(found.s("gtt_id"))),
                    ("gtt_trigger", Val::Dec(level)),
                    ("gtt_armed_at", Val::Stamp(now)),
                ]),
            )
            .await?;
        attached.push(position.int("id"));
    }
    let ours: Vec<String> = store
        .open_positions()
        .await?
        .iter()
        .map(|r| r.s("symbol"))
        .collect();
    let unmatched =
        i64::try_from(by_symbol.iter().filter(|(s, _)| ours.contains(s)).count()).unwrap_or(0);
    Ok(ReconcileReport {
        attached: i64::try_from(attached.len()).unwrap_or(0),
        unmatched,
        positions: attached,
    })
}

/// What the stop-the-sleeve command did (`HaltReport`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HaltReport {
    /// `halted`.
    pub halted: bool,
    /// `sleeve_capital_inr_before`.
    pub sleeve_capital_inr_before: Option<Decimal>,
    /// `plans_expired`.
    pub plans_expired: i64,
}

impl HaltReport {
    /// `line()`: one line, because it is read on a phone.
    #[must_use]
    pub fn line(&self) -> String {
        let before = self
            .sleeve_capital_inr_before
            .map_or_else(|| "unset".to_owned(), |d| format!("\u{20b9}{}", dstr(d)));
        format!(
            "TWT halted: sleeve capital {before} -> \u{20b9}0 (audited), {} live plan(s) expired; \
             every resting GTT is untouched and the ratchet still works.",
            self.plans_expired
        )
    }
}

/// `halt_sleeve(store, confirm=..., now=...)`: capital to ₹0 (audited), every live plan expired,
/// **protection never touched** — there is not one line here that could cancel a stop.
///
/// # Errors
/// 400 without `confirm`, or the store.
pub async fn halt_sleeve(
    store: &mut dyn TwtStore,
    confirm: &str,
    now: DateTime<FixedOffset>,
) -> ExecResult<HaltReport> {
    if confirm.to_lowercase() != "true" {
        return Err(http(
            400,
            "Halting the sleeve requires explicit confirmation.",
        ));
    }
    let before = store.zero_capital(now, HALT_CHANGED_BY).await?;
    let expired = store.expire_plans(now).await?;
    Ok(HaltReport {
        halted: true,
        sleeve_capital_inr_before: before,
        plans_expired: expired,
    })
}

// --- fills that arrive later ------------------------------------------------------------------

/// A broker report (`payload`): `order_id`, `status`, `filled_quantity`, `average_price`,
/// `status_message`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct OrderReport {
    /// `order_id`.
    pub order_id: String,
    /// `status`.
    pub status: String,
    /// `filled_quantity`.
    pub filled_quantity: i64,
    /// `average_price` (`None` when absent).
    pub average_price: Option<Decimal>,
    /// `status_message` / `status_message_raw`.
    pub status_message: Option<String>,
}

/// `on_order_update(store, gateway, payload, now=...)`: a broker report on one of this sleeve's
/// orders (LV2). `None` when it is not this sleeve's or says nothing new.
///
/// # Errors
/// The gateway's raise, or the store.
pub async fn on_order_update(
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    payload: &OrderReport,
    now: DateTime<FixedOffset>,
) -> ExecResult<Option<ExecOutcome>> {
    let broker_id = payload.order_id.clone();
    if broker_id.is_empty() {
        return Ok(None);
    }
    let status = payload.status.to_uppercase();
    let filled = payload.filled_quantity;
    let average = payload.average_price.unwrap_or(Decimal::ZERO);
    let day = session_day(&now);
    let gates = twt_gates(settings);
    let Some(order) = store.order_by_broker_id(&broker_id).await? else {
        let Some(exit_order) = store.exit_order_by_broker_id(&broker_id).await? else {
            return Ok(None);
        };
        store.lock_session_for_update(day).await?;
        return apply_exit_update(
            store,
            gateway,
            settings,
            &exit_order,
            &status,
            filled,
            average,
            gates,
            now,
        )
        .await;
    };
    if order.opt_str("side").unwrap_or_else(|| "BUY".to_owned()) != "BUY" {
        return Ok(None);
    }
    if !matches!(order.s("state").as_str(), "SENT" | "PARTIAL") {
        return Ok(None);
    }
    store.lock_session_for_update(day).await?;
    let Some(current) = store.order(order.int("id")).await? else {
        return Ok(None);
    };
    if !matches!(current.s("state").as_str(), "SENT" | "PARTIAL") {
        return Ok(None);
    }
    let recorded = current.int("filled_quantity");
    let position_id = current.opt_int("position_id");
    let complete = status == ORDER_COMPLETE;
    let mut outcome = None;
    if filled > recorded && average > Decimal::ZERO {
        outcome = Some(match position_id {
            None => {
                apply_fill(
                    store,
                    gateway,
                    settings,
                    Fill {
                        line: None,
                        plan_day: None,
                        order_row: current.int("id"),
                        quantity: filled,
                        fill_price: average,
                        gates,
                        now,
                        complete,
                    },
                )
                .await?
            }
            Some(pid) => {
                grow_position(
                    store, gateway, settings, &current, pid, filled, average, gates, now, complete,
                )
                .await?
            }
        });
    }
    if ORDER_DEAD_STATUSES.contains(&status.as_str()) {
        return Ok(Some(
            close_dead_order(store, settings, &current, &status, payload, outcome).await?,
        ));
    }
    if complete
        && outcome.is_none()
        && let Some(pid) = position_id
    {
        store
            .update_order(current.int("id"), Rec::of([("state", Val::from("FILLED"))]))
            .await?;
        if let Some(line_id) = line_for_order(store, &current).await? {
            store
                .set_line(line_id, "FILLED", LineUpdate::position(pid))
                .await?;
        }
        return Ok(Some(ExecOutcome::new(
            "FILLED",
            "",
            None,
            None,
            Some(current.int("id")),
            Some(pid),
            gates.dry_run,
            false,
        )));
    }
    Ok(outcome)
}

async fn line_for_order(store: &mut dyn TwtStore, order: &Rec) -> ExecResult<Option<i64>> {
    if !order.truthy("client_id") {
        return Ok(None);
    }
    Ok(store
        .line_by_client_id(&order.s("client_id"))
        .await?
        .map(|l| l.int("id")))
}

#[allow(clippy::too_many_arguments)]
async fn grow_position(
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    order: &Rec,
    position_id: i64,
    filled: i64,
    average: Decimal,
    gates: ProductGates,
    now: DateTime<FixedOffset>,
    complete: bool,
) -> ExecResult<ExecOutcome> {
    let position = store
        .position(position_id)
        .await?
        .ok_or_else(|| bad("AssertionError: the position is gone"))?;
    let symbol = position.s("symbol");
    let entered = position.int("quantity_entered");
    let open_qty = position.int("quantity_open");
    let delta = filled - entered;
    let order_id = order.int("id");
    let final_state = if complete { "FILLED" } else { "SENT" };
    if delta <= 0 {
        return Ok(ExecOutcome::new(
            final_state,
            "",
            None,
            None,
            Some(order_id),
            Some(position_id),
            gates.dry_run,
            false,
        ));
    }
    let old_avg = price(&position, "entry_avg")?;
    let mut delta_price = quantize(
        ddiv(
            dmul(average, Decimal::from(filled)) - dmul(old_avg, Decimal::from(entered)),
            Decimal::from(delta),
        )
        .map_err(bad)?,
        -4,
    );
    if delta_price <= Decimal::ZERO {
        delta_price = average;
    }
    let new_open = open_qty + delta;
    store
        .add_fill(Rec::of([
            ("position_id", Val::Int(position_id)),
            ("order_id", Val::Int(order_id)),
            ("side", Val::from("BUY")),
            ("quantity", Val::Int(delta)),
            ("price", Val::Dec(delta_price)),
            ("filled_at", Val::Stamp(now)),
            ("simulated", Val::Bool(gates.dry_run)),
        ]))
        .await?;
    store
        .update_position(
            position_id,
            Rec::of([
                ("quantity_entered", Val::Int(filled)),
                ("quantity_open", Val::Int(new_open)),
                ("entry_avg", Val::Dec(average)),
            ]),
        )
        .await?;
    store
        .update_order(
            order_id,
            Rec::of([
                (
                    "state",
                    Val::from(if complete { "FILLED" } else { "PARTIAL" }),
                ),
                ("filled_quantity", Val::Int(filled)),
                ("avg_fill_price", Val::Dec(average)),
            ]),
        )
        .await?;
    if let Some(line_id) = line_for_order(store, order).await? {
        store
            .set_line(line_id, final_state, LineUpdate::position(position_id))
            .await?;
    }
    let trigger = resting_trigger(&position)?;
    let gtt_id = position.val("gtt_id");
    let (gtt, ok) = if gtt_id.is_none() {
        let gtt = arm(
            gateway,
            settings,
            Arm {
                symbol: &symbol,
                qty: new_open,
                trigger,
                last_price: average,
                client_id: format!("fill:{order_id}:{symbol}:{ARM_GTT}"),
            },
        )
        .await?;
        let ok = in_set(&res_str_none(&gtt, "status"), &GTT_PLACED_STATUSES);
        if ok {
            store
                .update_position(position_id, gtt_fields(&gtt, trigger, &now))
                .await?;
        }
        (gtt, ok)
    } else {
        let gtt = resize_gtt(
            gateway, settings, &gtt_id, &symbol, new_open, trigger, average,
        )
        .await?;
        let ok = in_set(&res_str_none(&gtt, "status"), &GTT_MODIFIED_STATUSES);
        if ok {
            store
                .update_position(position_id, Rec::of([("gtt_armed_at", Val::Stamp(now))]))
                .await?;
        }
        (gtt, ok)
    };
    if ok {
        return Ok(ExecOutcome::new(
            final_state,
            format!(
                "{symbol}: +{delta} filled at {}, GTT now covers {new_open}",
                dstr(delta_price)
            ),
            None,
            Some(gtt),
            Some(order_id),
            Some(position_id),
            gates.dry_run,
            false,
        ));
    }
    let reason = format!(
        "{symbol}: +{delta} filled, but the GTT could not be re-sized to {new_open} \u{2014} {}; it \
         still covers {open_qty}",
        blocked_reason(&gtt, "the GTT")
    );
    Ok(ExecOutcome::new(
        final_state,
        reason,
        None,
        Some(gtt),
        Some(order_id),
        Some(position_id),
        gates.dry_run,
        true,
    ))
}

async fn close_dead_order(
    store: &mut dyn TwtStore,
    settings: &Settings,
    order: &Rec,
    status: &str,
    payload: &OrderReport,
    outcome: Option<ExecOutcome>,
) -> ExecResult<ExecOutcome> {
    let mut position_id = order.opt_int("position_id");
    if let Some(o) = &outcome
        && o.position_id.is_some()
    {
        position_id = o.position_id;
    }
    let reason = payload
        .status_message
        .clone()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| status.to_owned());
    let line_id = line_for_order(store, order).await?;
    let order_id = order.int("id");
    let sim = twt_gates(settings).dry_run;
    if let Some(pid) = position_id {
        store
            .update_order(order_id, Rec::of([("state", Val::from("FILLED"))]))
            .await?;
        if let Some(line_id) = line_id {
            store
                .set_line(
                    line_id,
                    "FILLED",
                    LineUpdate {
                        position_id: Some(pid),
                        note: Some(format!(
                            "{status}: {} filled, the rest never did",
                            order.int("filled_quantity")
                        )),
                        ..LineUpdate::default()
                    },
                )
                .await?;
        }
        return Ok(outcome.unwrap_or_else(|| {
            ExecOutcome::new(
                "FILLED",
                format!("{status} with shares held"),
                None,
                None,
                Some(order_id),
                Some(pid),
                sim,
                false,
            )
        }));
    }
    store
        .update_order(
            order_id,
            Rec::of([(
                "state",
                Val::from(if status == "REJECTED" {
                    "REJECTED"
                } else {
                    "CANCELLED"
                }),
            )]),
        )
        .await?;
    if let Some(line_id) = line_id {
        store
            .set_line(
                line_id,
                "REJECTED",
                LineUpdate::note(format!("{status}: {reason}")),
            )
            .await?;
    }
    Ok(ExecOutcome::new(
        "REJECTED",
        format!("{status}: {reason}"),
        None,
        None,
        Some(order_id),
        None,
        sim,
        false,
    ))
}

#[allow(clippy::too_many_arguments)]
async fn apply_exit_update(
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    exit_order: &Rec,
    status: &str,
    filled: i64,
    average: Decimal,
    gates: ProductGates,
    now: DateTime<FixedOffset>,
) -> ExecResult<Option<ExecOutcome>> {
    if !matches!(exit_order.s("state").as_str(), "SENT" | "PARTIAL") {
        return Ok(None);
    }
    let Some(position) = store.position(exit_order.int("position_id")).await? else {
        return Ok(None);
    };
    let position_id = position.int("id");
    let recorded = exit_order.int("filled_quantity");
    let complete = status == ORDER_COMPLETE;
    let symbol = exit_order.s("symbol");
    let exit_id = exit_order.int("id");
    let mut outcome = None;
    if filled > recorded && average > Decimal::ZERO {
        let delta = filled - recorded;
        let old_avg = exit_order.dec("avg_fill_price").unwrap_or(Decimal::ZERO);
        let mut delta_price = if recorded != 0 {
            ddiv(
                dmul(average, Decimal::from(filled)) - dmul(old_avg, Decimal::from(recorded)),
                Decimal::from(delta),
            )
            .map_err(bad)?
        } else {
            average
        };
        if delta_price <= Decimal::ZERO {
            delta_price = average;
        }
        let reason = exit_order
            .opt_str("reason")
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "MANUAL".to_owned());
        outcome = Some(
            apply_exit_fill(
                store,
                gateway,
                settings,
                ExitFill {
                    position: &position,
                    line_id: None,
                    quantity: delta,
                    average: quantize(delta_price, -4),
                    reason: &reason,
                    day: session_day(&now),
                    simulated: false,
                    now,
                },
            )
            .await?,
        );
        store
            .update_exit_order(
                exit_id,
                Rec::of([
                    (
                        "state",
                        Val::from(if complete { "FILLED" } else { "PARTIAL" }),
                    ),
                    ("filled_quantity", Val::Int(filled)),
                    ("avg_fill_price", Val::Dec(average)),
                ]),
            )
            .await?;
    }
    if ORDER_DEAD_STATUSES.contains(&status) {
        let most = filled.max(recorded);
        let final_state = if most > 0 {
            "FILLED"
        } else if status == "REJECTED" {
            "REJECTED"
        } else {
            "CANCELLED"
        };
        store
            .update_exit_order(exit_id, Rec::of([("state", Val::from(final_state))]))
            .await?;
        if let Some(line_id) = exit_order.opt_int("line_id") {
            store
                .set_line(
                    line_id,
                    if final_state == "FILLED" {
                        "FILLED"
                    } else {
                        "REJECTED"
                    },
                    LineUpdate::note(format!(
                        "{status}: {most} of {} sold",
                        exit_order.s("quantity")
                    )),
                )
                .await?;
        }
        return Ok(Some(ExecOutcome::new(
            final_state,
            format!("{symbol}: sell {status}"),
            None,
            None,
            None,
            Some(position_id),
            gates.dry_run,
            false,
        )));
    }
    if complete {
        store
            .update_exit_order(exit_id, Rec::of([("state", Val::from("FILLED"))]))
            .await?;
        if let Some(line_id) = exit_order.opt_int("line_id") {
            store
                .set_line(line_id, "FILLED", LineUpdate::position(position_id))
                .await?;
        }
        return Ok(Some(outcome.unwrap_or_else(|| {
            ExecOutcome::new(
                "FILLED",
                format!("{symbol}: sold {filled} at {}", dstr(average)),
                None,
                None,
                None,
                Some(position_id),
                gates.dry_run,
                false,
            )
        })));
    }
    Ok(outcome)
}

/// `_refused_json`'s outcome: a guard's refusal (non-negotiable 7) as an answer, not a 500.
#[must_use]
pub fn refused_rec(message: &str, simulated: bool) -> Rec {
    Rec::of([
        ("status", Val::from("BLOCKED")),
        ("reason", Val::from(message)),
        ("order", Val::None),
        ("gtt", Val::None),
        ("order_row_id", Val::None),
        ("position_id", Val::None),
        ("simulated", Val::Bool(simulated)),
        ("naked", Val::Bool(false)),
    ])
}
