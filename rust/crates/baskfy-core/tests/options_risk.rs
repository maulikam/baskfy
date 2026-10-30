//! Port of `tests/test_options_risk.py`: `04` §9 per-sleeve R limits, the per-trade breach, the
//! book's rupee limits.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use chrono::{NaiveDate, TimeDelta};
use rust_decimal::Decimal;

use baskfy_core::error::CoreResult;
use baskfy_core::options::config::{OptionsCeilings, OptionsConfig, RiskConfig, Sleeve};
use baskfy_core::options::risk::{
    BookLimits, Pause, PauseReason, REJECTED_PAUSED, RealisedTrade, SleeveDay, book_limits,
    evaluate_book, evaluate_sleeve, last_trading_day_of_week, month_end, plan_refusal,
    trade_breached,
};
use options_common::{d, dec};

fn risk() -> RiskConfig {
    OptionsConfig::default().risk
}

fn wed() -> NaiveDate {
    d(2026, 10, 21)
}

/// That week's trading days, with Friday 23rd a holiday.
fn week() -> Vec<NaiveDate> {
    vec![d(2026, 10, 19), d(2026, 10, 20), wed(), d(2026, 10, 22)]
}

fn r() -> Decimal {
    Decimal::from(1000)
}

fn trade_in(day: NaiveDate, rm: &str, sleeve: Sleeve) -> RealisedTrade {
    RealisedTrade {
        sleeve,
        trade_date: day,
        net_pnl_inr: dec(rm) * r(),
        r_multiple: dec(rm),
    }
}

fn trade(day: NaiveDate, rm: &str) -> RealisedTrade {
    trade_in(day, rm, Sleeve::O2)
}

fn try_eval(
    history: Vec<RealisedTrade>,
    marked: &str,
    r_today: Decimal,
) -> CoreResult<Option<Pause>> {
    evaluate_sleeve(
        &SleeveDay {
            sleeve: Sleeve::O2,
            today: wed(),
            history,
            today_marked_inr: dec(marked),
            r_today_inr: r_today,
            trading_days: week(),
        },
        &risk(),
    )
}

fn eval(history: Vec<RealisedTrade>, marked: &str) -> Option<Pause> {
    try_eval(history, marked, r()).unwrap()
}

fn pause(until: NaiveDate, reasons: &[PauseReason], close_now: bool) -> Pause {
    Pause {
        paused_until: until,
        reasons: reasons.to_vec(),
        close_now,
    }
}

#[test]
fn minus_two_r_today_closes_and_pauses_the_day() {
    let got = eval(vec![trade(wed(), "-1.5")], "-500");
    assert_eq!(got, Some(pause(wed(), &[PauseReason::DailyR], true)));
}

#[test]
fn just_inside_the_daily_limit() {
    assert_eq!(eval(vec![trade(wed(), "-1.5")], "-499.99"), None);
}

#[test]
fn weekly_pauses_to_the_weeks_last_trading_day() {
    let history = vec![
        trade(d(2026, 10, 19), "-2"),
        trade(d(2026, 10, 20), "-1.5"),
        trade(wed(), "-0.5"),
    ];
    let got = eval(history, "0").unwrap();
    assert!(got.reasons.contains(&PauseReason::WeeklyR));
    assert_eq!(got.paused_until, d(2026, 10, 22));
}

#[test]
fn last_weeks_losses_do_not_count() {
    assert_eq!(eval(vec![trade(d(2026, 10, 16), "-4")], "0"), None);
}

#[test]
fn monthly_pauses_to_month_end() {
    let history = [5, 6, 12, 13]
        .into_iter()
        .map(|x| trade(d(2026, 10, x), "-2"))
        .collect();
    assert_eq!(
        eval(history, "0"),
        Some(pause(d(2026, 10, 31), &[PauseReason::MonthlyR], false))
    );
}

#[test]
fn several_limits_report_every_reason_and_the_latest_date_wins() {
    let mut history: Vec<RealisedTrade> = [5, 6, 12]
        .into_iter()
        .map(|x| trade(d(2026, 10, x), "-2"))
        .collect();
    history.push(trade(wed(), "-2"));
    let got = eval(history, "0").unwrap();
    assert_eq!(
        got.reasons,
        vec![PauseReason::DailyR, PauseReason::MonthlyR]
    );
    assert_eq!(got.paused_until, d(2026, 10, 31));
    assert!(got.close_now);
}

#[test]
fn the_ledger_never_pools_sleeves() {
    let e = try_eval(vec![trade_in(wed(), "-1", Sleeve::O1w)], "0", r()).unwrap_err();
    assert!(e.to_string().contains("never pool"));
}

#[test]
fn r_must_be_positive() {
    let e = try_eval(vec![], "0", Decimal::ZERO).unwrap_err();
    assert!(e.to_string().contains("positive"));
}

#[test]
fn calendar_helpers() {
    assert_eq!(month_end(d(2026, 2, 3)), d(2026, 2, 28));
    assert_eq!(last_trading_day_of_week(wed(), &[]), wed());
}

#[test]
fn beyond_the_budget_closes() {
    assert!(trade_breached(dec("1000.01"), r(), &risk()).unwrap());
    assert!(!trade_breached(Decimal::from(1000), r(), &risk()).unwrap());
}

fn limits(capitals: &[i64], daily: i64, monthly: i64) -> Option<BookLimits> {
    let caps: Vec<Decimal> = capitals.iter().map(|c| Decimal::from(*c)).collect();
    book_limits(
        &caps,
        Decimal::from(daily),
        Decimal::from(monthly),
        &risk(),
        &OptionsCeilings::default(),
    )
    .unwrap()
}

fn bl(daily: i64, monthly: i64) -> BookLimits {
    BookLimits {
        daily_loss_inr: Decimal::from(daily),
        monthly_pause_inr: Decimal::from(monthly),
    }
}

#[test]
fn off_until_some_capital() {
    assert_eq!(limits(&[0, 0, 0, 0], 0, 0), None);
    assert_eq!(
        evaluate_book(
            wed(),
            Decimal::from(-1_000_000_000_i64),
            Decimal::ZERO,
            None
        )
        .unwrap(),
        None
    );
}

#[test]
fn derived_from_capital() {
    assert_eq!(
        limits(&[500_000, 500_000, 0, 0], 0, 0),
        Some(bl(15000, 50000))
    );
}

#[test]
fn capped_by_the_ceilings() {
    assert_eq!(limits(&[4_000_000], 0, 0), Some(bl(30000, 75000)));
}

#[test]
fn an_explicit_setting_is_used() {
    assert_eq!(limits(&[1_000_000], 10000, 20000), Some(bl(10000, 20000)));
}

#[test]
fn a_daily_breach_closes_everything_today() {
    let l = bl(15000, 50000);
    let got = evaluate_book(wed(), Decimal::from(-15000), Decimal::ZERO, Some(&l)).unwrap();
    assert_eq!(got, Some(pause(wed(), &[PauseReason::BookDailyInr], true)));
    assert_eq!(
        evaluate_book(wed(), dec("-14999.99"), Decimal::ZERO, Some(&l)).unwrap(),
        None
    );
}

#[test]
fn a_monthly_breach_pauses_to_month_end() {
    let l = bl(15000, 50000);
    let got = evaluate_book(wed(), Decimal::ZERO, Decimal::from(-50000), Some(&l)).unwrap();
    assert_eq!(
        got,
        Some(pause(
            d(2026, 10, 31),
            &[PauseReason::BookMonthlyInr],
            false
        ))
    );
}

#[test]
fn sleeve_paused_today() {
    assert_eq!(
        plan_refusal(Some(wed()), None, wed()),
        Some(REJECTED_PAUSED)
    );
}

#[test]
fn pause_over() {
    assert_eq!(
        plan_refusal(Some(wed() - TimeDelta::days(1)), None, wed()),
        None
    );
}

#[test]
fn book_paused() {
    assert_eq!(
        plan_refusal(None, Some(d(2026, 10, 31)), wed()),
        Some(REJECTED_PAUSED)
    );
}
