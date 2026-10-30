//! Alerts. Ports the parts of `baskfy_worker.alerts` and `ops.alert_for_outcome` the nightly
//! task raises: [`Alert`], [`alert_for_outcome`], and [`dispatch`] to the log and to email.
//!
//! **Not ported** (DECISIONS-RS R7.5): the Sentry sink (no DSN is configured on the local box),
//! the JSON webhook (logged as not ported when `BASKFY_OPS_ALERT_WEBHOOK_URL` is set), the
//! Prometheus counter and the span annotation. A sink never propagates: `dispatch` never fails.

use baskfy_api::Settings;
use baskfy_api::mailer::{Mailer, Message};
use baskfy_db::OrderedJson;
use chrono::{DateTime, Utc};

use crate::orchestrator::PipelineOutcome;
use crate::pyjson::{self, object, s};
use crate::steps::{RunStatus, log_error, log_warning};

/// `Severity`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    /// Wake someone.
    Critical,
    /// Look at it in the morning.
    Warning,
}

impl Severity {
    /// The stable string.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Critical => "critical",
            Self::Warning => "warning",
        }
    }
}

/// The alert names the nightly raises (`AlertName`), by their stable strings.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AlertName {
    /// `pipeline_failed`.
    PipelineFailed,
    /// `pipeline_abandoned`.
    PipelineAbandoned,
    /// `data_quality_gate_failed`.
    GateFailed,
    /// `SWING_POSITION_NAKED` (SW11; added by wave-6 lane swing_live).
    SwingPositionNaked,
    /// `SWING_MONITOR_DID_NOT_START`.
    SwingMonitorDidNotStart,
    /// `SWING_DETECT_STALE`.
    SwingDetectStale,
    /// `SWING_ORDER_OPEN_AFTER_CUTOFF`.
    SwingOrderOpenAfterCutoff,
    /// `SWING_GTT_MISSING_AT_1515`.
    SwingGttMissingAt1515,
}

impl AlertName {
    /// The stable string (the `alertname` label, the `alert` log field).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::PipelineFailed => "pipeline_failed",
            Self::PipelineAbandoned => "pipeline_abandoned",
            Self::GateFailed => "data_quality_gate_failed",
            Self::SwingPositionNaked => "SWING_POSITION_NAKED",
            Self::SwingMonitorDidNotStart => "SWING_MONITOR_DID_NOT_START",
            Self::SwingDetectStale => "SWING_DETECT_STALE",
            Self::SwingOrderOpenAfterCutoff => "SWING_ORDER_OPEN_AFTER_CUTOFF",
            Self::SwingGttMissingAt1515 => "SWING_GTT_MISSING_AT_1515",
        }
    }

    /// `ops.RUNBOOKS[name]`.
    #[must_use]
    pub fn runbook(self) -> &'static str {
        match self {
            Self::PipelineFailed | Self::PipelineAbandoned => "docs/runbooks/pipeline-failed.md",
            Self::GateFailed => "docs/runbooks/bad-data-published.md",
            Self::SwingPositionNaked
            | Self::SwingMonitorDidNotStart
            | Self::SwingDetectStale
            | Self::SwingOrderOpenAfterCutoff
            | Self::SwingGttMissingAt1515 => "docs/runbooks/06-swing-morning.md",
        }
    }
}

/// `Alert`.
#[derive(Debug, Clone, PartialEq)]
pub struct Alert {
    /// `name`.
    pub name: AlertName,
    /// `severity`.
    pub severity: Severity,
    /// `summary`.
    pub summary: String,
    /// `labels`, in insertion order.
    pub labels: Vec<(String, String)>,
    /// `detail`.
    pub detail: OrderedJson,
    /// `runbook`.
    pub runbook: String,
}

impl Alert {
    /// `as_dict()`, stamped `raised_at` (`datetime.now(tz=UTC).isoformat()`).
    #[must_use]
    pub fn as_dict(&self, raised_at: DateTime<Utc>) -> OrderedJson {
        object([
            ("alert", s(self.name.value())),
            ("severity", s(self.severity.value())),
            ("summary", s(self.summary.clone())),
            (
                "labels",
                object(self.labels.iter().map(|(k, v)| (k.clone(), s(v.clone())))),
            ),
            ("detail", self.detail.clone()),
            ("runbook", s(self.runbook.clone())),
            ("raised_at", s(py_isoformat(raised_at))),
        ])
    }
}

/// `datetime.isoformat()` for an aware UTC datetime: microseconds only when non-zero.
#[must_use]
pub fn py_isoformat(at: DateTime<Utc>) -> String {
    let micros = at.timestamp_subsec_micros();
    if micros == 0 {
        at.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        format!("{}.{micros:06}+00:00", at.format("%Y-%m-%dT%H:%M:%S"))
    }
}

/// `ops.alert_for_outcome`: the alert a finished outcome deserves, or `None` (a success, or an
/// aborted run — a non-trading day is not an alert).
#[must_use]
pub fn alert_for_outcome(outcome: &PipelineOutcome) -> Option<Alert> {
    if matches!(outcome.status, RunStatus::Succeeded | RunStatus::Aborted) {
        return None;
    }
    let day = outcome.trade_date.format("%Y-%m-%d").to_string();
    let completed = pyjson::strings(outcome.steps_completed.iter().map(|s| s.value()));
    if let Some(gate) = &outcome.gate
        && !gate.passed()
    {
        let failures = OrderedJson::Array(
            gate.failures()
                .into_iter()
                .map(|f| {
                    object([
                        ("assertion", OrderedJson::Int(f.assertion)),
                        ("message", s(f.message.clone())),
                    ])
                })
                .collect(),
        );
        return Some(Alert {
            name: AlertName::GateFailed,
            severity: Severity::Critical,
            summary: format!(
                "The data-quality gate failed for {day}; data_version was not bumped."
            ),
            labels: vec![
                ("trade_date".to_owned(), day),
                ("step".to_owned(), "data_quality_gate".to_owned()),
            ],
            detail: object([("failures", failures), ("steps_completed", completed)]),
            runbook: AlertName::GateFailed.runbook().to_owned(),
        });
    }
    Some(Alert {
        name: AlertName::PipelineFailed,
        severity: Severity::Critical,
        summary: format!("The nightly pipeline failed for {day}."),
        labels: vec![
            ("trade_date".to_owned(), day),
            (
                "step".to_owned(),
                outcome
                    .failed_step
                    .map_or_else(|| "unknown".to_owned(), |s| s.value().to_owned()),
            ),
        ],
        detail: object([
            ("error", pyjson::opt_s(outcome.error.clone())),
            ("steps_completed", completed),
        ]),
        runbook: AlertName::PipelineFailed.runbook().to_owned(),
    })
}

/// The alert the nightly task raises when the chain itself raised (`celery_tasks.nightly_pipeline`'s
/// `except Exception` branch).
#[must_use]
pub fn chain_raised(day: chrono::NaiveDate, run_id: i64, kind: &str, message: &str) -> Alert {
    let day = day.format("%Y-%m-%d").to_string();
    Alert {
        name: AlertName::PipelineFailed,
        severity: Severity::Critical,
        summary: format!("The nightly pipeline raised {kind} for {day}."),
        labels: vec![
            ("trade_date".to_owned(), day),
            ("run_id".to_owned(), run_id.to_string()),
        ],
        detail: object([("error", s(message)), ("error_type", s(kind))]),
        runbook: AlertName::PipelineFailed.runbook().to_owned(),
    }
}

/// `html.escape(text)` (quote=True).
fn html_escape(text: &str) -> String {
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

/// `dispatch(alert)`: the log line always; email when `BASKFY_OPS_ALERT_EMAIL` is set. Never
/// fails. Returns the payload Python returns (`as_dict()` plus `delivered_to`).
pub async fn dispatch(alert: &Alert, settings: &Settings) -> OrderedJson {
    use baskfy_providers::clock::Clock as _;
    let raised_at = baskfy_providers::clock::system_clock().now();
    let payload = alert.as_dict(raised_at);
    let labels: Vec<String> = alert
        .labels
        .iter()
        .map(|(k, v)| format!("alert_{k}={v}"))
        .collect();
    log_error(&format!(
        "alert alert={} severity={} summary={:?} runbook={} {}",
        alert.name.value(),
        alert.severity.value(),
        alert.summary,
        alert.runbook,
        labels.join(" ")
    ));
    let mut delivered = vec!["log"];
    if !settings.ops_alert_email.is_empty() && send_email(alert, &payload, settings).await {
        delivered.push("email");
    }
    if !settings.ops_alert_webhook_url.is_empty() {
        log_warning("the alert webhook sink is not ported to Rust (DECISIONS-RS R7.5)");
    }
    if delivered == ["log"] {
        log_warning(
            "alerts have no delivery sink configured; set BASKFY_SENTRY_DSN, \
             BASKFY_OPS_ALERT_EMAIL or BASKFY_OPS_ALERT_WEBHOOK_URL",
        );
    }
    let OrderedJson::Object(mut entries) = payload else {
        return payload;
    };
    pyjson::update(&mut entries, "delivered_to", pyjson::strings(delivered));
    OrderedJson::Object(entries)
}

/// `_send_email`: the whole payload verbatim, through the API's mailer.
async fn send_email(alert: &Alert, payload: &OrderedJson, settings: &Settings) -> bool {
    let body = match pyjson::dumps_indent2(payload) {
        Ok(body) => body,
        Err(e) => {
            log_error(&format!(
                "alert email failed alert={} error={e}",
                alert.name.value()
            ));
            return false;
        }
    };
    let runbook = if alert.runbook.is_empty() {
        "(none)"
    } else {
        alert.runbook.as_str()
    };
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
    Mailer::from_settings(settings).deliver(&message).await
}
