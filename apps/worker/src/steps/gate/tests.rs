//! The step-level assertions of `services/worker/tests/test_quality_gate.py` and
//! `test_quality_universe_sources.py` that hold without a database, ported as the **spec**:
//! each verdict is computed from the numbers its check reads. The database half of the gate is
//! graded by `tools/parity/nightly_diff.py --step data_quality_gate` (fresh, rerun, gate-refuse).
#![allow(clippy::unwrap_used, clippy::float_cmp, clippy::panic)]

use std::collections::HashMap;

use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use super::quality::{
    CHECK_NAMES, CheckResult, CheckStatus, NO_MEMBERSHIP_SOURCE, NOMINAL_SIZES, UNIVERSES,
    bar_count_verdict, factor_rows_verdict, index_level_verdict, record_report,
    return_distribution_verdict, universe_sizes_verdict,
};
use super::{GateReport, report_of};
use crate::pyjson;
use crate::steps::StepOutcome;

fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 17).unwrap()
}

fn observed<'a>(r: &'a CheckResult, key: &str) -> &'a OrderedJson {
    r.observed.get(key).unwrap()
}

// --- the gate itself ---------------------------------------------------------------------------

#[test]
fn all_eight_assertions_are_implemented_in_document_order() {
    assert_eq!(CHECK_NAMES.len(), 8);
    assert_eq!(CHECK_NAMES[0], "bar_count_against_baseline");
    assert_eq!(CHECK_NAMES[7], "return_distribution");
}

fn mixed_results() -> Vec<CheckResult> {
    vec![
        bar_count_verdict(100, &[100, 100], 0.9),    // passed
        bar_count_verdict(1, &[], 0.9),              // skipped
        factor_rows_verdict(0, 1),                   // failed
        index_level_verdict(24500.35, 0),            // skipped
        return_distribution_verdict(None, &[], 3.0), // skipped
    ]
}

#[test]
fn a_skipped_assertion_does_not_block_publishing() {
    let results = mixed_results();
    let report = report_of(day(), &results);
    // 1. Skips really happen here (or the rule below is vacuous).
    assert_eq!(report.skipped().len(), 3);
    // 2. A skip is never a failure.
    assert!(report.skipped().iter().all(|r| r.status == "skipped"));
    assert!(
        results
            .iter()
            .filter(|r| r.status == CheckStatus::Skipped)
            .all(|r| !r.blocks_publish())
    );
    assert!(!report.passed(), "the factor-row failure blocks");
    // 3. Passes and skips alone publish.
    let skips_only: Vec<CheckResult> = results
        .into_iter()
        .filter(|r| r.status != CheckStatus::Failed)
        .collect();
    let skips_only = report_of(day(), &skips_only);
    assert_eq!(skips_only.skipped().len(), 3);
    assert!(
        skips_only.passed(),
        "skips alone must never stop a session publishing"
    );
}

#[test]
fn the_step_payload_records_every_verdict() {
    let results = mixed_results();
    let mut outcome = StepOutcome::default();
    record_report(&mut outcome, day(), &results);
    assert_eq!(outcome.rows_in, 5);
    assert_eq!(outcome.rows_out, 1);
    let detail = outcome.detail_json();
    assert_eq!(detail.get("trade_date"), Some(&pyjson::s("2026-08-17")));
    assert_eq!(detail.get("passed"), Some(&OrderedJson::Bool(false)));
    let Some(OrderedJson::Array(checks)) = detail.get("checks") else {
        panic!("checks is a list");
    };
    assert_eq!(checks.len(), 5);
    let first = checks.first().unwrap();
    let keys: Vec<&str> = match first {
        OrderedJson::Object(e) => e.iter().map(|(k, _)| k.as_str()).collect(),
        _ => Vec::new(),
    };
    assert_eq!(keys, ["assertion", "name", "status", "message", "observed"]);
}

#[test]
fn the_report_keeps_the_order_and_the_python_strings() {
    let report: GateReport = report_of(day(), &mixed_results());
    assert_eq!(report.results[0].name, "bar_count_against_baseline");
    assert_eq!(report.results[0].status, "passed");
    assert_eq!(report.failures().len(), 1);
    assert_eq!(report.failures()[0].assertion, 4);
}

// --- assertion 1 -------------------------------------------------------------------------------

#[test]
fn bar_count_skips_without_a_baseline() {
    let r = bar_count_verdict(5, &[0, 0, 0], 0.9);
    assert_eq!(r.status, CheckStatus::Skipped);
    assert_eq!(
        r.message,
        "no prior trading day has bars, so there is no baseline to compare against"
    );
    assert_eq!(observed(&r, "today"), &OrderedJson::Int(5));
}

#[test]
fn bar_count_passes_when_today_matches_history() {
    let r = bar_count_verdict(1, &[1, 1, 1], 0.9);
    assert_eq!(r.status, CheckStatus::Passed);
}

#[test]
fn bar_count_fails_when_most_of_the_day_is_missing() {
    // docs/09 1 is what catches a half-published NSE file (M88's partial day).
    let r = bar_count_verdict(1, &[10, 10, 10], 0.9);
    assert_eq!(r.status, CheckStatus::Failed);
    assert_eq!(observed(&r, "today"), &OrderedJson::Int(1));
}

#[test]
fn bar_count_message_and_observed_are_pythons() {
    // Run 103's own numbers: an even count gives a float median and `.0f` rounds half to even.
    let counts = [4216, 4217, 4100, 4300, 4210, 4220, 4230, 4200, 4250, 4180];
    let r = bar_count_verdict(4205, &counts, 0.9);
    assert_eq!(r.status, CheckStatus::Passed);
    assert_eq!(
        r.message,
        "4205 bars against a 10-day median of 4216 (threshold 3795)"
    );
    assert_eq!(observed(&r, "median"), &OrderedJson::Float(4216.5));
    assert_eq!(observed(&r, "threshold"), &OrderedJson::Float(3794.85));
    // An odd count keeps the int median.
    let r = bar_count_verdict(1687, &[4216, 4100, 4300], 0.9);
    assert_eq!(r.status, CheckStatus::Failed);
    assert_eq!(observed(&r, "median"), &OrderedJson::Int(4216));
    assert_eq!(
        r.message,
        "1687 bars against a 10-day median of 4216 (threshold 3794)"
    );
    // Zero-bar days are not part of the baseline.
    let r = bar_count_verdict(10, &[0, 10, 0], 0.9);
    assert_eq!(observed(&r, "median"), &OrderedJson::Int(10));
}

// --- assertion 4 -------------------------------------------------------------------------------

#[test]
fn factor_rows_must_match_bars_exactly() {
    assert_eq!(factor_rows_verdict(0, 1).status, CheckStatus::Failed);
    assert_eq!(factor_rows_verdict(1, 1).status, CheckStatus::Passed);
    assert_eq!(factor_rows_verdict(3, 2).status, CheckStatus::Failed);
    assert_eq!(
        factor_rows_verdict(4205, 4205).message,
        "4205 factor rows against 4205 bars"
    );
}

// --- assertion 5 -------------------------------------------------------------------------------

fn full_counts() -> HashMap<i64, i64> {
    UNIVERSES
        .iter()
        .map(|(id, slug)| {
            let n = NOMINAL_SIZES
                .iter()
                .find(|(s, _)| s == slug)
                .map_or(100, |(_, n)| *n);
            (*id, n)
        })
        .collect()
}

#[test]
fn an_empty_universe_fails() {
    let r = universe_sizes_verdict(&HashMap::new(), 10, 0.05);
    assert_eq!(r.status, CheckStatus::Failed);
}

#[test]
fn a_correctly_sized_nifty_50_passes_its_own_check() {
    let counts: HashMap<i64, i64> = [(1, 50)].into_iter().collect();
    let r = universe_sizes_verdict(&counts, 10, 0.05);
    let OrderedJson::Object(c) = observed(&r, "counts") else {
        panic!("counts is a dict");
    };
    assert_eq!(
        c.first(),
        Some(&("nifty-50".to_owned(), OrderedJson::Int(50)))
    );
    assert!(!r.message.contains("nifty-50:"));
    assert_eq!(
        r.status,
        CheckStatus::Failed,
        "the other universes still fail"
    );
}

#[test]
fn the_tolerated_universes_are_reported_on_a_passing_run_and_never_fail_it() {
    let mut counts = full_counts();
    for (id, slug) in UNIVERSES {
        if NO_MEMBERSHIP_SOURCE.contains(&slug) {
            counts.insert(id, 0);
        }
    }
    let r = universe_sizes_verdict(&counts, 10, 0.05);
    assert_eq!(r.status, CheckStatus::Passed);
    assert_eq!(
        r.message,
        "every sourced universe is within tolerance; no membership source wired for: etf, \
         nifty-fno"
    );
    assert_eq!(
        observed(&r, "unsourced"),
        &pyjson::strings(["etf", "nifty-fno"])
    );
    assert_eq!(observed(&r, "tolerance"), &OrderedJson::Float(0.05));
}

#[test]
fn the_tolerated_list_is_the_two_that_do_not_block_a_night() {
    assert_eq!(NO_MEMBERSHIP_SOURCE, ["etf", "nifty-fno"]);
    for slug in NO_MEMBERSHIP_SOURCE {
        assert!(UNIVERSES.iter().any(|(_, s)| *s == slug));
    }
    // The indices with real constituent files are never exempt; neither is allcap.
    for (slug, _) in NOMINAL_SIZES {
        assert!(!NO_MEMBERSHIP_SOURCE.contains(&slug));
    }
    assert!(!NO_MEMBERSHIP_SOURCE.contains(&"nifty-allcap"));
}

#[test]
fn an_empty_emerge_fails_only_where_the_box_holds_emerge_instruments() {
    let mut counts = full_counts();
    counts.insert(15, 0);
    let unsourced = universe_sizes_verdict(&counts, 0, 0.05);
    assert_eq!(unsourced.status, CheckStatus::Passed);
    assert_eq!(
        observed(&unsourced, "unsourced"),
        &pyjson::strings(["nse-sme-emerge"])
    );
    let sourced = universe_sizes_verdict(&counts, 459, 0.05);
    assert_eq!(sourced.status, CheckStatus::Failed);
    assert_eq!(sourced.message, "nse-sme-emerge has no members");
}

#[test]
fn a_universe_outside_tolerance_is_named() {
    let mut counts = full_counts();
    counts.insert(5, 470); // nifty-500: 30 out, more than 25
    counts.insert(1, 48); // nifty-50: 2 out, within 2.5
    let r = universe_sizes_verdict(&counts, 10, 0.05);
    assert_eq!(r.status, CheckStatus::Failed);
    assert_eq!(r.message, "nifty-500: 470 members against a nominal 500");
    assert_eq!(observed(&r, "unsourced"), &OrderedJson::Null);
}

#[test]
fn run_103s_counts_pass() {
    let counts: HashMap<i64, i64> = [
        (1, 50),
        (2, 50),
        (3, 100),
        (4, 200),
        (5, 497),
        (6, 748),
        (7, 249),
        (8, 149),
        (9, 248),
        (10, 251),
        (11, 397),
        (12, 4074),
        (13, 218),
        (14, 358),
        (15, 459),
    ]
    .into_iter()
    .collect();
    let r = universe_sizes_verdict(&counts, 459, 0.05);
    assert_eq!(r.status, CheckStatus::Passed);
    assert_eq!(r.message, "every universe is within tolerance");
}

// --- assertion 6 -------------------------------------------------------------------------------

#[test]
fn index_level_skips_loudly_when_weights_are_unpopulated() {
    let r = index_level_verdict(24500.35, 0);
    assert_eq!(r.status, CheckStatus::Skipped);
    assert!(r.message.contains("weight is unpopulated"));
    assert_eq!(
        observed(&r, "published_level"),
        &OrderedJson::Float(24500.35)
    );
}

#[test]
fn index_level_evaluates_once_weights_exist() {
    let r = index_level_verdict(24500.35, 1);
    assert_eq!(r.status, CheckStatus::Passed);
    assert_eq!(
        r.message,
        "published NIFTY 50 level 24500.35 with 1 weighted constituents"
    );
}

// --- assertion 8 -------------------------------------------------------------------------------

#[test]
fn return_distribution_skips_while_ret_12m_is_unpopulated() {
    let r = return_distribution_verdict(None, &[], 3.0);
    assert_eq!(r.status, CheckStatus::Skipped);
    assert!(r.message.contains("factor engine has not run"));
    assert_eq!(r.observed, OrderedJson::Object(Vec::new()));
}

#[test]
fn return_distribution_needs_two_medians_and_a_variance() {
    let r = return_distribution_verdict(Some(1.0), &[1.0], 3.0);
    assert_eq!(r.status, CheckStatus::Skipped);
    assert_eq!(
        r.message,
        "only 1 historical medians available; need at least 2 for a sigma"
    );
    let r = return_distribution_verdict(Some(1.0), &[2.0, 2.0], 3.0);
    assert_eq!(r.status, CheckStatus::Skipped);
    assert_eq!(observed(&r, "mean"), &OrderedJson::Float(2.0));
}

#[test]
fn return_distribution_bands_at_three_sigma() {
    let history = super::pystats::tests_support::bed_medians();
    let r = return_distribution_verdict(Some(-18.01), &history, 3.0);
    // CPython over the same list: mean -12.339830508474575, band 8.85093415039757 (run 103's).
    assert_eq!(r.status, CheckStatus::Passed);
    assert_eq!(
        r.message,
        "ret_12m median -18.01 against a 59-day mean -12.34 +/- 8.85"
    );
    assert_eq!(
        observed(&r, "band"),
        &OrderedJson::Float(8.850_934_150_397_57)
    );
    // A mass mis-adjustment drags the median far outside the band.
    let r = return_distribution_verdict(Some(-80.0), &history, 3.0);
    assert_eq!(r.status, CheckStatus::Failed);
}
