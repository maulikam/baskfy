//! Index-led regime jobs: nightly (after the detectors, from the 21:30 freshness check) and
//! intraday (every 15 minutes 09:15–15:30 IST, piggy-backed on the minute's scan sweep because the
//! task registry and Beat table are held to Python's). Candidate rows only; Kite is read-only
//! (`/quote` through the shared limiter). Never a plan, an order or a flag.

use std::collections::HashMap;

use baskfy_core::regime::RegimeMode;
use baskfy_providers::factory::KiteLane;
use chrono::{Datelike, NaiveDate, NaiveTime, Weekday};
use rust_decimal::prelude::ToPrimitive;

use super::TaskContext;
use super::quotes::kite_provider;
use crate::error::WorkerError;
use crate::regime;

/// Minutes between intraday runs.
pub const INTRADAY_EVERY_MINUTES: i64 = 15;

fn in_session(ctx: &TaskContext) -> bool {
    let now = ctx.now_ist();
    let open = NaiveTime::from_hms_opt(9, 15, 0).unwrap_or_default();
    let close = NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default();
    !matches!(now.weekday(), Weekday::Sat | Weekday::Sun)
        && open <= now.time()
        && now.time() <= close
}

/// Nightly run for `as_of` (published bars only).
///
/// # Errors
/// The database's.
pub async fn run_nightly(
    ctx: &TaskContext,
    as_of: NaiveDate,
) -> Result<regime::RunReport, WorkerError> {
    let mut tx = ctx.db.begin().await?;
    let report = regime::run_candidates(
        &mut tx,
        ctx.now_utc(),
        as_of,
        "nightly",
        ctx.sole_user_id(),
        None,
    )
    .await?;
    tx.commit().await?;
    Ok(report)
}

/// Intraday run now: published closes plus each index's live Kite quote.
///
/// # Errors
/// The database's and the provider's.
pub async fn run_intraday(ctx: &TaskContext) -> Result<regime::RunReport, WorkerError> {
    let today = ctx.today_ist();
    let mut tx = ctx.db.begin().await?;
    let symbols = if regime::index_tables_ready(&mut tx).await? {
        regime::quote_symbols(&mut tx, regime::config()).await?
    } else {
        Vec::new()
    };
    tx.commit().await?;
    let mut live: HashMap<String, f64> = HashMap::new();
    if !symbols.is_empty() {
        let provider = kite_provider(ctx, KiteLane::Interactive).await?;
        let wanted: Vec<&str> = symbols.iter().map(|(_, s)| s.as_str()).collect();
        let quotes = provider
            .quotes(&wanted, "NSE")
            .await
            .map_err(WorkerError::Provider)?;
        let by_symbol: HashMap<&str, f64> = quotes
            .iter()
            .filter_map(|q| Some((q.symbol.as_str(), q.last_price.to_f64()?)))
            .filter(|(_, p)| *p > 0.0)
            .collect();
        for (name, sym) in &symbols {
            if let Some(p) = by_symbol.get(sym.as_str()) {
                live.insert(name.clone(), *p);
            }
        }
    }
    let mut tx = ctx.db.begin().await?;
    let report = regime::run_candidates(
        &mut tx,
        ctx.now_utc(),
        today,
        "intraday",
        ctx.sole_user_id(),
        Some(&live),
    )
    .await?;
    tx.commit().await?;
    Ok(report)
}

/// The sweep's hook: an intraday run when one is due. Failures are logged, never raised, so the
/// sweep's own job is untouched; the harness (replay) never runs it.
pub async fn maybe_intraday(ctx: &TaskContext) {
    if ctx.is_replay() || regime::config().mode == RegimeMode::Legacy || !in_session(ctx) {
        return;
    }
    let due = async {
        let mut tx = ctx.db.begin().await?;
        let last = regime::last_intraday_run(&mut tx, ctx.today_ist()).await?;
        tx.commit().await?;
        Ok::<bool, WorkerError>(last.is_none_or(|t| {
            (ctx.now_utc() - t).num_minutes() >= INTRADAY_EVERY_MINUTES
        }))
    }
    .await;
    match due {
        Ok(true) => {
            if let Err(e) = run_intraday(ctx).await {
                crate::steps::log_warning(&format!("regime intraday run failed: {e}"));
            }
        }
        Ok(false) => {}
        Err(e) => crate::steps::log_warning(&format!("regime intraday check failed: {e}")),
    }
}

/// Per-stock regime signals for `on` (every mode: the scan pages show the column).
///
/// # Errors
/// The database's.
pub async fn run_signals(
    ctx: &TaskContext,
    on: NaiveDate,
) -> Result<crate::regime_signal::Report, WorkerError> {
    let mut tx = ctx.db.begin().await?;
    let report = crate::regime_signal::compute(&mut tx, on, ctx.sole_user_id()).await?;
    tx.commit().await?;
    Ok(report)
}

/// A scan job's hook: signals for `on` after its rows are written. Logged, never raised; the
/// harness (replay) never runs it.
pub async fn after_scan(ctx: &TaskContext, on: Option<NaiveDate>) {
    let Some(on) = on else { return };
    if ctx.is_replay() {
        return;
    }
    if let Err(e) = run_signals(ctx, on).await {
        crate::steps::log_warning(&format!("regime signals run failed: {e}"));
    }
}

/// The 21:30 check's hook: a nightly run for the published date. Logged, never raised.
pub async fn after_detect(ctx: &TaskContext, published: Option<NaiveDate>) {
    after_scan(ctx, published).await;
    let Some(on) = published else { return };
    if ctx.is_replay() || regime::config().mode == RegimeMode::Legacy {
        return;
    }
    if let Err(e) = run_nightly(ctx, on).await {
        crate::steps::log_warning(&format!("regime nightly run failed: {e}"));
    }
}

/// Backfill `index_bar_daily` for `days` calendar days back from today through Kite's
/// historical endpoint (the shared rate-limited provider, bulk lane). Existing rows are kept
/// (`ON CONFLICT DO NOTHING`), so the index service's own writes always win.
///
/// # Errors
/// The database's and the provider's.
pub async fn backfill_index_bars(
    ctx: &TaskContext,
    days: u64,
) -> Result<serde_json::Value, WorkerError> {
    use polars::prelude::DataType;
    let end = ctx.today_ist();
    let start = end
        .checked_sub_days(chrono::Days::new(days))
        .ok_or_else(|| WorkerError::python("ValueError", "date out of range"))?;
    let mut tx = ctx.db.begin().await?;
    if !regime::index_tables_ready(&mut tx).await? {
        return Err(WorkerError::python(
            "ValueError",
            "index_bar_daily (0060) is not migrated",
        ));
    }
    let indices: Vec<(i16, String, i64)> = sqlx::query_as(
        "SELECT id, name, kite_token FROM index_master WHERE live AND kite_token IS NOT NULL \
         ORDER BY sort_order",
    )
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    let provider = kite_provider(ctx, KiteLane::Bulk).await?;
    let mut written = 0_u64;
    let mut failed: Vec<String> = Vec::new();
    for (id, name, token) in indices {
        let frame = match provider.daily_bars(token, start, end).await {
            Ok(f) => f,
            Err(e) => {
                crate::steps::log_warning(&format!("index backfill {name}: {e}"));
                failed.push(name);
                continue;
            }
        };
        let col = |c: &str| -> Result<Vec<Option<f64>>, WorkerError> {
            Ok(frame
                .column(c)
                .and_then(|s| s.cast(&DataType::Float64))
                .map_err(|e| WorkerError::python("ValueError", e.to_string()))?
                .f64()
                .map_err(|e| WorkerError::python("ValueError", e.to_string()))?
                .iter()
                .collect())
        };
        let dates = crate::steps::sleeves::for_vbt_jobs::dates(&frame, "date")?;
        let (o, h, l, c) = (col("open")?, col("high")?, col("low")?, col("close")?);
        let mut tx = ctx.db.begin().await?;
        for i in 0..dates.len() {
            let (Some(day), Some(o), Some(h), Some(l), Some(c)) = (
                dates[i].and_then(epoch_day),
                o[i],
                h[i],
                l[i],
                c[i],
            ) else {
                continue;
            };
            let d2 = |x: f64| {
                rust_decimal::Decimal::from_f64_retain(x)
                    .map(|d| d.round_dp(2))
                    .unwrap_or_default()
            };
            let r = sqlx::query(
                "INSERT INTO index_bar_daily (index_id, date, open, high, low, close) \
                 VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (index_id, date) DO NOTHING",
            )
            .bind(id)
            .bind(day)
            .bind(d2(o))
            .bind(d2(h))
            .bind(d2(l))
            .bind(d2(c))
            .execute(&mut *tx)
            .await?;
            written += r.rows_affected();
        }
        tx.commit().await?;
    }
    Ok(serde_json::json!({
        "start": start.to_string(), "end": end.to_string(),
        "rows_written": written, "failed": failed,
    }))
}

fn epoch_day(n: i32) -> Option<NaiveDate> {
    NaiveDate::from_ymd_opt(1970, 1, 1)?.checked_add_signed(chrono::Duration::days(i64::from(n)))
}
