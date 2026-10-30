//! `app.config` and every module-level `os.getenv`, held to the live Python's answers
//! (`fixtures/settings_defaults.json`, `tools/parity/desk_settings_dump.py`), and the safety
//! defaults the non-negotiables require.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use std::collections::BTreeSet;

use baskfy_desk::settings::{ConfigValue, ENV_VARS, FUNCTION_LEVEL, Settings};
use serde_json::Value;

fn fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/settings_defaults.json")).unwrap()
}

fn matches(rust: &ConfigValue, py: &Value) -> bool {
    match (rust, py) {
        (ConfigValue::Bool(a), Value::Bool(b)) => a == b,
        (ConfigValue::Int(a), Value::Number(n)) => n.is_i64() && n.as_i64() == Some(*a),
        (ConfigValue::Float(a), Value::Number(n)) => {
            n.is_f64() && n.as_f64().is_some_and(|b| b.to_bits() == a.to_bits())
        }
        (ConfigValue::Float(a), Value::Object(o)) => match o.get("float").and_then(Value::as_str) {
            Some("nan") => a.is_nan(),
            Some("inf") => *a == f64::INFINITY,
            Some("-inf") => *a == f64::NEG_INFINITY,
            _ => false,
        },
        (ConfigValue::Str(a), Value::String(b)) => a == b,
        (ConfigValue::Tuple(a), Value::Object(o)) => o
            .get("tuple")
            .and_then(Value::as_array)
            .is_some_and(|items| {
                items.iter().map(Value::as_str).collect::<Vec<_>>()
                    == a.iter().map(|s| Some(s.as_str())).collect::<Vec<_>>()
            }),
        _ => false,
    }
}

#[test]
fn the_rust_table_names_exactly_the_python_variables() {
    let f = fixture();
    let py: BTreeSet<(String, String)> = f["vars"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            (
                r["key"].as_str().unwrap().to_owned(),
                r["module"].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    let rs: BTreeSet<(String, String)> = ENV_VARS
        .iter()
        .map(|v| (v.key.to_owned(), v.module.to_owned()))
        .collect();
    assert_eq!(
        rs, py,
        "module-level env reads differ from the Python desk's"
    );
    let py_fn: BTreeSet<(String, String, String)> = f["function_level"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            (
                r["key"].as_str().unwrap().to_owned(),
                r["module"].as_str().unwrap().to_owned(),
                r["default_literal"].as_str().unwrap_or("").to_owned(),
            )
        })
        .collect();
    let rs_fn: BTreeSet<(String, String, String)> = FUNCTION_LEVEL
        .iter()
        .map(|v| (v.key.to_owned(), v.module.to_owned(), v.default.to_owned()))
        .collect();
    assert_eq!(rs_fn, py_fn, "function-level env reads differ");
}

#[test]
fn every_default_and_every_probe_parses_as_python_does() {
    let f = fixture();
    let mut checked = 0;
    for row in f["vars"].as_array().unwrap() {
        let key = row["key"].as_str().unwrap();
        let module = row["module"].as_str().unwrap();
        let var = ENV_VARS
            .iter()
            .find(|v| v.key == key && v.module == module)
            .unwrap();
        let attrs: Vec<&str> = row["attrs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a.as_str().unwrap())
            .collect();
        assert_eq!(var.attrs, attrs.as_slice(), "{key}: attribute names");
        let mut cases = vec![(None, &row["default"])];
        for (probe, answer) in row["probes"].as_object().unwrap() {
            cases.push((Some(probe.as_str()), answer));
        }
        for (raw, answer) in cases {
            let got = var.parse(raw);
            if answer.get("error").is_some() {
                assert!(
                    got.is_err(),
                    "{key}={raw:?}: Python raises, Rust gave {got:?}"
                );
            } else {
                let got = got.unwrap_or_else(|e| {
                    panic!("{key}={raw:?}: Rust refused ({e}), Python gave {answer}")
                });
                for (attr, value) in got {
                    assert!(
                        matches(&value, &answer[attr]),
                        "{key}={raw:?} {attr}: Rust {value:?}, Python {}",
                        answer[attr]
                    );
                }
            }
            checked += 1;
        }
    }
    assert!(checked > 2000, "only {checked} cases checked");
}

#[test]
fn the_defaults_simulate_and_every_execution_switch_is_off() {
    let s = Settings::defaults().unwrap();
    assert!(
        s.dry_run,
        "DRY_RUN must default true (non-negotiable 1, safety rail)"
    );
    for (attr, value) in s.values() {
        let switch = attr.ends_with("_EXECUTION_ENABLED")
            || attr.contains("AUTO_EXECUTE")
            || attr.contains("AUTO_EXIT")
            || matches!(
                attr,
                "INTRADAY_ENABLED" | "OPTIONS_ENABLED" | "FNO_CARRY_ENABLED"
            );
        if switch {
            assert_eq!(value, ConfigValue::Bool(false), "{attr} must default false");
        }
    }
    assert!(!s.intraday_enabled && !s.options_enabled && !s.fno_carry_enabled);
}

#[test]
fn there_are_exactly_three_auto_execute_exceptions_and_each_needs_the_word_true() {
    let auto: BTreeSet<&str> = ENV_VARS
        .iter()
        .filter(|v| v.key.contains("AUTO"))
        .map(|v| v.key)
        .collect();
    assert_eq!(
        auto,
        BTreeSet::from([
            "BASKFY_FNO_F3_AUTO_EXIT",
            "BASKFY_SWING_AUTO_EXECUTE",
            "BASKFY_TWT_AUTO_EXECUTE"
        ]),
        "non-negotiable 1 names three exceptions; an agent may not add a fourth"
    );
    for key in &auto {
        let var = ENV_VARS.iter().find(|v| v.key == *key).unwrap();
        assert_eq!(var.default, "false", "{key} defaults false");
        for almost in ["1", "yes", "on", "True ", " true", "y"] {
            let got = var.parse(Some(almost)).unwrap();
            assert_eq!(
                got[0].1,
                ConfigValue::Bool(false),
                "{key}={almost:?} must not arm it"
            );
        }
        assert_eq!(
            var.parse(Some("TRUE")).unwrap()[0].1,
            ConfigValue::Bool(true),
            "Python lower()s"
        );
    }
    let s = Settings::defaults().unwrap();
    assert_eq!(
        s.auto_execute_flags().iter().filter(|(_, on)| *on).count(),
        0
    );
}

#[test]
fn dry_run_is_off_only_for_the_word_false() {
    let var = ENV_VARS.iter().find(|v| v.key == "DRY_RUN").unwrap();
    for keep in ["", "0", "no", "off", "False ", "true", "anything"] {
        assert_eq!(
            var.parse(Some(keep)).unwrap()[0].1,
            ConfigValue::Bool(true),
            "DRY_RUN={keep:?}"
        );
    }
    assert_eq!(
        var.parse(Some("FALSE")).unwrap()[0].1,
        ConfigValue::Bool(false)
    );
}

#[test]
fn a_settings_page_override_round_trips_and_a_wrong_type_is_refused() {
    let mut s = Settings::defaults().unwrap();
    s.set("REGIME_BUFFER_BPS", ConfigValue::Int(175)).unwrap();
    assert_eq!(s.regime_buffer_bps, 175);
    assert!(
        s.set("REGIME_BUFFER_BPS", ConfigValue::Str("x".into()))
            .is_err()
    );
    assert!(s.set("NOT_A_SETTING", ConfigValue::Int(1)).is_err());
}

#[test]
fn a_value_python_refuses_fails_the_whole_load() {
    let err =
        Settings::from_lookup(&|k| (k == "DESK_PORT").then(|| "eighty".to_owned())).unwrap_err();
    assert_eq!(err.key, "DESK_PORT");
    assert_eq!(err.python_error, "ValueError");
}
