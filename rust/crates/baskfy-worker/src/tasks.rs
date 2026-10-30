//! Every Celery task of the Python worker, by name, as Rust functions (wave 6; DECISIONS-RS R8.1).
//!
//! Python registers 76 `baskfy.*` tasks (`tasks/celery_tasks.py`, `tasks/desk.py`) and Beat fires
//! 61 schedule entries (`celery_app.BEAT_SCHEDULE`). This module is their Rust surface:
//!
//! * [`registry`] — one [`TaskSpec`] per Python task: its name, the Python function, its
//!   parameters (names, annotations, defaults), the retry policy and queue Celery applies, and
//!   the Rust function that runs it. Held equal to Python by `tests/task_registry.rs` against
//!   `tests/fixtures/python_task_registry.json` (written by `tools/parity/task_registry.py`).
//! * [`beat`] — `BEAT_SCHEDULE` as data (crontabs in IST, args, kwargs, options), held equal the
//!   same way, with Celery's crontab expansion and a `due` check.
//! * [`args`] — a message's `args`/`kwargs` bound to a task's parameters as Python binds them.
//! * [`context`] — what a task runs with: the database, settings, the provider stack and the
//!   **clock** (`--now` injects it; a task never reads the system clock directly).
//! * [`pipeline`] — the tasks wave 5's nightly code already answers (the foundation wires them).
//! * one module per **lane** — `screener`, `api_routes`, `curated`, `backtest_engine`,
//!   `sleeve_backtests`, `swing_eod`, `swing_live`, `twt_jobs`, `vbt_jobs`, `portfolio`,
//!   `ops_alerts`, `fno`, `options`, `backfills`, `clis` — each a **declared stub**: one async
//!   function per task returning [`WorkerError::TaskNotPorted`], and a `PORTED` list the lane
//!   grows as it lands bodies. A lane edits only its own file (and private submodules it declares
//!   there); never `lib.rs`, this file, the registry or the Beat table.
//!
//! Run one task: `baskfy-worker task <name> --args '<json>' [--now YYYY-MM-DDTHH:MM[:SS]]`.
//! Grade it against Python: `tools/parity/task_diff.py` (README "Grading a task").
//!
//! **Law 2.** No task here places, modifies or cancels an order or a GTT, or calls a Kite write
//! endpoint (`tests/law_two.rs` scans these files too). The swing and VBT "check" tasks *read*
//! the desk's books; execution is the last wave.

pub mod api_routes;
pub mod args;
pub mod backfills;
pub mod backtest_engine;
pub mod beat;
pub mod clis;
pub mod context;
pub mod curated;
pub mod fno;
pub mod ops_alerts;
pub mod options;
pub mod pipeline;
pub mod portfolio;
pub mod registry;
pub mod screener;
pub mod sleeve_backtests;
pub mod swing_eod;
pub mod swing_live;
pub mod twt_jobs;
pub mod vbt_jobs;

use std::future::Future;
use std::pin::Pin;

use baskfy_db::OrderedJson;

pub use args::BoundArgs;
pub use context::TaskContext;

use crate::error::WorkerError;

/// What every task returns: its JSON result (Python's return value) or the error it raised.
pub type TaskResult = Result<OrderedJson, WorkerError>;

/// A running task. Not `Send`: the nightly chain's publish step holds a `!Send` screen query
/// across an await (wave 5), so a task is driven on the thread that started it (`block_on`, or a
/// `LocalSet` in a future scheduler) — R8.1.
pub type TaskFuture<'a> = Pin<Box<dyn Future<Output = TaskResult> + 'a>>;

/// The Rust body of one task: the context and the bound arguments in, the result out.
pub type TaskFn = for<'a> fn(&'a TaskContext, BoundArgs) -> TaskFuture<'a>;

/// The Celery queues (`celery_app.QUEUES`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Queue {
    /// Network-bound, rate-limited, long.
    Ingest,
    /// CPU-bound polars work.
    Compute,
    /// Backtests (docs/03 "Scaling plan" step 4).
    Backtest,
    /// Orchestration, publishing, alerts.
    Default,
}

impl Queue {
    /// The queue's name on the broker.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Ingest => "ingest",
            Self::Compute => "compute",
            Self::Backtest => "backtest",
            Self::Default => "default",
        }
    }
}

/// `celery_app.QUEUES`, in Python's order.
pub const QUEUES: [Queue; 4] = [
    Queue::Ingest,
    Queue::Compute,
    Queue::Backtest,
    Queue::Default,
];

/// `celery_app.TASK_ROUTES`, in Python's order (`dict` order; Celery tries them in order and the
/// first glob that matches wins).
pub const TASK_ROUTES: &[(&str, Queue)] = &[
    ("baskfy.ingest.*", Queue::Ingest),
    ("baskfy.eq_bars.*", Queue::Ingest),
    ("baskfy.compute.*", Queue::Compute),
    ("baskfy.backtest.*", Queue::Backtest),
    ("baskfy.pipeline.*", Queue::Default),
    ("baskfy.ops.*", Queue::Default),
    ("baskfy.alerts.*", Queue::Default),
    ("baskfy.desk.*", Queue::Default),
    ("baskfy.swing.check_*", Queue::Default),
    ("baskfy.vbt.check_*", Queue::Default),
    ("baskfy.vbt.backtest", Queue::Compute),
    ("baskfy.overlap.catalyst_scan", Queue::Compute),
    ("baskfy.fno.retest", Queue::Compute),
    ("baskfy.vbt.rescan", Queue::Compute),
    ("baskfy.vbt.rescan_sweep", Queue::Default),
    ("baskfy.twt.scan", Queue::Compute),
    ("baskfy.twt.scan_publish", Queue::Default),
    ("baskfy.swing.scan_now", Queue::Compute),
    ("baskfy.swing.scan_after_login", Queue::Compute),
    ("baskfy.swing.scan_sweep", Queue::Default),
    ("baskfy.kite.*", Queue::Compute),
    ("baskfy.cb.*", Queue::Compute),
    ("baskfy.portfolio.*", Queue::Compute),
    ("baskfy.options.*", Queue::Default),
];

/// `task_default_queue`.
pub const DEFAULT_QUEUE: Queue = Queue::Default;

/// Celery's class default for `Task.default_retry_delay` (3 × 60 s). The worker configures
/// `task_default_retry_delay = BASKFY_TASK_RETRY_BACKOFF_SECONDS` (60), but Celery only applies a
/// config value to a task attribute that is `None`, and this one never is — so every task retries
/// after 180 s. Reproduced, not corrected (DECISIONS-RS R8.1).
pub const CELERY_DEFAULT_RETRY_DELAY_SECONDS: i64 = 180;

/// Celery's default `retry_backoff_max` (seconds).
pub const CELERY_RETRY_BACKOFF_MAX_SECONDS: i64 = 600;

/// The lanes of wave 6, plus the foundation's own wiring of wave-5 code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Lane {
    /// Wave-5 code the foundation wires as tasks ([`pipeline`]).
    Pipeline,
    /// [`screener`].
    Screener,
    /// [`api_routes`].
    ApiRoutes,
    /// [`curated`].
    Curated,
    /// [`backtest_engine`].
    BacktestEngine,
    /// [`sleeve_backtests`].
    SleeveBacktests,
    /// [`swing_eod`].
    SwingEod,
    /// [`swing_live`].
    SwingLive,
    /// [`twt_jobs`].
    TwtJobs,
    /// [`vbt_jobs`].
    VbtJobs,
    /// [`portfolio`].
    Portfolio,
    /// [`ops_alerts`].
    OpsAlerts,
    /// [`fno`].
    Fno,
    /// [`options`].
    Options,
    /// [`backfills`].
    Backfills,
    /// [`clis`].
    Clis,
}

impl Lane {
    /// The lane's module name.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Pipeline => "pipeline",
            Self::Screener => "screener",
            Self::ApiRoutes => "api_routes",
            Self::Curated => "curated",
            Self::BacktestEngine => "backtest_engine",
            Self::SleeveBacktests => "sleeve_backtests",
            Self::SwingEod => "swing_eod",
            Self::SwingLive => "swing_live",
            Self::TwtJobs => "twt_jobs",
            Self::VbtJobs => "vbt_jobs",
            Self::Portfolio => "portfolio",
            Self::OpsAlerts => "ops_alerts",
            Self::Fno => "fno",
            Self::Options => "options",
            Self::Backfills => "backfills",
            Self::Clis => "clis",
        }
    }

    /// The task names this lane has ported (its own `PORTED` list).
    #[must_use]
    pub fn ported(self) -> &'static [&'static str] {
        match self {
            Self::Pipeline => pipeline::PORTED,
            Self::Screener => screener::PORTED,
            Self::ApiRoutes => api_routes::PORTED,
            Self::Curated => curated::PORTED,
            Self::BacktestEngine => backtest_engine::PORTED,
            Self::SleeveBacktests => sleeve_backtests::PORTED,
            Self::SwingEod => swing_eod::PORTED,
            Self::SwingLive => swing_live::PORTED,
            Self::TwtJobs => twt_jobs::PORTED,
            Self::VbtJobs => vbt_jobs::PORTED,
            Self::Portfolio => portfolio::PORTED,
            Self::OpsAlerts => ops_alerts::PORTED,
            Self::Fno => fno::PORTED,
            Self::Options => options::PORTED,
            Self::Backfills => backfills::PORTED,
            Self::Clis => clis::PORTED,
        }
    }
}

/// A Python default value, as `inspect.signature` reports it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum PyDefault {
    /// No default: the argument is required.
    Required,
    /// `None`.
    None,
    /// A `str`.
    Str(&'static str),
    /// An `int`.
    Int(i64),
    /// A `bool`.
    Bool(bool),
}

/// One parameter of a task's `run`.
#[derive(Debug, Clone, Copy)]
pub struct Param {
    /// The parameter's name (a keyword argument of the message).
    pub name: &'static str,
    /// Its annotation as written in Python (`"str | None"`, `"int"`, ...).
    pub annotation: &'static str,
    /// Its default.
    pub default: PyDefault,
}

/// One Celery task: what Python registered, and what runs it in Rust.
#[derive(Clone, Copy)]
pub struct TaskSpec {
    /// The Celery name (`baskfy.pipeline.nightly`).
    pub name: &'static str,
    /// The Python function (`module.qualname`).
    pub python: &'static str,
    /// The lane that owns the port.
    pub lane: Lane,
    /// `run`'s parameters, in order (a bound task's `self` excluded).
    pub params: &'static [Param],
    /// `bind=True`: the body calls `self.retry(...)` itself.
    pub bound: bool,
    /// `acks_late` (the app default is `True`; a few tasks set `False`).
    pub acks_late: bool,
    /// `autoretry_for`: the Python classes that retry automatically.
    pub autoretry_for: &'static [&'static str],
    /// `retry_backoff`.
    pub retry_backoff: bool,
    /// `time_limit`, seconds.
    pub time_limit: Option<i64>,
    /// The task's own `queue=` option (applied by `apply_async`, not by `send_task`).
    pub queue_option: Option<Queue>,
    /// The Rust body.
    pub run: TaskFn,
}

impl std::fmt::Debug for TaskSpec {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TaskSpec")
            .field("name", &self.name)
            .field("lane", &self.lane)
            .finish_non_exhaustive()
    }
}

impl TaskSpec {
    /// The queue a message published **by name** lands on (`send_task`: the API's producer, the
    /// Rust producer, Beat): `TASK_ROUTES` only.
    #[must_use]
    pub fn queue_by_name(&self) -> Queue {
        route_by_name(self.name)
    }

    /// The queue a message published **through the task** lands on (`task.apply_async`): the
    /// task's own `queue=` option first, then `TASK_ROUTES`.
    #[must_use]
    pub fn queue_via_task(&self) -> Queue {
        self.queue_option
            .unwrap_or_else(|| route_by_name(self.name))
    }

    /// `max_retries`: the `task_annotations["*"]` entry, which is `BASKFY_TASK_MAX_RETRIES`.
    #[must_use]
    pub const fn max_retries(&self, worker: &crate::settings::WorkerSettings) -> i64 {
        worker.task_max_retries
    }

    /// Would Celery retry this task automatically for `error` (`autoretry_for`, subclasses
    /// included)?
    #[must_use]
    pub fn retries_on(&self, error: &WorkerError) -> bool {
        self.autoretry_for
            .iter()
            .any(|class| match (*class, error) {
                ("TransientProviderError", WorkerError::Provider(e)) => e.is_transient(),
                (other, e) => e.python_type() == other,
            })
    }

    /// Is the body ported (its lane lists it)?
    #[must_use]
    pub fn is_ported(&self) -> bool {
        self.lane.ported().contains(&self.name)
    }
}

/// Celery's router for a name: the first `TASK_ROUTES` glob that matches, else the default queue.
#[must_use]
pub fn route_by_name(name: &str) -> Queue {
    TASK_ROUTES
        .iter()
        .find(|(pattern, _)| glob_matches(pattern, name))
        .map_or(DEFAULT_QUEUE, |(_, q)| *q)
}

/// `fnmatch.fnmatchcase` for the patterns `TASK_ROUTES` uses (`*` only; no `?` or classes).
fn glob_matches(pattern: &str, name: &str) -> bool {
    match pattern.split_once('*') {
        None => pattern == name,
        Some((prefix, rest)) => {
            if !name.starts_with(prefix) {
                return false;
            }
            let tail = &name[prefix.len()..];
            if rest.is_empty() {
                return true;
            }
            (0..=tail.len()).any(|i| tail.is_char_boundary(i) && glob_matches(rest, &tail[i..]))
        }
    }
}

/// Look a task up by its Celery name.
#[must_use]
pub fn find(name: &str) -> Option<&'static TaskSpec> {
    registry::REGISTRY.iter().find(|t| t.name == name)
}

/// The typed "not ported" error every declared stub returns.
///
/// # Errors
/// Always [`WorkerError::TaskNotPorted`].
pub fn not_ported(task: &'static str, lane: Lane) -> TaskResult {
    Err(WorkerError::TaskNotPorted {
        task,
        lane: lane.name(),
    })
}

/// Run `name` with the message's arguments (`args` positional, `kwargs` by name), as a Celery
/// worker would call the task body once — no retry: retries belong to whoever consumes the
/// message (R8.1).
///
/// # Errors
/// `KeyError`-shaped [`WorkerError::Python`] for an unregistered name; Python's `TypeError` for
/// arguments that do not bind; whatever the body raises.
pub async fn run_task(
    ctx: &TaskContext,
    name: &str,
    positional: Vec<OrderedJson>,
    keywords: Vec<(String, OrderedJson)>,
) -> TaskResult {
    let Some(spec) = find(name) else {
        return Err(WorkerError::python(
            "NotRegistered",
            format!("'{}'", name.replace('\'', "\\'")),
        ));
    };
    let bound = args::bind(spec, positional, keywords)?;
    (spec.run)(ctx, bound).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn globs_match_as_fnmatch() {
        assert!(glob_matches(
            "baskfy.swing.check_*",
            "baskfy.swing.check_gtt_at_1515"
        ));
        assert!(!glob_matches(
            "baskfy.swing.check_*",
            "baskfy.swing.catalyst"
        ));
        assert!(glob_matches("baskfy.vbt.rescan", "baskfy.vbt.rescan"));
        assert!(!glob_matches(
            "baskfy.vbt.rescan",
            "baskfy.vbt.rescan_sweep"
        ));
        assert!(glob_matches("baskfy.*", "baskfy."));
    }

    #[test]
    fn routes_by_name_follow_python_order() {
        // `baskfy.vbt.rescan_sweep` is not caught by `baskfy.vbt.rescan` (no glob).
        assert_eq!(route_by_name("baskfy.vbt.rescan_sweep"), Queue::Default);
        assert_eq!(route_by_name("baskfy.vbt.rescan"), Queue::Compute);
        // No route: the default queue (the swing backtest's own `queue=compute` is not a route).
        assert_eq!(route_by_name("baskfy.swing.backtest"), Queue::Default);
    }
}
