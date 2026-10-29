//! The shared HTML/plain shell of `baskfy_api.email.templates` (`_masthead`, `_document`,
//! `_plain`) and the support form's two messages. Byte-for-byte Python's: the harness compares
//! each part's decoded text.

use crate::mailer::Message;

/// `PRODUCT_NAME`.
pub const PRODUCT_NAME: &str = "Baskfy";

/// `DISCLAIMER`.
pub const DISCLAIMER: &str = "Baskfy is not a SEBI-registered investment adviser. Everything \
                              here is factual analysis of published market data, not investment \
                              advice.";

/// `FOOTER_TEXT`.
pub const FOOTER_TEXT: &str = "You are receiving this because someone used this address at \
                               Baskfy. If it was not you, you can ignore this message.";

/// `html.escape(s, quote=True)`.
#[must_use]
pub fn escape(s: &str) -> String {
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

/// `LOGO_URL`: `BASKFY_BRAND_ORIGIN` (read from the process environment, as Python reads it at
/// import) or the staging origin.
fn logo_url() -> String {
    let origin = std::env::var("BASKFY_BRAND_ORIGIN")
        .unwrap_or_else(|_| "https://staging.baskfy.com".to_owned());
    format!("{origin}/brand/logo-mark-192.png")
}

/// `_masthead()`.
fn masthead() -> String {
    format!(
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" \
         style=\"margin:0 0 24px\"><tr>\
         <td style=\"padding-right:10px;vertical-align:middle\">\
         <img src=\"{}\" width=\"34\" height=\"34\" alt=\"\" \
         style=\"display:block;border:0;outline:none;text-decoration:none;height:34px;width:auto\">\
         </td>\
         <td style=\"vertical-align:middle;font-size:18px;font-weight:600;\
         letter-spacing:-0.035em;color:#14161a\">Baskfy</td>\
         </tr></table>",
        logo_url()
    )
}

/// `_document(heading, paragraphs, footer)`: `paragraphs` are already-escaped fragments.
#[must_use]
pub fn document(heading: &str, paragraphs: &[String], footer: &str) -> String {
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
        escape(footer),
        escape(DISCLAIMER)
    )
}

/// `_plain(heading, paragraphs, footer)`.
#[must_use]
pub fn plain(heading: &str, paragraphs: &[String], footer: &str) -> String {
    let mut lines = vec![heading.to_owned(), String::new()];
    lines.extend(paragraphs.iter().cloned());
    lines.extend([
        String::new(),
        "--".to_owned(),
        footer.to_owned(),
        DISCLAIMER.to_owned(),
    ]);
    lines.join("\n") + "\n"
}

/// `SupportSubmission`.
#[derive(Debug, Clone, Copy)]
pub struct SupportSubmission<'a> {
    /// `topic`.
    pub topic: &'a str,
    /// `from_name`.
    pub from_name: &'a str,
    /// `from_email`.
    pub from_email: &'a str,
    /// `body_text`.
    pub body_text: &'a str,
    /// `account_public_id`.
    pub account_public_id: Option<&'a str>,
}

/// `support_request(to, submission)`: to support, `Reply-To` the sender.
#[must_use]
pub fn support_request(to: &str, s: &SupportSubmission<'_>) -> Message {
    let heading = format!("Support: {}", s.topic);
    let attribution = match s.account_public_id {
        Some(id) if !id.is_empty() => format!("Signed in as account {id}."),
        _ => "Not signed in when this was sent.".to_owned(),
    };
    let paragraphs = vec![
        format!("From: {} <{}>", s.from_name, s.from_email),
        attribution,
        s.body_text.to_owned(),
    ];
    let footer = "Sent by the Baskfy support form. Reply to this message to answer the sender.";
    let escaped: Vec<String> = paragraphs.iter().map(|p| escape(p)).collect();
    Message {
        to: to.to_owned(),
        subject: format!("[{PRODUCT_NAME} support] {}", s.topic),
        text: plain(&heading, &paragraphs, footer),
        html: document(&heading, &escaped, footer),
        reply_to: Some(s.from_email.to_owned()),
    }
}

/// `support_receipt(to, topic=)`: the sender's copy.
#[must_use]
pub fn support_receipt(to: &str, topic: &str) -> Message {
    let heading = "We have your message";
    let body = vec![
        format!("Thanks — your message about “{topic}” reached us and someone will read it."),
        "Replies come from a real address, so you can answer them directly.".to_owned(),
        "This is a copy for your records; you do not need to do anything.".to_owned(),
    ];
    let escaped: Vec<String> = body.iter().map(|p| escape(p)).collect();
    Message {
        to: to.to_owned(),
        subject: format!("We have your {PRODUCT_NAME} support message"),
        text: plain(heading, &body, FOOTER_TEXT),
        html: document(heading, &escaped, FOOTER_TEXT),
        reply_to: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escaping_is_html_escape_with_quotes() {
        assert_eq!(
            escape("<a href=\"x\">'&'</a>"),
            "&lt;a href=&quot;x&quot;&gt;&#x27;&amp;&#x27;&lt;/a&gt;"
        );
    }

    #[test]
    fn the_request_names_the_sender_and_replies_to_them() {
        // test_api_support.py: the subject carries only the topic; Reply-To is the sender.
        let m = support_request(
            "support@baskfy.com",
            &SupportSubmission {
                topic: "My account",
                from_name: "A <b>",
                from_email: "a@example.com",
                body_text: "x",
                account_public_id: None,
            },
        );
        assert_eq!(m.subject, "[Baskfy support] My account");
        assert_eq!(m.reply_to.as_deref(), Some("a@example.com"));
        assert!(m.text.contains("From: A <b> <a@example.com>"));
        assert!(m.html.contains("From: A &lt;b&gt; &lt;a@example.com&gt;"));
        assert!(m.text.contains("Not signed in when this was sent."));
    }
}
