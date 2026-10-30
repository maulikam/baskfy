//! SW18 — the one-tap morning Kite login: `tasks/kite_login_nudge.run_login_nudge`.
//!
//! At 08:45 (and 09:05) IST on an NSE session, when no Kite session issued **today** is stored,
//! send one message carrying a Kite login link built by the API's own builder
//! (`broker_oauth.kite_login_url` → [`baskfy_api::routes::brokers::kite_login_url_for_worker`]),
//! whose one-time `state` the API's callback accepts. Nothing here logs in, stores a password or
//! reads market data — a tap starts a *login*, and the only thing a completed login does is store
//! a token (law 2 untouched).
//!
//! * **One message per session date per window**: an `O_EXCL` marker file beside the token blob
//!   (`kite-login-nudge-<date>.<window>`), claimed **before** the send.
//! * **A holiday is not a morning**: weekends by weekday, holidays by the NSE calendar.
//! * **Fail soft, always**: every refusal is a note; this never raises.
//! * **Nothing secret is rendered or logged**: the URL's `api_key` is a public app id; no token,
//!   no Fernet key, no recipient address, no blob path.

use std::io::Write as _;
use std::path::{Path, PathBuf};

use baskfy_api::mailer::Message;
use baskfy_db::OrderedJson;
use baskfy_providers::tokens::AccessTokenStore;
use chrono::{DateTime, Datelike as _, FixedOffset, NaiveDate};

use super::alerting::{MailSink, html_escape};
use crate::pyjson::{object, s, strings};
use crate::steps::{log_error, log_warning};

/// `SUBJECT`: identical for both windows, so the two land in one thread on a phone.
pub(crate) const SUBJECT: &str = "Kite login needed before 09:15";
/// `MARKER_PREFIX`.
pub(crate) const MARKER_PREFIX: &str = "kite-login-nudge";
/// `DEFAULT_ACCOUNT`: a label — never a client id or an account number.
pub(crate) const DEFAULT_ACCOUNT: &str = "Zerodha — Baskfy's Kite Connect app";
/// `broker_oauth.SIMULATED_TOKEN_PREFIX`.
const SIMULATED_TOKEN_PREFIX: &str = baskfy_api::routes::brokers::SIMULATED_TOKEN_PREFIX;

/// `NudgeWindow`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum NudgeWindow {
    /// `first` — 08:45.
    First,
    /// `second` — 09:05, the last.
    Second,
}

impl NudgeWindow {
    /// `NudgeWindow(value)`.
    pub(crate) fn parse(value: &str) -> Option<Self> {
        match value {
            "first" => Some(Self::First),
            "second" => Some(Self::Second),
            _ => None,
        }
    }

    pub(crate) const fn value(self) -> &'static str {
        match self {
            Self::First => "first",
            Self::Second => "second",
        }
    }

    const fn is_last(self) -> bool {
        matches!(self, Self::Second)
    }

    const fn clock(self) -> &'static str {
        if self.is_last() { "09:05" } else { "08:45" }
    }
}

/// `KiteLoginUrl`: the URL and how long its `state` stays consumable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct LoginUrl {
    pub(crate) url: String,
    pub(crate) valid_for_minutes: i64,
}

/// `NudgeReport`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct NudgeReport {
    pub(crate) window: NudgeWindow,
    pub(crate) date: NaiveDate,
    pub(crate) sent: bool,
    pub(crate) notes: Vec<String>,
}

impl NudgeReport {
    fn note(&mut self, text: impl Into<String>) {
        self.notes.push(text.into());
    }

    /// `as_detail()`.
    pub(crate) fn as_detail(&self) -> OrderedJson {
        object([
            ("window", s(self.window.value())),
            ("date", s(self.date.format("%Y-%m-%d").to_string())),
            ("sent", OrderedJson::Bool(self.sent)),
            ("notes", strings(self.notes.iter().cloned())),
        ])
    }
}

/// `marker_path(state_dir, day, window)`.
pub(crate) fn marker_path(state_dir: &Path, day: NaiveDate, window: NudgeWindow) -> PathBuf {
    state_dir.join(format!(
        "{MARKER_PREFIX}-{}.{}",
        day.format("%Y-%m-%d"),
        window.value()
    ))
}

/// `_claim(state_dir, day, window)`: take the window atomically (`O_EXCL`); `false` if somebody
/// already holds it. An unwritable directory sends anyway (two mails beat none).
pub(crate) fn claim(state_dir: &Path, day: NaiveDate, window: NudgeWindow) -> bool {
    let path = marker_path(state_dir, day, window);
    let result = std::fs::create_dir_all(state_dir).and_then(|()| {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)?;
        file.write_all(format!("{} {}\n", day.format("%Y-%m-%d"), window.value()).as_bytes())
    });
    match result {
        Ok(()) => true,
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => false,
        Err(e) => {
            log_warning(&format!(
                "could not write the login-nudge marker ({}); sending anyway",
                io_class(&e)
            ));
            true
        }
    }
}

/// The `OSError` subclass Python names for an I/O failure.
fn io_class(e: &std::io::Error) -> &'static str {
    match e.kind() {
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::AlreadyExists => "FileExistsError",
        _ => "OSError",
    }
}

/// `_token_state(store, now=)`: `Ok(None)` when a usable session for today is stored; else why
/// not. A failure that is not a `ProviderError` (an `OSError` reading the blob) gets through
/// Python's `except ProviderError`, and the task raises it: `Err`.
pub(crate) fn token_state(
    store: &AccessTokenStore,
    now: DateTime<FixedOffset>,
) -> Result<Option<String>, Raised> {
    match store.require_fresh(Some(now)) {
        Err(e) if e.kind() != baskfy_providers::ErrorKind::Foreign => {
            Ok(Some("no usable Kite session is stored".to_owned()))
        }
        Err(e) => Err(Raised {
            class: e.python_type().to_owned(),
            message: e.to_string(),
        }),
        Ok(token) if token.value.expose().starts_with(SIMULATED_TOKEN_PREFIX) => Ok(Some(
            "the only stored session is a simulated one, which Kite will not accept".to_owned(),
        )),
        Ok(_) => Ok(None),
    }
}

/// `render_nudge(login, to=, day=, window=, account=, reason=)`.
pub(crate) fn render_nudge(
    login: &LoginUrl,
    to: &str,
    day: NaiveDate,
    window: NudgeWindow,
    account: &str,
    reason: &str,
) -> Message {
    let opening = if window.is_last() {
        "Second and last reminder — Baskfy's own box is asking, not Zerodha."
    } else {
        "Baskfy's own box is asking, not Zerodha."
    };
    let closing = if window.is_last() {
        "There is no further reminder today."
    } else {
        "If it has expired by the time you tap it, the 09:05 check sends a fresh one."
    };
    let lines: Vec<String> = vec![
        opening.to_owned(),
        String::new(),
        format!(
            "Sent by the Baskfy worker on your own box at the {} check. It holds no Zerodha \
             password and no 2FA seed — it cannot log in for you, which is the point. The link \
             below opens Zerodha's own login page.",
            window.clock()
        ),
        String::new(),
        format!("For {}: {reason}.", day.format("%A %d %B %Y")),
        String::new(),
        "Open this and log in:".to_owned(),
        String::new(),
        format!("  {}", login.url),
        String::new(),
        format!("Account: {account}."),
        String::new(),
        "If Baskfy asks you to sign in on the way back from Zerodha, that is the last step and \
         not a failure — the session is stored once it finishes."
            .to_owned(),
        String::new(),
        "If you ignore this: the swing monitor starts at 09:14 with no session and idles, no plan \
         line can be sent, and no order is possible today. Nothing else on the box breaks, and \
         nothing is placed on your behalf either way."
            .to_owned(),
        String::new(),
        format!(
            "The link can be used once and is good for {} minutes. {closing}",
            login.valid_for_minutes
        ),
        String::new(),
    ];
    let prose: Vec<&String> = lines
        .iter()
        .filter(|l| !l.is_empty() && !l.starts_with("  "))
        .collect();
    let para = |l: &&String| format!("<p>{}</p>", html_escape(l));
    let html = format!(
        "{}<p><a href=\"{}\">Log in to Kite on Zerodha's site</a></p>{}",
        prose.iter().take(5).map(para).collect::<String>(),
        html_escape(&login.url),
        prose.iter().skip(5).map(para).collect::<String>(),
    );
    Message {
        to: to.to_owned(),
        subject: SUBJECT.to_owned(),
        text: lines.join("\n"),
        html,
        reply_to: None,
    }
}

/// What `run_login_nudge` reads and where it sends.
pub(crate) struct NudgeInputs<'a, M: MailSink> {
    /// `now` (aware).
    pub(crate) now: DateTime<FixedOffset>,
    pub(crate) token_store: &'a AccessTokenStore,
    pub(crate) mailer: &'a M,
    /// `login_url_for(window)`: `Err(class name)` when the URL could not be built.
    pub(crate) login_url_for: &'a dyn Fn(NudgeWindow) -> Result<LoginUrl, String>,
    pub(crate) to: &'a str,
    pub(crate) state_dir: &'a Path,
    pub(crate) window: NudgeWindow,
    pub(crate) account: &'a str,
}

/// An `OSError` reading the token blob that Python lets through (`run_in_session` raises it).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Raised {
    pub(crate) class: String,
    pub(crate) message: String,
}

/// `run_login_nudge(session, outcome, now=, token_store=, mailer=, login_url_for=, to=,
/// state_dir=, window=, account=)`. `is_session` is `swing_health.is_session_day` for the day.
///
/// # Errors
/// Only a non-provider error reading the token blob, as Python.
pub(crate) async fn run_login_nudge<M: MailSink>(
    inputs: &NudgeInputs<'_, M>,
    is_session: impl std::future::Future<Output = Result<bool, crate::error::WorkerError>>,
) -> Result<Result<NudgeReport, Raised>, crate::error::WorkerError> {
    let ist = baskfy_providers::tokens::ist();
    let local = inputs.now.with_timezone(&ist);
    let day = local.date_naive();
    let window = inputs.window;
    let mut report = NudgeReport {
        window,
        date: day,
        sent: false,
        notes: Vec::new(),
    };
    if inputs.to.is_empty() {
        report.note("BASKFY_KITE_LOGIN_NUDGE_TO is not set; nothing was sent");
        return Ok(Ok(report));
    }
    if local.weekday().num_days_from_monday() >= 5 {
        report.note("not a weekday");
        return Ok(Ok(report));
    }
    if !is_session.await? {
        report.note("an NSE holiday; there is no session to log in for");
        return Ok(Ok(report));
    }
    let reason = match token_state(inputs.token_store, inputs.now) {
        Ok(None) => {
            report.note("a Kite session issued today is already stored; no message sent");
            return Ok(Ok(report));
        }
        Err(raised) => return Ok(Err(raised)),
        Ok(Some(r)) => r,
    };
    if !claim(inputs.state_dir, day, window) {
        report.note(format!(
            "the {} window has already been used today",
            window.value()
        ));
        return Ok(Ok(report));
    }
    let login = match (inputs.login_url_for)(window) {
        Ok(login) => login,
        Err(class) => {
            report.note(format!("the login URL could not be built ({class})"));
            log_error(&format!(
                "login nudge: could not build the login URL error={class}"
            ));
            return Ok(Ok(report));
        }
    };
    let message = render_nudge(&login, inputs.to, day, window, inputs.account, &reason);
    report.sent = inputs.mailer.deliver(&message).await;
    report.note(if report.sent {
        format!("{} login link sent", window.value())
    } else {
        "the mailer refused the message".to_owned()
    });
    Ok(Ok(report))
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::panic,
        clippy::field_reassign_with_default,
        clippy::assertions_on_constants
    )]
    use std::cell::RefCell;

    use baskfy_providers::secret::Secret;

    use super::*;

    const KEY: &str = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=";

    struct Sink(RefCell<Vec<Message>>, bool);
    impl MailSink for Sink {
        fn deliver<'a>(&'a self, message: &'a Message) -> super::super::alerting::BoolFuture<'a> {
            Box::pin(async move {
                self.0.borrow_mut().push(message.clone());
                self.1
            })
        }
    }

    struct Dir(PathBuf);
    impl Dir {
        fn new(tag: &str) -> Self {
            let p = std::env::temp_dir().join(format!(
                "baskfy-nudge-{tag}-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ));
            let _ = std::fs::remove_dir_all(&p);
            std::fs::create_dir_all(&p).unwrap();
            Self(p)
        }
    }
    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn at(text: &str) -> DateTime<FixedOffset> {
        DateTime::parse_from_rfc3339(&format!("{text}+05:30")).unwrap()
    }

    fn store(dir: &Dir, issued: Option<(&str, &str)>) -> AccessTokenStore {
        let s = AccessTokenStore::new(dir.0.join("kite-token.enc"), Secret::new(KEY));
        if let Some((value, when)) = issued {
            s.save(value, Some(at(when))).unwrap();
        }
        s
    }

    fn url_for(window: NudgeWindow) -> Result<LoginUrl, String> {
        Ok(LoginUrl {
            url: format!(
                "https://kite.zerodha.com/connect/login?api_key=app&v=3&redirect_params=state%3D{}",
                window.value()
            ),
            valid_for_minutes: 30,
        })
    }

    async fn nudge(
        dir: &Dir,
        store: &AccessTokenStore,
        sink: &Sink,
        now: &str,
        window: NudgeWindow,
        session: bool,
    ) -> NudgeReport {
        let inputs = NudgeInputs {
            now: at(now),
            token_store: store,
            mailer: sink,
            login_url_for: &url_for,
            to: "me@example.com",
            state_dir: &dir.0,
            window,
            account: DEFAULT_ACCOUNT,
        };
        run_login_nudge(&inputs, async move { Ok(session) })
            .await
            .unwrap()
            .unwrap()
    }

    // TestTheTokenDecidesWhetherAnyoneIsDisturbed.
    #[tokio::test]
    async fn a_token_issued_today_sends_nothing_and_yesterdays_is_stale() {
        let dir = Dir::new("today");
        let sink = Sink(RefCell::new(vec![]), true);
        let fresh = store(&dir, Some(("tok", "2026-10-30T08:00:00")));
        let r = nudge(
            &dir,
            &fresh,
            &sink,
            "2026-10-30T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        assert!(!r.sent);
        assert_eq!(
            r.notes,
            ["a Kite session issued today is already stored; no message sent"]
        );
        let stale = store(&dir, Some(("tok", "2026-10-29T23:30:00")));
        let r = nudge(
            &dir,
            &stale,
            &sink,
            "2026-10-30T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        assert!(r.sent);
        assert!(
            sink.0.borrow()[0]
                .text
                .contains("no usable Kite session is stored")
        );
    }

    #[tokio::test]
    async fn a_simulated_token_is_not_a_session() {
        let dir = Dir::new("sim");
        let sink = Sink(RefCell::new(vec![]), true);
        let s = store(&dir, Some(("sim_abc", "2026-10-30T08:00:00")));
        let r = nudge(
            &dir,
            &s,
            &sink,
            "2026-10-30T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        assert!(r.sent);
        assert!(sink.0.borrow()[0].text.contains("simulated one"));
    }

    // TestOneMessagePerMorning.
    #[tokio::test]
    async fn one_message_per_window_per_morning() {
        let dir = Dir::new("once");
        let sink = Sink(RefCell::new(vec![]), true);
        let none = store(&dir, None);
        let first = nudge(
            &dir,
            &none,
            &sink,
            "2026-10-30T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        assert_eq!(first.notes, ["first login link sent"]);
        let again = nudge(
            &dir,
            &none,
            &sink,
            "2026-10-30T08:50:00",
            NudgeWindow::First,
            true,
        )
        .await;
        assert_eq!(
            again.notes,
            ["the first window has already been used today"]
        );
        let second = nudge(
            &dir,
            &none,
            &sink,
            "2026-10-30T09:05:00",
            NudgeWindow::Second,
            true,
        )
        .await;
        assert!(second.sent);
        assert!(
            sink.0.borrow()[1]
                .text
                .starts_with("Second and last reminder")
        );
        assert_eq!(sink.0.borrow().len(), 2);
        // test_tomorrow_is_a_new_morning.
        let monday = nudge(
            &dir,
            &none,
            &sink,
            "2026-11-02T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        assert!(monday.sent);
        // test_the_marker_is_claimed_before_the_send: a refusing mailer still burns the window.
        let refusing = Sink(RefCell::new(vec![]), false);
        let r = nudge(
            &dir,
            &none,
            &refusing,
            "2026-11-03T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        assert_eq!(r.notes, ["the mailer refused the message"]);
        assert!(
            marker_path(
                &dir.0,
                NaiveDate::from_ymd_opt(2026, 11, 3).unwrap(),
                NudgeWindow::First
            )
            .is_file()
        );
    }

    // TestTheDaysThatAreNotMornings.
    #[tokio::test]
    async fn holidays_and_weekends_produce_no_nudge() {
        let dir = Dir::new("days");
        let sink = Sink(RefCell::new(vec![]), true);
        let none = store(&dir, None);
        let r = nudge(
            &dir,
            &none,
            &sink,
            "2026-11-10T08:45:00",
            NudgeWindow::First,
            false,
        )
        .await;
        assert_eq!(
            r.notes,
            ["an NSE holiday; there is no session to log in for"]
        );
        let r = nudge(
            &dir,
            &none,
            &sink,
            "2026-10-31T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        assert_eq!(r.notes, ["not a weekday"]);
        assert!(sink.0.borrow().is_empty());
    }

    // TestItNeverGetsInTheWayOfTheMorning.
    #[tokio::test]
    async fn no_recipient_and_a_failing_builder_are_notes() {
        let dir = Dir::new("soft");
        let sink = Sink(RefCell::new(vec![]), true);
        let none = store(&dir, None);
        let inputs = NudgeInputs {
            now: at("2026-10-30T08:45:00"),
            token_store: &none,
            mailer: &sink,
            login_url_for: &url_for,
            to: "",
            state_dir: &dir.0,
            window: NudgeWindow::First,
            account: DEFAULT_ACCOUNT,
        };
        let r = run_login_nudge(&inputs, async { Ok(true) })
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            r.notes,
            ["BASKFY_KITE_LOGIN_NUDGE_TO is not set; nothing was sent"]
        );
        let broken = |_: NudgeWindow| -> Result<LoginUrl, String> { Err("ValueError".to_owned()) };
        let inputs = NudgeInputs {
            to: "me@example.com",
            login_url_for: &broken,
            ..inputs
        };
        let r = run_login_nudge(&inputs, async { Ok(true) })
            .await
            .unwrap()
            .unwrap();
        assert_eq!(r.notes, ["the login URL could not be built (ValueError)"]);
        assert!(sink.0.borrow().is_empty());
    }

    // TestNothingSecretIsEverRendered / TestTheMessageReadsRightAtQuarterToNine.
    #[test]
    fn the_message_reads_right_and_holds_one_link() {
        let login = url_for(NudgeWindow::First).unwrap();
        let day = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
        let m = render_nudge(
            &login,
            "me@example.com",
            day,
            NudgeWindow::First,
            DEFAULT_ACCOUNT,
            "no usable Kite session is stored",
        );
        assert_eq!(m.subject, SUBJECT);
        assert!(
            m.text
                .contains("For Friday 30 October 2026: no usable Kite session is stored.")
        );
        assert!(m.text.contains("at the 08:45 check"));
        assert!(m.text.contains("good for 30 minutes. If it has expired"));
        assert!(m.text.ends_with("\n"));
        assert_eq!(m.html.matches("<a href=").count(), 1);
        assert!(m.html.contains("&amp;v=3&amp;redirect_params"));
        assert!(!m.text.contains(KEY));
        let second = render_nudge(&login, "x", day, NudgeWindow::Second, DEFAULT_ACCOUNT, "r");
        assert_eq!(second.subject, m.subject);
        assert!(second.text.contains("There is no further reminder today."));
    }

    // test_two_workers_racing_the_same_tick_claim_it_once.
    #[test]
    fn two_claims_of_one_window_succeed_once() {
        let dir = Dir::new("race");
        let day = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
        let wins = (0..8)
            .filter(|_| claim(&dir.0, day, NudgeWindow::First))
            .count();
        assert_eq!(wins, 1);
        assert_eq!(
            std::fs::read_to_string(marker_path(&dir.0, day, NudgeWindow::First)).unwrap(),
            "2026-10-30 first\n"
        );
    }
    /// `test_a_token_that_expires_mid_morning_is_seen_at_the_second_window`: a rotated key makes
    /// the 08:12 token unreadable by 09:05, and the second window nudges.
    #[tokio::test]
    async fn a_token_unreadable_by_the_second_window_nudges() {
        let dir = Dir::new("rotated");
        let sink = Sink(RefCell::new(vec![]), true);
        let fresh = store(&dir, Some(("tok", "2026-10-30T08:12:00")));
        let first = nudge(
            &dir,
            &fresh,
            &sink,
            "2026-10-30T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        assert!(!first.sent);
        let rotated = AccessTokenStore::new(
            dir.0.join("kite-token.enc"),
            Secret::new("Yk0yV2xQd2dQZ0Y2bEVhR0xqU1l3bVVpWkJ4Q2JzaW8="),
        );
        let second = nudge(
            &dir,
            &rotated,
            &sink,
            "2026-10-30T09:05:00",
            NudgeWindow::Second,
            true,
        )
        .await;
        assert!(second.sent);
        assert_eq!(sink.0.borrow().len(), 1);
    }

    /// `test_no_recipient_configured_sends_nothing_and_raises_nothing`: the window stays
    /// unclaimed, so a box configured at 09:00 still gets its 09:05 mail.
    #[tokio::test]
    async fn no_recipient_leaves_the_window_unclaimed() {
        let dir = Dir::new("unclaimed");
        let sink = Sink(RefCell::new(vec![]), true);
        let none = store(&dir, None);
        let inputs = NudgeInputs {
            now: at("2026-10-30T08:45:00"),
            token_store: &none,
            mailer: &sink,
            login_url_for: &url_for,
            to: "",
            state_dir: &dir.0,
            window: NudgeWindow::First,
            account: DEFAULT_ACCOUNT,
        };
        let r = run_login_nudge(&inputs, async { Ok(true) })
            .await
            .unwrap()
            .unwrap();
        assert!(!r.sent);
        let day = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
        assert!(!marker_path(&dir.0, day, NudgeWindow::First).exists());
    }

    /// `TestNothingSecretIsEverRendered` and `test_the_step_outcome_carries_what_happened`.
    #[tokio::test]
    async fn nothing_secret_is_rendered_and_the_detail_says_what_happened() {
        let dir = Dir::new("secret");
        let sink = Sink(RefCell::new(vec![]), true);
        let stale = store(&dir, Some(("tok-secret-value", "2026-10-29T23:55:00")));
        let r = nudge(
            &dir,
            &stale,
            &sink,
            "2026-10-30T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        let m = sink.0.borrow()[0].clone();
        let rendered = format!("{}{}{}", m.text, m.html, m.subject);
        assert!(!rendered.contains("tok-secret-value"));
        assert!(!rendered.contains(KEY));
        assert!(!rendered.contains(&stale.path().display().to_string()));
        for phrase in [
            "your own box",
            "no Zerodha password and no 2FA seed",
            "Zerodha's own login page",
            "idles",
            "no order is possible today",
            "Zerodha — Baskfy's Kite Connect app",
        ] {
            assert!(m.text.contains(phrase), "{phrase}");
        }
        let detail = r.as_detail();
        assert_eq!(detail.get("sent"), Some(&OrderedJson::Bool(true)));
        assert_eq!(detail.get("window"), Some(&s("first")));
        let refusing = Sink(RefCell::new(vec![]), false);
        let r = nudge(
            &dir,
            &stale,
            &refusing,
            "2026-11-02T08:45:00",
            NudgeWindow::First,
            true,
        )
        .await;
        let joined = r.notes.join(" ");
        assert!(!joined.contains("example.com"));
        assert!(!joined.contains(&stale.path().display().to_string()));
    }
}
