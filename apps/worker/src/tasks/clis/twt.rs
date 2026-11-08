//! `baskfy_worker.twt_cli`: three-weeks-tight detection for a date (or the last N sessions), or
//! `--plan [EVENING|MORNING]` — the session's plan, every line `PROPOSED`. Places nothing.

use baskfy_api::Settings;
use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use super::cli::{Failure, Outcome, Parsed, Spec, code, context, emit, iso, parse_date};
use crate::steps::StepOutcome;
use crate::tasks::TaskContext;
use crate::tasks::swing_eod::recent_trading_days;
use crate::tasks::twt_jobs::detect::detect_session;
use crate::tasks::twt_jobs::evening::{
    EveningInputs, SOURCE_EVENING, SOURCE_MORNING, last_detected_session, run_twt_evening,
};

const SPEC: Spec = Spec {
    valued: &["--date", "--sessions"],
    switches: &["--force"],
    optional: &[("--plan", SOURCE_EVENING)],
    positionals: 0,
};

const NO_USER: &str = "BASKFY_SOLE_USER_ID is not set, and the tw_ schema is keyed by user. Set \
                       it, or run `make seed` to create the development account.";

pub(super) async fn run(args: &[String]) -> Outcome {
    let parsed = Parsed::parse(args, SPEC)?;
    let sessions = parsed.int("--sessions")?.unwrap_or(1);
    let date = parsed.get("--date").map(parse_date).transpose()?;
    let ctx = context()?;
    let result = match parsed.get("--plan") {
        Some(source) => plan(&ctx, date, &source.to_uppercase()).await,
        None => {
            let day = date.unwrap_or_else(|| ctx.today_ist());
            detect(&ctx, day, sessions, parsed.on("--force")).await
        }
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

/// `_run(day, sessions, force=)`: one transaction, committed at the end.
async fn detect(
    ctx: &TaskContext,
    day: NaiveDate,
    sessions: i64,
    force: bool,
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
        rows.push(detect_session(&mut tx, one, user_id, force).await?);
    }
    tx.commit().await?;
    Ok(OrderedJson::Object(vec![
        ("user_id".to_owned(), OrderedJson::Int(user_id)),
        (
            "nightly_enabled".to_owned(),
            OrderedJson::Bool(ctx.worker.twt_nightly_enabled),
        ),
        ("sessions".to_owned(), OrderedJson::Array(rows)),
    ]))
}

/// The settings with every alert sink cleared: the CLI's `notify=False`.
pub(super) fn silent(settings: &Settings) -> Settings {
    let mut quiet = settings.clone();
    quiet.ops_alert_email.clear();
    quiet.ops_alert_webhook_url.clear();
    quiet
}

/// `_plan(day, source)`: TW6's plan for one session, without notifying.
async fn plan(
    ctx: &TaskContext,
    day: Option<NaiveDate>,
    source: &str,
) -> Result<OrderedJson, Failure> {
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(error(NO_USER));
    };
    if source != SOURCE_EVENING && source != SOURCE_MORNING {
        return Ok(error(&format!(
            "SOURCE must be {SOURCE_EVENING} or {SOURCE_MORNING}, not '{source}'"
        )));
    }
    let mut tx = ctx.db.begin().await?;
    let one = match day {
        Some(day) => Some(day),
        None => last_detected_session(&mut tx, user_id, ctx.today_ist()).await?,
    };
    let Some(one) = one else {
        tx.commit().await?;
        return Ok(error(
            "no detected session to plan from; run `make twt DATE=…` first",
        ));
    };
    let settings = silent(&ctx.settings);
    let mut outcome = StepOutcome::default();
    let inputs = EveningInputs {
        trade_date: one,
        user_id,
        execution_enabled: ctx.worker.twt_execution_enabled,
        source,
        now: ctx.now_utc(),
        settings: &settings,
    };
    let report = run_twt_evening(&mut tx, &mut outcome, &inputs).await?;
    tx.commit().await?;
    Ok(match report {
        None => OrderedJson::Object(vec![
            ("date".to_owned(), OrderedJson::Str(iso(one))),
            ("skipped".to_owned(), outcome.detail_json()),
        ]),
        Some(report) => OrderedJson::Object(vec![
            ("user_id".to_owned(), OrderedJson::Int(user_id)),
            ("plan".to_owned(), report.as_detail()),
        ]),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_plan_flag_defaults_to_the_evening_and_takes_a_source() {
        let a = |v: &[&str]| v.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let p = Parsed::parse(&a(&["--plan"]), SPEC).ok();
        assert_eq!(p.as_ref().and_then(|p| p.get("--plan")), Some("EVENING"));
        let p = Parsed::parse(&a(&["--plan", "morning", "--date", "2026-09-10"]), SPEC).ok();
        assert_eq!(p.as_ref().and_then(|p| p.get("--plan")), Some("morning"));
        assert_eq!(p.as_ref().and_then(|p| p.get("--date")), Some("2026-09-10"));
    }
}
