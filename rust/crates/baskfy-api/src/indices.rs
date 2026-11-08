//! The index service: one master of NSE equity indices (`index_master`), their daily bars and
//! stats (`index_bar_daily`, `index_stats_daily`) and constituents (`index_constituents`).
//! Shared by the API's `/indices` routes and the worker's nightly. Kite use is read-only
//! (instruments, quotes, historical) through `baskfy_providers`' rate-limited provider.

pub mod history;
pub mod master;
pub mod nse;
pub mod nse_board;
pub mod quotes;

use chrono::{DateTime, Datelike, FixedOffset, NaiveTime, Weekday};

/// Constituent lists older than this are refetched.
pub const CONSTITUENTS_MAX_AGE_DAYS: i64 = 7;

/// Now, in IST.
#[must_use]
pub fn now_ist() -> DateTime<FixedOffset> {
    crate::entitlements::utc_now().with_timezone(&baskfy_providers::tokens::ist())
}

/// 09:15–15:30 IST on a weekday (holidays are not modelled; a holiday quote is the last close).
#[must_use]
pub fn market_open(now: DateTime<FixedOffset>) -> bool {
    if baskfy_core::special_session::special_session_open(now) {
        return true;
    }
    if matches!(now.weekday(), Weekday::Sat | Weekday::Sun) {
        return false;
    }
    let t = now.time();
    let open = NaiveTime::from_hms_opt(9, 15, 0).unwrap_or(NaiveTime::MIN);
    let close = NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN);
    t >= open && t <= close
}

/// `name` as a URL slug: `"Nifty Oil & Gas"` -> `"nifty-oil-gas"`.
#[must_use]
pub fn slugify(name: &str) -> String {
    let mut out = String::new();
    for c in name.to_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
        } else if !out.ends_with('-') && !out.is_empty() {
            out.push('-');
        }
    }
    out.trim_end_matches('-').to_owned()
}
