//! `kite_session_cli.refresh_quietly` and what it stands on: Baskfy's own Kite session first
//! (`local_session_is_live` — a read-only `GET /user/profile`), then the desk's over SSH
//! (`pull` → `fetch_desk_token`, `verify_session`, `deposit`). A failure is a log line and
//! `false`, never a stopped night: daily bars do not depend on a Kite session (M84).
//!
//! Nothing here logs in, places, modifies or cancels anything (law 2): the profile call reads
//! nothing and changes nothing; the SSH command reads one string from the desk's forced command;
//! the deposit writes the encrypted token blob. The token is never printed, logged or put on a
//! command line. Python's `print`s go to standard error here, because standard output carries a
//! task's JSON payload.

use std::io::Write as _;
use std::process::Stdio;
use std::sync::Arc;

use baskfy_providers::clock::Clock;
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::AccessTokenStore;
use baskfy_providers::{ErrorKind, ProviderError};

use crate::error::WorkerError;

/// `_PROFILE_URL`: Kite's cheapest authenticated call, read-only.
pub(crate) const PROFILE_URL: &str = "https://api.kite.trade/user/profile";

/// What a step of the bridge failed with: Python's `SystemExit(message)`, or anything else it
/// lets propagate.
#[derive(Debug)]
pub(crate) enum Failure {
    /// `SystemExit(message)` — caught by `refresh_quietly` and `local_session_is_live`.
    Exit(String),
    /// Any other exception — propagates.
    Raise(WorkerError),
}

impl From<WorkerError> for Failure {
    fn from(e: WorkerError) -> Self {
        Self::Raise(e)
    }
}

fn info(message: &str) {
    let _ = writeln!(
        std::io::stderr(),
        "INFO baskfy_worker.kite_session_cli: {message}"
    );
}

/// `_TOKEN_PATTERN`: `\A[A-Za-z0-9_-]{16,128}\Z`.
pub(crate) fn looks_like_a_token(token: &str) -> bool {
    (16..=128).contains(&token.len())
        && token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// `_store()`: refuses with no encryption key.
fn store(provider: &ProviderSettings, clock: Arc<dyn Clock>) -> Result<AccessTokenStore, Failure> {
    if !provider.kite_token_encryption_key.is_set() {
        return Err(Failure::Exit(
            "BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set; refusing to write a live broker \
             credential to disk unencrypted."
                .to_owned(),
        ));
    }
    Ok(AccessTokenStore::with_clock(
        provider.kite_token_path.clone(),
        provider.kite_token_encryption_key.clone(),
        clock,
    ))
}

/// `deposit(token)`: store, read back, compare; report the length, never the token.
///
/// # Errors
/// `Exit` with no key or a token that does not read back; the store's errors otherwise.
pub(crate) fn deposit(
    token: &str,
    provider: &ProviderSettings,
    clock: Arc<dyn Clock>,
) -> Result<i32, Failure> {
    let store = store(provider, clock)?;
    let record = store
        .save(token.trim(), None)
        .map_err(|e| Failure::Raise(e.into()))?;
    let reloaded = store.load().map_err(|e| Failure::Raise(e.into()))?;
    if reloaded.value.expose() != record.value.expose() {
        return Err(Failure::Exit(
            "stored token did not read back identically; refusing to report success".to_owned(),
        ));
    }
    info(&format!(
        "stored: {} chars, issued_at={}",
        record.value.expose().chars().count(),
        baskfy_providers::records::PyDateTime::Aware(record.issued_at).isoformat()
    ));
    info(&format!("path  : {}", store.path().display()));
    Ok(0)
}

/// `desk_session_pull_configured()`: both the target and the key.
fn pull_configured(provider: &ProviderSettings) -> bool {
    !provider.kite_desk_ssh_target.is_empty() && !provider.kite_desk_ssh_key_path.is_empty()
}

/// `fetch_desk_token()`: today's token from the desk over SSH (fixed argv, no shell, the token
/// on stdout, never echoed back on failure).
///
/// # Errors
/// `Exit` for every refusal Python turns into `SystemExit`.
pub(crate) async fn fetch_desk_token(provider: &ProviderSettings) -> Result<String, Failure> {
    if !pull_configured(provider) {
        return Err(Failure::Exit(
            "BASKFY_KITE_DESK_SSH_TARGET is not set; this deployment cannot pull a session. \
             Use `deposit --token` for a one-off."
                .to_owned(),
        ));
    }
    let timeout = provider.kite_desk_ssh_timeout_seconds;
    // `int(timeout)` truncates toward zero, as Python's `int()` of a float.
    #[allow(clippy::cast_possible_truncation)]
    let connect_timeout = timeout.trunc() as i64;
    let spawned = std::process::Command::new("ssh")
        .args([
            "-T",
            "-n",
            "-i",
            &provider.kite_desk_ssh_key_path,
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            &format!("UserKnownHostsFile={}", provider.kite_desk_known_hosts_path),
            "-o",
            &format!("ConnectTimeout={connect_timeout}"),
            &provider.kite_desk_ssh_target,
            "kite-access-token",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();
    let mut child = match spawned {
        Ok(child) => child,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(Failure::Exit(
                "no ssh client on this host; the image needs openssh-client".to_owned(),
            ));
        }
        Err(e) => {
            return Err(Failure::Raise(WorkerError::python(
                "OSError",
                e.to_string(),
            )));
        }
    };
    let started = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => {}
            Err(e) => {
                return Err(Failure::Raise(WorkerError::python(
                    "OSError",
                    e.to_string(),
                )));
            }
        }
        if started.elapsed().as_secs_f64() >= timeout {
            let _ = child.kill();
            let _ = child.wait();
            return Err(Failure::Exit(format!(
                "the desk did not answer within {}s",
                baskfy_core::py::float_repr(timeout)
            )));
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let output = child
        .wait_with_output()
        .map_err(|e| Failure::Raise(WorkerError::python("OSError", e.to_string())))?;
    if !output.status.success() {
        let code = output.status.code().unwrap_or_else(|| {
            use std::os::unix::process::ExitStatusExt as _;
            output.status.signal().map_or(-1, |s| -s)
        });
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = stderr
            .trim()
            .lines()
            .last()
            .unwrap_or("no detail")
            .to_owned();
        return Err(Failure::Exit(format!(
            "desk refused or was unreachable (exit {code}): {detail}"
        )));
    }
    let token = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if !looks_like_a_token(&token) {
        return Err(Failure::Exit(format!(
            "the desk returned {} characters that do not look like an access token; refusing \
             to store it",
            token.chars().count()
        )));
    }
    Ok(token)
}

/// httpx's class for a reqwest failure (what `type(exc).__name__` names).
fn httpx_class(e: &reqwest::Error) -> &'static str {
    if e.is_timeout() {
        if e.is_connect() {
            "ConnectTimeout"
        } else {
            "ReadTimeout"
        }
    } else if e.is_connect() {
        "ConnectError"
    } else {
        "HTTPError"
    }
}

/// `verify_session(token)`: prove the token works (`GET /user/profile`, the token in a header),
/// returning the Kite user id it belongs to.
///
/// # Errors
/// `Exit` for every refusal; a body that is not JSON raises (`JSONDecodeError`), as Python's
/// `response.json()`.
pub(crate) async fn verify_session(
    token: &str,
    provider: &ProviderSettings,
    url: &str,
) -> Result<String, Failure> {
    if !provider.kite_api_key.is_set() {
        return Err(Failure::Exit(
            "BASKFY_KITE_API_KEY is not set; a token alone cannot call Kite".to_owned(),
        ));
    }
    let unreachable = |class: &str| {
        Failure::Exit(format!(
            "could not reach Kite to verify the session: {class}"
        ))
    };
    let client = baskfy_providers::net::GuardedClient::from_builder(
        baskfy_providers::net::with_kite_proxy(
            baskfy_providers::net::client_builder().timeout(std::time::Duration::from_secs(15)),
        )
        .map_err(|_| unreachable("ConnectError"))?,
    )
    .map_err(|_| unreachable("ConnectError"))?;
    let request = client.get(url).map_err(|e| unreachable(e.python_type()))?;
    let response = request
        .header("X-Kite-Version", "3")
        .header(
            "Authorization",
            format!("token {}:{token}", provider.kite_api_key.expose()),
        )
        .send()
        .await
        .map_err(|e| unreachable(httpx_class(&e)))?;
    let status = response.status().as_u16();
    if status == 403 {
        return Err(Failure::Exit(
            "Kite refused the token (403). Either the desk has not logged in today, or this \
             host's IP is not whitelisted on the Kite app."
                .to_owned(),
        ));
    }
    if status != 200 {
        return Err(Failure::Exit(format!(
            "Kite answered {status} when verifying the session"
        )));
    }
    let body = response
        .bytes()
        .await
        .map_err(|e| unreachable(httpx_class(&e)))?;
    let payload: serde_json::Value = serde_json::from_slice(&body)
        .map_err(|e| Failure::Raise(WorkerError::python("JSONDecodeError", e.to_string())))?;
    match payload
        .get("data")
        .and_then(|d| d.get("user_id"))
        .and_then(serde_json::Value::as_str)
    {
        Some(user) if !user.is_empty() => Ok(user.to_owned()),
        _ => Err(Failure::Exit(
            "Kite's profile response carried no user_id; refusing to trust it".to_owned(),
        )),
    }
}

/// `pull(verify=True)`: fetch the desk's token, prove it works, store it.
///
/// # Errors
/// As [`fetch_desk_token`], [`verify_session`] and [`deposit`].
pub(crate) async fn pull(
    verify: bool,
    provider: &ProviderSettings,
    clock: Arc<dyn Clock>,
) -> Result<i32, Failure> {
    let token = fetch_desk_token(provider).await?;
    if verify {
        let user = verify_session(&token, provider, PROFILE_URL).await?;
        info(&format!("verified: live Kite session for {user}"));
    }
    deposit(&token, provider, clock)
}

/// `local_session_is_live()`: does Baskfy already hold a session Kite accepts right now?
/// `SystemExit` and `CredentialsMissing` are `false`; anything else propagates.
///
/// # Errors
/// What Python lets through (`UnexpectedPayload`, an `OSError`, a non-JSON profile body).
pub(crate) async fn local_session_is_live(
    provider: &ProviderSettings,
    clock: Arc<dyn Clock>,
) -> Result<bool, WorkerError> {
    let store = match store(provider, clock) {
        Ok(s) => s,
        Err(Failure::Exit(_)) => return Ok(false),
        Err(Failure::Raise(e)) => return Err(e),
    };
    if !store.exists() {
        return Ok(false);
    }
    let token = match store.load() {
        Ok(t) => t,
        Err(e) if is_credentials_missing(&e) => return Ok(false),
        Err(e) => return Err(e.into()),
    };
    match verify_session(token.value.expose(), provider, PROFILE_URL).await {
        Ok(_) => Ok(true),
        Err(Failure::Exit(_)) => Ok(false),
        Err(Failure::Raise(e)) => Err(e),
    }
}

fn is_credentials_missing(e: &ProviderError) -> bool {
    e.kind() == ErrorKind::CredentialsMissing
}

/// `refresh_quietly()`: Baskfy's own live session, else the desk's; `false` (and a log line)
/// on a `SystemExit`.
///
/// # Errors
/// What Python lets propagate.
pub(crate) async fn refresh_quietly(
    provider: &ProviderSettings,
    clock: Arc<dyn Clock>,
) -> Result<bool, WorkerError> {
    if local_session_is_live(provider, Arc::clone(&clock)).await? {
        return Ok(true);
    }
    match pull(true, provider, clock).await {
        Ok(_) => Ok(true),
        Err(Failure::Exit(message)) => {
            let _ = writeln!(std::io::stderr(), "kite session not refreshed: {message}");
            Ok(false)
        }
        Err(Failure::Raise(e)) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::panic,
        clippy::field_reassign_with_default,
        clippy::assertions_on_constants
    )]
    use baskfy_providers::clock::SystemClock;
    use baskfy_providers::secret::Secret;

    use super::*;

    // TestItRefusesAnythingThatIsNotAToken.
    #[test]
    fn only_a_token_shaped_string_is_accepted() {
        assert!(looks_like_a_token("abcdefABCDEF0123_-xy"));
        assert!(!looks_like_a_token("short"));
        assert!(!looks_like_a_token("Permission denied (publickey)."));
        assert!(!looks_like_a_token(&"a".repeat(129)));
        assert!(!looks_like_a_token("abcdefghijklmnop\nqrs"));
    }

    // TestPullFetchesTheSessionWithoutAHuman.test_a_deployment_without_a_desk_is_told_what_to_do_instead.
    #[tokio::test]
    async fn a_deployment_without_a_desk_is_told_what_to_do_instead() {
        let provider = ProviderSettings::default();
        match fetch_desk_token(&provider).await {
            Err(Failure::Exit(m)) => assert!(m.contains("BASKFY_KITE_DESK_SSH_TARGET is not set")),
            other => panic!("{other:?}"),
        }
    }

    // TestItRefusesRatherThanFailLater.test_no_encryption_key_is_refused_up_front.
    #[test]
    fn no_encryption_key_is_refused_up_front() {
        let provider = ProviderSettings::default();
        match deposit(
            "abcdefghijklmnopqrstuvwxyz",
            &provider,
            Arc::new(SystemClock),
        ) {
            Err(Failure::Exit(m)) => assert!(m.contains("refusing to write a live broker")),
            other => panic!("{other:?}"),
        }
    }

    // TestAPulledSessionIsProvenBeforeItIsTrusted.test_no_api_key_is_named_as_the_missing_piece.
    #[tokio::test]
    async fn no_api_key_is_named_as_the_missing_piece() {
        let provider = ProviderSettings::default();
        match verify_session("tok", &provider, PROFILE_URL).await {
            Err(Failure::Exit(m)) => assert!(m.contains("BASKFY_KITE_API_KEY is not set")),
            other => panic!("{other:?}"),
        }
    }

    // TestBaskfysOwnSessionComesFirst.test_no_local_token_and_no_desk_reports_false_without_raising.
    #[tokio::test]
    async fn no_local_token_and_no_desk_reports_false_without_raising() {
        let dir = std::env::temp_dir().join(format!("baskfy-ks-{}", std::process::id()));
        let mut provider = ProviderSettings::default();
        provider.kite_token_path = dir.join("absent.enc").display().to_string();
        provider.kite_token_encryption_key =
            Secret::new("ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=");
        assert!(
            !refresh_quietly(&provider, Arc::new(SystemClock))
                .await
                .unwrap()
        );
    }

    // TestTheTokenReachesTheProvidersStore: a deposited token is what the provider will read.
    #[test]
    fn a_deposited_token_reads_back() {
        let dir = std::env::temp_dir().join(format!("baskfy-ks-dep-{}", std::process::id()));
        let mut provider = ProviderSettings::default();
        provider.kite_token_path = dir.join("t.enc").display().to_string();
        provider.kite_token_encryption_key =
            Secret::new("ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=");
        assert_eq!(
            deposit("  abcdefghijklmnop1234  ", &provider, Arc::new(SystemClock)).unwrap(),
            0
        );
        let raw = std::fs::read(dir.join("t.enc")).unwrap();
        assert!(!String::from_utf8_lossy(&raw).contains("abcdefghijklmnop1234"));
        let store = AccessTokenStore::new(
            provider.kite_token_path.clone(),
            provider.kite_token_encryption_key.clone(),
        );
        assert_eq!(store.load().unwrap().value.expose(), "abcdefghijklmnop1234");
        let _ = std::fs::remove_dir_all(dir);
    }
}
