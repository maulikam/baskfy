//! The harness proof: every golden under go/testdata/golden loads, and its inputs and output
//! decode structurally (frames into polars, dates/decimals parsed) without error. A frame also
//! survives decode -> encode unchanged, which is what grading a port's frame relies on.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::print_stdout
)]

use baskfy_testkit::{
    Decoded, all_cases, cases_for, compare, decode_frame, decode_value, encode_frame, load,
    wide_decimals,
};
use serde_json::Value;

/// Golden files present when the harness was written (29 Oct 2026). More may be added;
/// fewer means goldens went missing.
const AT_LEAST: usize = 126;

/// Decimal strings Python printed that `rust_decimal` cannot hold (scale > 28). Pinned, so a
/// new one fails this test and is looked at rather than discovered by a port (R1.3).
const KNOWN_WIDE_DECIMALS: &[(&str, &str, &str)] = &[(
    "L1/backtest/year_fraction.case_004",
    "$",
    "-0.07939767282683093771389459274",
)];

fn roundtrip_frames(v: &Value, case: &str) -> usize {
    match v {
        // The polars shape only ({columns, dtypes, rows}), the same rule as `decode_value`.
        // The ranking goldens' pandas shapes ({columns, dtypes, index, rows} and the input
        // form {columns, date_columns, rows}) are not polars frames; wave 2 adds their decoder.
        Value::Object(m)
            if m.len() == 3
                && m.contains_key("columns")
                && m.contains_key("dtypes")
                && m.contains_key("rows") =>
        {
            let df = decode_frame(v).unwrap_or_else(|e| panic!("{case}: {e}"));
            let back = encode_frame(&df).unwrap_or_else(|e| panic!("{case}: {e}"));
            // Exact round trip (zero tolerance): the decode is lossless.
            compare(v, &back, &baskfy_testkit::Tolerance { float: 0.0 })
                .unwrap_or_else(|m| panic!("{case}: frame round trip\n{m}"));
            1
        }
        Value::Object(m) => m.values().map(|x| roundtrip_frames(x, case)).sum(),
        Value::Array(xs) => xs.iter().map(|x| roundtrip_frames(x, case)).sum(),
        _ => 0,
    }
}

#[test]
fn every_golden_parses_and_decodes() {
    let cases = all_cases().unwrap();
    let mut frames = 0;
    let mut wide = Vec::new();
    for g in &cases {
        for (at, s) in wide_decimals(&g.inputs) {
            wide.push((g.case.clone(), format!("inputs{}", &at[1..]), s));
        }
        for (at, s) in wide_decimals(&g.output) {
            wide.push((g.case.clone(), at, s));
        }
        assert!(
            g.fn_name.starts_with("baskfy_core."),
            "{}: fn {}",
            g.case,
            g.fn_name
        );
        let inputs = decode_value(&g.inputs).unwrap_or_else(|e| panic!("{}: inputs: {e}", g.case));
        assert!(
            matches!(inputs, Decoded::Map(_)),
            "{}: inputs is not an object",
            g.case
        );
        decode_value(&g.output).unwrap_or_else(|e| panic!("{}: output: {e}", g.case));
        frames += roundtrip_frames(&g.inputs, &g.case) + roundtrip_frames(&g.output, &g.case);
        // A golden always matches itself.
        g.check_value(&g.output).unwrap();
    }
    println!(
        "golden files parsed: {} (frames round-tripped: {frames})",
        cases.len()
    );
    assert!(
        cases.len() >= AT_LEAST,
        "only {} goldens found",
        cases.len()
    );
    assert!(frames > 0, "no frame was decoded");
    let known: Vec<(String, String, String)> = KNOWN_WIDE_DECIMALS
        .iter()
        .map(|(c, a, s)| ((*c).to_owned(), (*a).to_owned(), (*s).to_owned()))
        .collect();
    assert_eq!(wide, known, "decimals beyond rust_decimal changed");
}

#[test]
fn typed_accessors_on_a_known_case() {
    let g = load("L1/swing/manage.case_001").unwrap();
    assert_eq!(g.fn_name, "baskfy_core.swing.stops.manage");
    let pos = g.input("position").unwrap();
    let entry = baskfy_testkit::decimal(&pos["entry"]).unwrap();
    assert_eq!(entry.to_string(), "100");
    let detect = load("L1/swing/detect_setups.case_001.json").unwrap();
    let bars = detect.input_frame("bars").unwrap();
    assert_eq!(
        bars.column("date").unwrap().dtype(),
        &polars::prelude::DataType::Date
    );
    assert_eq!(
        detect.input_date("as_of").unwrap().to_string(),
        "2026-05-20"
    );
    let out = detect.output_frame().unwrap();
    detect.assert_frame(&out);
}

#[test]
fn cases_for_finds_every_case_of_a_function_and_refuses_a_typo() {
    assert_eq!(
        cases_for("baskfy_core.swing.stops.manage").unwrap().len(),
        16
    );
    assert!(cases_for("baskfy_core.swing.stops.manag").is_err());
}

#[test]
fn grading_rules() {
    use serde_json::json;
    let tol = baskfy_testkit::Tolerance::default();
    // floats: 1e-9 * max(1, |expected|)
    assert!(compare(&json!(1000.0), &json!(1000.0 + 5e-7), &tol).is_ok());
    assert!(compare(&json!(1000.0), &json!(1000.0 + 2e-6), &tol).is_err());
    assert!(compare(&json!(0.5), &json!(0.5 + 5e-10), &tol).is_ok());
    // Decimal strings are byte-exact; integers are not floats.
    assert!(compare(&json!("5.10"), &json!("5.1"), &tol).is_err());
    assert!(compare(&json!(3), &json!(3.0), &tol).is_err());
    // null only matches null; key sets must agree.
    assert!(compare(&json!(null), &json!(0.0), &tol).is_err());
    assert!(compare(&json!({"a": 1}), &json!({"a": 1, "b": 2}), &tol).is_err());
    let err = compare(&json!({"a": [1, 2]}), &json!({"a": [1, 3]}), &tol).unwrap_err();
    assert_eq!(err.0[0].path, "$.a[1]");
}
