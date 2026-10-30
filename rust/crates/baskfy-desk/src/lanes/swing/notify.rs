//! Ports `app/swing_notify.py` (SW11, STANDING-ANSWERS A2): a TRIGGERED signal for a
//! daily-focus name, told once, one way. Never a confirm path — a reply or a tap must not place
//! an order. This module composes a message and hands it to a [`Sender`]; it imports nothing
//! from the execution side, names no route that acts, and reads nothing back.
//!
//! **Transports (DECISIONS-RS R8.7).** The notice, its text, its JSON line, the fan-out and the
//! fail-soft rules are ported. The two concrete transports (`smtplib` and Telegram's
//! `sendMessage`) are not: they need an SMTP client this workspace does not carry, and the Rust
//! desk is harness-only until cutover. [`build_senders`] still reads the same settings and
//! builds the same two senders; each answers "not sent" and logs why, which is the Python
//! senders' own failure answer. Wiring a transport is a cutover item.

use chrono::{DateTime, FixedOffset};
use rust_decimal::Decimal;

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
pub trait Sender: Send + Sync {
    /// `name`.
    fn name(&self) -> &str;
    /// `send(subject, text)`.
    ///
    /// # Errors
    /// A channel may fail by raising; [`Notifier::notify`] counts that as not sent.
    fn send(&self, subject: &str, text: &str) -> Result<bool, String>;
}

/// `EmailSender`'s configuration (`DESK_SMTP_*`). The transport is a cutover item.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmailSender {
    /// `DESK_SMTP_HOST`.
    pub host: String,
    /// `DESK_SMTP_PORT`.
    pub port: i64,
    /// `DESK_SMTP_STARTTLS`.
    pub starttls: bool,
    /// `DESK_NOTIFY_FROM`.
    pub sender: String,
    /// `DESK_NOTIFY_TO`.
    pub to: String,
    has_login: bool,
}

impl Sender for EmailSender {
    fn name(&self) -> &str {
        "email"
    }

    fn send(&self, subject: &str, _text: &str) -> Result<bool, String> {
        let _ = (self.port, self.starttls, self.has_login);
        tracing_warn(&format!(
            "swing email not sent ({subject}): the Rust desk has no SMTP transport yet \
             (a cutover item, DECISIONS-RS R8.7)"
        ));
        Ok(false)
    }
}

/// `TelegramSender`: one `sendMessage` to one chat. One-way: no method that reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TelegramSender {
    /// The chat.
    pub chat_id: String,
}

impl TelegramSender {
    /// Both the token and the chat id, or nothing (Python's `ValueError`).
    ///
    /// # Errors
    /// One without the other.
    pub fn new(token: &str, chat_id: &str) -> Result<Self, String> {
        if token.is_empty() || chat_id.is_empty() {
            return Err("a Telegram sender needs both a bot token and a chat id".to_owned());
        }
        Ok(Self {
            chat_id: chat_id.to_owned(),
        })
    }
}

impl Sender for TelegramSender {
    fn name(&self) -> &str {
        "telegram"
    }

    fn send(&self, subject: &str, _text: &str) -> Result<bool, String> {
        tracing_warn(&format!(
            "swing telegram not sent ({subject}): the Rust desk has no Telegram transport yet \
             (a cutover item, DECISIONS-RS R8.7)"
        ));
        Ok(false)
    }
}

fn tracing_warn(message: &str) {
    crate::lanes::swing::swing_monitor::emit(&format!("WARNING swing_notify {message}"));
}

/// `build_senders`: the channels the settings configure — possibly none. Telegram needs BOTH
/// the token and the chat id; one without the other builds nothing.
#[must_use]
pub fn build_senders(settings: &Settings) -> Vec<Box<dyn Sender>> {
    let mut senders: Vec<Box<dyn Sender>> = Vec::new();
    if !settings.desk_smtp_host.is_empty()
        && !settings.desk_notify_from.is_empty()
        && !settings.desk_notify_to.is_empty()
    {
        senders.push(Box::new(EmailSender {
            host: settings.desk_smtp_host.clone(),
            port: settings.desk_smtp_port,
            starttls: settings.desk_smtp_starttls,
            sender: settings.desk_notify_from.clone(),
            to: settings.desk_notify_to.clone(),
            has_login: !settings.desk_smtp_user.is_empty(),
        }));
    }
    if !settings.swing_telegram_bot_token.is_empty()
        && !settings.swing_telegram_chat_id.is_empty()
        && let Ok(t) = TelegramSender::new(
            &settings.swing_telegram_bot_token,
            &settings.swing_telegram_chat_id,
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
    /// Over `senders`, with an optional `(channel, outcome)` sink.
    #[must_use]
    pub fn new(senders: Vec<Box<dyn Sender>>, observe: Option<ObserveSink>) -> Self {
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
    pub fn notify(&mut self, notice: &SignalNotice) -> usize {
        let (subject, text) = (notice.subject(), notice.text());
        let mut delivered = 0;
        for sender in &self.senders {
            let ok = match sender.send(&subject, &text) {
                Ok(ok) => ok,
                Err(e) => {
                    tracing_warn(&format!("swing notifier {} raised: {e}", sender.name()));
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
