//! Shared setup for the API's HTTP tests — `api_helpers.py` in Rust.
#![allow(dead_code, clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{HeaderMap, Method, Request, StatusCode};
use baskfy_api::ratelimit::RateLimiter;
use baskfy_api::{AppState, Settings, build_app};
use baskfy_db::Db;
use baskfy_providers::secret::Secret;
use tower::Service;

/// `TEST_JWT_SECRET`: 32 bytes, as RFC 7518 §3.2 wants.
pub const TEST_JWT_SECRET: &str = "baskfy-test-secret-0123456789abc";

/// `PROBLEM_MEDIA_TYPE`.
pub const PROBLEM: &str = "application/problem+json";

/// The bed's `owner` account (`tools/parity/api_diff.py` USERS).
pub const OWNER: &str = "2fe482ed2015";
/// A plain account on the bed.
pub const PLAIN: &str = "a0a1fd14fa3e";
/// The bed's account with an active forever subscription.
pub const SUBSCRIBER: &str = "0159baaac142";

/// A loopback port nothing listens on: a database "down" without any network.
pub const DEAD_DATABASE: &str = "postgresql+asyncpg://u:p@127.0.0.1:9/none";

/// `api_settings(database_url)`: the production path with limits that do not bite.
pub fn settings(database_url: &str) -> Settings {
    Settings {
        database_url: Secret::new(database_url),
        environment: "test".to_owned(),
        jwt_secret: Secret::new(TEST_JWT_SECRET),
        rate_limit_anonymous_per_minute: 100_000,
        rate_limit_authenticated_per_minute: 100_000,
        rate_limit_auth_per_minute: 100_000,
        rate_limit_enabled: false,
        cookie_secure: false,
        db_pool_timeout_seconds: 2.0,
        ..Settings::default()
    }
}

/// The bed's URL, when this build was made with it (`cfg(bed)`).
pub fn bed_url() -> String {
    std::env::var("BASKFY_RS_TEST_DATABASE_URL").expect("cfg(bed) implies the variable")
}

/// The app over `settings`, with an optional injected limiter.
pub fn app(settings: Settings, limiter: Option<RateLimiter>) -> Router {
    // Keep the test output readable: warnings and errors only, one text line each.
    baskfy_api::logging::configure("WARNING", false);
    let db = Db::connect_lazy(&settings.db_settings()).unwrap();
    build_app(AppState {
        settings: Arc::new(settings),
        db,
        limiter: limiter.map(Arc::new),
        write: baskfy_api::state::WriteAccess::none(),
    })
}

/// A response, read whole.
pub struct Answer {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: Vec<u8>,
}

impl Answer {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(name).and_then(|v| v.to_str().ok())
    }

    pub fn json(&self) -> serde_json::Value {
        serde_json::from_slice(&self.body)
            .unwrap_or_else(|e| panic!("not JSON ({e}): {}", String::from_utf8_lossy(&self.body)))
    }

    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }

    /// `assert_problem(response, status, type)`.
    pub fn assert_problem(&self, status: u16, kind: &str) -> serde_json::Value {
        assert_eq!(self.status.as_u16(), status, "{}", self.text());
        assert!(
            self.header("content-type")
                .unwrap_or("")
                .starts_with(PROBLEM),
            "{:?}",
            self.header("content-type")
        );
        let body = self.json();
        assert_eq!(body["type"], kind, "{body}");
        assert_eq!(body["status"], status);
        body
    }
}

/// Sends one request through the whole app.
pub async fn call(app: &Router, method: &str, uri: &str, headers: &[(&str, &str)]) -> Answer {
    let mut builder = Request::builder()
        .method(Method::from_bytes(method.as_bytes()).unwrap())
        .uri(uri)
        .header("host", "testserver");
    for (k, v) in headers {
        builder = builder.header(*k, *v);
    }
    let request = builder.body(Body::empty()).unwrap();
    let response = app.clone().call(request).await.unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let body = to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    Answer {
        status,
        headers,
        body,
    }
}

/// Unix seconds now.
pub fn now() -> i64 {
    i64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs(),
    )
    .unwrap()
}

/// `bearer(public_id, ...)`: an `Authorization` value for a token minted like the web app's.
pub fn bearer(public_id: &str, secret: &str, issued_at: i64, lifetime: i64, epoch: i64) -> String {
    let token =
        baskfy_api::auth::encode_token(public_id, secret.as_bytes(), issued_at, lifetime, epoch);
    format!("Bearer {token}")
}

/// A fresh, valid bearer for `public_id` with the test secret.
pub fn valid(public_id: &str) -> String {
    bearer(public_id, TEST_JWT_SECRET, now(), 600, 0)
}
