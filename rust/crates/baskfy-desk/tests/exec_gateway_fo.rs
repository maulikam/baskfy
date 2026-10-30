//! Lane **exec_gateway** (wave D2): the gateway halves of the execution package's tests that wave
//! D left unported — `decile-blueprint/packages/execution/tests/`:
//!
//! * `test_fno_carry_gate.py` — the gateway half (the pure allow-list half is
//!   `exec_guards/tests_guards.rs`): every product × switch on NFO through the gateway, the
//!   tenant clause first, the F2 stock-future GTT the one derivative GTT, an option GTT never.
//! * `test_fno_covered_overnight.py` — `assert_overnight_option_is_covered` **in the gateway**,
//!   with the core predicate wired (`guards::core_coverage`), against a recording broker.
//! * `test_fno_gtt_trail_modify.py` — F2's evening trail through `modify_gtt_quantity`.
//! * `test_gtt_limit_fraction.py` — `place_gtt_stop(limit_fraction=...)`.
//! * `test_gtt_gateway.py` — the cases `exec_gateway_gtt.rs` did not carry: untouchable and
//!   G-sec refusals, the symbol-less cancel, tenancy, the F&O switch on a GTT, DRY_RUN, the 8–12 %
//!   band, the rate-limit slots, the journal's terminal outcomes and the placed record, the
//!   status vocabulary and the unnameable-but-created trigger.
//!
//! The broker is the replay Kite (records every call, sends nothing); `kc.calls == []` in Python
//! is `bed.routes().is_empty()` here — not even the instrument dump was read. Never a live call.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use baskfy_core::fno::covered::{OptionPosition, net_of_plan, uncovered};
use baskfy_core::fno::vol::OptionType;
use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{
    DeleteGtt, FoPlanRef, GTT_LIMIT_FRACTION, GatesFn, GttStop, ModifyGtt, PlaceOrder,
    ProductGates, StopBand, TenantIds, gtt_status,
};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::kite::{GttParams, KiteError, KiteReads, KiteWrites, OrderParams};
use baskfy_desk::lanes::exec_core::guards::{CoverageRule, SignedLeg, core_coverage};
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::ratelimit::GatewayLimits;
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskLayer, RiskManager};
use baskfy_desk::wire::{PyDict, PyValue};
use baskfy_providers::secret::Secret;
use chrono::{NaiveDate, TimeZone};
use rust_decimal::Decimal;

const STOCK_FUT: &str = "RELIANCE26NOVFUT";
const INDEX_FUT: &str = "NIFTY26NOVFUT";
const OPTION: &str = "NIFTY26NOV25500CE";

/// `test_fno_carry_gate.SpyKC.instruments`: the same two rows whatever the exchange.
const FO_CSV: &str = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n\
11,11,RELIANCE26NOVFUT,RELIANCE,0,,0,0.1,250,FUT,NFO-FUT,NFO\n\
738561,2885,RELIANCE,RELIANCE,0,,0,0.05,1,EQ,NSE,NSE\n";

/// `test_gtt_gateway.INSTRUMENTS`.
const NSE_CSV: &str = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n\
738561,2885,RELIANCE,RELIANCE,0,,0,0.05,1,EQ,NSE,NSE\n\
2748929,10738,OFSS,OFSS,0,,0,1.0,1,EQ,NSE,NSE\n\
1,1,SONACOMS,SONACOMS,0,,0,0.1,1,EQ,NSE,NSE\n\
2,2,NOTICK,NOTICK,0,,0,0,1,EQ,NSE,NSE\n";

fn tenant() -> TenantIds {
    TenantIds::new(1, 1).unwrap()
}

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

/// `itertools.product((False, True), repeat=4)`.
fn four() -> Vec<(bool, bool, bool, bool)> {
    let mut out = Vec::new();
    for a in [false, true] {
        for b in [false, true] {
            for c in [false, true] {
                for d in [false, true] {
                    out.push((a, b, c, d));
                }
            }
        }
    }
    out
}

/// A limiter that counts `api_slot` (Python patches `gw.limits.api_slot`) and never waits.
#[derive(Default)]
struct Counting {
    api: AtomicUsize,
    orders: AtomicUsize,
}

#[async_trait]
impl GatewayLimits for Counting {
    async fn order_slot(&self) {
        self.orders.fetch_add(1, Ordering::SeqCst);
    }

    async fn api_slot(&self) {
        self.api.fetch_add(1, Ordering::SeqCst);
    }
}

struct Bed {
    dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
}

impl Bed {
    fn new(fail: &[&str], files: &[(&str, &str)]) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let routes: serde_json::Map<String, serde_json::Value> = fail
            .iter()
            .map(|route| {
                (
                    (*route).to_owned(),
                    serde_json::json!({"error": {"type": "InputException", "message": format!("broker refused {route}"), "code": 400}}),
                )
            })
            .collect();
        let fixture = serde_json::json!({
            "session": {"authed": true}, "routes": routes, "quotes": {}, "historical": {}
        });
        std::fs::write(dir.path().join("kite.json"), fixture.to_string()).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        for (exchange, csv) in files {
            std::fs::write(dir.path().join(format!("instruments/{exchange}.csv")), csv).unwrap();
        }
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        Self { dir, kite }
    }

    /// `test_fno_carry_gate`'s and `test_fno_gtt_trail_modify`'s broker.
    fn fo() -> Self {
        Self::new(&[], &[("NFO", FO_CSV), ("NSE", FO_CSV), ("BFO", FO_CSV)])
    }

    /// `test_gtt_gateway`'s broker.
    fn nse(fail: &[&str]) -> Self {
        Self::new(fail, &[("NSE", NSE_CSV)])
    }

    async fn build(
        &self,
        gates: ProductGates,
        coverage: Option<CoverageRule>,
        limits: Option<Arc<dyn GatewayLimits>>,
        risk: Option<Arc<dyn RiskLayer>>,
    ) -> OrderGateway {
        let kite: Arc<dyn KiteWrites> = self.kite.clone();
        self.build_on(kite, gates, coverage, limits, risk).await
    }

    async fn build_on(
        &self,
        kite: Arc<dyn KiteWrites>,
        gates: ProductGates,
        coverage: Option<CoverageRule>,
        limits: Option<Arc<dyn GatewayLimits>>,
        risk: Option<Arc<dyn RiskLayer>>,
    ) -> OrderGateway {
        let gates_fn: GatesFn = Arc::new(move || gates);
        let risk = match risk {
            Some(r) => r,
            None => fresh_risk().await,
        };
        OrderGateway::new(
            kite,
            risk,
            GatewayOptions {
                gates: gates_fn,
                journal_path: self.dir.path().join("journal.jsonl"),
                stop_band: None,
                coverage,
                clock: clock(),
                ids: Arc::new(HarnessIds::new("exec-gateway-fo")),
                limits,
            },
        )
        .unwrap()
    }

    fn routes(&self) -> Vec<String> {
        self.kite.calls().into_iter().map(|c| c.route).collect()
    }

    fn journal_text(&self) -> String {
        std::fs::read_to_string(self.dir.path().join("journal.jsonl")).unwrap_or_default()
    }

    fn journal(&self) -> Vec<serde_json::Value> {
        self.journal_text()
            .lines()
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

    /// The `orders` legs of every `gtt.place` / `gtt.modify` call, parsed.
    fn gtt_legs(&self, route: &str) -> Vec<Vec<serde_json::Value>> {
        self.kite
            .calls()
            .into_iter()
            .filter(|c| c.route == route)
            .map(|c| {
                let Some(PyValue::Map(p)) = c.params else {
                    panic!("no params")
                };
                let Some(PyValue::Str(orders)) = p.get("orders") else {
                    panic!("no orders")
                };
                serde_json::from_str::<Vec<serde_json::Value>>(orders).unwrap()
            })
            .collect()
    }

    fn gtt_condition(&self, route: &str) -> Vec<serde_json::Value> {
        self.kite
            .calls()
            .into_iter()
            .filter(|c| c.route == route)
            .map(|c| {
                let Some(PyValue::Map(p)) = c.params else {
                    panic!("no params")
                };
                let Some(PyValue::Str(cond)) = p.get("condition") else {
                    panic!("no condition")
                };
                serde_json::from_str(cond).unwrap()
            })
            .collect()
    }

    fn placed_orders(&self) -> Vec<PyDict> {
        self.kite
            .calls()
            .into_iter()
            .filter(|c| c.route == "order.place")
            .map(|c| match c.params {
                Some(PyValue::Map(p)) => p,
                _ => panic!("no params"),
            })
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

fn gates(dry_run: bool, intraday: bool, options: bool, carry: bool) -> ProductGates {
    ProductGates {
        dry_run,
        intraday_enabled: intraday,
        options_enabled: options,
        fno_carry_enabled: carry,
    }
}

fn plan_ref(plan_id: &str, sleeve: &str) -> FoPlanRef {
    FoPlanRef {
        plan_id: plan_id.to_owned(),
        sleeve: sleeve.to_owned(),
        step: None,
        broker_positions: Vec::new(),
        plan_filled: Vec::new(),
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

/// `_spec` of `test_fno_carry_gate.py`: the OP2 allow-list plus the one NRML/NFO branch.
fn spec(
    product: &str,
    exchange: &str,
    intraday: bool,
    options: bool,
    carry: bool,
    fo: bool,
) -> bool {
    let old = baskfy_desk::lanes::exec_core::guards::product_exchange_refusal(
        product, exchange, intraday, options, false, false,
    );
    old.is_empty() || (exchange == "NFO" && product == "NRML" && options && carry && fo)
}

const PRODUCTS: [&str; 6] = ["CNC", "MIS", "NRML", "MTF", "CO", "BO"];

// ================================================================================
// test_fno_carry_gate.py — inside the gateway
// ================================================================================

#[tokio::test]
async fn carry_an_nfo_future_through_the_gateway() {
    for product in PRODUCTS {
        for (intraday, options, carry, with_plan) in four() {
            let bed = Bed::fo();
            let gw = bed
                .build(gates(false, intraday, options, carry), None, None, None)
                .await;
            let out = gw
                .place(&PlaceOrder {
                    symbol: STOCK_FUT.to_owned(),
                    qty: 250,
                    side: "BUY".to_owned(),
                    product: product.to_owned(),
                    order_type: "LIMIT".to_owned(),
                    price: Some(10.0),
                    exchange: "NFO".to_owned(),
                    gross_exposure: Some(0.0),
                    client_id: Some(format!("FOPLAN-F2-1:{STOCK_FUT}")),
                    tenant: Some(tenant()),
                    plan_tenant: Some(tenant()),
                    fo_plan: with_plan.then(|| plan_ref("FOPLAN-F2-1", "F2")),
                    ..PlaceOrder::default()
                })
                .await
                .unwrap();
            let case = format!("{product} {intraday} {options} {carry} {with_plan}");
            if spec(product, "NFO", intraday, options, carry, with_plan) {
                assert_eq!(status(&out), "PLACED", "{case}: {out:?}");
                assert_eq!(bed.routes(), vec!["order.place".to_owned()], "{case}");
                assert_eq!(
                    bed.placed_orders()[0].get("product"),
                    Some(&PyValue::Str(product.to_owned())),
                    "{case}"
                );
            } else {
                assert_eq!(status(&out), "BLOCKED", "{case}: {out:?}");
                assert!(bed.routes().is_empty(), "{case}");
            }
        }
    }
}

#[tokio::test]
async fn carry_the_multi_tenant_clause_refuses_an_fo_order_first() {
    let bed = Bed::fo();
    let gw = bed
        .build(gates(false, false, true, true), None, None, None)
        .await;
    let out = gw
        .place(&PlaceOrder {
            symbol: STOCK_FUT.to_owned(),
            qty: 250,
            side: "BUY".to_owned(),
            product: "NRML".to_owned(),
            price: Some(10.0),
            exchange: "NFO".to_owned(),
            gross_exposure: Some(0.0),
            tenant: Some(tenant()),
            plan_tenant: Some(TenantIds::new(2, 1).unwrap()),
            fo_plan: Some(plan_ref("FOPLAN-F2-1", "F2")),
            ..PlaceOrder::default()
        })
        .await
        .unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("tenant mismatch"));
    assert!(bed.routes().is_empty());
}

fn fo_arm(symbol: &str, fo_plan: Option<FoPlanRef>) -> GttStop {
    GttStop {
        symbol: symbol.to_owned(),
        qty: 250,
        trigger: 89.0,
        last_price: 100.0,
        exchange: "NFO".to_owned(),
        series: None,
        client_id: Some(format!("PLAN:{symbol}")),
        tenant: Some(tenant()),
        plan_tenant: Some(tenant()),
        limit_fraction: None,
        fo_plan,
    }
}

#[tokio::test]
async fn carry_an_option_gtt_is_refused_whatever_the_switches_and_the_plan() {
    for plan in [
        None,
        Some(plan_ref("FOPLAN-F1-1", "F1N")),
        Some(plan_ref("FOPLAN-F2-1", "F2")),
    ] {
        for (dry_run, intraday, options, carry) in four() {
            let bed = Bed::fo();
            let gw = bed
                .build(gates(dry_run, intraday, options, carry), None, None, None)
                .await;
            let out = gw
                .place_gtt_stop(&fo_arm(OPTION, plan.clone()))
                .await
                .unwrap();
            assert_eq!(
                status(&out),
                "BLOCKED",
                "{plan:?} {dry_run} {intraday} {options} {carry}"
            );
            assert!(bed.routes().is_empty());
        }
    }
}

#[tokio::test]
async fn carry_an_f2_stock_future_gtt_needs_both_flags() {
    for (dry_run, intraday, options, carry) in four() {
        let bed = Bed::fo();
        let gw = bed
            .build(gates(dry_run, intraday, options, carry), None, None, None)
            .await;
        let out = gw
            .place_gtt_stop(&fo_arm(STOCK_FUT, Some(plan_ref("FOPLAN-F2-1", "F2"))))
            .await
            .unwrap();
        let case = format!("{dry_run} {intraday} {options} {carry}");
        if !(options && carry) {
            assert_eq!(status(&out), "BLOCKED", "{case}: {out:?}");
            assert!(bed.routes().is_empty(), "{case}");
            continue;
        }
        if dry_run {
            assert_eq!(status(&out), gtt_status::DRY_RUN_GTT, "{case}");
            assert!(bed.routes().is_empty(), "{case}");
            continue;
        }
        assert_eq!(status(&out), gtt_status::GTT_PLACED, "{case}: {out:?}");
        let placed = bed.gtt_legs("gtt.place");
        assert_eq!(placed.len(), 1);
        assert_eq!(placed[0][0]["product"], "NRML");
        assert_eq!(placed[0][0]["transaction_type"], "SELL");
    }
}

#[tokio::test]
async fn carry_every_other_nfo_gtt_is_refused_with_both_flags_on() {
    for (symbol, plan) in [
        (INDEX_FUT, Some(plan_ref("FOPLAN-F2-1", "F2"))),
        (STOCK_FUT, Some(plan_ref("FOPLAN-F1-1", "F1N"))),
        (STOCK_FUT, None),
        (STOCK_FUT, Some(plan_ref("", "F2"))),
        (STOCK_FUT, Some(plan_ref("P", "O1M"))),
    ] {
        let bed = Bed::fo();
        let gw = bed
            .build(gates(false, true, true, true), None, None, None)
            .await;
        let out = gw
            .place_gtt_stop(&fo_arm(symbol, plan.clone()))
            .await
            .unwrap();
        assert_eq!(status(&out), "BLOCKED", "{symbol} {plan:?}: {out:?}");
        assert!(bed.routes().is_empty());
    }
}

#[tokio::test]
async fn carry_an_f2_reference_does_not_open_a_gtt_on_another_venue() {
    let bed = Bed::fo();
    let gw = bed
        .build(gates(false, false, true, true), None, None, None)
        .await;
    let mut stop = fo_arm(STOCK_FUT, Some(plan_ref("FOPLAN-F2-1", "F2")));
    stop.exchange = "BFO".to_owned();
    stop.client_id = None;
    let out = gw.place_gtt_stop(&stop).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn carry_a_cash_gtt_is_byte_for_byte_what_it_was() {
    let bed = Bed::fo();
    let gw = bed
        .build(gates(false, false, true, true), None, None, None)
        .await;
    let out = gw
        .place_gtt_stop(&GttStop {
            symbol: "RELIANCE".to_owned(),
            qty: 10,
            trigger: 89.0,
            last_price: 100.0,
            exchange: "NSE".to_owned(),
            series: None,
            client_id: None,
            tenant: Some(tenant()),
            plan_tenant: Some(tenant()),
            limit_fraction: None,
            fo_plan: None,
        })
        .await
        .unwrap();
    assert_eq!(status(&out), gtt_status::GTT_PLACED);
    assert_eq!(bed.gtt_legs("gtt.place")[0][0]["product"], "CNC");
}

// ================================================================================
// test_fno_covered_overnight.py — the guard in the gateway, every switch on, not DRY_RUN
// ================================================================================

const LOT: i64 = 75;

fn expiry() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 11, 24).unwrap()
}

fn opt(kind: OptionType, strike: i64, quantity: i64) -> OptionPosition {
    OptionPosition {
        underlying: "NIFTY".to_owned(),
        expiry: expiry(),
        option_type: kind,
        strike: Decimal::from(strike),
        quantity,
    }
}

fn long_put() -> OptionPosition {
    opt(OptionType::Pe, 23500, LOT)
}
fn long_call() -> OptionPosition {
    opt(OptionType::Ce, 25500, LOT)
}
fn short_put() -> OptionPosition {
    opt(OptionType::Pe, 24000, -LOT)
}
fn short_call() -> OptionPosition {
    opt(OptionType::Ce, 25000, -LOT)
}
/// `04` §2: longs enter before shorts.
fn entry() -> Vec<OptionPosition> {
    vec![long_put(), long_call(), short_put(), short_call()]
}
/// A short call with no long above it anywhere in the condor.
fn naked() -> OptionPosition {
    opt(OptionType::Ce, 26000, -LOT)
}

fn kind_text(k: OptionType) -> &'static str {
    match k {
        OptionType::Ce => "CE",
        OptionType::Pe => "PE",
    }
}

fn symbol_of(leg: &OptionPosition) -> String {
    format!(
        "{}26NOV{}{}",
        leg.underlying,
        leg.strike,
        kind_text(leg.option_type)
    )
}

fn wire(leg: &OptionPosition) -> PyValue {
    SignedLeg {
        underlying: leg.underlying.clone(),
        expiry: leg.expiry,
        option_type: kind_text(leg.option_type).to_owned(),
        strike: leg.strike,
        quantity: leg.quantity,
    }
    .to_value()
}

async fn cover_gateway(bed: &Bed, wired: bool, dry_run: bool) -> OrderGateway {
    bed.build(
        gates(dry_run, true, true, true),
        wired.then(core_coverage),
        None,
        None,
    )
    .await
}

#[derive(Default)]
struct Opts<'a> {
    broker: &'a [OptionPosition],
    filled: &'a [OptionPosition],
    product: Option<&'a str>,
    plan_id: Option<&'a str>,
    symbol: Option<&'a str>,
    qty: Option<i64>,
    side: Option<&'a str>,
}

async fn send(gw: &OrderGateway, leg: &OptionPosition, o: Opts<'_>) -> PyDict {
    let sym = o.symbol.map_or_else(|| symbol_of(leg), str::to_owned);
    let plan_id = o.plan_id.unwrap_or("FOPLAN-1");
    gw.place(&PlaceOrder {
        symbol: sym.clone(),
        qty: o.qty.unwrap_or(leg.quantity.abs()),
        side: o
            .side
            .unwrap_or(if leg.quantity > 0 { "BUY" } else { "SELL" })
            .to_owned(),
        product: o.product.unwrap_or("NRML").to_owned(),
        order_type: "LIMIT".to_owned(),
        price: Some(10.0),
        exchange: "NFO".to_owned(),
        gross_exposure: Some(0.0),
        client_id: Some(format!("{plan_id}:{sym}")),
        tenant: Some(tenant()),
        plan_tenant: Some(tenant()),
        fo_plan: Some(FoPlanRef {
            plan_id: plan_id.to_owned(),
            sleeve: "F1N".to_owned(),
            step: Some(wire(leg)),
            broker_positions: o.broker.iter().map(wire).collect(),
            plan_filled: o.filled.iter().map(wire).collect(),
        }),
        ..PlaceOrder::default()
    })
    .await
    .unwrap()
}

#[tokio::test]
async fn covered_the_entry_sequence_is_admitted_step_by_step() {
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, true, false).await;
    let legs = entry();
    for k in 0..legs.len() {
        let out = send(
            &gw,
            &legs[k],
            Opts {
                filled: &legs[..k],
                ..Opts::default()
            },
        )
        .await;
        assert_eq!(status(&out), "PLACED", "{:?}: {out:?}", legs[k]);
    }
    let sent = bed.placed_orders();
    let products: Vec<_> = sent.iter().map(|p| p.get("product").cloned()).collect();
    assert_eq!(products, vec![Some(PyValue::Str("NRML".to_owned())); 4]);
    let symbols: Vec<_> = sent
        .iter()
        .map(|p| p.get("tradingsymbol").cloned())
        .collect();
    let want: Vec<_> = legs
        .iter()
        .map(|l| Some(PyValue::Str(symbol_of(l))))
        .collect();
    assert_eq!(symbols, want);
}

#[tokio::test]
async fn covered_a_naked_short_is_refused_at_every_prefix() {
    let legs = entry();
    let cover = [(short_put(), long_put()), (short_call(), long_call())];
    for prefix in 0..=legs.len() {
        let bed = Bed::fo();
        let gw = cover_gateway(&bed, true, false).await;
        let filled = &legs[..prefix];
        let out = send(
            &gw,
            &naked(),
            Opts {
                filled,
                ..Opts::default()
            },
        )
        .await;
        assert_eq!(status(&out), "BLOCKED", "{out:?}");
        assert!(text(&out, "error").contains("uncovered"));
        let plan = format!("FOPLAN-{prefix}");
        for (short, long) in &cover {
            if filled.contains(long) || filled.contains(short) {
                continue;
            }
            let out = send(
                &gw,
                short,
                Opts {
                    filled,
                    plan_id: Some(&plan),
                    ..Opts::default()
                },
            )
            .await;
            assert_eq!(status(&out), "BLOCKED", "{short:?}: {out:?}");
        }
        assert!(bed.routes().is_empty(), "prefix {prefix}");
    }
}

#[tokio::test]
async fn covered_shorts_first_is_refused_at_the_first_step() {
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, true, false).await;
    for leg in [short_put(), short_call()] {
        assert_eq!(status(&send(&gw, &leg, Opts::default()).await), "BLOCKED");
    }
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn covered_a_partly_filled_long_covers_only_what_filled() {
    for partial in [1, 25, 50, 74] {
        let bed = Bed::fo();
        let gw = cover_gateway(&bed, true, false).await;
        let mut lp = long_put();
        lp.quantity = partial;
        let filled = [lp];
        let out = send(
            &gw,
            &short_put(),
            Opts {
                filled: &filled,
                ..Opts::default()
            },
        )
        .await;
        assert_eq!(status(&out), "BLOCKED");
        assert!(bed.routes().is_empty());
        let mut sp = short_put();
        sp.quantity = -partial;
        let out = send(
            &gw,
            &sp,
            Opts {
                filled: &filled,
                plan_id: Some("FOPLAN-2"),
                ..Opts::default()
            },
        )
        .await;
        assert_eq!(status(&out), "PLACED", "{partial}: {out:?}");
    }
}

#[tokio::test]
async fn covered_the_exit_is_shorts_first_and_a_long_sold_early_is_refused() {
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, true, false).await;
    let book = entry();
    let mut early_leg = long_call();
    early_leg.quantity = -LOT;
    let early = send(
        &gw,
        &early_leg,
        Opts {
            broker: &book,
            plan_id: Some("EXIT-0"),
            ..Opts::default()
        },
    )
    .await;
    assert_eq!(status(&early), "BLOCKED");
    assert!(bed.routes().is_empty());
    let flip = |mut p: OptionPosition| {
        p.quantity = -p.quantity;
        p
    };
    let exits = [
        flip(short_call()),
        flip(short_put()),
        flip(long_call()),
        flip(long_put()),
    ];
    for k in 0..exits.len() {
        let out = send(
            &gw,
            &exits[k],
            Opts {
                broker: &book,
                filled: &exits[..k],
                plan_id: Some("EXIT-1"),
                ..Opts::default()
            },
        )
        .await;
        assert_eq!(status(&out), "PLACED", "{:?}: {out:?}", exits[k]);
    }
}

#[tokio::test]
async fn covered_cover_must_be_same_underlying_expiry_and_type_and_further_out() {
    let mut other_underlying = long_call();
    other_underlying.underlying = "BANKNIFTY".to_owned();
    let mut other_expiry = long_call();
    other_expiry.expiry = NaiveDate::from_ymd_opt(2026, 12, 29).unwrap();
    let mut other_type = long_call();
    other_type.option_type = OptionType::Pe;
    other_type.strike = Decimal::from(23500);
    let mut nearer = long_call();
    nearer.strike = Decimal::from(24800);
    for cover in [other_underlying, other_expiry, other_type, nearer] {
        let bed = Bed::fo();
        let gw = cover_gateway(&bed, true, false).await;
        let broker = [cover.clone()];
        let out = send(
            &gw,
            &short_call(),
            Opts {
                broker: &broker,
                ..Opts::default()
            },
        )
        .await;
        assert_eq!(status(&out), "BLOCKED", "{cover:?}");
        assert!(bed.routes().is_empty());
    }
}

#[tokio::test]
async fn covered_a_filled_long_is_counted_once_not_twice() {
    let filled = [long_call()];
    let raw_after_fill = [long_call()];
    let mut double = short_call();
    double.quantity = -2 * LOT;
    // The test bites: the naive composition would have called this covered.
    let naive: Vec<OptionPosition> = raw_after_fill
        .iter()
        .chain(filled.iter())
        .chain(std::iter::once(&double))
        .cloned()
        .collect();
    assert!(uncovered(naive.iter()).is_empty());
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, true, false).await;
    let broker = net_of_plan(&raw_after_fill, &filled);
    let out = send(
        &gw,
        &double,
        Opts {
            broker: &broker,
            filled: &filled,
            ..Opts::default()
        },
    )
    .await;
    assert_eq!(status(&out), "BLOCKED", "{out:?}");
    assert!(bed.routes().is_empty());
    let out = send(
        &gw,
        &short_call(),
        Opts {
            broker: &broker,
            filled: &filled,
            ..Opts::default()
        },
    )
    .await;
    assert_eq!(status(&out), "PLACED", "{out:?}");
}

#[tokio::test]
async fn covered_a_broker_book_that_lags_a_fill_only_makes_the_guard_stricter() {
    let filled = [long_call()];
    let net = net_of_plan(&[], &filled);
    let mut want = long_call();
    want.quantity = -LOT;
    assert_eq!(net, vec![want]);
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, true, false).await;
    let out = send(
        &gw,
        &short_call(),
        Opts {
            broker: &net,
            filled: &filled,
            ..Opts::default()
        },
    )
    .await;
    assert_eq!(status(&out), "BLOCKED");
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn covered_a_step_that_is_not_the_order_is_refused() {
    let cases: [(Option<&str>, Option<i64>, Option<&str>); 7] = [
        (None, Some(150), None),
        (None, None, Some("BUY")),
        (Some("NIFTY26NOV25100CE"), None, None),
        (Some("NIFTY26NOV25000PE"), None, None),
        (Some("BANKNIFTY26NOV25000CE"), None, None),
        (Some("NIFTYNXT5026NOV25000CE"), None, None),
        (Some("NIFTY26N24125000CE"), None, None),
    ];
    for (symbol, qty, side) in cases {
        let bed = Bed::fo();
        let gw = cover_gateway(&bed, true, false).await;
        let broker = [long_call()];
        let out = send(
            &gw,
            &short_call(),
            Opts {
                broker: &broker,
                symbol,
                qty,
                side,
                ..Opts::default()
            },
        )
        .await;
        assert_eq!(
            status(&out),
            "BLOCKED",
            "{symbol:?} {qty:?} {side:?}: {out:?}"
        );
        assert!(bed.routes().is_empty());
    }
}

#[tokio::test]
async fn covered_no_predicate_wired_refuses_every_fo_option_order() {
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, false, false).await;
    let out = send(&gw, &long_put(), Opts::default()).await;
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("no covered-overnight rule"));
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn covered_an_f1_reference_on_a_future_or_an_unknown_sleeve_is_refused() {
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, true, false).await;
    let lp = symbol_of(&long_put());
    for (sleeve, symbol) in [("F1N", "NIFTY26NOVFUT"), ("F2", lp.as_str()), ("O1M", "X")] {
        let out = gw
            .place(&PlaceOrder {
                symbol: symbol.to_owned(),
                qty: LOT,
                side: "BUY".to_owned(),
                product: "NRML".to_owned(),
                price: Some(10.0),
                exchange: "NFO".to_owned(),
                gross_exposure: Some(0.0),
                tenant: Some(tenant()),
                plan_tenant: Some(tenant()),
                fo_plan: Some(FoPlanRef {
                    step: Some(wire(&long_put())),
                    ..plan_ref("P", sleeve)
                }),
                ..PlaceOrder::default()
            })
            .await
            .unwrap();
        assert_eq!(status(&out), "BLOCKED", "{sleeve}: {out:?}");
    }
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn covered_an_fo_option_under_mis_still_meets_the_cover_rule() {
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, true, false).await;
    let out = send(
        &gw,
        &naked(),
        Opts {
            product: Some("MIS"),
            ..Opts::default()
        },
    )
    .await;
    assert_eq!(status(&out), "BLOCKED");
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn covered_a_dry_run_fo_leg_is_simulated_and_journalled_with_its_plan() {
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, true, true).await;
    let out = send(&gw, &long_put(), Opts::default()).await;
    assert_eq!(status(&out), "DRY_RUN");
    assert!(bed.routes().is_empty());
    assert!(bed.journal_text().contains("\"fo_plan\": \"FOPLAN-1\""));
}

#[tokio::test]
async fn covered_an_uncovered_refusal_is_journalled() {
    let bed = Bed::fo();
    let gw = cover_gateway(&bed, true, false).await;
    send(&gw, &naked(), Opts::default()).await;
    let text = bed.journal_text();
    assert!(text.contains("\"event\": \"fo_uncovered_block\""));
    assert!(text.contains(&format!(
        "\"client_id\": \"FOPLAN-1:{}\"",
        symbol_of(&naked())
    )));
}

#[tokio::test]
async fn covered_an_o_sleeve_option_under_a_carry_product_is_still_refused_by_the_old_guard() {
    for product in ["NRML", "CNC"] {
        let bed = Bed::fo();
        let gw = cover_gateway(&bed, true, false).await;
        let out = gw
            .place(&PlaceOrder {
                symbol: "NIFTY2692223700CE".to_owned(),
                qty: LOT,
                side: "BUY".to_owned(),
                product: product.to_owned(),
                price: Some(10.0),
                exchange: "NFO".to_owned(),
                gross_exposure: Some(0.0),
                client_id: Some("O1M-PLAN:NIFTY2692223700CE".to_owned()),
                tenant: Some(tenant()),
                plan_tenant: Some(tenant()),
                ..PlaceOrder::default()
            })
            .await
            .unwrap();
        assert_eq!(status(&out), "BLOCKED");
        assert!(text(&out, "error").contains("past the close"));
        assert!(bed.routes().is_empty());
        assert!(
            bed.journal_text()
                .contains("\"event\": \"overnight_option_block\"")
        );
    }
}

// ================================================================================
// test_fno_gtt_trail_modify.py — F2's evening trail moves its GTT through the gateway
// ================================================================================

fn trail(symbol: &str, trigger: f64, floor: Option<f64>, fo_plan: Option<FoPlanRef>) -> ModifyGtt {
    ModifyGtt {
        gtt_id: 4242,
        symbol: symbol.to_owned(),
        qty: 500,
        trigger,
        last_price: 1100.0,
        exchange: "NFO".to_owned(),
        series: None,
        client_id: None,
        tenant: Some(tenant()),
        plan_tenant: Some(tenant()),
        limit_fraction: None,
        fo_plan,
        floor_trigger: floor,
    }
}

fn f2() -> FoPlanRef {
    plan_ref("F2-20261027-RELIANCE", "F2")
}

fn f1() -> FoPlanRef {
    plan_ref("F1N-20261027-NIFTY", "F1N")
}

async fn move_stop(gw: &OrderGateway, m: &ModifyGtt) -> PyDict {
    gw.modify_gtt_value(m, &PyValue::Int(m.gtt_id))
        .await
        .unwrap()
}

#[tokio::test]
async fn trail_a_paper_trail_is_journalled_nrml_and_reaches_no_broker() {
    let bed = Bed::fo();
    let gw = bed
        .build(gates(true, false, true, true), None, None, None)
        .await;
    let out = move_stop(&gw, &trail(STOCK_FUT, 1040.0, Some(1000.0), Some(f2()))).await;
    assert_eq!(status(&out), gtt_status::DRY_RUN_GTT_MODIFY);
    assert!(bed.routes().is_empty());
    let rows = bed.journal();
    let last = rows.last().unwrap();
    assert_eq!(last["event"], "gtt_dry_run_modify");
    assert_eq!(last["product"], "NRML");
    assert_eq!(last["fo_plan"], "F2-20261027-RELIANCE");
    assert_eq!(last["floor_trigger"], 1000.0);
}

#[tokio::test]
async fn trail_with_both_flags_the_trigger_moves_and_the_leg_stays_nrml() {
    let bed = Bed::fo();
    let gw = bed
        .build(gates(false, false, true, true), None, None, None)
        .await;
    let out = move_stop(&gw, &trail(STOCK_FUT, 1040.0, Some(1000.0), Some(f2()))).await;
    assert_eq!(status(&out), gtt_status::GTT_MODIFIED, "{out:?}");
    let sent = bed.gtt_legs("gtt.modify");
    assert_eq!(sent.len(), 1);
    assert_eq!(
        bed.gtt_condition("gtt.modify")[0]["trigger_values"],
        serde_json::json!([1040.0])
    );
    assert_eq!(sent[0][0]["product"], "NRML");
}

#[tokio::test]
async fn trail_a_lower_trigger_is_refused_before_anything_is_sent() {
    for dry_run in [false, true] {
        let bed = Bed::fo();
        let gw = bed
            .build(gates(dry_run, false, true, true), None, None, None)
            .await;
        let out = move_stop(&gw, &trail(STOCK_FUT, 990.0, Some(1000.0), Some(f2()))).await;
        assert_eq!(status(&out), "BLOCKED");
        assert!(text(&out, "error").contains("never lowered"));
        assert!(bed.routes().is_empty());
    }
}

#[tokio::test]
async fn trail_an_fo_modify_must_name_the_trigger_it_replaces() {
    let bed = Bed::fo();
    let gw = bed
        .build(gates(false, false, true, true), None, None, None)
        .await;
    let out = move_stop(&gw, &trail(STOCK_FUT, 1040.0, None, Some(f2()))).await;
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("never lowered"));
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn trail_every_switch_combination() {
    for (dry_run, intraday, options, carry) in four() {
        let bed = Bed::fo();
        let gw = bed
            .build(gates(dry_run, intraday, options, carry), None, None, None)
            .await;
        let out = move_stop(&gw, &trail(STOCK_FUT, 1040.0, Some(1000.0), Some(f2()))).await;
        let case = format!("{dry_run} {intraday} {options} {carry}");
        if !(options && carry) {
            assert_eq!(status(&out), "BLOCKED", "{case}: {out:?}");
            assert!(bed.routes().is_empty(), "{case}");
        } else if dry_run {
            assert_eq!(status(&out), gtt_status::DRY_RUN_GTT_MODIFY, "{case}");
            assert!(bed.routes().is_empty(), "{case}");
        } else {
            assert_eq!(status(&out), gtt_status::GTT_MODIFIED, "{case}");
        }
    }
}

#[tokio::test]
async fn trail_only_an_f2_stock_future_may_move_its_stop() {
    for (symbol, plan) in [
        (OPTION, f2()),
        (OPTION, f1()),
        (INDEX_FUT, f2()),
        (STOCK_FUT, f1()),
    ] {
        let bed = Bed::fo();
        let gw = bed
            .build(gates(false, false, true, true), None, None, None)
            .await;
        let out = move_stop(
            &gw,
            &trail(symbol, 1040.0, Some(1000.0), Some(plan.clone())),
        )
        .await;
        assert_eq!(status(&out), "BLOCKED", "{symbol} {plan:?}: {out:?}");
        assert!(bed.routes().is_empty());
    }
}

// ================================================================================
// test_gtt_limit_fraction.py
// ================================================================================

const SWING_LIMIT_FRACTION: f64 = 0.97;

fn nse_arm(symbol: &str, qty: i64, trigger: f64, last: f64) -> GttStop {
    GttStop {
        symbol: symbol.to_owned(),
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
    }
}

fn weekly_gates(dry_run: bool) -> ProductGates {
    ProductGates {
        dry_run,
        ..ProductGates::default()
    }
}

fn one_leg(bed: &Bed) -> serde_json::Value {
    let legs = bed.gtt_legs("gtt.place");
    assert_eq!(legs.len(), 1);
    assert_eq!(legs[0].len(), 1);
    legs[0][0].clone()
}

#[tokio::test]
async fn fraction_the_swing_fraction_lands_as_the_gtts_limit() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let mut stop = nse_arm("RELIANCE", 10, 89.0, 100.0);
    stop.limit_fraction = Some(SWING_LIMIT_FRACTION);
    let out = gw.place_gtt_stop(&stop).await.unwrap();
    assert_eq!(status(&out), gtt_status::GTT_PLACED);
    assert_eq!(f(&out, "trigger"), 89.0);
    assert_eq!(f(&out, "limit"), 86.35);
    let leg = one_leg(&bed);
    assert_eq!(leg["price"], 86.35);
    assert!(leg["price"].as_f64().unwrap() < f(&out, "trigger"));
    let placed: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_placed")
        .collect();
    assert_eq!(placed.len(), 1);
    assert_eq!(placed[0]["limit"], 86.35);
    assert_eq!(placed[0]["limit_fraction"], SWING_LIMIT_FRACTION);
}

#[tokio::test]
async fn fraction_the_swing_fraction_is_snapped_to_a_coarse_tick_too() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let mut stop = nse_arm("OFSS", 3, 2515.1, 2750.0);
    stop.limit_fraction = Some(SWING_LIMIT_FRACTION);
    let out = gw.place_gtt_stop(&stop).await.unwrap();
    assert_eq!(status(&out), gtt_status::GTT_PLACED);
    assert_eq!(f(&out, "trigger"), 2515.0);
    assert_eq!(f(&out, "limit"), 2440.0);
    assert_eq!(one_leg(&bed)["price"], 2440.0);
}

#[tokio::test]
async fn fraction_omitting_the_keyword_keeps_the_weekly_books_half_percent() {
    assert!((GTT_LIMIT_FRACTION - 0.995).abs() < f64::EPSILON);
    for fraction in [None, Some(GTT_LIMIT_FRACTION)] {
        let bed = Bed::nse(&[]);
        let gw = bed.build(weekly_gates(false), None, None, None).await;
        let mut stop = nse_arm("RELIANCE", 10, 89.0, 100.0);
        stop.limit_fraction = fraction;
        let out = gw.place_gtt_stop(&stop).await.unwrap();
        assert_eq!(status(&out), gtt_status::GTT_PLACED);
        assert_eq!(f(&out, "limit"), 88.55);
        assert_eq!(one_leg(&bed)["price"], 88.55);
        let placed: Vec<_> = bed
            .journal()
            .into_iter()
            .filter(|r| r["event"] == "gtt_placed")
            .collect();
        assert_eq!(placed.len(), 1);
        assert_eq!(placed[0]["limit_fraction"], GTT_LIMIT_FRACTION);
    }
}

#[tokio::test]
async fn fraction_a_dry_run_records_the_fraction_it_would_have_used() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(true), None, None, None).await;
    let mut stop = nse_arm("RELIANCE", 10, 89.0, 100.0);
    stop.limit_fraction = Some(SWING_LIMIT_FRACTION);
    let out = gw.place_gtt_stop(&stop).await.unwrap();
    assert_eq!(status(&out), gtt_status::DRY_RUN_GTT);
    assert!(bed.routes().is_empty(), "a dry run reaches no broker");
    let dry: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_dry_run")
        .collect();
    assert_eq!(dry.len(), 1);
    assert_eq!(dry[0]["limit_fraction"], SWING_LIMIT_FRACTION);

    let weekly = Bed::nse(&[]);
    let gw = weekly.build(weekly_gates(true), None, None, None).await;
    gw.place_gtt_stop(&nse_arm("RELIANCE", 10, 89.0, 100.0))
        .await
        .unwrap();
    let dry: Vec<_> = weekly
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_dry_run")
        .collect();
    assert_eq!(dry.len(), 1);
    assert_eq!(dry[0]["limit_fraction"], GTT_LIMIT_FRACTION);
}

#[tokio::test]
async fn fraction_that_would_rest_the_limit_above_the_trigger_is_a_caller_bug() {
    for fraction in [0.0, -0.5, 1.03, 2.0] {
        let bed = Bed::nse(&[]);
        let gw = bed.build(weekly_gates(false), None, None, None).await;
        let mut stop = nse_arm("RELIANCE", 10, 89.0, 100.0);
        stop.limit_fraction = Some(fraction);
        let e = gw.place_gtt_stop(&stop).await.unwrap_err();
        assert_eq!(e.python_type, "ValueError");
        assert!(e.message.contains("limit_fraction"), "{e}");
        assert!(bed.routes().is_empty());
        assert!(bed.journal().is_empty());
    }
}

#[tokio::test]
async fn fraction_exactly_one_is_allowed_and_rests_the_limit_on_the_trigger() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let mut stop = nse_arm("RELIANCE", 10, 89.0, 100.0);
    stop.limit_fraction = Some(1.0);
    let out = gw.place_gtt_stop(&stop).await.unwrap();
    assert_eq!(status(&out), gtt_status::GTT_PLACED);
    assert_eq!(f(&out, "limit"), 89.0);
    assert_eq!(f(&out, "trigger"), 89.0);
    assert_eq!(one_leg(&bed)["price"], 89.0);
}

// ================================================================================
// test_gtt_gateway.py — the cases exec_gateway_gtt.rs did not carry
// ================================================================================

fn cancel(symbol: &str) -> DeleteGtt {
    DeleteGtt {
        gtt_id: 4242,
        symbol: symbol.to_owned(),
        exchange: "NSE".to_owned(),
        series: None,
        client_id: None,
        tenant: Some(caller()),
        plan_tenant: Some(caller()),
    }
}

async fn do_cancel(gw: &OrderGateway, r: &DeleteGtt) -> PyDict {
    gw.delete_gtt_value(r, &PyValue::Int(r.gtt_id))
        .await
        .unwrap()
}

#[tokio::test]
async fn gtt_the_gateway_can_create_and_destroy_a_gtt() {
    // Both halves are methods of the one gateway (the `Gateway` trait names all five verbs).
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(true), None, None, None).await;
    let made = gw
        .place_gtt_stop(&nse_arm("RELIANCE", 10, 89.0, 100.0))
        .await
        .unwrap();
    let gone = do_cancel(&gw, &cancel("RELIANCE")).await;
    assert_eq!(status(&made), gtt_status::DRY_RUN_GTT);
    assert_eq!(status(&gone), gtt_status::DRY_RUN_GTT_DELETE);
}

#[tokio::test]
async fn gtt_an_untouchable_symbol_is_refused_a_gtt_before_the_broker_is_reached() {
    for symbol in ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"] {
        let bed = Bed::nse(&[]);
        let gw = bed.build(weekly_gates(false), None, None, None).await;
        let out = gw
            .place_gtt_stop(&nse_arm(symbol, 10, 89.0, 100.0))
            .await
            .unwrap();
        assert_eq!(status(&out), "BLOCKED");
        assert!(text(&out, "error").contains("protected instrument"));
        assert!(bed.routes().is_empty(), "{symbol}");
    }
}

#[tokio::test]
async fn gtt_a_g_sec_series_is_refused_a_gtt_before_the_broker_is_reached() {
    for series in ["GB", "GS"] {
        let bed = Bed::nse(&[]);
        let gw = bed.build(weekly_gates(false), None, None, None).await;
        let mut stop = nse_arm("SOMEBOND", 10, 89.0, 100.0);
        stop.series = Some(series.to_owned());
        let out = gw.place_gtt_stop(&stop).await.unwrap();
        assert_eq!(status(&out), "BLOCKED");
        assert!(text(&out, "error").contains("protected instrument"));
        assert!(bed.routes().is_empty());
    }
}

#[tokio::test]
async fn gtt_an_untouchable_gtt_cannot_be_cancelled_around_the_guard_either() {
    for symbol in ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"] {
        let bed = Bed::nse(&[]);
        let gw = bed.build(weekly_gates(false), None, None, None).await;
        let out = do_cancel(&gw, &cancel(symbol)).await;
        assert_eq!(status(&out), "BLOCKED");
        assert!(text(&out, "error").contains("protected instrument"));
        assert!(bed.routes().is_empty());
    }
}

#[tokio::test]
async fn gtt_a_cancel_without_a_symbol_is_refused_rather_than_run_unguarded() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let out = do_cancel(&gw, &cancel("")).await;
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("without naming its instrument"));
    assert!(bed.routes().is_empty());
    assert!(bed.events().contains(&"gtt_delete_block".to_owned()));
}

#[tokio::test]
async fn gtt_a_cross_tenant_gtt_and_cancel_are_blocked_not_raised() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let mut stop = nse_arm("RELIANCE", 10, 89.0, 100.0);
    stop.plan_tenant = Some(other());
    let out = gw.place_gtt_stop(&stop).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("tenant mismatch"));
    let mut c = cancel("RELIANCE");
    c.plan_tenant = Some(other());
    let out = do_cancel(&gw, &c).await;
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("tenant mismatch"));
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn gtt_an_fno_gtt_is_blocked_while_options_are_disabled() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let mut stop = nse_arm("NIFTY25SEPFUT", 10, 89.0, 100.0);
    stop.exchange = "NFO".to_owned();
    let out = gw.place_gtt_stop(&stop).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(text(&out, "error").contains("F&O/derivatives disabled"));
    assert!(bed.routes().is_empty());
}

#[tokio::test]
async fn gtt_an_option_gtt_is_refused_the_stricter_way_with_options_off_or_on() {
    for options in [false, true] {
        let bed = Bed::nse(&[]);
        let g = ProductGates {
            dry_run: false,
            options_enabled: options,
            ..ProductGates::default()
        };
        let gw = bed.build(g, None, None, None).await;
        let mut stop = nse_arm("NIFTY25SEP24000CE", 10, 89.0, 100.0);
        stop.exchange = "NFO".to_owned();
        let out = gw.place_gtt_stop(&stop).await.unwrap();
        assert_eq!(status(&out), "BLOCKED");
        assert!(text(&out, "error").contains("past the close"));
        assert!(bed.routes().is_empty());
        if options {
            assert!(
                bed.events()
                    .contains(&"gtt_overnight_option_block".to_owned())
            );
        }
    }
}

#[tokio::test]
async fn gtt_dry_run_arms_and_cancels_nothing_and_still_answers() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(true), None, None, None).await;
    let out = gw
        .place_gtt_stop(&nse_arm("RELIANCE", 10, 89.0, 100.0))
        .await
        .unwrap();
    assert_eq!(status(&out), gtt_status::DRY_RUN_GTT);
    assert_eq!(f(&out, "trigger"), 89.0);
    assert_eq!(out.get("qty"), Some(&PyValue::Int(10)));
    assert!(bed.events().contains(&"gtt_dry_run".to_owned()));
    let out = do_cancel(&gw, &cancel("RELIANCE")).await;
    assert_eq!(status(&out), gtt_status::DRY_RUN_GTT_DELETE);
    assert!(bed.events().contains(&"gtt_dry_run_delete".to_owned()));
    gw.place_gtt_stop(&nse_arm("OFSS", 1, 2515.1, 2750.0))
        .await
        .unwrap();
    assert!(bed.routes().is_empty(), "DRY_RUN reached the broker");
}

#[tokio::test]
async fn gtt_a_stop_inside_the_band_raises_no_finding() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    gw.place_gtt_stop(&nse_arm("RELIANCE", 10, 89.0, 100.0))
        .await
        .unwrap();
    assert!(!bed.events().contains(&"gtt_band_warning".to_owned()));
}

#[tokio::test]
async fn gtt_a_stop_outside_the_band_is_journalled_and_still_armed() {
    for (trigger, finding) in [(99.5, "too_close"), (80.0, "too_far")] {
        let bed = Bed::nse(&[]);
        let gw = bed.build(weekly_gates(false), None, None, None).await;
        let out = gw
            .place_gtt_stop(&nse_arm("RELIANCE", 10, trigger, 100.0))
            .await
            .unwrap();
        assert_eq!(status(&out), gtt_status::GTT_PLACED);
        assert!(bed.routes().contains(&"gtt.place".to_owned()));
        let warnings: Vec<_> = bed
            .journal()
            .into_iter()
            .filter(|r| r["event"] == "gtt_band_warning")
            .collect();
        assert!(!warnings.is_empty());
        assert_eq!(warnings[0]["finding"], finding);
    }
}

#[tokio::test]
async fn gtt_the_band_boundaries_are_inside_the_band() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let mut a = nse_arm("RELIANCE", 10, 92.0, 100.0);
    a.client_id = Some("min".to_owned());
    let mut b = nse_arm("RELIANCE", 10, 88.0, 100.0);
    b.client_id = Some("max".to_owned());
    gw.place_gtt_stop(&a).await.unwrap();
    gw.place_gtt_stop(&b).await.unwrap();
    assert!(!bed.events().contains(&"gtt_band_warning".to_owned()));
}

#[test]
fn gtt_the_band_is_injectable_and_refuses_an_inverted_one() {
    assert!((StopBand::default().min_pct() - 0.08).abs() < f64::EPSILON);
    assert!((StopBand::default().max_pct() - 0.12).abs() < f64::EPSILON);
    let e = StopBand::new(0.2, 0.1).unwrap_err();
    assert!(e.to_string().contains("inverted or out of range"));
}

#[tokio::test]
async fn gtt_every_gtt_takes_a_rate_limit_slot() {
    let bed = Bed::nse(&[]);
    let counting = Arc::new(Counting::default());
    let limits: Arc<dyn GatewayLimits> = counting.clone();
    let gw = bed
        .build(weekly_gates(false), None, Some(limits), None)
        .await;
    let mut a = nse_arm("RELIANCE", 10, 89.0, 100.0);
    a.client_id = Some("a".to_owned());
    gw.place_gtt_stop(&a).await.unwrap();
    // One slot for the GTT, one for the cold instrument dump.
    assert_eq!(counting.api.load(Ordering::SeqCst), 2);
    do_cancel(&gw, &cancel("RELIANCE")).await;
    assert_eq!(counting.api.load(Ordering::SeqCst), 3);
    assert_eq!(
        counting.orders.load(Ordering::SeqCst),
        0,
        "a stop is not an order"
    );
}

#[tokio::test]
async fn gtt_a_refused_gtt_spends_no_rate_limit_slot() {
    let bed = Bed::nse(&[]);
    let counting = Arc::new(Counting::default());
    let limits: Arc<dyn GatewayLimits> = counting.clone();
    let gw = bed
        .build(weekly_gates(false), None, Some(limits), None)
        .await;
    let out = gw
        .place_gtt_stop(&nse_arm("SGBJUN29", 10, 89.0, 100.0))
        .await
        .unwrap();
    assert_eq!(status(&out), "BLOCKED");
    gw.place_gtt_stop(&nse_arm("RELIANCE", 10, 200.0, 100.0))
        .await
        .unwrap();
    assert_eq!(counting.api.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn gtt_a_cancel_reaches_the_broker_with_an_integer_trigger_id() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let out = do_cancel(&gw, &cancel("RELIANCE")).await;
    assert_eq!(status(&out), gtt_status::GTT_DELETED);
    assert_eq!(out.get("gtt_id"), Some(&PyValue::Int(4242)));
    let deletes: Vec<_> = bed
        .kite
        .calls()
        .into_iter()
        .filter(|c| c.route == "gtt.delete")
        .collect();
    assert_eq!(deletes.len(), 1);
    assert_eq!(
        deletes[0]
            .url_args
            .as_ref()
            .and_then(|u| u.get("trigger_id")),
        Some(&PyValue::Int(4242))
    );
}

#[tokio::test]
async fn gtt_a_broker_refusal_to_arm_is_reported_not_swallowed() {
    let bed = Bed::nse(&["gtt.place"]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let out = gw
        .place_gtt_stop(&nse_arm("RELIANCE", 10, 89.0, 100.0))
        .await
        .unwrap();
    assert_eq!(status(&out), gtt_status::GTT_ERROR);
    assert!(text(&out, "error").contains("broker refused gtt.place"));
    assert!(
        status(&out) != gtt_status::GTT_PLACED && status(&out) != gtt_status::DRY_RUN_GTT,
        "not in GTT_PLACED_STATUSES"
    );
    assert_eq!(out.get("reached_exchange"), Some(&PyValue::None));
    let errors: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_error")
        .collect();
    assert!(!errors.is_empty());
    assert_eq!(errors[0]["stage"], "place_gtt");
}

#[tokio::test]
async fn gtt_every_terminal_gtt_outcome_is_journalled() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let mut ok = nse_arm("RELIANCE", 10, 89.0, 100.0);
    ok.client_id = Some("ok".to_owned());
    gw.place_gtt_stop(&ok).await.unwrap();
    do_cancel(&gw, &cancel("RELIANCE")).await;
    let mut bad = nse_arm("OFSS", 10, 2749.6, 2750.0);
    bad.client_id = Some("bad".to_owned());
    gw.place_gtt_stop(&bad).await.unwrap();
    let seen = bed.events();
    for e in ["gtt_placed", "gtt_deleted", "gtt_block"] {
        assert!(seen.contains(&e.to_owned()), "{e} missing from {seen:?}");
    }
}

#[tokio::test]
async fn gtt_the_placed_record_carries_what_slippage_and_coverage_are_measured_from() {
    let bed = Bed::nse(&[]);
    let gw = bed.build(weekly_gates(false), None, None, None).await;
    let out = gw
        .place_gtt_stop(&nse_arm("RELIANCE", 10, 89.0, 100.0))
        .await
        .unwrap();
    let placed: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_placed")
        .collect();
    assert_eq!(placed.len(), 1);
    let p = &placed[0];
    assert_eq!(p["symbol"], "RELIANCE");
    // Python's SpyKC answers 777; the replay Kite answers 900000 + n. The record carries the
    // broker's id, whatever it is.
    assert_eq!(p["gtt_id"], 900_001);
    assert_eq!(out.get("gtt_id"), Some(&PyValue::Int(900_001)));
    assert_eq!(p["qty"], 10);
    assert_eq!(p["trigger"], 89.0);
    assert_eq!(p["limit"], 88.55);
    assert_eq!(p["last_price"], 100.0);
    assert_eq!(p["drop_pct"], 11.0);
    assert!(p.get("ts").is_some());
}

#[test]
fn gtt_the_status_strings_are_the_ones_the_desk_route_already_matches_on() {
    assert_eq!(gtt_status::GTT_PLACED, "GTT_PLACED");
    assert_eq!(gtt_status::DRY_RUN_GTT, "DRY_RUN_GTT");
    assert_eq!(gtt_status::GTT_ERROR, "GTT_ERROR");
    assert_eq!(gtt_status::GTT_DELETED, "GTT_DELETED");
    assert_eq!(gtt_status::DRY_RUN_GTT_DELETE, "DRY_RUN_GTT_DELETE");
    assert_eq!(gtt_status::GTT_DELETE_ERROR, "GTT_DELETE_ERROR");
}

/// The replay Kite with one change: `place_gtt` succeeds (and is recorded) but answers `{}` —
/// `SpyKC(trigger_id=None)`.
struct Unnamed(Arc<ReplayKite>);

#[async_trait]
impl KiteReads for Unnamed {
    fn login_url(&self) -> String {
        self.0.login_url()
    }
    fn set_access_token(&self, token: &Secret) {
        self.0.set_access_token(token);
    }
    async fn generate_session(&self, r: &str, s: &Secret) -> Result<PyValue, KiteError> {
        self.0.generate_session(r, s).await
    }
    async fn profile(&self) -> Result<PyValue, KiteError> {
        self.0.profile().await
    }
    async fn holdings(&self) -> Result<PyValue, KiteError> {
        self.0.holdings().await
    }
    async fn positions(&self) -> Result<PyValue, KiteError> {
        self.0.positions().await
    }
    async fn margins(&self, segment: Option<&str>) -> Result<PyValue, KiteError> {
        self.0.margins(segment).await
    }
    async fn ltp(&self, i: &[String]) -> Result<PyValue, KiteError> {
        self.0.ltp(i).await
    }
    async fn ohlc(&self, i: &[String]) -> Result<PyValue, KiteError> {
        self.0.ohlc(i).await
    }
    async fn quote(&self, i: &[String]) -> Result<PyValue, KiteError> {
        self.0.quote(i).await
    }
    async fn instruments(&self, exchange: Option<&str>) -> Result<PyValue, KiteError> {
        self.0.instruments(exchange).await
    }
    async fn historical_data(
        &self,
        token: i64,
        from: &str,
        to: &str,
        interval: &str,
        continuous: bool,
        oi: bool,
    ) -> Result<PyValue, KiteError> {
        self.0
            .historical_data(token, from, to, interval, continuous, oi)
            .await
    }
    async fn orders(&self) -> Result<PyValue, KiteError> {
        self.0.orders().await
    }
    async fn order_history(&self, id: &str) -> Result<PyValue, KiteError> {
        self.0.order_history(id).await
    }
    async fn trades(&self) -> Result<PyValue, KiteError> {
        self.0.trades().await
    }
    async fn get_gtts(&self) -> Result<PyValue, KiteError> {
        self.0.get_gtts().await
    }
    async fn basket_order_margins(
        &self,
        params: &[PyValue],
        consider_positions: bool,
        mode: Option<&str>,
    ) -> Result<PyValue, KiteError> {
        self.0
            .basket_order_margins(params, consider_positions, mode)
            .await
    }
}

#[async_trait]
impl KiteWrites for Unnamed {
    async fn place_order(&self, order: &OrderParams) -> Result<String, KiteError> {
        self.0.place_order(order).await
    }
    async fn modify_order(
        &self,
        variety: &str,
        order_id: &str,
        params: &PyDict,
    ) -> Result<String, KiteError> {
        self.0.modify_order(variety, order_id, params).await
    }
    async fn cancel_order(
        &self,
        variety: &str,
        order_id: &str,
        parent: Option<&str>,
    ) -> Result<String, KiteError> {
        self.0.cancel_order(variety, order_id, parent).await
    }
    async fn place_gtt(&self, gtt: &GttParams) -> Result<PyValue, KiteError> {
        self.0.place_gtt(gtt).await?;
        Ok(PyValue::Map(PyDict::new()))
    }
    async fn modify_gtt(&self, id: i64, gtt: &GttParams) -> Result<PyValue, KiteError> {
        self.0.modify_gtt(id, gtt).await
    }
    async fn delete_gtt(&self, id: i64) -> Result<PyValue, KiteError> {
        self.0.delete_gtt(id).await
    }
}

#[tokio::test]
async fn gtt_an_unnameable_but_created_trigger_is_reported_as_placed() {
    let bed = Bed::nse(&[]);
    let kite: Arc<dyn KiteWrites> = Arc::new(Unnamed(bed.kite.clone()));
    let gw = bed
        .build_on(kite, weekly_gates(false), None, None, None)
        .await;
    let mut stop = nse_arm("RELIANCE", 10, 89.0, 100.0);
    stop.client_id = Some("P:X".to_owned());
    let out = gw.place_gtt_stop(&stop).await.unwrap();
    assert_eq!(status(&out), gtt_status::GTT_PLACED);
    assert_eq!(out.get("gtt_id"), Some(&PyValue::None));
    let placed: Vec<_> = bed
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_placed")
        .collect();
    assert_eq!(placed.len(), 1);
    assert!(
        placed[0]["warning"]
            .as_str()
            .unwrap_or("")
            .contains("cannot be addressed")
    );
    // And it is still deduplicated, so nothing re-arms it by accident.
    let again = gw.place_gtt_stop(&stop).await.unwrap();
    assert_eq!(status(&again), "DUPLICATE");
    assert_eq!(bed.routes().iter().filter(|r| *r == "gtt.place").count(), 1);
}
