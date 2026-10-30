//! Ports `tests/test_guards.py`, `packages/execution/tests/test_derivative_product_gate.py`,
//! the pure half of `test_fno_carry_gate.py` and the guard-level cases of
//! `test_fno_covered_overnight.py`. Non-negotiables 5 and 7: SGB*/G-sec untouchable at the
//! lowest layer, the product allow-list, and every refusal before any broker call — asserted
//! against a recording fake broker that is never reached.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use chrono::{NaiveDate, TimeZone as _};
use rust_decimal::Decimal;

use super::super::guards::{
    CARRY_PRODUCTS, DERIVATIVE_EXCHANGES, GuardKind, SignedLeg, assert_not_overnight_option,
    assert_overnight_option_is_covered, assert_tradeable, filter_tradeable, fo_gtt_refusal,
    fo_order_refusal, future_underlying, is_option, product_exchange_refusal,
};
use super::super::order_gateway::{GatewayOptions, OrderGateway};
use super::super::risk::{RiskBacking, RiskConfig, RiskManager};
use crate::clock::{FixedClock, ist};
use crate::fake_kite::ReplayKite;
use crate::gateway::{FoPlanRef, GttStop, PlaceOrder, ProductGates, TenantIds};
use crate::ids::RandomIds;
use crate::wire::PyValue;

// --- the pure guard (test_guards.py::TestUntouchableInstruments) ----------------------------

#[test]
fn test_named_sgb_is_blocked() {
    let e = assert_tradeable("SGBDE31III", None).unwrap_err();
    assert_eq!(e.kind, GuardKind::Untouchable);
    assert_eq!(e.kind.python_type(), "UntouchableInstrumentError");
}

#[test]
fn test_any_sgb_prefix_is_blocked() {
    for sym in ["SGBAUG28", "SGBJUN31IV", "SGBMAR30X", "SGB", "sgbnov29ii"] {
        assert!(assert_tradeable(sym, None).is_err(), "{sym}");
    }
}

#[test]
fn test_real_account_sgb_symbol_is_blocked() {
    assert!(assert_tradeable("SGBDE31III-GB", None).is_err());
    assert_eq!(
        filter_tradeable(&["SGBDE31III-GB".to_owned(), "RRKABEL".to_owned()]),
        vec!["RRKABEL".to_owned()]
    );
}

#[test]
fn test_gb_series_is_blocked() {
    assert!(assert_tradeable("RRKABEL", Some("GB")).is_err());
    assert!(assert_tradeable("ANYTHING", Some("gb")).is_err());
}

#[test]
fn test_gs_series_is_blocked() {
    assert!(assert_tradeable("SOMEGSEC", Some("GS")).is_err());
}

#[test]
fn test_whitespace_and_case_do_not_bypass() {
    for sym in ["  sgbde31iii  ", "SgBdE31III", " SGBAUG28"] {
        assert!(assert_tradeable(sym, None).is_err(), "{sym:?}");
    }
}

#[test]
fn test_rrkabel_passes() {
    assert!(assert_tradeable("RRKABEL", None).is_ok());
    assert!(assert_tradeable("RRKABEL", Some("EQ")).is_ok());
    assert!(assert_tradeable("rrkabel", Some("EQ")).is_ok());
}

#[test]
fn test_other_ordinary_symbols_pass() {
    for sym in ["DIXON", "POLYCAB", "HAL", "SBIN"] {
        assert!(assert_tradeable(sym, Some("EQ")).is_ok(), "{sym}");
    }
}

#[test]
fn test_filter_tradeable_drops_only_untouchables() {
    let got = filter_tradeable(
        &["RRKABEL", "SGBDE31III", "DIXON", "SGBAUG28", "POLYCAB"].map(str::to_owned),
    );
    assert_eq!(got, ["RRKABEL", "DIXON", "POLYCAB"].map(str::to_owned));
}

#[test]
fn the_refusal_message_is_pythons() {
    let e = assert_tradeable("SGBDE31III-GB", None).unwrap_err();
    assert_eq!(
        e.message,
        "SGBDE31III-GB: protected instrument (SGB/G-sec). This system will never trade it."
    );
}

#[test]
fn an_option_needs_a_strike_digit_before_ce_or_pe() {
    assert!(
        !is_option("RELIANCE"),
        "RELIANCE ends in CE but is not a call"
    );
    assert!(!is_option("JUSTDIAL"));
    assert!(is_option("NIFTY2692223700CE"));
    assert!(is_option(" nifty26nov25500pe "));
    assert_eq!(
        future_underlying("RELIANCE26SEPFUT").as_deref(),
        Some("RELIANCE")
    );
    assert_eq!(future_underlying("NIFTY2692223700CE"), None);
}

#[test]
fn an_option_under_a_carry_product_is_refused_in_both_directions() {
    for product in CARRY_PRODUCTS {
        let e = assert_not_overnight_option("NIFTY2692223700CE", "NFO", product).unwrap_err();
        assert_eq!(e.kind, GuardKind::OvernightOption);
        assert!(e.message.contains("past the close"), "{}", e.message);
    }
    assert!(assert_not_overnight_option("NIFTY2692223700CE", "NFO", "MIS").is_ok());
    assert!(assert_not_overnight_option("RELIANCE", "NSE", "CNC").is_ok());
}

// --- the allow-list, exhaustively (test_derivative_product_gate.py) -------------------------

const PRODUCTS: [&str; 6] = ["CNC", "MIS", "NRML", "MTF", "CO", "BO"];
const SWITCHES: [(bool, bool); 4] = [(false, false), (false, true), (true, false), (true, true)];

fn refusal(product: &str, exchange: &str, intraday: bool, options: bool) -> String {
    product_exchange_refusal(product, exchange, intraday, options, false, false)
}

#[test]
fn test_a_derivative_venue_admits_mis_only_and_only_with_both_switches() {
    for exchange in DERIVATIVE_EXCHANGES {
        for product in PRODUCTS {
            for (intraday, options) in SWITCHES {
                let why = refusal(product, exchange, intraday, options);
                assert_eq!(
                    why.is_empty(),
                    product == "MIS" && intraday && options,
                    "{product} on {exchange} intraday={intraday} options={options}: {why:?}"
                );
            }
        }
    }
}

#[test]
fn test_options_enabled_alone_no_longer_admits_mis() {
    for exchange in DERIVATIVE_EXCHANGES {
        assert!(refusal("MIS", exchange, false, true).contains("INTRADAY_ENABLED"));
    }
}

#[test]
fn test_a_carry_product_on_a_derivative_venue_is_refused_even_with_both_switches() {
    for exchange in DERIVATIVE_EXCHANGES {
        for product in CARRY_PRODUCTS {
            let why = refusal(product, exchange, true, true);
            assert!(why.contains("MIS"), "{why}");
        }
    }
}

#[test]
fn test_without_options_enabled_the_refusal_names_that_switch_first() {
    for exchange in DERIVATIVE_EXCHANGES {
        for product in PRODUCTS {
            assert!(
                refusal(product, exchange, true, false)
                    .contains("F&O/derivatives disabled (config.OPTIONS_ENABLED)")
            );
        }
    }
}

#[test]
fn test_the_cash_venues_are_unchanged() {
    for (intraday, options) in SWITCHES {
        for exchange in ["NSE", "BSE"] {
            assert_eq!(refusal("CNC", exchange, intraday, options), "");
            assert_eq!(
                refusal("MIS", exchange, intraday, options).is_empty(),
                intraday
            );
            assert!(!refusal("NRML", exchange, intraday, options).is_empty());
        }
    }
}

#[test]
fn an_unknown_venue_is_refused_by_name() {
    assert_eq!(
        refusal("CNC", "XYZ", true, true),
        "XYZ: exchange is neither cash equity (NSE/BSE) nor an enabled derivative venue"
    );
    assert_eq!(
        refusal("CNC", "", true, true),
        "'': exchange is neither cash equity (NSE/BSE) nor an enabled derivative venue"
    );
}

// --- the FO carry branch (test_fno_carry_gate.py, pure half) ---------------------------------

const EXCHANGES: [&str; 8] = ["BCD", "BFO", "CDS", "MCX", "NFO", "NSE", "BSE", "XYZ"];

#[test]
fn test_every_product_venue_and_switch_combination() {
    for exchange in EXCHANGES {
        for product in PRODUCTS {
            for bits in 0..16_u8 {
                let (intraday, options, carry, fo_plan) =
                    (bits & 1 != 0, bits & 2 != 0, bits & 4 != 0, bits & 8 != 0);
                let why =
                    product_exchange_refusal(product, exchange, intraday, options, carry, fo_plan);
                let old = refusal(product, exchange, intraday, options);
                let spec = old.is_empty()
                    || (exchange == "NFO" && product == "NRML" && options && carry && fo_plan);
                assert_eq!(
                    why.is_empty(),
                    spec,
                    "{product} {exchange} {bits:04b}: {why}"
                );
                if !(exchange == "NFO" && product == "NRML" && fo_plan && options) {
                    assert_eq!(why, old, "outside the FO pair the answer is the old one");
                }
            }
        }
    }
}

#[test]
fn test_nrml_on_nfo_with_a_plan_but_no_carry_flag_is_refused_and_says_why() {
    for intraday in [false, true] {
        let why = product_exchange_refusal("NRML", "NFO", intraday, true, false, true);
        assert!(
            why.contains("MIS") && why.contains("BASKFY_FNO_CARRY_ENABLED"),
            "{why}"
        );
    }
}

#[test]
fn test_the_carry_branch_is_nfo_only() {
    for exchange in DERIVATIVE_EXCHANGES.iter().filter(|e| **e != "NFO") {
        assert!(!product_exchange_refusal("NRML", exchange, true, true, true, true).is_empty());
    }
}

#[test]
fn test_the_carry_flag_does_not_stand_in_for_options_enabled() {
    let why = product_exchange_refusal("NRML", "NFO", true, false, true, true);
    assert!(why.contains("F&O/derivatives disabled (config.OPTIONS_ENABLED)"));
}

#[test]
fn test_the_gates_fail_closed_on_the_carry_switch() {
    let g = ProductGates::default();
    assert!(g.dry_run && !g.intraday_enabled && !g.options_enabled && !g.fno_carry_enabled);
}

fn plan_ref(plan_id: &str, sleeve: &str, step: Option<PyValue>) -> FoPlanRef {
    FoPlanRef {
        plan_id: plan_id.to_owned(),
        sleeve: sleeve.to_owned(),
        step,
        broker_positions: Vec::new(),
        plan_filled: Vec::new(),
    }
}

#[test]
fn an_fo_reference_must_match_its_order() {
    let f1 = plan_ref("P", "F1N", None);
    let f2 = plan_ref("P", "F2", None);
    assert_eq!(fo_order_refusal("NIFTY26NOV25500CE", "NFO", &f1), "");
    assert_eq!(fo_order_refusal("RELIANCE26NOVFUT", "NFO", &f2), "");
    assert!(
        !fo_order_refusal("NIFTY26NOVFUT", "NFO", &f1).is_empty(),
        "F1 trades options"
    );
    assert!(
        !fo_order_refusal("NIFTY26NOV25500CE", "NFO", &f2).is_empty(),
        "F2 trades futures"
    );
    assert!(!fo_order_refusal("X", "NFO", &plan_ref("P", "O1M", None)).is_empty());
    assert!(
        !fo_order_refusal("RELIANCE26NOVFUT", "BFO", &f2).is_empty(),
        "NFO only"
    );
    assert_eq!(
        fo_order_refusal("RELIANCE26NOVFUT", "NFO", &plan_ref("  ", "F2", None)),
        "fo_plan reference carries no plan_id"
    );
}

#[test]
fn test_an_option_gtt_is_refused_whatever_the_switches_and_the_plan() {
    for (options, carry) in SWITCHES {
        for r in [plan_ref("P", "F1N", None), plan_ref("P", "F2", None)] {
            assert!(!fo_gtt_refusal("NIFTY26NOV25500CE", "NFO", &r, options, carry).is_empty());
        }
    }
}

#[test]
fn test_an_f2_stock_future_gtt_needs_both_flags() {
    let f2 = plan_ref("FOPLAN-F2-1", "F2", None);
    for (options, carry) in SWITCHES {
        let why = fo_gtt_refusal("RELIANCE26NOVFUT", "NFO", &f2, options, carry);
        assert_eq!(why.is_empty(), options && carry, "{options} {carry}: {why}");
    }
    assert!(
        !fo_gtt_refusal("NIFTY26NOVFUT", "NFO", &f2, true, true).is_empty(),
        "index"
    );
    let f1 = plan_ref("FOPLAN-F1-1", "F1N", None);
    assert!(!fo_gtt_refusal("RELIANCE26NOVFUT", "NFO", &f1, true, true).is_empty());
}

// --- the covered-overnight guard (test_fno_covered_overnight.py, guard level) ----------------

fn leg(strike: i64, kind: &str, qty: i64) -> SignedLeg {
    SignedLeg {
        underlying: "NIFTY".to_owned(),
        expiry: NaiveDate::from_ymd_opt(2026, 11, 24).unwrap(),
        option_type: kind.to_owned(),
        strike: Decimal::from(strike),
        quantity: qty,
    }
}

#[test]
fn test_an_option_order_whose_reference_names_no_step_is_refused() {
    let r = plan_ref("P", "F1N", None);
    let rule = super::super::guards::core_coverage();
    let e = assert_overnight_option_is_covered("NIFTY26NOV25500CE", 75, "SELL", &r, Some(&rule))
        .unwrap_err();
    assert_eq!(e.kind, GuardKind::UncoveredOption);
    assert!(
        e.is_overnight_option(),
        "a subclass of OvernightOptionError"
    );
}

#[test]
fn test_no_predicate_wired_refuses_every_fo_option_order() {
    let r = plan_ref("P", "F1N", Some(leg(25500, "CE", 75).to_value()));
    let e =
        assert_overnight_option_is_covered("NIFTY26NOV25500CE", 75, "BUY", &r, None).unwrap_err();
    assert!(
        e.message.contains("no covered-overnight rule"),
        "{}",
        e.message
    );
}

#[test]
fn a_naked_short_is_refused_and_a_covered_one_admitted() {
    let rule = super::super::guards::core_coverage();
    let naked = plan_ref("P", "F1N", Some(leg(25500, "CE", -75).to_value()));
    assert!(
        assert_overnight_option_is_covered("NIFTY26NOV25500CE", 75, "SELL", &naked, Some(&rule))
            .is_err()
    );
    let mut covered = plan_ref("P", "F1N", Some(leg(25500, "CE", -75).to_value()));
    covered.broker_positions = vec![leg(25700, "CE", 75).to_value()];
    assert!(
        assert_overnight_option_is_covered("NIFTY26NOV25500CE", 75, "SELL", &covered, Some(&rule))
            .is_ok()
    );
    // Cover must be FURTHER from the money: a long call below the short does not cover it.
    let mut inside = plan_ref("P", "F1N", Some(leg(25500, "CE", -75).to_value()));
    inside.broker_positions = vec![leg(25300, "CE", 75).to_value()];
    assert!(
        assert_overnight_option_is_covered("NIFTY26NOV25500CE", 75, "SELL", &inside, Some(&rule))
            .is_err()
    );
}

#[test]
fn test_a_step_that_is_not_the_order_is_refused() {
    let rule = super::super::guards::core_coverage();
    let mut r = plan_ref("P", "F1N", Some(leg(25500, "CE", -75).to_value()));
    r.broker_positions = vec![leg(25700, "CE", 75).to_value()];
    for (symbol, qty, side) in [
        ("NIFTY26NOV25500CE", 50, "SELL"),
        ("NIFTY26NOV25500CE", 75, "BUY"),
        ("NIFTY26NOV25600CE", 75, "SELL"),
        ("NIFTY26NOV25500PE", 75, "SELL"),
        ("BANKNIFTY26NOV25500CE", 75, "SELL"),
        ("NIFTYNXT5026NOV25500CE", 75, "SELL"),
        ("NIFTY26N24125500CE", 75, "SELL"),
    ] {
        assert!(
            assert_overnight_option_is_covered(symbol, qty, side, &r, Some(&rule)).is_err(),
            "{symbol} {qty} {side}"
        );
    }
}

// --- inside the gateway, before any network call --------------------------------------------

const KITE_FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;

struct Rig {
    _dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
    gw: OrderGateway,
}

async fn rig(gates: ProductGates, cfg: RiskConfig) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("kite.json"), KITE_FIXTURE).unwrap();
    std::fs::create_dir(dir.path().join("instruments")).unwrap();
    let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
    let clock = Arc::new(FixedClock::new(
        ist().with_ymd_and_hms(2026, 10, 30, 10, 0, 0).unwrap(),
    ));
    let risk = RiskManager::new(cfg, clock.clone(), RiskBacking::Memory)
        .await
        .unwrap();
    let gw = OrderGateway::new(
        kite.clone(),
        Arc::new(risk),
        GatewayOptions {
            gates: Arc::new(move || gates),
            journal_path: dir.path().join("journal.jsonl"),
            stop_band: None,
            coverage: None,
            clock,
            ids: Arc::new(RandomIds),
            limits: None,
        },
    )
    .unwrap();
    Rig {
        _dir: dir,
        kite,
        gw,
    }
}

fn gates(dry_run: bool, intraday: bool, options: bool) -> ProductGates {
    ProductGates {
        dry_run,
        intraday_enabled: intraday,
        options_enabled: options,
        fno_carry_enabled: false,
    }
}

fn order(symbol: &str, qty: i64, side: &str, price: f64) -> PlaceOrder {
    let tenant = TenantIds::new(1, 1).unwrap();
    PlaceOrder {
        symbol: symbol.to_owned(),
        qty,
        side: side.to_owned(),
        price: Some(price),
        gross_exposure: Some(0.0),
        tenant: Some(tenant),
        plan_tenant: Some(tenant),
        ..PlaceOrder::default()
    }
}

fn status(out: &crate::wire::PyDict) -> String {
    match out.get("status") {
        Some(PyValue::Str(s)) => s.clone(),
        other => panic!("no status: {other:?}"),
    }
}

fn error(out: &crate::wire::PyDict) -> String {
    match out.get("error") {
        Some(PyValue::Str(s)) => s.clone(),
        other => format!("{other:?}"),
    }
}

fn big() -> RiskConfig {
    RiskConfig {
        max_position_value: 1e12,
        ..RiskConfig::default()
    }
}

#[tokio::test]
async fn test_gateway_blocks_before_any_broker_call() {
    for dry_run in [true, false] {
        let r = rig(gates(dry_run, false, false), RiskConfig::default()).await;
        let out =
            r.gw.place(&order("SGBDE31III", 1, "SELL", 7400.0))
                .await
                .unwrap();
        assert_eq!(status(&out), "BLOCKED");
        let mut gb = order("RRKABEL", 1, "BUY", 2275.0);
        gb.series = Some("GB".to_owned());
        assert_eq!(status(&r.gw.place(&gb).await.unwrap()), "BLOCKED");
        assert!(r.kite.write_calls().is_empty(), "dry_run={dry_run}");
    }
}

#[tokio::test]
async fn test_gateway_accepts_ordinary_equity_in_dry_run_without_reaching_the_broker() {
    let r = rig(gates(true, false, false), RiskConfig::default()).await;
    let mut o = order("RRKABEL", 10, "BUY", 2275.0);
    o.series = Some("EQ".to_owned());
    let out = r.gw.place(&o).await.unwrap();
    assert_eq!(status(&out), "DRY_RUN");
    assert_eq!(out.get("symbol"), Some(&PyValue::Str("RRKABEL".to_owned())));
    assert!(
        r.kite.write_calls().is_empty(),
        "DRY_RUN never reaches the broker"
    );
}

#[tokio::test]
async fn test_mis_and_fno_are_blocked_cleanly() {
    let r = rig(gates(false, false, false), big()).await;
    let mut mis = order("DIXON", 10, "BUY", 639.4);
    mis.product = "MIS".to_owned();
    let out = r.gw.place(&mis).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(error(&out).contains("INTRADAY_ENABLED"), "{}", error(&out));
    let mut fno = order("NIFTY26AUGCE", 50, "BUY", 120.0);
    fno.product = "NRML".to_owned();
    fno.exchange = "NFO".to_owned();
    let out = r.gw.place(&fno).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(error(&out).contains("OPTIONS_ENABLED"), "{}", error(&out));
    assert!(r.kite.write_calls().is_empty());
}

#[tokio::test]
async fn test_an_untouchable_instrument_never_reaches_the_broker_when_live() {
    let r = rig(gates(false, false, false), big()).await;
    let out =
        r.gw.place(&order("SGBDE31III-GB", 1, "SELL", 15306.0))
            .await
            .unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(
        r.kite.write_calls().is_empty(),
        "a protected instrument was sent"
    );
}

#[tokio::test]
async fn test_a_legitimate_cnc_order_reaches_the_recording_broker_only_when_live() {
    let r = rig(gates(false, false, false), big()).await;
    let out = r.gw.place(&order("DIXON", 10, "BUY", 639.4)).await.unwrap();
    assert_ne!(status(&out), "BLOCKED", "{out:?}");
    let calls = r.kite.write_calls();
    assert_eq!(calls.len(), 1);
    let Some(PyValue::Map(p)) = &calls[0].params else {
        panic!("no params")
    };
    assert_eq!(p.get("product"), Some(&PyValue::Str("CNC".to_owned())));
    assert_eq!(p.get("exchange"), Some(&PyValue::Str("NSE".to_owned())));
    assert_eq!(p.get("quantity"), Some(&PyValue::Int(10)));
}

#[tokio::test]
async fn test_derivative_orders_inside_the_gateway() {
    // NFO MIS option with options but not intraday.
    let r = rig(gates(false, false, true), RiskConfig::default()).await;
    let mut o = order("NIFTY2692223700CE", 65, "BUY", 100.0);
    o.product = "MIS".to_owned();
    o.exchange = "NFO".to_owned();
    let out = r.gw.place(&o).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(error(&out).contains("INTRADAY_ENABLED"));
    // NRML future with both switches on.
    let r = rig(gates(false, true, true), RiskConfig::default()).await;
    let mut f = order("NIFTY26SEPFUT", 65, "BUY", 100.0);
    f.product = "NRML".to_owned();
    f.exchange = "NFO".to_owned();
    let out = r.gw.place(&f).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(error(&out).contains("MIS"));
    // NRML option: the overnight guard refuses first.
    let mut n = order("NIFTY2692223700CE", 65, "BUY", 100.0);
    n.product = "NRML".to_owned();
    n.exchange = "NFO".to_owned();
    let out = r.gw.place(&n).await.unwrap();
    assert_eq!(status(&out), "BLOCKED");
    assert!(error(&out).contains("past the close"), "{}", error(&out));
    assert!(r.kite.write_calls().is_empty());
    // The one admitted shape, under DRY_RUN: simulated, never sent.
    let r = rig(gates(true, true, true), RiskConfig::default()).await;
    let mut m = order("NIFTY2692223700CE", 65, "BUY", 100.0);
    m.product = "MIS".to_owned();
    m.exchange = "NFO".to_owned();
    let out = r.gw.place(&m).await.unwrap();
    assert_ne!(status(&out), "BLOCKED", "{out:?}");
    assert!(r.kite.write_calls().is_empty());
}

#[tokio::test]
async fn test_a_gtt_on_a_derivative_venue_or_an_untouchable_is_refused() {
    let r = rig(gates(false, true, true), RiskConfig::default()).await;
    let tenant = TenantIds::new(1, 1).unwrap();
    for (symbol, exchange) in [("NIFTY26SEPFUT", "NFO"), ("SGBDE31III-GB", "NSE")] {
        let out =
            r.gw.place_gtt_stop(&GttStop {
                symbol: symbol.to_owned(),
                qty: 65,
                trigger: 89.0,
                last_price: 100.0,
                exchange: exchange.to_owned(),
                series: None,
                client_id: None,
                tenant: Some(tenant),
                plan_tenant: Some(tenant),
                limit_fraction: None,
                fo_plan: None,
            })
            .await
            .unwrap();
        assert_eq!(status(&out), "BLOCKED", "{symbol}: {out:?}");
    }
    assert!(r.kite.write_calls().is_empty());
    assert!(
        r.kite.calls().is_empty(),
        "not even a read before the refusal"
    );
}

#[test]
fn test_config_defines_both_gates_and_both_default_off() {
    let s = crate::settings::Settings::defaults().unwrap();
    assert!(!s.intraday_enabled);
    assert!(!s.options_enabled);
    assert!(s.dry_run, "DRY_RUN is the default");
}

// --- review R8 (exec_guards): no overflow panic on an absurd quantity ------------------------

#[test]
fn an_order_quantity_of_i64_min_is_refused_not_a_panic() {
    let rule = super::super::guards::core_coverage();
    let r = plan_ref("P", "F1N", Some(leg(25500, "CE", -75).to_value()));
    let e =
        assert_overnight_option_is_covered("NIFTY26NOV25500CE", i64::MIN, "SELL", &r, Some(&rule))
            .unwrap_err();
    assert_eq!(e.kind, GuardKind::UncoveredOption);
    assert!(
        e.message.contains("but the plan step is -75"),
        "{}",
        e.message
    );
}

#[test]
fn the_guard_patterns_compile() {
    // The patterns sit behind `LazyLock<Option<Regex>>`; a pattern that failed to compile would
    // make `is_option` false and the overnight guard fail OPEN. Pin all three.
    assert!(is_option("NIFTY26NOV25500CE"));
    assert_eq!(
        future_underlying("RELIANCE26SEPFUT").as_deref(),
        Some("RELIANCE")
    );
    let r = plan_ref("P", "F1N", Some(leg(25500, "CE", -75).to_value()));
    let rule = super::super::guards::core_coverage();
    let e = assert_overnight_option_is_covered("NIFTYXX25500CE", 75, "SELL", &r, Some(&rule))
        .unwrap_err();
    assert!(e.message.contains("does not read as"), "{}", e.message);
}
