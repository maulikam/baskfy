//! `app/vbt_execute.py` — one confirmed plan line becomes an order, a stop, a cancel, or a
//! refusal with its reason. Written against [`VbtStore`] and **handed** its gateway, so the same
//! code runs over the desk's Postgres and over a test's SQLite file, and a test can prove the
//! whole path with a broker that records every call.
//!
//! The rules it enforces are the module docstring's: **no auto-execution, and no flag that
//! changes it** (`docs/vbt/02` Track C §3, DECISIONS-VB PACK.2 — non-negotiable 1's named
//! exceptions belong to the swing, TWT and F3 books; this sleeve has none and never gains one);
//! nothing runs without `confirm="true"`, a `plan_id` issued in the last thirty minutes and a
//! line still `PROPOSED` (400/404/410/409); the sleeve never sells a holding it did not buy; a
//! stop never falls, and every filled quantity gets one the same session (non-negotiable 4); a
//! `PLACE_LIMIT` is a LIMIT at the signal's close; `client_id = plan_id:symbol:kind`; CNC only
//! ([`vbt_gates`] hands the gateway intraday and options **off**); and with
//! `BASKFY_VBT_EXECUTION_ENABLED=false` the same path runs through the gateway's dry-run branch.
//! Every order goes through the [`Gateway`] this module is handed; nothing here names a broker
//! method.

use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use super::super::twt::common::{
    HttpError, Rec, Val, dfloat, dmul, dstr, protection_unresolved, py_isoformat, py_map, quantize,
    refusal, res_get, res_str, tenant, to_ist,
};
use super::store::{LineUpdate, SessionBump, StoreError, VbtStore};
use crate::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder, ProductGates,
    StopBand, TenantIds,
};
use crate::settings::Settings;
use crate::wire::PyDict;

use baskfy_core::vbt::config::VbtConfig;

/// `LineKind.PLACE_LIMIT`.
pub const PLACE_LIMIT: &str = "PLACE_LIMIT";
/// `LineKind.SELL_AT_OPEN`.
pub const SELL_AT_OPEN: &str = "SELL_AT_OPEN";
/// `LineKind.CANCEL_LIMIT`.
pub const CANCEL_LIMIT: &str = "CANCEL_LIMIT";
/// `LineKind.ARM_GTT`.
pub const ARM_GTT: &str = "ARM_GTT";
/// `LineKind.BUY_AT_MARKET` (LV8).
pub const BUY_AT_MARKET: &str = "BUY_AT_MARKET";
/// `LineKind.RAISE_GTT_STOP` (LV9).
pub const RAISE_GTT_STOP: &str = "RAISE_GTT_STOP";

/// `EXECUTABLE_KINDS`: six since LV9. None of them opens a short.
pub const EXECUTABLE_KINDS: [&str; 6] = [
    PLACE_LIMIT,
    SELL_AT_OPEN,
    CANCEL_LIMIT,
    ARM_GTT,
    BUY_AT_MARKET,
    RAISE_GTT_STOP,
];

/// `PLACED_STATUSES`.
pub const PLACED_STATUSES: [&str; 2] = ["PLACED", "DUPLICATE"];
/// `DRY_RUN_STATUSES`.
pub const DRY_RUN_STATUSES: [&str; 1] = ["DRY_RUN"];
/// `VBT_MARKET_PROTECTION`: Kite's own auto band, as TWT sends it (LV8).
pub const VBT_MARKET_PROTECTION: f64 = -1.0;
/// `ORDER_COMPLETE`.
pub const ORDER_COMPLETE: &str = "COMPLETE";
/// `ORDER_DEAD_STATUSES`.
pub const ORDER_DEAD_STATUSES: [&str; 3] = ["REJECTED", "CANCELLED", "CANCELLED AMO"];
/// `SIMULATED_GTT_PREFIX`.
pub const SIMULATED_GTT_PREFIX: &str = "DRY-";
/// `VBT_JOURNAL_NAME`: this sleeve's own journal, beside the weekly book's and the swing book's.
pub const VBT_JOURNAL_NAME: &str = "vbt_orders_journal.jsonl";
/// `baskfy_execution.gtt.GTT_PLACED_STATUSES`.
pub const GTT_PLACED_STATUSES: [&str; 2] = ["GTT_PLACED", "DRY_RUN_GTT"];
/// `baskfy_execution.gtt.GTT_MODIFIED_STATUSES`.
pub const GTT_MODIFIED_STATUSES: [&str; 2] = ["GTT_MODIFIED", "DRY_RUN_GTT_MODIFY"];

/// `vbt_gates()`: an order is real only when the desk is out of `DRY_RUN` **and** the sleeve's
/// flag is on; intraday and options **false unconditionally** (CNC only, Track C §1).
#[must_use]
pub const fn vbt_gates(s: &Settings) -> ProductGates {
    ProductGates {
        dry_run: s.dry_run || !s.vbt_execution_enabled,
        intraday_enabled: false,
        options_enabled: false,
        fno_carry_enabled: false,
    }
}

/// `vbt_stop_band()`: `04` §6.1, 0.5–15 % below the entry (the settings' two values).
///
/// # Errors
/// A band out of range.
pub fn vbt_stop_band(s: &Settings) -> Result<StopBand, crate::gateway::ClientIdError> {
    StopBand::new(s.vbt_stop_band_min, s.vbt_stop_band_max)
}

/// `is_simulated()`.
#[must_use]
pub const fn is_simulated(s: &Settings) -> bool {
    vbt_gates(s).dry_run
}

fn config() -> VbtConfig {
    VbtConfig::default()
}

/// What one confirmed line came to (`ExecOutcome`).
#[derive(Debug, Clone, PartialEq)]
pub struct ExecOutcome {
    /// `SIMULATED` / `SENT` / `FILLED` / `BLOCKED` / `REJECTED` / `CANCELLED`.
    pub status: String,
    /// The words.
    pub reason: String,
    /// The gateway's order result.
    pub order: Option<PyDict>,
    /// The gateway's GTT result.
    pub gtt: Option<PyDict>,
    /// `vb_order.id`.
    pub order_row_id: Option<i64>,
    /// `vb_position.id`.
    pub position_id: Option<i64>,
    /// Journalled `simulated`.
    pub simulated: bool,
}

impl ExecOutcome {
    fn new(
        status: &str,
        reason: impl Into<String>,
        order: Option<PyDict>,
        gtt: Option<PyDict>,
        ids: (Option<i64>, Option<i64>),
        simulated: bool,
    ) -> Self {
        Self {
            status: status.to_owned(),
            reason: reason.into(),
            order,
            gtt,
            order_row_id: ids.0,
            position_id: ids.1,
            simulated,
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
        ])
    }
}

fn blocked(reason: impl Into<String>, simulated: bool) -> ExecOutcome {
    ExecOutcome::new("BLOCKED", reason, None, None, (None, None), simulated)
}

/// Why a confirm did not return an outcome.
#[derive(Debug, thiserror::Error)]
pub enum ExecError {
    /// `HTTPException(status, detail)`.
    #[error("{} {}", .0.status, .0.detail)]
    Http(HttpError),
    /// The gateway raised (a 500 at the route, as in Python).
    #[error(transparent)]
    Gateway(GatewayError),
    /// The store, or a value Python would raise on.
    #[error(transparent)]
    Store(#[from] StoreError),
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

/// `Decimal(str(row[key]))`, refusing what Python's `Decimal` refuses.
fn price(row: &Rec, key: &str) -> ExecResult<Decimal> {
    row.dec(key).ok_or_else(|| {
        bad(format!(
            "decimal.InvalidOperation: [<class 'decimal.ConversionSyntax'>] ({key} is {})",
            row.s(key)
        ))
    })
}

fn status_of(result: &PyDict) -> String {
    res_str(result, "status")
}

fn placed_or_dry(status: &str) -> bool {
    PLACED_STATUSES.contains(&status) || DRY_RUN_STATUSES.contains(&status)
}

fn gtt_ok(result: &PyDict) -> bool {
    let status = status_of(result);
    GTT_PLACED_STATUSES.contains(&status.as_str()) || status.starts_with("DRY_RUN")
}

fn refused(status: &str) -> bool {
    status.ends_with("BLOCKED") || status == "ERROR"
}

fn mode(simulated: bool) -> String {
    if simulated { "DRY_RUN" } else { "LIVE" }.to_owned()
}

fn session_day(now: &DateTime<FixedOffset>) -> NaiveDate {
    to_ist(now).date_naive()
}

fn plan_day(plan: &Rec) -> ExecResult<NaiveDate> {
    plan.date("session_date")
        .ok_or_else(|| bad("plan has no session_date"))
}

fn client_id(line: &Rec) -> Option<String> {
    line.opt_str("client_id")
}

/// `_validate(store, plan_id, line_id, confirm, now)`: the four refusals, in the order a caller
/// hits them. A missing `confirm` is answered before anything is read.
///
/// # Errors
/// The `HTTPException`s, or the store.
pub async fn validate(
    store: &mut dyn VbtStore,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    now: DateTime<FixedOffset>,
) -> ExecResult<Rec> {
    validate_plan(store, plan_id, confirm, now).await?;
    let line = store.line(line_id).await?;
    let Some(line) = line.filter(|l| l.s("plan_id") == plan_id) else {
        return Err(http(404, format!("no line {line_id} on plan {plan_id}")));
    };
    let kind = line.s("kind");
    if !EXECUTABLE_KINDS.contains(&kind.as_str()) {
        return Err(http(400, format!("a {kind} line cannot be executed")));
    }
    let state = line.s("state");
    if state != "PROPOSED" {
        return Err(http(
            409,
            format!("line {line_id} is already {state} \u{2014} a line is confirmed once"),
        ));
    }
    Ok(line)
}

/// `_validate` for a `line_id` no `bigint` can hold (FastAPI's `int` is unbounded, so Python
/// gets that far, and `store.line(...)` finds nothing): the same first three refusals in the
/// same order, then the 404 naming the id as Python's `str(int)` prints it. Always an error.
///
/// # Errors
/// Always: the 400/404/410, or the 404 for the line.
pub async fn validate_unknown_line(
    store: &mut dyn VbtStore,
    plan_id: &str,
    line_text: &str,
    confirm: &str,
    now: DateTime<FixedOffset>,
) -> ExecError {
    match validate_plan(store, plan_id, confirm, now).await {
        Err(e) => e,
        Ok(()) => http(404, format!("no line {line_text} on plan {plan_id}")),
    }
}

/// `_validate`'s first three refusals: no `confirm`, no plan, an expired plan.
async fn validate_plan(
    store: &mut dyn VbtStore,
    plan_id: &str,
    confirm: &str,
    now: DateTime<FixedOffset>,
) -> ExecResult<()> {
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
        && now > expires
    {
        return Err(http(
            410,
            format!(
                "plan {plan_id} expired at {}; rebuild it and confirm again",
                py_isoformat(&expires, 'T')
            ),
        ));
    }
    Ok(())
}

/// LV10: how many open entries the sleeve holds in a name (pyramiding counts entries).
async fn open_entries_in(store: &mut dyn VbtStore, instrument_id: i64) -> ExecResult<i64> {
    let rows = store.open_positions().await?;
    #[allow(clippy::cast_possible_wrap)]
    let n = rows
        .iter()
        .filter(|r| r.int("instrument_id") == instrument_id && r.int("quantity_open") > 0)
        .count() as i64;
    Ok(n)
}

/// `_held_refusal(store, instrument_id, sizing)`.
async fn held_refusal(store: &mut dyn VbtStore, instrument_id: i64) -> ExecResult<Option<String>> {
    let sizing = config().sizing;
    let held = open_entries_in(store, instrument_id).await?;
    if held != 0 && !sizing.pyramiding {
        return Ok(Some(
            "ALREADY_HELD: the sleeve holds this name; the book never averages down".to_owned(),
        ));
    }
    let cap = sizing.max_entries_per_name;
    if held >= cap {
        return Ok(Some(format!(
            "ALREADY_HELD: {held} open entries in this name already; {cap} is the cap"
        )));
    }
    Ok(None)
}

/// `naked_positions(store)`: every `OPEN` position with shares and no resting `gtt_id`.
///
/// # Errors
/// The store.
pub async fn naked_positions(store: &mut dyn VbtStore) -> ExecResult<Vec<Rec>> {
    Ok(store
        .open_positions()
        .await?
        .into_iter()
        .filter(|r| r.is_none("gtt_id") && r.int("quantity_open") > 0)
        .collect())
}

async fn reject_line(
    store: &mut dyn VbtStore,
    line: &Rec,
    reason: String,
    simulated: bool,
) -> ExecResult<ExecOutcome> {
    store
        .set_line(line.int("id"), "REJECTED", LineUpdate::note(reason.clone()))
        .await?;
    Ok(blocked(reason, simulated))
}

/// The refusals `_place_limit` and `_buy_at_market` share: session cap, held, working,
/// protection unresolved. `working_words` is the one phrase that differs.
async fn entry_refusal(
    store: &mut dyn VbtStore,
    line: &Rec,
    now: &DateTime<FixedOffset>,
    working_words: &str,
) -> ExecResult<Option<String>> {
    let taken = store.entries_taken(session_day(now)).await?;
    let cap = config().sizing.max_new_entries_per_session;
    if taken >= cap {
        return Ok(Some(format!(
            "SESSION_CAP: {taken} entries already taken today; {cap} is the session's cap"
        )));
    }
    let instrument_id = line.int("instrument_id");
    if let Some(held) = held_refusal(store, instrument_id).await? {
        return Ok(Some(held));
    }
    if store.working_order_for(instrument_id).await?.is_some() {
        return Ok(Some(format!("ALREADY_WORKING: {working_words}")));
    }
    let naked = naked_positions(store).await?;
    let issues = store.open_protection_issues().await?;
    let unresolved = protection_unresolved(&naked, &issues);
    if !unresolved.is_empty() {
        return Ok(Some(refusal(&unresolved)));
    }
    Ok(None)
}

fn place_failed(result: PyDict, status: &str, simulated: bool) -> (String, ExecOutcome) {
    let error = res_str(&result, "error");
    let reason = if error.is_empty() {
        format!("the gateway answered {status}")
    } else {
        error
    };
    let kind = if status.ends_with("BLOCKED") {
        "BLOCKED"
    } else {
        "REJECTED"
    };
    let outcome = ExecOutcome::new(
        kind,
        reason.clone(),
        Some(result),
        None,
        (None, None),
        simulated,
    );
    (reason, outcome)
}

/// The `vb_order` row a sent buy records.
fn order_fields(
    line: &Rec,
    plan: &Rec,
    limit: Decimal,
    stop: Decimal,
    result: &PyDict,
    simulated: bool,
    now: &DateTime<FixedOffset>,
) -> Rec {
    Rec::of([
        ("instrument_id", Val::Int(line.int("instrument_id"))),
        ("signal_date", plan.val("session_date")),
        ("limit_price", Val::Dec(limit)),
        ("stop_price", Val::Dec(stop)),
        ("quantity", Val::Int(line.int("quantity"))),
        ("state", Val::from("SENT")),
        ("broker_order_id", res_get(result, "order_id")),
        ("client_id", line.val("client_id")),
        ("simulated", Val::Bool(simulated)),
        ("created_at", Val::Stamp(*now)),
    ])
}

async fn place_limit(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    line: &Rec,
    plan: &Rec,
    now: DateTime<FixedOffset>,
) -> ExecResult<ExecOutcome> {
    let gates = vbt_gates(settings);
    if let Some(reason) =
        entry_refusal(store, line, &now, "a limit is already resting in this name").await?
    {
        return reject_line(store, line, reason, gates.dry_run).await;
    }
    let limit = price(line, "limit_price")?;
    let quantity = line.int("quantity");
    let t = tenant_of(settings)?;
    let result = gateway
        .place(PlaceOrder {
            symbol: line.s("symbol"),
            qty: quantity,
            side: "BUY".to_owned(),
            product: "CNC".to_owned(),
            order_type: "LIMIT".to_owned(),
            price: Some(dfloat(limit)),
            client_id: client_id(line),
            gross_exposure: Some(dfloat(dmul(limit, Decimal::from(quantity)))),
            tenant: Some(t),
            plan_tenant: Some(t),
            ..PlaceOrder::default()
        })
        .await?;
    let status = status_of(&result);
    if !placed_or_dry(&status) {
        let (reason, outcome) = place_failed(result, &status, gates.dry_run);
        store
            .set_line(line.int("id"), "REJECTED", LineUpdate::note(reason))
            .await?;
        return Ok(outcome);
    }
    let stop = price(line, "stop_price")?;
    let order_row = store
        .create_order(order_fields(
            line,
            plan,
            limit,
            stop,
            &result,
            gates.dry_run,
            &now,
        ))
        .await?;
    store
        .set_line(
            line.int("id"),
            "SENT",
            LineUpdate {
                journal_ref: Some(res_str(&result, "order_id")),
                order_id: Some(order_row),
                ..LineUpdate::default()
            },
        )
        .await?;
    let day = plan_day(plan)?;
    store
        .bump_session(
            day,
            SessionBump {
                mode: mode(gates.dry_run),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    if DRY_RUN_STATUSES.contains(&status.as_str()) {
        // `02` Track B: the dry-run branch is a complete fill at the limit.
        return apply_fill(
            store,
            gateway,
            settings,
            Fill {
                line: Some(line),
                order_row,
                quantity,
                fill_price: limit,
                entry_date: day,
                now,
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
        (Some(order_row), None),
        gates.dry_run,
    ))
}

#[allow(clippy::too_many_arguments)]
async fn buy_at_market(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    line: &Rec,
    plan: &Rec,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> ExecResult<ExecOutcome> {
    let gates = vbt_gates(settings);
    if let Some(reason) = entry_refusal(
        store,
        line,
        &now,
        "an order is already working in this name",
    )
    .await?
    {
        return reject_line(store, line, reason, gates.dry_run).await;
    }
    let preview = if line.truthy("limit_price") {
        price(line, "limit_price")?
    } else {
        Decimal::ZERO
    };
    let reference = match last_price {
        Some(p) if p > Decimal::ZERO => p,
        _ => preview,
    };
    if reference <= Decimal::ZERO {
        return reject_line(
            store,
            line,
            "no price to value this market buy against".to_owned(),
            gates.dry_run,
        )
        .await;
    }
    let quantity = line.int("quantity");
    let t = tenant_of(settings)?;
    let result = gateway
        .place(PlaceOrder {
            symbol: line.s("symbol"),
            qty: quantity,
            side: "BUY".to_owned(),
            product: "CNC".to_owned(),
            order_type: "MARKET".to_owned(),
            market_protection: Some(VBT_MARKET_PROTECTION),
            client_id: client_id(line),
            reference_price: Some(dfloat(reference)),
            gross_exposure: Some(dfloat(dmul(reference, Decimal::from(quantity)))),
            tenant: Some(t),
            plan_tenant: Some(t),
            ..PlaceOrder::default()
        })
        .await?;
    let status = status_of(&result);
    if !placed_or_dry(&status) {
        let (reason, outcome) = place_failed(result, &status, gates.dry_run);
        store
            .set_line(line.int("id"), "REJECTED", LineUpdate::note(reason))
            .await?;
        return Ok(outcome);
    }
    let stop = price(line, "stop_price")?;
    let order_row = store
        .create_order(order_fields(
            line,
            plan,
            reference,
            stop,
            &result,
            gates.dry_run,
            &now,
        ))
        .await?;
    store
        .set_line(
            line.int("id"),
            "SENT",
            LineUpdate {
                journal_ref: Some(res_str(&result, "order_id")),
                order_id: Some(order_row),
                position_id: None,
                note: Some(format!(
                    "market buy sent against a reference of {}",
                    dstr(reference)
                )),
            },
        )
        .await?;
    let day = plan_day(plan)?;
    store
        .bump_session(
            day,
            SessionBump {
                mode: mode(gates.dry_run),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    if DRY_RUN_STATUSES.contains(&status.as_str()) {
        return apply_fill(
            store,
            gateway,
            settings,
            Fill {
                line: Some(line),
                order_row,
                quantity,
                fill_price: reference,
                entry_date: day,
                now,
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
        (Some(order_row), None),
        gates.dry_run,
    ))
}

/// `_apply_fill`'s keyword arguments.
struct Fill<'a> {
    line: Option<&'a Rec>,
    order_row: i64,
    quantity: i64,
    fill_price: Decimal,
    entry_date: NaiveDate,
    now: DateTime<FixedOffset>,
    complete: bool,
}

/// `_apply_fill`: a filled buy becomes a `vb_position`, a `vb_fill` **and a resting GTT**.
async fn apply_fill(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    f: Fill<'_>,
) -> ExecResult<ExecOutcome> {
    let gates = vbt_gates(settings);
    let order = store
        .order(f.order_row)
        .await?
        .ok_or_else(|| bad("AssertionError: the order row vanished"))?;
    let stop = price(&order, "stop_price")?;
    let symbol = f.line.map_or_else(|| order.s("symbol"), |l| l.s("symbol"));
    let position_id = store
        .create_position(Rec::of([
            ("instrument_id", Val::Int(order.int("instrument_id"))),
            ("entry_date", Val::Date(f.entry_date)),
            ("entry_avg", Val::Dec(quantize(f.fill_price, -4))),
            ("quantity_entered", Val::Int(f.quantity)),
            ("quantity_open", Val::Int(f.quantity)),
            ("initial_stop", Val::Dec(stop)),
            ("stop_price", Val::Dec(stop)),
            ("state", Val::from("OPEN")),
            ("simulated", Val::Bool(gates.dry_run)),
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
            ("simulated", Val::Bool(gates.dry_run)),
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
    let line_id = match f.line {
        Some(l) => Some(l.int("id")),
        None => line_for_order(store, &order).await?,
    };
    if let Some(line_id) = line_id {
        store
            .set_line(
                line_id,
                if f.complete { "FILLED" } else { "SENT" },
                LineUpdate::position(position_id),
            )
            .await?;
    }
    store
        .bump_session(
            f.entry_date,
            SessionBump {
                mode: mode(gates.dry_run),
                fills: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    let gtt = arm_stop(
        store,
        gateway,
        settings,
        Arm {
            symbol: &symbol,
            position_id,
            quantity: f.quantity,
            stop,
            last_price: f.fill_price,
            now: f.now,
        },
    )
    .await?;
    let status = if gates.dry_run {
        "SIMULATED"
    } else if f.complete {
        "FILLED"
    } else {
        "SENT"
    };
    Ok(ExecOutcome::new(
        status,
        "",
        None,
        Some(gtt),
        (Some(f.order_row), Some(position_id)),
        gates.dry_run,
    ))
}

/// `_line_for_order`: the plan line that produced `order`, by client id.
async fn line_for_order(store: &mut dyn VbtStore, order: &Rec) -> ExecResult<Option<i64>> {
    if !order.truthy("client_id") {
        return Ok(None);
    }
    Ok(store
        .line_by_client_id(&order.s("client_id"))
        .await?
        .map(|l| l.int("id")))
}

/// `_arm_stop`'s arguments.
struct Arm<'a> {
    symbol: &'a str,
    position_id: i64,
    quantity: i64,
    stop: Decimal,
    last_price: Decimal,
    now: DateTime<FixedOffset>,
}

/// `_arm_stop`: non-negotiable 4 — every buy gets a GTT stop the same session, through the
/// gateway, with the sleeve's own cushion (`exits.gtt_limit_fraction`).
async fn arm_stop(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    a: Arm<'_>,
) -> ExecResult<PyDict> {
    let t = tenant_of(settings)?;
    let result = gateway
        .place_gtt_stop(GttStop {
            symbol: a.symbol.to_owned(),
            qty: a.quantity,
            trigger: dfloat(a.stop),
            last_price: dfloat(a.last_price),
            exchange: "NSE".to_owned(),
            series: None,
            client_id: None,
            tenant: Some(t),
            plan_tenant: Some(t),
            limit_fraction: Some(config().exits.gtt_limit_fraction),
            fo_plan: None,
        })
        .await?;
    if gtt_ok(&result) {
        store
            .update_position(
                a.position_id,
                Rec::of([
                    ("gtt_id", Val::Str(res_str(&result, "gtt_id"))),
                    ("gtt_trigger", Val::Dec(a.stop)),
                    ("gtt_armed_at", Val::Stamp(a.now)),
                ]),
            )
            .await?;
    }
    Ok(result)
}

/// `_position_for_line`: the position a line names if still open, else the name's open one.
async fn position_for_line(store: &mut dyn VbtStore, line: &Rec) -> ExecResult<Option<Rec>> {
    if let Some(position_id) = line.opt_int("position_id")
        && let Some(found) = store.position(position_id).await?
        && found.s("state") == "OPEN"
        && found.int("quantity_open") > 0
    {
        return Ok(Some(found));
    }
    Ok(store.open_position_for(line.int("instrument_id")).await?)
}

#[allow(clippy::too_many_lines)]
async fn sell_at_open(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    line: &Rec,
    plan: &Rec,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> ExecResult<ExecOutcome> {
    let gates = vbt_gates(settings);
    let symbol = line.s("symbol");
    let Some(position) = position_for_line(store, line).await? else {
        let reason = format!(
            "{symbol} is not in this sleeve's book \u{2014} it may be the weekly book's or the \
             swing book's, and this sleeve never sells a holding it did not buy"
        );
        return reject_line(store, line, reason, gates.dry_run).await;
    };
    let owned = position.int("quantity_open");
    let quantity = line.int("quantity");
    let exit_reason = if line.truthy("reason") {
        line.s("reason")
    } else if position.truthy("exit_reason_queued") {
        position.s("exit_reason_queued")
    } else {
        "EMA_EXIT".to_owned()
    };
    if quantity > owned {
        let reason = format!("the line sells {quantity} and the sleeve holds {owned}");
        return reject_line(store, line, reason, gates.dry_run).await;
    }
    // A MARKET order has no price of its own; the live read is the reference, the entry the
    // fallback — a fallback rather than a refusal **because this is an exit**.
    let reference = match last_price {
        Some(p) if !p.is_zero() => p,
        _ => price(&position, "entry_avg")?,
    };
    let t = tenant_of(settings)?;
    let result = gateway
        .place(PlaceOrder {
            symbol: symbol.clone(),
            qty: quantity,
            side: "SELL".to_owned(),
            product: "CNC".to_owned(),
            order_type: "MARKET".to_owned(),
            client_id: client_id(line),
            reference_price: Some(dfloat(reference)),
            gross_exposure: Some(dfloat(dmul(reference, Decimal::from(quantity)))),
            tenant: Some(t),
            plan_tenant: Some(t),
            ..PlaceOrder::default()
        })
        .await?;
    let status = status_of(&result);
    if !placed_or_dry(&status) {
        let error = res_str(&result, "error");
        let reason = if error.is_empty() {
            format!("the gateway answered {status}")
        } else {
            error
        };
        return reject_line(store, line, reason, gates.dry_run).await;
    }
    let position_id = position.int("id");
    let day = plan_day(plan)?;
    if !DRY_RUN_STATUSES.contains(&status.as_str()) {
        // LV2: a live sell the broker ACCEPTED is not a sell that FILLED — nothing is booked.
        let exit_id = store
            .create_exit_order(Rec::of([
                ("sleeve", Val::from("vbt")),
                ("position_id", Val::Int(position_id)),
                ("line_id", Val::Int(line.int("id"))),
                ("symbol", Val::Str(symbol.clone())),
                ("broker_order_id", res_get(&result, "order_id")),
                ("client_id", line.val("client_id")),
                ("quantity", Val::Int(quantity)),
                ("reference_price", Val::Dec(reference)),
                ("state", Val::from("SENT")),
                ("filled_quantity", Val::Int(0)),
                ("reason", Val::Str(exit_reason.clone())),
                ("simulated", Val::Bool(false)),
            ]))
            .await?;
        if exit_reason != "PARTIAL" {
            store
                .update_position(
                    position_id,
                    Rec::of([
                        ("exit_queued_for", Val::Date(day)),
                        ("exit_reason_queued", Val::Str(exit_reason.clone())),
                    ]),
                )
                .await?;
        }
        store
            .set_line(
                line.int("id"),
                "SENT",
                LineUpdate {
                    journal_ref: Some(res_str(&result, "order_id")),
                    position_id: Some(position_id),
                    ..LineUpdate::default()
                },
            )
            .await?;
        store
            .bump_session(
                day,
                SessionBump {
                    mode: "LIVE".to_owned(),
                    confirms: 1,
                    ..SessionBump::default()
                },
            )
            .await?;
        let order_id = result
            .get("order_id")
            .map_or_else(|| "None".to_owned(), crate::wire::PyValue::py_str);
        return Ok(ExecOutcome::new(
            "SENT",
            format!(
                "{symbol}: sell {quantity} accepted as order {order_id} (exit {exit_id}); the \
                 position is reduced when the broker reports the fill"
            ),
            Some(result),
            None,
            (None, Some(position_id)),
            false,
        ));
    }
    let remaining = owned - quantity;
    store
        .add_fill(Rec::of([
            ("position_id", Val::Int(position_id)),
            ("order_id", Val::None),
            ("side", Val::from("SELL")),
            ("quantity", Val::Int(quantity)),
            ("price", Val::Dec(reference)),
            ("filled_at", Val::Stamp(now)),
            ("simulated", Val::Bool(gates.dry_run)),
        ]))
        .await?;
    let closed = remaining == 0;
    store
        .update_position(
            position_id,
            Rec::of([
                ("quantity_open", Val::Int(remaining)),
                ("state", Val::from(if closed { "CLOSED" } else { "OPEN" })),
                ("closed_on", if closed { Val::Date(day) } else { Val::None }),
                (
                    "close_reason",
                    if closed {
                        Val::from("EMA_EXIT")
                    } else {
                        Val::None
                    },
                ),
                ("exit_queued_for", Val::None),
                ("exit_reason_queued", Val::None),
            ]),
        )
        .await?;
    store
        .set_line(
            line.int("id"),
            "FILLED",
            LineUpdate {
                journal_ref: Some(res_str(&result, "order_id")),
                position_id: Some(position_id),
                ..LineUpdate::default()
            },
        )
        .await?;
    store
        .bump_session(
            day,
            SessionBump {
                mode: mode(gates.dry_run),
                confirms: 1,
                exits: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    Ok(ExecOutcome::new(
        if gates.dry_run { "SIMULATED" } else { "SENT" },
        "",
        Some(result),
        None,
        (None, Some(position_id)),
        gates.dry_run,
    ))
}

async fn cancel_limit(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    line: &Rec,
    plan: &Rec,
) -> ExecResult<ExecOutcome> {
    let gates = vbt_gates(settings);
    let symbol = line.s("symbol");
    let Some(order) = store.working_order_for(line.int("instrument_id")).await? else {
        let reason = format!("no working order in {symbol} to cancel");
        return reject_line(store, line, reason, gates.dry_run).await;
    };
    let mut result = PyDict::new();
    result.insert(
        "status",
        crate::wire::PyValue::Str("NO_BROKER_ORDER".to_owned()),
    );
    if order.truthy("broker_order_id") {
        let t = tenant_of(settings)?;
        result = gateway
            .cancel_order(CancelOrder {
                order_id: order.s("broker_order_id"),
                symbol: symbol.clone(),
                variety: "regular".to_owned(),
                series: None,
                client_id: Some(format!("{}:cancel", line.s("client_id"))),
                tenant: Some(t),
                plan_tenant: Some(t),
            })
            .await?;
        let status = status_of(&result);
        if refused(&status) {
            let error = res_str(&result, "error");
            let reason = if error.is_empty() {
                format!("the gateway answered {status}")
            } else {
                error
            };
            return reject_line(store, line, reason, gates.dry_run).await;
        }
    }
    let order_id = order.int("id");
    store
        .update_order(
            order_id,
            Rec::of([
                ("state", Val::from("CANCELLED")),
                ("cancel_reason", Val::from("EXPIRY_SWEEP")),
                ("cancelled_on", plan.val("session_date")),
            ]),
        )
        .await?;
    store
        .set_line(
            line.int("id"),
            "FILLED",
            LineUpdate {
                order_id: Some(order_id),
                ..LineUpdate::default()
            },
        )
        .await?;
    store
        .bump_session(
            plan_day(plan)?,
            SessionBump {
                mode: mode(gates.dry_run),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    Ok(ExecOutcome::new(
        if gates.dry_run { "SIMULATED" } else { "FILLED" },
        "",
        Some(result),
        None,
        (Some(order_id), None),
        gates.dry_run,
    ))
}

fn broker_gtt_id(gtt_id: &Val) -> ExecResult<Option<i64>> {
    if gtt_id.is_none() || gtt_id.py_str().starts_with(SIMULATED_GTT_PREFIX) {
        return Ok(None);
    }
    gtt_id
        .as_int()
        .or_else(|| gtt_id.py_str().trim().parse().ok())
        .map(Some)
        .ok_or_else(|| {
            bad(format!(
                "ValueError: invalid literal for int() with base 10: '{}'",
                gtt_id.py_str()
            ))
        })
}

#[allow(clippy::too_many_lines)]
async fn raise_gtt_stop(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    line: &Rec,
    plan: &Rec,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> ExecResult<ExecOutcome> {
    let gates = vbt_gates(settings);
    let symbol = line.s("symbol");
    let Some(position) = position_for_line(store, line).await? else {
        return reject_line(
            store,
            line,
            format!("{symbol} is not in this sleeve's book"),
            gates.dry_run,
        )
        .await;
    };
    let position_id = position.int("id");
    if line.is_none("stop_price") {
        return reject_line(
            store,
            line,
            format!("{symbol}: a RAISE line needs a new trigger"),
            gates.dry_run,
        )
        .await;
    }
    let new_trigger = price(line, "stop_price")?;
    let resting = if position.truthy("gtt_trigger") {
        price(&position, "gtt_trigger")?
    } else {
        price(&position, "stop_price")?
    };
    if new_trigger <= resting {
        let reason = format!(
            "{symbol}: the new trigger {} is not above the resting stop {} \u{2014} a stop never \
             falls",
            dstr(new_trigger),
            dstr(resting)
        );
        return reject_line(store, line, reason, gates.dry_run).await;
    }
    let open_qty = position.int("quantity_open");
    if open_qty <= 0 {
        return reject_line(
            store,
            line,
            format!("{symbol}: nothing is open to protect"),
            gates.dry_run,
        )
        .await;
    }
    let reference = match last_price {
        Some(p) => p,
        None => price(&position, "entry_avg")?,
    };
    if new_trigger >= reference {
        let reason = format!(
            "{symbol}: the new trigger {} is at or above the last price {} \u{2014} that would \
             fire at once",
            dstr(new_trigger),
            dstr(reference)
        );
        return reject_line(store, line, reason, gates.dry_run).await;
    }
    if let Some(broker_id) = broker_gtt_id(&position.val("gtt_id"))? {
        let t = tenant_of(settings)?;
        let cancel = gateway
            .delete_gtt(DeleteGtt {
                gtt_id: broker_id,
                symbol: symbol.clone(),
                exchange: "NSE".to_owned(),
                series: None,
                client_id: Some(format!("{}:cancel", line.s("client_id"))),
                tenant: Some(t),
                plan_tenant: Some(t),
            })
            .await?;
        let status = status_of(&cancel);
        if refused(&status) {
            let reason = format!(
                "{symbol}: the resting GTT could not be cancelled ({status}); the stop stays \
                 where it was"
            );
            return reject_line(store, line, reason, gates.dry_run).await;
        }
    }
    store
        .update_position(position_id, Rec::of([("gtt_id", Val::None)]))
        .await?;
    let armed = arm_stop(
        store,
        gateway,
        settings,
        Arm {
            symbol: &symbol,
            position_id,
            quantity: open_qty,
            stop: new_trigger,
            last_price: reference,
            now,
        },
    )
    .await?;
    if !gtt_ok(&armed) {
        let reason = format!(
            "{symbol}: the old trigger is gone and the new one was NOT armed \u{2014} the \
             position is naked; re-arm now"
        );
        store
            .set_line(line.int("id"), "REJECTED", LineUpdate::note(reason.clone()))
            .await?;
        return Ok(ExecOutcome::new(
            "REJECTED",
            reason,
            None,
            Some(armed),
            (None, Some(position_id)),
            gates.dry_run,
        ));
    }
    store
        .update_position(
            position_id,
            Rec::of([("stop_price", Val::Dec(new_trigger))]),
        )
        .await?;
    store
        .set_line(line.int("id"), "FILLED", LineUpdate::position(position_id))
        .await?;
    store
        .bump_session(
            plan_day(plan)?,
            SessionBump {
                mode: mode(gates.dry_run),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    Ok(ExecOutcome::new(
        if gates.dry_run { "SIMULATED" } else { "FILLED" },
        "",
        None,
        Some(armed),
        (None, Some(position_id)),
        gates.dry_run,
    ))
}

async fn arm_gtt_line(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    line: &Rec,
    plan: &Rec,
    now: DateTime<FixedOffset>,
) -> ExecResult<ExecOutcome> {
    let gates = vbt_gates(settings);
    let symbol = line.s("symbol");
    let Some(position) = store.open_position_for(line.int("instrument_id")).await? else {
        return reject_line(
            store,
            line,
            format!("{symbol} is not in this sleeve's book"),
            gates.dry_run,
        )
        .await;
    };
    let stop = if line.truthy("stop_price") {
        price(line, "stop_price")?
    } else {
        price(&position, "stop_price")?
    };
    let position_id = position.int("id");
    let gtt = arm_stop(
        store,
        gateway,
        settings,
        Arm {
            symbol: &symbol,
            position_id,
            quantity: position.int("quantity_open"),
            stop,
            last_price: price(&position, "entry_avg")?,
            now,
        },
    )
    .await?;
    store
        .set_line(line.int("id"), "FILLED", LineUpdate::position(position_id))
        .await?;
    store
        .bump_session(
            plan_day(plan)?,
            SessionBump {
                mode: mode(gates.dry_run),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await?;
    Ok(ExecOutcome::new(
        if gates.dry_run { "SIMULATED" } else { "FILLED" },
        "",
        None,
        Some(gtt),
        (None, Some(position_id)),
        gates.dry_run,
    ))
}

/// `execute_line(store, gateway, plan_id=..., line_id=..., confirm=..., now=..., last_price=...)`:
/// one confirmed line becomes an order, a stop, a cancel, or a refusal with its reason. The
/// whole of it runs under the day's `vb_session` row lock (`04` §9.4).
///
/// # Errors
/// The 400/404/410/409 refusals, the gateway raising, or the store.
#[allow(clippy::too_many_arguments)]
pub async fn execute_line(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> ExecResult<ExecOutcome> {
    let line = validate(store, plan_id, line_id, confirm, now).await?;
    let plan = store
        .plan(plan_id)
        .await?
        .ok_or_else(|| bad("AssertionError: the plan vanished"))?;
    store.lock_session_for_update(plan_day(&plan)?).await?;
    match line.s("kind").as_str() {
        SELL_AT_OPEN => sell_at_open(store, gateway, settings, &line, &plan, now, last_price).await,
        RAISE_GTT_STOP => {
            raise_gtt_stop(store, gateway, settings, &line, &plan, now, last_price).await
        }
        BUY_AT_MARKET => {
            buy_at_market(store, gateway, settings, &line, &plan, now, last_price).await
        }
        PLACE_LIMIT => place_limit(store, gateway, settings, &line, &plan, now).await,
        CANCEL_LIMIT => cancel_limit(store, gateway, settings, &line, &plan).await,
        ARM_GTT => arm_gtt_line(store, gateway, settings, &line, &plan, now).await,
        other => Err(http(400, format!("a {other} line cannot be executed"))),
    }
}

// =============================================================================================
// LV2: the broker's reports on this sleeve's orders
// =============================================================================================

/// `on_order_update(store, gateway, payload, now=...)`: a broker report on one of this sleeve's
/// orders — a resting buy or a pending exit. `None` when the order is not this sleeve's or the
/// report adds nothing. Idempotent.
///
/// # Errors
/// The gateway raising, or the store.
pub async fn on_order_update(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    payload: &PyDict,
    now: DateTime<FixedOffset>,
) -> ExecResult<Option<ExecOutcome>> {
    let broker_id = res_str(payload, "order_id");
    if broker_id.is_empty() {
        return Ok(None);
    }
    let status = res_str(payload, "status").to_uppercase();
    let filled = res_get(payload, "filled_quantity").as_int().unwrap_or(0);
    let average = match payload.get("average_price") {
        None | Some(crate::wire::PyValue::None) => Decimal::ZERO,
        Some(_) => {
            let rec = Rec::of([("p", res_get(payload, "average_price"))]);
            price(&rec, "p")?
        }
    };
    let day = session_day(&now);
    let report = Report {
        status,
        filled,
        average,
        now,
        day,
    };
    if let Some(exit_order) = store.exit_order_by_broker_id(&broker_id).await? {
        store.lock_session_for_update(day).await?;
        return apply_exit_update(store, gateway, settings, &exit_order, &report).await;
    }
    let Some(order) = store.order_by_broker_id(&broker_id).await? else {
        return Ok(None);
    };
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
    apply_buy_update(store, gateway, settings, &current, &report).await
}

/// One broker report, parsed.
struct Report {
    status: String,
    filled: i64,
    average: Decimal,
    now: DateTime<FixedOffset>,
    day: NaiveDate,
}

#[allow(clippy::too_many_lines)]
async fn apply_buy_update(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    order: &Rec,
    r: &Report,
) -> ExecResult<Option<ExecOutcome>> {
    let gates = vbt_gates(settings);
    let recorded = order.int("filled_quantity");
    let position_id = order.opt_int("position_id");
    let complete = r.status == ORDER_COMPLETE;
    let order_id = order.int("id");
    let mut outcome: Option<ExecOutcome> = None;
    if r.filled > recorded && r.average > Decimal::ZERO {
        outcome = Some(match position_id {
            None => {
                apply_fill(
                    store,
                    gateway,
                    settings,
                    Fill {
                        line: None,
                        order_row: order_id,
                        quantity: r.filled,
                        fill_price: r.average,
                        entry_date: r.day,
                        now: r.now,
                        complete,
                    },
                )
                .await?
            }
            Some(pid) => grow_position(store, gateway, settings, order, pid, r, complete).await?,
        });
    }
    if ORDER_DEAD_STATUSES.contains(&r.status.as_str()) {
        let held = outcome.as_ref().and_then(|o| o.position_id).or(position_id);
        let line_id = line_for_order(store, order).await?;
        if let Some(held) = held {
            store
                .update_order(order_id, Rec::of([("state", Val::from("FILLED"))]))
                .await?;
            if let Some(line_id) = line_id {
                store
                    .set_line(
                        line_id,
                        "FILLED",
                        LineUpdate {
                            position_id: Some(held),
                            note: Some(format!(
                                "{}: {} filled, the rest never did",
                                r.status,
                                r.filled.max(recorded)
                            )),
                            ..LineUpdate::default()
                        },
                    )
                    .await?;
            }
            return Ok(Some(outcome.unwrap_or_else(|| {
                ExecOutcome::new(
                    "FILLED",
                    format!("{} with shares held", r.status),
                    None,
                    None,
                    (Some(order_id), Some(held)),
                    gates.dry_run,
                )
            })));
        }
        let reason = if order.truthy("cancel_reason") {
            order.s("cancel_reason")
        } else {
            r.status.clone()
        };
        let rejected = r.status == "REJECTED";
        store
            .update_order(
                order_id,
                Rec::of([
                    (
                        "state",
                        Val::from(if rejected { "REJECTED" } else { "CANCELLED" }),
                    ),
                    ("cancelled_on", Val::Date(r.day)),
                    (
                        "cancel_reason",
                        if rejected {
                            Val::None
                        } else {
                            Val::from("MANUAL")
                        },
                    ),
                ]),
            )
            .await?;
        if let Some(line_id) = line_id {
            store
                .set_line(
                    line_id,
                    "REJECTED",
                    LineUpdate::note(format!("{}: {reason}", r.status)),
                )
                .await?;
        }
        return Ok(Some(ExecOutcome::new(
            "REJECTED",
            format!("{}: nothing filled", r.status),
            None,
            None,
            (Some(order_id), None),
            gates.dry_run,
        )));
    }
    if complete
        && outcome.is_none()
        && let Some(pid) = position_id
    {
        store
            .update_order(order_id, Rec::of([("state", Val::from("FILLED"))]))
            .await?;
        if let Some(line_id) = line_for_order(store, order).await? {
            store
                .set_line(line_id, "FILLED", LineUpdate::position(pid))
                .await?;
        }
        return Ok(Some(ExecOutcome::new(
            "FILLED",
            "",
            None,
            None,
            (Some(order_id), Some(pid)),
            gates.dry_run,
        )));
    }
    Ok(outcome)
}

/// `_grow_position`: more of the same buy filled — grow the position, re-size its GTT.
async fn grow_position(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    order: &Rec,
    position_id: i64,
    r: &Report,
    complete: bool,
) -> ExecResult<ExecOutcome> {
    let gates = vbt_gates(settings);
    let order_id = order.int("id");
    let final_status = if complete { "FILLED" } else { "SENT" };
    let position = store
        .position(position_id)
        .await?
        .ok_or_else(|| bad("AssertionError: the position vanished"))?;
    let symbol = if position.truthy("symbol") {
        position.s("symbol")
    } else {
        order.s("symbol")
    };
    let entered = position.int("quantity_entered");
    let open_qty = position.int("quantity_open");
    let delta = r.filled - entered;
    if delta <= 0 {
        return Ok(ExecOutcome::new(
            final_status,
            "",
            None,
            None,
            (Some(order_id), Some(position_id)),
            gates.dry_run,
        ));
    }
    let old_avg = price(&position, "entry_avg")?;
    let numer = dmul(r.average, Decimal::from(r.filled)) - dmul(old_avg, Decimal::from(entered));
    let mut delta_price = quantize(
        super::super::twt::common::ddiv(numer, Decimal::from(delta)).map_err(bad)?,
        -4,
    );
    if delta_price <= Decimal::ZERO {
        delta_price = r.average;
    }
    let new_open = open_qty + delta;
    store
        .add_fill(Rec::of([
            ("position_id", Val::Int(position_id)),
            ("order_id", Val::Int(order_id)),
            ("side", Val::from("BUY")),
            ("quantity", Val::Int(delta)),
            ("price", Val::Dec(delta_price)),
            ("filled_at", Val::Stamp(r.now)),
            ("simulated", Val::Bool(gates.dry_run)),
        ]))
        .await?;
    store
        .update_position(
            position_id,
            Rec::of([
                ("quantity_entered", Val::Int(r.filled)),
                ("quantity_open", Val::Int(new_open)),
                ("entry_avg", Val::Dec(quantize(r.average, -4))),
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
                ("filled_quantity", Val::Int(r.filled)),
                ("avg_fill_price", Val::Dec(r.average)),
            ]),
        )
        .await?;
    if let Some(line_id) = line_for_order(store, order).await? {
        store
            .set_line(line_id, final_status, LineUpdate::position(position_id))
            .await?;
    }
    let stop = price(&position, "stop_price")?;
    let ok = cover(
        store,
        gateway,
        settings,
        &position,
        Cover {
            symbol: &symbol,
            qty: new_open,
            stop,
            last_price: r.average,
            now: r.now,
        },
    )
    .await?;
    let reason = if ok {
        format!(
            "{symbol}: +{delta} filled at {}, GTT now covers {new_open}",
            dstr(delta_price)
        )
    } else {
        format!(
            "{symbol}: +{delta} filled, but the GTT could not be re-sized to {new_open}; it still \
             covers {open_qty}"
        )
    };
    Ok(ExecOutcome::new(
        final_status,
        reason,
        None,
        None,
        (Some(order_id), Some(position_id)),
        gates.dry_run,
    ))
}

/// `_cover`'s arguments.
struct Cover<'a> {
    symbol: &'a str,
    qty: i64,
    stop: Decimal,
    last_price: Decimal,
    now: DateTime<FixedOffset>,
}

/// `_cover`: the one resting GTT covers exactly `qty` — re-sized when it rests, armed when it
/// does not, cancelled when `qty` is zero. Every call goes through the gateway.
async fn cover(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    position: &Rec,
    c: Cover<'_>,
) -> ExecResult<bool> {
    let gtt_id = position.val("gtt_id");
    let position_id = position.int("id");
    let simulated_id = !gtt_id.is_none() && gtt_id.py_str().starts_with(SIMULATED_GTT_PREFIX);
    if c.qty <= 0 {
        if gtt_id.is_none() || simulated_id {
            store
                .update_position(position_id, Rec::of([("gtt_id", Val::None)]))
                .await?;
            return Ok(true);
        }
        let broker_id = broker_gtt_id(&gtt_id)?.unwrap_or_default();
        let t = tenant_of(settings)?;
        let result = gateway
            .delete_gtt(DeleteGtt {
                gtt_id: broker_id,
                symbol: c.symbol.to_owned(),
                exchange: "NSE".to_owned(),
                series: None,
                client_id: Some(format!("exit:{position_id}:{}:cancel", c.symbol)),
                tenant: Some(t),
                plan_tenant: Some(t),
            })
            .await?;
        let ok = !refused(&status_of(&result));
        if ok {
            store
                .update_position(position_id, Rec::of([("gtt_id", Val::None)]))
                .await?;
        }
        return Ok(ok);
    }
    if gtt_id.is_none() {
        let result = arm_stop(
            store,
            gateway,
            settings,
            Arm {
                symbol: c.symbol,
                position_id,
                quantity: c.qty,
                stop: c.stop,
                last_price: c.last_price,
                now: c.now,
            },
        )
        .await?;
        return Ok(gtt_ok(&result));
    }
    if simulated_id {
        store
            .update_position(position_id, Rec::of([("gtt_armed_at", Val::Stamp(c.now))]))
            .await?;
        return Ok(true);
    }
    let broker_id = broker_gtt_id(&gtt_id)?.unwrap_or_default();
    let t = tenant_of(settings)?;
    let result = gateway
        .modify_gtt_quantity(ModifyGtt {
            gtt_id: broker_id,
            symbol: c.symbol.to_owned(),
            qty: c.qty,
            trigger: dfloat(c.stop),
            last_price: dfloat(c.last_price),
            exchange: "NSE".to_owned(),
            series: None,
            client_id: Some(format!("resize:{}:{}:{}", gtt_id.py_str(), c.symbol, c.qty)),
            tenant: Some(t),
            plan_tenant: Some(t),
            limit_fraction: Some(config().exits.gtt_limit_fraction),
            fo_plan: None,
            floor_trigger: None,
        })
        .await?;
    let status = status_of(&result);
    let ok = GTT_MODIFIED_STATUSES.contains(&status.as_str()) || status.starts_with("DRY_RUN");
    if ok {
        store
            .update_position(position_id, Rec::of([("gtt_armed_at", Val::Stamp(c.now))]))
            .await?;
    }
    Ok(ok)
}

#[allow(clippy::too_many_lines)]
async fn apply_exit_update(
    store: &mut dyn VbtStore,
    gateway: &dyn Gateway,
    settings: &Settings,
    exit_order: &Rec,
    r: &Report,
) -> ExecResult<Option<ExecOutcome>> {
    let gates = vbt_gates(settings);
    if !matches!(exit_order.s("state").as_str(), "SENT" | "PARTIAL") {
        return Ok(None);
    }
    let Some(position) = store.position(exit_order.int("position_id")).await? else {
        return Ok(None);
    };
    let position_id = position.int("id");
    let exit_id = exit_order.int("id");
    let recorded = exit_order.int("filled_quantity");
    let symbol = exit_order.s("symbol");
    let complete = r.status == ORDER_COMPLETE;
    let mut wrote = false;
    if r.filled > recorded && r.average > Decimal::ZERO {
        let delta = r.filled - recorded;
        let old_avg = if exit_order.truthy("avg_fill_price") {
            price(exit_order, "avg_fill_price")?
        } else {
            Decimal::ZERO
        };
        let mut delta_price = if recorded != 0 {
            let numer =
                dmul(r.average, Decimal::from(r.filled)) - dmul(old_avg, Decimal::from(recorded));
            quantize(
                super::super::twt::common::ddiv(numer, Decimal::from(delta)).map_err(bad)?,
                -4,
            )
        } else {
            r.average
        };
        if delta_price <= Decimal::ZERO {
            delta_price = r.average;
        }
        let remaining = (position.int("quantity_open") - delta).max(0);
        store
            .add_fill(Rec::of([
                ("position_id", Val::Int(position_id)),
                ("order_id", Val::None),
                ("side", Val::from("SELL")),
                ("quantity", Val::Int(delta)),
                ("price", Val::Dec(delta_price)),
                ("filled_at", Val::Stamp(r.now)),
                ("simulated", Val::Bool(false)),
            ]))
            .await?;
        let closed = remaining == 0;
        let partial = exit_order.s("reason") == "PARTIAL";
        let reason_or = if exit_order.truthy("reason") {
            exit_order.s("reason")
        } else {
            "EMA_EXIT".to_owned()
        };
        store
            .update_position(
                position_id,
                Rec::of([
                    ("quantity_open", Val::Int(remaining)),
                    ("state", Val::from(if closed { "CLOSED" } else { "OPEN" })),
                    (
                        "closed_on",
                        if closed { Val::Date(r.day) } else { Val::None },
                    ),
                    (
                        "partial_done",
                        if partial {
                            Val::Bool(true)
                        } else {
                            position
                                .get("partial_done")
                                .cloned()
                                .unwrap_or(Val::Bool(false))
                        },
                    ),
                    (
                        "close_reason",
                        if closed {
                            Val::Str(if partial {
                                "MANUAL".to_owned()
                            } else {
                                reason_or
                            })
                        } else {
                            Val::None
                        },
                    ),
                    (
                        "exit_avg",
                        if closed {
                            Val::Dec(quantize(r.average, -4))
                        } else {
                            Val::None
                        },
                    ),
                    (
                        "exit_queued_for",
                        if closed {
                            Val::None
                        } else {
                            position.val("exit_queued_for")
                        },
                    ),
                    (
                        "exit_reason_queued",
                        if closed {
                            Val::None
                        } else {
                            position.val("exit_reason_queued")
                        },
                    ),
                ]),
            )
            .await?;
        store
            .update_exit_order(
                exit_id,
                Rec::of([
                    (
                        "state",
                        Val::from(if complete { "FILLED" } else { "PARTIAL" }),
                    ),
                    ("filled_quantity", Val::Int(r.filled)),
                    ("avg_fill_price", Val::Dec(r.average)),
                ]),
            )
            .await?;
        let stop = price(&position, "stop_price")?;
        cover(
            store,
            gateway,
            settings,
            &position,
            Cover {
                symbol: &symbol,
                qty: remaining,
                stop,
                last_price: r.average,
                now: r.now,
            },
        )
        .await?;
        store
            .bump_session(
                r.day,
                SessionBump {
                    mode: "LIVE".to_owned(),
                    exits: i64::from(closed),
                    ..SessionBump::default()
                },
            )
            .await?;
        wrote = true;
    }
    if ORDER_DEAD_STATUSES.contains(&r.status.as_str()) {
        let sold = r.filled.max(recorded);
        let final_state = if sold > 0 {
            "FILLED"
        } else if r.status == "REJECTED" {
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
                        "{}: {sold} of {} sold",
                        r.status,
                        exit_order.s("quantity")
                    )),
                )
                .await?;
        }
        if final_state != "FILLED" {
            store
                .update_position(
                    position_id,
                    Rec::of([
                        ("exit_queued_for", Val::None),
                        ("exit_reason_queued", Val::None),
                    ]),
                )
                .await?;
        }
        return Ok(Some(ExecOutcome::new(
            final_state,
            format!("{symbol}: sell {}", r.status),
            None,
            None,
            (None, Some(position_id)),
            gates.dry_run,
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
        return Ok(Some(ExecOutcome::new(
            "FILLED",
            format!("{symbol}: sold {} at {}", r.filled, dstr(r.average)),
            None,
            None,
            (None, Some(position_id)),
            gates.dry_run,
        )));
    }
    if wrote {
        return Ok(Some(ExecOutcome::new(
            "SENT",
            format!(
                "{symbol}: {} of {} sold so far",
                r.filled,
                exit_order.s("quantity")
            ),
            None,
            None,
            (None, Some(position_id)),
            gates.dry_run,
        )));
    }
    Ok(None)
}
