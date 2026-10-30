//! The evening email — ports `baskfy_api.email.templates.swing_eod` (with its `SwingCandidate`,
//! `SwingDigest`, `_swing_lines`) and the shared shell it is rendered in (`_masthead`, `_document`,
//! `_plain`). The API crate keeps its copy of the shell private (`routes::support::templates`), so
//! the lane carries one; both are byte-for-byte Python's.

use baskfy_api::mailer::Message;
use chrono::NaiveDate;
use rust_decimal::{Decimal, RoundingStrategy};

/// `DISCLAIMER`.
const DISCLAIMER: &str = "Baskfy is not a SEBI-registered investment adviser. Everything here is \
                          factual analysis of published market data, not investment advice.";

/// `ALERT_FOOTER_TEXT`.
const ALERT_FOOTER_TEXT: &str = "You are receiving this because you subscribed a Baskfy screen to \
                                 alerts. Every alert carries its own link to stop it.";

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

/// `LOGO_URL` (`BASKFY_BRAND_ORIGIN`, read from the environment as Python reads it at import).
fn logo_url() -> String {
    let origin = std::env::var("BASKFY_BRAND_ORIGIN")
        .unwrap_or_else(|_| "https://staging.baskfy.com".to_owned());
    format!("{origin}/brand/logo-mark-192.png")
}

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

fn document(heading: &str, paragraphs: &[String], footer: &str) -> String {
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

fn plain(heading: &str, paragraphs: &[String], footer: &str) -> String {
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

/// `f"{value:,.2f}"` for a `Decimal` (the context's half-even rounding, a comma every three
/// digits).
pub(super) fn money(value: Decimal) -> String {
    let rounded = value.round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven);
    let negative = rounded.is_sign_negative() && !rounded.is_zero();
    let text = format!("{:.2}", rounded.abs());
    let (int_part, frac) = text.split_once('.').unwrap_or((text.as_str(), "00"));
    let digits: Vec<char> = int_part.chars().collect();
    let mut grouped = String::new();
    for (i, c) in digits.iter().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            grouped.push(',');
        }
        grouped.push(*c);
    }
    let sign = if negative || (rounded.is_zero() && value.is_sign_negative()) {
        "-"
    } else {
        ""
    };
    format!("{sign}{grouped}.{frac}")
}

/// `SwingCandidate`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct SwingCandidate {
    pub(super) symbol: String,
    pub(super) setup: String,
    pub(super) trigger: String,
    pub(super) stop: String,
    pub(super) note: String,
}

/// `SwingDigest`.
#[derive(Debug, Clone)]
pub(super) struct SwingDigest {
    pub(super) as_of: NaiveDate,
    pub(super) gate: String,
    pub(super) exposure_level: i64,
    pub(super) max_open_positions: i64,
    pub(super) naked: Vec<String>,
    pub(super) exits: Vec<SwingCandidate>,
    pub(super) entries: Vec<SwingCandidate>,
    pub(super) skips: Vec<(String, String)>,
    pub(super) flags: Vec<SwingCandidate>,
    pub(super) eps: Vec<SwingCandidate>,
    pub(super) sessions_logged: i64,
    pub(super) sessions_required: i64,
}

/// `f"{text:<width}"` / `f"{text:>width}"`: padded by code points, never truncated.
fn left(text: &str, width: usize) -> String {
    let n = text.chars().count();
    format!("{text}{}", " ".repeat(width.saturating_sub(n)))
}

fn right(text: &str, width: usize) -> String {
    let n = text.chars().count();
    format!("{}{text}", " ".repeat(width.saturating_sub(n)))
}

fn swing_lines(title: &str, rows: &[SwingCandidate]) -> Vec<String> {
    if rows.is_empty() {
        return vec![format!("{title}: none.")];
    }
    let mut out = vec![format!("{title}:")];
    for row in rows {
        let mut line = format!(
            "  {} {} trigger {}  stop {}",
            left(&row.symbol, 14),
            left(&row.setup, 6),
            right(&row.trigger, 10),
            right(&row.stop, 10)
        );
        if !row.note.is_empty() {
            line.push_str("  ");
            line.push_str(&row.note);
        }
        out.push(line);
    }
    out
}

/// `swing_eod(to, digest, swing_url=)`.
pub(super) fn swing_eod(to: &str, digest: &SwingDigest, swing_url: &str) -> Message {
    let as_of = digest.as_of.format("%Y-%m-%d").to_string();
    let naked_line = if digest.naked.is_empty() {
        "Every open position has a resting stop.".to_owned()
    } else {
        format!(
            "UNPROTECTED: {} — no resting stop. Arm one before the open.",
            digest.naked.join(", ")
        )
    };
    let heading = format!("Swing · {as_of} · {}", digest.gate);
    let urgent = if digest.naked.is_empty() {
        String::new()
    } else {
        format!("{} unprotected · ", digest.naked.len())
    };
    let subject = format!(
        "{urgent}{} exits, {} entries ({as_of})",
        digest.exits.len(),
        digest.entries.len()
    );
    let mut text_lines: Vec<String> = vec![
        naked_line.clone(),
        String::new(),
        format!(
            "Tape: {}. Rung {} of 4, up to {} positions.",
            digest.gate,
            digest.exposure_level + 1,
            digest.max_open_positions
        ),
        String::new(),
    ];
    text_lines.extend(swing_lines("Tomorrow's exits", &digest.exits));
    text_lines.push(String::new());
    text_lines.extend(swing_lines("Tomorrow's entries", &digest.entries));
    text_lines.push(String::new());
    if digest.skips.is_empty() {
        text_lines.push("Refused, and why: nothing was refused.".to_owned());
    } else {
        text_lines.push("Refused, and why:".to_owned());
        for (symbol, reason) in &digest.skips {
            text_lines.push(format!("  {} {reason}", left(symbol, 14)));
        }
    }
    text_lines.push(String::new());
    text_lines.extend(swing_lines("Flags forming", &digest.flags));
    text_lines.push(String::new());
    text_lines.extend(swing_lines("Episodic pivots", &digest.eps));
    text_lines.extend([
        String::new(),
        format!(
            "{} of {} paper sessions logged.",
            digest.sessions_logged, digest.sessions_required
        ),
        String::new(),
        format!("Open the swing hub: {swing_url}"),
    ]);

    let body_html = vec![
        format!(
            "<p style=\"margin:0 0 16px;font-size:15px{}\">{}</p>",
            if digest.naked.is_empty() {
                ""
            } else {
                ";color:#b45309;font-weight:600"
            },
            escape(&naked_line)
        ),
        format!(
            "<p style=\"margin:0 0 16px;font-size:14px\">Tape: <strong>{}</strong>. Rung {} of \
             4, up to {} positions.</p>",
            escape(&digest.gate),
            digest.exposure_level + 1,
            digest.max_open_positions
        ),
        format!(
            "<pre style=\"margin:0 0 16px;font-size:13px;white-space:pre-wrap\">{}</pre>",
            escape(&text_lines[4..].join("\n"))
        ),
        format!(
            "<p style=\"margin:0 0 16px;font-size:13px\"><a href=\"{}\">Open the swing hub</a></p>",
            escape(swing_url)
        ),
    ];
    Message {
        to: to.to_owned(),
        subject,
        text: plain(&heading, &text_lines, ALERT_FOOTER_TEXT),
        html: document(&heading, &body_html, ALERT_FOOTER_TEXT),
        reply_to: None,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn money_is_pythons_comma_two_places() {
        let d = |s: &str| s.parse::<Decimal>().unwrap();
        assert_eq!(money(d("1234567.5")), "1,234,567.50");
        assert_eq!(money(d("999.995")), "1,000.00");
        assert_eq!(money(d("0.125")), "0.12");
        assert_eq!(money(d("100")), "100.00");
        assert_eq!(money(d("-1234.5")), "-1,234.50");
    }

    #[test]
    fn the_email_says_what_is_unprotected_first_and_counts_in_the_subject() {
        let digest = SwingDigest {
            as_of: NaiveDate::from_ymd_opt(2026, 10, 29).unwrap(),
            gate: "RED".to_owned(),
            exposure_level: 0,
            max_open_positions: 0,
            naked: vec!["ABC".to_owned()],
            exits: vec![],
            entries: vec![SwingCandidate {
                symbol: "XYZ".to_owned(),
                setup: "FLAG".to_owned(),
                trigger: "1,234.50".to_owned(),
                stop: "1,100.00".to_owned(),
                note: String::new(),
            }],
            skips: vec![],
            flags: vec![],
            eps: vec![],
            sessions_logged: 3,
            sessions_required: 20,
        };
        let m = swing_eod("a@b.c", &digest, "http://x/swing");
        assert_eq!(m.subject, "1 unprotected · 0 exits, 1 entries (2026-10-29)");
        assert!(
            m.text
                .starts_with("Swing · 2026-10-29 · RED\n\nUNPROTECTED: ABC — no resting stop.")
        );
        assert!(
            m.text
                .contains("  XYZ            FLAG   trigger   1,234.50  stop   1,100.00\n")
        );
        assert!(m.text.contains("Refused, and why: nothing was refused."));
        assert!(m.html.contains(";color:#b45309;font-weight:600"));
    }
}
