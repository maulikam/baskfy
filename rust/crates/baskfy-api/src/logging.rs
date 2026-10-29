//! Structured logging with a request id and secret redaction. Ports `baskfy_api.logging`.
//!
//! One line per record on stderr (uvicorn's handler stream): JSON when `BASKFY_LOG_JSON` is true
//! (`{"timestamp", "level", "logger", "message", "request_id"?, ...fields}`), otherwise Python's
//! text form `LEVEL    [rid8] logger: message`.
//!
//! **Redaction is applied to every record before it is formatted** (`RedactingFilter`): a field
//! whose name looks secret has its value replaced by `[redacted]`, and the message text has
//! bearer tokens, `key=value` credentials and bare JWTs replaced. It is a backstop: nothing in
//! this crate passes a secret to the logger in the first place.

use std::io::Write;
use std::sync::OnceLock;
use std::sync::atomic::{AtomicU8, Ordering};

use regex::Regex;

/// `REQUEST_ID_HEADER`.
pub const REQUEST_ID_HEADER: &str = "X-Request-Id";

/// `REDACTED`.
pub const REDACTED: &str = "[redacted]";

tokio::task_local! {
    /// `request_id_var`: set by the request-context layer for the lifetime of one request.
    pub static REQUEST_ID: String;
}

/// `current_request_id()`.
#[must_use]
pub fn current_request_id() -> Option<String> {
    REQUEST_ID.try_with(Clone::clone).ok()
}

/// `new_request_id()`: `uuid.uuid4().hex` — 32 lowercase hex digits, version 4, RFC 4122 variant.
#[must_use]
pub fn new_request_id() -> String {
    let mut bytes: [u8; 16] = rand::random();
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// `_SECRET_FIELD_MARKERS`.
const SECRET_FIELD_MARKERS: [&str; 11] = [
    "password",
    "token",
    "secret",
    "authorization",
    "cookie",
    "otp",
    "code",
    "api_key",
    "apikey",
    "credential",
    "signature",
];

/// `_SECRET_FIELD_ALLOWLIST`.
const SECRET_FIELD_ALLOWLIST: [&str; 5] = [
    "status_code",
    "token_count",
    "error_code",
    "problem_code",
    "reason_code",
];

/// `_is_secret_field`.
#[must_use]
pub fn is_secret_field(name: &str) -> bool {
    if SECRET_FIELD_ALLOWLIST.contains(&name) {
        return false;
    }
    let lowered = name.to_lowercase();
    SECRET_FIELD_MARKERS.iter().any(|m| lowered.contains(m))
}

fn patterns() -> &'static Option<[Regex; 3]> {
    static PATTERNS: OnceLock<Option<[Regex; 3]>> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        Some([
            Regex::new(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}").ok()?,
            Regex::new(
                r#"(?i)\b([a-z_]*(?:password|token|secret|otp|api[_-]?key)[a-z_]*)(\s*[=:]\s*)["']?[^\s"',;)}]+"#,
            )
            .ok()?,
            Regex::new(r"\beyJ[A-Za-z0-9._\-]{16,}").ok()?,
        ])
    })
}

/// `redact_text`: replace anything in a free-text message that looks like a credential. If the
/// patterns could not be built (they are constants, so never), the whole message is withheld
/// rather than printed unredacted.
#[must_use]
pub fn redact_text(message: &str) -> String {
    let Some([bearer, pair, jwt]) = patterns() else {
        return REDACTED.to_owned();
    };
    let step = bearer.replace_all(message, format!("${{1}} {REDACTED}").as_str());
    let step = pair.replace_all(&step, format!("${{1}}${{2}}{REDACTED}").as_str());
    jwt.replace_all(&step, REDACTED).into_owned()
}

/// A level, ordered as Python's.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    /// `DEBUG` (10).
    Debug = 10,
    /// `INFO` (20).
    Info = 20,
    /// `WARNING` (30).
    Warning = 30,
    /// `ERROR` (40).
    Error = 40,
}

impl Level {
    fn name(self) -> &'static str {
        match self {
            Self::Debug => "DEBUG",
            Self::Info => "INFO",
            Self::Warning => "WARNING",
            Self::Error => "ERROR",
        }
    }
}

static THRESHOLD: AtomicU8 = AtomicU8::new(20);
static JSON_OUTPUT: AtomicU8 = AtomicU8::new(1);

/// `configure_logging(level=..., json_output=...)`.
pub fn configure(level: &str, json_output: bool) {
    let threshold = match level.to_uppercase().as_str() {
        "DEBUG" => 10,
        "WARNING" | "WARN" => 30,
        "ERROR" => 40,
        "CRITICAL" => 50,
        _ => 20,
    };
    THRESHOLD.store(threshold, Ordering::Relaxed);
    JSON_OUTPUT.store(u8::from(json_output), Ordering::Relaxed);
}

/// The line a record renders to, after redaction (exposed for the redaction tests).
#[must_use]
pub fn render(
    level: Level,
    logger: &str,
    message: &str,
    fields: &[(&str, &str)],
    json: bool,
) -> String {
    let message = redact_text(message);
    let request_id = current_request_id();
    if json {
        let mut entries: Vec<(String, baskfy_db::OrderedJson)> = vec![
            (
                "timestamp".to_owned(),
                baskfy_db::OrderedJson::Str(now_iso()),
            ),
            (
                "level".to_owned(),
                baskfy_db::OrderedJson::Str(level.name().to_owned()),
            ),
            (
                "logger".to_owned(),
                baskfy_db::OrderedJson::Str(logger.to_owned()),
            ),
            ("message".to_owned(), baskfy_db::OrderedJson::Str(message)),
        ];
        if let Some(rid) = request_id {
            entries.push(("request_id".to_owned(), baskfy_db::OrderedJson::Str(rid)));
        }
        for (k, v) in fields {
            let value = if is_secret_field(k) {
                REDACTED.to_owned()
            } else {
                (*v).to_owned()
            };
            entries.push(((*k).to_owned(), baskfy_db::OrderedJson::Str(value)));
        }
        baskfy_db::OrderedJson::Object(entries)
            .to_python_json()
            .unwrap_or_default()
    } else {
        let prefix = request_id
            .map(|r| format!("[{}] ", r.chars().take(8).collect::<String>()))
            .unwrap_or_default();
        format!("{:<8} {prefix}{logger}: {message}", level.name())
    }
}

fn now_iso() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = i64::try_from(now.as_secs()).unwrap_or(0);
    chrono::DateTime::from_timestamp(secs, now.subsec_micros() * 1000)
        .map(|t| t.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string())
        .unwrap_or_default()
}

/// Logs one record. `fields` are Python's `extra=`; a secret-looking name is redacted.
pub fn log(level: Level, logger: &str, message: &str, fields: &[(&str, &str)]) {
    if (level as u8) < THRESHOLD.load(Ordering::Relaxed) {
        return;
    }
    let json = JSON_OUTPUT.load(Ordering::Relaxed) == 1;
    let line = render(level, logger, message, fields, json);
    let mut err = std::io::stderr().lock();
    // A failed write to stderr has nowhere left to be reported.
    let _ = writeln!(err, "{line}");
}

/// `log.info`.
pub fn info(logger: &str, message: &str, fields: &[(&str, &str)]) {
    log(Level::Info, logger, message, fields);
}

/// `log.warning`.
pub fn warning(logger: &str, message: &str, fields: &[(&str, &str)]) {
    log(Level::Warning, logger, message, fields);
}

/// `log.error`.
pub fn error(logger: &str, message: &str, fields: &[(&str, &str)]) {
    log(Level::Error, logger, message, fields);
}

#[cfg(test)]
mod tests {
    // Ports test_log_redaction.py's TestTheFilter.
    use super::*;

    #[test]
    fn a_field_named_like_a_secret_is_redacted_and_innocent_lookalikes_are_not() {
        let line = render(
            Level::Info,
            "test",
            "signed in",
            &[
                ("password", "hunter2"),
                ("refresh_token", "abc"),
                ("email", "a@b.c"),
                ("status_code", "200"),
                ("token_count", "3"),
            ],
            true,
        );
        let v: serde_json::Value = serde_json::from_str(&line).unwrap_or_default();
        assert_eq!(v["password"], REDACTED);
        assert_eq!(v["refresh_token"], REDACTED);
        assert_eq!(v["email"], "a@b.c");
        assert_eq!(v["status_code"], "200");
        assert_eq!(v["token_count"], "3");
        assert!(!line.contains("hunter2"));
    }

    #[test]
    fn credentials_in_the_message_are_redacted() {
        assert!(
            !redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.body.sig").contains("eyJ")
        );
        assert!(!redact_text("login failed password=hunter2").contains("hunter2"));
        assert!(!redact_text(r#"otp: "123456""#).contains("123456"));
        assert!(!redact_text("api_key=sk_live_x").contains("sk_live_x"));
        let token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcdefghij";
        assert!(!redact_text(&format!("rejected {token}")).contains(token));
        let line = render(Level::Info, "t", "token=secret-value for a@b.c", &[], false);
        assert!(
            !line.contains("secret-value") && line.contains("a@b.c"),
            "{line}"
        );
    }

    #[test]
    fn the_text_format_is_pythons() {
        assert_eq!(
            render(Level::Warning, "baskfy_api.app", "hello", &[], false),
            "WARNING  baskfy_api.app: hello"
        );
    }

    #[test]
    fn request_ids_are_uuid4_hex() {
        let id = new_request_id();
        assert_eq!(id.len(), 32);
        assert_eq!(&id[12..13], "4");
        assert!(matches!(&id[16..17], "8" | "9" | "a" | "b"));
        assert_ne!(id, new_request_id());
    }
}
