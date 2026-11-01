//! `baskfy_worker.options_cli` as a subcommand. `probe` is refused (no Rust port); no command
//! here has an order path.

mod seed;

use std::process::ExitCode;

use baskfy_db::OrderedJson;
use baskfy_providers::factory::{KiteFamily, KiteLane};
use baskfy_providers::tokens::ist;
use chrono::{DateTime, FixedOffset, NaiveDate};

use super::cli::{
    Failure, Outcome, Parsed, Spec, command, context, emit, iso, parse_date, today_ist,
};
use crate::error::WorkerError;
use crate::tasks::TaskContext;
use crate::tasks::context::sole_user_id;
use crate::tasks::fno::common::fromisoformat_datetime;
use crate::tasks::options::{collector, common, index_bars, master, plan, plan_o2, plan_o3};

const CHOICES: [&str; 8] = [
    "seed",
    "refresh-master",
    "probe",
    "backfill-index-bars",
    "collect-once",
    "plan",
    "plan-o2",
    "plan-o3",
];

const SPEC: Spec = Spec {
    valued: &["--date", "--at", "--from", "--to"],
    switches: &[],
    optional: &[],
    positionals: 1,
};

/// Why `probe` is not run from Rust.
pub(super) const PROBE_REFUSED: &str = "options_cli probe is not ported: OP0's six live Kite \
     reads (and the basket_order_margins calculator POST) stay in Python; run `python -m \
     baskfy_worker.options_cli probe`";

fn skipped(why: &str) -> OrderedJson {
    OrderedJson::Object(vec![(
        "skipped".to_owned(),
        OrderedJson::Str(why.to_owned()),
    )])
}

fn entries(value: OrderedJson) -> Vec<(String, OrderedJson)> {
    match value {
        OrderedJson::Object(e) => e,
        _ => Vec::new(),
    }
}

/// `--at` (ISO; naive is IST), else now.
pub(super) fn plan_moment(
    at: Option<&str>,
    now: DateTime<FixedOffset>,
) -> Result<DateTime<FixedOffset>, Failure> {
    let Some(text) = at.filter(|t| !t.is_empty()) else {
        return Ok(now);
    };
    let (naive, tz) = fromisoformat_datetime(text)
        .map_err(|e| Failure::Exit(format!("{}: {e}", e.python_type())))?;
    naive
        .and_local_timezone(tz.unwrap_or_else(ist))
        .single()
        .ok_or_else(|| Failure::Exit(format!("ValueError: Invalid isoformat string: '{text}'")))
}

pub(super) async fn run(args: &[String]) -> Outcome {
    let parsed = Parsed::parse(args, SPEC)?;
    let (cmd, _) = command(&parsed.positionals, &CHOICES)?;
    let today = match parsed.get("--date").filter(|v| !v.is_empty()) {
        Some(v) => parse_date(v)?,
        None => today_ist(),
    };
    let result = match cmd {
        "seed" => run_seed().await?,
        "refresh-master" => run_refresh(today).await?,
        "probe" => return Err(Failure::Exit(PROBE_REFUSED.to_owned())),
        "backfill-index-bars" => {
            let Some(start) = parsed.get("--from").filter(|v| !v.is_empty()) else {
                return Err(Failure::Usage(
                    "backfill-index-bars needs --from".to_owned(),
                ));
            };
            let start = parse_date(start)?;
            let end = match parsed.get("--to").filter(|v| !v.is_empty()) {
                Some(v) => parse_date(v)?,
                None => today_ist(),
            };
            run_backfill(start, end).await?
        }
        "collect-once" => run_collect_once().await?,
        plan => {
            let ctx = context()?;
            let at = plan_moment(parsed.get("--at"), common::now_ist(&ctx))?;
            let out = run_plan(&ctx, plan, at).await;
            ctx.db.close().await;
            out?
        }
    };
    emit(&result);
    Ok(ExitCode::SUCCESS)
}

/// `_plan` / `_plan_o2` / `_plan_o3`: the builder once at `at`, ignoring the monitor flag but
/// nothing else; alerts are printed as summaries, never sent.
pub(super) async fn run_plan(
    ctx: &TaskContext,
    command: &str,
    at: DateTime<FixedOffset>,
) -> Result<OrderedJson, Failure> {
    let Some(user_id) = sole_user_id() else {
        return Ok(skipped("no BASKFY_SOLE_USER_ID configured"));
    };
    let margin = if command == "plan-o2" {
        None
    } else {
        crate::tasks::options::margin_reader(ctx).await?
    };
    if let Some(refusal) = plan_gate(ctx, at).await? {
        return Ok(refusal);
    }
    let mut tx = ctx.db.begin().await?;
    let built = match command {
        "plan" => plan::plan_o1_minute(&mut tx, user_id, at, margin.as_ref(), &ctx.worker).await,
        "plan-o2" => plan_o2::plan_o2_minute(&mut tx, user_id, at, &ctx.worker).await,
        _ => plan_o3::plan_o3_minute(&mut tx, user_id, at, margin.as_ref(), &ctx.worker).await,
    };
    let (mut report, alerts) = match built {
        Ok(b) => b,
        Err(e) => {
            let _ = tx.rollback().await;
            return Err(Failure::Raised(e));
        }
    };
    tx.commit().await?;
    crate::pyjson::update(
        &mut report,
        "alerts",
        OrderedJson::Array(
            alerts
                .iter()
                .map(|a| OrderedJson::Str(a.summary.clone()))
                .collect(),
        ),
    );
    Ok(OrderedJson::Object(report))
}

/// The refusals Python answers before planning: the tenant, then the NSE calendar.
async fn plan_gate(
    ctx: &TaskContext,
    at: DateTime<FixedOffset>,
) -> Result<Option<OrderedJson>, Failure> {
    if sole_user_id().is_none() {
        return Ok(Some(skipped("no BASKFY_SOLE_USER_ID configured")));
    }
    let mut tx = ctx.db.begin().await?;
    let day = common::in_ist(at).date_naive();
    let session = common::is_session_day(&mut tx, day).await?;
    tx.commit().await?;
    if !session {
        return Ok(Some(skipped("not an NSE trading day")));
    }
    Ok(None)
}

async fn run_seed() -> Result<OrderedJson, Failure> {
    let Some(user_id) = sole_user_id() else {
        return Ok(skipped("no BASKFY_SOLE_USER_ID configured"));
    };
    let ctx = context()?;
    let mut tx = ctx.db.begin().await?;
    let written = seed::seed_options(&mut tx, user_id).await?;
    tx.commit().await?;
    ctx.db.close().await;
    let mut out = vec![("user_id".to_owned(), OrderedJson::Int(user_id))];
    out.extend(written);
    Ok(OrderedJson::Object(out))
}

async fn run_refresh(day: NaiveDate) -> Result<OrderedJson, Failure> {
    let ctx = context()?;
    let provider = common::plain_kite(&ctx, KiteLane::Bulk).await?;
    let records = provider
        .fno_option_contracts()
        .await
        .map_err(WorkerError::Provider)?;
    let mut tx = ctx.db.begin().await?;
    let reports = match master::refresh_master_all(&mut tx, &records, day).await? {
        Ok(reports) => reports,
        Err(master::EmptyMaster(why)) => {
            tx.commit().await?;
            ctx.db.close().await;
            return Ok(OrderedJson::Object(vec![(
                "refused".to_owned(),
                OrderedJson::Str(why),
            )]));
        }
    };
    tx.commit().await?;
    ctx.db.close().await;
    let Some(nifty) = reports.nifty() else {
        return Err(Failure::Raised(WorkerError::python("KeyError", "'NIFTY'")));
    };
    let mut out = nifty.pairs();
    for (k, v) in reports.summary() {
        crate::pyjson::update(&mut out, &k, v);
    }
    if let Some(alert) = master::master_alert(nifty) {
        crate::pyjson::update(&mut out, "alert_summary", OrderedJson::Str(alert.summary));
    }
    Ok(OrderedJson::Object(out))
}

async fn run_backfill(start: NaiveDate, end: NaiveDate) -> Result<OrderedJson, Failure> {
    let ctx = context()?;
    let bars = common::family_kite(&ctx, KiteFamily::Historical, KiteLane::Bulk).await?;
    let report = index_bars::backfill(&ctx.db, &bars, start, end, common::now_ist(&ctx)).await;
    ctx.db.close().await;
    let mut out = vec![
        ("from".to_owned(), OrderedJson::Str(iso(start))),
        ("to".to_owned(), OrderedJson::Str(iso(end))),
    ];
    for (k, v) in report?.pairs() {
        crate::pyjson::update(&mut out, &k, v);
    }
    Ok(OrderedJson::Object(out))
}

/// `collect_gate(now, enabled=True, ...)`: the window, the NSE calendar, a usable Kite token.
pub(super) async fn collect_refusal(
    ctx: &TaskContext,
    conn: &mut sqlx::PgConnection,
    now: DateTime<FixedOffset>,
) -> Result<Option<&'static str>, WorkerError> {
    if !collector::in_session(now) {
        return Ok(Some("outside 09:15-15:30 IST"));
    }
    if !common::is_session_day(conn, common::in_ist(now).date_naive()).await? {
        return Ok(Some("not an NSE trading day"));
    }
    if !common::kite_session_usable(ctx) {
        return Ok(Some("no usable Kite session"));
    }
    Ok(None)
}

async fn run_collect_once() -> Result<OrderedJson, Failure> {
    let ctx = context()?;
    let now = common::now_ist(&ctx);
    let mut tx = ctx.db.begin().await?;
    if let Some(why) = collect_refusal(&ctx, &mut tx, now).await? {
        tx.commit().await?;
        ctx.db.close().await;
        return Ok(skipped(why));
    }
    let quotes = common::family_kite(&ctx, KiteFamily::Quote, KiteLane::Interactive).await?;
    let report = collector::collect_minute(&mut tx, &quotes, now).await?;
    tx.commit().await?;
    ctx.db.close().await;
    Ok(OrderedJson::Object(entries(report.as_dict())))
}

#[cfg(test)]
pub(crate) mod db_tests;

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    #[tokio::test]
    async fn probe_is_refused_with_a_clear_error() {
        let Err(Failure::Exit(m)) = run(&args(&["probe"])).await else {
            panic!("probe must be refused");
        };
        assert_eq!(m, PROBE_REFUSED);
        assert!(m.contains("not ported"));
    }

    #[tokio::test]
    async fn backfill_index_bars_needs_from() {
        let Err(Failure::Usage(m)) = run(&args(&["backfill-index-bars"])).await else {
            panic!("--from is required");
        };
        assert_eq!(m, "backfill-index-bars needs --from");
    }

    #[tokio::test]
    async fn an_unknown_command_is_a_usage_error() {
        let Err(Failure::Usage(m)) = run(&args(&["place-order"])).await else {
            panic!("an unknown command must be refused");
        };
        assert!(m.starts_with("argument command: invalid choice: 'place-order'"));
    }

    #[test]
    fn a_naive_at_is_ist_and_an_aware_one_keeps_its_zone() {
        let now = DateTime::parse_from_rfc3339("2026-09-24T10:05:00+05:30").unwrap();
        assert_eq!(plan_moment(None, now).unwrap(), now);
        let at = plan_moment(Some("2026-09-24T10:00"), now).unwrap();
        assert_eq!(at.to_rfc3339(), "2026-09-24T10:00:00+05:30");
        let at = plan_moment(Some("2026-09-24T04:30:00+00:00"), now).unwrap();
        assert_eq!(at.to_rfc3339(), "2026-09-24T04:30:00+00:00");
        assert!(matches!(
            plan_moment(Some("tomorrow"), now),
            Err(Failure::Exit(_))
        ));
    }
}
