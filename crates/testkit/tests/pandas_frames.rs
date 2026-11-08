//! The pandas decoder (`baskfy_testkit::pandas`): unit cases for its dtype rules, and the proof
//! over the committed goldens — every pandas-shaped frame and series decodes, and every
//! *output* shape (which states its dtypes and index) survives decode -> encode exactly.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_testkit::{
    Tolerance, all_cases, compare, decode_pandas_frame, decode_pandas_series, encode_pandas_frame,
    encode_pandas_series, float_series, is_pandas_frame, is_pandas_series,
};
use polars::prelude::DataType;
use serde_json::{Value, json};

#[test]
fn input_shape_infers_like_pandas() {
    let v = json!({
        "columns": ["id", "x", "n", "flag", "maybe", "name", "day", "empty"],
        "date_columns": ["day"],
        "rows": [
            [1, 1.5, 3, true, true, "A", "2026-08-18", null],
            [2, null, null, false, null, "B", null, null],
        ],
    });
    assert!(is_pandas_frame(&v));
    let p = decode_pandas_frame(&v).unwrap();
    let dt: Vec<DataType> = p
        .frame
        .columns()
        .iter()
        .map(|c| c.dtype().clone())
        .collect();
    assert_eq!(
        dt,
        vec![
            DataType::Int64,
            DataType::Float64,
            // integers beside a null: pandas makes them float64 with NaN
            DataType::Float64,
            DataType::Boolean,
            DataType::Boolean,
            DataType::String,
            DataType::Date,
            DataType::Null,
        ]
    );
    assert_eq!(p.index, vec![json!(0), json!(1)]);
    assert_eq!(p.dtypes[0], ("id".to_owned(), "int64".to_owned()));
    assert_eq!(p.dtypes[6], ("day".to_owned(), "object".to_owned()));
}

#[test]
fn output_shape_keeps_stated_dtypes_and_index() {
    let v = json!({
        "columns": ["rank", "score", "ok", "why", "when"],
        "dtypes": {"rank": "Int64", "score": "float64", "ok": "object", "why": "object",
                   "when": "object"},
        "index": [4, 2],
        "rows": [[1, 0.5, true, "", "2026-08-18"], [null, null, null, null, null]],
    });
    let p = decode_pandas_frame(&v).unwrap();
    assert_eq!(p.frame.column("rank").unwrap().dtype(), &DataType::Int64);
    assert_eq!(p.frame.column("ok").unwrap().dtype(), &DataType::Boolean);
    assert_eq!(p.frame.column("why").unwrap().dtype(), &DataType::String);
    let back = encode_pandas_frame(&p.frame, &p.index, &p.dtypes).unwrap();
    compare(&v, &back, &Tolerance { float: 0.0 }).unwrap();
}

#[test]
fn polars_shape_is_not_pandas_and_mixed_objects_are_refused() {
    let polars = json!({"columns": [], "dtypes": {}, "rows": []});
    assert!(!is_pandas_frame(&polars));
    let mixed = json!({"columns": ["a"], "rows": [[true], ["x"]]});
    assert!(decode_pandas_frame(&mixed).is_err());
    let short = json!({"columns": ["a"], "dtypes": {"a": "float64"}, "index": [0], "rows": []});
    assert!(decode_pandas_frame(&short).is_err());
}

#[test]
fn series_round_trip() {
    let v = json!({"dtype": "float64", "index": [0, 1, 2], "values": [0.5, null, 2.0]});
    assert!(is_pandas_series(&v));
    let (s, index) = decode_pandas_series("x", &v).unwrap();
    assert_eq!(s.dtype(), &DataType::Float64);
    let back = encode_pandas_series(&s, &index, None).unwrap();
    compare(&v, &back, &Tolerance { float: 0.0 }).unwrap();
    let f = float_series("f", &[json!(1), Value::Null]).unwrap();
    assert_eq!(f.null_count(), 1);
}

fn visit(v: &Value, case: &str, out: &mut (usize, usize, usize)) {
    match v {
        Value::Object(m) if is_pandas_frame(v) => {
            let p = decode_pandas_frame(v).unwrap_or_else(|e| panic!("{case}: {e}"));
            out.0 += 1;
            if m.contains_key("dtypes") {
                let back = encode_pandas_frame(&p.frame, &p.index, &p.dtypes)
                    .unwrap_or_else(|e| panic!("{case}: {e}"));
                compare(v, &back, &Tolerance { float: 0.0 })
                    .unwrap_or_else(|mm| panic!("{case}: pandas frame round trip\n{mm}"));
                out.1 += 1;
            }
        }
        Value::Object(m) if is_pandas_series(v) => {
            let dtype = m.get("dtype").and_then(Value::as_str);
            let (s, index) =
                decode_pandas_series("values", v).unwrap_or_else(|e| panic!("{case}: {e}"));
            let back =
                encode_pandas_series(&s, &index, dtype).unwrap_or_else(|e| panic!("{case}: {e}"));
            compare(v, &back, &Tolerance { float: 0.0 })
                .unwrap_or_else(|mm| panic!("{case}: pandas series round trip\n{mm}"));
            out.2 += 1;
        }
        Value::Object(m) => m.values().for_each(|x| visit(x, case, out)),
        Value::Array(xs) => xs.iter().for_each(|x| visit(x, case, out)),
        _ => {}
    }
}

#[test]
fn every_pandas_shape_in_the_goldens_decodes_and_outputs_round_trip() {
    let mut counts = (0, 0, 0);
    for g in all_cases().unwrap() {
        if !g.fn_name.starts_with("baskfy_core.ranking_engine.") {
            continue;
        }
        visit(&g.inputs, &g.case, &mut counts);
        visit(&g.output, &g.case, &mut counts);
    }
    // Counted on 29 Oct 2026 (golden_ranking.py): 67 frames, 29 of them outputs; 22 series.
    assert!(counts.0 >= 67, "pandas frames found: {}", counts.0);
    assert!(counts.1 >= 29, "pandas output frames found: {}", counts.1);
    assert!(counts.2 >= 22, "pandas series found: {}", counts.2);
}
