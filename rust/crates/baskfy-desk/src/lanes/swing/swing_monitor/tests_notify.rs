//! `tests/test_swing_notify.py`, whole: the email carries the whole line, a skip says why, a
//! failing relay is `false`, Telegram is built only with both knobs and is one POST that fails
//! soft, the process default is no channel, the module names no confirm path, and the notifier
//! never stops the monitor (a raising channel, a `false` channel, only the daily focus, a
//! skipped focus trigger told with its reason, a raising metrics sink). The SMTP dialogue is
//! graded against `smtplib`'s own command spellings over an in-memory relay (no socket), and
//! against Python's in `desk_diff.py --plans` through the harness's mail sink.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use baskfy_core::swing::opening_range::TriggerState;
use chrono::{DateTime, FixedOffset, TimeZone};
use rust_decimal::Decimal;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

use super::notify::{
    EmailSender, FormPost, Notifier, Sender, SignalNotice, SmtpConnect, TelegramSender, Wire,
    build_senders, build_senders_over, email_message, hmac_md5_hex, notice_json, urlencode,
};
use super::tests_store::{Book, day, flags, name, new_lines, signal_of, store_over};
use crate::clock::ist;
use crate::settings::Settings;

fn d(s: &str) -> Decimal {
    s.parse().unwrap()
}

/// `AT`: 09:31 on `DAY`, aware.
fn at() -> DateTime<FixedOffset> {
    ist()
        .from_local_datetime(&day().and_hms_opt(9, 31, 0).unwrap())
        .single()
        .unwrap()
}

/// `_notice(**over)`.
fn notice() -> SignalNotice {
    SignalNotice {
        symbol: "ALPHAFLAG".to_owned(),
        setup: "FLAG".to_owned(),
        at: at(),
        entry: Some(d("100.80")),
        stop: Some(d("97.80")),
        quantity: Some(1666),
        risk_inr: Some(d("4998.00")),
        plan_expires_at: Some(at() + chrono::Duration::minutes(30)),
        skipped: None,
        mode: "DRY_RUN".to_owned(),
    }
}

// --- an in-memory relay ------------------------------------------------------------------

/// What the fake relay saw: the commands in order (AUTH payloads decoded), and the message.
#[derive(Default)]
struct Relay {
    commands: Mutex<Vec<String>>,
    data: Mutex<Vec<u8>>,
    tls: Mutex<Vec<String>>,
}

/// `smtplib.SMTP` as the test's relay: a scripted ESMTP server on an in-memory pipe.
struct FakeSmtp {
    relay: Arc<Relay>,
    refuse_connect: bool,
    /// The EHLO reply's extension lines.
    extensions: Vec<&'static str>,
}

impl FakeSmtp {
    fn new(extensions: Vec<&'static str>) -> Self {
        Self {
            relay: Arc::new(Relay::default()),
            refuse_connect: false,
            extensions,
        }
    }
}

async fn serve(stream: tokio::io::DuplexStream, relay: Arc<Relay>, extensions: Vec<&'static str>) {
    let (read, mut write) = tokio::io::split(stream);
    let mut reader = BufReader::new(read);
    write.write_all(b"220 fake ESMTP\r\n").await.unwrap();
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).await.unwrap() == 0 {
            return;
        }
        let cmd = line.trim_end().to_owned();
        relay.commands.lock().unwrap().push(cmd.clone());
        let lower = cmd.to_ascii_lowercase();
        let reply: String = if lower.starts_with("ehlo") {
            let mut r = String::from("250-fake\r\n");
            for (i, e) in extensions.iter().enumerate() {
                let sep = if i + 1 == extensions.len() { ' ' } else { '-' };
                r.push_str(&format!("250{sep}{e}\r\n"));
            }
            if extensions.is_empty() {
                r = "250 fake\r\n".to_owned();
            }
            r
        } else if lower == "starttls" {
            "220 go ahead\r\n".to_owned()
        } else if lower.starts_with("auth plain") {
            "235 ok\r\n".to_owned()
        } else if lower.starts_with("mail from:") || lower.starts_with("rcpt to:") {
            "250 ok\r\n".to_owned()
        } else if lower == "data" {
            write.write_all(b"354 go\r\n").await.unwrap();
            let mut data = Vec::new();
            loop {
                let mut chunk = Vec::new();
                reader.read_until(b'\n', &mut chunk).await.unwrap();
                if chunk == b".\r\n" || chunk.is_empty() {
                    break;
                }
                data.extend_from_slice(&chunk);
            }
            *relay.data.lock().unwrap() = data;
            "250 queued\r\n".to_owned()
        } else if lower == "quit" {
            write.write_all(b"221 bye\r\n").await.unwrap();
            return;
        } else {
            "502 no\r\n".to_owned()
        };
        write.write_all(reply.as_bytes()).await.unwrap();
    }
}

#[async_trait]
impl SmtpConnect for FakeSmtp {
    async fn connect(
        &self,
        host: &str,
        port: u16,
        timeout: Duration,
    ) -> Result<Box<dyn Wire>, String> {
        self.relay.commands.lock().unwrap().push(format!(
            "connect {host}:{port} timeout={}",
            timeout.as_secs_f64()
        ));
        if self.refuse_connect {
            return Err("OSError: relay down".to_owned());
        }
        let (client, server) = tokio::io::duplex(1 << 16);
        tokio::spawn(serve(server, self.relay.clone(), self.extensions.clone()));
        Ok(Box::new(client))
    }

    async fn tls(&self, stream: Box<dyn Wire>, host: &str) -> Result<Box<dyn Wire>, String> {
        // The pipe stays plain; what is asserted is that the upgrade was asked for, for whom.
        self.relay.tls.lock().unwrap().push(host.to_owned());
        Ok(stream)
    }
}

fn email(smtp: Arc<FakeSmtp>, user: &str, starttls: bool) -> EmailSender {
    EmailSender::new(
        "relay", 587, user, "p", starttls, "desk@x", "maulik@x", smtp,
    )
}

// =============================================================================================
// TestTheEmail
// =============================================================================================

#[tokio::test]
async fn test_email_signal_carries_the_whole_line() {
    let smtp = Arc::new(FakeSmtp::new(vec![
        "STARTTLS",
        "AUTH PLAIN LOGIN",
        "8BITMIME",
    ]));
    let sender = email(smtp.clone(), "u", true);
    let n = notice();
    assert!(sender.send(&n.subject(), &n.text()).await.unwrap());
    let raw = String::from_utf8(smtp.relay.data.lock().unwrap().clone()).unwrap();
    assert!(
        raw.contains("To: maulik@x\r\n") && raw.contains("From: desk@x\r\n"),
        "{raw}"
    );
    let body = raw.split("\r\n\r\n").nth(1).unwrap();
    for needle in [
        "100.80",
        "97.80",
        "1666",
        "₹4,998.00",
        "expires 10:01",
        "DRY_RUN",
    ] {
        assert!(body.contains(needle), "{needle} missing from the email");
    }
    // The subject (non-ASCII: the em dash) decodes to the notice's.
    let subject = raw
        .lines()
        .find(|l| l.starts_with("Subject: "))
        .unwrap()
        .trim_start_matches("Subject: =?utf-8?b?")
        .trim_end_matches("?=");
    use base64::Engine as _;
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(subject)
        .unwrap();
    let subject = String::from_utf8(decoded).unwrap();
    assert!(subject.contains("ALPHAFLAG") && subject.contains("09:31"));
    // `connect relay:587 timeout=5.0`, `starttls`, `login u` — smtplib's spellings.
    let commands = smtp.relay.commands.lock().unwrap().clone();
    assert_eq!(commands[0], "connect relay:587 timeout=5");
    let host = std::env::var("HOSTNAME")
        .ok()
        .filter(|h| !h.is_empty() && h.is_ascii() && !h.contains(char::is_whitespace))
        .unwrap_or_else(|| "localhost".to_owned());
    assert_eq!(commands[1], format!("ehlo {host}"));
    assert_eq!(commands[2], "STARTTLS");
    assert_eq!(*smtp.relay.tls.lock().unwrap(), vec!["relay".to_owned()]);
    assert_eq!(
        commands[3],
        format!("ehlo {host}"),
        "the session is re-introduced under TLS"
    );
    assert!(commands[4].starts_with("AUTH PLAIN "), "{}", commands[4]);
    assert_eq!(commands[5], "mail FROM:<desk@x>");
    assert_eq!(commands[6], "rcpt TO:<maulik@x>");
    assert_eq!(commands[7], "data");
    assert_eq!(commands[8], "QUIT");
}

#[test]
fn test_email_signal_for_a_skipped_trigger_says_why() {
    let mut n = notice();
    n.quantity = None;
    n.risk_inr = None;
    n.plan_expires_at = None;
    n.skipped = Some("GATE_RED gate is RED".to_owned());
    assert!(n.text().contains("No line: GATE_RED gate is RED."));
    assert!(n.subject().contains("skipped (GATE_RED gate is RED)"));
    assert!(notice_json(&n).contains("\"quantity\": null"));
}

#[tokio::test]
async fn test_email_signal_failure_is_false_not_an_exception() {
    let mut refusing = FakeSmtp::new(vec![]);
    refusing.refuse_connect = true;
    let sender = EmailSender::new("relay", 587, "", "", true, "a", "b", Arc::new(refusing));
    assert!(!sender.send("s", "t").await.unwrap());
    // A relay that does not offer STARTTLS when it is asked for is refused, not skipped.
    let plain = Arc::new(FakeSmtp::new(vec!["8BITMIME"]));
    assert!(!email(plain, "", true).send("s", "t").await.unwrap());
}

#[tokio::test]
async fn an_unauthenticated_plain_relay_gets_the_message_as_python_sends_it() {
    let smtp = Arc::new(FakeSmtp::new(vec!["8BITMIME", "SIZE 1000000"]));
    let sender = email(smtp.clone(), "", false);
    assert!(
        sender
            .send("[swing] A — B", "line one\n.dot line")
            .await
            .unwrap()
    );
    let commands = smtp.relay.commands.lock().unwrap().clone();
    assert!(
        !commands
            .iter()
            .any(|c| c == "STARTTLS" || c.starts_with("AUTH"))
    );
    let expected = email_message("desk@x", "maulik@x", "[swing] A — B", "line one\n.dot line");
    assert_eq!(
        commands[2],
        format!("mail FROM:<desk@x> size={}", expected.len()),
        "SIZE is advertised: smtplib sends it"
    );
    // A line that starts with a dot is doubled on the wire and arrives whole.
    let data = smtp.relay.data.lock().unwrap().clone();
    assert!(
        String::from_utf8(data)
            .unwrap()
            .contains("\r\n..dot line\r\n")
    );
    let msg = String::from_utf8(expected).unwrap();
    assert!(msg.contains("Content-Type: text/plain; charset=\"utf-8\"\r\n"));
    assert!(msg.contains("Content-Transfer-Encoding: 7bit\r\n"));
    assert!(msg.contains("MIME-Version: 1.0\r\n"));
}

#[test]
fn the_message_encoding_follows_set_contents_heuristic() {
    let utf8 = String::from_utf8(email_message("a", "b", "s", "risk ₹1")).unwrap();
    assert!(utf8.contains("Content-Transfer-Encoding: 8bit\r\n"));
    let long = "x".repeat(90);
    let b64 = String::from_utf8(email_message("a", "b", "s", &long)).unwrap();
    assert!(b64.contains("Content-Transfer-Encoding: base64\r\n"));
    // RFC 2104's first HMAC-MD5 vector (CRAM-MD5's digest).
    assert_eq!(
        hmac_md5_hex(&[0x0b; 16], b"Hi There"),
        "9294727a3638bb1c13f48ef8158bfc9d"
    );
}

// =============================================================================================
// TestTelegramIsDark
// =============================================================================================

#[test]
fn test_telegram_sender_is_not_constructed_unless_both_are_set() {
    let mut s = Settings::defaults().unwrap();
    s.desk_smtp_host = String::new();
    s.swing_telegram_bot_token = "123:abc".to_owned();
    s.swing_telegram_chat_id = String::new();
    assert!(build_senders(&s).is_empty());
    s.swing_telegram_bot_token = String::new();
    s.swing_telegram_chat_id = "42".to_owned();
    assert!(build_senders(&s).is_empty());
    s.swing_telegram_bot_token = "123:abc".to_owned();
    let names: Vec<String> = build_senders(&s)
        .iter()
        .map(|x| x.name().to_owned())
        .collect();
    assert_eq!(names, vec!["telegram".to_owned()]);
    assert!(TelegramSender::new("", "42", Arc::new(Opened::default())).is_err());
}

#[test]
fn test_the_process_default_is_no_channel_at_all() {
    let s = Settings::defaults().unwrap();
    assert!(s.swing_telegram_bot_token.is_empty() && s.swing_telegram_chat_id.is_empty());
    assert!(s.desk_smtp_host.is_empty());
    assert!(build_senders(&s).is_empty());
}

#[derive(Default)]
struct Opened {
    seen: Mutex<Vec<(String, Vec<u8>, Duration)>>,
    fail: bool,
}

#[async_trait]
impl FormPost for Opened {
    async fn post(&self, url: &str, body: &[u8], timeout: Duration) -> Result<(), String> {
        self.seen
            .lock()
            .unwrap()
            .push((url.to_owned(), body.to_vec(), timeout));
        if self.fail {
            Err("OSError: no network".to_owned())
        } else {
            Ok(())
        }
    }
}

#[tokio::test]
async fn test_telegram_send_is_one_post_to_send_message_and_fails_soft() {
    let opener = Arc::new(Opened::default());
    let sender = TelegramSender::new("123:abc", "42", opener.clone()).unwrap();
    assert!(sender.send("subj", "body").await.unwrap());
    let seen = opener.seen.lock().unwrap().clone();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].0, "https://api.telegram.org/bot123:abc/sendMessage");
    let body = String::from_utf8(seen[0].1.clone()).unwrap();
    assert!(
        body.contains("chat_id=42") && body.contains("subj"),
        "{body}"
    );
    assert_eq!(
        body,
        urlencode(&[("chat_id", "42"), ("text", "subj\nbody")])
    );
    assert_eq!(body, "chat_id=42&text=subj%0Abody");
    assert_eq!(seen[0].2, Duration::from_secs(5));
    let failing = Arc::new(Opened {
        fail: true,
        ..Opened::default()
    });
    assert!(
        !TelegramSender::new("1:a", "2", failing)
            .unwrap()
            .send("s", "t")
            .await
            .unwrap()
    );
}

#[test]
fn test_the_notifier_module_names_no_confirm_path() {
    let code = super::tests::code_only(include_str!("../notify.rs"));
    for pattern in [
        r"(?i)execute",
        r"(?i)gateway",
        r"(?i)\bplace",
        r"(?i)confirm",
        r"(?i)\bkc\b",
        r"(?i)kiteconnect",
        r"(?i)kite_client",
        r"(?i)getUpdates",
        r"(?i)setWebhook",
        r"(?i)swing_exec",
        r"(?i)swing_desk",
        r"(?i)\border\b",
    ] {
        let re = regex::Regex::new(pattern).unwrap();
        assert!(!re.is_match(&code), "notify.rs names {pattern}");
    }
    // The one crate import is the settings (Python's `config`); nothing from the trading side.
    let crate_uses: Vec<&str> = code
        .lines()
        .filter(|l| {
            l.trim_start().starts_with("use crate::") || l.trim_start().starts_with("use super::")
        })
        .collect();
    assert_eq!(
        crate_uses,
        vec![
            "use super::rules::{money, py_dec};",
            "use crate::settings::Settings;"
        ]
    );
    for l in code.lines().filter(|l| l.contains("crate::")) {
        for word in ["exec", "gateway", "kite"] {
            assert!(!l.contains(word), "notify.rs reaches {word}: {l}");
        }
    }
}

// =============================================================================================
// TestTheNotifierNeverStopsTheMonitor
// =============================================================================================

struct RaisingSender;
#[async_trait]
impl Sender for RaisingSender {
    fn name(&self) -> &str {
        "boom"
    }
    async fn send(&self, _s: &str, _t: &str) -> Result<bool, String> {
        Err("RuntimeError: channel exploded".to_owned())
    }
}

struct FlakySender;
#[async_trait]
impl Sender for FlakySender {
    fn name(&self) -> &str {
        "flaky"
    }
    async fn send(&self, _s: &str, _t: &str) -> Result<bool, String> {
        Ok(false)
    }
}

#[derive(Clone, Default)]
struct Recording(Arc<Mutex<Vec<(String, String)>>>);
#[async_trait]
impl Sender for Recording {
    fn name(&self) -> &str {
        "rec"
    }
    async fn send(&self, s: &str, t: &str) -> Result<bool, String> {
        self.0.lock().unwrap().push((s.to_owned(), t.to_owned()));
        Ok(true)
    }
}

fn focused(symbol: &str, token: i64, focus: bool) -> crate::lanes::swing::breakout::WatchedName {
    let mut w = name(symbol, token);
    w.focus = focus;
    w
}

#[tokio::test]
async fn test_a_notifier_failure_never_stops_the_monitor() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Book::default().open(dir.path()).await;
    let mut s = store_over(conn, flags(true, false));
    s.notifier = Some(Notifier::new(vec![Box::new(RaisingSender)], None));
    use crate::lanes::swing::breakout::SignalStore as _;
    s.raise_signal(&signal_of(
        focused("AAA", 1, true),
        TriggerState::Triggered,
        Some("100.80"),
        Some("97.80"),
    ))
    .await
    .unwrap();
    assert_eq!(
        super::tests_store::rows(&mut s.conn, "SELECT * FROM public.sw_signal")
            .await
            .len(),
        1
    );
    assert_eq!(new_lines(&mut s.conn).await.len(), 1);
    assert_eq!(s.notices.len(), 1);
    assert_eq!(s.notices[0].symbol, "AAA");
    let subject = s.notices[0].subject();
    assert_eq!(
        s.notifier.as_ref().unwrap().sent,
        vec![("boom".to_owned(), subject, false)]
    );
}

#[tokio::test]
async fn test_notifier_counts_a_channel_that_answers_false_as_not_sent() {
    let rec = Recording::default();
    let mut notifier = Notifier::new(vec![Box::new(FlakySender), Box::new(rec.clone())], None);
    assert_eq!(notifier.notify(&notice()).await, 1);
    let ok: Vec<bool> = notifier.sent.iter().map(|s| s.2).collect();
    assert_eq!(ok, vec![false, true]);
    assert!(
        rec.0.lock().unwrap()[0]
            .0
            .starts_with("[swing] ALPHAFLAG triggered at 09:31")
    );
    assert_eq!(notifier.channels(), vec!["flaky", "rec"]);
}

#[tokio::test]
async fn test_only_daily_focus_names_are_notified() {
    let rec = Recording::default();
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        stats: vec![("BBB", 2, "5.00", 100_000_000, "61.00")],
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.notifier = Some(Notifier::new(vec![Box::new(rec.clone())], None));
    use crate::lanes::swing::breakout::SignalStore as _;
    s.raise_signal(&signal_of(
        focused("BBB", 2, false),
        TriggerState::Triggered,
        Some("100.80"),
        Some("97.80"),
    ))
    .await
    .unwrap();
    assert!(rec.0.lock().unwrap().is_empty() && s.notices.is_empty());
    assert_eq!(
        new_lines(&mut s.conn).await.len(),
        1,
        "the line is still written"
    );
    s.raise_signal(&signal_of(
        focused("AAA", 1, true),
        TriggerState::Triggered,
        Some("100.80"),
        Some("97.80"),
    ))
    .await
    .unwrap();
    let messages = rec.0.lock().unwrap().clone();
    assert_eq!(messages.len(), 1);
    assert!(messages[0].0.contains("AAA"));
    let body = &messages[0].1;
    assert!(
        body.contains("qty     1666")
            && body.contains("₹4,998.00")
            && body.contains("expires 10:01"),
        "{body}"
    );
}

#[tokio::test]
async fn test_a_focus_trigger_that_is_skipped_is_told_with_the_reason() {
    let rec = Recording::default();
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        gate: "RED",
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.notifier = Some(Notifier::new(vec![Box::new(rec.clone())], None));
    use crate::lanes::swing::breakout::SignalStore as _;
    s.raise_signal(&signal_of(
        focused("AAA", 1, true),
        TriggerState::Triggered,
        Some("100.80"),
        Some("97.80"),
    ))
    .await
    .unwrap();
    assert!(new_lines(&mut s.conn).await.is_empty());
    assert!(rec.0.lock().unwrap()[0].0.contains("skipped (GATE_RED"));
    assert!(s.notices[0].quantity.is_none());
}

#[tokio::test]
async fn test_notifier_observe_sink_that_raises_is_dropped() {
    let mut notifier = Notifier::new(
        vec![Box::new(Recording::default())],
        Some(Box::new(|_: &str, _: &str| {
            Err("RuntimeError: metrics down".to_owned())
        })),
    );
    assert_eq!(notifier.notify(&notice()).await, 1);
}

#[test]
fn the_desk_settings_build_an_email_sender_over_the_relay() {
    let mut s = Settings::defaults().unwrap();
    s.desk_smtp_host = "127.0.0.1".to_owned();
    s.desk_notify_from = "desk@example.invalid".to_owned();
    s.desk_notify_to = "op@example.invalid".to_owned();
    let senders = build_senders_over(
        &s,
        Arc::new(FakeSmtp::new(vec![])),
        Arc::new(Opened::default()),
    );
    assert_eq!(senders.len(), 1);
    assert_eq!(senders[0].name(), "email");
    let _ = day();
}
