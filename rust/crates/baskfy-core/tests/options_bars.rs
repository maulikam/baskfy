//! Port of `tests/test_options_bars.py` — OP4's shared readings: windows, 5-minute bars aligned
//! to 09:15 from one-minute bars, closed minutes only, IST whatever the stored zone.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use baskfy_core::error::CoreError;
use baskfy_core::options::bars as b;
use chrono::{FixedOffset, NaiveDateTime, NaiveTime, TimeDelta, TimeZone, Utc};
use options_common::{at, bars_from_closes, d, quiet_monthly, ramp};
use rust_decimal::Decimal;

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap()
}

fn open() -> NaiveTime {
    hm(9, 15)
}

fn day_bars(count: usize) -> Vec<b::Bar> {
    bars_from_closes(
        quiet_monthly(),
        Decimal::from(25000),
        &ramp(Decimal::from(25000), Decimal::ONE, count),
    )
}

fn invalid<T: std::fmt::Debug>(r: Result<T, CoreError>, needle: &str) {
    match r {
        Err(CoreError::InvalidInput(m)) => assert!(m.contains(needle), "{m}"),
        other => panic!("expected InvalidInput({needle}), got {other:?}"),
    }
}

// --- TestMinutes ---

#[test]
fn an_aware_utc_timestamp_is_read_in_ist() {
    let utc = Utc
        .with_ymd_and_hms(2026, 10, 27, 3, 45, 0)
        .unwrap()
        .with_timezone(&FixedOffset::east_opt(0).unwrap());
    assert_eq!(b::minute(&utc), hm(9, 15));
    assert_eq!(b::ist(&utc), NaiveDateTime::new(d(2026, 10, 27), hm(9, 15)));
}

#[test]
fn only_closed_minutes_count() {
    let now = at(quiet_monthly(), 9, 20) + TimeDelta::seconds(59);
    let seen = b::closed(&day_bars(30), &now);
    assert_eq!(b::minute(&seen.last().unwrap().ts), hm(9, 19));
    assert_eq!(
        b::closed(&day_bars(30), &at(quiet_monthly(), 9, 20)).len(),
        5
    );
}

#[test]
fn a_duplicate_minute_keeps_the_last_reading() {
    let first = day_bars(1)[0].clone();
    let again = b::Bar {
        close: Decimal::ONE,
        ..first.clone()
    };
    assert_eq!(
        b::closed(&[first, again.clone()], &at(quiet_monthly(), 10, 0)),
        vec![again]
    );
}

#[test]
fn windows_are_inclusive_by_start_minute() {
    assert_eq!(b::window(&day_bars(30), hm(9, 15), hm(9, 44)).len(), 30);
    assert_eq!(b::expected_minutes(hm(9, 15), hm(9, 44)), 30);
    assert_eq!(b::expected_minutes(hm(9, 15), hm(10, 14)), 60);
}

#[test]
fn a_window_settles_on_its_last_minute_or_after_the_grace() {
    let bars = day_bars(29);
    assert!(!b::window_settled(
        &bars,
        hm(9, 44),
        &at(quiet_monthly(), 9, 46),
        120
    ));
    assert!(b::window_settled(
        &bars,
        hm(9, 44),
        &at(quiet_monthly(), 9, 47),
        120
    ));
    assert!(b::window_settled(
        &day_bars(30),
        hm(9, 44),
        &at(quiet_monthly(), 9, 45),
        120
    ));
}

#[test]
fn the_clock_helpers() {
    assert_eq!(b::minute_after(hm(9, 59)), hm(10, 0));
    assert_eq!(
        b::add_seconds(hm(23, 59), 120),
        NaiveTime::from_hms_micro_opt(23, 59, 59, 999_999).unwrap()
    );
    invalid(b::pct(Decimal::ONE, Decimal::ZERO), "percent of zero");
}

// --- TestFiveMinuteBars ---

#[test]
fn aligned_to_0915_and_named_by_the_last_minute() {
    let fives = b::five_minute_bars(&day_bars(12), open(), 5).unwrap();
    let spans: Vec<(NaiveTime, NaiveTime)> =
        fives.iter().map(|f| (f.start, f.close_time)).collect();
    assert_eq!(
        spans,
        vec![
            (hm(9, 15), hm(9, 19)),
            (hm(9, 20), hm(9, 24)),
            (hm(9, 25), hm(9, 29)),
        ]
    );
    let first = &fives[0];
    assert_eq!(first.open, Decimal::from(25000));
    assert_eq!(first.close, Some(Decimal::from(25005)));
    assert_eq!(first.high, Decimal::from(25007));
    assert_eq!(first.low, Decimal::from(24998));
    assert!(first.complete());
    assert_eq!(first.minutes, 5);
}

#[test]
fn a_bucket_without_its_last_minute_is_not_complete() {
    let fives = b::five_minute_bars(&day_bars(12), open(), 5).unwrap();
    let last = fives.last().unwrap();
    assert!(!last.complete());
    assert_eq!(last.close, None);
    assert_eq!(last.minutes, 2);
}

#[test]
fn a_missing_middle_minute_keeps_the_close() {
    let bars: Vec<b::Bar> = day_bars(5)
        .into_iter()
        .filter(|x| b::minute(&x.ts) != hm(9, 17))
        .collect();
    let fives = b::five_minute_bars(&bars, open(), 5).unwrap();
    assert_eq!(fives.len(), 1);
    let five = &fives[0];
    assert!(five.complete());
    assert_eq!(five.minutes, 4);
    assert_eq!(five.close, Some(Decimal::from(25005)));
}

#[test]
fn bars_before_the_open_are_ignored_and_width_must_be_positive() {
    let early = b::Bar {
        ts: at(quiet_monthly(), 9, 10),
        open: Decimal::ONE,
        high: Decimal::ONE,
        low: Decimal::ONE,
        close: Decimal::ONE,
    };
    assert!(b::five_minute_bars(&[early], open(), 5).unwrap().is_empty());
    invalid(b::five_minute_bars(&[], open(), 0), "one minute");
}
