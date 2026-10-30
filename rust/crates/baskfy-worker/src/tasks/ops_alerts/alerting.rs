//! The alerts this lane raises, and `baskfy_worker.alerts.dispatch` in full for them: the log
//! line always, then — unless the delivery is **suppressed** — email when
//! `BASKFY_OPS_ALERT_EMAIL` is set and the JSON webhook when `BASKFY_OPS_ALERT_WEBHOOK_URL` is
//! (Alertmanager-shaped, `webhook_payload`), with "no sink configured" said once per process.
//!
//! [`crate::alerts::AlertName`] is the nightly's closed set and another lane's file; this lane
//! spells its four names (`pipeline_abandoned`, `publish_late`, `kite_token_expiring`,
//! `queue_backlog`) with `alerts.py`'s values and `ops.RUNBOOKS`' runbooks, as lanes `twt_jobs`
//! and `vbt_jobs` do for theirs. `raised_at` (and the webhook's `startsAt`) read the **task
//! clock**, as Python's `datetime.now` does under the harness's injection.
//!
//! **Not ported** (DECISIONS-RS R7.5, restated in R8.oa2): the Sentry sink (no DSN on the local
//! box; when one is configured the alert is logged as not sent there), the Prometheus counter and
//! the span annotation. A sink never propagates: [`dispatch`] never fails.

use std::sync::atomic::{AtomicBool, Ordering};

use baskfy_api::Settings;
use baskfy_api::mailer::{Mailer, Message};
use baskfy_db::OrderedJson;
use chrono::{DateTime, Utc};

use crate::alerts::py_isoformat;
use crate::pyjson::{self, object, s};
use crate::steps::{log_error, log_warning};

/// `WEBHOOK_TIMEOUT_SECONDS`: how long the ops webhook is given before it is abandoned.
pub(crate) const WEBHOOK_TIMEOUT_SECONDS: f64 = 5.0;

/// `_warned_no_sink`: "no delivery sink" is said once per process.
static WARNED_NO_SINK: AtomicBool = AtomicBool::new(false);

/// `Severity`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Severity {
    /// `critical`.
    Critical,
    /// `warning`.
    Warning,
}

impl Severity {
    pub(crate) const fn value(self) -> &'static str {
        match self {
            Self::Critical => "critical",
            Self::Warning => "warning",
        }
    }
}

/// The four `AlertName`s this lane raises, with `ops.RUNBOOKS`' entry for each.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Name {
    /// `PIPELINE_ABANDONED`.
    PipelineAbandoned,
    /// `PUBLISH_LATE`.
    PublishLate,
    /// `KITE_TOKEN_EXPIRING`.
    KiteTokenExpiring,
    /// `QUEUE_BACKLOG`.
    QueueBacklog,
}

impl Name {
    /// The stable string (`AlertName.<X>.value`).
    pub(crate) const fn value(self) -> &'static str {
        match self {
            Self::PipelineAbandoned => "pipeline_abandoned",
            Self::PublishLate => "publish_late",
            Self::KiteTokenExpiring => "kite_token_expiring",
            Self::QueueBacklog => "queue_backlog",
        }
    }

    /// `ops.RUNBOOKS[name]`.
    pub(crate) const fn runbook(self) -> &'static str {
        match self {
            Self::KiteTokenExpiring => "docs/runbooks/kite-token-expired.md",
            Self::PipelineAbandoned | Self::PublishLate | Self::QueueBacklog => {
                "docs/runbooks/pipeline-failed.md"
            }
        }
    }
}

/// `Alert`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct OpsAlert {
    pub(crate) name: Name,
    pub(crate) severity: Severity,
    pub(crate) summary: String,
    /// `labels`, in insertion order.
    pub(crate) labels: Vec<(String, String)>,
    /// `detail` (an object).
    pub(crate) detail: OrderedJson,
}

impl OpsAlert {
    /// `Alert.as_dict()`, `raised_at` being `datetime.now(tz=UTC).isoformat()`.
    pub(crate) fn as_dict(&self, raised_at: DateTime<Utc>) -> OrderedJson {
        object([
            ("alert", s(self.name.value())),
            ("severity", s(self.severity.value())),
            ("summary", s(self.summary.clone())),
            (
                "labels",
                object(self.labels.iter().map(|(k, v)| (k.clone(), s(v.clone())))),
            ),
            ("detail", self.detail.clone()),
            ("runbook", s(self.name.runbook())),
            ("raised_at", s(py_isoformat(raised_at))),
        ])
    }

    /// A key of `detail`, if present.
    pub(crate) fn detail_get(&self, key: &str) -> Option<&OrderedJson> {
        match &self.detail {
            OrderedJson::Object(entries) => entries.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
}

/// `webhook_payload(alert)`: the Alertmanager-v2-shaped body posted to the ops webhook.
pub(crate) fn webhook_payload(alert: &OpsAlert, starts_at: DateTime<Utc>) -> OrderedJson {
    let mut labels = vec![
        ("alertname".to_owned(), s(alert.name.value())),
        ("severity".to_owned(), s(alert.severity.value())),
        ("service".to_owned(), s("decile")),
    ];
    for (k, v) in &alert.labels {
        pyjson::update(&mut labels, k, s(v.clone()));
    }
    object([
        ("status", s("firing")),
        ("labels", OrderedJson::Object(labels)),
        (
            "annotations",
            object([
                ("summary", s(alert.summary.clone())),
                ("runbook_url", s(alert.name.runbook())),
            ]),
        ),
        ("startsAt", s(py_isoformat(starts_at))),
        ("detail", alert.detail.clone()),
    ])
}

/// `html.escape(text)` (`quote=True`).
pub(crate) fn html_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

/// Where the sinks deliver: the mail transport, and the ops webhook's poster (injected by the
/// tests; production posts through the guarded client).
pub(crate) struct Sinks<'a> {
    pub(crate) settings: &'a Settings,
    /// `None`: the API's mailer built from `settings` (`Mailer(build_transport(settings))`).
    pub(crate) mailer: Option<&'a dyn MailSink>,
    /// `None`: [`post_webhook`].
    pub(crate) webhook: Option<&'a dyn WebhookSink>,
}

/// A sink's answer: `true` when it took the alert.
pub(crate) type BoolFuture<'a> = std::pin::Pin<Box<dyn std::future::Future<Output = bool> + 'a>>;

/// A mail transport (`Mailer.deliver`): `true` when the message was handed over.
pub(crate) trait MailSink {
    /// Deliver one message.
    fn deliver<'a>(&'a self, message: &'a Message) -> BoolFuture<'a>;
}

/// The ops webhook poster (`_post_webhook`): `true` on a 2xx.
pub(crate) trait WebhookSink {
    /// POST `body` to `url`.
    fn post<'a>(&'a self, url: &'a str, body: &'a str) -> BoolFuture<'a>;
}

/// The API's mailer over `settings` (`Mailer(build_transport(settings))`).
pub(crate) struct ApiMailer<'a>(pub(crate) &'a Settings);

impl MailSink for ApiMailer<'_> {
    fn deliver<'a>(&'a self, message: &'a Message) -> BoolFuture<'a> {
        Box::pin(async move { Mailer::from_settings(self.0).deliver(message).await })
    }
}

async fn deliver_mail(sinks: &Sinks<'_>, message: &Message) -> bool {
    match sinks.mailer {
        Some(mailer) => mailer.deliver(message).await,
        None => Mailer::from_settings(sinks.settings).deliver(message).await,
    }
}

/// `dispatch(alert, settings, mailer=, suppressed=)`: raise `alert` to every configured sink.
/// Never fails; returns `as_dict()` plus `delivered_to` (and `suppressed`, when given).
pub(crate) async fn dispatch(
    alert: &OpsAlert,
    sinks: &Sinks<'_>,
    now: DateTime<Utc>,
    suppressed: Option<&str>,
) -> OrderedJson {
    let settings = sinks.settings;
    let payload = alert.as_dict(now);
    let labels: Vec<String> = alert
        .labels
        .iter()
        .map(|(k, v)| format!("alert_{k}={v}"))
        .collect();
    let suppressed_field = suppressed.map_or_else(String::new, |r| format!(" suppressed={r:?}"));
    log_error(&format!(
        "alert alert={} severity={} summary={:?} runbook={}{suppressed_field} {}",
        alert.name.value(),
        alert.severity.value(),
        alert.summary,
        alert.name.runbook(),
        labels.join(" ")
    ));
    let mut delivered: Vec<&str> = vec!["log"];
    let OrderedJson::Object(mut entries) = payload else {
        return payload;
    };
    if let Some(reason) = suppressed {
        pyjson::update(&mut entries, "delivered_to", pyjson::strings(delivered));
        pyjson::update(&mut entries, "suppressed", s(reason));
        return OrderedJson::Object(entries);
    }
    if settings.sentry_dsn.is_set() {
        log_warning(&format!(
            "the Sentry alert sink is not ported to Rust (DECISIONS-RS R7.5); alert {} was not \
             sent to Sentry",
            alert.name.value()
        ));
    }
    if !settings.ops_alert_email.is_empty() && send_email(alert, sinks, now).await {
        delivered.push("email");
    }
    if !settings.ops_alert_webhook_url.is_empty() {
        let body = pyjson::dumps(&webhook_payload(alert, now)).unwrap_or_default();
        let posted = match sinks.webhook {
            Some(sink) => sink.post(&settings.ops_alert_webhook_url, &body).await,
            None => post_webhook(alert, &settings.ops_alert_webhook_url, body).await,
        };
        if posted {
            delivered.push("webhook");
        }
    }
    if delivered == ["log"] && !WARNED_NO_SINK.swap(true, Ordering::SeqCst) {
        log_warning(
            "alerts have no delivery sink configured; set BASKFY_SENTRY_DSN, \
             BASKFY_OPS_ALERT_EMAIL or BASKFY_OPS_ALERT_WEBHOOK_URL",
        );
    }
    pyjson::update(&mut entries, "delivered_to", pyjson::strings(delivered));
    OrderedJson::Object(entries)
}

/// `_send_email`: the whole payload verbatim (`json.dumps(as_dict(), indent=2)`).
async fn send_email(alert: &OpsAlert, sinks: &Sinks<'_>, now: DateTime<Utc>) -> bool {
    let settings = sinks.settings;
    let body = match pyjson::dumps_indent2(&alert.as_dict(now)) {
        Ok(body) => body,
        Err(e) => {
            log_error(&format!(
                "alert email failed alert={} error={e}",
                alert.name.value()
            ));
            return false;
        }
    };
    let runbook = alert.name.runbook();
    let message = Message {
        to: settings.ops_alert_email.clone(),
        subject: format!(
            "[decile/{}] {}: {}",
            settings.environment,
            alert.severity.value(),
            alert.summary
        ),
        text: format!("{}\n\nRunbook: {runbook}\n\n{body}\n", alert.summary),
        html: format!(
            "<p><strong>{}</strong></p><p>Runbook: {}</p><pre>{}</pre>",
            html_escape(&alert.summary),
            html_escape(runbook),
            html_escape(&body)
        ),
        reply_to: None,
    };
    deliver_mail(sinks, &message).await
}

/// `_post_webhook`: POST the alert as JSON, five seconds, a 2xx is delivery. Through the guarded
/// client, so the harness's network block reaches it.
async fn post_webhook(alert: &OpsAlert, url: &str, body: String) -> bool {
    let fail = |error: String| {
        log_error(&format!(
            "alert webhook failed alert={} error={error}",
            alert.name.value()
        ));
        false
    };
    let client = match baskfy_providers::net::GuardedClient::from_builder(
        baskfy_providers::net::client_builder()
            .timeout(std::time::Duration::from_secs_f64(WEBHOOK_TIMEOUT_SECONDS)),
    ) {
        Ok(c) => c,
        Err(e) => return fail(e.to_string()),
    };
    let request = match client.post(url) {
        Ok(r) => r,
        Err(e) => return fail(e.to_string()),
    };
    match request
        .header("Content-Type", "application/json")
        .body(body)
        .send()
        .await
    {
        Ok(response) if response.status().is_success() => true,
        Ok(response) => fail(format!("HTTP Error {}", response.status().as_u16())),
        Err(e) => fail(e.to_string()),
    }
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

    use super::*;

    struct Inbox(RefCell<Vec<Message>>, bool);
    impl MailSink for Inbox {
        fn deliver<'a>(&'a self, message: &'a Message) -> BoolFuture<'a> {
            Box::pin(async move {
                self.0.borrow_mut().push(message.clone());
                self.1
            })
        }
    }
    struct Hook(RefCell<Vec<(String, String)>>, bool);
    impl WebhookSink for Hook {
        fn post<'a>(&'a self, url: &'a str, body: &'a str) -> BoolFuture<'a> {
            Box::pin(async move {
                self.0.borrow_mut().push((url.to_owned(), body.to_owned()));
                self.1
            })
        }
    }

    fn alert() -> OpsAlert {
        OpsAlert {
            name: Name::PipelineAbandoned,
            severity: Severity::Critical,
            summary: "Pipeline run 7 for 2026-10-29 was abandoned".to_owned(),
            labels: vec![("trade_date".to_owned(), "2026-10-29".to_owned())],
            detail: object([("age_minutes", OrderedJson::Int(91))]),
        }
    }

    fn at() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-10-29T15:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn keys(v: &OrderedJson) -> Vec<String> {
        match v {
            OrderedJson::Object(e) => e.iter().map(|(k, _)| k.clone()).collect(),
            _ => vec![],
        }
    }

    fn get<'a>(v: &'a OrderedJson, key: &str) -> &'a OrderedJson {
        match v {
            OrderedJson::Object(e) => &e.iter().find(|(k, _)| k == key).unwrap().1,
            _ => &OrderedJson::Null,
        }
    }

    // test_ops_and_alerts.TestDispatch.test_it_always_logs_and_always_counts (the log and the
    // payload half; the counter is not ported).
    #[tokio::test]
    async fn with_no_sink_it_still_logs_and_returns_the_payload() {
        let settings = Settings::default();
        let sinks = Sinks {
            settings: &settings,
            mailer: None,
            webhook: None,
        };
        let payload = dispatch(&alert(), &sinks, at(), None).await;
        assert_eq!(
            keys(&payload),
            [
                "alert",
                "severity",
                "summary",
                "labels",
                "detail",
                "runbook",
                "raised_at",
                "delivered_to"
            ]
        );
        assert_eq!(get(&payload, "delivered_to"), &pyjson::strings(["log"]));
        assert_eq!(get(&payload, "raised_at"), &s("2026-10-29T15:00:00+00:00"));
    }

    // TestDispatch.test_a_failing_sink_never_propagates.
    #[tokio::test]
    async fn a_failing_sink_never_propagates() {
        let mut settings = Settings::default();
        "ops@example.com".clone_into(&mut settings.ops_alert_email);
        "https://hooks.example.com/x".clone_into(&mut settings.ops_alert_webhook_url);
        let inbox = Inbox(RefCell::new(vec![]), false);
        let hook = Hook(RefCell::new(vec![]), false);
        let sinks = Sinks {
            settings: &settings,
            mailer: Some(&inbox),
            webhook: Some(&hook),
        };
        let payload = dispatch(&alert(), &sinks, at(), None).await;
        assert_eq!(get(&payload, "delivered_to"), &pyjson::strings(["log"]));
        assert_eq!(inbox.0.borrow().len(), 1);
        assert_eq!(hook.0.borrow().len(), 1);
    }

    // TestDispatch.test_the_email_sink_reports_success.
    #[tokio::test]
    async fn the_email_sink_reports_success_and_carries_the_payload() {
        let mut settings = Settings::default();
        "ops@example.com".clone_into(&mut settings.ops_alert_email);
        let inbox = Inbox(RefCell::new(vec![]), true);
        let sinks = Sinks {
            settings: &settings,
            mailer: Some(&inbox),
            webhook: None,
        };
        let payload = dispatch(&alert(), &sinks, at(), None).await;
        assert_eq!(
            get(&payload, "delivered_to"),
            &pyjson::strings(["log", "email"])
        );
        let sent = inbox.0.borrow();
        assert_eq!(sent[0].to, "ops@example.com");
        assert!(sent[0].subject.starts_with("[decile/local] critical: "));
        assert!(
            sent[0]
                .text
                .contains("Runbook: docs/runbooks/pipeline-failed.md")
        );
        assert!(sent[0].text.contains("\"age_minutes\": 91"));
    }

    // TestDispatch.test_the_webhook_body_is_alertmanager_shaped.
    #[test]
    fn the_webhook_body_is_alertmanager_shaped() {
        let body = webhook_payload(&alert(), at());
        assert_eq!(
            keys(&body),
            ["status", "labels", "annotations", "startsAt", "detail"]
        );
        assert_eq!(
            keys(get(&body, "labels")),
            ["alertname", "severity", "service", "trade_date"]
        );
        assert_eq!(
            get(get(&body, "annotations"), "runbook_url"),
            &s("docs/runbooks/pipeline-failed.md")
        );
    }

    // ops.run_kite_token_check's suppressed delivery: logged and returned, never sent.
    #[tokio::test]
    async fn a_suppressed_alert_is_recorded_and_not_sent() {
        let mut settings = Settings::default();
        "ops@example.com".clone_into(&mut settings.ops_alert_email);
        let inbox = Inbox(RefCell::new(vec![]), true);
        let sinks = Sinks {
            settings: &settings,
            mailer: Some(&inbox),
            webhook: None,
        };
        let payload = dispatch(&alert(), &sinks, at(), Some("already delivered")).await;
        assert!(inbox.0.borrow().is_empty());
        assert_eq!(get(&payload, "suppressed"), &s("already delivered"));
        assert_eq!(get(&payload, "delivered_to"), &pyjson::strings(["log"]));
    }

    #[test]
    fn every_name_carries_alerts_py_value_and_its_runbook() {
        assert_eq!(Name::PublishLate.value(), "publish_late");
        assert_eq!(Name::KiteTokenExpiring.value(), "kite_token_expiring");
        assert_eq!(Name::QueueBacklog.value(), "queue_backlog");
        assert_eq!(
            Name::KiteTokenExpiring.runbook(),
            "docs/runbooks/kite-token-expired.md"
        );
        for name in [
            Name::PipelineAbandoned,
            Name::PublishLate,
            Name::KiteTokenExpiring,
            Name::QueueBacklog,
        ] {
            // TestEveryAlertNamesARunbookThatExists.test_the_file_is_there.
            let root =
                std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../decile-blueprint");
            assert!(root.join(name.runbook()).is_file(), "{}", name.runbook());
        }
    }
}
