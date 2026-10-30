//! `tests/test_twt_execute.py`'s gateways: `gateway()` — `SpyGateway(build_twt_gateway(
//! ExplodingKC(), RiskManager()))`, the REAL gateway (guards, risk, rate limits, idempotency,
//! journal) bound to this sleeve's gates, band and journal over a broker that explodes on every
//! write — and the hand-made ones a test needs to make the *broker* say no (`FlakyGateway`,
//! `_BuyOkArmFails`) or to see what was asked (`_Recorder`, `Recording`, `Accepting`).
//!
//! `ExplodingKC` here is the replay Kite with an error recorded for every write route, so a call
//! that ever left the gateway's dry-run branch comes back `ERROR … reached the broker` **and** is
//! logged in [`ReplayKite::write_calls`] — which `_CountingKC`'s test asserts is empty.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex};

use async_trait::async_trait;

use super::execute as x;
use crate::clock::FixedClock;
use crate::fake_kite::ReplayKite;
use crate::gateway::{
    CancelOrder, DeleteGtt, GatesFn, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
    ProductGates,
};
use crate::ids::RandomIds;
use crate::lanes::exec_core::desk::ShimGateway;
use crate::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use crate::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskManager};
use crate::settings::Settings;
use crate::wire::{PyDict, PyValue, dict};

/// `ExplodingKC`: every method that could reach Zerodha raises.
pub const EXPLODING: &str = r#"{
  "session": {"authed": true},
  "routes": {
    "order.place": {"error": {"type": "GeneralException", "message": "an order reached the broker", "code": 500}},
    "order.modify": {"error": {"type": "GeneralException", "message": "an order modify reached the broker", "code": 500}},
    "order.cancel": {"error": {"type": "GeneralException", "message": "a cancel reached the broker", "code": 500}},
    "gtt.place": {"error": {"type": "GeneralException", "message": "a GTT reached the broker", "code": 500}},
    "gtt.modify": {"error": {"type": "GeneralException", "message": "a GTT modify reached the broker", "code": 500}},
    "gtt.delete": {"error": {"type": "GeneralException", "message": "a GTT cancel reached the broker", "code": 500}}
  },
  "quotes": {},
  "historical": {}
}"#;

/// The desk's flags as a test sets them: `DRY_RUN`, `BASKFY_TWT_EXECUTION_ENABLED`,
/// `BASKFY_TWT_AUTO_EXECUTE`, and the operator tenant.
pub fn flags(dry_run: bool, execution: bool, auto: bool) -> Settings {
    let mut s = Settings::defaults().unwrap();
    s.dry_run = dry_run;
    s.twt_execution_enabled = execution;
    s.twt_auto_execute = auto;
    s.sole_user_id = 1;
    s.sole_broker_account_id = 1;
    s
}

/// `_flag_is_false`: the sleeve's flag false and the desk in dry run. **No test flips either.**
pub fn dry() -> Settings {
    flags(true, false, false)
}

/// `SpyGateway.tape`: shared across the module, so the closing test can say the spy was
/// actually exercised.
pub static TAPE: Mutex<Vec<(String, String)>> = Mutex::new(Vec::new());

/// `gateway()`: the real gateway behind the spy, over `ExplodingKC`.
pub struct Spy {
    shim: ShimGateway,
    core: Arc<OrderGateway>,
    pub kite: Arc<ReplayKite>,
    _dir: tempfile::TempDir,
}

impl Spy {
    /// `build_twt_gateway(ExplodingKC(), RiskManager())` under `settings`, behind the spy.
    pub async fn new(settings: &Settings) -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("kite.json"), EXPLODING).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        let clock = Arc::new(FixedClock::new(super::memory::now()));
        let risk = Arc::new(
            RiskManager::new(RiskConfig::default(), clock.clone(), RiskBacking::Memory)
                .await
                .unwrap(),
        );
        let frozen = settings.clone();
        // `gates=twt_gates`: a callable, read at the moment of each order.
        let gates: GatesFn = Arc::new(move || -> ProductGates { x::twt_gates(&frozen) });
        let core = Arc::new(
            OrderGateway::new(
                kite.clone(),
                risk,
                GatewayOptions {
                    gates,
                    journal_path: dir.path().join(x::TWT_JOURNAL_NAME),
                    stop_band: Some(x::twt_stop_band().unwrap()),
                    coverage: None,
                    clock,
                    ids: Arc::new(RandomIds),
                    limits: None,
                },
            )
            .unwrap(),
        );
        let shim = ShimGateway::new(
            core.clone(),
            Arc::new(std::sync::RwLock::new(settings.clone())),
        );
        Self {
            shim,
            core,
            kite,
            _dir: dir,
        }
    }

    /// How many lines the gateway journalled (none when it was asked nothing).
    pub fn journal_lines(&self) -> usize {
        let path = self.core.journal_path();
        std::fs::read_to_string(path)
            .map_or(0, |t| t.lines().filter(|l| !l.trim().is_empty()).count())
    }

    /// `_record(call, result)`: tape it, and refuse anything but a dry-run answer **on every
    /// call** — a single `PLACED` would be a real order.
    fn record(call: &str, result: Result<PyDict, GatewayError>) -> Result<PyDict, GatewayError> {
        let result = result?;
        let status = result
            .get("status")
            .map_or_else(|| "None".to_owned(), PyValue::py_str);
        TAPE.lock().unwrap().push((call.to_owned(), status.clone()));
        assert!(
            status.starts_with("DRY_RUN"),
            "{call} answered {status:?} with BASKFY_TWT_EXECUTION_ENABLED false — that is a real \
             order, and this suite must place none"
        );
        Ok(result)
    }
}

#[async_trait]
impl Gateway for Spy {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        Self::record("place", self.shim.place(order).await)
    }
    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        Self::record("place_gtt_stop", self.core.place_gtt_stop(&stop).await)
    }
    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        let id = PyValue::Int(req.gtt_id);
        Self::record("delete_gtt", self.core.delete_gtt_value(&req, &id).await)
    }
    async fn modify_gtt_quantity(&self, _req: ModifyGtt) -> Result<PyDict, GatewayError> {
        panic!("SpyGateway has no modify_gtt_quantity: this module never modifies a GTT")
    }
    async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
        panic!("SpyGateway has no cancel_order: this module never cancels an order")
    }
}

fn s(text: &str) -> PyValue {
    PyValue::Str(text.to_owned())
}

/// `FlakyGateway(cancel_ok=..., arm_ok=...)`: the two GTT halves made to fail one at a time.
/// Not a stand-in for the gateway's logic; a way to make the *broker* say no.
pub struct Flaky {
    pub cancel_ok: bool,
    pub arm_ok: bool,
    pub armed: Mutex<Vec<GttStop>>,
    pub cancelled: Mutex<Vec<DeleteGtt>>,
}

impl Flaky {
    pub fn new(cancel_ok: bool, arm_ok: bool) -> Self {
        Self {
            cancel_ok,
            arm_ok,
            armed: Mutex::new(Vec::new()),
            cancelled: Mutex::new(Vec::new()),
        }
    }

    pub fn ok() -> Self {
        Self::new(true, true)
    }

    pub fn armed(&self) -> usize {
        self.armed.lock().unwrap().len()
    }

    pub fn cancelled(&self) -> usize {
        self.cancelled.lock().unwrap().len()
    }
}

#[async_trait]
impl Gateway for Flaky {
    async fn place(&self, _order: PlaceOrder) -> Result<PyDict, GatewayError> {
        panic!("the flaky gateway is for GTTs; no buy should reach it")
    }
    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        let id = req.gtt_id;
        self.cancelled.lock().unwrap().push(req);
        if self.cancel_ok {
            return Ok(dict([
                ("status", s("GTT_DELETED")),
                ("gtt_id", PyValue::Int(id)),
            ]));
        }
        Ok(dict([
            ("status", s("GTT_DELETE_ERROR")),
            ("error", s("the exchange refused the cancel")),
        ]))
    }
    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        let trigger = stop.trigger;
        self.armed.lock().unwrap().push(stop);
        if self.arm_ok {
            return Ok(dict([
                ("status", s("GTT_PLACED")),
                ("gtt_id", PyValue::Int(998_877)),
                ("trigger", PyValue::Float(trigger)),
            ]));
        }
        Ok(dict([
            ("status", s("GTT_ERROR")),
            ("error", s("the exchange refused the trigger")),
        ]))
    }
    async fn modify_gtt_quantity(&self, _req: ModifyGtt) -> Result<PyDict, GatewayError> {
        panic!("FlakyGateway has no modify_gtt_quantity")
    }
    async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
        panic!("FlakyGateway has no cancel_order")
    }
}

/// What a hand-made gateway does on `place` and `place_gtt_stop`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// `_BuyOkArmFails`: the buy simulates, the GTT will not arm.
    BuyOkArmFails,
    /// `_Recorder`: records the buy, simulates both.
    Recorder,
    /// `Recording` (TestTheSellAtOpen): records the sell; it has no GTT method.
    Recording,
    /// `Accepting`: the broker accepted the sell (`PLACED`); no GTT method.
    Accepting,
}

/// The hand-made gateways, with what they were asked.
pub struct Hand {
    pub kind: Kind,
    pub seen: Mutex<Vec<PlaceOrder>>,
}

impl Hand {
    pub fn new(kind: Kind) -> Self {
        Self {
            kind,
            seen: Mutex::new(Vec::new()),
        }
    }

    pub fn seen(&self) -> Vec<PlaceOrder> {
        self.seen.lock().unwrap().clone()
    }
}

fn absent(what: &str) -> GatewayError {
    GatewayError {
        python_type: "AttributeError".to_owned(),
        message: format!("the test gateway has no attribute '{what}'"),
    }
}

#[async_trait]
impl Gateway for Hand {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        self.seen.lock().unwrap().push(order);
        Ok(match self.kind {
            Kind::BuyOkArmFails | Kind::Recorder => {
                dict([("status", s("DRY_RUN")), ("order_id", s("DRY-1"))])
            }
            Kind::Recording => dict([("status", s("DRY_RUN")), ("order_id", s("dry"))]),
            Kind::Accepting => dict([("status", s("PLACED")), ("order_id", s("BRK-SELL-1"))]),
        })
    }
    async fn place_gtt_stop(&self, _stop: GttStop) -> Result<PyDict, GatewayError> {
        match self.kind {
            Kind::BuyOkArmFails => Ok(dict([
                ("status", s("GTT_ERROR")),
                ("error", s("the exchange refused the trigger")),
            ])),
            Kind::Recorder => Ok(dict([
                ("status", s("DRY_RUN_GTT")),
                ("gtt_id", PyValue::None),
                ("client_id", s("c")),
            ])),
            Kind::Recording | Kind::Accepting => Err(absent("place_gtt_stop")),
        }
    }
    async fn delete_gtt(&self, _req: DeleteGtt) -> Result<PyDict, GatewayError> {
        if self.kind == Kind::BuyOkArmFails {
            panic!("a fill never cancels a trigger");
        }
        Err(absent("delete_gtt"))
    }
    async fn modify_gtt_quantity(&self, _req: ModifyGtt) -> Result<PyDict, GatewayError> {
        Err(absent("modify_gtt_quantity"))
    }
    async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
        Err(absent("cancel_order"))
    }
}
