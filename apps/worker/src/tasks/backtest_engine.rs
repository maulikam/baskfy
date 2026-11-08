//! Lane **backtest_engine** — `baskfy.backtest.run`, the screen backtest on its own queue.
//!
//! Python: `tasks/backtests.run_backtest_job` (archive + event publisher; a redelivered message
//! for a run another worker took returns `skipped`), `baskfy_worker.backtest` (the loader:
//! calendar, a screen per rebalance date, the candidates' bars, the benchmark, the coverage
//! guards) and the job's half of `baskfy_api.backtests` (CSV artefacts, the stored payload and
//! its metrics hash, the progress frames). The simulation is `baskfy_core::backtest` (wave 2),
//! the metrics `baskfy_core::backtest_metrics`; the screen is the publish step's
//! (`steps::gate::execute_screen_with_limit`).
//!
//! This consumes the message `POST /backtests` enqueues with the Celery executor (RS4c R6.28):
//! `baskfy.backtest.run(public_id)`. The in-process (`inline`) executor's two-transaction
//! variant (`run_backtest_inline`) belongs to the API process, not to a Celery task; it is not a
//! task and is not ported here (DECISIONS-RS, lane label R-be.1).
//!
//! Private submodules: `loader` (`baskfy_worker.backtest`), `payload` (`baskfy_api.backtests`),
//! `job` (`tasks/backtests.py`), `risk_free` (`baskfy_core.risk_free`). Grade with
//! `tools/parity/task_diff.py --cases tools/parity/task_cases/backtest_engine.json`.
//!
//! **Law 2.** Nothing here reaches a broker: a backtest reads the database and writes its own
//! row, three CSV files and Redis progress frames.

mod job;
mod loader;
mod payload;
mod risk_free;

use baskfy_db::OrderedJson;

use super::{BoundArgs, TaskContext, TaskResult};

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &["baskfy.backtest.run"];

/// Python's truth value of a JSON argument (`if fragility:`).
fn truthy(value: Option<&OrderedJson>) -> bool {
    match value {
        None | Some(OrderedJson::Null) => false,
        Some(OrderedJson::Bool(b)) => *b,
        Some(OrderedJson::Int(i)) => *i != 0,
        Some(OrderedJson::UInt(u)) => *u != 0,
        Some(OrderedJson::BigInt(_)) => true,
        Some(OrderedJson::Float(f)) => *f != 0.0,
        Some(OrderedJson::Str(s)) => !s.is_empty(),
        Some(OrderedJson::Array(a)) => !a.is_empty(),
        Some(OrderedJson::Object(o)) => !o.is_empty(),
    }
}

/// Ports `baskfy_worker.tasks.celery_tasks.run_backtest_task` — `baskfy.backtest.run(public_id, fragility=True)`.
///
/// Deliberately not auto-retried (Python's docstring): a failure belongs on the row
/// (`backtest.error`), where the user reads it.
///
/// # Errors
/// What Python's task raises: a `public_id` that is not a string (`TypeError`), a database or
/// settings failure, or any failure the job does not record on the row (the transaction rolls
/// back and the row stays `queued`).
pub async fn run_backtest_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let public_id = args.str("public_id")?;
    let fragility = truthy(args.value("fragility"));
    job::run(ctx, &public_id, fragility).await
}
