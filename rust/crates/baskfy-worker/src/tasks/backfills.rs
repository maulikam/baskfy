//! Lane **backfills**: the 18:15 bhavcopy ingest, the 15:45 equity-bars session job and the six backfill CLIs.

mod bhavcopy;
mod breadth;
pub mod cli;
mod common;
mod cursor;
mod deep;
pub mod eq_bars;
mod index_backfill;
mod index_repair;

use baskfy_db::OrderedJson;
use baskfy_providers::factory::{RedisSource, build_nse_provider};
use chrono::{Duration, NaiveDate};
use sqlx::PgConnection;

use super::{BoundArgs, TaskContext, TaskResult};
use crate::calendar::NSE_EXCHANGE_ID;
use crate::catch_up::SESSION_DATA_READY_IST;
use crate::error::WorkerError;
use crate::pyjson::{self, object, s};
use crate::window::DateWindow;
use bhavcopy::{EQUITY_SERIES, Source};
use common::{
    count, iso, kite_bars, kite_session_usable, now_ist, provider_settings, trade_date_arg,
};

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &["baskfy.pipeline.bhavcopy_ingest", "baskfy.eq_bars.session"];

fn skipped(day: NaiveDate, reason: String) -> OrderedJson {
    object([
        ("trade_date", s(iso(day))),
        ("status", s("skipped")),
        ("reason", s(reason)),
    ])
}

/// Ports `baskfy_worker.tasks.celery_tasks.bhavcopy_ingest` — `baskfy.pipeline.bhavcopy_ingest(trade_date=None)`.
pub async fn bhavcopy_ingest(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let given = trade_date_arg(&args, "trade_date")?;
    let day = given.unwrap_or_else(|| ctx.today_ist());
    if given.is_none() {
        let mut tx = ctx.db.begin().await?;
        let trading = crate::ops::is_trading_day(&mut tx, day).await?;
        tx.rollback().await?;
        if !trading {
            return Ok(skipped(day, "not a trading day".to_owned()));
        }
        let now = ctx.now_ist();
        if now.date() == day && now.time() < SESSION_DATA_READY_IST {
            return Ok(skipped(
                day,
                format!("the {} bhavcopy is not published yet", iso(day)),
            ));
        }
    }
    let settings = provider_settings()?;
    let nse = build_nse_provider(
        &RedisSource::from_settings(&settings),
        &settings,
        None,
        None,
    )
    .await?;
    let series: Vec<String> = EQUITY_SERIES.iter().map(|s| (*s).to_owned()).collect();
    let report = bhavcopy::backfill_bars_from_bhavcopy(
        &ctx.db,
        &Source::Nse(&nse),
        DateWindow::single(day),
        &series,
        0,
    )
    .await?;
    Ok(object([
        ("trade_date", s(iso(day))),
        (
            "status",
            s(if report.succeeded() {
                "succeeded"
            } else {
                "failed"
            }),
        ),
        ("bars_written", pyjson::int(report.bars_written)),
        ("days_written", pyjson::int(report.days_written)),
        (
            "missing_days",
            if report.missing_days.is_empty() {
                OrderedJson::Null
            } else {
                pyjson::strings(report.missing_days.iter().map(|d| iso(*d)))
            },
        ),
        (
            "unmatched_symbol_count",
            if report.unmatched_symbols.is_empty() {
                OrderedJson::Null
            } else {
                count(report.unmatched_symbols.len())
            },
        ),
        (
            "failures",
            if report.failures.is_empty() {
                OrderedJson::Null
            } else {
                OrderedJson::Object(report.failures)
            },
        ),
    ]))
}

async fn is_session_day(conn: &mut PgConnection, day: NaiveDate) -> Result<bool, WorkerError> {
    let row: Option<(bool,)> = sqlx::query_as(
        "SELECT is_trading_day FROM trading_day WHERE exchange_id = $1 AND date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.is_none_or(|(known,)| known))
}

fn eq_skip(day: NaiveDate, as_of: Option<NaiveDate>, why: &str) -> OrderedJson {
    let mut pairs = vec![("date", s(iso(day)))];
    if let Some(a) = as_of {
        pairs.push(("as_of", s(iso(a))));
    }
    pairs.push(("skipped", s(why)));
    object(pairs)
}

/// Ports `baskfy_worker.tasks.celery_tasks.eq_bars_session_task` — `baskfy.eq_bars.session(trade_date=None)`.
pub async fn eq_bars_session_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = now_ist(ctx);
    let day = trade_date_arg(&args, "trade_date")?.unwrap_or_else(|| now.date_naive());
    if !ctx.worker.eq_bars_enabled {
        return Ok(eq_skip(day, None, "BASKFY_EQ_BARS_ENABLED is false"));
    }
    if !kite_session_usable(ctx) {
        return Ok(eq_skip(day, None, "no usable Kite session"));
    }
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(eq_skip(day, None, "BASKFY_SOLE_USER_ID is not set"));
    };
    let mut tx = ctx.db.begin().await?;
    if !is_session_day(&mut tx, day).await? {
        return Ok(eq_skip(day, None, "not an NSE trading day"));
    }
    let published = super::swing_live::published_session::last_published_session(
        &mut tx,
        Some(day - Duration::days(1)),
    )
    .await?;
    let Some(as_of) = published else {
        return Ok(eq_skip(day, None, "nothing published yet"));
    };
    let names = eq_bars::liquid_names(&mut tx, as_of, user_id).await?;
    tx.rollback().await?;
    if names.is_empty() {
        return Ok(eq_skip(day, Some(as_of), "no liquid names"));
    }
    let kite = kite_bars(ctx).await?;
    let report = eq_bars::reconcile_session(&ctx.db, &kite, day, now, &names).await?;
    let mut pairs = vec![
        ("date".to_owned(), s(iso(day))),
        ("as_of".to_owned(), s(iso(as_of))),
    ];
    pairs.extend(report.pairs());
    Ok(OrderedJson::Object(pairs))
}
