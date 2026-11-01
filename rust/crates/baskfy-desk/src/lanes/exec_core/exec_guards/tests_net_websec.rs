//! Ports `tests/test_outbound_ip.py` (the IPv4 pin, and that the Kite client pins it before it
//! builds anything, and only when `FORCE_IPV4` says so) and all of `tests/test_websec.py`: the
//! allow-lists, the Basic check, the closed defaults, and every HTTP case run through the
//! middleware's own decision ([`refusal`]) in front of a stand-in router, so the password cases
//! run here although `DESK_PASSWORD` is per process on the HTTP harness. The same cases are
//! also graded against Python over HTTP by `desk_diff.py` (`desk_cases/exec_guards.json`, and
//! `desk_cases/exec_guards_variants/password.json` under `--env DESK_PASSWORD=hunter2`).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use axum::Router;
use axum::body::Body;
use axum::extract::Request;
use axum::http::{Method, StatusCode};
use axum::middleware::Next;
use axum::routing::{get, post};
use base64::Engine as _;
use tower::ServiceExt as _;

use super::super::net::{force_ipv4, ipv4_only, local_bind, outbound_ip};
use crate::settings::Settings;
use crate::websec::{CompareDigestTypeError, NO_AUTH, basic_ok, hosts, origins, refusal};

// =============================================================================================
// test_outbound_ip.py
// =============================================================================================

#[test]
fn test_pinning_selects_the_ipv4_family() {
    let _ = force_ipv4();
    assert!(ipv4_only());
    assert_eq!(
        local_bind(),
        Some(std::net::IpAddr::V4(std::net::Ipv4Addr::UNSPECIFIED))
    );
}

#[test]
fn test_pinning_twice_changes_nothing() {
    // Several entry points construct a Kite client; applying it again is a no-op, not a stacked
    // patch.
    let _first = force_ipv4();
    assert!(!force_ipv4(), "a second pin is a no-op");
    assert!(ipv4_only());
}

#[test]
fn test_a_failure_to_patch_is_reported_not_raised() {
    // Python's patch can fail (no `urllib3`) and must return False rather than raise, because an
    // app that will not start is worse than an order refused for the wrong address. The Rust pin
    // is a process-wide switch with no failure path at all: its type is `fn() -> bool`, so it
    // cannot raise, and that is the property asserted.
    let pin: fn() -> bool = force_ipv4;
    let _ = pin();
}

/// The constructor `Kite()` runs, as source (`DeskKite::with_limit_clock`).
fn kite_constructor() -> &'static str {
    let src = include_str!("../kite_client.rs");
    let start = src
        .find("pub fn with_limit_clock(")
        .expect("DeskKite's constructor");
    let rest = &src[start..];
    let end = rest.find("\n    }\n").expect("the constructor's end");
    &rest[..end]
}

#[test]
fn test_the_kite_client_pins_before_it_builds_a_session() {
    // Python: the pin, then `KiteConnect(...)`, in that order. The Rust client receives its
    // transport ready-made and opens no connection in the constructor; the pin is the first
    // thing the constructor does (before the shared limiter's Redis connection and before the
    // token is loaded), and a live transport reads `local_bind()` when it connects.
    let ctor = kite_constructor();
    let pin = ctor
        .find("super::net::force_ipv4()")
        .expect("the constructor pins");
    for later in ["SharedSpacers::new(", "let me = Self {", "me.load_token()"] {
        let at = ctor.find(later).expect(later);
        assert!(pin < at, "the pin comes after {later}");
    }
    // And behaviourally: a client built with the default settings leaves the process pinned.
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("kite.json"),
        r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#,
    )
    .unwrap();
    let kite =
        Arc::new(crate::fake_kite::ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
    let mut settings = Settings::defaults().unwrap();
    settings.token_file = dir.path().join("token.json").display().to_string();
    assert!(settings.force_ipv4);
    let _client = super::super::kite_client::DeskKite::new(
        kite,
        Arc::new(std::sync::RwLock::new(settings)),
        Arc::new(crate::clock::FixedClock::new(
            chrono::TimeZone::with_ymd_and_hms(&crate::clock::ist(), 2026, 10, 30, 10, 0, 0)
                .unwrap(),
        )),
    );
    assert!(ipv4_only());
}

#[test]
fn test_the_switch_can_be_turned_off() {
    // A host with a static IPv6 wants it off: the pin is conditional on `FORCE_IPV4`. (The pin
    // is process-global and other tests set it, so the condition is asserted on the source.)
    let ctor = kite_constructor();
    let guard = ctor
        .find("if s.force_ipv4 {")
        .expect("the pin is conditional");
    let pin = ctor.find("super::net::force_ipv4()").unwrap();
    assert!(guard < pin);
    assert_eq!(
        ctor.matches("force_ipv4()").count(),
        1,
        "pinned on one path only"
    );
}

#[test]
fn test_it_defaults_to_on() {
    assert!(Settings::defaults().unwrap().force_ipv4);
}

#[tokio::test]
async fn a_failed_lookup_is_none_not_an_error() {
    assert_eq!(
        outbound_ip(|| async { Err("timed out".to_owned()) }).await,
        None
    );
    assert_eq!(
        outbound_ip(|| async { Ok(" 203.0.113.7\n".to_owned()) })
            .await
            .as_deref(),
        Some("203.0.113.7")
    );
}

// =============================================================================================
// test_websec.py — the pure half
// =============================================================================================

#[test]
fn the_names_it_answers_to_default_to_loopback_and_are_configurable() {
    let h = hosts("");
    assert!(h.contains("127.0.0.1") && h.contains("localhost") && h.len() == 2);
    let h = hosts(" Desk.Internal ,, ");
    assert_eq!(h.into_iter().collect::<Vec<_>>(), ["desk.internal"]);
    let o = origins("", 8420);
    for want in [
        "http://localhost",
        "http://localhost:8420",
        "https://127.0.0.1",
        "https://127.0.0.1:8420",
    ] {
        assert!(o.contains(want), "{want}");
    }
    assert!(!o.contains("https://evil.example"));
}

#[test]
fn test_no_password_by_default_so_the_laptop_is_unchanged() {
    let s = Settings::defaults().unwrap();
    assert_eq!(s.desk_password, "");
    assert_eq!(NO_AUTH, ["/status"]);
}

#[test]
fn test_the_comparison_is_constant_time() {
    // Python inspects `_basic_ok`'s source for `compare_digest`; the Rust check is the
    // fixed-time fold, and the source says so.
    let src = include_str!("../../../websec.rs");
    let body = &src[src.find("pub fn basic_ok(").unwrap()..];
    let body = &body[..body.find("\n}\n").unwrap()];
    assert!(body.contains("constant_time_eq("), "{body}");
    let eq = &src[src.find("fn constant_time_eq(").unwrap()..];
    assert!(
        eq.contains("fold(0u8"),
        "a short-circuiting compare leaks the password"
    );
}

fn basic(user_pass: &str) -> String {
    format!(
        "Basic {}",
        base64::engine::general_purpose::STANDARD.encode(user_pass)
    )
}

#[test]
fn the_right_password_passes_the_check_and_a_wrong_one_does_not() {
    assert_eq!(basic_ok(Some(&basic("desk:hunter2")), "hunter2"), Ok(true));
    assert_eq!(
        basic_ok(Some(&basic("anyone:hunter2")), "hunter2"),
        Ok(true)
    );
    assert_eq!(basic_ok(Some(&basic("desk:hunter3")), "hunter2"), Ok(false));
    assert_eq!(
        basic_ok(Some(&basic("desk:hunter22")), "hunter2"),
        Ok(false)
    );
}

#[test]
fn malformed_credentials_fail_the_check() {
    for bad in ["", "Basic", "Basic !!!not-base64!!!", "Bearer hunter2"] {
        assert_eq!(basic_ok(Some(bad), "hunter2"), Ok(false), "{bad:?}");
    }
    assert_eq!(basic_ok(None, "hunter2"), Ok(false));
}

// --- review R8 (exec_guards): headers are read as Starlette reads them -----------------------

#[test]
fn a_non_ascii_header_is_read_as_latin1_not_as_absent() {
    let v = axum::http::HeaderValue::from_bytes(b"\xe9vil.example").unwrap();
    assert_eq!(crate::websec::latin1(&v), "\u{e9}vil.example");
}

#[test]
fn the_basic_header_decodes_like_b64decode() {
    // `base64.b64decode` drops characters outside the alphabet (a wrapped header still works).
    let enc = base64::engine::general_purpose::STANDARD.encode("desk:hunter2");
    let (a, b) = enc.split_at(4);
    assert_eq!(
        basic_ok(Some(&format!("Basic {a}\n{b}")), "hunter2"),
        Ok(true)
    );
    assert_eq!(
        basic_ok(Some(&format!("Basic {a} {b}")), "hunter2"),
        Ok(true)
    );
    // CPython: a stray or excess `=` is skipped; missing padding is "Incorrect padding".
    for (enc, ok) in [
        ("ZGVzazpodW50ZXIy=", true),
        ("ZGVzazpodW50ZXIy==", true),
        ("ZGVz=azpodW50ZXIy", true),
        ("ZGVzazpodW50ZX", false),
        ("ZGVzazpodW50ZXIyZ", false),
    ] {
        assert_eq!(
            basic_ok(Some(&format!("Basic {enc}")), "hunter2"),
            Ok(ok),
            "{enc}"
        );
    }
}

// --- wave D2 (exec_guards): the last two divergences, reproduced --------------------------

#[test]
fn a_non_ascii_character_in_the_encoded_credentials_is_a_refusal() {
    // `b64decode(str)` encodes the text as ASCII first and raises; `_basic_ok` catches it. The
    // byte-skipping decoder would otherwise read "\u{e9}ZGVzazpodW50ZXIy" as the right password.
    let enc = base64::engine::general_purpose::STANDARD.encode("desk:hunter2");
    assert_eq!(
        basic_ok(Some(&format!("Basic \u{e9}{enc}")), "hunter2"),
        Ok(false)
    );
}

#[test]
fn compare_digest_raises_on_a_non_ascii_str_on_either_side() {
    // `hmac.compare_digest(str, str)` refuses non-ASCII text with `TypeError` (R8.10.1).
    assert_eq!(
        basic_ok(Some(&basic("desk:h\u{fc}nter2")), "hunter2"),
        Err(CompareDigestTypeError)
    );
    assert_eq!(
        basic_ok(Some(&basic("desk:hunter2")), "h\u{fc}nter2"),
        Err(CompareDigestTypeError)
    );
    // A credential that is not UTF-8 decodes with U+FFFD ("replace"), which is non-ASCII too.
    let raw = base64::engine::general_purpose::STANDARD.encode(b"desk:\xff\xfe");
    assert_eq!(
        basic_ok(Some(&format!("Basic {raw}")), "hunter2"),
        Err(CompareDigestTypeError)
    );
    // Nothing to compare yet is still an ordinary refusal, whatever the password.
    assert_eq!(basic_ok(None, "h\u{fc}nter2"), Ok(false));
    assert_eq!(basic_ok(Some("Bearer x"), "h\u{fc}nter2"), Ok(false));
}

// =============================================================================================
// test_websec.py — over HTTP, through the middleware's decision
// =============================================================================================

const ORIGIN: &str = "http://testserver:8420";

/// Stand-ins for the routes the Python tests reach (`/`, `/status`, `/performance`,
/// `/performance/data`, `POST /ops/run`, `POST /settings`), behind [`refusal`] exactly as
/// `crate::websec::layer` calls it. The settings are `app.config`'s defaults (allowed hosts
/// `127.0.0.1,localhost,testserver`, port 8420) with the test's overrides.
fn app(allowed: Option<&str>, password: &str) -> Router {
    let defaults = Settings::defaults().unwrap();
    let allowed = allowed.map_or(defaults.desk_allowed_hosts, str::to_owned);
    let port = defaults.desk_port;
    let password = password.to_owned();
    Router::new()
        .route("/", get(|| async { "desk" }))
        .route("/status", get(|| async { "{}" }))
        .route("/performance", get(|| async { "performance" }))
        .route("/performance/data", get(|| async { "{}" }))
        .route(
            "/ops/run",
            post(|| async { (StatusCode::BAD_REQUEST, "unknown op") }),
        )
        .route("/settings", post(|| async { "saved" }))
        .layer(axum::middleware::from_fn(
            move |request: Request, next: Next| {
                let (allowed, password) = (allowed.clone(), password.clone());
                async move {
                    match refusal(
                        &allowed,
                        port,
                        &password,
                        request.method(),
                        request.uri().path(),
                        request.headers(),
                    ) {
                        Some(refused) => refused,
                        None => next.run(request).await,
                    }
                }
            },
        ))
}

/// One request (Host `testserver`, as Starlette's `TestClient` sends unless told otherwise).
async fn call(
    app: Router,
    method: Method,
    path: &str,
    headers: &[(&str, &str)],
) -> (StatusCode, axum::http::HeaderMap, String) {
    let mut req = Request::builder().method(method).uri(path);
    if !headers.iter().any(|(k, _)| k.eq_ignore_ascii_case("host")) {
        req = req.header("host", "testserver");
    }
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    let body = if headers
        .iter()
        .any(|(k, _)| k.eq_ignore_ascii_case("content-type"))
    {
        Body::from("op=daily")
    } else {
        Body::empty()
    };
    let resp = app.oneshot(req.body(body).unwrap()).await.unwrap();
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        headers,
        String::from_utf8_lossy(&bytes).into_owned(),
    )
}

const FORM: (&str, &str) = ("content-type", "application/x-www-form-urlencoded");

#[tokio::test]
async fn test_a_request_under_another_name_is_refused() {
    let (status, _, text) = call(
        app(None, ""),
        Method::GET,
        "/performance/data",
        &[("host", "evil.example")],
    )
    .await;
    assert_eq!(status, StatusCode::MISDIRECTED_REQUEST);
    assert!(text.contains("evil.example"), "{text}");
}

#[tokio::test]
async fn test_the_names_it_does_answer_to_still_work() {
    for host in ["127.0.0.1", "localhost"] {
        let (status, _, _) = call(app(None, ""), Method::GET, "/status", &[("host", host)]).await;
        assert_eq!(status, StatusCode::OK, "{host}");
    }
}

#[tokio::test]
async fn test_the_allowlist_is_configurable() {
    let only = || app(Some("desk.internal"), "");
    let (status, _, _) = call(only(), Method::GET, "/status", &[("host", "desk.internal")]).await;
    assert_eq!(status, StatusCode::OK);
    let (status, _, _) = call(only(), Method::GET, "/status", &[("host", "127.0.0.1")]).await;
    assert_eq!(status, StatusCode::MISDIRECTED_REQUEST);
}

#[tokio::test]
async fn test_a_post_with_no_origin_is_refused() {
    let (status, _, text) = call(
        app(None, ""),
        Method::POST,
        "/ops/run",
        &[FORM, ("origin", "")],
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(text.contains("did not come from the desk"), "{text}");
}

#[tokio::test]
async fn test_a_post_from_another_origin_is_refused() {
    let (status, _, _) = call(
        app(None, ""),
        Method::POST,
        "/settings",
        &[FORM, ("origin", "https://evil.example")],
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn test_a_post_from_the_desk_is_allowed() {
    let (status, _, _) = call(
        app(None, ""),
        Method::POST,
        "/ops/run",
        &[FORM, ("origin", ORIGIN)],
    )
    .await;
    assert_ne!(status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn test_a_referer_stands_in_when_origin_is_absent() {
    let referer = format!("{ORIGIN}/ops");
    let (status, _, _) = call(
        app(None, ""),
        Method::POST,
        "/ops/run",
        &[FORM, ("origin", ""), ("referer", &referer)],
    )
    .await;
    assert_ne!(status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn test_reads_are_never_blocked_by_the_origin_rule() {
    for path in ["/", "/status", "/performance"] {
        let (status, _, _) = call(app(None, ""), Method::GET, path, &[("origin", "")]).await;
        assert_eq!(status, StatusCode::OK, "{path}");
    }
}

#[tokio::test]
async fn test_when_set_every_page_requires_it() {
    let (status, headers, _) = call(app(None, "hunter2"), Method::GET, "/", &[]).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let challenge = headers
        .get("www-authenticate")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    assert!(challenge.contains("Basic"), "{challenge}");
}

#[tokio::test]
async fn test_the_right_password_gets_in() {
    let auth = basic("desk:hunter2");
    let (status, _, _) = call(
        app(None, "hunter2"),
        Method::GET,
        "/",
        &[("authorization", &auth)],
    )
    .await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn test_a_wrong_password_does_not() {
    let auth = basic("desk:hunter3");
    let (status, _, _) = call(
        app(None, "hunter2"),
        Method::GET,
        "/",
        &[("authorization", &auth)],
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn test_status_stays_reachable_so_a_health_check_still_works() {
    let (status, _, _) = call(app(None, "hunter2"), Method::GET, "/status", &[]).await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn test_malformed_credentials_are_rejected_rather_than_crashing() {
    for bad in ["", "Basic", "Basic !!!not-base64!!!", "Bearer hunter2"] {
        let (status, _, _) = call(
            app(None, "hunter2"),
            Method::GET,
            "/",
            &[("authorization", bad)],
        )
        .await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "{bad:?}");
    }
}

#[tokio::test]
async fn a_non_ascii_credential_is_pythons_plain_500() {
    // R8.10.1: Starlette's `ServerErrorMiddleware` answer to the escaped `TypeError`.
    let auth = basic("desk:h\u{fc}nter2");
    let (status, headers, text) = call(
        app(None, "hunter2"),
        Method::GET,
        "/",
        &[("authorization", &auth)],
    )
    .await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(text, "Internal Server Error");
    assert_eq!(
        headers.get("content-type").unwrap(),
        "text/plain; charset=utf-8"
    );
}

#[test]
fn test_the_docs_are_closed_by_default() {
    // `/docs`, `/redoc` and `/openapi.json` answering 404 is graded over HTTP against Python by
    // `desk_cases/exec_guards.json`; the switch behind them is off by default.
    assert!(!Settings::defaults().unwrap().desk_docs);
}
