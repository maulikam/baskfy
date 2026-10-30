//! Lane weekly: the scorer, the planner and the cost model, graded against the Python desk.
//!
//! The answer key is `fixtures/weekly/*.json`, dumped from the live Python by
//! `tools/parity/desk_weekly_goldens.py` (numpy 2.5, pandas 3.0, the worktree's `app` and
//! `baskfy_core`). Nothing here is tolerance-compared: floats are compared by their Python repr,
//! plans by their orjson bytes.
//!
//! The spec tests at the bottom port `test_no_trade_band.py`, `test_rebalance_pricing.py`,
//! `test_basket_moved_to_core.py` and `test_costs.py`'s assertions (the properties, not the
//! numbers), so a change that keeps the goldens but breaks a rule still fails.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::print_stderr,
    clippy::float_cmp,
    missing_docs
)]

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use baskfy_desk::lanes::weekly::basket::{
    BasketConfig, PlanInputs, build_plan, cash_pct_for, inside_hold_band, subtract_months,
    tradeable,
};
use baskfy_desk::lanes::weekly::costs::{CostLine, cost_pct, order_cost, plan_cost};
use baskfy_desk::lanes::weekly::frame::{Column, read_csv};
use baskfy_desk::lanes::weekly::pandas::{
    clip, nargsort, np_round, py_round, py_round_int, quantile, rank_pct,
};
use baskfy_desk::lanes::weekly::score::{Scored, ScoringConfig, audit, load_scan, score};
use baskfy_desk::wire::{PyDict, PyValue, orjson_dumps};
use serde_json::Value;

const CORPUS: &str = "/Volumes/T7-Work/Dev/projects/baskfy/kite-momentum-rebalancer/data/uploads";

fn fixture(name: &str) -> Value {
    let p = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("fixtures")
        .join("weekly")
        .join(name);
    serde_json::from_slice(&std::fs::read(&p).unwrap()).unwrap()
}

fn f(s: &Value) -> f64 {
    match s.as_str().unwrap() {
        "nan" => f64::NAN,
        "inf" => f64::INFINITY,
        "-inf" => f64::NEG_INFINITY,
        t => t.parse().unwrap(),
    }
}

fn repr(x: f64) -> String {
    baskfy_core::py::float_repr(x)
}

fn reprs(v: &[f64]) -> Vec<String> {
    v.iter().map(|x| repr(*x)).collect()
}

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_owned())
        .collect()
}

// ---------------------------------------------------------------------------------------------
// primitives
// ---------------------------------------------------------------------------------------------

#[test]
fn numpy_and_pandas_primitives_match_bit_for_bit() {
    let fx = fixture("primitives.json");
    for (k, case) in fx["cases"].as_array().unwrap().iter().enumerate() {
        let vals: Vec<f64> = case["values"].as_array().unwrap().iter().map(f).collect();
        let want = |key: &str| -> Vec<usize> {
            case[key]
                .as_array()
                .unwrap()
                .iter()
                .map(|x| usize::try_from(x.as_u64().unwrap()).unwrap())
                .collect()
        };
        assert_eq!(nargsort(&vals, false), want("desc"), "case {k} desc");
        assert_eq!(nargsort(&vals, true), want("asc"), "case {k} asc");
        let rank: Vec<f64> = rank_pct(&vals).into_iter().map(|x| x * 100.0).collect();
        assert_eq!(reprs(&rank), strs(&case["rank_pct"]), "case {k} rank");
        let q = [quantile(&vals, 0.01), quantile(&vals, 0.99)];
        assert_eq!(reprs(&q), strs(&case["q01_q99"]), "case {k} quantile");
        assert_eq!(
            repr(quantile(&vals, 0.85)),
            case["q85"].as_str().unwrap(),
            "case {k} q85"
        );
        let r1: Vec<f64> = vals.iter().map(|x| np_round(*x, 1)).collect();
        assert_eq!(reprs(&r1), strs(&case["np_round1"]), "case {k} np.round");
        if !vals.is_empty() {
            let c: Vec<f64> = vals.iter().map(|x| clip(*x, q[0], q[1])).collect();
            assert_eq!(reprs(&c), strs(&case["clipped"]), "case {k} clip");
        }
        for (nd, key) in [(1, "py_round1"), (2, "py_round2"), (3, "py_round3")] {
            let r: Vec<f64> = vals.iter().map(|x| py_round(*x, nd)).collect();
            assert_eq!(reprs(&r), strs(&case[key]), "case {k} {key}");
        }
        let r0: Vec<Option<i64>> = vals.iter().map(|x| py_round_int(*x)).collect();
        let want0: Vec<Option<i64>> = case["py_round0"]
            .as_array()
            .unwrap()
            .iter()
            .map(Value::as_i64)
            .collect();
        assert_eq!(r0, want0, "case {k} round()");
    }
    for h in fx["halves"].as_array().unwrap() {
        let v = f(&h["v"]);
        assert_eq!(
            repr(np_round(v, 1)),
            h["np_round1"].as_str().unwrap(),
            "{v}"
        );
        assert_eq!(
            repr(py_round(v, 1)),
            h["py_round1"].as_str().unwrap(),
            "{v}"
        );
        assert_eq!(
            repr(py_round(v, 2)),
            h["py_round2"].as_str().unwrap(),
            "{v}"
        );
        assert_eq!(
            repr(py_round(v, 3)),
            h["py_round3"].as_str().unwrap(),
            "{v}"
        );
        assert_eq!(py_round_int(v), h["py_round0"].as_i64(), "{v}");
    }
}

// ---------------------------------------------------------------------------------------------
// scans
// ---------------------------------------------------------------------------------------------

fn column_matches(name: &str, col: &Column, want: &Value) {
    assert_eq!(
        col.dtype(),
        want["dtype"].as_str().unwrap(),
        "dtype of {name}"
    );
    let vals = want["values"].as_array().unwrap();
    match col {
        Column::Int(v) => {
            let w: Vec<i64> = vals.iter().map(|x| x.as_i64().unwrap()).collect();
            assert_eq!(v, &w, "{name}");
        }
        Column::Float(v) => {
            let w = strs(&want["values"]);
            assert_eq!(reprs(v), w, "{name}");
        }
        Column::Bool(v) => {
            let w: Vec<bool> = vals.iter().map(|x| x.as_bool().unwrap()).collect();
            assert_eq!(v, &w, "{name}");
        }
        Column::Str(v) => {
            let w: Vec<Option<String>> =
                vals.iter().map(|x| x.as_str().map(str::to_owned)).collect();
            assert_eq!(v, &w, "{name}");
        }
    }
}

fn scored_matches(label: &str, sc: &Scored, case: &Value) {
    assert_eq!(sc.reject, strs(&case["reject"]), "{label}: reject");
    let parts = [
        ("SCORE", &sc.score),
        ("rank", &sc.rank),
        ("ext_over_20dma", &sc.ext_over_20dma),
        ("A_trend", &sc.parts[0]),
        ("B_momentum", &sc.parts[1]),
        ("C_sharpe", &sc.parts[2]),
        ("D_consistency", &sc.parts[3]),
        ("E_liquidity", &sc.parts[4]),
        ("F_penalty", &sc.parts[5]),
    ];
    for (name, got) in parts {
        let Some(want) = case["scored"].get(name) else {
            continue;
        };
        let w: Vec<String> = want["values"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| match x {
                Value::String(s) => s.clone(),
                Value::Number(n) => {
                    // int64 columns (rank with no rejected row, A_trend) compare as floats
                    repr(n.as_f64().unwrap())
                }
                other => panic!("{other}"),
            })
            .collect();
        assert_eq!(reprs(got), w, "{label}: {name}");
    }
}

fn check_scan(label: &str, raw: &[u8], case: &Value, check_loaded: bool) {
    let cfg = ScoringConfig::default();
    let loaded = load_scan(raw, &cfg);
    if let Some(err) = case.get("error") {
        let e = loaded.expect_err(label);
        assert_eq!(e.0, err.as_str().unwrap(), "{label}");
        return;
    }
    let u = loaded.unwrap();
    if check_loaded {
        let want = &case["loaded"];
        assert_eq!(u.names, strs(&want["columns"]), "{label}: columns");
        for name in &u.names {
            column_matches(name, u.col(name).unwrap(), &want["data"][name]);
        }
    }
    let a = audit(&u, &cfg).unwrap();
    assert_eq!(
        orjson_dumps(&PyValue::Map(a)).unwrap(),
        case["audit"].as_str().unwrap(),
        "{label}: audit"
    );
    let sc = score(&u, &cfg).unwrap();
    scored_matches(label, &sc, case);
}

#[test]
fn synthetic_scans_load_audit_and_score_as_python() {
    let fx = fixture("scans.json");
    for case in fx["scans"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        check_scan(name, case["csv"].as_str().unwrap().as_bytes(), case, true);
    }
}

fn corpus_file(name: &str) -> Option<Vec<u8>> {
    let p = PathBuf::from(CORPUS).join(name);
    std::fs::read(p).ok()
}

#[test]
fn the_regression_corpus_scores_as_python() {
    let fx = fixture("corpus.json");
    let mut seen = 0;
    for case in fx["files"].as_array().unwrap() {
        let name = case["file"].as_str().unwrap();
        let Some(raw) = corpus_file(name) else {
            eprintln!("corpus file {name} not present; skipped");
            continue;
        };
        let digest = sha256_hex(&raw);
        assert_eq!(digest, case["sha256"].as_str().unwrap(), "{name} changed");
        check_scan(name, &raw, case, false);
        seen += 1;
    }
    eprintln!("{seen} corpus files graded");
}

fn sha256_hex(raw: &[u8]) -> String {
    use sha2::Digest as _;
    sha2::Sha256::digest(raw)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

// ---------------------------------------------------------------------------------------------
// plans
// ---------------------------------------------------------------------------------------------

fn py_from_json(text: &str) -> PyValue {
    PyValue::from_json(text.as_bytes()).unwrap()
}

fn cfg_from(over: &Value) -> BasketConfig {
    let mut cfg = BasketConfig::default();
    for (k, v) in over.as_object().unwrap() {
        match k.as_str() {
            "FULLY_INVESTED" => cfg.fully_invested = v.as_bool().unwrap(),
            "REPLACEMENT_EDGE" => cfg.replacement_edge = v.as_f64().unwrap(),
            "RETENTION_BUFFER" => cfg.retention_buffer = v.as_i64().unwrap(),
            "TARGET_POSITIONS" => {
                let a = v.as_array().unwrap();
                cfg.target_positions = (a[0].as_i64().unwrap(), a[1].as_i64().unwrap());
            }
            other => panic!("no override {other}"),
        }
    }
    cfg
}

fn scan_bytes(name: &str, scans: &Value) -> Option<Vec<u8>> {
    if let Some(case) = scans["scans"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == name)
    {
        return Some(case["csv"].as_str().unwrap().as_bytes().to_vec());
    }
    corpus_file(name)
}

fn uuid_hex12(hex32: &str) -> String {
    hex32[..12].to_owned()
}

#[test]
fn plans_are_byte_equal_to_python() {
    let fx = fixture("plans.json");
    let scans = fixture("scans.json");
    let mut graded = 0;
    for case in fx["cases"].as_array().unwrap() {
        let label = format!("{} / {}", case["scan"], case["name"]);
        let Some(raw) = scan_bytes(case["scan"].as_str().unwrap(), &scans) else {
            eprintln!("{label}: scan not present; skipped");
            continue;
        };
        let cfg = cfg_from(&case["cfg"]);
        let u = load_scan(&raw, &cfg.scoring).unwrap();
        let sc = score(&u, &cfg.scoring).unwrap();
        let holdings: Vec<PyDict> = match py_from_json(case["holdings"].as_str().unwrap()) {
            PyValue::List(items) => items
                .into_iter()
                .map(|h| match h {
                    PyValue::Map(m) => m,
                    _ => panic!(),
                })
                .collect(),
            _ => panic!(),
        };
        let cash = match py_from_json(case["cash"].as_str().unwrap()) {
            PyValue::Int(i) => i as f64,
            PyValue::Float(x) => x,
            _ => panic!(),
        };
        let live: Vec<(String, f64)> = match py_from_json(case["live"].as_str().unwrap()) {
            PyValue::Map(m) => m
                .iter()
                .map(|(k, v)| {
                    (
                        k.to_owned(),
                        match v {
                            PyValue::Int(i) => *i as f64,
                            PyValue::Float(x) => *x,
                            _ => panic!(),
                        },
                    )
                })
                .collect(),
            _ => panic!(),
        };
        let clusters: HashMap<String, String> = case["clusters"]
            .as_object()
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
                    .collect()
            })
            .unwrap_or_default();
        let inputs = PlanInputs {
            holdings,
            cash,
            live_prices: live,
            breadth_override: case["breadth"].as_f64(),
            clusters,
            plan_id: uuid_hex12(case["plan_id_uuid"].as_str().unwrap()),
        };
        let got = build_plan(&sc, &inputs, &cfg);
        if let Some(err) = case.get("error") {
            assert_eq!(got.unwrap_err().0, err.as_str().unwrap(), "{label}");
        } else {
            let got = orjson_dumps(&PyValue::Map(got.unwrap())).unwrap();
            assert_eq!(got, case["plan"].as_str().unwrap(), "{label}");
        }
        graded += 1;
    }
    assert!(graded >= 90, "only {graded} plans graded");
}

// ---------------------------------------------------------------------------------------------
// costs
// ---------------------------------------------------------------------------------------------

#[test]
fn the_cost_model_matches_python() {
    let fx = fixture("costs.json");
    for c in fx["single"].as_array().unwrap() {
        let v = f(&c["value"]);
        let side = c["side"].as_str().unwrap();
        let cost = order_cost(v, side);
        assert_eq!(
            orjson_dumps(&PyValue::Map(cost.as_dict())).unwrap(),
            c["as_dict"].as_str().unwrap(),
            "{v} {side}"
        );
        assert_eq!(repr(cost.total()), c["total"].as_str().unwrap());
        assert_eq!(repr(cost_pct(v, side)), c["cost_pct"].as_str().unwrap());
    }
    for c in fx["plans"].as_array().unwrap() {
        let orders: Vec<CostLine> = match py_from_json(c["orders"].as_str().unwrap()) {
            PyValue::List(items) => items
                .iter()
                .map(|o| {
                    let PyValue::Map(o) = o else { panic!() };
                    let delta = match o.get("delta") {
                        Some(PyValue::Int(d)) => *d,
                        _ => 0,
                    };
                    let ref_price = match o.get("ref_price") {
                        Some(PyValue::Float(x)) => *x,
                        Some(PyValue::Int(i)) => *i as f64,
                        _ => 0.0,
                    };
                    CostLine { delta, ref_price }
                })
                .collect(),
            _ => panic!(),
        };
        assert_eq!(
            orjson_dumps(&PyValue::Map(plan_cost(&orders))).unwrap(),
            c["plan_cost"].as_str().unwrap()
        );
    }
}

// ---------------------------------------------------------------------------------------------
// the spec, as the Python tests state it
// ---------------------------------------------------------------------------------------------

fn sample() -> Option<Scored> {
    let raw = corpus_file("sample_scan.csv").or_else(|| {
        let scans = fixture("scans.json");
        scan_bytes("sample", &scans)
    })?;
    let cfg = ScoringConfig::default();
    Some(score(&load_scan(&raw, &cfg).unwrap(), &cfg).unwrap())
}

fn closes(sc: &Scored) -> Vec<(String, f64)> {
    (0..sc.len())
        .map(|i| (sc.symbol(i), sc.num("close", i)))
        .collect()
}

fn top(sc: &Scored) -> Vec<String> {
    let mut rows: Vec<usize> = (0..sc.len()).filter(|&i| sc.reject[i].is_empty()).collect();
    rows.sort_by(|a, b| sc.rank[*a].partial_cmp(&sc.rank[*b]).unwrap());
    rows.into_iter().map(|i| sc.symbol(i)).collect()
}

fn holding(sym: &str, qty: i64, px: f64) -> PyDict {
    let mut h = PyDict::new();
    h.insert("symbol", PyValue::Str(sym.to_owned()));
    h.insert("exchange", PyValue::Str("NSE".to_owned()));
    h.insert("quantity", PyValue::Int(qty));
    h.insert("pledged_qty", PyValue::Int(0));
    h.insert("average_price", PyValue::Float(px));
    h.insert("last_price", PyValue::Float(px));
    h
}

fn plan(sc: &Scored, holdings: Vec<PyDict>, cash: f64, live: Vec<(String, f64)>) -> PyDict {
    let inputs = PlanInputs {
        holdings,
        cash,
        live_prices: live,
        plan_id: "abcdef012345".to_owned(),
        ..PlanInputs::default()
    };
    build_plan(sc, &inputs, &BasketConfig::default()).unwrap()
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

fn order_for(p: &PyDict, sym: &str) -> PyDict {
    orders(p)
        .into_iter()
        .find(|o| o.get("symbol") == Some(&PyValue::Str(sym.to_owned())))
        .unwrap()
}

fn int(o: &PyDict, k: &str) -> i64 {
    match o.get(k) {
        Some(PyValue::Int(i)) => *i,
        other => panic!("{k}: {other:?}"),
    }
}

#[test]
fn a_one_share_drift_is_held_by_the_no_trade_band_and_reports_its_intent() {
    let Some(sc) = sample() else { return };
    let sym = top(&sc)[0].clone();
    let px = closes(&sc).into_iter().find(|(s, _)| *s == sym).unwrap().1;
    let first = plan(&sc, vec![], 5_000_000.0, closes(&sc));
    let target = int(&order_for(&first, &sym), "qty_final");
    let qty = target - 1;
    let p = plan(
        &sc,
        vec![holding(&sym, qty, px)],
        5_000_000.0 - qty as f64 * px,
        closes(&sc),
    );
    let o = order_for(&p, &sym);
    assert_eq!(o.get("action"), Some(&PyValue::Str("HOLD".to_owned())));
    assert_eq!(int(&o, "delta"), 0);
    assert!(o.get("skipped_delta").is_some());
    assert_eq!(int(&o, "qty_final"), int(&o, "qty_now"));
    match o.get("note") {
        Some(PyValue::Str(n)) => assert!(n.contains("no-trade band")),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_full_exit_is_never_suppressed_and_new_buys_always_clear() {
    let Some(sc) = sample() else { return };
    let mut live = closes(&sc);
    live.push(("ZZZNOTINSCAN".to_owned(), 50.0));
    let p = plan(
        &sc,
        vec![holding("ZZZNOTINSCAN", 3, 50.0)],
        1_000_000.0,
        live.clone(),
    );
    let z = order_for(&p, "ZZZNOTINSCAN");
    assert_eq!(z.get("action"), Some(&PyValue::Str("EXIT".to_owned())));
    assert_eq!(int(&z, "delta"), -3);
    assert_eq!(int(&z, "qty_final"), 0);
    assert!(z.get("skipped_delta").is_none());
    let fresh = plan(&sc, vec![], 5_000_000.0, live);
    let buys: Vec<PyDict> = orders(&fresh)
        .into_iter()
        .filter(|o| o.get("action") == Some(&PyValue::Str("BUY".to_owned())))
        .collect();
    assert!(!buys.is_empty());
    assert!(
        buys.iter()
            .all(|o| int(o, "delta") > 0 && o.get("skipped_delta").is_none())
    );
}

#[test]
fn the_planner_never_proposes_an_untouchable_and_does_not_size_against_it() {
    let Some(sc) = sample() else { return };
    let mut live = closes(&sc);
    live.push(("SGBDE31III-GB".to_owned(), 15_430.0));
    let with_sgb = plan(
        &sc,
        vec![holding("SGBDE31III-GB", 392, 15_430.0)],
        5_000_000.0,
        live.clone(),
    );
    assert!(
        orders(&with_sgb)
            .iter()
            .all(|o| o.get("symbol") != Some(&PyValue::Str("SGBDE31III-GB".to_owned())))
    );
    assert_eq!(
        with_sgb.get("excluded"),
        Some(&PyValue::List(vec![PyValue::Str(
            "SGBDE31III-GB".to_owned()
        )]))
    );
    let without = plan(&sc, vec![], 5_000_000.0, live);
    assert_eq!(with_sgb.get("capital"), without.get("capital"));
    // The same guard the gateway applies: prefix-aware, not an exact-match set.
    assert!(tradeable("RELIANCE"));
    assert!(!tradeable("SGBDE31III-GB"));
    assert!(!tradeable("SGBDE31III"));
    assert!(!tradeable(" sgbmar29 "));
}

fn pricing_frame() -> Scored {
    let csv = "symbol,series,date,close,marketcap,median_volume_one_year,rsi_one_month,\
volatility_one_year,beta,circuits_three_months,circuits_one_year,\
positive_days_percent_three_months,positive_days_percent_six_months,ma_20,ma_50,ma_100,ma_200,\
away_from_high_one_year,is_nifty_fno,absolute_return_one_month,absolute_return_three_months,\
absolute_return_six_months,absolute_return_nine_months,absolute_return_one_year,\
sharpe_return_one_month,sharpe_return_three_months,sharpe_return_six_months,\
sharpe_return_nine_months,sharpe_return_one_year
AAA,EQ,2026-08-14,100.0,50000,1e12,55,0.3,1,0,0,60,60,95,90,85,80,-2,1,10,20,30,40,50,1,2,3,4,5
BBB,EQ,2026-08-14,200.0,50000,1e12,55,0.3,1,0,0,59,59,190,180,170,160,-2,1,9,19,29,39,49,1,2,3,4,4.9
CCC,EQ,2026-08-14,300.0,50000,1e12,55,0.3,1,0,0,58,58,285,270,255,240,-2,1,8,18,28,38,48,1,2,3,4,4.8
DDD,EQ,2026-08-14,400.0,50000,1e12,55,0.3,1,0,0,57,57,380,360,340,320,-2,1,7,17,27,37,47,1,2,3,4,4.7
REJ,BE,2026-08-14,500.0,50000,1e12,55,0.3,1,0,0,56,56,475,450,425,400,-2,1,6,16,26,36,46,1,2,3,4,4.6
LOW,BE,2026-08-14,50.0,50000,1e12,55,0.3,1,0,0,55,55,60,45,40,35,-2,1,5,15,25,35,45,1,2,3,4,4.5
";
    let cfg = ScoringConfig::default();
    score(&load_scan(csv.as_bytes(), &cfg).unwrap(), &cfg).unwrap()
}

fn live(pairs: &[(&str, f64)]) -> Vec<(String, f64)> {
    pairs.iter().map(|(s, p)| ((*s).to_owned(), *p)).collect()
}

#[test]
fn a_new_buy_is_priced_and_sized_from_live_not_from_the_scan() {
    let sc = pricing_frame();
    let p = plan(
        &sc,
        vec![],
        1_000_000.0,
        live(&[
            ("AAA", 250.0),
            ("BBB", 200.0),
            ("CCC", 300.0),
            ("DDD", 400.0),
        ]),
    );
    assert_eq!(
        order_for(&p, "AAA").get("ref_price"),
        Some(&PyValue::Float(250.0))
    );
    let cheap = plan(
        &sc,
        vec![],
        1_000_000.0,
        live(&[
            ("AAA", 100.0),
            ("BBB", 200.0),
            ("CCC", 300.0),
            ("DDD", 400.0),
        ]),
    );
    let dear = plan(
        &sc,
        vec![],
        1_000_000.0,
        live(&[
            ("AAA", 400.0),
            ("BBB", 200.0),
            ("CCC", 300.0),
            ("DDD", 400.0),
        ]),
    );
    let (qc, qd) = (
        int(&order_for(&cheap, "AAA"), "delta"),
        int(&order_for(&dear, "AAA"), "delta"),
    );
    assert!(qc > qd);
    assert!((qc as f64 / qd as f64 - 4.0).abs() < 0.35);
}

#[test]
fn an_unpriceable_candidate_is_named_not_guessed_and_breadth_still_counts_it() {
    let sc = pricing_frame();
    let full = plan(
        &sc,
        vec![],
        1_000_000.0,
        live(&[
            ("AAA", 100.0),
            ("BBB", 200.0),
            ("CCC", 300.0),
            ("DDD", 400.0),
        ]),
    );
    let short = plan(
        &sc,
        vec![],
        1_000_000.0,
        live(&[("BBB", 200.0), ("CCC", 300.0), ("DDD", 400.0)]),
    );
    assert_eq!(
        short.get("unpriced"),
        Some(&PyValue::List(vec![PyValue::Str("AAA".to_owned())]))
    );
    assert!(
        orders(&short)
            .iter()
            .all(|o| o.get("symbol") != Some(&PyValue::Str("AAA".to_owned())))
    );
    assert_eq!(full.get("unpriced"), Some(&PyValue::List(vec![])));
    assert_eq!(
        full.get("breadth_above_20dma"),
        short.get("breadth_above_20dma")
    );
    assert_eq!(full.get("cash_target_pct"), short.get("cash_target_pct"));
    let zero = plan(
        &sc,
        vec![],
        1_000_000.0,
        live(&[("AAA", 0.0), ("BBB", 200.0), ("CCC", 300.0), ("DDD", 400.0)]),
    );
    assert_eq!(
        zero.get("unpriced"),
        Some(&PyValue::List(vec![PyValue::Str("AAA".to_owned())]))
    );
}

#[test]
fn cash_bands_hold_band_and_month_arithmetic() {
    let cfg = BasketConfig::default();
    assert_eq!(cash_pct_for(70.0, &cfg), 5.0);
    assert_eq!(cash_pct_for(65.0, &cfg), 5.0);
    assert_eq!(cash_pct_for(50.0, &cfg), 15.0);
    assert_eq!(cash_pct_for(10.0, &cfg), 35.0);
    assert_eq!(cash_pct_for(f64::NAN, &cfg), 35.0);
    let full = BasketConfig {
        fully_invested: true,
        ..BasketConfig::default()
    };
    assert_eq!(cash_pct_for(10.0, &full), 0.0);
    // rank == top_n + buffer holds; one worse exits.
    assert!(inside_hold_band(20, 15, 5));
    assert!(!inside_hold_band(21, 15, 5));
    let d = |y, m, dd| chrono::NaiveDate::from_ymd_opt(y, m, dd).unwrap();
    assert_eq!(subtract_months(d(2026, 3, 31), 1), Some(d(2026, 2, 28)));
    assert_eq!(subtract_months(d(2026, 8, 14), 18), Some(d(2025, 2, 14)));
    assert_eq!(subtract_months(d(2024, 3, 31), 1), Some(d(2024, 2, 29)));
}

#[test]
fn every_buy_carries_a_stop_inside_the_eight_to_twelve_percent_band() {
    let Some(sc) = sample() else { return };
    let p = plan(&sc, vec![], 5_000_000.0, closes(&sc));
    for o in orders(&p) {
        if int(&o, "delta") <= 0 {
            continue;
        }
        let (Some(PyValue::Float(px)), Some(PyValue::Float(stop))) =
            (o.get("ref_price"), o.get("stop"))
        else {
            panic!("a buy without a stop: {o:?}")
        };
        let pct = 1.0 - stop / px;
        assert!((0.0795..=0.1205).contains(&pct), "{pct}");
    }
}

#[test]
fn csv_reading_handles_quotes_bom_and_short_rows() {
    let raw = "\u{feff}a,b,c\r\n\"x, \"\"y\"\"\",1,\r\nz,2,3.5\r\n\r\n";
    let fr = read_csv(raw.as_bytes()).unwrap();
    assert_eq!(fr.names, vec!["a", "b", "c"]);
    assert_eq!(
        fr.col("a"),
        Some(&Column::Str(vec![
            Some("x, \"y\"".to_owned()),
            Some("z".to_owned())
        ]))
    );
    assert_eq!(fr.col("b"), Some(&Column::Int(vec![1, 2])));
    match fr.col("c") {
        Some(Column::Float(v)) => assert!(v[0].is_nan() && v[1] == 3.5),
        other => panic!("{other:?}"),
    }
}
