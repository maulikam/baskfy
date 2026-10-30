//! `baskfy_worker.tasks.backtests.run_backtest_job` as the Celery task runs it: one transaction
//! that claims the row (`queued` → `running`, `FOR UPDATE`), simulates, writes the three CSV
//! artefacts, and records the outcome — `done` with the metrics and the curve, or `failed` with
//! `"<class>: <message>"` for every failure Python catches (`BacktestError`, `ValueError`,
//! `ArchiveError`). Anything else rolls the transaction back and raises, so the row stays
//! `queued`, as Python's `run_in_session` leaves it.
//!
//! Progress goes to Redis twice per frame, as Python's `RedisPublisher` does: `SET` on
//! `backtest:progress:<id>` (thirty minutes) and `PUBLISH` on `backtest:events:<id>`; a Redis
//! failure is logged and never stops the run.

use std::path::Path;
use std::sync::Arc;

use baskfy_core::backtest::engine::{BacktestConfig, BacktestProgress};
use baskfy_core::screener::{ScreenDefinition, ScreenError};
use baskfy_db::OrderedJson;
use baskfy_providers::archive::RawArchive;
use chrono::{DateTime, Utc};
use sqlx::{PgConnection, Row as _};

use super::loader::{Stop, execute_backtest, notes_for};
use super::payload::{
    artefact_bytes, artefact_key, build_payload, events_channel, progress_frame, progress_key,
};
use crate::error::WorkerError;
use crate::tasks::TaskContext;

/// `PROGRESS_TTL_SECONDS`: the last frame outlives the run by thirty minutes.
pub(super) const PROGRESS_TTL_SECONDS: u64 = 30 * 60;

const STATUS_QUEUED: &str = "queued";
const STATUS_RUNNING: &str = "running";
const STATUS_DONE: &str = "done";
const STATUS_FAILED: &str = "failed";

const LOGGER: &str = "baskfy_worker.tasks.backtests";

/// `RedisPublisher`: redis-py's synchronous client, which connects on a command, not on
/// construction, and reconnects on the next one after a failure.
pub(super) struct Publisher {
    client: Option<redis::Client>,
    connection: Option<redis::Connection>,
}

impl Publisher {
    /// `build_publisher(settings)`: `None`-like (a publisher that drops every frame) when the URL
    /// does not parse, logged as Python logs it.
    pub(super) fn new(url: &str) -> Self {
        let client = match redis::Client::open(url) {
            Ok(c) => Some(c),
            Err(e) => {
                baskfy_api::logging::error(
                    LOGGER,
                    "no progress publisher",
                    &[("error", &e.to_string())],
                );
                None
            }
        };
        Self {
            client,
            connection: None,
        }
    }

    /// `publish(public_id, payload)`: `SET key payload EX ttl`, then `PUBLISH channel payload`.
    pub(super) fn publish(&mut self, public_id: &str, payload: &str) {
        let Some(client) = &self.client else {
            return;
        };
        let sent = (|| -> Result<(), redis::RedisError> {
            if self.connection.is_none() {
                self.connection = Some(client.get_connection()?);
            }
            let Some(con) = self.connection.as_mut() else {
                return Ok(());
            };
            redis::cmd("SET")
                .arg(progress_key(public_id))
                .arg(payload)
                .arg("EX")
                .arg(PROGRESS_TTL_SECONDS)
                .query::<()>(con)?;
            redis::cmd("PUBLISH")
                .arg(events_channel(public_id))
                .arg(payload)
                .query::<i64>(con)?;
            Ok(())
        })();
        if let Err(e) = sent {
            self.connection = None;
            baskfy_api::logging::warning(
                LOGGER,
                "backtest progress publish failed",
                &[("error", &e.to_string())],
            );
        }
    }
}

/// `JobOutcome.as_dict()`.
fn outcome(
    public_id: &str,
    status: &str,
    metrics_hash: Option<String>,
    error: Option<String>,
    trades: usize,
    rebalances: usize,
) -> OrderedJson {
    let n = |v: usize| OrderedJson::Int(i64::try_from(v).unwrap_or(i64::MAX));
    OrderedJson::Object(vec![
        (
            "public_id".to_owned(),
            OrderedJson::Str(public_id.to_owned()),
        ),
        ("status".to_owned(), OrderedJson::Str(status.to_owned())),
        (
            "metrics_hash".to_owned(),
            metrics_hash.map_or(OrderedJson::Null, OrderedJson::Str),
        ),
        (
            "error".to_owned(),
            error.map_or(OrderedJson::Null, OrderedJson::Str),
        ),
        ("trades".to_owned(), n(trades)),
        ("rebalances".to_owned(), n(rebalances)),
    ])
}

/// How the job ended before it could record anything.
pub(super) enum JobStop {
    /// `BacktestNotRunnable`: the row is gone, or somebody else already took it.
    NotRunnable(String),
    /// Anything Python does not catch: the transaction rolls back and the task raises.
    Raised(WorkerError),
}

impl From<WorkerError> for JobStop {
    fn from(e: WorkerError) -> Self {
        Self::Raised(e)
    }
}

impl From<sqlx::Error> for JobStop {
    fn from(e: sqlx::Error) -> Self {
        Self::Raised(e.into())
    }
}

fn repr(text: &str) -> String {
    baskfy_providers::py::repr_str(text)
}

/// The claimed row, as far as the job reads it.
struct Claimed {
    id: i64,
    screen_id: Option<i64>,
    config: OrderedJson,
}

/// `_claim(session, public_id)`: move the row from `queued` to `running` under `FOR UPDATE`.
async fn claim(
    conn: &mut PgConnection,
    public_id: &str,
    now: DateTime<Utc>,
) -> Result<Claimed, JobStop> {
    let row = sqlx::query(
        "SELECT backtest.id, backtest.screen_id, backtest.config, backtest.status FROM backtest \
         WHERE backtest.public_id = $1 FOR UPDATE",
    )
    .bind(public_id)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(row) = row else {
        return Err(JobStop::NotRunnable(format!(
            "no backtest with public_id {}",
            repr(public_id)
        )));
    };
    let status: String = row.try_get("status")?;
    if status != STATUS_QUEUED {
        return Err(JobStop::NotRunnable(format!(
            "backtest {} is {}, not {}",
            repr(public_id),
            repr(&status),
            repr(STATUS_QUEUED)
        )));
    }
    let id: i64 = row.try_get("id")?;
    let claimed = Claimed {
        id,
        screen_id: row.try_get("screen_id")?,
        config: baskfy_db::row::jsonb(&row, "config").map_err(WorkerError::from)?,
    };
    sqlx::query(
        "UPDATE backtest SET status = $1, error = NULL, started_at = $2 WHERE backtest.id = $3",
    )
    .bind(STATUS_RUNNING)
    .bind(now)
    .bind(id)
    .execute(&mut *conn)
    .await?;
    Ok(claimed)
}

/// `_migrated(stored)`: a pre-M39 `dividends: "reinvest"` reads as `ignore` (bit-identical
/// arithmetic under its honest name); the stored row is not rewritten.
pub(super) fn migrated(stored: &OrderedJson) -> OrderedJson {
    match stored {
        OrderedJson::Object(entries) => OrderedJson::Object(
            entries
                .iter()
                .map(|(k, v)| {
                    if k == "dividends" && *v == OrderedJson::Str("reinvest".to_owned()) {
                        (k.clone(), OrderedJson::Str("ignore".to_owned()))
                    } else {
                        (k.clone(), v.clone())
                    }
                })
                .collect(),
        ),
        other => other.clone(),
    }
}

/// `BacktestConfig.model_validate(config)`, its refusal as pydantic's `ValidationError` (caught
/// by the job). The message is serde's, not pydantic's: the API writes only configs pydantic
/// accepted, so no row on the bed or the box reaches it (DECISIONS-RS, lane label R-be.2).
pub(super) fn parse_config(stored: &OrderedJson) -> Result<BacktestConfig, Stop> {
    let invalid = |message: String| Stop::caught("ValidationError", message);
    let text = stored
        .to_python_json()
        .map_err(|e| invalid(e.to_string()))?;
    let config: BacktestConfig = serde_json::from_str(&text)
        .map_err(|e| invalid(format!("1 validation error for BacktestConfig\n{e}")))?;
    config.validate().map_err(|e| invalid(e.to_string()))?;
    Ok(config)
}

fn screen_error(e: &ScreenError) -> Stop {
    Stop::from(WorkerError::python(e.python_type(), e.to_string()))
}

/// `_definition(session, row, config)`: the saved screen if it still exists, else the inline
/// definition, else a `BacktestError`.
async fn definition(
    conn: &mut PgConnection,
    screen_id: Option<i64>,
    config: &BacktestConfig,
) -> Result<ScreenDefinition, Stop> {
    if let Some(id) = screen_id {
        let row = sqlx::query("SELECT screen.definition FROM screen WHERE screen.id = $1")
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?;
        if let Some(row) = row {
            let stored = baskfy_db::row::jsonb(&row, "definition").map_err(WorkerError::from)?;
            let payload = baskfy_api::routes::screens::payload_of(&stored);
            return ScreenDefinition::model_validate(&payload).map_err(|e| screen_error(&e));
        }
    }
    if let Some(inline) = &config.screen_definition {
        let json = serde_json::to_string(inline)
            .ok()
            .and_then(|t| OrderedJson::parse(&t).ok())
            .ok_or_else(|| Stop::caught("ValueError", "the inline definition is not JSON"))?;
        let payload = baskfy_api::routes::screens::payload_of(&json);
        return ScreenDefinition::model_validate(&payload).map_err(|e| screen_error(&e));
    }
    Err(Stop::caught(
        "BacktestError",
        "this backtest names neither a screen that still exists nor an inline definition",
    ))
}

/// What the simulation produced, ready to record.
struct Done {
    metrics: OrderedJson,
    equity_curve: OrderedJson,
    trades_key: String,
    metrics_hash: String,
    trades: usize,
    rebalances: usize,
}

/// The part of `run_backtest_job` inside its `try`.
async fn simulate(
    conn: &mut PgConnection,
    claimed: &Claimed,
    public_id: &str,
    archive: &Arc<dyn RawArchive>,
    publisher: &mut Publisher,
    fragility: bool,
) -> Result<Done, Stop> {
    let config = parse_config(&migrated(&claimed.config))?;
    let definition = definition(conn, claimed.screen_id, &config).await?;
    let mut emit = |event: &BacktestProgress| {
        let frame = progress_frame(
            public_id,
            STATUS_RUNNING,
            &event.stage,
            event.completed,
            event.total,
            event.as_of,
            None,
        );
        publisher.publish(public_id, &frame);
    };
    let outcome = execute_backtest(conn, &config, &definition, fragility, Some(&mut emit)).await?;
    for (artefact, bytes) in artefact_bytes(&outcome.result)? {
        archive
            .put(&artefact_key(public_id, artefact), &bytes, "text/csv")
            .await
            .map_err(|e| Stop::from(WorkerError::from(e)))?;
    }
    let stored = build_payload(
        public_id,
        &config,
        &outcome.result,
        outcome.fragility.as_ref(),
        &notes_for(&outcome.loaded, &outcome.result),
    )?;
    Ok(Done {
        metrics: stored.metrics,
        equity_curve: stored.equity_curve,
        trades_key: stored.trades_key,
        metrics_hash: stored.metrics_hash,
        trades: outcome.result.trades.len(),
        rebalances: outcome.result.rebalance_dates.len(),
    })
}

fn jsonb_text(value: &OrderedJson) -> Result<String, WorkerError> {
    value
        .to_python_json()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))
}

/// `run_backtest_job(session, public_id, archive=, publisher=, fragility=)` — claim, simulate,
/// store, record. Failures Python catches land on the row and return `failed`.
async fn run_backtest_job(
    ctx: &TaskContext,
    conn: &mut PgConnection,
    public_id: &str,
    archive: &Arc<dyn RawArchive>,
    publisher: &mut Publisher,
    fragility: bool,
) -> Result<OrderedJson, JobStop> {
    let claimed = claim(conn, public_id, ctx.now_utc()).await?;
    match simulate(conn, &claimed, public_id, archive, publisher, fragility).await {
        Ok(done) => {
            sqlx::query(
                "UPDATE backtest SET metrics = CAST($1 AS JSONB), equity_curve = CAST($2 AS \
                 JSONB), trades_key = $3, status = $4, finished_at = $5 WHERE backtest.id = $6",
            )
            .bind(jsonb_text(&done.metrics)?)
            .bind(jsonb_text(&done.equity_curve)?)
            .bind(&done.trades_key)
            .bind(STATUS_DONE)
            .bind(ctx.now_utc())
            .bind(claimed.id)
            .execute(&mut *conn)
            .await?;
            publisher.publish(
                public_id,
                &progress_frame(public_id, STATUS_DONE, "done", 1, 1, None, None),
            );
            Ok(outcome(
                public_id,
                STATUS_DONE,
                Some(done.metrics_hash),
                None,
                done.trades,
                done.rebalances,
            ))
        }
        Err(Stop::Caught { class, message }) => {
            let error = format!("{class}: {message}");
            sqlx::query(
                "UPDATE backtest SET status = $1, error = $2, finished_at = $3 WHERE backtest.id \
                 = $4",
            )
            .bind(STATUS_FAILED)
            .bind(&error)
            .bind(ctx.now_utc())
            .bind(claimed.id)
            .execute(&mut *conn)
            .await?;
            publisher.publish(
                public_id,
                &progress_frame(public_id, STATUS_FAILED, "failed", 0, 1, None, Some(&error)),
            );
            baskfy_api::logging::warning(
                LOGGER,
                "backtest failed",
                &[("public_id", public_id), ("error", &message)],
            );
            Ok(outcome(public_id, STATUS_FAILED, None, Some(error), 0, 0))
        }
        Err(Stop::Raised(e)) => Err(JobStop::Raised(e)),
    }
}

/// `build_backtest_archive(settings)`: R2 when a bucket is configured, else
/// `BASKFY_INVOICE_LOCAL_DIR` — the same choice invoices make.
fn build_backtest_archive(ctx: &TaskContext) -> Result<Arc<dyn RawArchive>, WorkerError> {
    let provider = baskfy_providers::settings::get_provider_settings()
        .map_err(|e| WorkerError::Settings(e.to_string()))?;
    Ok(baskfy_providers::factory::build_archive(
        provider,
        Some(Path::new(&ctx.settings.invoice_local_dir)),
    )?)
}

/// `run_backtest_task(public_id, fragility=True)`'s body: one transaction (`run_in_session`),
/// committed when the job returns, rolled back when it raises; a redelivered message for a run
/// somebody else took returns `skipped`.
pub(super) async fn run(
    ctx: &TaskContext,
    public_id: &str,
    fragility: bool,
) -> Result<OrderedJson, WorkerError> {
    let archive = build_backtest_archive(ctx)?;
    let mut publisher = Publisher::new(ctx.settings.redis_url.expose());
    let mut tx = ctx.db.begin().await?;
    match run_backtest_job(ctx, &mut tx, public_id, &archive, &mut publisher, fragility).await {
        Ok(result) => {
            tx.commit().await?;
            Ok(result)
        }
        Err(JobStop::NotRunnable(detail)) => {
            tx.rollback().await?;
            Ok(OrderedJson::Object(vec![
                (
                    "public_id".to_owned(),
                    OrderedJson::Str(public_id.to_owned()),
                ),
                ("status".to_owned(), OrderedJson::Str("skipped".to_owned())),
                ("detail".to_owned(), OrderedJson::Str(detail)),
            ]))
        }
        Err(JobStop::Raised(e)) => {
            // Best effort: the error the task raises is the job's, not the rollback's.
            let _ = tx.rollback().await;
            Err(e)
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::*;

    #[test]
    fn a_pre_m39_reinvest_config_reads_as_ignore_and_nothing_else_moves() {
        let stored = OrderedJson::parse(
            r#"{"start": "2024-01-01", "end": "2024-06-30", "dividends": "reinvest"}"#,
        )
        .unwrap();
        let config = parse_config(&migrated(&stored)).unwrap();
        assert_eq!(config.dividends.value(), "ignore");
        let cash = OrderedJson::parse(
            r#"{"start": "2024-01-01", "end": "2024-06-30", "dividends": "cash"}"#,
        )
        .unwrap();
        assert_eq!(migrated(&cash), cash);
    }

    #[test]
    fn a_config_pydantic_would_refuse_is_a_validation_error() {
        for bad in [
            r#"{"start": "2024-06-30", "end": "2024-01-01"}"#,
            r#"{"start": "2024-01-01", "end": "2024-06-30", "surprise": 1}"#,
            r#"{"end": "2024-06-30"}"#,
        ] {
            match parse_config(&OrderedJson::parse(bad).unwrap()) {
                Err(Stop::Caught { class, .. }) => assert_eq!(class, "ValidationError"),
                _ => panic!("{bad} must be refused"),
            }
        }
    }

    #[test]
    fn the_outcome_has_pythons_keys_in_pythons_order() {
        let text = outcome("p", "done", Some("h".into()), None, 3, 2)
            .to_python_json()
            .unwrap();
        assert_eq!(
            text,
            "{\"public_id\":\"p\",\"status\":\"done\",\"metrics_hash\":\"h\",\"error\":null,\
             \"trades\":3,\"rebalances\":2}"
        );
    }

    #[test]
    fn a_publisher_without_a_reachable_redis_drops_frames_quietly() {
        let mut nowhere = Publisher::new("redis://127.0.0.1:1/0");
        nowhere.publish("p", "{}");
        let mut malformed = Publisher::new("not a url");
        malformed.publish("p", "{}");
    }
}
