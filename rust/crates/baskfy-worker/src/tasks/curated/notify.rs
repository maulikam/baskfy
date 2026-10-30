//! `tasks/curated_rebalance_notify.run_curated_rebalance_notify` (T8.3): one email per open
//! `REBALANCE_AVAILABLE` pending action lacking `payload.notified_at`, then the stamp, so a
//! second run is a no-op. The message is `baskfy_api.email.templates.rebalance_available`;
//! delivery is `baskfy_api`'s [`Mailer`] (the harness points it at its SMTP sink). Places no
//! order.

use baskfy_api::mailer::{Mailer, Message};
use baskfy_db::OrderedJson;
use baskfy_db::row::jsonb;
use chrono::{DateTime, Utc};
use sqlx::Row;

use crate::alerts::py_isoformat;
use crate::error::WorkerError;
use crate::pyjson::{int, object, update};
use crate::steps::log_warning;
use crate::tasks::TaskContext;

/// `_ACTION_TYPE`.
const ACTION_TYPE: &str = "REBALANCE_AVAILABLE";

/// `templates.DISCLAIMER`.
const DISCLAIMER: &str = "Baskfy is not a SEBI-registered investment adviser. Everything here \
                          is factual analysis of published market data, not investment advice.";

/// `templates.FOOTER_TEXT`.
const FOOTER_TEXT: &str = "You are receiving this because someone used this address at Baskfy. \
                           If it was not you, you can ignore this message.";

/// `html.escape(s, quote=True)`.
fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            other => out.push(other),
        }
    }
    out
}

/// `_masthead()` with `LOGO_URL` from `BASKFY_BRAND_ORIGIN` (Python reads it at import).
fn masthead() -> String {
    let origin = std::env::var("BASKFY_BRAND_ORIGIN")
        .unwrap_or_else(|_| "https://staging.baskfy.com".to_owned());
    format!(
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" \
         style=\"margin:0 0 24px\"><tr>\
         <td style=\"padding-right:10px;vertical-align:middle\">\
         <img src=\"{origin}/brand/logo-mark-192.png\" width=\"34\" height=\"34\" alt=\"\" \
         style=\"display:block;border:0;outline:none;text-decoration:none;height:34px;width:auto\">\
         </td>\
         <td style=\"vertical-align:middle;font-size:18px;font-weight:600;\
         letter-spacing:-0.035em;color:#14161a\">Baskfy</td>\
         </tr></table>"
    )
}

/// `_document(heading, paragraphs)` (default footer); `paragraphs` are escaped fragments.
fn document(heading: &str, paragraphs: &[String]) -> String {
    let body: String = paragraphs
        .iter()
        .map(|p| {
            if p.starts_with('<') {
                p.clone()
            } else {
                format!("<p style=\"margin:0 0 16px;line-height:1.5\">{p}</p>")
            }
        })
        .collect();
    format!(
        "<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;\
         font-size:15px;color:#14161a;max-width:560px;margin:0 auto;padding:24px\">\
         {}\
         <h1 style=\"font-size:18px;margin:0 0 20px\">{}</h1>\
         {body}\
         <hr style=\"border:none;border-top:1px solid #e4e6eb;margin:24px 0\">\
         <p style=\"font-size:12px;color:#6b7280;margin:0 0 8px\">{}</p>\
         <p style=\"font-size:12px;color:#6b7280;margin:0\">{}</p>\
         </div>",
        masthead(),
        escape(heading),
        escape(FOOTER_TEXT),
        escape(DISCLAIMER)
    )
}

/// `_plain(heading, paragraphs)` (default footer).
fn plain(heading: &str, paragraphs: &[String]) -> String {
    let mut lines = vec![heading.to_owned(), String::new()];
    lines.extend(paragraphs.iter().cloned());
    lines.extend([
        String::new(),
        "--".to_owned(),
        FOOTER_TEXT.to_owned(),
        DISCLAIMER.to_owned(),
    ]);
    lines.join("\n") + "\n"
}

/// `templates.rebalance_available(to, basket_name=, version_no=)`; `version_no` is already
/// Python's `str(int)` (a JSON `true` prints `True`).
#[must_use]
pub fn rebalance_available(to: &str, basket_name: &str, version_no: Option<&str>) -> Message {
    let heading = format!("Rebalance update available for {basket_name}");
    let version_bit = version_no.map_or_else(String::new, |v| format!("Version {v} is live. "));
    let body = vec![
        format!("{version_bit}A new target is ready for {basket_name}."),
        "Open Investments (/me/investments) to review the update. This message does not place \
         an order; you decide at the broker."
            .to_owned(),
    ];
    let html_paragraphs: Vec<String> = body.iter().map(|p| escape(p)).collect();
    Message {
        to: to.to_owned(),
        subject: format!("Rebalance update: {basket_name}"),
        text: plain(&heading, &body),
        html: document(&heading, &html_paragraphs),
        reply_to: None,
    }
}

/// Python truthiness of a JSON value (`payload.get("notified_at")`).
pub(super) fn truthy(v: &OrderedJson) -> bool {
    match v {
        OrderedJson::Null | OrderedJson::Bool(false) => false,
        OrderedJson::Int(i) => *i != 0,
        OrderedJson::UInt(u) => *u != 0,
        OrderedJson::Float(f) => *f != 0.0,
        OrderedJson::Str(s) => !s.is_empty(),
        OrderedJson::Array(a) => !a.is_empty(),
        OrderedJson::Object(o) => !o.is_empty(),
        OrderedJson::Bool(true) | OrderedJson::BigInt(_) => true,
    }
}

/// `isinstance(x, int)` (a `bool` is one) as an `i64`, with its `str()`.
fn py_int(v: Option<&OrderedJson>) -> Option<(i64, String)> {
    match v? {
        OrderedJson::Int(i) => Some((*i, i.to_string())),
        OrderedJson::Bool(b) => Some((i64::from(*b), if *b { "True" } else { "False" }.to_owned())),
        _ => None,
    }
}

/// `run_curated_rebalance_notify(session)`, in one transaction.
///
/// # Errors
/// A database failure (rolled back; mail already delivered stays delivered, as in Python).
pub async fn run_rebalance_notify(
    ctx: &TaskContext,
    moment: DateTime<Utc>,
) -> Result<OrderedJson, WorkerError> {
    let postman = Mailer::from_settings(&ctx.settings);
    let mut tx = ctx.db.begin().await?;
    let rows = sqlx::query(
        "SELECT cb_pending_action.id, cb_pending_action.user_id, cb_pending_action.type, \
         cb_pending_action.payload, cb_pending_action.created_at, cb_pending_action.dismissed_at, \
         cb_pending_action.resolved_at \nFROM cb_pending_action \nWHERE cb_pending_action.type = \
         $1::VARCHAR AND cb_pending_action.dismissed_at IS NULL AND \
         cb_pending_action.resolved_at IS NULL",
    )
    .bind(ACTION_TYPE)
    .fetch_all(&mut *tx)
    .await?;
    let (mut sent, mut skipped) = (0_i64, 0_i64);
    let mut stamped: Vec<(i64, String)> = Vec::new();
    for r in &rows {
        let id: i64 = r.try_get("id")?;
        let user_id: i64 = r.try_get("user_id")?;
        let mut payload = match jsonb(r, "payload")? {
            OrderedJson::Object(entries) => entries,
            _ => Vec::new(),
        };
        let notified = payload
            .iter()
            .find(|(k, _)| k == "notified_at")
            .map(|(_, v)| v);
        if notified.is_some_and(truthy) {
            skipped += 1;
            continue;
        }
        let email: Option<String> = sqlx::query_scalar(
            "SELECT app_user.email::text \nFROM app_user \nWHERE app_user.id = $1::BIGINT",
        )
        .bind(user_id)
        .fetch_optional(&mut *tx)
        .await?;
        let Some(email) = email.filter(|e| !e.trim().is_empty()) else {
            skipped += 1;
            continue;
        };
        let lookup = |key: &str| payload.iter().find(|(k, _)| k == key).map(|(_, v)| v);
        let mut basket_name = "your basket".to_owned();
        if let Some((basket_id, _)) = py_int(lookup("basket_id")) {
            let name: Option<String> = sqlx::query_scalar(
                "SELECT cb_basket.name \nFROM cb_basket \nWHERE cb_basket.id = $1::BIGINT",
            )
            .bind(basket_id)
            .fetch_optional(&mut *tx)
            .await?;
            if let Some(name) = name {
                basket_name = name;
            }
        }
        let version_no = py_int(lookup("version_no")).map(|(_, text)| text);
        let message = rebalance_available(&email, &basket_name, version_no.as_deref());
        if !postman.deliver(&message).await {
            log_warning(&format!("rebalance notify failed for action {id}"));
            skipped += 1;
            continue;
        }
        update(
            &mut payload,
            "notified_at",
            OrderedJson::Str(py_isoformat(moment)),
        );
        update(
            &mut payload,
            "delivery",
            OrderedJson::Str("email".to_owned()),
        );
        let text = OrderedJson::Object(payload)
            .to_python_json()
            .map_err(|e| WorkerError::python("TypeError", e.to_string()))?;
        stamped.push((id, text));
        sent += 1;
    }
    for (id, text) in stamped {
        sqlx::query(
            "UPDATE cb_pending_action SET payload=$1::JSONB WHERE cb_pending_action.id = $2::BIGINT",
        )
        .bind(text)
        .bind(id)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(object([
        (
            "examined",
            int(i64::try_from(rows.len()).unwrap_or(i64::MAX)),
        ),
        ("sent", int(sent)),
        ("skipped", int(skipped)),
    ]))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::*;

    #[test]
    fn the_message_names_the_basket_and_version_and_places_no_order() {
        let m = rebalance_available("a@example.com", "Momentum <Scan>", Some("3"));
        assert_eq!(m.subject, "Rebalance update: Momentum <Scan>");
        assert!(
            m.text
                .starts_with("Rebalance update available for Momentum <Scan>\n\n")
        );
        assert!(
            m.text
                .contains("Version 3 is live. A new target is ready for Momentum <Scan>.")
        );
        assert!(m.text.contains("This message does not place an order"));
        assert!(m.html.contains("Momentum &lt;Scan&gt;"));
        let m = rebalance_available("a@example.com", "your basket", None);
        assert!(
            m.text
                .contains("\nA new target is ready for your basket.\n")
        );
    }

    #[test]
    fn notified_at_truthiness_is_pythons() {
        assert!(!truthy(&OrderedJson::Str(String::new())));
        assert!(!truthy(&OrderedJson::Null));
        assert!(!truthy(&OrderedJson::Int(0)));
        assert!(truthy(&OrderedJson::Str(
            "2026-10-30T04:00:00+00:00".to_owned()
        )));
        assert_eq!(
            py_int(Some(&OrderedJson::Bool(true))),
            Some((1, "True".to_owned()))
        );
        assert_eq!(py_int(Some(&OrderedJson::Float(5.0))), None);
    }
}
