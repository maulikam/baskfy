//! Lane **portfolio** — the official end-of-day NAV and the evening capture of Kite's trades
//! (read-only: `/trades` is emptied nightly).
//!
//! Python: `tasks/portfolio_nav_job.run_portfolio_nav` ([`nav`], on the ledger arithmetic in
//! [`ledger`]) and `baskfy_api.broker_trades.capture_kite_trades` ([`capture`]; holdings and
//! trades from the replay upstream under the harness). Graded with `tools/parity/task_diff.py
//! --cases tools/parity/task_cases/portfolio.json` (DECISIONS-RS R8.pf1–R8.pf4).
//!
//! The lane's other named modules have no task of their own here: `tasks/desk.py`'s three tasks
//! are lane `clis`'s registry rows, `tasks/resync.py` is lane `api_routes`'s
//! (`baskfy.ops.resync`), `tasks/desk_score.py` is wave 5's nightly step
//! (`steps::compute::desk`), and `tasks/holdings_sync.run_holdings_sync` has no caller — no task,
//! Beat entry or CLI reaches it (R8.pf3).
//!
//! **Law 2.** Neither task places, modifies or cancels anything at a broker; the one Kite call is
//! the read-only `GET /trades`.

use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use super::{BoundArgs, TaskContext, TaskResult};
use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};

mod capture;
#[cfg(test)]
mod db_tests;
pub mod ledger;
pub mod nav;

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.portfolio.eod_nav",
    "baskfy.pipeline.capture_kite_trades",
];

/// `dt.date.fromisoformat(as_of) if as_of else today_ist`: Python's truthiness decides, so an
/// empty string, `0`, `false` or an empty list is "today"; any other non-string is the
/// `TypeError` `fromisoformat` raises.
fn as_of_or_today(
    ctx: &TaskContext,
    value: Option<&OrderedJson>,
) -> Result<NaiveDate, WorkerError> {
    let truthy = match value {
        None | Some(OrderedJson::Null) => false,
        Some(OrderedJson::Bool(b)) => *b,
        Some(OrderedJson::Int(i)) => *i != 0,
        Some(OrderedJson::UInt(u)) => *u != 0,
        Some(OrderedJson::BigInt(_)) => true,
        Some(OrderedJson::Float(f)) => *f != 0.0,
        Some(OrderedJson::Str(t)) => !t.is_empty(),
        Some(OrderedJson::Array(a)) => !a.is_empty(),
        Some(OrderedJson::Object(o)) => !o.is_empty(),
    };
    if !truthy {
        return Ok(ctx.today_ist());
    }
    match value {
        Some(OrderedJson::Str(t)) => super::args::fromisoformat_date(t),
        _ => Err(WorkerError::python(
            "TypeError",
            "fromisoformat: argument must be str",
        )),
    }
}

/// Ports `baskfy_worker.tasks.celery_tasks.portfolio_eod_nav_task` —
/// `baskfy.portfolio.eod_nav(as_of=None)`: value every user's portfolios at the close of the
/// date (today in IST by default) and store the day, in one transaction.
///
/// # Errors
/// `ValueError`/`TypeError` for a bad date; whatever `run_portfolio_nav` raises (the
/// transaction is rolled back).
pub async fn portfolio_eod_nav_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = as_of_or_today(ctx, args.value("as_of"))?;
    let mut tx = ctx.db.begin().await?;
    let result = nav::run_portfolio_nav(&mut tx, day).await?;
    let payload = result.as_json()?;
    tx.commit().await?;
    Ok(payload)
}

/// `swing_health.is_session_day`: a day the calendar does not carry is a session.
async fn is_session_day(
    conn: &mut sqlx::PgConnection,
    day: NaiveDate,
) -> Result<bool, WorkerError> {
    let row: Option<(bool,)> = sqlx::query_as(
        "SELECT trading_day.is_trading_day FROM trading_day \
         WHERE trading_day.exchange_id = $1 AND trading_day.date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.is_none_or(|(known,)| known))
}

/// `curated_seed.resolve_sole_user_id`: `BASKFY_SOLE_USER_ID` read with Python's `int()`
/// (stripped; blank or not a number is "unset", with Python's warning), else the seeded
/// development account, else the `pipeline-degraded` `Problem`.
async fn resolve_sole_user_id(conn: &mut sqlx::PgConnection) -> Result<i64, WorkerError> {
    let raw = std::env::var("BASKFY_SOLE_USER_ID").unwrap_or_default();
    let raw = raw.trim();
    if !raw.is_empty() {
        if let Some(id) = python_int(raw) {
            return Ok(id);
        }
        baskfy_api::logging::warning(
            "baskfy_api.curated_seed",
            &format!(
                "BASKFY_SOLE_USER_ID is not a number ({}); reading it as 'no sole tenant \
                 configured', which is what baskfy_worker.providers._sole_user_id does with the \
                 same value",
                baskfy_providers::py::repr_str(raw)
            ),
            &[],
        );
    }
    let found: Option<i64> =
        sqlx::query_scalar("SELECT app_user.id FROM app_user WHERE app_user.public_id = $1")
            .bind("e2e000000001")
            .fetch_optional(&mut *conn)
            .await?;
    found.ok_or_else(|| {
        WorkerError::python(
            "Problem",
            "The sole-tenant account is not configured on this deployment. Set \
             BASKFY_SOLE_USER_ID, or run `make seed` to create the development account.",
        )
    })
}

/// Python's `int(text)` for a stripped string: an optional sign, digits with single
/// underscores between them. `None` when Python would raise (or the value is beyond `i64`,
/// which no `bigint` id can be).
fn python_int(text: &str) -> Option<i64> {
    let body = text.strip_prefix(['+', '-']).unwrap_or(text);
    let valid = !body.is_empty()
        && !body.starts_with('_')
        && !body.ends_with('_')
        && !body.contains("__")
        && body.chars().all(|c| c.is_ascii_digit() || c == '_');
    valid
        .then(|| text.replace('_', "").parse::<i64>().ok())
        .flatten()
}

/// Ports `baskfy_worker.tasks.celery_tasks.capture_kite_trades_task` —
/// `baskfy.pipeline.capture_kite_trades()`: on an NSE session, read today's executions from
/// Kite's read-only `GET /trades`, store what is new and re-date the holdings; silent otherwise.
///
/// # Errors
/// The sole-tenant `Problem` when no account is configured; a database failure.
pub async fn capture_kite_trades_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let today = ctx.today_ist();
    let mut tx = ctx.db.begin().await?;
    if !is_session_day(&mut tx, today).await? {
        tx.commit().await?;
        return Ok(object([
            ("captured", OrderedJson::Bool(false)),
            (
                "reason",
                s(format!(
                    "{} is not an NSE session",
                    today.format("%Y-%m-%d")
                )),
            ),
        ]));
    }
    let user_id = resolve_sole_user_id(&mut tx).await?;
    let report = capture::capture_kite_trades(ctx, &mut tx, user_id).await?;
    tx.commit().await?;
    let count = |n: usize| int(i64::try_from(n).unwrap_or(i64::MAX));
    Ok(object([
        ("captured", OrderedJson::Bool(report.captured)),
        ("reason", s(report.reason)),
        ("inserted", count(report.inserted)),
        ("dated_holdings", count(report.dated_holdings)),
    ]))
}
