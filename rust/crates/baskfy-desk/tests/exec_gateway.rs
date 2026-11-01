//! Lane **exec_gateway**: the gateway's non-negotiables, ported test-first from
//! `decile-blueprint/packages/execution/tests/{test_non_negotiables,test_client_id_journal,
//! test_tenant_isolation,test_derivative_product_gate}.py` (the gateway halves) and the desk's
//! `app/core/gateway.py` shim (`test_execute_gateway.py`'s gateway-level assertions).
//!
//! Every test runs with the network blocked: the broker is the replay Kite, which records every
//! call and sends nothing. A refused order must leave its call log EMPTY (Python's
//! `ExplodingKC`), and `DRY_RUN` must never reach it at all.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::Path;
use std::sync::Arc;

use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{
    DeleteGtt, GatesFn, Gateway, GttStop, PlaceOrder, ProductGates, StopBand, TenantIds,
    mint_client_id, parse_client_id,
};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::kite::KiteWrites;
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskLayer, RiskManager};
use baskfy_desk::wire::{PyDict, PyValue};
use chrono::TimeZone;

const FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;

fn tenant() -> TenantIds {
    TenantIds::new(1, 1).unwrap()
}

fn clock() -> Arc<dyn Clock> {
    Arc::new(FixedClock::new(
        ist().with_ymd_and_hms(2026, 10, 30, 10, 0, 0).unwrap(),
    ))
}

struct Bed {
    dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
}

impl Bed {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("kite.json"), FIXTURE).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        std::fs::write(
            dir.path().join("instruments/NSE.csv"),
            "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n738561,2885,RELIANCE,RELIANCE,0,,0,0.05,1,EQ,NSE,NSE\n",
        )
        .unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        Self { dir, kite }
    }

    fn journal(&self) -> std::path::PathBuf {
        self.dir.path().join("journal.jsonl")
    }

    async fn gateway_with(&self, gates: ProductGates, risk: Arc<dyn RiskLayer>) -> OrderGateway {
        let gates: GatesFn = Arc::new(move || gates);
        let kite: Arc<dyn KiteWrites> = self.kite.clone();
        OrderGateway::new(
            kite,
            risk,
            GatewayOptions {
                gates,
                journal_path: self.journal(),
                stop_band: None,
                coverage: None,
                clock: clock(),
                ids: Arc::new(HarnessIds::new("exec-gateway-test")),
                limits: None,
            },
        )
        .unwrap()
    }

    async fn gateway(&self, gates: ProductGates) -> OrderGateway {
        self.gateway_with(gates, manager(RiskConfig::default()).await)
            .await
    }

    fn sent(&self) -> usize {
        self.kite.write_calls().len()
    }

    fn lines(&self) -> Vec<serde_json::Value> {
        lines(&self.journal())
    }
}

fn lines(path: &Path) -> Vec<serde_json::Value> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

async fn manager(cfg: RiskConfig) -> Arc<dyn RiskLayer> {
    Arc::new(
        RiskManager::new(cfg, clock(), RiskBacking::Memory)
            .await
            .unwrap(),
    )
}

fn gates(dry_run: bool, intraday: bool, options: bool) -> ProductGates {
    ProductGates {
        dry_run,
        intraday_enabled: intraday,
        options_enabled: options,
        fno_carry_enabled: false,
    }
}

fn buy(symbol: &str) -> PlaceOrder {
    PlaceOrder {
        symbol: symbol.to_owned(),
        qty: 1,
        side: "BUY".to_owned(),
        price: Some(100.0),
        gross_exposure: Some(100.0),
        tenant: Some(tenant()),
        plan_tenant: Some(tenant()),
        ..PlaceOrder::default()
    }
}

fn stop(symbol: &str, qty: i64, trigger: f64, last: f64) -> GttStop {
    GttStop {
        symbol: symbol.to_owned(),
        qty,
        trigger,
        last_price: last,
        exchange: "NSE".to_owned(),
        series: None,
        client_id: None,
        tenant: Some(tenant()),
        plan_tenant: Some(tenant()),
        limit_fraction: None,
        fo_plan: None,
    }
}

fn delete(symbol: &str, client_id: Option<&str>) -> DeleteGtt {
    DeleteGtt {
        gtt_id: 1,
        symbol: symbol.to_owned(),
        exchange: "NSE".to_owned(),
        series: None,
        client_id: client_id.map(str::to_owned),
        tenant: Some(tenant()),
        plan_tenant: Some(tenant()),
    }
}

fn status(out: &PyDict) -> String {
    match out.get("status") {
        Some(PyValue::Str(s)) => s.clone(),
        other => panic!("no status: {other:?}"),
    }
}

fn text(out: &PyDict, key: &str) -> String {
    match out.get(key) {
        Some(PyValue::Str(s)) => s.clone(),
        Some(PyValue::None) | None => String::new(),
        Some(other) => format!("{other:?}"),
    }
}

// ================================================================================
// 1. Never auto-execute — DRY_RUN simulates end to end and places nothing.
// ================================================================================

#[tokio::test]
async fn non_negotiable_1_dry_run_simulates_an_order_end_to_end() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let out = gw.place(&buy("RELIANCE")).await.unwrap();
    assert_eq!(status(&out), "DRY_RUN");
    assert!(text(&out, "order_id").starts_with("DRY-"));
    assert_eq!(bed.sent(), 0, "DRY_RUN reached the broker");
}

#[tokio::test]
async fn non_negotiable_1b_dry_run_simulates_a_gtt_end_to_end() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let out = gw
        .place_gtt_stop(&stop("RELIANCE", 10, 89.0, 100.0))
        .await
        .unwrap();
    assert_eq!(status(&out), "DRY_RUN_GTT");
    assert_eq!(out.get("trigger"), Some(&PyValue::Float(89.0)));
    assert_eq!(bed.sent(), 0);
}

#[tokio::test]
async fn non_negotiable_1c_dry_run_simulates_a_cancellation_too() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let out = gw
        .delete_gtt_value(&delete("RELIANCE", None), &PyValue::Int(1))
        .await
        .unwrap();
    assert_eq!(status(&out), "DRY_RUN_GTT_DELETE");
    assert_eq!(bed.sent(), 0);
}

// ================================================================================
// 4. Every buy gets a GTT stop, vol-scaled 8-12%.
// ================================================================================

#[test]
fn non_negotiable_4_the_stop_band_is_eight_to_twelve_percent() {
    let band = StopBand::default();
    assert_eq!(band, StopBand::new(0.08, 0.12).unwrap());
}

#[tokio::test]
async fn non_negotiable_4b_a_stop_can_be_armed_through_the_gateway() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let out = gw
        .place_gtt_stop(&stop("RELIANCE", 10, 89.0, 100.0))
        .await
        .unwrap();
    assert!(["GTT_PLACED", "DRY_RUN_GTT"].contains(&status(&out).as_str()));
}

// ================================================================================
// 5. Product gates: CNC only unless explicitly enabled, enforced INSIDE the gateway.
// ================================================================================

#[tokio::test]
async fn non_negotiable_5_product_gates_block_inside_the_gateway() {
    let cases: [(&str, &str, ProductGates, &str); 5] = [
        (
            "MIS",
            "NSE",
            gates(false, false, false),
            "MIS/intraday disabled",
        ),
        (
            "NRML",
            "NFO",
            gates(false, false, false),
            "F&O/derivatives disabled",
        ),
        (
            "NRML",
            "BFO",
            gates(false, false, false),
            "F&O/derivatives disabled",
        ),
        (
            "NRML",
            "MCX",
            gates(false, false, false),
            "F&O/derivatives disabled",
        ),
        (
            "NRML",
            "NSE",
            gates(false, false, false),
            "only CNC is allowed",
        ),
    ];
    for (product, exchange, g, why) in cases {
        let bed = Bed::new();
        let gw = bed.gateway(g).await;
        let order = PlaceOrder {
            product: product.to_owned(),
            exchange: exchange.to_owned(),
            ..buy("RELIANCE")
        };
        let out = gw.place(&order).await.unwrap();
        assert_eq!(status(&out), "BLOCKED", "{product} on {exchange}");
        assert!(
            text(&out, "error").contains(why),
            "{product}/{exchange}: {out:?}"
        );
        assert_eq!(bed.sent(), 0, "{product}/{exchange} reached the broker");
    }
}

#[tokio::test]
async fn non_negotiable_5b_a_gtt_is_gated_on_the_same_switch() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(false, false, false)).await;
    let mut fut = stop("NIFTY25SEPFUT", 50, 89.0, 100.0);
    fut.exchange = "NFO".to_owned();
    let out = gw.place_gtt_stop(&fut).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("F&O/derivatives disabled"));
    let mut opt = stop("NIFTY25SEP24000CE", 50, 89.0, 100.0);
    opt.exchange = "NFO".to_owned();
    let out = gw.place_gtt_stop(&opt).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("past the close"));
    assert_eq!(bed.sent(), 0);
}

#[test]
fn non_negotiable_5c_the_gates_fail_closed_when_nobody_supplies_them() {
    let d = ProductGates::default();
    assert!(d.dry_run);
    assert!(!d.intraday_enabled);
    assert!(!d.options_enabled);
    assert!(!d.fno_carry_enabled);
}

#[tokio::test]
async fn derivative_mis_option_needs_intraday_too() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(false, false, true)).await;
    let order = PlaceOrder {
        symbol: "NIFTY2692223700CE".to_owned(),
        product: "MIS".to_owned(),
        exchange: "NFO".to_owned(),
        ..buy("NIFTY2692223700CE")
    };
    let out = gw.place(&order).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("INTRADAY_ENABLED"), "{out:?}");
    assert_eq!(bed.sent(), 0);
}

#[tokio::test]
async fn derivative_nrml_future_is_blocked_with_both_switches_on() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(false, true, true)).await;
    let order = PlaceOrder {
        product: "NRML".to_owned(),
        exchange: "NFO".to_owned(),
        ..buy("NIFTY26SEPFUT")
    };
    let out = gw.place(&order).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("MIS"));
    assert_eq!(bed.sent(), 0);
}

#[tokio::test]
async fn derivative_nrml_option_is_refused_by_the_overnight_guard_first() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(false, true, true)).await;
    let order = PlaceOrder {
        product: "NRML".to_owned(),
        exchange: "NFO".to_owned(),
        ..buy("NIFTY2692223700CE")
    };
    let out = gw.place(&order).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("past the close"));
    assert_eq!(bed.sent(), 0);
}

#[tokio::test]
async fn derivative_mis_option_with_both_switches_reaches_only_the_dry_run_branch() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, true, true)).await;
    let order = PlaceOrder {
        product: "MIS".to_owned(),
        exchange: "NFO".to_owned(),
        ..buy("NIFTY2692223700CE")
    };
    let out = gw.place(&order).await.unwrap();
    assert_ne!(status(&out), "BLOCKED", "{out:?}");
    assert_eq!(bed.sent(), 0);
}

#[tokio::test]
async fn derivative_gtt_is_refused_whatever_the_switches() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(false, true, true)).await;
    let mut fut = stop("NIFTY26SEPFUT", 65, 89.0, 100.0);
    fut.exchange = "NFO".to_owned();
    let out = gw.place_gtt_stop(&fut).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert_eq!(bed.sent(), 0);
}

// ================================================================================
// 6. All order flow goes through the gateway, in layer order; client_id = plan_id:symbol.
// ================================================================================

fn gateway_source() -> String {
    std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes/exec_core/order_gateway.rs"),
    )
    .unwrap()
}

fn in_order(src: &str, marks: &[&str]) {
    let body = src.split_once("impl OrderGateway").map_or(src, |(_, b)| b);
    let positions: Vec<usize> = marks
        .iter()
        .map(|m| body.find(m).unwrap_or_else(|| panic!("{m} is missing")))
        .collect();
    let mut sorted = positions.clone();
    sorted.sort_unstable();
    assert_eq!(positions, sorted, "layers out of order: {marks:?}");
}

#[test]
fn non_negotiable_6_the_four_layers_are_in_order() {
    in_order(
        &gateway_source(),
        &[
            "// layer 1: untouchables",
            "// layer 2: risk",
            "// layer 3: idempotency",
            "// layer 4: rate limits",
        ],
    );
}

#[test]
fn non_negotiable_6b_the_gtt_path_has_the_same_four_layers() {
    in_order(
        &gateway_source(),
        &[
            "// GTT layer 1: untouchables",
            "// GTT layer 2: risk",
            "// GTT layer 3: idempotency",
            "// GTT layer 4: rate limits",
        ],
    );
}

/// `test_non_negotiable_6c`: within the gateway lane, the broker's order verbs are called from
/// `order_gateway.rs` and nowhere else (the weekly `/stops/arm` exception lives in
/// `kite_client.rs`, documented by non-negotiable 6's caveat).
#[test]
fn non_negotiable_6c_nothing_else_in_the_lane_reaches_an_order_verb() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes/exec_core");
    let verbs = [".place_order(", ".modify_order(", ".cancel_order("];
    let mut offenders = Vec::new();
    for entry in std::fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path();
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        if name == "order_gateway.rs" || path.is_dir() {
            continue;
        }
        let code = std::fs::read_to_string(&path).unwrap();
        for (n, line) in code.lines().enumerate() {
            if line.trim_start().starts_with("//") {
                continue;
            }
            for v in verbs {
                // `Gateway::cancel_order(&self.shim, …)` is the gateway's own verb, not the broker's.
                if line.contains(v) && !line.contains("Gateway::") && !line.contains("fn ") {
                    offenders.push(format!("{name}:{} {v}", n + 1));
                }
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "{offenders:?} reach the broker outside the gateway"
    );
}

#[tokio::test]
async fn non_negotiable_6d_a_replayed_client_id_cannot_double_send() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let order = PlaceOrder {
        client_id: Some("PLAN1:RELIANCE".to_owned()),
        ..buy("RELIANCE")
    };
    assert_eq!(status(&gw.place(&order).await.unwrap()), "DRY_RUN");
    assert_eq!(status(&gw.place(&order).await.unwrap()), "DUPLICATE");
}

#[tokio::test]
async fn a_replayed_journal_still_refuses_the_second_send_after_a_restart() {
    let bed = Bed::new();
    let order = PlaceOrder {
        client_id: Some(mint_client_id("plan-1", "RELIANCE").unwrap()),
        ..buy("RELIANCE")
    };
    let first = bed.gateway(gates(true, false, false)).await;
    assert_eq!(status(&first.place(&order).await.unwrap()), "DRY_RUN");
    drop(first);
    let second = bed.gateway(gates(true, false, false)).await;
    assert_eq!(status(&second.place(&order).await.unwrap()), "DUPLICATE");
}

#[tokio::test]
async fn a_multi_leg_plan_sends_each_leg_once_and_replays_none_of_them() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    for round in ["DRY_RUN", "DUPLICATE"] {
        for symbol in ["RELIANCE", "INFY"] {
            let order = PlaceOrder {
                qty: 5,
                gross_exposure: Some(500.0),
                client_id: Some(mint_client_id("plan-1", symbol).unwrap()),
                ..buy(symbol)
            };
            assert_eq!(status(&gw.place(&order).await.unwrap()), round);
        }
    }
    let placements = bed
        .lines()
        .iter()
        .filter(|l| l["event"] == "dry_run")
        .count();
    assert_eq!(placements, 2);
}

// ================================================================================
// 7. Untouchable instruments, at the lowest layer — BLOCKED, never raised (AF 3.5).
// ================================================================================

#[tokio::test]
async fn non_negotiable_7_an_untouchable_is_refused_before_any_network_call() {
    for symbol in ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"] {
        let bed = Bed::new();
        let gw = bed.gateway(gates(false, false, false)).await;
        let order = PlaceOrder {
            side: "SELL".to_owned(),
            price: Some(7000.0),
            gross_exposure: Some(7000.0),
            ..buy(symbol)
        };
        let out = gw.place(&order).await.unwrap();
        assert_eq!(status(&out), "BLOCKED", "{symbol}");
        assert!(text(&out, "error").contains("protected instrument"));
        assert_eq!(bed.sent(), 0, "{symbol} reached the broker");
    }
}

#[tokio::test]
async fn non_negotiable_7b_an_untouchable_is_refused_a_gtt_in_both_directions() {
    for symbol in ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"] {
        let bed = Bed::new();
        let gw = bed.gateway(gates(false, false, false)).await;
        let out = gw
            .place_gtt_stop(&stop(symbol, 1, 6300.0, 7000.0))
            .await
            .unwrap();
        assert_eq!(status(&out), "BLOCKED");
        assert!(text(&out, "error").contains("protected instrument"));
        let out = gw
            .delete_gtt_value(&delete(symbol, None), &PyValue::Int(1))
            .await
            .unwrap();
        assert_eq!(status(&out), "BLOCKED");
        assert!(text(&out, "error").contains("protected instrument"));
        assert_eq!(bed.sent(), 0, "{symbol} reached the broker");
    }
}

// ================================================================================
// Risk values every order, including a MARKET order that carries no price of its own.
// ================================================================================

struct RecordingRisk(std::sync::Mutex<Vec<(String, f64, f64)>>);

#[async_trait::async_trait]
impl RiskLayer for RecordingRisk {
    async fn pre_order(
        &self,
        symbol: &str,
        value: f64,
        gross: f64,
        _side: &str,
    ) -> Result<(bool, String), baskfy_desk::lanes::exec_core::risk::RiskError> {
        self.0
            .lock()
            .unwrap()
            .push((symbol.to_owned(), value, gross));
        Ok((true, String::new()))
    }

    async fn kill_switch_reason(
        &self,
    ) -> Result<String, baskfy_desk::lanes::exec_core::risk::RiskError> {
        Ok(String::new())
    }
}

#[tokio::test]
async fn a_market_order_is_valued_for_risk_by_its_reference_price() {
    let bed = Bed::new();
    let risk = Arc::new(RecordingRisk(std::sync::Mutex::new(Vec::new())));
    let gw = bed
        .gateway_with(gates(true, false, false), risk.clone())
        .await;
    let order = PlaceOrder {
        qty: 100,
        order_type: "MARKET".to_owned(),
        price: None,
        reference_price: Some(250.0),
        market_protection: Some(0.5),
        gross_exposure: Some(0.0),
        ..buy("RELIANCE")
    };
    gw.place(&order).await.unwrap();
    assert_eq!(
        *risk.0.lock().unwrap(),
        vec![("RELIANCE".to_owned(), 25_000.0, 0.0)],
        "a MARKET order was valued at zero"
    );
}

#[tokio::test]
async fn an_order_with_no_price_and_no_reference_is_blocked_not_valued_at_zero() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let order = PlaceOrder {
        qty: 100,
        order_type: "MARKET".to_owned(),
        price: None,
        reference_price: None,
        gross_exposure: Some(0.0),
        ..buy("RELIANCE")
    };
    let out = gw.place(&order).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("risk check cannot value it"));
    let journal = std::fs::read_to_string(bed.journal()).unwrap();
    assert!(journal.contains("unpriced_order_block"));
}

#[tokio::test]
async fn market_protection_reaches_the_broker_only_on_a_market_order() {
    let bed = Bed::new();
    let market = PlaceOrder {
        order_type: "MARKET".to_owned(),
        price: None,
        reference_price: Some(100.0),
        market_protection: Some(0.44),
        client_id: Some("a".to_owned()),
        ..buy("RELIANCE")
    };
    let limit = PlaceOrder {
        market_protection: Some(0.44),
        client_id: Some("b".to_owned()),
        ..buy("RELIANCE")
    };
    bed.gateway(gates(false, false, false))
        .await
        .place(&market)
        .await
        .unwrap();
    bed.gateway(gates(false, false, false))
        .await
        .place(&limit)
        .await
        .unwrap();
    let calls = bed.kite.write_calls();
    assert_eq!(calls.len(), 2);
    let params = |i: usize| match &calls[i].params {
        Some(PyValue::Map(m)) => m.clone(),
        other => panic!("{other:?}"),
    };
    let p0 = params(0);
    assert_eq!(
        p0.get("order_type"),
        Some(&PyValue::Str("MARKET".to_owned()))
    );
    assert_eq!(p0.get("market_protection"), Some(&PyValue::Float(0.44)));
    assert!(p0.get("price").is_none(), "a MARKET order carries no price");
    let p1 = params(1);
    assert_eq!(
        p1.get("order_type"),
        Some(&PyValue::Str("LIMIT".to_owned()))
    );
    assert!(p1.get("market_protection").is_none());
}

// ================================================================================
// Tenant isolation (law 2's multi-tenant clause).
// ================================================================================

#[tokio::test]
async fn cross_tenant_order_is_blocked_not_raised_and_never_reaches_the_broker() {
    for dry_run in [true, false] {
        let bed = Bed::new();
        let gw = bed.gateway(gates(dry_run, false, false)).await;
        let order = PlaceOrder {
            tenant: Some(TenantIds::new(1, 10).unwrap()),
            plan_tenant: Some(TenantIds::new(2, 20).unwrap()),
            ..buy("RELIANCE")
        };
        let out = gw.place(&order).await.unwrap();
        assert_eq!(status(&out), "BLOCKED");
        assert!(text(&out, "error").contains("tenant mismatch"));
        assert!(matches!(out.get("order_id"), None | Some(PyValue::None)));
        assert_eq!(bed.sent(), 0);
    }
}

#[tokio::test]
async fn matching_tenant_still_dry_runs() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let order = PlaceOrder {
        tenant: Some(TenantIds::new(1, 10).unwrap()),
        plan_tenant: Some(TenantIds::new(1, 10).unwrap()),
        ..buy("RELIANCE")
    };
    assert_eq!(status(&gw.place(&order).await.unwrap()), "DRY_RUN");
}

// ================================================================================
// The journal: every line carries the client_id it was keyed on.
// ================================================================================

#[test]
fn structurally_every_journal_call_in_the_gateway_passes_a_client_id() {
    let src = gateway_source();
    let mut offenders = Vec::new();
    for (n, line) in src.lines().enumerate() {
        let t = line.trim_start();
        if t.starts_with("//") || !t.contains("self.journal(") {
            continue;
        }
        // The call spans lines; its arguments end at the next `)?` — a client id argument is
        // an expression, never left to a default (the helper has no default to leave it to).
        let window: String = src.lines().skip(n).take(40).collect::<Vec<_>>().join("\n");
        let call = window.split_once(")?").map_or(window.as_str(), |(c, _)| c);
        if !(call.contains("cid") || call.contains("client_id")) {
            offenders.push(n + 1);
        }
    }
    assert!(
        offenders.is_empty(),
        "journal lines with no client_id: {offenders:?}"
    );
}

#[tokio::test]
async fn a_dry_run_order_is_journalled_under_its_plan() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let cid = mint_client_id("plan-9", "RELIANCE").unwrap();
    gw.place(&PlaceOrder {
        client_id: Some(cid.clone()),
        ..buy("RELIANCE")
    })
    .await
    .unwrap();
    let lines = bed.lines();
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0]["client_id"], cid.as_str());
    assert_eq!(
        parse_client_id(lines[0]["client_id"].as_str().unwrap()).unwrap(),
        ("plan-9".to_owned(), "RELIANCE".to_owned())
    );
}

#[tokio::test]
async fn a_live_order_is_journalled_under_its_plan_too() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(false, false, false)).await;
    let cid = mint_client_id("plan-9", "RELIANCE").unwrap();
    let out = gw
        .place(&PlaceOrder {
            client_id: Some(cid.clone()),
            ..buy("RELIANCE")
        })
        .await
        .unwrap();
    assert_eq!(status(&out), "PLACED");
    let lines = bed.lines();
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0]["event"], "placed");
    assert_eq!(lines[0]["client_id"], cid.as_str());
    // The replay broker saw exactly one order, keyed on the minted id as the SDK sends it.
    assert_eq!(bed.sent(), 1);
}

#[tokio::test]
async fn a_risk_refusal_is_journalled_under_its_plan() {
    let bed = Bed::new();
    let risk = manager(RiskConfig {
        max_position_value: 1.0,
        ..RiskConfig::default()
    })
    .await;
    let gw = bed.gateway_with(gates(true, false, false), risk).await;
    let cid = mint_client_id("plan-9", "RELIANCE").unwrap();
    let out = gw
        .place(&PlaceOrder {
            qty: 100,
            client_id: Some(cid.clone()),
            ..buy("RELIANCE")
        })
        .await
        .unwrap();
    assert_eq!(status(&out), "RISK_BLOCKED");
    let lines = bed.lines();
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0]["event"], "risk_block");
    assert_eq!(lines[0]["client_id"], cid.as_str());
}

#[tokio::test]
async fn an_armed_and_a_refused_stop_are_journalled_under_their_plan() {
    for (trigger, want) in [(90.0, "DRY_RUN_GTT"), (110.0, "BLOCKED")] {
        let bed = Bed::new();
        let gw = bed.gateway(gates(true, false, false)).await;
        let cid = mint_client_id("plan-9", "RELIANCE").unwrap();
        let mut g = stop("RELIANCE", 10, trigger, 100.0);
        g.client_id = Some(cid.clone());
        let out = gw.place_gtt_stop(&g).await.unwrap();
        assert_eq!(status(&out), want);
        let ids: Vec<_> = bed.lines().iter().map(|l| l["client_id"].clone()).collect();
        assert_eq!(ids, vec![serde_json::Value::from(cid.as_str())]);
    }
}

#[tokio::test]
async fn a_cancelled_stop_names_its_plan_or_records_null() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let cid = mint_client_id("plan-9", "RELIANCE").unwrap();
    let out = gw
        .delete_gtt_value(&delete("RELIANCE", Some(&cid)), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&out), "DRY_RUN_GTT_DELETE");
    gw.delete_gtt_value(&delete("RELIANCE", None), &PyValue::Int(4243))
        .await
        .unwrap();
    let lines = bed.lines();
    assert_eq!(lines[0]["client_id"], cid.as_str());
    assert!(lines[1].as_object().unwrap().contains_key("client_id"));
    assert!(lines[1]["client_id"].is_null());
}

#[tokio::test]
async fn an_order_with_no_client_id_still_journals_the_key_it_used() {
    let bed = Bed::new();
    let gw = bed.gateway(gates(true, false, false)).await;
    let out = gw.place(&buy("RELIANCE")).await.unwrap();
    let lines = bed.lines();
    assert_eq!(lines.len(), 1);
    let journalled = lines[0]["client_id"].as_str().unwrap().to_owned();
    assert!(!journalled.is_empty());
    assert_eq!(text(&out, "order_id"), format!("DRY-{journalled}"));
}

// ================================================================================
// The desk's shim (`app/core/gateway.py`): gates read per order, tenant stamped, gross filled.
// ================================================================================

mod shim {
    use super::*;
    use baskfy_desk::lanes::exec_core::desk::{DeskGateway, DeskParts, SharedRisk, ShimGateway};
    use baskfy_desk::settings::{Settings, SharedSettings};
    use baskfy_desk::storage::{Db, DbConfig};

    async fn desk(bed: &Bed, settings: SharedSettings, risk: Arc<RiskManager>) -> DeskGateway {
        let db = Db::open(DbConfig {
            backend: "sqlite".to_owned(),
            schema: String::new(),
            database_url: String::new(),
            db_path: bed.dir.path().join("desk.db"),
        })
        .await
        .unwrap();
        let kite: Arc<dyn KiteWrites> = bed.kite.clone();
        let parts = DeskParts {
            kite,
            settings: settings.clone(),
            clock: clock(),
            ids: Arc::new(HarnessIds::new("exec-gateway-shim")),
            journal_path: bed.journal(),
            db: db.clone(),
        };
        let cell = Arc::new(SharedRisk::of(risk, settings, clock(), db));
        DeskGateway::build_with_risk(parts, cell).unwrap()
    }

    async fn risk() -> Arc<RiskManager> {
        Arc::new(
            RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
                .await
                .unwrap(),
        )
    }

    fn settings() -> SharedSettings {
        let s = Settings::defaults().unwrap();
        assert!(s.dry_run, "DRY_RUN defaults true");
        Arc::new(std::sync::RwLock::new(s))
    }

    #[tokio::test]
    async fn the_gates_are_read_at_the_moment_of_each_order_not_at_construction() {
        let bed = Bed::new();
        let settings = settings();
        let gw = desk(&bed, settings.clone(), risk().await).await;
        let mis = PlaceOrder {
            product: "MIS".to_owned(),
            tenant: None,
            plan_tenant: None,
            gross_exposure: None,
            ..buy("RELIANCE")
        };
        let out = Gateway::place(&gw, mis.clone()).await.unwrap();
        assert_eq!(status(&out), "BLOCKED", "MIS without INTRADAY_ENABLED");
        settings.write().unwrap().intraday_enabled = true;
        let out = Gateway::place(&gw, mis).await.unwrap();
        assert_eq!(
            status(&out),
            "DRY_RUN",
            "the switch took effect on the next order"
        );
        assert_eq!(bed.sent(), 0);
    }

    #[tokio::test]
    async fn the_sole_tenant_is_stamped_and_gross_exposure_is_filled() {
        let bed = Bed::new();
        let rec = Arc::new(RecordingRisk(std::sync::Mutex::new(Vec::new())));
        let gw = bed
            .gateway_with(gates(true, false, false), rec.clone())
            .await;
        // The core refuses an order without its tenant or its gross (P4.3, AF 3.7)…
        let bare = PlaceOrder {
            tenant: None,
            plan_tenant: None,
            gross_exposure: None,
            ..buy("RELIANCE")
        };
        assert!(gw.place(&bare).await.is_err());
        // …and the desk's shim supplies both: the sole tenant, and `|price × qty|`.
        let shim = ShimGateway::new(Arc::new(gw), settings());
        let order = PlaceOrder {
            qty: 7,
            price: Some(3.0),
            ..bare
        };
        let out = Gateway::place(&shim, order).await.unwrap();
        assert_eq!(status(&out), "DRY_RUN");
        assert_eq!(
            *rec.0.lock().unwrap(),
            vec![("RELIANCE".to_owned(), 21.0, 21.0)]
        );
        // A caller's own gross is kept, never overwritten.
        let order = PlaceOrder {
            qty: 7,
            price: Some(3.0),
            gross_exposure: Some(5.0),
            client_id: Some("p:RELIANCE".to_owned()),
            tenant: None,
            plan_tenant: None,
            ..buy("RELIANCE")
        };
        Gateway::place(&shim, order).await.unwrap();
        assert_eq!(rec.0.lock().unwrap()[1], ("RELIANCE".to_owned(), 21.0, 5.0));
    }

    #[tokio::test]
    async fn the_shim_never_reaches_the_broker_under_dry_run() {
        let bed = Bed::new();
        let gw = desk(&bed, settings(), risk().await).await;
        let bare = PlaceOrder {
            tenant: None,
            plan_tenant: None,
            gross_exposure: None,
            ..buy("RELIANCE")
        };
        Gateway::place(&gw, bare).await.unwrap();
        Gateway::place_gtt_stop(&gw, stop("RELIANCE", 10, 90.0, 100.0))
            .await
            .unwrap();
        Gateway::delete_gtt(&gw, delete("RELIANCE", None))
            .await
            .unwrap();
        assert_eq!(bed.sent(), 0, "DRY_RUN reached the broker through the shim");
    }
}
