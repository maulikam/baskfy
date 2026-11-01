//! `baskfy_api.email.templates.screen_alert` (with `AlertRow`, `AlertSection` and the shared
//! `_masthead`/`_document`/`_plain` shell) — byte-for-byte Python's, because the harness compares
//! each part's decoded text.
//!
//! The shell is restated here because the API's copy (`routes::support::templates`) is private to
//! that crate; both are held to the same Python by their graders.

use baskfy_api::mailer::Message;
use chrono::NaiveDate;

/// `PRODUCT_NAME`.
const PRODUCT_NAME: &str = "Baskfy";

/// `DISCLAIMER`.
const DISCLAIMER: &str = "Baskfy is not a SEBI-registered investment adviser. Everything here \
                          is factual analysis of published market data, not investment advice.";

/// `ALERT_FOOTER_TEXT`.
const ALERT_FOOTER_TEXT: &str = "You are receiving this because you subscribed a Baskfy screen \
                                 to alerts. Every alert carries its own link to stop it.";

/// `_RANK_WIDTH`, `_SYMBOL_WIDTH`, `_MOVE_WIDTH`.
const RANK_WIDTH: usize = 6;
const SYMBOL_WIDTH: usize = 14;
const MOVE_WIDTH: usize = 4;

/// `AlertRow`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct AlertRow {
    pub(super) symbol: String,
    pub(super) name: String,
    pub(super) rank: i64,
    pub(super) previous_rank: Option<i64>,
}

impl AlertRow {
    /// `places_moved`.
    fn places_moved(&self) -> i64 {
        self.previous_rank.map_or(0, |p| p - self.rank)
    }
}

/// `AlertSection`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct AlertSection {
    pub(super) screen_name: String,
    pub(super) screen_url: String,
    pub(super) unsubscribe_url: String,
    pub(super) as_of: NaiveDate,
    pub(super) previous_as_of: NaiveDate,
    pub(super) entries: Vec<AlertRow>,
    pub(super) exits: Vec<AlertRow>,
    pub(super) movers: Vec<AlertRow>,
    pub(super) entry_total: i64,
    pub(super) exit_total: i64,
    pub(super) mover_total: i64,
    pub(super) held_count: i64,
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

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

/// `str.ljust(width)` (code points).
fn ljust(s: &str, width: usize) -> String {
    let n = s.chars().count();
    let mut out = s.to_owned();
    out.extend(std::iter::repeat_n(' ', width.saturating_sub(n)));
    out
}

/// `str.rjust(width)` (code points).
fn rjust(s: &str, width: usize) -> String {
    let n = s.chars().count();
    let mut out: String = std::iter::repeat_n(' ', width.saturating_sub(n)).collect();
    out.push_str(s);
    out
}

/// `f"{n:+d}"`.
fn signed(n: i64) -> String {
    if n >= 0 {
        format!("+{n}")
    } else {
        n.to_string()
    }
}

/// `LOGO_URL`: `BASKFY_BRAND_ORIGIN` or the staging origin.
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

/// `_document(heading, paragraphs, footer)`.
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

/// `_plain(heading, paragraphs, footer)`.
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

/// `_text_row(row, show_move=...)`.
fn text_row(row: &AlertRow, show_move: bool) -> String {
    let rank = ljust(&format!("#{}", row.rank), RANK_WIDTH);
    let clipped: String = row.symbol.chars().take(SYMBOL_WIDTH).collect();
    let symbol = ljust(&clipped, SYMBOL_WIDTH);
    if !show_move {
        return format!("  {rank}{symbol}{}", row.name);
    }
    let marker = rjust(&signed(row.places_moved()), MOVE_WIDTH);
    let was = row
        .previous_rank
        .map_or_else(|| "None".to_owned(), |p| p.to_string());
    format!("  {marker}  {rank}{symbol}{} (was #{was})", row.name)
}

/// `_held(count)`.
fn held(count: i64) -> String {
    if count == 1 {
        "1 name held its rank.".to_owned()
    } else {
        format!("{count} names held their rank.")
    }
}

/// `_more(shown, total)`.
fn more(shown: usize, total: i64) -> Vec<String> {
    let shown = i64::try_from(shown).unwrap_or(i64::MAX);
    if total > shown {
        vec![format!("  … and {} more", total - shown)]
    } else {
        Vec::new()
    }
}

/// `_section_text(section, with_title=...)`.
fn section_text(section: &AlertSection, with_title: bool) -> Vec<String> {
    let mut lines: Vec<String> = if with_title {
        vec![section.screen_name.clone()]
    } else {
        Vec::new()
    };
    lines.push(format!(
        "{} → {}",
        iso(section.previous_as_of),
        iso(section.as_of)
    ));
    lines.push(String::new());
    let groups: [(&str, &[AlertRow], i64, bool); 3] = [
        ("Entries", &section.entries, section.entry_total, false),
        ("Exits", &section.exits, section.exit_total, false),
        ("Rank changes", &section.movers, section.mover_total, true),
    ];
    for (i, (title, rows, total, show_move)) in groups.into_iter().enumerate() {
        if i > 0 {
            lines.push(String::new());
        }
        lines.push(format!("{title} ({total})"));
        lines.extend(rows.iter().map(|r| text_row(r, show_move)));
        lines.extend(more(rows.len(), total));
        if total == 0 {
            lines.push("  none".to_owned());
        }
    }
    lines.extend([
        String::new(),
        held(section.held_count),
        String::new(),
        format!("View the screen: {}", section.screen_url),
        format!("Stop this alert: {}", section.unsubscribe_url),
    ]);
    lines
}

/// `_section_html`'s inner `table(rows, total, show_move=...)`.
fn table(rows: &[AlertRow], total: i64, show_move: bool) -> String {
    if total == 0 {
        return "<p style=\"margin:0 0 16px;color:#6b7280\">none</p>".to_owned();
    }
    let mut cells = String::new();
    for row in rows {
        cells.push_str(
            "<tr><td style=\"padding:2px 12px 2px 0;font-variant-numeric:tabular-nums\">",
        );
        if show_move {
            let was = row
                .previous_rank
                .map_or_else(|| "None".to_owned(), |p| p.to_string());
            cells.push_str(&format!(
                "{} (#{was} → #{})",
                escape(&signed(row.places_moved())),
                row.rank
            ));
        } else {
            cells.push_str(&format!("#{}", row.rank));
        }
        cells.push_str("</td><td style=\"padding:2px 12px 2px 0\"><strong>");
        cells.push_str(&escape(&row.symbol));
        cells.push_str("</strong></td><td style=\"padding:2px 0;color:#4b5563\">");
        cells.push_str(&escape(&row.name));
        cells.push_str("</td></tr>");
    }
    let shown = i64::try_from(rows.len()).unwrap_or(i64::MAX);
    let tail = if total > shown {
        format!(
            "<tr><td colspan=\"3\" style=\"padding:2px 0;color:#6b7280\">… and {} more</td></tr>",
            total - shown
        )
    } else {
        String::new()
    };
    format!(
        "<table style=\"border-collapse:collapse;font-size:14px;margin:0 0 16px\">{cells}{tail}</table>"
    )
}

/// `_section_html(section, with_title=...)`.
fn section_html(section: &AlertSection, with_title: bool) -> String {
    let title = if with_title {
        format!(
            "<h2 style=\"font-size:16px;margin:24px 0 4px\">{}</h2>",
            escape(&section.screen_name)
        )
    } else {
        String::new()
    };
    format!(
        "{title}\
         <p style=\"margin:0 0 16px;color:#6b7280;font-size:13px\">{} &rarr; {}</p>\
         <h3 style=\"font-size:14px;margin:0 0 6px\">Entries ({})</h3>{}\
         <h3 style=\"font-size:14px;margin:0 0 6px\">Exits ({})</h3>{}\
         <h3 style=\"font-size:14px;margin:0 0 6px\">Rank changes ({})</h3>{}\
         <p style=\"margin:0 0 16px;color:#6b7280;font-size:13px\">{}</p>\
         <p style=\"margin:0 0 16px\"><a href=\"{}\">View the screen</a> &middot; \
         <a href=\"{}\">Stop this alert</a></p>",
        iso(section.previous_as_of),
        iso(section.as_of),
        section.entry_total,
        table(&section.entries, section.entry_total, false),
        section.exit_total,
        table(&section.exits, section.exit_total, false),
        section.mover_total,
        table(&section.movers, section.mover_total, true),
        escape(&held(section.held_count)),
        escape(&section.screen_url),
        escape(&section.unsubscribe_url),
    )
}

/// `screen_alert(to, sections, manage_url=...)`: one screen, or a digest of several. `None` for
/// no sections (Python's `ValueError`, which the dispatch never reaches).
pub(super) fn screen_alert(
    to: &str,
    sections: &[AlertSection],
    manage_url: &str,
) -> Option<Message> {
    let (heading, subject) = match sections {
        [] => return None,
        [only] => (
            only.screen_name.clone(),
            format!(
                "{}: {} in, {} out ({})",
                only.screen_name,
                only.entry_total,
                only.exit_total,
                iso(only.as_of)
            ),
        ),
        many => (
            format!("Your {PRODUCT_NAME} screen alerts"),
            format!(
                "{PRODUCT_NAME} screen alerts: {} screens changed",
                many.len()
            ),
        ),
    };
    let with_title = sections.len() > 1;
    let mut text_lines: Vec<String> = Vec::new();
    for (index, section) in sections.iter().enumerate() {
        if index > 0 {
            text_lines.extend([String::new(), "-".repeat(60), String::new()]);
        }
        text_lines.extend(section_text(section, with_title));
    }
    text_lines.extend([String::new(), format!("Manage your alerts: {manage_url}")]);
    let sections_html: String = sections
        .iter()
        .map(|s| section_html(s, with_title))
        .collect();
    let html = document(
        &heading,
        &[
            sections_html,
            format!(
                "<p style=\"margin:0 0 16px;font-size:13px\"><a href=\"{}\">Manage your alerts</a></p>",
                escape(manage_url)
            ),
        ],
        ALERT_FOOTER_TEXT,
    );
    Some(Message {
        to: to.to_owned(),
        subject,
        text: plain(&heading, &text_lines, ALERT_FOOTER_TEXT),
        html,
        reply_to: None,
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn section(name: &str) -> AlertSection {
        AlertSection {
            screen_name: name.to_owned(),
            screen_url: "http://w/screens/abc".to_owned(),
            unsubscribe_url: "http://w/alerts/unsubscribe?token=t".to_owned(),
            as_of: NaiveDate::from_ymd_opt(2026, 10, 29).unwrap(),
            previous_as_of: NaiveDate::from_ymd_opt(2026, 10, 28).unwrap(),
            entries: vec![AlertRow {
                symbol: "RELIANCE".to_owned(),
                name: "Reliance Industries".to_owned(),
                rank: 3,
                previous_rank: None,
            }],
            exits: Vec::new(),
            movers: vec![AlertRow {
                symbol: "TCS".to_owned(),
                name: "Tata".to_owned(),
                rank: 12,
                previous_rank: Some(40),
            }],
            entry_total: 2,
            exit_total: 0,
            mover_total: 1,
            held_count: 1,
        }
    }

    #[test]
    fn a_single_alert_names_the_screen_and_lays_out_the_text_table() {
        let m = screen_alert("a@b.co", &[section("Mom & Co")], "http://w/alerts").unwrap();
        assert_eq!(m.subject, "Mom & Co: 2 in, 0 out (2026-10-29)");
        let lines: Vec<&str> = m.text.lines().collect();
        assert_eq!(lines[0], "Mom & Co");
        assert_eq!(lines[2], "2026-10-28 → 2026-10-29");
        assert!(lines.contains(&"  #3    RELIANCE      Reliance Industries"));
        assert!(lines.contains(&"  … and 1 more"));
        assert!(lines.contains(&"   +28  #12   TCS           Tata (was #40)"));
        assert!(lines.contains(&"1 name held its rank."));
        assert!(m.text.ends_with("not investment advice.\n"));
        assert!(
            m.html
                .contains("<h1 style=\"font-size:18px;margin:0 0 20px\">Mom &amp; Co</h1>")
        );
        assert!(m.html.contains("+28 (#40 → #12)"));
        assert!(!m.html.contains("<h2"));
    }

    #[test]
    fn a_digest_counts_the_screens_and_titles_each_section() {
        let m = screen_alert("a@b.co", &[section("A"), section("B")], "u").unwrap();
        assert_eq!(m.subject, "Baskfy screen alerts: 2 screens changed");
        assert!(m.text.starts_with("Your Baskfy screen alerts\n"));
        assert!(m.text.contains(&"-".repeat(60)));
        assert_eq!(m.html.matches("<h2").count(), 2);
        assert!(screen_alert("a@b.co", &[], "u").is_none());
    }
}
