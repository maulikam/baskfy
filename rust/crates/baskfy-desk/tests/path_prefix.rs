//! `tests/test_path_prefix.py`, ported: the desk at `/desk` on the app's address.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use axum::response::{Html, IntoResponse, Redirect, Response};
use axum::routing::get;
use baskfy_desk::path_prefix::{layer, normalise, rewrite_body, rewrite_location};
use tower::ServiceExt as _;

const PAGE: &str = r##"<a href="/swing">Swing</a> <a href='/ops'>Ops</a> <form action="/execute" method="post">
<link href="/static/app.css"> <img src="/static/logo.svg"> <a href="https://kite.zerodha.com/x">K</a>
<a href="//cdn.example/x">cdn</a> <a href="/desk/already">done</a> <a href="#top">top</a>
<script>fetch('/analyze', {method: 'POST'}); fetch(`/swing/data`); fetch("/vbt/rescan")</script>"##;

fn app(prefix: &str) -> Router {
    Router::new()
        .route("/page", get(|| async { Html(PAGE) }))
        .route(
            "/go",
            get(|| async { Redirect::to("/tradebook?imported=3") }),
        )
        .route(
            "/data",
            get(|| async {
                Response::builder()
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(r#"{"href":"/swing","amount":"1000.00"}"#))
                    .unwrap()
            }),
        )
        .route(
            "/text",
            get(|| async { r#"href="/swing""#.into_response() }),
        )
        .layer(axum::middleware::from_fn_with_state(
            normalise(prefix),
            layer,
        ))
}

async fn get_page(app: Router, path: &str) -> (StatusCode, axum::http::HeaderMap, String) {
    let r = app
        .oneshot(Request::get(path).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = r.status();
    let headers = r.headers().clone();
    let body =
        String::from_utf8(to_bytes(r.into_body(), usize::MAX).await.unwrap().to_vec()).unwrap();
    (status, headers, body)
}

#[tokio::test]
async fn empty_prefix_leaves_every_byte_alone() {
    assert_eq!(get_page(app(""), "/page").await.2, PAGE);
    assert_eq!(
        get_page(app(""), "/go").await.1[header::LOCATION],
        "/tradebook?imported=3"
    );
}

#[tokio::test]
async fn links_forms_assets_and_fetches_are_prefixed() {
    let body = get_page(app("/desk"), "/page").await.2;
    for url in [
        r#"href="/desk/swing""#,
        "href='/desk/ops'",
        r#"action="/desk/execute""#,
        r#"href="/desk/static/app.css""#,
        r#"src="/desk/static/logo.svg""#,
        "fetch('/desk/analyze'",
        "fetch(`/desk/swing/data`",
        r#"fetch("/desk/vbt/rescan""#,
    ] {
        assert!(body.contains(url), "{url}");
    }
}

#[tokio::test]
async fn external_protocol_relative_fragment_and_already_prefixed_urls_are_untouched() {
    let body = get_page(app("/desk"), "/page").await.2;
    assert!(body.contains(r#"href="https://kite.zerodha.com/x""#));
    assert!(body.contains(r#"href="//cdn.example/x""#));
    assert!(body.contains(r#"href="/desk/already""#) && !body.contains("/desk/desk/"));
    assert!(body.contains(r##"href="#top""##));
}

#[tokio::test]
async fn redirects_are_prefixed() {
    assert_eq!(
        get_page(app("/desk"), "/go").await.1[header::LOCATION],
        "/desk/tradebook?imported=3"
    );
}

#[tokio::test]
async fn content_length_matches_the_rewritten_body() {
    let (_, headers, body) = get_page(app("/desk"), "/page").await;
    assert_eq!(
        headers[header::CONTENT_LENGTH]
            .to_str()
            .unwrap()
            .parse::<usize>()
            .unwrap(),
        body.len()
    );
}

#[tokio::test]
async fn json_and_plain_text_are_never_rewritten() {
    assert_eq!(
        get_page(app("/desk"), "/data").await.2,
        r#"{"href":"/swing","amount":"1000.00"}"#
    );
    assert_eq!(get_page(app("/desk"), "/text").await.2, r#"href="/swing""#);
}

#[test]
fn prefix_spellings_normalise() {
    assert_eq!(normalise("desk"), "/desk");
    assert_eq!(normalise("/desk/"), "/desk");
    assert_eq!(normalise(" /desk "), "/desk");
    assert_eq!(normalise(""), "");
    assert_eq!(normalise("/"), "");
}

#[test]
fn location_rules() {
    assert_eq!(
        rewrite_location("/stops?armed=1", "/desk"),
        "/desk/stops?armed=1"
    );
    assert_eq!(rewrite_location("/desk", "/desk"), "/desk");
    assert_eq!(
        rewrite_location("https://kite.trade/connect", "/desk"),
        "https://kite.trade/connect"
    );
    assert_eq!(rewrite_location("//x.example/", "/desk"), "//x.example/");
}

#[test]
fn a_root_path_that_merely_starts_with_the_prefix_word_is_still_prefixed() {
    assert_eq!(
        rewrite_body(r#"href="/desktop""#, "/desk"),
        r#"href="/desk/desktop""#
    );
}
