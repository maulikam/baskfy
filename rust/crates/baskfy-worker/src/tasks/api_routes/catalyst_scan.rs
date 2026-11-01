//! `baskfy.overlap.catalyst_scan` — ports `celery_tasks.overlap_catalyst_scan_task` (the overlap
//! page's "Scan filings" button, `baskfy_api.overlap_scan`).
//!
//! Resolves the names `/build/overlap` lists for the scope **exactly as the page does** — the
//! API's own view, `baskfy_api::routes::overlap::view::overlap`, with no cache (Python passes
//! none) — reads each one's filings and results date through the morning feed's own loop
//! (`swing_catalyst.run_swing_catalyst` with an explicit symbol set), then wakes the Laya sidecar.
//! Progress goes to Redis; the lock the API took is released at the end, whatever happened.
//!
//! Law 2: filings are read, never anything placed.

use baskfy_api::routes::overlap::view;
use baskfy_db::OrderedJson;
use chrono::{DateTime, Timelike, Utc};
use redis::AsyncCommands as _;

use baskfy_providers::nse::NseProvider;
use redis::aio::MultiplexedConnection;
use tokio::sync::mpsc;

use super::super::TaskContext;
use super::super::swing_eod::for_catalyst_scan::{OnSymbol, build_provider, run_swing_catalyst};
use crate::error::WorkerError;
use crate::pyjson::{self, int, s};
use crate::steps::StepOutcome;

/// `overlap_scan.LAYA_WAKE_KEY`.
pub const LAYA_WAKE_KEY: &str = "laya:wake";
/// `overlap_scan.STATUS_TTL_SECONDS`.
pub const STATUS_TTL_SECONDS: u64 = 7 * 24 * 3600;
/// The wake key's TTL.
pub const LAYA_WAKE_TTL_SECONDS: u64 = 3600;

/// `overlap_scan.status_key(user_id)`.
#[must_use]
pub fn status_key(user_id: i64) -> String {
    format!("overlap:catalyst-scan:{user_id}")
}

/// `overlap_scan.lock_key(user_id)`.
#[must_use]
pub fn lock_key(user_id: i64) -> String {
    format!("overlap:catalyst-scan:{user_id}:lock")
}

/// `datetime.isoformat()` of an aware UTC moment.
#[must_use]
pub fn py_isoformat_utc(t: DateTime<Utc>) -> String {
    let base = t.format("%Y-%m-%dT%H:%M:%S").to_string();
    let micros = t.nanosecond() / 1000;
    if micros == 0 {
        format!("{base}+00:00")
    } else {
        format!("{base}.{micros:06}+00:00")
    }
}

/// `str(exc)` truncated as Python's `f"{type(exc).__name__}: {exc}"[:500]` (by code points).
fn error_text(e: &WorkerError) -> String {
    format!("{}: {e}", e.python_type())
        .chars()
        .take(500)
        .collect()
}

/// The swing feed's loop over an explicit symbol set: `run_swing_catalyst(session, outcome, day,
/// user_id=, provider=, symbols=, on_symbol=)` → `report.as_detail()`.
///
/// # Errors
/// The database's (a provider error on a name is the feed's own fail-soft count).
pub async fn feed_loop(
    conn: &mut sqlx::PgConnection,
    day: chrono::NaiveDate,
    user_id: i64,
    provider: &NseProvider,
    symbols: &[(String, i64)],
    on_symbol: Option<OnSymbol<'_>>,
) -> Result<OrderedJson, WorkerError> {
    let mut outcome = StepOutcome::default();
    let report = run_swing_catalyst(
        conn,
        &mut outcome,
        day,
        user_id,
        Some(provider),
        Some(symbols),
        on_symbol,
    )
    .await?;
    Ok(report.as_detail())
}

/// The names the page lists for `scope`, sorted by symbol (`dict(sorted({symbol: id}))`).
///
/// # Errors
/// The view's (the database).
pub async fn listed_symbols(
    conn: &mut sqlx::PgConnection,
    user_id: i64,
    scope_all: bool,
    today_utc: chrono::NaiveDate,
) -> Result<Vec<(String, i64)>, WorkerError> {
    let listed = view::overlap(conn, user_id, Some(user_id), scope_all, None, today_utc)
        .await
        .map_err(|p| WorkerError::python("RuntimeError", p.detail))?;
    let mut symbols: Vec<(String, i64)> = Vec::new();
    for row in listed.rows {
        pyjson_update(&mut symbols, row.symbol, row.instrument_id);
    }
    symbols.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(symbols)
}

/// `on_symbol`'s `_progress(done, total)`: every name's count saved to the status key, in order.
async fn write_progress(
    mut cache: MultiplexedConnection,
    key: String,
    mut status: Vec<(String, OrderedJson)>,
    mut receiver: mpsc::UnboundedReceiver<(i64, i64)>,
) -> Result<(), WorkerError> {
    while let Some((done, total)) = receiver.recv().await {
        pyjson::update(&mut status, "done", int(done));
        pyjson::update(&mut status, "total", int(total));
        let text = pyjson::dumps(&OrderedJson::Object(status.clone()))
            .map_err(|e| WorkerError::python("ValueError", e))?;
        let _: () = cache
            .set_ex(&key, text, STATUS_TTL_SECONDS)
            .await
            .map_err(|e| WorkerError::python("ConnectionError", e.to_string()))?;
    }
    Ok(())
}

fn pyjson_update(entries: &mut Vec<(String, i64)>, key: String, value: i64) {
    if let Some(slot) = entries.iter_mut().find(|(k, _)| *k == key) {
        slot.1 = value;
    } else {
        entries.push((key, value));
    }
}

/// `overlap_catalyst_scan_task(user_id, scope="actionable")`.
///
/// # Errors
/// Redis's; the scan's own (after the status says `failed`).
pub async fn run(ctx: &TaskContext, user_id: i64, scope: &str) -> Result<OrderedJson, WorkerError> {
    let redis_err = |e: redis::RedisError| WorkerError::python("ConnectionError", e.to_string());
    let client = redis::Client::open(ctx.worker.redis_url.as_str()).map_err(redis_err)?;
    let mut cache = client
        .get_multiplexed_async_connection()
        .await
        .map_err(redis_err)?;
    let key = status_key(user_id);
    let mut status: Vec<(String, OrderedJson)> = vec![
        ("state".to_owned(), s("running")),
        ("scope".to_owned(), s(scope)),
        ("started_at".to_owned(), s(py_isoformat_utc(ctx.now_utc()))),
        ("total".to_owned(), OrderedJson::Null),
        ("done".to_owned(), int(0)),
    ];
    let save = |status: &Vec<(String, OrderedJson)>| {
        pyjson::dumps(&OrderedJson::Object(status.clone()))
            .map_err(|e| WorkerError::python("ValueError", e))
    };
    let _: () = cache
        .set_ex(&key, save(&status)?, STATUS_TTL_SECONDS)
        .await
        .map_err(redis_err)?;
    let day = ctx.today_ist();
    let provider = build_provider().await?;
    let scope_all = scope == "all";

    let outcome: Result<OrderedJson, WorkerError> = async {
        let mut tx = ctx.db.begin().await?;
        let symbols =
            listed_symbols(&mut tx, user_id, scope_all, ctx.now_utc().date_naive()).await?;
        let total = i64::try_from(symbols.len()).unwrap_or(i64::MAX);
        pyjson::update(&mut status, "done", int(0));
        pyjson::update(&mut status, "total", int(total));
        let _: () = cache
            .set_ex(&key, save(&status)?, STATUS_TTL_SECONDS)
            .await
            .map_err(redis_err)?;
        let (sender, receiver) = mpsc::unbounded_channel();
        let writer = tokio::spawn(write_progress(
            cache.clone(),
            key.clone(),
            status.clone(),
            receiver,
        ));
        let mut progress = move |done: i64, total: i64| {
            let _ = sender.send((done, total));
        };
        let detail = feed_loop(
            &mut tx,
            day,
            user_id,
            &provider,
            &symbols,
            Some(&mut progress),
        )
        .await;
        drop(progress);
        let written = writer
            .await
            .map_err(|e| WorkerError::python("RuntimeError", e.to_string()))?;
        let detail = detail?;
        written?;
        // `on_symbol(len, len)` after the last name.
        if total > 0 {
            pyjson::update(&mut status, "done", int(total));
            pyjson::update(&mut status, "total", int(total));
        }
        tx.commit().await?;
        Ok(detail)
    }
    .await;

    let result = match outcome {
        Err(e) => {
            pyjson::update(&mut status, "state", s("failed"));
            pyjson::update(&mut status, "error", s(error_text(&e)));
            Err(e)
        }
        Ok(detail) => {
            pyjson::update(&mut status, "state", s("done"));
            for k in ["announcements", "earnings_dates", "rows_written"] {
                pyjson::update(
                    &mut status,
                    k,
                    detail.get(k).cloned().unwrap_or(OrderedJson::Null),
                );
            }
            let failed = match detail.get("failed") {
                Some(OrderedJson::Array(items)) if !items.is_empty() => {
                    OrderedJson::Array(items.clone())
                }
                _ => OrderedJson::Array(Vec::new()),
            };
            pyjson::update(&mut status, "failed", failed);
            let _: () = cache
                .set_ex(LAYA_WAKE_KEY, "1", LAYA_WAKE_TTL_SECONDS)
                .await
                .map_err(redis_err)?;
            let mut out = vec![
                ("user_id".to_owned(), int(user_id)),
                ("scope".to_owned(), s(scope)),
            ];
            if let OrderedJson::Object(entries) = detail {
                for (k, v) in entries {
                    pyjson::update(&mut out, &k, v);
                }
            }
            Ok(OrderedJson::Object(out))
        }
    };
    pyjson::update(
        &mut status,
        "finished_at",
        s(py_isoformat_utc(ctx.now_utc())),
    );
    let _: () = cache
        .set_ex(&key, save(&status)?, STATUS_TTL_SECONDS)
        .await
        .map_err(redis_err)?;
    let _: i64 = cache.del(lock_key(user_id)).await.map_err(redis_err)?;
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_and_stamps_are_pythons() {
        assert_eq!(status_key(1), "overlap:catalyst-scan:1");
        assert_eq!(lock_key(7), "overlap:catalyst-scan:7:lock");
        let t = DateTime::parse_from_rfc3339("2026-10-30T07:45:00Z")
            .map(|t| t.with_timezone(&Utc))
            .unwrap_or_default();
        assert_eq!(py_isoformat_utc(t), "2026-10-30T07:45:00+00:00");
        let t = DateTime::parse_from_rfc3339("2026-10-30T07:45:00.000250Z")
            .map(|t| t.with_timezone(&Utc))
            .unwrap_or_default();
        assert_eq!(py_isoformat_utc(t), "2026-10-30T07:45:00.000250+00:00");
    }
}
