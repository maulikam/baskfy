//! Shared plumbing for the container clocks that launch `baskfy-desk service <name>` children.

use chrono::{DateTime, FixedOffset};

pub use crate::lanes::options::monitor_loop::{LoopSeams, ProcessSeams};

/// The flags `main` reads (each with a value), forwarded to a child so it sees the same desk.
pub const MAIN_FLAGS: [&str; 8] = [
    "--host",
    "--port",
    "--root",
    "--env-file",
    "--harness",
    "--harness-clock",
    "--harness-seed",
    "--kite-log",
];

/// This binary's path, as `sys.executable`.
#[must_use]
pub fn program() -> String {
    std::env::current_exe()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| "baskfy-desk".to_owned())
}

fn main_flag_pairs(args: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if MAIN_FLAGS.contains(&a.as_str())
            && let Some(v) = it.next()
        {
            out.push(a.clone());
            out.push(v.clone());
        }
    }
    out
}

/// `[program, <forwarded main flags>, "service", name, extra...]`.
#[must_use]
pub fn child_argv(program: &str, name: &str, extra: &[&str]) -> Vec<String> {
    let args: Vec<String> = std::env::args().collect();
    let mut argv = vec![program.to_owned()];
    argv.extend(main_flag_pairs(args.get(1..).unwrap_or_default()));
    argv.push("service".to_owned());
    argv.push(name.to_owned());
    argv.extend(extra.iter().map(|s| (*s).to_owned()));
    argv
}

/// The arguments after `service <name>`, without `main`'s own flags.
#[must_use]
pub fn service_args() -> Vec<String> {
    let args: Vec<String> = std::env::args().collect();
    let rest = args.get(3..).unwrap_or_default();
    let mut out = Vec::new();
    let mut it = rest.iter();
    while let Some(a) = it.next() {
        if MAIN_FLAGS.contains(&a.as_str()) {
            it.next();
        } else {
            out.push(a.clone());
        }
    }
    out
}

/// `max(0.0, (when - now).total_seconds())`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn seconds_until(when: DateTime<FixedOffset>, now: DateTime<FixedOffset>) -> f64 {
    let left = when - now;
    (left.num_microseconds().unwrap_or(i64::MAX) as f64 / 1e6).max(0.0)
}

/// `print(line, flush=True)`.
#[allow(clippy::print_stdout)]
pub fn say(line: &str) {
    use std::io::Write as _;
    println!("{line}");
    let _ = std::io::stdout().flush();
}

/// Exit with Python's code: `Ok` for 0, else flush and `exit(code)`.
///
/// # Errors
/// Never; a non-zero code exits the process.
pub fn exit_with(code: i32) -> Result<(), String> {
    use std::io::Write as _;
    if code == 0 {
        return Ok(());
    }
    let _ = std::io::stdout().flush();
    let _ = std::io::stderr().flush();
    std::process::exit(code)
}

/// Run a blocking loop off the async runtime.
///
/// # Errors
/// The blocking task panicked.
pub async fn blocking<F: FnOnce() + Send + 'static>(f: F) -> Result<(), String> {
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| e.to_string())
}
