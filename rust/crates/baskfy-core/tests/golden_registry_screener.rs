#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! `baskfy_core.factor_registry` and `baskfy_core.screener` against the goldens
//! (`go/testdata/golden/L1/ranking/{factor_registry,screener}.*.case_NNN.json`).
//!
//! Every case for each function is loaded and graded strictly by the testkit, and each
//! function's case count is asserted so a dropped golden fails loudly. A Python raise is graded
//! as `{"raises": {"type", "message"}}` (pydantic: `"errors"`), exactly as the dumper stores it.
//!
//! The harness side mirrors `tools/parity/golden_ranking.py`: a definition is
//! `ScreenDefinition.model_validate(inputs["definition"])`, then `smuggle` is applied without
//! validation (`model_copy(update=...)`); `canonical_json` payloads untag `$decimal`, `$date`
//! and `$object`.

use baskfy_core::factor_registry::{self as fr, RegistryError};
use baskfy_core::screener::{
    self, CompiledStatement, CustomFilter, ExtraFactor, MAX_RESULT_ROWS, Payload, PyDecimal,
    RankingFactor, ScreenDefinition, ScreenError, ScreenResult, ScreenResultRow,
};
use baskfy_testkit::Golden;
use chrono::NaiveDate;
use serde_json::{Map, Value, json};

const REG: &str = "baskfy_core.factor_registry.";
const SCR: &str = "baskfy_core.screener.";

// ---------------------------------------------------------------------------
// Encoding helpers
// ---------------------------------------------------------------------------

fn raises(kind: &str, message: &str) -> Value {
    json!({"raises": {"type": kind, "message": message}})
}

fn registry_raise(e: &RegistryError) -> Value {
    raises(e.python_type(), e.message())
}

fn screen_raise(e: &ScreenError) -> Value {
    match e {
        ScreenError::Validation(v) => {
            json!({"raises": {"type": "ValidationError", "errors": serde_json::to_value(&v.errors).unwrap()}})
        }
        ScreenError::Query(m)
        | ScreenError::Value(m)
        | ScreenError::Type(m)
        | ScreenError::Key(m) => raises(e.python_type(), m),
    }
}

fn capture(result: Result<Value, ScreenError>) -> Value {
    result.unwrap_or_else(|e| screen_raise(&e))
}

/// A JSON value as the Python object the dumper hands over (`_untag` applied).
fn payload(v: &Value) -> Payload {
    match v {
        Value::Null => Payload::Null,
        Value::Bool(b) => Payload::Bool(*b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => Payload::Int(i),
            None => Payload::Float(n.as_f64().unwrap()),
        },
        Value::String(s) => Payload::Str(s.clone()),
        Value::Array(items) => Payload::List(items.iter().map(payload).collect()),
        Value::Object(map) => {
            if map.len() == 1 {
                if let Some(Value::String(d)) = map.get("$decimal") {
                    return Payload::Decimal(PyDecimal::parse(d).expect("decimal tag"));
                }
                if let Some(Value::String(d)) = map.get("$date") {
                    return Payload::Date(NaiveDate::parse_from_str(d, "%Y-%m-%d").unwrap());
                }
                if map.contains_key("$object") {
                    return Payload::Opaque("object".to_owned());
                }
            }
            Payload::Map(map.iter().map(|(k, v)| (k.clone(), payload(v))).collect())
        }
    }
}

fn date(g: &Golden, key: &str) -> NaiveDate {
    g.input_date(key).unwrap()
}

fn str_list(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array)
        .map(|a| a.iter().map(|x| x.as_str().unwrap().to_owned()).collect())
        .unwrap_or_default()
}

fn columns_input(g: &Golden) -> Vec<String> {
    str_list(g.inputs.get("columns"))
}

fn limit_input(g: &Golden) -> i64 {
    g.inputs
        .get("limit")
        .and_then(Value::as_i64)
        .unwrap_or(MAX_RESULT_ROWS)
}

/// `model_copy(update=...)` skips validation; nested models are `model_construct`ed.
fn smuggle(d: &mut ScreenDefinition, update: &Map<String, Value>) {
    for (key, value) in update {
        match key.as_str() {
            "sort_by" => d.sort_by = value.as_str().unwrap().to_owned(),
            "apply_filters_on" => d.apply_filters_on = value.as_str().unwrap().to_owned(),
            "custom_filters" => {
                d.custom_filters = value
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|c| CustomFilter {
                        enabled: c.get("enabled").and_then(Value::as_bool).unwrap_or(true),
                        left: c["left"].as_str().unwrap().to_owned(),
                        op: c["op"].as_str().unwrap().to_owned(),
                        right: c["right"].as_str().unwrap().to_owned(),
                    })
                    .collect();
            }
            "factor_two" | "factor_three" => {
                let extra = ExtraFactor {
                    enabled: value
                        .get("enabled")
                        .and_then(Value::as_bool)
                        .unwrap_or(false),
                    sort_by: value
                        .get("sort_by")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                    sort_direction: value
                        .get("sort_direction")
                        .and_then(Value::as_str)
                        .unwrap_or("desc")
                        .to_owned(),
                };
                if key == "factor_two" {
                    d.factor_two = extra;
                } else {
                    d.factor_three = extra;
                }
            }
            other => panic!("the harness does not smuggle {other}"),
        }
    }
}

fn definition(g: &Golden) -> Result<ScreenDefinition, ScreenError> {
    let mut d = ScreenDefinition::model_validate(&payload(g.input("definition").unwrap()))?;
    if let Some(Value::Object(update)) = g.inputs.get("smuggle") {
        smuggle(&mut d, update);
    }
    Ok(d)
}

fn compiled_doc(c: &CompiledStatement) -> Map<String, Value> {
    let params: Map<String, Value> = c
        .params
        .iter()
        .map(|(k, v)| (k.clone(), serde_json::to_value(v).unwrap()))
        .collect();
    let mut doc = Map::new();
    doc.insert("sql".into(), json!(c.sql));
    doc.insert("params".into(), Value::Object(params));
    doc.insert("param_order".into(), json!(c.param_order));
    doc
}

fn factor_value(f: &RankingFactor) -> Value {
    serde_json::to_value(f).unwrap()
}

struct Common<'a> {
    as_of: NaiveDate,
    index_id: i64,
    universe_slug: &'a str,
    definition_hash: &'a str,
}

fn query_doc(c: &CompiledStatement, common: &Common<'_>) -> Map<String, Value> {
    let mut doc = compiled_doc(c);
    doc.insert(
        "as_of".into(),
        json!(common.as_of.format("%Y-%m-%d").to_string()),
    );
    doc.insert("index_id".into(), json!(common.index_id));
    doc.insert("universe_slug".into(), json!(common.universe_slug));
    doc.insert("definition_hash".into(), json!(common.definition_hash));
    doc
}

// ---------------------------------------------------------------------------
// factor_registry
// ---------------------------------------------------------------------------

fn run_registry_table(g: &Golden) -> Result<(), String> {
    let out = json!({
        "factors": serde_json::to_value(fr::factors()).unwrap(),
        "DOCUMENTED_FACTOR_COUNT": fr::DOCUMENTED_FACTOR_COUNT,
        "NAMED_FACTOR_COUNT": fr::named_factor_count(),
        "DOCUMENTED_COLUMN_COUNT": fr::DOCUMENTED_COLUMN_COUNT,
        "SORT_FACTOR_KEYS": fr::sort_factor_keys(),
        "CUSTOM_FILTER_OPERANDS": fr::CUSTOM_FILTER_OPERANDS.as_slice(),
        "COLUMN_PICKER_KEYS": fr::COLUMN_PICKER_KEYS.as_slice(),
        "BACKTEST_SIGNAL_KEYS": fr::backtest_signal_keys(),
        "REGIME_PRIORITY_SQL": fr::REGIME_PRIORITY_SQL,
    });
    g.check_output(&out)
}

fn run_columns(g: &Golden) -> Result<(), String> {
    g.check_output(&fr::columns())
}

fn run_by_family(g: &Golden) -> Result<(), String> {
    let out: Map<String, Value> = fr::by_family()
        .into_iter()
        .map(|(family, items)| {
            let keys: Vec<&str> = items.iter().map(|f| f.key.as_str()).collect();
            (family.value().to_owned(), json!(keys))
        })
        .collect();
    g.check_output(&out)
}

fn run_family_counts(g: &Golden) -> Result<(), String> {
    let out: Map<String, Value> = fr::family_counts()
        .into_iter()
        .map(|(family, n)| (family.to_owned(), json!(n)))
        .collect();
    g.check_output(&out)
}

fn key_input(g: &Golden) -> String {
    g.input("key").unwrap().as_str().unwrap().to_owned()
}

fn run_get(g: &Golden) -> Result<(), String> {
    let out = match fr::get(&key_input(g)) {
        Ok(f) => serde_json::to_value(f).unwrap(),
        Err(e) => registry_raise(&e),
    };
    g.check_output(&out)
}

fn run_sql_for(g: &Golden) -> Result<(), String> {
    let out = match fr::sql_for(&key_input(g)) {
        Ok(s) => json!(s),
        Err(e) => registry_raise(&e),
    };
    g.check_output(&out)
}

// ---------------------------------------------------------------------------
// screener
// ---------------------------------------------------------------------------

fn run_validate_definition(g: &Golden) -> Result<(), String> {
    let out = capture((|| {
        let d = definition(g)?;
        screener::validate_definition(&d)?;
        Ok(json!({"valid": true}))
    })());
    g.check_output(&out)
}

fn run_resolve_columns(g: &Golden) -> Result<(), String> {
    let saved = str_list(g.inputs.get("saved"));
    let out = capture(screener::resolve_columns(&saved).map(|c| json!(c)));
    g.check_output(&out)
}

fn run_build_screen_query(g: &Golden) -> Result<(), String> {
    let out = capture((|| {
        let d = definition(g)?;
        let q =
            screener::build_screen_query(&d, date(g, "as_of"), &columns_input(g), limit_input(g))?;
        let mut doc = query_doc(
            &q.statement.compile(),
            &Common {
                as_of: q.as_of,
                index_id: q.index_id,
                universe_slug: &q.universe_slug,
                definition_hash: &q.definition_hash,
            },
        );
        doc.insert("columns".into(), json!(q.columns));
        doc.insert("sorting_factor".into(), factor_value(&q.sorting_factor));
        doc.insert(
            "ranking_factors".into(),
            Value::Array(q.ranking_factors.iter().map(factor_value).collect()),
        );
        Ok(Value::Object(doc))
    })());
    g.check_output(&out)
}

fn run_build_survivors_query(g: &Golden) -> Result<(), String> {
    let out = capture((|| {
        let d = definition(g)?;
        let q = screener::build_survivors_query(
            &d,
            date(g, "as_of"),
            &columns_input(g),
            limit_input(g),
        )?;
        let mut doc = query_doc(
            &q.statement.compile(),
            &Common {
                as_of: q.as_of,
                index_id: q.index_id,
                universe_slug: &q.universe_slug,
                definition_hash: &q.definition_hash,
            },
        );
        doc.insert("columns".into(), json!(q.columns));
        doc.insert("row_columns".into(), json!(q.row_columns));
        doc.insert("sorting_factor".into(), factor_value(&q.sorting_factor));
        doc.insert(
            "ranking_factors".into(),
            Value::Array(q.ranking_factors.iter().map(factor_value).collect()),
        );
        Ok(Value::Object(doc))
    })());
    g.check_output(&out)
}

fn run_build_ranking_frame_query(g: &Golden) -> Result<(), String> {
    let out = capture((|| {
        let d = definition(g)?;
        let q = screener::build_ranking_frame_query(&d, date(g, "as_of"))?;
        let mut doc = query_doc(
            &q.statement.compile(),
            &Common {
                as_of: q.as_of,
                index_id: q.index_id,
                universe_slug: &q.universe_slug,
                definition_hash: &q.definition_hash,
            },
        );
        doc.insert("term_columns".into(), json!(q.term_columns));
        doc.insert(
            "clause_details".into(),
            Value::Array(
                q.clause_details
                    .iter()
                    .map(|(k, v)| json!([k, v]))
                    .collect(),
            ),
        );
        doc.insert("fail_columns".into(), json!(q.fail_columns()));
        Ok(Value::Object(doc))
    })());
    g.check_output(&out)
}

fn run_build_export_query(g: &Golden) -> Result<(), String> {
    let out = capture((|| {
        let d = definition(g)?;
        let statement = screener::build_export_query(&d, date(g, "as_of"), limit_input(g))?;
        Ok(Value::Object(compiled_doc(&statement.compile())))
    })());
    g.check_output(&out)
}

fn run_cache_key(g: &Golden) -> Result<(), String> {
    let out = capture((|| {
        let d = definition(g)?;
        let version = g.input("data_version").unwrap().as_i64().unwrap();
        Ok(json!(screener::cache_key(
            &d,
            date(g, "as_of"),
            version,
            &columns_input(g)
        )?))
    })());
    g.check_output(&out)
}

fn run_canonical_json(g: &Golden) -> Result<(), String> {
    let out =
        capture(screener::canonical_json(&payload(g.input("payload").unwrap())).map(|s| json!(s)));
    g.check_output(&out)
}

fn run_screen_result_to_json(g: &Golden) -> Result<(), String> {
    let out = capture((|| {
        let sf = g.input("sorting_factor").unwrap();
        let rows = g
            .input("rows")
            .unwrap()
            .as_array()
            .unwrap()
            .iter()
            .map(|r| {
                let ranks: Vec<i64> = r["ranks"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|x| x.as_i64().unwrap())
                    .collect();
                let values = match payload(&r["values"]) {
                    Payload::Map(entries) => entries,
                    other => panic!("values is not a mapping: {other:?}"),
                };
                ScreenResultRow {
                    rank: r["rank"].as_i64().unwrap(),
                    instrument_id: r["instrument_id"].as_i64().unwrap(),
                    combined_rank: r["combined_rank"].as_i64().unwrap(),
                    ranks: [ranks[0], ranks[1], ranks[2]],
                    values,
                }
            })
            .collect();
        let result = ScreenResult {
            as_of: date(g, "as_of"),
            data_version: g.input("data_version").unwrap().as_i64().unwrap(),
            sorting_factor: RankingFactor {
                position: sf["position"].as_i64().unwrap(),
                key: sf["key"].as_str().unwrap().to_owned(),
                label: sf["label"].as_str().unwrap().to_owned(),
                direction: sf["direction"].as_str().unwrap().to_owned(),
            },
            columns: str_list(g.inputs.get("columns")),
            rows,
            requested_as_of: None,
            truncated: false,
            ranking_factors: Vec::new(),
        };
        Ok(json!({
            "to_json": result.to_json()?,
            "result_count": result.result_count(),
            "symbols": result.symbols()?,
        }))
    })());
    g.check_output(&out)
}

// ---------------------------------------------------------------------------
// Grading
// ---------------------------------------------------------------------------

fn grade(fn_name: &str, expected: usize, run: fn(&Golden) -> Result<(), String>) {
    let cases: Vec<Golden> = baskfy_testkit::cases_for(fn_name).unwrap();
    assert_eq!(
        cases.len(),
        expected,
        "{fn_name}: expected {expected} goldens, found {}",
        cases.len()
    );
    let mut failures = Vec::new();
    for g in &cases {
        if let Err(why) = run(g) {
            failures.push(format!(
                "{} (known_bug: {:?}):\n{why}",
                g.case, g.meta.known_bug
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} {fn_name} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n\n")
    );
}

#[test]
fn registry_has_no_duplicate_keys() {
    assert!(
        fr::duplicate_keys().is_empty(),
        "{:?}",
        fr::duplicate_keys()
    );
}

#[test]
fn factor_registry_factors() {
    grade(&format!("{REG}FACTORS"), 1, run_registry_table);
}

#[test]
fn factor_registry_columns() {
    grade(&format!("{REG}columns"), 1, run_columns);
}

#[test]
fn factor_registry_by_family() {
    grade(&format!("{REG}by_family"), 1, run_by_family);
}

#[test]
fn factor_registry_family_counts() {
    grade(&format!("{REG}family_counts"), 1, run_family_counts);
}

#[test]
fn factor_registry_get() {
    grade(&format!("{REG}get"), 10, run_get);
}

#[test]
fn factor_registry_sql_for() {
    grade(&format!("{REG}sql_for"), 10, run_sql_for);
}

#[test]
fn screener_validate_definition() {
    grade(
        &format!("{SCR}validate_definition"),
        11,
        run_validate_definition,
    );
}

#[test]
fn screener_resolve_columns() {
    grade(&format!("{SCR}resolve_columns"), 6, run_resolve_columns);
}

#[test]
fn screener_build_screen_query() {
    grade(
        &format!("{SCR}build_screen_query"),
        14,
        run_build_screen_query,
    );
}

#[test]
fn screener_build_survivors_query() {
    grade(
        &format!("{SCR}build_survivors_query"),
        5,
        run_build_survivors_query,
    );
}

#[test]
fn screener_build_ranking_frame_query() {
    grade(
        &format!("{SCR}build_ranking_frame_query"),
        6,
        run_build_ranking_frame_query,
    );
}

#[test]
fn screener_build_export_query() {
    grade(
        &format!("{SCR}build_export_query"),
        5,
        run_build_export_query,
    );
}

#[test]
fn screener_cache_key() {
    grade(&format!("{SCR}cache_key"), 8, run_cache_key);
}

#[test]
fn screener_canonical_json() {
    grade(&format!("{SCR}canonical_json"), 9, run_canonical_json);
}

#[test]
fn screener_screen_result_to_json() {
    grade(
        &format!("{SCR}ScreenResult.to_json"),
        2,
        run_screen_result_to_json,
    );
}

#[test]
fn ninety_cases_in_all() {
    let total: usize = [
        "factor_registry.FACTORS",
        "factor_registry.columns",
        "factor_registry.by_family",
        "factor_registry.family_counts",
        "factor_registry.get",
        "factor_registry.sql_for",
        "screener.validate_definition",
        "screener.resolve_columns",
        "screener.build_screen_query",
        "screener.build_survivors_query",
        "screener.build_ranking_frame_query",
        "screener.build_export_query",
        "screener.cache_key",
        "screener.canonical_json",
        "screener.ScreenResult.to_json",
    ]
    .iter()
    .map(|f| {
        baskfy_testkit::cases_for(&format!("baskfy_core.{f}"))
            .unwrap()
            .len()
    })
    .sum();
    assert_eq!(total, 90);
}
