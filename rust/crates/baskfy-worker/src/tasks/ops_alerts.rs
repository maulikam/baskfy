//! Lane **ops_alerts** — the operational checks and their alerts: abandoned runs, the publish
//! deadline, the Kite token, queue backlog, the webhook sweep, the login nudge — and the Kite
//! session refresh the scheduled nightly borrows ([`refresh_kite_session_quietly`]).
//!
//! | Celery task | Python | Rust |
//! |---|---|---|
//! | `baskfy.ops.reap_abandoned_runs` | `ops.reap_abandoned_runs` + `alerts.dispatch` | [`checks`], [`alerting`] |
//! | `baskfy.ops.check_publish_deadline` | `ops.check_publish_deadline` | [`checks`] |
//! | `baskfy.ops.check_kite_token` | `ops.run_kite_token_check` (once per token per IST day) | [`checks`] |
//! | `baskfy.ops.check_queue_backlog` | `ops.check_queue_backlog` (`LLEN` on the worker's Redis) | [`checks`] |
//! | `baskfy.alerts.sweep_webhooks` | `tasks/alerts.run_webhook_sweep` over `baskfy_api.webhooks` | [`webhooks`] |
//! | `baskfy.kite.login_nudge` | `tasks/kite_login_nudge.run_login_nudge` | [`login_nudge`] |
//! | (nightly's) | `kite_session_cli.refresh_quietly` | [`kite_session`] |
//!
//! Every moment is the task clock's ([`TaskContext::now_utc`]); every alert goes through this
//! lane's full port of `alerts.dispatch` (log, email, the ops webhook; suppression). Nothing here
//! places, modifies or cancels an order or a GTT, or calls a Kite write endpoint (law 2): the one
//! Kite call is the read-only `GET /user/profile`. DECISIONS-RS R8.oa1–R8.oa6.
//!
//! The rest of the modules the lane was handed are covered elsewhere: `purge_accounts` is lane
//! `api_routes`' (`baskfy.accounts.purge`); `window`, `calendar`, `tasks/fundamentals` and
//! `tasks/listings` are wave 5's nightly steps (`window.rs`, `calendar.rs`, `steps::ingest`,
//! `steps::index`); `telemetry` (OpenTelemetry, Prometheus) is not ported (R7.4); the
//! `action_recovery` operator CLI is not ported here (R8.oa6).

mod alerting;
mod checks;
#[cfg(test)]
mod db_tests;
mod kite_session;
mod login_nudge;
mod webhooks;

use baskfy_db::OrderedJson;
use baskfy_providers::settings::ProviderSettings;
use chrono::NaiveDate;

use super::{BoundArgs, Lane, TaskContext, TaskResult};
use crate::error::WorkerError;
use crate::pyjson::{object, s};

/// The tasks this lane has ported (the `tasks` listing and the cutover read it).
pub const PORTED: &[&str] = &[
    "baskfy.ops.reap_abandoned_runs",
    "baskfy.ops.check_publish_deadline",
    "baskfy.ops.check_kite_token",
    "baskfy.ops.check_queue_backlog",
    "baskfy.alerts.sweep_webhooks",
    "baskfy.kite.login_nudge",
];

const _LANE: Lane = Lane::OpsAlerts;

fn sinks(ctx: &TaskContext) -> alerting::Sinks<'_> {
    alerting::Sinks {
        settings: &ctx.settings,
        mailer: None,
        webhook: None,
    }
}

fn provider_settings() -> Result<ProviderSettings, WorkerError> {
    ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))
}

/// `kite_session_cli.refresh_quietly()`: borrow a Kite session before the scheduled nightly —
/// Baskfy's own live one first, then the desk's over SSH; `false` on any refusal.
///
/// Under the harness (`--replay-bundle`) it answers `false` without reaching anything, exactly as
/// the Python side's injection makes `refresh_quietly` a no-op (`task_py.py`, `nightly_py.py`).
///
/// # Errors
/// What Python lets propagate (an unreadable token payload, an `OSError`, a non-JSON profile).
pub async fn refresh_kite_session_quietly(ctx: &TaskContext) -> Result<bool, WorkerError> {
    if ctx.is_replay() {
        return Ok(false);
    }
    kite_session::refresh_quietly(&provider_settings()?, ctx.clock()).await
}

/// Ports `baskfy_worker.tasks.celery_tasks.reap_abandoned_runs_task` — `baskfy.ops.reap_abandoned_runs()`:
/// fail every abandoned run and dispatch one alert each, in one transaction.
///
/// # Errors
/// The database's (rolled back).
pub async fn reap_abandoned_runs_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let mut tx = ctx.db.begin().await?;
    let alerts = checks::reap_abandoned_runs(&mut tx, &ctx.settings, ctx.now_utc()).await?;
    let mut dispatched = Vec::with_capacity(alerts.len());
    for alert in &alerts {
        dispatched.push(alerting::dispatch(alert, &sinks(ctx), ctx.now_utc(), None).await);
    }
    tx.commit().await?;
    Ok(object([
        (
            "reaped",
            OrderedJson::Int(i64::try_from(dispatched.len()).unwrap_or(i64::MAX)),
        ),
        ("alerts", OrderedJson::Array(dispatched)),
    ]))
}

/// Ports `baskfy_worker.tasks.celery_tasks.check_publish_deadline_task` — `baskfy.ops.check_publish_deadline()`.
///
/// # Errors
/// The database's.
pub async fn check_publish_deadline_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let mut tx = ctx.db.begin().await?;
    let alert = checks::check_publish_deadline(&mut tx, &ctx.settings, ctx.now_utc()).await?;
    let dispatched = match alert {
        Some(alert) => alerting::dispatch(&alert, &sinks(ctx), ctx.now_utc(), None).await,
        None => OrderedJson::Null,
    };
    tx.commit().await?;
    Ok(object([("alert", dispatched)]))
}

/// `swing_health.is_session_day` on a session of its own, per call (the token check's calendar).
struct DbCalendar<'a>(&'a TaskContext);

impl checks::SessionCalendar for DbCalendar<'_> {
    async fn is_session(&self, day: NaiveDate) -> Result<bool, WorkerError> {
        let mut tx = self.0.db.begin().await?;
        let session = checks::is_session_day(&mut tx, day).await?;
        tx.commit().await?;
        Ok(session)
    }
}

/// Ports `baskfy_worker.tasks.celery_tasks.check_kite_token_task` — `baskfy.ops.check_kite_token()`.
///
/// # Errors
/// Provider settings that do not parse.
pub async fn check_kite_token_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let provider = provider_settings()?;
    let cache = checks::RedisCache::from_url(&ctx.worker.redis_url);
    Ok(checks::run_kite_token_check(
        &DbCalendar(ctx),
        cache.as_ref(),
        &sinks(ctx),
        &provider,
        ctx.clock(),
    )
    .await)
}

/// Ports `baskfy_worker.tasks.celery_tasks.check_queue_backlog_task` — `baskfy.ops.check_queue_backlog()`.
///
/// # Errors
/// None: an unreachable broker is a log line.
pub async fn check_queue_backlog_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let _ = args;
    let Some(cache) = checks::RedisCache::from_url(&ctx.worker.redis_url) else {
        return Ok(object([
            ("alerts", OrderedJson::Array(Vec::new())),
            ("detail", s("no broker client could be built")),
        ]));
    };
    let alerts = checks::check_queue_backlog(&cache, &checks::QUEUE_NAMES, &ctx.settings).await;
    let mut dispatched = Vec::with_capacity(alerts.len());
    for alert in &alerts {
        dispatched.push(alerting::dispatch(alert, &sinks(ctx), ctx.now_utc(), None).await);
    }
    Ok(object([("alerts", OrderedJson::Array(dispatched))]))
}

/// Ports `baskfy_worker.tasks.celery_tasks.sweep_webhooks_task` — `baskfy.alerts.sweep_webhooks(limit=100)`.
///
/// # Errors
/// The database's (the whole sweep rolls back); `WebhookNotConfigured`; a wrong-typed `limit`.
pub async fn sweep_webhooks_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    let limit = args.int("limit")?;
    let mut tx = ctx.db.begin().await?;
    let report = webhooks::run_webhook_sweep(
        &mut tx,
        &ctx.settings,
        ctx.now_utc(),
        limit,
        webhooks::HttpTransport::new,
        &webhooks::SystemResolver,
    )
    .await?;
    tx.commit().await?;
    Ok(report.as_dict())
}

/// `repr(value)` for `NudgeWindow(value)`'s `ValueError`.
fn py_repr(value: Option<&OrderedJson>) -> String {
    match value {
        Some(OrderedJson::Str(text)) => baskfy_providers::py::repr_str(text),
        Some(OrderedJson::Int(i)) => i.to_string(),
        Some(OrderedJson::Bool(true)) => "True".to_owned(),
        Some(OrderedJson::Bool(false)) => "False".to_owned(),
        Some(OrderedJson::Null) | None => "None".to_owned(),
        Some(other) => crate::pyjson::dumps(other).unwrap_or_default(),
    }
}

/// Ports `baskfy_worker.tasks.celery_tasks.kite_login_nudge_task` — `baskfy.kite.login_nudge(window="first")`.
///
/// # Errors
/// `ValueError` for a window that is not `first`/`second`; the database's; an `OSError` reading
/// the token blob (as Python).
pub async fn kite_login_nudge_task(ctx: &TaskContext, args: BoundArgs) -> TaskResult {
    if !ctx.worker.kite_login_nudge_enabled {
        return Ok(object([(
            "skipped",
            s("BASKFY_KITE_LOGIN_NUDGE_ENABLED is false"),
        )]));
    }
    let Some(user_id) = ctx.sole_user_id() else {
        return Ok(object([(
            "skipped",
            s("no BASKFY_SOLE_USER_ID configured"),
        )]));
    };
    if std::env::var("BASKFY_BROKER_OAUTH_STATE_PATH")
        .unwrap_or_default()
        .trim()
        .is_empty()
    {
        return Ok(object([(
            "skipped",
            s(
                "BASKFY_BROKER_OAUTH_STATE_PATH is not set, so a state minted by the worker could \
               not be read by the API's callback and the login would fail at the end",
            ),
        )]));
    }
    let raw = args.value("window");
    let window = match raw {
        Some(OrderedJson::Str(text)) => login_nudge::NudgeWindow::parse(text),
        _ => None,
    }
    .ok_or_else(|| {
        WorkerError::python(
            "ValueError",
            format!("{} is not a valid NudgeWindow", py_repr(raw)),
        )
    })?;
    let token_path = token_store_path();
    let key = std::env::var("BASKFY_KITE_TOKEN_ENCRYPTION_KEY")
        .unwrap_or_default()
        .trim()
        .to_owned();
    let store = baskfy_providers::tokens::AccessTokenStore::with_clock(
        token_path.clone(),
        baskfy_providers::secret::Secret::new(key),
        ctx.clock(),
    );
    let state_dir = token_path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map_or_else(
            || std::path::PathBuf::from("."),
            std::path::Path::to_path_buf,
        );
    let api_key = std::env::var("BASKFY_KITE_API_KEY")
        .unwrap_or_default()
        .trim()
        .to_owned();
    let url_for = |_: login_nudge::NudgeWindow| -> Result<login_nudge::LoginUrl, String> {
        let (url, _state) =
            baskfy_api::routes::brokers::kite_login_url_for_worker(&api_key, user_id);
        Ok(login_nudge::LoginUrl {
            url,
            valid_for_minutes: baskfy_api::routes::brokers::STATE_VALID_FOR_MINUTES,
        })
    };
    let to = ctx.worker.kite_login_nudge_to.trim().to_owned();
    let mailer = alerting::ApiMailer(&ctx.settings);
    let now = ctx
        .now_utc()
        .with_timezone(&baskfy_providers::tokens::ist());
    let inputs = login_nudge::NudgeInputs {
        now,
        token_store: &store,
        mailer: &mailer,
        login_url_for: &url_for,
        to: &to,
        state_dir: &state_dir,
        window,
        account: login_nudge::DEFAULT_ACCOUNT,
    };
    let mut tx = ctx.db.begin().await?;
    let day = now.date_naive();
    let report =
        login_nudge::run_login_nudge(&inputs, checks::is_session_day(&mut tx, day)).await?;
    match report {
        Ok(report) => {
            tx.commit().await?;
            Ok(report.as_detail())
        }
        Err(raised) => Err(WorkerError::python(raised.class, raised.message)),
    }
}

/// `broker_oauth.token_store_path()`: `BASKFY_BROKER_TOKEN_PATH`, then `BASKFY_KITE_TOKEN_PATH`,
/// then `.secrets/broker-token.enc`.
fn token_store_path() -> std::path::PathBuf {
    let read = |name: &str| std::env::var(name).unwrap_or_default().trim().to_owned();
    let broker = read("BASKFY_BROKER_TOKEN_PATH");
    let kite = read("BASKFY_KITE_TOKEN_PATH");
    std::path::PathBuf::from(if !broker.is_empty() {
        broker
    } else if !kite.is_empty() {
        kite
    } else {
        ".secrets/broker-token.enc".to_owned()
    })
}
