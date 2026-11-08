//! The six `FNO_*` alerts this lane raises, through the sinks `alerts.dispatch` serves: the log
//! line always, the email when `BASKFY_OPS_ALERT_EMAIL` is set.
//!
//! [`crate::alerts::AlertName`] is the nightly's closed set and another lane's file; as lanes
//! `twt_jobs`, `vbt_jobs` and `ops_alerts` do, the names are spelled here with `alerts.py`'s
//! values and `ops.RUNBOOKS`' runbook (one for all six), and `raised_at` reads the **task
//! clock**, as Python's `datetime.now` does under the harness's injection. Never fails.
//!
//! **Not ported** (DECISIONS-RS R7.5): the Sentry sink, the JSON webhook, the counter.

use baskfy_api::Settings;
use baskfy_api::mailer::{Mailer, Message};
use baskfy_db::OrderedJson;
use chrono::{DateTime, Utc};

use crate::alerts::py_isoformat;
use crate::pyjson::{self, object, s};
use crate::steps::{log_error, log_warning};

/// `ops.RUNBOOKS[AlertName.FNO_*]`: one runbook for all six.
pub(crate) const RUNBOOK: &str = "docs/runbooks/13-fno-book.md";

/// `AlertName.FNO_*`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FnoName {
    /// `FNO_WEEKLY`.
    Weekly,
    /// `FNO_PLAN`.
    Plan,
    /// `FNO_EXIT`.
    Exit,
    /// `FNO_HARD_EXIT_TOMORROW`.
    HardExitTomorrow,
    /// `FNO_LATE_EXIT`.
    LateExit,
    /// `FNO_BHAVCOPY_MISSING`.
    BhavcopyMissing,
}

impl FnoName {
    /// The stable string.
    pub(crate) const fn value(self) -> &'static str {
        match self {
            Self::Weekly => "FNO_WEEKLY",
            Self::Plan => "FNO_PLAN",
            Self::Exit => "FNO_EXIT",
            Self::HardExitTomorrow => "FNO_HARD_EXIT_TOMORROW",
            Self::LateExit => "FNO_LATE_EXIT",
            Self::BhavcopyMissing => "FNO_BHAVCOPY_MISSING",
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
pub(crate) struct FnoAlert {
    pub(crate) name: FnoName,
    pub(crate) severity: Level,
    pub(crate) summary: String,
    /// In insertion order.
    pub(crate) labels: Vec<(String, String)>,
    pub(crate) detail: OrderedJson,
}

impl FnoAlert {
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

/// `dispatch(alert)`: the payload plus `delivered_to`. Never fails.
pub(crate) async fn dispatch(
    alert: &FnoAlert,
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
        alert.name.value(),
        alert.severity.value(),
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

/// `_send_email`: the whole payload verbatim (`json.dumps(payload, indent=2)`).
async fn send_email(alert: &FnoAlert, payload: &OrderedJson, settings: &Settings) -> bool {
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
    let message = Message {
        to: settings.ops_alert_email.clone(),
        subject: format!(
            "[decile/{}] {}: {}",
            settings.environment,
            alert.severity.value(),
            alert.summary
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

/// `alerts.bhavcopy_missing_alert(result)`: `FNO_BHAVCOPY_MISSING` from a `run_night` result,
/// or `None` when the day is not `MISSING`.
pub(crate) fn bhavcopy_missing_alert(result: &OrderedJson) -> Option<FnoAlert> {
    let ingest = result.get("ingest")?;
    if !matches!(ingest, OrderedJson::Object(_)) {
        return None;
    }
    if ingest.get("status") != Some(&OrderedJson::Str("MISSING".to_owned())) {
        return None;
    }
    let truthy = |v: Option<&OrderedJson>| match v {
        Some(OrderedJson::Str(t)) if !t.is_empty() => Some(t.clone()),
        _ => None,
    };
    let day = truthy(ingest.get("trade_date"))
        .or_else(|| truthy(result.get("trade_date")))
        .unwrap_or_else(|| "None".to_owned());
    Some(FnoAlert {
        name: FnoName::BhavcopyMissing,
        severity: Level::Critical,
        summary: format!(
            "The F&O bhavcopy for {day} is MISSING after the 23:30 attempt: fo_contract_daily \
             has no rows for it, so no FO mark, F2 trail or scan was written from it. Never \
             interpolated (docs/fno/04 §4); re-run `fno_cli ingest` once NSE publishes it."
        ),
        labels: vec![("trade_date".to_owned(), day)],
        detail: object([(
            "error",
            ingest.get("error").cloned().unwrap_or(OrderedJson::Null),
        )]),
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]
    use super::*;

    #[test]
    fn missing_is_raised_only_for_missing() {
        let result = object([
            ("trade_date", s("2026-10-29")),
            (
                "ingest",
                object([
                    ("trade_date", s("2026-10-29")),
                    ("status", s("MISSING")),
                    ("error", s("ProviderError: 404")),
                ]),
            ),
        ]);
        let alert = bhavcopy_missing_alert(&result).unwrap();
        assert_eq!(alert.name.value(), "FNO_BHAVCOPY_MISSING");
        assert!(
            alert
                .summary
                .starts_with("The F&O bhavcopy for 2026-10-29 is MISSING")
        );
        let pending = object([("ingest", object([("status", s("PENDING"))]))]);
        assert!(bhavcopy_missing_alert(&pending).is_none());
        assert!(bhavcopy_missing_alert(&object([("skipped", s("x"))])).is_none());
    }

    #[test]
    fn the_payload_keeps_alerts_py_key_order() {
        let alert = FnoAlert {
            name: FnoName::Weekly,
            severity: Level::Warning,
            summary: "x".to_owned(),
            labels: vec![],
            detail: object::<&str>([]),
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
