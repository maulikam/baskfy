//! Ports `app.options_execute` (OP10): confirming an options plan and closing its position, on
//! paper.
//!
//! **Law 2.** Every leg is a [`Gateway::place`] — `exchange="NFO"`, `product="MIS"`,
//! `order_type="LIMIT"` — on a gateway derived per sleeve with
//! [`super::gates::product_gates`] read at the moment of each order. In PAPER that gateway
//! carries `dry_run=true`, so every leg runs the real guards, risk check, rate limit and journal
//! and comes back `DRY_RUN` without a broker call; the fill is then simulated by walking the
//! leg's live depth (`simulate_fill`, `04` §8.4) and written with `simulated=true`.
//!
//! **LIVE is refused** (`LIVE_NOT_BUILT`, OP10.3). **Nothing here is scheduled**: an entry runs
//! only from `POST /nifty-options/execute` with `confirm=true`; an exit from `POST
//! /nifty-options/close` or the monitor's exit plan (the entry's confirm at work, PACK.2).

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock, Weak};

use chrono::{DateTime, FixedOffset};
use rust_decimal::Decimal;

use super::core::{
    Attempt, Book, ExecResult, ExecutionConfig, Fill, LegRole, Level, Mode, Outcome, Side, Sleeve,
    Structure, Venue, VenueError, role_str, run_entry, run_exit, sleeve_str,
};
use super::py;
use super::store::{LegRow, PgOptionsStore, PlanRow, StoreError};
use crate::app::AppState;
use crate::gateway::{DerivedGateway, Gateway, PlaceOrder};
use crate::kite::KiteReads;
use crate::wire::{PyDict, PyValue};

/// `TICK`.
#[must_use]
pub fn tick() -> Decimal {
    Decimal::new(5, 2)
}

/// `DRY_RUN`.
pub const DRY_RUN: &str = "DRY_RUN";

/// `Refused(status, code, message)`: a confirm the desk answers before any order.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct Refused {
    /// HTTP status.
    pub status: u16,
    /// Code.
    pub code: String,
    /// Message.
    pub message: String,
}

impl Refused {
    /// `Refused(status, code, message)`.
    #[must_use]
    pub fn new(status: u16, code: &str, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.to_owned(),
            message: message.into(),
        }
    }
}

/// Everything that can stop a confirm: a refusal (an HTTP answer) or an exception (a 500).
#[derive(Debug, thiserror::Error)]
pub enum ExecError {
    /// `Refused`.
    #[error("{0}")]
    Refused(Refused),
    /// Anything else: Python lets it propagate (Starlette answers 500).
    #[error("{0}")]
    Other(String),
}

impl From<StoreError> for ExecError {
    fn from(e: StoreError) -> Self {
        Self::Other(e.py_text())
    }
}

impl From<VenueError> for ExecError {
    fn from(e: VenueError) -> Self {
        match e {
            VenueError::Refused {
                status,
                code,
                message,
            } => Self::Refused(Refused {
                status,
                code,
                message,
            }),
            VenueError::Other(m) => Self::Other(m),
        }
    }
}

impl From<Refused> for ExecError {
    fn from(r: Refused) -> Self {
        Self::Refused(r)
    }
}

/// A leg's live book (`Quote`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Quote {
    /// Best bid.
    pub bid: Decimal,
    /// Best ask.
    pub ask: Decimal,
    /// The bid ladder.
    pub bids: Vec<Level>,
    /// The ask ladder.
    pub asks: Vec<Level>,
}

/// `QuoteSource`: an instrument token's book, or a refusal (`NO_QUOTE`) / an exception.
#[async_trait::async_trait]
pub trait QuoteSource: Send + Sync {
    /// `quotes(token)`.
    ///
    /// # Errors
    /// `Refused(503, "NO_QUOTE")` without a two-sided book; anything Kite raises.
    async fn quote(&self, token: i64) -> Result<Quote, VenueError>;
}

/// `ExecOutcome`.
#[derive(Debug, Clone, PartialEq)]
pub struct ExecOutcome {
    /// `plan_id`.
    pub plan_id: String,
    /// `sleeve`.
    pub sleeve: String,
    /// `outcome`.
    pub outcome: String,
    /// `simulated`.
    pub simulated: bool,
    /// `session_state`.
    pub session_state: String,
    /// `orders`.
    pub orders: i64,
    /// `entry_points`.
    pub entry_points: Option<String>,
    /// `detail`.
    pub detail: PyDict,
}

/// `outcome_json(outcome)` = `dataclasses.asdict`.
#[must_use]
pub fn outcome_json(o: &ExecOutcome) -> PyValue {
    PyValue::Map(crate::wire::dict([
        ("plan_id", PyValue::Str(o.plan_id.clone())),
        ("sleeve", PyValue::Str(o.sleeve.clone())),
        ("outcome", PyValue::Str(o.outcome.clone())),
        ("simulated", PyValue::Bool(o.simulated)),
        ("session_state", PyValue::Str(o.session_state.clone())),
        ("orders", PyValue::Int(o.orders)),
        ("entry_points", py::vstr(o.entry_points.clone())),
        ("detail", PyValue::Map(o.detail.clone())),
    ]))
}

/// `client_id(plan_id, symbol, closing=, number=)`: `plan:SYM` for an entry's first attempt,
/// `plan:SYM:CLOSE` for a close's, `:R<n>` for every later attempt (OP10.4).
#[must_use]
pub fn client_id(plan_id: &str, symbol: &str, closing: bool, number: i64) -> String {
    let base = if closing {
        format!("{plan_id}:{symbol}:CLOSE")
    } else {
        format!("{plan_id}:{symbol}")
    };
    if number == 1 {
        base
    } else {
        format!("{base}:R{number}")
    }
}

/// `float(Decimal)`.
#[must_use]
pub fn to_f64(d: Decimal) -> f64 {
    d.to_string().parse().unwrap_or(0.0)
}

/// The executor's clock: Python's `clock` callable (`now or (lambda: dt.datetime.now(tz=IST))`),
/// read afresh at each step it is read in Python — the plan's validation, the confirm, each
/// fill, the open and the close (DECISIONS-RS R8.20.1).
pub type Now<'a> = &'a (dyn Fn() -> DateTime<FixedOffset> + Sync);

/// `DeskVenue`: the executor's venue over the real gateway.
pub struct DeskVenue<'s, 'c> {
    gateway: Arc<dyn Gateway>,
    store: &'s mut PgOptionsStore<'c>,
    plan: PlanRow,
    quotes: &'s dyn QuoteSource,
    now: Now<'s>,
    execution: ExecutionConfig,
    close_counts: HashMap<i64, i64>,
    /// Orders sent.
    pub orders: i64,
}

impl DeskVenue<'_, '_> {
    fn leg(&self, role: LegRole) -> Result<LegRow, VenueError> {
        self.plan
            .legs
            .iter()
            .find(|l| l.role == role)
            .cloned()
            .ok_or_else(|| VenueError::Other(format!("KeyError: <LegRole.{}>", role_str(role))))
    }
}

fn store_err(e: StoreError) -> VenueError {
    VenueError::Other(e.py_text())
}

#[async_trait::async_trait]
impl Venue for DeskVenue<'_, '_> {
    async fn quote(&mut self, role: LegRole) -> Result<Book, VenueError> {
        let leg = self.leg(role)?;
        let q = self.quotes.quote(leg.instrument_token).await?;
        Ok(Book {
            bid: q.bid,
            ask: q.ask,
        })
    }

    async fn send(
        &mut self,
        role: LegRole,
        attempt: &Attempt,
        quantity: i64,
        closing: bool,
    ) -> Result<Fill, VenueError> {
        let leg = self.leg(role)?;
        let mut number = attempt.number;
        if closing {
            let known = self.close_counts.get(&leg.id).copied();
            let base = match known {
                Some(n) => n,
                None => self.store.close_orders(leg.id).await.map_err(store_err)?,
            };
            number = base + 1;
            self.close_counts.insert(leg.id, number);
        }
        let cid = client_id(&self.plan.plan_id, &leg.tradingsymbol, closing, number);
        let book = self.quotes.quote(leg.instrument_token).await?;
        let reference = if attempt.side == Side::Buy {
            book.ask
        } else {
            book.bid
        };
        let order = PlaceOrder {
            symbol: leg.tradingsymbol.clone(),
            qty: quantity,
            side: super::core::side_str(attempt.side).to_owned(),
            product: "MIS".to_owned(),
            order_type: "LIMIT".to_owned(),
            price: Some(to_f64(attempt.price)),
            exchange: "NFO".to_owned(),
            client_id: Some(cid.clone()),
            gross_exposure: Some(to_f64(attempt.price) * quantity as f64),
            reference_price: Some(to_f64(reference)),
            ..PlaceOrder::default()
        };
        let result = self
            .gateway
            .place(order)
            .await
            .map_err(|e| VenueError::Other(e.to_string()))?;
        let status = result
            .get("status")
            .map_or_else(|| "None".to_owned(), PyValue::py_str);
        let order_id = self
            .store
            .write_order(
                &leg,
                &cid,
                attempt.side,
                quantity,
                attempt.price,
                &result,
                status == DRY_RUN,
            )
            .await
            .map_err(store_err)?;
        self.orders += 1;
        if status != DRY_RUN {
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
        if sim.filled != 0 {
            self.store
                .write_fill(
                    &leg,
                    order_id,
                    (self.now)(),
                    sim.filled,
                    sim.avg_price,
                    sim.requested,
                    sim.levels_consumed,
                    attempt.price,
                )
                .await
                .map_err(store_err)?;
        }
        Ok(Fill {
            quantity: sim.filled,
            avg_price: sim.avg_price,
        })
    }
}

/// `entry_points(structure, result)`: the condor's credit, the long's price, the spread's
/// debit, from fills.
#[must_use]
pub fn entry_points(structure: Structure, result: &ExecResult) -> Decimal {
    let mut total = Decimal::ZERO;
    for (role, _) in &result.fills {
        let price = result.avg_price(*role).unwrap_or(Decimal::ZERO);
        let signed = if structure == Structure::IronCondor {
            if role.is_long() { -price } else { price }
        } else if role.is_long() {
            price
        } else {
            -price
        };
        total += signed;
    }
    py::q2(total)
}

fn validate(
    plan: Option<PlanRow>,
    confirm: bool,
    now: DateTime<FixedOffset>,
) -> Result<PlanRow, Refused> {
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
    if plan.kind != "ENTRY" {
        return Err(Refused::new(
            400,
            "NOT_AN_ENTRY",
            "only an entry plan is confirmed",
        ));
    }
    if plan.status != "ISSUED" || plan.session_state != "PLANNED" {
        return Err(Refused::new(
            409,
            "NOT_ISSUED",
            format!(
                "the plan is {}, its session {}",
                plan.status, plan.session_state
            ),
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

fn left_open(result: &ExecResult) -> PyDict {
    let mut out = PyDict::new();
    for (role, q) in &result.position {
        if *q != 0 {
            out.insert(role_str(*role), PyValue::Int(*q));
        }
    }
    out
}

/// `execute_entry(store, gateway, quotes=, plan_id=, confirm=, mode_of=, now=)`.
///
/// The gateway is resolved by the caller before this runs, as Python evaluates
/// `gateway_for(sleeve)` as an argument: no gateway is the caller's 500, before any read here
/// (R8.20.1).
///
/// # Errors
/// A refusal, or an exception.
pub async fn execute_entry(
    store: &mut PgOptionsStore<'_>,
    gateway: Arc<dyn Gateway>,
    quotes: &dyn QuoteSource,
    plan_id: &str,
    confirm: bool,
    mode_of: &(dyn Fn(Sleeve) -> Mode + Sync),
    now: Now<'_>,
) -> Result<ExecOutcome, ExecError> {
    let found = store.plan(plan_id).await?;
    let plan = validate(found, confirm, now())?;
    if mode_of(plan.sleeve) == Mode::Live {
        return Err(Refused::new(
            409,
            "LIVE_NOT_BUILT",
            "live execution is not built yet (OP10.3); nothing sent",
        )
        .into());
    }
    let holder = store.slot_holder(plan.trade_date).await?;
    let slot = super::core::slot_after_confirm(plan.sleeve, holder)
        .map_err(|m| Refused::new(409, "SLOT_TAKEN", m))?;
    let confirmed_at = now();
    store.confirm(&plan, confirmed_at, slot).await?;
    let quantity = plan.lots * plan.lot_size;
    let roles: Vec<LegRole> = plan.legs.iter().map(|l| l.role).collect();
    let mut venue = DeskVenue {
        gateway,
        store,
        plan: plan.clone(),
        quotes,
        now,
        execution: ExecutionConfig::default(),
        close_counts: HashMap::new(),
        orders: 0,
    };
    let result = run_entry(
        &mut venue,
        &roles,
        quantity,
        plan.sleeve,
        tick(),
        &ExecutionConfig::default(),
    )
    .await?;
    let orders = venue.orders;
    store.settle_legs(&plan, &result).await?;
    if result.outcome == Outcome::Open {
        let points = entry_points(plan.structure, &result);
        let hard_exit = py::ist_at(plan.trade_date, super::core::hard_exit_time(plan.sleeve))
            .ok_or_else(|| ExecError::Other("ValueError: no hard exit".to_owned()))?;
        store.open_position(&plan, points, now(), hard_exit).await?;
        let mut detail = PyDict::new();
        detail.insert(
            "events",
            PyValue::Int(i64::try_from(result.events.len()).unwrap_or(i64::MAX)),
        );
        return Ok(ExecOutcome {
            plan_id: plan.plan_id.clone(),
            sleeve: sleeve_str(plan.sleeve).to_owned(),
            outcome: "OPEN".to_owned(),
            simulated: true,
            session_state: "OPEN".to_owned(),
            orders,
            entry_points: Some(py::dstr(points)),
            detail,
        });
    }
    // Python's `store.abandon(plan, clock())`: the time is read and not written.
    let _abandoned_at = now();
    store.abandon(&plan).await?;
    let mut detail = PyDict::new();
    detail.insert("left_open", PyValue::Map(left_open(&result)));
    Ok(ExecOutcome {
        plan_id: plan.plan_id.clone(),
        sleeve: sleeve_str(plan.sleeve).to_owned(),
        outcome: Outcome::AbandonedEntry.as_str().to_owned(),
        simulated: true,
        session_state: "CLOSED".to_owned(),
        orders,
        entry_points: None,
        detail,
    })
}

/// The exception when no per-sleeve gateway can be derived (`gateway_for` raising): fail
/// closed, nothing read or sent after it.
#[must_use]
pub fn no_gateway() -> ExecError {
    ExecError::Other(
        "RuntimeError: the options gateway could not be built (Gateway::derive); nothing was sent"
            .to_owned(),
    )
}

/// `execute_exit(store, gateway, quotes=, plan=, exit_plan_id=, reason=, mode=)`: shorts
/// first; `FLAT` closes the position and runs the ledger, `PARTIAL_EXIT` leaves it open.
///
/// # Errors
/// A refusal, or an exception.
#[allow(clippy::too_many_arguments)]
pub async fn execute_exit(
    store: &mut PgOptionsStore<'_>,
    gateway: Arc<dyn Gateway>,
    quotes: &dyn QuoteSource,
    plan: &PlanRow,
    exit_plan_id: Option<&str>,
    reason: &str,
    mode: Mode,
    now: Now<'_>,
) -> Result<ExecOutcome, ExecError> {
    let position = store.net_open(plan.session_id).await?;
    let mut venue = DeskVenue {
        gateway,
        store,
        plan: plan.clone(),
        quotes,
        now,
        execution: ExecutionConfig::default(),
        close_counts: HashMap::new(),
        orders: 0,
    };
    let result = run_exit(
        &mut venue,
        position,
        plan.sleeve,
        tick(),
        &ExecutionConfig::default(),
    )
    .await?;
    let orders = venue.orders;
    let mut applied: Vec<String> = Vec::new();
    if result.outcome == Outcome::Flat {
        let closed_at = now();
        store
            .close_position(plan.session_id, exit_plan_id, closed_at, reason)
            .await?;
        applied =
            super::ledger::after_close(store, plan.session_id, plan.sleeve, reason, closed_at, mode)
                .await
                .map_err(|e| ExecError::Other(e.py_text()))?;
    }
    let mut detail = PyDict::new();
    detail.insert("reason", PyValue::Str(reason.to_owned()));
    detail.insert(
        "pauses",
        PyValue::List(applied.into_iter().map(PyValue::Str).collect()),
    );
    Ok(ExecOutcome {
        plan_id: plan.plan_id.clone(),
        sleeve: sleeve_str(plan.sleeve).to_owned(),
        outcome: result.outcome.as_str().to_owned(),
        simulated: true,
        session_state: if result.outcome == Outcome::Flat {
            "CLOSED"
        } else {
            "OPEN"
        }
        .to_owned(),
        orders,
        entry_points: None,
        detail,
    })
}

/// `run_pending_exits(store, gateway_for, quotes=, mode_of=)`: every open position the
/// monitor raised an exit for, closed under its entry's confirm (PACK.2).
///
/// # Errors
/// An exception (a refusal waits for the next pass).
pub async fn run_pending_exits(
    store: &mut PgOptionsStore<'_>,
    gateway_for: &(dyn Fn(Sleeve) -> Option<Arc<dyn Gateway>> + Sync),
    quotes: &dyn QuoteSource,
    mode_of: &(dyn Fn(Sleeve) -> Mode + Sync),
    now: Now<'_>,
) -> Result<Vec<ExecOutcome>, ExecError> {
    let sql = format!(
        "SELECT p.session_id, p.exit_plan_id, x.detail FROM {} p \
         JOIN {} x ON x.plan_id = p.exit_plan_id \
         WHERE p.user_id = ? AND p.closed_at IS NULL ORDER BY p.session_id",
        store.t("op_position"),
        store.t("op_plan")
    );
    let rows = store
        .conn
        .execute(&sql, &[crate::storage::Param::Int(store.user_id)])
        .await
        .map_err(|e| ExecError::Other(StoreError::from(e).py_text()))?
        .rows;
    let mut done = Vec::new();
    for row in &rows {
        let session = py::int(py::col(row, "session_id"));
        let Some(plan) = store.entry_plan_for(session).await? else {
            continue;
        };
        let detail = match py::col(row, "detail") {
            crate::storage::DbValue::Json(s) => match PyValue::from_json(s.as_bytes()) {
                Ok(PyValue::Map(m)) => m,
                _ => PyDict::new(),
            },
            _ => PyDict::new(),
        };
        let reason = match detail.get("code") {
            Some(v) if v.truthy() => v.py_str(),
            _ => "EXIT".to_owned(),
        };
        let exit_id = py::text(py::col(row, "exit_plan_id"));
        // `gateway_for(plan.sleeve)` is an argument: its exception leaves the sweep before
        // the position is read (R8.20.1).
        let gateway = gateway_for(plan.sleeve).ok_or_else(no_gateway)?;
        match execute_exit(
            store,
            gateway,
            quotes,
            &plan,
            Some(&exit_id),
            &reason,
            mode_of(plan.sleeve),
            now,
        )
        .await
        {
            Ok(o) => done.push(o),
            Err(ExecError::Refused(_)) => {}
            Err(e) => return Err(e),
        }
    }
    Ok(done)
}

// --- the gateway and the book on the desk -------------------------------------------------------

/// `options_journal_path()`: each sleeve's journal beside the desk's own.
#[must_use]
pub fn options_journal_path(state: &AppState) -> PathBuf {
    state
        .paths
        .resolve(crate::gateway::JOURNAL)
        .with_file_name("options_orders_journal.jsonl")
}

/// One desk's `_GATEWAYS`: the desk it belongs to (its gateway, held weakly), the sleeve, the
/// sleeve's gateway.
type GatewayCache = Mutex<Vec<(Weak<dyn Gateway>, &'static str, Arc<dyn Gateway>)>>;

fn cache() -> &'static GatewayCache {
    static CACHE: OnceLock<GatewayCache> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(Vec::new()))
}

fn same_desk(entry: &Weak<dyn Gateway>, desk: &Arc<dyn Gateway>) -> bool {
    entry
        .upgrade()
        .is_some_and(|g| std::ptr::addr_eq(Arc::as_ptr(&g), Arc::as_ptr(desk)))
}

/// `options_gateway(sleeve, kc, risk)`: one gateway per sleeve whose gates are that sleeve's,
/// read per order, sharing the desk's risk manager (OP10.2). `None` when the desk's gateway
/// cannot derive one (fail closed).
///
/// Python's `_GATEWAYS` is keyed by sleeve in a module, and one process holds one desk. Here
/// the cache is keyed by the desk (its gateway, `state.gateway`) and the sleeve, so a second
/// `AppState` in one process — a test — never gets a gateway built over another desk's Kite
/// and risk manager; with one desk it is Python's map exactly (R8.20.1).
#[must_use]
pub fn options_gateway(state: &AppState, sleeve: Sleeve) -> Option<Arc<dyn Gateway>> {
    let journal = options_journal_path(state);
    let name = sleeve_str(sleeve);
    if let Ok(mut found) = cache().lock() {
        found.retain(|(desk, _, _)| desk.strong_count() > 0);
        if let Some((_, _, g)) = found
            .iter()
            .find(|(desk, s, _)| *s == name && same_desk(desk, &state.gateway))
        {
            return Some(g.clone());
        }
    }
    let settings = state.settings.clone();
    let gates: crate::gateway::GatesFn = Arc::new(move || {
        let s = match settings.read() {
            Ok(s) => s.clone(),
            Err(p) => p.into_inner().clone(),
        };
        super::gates::product_gates(&s, sleeve)
    });
    let built = state.gateway.derive(DerivedGateway {
        gates,
        journal_path: journal,
        fo_coverage: false,
    })?;
    if let Ok(mut c) = cache().lock() {
        c.push((Arc::downgrade(&state.gateway), name, built.clone()));
    }
    Some(built)
}

/// `gateway_for(sleeve)` as the desk's routes call it: Python goes through `app.main.gateway()`,
/// which builds the process's shared risk manager as a side effect (on Postgres: the day's
/// `risk_ledger` row) before the sleeve's gateway is derived; the same happens here through the
/// exec_core lane's shared risk manager. The sleeve's gateway itself is [`options_gateway`].
pub async fn options_gateway_ready(state: &AppState, sleeve: Sleeve) -> Option<Arc<dyn Gateway>> {
    if let Some(desk) = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
    {
        let _ = desk.risk().await;
    }
    options_gateway(state, sleeve)
}

/// `kite_quotes(kite)`: a leg's book from `quote_raw([str(token)])`, one call per leg.
pub struct KiteQuotes {
    /// The desk's reads.
    pub kite: Arc<dyn KiteReads>,
}

fn levels(raw: Option<&PyValue>) -> Vec<Level> {
    let Some(PyValue::List(items)) = raw else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for item in items {
        let PyValue::Map(m) = item else { continue };
        let price = match m.get("price") {
            Some(v) if v.truthy() => py::dec_py(Some(v)).unwrap_or(Decimal::ZERO),
            _ => Decimal::ZERO,
        };
        let quantity = py::py_int(m.get("quantity"), 0);
        if price > Decimal::ZERO && quantity > 0 {
            out.push(Level { price, quantity });
        }
    }
    out
}

/// `_levels(raw)` (shared with the FO book's reader).
#[must_use]
pub fn depth_levels(raw: Option<&PyValue>) -> Vec<Level> {
    levels(raw)
}

#[async_trait::async_trait]
impl QuoteSource for KiteQuotes {
    async fn quote(&self, token: i64) -> Result<Quote, VenueError> {
        let payload = self
            .kite
            .quote(&[token.to_string()])
            .await
            .map_err(|e| VenueError::Other(e.to_string()))?;
        let quote = match &payload {
            PyValue::Map(m) => m.iter().next().map(|(_, v)| v.clone()),
            _ => None,
        }
        .unwrap_or(PyValue::Map(PyDict::new()));
        let depth = match &quote {
            PyValue::Map(m) => match m.get("depth") {
                Some(PyValue::Map(d)) => d.clone(),
                _ => PyDict::new(),
            },
            _ => PyDict::new(),
        };
        let bids = levels(depth.get("buy"));
        let asks = levels(depth.get("sell"));
        match (bids.first(), asks.first()) {
            (Some(b), Some(a)) => Ok(Quote {
                bid: b.price,
                ask: a.price,
                bids: bids.clone(),
                asks: asks.clone(),
            }),
            _ => Err(VenueError::Refused {
                status: 503,
                code: "NO_QUOTE".to_owned(),
                message: format!("no two-sided quote for {token}"),
            }),
        }
    }
}
