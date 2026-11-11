//! Lane **mb** (PM6) — the MB sleeve's scorer and trainer: `machinelearning/python/research/portfolio-ml/mb_task.py`
//! run as a subprocess (DECISIONS-PM PM6.1). The script writes only `mb_model` / `mb_score_daily`.
//!
//! **Law 2 / Track C.** Read-only sleeve: nothing here builds a plan, calls the gateway or reads
//! an execution flag, and there is no flag to read.

use std::path::Path;
use std::time::Duration;

use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use super::args::fromisoformat_date;
use super::{BoundArgs, TaskContext, TaskResult};
use crate::error::WorkerError;
use crate::pyjson::{object, s};

/// The tasks this lane has ported.
pub const PORTED: &[&str] = &["baskfy.mb.catch_up", "baskfy.mb.score", "baskfy.mb.train"];

/// How far back `baskfy.mb.catch_up` looks for a published session with no scores.
pub const CATCH_UP_DAYS: i64 = 7;

/// The image's entry point (`deploy/Dockerfile.rust`); `BASKFY_MB_RUN` points elsewhere in tests.
pub const MB_RUN: &str = "/opt/mb/run";
const SCORE_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const TRAIN_TIMEOUT: Duration = Duration::from_secs(4 * 60 * 60);

/// What one run of the script did. Never an error: a failure is a value the caller reports.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MbOutcome {
    /// `succeeded`, `skipped` (exit 3, or no runtime in this image), `held` (exit 4) or `failed`.
    pub status: &'static str,
    /// The script's last line (stdout, or stderr on a failure).
    pub detail: String,
}

impl MbOutcome {
    /// As the task's return value.
    #[must_use]
    pub fn json(&self) -> OrderedJson {
        object([
            ("status", s(self.status)),
            ("detail", s(self.detail.clone())),
        ])
    }
}

fn program() -> String {
    std::env::var("BASKFY_MB_RUN").unwrap_or_else(|_| MB_RUN.to_owned())
}

fn last_line(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .lines()
        .rev()
        .find(|l| !l.trim().is_empty())
        .unwrap_or("")
        .to_owned()
}

/// Runs `program args...` with a deadline; the child is killed if the deadline passes.
pub async fn run_mb(program: &str, args: &[String], limit: Duration) -> MbOutcome {
    if !Path::new(program).exists() {
        return MbOutcome {
            status: "skipped",
            detail: format!("no MB runtime at {program}"),
        };
    }
    let child = tokio::process::Command::new(program)
        .args(args)
        .kill_on_drop(true)
        .output();
    match tokio::time::timeout(limit, child).await {
        Err(_) => MbOutcome {
            status: "failed",
            detail: format!("timed out after {}s", limit.as_secs()),
        },
        Ok(Err(e)) => MbOutcome {
            status: "failed",
            detail: format!("could not start {program}: {e}"),
        },
        Ok(Ok(out)) => match out.status.code() {
            Some(0) => MbOutcome {
                status: "succeeded",
                detail: last_line(&out.stdout),
            },
            Some(3) => MbOutcome {
                status: "skipped",
                detail: last_line(&out.stdout),
            },
            Some(4) => MbOutcome {
                status: "held",
                detail: last_line(&out.stdout),
            },
            code => MbOutcome {
                status: "failed",
                detail: format!("exit {code:?}: {}", last_line(&out.stderr)),
            },
        },
    }
}

/// The nightly's post-commit step: scores `day` once its data is committed. Cannot fail the night.
pub async fn score_after_publish(day: NaiveDate) -> MbOutcome {
    let args = [
        "score".to_owned(),
        "--date".to_owned(),
        day.format("%Y-%m-%d").to_string(),
    ];
    let out = run_mb(&program(), &args, SCORE_TIMEOUT).await;
    if out.status == "failed" {
        crate::steps::log_warning(&format!("mb score failed for {day}: {}", out.detail));
    }
    out
}

/// `baskfy.mb.score(trade_date=None)`: re-score a day by hand (the nightly does this itself).
///
/// # Errors
/// `ValueError` for a date that does not parse.
pub async fn mb_score_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = match args.opt_str("trade_date")? {
        Some(text) if !text.is_empty() => fromisoformat_date(&text)?,
        _ => ctx.today_ist(),
    };
    Ok(score_after_publish(day).await.json())
}

/// `baskfy.mb.catch_up()`: hourly. Scores every session of the last week that published but has no
/// `mb_score_daily` rows (a night the laptop slept through, or a scorer that failed), oldest first.
/// Nothing to do while no model is published.
///
/// # Errors
/// The database's, on the read.
pub async fn mb_catch_up_task(ctx: &TaskContext, _args: BoundArgs) -> TaskResult {
    let mut tx = ctx.db.begin().await?;
    let days: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT DISTINCT r.trade_date FROM pipeline_run r \
         WHERE r.data_version IS NOT NULL AND r.trade_date >= $1::DATE \
         AND EXISTS (SELECT 1 FROM mb_model WHERE published_at IS NOT NULL) \
         AND NOT EXISTS (SELECT 1 FROM mb_score_daily s WHERE s.date = r.trade_date) \
         ORDER BY r.trade_date",
    )
    .bind(ctx.today_ist() - chrono::Duration::days(CATCH_UP_DAYS))
    .fetch_all(&mut *tx)
    .await
    .map_err(|e| WorkerError::python("DatabaseError", e.to_string()))?;
    tx.commit()
        .await
        .map_err(|e| WorkerError::python("DatabaseError", e.to_string()))?;
    let mut done = Vec::with_capacity(days.len());
    for day in days {
        let out = score_after_publish(day).await;
        done.push(object([
            ("date", s(day.format("%Y-%m-%d").to_string())),
            ("status", s(out.status)),
            ("detail", s(out.detail)),
        ]));
    }
    Ok(object([("scored", OrderedJson::Array(done))]))
}

/// `baskfy.mb.train()`: Beat, first Saturday of each quarter. A model whose ranking moves too far
/// (Spearman < 0.7 against the published one) is held for an operator's `mb_task.py publish`.
///
/// # Errors
/// None of its own; a failed run is the returned `status`.
pub async fn mb_train_task(_ctx: &TaskContext, _args: BoundArgs) -> TaskResult {
    let out = run_mb(&program(), &["train".to_owned()], TRAIN_TIMEOUT).await;
    if out.status == "failed" {
        return Err(WorkerError::python(
            "RuntimeError",
            format!("mb train: {}", out.detail),
        ));
    }
    // PM11: the swing-picks model retrains on the same quarter.
    let rr = run_mb(&program(), &["rr-train".to_owned()], TRAIN_TIMEOUT).await;
    if rr.status == "failed" {
        return Err(WorkerError::python(
            "RuntimeError",
            format!("rr train: {}", rr.detail),
        ));
    }
    Ok(object([("mb", out.json()), ("rr", rr.json())]))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn script(dir: &Path, body: &str) -> String {
        use std::os::unix::fs::PermissionsExt as _;
        let p = dir.join("run");
        std::fs::write(&p, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
        p.to_string_lossy().into_owned()
    }

    #[tokio::test]
    async fn exit_codes_map_to_outcomes_and_never_raise() {
        let dir = std::env::temp_dir().join(format!("mb-run-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let none = run_mb("/nonexistent/mb/run", &[], Duration::from_secs(5)).await;
        assert_eq!(none.status, "skipped");
        for (body, status, detail) in [
            ("echo scored 812; exit 0", "succeeded", "scored 812"),
            (
                "echo no published mb_model; exit 3",
                "skipped",
                "no published mb_model",
            ),
            ("echo HELD; exit 4", "held", "HELD"),
            ("echo boom >&2; exit 1", "failed", "exit Some(1): boom"),
        ] {
            let out = run_mb(&script(&dir, body), &[], Duration::from_secs(5)).await;
            assert_eq!(
                (out.status, out.detail.as_str()),
                (status, detail),
                "{body}"
            );
        }
        let slow = run_mb(&script(&dir, "sleep 5"), &[], Duration::from_millis(200)).await;
        assert_eq!(slow.status, "failed");
    }
}
