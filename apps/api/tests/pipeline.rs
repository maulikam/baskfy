//! The request pipeline over HTTP: Starlette's routing rules, the HTTP cache (the wired half of
//! `test_http_cache.py`), CORS, and the problem documents for framework errors. No database:
//! every route here is served from `baskfy_core`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use common::*;

fn fresh() -> axum::Router {
    app(settings(DEAD_DATABASE), None)
}

#[tokio::test]
async fn a_published_read_carries_a_validator_and_presenting_it_back_is_a_304() {
    let app = fresh();
    let first = call(&app, "GET", "/api/v1/meta/columns", &[]).await;
    assert_eq!(first.status, 200);
    let etag = first.header("etag").unwrap().to_owned();
    assert!(
        etag.starts_with("W/\""),
        "a versionless route still gets a weak tag: {etag}"
    );
    assert_eq!(
        first.header("cache-control"),
        Some("private, max-age=0, must-revalidate, stale-while-revalidate=60")
    );
    assert_eq!(first.header("vary"), Some("Authorization"));
    assert_eq!(
        first.header("content-length").unwrap(),
        first.body.len().to_string()
    );

    let again = call(&app, "GET", "/api/v1/meta/columns", &[]).await;
    assert_eq!(
        again.header("etag").unwrap(),
        etag,
        "the validator is stable"
    );

    let not_modified = call(
        &app,
        "GET",
        "/api/v1/meta/columns",
        &[("If-None-Match", &etag)],
    )
    .await;
    assert_eq!(not_modified.status, 304);
    assert!(not_modified.body.is_empty());
    assert_eq!(not_modified.header("etag").unwrap(), etag);
    assert!(not_modified.header("content-length").is_none());
    assert!(
        not_modified.header("x-request-id").is_some(),
        "the request id wraps the cache"
    );

    let stale = call(
        &app,
        "GET",
        "/api/v1/meta/columns",
        &[("If-None-Match", "W/\"v1-x\"")],
    )
    .await;
    assert_eq!(stale.status, 200);
    assert_eq!(stale.body, first.body);
}

#[tokio::test]
async fn per_user_and_ops_routes_get_no_validator() {
    let app = fresh();
    let health = call(&app, "GET", "/health", &[]).await;
    assert_eq!(health.status, 200);
    assert_eq!(health.text(), r#"{"status":"ok","environment":"test"}"#);
    assert!(health.header("etag").is_none());
    let problem = call(&app, "GET", "/api/v1/me", &[]).await;
    assert!(problem.header("etag").is_none());
}

#[tokio::test]
async fn starlettes_routing_rules() {
    let app = fresh();
    // redirect_slashes, either way, keeping the query; Location is absolute.
    let r = call(&app, "GET", "/api/v1/me/", &[]).await;
    assert_eq!(r.status, 307);
    assert_eq!(r.header("location"), Some("http://testserver/api/v1/me"));
    assert!(r.body.is_empty());
    let r = call(&app, "POST", "/api/v1/meta/columns//?a=1&b=x%20y", &[]).await;
    assert_eq!(r.status, 307);
    assert_eq!(
        r.header("location"),
        Some("http://testserver/api/v1/meta/columns?a=1&b=x%20y")
    );
    // No redirect for "/" and none when the toggled path matches nothing.
    assert_eq!(call(&app, "GET", "/", &[]).await.status, 404);
    assert_eq!(call(&app, "GET", "/api/v1/nope/", &[]).await.status, 404);
    // The decoded path is routed.
    assert_eq!(
        call(&app, "GET", "/api/v1/meta%2Fcolumns", &[])
            .await
            .status,
        200
    );
    let r = call(&app, "GET", "/api/v1/caf%C3%A9%20x", &[]).await;
    assert_eq!(r.json()["instance"], "/api/v1/café x");
    // A wrong method is 405 internal-error with the status kept and no Allow header; HEAD is a
    // wrong method on a GET route.
    let r = call(&app, "POST", "/api/v1/meta/columns", &[]).await;
    let body = r.assert_problem(405, "internal-error");
    assert_eq!(body["detail"], "Method Not Allowed");
    assert_eq!(body["title"], "Internal server error");
    assert!(r.header("allow").is_none());
    assert_eq!(
        call(&app, "HEAD", "/api/v1/meta/columns", &[]).await.status,
        405
    );
    assert_eq!(call(&app, "HEAD", "/nowhere", &[]).await.status, 404);
}

#[tokio::test]
async fn cors_is_starlettes() {
    let app = fresh();
    let pre = call(
        &app,
        "OPTIONS",
        "/api/v1/me",
        &[
            ("Origin", "http://localhost:3000"),
            ("Access-Control-Request-Method", "GET"),
            (
                "Access-Control-Request-Headers",
                "authorization, x-request-id",
            ),
        ],
    )
    .await;
    assert_eq!(pre.status, 200);
    assert_eq!(pre.text(), "OK");
    assert_eq!(
        pre.header("content-type"),
        Some("text/plain; charset=utf-8")
    );
    assert_eq!(
        pre.header("access-control-allow-origin"),
        Some("http://localhost:3000")
    );
    assert_eq!(
        pre.header("access-control-allow-methods"),
        Some("GET, POST, PATCH, DELETE, OPTIONS")
    );
    assert_eq!(
        pre.header("access-control-allow-headers"),
        Some(
            "Accept, Accept-Language, Authorization, Content-Language, Content-Type, Idempotency-Key, X-CSRF-Token, X-Request-Id, traceparent, tracestate"
        )
    );
    assert_eq!(pre.header("access-control-max-age"), Some("600"));
    assert!(
        pre.header("x-request-id").is_none(),
        "a preflight never reaches the app"
    );

    let bad = call(
        &app,
        "OPTIONS",
        "/api/v1/me",
        &[
            ("Origin", "https://evil.example"),
            ("Access-Control-Request-Method", "PUT"),
            ("Access-Control-Request-Headers", "x-evil"),
        ],
    )
    .await;
    assert_eq!(bad.status, 400);
    assert_eq!(bad.text(), "Disallowed CORS origin, method, headers");
    assert!(bad.header("access-control-allow-origin").is_none());

    let simple = call(
        &app,
        "GET",
        "/api/v1/meta/columns",
        &[("Origin", "http://localhost:3000")],
    )
    .await;
    assert_eq!(simple.header("vary"), Some("Authorization, Origin"));
    assert_eq!(
        simple.header("access-control-allow-origin"),
        Some("http://localhost:3000")
    );
    assert_eq!(
        simple.header("access-control-allow-credentials"),
        Some("true")
    );
    assert_eq!(
        simple.header("access-control-expose-headers"),
        Some("X-Request-Id, X-Trace-Id, X-Baskfy-As-Of, X-Baskfy-Data-Version")
    );
    let other = call(
        &app,
        "GET",
        "/health",
        &[("Origin", "https://evil.example")],
    )
    .await;
    assert!(other.header("access-control-allow-origin").is_none());
    assert_eq!(
        other.header("access-control-allow-credentials"),
        Some("true")
    );
}

#[tokio::test]
async fn meta_reference_data_matches_the_registry() {
    let app = fresh();
    let factors = call(&app, "GET", "/api/v1/meta/factors", &[]).await.json();
    let list = factors.as_array().unwrap();
    assert_eq!(list.len(), baskfy_core::factor_registry::factors().len());
    let keys: Vec<&str> = list[0]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(keys.len(), 10);
    let columns = call(&app, "GET", "/api/v1/meta/columns", &[]).await.json();
    assert_eq!(columns.as_array().unwrap().len(), 36);
    assert_eq!(columns[0]["key"], "series");
}

#[tokio::test]
async fn trading_days_validates_before_it_reads() {
    let app = fresh();
    let r = call(&app, "GET", "/api/v1/meta/trading-days", &[]).await;
    let body = r.assert_problem(400, "invalid-screen-definition");
    assert_eq!(
        body["errors"],
        serde_json::json!([
            {"field": "from", "message": "Field required", "type": "missing"},
            {"field": "to", "message": "Field required", "type": "missing"}
        ])
    );
    let r = call(
        &app,
        "GET",
        "/api/v1/meta/trading-days?from=2026-10-10&to=2026-10-01",
        &[],
    )
    .await;
    let body = r.assert_problem(422, "no-trading-day");
    assert_eq!(
        body["detail"],
        "`to` (2026-10-01) is before `from` (2026-10-10)."
    );
    let r = call(
        &app,
        "GET",
        "/api/v1/meta/trading-days?from=2020-01-01&to=2026-10-01",
        &[],
    )
    .await;
    assert_eq!(
        r.assert_problem(422, "no-trading-day")["detail"],
        "Range exceeds the 1830-day maximum for one request."
    );
}
