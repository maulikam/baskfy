//! `scripts/desk_daily_loop.py`: `daily --quiet --source schedule` at 18:30 IST and `autorun`
//! at 18:50, Mon-Fri, with same-day catch-up after a restart. Collection only, never an order.

use std::collections::HashSet;

use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveTime, TimeZone, Weekday};

use crate::lanes::service_loop::{LoopSeams, child_argv, say, seconds_until};

/// `SETTLE_SECONDS`.
pub const SETTLE_SECONDS: f64 = 90.0;

/// One collection job and its IST clock.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Slot {
    /// `name`.
    pub name: &'static str,
    /// `at` as (hour, minute).
    pub at: (u32, u32),
    /// The service and its arguments.
    pub argv: &'static [&'static str],
}

impl Slot {
    /// `at` as a time.
    #[must_use]
    pub fn time(&self) -> NaiveTime {
        NaiveTime::from_hms_opt(self.at.0, self.at.1, 0).unwrap_or(NaiveTime::MIN)
    }
}

/// `SLOTS`.
pub const SLOTS: [Slot; 2] = [
    Slot {
        name: "daily",
        at: (18, 30),
        argv: &["daily", "--quiet", "--source", "schedule"],
    },
    Slot {
        name: "autorun",
        at: (18, 50),
        argv: &["autorun"],
    },
];

fn weekend(d: NaiveDate) -> bool {
    matches!(d.weekday(), Weekday::Sat | Weekday::Sun)
}

fn at_on(now: DateTime<FixedOffset>, day: NaiveDate, at: NaiveTime) -> DateTime<FixedOffset> {
    now.timezone()
        .from_local_datetime(&day.and_time(at))
        .single()
        .unwrap_or(now)
}

/// `next_weekday_at(now, at)`: the next Mon-Fri `at` strictly after `now`.
#[must_use]
pub fn next_weekday_at(now: DateTime<FixedOffset>, at: NaiveTime) -> DateTime<FixedOffset> {
    let mut run = at_on(now, now.date_naive(), at);
    if run <= now {
        run += Duration::days(1);
    }
    while weekend(run.date_naive()) {
        run += Duration::days(1);
    }
    run
}

/// `due_now(now, at, already_ran=)`.
#[must_use]
pub fn due_now(now: DateTime<FixedOffset>, at: NaiveTime, already_ran: bool) -> bool {
    if already_ran || weekend(now.date_naive()) {
        return false;
    }
    now >= at_on(now, now.date_naive(), at)
}

/// `next_job(now, ran=)`: the next (slot, when); catch-up returns `when == now`, daily first.
#[must_use]
pub fn next_job(
    now: DateTime<FixedOffset>,
    ran: &HashSet<(NaiveDate, &'static str)>,
) -> (Slot, DateTime<FixedOffset>) {
    let mut candidates: Vec<(DateTime<FixedOffset>, NaiveTime, Slot)> = SLOTS
        .iter()
        .map(|slot| {
            let already = ran.contains(&(now.date_naive(), slot.name));
            let when = if due_now(now, slot.time(), already) {
                now
            } else {
                next_weekday_at(now, slot.time())
            };
            (when, slot.time(), *slot)
        })
        .collect();
    candidates.sort_by_key(|c| (c.0, c.1));
    let (when, _, slot) = candidates[0];
    (slot, when)
}

/// `run_forever(...)`; `limit` is a test seam.
pub fn run_forever(seams: &mut dyn LoopSeams, program: &str, limit: Option<usize>) {
    let mut ran: HashSet<(NaiveDate, &'static str)> = HashSet::new();
    let mut fired = 0usize;
    loop {
        let current = seams.now();
        let (slot, when) = next_job(current, &ran);
        let wait = seconds_until(when, seams.now());
        say(&format!(
            "desk-daily: next {} {} IST (wait {wait:.0}s)",
            slot.name,
            when.format("%a %Y-%m-%d %H:%M")
        ));
        if wait > 0.0 {
            seams.sleep(wait);
        }
        let (name, rest) = slot.argv.split_first().unwrap_or((&"daily", &[]));
        let rc = seams.call(&child_argv(program, name, rest));
        say(&format!("desk-daily: {} exited {rc}", slot.name));
        ran.insert((seams.now().date_naive(), slot.name));
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
        runs: Vec<(DateTime<FixedOffset>, Vec<String>)>,
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
            let i = argv.iter().position(|a| a == "service").unwrap();
            self.runs.push((self.now, argv[i + 1..].to_vec()));
            2
        }
    }

    fn ist(d: u32, h: u32, mi: u32) -> DateTime<FixedOffset> {
        crate::clock::ist()
            .with_ymd_and_hms(2026, 9, d, h, mi, 0)
            .single()
            .unwrap()
    }

    #[test]
    fn morning_waits_for_daily_at_eighteen_thirty() {
        let (slot, when) = next_job(ist(21, 9, 0), &HashSet::new());
        assert_eq!((slot.name, when), ("daily", ist(21, 18, 30)));
    }

    #[test]
    fn a_late_restart_catches_up_daily_first_then_autorun() {
        let now = ist(21, 19, 0);
        let (slot, when) = next_job(now, &HashSet::new());
        assert_eq!((slot.name, when), ("daily", now));
        let ran: HashSet<_> = [(now.date_naive(), "daily")].into_iter().collect();
        let (slot, when) = next_job(now, &ran);
        assert_eq!((slot.name, when), ("autorun", now));
    }

    #[test]
    fn a_weekend_never_catches_up_friday() {
        let (slot, when) = next_job(ist(26, 20, 0), &HashSet::new());
        assert_eq!((slot.name, when), ("daily", ist(28, 18, 30)));
    }

    #[test]
    fn the_loop_runs_both_slots_then_the_next_weekday() {
        let mut f = Fake {
            now: ist(25, 12, 0),
            runs: vec![],
        };
        run_forever(&mut f, "desk", Some(3));
        let s = |v: &[&str]| v.iter().map(|x| (*x).to_owned()).collect::<Vec<_>>();
        assert_eq!(
            f.runs,
            vec![
                (ist(25, 18, 30), s(&["daily", "--quiet", "--source", "schedule"])),
                (ist(25, 18, 50), s(&["autorun"])),
                (ist(28, 18, 30), s(&["daily", "--quiet", "--source", "schedule"])),
            ]
        );
    }
}
