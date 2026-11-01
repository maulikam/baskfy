//! `tests/test_telemetry.py`'s two order-path tests, over the Rust ports of `app/main.py`'s
//! plan → execute → GTT routes.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::Path;

/// The ports of `main.py`'s `/analyze`, `/execute` and `/stops/arm`.
const ORDER_PATH: [&str; 2] = ["src/lanes/weekly/analyze.rs", "src/lanes/weekly/stops.rs"];

fn source() -> String {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    ORDER_PATH
        .iter()
        .map(|f| std::fs::read_to_string(root.join(f)).unwrap())
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn each_stage_is_covered() {
    let src = source();
    for (marker, stage) in [
        ("telemetry::span(\"desk.plan.build\"", "plan"),
        ("telemetry::count(\"orders\"", "execute"),
        ("telemetry::count(\"gtt\"", "GTT"),
        ("telemetry::observe(\"execute_seconds\"", "execute timing"),
        ("telemetry::capture(", "error capture"),
    ] {
        assert!(src.contains(marker), "{stage} is not instrumented");
    }
}

#[test]
fn no_price_quantity_or_token_is_ever_an_attribute() {
    let src = source();
    let instrumented: Vec<&str> = src
        .lines()
        .filter(|l| {
            l.contains("telemetry::span(")
                || l.contains("telemetry::count(")
                || l.contains("telemetry::observe(")
        })
        .collect();
    assert!(
        !instrumented.is_empty(),
        "the order path is not instrumented at all"
    );
    for line in instrumented {
        for forbidden in [
            "price",
            "ref_price",
            "qty",
            "quantity",
            "nav",
            "token",
            "cash",
        ] {
            assert!(
                !line.to_lowercase().contains(forbidden),
                "{forbidden:?} must not leave the desk: {line}"
            );
        }
    }
}
