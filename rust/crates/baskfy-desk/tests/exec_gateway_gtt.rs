//! Lane **exec_gateway** (adversarial review): the gateway's live-transport halves, ported from
//! `decile-blueprint/packages/execution/tests/{test_gtt_gateway,test_gtt_modify_and_cancel}.py`
//! and the order path's idempotency rules (`gateway.py` `place`: a definitive refusal releases
//! the reservation, an unknown ERROR keeps it, a concurrent twin cannot double-send).
//!
//! The broker is the replay Kite, which records every call and sends nothing; errors are the
//! fixture's recorded `error` for a route (Python's `SpyKC(fail=...)`). Instruments are the
//! Python test's `INSTRUMENTS`: RELIANCE 0.05, OFSS 1.00, SONACOMS 0.10, NOTICK (no tick).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, GatesFn, GttStop, ModifyGtt, PlaceOrder, ProductGates, TenantIds,
};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::kite::KiteWrites;
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskLayer, RiskManager};
use baskfy_desk::wire::{PyDict, PyValue};
use chrono::TimeZone;

const NSE_CSV: &str = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n\
738561,2885,RELIANCE,RELIANCE,0,,0,0.05,1,EQ,NSE,NSE\n\
2748929,10738,OFSS,OFSS,0,,0,1.0,1,EQ,NSE,NSE\n\
1,1,SONACOMS,SONACOMS,0,,0,0.1,1,EQ,NSE,NSE\n\
2,2,NOTICK,NOTICK,0,,0,0,1,EQ,NSE,NSE\n";

fn caller() -> TenantIds {
    TenantIds::new(1, 10).unwrap()
}

fn other() -> TenantIds {
    TenantIds::new(2, 20).unwrap()
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
    /// `fail`: a route whose recorded answer is an error (`SpyKC(fail=...)`); `csv`: the NSE
    /// dump (`None` → no dump at all, so the instrument read raises).
    fn new(fail: &[(&str, &str)], csv: Option<&str>) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let routes: serde_json::Map<String, serde_json::Value> = fail
            .iter()
            .map(|(route, kind)| {
                (
                    (*route).to_owned(),
                    serde_json::json!({"error": {"type": kind, "message": format!("broker refused {route}"), "code": 400}}),
                )
            })
            .collect();
        let fixture = serde_json::json!({
            "session": {"authed": true}, "routes": routes, "quotes": {}, "historical": {}
        });
        std::fs::write(dir.path().join("kite.json"), fixture.to_string()).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        if let Some(csv) = csv {
            std::fs::write(dir.path().join("instruments/NSE.csv"), csv).unwrap();
        }
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        Self { dir, kite }
    }

    fn ok() -> Self {
        Self::new(&[], Some(NSE_CSV))
    }

    fn gateway_with(&self, dry_run: bool, risk: Arc<dyn RiskLayer>) -> OrderGateway {
        let g = ProductGates {
            dry_run,
            ..ProductGates::default()
        };
        let gates: GatesFn = Arc::new(move || g);
        let kite: Arc<dyn KiteWrites> = self.kite.clone();
        OrderGateway::new(
            kite,
            risk,
            GatewayOptions {
                gates,
                journal_path: self.dir.path().join("journal.jsonl"),
                stop_band: None,
                coverage: None,
                clock: clock(),
                ids: Arc::new(HarnessIds::new("exec-gateway-gtt")),
                limits: None,
            },
        )
        .unwrap()
    }

    async fn gateway(&self, dry_run: bool) -> OrderGateway {
        self.gateway_with(dry_run, fresh_risk().await)
    }

    /// The route names of every call that reached the broker, reads included.
    fn routes(&self) -> Vec<String> {
        self.kite.calls().into_iter().map(|c| c.route).collect()
    }

    fn count(&self, route: &str) -> usize {
        self.routes().iter().filter(|r| *r == route).count()
    }

    fn journal(&self) -> Vec<serde_json::Value> {
        let Ok(text) = std::fs::read_to_string(self.dir.path().join("journal.jsonl")) else {
            return Vec::new();
        };
        text.lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }

    fn events(&self) -> Vec<String> {
        self.journal()
            .iter()
            .map(|r| r["event"].as_str().unwrap_or("").to_owned())
            .collect()
    }
}

async fn fresh_risk() -> Arc<dyn RiskLayer> {
    Arc::new(
        RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
            .await
            .unwrap(),
    )
}

async fn killed_risk() -> Arc<RiskManager> {
    let risk = Arc::new(
        RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
            .await
            .unwrap(),
    );
    risk.on_pnl(-500_000.0).await.unwrap();
    assert!(risk.state().await.killed);
    risk
}

fn arm(symbol: &str, qty: i64, trigger: f64, last: f64, cid: Option<&str>) -> GttStop {
    GttStop {
        symbol: symbol.to_owned(),
        qty,
        trigger,
        last_price: last,
        exchange: "NSE".to_owned(),
        series: None,
        client_id: cid.map(str::to_owned),
        tenant: Some(caller()),
        plan_tenant: Some(caller()),
        limit_fraction: None,
        fo_plan: None,
    }
}

fn cancel_gtt() -> DeleteGtt {
    DeleteGtt {
        gtt_id: 4242,
        symbol: "RELIANCE".to_owned(),
        exchange: "NSE".to_owned(),
        series: None,
        client_id: None,
        tenant: Some(caller()),
        plan_tenant: Some(caller()),
    }
}

fn modify(qty: i64, trigger: f64, last: f64) -> ModifyGtt {
    ModifyGtt {
        gtt_id: 4242,
        symbol: "RELIANCE".to_owned(),
        qty,
        trigger,
        last_price: last,
        exchange: "NSE".to_owned(),
        series: None,
        client_id: None,
        tenant: Some(caller()),
        plan_tenant: Some(caller()),
        limit_fraction: None,
        fo_plan: None,
        floor_trigger: None,
    }
}

fn cancel_order(order_id: &str, symbol: &str) -> CancelOrder {
    CancelOrder {
        order_id: order_id.to_owned(),
        symbol: symbol.to_owned(),
        variety: "regular".to_owned(),
        series: None,
        client_id: None,
        tenant: Some(caller()),
        plan_tenant: Some(caller()),
    }
}

fn buy(cid: &str) -> PlaceOrder {
    PlaceOrder {
        symbol: "RELIANCE".to_owned(),
        qty: 10,
        side: "BUY".to_owned(),
        price: Some(100.0),
        client_id: Some(cid.to_owned()),
        gross_exposure: Some(1000.0),
        tenant: Some(caller()),
        plan_tenant: Some(caller()),
        ..PlaceOrder::default()
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

fn f(out: &PyDict, key: &str) -> f64 {
    match out.get(key) {
        Some(PyValue::Float(v)) => *v,
        other => panic!("{key}: {other:?}"),
    }
}

// ================================================================================
// test_gtt_gateway.py — the live stop
// ================================================================================

#[tokio::test]
async fn a_live_stop_is_a_cnc_sell_limit_just_under_a_tick_snapped_trigger() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let out = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, None))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_PLACED");
    assert_eq!(f(&out, "trigger"), 89.0);
    // 89.0 * 0.995 = 88.555, snapped to the 0.05 tick.
    assert_eq!(f(&out, "limit"), 88.55);
    let writes = bed.kite.write_calls();
    assert_eq!(writes.len(), 1);
    assert_eq!(writes[0].route, "gtt.place");
    let params = format!("{:?}", writes[0].params);
    for needle in [
        "\"single\"",
        "RELIANCE",
        "SELL",
        "LIMIT",
        "CNC",
        "88.55",
        "89.0",
    ] {
        assert!(params.contains(needle), "{needle} missing from {params}");
    }
    assert!(!params.contains("MIS") && !params.contains("NRML"));
    let placed: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_placed")
        .collect();
    assert_eq!(placed.len(), 1);
    assert_eq!(placed[0]["qty"], 10);
    assert_eq!(placed[0]["limit"], 88.55);
    assert_eq!(placed[0]["last_price"], 100.0);
    assert_eq!(placed[0]["drop_pct"], 11.0);
    assert!(placed[0].get("ts").is_some());
}

#[tokio::test]
async fn a_coarse_tick_scrip_is_snapped_to_whole_rupees() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let out = gw
        .place_gtt_stop(&arm("OFSS", 3, 2515.1, 2750.0, None))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_PLACED");
    assert_eq!(f(&out, "trigger"), 2515.0);
    assert_eq!(f(&out, "limit"), 2502.0);
}

#[tokio::test]
async fn an_instrument_with_no_tick_size_falls_back_to_five_paise() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let out = gw
        .place_gtt_stop(&arm("NOTICK", 10, 89.03, 100.0, None))
        .await
        .unwrap();
    assert_eq!(f(&out, "trigger"), 89.05);
}

#[tokio::test]
async fn the_instrument_dump_is_fetched_once_per_exchange() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    gw.place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("a")))
        .await
        .unwrap();
    gw.place_gtt_stop(&arm("SONACOMS", 10, 89.0, 100.0, Some("b")))
        .await
        .unwrap();
    assert_eq!(bed.count("market.instruments"), 1);
}

#[tokio::test]
async fn a_trigger_that_is_not_a_stop_never_reaches_the_broker() {
    let cases: [(i64, f64, f64, &str); 7] = [
        (0, 89.0, 100.0, "quantity must be positive"),
        (-5, 89.0, 100.0, "quantity must be positive"),
        (10, 0.0, 100.0, "finite positive"),
        (10, -1.0, 100.0, "finite positive"),
        (10, 89.0, 0.0, "cannot be checked against anything"),
        (10, 100.0, 100.0, "at or above the last price"),
        (10, 120.0, 100.0, "at or above the last price"),
    ];
    for (qty, trigger, last, why) in cases {
        let bed = Bed::ok();
        let gw = bed.gateway(false).await;
        let out = gw
            .place_gtt_stop(&arm("RELIANCE", qty, trigger, last, None))
            .await
            .unwrap();
        assert_eq!(status(&out), "BLOCKED", "{qty} {trigger} {last}");
        assert!(text(&out, "error").contains(why), "{}", text(&out, "error"));
        assert!(bed.routes().is_empty(), "{:?}", bed.routes());
        assert!(bed.events().contains(&"gtt_block".to_owned()));
    }
}

#[tokio::test]
async fn a_trigger_that_snaps_up_through_the_last_price_is_refused_after_snapping() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let out = gw
        .place_gtt_stop(&arm("OFSS", 10, 2749.6, 2750.0, None))
        .await
        .unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("at or above the last price"));
    assert_eq!(
        bed.routes(),
        ["market.instruments"],
        "the GTT was still placed"
    );
    let snapped: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["stage"] == "after_tick_snap")
        .collect();
    assert_eq!(snapped[0]["trigger"], 2750.0);
    assert_eq!(snapped[0]["requested_trigger"], 2749.6);
}

#[tokio::test]
async fn a_tripped_kill_switch_does_not_leave_a_position_unstopped() {
    let bed = Bed::ok();
    let risk = killed_risk().await;
    let gw = bed.gateway_with(false, risk);
    let out = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, None))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_PLACED");
    assert_eq!(bed.count("gtt.place"), 1);
    let notes: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_risk_note")
        .collect();
    assert!(notes[0]["why"].as_str().unwrap().contains("KILL SWITCH"));
}

#[tokio::test]
async fn a_tripped_kill_switch_refuses_to_remove_protection() {
    let bed = Bed::ok();
    let gw = bed.gateway_with(false, killed_risk().await);
    let out = gw
        .delete_gtt_value(&cancel_gtt(), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&out), "RISK_BLOCKED");
    assert!(text(&out, "error").contains("KILL SWITCH"));
    assert!(bed.routes().is_empty());
    assert!(bed.events().contains(&"gtt_delete_risk_block".to_owned()));
}

#[tokio::test]
async fn arming_a_stop_does_not_spend_the_days_order_budget() {
    let bed = Bed::ok();
    let risk = Arc::new(
        RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
            .await
            .unwrap(),
    );
    let gw = bed.gateway_with(false, risk.clone());
    for n in 0..5 {
        let cid = format!("c{n}");
        gw.place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some(&cid)))
            .await
            .unwrap();
    }
    assert_eq!(risk.state().await.orders_today, 0);
}

#[tokio::test]
async fn a_replayed_stop_is_not_armed_twice() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let first = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("PLAN1:RELIANCE")))
        .await
        .unwrap();
    let again = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("PLAN1:RELIANCE")))
        .await
        .unwrap();
    assert_eq!(status(&first), "GTT_PLACED");
    assert_eq!(status(&again), "DUPLICATE");
    assert_eq!(again.get("gtt_id"), first.get("gtt_id"));
    assert_eq!(bed.count("gtt.place"), 1);
    // And after a restart, from the journal.
    drop(gw);
    let gw = bed.gateway(false).await;
    let after = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("PLAN1:RELIANCE")))
        .await
        .unwrap();
    assert_eq!(status(&after), "DUPLICATE");
    assert_eq!(after.get("gtt_id"), first.get("gtt_id"));
    assert_eq!(bed.count("gtt.place"), 1);
}

#[tokio::test]
async fn an_order_and_its_stop_do_not_share_an_idempotency_namespace() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let placed = gw.place(&buy("PLAN1:RELIANCE")).await.unwrap();
    assert_eq!(status(&placed), "PLACED");
    let stop = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("PLAN1:RELIANCE")))
        .await
        .unwrap();
    assert_eq!(status(&stop), "GTT_PLACED");
    assert_eq!(bed.count("gtt.place"), 1);
    assert_eq!(bed.count("order.place"), 1);
}

#[tokio::test]
async fn a_dry_run_stop_is_also_deduplicated_and_makes_no_instrument_call() {
    let bed = Bed::ok();
    let gw = bed.gateway(true).await;
    let a = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("P:X")))
        .await
        .unwrap();
    let b = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("P:X")))
        .await
        .unwrap();
    assert_eq!(status(&a), "DRY_RUN_GTT");
    assert_eq!(status(&b), "DUPLICATE");
    assert!(bed.routes().is_empty(), "{:?}", bed.routes());
}

#[tokio::test]
async fn a_broker_refusal_to_arm_is_reported_unknown_and_not_retried_blind() {
    let bed = Bed::new(&[("gtt.place", "InputException")], Some(NSE_CSV));
    let gw = bed.gateway(false).await;
    let out = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("P:R")))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_ERROR");
    assert!(text(&out, "error").contains("broker refused gtt.place"));
    assert_eq!(out.get("reached_exchange"), Some(&PyValue::None));
    let errors: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_error")
        .collect();
    assert_eq!(errors[0]["stage"], "place_gtt");
    // The reservation is kept (Python leaves PENDING): a blind retry is how a position gets two
    // stops, so the same client id is a DUPLICATE, not a second send.
    let again = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("P:R")))
        .await
        .unwrap();
    assert_eq!(status(&again), "DUPLICATE");
    assert_eq!(bed.count("gtt.place"), 1);
}

#[tokio::test]
async fn a_failed_instrument_dump_does_not_place_an_unsnapped_trigger() {
    let bed = Bed::new(&[], None);
    let gw = bed.gateway(false).await;
    let out = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, None))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_ERROR");
    assert_eq!(bed.routes(), ["market.instruments"]);
    let errors: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_error")
        .collect();
    assert_eq!(errors[0]["stage"], "tick_size");
}

#[tokio::test]
async fn an_empty_instrument_dump_is_an_error_and_is_not_cached() {
    let header = NSE_CSV.lines().next().unwrap().to_owned() + "\n";
    let bed = Bed::new(&[], Some(&header));
    let gw = bed.gateway(false).await;
    let first = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("a")))
        .await
        .unwrap();
    assert_eq!(status(&first), "GTT_ERROR");
    assert!(text(&first, "error").contains("came back empty"));
    let second = gw
        .place_gtt_stop(&arm("RELIANCE", 10, 89.0, 100.0, Some("b")))
        .await
        .unwrap();
    assert_eq!(status(&second), "GTT_ERROR");
    assert_eq!(
        bed.count("market.instruments"),
        2,
        "the empty dump was cached"
    );
}

#[tokio::test]
async fn a_cancel_reaches_the_broker_once_and_a_refusal_is_reported() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let out = gw
        .delete_gtt_value(&cancel_gtt(), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_DELETED");
    assert_eq!(bed.count("gtt.delete"), 1);

    let bed = Bed::new(&[("gtt.delete", "InputException")], Some(NSE_CSV));
    let gw = bed.gateway(false).await;
    let out = gw
        .delete_gtt_value(&cancel_gtt(), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_DELETE_ERROR");
    assert!(text(&out, "error").contains("broker refused gtt.delete"));
    assert!(bed.events().contains(&"gtt_delete_error".to_owned()));
}

#[tokio::test]
async fn a_malformed_trigger_id_never_reaches_the_broker() {
    for bad in [
        PyValue::Int(0),
        PyValue::Int(-1),
        PyValue::Str("4242".to_owned()),
        PyValue::Float(42.0),
        PyValue::Bool(true),
        PyValue::None,
    ] {
        let bed = Bed::ok();
        let gw = bed.gateway(false).await;
        let out = gw.delete_gtt_value(&cancel_gtt(), &bad).await.unwrap();
        assert_eq!(status(&out), "BLOCKED", "{bad:?}");
        assert!(text(&out, "error").contains("is not a GTT trigger id"));
        assert!(bed.routes().is_empty());
    }
}

// ================================================================================
// test_gtt_modify_and_cancel.py
// ================================================================================

#[tokio::test]
async fn a_live_modify_re_sizes_the_same_trigger_and_journals_it() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let out = gw
        .modify_gtt_value(&modify(7, 89.0, 100.0), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_MODIFIED");
    assert_eq!(out.get("qty"), Some(&PyValue::Int(7)));
    let writes = bed.kite.write_calls();
    assert_eq!(writes.len(), 1);
    assert_eq!(writes[0].route, "gtt.modify");
    assert!(format!("{:?}", writes[0].url_args).contains("4242"));
    assert!(bed.events().contains(&"gtt_modified".to_owned()));
}

#[tokio::test]
async fn dry_run_modify_and_cancel_touch_nothing_and_still_answer() {
    let bed = Bed::ok();
    let gw = bed.gateway(true).await;
    let m = gw
        .modify_gtt_value(&modify(7, 89.0, 100.0), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&m), "DRY_RUN_GTT_MODIFY");
    let c = gw
        .cancel_order_value(
            &cancel_order("O-1", "RELIANCE"),
            &PyValue::Str("O-1".into()),
        )
        .await
        .unwrap();
    assert_eq!(status(&c), "ORDER_CANCEL_DRY_RUN");
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn a_modify_is_guarded_like_an_arm() {
    // untouchable
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let mut m = modify(7, 6300.0, 7000.0);
    m.symbol = "SGBJUN29".to_owned();
    let out = gw.modify_gtt_value(&m, &PyValue::Int(4242)).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("protected instrument"));
    // cross tenant
    let mut m = modify(7, 89.0, 100.0);
    m.plan_tenant = Some(other());
    let out = gw.modify_gtt_value(&m, &PyValue::Int(4242)).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    // zero shares
    let out = gw
        .modify_gtt_value(&modify(0, 89.0, 100.0), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("cancel it with delete_gtt instead"));
    // not a stop
    let out = gw
        .modify_gtt_value(&modify(7, 120.0, 100.0), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&out), "BLOCKED");
    // bad fraction raises before anything
    let mut m = modify(7, 89.0, 100.0);
    m.limit_fraction = Some(1.5);
    let err = gw
        .modify_gtt_value(&m, &PyValue::Int(4242))
        .await
        .unwrap_err();
    assert_eq!(err.python_type, "ValueError");
    assert!(bed.routes().is_empty(), "{:?}", bed.routes());
}

#[tokio::test]
async fn a_tripped_kill_switch_does_not_stop_a_stop_from_being_re_sized() {
    let bed = Bed::ok();
    let gw = bed.gateway_with(false, killed_risk().await);
    let out = gw
        .modify_gtt_value(&modify(7, 89.0, 100.0), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_MODIFIED");
    assert!(bed.events().contains(&"gtt_risk_note".to_owned()));
}

#[tokio::test]
async fn modifying_twice_to_the_same_quantity_is_not_a_duplicate() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    for _ in 0..2 {
        let out = gw
            .modify_gtt_value(&modify(7, 89.0, 100.0), &PyValue::Int(4242))
            .await
            .unwrap();
        assert_eq!(status(&out), "GTT_MODIFIED");
    }
    assert_eq!(bed.count("gtt.modify"), 2);
}

#[tokio::test]
async fn a_broker_failure_on_modify_and_cancel_is_reported_not_raised() {
    let bed = Bed::new(
        &[
            ("gtt.modify", "InputException"),
            ("order.cancel", "OrderException"),
        ],
        Some(NSE_CSV),
    );
    let gw = bed.gateway(false).await;
    let out = gw
        .modify_gtt_value(&modify(7, 89.0, 100.0), &PyValue::Int(4242))
        .await
        .unwrap();
    assert_eq!(status(&out), "GTT_MODIFY_ERROR");
    assert_eq!(out.get("reached_exchange"), Some(&PyValue::None));
    let out = gw
        .cancel_order_value(
            &cancel_order("O-1", "RELIANCE"),
            &PyValue::Str("O-1".into()),
        )
        .await
        .unwrap();
    assert_eq!(status(&out), "ORDER_CANCEL_ERROR");
    assert!(text(&out, "error").contains("broker refused order.cancel"));
}

#[tokio::test]
async fn a_cancel_is_guarded_and_the_kill_switch_does_not_refuse_it() {
    let bed = Bed::ok();
    let gw = bed.gateway_with(false, killed_risk().await);
    for (oid, sym, why) in [
        ("O-1", "SGBJUN29", "protected instrument"),
        ("O-1", "  ", "without naming its instrument"),
        ("  ", "RELIANCE", "is not an order id"),
    ] {
        let out = gw
            .cancel_order_value(&cancel_order(oid, sym), &PyValue::Str(oid.into()))
            .await
            .unwrap();
        assert_eq!(status(&out), "BLOCKED", "{oid} {sym}");
        assert!(text(&out, "error").contains(why), "{}", text(&out, "error"));
    }
    let mut x = cancel_order("O-1", "RELIANCE");
    x.plan_tenant = Some(other());
    let out = gw
        .cancel_order_value(&x, &PyValue::Str("O-1".into()))
        .await
        .unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(bed.routes().is_empty());
    // A cancel reduces exposure: the tripped kill switch lets it through.
    let out = gw
        .cancel_order_value(
            &cancel_order("O-1", "RELIANCE"),
            &PyValue::Str("O-1".into()),
        )
        .await
        .unwrap();
    assert_eq!(status(&out), "ORDER_CANCELLED");
    assert_eq!(bed.count("order.cancel"), 1);
}

// ================================================================================
// gateway.py place(): the live order's idempotency (AF 3.4)
// ================================================================================

#[tokio::test]
async fn a_definitive_refusal_releases_the_client_id_for_a_corrected_retry() {
    for kind in [
        "InputException",
        "OrderException",
        "PermissionException",
        "TokenException",
    ] {
        let bed = Bed::new(&[("order.place", kind)], Some(NSE_CSV));
        let gw = bed.gateway(false).await;
        let out = gw.place(&buy("P:RELIANCE")).await.unwrap();
        assert_eq!(status(&out), "REJECTED", "{kind}");
        assert_eq!(out.get("reached_exchange"), Some(&PyValue::Bool(false)));
        assert_eq!(text(&out, "exception"), kind);
        let again = gw.place(&buy("P:RELIANCE")).await.unwrap();
        assert_eq!(
            status(&again),
            "REJECTED",
            "{kind}: the retry never reached the broker"
        );
        assert_eq!(bed.count("order.place"), 2);
        assert!(bed.events().contains(&"rejected".to_owned()));
    }
}

#[tokio::test]
async fn an_unknown_failure_keeps_the_reservation_so_nothing_double_sends() {
    for kind in ["NetworkException", "DataException", "GeneralException"] {
        let bed = Bed::new(&[("order.place", kind)], Some(NSE_CSV));
        let gw = bed.gateway(false).await;
        let out = gw.place(&buy("P:RELIANCE")).await.unwrap();
        assert_eq!(status(&out), "ERROR", "{kind}");
        assert_eq!(out.get("reached_exchange"), Some(&PyValue::None));
        let again = gw.place(&buy("P:RELIANCE")).await.unwrap();
        assert_eq!(status(&again), "DUPLICATE", "{kind}");
        assert_eq!(bed.count("order.place"), 1);
    }
}

#[tokio::test]
async fn two_concurrent_twins_send_once() {
    let bed = Bed::ok();
    let gw = Arc::new(bed.gateway(false).await);
    let (x, y) = (buy("P:RELIANCE"), buy("P:RELIANCE"));
    let (a, b) = tokio::join!(gw.place(&x), gw.place(&y));
    let mut got = [status(&a.unwrap()), status(&b.unwrap())];
    got.sort();
    assert_eq!(got, ["DUPLICATE", "PLACED"]);
    assert_eq!(bed.count("order.place"), 1);
}

#[tokio::test]
async fn a_risk_block_never_reaches_the_broker_and_is_a_failed_status() {
    let bed = Bed::ok();
    let gw = bed.gateway_with(false, killed_risk().await);
    let out = gw.place(&buy("P:RELIANCE")).await.unwrap();
    assert_eq!(status(&out), "RISK_BLOCKED");
    assert!(baskfy_desk::gateway::FAILED_STATUSES.contains(&"RISK_BLOCKED"));
    assert!(bed.routes().is_empty());
    // A refused order reserves nothing: the same id goes through once the switch is clear.
    let bed2 = Bed::ok();
    let gw2 = bed2.gateway(false).await;
    assert_eq!(
        status(&gw2.place(&buy("P:RELIANCE")).await.unwrap()),
        "PLACED"
    );
}

#[tokio::test]
async fn a_live_limit_is_snapped_to_one_decimal_or_the_given_tick() {
    let bed = Bed::ok();
    let gw = bed.gateway(false).await;
    let mut o = buy("P:A");
    o.price = Some(100.04);
    gw.place(&o).await.unwrap();
    let mut o = buy("P:B");
    o.price = Some(100.07);
    o.tick_size = Some(0.05);
    gw.place(&o).await.unwrap();
    let journal = bed.journal();
    let placed: Vec<_> = journal.iter().filter(|r| r["event"] == "placed").collect();
    assert_eq!(placed[0]["price"], 100.0);
    assert_eq!(placed[1]["price"], 100.05);
    assert_eq!(placed[0]["validity"], "DAY");
    assert_eq!(placed[0]["product"], "CNC");
    assert_eq!(placed[0]["client_id"], "P:A");
}

#[tokio::test]
async fn a_journal_line_that_is_not_an_object_refuses_to_build_the_gateway() {
    // Python's `_replay_journal` calls `rec.get(...)` and raises AttributeError, so the
    // gateway never exists without its idempotency memory; skipping the line would build one.
    let bed = Bed::ok();
    std::fs::write(bed.dir.path().join("journal.jsonl"), "[1, 2]\n").unwrap();
    let kite: Arc<dyn KiteWrites> = bed.kite.clone();
    let g = ProductGates::default();
    let built = OrderGateway::new(
        kite,
        fresh_risk().await,
        GatewayOptions {
            gates: Arc::new(move || g),
            journal_path: bed.dir.path().join("journal.jsonl"),
            stop_band: None,
            coverage: None,
            clock: clock(),
            ids: Arc::new(HarnessIds::new("exec-gateway-gtt")),
            limits: None,
        },
    );
    assert!(built.is_err());
}
