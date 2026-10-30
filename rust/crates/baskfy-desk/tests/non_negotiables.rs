//! The non-negotiables the foundation already carries, as tests (DECISIONS-RS R8.0): the
//! confirm gate and the 30-minute expiry, `client_id = plan_id:symbol`, law 2's tenant clause,
//! fail-closed product gates, the stop band, and law 2 in the types (no lane but exec_core may
//! hold a `KiteWrites`).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_desk::gateway::{
    FAILED_STATUSES, ProductGates, StopBand, TenantIds, mint_client_id, parse_client_id,
    refuse_cross_tenant,
};
use baskfy_desk::plan::{PLAN_TTL_SECONDS, Plan, PlanStore, check_execute, is_expired};
use baskfy_desk::wire::PyDict;

fn plan(created_at: f64) -> Plan {
    Plan {
        plan_id: "a1b2c3d4e5f6".to_owned(),
        created_at,
        body: PyDict::new(),
    }
}

#[test]
fn execute_needs_the_word_true_then_a_known_plan_then_a_fresh_one() {
    let p = plan(1_000.0);
    assert_eq!(
        check_execute("false", Some(&p), 1_000.0)
            .unwrap_err()
            .status,
        400
    );
    assert_eq!(
        check_execute("True", Some(&p), 1_000.0).unwrap_err().status,
        400
    );
    assert_eq!(
        check_execute("", None, 1_000.0).unwrap_err().status,
        400,
        "confirm is checked first"
    );
    let unknown = check_execute("true", None, 1_000.0).unwrap_err();
    assert_eq!(
        (unknown.status, unknown.detail),
        (404, "Unknown or expired plan_id \u{2014} re-run Analyze.")
    );
    assert!(
        check_execute("true", Some(&p), 1_000.0 + PLAN_TTL_SECONDS).is_ok(),
        "exactly 30 min is not stale (`>`)"
    );
    let stale = check_execute("true", Some(&p), 1_000.0 + PLAN_TTL_SECONDS + 0.001).unwrap_err();
    assert_eq!(
        (stale.status, stale.detail),
        (
            410,
            "Plan older than 30 minutes \u{2014} prices stale, re-run Analyze."
        )
    );
    assert!(is_expired(0.0, 1800.5) && !is_expired(0.0, 1800.0));
}

#[test]
fn plans_live_in_the_process_and_nowhere_else() {
    let store = PlanStore::default();
    assert!(store.get("a1b2c3d4e5f6").is_none());
    store.insert(plan(5.0));
    assert_eq!(
        store.get("a1b2c3d4e5f6").unwrap().created_at,
        5.0_f64.max(5.0)
    );
}

#[test]
fn client_ids_are_plan_colon_symbol_and_refuse_ambiguity() {
    assert_eq!(mint_client_id("PLAN1", "infy").unwrap(), "PLAN1:INFY");
    assert_eq!(
        parse_client_id("PLAN1:INFY").unwrap(),
        ("PLAN1".to_owned(), "INFY".to_owned())
    );
    for (p, s) in [
        ("", "INFY"),
        ("PLAN1", ""),
        ("a:b", "c"),
        ("a", "b:c"),
        ("PLAN1 ", "INFY"),
        ("PLAN1", "IN FY"),
    ] {
        assert!(mint_client_id(p, s).is_err(), "{p:?}/{s:?}");
    }
    assert_eq!(
        mint_client_id("a:b", "c").unwrap_err().0,
        "plan_id cannot contain ':' ('a:b'): the client id would be ambiguous, and two different orders could mint the same key"
    );
    assert!(parse_client_id("a:b:c").is_err() && parse_client_id("abc").is_err());
}

#[test]
fn a_plan_for_another_tenant_is_refused_and_ids_must_be_positive() {
    let me = TenantIds::new(1, 1).unwrap();
    assert!(refuse_cross_tenant(me, me).is_none());
    assert_eq!(
        refuse_cross_tenant(me, TenantIds::new(2, 1).unwrap()).unwrap(),
        "tenant mismatch: caller (1,1) != plan (2,1)"
    );
    assert!(TenantIds::new(0, 1).is_err() && TenantIds::new(1, 0).is_err());
}

#[test]
fn product_gates_fail_closed() {
    let g = ProductGates::default();
    assert!(g.dry_run && !g.intraday_enabled && !g.options_enabled && !g.fno_carry_enabled);
    let from = ProductGates::from_settings(&baskfy_desk::Settings::defaults().unwrap());
    assert_eq!(from, g);
    assert_eq!(
        FAILED_STATUSES,
        ["ERROR", "REJECTED", "BLOCKED", "RISK_BLOCKED"]
    );
}

#[test]
fn the_weekly_stop_band_is_eight_to_twelve_percent() {
    let band = StopBand::default();
    assert!(
        (band.min_pct() - 0.08).abs() < f64::EPSILON
            && (band.max_pct() - 0.12).abs() < f64::EPSILON
    );
    assert!(
        StopBand::new(0.12, 0.08).is_err()
            && StopBand::new(0.0, 0.1).is_err()
            && StopBand::new(0.1, 1.0).is_err()
    );
    assert!(StopBand::new(0.005, 0.10).is_ok(), "the swing book's band");
}

/// Law 2 in the types: `KiteWrites` (the order verbs) is named only where the broker is built
/// and where the gateway is. A page that wants to place must go through `Gateway`.
#[test]
fn only_the_gateway_lane_may_hold_the_order_verbs() {
    let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let allowed = [
        "kite.rs",
        "fake_kite.rs",
        "app.rs",
        "main.rs",
        "exec_core.rs",
        "lib.rs",
    ];
    let mut offenders = Vec::new();
    let mut stack = vec![src];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
                continue;
            }
            let name = path.file_name().unwrap().to_string_lossy().to_string();
            let text = std::fs::read_to_string(&path).unwrap();
            let code: String = text
                .lines()
                .filter(|l| !l.trim_start().starts_with("//"))
                .collect::<Vec<_>>()
                .join("\n");
            if code.contains("KiteWrites") && !allowed.contains(&name.as_str()) {
                offenders.push(path.display().to_string());
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "KiteWrites named outside the gateway's lane: {offenders:?}"
    );
}
