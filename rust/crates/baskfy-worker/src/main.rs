//! `baskfy-worker` — the worker's command line (Python: `python -m baskfy_worker.pipeline_cli`,
//! and the Celery tasks Beat fires).
//!
//! ```text
//! baskfy-worker nightly  --date YYYY-MM-DD [--replay-bundle DIR]
//! baskfy-worker step <name> --date YYYY-MM-DD --run-id N [--context FILE] [--replay-bundle DIR]
//! baskfy-worker catch-up [--now YYYY-MM-DDTHH:MM[:SS]] [--replay-bundle DIR]
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
use std::sync::Arc;

use baskfy_api::Settings;
use baskfy_db::{OrderedJson, WriteDb};
use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::factory::{RedisSource, build_provider_stack};
use baskfy_providers::replay::ReplayProvider;
use baskfy_providers::settings::ProviderSettings;
use baskfy_worker::deps::PipelineDependencies;
use baskfy_worker::error::WorkerError;
use baskfy_worker::nightly::{NightlyResult, run_nightly};
use baskfy_worker::orchestrator::{ChainState, run_single_step};
use baskfy_worker::pyjson::{self, object, s};
use baskfy_worker::settings::WorkerSettings;
use baskfy_worker::steps::{PipelineStep, RunStatus, log_warning};
use chrono::NaiveDate;

const USAGE: &str = "usage: baskfy-worker nightly --date YYYY-MM-DD [--replay-bundle DIR]\n       \
                     baskfy-worker step <name> --date YYYY-MM-DD --run-id N [--context FILE] \
                     [--replay-bundle DIR]\n       baskfy-worker catch-up [--now YYYY-MM-DDTHH:MM[:SS]] [--replay-bundle DIR]";

#[derive(Debug, Default)]
struct Args {
    command: String,
    step: Option<String>,
    date: Option<NaiveDate>,
    run_id: Option<i64>,
    context: Option<PathBuf>,
    replay: Option<PathBuf>,
    now: Option<chrono::NaiveDateTime>,
}

fn parse_args(raw: &[String]) -> Result<Args, String> {
    let mut args = Args::default();
    let mut it = raw.iter();
    args.command = it.next().cloned().ok_or("no command")?;
    if args.command == "step" {
        args.step = Some(it.next().cloned().ok_or("step needs a name")?);
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

/// `providers._sole_user_id`: `BASKFY_SOLE_USER_ID` with Python's `int()`, or `None`.
fn sole_user_id() -> Option<i64> {
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

async fn provider(replay: Option<&PathBuf>) -> Result<CompositeProvider, String> {
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
async fn dependencies(
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
