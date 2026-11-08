//! NSE sessions outside the weekday 09:15–15:30 rule (Muhurat). Quote and display clocks only:
//! the auto-trading loops and the published daily series do not read this.

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime};

type Session = ((i32, u32, u32), (u32, u32), (u32, u32));

/// (date, continuous-trading start, end), IST. Add each year's Muhurat from the NSE circular.
const SESSIONS: [Session; 1] = [((2026, 11, 8), (14, 20), (15, 30))];

/// Is `now` (IST) inside a dated special session's continuous trading?
#[must_use]
pub fn special_session_open(now: DateTime<FixedOffset>) -> bool {
    SESSIONS.iter().any(|&((y, m, d), (oh, om), (ch, cm))| {
        let window = (
            NaiveDate::from_ymd_opt(y, m, d),
            NaiveTime::from_hms_opt(oh, om, 0),
            NaiveTime::from_hms_opt(ch, cm, 0),
        );
        matches!(window, (Some(day), Some(open), Some(close))
            if now.date_naive() == day && open <= now.time() && now.time() <= close)
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::special_session_open;
    use chrono::DateTime;

    fn at(s: &str) -> bool {
        special_session_open(DateTime::parse_from_rfc3339(s).unwrap())
    }

    #[test]
    fn only_the_dated_window_is_open() {
        assert!(!at("2026-11-08T14:19:59+05:30"));
        assert!(at("2026-11-08T14:20:00+05:30"));
        assert!(at("2026-11-08T15:30:00+05:30"));
        assert!(!at("2026-11-08T15:30:01+05:30"));
        assert!(!at("2026-11-15T14:30:00+05:30"));
    }
}
