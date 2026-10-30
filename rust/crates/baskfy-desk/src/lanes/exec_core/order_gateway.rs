//! Ports `baskfy_execution.gateway.OrderGateway`: THE ONLY PATH TO AN ORDER (law 2).
//!
//! guards → risk → rate limits → idempotency → journal → broker. Nothing else holds the
//! broker's order verbs ([`crate::kite::KiteWrites`]); the guard refuses an untouchable instrument **before
//! any network call**; `DRY_RUN` (read from the gates at the moment of each order, never at
//! construction) simulates end to end and never reaches the transport.
//!
//! The four layers appear in this file in order under their Python names — "layer 1:
//! untouchables", "layer 2: risk", "layer 3: idempotency", "layer 4: rate limits" for an order,
//! and "GTT layer 1: untouchables", "GTT layer 1b", "GTT layer 2: risk", "GTT layer 3:
//! idempotency", "GTT layer 4: rate limits" for a stop — and `tests/exec_core_*.rs` checks the
//! order, as `test_non_negotiables.py` does over the Python source.
//!
//! Result dicts, journal records and messages are Python's, key for key.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use tokio::sync::Mutex;

use super::Broker;
use super::gtt::{
    TickSizes, band_finding, drop_pct, gtt_params, py_round, py_round0, refuse_stop, to_tick,
};
use super::guards::{
    self, CoverageRule, FO_CARRY_PRODUCT, assert_not_overnight_option,
    assert_overnight_option_is_covered, assert_tradeable, fo_gtt_refusal, fo_order_refusal,
    product_exchange_refusal,
};
use super::journal::Journal;
use super::ratelimit::{GatewayLimits, KiteLimits};
use super::risk::{RiskError, RiskLayer};
use crate::clock::Clock;
use crate::gateway::{
    CancelOrder, DEFINITIVE_REFUSALS, DeleteGtt, GTT_LIMIT_FRACTION, GatesFn, GatewayError,
    GttStop, ModifyGtt, PlaceOrder, StopBand, TenantIds, gtt_status, refuse_cross_tenant,
};
use crate::ids::IdSource;
use crate::kite::{KiteError, OrderParams};
use crate::wire::{PyDict, PyValue, dict};

/// How a gateway is built: `OrderGateway(kc, risk, gates=, journal_path=, stop_band=,
/// coverage=)`, plus the clock (`time.time` in the journal), `uuid4` and the limiter.
#[derive(Clone)]
pub struct GatewayOptions {
    /// `gates=` — a callable, read per order. [`crate::gateway::ProductGates::default`]
    /// (fail closed) when the caller has none.
    pub gates: GatesFn,
    /// `journal_path=`.
    pub journal_path: PathBuf,
    /// `stop_band=` (`None` → the weekly book's 8–12%).
    pub stop_band: Option<StopBand>,
    /// `coverage=` (`None` fails closed: every FO option order is refused).
    pub coverage: Option<CoverageRule>,
    /// The clock.
    pub clock: Arc<dyn Clock>,
    /// `uuid4`.
    pub ids: Arc<dyn IdSource>,
    /// The limiter (`None` → a fresh [`KiteLimits`], one per gateway as in Python).
    pub limits: Option<Arc<dyn GatewayLimits>>,
}

/// The gateway.
pub struct OrderGateway {
    kite: Arc<Broker>,
    risk: Arc<dyn RiskLayer>,
    limits: Arc<dyn GatewayLimits>,
    gates: GatesFn,
    journal: Journal,
    clock: Arc<dyn Clock>,
    ids: Arc<dyn IdSource>,
    stop_band: StopBand,
    coverage: Option<CoverageRule>,
    sent: Mutex<HashMap<String, String>>,
    gtt_sent: Mutex<HashMap<String, PyValue>>,
    ticks: Mutex<TickSizes>,
}

impl std::fmt::Debug for OrderGateway {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OrderGateway")
            .field("journal", &self.journal)
            .field("stop_band", &self.stop_band)
            .finish_non_exhaustive()
    }
}

fn err(python_type: &str, message: impl Into<String>) -> GatewayError {
    GatewayError {
        python_type: python_type.to_owned(),
        message: message.into(),
    }
}

fn io_err(e: &std::io::Error) -> GatewayError {
    err("OSError", e.to_string())
}

fn risk_err(e: RiskError) -> GatewayError {
    err(&e.python_type, e.message)
}

fn s(v: &str) -> PyValue {
    PyValue::Str(v.to_owned())
}

fn opt_s(v: Option<&str>) -> PyValue {
    v.map_or(PyValue::None, s)
}

fn opt_f(v: Option<f64>) -> PyValue {
    v.map_or(PyValue::None, PyValue::Float)
}

/// Python's `repr()` of a value an id arrived as.
#[must_use]
pub fn py_repr(v: &PyValue) -> String {
    match v {
        PyValue::None => "None".to_owned(),
        PyValue::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
        PyValue::Int(i) => i.to_string(),
        PyValue::Float(f) => baskfy_core::py::float_repr(*f),
        PyValue::Str(st) => baskfy_providers::py::repr_str(st),
        other => format!("{other:?}"),
    }
}

/// `str(v)`.
fn py_str(v: &PyValue) -> String {
    match v {
        PyValue::Str(st) => st.clone(),
        PyValue::None => "None".to_owned(),
        other => py_repr(other),
    }
}

/// A positive `int` that is not a `bool` — what a trigger id must be.
fn trigger_id(v: &PyValue) -> Option<i64> {
    match v {
        PyValue::Int(i) if *i > 0 => Some(*i),
        _ => None,
    }
}

fn tenants(
    tenant: Option<TenantIds>,
    plan_tenant: Option<TenantIds>,
    method: &str,
) -> Result<(TenantIds, TenantIds), GatewayError> {
    match (tenant, plan_tenant) {
        (Some(t), Some(p)) => Ok((t, p)),
        (None, None) => Err(err(
            "TypeError",
            format!(
                "OrderGateway.{method}() missing 2 required keyword-only arguments: 'tenant' and \
                 'plan_tenant'"
            ),
        )),
        (None, _) => Err(err(
            "TypeError",
            format!("OrderGateway.{method}() missing 1 required keyword-only argument: 'tenant'"),
        )),
        (_, None) => Err(err(
            "TypeError",
            format!(
                "OrderGateway.{method}() missing 1 required keyword-only argument: 'plan_tenant'"
            ),
        )),
    }
}

fn check_fraction(limit_fraction: Option<f64>) -> Result<f64, GatewayError> {
    match limit_fraction {
        Some(f) if !(0.0 < f && f <= 1.0) => Err(err(
            "ValueError",
            format!(
                "limit_fraction must be in (0, 1], got {}: a GTT sell limit rests at or under its \
                 trigger, never above it",
                baskfy_core::py::float_repr(f)
            ),
        )),
        Some(f) => Ok(f),
        None => Ok(GTT_LIMIT_FRACTION),
    }
}

impl OrderGateway {
    /// Build the gateway and replay its journal into the idempotency maps (AF 3.4).
    ///
    /// # Errors
    /// The journal directory cannot be created, or the journal cannot be replayed.
    pub fn new(
        kite: Arc<Broker>,
        risk: Arc<dyn RiskLayer>,
        options: GatewayOptions,
    ) -> Result<Self, GatewayError> {
        let journal = Journal::open(&options.journal_path).map_err(|e| io_err(&e))?;
        let replayed = journal.replay().map_err(|e| err("JSONDecodeError", e))?;
        Ok(Self {
            kite,
            risk,
            limits: options
                .limits
                .unwrap_or_else(|| Arc::new(KiteLimits::default())),
            gates: options.gates,
            journal,
            clock: options.clock,
            ids: options.ids,
            stop_band: options.stop_band.unwrap_or_default(),
            coverage: options.coverage,
            sent: Mutex::new(replayed.sent),
            gtt_sent: Mutex::new(replayed.gtt_sent),
            ticks: Mutex::new(TickSizes::default()),
        })
    }

    /// The journal file.
    #[must_use]
    pub fn journal_path(&self) -> &std::path::Path {
        self.journal.path()
    }

    /// The broker (for siblings that share it).
    #[must_use]
    pub fn kite(&self) -> Arc<Broker> {
        self.kite.clone()
    }

    /// The risk layer (for siblings that share it).
    #[must_use]
    pub fn risk(&self) -> Arc<dyn RiskLayer> {
        self.risk.clone()
    }

    /// A copy of `_sent` (client_id → order id).
    pub async fn sent(&self) -> HashMap<String, String> {
        self.sent.lock().await.clone()
    }

    /// A copy of `_gtt_sent` (client_id → trigger id).
    pub async fn gtt_sent(&self) -> HashMap<String, PyValue> {
        self.gtt_sent.lock().await.clone()
    }

    fn journal(&self, rec: PyDict, client_id: Option<&str>) -> Result<(), GatewayError> {
        self.journal
            .write(rec, client_id, self.clock.time())
            .map_err(|e| io_err(&e))
    }

    fn fresh_cid(&self, client_id: Option<&String>) -> String {
        match client_id {
            Some(c) if !c.is_empty() => c.clone(),
            _ => self.ids.uuid4_hex().chars().take(10).collect(),
        }
    }

    /// `place(...)`: tenant → untouchables → overnight-option / FO cover → product gates →
    /// risk → idempotency → rate limit → `DRY_RUN` or the broker.
    ///
    /// # Errors
    /// A missing required argument (`tenant`, `plan_tenant`, `gross_exposure`), the journal,
    /// or the risk store.
    #[allow(clippy::too_many_lines)]
    pub async fn place(&self, o: &PlaceOrder) -> Result<PyDict, GatewayError> {
        let symbol = o.symbol.as_str();
        let (tenant, plan_tenant) = tenants(o.tenant, o.plan_tenant, "place")?;
        let Some(gross_exposure) = o.gross_exposure else {
            return Err(err(
                "TypeError",
                "OrderGateway.place() missing 1 required keyword-only argument: 'gross_exposure'",
            ));
        };
        // Law 2 multi-tenant clause (P4.3): BLOCKED before any guard, risk or network call.
        if let Some(mismatch) = refuse_cross_tenant(tenant, plan_tenant) {
            return Ok(blocked(symbol, &mismatch));
        }
        let gates = (self.gates)();
        // The id is resolved before any refusal can be journalled.
        let cid = self.fresh_cid(o.client_id.as_ref());
        if let Err(exc) = assert_tradeable(symbol, o.series.as_deref()) {
            // layer 1: untouchables — BLOCKED, not raised (AF 3.5).
            self.journal(
                dict([
                    ("event", s("untouchable_block")),
                    ("symbol", s(symbol)),
                    ("series", opt_s(o.series.as_deref())),
                    ("side", s(&o.side)),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked(symbol, &exc.message));
        }
        match &o.fo_plan {
            None => {
                if let Err(exc) = assert_not_overnight_option(symbol, &o.exchange, &o.product) {
                    self.journal(
                        dict([
                            ("event", s("overnight_option_block")),
                            ("symbol", s(symbol)),
                            ("product", s(&o.product)),
                            ("side", s(&o.side)),
                            ("exchange", s(&o.exchange)),
                        ]),
                        Some(&cid),
                    )?;
                    return Ok(blocked(symbol, &exc.message));
                }
            }
            Some(fo_plan) => {
                let mut fo_why = fo_order_refusal(symbol, &o.exchange, fo_plan);
                if fo_why.is_empty()
                    && let Err(exc) = assert_overnight_option_is_covered(
                        symbol,
                        o.qty,
                        &o.side,
                        fo_plan,
                        self.coverage.as_ref(),
                    )
                {
                    fo_why = exc.message;
                }
                if !fo_why.is_empty() {
                    self.journal(
                        dict([
                            ("event", s("fo_uncovered_block")),
                            ("symbol", s(symbol)),
                            ("product", s(&o.product)),
                            ("side", s(&o.side)),
                            ("qty", PyValue::Int(o.qty)),
                            ("exchange", s(&o.exchange)),
                            ("fo_plan", s(&fo_plan.plan_id)),
                            ("why", s(&fo_why)),
                        ]),
                        Some(&cid),
                    )?;
                    return Ok(blocked(symbol, &fo_why));
                }
            }
        }
        let gate_why = product_exchange_refusal(
            &o.product,
            &o.exchange,
            gates.intraday_enabled,
            gates.options_enabled,
            gates.fno_carry_enabled,
            o.fo_plan.is_some(),
        );
        if !gate_why.is_empty() {
            return Ok(blocked(symbol, &gate_why));
        }
        // A MARKET order has no price: it is valued at its reference price, and one with
        // neither is refused rather than valued at zero (NaN/Inf likewise, AF 3.6).
        let price_truthy = o.price.is_some_and(|p| p.is_nan() || p.abs() > 0.0);
        let valuation = if price_truthy {
            o.price
        } else {
            o.reference_price
        };
        let Some(valuation) = valuation.filter(|v| v.is_finite() && *v > 0.0) else {
            let shown = if price_truthy {
                o.price
            } else {
                o.reference_price
            };
            self.journal(
                dict([
                    ("event", s("unpriced_order_block")),
                    ("symbol", s(symbol)),
                    ("side", s(&o.side)),
                    ("qty", PyValue::Int(o.qty)),
                    ("order_type", s(&o.order_type)),
                    ("valuation", opt_f(shown)),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked(
                symbol,
                &format!(
                    "{} order for {symbol} carries no finite positive price and no finite \
                     reference_price; the risk check cannot value it",
                    o.order_type
                ),
            ));
        };
        if !gross_exposure.is_finite() {
            return Ok(blocked(
                symbol,
                &format!(
                    "gross_exposure must be finite, got {}",
                    baskfy_core::py::float_repr(gross_exposure)
                ),
            ));
        }
        #[allow(clippy::cast_precision_loss)]
        let value = (o.qty.abs() as f64) * valuation;
        let (ok, why) = self
            .risk
            .pre_order(symbol, value, gross_exposure, &o.side)
            .await
            .map_err(risk_err)?; // layer 2: risk
        if !ok {
            self.journal(
                dict([
                    ("event", s("risk_block")),
                    ("symbol", s(symbol)),
                    ("why", s(&why)),
                ]),
                Some(&cid),
            )?;
            return Ok(dict([
                ("symbol", s(symbol)),
                ("status", s("RISK_BLOCKED")),
                ("error", s(&why)),
            ]));
        }
        {
            // layer 3: idempotency — reserved under the lock BEFORE the rate-limit await, so a
            // twin request cannot also pass and double-send (AF 3.4).
            let mut sent = self.sent.lock().await;
            if let Some(existing) = sent.get(&cid) {
                return Ok(dict([
                    ("symbol", s(symbol)),
                    ("status", s("DUPLICATE")),
                    ("order_id", s(existing)),
                ]));
            }
            sent.insert(cid.clone(), format!("PENDING-{cid}"));
        }
        self.limits.order_slot().await; // layer 4: rate limits
        if gates.dry_run {
            let oid = format!("DRY-{cid}");
            self.sent.lock().await.insert(cid.clone(), oid.clone());
            let mut rec = dict([
                ("event", s("dry_run")),
                ("symbol", s(symbol)),
                ("side", s(&o.side)),
                ("qty", PyValue::Int(o.qty)),
                ("price", opt_f(o.price)),
                ("product", s(&o.product)),
                ("order_type", s(&o.order_type)),
                ("market_protection", opt_f(o.market_protection)),
                ("reference_price", opt_f(o.reference_price)),
            ]);
            if let Some(fo_plan) = &o.fo_plan {
                rec.insert("fo_plan", s(&fo_plan.plan_id));
            }
            self.journal(rec, Some(&cid))?;
            return Ok(dict([
                ("symbol", s(symbol)),
                ("status", s("DRY_RUN")),
                ("order_id", s(&oid)),
            ]));
        }
        // ---- the live transport: compiled behind the DRY_RUN check, exactly as Python ----
        let mut params = dict([
            ("variety", s(&o.variety)),
            ("exchange", s(&o.exchange)),
            ("tradingsymbol", s(symbol)),
            ("quantity", PyValue::Int(o.qty.abs())),
            ("transaction_type", s(&o.side)),
            ("product", s(&o.product)),
            ("order_type", s(&o.order_type)),
            ("validity", s("DAY")),
        ]);
        let mut wire = OrderParams {
            variety: o.variety.clone(),
            exchange: o.exchange.clone(),
            tradingsymbol: symbol.to_owned(),
            transaction_type: o.side.clone(),
            quantity: o.qty.abs(),
            product: o.product.clone(),
            order_type: o.order_type.clone(),
            validity: Some("DAY".to_owned()),
            ..OrderParams::default()
        };
        if let Some(mp) = o.market_protection
            && o.order_type == "MARKET"
        {
            let mp = PyValue::Float(py_round(mp, 2));
            params.insert("market_protection", mp.clone());
            wire.market_protection = Some(mp);
        }
        if let Some(px) = o.price.filter(|_| price_truthy)
            && o.order_type == "LIMIT"
        {
            if !px.is_finite() {
                self.sent.lock().await.remove(&cid);
                return Ok(blocked(
                    symbol,
                    &format!(
                        "LIMIT price must be finite, got {}",
                        baskfy_core::py::float_repr(px)
                    ),
                ));
            }
            let snapped = match o.tick_size {
                Some(t) if t > 0.0 => py_round(py_round0(px / t) * t, 2),
                _ => py_round(px, 1),
            };
            params.insert("price", PyValue::Float(snapped));
            wire.price = Some(PyValue::Float(snapped));
        }
        match self.kite.place_order(&wire).await {
            Ok(oid) => {
                self.sent.lock().await.insert(cid.clone(), oid.clone());
                let mut rec = dict([("event", s("placed")), ("order_id", s(&oid))]);
                for (k, v) in params.iter() {
                    rec.insert(k, v.clone());
                }
                self.journal(rec, Some(&cid))?;
                Ok(dict([
                    ("symbol", s(symbol)),
                    ("status", s("PLACED")),
                    ("order_id", s(&oid)),
                ]))
            }
            Err(exc) => {
                let name = exc.python_type().to_owned();
                let definitive = DEFINITIVE_REFUSALS.contains(&name.as_str());
                // A definitive refusal never reached the exchange — release so a corrected
                // retry can use the same client_id. An unknown ERROR might have: keep it.
                if definitive {
                    self.sent.lock().await.remove(&cid);
                }
                let text = exc.to_string();
                self.journal(
                    dict([
                        ("event", s(if definitive { "rejected" } else { "error" })),
                        ("symbol", s(symbol)),
                        ("error", s(&text)),
                        ("exception", s(&name)),
                    ]),
                    Some(&cid),
                )?;
                Ok(dict([
                    ("symbol", s(symbol)),
                    ("status", s(if definitive { "REJECTED" } else { "ERROR" })),
                    ("error", s(&text)),
                    ("exception", s(&name)),
                    (
                        "reached_exchange",
                        if definitive {
                            PyValue::Bool(false)
                        } else {
                            PyValue::None
                        },
                    ),
                ]))
            }
        }
    }

    /// `_tick_size(symbol, exchange)`: fetched once per exchange (a network call, its own
    /// rate-limit slot, only after the guards); an empty dump is an error, never cached.
    async fn tick_size(&self, symbol: &str, exchange: &str) -> Result<f64, (String, String)> {
        let mut ticks = self.ticks.lock().await;
        if !ticks.cached(exchange) {
            self.limits.api_slot().await;
            let dump = self
                .kite
                .instruments(Some(exchange))
                .await
                .map_err(|e: KiteError| (e.python_type().to_owned(), e.to_string()))?;
            let rows = match dump {
                PyValue::List(rows) if !rows.is_empty() => rows,
                _ => {
                    return Err((
                        "RuntimeError".to_owned(),
                        format!(
                            "{exchange}: the instrument dump came back empty, so no tick size can \
                             be trusted; refusing to snap a trigger against a guess"
                        ),
                    ));
                }
            };
            ticks.remember(exchange, &rows);
        }
        Ok(ticks.get(symbol, exchange))
    }

    /// `place_gtt_stop(...)`: rest a vol-scaled stop. The ONLY way to create a GTT.
    ///
    /// # Errors
    /// `ValueError` for a `limit_fraction` outside `(0, 1]` (a caller bug, refused before
    /// anything is journalled or sent); a missing tenant; the journal; the risk store.
    #[allow(clippy::too_many_lines)]
    pub async fn place_gtt_stop(&self, g: &GttStop) -> Result<PyDict, GatewayError> {
        let (tenant, plan_tenant) = tenants(g.tenant, g.plan_tenant, "place_gtt_stop")?;
        let fraction = check_fraction(g.limit_fraction)?;
        let symbol = g.symbol.as_str();
        let (qty, trigger, last_price) = (g.qty, g.trigger, g.last_price);
        if let Some(mismatch) = refuse_cross_tenant(tenant, plan_tenant) {
            return Ok(blocked(symbol, &mismatch));
        }
        let gates = (self.gates)();
        let cid = self.fresh_cid(g.client_id.as_ref());
        if let Err(exc) = assert_tradeable(symbol, g.series.as_deref()) {
            // GTT layer 1: untouchables
            self.journal(
                dict([
                    ("event", s("gtt_untouchable_block")),
                    ("symbol", s(symbol)),
                    ("series", opt_s(g.series.as_deref())),
                    ("qty", PyValue::Int(qty)),
                    ("trigger", PyValue::Float(trigger)),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked(symbol, &exc.message));
        }
        let mut leg_product: Option<&str> = None;
        match &g.fo_plan {
            None => {
                if let Err(exc) = assert_not_overnight_option(symbol, &g.exchange, "CNC") {
                    self.journal(
                        dict([
                            ("event", s("gtt_overnight_option_block")),
                            ("symbol", s(symbol)),
                            ("exchange", s(&g.exchange)),
                            ("qty", PyValue::Int(qty)),
                            ("trigger", PyValue::Float(trigger)),
                        ]),
                        Some(&cid),
                    )?;
                    return Ok(blocked(symbol, &exc.message));
                }
                let gate_why = product_exchange_refusal(
                    "CNC",
                    &g.exchange,
                    gates.intraday_enabled,
                    gates.options_enabled,
                    false,
                    false,
                );
                if !gate_why.is_empty() {
                    return Ok(blocked(symbol, &gate_why));
                }
            }
            Some(fo_plan) => {
                let fo_why = fo_gtt_refusal(
                    symbol,
                    &g.exchange,
                    fo_plan,
                    gates.options_enabled,
                    gates.fno_carry_enabled,
                );
                if !fo_why.is_empty() {
                    self.journal(
                        dict([
                            ("event", s("gtt_fo_block")),
                            ("symbol", s(symbol)),
                            ("exchange", s(&g.exchange)),
                            ("qty", PyValue::Int(qty)),
                            ("trigger", PyValue::Float(trigger)),
                            ("fo_plan", s(&fo_plan.plan_id)),
                            ("why", s(&fo_why)),
                        ]),
                        Some(&cid),
                    )?;
                    return Ok(blocked(symbol, &fo_why));
                }
                leg_product = Some(FO_CARRY_PRODUCT);
            }
        }
        // GTT layer 1b: is this actually a stop?
        let refusal = refuse_stop(symbol, qty, trigger, last_price);
        if !refusal.is_empty() {
            self.journal(
                dict([
                    ("event", s("gtt_block")),
                    ("symbol", s(symbol)),
                    ("qty", PyValue::Int(qty)),
                    ("trigger", PyValue::Float(trigger)),
                    ("last_price", PyValue::Float(last_price)),
                    ("why", s(&refusal)),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked(symbol, &refusal));
        }
        // GTT layer 2: risk — consulted, recorded, deliberately NOT obeyed: the day the loss
        // cap trips is the day an unstopped book is most dangerous, and a GTT is not an order.
        let killed = self.risk.kill_switch_reason().await.map_err(risk_err)?;
        if !killed.is_empty() {
            self.journal(
                dict([
                    ("event", s("gtt_risk_note")),
                    ("symbol", s(symbol)),
                    ("qty", PyValue::Int(qty)),
                    ("trigger", PyValue::Float(trigger)),
                    ("why", s(&killed)),
                    (
                        "note",
                        s("kill switch is live; a protective stop is still armed"),
                    ),
                ]),
                Some(&cid),
            )?;
        }
        let finding = band_finding(trigger, last_price, self.stop_band);
        if !finding.is_empty() {
            self.journal(
                dict([
                    ("event", s("gtt_band_warning")),
                    ("symbol", s(symbol)),
                    ("finding", s(finding)),
                    ("drop_pct", PyValue::Float(drop_pct(trigger, last_price))),
                    (
                        "band",
                        PyValue::List(vec![
                            PyValue::Float(self.stop_band.min_pct()),
                            PyValue::Float(self.stop_band.max_pct()),
                        ]),
                    ),
                ]),
                Some(&cid),
            )?;
        }
        {
            // GTT layer 3: idempotency — two triggers on one position sell twice what is held.
            let mut gtt_sent = self.gtt_sent.lock().await;
            if let Some(existing) = gtt_sent.get(&cid) {
                return Ok(dict([
                    ("symbol", s(symbol)),
                    ("status", s("DUPLICATE")),
                    ("gtt_id", existing.clone()),
                ]));
            }
            gtt_sent.insert(cid.clone(), PyValue::Str(format!("PENDING-{cid}")));
        }
        self.limits.api_slot().await; // GTT layer 4: rate limits
        if gates.dry_run {
            // The UNSNAPPED trigger: snapping needs the instrument dump, a network call a dry
            // run must not make.
            let mut rec = dict([
                ("event", s("gtt_dry_run")),
                ("symbol", s(symbol)),
                ("qty", PyValue::Int(qty)),
                ("trigger", PyValue::Float(trigger)),
                ("last_price", PyValue::Float(last_price)),
                ("exchange", s(&g.exchange)),
                ("limit_fraction", PyValue::Float(fraction)),
            ]);
            if let Some(fo_plan) = &g.fo_plan {
                rec.insert("fo_plan", s(&fo_plan.plan_id));
                rec.insert("product", opt_s(leg_product));
            }
            self.journal(rec, Some(&cid))?;
            self.gtt_sent
                .lock()
                .await
                .insert(cid.clone(), PyValue::Str(format!("DRY-{cid}")));
            return Ok(dict([
                ("symbol", s(symbol)),
                ("status", s(gtt_status::DRY_RUN_GTT)),
                ("trigger", PyValue::Float(trigger)),
                ("qty", PyValue::Int(qty)),
            ]));
        }
        // ---- the live transport ----
        let (tick, trig, limit) = match self.tick_size(symbol, &g.exchange).await {
            Ok(tick) => {
                let trig = to_tick(trigger, tick);
                (tick, trig, to_tick(trig * fraction, tick))
            }
            Err((name, text)) => {
                self.journal(
                    dict([
                        ("event", s("gtt_error")),
                        ("symbol", s(symbol)),
                        ("stage", s("tick_size")),
                        ("error", s(&text)),
                        ("exception", s(&name)),
                    ]),
                    Some(&cid),
                )?;
                return Ok(dict([
                    ("symbol", s(symbol)),
                    ("status", s(gtt_status::GTT_ERROR)),
                    ("error", s(&text)),
                ]));
            }
        };
        // Re-checked after snapping: a coarse tick can round a trigger UP through the price.
        let snapped_refusal = refuse_stop(symbol, qty, trig, last_price);
        if !snapped_refusal.is_empty() {
            self.journal(
                dict([
                    ("event", s("gtt_block")),
                    ("symbol", s(symbol)),
                    ("qty", PyValue::Int(qty)),
                    ("trigger", PyValue::Float(trig)),
                    ("requested_trigger", PyValue::Float(trigger)),
                    ("tick", PyValue::Float(tick)),
                    ("last_price", PyValue::Float(last_price)),
                    ("stage", s("after_tick_snap")),
                    ("why", s(&snapped_refusal)),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked(symbol, &snapped_refusal));
        }
        let params = gtt_params(
            symbol,
            &g.exchange,
            qty,
            trig,
            limit,
            last_price,
            leg_product,
        );
        // ONLY the broker call is inside the error arm: once it returns, the trigger exists.
        let gid = match self.kite.place_gtt(&params).await {
            Ok(gid) => gid,
            Err(exc) => {
                let (name, text) = (exc.python_type().to_owned(), exc.to_string());
                self.journal(
                    dict([
                        ("event", s("gtt_error")),
                        ("symbol", s(symbol)),
                        ("stage", s("place_gtt")),
                        ("error", s(&text)),
                        ("exception", s(&name)),
                        ("trigger", PyValue::Float(trig)),
                        ("qty", PyValue::Int(qty)),
                    ]),
                    Some(&cid),
                )?;
                return Ok(dict([
                    ("symbol", s(symbol)),
                    ("status", s(gtt_status::GTT_ERROR)),
                    ("error", s(&text)),
                    ("exception", s(&name)),
                    ("reached_exchange", PyValue::None),
                ]));
            }
        };
        let trigger_id = match &gid {
            PyValue::Map(m) => m.get("trigger_id").cloned().unwrap_or(PyValue::None),
            _ => PyValue::None,
        };
        self.gtt_sent
            .lock()
            .await
            .insert(cid.clone(), trigger_id.clone());
        let mut rec = dict([
            ("event", s("gtt_placed")),
            ("symbol", s(symbol)),
            ("gtt_id", trigger_id.clone()),
            ("qty", PyValue::Int(qty)),
            ("trigger", PyValue::Float(trig)),
            ("limit", PyValue::Float(limit)),
            ("limit_fraction", PyValue::Float(fraction)),
            ("last_price", PyValue::Float(last_price)),
            ("exchange", s(&g.exchange)),
            ("drop_pct", PyValue::Float(drop_pct(trig, last_price))),
        ]);
        if trigger_id == PyValue::None {
            rec.insert(
                "warning",
                s(
                    "the broker named no trigger_id; the GTT exists but cannot be addressed from \
                   this record",
                ),
            );
        }
        self.journal(rec, Some(&cid))?;
        Ok(dict([
            ("symbol", s(symbol)),
            ("status", s(gtt_status::GTT_PLACED)),
            ("gtt_id", trigger_id),
            ("trigger", PyValue::Float(trig)),
            ("limit", PyValue::Float(limit)),
        ]))
    }

    /// `delete_gtt(...)` with the trigger id as it arrived (`int`, or a malformed value the
    /// gateway refuses). The ONLY way to destroy a GTT.
    ///
    /// # Errors
    /// A missing tenant; the journal; the risk store.
    #[allow(clippy::too_many_lines)]
    pub async fn delete_gtt_value(
        &self,
        r: &DeleteGtt,
        gtt_id: &PyValue,
    ) -> Result<PyDict, GatewayError> {
        let symbol = r.symbol.as_str();
        let cid = r.client_id.as_deref();
        let (tenant, plan_tenant) = tenants(r.tenant, r.plan_tenant, "delete_gtt")?;
        let out = |status: &str, error: Option<&str>| {
            let mut d = dict([
                ("symbol", s(symbol)),
                ("gtt_id", gtt_id.clone()),
                ("status", s(status)),
            ]);
            if let Some(e) = error {
                d.insert("error", s(e));
            }
            d
        };
        if let Some(mismatch) = refuse_cross_tenant(tenant, plan_tenant) {
            return Ok(out("BLOCKED", Some(&mismatch)));
        }
        let gates = (self.gates)();
        if guards::py_strip(symbol).is_empty() {
            let why = "a GTT cannot be cancelled without naming its instrument: the untouchable \
                       guard has nothing to check";
            self.journal(
                dict([
                    ("event", s("gtt_delete_block")),
                    ("gtt_id", gtt_id.clone()),
                    ("why", s(why)),
                ]),
                cid,
            )?;
            return Ok(out("BLOCKED", Some(why)));
        }
        if let Err(exc) = assert_tradeable(symbol, r.series.as_deref()) {
            // GTT layer 1: untouchables
            self.journal(
                dict([
                    ("event", s("gtt_delete_untouchable_block")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                ]),
                cid,
            )?;
            return Ok(out("BLOCKED", Some(&exc.message)));
        }
        let Some(trigger) = trigger_id(gtt_id) else {
            let why = format!("{symbol}: {} is not a GTT trigger id", py_repr(gtt_id));
            self.journal(
                dict([
                    ("event", s("gtt_delete_block")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("why", s(&why)),
                ]),
                cid,
            )?;
            return Ok(out("BLOCKED", Some(&why)));
        };
        // GTT layer 2: risk — cancelling removes protection, so the kill switch refuses it.
        let killed = self.risk.kill_switch_reason().await.map_err(risk_err)?;
        if !killed.is_empty() {
            let why = format!("{killed} — a stop is not removed while the kill switch is live");
            self.journal(
                dict([
                    ("event", s("gtt_delete_risk_block")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("why", s(&why)),
                ]),
                cid,
            )?;
            return Ok(out("RISK_BLOCKED", Some(&why)));
        }
        self.limits.api_slot().await; // GTT layer 4: rate limits
        if gates.dry_run {
            self.journal(
                dict([
                    ("event", s("gtt_dry_run_delete")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("exchange", s(&r.exchange)),
                ]),
                cid,
            )?;
            return Ok(out(gtt_status::DRY_RUN_GTT_DELETE, None));
        }
        match self.kite.delete_gtt(trigger).await {
            Ok(_) => {
                self.journal(
                    dict([
                        ("event", s("gtt_deleted")),
                        ("symbol", s(symbol)),
                        ("gtt_id", gtt_id.clone()),
                        ("exchange", s(&r.exchange)),
                    ]),
                    cid,
                )?;
                Ok(out(gtt_status::GTT_DELETED, None))
            }
            Err(exc) => {
                let text = exc.to_string();
                self.journal(
                    dict([
                        ("event", s("gtt_delete_error")),
                        ("symbol", s(symbol)),
                        ("gtt_id", gtt_id.clone()),
                        ("error", s(&text)),
                        ("exception", s(exc.python_type())),
                    ]),
                    cid,
                )?;
                Ok(out(gtt_status::GTT_DELETE_ERROR, Some(&text)))
            }
        }
    }

    /// `modify_gtt_quantity(...)` with the trigger id as it arrived. The ONLY way to modify a
    /// GTT; with a `fo_plan` the F2 trail moves the trigger and is never lowered.
    ///
    /// # Errors
    /// `ValueError` for a bad `limit_fraction`; a missing tenant; the journal; the risk store.
    #[allow(clippy::too_many_lines)]
    pub async fn modify_gtt_value(
        &self,
        r: &ModifyGtt,
        gtt_id: &PyValue,
    ) -> Result<PyDict, GatewayError> {
        let (tenant, plan_tenant) = tenants(r.tenant, r.plan_tenant, "modify_gtt_quantity")?;
        let fraction = check_fraction(r.limit_fraction)?;
        let symbol = r.symbol.as_str();
        let (qty, trigger, last_price) = (r.qty, r.trigger, r.last_price);
        let blocked_m = |why: &str| {
            dict([
                ("symbol", s(symbol)),
                ("gtt_id", gtt_id.clone()),
                ("status", s("BLOCKED")),
                ("error", s(why)),
            ])
        };
        if let Some(mismatch) = refuse_cross_tenant(tenant, plan_tenant) {
            return Ok(blocked_m(&mismatch));
        }
        let gates = (self.gates)();
        let cid = self.fresh_cid(r.client_id.as_ref());
        if let Err(exc) = assert_tradeable(symbol, r.series.as_deref()) {
            // GTT layer 1: untouchables
            self.journal(
                dict([
                    ("event", s("gtt_modify_untouchable_block")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked_m(&exc.message));
        }
        let Some(trigger_no) = trigger_id(gtt_id) else {
            let why = format!("{symbol}: {} is not a GTT trigger id", py_repr(gtt_id));
            self.journal(
                dict([
                    ("event", s("gtt_modify_block")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("why", s(&why)),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked_m(&why));
        };
        if qty <= 0 {
            let why = format!(
                "{symbol}: a GTT for {qty} shares is not a stop; cancel it with delete_gtt instead"
            );
            self.journal(
                dict([
                    ("event", s("gtt_modify_block")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("qty", PyValue::Int(qty)),
                    ("why", s(&why)),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked_m(&why));
        }
        let mut leg_product: Option<&str> = None;
        if let Some(fo_plan) = &r.fo_plan {
            let mut fo_why = fo_gtt_refusal(
                symbol,
                &r.exchange,
                fo_plan,
                gates.options_enabled,
                gates.fno_carry_enabled,
            );
            if fo_why.is_empty() && r.floor_trigger.is_none() {
                fo_why = format!(
                    "{symbol}: an FO stop modify must name the trigger it replaces; a trail is \
                     never lowered (docs/fno/04 §10)"
                );
            }
            if let Some(floor) = r.floor_trigger
                && fo_why.is_empty()
                && trigger < floor
            {
                fo_why = format!(
                    "{symbol}: trigger {} is below the resting {}; a trail is never lowered \
                     (docs/fno/04 §10)",
                    baskfy_core::py::float_repr(trigger),
                    baskfy_core::py::float_repr(floor)
                );
            }
            if !fo_why.is_empty() {
                self.journal(
                    dict([
                        ("event", s("gtt_modify_fo_block")),
                        ("symbol", s(symbol)),
                        ("gtt_id", gtt_id.clone()),
                        ("exchange", s(&r.exchange)),
                        ("qty", PyValue::Int(qty)),
                        ("trigger", PyValue::Float(trigger)),
                        ("floor_trigger", opt_f(r.floor_trigger)),
                        ("fo_plan", s(&fo_plan.plan_id)),
                        ("why", s(&fo_why)),
                    ]),
                    Some(&cid),
                )?;
                return Ok(blocked_m(&fo_why));
            }
            leg_product = Some(FO_CARRY_PRODUCT);
        }
        let refusal = refuse_stop(symbol, qty, trigger, last_price);
        if !refusal.is_empty() {
            // GTT layer 1b: is it a stop?
            self.journal(
                dict([
                    ("event", s("gtt_modify_block")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("qty", PyValue::Int(qty)),
                    ("trigger", PyValue::Float(trigger)),
                    ("last_price", PyValue::Float(last_price)),
                    ("why", s(&refusal)),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked_m(&refusal));
        }
        let killed = self.risk.kill_switch_reason().await.map_err(risk_err)?; // GTT layer 2
        if !killed.is_empty() {
            self.journal(
                dict([
                    ("event", s("gtt_risk_note")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("qty", PyValue::Int(qty)),
                    ("trigger", PyValue::Float(trigger)),
                    ("why", s(&killed)),
                    (
                        "note",
                        s("kill switch is live; a protective stop is still re-sized"),
                    ),
                ]),
                Some(&cid),
            )?;
        }
        self.limits.api_slot().await; // GTT layer 4: rate limits
        if gates.dry_run {
            let mut rec = dict([
                ("event", s("gtt_dry_run_modify")),
                ("symbol", s(symbol)),
                ("gtt_id", gtt_id.clone()),
                ("qty", PyValue::Int(qty)),
                ("trigger", PyValue::Float(trigger)),
                ("last_price", PyValue::Float(last_price)),
                ("exchange", s(&r.exchange)),
                ("limit_fraction", PyValue::Float(fraction)),
            ]);
            if let Some(fo_plan) = &r.fo_plan {
                rec.insert("fo_plan", s(&fo_plan.plan_id));
                rec.insert("product", opt_s(leg_product));
                rec.insert("floor_trigger", opt_f(r.floor_trigger));
            }
            self.journal(rec, Some(&cid))?;
            return Ok(dict([
                ("symbol", s(symbol)),
                ("gtt_id", gtt_id.clone()),
                ("status", s(gtt_status::DRY_RUN_GTT_MODIFY)),
                ("trigger", PyValue::Float(trigger)),
                ("qty", PyValue::Int(qty)),
            ]));
        }
        // ---- the live transport ----
        let (tick, trig, limit) = match self.tick_size(symbol, &r.exchange).await {
            Ok(tick) => {
                let trig = to_tick(trigger, tick);
                (tick, trig, to_tick(trig * fraction, tick))
            }
            Err((name, text)) => {
                self.journal(
                    dict([
                        ("event", s("gtt_modify_error")),
                        ("symbol", s(symbol)),
                        ("gtt_id", gtt_id.clone()),
                        ("stage", s("tick_size")),
                        ("error", s(&text)),
                        ("exception", s(&name)),
                    ]),
                    Some(&cid),
                )?;
                return Ok(dict([
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("status", s(gtt_status::GTT_MODIFY_ERROR)),
                    ("error", s(&text)),
                ]));
            }
        };
        let snapped_refusal = refuse_stop(symbol, qty, trig, last_price);
        if !snapped_refusal.is_empty() {
            self.journal(
                dict([
                    ("event", s("gtt_modify_block")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("qty", PyValue::Int(qty)),
                    ("trigger", PyValue::Float(trig)),
                    ("requested_trigger", PyValue::Float(trigger)),
                    ("tick", PyValue::Float(tick)),
                    ("last_price", PyValue::Float(last_price)),
                    ("stage", s("after_tick_snap")),
                    ("why", s(&snapped_refusal)),
                ]),
                Some(&cid),
            )?;
            return Ok(blocked_m(&snapped_refusal));
        }
        let params = gtt_params(
            symbol,
            &r.exchange,
            qty,
            trig,
            limit,
            last_price,
            leg_product,
        );
        if let Err(exc) = self.kite.modify_gtt(trigger_no, &params).await {
            let (name, text) = (exc.python_type().to_owned(), exc.to_string());
            self.journal(
                dict([
                    ("event", s("gtt_modify_error")),
                    ("symbol", s(symbol)),
                    ("gtt_id", gtt_id.clone()),
                    ("stage", s("modify_gtt")),
                    ("error", s(&text)),
                    ("exception", s(&name)),
                    ("trigger", PyValue::Float(trig)),
                    ("qty", PyValue::Int(qty)),
                ]),
                Some(&cid),
            )?;
            return Ok(dict([
                ("symbol", s(symbol)),
                ("gtt_id", gtt_id.clone()),
                ("status", s(gtt_status::GTT_MODIFY_ERROR)),
                ("error", s(&text)),
                ("exception", s(&name)),
                ("reached_exchange", PyValue::None),
            ]));
        }
        self.journal(
            dict([
                ("event", s("gtt_modified")),
                ("symbol", s(symbol)),
                ("gtt_id", gtt_id.clone()),
                ("qty", PyValue::Int(qty)),
                ("trigger", PyValue::Float(trig)),
                ("limit", PyValue::Float(limit)),
                ("limit_fraction", PyValue::Float(fraction)),
                ("last_price", PyValue::Float(last_price)),
                ("exchange", s(&r.exchange)),
            ]),
            Some(&cid),
        )?;
        Ok(dict([
            ("symbol", s(symbol)),
            ("gtt_id", gtt_id.clone()),
            ("status", s(gtt_status::GTT_MODIFIED)),
            ("trigger", PyValue::Float(trig)),
            ("limit", PyValue::Float(limit)),
            ("qty", PyValue::Int(qty)),
        ]))
    }

    /// `cancel_order(...)` with the order id as it arrived. The ONLY way to cancel an order;
    /// the kill switch does not refuse it (a cancel reduces exposure).
    ///
    /// # Errors
    /// A missing tenant; the journal.
    pub async fn cancel_order_value(
        &self,
        r: &CancelOrder,
        order_id: &PyValue,
    ) -> Result<PyDict, GatewayError> {
        let symbol = r.symbol.as_str();
        let cid = r.client_id.as_deref();
        let (tenant, plan_tenant) = tenants(r.tenant, r.plan_tenant, "cancel_order")?;
        let out = |status: &str, error: Option<&str>| {
            let mut d = dict([
                ("symbol", s(symbol)),
                ("order_id", order_id.clone()),
                ("status", s(status)),
            ]);
            if let Some(e) = error {
                d.insert("error", s(e));
            }
            d
        };
        if let Some(mismatch) = refuse_cross_tenant(tenant, plan_tenant) {
            return Ok(out("BLOCKED", Some(&mismatch)));
        }
        let gates = (self.gates)();
        if guards::py_strip(symbol).is_empty() {
            let why = "an order cannot be cancelled without naming its instrument: the \
                       untouchable guard has nothing to check";
            self.journal(
                dict([
                    ("event", s("order_cancel_block")),
                    ("order_id", order_id.clone()),
                    ("why", s(why)),
                ]),
                cid,
            )?;
            return Ok(out("BLOCKED", Some(why)));
        }
        if let Err(exc) = assert_tradeable(symbol, r.series.as_deref()) {
            // layer 1: untouchables
            self.journal(
                dict([
                    ("event", s("order_cancel_untouchable_block")),
                    ("symbol", s(symbol)),
                    ("order_id", order_id.clone()),
                ]),
                cid,
            )?;
            return Ok(out("BLOCKED", Some(&exc.message)));
        }
        let oid_text = if order_id.truthy() {
            py_str(order_id)
        } else {
            String::new()
        };
        if guards::py_strip(&oid_text).is_empty() {
            let why = format!("{symbol}: {} is not an order id", py_repr(order_id));
            self.journal(
                dict([
                    ("event", s("order_cancel_block")),
                    ("symbol", s(symbol)),
                    ("order_id", order_id.clone()),
                    ("why", s(&why)),
                ]),
                cid,
            )?;
            return Ok(out("BLOCKED", Some(&why)));
        }
        self.limits.api_slot().await; // layer 4: rate limits
        if gates.dry_run {
            self.journal(
                dict([
                    ("event", s("order_cancel_dry_run")),
                    ("symbol", s(symbol)),
                    ("order_id", order_id.clone()),
                    ("variety", s(&r.variety)),
                ]),
                cid,
            )?;
            return Ok(out(gtt_status::ORDER_CANCEL_DRY_RUN, None));
        }
        match self.kite.cancel_order(&r.variety, &oid_text, None).await {
            Ok(_) => {
                self.journal(
                    dict([
                        ("event", s("order_cancelled")),
                        ("symbol", s(symbol)),
                        ("order_id", order_id.clone()),
                        ("variety", s(&r.variety)),
                    ]),
                    cid,
                )?;
                Ok(out(gtt_status::ORDER_CANCELLED, None))
            }
            Err(exc) => {
                let text = exc.to_string();
                self.journal(
                    dict([
                        ("event", s("order_cancel_error")),
                        ("symbol", s(symbol)),
                        ("order_id", order_id.clone()),
                        ("error", s(&text)),
                        ("exception", s(exc.python_type())),
                    ]),
                    cid,
                )?;
                Ok(out(gtt_status::ORDER_CANCEL_ERROR, Some(&text)))
            }
        }
    }
}

fn blocked(symbol: &str, why: &str) -> PyDict {
    dict([
        ("symbol", s(symbol)),
        ("status", s("BLOCKED")),
        ("error", s(why)),
    ])
}
