//! Port of `tests/test_options_journal.py`: `04` §12 the journal in R, never pooling sleeves.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeMap;

use rust_decimal::Decimal;

use baskfy_core::options::config::{ExpiryKind, SizingMode, Sleeve};
use baskfy_core::options::journal::{
    Bucket, JournalRow, PoolKey, max_drawdown, r_multiple, summarize, summarize_one,
};
use options_common::{d, dec};

const ONE_LOT: SizingMode = SizingMode::PaperOneLot;

fn r() -> Decimal {
    Decimal::from(1000)
}

struct RowOpts {
    simulated: bool,
    mode: SizingMode,
    closed: &'static str,
    kind: Option<ExpiryKind>,
    minutes: i64,
}

impl Default for RowOpts {
    fn default() -> Self {
        Self {
            simulated: true,
            mode: ONE_LOT,
            closed: "PROFIT",
            kind: None,
            minutes: 60,
        }
    }
}

fn row_with(sleeve: Sleeve, day: u32, rm: Option<&str>, o: RowOpts) -> JournalRow {
    let mut row = JournalRow::new(sleeve, d(2026, 10, day), o.simulated, o.mode, rm.is_some());
    match rm {
        None => row.skip_reason = Some(o.closed.to_owned()),
        Some(x) => {
            row.expiry_kind = o.kind;
            row.net_pnl_inr = dec(x) * r();
            row.r_inr = r();
            row.closed_reason = Some(o.closed.to_owned());
            row.minutes_held = o.minutes;
            row.mae_r = dec("-0.5");
            row.mfe_r = dec(x).abs();
        }
    }
    row
}

fn row(sleeve: Sleeve, day: u32, rm: Option<&str>) -> JournalRow {
    row_with(sleeve, day, rm, RowOpts::default())
}

fn closed(c: &'static str, minutes: i64) -> RowOpts {
    RowOpts {
        closed: c,
        minutes,
        ..RowOpts::default()
    }
}

fn real() -> RowOpts {
    RowOpts {
        simulated: false,
        ..RowOpts::default()
    }
}

fn budget() -> RowOpts {
    RowOpts {
        mode: SizingMode::Budget,
        ..RowOpts::default()
    }
}

fn key(sleeve: Sleeve, simulated: bool, sizing_mode: SizingMode) -> PoolKey {
    PoolKey {
        sleeve,
        simulated,
        sizing_mode,
    }
}

fn bucket(count: i64, mean: Decimal) -> Bucket {
    Bucket {
        count,
        mean_r: mean,
    }
}

#[test]
fn one_summary_per_sleeve_simulated_and_sizing_mode() {
    let rows = vec![
        row(Sleeve::O1m, 27, Some("1")),
        row_with(Sleeve::O1m, 27, Some("1"), real()),
        row(Sleeve::O2, 21, Some("-1")),
        row_with(Sleeve::O2, 22, Some("2"), budget()),
        row(Sleeve::O3a, 20, Some("1")),
        row(Sleeve::O3b, 20, Some("1")),
    ];
    let keys: Vec<PoolKey> = summarize(&rows).unwrap().iter().map(|s| s.key).collect();
    let mut unique = keys.clone();
    unique.sort();
    unique.dedup();
    assert_eq!(keys.len(), 6);
    assert_eq!(unique.len(), 6);
    assert!(keys.contains(&key(Sleeve::O1m, false, ONE_LOT)));
    assert!(keys.contains(&key(Sleeve::O3a, true, ONE_LOT)));
    assert!(keys.contains(&key(Sleeve::O3b, true, ONE_LOT)));
}

#[test]
fn summarize_one_refuses_mixed_rows() {
    let others = [
        row(Sleeve::O1w, 20, Some("1")),
        row_with(Sleeve::O1m, 27, Some("1"), real()),
        row_with(Sleeve::O1m, 27, Some("1"), budget()),
    ];
    for other in others {
        assert!(summarize_one(&[row(Sleeve::O1m, 27, Some("1")), other]).is_err());
    }
}

#[test]
fn no_rows_is_not_a_summary() {
    assert!(summarize_one(&[]).is_err());
}

fn the_rows() -> Vec<JournalRow> {
    vec![
        row_with(Sleeve::O2, 19, Some("1"), closed("TARGET", 30)),
        row_with(Sleeve::O2, 20, Some("-2"), closed("STOP", 10)),
        row_with(Sleeve::O2, 21, Some("-1"), closed("STOP", 20)),
        row_with(Sleeve::O2, 22, Some("3"), closed("TARGET", 60)),
        row_with(Sleeve::O2, 23, None, closed("GAP_TOO_BIG", 60)),
        row_with(Sleeve::O2, 26, None, closed("GAP_TOO_BIG", 60)),
        row_with(Sleeve::O2, 27, None, closed("VIX_TOO_HIGH", 60)),
    ]
}

#[test]
fn counts_and_skips_by_reason() {
    let s = summarize_one(&the_rows()).unwrap();
    assert_eq!((s.count, s.traded), (7, 4));
    assert_eq!(
        s.skipped_by_reason,
        vec![
            ("GAP_TOO_BIG".to_owned(), 2),
            ("VIX_TOO_HIGH".to_owned(), 1)
        ]
    );
}

#[test]
fn r_statistics() {
    let s = summarize_one(&the_rows()).unwrap();
    assert_eq!(s.win_rate, Some(dec("0.5")));
    assert_eq!(s.mean_r, s.expectancy_r);
    assert_eq!(s.mean_r, Some(dec("0.25")));
    assert_eq!(s.expectancy_inr, Some(Decimal::from(250)));
    assert_eq!(s.worst_r, Some(Decimal::from(-2)));
}

#[test]
fn drawdown_on_the_running_sum() {
    let s = summarize_one(&the_rows()).unwrap();
    assert_eq!(s.max_drawdown_r, Decimal::from(3));
    assert_eq!(s.max_drawdown_inr, Decimal::from(3000));
}

#[test]
fn distributions_and_splits() {
    let s = summarize_one(&the_rows()).unwrap();
    assert_eq!(s.mean_minutes_held, Some(Decimal::from(30)));
    assert_eq!(
        s.mfe_r,
        vec![
            Decimal::from(1),
            Decimal::from(1),
            Decimal::from(2),
            Decimal::from(3)
        ]
    );
    let by_reason: BTreeMap<String, Bucket> = s.by_closed_reason.into_iter().collect();
    assert_eq!(
        by_reason,
        BTreeMap::from([
            ("STOP".to_owned(), bucket(2, dec("-1.5"))),
            ("TARGET".to_owned(), bucket(2, Decimal::from(2))),
        ])
    );
    let by_weekday: BTreeMap<i64, Bucket> = s.by_weekday.into_iter().collect();
    assert_eq!(
        by_weekday,
        BTreeMap::from([
            (1, bucket(1, Decimal::from(1))),
            (2, bucket(1, Decimal::from(-2))),
            (3, bucket(1, Decimal::from(-1))),
            (4, bucket(1, Decimal::from(3))),
        ])
    );
    assert!(s.by_expiry_kind.is_empty());
}

#[test]
fn expiry_kind_split_for_o3() {
    let kind = |k| RowOpts {
        kind: Some(k),
        ..RowOpts::default()
    };
    let rows = vec![
        row_with(Sleeve::O3a, 20, Some("1"), kind(ExpiryKind::Weekly)),
        row_with(Sleeve::O3a, 27, Some("-1"), kind(ExpiryKind::Monthly)),
        row_with(Sleeve::O3a, 13, Some("2"), kind(ExpiryKind::Weekly)),
    ];
    let s = summarize_one(&rows).unwrap();
    let by_kind: BTreeMap<String, Bucket> = s.by_expiry_kind.into_iter().collect();
    assert_eq!(
        by_kind,
        BTreeMap::from([
            ("MONTHLY".to_owned(), bucket(1, Decimal::from(-1))),
            ("WEEKLY".to_owned(), bucket(2, dec("1.5"))),
        ])
    );
    assert!(s.by_weekday.is_empty());
}

#[test]
fn only_skips() {
    let s = summarize_one(&[row_with(Sleeve::O1m, 27, None, closed("ER_TOO_HIGH", 60))]).unwrap();
    assert_eq!(s.traded, 0);
    assert_eq!(s.win_rate, None);
    assert_eq!(s.mean_r, None);
    assert_eq!(s.worst_r, None);
    assert_eq!(s.max_drawdown_r, Decimal::ZERO);
}

#[test]
fn r_multiple_helper() {
    assert_eq!(r_multiple(Decimal::from(-1500), r()).unwrap(), dec("-1.5"));
    let e = r_multiple(Decimal::ONE, Decimal::ZERO).unwrap_err();
    assert!(e.to_string().contains("positive"));
}

#[test]
fn drawdown_from_the_first_trade() {
    assert_eq!(
        max_drawdown(&[Decimal::from(-1), Decimal::from(-1)]).unwrap(),
        Decimal::from(2)
    );
    assert_eq!(
        max_drawdown(&[Decimal::from(1), Decimal::from(2)]).unwrap(),
        Decimal::ZERO
    );
}
