//! Lane **swing_monitor**: `tests/test_swing_monitor.py::TestTheDeskRereadsAFreshToken`, the
//! four tests that guard SW18 for the monitor process — the desk's Kite wrapper re-reads the
//! token blob when its mtime moves, so a morning login reaches a long-running monitor without a
//! restart; an unchanged blob costs one stat and no reload; `is_authed` refreshes first; a
//! missing blob is not an error.
//!
//! It lives under `tests/` because the broker double must implement the order-verb trait, which
//! only the gateway's lane may name inside `src/` (law 2, `non_negotiables.rs`). The double
//! records `set_access_token` and answers `profile`; everything else refuses. No network.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex, RwLock};

use async_trait::async_trait;
use baskfy_desk::clock::{Clock, FixedClock};
use baskfy_desk::kite::{
    GttParams, KiteError, KiteExceptionKind, KiteReads, KiteWrites, OrderParams,
};
use baskfy_desk::lanes::exec_core::kite_client::DeskKite;
use baskfy_desk::lanes::exec_core::token_store::store_for;
use baskfy_desk::settings::{Settings, SharedSettings};
use baskfy_desk::wire::{PyDict, PyValue};
use baskfy_providers::secret::Secret;

/// `FakeKC`: `set_access_token` recorded, `profile` answers.
#[derive(Default)]
struct FakeKc {
    seen: Mutex<Vec<String>>,
}

fn refused() -> KiteError {
    KiteError::Kite {
        kind: KiteExceptionKind::General,
        message: "not used by this test".to_owned(),
        code: 500,
    }
}

#[async_trait]
impl KiteReads for FakeKc {
    fn login_url(&self) -> String {
        String::new()
    }
    fn set_access_token(&self, token: &Secret) {
        self.seen.lock().unwrap().push(token.expose().to_owned());
    }
    async fn generate_session(&self, _r: &str, _s: &Secret) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn profile(&self) -> Result<PyValue, KiteError> {
        let mut m = PyDict::default();
        m.insert("user_id", PyValue::Str("AB1234".to_owned()));
        Ok(PyValue::Map(m))
    }
    async fn holdings(&self) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn positions(&self) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn margins(&self, _s: Option<&str>) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn ltp(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn ohlc(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn quote(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn instruments(&self, _e: Option<&str>) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn historical_data(
        &self,
        _t: i64,
        _f: &str,
        _to: &str,
        _i: &str,
        _c: bool,
        _o: bool,
    ) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn orders(&self) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn order_history(&self, _o: &str) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn trades(&self) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn get_gtts(&self) -> Result<PyValue, KiteError> {
        Err(refused())
    }
    async fn basket_order_margins(
        &self,
        _p: &[PyValue],
        _c: bool,
        _m: Option<&str>,
    ) -> Result<PyValue, KiteError> {
        Err(refused())
    }
}

#[async_trait]
impl KiteWrites for FakeKc {
    async fn place_order(&self, _o: &OrderParams) -> Result<String, KiteError> {
        panic!("the token test placed an order");
    }
    async fn modify_order(&self, _v: &str, _o: &str, _p: &PyDict) -> Result<String, KiteError> {
        panic!("the token test modified an order");
    }
    async fn cancel_order(
        &self,
        _v: &str,
        _o: &str,
        _p: Option<&str>,
    ) -> Result<String, KiteError> {
        panic!("the token test cancelled an order");
    }
    async fn place_gtt(&self, _g: &GttParams) -> Result<PyValue, KiteError> {
        panic!("the token test placed a GTT");
    }
    async fn modify_gtt(&self, _t: i64, _g: &GttParams) -> Result<PyValue, KiteError> {
        panic!("the token test modified a GTT");
    }
    async fn delete_gtt(&self, _t: i64) -> Result<PyValue, KiteError> {
        panic!("the token test deleted a GTT");
    }
}

struct Client {
    kite: DeskKite,
    kc: Arc<FakeKc>,
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
    let kc = Arc::new(FakeKc::default());
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

fn seen(c: &Client) -> Vec<String> {
    c.kc.seen.lock().unwrap().clone()
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
    assert_eq!(seen(&c), ["first-token"]);
    rewrite(&c, "second-token");
    assert!(c.kite.refresh_token_if_changed().unwrap());
    assert_eq!(seen(&c).last().unwrap(), "second-token");
}

#[test]
fn test_an_unchanged_blob_costs_one_stat_and_no_reload() {
    let c = client("first-token");
    assert!(!c.kite.refresh_token_if_changed().unwrap());
    assert!(!c.kite.refresh_token_if_changed().unwrap());
    assert_eq!(seen(&c), ["first-token"]);
}

#[tokio::test]
async fn test_is_authed_refreshes_first() {
    let c = client("first-token");
    c.kite.ready().unwrap();
    rewrite(&c, "third-token");
    assert!(c.kite.is_authed().await);
    assert_eq!(seen(&c).last().unwrap(), "third-token");
}

#[test]
fn test_a_missing_blob_is_not_an_error() {
    let c = client("first-token");
    c.kite.ready().unwrap();
    std::fs::remove_file(&c.path).unwrap();
    assert!(!c.kite.refresh_token_if_changed().unwrap());
}
