//! The Kite half of `tests/test_holdings_port.py` and of `test_records_and_ports.py`'s port
//! conformance cases: Kite satisfies the bars and holdings ports and not the reference port,
//! its capability set is bars plus the read-only ledger, and — law 2 — nothing in `kite.rs` can
//! place, modify or cancel an order. (The fixture adapter, the composite's routing and the
//! production stack's registration are `fixtures.rs`', `composite.rs`' and `factory.rs`'.)
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;
mod kite_common;

use std::marker::PhantomData;
use std::path::PathBuf;

use baskfy_providers::kite::KiteProvider;
use baskfy_providers::ports::{
    BarsProvider, Capability, HealthReporting, HoldingsProvider, ReferenceProvider,
    bars_capabilities, holdings_capabilities, reference_capabilities,
};
use common::arm;
use kite_common::settings;

/// `isinstance(provider, HoldingsProvider)`: the coercion compiles only if the trait holds.
fn as_holdings(provider: &KiteProvider) -> &dyn HoldingsProvider {
    provider
}

/// `isinstance(provider, BarsProvider)`.
fn as_bars(provider: &KiteProvider) -> &dyn BarsProvider {
    provider
}

/// `isinstance(x, ReferenceProvider)` without specialisation: the inherent method exists only
/// when the bound holds; otherwise the blanket trait's `false` answers.
struct Probe<T>(PhantomData<T>);

trait NotReference {
    fn is_reference(&self) -> bool {
        false
    }
}

impl<T> NotReference for Probe<T> {}

impl<T: ReferenceProvider> Probe<T> {
    #[allow(dead_code)]
    fn is_reference(&self) -> bool {
        true
    }
}

#[test]
fn kite_satisfies_the_holdings_port() {
    arm();
    let env = settings();
    let provider = KiteProvider::new(env.settings.clone(), Default::default()).unwrap();
    assert_eq!(as_holdings(&provider).name(), "kite");
}

#[test]
fn kite_satisfies_the_bars_port() {
    arm();
    let env = settings();
    let provider = KiteProvider::new(env.settings.clone(), Default::default()).unwrap();
    assert_eq!(as_bars(&provider).name(), "kite");
}

#[test]
fn kite_does_not_satisfy_the_reference_port() {
    arm();
    // docs/02: Kite has no constituents, PE/PB or corporate actions. The type says so.
    assert!(!Probe::<KiteProvider>(PhantomData).is_reference());
}

#[test]
fn kite_serves_bars_and_the_ledger_and_no_reference_capability() {
    arm();
    let env = settings();
    let provider = KiteProvider::new(env.settings.clone(), Default::default()).unwrap();
    let caps = provider.capabilities();
    assert!(holdings_capabilities().is_subset(&caps));
    assert!(bars_capabilities().is_subset(&caps));
    assert!(reference_capabilities().is_disjoint(&caps));
    let values: Vec<&str> = holdings_capabilities().iter().map(|c| c.value()).collect();
    assert_eq!(values, vec!["broker_holdings", "broker_cash"]);
    assert!(caps.contains(&Capability::BrokerHoldings));
}

#[test]
fn nothing_in_the_kite_adapter_can_reach_an_order() {
    arm();
    // Law 2: `packages/execution` is the only path to an order, and this adapter is not it.
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/kite.rs");
    let code: String = std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    for forbidden in [
        "place_order",
        "modify_order",
        "cancel_order",
        "exit_order",
        "place_gtt",
        "modify_gtt",
        "delete_gtt",
        "\"/orders",
        "\"/gtt",
        "convert_position",
        "Method::PUT",
        "Method::DELETE",
    ] {
        assert!(!code.contains(forbidden), "kite.rs mentions {forbidden}");
    }
    // The one POST is the margin calculator.
    assert_eq!(code.matches("Method::POST").count(), 1);
    assert!(code.contains("\"/margins/basket\""));
}
