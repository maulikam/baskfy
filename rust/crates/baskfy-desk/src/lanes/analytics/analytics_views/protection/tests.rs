//! `tests/test_protection.py`, ported (the spec, not the code's current behaviour).

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use super::*;
use crate::lanes::exec_core::stops::{STOP_MAX, STOP_MIN};

fn band() -> StopConfig {
    StopConfig::default()
}

fn hold(symbol: &str, qty: i64, px: f64) -> PyValue {
    PyValue::Map(dict([
        ("symbol", PyValue::Str(symbol.to_owned())),
        ("quantity", PyValue::Int(qty)),
        ("last_price", PyValue::Float(px)),
        ("pledged_qty", PyValue::Int(0)),
    ]))
}

fn gtt_s(symbol: &str, trigger: f64, qty: i64, status: &str) -> PyValue {
    PyValue::Map(dict([
        ("status", PyValue::Str(status.to_owned())),
        (
            "condition",
            PyValue::Map(dict([
                ("tradingsymbol", PyValue::Str(symbol.to_owned())),
                (
                    "trigger_values",
                    PyValue::List(vec![PyValue::Float(trigger)]),
                ),
            ])),
        ),
        (
            "orders",
            PyValue::List(vec![PyValue::Map(dict([("quantity", PyValue::Int(qty))]))]),
        ),
    ]))
}

fn gtt(symbol: &str, trigger: f64, qty: i64) -> PyValue {
    gtt_s(symbol, trigger, qty, "active")
}

fn gtt_for(symbol: &str, qty: i64, trigger: f64, gid: i64) -> PyValue {
    PyValue::Map(dict([
        ("id", PyValue::Int(gid)),
        ("status", PyValue::Str("active".to_owned())),
        (
            "condition",
            PyValue::Map(dict([
                ("tradingsymbol", PyValue::Str(symbol.to_owned())),
                (
                    "trigger_values",
                    PyValue::List(vec![PyValue::Float(trigger)]),
                ),
            ])),
        ),
        (
            "orders",
            PyValue::List(vec![PyValue::Map(dict([
                ("tradingsymbol", PyValue::Str(symbol.to_owned())),
                ("quantity", PyValue::Int(qty)),
                ("transaction_type", PyValue::Str("SELL".to_owned())),
            ]))]),
        ),
    ]))
}

fn kinds(r: &Review) -> Vec<&'static str> {
    r.findings.iter().map(|f| f.kind).collect()
}

fn plan(h: &[PyValue], g: &[PyValue]) -> PyDict {
    build_stop_plan(h, g, &[], "p", band())
}

fn rows(p: &PyDict) -> Vec<PyDict> {
    match p.get("rows") {
        Some(PyValue::List(xs)) => xs
            .iter()
            .filter_map(|x| match x {
                PyValue::Map(m) => Some(m.clone()),
                _ => None,
            })
            .collect(),
        _ => vec![],
    }
}

fn int(p: &PyDict, k: &str) -> i64 {
    match p.get(k) {
        Some(PyValue::Int(i)) => *i,
        _ => i64::MIN,
    }
}

fn float(p: &PyDict, k: &str) -> f64 {
    match p.get(k) {
        Some(PyValue::Float(x)) => *x,
        _ => f64::NAN,
    }
}

fn text(p: &PyDict, k: &str) -> String {
    p.get(k).map(PyValue::py_str).unwrap_or_default()
}

#[test]
fn a_position_with_no_trigger_is_reported_missing() {
    let r = review(&[hold("AAA", 100, 100.0)], &[], band());
    assert_eq!(kinds(&r), vec![MISSING]);
    assert!(!r.healthy);
}

#[test]
fn a_covered_position_is_healthy() {
    let r = review(&[hold("AAA", 100, 100.0)], &[gtt("AAA", 90.0, 100)], band());
    assert!(r.findings.is_empty() && r.healthy);
    assert_eq!(r.coverage_pct, Some(100.0));
}

#[test]
fn a_stop_for_fewer_shares_than_held_is_partial() {
    let r = review(&[hold("AAA", 200, 100.0)], &[gtt("AAA", 90.0, 100)], band());
    let f = r.findings.iter().find(|f| f.kind == PARTIAL);
    assert_eq!(f.and_then(|f| f.covered), Some(100));
    assert_eq!(f.map(|f| f.value), Some(10_000));
    assert!(!r.healthy);
}

#[test]
fn an_inactive_trigger_does_not_count_as_protection() {
    for status in ["cancelled", "expired", "rejected", "triggered", "deleted"] {
        let r = review(
            &[hold("AAA", 100, 100.0)],
            &[gtt_s("AAA", 90.0, 100, status)],
            band(),
        );
        assert_eq!(r.findings[0].kind, MISSING, "{status}");
    }
}

#[test]
fn a_trigger_with_no_holding_is_an_orphan() {
    let r = review(&[], &[gtt("ZZZ", 90.0, 100)], band());
    assert_eq!(kinds(&r), vec![ORPHAN]);
}

#[test]
fn a_stop_further_than_the_band_is_flagged() {
    let r = review(
        &[hold("AAA", 100, 100.0)],
        &[gtt("AAA", 100.0 * (1.0 - STOP_MAX - 0.05), 100)],
        band(),
    );
    assert!(kinds(&r).contains(&TOO_FAR));
}

#[test]
fn a_stop_tighter_than_the_band_is_flagged() {
    let r = review(
        &[hold("AAA", 100, 100.0)],
        &[gtt("AAA", 100.0 * (1.0 - STOP_MIN + 0.02), 100)],
        band(),
    );
    assert!(kinds(&r).contains(&TOO_CLOSE));
}

#[test]
fn a_stop_inside_the_band_is_not_flagged() {
    let mid = f64::midpoint(STOP_MIN, STOP_MAX);
    let r = review(
        &[hold("AAA", 100, 100.0)],
        &[gtt("AAA", 100.0 * (1.0 - mid), 100)],
        band(),
    );
    assert!(r.findings.is_empty());
}

#[test]
fn the_binding_stop_is_the_highest_one() {
    let r = review(
        &[hold("AAA", 100, 100.0)],
        &[gtt("AAA", 50.0, 50), gtt("AAA", 92.0, 50)],
        band(),
    );
    assert!(!kinds(&r).contains(&TOO_FAR));
}

#[test]
fn an_untouchable_holding_is_not_called_unprotected() {
    let r = review(
        &[hold("SGBDE31III-GB", 392, 15306.0), hold("AAA", 100, 100.0)],
        &[],
        band(),
    );
    assert!(r.excluded.contains(&"SGBDE31III-GB".to_owned()));
    let syms: Vec<&str> = r.findings.iter().map(|f| f.symbol.as_str()).collect();
    assert_eq!(syms, vec!["AAA"]);
}

#[test]
fn untouchable_value_is_left_out_of_the_totals() {
    let r = review(&[hold("SGBDE31III-GB", 392, 15306.0)], &[], band());
    assert_eq!(r.tradeable_value, 0);
    assert!(r.findings.is_empty());
}

#[test]
fn unprotected_never_exceeds_tradeable() {
    let holds: Vec<PyValue> = (0..17)
        .map(|i| hold(&format!("S{i}"), 7, 1_449.93))
        .collect();
    let r = review(&holds, &[], band());
    assert!(r.unprotected_value <= r.tradeable_value);
}

#[test]
fn coverage_is_never_negative_zero() {
    let r = review(&[hold("AAA", 7, 1_449.93)], &[], band());
    assert_eq!(r.coverage_pct, Some(0.0));
    assert!(r.coverage_pct.is_some_and(f64::is_sign_positive));
}

#[test]
fn coverage_with_nothing_held_is_none_not_zero() {
    assert_eq!(review(&[], &[], band()).coverage_pct, None);
}

#[test]
fn findings_are_ordered_worst_first_then_by_value() {
    let r = review(
        &[hold("SMALL", 1, 100.0), hold("BIG", 100, 100.0)],
        &[],
        band(),
    );
    let syms: Vec<&str> = r.findings.iter().map(|f| f.symbol.as_str()).collect();
    assert_eq!(syms, vec!["BIG", "SMALL"]);
}

#[test]
fn the_module_never_places_or_cancels_a_stop() {
    let src = include_str!("../protection.rs");
    let forbidden = [
        ["place", "_gtt"].concat(),
        ["delete", "_gtt"].concat(),
        ["modify", "_gtt"].concat(),
        ["place", "_order"].concat(),
    ];
    for f in forbidden {
        assert!(!src.contains(&f), "{f}");
    }
}

#[test]
fn the_summary_line_states_the_money_at_risk() {
    let r = review(&[hold("AAA", 100, 1000.0)], &[], band());
    assert!(summary_line(&r).contains("100,000"));
}

#[test]
fn the_summary_line_is_positive_when_everything_is_covered() {
    let r = review(&[hold("AAA", 100, 100.0)], &[gtt("AAA", 90.0, 100)], band());
    assert!(summary_line(&r).contains("carry a stop"));
}

#[test]
fn a_plan_proposes_one_stop_per_uncovered_position() {
    let p = plan(&[hold("AAA", 100, 100.0), hold("BBB", 50, 200.0)], &[]);
    assert_eq!(int(&p, "count"), 2);
    let mut syms: Vec<String> = rows(&p).iter().map(|r| text(r, "symbol")).collect();
    syms.sort();
    assert_eq!(syms, vec!["AAA", "BBB"]);
}

#[test]
fn a_covered_position_is_not_proposed_for() {
    let p = plan(&[hold("AAA", 100, 100.0)], &[gtt("AAA", 90.0, 100)]);
    assert_eq!(int(&p, "count"), 0);
}

#[test]
fn a_partial_position_is_proposed_for_the_uncovered_shares_only() {
    let p = plan(&[hold("AAA", 200, 100.0)], &[gtt("AAA", 90.0, 100)]);
    let q: Vec<i64> = rows(&p).iter().map(|r| int(r, "qty")).collect();
    assert_eq!(q, vec![100]);
}

#[test]
fn a_stop_outside_the_band_is_left_alone() {
    let p = plan(&[hold("AAA", 100, 100.0)], &[gtt("AAA", 10.0, 100)]);
    assert_eq!(int(&p, "count"), 0);
}

#[test]
fn an_untouchable_position_is_never_proposed_for() {
    let p = plan(&[hold("SGBDE31III-GB", 392, 15306.0)], &[]);
    assert_eq!(int(&p, "count"), 0);
}

#[test]
fn the_trigger_sits_inside_the_configured_band() {
    let p = plan(&[hold("AAA", 100, 1000.0)], &[]);
    let drop = float(&rows(&p)[0], "drop_pct") / 100.0;
    assert!((STOP_MIN - 1e-9..=STOP_MAX + 1e-9).contains(&drop));
}

#[test]
fn a_higher_volatility_gives_a_wider_stop() {
    let h = [hold("AAA", 100, 1000.0)];
    let tight = rows(&build_stop_plan(
        &h,
        &[],
        &[("AAA".to_owned(), 0.10)],
        "p",
        band(),
    ))[0]
        .clone();
    let wide = rows(&build_stop_plan(
        &h,
        &[],
        &[("AAA".to_owned(), 0.90)],
        "p",
        band(),
    ))[0]
        .clone();
    assert!(float(&wide, "drop_pct") > float(&tight, "drop_pct"));
    assert_eq!(text(&tight, "vol_source"), "scan");
}

#[test]
fn a_missing_volatility_is_labelled_not_hidden() {
    let p = plan(&[hold("AAA", 100, 100.0)], &[]);
    assert_eq!(text(&rows(&p)[0], "vol_source"), "default");
    assert_eq!(
        p.get("using_default_vol"),
        Some(&PyValue::List(vec![PyValue::Str("AAA".to_owned())]))
    );
}

#[test]
fn at_risk_is_the_distance_from_price_to_trigger() {
    let r = rows(&plan(&[hold("AAA", 100, 1000.0)], &[]))[0].clone();
    #[allow(clippy::cast_precision_loss)]
    let at_risk = int(&r, "at_risk") as f64;
    assert!((at_risk - (1000.0 - float(&r, "trigger")) * 100.0).abs() <= 1.0);
}

#[test]
fn rows_are_ordered_by_value_at_stake() {
    let p = plan(&[hold("SMALL", 1, 100.0), hold("BIG", 500, 100.0)], &[]);
    let syms: Vec<String> = rows(&p).iter().map(|r| text(r, "symbol")).collect();
    assert_eq!(syms, vec!["BIG", "SMALL"]);
}

#[test]
fn every_plan_carries_the_id_it_was_given() {
    // Python draws `uuid4().hex[:12]` per plan; here the caller's id source supplies it, so
    // two plans get two ids exactly when the source does.
    let a = build_stop_plan(&[hold("AAA", 100, 100.0)], &[], &[], "a1", band());
    let b = build_stop_plan(&[hold("AAA", 100, 100.0)], &[], &[], "b2", band());
    assert_ne!(text(&a, "plan_id"), text(&b, "plan_id"));
}

#[test]
fn a_zero_price_position_is_skipped_rather_than_priced_at_zero() {
    assert_eq!(int(&plan(&[hold("AAA", 100, 0.0)], &[]), "count"), 0);
}

#[test]
fn a_stop_covering_more_than_is_held_is_flagged() {
    let r = review(
        &[hold("SONACOMS", 342, 800.0)],
        &[gtt_for("SONACOMS", 861, 90.0, 1)],
        band(),
    );
    let ex = r.findings.iter().find(|f| f.kind == EXCESS);
    assert_eq!(ex.and_then(|f| f.covered), Some(861));
    assert_eq!(ex.map(|f| f.qty), Some(342));
    assert!(ex.is_some_and(|f| f.detail.contains("519 you do not own")));
}

#[test]
fn excess_outranks_everything_but_a_missing_stop() {
    assert!(severity(MISSING) < severity(EXCESS) && severity(EXCESS) < severity(PARTIAL));
}

#[test]
fn an_exact_cover_is_not_flagged() {
    let r = review(
        &[hold("TITAN", 100, 3000.0)],
        &[gtt_for("TITAN", 100, 2700.0, 1)],
        band(),
    );
    assert!(!kinds(&r).contains(&EXCESS));
}

#[test]
fn an_excess_position_is_cancelled_then_re_armed_for_what_is_held() {
    let p = build_stop_plan(
        &[hold("SONACOMS", 342, 800.0)],
        &[
            gtt_for("SONACOMS", 861, 90.0, 111),
            gtt_for("SONACOMS", 400, 90.0, 222),
        ],
        &[],
        "p",
        band(),
    );
    let ids: Vec<i64> = match p.get("cancels") {
        Some(PyValue::List(xs)) => xs
            .iter()
            .filter_map(|c| match c {
                PyValue::Map(m) => Some(int(m, "gtt_id")),
                _ => None,
            })
            .collect(),
        _ => vec![],
    };
    assert_eq!(ids, vec![111, 222]);
    let row = rows(&p)
        .into_iter()
        .find(|r| text(r, "symbol") == "SONACOMS");
    assert_eq!(row.map(|r| int(&r, "qty")), Some(342));
}

#[test]
fn an_orphan_trigger_is_cancelled() {
    let p = build_stop_plan(&[], &[gtt_for("PARAS", 438, 90.0, 999)], &[], "p", band());
    assert_eq!(int(&p, "cancel_count"), 1);
    assert!(rows(&p).is_empty());
}

#[test]
fn a_partial_cover_still_only_tops_up_the_uncovered_shares() {
    let p = plan(
        &[hold("TITAN", 100, 3000.0)],
        &[gtt_for("TITAN", 40, 2700.0, 1)],
    );
    assert_eq!(int(&rows(&p)[0], "qty"), 60);
    assert_eq!(p.get("cancels"), Some(&PyValue::List(vec![])));
}

#[test]
fn over_coverage_makes_the_book_unhealthy() {
    let r = review(
        &[hold("SONACOMS", 342, 800.0)],
        &[gtt_for("SONACOMS", 861, 730.0, 1)],
        band(),
    );
    assert!(!r.healthy);
}

#[test]
fn an_orphan_trigger_makes_the_book_unhealthy() {
    assert!(!review(&[], &[gtt_for("PARAS", 438, 90.0, 1)], band()).healthy);
}

#[test]
fn a_correctly_covered_book_is_healthy() {
    let r = review(
        &[hold("TITAN", 100, 3000.0)],
        &[gtt_for("TITAN", 100, 2700.0, 1)],
        band(),
    );
    assert!(r.healthy);
}

#[test]
fn a_stop_merely_outside_the_band_is_still_healthy() {
    let r = review(
        &[hold("CUPID", 100, 283.0)],
        &[gtt_for("CUPID", 100, 245.0, 1)],
        band(),
    );
    assert!(kinds(&r).contains(&TOO_FAR));
    assert!(r.healthy);
}

#[test]
fn finding_dicts_keep_pythons_key_order() {
    let r = review(
        &[hold("SONACOMS", 342, 800.0)],
        &[gtt_for("SONACOMS", 861, 730.0, 7)],
        band(),
    );
    let d = r.findings[0].to_py();
    let keys: Vec<&str> = d.iter().map(|(k, _)| k).collect();
    assert_eq!(
        keys,
        vec![
            "kind", "symbol", "qty", "covered", "gtt_ids", "value", "detail"
        ]
    );
}

/// Differential fuzz against Python: `PROT_FUZZ=<cases.json>`, each case holding the
/// holdings, GTTs and vols fed to Python's `review`/`summary_line`/`build_stop_plan` and
/// what Python returned. Ignored by default because the case file is generated outside the
/// repo.
#[test]
#[ignore = "needs PROT_FUZZ, a case file generated from Python"]
fn fuzz_cases_match_python_exactly() {
    let path = std::env::var("PROT_FUZZ").unwrap();
    let g = PyValue::from_json(&std::fs::read(path).unwrap()).unwrap();
    let PyValue::Map(top) = &g else { panic!() };
    let Some(PyValue::List(cases)) = top.get("cases") else {
        panic!()
    };
    let dumps = |v: &PyValue| crate::wire::py_json_dumps(v).unwrap();
    let mut bad = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let PyValue::Map(c) = case else { panic!() };
        let list = |k: &str| match c.get(k) {
            Some(PyValue::List(xs)) => xs.clone(),
            _ => vec![],
        };
        let (holds, gtts) = (list("holdings"), list("gtts"));
        let vols: Vec<(String, f64)> = match c.get("vols") {
            Some(PyValue::Map(m)) => m
                .iter()
                .map(|(k, v)| {
                    let x = match v {
                        PyValue::Int(i) => *i as f64,
                        PyValue::Float(f) => *f,
                        _ => panic!(),
                    };
                    (k.to_owned(), x)
                })
                .collect(),
            _ => vec![],
        };
        let rev = review(&holds, &gtts, band());
        let checks = [
            ("review", dumps(&PyValue::Map(rev.to_py()))),
            ("summary", dumps(&PyValue::Str(summary_line(&rev)))),
            (
                "plan",
                dumps(&PyValue::Map(build_stop_plan(
                    &holds,
                    &gtts,
                    &vols,
                    "fixedplan01",
                    band(),
                ))),
            ),
        ];
        for (what, got) in checks {
            let want = dumps(c.get(what).unwrap());
            if got != want {
                let at = got
                    .bytes()
                    .zip(want.bytes())
                    .position(|(a, b)| a != b)
                    .unwrap_or(got.len().min(want.len()));
                let lo = at.saturating_sub(100);
                bad.push(format!(
                    "case {i} {what}\n  rust: {}\n  py:   {}",
                    got.get(lo..(at + 100).min(got.len())).unwrap_or(""),
                    want.get(lo..(at + 100).min(want.len())).unwrap_or("")
                ));
            }
        }
    }
    assert!(
        bad.is_empty(),
        "{} diffs:\n{}",
        bad.len(),
        bad.iter().take(12).cloned().collect::<Vec<_>>().join("\n")
    );
}
