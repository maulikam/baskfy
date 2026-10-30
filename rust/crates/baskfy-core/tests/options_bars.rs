//! Ports `decile-blueprint/packages/core/tests/test_options_bars.py` (OP4): windows, 5-minute
//! bars aligned to 09:15 and built from one-minute bars, closed minutes only, IST whatever the
//! stored zone. The fixture helpers are `options_scan_fixtures.at`/`bars_from_closes`/`ramp`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_core::options::bars as b;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use rust_decimal::Decimal;

fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, 27).unwrap()
}

fn t(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap()
}

fn at(h: u32, m: u32) -> NaiveDateTime {
    day().and_time(t(h, m))
}

fn ramp(start: Decimal, per_minute: Decimal, count: i64) -> Vec<Decimal> {
    (0..count)
        .map(|i| start + per_minute * Decimal::from(i + 1))
        .collect()
}

fn bars_from_closes(first_open: Decimal, closes: &[Decimal]) -> Vec<b::Bar> {
    let wick = Decimal::from(2);
    let mut prev = first_open;
    let mut out = Vec::new();
    for (i, close) in closes.iter().enumerate() {
        out.push(b::Bar {
            ts: at(9, 15) + Duration::minutes(i64::try_from(i).unwrap()),
            open: prev,
            high: prev.max(*close) + wick,
            low: prev.min(*close) - wick,
            close: *close,
        });
        prev = *close;
    }
    out
}

fn day_bars(count: i64) -> Vec<b::Bar> {
    bars_from_closes(
        Decimal::from(25000),
        &ramp(Decimal::from(25000), Decimal::ONE, count),
    )
}

#[test]
fn an_aware_utc_timestamp_is_read_in_ist() {
    let utc: DateTime<FixedOffset> = Utc
        .with_ymd_and_hms(2026, 10, 27, 3, 45, 0)
        .unwrap()
        .fixed_offset();
    assert_eq!(b::minute(b::ist(&utc)), t(9, 15));
    assert_eq!(b::ist(&utc), at(9, 15));
}

#[test]
fn only_closed_minutes_count() {
    let seen = b::closed(
        &day_bars(30),
        day().and_time(NaiveTime::from_hms_opt(9, 20, 59).unwrap()),
    );
    assert_eq!(b::minute(seen.last().unwrap().ts), t(9, 19));
    assert_eq!(b::closed(&day_bars(30), at(9, 20)).len(), 5);
}

#[test]
fn a_duplicate_minute_keeps_the_last_reading() {
    let first = day_bars(1).remove(0);
    let again = b::Bar {
        close: Decimal::ONE,
        ..first.clone()
    };
    assert_eq!(b::closed(&[first, again.clone()], at(10, 0)), vec![again]);
}

#[test]
fn windows_are_inclusive_by_start_minute() {
    assert_eq!(b::window(&day_bars(30), t(9, 15), t(9, 44)).len(), 30);
    assert_eq!(b::expected_minutes(t(9, 15), t(9, 44)), 30);
    assert_eq!(b::expected_minutes(t(9, 15), t(10, 14)), 60);
}

#[test]
fn a_window_settles_on_its_last_minute_or_after_the_grace() {
    let bars = day_bars(29);
    assert!(!b::window_settled(&bars, t(9, 44), at(9, 46), 120));
    assert!(b::window_settled(&bars, t(9, 44), at(9, 47), 120));
    assert!(b::window_settled(&day_bars(30), t(9, 44), at(9, 45), 120));
}

#[test]
fn the_clock_helpers() {
    assert_eq!(b::minute_after(t(9, 59)), t(10, 0));
    assert_eq!(b::add_seconds(t(23, 59), 120), b::time_max());
    let err = b::pct(Decimal::ONE, Decimal::ZERO).unwrap_err();
    assert!(err.to_string().contains("percent of zero"));
}

#[test]
fn aligned_to_0915_and_named_by_the_last_minute() {
    let fives = b::five_minute_bars(&day_bars(12), t(9, 15), 5).unwrap();
    let names: Vec<_> = fives.iter().map(|f| (f.start, f.close_time)).collect();
    assert_eq!(
        names,
        vec![(t(9, 15), t(9, 19)), (t(9, 20), t(9, 24)), (t(9, 25), t(9, 29))]
    );
    let first = &fives[0];
    assert_eq!(first.open, Decimal::from(25000));
    assert_eq!(first.close, Some(Decimal::from(25005)));
    assert_eq!(first.high, Decimal::from(25007));
    assert_eq!(first.low, Decimal::from(24998));
    assert!(first.complete() && first.minutes == 5);
}

#[test]
fn a_bucket_without_its_last_minute_is_not_complete() {
    let fives = b::five_minute_bars(&day_bars(12), t(9, 15), 5).unwrap();
    let last = fives.last().unwrap();
    assert!(!last.complete() && last.close.is_none() && last.minutes == 2);
}

#[test]
fn a_missing_middle_minute_keeps_the_close() {
    let bars: Vec<_> = day_bars(5)
        .into_iter()
        .filter(|x| b::minute(x.ts) != t(9, 17))
        .collect();
    let fives = b::five_minute_bars(&bars, t(9, 15), 5).unwrap();
    assert_eq!(fives.len(), 1);
    let five = &fives[0];
    assert!(five.complete() && five.minutes == 4 && five.close == Some(Decimal::from(25005)));
}

#[test]
fn bars_before_the_open_are_ignored_and_width_must_be_positive() {
    let early = b::Bar {
        ts: at(9, 10),
        open: Decimal::ONE,
        high: Decimal::ONE,
        low: Decimal::ONE,
        close: Decimal::ONE,
    };
    assert!(b::five_minute_bars(&[early], t(9, 15), 5).unwrap().is_empty());
    let err = b::five_minute_bars(&[], t(9, 15), 0).unwrap_err();
    assert!(err.to_string().contains("one minute"));
}

#[test]
fn the_efficiency_ratio_and_its_path() {
    // Not a Python test of its own: `er_points` + `efficiency_ratio` as condor §2.4 states them.
    let bars = day_bars(3);
    let path = b::er_points(&bars);
    assert_eq!(path, vec![Decimal::from(25000), Decimal::from(25002), Decimal::from(25003)]);
    assert_eq!(b::efficiency_ratio(&path).unwrap(), Decimal::ONE);
    assert_eq!(b::efficiency_ratio(&[Decimal::ONE]).unwrap(), Decimal::ZERO);
}
