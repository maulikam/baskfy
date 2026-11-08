//! The worker's command-line modules as `baskfy-worker` subcommands, with Python's arguments.

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write as _;
use std::process::ExitCode;

use baskfy_api::Settings;
use baskfy_db::{DbSettings, OrderedJson, WriteDb};
use baskfy_providers::secret::Secret;
use chrono::{NaiveDate, NaiveDateTime, Utc};

use super::{
    cb_metrics, eq_bars, factors, fno, fundamentals, kite_session, options, pipeline, reconcile,
    reference_backfill, swing, twt, vbt,
};
use crate::error::WorkerError;
use crate::settings::WorkerSettings;
use crate::tasks::context::{TaskClock, TaskContext};

/// The subcommands, as `python -m baskfy_worker.<module>` names them (`-`/`_`, `_cli` optional).
pub const SUBCOMMANDS: [&str; 13] = [
    "cb_metrics",
    "eq_bars",
    "factors",
    "fno",
    "fundamentals",
    "kite_session",
    "options",
    "pipeline",
    "reconcile",
    "reference_backfill",
    "swing",
    "twt",
    "vbt",
];

fn canonical(name: &str) -> Option<&'static str> {
    let wanted = name.replace('-', "_");
    let wanted = wanted.strip_suffix("_cli").unwrap_or(&wanted);
    SUBCOMMANDS.iter().copied().find(|s| *s == wanted)
}

/// Is `name` one of these subcommands?
#[must_use]
pub fn is_subcommand(name: &str) -> bool {
    canonical(name).is_some()
}

/// Runs one subcommand: `raw` is the command line after the binary's name.
pub async fn run(raw: &[String]) -> ExitCode {
    let Some(name) = raw.first().and_then(|n| canonical(n)) else {
        return usage("baskfy-worker", "unknown subcommand");
    };
    let rest = &raw[1..];
    let outcome = match name {
        "cb_metrics" => cb_metrics::run(rest).await,
        "eq_bars" => eq_bars::run(rest).await,
        "factors" => factors::run(rest).await,
        "fno" => fno::run(rest).await,
        "fundamentals" => fundamentals::run(rest).await,
        "kite_session" => kite_session::run(rest).await,
        "options" => options::run(rest).await,
        "pipeline" => pipeline::run(rest).await,
        "reconcile" => reconcile::run(rest).await,
        "reference_backfill" => reference_backfill::run(rest).await,
        "swing" => swing::run(rest).await,
        "twt" => twt::run(rest).await,
        _ => vbt::run(rest).await,
    };
    match outcome {
        Ok(code) => code,
        Err(Failure::Usage(m)) => usage(name, &m),
        Err(Failure::Exit(m)) => {
            let _ = writeln!(std::io::stderr(), "{m}");
            ExitCode::from(1)
        }
        Err(Failure::Raised(e)) => {
            let _ = writeln!(std::io::stderr(), "{}: {e}", e.python_type());
            ExitCode::from(1)
        }
    }
}

/// How a subcommand fails: argparse's exit 2, `SystemExit(msg)`'s exit 1, a raise's exit 1.
#[derive(Debug)]
pub(crate) enum Failure {
    Usage(String),
    Exit(String),
    Raised(WorkerError),
}

impl From<String> for Failure {
    fn from(m: String) -> Self {
        Self::Usage(m)
    }
}

impl From<WorkerError> for Failure {
    fn from(e: WorkerError) -> Self {
        Self::Raised(e)
    }
}

impl From<baskfy_db::DbError> for Failure {
    fn from(e: baskfy_db::DbError) -> Self {
        Self::Raised(WorkerError::from(e))
    }
}

impl From<sqlx::Error> for Failure {
    fn from(e: sqlx::Error) -> Self {
        Self::Raised(WorkerError::from(e))
    }
}

pub(crate) type Outcome = Result<ExitCode, Failure>;

fn usage(prog: &str, message: &str) -> ExitCode {
    let _ = writeln!(std::io::stderr(), "baskfy-worker {prog}: error: {message}");
    ExitCode::from(2)
}

/// Exit 0 when `ok`, 1 otherwise.
pub(crate) fn code(ok: bool) -> ExitCode {
    if ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

/// `print(text)`.
pub(crate) fn say(text: &str) {
    let _ = writeln!(std::io::stdout(), "{text}");
}

/// `print(text, file=sys.stderr)`.
pub(crate) fn warn(text: &str) {
    let _ = writeln!(std::io::stderr(), "{text}");
}

/// `dt.date.isoformat()`.
pub(crate) fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `dt.datetime.now(tz=IST).date()`.
pub(crate) fn today_ist() -> NaiveDate {
    now_ist().date()
}

/// `dt.datetime.now(tz=IST)`, naive.
pub(crate) fn now_ist() -> NaiveDateTime {
    (Utc::now() + chrono::Duration::minutes(330)).naive_utc()
}

/// `dt.date.fromisoformat(text)`, with Python's message.
pub(crate) fn parse_date(text: &str) -> Result<NaiveDate, Failure> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .map_err(|_| Failure::Exit(format!("ValueError: Invalid isoformat string: '{text}'")))
}

/// Recursively sorts object keys (`json.dumps(sort_keys=True)`).
#[must_use]
pub(crate) fn sort_keys(value: OrderedJson) -> OrderedJson {
    match value {
        OrderedJson::Object(entries) => {
            let mut sorted: Vec<(String, OrderedJson)> = entries
                .into_iter()
                .map(|(k, v)| (k, sort_keys(v)))
                .collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            OrderedJson::Object(sorted)
        }
        OrderedJson::Array(items) => OrderedJson::Array(items.into_iter().map(sort_keys).collect()),
        other => other,
    }
}

/// `json.dumps(value, indent=2)` — Python's separators and `ensure_ascii=True`.
#[must_use]
pub(crate) fn pretty(value: &OrderedJson) -> String {
    crate::pyjson::dumps_indent2(value)
        .unwrap_or_else(|e| format!("{{\"error\": \"unprintable payload: {e}\"}}"))
}

/// `print(json.dumps(value, indent=2, default=str))`.
pub(crate) fn emit(value: &OrderedJson) {
    say(&pretty(value));
}

/// The database: `--database-url` when given, else `BASKFY_DATABASE_URL` (Python's settings).
pub(crate) fn connect(database_url: Option<&str>) -> Result<WriteDb, Failure> {
    let settings = match database_url {
        Some(url) => DbSettings::with_url(Secret::new(url)),
        None => Settings::from_env()
            .map_err(|e| Failure::Raised(WorkerError::Settings(e.to_string())))?
            .db_settings(),
    };
    WriteDb::connect_lazy(&settings).map_err(Failure::from)
}

/// A task context over the environment's database and settings, the system clock, no replay.
pub(crate) fn context() -> Result<TaskContext, Failure> {
    let settings =
        Settings::from_env().map_err(|e| Failure::Raised(WorkerError::Settings(e.to_string())))?;
    let worker = WorkerSettings::from_env()?;
    let db = WriteDb::connect_lazy(&settings.db_settings())?;
    Ok(TaskContext::new(
        db,
        settings,
        worker,
        TaskClock::system(),
        None,
        None,
    ))
}

/// One subparser's flags, as argparse declares them.
#[derive(Debug, Default, Clone, Copy)]
pub(crate) struct Spec {
    /// `--flag VALUE`.
    pub valued: &'static [&'static str],
    /// `--flag` (`store_true`).
    pub switches: &'static [&'static str],
    /// `--flag [VALUE]` (`nargs="?"`), with its `const`.
    pub optional: &'static [(&'static str, &'static str)],
    /// How many positionals the parser takes.
    pub positionals: usize,
}

/// A parsed command line.
#[derive(Debug, Default)]
pub(crate) struct Parsed {
    values: BTreeMap<String, String>,
    switches: BTreeSet<String>,
    /// The positionals, in order.
    pub positionals: Vec<String>,
}

impl Parsed {
    /// Parses `args` against `spec`, with argparse's messages.
    pub(crate) fn parse(args: &[String], spec: Spec) -> Result<Self, Failure> {
        let mut parsed = Self::default();
        let mut i = 0;
        let mut extra: Vec<String> = Vec::new();
        while let Some(arg) = args.get(i) {
            i += 1;
            if !arg.starts_with("--") || arg == "--" {
                if parsed.positionals.len() < spec.positionals {
                    parsed.positionals.push(arg.clone());
                } else {
                    extra.push(arg.clone());
                }
                continue;
            }
            let (name, inline) = match arg.split_once('=') {
                Some((n, v)) => (n.to_owned(), Some(v.to_owned())),
                None => (arg.clone(), None),
            };
            if spec.switches.contains(&name.as_str()) {
                if inline.is_some() {
                    return Err(Failure::Usage(format!(
                        "argument {name}: ignored explicit argument '{}'",
                        inline.unwrap_or_default()
                    )));
                }
                parsed.switches.insert(name);
            } else if spec.valued.contains(&name.as_str()) {
                let value = match inline {
                    Some(v) => v,
                    None => match args.get(i) {
                        Some(v) if !v.starts_with("--") => {
                            i += 1;
                            v.clone()
                        }
                        _ => {
                            return Err(Failure::Usage(format!(
                                "argument {name}: expected one argument"
                            )));
                        }
                    },
                };
                parsed.values.insert(name, value);
            } else if let Some((_, constant)) = spec.optional.iter().find(|(f, _)| *f == name) {
                let value = match inline {
                    Some(v) => v,
                    None => match args.get(i) {
                        Some(v) if !v.starts_with('-') => {
                            i += 1;
                            v.clone()
                        }
                        _ => (*constant).to_owned(),
                    },
                };
                parsed.values.insert(name, value);
            } else {
                extra.push(arg.clone());
            }
        }
        if !extra.is_empty() {
            return Err(Failure::Usage(format!(
                "unrecognized arguments: {}",
                extra.join(" ")
            )));
        }
        Ok(parsed)
    }

    /// The value of a valued or optional flag.
    pub(crate) fn get(&self, name: &str) -> Option<&str> {
        self.values.get(name).map(String::as_str)
    }

    /// Was the switch given?
    pub(crate) fn on(&self, name: &str) -> bool {
        self.switches.contains(name)
    }

    /// A required flag's value, with argparse's message.
    pub(crate) fn required(&self, name: &str) -> Result<&str, Failure> {
        self.get(name)
            .ok_or_else(|| Failure::Usage(format!("the following arguments are required: {name}")))
    }

    /// `type=dt.date.fromisoformat`: argparse's message on a bad value.
    pub(crate) fn date(&self, name: &str) -> Result<Option<NaiveDate>, Failure> {
        self.get(name)
            .map(|v| {
                NaiveDate::parse_from_str(v, "%Y-%m-%d").map_err(|_| {
                    Failure::Usage(format!(
                        "argument {name}: invalid fromisoformat value: '{v}'"
                    ))
                })
            })
            .transpose()
    }

    /// `type=int`: argparse's message on a bad value.
    pub(crate) fn int(&self, name: &str) -> Result<Option<i64>, Failure> {
        self.get(name)
            .map(|v| {
                v.trim().parse::<i64>().map_err(|_| {
                    Failure::Usage(format!("argument {name}: invalid int value: '{v}'"))
                })
            })
            .transpose()
    }
}

/// Splits `[command, rest...]` against argparse's `choices`.
pub(crate) fn command<'a>(
    args: &'a [String],
    choices: &[&str],
) -> Result<(&'a str, &'a [String]), Failure> {
    let quoted = || {
        choices
            .iter()
            .map(|c| format!("'{c}'"))
            .collect::<Vec<_>>()
            .join(", ")
    };
    let Some(first) = args.first() else {
        return Err(Failure::Usage(
            "the following arguments are required: command".to_owned(),
        ));
    };
    if !choices.contains(&first.as_str()) {
        return Err(Failure::Usage(format!(
            "argument command: invalid choice: '{first}' (choose from {})",
            quoted()
        )));
    }
    Ok((first.as_str(), &args[1..]))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn subcommand_names_accept_module_spellings() {
        assert!(is_subcommand("factors_cli"));
        assert!(is_subcommand("kite-session"));
        assert!(is_subcommand("reference_backfill"));
        assert!(!is_subcommand("nightly"));
        assert!(!is_subcommand("backfill"));
    }

    #[test]
    fn flags_parse_as_argparse_does() {
        let spec = Spec {
            valued: &["--date", "--sessions"],
            switches: &["--force"],
            optional: &[("--plan", "EVENING")],
            positionals: 0,
        };
        let p = Parsed::parse(&args(&["--date=2026-09-01", "--force", "--plan"]), spec).unwrap();
        assert_eq!(p.get("--date"), Some("2026-09-01"));
        assert!(p.on("--force"));
        assert_eq!(p.get("--plan"), Some("EVENING"));
        let p = Parsed::parse(&args(&["--plan", "MORNING", "--sessions", "3"]), spec).unwrap();
        assert_eq!(p.get("--plan"), Some("MORNING"));
        assert_eq!(p.int("--sessions").unwrap(), Some(3));
        let Err(Failure::Usage(m)) = Parsed::parse(&args(&["--bogus"]), spec) else {
            panic!("an unknown flag must be a usage error");
        };
        assert_eq!(m, "unrecognized arguments: --bogus");
        let Err(Failure::Usage(m)) = Parsed::parse(&args(&["--date"]), spec) else {
            panic!("a flag without its value must be a usage error");
        };
        assert_eq!(m, "argument --date: expected one argument");
        let p = Parsed::parse(&args(&["--sessions", "x"]), spec).unwrap();
        assert!(matches!(p.int("--sessions"), Err(Failure::Usage(_))));
    }

    #[test]
    fn command_refuses_an_unknown_choice() {
        let a = args(&["scan", "--date", "2026-09-01"]);
        let (cmd, rest) = command(&a, &["seed", "scan"]).unwrap();
        assert_eq!(cmd, "scan");
        assert_eq!(rest.len(), 2);
        let Err(Failure::Usage(m)) = command(&args(&["nope"]), &["seed", "scan"]) else {
            panic!("an unknown command must be a usage error");
        };
        assert_eq!(
            m,
            "argument command: invalid choice: 'nope' (choose from 'seed', 'scan')"
        );
    }

    #[test]
    fn pretty_is_pythons_indent_two_with_ascii_escapes() {
        let value = OrderedJson::Object(vec![
            ("b".to_owned(), OrderedJson::Int(1)),
            (
                "a".to_owned(),
                OrderedJson::Array(vec![OrderedJson::Str("₹ 10".to_owned())]),
            ),
            ("e".to_owned(), OrderedJson::Object(Vec::new())),
        ]);
        assert_eq!(
            pretty(&value),
            "{\n  \"b\": 1,\n  \"a\": [\n    \"\\u20b9 10\"\n  ],\n  \"e\": {}\n}"
        );
        assert_eq!(
            pretty(&sort_keys(value)),
            "{\n  \"a\": [\n    \"\\u20b9 10\"\n  ],\n  \"b\": 1,\n  \"e\": {}\n}"
        );
    }
}
