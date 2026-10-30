//! `tests/test_backtest_view.py`, ported: the spec tests on the geometry, plus a byte-level
//! comparison against `goldens.json` — Python's own `backtest_view` output for the test
//! module's datasets (SAMPLE, CURVES, EXP, TL, SP, the robustness grids) and a few more,
//! generated once from the worktree's Python and never edited.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use super::*;

fn goldens() -> PyValue {
    PyValue::from_json(include_bytes!("goldens.json")).unwrap()
}

fn map(v: &PyValue) -> &PyDict {
    match v {
        PyValue::Map(m) => m,
        _ => panic!("not a dict"),
    }
}

fn list(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(xs) => xs,
        _ => &[],
    }
}

fn at<'a>(v: &'a PyValue, k: &str) -> &'a PyValue {
    map(v).get(k).unwrap_or(&PyValue::None)
}

fn f(v: &PyValue) -> f64 {
    num(v).map_or(f64::NAN, Num::f)
}

fn dumps(v: &PyValue) -> String {
    crate::wire::py_json_dumps(v).unwrap()
}

fn sample() -> PyValue {
    at(at(&goldens(), "cases"), "sample").clone()
}

#[test]
fn every_golden_case_matches_python_exactly() {
    let g = goldens();
    let cases = map(at(&g, "cases"));
    assert!(cases.len() >= 10);
    for (name, case) in cases.iter() {
        let input = at(case, "input");
        let checks: [(&str, PyValue); 5] = [
            ("build", build(Some(input))),
            ("curves", curves(input)),
            ("robustness", robustness(input, "CAGR")),
            ("timelines", timelines(input)),
            ("subperiods", subperiods(input)),
        ];
        for (what, got) in checks {
            assert_eq!(dumps(&got), dumps(at(case, what)), "{name}: {what}");
        }
    }
}

#[test]
fn every_golden_axis_matches_python_exactly() {
    let g = goldens();
    for (key, want) in map(at(&g, "axes")).iter() {
        let (lo, hi) = key.split_once('|').unwrap();
        let parse = |s: &str| {
            s.parse::<i64>()
                .map(Num::I)
                .unwrap_or_else(|_| Num::F(s.parse::<f64>().unwrap()))
        };
        let got = axis(parse(lo), parse(hi), 5).py();
        assert_eq!(dumps(&got), dumps(want), "{key}");
    }
}

#[test]
fn an_axis_covers_its_data() {
    for (lo, hi) in [(0.0, 70.45), (0.0, 3.0), (0.0, 0.4), (-5.0, 25.0)] {
        #[allow(clippy::cast_possible_truncation)]
        let lo_n = if lo == 0.0 {
            Num::I(0)
        } else {
            Num::I(lo as i64)
        };
        let a = axis(lo_n, Num::F(hi), 5);
        assert!(a.min.f() <= lo && a.max.f() >= hi);
        assert_eq!(a.ticks[0].f(), a.min.f());
        assert_eq!(a.ticks[a.ticks.len() - 1].f(), a.max.f());
    }
}

#[test]
fn axis_ticks_are_evenly_spaced() {
    let t = axis(Num::I(0), Num::F(70.45), 5).ticks;
    let gaps: Vec<f64> = t
        .windows(2)
        .map(|w| py_round(w[1].f() - w[0].f(), 6))
        .collect();
    assert!(gaps.iter().all(|g| *g == gaps[0]));
}

#[test]
fn a_degenerate_range_does_not_divide_by_zero() {
    let a = axis(Num::I(5), Num::I(5), 5);
    assert!(a.max.f() > a.min.f());
}

#[test]
fn ablations_are_not_candidates() {
    assert!(!is_candidate("abl_breadth"));
    for (k, fam) in FAMILY_OF {
        assert!(is_candidate(k));
        assert!(FAMILIES.iter().any(|(f, _, _)| *f == fam));
    }
}

fn marks(v: &PyValue) -> Vec<PyValue> {
    list(at(v, "marks")).to_vec()
}

#[test]
fn the_best_ratio_is_marked_and_tiered_is_proxy() {
    let s = sample();
    let sc = risk_return_scatter(map(at(at(&s, "input"), "variants")));
    let ms = marks(&sc);
    let best: Vec<String> = ms
        .iter()
        .filter(|m| at(m, "best") == &PyValue::Bool(true))
        .map(|m| at(m, "key").py_str())
        .collect();
    assert_eq!(best, vec!["B_raw"]);
    for m in &ms {
        if at(m, "family").py_str() == "tiered" {
            assert_eq!(at(m, "proxy"), &PyValue::Bool(true));
        }
    }
    assert!(!ms.iter().any(|m| at(m, "key").py_str().starts_with("abl_")));
}

fn label_rect(m: &PyValue) -> (f64, f64, f64, f64) {
    let (lx, ly) = (f(at(m, "lx")), f(at(m, "ly")));
    let x0 = if at(m, "anchor").py_str() == "start" {
        lx
    } else {
        lx - LABEL_W
    };
    (x0, ly - LABEL_ABOVE, x0 + LABEL_W, ly + LABEL_BELOW)
}

#[test]
fn no_two_labels_overlap_and_none_sits_on_a_mark() {
    let s = sample();
    let sc = risk_return_scatter(map(at(at(&s, "input"), "variants")));
    let ms = marks(&sc);
    for (i, a) in ms.iter().enumerate() {
        let ra = label_rect(a);
        assert!(ra.0 >= 4.0 && ra.2 <= 716.0, "label inside the canvas");
        for (j, b) in ms.iter().enumerate() {
            if i == j {
                continue;
            }
            assert!(!overlap(ra, label_rect(b)), "labels overlap");
            let (bx, by) = (f(at(b, "x")), f(at(b, "y")));
            assert!(!overlap(
                ra,
                (bx - MARK_R, by - MARK_R, bx + MARK_R, by + MARK_R)
            ));
        }
    }
}

#[test]
fn bars_are_sorted_descending_and_never_invisible() {
    let s = sample();
    let b = ranked_bars(map(at(at(&s, "input"), "variants")), "sharpe", None, 148);
    let vals: Vec<f64> = list(at(&b, "bars"))
        .iter()
        .map(|x| f(at(x, "value")))
        .collect();
    assert!(vals.windows(2).all(|w| w[0] >= w[1]));
    assert!(list(at(&b, "bars")).iter().all(|x| f(at(x, "w")) >= 1.5));
}

#[test]
fn ablation_bars_use_the_requested_keys() {
    let s = sample();
    let keys = vec!["abl_breadth".to_owned()];
    let b = ranked_bars(
        map(at(at(&s, "input"), "variants")),
        "CAGR",
        Some(&keys),
        196,
    );
    let got: Vec<String> = list(at(&b, "bars"))
        .iter()
        .map(|x| at(x, "key").py_str())
        .collect();
    assert_eq!(got, keys);
}

#[test]
fn build_returns_nothing_without_results() {
    assert_eq!(build(None), PyValue::None);
    assert_eq!(build(Some(&PyValue::Map(PyDict::new()))), PyValue::None);
}

#[test]
fn build_carries_the_breadth_caveat() {
    let s = sample();
    let b = build(Some(at(&s, "input")));
    assert_eq!(
        at(&b, "breadth_caveat").py_str(),
        "INSUFFICIENT for this window"
    );
}

#[test]
fn a_gap_in_a_series_breaks_the_path_instead_of_bridging_it() {
    let data = PyValue::from_json(
        br#"{"curves": {"dates": ["2020-01-01", "2020-01-08", "2020-01-15"],
             "series": {"A": {"equity": [100.0, null, 120.0], "drawdown": [0.0, null, 0.0]}}}}"#,
    )
    .unwrap();
    let c = curves(&data);
    let eq = at(&list(at(&c, "lines"))[0], "equity").py_str();
    assert_eq!(eq.matches('M').count(), 2);
}

#[test]
fn no_sections_without_their_data() {
    let empty = PyValue::Map(PyDict::new());
    assert_eq!(curves(&empty), PyValue::None);
    assert_eq!(robustness(&empty, "CAGR"), PyValue::None);
    assert_eq!(timelines(&empty), PyValue::None);
    assert_eq!(subperiods(&empty), PyValue::None);
}

#[test]
fn the_ramp_is_the_sequential_one_with_one_ink() {
    assert_eq!(ramp_color(0.0, 0.0, 1.0), RAMP[0]);
    assert_eq!(ramp_color(1.0, 0.0, 1.0), RAMP[4]);
    assert_eq!(ramp_color(5.0, 5.0, 5.0), RAMP[2]);
}

/// Differential fuzz against Python: `BV_FUZZ=<cases.json>` (same shape as `goldens.json`'s
/// `cases`, generated by running Python's `backtest_view` over random stored results).
/// Ignored by default because the case file is produced outside the repo.
#[test]
#[ignore = "needs BV_FUZZ, a case file generated from Python"]
fn fuzz_cases_match_python_exactly() {
    let path = std::env::var("BV_FUZZ").unwrap();
    let g = PyValue::from_json(&std::fs::read(path).unwrap()).unwrap();
    let mut bad = Vec::new();
    for (name, case) in map(at(&g, "cases")).iter() {
        let input = at(case, "input");
        let checks: [(&str, PyValue); 5] = [
            ("build", build(Some(input))),
            ("curves", curves(input)),
            ("robustness", robustness(input, "CAGR")),
            ("timelines", timelines(input)),
            ("subperiods", subperiods(input)),
        ];
        for (what, got) in checks {
            let (got, want) = (dumps(&got), dumps(at(case, what)));
            if got != want {
                let at_ = got
                    .bytes()
                    .zip(want.bytes())
                    .position(|(a, b)| a != b)
                    .unwrap_or(got.len().min(want.len()));
                let lo = at_.saturating_sub(80);
                bad.push(format!(
                    "{name}:{what}\n  rust: {}\n  py:   {}",
                    got.get(lo..(at_ + 80).min(got.len())).unwrap_or(""),
                    want.get(lo..(at_ + 80).min(want.len())).unwrap_or("")
                ));
            }
        }
    }
    assert!(bad.is_empty(), "{} diffs:\n{}", bad.len(), bad.join("\n"));
}

/// Found by the differential fuzz: when no 1-2-5 tick falls inside the equity range,
/// `_log_ticks` returns `[lo, hi]` exactly as `min`/`max` returned them, so an int stays an
/// int (`158`, not `158.0`). Expected values printed by Python's `backtest_view.curves`.
#[test]
fn the_empty_log_range_fallback_keeps_the_stored_number_types() {
    let input = PyValue::from_json(
        br#"{"curves": {"dates": ["2010-01-01", "2010-01-08"], "series": {"A": {"equity": [128.0, 158], "drawdown": [0, -1.5]}}}}"#,
    )
    .unwrap();
    assert_eq!(
        dumps(at(&curves(&input), "eq_ticks")),
        r#"[{"v": 128.0, "px": 274.0, "label": "128"}, {"v": 158, "px": 14.0, "label": "158"}]"#
    );
}

/// Differential render against Python's Jinja: `BV_RENDER=<cases.json>` holds, per stored
/// results dict, the `regime_backtest.html` Python rendered (asset_v and options_enabled as
/// the file states, `fno_badge` unregistered on both sides). Ignored by default because the
/// case file is generated outside the repo.
#[test]
#[ignore = "needs BV_RENDER, a case file rendered by Python"]
fn the_page_with_results_renders_byte_equal_to_python() {
    let path = std::env::var("BV_RENDER").unwrap();
    let g = PyValue::from_json(&std::fs::read(path).unwrap()).unwrap();
    let asset_v = at(&g, "asset_v").py_str();
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../kite-momentum-rebalancer/app/templates");
    let t = crate::templates::Templates::new(&dir, &asset_v, false);
    let mut bad = Vec::new();
    for (name, case) in map(at(&g, "cases")).iter() {
        let input = at(case, "input");
        let ctx = PyValue::Map(crate::wire::dict([
            ("b", input.clone()),
            (
                "path",
                PyValue::Str(super::super::REGIME_BACKTEST_PATH.to_owned()),
            ),
            ("charts", build(Some(input))),
            (
                "request",
                crate::lanes::analytics::web::request_value("/regime/backtest"),
            ),
        ]));
        let got = t
            .render("regime_backtest.html", crate::templates::py_value(&ctx))
            .unwrap_or_else(|e| format!("<render error {e}>"));
        let want = at(case, "html").py_str();
        if got != want {
            let i = got
                .bytes()
                .zip(want.bytes())
                .position(|(a, b)| a != b)
                .unwrap_or(got.len().min(want.len()));
            let lo = i.saturating_sub(120);
            bad.push(format!(
                "{name}\n  rust: {:?}\n  py:   {:?}",
                got.get(lo..(i + 120).min(got.len())).unwrap_or(""),
                want.get(lo..(i + 120).min(want.len())).unwrap_or("")
            ));
        }
    }
    assert!(
        bad.is_empty(),
        "{} differ:\n{}",
        bad.len(),
        bad.iter().take(6).cloned().collect::<Vec<_>>().join("\n")
    );
}
