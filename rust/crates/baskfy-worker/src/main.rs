//! `baskfy-worker` — the worker's command line (Python: `python -m baskfy_worker.pipeline_cli`,
//! and the Celery tasks Beat fires).
//!
//! ```text
//! baskfy-worker nightly  --date YYYY-MM-DD [--replay-bundle DIR]
//! baskfy-worker step <name> --date YYYY-MM-DD --run-id N [--context FILE] [--replay-bundle DIR]
//! baskfy-worker catch-up [--now YYYY-MM-DDTHH:MM[:SS]] [--replay-bundle DIR]
//! baskfy-worker task <celery name> [--args JSON] [--now YYYY-MM-DDTHH:MM[:SS]]
//!                   [--replay-bundle DIR] [--replay-upstream URL]
//! baskfy-worker tasks
//! baskfy-worker beat --now YYYY-MM-DDTHH:MM
//! ```
//!
//! * `nightly` is `nightly_pipeline(trade_date)`: three transactions, the alert, the task's
//!   return value as JSON on stdout. Exit 0 when the run succeeded, 1 otherwise (a gate refusal,
//!   an aborted day, a raised chain), 2 for a usage or configuration error.
//! * `step` runs **one** step's block into the open run `N` on one transaction (committed when
//!   the block returns, the gate's refusal included; rolled back when it raises). For grading:
//!   `tools/parity/nightly_diff.py --step`. `--context` is a JSON file `{"touched": [ids]}`
//!   carrying step 3's answer to step 4.
//! * `catch-up` is `session_catch_up`'s decision only: `unlanded_sessions(through=today IST,
//!   lookback_days=7, now=now IST)` printed as `{"checked_through": D, "sessions": [D, ...]}` on
//!   a transaction that is rolled back (it writes nothing). Running the nightly for each, the
//!   swing EOD and the derived-holiday demotion stay with the Celery task (R-gate.4). `--now`
//!   injects the IST wall clock (naive, IST) for grading; without it the system clock is read.
//! * `task` runs **one Celery task** by its name (`baskfy_worker::tasks`): `--args` is the
//!   message's arguments — a JSON array (positional `args`) or object (`kwargs`) — bound as
//!   CPython binds them; `--now` injects the IST wall clock the task reads (it then advances in
//!   real time). The task's return value is printed as JSON; a raised error as
//!   `{"raised": <Python class>, "error": <str(exc)>}`. Exit 0 when it returned, 75 when it raised
//!   an error its `autoretry_for` names (the Celery shim retries; R8.1), 1 for any other raise, 2
//!   for a usage error or an unregistered name. One attempt: retries belong to the consumer.
//! * `tasks` lists the registry (name, queue by name, lane, ported or not) as JSON lines.
//! * `beat --now` lists the Beat entries due in that IST minute (Celery's crontab semantics).
//! * `--replay-upstream URL` (with `--replay-bundle`) points the Kite REST client a task builds at
//!   the harness's loopback server of recorded upstream answers (`tools/parity/replay_upstream.py`).
//! * `--replay-bundle DIR` swaps the provider stack for the replay adapter over `DIR`
//!   (`baskfy_providers::replay`) behind the production `CompositeProvider`, and arms the
//!   network block: nothing leaves the box (DECISIONS-RS R7.3).
//!
//! Configuration is the environment, as Python's: `BASKFY_DATABASE_URL` and the rest of
//! `baskfy_api.Settings`, `WorkerSettings`, `ProviderSettings`, `BASKFY_SOLE_USER_ID`. The log
//! goes to stderr; stdout carries only the JSON payload. No secret is ever printed.

use std::collections::BTreeSet;
use std::io::Write as _;
use std::path::PathBuf;
use std::process::ExitCode;

use baskfy_api::Settings;
use baskfy_db::{OrderedJson, WriteDb};
use baskfy_worker::deps::PipelineDependencies;
use baskfy_worker::error::WorkerError;
use baskfy_worker::nightly::{NightlyResult, run_nightly};
use baskfy_worker::orchestrator::{ChainState, run_single_step};
use baskfy_worker::pyjson::{self, object, s};
use baskfy_worker::settings::WorkerSettings;
use baskfy_worker::steps::{PipelineStep, RunStatus};
use baskfy_worker::tasks::{self, TaskContext, context::TaskClock};
use chrono::NaiveDate;

const USAGE: &str = "usage: baskfy-worker nightly --date YYYY-MM-DD [--replay-bundle DIR]\n       \
                     baskfy-worker step <name> --date YYYY-MM-DD --run-id N [--context FILE] \
                     [--replay-bundle DIR]\n       baskfy-worker catch-up [--now YYYY-MM-DDTHH:MM[:SS]] [--replay-bundle DIR]\n       \
                     baskfy-worker task <name> [--args JSON] [--now YYYY-MM-DDTHH:MM[:SS]] \
                     [--replay-bundle DIR] [--replay-upstream URL]\n       baskfy-worker tasks\n       \
                     baskfy-worker beat --now YYYY-MM-DDTHH:MM";

#[derive(Debug, Default)]
struct Args {
    command: String,
    step: Option<String>,
    date: Option<NaiveDate>,
    run_id: Option<i64>,
    context: Option<PathBuf>,
    replay: Option<PathBuf>,
    now: Option<chrono::NaiveDateTime>,
    task: Option<String>,
    task_args: Option<String>,
    upstream: Option<String>,
}

fn parse_args(raw: &[String]) -> Result<Args, String> {
    let mut args = Args::default();
    let mut it = raw.iter();
    args.command = it.next().cloned().ok_or("no command")?;
    if args.command == "step" {
        args.step = Some(it.next().cloned().ok_or("step needs a name")?);
    }
    if args.command == "task" {
        args.task = Some(it.next().cloned().ok_or("task needs a Celery task name")?);
    }
    while let Some(flag) = it.next() {
        let mut value = || it.next().cloned().ok_or(format!("{flag} needs a value"));
        match flag.as_str() {
            "--date" => {
                let v = value()?;
                args.date = Some(
                    NaiveDate::parse_from_str(&v, "%Y-%m-%d")
                        .map_err(|_| format!("--date {v:?} is not YYYY-MM-DD"))?,
                );
            }
            "--run-id" => {
                let v = value()?;
                args.run_id = Some(
                    v.parse()
                        .map_err(|_| format!("--run-id {v:?} is not an integer"))?,
                );
            }
            "--context" => args.context = Some(PathBuf::from(value()?)),
            "--replay-bundle" => args.replay = Some(PathBuf::from(value()?)),
            "--replay-upstream" => args.upstream = Some(value()?),
            "--args" => args.task_args = Some(value()?),
            "--now" => {
                let v = value()?;
                args.now = Some(
                    chrono::NaiveDateTime::parse_from_str(&v, "%Y-%m-%dT%H:%M:%S")
                        .or_else(|_| chrono::NaiveDateTime::parse_from_str(&v, "%Y-%m-%dT%H:%M"))
                        .map_err(|_| format!("--now {v:?} is not YYYY-MM-DDTHH:MM[:SS]"))?,
                );
            }
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    Ok(args)
}

fn emit(payload: &OrderedJson) {
    let text = payload
        .to_python_json()
        .unwrap_or_else(|e| format!("{{\"error\": \"unprintable payload: {e}\"}}"));
    let _ = writeln!(std::io::stdout(), "{text}");
}

fn fail(message: &str) -> ExitCode {
    let _ = writeln!(std::io::stderr(), "baskfy-worker: {message}\n{USAGE}");
    ExitCode::from(2)
}

/// `build_pipeline_dependencies()` (shared with the task runner).
async fn dependencies(
    worker: &WorkerSettings,
    replay: Option<&PathBuf>,
) -> Result<PipelineDependencies, String> {
    baskfy_worker::tasks::context::build_pipeline_dependencies(worker, replay).await
}

fn read_context(path: Option<&PathBuf>) -> Result<ChainState, String> {
    let Some(path) = path else {
        return Ok(ChainState::default());
    };
    let text = std::fs::read_to_string(path).map_err(|e| format!("cannot read --context: {e}"))?;
    let value: serde_json::Value =
        serde_json::from_str(&text).map_err(|e| format!("--context does not parse: {e}"))?;
    let touched = match value.get("touched") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::Array(items)) => Some(
            items
                .iter()
                .map(|v| v.as_i64().ok_or("--context touched holds a non-integer"))
                .collect::<Result<BTreeSet<i64>, _>>()?,
        ),
        Some(_) => return Err("--context touched is not a list".to_owned()),
    };
    Ok(ChainState { touched })
}

fn raised(error: &WorkerError) -> OrderedJson {
    object([
        ("raised", s(error.python_type())),
        ("error", s(error.to_string())),
    ])
}

async fn run(args: Args) -> ExitCode {
    if args.replay.is_some() {
        baskfy_providers::net::block_network();
    }
    if args.upstream.is_some() && args.replay.is_none() {
        return fail("--replay-upstream is a harness seam; it needs --replay-bundle");
    }
    match args.command.as_str() {
        "tasks" => return list_tasks(),
        "beat" => return beat_due(args.now),
        _ => {}
    }
    let settings = match Settings::from_env() {
        Ok(s) => s,
        Err(e) => return fail(&format!("settings: {e}")),
    };
    let worker = match WorkerSettings::from_env() {
        Ok(s) => s,
        Err(e) => return fail(&format!("worker settings: {e}")),
    };
    let db = match WriteDb::connect_lazy(&settings.db_settings()) {
        Ok(db) => db,
        Err(e) => return fail(&format!("database: {e}")),
    };
    if args.command == "task" {
        let clock = args
            .now
            .map_or_else(TaskClock::system, TaskClock::starting_at_ist);
        let ctx = TaskContext::new(db, settings, worker, clock, args.replay, args.upstream);
        let code = task_command(&ctx, args.task.as_deref().unwrap_or(""), args.task_args).await;
        ctx.db.close().await;
        return code;
    }
    let deps = match dependencies(&worker, args.replay.as_ref()).await {
        Ok(d) => d,
        Err(e) => return fail(&format!("providers: {e}")),
    };
    let code = match args.command.as_str() {
        "nightly" => {
            let Some(day) = args.date else {
                return fail("nightly needs --date");
            };
            match run_nightly(&db, &settings, &deps, day).await {
                Ok(NightlyResult::Returned { outcome, payload }) => {
                    emit(&payload);
                    if outcome.status == RunStatus::Succeeded {
                        ExitCode::SUCCESS
                    } else {
                        ExitCode::from(1)
                    }
                }
                Ok(NightlyResult::Raised { run_id, error }) => {
                    let OrderedJson::Object(mut entries) = raised(&error) else {
                        return ExitCode::from(1);
                    };
                    entries.insert(0, ("run_id".to_owned(), OrderedJson::Int(run_id)));
                    entries.insert(
                        1,
                        (
                            "trade_date".to_owned(),
                            s(day.format("%Y-%m-%d").to_string()),
                        ),
                    );
                    emit(&OrderedJson::Object(entries));
                    ExitCode::from(1)
                }
                Err(e) => {
                    emit(&raised(&e));
                    ExitCode::from(1)
                }
            }
        }
        "step" => {
            let (Some(name), Some(day), Some(run_id)) =
                (args.step.as_deref(), args.date, args.run_id)
            else {
                return fail("step needs a name, --date and --run-id");
            };
            let Some(step) = PipelineStep::from_value(name) else {
                return fail(&format!("unknown step {name:?}"));
            };
            let state = match read_context(args.context.as_ref()) {
                Ok(s) => s,
                Err(e) => return fail(&e),
            };
            step_command(&db, &deps, step, day, run_id, state).await
        }
        "catch-up" => {
            let mut result = ExitCode::from(1);
            match db.begin().await {
                Ok(mut tx) => {
                    use baskfy_providers::clock::Clock as _;
                    let now_ist = args.now.unwrap_or_else(|| {
                        (baskfy_providers::clock::system_clock().now()
                            + chrono::Duration::minutes(330))
                        .naive_utc()
                    });
                    match baskfy_worker::catch_up::unlanded_sessions(
                        &mut tx,
                        now_ist.date(),
                        baskfy_worker::catch_up::DEFAULT_LOOKBACK_DAYS,
                        Some(now_ist),
                    )
                    .await
                    {
                        Ok(days) => {
                            emit(&object([
                                (
                                    "checked_through",
                                    s(now_ist.date().format("%Y-%m-%d").to_string()),
                                ),
                                (
                                    "sessions",
                                    pyjson::strings(
                                        days.iter().map(|d| d.format("%Y-%m-%d").to_string()),
                                    ),
                                ),
                            ]));
                            result = ExitCode::SUCCESS;
                        }
                        Err(e) => emit(&raised(&e)),
                    }
                    let _ = tx.rollback().await;
                }
                Err(e) => emit(&raised(&WorkerError::from(e))),
            }
            result
        }
        other => fail(&format!("unknown command {other:?}")),
    };
    db.close().await;
    code
}

async fn step_command(
    db: &WriteDb,
    deps: &PipelineDependencies,
    step: PipelineStep,
    day: NaiveDate,
    run_id: i64,
    state: ChainState,
) -> ExitCode {
    let base = [
        ("step", s(step.value())),
        ("trade_date", s(day.format("%Y-%m-%d").to_string())),
        ("run_id", OrderedJson::Int(run_id)),
    ];
    let mut tx = match db.begin().await {
        Ok(tx) => tx,
        Err(e) => {
            emit(&raised(&WorkerError::from(e)));
            return ExitCode::from(1);
        }
    };
    match run_single_step(&mut tx, run_id, day, deps, step, state).await {
        Ok(done) => {
            if let Err(e) = tx.commit().await {
                emit(&raised(&WorkerError::from(e)));
                return ExitCode::from(1);
            }
            let refused = done.hard_failure.is_some();
            let mut entries: Vec<(String, OrderedJson)> =
                base.into_iter().map(|(k, v)| (k.to_owned(), v)).collect();
            for (k, v) in [
                (
                    "result",
                    s(if refused { "hard_failure" } else { "completed" }),
                ),
                (
                    "data_version",
                    done.outcome
                        .data_version
                        .map_or(OrderedJson::Null, OrderedJson::Int),
                ),
                (
                    "gate_passed",
                    done.outcome
                        .gate
                        .as_ref()
                        .map_or(OrderedJson::Null, |g| OrderedJson::Bool(g.passed())),
                ),
                ("hard_failure", pyjson::opt_s(done.hard_failure.clone())),
            ] {
                pyjson::update(&mut entries, k, v);
            }
            emit(&OrderedJson::Object(entries));
            if refused {
                ExitCode::from(1)
            } else {
                ExitCode::SUCCESS
            }
        }
        Err(e) => {
            let _ = tx.rollback().await;
            let mut entries: Vec<(String, OrderedJson)> =
                base.into_iter().map(|(k, v)| (k.to_owned(), v)).collect();
            pyjson::update(&mut entries, "result", s("raised"));
            pyjson::update(&mut entries, "error_type", s(e.python_type()));
            pyjson::update(&mut entries, "error", s(e.to_string()));
            emit(&OrderedJson::Object(entries));
            ExitCode::from(1)
        }
    }
}

/// A Celery message's `args` (positional) and `kwargs` (keywords).
type MessageArgs = (Vec<OrderedJson>, Vec<(String, OrderedJson)>);

/// `--args`: a JSON array (positional) or object (keywords).
fn message_arguments(raw: Option<String>) -> Result<MessageArgs, String> {
    let Some(text) = raw else {
        return Ok((Vec::new(), Vec::new()));
    };
    match OrderedJson::parse(&text).map_err(|e| format!("--args is not JSON: {e}"))? {
        OrderedJson::Array(items) => Ok((items, Vec::new())),
        OrderedJson::Object(entries) => Ok((Vec::new(), entries)),
        _ => Err("--args must be a JSON array (args) or object (kwargs)".to_owned()),
    }
}

async fn task_command(ctx: &TaskContext, name: &str, raw: Option<String>) -> ExitCode {
    let Some(spec) = tasks::find(name) else {
        emit(&object([
            ("raised", s("NotRegistered")),
            ("error", s(format!("'{name}'"))),
        ]));
        return ExitCode::from(2);
    };
    let (positional, keywords) = match message_arguments(raw) {
        Ok(a) => a,
        Err(e) => return fail(&e),
    };
    match tasks::run_task(ctx, spec.name, positional, keywords).await {
        Ok(payload) => {
            emit(&payload);
            ExitCode::SUCCESS
        }
        Err(e) => {
            emit(&raised(&e));
            if spec.retries_on(&e) {
                ExitCode::from(75)
            } else {
                ExitCode::from(1)
            }
        }
    }
}

fn list_tasks() -> ExitCode {
    for spec in tasks::registry::REGISTRY {
        emit(&object([
            ("name", s(spec.name)),
            ("queue", s(spec.queue_by_name().name())),
            ("lane", s(spec.lane.name())),
            ("ported", OrderedJson::Bool(spec.is_ported())),
        ]));
    }
    ExitCode::SUCCESS
}

fn beat_due(now: Option<chrono::NaiveDateTime>) -> ExitCode {
    let Some(at) = now else {
        return fail("beat needs --now (an IST minute)");
    };
    match tasks::beat::due_at(at) {
        Ok(entries) => {
            for e in entries {
                emit(&object([
                    ("entry", s(e.name)),
                    ("task", s(e.task)),
                    ("queue", s(e.options.queue.name())),
                ]));
            }
            ExitCode::SUCCESS
        }
        Err(e) => fail(&e),
    }
}

fn main() -> ExitCode {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let args = match parse_args(&raw) {
        Ok(a) => a,
        Err(e) => return fail(&e),
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => return fail(&format!("cannot start the runtime: {e}")),
    };
    runtime.block_on(run(args))
}
