//! `baskfy_worker.fno_cli` as a subcommand: seed, backfill, ingest, scan, retest, index-daily.

mod seed;

use std::path::PathBuf;

use baskfy_db::OrderedJson;
use baskfy_providers::factory::{KiteFamily, RedisSource, build_archive, build_nse_provider};
use baskfy_providers::settings::ProviderSettings;
use chrono::NaiveDate;
use sqlx::PgConnection;

use super::cli::{
    Failure, Outcome, Parsed, Spec, command, context, emit, iso, now_ist, parse_date, today_ist,
};
use crate::error::WorkerError;
use crate::tasks::context::sole_user_id;
use crate::tasks::fno::{backfill, common, index_daily, nightly, retest, scan, underlying};

const CHOICES: [&str; 6] = [
    "seed",
    "backfill",
    "ingest",
    "scan",
    "retest",
    "index-daily",
];

const SPEC: Spec = Spec {
    valued: &["--from", "--to", "--seed-archive", "--date", "--families"],
    switches: &["--force"],
    optional: &[],
    positionals: 1,
};

/// `Path(text).expanduser()`.
pub(super) fn expand_user(text: &str) -> PathBuf {
    match text.strip_prefix('~') {
        Some(rest) if rest.is_empty() || rest.starts_with('/') => {
            let home = std::env::var("HOME").unwrap_or_default();
            PathBuf::from(format!("{home}{rest}"))
        }
        _ => PathBuf::from(text),
    }
}

/// `[f.strip() for f in families.split(",")]`.
pub(super) fn families(text: Option<&str>) -> Option<Vec<String>> {
    text.filter(|t| !t.is_empty())
        .map(|t| t.split(',').map(|f| f.trim().to_owned()).collect())
}

fn date_or(parsed: &Parsed, name: &str, default: NaiveDate) -> Result<NaiveDate, Failure> {
    parsed
        .get(name)
        .filter(|v| !v.is_empty())
        .map_or(Ok(default), parse_date)
}

fn object(head: Vec<(String, OrderedJson)>, tail: Vec<(String, OrderedJson)>) -> OrderedJson {
    let mut out = head;
    for (k, v) in tail {
        crate::pyjson::update(&mut out, &k, v);
    }
    OrderedJson::Object(out)
}

fn provider_settings() -> Result<ProviderSettings, Failure> {
    ProviderSettings::from_env().map_err(|e| Failure::Raised(WorkerError::Settings(e.to_string())))
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let parsed = Parsed::parse(args, SPEC)?;
    let (cmd, _) = command(&parsed.positionals, &CHOICES)?;
    let today = today_ist();
    let result = match cmd {
        "seed" => run_seed().await?,
        "backfill" => {
            let start = date_or(&parsed, "--from", backfill::backfill_start())?;
            let end = date_or(&parsed, "--to", today)?;
            if end < start {
                return Err(Failure::Usage("--to is before --from".to_owned()));
            }
            let seed_dir = parsed
                .get("--seed-archive")
                .filter(|v| !v.is_empty())
                .map(expand_user);
            if let Some(dir) = seed_dir.as_ref().filter(|d| !d.is_dir()) {
                return Err(Failure::Usage(format!(
                    "--seed-archive {} is not a directory",
                    dir.display()
                )));
            }
            run_backfill(start, end, seed_dir).await?
        }
        "retest" => {
            let families = families(parsed.get("--families"));
            let ctx = context()?;
            let mut tx = ctx.db.begin().await?;
            let out = retest::run_retest(&mut tx, today, parsed.on("--force"), families).await?;
            tx.commit().await?;
            ctx.db.close().await;
            out
        }
        "scan" => {
            let day = date_or(&parsed, "--date", today)?;
            let ctx = context()?;
            let mut tx = ctx.db.begin().await?;
            let out = scan::run_scan(&mut tx, day, None).await?;
            tx.commit().await?;
            ctx.db.close().await;
            out
        }
        "index-daily" => {
            let from = parsed
                .get("--from")
                .filter(|v| !v.is_empty())
                .map(parse_date)
                .transpose()?;
            let to = date_or(&parsed, "--to", today)?;
            let ctx = context()?;
            let kite = common::kite_family(&ctx, KiteFamily::Historical).await?;
            let mut tx = ctx.db.begin().await?;
            let report = match from {
                Some(start) => index_daily::backfill(&mut tx, &kite, start, to).await?,
                None => index_daily::extend(&mut tx, &kite, to).await?,
            };
            tx.commit().await?;
            ctx.db.close().await;
            object(Vec::new(), report.entries())
        }
        _ => {
            let day = date_or(&parsed, "--date", today)?;
            let provider = common::nse_provider().await?;
            let ctx = context()?;
            let mut tx = ctx.db.begin().await?;
            let out = nightly::run_night(&mut tx, &provider, day, now_ist()).await?;
            tx.commit().await?;
            ctx.db.close().await;
            out
        }
    };
    emit(&result);
    Ok(std::process::ExitCode::SUCCESS)
}

async fn run_seed() -> Result<OrderedJson, Failure> {
    let Some(user_id) = sole_user_id() else {
        return Ok(object(
            vec![(
                "skipped".to_owned(),
                OrderedJson::Str("no BASKFY_SOLE_USER_ID configured".to_owned()),
            )],
            Vec::new(),
        ));
    };
    let ctx = context()?;
    let mut tx = ctx.db.begin().await?;
    let written = seed::seed_fno(&mut tx, user_id).await?;
    tx.commit().await?;
    ctx.db.close().await;
    Ok(object(
        vec![("user_id".to_owned(), OrderedJson::Int(user_id))],
        written,
    ))
}

/// `session.commit()` inside a checkpointed session: the work so far is durable, a new
/// transaction begins.
pub(super) async fn checkpoint(conn: &mut PgConnection) -> Result<(), WorkerError> {
    sqlx::query("COMMIT").execute(&mut *conn).await?;
    sqlx::query("BEGIN").execute(&mut *conn).await?;
    Ok(())
}

async fn run_backfill(
    start: NaiveDate,
    end: NaiveDate,
    seed_dir: Option<PathBuf>,
) -> Result<OrderedJson, Failure> {
    let settings = provider_settings()?;
    let archive = build_archive(&settings, None).map_err(WorkerError::from)?;
    let provider = build_nse_provider(
        &RedisSource::from_settings(&settings),
        &settings,
        Some(archive.clone()),
        None,
    )
    .await
    .map_err(WorkerError::from)?;
    let ctx = context()?;
    let mut tx = ctx.db.begin().await?;
    let days = underlying::trading_days(&mut tx, start, end).await?;
    let seed = seed_dir.as_deref().map(|dir| (archive.as_ref(), dir));
    let report = backfill::backfill_days(&mut tx, &provider, &days, seed, now_ist(), async |c| {
        checkpoint(c).await
    })
    .await?;
    tx.commit().await?;
    ctx.db.close().await;
    Ok(object(
        vec![
            ("from".to_owned(), OrderedJson::Str(iso(start))),
            ("to".to_owned(), OrderedJson::Str(iso(end))),
        ],
        match report.as_dict() {
            OrderedJson::Object(entries) => entries,
            _ => Vec::new(),
        },
    ))
}

#[cfg(test)]
mod db_tests;

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn families_are_split_and_stripped_as_python_does() {
        assert_eq!(families(None), None);
        assert_eq!(families(Some("")), None);
        assert_eq!(
            families(Some("f1, f3 ,")),
            Some(vec!["f1".to_owned(), "f3".to_owned(), String::new()])
        );
    }

    #[test]
    fn expand_user_reads_home() {
        let home = std::env::var("HOME").unwrap_or_default();
        assert_eq!(expand_user("~/x"), PathBuf::from(format!("{home}/x")));
        assert_eq!(expand_user("/a/~b"), PathBuf::from("/a/~b"));
    }

    #[tokio::test]
    async fn an_unknown_command_is_a_usage_error() {
        let Err(Failure::Usage(m)) = run(&args(&["trade"])).await else {
            panic!("an unknown command must be refused by argparse");
        };
        assert!(m.starts_with("argument command: invalid choice: 'trade'"));
    }

    #[tokio::test]
    async fn backfill_refuses_an_inverted_range_before_any_read() {
        let Err(Failure::Usage(m)) = run(&args(&[
            "backfill",
            "--from",
            "2026-09-10",
            "--to",
            "2026-09-01",
        ]))
        .await
        else {
            panic!("an inverted range must be refused");
        };
        assert_eq!(m, "--to is before --from");
    }

    #[tokio::test]
    async fn backfill_refuses_a_seed_archive_that_is_not_a_directory() {
        let Err(Failure::Usage(m)) = run(&args(&[
            "backfill",
            "--from",
            "2026-09-01",
            "--to",
            "2026-09-02",
            "--seed-archive",
            "/nonexistent/fno-archive",
        ]))
        .await
        else {
            panic!("a missing seed archive must be refused");
        };
        assert_eq!(
            m,
            "--seed-archive /nonexistent/fno-archive is not a directory"
        );
    }

    #[tokio::test]
    async fn a_bad_date_is_pythons_value_error() {
        let Err(Failure::Exit(m)) = run(&args(&["scan", "--date", "2026-13-01"])).await else {
            panic!("a bad --date must raise");
        };
        assert_eq!(m, "ValueError: Invalid isoformat string: '2026-13-01'");
    }
}
