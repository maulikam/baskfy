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
    out.push_str(&format!("Subject: {}\r\n", header_value(&message.subject)));
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
        assert!(raw.contains("Subject: =?utf-8?b?"));
        assert!(raw.contains("MIME-Version: 1.0\r\n"));
        let encoded = base64::engine::general_purpose::STANDARD.encode(b"one\n\ntwo\n");
        assert!(raw.contains(&encoded));
    }
}
