//! Lane **exec_gateway** (wave D2): the gateway's LIVE transport against the live Python's own
//! answers.
//!
//! `desk_diff.py` runs both desks with `DRY_RUN=true` — a safety rail — so the journal records
//! only the live path writes (`placed`, `gtt_placed`, `gtt_modified`, `gtt_deleted`,
//! `order_cancelled`, every broker-error record) never reach a graded case there.
//! `fixtures/exec_gateway/live_goldens.json` is written by
//! `tools/parity/exec_gateway_live_goldens.py`: the Python `OrderGateway` with `dry_run=False`
//! over the harness's Python replay Kite (kiteconnect shaping each request, nothing sent). This
//! test runs the same operations through the Rust gateway over `fake_kite.rs` and requires, per
//! scenario, the **same result dicts** (or the same exception), the **same journal lines**
//! (`ts` aside: it is `time.time()`) and the **same broker call log**, line for line.
//!
//! No network and no credential: both replay Kites answer from a fixture in a temp dir.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, FoPlanRef, GatesFn, GttStop, ModifyGtt, PlaceOrder, ProductGates,
    StopBand, TenantIds,
};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::kite::KiteWrites;
use baskfy_desk::lanes::exec_core::guards::core_coverage;
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskLayer, RiskManager};
use baskfy_desk::wire::{PyDict, PyValue};
use chrono::TimeZone;
use serde_json::Value;

const GOLDENS: &str = include_str!("../fixtures/exec_gateway/live_goldens.json");

fn clock() -> Arc<dyn Clock> {
    Arc::new(FixedClock::new(
        ist().with_ymd_and_hms(2026, 10, 30, 10, 0, 0).unwrap(),
    ))
}

/// A JSON value as the Python value the golden script passed.
fn py(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => n
            .as_i64()
            .map_or_else(|| PyValue::Float(n.as_f64().unwrap()), PyValue::Int),
        Value::String(s) => PyValue::Str(s.clone()),
        Value::Array(a) => PyValue::List(a.iter().map(py).collect()),
        Value::Object(m) => {
            let mut d = PyDict::new();
            for (k, v) in m {
                d.insert(k.clone(), py(v));
            }
            PyValue::Map(d)
        }
    }
}

/// A result dict as `json.loads(json.dumps(out, default=str))`.
fn json_of(d: &PyDict) -> Value {
    let text = baskfy_providers::kite::py_json_dumps(&PyValue::Map(d.clone())).unwrap();
    serde_json::from_str(&text).unwrap()
}

fn s(a: &Value, k: &str) -> Option<String> {
    a.get(k).and_then(Value::as_str).map(str::to_owned)
}

fn f(a: &Value, k: &str) -> Option<f64> {
    a.get(k).and_then(Value::as_f64)
}

fn tenant(a: &Value, k: &str) -> Option<TenantIds> {
    let pair = a.get(k)?.as_array()?;
    Some(TenantIds::new(pair[0].as_i64().unwrap(), pair[1].as_i64().unwrap()).unwrap())
}

fn fo_plan(a: &Value) -> Option<FoPlanRef> {
    let p = a.get("fo_plan")?;
    if p.is_null() {
        return None;
    }
    let list = |k: &str| {
        p.get(k)
            .and_then(Value::as_array)
            .map(|v| v.iter().map(py).collect())
            .unwrap_or_default()
    };
    Some(FoPlanRef {
        plan_id: s(p, "plan_id").unwrap(),
        sleeve: s(p, "sleeve").unwrap(),
        step: p.get("step").filter(|v| !v.is_null()).map(py),
        broker_positions: list("broker_positions"),
        plan_filled: list("plan_filled"),
    })
}

fn place_order(a: &Value) -> PlaceOrder {
    let d = PlaceOrder::default();
    PlaceOrder {
        symbol: s(a, "symbol").unwrap(),
        qty: a["qty"].as_i64().unwrap(),
        side: s(a, "side").unwrap(),
        product: s(a, "product").unwrap_or(d.product),
        order_type: s(a, "order_type").unwrap_or(d.order_type),
        price: f(a, "price"),
        exchange: s(a, "exchange").unwrap_or(d.exchange),
        variety: s(a, "variety").unwrap_or(d.variety),
        client_id: s(a, "client_id"),
        gross_exposure: f(a, "gross_exposure"),
        series: s(a, "series"),
        tick_size: f(a, "tick_size"),
        market_protection: f(a, "market_protection"),
        reference_price: f(a, "reference_price"),
        tenant: tenant(a, "tenant"),
        plan_tenant: tenant(a, "plan_tenant"),
        fo_plan: fo_plan(a),
    }
}

fn gtt_stop(a: &Value) -> GttStop {
    GttStop {
        symbol: s(a, "symbol").unwrap(),
        qty: a["qty"].as_i64().unwrap(),
        trigger: f(a, "trigger").unwrap(),
        last_price: f(a, "last_price").unwrap(),
        exchange: s(a, "exchange").unwrap_or_else(|| "NSE".to_owned()),
        series: s(a, "series"),
        client_id: s(a, "client_id"),
        tenant: tenant(a, "tenant"),
        plan_tenant: tenant(a, "plan_tenant"),
        limit_fraction: f(a, "limit_fraction"),
        fo_plan: fo_plan(a),
    }
}

fn delete_gtt(a: &Value) -> DeleteGtt {
    DeleteGtt {
        gtt_id: a["gtt_id"].as_i64().unwrap_or(0),
        symbol: s(a, "symbol").unwrap(),
        exchange: s(a, "exchange").unwrap_or_else(|| "NSE".to_owned()),
        series: s(a, "series"),
        client_id: s(a, "client_id"),
        tenant: tenant(a, "tenant"),
        plan_tenant: tenant(a, "plan_tenant"),
    }
}

fn modify_gtt(a: &Value) -> ModifyGtt {
    ModifyGtt {
        gtt_id: a["gtt_id"].as_i64().unwrap_or(0),
        symbol: s(a, "symbol").unwrap(),
        qty: a["qty"].as_i64().unwrap(),
        trigger: f(a, "trigger").unwrap(),
        last_price: f(a, "last_price").unwrap(),
        exchange: s(a, "exchange").unwrap_or_else(|| "NSE".to_owned()),
        series: s(a, "series"),
        client_id: s(a, "client_id"),
        tenant: tenant(a, "tenant"),
        plan_tenant: tenant(a, "plan_tenant"),
        limit_fraction: f(a, "limit_fraction"),
        fo_plan: fo_plan(a),
        floor_trigger: f(a, "floor_trigger"),
    }
}

fn cancel_order(a: &Value) -> CancelOrder {
    CancelOrder {
        order_id: s(a, "order_id").unwrap(),
        symbol: s(a, "symbol").unwrap(),
        variety: s(a, "variety").unwrap_or_else(|| "regular".to_owned()),
        series: s(a, "series"),
        client_id: s(a, "client_id"),
        tenant: tenant(a, "tenant"),
        plan_tenant: tenant(a, "plan_tenant"),
    }
}

fn lines(path: &std::path::Path, drop_ts: bool) -> Vec<Value> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            let mut v: Value = serde_json::from_str(l).unwrap();
            if drop_ts && let Some(m) = v.as_object_mut() {
                m.remove("ts");
            }
            v
        })
        .collect()
}

async fn run(golden: &Value, scenario: &Value) {
    let name = s(scenario, "name").unwrap();
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("instruments")).unwrap();
    std::fs::write(
        dir.path().join("instruments/NSE.csv"),
        golden["nse_csv"].as_str().unwrap(),
    )
    .unwrap();
    std::fs::write(
        dir.path().join("instruments/NFO.csv"),
        golden["nfo_csv"].as_str().unwrap(),
    )
    .unwrap();
    let fixture = serde_json::json!({
        "session": {"authed": true}, "routes": scenario["fail"], "quotes": {}, "historical": {}
    });
    std::fs::write(dir.path().join("kite.json"), fixture.to_string()).unwrap();
    let log = dir.path().join("calls.jsonl");
    let kite: Arc<dyn KiteWrites> =
        Arc::new(ReplayKite::load(dir.path(), Some(log.clone()), "test-api-key").unwrap());
    let risk = Arc::new(
        RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
            .await
            .unwrap(),
    );
    let g = &scenario["gates"];
    let gates = ProductGates {
        dry_run: g["dry_run"].as_bool().unwrap(),
        intraday_enabled: g["intraday_enabled"].as_bool().unwrap(),
        options_enabled: g["options_enabled"].as_bool().unwrap(),
        fno_carry_enabled: g["fno_carry_enabled"].as_bool().unwrap(),
    };
    assert!(!gates.dry_run, "{name}: the live path is the point");
    let gates_fn: GatesFn = Arc::new(move || gates);
    let band = scenario
        .get("stop_band")
        .and_then(Value::as_array)
        .map(|b| StopBand::new(b[0].as_f64().unwrap(), b[1].as_f64().unwrap()).unwrap());
    let coverage = scenario
        .get("coverage")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        .then(core_coverage);
    let layer: Arc<dyn RiskLayer> = risk.clone();
    let gw = OrderGateway::new(
        kite,
        layer,
        GatewayOptions {
            gates: gates_fn,
            journal_path: dir.path().join("journal.jsonl"),
            stop_band: band,
            coverage,
            clock: clock(),
            ids: Arc::new(HarnessIds::new("exec-gateway-live")),
            limits: None,
        },
    )
    .unwrap();
    let mut results = Vec::new();
    for op in scenario["ops"].as_array().unwrap() {
        let a = &op["args"];
        let out = match op["op"].as_str().unwrap() {
            "kill" => {
                risk.on_pnl(-500_000.0).await.unwrap();
                results.push(serde_json::json!({"killed": risk.state().await.killed}));
                continue;
            }
            "place" => gw.place(&place_order(a)).await,
            "gtt" => gw.place_gtt_stop(&gtt_stop(a)).await,
            "delete" => gw.delete_gtt_value(&delete_gtt(a), &py(&a["gtt_id"])).await,
            "modify" => gw.modify_gtt_value(&modify_gtt(a), &py(&a["gtt_id"])).await,
            "cancel" => {
                gw.cancel_order_value(&cancel_order(a), &py(&a["order_id"]))
                    .await
            }
            other => panic!("unknown op {other}"),
        };
        results.push(match out {
            Ok(d) => json_of(&d),
            Err(e) => serde_json::json!({"raised": [e.python_type, e.message]}),
        });
    }
    let want = scenario["results"].as_array().unwrap();
    assert_eq!(results.len(), want.len(), "{name}");
    for (k, (got, want)) in results.iter().zip(want).enumerate() {
        assert_eq!(got, want, "{name}: operation {k} ({})", scenario["ops"][k]);
    }
    let journal = lines(&dir.path().join("journal.jsonl"), true);
    let want = scenario["journal"].as_array().unwrap();
    assert_eq!(journal.len(), want.len(), "{name}: journal {journal:#?}");
    for (k, (got, want)) in journal.iter().zip(want).enumerate() {
        assert_eq!(got, want, "{name}: journal line {k}");
    }
    let calls = lines(&log, false);
    let want = scenario["calls"].as_array().unwrap();
    assert_eq!(calls.len(), want.len(), "{name}: calls {calls:#?}");
    for (k, (got, want)) in calls.iter().zip(want).enumerate() {
        assert_eq!(got, want, "{name}: broker call {k}");
    }
}

#[tokio::test]
async fn the_live_transport_matches_python_result_for_result_line_for_line() {
    let golden: Value = serde_json::from_str(GOLDENS).unwrap();
    let scenarios = golden["scenarios"].as_array().unwrap();
    assert_eq!(scenarios.len(), 9);
    for scenario in scenarios {
        run(&golden, scenario).await;
    }
}
