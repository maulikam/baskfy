//! Ports `scripts/options_monitor_loop.py` (OP15): the `options-monitor` container's clock —
//! the monitor each weekday session.
//!
//! ```text
//! 09:14 IST, Mon-Fri    baskfy-desk service options-monitor   (runs until the 15:30 close)
//! ```
//!
//! A container that comes up **inside** the session on a weekday starts the monitor at once (it
//! resumes from `op_position`, OP9). If the monitor exits non-zero inside the session it is
//! started again after [`RETRY_SECONDS`]; a clean exit, or any exit after the close, ends the day.
//!
//! **This loop decides nothing and holds no gateway.** The monitor exits 0 at once unless
//! `BASKFY_OPTIONS_MONITOR_ENABLED` is true, and it raises exit plans, never orders.
//!
//! The one difference from Python is the command: Python runs `[sys.executable, "-m",
//! "app.options_monitor"]`, this runs `[<this binary>, "service", "options-monitor"]`
//! ([`ARGV`]) — the same module's entry point in this binary (R8.20.2).

use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveTime, TimeZone, Weekday};

/// A minute before the open, so the first index tick is subscribed when it prints.
#[must_use]
pub fn run_at() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 14, 0).unwrap_or(NaiveTime::MIN)
}

/// The session's end: a start after it waits for tomorrow.
#[must_use]
pub fn session_end() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// After a crash inside the session, wait this long and start again (restart resume, OP9).
pub const RETRY_SECONDS: i64 = 30;

/// The monitor's arguments after the program (Python's `("-m", "app.options_monitor")`).
pub const ARGV: [&str; 2] = ["service", "options-monitor"];

fn ist() -> FixedOffset {
    crate::clock::ist()
}

fn weekday_le_friday(day: chrono::NaiveDate) -> bool {
    !matches!(day.weekday(), Weekday::Sat | Weekday::Sun)
}

/// `next_run(now, ran_today=)`: the next moment to start — `now` itself inside a weekday's
/// session not yet run. (`now` is aware by its type; Python's `ValueError` for a naive one
/// cannot arise.)
#[must_use]
pub fn next_run(now: DateTime<FixedOffset>, ran_today: bool) -> DateTime<FixedOffset> {
    let local = now.with_timezone(&ist());
    let at = |d: chrono::NaiveDate| {
        ist()
            .from_local_datetime(&d.and_time(run_at()))
            .single()
            .unwrap_or(local)
    };
    let mut run = at(local.date_naive());
    if weekday_le_friday(local.date_naive()) && !ran_today {
        if local < run {
            return run;
        }
        if local.time() < session_end() {
            return local;
        }
    }
    run += Duration::days(1);
    while !weekday_le_friday(run.date_naive()) {
        run += Duration::days(1);
    }
    run
}

/// `in_session(now)`: a weekday between 09:14 and 15:30 IST.
#[must_use]
pub fn in_session(now: DateTime<FixedOffset>) -> bool {
    let local = now.with_timezone(&ist());
    weekday_le_friday(local.date_naive())
        && run_at() <= local.time()
        && local.time() < session_end()
}

/// The loop's seams: `now_fn`, `sleep_fn`, `call_fn`.
pub trait LoopSeams {
    /// `datetime.now(IST)`.
    fn now(&mut self) -> DateTime<FixedOffset>;
    /// `time.sleep(seconds)`.
    fn sleep(&mut self, seconds: f64);
    /// `subprocess.call(argv)`: the exit code.
    fn call(&mut self, argv: &[String]) -> i32;
}

#[allow(clippy::print_stdout)]
fn say(line: &str) {
    println!("{line}");
}

/// `run_forever(now_fn=, sleep_fn=, call_fn=, limit=)`: sleep until the next start, run the
/// monitor, repeat. `limit` is a test seam.
pub fn run_forever(seams: &mut dyn LoopSeams, program: &str, limit: Option<usize>) {
    let mut last: Option<chrono::NaiveDate> = None;
    let mut fired = 0usize;
    loop {
        let current = seams.now();
        let today = current.with_timezone(&ist()).date_naive();
        let when = next_run(current, last == Some(today));
        let left = when - seams.now();
        #[allow(clippy::cast_precision_loss)]
        let wait = (left.num_microseconds().unwrap_or(i64::MAX) as f64 / 1e6).max(0.0);
        say(&format!(
            "options-monitor: next start {} IST (wait {wait:.0}s)",
            when.format("%a %Y-%m-%d %H:%M")
        ));
        if wait > 0.0 {
            seams.sleep(wait);
        }
        let mut argv = vec![program.to_owned()];
        argv.extend(ARGV.iter().map(|a| (*a).to_owned()));
        let rc = seams.call(&argv);
        say(&format!("options-monitor: app.options_monitor exited {rc}"));
        fired += 1;
        // `rc != 0 and in_session(now())`: the clock is read only for a failed run.
        if rc != 0 && in_session(seams.now()) {
            #[allow(clippy::cast_precision_loss)]
            seams.sleep(RETRY_SECONDS as f64); // restart resume: the day is not spent
        } else {
            last = Some(seams.now().with_timezone(&ist()).date_naive());
        }
        if limit.is_some_and(|l| fired >= l) {
            return;
        }
    }
}

/// The real seams: the desk's clock, a blocking sleep, this binary as a child process.
pub struct ProcessSeams {
    /// The desk's clock.
    pub clock: std::sync::Arc<dyn crate::clock::Clock>,
}

impl LoopSeams for ProcessSeams {
    fn now(&mut self) -> DateTime<FixedOffset> {
        self.clock.now_ist()
    }

    fn sleep(&mut self, seconds: f64) {
        std::thread::sleep(std::time::Duration::from_secs_f64(seconds.max(0.0)));
    }

    fn call(&mut self, argv: &[String]) -> i32 {
        let Some((program, args)) = argv.split_first() else {
            return 1;
        };
        std::process::Command::new(program)
            .args(args)
            .status()
            .ok()
            .and_then(|s| s.code())
            .unwrap_or(1)
    }
}
