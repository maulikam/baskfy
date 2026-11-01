//! Port of `tests/test_options_calendar.py` — `04` §1: expiries, day roles and lot sizes from the
//! master, never a weekday. The load-bearing case is the holiday-shifted monthly.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use baskfy_core::error::CoreError;
use baskfy_core::options::calendar::{
    Contract, RoleReason, expiries, expiry_for_o1_o3, expiry_for_o2, kind, lot_size_for,
    monthly_expiry, next_session, role, tick_size_for,
};
use baskfy_core::options::config::{ExpiryKind, OptionType, Sleeve};
use chrono::{Datelike, NaiveDate, TimeDelta, Weekday};
use options_common::{
    LOT, contract_full, d, dec, expiries_fixture, master, shifted_master, shifted_monthly, tick,
};
use rust_decimal::Decimal;

const NIFTY: &str = "NIFTY";

fn mon_19() -> NaiveDate {
    d(2026, 10, 19)
}
fn tue_20() -> NaiveDate {
    d(2026, 10, 20)
}
fn wed_21() -> NaiveDate {
    d(2026, 10, 21)
}
fn tue_27() -> NaiveDate {
    d(2026, 10, 27)
}

/// What a weekday implementation would answer: the last Tuesday of the month.
fn weekday_rule_monthly(year: i32, month: u32) -> NaiveDate {
    let (ny, nm) = if month == 12 {
        (year + 1, 1)
    } else {
        (year, month + 1)
    };
    let mut day = d(ny, nm, 1) - TimeDelta::days(1);
    while day.weekday() != Weekday::Tue {
        day -= TimeDelta::days(1);
    }
    day
}

fn invalid<T: std::fmt::Debug>(r: Result<T, CoreError>, needle: &str) {
    match r {
        Err(CoreError::InvalidInput(m)) => assert!(m.contains(needle), "{m}"),
        other => panic!("expected InvalidInput({needle}), got {other:?}"),
    }
}

// --- TestExpiriesComeFromTheMaster ---

#[test]
fn expiries_are_the_distinct_nifty_expiries_ascending() {
    assert_eq!(expiries(&master(), NIFTY), expiries_fixture());
}

#[test]
fn another_underlying_never_leaks_in() {
    assert!(!expiries(&master(), NIFTY).contains(&d(2026, 10, 29)));
    assert_eq!(expiries(&master(), "BANKNIFTY"), vec![d(2026, 10, 29)]);
}

#[test]
fn monthly_is_the_last_expiry_of_the_month() {
    assert_eq!(monthly_expiry(&master(), 2026, 10, NIFTY), Some(tue_27()));
    assert_eq!(
        monthly_expiry(&master(), 2026, 11, NIFTY),
        Some(d(2026, 11, 24))
    );
    assert_eq!(monthly_expiry(&master(), 2026, 12, NIFTY), None);
}

#[test]
fn the_holiday_shift_moves_the_monthly() {
    assert_eq!(
        monthly_expiry(&shifted_master(), 2026, 10, NIFTY),
        Some(shifted_monthly())
    );
    assert_eq!(
        kind(&shifted_master(), shifted_monthly(), NIFTY).unwrap(),
        ExpiryKind::Monthly
    );
}

#[test]
fn the_holiday_fixture_defeats_a_weekday_rule() {
    assert_ne!(
        Some(weekday_rule_monthly(2026, 10)),
        monthly_expiry(&shifted_master(), 2026, 10, NIFTY)
    );
    assert_eq!(
        Some(weekday_rule_monthly(2026, 10)),
        monthly_expiry(&master(), 2026, 10, NIFTY)
    );
}

#[test]
fn kind_distinguishes_weekly_and_monthly() {
    assert_eq!(
        kind(&master(), tue_20(), NIFTY).unwrap(),
        ExpiryKind::Weekly
    );
    assert_eq!(
        kind(&master(), tue_27(), NIFTY).unwrap(),
        ExpiryKind::Monthly
    );
}

#[test]
fn kind_refuses_a_date_that_is_not_an_expiry() {
    invalid(kind(&master(), wed_21(), NIFTY), "not a NIFTY expiry");
}

#[test]
fn the_module_does_no_weekday_arithmetic() {
    let source = include_str!("../src/options/calendar.rs");
    for banned in [
        ".weekday(",
        ".iso_weekday(",
        ".isoweekday(",
        "Weekday::",
        "TUESDAY",
        "THURSDAY",
    ] {
        assert!(!source.contains(banned), "calendar.rs contains {banned}");
    }
}

// --- TestDayRoles ---

fn role_of(day: NaiveDate, sleeve: Sleeve, events: &[NaiveDate], trading: bool) -> RoleReason {
    role(day, sleeve, &master(), events, trading, NIFTY)
        .unwrap()
        .reason
}

#[test]
fn role_table() {
    let cases = [
        (tue_27(), Sleeve::O1m, RoleReason::Trades),
        (tue_20(), Sleeve::O1m, RoleReason::NotMonthly),
        (tue_20(), Sleeve::O1w, RoleReason::Trades),
        (tue_27(), Sleeve::O1w, RoleReason::MonthlyExpiry),
        (tue_20(), Sleeve::O3a, RoleReason::Trades),
        (tue_27(), Sleeve::O3b, RoleReason::Trades),
        (wed_21(), Sleeve::O3a, RoleReason::NotExpiry),
        (wed_21(), Sleeve::O1w, RoleReason::NotExpiry),
        (wed_21(), Sleeve::O2, RoleReason::Trades),
        (tue_27(), Sleeve::O2, RoleReason::Trades),
    ];
    for (day, sleeve, expected) in cases {
        assert_eq!(
            role_of(day, sleeve, &[], true),
            expected,
            "{day} {sleeve:?}"
        );
    }
}

#[test]
fn o1w_refuses_the_monthly_tuesday() {
    let verdict = role(tue_27(), Sleeve::O1w, &master(), &[], true, NIFTY).unwrap();
    assert!(!verdict.trades);
    assert_eq!(verdict.reason, RoleReason::MonthlyExpiry);
}

#[test]
fn an_event_day_stops_every_sleeve() {
    for sleeve in Sleeve::ALL {
        assert_eq!(
            role_of(tue_27(), sleeve, &[tue_27()], true),
            RoleReason::EventDay
        );
    }
}

#[test]
fn a_non_trading_day_stops_every_sleeve() {
    for sleeve in Sleeve::ALL {
        assert_eq!(
            role_of(tue_27(), sleeve, &[], false),
            RoleReason::NotTradingDay
        );
    }
}

#[test]
fn the_shifted_monthly_is_o1ms_day_and_the_tuesday_is_nobodys() {
    let rows = shifted_master();
    assert!(
        role(shifted_monthly(), Sleeve::O1m, &rows, &[], true, NIFTY)
            .unwrap()
            .trades
    );
    let on_tuesday = role(tue_27(), Sleeve::O1m, &rows, &[], true, NIFTY).unwrap();
    assert_eq!(on_tuesday.reason, RoleReason::NotExpiry);
}

// --- TestNextSession ---

#[test]
fn next_monthly() {
    assert_eq!(
        next_session(d(2026, 10, 1), Sleeve::O1m, &master(), &[], NIFTY).unwrap(),
        Some(tue_27())
    );
}

#[test]
fn an_event_expiry_is_skipped_not_shifted() {
    let found = next_session(d(2026, 10, 1), Sleeve::O1m, &master(), &[tue_27()], NIFTY).unwrap();
    assert_eq!(found, Some(d(2026, 11, 24)));
}

#[test]
fn next_weekly_skips_the_monthly() {
    assert_eq!(
        next_session(tue_20(), Sleeve::O1w, &master(), &[], NIFTY).unwrap(),
        Some(d(2026, 11, 3))
    );
}

#[test]
fn strictly_after() {
    assert_eq!(
        next_session(tue_20(), Sleeve::O3a, &master(), &[], NIFTY).unwrap(),
        Some(tue_27())
    );
}

#[test]
fn none_past_the_master() {
    assert_eq!(
        next_session(d(2026, 11, 24), Sleeve::O3a, &master(), &[], NIFTY).unwrap(),
        None
    );
}

#[test]
fn o2_is_not_answered_from_the_master() {
    invalid(
        next_session(tue_20(), Sleeve::O2, &master(), &[], NIFTY),
        "trading calendar",
    );
}

// --- TestContractChoice ---

#[test]
fn monday_uses_tuesdays_contract() {
    assert_eq!(expiry_for_o2(mon_19(), &master(), NIFTY), Some(tue_20()));
}

#[test]
fn tuesday_uses_next_weeks_contract() {
    assert_eq!(expiry_for_o2(tue_20(), &master(), NIFTY), Some(tue_27()));
}

#[test]
fn midweek_uses_the_next_expiry() {
    assert_eq!(expiry_for_o2(wed_21(), &master(), NIFTY), Some(tue_27()));
}

#[test]
fn no_expiry_left() {
    assert_eq!(expiry_for_o2(d(2026, 11, 24), &master(), NIFTY), None);
}

#[test]
fn o1_and_o3_trade_todays_contract() {
    assert_eq!(expiry_for_o1_o3(tue_20()), tue_20());
}

// --- TestLotAndTickSize ---

#[test]
fn read_from_the_master() {
    assert_eq!(lot_size_for(&master(), tue_20(), NIFTY), Some(LOT));
    assert_eq!(tick_size_for(&master(), tue_20(), NIFTY), Some(dec("0.05")));
}

#[test]
fn banknifty_lot_size_is_its_own() {
    assert_eq!(
        lot_size_for(&master(), d(2026, 10, 29), "BANKNIFTY"),
        Some(30)
    );
    assert_eq!(lot_size_for(&master(), d(2026, 10, 29), NIFTY), None);
}

#[test]
fn missing_expiry() {
    assert_eq!(lot_size_for(&master(), wed_21(), NIFTY), None);
    assert_eq!(tick_size_for(&master(), wed_21(), NIFTY), None);
}

#[test]
fn zero_is_refused() {
    let rows = vec![contract_full(
        tue_20(),
        Decimal::from(25000),
        OptionType::Ce,
        0,
        Decimal::ZERO,
        NIFTY,
        0,
    )];
    assert_eq!(lot_size_for(&rows, tue_20(), NIFTY), None);
    assert_eq!(tick_size_for(&rows, tue_20(), NIFTY), None);
}

#[test]
fn disagreeing_rows_are_refused() {
    let rows = vec![
        contract_full(
            tue_20(),
            Decimal::from(25000),
            OptionType::Ce,
            65,
            tick(),
            NIFTY,
            0,
        ),
        contract_full(
            tue_20(),
            Decimal::from(25000),
            OptionType::Pe,
            75,
            tick(),
            NIFTY,
            0,
        ),
    ];
    assert_eq!(lot_size_for(&rows, tue_20(), NIFTY), None);
}

// --- TestVerdictShape ---

#[test]
fn trades_iff_the_reason_is_trades() {
    let no_events: Vec<NaiveDate> = Vec::new();
    for sleeve in Sleeve::ALL {
        for day in [mon_19(), tue_20(), wed_21(), tue_27()] {
            for trading in [true, false] {
                for events in [no_events.clone(), vec![tue_27()]] {
                    let verdict = role(day, sleeve, &master(), &events, trading, NIFTY).unwrap();
                    assert_eq!(verdict.trades, verdict.reason == RoleReason::Trades);
                }
            }
        }
    }
}

#[test]
fn a_lot_size_of_one_is_a_lot_size() {
    let rows: Vec<Contract> = vec![contract_full(
        tue_20(),
        Decimal::from(25000),
        OptionType::Ce,
        1,
        tick(),
        NIFTY,
        0,
    )];
    assert_eq!(lot_size_for(&rows, tue_20(), NIFTY), Some(1));
}
