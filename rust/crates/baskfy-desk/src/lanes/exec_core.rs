//! Lane **exec_core** — the execution core — the one path to an order (law 2), ported test-first, DRY_RUN-only (R8.0).
//!
//! **STATUS: the gateway is ported (lane exec_gateway).** [`gateway`] builds
//! [`desk::DeskGateway`] — guards → risk → rate limits → idempotency → journal → broker
//! ([`order_gateway`]), DRY_RUN-only in this binary (R8.0, R8.3). Sibling lanes wire their own
//! modules into this file. The lane owns this file (and may add
//! private submodules under `src/lanes/exec_core/`); it never edits `lib.rs`, `app.rs` or
//! `lanes.rs`. A new dependency is a DECISIONS-RS entry the integrator applies.
//!
//! **Python it ports:**
//! * `decile-blueprint/packages/execution/src/baskfy_execution/{gateway,guards,risk,ratelimit,gtt,client_ids,tenancy,broker_ports,brokers,adapters}.py`
//! * `app/core/gateway.py (the desk shim: tenant stamp, gross_exposure fill, gates read per order)`
//! * `app/core/{guards,risk,ratelimit,kite_limits,risk_store,net}.py`
//!
//! **Tests to port first** (`kite-momentum-rebalancer/tests/`, test functions per file):
//! * `test_execute_gateway.py` (40)
//! * `test_guards.py` (20)
//! * `test_seven_non_negotiables.py` (12)
//! * `test_ratelimit.py` (7)
//! * `test_kite_limits.py` (39)
//! * `test_risk_store.py` (3)
//! * `test_outbound_ip.py` (6)
//!
//! and `decile-blueprint/packages/execution/tests/`:
//! * `test_af_lane_e.py` (11)
//! * `test_broker_adapters.py` (6)
//! * `test_broker_ports.py` (2)
//! * `test_client_id_journal.py` (21)
//! * `test_derivative_product_gate.py` (10)
//! * `test_fno_carry_gate.py` (13)
//! * `test_fno_covered_overnight.py` (16)
//! * `test_fno_gtt_trail_modify.py` (6)
//! * `test_gtt_gateway.py` (43)
//! * `test_gtt_limit_fraction.py` (6)
//! * `test_gtt_modify_and_cancel.py` (18)
//! * `test_non_negotiables.py` (17)
//! * `test_risk_store.py` (9)
//! * `test_stop_from_vol.py` (6)
//! * `test_tenant_isolation.py` (3)
//!
//! 314 test functions in all (before parametrisation).

pub mod desk;
pub mod exec_guards;
pub mod exec_kite;
pub mod exit_rules;
pub mod gtt;
pub mod guards;
pub mod journal;
pub mod kite_client;
pub mod kite_limits;
pub mod lifecycle;
pub mod net;
pub mod order_gateway;
pub mod ratelimit;
pub mod risk;
pub mod stops;
pub mod token_store;

use axum::Router;

use crate::app::AppState;

use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;

use crate::clock::Clock;
use crate::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
};
use crate::ids::IdSource;
use crate::kite::KiteWrites;
use crate::settings::SharedSettings;
use crate::storage::Db;
use crate::wire::PyDict;

/// The broker with its order verbs, as the gateway lane names it. Law 2 in the types (R8.3):
/// the alias is visible only inside `lanes::exec_core`, and `tests/non_negotiables.rs` refuses
/// the trait's name in any other source file, so no page or sleeve can hold one.
pub(in crate::lanes::exec_core) type Broker = dyn KiteWrites;

/// What the gateway is built from — the **only** place a [`KiteWrites`] handle goes.
pub struct GatewayParts {
    /// The broker, with its order verbs.
    pub kite: Arc<dyn KiteWrites>,
    /// The settings (product gates are read per order, as `_gates_from_config` does).
    pub settings: SharedSettings,
    /// The clock (risk-day roll, journal `ts`).
    pub clock: Arc<dyn Clock>,
    /// `uuid4` (a missing `client_id`).
    pub ids: Arc<dyn IdSource>,
    /// `data/outputs/orders_journal.jsonl`, resolved.
    pub journal_path: PathBuf,
    /// The risk ledger's store (`PgRiskStateStore` on Postgres).
    pub db: Db,
}

/// Build the desk's gateway ([`desk::DeskGateway`]: `app.core.gateway.OrderGateway` over
/// `baskfy_execution`'s). If it cannot be built (its journal cannot be created or replayed) the
/// answer is [`NotPortedGateway`], which refuses every call before touching anything — fail
/// closed, never a gateway without its idempotency memory.
#[must_use]
pub fn gateway(parts: GatewayParts) -> Arc<dyn Gateway> {
    let built = desk::DeskGateway::build(desk::DeskParts {
        kite: parts.kite,
        settings: parts.settings,
        clock: parts.clock,
        ids: parts.ids,
        journal_path: parts.journal_path,
        db: parts.db,
    });
    match built {
        Ok(g) => Arc::new(g),
        Err(_) => Arc::new(NotPortedGateway),
    }
}

/// The placeholder gateway: every method refuses with `NotPorted`, no broker call, no journal.
#[derive(Debug, Clone, Copy, Default)]
pub struct NotPortedGateway;

fn not_ported(what: &str) -> GatewayError {
    GatewayError {
        python_type: "NotPorted".to_owned(),
        message: format!(
            "the Rust gateway's {what} is not ported yet (lane exec_core); nothing was sent"
        ),
    }
}

#[async_trait]
impl Gateway for NotPortedGateway {
    async fn place(&self, _order: PlaceOrder) -> Result<PyDict, GatewayError> {
        Err(not_ported("place"))
    }

    async fn place_gtt_stop(&self, _stop: GttStop) -> Result<PyDict, GatewayError> {
        Err(not_ported("place_gtt_stop"))
    }

    async fn delete_gtt(&self, _req: DeleteGtt) -> Result<PyDict, GatewayError> {
        Err(not_ported("delete_gtt"))
    }

    async fn modify_gtt_quantity(&self, _req: ModifyGtt) -> Result<PyDict, GatewayError> {
        Err(not_ported("modify_gtt_quantity"))
    }

    async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
        Err(not_ported("cancel_order"))
    }
}

/// The lane's routes. Empty until ported.
pub fn routes() -> Router<AppState> {
    Router::new()
        .merge(exec_guards::routes())
        .merge(exec_kite::routes())
        .merge(lifecycle::routes())
}

/// The lane's template globals and filters (e.g. the fno lane's `fno_badge`).
pub fn register_templates(templates: &mut crate::templates::Templates) {
    let _ = templates;
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    let _ = state;
    let _ = name;
    None
}
