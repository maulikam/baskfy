//! The order path's interface (DECISIONS-RS R8.0, R8.3): `baskfy_execution.gateway.OrderGateway`
//! as the desk calls it through `app/core/gateway.py`, plus the small pure pieces every lane
//! needs **now** and the non-negotiables pin: `client_id = plan_id:symbol`
//! ([`mint_client_id`]), law 2's tenant clause ([`TenantIds`], [`refuse_cross_tenant`]), the
//! product switches ([`ProductGates`], fail-closed defaults), the outcome vocabulary
//! ([`FAILED_STATUSES`], the GTT statuses) and the stop band ([`StopBand`]).
//!
//! **The gateway itself is the `exec_core` lane's** (`lanes/exec_core.rs`): guards → risk →
//! rate limit → journal → broker, ported test-first from `packages/execution/tests` and the
//! desk's `test_execute_gateway.py`/`test_seven_non_negotiables.py`, DRY_RUN-only, and never
//! cut over without Maulik's explicit go after two shadow Fridays (R8.0). Every lane codes
//! against [`Gateway`]; none holds a [`crate::kite::KiteWrites`].
//!
//! The documented GTT exception of non-negotiable 6 is history in this code: since M16 the
//! gateway *has* the GTT methods ([`Gateway::place_gtt_stop`], [`Gateway::delete_gtt`],
//! [`Gateway::modify_gtt_quantity`]); `kite_client.place_gtt_stop` survives in Python only for
//! the weekly `/stops/arm` path, which the weekly lane ports onto the gateway's method or keeps
//! as documented — its call, recorded in DECISIONS-RS.

use async_trait::async_trait;

use crate::wire::{PyDict, PyValue};

/// `client_ids.CLIENT_ID_SEPARATOR`.
pub const CLIENT_ID_SEPARATOR: char = ':';

/// Why a client id cannot be minted (`ValueError` in Python).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("ValueError: {0}")]
pub struct ClientIdError(pub String);

fn reject(part: &str, value: &str) -> Result<(), ClientIdError> {
    if value.is_empty() {
        return Err(ClientIdError(format!(
            "{part} cannot be empty: a client id needs both halves"
        )));
    }
    let repr = baskfy_providers::py::repr_str(value);
    if value.contains(CLIENT_ID_SEPARATOR) {
        return Err(ClientIdError(format!(
            "{part} cannot contain ':' ({repr}): the client id would be ambiguous, and two \
             different orders could mint the same key"
        )));
    }
    let py_space = |c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c);
    if value.chars().any(py_space) {
        let stripped = baskfy_providers::py::repr_str(value.trim_matches(py_space));
        return Err(ClientIdError(format!(
            "{part} cannot contain whitespace ({repr}): {stripped} and {repr} would be two keys \
             for one order, which is how a re-posted plan double-sends"
        )));
    }
    Ok(())
}

/// Ports `baskfy_execution.client_ids.mint_client_id`: `plan_id:SYMBOL` (symbol upper-cased),
/// refusing an empty half, a `:` and whitespace. Deterministic, so a re-posted plan mints the
/// same key and the gateway answers `DUPLICATE` (non-negotiable 6).
///
/// # Errors
/// As Python's `ValueError`s.
pub fn mint_client_id(plan_id: &str, symbol: &str) -> Result<String, ClientIdError> {
    reject("plan_id", plan_id)?;
    reject("symbol", symbol)?;
    Ok(format!(
        "{plan_id}{CLIENT_ID_SEPARATOR}{}",
        symbol.to_uppercase()
    ))
}

/// Ports `baskfy_execution.client_ids.parse_client_id`: `(plan_id, symbol)`.
///
/// # Errors
/// An id that is not exactly two valid halves.
pub fn parse_client_id(client_id: &str) -> Result<(String, String), ClientIdError> {
    let parts: Vec<&str> = client_id.split(CLIENT_ID_SEPARATOR).collect();
    let [plan_id, symbol] = parts.as_slice() else {
        return Err(ClientIdError(format!(
            "{} is not a minted client id: expected exactly one ':' between plan_id and symbol",
            baskfy_providers::py::repr_str(client_id)
        )));
    };
    reject("plan_id", plan_id)?;
    reject("symbol", symbol)?;
    Ok(((*plan_id).to_owned(), (*symbol).to_owned()))
}

/// Ports `baskfy_execution.tenancy.TenantIds`: the two ids law 2 requires on every order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TenantIds {
    user_id: i64,
    broker_account_id: i64,
}

impl TenantIds {
    /// Both must be positive (`ValueError("tenant ids must be positive")`).
    ///
    /// # Errors
    /// A non-positive id.
    pub fn new(user_id: i64, broker_account_id: i64) -> Result<Self, ClientIdError> {
        if user_id < 1 || broker_account_id < 1 {
            return Err(ClientIdError("tenant ids must be positive".to_owned()));
        }
        Ok(Self {
            user_id,
            broker_account_id,
        })
    }

    /// `user_id`.
    #[must_use]
    pub const fn user_id(&self) -> i64 {
        self.user_id
    }

    /// `broker_account_id`.
    #[must_use]
    pub const fn broker_account_id(&self) -> i64 {
        self.broker_account_id
    }
}

/// Ports `baskfy_execution.tenancy.refuse_cross_tenant`: `None` when the pair matches, else
/// the refusal the gateway returns as `BLOCKED`.
#[must_use]
pub fn refuse_cross_tenant(caller: TenantIds, plan: TenantIds) -> Option<String> {
    if caller == plan {
        return None;
    }
    Some(format!(
        "tenant mismatch: caller ({},{}) != plan ({},{})",
        caller.user_id, caller.broker_account_id, plan.user_id, plan.broker_account_id
    ))
}

/// Ports `baskfy_execution.gateway.ProductGates`. **Fail closed**: the default simulates and
/// refuses MIS and F&O (non-negotiable 5). The desk's gateway reads these off the settings at
/// the moment of each order (`app/core/gateway.py::_gates_from_config`), never at
/// construction.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProductGates {
    /// `DRY_RUN`.
    pub dry_run: bool,
    /// `INTRADAY_ENABLED` (MIS).
    pub intraday_enabled: bool,
    /// `OPTIONS_ENABLED` (NFO/BFO).
    pub options_enabled: bool,
    /// `BASKFY_FNO_CARRY_ENABLED` (NRML on NFO for an order carrying a `fo_plan`).
    pub fno_carry_enabled: bool,
}

impl Default for ProductGates {
    fn default() -> Self {
        Self {
            dry_run: true,
            intraday_enabled: false,
            options_enabled: false,
            fno_carry_enabled: false,
        }
    }
}

impl ProductGates {
    /// `_gates_from_config()`: the desk's three switches (the carry switch is the FO book's own
    /// gateway's, `app.fno_execute.fo_gateway`).
    #[must_use]
    pub const fn from_settings(s: &crate::settings::Settings) -> Self {
        Self {
            dry_run: s.dry_run,
            intraday_enabled: s.intraday_enabled,
            options_enabled: s.options_enabled,
            fno_carry_enabled: false,
        }
    }
}

/// `gateway.FAILED_STATUSES`: the order did not reach the exchange, and says why. `/execute`'s
/// circuit breaker counts these; `DUPLICATE` and `DRY_RUN` are deliberately absent.
pub const FAILED_STATUSES: [&str; 4] = ["ERROR", "REJECTED", "BLOCKED", "RISK_BLOCKED"];

/// `gateway._DEFINITIVE_REFUSALS`: Kite exceptions decided before the exchange saw anything.
pub const DEFINITIVE_REFUSALS: [&str; 4] = [
    "InputException",
    "OrderException",
    "PermissionException",
    "TokenException",
];

/// `gateway.JOURNAL`: the order journal, relative to the desk's working directory.
pub const JOURNAL: &str = "data/outputs/orders_journal.jsonl";

/// `gtt.GTT_LIMIT_FRACTION`: the weekly book's GTT limit cushion.
pub const GTT_LIMIT_FRACTION: f64 = 0.995;

/// `gtt.DEFAULT_TICK`.
pub const DEFAULT_TICK: f64 = 0.05;

/// The GTT outcome vocabulary (`baskfy_execution.gtt`).
pub mod gtt_status {
    /// A stop rested.
    pub const GTT_PLACED: &str = "GTT_PLACED";
    /// A stop simulated.
    pub const DRY_RUN_GTT: &str = "DRY_RUN_GTT";
    /// Arming failed.
    pub const GTT_ERROR: &str = "GTT_ERROR";
    /// A stop removed.
    pub const GTT_DELETED: &str = "GTT_DELETED";
    /// A removal simulated.
    pub const DRY_RUN_GTT_DELETE: &str = "DRY_RUN_GTT_DELETE";
    /// Removal failed.
    pub const GTT_DELETE_ERROR: &str = "GTT_DELETE_ERROR";
    /// A stop re-sized.
    pub const GTT_MODIFIED: &str = "GTT_MODIFIED";
    /// A re-size simulated.
    pub const DRY_RUN_GTT_MODIFY: &str = "DRY_RUN_GTT_MODIFY";
    /// Re-size failed.
    pub const GTT_MODIFY_ERROR: &str = "GTT_MODIFY_ERROR";
    /// An order cancelled.
    pub const ORDER_CANCELLED: &str = "ORDER_CANCELLED";
    /// A cancel simulated.
    pub const ORDER_CANCEL_DRY_RUN: &str = "ORDER_CANCEL_DRY_RUN";
    /// Cancel failed.
    pub const ORDER_CANCEL_ERROR: &str = "ORDER_CANCEL_ERROR";
}

/// Ports `baskfy_execution.gtt.StopBand`: how far below the last price a stop may rest. The
/// default is the weekly book's 8–12% (`stop_from_vol`'s clamp, non-negotiable 4); the swing
/// and VBT books pass their own. A deviation is journalled, not refused.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct StopBand {
    min_pct: f64,
    max_pct: f64,
}

impl Default for StopBand {
    fn default() -> Self {
        Self {
            min_pct: 0.08,
            max_pct: 0.12,
        }
    }
}

impl StopBand {
    /// `StopBand(min_pct, max_pct)`.
    ///
    /// # Errors
    /// Unless `0 < min < max < 1` (Python's `ValueError`).
    pub fn new(min_pct: f64, max_pct: f64) -> Result<Self, ClientIdError> {
        if !(0.0 < min_pct && min_pct < max_pct && max_pct < 1.0) {
            return Err(ClientIdError(format!(
                "stop band is inverted or out of range: {} .. {}",
                baskfy_core::py::float_repr(min_pct),
                baskfy_core::py::float_repr(max_pct)
            )));
        }
        Ok(Self { min_pct, max_pct })
    }

    /// `min_pct`.
    #[must_use]
    pub const fn min_pct(&self) -> f64 {
        self.min_pct
    }

    /// `max_pct`.
    #[must_use]
    pub const fn max_pct(&self) -> f64 {
        self.max_pct
    }
}

/// `guards.FoPlanRef`: the reference an FO order carries. The signed legs stay the fno lane's
/// (they are `baskfy_core::fno` types); here they travel as Python values.
#[derive(Debug, Clone, PartialEq)]
pub struct FoPlanRef {
    /// `plan_id`.
    pub plan_id: String,
    /// `sleeve` (`F1N`, `F2`, …).
    pub sleeve: String,
    /// `step`.
    pub step: Option<PyValue>,
    /// `broker_positions`.
    pub broker_positions: Vec<PyValue>,
    /// `plan_filled`.
    pub plan_filled: Vec<PyValue>,
}

/// `OrderGateway.place(**kwargs)`'s keyword arguments, with Python's defaults in [`Default`].
#[derive(Debug, Clone, PartialEq)]
pub struct PlaceOrder {
    /// `symbol`.
    pub symbol: String,
    /// `qty`.
    pub qty: i64,
    /// `side` (`BUY`/`SELL`).
    pub side: String,
    /// `product` (`"CNC"`).
    pub product: String,
    /// `order_type` (`"LIMIT"`).
    pub order_type: String,
    /// `price`.
    pub price: Option<f64>,
    /// `exchange` (`"NSE"`).
    pub exchange: String,
    /// `variety` (`"regular"`).
    pub variety: String,
    /// `client_id` (`plan_id:symbol`; `None` → a fresh `uuid4().hex[:10]`).
    pub client_id: Option<String>,
    /// `gross_exposure` (required by the core gateway; the desk shim fills `|price × qty|`).
    pub gross_exposure: Option<f64>,
    /// `series`.
    pub series: Option<String>,
    /// `tick_size`.
    pub tick_size: Option<f64>,
    /// `market_protection` (MARKET orders only).
    pub market_protection: Option<f64>,
    /// `reference_price` (what a MARKET order is worth to the risk check).
    pub reference_price: Option<f64>,
    /// `tenant` (`None` → the desk's sole tenant, as the shim stamps it).
    pub tenant: Option<TenantIds>,
    /// `plan_tenant` (`None` → the sole tenant).
    pub plan_tenant: Option<TenantIds>,
    /// `fo_plan`.
    pub fo_plan: Option<FoPlanRef>,
}

impl Default for PlaceOrder {
    fn default() -> Self {
        Self {
            symbol: String::new(),
            qty: 0,
            side: String::new(),
            product: "CNC".to_owned(),
            order_type: "LIMIT".to_owned(),
            price: None,
            exchange: "NSE".to_owned(),
            variety: "regular".to_owned(),
            client_id: None,
            gross_exposure: None,
            series: None,
            tick_size: None,
            market_protection: None,
            reference_price: None,
            tenant: None,
            plan_tenant: None,
            fo_plan: None,
        }
    }
}

/// `OrderGateway.place_gtt_stop(**kwargs)`.
#[derive(Debug, Clone, PartialEq)]
pub struct GttStop {
    /// `symbol`.
    pub symbol: String,
    /// `qty`.
    pub qty: i64,
    /// `trigger` (from `stop_from_vol`; the gateway never recomputes it).
    pub trigger: f64,
    /// `last_price`.
    pub last_price: f64,
    /// `exchange`.
    pub exchange: String,
    /// `series`.
    pub series: Option<String>,
    /// `client_id`.
    pub client_id: Option<String>,
    /// `tenant`.
    pub tenant: Option<TenantIds>,
    /// `plan_tenant`.
    pub plan_tenant: Option<TenantIds>,
    /// `limit_fraction` (`None` → [`GTT_LIMIT_FRACTION`]).
    pub limit_fraction: Option<f64>,
    /// `fo_plan`.
    pub fo_plan: Option<FoPlanRef>,
}

/// `OrderGateway.delete_gtt(**kwargs)`.
#[derive(Debug, Clone, PartialEq)]
pub struct DeleteGtt {
    /// `gtt_id`.
    pub gtt_id: i64,
    /// `symbol` (required: the untouchable guard needs it).
    pub symbol: String,
    /// `exchange`.
    pub exchange: String,
    /// `series`.
    pub series: Option<String>,
    /// `client_id`.
    pub client_id: Option<String>,
    /// `tenant`.
    pub tenant: Option<TenantIds>,
    /// `plan_tenant`.
    pub plan_tenant: Option<TenantIds>,
}

/// `OrderGateway.modify_gtt_quantity(**kwargs)`.
#[derive(Debug, Clone, PartialEq)]
pub struct ModifyGtt {
    /// `gtt_id`.
    pub gtt_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `qty`.
    pub qty: i64,
    /// `trigger`.
    pub trigger: f64,
    /// `last_price`.
    pub last_price: f64,
    /// `exchange`.
    pub exchange: String,
    /// `series`.
    pub series: Option<String>,
    /// `client_id`.
    pub client_id: Option<String>,
    /// `tenant`.
    pub tenant: Option<TenantIds>,
    /// `plan_tenant`.
    pub plan_tenant: Option<TenantIds>,
    /// `limit_fraction`.
    pub limit_fraction: Option<f64>,
    /// `fo_plan`.
    pub fo_plan: Option<FoPlanRef>,
    /// `floor_trigger` (an F2 trail is never lowered).
    pub floor_trigger: Option<f64>,
}

/// `OrderGateway.cancel_order(**kwargs)`.
#[derive(Debug, Clone, PartialEq)]
pub struct CancelOrder {
    /// `order_id`.
    pub order_id: String,
    /// `symbol`.
    pub symbol: String,
    /// `variety`.
    pub variety: String,
    /// `series`.
    pub series: Option<String>,
    /// `client_id`.
    pub client_id: Option<String>,
    /// `tenant`.
    pub tenant: Option<TenantIds>,
    /// `plan_tenant`.
    pub plan_tenant: Option<TenantIds>,
}

/// What the gateway raises instead of returning a result dict (a caller bug, e.g. a
/// `limit_fraction` outside `(0, 1]`, or an untouchable symbol on a path that raises).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{python_type}: {message}")]
pub struct GatewayError {
    /// The Python exception type (`ValueError`, `UntouchableInstrumentError`).
    pub python_type: String,
    /// Its message.
    pub message: String,
}

/// The one path to an order (law 2). Implemented by `lanes::exec_core`; each method returns
/// the result dict Python's returns (`{"symbol", "status", ...}`), in Python's key order.
#[async_trait]
pub trait Gateway: Send + Sync {
    /// `place(...)`: tenant → untouchables → overnight-option / FO cover → product gates →
    /// risk → idempotency (`client_id`) → rate limit → journal → broker (or `DRY_RUN`).
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError>;

    /// `place_gtt_stop(...)`: the ONLY way to create a GTT.
    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError>;

    /// `delete_gtt(...)`: the ONLY way to destroy a GTT.
    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError>;

    /// `modify_gtt_quantity(...)`: the ONLY way to modify a GTT.
    async fn modify_gtt_quantity(&self, req: ModifyGtt) -> Result<PyDict, GatewayError>;

    /// `cancel_order(...)`: the ONLY way to cancel an order.
    async fn cancel_order(&self, req: CancelOrder) -> Result<PyDict, GatewayError>;

    /// A sibling gateway over the same broker and the same risk manager (one account, one
    /// daily-loss cap, one order counter) but with its own product gates, journal and
    /// covered-overnight rule — Python's `OrderGateway(kc, risk, gates=..., journal_path=...,
    /// coverage=...)` as `app.options_execute.options_gateway`, `app.fno_execute.fo_gateway`
    /// (and the vbt/twt books) build theirs. `None` (the default) means this gateway cannot
    /// derive one; the caller then refuses to send (fail closed). Added by the fno/options
    /// lane; the exec_core lane implements it.
    fn derive(&self, spec: DerivedGateway) -> Option<std::sync::Arc<dyn Gateway>> {
        let _ = spec;
        None
    }

    /// A sleeve's own gateway with its own **stop band** as well (`build_twt_gateway`,
    /// `build_vbt_gateway`: `OrderGateway(kc, risk, gates=..., journal_path=...,
    /// stop_band=...)`) — [`Gateway::derive`] plus the band the GTT check reads (TWT 0.5–30 %,
    /// VBT 0.5–15 %; the default 8–12 % would journal a deviation on every sleeve stop). `None`
    /// (the default) means this gateway cannot build one; the caller refuses to send (fail
    /// closed). Added by the twt-vbt lane; the exec_core lane implements it, and the caller
    /// builds it once per process (the idempotency map is per instance).
    fn derive_sleeve(&self, spec: SleeveGatewaySpec) -> Option<std::sync::Arc<dyn Gateway>> {
        let _ = spec;
        None
    }

    /// The exec_core lane's handles behind this gateway (the shared risk manager, the desk's
    /// Kite wrapper `app.kite_client.Kite`, a sibling with its own stop band), reached through
    /// `lanes::exec_core::handles(&state)`. `None` (the default) for any other gateway. Added
    /// by the exec_core lane.
    fn as_any(&self) -> Option<&(dyn std::any::Any + Send + Sync)> {
        None
    }
}

/// The product gates a derived gateway reads **at the moment of each order** (Python passes a
/// callable, never a value: a setting changed mid-session takes effect on the next order).
pub type GatesFn = std::sync::Arc<dyn Fn() -> ProductGates + Send + Sync>;

/// What [`Gateway::derive`] builds a sibling gateway from.
#[derive(Clone)]
pub struct DerivedGateway {
    /// `gates=`: read per order.
    pub gates: GatesFn,
    /// `journal_path=`: the sleeve's own journal beside `data/outputs/orders_journal.jsonl`
    /// (`options_orders_journal.jsonl`, `fno_orders_journal.jsonl`), resolved.
    pub journal_path: std::path::PathBuf,
    /// `coverage=baskfy_core.fno.covered.uncovered` when true (the FO book, FO6.1); `None`
    /// in Python when false, which refuses every order carrying a `fo_plan` (fail closed).
    pub fo_coverage: bool,
}

impl std::fmt::Debug for DerivedGateway {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DerivedGateway")
            .field("gates", &(self.gates)())
            .field("journal_path", &self.journal_path)
            .field("fo_coverage", &self.fo_coverage)
            .finish()
    }
}

/// What [`Gateway::derive_sleeve`] builds a sleeve's own gateway from: the sibling's gates and
/// journal ([`DerivedGateway`], `fo_coverage` false) and the sleeve's stop band. Added by the
/// twt-vbt lane.
#[derive(Clone, Debug)]
pub struct SleeveGatewaySpec {
    /// `gates=` and `journal_path=` (`twt_orders_journal.jsonl`, `vbt_orders_journal.jsonl`).
    pub derived: DerivedGateway,
    /// `stop_band=`.
    pub stop_band: StopBand,
}
