//! Lane **swing_monitor**: `tests/test_swing_monitor.py::TestTheDeskRereadsAFreshToken`, the
//! four tests that guard SW18 for the monitor process — the desk's Kite wrapper re-reads the
//! token blob when its mtime moves, so a morning login reaches a long-running monitor without a
//! restart; an unchanged blob costs one stat and no reload; `is_authed` refreshes first; a
//! missing blob is not an error. The broker is the replay fake (writes refused, never sent).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, RwLock};

use baskfy_desk::clock::{Clock, FixedClock};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::lanes::exec_core::kite_client::DeskKite;
use baskfy_desk::lanes::exec_core::token_store::store_for;
use baskfy_desk::settings::{Settings, SharedSettings};

const FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {"user.profile": {"data": {"user_id": "AB1234"}}}}"#;

struct Client {
    kite: DeskKite,
    kc: Arc<ReplayKite>,
    path: std::path::PathBuf,
    _dir: tempfile::TempDir,
}

/// `_client(tmp_path, monkeypatch, token="first-token")`: no encryption key configured (the
/// fallback key beside the blob), a key for `Kite()`, the token saved before the client is
/// built.
fn client(token: &str) -> Client {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("kite-token.enc");
    let mut s = Settings::defaults().unwrap();
    s.token_file = path.display().to_string();
    s.kite_api_key = "key".to_owned();
    s.force_ipv4 = false;
    s.shared_read_limits = false;
    s.shared_read_limits_redis_url = String::new();
    store_for(&path, "").unwrap().save(token, None).unwrap();
    let settings: SharedSettings = Arc::new(RwLock::new(s));
    std::fs::write(dir.path().join("kite.json"), FIXTURE).unwrap();
    let kc = Arc::new(ReplayKite::load(dir.path(), None, "key").unwrap());
    let at = chrono::DateTime::parse_from_rfc3339("2026-08-19T09:10:00+05:30").unwrap();
    let kite = DeskKite::new(
        kc.clone(),
        settings,
        Arc::new(FixedClock::new(at)) as Arc<dyn Clock>,
    );
    Client {
        kite,
        kc,
        path,
        _dir: dir,
    }
}

/// The token the blob holds now (what a reload hands the broker).
fn blob(c: &Client) -> String {
    store_for(&c.path, "")
        .unwrap()
        .load()
        .unwrap()
        .value
        .expose()
        .to_owned()
}

/// Another process saves a new token, and its mtime moves on.
fn rewrite(c: &Client, token: &str) {
    store_for(&c.path, "").unwrap().save(token, None).unwrap();
    let file = std::fs::File::options().write(true).open(&c.path).unwrap();
    let later = std::fs::metadata(&c.path).unwrap().modified().unwrap()
        + std::time::Duration::from_secs(10);
    file.set_modified(later).unwrap();
}

#[test]
fn test_a_token_written_after_start_is_picked_up_without_a_restart() {
    let c = client("first-token");
    c.kite.ready().unwrap();
    assert!(
        !c.kite.refresh_token_if_changed().unwrap(),
        "loaded at start"
    );
    rewrite(&c, "second-token");
    assert!(c.kite.refresh_token_if_changed().unwrap());
    assert_eq!(blob(&c), "second-token");
    assert!(!c.kite.refresh_token_if_changed().unwrap(), "reloaded once");
    assert!(c.kc.write_calls().is_empty());
}

#[test]
fn test_an_unchanged_blob_costs_one_stat_and_no_reload() {
    let c = client("first-token");
    assert!(!c.kite.refresh_token_if_changed().unwrap());
    assert!(!c.kite.refresh_token_if_changed().unwrap());
    assert_eq!(blob(&c), "first-token");
    assert!(c.kc.calls().is_empty(), "a stat, never a broker call");
}

#[tokio::test]
async fn test_is_authed_refreshes_first() {
    let c = client("first-token");
    c.kite.ready().unwrap();
    rewrite(&c, "third-token");
    assert!(c.kite.is_authed().await);
    assert!(
        !c.kite.refresh_token_if_changed().unwrap(),
        "is_authed already reloaded the new blob"
    );
    assert_eq!(blob(&c), "third-token");
}

#[test]
fn test_a_missing_blob_is_not_an_error() {
    let c = client("first-token");
    c.kite.ready().unwrap();
    std::fs::remove_file(&c.path).unwrap();
    assert!(!c.kite.refresh_token_if_changed().unwrap());
}
