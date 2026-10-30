//! The formatting and serialisation engines against the live Python's answers:
//! `fixtures/pyfmt_probes.json` (`%` and `format()`), `fixtures/wire_probes.json` (orjson,
//! `json.dumps`), `fixtures/template_probes.json` (Jinja2 through the desk's own environment) —
//! all written by `tools/parity/desk_probes.py`. And every desk template must compile.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_desk::pyfmt::{PyArg, format_value, percent_format, str_format};
use baskfy_desk::templates::Templates;
use baskfy_desk::wire::{PyValue, orjson_dumps, orjson_float, py_json_dumps};
use serde_json::Value;

fn arg(v: &Value) -> PyArg {
    match v {
        Value::Null => PyArg::None,
        Value::Bool(b) => PyArg::Bool(*b),
        Value::Number(n) if n.is_f64() => PyArg::Float(n.as_f64().unwrap()),
        Value::Number(n) => PyArg::Int(n.as_i64().unwrap()),
        Value::String(s) => PyArg::Str(s.clone()),
        Value::Object(o) => PyArg::Float(match o["float"].as_str().unwrap() {
            "nan" => f64::NAN,
            "inf" => f64::INFINITY,
            _ => f64::NEG_INFINITY,
        }),
        Value::Array(_) => panic!("no list args"),
    }
}

fn check(
    label: &str,
    got: Result<String, baskfy_desk::pyfmt::FmtError>,
    case: &Value,
    failures: &mut Vec<String>,
) {
    match (got, case.get("ok"), case.get("err")) {
        (Ok(g), Some(Value::String(want)), _) if g == *want => {}
        (Err(e), None, Some(_)) if e.python_type != "NotImplemented" => {}
        (got, ok, err) => failures.push(format!(
            "{label}: Rust {got:?}, Python ok={ok:?} err={err:?}"
        )),
    }
}

#[test]
fn percent_formatting_is_pythons() {
    let f: Value = serde_json::from_str(include_str!("../fixtures/pyfmt_probes.json")).unwrap();
    let mut failures = Vec::new();
    for case in f["percent"].as_array().unwrap() {
        let fmt = case["fmt"].as_str().unwrap();
        let args: Vec<PyArg> = case["args"].as_array().unwrap().iter().map(arg).collect();
        check(
            &format!("{fmt:?} % {args:?}"),
            percent_format(fmt, &args),
            case,
            &mut failures,
        );
    }
    assert!(
        failures.is_empty(),
        "{} mismatches:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn format_specs_are_pythons() {
    let f: Value = serde_json::from_str(include_str!("../fixtures/pyfmt_probes.json")).unwrap();
    let mut failures = Vec::new();
    for case in f["format"].as_array().unwrap() {
        let spec = case["spec"].as_str().unwrap();
        let value = arg(&case["value"]);
        check(
            &format!("format({value:?}, {spec:?})"),
            format_value(&value, spec),
            case,
            &mut failures,
        );
    }
    for case in f["str_format"].as_array().unwrap() {
        let tpl = case["template"].as_str().unwrap();
        let args: Vec<PyArg> = case["args"].as_array().unwrap().iter().map(arg).collect();
        check(
            &format!("{tpl:?}.format{args:?}"),
            str_format(tpl, &args),
            case,
            &mut failures,
        );
    }
    assert!(
        failures.is_empty(),
        "{} mismatches:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn orjson_floats_and_documents_are_orjsons() {
    let f: Value = serde_json::from_str(include_str!("../fixtures/wire_probes.json")).unwrap();
    for case in f["floats"].as_array().unwrap() {
        let PyArg::Float(x) = arg(&case["value"]) else {
            let PyArg::Int(i) = arg(&case["value"]) else {
                panic!()
            };
            #[allow(clippy::cast_precision_loss)]
            let x = i as f64;
            assert_eq!(orjson_float(x).unwrap(), case["orjson"].as_str().unwrap());
            continue;
        };
        let want = case["orjson"].as_str().unwrap();
        assert_eq!(
            orjson_float(x).unwrap_or_else(|| "null".to_owned()),
            want,
            "{x:e}"
        );
    }
    let sample = PyValue::from_json(
        r#"{"plan_id": "abc123", "n": 3, "x": 1e-05, "big": 1e+16, "neg": -0.0, "none": null, "flag": true,
             "text": "\u00e9<>&\"\\\n\t\u0001\u007f", "list": [1, 2.5, "s"], "nested": {"b": 1, "a": [null, false]},
             "d": "2026-10-30", "t": "2026-10-30T09:15:00+05:30", "tu": "2026-10-30T09:15:00.123456"}"#
            .as_bytes(),
    )
    .unwrap();
    assert_eq!(
        orjson_dumps(&sample).unwrap(),
        f["sample_orjson"].as_str().unwrap()
    );
    // json.dumps(default=str) prints a datetime with a space; the sample's strings stand in.
    let dumped = py_json_dumps(&sample).unwrap();
    let want = f["sample_json"]
        .as_str()
        .unwrap()
        .replace("2026-10-30 09:15", "2026-10-30T09:15");
    assert_eq!(dumped, want);
}

fn templates() -> Templates {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../kite-momentum-rebalancer/app/templates");
    Templates::new(&dir, "0123abcd", false)
}

#[test]
fn every_desk_template_compiles() {
    let t = templates();
    let mut names: Vec<String> = std::fs::read_dir(t.dir())
        .unwrap()
        .filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().to_string()))
        .filter(|n| n.ends_with(".html"))
        .collect();
    names.sort();
    assert!(names.len() >= 18, "{names:?}");
    let mut broken = Vec::new();
    for name in &names {
        if let Err(e) = t.check(name) {
            broken.push(format!("{name}: {e:#}"));
        }
    }
    assert!(
        broken.is_empty(),
        "templates minijinja cannot parse:\n{}",
        broken.join("\n")
    );
}

#[test]
fn probe_snippets_render_exactly_as_jinja2_does() {
    let f: Value = serde_json::from_str(include_str!("../fixtures/template_probes.json")).unwrap();
    assert_eq!(f["asset_v_len"], 8);
    assert_eq!(f["options_enabled"], false);
    let t = templates();
    // The context in Python's key order (serde_json's map would sort it).
    let raw = include_str!("../fixtures/template_probes.json");
    let PyValue::Map(whole) = PyValue::from_json(raw.as_bytes()).unwrap() else {
        panic!()
    };
    let ctx = baskfy_desk::templates::py_value(whole.get("context").unwrap());
    let mut failures = Vec::new();
    for probe in f["probes"].as_array().unwrap() {
        let name = probe["name"].as_str().unwrap();
        let src = probe["source"].as_str().unwrap();
        let got = t.render_str(src, ctx.clone());
        match (&got, probe.get("ok")) {
            (Ok(g), Some(Value::String(want))) if g == want => {}
            _ => failures.push(format!(
                "{name}:\n  rust:   {got:?}\n  jinja2: {:?}",
                probe.get("ok")
            )),
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
