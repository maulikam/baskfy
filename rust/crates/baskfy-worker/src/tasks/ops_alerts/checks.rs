//! The operational checks of `baskfy_worker.ops`: the abandoned-run reaper, the publish
//! deadline, the Kite token check and its delivery rule, and the queue backlog.
//!
//! * [`reap_abandoned_runs`] — a run `running` past `pipeline_stale_after_minutes` (by
//!   `started_at`, never a heartbeat) is marked `failed` at the task clock's moment, one CRITICAL
//!   `pipeline_abandoned` each. Idempotent: a reaped run is no longer `running`.
//! * [`check_publish_deadline`] — on an NSE trading day (the `trading_day` row, exchange 1; an
//!   absent row is not one) with no `pipeline_run` carrying a `data_version` for today (IST), one
//!   CRITICAL `publish_late` naming `BASKFY_PUBLISH_DEADLINE_IST` (20:15 on a bad value).
//! * [`check_kite_token`] / [`run_kite_token_check`] — arithmetic over the stored token's issue
//!   time (no network). Raised only on an NSE session (weekends by weekday; holidays by
//!   `swing_health.is_session_day`, which counts an absent row — or an unreadable calendar — as a
//!   session); delivered at most once per stored token per IST day through a Redis `SET NX`
//!   marker (fail open; released when no sink took the alert).
//! * [`check_queue_backlog`] — `LLEN` of each Celery queue on the worker's Redis; one WARNING per
//!   queue at or over `queue_backlog_threshold`; an unreachable broker is a log line, not an alert.

use std::sync::Arc;

use baskfy_api::Settings;
use baskfy_db::OrderedJson;
use baskfy_providers::clock::Clock;
use baskfy_providers::records::PyDateTime;
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::{AccessTokenStore, ist};
use chrono::{DateTime, Datelike as _, Duration, NaiveDate, NaiveTime, Utc};
use sqlx::PgConnection;

use super::alerting::{Name, OpsAlert, Severity, Sinks, dispatch};
use crate::alerts::py_isoformat;
use crate::error::WorkerError;
use crate::pyjson::{object, s};
use crate::steps::{RunStatus, log_warning};

/// `seed_data.NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i16 = 1;

/// `KITE_TOKEN_ALERT_MARKER_TTL_SECONDS`: 36 hours outlives the IST day the key names.
pub(crate) const KITE_TOKEN_ALERT_MARKER_TTL_SECONDS: u64 = 36 * 60 * 60;

/// `KITE_TOKEN_ALERT_MARKER_PREFIX`.
pub(crate) const KITE_TOKEN_ALERT_MARKER_PREFIX: &str = "ops:kite-token-alert:";

/// `celery_app.QUEUES`, by name, in Python's order.
pub(crate) const QUEUE_NAMES: [&str; 4] = ["ingest", "compute", "backtest", "default"];

// ---------------------------------------------------------------------------------------------
// The reaper
// ---------------------------------------------------------------------------------------------

/// `int((moment - started).total_seconds() // 60)`.
fn whole_minutes(moment: DateTime<Utc>, started: DateTime<Utc>) -> i64 {
    let micros = (moment - started).num_microseconds().unwrap_or(i64::MAX);
    micros.div_euclid(60_000_000)
}

/// `ops.reap_abandoned_runs(session, now=)`: fail every run `running` since before the stale
/// window, oldest trade date first, and return one alert per run. The caller commits.
///
/// # Errors
/// The database's.
pub(crate) async fn reap_abandoned_runs(
    conn: &mut PgConnection,
    settings: &Settings,
    moment: DateTime<Utc>,
) -> Result<Vec<OpsAlert>, WorkerError> {
    let stale_after = settings.pipeline_stale_after_minutes;
    let cutoff = moment - Duration::minutes(stale_after);
    let stale: Vec<(i64, NaiveDate, DateTime<Utc>)> = sqlx::query_as(
        "SELECT id, trade_date, started_at FROM pipeline_run \
         WHERE status = $1 AND started_at < $2 ORDER BY trade_date ASC",
    )
    .bind(RunStatus::Running.value())
    .bind(cutoff)
    .fetch_all(&mut *conn)
    .await?;
    let mut alerts = Vec::with_capacity(stale.len());
    for (id, trade_date, started) in stale {
        let age_minutes = whole_minutes(moment, started);
        sqlx::query("UPDATE pipeline_run SET status = $1, finished_at = $2 WHERE id = $3")
            .bind(RunStatus::Failed.value())
            .bind(moment)
            .bind(id)
            .execute(&mut *conn)
            .await?;
        let day = trade_date.format("%Y-%m-%d").to_string();
        alerts.push(OpsAlert {
            name: Name::PipelineAbandoned,
            severity: Severity::Critical,
            summary: format!(
                "Pipeline run {id} for {day} was abandoned after {age_minutes} minutes and has \
                 been marked failed."
            ),
            labels: vec![
                ("trade_date".to_owned(), day),
                ("run_id".to_owned(), id.to_string()),
            ],
            detail: object([
                ("started_at", s(py_isoformat(started))),
                ("age_minutes", OrderedJson::Int(age_minutes)),
                ("stale_after_minutes", OrderedJson::Int(stale_after)),
                (
                    "likely_cause",
                    s(
                        "the worker process died mid-run; its transaction rolled back, so no \
                       step rows survive for the steps that had completed",
                    ),
                ),
            ]),
        });
    }
    Ok(alerts)
}

// ---------------------------------------------------------------------------------------------
// The publish deadline
// ---------------------------------------------------------------------------------------------

/// Python's `int(text)` for a deadline part: surrounding whitespace, one sign, digits with
/// single underscores between them.
fn py_int(text: &str) -> Option<i64> {
    let t = text.trim_matches(|c: char| c.is_whitespace());
    let body = t.strip_prefix(['+', '-']).unwrap_or(t);
    let ok = !body.is_empty()
        && !body.starts_with('_')
        && !body.ends_with('_')
        && !body.contains("__")
        && body.chars().all(|c| c.is_ascii_digit() || c == '_');
    if !ok {
        return None;
    }
    t.replace('_', "").parse::<i64>().ok()
}

/// `ops._deadline(settings)`: `BASKFY_PUBLISH_DEADLINE_IST` as a time, 20:15 on a bad value.
pub(crate) fn deadline(configured: &str) -> NaiveTime {
    let parsed = configured.split_once(':').and_then(|(h, m)| {
        let (h, m) = (py_int(h)?, py_int(m)?);
        NaiveTime::from_hms_opt(u32::try_from(h).ok()?, u32::try_from(m).ok()?, 0)
    });
    parsed.unwrap_or_else(|| {
        log_warning(&format!(
            "BASKFY_PUBLISH_DEADLINE_IST is not HH:MM; using docs/11's 20:15 configured={configured:?}"
        ));
        NaiveTime::from_hms_opt(20, 15, 0).unwrap_or(NaiveTime::MIN)
    })
}

/// `datetime.isoformat()` for an aware IST moment.
fn ist_isoformat(moment: DateTime<Utc>) -> String {
    PyDateTime::Aware(moment.with_timezone(&ist())).isoformat()
}

/// `ops.check_publish_deadline(session, now=)`: `None` on a non-trading day and on a day that
/// published; otherwise the `publish_late` alert.
///
/// # Errors
/// The database's.
pub(crate) async fn check_publish_deadline(
    conn: &mut PgConnection,
    settings: &Settings,
    now: DateTime<Utc>,
) -> Result<Option<OpsAlert>, WorkerError> {
    let today = now.with_timezone(&ist()).date_naive();
    let trading: Option<(bool,)> = sqlx::query_as(
        "SELECT is_trading_day FROM trading_day WHERE exchange_id = $1 AND date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(today)
    .fetch_optional(&mut *conn)
    .await?;
    if !trading.is_some_and(|(t,)| t) {
        return Ok(None);
    }
    let published: Option<(i64,)> = sqlx::query_as(
        "SELECT id FROM pipeline_run WHERE trade_date = $1 AND data_version IS NOT NULL LIMIT 1",
    )
    .bind(today)
    .fetch_optional(&mut *conn)
    .await?;
    if published.is_some() {
        return Ok(None);
    }
    let at = deadline(&settings.publish_deadline_ist)
        .format("%H:%M")
        .to_string();
    let day = today.format("%Y-%m-%d").to_string();
    Ok(Some(OpsAlert {
        name: Name::PublishLate,
        severity: Severity::Critical,
        summary: format!("{day} is a trading day and nothing has published by {at} IST."),
        labels: vec![("trade_date".to_owned(), day)],
        detail: object([
            ("deadline_ist", s(at)),
            ("checked_at_ist", s(ist_isoformat(now))),
            (
                "slo",
                s("docs/11 §Reliability: published by 20:15 IST on >=95% of trading days"),
            ),
        ]),
    }))
}

// ---------------------------------------------------------------------------------------------
// The Kite token
// ---------------------------------------------------------------------------------------------

fn token_alert(summary: String, detail: OrderedJson, severity: Severity) -> OpsAlert {
    OpsAlert {
        name: Name::KiteTokenExpiring,
        severity,
        summary,
        labels: vec![("provider".to_owned(), "kite".to_owned())],
        detail,
    }
}

/// `ops.check_kite_token(settings, now=)`: `None` with no token store configured (local
/// development) or a token comfortably alive; otherwise the `kite_token_expiring` alert. Reads
/// the encrypted store, never Kite.
pub(crate) fn check_kite_token(
    settings: &Settings,
    provider: &ProviderSettings,
    clock: Arc<dyn Clock>,
    now: DateTime<Utc>,
) -> Option<OpsAlert> {
    if provider.kite_token_path.is_empty() || !provider.kite_token_encryption_key.is_set() {
        return None;
    }
    let store = AccessTokenStore::with_clock(
        provider.kite_token_path.clone(),
        provider.kite_token_encryption_key.clone(),
        clock,
    );
    if !store.exists() {
        return Some(token_alert(
            "No Kite access token is stored; the next bar fetch will fail.".to_owned(),
            object::<&str>([]),
            Severity::Critical,
        ));
    }
    let token = match store.load() {
        Ok(token) => token,
        Err(e) => {
            return Some(token_alert(
                "The stored Kite access token could not be read.".to_owned(),
                object([("error", s(e.python_type()))]),
                Severity::Critical,
            ));
        }
    };
    let issued = PyDateTime::Aware(token.issued_at).isoformat();
    if token.is_expired(now.with_timezone(&ist())) {
        return Some(token_alert(
            "The stored Kite access token has expired; a new login is required.".to_owned(),
            object([("issued_at", s(issued))]),
            Severity::Critical,
        ));
    }
    let hours = settings.kite_token_warning_hours;
    let warn_from = now + Duration::hours(hours);
    if token.is_expired(warn_from.with_timezone(&ist())) {
        return Some(token_alert(
            format!("The stored Kite access token expires within {hours} hours."),
            object([("issued_at", s(issued))]),
            Severity::Warning,
        ));
    }
    None
}

/// `kite_token_alert_marker(alert, day)`: keyed on the token's issue time (never its value), or
/// on the state for the two alerts that name no token.
pub(crate) fn kite_token_alert_marker(alert: &OpsAlert, day: NaiveDate) -> String {
    let token_ref = match alert.detail_get("issued_at") {
        // Always the issue time's isoformat (a string): `check_kite_token` writes nothing else.
        Some(OrderedJson::Str(issued)) => format!("issued:{issued}"),
        _ if alert.detail_get("error").is_some() => "unreadable".to_owned(),
        _ => "absent".to_owned(),
    };
    format!(
        "{KITE_TOKEN_ALERT_MARKER_PREFIX}{}:{token_ref}",
        day.format("%Y-%m-%d")
    )
}

/// The calendar the token check consults (`swing_health.is_session_day` over its own session).
#[allow(async_fn_in_trait)]
pub(crate) trait SessionCalendar {
    /// Is `day` a session? An error is an unreadable calendar.
    async fn is_session(&self, day: NaiveDate) -> Result<bool, WorkerError>;
}

/// The marker store (`build_cache()`'s Redis client): `SET NX EX` and `DEL`.
#[allow(async_fn_in_trait)]
pub(crate) trait MarkerCache {
    /// `SET key "1" NX EX ttl`: `Ok(true)` when this call set it. An error is Redis down.
    async fn claim(&self, key: &str, ttl_seconds: u64) -> Result<bool, String>;
    /// `DEL key`.
    async fn release(&self, key: &str) -> Result<(), String>;
}

/// `kite_token_alert_session_day(calendar, day)`: weekends are not; the calendar decides the
/// rest, and an unreadable calendar is a session (an alert check fails towards speaking).
pub(crate) async fn kite_token_alert_session_day(
    calendar: &impl SessionCalendar,
    day: NaiveDate,
) -> bool {
    if day.weekday().num_days_from_monday() >= 5 {
        return false;
    }
    match calendar.is_session(day).await {
        Ok(session) => session,
        Err(e) => {
            log_warning(&format!(
                "kite token check: the trading calendar could not be read; treating {} as a \
                 session error={}",
                day.format("%Y-%m-%d"),
                e.python_type()
            ));
            true
        }
    }
}

/// `_claim_marker`: true when this run is the first today, or when Redis cannot say.
async fn claim_marker(cache: Option<&impl MarkerCache>, marker: &str) -> bool {
    let Some(cache) = cache else {
        log_warning("kite token check: no Redis client; delivering without de-duplication");
        return true;
    };
    match cache
        .claim(marker, KITE_TOKEN_ALERT_MARKER_TTL_SECONDS)
        .await
    {
        Ok(claimed) => claimed,
        Err(e) => {
            log_warning(&format!(
                "kite token check: Redis is unavailable; delivering without de-duplication \
                 error={e}"
            ));
            true
        }
    }
}

/// `_release_marker`: give the day back when no sink took the alert.
async fn release_marker(cache: Option<&impl MarkerCache>, marker: &str) {
    if let Some(cache) = cache
        && let Err(e) = cache.release(marker).await
    {
        log_warning(&format!(
            "kite token check: could not release an undelivered marker error={e}"
        ));
    }
}

/// `ops.run_kite_token_check(calendar, cache, now=)`: trading days only, delivered once per
/// stored token per IST day.
pub(crate) async fn run_kite_token_check(
    calendar: &impl SessionCalendar,
    cache: Option<&impl MarkerCache>,
    sinks: &Sinks<'_>,
    provider: &ProviderSettings,
    clock: Arc<dyn Clock>,
) -> OrderedJson {
    let moment = clock.now();
    let day = moment.with_timezone(&ist()).date_naive();
    let iso_day = day.format("%Y-%m-%d").to_string();
    if !kite_token_alert_session_day(calendar, day).await {
        return object([
            ("alert", OrderedJson::Null),
            (
                "skipped",
                s(format!("{iso_day} is not an NSE trading session")),
            ),
        ]);
    }
    let Some(alert) = check_kite_token(sinks.settings, provider, Arc::clone(&clock), moment) else {
        return object([("alert", OrderedJson::Null)]);
    };
    let marker = kite_token_alert_marker(&alert, day);
    if !claim_marker(cache, &marker).await {
        let reason = format!(
            "already delivered for this stored token on {iso_day} (IST); delivered at most once \
             per token per trading day"
        );
        log_warning(&format!(
            "kite token check: delivery suppressed reason={reason:?}"
        ));
        let payload = dispatch(&alert, sinks, clock.now(), Some(&reason)).await;
        return object([("alert", payload)]);
    }
    let payload = dispatch(&alert, sinks, clock.now(), None).await;
    let only_log = match &payload {
        OrderedJson::Object(entries) => match entries.iter().find(|(k, _)| k == "delivered_to") {
            Some((_, OrderedJson::Array(items))) => {
                items.len() == 1 && items.first() == Some(&s("log"))
            }
            _ => true,
        },
        _ => true,
    };
    if only_log {
        release_marker(cache, &marker).await;
    }
    object([("alert", payload)])
}

/// The worker's Redis (`build_cache()`): `None` when the URL does not parse.
pub(crate) struct RedisCache(pub(crate) redis::Client);

impl RedisCache {
    /// `Redis.from_url(get_worker_settings().redis_url)`, or `None` (`ValueError`).
    pub(crate) fn from_url(url: &str) -> Option<Self> {
        redis::Client::open(url).ok().map(Self)
    }

    async fn connection(&self) -> Result<redis::aio::MultiplexedConnection, String> {
        self.0
            .get_multiplexed_async_connection()
            .await
            .map_err(|e| e.to_string())
    }

    /// `LLEN key`.
    pub(crate) async fn llen(&self, key: &str) -> Result<i64, String> {
        let mut conn = self.connection().await?;
        redis::cmd("LLEN")
            .arg(key)
            .query_async::<i64>(&mut conn)
            .await
            .map_err(|e| e.to_string())
    }
}

impl MarkerCache for RedisCache {
    async fn claim(&self, key: &str, ttl_seconds: u64) -> Result<bool, String> {
        let mut conn = self.connection().await?;
        let answer: Option<String> = redis::cmd("SET")
            .arg(key)
            .arg("1")
            .arg("NX")
            .arg("EX")
            .arg(ttl_seconds)
            .query_async(&mut conn)
            .await
            .map_err(|e| e.to_string())?;
        Ok(answer.is_some())
    }

    async fn release(&self, key: &str) -> Result<(), String> {
        let mut conn = self.connection().await?;
        redis::cmd("DEL")
            .arg(key)
            .query_async::<i64>(&mut conn)
            .await
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
}

// ---------------------------------------------------------------------------------------------
// The queue backlog
// ---------------------------------------------------------------------------------------------

/// A broker that can answer `LLEN`.
#[allow(async_fn_in_trait)]
pub(crate) trait QueueDepth {
    /// The queue's depth; an error is an unreachable broker.
    async fn depth(&self, queue: &str) -> Result<i64, String>;
}

impl QueueDepth for RedisCache {
    async fn depth(&self, queue: &str) -> Result<i64, String> {
        self.llen(queue).await
    }
}

/// `ops.check_queue_backlog(broker, QUEUES)`: one WARNING per queue at or over the threshold.
pub(crate) async fn check_queue_backlog(
    broker: &impl QueueDepth,
    queues: &[&str],
    settings: &Settings,
) -> Vec<OpsAlert> {
    let threshold = settings.queue_backlog_threshold;
    let mut alerts = Vec::new();
    for queue in queues {
        let depth = match broker.depth(queue).await {
            Ok(d) => d,
            Err(e) => {
                log_warning(&format!("queue depth unavailable queue={queue} error={e}"));
                continue;
            }
        };
        if depth < threshold {
            continue;
        }
        alerts.push(OpsAlert {
            name: Name::QueueBacklog,
            severity: Severity::Warning,
            summary: format!(
                "Celery queue {} has {depth} messages waiting.",
                baskfy_providers::py::repr_str(queue)
            ),
            labels: vec![("queue".to_owned(), (*queue).to_owned())],
            detail: object([
                ("depth", OrderedJson::Int(depth)),
                ("threshold", OrderedJson::Int(threshold)),
            ]),
        });
    }
    alerts
}

/// `swing_health.is_session_day(session, day)`: a named holiday is not a session; a day the
/// calendar does not carry is.
///
/// # Errors
/// The database's.
pub(crate) async fn is_session_day(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<bool, WorkerError> {
    let known: Option<(bool,)> = sqlx::query_as(
        "SELECT is_trading_day FROM trading_day WHERE exchange_id = $1 AND date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(known.is_none_or(|(t,)| t))
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::panic,
        clippy::field_reassign_with_default,
        clippy::assertions_on_constants
    )]
    use std::cell::RefCell;
    use std::collections::BTreeSet;

    use baskfy_api::mailer::Message;
    use baskfy_providers::secret::Secret;

    use super::super::alerting::MailSink;
    use super::*;

    struct FixedClock(DateTime<Utc>);
    impl Clock for FixedClock {
        fn monotonic(&self) -> f64 {
            0.0
        }
        fn now(&self) -> DateTime<Utc> {
            self.0
        }
    }

    fn ist_at(text: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(&format!("{text}+05:30"))
            .unwrap()
            .with_timezone(&Utc)
    }

    struct Calendar(Result<bool, ()>);
    impl SessionCalendar for Calendar {
        async fn is_session(&self, _day: NaiveDate) -> Result<bool, WorkerError> {
            self.0
                .map_err(|()| WorkerError::python("OperationalError", "calendar down"))
        }
    }

    #[derive(Default)]
    struct Markers {
        set: RefCell<BTreeSet<String>>,
        down: bool,
    }
    impl MarkerCache for Markers {
        async fn claim(&self, key: &str, _ttl: u64) -> Result<bool, String> {
            if self.down {
                return Err("Connection refused".to_owned());
            }
            Ok(self.set.borrow_mut().insert(key.to_owned()))
        }
        async fn release(&self, key: &str) -> Result<(), String> {
            self.set.borrow_mut().remove(key);
            Ok(())
        }
    }

    struct Inbox(RefCell<Vec<Message>>, bool);
    impl MailSink for Inbox {
        fn deliver<'a>(&'a self, message: &'a Message) -> super::super::alerting::BoolFuture<'a> {
            Box::pin(async move {
                self.0.borrow_mut().push(message.clone());
                self.1
            })
        }
    }

    struct Fixture {
        _dir: tempfile_lite::Dir,
        provider: ProviderSettings,
        settings: Settings,
    }

    /// A Fernet key and (optionally) a token issued at `issued` (IST wall clock).
    fn fixture(issued: Option<&str>) -> Fixture {
        let dir = tempfile_lite::Dir::new();
        let mut provider = ProviderSettings::default();
        provider.kite_token_path = dir.path().join("kite-token.enc").display().to_string();
        provider.kite_token_encryption_key =
            Secret::new("ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=");
        if let Some(at) = issued {
            let store = AccessTokenStore::new(
                provider.kite_token_path.clone(),
                provider.kite_token_encryption_key.clone(),
            );
            store
                .save("test-token", Some(ist_at(at).with_timezone(&ist())))
                .unwrap();
        }
        let mut settings = Settings::default();
        "ops@example.com".clone_into(&mut settings.ops_alert_email);
        Fixture {
            _dir: dir,
            provider,
            settings,
        }
    }

    async fn run(
        f: &Fixture,
        calendar: &Calendar,
        markers: &Markers,
        inbox: &Inbox,
        now: &str,
    ) -> OrderedJson {
        let sinks = Sinks {
            settings: &f.settings,
            mailer: Some(inbox),
            webhook: None,
        };
        run_kite_token_check(
            calendar,
            Some(markers),
            &sinks,
            &f.provider,
            Arc::new(FixedClock(ist_at(now))),
        )
        .await
    }

    fn get<'a>(v: &'a OrderedJson, key: &str) -> &'a OrderedJson {
        match v {
            OrderedJson::Object(e) => e
                .iter()
                .find(|(k, _)| k == key)
                .map_or(&OrderedJson::Null, |(_, v)| v),
            _ => &OrderedJson::Null,
        }
    }

    // TestKiteTokenCheck.test_no_token_store_configured_is_not_an_incident.
    #[test]
    fn no_token_store_configured_is_not_an_incident() {
        let provider = ProviderSettings::default();
        let clock: Arc<dyn Clock> = Arc::new(FixedClock(ist_at("2026-10-29T10:00:00")));
        assert!(
            check_kite_token(
                &Settings::default(),
                &provider,
                clock,
                ist_at("2026-10-29T10:00:00")
            )
            .is_none()
        );
    }

    // TestKiteTokenCheck.test_an_expired_token_is_critical.
    #[test]
    fn an_expired_token_is_critical() {
        let f = fixture(Some("2026-10-28T08:00:00"));
        let now = ist_at("2026-10-29T10:00:00");
        let alert =
            check_kite_token(&f.settings, &f.provider, Arc::new(FixedClock(now)), now).unwrap();
        assert_eq!(alert.severity, Severity::Critical);
        assert!(alert.summary.contains("has expired"));
        assert_eq!(
            alert.detail_get("issued_at"),
            Some(&s("2026-10-28T08:00:00+05:30"))
        );
    }

    // TestKiteTokenCheck.test_a_fresh_token_warns_before_it_dies_not_after.
    #[test]
    fn a_fresh_token_warns_before_it_dies_not_after() {
        let f = fixture(Some("2026-10-29T08:00:00"));
        let morning = ist_at("2026-10-29T10:00:00");
        assert!(
            check_kite_token(
                &f.settings,
                &f.provider,
                Arc::new(FixedClock(morning)),
                morning
            )
            .is_none()
        );
        let evening = ist_at("2026-10-29T19:00:00");
        let alert = check_kite_token(
            &f.settings,
            &f.provider,
            Arc::new(FixedClock(evening)),
            evening,
        )
        .unwrap();
        assert_eq!(alert.severity, Severity::Warning);
        assert_eq!(
            alert.summary,
            "The stored Kite access token expires within 6 hours."
        );
    }

    #[test]
    fn a_missing_and_an_unreadable_token_are_critical_and_keyed_on_their_state() {
        let f = fixture(None);
        let now = ist_at("2026-10-29T10:00:00");
        let day = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap();
        let absent =
            check_kite_token(&f.settings, &f.provider, Arc::new(FixedClock(now)), now).unwrap();
        assert_eq!(
            kite_token_alert_marker(&absent, day),
            "ops:kite-token-alert:2026-10-29:absent"
        );
        std::fs::write(&f.provider.kite_token_path, b"not a fernet blob").unwrap();
        let unreadable =
            check_kite_token(&f.settings, &f.provider, Arc::new(FixedClock(now)), now).unwrap();
        assert_eq!(
            unreadable.detail_get("error"),
            Some(&s("CredentialsMissing"))
        );
        assert_eq!(
            kite_token_alert_marker(&unreadable, day),
            "ops:kite-token-alert:2026-10-29:unreadable"
        );
    }

    // TestKiteTokenAlertDelivery.test_a_holiday_raises_no_alert.
    #[tokio::test]
    async fn a_holiday_raises_no_alert() {
        let f = fixture(Some("2026-11-09T08:00:00"));
        let (markers, inbox) = (Markers::default(), Inbox(RefCell::new(vec![]), true));
        let out = run(
            &f,
            &Calendar(Ok(false)),
            &markers,
            &inbox,
            "2026-11-10T10:00:00",
        )
        .await;
        assert_eq!(
            get(&out, "skipped"),
            &s("2026-11-10 is not an NSE trading session")
        );
        assert!(inbox.0.borrow().is_empty());
    }

    // TestKiteTokenAlertDelivery.test_a_weekend_raises_no_alert.
    #[tokio::test]
    async fn a_weekend_raises_no_alert() {
        let f = fixture(Some("2026-10-30T08:00:00"));
        let (markers, inbox) = (Markers::default(), Inbox(RefCell::new(vec![]), true));
        let out = run(
            &f,
            &Calendar(Ok(true)),
            &markers,
            &inbox,
            "2026-10-31T10:00:00",
        )
        .await;
        assert_eq!(
            get(&out, "skipped"),
            &s("2026-10-31 is not an NSE trading session")
        );
    }

    // test_an_expired_token_sends_once_on_a_session_and_then_is_suppressed, and
    // test_the_next_session_sends_again.
    #[tokio::test]
    async fn an_expired_token_sends_once_a_session_then_is_suppressed() {
        let f = fixture(Some("2026-10-28T08:00:00"));
        let (markers, inbox) = (Markers::default(), Inbox(RefCell::new(vec![]), true));
        let cal = Calendar(Ok(true));
        let first = run(&f, &cal, &markers, &inbox, "2026-10-29T09:00:00").await;
        assert_eq!(
            get(get(&first, "alert"), "delivered_to"),
            &crate::pyjson::strings(["log", "email"])
        );
        let second = run(&f, &cal, &markers, &inbox, "2026-10-29T10:00:00").await;
        assert!(matches!(
            get(get(&second, "alert"), "suppressed"),
            OrderedJson::Str(_)
        ));
        assert_eq!(inbox.0.borrow().len(), 1);
        let next_day = run(&f, &cal, &markers, &inbox, "2026-10-30T09:00:00").await;
        assert!(matches!(
            get(get(&next_day, "alert"), "suppressed"),
            OrderedJson::Null
        ));
        assert_eq!(inbox.0.borrow().len(), 2);
    }

    // test_a_new_token_sends_its_own_first_warning.
    #[tokio::test]
    async fn a_new_token_sends_its_own_first_warning() {
        let f = fixture(Some("2026-10-29T08:00:00"));
        let (markers, inbox) = (Markers::default(), Inbox(RefCell::new(vec![]), true));
        let cal = Calendar(Ok(true));
        run(&f, &cal, &markers, &inbox, "2026-10-29T19:00:00").await;
        let store = AccessTokenStore::new(
            f.provider.kite_token_path.clone(),
            f.provider.kite_token_encryption_key.clone(),
        );
        store
            .save(
                "second",
                Some(ist_at("2026-10-29T19:30:00").with_timezone(&ist())),
            )
            .unwrap();
        run(&f, &cal, &markers, &inbox, "2026-10-29T20:00:00").await;
        assert_eq!(inbox.0.borrow().len(), 2);
    }

    // test_redis_being_down_still_sends.
    #[tokio::test]
    async fn redis_being_down_still_sends() {
        let f = fixture(Some("2026-10-28T08:00:00"));
        let markers = Markers {
            down: true,
            ..Markers::default()
        };
        let inbox = Inbox(RefCell::new(vec![]), true);
        run(
            &f,
            &Calendar(Ok(true)),
            &markers,
            &inbox,
            "2026-10-29T09:00:00",
        )
        .await;
        run(
            &f,
            &Calendar(Ok(true)),
            &markers,
            &inbox,
            "2026-10-29T10:00:00",
        )
        .await;
        assert_eq!(inbox.0.borrow().len(), 2);
    }

    // test_an_unreadable_calendar_still_sends.
    #[tokio::test]
    async fn an_unreadable_calendar_still_sends() {
        let f = fixture(Some("2026-10-28T08:00:00"));
        let (markers, inbox) = (Markers::default(), Inbox(RefCell::new(vec![]), true));
        run(
            &f,
            &Calendar(Err(())),
            &markers,
            &inbox,
            "2026-10-29T09:00:00",
        )
        .await;
        assert_eq!(inbox.0.borrow().len(), 1);
    }

    // test_an_undelivered_alert_does_not_burn_the_day.
    #[tokio::test]
    async fn an_undelivered_alert_does_not_burn_the_day() {
        let f = fixture(Some("2026-10-28T08:00:00"));
        let (markers, inbox) = (Markers::default(), Inbox(RefCell::new(vec![]), false));
        run(
            &f,
            &Calendar(Ok(true)),
            &markers,
            &inbox,
            "2026-10-29T09:00:00",
        )
        .await;
        assert!(markers.set.borrow().is_empty());
        run(
            &f,
            &Calendar(Ok(true)),
            &markers,
            &inbox,
            "2026-10-29T10:00:00",
        )
        .await;
        assert_eq!(inbox.0.borrow().len(), 2);
    }

    struct Depths(Vec<Result<i64, String>>);
    impl QueueDepth for Depths {
        async fn depth(&self, queue: &str) -> Result<i64, String> {
            let i = QUEUE_NAMES.iter().position(|q| *q == queue).unwrap();
            self.0[i].clone()
        }
    }

    // TestQueueBacklog.test_only_queues_over_the_threshold_alert.
    #[tokio::test]
    async fn only_queues_over_the_threshold_alert() {
        let broker = Depths(vec![Ok(5), Ok(100), Ok(250), Ok(99)]);
        let alerts = check_queue_backlog(&broker, &QUEUE_NAMES, &Settings::default()).await;
        let named: Vec<&str> = alerts.iter().map(|a| a.labels[0].1.as_str()).collect();
        assert_eq!(named, ["compute", "backtest"]);
        assert_eq!(
            alerts[0].summary,
            "Celery queue 'compute' has 100 messages waiting."
        );
    }

    // TestQueueBacklog.test_an_unreachable_broker_is_not_an_alert_storm.
    #[tokio::test]
    async fn an_unreachable_broker_is_not_an_alert_storm() {
        let broker = Depths(vec![Err("down".to_owned()); 4]);
        assert!(
            check_queue_backlog(&broker, &QUEUE_NAMES, &Settings::default())
                .await
                .is_empty()
        );
    }

    #[test]
    fn the_deadline_parses_as_python_and_falls_back_to_2015() {
        assert_eq!(
            deadline("20:15"),
            NaiveTime::from_hms_opt(20, 15, 0).unwrap()
        );
        assert_eq!(
            deadline(" 7 : 05"),
            NaiveTime::from_hms_opt(7, 5, 0).unwrap()
        );
        assert_eq!(
            deadline("21:3_0"),
            NaiveTime::from_hms_opt(21, 30, 0).unwrap()
        );
        for bad in ["2015", "25:00", "20:15:00", "x:y", "", "20:-1"] {
            assert_eq!(
                deadline(bad),
                NaiveTime::from_hms_opt(20, 15, 0).unwrap(),
                "{bad}"
            );
        }
    }

    #[test]
    fn whole_minutes_floors_as_python() {
        let start = ist_at("2026-10-29T10:00:00");
        assert_eq!(whole_minutes(start + Duration::seconds(5459), start), 90);
        assert_eq!(whole_minutes(start + Duration::seconds(5460), start), 91);
    }

    /// A temporary directory (the worker has no tempfile dev-dependency).
    mod tempfile_lite {
        use std::path::{Path, PathBuf};
        use std::sync::atomic::{AtomicU64, Ordering};

        static NEXT: AtomicU64 = AtomicU64::new(0);

        pub(super) struct Dir(PathBuf);

        impl Dir {
            pub(super) fn new() -> Self {
                let n = NEXT.fetch_add(1, Ordering::SeqCst);
                let path = std::env::temp_dir()
                    .join(format!("baskfy-ops-alerts-{}-{n}", std::process::id()));
                let _ = std::fs::remove_dir_all(&path);
                std::fs::create_dir_all(&path).unwrap();
                Self(path)
            }

            pub(super) fn path(&self) -> &Path {
                &self.0
            }
        }

        impl Drop for Dir {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
    }
}
