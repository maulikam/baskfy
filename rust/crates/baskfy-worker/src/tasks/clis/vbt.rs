//! `baskfy_worker.vbt_cli`: VBT detection for a date (or the last N sessions), `--plan
//! [EVENING|MORNING]` (VB6, places nothing), or `--backtest` (VB9, appends a run row).

use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Timelike as _, Utc};

use super::cli::{Failure, Outcome, Parsed, Spec, code, context, emit, iso, parse_date};
use crate::steps::StepOutcome;
use crate::steps::sleeves::for_vbt_jobs::run_detect_vbt_for;
use crate::tasks::swing_eod::recent_trading_days;
use crate::tasks::vbt_jobs::evening::{
    EveningInputs, SOURCE_EVENING, SOURCE_MORNING, run_vbt_evening,
};
use crate::tasks::vbt_jobs::published_signal_count;
use crate::tasks::{TaskContext, run_task};

const SPEC: Spec = Spec {
    valued: &["--date", "--sessions", "--start"],
    switches: &["--force", "--backtest"],
    optional: &[("--plan", SOURCE_EVENING)],
    positionals: 0,
};

const NO_USER: &str = "BASKFY_SOLE_USER_ID is not set, and the vb_ schema is keyed by user. Set \
                       it, or run `make seed` to create the development account.";

pub(super) async fn run(args: &[String]) -> Outcome {
    let parsed = Parsed::parse(args, SPEC)?;
    let sessions = parsed.int("--sessions")?.unwrap_or(1);
    let plan = parsed.get("--plan");
    if let Some(source) = plan
        && source != SOURCE_EVENING
        && source != SOURCE_MORNING
    {
        return Err(Failure::Usage(format!(
            "argument --plan: invalid choice: '{source}' (choose from 'EVENING', 'MORNING')"
        )));
    }
    let date = parsed.get("--date").map(parse_date).transpose()?;
    let start = parsed.get("--start").map(parse_date).transpose()?;
    let ctx = context()?;
    let day = date.unwrap_or_else(|| ctx.today_ist());
    let result = if parsed.on("--backtest") {
        backtest(&ctx, start, day).await
    } else {
        sessions_run(&ctx, day, sessions, parsed.on("--force"), plan).await
    };
    ctx.db.close().await;
    let result = result?;
    emit(&result);
    Ok(code(result.get("error").is_none()))
}

fn error(text: &str) -> OrderedJson {
    OrderedJson::Object(vec![(
        "error".to_owned(),
        OrderedJson::Str(text.to_owned()),
    )])
}

async fn sessions_run(
    ctx: &TaskContext,
    day: NaiveDate,
    sessions: i64,
    force: bool,
    plan: Option<&str>,
) -> Result<OrderedJson, Failure> {
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(error(NO_USER));
    };
    let mut tx = ctx.db.begin().await?;
    let days = if sessions <= 1 {
        vec![day]
    } else {
        recent_trading_days(&mut tx, day, sessions).await?
    };
    let mut rows = Vec::with_capacity(days.len());
    for one in days {
        let row = match plan {
            Some(source) => plan_one(&mut tx, ctx, one, user_id, source).await?,
            None => detect_one(&mut tx, one, user_id, force).await?,
        };
        rows.push(row);
    }
    tx.commit().await?;
    let (flag, key) = match plan {
        Some(_) => (
            ctx.worker.vbt_execution_enabled,
            ("execution_enabled", "plans"),
        ),
        None => (
            ctx.worker.vbt_nightly_enabled,
            ("nightly_enabled", "sessions"),
        ),
    };
    Ok(OrderedJson::Object(vec![
        ("user_id".to_owned(), OrderedJson::Int(user_id)),
        (key.0.to_owned(), OrderedJson::Bool(flag)),
        (key.1.to_owned(), OrderedJson::Array(rows)),
    ]))
}

/// `_detect_one`: a session that already has rows is answered without work unless `--force`.
async fn detect_one(
    conn: &mut sqlx::PgConnection,
    day: NaiveDate,
    user_id: i64,
    force: bool,
) -> Result<OrderedJson, Failure> {
    if !force {
        let already = published_signal_count(conn, user_id, day).await?;
        if already != 0 {
            return Ok(OrderedJson::Object(vec![
                ("date".to_owned(), OrderedJson::Str(iso(day))),
                (
                    "skipped".to_owned(),
                    OrderedJson::Str("already detected".to_owned()),
                ),
                ("rows".to_owned(), OrderedJson::Int(already)),
            ]));
        }
    }
    let mut outcome = StepOutcome::default();
    let signals = run_detect_vbt_for(conn, &mut outcome, day, user_id, None, None, false).await?;
    Ok(OrderedJson::Object(vec![
        ("date".to_owned(), OrderedJson::Str(iso(day))),
        ("signals".to_owned(), OrderedJson::Int(signals)),
        (
            "status".to_owned(),
            OrderedJson::Str(outcome.status.value().to_owned()),
        ),
        ("detail".to_owned(), outcome.detail_json()),
    ]))
}

/// `_plan_one`: `run_vbt_evening` with Python's default `execution_enabled=False`.
async fn plan_one(
    conn: &mut sqlx::PgConnection,
    ctx: &TaskContext,
    day: NaiveDate,
    user_id: i64,
    source: &str,
) -> Result<OrderedJson, Failure> {
    let mut outcome = StepOutcome::default();
    let inputs = EveningInputs {
        trade_date: day,
        user_id,
        execution_enabled: false,
        source,
        now: ctx.now_utc(),
    };
    let report = run_vbt_evening(conn, &mut outcome, &inputs).await?;
    Ok(match report {
        Some(report) => OrderedJson::Object(report.as_detail()),
        None => OrderedJson::Object(vec![
            ("date".to_owned(), OrderedJson::Str(iso(day))),
            (
                "status".to_owned(),
                OrderedJson::Str(outcome.status.value().to_owned()),
            ),
            ("detail".to_owned(), outcome.detail_json()),
        ]),
    })
}

/// `datetime.isoformat()` of a UTC timestamp.
pub(super) fn iso_utc(moment: DateTime<Utc>) -> String {
    let micros = moment.nanosecond() / 1_000;
    if micros == 0 {
        moment.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        format!("{}.{micros:06}+00:00", moment.format("%Y-%m-%dT%H:%M:%S"))
    }
}

/// `_backtest_one`: the `baskfy.vbt.backtest` body ending at `--date`, reported with the
/// headline of `stats.full`.
async fn backtest(
    ctx: &TaskContext,
    start: Option<NaiveDate>,
    end: NaiveDate,
) -> Result<OrderedJson, Failure> {
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(error(NO_USER));
    };
    let mut kwargs = vec![("end".to_owned(), OrderedJson::Str(iso(end)))];
    if let Some(start) = start {
        kwargs.push(("start".to_owned(), OrderedJson::Str(iso(start))));
    }
    let done = run_task(ctx, "baskfy.vbt.backtest", Vec::new(), kwargs).await?;
    let Some(OrderedJson::Int(run_id)) = done.get("run_id").cloned() else {
        return Ok(done);
    };
    let mut tx = ctx.db.begin().await?;
    let row: (String, DateTime<Utc>, Option<String>) =
        sqlx::query_as("SELECT source, started_at, stats::text FROM vb_backtest_run WHERE id = $1")
            .bind(run_id)
            .fetch_one(&mut *tx)
            .await?;
    tx.rollback().await?;
    let stats = row
        .2
        .map(|t| OrderedJson::parse(&t))
        .transpose()
        .map_err(|e| {
            Failure::Raised(crate::error::WorkerError::python(
                "ValueError",
                e.to_string(),
            ))
        })?
        .unwrap_or(OrderedJson::Null);
    Ok(OrderedJson::Object(vec![
        ("user_id".to_owned(), OrderedJson::Int(user_id)),
        (
            "backtest".to_owned(),
            backtest_payload(run_id, row.0, row.1, &done, &stats),
        ),
    ]))
}

/// The CLI's report of one backtest run.
pub(super) fn backtest_payload(
    run_id: i64,
    source: String,
    started_at: DateTime<Utc>,
    done: &OrderedJson,
    stats: &OrderedJson,
) -> OrderedJson {
    let full = match stats.get("full") {
        Some(full @ OrderedJson::Object(_)) => Some(full),
        _ => None,
    };
    let headline = |key: &str| {
        full.and_then(|f| f.get(key))
            .cloned()
            .unwrap_or(OrderedJson::Null)
    };
    let carried = |key: &str| done.get(key).cloned().unwrap_or(OrderedJson::Null);
    OrderedJson::Object(vec![
        ("run_id".to_owned(), OrderedJson::Int(run_id)),
        ("source".to_owned(), OrderedJson::Str(source)),
        (
            "started_at".to_owned(),
            OrderedJson::Str(iso_utc(started_at)),
        ),
        ("finished_at".to_owned(), carried("finished_at")),
        ("cagr_pct".to_owned(), headline("cagr_pct")),
        ("max_drawdown_pct".to_owned(), headline("max_drawdown_pct")),
        ("trades".to_owned(), headline("trades")),
        ("drift".to_owned(), carried("drift")),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone as _;

    #[test]
    fn isoformat_drops_zero_microseconds_as_python_does() {
        let at = Utc.with_ymd_and_hms(2026, 9, 10, 12, 0, 5).single();
        assert_eq!(
            at.map(iso_utc).as_deref(),
            Some("2026-09-10T12:00:05+00:00")
        );
        let at = at.map(|t| t + chrono::Duration::microseconds(42));
        assert_eq!(
            at.map(iso_utc).as_deref(),
            Some("2026-09-10T12:00:05.000042+00:00")
        );
    }

    #[test]
    fn the_backtest_headline_reads_stats_full_and_tolerates_its_absence() {
        let Some(at) = Utc.with_ymd_and_hms(2026, 9, 10, 12, 0, 0).single() else {
            return;
        };
        let done = OrderedJson::Object(vec![
            ("finished_at".to_owned(), OrderedJson::Str("x".to_owned())),
            ("drift".to_owned(), OrderedJson::Null),
        ]);
        let stats = OrderedJson::Object(vec![(
            "full".to_owned(),
            OrderedJson::Object(vec![
                ("cagr_pct".to_owned(), OrderedJson::Float(21.5)),
                ("trades".to_owned(), OrderedJson::Int(164)),
            ]),
        )]);
        let out = backtest_payload(9, "PLANT".to_owned(), at, &done, &stats);
        assert_eq!(out.get("cagr_pct"), Some(&OrderedJson::Float(21.5)));
        assert_eq!(out.get("trades"), Some(&OrderedJson::Int(164)));
        assert_eq!(out.get("max_drawdown_pct"), Some(&OrderedJson::Null));
        let out = backtest_payload(9, "PLANT".to_owned(), at, &done, &OrderedJson::Null);
        assert_eq!(out.get("cagr_pct"), Some(&OrderedJson::Null));
        assert_eq!(
            out.get("finished_at"),
            Some(&OrderedJson::Str("x".to_owned()))
        );
    }
}
