//! `baskfy_worker.kite_session_cli` as a subcommand: deposit, pull or report the Kite session.

use std::future::Future;
use std::io::Read as _;
use std::process::{Command, ExitCode, Stdio};
use std::time::{Duration, Instant};

use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::{AccessTokenStore, ist};

use super::cli::{Failure, Outcome, Parsed, Spec, command, say};
use crate::error::WorkerError;

const PROFILE_URL: &str = "https://api.kite.trade/user/profile";

pub(super) async fn run(args: &[String]) -> Outcome {
    let (cmd, rest) = command(args, &["deposit", "pull", "status"])?;
    let settings = ProviderSettings::from_env()
        .map_err(|e| Failure::Raised(WorkerError::Settings(e.to_string())))?;
    match cmd {
        "deposit" => {
            let flags = Parsed::parse(
                rest,
                Spec {
                    valued: &["--token"],
                    ..Spec::default()
                },
            )?;
            let token = flags.required("--token")?.to_owned();
            for line in deposit(&store(&settings)?, &token)? {
                say(&line);
            }
            Ok(ExitCode::SUCCESS)
        }
        "pull" => {
            let flags = Parsed::parse(
                rest,
                Spec {
                    switches: &["--no-verify"],
                    ..Spec::default()
                },
            )?;
            let verify = !flags.on("--no-verify");
            let lines = pull(&settings, verify, run_ssh, |token| {
                let settings = settings.clone();
                async move { verify_session(&settings, &token).await }
            })
            .await?;
            for line in lines {
                say(&line);
            }
            Ok(ExitCode::SUCCESS)
        }
        _ => {
            Parsed::parse(rest, Spec::default())?;
            let (lines, ok) = status(&store(&settings)?)?;
            for line in lines {
                say(&line);
            }
            Ok(super::cli::code(ok))
        }
    }
}

fn store(settings: &ProviderSettings) -> Result<AccessTokenStore, Failure> {
    if !settings.kite_token_encryption_key.is_set() {
        return Err(Failure::Exit(
            "BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set; refusing to write a live broker \
             credential to disk unencrypted."
                .to_owned(),
        ));
    }
    Ok(AccessTokenStore::new(
        &settings.kite_token_path,
        settings.kite_token_encryption_key.clone(),
    ))
}

fn provider(e: baskfy_providers::ProviderError) -> Failure {
    Failure::Raised(WorkerError::from(e))
}

/// `deposit(token)`: store, read back, and say so without ever naming the token.
fn deposit(store: &AccessTokenStore, token: &str) -> Result<Vec<String>, Failure> {
    let record = store.save(token.trim(), None).map_err(provider)?;
    let reloaded = store.load().map_err(provider)?;
    if reloaded.value.expose() != record.value.expose() {
        return Err(Failure::Exit(
            "stored token did not read back identically; refusing to report success".to_owned(),
        ));
    }
    let issued = baskfy_providers::records::PyDateTime::Aware(record.issued_at).isoformat();
    Ok(vec![
        format!(
            "stored: {} chars, issued_at={issued}",
            record.value.expose().chars().count()
        ),
        format!("path  : {}", store.path().display()),
    ])
}

/// `status()`: the lines it prints and whether a usable session exists.
fn status(store: &AccessTokenStore) -> Result<(Vec<String>, bool), Failure> {
    if !store.exists() {
        return Ok((
            vec![format!("no token at {}", store.path().display())],
            false,
        ));
    }
    let record = store.load().map_err(provider)?;
    let expired = record.is_expired(chrono::Utc::now().with_timezone(&ist()));
    let issued = baskfy_providers::records::PyDateTime::Aware(record.issued_at).isoformat();
    Ok((
        vec![
            format!("path      : {}", store.path().display()),
            format!("issued_at : {issued}"),
            format!("expired   : {}", if expired { "True" } else { "False" }),
        ],
        !expired,
    ))
}

/// `desk_session_pull_configured()`.
fn pull_configured(settings: &ProviderSettings) -> bool {
    !settings.kite_desk_ssh_target.is_empty() && !settings.kite_desk_ssh_key_path.is_empty()
}

/// The ssh argv `fetch_desk_token` runs, without a shell.
fn ssh_argv(settings: &ProviderSettings) -> Vec<String> {
    #[allow(clippy::cast_possible_truncation)]
    let connect_timeout = settings.kite_desk_ssh_timeout_seconds.trunc() as i64;
    [
        "ssh",
        "-T",
        "-n",
        "-i",
        &settings.kite_desk_ssh_key_path,
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        &format!("UserKnownHostsFile={}", settings.kite_desk_known_hosts_path),
        "-o",
        &format!("ConnectTimeout={connect_timeout}"),
        &settings.kite_desk_ssh_target,
        "kite-access-token",
    ]
    .iter()
    .map(|s| (*s).to_owned())
    .collect()
}

/// What one ssh run produced.
#[derive(Debug, Clone)]
struct SshOutput {
    code: i32,
    stdout: String,
    stderr: String,
}

/// Why an ssh run produced nothing.
#[derive(Debug, Clone)]
enum SshError {
    Timeout(f64),
    NoClient,
    Os(String),
}

fn run_ssh(argv: &[String], timeout: f64) -> Result<SshOutput, SshError> {
    let (program, rest) = argv.split_first().ok_or(SshError::NoClient)?;
    let mut child = Command::new(program)
        .args(rest)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                SshError::NoClient
            } else {
                SshError::Os(e.to_string())
            }
        })?;
    let drain = |pipe: Option<Box<dyn std::io::Read + Send>>| {
        std::thread::spawn(move || {
            let mut text = String::new();
            if let Some(mut p) = pipe {
                let _ = p.read_to_string(&mut text);
            }
            text
        })
    };
    let out = drain(
        child
            .stdout
            .take()
            .map(|p| Box::new(p) as Box<dyn std::io::Read + Send>),
    );
    let err = drain(
        child
            .stderr
            .take()
            .map(|p| Box::new(p) as Box<dyn std::io::Read + Send>),
    );
    let deadline = Instant::now() + Duration::from_secs_f64(timeout.max(0.0));
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(SshError::Timeout(timeout));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(25)),
            Err(e) => return Err(SshError::Os(e.to_string())),
        }
    };
    #[cfg(unix)]
    let code = {
        use std::os::unix::process::ExitStatusExt as _;
        status
            .code()
            .unwrap_or_else(|| -status.signal().unwrap_or(1))
    };
    #[cfg(not(unix))]
    let code = status.code().unwrap_or(1);
    Ok(SshOutput {
        code,
        stdout: out.join().unwrap_or_default(),
        stderr: err.join().unwrap_or_default(),
    })
}

/// `_TOKEN_PATTERN`: `\A[A-Za-z0-9_-]{16,128}\Z`.
fn looks_like_token(token: &str) -> bool {
    (16..=128).contains(&token.len())
        && token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// `fetch_desk_token()`, with the ssh run injected.
fn fetch_desk_token<R>(settings: &ProviderSettings, runner: R) -> Result<String, Failure>
where
    R: FnOnce(&[String], f64) -> Result<SshOutput, SshError>,
{
    if !pull_configured(settings) {
        return Err(Failure::Exit(
            "BASKFY_KITE_DESK_SSH_TARGET is not set; this deployment cannot pull a session. Use \
             `deposit --token` for a one-off."
                .to_owned(),
        ));
    }
    let timeout = settings.kite_desk_ssh_timeout_seconds;
    let completed = match runner(&ssh_argv(settings), timeout) {
        Ok(c) => c,
        Err(SshError::Timeout(t)) => {
            return Err(Failure::Exit(format!(
                "the desk did not answer within {}s",
                baskfy_core::py::float_repr(t)
            )));
        }
        Err(SshError::NoClient) => {
            return Err(Failure::Exit(
                "no ssh client on this host; the image needs openssh-client".to_owned(),
            ));
        }
        Err(SshError::Os(e)) => {
            return Err(Failure::Raised(WorkerError::python("OSError", e)));
        }
    };
    if completed.code != 0 {
        let detail = completed
            .stderr
            .trim()
            .lines()
            .last()
            .unwrap_or("no detail")
            .to_owned();
        return Err(Failure::Exit(format!(
            "desk refused or was unreachable (exit {}): {detail}",
            completed.code
        )));
    }
    let token = completed.stdout.trim().to_owned();
    if !looks_like_token(&token) {
        return Err(Failure::Exit(format!(
            "the desk returned {} characters that do not look like an access token; refusing to \
             store it",
            token.chars().count()
        )));
    }
    Ok(token)
}

/// `verify_session`'s reading of Kite's answer: the user id, or Python's `SystemExit` text.
fn classify_profile(status: u16, body: &str) -> Result<String, Failure> {
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
    let payload: serde_json::Value = serde_json::from_str(body)
        .map_err(|e| Failure::Raised(WorkerError::python("JSONDecodeError", e.to_string())))?;
    let user_id = payload
        .get("data")
        .filter(|d| d.is_object())
        .and_then(|d| d.get("user_id"))
        .and_then(serde_json::Value::as_str)
        .filter(|u| !u.is_empty());
    user_id.map(str::to_owned).ok_or_else(|| {
        Failure::Exit("Kite's profile response carried no user_id; refusing to trust it".to_owned())
    })
}

/// The profile call's headers: the token travels in `Authorization`, never in the URL.
fn profile_headers(api_key: &str, token: &str) -> [(&'static str, String); 2] {
    [
        ("X-Kite-Version", "3".to_owned()),
        ("Authorization", format!("token {api_key}:{token}")),
    ]
}

fn transport_name(error: &reqwest::Error) -> &'static str {
    if error.is_timeout() && error.is_connect() {
        "ConnectTimeout"
    } else if error.is_timeout() {
        "ReadTimeout"
    } else if error.is_connect() {
        "ConnectError"
    } else {
        "TransportError"
    }
}

/// `verify_session(token)`: one read-only GET of Kite's profile.
async fn verify_session(settings: &ProviderSettings, token: &str) -> Result<String, Failure> {
    if !settings.kite_api_key.is_set() {
        return Err(Failure::Exit(
            "BASKFY_KITE_API_KEY is not set; a token alone cannot call Kite".to_owned(),
        ));
    }
    let unreachable = |name: &str| {
        Failure::Exit(format!(
            "could not reach Kite to verify the session: {name}"
        ))
    };
    let client = baskfy_providers::net::GuardedClient::from_builder(
        baskfy_providers::net::with_kite_proxy(
            baskfy_providers::net::client_builder().timeout(Duration::from_secs(15)),
        )
        .map_err(provider)?,
    )
    .map_err(provider)?;
    let mut request = client.get(PROFILE_URL).map_err(provider)?;
    for (name, value) in profile_headers(settings.kite_api_key.expose(), token) {
        request = request.header(name, value);
    }
    let response = request
        .send()
        .await
        .map_err(|e| unreachable(transport_name(&e)))?;
    let status = response.status().as_u16();
    let body = response
        .text()
        .await
        .map_err(|e| unreachable(transport_name(&e)))?;
    classify_profile(status, &body)
}

/// `pull(verify)`: fetch, prove, then store; the lines it prints.
async fn pull<R, V, F>(
    settings: &ProviderSettings,
    verify: bool,
    runner: R,
    verifier: V,
) -> Result<Vec<String>, Failure>
where
    R: FnOnce(&[String], f64) -> Result<SshOutput, SshError>,
    V: FnOnce(String) -> F,
    F: Future<Output = Result<String, Failure>>,
{
    let token = fetch_desk_token(settings, runner)?;
    let mut lines = Vec::new();
    if verify {
        let user_id = verifier(token.clone()).await?;
        lines.push(format!("verified: live Kite session for {user_id}"));
    }
    lines.extend(deposit(&store(settings)?, &token)?);
    Ok(lines)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use std::cell::RefCell;
    use std::path::PathBuf;

    use baskfy_providers::secret::Secret;

    use super::*;

    const KEY: &str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
    const DESK_TOKEN: &str = "AbCd1234EfGh5678IjKl9012MnOp3456";

    fn dir(name: &str) -> PathBuf {
        let d =
            std::env::temp_dir().join(format!("baskfy-clis-kite-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn settings(name: &str) -> ProviderSettings {
        ProviderSettings {
            kite_token_encryption_key: Secret::new(KEY),
            kite_token_path: dir(name).join("kite.enc").display().to_string(),
            kite_api_key: Secret::new("apikey0123456789"),
            ..ProviderSettings::default()
        }
    }

    fn pullable(name: &str) -> ProviderSettings {
        let mut s = settings(name);
        s.kite_desk_ssh_target = "baskfy@desk.example".to_owned();
        s.kite_desk_ssh_key_path = "/keys/kite-session".to_owned();
        s.kite_desk_known_hosts_path = "/keys/known_hosts".to_owned();
        s
    }

    fn answered(
        code: i32,
        stdout: &str,
        stderr: &str,
    ) -> impl FnOnce(&[String], f64) -> Result<SshOutput, SshError> {
        let out = SshOutput {
            code,
            stdout: stdout.to_owned(),
            stderr: stderr.to_owned(),
        };
        move |_, _| Ok(out)
    }

    fn exit_text(f: Failure) -> String {
        match f {
            Failure::Exit(m) => m,
            other => panic!("expected SystemExit, got {other:?}"),
        }
    }

    async fn never(_: String) -> Result<String, Failure> {
        panic!("Kite must not be called when verification is off")
    }

    #[test]
    fn a_deposited_token_is_what_the_provider_will_read_and_is_never_printed() {
        let s = settings("deposit");
        let st = store(&s).unwrap();
        let lines = deposit(&st, "  live-token-xyz  ").unwrap();
        assert_eq!(st.load().unwrap().value.expose(), "live-token-xyz");
        assert!(lines.iter().all(|l| !l.contains("live-token-xyz")));
        assert!(lines[0].starts_with("stored: 14 chars, issued_at="));
        assert!(lines[1].starts_with("path  : "));
        let bytes = std::fs::read(&s.kite_token_path).unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains("live-token-xyz"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            let mode = std::fs::metadata(&s.kite_token_path)
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o077, 0);
        }
    }

    #[test]
    fn no_encryption_key_is_refused_up_front() {
        let mut s = settings("nokey");
        s.kite_token_encryption_key = Secret::default();
        let text = exit_text(store(&s).unwrap_err());
        assert!(text.contains("BASKFY_KITE_TOKEN_ENCRYPTION_KEY is not set"));
    }

    #[test]
    fn an_empty_token_is_refused() {
        let st = store(&settings("empty")).unwrap();
        let Err(Failure::Raised(e)) = deposit(&st, "   ") else {
            panic!("an empty token must raise");
        };
        assert_eq!(e.python_type(), "ValueError");
    }

    #[test]
    fn status_answers_present_fresh_and_expired() {
        let s = settings("status");
        let st = store(&s).unwrap();
        assert!(!status(&st).unwrap().1);
        deposit(&st, "live-token-xyz").unwrap();
        let (lines, ok) = status(&st).unwrap();
        assert!(ok);
        assert_eq!(lines[2], "expired   : False");
        assert!(lines.iter().all(|l| !l.contains("live-token-xyz")));
        let stale = chrono::Utc::now().with_timezone(&ist()) - chrono::Duration::days(3);
        st.save("stale", Some(stale)).unwrap();
        let (lines, ok) = status(&st).unwrap();
        assert!(!ok);
        assert_eq!(lines[2], "expired   : True");
    }

    #[test]
    fn the_ssh_command_never_prompts_pins_the_host_and_carries_no_token() {
        let argv = ssh_argv(&pullable("argv"));
        assert_eq!(
            argv,
            [
                "ssh",
                "-T",
                "-n",
                "-i",
                "/keys/kite-session",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "PasswordAuthentication=no",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=/keys/known_hosts",
                "-o",
                "ConnectTimeout=20",
                "baskfy@desk.example",
                "kite-access-token",
            ]
        );
    }

    #[tokio::test]
    async fn what_the_desk_returns_is_what_the_provider_will_read() {
        let s = pullable("pulled");
        let lines = pull(
            &s,
            false,
            answered(0, &format!("{DESK_TOKEN}\n"), ""),
            never,
        )
        .await
        .unwrap();
        assert_eq!(
            store(&s).unwrap().load().unwrap().value.expose(),
            DESK_TOKEN
        );
        assert!(lines.iter().all(|l| !l.contains(DESK_TOKEN)));
        assert!(lines[0].starts_with("stored: 32 chars"));
    }

    #[tokio::test]
    async fn a_live_token_is_stored_and_its_owner_named() {
        let s = pullable("verified");
        let seen = RefCell::new(String::new());
        let lines = pull(&s, true, answered(0, DESK_TOKEN, ""), |t| {
            seen.replace(t);
            async { classify_profile(200, r#"{"status":"success","data":{"user_id":"YP8452"}}"#) }
        })
        .await
        .unwrap();
        assert_eq!(lines[0], "verified: live Kite session for YP8452");
        assert_eq!(*seen.borrow(), DESK_TOKEN);
    }

    #[tokio::test]
    async fn a_token_that_fails_verification_is_not_stored() {
        let s = pullable("refused");
        let err = pull(&s, true, answered(0, DESK_TOKEN, ""), |_| async {
            classify_profile(403, r#"{"status":"error"}"#)
        })
        .await
        .unwrap_err();
        let text = exit_text(err);
        assert!(text.contains("logged in") && text.contains("whitelisted"));
        assert!(!store(&s).unwrap().exists());
    }

    #[test]
    fn a_profile_without_a_user_id_is_not_treated_as_proof() {
        let text =
            exit_text(classify_profile(200, r#"{"status":"success","data":{}}"#).unwrap_err());
        assert!(text.contains("user_id"));
        let text = exit_text(classify_profile(500, "{}").unwrap_err());
        assert_eq!(text, "Kite answered 500 when verifying the session");
    }

    #[test]
    fn the_token_travels_in_a_header_and_not_a_query_string() {
        let headers = profile_headers("apikey0123456789", DESK_TOKEN);
        assert_eq!(headers[1].0, "Authorization");
        assert!(headers[1].1.contains(DESK_TOKEN));
        assert!(!PROFILE_URL.contains('?'));
    }

    #[tokio::test]
    async fn no_api_key_is_named_as_the_missing_piece() {
        let mut s = pullable("noapikey");
        s.kite_api_key = Secret::default();
        let text = exit_text(verify_session(&s, DESK_TOKEN).await.unwrap_err());
        assert!(text.contains("BASKFY_KITE_API_KEY"));
    }

    #[tokio::test]
    async fn a_deployment_without_a_desk_is_told_what_to_do_instead() {
        let s = settings("nodesk");
        let err = pull(
            &s,
            false,
            |_: &[String], _| panic!("ssh must not run"),
            never,
        )
        .await
        .unwrap_err();
        let text = exit_text(err);
        assert!(text.contains("BASKFY_KITE_DESK_SSH_TARGET") && text.contains("deposit --token"));
    }

    #[test]
    fn prose_from_the_desk_is_refused_and_not_quoted_back() {
        let s = pullable("prose");
        let prose = "Welcome to Ubuntu 24.04 LTS and secret-ish words";
        let text = exit_text(fetch_desk_token(&s, answered(0, prose, "")).unwrap_err());
        assert!(text.contains("do not look like an access token"));
        assert!(!text.contains("Welcome"));
        assert!(!store(&s).unwrap().exists());
        let text = exit_text(
            fetch_desk_token(&s, answered(255, DESK_TOKEN, "a\nPermission denied\n")).unwrap_err(),
        );
        assert_eq!(
            text,
            "desk refused or was unreachable (exit 255): Permission denied"
        );
        assert!(!text.contains(DESK_TOKEN));
    }

    #[test]
    fn an_unreachable_desk_names_the_timeout_and_a_missing_client_says_so() {
        let s = pullable("timeout");
        let text = exit_text(fetch_desk_token(&s, |_, t| Err(SshError::Timeout(t))).unwrap_err());
        assert_eq!(text, "the desk did not answer within 20.0s");
        let text = exit_text(fetch_desk_token(&s, |_, _| Err(SshError::NoClient)).unwrap_err());
        assert!(text.contains("openssh-client"));
    }

    #[test]
    fn the_token_pattern_is_strict_on_charset_and_loose_on_length() {
        assert!(looks_like_token(DESK_TOKEN));
        assert!(looks_like_token(&"a".repeat(16)));
        assert!(!looks_like_token(&"a".repeat(15)));
        assert!(!looks_like_token(&"a".repeat(129)));
        assert!(!looks_like_token("abcdefghijklmnop qrst"));
        assert!(!looks_like_token("abcdefghijklmnop\n"));
    }
}
