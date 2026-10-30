//! `baskfy_api.alerts.dispatch` (with `ensure_run`, `resolve_runs`, `build_section`, `_record`,
//! `webhook_payload`, `_recipient_addresses`) and `baskfy_api.webhooks.enqueue` — the nightly
//! screen-alert email and the webhook fan-out, as `tasks/alerts.run_alert_dispatch` runs them.
//!
//! One pass, screen by screen in id order (each screen's two `screen_run` rows loaded once; a
//! screen with no run for the date is run first, `ensure_run`), then every active alert in
//! `(user_id, id)` order, then the emails (single alerts first, then each user's digest), then the
//! webhooks. Every send and every skip is a `screen_alert_delivery` row, unique on
//! `(alert, as_of)`: a second dispatch for a date sends nothing (house rule 7). Webhooks are only
//! **enqueued** here (`webhook_delivery`, unique on `(endpoint, idempotency_key)`); the sweep
//! sends them.

use baskfy_api::Settings;
use baskfy_api::mailer::Mailer;
use baskfy_core::screener::{ScreenDefinition, ScreenError};
use baskfy_db::OrderedJson;
use chrono::{DateTime, Datelike as _, Duration, NaiveDate, Utc};
use sha2::{Digest as _, Sha256};
use sqlx::postgres::PgRow;
use sqlx::{PgConnection, Row as _};

use super::alert_mail::{AlertRow, AlertSection, screen_alert};
use super::diff::{
    MIN_REPORTABLE_MOVE, RankedRow, ScreenDiff, diff_runs, len_i64, rows_from_results,
};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::gate::{current_data_version, execute_screen_raising, record_screen_run};
use crate::steps::log_warning;

/// `DEFAULT_WEEKLY_WEEKDAY`: Friday.
const DEFAULT_WEEKLY_WEEKDAY: i64 = 4;
/// `UNSUBSCRIBE_TOKEN_HEX`.
const UNSUBSCRIBE_TOKEN_HEX: usize = 32;

/// `SkipReason`.
const NO_RUN: &str = "no_run_for_date";
const NO_PREVIOUS_RUN: &str = "no_previous_run_in_window";
const DEFINITION_CHANGED: &str = "definition_changed";
const NO_CHANGE: &str = "no_change";

/// A screen as the dispatch reads it.
#[derive(Debug, Clone)]
struct ScreenRow {
    id: i64,
    public_id: String,
    name: String,
    definition: OrderedJson,
    columns: OrderedJson,
}

/// An active alert.
#[derive(Debug, Clone)]
struct AlertRec {
    id: i64,
    public_id: String,
    user_id: i64,
    screen_id: i64,
    frequency: String,
    weekday: Option<i64>,
    top_n: Option<i64>,
    min_move: i64,
    digest: bool,
    last_run_id: Option<i64>,
    last_sent_at: Option<DateTime<Utc>>,
}

/// An active webhook endpoint.
#[derive(Debug, Clone)]
struct EndpointRec {
    id: i64,
    screen_id: i64,
    events: OrderedJson,
}

/// A `screen_run` row.
#[derive(Debug, Clone)]
struct RunRec {
    id: i64,
    as_of: NaiveDate,
    definition_hash: String,
    results: OrderedJson,
}

/// `RunPair`.
#[derive(Debug, Clone)]
struct RunPair {
    current: Option<RunRec>,
    previous: Option<RunRec>,
    skip_reason: Option<&'static str>,
}

/// `DispatchReport`.
#[derive(Debug)]
struct Report {
    as_of: NaiveDate,
    alerts_considered: i64,
    emails_sent: i64,
    alerts_sent: i64,
    alerts_skipped: i64,
    alerts_failed: i64,
    webhooks_enqueued: i64,
    skips: Vec<(String, i64)>,
}

impl Report {
    fn skip(&mut self, reason: &str) {
        self.alerts_skipped += 1;
        match self.skips.iter_mut().find(|(k, _)| k == reason) {
            Some(slot) => slot.1 += 1,
            None => self.skips.push((reason.to_owned(), 1)),
        }
    }

    fn as_dict(&self) -> OrderedJson {
        object([
            ("as_of", s(self.as_of.format("%Y-%m-%d").to_string())),
            ("alerts_considered", int(self.alerts_considered)),
            ("emails_sent", int(self.emails_sent)),
            ("alerts_sent", int(self.alerts_sent)),
            ("alerts_skipped", int(self.alerts_skipped)),
            ("alerts_failed", int(self.alerts_failed)),
            ("webhooks_enqueued", int(self.webhooks_enqueued)),
            (
                "skips",
                OrderedJson::Object(
                    self.skips
                        .iter()
                        .map(|(k, n)| (k.clone(), int(*n)))
                        .collect(),
                ),
            ),
        ])
    }
}

/// HMAC-SHA256 (RFC 2104), as Python's `hmac.new(key, msg, hashlib.sha256)`.
fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    const BLOCK: usize = 64;
    let mut block = [0_u8; BLOCK];
    if key.len() > BLOCK {
        let digest = Sha256::digest(key);
        block[..digest.len()].copy_from_slice(&digest);
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(block.map(|b| b ^ 0x36));
    inner.update(message);
    let inner = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(block.map(|b| b ^ 0x5c));
    outer.update(inner);
    outer.finalize().into()
}

/// `unsubscribe_token(settings, public_id)`.
fn unsubscribe_token(settings: &Settings, public_id: &str) -> Result<String, WorkerError> {
    let signing = settings.webhook_signing_secret.expose();
    let master = if signing.is_empty() {
        settings.jwt_secret.expose()
    } else {
        signing
    };
    if master.is_empty() {
        return Err(WorkerError::python(
            "RuntimeError",
            "No BASKFY_WEBHOOK_SIGNING_SECRET or BASKFY_JWT_SECRET, so an unsubscribe link \
             could not be signed. Alerts are refused rather than sent without one.",
        ));
    }
    let mac = hmac_sha256(master.as_bytes(), format!("alert:{public_id}").as_bytes());
    let hex: String = mac.iter().map(|b| format!("{b:02x}")).collect();
    Ok(hex.chars().take(UNSUBSCRIBE_TOKEN_HEX).collect())
}

/// `is_due(alert, day)` (the query keeps only active alerts).
fn is_due(alert: &AlertRec, day: NaiveDate) -> bool {
    if alert.frequency == "daily" {
        return true;
    }
    let wanted = alert.weekday.unwrap_or(DEFAULT_WEEKLY_WEEKDAY);
    i64::from(day.weekday().num_days_from_monday()) == wanted
}

/// `xs[:cap]` with Python's slice semantics (a negative cap counts from the end).
fn head<T: Clone>(xs: &[T], cap: i64) -> Vec<T> {
    let n = len_i64(xs.len());
    let end = if cap < 0 {
        (n + cap).max(0)
    } else {
        cap.min(n)
    };
    xs.iter()
        .take(usize::try_from(end).unwrap_or(0))
        .cloned()
        .collect()
}

fn screen_of(row: &PgRow) -> Result<ScreenRow, WorkerError> {
    Ok(ScreenRow {
        id: row.try_get("s_id")?,
        public_id: row.try_get("s_public_id")?,
        name: row.try_get("s_name")?,
        definition: baskfy_db::row::jsonb(row, "s_definition")?,
        columns: baskfy_db::row::jsonb(row, "s_columns")?,
    })
}

fn run_of(row: &PgRow) -> Result<RunRec, WorkerError> {
    Ok(RunRec {
        id: row.try_get("id")?,
        as_of: row.try_get("as_of")?,
        definition_hash: row.try_get("definition_hash")?,
        results: baskfy_db::row::jsonb(row, "results")?,
    })
}

const RUN_COLUMNS: &str = "screen_run.id, screen_run.as_of, screen_run.definition_hash, \
                           screen_run.results";

/// `resolve_runs(session, screen_id, as_of, lookback_days=...)`.
async fn resolve_runs(
    conn: &mut PgConnection,
    screen_id: i64,
    as_of: NaiveDate,
    lookback_days: i64,
) -> Result<RunPair, WorkerError> {
    let current = sqlx::query(&format!(
        "SELECT {RUN_COLUMNS} FROM screen_run WHERE screen_run.screen_id = $1::BIGINT AND \
         screen_run.as_of = $2::DATE ORDER BY screen_run.created_at DESC, screen_run.id DESC \
         LIMIT $3::INTEGER"
    ))
    .bind(screen_id)
    .bind(as_of)
    .bind(1_i32)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(current) = current.as_ref().map(run_of).transpose()? else {
        return Ok(RunPair {
            current: None,
            previous: None,
            skip_reason: Some(NO_RUN),
        });
    };
    let earliest = Duration::try_days(lookback_days)
        .and_then(|d| as_of.checked_sub_signed(d))
        .ok_or_else(|| {
            WorkerError::python("OverflowError", "date value out of range".to_owned())
        })?;
    let previous = sqlx::query(&format!(
        "SELECT {RUN_COLUMNS} FROM screen_run WHERE screen_run.screen_id = $1::BIGINT AND \
         screen_run.as_of < $2::DATE AND screen_run.as_of >= $3::DATE ORDER BY screen_run.as_of \
         DESC, screen_run.id DESC LIMIT $4::INTEGER"
    ))
    .bind(screen_id)
    .bind(as_of)
    .bind(earliest)
    .bind(1_i32)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(previous) = previous.as_ref().map(run_of).transpose()? else {
        return Ok(RunPair {
            current: Some(current),
            previous: None,
            skip_reason: Some(NO_PREVIOUS_RUN),
        });
    };
    let reason =
        (previous.definition_hash != current.definition_hash).then_some(DEFINITION_CHANGED);
    Ok(RunPair {
        current: Some(current),
        previous: Some(previous),
        skip_reason: reason,
    })
}

/// `list(screen.columns)` as the screener takes it.
fn columns_of(screen: &ScreenRow) -> Result<Vec<String>, WorkerError> {
    let OrderedJson::Array(items) = &screen.columns else {
        return Err(WorkerError::python(
            "TypeError",
            format!("screen {}: columns is not a list", screen.public_id),
        ));
    };
    items
        .iter()
        .map(|c| match c {
            OrderedJson::Str(s) => Ok(s.clone()),
            other => Err(WorkerError::python(
                "TypeError",
                format!(
                    "screen {}: a column is not a string: {other:?}",
                    screen.public_id
                ),
            )),
        })
        .collect()
}

/// `ensure_run(session, screen, as_of)`: run the screen for `as_of` and record it, unless its
/// stored definition no longer validates (`None`, logged).
async fn ensure_run(
    conn: &mut PgConnection,
    screen: &ScreenRow,
    as_of: NaiveDate,
) -> Result<Option<i64>, WorkerError> {
    let payload = baskfy_api::routes::screens::payload_of(&screen.definition);
    let definition = match ScreenDefinition::model_validate(&payload) {
        Ok(d) => d,
        Err(ScreenError::Validation(_)) => {
            log_warning(&format!(
                "screen has an invalid definition; no alert run written (screen_public_id={})",
                screen.public_id
            ));
            return Ok(None);
        }
        Err(other) => {
            return Err(WorkerError::python(other.python_type(), other.to_string()));
        }
    };
    let columns = columns_of(screen)?;
    let data_version = current_data_version(conn).await?;
    let result =
        execute_screen_raising(conn, &definition, as_of, data_version, &columns, None).await?;
    let hash = definition.definition_hash();
    record_screen_run(conn, screen.id, &result, &hash).await?;
    let found: Option<(i64,)> = sqlx::query_as(
        "SELECT screen_run.id FROM screen_run WHERE screen_run.screen_id = $1::BIGINT AND \
         screen_run.as_of = $2::DATE AND screen_run.definition_hash = $3::VARCHAR",
    )
    .bind(screen.id)
    .bind(as_of)
    .bind(&hash)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(found.map(|(id,)| id))
}

/// `_symbols(session, ids)`: `id → (symbol, name)`.
async fn symbols(
    conn: &mut PgConnection,
    ids: &[i64],
) -> Result<Vec<(i64, String, String)>, WorkerError> {
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    let rows: Vec<(i64, String, String)> = sqlx::query_as(
        "SELECT instrument.id, instrument.symbol, instrument.name FROM instrument WHERE \
         instrument.id = ANY($1::BIGINT[])",
    )
    .bind(ids)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows)
}

fn named(names: &[(i64, String, String)], id: i64) -> Option<&(i64, String, String)> {
    names.iter().find(|(k, _, _)| *k == id)
}

/// `_row(names, instrument_id, rank, previous)`.
fn alert_row(
    names: &[(i64, String, String)],
    id: i64,
    rank: i64,
    previous: Option<i64>,
) -> AlertRow {
    let (symbol, name) = named(names, id).map_or_else(
        || (format!("#{id}"), String::new()),
        |(_, s, n)| (s.clone(), n.clone()),
    );
    AlertRow {
        symbol,
        name,
        rank,
        previous_rank: previous,
    }
}

/// `build_section(session, screen=, alert=, diff=, settings=)`.
async fn build_section(
    conn: &mut PgConnection,
    screen: &ScreenRow,
    alert: &AlertRec,
    diff: &ScreenDiff,
    settings: &Settings,
) -> Result<AlertSection, WorkerError> {
    let names = symbols(conn, &diff.instrument_ids()).await?;
    let cap = settings.alert_max_rows;
    let origin = &settings.web_origin;
    let token = unsubscribe_token(settings, &alert.public_id)?;
    Ok(AlertSection {
        screen_name: screen.name.clone(),
        screen_url: format!("{origin}/screens/{}", screen.public_id),
        unsubscribe_url: format!("{origin}/alerts/unsubscribe?token={token}"),
        as_of: diff.current_as_of,
        previous_as_of: diff.previous_as_of,
        entries: head(&diff.entries, cap)
            .iter()
            .map(|r| alert_row(&names, r.instrument_id, r.rank, None))
            .collect(),
        exits: head(&diff.exits, cap)
            .iter()
            .map(|r| alert_row(&names, r.instrument_id, r.rank, None))
            .collect(),
        movers: head(&diff.rank_changes, cap)
            .iter()
            .map(|c| {
                alert_row(
                    &names,
                    c.instrument_id,
                    c.current_rank,
                    Some(c.previous_rank),
                )
            })
            .collect(),
        entry_total: diff.entry_count(),
        exit_total: diff.exit_count(),
        mover_total: diff.change_count(),
        held_count: diff.held_count,
    })
}

/// `_already_delivered(session, alert_id, as_of)`.
async fn already_delivered(
    conn: &mut PgConnection,
    alert_id: i64,
    as_of: NaiveDate,
) -> Result<bool, WorkerError> {
    let row: Option<(i64,)> = sqlx::query_as(
        "SELECT screen_alert_delivery.id FROM screen_alert_delivery WHERE \
         screen_alert_delivery.alert_id = $1::BIGINT AND screen_alert_delivery.as_of = $2::DATE",
    )
    .bind(alert_id)
    .bind(as_of)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.is_some())
}

/// `_record(...)`: one delivery row, `ON CONFLICT DO NOTHING`. `detail` is `None` → JSON `null`
/// (SQLAlchemy's `JSONB` persists `None` as JSON, not SQL `NULL`).
async fn record(
    conn: &mut PgConnection,
    alert_id: i64,
    as_of: NaiveDate,
    previous_as_of: Option<NaiveDate>,
    status: &str,
    diff: Option<&ScreenDiff>,
    detail: &OrderedJson,
) -> Result<(), WorkerError> {
    let detail = detail
        .to_python_json()
        .map_err(|e| WorkerError::python("TypeError", e.to_string()))?;
    let count =
        |f: fn(&ScreenDiff) -> i64| diff.map_or(0, |d| i32::try_from(f(d)).unwrap_or(i32::MAX));
    sqlx::query(
        "INSERT INTO screen_alert_delivery (alert_id, as_of, previous_as_of, status, entry_count, \
         exit_count, change_count, detail) VALUES ($1::BIGINT, $2::DATE, $3::DATE, $4::VARCHAR, \
         $5::INTEGER, $6::INTEGER, $7::INTEGER, CAST($8 AS JSONB)) ON CONFLICT (alert_id, as_of) \
         DO NOTHING",
    )
    .bind(alert_id)
    .bind(as_of)
    .bind(previous_as_of)
    .bind(status)
    .bind(count(ScreenDiff::entry_count))
    .bind(count(ScreenDiff::exit_count))
    .bind(count(ScreenDiff::change_count))
    .bind(detail)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

fn reason(text: &str) -> OrderedJson {
    object([("reason", s(text))])
}

/// `webhook_payload(screen, diff, names, event)`.
fn webhook_payload(
    screen: &ScreenRow,
    diff: &ScreenDiff,
    names: &[(i64, String, String)],
    event: &str,
) -> OrderedJson {
    let rows: &[RankedRow] = if event == "screen.entries" {
        &diff.entries
    } else {
        &diff.exits
    };
    object([
        ("event", s(event)),
        (
            "screen",
            object([
                ("public_id", s(screen.public_id.clone())),
                ("name", s(screen.name.clone())),
            ]),
        ),
        (
            "as_of",
            s(diff.current_as_of.format("%Y-%m-%d").to_string()),
        ),
        (
            "previous_as_of",
            s(diff.previous_as_of.format("%Y-%m-%d").to_string()),
        ),
        (
            "instruments",
            OrderedJson::Array(
                rows.iter()
                    .map(|r| {
                        let symbol = named(names, r.instrument_id)
                            .map_or_else(|| format!("#{}", r.instrument_id), |(_, s, _)| s.clone());
                        object([("symbol", s(symbol)), ("rank", int(r.rank))])
                    })
                    .collect(),
            ),
        ),
    ])
}

/// `webhooks.enqueue(session, endpoint, event=, payload=, idempotency_key=, now=)`.
async fn enqueue_webhook(
    conn: &mut PgConnection,
    endpoint_id: i64,
    event: &str,
    payload: &OrderedJson,
    idempotency_key: &str,
    moment: DateTime<Utc>,
) -> Result<(), WorkerError> {
    let payload = payload
        .to_python_json()
        .map_err(|e| WorkerError::python("TypeError", e.to_string()))?;
    sqlx::query(
        "INSERT INTO webhook_delivery (endpoint_id, event, idempotency_key, payload, status, \
         attempts, next_attempt_at) VALUES ($1::BIGINT, $2::VARCHAR, $3::VARCHAR, CAST($4 AS \
         JSONB), $5::VARCHAR, $6::INTEGER, $7::TIMESTAMP WITH TIME ZONE) ON CONFLICT \
         (endpoint_id, idempotency_key) DO NOTHING",
    )
    .bind(endpoint_id)
    .bind(event)
    .bind(idempotency_key)
    .bind(payload)
    .bind("pending")
    .bind(0_i32)
    .bind(moment)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

fn opt_i64(row: &PgRow, column: &str) -> Result<Option<i64>, WorkerError> {
    // `weekday` is smallint, `top_n` integer, `last_run_id` bigint.
    let raw: Result<Option<i16>, _> = row.try_get(column);
    if let Ok(v) = raw {
        return Ok(v.map(i64::from));
    }
    let raw: Result<Option<i32>, _> = row.try_get(column);
    if let Ok(v) = raw {
        return Ok(v.map(i64::from));
    }
    Ok(row.try_get::<Option<i64>, _>(column)?)
}

async fn load_alerts(conn: &mut PgConnection) -> Result<Vec<(AlertRec, ScreenRow)>, WorkerError> {
    let rows = sqlx::query(
        "SELECT a.id, a.public_id, a.user_id, a.screen_id, a.frequency, a.weekday, a.top_n, \
         a.min_move, a.digest, a.last_run_id, a.last_sent_at, s.id AS s_id, s.public_id AS \
         s_public_id, s.name AS s_name, s.definition AS s_definition, s.columns AS s_columns \
         FROM screen_alert AS a JOIN screen AS s ON s.id = a.screen_id WHERE a.is_active IS true \
         ORDER BY a.user_id, a.id",
    )
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|row| {
            let alert = AlertRec {
                id: row.try_get("id")?,
                public_id: row.try_get("public_id")?,
                user_id: row.try_get("user_id")?,
                screen_id: row.try_get("screen_id")?,
                frequency: row.try_get("frequency")?,
                weekday: opt_i64(row, "weekday")?,
                top_n: opt_i64(row, "top_n")?,
                min_move: i64::from(row.try_get::<i32, _>("min_move")?),
                digest: row.try_get("digest")?,
                last_run_id: opt_i64(row, "last_run_id")?,
                last_sent_at: row.try_get("last_sent_at")?,
            };
            Ok((alert, screen_of(row)?))
        })
        .collect()
}

async fn load_endpoints(
    conn: &mut PgConnection,
) -> Result<Vec<(EndpointRec, ScreenRow)>, WorkerError> {
    let rows = sqlx::query(
        "SELECT e.id, e.screen_id, e.events, s.id AS s_id, s.public_id AS s_public_id, s.name \
         AS s_name, s.definition AS s_definition, s.columns AS s_columns FROM webhook_endpoint AS \
         e JOIN screen AS s ON s.id = e.screen_id WHERE e.is_active IS true ORDER BY e.id",
    )
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|row| {
            let endpoint = EndpointRec {
                id: row.try_get("id")?,
                screen_id: row.try_get("screen_id")?,
                events: baskfy_db::row::jsonb(row, "events")?,
            };
            Ok((endpoint, screen_of(row)?))
        })
        .collect()
}

/// `_recipient_addresses(session, user_ids)`.
async fn recipient_addresses(
    conn: &mut PgConnection,
    user_ids: &[i64],
) -> Result<Vec<(i64, String)>, WorkerError> {
    if user_ids.is_empty() {
        return Ok(Vec::new());
    }
    let mut ids = user_ids.to_vec();
    ids.sort_unstable();
    ids.dedup();
    let rows: Vec<(i64, String)> = sqlx::query_as(
        "SELECT app_user.id, app_user.email FROM app_user WHERE app_user.id = ANY($1::BIGINT[])",
    )
    .bind(&ids)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows)
}

/// The ORM's flush of `alert.last_run_id = ...; alert.last_sent_at = moment`: an `UPDATE` only
/// for a net change, with `updated_at`'s `onupdate=now()`.
async fn mark_alert(
    conn: &mut PgConnection,
    alert: &AlertRec,
    run_id: i64,
    moment: DateTime<Utc>,
) -> Result<(), WorkerError> {
    if alert.last_run_id == Some(run_id) && alert.last_sent_at == Some(moment) {
        return Ok(());
    }
    sqlx::query(
        "UPDATE screen_alert SET last_run_id = $1::BIGINT, last_sent_at = $2::TIMESTAMP WITH \
         TIME ZONE, updated_at = now() WHERE screen_alert.id = $3::BIGINT",
    )
    .bind(run_id)
    .bind(moment)
    .bind(alert.id)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

type DiffRows = (Vec<RankedRow>, Vec<RankedRow>);

/// `dispatch(session, as_of=, mailer=, settings=)` → `report.as_dict()`.
pub(super) async fn dispatch(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    mailer: &Mailer,
    settings: &Settings,
    moment: DateTime<Utc>,
) -> Result<OrderedJson, WorkerError> {
    let mut report = Report {
        as_of,
        alerts_considered: 0,
        emails_sent: 0,
        alerts_sent: 0,
        alerts_skipped: 0,
        alerts_failed: 0,
        webhooks_enqueued: 0,
        skips: Vec::new(),
    };
    let alerts = load_alerts(conn).await?;
    let endpoints = load_endpoints(conn).await?;

    let mut screen_ids: Vec<i64> = alerts
        .iter()
        .map(|(a, _)| a.screen_id)
        .chain(endpoints.iter().map(|(e, _)| e.screen_id))
        .collect();
    screen_ids.sort_unstable();
    screen_ids.dedup();
    let mut screens_by_id: Vec<(i64, ScreenRow)> = Vec::new();
    for (_, screen) in alerts
        .iter()
        .map(|(a, s)| (a.id, s))
        .chain(endpoints.iter().map(|(e, s)| (e.id, s)))
    {
        match screens_by_id.iter_mut().find(|(k, _)| *k == screen.id) {
            Some(slot) => slot.1 = screen.clone(),
            None => screens_by_id.push((screen.id, screen.clone())),
        }
    }

    let lookback = settings.alert_lookback_days;
    let mut pairs: Vec<(i64, RunPair)> = Vec::new();
    let mut rows_cache: Vec<(i64, DiffRows)> = Vec::new();
    for &screen_id in &screen_ids {
        let mut pair = resolve_runs(conn, screen_id, as_of, lookback).await?;
        if pair.skip_reason == Some(NO_RUN) {
            let screen_row = screens_by_id
                .iter()
                .find(|(k, _)| *k == screen_id)
                .map(|(_, s)| s.clone());
            if let Some(screen_row) = screen_row
                && ensure_run(conn, &screen_row, as_of).await?.is_some()
            {
                pair = resolve_runs(conn, screen_id, as_of, lookback).await?;
            }
        }
        if pair.skip_reason.is_none()
            && let (Some(current), Some(previous)) = (&pair.current, &pair.previous)
        {
            rows_cache.push((
                screen_id,
                (
                    rows_from_results(&previous.results)?,
                    rows_from_results(&current.results)?,
                ),
            ));
        }
        pairs.push((screen_id, pair));
    }
    let pair_of = |id: i64| pairs.iter().find(|(k, _)| *k == id).map(|(_, p)| p);
    let rows_of = |id: i64| rows_cache.iter().find(|(k, _)| *k == id).map(|(_, r)| r);

    // --- emails ---------------------------------------------------------
    let mut per_user: Vec<(i64, Vec<(AlertRec, AlertSection)>)> = Vec::new();
    let mut singles: Vec<(AlertRec, AlertSection)> = Vec::new();

    for (alert, screen) in &alerts {
        report.alerts_considered += 1;
        if !is_due(alert, as_of) {
            continue;
        }
        if already_delivered(conn, alert.id, as_of).await? {
            continue;
        }
        let Some(pair) = pair_of(alert.screen_id) else {
            return Err(WorkerError::python("KeyError", alert.screen_id.to_string()));
        };
        if let Some(why) = pair.skip_reason {
            record(
                conn,
                alert.id,
                as_of,
                pair.previous.as_ref().map(|p| p.as_of),
                "skipped",
                None,
                &reason(why),
            )
            .await?;
            report.skip(why);
            continue;
        }
        let (Some(current_run), Some(previous_run)) = (&pair.current, &pair.previous) else {
            continue;
        };
        let Some((before, after)) = rows_of(alert.screen_id) else {
            return Err(WorkerError::python("KeyError", alert.screen_id.to_string()));
        };
        let diff = diff_runs(
            before,
            after,
            previous_run.as_of,
            current_run.as_of,
            alert.min_move.max(MIN_REPORTABLE_MOVE),
            alert.top_n,
        )?;
        if diff.is_empty() {
            record(
                conn,
                alert.id,
                as_of,
                Some(previous_run.as_of),
                "skipped",
                Some(&diff),
                &reason(NO_CHANGE),
            )
            .await?;
            report.skip(NO_CHANGE);
            continue;
        }
        let section = build_section(conn, screen, alert, &diff, settings).await?;
        if alert.digest {
            match per_user.iter_mut().find(|(u, _)| *u == alert.user_id) {
                Some(slot) => slot.1.push((alert.clone(), section)),
                None => per_user.push((alert.user_id, vec![(alert.clone(), section)])),
            }
        } else {
            singles.push((alert.clone(), section));
        }
        mark_alert(conn, alert, current_run.id, moment).await?;
    }

    let user_ids: Vec<i64> = singles
        .iter()
        .map(|(a, _)| a.user_id)
        .chain(
            per_user
                .iter()
                .flat_map(|(_, bundle)| bundle.iter().map(|(a, _)| a.user_id)),
        )
        .collect();
    let recipients = recipient_addresses(conn, &user_ids).await?;
    let address_of = |user_id: i64| {
        recipients
            .iter()
            .find(|(k, _)| *k == user_id)
            .map(|(_, e)| e.clone())
    };
    let manage = format!("{}/alerts", settings.web_origin);

    for (alert, section) in &singles {
        let Some(address) = address_of(alert.user_id) else {
            report.skip("no_email_address");
            continue;
        };
        let sent = match screen_alert(&address, std::slice::from_ref(section), &manage) {
            Some(message) => mailer.deliver(&message).await,
            None => false,
        };
        record(
            conn,
            alert.id,
            as_of,
            Some(section.previous_as_of),
            if sent { "sent" } else { "failed" },
            None,
            &if sent {
                OrderedJson::Null
            } else {
                reason("email_not_sent")
            },
        )
        .await?;
        if sent {
            report.emails_sent += 1;
            report.alerts_sent += 1;
        } else {
            report.alerts_failed += 1;
        }
    }

    for (user_id, bundle) in &per_user {
        let Some(address) = address_of(*user_id) else {
            report.skip("no_email_address");
            continue;
        };
        let sections: Vec<AlertSection> = bundle.iter().map(|(_, s)| s.clone()).collect();
        let sent = match screen_alert(&address, &sections, &manage) {
            Some(message) => mailer.deliver(&message).await,
            None => false,
        };
        if sent {
            report.emails_sent += 1;
        }
        for (alert, section) in bundle {
            record(
                conn,
                alert.id,
                as_of,
                Some(section.previous_as_of),
                if sent { "sent" } else { "failed" },
                None,
                &if sent {
                    OrderedJson::Null
                } else {
                    reason("email_not_sent")
                },
            )
            .await?;
            if sent {
                report.alerts_sent += 1;
            } else {
                report.alerts_failed += 1;
            }
        }
    }

    // --- webhooks -------------------------------------------------------
    for (endpoint, screen) in &endpoints {
        let Some(pair) = pair_of(endpoint.screen_id) else {
            continue;
        };
        let (None, Some(current), Some(previous)) =
            (pair.skip_reason, &pair.current, &pair.previous)
        else {
            continue;
        };
        let Some((before, after)) = rows_of(endpoint.screen_id) else {
            continue;
        };
        let diff = diff_runs(
            before,
            after,
            previous.as_of,
            current.as_of,
            MIN_REPORTABLE_MOVE,
            None,
        )?;
        let names = symbols(conn, &diff.instrument_ids()).await?;
        let events: Vec<String> = match &endpoint.events {
            OrderedJson::Array(items) => items
                .iter()
                .filter_map(|e| match e {
                    OrderedJson::Str(s) => Some(s.clone()),
                    _ => None,
                })
                .collect(),
            _ => Vec::new(),
        };
        for event in events {
            let rows = if event == "screen.entries" {
                &diff.entries
            } else {
                &diff.exits
            };
            if rows.is_empty() {
                continue;
            }
            let key = format!("{}:{}:{event}", screen.public_id, as_of.format("%Y-%m-%d"));
            let payload = webhook_payload(screen, &diff, &names, &event);
            enqueue_webhook(conn, endpoint.id, &event, &payload, &key, moment).await?;
            report.webhooks_enqueued += 1;
        }
    }
    Ok(report.as_dict())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn hmac_matches_rfc_4231_case_2() {
        let mac = hmac_sha256(b"Jefe", b"what do ya want for nothing?");
        let hex: String = mac.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(
            hex,
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
        // A key longer than the block is hashed first (RFC 4231 case 6).
        let mac = hmac_sha256(
            &[0xaa; 131],
            b"Test Using Larger Than Block-Size Key - Hash Key First",
        );
        let hex: String = mac.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(
            hex,
            "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"
        );
    }

    #[test]
    fn the_unsubscribe_token_needs_a_master_secret() {
        let mut settings = Settings::default();
        let e = unsubscribe_token(&settings, "abc").unwrap_err();
        assert_eq!(e.python_type(), "RuntimeError");
        settings.jwt_secret = baskfy_providers::secret::Secret::new("k");
        let t = unsubscribe_token(&settings, "abc").unwrap();
        assert_eq!(t.len(), 32);
        settings.webhook_signing_secret = baskfy_providers::secret::Secret::new("w");
        assert_ne!(unsubscribe_token(&settings, "abc").unwrap(), t);
    }

    #[test]
    fn a_weekly_alert_defaults_to_friday() {
        let mut alert = AlertRec {
            id: 1,
            public_id: "p".to_owned(),
            user_id: 1,
            screen_id: 1,
            frequency: "weekly".to_owned(),
            weekday: None,
            top_n: None,
            min_move: 1,
            digest: false,
            last_run_id: None,
            last_sent_at: None,
        };
        let thursday = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap();
        let friday = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
        assert!(!is_due(&alert, thursday));
        assert!(is_due(&alert, friday));
        alert.weekday = Some(3);
        assert!(is_due(&alert, thursday));
        alert.frequency = "daily".to_owned();
        alert.weekday = Some(0);
        assert!(is_due(&alert, thursday));
    }

    #[test]
    fn slicing_a_cap_follows_python() {
        let xs = [1, 2, 3];
        assert_eq!(head(&xs, 2), vec![1, 2]);
        assert_eq!(head(&xs, 9), vec![1, 2, 3]);
        assert_eq!(head(&xs, -1), vec![1, 2]);
        assert_eq!(head(&xs, -9), Vec::<i32>::new());
        assert_eq!(head(&xs, 0), Vec::<i32>::new());
    }
}
