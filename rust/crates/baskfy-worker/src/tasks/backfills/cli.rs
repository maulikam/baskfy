//! The six backfill CLIs as `baskfy-worker` subcommands, with the Python modules' arguments.

use std::collections::BTreeMap;
use std::io::Write as _;
use std::process::ExitCode;
use std::sync::Arc;

use baskfy_db::{DbSettings, WriteDb};
use baskfy_providers::factory::{KiteLane, RedisSource, build_kite_provider, build_nse_provider};
use baskfy_providers::secret::Secret;
use chrono::{NaiveDate, Utc};

use super::bhavcopy::{self, EQUITY_SERIES, Source};
use super::common::{iso, provider_settings};
use super::{breadth, cursor, deep, index_backfill, index_repair};
use crate::error::WorkerError;
use crate::settings::WorkerSettings;
use crate::window::DateWindow;

/// The subcommands, as `python -m baskfy_worker.<module>` names them (`-` or `_`).
pub const SUBCOMMANDS: [&str; 6] = [
    "backfill",
    "deep_backfill",
    "bhavcopy_backfill",
    "breadth_backfill",
    "index_backfill",
    "index_repair",
];

fn canonical(name: &str) -> Option<&'static str> {
    let wanted = name.replace('-', "_");
    SUBCOMMANDS.iter().copied().find(|s| *s == wanted)
}

/// Is `name` one of the backfill subcommands?
#[must_use]
pub fn is_subcommand(name: &str) -> bool {
    canonical(name).is_some()
}

struct Flags {
    values: BTreeMap<String, String>,
    switches: Vec<String>,
}

impl Flags {
    fn parse(args: &[String], valued: &[&str], switches: &[&str]) -> Result<Self, String> {
        let mut flags = Self {
            values: BTreeMap::new(),
            switches: Vec::new(),
        };
        let mut it = args.iter();
        while let Some(arg) = it.next() {
            let (name, inline) = match arg.split_once('=') {
                Some((n, v)) if n.starts_with("--") => (n.to_owned(), Some(v.to_owned())),
                _ => (arg.clone(), None),
            };
            if switches.contains(&name.as_str()) && inline.is_none() {
                flags.switches.push(name);
            } else if valued.contains(&name.as_str()) {
                let value = match inline {
                    Some(v) => v,
                    None => it
                        .next()
                        .cloned()
                        .ok_or_else(|| format!("argument {name}: expected one argument"))?,
                };
                flags.values.insert(name, value);
            } else {
                return Err(format!("unrecognized arguments: {arg}"));
            }
        }
        Ok(flags)
    }

    fn get(&self, name: &str) -> Option<&str> {
        self.values.get(name).map(String::as_str)
    }

    fn on(&self, name: &str) -> bool {
        self.switches.iter().any(|s| s == name)
    }

    fn date(&self, name: &str) -> Result<Option<NaiveDate>, String> {
        self.get(name)
            .map(|v| {
                NaiveDate::parse_from_str(v, "%Y-%m-%d")
                    .map_err(|_| format!("Invalid isoformat string: '{v}'"))
            })
            .transpose()
    }

    fn required_date(&self, name: &str) -> Result<NaiveDate, String> {
        self.date(name)?
            .ok_or_else(|| format!("the following arguments are required: {name}"))
    }

    fn int(&self, name: &str) -> Result<Option<i64>, String> {
        self.get(name)
            .map(|v| {
                v.trim()
                    .parse::<i64>()
                    .map_err(|_| format!("argument {name}: invalid int value: '{v}'"))
            })
            .transpose()
    }
}

fn split_list(text: &str) -> Vec<String> {
    text.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}

fn today_ist() -> NaiveDate {
    (Utc::now() + chrono::Duration::minutes(330)).date_naive()
}

fn connect(flags: &Flags) -> Result<WriteDb, String> {
    let settings = match flags.get("--database-url") {
        Some(url) => DbSettings::with_url(Secret::new(url)),
        None => baskfy_api::Settings::from_env()
            .map_err(|e| format!("settings: {e}"))?
            .db_settings(),
    };
    WriteDb::connect_lazy(&settings).map_err(|e| format!("database: {e}"))
}

fn say(text: &str) {
    let _ = writeln!(std::io::stdout(), "{text}");
}

fn usage(prog: &str, message: &str) -> ExitCode {
    let _ = writeln!(std::io::stderr(), "baskfy-worker {prog}: error: {message}");
    ExitCode::from(2)
}

fn raised(error: &WorkerError) -> ExitCode {
    let _ = writeln!(std::io::stderr(), "{}: {error}", error.python_type());
    ExitCode::from(1)
}

fn code(ok: bool) -> ExitCode {
    if ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

/// Runs one backfill subcommand: `raw` is the command line after the binary's name.
pub async fn run(raw: &[String]) -> ExitCode {
    let Some(name) = raw.first().and_then(|n| canonical(n)) else {
        return usage("backfill", "unknown subcommand");
    };
    let rest = &raw[1..];
    let outcome = match name {
        "backfill" => run_cursor(rest).await,
        "deep_backfill" => run_deep(rest).await,
        "bhavcopy_backfill" => run_bhavcopy(rest).await,
        "breadth_backfill" => run_breadth(rest).await,
        "index_backfill" => run_index_backfill(rest).await,
        _ => run_index_repair(rest).await,
    };
    match outcome {
        Ok(code) => code,
        Err(Failure::Usage(m)) => usage(name, &m),
        Err(Failure::Raised(e)) => raised(&e),
    }
}

enum Failure {
    Usage(String),
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

type Outcome = Result<ExitCode, Failure>;

async fn composite() -> Result<Arc<baskfy_providers::composite::CompositeProvider>, Failure> {
    crate::tasks::context::provider(None)
        .await
        .map(Arc::new)
        .map_err(|e| Failure::Raised(WorkerError::Settings(e)))
}

fn window(start: NaiveDate, end: NaiveDate) -> Result<DateWindow, Failure> {
    Ok(DateWindow::new(start, end)?)
}

async fn run_cursor(args: &[String]) -> Outcome {
    let flags = Flags::parse(
        args,
        &[
            "--from",
            "--to",
            "--instruments",
            "--concurrency",
            "--chunk-days",
            "--database-url",
        ],
        &["--resume"],
    )?;
    let start = flags.required_date("--from")?;
    let end = flags.date("--to")?.unwrap_or_else(today_ist);
    let instruments = flags.get("--instruments").unwrap_or("all");
    let symbols = if instruments.trim().eq_ignore_ascii_case("all") {
        None
    } else {
        Some(split_list(instruments))
    };
    let concurrency = match flags.int("--concurrency")?.filter(|n| *n != 0) {
        Some(n) => n,
        None => {
            WorkerSettings::from_env()
                .map_err(|e| Failure::Raised(WorkerError::Settings(e.to_string())))?
                .ingest_concurrency
        }
    };
    let chunk_days = flags
        .int("--chunk-days")?
        .unwrap_or(cursor::DEFAULT_CHUNK_DAYS);
    let window = window(start, end)?;
    let db = connect(&flags)?;
    let provider = composite().await?;
    let report = cursor::run_backfill(
        &db,
        provider,
        window,
        cursor::Options {
            symbols: symbols.as_deref(),
            concurrency: usize::try_from(concurrency).unwrap_or(1),
            resume: flags.on("--resume"),
            chunk_days,
            stop_after: None,
        },
    )
    .await;
    db.close().await;
    let report = report?;
    say(&format!(
        "planned {} unit(s); {} already done; completed {}; failed {}; {} bar rows written",
        report.planned, report.already_done, report.completed, report.failed, report.rows_written
    ));
    for error in report.errors.iter().take(20) {
        say(&format!("  error: {error}"));
    }
    Ok(code(report.failed == 0))
}

async fn run_deep(args: &[String]) -> Outcome {
    let flags = Flags::parse(
        args,
        &["--from", "--to", "--symbols", "--limit", "--database-url"],
        &["--write"],
    )?;
    let start = flags.date("--from")?.unwrap_or_else(deep::default_start);
    let end = flags.date("--to")?.unwrap_or_else(today_ist);
    let symbols = split_list(flags.get("--symbols").unwrap_or(""));
    let limit = flags.int("--limit")?;
    let write = flags.on("--write");
    let db = connect(&flags)?;
    let provider = composite().await?;
    let symbols = (!symbols.is_empty()).then_some(symbols);
    let report = deep::run(&db, &provider, write, start, end, symbols.as_deref(), limit).await;
    db.close().await;
    let report = report?;
    say(&deep::render(&report, write));
    Ok(code(report.errors.is_empty()))
}

async fn run_bhavcopy(args: &[String]) -> Outcome {
    let flags = Flags::parse(args, &["--from", "--to", "--series", "--database-url"], &[])?;
    let start = flags.required_date("--from")?;
    let end = flags.date("--to")?.unwrap_or_else(today_ist);
    let series = match flags.get("--series") {
        Some(s) => split_list(s),
        None => EQUITY_SERIES.iter().map(|s| (*s).to_owned()).collect(),
    };
    if let Some(other) = series.iter().find(|s| !EQUITY_SERIES.contains(&s.as_str())) {
        return Err(Failure::Usage(format!(
            "--series {other}: only the equity series {} are written",
            EQUITY_SERIES.join(",")
        )));
    }
    let window = window(start, end)?;
    let db = connect(&flags)?;
    let provider = composite().await?;
    let report = bhavcopy::backfill_bars_from_bhavcopy(
        &db,
        &Source::Composite(&provider),
        window,
        &series,
        25,
    )
    .await;
    db.close().await;
    let report = report?;
    say(&format!(
        "{} trading day(s) in {window}; {} written; {} bars; {} day(s) with no bhavcopy; \
         {} unmatched symbol(s)",
        report.trading_days,
        report.days_written,
        report.bars_written,
        report.missing_days.len(),
        report.unmatched_symbols.len()
    ));
    if let (Some(first), Some(last)) = (report.missing_days.first(), report.missing_days.last()) {
        say(&format!(
            "  no bhavcopy published between {} and {}",
            iso(*first),
            iso(*last)
        ));
    }
    for (day, error) in report.failures.iter().take(20) {
        let text = match error {
            baskfy_db::OrderedJson::Str(s) => s.clone(),
            other => other.to_python_json().unwrap_or_default(),
        };
        say(&format!("  {day}: {text}"));
    }
    Ok(code(report.succeeded()))
}

async fn run_breadth(args: &[String]) -> Outcome {
    let flags = Flags::parse(
        args,
        &["--from", "--to", "--every", "--database-url"],
        &["--write"],
    )?;
    let start = flags.required_date("--from")?;
    let end = flags.date("--to")?.unwrap_or_else(today_ist);
    let every = flags.int("--every")?.map_or(breadth::DEFAULT_EVERY, |n| {
        usize::try_from(n.max(1)).unwrap_or(1)
    });
    let write = flags.on("--write");
    let db = connect(&flags)?;
    let report = breadth::run(&db, write, start, end, every).await;
    db.close().await;
    let report = report?;
    say(&breadth::render(&report, write));
    Ok(code(report.errors.is_empty()))
}

async fn run_index_backfill(args: &[String]) -> Outcome {
    let flags = Flags::parse(args, &["--from", "--to", "--database-url"], &["--write"])?;
    let start = flags
        .date("--from")?
        .unwrap_or_else(index_backfill::default_start)
        .max(deep::kite_epoch());
    let end = flags.date("--to")?.unwrap_or_else(today_ist);
    let write = flags.on("--write");
    let db = connect(&flags)?;
    let provider = composite().await?;
    let settings = provider_settings()?;
    let kite = build_kite_provider(
        &RedisSource::from_settings(&settings),
        &settings,
        None,
        KiteLane::Bulk,
    )
    .await
    .map_err(WorkerError::from)?;
    let indices =
        index_backfill::kite_indices(&kite.list_instruments().await.map_err(WorkerError::from)?);
    let report = index_backfill::run(&db, &provider, &indices, write, start, end).await;
    db.close().await;
    let report = report?;
    say(&index_backfill::render(&report, write));
    Ok(code(report.errors.is_empty()))
}

async fn run_index_repair(args: &[String]) -> Outcome {
    let flags = Flags::parse(
        args,
        &["--from", "--to", "--slugs", "--database-url"],
        &["--dry-run"],
    )?;
    let window = window(flags.required_date("--from")?, flags.required_date("--to")?)?;
    let slugs = flags
        .get("--slugs")
        .map(split_list)
        .filter(|v| !v.is_empty());
    let db = connect(&flags)?;
    let settings = provider_settings()?;
    let nse = build_nse_provider(
        &RedisSource::from_settings(&settings),
        &settings,
        None,
        None,
    )
    .await
    .map_err(WorkerError::from)?;
    let report = index_repair::repair_index_snapshots(
        &db,
        &nse,
        window,
        slugs.as_deref(),
        flags.on("--dry-run"),
    )
    .await;
    db.close().await;
    let report = report?;
    say(&index_repair::format_report(&report));
    Ok(code(report.succeeded()))
}
