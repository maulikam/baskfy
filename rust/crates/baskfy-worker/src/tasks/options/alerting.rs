//! The `OPTIONS_*` alerts this lane raises, through the sinks `alerts.dispatch` serves: the log
//! line always, the email when `BASKFY_OPS_ALERT_EMAIL` is set.
//!
//! [`crate::alerts::AlertName`] is the nightly's closed set; as lanes `fno`, `twt_jobs` and
//! `vbt_jobs` do, the names are spelled here with `alerts.py`'s values, and each alert carries
//! the runbook its Python constructor gives it (`master_alert` gives none: `""`, mailed as
//! `(none)`). `raised_at` reads the **task clock**, as Python's `datetime.now` does under the
//! harness's injection. Never fails.
//!
//! **Not ported** (DECISIONS-RS R7.5): the Sentry sink, the JSON webhook, the counter.

use baskfy_api::Settings;
use baskfy_api::mailer::{Mailer, Message};
use baskfy_db::OrderedJson;
use chrono::{DateTime, Utc};

use crate::alerts::py_isoformat;
use crate::pyjson::{self, object, s};
use crate::steps::{log_error, log_warning};

/// `ops.RUNBOOKS[AlertName.OPTIONS_WEEKLY]` (and `OPTIONS_PLAN`'s).
pub(crate) const RUNBOOK_PLAN: &str = "docs/runbooks/11-options-plan.md";
/// `ops.RUNBOOKS[AlertName.OPTIONS_CHECK_FAILED]`.
pub(crate) const RUNBOOK_HEALTH: &str = "docs/runbooks/12-options-health.md";

/// `AlertName.OPTIONS_*` (the ones this lane raises).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum OptionsName {
    /// `OPTIONS_MASTER_CHANGED`.
    MasterChanged,
    /// `OPTIONS_WEEKLY`.
    Weekly,
    /// `OPTIONS_CHECK_FAILED`.
    CheckFailed,
    /// `OPTIONS_PLAN`.
    Plan,
}

impl OptionsName {
    /// The stable string.
    pub(crate) const fn value(self) -> &'static str {
        match self {
            Self::MasterChanged => "OPTIONS_MASTER_CHANGED",
            Self::Weekly => "OPTIONS_WEEKLY",
            Self::CheckFailed => "OPTIONS_CHECK_FAILED",
            Self::Plan => "OPTIONS_PLAN",
        }
    }
}

/// `Severity`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Level {
    /// `critical`.
    Critical,
    /// `warning`.
    Warning,
}

impl Level {
    const fn value(self) -> &'static str {
        match self {
            Self::Critical => "critical",
            Self::Warning => "warning",
        }
    }
}

/// `Alert`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct OptionsAlert {
    pub(crate) name: OptionsName,
    pub(crate) severity: Level,
    pub(crate) summary: String,
    /// In insertion order.
    pub(crate) labels: Vec<(String, String)>,
    pub(crate) detail: OrderedJson,
    pub(crate) runbook: &'static str,
}

impl OptionsAlert {
    /// `Alert.as_dict()`.
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
            ("runbook", s(self.runbook)),
            ("raised_at", s(py_isoformat(raised_at))),
        ])
    }
}

/// `html.escape(text)` (`quote=True`).
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

/// `dispatch(alert)`: the payload plus `delivered_to`. Never fails.
pub(crate) async fn dispatch(
    alert: &OptionsAlert,
    settings: &Settings,
    raised_at: DateTime<Utc>,
) -> OrderedJson {
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

/// `_send_email`: the whole payload verbatim (`json.dumps(payload, indent=2)`).
async fn send_email(alert: &OptionsAlert, payload: &OrderedJson, settings: &Settings) -> bool {
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
        alert.runbook
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

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]
    use super::*;

    #[test]
    fn the_payload_keeps_alerts_py_key_order() {
        let alert = OptionsAlert {
            name: OptionsName::Weekly,
            severity: Level::Warning,
            summary: "x".to_owned(),
            labels: vec![],
            detail: object::<&str>([]),
            runbook: RUNBOOK_PLAN,
        };
        let OrderedJson::Object(entries) = alert.as_dict(DateTime::<Utc>::default()) else {
            panic!("an object");
        };
        let keys: Vec<&str> = entries.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(
            keys,
            [
                "alert",
                "severity",
                "summary",
                "labels",
                "detail",
                "runbook",
                "raised_at"
            ]
        );
    }
}
