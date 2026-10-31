//! The desk's wiring of the gateway: `app/core/gateway.py` (the shim — the operator tenant
//! stamped when the console passes none, `gross_exposure` filled from `|price × qty|`, the
//! product gates read off the settings at the moment of each order) and `app.main.gateway()`
//! (one risk manager for the process, limits derived from the latest NAV, the shared
//! `risk_ledger` row on a Postgres desk), plus the sleeves' siblings (`Gateway::derive`).
//!
//! **Lazy, as Python is.** `app.main.gateway()` builds the risk manager on first use, reading
//! the NAV then; so does [`SharedRisk`]. A sibling built before that shares the same cell.

use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;
use tokio::sync::OnceCell;

use super::Broker;
use super::guards::{CoverageRule, core_coverage};
use super::kite_client::DeskKite;
use super::order_gateway::{GatewayOptions, OrderGateway};
use super::risk::{PgRiskStateStore, RiskBacking, RiskError, RiskLayer, RiskManager, risk_config};
use crate::clock::Clock;
use crate::gateway::{
    CancelOrder, DeleteGtt, DerivedGateway, GatesFn, Gateway, GatewayError, GttStop, ModifyGtt,
    PlaceOrder, ProductGates, SleeveGatewaySpec, StopBand, TenantIds,
};
use crate::ids::IdSource;
use crate::settings::{Settings, SharedSettings};
use crate::storage::{Db, DbValue};
use crate::wire::{PyDict, PyValue};

fn snapshot(settings: &SharedSettings) -> Settings {
    match settings.read() {
        Ok(s) => s.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    }
}

/// `_gates_from_config()`: the desk's three switches, read now (the carry switch is the FO
/// book's own gateway's).
#[must_use]
pub fn gates_from_settings(settings: SharedSettings) -> GatesFn {
    Arc::new(move || ProductGates::from_settings(&snapshot(&settings)))
}

/// `app.main._latest_nav()`: the newest stored EOD snapshot's NAV, `0.0` on any failure.
pub async fn latest_nav(db: &Db) -> f64 {
    let Ok(mut conn) = db.connect().await else {
        return 0.0;
    };
    if conn.migrate().await.is_err() {
        return 0.0;
    }
    let Ok(cur) = conn
        .execute(
            "SELECT date, nav, invested, cash, index_value FROM snapshots ORDER BY date",
            &[],
        )
        .await
    else {
        return 0.0;
    };
    cur.rows
        .last()
        .and_then(|r| r.get("nav"))
        .and_then(DbValue::as_f64)
        .unwrap_or(0.0)
}

/// The one risk manager of the process (`app.main._risk`), built on first use.
pub struct SharedRisk {
    cell: OnceCell<Arc<RiskManager>>,
    settings: SharedSettings,
    clock: Arc<dyn Clock>,
    db: Db,
}

impl std::fmt::Debug for SharedRisk {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SharedRisk")
            .field("built", &self.cell.initialized())
            .finish_non_exhaustive()
    }
}

impl SharedRisk {
    /// A cell that will build the manager from these on first use.
    #[must_use]
    pub fn new(settings: SharedSettings, clock: Arc<dyn Clock>, db: Db) -> Self {
        Self {
            cell: OnceCell::new(),
            settings,
            clock,
            db,
        }
    }

    /// A cell holding an already-built manager (tests).
    #[must_use]
    pub fn of(
        manager: Arc<RiskManager>,
        settings: SharedSettings,
        clock: Arc<dyn Clock>,
        db: Db,
    ) -> Self {
        Self {
            cell: OnceCell::new_with(Some(manager)),
            settings,
            clock,
            db,
        }
    }

    /// `app.main.gateway()`'s risk half: NAV → limits → `RiskManager(cfg, store=_risk_store())`.
    ///
    /// # Errors
    /// An unparsable limit override, or the risk store.
    pub async fn get(&self) -> Result<Arc<RiskManager>, RiskError> {
        self.cell
            .get_or_try_init(|| async {
                let nav = latest_nav(&self.db).await;
                let settings = snapshot(&self.settings);
                let cfg = risk_config(nav, &settings)?;
                let backing = if self.db.config().is_postgres() {
                    let clock = self.clock.clone();
                    RiskBacking::Store(Arc::new(PgRiskStateStore::new(
                        self.db.clone(),
                        settings.sole_user_id,
                        "public",
                        Arc::new(move || clock.today_ist()),
                    )))
                } else {
                    RiskBacking::Memory
                };
                Ok(Arc::new(
                    RiskManager::new(cfg, self.clock.clone(), backing).await?,
                ))
            })
            .await
            .cloned()
    }
}

#[async_trait]
impl RiskLayer for SharedRisk {
    async fn pre_order(
        &self,
        symbol: &str,
        value: f64,
        gross: f64,
        side: &str,
    ) -> Result<(bool, String), RiskError> {
        self.get()
            .await?
            .pre_order(symbol, value, gross, side)
            .await
    }

    async fn kill_switch_reason(&self) -> Result<String, RiskError> {
        RiskLayer::kill_switch_reason(self.get().await?.as_ref()).await
    }
}

/// `app.core.gateway.OrderGateway`: the core gateway with the desk's `place()` stamping.
#[derive(Debug, Clone)]
pub struct ShimGateway {
    core: Arc<OrderGateway>,
    settings: SharedSettings,
}

impl ShimGateway {
    /// The core gateway's rate limiter.
    #[must_use]
    pub fn limits(&self) -> Arc<dyn crate::lanes::exec_core::ratelimit::GatewayLimits> {
        self.core.limits()
    }

    /// Wrap a core gateway.
    #[must_use]
    pub const fn new(core: Arc<OrderGateway>, settings: SharedSettings) -> Self {
        Self { core, settings }
    }

    /// The core gateway (its `_sent`, its journal).
    #[must_use]
    pub const fn core(&self) -> &Arc<OrderGateway> {
        &self.core
    }

    /// The operator tenant (`TenantIds(SOLE_USER_ID, SOLE_BROKER_ACCOUNT_ID)`).
    ///
    /// # Errors
    /// `ValueError("tenant ids must be positive")`.
    pub fn sole_tenant(&self) -> Result<TenantIds, GatewayError> {
        let s = snapshot(&self.settings);
        TenantIds::new(s.sole_user_id, s.sole_broker_account_id).map_err(|e| GatewayError {
            python_type: "ValueError".to_owned(),
            message: e.0.trim_start_matches("ValueError: ").to_owned(),
        })
    }

    /// The shim's `place`: tenant stamped when absent, `gross_exposure` filled when absent.
    ///
    /// # Errors
    /// As [`OrderGateway::place`].
    pub async fn place(&self, mut order: PlaceOrder) -> Result<PyDict, GatewayError> {
        let sole = self.sole_tenant()?;
        if order.gross_exposure.is_none() {
            let price = order.price.or(order.reference_price).unwrap_or(0.0);
            #[allow(clippy::cast_precision_loss)]
            let qty = order.qty as f64;
            order.gross_exposure = Some((price * qty).abs());
        }
        order.tenant = Some(order.tenant.unwrap_or(sole));
        order.plan_tenant = Some(order.plan_tenant.unwrap_or(sole));
        self.core.place(&order).await
    }
}

#[async_trait]
impl Gateway for ShimGateway {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        Self::place(self, order).await
    }

    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        self.core.place_gtt_stop(&stop).await
    }

    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        let id = PyValue::Int(req.gtt_id);
        self.core.delete_gtt_value(&req, &id).await
    }

    async fn modify_gtt_quantity(&self, req: ModifyGtt) -> Result<PyDict, GatewayError> {
        let id = PyValue::Int(req.gtt_id);
        self.core.modify_gtt_value(&req, &id).await
    }

    async fn cancel_order(&self, req: CancelOrder) -> Result<PyDict, GatewayError> {
        let id = PyValue::Str(req.order_id.clone());
        self.core.cancel_order_value(&req, &id).await
    }
}

/// What the desk's gateway was built from.
#[derive(Clone)]
pub struct DeskParts {
    /// The broker (the only `KiteWrites` handle in the process).
    pub kite: Arc<Broker>,
    /// Settings.
    pub settings: SharedSettings,
    /// Clock.
    pub clock: Arc<dyn Clock>,
    /// `uuid4`.
    pub ids: Arc<dyn IdSource>,
    /// The desk's journal (`data/outputs/orders_journal.jsonl`, resolved).
    pub journal_path: PathBuf,
    /// The database.
    pub db: Db,
}

/// A sibling's specification: `OrderGateway(kc, risk, gates=, journal_path=, stop_band=,
/// coverage=)` as the sleeves' `build_*_gateway` helpers call it.
#[derive(Clone)]
pub struct SiblingSpec {
    /// `gates=`.
    pub gates: GatesFn,
    /// `journal_path=`.
    pub journal_path: PathBuf,
    /// `stop_band=` (`None` → 8–12%).
    pub stop_band: Option<StopBand>,
    /// `coverage=`.
    pub coverage: Option<CoverageRule>,
}

/// The desk's gateway: [`ShimGateway`] over the desk's journal and config gates, plus the
/// handles the sleeves need (`handles(&state)`).
pub struct DeskGateway {
    shim: ShimGateway,
    parts: DeskParts,
    risk: Arc<SharedRisk>,
    kite_client: Arc<DeskKite>,
}

impl std::fmt::Debug for DeskGateway {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DeskGateway")
            .field("shim", &self.shim)
            .finish_non_exhaustive()
    }
}

impl DeskGateway {
    /// Build the desk's gateway (the risk manager and the NAV read wait for first use).
    ///
    /// # Errors
    /// The journal directory cannot be created or the journal cannot be replayed.
    pub fn build(parts: DeskParts) -> Result<Self, GatewayError> {
        let risk = Arc::new(SharedRisk::new(
            parts.settings.clone(),
            parts.clock.clone(),
            parts.db.clone(),
        ));
        Self::build_with_risk(parts, risk)
    }

    /// As [`DeskGateway::build`] over a given risk cell (tests).
    ///
    /// # Errors
    /// As [`DeskGateway::build`].
    pub fn build_with_risk(parts: DeskParts, risk: Arc<SharedRisk>) -> Result<Self, GatewayError> {
        let core = OrderGateway::new(
            parts.kite.clone(),
            risk.clone(),
            GatewayOptions {
                gates: gates_from_settings(parts.settings.clone()),
                journal_path: parts.journal_path.clone(),
                stop_band: None,
                coverage: None,
                clock: parts.clock.clone(),
                ids: parts.ids.clone(),
                limits: None,
            },
        )?;
        let kite_client = Arc::new(DeskKite::new(
            parts.kite.clone(),
            parts.settings.clone(),
            parts.clock.clone(),
        ));
        Ok(Self {
            shim: ShimGateway::new(Arc::new(core), parts.settings.clone()),
            parts,
            risk,
            kite_client,
        })
    }

    /// The shim (its core's `_sent`, journal).
    #[must_use]
    pub const fn shim(&self) -> &ShimGateway {
        &self.shim
    }

    /// `app.main._risk`: the process's one risk manager (built on first call).
    ///
    /// # Errors
    /// As [`SharedRisk::get`].
    pub async fn risk(&self) -> Result<Arc<RiskManager>, RiskError> {
        self.risk.get().await
    }

    /// `gw.limits`: the gateway's rate limiter.
    #[must_use]
    pub fn limits(&self) -> Arc<dyn crate::lanes::exec_core::ratelimit::GatewayLimits> {
        self.shim.limits()
    }

    /// `app.main.kite()`: the desk's Kite wrapper (reads limited per family, the LTP cache,
    /// holdings with T1/collateral/same-day buys, and the documented GTT exception).
    #[must_use]
    pub fn kite(&self) -> Arc<DeskKite> {
        self.kite_client.clone()
    }

    /// A sibling over the same broker and the same risk manager, with its own gates, journal,
    /// band and cover rule (`build_swing_gateway`, `build_twt_gateway`, `fo_gateway`, …).
    ///
    /// # Errors
    /// Its journal cannot be opened or replayed.
    pub fn sibling(&self, spec: SiblingSpec) -> Result<ShimGateway, GatewayError> {
        let core = OrderGateway::new(
            self.parts.kite.clone(),
            self.risk.clone(),
            GatewayOptions {
                gates: spec.gates,
                journal_path: spec.journal_path,
                stop_band: spec.stop_band,
                coverage: spec.coverage,
                clock: self.parts.clock.clone(),
                ids: self.parts.ids.clone(),
                limits: None,
            },
        )?;
        Ok(ShimGateway::new(
            Arc::new(core),
            self.parts.settings.clone(),
        ))
    }
}

#[async_trait]
impl Gateway for DeskGateway {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        self.shim.place(order).await
    }

    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        Gateway::place_gtt_stop(&self.shim, stop).await
    }

    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        Gateway::delete_gtt(&self.shim, req).await
    }

    async fn modify_gtt_quantity(&self, req: ModifyGtt) -> Result<PyDict, GatewayError> {
        Gateway::modify_gtt_quantity(&self.shim, req).await
    }

    async fn cancel_order(&self, req: CancelOrder) -> Result<PyDict, GatewayError> {
        Gateway::cancel_order(&self.shim, req).await
    }

    fn derive(&self, spec: DerivedGateway) -> Option<Arc<dyn Gateway>> {
        let sibling = self
            .sibling(SiblingSpec {
                gates: spec.gates,
                journal_path: spec.journal_path,
                stop_band: None,
                coverage: spec.fo_coverage.then(core_coverage),
            })
            .ok()?;
        Some(Arc::new(sibling))
    }

    fn derive_sleeve(&self, spec: SleeveGatewaySpec) -> Option<Arc<dyn Gateway>> {
        let sibling = self
            .sibling(SiblingSpec {
                gates: spec.derived.gates,
                journal_path: spec.derived.journal_path,
                stop_band: Some(spec.stop_band),
                coverage: spec.derived.fo_coverage.then(core_coverage),
            })
            .ok()?;
        Some(Arc::new(sibling))
    }

    fn as_any(&self) -> Option<&(dyn std::any::Any + Send + Sync)> {
        Some(self)
    }
}
