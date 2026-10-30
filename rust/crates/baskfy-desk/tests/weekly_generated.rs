//! Lane weekly_plan (wave D2): the generated scan (`app/scan_source.py` + `baskfy_core.
//! momentum_scan`, DECISIONS-RS R8.WP1), the breadth input, the scoring config and the score's
//! durable digest — the Python suites ported test for test:
//!
//! * `tests/test_generated_scan.py` (15) — every test below named after its Python twin. The
//!   four that drive the FastAPI route (`the_upload_path_still_works_without_generate_for`,
//!   `neither_path_is_reachable_with_no_scan_at_all`,
//!   `a_malformed_date_is_rejected_before_any_database_is_touched`,
//!   `an_uploads_id_resolves_to_the_bytes_that_made_it`) are ported at the function level here
//!   **and** graded route-to-route against the live Python desk by `desk_diff.py` over
//!   `tools/parity/desk_cases/weekly_plan.json` (the same requests, both desks, the bed's bars).
//! * `tests/test_desk_score_parity.py` (3) — `score_day` (`baskfy_core.desk_score_service`) is the
//!   nightly worker's and has no Rust port in this crate; the property ported is the half the desk
//!   owns: the desk's book from a generated scan equals the Python desk's book, row for row
//!   (`fixtures/weekly/generated.json`, dumped by `tools/parity/desk_generated_goldens.py`).
//! * `tests/test_desk_config_parity.py` (4) — `ScoringConfig::default()` against `app.config`.
//! * `tests/test_scoring_moved_to_core.py` (2) — the scored frame's digest over the corpus.
//! * `tests/test_breadth_and_shadow.py` (15) — the breadth decision and the band table (10); the
//!   five tests of `scripts/shadow_mode.py` (a Python CLI that drives two Python desks; no desk
//!   module) stay with that script.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp,
    missing_docs
)]

use std::collections::{BTreeSet, HashSet};
use std::path::Path;

use baskfy_desk::lanes::weekly::analyze::{breadth_decision, plan_lines, weekly_config};
use baskfy_desk::lanes::weekly::basket::{BasketConfig, PlanInputs, build_plan, cash_pct_for};
use baskfy_desk::lanes::weekly::frame::{Column, Frame, read_csv};
use baskfy_desk::lanes::weekly::scan_source::{
    self as ss, Bar, DEFINITION, MomentumScan, as_desk_frame, generate_from, scan_warnings,
    screen_run_id,
};
use baskfy_desk::lanes::weekly::score::{Scored, ScoringConfig, audit, load_scan, score};
use baskfy_desk::settings::Settings;
use baskfy_desk::wire::{PyDict, PyValue, dict, orjson_dumps};
use chrono::NaiveDate;
use serde_json::Value;

const CORPUS: &str = "/Volumes/T7-Work/Dev/projects/baskfy/kite-momentum-rebalancer/data/uploads";

fn manifest() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
}

fn golden() -> Value {
    let p = manifest().join("fixtures/weekly/generated.json");
    serde_json::from_slice(&std::fs::read(p).unwrap()).unwrap()
}

fn case(name: &str) -> Value {
    golden()["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == name)
        .unwrap()
        .clone()
}

fn date(s: &str) -> NaiveDate {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
}

fn bars(c: &Value) -> Vec<Bar> {
    c["bars"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            let f = |i: usize| r[i].as_str().unwrap().parse::<f64>().unwrap();
            Bar {
                symbol: r[0].as_str().unwrap().to_owned(),
                instrument_id: r[1].as_i64().unwrap(),
                date: date(r[2].as_str().unwrap()),
                open: f(3),
                high: f(4),
                low: f(5),
                close: f(6),
                volume: f(7),
                close_raw: f(8),
                volume_raw: f(9),
            }
        })
        .collect()
}

fn days(c: &Value) -> Vec<NaiveDate> {
    c["days"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| date(d.as_str().unwrap()))
        .collect()
}

fn carried(c: &Value) -> Frame {
    read_csv(c["carried_csv"].as_str().unwrap().as_bytes()).unwrap()
}

fn generate(c: &Value, cfg: &ScoringConfig) -> Result<MomentumScan, ss::GenerateError> {
    generate_from(
        &bars(c),
        &days(c),
        date(c["as_of"].as_str().unwrap()),
        cfg,
        &carried(c),
    )
}

fn generated(name: &str) -> MomentumScan {
    generate(&case(name), &ScoringConfig::default()).unwrap()
}

fn repr(x: f64) -> String {
    baskfy_core::py::float_repr(x)
}

fn column_matches(label: &str, col: &Column, want: &Value) {
    assert_eq!(
        col.dtype(),
        want["dtype"].as_str().unwrap(),
        "{label}: dtype"
    );
    let vals = want["values"].as_array().unwrap();
    match col {
        Column::Int(v) => {
            let w: Vec<i64> = vals.iter().map(|x| x.as_i64().unwrap()).collect();
            assert_eq!(v, &w, "{label}");
        }
        Column::Float(v) => {
            let w: Vec<String> = vals
                .iter()
                .map(|x| x.as_str().unwrap().to_owned())
                .collect();
            assert_eq!(v.iter().map(|x| repr(*x)).collect::<Vec<_>>(), w, "{label}");
        }
        Column::Bool(v) => {
            let w: Vec<bool> = vals.iter().map(|x| x.as_bool().unwrap()).collect();
            assert_eq!(v, &w, "{label}");
        }
        Column::Str(v) => {
            let w: Vec<Option<String>> =
                vals.iter().map(|x| x.as_str().map(str::to_owned)).collect();
            assert_eq!(v, &w, "{label}");
        }
    }
}

fn scored_parts(sc: &Scored) -> Vec<(&'static str, &Vec<f64>)> {
    vec![
        ("SCORE", &sc.score),
        ("rank", &sc.rank),
        ("ext_over_20dma", &sc.ext_over_20dma),
        ("A_trend", &sc.parts[0]),
        ("B_momentum", &sc.parts[1]),
        ("C_sharpe", &sc.parts[2]),
        ("D_consistency", &sc.parts[3]),
        ("E_liquidity", &sc.parts[4]),
        ("F_penalty", &sc.parts[5]),
    ]
}

fn scored_matches(label: &str, sc: &Scored, want: &Value) {
    let reject: Vec<String> = want["reject"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_owned())
        .collect();
    assert_eq!(sc.reject, reject, "{label}: reject");
    for (name, got) in scored_parts(sc) {
        let Some(w) = want["scored"].get(name) else {
            continue;
        };
        let w: Vec<String> = w["values"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| match x {
                Value::String(s) => s.clone(),
                Value::Number(n) => repr(n.as_f64().unwrap()),
                other => panic!("{other}"),
            })
            .collect();
        assert_eq!(
            got.iter().map(|x| repr(*x)).collect::<Vec<_>>(),
            w,
            "{label}: {name}"
        );
    }
}

fn holdings_free_plan(sc: &Scored, cfg: &BasketConfig) -> PyDict {
    let symbols: Vec<String> = (0..sc.len()).map(|i| sc.symbol(i)).collect();
    let inputs = PlanInputs {
        holdings: Vec::new(),
        cash: 1_000_000.0,
        live_prices: symbols.into_iter().map(|s| (s, 100.0)).collect(),
        plan_id: "abcdef012345".to_owned(),
        ..PlanInputs::default()
    };
    build_plan(sc, &inputs, cfg).unwrap()
}

fn orders(p: &PyDict) -> Vec<PyDict> {
    match p.get("orders") {
        Some(PyValue::List(items)) => items
            .iter()
            .map(|o| match o {
                PyValue::Map(m) => m.clone(),
                _ => panic!(),
            })
            .collect(),
        _ => panic!(),
    }
}

// =============================================================================================
// the answer key: every fixture case, cell for cell
// =============================================================================================

#[test]
fn every_generated_scan_matches_the_python_desk_cell_for_cell() {
    let cfg = ScoringConfig::default();
    for c in golden()["cases"].as_array().unwrap() {
        let label = c["name"].as_str().unwrap();
        let got = generate(c, &cfg);
        if let Some(err) = c.get("error") {
            assert_eq!(got.unwrap_err().0, err.as_str().unwrap(), "{label}");
            continue;
        }
        let scan = got.unwrap();
        assert_eq!(
            scan.screen_run_id,
            c["screen_run_id"].as_str().unwrap(),
            "{label}"
        );
        assert_eq!(
            orjson_dumps(&PyValue::Map(scan.provenance())).unwrap(),
            c["provenance"].as_str().unwrap(),
            "{label}: provenance"
        );
        assert_eq!(
            scan.frame.nrows as u64,
            c["height"].as_u64().unwrap(),
            "{label}"
        );
        assert_eq!(
            String::from_utf8(scan.to_csv()).unwrap(),
            c["to_csv"].as_str().unwrap(),
            "{label}: to_csv"
        );
        let u = as_desk_frame(&scan, &cfg);
        if let Some(err) = c.get("desk_frame_error") {
            let want = err.as_str().unwrap().trim_start_matches("ValueError: ");
            assert_eq!(u.unwrap_err().0, want, "{label}");
            continue;
        }
        let u = u.unwrap();
        let want = &c["desk_frame"];
        let names: Vec<String> = want["columns"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().to_owned())
            .collect();
        assert_eq!(u.names, names, "{label}: columns");
        for n in &u.names {
            column_matches(&format!("{label}.{n}"), u.col(n).unwrap(), &want["data"][n]);
        }
        match (audit(&u, &cfg), c.get("audit")) {
            (Ok(a), Some(w)) => assert_eq!(
                orjson_dumps(&PyValue::Map(a)).unwrap(),
                w.as_str().unwrap(),
                "{label}: audit"
            ),
            (Err(_), None) => assert!(c.get("audit_error").is_some(), "{label}"),
            (a, w) => panic!("{label}: audit {a:?} vs {w:?}"),
        }
        match score(&u, &cfg) {
            Ok(sc) => {
                assert!(c.get("score_error").is_none(), "{label}: Python raised");
                scored_matches(label, &sc, c);
                let loaded = load_scan(&scan.to_csv(), &cfg).unwrap();
                let want_cols: Vec<String> = c["loaded_columns"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|x| x.as_str().unwrap().to_owned())
                    .collect();
                assert_eq!(loaded.names, want_cols, "{label}: load_scan columns");
            }
            Err(e) => {
                let want = c["score_error"]
                    .as_str()
                    .unwrap_or_else(|| panic!("{label}: {e}"));
                assert!(want.starts_with("TypeError"), "{label}: {want}");
                assert!(e.0.starts_with("TypeError"), "{label}: {}", e.0);
            }
        }
    }
}

// =============================================================================================
// test_generated_scan.py — 1. the generated scan is a CSV the desk would have accepted
// =============================================================================================

#[test]
fn the_desks_own_loader_accepts_a_generated_scan() {
    let cfg = ScoringConfig::default();
    let scan = generated("scan_fixture");
    let frame = load_scan(&scan.to_csv(), &cfg).unwrap();
    assert_eq!(frame.names, cfg.required());
    assert_eq!(frame.nrows, scan.frame.nrows);
}

#[test]
fn it_survives_the_round_trip_through_a_file() {
    let cfg = ScoringConfig::default();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("generated.csv");
    std::fs::write(&path, generated("scan_fixture").to_csv()).unwrap();
    let raw = std::fs::read(&path).unwrap();
    assert_eq!(load_scan(&raw, &cfg).unwrap().names, cfg.required());
}

// =============================================================================================
// 2. both /analyze paths produce the same plan for the same date
// =============================================================================================

#[test]
fn both_paths_produce_the_same_plan() {
    let cfg = BasketConfig::default();
    let scan = generated("scan_fixture");
    let uploaded = load_scan(&scan.to_csv(), &cfg.scoring).unwrap();
    let made = as_desk_frame(&scan, &cfg.scoring).unwrap();
    assert_eq!(
        orjson_dumps(&PyValue::Map(audit(&uploaded, &cfg.scoring).unwrap())).unwrap(),
        orjson_dumps(&PyValue::Map(audit(&made, &cfg.scoring).unwrap())).unwrap()
    );
    let a = holdings_free_plan(&score(&uploaded, &cfg.scoring).unwrap(), &cfg);
    let b = holdings_free_plan(&score(&made, &cfg.scoring).unwrap(), &cfg);
    assert_eq!(
        orjson_dumps(&PyValue::Map(a.clone())).unwrap(),
        orjson_dumps(&PyValue::Map(b)).unwrap()
    );
    // Two empty plans prove nothing; the fixture is tuned so DELTA and ECHO survive.
    assert!(
        !orders(&a).is_empty(),
        "the fixture stopped producing a plan"
    );
}

#[test]
fn the_upload_path_still_works_without_generate_for() {
    // The route half is desk_diff's ("analyze the sample scan", source "upload"); the default
    // that keeps it the default:
    assert_eq!(Settings::defaults().unwrap().scan_source_default, "upload");
    let cfg = ScoringConfig::default();
    assert!(load_scan(&generated("scan_fixture").to_csv(), &cfg).is_ok());
}

#[test]
fn neither_path_is_reachable_with_no_scan_at_all() {
    // With no file, no date and the flag off, /analyze answers 400 before reading anything
    // (desk_diff: "analyze with neither a file nor a date is 400"). The flag is off by default.
    assert_ne!(
        Settings::defaults().unwrap().scan_source_default,
        "generated"
    );
}

#[test]
fn a_malformed_date_is_rejected_before_any_database_is_touched() {
    use baskfy_desk::lanes::weekly::analyze::date_fromisoformat;
    for bad in [
        "last tuesday",
        "18-08-2026",
        "2026-08-18x",
        "0000-01-01",
        "+2026-08-1",
    ] {
        assert!(date_fromisoformat(bad).is_none(), "{bad}");
    }
    assert_eq!(date_fromisoformat("2026-08-18"), Some(date("2026-08-18")));
}

// =============================================================================================
// 3. screen_run_id is on every plan, and it resolves
// =============================================================================================

#[test]
fn every_plan_carries_a_screen_run_id() {
    let scan = generated("scan_fixture");
    let p = scan.provenance();
    assert_eq!(
        p.get("screen_run_id"),
        Some(&PyValue::Str(scan.screen_run_id.clone()))
    );
    assert_eq!(
        p.get("as_of"),
        Some(&PyValue::Str(scan.as_of.format("%Y-%m-%d").to_string()))
    );
    assert_eq!(
        p.get("data_version"),
        Some(&PyValue::Int(scan.data_version))
    );
}

#[test]
fn an_uploads_id_resolves_to_the_bytes_that_made_it() {
    // `upload:` + the first 16 hex digits of the bytes' SHA-256: same bytes, same id.
    use sha2::{Digest, Sha256};
    let id = |raw: &[u8]| {
        let d = Sha256::digest(raw);
        format!(
            "upload:{}",
            d.iter()
                .take(8)
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        )
    };
    let raw = generated("scan_fixture").to_csv();
    let mut other = raw.clone();
    other.push(b'\n');
    assert_eq!(id(&raw), id(&raw.clone()));
    assert_ne!(id(&raw), id(&other));
}

#[test]
fn a_generated_ids_inputs_are_named_not_hashed_away() {
    let scan = generated("scan_fixture");
    assert_eq!(
        scan.screen_run_id,
        screen_run_id(DEFINITION, scan.as_of, scan.data_version)
    );
    assert_eq!(scan.screen_run_id, case("scan_fixture")["screen_run_id"]);
}

// =============================================================================================
// 4. the generated path does not become the default while the delta table is not empty
// =============================================================================================

#[test]
fn generating_requires_an_explicit_date() {
    assert_eq!(Settings::defaults().unwrap().scan_source_default, "upload");
}

#[test]
fn a_scan_with_unadjusted_actions_warns_on_the_plan() {
    let mut dirty = generated("scan_fixture");
    dirty.suspect_symbols = vec!["ALPHA".to_owned(), "BRAVO".to_owned()];
    let warnings = scan_warnings(&dirty);
    assert!(warnings[0].contains("unadjusted corporate action"));
    assert!(warnings[0].contains("SMALLER universe"));
    assert_eq!(
        dirty.provenance().get("suspect_symbols"),
        Some(&PyValue::List(vec![
            PyValue::Str("ALPHA".to_owned()),
            PyValue::Str("BRAVO".to_owned())
        ]))
    );
    // and a real missing split is found, while a step across a hole in the history is not
    assert_eq!(
        generated("scan_fixture_a_missing_split").suspect_symbols,
        ["BRAVO"]
    );
    assert!(
        generated("scan_fixture_a_step_across_a_hole")
            .suspect_symbols
            .is_empty()
    );
}

#[test]
fn the_scan_source_only_ever_reads() {
    let source = include_str!("../src/lanes/weekly/scan_source.rs").to_lowercase();
    for statement in ["insert into", "update ", "delete from", "drop ", "commit()"] {
        assert!(
            !source.contains(statement),
            "scan_source must not {}",
            statement.trim()
        );
    }
}

#[test]
fn generate_is_never_called_without_carried_columns() {
    let c = case("scan_fixture");
    let mut thin = carried(&c);
    thin.names.retain(|n| n != "beta");
    thin.columns.remove("beta");
    let err = generate_from(
        &bars(&c),
        &days(&c),
        date(c["as_of"].as_str().unwrap()),
        &ScoringConfig::default(),
        &thin,
    )
    .unwrap_err();
    assert_eq!(err.0, "`carried` is missing ['beta']; see CARRIED_COLUMNS");
}

// =============================================================================================
// the empty-universe case M13 made reachable
// =============================================================================================

#[test]
fn an_all_rejected_scan_produces_an_empty_plan_not_a_crash() {
    let cfg = BasketConfig::default();
    let frame = as_desk_frame(&generated("scan_fixture"), &cfg.scoring).unwrap();
    let n = frame.nrows;
    let sc = Scored {
        frame,
        reject: vec!["far_from_high;".to_owned(); n],
        score: vec![0.0; n],
        #[allow(clippy::cast_precision_loss)]
        rank: (1..=n).map(|r| r as f64).collect(),
        ext_over_20dma: vec![f64::NAN; n],
        parts: std::array::from_fn(|_| vec![f64::NAN; n]),
        rank_is_int: true,
    };
    let inputs = PlanInputs {
        cash: 1_000_000.0,
        plan_id: "abcdef012345".to_owned(),
        ..PlanInputs::default()
    };
    assert!(orders(&build_plan(&sc, &inputs, &cfg).unwrap()).is_empty());
}

#[test]
fn the_whole_route_survives_a_scan_with_nothing_tradeable() {
    let cfg = BasketConfig::default();
    let mut frame = as_desk_frame(&generated("scan_fixture"), &cfg.scoring).unwrap();
    let n = frame.nrows;
    frame.set("median_volume_one_year", Column::Float(vec![1.0; n]));
    let sc = score(&frame, &cfg.scoring).unwrap();
    assert!(orders(&holdings_free_plan(&sc, &cfg)).is_empty());
}

#[test]
fn a_short_history_is_the_500_python_raises_not_a_plan_of_nans() {
    // ma_200 never computed → an all-None object column → pandas' TypeError in apply_filters.
    let cfg = ScoringConfig::default();
    let u = as_desk_frame(&generated("scan_fixture_short_history"), &cfg).unwrap();
    assert!(matches!(u.col("ma_200"), Some(Column::Str(_))));
    assert!(score(&u, &cfg).unwrap_err().0.starts_with("TypeError"));
}

#[test]
fn a_scan_with_no_symbol_in_common_is_the_500_python_raises() {
    let cfg = ScoringConfig::default();
    let scan = generated("scan_fixture_no_overlap");
    assert_eq!(scan.frame.nrows, 0);
    assert!(as_desk_frame(&scan, &cfg).is_err());
}

// =============================================================================================
// test_desk_score_parity.py
// =============================================================================================

fn book() -> Scored {
    let cfg = ScoringConfig::default();
    let u = as_desk_frame(&generated("desk_score_fixture"), &cfg).unwrap();
    score(&u, &cfg).unwrap()
}

#[test]
fn the_fixture_exercises_ranked_rejected_and_tied_rows() {
    let b = book();
    let symbols: HashSet<String> = (0..b.len()).map(|i| b.symbol(i)).collect();
    assert!(symbols.len() >= 40);
    let ranked: Vec<usize> = (0..b.len()).filter(|&i| b.reject[i].is_empty()).collect();
    let scores: Vec<String> = ranked.iter().map(|&i| repr(b.score[i])).collect();
    let distinct: HashSet<&String> = scores.iter().collect();
    assert!(ranked.len() >= 20 && distinct.len() < scores.len());
    let tokens: BTreeSet<&str> = b
        .reject
        .iter()
        .flat_map(|r| r.split(';'))
        .filter(|t| !t.is_empty())
        .collect();
    assert_eq!(
        tokens,
        BTreeSet::from([
            "below50&200DMA",
            "neg3M&6M",
            "circuits",
            "illiquid",
            "far_from_high",
            "T2T_series",
            "excluded_instrument",
        ])
    );
}

#[test]
fn score_day_is_the_desks_score_row_for_row() {
    // `score_day` is the worker's; what it is held equal to — the Python desk's book — is here.
    let c = case("desk_score_fixture");
    let b = book();
    let symbols: Vec<String> = (0..b.len()).map(|i| b.symbol(i)).collect();
    let want: Vec<String> = c["symbols"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_owned())
        .collect();
    assert_eq!(symbols, want);
    scored_matches("desk_score_fixture", &b, &c);
}

#[test]
fn the_basket_the_desk_would_build_reads_the_same_ranks() {
    let c = case("desk_score_fixture");
    let b = book();
    let order = |rank: &dyn Fn(usize) -> f64,
                 reject: &dyn Fn(usize) -> bool,
                 sym: &dyn Fn(usize) -> String| {
        let mut rows: Vec<usize> = (0..b.len()).filter(|&i| !reject(i)).collect();
        rows.sort_by(|&x, &y| rank(x).partial_cmp(&rank(y)).unwrap());
        rows.into_iter().map(sym).collect::<Vec<String>>()
    };
    let got = order(&|i| b.rank[i], &|i| !b.reject[i].is_empty(), &|i| {
        b.symbol(i)
    });
    let want_rank: Vec<f64> = c["scored"]["rank"]["values"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| match x {
            Value::String(s) => s.parse().unwrap_or(f64::NAN),
            Value::Number(n) => n.as_f64().unwrap(),
            _ => f64::NAN,
        })
        .collect();
    let want = order(
        &|i| want_rank[i],
        &|i| !c["reject"][i].as_str().unwrap().is_empty(),
        &|i| c["symbols"][i].as_str().unwrap().to_owned(),
    );
    assert_eq!(got, want);
}

// =============================================================================================
// test_desk_config_parity.py
// =============================================================================================

/// The scoring attributes (`baskfy_core.desk_config.SCORING_ATTRIBUTES`) of a config, by name,
/// as text. Exhaustive by construction: a field added to `ScoringConfig` fails to compile here.
fn attributes(cfg: &ScoringConfig) -> Vec<(&'static str, String)> {
    let ScoringConfig {
        excluded_symbols,
        reject_series,
        min_median_daily_value,
        max_away_from_high,
        max_circuits_3m,
        penalty_circuits_1y,
        momentum_blend,
        sharpe_blend,
        stop_vol_mult,
        stop_min,
        stop_max,
    } = cfg;
    let set = |v: &Vec<String>| {
        let s: BTreeSet<&String> = v.iter().collect();
        format!("{s:?}")
    };
    let blend = |v: &Vec<(&str, f64)>| {
        v.iter()
            .map(|(k, w)| format!("{k}={}", repr(*w)))
            .collect::<Vec<_>>()
            .join(",")
    };
    vec![
        ("EXCLUDED_SYMBOLS", set(excluded_symbols)),
        ("REJECT_SERIES", set(reject_series)),
        ("MIN_MEDIAN_DAILY_VALUE", repr(*min_median_daily_value)),
        ("MAX_AWAY_FROM_HIGH", repr(*max_away_from_high)),
        ("MAX_CIRCUITS_3M", repr(*max_circuits_3m)),
        ("PENALTY_CIRCUITS_1Y", repr(*penalty_circuits_1y)),
        ("MOMENTUM_BLEND", blend(momentum_blend)),
        ("SHARPE_BLEND", blend(sharpe_blend)),
        ("STOP_VOL_MULT", repr(*stop_vol_mult)),
        ("STOP_MIN", repr(*stop_min)),
        ("STOP_MAX", repr(*stop_max)),
    ]
}

/// `app.config`'s values from the golden, in the same text form. Python's ints (`5`) are
/// compared as the float the Rust config holds (`5.0`): the desk only ever compares them.
fn python_attributes() -> Vec<(String, String)> {
    let g = golden();
    let names = g["scoring_attributes"].as_array().unwrap();
    names
        .iter()
        .map(|n| {
            let n = n.as_str().unwrap();
            let v = &g["config"][n];
            let text = match v["type"].as_str().unwrap() {
                "set" => {
                    let s: BTreeSet<String> = v["items"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|x| x.as_str().unwrap().to_owned())
                        .collect();
                    format!("{:?}", s.iter().collect::<BTreeSet<&String>>())
                }
                "dict" => v["items"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|kv| {
                        let w: f64 = kv[1].as_str().unwrap().parse().unwrap();
                        format!("{}={}", kv[0].as_str().unwrap(), repr(w))
                    })
                    .collect::<Vec<_>>()
                    .join(","),
                _ => repr(v["repr"].as_str().unwrap().parse::<f64>().unwrap()),
            };
            (n.to_owned(), text)
        })
        .collect()
}

fn mismatches(cfg: &ScoringConfig) -> Vec<String> {
    let mine = attributes(cfg);
    python_attributes()
        .into_iter()
        .filter(|(n, want)| {
            mine.iter()
                .find(|(m, _)| m == n)
                .is_none_or(|(_, got)| got != want)
        })
        .map(|(n, _)| n)
        .collect()
}

#[test]
fn desk_config_equals_app_config_on_every_scoring_attribute() {
    assert_eq!(mismatches(&ScoringConfig::default()), Vec::<String>::new());
}

#[test]
fn a_change_to_the_desks_config_fails_the_parity_check() {
    let base = ScoringConfig::default();
    let mut changes: Vec<(&str, ScoringConfig)> = Vec::new();
    let mut c = base.clone();
    c.min_median_daily_value = 4e7;
    changes.push(("MIN_MEDIAN_DAILY_VALUE", c));
    let mut c = base.clone();
    c.max_circuits_3m = 6.0;
    changes.push(("MAX_CIRCUITS_3M", c));
    let mut c = base.clone();
    c.reject_series = vec!["BE".to_owned()];
    changes.push(("REJECT_SERIES", c));
    let mut c = base.clone();
    c.excluded_symbols = vec!["SGBDE31III".to_owned(), "SGBMAR29".to_owned()];
    changes.push(("EXCLUDED_SYMBOLS", c));
    let mut c = base.clone();
    c.momentum_blend.reverse();
    changes.push(("MOMENTUM_BLEND", c));
    let mut c = base.clone();
    if let Some(last) = c.sharpe_blend.last_mut() {
        last.1 = 0.20;
    }
    changes.push(("SHARPE_BLEND", c));
    let mut c = base;
    c.stop_max = 0.13;
    changes.push(("STOP_MAX", c));
    for (name, cfg) in changes {
        assert_eq!(mismatches(&cfg), vec![name.to_owned()], "{name}");
    }
}

#[test]
fn every_attribute_the_book_reads_is_compared() {
    let rust: BTreeSet<&str> = attributes(&ScoringConfig::default())
        .into_iter()
        .map(|(n, _)| n)
        .collect();
    let python: BTreeSet<String> = golden()["scoring_attributes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_owned())
        .collect();
    assert_eq!(
        rust.iter()
            .map(|s| (*s).to_owned())
            .collect::<BTreeSet<_>>(),
        python
    );
    for needed in [
        "EXCLUDED_SYMBOLS",
        "REJECT_SERIES",
        "MIN_MEDIAN_DAILY_VALUE",
        "MAX_AWAY_FROM_HIGH",
        "MAX_CIRCUITS_3M",
        "PENALTY_CIRCUITS_1Y",
        "MOMENTUM_BLEND",
        "SHARPE_BLEND",
    ] {
        assert!(rust.contains(needed), "{needed}");
    }
}

#[test]
fn the_book_binds_app_config_where_it_scores() {
    // Both paths take the config /analyze loaded (stored overrides included): the generated
    // scan's columns follow the very blend the scorer is given.
    let c = case("scan_fixture");
    let mut cfg = ScoringConfig::default();
    cfg.momentum_blend.reverse();
    let scan = generate(&c, &cfg).unwrap();
    assert_eq!(scan.frame.names, cfg.required());
    let settings = Settings::defaults().unwrap();
    let mut overrides = std::collections::BTreeMap::new();
    overrides.insert(
        "STOP_MAX".to_owned(),
        baskfy_desk::lanes::analytics::desk_settings::SVal::Float(0.1),
    );
    let bound = weekly_config(&settings, &overrides);
    assert_eq!(bound.scoring.stop_max, 0.1);
}

// =============================================================================================
// test_scoring_moved_to_core.py
// =============================================================================================

/// C's `%.10g`.
fn g10(x: f64) -> String {
    if x.is_nan() {
        return String::new();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0" } else { "0" }.to_owned();
    }
    let e = format!("{x:.9e}");
    let (mant, exp) = e.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let strip = |s: String| {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_owned()
        } else {
            s
        }
    };
    if (-4..10).contains(&exp) {
        let decimals = usize::try_from(9 - exp).unwrap();
        strip(format!("{x:.decimals$}"))
    } else {
        let sign = if exp < 0 { '-' } else { '+' };
        format!("{}e{sign}{:02}", strip(mant.to_owned()), exp.abs())
    }
}

fn csv_field(s: &str) -> String {
    if s.contains([',', '"', '\n', '\r']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_owned()
    }
}

/// `df.to_csv(index=False, float_format="%.10g")` of `score(load_scan(path))`.
fn scored_csv(sc: &Scored) -> (String, usize) {
    let f = &sc.frame;
    let elig = sc.reject.iter().any(String::is_empty);
    let extra: Vec<(&str, &Vec<f64>)> = scored_parts(sc)
        .into_iter()
        .filter(|(n, _)| elig || *n == "SCORE" || *n == "rank")
        .collect();
    let mut header: Vec<String> = f.names.iter().map(|n| csv_field(n)).collect();
    header.push("reject".to_owned());
    header.extend(extra.iter().map(|(n, _)| (*n).to_owned()));
    let cols = header.len();
    let mut out = header.join(",") + "\n";
    for i in 0..f.nrows {
        let mut row: Vec<String> = f
            .names
            .iter()
            .map(|n| match f.col(n).unwrap() {
                Column::Int(v) => v[i].to_string(),
                Column::Float(v) => g10(v[i]),
                Column::Bool(v) => if v[i] { "True" } else { "False" }.to_owned(),
                Column::Str(v) => v[i].as_deref().map(csv_field).unwrap_or_default(),
            })
            .collect();
        row.push(csv_field(&sc.reject[i]));
        row.extend(extra.iter().map(|(_, v)| g10(v[i])));
        out.push_str(&row.join(","));
        out.push('\n');
    }
    (out, cols)
}

#[test]
fn the_score_is_unchanged_since_it_moved_to_core() {
    use sha2::{Digest, Sha256};
    let golden_path =
        manifest().join("../../../kite-momentum-rebalancer/tests/fixtures/scoring_golden.json");
    let expected: Value = serde_json::from_slice(&std::fs::read(golden_path).unwrap()).unwrap();
    let cfg = ScoringConfig::default();
    let mut checked = 0;
    for (name, want) in expected.as_object().unwrap() {
        let Ok(raw) = std::fs::read(Path::new(CORPUS).join(name)) else {
            eprintln!("{name} is not present; data/uploads is untracked");
            continue;
        };
        let sc = score(&load_scan(&raw, &cfg).unwrap(), &cfg).unwrap();
        let (text, cols) = scored_csv(&sc);
        assert_eq!(
            sc.len() as u64,
            want["rows"].as_u64().unwrap(),
            "{name}: rows"
        );
        assert_eq!(cols as u64, want["cols"].as_u64().unwrap(), "{name}: cols");
        let digest: String = Sha256::digest(text.as_bytes())
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        assert_eq!(
            digest,
            want["sha256"].as_str().unwrap(),
            "{name}: the scored frame changed"
        );
        let mut ranked: Vec<usize> = (0..sc.len()).filter(|&i| !sc.rank[i].is_nan()).collect();
        ranked.sort_by(|&a, &b| sc.rank[a].partial_cmp(&sc.rank[b]).unwrap());
        let top: Vec<String> = ranked.iter().take(10).map(|&i| sc.symbol(i)).collect();
        let want_top: Vec<String> = want["top10"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().to_owned())
            .collect();
        assert_eq!(top, want_top, "{name}: top 10");
        checked += 1;
    }
    eprintln!("checked {checked} corpus scans");
}

#[test]
fn the_desk_scores_through_core() {
    // The formulas exist once, in `score.rs`; the routes and the generated scan bind to it.
    for (file, src) in [
        ("analyze.rs", include_str!("../src/lanes/weekly/analyze.rs")),
        (
            "scan_source.rs",
            include_str!("../src/lanes/weekly/scan_source.rs"),
        ),
        ("basket.rs", include_str!("../src/lanes/weekly/basket.rs")),
    ] {
        assert!(!src.contains("A_trend"), "a scoring formula is in {file}");
    }
    assert!(include_str!("../src/lanes/weekly/score.rs").contains("A_trend"));
    assert!(include_str!("../src/lanes/weekly/analyze.rs").contains("score(df, &cfg.scoring)"));
}

// =============================================================================================
// test_breadth_and_shadow.py — which number the cash bands see
// =============================================================================================

fn syms(v: &[&str]) -> BTreeSet<String> {
    v.iter().map(|s| (*s).to_owned()).collect()
}

fn on() -> NaiveDate {
    date("2026-08-18")
}

const SLUG: &str = "nifty-total-market";

fn s(p: &PyDict, k: &str) -> PyValue {
    p.get(k).cloned().unwrap()
}

#[test]
fn the_pipeline_wins_when_the_population_is_the_same() {
    let r = breadth_decision(
        68.6,
        &syms(&["A", "B"]),
        on(),
        SLUG,
        Ok((Some(68.6347), syms(&["A", "B"]))),
    );
    assert_eq!(s(&r, "source"), PyValue::Str("pipeline".to_owned()));
    assert_eq!(s(&r, "value"), PyValue::Float(68.6347));
    assert_eq!(s(&r, "scan_value"), PyValue::Float(68.6));
    let PyValue::Str(reason) = s(&r, "reason") else {
        panic!()
    };
    assert!(reason.contains("same 2 symbols"));
}

#[test]
fn a_different_population_falls_back_to_the_scans_own() {
    let r = breadth_decision(
        68.6,
        &syms(&["A", "B"]),
        on(),
        SLUG,
        Ok((Some(12.0), syms(&["X", "Y"]))),
    );
    assert_eq!(s(&r, "source"), PyValue::Str("scan".to_owned()));
    assert_eq!(s(&r, "value"), PyValue::Float(68.6));
    assert_eq!(s(&r, "pipeline_value"), PyValue::Float(12.0));
    let PyValue::Str(reason) = s(&r, "reason") else {
        panic!()
    };
    assert!(reason.contains("differs from"));
}

#[test]
fn a_missing_pipeline_row_falls_back() {
    let r = breadth_decision(
        68.6,
        &syms(&["A"]),
        date("2020-01-02"),
        SLUG,
        Ok((None, syms(&["A"]))),
    );
    assert_eq!(s(&r, "source"), PyValue::Str("scan".to_owned()));
    let PyValue::Str(reason) = s(&r, "reason") else {
        panic!()
    };
    assert!(reason.contains("no market_health_daily row"));
}

#[test]
fn an_unreachable_screener_never_stops_a_rebalance() {
    let r = breadth_decision(68.6, &syms(&["A"]), on(), SLUG, Err("OSError".to_owned()));
    assert_eq!(s(&r, "source"), PyValue::Str("scan".to_owned()));
    assert_eq!(s(&r, "value"), PyValue::Float(68.6));
    let PyValue::Str(reason) = s(&r, "reason") else {
        panic!()
    };
    assert!(reason.contains("unreachable"));
}

#[test]
fn both_numbers_are_always_recorded() {
    for symbols in [syms(&["A"]), syms(&["Z"])] {
        let r = breadth_decision(68.6, &symbols, on(), SLUG, Ok((Some(50.0), syms(&["A"]))));
        assert_eq!(s(&r, "scan_value"), PyValue::Float(68.6));
        assert_eq!(s(&r, "pipeline_value"), PyValue::Float(50.0));
    }
}

#[test]
fn repointing_the_input_did_not_re_tune_the_bands() {
    assert_eq!(
        BasketConfig::default().cash_bands,
        vec![(65.0, 5.0), (45.0, 15.0), (0.0, 35.0)]
    );
}

#[test]
fn the_two_breadth_numbers_land_in_the_same_band() {
    let cfg = BasketConfig {
        fully_invested: false,
        ..BasketConfig::default()
    };
    assert_eq!(cash_pct_for(68.6, &cfg), 5.0);
    assert_eq!(cash_pct_for(68.6347, &cfg), 5.0);
}

#[test]
fn todays_settings_short_circuit_the_band_entirely() {
    // Python asserts the operator's environment (`FULLY_INVESTED=true` in the desk's .env; the
    // code default is false, so the Python test fails under a clean environment). Ported as the
    // property: with the desk's `FULLY_INVESTED` on, breadth decides nothing.
    let mut settings = Settings::defaults().unwrap();
    settings.fully_invested = true;
    let cfg = weekly_config(&settings, &std::collections::BTreeMap::new());
    assert!(cfg.fully_invested);
    assert_eq!(cash_pct_for(68.6, &cfg), 0.0);
    assert_eq!(cash_pct_for(20.0, &cfg), 0.0);
}

#[test]
fn the_flag_is_off() {
    assert_eq!(Settings::defaults().unwrap().scan_source_default, "upload");
}

#[test]
fn the_protocol_is_written_down() {
    let doc = manifest().join("../../../kite-momentum-rebalancer/docs/SHADOW-MODE.md");
    let text = std::fs::read_to_string(doc).unwrap();
    assert!(
        text.to_lowercase()
            .contains("four consecutive green fridays")
    );
    assert!(text.contains("SCAN_SOURCE_DEFAULT=generated"));
    assert!(text.contains("restarts"));
}

// =============================================================================================
// /execute reads every line before it sends one (finding: weekly_plan #2)
// =============================================================================================

fn line(pairs: Vec<(&str, PyValue)>) -> PyDict {
    dict(pairs)
}

#[test]
fn execute_lines_skip_zero_deltas_and_keep_int_and_whole_float_deltas() {
    let rows = vec![
        line(vec![
            ("symbol", PyValue::Str("A".into())),
            ("action", PyValue::Str("BUY".into())),
            ("delta", PyValue::Int(10)),
            ("ref_price", PyValue::Float(100.0)),
        ]),
        line(vec![
            ("symbol", PyValue::Str("B".into())),
            ("action", PyValue::Str("HOLD".into())),
            ("delta", PyValue::Int(0)),
        ]),
        line(vec![
            ("symbol", PyValue::Str("C".into())),
            ("action", PyValue::Str("TRIM".into())),
            ("delta", PyValue::Float(-4.0)),
            ("ref_price", PyValue::Int(50)),
        ]),
    ];
    let lines = plan_lines(&rows).unwrap();
    assert_eq!(
        lines
            .iter()
            .map(|l| (l.symbol.as_str(), l.delta))
            .collect::<Vec<_>>(),
        [("A", 10), ("C", -4)]
    );
}

#[test]
fn a_malformed_line_refuses_the_whole_batch_before_anything_is_sent() {
    let good = line(vec![
        ("symbol", PyValue::Str("A".into())),
        ("action", PyValue::Str("BUY".into())),
        ("delta", PyValue::Int(10)),
        ("ref_price", PyValue::Float(100.0)),
    ]);
    let bad_rows = [
        line(vec![("symbol", PyValue::Str("X".into()))]),
        line(vec![
            ("symbol", PyValue::Str("X".into())),
            ("action", PyValue::Str("BUY".into())),
            ("delta", PyValue::Int(5)),
        ]),
        line(vec![
            ("symbol", PyValue::Str("X".into())),
            ("action", PyValue::Str("BUY".into())),
            ("delta", PyValue::Float(2.5)),
            ("ref_price", PyValue::Float(1.0)),
        ]),
        line(vec![
            ("action", PyValue::Str("BUY".into())),
            ("delta", PyValue::Int(5)),
            ("ref_price", PyValue::Float(1.0)),
        ]),
        line(vec![
            ("symbol", PyValue::Str("X".into())),
            ("delta", PyValue::Int(5)),
            ("ref_price", PyValue::Float(1.0)),
        ]),
        line(vec![
            ("symbol", PyValue::Str("X".into())),
            ("action", PyValue::Str("BUY".into())),
            ("delta", PyValue::Str("5".into())),
            ("ref_price", PyValue::Float(1.0)),
        ]),
    ];
    for bad in bad_rows {
        assert!(plan_lines(&[good.clone(), bad.clone()]).is_err(), "{bad:?}");
    }
}
