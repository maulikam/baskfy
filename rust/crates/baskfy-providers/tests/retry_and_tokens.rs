//! Retry policy and encrypted token storage — ports `tests/test_retry_and_tokens.py`.
//!
//! docs/09: the access token is daily, stored encrypted, and its expiry must "alert loudly".
//! docs/11: "Kite access token encrypted at rest."
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

mod common;

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};

use baskfy_providers::errors::{ErrorKind, ProviderError};
use baskfy_providers::retry::{RetryHooks, RetryPolicy, call_with_retry};
use baskfy_providers::secret::Secret;
use baskfy_providers::tokens::{AccessToken, AccessTokenStore, ist};
use chrono::{DateTime, Duration, FixedOffset, TimeZone};
use common::{RecordingSleeper, arm};

fn issued() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 8, 20, 9, 15, 0).unwrap()
}

fn at(y: i32, m: u32, d: u32, h: u32, min: u32) -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(y, m, d, h, min, 0).unwrap()
}

fn new_key() -> Secret {
    Secret::new(fernet::Fernet::generate_key())
}

fn store(dir: &tempfile::TempDir) -> AccessTokenStore {
    AccessTokenStore::new(dir.path().join("kite.enc"), new_key())
}

fn quiet_hooks() -> RetryHooks {
    RetryHooks {
        sleeper: Arc::new(RecordingSleeper::default()),
        ..RetryHooks::default()
    }
}

async fn fail() -> Result<(), ProviderError> {
    Err(ProviderError::upstream_unavailable("503"))
}

// --- TestRetryPolicy ----------------------------------------------------------------------

#[test]
fn backoff_doubles() {
    arm();
    let policy = RetryPolicy::new(5, 0.5, 30.0).unwrap();
    let ceilings: Vec<f64> = (1..=4)
        .map(|n| policy.backoff_ceiling(n).unwrap())
        .collect();
    assert_eq!(ceilings, vec![0.5, 1.0, 2.0, 4.0]);
}

#[test]
fn backoff_is_capped() {
    arm();
    let policy = RetryPolicy::new(5, 1.0, 8.0).unwrap();
    assert_eq!(policy.backoff_ceiling(10).unwrap(), 8.0);
}

#[test]
fn attempts_are_one_based() {
    arm();
    let err = RetryPolicy::default().backoff_ceiling(0).unwrap_err();
    assert!(err.to_string().contains("1-based"));
}

#[test]
fn configuration_is_validated() {
    arm();
    assert!(
        RetryPolicy::new(0, 0.5, 30.0)
            .unwrap_err()
            .to_string()
            .contains("max_attempts")
    );
    assert!(
        RetryPolicy::new(5, 0.0, 30.0)
            .unwrap_err()
            .to_string()
            .contains("base_seconds")
    );
    assert!(
        RetryPolicy::new(5, 10.0, 1.0)
            .unwrap_err()
            .to_string()
            .contains("max_seconds")
    );
}

// --- TestJitter ---------------------------------------------------------------------------

#[test]
fn the_default_jitter_is_full_not_symmetric() {
    arm();
    let hooks = RetryHooks::default();
    let draws: Vec<f64> = (0..200).map(|_| hooks.draw(4.0)).collect();
    assert!(draws.iter().all(|d| (0.0..=4.0).contains(d)));
    assert!(draws.iter().copied().fold(f64::INFINITY, f64::min) < 1.0);
    assert!(draws.iter().copied().fold(f64::NEG_INFINITY, f64::max) > 3.0);
}

#[test]
fn jitter_is_injectable_for_deterministic_tests() {
    arm();
    let hooks = RetryHooks {
        jitter: Some(Arc::new(|ceiling| ceiling / 2.0)),
        ..RetryHooks::default()
    };
    assert_eq!(hooks.draw(4.0), 2.0);
}

// --- TestRetryLoop ------------------------------------------------------------------------

#[tokio::test]
async fn a_success_is_returned_without_sleeping() {
    arm();
    let sleeper = Arc::new(RecordingSleeper::default());
    let hooks = RetryHooks {
        sleeper: sleeper.clone(),
        ..RetryHooks::default()
    };
    let result = call_with_retry(
        || async { Ok::<_, ProviderError>("ok") },
        &RetryPolicy::default(),
        None,
        Some(&hooks),
    )
    .await
    .unwrap();
    assert_eq!(result, "ok");
    assert!(sleeper.sleeps().is_empty());
}

#[tokio::test]
async fn it_stops_at_max_attempts() {
    arm();
    let calls = AtomicU32::new(0);
    let hooks = RetryHooks {
        jitter: Some(Arc::new(|c| c)),
        ..quiet_hooks()
    };
    let err = call_with_retry(
        || {
            calls.fetch_add(1, Ordering::SeqCst);
            fail()
        },
        &RetryPolicy::new(3, 0.5, 30.0).unwrap(),
        None,
        Some(&hooks),
    )
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::RetryBudgetExhausted);
    assert_eq!(calls.load(Ordering::SeqCst), 3);
}

#[tokio::test]
async fn the_final_error_is_carried() {
    arm();
    let err = call_with_retry(
        fail,
        &RetryPolicy::new(2, 0.5, 30.0).unwrap(),
        Some("kite"),
        Some(&quiet_hooks()),
    )
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::RetryBudgetExhausted);
    assert_eq!(
        err.last_error().unwrap().kind(),
        ErrorKind::UpstreamUnavailable
    );
    assert_eq!(err.attempts(), Some(2));
    assert!(err.to_string().contains("kite"));
}

#[tokio::test]
async fn a_non_transient_error_is_not_retried() {
    arm();
    let calls = AtomicU32::new(0);
    let err = call_with_retry(
        || {
            calls.fetch_add(1, Ordering::SeqCst);
            async { Err::<(), _>(ProviderError::credentials_missing("no key")) }
        },
        &RetryPolicy::new(5, 0.5, 30.0).unwrap(),
        None,
        Some(&quiet_hooks()),
    )
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CredentialsMissing);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn a_foreign_error_is_not_retried_either() {
    // Python's `except TransientProviderError` lets a ValueError or a redis error through.
    arm();
    let calls = AtomicU32::new(0);
    let err = call_with_retry(
        || {
            calls.fetch_add(1, Ordering::SeqCst);
            async { Err::<(), _>(ProviderError::foreign("ConnectionError", "redis down")) }
        },
        &RetryPolicy::default(),
        None,
        Some(&quiet_hooks()),
    )
    .await
    .unwrap_err();
    assert_eq!(err.python_type(), "ConnectionError");
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn the_retry_hook_observes_each_attempt() {
    arm();
    let observed = Arc::new(Mutex::new(Vec::<(u32, f64)>::new()));
    let sink = observed.clone();
    let hooks = RetryHooks {
        jitter: Some(Arc::new(|c| c)),
        on_retry: Some(Arc::new(move |attempt, delay, _e: &ProviderError| {
            sink.lock().unwrap().push((attempt, delay));
        })),
        ..quiet_hooks()
    };
    let err = call_with_retry(
        fail,
        &RetryPolicy::new(3, 1.0, 30.0).unwrap(),
        None,
        Some(&hooks),
    )
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::RetryBudgetExhausted);
    assert_eq!(*observed.lock().unwrap(), vec![(1, 1.0), (2, 2.0)]);
}

#[tokio::test]
async fn a_single_attempt_policy_never_sleeps() {
    arm();
    let sleeper = Arc::new(RecordingSleeper::default());
    let hooks = RetryHooks {
        sleeper: sleeper.clone(),
        ..RetryHooks::default()
    };
    let err = call_with_retry(
        fail,
        &RetryPolicy::new(1, 0.5, 30.0).unwrap(),
        None,
        Some(&hooks),
    )
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::RetryBudgetExhausted);
    assert!(sleeper.sleeps().is_empty());
}

// --- TestTokenExpiry ----------------------------------------------------------------------

#[test]
fn a_token_issued_today_is_fresh() {
    arm();
    let token = AccessToken::new("abc", issued());
    assert!(!token.is_expired(issued() + Duration::hours(14)));
}

#[test]
fn a_token_is_expired_once_the_ist_day_rolls_over() {
    arm();
    let token = AccessToken::new("abc", at(2026, 8, 20, 23, 55));
    assert!(token.is_expired(at(2026, 8, 21, 0, 5)));
}

#[test]
fn expiry_is_judged_in_ist_not_utc() {
    arm();
    let issued = at(2026, 8, 20, 20, 0); // 14:30 UTC
    let later = at(2026, 8, 21, 1, 0); // 19:30 UTC the same UTC day
    assert!(AccessToken::new("abc", issued).is_expired(later));
}

// --- TestTokenStore -----------------------------------------------------------------------

#[test]
fn round_trip() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let s = store(&dir);
    s.save("secret-token", Some(issued())).unwrap();
    let loaded = s.load().unwrap();
    assert_eq!(loaded.value.expose(), "secret-token");
    assert_eq!(loaded.issued_at, issued());
}

#[test]
fn the_file_is_encrypted_at_rest() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let s = store(&dir);
    s.save("secret-token", Some(issued())).unwrap();
    let bytes = std::fs::read(s.path()).unwrap();
    assert!(!bytes.windows(12).any(|w| w == b"secret-token"));
}

#[cfg(unix)]
#[test]
fn the_file_is_not_world_readable() {
    use std::os::unix::fs::PermissionsExt;
    arm();
    let dir = tempfile::tempdir().unwrap();
    let s = store(&dir);
    s.save("secret-token", Some(issued())).unwrap();
    let mode = std::fs::metadata(s.path()).unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o600);
}

#[test]
fn require_fresh_returns_a_same_day_token() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let s = store(&dir);
    s.save("secret-token", Some(issued())).unwrap();
    let token = s
        .require_fresh(Some(issued() + Duration::hours(6)))
        .unwrap();
    assert_eq!(token.value.expose(), "secret-token");
}

#[test]
fn require_fresh_raises_loudly_once_stale() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let s = store(&dir);
    s.save("secret-token", Some(issued())).unwrap();
    let err = s
        .require_fresh(Some(issued() + Duration::days(1)))
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::AccessTokenExpired);
    assert!(err.to_string().contains("login flow"));
    assert!(err.to_string().contains("was issued on 2026-08-20"));
}

#[test]
fn a_missing_file_is_credentials_missing() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let err = store(&dir).load().unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CredentialsMissing);
    assert!(err.to_string().contains("no Kite access token"));
}

#[test]
fn an_empty_token_is_refused() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let err = store(&dir).save("", None).unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert!(err.to_string().contains("empty"));
}

#[test]
fn no_encryption_key_is_credentials_missing() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let err = AccessTokenStore::new(dir.path().join("k.enc"), Secret::default())
        .save("abc", None)
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CredentialsMissing);
    assert!(err.to_string().contains("ENCRYPTION_KEY"));
}

#[test]
fn an_invalid_encryption_key_is_reported_clearly() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let err = AccessTokenStore::new(dir.path().join("k.enc"), Secret::new("not-a-fernet-key"))
        .save("abc", None)
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CredentialsMissing);
    assert!(err.to_string().contains("valid Fernet key"));
}

#[test]
fn a_rotated_key_is_diagnosed_rather_than_crashing() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("k.enc");
    AccessTokenStore::new(&path, new_key())
        .save("abc", Some(issued()))
        .unwrap();
    let err = AccessTokenStore::new(&path, new_key()).load().unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CredentialsMissing);
    assert!(err.to_string().contains("rotated"));
}

#[test]
fn a_corrupt_payload_is_reported_as_such() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let key = new_key();
    let path = dir.path().join("k.enc");
    let blob = fernet::Fernet::new(key.expose())
        .unwrap()
        .encrypt(b"this is not json");
    std::fs::write(&path, blob).unwrap();
    let err = AccessTokenStore::new(&path, key).load().unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
}

#[test]
fn the_secret_never_prints() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let key = new_key();
    let s = AccessTokenStore::new(dir.path().join("kite.enc"), key.clone());
    let token = s.save("secret-token", Some(issued())).unwrap();
    assert!(!format!("{token:?}").contains("secret-token"));
    assert!(!format!("{s:?}").contains(key.expose()));
}

// --- Interop with Python's AccessTokenStore -------------------------------------------------

fn python_fixture() -> serde_json::Value {
    let text = include_str!("fixtures/python-token-blob.json");
    serde_json::from_str(text).unwrap()
}

#[test]
fn a_blob_python_wrote_is_read_by_rust() {
    arm();
    let fx = python_fixture();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("k.enc");
    std::fs::write(&path, fx["blob"].as_str().unwrap()).unwrap();
    let token = AccessTokenStore::new(&path, Secret::new(fx["key"].as_str().unwrap()))
        .load()
        .unwrap();
    assert_eq!(token.value.expose(), fx["token"].as_str().unwrap());
    let expected = DateTime::parse_from_rfc3339(fx["issued_at"].as_str().unwrap()).unwrap();
    assert_eq!(token.issued_at, expected);
}

#[test]
fn rust_writes_python_s_plaintext_byte_for_byte() {
    arm();
    let fx = python_fixture();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("k.enc");
    let key = Secret::new(fx["key"].as_str().unwrap());
    let issued_at = DateTime::parse_from_rfc3339(fx["issued_at"].as_str().unwrap()).unwrap();
    AccessTokenStore::new(&path, key.clone())
        .save(fx["token"].as_str().unwrap(), Some(issued_at))
        .unwrap();
    let blob = std::fs::read_to_string(&path).unwrap();
    let plaintext = fernet::Fernet::new(key.expose())
        .unwrap()
        .decrypt(&blob)
        .unwrap();
    assert_eq!(
        String::from_utf8(plaintext).unwrap(),
        fx["plaintext"].as_str().unwrap()
    );
}
