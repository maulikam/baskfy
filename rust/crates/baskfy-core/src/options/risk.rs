//! Port of `baskfy_core.options.risk`: per-sleeve limits in R, the per-trade breach, the book's limits (`04` §9).

use chrono::{Datelike, NaiveDate, TimeDelta};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::config::{OptionsCeilings, RiskConfig, Sleeve};
use crate::options::num;

/// Why a sleeve or the book is paused.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PauseReason {
    /// The daily R limit.
    DailyR,
    /// The weekly R limit.
    WeeklyR,
    /// The monthly R limit.
    MonthlyR,
    /// The book's daily rupee limit.
    BookDailyInr,
    /// The book's monthly rupee limit.
    BookMonthlyInr,
}

/// One closed journal row as the ledger reads it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RealisedTrade {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// Net P&L.
    pub net_pnl_inr: Decimal,
    /// Net over R.
    pub r_multiple: Decimal,
}

/// A pause: until when (inclusive), why, and whether to close now.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Pause {
    /// Inclusive.
    pub paused_until: NaiveDate,
    /// Every reason.
    pub reasons: Vec<PauseReason>,
    /// Close the open position at once.
    pub close_now: bool,
}

fn week_bounds(day: NaiveDate) -> (NaiveDate, NaiveDate) {
    let monday = day - TimeDelta::days(i64::from(day.weekday().number_from_monday()) - 1);
    (monday, monday + TimeDelta::days(6))
}

/// The ISO week's last trading day on or after `day`; `day` itself if none.
pub fn last_trading_day_of_week(day: NaiveDate, trading_days: &[NaiveDate]) -> NaiveDate {
    let (_, sunday) = week_bounds(day);
    trading_days
        .iter()
        .filter(|d| day <= **d && **d <= sunday)
        .max()
        .copied()
        .unwrap_or(day)
}

/// The calendar month's last day.
pub fn month_end(day: NaiveDate) -> NaiveDate {
    let (y, m) = if day.month() == 12 {
        (day.year() + 1, 1)
    } else {
        (day.year(), day.month() + 1)
    };
    NaiveDate::from_ymd_opt(y, m, 1)
        .and_then(|first| first.pred_opt())
        .unwrap_or(day)
}

fn pause(found: &[(PauseReason, NaiveDate)], closing: PauseReason) -> Option<Pause> {
    let until = found.iter().map(|(_, d)| *d).max()?;
    Some(Pause {
        paused_until: until,
        reasons: found.iter().map(|(r, _)| *r).collect(),
        close_now: found.iter().any(|(r, _)| *r == closing),
    })
}

/// What `evaluate_sleeve` reads besides the config.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SleeveDay {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// Today.
    pub today: NaiveDate,
    /// The sleeve's closed rows.
    pub history: Vec<RealisedTrade>,
    /// Today's marked P&L on the open position.
    pub today_marked_inr: Decimal,
    /// Today's R.
    pub r_today_inr: Decimal,
    /// The trading calendar.
    pub trading_days: Vec<NaiveDate>,
}

/// `04` §9.1 for one sleeve: daily, weekly and monthly limits in R.
pub fn evaluate_sleeve(day: &SleeveDay, config: &RiskConfig) -> CoreResult<Option<Pause>> {
    let today = day.today;
    if day.history.iter().any(|r| r.sleeve != day.sleeve) {
        return Err(CoreError::InvalidInput(
            "evaluate_sleeve reads one sleeve's rows; never pool sleeves".to_owned(),
        ));
    }
    if day.r_today_inr <= Decimal::ZERO {
        return Err(CoreError::InvalidInput("R must be positive".to_owned()));
    }
    let mut found = Vec::new();
    let realised_today = num::sum(
        day.history
            .iter()
            .filter(|r| r.trade_date == today)
            .map(|r| r.net_pnl_inr),
    )?;
    let today_r = num::div(
        num::add(realised_today, day.today_marked_inr)?,
        day.r_today_inr,
    )?;
    if today_r <= num::neg(config.daily_loss_r)? {
        found.push((PauseReason::DailyR, today));
    }
    let (monday, sunday) = week_bounds(today);
    let week_r = num::sum(
        day.history
            .iter()
            .filter(|r| monday <= r.trade_date && r.trade_date <= sunday)
            .map(|r| r.r_multiple),
    )?;
    if week_r <= num::neg(config.weekly_loss_r)? {
        found.push((
            PauseReason::WeeklyR,
            last_trading_day_of_week(today, &day.trading_days),
        ));
    }
    let month_r = num::sum(
        day.history
            .iter()
            .filter(|r| {
                (r.trade_date.year(), r.trade_date.month()) == (today.year(), today.month())
                    && r.trade_date <= today
            })
            .map(|r| r.r_multiple),
    )?;
    if month_r <= num::neg(config.monthly_loss_r)? {
        found.push((PauseReason::MonthlyR, month_end(today)));
    }
    Ok(pause(&found, PauseReason::DailyR))
}

/// `04` §9.2: a marked loss beyond `risk_budget * budget_breach_frac` closes the position.
pub fn trade_breached(
    marked_loss_inr: Decimal,
    risk_budget_inr: Decimal,
    config: &RiskConfig,
) -> CoreResult<bool> {
    Ok(marked_loss_inr > num::mul(risk_budget_inr, config.budget_breach_frac)?)
}

/// The book's rupee limits.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BookLimits {
    /// Daily loss limit.
    pub daily_loss_inr: Decimal,
    /// Monthly pause limit.
    pub monthly_pause_inr: Decimal,
}

/// `04` §9.3 — `None` until some sleeve has capital; a 0 setting is derived; each capped.
pub fn book_limits(
    sleeve_capitals_inr: &[Decimal],
    daily_loss_limit_inr: Decimal,
    monthly_pause_inr: Decimal,
    config: &RiskConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<Option<BookLimits>> {
    let total = num::sum(sleeve_capitals_inr.iter().copied())?;
    if total <= Decimal::ZERO {
        return Ok(None);
    }
    let derived = |pct: Decimal| -> CoreResult<Decimal> {
        num::div(num::mul(total, pct)?, Decimal::ONE_HUNDRED)
    };
    let daily = if daily_loss_limit_inr > Decimal::ZERO {
        daily_loss_limit_inr
    } else {
        derived(config.book_daily_loss_pct)?
    };
    let monthly = if monthly_pause_inr > Decimal::ZERO {
        monthly_pause_inr
    } else {
        derived(config.book_monthly_loss_pct)?
    };
    Ok(Some(BookLimits {
        daily_loss_inr: num::min(daily, ceilings.book_daily_loss_inr_max),
        monthly_pause_inr: num::min(monthly, ceilings.book_monthly_loss_inr_max),
    }))
}

/// The book's daily and monthly breaches (`04` §9.3).
pub fn evaluate_book(
    today: NaiveDate,
    today_net_inr: Decimal,
    month_realised_inr: Decimal,
    limits: Option<&BookLimits>,
) -> CoreResult<Option<Pause>> {
    let Some(limits) = limits else {
        return Ok(None);
    };
    let mut found = Vec::new();
    if today_net_inr <= num::neg(limits.daily_loss_inr)? {
        found.push((PauseReason::BookDailyInr, today));
    }
    if month_realised_inr <= num::neg(limits.monthly_pause_inr)? {
        found.push((PauseReason::BookMonthlyInr, month_end(today)));
    }
    Ok(pause(&found, PauseReason::BookDailyInr))
}

/// Paused on `today`.
pub fn is_paused(paused_until: Option<NaiveDate>, today: NaiveDate) -> bool {
    paused_until.is_some_and(|u| today <= u)
}

/// The refusal code while paused.
pub const REJECTED_PAUSED: &str = "REJECTED_PAUSED";

/// `04` §9.4: `REJECTED_PAUSED` while the sleeve or the book is paused.
pub fn plan_refusal(
    sleeve_paused_until: Option<NaiveDate>,
    book_paused_until: Option<NaiveDate>,
    today: NaiveDate,
) -> Option<&'static str> {
    if is_paused(sleeve_paused_until, today) || is_paused(book_paused_until, today) {
        Some(REJECTED_PAUSED)
    } else {
        None
    }
}
