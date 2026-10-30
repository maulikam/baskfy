//! Ports `app.fno_clock` (`docs/fno/06` FO7): the FO monitor's day — a tick every 60 s through
//! the session, then the night's marks, retried until the bhavcopy lands.
//!
//! ```text
//!     09:15-15:30  monitor.tick(now) every TICK_SECONDS (plans at 09:20, exits on live marks)
//!     15:30-23:30  monitor.nightly(day) every NIGHT_RETRY_SECONDS until the ingest has landed
//! ```
//!
//! The clock decides nothing — every verdict is the monitor's, every order the gateway's.

use std::future::Future;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, Timelike};

use crate::lanes::options::py;

/// `SESSION_OPEN` (09:15).
pub const SESSION_OPEN: (u32, u32) = (9, 15);
/// `SESSION_CLOSE` (15:30).
pub const SESSION_CLOSE: (u32, u32) = (15, 30);
/// `NIGHT_END` (23:30).
pub const NIGHT_END: (u32, u32) = (23, 30);
/// `TICK_SECONDS`.
pub const TICK_SECONDS: f64 = 60.0;
/// `NIGHT_RETRY_SECONDS`.
pub const NIGHT_RETRY_SECONDS: f64 = 900.0;

fn hm(at: (u32, u32)) -> NaiveTime {
    NaiveTime::from_hms_opt(at.0, at.1, 0).unwrap_or(NaiveTime::MIN)
}

/// What `run_day` needs of the monitor (`Monitor` protocol). Errors are the Python exception's
/// `"{type}: {message}"`.
pub trait Monitor {
    /// `await monitor.tick(now)`.
    fn tick(&mut self, now: DateTime<FixedOffset>) -> impl Future<Output = Result<(), String>>;
    /// `await monitor.nightly(day)` → `night.landed`.
    fn nightly(&mut self, day: NaiveDate) -> impl Future<Output = Result<bool, String>>;
}

/// `DayReport`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DayReport {
    /// Ticks run (including ones that raised).
    pub ticks: u32,
    /// Nights tried.
    pub nights: u32,
    /// The night landed.
    pub marked: bool,
    /// One line per failure (`"tick HH:MM: Type: msg"` / `"night: Type: msg"`).
    pub errors: Vec<String>,
}

/// `log.exception(...)` on the `desk.fno_clock` logger: a failed tick or night is logged, not
/// only counted.
#[allow(clippy::print_stderr)]
fn log_failure(line: &str) {
    eprintln!("ERROR:desk.fno_clock:{line}");
}

/// `run_day(monitor, now=, sleep=)`: one trading day. A tick that fails is logged and the next
/// minute tries again: a monitor that stops on one bad quote leaves an overnight book unwatched.
pub async fn run_day<M, N, S, F>(monitor: &mut M, mut now: N, mut sleep: S) -> DayReport
where
    M: Monitor,
    N: FnMut() -> DateTime<FixedOffset>,
    S: FnMut(f64) -> F,
    F: Future<Output = ()>,
{
    let mut report = DayReport::default();
    let ist = py::ist();
    let day = now().with_timezone(&ist).date_naive();
    loop {
        let current = now().with_timezone(&ist);
        if current.time() >= hm(SESSION_CLOSE) {
            break;
        }
        if current.time() >= hm(SESSION_OPEN) {
            if let Err(e) = monitor.tick(current).await {
                log_failure(&format!("FO tick at {current} failed: {e}"));
                report.errors.push(format!(
                    "tick {:02}:{:02}: {e}",
                    current.hour(),
                    current.minute()
                ));
            }
            report.ticks += 1;
            sleep(TICK_SECONDS).await;
        } else {
            let opening = py::ist_at(day, hm(SESSION_OPEN)).unwrap_or(current);
            let wait = (opening - current).num_microseconds().unwrap_or(0);
            #[allow(clippy::cast_precision_loss)]
            let seconds = wait as f64 / 1e6;
            sleep(seconds.max(1.0)).await;
        }
    }
    while now().with_timezone(&ist).time() < hm(NIGHT_END) {
        report.nights += 1;
        match monitor.nightly(day).await {
            Err(e) => {
                log_failure(&format!("FO night for {day} failed: {e}"));
                report.errors.push(format!("night: {e}"));
            }
            Ok(true) => {
                report.marked = true;
                return report;
            }
            Ok(false) => {}
        }
        sleep(NIGHT_RETRY_SECONDS).await;
    }
    report
}

#[cfg(test)]
mod tests {
    //! `tests/test_fno_monitor_loop.py::TestTheDay`.
    use std::cell::Cell;
    use std::rc::Rc;

    use chrono::{Duration, TimeZone};

    use super::*;

    fn at(h: u32, m: u32) -> DateTime<FixedOffset> {
        py::ist()
            .with_ymd_and_hms(2026, 11, 18, h, m, 0)
            .single()
            .unwrap_or_else(|| unreachable!())
    }

    struct FakeMonitor {
        clock: Rc<Cell<DateTime<FixedOffset>>>,
        ticks: Vec<DateTime<FixedOffset>>,
        nights: Vec<NaiveDate>,
        land_at: NaiveTime,
    }

    impl Monitor for FakeMonitor {
        async fn tick(&mut self, now: DateTime<FixedOffset>) -> Result<(), String> {
            self.ticks.push(now);
            if now.time() == hm((11, 0)) {
                return Err("RuntimeError: fixture: one bad quote".to_owned());
            }
            Ok(())
        }

        async fn nightly(&mut self, day: NaiveDate) -> Result<bool, String> {
            self.nights.push(day);
            Ok(self.clock.get().time() >= self.land_at)
        }
    }

    async fn run(start: DateTime<FixedOffset>, land_at: NaiveTime) -> (DayReport, FakeMonitor) {
        let clock = Rc::new(Cell::new(start));
        let mut monitor = FakeMonitor {
            clock: Rc::clone(&clock),
            ticks: Vec::new(),
            nights: Vec::new(),
            land_at,
        };
        let c1 = Rc::clone(&clock);
        let c2 = Rc::clone(&clock);
        let report = run_day(
            &mut monitor,
            move || c1.get(),
            move |s: f64| {
                #[allow(clippy::cast_possible_truncation)]
                let step = Duration::microseconds((s * 1e6) as i64);
                c2.set(c2.get() + step);
                std::future::ready(())
            },
        )
        .await;
        (report, monitor)
    }

    #[tokio::test]
    async fn a_tick_a_minute_in_session_then_the_night_until_it_lands() {
        let (report, monitor) = run(at(9, 14), hm((19, 0))).await;
        assert_eq!(monitor.ticks.first(), Some(&at(9, 15)));
        assert_eq!(monitor.ticks.last(), Some(&at(15, 29)));
        assert_eq!(monitor.ticks.len(), 375);
        assert!(
            report
                .errors
                .first()
                .is_some_and(|e| e.contains("one bad quote"))
        );
        assert!(report.marked);
        assert_eq!(monitor.nights.last(), Some(&at(9, 0).date_naive()));
        assert!(monitor.clock.get().time() < hm((19, 16)));
    }

    #[tokio::test]
    async fn a_night_that_never_lands_ends_at_2330() {
        let (report, monitor) = run(at(15, 31), hm((23, 59))).await;
        assert!(!report.marked);
        assert!(monitor.ticks.is_empty());
        assert!(monitor.clock.get().time() >= hm(NIGHT_END));
    }
}
