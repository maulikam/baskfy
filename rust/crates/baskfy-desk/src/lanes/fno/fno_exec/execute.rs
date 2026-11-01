//! Ports `app.fno_execute`'s order half (FO7/FO8): the confirm `POST /fno/execute` calls, the
//! click on an F3 exit (M.5), and the exits and rolls the monitor raises — every leg on paper.
//!
//! **Law 2.** Every leg is a [`Gateway::place`] with `product="NRML"`, `exchange="NFO"` and a
//! [`FoPlanRef`]; every F2 stop is [`Gateway::place_gtt_stop`] / [`Gateway::modify_gtt_quantity`]
//! / [`Gateway::delete_gtt`] with the same reference. The gateway is derived per sleeve
//! ([`fo_gateway`]) with `coverage=uncovered` (FO6.1) and the sleeve's switches **with
//! `dry_run` pinned true** ([`crate::lanes::fno::gates::paper_gates`], FO7.1), read at the moment of each
//! order — `OPTIONS_ENABLED` (default off) and the carry switch are the gateway's to enforce
//! (`fno_gates` passes them through when the sleeve's execution switch is on), and every leg
//! comes back `DRY_RUN` and is filled by walking the leg's depth
//! (`simulate_fill`) into `fo_fill` with `simulated=true`.
//!
//! **LIVE is refused** (`LIVE_NOT_BUILT`) before any order. **Nothing here is scheduled and
//! nothing here confirms**: [`execute_entry`] runs only with `confirm=true` on an `ISSUED`
//! ENTRY/ADD plan inside its 30 minutes; [`execute_click_exit`] only with `confirm=true` on an
//! `ISSUED` EXIT; [`run_pending`] sends EXIT and ROLL plans the monitor raised — never an ENTRY
//! or an ADD (the store's `pending_actions` selects `kind IN ('EXIT','ROLL')` only).

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};

use async_trait::async_trait;
use chrono::{DateTime, FixedOffset};
use rust_decimal::Decimal;

use baskfy_core::fno::config::{FnoConfig, FoSleeve};
use baskfy_core::fno::covered::{OptionPosition, net_of_plan};
use baskfy_core::options::gating::Mode;

use super::store::{FoLegRow, FoPlanRow, FoQuote, FoStore, PositionRow, Val, money};
use crate::app::AppState;
use crate::gateway::{
    DeleteGtt, DerivedGateway, FoPlanRef, Gateway, GttStop, ModifyGtt, PlaceOrder, TenantIds,
    gtt_status,
};
use crate::lanes::exec_core::guards::SignedLeg;
use crate::lanes::options::core::{
    Attempt, Book, ExecutionConfig, Fill, LegRole, Outcome, Side, Sleeve, Venue, VenueError,
    next_attempt, role_of, role_str, run_entry, run_exit, side_str,
};
pub use crate::lanes::options::execute::{ExecError, Refused};
use crate::lanes::options::py;
use crate::wire::{PyDict, PyValue};

/// `DRY_RUN`.
pub const DRY_RUN: &str = "DRY_RUN";
/// `FUTURE_ROLE`.
pub const FUTURE_ROLE: &str = "LONG_FUTURE";
/// `FUTURE_TYPE`.
pub const FUTURE_TYPE: &str = "XX";
const DUPLICATE: &str = "DUPLICATE";
/// `_CONDOR_RULES`: a condor runs as O1 (only the short buy-backs reduce risk).
const CONDOR_RULES: Sleeve = Sleeve::O1m;

/// `TICK`.
#[must_use]
pub fn tick() -> Decimal {
    Decimal::new(5, 2)
}

/// `GTT_PLACED_STATUSES`.
pub const GTT_PLACED_STATUSES: [&str; 2] = [gtt_status::GTT_PLACED, gtt_status::DRY_RUN_GTT];
/// `GTT_MODIFIED_STATUSES`.
pub const GTT_MODIFIED_STATUSES: [&str; 2] =
    [gtt_status::GTT_MODIFIED, gtt_status::DRY_RUN_GTT_MODIFY];

/// `PlanKind` values.
pub mod kind {
    /// An entry.
    pub const ENTRY: &str = "ENTRY";
    /// F3's add.
    pub const ADD: &str = "ADD";
    /// An exit.
    pub const EXIT: &str = "EXIT";
    /// F2's roll.
    pub const ROLL: &str = "ROLL";
}

/// `PlanState` values used here.
pub mod state {
    /// Raised, waiting for the click.
    pub const ISSUED: &str = "ISSUED";
    /// Confirmed.
    pub const CONFIRMED: &str = "CONFIRMED";
    /// Orders going out.
    pub const FILLING: &str = "FILLING";
    /// The position is open.
    pub const OPEN: &str = "OPEN";
    /// Closing.
    pub const EXITING: &str = "EXITING";
    /// Done.
    pub const CLOSED: &str = "CLOSED";
    /// A leg fell short; abandoned before any short was left naked.
    pub const ABANDONED_PARTIAL: &str = "ABANDONED_PARTIAL";
}

/// `Structure` values.
pub mod structure {
    /// F1.
    pub const IRON_CONDOR: &str = "IRON_CONDOR";
    /// F2.
    pub const FUTURE: &str = "FUTURE";
    /// F3.
    pub const CREDIT_SPREAD: &str = "CREDIT_SPREAD";
}

/// `FoOutcome`.
#[derive(Debug, Clone, PartialEq)]
pub struct FoOutcome {
    /// `plan_id`.
    pub plan_id: String,
    /// `sleeve`.
    pub sleeve: String,
    /// `kind`.
    pub kind: String,
    /// `outcome`.
    pub outcome: String,
    /// `simulated`.
    pub simulated: bool,
    /// `orders`.
    pub orders: i64,
    /// `position_id`.
    pub position_id: Option<i64>,
    /// `detail` (Decimals already `str`, as `_jsonable` makes them).
    pub detail: PyDict,
}

impl FoOutcome {
    fn new(plan: &FoPlanRow, outcome: &str, orders: i64) -> Self {
        Self {
            plan_id: plan.plan_id.clone(),
            sleeve: plan.sleeve.value().to_owned(),
            kind: plan.kind.clone(),
            outcome: outcome.to_owned(),
            simulated: true,
            orders,
            position_id: None,
            detail: PyDict::new(),
        }
    }

    fn with(mut self, position_id: Option<i64>, detail: PyDict) -> Self {
        self.position_id = position_id;
        self.detail = detail;
        self
    }
}

/// `fno_desk.outcome_json(outcome)`: the route's body, in Python's key order.
#[must_use]
pub fn outcome_json(o: &FoOutcome) -> PyDict {
    crate::wire::dict([
        ("plan_id", PyValue::Str(o.plan_id.clone())),
        ("sleeve", PyValue::Str(o.sleeve.clone())),
        ("kind", PyValue::Str(o.kind.clone())),
        ("outcome", PyValue::Str(o.outcome.clone())),
        ("simulated", PyValue::Bool(o.simulated)),
        ("orders", PyValue::Int(o.orders)),
        (
            "position_id",
            o.position_id.map_or(PyValue::None, PyValue::Int),
        ),
        ("detail", py::jsonable(&PyValue::Map(o.detail.clone()))),
    ])
}

/// `QuoteSource`: tradingsymbols in, their books out; a symbol Kite does not answer is absent.
#[async_trait]
pub trait FoQuoteSource: Send + Sync {
    /// `quotes(symbols)`.
    ///
    /// # Errors
    /// Whatever the reader raises (a 500 in Python).
    async fn quotes(&self, symbols: &[String]) -> Result<BTreeMap<String, FoQuote>, ExecError>;
}

/// A fixed book (tests and replays).
#[derive(Debug, Clone, Default)]
pub struct StaticQuotes {
    /// The books by tradingsymbol.
    pub books: BTreeMap<String, FoQuote>,
}

#[async_trait]
impl FoQuoteSource for StaticQuotes {
    async fn quotes(&self, symbols: &[String]) -> Result<BTreeMap<String, FoQuote>, ExecError> {
        Ok(symbols
            .iter()
            .filter_map(|s| self.books.get(s).map(|q| (s.clone(), q.clone())))
            .collect())
    }
}

/// `BrokerBook`: the broker's NRML option book net of the plan's own fills (FO1.2/FO6.3).
pub type BrokerBook = dyn Fn(&[OptionPosition]) -> Vec<OptionPosition> + Send + Sync;

/// `paper_book`: in PAPER the plan covers itself (FO7.4) — the broker holds none of its legs.
#[must_use]
pub fn paper_book(plan_filled: &[OptionPosition]) -> Vec<OptionPosition> {
    let _ = plan_filled;
    Vec::new()
}

/// `live_book(raw)`: the raw NRML book net of the plan's own fills.
pub fn live_book(raw: Arc<dyn Fn() -> Vec<OptionPosition> + Send + Sync>) -> Arc<BrokerBook> {
    Arc::new(move |filled: &[OptionPosition]| net_of_plan(&raw(), filled))
}

/// `alert(message)`. An alias so `#[async_trait]` keeps it higher-ranked (`for<'m>`): written
/// inline in a trait method, the macro would name the `&str`'s lifetime and the callee could
/// not hand it a borrowed message.
pub type Alert = dyn for<'m> Fn(&'m str) + Send + Sync;

/// `fno_ledger`'s two calls at a close (FO10): the journal row and `04` §7's pauses. The
/// ledger is the fno lane's sibling module; it implements this.
#[async_trait]
pub trait FoLedger: Send + Sync {
    /// `fno_ledger.after_close(store, position_id, now=, alert=)`.
    ///
    /// # Errors
    /// The database.
    async fn after_close(
        &self,
        store: &mut FoStore<'_>,
        position_id: i64,
        at: DateTime<FixedOffset>,
        alert: &Alert,
    ) -> Result<(), ExecError>;

    /// `fno_ledger.journal_abandoned(store, plan, at)`: the position id journalled, if any.
    ///
    /// # Errors
    /// The database.
    async fn journal_abandoned(
        &self,
        store: &mut FoStore<'_>,
        plan: &FoPlanRow,
        at: DateTime<FixedOffset>,
    ) -> Result<Option<i64>, ExecError>;
}

/// A ledger that writes nothing (tests of the order path alone).
#[derive(Debug, Clone, Copy, Default)]
pub struct NoLedger;

#[async_trait]
impl FoLedger for NoLedger {
    async fn after_close(
        &self,
        store: &mut FoStore<'_>,
        position_id: i64,
        at: DateTime<FixedOffset>,
        alert: &Alert,
    ) -> Result<(), ExecError> {
        let _ = (store, position_id, at, alert);
        Ok(())
    }

    async fn journal_abandoned(
        &self,
        store: &mut FoStore<'_>,
        plan: &FoPlanRow,
        at: DateTime<FixedOffset>,
    ) -> Result<Option<i64>, ExecError> {
        let _ = (store, plan, at);
        Ok(None)
    }
}

/// Everything a confirm or a pass needs beyond the store and the gateway.
pub struct FoCtx<'a> {
    /// The live books.
    pub quotes: &'a dyn FoQuoteSource,
    /// `now()`.
    pub now: &'a (dyn Fn() -> DateTime<FixedOffset> + Send + Sync),
    /// The broker's book.
    pub broker_book: &'a BrokerBook,
    /// `alert`.
    pub alert: &'a Alert,
    /// `fno_ledger`.
    pub ledger: &'a dyn FoLedger,
    /// The console's one tenant (`SOLE_USER_ID`, `SOLE_BROKER_ACCOUNT_ID`), for the GTT calls.
    pub tenant: Option<TenantIds>,
}

/// `log.error` on `app.fno_execute`.
#[allow(clippy::print_stderr)]
pub fn log_error(message: &str) {
    eprintln!("ERROR:app.fno_execute:{message}");
}

fn other(e: impl std::fmt::Display) -> ExecError {
    ExecError::Other(e.to_string())
}

fn refused(status: u16, code: &str, message: impl Into<String>) -> ExecError {
    ExecError::Refused(Refused::new(status, code, message))
}

fn dstr(d: Decimal) -> PyValue {
    PyValue::Str(py::dstr(d))
}

fn f64_of(d: Decimal) -> f64 {
    crate::lanes::options::execute::to_f64(d)
}

/// `_client_id(plan_id, symbol, closing=, number=)`.
#[must_use]
pub fn client_id(plan_id: &str, symbol: &str, closing: bool, number: i64) -> String {
    crate::lanes::options::execute::client_id(plan_id, symbol, closing, number)
}

/// `paper_gtt_handle(position_id)` (FO7.5).
#[must_use]
pub fn paper_gtt_handle(position_id: i64) -> String {
    format!("PAPER-{position_id}")
}

/// `gtt_handle_int(gtt_id)`: the exchange's id, or a paper handle's position id.
#[must_use]
pub fn gtt_handle_int(gtt_id: Option<&str>) -> Option<i64> {
    let raw = gtt_id.filter(|s| !s.is_empty())?;
    let raw = raw.strip_prefix("PAPER-").unwrap_or(raw);
    if raw.is_empty() || !raw.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    raw.parse::<i64>().ok().filter(|n| *n > 0)
}

fn option_type_of(text: &str) -> Result<baskfy_core::fno::vol::OptionType, ExecError> {
    use baskfy_core::fno::vol::OptionType;
    match text {
        "CE" => Ok(OptionType::Ce),
        "PE" => Ok(OptionType::Pe),
        other => Err(ExecError::Other(format!(
            "ValueError: '{other}' is not a valid OptionType"
        ))),
    }
}

/// `_option_leg(underlying, leg, units)`.
///
/// # Errors
/// A leg without a strike (`ValueError`).
pub fn option_leg(
    underlying: &str,
    leg: &FoLegRow,
    units: i64,
) -> Result<OptionPosition, ExecError> {
    let Some(strike) = leg.strike else {
        return Err(ExecError::Other(format!(
            "ValueError: {}: an option leg without a strike",
            leg.tradingsymbol
        )));
    };
    Ok(OptionPosition {
        underlying: underlying.to_owned(),
        expiry: leg.expiry,
        option_type: option_type_of(&leg.option_type)?,
        strike,
        quantity: units,
    })
}

fn signed_value(p: &OptionPosition) -> PyValue {
    use baskfy_core::fno::vol::OptionType;
    SignedLeg {
        underlying: p.underlying.clone(),
        expiry: p.expiry,
        option_type: match p.option_type {
            OptionType::Ce => "CE".to_owned(),
            OptionType::Pe => "PE".to_owned(),
        },
        strike: p.strike,
        quantity: p.quantity,
    }
    .to_value()
}

fn fo_ref(plan_id: &str, sleeve: FoSleeve) -> FoPlanRef {
    FoPlanRef {
        plan_id: plan_id.to_owned(),
        sleeve: sleeve.value().to_owned(),
        step: None,
        broker_positions: Vec::new(),
        plan_filled: Vec::new(),
    }
}

fn status_of(result: &PyDict) -> String {
    result
        .get("status")
        .map_or_else(|| "None".to_owned(), PyValue::py_str)
}

fn error_of(result: &PyDict) -> String {
    match result.get("error") {
        Some(v) if v.truthy() => v.py_str(),
        _ => String::new(),
    }
}

fn error_repr(result: &PyDict) -> String {
    result
        .get("error")
        .map_or_else(|| "None".to_owned(), PyValue::py_str)
}

// --- the gateway --------------------------------------------------------------------------------

/// `fo_journal_path()`: `fno_orders_journal.jsonl` beside the desk's own journal.
#[must_use]
pub fn fo_journal_path(state: &AppState) -> PathBuf {
    state
        .paths
        .resolve(crate::gateway::JOURNAL)
        .with_file_name("fno_orders_journal.jsonl")
}

type GatewayCache = Mutex<BTreeMap<(PathBuf, &'static str), Arc<dyn Gateway>>>;

fn cache() -> &'static GatewayCache {
    static CACHE: OnceLock<GatewayCache> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(BTreeMap::new()))
}

/// `fo_gateway(sleeve, kc, risk)`: one per sleeve, sharing the desk's risk manager, with the
/// sleeve's paper-pinned switches read per order and the covered-overnight rule wired. `None`
/// when the desk's gateway cannot derive one — the caller then sends nothing (fail closed).
#[must_use]
pub fn fo_gateway(state: &AppState, sleeve: FoSleeve) -> Option<Arc<dyn Gateway>> {
    let journal = fo_journal_path(state);
    let key = (journal.clone(), sleeve.value());
    if let Ok(found) = cache().lock()
        && let Some(g) = found.get(&key)
    {
        return Some(g.clone());
    }
    let settings = state.settings.clone();
    let gates: crate::gateway::GatesFn = Arc::new(move || {
        let s = match settings.read() {
            Ok(s) => s.clone(),
            Err(p) => p.into_inner().clone(),
        };
        crate::lanes::fno::gates::paper_gates(&s, sleeve)
    });
    let built = state.gateway.derive(DerivedGateway {
        gates,
        journal_path: journal,
        fo_coverage: true,
    })?;
    if let Ok(mut c) = cache().lock() {
        c.insert(key, built.clone());
    }
    Some(built)
}

/// `fno_desk.gateway_for(sleeve)`: Python calls `app.main.gateway()` first, which builds the
/// process's shared risk manager as a side effect (on Postgres: the day's `risk_ledger` row),
/// then derives the sleeve's gateway ([`fo_gateway`]). The same happens here through the
/// exec_core lane's shared risk manager. The desk's routes call this one.
pub async fn fo_gateway_ready(state: &AppState, sleeve: FoSleeve) -> Option<Arc<dyn Gateway>> {
    if let Some(desk) = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
    {
        let _ = desk.risk().await;
    }
    fo_gateway(state, sleeve)
}

/// `_tenancy()`: the console's one account, as `place` is stamped (P4.3).
///
/// # Errors
/// Non-positive ids (`ValueError("tenant ids must be positive")`).
pub fn tenancy(s: &crate::settings::Settings) -> Result<TenantIds, ExecError> {
    TenantIds::new(s.sole_user_id, s.sole_broker_account_id).map_err(other)
}

fn tenant(ctx: &FoCtx<'_>) -> Result<TenantIds, ExecError> {
    ctx.tenant
        .ok_or_else(|| ExecError::Other("ValueError: tenant ids must be positive".to_owned()))
}

// --- the option venue ---------------------------------------------------------------------------

/// `OptionVenue`: the executor's venue over the FO gateway. `held` is the plan's legs as they
/// stand (signed units per role), which every order's `FoPlanRef.plan_filled` carries.
pub struct OptionVenue<'s, 'c, 'x> {
    gateway: Arc<dyn Gateway>,
    store: &'s mut FoStore<'c>,
    plan: FoPlanRow,
    underlying: String,
    ctx: &'s FoCtx<'x>,
    position_id: Option<i64>,
    held: Vec<(String, i64)>,
    closing_is_exit: bool,
    execution: ExecutionConfig,
    /// Orders sent.
    pub orders: i64,
    /// `f"{cid}: {status} {error}"` per order the gateway did not simulate.
    pub refusals: Vec<String>,
}

impl OptionVenue<'_, '_, '_> {
    fn leg(&self, role: &str) -> Result<FoLegRow, VenueError> {
        self.plan
            .leg(role)
            .cloned()
            .ok_or_else(|| VenueError::Other(format!("KeyError: '{role}'")))
    }

    async fn book(&self, symbol: &str) -> Result<FoQuote, VenueError> {
        let found = self
            .ctx
            .quotes
            .quotes(&[symbol.to_owned()])
            .await
            .map_err(venue_err)?;
        match found.get(symbol) {
            Some(q) if q.two_sided() => Ok(q.clone()),
            _ => Err(VenueError::Refused {
                status: 503,
                code: "NO_QUOTE".to_owned(),
                message: format!("no two-sided quote for {symbol}"),
            }),
        }
    }

    fn filled(&self) -> Result<Vec<OptionPosition>, VenueError> {
        let mut out = Vec::new();
        for (role, q) in &self.held {
            if *q != 0 {
                let leg = self.leg(role)?;
                out.push(option_leg(&self.underlying, &leg, *q).map_err(venue_err)?);
            }
        }
        Ok(out)
    }

    fn hold(&mut self, role: &str, delta: i64) {
        if let Some(slot) = self.held.iter_mut().find(|(r, _)| r == role) {
            slot.1 += delta;
        } else {
            self.held.push((role.to_owned(), delta));
        }
    }
}

fn venue_err(e: ExecError) -> VenueError {
    match e {
        ExecError::Refused(r) => VenueError::Refused {
            status: r.status,
            code: r.code,
            message: r.message,
        },
        ExecError::Other(m) => VenueError::Other(m),
    }
}

fn store_err(e: crate::lanes::options::store::StoreError) -> VenueError {
    VenueError::Other(e.py_text())
}

#[async_trait]
impl Venue for OptionVenue<'_, '_, '_> {
    async fn quote(&mut self, role: LegRole) -> Result<Book, VenueError> {
        let leg = self.leg(role_str(role))?;
        let q = self.book(&leg.tradingsymbol).await?;
        Ok(Book {
            bid: q.bid.unwrap_or(Decimal::ZERO),
            ask: q.ask.unwrap_or(Decimal::ZERO),
        })
    }

    async fn send(
        &mut self,
        role: LegRole,
        attempt: &Attempt,
        quantity: i64,
        closing: bool,
    ) -> Result<Fill, VenueError> {
        let leg = self.leg(role_str(role))?;
        // An exit plan's legs ARE closes: their client ids read as the exit plan's own orders.
        let tagged_close = closing && !self.closing_is_exit;
        let key = format!(
            "{}:{}",
            leg.tradingsymbol,
            if tagged_close { "CLOSE" } else { "OPEN" }
        );
        let number = self
            .store
            .next_attempt(self.plan.pk, &key)
            .await
            .map_err(store_err)?;
        let cid = client_id(&self.plan.plan_id, &leg.tradingsymbol, tagged_close, number);
        let signed = if attempt.side == Side::Buy {
            quantity
        } else {
            -quantity
        };
        let step = option_leg(&self.underlying, &leg, signed).map_err(venue_err)?;
        let filled = self.filled()?;
        let broker = (self.ctx.broker_book)(&filled);
        let fo_plan = FoPlanRef {
            plan_id: self.plan.plan_id.clone(),
            sleeve: self.plan.sleeve.value().to_owned(),
            step: Some(signed_value(&step)),
            broker_positions: broker.iter().map(signed_value).collect(),
            plan_filled: filled.iter().map(signed_value).collect(),
        };
        let book = self.book(&leg.tradingsymbol).await?;
        let touch = if attempt.side == Side::Buy {
            book.ask
        } else {
            book.bid
        };
        let order = PlaceOrder {
            symbol: leg.tradingsymbol.clone(),
            qty: quantity,
            side: side_str(attempt.side).to_owned(),
            product: "NRML".to_owned(),
            order_type: "LIMIT".to_owned(),
            price: Some(f64_of(attempt.price)),
            exchange: "NFO".to_owned(),
            client_id: Some(cid.clone()),
            gross_exposure: Some(f64_of(attempt.price) * quantity as f64),
            reference_price: Some(touch.map_or(0.0, f64_of)),
            fo_plan: Some(fo_plan),
            ..PlaceOrder::default()
        };
        let result = self
            .gateway
            .place(order)
            .await
            .map_err(|e| VenueError::Other(e.to_string()))?;
        self.orders += 1;
        let status = status_of(&result);
        if status != DRY_RUN {
            self.refusals.push(
                format!("{cid}: {status} {}", error_of(&result))
                    .trim()
                    .to_owned(),
            );
            return Ok(Fill {
                quantity: 0,
                avg_price: None,
            });
        }
        let ladder = if attempt.side == Side::Buy {
            &book.asks
        } else {
            &book.bids
        };
        let sim = baskfy_core::options::execution::simulate_fill(
            attempt.side,
            ladder,
            quantity,
            attempt.price,
            tick(),
            &self.execution,
        )
        .map_err(|e| VenueError::Other(format!("ValueError: {e}")))?;
        if sim.filled != 0
            && let Some(avg) = sim.avg_price
        {
            let detail = crate::wire::dict([
                ("limit", dstr(attempt.price)),
                ("requested", PyValue::Int(quantity)),
                ("levels_consumed", PyValue::Int(sim.levels_consumed)),
                ("marketable", PyValue::Bool(attempt.marketable)),
            ]);
            let at = (self.ctx.now)();
            self.store
                .write_fill(
                    &leg,
                    self.position_id,
                    &cid,
                    at,
                    side_str(attempt.side),
                    sim.filled,
                    avg,
                    &detail,
                )
                .await
                .map_err(store_err)?;
            let delta = if attempt.side == Side::Buy {
                sim.filled
            } else {
                -sim.filled
            };
            self.hold(&leg.role, delta);
        }
        Ok(Fill {
            quantity: sim.filled,
            avg_price: sim.avg_price,
        })
    }
}

// --- the F2 leg ---------------------------------------------------------------------------------

/// `_send_future(...)`: attempts until `quantity` fills or they run out — two limits, and a
/// third marketable one for a sale (it reduces risk). `(filled, average, refusals)`.
#[allow(clippy::too_many_arguments)]
async fn send_future(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    plan: &FoPlanRow,
    leg: &FoLegRow,
    side: Side,
    quantity: i64,
    closing: bool,
    ctx: &FoCtx<'_>,
    position_id: Option<i64>,
    execution: &ExecutionConfig,
) -> Result<(i64, Option<Decimal>, Vec<String>), ExecError> {
    let mut remaining = quantity;
    let mut value = Decimal::ZERO;
    let mut refusals = Vec::new();
    for n in 1..=3 {
        let books = ctx
            .quotes
            .quotes(std::slice::from_ref(&leg.tradingsymbol))
            .await?;
        let book = match books.get(&leg.tradingsymbol) {
            Some(b) if b.two_sided() => b.clone(),
            _ => {
                refusals.push(format!("{}: no two-sided quote", leg.tradingsymbol));
                break;
            }
        };
        let (Some(bid), Some(ask)) = (book.bid, book.ask) else {
            break;
        };
        let reduces = closing && side == Side::Sell;
        let Some(attempt) =
            next_attempt(n, side, bid, ask, tick(), reduces, execution).map_err(other)?
        else {
            break;
        };
        let key = format!(
            "{}:{}",
            leg.tradingsymbol,
            if closing { "CLOSE" } else { "OPEN" }
        );
        let number = store.next_attempt(plan.pk, &key).await?;
        let cid = client_id(&plan.plan_id, &leg.tradingsymbol, closing, number);
        let order = PlaceOrder {
            symbol: leg.tradingsymbol.clone(),
            qty: remaining,
            side: side_str(side).to_owned(),
            product: "NRML".to_owned(),
            order_type: "LIMIT".to_owned(),
            price: Some(f64_of(attempt.price)),
            exchange: "NFO".to_owned(),
            client_id: Some(cid.clone()),
            gross_exposure: Some(f64_of(attempt.price) * remaining as f64),
            reference_price: Some(f64_of(if side == Side::Buy { ask } else { bid })),
            fo_plan: Some(fo_ref(&plan.plan_id, plan.sleeve)),
            ..PlaceOrder::default()
        };
        let result = gateway.place(order).await.map_err(other)?;
        let status = status_of(&result);
        if status != DRY_RUN {
            refusals.push(
                format!("{cid}: {status} {}", error_of(&result))
                    .trim()
                    .to_owned(),
            );
            break;
        }
        let ladder = if side == Side::Buy {
            &book.asks
        } else {
            &book.bids
        };
        let sim = baskfy_core::options::execution::simulate_fill(
            side,
            ladder,
            remaining,
            attempt.price,
            tick(),
            execution,
        )
        .map_err(|e| ExecError::Other(format!("ValueError: {e}")))?;
        if sim.filled != 0
            && let Some(avg) = sim.avg_price
        {
            let detail = crate::wire::dict([
                ("limit", dstr(attempt.price)),
                ("requested", PyValue::Int(remaining)),
                ("levels_consumed", PyValue::Int(sim.levels_consumed)),
                ("marketable", PyValue::Bool(attempt.marketable)),
            ]);
            store
                .write_fill(
                    leg,
                    position_id,
                    &cid,
                    (ctx.now)(),
                    side_str(side),
                    sim.filled,
                    avg,
                    &detail,
                )
                .await?;
            value += avg * Decimal::from(sim.filled);
            remaining -= sim.filled;
        }
        if remaining == 0 {
            break;
        }
    }
    let filled = quantity - remaining;
    let avg = if filled == 0 {
        None
    } else {
        Some(money(py::div(value, Decimal::from(filled)).map_err(other)?))
    };
    Ok((filled, avg, refusals))
}

// --- F2's stop ----------------------------------------------------------------------------------

/// `place_future_stop(...)` (`04` §10): the GTT in the fill's session, through the gateway's
/// F2 branch. A GTT that fails to place is an alert and a `NAKED_FUTURE` violation.
///
/// # Errors
/// The database, or the gateway raising.
#[allow(clippy::too_many_arguments)]
pub async fn place_future_stop(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    position_id: i64,
    plan: &FoPlanRow,
    symbol: &str,
    qty: i64,
    trigger: Decimal,
    last_price: Decimal,
    ctx: &FoCtx<'_>,
) -> Result<Option<String>, ExecError> {
    let ids = tenant(ctx)?;
    let result = gateway
        .place_gtt_stop(GttStop {
            symbol: symbol.to_owned(),
            qty,
            trigger: f64_of(trigger),
            last_price: f64_of(last_price),
            exchange: "NFO".to_owned(),
            series: None,
            client_id: Some(format!("{}:{symbol}:GTT", plan.plan_id)),
            tenant: Some(ids),
            plan_tenant: Some(ids),
            limit_fraction: None,
            fo_plan: Some(fo_ref(&plan.plan_id, plan.sleeve)),
        })
        .await
        .map_err(other)?;
    let status = status_of(&result);
    if GTT_PLACED_STATUSES.contains(&status.as_str()) || status == DUPLICATE {
        let raw = match result.get("gtt_id") {
            Some(v) if v.truthy() => v.py_str(),
            _ => String::new(),
        };
        let handle = if !raw.is_empty() && raw.chars().all(|c| c.is_ascii_digit()) {
            raw
        } else {
            paper_gtt_handle(position_id)
        };
        store
            .update_position(
                position_id,
                &[
                    ("gtt_id", Val::Str(handle.clone())),
                    ("stop_price", Val::Dec(money(trigger))),
                ],
            )
            .await?;
        return Ok(Some(handle));
    }
    let message = format!(
        "F2 {symbol}: the GTT stop did not place ({status}: {})",
        error_repr(&result)
    );
    (ctx.alert)(&message);
    let entry_plan = plan
        .parent_plan_id
        .clone()
        .unwrap_or_else(|| plan.plan_id.clone());
    store
        .add_violation(&entry_plan, "NAKED_FUTURE", &message, (ctx.now)())
        .await?;
    store
        .update_position(position_id, &[("gtt_id", Val::None)])
        .await?;
    Ok(None)
}

/// `move_future_stop(...)`: the evening modify — up to `trigger`, never below `floor`.
///
/// # Errors
/// The database, or the gateway raising.
#[allow(clippy::too_many_arguments)]
pub async fn move_future_stop(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    position: &PositionRow,
    symbol: &str,
    qty: i64,
    trigger: Decimal,
    floor: Decimal,
    last_price: Decimal,
    ctx: &FoCtx<'_>,
) -> Result<String, ExecError> {
    let Some(handle) = gtt_handle_int(position.gtt_id.as_deref()) else {
        return Ok("NO_GTT".to_owned());
    };
    let ids = tenant(ctx)?;
    let result = gateway
        .modify_gtt_quantity(ModifyGtt {
            gtt_id: handle,
            symbol: symbol.to_owned(),
            qty,
            trigger: f64_of(trigger),
            last_price: f64_of(last_price),
            exchange: "NFO".to_owned(),
            series: None,
            client_id: Some(format!(
                "{}:{symbol}:TRAIL:{}",
                position.entry_plan_id,
                py::dstr(trigger)
            )),
            tenant: Some(ids),
            plan_tenant: Some(ids),
            limit_fraction: None,
            fo_plan: Some(fo_ref(&position.entry_plan_id, position.sleeve)),
            floor_trigger: Some(f64_of(floor)),
        })
        .await
        .map_err(other)?;
    let status = status_of(&result);
    if GTT_MODIFIED_STATUSES.contains(&status.as_str()) {
        store
            .update_position(position.id, &[("stop_price", Val::Dec(money(trigger)))])
            .await?;
    } else {
        (ctx.alert)(&format!(
            "F2 {symbol}: the trail modify was refused ({status}: {})",
            error_repr(&result)
        ));
    }
    Ok(status)
}

/// `delete_future_stop(...)`: a real resting trigger is cancelled; a paper one rests nowhere.
///
/// # Errors
/// The gateway raising.
pub async fn delete_future_stop(
    gateway: &Arc<dyn Gateway>,
    position: &PositionRow,
    symbol: &str,
    ctx: &FoCtx<'_>,
) -> Result<(), ExecError> {
    let Some(gtt) = position.gtt_id.as_deref() else {
        return Ok(());
    };
    if gtt.starts_with("PAPER-") {
        return Ok(());
    }
    if let Some(handle) = gtt_handle_int(Some(gtt)) {
        let ids = tenant(ctx)?;
        gateway
            .delete_gtt(DeleteGtt {
                gtt_id: handle,
                symbol: symbol.to_owned(),
                exchange: "NFO".to_owned(),
                series: None,
                client_id: Some(format!("{}:{symbol}:GTT-DEL", position.entry_plan_id)),
                tenant: Some(ids),
                plan_tenant: Some(ids),
            })
            .await
            .map_err(other)?;
    }
    Ok(())
}

// --- the confirm --------------------------------------------------------------------------------

/// `_validate(plan, confirm, now)`: the refusals before anything is read twice or sent.
///
/// # Errors
/// `CONFIRM_REQUIRED` 400, `UNKNOWN_PLAN` 404, `NOT_AN_ENTRY` 400, `NOT_ISSUED` 409,
/// `PLAN_EXPIRED` 410.
pub fn validate(
    plan: Option<FoPlanRow>,
    confirm: bool,
    now: DateTime<FixedOffset>,
) -> Result<FoPlanRow, Refused> {
    if !confirm {
        return Err(Refused::new(
            400,
            "CONFIRM_REQUIRED",
            "confirm must be true",
        ));
    }
    let Some(plan) = plan else {
        return Err(Refused::new(404, "UNKNOWN_PLAN", "no such plan"));
    };
    if plan.kind != kind::ENTRY && plan.kind != kind::ADD {
        return Err(Refused::new(
            400,
            "NOT_AN_ENTRY",
            "only an entry or an add plan is confirmed here",
        ));
    }
    if plan.status != state::ISSUED {
        return Err(Refused::new(
            409,
            "NOT_ISSUED",
            format!("the plan is {}", plan.status),
        ));
    }
    if now >= plan.expires_at {
        return Err(Refused::new(
            410,
            "PLAN_EXPIRED",
            "the plan's 30-minute window has passed",
        ));
    }
    Ok(plan)
}

/// `execute_entry(...)`: `POST /fno/execute {plan_id, confirm: true}` — refuse, or send the
/// legs in `entry_seq` (F1/F3 through `run_entry`, longs first; F2 the future then its GTT).
///
/// # Errors
/// A [`Refused`] (an HTTP answer) or an exception (a 500).
pub async fn execute_entry(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    ctx: &FoCtx<'_>,
    plan_id: &str,
    confirm: bool,
    mode_of: &(dyn Fn(FoSleeve) -> Mode + Send + Sync),
) -> Result<FoOutcome, ExecError> {
    let found = if confirm {
        store.plan(plan_id).await?
    } else {
        None
    };
    let plan = validate(found, confirm, (ctx.now)())?;
    if mode_of(plan.sleeve) == Mode::Live {
        return Err(refused(
            409,
            "LIVE_NOT_BUILT",
            "live FO execution is not built (FO7.1); nothing sent",
        ));
    }
    for leg in &plan.legs {
        let books = ctx
            .quotes
            .quotes(std::slice::from_ref(&leg.tradingsymbol))
            .await?;
        if !books
            .get(&leg.tradingsymbol)
            .is_some_and(FoQuote::two_sided)
        {
            return Err(refused(
                503,
                "NO_QUOTE",
                format!("no two-sided quote for {}", leg.tradingsymbol),
            ));
        }
    }
    if !store.confirm(&plan, (ctx.now)()).await? {
        return Err(refused(
            409,
            "NOT_ISSUED",
            "the plan was confirmed or closed meanwhile",
        ));
    }
    store
        .set_status(&plan.plan_id, state::FILLING, None, &[])
        .await?;
    if plan.kind == kind::ADD {
        return add_to_spread(store, gateway, &plan, ctx).await;
    }
    if plan.structure == structure::FUTURE {
        return enter_future(store, gateway, &plan, ctx).await;
    }
    enter_condor(store, gateway, &plan, ctx).await
}

/// `execute_click_exit(...)`: `POST /fno/execute` on an EXIT the monitor raised for the click
/// (F3, M.5): an `ISSUED` EXIT is confirmed here and closed at once, shorts first.
///
/// # Errors
/// `CONFIRM_REQUIRED`, `UNKNOWN_PLAN`, `NOT_AN_EXIT`, `NOT_ISSUED`; or an exception.
pub async fn execute_click_exit(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    ctx: &FoCtx<'_>,
    plan_id: &str,
    confirm: bool,
) -> Result<FoOutcome, ExecError> {
    if !confirm {
        return Err(refused(400, "CONFIRM_REQUIRED", "confirm must be true"));
    }
    let Some(plan) = store.plan(plan_id).await? else {
        return Err(refused(404, "UNKNOWN_PLAN", "no such plan"));
    };
    if plan.kind != kind::EXIT {
        return Err(refused(
            400,
            "NOT_AN_EXIT",
            "only an exit plan is confirmed here",
        ));
    }
    if plan.status != state::ISSUED {
        return Err(refused(
            409,
            "NOT_ISSUED",
            format!("the plan is {}", plan.status),
        ));
    }
    if !store.confirm(&plan, (ctx.now)()).await? {
        return Err(refused(
            409,
            "NOT_ISSUED",
            "the plan was confirmed or closed meanwhile",
        ));
    }
    let Some(confirmed) = store.plan(plan_id).await? else {
        return Err(ExecError::Other("AssertionError".to_owned()));
    };
    execute_exit(store, gateway, &confirmed, ctx).await
}

fn legs_json(plan: &FoPlanRow, avg: &[(String, Option<Decimal>)]) -> PyValue {
    PyValue::List(
        plan.legs
            .iter()
            .map(|lg| {
                let price = avg
                    .iter()
                    .find(|(r, _)| *r == lg.role)
                    .and_then(|(_, p)| *p);
                PyValue::Map(crate::wire::dict([
                    ("role", PyValue::Str(lg.role.clone())),
                    ("tradingsymbol", PyValue::Str(lg.tradingsymbol.clone())),
                    ("instrument_token", PyValue::Int(lg.instrument_token)),
                    ("expiry", PyValue::Date(lg.expiry)),
                    ("strike", py::vdec(lg.strike)),
                    ("option_type", PyValue::Str(lg.option_type.clone())),
                    ("quantity", PyValue::Int(lg.quantity)),
                    ("avg_price", py::vdec(price)),
                ]))
            })
            .collect(),
    )
}

fn entry_roles(plan: &FoPlanRow) -> Vec<LegRole> {
    baskfy_core::fno::condor::ENTRY_SEQUENCE
        .iter()
        .filter(|r| plan.leg(r.value()).is_some())
        .filter_map(|r| role_of(r.value()))
        .collect()
}

/// The credit actually taken: shorts' prices minus longs'.
fn credit_of(
    result: &crate::lanes::options::core::ExecResult,
) -> (Vec<(String, Option<Decimal>)>, Decimal) {
    let mut avg = Vec::new();
    let mut credit = Decimal::ZERO;
    for (role, _) in &result.fills {
        let price = result.avg_price(*role);
        let p = price.unwrap_or(Decimal::ZERO);
        credit += if role.is_long() { -p } else { p };
        avg.push((role_str(*role).to_owned(), price));
    }
    (avg, money(credit))
}

fn detail_dec(detail: &PyDict, key: &str) -> Option<Decimal> {
    match detail.get(key) {
        None | Some(PyValue::None) => None,
        Some(PyValue::Str(s)) if s.is_empty() => None,
        Some(v) => py::dec_py(Some(v)),
    }
}

fn nonzero(d: Option<Decimal>) -> Decimal {
    d.unwrap_or(Decimal::ZERO)
}

/// `_spread_levels(credit)`: F3's decay-target and loss-cut marks (`04` §11).
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn spread_levels(credit: Decimal) -> Result<(Option<Decimal>, Option<Decimal>), ExecError> {
    if credit <= Decimal::ZERO {
        return Ok((None, None));
    }
    let f3 = FnoConfig::default().f3;
    let d3 = baskfy_core::fno::directional::decay_target_mark(credit, &f3).map_err(other)?;
    let l3 = baskfy_core::fno::directional::loss_cut_mark(credit, &f3).map_err(other)?;
    Ok((Some(d3), Some(l3)))
}

/// `_exit_levels(raw, credit)`: F1's profit-take and loss-close costs on the credit taken.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn exit_levels(
    raw: Option<&PyValue>,
    credit: Decimal,
) -> Result<(Option<Decimal>, Option<Decimal>), ExecError> {
    let Some(PyValue::Map(m)) = raw else {
        return Ok((None, None));
    };
    if credit <= Decimal::ZERO {
        return Ok((None, None));
    }
    let get = |k: &str| m.get(k).and_then(|v| py::dec_py(Some(v)));
    let (Some(lc), Some(sc), Some(sp), Some(lp)) = (
        get("LONG_CALL"),
        get("SHORT_CALL"),
        get("SHORT_PUT"),
        get("LONG_PUT"),
    ) else {
        return Ok((None, None));
    };
    let strikes = baskfy_core::fno::condor::CondorStrikes {
        long_call: lc,
        short_call: sc,
        short_put: sp,
        long_put: lp,
    };
    let f1 = FnoConfig::default().f1;
    let take = baskfy_core::fno::condor::profit_take_level(credit, &f1).map_err(other)?;
    let close = baskfy_core::fno::condor::loss_close_level(credit, &strikes, &f1).map_err(other)?;
    Ok((Some(money(take)), Some(money(close))))
}

fn left_open(result: &crate::lanes::options::core::ExecResult) -> PyDict {
    let mut left = PyDict::new();
    for (role, q) in &result.position {
        if *q != 0 {
            left.insert(role_str(*role), PyValue::Int(*q));
        }
    }
    left
}

fn left_text(left: &PyDict) -> String {
    PyValue::Map(left.clone()).py_str()
}

async fn enter_condor(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    plan: &FoPlanRow,
    ctx: &FoCtx<'_>,
) -> Result<FoOutcome, ExecError> {
    let quantity = plan.lots * plan.lot_size;
    let roles = entry_roles(plan);
    let (result, orders, refusals) = {
        let mut venue = OptionVenue {
            gateway: gateway.clone(),
            store,
            plan: plan.clone(),
            underlying: plan.symbol.clone(),
            ctx,
            position_id: None,
            held: Vec::new(),
            closing_is_exit: false,
            execution: ExecutionConfig::default(),
            orders: 0,
            refusals: Vec::new(),
        };
        let result = run_entry(
            &mut venue,
            &roles,
            quantity,
            CONDOR_RULES,
            tick(),
            &ExecutionConfig::default(),
        )
        .await?;
        (result, venue.orders, venue.refusals)
    };
    store.settle_legs(plan).await?;
    if result.outcome == Outcome::Open {
        let (avg, credit) = credit_of(&result);
        let detail = &plan.detail;
        let width = nonzero(detail_dec(detail, "width_points"));
        let max_loss = if width.is_zero() {
            None
        } else {
            Some(money((width - credit) * Decimal::from(quantity)))
        };
        let levels = exit_levels(detail.get("strikes"), credit)?;
        let spread = plan.structure == structure::CREDIT_SPREAD;
        let get = |k: &str| detail.get(k).cloned().unwrap_or(PyValue::None);
        let mut carry = crate::wire::dict([("strikes", get("strikes"))]);
        if spread {
            carry.insert("direction", get("direction"));
            carry.insert("level", get("level"));
            carry.insert("added", PyValue::Bool(false));
        }
        let (take, cut) = if spread {
            spread_levels(credit)?
        } else {
            levels
        };
        let legs = crate::wire::dict([
            ("legs", legs_json(plan, &avg)),
            ("carry", PyValue::Map(carry)),
        ]);
        let position_id = store
            .open_position(&[
                ("sleeve", Val::Str(plan.sleeve.value().to_owned())),
                ("symbol", Val::Str(plan.symbol.clone())),
                ("structure", Val::Str(plan.structure.clone())),
                ("entry_plan_id", Val::Str(plan.plan_id.clone())),
                ("legs", Val::Json(PyValue::Map(legs))),
                ("lots", Val::Int(plan.lots)),
                ("lot_size", Val::Int(plan.lot_size)),
                ("entry_credit", Val::Dec(credit)),
                ("max_loss_inr", Val::dec(max_loss)),
                ("profit_take_points", Val::dec(take)),
                ("loss_close_points", Val::dec(cut)),
                (
                    "stop_price",
                    Val::dec(if spread {
                        detail_dec(detail, "level")
                    } else {
                        None
                    }),
                ),
                ("hard_exit_date", Val::date(plan.hard_exit_date)),
                ("opened_at", Val::At((ctx.now)())),
                ("simulated", Val::Bool(true)),
            ])
            .await?;
        store.attach_fills(plan, position_id).await?;
        store
            .set_status(&plan.plan_id, state::OPEN, None, &[])
            .await?;
        return Ok(FoOutcome::new(plan, "OPEN", orders).with(
            Some(position_id),
            crate::wire::dict([("credit", dstr(credit))]),
        ));
    }
    let left = left_open(&result);
    let mut reason = "a leg did not fill in full; the entry was abandoned before any short was \
                      left uncovered and the filled longs were closed at once"
        .to_owned();
    if !refusals.is_empty() {
        reason.push_str(&format!(" ({})", refusals.join("; ")));
    }
    abandon(store, plan, &reason, &left, &refusals).await?;
    if left.is_empty() {
        journal_abandoned(store, plan, ctx).await?;
    } else {
        (ctx.alert)(&format!(
            "FO {}: abandoned entry left {} open; close it by hand",
            plan.plan_id,
            left_text(&left)
        ));
    }
    Ok(FoOutcome::new(plan, state::ABANDONED_PARTIAL, orders)
        .with(None, abandoned_detail(&left, &refusals)))
}

fn abandoned_detail(left: &PyDict, refusals: &[String]) -> PyDict {
    crate::wire::dict([
        ("left_open", PyValue::Map(left.clone())),
        (
            "refusals",
            PyValue::List(refusals.iter().cloned().map(PyValue::Str).collect()),
        ),
    ])
}

async fn abandon(
    store: &mut FoStore<'_>,
    plan: &FoPlanRow,
    reason: &str,
    left: &PyDict,
    refusals: &[String],
) -> Result<(), ExecError> {
    store
        .set_status(&plan.plan_id, state::ABANDONED_PARTIAL, Some(reason), &[])
        .await?;
    store
        .merge_detail(&plan.plan_id, &abandoned_detail(left, refusals))
        .await?;
    Ok(())
}

async fn add_to_spread(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    plan: &FoPlanRow,
    ctx: &FoCtx<'_>,
) -> Result<FoOutcome, ExecError> {
    let Some(position) = position_for(store, plan).await? else {
        store
            .set_status(
                &plan.plan_id,
                state::CLOSED,
                Some("no open position to add to"),
                &[],
            )
            .await?;
        return Ok(FoOutcome::new(plan, "NO_POSITION", 0));
    };
    let quantity = plan.lots * plan.lot_size;
    let roles = entry_roles(plan);
    let (result, orders, refusals) = {
        let mut venue = OptionVenue {
            gateway: gateway.clone(),
            store,
            plan: plan.clone(),
            underlying: plan.symbol.clone(),
            ctx,
            position_id: Some(position.id),
            held: Vec::new(),
            closing_is_exit: false,
            execution: ExecutionConfig::default(),
            orders: 0,
            refusals: Vec::new(),
        };
        let result = run_entry(
            &mut venue,
            &roles,
            quantity,
            CONDOR_RULES,
            tick(),
            &ExecutionConfig::default(),
        )
        .await?;
        (result, venue.orders, venue.refusals)
    };
    store.settle_legs(plan).await?;
    if result.outcome == Outcome::Open {
        let (_avg, credit) = credit_of(&result);
        let old_units = Decimal::from(position.lots * position.lot_size);
        let old_credit = position.entry_credit.unwrap_or(Decimal::ZERO);
        let q = Decimal::from(quantity);
        let new_credit =
            money(py::div(old_credit * old_units + credit * q, old_units + q).map_err(other)?);
        let width = nonzero(detail_dec(&plan.detail, "width_points"));
        let add_loss = if width.is_zero() {
            Decimal::ZERO
        } else {
            money((width - credit) * q)
        };
        let mut legs_now = position.leg_list();
        for lg in &plan.legs {
            if let Some(held) = legs_now
                .iter_mut()
                .rev()
                .find(|x| x.get("role").map(PyValue::py_str).as_deref() == Some(lg.role.as_str()))
            {
                let was = py::py_int(held.get("quantity"), 0);
                held.insert("quantity", PyValue::Int(was + lg.quantity));
            }
        }
        let mut carry = position.carry();
        carry.insert("added", PyValue::Bool(true));
        carry.insert("add_plan_id", PyValue::Str(plan.plan_id.clone()));
        carry.insert("add_credit", dstr(credit));
        let legs = crate::wire::dict([
            (
                "legs",
                PyValue::List(legs_now.into_iter().map(PyValue::Map).collect()),
            ),
            ("carry", PyValue::Map(carry)),
        ]);
        let lots = position.lots + plan.lots;
        store
            .update_position(
                position.id,
                &[
                    ("lots", Val::Int(lots)),
                    ("entry_credit", Val::Dec(new_credit)),
                    (
                        "max_loss_inr",
                        Val::Dec(position.max_loss_inr.unwrap_or(Decimal::ZERO) + add_loss),
                    ),
                    ("legs", Val::Json(PyValue::Map(legs))),
                ],
            )
            .await?;
        store.attach_fills(plan, position.id).await?;
        store
            .set_status(&plan.plan_id, state::OPEN, None, &[])
            .await?;
        return Ok(FoOutcome::new(plan, "OPEN", orders).with(
            Some(position.id),
            crate::wire::dict([("credit", dstr(credit)), ("lots", PyValue::Int(lots))]),
        ));
    }
    let left = left_open(&result);
    let mut reason =
        "a leg of the add did not fill in full; the add was abandoned and the position is unchanged"
            .to_owned();
    if !refusals.is_empty() {
        reason.push_str(&format!(" ({})", refusals.join("; ")));
    }
    abandon(store, plan, &reason, &left, &refusals).await?;
    if !left.is_empty() {
        (ctx.alert)(&format!(
            "FO {}: abandoned add left {} open; close it by hand",
            plan.plan_id,
            left_text(&left)
        ));
    }
    Ok(FoOutcome::new(plan, state::ABANDONED_PARTIAL, orders)
        .with(None, abandoned_detail(&left, &refusals)))
}

async fn enter_future(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    plan: &FoPlanRow,
    ctx: &FoCtx<'_>,
) -> Result<FoOutcome, ExecError> {
    let Some(leg) = plan.legs.first().cloned() else {
        return Err(ExecError::Other(
            "IndexError: tuple index out of range".to_owned(),
        ));
    };
    let execution = ExecutionConfig::default();
    let (filled, avg, refusals) = send_future(
        store,
        gateway,
        plan,
        &leg,
        Side::Buy,
        leg.quantity,
        false,
        ctx,
        None,
        &execution,
    )
    .await?;
    let orders = refusals.len() as i64 + i64::from(filled != 0);
    let entry = match avg {
        Some(a) if filled >= leg.quantity => a,
        _ => {
            if filled != 0 {
                send_future(
                    store,
                    gateway,
                    plan,
                    &leg,
                    Side::Sell,
                    filled,
                    true,
                    ctx,
                    None,
                    &execution,
                )
                .await?;
            }
            store.settle_legs(plan).await?;
            let mut reason = format!(
                "the future filled {filled} of {}; the entry was abandoned",
                leg.quantity
            );
            if !refusals.is_empty() {
                reason.push_str(&format!(" ({})", refusals.join("; ")));
            }
            store
                .set_status(&plan.plan_id, state::ABANDONED_PARTIAL, Some(&reason), &[])
                .await?;
            if filled != 0 {
                journal_abandoned(store, plan, ctx).await?;
            }
            return Ok(FoOutcome::new(plan, state::ABANDONED_PARTIAL, orders).with(
                None,
                crate::wire::dict([(
                    "refusals",
                    PyValue::List(refusals.into_iter().map(PyValue::Str).collect()),
                )]),
            ));
        }
    };
    store.settle_legs(plan).await?;
    let d = &plan.detail;
    let atr = nonzero(detail_dec(d, "atr14"));
    let cfg = FnoConfig::default();
    let ann_vol = match d.get("ann_vol") {
        Some(v) if v.truthy() => match v {
            PyValue::Float(f) => *f,
            PyValue::Int(i) => *i as f64,
            other => other.py_str().parse::<f64>().map_err(|e| {
                ExecError::Other(format!(
                    "ValueError: could not convert string to float: {e}"
                ))
            })?,
        },
        _ => 0.0,
    };
    let stop = money(baskfy_core::fno::exits::f2_initial_stop(entry, atr, &cfg.f2).map_err(other)?);
    let trigger = money(
        baskfy_core::fno::exits::gtt_trigger(stop, entry, ann_vol, &cfg.stop_vol).map_err(other)?,
    );
    let get = |k: &str| d.get(k).cloned().unwrap_or(PyValue::None);
    let carry = crate::wire::dict([
        ("trail_stop", dstr(stop)),
        ("highest_close", dstr(entry)),
        ("atr_at_entry", dstr(atr)),
        ("ann_vol", PyValue::Float(ann_vol)),
        ("entry_session", PyValue::Date(plan.trade_date)),
        ("time_exit_date", get("time_exit_date")),
        ("contract_expiry", PyValue::Date(leg.expiry)),
        ("rolls", PyValue::Int(0)),
        ("realised_inr", PyValue::Str("0".to_owned())),
        ("lots_at_ceiling", get("lots_at_ceiling")),
    ]);
    let legs = crate::wire::dict([
        ("legs", legs_json(plan, &[(leg.role.clone(), Some(entry))])),
        ("carry", PyValue::Map(carry)),
    ]);
    let roll_date = d.get("roll_date").and_then(|v| py::opt_day_py(Some(v)));
    let position_id = store
        .open_position(&[
            ("sleeve", Val::Str(plan.sleeve.value().to_owned())),
            ("symbol", Val::Str(plan.symbol.clone())),
            ("structure", Val::Str(plan.structure.clone())),
            ("entry_plan_id", Val::Str(plan.plan_id.clone())),
            ("legs", Val::Json(PyValue::Map(legs))),
            ("lots", Val::Int(plan.lots)),
            ("lot_size", Val::Int(plan.lot_size)),
            ("entry_price", Val::Dec(entry)),
            (
                "max_loss_inr",
                Val::Dec(money((entry - trigger) * Decimal::from(leg.quantity))),
            ),
            ("stop_price", Val::Dec(trigger)),
            ("next_roll_date", Val::date(roll_date)),
            ("opened_at", Val::At((ctx.now)())),
            ("simulated", Val::Bool(true)),
        ])
        .await?;
    store.attach_fills(plan, position_id).await?;
    store
        .set_status(&plan.plan_id, state::OPEN, None, &[])
        .await?;
    let last = last_or_mid(ctx, &leg.tradingsymbol).await?;
    let handle = place_future_stop(
        store,
        gateway,
        position_id,
        plan,
        &leg.tradingsymbol,
        leg.quantity,
        trigger,
        last.unwrap_or(entry),
        ctx,
    )
    .await?;
    Ok(FoOutcome::new(plan, "OPEN", orders + 1).with(
        Some(position_id),
        crate::wire::dict([
            ("entry", dstr(entry)),
            ("trigger", dstr(trigger)),
            ("gtt", handle.map_or(PyValue::None, PyValue::Str)),
        ]),
    ))
}

/// `(book.last or book.mid) if book is not None else None` — `or` treats `Decimal(0)` as
/// missing.
async fn last_or_mid(ctx: &FoCtx<'_>, symbol: &str) -> Result<Option<Decimal>, ExecError> {
    let books = ctx.quotes.quotes(&[symbol.to_owned()]).await?;
    Ok(books.get(symbol).and_then(|b| {
        b.last
            .filter(|l| !l.is_zero())
            .or_else(|| b.mid())
            .filter(|v| !v.is_zero())
    }))
}

// --- exits and rolls under the entry's confirm --------------------------------------------------

async fn position_for(
    store: &mut FoStore<'_>,
    plan: &FoPlanRow,
) -> Result<Option<PositionRow>, ExecError> {
    match plan.detail.get("position_id") {
        None | Some(PyValue::None) => Ok(None),
        Some(v) => {
            let id = match v {
                PyValue::Int(i) => *i,
                other => other.py_str().parse::<i64>().map_err(|_| {
                    ExecError::Other(format!(
                        "ValueError: invalid literal for int() with base 10: {}",
                        other.py_repr()
                    ))
                })?,
            };
            Ok(store.position(id).await?)
        }
    }
}

fn net_get(net: &[(String, i64)], symbol: &str) -> i64 {
    net.iter().find(|(s, _)| s == symbol).map_or(0, |(_, n)| *n)
}

/// `execute_exit(...)`: close a position, shorts first (`04` §2). `CLOSED` when flat;
/// otherwise the plan stays `FILLING` and the next pass tries again — never naked.
///
/// # Errors
/// A refusal (no quote) or an exception.
pub async fn execute_exit(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    plan: &FoPlanRow,
    ctx: &FoCtx<'_>,
) -> Result<FoOutcome, ExecError> {
    let Some(position) = position_for(store, plan).await? else {
        store
            .set_status(
                &plan.plan_id,
                state::CLOSED,
                Some("no open position to close"),
                &[],
            )
            .await?;
        return Ok(FoOutcome::new(plan, "NO_POSITION", 0));
    };
    store
        .set_status(&plan.plan_id, state::FILLING, None, &[])
        .await?;
    store
        .set_status(
            &position.entry_plan_id,
            state::EXITING,
            None,
            &[state::OPEN],
        )
        .await?;
    let net = store.net_by_symbol(position.id).await?;
    let mut orders = 0;
    let flat;
    if position.structure == structure::IRON_CONDOR
        || position.structure == structure::CREDIT_SPREAD
    {
        let mut held: Vec<(String, i64)> = Vec::new();
        for (sym, q) in &net {
            if let Some(lg) = plan.legs.iter().rev().find(|l| &l.tradingsymbol == sym) {
                if let Some(slot) = held.iter_mut().find(|(r, _)| *r == lg.role) {
                    slot.1 = *q;
                } else {
                    held.push((lg.role.clone(), *q));
                }
            }
        }
        let mut open_qty: crate::lanes::options::core::Position = Vec::new();
        for (r, q) in &held {
            if *q != 0 {
                let role = role_of(r).ok_or_else(|| {
                    ExecError::Other(format!("ValueError: '{r}' is not a valid LegRole"))
                })?;
                open_qty.push((role, q.abs()));
            }
        }
        let (result, sent) = {
            let mut venue = OptionVenue {
                gateway: gateway.clone(),
                store,
                plan: plan.clone(),
                underlying: position.symbol.clone(),
                ctx,
                position_id: Some(position.id),
                held,
                closing_is_exit: true,
                execution: ExecutionConfig::default(),
                orders: 0,
                refusals: Vec::new(),
            };
            let result = run_exit(
                &mut venue,
                open_qty,
                CONDOR_RULES,
                tick(),
                &ExecutionConfig::default(),
            )
            .await?;
            (result, venue.orders)
        };
        orders = sent;
        flat = result.outcome == Outcome::Flat;
    } else {
        for leg in &plan.legs {
            let units = net_get(&net, &leg.tradingsymbol);
            if units > 0 {
                let (_f, _a, refusals) = send_future(
                    store,
                    gateway,
                    plan,
                    leg,
                    Side::Sell,
                    units,
                    true,
                    ctx,
                    Some(position.id),
                    &ExecutionConfig::default(),
                )
                .await?;
                orders += 1 + refusals.len() as i64;
            }
        }
        flat = store
            .net_by_symbol(position.id)
            .await?
            .iter()
            .all(|(_, q)| *q == 0);
    }
    store.settle_legs(plan).await?;
    if !flat {
        return Ok(
            FoOutcome::new(plan, "PARTIAL_EXIT", orders).with(Some(position.id), PyDict::new())
        );
    }
    let reason = match plan.detail.get("code") {
        Some(v) if v.truthy() => v.py_str(),
        _ => "EXIT".to_owned(),
    };
    let at = (ctx.now)();
    store.close_position(position.id, &reason, at).await?;
    store
        .set_status(&plan.plan_id, state::CLOSED, None, &[])
        .await?;
    store
        .set_status(
            &position.entry_plan_id,
            state::CLOSED,
            None,
            &[state::OPEN, state::EXITING],
        )
        .await?;
    if position.structure == structure::FUTURE && reason != "STOP" {
        for leg in &plan.legs {
            delete_future_stop(gateway, &position, &leg.tradingsymbol, ctx).await?;
        }
    }
    // `_after_close(store, position.id, now(), log.error)`: an exit's journal failure is
    // logged, not the caller's alert (Python passes `log.error` here, whatever the caller's).
    ctx.ledger
        .after_close(store, position.id, (ctx.now)(), &log_error)
        .await?;
    Ok(FoOutcome::new(plan, "CLOSED", orders).with(
        Some(position.id),
        crate::wire::dict([("reason", PyValue::Str(reason))]),
    ))
}

async fn fill_avg(
    store: &mut FoStore<'_>,
    leg: &FoLegRow,
    side: &str,
) -> Result<Option<Decimal>, ExecError> {
    let sql = format!(
        "SELECT COALESCE(SUM(quantity), 0) AS q, SUM(quantity * price) AS v FROM {} \
         WHERE leg_id = ? AND side = ?",
        store.t("fo_fill")
    );
    let row = store
        .one(
            &sql,
            &[
                crate::storage::Param::Int(leg.id),
                crate::storage::Param::Text(side.to_owned()),
            ],
        )
        .await?
        .unwrap_or_default();
    let q = py::int(py::col(&row, "q"));
    if q == 0 {
        return Ok(None);
    }
    let v = py::dec(py::col(&row, "v")).unwrap_or(Decimal::ZERO);
    Ok(Some(money(py::div(v, Decimal::from(q)).map_err(other)?)))
}

/// `execute_roll(...)`: F2's calendar roll — the held month sold, the next bought, same
/// quantity, stop carried, under the original confirm.
///
/// # Errors
/// A refusal or an exception.
#[allow(clippy::too_many_lines)]
pub async fn execute_roll(
    store: &mut FoStore<'_>,
    gateway: &Arc<dyn Gateway>,
    plan: &FoPlanRow,
    ctx: &FoCtx<'_>,
) -> Result<FoOutcome, ExecError> {
    let position = position_for(store, plan).await?;
    let (Some(position), [old, new]) = (position, plan.legs.as_slice()) else {
        store
            .set_status(
                &plan.plan_id,
                state::CLOSED,
                Some("no open position to roll"),
                &[],
            )
            .await?;
        return Ok(FoOutcome::new(plan, "NO_POSITION", 0));
    };
    store
        .set_status(&plan.plan_id, state::FILLING, None, &[])
        .await?;
    let execution = ExecutionConfig::default();
    let net = store.net_by_symbol(position.id).await?;
    let mut orders = 0;
    let held = net_get(&net, &old.tradingsymbol);
    if held > 0 {
        let (_f, _a, refusals) = send_future(
            store,
            gateway,
            plan,
            old,
            Side::Sell,
            held,
            true,
            ctx,
            Some(position.id),
            &execution,
        )
        .await?;
        orders += 1 + refusals.len() as i64;
        if net_get(&store.net_by_symbol(position.id).await?, &old.tradingsymbol) > 0 {
            store.settle_legs(plan).await?;
            return Ok(
                FoOutcome::new(plan, "PARTIAL_ROLL", orders).with(Some(position.id), PyDict::new())
            );
        }
    }
    let mut bought = net_get(&store.net_by_symbol(position.id).await?, &new.tradingsymbol);
    let mut refusals: Vec<String> = Vec::new();
    let mut avg: Option<Decimal> = None;
    if bought < new.quantity {
        let (_f, a, r) = send_future(
            store,
            gateway,
            plan,
            new,
            Side::Buy,
            new.quantity - bought,
            false,
            ctx,
            Some(position.id),
            &execution,
        )
        .await?;
        avg = a;
        refusals = r;
        orders += 1 + refusals.len() as i64;
    }
    store.settle_legs(plan).await?;
    bought = net_get(&store.net_by_symbol(position.id).await?, &new.tradingsymbol);
    if bought < new.quantity {
        if bought > 0 {
            send_future(
                store,
                gateway,
                plan,
                new,
                Side::Sell,
                bought,
                true,
                ctx,
                Some(position.id),
                &execution,
            )
            .await?;
        }
        let reason = format!(
            "the roll sold {} but bought {bought} of {}",
            old.tradingsymbol, new.quantity
        );
        let full = if refusals.is_empty() {
            reason.clone()
        } else {
            format!("{reason} ({})", refusals.join("; "))
        };
        store
            .set_status(&plan.plan_id, state::ABANDONED_PARTIAL, Some(&full), &[])
            .await?;
        store
            .close_position(position.id, "ROLL_INCOMPLETE", (ctx.now)())
            .await?;
        store
            .set_status(&position.entry_plan_id, state::CLOSED, None, &[state::OPEN])
            .await?;
        delete_future_stop(gateway, &position, &old.tradingsymbol, ctx).await?;
        (ctx.alert)(&format!(
            "F2 {}: {reason}; the position is flat",
            position.symbol
        ));
        ctx.ledger
            .after_close(store, position.id, (ctx.now)(), ctx.alert)
            .await?;
        return Ok(
            FoOutcome::new(plan, "ROLL_INCOMPLETE", orders).with(Some(position.id), PyDict::new())
        );
    }
    let leg_list = position.leg_list();
    let old_leg = leg_list.iter().find(|x| {
        x.get("tradingsymbol").map(PyValue::py_str).as_deref() == Some(old.tradingsymbol.as_str())
    });
    let old_avg = old_leg.and_then(|x| match x.get("avg_price") {
        None | Some(PyValue::None) => None,
        Some(v) => py::dec_py(Some(v)),
    });
    let old_entry = old_avg
        .filter(|d| !d.is_zero())
        .or(position.entry_price.filter(|d| !d.is_zero()))
        .unwrap_or(Decimal::ZERO);
    let sold = fill_avg(store, old, "SELL").await?;
    let mut carry = position.carry();
    let realised_before = match carry.get("realised_inr") {
        None | Some(PyValue::None) => Decimal::ZERO,
        Some(v) => py::dec_py(Some(v)).unwrap_or(Decimal::ZERO),
    };
    let realised = realised_before
        + sold.map_or(Decimal::ZERO, |s| {
            (s - old_entry) * Decimal::from(old.quantity)
        });
    let new_avg = match avg.filter(|a| !a.is_zero()) {
        Some(a) => a,
        None => fill_avg(store, new, "BUY")
            .await?
            .filter(|a| !a.is_zero())
            .unwrap_or(Decimal::ZERO),
    };
    let rolls = py::py_int(carry.get("rolls"), 0);
    carry.insert("rolls", PyValue::Int(rolls + 1));
    carry.insert("realised_inr", dstr(money(realised)));
    carry.insert("contract_expiry", PyValue::Date(new.expiry));
    let legs = PyValue::List(vec![PyValue::Map(crate::wire::dict([
        ("role", PyValue::Str(FUTURE_ROLE.to_owned())),
        ("tradingsymbol", PyValue::Str(new.tradingsymbol.clone())),
        ("instrument_token", PyValue::Int(new.instrument_token)),
        ("expiry", PyValue::Date(new.expiry)),
        ("strike", PyValue::None),
        ("option_type", PyValue::Str(FUTURE_TYPE.to_owned())),
        ("quantity", PyValue::Int(new.quantity)),
        ("avg_price", dstr(new_avg)),
    ]))]);
    let next_roll = plan
        .detail
        .get("next_roll_date")
        .and_then(|v| py::opt_day_py(Some(v)));
    store
        .update_position(
            position.id,
            &[
                (
                    "legs",
                    Val::Json(PyValue::Map(crate::wire::dict([
                        ("legs", legs),
                        ("carry", PyValue::Map(carry)),
                    ]))),
                ),
                ("next_roll_date", Val::date(next_roll)),
            ],
        )
        .await?;
    store
        .set_status(&plan.plan_id, state::OPEN, None, &[])
        .await?;
    delete_future_stop(gateway, &position, &old.tradingsymbol, ctx).await?;
    let trigger = position.stop_price.unwrap_or(Decimal::ZERO);
    let last = last_or_mid(ctx, &new.tradingsymbol).await?;
    place_future_stop(
        store,
        gateway,
        position.id,
        plan,
        &new.tradingsymbol,
        new.quantity,
        trigger,
        last.unwrap_or(new_avg),
        ctx,
    )
    .await?;
    Ok(FoOutcome::new(plan, "OPEN", orders).with(
        Some(position.id),
        crate::wire::dict([("rolled_to", PyValue::Str(new.tradingsymbol.clone()))]),
    ))
}

async fn journal_abandoned(
    store: &mut FoStore<'_>,
    plan: &FoPlanRow,
    ctx: &FoCtx<'_>,
) -> Result<(), ExecError> {
    let at = (ctx.now)();
    if let Some(position_id) = ctx.ledger.journal_abandoned(store, plan, at).await? {
        ctx.ledger
            .after_close(store, position_id, at, ctx.alert)
            .await?;
    }
    Ok(())
}

/// `run_pending(...)`: every EXIT and ROLL plan the monitor raised, sent under its entry's
/// confirm. **Never an ENTRY or an ADD** — `pending_actions` selects neither. A plan that
/// cannot get a quote this pass waits for the next.
///
/// # Errors
/// An exception (a refusal only makes the plan wait).
pub async fn run_pending(
    store: &mut FoStore<'_>,
    gateway_for: &(dyn Fn(FoSleeve) -> Option<Arc<dyn Gateway>> + Send + Sync),
    ctx: &FoCtx<'_>,
) -> Result<Vec<FoOutcome>, ExecError> {
    let mut done = Vec::new();
    for plan in store.pending_actions().await? {
        if plan.kind != kind::EXIT && plan.kind != kind::ROLL {
            continue; // belt and braces: an entry is a click, never a pass (non-negotiable 1)
        }
        let Some(gateway) = gateway_for(plan.sleeve) else {
            (ctx.alert)(&format!(
                "fo {} waits: no FO gateway could be built",
                plan.plan_id
            ));
            continue;
        };
        let sent = if plan.kind == kind::ROLL {
            execute_roll(store, &gateway, &plan, ctx).await
        } else {
            execute_exit(store, &gateway, &plan, ctx).await
        };
        match sent {
            Ok(o) => done.push(o),
            Err(ExecError::Refused(_waits)) => {} // `log.warning("fo %s waits")`: next pass
            Err(e) => return Err(e),
        }
    }
    Ok(done)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::*;

    fn plan(kind_: &str, status: &str) -> FoPlanRow {
        let at = chrono::DateTime::parse_from_rfc3339("2026-11-12T09:20:00+05:30").unwrap();
        FoPlanRow {
            pk: 1,
            plan_id: "F3N-20261112-NIFTY-u1".to_owned(),
            sleeve: FoSleeve::F3N,
            symbol: "NIFTY".to_owned(),
            trade_date: at.date_naive(),
            structure: structure::CREDIT_SPREAD.to_owned(),
            kind: kind_.to_owned(),
            status: status.to_owned(),
            issued_at: at,
            expires_at: at + chrono::Duration::minutes(30),
            lots: 1,
            lot_size: 65,
            parent_plan_id: None,
            hard_exit_date: None,
            confirmed_at: None,
            detail: PyDict::new(),
            legs: Vec::new(),
        }
    }

    fn code(r: Result<FoPlanRow, Refused>) -> (u16, String) {
        let e = r.err().unwrap();
        (e.status, e.code)
    }

    #[test]
    fn the_confirm_refuses_in_pythons_order() {
        let p = plan(kind::ENTRY, state::ISSUED);
        let in_window = p.issued_at + chrono::Duration::minutes(5);
        assert_eq!(
            code(validate(Some(p.clone()), false, in_window)),
            (400, "CONFIRM_REQUIRED".into())
        );
        assert_eq!(
            code(validate(None, true, in_window)),
            (404, "UNKNOWN_PLAN".into())
        );
        assert_eq!(
            code(validate(
                Some(plan(kind::EXIT, state::ISSUED)),
                true,
                in_window
            )),
            (400, "NOT_AN_ENTRY".into())
        );
        assert_eq!(
            code(validate(
                Some(plan(kind::ROLL, state::ISSUED)),
                true,
                in_window
            )),
            (400, "NOT_AN_ENTRY".into())
        );
        let lapsed = validate(Some(plan(kind::ENTRY, "LAPSED")), true, in_window)
            .err()
            .unwrap();
        assert_eq!(
            (lapsed.status, lapsed.code.as_str(), lapsed.message.as_str()),
            (409, "NOT_ISSUED", "the plan is LAPSED")
        );
        // 30 minutes is the window: at expires_at exactly the plan is expired.
        assert_eq!(
            code(validate(Some(p.clone()), true, p.expires_at)),
            (410, "PLAN_EXPIRED".into())
        );
        assert!(
            validate(
                Some(p.clone()),
                true,
                p.expires_at - chrono::Duration::seconds(1)
            )
            .is_ok()
        );
        assert!(validate(Some(plan(kind::ADD, state::ISSUED)), true, in_window).is_ok());
    }

    #[test]
    fn this_source_names_no_auto_execute_flag_and_no_broker() {
        let text = include_str!("execute.rs");
        let code = text.split("#[cfg(test)]").next().unwrap_or("");
        assert!(!code.contains("AUTO_EXEC"));
        assert!(!code.contains(concat!("Kite", "Writes")));
        assert!(!code.contains("place_order"));
        let monitor = include_str!("monitor.rs");
        let mcode = monitor.split("#[cfg(test)]").next().unwrap_or("");
        assert!(!mcode.contains("AUTO_EXEC"));
        assert!(
            !mcode.contains("execute_entry"),
            "the monitor never confirms an entry"
        );
    }

    #[test]
    fn client_ids_are_plan_symbol_and_never_reused() {
        assert_eq!(client_id("P", "NIFTY24NOVFUT", false, 1), "P:NIFTY24NOVFUT");
        assert_eq!(client_id("P", "X", true, 1), "P:X:CLOSE");
        assert_eq!(client_id("P", "X", false, 2), "P:X:R2");
        assert_eq!(client_id("P", "X", true, 3), "P:X:CLOSE:R3");
    }

    #[test]
    fn gtt_handles() {
        assert_eq!(paper_gtt_handle(7), "PAPER-7");
        assert_eq!(gtt_handle_int(Some("PAPER-7")), Some(7));
        assert_eq!(gtt_handle_int(Some("123")), Some(123));
        assert_eq!(gtt_handle_int(Some("PAPER-0")), None);
        assert_eq!(gtt_handle_int(Some("abc")), None);
        assert_eq!(gtt_handle_int(Some("")), None);
        assert_eq!(gtt_handle_int(None), None);
    }

    #[test]
    fn spread_levels_need_a_credit() {
        assert!(matches!(spread_levels(Decimal::ZERO), Ok((None, None))));
        assert!(matches!(exit_levels(None, Decimal::ONE), Ok((None, None))));
    }
}
