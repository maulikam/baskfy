//! `scripts/twt_auto_loop.py` (TW17): run `twt-auto` at 09:15:10 IST on a weekday, or at once
//! inside 09:15-09:35; the service itself holds every flag (default off).

use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveTime, TimeZone, Weekday};

use crate::lanes::service_loop::{LoopSeams, child_argv, say, seconds_until};

/// `SETTLE_SECONDS`.
pub const SETTLE_SECONDS: f64 = 90.0;

/// `RUN_AT`.
#[must_use]
pub fn run_at() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 10).unwrap_or(NaiveTime::MIN)
}

/// `JOIN_UNTIL`.
#[must_use]
pub fn join_until() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 35, 0).unwrap_or(NaiveTime::MIN)
}

fn weekday(d: NaiveDate) -> bool {
    !matches!(d.weekday(), Weekday::Sat | Weekday::Sun)
}

/// `next_run(now, ran_today=)`.
#[must_use]
pub fn next_run(now: DateTime<FixedOffset>, ran_today: bool) -> DateTime<FixedOffset> {
    let ist = crate::clock::ist();
    let local = now.with_timezone(&ist);
    let mut run = ist
        .from_local_datetime(&local.date_naive().and_time(run_at()))
        .single()
        .unwrap_or(local);
    if weekday(local.date_naive()) && !ran_today {
        if local < run {
            return run;
        }
        if local.time() < join_until() {
            return local;
        }
    }
    run += Duration::days(1);
    while !weekday(run.date_naive()) {
        run += Duration::days(1);
    }
    run
}

/// `run_forever(...)`; `limit` is a test seam.
pub fn run_forever(seams: &mut dyn LoopSeams, program: &str, limit: Option<usize>) {
    let ist = crate::clock::ist();
    let mut last: Option<NaiveDate> = None;
    let mut fired = 0usize;
    loop {
        let current = seams.now();
        let when = next_run(current, last == Some(current.with_timezone(&ist).date_naive()));
        let wait = seconds_until(when, seams.now());
        say(&format!(
            "twt-auto: next run {} IST (wait {wait:.0}s)",
            when.format("%a %Y-%m-%d %H:%M:%S")
        ));
        if wait > 0.0 {
            seams.sleep(wait);
        }
        let rc = seams.call(&child_argv(program, "twt-auto", &[]));
        say(&format!("twt-auto: app.twt_auto exited {rc}"));
        last = Some(seams.now().with_timezone(&ist).date_naive());
        seams.sleep(SETTLE_SECONDS);
        fired += 1;
        if limit.is_some_and(|l| fired >= l) {
            return;
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::*;

    struct Fake {
        now: DateTime<FixedOffset>,
        runs: Vec<DateTime<FixedOffset>>,
    }

    impl LoopSeams for Fake {
        fn now(&mut self) -> DateTime<FixedOffset> {
            self.now
        }
        #[allow(clippy::cast_possible_truncation)]
        fn sleep(&mut self, seconds: f64) {
            self.now += Duration::milliseconds((seconds * 1000.0) as i64);
        }
        fn call(&mut self, argv: &[String]) -> i32 {
            assert_eq!(&argv[argv.len() - 2..], ["service", "twt-auto"]);
            self.runs.push(self.now);
            0
        }
    }

    fn ist(d: u32, h: u32, mi: u32, s: u32) -> DateTime<FixedOffset> {
        crate::clock::ist()
            .with_ymd_and_hms(2026, 9, d, h, mi, s)
            .single()
            .unwrap()
    }

    #[test]
    fn before_the_open_waits_for_nine_fifteen_ten() {
        assert_eq!(next_run(ist(21, 8, 0, 0), false), ist(21, 9, 15, 10));
    }

    #[test]
    fn inside_the_window_runs_at_once() {
        assert_eq!(next_run(ist(21, 9, 20, 0), false), ist(21, 9, 20, 0));
    }

    #[test]
    fn after_the_window_waits_for_tomorrow() {
        assert_eq!(next_run(ist(21, 9, 35, 0), false), ist(22, 9, 15, 10));
    }

    #[test]
    fn friday_rolls_to_monday_and_ran_today_skips() {
        assert_eq!(next_run(ist(25, 9, 15, 10), true), ist(28, 9, 15, 10));
        assert_eq!(next_run(ist(26, 9, 0, 0), false), ist(28, 9, 15, 10));
    }

    #[test]
    fn the_loop_runs_once_a_weekday() {
        let mut f = Fake {
            now: ist(24, 9, 20, 0),
            runs: vec![],
        };
        run_forever(&mut f, "desk", Some(3));
        assert_eq!(
            f.runs,
            vec![ist(24, 9, 20, 0), ist(25, 9, 15, 10), ist(28, 9, 15, 10)]
        );
    }
}
