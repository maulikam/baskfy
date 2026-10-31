//! `Dockerfile.desk`'s `swing-monitor-loop`: 09:14 IST on weekdays, or at once mid-session with
//! the flag on, run the swing monitor; then 90 s settle and repeat.

use chrono::NaiveTime;

use crate::lanes::service_loop::{LoopSeams, child_argv, say, seconds_until};
use crate::lanes::swing::swing_monitor::service::next_run;

/// `time.sleep(90)` after a run.
pub const SETTLE_SECONDS: f64 = 90.0;

/// `CLOSE`: `DEFAULT_SWING_CONFIG.opening_range.monitor_close`.
#[must_use]
pub fn close() -> NaiveTime {
    let (h, m) = baskfy_core::swing::setups::SwingConfig::default()
        .opening_range
        .monitor_close;
    u32::try_from(h)
        .ok()
        .zip(u32::try_from(m).ok())
        .and_then(|(h, m)| NaiveTime::from_hms_opt(h, m, 0))
        .unwrap_or_else(|| NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN))
}

/// `os.environ.get("BASKFY_SWING_MONITOR_ENABLED", "false").lower() == "true"`.
#[must_use]
pub fn env_flag() -> bool {
    std::env::var("BASKFY_SWING_MONITOR_ENABLED")
        .is_ok_and(|v| v.to_lowercase() == "true")
}

/// The loop; `flag` is read every pass, `limit` is a test seam.
pub fn run_forever(
    seams: &mut dyn LoopSeams,
    program: &str,
    flag: &dyn Fn() -> bool,
    limit: Option<usize>,
) {
    let mut fired = 0usize;
    loop {
        let now = seams.now();
        let on = flag();
        let run = next_run(now, on, close());
        say(&format!(
            "swing-monitor: flag {}; next run {} IST",
            if on { "ON" } else { "off" },
            run.format("%a %Y-%m-%d %H:%M")
        ));
        let wait = seconds_until(run, seams.now()).max(1.0);
        seams.sleep(wait);
        let rc = seams.call(&child_argv(program, "swing-monitor", &[]));
        say(&format!("swing-monitor: app.swing_monitor exited {rc}"));
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
    use chrono::{DateTime, Duration, FixedOffset, TimeZone};

    struct Fake {
        now: DateTime<FixedOffset>,
        sleeps: Vec<f64>,
        calls: Vec<Vec<String>>,
    }

    impl LoopSeams for Fake {
        fn now(&mut self) -> DateTime<FixedOffset> {
            self.now
        }
        #[allow(clippy::cast_possible_truncation)]
        fn sleep(&mut self, seconds: f64) {
            self.sleeps.push(seconds);
            self.now += Duration::milliseconds((seconds * 1000.0) as i64);
        }
        fn call(&mut self, argv: &[String]) -> i32 {
            self.calls.push(argv.to_vec());
            0
        }
    }

    fn ist(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> DateTime<FixedOffset> {
        crate::clock::ist()
            .with_ymd_and_hms(y, mo, d, h, mi, 0)
            .single()
            .unwrap()
    }

    fn fake(now: DateTime<FixedOffset>) -> Fake {
        Fake {
            now,
            sleeps: vec![],
            calls: vec![],
        }
    }

    #[test]
    fn mid_session_with_flag_on_runs_after_the_one_second_floor() {
        let mut f = fake(ist(2026, 9, 11, 13, 9));
        run_forever(&mut f, "desk", &|| true, Some(1));
        assert_eq!(f.sleeps, vec![1.0, 90.0]);
        let argv = f.calls.first().unwrap();
        assert_eq!(&argv[argv.len() - 2..], ["service", "swing-monitor"]);
    }

    #[test]
    fn flag_off_waits_for_the_next_weekday_open() {
        let mut f = fake(ist(2026, 9, 11, 13, 9));
        run_forever(&mut f, "desk", &|| false, Some(1));
        let want = (ist(2026, 9, 14, 9, 14) - ist(2026, 9, 11, 13, 9)).num_seconds();
        #[allow(clippy::cast_precision_loss)]
        let want = want as f64;
        assert!((f.sleeps[0] - want).abs() < 1e-6);
        assert_eq!(f.now, ist(2026, 9, 14, 9, 15) + Duration::seconds(30));
    }

    #[test]
    fn before_the_open_waits_until_nine_fourteen() {
        let mut f = fake(ist(2026, 9, 11, 6, 0));
        run_forever(&mut f, "desk", &|| true, Some(1));
        assert!((f.sleeps[0] - 11_640.0).abs() < 1e-6);
    }
}
