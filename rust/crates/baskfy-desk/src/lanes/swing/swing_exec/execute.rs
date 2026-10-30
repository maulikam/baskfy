//! Ports `app/swing_execute.py` (SW7, SW10.4, SW10.5): one confirmed plan line becomes orders,
//! a stop, and rows — through the gateway it is handed, and nothing else.
//!
//! The rules it enforces, as Python does: no auto-execution (`confirm == "true"`, a `plan_id`
//! issued in the last thirty minutes, a `PROPOSED` line; 400 / 404 / 410 / 409); the sleeve
//! never sells what it did not buy (checked against `sw_position`, never the broker's
//! holdings); a stop never falls; every BUY's GTT is armed in the same call; with
//! `BASKFY_SWING_EXECUTION_ENABLED=false` the same path runs through the gateway's dry-run
//! branch whatever `DRY_RUN` says ([`swing_gates`]); the confirm-time gate (A5) re-sizes under
//! the day's session lock; A8's entry cap is sent as MARKET with `market_protection`, a price
//! past the cap is refused; A9's half risk applies only for a real order.
//!
//! Every order — the buy, the sell, the stop, its cancellation, its re-sizing, the cancel of an
//! open remainder — goes through the [`Gateway`]. Nothing here names a broker method.

use async_trait::async_trait;
use baskfy_core::swing::plan::{SkipReason, WatchItem, first_live_multiplier};
use baskfy_core::swing::setups::{Setup, SwingConfig, TRADEABLE_SETUPS};
use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use super::super::context::{SignalContext, entries_now};
use super::super::rules::{
    closed_pnl, dec_of_float, exit_average, float_g, market_protection_pct, marketable_limit,
    money, py_dec, q2, quantize, r_multiple, skip_str,
};
use super::super::store::{Fields, LineRow, PositionRow, SleeveConfig, StoreError, SwingStore};
use super::super::values::Val;
use crate::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder, ProductGates,
    StopBand, TenantIds, gtt_status,
};
use crate::settings::Settings;
use crate::wire::{PyDict, PyValue};

/// `SWING_JOURNAL_NAME`: the swing book's own journal, beside the weekly book's.
pub const SWING_JOURNAL_NAME: &str = "swing_orders_journal.jsonl";

/// `BUY_ON_TRIGGER`.
pub const BUY_ON_TRIGGER: &str = "BUY_ON_TRIGGER";
/// `SELL_AT_OPEN`.
pub const SELL_AT_OPEN: &str = "SELL_AT_OPEN";
/// `RAISE_GTT_STOP`.
pub const RAISE_GTT_STOP: &str = "RAISE_GTT_STOP";
/// `PENDING_RANGE` (A7): never executed.
pub const PENDING_RANGE: &str = "PENDING_RANGE";
/// `EXECUTABLE_KINDS`: the kinds a confirm may turn into an order. `PENDING_RANGE` is not one.
pub const EXECUTABLE_KINDS: [&str; 3] = [BUY_ON_TRIGGER, SELL_AT_OPEN, RAISE_GTT_STOP];
/// `ENTRY_ORDER_TYPES`: the only two order types the swing book can ever send (Track C).
pub const ENTRY_ORDER_TYPES: [&str; 2] = ["LIMIT", "MARKET"];

/// `ORDER_COMPLETE`.
pub const ORDER_COMPLETE: &str = "COMPLETE";
/// `ORDER_DEAD_STATUSES`.
pub const ORDER_DEAD_STATUSES: [&str; 3] = ["REJECTED", "CANCELLED", "CANCEL PENDING"];

/// `_KNOWN_CLOSE_REASONS`.
const KNOWN_CLOSE_REASONS: [&str; 7] = [
    "PARTIAL_INTO_STRENGTH",
    "BREAKEVEN_AFTER_PARTIAL",
    "BREAKEVEN_AT_R",
    "CLOSE_BELOW_TRAIL_MA",
    "EP_FAILED_RED_ON_DAY",
    "HARD_STOP_HIT",
    "NOTHING_TO_DO",
];

/// `_SIMULATED_GTT_PREFIX`.
const SIMULATED_GTT_PREFIX: &str = "DRY-";

/// The statuses the GTT paths accept (`baskfy_execution.gtt`).
const GTT_PLACED_STATUSES: [&str; 2] = [gtt_status::GTT_PLACED, gtt_status::DRY_RUN_GTT];
const GTT_DELETED_STATUSES: [&str; 2] = [gtt_status::GTT_DELETED, gtt_status::DRY_RUN_GTT_DELETE];
const GTT_MODIFIED_STATUSES: [&str; 2] = [gtt_status::GTT_MODIFIED, gtt_status::DRY_RUN_GTT_MODIFY];
const ORDER_CANCELLED_STATUSES: [&str; 2] = [
    gtt_status::ORDER_CANCELLED,
    gtt_status::ORDER_CANCEL_DRY_RUN,
];

/// `_ORDER_STATUS`: the gateway's statuses onto C1's.
fn order_status(gateway_status: &str) -> &'static str {
    match gateway_status {
        "DRY_RUN" => "SIMULATED",
        "PLACED" => "SENT",
        "BLOCKED" | "RISK_BLOCKED" | "DUPLICATE" => "BLOCKED",
        _ => "REJECTED",
    }
}

/// An HTTP refusal raised before anything is touched (`HTTPException`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{status}: {detail}")]
pub struct HttpRefusal {
    /// 400 / 404 / 409 / 410.
    pub status: u16,
    /// The detail sentence.
    pub detail: String,
}

/// What can stop a confirm.
#[derive(Debug, thiserror::Error)]
pub enum ExecError {
    /// A caller's refusal (`HTTPException`).
    #[error(transparent)]
    Http(#[from] HttpRefusal),
    /// The store.
    #[error(transparent)]
    Store(#[from] StoreError),
    /// The gateway raised (an untouchable instrument, a caller's bug).
    #[error(transparent)]
    Gateway(#[from] GatewayError),
}

fn http(status: u16, detail: impl Into<String>) -> ExecError {
    ExecError::Http(HttpRefusal {
        status,
        detail: detail.into(),
    })
}

/// `ExecOutcome`: what one confirmed line came to (C1's vocabulary).
#[derive(Debug, Clone, PartialEq)]
pub struct ExecOutcome {
    /// SIMULATED / SENT / FILLED / BLOCKED / EXPIRED / REJECTED.
    pub status: String,
    /// A sentence; `""` on success.
    pub reason: String,
    /// The gateway's `place()` result.
    pub order: Option<PyDict>,
    /// The gateway's GTT result.
    pub gtt: Option<PyDict>,
    /// The position.
    pub position_id: Option<i64>,
    /// Whether the book was written `simulated=true`.
    pub simulated: bool,
    /// A8: shares filled when the request answered.
    pub filled_quantity: i64,
}

impl ExecOutcome {
    fn new(
        status: &str,
        reason: impl Into<String>,
        order: Option<PyDict>,
        gtt: Option<PyDict>,
        position_id: Option<i64>,
        simulated: bool,
    ) -> Self {
        Self {
            status: status.to_owned(),
            reason: reason.into(),
            order,
            gtt,
            position_id,
            simulated,
            filled_quantity: 0,
        }
    }

    fn filled(mut self, quantity: i64) -> Self {
        self.filled_quantity = quantity;
        self
    }

    /// `dataclasses.asdict(outcome)` in field order.
    #[must_use]
    pub fn to_py(&self) -> PyDict {
        let mut m = PyDict::new();
        m.insert("status", PyValue::Str(self.status.clone()));
        m.insert("reason", PyValue::Str(self.reason.clone()));
        m.insert(
            "order",
            self.order.clone().map_or(PyValue::None, PyValue::Map),
        );
        m.insert("gtt", self.gtt.clone().map_or(PyValue::None, PyValue::Map));
        m.insert(
            "position_id",
            self.position_id.map_or(PyValue::None, PyValue::Int),
        );
        m.insert("simulated", PyValue::Bool(self.simulated));
        m.insert("filled_quantity", PyValue::Int(self.filled_quantity));
        m
    }
}

/// `OrderReport`: one order as the broker reports it.
#[derive(Debug, Clone, PartialEq)]
pub struct OrderReport {
    /// Kite's status.
    pub status: String,
    /// `filled_quantity`.
    pub filled_quantity: i64,
    /// `average_price`.
    pub average_price: Decimal,
}

/// `OrderSource`: where the confirm asks how its order is doing (a READ).
#[async_trait]
pub trait OrderSource: Send + Sync {
    /// The order's current state.
    async fn order_status(&self, order_id: &str) -> OrderReport;
}

/// The fill poll's clock and sleep (A8), injectable so a test runs ten seconds in no time.
#[async_trait]
pub trait PollTimer: Send + Sync {
    /// `time.monotonic()`.
    fn now(&self) -> f64;
    /// `asyncio.sleep(seconds)`.
    async fn sleep(&self, seconds: f64);
}

/// The real poll timer.
#[derive(Debug, Default, Clone, Copy)]
pub struct TokioTimer;

#[async_trait]
impl PollTimer for TokioTimer {
    fn now(&self) -> f64 {
        baskfy_providers::clock::Clock::monotonic(&baskfy_providers::clock::SystemClock)
    }

    async fn sleep(&self, seconds: f64) {
        tokio::time::sleep(std::time::Duration::from_secs_f64(seconds.max(0.0))).await;
    }
}

/// `swing_gates()`: dry run unless BOTH `DRY_RUN` is off AND `BASKFY_SWING_EXECUTION_ENABLED`
/// is on; intraday and options false unconditionally (CNC-only, Track C §1).
#[must_use]
pub const fn swing_gates(settings: &Settings) -> ProductGates {
    ProductGates {
        dry_run: settings.dry_run || !settings.swing_execution_enabled,
        intraday_enabled: false,
        options_enabled: false,
        fno_carry_enabled: false,
    }
}

/// `swing_stop_band()`: PACK.3's 0.5–10 % from the settings, not the weekly 8–12 %.
///
/// # Errors
/// An inverted band in the settings.
pub fn swing_stop_band(settings: &Settings) -> Result<StopBand, crate::gateway::ClientIdError> {
    StopBand::new(settings.swing_stop_band_min, settings.swing_stop_band_max)
}

/// `swing_journal_path()`: beside the desk's journal.
#[must_use]
pub fn swing_journal_path() -> String {
    let dir = std::path::Path::new(crate::gateway::JOURNAL)
        .parent()
        .map_or_else(String::new, |p| p.display().to_string());
    if dir.is_empty() {
        SWING_JOURNAL_NAME.to_owned()
    } else {
        format!("{dir}/{SWING_JOURNAL_NAME}")
    }
}

/// `_sole_tenant()`: the one account this run trades. Python's `TenantIds(...)` raises
/// `ValueError` on a non-positive id BEFORE any gateway call — on the GTT paths too, which the
/// desk shim does not stamp — so this refuses rather than handing the gateway `None`.
fn sole_tenant(settings: &Settings) -> Result<Option<TenantIds>, GatewayError> {
    TenantIds::new(settings.sole_user_id, settings.sole_broker_account_id)
        .map(Some)
        .map_err(|e| GatewayError {
            python_type: "ValueError".to_owned(),
            message: e.0.trim_start_matches("ValueError: ").to_owned(),
        })
}

/// `_price(value)` on a column Python reads unconditionally: `Decimal(str(None))` raises, so a
/// missing price is an error here too — never a zero that would arm a trigger at 0.
fn required(value: Option<Decimal>, what: &str) -> Result<Decimal, StoreError> {
    value.ok_or_else(|| StoreError::Value(format!("{what} is None")))
}

fn session_day(now: &DateTime<FixedOffset>) -> NaiveDate {
    now.with_timezone(&crate::clock::ist()).date_naive()
}

fn status_of(result: &PyDict) -> String {
    match result.get("status") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

fn py_str_value(v: Option<&PyValue>) -> String {
    match v {
        None | Some(PyValue::None) => "None".to_owned(),
        Some(PyValue::Str(s)) => s.clone(),
        Some(PyValue::Int(i)) => i.to_string(),
        Some(PyValue::Float(f)) => baskfy_core::py::float_repr(*f),
        Some(PyValue::Bool(b)) => if *b { "True" } else { "False" }.to_owned(),
        Some(other) => format!("{other:?}"),
    }
}

/// `_blocked_reason`: the gateway's own words for a refusal, in one sentence.
fn blocked_reason(result: &PyDict, what: &str) -> String {
    let status = match result.get("status") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => "?".to_owned(),
    };
    if status == "DUPLICATE" {
        let reference = result.get("order_id").or_else(|| result.get("gtt_id"));
        return format!(
            "{what} already sent for this plan ({status}: {})",
            py_str_value(reference)
        );
    }
    let error = match result.get("error") {
        Some(PyValue::Str(s)) if !s.is_empty() => s.clone(),
        Some(v @ (PyValue::Int(_) | PyValue::Float(_))) => py_str_value(Some(v)),
        _ => status.clone(),
    };
    format!("{what} refused by the gateway ({status}): {error}")
}

/// `risk_multiplier_for` (A9): 0.5 while the countdown runs AND the order would be real.
#[must_use]
pub fn risk_multiplier_for(config: &SleeveConfig, simulated: bool) -> Decimal {
    first_live_multiplier(
        config.first_live_sessions_left,
        !simulated,
        &SwingConfig::default().sizing,
    )
    .unwrap_or(Decimal::ONE)
}

/// `is_simulated_gtt`.
#[must_use]
pub fn is_simulated_gtt(gtt_id: Option<&str>) -> bool {
    gtt_id.is_some_and(|g| g.starts_with(SIMULATED_GTT_PREFIX))
}

/// `sizing_config`: the pack's defaults with the person's three sizing knobs, `risk_per_trade`
/// scaled by `risk_multiplier`.
#[must_use]
pub fn sizing_config(config: &SleeveConfig, risk_multiplier: Decimal) -> SwingConfig {
    let mut out = SwingConfig::default();
    let defaults = baskfy_core::swing::sizing::SizingConfig::default();
    let risk = config.risk_per_trade_pct * risk_multiplier;
    out.sizing = defaults;
    out.sizing.risk_per_trade_pct = risk.to_string().parse().unwrap_or(0.5);
    out.sizing.max_position_pct = config.max_position_pct.to_string().parse().unwrap_or(20.0);
    out.sizing.max_open_positions = config.max_open_positions;
    out
}

/// `Resized`: the size to send, or the refusal.
#[derive(Debug, Clone, PartialEq)]
pub struct Resized {
    /// Shares.
    pub quantity: i64,
    /// Rupees at risk.
    pub risk_inr: Decimal,
    /// Rupees deployed.
    pub position_value: Decimal,
    /// `""` when sized; else the skip code.
    pub refusal: String,
    /// The skip's detail, or how the size changed.
    pub detail: String,
    /// The line was shrunk.
    pub changed: bool,
}

fn refused(code: &str, detail: impl Into<String>) -> Resized {
    Resized {
        quantity: 0,
        risk_inr: Decimal::ZERO,
        position_value: Decimal::ZERO,
        refusal: code.to_owned(),
        detail: detail.into(),
        changed: false,
    }
}

fn setup_from(value: &str) -> Option<Setup> {
    match value {
        "FLAG" => Some(Setup::Flag),
        "EP" => Some(Setup::Ep),
        "PARABOLIC_SHORT" => Some(Setup::ParabolicShort),
        _ => None,
    }
}

fn pct_of(value: Decimal, equity: Decimal) -> String {
    if equity <= Decimal::ZERO {
        return "n/a".to_owned();
    }
    format!("{}%", py_dec(q2(value / equity * Decimal::ONE_HUNDRED)))
}

/// `resize_buy`: re-size one BUY line against the book as it is now. Never above the line.
#[must_use]
pub fn resize_buy(
    line: &LineRow,
    context: &SignalContext,
    config: &SwingConfig,
    day: NaiveDate,
    live: bool,
) -> Resized {
    let symbol = line.symbol.clone();
    let setup_name = line.setup.clone();
    let Some(setup) = setup_name.as_deref().and_then(setup_from) else {
        let repr = setup_name
            .as_deref()
            .map_or_else(|| "None".to_owned(), baskfy_providers::py::repr_str);
        return refused(
            &skip_str(SkipReason::NotTradeableSetup),
            format!("setup {repr} is not one this book trades"),
        );
    };
    if !TRADEABLE_SETUPS.contains(&setup) {
        return refused(&skip_str(SkipReason::NotTradeableSetup), setup.as_str());
    }
    let Some((adr, turnover, score)) = context.detected.get(&symbol).cloned() else {
        return refused(
            &skip_str(SkipReason::SizeRefused),
            format!(
                "no detection row for {symbol} before {} — its ADR is unknown",
                day.format("%Y-%m-%d")
            ),
        );
    };
    let (Some(trigger), Some(stop)) = (line.trigger, line.stop) else {
        return refused(&skip_str(SkipReason::SizeRefused), "no trigger or stop");
    };
    let item = WatchItem {
        symbol: symbol.clone(),
        setup,
        trigger,
        stop_ref: Some(stop),
        adr_pct: adr,
        avg_turnover_inr: turnover,
        score,
        locked_upper_circuit: false,
    };
    let Ok((lines, skipped)) = entries_now(&item, context, config, day, live) else {
        return refused(
            &skip_str(SkipReason::SizeRefused),
            "the sizing rules failed",
        );
    };
    let Some(first) = lines.first() else {
        return skipped.first().map_or_else(
            || refused(&skip_str(SkipReason::SizeRefused), ""),
            |s| refused(&skip_str(s.reason), s.detail.clone()),
        );
    };
    let allowed = first.quantity;
    let planned = line.quantity;
    let quantity = planned.min(allowed);
    let risk = q2((trigger - stop) * Decimal::from(quantity));
    let value = q2(trigger * Decimal::from(quantity));
    if quantity == planned {
        return Resized {
            quantity,
            risk_inr: risk,
            position_value: value,
            refusal: String::new(),
            detail: format!("{quantity} as planned"),
            changed: false,
        };
    }
    let account = &context.account;
    let note_part = first.note.split("; ").nth(2).unwrap_or("").to_owned();
    let entries = context.entries_today;
    Resized {
        quantity,
        risk_inr: risk,
        position_value: value,
        refusal: String::new(),
        detail: format!(
            "re-sized at confirm {planned} → {quantity} ({note_part}): book ₹{} + ₹{} = {} of the \
             sleeve, ceiling {}% at rung {}, {entries} entr{} today",
            money(account.open_exposure_inr),
            money(value),
            pct_of(account.open_exposure_inr + value, account.equity),
            float_g(context.tier.max_exposure_pct),
            context.tier.level,
            if entries == 1 { "y" } else { "ies" },
        ),
        changed: true,
    }
}

fn broker_gtt_id(gtt_id: Option<&str>) -> Option<i64> {
    let g = gtt_id?;
    if is_simulated_gtt(Some(g)) {
        return None;
    }
    g.trim().parse().ok()
}

/// Everything a confirm needs besides the store and the gateway.
pub struct ExecContext<'a> {
    /// The settings at the moment of the confirm.
    pub settings: &'a Settings,
    /// The order source for the fill poll (`None` without a session).
    pub orders: Option<&'a dyn OrderSource>,
    /// The poll's clock and sleep.
    pub timer: &'a dyn PollTimer,
}

/// `_validate`: the four refusals that are the caller's, not the gateway's.
async fn validate(
    store: &mut dyn SwingStore,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    now: &DateTime<FixedOffset>,
) -> Result<LineRow, ExecError> {
    if confirm != "true" {
        return Err(http(400, "Execution requires explicit confirmation."));
    }
    let Some(plan) = store.plan(plan_id).await? else {
        return Err(http(404, "Unknown plan_id — reload the swing page."));
    };
    let line = store.line(line_id).await?;
    let Some(line) = line.filter(|l| l.plan_id == plan_id) else {
        return Err(http(
            404,
            "Unknown line_id for this plan — reload the swing page.",
        ));
    };
    if !EXECUTABLE_KINDS.contains(&line.kind.as_str()) {
        return Err(http(
            400,
            format!(
                "A {} line cannot be executed — it is information only; the SIGNAL plan at \
                 range close is the line.",
                line.kind
            ),
        ));
    }
    // `_aware(plan["expires_at"])` on a None raises `AttributeError` before the line is
    // touched — FastAPI's 500, not a 410 (DECISIONS-RS R8.15.1). The column is NOT NULL in both
    // schemas, so only a hand-built store reaches this; it fails closed either way.
    let Some(expires_at) = plan.expires_at else {
        return Err(ExecError::Store(StoreError::Value(
            "'NoneType' object has no attribute 'tzinfo'".to_owned(),
        )));
    };
    if *now > expires_at {
        if line.state == "PROPOSED" {
            store.set_line(line_id, "EXPIRED", None, None).await?;
        }
        return Err(http(
            410,
            "Plan older than 30 minutes — prices stale, wait for the next plan.",
        ));
    }
    if line.state != "PROPOSED" {
        return Err(http(
            409,
            format!(
                "Line is {}, not PROPOSED — it cannot be confirmed twice.",
                line.state
            ),
        ));
    }
    Ok(line)
}

/// `execute_line`: execute one confirmed plan line through the gateway and write what happened.
///
/// Refuses 400 / 404 / 410 / 409 before anything is touched. Past that the whole confirm runs
/// under the day's session lock; the line is marked `CONFIRMED` before the gateway is called.
///
/// # Errors
/// A refusal (`ExecError::Http`), or a store/gateway failure after which the line is marked
/// `REJECTED` and the click counted, as Python does.
#[allow(clippy::too_many_arguments)]
pub async fn execute_line(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    cx: &ExecContext<'_>,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> Result<ExecOutcome, ExecError> {
    execute_line_observed(
        store,
        gw,
        cx,
        plan_id,
        line_id,
        confirm,
        now,
        last_price,
        &DeskTelemetry,
    )
    .await
}

/// SW11's observability sink for one confirm (`app.telemetry`'s `span`, `count`, `observe`,
/// `capture`), as a seam: an `Err` is a sink that raised. [`DeskTelemetry`] is the desk's own
/// (the library-absent branch, which never fails); a test hands one that fails every call.
pub trait ConfirmTelemetry: Send + Sync {
    /// `telemetry.span(name, **attributes)`.
    ///
    /// # Errors
    /// The sink raised.
    fn span(&self, name: &str, attributes: &[(&str, &str)]) -> Result<(), String>;
    /// `telemetry.count(metric, **labels)`.
    ///
    /// # Errors
    /// The sink raised.
    fn count(&self, metric: &str, labels: &[(&str, &str)]) -> Result<(), String>;
    /// `telemetry.observe(metric, value)`.
    ///
    /// # Errors
    /// The sink raised.
    fn observe(&self, metric: &str, value: f64) -> Result<(), String>;
    /// `telemetry.capture(exc, **context)`.
    ///
    /// # Errors
    /// The sink raised.
    fn capture(&self, error: &str, context: &[(&str, &str)]) -> Result<(), String>;
}

/// The desk's telemetry: `lanes::session::telemetry`, which cannot fail.
#[derive(Debug, Default, Clone, Copy)]
pub struct DeskTelemetry;

impl ConfirmTelemetry for DeskTelemetry {
    fn span(&self, name: &str, attributes: &[(&str, &str)]) -> Result<(), String> {
        crate::lanes::session::telemetry::span(name, attributes, |_| ());
        Ok(())
    }

    fn count(&self, metric: &str, labels: &[(&str, &str)]) -> Result<(), String> {
        crate::lanes::session::telemetry::count(metric, labels);
        Ok(())
    }

    fn observe(&self, metric: &str, value: f64) -> Result<(), String> {
        crate::lanes::session::telemetry::observe(metric, value);
        Ok(())
    }

    fn capture(&self, error: &str, context: &[(&str, &str)]) -> Result<(), String> {
        crate::lanes::session::telemetry::capture(&error, context);
        Ok(())
    }
}

/// `_span`: a span is never a reason not to trade — a sink that raises is a span not taken.
fn observed_span(tel: &dyn ConfirmTelemetry, name: &str, attributes: &[(&str, &str)]) {
    let _ = tel.span(name, attributes);
}

/// `_observe_confirm`: one `try` around both, as Python's (a raising `count` skips `observe`).
fn observe_confirm(tel: &dyn ConfirmTelemetry, kind: &str, outcome: &str, seconds: f64) {
    let _ = tel
        .count("swing_confirms", &[("kind", kind), ("outcome", outcome)])
        .and_then(|()| tel.observe("swing_confirm_seconds", seconds));
}

/// `_capture`: error capture failing is not a second error.
fn capture_error(tel: &dyn ConfirmTelemetry, error: &ExecError, kind: &str, line_id: i64) {
    let line = line_id.to_string();
    let _ = tel.capture(&error.to_string(), &[("kind", kind), ("line_id", &line)]);
}

/// [`execute_line`] with its telemetry sink named: what the confirm does is the same whatever
/// the sink does (`test_telemetry_never_raises_into_execute_line`).
///
/// # Errors
/// As [`execute_line`].
#[allow(clippy::too_many_arguments)]
pub async fn execute_line_observed(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    cx: &ExecContext<'_>,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
    tel: &dyn ConfirmTelemetry,
) -> Result<ExecOutcome, ExecError> {
    let validated = validate(store, plan_id, line_id, confirm, &now).await?;
    let simulated = swing_gates(cx.settings).dry_run;
    let day = session_day(&now);
    let mode = if simulated { "DRY_RUN" } else { "LIVE" };
    let kind = validated.kind;
    let started = std::time::Instant::now();
    observed_span(
        tel,
        "swing.execute_line",
        &[
            ("kind", kind.as_str()),
            ("simulated", if simulated { "True" } else { "False" }),
        ],
    );
    let body = async {
        // `with store.lock_session_for_update(day):` — entering it can raise too (the store
        // rolls its own `BEGIN` back), and that is the `except Exception` branch below.
        store.lock_begin(day).await?;
        let inner = async {
            let current = store.line(line_id).await?;
            let Some(line) = current.filter(|c| c.state == "PROPOSED") else {
                let state = store
                    .line(line_id)
                    .await?
                    .map_or_else(|| "?".to_owned(), |c| c.state);
                return Err(http(
                    409,
                    format!("Line is {state}, not PROPOSED — it cannot be confirmed twice."),
                ));
            };
            let context = if kind == BUY_ON_TRIGGER {
                Some(store.session_context(day).await?)
            } else {
                None
            };
            store.set_line(line_id, "CONFIRMED", None, None).await?;
            store.bump_session(day, mode, 1, 0, 0).await?;
            let outcome = match kind.as_str() {
                BUY_ON_TRIGGER => {
                    buy(
                        store, gw, cx, &line, plan_id, now, simulated, context, last_price,
                    )
                    .await?
                }
                SELL_AT_OPEN => {
                    sell(store, gw, cx, &line, plan_id, now, simulated, last_price).await?
                }
                RAISE_GTT_STOP => {
                    raise_stop(store, gw, cx, &line, plan_id, now, simulated, last_price).await?
                }
                other => ExecOutcome::new(
                    "BLOCKED",
                    format!(
                        "{} is not a kind this desk executes",
                        baskfy_providers::py::repr_str(other)
                    ),
                    None,
                    None,
                    None,
                    simulated,
                ),
            };
            record_line(store, line_id, &outcome).await?;
            let is_fill = matches!(outcome.status.as_str(), "SIMULATED" | "FILLED");
            // A buy or a sell that filled is a fill; so is a partial (A8). Any other success is a
            // manage action.
            let bought_or_sold = is_fill && (kind == BUY_ON_TRIGGER || kind == SELL_AT_OPEN);
            if bought_or_sold || (outcome.status == "SENT" && outcome.filled_quantity > 0) {
                store.bump_session(day, mode, 0, 1, 0).await?;
            } else if is_fill {
                store.bump_session(day, mode, 0, 0, 1).await?;
            }
            Ok::<ExecOutcome, ExecError>(outcome)
        }
        .await;
        match inner {
            // The lock's `else: COMMIT` — a failed commit raises inside the `with` as well.
            Ok(outcome) => store
                .lock_commit()
                .await
                .map(|()| outcome)
                .map_err(Into::into),
            Err(e) => {
                store.lock_rollback().await?;
                Err(e)
            }
        }
    }
    .await;
    let seconds = || started.elapsed().as_secs_f64();
    match body {
        Ok(outcome) => {
            observe_confirm(tel, &kind, &outcome.status, seconds());
            Ok(outcome)
        }
        Err(ExecError::Http(refusal)) => {
            observe_confirm(tel, &kind, "REFUSED", seconds());
            Err(ExecError::Http(refusal))
        }
        Err(e) => {
            // Not swallowed: the transaction rolled back with the lock, then the line is
            // marked so it cannot be re-posted, the click is counted, and the error goes up.
            store.set_line(line_id, "REJECTED", None, None).await?;
            store.bump_session(day, mode, 1, 0, 0).await?;
            observe_confirm(tel, &kind, "ERROR", seconds());
            capture_error(tel, &e, &kind, line_id);
            Err(e)
        }
    }
}

/// `rearm_gtt`: arm a stop for a NAKED position — nothing else.
///
/// # Errors
/// 400 without confirm, 404 for an unknown position, the store's or the gateway's.
pub async fn rearm_gtt(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    position_id: i64,
    confirm: &str,
    now: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> Result<ExecOutcome, ExecError> {
    if confirm != "true" {
        return Err(http(400, "Arming a stop requires explicit confirmation."));
    }
    let Some(pos) = store.position(position_id).await? else {
        return Err(http(404, "Unknown position_id."));
    };
    let simulated = swing_gates(settings).dry_run;
    let blocked = |reason: String| {
        ExecOutcome::new("BLOCKED", reason, None, None, Some(position_id), simulated)
    };
    if let Some(g) = &pos.gtt_id {
        return Ok(blocked(format!(
            "{}: a GTT is already resting ({g}); re-arming would leave two",
            pos.symbol
        )));
    }
    let open_qty = pos.quantity_open;
    if open_qty <= 0 || pos.state == "CLOSED" {
        return Ok(blocked(format!(
            "{}: nothing is open to protect",
            pos.symbol
        )));
    }
    let stop = required(pos.stop, "sw_position.stop")?;
    let reference = match last_price {
        Some(p) => p,
        None => required(pos.entry_avg, "sw_position.entry_avg")?,
    };
    if stop >= reference {
        return Ok(blocked(format!(
            "{}: stop {} is not below {} {} — pass a last_price above it",
            pos.symbol,
            py_dec(stop),
            if last_price.is_some() {
                "the last price"
            } else {
                "the entry"
            },
            py_dec(reference)
        )));
    }
    let client_id = format!(
        "rearm:{position_id}:{}:GTT:{}",
        pos.symbol,
        session_day(&now).format("%Y-%m-%d")
    );
    let gtt = arm(
        gw,
        settings,
        &pos.symbol,
        open_qty,
        stop,
        reference,
        &client_id,
    )
    .await?;
    if !GTT_PLACED_STATUSES.contains(&status_of(&gtt).as_str()) {
        let reason = blocked_reason(&gtt, "the GTT");
        return Ok(ExecOutcome::new(
            "BLOCKED",
            reason,
            None,
            Some(gtt),
            Some(position_id),
            simulated,
        ));
    }
    store
        .update_position(position_id, gtt_fields(&gtt, stop, now))
        .await?;
    store
        .bump_session(
            session_day(&now),
            if simulated { "DRY_RUN" } else { "LIVE" },
            0,
            0,
            1,
        )
        .await?;
    Ok(ExecOutcome::new(
        if simulated { "SIMULATED" } else { "FILLED" },
        "",
        None,
        Some(gtt),
        Some(position_id),
        simulated,
    ))
}

async fn open_entries_in(
    store: &mut dyn SwingStore,
    instrument_id: i64,
) -> Result<i64, StoreError> {
    Ok(i64::try_from(
        store
            .open_positions()
            .await?
            .iter()
            .filter(|r| r.instrument_id == instrument_id && r.quantity_open > 0)
            .count(),
    )
    .unwrap_or(0))
}

/// `_held_refusal` (LV10): a fresh signal in a held name is a new entry, up to the cap.
async fn held_refusal(
    store: &mut dyn SwingStore,
    instrument_id: i64,
) -> Result<Option<String>, StoreError> {
    let sizing = SwingConfig::default().sizing;
    let held = open_entries_in(store, instrument_id).await?;
    if held > 0 && !sizing.pyramiding {
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

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
async fn buy(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    cx: &ExecContext<'_>,
    line: &LineRow,
    plan_id: &str,
    now: DateTime<FixedOffset>,
    simulated: bool,
    context: Option<SignalContext>,
    last_price: Option<Decimal>,
) -> Result<ExecOutcome, ExecError> {
    let symbol = line.symbol.clone();
    let quantity = line.quantity;
    let blocked = |reason: String| ExecOutcome::new("BLOCKED", reason, None, None, None, simulated);
    if quantity <= 0 {
        return Ok(blocked(format!(
            "{symbol}: a line for {quantity} shares is not a buy"
        )));
    }
    let (Some(trigger), Some(stop)) = (line.trigger, line.stop) else {
        return Ok(blocked(format!(
            "{symbol}: a buy line needs a trigger and a stop"
        )));
    };
    if stop >= trigger {
        return Ok(blocked(format!(
            "{symbol}: stop {} is not below entry {}",
            py_dec(stop),
            py_dec(trigger)
        )));
    }
    if let Some(held) = held_refusal(store, line.instrument_id).await? {
        return Ok(blocked(format!("{symbol}: {held}")));
    }
    let issues = store.open_protection_issues().await?;
    if !issues.is_empty() {
        let reasons: Vec<String> = issues
            .iter()
            .map(|i| format!("{}: {} — {}", i.symbol, i.kind, i.detail))
            .collect();
        return Ok(blocked(format!(
            "PROTECTION_UNRESOLVED: {}",
            reasons.join("; ")
        )));
    }
    let config = store.config().await?;
    let context = match context {
        Some(c) => c,
        None => store.session_context(session_day(&now)).await?,
    };
    let multiplier = first_live_multiplier(
        context.first_live_sessions_left,
        !simulated,
        &SwingConfig::default().sizing,
    )
    .unwrap_or(Decimal::ONE);
    let sized = resize_buy(
        line,
        &context,
        &sizing_config(&config, Decimal::ONE),
        session_day(&now),
        !simulated,
    );
    if !sized.refusal.is_empty() {
        return Ok(blocked(format!(
            "{}: {symbol} — {}",
            sized.refusal, sized.detail
        )));
    }
    if sized.changed {
        store
            .resize_line(
                line.id,
                sized.quantity,
                sized.risk_inr,
                sized.position_value,
                &sized.detail,
            )
            .await?;
    }
    let qty = sized.quantity;
    let window = SwingConfig::default().opening_range;
    let adr = context
        .detected
        .get(&symbol)
        .map_or(Decimal::ZERO, |(a, _, _)| *a);
    let range_high = store.range_high_for(line.id).await?;
    let cap = marketable_limit(trigger, range_high, adr, &window);
    let order_type = window.entry_order_type.clone();
    if !ENTRY_ORDER_TYPES.contains(&order_type.as_str()) {
        return Ok(blocked(format!(
            "{symbol}: entry_order_type {} is not one of ['LIMIT', 'MARKET']",
            baskfy_providers::py::repr_str(&order_type)
        )));
    }
    let reference = match last_price {
        Some(p) => p,
        None if !simulated => {
            return Ok(blocked(format!(
                "{symbol}: no live price — a market entry is not sent without one (no Kite \
                 session, or the quote read failed)"
            )));
        }
        None => trigger,
    };
    let protection = market_protection_pct(cap, reference, &window);
    if protection.is_none() && !simulated {
        return Ok(blocked(format!(
            "{symbol}: the price has run past the entry cap — {} is at or above {} (trigger {}). \
             Not chased; the setup is gone for today, not cheaper later",
            py_dec(reference),
            py_dec(cap),
            py_dec(trigger)
        )));
    }
    let to_f = |d: Decimal| -> f64 { d.to_string().parse().unwrap_or(0.0) };
    let order = gw
        .place(PlaceOrder {
            symbol: symbol.clone(),
            qty,
            side: "BUY".to_owned(),
            product: "CNC".to_owned(),
            order_type: order_type.clone(),
            price: if order_type == "LIMIT" {
                Some(to_f(cap))
            } else {
                None
            },
            exchange: "NSE".to_owned(),
            client_id: Some(format!("{plan_id}:{symbol}:BUY")),
            gross_exposure: Some(to_f(cap * Decimal::from(qty))),
            market_protection: protection.map(to_f),
            reference_price: Some(to_f(reference)),
            tenant: sole_tenant(cx.settings)?,
            plan_tenant: sole_tenant(cx.settings)?,
            ..PlaceOrder::default()
        })
        .await?;
    let status = order_status(&status_of(&order));
    if status != "SIMULATED" && status != "SENT" {
        let reason = blocked_reason(&order, "the buy");
        return Ok(ExecOutcome::new(
            status,
            reason,
            Some(order),
            None,
            None,
            simulated,
        ));
    }
    let half_risk = multiplier < Decimal::ONE;
    if status == "SIMULATED" {
        return apply_buy_fill(
            store,
            gw,
            cx.settings,
            line,
            plan_id,
            now,
            order,
            qty,
            trigger,
            true,
            true,
            half_risk,
        )
        .await;
    }
    let order_id = py_str_value(order.get("order_id"));
    let report = poll_fill(cx.orders, &order_id, cx.timer).await;
    let Some(report) = report else {
        return Ok(ExecOutcome::new(
            "SENT",
            format!(
                "{symbol}: order {order_id} accepted, no order source to poll — no position, no \
                 stop, until a fill is reported"
            ),
            Some(order),
            None,
            None,
            simulated,
        ));
    };
    if ORDER_DEAD_STATUSES.contains(&report.status.as_str()) && report.filled_quantity <= 0 {
        return Ok(ExecOutcome::new(
            "REJECTED",
            format!(
                "{symbol}: order {order_id} is {} with nothing filled",
                report.status
            ),
            Some(order),
            None,
            None,
            simulated,
        ));
    }
    apply_buy_fill(
        store,
        gw,
        cx.settings,
        line,
        plan_id,
        now,
        order,
        report.filled_quantity,
        report.average_price,
        report.status == ORDER_COMPLETE,
        false,
        half_risk,
    )
    .await
}

/// `_poll_fill`: ≤ `fill_poll_seconds` at ≤ 1 / `fill_poll_interval_seconds` a second.
async fn poll_fill(
    orders: Option<&dyn OrderSource>,
    order_id: &str,
    timer: &dyn PollTimer,
) -> Option<OrderReport> {
    let orders = orders?;
    let window = SwingConfig::default().opening_range;
    let started = timer.now();
    loop {
        let last = orders.order_status(order_id).await;
        if last.status == ORDER_COMPLETE || ORDER_DEAD_STATUSES.contains(&last.status.as_str()) {
            return Some(last);
        }
        if timer.now() - started + window.fill_poll_interval_seconds > window.fill_poll_seconds {
            return Some(last);
        }
        timer.sleep(window.fill_poll_interval_seconds).await;
    }
}

#[allow(clippy::too_many_arguments)]
async fn apply_buy_fill(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    line: &LineRow,
    plan_id: &str,
    now: DateTime<FixedOffset>,
    order: PyDict,
    filled: i64,
    average: Decimal,
    complete: bool,
    simulated: bool,
    half_risk: bool,
) -> Result<ExecOutcome, ExecError> {
    let symbol = line.symbol.clone();
    let order_id = py_str_value(order.get("order_id"));
    if filled <= 0 {
        return Ok(ExecOutcome::new(
            "SENT",
            format!(
                "{symbol}: order {order_id} accepted, not yet filled — no position, no stop, \
                 until it fills"
            ),
            Some(order),
            None,
            None,
            simulated,
        ));
    }
    let stop = required(line.stop, "sw_plan_line.stop")?;
    let fields: Fields = vec![
        ("instrument_id", Val::Int(line.instrument_id)),
        ("symbol", Val::Text(symbol.clone())),
        ("setup", line.setup.clone().map_or(Val::Null, Val::Text)),
        ("entry_date", Val::Date(session_day(&now))),
        (
            "entry_avg",
            Val::Dec(quantize(
                average,
                4,
                rust_decimal::RoundingStrategy::MidpointNearestEven,
            )),
        ),
        ("quantity_entered", Val::Int(filled)),
        ("quantity_open", Val::Int(filled)),
        ("initial_stop", Val::Dec(stop)),
        ("stop", Val::Dec(stop)),
        ("gtt_id", Val::Null),
        ("gtt_trigger", Val::Null),
        ("gtt_armed_at", Val::Null),
        (
            "trail",
            Val::Text(
                line.trail
                    .clone()
                    .filter(|t| !t.is_empty())
                    .unwrap_or_else(|| "MA20".to_owned()),
            ),
        ),
        ("partial_done", Val::Bool(false)),
        ("partial_date", Val::Null),
        ("state", Val::Text("OPEN".to_owned())),
        ("closed_on", Val::Null),
        ("exit_avg", Val::Null),
        ("close_reason", Val::Null),
        ("r_multiple", Val::Null),
        ("pnl_inr", Val::Null),
        ("simulated", Val::Bool(simulated)),
        ("half_risk", Val::Bool(half_risk)),
    ];
    let position_id = store.create_position(fields).await?;
    let journal_ref = match order.get("order_id") {
        Some(PyValue::Str(s)) => Val::Text(s.clone()),
        Some(PyValue::Int(i)) => Val::Int(*i),
        _ => Val::Null,
    };
    store
        .add_fill(vec![
            ("position_id", Val::Int(position_id)),
            ("side", Val::Text("BUY".to_owned())),
            ("quantity", Val::Int(filled)),
            ("price", Val::Dec(average)),
            ("filled_at", Val::Stamp(now)),
            ("journal_ref", journal_ref),
            ("simulated", Val::Bool(simulated)),
        ])
        .await?;
    let gtt = arm(
        gw,
        settings,
        &symbol,
        filled,
        stop,
        average,
        &format!("{plan_id}:{symbol}:GTT"),
    )
    .await?;
    let status = if simulated {
        "SIMULATED"
    } else if complete {
        "FILLED"
    } else {
        "SENT"
    };
    let planned = line.quantity;
    let partial = if complete {
        String::new()
    } else {
        format!("; {filled} of {planned} filled so far, order {order_id} still open for the rest")
    };
    if GTT_PLACED_STATUSES.contains(&status_of(&gtt).as_str()) {
        store
            .update_position(position_id, gtt_fields(&gtt, stop, now))
            .await?;
        return Ok(ExecOutcome::new(
            status,
            partial.trim_start_matches([';', ' ']).to_owned(),
            Some(order),
            Some(gtt),
            Some(position_id),
            simulated,
        )
        .filled(filled));
    }
    let reason = format!(
        "{symbol}: bought {filled}, but the stop was not armed — {}; position {position_id} is \
         NAKED, re-arm it{partial}",
        blocked_reason(&gtt, "the GTT")
    );
    Ok(ExecOutcome::new(
        status,
        reason,
        Some(order),
        Some(gtt),
        Some(position_id),
        simulated,
    )
    .filled(filled))
}

/// `on_order_update`: the postback handler (A8) — anything that is not a `SENT` swing BUY's
/// order is ignored; under the session lock, grow the position and **modify** its GTT.
///
/// # Errors
/// The store's or the gateway's.
pub async fn on_order_update(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    update: &OrderUpdate,
    now: DateTime<FixedOffset>,
) -> Result<Option<ExecOutcome>, ExecError> {
    let order_id = update.order_id.clone();
    let line = if order_id.is_empty() {
        None
    } else {
        store.line_by_order(&order_id).await?
    };
    let Some(line) = line else { return Ok(None) };
    let day = session_day(&now);
    let simulated = swing_gates(settings).dry_run;
    let mode = if simulated { "DRY_RUN" } else { "LIVE" };
    store.lock_begin(day).await?;
    let body = async {
        let current = store.line(line.id).await?;
        let Some(current) = current.filter(|c| c.state == "SENT") else {
            return Ok(None);
        };
        apply_update(store, gw, settings, &current, update, now, mode)
            .await
            .map(Some)
    }
    .await;
    match body {
        Ok(v) => {
            store.lock_commit().await?;
            Ok(v)
        }
        Err(e) => {
            store.lock_rollback().await?;
            Err(e)
        }
    }
}

/// A broker order update (Kite's postback / order-book shape).
#[derive(Debug, Clone, PartialEq)]
pub struct OrderUpdate {
    /// `order_id`.
    pub order_id: String,
    /// `status`.
    pub status: String,
    /// `filled_quantity`.
    pub filled_quantity: i64,
    /// `average_price`.
    pub average_price: Decimal,
}

fn order_stub(order_id: &str, status: &str) -> PyDict {
    let mut m = PyDict::new();
    m.insert("order_id", PyValue::Str(order_id.to_owned()));
    m.insert("status", PyValue::Str(status.to_owned()));
    m
}

async fn apply_update(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    line: &LineRow,
    update: &OrderUpdate,
    now: DateTime<FixedOffset>,
    mode: &str,
) -> Result<ExecOutcome, ExecError> {
    let symbol = line.symbol.clone();
    let plan_id = line.plan_id.clone();
    let order_id = update.order_id.clone();
    let status = update.status.clone();
    let filled = update.filled_quantity;
    let average = update.average_price;
    let simulated = swing_gates(settings).dry_run;
    let config = store.config().await?;
    let half_risk = risk_multiplier_for(&config, simulated) < Decimal::ONE;
    let position = match line.position_id {
        Some(p) => store.position(p).await?,
        None => None,
    };
    let outcome = match position {
        None if filled <= 0 => ExecOutcome::new(
            "SENT",
            format!("{symbol}: {order_id} {status}, nothing filled"),
            Some(order_stub(&order_id, &status)),
            None,
            None,
            simulated,
        ),
        None => {
            let outcome = apply_buy_fill(
                store,
                gw,
                settings,
                line,
                &plan_id,
                now,
                order_stub(&order_id, &status),
                filled,
                average,
                status == ORDER_COMPLETE,
                simulated,
                half_risk,
            )
            .await?;
            store.bump_session(session_day(&now), mode, 0, 1, 0).await?;
            outcome
        }
        Some(position) => {
            grow_position(
                store, gw, settings, &position, line, &order_id, &status, filled, average, now,
                simulated,
            )
            .await?
        }
    };
    close_line_after_update(store, line, &outcome, &status, &order_id).await?;
    Ok(outcome)
}

#[allow(clippy::too_many_arguments)]
async fn grow_position(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    position: &PositionRow,
    line: &LineRow,
    order_id: &str,
    status: &str,
    filled: i64,
    average: Decimal,
    now: DateTime<FixedOffset>,
    simulated: bool,
) -> Result<ExecOutcome, ExecError> {
    let symbol = position.symbol.clone();
    let position_id = position.id;
    let entered = position.quantity_entered;
    let open_qty = position.quantity_open;
    let delta = filled - entered;
    let final_state = if status == ORDER_COMPLETE {
        "FILLED"
    } else {
        "SENT"
    };
    if delta <= 0 {
        return Ok(
            ExecOutcome::new(final_state, "", None, None, Some(position_id), simulated)
                .filled(entered),
        );
    }
    let old_avg = required(position.entry_avg, "sw_position.entry_avg")?;
    let four = |d: Decimal| quantize(d, 4, rust_decimal::RoundingStrategy::MidpointNearestEven);
    let mut delta_price = four(
        (average * Decimal::from(filled) - old_avg * Decimal::from(entered)) / Decimal::from(delta),
    );
    if delta_price <= Decimal::ZERO {
        delta_price = average;
    }
    let new_open = open_qty + delta;
    store
        .add_fill(vec![
            ("position_id", Val::Int(position_id)),
            ("side", Val::Text("BUY".to_owned())),
            ("quantity", Val::Int(delta)),
            ("price", Val::Dec(delta_price)),
            ("filled_at", Val::Stamp(now)),
            ("journal_ref", Val::Text(order_id.to_owned())),
            ("simulated", Val::Bool(simulated)),
        ])
        .await?;
    store
        .update_position(
            position_id,
            vec![
                ("quantity_entered", Val::Int(filled)),
                ("quantity_open", Val::Int(new_open)),
                ("entry_avg", Val::Dec(four(average))),
            ],
        )
        .await?;
    let stop = required(position.stop, "sw_position.stop")?;
    let client_id = format!("{}:{symbol}:GTT", line.plan_id);
    let (gtt, ok) = if position.gtt_id.is_none() {
        let gtt = arm(gw, settings, &symbol, new_open, stop, average, &client_id).await?;
        let ok = GTT_PLACED_STATUSES.contains(&status_of(&gtt).as_str());
        if ok {
            store
                .update_position(position_id, gtt_fields(&gtt, stop, now))
                .await?;
        }
        (gtt, ok)
    } else {
        let gtt = modify(
            gw,
            settings,
            position.gtt_id.as_deref(),
            &symbol,
            new_open,
            stop,
            average,
            &client_id,
        )
        .await?;
        let ok = GTT_MODIFIED_STATUSES.contains(&status_of(&gtt).as_str());
        if ok {
            store
                .update_position(position_id, vec![("gtt_armed_at", Val::Stamp(now))])
                .await?;
        }
        (gtt, ok)
    };
    if ok {
        return Ok(ExecOutcome::new(
            final_state,
            format!(
                "{symbol}: +{delta} filled at {}, GTT now covers {new_open}",
                py_dec(delta_price)
            ),
            Some(order_stub(order_id, status)),
            Some(gtt),
            Some(position_id),
            simulated,
        )
        .filled(filled));
    }
    let reason = format!(
        "{symbol}: +{delta} filled, but the GTT could not be re-sized to {new_open} — {}; it \
         still covers {open_qty}, re-arm",
        blocked_reason(&gtt, "the GTT")
    );
    Ok(ExecOutcome::new(
        final_state,
        reason,
        Some(order_stub(order_id, status)),
        Some(gtt),
        Some(position_id),
        simulated,
    )
    .filled(filled))
}

async fn close_line_after_update(
    store: &mut dyn SwingStore,
    line: &LineRow,
    outcome: &ExecOutcome,
    status: &str,
    order_id: &str,
) -> Result<(), StoreError> {
    let line_id = line.id;
    let position_id = outcome.position_id;
    if status == ORDER_COMPLETE {
        store
            .set_line(line_id, "FILLED", Some(order_id), position_id)
            .await?;
    } else if ORDER_DEAD_STATUSES.contains(&status) {
        if position_id.is_some() {
            store
                .set_line(line_id, "FILLED", Some(order_id), position_id)
                .await?;
            store
                .note_line(
                    line_id,
                    &format!(
                        "{status}: {} filled, the rest never did",
                        outcome.filled_quantity
                    ),
                )
                .await?;
        } else {
            store
                .set_line(line_id, "EXPIRED", Some(order_id), None)
                .await?;
            store
                .note_line(line_id, &format!("{status} with nothing filled"))
                .await?;
        }
    } else if position_id.is_some() {
        store
            .set_line(line_id, "SENT", Some(order_id), position_id)
            .await?;
    }
    Ok(())
}

/// `CutoffReport`: what the 10:45 sweep did.
#[derive(Debug, Clone, PartialEq)]
pub struct CutoffReport {
    /// Fills applied before cancelling.
    pub reconciled: i64,
    /// Remainders cancelled.
    pub cancelled: i64,
    /// Cancels the gateway refused.
    pub cancel_failed: i64,
    /// PENDING_RANGE slots freed.
    pub slots_freed: i64,
    /// Every reconcile outcome.
    pub outcomes: Vec<ExecOutcome>,
}

/// `cutoff_open_orders`: cancel every open remainder (the GTT covering what filled is
/// untouched), free every unclaimed slot.
///
/// # Errors
/// The store's or the gateway's.
pub async fn cutoff_open_orders(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    orders: Option<&dyn OrderSource>,
    now: DateTime<FixedOffset>,
) -> Result<CutoffReport, ExecError> {
    let day = session_day(&now);
    let mut outcomes = Vec::new();
    let (mut reconciled, mut cancelled, mut failed) = (0, 0, 0);
    for line in store.sent_buy_lines(day).await? {
        let order_id = line.journal_ref.clone().unwrap_or_default();
        if order_id.is_empty() {
            continue;
        }
        if let Some(source) = orders {
            let seen = source.order_status(&order_id).await;
            let update = OrderUpdate {
                order_id: order_id.clone(),
                status: seen.status.clone(),
                filled_quantity: seen.filled_quantity,
                average_price: seen.average_price,
            };
            if let Some(applied) = on_order_update(store, gw, settings, &update, now).await? {
                outcomes.push(applied);
                reconciled += 1;
            }
            if update.status == ORDER_COMPLETE
                || ORDER_DEAD_STATUSES.contains(&update.status.as_str())
            {
                continue;
            }
        }
        let current = store.line(line.id).await?;
        let Some(current) = current.filter(|c| c.state == "SENT") else {
            continue;
        };
        let cancel = gw
            .cancel_order(CancelOrder {
                order_id: order_id.clone(),
                symbol: current.symbol.clone(),
                variety: "regular".to_owned(),
                series: None,
                client_id: Some(format!("{}:{}:CANCEL", current.plan_id, current.symbol)),
                tenant: sole_tenant(settings)?,
                plan_tenant: sole_tenant(settings)?,
            })
            .await?;
        if !ORDER_CANCELLED_STATUSES.contains(&status_of(&cancel).as_str()) {
            failed += 1;
            store
                .note_line(
                    current.id,
                    &format!(
                        "10:45 cancel refused: {}",
                        blocked_reason(&cancel, "the cancel")
                    ),
                )
                .await?;
            continue;
        }
        cancelled += 1;
        store.lock_begin(day).await?;
        let body = async {
            let held = match current.position_id {
                Some(p) => store.position(p).await?.map_or(0, |pos| pos.quantity_open),
                None => 0,
            };
            let planned = current.quantity;
            if let Some(p) = current.position_id {
                store.set_line(current.id, "FILLED", None, Some(p)).await?;
                store
                    .note_line(
                        current.id,
                        &format!(
                            "10:45 cutoff: {held} filled, the remaining {} cancelled ({order_id}); \
                             GTT untouched",
                            (planned - held).max(0)
                        ),
                    )
                    .await?;
            } else {
                store.set_line(current.id, "EXPIRED", None, None).await?;
                store
                    .note_line(
                        current.id,
                        &format!("10:45 cutoff: nothing filled, order {order_id} cancelled"),
                    )
                    .await?;
            }
            Ok::<(), StoreError>(())
        }
        .await;
        match body {
            Ok(()) => store.lock_commit().await?,
            Err(e) => {
                store.lock_rollback().await?;
                return Err(e.into());
            }
        }
    }
    let freed = store.expire_pending(day).await?;
    Ok(CutoffReport {
        reconciled,
        cancelled,
        cancel_failed: failed,
        slots_freed: freed,
        outcomes,
    })
}

/// `eod_gtt_sweep`: every open position with shares and no resting GTT is re-armed through
/// [`rearm_gtt`], given a last price above its stop.
///
/// # Errors
/// The store's or the gateway's.
pub async fn eod_gtt_sweep(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    now: DateTime<FixedOffset>,
    prices: &std::collections::BTreeMap<String, Decimal>,
) -> Result<Vec<ExecOutcome>, ExecError> {
    let naked: Vec<PositionRow> = store
        .open_positions()
        .await?
        .into_iter()
        .filter(|p| p.gtt_id.is_none() && p.quantity_open > 0)
        .collect();
    let mut outcomes = Vec::new();
    for pos in naked {
        let price = prices.get(&pos.symbol).copied();
        outcomes.push(rearm_gtt(store, gw, settings, pos.id, "true", now, price).await?);
    }
    Ok(outcomes)
}

async fn position_for_line(
    store: &mut dyn SwingStore,
    line: &LineRow,
) -> Result<Option<PositionRow>, StoreError> {
    if let Some(p) = line.position_id
        && let Some(found) = store.position(p).await?
        && found.quantity_open > 0
    {
        return Ok(Some(found));
    }
    store.open_position_for(line.instrument_id).await
}

#[allow(clippy::too_many_arguments)]
async fn sell(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    cx: &ExecContext<'_>,
    line: &LineRow,
    plan_id: &str,
    now: DateTime<FixedOffset>,
    simulated: bool,
    last_price: Option<Decimal>,
) -> Result<ExecOutcome, ExecError> {
    let symbol = line.symbol.clone();
    let quantity = line.quantity;
    let Some(pos) = position_for_line(store, line).await? else {
        return Ok(ExecOutcome::new(
            "BLOCKED",
            format!("{symbol}: not a swing position"),
            None,
            None,
            None,
            simulated,
        ));
    };
    let blocked =
        |reason: String| ExecOutcome::new("BLOCKED", reason, None, None, Some(pos.id), simulated);
    let open_qty = pos.quantity_open;
    if quantity <= 0 {
        return Ok(blocked(format!(
            "{symbol}: a line for {quantity} shares is not a sell"
        )));
    }
    if quantity > open_qty {
        return Ok(blocked(format!(
            "{symbol}: SELL for {quantity} exceeds the {open_qty} open"
        )));
    }
    if simulated && last_price.is_none() {
        return Ok(blocked(format!(
            "{symbol}: no last price to simulate a market fill at — pass last_price"
        )));
    }
    let to_f = |d: Decimal| -> f64 { d.to_string().parse().unwrap_or(0.0) };
    let order = gw
        .place(PlaceOrder {
            symbol: symbol.clone(),
            qty: quantity,
            side: "SELL".to_owned(),
            product: "CNC".to_owned(),
            order_type: "MARKET".to_owned(),
            price: last_price.map(to_f),
            exchange: "NSE".to_owned(),
            client_id: Some(format!("{plan_id}:{symbol}:SELL")),
            tenant: sole_tenant(cx.settings)?,
            plan_tenant: sole_tenant(cx.settings)?,
            ..PlaceOrder::default()
        })
        .await?;
    let status = order_status(&status_of(&order));
    if status != "SIMULATED" && status != "SENT" {
        let reason = blocked_reason(&order, "the sell");
        return Ok(ExecOutcome::new(
            status,
            reason,
            Some(order),
            None,
            Some(pos.id),
            simulated,
        ));
    }
    if status == "SENT" {
        let order_id = py_str_value(order.get("order_id"));
        return Ok(ExecOutcome::new(
            "SENT",
            format!(
                "{symbol}: sell order {order_id} accepted, not yet filled — the book is \
                 unchanged until it fills"
            ),
            Some(order),
            None,
            Some(pos.id),
            simulated,
        ));
    }
    // The gateway answered DRY_RUN; Python's `_price(last_price)` raises on None, and a fill
    // booked at 0 would be a lie in the record.
    let price = required(last_price, "last_price")?;
    apply_sell_fill(
        store,
        gw,
        cx.settings,
        &pos,
        line,
        plan_id,
        now,
        quantity,
        price,
        order,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn apply_sell_fill(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    pos: &PositionRow,
    line: &LineRow,
    plan_id: &str,
    now: DateTime<FixedOffset>,
    quantity: i64,
    price: Decimal,
    order: PyDict,
) -> Result<ExecOutcome, ExecError> {
    let position_id = pos.id;
    let symbol = pos.symbol.clone();
    let day = session_day(&now);
    let journal_ref = match order.get("order_id") {
        Some(PyValue::Str(s)) => Val::Text(s.clone()),
        Some(PyValue::Int(i)) => Val::Int(*i),
        _ => Val::Null,
    };
    store
        .add_fill(vec![
            ("position_id", Val::Int(position_id)),
            ("side", Val::Text("SELL".to_owned())),
            ("quantity", Val::Int(quantity)),
            ("price", Val::Dec(price)),
            ("filled_at", Val::Stamp(now)),
            ("journal_ref", journal_ref),
            ("simulated", Val::Bool(true)),
        ])
        .await?;
    let entered = pos.quantity_entered;
    let open_qty = pos.quantity_open;
    let sold_before = entered - open_qty;
    let mut fills: Vec<(i64, Decimal)> = Vec::new();
    if sold_before > 0
        && let Some(prev) = pos.exit_avg
    {
        fills.push((sold_before, prev));
    }
    fills.push((quantity, price));
    let exit_avg = exit_average(&fills).unwrap_or(price);
    let remaining = open_qty - quantity;
    let note = line.note.clone().unwrap_or_default();
    let gtt_cid = format!("{plan_id}:{symbol}:SELL:GTT");
    let cancel = cancel_gtt(gw, settings, pos.gtt_id.as_deref(), &symbol, &gtt_cid).await?;
    if let Some(c) = &cancel
        && !GTT_DELETED_STATUSES.contains(&status_of(c).as_str())
    {
        store
            .update_position(
                position_id,
                book_after_sell(pos, remaining, exit_avg, &note, day),
            )
            .await?;
        let reason = format!(
            "{symbol}: sold {quantity}, but the resting GTT {} could not be cancelled — {}; it \
             still covers {open_qty}",
            pos.gtt_id.clone().unwrap_or_else(|| "None".to_owned()),
            blocked_reason(c, "the cancel")
        );
        return Ok(ExecOutcome::new(
            "SIMULATED",
            reason,
            Some(order),
            cancel.clone(),
            Some(position_id),
            true,
        ));
    }
    let mut fields = book_after_sell(pos, remaining, exit_avg, &note, day);
    fields.push(("gtt_id", Val::Null));
    fields.push(("gtt_trigger", Val::Null));
    fields.push(("gtt_armed_at", Val::Null));
    if remaining == 0 {
        store.update_position(position_id, fields).await?;
        return Ok(ExecOutcome::new(
            "SIMULATED",
            "",
            Some(order),
            cancel,
            Some(position_id),
            true,
        ));
    }
    let stop = required(pos.stop, "sw_position.stop")?;
    let gtt = arm(gw, settings, &symbol, remaining, stop, price, &gtt_cid).await?;
    if GTT_PLACED_STATUSES.contains(&status_of(&gtt).as_str()) {
        let armed = gtt_fields(&gtt, stop, now);
        merge_fields(&mut fields, armed);
        store.update_position(position_id, fields).await?;
        return Ok(ExecOutcome::new(
            "SIMULATED",
            "",
            Some(order),
            Some(gtt),
            Some(position_id),
            true,
        ));
    }
    store.update_position(position_id, fields).await?;
    let reason = format!(
        "{symbol}: sold {quantity}, but the stop for the remaining {remaining} was not armed — \
         {}; position {position_id} is NAKED, re-arm it",
        blocked_reason(&gtt, "the GTT")
    );
    Ok(ExecOutcome::new(
        "SIMULATED",
        reason,
        Some(order),
        Some(gtt),
        Some(position_id),
        true,
    ))
}

/// `dict.update(...)`: replace a key in place, append a new one.
fn merge_fields(fields: &mut Fields, update: Fields) {
    for (k, v) in update {
        if let Some(slot) = fields.iter_mut().find(|(c, _)| *c == k) {
            slot.1 = v;
        } else {
            fields.push((k, v));
        }
    }
}

fn book_after_sell(
    pos: &PositionRow,
    remaining: i64,
    exit_avg: Decimal,
    note: &str,
    day: NaiveDate,
) -> Fields {
    if remaining > 0 {
        return vec![
            ("quantity_open", Val::Int(remaining)),
            ("state", Val::Text("PARTIAL".to_owned())),
            ("partial_done", Val::Bool(true)),
            ("partial_date", Val::Date(day)),
            ("exit_avg", Val::Dec(exit_avg)),
        ];
    }
    let entry = pos.entry_avg.unwrap_or_default();
    let initial_stop = pos.initial_stop.unwrap_or_default();
    let reason = if KNOWN_CLOSE_REASONS.contains(&note) {
        note.to_owned()
    } else {
        "MANUAL".to_owned()
    };
    vec![
        ("quantity_open", Val::Int(0)),
        ("state", Val::Text("CLOSED".to_owned())),
        ("closed_on", Val::Date(day)),
        ("exit_avg", Val::Dec(exit_avg)),
        ("close_reason", Val::Text(reason)),
        (
            "r_multiple",
            r_multiple(entry, initial_stop, exit_avg).map_or(Val::Null, Val::Dec),
        ),
        (
            "pnl_inr",
            Val::Dec(closed_pnl(entry, exit_avg, pos.quantity_entered)),
        ),
    ]
}

#[allow(clippy::too_many_arguments)]
async fn raise_stop(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    cx: &ExecContext<'_>,
    line: &LineRow,
    plan_id: &str,
    now: DateTime<FixedOffset>,
    simulated: bool,
    last_price: Option<Decimal>,
) -> Result<ExecOutcome, ExecError> {
    let symbol = line.symbol.clone();
    let Some(pos) = position_for_line(store, line).await? else {
        return Ok(ExecOutcome::new(
            "BLOCKED",
            format!("{symbol}: not a swing position"),
            None,
            None,
            None,
            simulated,
        ));
    };
    let blocked = |reason: String, gtt: Option<PyDict>| {
        ExecOutcome::new("BLOCKED", reason, None, gtt, Some(pos.id), simulated)
    };
    let Some(new_stop) = line.stop else {
        return Ok(blocked(
            format!("{symbol}: a RAISE line needs a stop"),
            None,
        ));
    };
    // A resting stop of None read as 0 would let ANY raise pass "a stop never falls".
    let resting = required(pos.stop, "sw_position.stop")?;
    if new_stop <= resting {
        return Ok(blocked(
            format!(
                "{symbol}: new stop {} is not above the resting stop {} — a stop never falls",
                py_dec(new_stop),
                py_dec(resting)
            ),
            None,
        ));
    }
    let open_qty = pos.quantity_open;
    if open_qty <= 0 {
        return Ok(blocked(
            format!("{symbol}: nothing is open to protect"),
            None,
        ));
    }
    let Some(last) = last_price else {
        return Ok(blocked(
            format!("{symbol}: no last price to check the new stop against — pass last_price"),
            None,
        ));
    };
    if new_stop >= last {
        return Ok(blocked(
            format!(
                "{symbol}: new stop {} is at or above the last price {} — that would fire at once",
                py_dec(new_stop),
                py_dec(last)
            ),
            None,
        ));
    }
    let gtt_cid = format!("{plan_id}:{symbol}:RAISE:GTT");
    let cancel = cancel_gtt(gw, cx.settings, pos.gtt_id.as_deref(), &symbol, &gtt_cid).await?;
    if let Some(c) = &cancel
        && !GTT_DELETED_STATUSES.contains(&status_of(c).as_str())
    {
        return Ok(blocked(
            blocked_reason(c, "cancelling the old GTT"),
            cancel.clone(),
        ));
    }
    let gtt = arm(gw, cx.settings, &symbol, open_qty, new_stop, last, &gtt_cid).await?;
    if GTT_PLACED_STATUSES.contains(&status_of(&gtt).as_str()) {
        store
            .update_position(pos.id, gtt_fields(&gtt, new_stop, now))
            .await?;
        return Ok(ExecOutcome::new(
            if simulated { "SIMULATED" } else { "FILLED" },
            "",
            None,
            Some(gtt),
            Some(pos.id),
            simulated,
        ));
    }
    store
        .update_position(
            pos.id,
            vec![
                ("stop", Val::Dec(new_stop)),
                ("gtt_id", Val::Null),
                ("gtt_trigger", Val::Null),
                ("gtt_armed_at", Val::Null),
            ],
        )
        .await?;
    let reason = format!(
        "{symbol}: the old stop was cancelled but the new one was not armed — {}; position {} is \
         NAKED, re-arm it",
        blocked_reason(&gtt, "the GTT"),
        pos.id
    );
    Ok(blocked(reason, Some(gtt)))
}

fn to_float(d: Decimal) -> f64 {
    d.to_string().parse().unwrap_or(0.0)
}

/// `_arm`: one GTT through the gateway, with the swing book's limit fraction.
async fn arm(
    gw: &dyn Gateway,
    settings: &Settings,
    symbol: &str,
    qty: i64,
    stop: Decimal,
    last_price: Decimal,
    client_id: &str,
) -> Result<PyDict, GatewayError> {
    let mut result = gw
        .place_gtt_stop(GttStop {
            symbol: symbol.to_owned(),
            qty,
            trigger: to_float(stop),
            last_price: to_float(last_price),
            exchange: "NSE".to_owned(),
            series: None,
            client_id: Some(client_id.to_owned()),
            tenant: sole_tenant(settings)?,
            plan_tenant: sole_tenant(settings)?,
            limit_fraction: Some(settings.swing_gtt_limit_fraction),
            fo_plan: None,
        })
        .await?;
    if result.get("client_id").is_none() {
        result.insert("client_id", PyValue::Str(client_id.to_owned()));
    }
    Ok(result)
}

/// `_cancel`: pull a resting trigger; a simulated one is recorded as cancelled locally.
async fn cancel_gtt(
    gw: &dyn Gateway,
    settings: &Settings,
    gtt_id: Option<&str>,
    symbol: &str,
    client_id: &str,
) -> Result<Option<PyDict>, GatewayError> {
    let Some(g) = gtt_id else { return Ok(None) };
    let Some(broker_id) = broker_gtt_id(Some(g)) else {
        let mut m = PyDict::new();
        m.insert("symbol", PyValue::Str(symbol.to_owned()));
        m.insert("gtt_id", PyValue::Str(g.to_owned()));
        m.insert(
            "status",
            PyValue::Str(gtt_status::DRY_RUN_GTT_DELETE.to_owned()),
        );
        m.insert("simulated", PyValue::Bool(true));
        return Ok(Some(m));
    };
    gw.delete_gtt(DeleteGtt {
        gtt_id: broker_id,
        symbol: symbol.to_owned(),
        exchange: "NSE".to_owned(),
        series: None,
        client_id: Some(client_id.to_owned()),
        tenant: sole_tenant(settings)?,
        plan_tenant: sole_tenant(settings)?,
    })
    .await
    .map(Some)
}

/// `_modify`: re-size a resting trigger to `qty`; a simulated one locally.
#[allow(clippy::too_many_arguments)]
async fn modify(
    gw: &dyn Gateway,
    settings: &Settings,
    gtt_id: Option<&str>,
    symbol: &str,
    qty: i64,
    stop: Decimal,
    last_price: Decimal,
    client_id: &str,
) -> Result<PyDict, GatewayError> {
    let Some(broker_id) = broker_gtt_id(gtt_id) else {
        let mut m = PyDict::new();
        m.insert("symbol", PyValue::Str(symbol.to_owned()));
        m.insert(
            "gtt_id",
            gtt_id.map_or(PyValue::None, |g| PyValue::Str(g.to_owned())),
        );
        m.insert(
            "status",
            PyValue::Str(gtt_status::DRY_RUN_GTT_MODIFY.to_owned()),
        );
        m.insert("qty", PyValue::Int(qty));
        m.insert("trigger", PyValue::Float(to_float(stop)));
        m.insert("simulated", PyValue::Bool(true));
        return Ok(m);
    };
    let mut result = gw
        .modify_gtt_quantity(ModifyGtt {
            gtt_id: broker_id,
            symbol: symbol.to_owned(),
            qty,
            trigger: to_float(stop),
            last_price: to_float(last_price),
            exchange: "NSE".to_owned(),
            series: None,
            client_id: Some(client_id.to_owned()),
            tenant: sole_tenant(settings)?,
            plan_tenant: sole_tenant(settings)?,
            limit_fraction: Some(settings.swing_gtt_limit_fraction),
            fo_plan: None,
            floor_trigger: None,
        })
        .await?;
    if result.get("client_id").is_none() {
        result.insert("client_id", PyValue::Str(client_id.to_owned()));
    }
    Ok(result)
}

/// `_gtt_fields`: the position's three GTT columns from a placed (or simulated) trigger.
fn gtt_fields(gtt: &PyDict, stop: Decimal, now: DateTime<FixedOffset>) -> Fields {
    let placed = status_of(gtt) == gtt_status::GTT_PLACED;
    let gtt_id = match gtt.get("gtt_id") {
        Some(v) if placed && !matches!(v, PyValue::None) => py_str_value(Some(v)),
        _ => {
            let handle = match gtt.get("client_id") {
                Some(PyValue::Str(s)) if !s.is_empty() => s.clone(),
                _ => py_str_value(gtt.get("symbol")),
            };
            format!("{SIMULATED_GTT_PREFIX}{handle}")
        }
    };
    let trigger = match gtt.get("trigger") {
        Some(PyValue::Float(f)) => Some(q2(dec_of_float(*f))),
        Some(PyValue::Int(i)) => Some(q2(Decimal::from(*i))),
        Some(PyValue::Str(s)) => super::super::rules::dec(s).map(q2),
        _ => None,
    };
    vec![
        ("stop", Val::Dec(stop)),
        ("gtt_id", Val::Text(gtt_id)),
        ("gtt_trigger", Val::Dec(trigger.unwrap_or(stop))),
        ("gtt_armed_at", Val::Stamp(now)),
    ]
}

async fn record_line(
    store: &mut dyn SwingStore,
    line_id: i64,
    outcome: &ExecOutcome,
) -> Result<(), StoreError> {
    let reference = if let Some(order) = &outcome.order {
        order.get("order_id").cloned()
    } else {
        outcome.gtt.as_ref().and_then(|g| g.get("gtt_id").cloned())
    };
    let reference = match reference {
        None | Some(PyValue::None) => None,
        v => Some(py_str_value(v.as_ref())),
    };
    let state = match outcome.status.as_str() {
        "SIMULATED" | "FILLED" => "FILLED",
        "SENT" => "SENT",
        _ => "REJECTED",
    };
    store
        .set_line(line_id, state, reference.as_deref(), outcome.position_id)
        .await
}
