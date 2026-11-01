//! `scripts/twt_auto_loop.py`: the `twt-auto` container's clock — one run of the `twt-auto`
//! service (`python -m app.twt_auto`; here `baskfy-desk service twt-auto`) once a weekday, at the
//! open. TW17 (Maulik, in session, 21 Sep 2026).
//!
//! ```text
//! 09:15:10 IST, Mon-Fri    baskfy-desk service twt-auto
//! ```
//!
//! `twt-morning` builds the MORNING plan at 09:05 and it expires thirty minutes later, so the
//! window in which a run can do anything is 09:15–09:35. A start **inside** that window on a
//! weekday runs at once rather than forfeiting the day; one after it waits for tomorrow.
//!
//! **This loop decides nothing.** The service returns without reading a plan unless all three
//! flags (`DRY_RUN` off, `BASKFY_TWT_EXECUTION_ENABLED`, `BASKFY_TWT_AUTO_EXECUTE`) are on, so the
//! loop runs it every weekday regardless and the service is where the answer lives.
//!
//! Ported as the pure schedule plus [`run_forever`] over its seams (Python's `now_fn`,
//! `sleep_fn`, `call_fn`, `limit`); nothing in the desk binary starts this loop yet — the
//! container wiring is cutover's (DECISIONS-RS R8.TX2).

use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveTime, TimeZone};

/// `RUN_AT`: ten seconds past the open — the first prints are in and a MARKET order is an
/// open-session order.
#[must_use]
pub fn run_at() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 10).unwrap_or_default()
}

/// `JOIN_UNTIL`: the end of the window a late start may still join (the 09:05 plan's expiry).
#[must_use]
pub fn join_until() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 35, 0).unwrap_or_default()
}

/// `SETTLE_SECONDS`: after a run, sit still long enough that [`next_run`] cannot pick today
/// again.
pub const SETTLE_SECONDS: f64 = 90.0;

/// `ARGV`: what one run is (`("-m", "app.twt_auto")` in Python).
pub const ARGV: [&str; 2] = ["service", "twt-auto"];

fn is_weekday(t: &DateTime<FixedOffset>) -> bool {
    t.weekday().num_days_from_monday() <= 4
}

/// `next_run(now, ran_today=...)`: the next moment to run — `now` itself when a weekday's window
/// is open and unspent. `now` is aware by construction (Python's `ValueError` for a naive one
/// cannot arise).
#[must_use]
pub fn next_run(now: DateTime<FixedOffset>, ran_today: bool) -> DateTime<FixedOffset> {
    let ist = crate::clock::ist();
    let local = now.with_timezone(&ist);
    let mut run = ist
        .from_local_datetime(&local.date_naive().and_time(run_at()))
        .single()
        .unwrap_or(local);
    if is_weekday(&local) && !ran_today {
        if local < run {
            return run;
        }
        if local.time() < join_until() {
            return local;
        }
    }
    run += Duration::days(1);
    while !is_weekday(&run) {
        run += Duration::days(1);
    }
    run
}

/// `run_forever(now_fn=..., sleep_fn=..., call_fn=..., limit=...)`: sleep until the next run,
/// run it, repeat. `limit` is a test seam. Returns the lines it printed, in order (Python prints
/// them with `flush=True`; the caller here prints them).
pub fn run_forever(
    now_fn: &mut dyn FnMut() -> DateTime<FixedOffset>,
    sleep_fn: &mut dyn FnMut(f64),
    call_fn: &mut dyn FnMut(&[&str]) -> i32,
    limit: Option<usize>,
    print: &mut dyn FnMut(String),
) {
    let ist = crate::clock::ist();
    let mut last: Option<chrono::NaiveDate> = None;
    let mut fired = 0usize;
    loop {
        let current = now_fn();
        let when = next_run(
            current,
            last == Some(current.with_timezone(&ist).date_naive()),
        );
        let wait = micros(when - now_fn()).max(0.0);
        print(format!(
            "twt-auto: next run {} IST (wait {}s)",
            when.format("%a %Y-%m-%d %H:%M:%S"),
            py_format0(wait)
        ));
        if wait != 0.0 {
            sleep_fn(wait);
        }
        let rc = call_fn(&ARGV);
        print(format!("twt-auto: app.twt_auto exited {rc}"));
        last = Some(now_fn().with_timezone(&ist).date_naive());
        sleep_fn(SETTLE_SECONDS);
        fired += 1;
        if limit.is_some_and(|l| fired >= l) {
            return;
        }
    }
}

/// `timedelta.total_seconds()`.
#[allow(clippy::cast_precision_loss)]
fn micros(d: Duration) -> f64 {
    d.num_microseconds().map_or(0.0, |us| us as f64 / 1e6)
}

/// `f"{wait:.0f}"`: both formatters round the exact binary value, ties to even.
fn py_format0(x: f64) -> String {
    format!("{x:.0}")
}
