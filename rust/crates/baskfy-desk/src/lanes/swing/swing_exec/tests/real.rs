//! The Python suites' `gw` fixture: `build_swing_gateway(ExplodingKC(), RiskManager())` — the
//! REAL gateway (guards, risk, rate limits, idempotency, journal) bound to the swing book's
//! gates, band and journal, over a broker that explodes on every write. Here that broker is the
//! replay Kite with an error recorded for each write route, so a call that ever left the
//! gateway's dry-run branch would come back `ERROR … reached the broker` and be logged in
//! [`Rig::write_calls`]. The journal is a file in a temporary directory, read back the way
//! `journal_events(gw)` reads `gw._journal_path`.

use std::sync::Arc;

use super::*;
use crate::clock::FixedClock;
use crate::fake_kite::ReplayKite;
use crate::gateway::{GatesFn, ProductGates};
use crate::ids::RandomIds;
use crate::lanes::exec_core::desk::ShimGateway;
use crate::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use crate::lanes::exec_core::ratelimit::GatewayLimits;
use crate::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskManager};

/// `ExplodingKC`: every method that could reach Zerodha raises.
const EXPLODING: &str = r#"{
  "session": {"authed": true},
  "routes": {
    "order.place": {"error": {"type": "GeneralException", "message": "an order reached the broker", "code": 500}},
    "order.modify": {"error": {"type": "GeneralException", "message": "an order modify reached the broker", "code": 500}},
    "order.cancel": {"error": {"type": "GeneralException", "message": "an order cancel reached the broker", "code": 500}},
    "gtt.place": {"error": {"type": "GeneralException", "message": "a GTT reached the broker", "code": 500}},
    "gtt.modify": {"error": {"type": "GeneralException", "message": "a GTT modify reached the broker", "code": 500}},
    "gtt.delete": {"error": {"type": "GeneralException", "message": "a GTT cancel reached the broker", "code": 500}}
  },
  "quotes": {},
  "historical": {}
}"#;

/// `_unthrottled(gw)`: the limiter's clock removed; guards, risk, idempotency and journal stay.
struct NoWait;

#[async_trait]
impl GatewayLimits for NoWait {
    async fn order_slot(&self) {}
    async fn api_slot(&self) {}
}

/// One `gw`: the real gateway, what it saw, and where it journals.
pub(super) struct Rig {
    pub(super) settings: Settings,
    pub(super) kite: Arc<ReplayKite>,
    /// `app.core.gateway.OrderGateway`: the desk's shim over the core gateway (the operator
    /// tenant stamped, `gross_exposure` filled from price × qty when the caller passes none) —
    /// what `build_swing_gateway` builds.
    shim: ShimGateway,
    pub(super) core: Arc<OrderGateway>,
    pub(super) risk: Arc<RiskManager>,
    gates: GatesFn,
    /// `SpyGateway.seen`: every order-shaped call with the `dry_run` its gates said then.
    pub(super) seen: Mutex<Vec<(String, bool)>>,
    /// `Watched`: the order of lock, unlock and gateway calls, shared with the store.
    pub(super) log: Option<Arc<Mutex<Vec<String>>>>,
    _dir: tempfile::TempDir,
}

#[derive(Default)]
pub(super) struct RigOptions {
    pub(super) risk: RiskConfig,
    pub(super) unthrottled: bool,
}

impl Rig {
    /// `build_swing_gateway(ExplodingKC(), RiskManager())` under `settings`.
    pub(super) async fn new(settings: &Settings) -> Self {
        Self::with(settings, RigOptions::default()).await
    }

    /// `fast_gw`: the same, unthrottled.
    pub(super) async fn fast(settings: &Settings) -> Self {
        Self::with(
            settings,
            RigOptions {
                unthrottled: true,
                ..RigOptions::default()
            },
        )
        .await
    }

    pub(super) async fn with(settings: &Settings, options: RigOptions) -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("kite.json"), EXPLODING).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        let clock = Arc::new(FixedClock::new(now()));
        let risk = Arc::new(
            RiskManager::new(options.risk, clock.clone(), RiskBacking::Memory)
                .await
                .unwrap(),
        );
        let frozen = settings.clone();
        // `gates=swing_gates`: a callable, read at the moment of each order.
        let gates: GatesFn = Arc::new(move || -> ProductGates { x::swing_gates(&frozen) });
        let core = Arc::new(
            OrderGateway::new(
                kite.clone(),
                risk.clone(),
                GatewayOptions {
                    gates: gates.clone(),
                    journal_path: dir.path().join(x::SWING_JOURNAL_NAME),
                    stop_band: Some(x::swing_stop_band(settings).unwrap()),
                    coverage: None,
                    clock,
                    ids: Arc::new(RandomIds),
                    limits: if options.unthrottled {
                        Some(Arc::new(NoWait))
                    } else {
                        None
                    },
                },
            )
            .unwrap(),
        );
        let shim = ShimGateway::new(
            core.clone(),
            Arc::new(std::sync::RwLock::new(settings.clone())),
        );
        Self {
            settings: settings.clone(),
            kite,
            shim,
            core,
            risk,
            gates,
            seen: Mutex::new(Vec::new()),
            log: None,
            _dir: dir,
        }
    }

    fn record(&self, method: &str) {
        let dry = (self.gates)().dry_run;
        self.seen.lock().unwrap().push((method.to_owned(), dry));
        if let Some(log) = &self.log {
            log.lock().unwrap().push(method.to_owned());
        }
    }

    pub(super) fn methods(&self) -> Vec<String> {
        self.seen
            .lock()
            .unwrap()
            .iter()
            .map(|(m, _)| m.clone())
            .collect()
    }

    /// Every JSON line of the journal, in order (none when nothing reached the gateway).
    pub(super) fn journal(&self) -> Vec<serde_json::Value> {
        let path = self.core.journal_path();
        if !path.exists() {
            return Vec::new();
        }
        std::fs::read_to_string(path)
            .unwrap()
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }

    /// `journal_events(gw)`.
    pub(super) fn events(&self) -> Vec<String> {
        self.journal()
            .iter()
            .map(|r| r["event"].as_str().unwrap_or_default().to_owned())
            .collect()
    }

    /// `sent_quantities(gw)`: the quantity of every dry-run ORDER journalled.
    pub(super) fn sent_quantities(&self) -> Vec<i64> {
        self.journal()
            .iter()
            .filter(|r| r["event"] == "dry_run")
            .map(|r| r["qty"].as_i64().unwrap())
            .collect()
    }

    /// `gw._sent`.
    pub(super) async fn sent(&self) -> std::collections::HashMap<String, String> {
        self.core.sent().await
    }

    /// `gw._gtt_sent`.
    pub(super) async fn gtt_sent(&self) -> std::collections::HashMap<String, PyValue> {
        self.core.gtt_sent().await
    }

    /// What reached the (exploding) broker: always empty on a dry path.
    pub(super) fn write_calls(&self) -> usize {
        self.kite.write_calls().len()
    }
}

#[async_trait]
impl Gateway for Rig {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        self.record("place");
        self.shim.place(order).await
    }

    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        self.record("place_gtt_stop");
        self.core.place_gtt_stop(&stop).await
    }

    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        self.record("delete_gtt");
        let id = PyValue::Int(req.gtt_id);
        self.core.delete_gtt_value(&req, &id).await
    }

    async fn modify_gtt_quantity(&self, req: ModifyGtt) -> Result<PyDict, GatewayError> {
        self.record("modify_gtt_quantity");
        let id = PyValue::Int(req.gtt_id);
        self.core.modify_gtt_value(&req, &id).await
    }

    async fn cancel_order(&self, req: CancelOrder) -> Result<PyDict, GatewayError> {
        self.record("cancel_order");
        let id = PyValue::Str(req.order_id.clone());
        self.core.cancel_order_value(&req, &id).await
    }
}

// --- the drivers over the rig ----------------------------------------------------------------

/// `execute(store, gw, plan_id, line_id, confirm=..., now=..., last_price=...)`.
pub(super) async fn run_line(
    store: &mut MemoryStore,
    gw: &dyn Gateway,
    settings: &Settings,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    at: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> Result<ExecOutcome, ExecError> {
    let clock = FakeClock::default();
    let cx = ExecContext {
        settings,
        orders: None,
        timer: &clock,
    };
    x::execute_line(store, gw, &cx, plan_id, line_id, confirm, at, last_price).await
}

/// `execute(store, gw, plan_id, line_id, last_price=...)` over the rig, confirmed, at `NOW`.
pub(super) async fn exec(
    store: &mut MemoryStore,
    rig: &Rig,
    plan_id: &str,
    line_id: i64,
    last_price: Option<Decimal>,
) -> Result<ExecOutcome, ExecError> {
    run_line(
        store,
        rig,
        &rig.settings,
        plan_id,
        line_id,
        "true",
        now(),
        last_price,
    )
    .await
}

/// `run(X.rearm_gtt(store, gw, position_id=..., confirm=..., now=..., last_price=...))`.
pub(super) async fn rearm(
    store: &mut MemoryStore,
    gw: &dyn Gateway,
    settings: &Settings,
    position_id: i64,
    confirm: &str,
    last_price: Option<Decimal>,
) -> Result<ExecOutcome, ExecError> {
    x::rearm_gtt(store, gw, settings, position_id, confirm, now(), last_price).await
}

// --- reading a result dict --------------------------------------------------------------------

pub(super) fn st(d: &PyDict) -> String {
    match d.get("status") {
        Some(PyValue::Str(s)) => s.clone(),
        other => format!("{other:?}"),
    }
}

pub(super) fn int(d: &PyDict, k: &str) -> i64 {
    match d.get(k) {
        Some(PyValue::Int(i)) => *i,
        other => panic!("{k} is {other:?}"),
    }
}

pub(super) fn float(d: &PyDict, k: &str) -> f64 {
    match d.get(k) {
        Some(PyValue::Float(f)) => *f,
        #[allow(clippy::cast_precision_loss)]
        Some(PyValue::Int(i)) => *i as f64,
        other => panic!("{k} is {other:?}"),
    }
}

pub(super) fn text(d: &PyDict, k: &str) -> String {
    match d.get(k) {
        Some(PyValue::Str(s)) => s.clone(),
        Some(PyValue::Int(i)) => i.to_string(),
        other => panic!("{k} is {other:?}"),
    }
}

/// A fill row's field.
pub(super) fn fill_val<'a>(fill: &'a Fields, k: &str) -> &'a Val {
    &fill.iter().find(|(c, _)| *c == k).unwrap().1
}
