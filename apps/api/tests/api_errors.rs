//! Ports `services/api/tests/test_api_errors.py` (problem documents, authentication, request
//! ids, rate limiting) and the GET paths of `test_api_auth.py` (`TestMe`, the token half of
//! `TestSecurity`). Cases needing an account run on the bed (`cfg(bed)`) and are reported
//! ignored, with the reason, elsewhere.
//!
//! Not ported here, and why: `TestPipelineDegraded` reads `/meta/status` (not yet ported —
//! the meta lane owns it); "a valid token authenticates" posts a screen (a write; the screens
//! lane); the Google sign-in, refresh/logout, PATCH/DELETE `/me`, export and restore tests are
//! writes (the auth lane, once the pool may write).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::sync::Arc;

use baskfy_api::ratelimit::RateLimiter;
use baskfy_providers::clock::{Clock, TokioClock};
use baskfy_providers::ratelimit::InMemoryScriptBackend;
use common::*;

// --- TestProblemDocuments -----------------------------------------------------------------

#[tokio::test]
async fn an_unrouted_path_is_problem_json_not_html() {
    let app = app(settings(DEAD_DATABASE), None);
    let r = call(&app, "GET", "/api/v1/nothing-here", &[]).await;
    let body = r.assert_problem(404, "not-found");
    assert_eq!(body["instance"], "/api/v1/nothing-here");
    assert_eq!(body["detail"], "Not Found");
}

#[tokio::test]
async fn every_problem_carries_the_instance_and_the_request_id() {
    // Python's test asks `/screens/unknown-id` with a live database (a 404 problem). Here the
    // database is dead, and since wave 4b routes /screens, that path would hit the DB-failure
    // 500 (which carries no request id, as Starlette's does) — so the same property is asked of
    // a path nothing routes (DECISIONS-RS R5.10).
    let app = app(settings(DEAD_DATABASE), None);
    let r = call(&app, "GET", "/api/v1/still-unrouted/unknown-id", &[]).await;
    let body = r.json();
    assert_eq!(body["instance"], "/api/v1/still-unrouted/unknown-id");
    let generated = r.header("x-request-id").unwrap().to_owned();
    assert_eq!(generated.len(), 32);
    assert!(
        generated
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    );
    assert_eq!(&generated[12..13], "4", "a uuid4");

    let other = call(&app, "GET", "/api/v1/still-unrouted/unknown-id", &[]).await;
    assert_ne!(other.header("x-request-id").unwrap(), generated);

    let supplied = "0123456789abcdef0123456789abcdef";
    let echoed = call(
        &app,
        "GET",
        "/api/v1/still-unrouted/unknown-id",
        &[("X-Request-Id", supplied)],
    )
    .await;
    assert_eq!(echoed.header("x-request-id"), Some(supplied));
}

#[tokio::test]
async fn an_unhandled_failure_becomes_a_500_problem_that_leaks_nothing() {
    // A database that cannot be reached under a valid token: SQLAlchemy would raise, the
    // catch-all answers. The cause is logged, never sent; no request id (Starlette's
    // ServerErrorMiddleware sits outside the request-id middleware).
    let mut s = settings(DEAD_DATABASE);
    s.db_pool_timeout_seconds = 0.5;
    let app = app(s, None);
    let r = call(
        &app,
        "GET",
        "/api/v1/me",
        &[("Authorization", &valid(OWNER))],
    )
    .await;
    let body = r.assert_problem(500, "internal-error");
    assert_eq!(body["detail"], "An unexpected error occurred.");
    assert!(!r.text().contains("127.0.0.1"), "{}", r.text());
    assert!(r.header("x-request-id").is_none());
}

// --- TestAuthentication -------------------------------------------------------------------

#[tokio::test]
async fn no_token_is_anonymous_not_an_error() {
    let app = app(settings(DEAD_DATABASE), None);
    assert_eq!(
        call(&app, "GET", "/api/v1/meta/factors", &[]).await.status,
        200
    );
}

#[tokio::test]
async fn a_garbage_token_is_401() {
    let app = app(settings(DEAD_DATABASE), None);
    let r = call(
        &app,
        "GET",
        "/api/v1/meta/factors",
        &[("Authorization", "Bearer not-a-token")],
    )
    .await;
    let body = r.assert_problem(401, "unauthenticated");
    assert_eq!(body["detail"], "The bearer token is not valid.");
    assert_eq!(r.header("www-authenticate"), Some("Bearer"));
}

#[tokio::test]
async fn a_token_signed_with_the_wrong_key_is_401() {
    let app = app(settings(DEAD_DATABASE), None);
    let header = bearer(
        OWNER,
        "a-different-secret-of-the-same-length!!",
        now(),
        600,
        0,
    );
    call(
        &app,
        "GET",
        "/api/v1/meta/factors",
        &[("Authorization", &header)],
    )
    .await
    .assert_problem(401, "unauthenticated");
}

#[tokio::test]
async fn an_expired_token_is_401() {
    let app = app(settings(DEAD_DATABASE), None);
    let header = bearer(OWNER, TEST_JWT_SECRET, now() - 7200, 60, 0);
    call(
        &app,
        "GET",
        "/api/v1/meta/factors",
        &[("Authorization", &header)],
    )
    .await
    .assert_problem(401, "unauthenticated");
}

#[tokio::test]
async fn an_over_long_lifetime_is_refused() {
    let app = app(settings(DEAD_DATABASE), None);
    let header = bearer(OWNER, TEST_JWT_SECRET, now(), 365 * 24 * 3600, 0);
    let body = call(
        &app,
        "GET",
        "/api/v1/meta/factors",
        &[("Authorization", &header)],
    )
    .await
    .assert_problem(401, "unauthenticated");
    assert_eq!(
        body["detail"],
        "The bearer token's lifetime exceeds the accepted maximum."
    );
}

#[tokio::test]
async fn an_unsigned_token_is_refused() {
    use base64::Engine;
    let e = base64::engine::general_purpose::URL_SAFE_NO_PAD;
    let header = e.encode(br#"{"alg":"none","typ":"JWT"}"#);
    let n = now();
    let payload = e.encode(format!(
        r#"{{"sub":"{OWNER}","iat":{n},"exp":{}}}"#,
        n + 300
    ));
    let forged = format!("Bearer {header}.{payload}.");
    let app = app(settings(DEAD_DATABASE), None);
    call(
        &app,
        "GET",
        "/api/v1/meta/factors",
        &[("Authorization", &forged)],
    )
    .await
    .assert_problem(401, "unauthenticated");
}

#[tokio::test]
async fn a_deployment_without_a_secret_verifies_nothing() {
    let mut s = settings(DEAD_DATABASE);
    s.jwt_secret = baskfy_providers::secret::Secret::default();
    let app = app(s, None);
    let body = call(
        &app,
        "GET",
        "/api/v1/meta/factors",
        &[("Authorization", &valid(OWNER))],
    )
    .await
    .assert_problem(401, "unauthenticated");
    assert_eq!(
        body["detail"],
        "This deployment cannot verify bearer tokens."
    );
}

#[tokio::test]
async fn me_needs_authentication() {
    let app = app(settings(DEAD_DATABASE), None);
    let r = call(&app, "GET", "/api/v1/me", &[]).await;
    let body = r.assert_problem(401, "unauthenticated");
    assert_eq!(body["detail"], "A valid bearer token is required.");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn a_token_for_an_unknown_account_is_401() {
    let app = app(settings(&bed_url()), None);
    let body = call(
        &app,
        "GET",
        "/api/v1/me",
        &[("Authorization", &valid("ghost0000000"))],
    )
    .await
    .assert_problem(401, "unauthenticated");
    assert_eq!(
        body["detail"],
        "The bearer token refers to an unknown account."
    );
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn a_valid_token_authenticates_and_me_carries_profile_and_entitlements() {
    let app = app(settings(&bed_url()), None);
    let r = call(
        &app,
        "GET",
        "/api/v1/me",
        &[("Authorization", &valid(OWNER))],
    )
    .await;
    assert_eq!(r.status, 200, "{}", r.text());
    let body = r.json();
    assert_eq!(body["public_id"], OWNER);
    assert_eq!(body["email_verified"], true);
    let keys: Vec<&str> = body["entitlements"]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    let mut expected = vec![
        "screener",
        "export_csv",
        "custom_columns",
        "historical_ranks",
        "backtests",
        "api_access",
        "max_screens",
    ];
    let mut got = keys.clone();
    got.sort_unstable();
    expected.sort_unstable();
    assert_eq!(got, expected);
    // The genuine token works, a forged one for the same account does not (TestSecurity).
    let forged = bearer(
        OWNER,
        "a-different-secret-that-is-long-enough-32",
        now(),
        600,
        0,
    );
    call(&app, "GET", "/api/v1/me", &[("Authorization", &forged)])
        .await
        .assert_problem(401, "unauthenticated");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn a_token_older_than_the_accounts_session_epoch_is_revoked() {
    // TestSessionEpoch's read half: the account's epoch is 0 on the bed, a token stamped -1
    // predates it, a token stamped 0 or later is current.
    let app = app(settings(&bed_url()), None);
    let stale = bearer(OWNER, TEST_JWT_SECRET, now(), 600, -1);
    let body = call(&app, "GET", "/api/v1/me", &[("Authorization", &stale)])
        .await
        .assert_problem(401, "unauthenticated");
    assert_eq!(body["detail"], "The bearer token has been revoked.");
    let ahead = bearer(OWNER, TEST_JWT_SECRET, now(), 600, 5);
    let me = call(&app, "GET", "/api/v1/me", &[("Authorization", &ahead)]).await;
    assert_eq!(me.status, 200);
    assert_eq!(me.json()["session_epoch"], 0);
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn each_account_sees_only_itself() {
    let app = app(settings(&bed_url()), None);
    for id in [OWNER, PLAIN, SUBSCRIBER] {
        let me = call(&app, "GET", "/api/v1/me", &[("Authorization", &valid(id))]).await;
        assert_eq!(me.json()["public_id"], id);
    }
    let sub = call(
        &app,
        "GET",
        "/api/v1/me",
        &[("Authorization", &valid(SUBSCRIBER))],
    )
    .await
    .json();
    assert_eq!(sub["plan_code"], "forever");
    assert_eq!(sub["entitlements"]["backtests"], true);
    let plain = call(
        &app,
        "GET",
        "/api/v1/me",
        &[("Authorization", &valid(PLAIN))],
    )
    .await
    .json();
    assert_eq!(plain["entitlements"]["backtests"], false);
    assert_eq!(plain["entitlements"]["max_screens"], 5);
    assert_eq!(plain["is_staff"], false);
}

// --- TestRequestIds -----------------------------------------------------------------------

#[tokio::test]
async fn every_response_carries_a_request_id_and_an_inbound_one_is_preserved() {
    let app = app(settings(DEAD_DATABASE), None);
    let first = call(&app, "GET", "/api/v1/meta/columns", &[]).await;
    assert_eq!(first.header("x-request-id").unwrap().len(), 32);
    let edge = call(
        &app,
        "GET",
        "/api/v1/meta/columns",
        &[("X-Request-Id", "edge-request-1")],
    )
    .await;
    assert_eq!(edge.header("x-request-id"), Some("edge-request-1"));
    let second = call(&app, "GET", "/api/v1/meta/columns", &[]).await;
    assert_ne!(first.header("x-request-id"), second.header("x-request-id"));
    // An empty inbound id is replaced, not echoed.
    let blank = call(&app, "GET", "/health", &[("X-Request-Id", "")]).await;
    assert_eq!(blank.header("x-request-id").unwrap().len(), 32);
}

// --- TestRateLimiting ---------------------------------------------------------------------

fn limiter() -> RateLimiter {
    let clock: Arc<dyn Clock> = Arc::new(TokioClock::new());
    RateLimiter::with_backend(
        Arc::new(InMemoryScriptBackend::new(Arc::clone(&clock))),
        clock,
    )
}

#[tokio::test(start_paused = true)]
async fn anonymous_traffic_is_capped_at_ten_a_minute() {
    let mut s = settings(DEAD_DATABASE);
    s.rate_limit_enabled = true;
    s.rate_limit_anonymous_per_minute = 10;
    s.rate_limit_authenticated_per_minute = 60;
    let app = app(s, Some(limiter()));
    let mut statuses = Vec::new();
    for _ in 0..12 {
        statuses.push(
            call(&app, "GET", "/api/v1/meta/columns", &[])
                .await
                .status
                .as_u16(),
        );
    }
    assert_eq!(&statuses[..10], &[200; 10]);
    assert_eq!(&statuses[10..], &[429, 429]);
    let refused = call(&app, "GET", "/api/v1/meta/columns", &[]).await;
    let body = refused.assert_problem(429, "rate-limited");
    assert_eq!(body["limit_per_minute"], 10);
    assert_eq!(
        body["detail"],
        "Rate limit of 10 requests per minute exceeded."
    );
    let retry: i64 = refused.header("retry-after").unwrap().parse().unwrap();
    assert!(retry >= 1);
    assert_eq!(body["retry_after"], retry);
    // A minute later the bucket has refilled.
    tokio::time::advance(std::time::Duration::from_secs(60)).await;
    assert_eq!(
        call(&app, "GET", "/api/v1/meta/columns", &[]).await.status,
        200
    );
}

#[tokio::test(start_paused = true)]
async fn an_unrouted_path_and_health_cost_nothing() {
    let mut s = settings(DEAD_DATABASE);
    s.rate_limit_enabled = true;
    s.rate_limit_anonymous_per_minute = 1;
    let app = app(s, Some(limiter()));
    for _ in 0..5 {
        assert_eq!(call(&app, "GET", "/health", &[]).await.status, 200);
        assert_eq!(call(&app, "GET", "/api/v1/nope", &[]).await.status, 404);
    }
    assert_eq!(
        call(&app, "GET", "/api/v1/meta/columns", &[]).await.status,
        200
    );
    assert_eq!(
        call(&app, "GET", "/api/v1/meta/columns", &[]).await.status,
        429
    );
}

#[tokio::test(start_paused = true)]
async fn a_bad_token_is_refused_before_the_limit_is_charged() {
    let mut s = settings(DEAD_DATABASE);
    s.rate_limit_enabled = true;
    s.rate_limit_anonymous_per_minute = 1;
    let app = app(s, Some(limiter()));
    for _ in 0..3 {
        call(
            &app,
            "GET",
            "/api/v1/meta/columns",
            &[("Authorization", "Bearer x")],
        )
        .await
        .assert_problem(401, "unauthenticated");
    }
    assert_eq!(
        call(&app, "GET", "/api/v1/meta/columns", &[]).await.status,
        200
    );
}

#[tokio::test]
async fn an_unreachable_redis_fails_closed() {
    let mut s = settings(DEAD_DATABASE);
    s.rate_limit_enabled = true;
    let limiter = RateLimiter::new(baskfy_providers::secret::Secret::new(
        "redis://127.0.0.1:9/0",
    ));
    let app = app(s, Some(limiter));
    let body = call(&app, "GET", "/api/v1/meta/columns", &[])
        .await
        .assert_problem(503, "pipeline-degraded");
    assert_eq!(
        body["detail"],
        "Rate limiting is unavailable, so the request was refused."
    );
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn an_authenticated_caller_gets_the_higher_limit() {
    let mut s = settings(&bed_url());
    s.rate_limit_enabled = true;
    s.rate_limit_anonymous_per_minute = 2;
    s.rate_limit_authenticated_per_minute = 40;
    let app = app(s, Some(limiter()));
    let auth = valid(OWNER);
    let mut statuses = Vec::new();
    for _ in 0..12 {
        statuses.push(
            call(
                &app,
                "GET",
                "/api/v1/meta/columns",
                &[("Authorization", &auth)],
            )
            .await
            .status
            .as_u16(),
        );
    }
    assert_eq!(
        statuses,
        vec![200; 12],
        "an authenticated caller must not hit the anonymous cap"
    );
}
