//! What a task runs with: the database, the settings, the provider stack and the **clock**.
//!
//! Python's tasks read `dt.datetime.now(tz=IST)` wherever they need "now" — a nightly guard, a
//! session window, a token's issue date — so they are time-of-day sensitive, and grading one needs
//! the same moment on both sides. Every Rust task reads time through [`TaskContext::now_ist`] /
//! [`TaskContext::now_utc`] / [`TaskContext::clock`], never the system clock: `--now` pins the
//! start (IST wall clock), and the clock then **advances** with real elapsed time (monotonic) in
//! whole seconds, as the Python side's injected clock does (`tools/parity/task_py.py`). A frozen clock would hang
//! any task that waits for a moment to pass (`swing.timing_probe` polls until 09:15).
//!
//! What cannot be injected, on either side: Postgres's own `now()` (column defaults, SQL that
//! compares with `now()`); the harness masks those columns as `<before>`/`<during>` (R8.2).

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use baskfy_api::Settings;
use baskfy_db::WriteDb;
use baskfy_providers::clock::{Clock, SystemClock};
use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::factory::{RedisSource, build_provider_stack};
use baskfy_providers::replay::ReplayProvider;
use baskfy_providers::settings::ProviderSettings;
use chrono::{DateTime, Duration, NaiveDate, NaiveDateTime, Utc};

use crate::deps::PipelineDependencies;
use crate::error::WorkerError;
use crate::settings::WorkerSettings;
use crate::steps::log_warning;

/// IST's offset from UTC, minutes.
pub const IST_OFFSET_MINUTES: i64 = 330;

/// The task clock: the injected start (or the system's), advanced by real elapsed time.
#[derive(Debug, Clone)]
pub struct TaskClock {
    start_utc: Option<DateTime<Utc>>,
    started: Instant,
}

impl TaskClock {
    /// The system clock.
    #[must_use]
    pub fn system() -> Self {
        Self {
            start_utc: None,
            started: Instant::now(),
        }
    }

    /// A clock that reads `start_ist` (IST wall clock) now, and advances from there in whole
    /// seconds of real elapsed time.
    #[must_use]
    pub fn starting_at_ist(start_ist: NaiveDateTime) -> Self {
        Self {
            start_utc: Some((start_ist - Duration::minutes(IST_OFFSET_MINUTES)).and_utc()),
            started: Instant::now(),
        }
    }

    /// Was the clock injected?
    #[must_use]
    pub const fn is_injected(&self) -> bool {
        self.start_utc.is_some()
    }
}

impl Clock for TaskClock {
    fn monotonic(&self) -> f64 {
        SystemClock.monotonic()
    }

    fn now(&self) -> DateTime<Utc> {
        match self.start_utc {
            None => SystemClock.now(),
            Some(start) => {
                // Whole seconds, as the Python side's injected clock: a read in the task's first
                // second is exactly the case's moment on both sides (R8.2).
                let whole = i64::try_from(self.started.elapsed().as_secs()).unwrap_or(i64::MAX);
                start + Duration::seconds(whole)
            }
        }
    }
}

/// Everything a task body may use.
pub struct TaskContext {
    /// The read-write pool (Python's `session_scope` / `run_in_session`).
    pub db: WriteDb,
    /// `baskfy_api.settings` (the database URL, mail, alert sinks).
    pub settings: Settings,
    /// `WorkerSettings`.
    pub worker: WorkerSettings,
    /// `--replay-bundle`: the provider stack is the replay adapter over this bundle.
    pub replay_bundle: Option<PathBuf>,
    /// `--replay-upstream`: a loopback server answering recorded Kite REST and NSE archive
    /// requests (`tools/parity/replay_upstream.py`). A lane that builds a Kite client points it
    /// here ([`TaskContext::kite_root`]); the NSE adapter reads `BASKFY_NSE_ARCHIVE_URL` itself.
    pub replay_upstream: Option<String>,
    clock: Arc<TaskClock>,
}

impl std::fmt::Debug for TaskContext {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TaskContext")
            .field("replay_bundle", &self.replay_bundle)
            .field("replay_upstream", &self.replay_upstream)
            .field("clock", &self.clock)
            .finish_non_exhaustive()
    }
}

impl TaskContext {
    /// A context over `db` with the given settings and clock.
    #[must_use]
    pub fn new(
        db: WriteDb,
        settings: Settings,
        worker: WorkerSettings,
        clock: TaskClock,
        replay_bundle: Option<PathBuf>,
        replay_upstream: Option<String>,
    ) -> Self {
        Self {
            db,
            settings,
            worker,
            replay_bundle,
            replay_upstream,
            clock: Arc::new(clock),
        }
    }

    /// `datetime.now(tz=UTC)`.
    #[must_use]
    pub fn now_utc(&self) -> DateTime<Utc> {
        self.clock.now()
    }

    /// `datetime.now(tz=IST)`, as a naive IST wall-clock moment.
    #[must_use]
    pub fn now_ist(&self) -> NaiveDateTime {
        (self.clock.now() + Duration::minutes(IST_OFFSET_MINUTES)).naive_utc()
    }

    /// `datetime.now(tz=IST).date()` — the default date of every `trade_date=None` task.
    #[must_use]
    pub fn today_ist(&self) -> NaiveDate {
        self.now_ist().date()
    }

    /// The clock as the providers take it (an `AccessTokenStore`'s `now`, an archive key's date).
    #[must_use]
    pub fn clock(&self) -> Arc<dyn Clock> {
        Arc::clone(&self.clock) as Arc<dyn Clock>
    }

    /// Is the provider stack the replay (the harness), rather than the network?
    #[must_use]
    pub const fn is_replay(&self) -> bool {
        self.replay_bundle.is_some()
    }

    /// The Kite REST root a lane builds its client with: the replay upstream under the harness,
    /// otherwise `None` (the SDK's default, `https://api.kite.trade`).
    #[must_use]
    pub fn kite_root(&self) -> Option<&str> {
        self.replay_upstream.as_deref()
    }

    /// `providers.sole_user_id()`.
    #[must_use]
    pub fn sole_user_id(&self) -> Option<i64> {
        sole_user_id()
    }

    /// `build_pipeline_dependencies()`.
    ///
    /// # Errors
    /// A provider setting that does not parse, or a replay bundle that does not open.
    pub async fn pipeline_dependencies(&self) -> Result<PipelineDependencies, WorkerError> {
        build_pipeline_dependencies(&self.worker, self.replay_bundle.as_ref())
            .await
            .map_err(WorkerError::Settings)
    }
}

/// `providers._sole_user_id`: `BASKFY_SOLE_USER_ID` with Python's `int()`, or `None`.
#[must_use]
pub fn sole_user_id() -> Option<i64> {
    let raw = std::env::var("BASKFY_SOLE_USER_ID").unwrap_or_default();
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    let body = text.strip_prefix(['+', '-']).unwrap_or(text);
    let valid = !body.is_empty()
        && !body.starts_with('_')
        && !body.ends_with('_')
        && !body.contains("__")
        && body.chars().all(|c| c.is_ascii_digit() || c == '_');
    let parsed = valid
        .then(|| text.replace('_', "").parse::<i64>().ok())
        .flatten();
    if parsed.is_none() {
        log_warning(&format!(
            "BASKFY_SOLE_USER_ID is not a number ({raw:?}); the swing step will skip"
        ));
    }
    parsed
}

/// `build_provider_stack()`, or the replay composite over `replay`.
///
/// # Errors
/// The settings' or the bundle's, as text.
pub async fn provider(replay: Option<&PathBuf>) -> Result<CompositeProvider, String> {
    let settings = ProviderSettings::from_env().map_err(|e| e.to_string())?;
    let threshold = u32::try_from(settings.provider_circuit_failure_threshold)
        .map_err(|_| "provider_circuit_failure_threshold must be at least 1".to_owned())?;
    match replay {
        Some(dir) => {
            let replay = Arc::new(ReplayProvider::open(dir.clone()).map_err(|e| e.to_string())?);
            CompositeProvider::new(
                vec![replay.register()],
                threshold,
                settings.provider_circuit_reset_seconds,
                None,
            )
            .map_err(|e| e.to_string())
        }
        None => build_provider_stack(&RedisSource::from_settings(&settings), &settings, None)
            .await
            .map_err(|e| e.to_string()),
    }
}

/// `build_pipeline_dependencies()`: every flag read once, here.
///
/// # Errors
/// As [`provider`].
pub async fn build_pipeline_dependencies(
    worker: &WorkerSettings,
    replay: Option<&PathBuf>,
) -> Result<PipelineDependencies, String> {
    let mut deps = PipelineDependencies::new(Arc::new(provider(replay).await?));
    // `build_cache()`: an unparseable URL is no cache, never an error.
    deps.cache = redis::Client::open(worker.redis_url.as_str()).ok();
    let sole = sole_user_id();
    deps.swing_user_id = sole;
    deps.swing_index_slug.clone_from(&worker.swing_index_slug);
    deps.swing_execution_enabled = worker.swing_execution_enabled;
    deps.vbt_user_id = sole;
    deps.vbt_nightly_enabled = worker.vbt_nightly_enabled;
    deps.vbt_execution_enabled = worker.vbt_execution_enabled;
    deps.twt_user_id = sole;
    deps.twt_nightly_enabled = worker.twt_nightly_enabled;
    deps.twt_execution_enabled = worker.twt_execution_enabled;
    Ok(deps)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn an_injected_clock_starts_at_the_ist_moment_and_advances() {
        let at = NaiveDateTime::parse_from_str("2026-10-29 13:15:00", "%Y-%m-%d %H:%M:%S").unwrap();
        let clock = TaskClock::starting_at_ist(at);
        assert!(clock.is_injected());
        let now = clock.now();
        // Exactly the moment in the first second (whole-second advance), then never backwards.
        assert_eq!(
            now.format("%Y-%m-%dT%H:%M:%S%.f").to_string(),
            "2026-10-29T07:45:00"
        );
        assert!(clock.now() >= now);
    }
}
