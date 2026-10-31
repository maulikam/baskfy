//! Delivery — `baskfy_api.email.sender` (`Mailer`, the transports) and `templates.Message`.
//!
//! `Mailer::deliver` never raises: a failed send is logged (`email delivery failed`) and reported
//! as `false`, and the caller answers as if nothing went wrong — an auth endpoint whose response
//! changed with the mail would tell a stranger whether the address is registered.
//!
//! Transports (`BASKFY_EMAIL_TRANSPORT`):
//! * `console` — logs the recipient and subject (warning) and the plain-text body (info), as
//!   Python's `ConsoleTransport`;
//! * `smtp` — a plain SMTP session (EHLO, MAIL, RCPT, DATA, QUIT) with a `multipart/alternative`
//!   message, text part first, as `_mime` builds it; graded against Python's through the harness's
//!   mail sink (headers and decoded parts compared);
//! * **not ported** (DECISIONS-RS R6.6): `smtp` with `BASKFY_SMTP_STARTTLS` or a username (TLS
//!   and AUTH, what SES needs), and `resend` (an HTTPS API). Both are reported as a failed send —
//!   logged, `false` — where Python would deliver, so a route that mails stays on Python on any
//!   deployment configured that way (a cutover gate).

use std::time::Duration;

use base64::Engine;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;

use crate::logging;
use crate::settings::Settings;

/// `SEND_TIMEOUT_SECONDS`.
const SEND_TIMEOUT: Duration = Duration::from_secs(10);

/// `templates.Message`: what a caller asks to send.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    /// `to`.
    pub to: String,
    /// `subject`.
    pub subject: String,
    /// `text`.
    pub text: String,
    /// `html`.
    pub html: String,
    /// `reply_to` (the support form sets it; it wins over the deployment's).
    pub reply_to: Option<String>,
}

/// `EmailNotSent`: the reason, never the body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmailNotSent(pub String);

/// The transport chosen by `build_transport(settings)`.
#[derive(Debug, Clone)]
enum Transport {
    Console,
    Smtp {
        host: String,
        port: u16,
        from: String,
        reply_to: Option<String>,
        unsupported: bool,
    },
    Resend,
}

/// `Mailer`: the process-wide sender.
#[derive(Debug, Clone)]
pub struct Mailer {
    transport: Transport,
}

impl Mailer {
    /// `Mailer(build_transport(settings))`.
    #[must_use]
    pub fn from_settings(settings: &Settings) -> Self {
        let transport = match settings.email_transport.as_str() {
            "resend" => Transport::Resend,
            "smtp" => Transport::Smtp {
                host: settings.smtp_host.clone(),
                port: u16::try_from(settings.smtp_port).unwrap_or(0),
                from: settings.email_from.clone(),
                reply_to: settings.email_reply_to.clone(),
                unsupported: settings.smtp_starttls || !settings.smtp_username.is_empty(),
            },
            _ => Transport::Console,
        };
        Self { transport }
    }

    /// `deliver(message)`: `true` when the transport accepted it.
    pub async fn deliver(&self, message: &Message) -> bool {
        match self.send(message).await {
            Ok(()) => true,
            Err(EmailNotSent(reason)) => {
                logging::error(
                    "baskfy_api.email.sender",
                    "email delivery failed",
                    &[("email_to", &message.to), ("reason", &reason)],
                );
                false
            }
        }
    }

    async fn send(&self, message: &Message) -> Result<(), EmailNotSent> {
        match &self.transport {
            Transport::Console => {
                logging::warning(
                    "baskfy_api.email.sender",
                    "email not delivered (transport=console)",
                    &[
                        ("email_to", &message.to),
                        ("email_subject", &message.subject),
                    ],
                );
                logging::info(
                    "baskfy_api.email.sender",
                    &format!("email body\n{}", message.text),
                    &[],
                );
                Ok(())
            }
            Transport::Resend => Err(EmailNotSent(
                "Resend request failed: not ported to Rust (DECISIONS-RS R6.6)".to_owned(),
            )),
            Transport::Smtp {
                host,
                port,
                from,
                reply_to,
                unsupported,
            } => {
                if *unsupported {
                    return Err(EmailNotSent(
                        "SMTP delivery failed: STARTTLS/AUTH not ported to Rust (DECISIONS-RS R6.6)"
                            .to_owned(),
                    ));
                }
                let reply = message.reply_to.as_ref().or(reply_to.as_ref());
                let raw = mime(message, from, reply.map(String::as_str));
                tokio::time::timeout(
                    SEND_TIMEOUT,
                    smtp_send(host, *port, from, &message.to, &raw),
                )
                .await
                .map_err(|_| EmailNotSent("SMTP delivery failed: TimeoutError".to_owned()))?
            }
        }
    }
}

/// `email.utils.getaddresses([value])[0][1]` for the simple shapes a setting holds:
/// `Name <addr>` or a bare address.
fn address_of(value: &str) -> String {
    match (value.rfind('<'), value.rfind('>')) {
        (Some(open), Some(close)) if open < close => value[open + 1..close].trim().to_owned(),
        _ => value.trim().to_owned(),
    }
}

/// A header value: ASCII as is, otherwise RFC 2047 base64 (`=?utf-8?b?...?=`).
fn header_value(value: &str) -> String {
    if value.is_ascii() {
        value.to_owned()
    } else {
        format!(
            "=?utf-8?b?{}?=",
            base64::engine::general_purpose::STANDARD.encode(value.as_bytes())
        )
    }
}

const MAXLEN: i64 = 78;
const CHROME_LEN: i64 = 12;

fn is_wsp(c: char) -> bool {
    c == ' ' || c == '\t'
}

fn clen(s: &str) -> i64 {
    i64::try_from(s.chars().count()).unwrap_or(i64::MAX)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Name,
    Sep,
    Utext,
    Fws,
    Label,
    Cfws,
    Unstructured,
    EndBlocked,
}

#[derive(Clone)]
struct Tok {
    kind: Kind,
    text: String,
    children: Vec<Tok>,
}

impl Tok {
    fn term(kind: Kind, text: &str) -> Self {
        Self {
            kind,
            text: text.to_owned(),
            children: Vec::new(),
        }
    }
    fn list(kind: Kind, children: Vec<Tok>) -> Self {
        Self {
            kind,
            text: String::new(),
            children,
        }
    }
    fn is_list(&self) -> bool {
        matches!(self.kind, Kind::Label | Kind::Cfws | Kind::Unstructured)
    }
    fn render(&self) -> String {
        if self.is_list() {
            self.children.iter().map(Tok::render).collect()
        } else {
            self.text.clone()
        }
    }
    fn as_ew_allowed(&self) -> bool {
        match self.kind {
            Kind::Label => false,
            Kind::Cfws | Kind::Unstructured => self.children.iter().all(Tok::as_ew_allowed),
            _ => true,
        }
    }
    fn startswith_fws(&self) -> bool {
        match self.kind {
            Kind::Fws => self.text.starts_with(is_wsp),
            Kind::Label | Kind::Cfws | Kind::Unstructured => {
                self.children.first().is_some_and(Tok::startswith_fws)
            }
            _ => false,
        }
    }
}

/// `get_unstructured` for a value with no encoded words: WSP runs and non-WSP runs.
fn unstructured_tokens(value: &str) -> Vec<Tok> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut cur_ws = false;
    for c in value.chars() {
        if !cur.is_empty() && is_wsp(c) != cur_ws {
            out.push(Tok::term(
                if cur_ws { Kind::Fws } else { Kind::Utext },
                &cur,
            ));
            cur.clear();
        }
        cur_ws = is_wsp(c);
        cur.push(c);
    }
    if !cur.is_empty() {
        out.push(Tok::term(
            if cur_ws { Kind::Fws } else { Kind::Utext },
            &cur,
        ));
    }
    out
}

/// `email._encoded_words.encode(s, "utf-8")`: q unless b is five or more shorter.
fn ew_encode(s: &str) -> String {
    let bytes = s.as_bytes();
    let q_safe = |b: u8| b.is_ascii_alphanumeric() || b"-!*+/".contains(&b);
    let qlen: usize = bytes
        .iter()
        .map(|&b| if q_safe(b) || b == b' ' { 1 } else { 3 })
        .sum();
    let blen = bytes.len() / 3 * 4 + if bytes.len().is_multiple_of(3) { 0 } else { 4 };
    if qlen < blen + 5 {
        let mut q = String::new();
        for &b in bytes {
            if b == b' ' {
                q.push('_');
            } else if q_safe(b) {
                q.push(char::from(b));
            } else {
                q.push_str(&format!("={b:02X}"));
            }
        }
        format!("=?utf-8?q?{q}?=")
    } else {
        format!(
            "=?utf-8?b?{}?=",
            base64::engine::general_purpose::STANDARD.encode(bytes)
        )
    }
}

fn ew_decode(cte: char, text: &str) -> Option<String> {
    let bytes = if cte == 'b' {
        base64::engine::general_purpose::STANDARD
            .decode(text)
            .ok()?
    } else {
        let raw = text.as_bytes();
        let mut out = Vec::new();
        let mut i = 0;
        while i < raw.len() {
            match raw[i] {
                b'_' => out.push(b' '),
                b'=' if raw.len() >= i + 3 => {
                    let h = std::str::from_utf8(&raw[i + 1..i + 3]).ok()?;
                    out.push(u8::from_str_radix(h, 16).ok()?);
                    i += 2;
                }
                b => out.push(b),
            }
            i += 1;
        }
        out
    };
    String::from_utf8(bytes).ok()
}

/// `str(get_unstructured(s))` over a line this folder wrote: encoded words decoded, and the
/// whitespace between two encoded words dropped.
fn unstructured_str(s: &str) -> String {
    let mut toks: Vec<(bool, bool, String)> = Vec::new();
    let mut rest = s;
    while !rest.is_empty() {
        if rest.starts_with(is_wsp) {
            let n = rest.len() - rest.trim_start_matches(is_wsp).len();
            toks.push((false, true, rest[..n].to_owned()));
            rest = &rest[n..];
            continue;
        }
        if let Some(body) = rest.strip_prefix("=?utf-8?") {
            let cte = body.chars().next().unwrap_or(' ');
            if (cte == 'q' || cte == 'b')
                && body.get(1..).is_some_and(|b| b.starts_with('?'))
                && let Some(end) = body[2..].find("?=")
                && let Some(text) = ew_decode(cte, &body[2..2 + end])
            {
                let n = toks.len();
                if n > 1 && toks[n - 1].1 && toks[n - 2].0 {
                    toks[n - 1].2.clear();
                }
                toks.push((true, false, text));
                rest = &body[2 + end + 2..];
                continue;
            }
        }
        let mut n = rest.find(is_wsp).unwrap_or(rest.len());
        let first = rest.chars().next().map_or(0, char::len_utf8);
        if let Some(at) = rest[first..].find("=?utf-8?") {
            n = n.min(at + first);
        }
        toks.push((false, false, rest[..n].to_owned()));
        rest = &rest[n..];
    }
    toks.into_iter().map(|t| t.2).collect()
}

fn steal_trailing_wsp(lines: &mut [String]) -> String {
    if let Some(last) = lines.last_mut()
        && last.ends_with(is_wsp)
        && let Some(c) = last.pop()
    {
        return c.to_string();
    }
    String::new()
}

fn last_line(lines: &mut Vec<String>) -> &mut String {
    if lines.is_empty() {
        lines.push(String::new());
    }
    let n = lines.len() - 1;
    &mut lines[n]
}

/// `_fold_as_ew` (CPython 3.12) with `charset="utf-8"` and combining allowed.
fn fold_as_ew(
    to_encode: &str,
    lines: &mut Vec<String>,
    last_ew: Option<usize>,
    leading_whitespace: &str,
) -> Option<usize> {
    let mut to_encode: Vec<char> = to_encode.chars().collect();
    let mut leading_whitespace = leading_whitespace.to_owned();
    if let Some(at) = last_ew {
        let last = last_line(lines);
        let tail = last.get(at..).unwrap_or("").to_owned();
        last.truncate(at);
        let joined: String = tail + &to_encode.iter().collect::<String>();
        to_encode = unstructured_str(&joined).chars().collect();
    } else if to_encode.first().is_some_and(|&c| is_wsp(c)) {
        let leading = to_encode.remove(0);
        if clen(last_line(lines)) == MAXLEN {
            let w = steal_trailing_wsp(lines);
            lines.push(w);
        }
        last_line(lines).push(leading);
    }
    let mut trailing = None;
    if to_encode.last().is_some_and(|&c| is_wsp(c)) {
        trailing = to_encode.pop();
    }
    let mut new_last_ew = last_ew.unwrap_or_else(|| last_line(lines).len());
    while !to_encode.is_empty() {
        let remaining = MAXLEN - clen(last_line(lines));
        let text_space = remaining - CHROME_LEN - clen(&leading_whitespace);
        if text_space <= 0 {
            lines.push(" ".to_owned());
            continue;
        }
        if lines.len() > 1 && clen(last_line(lines)) == 1 && !leading_whitespace.is_empty() {
            let ew = ew_encode(&leading_whitespace);
            last_line(lines).push_str(&ew);
            leading_whitespace.clear();
        }
        let mut take = usize::try_from(text_space)
            .unwrap_or(0)
            .min(to_encode.len());
        let mut word: String = to_encode[..take].iter().collect();
        let mut encoded = ew_encode(&word);
        while clen(&encoded) > remaining && take > 0 {
            take -= 1;
            word = to_encode[..take].iter().collect();
            encoded = ew_encode(&word);
        }
        last_line(lines).push_str(&encoded);
        to_encode.drain(..take);
        leading_whitespace.clear();
        if !to_encode.is_empty() {
            lines.push(" ".to_owned());
            new_last_ew = 1;
        }
    }
    if let Some(c) = trailing {
        last_line(lines).push(c);
    }
    Some(new_last_ew)
}

/// `_refold_parse_tree` (CPython 3.12, `policy.default`, `utf8=False`) over the tree a list
/// of tokens makes; the lines without their separators.
fn refold(tree: Vec<Tok>) -> Vec<String> {
    let mut lines = vec![String::new()];
    let mut leading_whitespace = String::new();
    let mut last_ew: Option<usize> = None;
    let mut blocked = 0_i32;
    let mut want_encoding = false;
    let mut parts: std::collections::VecDeque<Tok> = tree.into();
    while let Some(part) = parts.pop_front() {
        if part.kind == Kind::EndBlocked {
            blocked -= 1;
            continue;
        }
        let tstr = part.render();
        if !want_encoding {
            want_encoding = tstr.contains(['\n', '\r']);
        }
        if !tstr.is_ascii() {
            want_encoding = true;
        }
        if want_encoding && blocked == 0 {
            if !part.as_ew_allowed() {
                want_encoding = false;
                last_ew = None;
                let folded = refold(part.children.clone());
                if folded.len() == 1 {
                    let encoded = folded.into_iter().next().unwrap_or_default();
                    if clen(&encoded) > MAXLEN - clen(last_line(&mut lines)) {
                        let w = steal_trailing_wsp(&mut lines);
                        lines.push(w);
                    }
                    last_line(&mut lines).push_str(&encoded);
                    continue;
                }
            }
            if part.is_list() {
                parts = part.children.into_iter().chain(parts).collect();
                want_encoding = false;
                continue;
            }
            last_ew = fold_as_ew(&tstr, &mut lines, last_ew, &leading_whitespace);
            leading_whitespace.clear();
            want_encoding = false;
            continue;
        }
        if clen(&tstr) <= MAXLEN - clen(last_line(&mut lines)) {
            last_line(&mut lines).push_str(&tstr);
            continue;
        }
        leading_whitespace.clear();
        if clen(&tstr) < MAXLEN {
            let newline = steal_trailing_wsp(&mut lines);
            if !newline.is_empty() || part.startswith_fws() {
                lines.push(newline + &tstr);
                leading_whitespace = last_line(&mut lines)
                    .chars()
                    .take_while(|&c| is_wsp(c))
                    .collect();
                last_ew = None;
                continue;
            }
        }
        if part.is_list() {
            let allowed = part.as_ew_allowed();
            let mut newparts = part.children;
            if !allowed {
                blocked += 1;
                newparts.push(Tok::term(Kind::EndBlocked, ""));
            }
            parts = newparts.into_iter().chain(parts).collect();
            continue;
        }
        if blocked == 0 {
            parts.push_front(part);
            want_encoding = true;
            continue;
        }
        let newline = steal_trailing_wsp(&mut lines);
        if !newline.is_empty() || part.startswith_fws() {
            lines.push(newline + &tstr);
        } else {
            last_line(&mut lines).push_str(&tstr);
        }
    }
    lines
}

/// An unstructured header (`Subject`) as `EmailMessage` + `smtplib.send_message` write it:
/// folded at 78 with RFC 2047 words, CRLF-terminated.
fn fold_unstructured(name: &str, value: &str) -> String {
    let mut tree = vec![Tok::list(
        Kind::Label,
        vec![Tok::term(Kind::Name, name), Tok::term(Kind::Sep, ":")],
    )];
    let body = unstructured_tokens(value);
    if !body.is_empty() {
        tree.push(Tok::list(Kind::Cfws, vec![Tok::term(Kind::Fws, " ")]));
    }
    tree.push(Tok::list(Kind::Unstructured, body));
    let mut out = refold(tree).join("\r\n");
    out.push_str("\r\n");
    out
}

/// A body part's content as `set_content` stores it: one trailing newline.
fn part_body(text: &str) -> String {
    let mut body = text.replace("\r\n", "\n");
    if !body.ends_with('\n') {
        body.push('\n');
    }
    let encoded = base64::engine::general_purpose::STANDARD.encode(body.as_bytes());
    let mut out = String::new();
    for chunk in encoded.as_bytes().chunks(76) {
        out.push_str(&String::from_utf8_lossy(chunk));
        out.push_str("\r\n");
    }
    out
}

/// `_mime(message, sender, reply_to)`: `multipart/alternative`, the text part first.
fn mime(message: &Message, from: &str, reply_to: Option<&str>) -> String {
    let boundary = format!(
        "==============={:019}==",
        rand::random::<u64>() % 10_u64.pow(19)
    );
    let mut out = String::new();
    out.push_str(&format!("From: {}\r\n", header_value(from)));
    out.push_str(&format!("To: {}\r\n", header_value(&message.to)));
    out.push_str(&fold_unstructured("Subject", &message.subject));
    if let Some(r) = reply_to {
        out.push_str(&format!("Reply-To: {}\r\n", header_value(r)));
    }
    out.push_str("MIME-Version: 1.0\r\n");
    out.push_str(&format!(
        "Content-Type: multipart/alternative; boundary=\"{boundary}\"\r\n\r\n"
    ));
    for (subtype, content) in [("plain", &message.text), ("html", &message.html)] {
        out.push_str(&format!("--{boundary}\r\n"));
        out.push_str(&format!(
            "Content-Type: text/{subtype}; charset=\"utf-8\"\r\n"
        ));
        out.push_str("Content-Transfer-Encoding: base64\r\n\r\n");
        out.push_str(&part_body(content));
    }
    out.push_str(&format!("--{boundary}--\r\n"));
    out
}

async fn expect(
    reader: &mut BufReader<tokio::net::tcp::OwnedReadHalf>,
    ok: &[u8],
    class: &str,
) -> Result<(), EmailNotSent> {
    loop {
        let mut line = String::new();
        let n = reader
            .read_line(&mut line)
            .await
            .map_err(|_| EmailNotSent("SMTP delivery failed: SMTPServerDisconnected".to_owned()))?;
        if n == 0 {
            return Err(EmailNotSent(
                "SMTP delivery failed: SMTPServerDisconnected".to_owned(),
            ));
        }
        // A multi-line reply continues with `NNN-`; the last line is `NNN `.
        if line.as_bytes().get(3) == Some(&b'-') {
            continue;
        }
        return if line.as_bytes().first().is_some_and(|b| ok.contains(b)) {
            Ok(())
        } else {
            Err(EmailNotSent(format!("SMTP delivery failed: {class}")))
        };
    }
}

/// One SMTP session: what `smtplib.SMTP(...).send_message(mail)` does on an unauthenticated,
/// unencrypted relay.
async fn smtp_send(
    host: &str,
    port: u16,
    from: &str,
    to: &str,
    raw: &str,
) -> Result<(), EmailNotSent> {
    let stream = TcpStream::connect((host, port)).await.map_err(|e| {
        let class = match e.kind() {
            std::io::ErrorKind::ConnectionRefused => "ConnectionRefusedError",
            std::io::ErrorKind::TimedOut => "TimeoutError",
            _ => "OSError",
        };
        EmailNotSent(format!("SMTP delivery failed: {class}"))
    })?;
    let (read, mut write) = stream.into_split();
    let mut reader = BufReader::new(read);
    let send = |line: String| line.into_bytes();
    expect(&mut reader, b"2", "SMTPConnectError").await?;
    let steps: [(Vec<u8>, &[u8], &str); 4] = [
        (send("EHLO localhost\r\n".to_owned()), b"2", "SMTPHeloError"),
        (
            send(format!("MAIL FROM:<{}>\r\n", address_of(from))),
            b"2",
            "SMTPSenderRefused",
        ),
        (
            send(format!("RCPT TO:<{}>\r\n", address_of(to))),
            b"2",
            "SMTPRecipientsRefused",
        ),
        (send("DATA\r\n".to_owned()), b"3", "SMTPDataError"),
    ];
    for (line, ok, class) in steps {
        write
            .write_all(&line)
            .await
            .map_err(|_| EmailNotSent("SMTP delivery failed: OSError".to_owned()))?;
        expect(&mut reader, ok, class).await?;
    }
    let mut data = String::new();
    for line in raw.split("\r\n") {
        if line.starts_with('.') {
            data.push('.');
        }
        data.push_str(line);
        data.push_str("\r\n");
    }
    // `raw` ends with CRLF, so the split left one empty last line; replace it with the end mark.
    if data.ends_with("\r\n\r\n") {
        data.truncate(data.len() - 2);
    }
    data.push_str(".\r\n");
    write
        .write_all(data.as_bytes())
        .await
        .map_err(|_| EmailNotSent("SMTP delivery failed: OSError".to_owned()))?;
    expect(&mut reader, b"2", "SMTPDataError").await?;
    let _ = write.write_all(b"QUIT\r\n").await;
    let _ = expect(&mut reader, b"2", "SMTPServerDisconnected").await;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_envelope_address_is_the_one_inside_the_angle_brackets() {
        assert_eq!(
            address_of("Baskfy <no-reply@baskfy.com>"),
            "no-reply@baskfy.com"
        );
        assert_eq!(address_of("a@b.co"), "a@b.co");
    }

    #[test]
    fn the_message_is_multipart_alternative_with_the_text_first() {
        let m = Message {
            to: "a@b.co".to_owned(),
            subject: "Hi ₹".to_owned(),
            text: "one\n\ntwo".to_owned(),
            html: "one<br/>two".to_owned(),
            reply_to: None,
        };
        let raw = mime(&m, "Baskfy <no-reply@baskfy.com>", None);
        let text_at = raw.find("text/plain").unwrap_or(usize::MAX);
        let html_at = raw.find("text/html").unwrap_or(0);
        assert!(text_at < html_at);
        assert!(raw.contains("Subject: Hi =?utf-8?b?4oK5?=\r\n"));
        assert!(raw.contains("MIME-Version: 1.0\r\n"));
        let encoded = base64::engine::general_purpose::STANDARD.encode(b"one\n\ntwo\n");
        assert!(raw.contains(&encoded));
    }

    #[test]
    fn subject_folding_is_byte_identical_to_python_email() {
        let vectors: Vec<(String, String)> =
            serde_json::from_str(include_str!("../tests/data/mail_subject_vectors.json"))
                .unwrap_or_default();
        assert!(vectors.len() >= 16);
        for (subject, want) in &vectors {
            assert_eq!(&fold_unstructured("Subject", subject), want, "{subject:?}");
        }
    }
}
