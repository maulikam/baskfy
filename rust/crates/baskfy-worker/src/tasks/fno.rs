//! Lane **fno** — the F&O sleeve's jobs: the evening bhavcopy ingest (hourly 18:30–23:30), the
//! index series, the night scan, the weekly report, the five-minute alerts, the quarterly
//! re-test and the 15:00 spread sample.
//!
//! Python: `baskfy_worker/fno/*` (`ingest`, `index_daily`, `nightly.run_night`, `scan`,
//! `f3_scan`, `weekly`, `alerts`, `retest`, `spreads`, `ledger`, `underlying`, `partitions`,
//! `backfill`) and the seven task bodies in `tasks/celery_tasks.py`. Inputs under the harness:
//! the F&O bhavcopy and ban list from the replay upstream (`BASKFY_NSE_ARCHIVE_URL`, raw files
//! from the archive), Kite's history and quotes through [`TaskContext::kite_root`].
//!
//! **Law 2.** Nothing here sends, modifies or cancels an order or a GTT, or calls a Kite write
//! endpoint: the Kite adapters built here carry reads only (`daily_bars`, `option_quotes`), and
//! F3's auto-exit lives in the desk, not in the worker. No auto-execute flag is read here.
//!
//! Grade with `tools/parity/task_diff.py --cases tools/parity/task_cases/fno.json`.

mod alerting;
#[allow(dead_code)] // `fno_cli backfill` (lane clis) is its caller; no task is
pub(crate) mod backfill;
mod common;
#[cfg(test)]
mod db_tests;
mod events;
mod f3_scan;
#[cfg(test)]
mod fixtures;
pub(crate) mod index_daily;
pub(crate) mod ingest;
mod ledger;
pub(crate) mod nightly;
mod retest;
pub(crate) mod scan;
mod spreads;
mod underlying;
mod weekly;

use baskfy_db::OrderedJson;
use baskfy_providers::factory::KiteFamily;
use baskfy_providers::records::OptionQuoteRecord;

use self::common::{
    falsy_date, iso, iso_dt, kite_family, kite_session_usable, nse_provider, options_now,
};
use super::{BoundArgs, TaskContext, TaskResult};
use crate::error::WorkerError;
use crate::pyjson::{self, object, s};

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.fno.ingest_bhavcopy",
    "baskfy.fno.index_daily",
    "baskfy.fno.scan",
    "baskfy.fno.weekly",
    "baskfy.fno.alerts",
    "baskfy.fno.retest",
    "baskfy.fno.spread_sample",
];

const SCAN_OFF: &str = "BASKFY_FNO_SCAN_ENABLED is false";
const MONITOR_OFF: &str = "BASKFY_FNO_MONITOR_ENABLED is false";

/// Ports `baskfy_worker.tasks.celery_tasks.fno_ingest_bhavcopy_task` — `baskfy.fno.ingest_bhavcopy(trade_date=None, at=None)`.
///
/// FO2: one session's F&O bhavcopy into `fo_contract_daily` and the next session's ban list.
/// Refuses before any NSE request when `BASKFY_FNO_SCAN_ENABLED` is false or the day is not a
/// session; the 23:30 attempt that records `MISSING` raises `FNO_BHAVCOPY_MISSING`.
///
/// # Errors
/// Python's: a bad date or moment (`ValueError`), the database's, a non-provider failure.
pub async fn fno_ingest_bhavcopy_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let day = falsy_date(&args, "trade_date")?.unwrap_or_else(|| now.date_naive());
    if !ctx.worker.fno_scan_enabled {
        return Ok(object([
            ("trade_date", s(iso(day))),
            ("skipped", s(SCAN_OFF)),
        ]));
    }
    let provider = nse_provider().await?;
    let mut tx = ctx.db.begin().await?;
    let out = nightly::run_night(&mut tx, &provider, day, now.naive_local()).await?;
    let out = match alerting::bhavcopy_missing_alert(&out) {
        Some(alert) => {
            let payload = alerting::dispatch(&alert, &ctx.settings, ctx.now_utc()).await;
            let OrderedJson::Object(mut entries) = out else {
                return Err(WorkerError::python(
                    "TypeError",
                    "run_night returned no dict",
                ));
            };
            pyjson::update(&mut entries, "alert", payload);
            OrderedJson::Object(entries)
        }
        None => out,
    };
    tx.commit().await?;
    Ok(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.fno_index_daily_task` — `baskfy.fno.index_daily(at=None, start=None)`.
///
/// F3: the two indices' daily OHLC into `fo_index_daily` from Kite's history — the evening
/// extension, or a backfill from `start`. Behind `BASKFY_FNO_SCAN_ENABLED`; skipped without a
/// usable Kite session. Reads only.
///
/// # Errors
/// Python's: a bad moment or date, the provider's, the database's.
pub async fn fno_index_daily_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    let today = now.date_naive();
    if !ctx.worker.fno_scan_enabled {
        return Ok(object([("date", s(iso(today))), ("skipped", s(SCAN_OFF))]));
    }
    if !kite_session_usable(ctx) {
        return Ok(object([
            ("date", s(iso(today))),
            ("skipped", s("no usable Kite session")),
        ]));
    }
    let mut tx = ctx.db.begin().await?;
    let kite = kite_family(ctx, KiteFamily::Historical).await?;
    let report = match falsy_date(&args, "start")? {
        Some(start) => index_daily::backfill(&mut tx, &kite, start, today).await?,
        None => index_daily::extend(&mut tx, &kite, today).await?,
    };
    tx.commit().await?;
    let mut out = vec![("date".to_owned(), s(iso(today)))];
    for (k, v) in report.entries() {
        pyjson::update(&mut out, &k, v);
    }
    Ok(OrderedJson::Object(out))
}

/// Ports `baskfy_worker.tasks.celery_tasks.fno_scan_task` — `baskfy.fno.scan(trade_date=None)`.
///
/// FO4: `fo_scan` for the session after `trade_date`, from the database alone (the re-run by
/// hand; the nightly chains the same scan). Behind `BASKFY_FNO_SCAN_ENABLED`.
///
/// # Errors
/// Python's: a bad date, the database's.
pub async fn fno_scan_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let day = falsy_date(&args, "trade_date")?.unwrap_or_else(|| ctx.today_ist());
    if !ctx.worker.fno_scan_enabled {
        return Ok(object([
            ("trade_date", s(iso(day))),
            ("skipped", s(SCAN_OFF)),
        ]));
    }
    let mut tx = ctx.db.begin().await?;
    let out = scan::run_scan(&mut tx, day, None).await?;
    tx.commit().await?;
    Ok(out)
}

/// Ports `baskfy_worker.tasks.celery_tasks.fno_weekly_task` — `baskfy.fno.weekly(at=None)`.
///
/// FO10: one `FNO_WEEKLY` alert per FO tenant. Read-only; refused before any database session
/// unless `BASKFY_FNO_MONITOR_ENABLED`.
///
/// # Errors
/// Python's: a bad moment, the database's.
pub async fn fno_weekly_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    if !ctx.worker.fno_monitor_enabled {
        return Ok(object([
            ("at", s(iso_dt(now))),
            ("skipped", s(MONITOR_OFF)),
        ]));
    }
    let today = now
        .with_timezone(&baskfy_providers::tokens::ist())
        .date_naive();
    let mut tx = ctx.db.begin().await?;
    let mut sent = Vec::new();
    for user_id in scan::scan_users(&mut tx).await? {
        let alert = weekly::build_weekly(&mut tx, user_id, today).await?;
        let payload = alerting::dispatch(&alert, &ctx.settings, ctx.now_utc()).await;
        pyjson::update(
            &mut sent,
            &user_id.to_string(),
            object([("summary", s(alert.summary.clone())), ("sent", payload)]),
        );
    }
    tx.commit().await?;
    Ok(object([
        ("at", s(iso_dt(now))),
        ("users", OrderedJson::Object(sent)),
    ]))
}

/// Ports `baskfy_worker.tasks.celery_tasks.fno_alerts_task` — `baskfy.fno.alerts(at=None)`.
///
/// FO12: today's FO events raised once each (a Redis marker per event) from what the desk wrote.
/// Read-only; reaches no broker. Refused unless `BASKFY_FNO_MONITOR_ENABLED`.
///
/// # Errors
/// Python's: a bad moment, the database's.
pub async fn fno_alerts_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    if !ctx.worker.fno_monitor_enabled {
        return Ok(object([
            ("at", s(iso_dt(now))),
            ("skipped", s(MONITOR_OFF)),
        ]));
    }
    let mut tx = ctx.db.begin().await?;
    // `build_cache()`: an unparseable URL is no cache, never an error.
    let cache = redis::Client::open(ctx.worker.redis_url.as_str()).ok();
    let users = scan::scan_users(&mut tx).await?;
    let out = events::run_fno_alerts(&mut tx, &users, cache.as_ref(), now, &ctx.settings, || {
        ctx.now_utc()
    })
    .await?;
    tx.commit().await?;
    Ok(out)
}

/// `force` with Python's truthiness.
fn truthy(value: Option<&OrderedJson>) -> bool {
    match value {
        None | Some(OrderedJson::Null | OrderedJson::Bool(false) | OrderedJson::Int(0)) => false,
        Some(OrderedJson::Str(t)) => !t.is_empty(),
        Some(OrderedJson::Array(a)) => !a.is_empty(),
        Some(OrderedJson::Object(o)) => !o.is_empty(),
        Some(OrderedJson::Float(f)) => f.abs() > 0.0 || f.is_nan(),
        Some(_) => true,
    }
}

/// Ports `baskfy_worker.tasks.celery_tasks.fno_retest_task` — `baskfy.fno.retest(families=None, force=False)`.
///
/// FO9: every `RESEARCH.md` family over `fo_contract_daily` into `fo_backtest_run`; refuses a
/// second run in a month unless `force`. Behind `BASKFY_FNO_SCAN_ENABLED`. Reads no network.
///
/// # Errors
/// Python's: a bad argument, the database's, a family's computation.
pub async fn fno_retest_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let today = ctx.today_ist();
    if !ctx.worker.fno_scan_enabled {
        return Ok(object([("skipped", s(SCAN_OFF))]));
    }
    let families = args.opt_str_list("families")?;
    let force = truthy(args.value("force"));
    let mut tx = ctx.db.begin().await?;
    let out = retest::run_retest(&mut tx, today, force, families).await?;
    tx.commit().await?;
    Ok(out)
}

/// The spread sample's Kite seam: the quote adapter is built only when a session exists.
struct KiteQuotes<'a>(&'a TaskContext);

impl spreads::QuoteSource for KiteQuotes<'_> {
    fn kite_ok(&self) -> bool {
        kite_session_usable(self.0)
    }

    async fn option_quotes(&self, keys: &[String]) -> Result<Vec<OptionQuoteRecord>, WorkerError> {
        let kite = kite_family(self.0, KiteFamily::Quote).await?;
        let keys: Vec<&str> = keys.iter().map(String::as_str).collect();
        Ok(kite.option_quotes(&keys).await?)
    }
}

/// Ports `baskfy_worker.tasks.celery_tasks.fno_spread_sample_task` — `baskfy.fno.spread_sample(at=None)`.
///
/// FO3: the 15:00 spread sample into `fo_spread_sample` and the forward results calendar.
/// Refuses before any database session when `BASKFY_FNO_SCAN_ENABLED` is false or outside
/// 09:15–15:30 IST, then on a holiday; no Kite call without a Kite session.
///
/// # Errors
/// Python's: a bad moment, the provider's, the database's.
pub async fn fno_spread_sample_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let now = options_now(ctx, &args)?;
    if !ctx.worker.fno_scan_enabled {
        return Ok(object([("at", s(iso_dt(now))), ("skipped", s(SCAN_OFF))]));
    }
    if !spreads::in_session(now) {
        return Ok(object([
            ("at", s(iso_dt(now))),
            ("skipped", s("outside 09:15-15:30 IST")),
        ]));
    }
    let mut tx = ctx.db.begin().await?;
    let today = now
        .with_timezone(&baskfy_providers::tokens::ist())
        .date_naive();
    if !crate::ops::is_trading_day(&mut tx, today).await? {
        tx.commit().await?;
        return Ok(object([
            ("at", s(iso_dt(now))),
            ("skipped", s("not an NSE trading day")),
        ]));
    }
    let results = nse_provider().await?;
    let report = spreads::run_spread_sample(&mut tx, now, &KiteQuotes(ctx), Some(&results)).await?;
    tx.commit().await?;
    Ok(report.as_dict())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_task_of_the_lane_is_ported_and_none_executes() {
        assert_eq!(PORTED.len(), 7);
        assert!(PORTED.iter().all(|t| t.starts_with("baskfy.fno.")));
        assert!(
            PORTED
                .iter()
                .all(|t| !t.contains("execute") && !t.contains("exit"))
        );
    }

    #[test]
    fn force_reads_as_python_truthiness() {
        assert!(!truthy(None));
        assert!(!truthy(Some(&OrderedJson::Bool(false))));
        assert!(truthy(Some(&OrderedJson::Bool(true))));
        assert!(truthy(Some(&OrderedJson::Int(1))));
        assert!(!truthy(Some(&OrderedJson::Str(String::new()))));
    }
}
