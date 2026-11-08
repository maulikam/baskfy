//! The webhook sweeper — `tasks/alerts.run_webhook_sweep` over `baskfy_api.webhooks`'
//! `due_deliveries` and `attempt_delivery` (Prompt 20 deliverable 4).
//!
//! Every `pending` delivery whose `next_attempt_at` has come (at the task clock's moment), on an
//! active endpoint, oldest first, at most `limit`: the endpoint's URL is re-checked against the
//! SSRF guard (AUDIT 2.6 — a refusal fails the delivery for good), the body is signed
//! (`X-Baskfy-Signature: t=<unix>,v1=<hex hmac-sha256 over "<t>.<body>">`, the secret derived
//! from `BASKFY_WEBHOOK_SIGNING_SECRET` or the JWT secret, never stored) and POSTed once. A 2xx
//! (or anything under 400: httpx follows no redirect) is delivered; a 4xx other than
//! 408/425/429 is permanent; anything else retries on `base * factor**(attempts-1)` until
//! `webhook_max_attempts`; `webhook_failure_threshold` consecutive failures disable the endpoint.
//!
//! The rows are the ORM's: the deliveries and their endpoints are loaded once, the endpoint's
//! state is carried in memory across its deliveries (SQLAlchemy's identity map), and each attempt
//! writes what the flush writes (`webhook_endpoint.updated_at` is Postgres's `now()`, as its
//! `onupdate`). The whole sweep is one transaction (`run_in_session`).
//!
//! **Approximated** (DECISIONS-RS R8.oa3): a transport failure's `last_error` names httpx's
//! class (`ConnectError`, `ConnectTimeout`, `ReadTimeout`, ...) by reqwest's error kind, and its
//! message is reqwest's, not httpx's.

use std::collections::HashMap;
use std::net::IpAddr;

use baskfy_api::Settings;
use baskfy_api::routes::webhook_endpoints::{
    WebhookUrlRefusal, assert_public_webhook_url_with, webhook_host_encodable,
};
use baskfy_db::OrderedJson;
use chrono::{DateTime, Duration, Utc};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::pyjson::{int, object};
use crate::steps::log_warning;

/// `SWEEP_BATCH`: the registry's default for `limit` (bounded, so a backlog is worked through
/// over several sweeps).
#[cfg(test)]
pub(crate) const SWEEP_BATCH: i64 = 100;
/// `SIGNATURE_HEADER`.
pub(crate) const SIGNATURE_HEADER: &str = "X-Baskfy-Signature";
/// `EVENT_HEADER`.
pub(crate) const EVENT_HEADER: &str = "X-Baskfy-Event";
/// `DELIVERY_HEADER`.
pub(crate) const DELIVERY_HEADER: &str = "X-Baskfy-Delivery";
/// `SECRET_DISPLAY_PREFIX`.
const SECRET_DISPLAY_PREFIX: &str = "whsec_";
/// `SECRET_HEX_LENGTH`.
const SECRET_HEX_LENGTH: usize = 32;
/// `RETRYABLE_CLIENT_STATUSES`: a 4xx that still deserves a retry.
const RETRYABLE_CLIENT_STATUSES: [u16; 3] = [408, 425, 429];
/// `MAX_ERROR_CHARS`.
const MAX_ERROR_CHARS: usize = 500;

/// `SweepReport`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct SweepReport {
    pub(crate) attempted: i64,
    pub(crate) delivered: i64,
    pub(crate) retrying: i64,
    pub(crate) failed: i64,
}

impl SweepReport {
    /// `as_dict()`.
    pub(crate) fn as_dict(self) -> OrderedJson {
        object([
            ("attempted", int(self.attempted)),
            ("delivered", int(self.delivered)),
            ("retrying", int(self.retrying)),
            ("failed", int(self.failed)),
        ])
    }
}

/// What one POST answered: the status, or the transport failure as `"<Class>: <message>"`.
pub(crate) type PostOutcome = Result<u16, String>;

/// The HTTP transport (`httpx.AsyncClient().post`), injected by the tests.
#[allow(async_fn_in_trait)]
pub(crate) trait Transport {
    /// POST `body` with `headers` to `url`, `timeout` seconds.
    async fn post(
        &self,
        url: &str,
        body: &[u8],
        headers: &[(&str, String)],
        timeout: f64,
    ) -> PostOutcome;
}

/// The name resolver the SSRF guard uses (`resolve_webhook_host`).
#[allow(async_fn_in_trait)]
pub(crate) trait Resolver {
    /// Every address `host` resolves to, or `None` (`OSError`: "does not resolve").
    async fn resolve(&self, host: &str) -> Option<Vec<IpAddr>>;
}

/// The production resolver: `getaddrinfo` through `baskfy_providers::net::GuardedResolver`, so
/// the harness's network block (armed under `--replay-bundle`) refuses every non-loopback name
/// exactly as `task_py.py`'s guard makes Python's `getaddrinfo` raise `OSError`.
pub(crate) struct SystemResolver;

impl Resolver for SystemResolver {
    async fn resolve(&self, host: &str) -> Option<Vec<IpAddr>> {
        baskfy_providers::net::GuardedResolver
            .lookup(host)
            .await
            .ok()
            .map(|addrs| addrs.into_iter().map(|a| a.ip()).collect())
    }
}

/// The production transport: a guarded reqwest client that, like httpx's default, follows no
/// redirect.
pub(crate) struct HttpTransport {
    client: baskfy_providers::net::GuardedClient,
}

impl HttpTransport {
    /// `httpx.AsyncClient()`.
    ///
    /// # Errors
    /// The TLS backend's.
    pub(crate) fn new() -> Result<Self, WorkerError> {
        let client = baskfy_providers::net::GuardedClient::from_builder(
            baskfy_providers::net::client_builder().redirect(reqwest::redirect::Policy::none()),
        )?;
        Ok(Self { client })
    }
}

/// httpx's class for a reqwest failure.
fn httpx_class(e: &reqwest::Error) -> &'static str {
    if e.is_timeout() {
        if e.is_connect() {
            "ConnectTimeout"
        } else {
            "ReadTimeout"
        }
    } else if e.is_connect() {
        "ConnectError"
    } else if e.is_redirect() {
        "TooManyRedirects"
    } else if e.is_body() || e.is_decode() {
        "ReadError"
    } else {
        "HTTPError"
    }
}

impl Transport for HttpTransport {
    async fn post(
        &self,
        url: &str,
        body: &[u8],
        headers: &[(&str, String)],
        timeout: f64,
    ) -> PostOutcome {
        let mut request = self
            .client
            .post(url)
            .map_err(|e| format!("{}: {e}", e.python_type()))?
            .body(body.to_vec())
            .timeout(std::time::Duration::from_secs_f64(timeout.max(0.0)));
        for (name, value) in headers {
            request = request.header(*name, value.as_str());
        }
        match request.send().await {
            Ok(response) => Ok(response.status().as_u16()),
            Err(e) => Err(format!("{}: {e}", httpx_class(&e))),
        }
    }
}

/// `str(x)[:n]` — by characters, as Python slices.
fn truncate(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

/// `master_secret(settings)`: the signing secret, else the JWT secret; neither is refused.
fn master_secret(settings: &Settings) -> Result<String, WorkerError> {
    let signing = settings.webhook_signing_secret.expose();
    let master = if signing.is_empty() {
        settings.jwt_secret.expose()
    } else {
        signing
    };
    if master.is_empty() {
        return Err(WorkerError::python(
            "WebhookNotConfigured",
            "BASKFY_WEBHOOK_SIGNING_SECRET (or BASKFY_JWT_SECRET) is empty; webhooks cannot be \
             signed, so no endpoint can be created.",
        ));
    }
    Ok(master.to_owned())
}

fn hmac_hex(key: &[u8], message: &[u8]) -> String {
    // A key of any length is valid for HMAC; `new_from_slice` cannot refuse one.
    let Ok(mut mac) = Hmac::<Sha256>::new_from_slice(key) else {
        return String::new();
    };
    mac.update(message);
    mac.finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// `signing_secret(settings, public_id, secret_version)`.
///
/// # Errors
/// `WebhookNotConfigured`.
pub(crate) fn signing_secret(
    settings: &Settings,
    public_id: &str,
    secret_version: i64,
) -> Result<String, WorkerError> {
    let master = master_secret(settings)?;
    let derived = hmac_hex(
        master.as_bytes(),
        format!("{public_id}:{secret_version}").as_bytes(),
    );
    Ok(format!(
        "{SECRET_DISPLAY_PREFIX}{}",
        derived.get(..SECRET_HEX_LENGTH).unwrap_or(&derived)
    ))
}

/// `signature_for(secret, body, timestamp=)`: `t=<t>,v1=<hex>` over `"<t>.<body>"`.
pub(crate) fn signature_for(secret: &str, body: &[u8], timestamp: i64) -> String {
    let mut signed = format!("{timestamp}.").into_bytes();
    signed.extend_from_slice(body);
    format!("t={timestamp},v1={}", hmac_hex(secret.as_bytes(), &signed))
}

/// `backoff_seconds(attempt, settings)`: `int(base * factor**(attempt-1))`, 0 before the first.
pub(crate) fn backoff_seconds(attempt: i64, settings: &Settings) -> Option<i64> {
    if attempt < 1 {
        return Some(0);
    }
    let exponent = u32::try_from(attempt - 1).ok()?;
    settings
        .webhook_backoff_factor
        .checked_pow(exponent)?
        .checked_mul(settings.webhook_backoff_base_seconds)
}

/// `_is_permanent(status)`: a 4xx the receiver meant.
fn is_permanent(status: u16) -> bool {
    (400..500).contains(&status) && !RETRYABLE_CLIENT_STATUSES.contains(&status)
}

/// `json.dumps(value, sort_keys=True, separators=(",", ":"))` (`ensure_ascii`).
pub(crate) fn dumps_sorted_compact(value: &OrderedJson) -> String {
    let mut out = String::new();
    write_sorted(&mut out, value);
    out
}

fn write_str(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if c.is_ascii() && (c as u32) >= 0x20 => out.push(c),
            c => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

fn write_sorted(out: &mut String, value: &OrderedJson) {
    match value {
        OrderedJson::Null => out.push_str("null"),
        OrderedJson::Bool(true) => out.push_str("true"),
        OrderedJson::Bool(false) => out.push_str("false"),
        OrderedJson::Int(i) => out.push_str(&i.to_string()),
        OrderedJson::UInt(u) => out.push_str(&u.to_string()),
        OrderedJson::BigInt(digits) => out.push_str(digits),
        OrderedJson::Float(f) => out.push_str(&float_json(*f)),
        OrderedJson::Str(text) => write_str(out, text),
        OrderedJson::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_sorted(out, item);
            }
            out.push(']');
        }
        OrderedJson::Object(entries) => {
            let mut sorted: Vec<&(String, OrderedJson)> = entries.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (i, (k, v)) in sorted.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_str(out, k);
                out.push(':');
                write_sorted(out, v);
            }
            out.push('}');
        }
    }
}

/// `float.__repr__` as `json.dumps` writes it (`NaN`/`Infinity` for the non-finite).
fn float_json(f: f64) -> String {
    if f.is_nan() {
        "NaN".to_owned()
    } else if f.is_infinite() {
        if f > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
    } else {
        baskfy_core::py::float_repr(f)
    }
}

/// One due delivery and its endpoint's id.
#[derive(Debug, Clone)]
struct Due {
    id: i64,
    endpoint_id: i64,
    event: String,
    payload: OrderedJson,
    attempts: i64,
}

/// The endpoint as the identity map holds it across the sweep.
#[derive(Debug, Clone)]
struct Endpoint {
    public_id: String,
    url: String,
    secret_version: i64,
    consecutive_failures: i64,
}

type DueRow = (i64, i64, String, String, i32, String, String, i32, i32);

/// `due_deliveries(session, limit=, now=)`, with each endpoint loaded once.
async fn due_deliveries(
    conn: &mut PgConnection,
    limit: i64,
    moment: DateTime<Utc>,
) -> Result<(Vec<Due>, HashMap<i64, Endpoint>), WorkerError> {
    let rows: Vec<DueRow> = sqlx::query_as(
        "SELECT d.id, d.endpoint_id, d.event, d.payload::text, d.attempts, \
                e.public_id, e.url, e.secret_version, e.consecutive_failures \
         FROM webhook_delivery AS d JOIN webhook_endpoint AS e ON e.id = d.endpoint_id \
         WHERE d.status = 'pending' AND d.next_attempt_at <= $1 AND e.is_active IS true \
         ORDER BY d.next_attempt_at, d.id LIMIT $2",
    )
    .bind(moment)
    .bind(limit)
    .fetch_all(&mut *conn)
    .await?;
    let mut due = Vec::with_capacity(rows.len());
    let mut endpoints = HashMap::new();
    for (id, endpoint_id, event, payload, attempts, public_id, url, version, failures) in rows {
        let payload = OrderedJson::parse(&payload)
            .map_err(|e| WorkerError::python("JSONDecodeError", format!("webhook payload: {e}")))?;
        due.push(Due {
            id,
            endpoint_id,
            event,
            payload,
            attempts: i64::from(attempts),
        });
        endpoints.entry(endpoint_id).or_insert(Endpoint {
            public_id,
            url,
            secret_version: i64::from(version),
            consecutive_failures: i64::from(failures),
        });
    }
    Ok((due, endpoints))
}

/// What one attempt did (`DeliveryOutcome`'s two flags the sweep counts).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Attempt {
    Delivered,
    Retrying,
    Failed,
}

/// `assert_public_webhook_url(url)` with `resolver`: `Ok(Err(message))` for
/// `WebhookUrlNotPublic`, `Err` for the `ValueError` Python lets through.
async fn ssrf_check(
    url: &str,
    resolver: &impl Resolver,
) -> Result<Result<(), String>, WorkerError> {
    let blocked = baskfy_providers::net::network_blocked();
    let answer = assert_public_webhook_url_with(url, |host: String, _port: u16| async move {
        // Under the harness's guard Python's `getaddrinfo` raises `OSError` before the codec
        // runs; outside it, the codec's errors come first and are not caught.
        if !blocked {
            webhook_host_encodable(&host)?;
        }
        resolver.resolve(&host).await.ok_or_else(|| {
            WebhookUrlRefusal::NotPublic(format!(
                "webhook host {} does not resolve",
                baskfy_providers::py::repr_str(&host)
            ))
        })
    })
    .await;
    match answer {
        Ok(()) => Ok(Ok(())),
        Err(WebhookUrlRefusal::NotPublic(message)) => Ok(Err(message)),
        Err(WebhookUrlRefusal::Unhandled(message)) => {
            Err(WorkerError::python("ValueError", message))
        }
    }
}

/// `attempt_delivery(session, delivery, endpoint, settings=, client=, now=)`.
#[allow(clippy::too_many_lines, clippy::too_many_arguments)]
async fn attempt_delivery(
    conn: &mut PgConnection,
    delivery: &Due,
    endpoint_id: i64,
    endpoints: &mut HashMap<i64, Endpoint>,
    settings: &Settings,
    transport: &impl Transport,
    resolver: &impl Resolver,
    moment: DateTime<Utc>,
) -> Result<Attempt, WorkerError> {
    let Some(endpoint) = endpoints.get_mut(&endpoint_id) else {
        return Err(WorkerError::Lookup(format!(
            "webhook endpoint {endpoint_id} was not loaded"
        )));
    };
    let attempts = delivery.attempts + 1;
    if let Err(message) = ssrf_check(&endpoint.url, resolver).await? {
        endpoint.consecutive_failures += 1;
        sqlx::query(
            "UPDATE webhook_delivery SET attempts = $1, status = 'failed', \
             next_attempt_at = NULL, last_error = $2 WHERE id = $3",
        )
        .bind(i32::try_from(attempts).unwrap_or(i32::MAX))
        .bind(truncate(&message, MAX_ERROR_CHARS))
        .bind(delivery.id)
        .execute(&mut *conn)
        .await?;
        sqlx::query(
            "UPDATE webhook_endpoint SET consecutive_failures = $1, updated_at = now() \
             WHERE id = $2",
        )
        .bind(i32::try_from(endpoint.consecutive_failures).unwrap_or(i32::MAX))
        .bind(endpoint_id)
        .execute(&mut *conn)
        .await?;
        return Ok(Attempt::Failed);
    }

    let body = dumps_sorted_compact(&delivery.payload).into_bytes();
    let secret = signing_secret(settings, &endpoint.public_id, endpoint.secret_version)?;
    let headers = [
        ("Content-Type", "application/json".to_owned()),
        (
            SIGNATURE_HEADER,
            signature_for(&secret, &body, moment.timestamp()),
        ),
        (EVENT_HEADER, delivery.event.clone()),
        (DELIVERY_HEADER, delivery.id.to_string()),
        ("User-Agent", "Baskfy-Webhooks/1".to_owned()),
    ];
    let (status, error) = match transport
        .post(
            &endpoint.url,
            &body,
            &headers,
            settings.webhook_timeout_seconds,
        )
        .await
    {
        Ok(status) => (
            Some(status),
            (status >= 400).then(|| format!("receiver answered {status}")),
        ),
        Err(e) => (None, Some(truncate(&e, MAX_ERROR_CHARS))),
    };
    let attempts_db = i32::try_from(attempts).unwrap_or(i32::MAX);

    if let Some(code) = status
        && code < 400
    {
        endpoint.consecutive_failures = 0;
        sqlx::query(
            "UPDATE webhook_delivery SET attempts = $1, status = 'delivered', delivered_at = $2, \
             next_attempt_at = NULL, response_status = $3, last_error = NULL WHERE id = $4",
        )
        .bind(attempts_db)
        .bind(moment)
        .bind(i32::from(code))
        .bind(delivery.id)
        .execute(&mut *conn)
        .await?;
        sqlx::query(
            "UPDATE webhook_endpoint SET consecutive_failures = 0, last_delivery_at = $1, \
             updated_at = now() WHERE id = $2",
        )
        .bind(moment)
        .bind(endpoint_id)
        .execute(&mut *conn)
        .await?;
        return Ok(Attempt::Delivered);
    }

    let last_error = truncate(error.as_deref().unwrap_or("no response"), MAX_ERROR_CHARS);
    let permanent = status.is_some_and(is_permanent);
    let exhausted = attempts >= settings.webhook_max_attempts;
    let (outcome, next_at) = if permanent || exhausted {
        (Attempt::Failed, None)
    } else {
        let wait = backoff_seconds(attempts, settings)
            .and_then(Duration::try_seconds)
            .and_then(|d| moment.checked_add_signed(d))
            .ok_or_else(|| WorkerError::python("OverflowError", "date value out of range"))?;
        (Attempt::Retrying, Some(wait))
    };
    sqlx::query(
        "UPDATE webhook_delivery SET attempts = $1, response_status = $2, last_error = $3, \
         status = CASE WHEN $4 THEN 'failed' ELSE status END, next_attempt_at = $5 WHERE id = $6",
    )
    .bind(attempts_db)
    .bind(status.map(i32::from))
    .bind(&last_error)
    .bind(outcome == Attempt::Failed)
    .bind(next_at)
    .bind(delivery.id)
    .execute(&mut *conn)
    .await?;

    endpoint.consecutive_failures += 1;
    let failures = endpoint.consecutive_failures;
    if failures >= settings.webhook_failure_threshold {
        sqlx::query(
            "UPDATE webhook_endpoint SET consecutive_failures = $1, is_active = false, \
             disabled_at = $2, disabled_reason = $3, updated_at = now() WHERE id = $4",
        )
        .bind(i32::try_from(failures).unwrap_or(i32::MAX))
        .bind(moment)
        .bind(format!("{failures} consecutive failed deliveries"))
        .bind(endpoint_id)
        .execute(&mut *conn)
        .await?;
        log_warning(&format!(
            "webhook endpoint disabled endpoint_public_id={}",
            endpoint.public_id
        ));
    } else {
        sqlx::query(
            "UPDATE webhook_endpoint SET consecutive_failures = $1, updated_at = now() \
             WHERE id = $2",
        )
        .bind(i32::try_from(failures).unwrap_or(i32::MAX))
        .bind(endpoint_id)
        .execute(&mut *conn)
        .await?;
    }
    Ok(outcome)
}

/// `run_webhook_sweep(session, now=, limit=)`: attempt every delivery whose backoff has
/// elapsed. The transport is built (`make_transport`) only when something is due, as Python
/// opens its `httpx.AsyncClient` only then. The caller commits.
///
/// # Errors
/// The database's; `WebhookNotConfigured` with no master secret; an uncaught `ValueError` from
/// the URL guard.
pub(crate) async fn run_webhook_sweep<T: Transport>(
    conn: &mut PgConnection,
    settings: &Settings,
    moment: DateTime<Utc>,
    limit: i64,
    make_transport: impl FnOnce() -> Result<T, WorkerError>,
    resolver: &impl Resolver,
) -> Result<SweepReport, WorkerError> {
    let (due, mut endpoints) = due_deliveries(conn, limit, moment).await?;
    let mut report = SweepReport::default();
    if due.is_empty() {
        return Ok(report);
    }
    let transport = make_transport()?;
    for delivery in &due {
        report.attempted += 1;
        let outcome = attempt_delivery(
            conn,
            delivery,
            delivery.endpoint_id,
            &mut endpoints,
            settings,
            &transport,
            resolver,
            moment,
        )
        .await?;
        match outcome {
            Attempt::Delivered => report.delivered += 1,
            Attempt::Retrying => report.retrying += 1,
            Attempt::Failed => report.failed += 1,
        }
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::panic,
        clippy::field_reassign_with_default,
        clippy::assertions_on_constants
    )]
    use super::*;

    #[test]
    fn the_secret_is_derived_and_stable() {
        let mut settings = Settings::default();
        settings.webhook_signing_secret = baskfy_providers::secret::Secret::new("master");
        let a = signing_secret(&settings, "abc", 1).unwrap();
        assert!(a.starts_with("whsec_"));
        assert_eq!(a, "whsec_975f7b5ce23cd8adcea8838e49b8622a");
        assert_eq!(a, signing_secret(&settings, "abc", 1).unwrap());
        assert_ne!(a, signing_secret(&settings, "abc", 2).unwrap());
    }

    #[test]
    fn no_master_secret_is_refused() {
        let settings = Settings::default();
        let e = signing_secret(&settings, "abc", 1).unwrap_err();
        assert_eq!(e.python_type(), "WebhookNotConfigured");
    }

    // HMAC-SHA256 against Python's `hmac.new(b"key", b"1700000000.{}", sha256).hexdigest()`.
    #[test]
    fn the_signature_is_stripes_scheme() {
        assert_eq!(
            signature_for("key", b"{}", 1_700_000_000),
            "t=1700000000,v1=9d713ed406bb7076d4123f0dc2c39d2df5c654ed4b0cd56b52c8b4c940bd63ae"
        );
    }

    // `backoff_seconds`: 30s, 2m, 8m, 32m, 2h8m by default.
    #[test]
    fn the_backoff_is_exponential_from_settings() {
        let settings = Settings::default();
        let waits: Vec<i64> = (0..=5)
            .map(|a| backoff_seconds(a, &settings).unwrap())
            .collect();
        assert_eq!(waits, [0, 30, 120, 480, 1920, 7680]);
    }

    #[test]
    fn only_a_meant_refusal_is_permanent() {
        assert!(is_permanent(400));
        assert!(is_permanent(401));
        assert!(!is_permanent(408));
        assert!(!is_permanent(425));
        assert!(!is_permanent(429));
        assert!(!is_permanent(500));
        assert!(!is_permanent(302));
    }

    #[test]
    fn the_body_is_sorted_compact_ascii_json() {
        let v =
            OrderedJson::parse(r#"{"b": [1, 2.5, null], "a": "é\n", "c": {"z": 1, "y": true}}"#)
                .unwrap();
        assert_eq!(
            dumps_sorted_compact(&v),
            r#"{"a":"\u00e9\n","b":[1,2.5,null],"c":{"y":true,"z":1}}"#
        );
    }

    #[test]
    fn the_sweep_batch_is_the_registrys_default() {
        // test_alert_tasks.TestTheSweepBatch.test_it_is_bounded.
        let spec = crate::tasks::find("baskfy.alerts.sweep_webhooks").unwrap();
        assert!(matches!(
            spec.params[0].default,
            crate::tasks::PyDefault::Int(SWEEP_BATCH)
        ));
        assert!(SWEEP_BATCH <= 1000);
    }

    #[test]
    fn the_report_keeps_pythons_key_order() {
        let r = SweepReport {
            attempted: 3,
            delivered: 1,
            retrying: 1,
            failed: 1,
        };
        assert_eq!(
            crate::pyjson::dumps(&r.as_dict()).unwrap(),
            r#"{"attempted": 3, "delivered": 1, "retrying": 1, "failed": 1}"#
        );
    }
}
