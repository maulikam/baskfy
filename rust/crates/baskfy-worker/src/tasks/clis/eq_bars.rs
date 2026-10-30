//! `baskfy_worker.eq_bars_cli`: one-minute equity bars by hand (`session`, `backfill`).

use baskfy_db::OrderedJson;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, Utc};

use super::cli::{Failure, Outcome, Parsed, Spec, command, context, emit, iso};
use crate::tasks::TaskContext;
use crate::tasks::backfills::common::kite_bars;
use crate::tasks::backfills::eq_bars::{self, EqName};
use crate::tasks::swing_live::published_session::last_published_session;

const SESSION: Spec = Spec {
    valued: &["--date"],
    switches: &[],
    optional: &[],
    positionals: 0,
};

const BACKFILL: Spec = Spec {
    valued: &["--from", "--to"],
    switches: &[],
    optional: &[],
    positionals: 0,
};

fn now_ist() -> DateTime<FixedOffset> {
    Utc::now().with_timezone(&baskfy_providers::tokens::ist())
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let (cmd, rest) = command(args, &["session", "backfill"])?;
    let now = now_ist();
    let today = now.date_naive();
    if cmd == "session" {
        let parsed = Parsed::parse(rest, SESSION)?;
        let day = parsed.date("--date")?.unwrap_or(today);
        let ctx = context()?;
        let out = session(&ctx, day, now).await;
        ctx.db.close().await;
        emit(&out?);
    } else {
        let parsed = Parsed::parse(rest, BACKFILL)?;
        let Some(start) = parsed.date("--from")? else {
            return Err(Failure::Usage(
                "the following arguments are required: --from".to_owned(),
            ));
        };
        let end = parsed.date("--to")?.unwrap_or(today);
        let ctx = context()?;
        let out = backfill(&ctx, start, end, now).await;
        ctx.db.close().await;
        emit(&out?);
    }
    Ok(std::process::ExitCode::SUCCESS)
}

/// `_names(session, on_or_before=)`.
async fn names(
    ctx: &TaskContext,
    on_or_before: NaiveDate,
) -> Result<(NaiveDate, Vec<EqName>), Failure> {
    let Some(user_id) = ctx.sole_user_id() else {
        return Err(Failure::Exit("BASKFY_SOLE_USER_ID is not set".to_owned()));
    };
    let mut tx = ctx.db.begin().await?;
    let Some(as_of) = last_published_session(&mut tx, Some(on_or_before)).await? else {
        return Err(Failure::Exit(
            "nothing is published yet; the universe has no session to stand on".to_owned(),
        ));
    };
    let names = eq_bars::liquid_names(&mut tx, as_of, user_id).await?;
    tx.rollback().await?;
    Ok((as_of, names))
}

async fn session(
    ctx: &TaskContext,
    day: NaiveDate,
    now: DateTime<FixedOffset>,
) -> Result<OrderedJson, Failure> {
    let kite = kite_bars(ctx).await?;
    let (as_of, names) = names(ctx, day - Duration::days(1)).await?;
    let report = eq_bars::reconcile_session(&ctx.db, &kite, day, now, &names).await?;
    let mut pairs = vec![
        ("date".to_owned(), OrderedJson::Str(iso(day))),
        ("as_of".to_owned(), OrderedJson::Str(iso(as_of))),
    ];
    pairs.extend(report.pairs());
    Ok(OrderedJson::Object(pairs))
}

async fn backfill(
    ctx: &TaskContext,
    start: NaiveDate,
    end: NaiveDate,
    now: DateTime<FixedOffset>,
) -> Result<OrderedJson, Failure> {
    let kite = kite_bars(ctx).await?;
    let (as_of, names) = names(ctx, end).await?;
    let report = eq_bars::backfill(&ctx.db, &kite, start, end, now, &names).await?;
    let mut pairs = vec![
        ("from".to_owned(), OrderedJson::Str(iso(start))),
        ("to".to_owned(), OrderedJson::Str(iso(end))),
        ("as_of".to_owned(), OrderedJson::Str(iso(as_of))),
    ];
    pairs.extend(report.pairs());
    Ok(OrderedJson::Object(pairs))
}
