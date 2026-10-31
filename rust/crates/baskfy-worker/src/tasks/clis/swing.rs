//! `baskfy_worker.swing_cli`: swing detection for a date (or the last N sessions), or SW6's
//! morning job with `--premarket LEVELS|GAPS`.

use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use super::cli::{Failure, Outcome, Parsed, Spec, code, context, emit, iso, parse_date};
use crate::steps::StepOutcome;
use crate::steps::sleeves::for_swing_eod::run_detect_swing_for;
use crate::tasks::swing_eod::recent_trading_days;
use crate::tasks::{TaskContext, run_task};

const SPEC: Spec = Spec {
    valued: &["--date", "--sessions", "--premarket"],
    switches: &[],
    optional: &[],
    positionals: 0,
};

/// `--premarket`'s choices.
const STAGES: [&str; 2] = ["LEVELS", "GAPS"];

pub(super) const NO_USER: &str = "BASKFY_SOLE_USER_ID is not set, and the sw_ schema is keyed \
                                  by user. Set it, or run `make seed` to create the development \
                                  account.";

pub(super) async fn run(args: &[String]) -> Outcome {
    let parsed = Parsed::parse(args, SPEC)?;
    let sessions = parsed.int("--sessions")?.unwrap_or(1);
    let stage = parsed.get("--premarket");
    if let Some(stage) = stage
        && !STAGES.contains(&stage)
    {
        return Err(Failure::Usage(format!(
            "argument --premarket: invalid choice: '{stage}' (choose from 'LEVELS', 'GAPS')"
        )));
    }
    let ctx = context()?;
    let day = match parsed.get("--date") {
        Some(text) => parse_date(text)?,
        None => ctx.today_ist(),
    };
    let result = match stage {
        Some(stage) => premarket(&ctx, day, stage).await,
        None => detect(&ctx, day, sessions).await,
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

/// `_run(day, sessions)`: every date on one transaction, committed at the end.
async fn detect(ctx: &TaskContext, day: NaiveDate, sessions: i64) -> Result<OrderedJson, Failure> {
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(error(NO_USER));
    };
    let slug = ctx.worker.swing_index_slug.clone();
    let execution = ctx.worker.swing_execution_enabled;
    let mut tx = ctx.db.begin().await?;
    let days = if sessions <= 1 {
        vec![day]
    } else {
        recent_trading_days(&mut tx, day, sessions).await?
    };
    let mut rows = Vec::with_capacity(days.len());
    for one in days {
        let mut outcome = StepOutcome::default();
        let written =
            run_detect_swing_for(&mut tx, &mut outcome, one, user_id, &slug, None).await?;
        rows.push(OrderedJson::Object(vec![
            ("date".to_owned(), OrderedJson::Str(iso(one))),
            ("candidates".to_owned(), OrderedJson::Int(written)),
            (
                "status".to_owned(),
                OrderedJson::Str(outcome.status.value().to_owned()),
            ),
            ("detail".to_owned(), outcome.detail_json()),
        ]));
    }
    tx.commit().await?;
    Ok(OrderedJson::Object(vec![
        ("user_id".to_owned(), OrderedJson::Int(user_id)),
        ("index_slug".to_owned(), OrderedJson::Str(slug)),
        ("execution_enabled".to_owned(), OrderedJson::Bool(execution)),
        ("sessions".to_owned(), OrderedJson::Array(rows)),
    ]))
}

/// `_run_premarket(day, stage)`: the `baskfy.swing.premarket` body, reported as the CLI does.
async fn premarket(ctx: &TaskContext, day: NaiveDate, stage: &str) -> Result<OrderedJson, Failure> {
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(error(
            "BASKFY_SOLE_USER_ID is not set, and the sw_ schema is keyed by user.",
        ));
    };
    let report = run_task(
        ctx,
        "baskfy.swing.premarket",
        Vec::new(),
        vec![
            ("session_date".to_owned(), OrderedJson::Str(iso(day))),
            ("stage".to_owned(), OrderedJson::Str(stage.to_owned())),
        ],
    )
    .await?;
    Ok(premarket_payload(user_id, stage, report))
}

/// `{"user_id", "status": outcome.status, **report.as_detail()}` from the task's
/// `{"date", **report.as_detail()}`: skipped when the calendar had no prior session, or when a
/// GAPS morning built no plan.
pub(super) fn premarket_payload(user_id: i64, stage: &str, report: OrderedJson) -> OrderedJson {
    let OrderedJson::Object(entries) = report else {
        return report;
    };
    let detail: Vec<(String, OrderedJson)> =
        entries.into_iter().filter(|(k, _)| k != "date").collect();
    let field = |key: &str| detail.iter().find(|(k, _)| k == key).map(|(_, v)| v);
    let skipped_reason = field("skipped_reason").is_some_and(|v| *v != OrderedJson::Null);
    let no_plan = field("plan")
        .and_then(|p| p.get("plan_id"))
        .is_none_or(|v| *v == OrderedJson::Null);
    let status = if skipped_reason || (stage != "LEVELS" && no_plan) {
        "skipped"
    } else {
        "succeeded"
    };
    let mut out = vec![
        ("user_id".to_owned(), OrderedJson::Int(user_id)),
        ("status".to_owned(), OrderedJson::Str(status.to_owned())),
    ];
    out.extend(detail);
    OrderedJson::Object(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(skipped: Option<&str>, plan_id: Option<&str>) -> OrderedJson {
        let opt = |v: Option<&str>| v.map_or(OrderedJson::Null, |t| OrderedJson::Str(t.to_owned()));
        OrderedJson::Object(vec![
            ("date".to_owned(), OrderedJson::Str("2026-09-02".to_owned())),
            (
                "session_date".to_owned(),
                OrderedJson::Str("2026-09-02".to_owned()),
            ),
            (
                "plan".to_owned(),
                OrderedJson::Object(vec![("plan_id".to_owned(), opt(plan_id))]),
            ),
            ("skipped_reason".to_owned(), opt(skipped)),
        ])
    }

    fn status(v: &OrderedJson) -> &OrderedJson {
        v.get("status").unwrap_or(&OrderedJson::Null)
    }

    #[test]
    fn premarket_status_follows_the_outcome_python_sets() {
        let skipped = OrderedJson::Str("skipped".to_owned());
        let ok = OrderedJson::Str("succeeded".to_owned());
        assert_eq!(
            status(&premarket_payload(7, "LEVELS", report(None, None))),
            &ok
        );
        assert_eq!(
            status(&premarket_payload(7, "GAPS", report(None, None))),
            &skipped
        );
        assert_eq!(
            status(&premarket_payload(7, "GAPS", report(None, Some("p")))),
            &ok
        );
        assert_eq!(
            status(&premarket_payload(
                7,
                "LEVELS",
                report(Some("no day"), None)
            )),
            &skipped
        );
        let out = premarket_payload(7, "GAPS", report(None, Some("p")));
        let OrderedJson::Object(entries) = out else {
            return;
        };
        let keys: Vec<&str> = entries.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(
            keys,
            [
                "user_id",
                "status",
                "session_date",
                "plan",
                "skipped_reason"
            ]
        );
    }
}
