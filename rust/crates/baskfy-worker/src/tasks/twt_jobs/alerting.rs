//! The two alerts this lane raises — `TWT_EVENING` (the evening digest, a warning) and
//! `TWT_ADJUSTMENT_RESET` (a position re-adjusted under the hold, critical) — through the same
//! sinks [`crate::alerts::dispatch`] serves: the log line always, the email when
//! `BASKFY_OPS_ALERT_EMAIL` is set.
//!
//! [`crate::alerts::AlertName`] is the nightly's closed set; this lane does not widen it (another
//! lane's file), so the two names are spelled here with `alerts.py`'s values and `ops.RUNBOOKS`'
//! runbook, and the payload and mail are built exactly as `alerts.dispatch`/`_send_email` build
//! them (`json.dumps(payload, indent=2, default=str)`, `html.escape`). Never fails.

use baskfy_api::Settings;
use baskfy_api::mailer::{Mailer, Message};
use baskfy_db::OrderedJson;
use chrono::{DateTime, Utc};

use crate::alerts::py_isoformat;
use crate::pyjson::{self, object, s};
use crate::steps::{log_error, log_warning};

/// `ops.RUNBOOKS[AlertName.TWT_*]`: one runbook for all four `TWT_*` names.
pub(crate) const RUNBOOK: &str = "docs/runbooks/09-twt-morning.md";

/// One alert, by its `AlertName` value.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct TwtAlert {
    /// `AlertName.TWT_EVENING` / `AlertName.TWT_ADJUSTMENT_RESET`.
    pub(crate) name: &'static str,
    /// `critical` / `warning`.
    pub(crate) severity: &'static str,
    pub(crate) summary: String,
    pub(crate) labels: Vec<(String, String)>,
    pub(crate) detail: OrderedJson,
}

impl TwtAlert {
    /// `Alert.as_dict()`.
    fn as_dict(&self, raised_at: DateTime<Utc>) -> OrderedJson {
        object([
            ("alert", s(self.name)),
            ("severity", s(self.severity)),
            ("summary", s(self.summary.clone())),
            (
                "labels",
                object(self.labels.iter().map(|(k, v)| (k.clone(), s(v.clone())))),
            ),
            ("detail", self.detail.clone()),
            ("runbook", s(RUNBOOK)),
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

/// `dispatch(alert, settings)`: the payload plus `delivered_to`. Never fails.
pub(crate) async fn dispatch(
    alert: &TwtAlert,
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
        "alert alert={} severity={} summary={:?} runbook={RUNBOOK} {}",
        alert.name,
        alert.severity,
        alert.summary,
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

/// `_send_email`: the whole payload verbatim.
async fn send_email(alert: &TwtAlert, payload: &OrderedJson, settings: &Settings) -> bool {
    let body = match pyjson::dumps_indent2(payload) {
        Ok(body) => body,
        Err(e) => {
            log_error(&format!(
                "alert email failed alert={} error={e}",
                alert.name
            ));
            return false;
        }
    };
    let message = Message {
        to: settings.ops_alert_email.clone(),
        subject: format!(
            "[decile/{}] {}: {}",
            settings.environment, alert.severity, alert.summary
        ),
        text: format!("{}\n\nRunbook: {RUNBOOK}\n\n{body}\n", alert.summary),
        html: format!(
            "<p><strong>{}</strong></p><p>Runbook: {}</p><pre>{}</pre>",
            html_escape(&alert.summary),
            html_escape(RUNBOOK),
            html_escape(&body)
        ),
        reply_to: None,
    };
    Mailer::from_settings(settings).deliver(&message).await
}

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]
    use super::*;

    #[test]
    fn html_escape_is_pythons() {
        assert_eq!(
            html_escape("a<b> & \"c\" 'd'"),
            "a&lt;b&gt; &amp; &quot;c&quot; &#x27;d&#x27;"
        );
    }

    #[test]
    fn the_payload_keeps_alerts_py_key_order() {
        let alert = TwtAlert {
            name: "TWT_EVENING",
            severity: "warning",
            summary: "x".to_owned(),
            labels: vec![("trade_date".to_owned(), "2026-10-29".to_owned())],
            detail: object([("a", pyjson::int(1))]),
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
