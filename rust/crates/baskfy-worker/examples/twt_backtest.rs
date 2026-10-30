//! `tools/twt/backtest.py`'s `_run`, in Rust — the grading entry for the TWT backtest, which has
//! no Celery task (lane `sleeve_backtests`, wave 6; `tools/parity/twt_backtest_diff.py`).
//!
//! ```text
//! twt_backtest --args '{"start": "2025-01-01", "end": null, "sleeve": "1000000",
//!                       "skip_etf_denominator": false}' [--now YYYY-MM-DDTHH:MM[:SS]]
//! ```
//!
//! Reads `BASKFY_DATABASE_URL` and `BASKFY_SOLE_USER_ID`; runs `run_twt_backtest` in one
//! transaction (committed on success, rolled back on a raise, as the script's
//! `session.begin()`); prints the report JSON (`json.dumps`), or `{"raised", "error"}` and exits 1.
//! Places nothing; writes one `tw_backtest_run` row.

use std::io::Write as _;
use std::process::ExitCode;
use std::str::FromStr;

use baskfy_api::Settings;
use baskfy_db::{OrderedJson, WriteDb};
use baskfy_providers::clock::Clock;
use baskfy_worker::error::WorkerError;
use baskfy_worker::pyjson;
use baskfy_worker::tasks::args::fromisoformat_date;
use baskfy_worker::tasks::context::{TaskClock, sole_user_id};
use baskfy_worker::tasks::sleeve_backtests::{TwtBacktestArgs, run_twt_backtest};
use chrono::{DateTime, NaiveDate, NaiveDateTime, Timelike, Utc};
use rust_decimal::Decimal;

fn iso(moment: DateTime<Utc>) -> String {
    let micros = moment.nanosecond() / 1_000;
    if micros == 0 {
        moment.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        format!("{}.{micros:06}+00:00", moment.format("%Y-%m-%dT%H:%M:%S"))
    }
}

fn emit(value: &OrderedJson) {
    let text = pyjson::dumps(value).unwrap_or_else(|e| e);
    let _ = writeln!(std::io::stdout(), "{text}");
}

fn say(message: &str) {
    let _ = writeln!(std::io::stderr(), "{message}");
}

fn raised(class: &str, message: &str) -> ExitCode {
    emit(&pyjson::object([
        ("raised", pyjson::s(class)),
        ("error", pyjson::s(message)),
    ]));
    ExitCode::from(1)
}

fn date_arg(args: &OrderedJson, name: &str) -> Result<Option<NaiveDate>, WorkerError> {
    match args.get(name) {
        Some(OrderedJson::Str(text)) if !text.is_empty() => fromisoformat_date(text).map(Some),
        _ => Ok(None),
    }
}

fn parse_now(text: &str) -> Option<NaiveDateTime> {
    NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S")
        .or_else(|_| NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M"))
        .ok()
}

async fn run(args: OrderedJson, clock: TaskClock) -> ExitCode {
    let Some(user_id) = sole_user_id() else {
        emit(&pyjson::object([(
            "error",
            pyjson::s(
                "BASKFY_SOLE_USER_ID is not set, and the tw_ schema is keyed by user. Set it, or \
                 run `make seed` to create the development account.",
            ),
        )]));
        return ExitCode::from(2);
    };
    // The script's `--start` default is `04` §12's start; `--sleeve` defaults to its capital.
    let start = match date_arg(&args, "start") {
        Ok(d) => d,
        Err(e) => return raised(&e.python_type(), &e.to_string()),
    };
    let end = match date_arg(&args, "end") {
        Ok(d) => d,
        Err(e) => return raised(&e.python_type(), &e.to_string()),
    };
    let sleeve = match args.get("sleeve") {
        Some(OrderedJson::Str(text)) => match Decimal::from_str(text) {
            Ok(d) => Some(d),
            Err(_) => return raised("InvalidOperation", "[<class 'decimal.ConversionSyntax'>]"),
        },
        _ => None,
    };
    let skip = matches!(
        args.get("skip_etf_denominator"),
        Some(OrderedJson::Bool(true))
    );
    let settings = match Settings::from_env() {
        Ok(s) => s,
        Err(e) => return raised("ValidationError", &e.to_string()),
    };
    let db = match WriteDb::connect_lazy(&settings.db_settings()) {
        Ok(db) => db,
        Err(e) => return raised("OperationalError", &e.to_string()),
    };
    let now = || clock.now();
    let outcome = async {
        let mut tx = db.begin().await?;
        let run = run_twt_backtest(
            &mut tx,
            &now,
            user_id,
            &TwtBacktestArgs {
                start,
                end,
                sleeve_inr: sleeve,
                skip_etf_denominator: skip,
            },
        )
        .await?;
        tx.commit().await?;
        Ok::<_, WorkerError>(run)
    }
    .await;
    db.close().await;
    match outcome {
        Ok(run) => {
            emit(&pyjson::object([
                ("run_id", pyjson::int(run.id)),
                ("user_id", pyjson::int(user_id)),
                ("source", pyjson::s("PLANT")),
                ("started_at", pyjson::s(iso(run.started_at))),
                (
                    "finished_at",
                    run.finished_at
                        .map_or(OrderedJson::Null, |at| pyjson::s(iso(at))),
                ),
                ("params", run.params),
                ("stats", run.stats.unwrap_or(OrderedJson::Null)),
                ("drift", run.drift.unwrap_or(OrderedJson::Null)),
            ]));
            ExitCode::SUCCESS
        }
        Err(e) => raised(&e.python_type(), &e.to_string()),
    }
}

fn main() -> ExitCode {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let mut args = OrderedJson::Object(Vec::new());
    let mut clock = TaskClock::system();
    let mut i = 0;
    while i < raw.len() {
        match (raw[i].as_str(), raw.get(i + 1)) {
            ("--args", Some(text)) => match OrderedJson::parse(text) {
                Ok(v) => args = v,
                Err(e) => {
                    say(&format!("--args: {e}"));
                    return ExitCode::from(2);
                }
            },
            ("--now", Some(text)) => match parse_now(text) {
                Some(now) => clock = TaskClock::starting_at_ist(now),
                None => {
                    say(&format!("--now {text:?} is not YYYY-MM-DDTHH:MM[:SS]"));
                    return ExitCode::from(2);
                }
            },
            (other, _) => {
                say(&format!("unknown argument {other:?}"));
                return ExitCode::from(2);
            }
        }
        i += 2;
    }
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            say(&format!("cannot start the runtime: {e}"));
            return ExitCode::from(2);
        }
    };
    runtime.block_on(run(args, clock))
}
