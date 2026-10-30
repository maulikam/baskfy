//! Ports `app/swing_notify.py` (SW11, STANDING-ANSWERS A2): a TRIGGERED signal for a
//! daily-focus name, told once, one way. Never a confirm path — a reply or a tap must not act.
//! This module composes a message and hands it to a [`Sender`]; it imports nothing from the
//! desk's trading side, names no route that acts, and reads nothing back from either channel.
//!
//! **Transports (DECISIONS-RS R8.SM3, closing R8.7's stub).** [`EmailSender`] speaks SMTP as
//! `smtplib.SMTP(host, port, timeout=5)` does — `ehlo`, `starttls` and `login` when configured,
//! `send_message`, `quit` — with the message `EmailMessage.set_content(text)` builds. Two
//! deliberate differences, both recorded: STARTTLS **verifies the relay's certificate** against
//! the webpki roots (Python's `starttls()` with no context does not verify), and a body whose
//! lines exceed 78 characters goes base64 where Python may pick quoted-printable (the decoded
//! text is identical). [`TelegramSender`] is one `sendMessage` POST and nothing else: there is
//! no `getUpdates`, no webhook, no handler. Both answer `false` and log on any failure, and the
//! Telegram token never reaches a log line (it is in the URL, so no URL is ever printed).

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use base64::Engine as _;
use chrono::{DateTime, FixedOffset};
use rust_decimal::Decimal;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};

use super::rules::{money, py_dec};
use crate::settings::Settings;

/// `SEND_TIMEOUT_SECONDS`.
pub const SEND_TIMEOUT_SECONDS: f64 = 5.0;

/// `TELEGRAM_SEND_URL` — the one method this module knows. There is no `getUpdates`.
pub const TELEGRAM_SEND_URL: &str = "https://api.telegram.org/bot{token}/sendMessage";

/// `SignalNotice`: what one TRIGGERED signal is told as.
#[derive(Debug, Clone, PartialEq)]
pub struct SignalNotice {
    /// `symbol`.
    pub symbol: String,
    /// `setup`.
    pub setup: String,
    /// `at` (aware, IST).
    pub at: DateTime<FixedOffset>,
    /// `entry`.
    pub entry: Option<Decimal>,
    /// `stop`.
    pub stop: Option<Decimal>,
    /// `quantity`.
    pub quantity: Option<i64>,
    /// `risk_inr`.
    pub risk_inr: Option<Decimal>,
    /// `plan_expires_at`.
    pub plan_expires_at: Option<DateTime<FixedOffset>>,
    /// The skip reason when the rules lined nothing.
    pub skipped: Option<String>,
    /// `DRY_RUN` / `LIVE`.
    pub mode: String,
}

fn opt_dec(d: Option<Decimal>) -> String {
    d.map_or_else(|| "None".to_owned(), py_dec)
}

/// `datetime.isoformat()` for an aware datetime (microseconds only when non-zero).
#[must_use]
pub fn iso(at: &DateTime<FixedOffset>) -> String {
    if at.timestamp_subsec_micros() == 0 {
        at.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
    } else {
        at.format("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
    }
}

impl SignalNotice {
    /// `subject()`.
    #[must_use]
    pub fn subject(&self) -> String {
        let hm = self.at.format("%H:%M");
        match &self.skipped {
            Some(skip) if !skip.is_empty() => {
                format!(
                    "[swing] {} triggered at {hm} — skipped ({skip})",
                    self.symbol
                )
            }
            _ => format!("[swing] {} triggered at {hm} — {}", self.symbol, self.mode),
        }
    }

    /// `text()`.
    #[must_use]
    pub fn text(&self) -> String {
        let mut lines = vec![format!(
            "{} ({}) broke its opening range at {} IST.",
            self.symbol,
            self.setup,
            self.at.format("%H:%M")
        )];
        match &self.skipped {
            Some(skip) if !skip.is_empty() => lines.push(format!("No line: {skip}.")),
            _ => {
                lines.push(format!("entry   {}", opt_dec(self.entry)));
                lines.push(format!("stop    {}", opt_dec(self.stop)));
                lines.push(format!(
                    "qty     {}",
                    self.quantity
                        .map_or_else(|| "None".to_owned(), |q| q.to_string())
                ));
                lines.push(self.risk_inr.map_or_else(
                    || "risk    -".to_owned(),
                    |r| format!("risk    ₹{}", money(r)),
                ));
                lines.push(self.plan_expires_at.map_or_else(
                    || "expires -".to_owned(),
                    |e| format!("expires {} IST", e.format("%H:%M")),
                ));
                lines.push(format!("mode    {}", self.mode));
            }
        }
        lines.push("This message tells; it cannot act. Open the desk page.".to_owned());
        lines.join("\n")
    }
}

/// `notice_json`: the notice as one JSON line (`json.dumps` defaults: `", "`/`": "`, ASCII).
#[must_use]
pub fn notice_json(notice: &SignalNotice) -> String {
    let s = |v: &str| baskfy_providers::py::json_str(v);
    let opt = |v: Option<String>| v.map_or_else(|| "null".to_owned(), |x| s(&x));
    format!(
        "{{\"symbol\": {}, \"setup\": {}, \"at\": {}, \"entry\": {}, \"stop\": {}, \
         \"quantity\": {}, \"risk_inr\": {}, \"plan_expires_at\": {}, \"skipped\": {}, \
         \"mode\": {}}}",
        s(&notice.symbol),
        s(&notice.setup),
        s(&iso(&notice.at)),
        opt(notice.entry.map(py_dec)),
        opt(notice.stop.map(py_dec)),
        notice
            .quantity
            .map_or_else(|| "null".to_owned(), |q| q.to_string()),
        opt(notice.risk_inr.map(py_dec)),
        opt(notice.plan_expires_at.as_ref().map(iso)),
        opt(notice.skipped.clone()),
        s(&notice.mode),
    )
}

/// `Sender`: one channel. `send` answers whether the channel accepted the message.
#[async_trait]
pub trait Sender: Send + Sync {
    /// `name`.
    fn name(&self) -> &str;
    /// `send(subject, text)`.
    ///
    /// # Errors
    /// A channel may fail by raising; [`Notifier::notify`] counts that as not sent.
    async fn send(&self, subject: &str, text: &str) -> Result<bool, String>;
}

fn warn(message: &str) {
    crate::lanes::swing::swing_monitor::emit(&format!("WARNING swing_notify {message}"));
}

// =============================================================================================
// Email: smtplib's dialogue
// =============================================================================================

/// A byte stream an SMTP session runs over (a TCP socket, the same socket under TLS, or a
/// test's in-memory pipe).
pub trait Wire: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Wire for T {}

/// What `smtplib.SMTP` connects with (`smtp=` in Python, for the tests).
#[async_trait]
pub trait SmtpConnect: Send + Sync {
    /// Open a stream to `host:port` within `timeout`.
    ///
    /// # Errors
    /// The connection's (`OSError` in Python).
    async fn connect(
        &self,
        host: &str,
        port: u16,
        timeout: Duration,
    ) -> Result<Box<dyn Wire>, String>;

    /// Wrap the stream in TLS for `host` (`starttls()`).
    ///
    /// # Errors
    /// The handshake's.
    async fn tls(&self, stream: Box<dyn Wire>, host: &str) -> Result<Box<dyn Wire>, String>;
}

/// The real network: TCP, and rustls with the webpki roots for STARTTLS.
#[derive(Debug, Default, Clone, Copy)]
pub struct TcpSmtp;

#[async_trait]
impl SmtpConnect for TcpSmtp {
    async fn connect(
        &self,
        host: &str,
        port: u16,
        timeout: Duration,
    ) -> Result<Box<dyn Wire>, String> {
        let stream = tokio::time::timeout(timeout, tokio::net::TcpStream::connect((host, port)))
            .await
            .map_err(|_| "TimeoutError: timed out".to_owned())?
            .map_err(|e| format!("OSError: {}", e.kind()))?;
        Ok(Box::new(stream))
    }

    async fn tls(&self, stream: Box<dyn Wire>, host: &str) -> Result<Box<dyn Wire>, String> {
        use tokio_rustls::rustls;
        let mut roots = rustls::RootCertStore::empty();
        roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let config = rustls::ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .map_err(|e| format!("SSLError: {e}"))?
            .with_root_certificates(roots)
            .with_no_client_auth();
        let name = rustls::pki_types::ServerName::try_from(host.to_owned())
            .map_err(|e| format!("SSLError: {e}"))?;
        let tls = tokio_rustls::TlsConnector::from(Arc::new(config))
            .connect(name, stream)
            .await
            .map_err(|e| format!("SSLError: {}", e.kind()))?;
        Ok(Box::new(tls))
    }
}

/// One SMTP conversation as `smtplib.SMTP` holds it.
struct Session {
    io: Option<BufReader<Box<dyn Wire>>>,
    timeout: Duration,
    ehlo_done: bool,
    does_esmtp: bool,
    features: Vec<(String, String)>,
    auth_challenges: u32,
}

fn smtp_error(class: &str, code: i64, text: &str) -> String {
    format!(
        "{class}: ({code}, {})",
        baskfy_providers::py::repr_str(text)
    )
}

impl Session {
    fn io(&mut self) -> Result<&mut BufReader<Box<dyn Wire>>, String> {
        self.io
            .as_mut()
            .ok_or_else(|| "SMTPServerDisconnected: please run connect() first".to_owned())
    }

    /// `putcmd(cmd, args)`: `"<cmd> <args>\r\n"`, or `"<cmd>\r\n"`.
    async fn put(&mut self, line: &str) -> Result<(), String> {
        let timeout = self.timeout;
        let io = self.io()?;
        let bytes = format!("{line}\r\n");
        tokio::time::timeout(timeout, io.get_mut().write_all(bytes.as_bytes()))
            .await
            .map_err(|_| "TimeoutError: timed out".to_owned())?
            .map_err(|e| format!("SMTPServerDisconnected: {}", e.kind()))
    }

    /// `getreply()`: the code and the lines joined by `\n`; `-1` for a code that is not one.
    async fn reply(&mut self) -> Result<(i64, String), String> {
        let timeout = self.timeout;
        let io = self.io()?;
        let mut lines: Vec<String> = Vec::new();
        loop {
            let mut raw = Vec::new();
            let n = tokio::time::timeout(timeout, io.read_until(b'\n', &mut raw))
                .await
                .map_err(|_| "TimeoutError: timed out".to_owned())?
                .map_err(|e| format!("SMTPServerDisconnected: {}", e.kind()))?;
            if n == 0 {
                return Err("SMTPServerDisconnected: Connection unexpectedly closed".to_owned());
            }
            let line = String::from_utf8_lossy(&raw).into_owned();
            let code = line
                .get(..3)
                .and_then(|c| c.parse::<i64>().ok())
                .unwrap_or(-1);
            lines.push(
                line.get(4..)
                    .unwrap_or("")
                    .trim_matches([' ', '\t', '\r', '\n'])
                    .to_owned(),
            );
            if line.as_bytes().get(3) != Some(&b'-') || code == -1 {
                return Ok((code, lines.join("\n")));
            }
        }
    }

    async fn docmd(&mut self, line: &str) -> Result<(i64, String), String> {
        self.put(line).await?;
        self.reply().await
    }

    fn has(&self, feature: &str) -> Option<&str> {
        self.features
            .iter()
            .find(|(k, _)| k == feature)
            .map(|(_, v)| v.as_str())
    }

    /// `ehlo_or_helo_if_needed()`, with `ehlo()`'s feature parsing.
    async fn hello(&mut self, name: &str) -> Result<(), String> {
        if self.ehlo_done {
            return Ok(());
        }
        let (code, text) = self.docmd(&format!("ehlo {name}")).await?;
        self.ehlo_done = true;
        if code == 250 {
            self.does_esmtp = true;
            for each in text.split('\n').skip(1) {
                let lower = each.to_ascii_lowercase();
                if let Some(rest) = lower.strip_prefix("auth=") {
                    let prior = self.has("auth").unwrap_or("").to_owned();
                    let value = format!("{prior} {}", &each[each.len() - rest.len()..]);
                    self.set_feature("auth", value);
                }
                let feature: String = each
                    .chars()
                    .take_while(|c| c.is_ascii_alphanumeric() || *c == '-')
                    .collect();
                if feature.is_empty() || feature.starts_with('-') {
                    continue;
                }
                let params = each[feature.len()..].trim().to_owned();
                let key = feature.to_ascii_lowercase();
                if key == "auth" {
                    let prior = self.has("auth").unwrap_or("").to_owned();
                    self.set_feature("auth", format!("{prior} {params}"));
                } else {
                    self.set_feature(&key, params);
                }
            }
            return Ok(());
        }
        if (200..=299).contains(&code) {
            return Ok(());
        }
        let (code, text) = self.docmd(&format!("helo {name}")).await?;
        if !(200..=299).contains(&code) {
            return Err(smtp_error("SMTPHeloError", code, &text));
        }
        Ok(())
    }

    fn set_feature(&mut self, key: &str, value: String) {
        if let Some(slot) = self.features.iter_mut().find(|(k, _)| k == key) {
            slot.1 = value;
        } else {
            self.features.push((key.to_owned(), value));
        }
    }

    fn reset_hello(&mut self) {
        self.ehlo_done = false;
        self.does_esmtp = false;
        self.features.clear();
    }

    /// `auth(mechanism, authobject)`.
    async fn auth(&mut self, mechanism: &str, user: &str, password: &str) -> Result<bool, String> {
        let first = match mechanism {
            "PLAIN" => Some(format!("\0{user}\0{password}")),
            "LOGIN" => Some(user.to_owned()),
            _ => None,
        };
        let b64 = |s: &str| base64::engine::general_purpose::STANDARD.encode(s.as_bytes());
        let (mut code, mut text) = match &first {
            Some(initial) => {
                self.auth_challenges = 1;
                self.docmd(&format!("AUTH {mechanism} {}", b64(initial)))
                    .await?
            }
            None => {
                self.auth_challenges = 0;
                self.docmd(&format!("AUTH {mechanism}")).await?
            }
        };
        while code == 334 {
            self.auth_challenges += 1;
            let challenge = base64::engine::general_purpose::STANDARD
                .decode(text.trim().as_bytes())
                .unwrap_or_default();
            let answer = match mechanism {
                "CRAM-MD5" => format!("{user} {}", hmac_md5_hex(password.as_bytes(), &challenge)),
                "PLAIN" => format!("\0{user}\0{password}"),
                _ if self.auth_challenges < 2 => user.to_owned(),
                _ => password.to_owned(),
            };
            let (c, t) = self.docmd(&b64(&answer)).await?;
            code = c;
            text = t;
            if self.auth_challenges > 5 {
                return Err("SMTPException: Server AUTH mechanism infinite loop".to_owned());
            }
        }
        Ok(code == 235 || code == 503)
    }

    /// `login(user, password)`: CRAM-MD5, PLAIN, LOGIN — the first the relay advertises that
    /// works.
    async fn login(&mut self, name: &str, user: &str, password: &str) -> Result<(), String> {
        self.hello(name).await?;
        let Some(advertised) = self.has("auth").map(str::to_owned) else {
            return Err(
                "SMTPNotSupportedError: SMTP AUTH extension not supported by server.".to_owned(),
            );
        };
        let offered: Vec<String> = advertised
            .split_whitespace()
            .map(str::to_ascii_uppercase)
            .collect();
        let methods: Vec<&str> = ["CRAM-MD5", "PLAIN", "LOGIN"]
            .into_iter()
            .filter(|m| offered.iter().any(|o| o == m))
            .collect();
        if methods.is_empty() {
            return Err("SMTPException: No suitable authentication method found.".to_owned());
        }
        for method in methods {
            if self.auth(method, user, password).await? {
                return Ok(());
            }
        }
        Err("SMTPAuthenticationError: authentication refused".to_owned())
    }
}

/// HMAC-MD5 as `hmac.HMAC(key, msg, 'md5').hexdigest()` (RFC 2104 over `md-5`).
#[must_use]
pub fn hmac_md5_hex(key: &[u8], message: &[u8]) -> String {
    use md5::{Digest, Md5};
    let mut block = [0_u8; 64];
    if key.len() > 64 {
        let digest = Md5::digest(key);
        block[..16].copy_from_slice(&digest);
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Md5::new();
    inner.update(block.map(|b| b ^ 0x36));
    inner.update(message);
    let mut outer = Md5::new();
    outer.update(block.map(|b| b ^ 0x5c));
    outer.update(inner.finalize());
    outer
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// A header value: ASCII as is, otherwise RFC 2047 base64 (`=?utf-8?b?...?=`) — decoded, the
/// same text Python's default policy writes.
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

/// `EmailMessage()` with From/To/Subject and `set_content(text)`: the content-transfer
/// encoding `_encode_text` chooses for short lines (`7bit` for ASCII, `8bit` otherwise), base64
/// for a line over 78 characters. Lines end CRLF.
#[must_use]
pub fn email_message(sender: &str, to: &str, subject: &str, text: &str) -> Vec<u8> {
    let lines: Vec<&str> = text.lines().collect();
    let longest = lines.iter().map(|l| l.len()).max().unwrap_or(0);
    let mut body = String::new();
    for l in &lines {
        body.push_str(l);
        body.push('\n');
    }
    let (cte, encoded) = if longest <= 78 {
        let cte = if body.is_ascii() { "7bit" } else { "8bit" };
        (cte, body.split('\n').collect::<Vec<_>>().join("\r\n"))
    } else {
        let raw = base64::engine::general_purpose::STANDARD.encode(body.as_bytes());
        let mut out = String::new();
        for chunk in raw.as_bytes().chunks(76) {
            out.push_str(&String::from_utf8_lossy(chunk));
            out.push_str("\r\n");
        }
        ("base64", out)
    };
    let mut out = String::new();
    out.push_str(&format!("From: {}\r\n", header_value(sender)));
    out.push_str(&format!("To: {}\r\n", header_value(to)));
    out.push_str(&format!("Subject: {}\r\n", header_value(subject)));
    out.push_str("Content-Type: text/plain; charset=\"utf-8\"\r\n");
    out.push_str(&format!("Content-Transfer-Encoding: {cte}\r\n"));
    out.push_str("MIME-Version: 1.0\r\n\r\n");
    out.push_str(&encoded);
    if !out.ends_with("\r\n") {
        out.push_str("\r\n");
    }
    out.into_bytes()
}

/// `email.utils.getaddresses([value])[0][1]` for the shapes a setting holds.
fn address_of(value: &str) -> String {
    match (value.rfind('<'), value.rfind('>')) {
        (Some(open), Some(close)) if open < close => value[open + 1..close].trim().to_owned(),
        _ => value.trim().to_owned(),
    }
}

/// `EmailSender`: one message through `DESK_SMTP_HOST`. Answers whether the relay accepted it.
pub struct EmailSender {
    /// `DESK_SMTP_HOST`.
    pub host: String,
    /// `DESK_SMTP_PORT`.
    pub port: i64,
    /// `DESK_SMTP_USER` (empty: no login).
    pub user: String,
    password: String,
    /// `DESK_SMTP_STARTTLS`.
    pub starttls: bool,
    /// `DESK_NOTIFY_FROM`.
    pub sender: String,
    /// `DESK_NOTIFY_TO`.
    pub to: String,
    /// `timeout`.
    pub timeout: Duration,
    smtp: Arc<dyn SmtpConnect>,
}

impl std::fmt::Debug for EmailSender {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("EmailSender")
            .field("host", &self.host)
            .field("port", &self.port)
            .field("starttls", &self.starttls)
            .finish_non_exhaustive()
    }
}

impl EmailSender {
    /// Python's constructor: the relay's knobs, `timeout=SEND_TIMEOUT_SECONDS`, `smtp` the
    /// transport.
    #[allow(clippy::too_many_arguments)]
    #[must_use]
    pub fn new(
        host: &str,
        port: i64,
        user: &str,
        password: &str,
        starttls: bool,
        sender: &str,
        to: &str,
        smtp: Arc<dyn SmtpConnect>,
    ) -> Self {
        Self {
            host: host.to_owned(),
            port,
            user: user.to_owned(),
            password: password.to_owned(),
            starttls,
            sender: sender.to_owned(),
            to: to.to_owned(),
            timeout: Duration::from_secs_f64(SEND_TIMEOUT_SECONDS),
            smtp,
        }
    }

    async fn deliver(&self, subject: &str, text: &str) -> Result<(), String> {
        let port = u16::try_from(self.port)
            .map_err(|_| "OverflowError: getsockaddrarg: port must be 0-65535.".to_owned())?;
        let stream = self.smtp.connect(&self.host, port, self.timeout).await?;
        let mut s = Session {
            io: Some(BufReader::new(stream)),
            timeout: self.timeout,
            ehlo_done: false,
            does_esmtp: false,
            features: Vec::new(),
            auth_challenges: 0,
        };
        let (code, text_reply) = s.reply().await?;
        if code != 220 {
            return Err(smtp_error("SMTPConnectError", code, &text_reply));
        }
        let name = local_hostname();
        let body = async {
            if self.starttls {
                s.hello(&name).await?;
                if s.has("starttls").is_none() {
                    return Err(
                        "SMTPNotSupportedError: STARTTLS extension not supported by server."
                            .to_owned(),
                    );
                }
                let (code, reply) = s.docmd("STARTTLS").await?;
                if code != 220 {
                    return Err(smtp_error("SMTPResponseException", code, &reply));
                }
                let plain =
                    s.io.take()
                        .ok_or_else(|| "SMTPServerDisconnected: closed".to_owned())?
                        .into_inner();
                let secured = self.smtp.tls(plain, &self.host).await?;
                s.io = Some(BufReader::new(secured));
                s.reset_hello();
            }
            if !self.user.is_empty() {
                s.login(&name, &self.user, &self.password).await?;
            }
            let message = email_message(&self.sender, &self.to, subject, text);
            s.hello(&name).await?;
            let mut mail = format!("mail FROM:<{}>", address_of(&self.sender));
            if s.does_esmtp && s.has("size").is_some() {
                mail.push_str(&format!(" size={}", message.len()));
            }
            let (code, reply) = s.docmd(&mail).await?;
            if code != 250 {
                let _ = s.docmd("rset").await;
                return Err(smtp_error("SMTPSenderRefused", code, &reply));
            }
            let (code, reply) = s
                .docmd(&format!("rcpt TO:<{}>", address_of(&self.to)))
                .await?;
            if code != 250 && code != 251 {
                let _ = s.docmd("rset").await;
                return Err(smtp_error("SMTPRecipientsRefused", code, &reply));
            }
            let (code, reply) = s.docmd("data").await?;
            if code != 354 {
                return Err(smtp_error("SMTPDataError", code, &reply));
            }
            let mut data: Vec<u8> = Vec::new();
            for line in message.split_inclusive(|b| *b == b'\n') {
                if line.first() == Some(&b'.') {
                    data.push(b'.');
                }
                data.extend_from_slice(line);
            }
            if !data.ends_with(b"\r\n") {
                data.extend_from_slice(b"\r\n");
            }
            data.extend_from_slice(b".\r\n");
            let timeout = s.timeout;
            let io = s.io()?;
            tokio::time::timeout(timeout, io.get_mut().write_all(&data))
                .await
                .map_err(|_| "TimeoutError: timed out".to_owned())?
                .map_err(|e| format!("SMTPServerDisconnected: {}", e.kind()))?;
            let (code, reply) = s.reply().await?;
            if code != 250 {
                return Err(smtp_error("SMTPDataError", code, &reply));
            }
            Ok(())
        }
        .await;
        // `__exit__`: QUIT, and a reply other than 221 is an SMTPResponseException; a server
        // that already hung up is not.
        let quit = match s.docmd("QUIT").await {
            Ok((221, _)) => Ok(()),
            Ok((code, reply)) => Err(smtp_error("SMTPResponseException", code, &reply)),
            Err(e) if e.starts_with("SMTPServerDisconnected") => Ok(()),
            Err(e) => Err(e),
        };
        body.and(quit)
    }
}

/// `socket.getfqdn()`, as far as the process can say without a resolver: `HOSTNAME`, else
/// `localhost`. Only the relay sees it (the `ehlo` argument).
fn local_hostname() -> String {
    std::env::var("HOSTNAME")
        .ok()
        .filter(|h| !h.is_empty() && h.is_ascii() && !h.contains(char::is_whitespace))
        .unwrap_or_else(|| "localhost".to_owned())
}

#[async_trait]
impl Sender for EmailSender {
    fn name(&self) -> &str {
        "email"
    }

    async fn send(&self, subject: &str, text: &str) -> Result<bool, String> {
        match self.deliver(subject, text).await {
            Ok(()) => Ok(true),
            Err(e) => {
                warn(&format!("swing email not sent ({subject}): {e}"));
                Ok(false)
            }
        }
    }
}

// =============================================================================================
// Telegram: one sendMessage, nothing that reads
// =============================================================================================

/// What `urllib.request.urlopen` is to the Telegram sender (`opener=` in Python): one POST of a
/// form body; `Ok` when the server accepted it. The error never carries the URL.
#[async_trait]
pub trait FormPost: Send + Sync {
    /// POST `body` (`application/x-www-form-urlencoded`) to `url` within `timeout`.
    ///
    /// # Errors
    /// A transport failure or a refusing status, in words that never include `url`.
    async fn post(&self, url: &str, body: &[u8], timeout: Duration) -> Result<(), String>;
}

/// The real network, through `reqwest` (rustls).
#[derive(Debug, Default, Clone, Copy)]
pub struct HttpsPost;

#[async_trait]
impl FormPost for HttpsPost {
    async fn post(&self, url: &str, body: &[u8], timeout: Duration) -> Result<(), String> {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .map_err(|_| "OSError: the HTTP client could not be built".to_owned())?;
        // Error words are classified, never `Display`ed: reqwest's message names the URL, and
        // the URL holds the bot token.
        let answer = client
            .post(url)
            .header("Content-Type", "application/x-www-form-urlencoded")
            .body(body.to_vec())
            .send()
            .await
            .map_err(|e| {
                if e.is_timeout() {
                    "TimeoutError: timed out".to_owned()
                } else if e.is_connect() {
                    "URLError: connection failed".to_owned()
                } else {
                    "URLError: request failed".to_owned()
                }
            })?;
        let status = answer.status();
        if status.is_client_error() || status.is_server_error() {
            return Err(format!(
                "HTTPError: HTTP Error {}: {}",
                status.as_u16(),
                status.canonical_reason().unwrap_or("")
            ));
        }
        Ok(())
    }
}

/// `TelegramSender`: one `sendMessage` to one chat. One-way: no method that reads.
pub struct TelegramSender {
    url: String,
    /// The chat.
    pub chat_id: String,
    /// `timeout`.
    pub timeout: Duration,
    opener: Arc<dyn FormPost>,
}

impl std::fmt::Debug for TelegramSender {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TelegramSender")
            .field("chat_id", &self.chat_id)
            .finish_non_exhaustive()
    }
}

/// `urllib.parse.urlencode` for string pairs (`quote_plus`, `safe=''`).
#[must_use]
pub fn urlencode(pairs: &[(&str, &str)]) -> String {
    let quote = |s: &str| {
        let mut out = String::new();
        for b in s.as_bytes() {
            match b {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                    out.push(char::from(*b));
                }
                b' ' => out.push('+'),
                other => out.push_str(&format!("%{other:02X}")),
            }
        }
        out
    };
    pairs
        .iter()
        .map(|(k, v)| format!("{}={}", quote(k), quote(v)))
        .collect::<Vec<_>>()
        .join("&")
}

impl TelegramSender {
    /// Both the token and the chat id, or nothing (Python's `ValueError`).
    ///
    /// # Errors
    /// One without the other.
    pub fn new(token: &str, chat_id: &str, opener: Arc<dyn FormPost>) -> Result<Self, String> {
        if token.is_empty() || chat_id.is_empty() {
            return Err(
                "ValueError: a Telegram sender needs both a bot token and a chat id".to_owned(),
            );
        }
        Ok(Self {
            url: TELEGRAM_SEND_URL
                .split("{token}")
                .collect::<Vec<_>>()
                .join(token),
            chat_id: chat_id.to_owned(),
            timeout: Duration::from_secs_f64(SEND_TIMEOUT_SECONDS),
            opener,
        })
    }

    /// The `sendMessage` URL (it carries the token: never log it).
    #[must_use]
    pub fn url(&self) -> &str {
        &self.url
    }
}

#[async_trait]
impl Sender for TelegramSender {
    fn name(&self) -> &str {
        "telegram"
    }

    async fn send(&self, subject: &str, text: &str) -> Result<bool, String> {
        let body = urlencode(&[
            ("chat_id", self.chat_id.as_str()),
            ("text", &format!("{subject}\n{text}")),
        ]);
        match self
            .opener
            .post(&self.url, body.as_bytes(), self.timeout)
            .await
        {
            Ok(()) => Ok(true),
            Err(e) => {
                warn(&format!("swing telegram not sent ({subject}): {e}"));
                Ok(false)
            }
        }
    }
}

/// `build_senders`: the channels the settings configure — possibly none. Email needs a host, a
/// from and a to; Telegram needs BOTH the token and the chat id, and one without the other
/// builds nothing.
#[must_use]
pub fn build_senders(settings: &Settings) -> Vec<Box<dyn Sender>> {
    build_senders_over(settings, Arc::new(TcpSmtp), Arc::new(HttpsPost))
}

/// [`build_senders`] over given transports (tests, the harness).
#[must_use]
pub fn build_senders_over(
    settings: &Settings,
    smtp: Arc<dyn SmtpConnect>,
    https: Arc<dyn FormPost>,
) -> Vec<Box<dyn Sender>> {
    let mut senders: Vec<Box<dyn Sender>> = Vec::new();
    if !settings.desk_smtp_host.is_empty()
        && !settings.desk_notify_from.is_empty()
        && !settings.desk_notify_to.is_empty()
    {
        senders.push(Box::new(EmailSender::new(
            &settings.desk_smtp_host,
            settings.desk_smtp_port,
            &settings.desk_smtp_user,
            &settings.desk_smtp_password,
            settings.desk_smtp_starttls,
            &settings.desk_notify_from,
            &settings.desk_notify_to,
            smtp,
        )));
    }
    if !settings.swing_telegram_bot_token.is_empty()
        && !settings.swing_telegram_chat_id.is_empty()
        && let Ok(t) = TelegramSender::new(
            &settings.swing_telegram_bot_token,
            &settings.swing_telegram_chat_id,
            https,
        )
    {
        senders.push(Box::new(t));
    }
    senders
}

/// The `(channel, outcome)` sink a notifier counts through (`observe`).
pub type ObserveSink = Box<dyn Fn(&str, &str) -> Result<(), String> + Send + Sync>;

/// `Notifier`: fan one notice out to every configured channel. Never raises.
pub struct Notifier {
    senders: Vec<Box<dyn Sender>>,
    /// `(channel, subject, delivered)` per attempt.
    pub sent: Vec<(String, String, bool)>,
    observe: Option<ObserveSink>,
}

impl Notifier {
    /// Over `senders`, with an optional `(channel, outcome)` sink. With no channel it says so
    /// once.
    #[must_use]
    pub fn new(senders: Vec<Box<dyn Sender>>, observe: Option<ObserveSink>) -> Self {
        if senders.is_empty() {
            crate::lanes::swing::swing_monitor::emit(
                "INFO swing_notify swing notifier: no channel configured (DESK_SMTP_HOST / \
                 Telegram); signals are the row, the page and this log",
            );
        }
        Self {
            senders,
            sent: Vec::new(),
            observe,
        }
    }

    /// `channels`.
    #[must_use]
    pub fn channels(&self) -> Vec<String> {
        self.senders.iter().map(|s| s.name().to_owned()).collect()
    }

    /// `notify`: send to every channel; how many accepted it. A channel that fails is counted
    /// as not sent; a sink that fails is dropped for that notice.
    pub async fn notify(&mut self, notice: &SignalNotice) -> usize {
        let (subject, text) = (notice.subject(), notice.text());
        let mut delivered = 0;
        for sender in &self.senders {
            let ok = match sender.send(&subject, &text).await {
                Ok(ok) => ok,
                Err(e) => {
                    warn(&format!("swing notifier {} raised: {e}", sender.name()));
                    false
                }
            };
            self.sent
                .push((sender.name().to_owned(), subject.clone(), ok));
            delivered += usize::from(ok);
            if let Some(observe) = &self.observe {
                let _ = observe(sender.name(), if ok { "sent" } else { "failed" });
            }
        }
        crate::lanes::swing::swing_monitor::emit(&format!(
            "INFO swing_notify swing notice {subject}: {delivered} of {} channels",
            self.senders.len()
        ));
        delivered
    }
}
