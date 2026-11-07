//! `baskfy-worker regime …`: the index-led candidate jobs on demand, the 2-year index bar
//! backfill, and the TWT/VBT legacy-vs-index_led backtest table (reads only, rolled back).

use std::io::Write as _;
use std::process::ExitCode;

use baskfy_worker::regime;
use baskfy_worker::tasks::TaskContext;
use baskfy_worker::tasks::sleeve_backtests::regime::{regime_backtest, render};
use baskfy_worker::tasks::swing_live::regime_jobs;
use chrono::{Days, NaiveDate};

fn out(text: &str) {
    let _ = writeln!(std::io::stdout(), "{text}");
}

fn err(text: &str) -> ExitCode {
    let _ = writeln!(std::io::stderr(), "baskfy-worker regime: {text}");
    ExitCode::from(1)
}

fn flag(rest: &[String], name: &str) -> Option<String> {
    rest.iter()
        .position(|a| a == name)
        .and_then(|i| rest.get(i + 1).cloned())
}

fn date_flag(rest: &[String], name: &str) -> Result<Option<NaiveDate>, String> {
    flag(rest, name)
        .map(|v| {
            NaiveDate::parse_from_str(&v, "%Y-%m-%d").map_err(|_| format!("{name} {v:?} is not YYYY-MM-DD"))
        })
        .transpose()
}

async fn last_published(ctx: &TaskContext) -> Result<NaiveDate, String> {
    let mut tx = ctx.db.begin().await.map_err(|e| e.to_string())?;
    let row: Option<(Option<NaiveDate>,)> =
        sqlx::query_as("SELECT max(date) FROM ohlcv_daily WHERE date <= $1")
            .bind(ctx.today_ist())
            .fetch_optional(&mut *tx)
            .await
            .map_err(|e| e.to_string())?;
    tx.rollback().await.map_err(|e| e.to_string())?;
    row.and_then(|r| r.0).ok_or_else(|| "no published bars".to_owned())
}

/// Run one subcommand.
pub async fn run(ctx: &TaskContext, rest: &[String]) -> ExitCode {
    match inner(ctx, rest).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => err(&e),
    }
}

async fn inner(ctx: &TaskContext, rest: &[String]) -> Result<(), String> {
    let sub = rest.first().map_or("", String::as_str);
    match sub {
        "candidates" => {
            let on = match date_flag(rest, "--date")? {
                Some(d) => d,
                None => last_published(ctx).await?,
            };
            let r = regime_jobs::run_nightly(ctx, on)
                .await
                .map_err(|e| e.to_string())?;
            out(&r.as_json().to_string());
        }
        "signals" => {
            let on = match date_flag(rest, "--date")? {
                Some(d) => d,
                None => last_published(ctx).await?,
            };
            let r = regime_jobs::run_signals(ctx, on)
                .await
                .map_err(|e| e.to_string())?;
            out(&r.as_json().to_string());
        }
        "ema-backfill" => {
            let mut tx = ctx.db.begin().await.map_err(|e| e.to_string())?;
            let n = baskfy_api::indices::history::backfill_all_stats(&mut tx).await?;
            tx.commit().await.map_err(|e| e.to_string())?;
            out(&serde_json::json!({"stats_rows": n}).to_string());
        }
        "intraday" => {
            let r = regime_jobs::run_intraday(ctx)
                .await
                .map_err(|e| e.to_string())?;
            out(&r.as_json().to_string());
        }
        "backfill" => {
            let days = flag(rest, "--days")
                .map(|v| v.parse::<u64>().map_err(|e| format!("--days: {e}")))
                .transpose()?
                .unwrap_or(730);
            let r = regime_jobs::backfill_index_bars(ctx, days)
                .await
                .map_err(|e| e.to_string())?;
            out(&r.to_string());
        }
        "backtest" => {
            let end = match date_flag(rest, "--end")? {
                Some(d) => d,
                None => last_published(ctx).await?,
            };
            let start = match date_flag(rest, "--start")? {
                Some(d) => d,
                None => end
                    .checked_sub_days(Days::new(730))
                    .ok_or("date out of range")?,
            };
            let cfg = regime::config();
            let mut tx = ctx.db.begin().await.map_err(|e| e.to_string())?;
            let result = regime_backtest(&mut tx, cfg, start, end).await;
            tx.rollback().await.map_err(|e| e.to_string())?;
            let (rows, covered) = result.map_err(|e| e.to_string())?;
            out(&render(&rows, start, end, covered, cfg));
        }
        other => {
            return Err(format!(
                "unknown subcommand {other:?}: candidates [--date D] | signals [--date D] | \
                 ema-backfill | intraday | \
                 backfill [--days N] | backtest [--start D] [--end D]"
            ));
        }
    }
    Ok(())
}
