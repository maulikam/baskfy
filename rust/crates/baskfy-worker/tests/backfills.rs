//! The backfills' pure contracts, ported from `test_index_backfill.py`, `test_deep_backfill.py`,
//! `test_bhavcopy_backfill.py` and `test_eq_bars_worker.py` (no database, no network).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::{BTreeMap, BTreeSet};

use baskfy_db::OrderedJson;
use baskfy_providers::records::{MinuteBarFields, MinuteBarRecord, PyDateTime};
use baskfy_providers::tokens::ist;
use baskfy_worker::tasks::backfills::{bhavcopy, breadth, cli, deep, eq_bars, index_backfill};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn overlap(a: &str, b: &str) -> bool {
    let a: BTreeSet<String> = index_backfill::keys(a).into_iter().collect();
    index_backfill::keys(b).iter().any(|k| a.contains(k))
}

#[test]
fn index_names_differ_only_in_case_and_punctuation() {
    for (ours, theirs) in [
        ("Nifty Bank", "NIFTY BANK"),
        ("NIFTY 50", "NIFTY 50"),
        ("Nifty50 Dividend Points", "NIFTY50 DIVIDEND POINTS"),
        ("Nifty MidSmallcap400 50:50", "NIFTY MIDSMALLCAP400 50 50"),
    ] {
        assert_eq!(
            index_backfill::normalise(ours),
            index_backfill::normalise(theirs)
        );
    }
    assert_ne!(
        index_backfill::normalise("NIFTY 50"),
        index_backfill::normalise("NIFTY 500")
    );
    assert_ne!(
        index_backfill::normalise("Nifty Midcap 50"),
        index_backfill::normalise("Nifty Midcap 100")
    );
}

#[test]
fn an_abbreviated_index_name_meets_its_long_form() {
    for (ours, theirs) in [
        ("NIFTY SMALLCAP 250", "NIFTY SMLCAP 250"),
        ("Nifty Infrastructure", "NIFTY INFRA"),
        ("Nifty India Consumption", "NIFTY CONSUMPTION"),
        ("Nifty50 PR 2x Leverage", "NIFTY50 PR 2X LEV"),
    ] {
        assert!(overlap(ours, theirs), "{ours} / {theirs}");
    }
}

#[test]
fn expansion_keeps_the_literal_form_and_runs_one_way() {
    assert!(
        index_backfill::keys("Nifty India Consumption")
            .contains(&"NIFTYINDIACONSUMPTION".to_owned())
    );
}

#[test]
fn distinct_indices_never_meet() {
    assert!(!overlap("NIFTY 50", "NIFTY 500"));
    assert!(!overlap("Nifty Midcap 150", "Nifty Smallcap 250"));
    assert!(!overlap("NIFTY IT", "NIFTY BANK"));
}

#[test]
fn every_abbreviation_shortens() {
    for (short, long) in index_backfill::ABBREVIATIONS {
        assert_ne!(short, long);
        assert!(short.len() < long.len());
    }
}

#[test]
fn no_deep_window_exceeds_kites_cap_and_none_has_a_gap() {
    let windows = deep::chunk_windows(d(2017, 1, 1), d(2026, 8, 21));
    assert_eq!(windows.len(), 2);
    assert_eq!(windows[0].0, d(2017, 1, 1));
    assert_eq!(windows.last().unwrap().1, d(2026, 8, 21));
    for (start, end) in &windows {
        assert!((*end - *start).num_days() < deep::MAX_SPAN_DAYS);
    }
    for pair in windows.windows(2) {
        assert_eq!(pair[1].0, pair[0].1 + Duration::days(1));
    }
}

#[test]
fn a_deep_request_before_kites_epoch_is_clamped() {
    assert_eq!(
        deep::chunk_windows(d(1990, 1, 1), d(2001, 1, 1))[0].0,
        deep::kite_epoch()
    );
    assert_eq!(deep::kite_epoch(), d(2000, 1, 3));
}

#[test]
fn d5_history_is_three_kite_requests_from_2011() {
    assert_eq!(deep::default_start(), d(2011, 1, 1));
    let windows = deep::chunk_windows(deep::default_start(), d(2026, 8, 21));
    assert_eq!(windows.len(), 3);
    assert_eq!(windows[0].0, d(2011, 1, 1));
}

fn series(points: &[(NaiveDate, f64)]) -> BTreeMap<NaiveDate, f64> {
    points.iter().copied().collect()
}

#[test]
fn the_splice_anchors_on_the_first_shared_nonzero_date() {
    let seam = d(2024, 1, 1);
    let (factor, joined) =
        deep::splice_factor(&series(&[(seam, 100.0)]), &series(&[(seam, 105.0)]));
    assert!((factor.unwrap() - 1.05).abs() < 1e-12);
    assert_eq!(joined, Some(seam));
    let late = d(2025, 1, 1);
    let (factor, joined) = deep::splice_factor(
        &series(&[(seam, 100.0), (late, 200.0)]),
        &series(&[(seam, 105.0), (late, 240.0)]),
    );
    assert_eq!(joined, Some(seam));
    assert!((factor.unwrap() - 1.05).abs() < 1e-12);
    assert_eq!(
        deep::splice_factor(&series(&[(seam, 100.0)]), &series(&[(late, 105.0)])),
        (None, None)
    );
    assert_eq!(
        deep::splice_factor(&series(&[(seam, 0.0)]), &series(&[(seam, 105.0)])),
        (None, None)
    );
    assert_eq!(
        deep::splice_factor(&series(&[(seam, 100.0)]), &series(&[(seam, 0.0)])),
        (None, None)
    );
}

#[test]
fn the_deep_report_names_a_refusal_and_what_to_decompress() {
    let mut report = deep::Report {
        skipped_compressed: 12,
        ..deep::Report::default()
    };
    report.compressed_ranges.extend([
        "2017-04-24..2018-04-19".to_owned(),
        "2018-04-19..2019-04-14".to_owned(),
    ]);
    let text = deep::render(&report, true);
    assert!(text.contains("REFUSED, compressed chunk   : 12"));
    assert!(text.contains("2017-04-24..2018-04-19") && text.contains("2018-04-19..2019-04-14"));
    assert!(text.contains("Decompress"));
    let clean = deep::render(
        &deep::Report {
            instruments: 3,
            written: 1_000,
            ..deep::Report::default()
        },
        true,
    );
    assert!(clean.contains("REFUSED, compressed chunk   : 0"));
    assert!(clean.contains("bars written") && clean.contains(": 1,000"));
    assert!(!clean.contains("Decompress"));
    assert!(deep::render(&deep::Report::default(), false).contains("[DRY RUN"));
}

#[test]
fn a_bhavcopy_report_fails_only_on_a_failure_not_a_missing_day() {
    assert!(bhavcopy::Report::default().succeeded());
    let mut missing = bhavcopy::Report::default();
    missing.missing_days.push(d(2026, 8, 18));
    assert!(missing.succeeded());
    let mut failed = bhavcopy::Report::default();
    failed
        .failures
        .push(("2026-08-18".to_owned(), OrderedJson::Str("boom".to_owned())));
    assert!(!failed.succeeded());
}

#[test]
fn the_default_series_are_main_board_and_emerge_never_government_paper() {
    assert_eq!(
        bhavcopy::EQUITY_SERIES,
        ["EQ", "BE", "BZ", "SM", "ST", "SZ"]
    );
    for s in ["GS", "GB", "TB", "N0"] {
        assert!(!bhavcopy::EQUITY_SERIES.contains(&s));
    }
}

#[test]
fn a_breadth_dry_run_estimates_rather_than_writes() {
    let report = breadth::Report {
        dates: 80,
        ..breadth::Report::default()
    };
    let text = breadth::render(&report, false);
    assert!(text.contains("[DRY RUN"));
    assert!(text.contains("~60 minutes"));
    assert_eq!(breadth::DEFAULT_EVERY, 5);
}

#[test]
fn the_six_python_modules_are_subcommands() {
    for name in [
        "backfill",
        "deep_backfill",
        "deep-backfill",
        "bhavcopy_backfill",
        "breadth-backfill",
        "index_backfill",
        "index-repair",
    ] {
        assert!(cli::is_subcommand(name), "{name}");
    }
    assert!(!cli::is_subcommand("nightly"));
}

fn minute(day: NaiveDate, h: u32, m: u32) -> DateTime<FixedOffset> {
    day.and_time(NaiveTime::from_hms_opt(h, m, 0).unwrap())
        .and_local_timezone(ist())
        .unwrap()
}

fn bar(ts: DateTime<FixedOffset>, price: &str) -> MinuteBarRecord {
    let v: Decimal = price.parse().unwrap();
    MinuteBarRecord::new(MinuteBarFields {
        ts: PyDateTime::Aware(ts),
        open: v,
        high: v,
        low: v,
        close: v,
        volume: 1,
    })
    .unwrap()
}

#[test]
fn only_closed_minutes_are_kept() {
    let day = d(2026, 9, 25);
    let bars = vec![
        bar(minute(day, 9, 15), "100"),
        bar(minute(day, 9, 16), "101"),
        bar(minute(day, 9, 17), "102"),
    ];
    let now = minute(day, 9, 17) + Duration::seconds(30);
    let kept: Vec<_> = eq_bars::closed_bars(bars, now)
        .into_iter()
        .map(|b| b.ts.aware().unwrap())
        .collect();
    assert_eq!(kept, vec![minute(day, 9, 15), minute(day, 9, 16)]);
}

#[test]
fn minute_windows_are_kites_sixty_day_chunks_inclusive() {
    let windows = eq_bars::day_windows(d(2026, 1, 1), d(2026, 3, 31)).unwrap();
    assert_eq!(windows[0], (d(2026, 1, 1), d(2026, 3, 1)));
    assert_eq!(windows.last().unwrap().1, d(2026, 3, 31));
    assert_eq!(windows.len(), 2);
    let err = eq_bars::day_windows(d(2026, 3, 1), d(2026, 1, 1)).unwrap_err();
    assert!(err.to_string().contains("precedes"));
    assert_eq!(err.python_type(), "ValueError");
}
