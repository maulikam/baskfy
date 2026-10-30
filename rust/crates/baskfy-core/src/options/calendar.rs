//! Port of `baskfy_core.options.calendar`: expiries, day roles and lot sizes from the NFO master.

use std::collections::BTreeSet;

use chrono::{Datelike, NaiveDate};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::config::{ExpiryKind, OptionType, Sleeve};

/// One row of `op_contract`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Contract {
    /// The instrument token.
    pub instrument_token: i64,
    /// The trading symbol.
    pub tradingsymbol: String,
    /// The underlying.
    pub underlying: String,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Strike.
    pub strike: Decimal,
    /// Call or put.
    pub option_type: OptionType,
    /// Lot size.
    pub lot_size: i64,
    /// Tick size.
    pub tick_size: Decimal,
}

/// Why a sleeve does or does not trade on a date (`04` §1.2).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RoleReason {
    /// It trades.
    Trades,
    /// Not an NSE trading day.
    NotTradingDay,
    /// An event day.
    EventDay,
    /// Not an expiry.
    NotExpiry,
    /// Not the monthly expiry.
    NotMonthly,
    /// The monthly expiry (O1-W stands aside).
    MonthlyExpiry,
    /// The setup is switched off.
    SetupDisabled,
}

impl RoleReason {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            RoleReason::Trades => "TRADES",
            RoleReason::NotTradingDay => "NOT_TRADING_DAY",
            RoleReason::EventDay => "EVENT_DAY",
            RoleReason::NotExpiry => "NOT_EXPIRY",
            RoleReason::NotMonthly => "NOT_MONTHLY",
            RoleReason::MonthlyExpiry => "MONTHLY_EXPIRY",
            RoleReason::SetupDisabled => "SETUP_DISABLED",
        }
    }
}

/// Whether a sleeve trades and why.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct DayRole {
    /// It trades.
    pub trades: bool,
    /// Why.
    pub reason: RoleReason,
}

/// The distinct expiries of the master's rows for `underlying`, ascending.
pub fn expiries(rows: &[Contract], underlying: &str) -> Vec<NaiveDate> {
    rows.iter()
        .filter(|r| r.underlying == underlying)
        .map(|r| r.expiry)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

/// The last master expiry inside the calendar month, or `None`.
pub fn monthly_expiry(
    rows: &[Contract],
    year: i32,
    month: u32,
    underlying: &str,
) -> Option<NaiveDate> {
    expiries(rows, underlying)
        .into_iter()
        .rfind(|d| d.year() == year && d.month() == month)
}

/// `MONTHLY` iff `expiry` is its month's last master expiry; an error if not an expiry.
pub fn kind(rows: &[Contract], expiry: NaiveDate, underlying: &str) -> CoreResult<ExpiryKind> {
    let listed = expiries(rows, underlying);
    if !listed.contains(&expiry) {
        return Err(CoreError::InvalidInput(format!(
            "{expiry} is not a {underlying} expiry in the master"
        )));
    }
    let last = listed
        .iter()
        .rfind(|d| (d.year(), d.month()) == (expiry.year(), expiry.month()));
    Ok(if last == Some(&expiry) {
        ExpiryKind::Monthly
    } else {
        ExpiryKind::Weekly
    })
}

/// Whether `day` is an event day.
pub fn is_event_day(day: NaiveDate, event_days: &[NaiveDate]) -> bool {
    event_days.contains(&day)
}

/// Whether `sleeve` trades on `day` (`04` §1.2).
pub fn role(
    day: NaiveDate,
    sleeve: Sleeve,
    rows: &[Contract],
    event_days: &[NaiveDate],
    trading_day: bool,
    underlying: &str,
) -> CoreResult<DayRole> {
    let out = |trades, reason| Ok(DayRole { trades, reason });
    if !trading_day {
        return out(false, RoleReason::NotTradingDay);
    }
    if is_event_day(day, event_days) {
        return out(false, RoleReason::EventDay);
    }
    if sleeve == Sleeve::O2 {
        return out(true, RoleReason::Trades);
    }
    if !expiries(rows, underlying).contains(&day) {
        return out(false, RoleReason::NotExpiry);
    }
    let day_kind = kind(rows, day, underlying)?;
    if sleeve == Sleeve::O1m && day_kind != ExpiryKind::Monthly {
        return out(false, RoleReason::NotMonthly);
    }
    if sleeve == Sleeve::O1w && day_kind == ExpiryKind::Monthly {
        return out(false, RoleReason::MonthlyExpiry);
    }
    out(true, RoleReason::Trades)
}

/// The next master expiry strictly after `after` on which an expiry sleeve trades.
pub fn next_session(
    after: NaiveDate,
    sleeve: Sleeve,
    rows: &[Contract],
    event_days: &[NaiveDate],
    underlying: &str,
) -> CoreResult<Option<NaiveDate>> {
    if sleeve == Sleeve::O2 {
        return Err(CoreError::InvalidInput(
            "O2 trades on every non-event trading day; ask the trading calendar".to_owned(),
        ));
    }
    for candidate in expiries(rows, underlying) {
        if candidate <= after {
            continue;
        }
        if role(candidate, sleeve, rows, event_days, true, underlying)?.trades {
            return Ok(Some(candidate));
        }
    }
    Ok(None)
}

fn one_value<T: PartialEq + Copy>(values: impl Iterator<Item = T>) -> Option<T> {
    let mut seen: Vec<T> = Vec::new();
    for v in values {
        if !seen.contains(&v) {
            seen.push(v);
        }
    }
    if seen.len() == 1 { seen.pop() } else { None }
}

/// The master's lot size for `expiry`; `None` when missing, zero or ambiguous.
pub fn lot_size_for(rows: &[Contract], expiry: NaiveDate, underlying: &str) -> Option<i64> {
    one_value(
        rows.iter()
            .filter(|r| r.underlying == underlying && r.expiry == expiry)
            .map(|r| r.lot_size),
    )
    .filter(|s| *s > 0)
}

/// The master's tick size for `expiry`; `None` when missing, zero or ambiguous.
pub fn tick_size_for(rows: &[Contract], expiry: NaiveDate, underlying: &str) -> Option<Decimal> {
    one_value(
        rows.iter()
            .filter(|r| r.underlying == underlying && r.expiry == expiry)
            .map(|r| r.tick_size),
    )
    .filter(|t| *t > Decimal::ZERO)
}

/// The smallest master expiry strictly after `day` (`04` §1.5).
pub fn expiry_for_o2(day: NaiveDate, rows: &[Contract], underlying: &str) -> Option<NaiveDate> {
    expiries(rows, underlying).into_iter().find(|d| *d > day)
}

/// O1 and O3 trade the contract expiring today.
pub fn expiry_for_o1_o3(day: NaiveDate) -> NaiveDate {
    day
}
