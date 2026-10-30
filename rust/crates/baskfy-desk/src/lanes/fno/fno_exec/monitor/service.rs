//! `python -m app.fno_monitor` (`main()`) and `python -m scripts.fno_monitor_loop` as desk
//! services: `baskfy-desk service fno-monitor` and `baskfy-desk service fno-monitor-loop`.
//!
//! **The flag.** With `BASKFY_FNO_MONITOR_ENABLED` false (its default) [`run`] returns `Ok`
//! before opening the database, Kite or a gateway (Python's `return 0`). With it on it builds
//! the monitor on the desk's Kite reads, the paper-pinned FO gateway per sleeve (FO7.1:
//! `dry_run` true in every row) and M.5's `f3_auto_exit_enabled` (default false, exits only),
//! and runs the day ([`crate::lanes::fno::clock::run_day`]). Nothing here confirms an entry.
//!
//! **The loop** ([`next_run`], [`in_session`], [`run_forever`]) decides nothing and holds no
//! gateway: it starts the monitor at 09:14 on a weekday (at once inside the day's window, which
//! runs to 23:30 for the night's marks) and starts it again after a crash inside the window.

use std::future::Future;
use std::sync::Arc;

use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveTime, TimeZone, Weekday};

use super::super::execute::{ExecError, fo_gateway};
use super::super::store::FoStore;
use super::engine::{FnoMonitor, log_error};
use super::seams::{
    DeskMarginReads, KiteFoQuotes, KiteIndexQuotes, KiteMargins, PgKiteInstruments, PgMarket,
};
use crate::app::AppState;
use crate::lanes::fno::clock::{Monitor, run_day};
use crate::storage::Conn;

#[allow(clippy::print_stderr)]
fn log_info(message: &str) {
    eprintln!("INFO:desk.fno_monitor:{message}");
}

fn text(e: ExecError) -> String {
    match e {
        ExecError::Other(m) => m,
        ExecError::Refused(r) => format!("Refused: {}", r.message),
    }
}

/// The day's runner over one connection (`with connect() as conn: ... run_day(monitor)`).
pub struct DayRunner {
    /// The engine.
    pub monitor: FnoMonitor,
    /// The store's connection.
    pub conn: Conn,
    /// `BASKFY_SOLE_USER_ID`.
    pub user_id: i64,
}

impl Monitor for DayRunner {
    async fn tick(&mut self, now: DateTime<FixedOffset>) -> Result<(), String> {
        let mut store = FoStore::new(&mut self.conn, self.user_id);
        self.monitor
            .tick(&mut store, now)
            .await
            .map(|_| ())
            .map_err(text)
    }

    async fn nightly(&mut self, day: NaiveDate) -> Result<bool, String> {
        let mut store = FoStore::new(&mut self.conn, self.user_id);
        self.monitor
            .nightly(&mut store, day)
            .await
            .map(|n| n.landed)
            .map_err(text)
    }
}

/// `main()`: the day's process. `Ok` and nothing touched with the flag off.
///
/// # Errors
/// `BASKFY_SOLE_USER_ID` unset (Python's exit 2), the desk's Kite or database refusing.
pub async fn run(state: &AppState) -> Result<(), String> {
    let settings = state.settings_snapshot();
    if !settings.fno_monitor_enabled {
        log_info("BASKFY_FNO_MONITOR_ENABLED is false; exiting without building the monitor");
        return Ok(());
    }
    // `int(os.environ.get("BASKFY_SOLE_USER_ID", "0") or 0)`: the environment, not a default.
    let raw = std::env::var("BASKFY_SOLE_USER_ID").unwrap_or_default();
    let user_id = if raw.is_empty() {
        0
    } else {
        raw.trim()
            .parse::<i64>()
            .map_err(|_| format!("ValueError: invalid literal for int() with base 10: '{raw}'"))?
    };
    if user_id == 0 {
        log_error("BASKFY_SOLE_USER_ID is not set; fo_ rows are keyed by user");
        return Err("BASKFY_SOLE_USER_ID is not set; fo_ rows are keyed by user".to_owned());
    }
    // `_main.kite()` then `_main.gateway()` (builds the shared risk manager).
    let desk = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
        .ok_or_else(|| "RuntimeError: the desk's gateway is not built".to_owned())?;
    let kite = desk.kite();
    kite.ready()
        .map_err(|e| format!("{}: {}", e.python_type, e.message))?;
    desk.risk().await.map_err(|e| e.to_string())?;
    let open = || async { state.db.connect().await.map_err(|e| e.to_string()) };
    let conn = open().await?;
    let gateway_state = state.clone();
    let flags = state.settings.clone();
    let mut monitor = FnoMonitor::new(
        &settings,
        Arc::new(move |sleeve| fo_gateway(&gateway_state, sleeve)),
        Arc::new(KiteFoQuotes(kite.clone())),
        Arc::new(PgKiteInstruments::new(Some(open().await?), kite.clone())),
        Arc::new(KiteMargins(Arc::new(DeskMarginReads {
            kite: kite.clone(),
            kc: state.kite.clone(),
        }))),
        Arc::new(PgMarket::new(open().await?)),
    );
    monitor.index_quotes = Arc::new(KiteIndexQuotes(kite));
    // M.5: `exit_by_monitor=lambda _sleeve: f3_auto_exit_enabled()`, read at each exit.
    monitor.exit_by_monitor = Arc::new(move |_sleeve| {
        let s = match flags.read() {
            Ok(s) => s.clone(),
            Err(p) => p.into_inner().clone(),
        };
        crate::lanes::fno::gates::f3_auto_exit_enabled(&s)
    });
    let mut runner = DayRunner {
        monitor,
        conn,
        user_id,
    };
    let clock = state.clock.clone();
    let report = run_day(
        &mut runner,
        move || clock.now_ist(),
        |seconds| tokio::time::sleep(std::time::Duration::from_secs_f64(seconds.max(0.0))),
    )
    .await;
    log_info(&format!("FO monitor done: {report:?}"));
    Ok(())
}

// --- the loop (`scripts.fno_monitor_loop`) ------------------------------------------------------

/// `RUN_AT`: a minute before the open.
#[must_use]
pub fn run_at() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 14, 0).unwrap_or(NaiveTime::MIN)
}

/// `SESSION_END`: the night's end; a start after it waits for tomorrow.
#[must_use]
pub fn session_end() -> NaiveTime {
    NaiveTime::from_hms_opt(23, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// `RETRY_SECONDS`.
pub const RETRY_SECONDS: f64 = 30.0;

fn weekday(d: NaiveDate) -> bool {
    !matches!(d.weekday(), Weekday::Sat | Weekday::Sun)
}

/// `next_run(now, ran_today=)`: the next moment to start — `now` itself inside a weekday's
/// window not yet run.
#[must_use]
pub fn next_run(now: DateTime<FixedOffset>, ran_today: bool) -> DateTime<FixedOffset> {
    let ist = crate::clock::ist();
    let local = now.with_timezone(&ist);
    let at = |d: NaiveDate| {
        ist.from_local_datetime(&d.and_time(run_at()))
            .single()
            .unwrap_or(local)
    };
    let mut run = at(local.date_naive());
    if weekday(local.date_naive()) && !ran_today {
        if local < run {
            return run;
        }
        if local.time() < session_end() {
            return local;
        }
    }
    run += Duration::days(1);
    while !weekday(run.date_naive()) {
        run += Duration::days(1);
    }
    run
}

/// `in_session(now)`: a weekday between 09:14 and 23:30.
#[must_use]
pub fn in_session(now: DateTime<FixedOffset>) -> bool {
    let local = now.with_timezone(&crate::clock::ist());
    weekday(local.date_naive()) && run_at() <= local.time() && local.time() < session_end()
}

/// `ARGV`: what the loop starts.
pub const ARGV: [&str; 2] = ["service", "fno-monitor"];

/// `run_forever(now_fn=, sleep_fn=, call_fn=, limit=)`: sleep until the next start, run the
/// monitor, repeat; a non-zero exit inside the window is retried after [`RETRY_SECONDS`].
pub async fn run_forever<N, S, SF, C, CF>(mut now: N, mut sleep: S, mut call: C, limit: Option<u32>)
where
    N: FnMut() -> DateTime<FixedOffset>,
    S: FnMut(f64) -> SF,
    SF: Future<Output = ()>,
    C: FnMut(Vec<String>) -> CF,
    CF: Future<Output = i32>,
{
    let ist = crate::clock::ist();
    let mut last: Option<NaiveDate> = None;
    let mut fired = 0u32;
    loop {
        let current = now();
        let when = next_run(
            current,
            last == Some(current.with_timezone(&ist).date_naive()),
        );
        #[allow(clippy::cast_precision_loss)]
        let wait = ((when - now()).num_microseconds().unwrap_or(0) as f64 / 1e6).max(0.0);
        log_info(&format!(
            "fno-monitor: next start {} IST (wait {wait:.0}s)",
            when.format("%a %Y-%m-%d %H:%M")
        ));
        if wait > 0.0 {
            sleep(wait).await;
        }
        let rc = call(ARGV.iter().map(|s| (*s).to_owned()).collect()).await;
        log_info(&format!("fno-monitor: app.fno_monitor exited {rc}"));
        fired += 1;
        if rc != 0 && in_session(now()) {
            sleep(RETRY_SECONDS).await; // restart resume: the day is not spent
        } else {
            last = Some(now().with_timezone(&ist).date_naive());
        }
        if limit.is_some_and(|l| fired >= l) {
            return;
        }
    }
}

/// `main()` of the loop: the desk binary starts itself as `service fno-monitor`, with the same
/// `--root`/`--env-file` it was given.
///
/// # Errors
/// Never in practice (the loop runs forever).
pub async fn run_loop() -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let args: Vec<String> = std::env::args().collect();
    let passed: Vec<String> = args.iter().skip(3).cloned().collect();
    let clock = crate::clock::SystemClock;
    run_forever(
        || crate::clock::Clock::now_ist(&clock),
        |s| tokio::time::sleep(std::time::Duration::from_secs_f64(s)),
        |argv| {
            let exe = exe.clone();
            let passed = passed.clone();
            async move {
                // `subprocess.call(argv)`: blocking, off the runtime's workers.
                let status = tokio::task::spawn_blocking(move || {
                    std::process::Command::new(&exe)
                        .args(&argv)
                        .args(&passed)
                        .status()
                })
                .await;
                match status {
                    Ok(Ok(s)) => s.code().unwrap_or(1),
                    _ => 1,
                }
            }
        },
        None,
    )
    .await;
    Ok(())
}
