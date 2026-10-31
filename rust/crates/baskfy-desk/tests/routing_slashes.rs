//! Starlette's `redirect_slashes` over the whole app, and the session lane's main.py routes
//! answering on a fresh SQLite desk.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::Path;
use std::sync::Arc;

use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use baskfy_desk::app::{DeskPaths, StateParts};
use baskfy_desk::clock::FixedClock;
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::storage::{Db, DbConfig};
use baskfy_desk::{Settings, build_app, build_state};
use chrono::{FixedOffset, TimeZone};
use tower::ServiceExt as _;

async fn app(dir: &Path) -> Router {
    let mut settings = Settings::defaults().unwrap();
    assert!(settings.dry_run);
    settings.kite_api_key = "replay-test-key".to_owned();
    settings.db_backend = "sqlite".to_owned();
    settings.db_path = dir.join("desk.db").display().to_string();
    settings.token_file = dir.join("no-such-token.json").display().to_string();
    std::fs::write(
        dir.join("kite.json"),
        r#"{"session": {"authed": false}, "routes": {}, "quotes": {}}"#,
    )
    .unwrap();
    let kite = Arc::new(ReplayKite::load(dir, None, &settings.kite_api_key).unwrap());
    let db = Db::open(DbConfig::from_settings(&settings, None))
        .await
        .unwrap();
    let desk = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer");
    let ist = FixedOffset::east_opt(19800).unwrap();
    let state = build_state(StateParts {
        settings,
        clock: Arc::new(FixedClock::new(
            ist.with_ymd_and_hms(2026, 10, 30, 11, 0, 0).unwrap(),
        )),
        ids: Arc::new(HarnessIds::new("routing")),
        db,
        kite,
        paths: DeskPaths {
            root: dir.to_path_buf(),
            templates: desk.join("app").join("templates"),
            static_dir: desk.join("app").join("static"),
        },
        harness: None,
    });
    build_app(state)
}

async fn get(app: &Router, uri: &str) -> (StatusCode, Option<String>, String) {
    let req = Request::builder()
        .uri(uri)
        .header(header::HOST, "127.0.0.1")
        .body(Body::empty())
        .unwrap();
    let r = app.clone().oneshot(req).await.unwrap();
    let status = r.status();
    let location = r
        .headers()
        .get(header::LOCATION)
        .map(|v| v.to_str().unwrap().to_owned());
    let body = to_bytes(r.into_body(), usize::MAX).await.unwrap();
    (
        status,
        location,
        String::from_utf8_lossy(&body).into_owned(),
    )
}

#[tokio::test]
async fn a_path_whose_twin_is_a_route_is_a_307_to_the_twin() {
    let dir = tempfile::tempdir().unwrap();
    let app = app(dir.path()).await;
    for (from, to) in [
        ("/status/", "http://127.0.0.1/status"),
        ("/status//", "http://127.0.0.1/status"),
        ("/twt/execute/", "http://127.0.0.1/twt/execute"),
        (
            "/reconcile/?plan_id=p1",
            "http://127.0.0.1/reconcile?plan_id=p1",
        ),
        ("/tradebook/data/", "http://127.0.0.1/tradebook/data"),
        ("/static", "http://127.0.0.1/static/"),
    ] {
        let (status, location, body) = get(&app, from).await;
        assert_eq!(status, StatusCode::TEMPORARY_REDIRECT, "{from}");
        assert_eq!(location.as_deref(), Some(to), "{from}");
        assert!(body.is_empty(), "{from}");
    }
}

#[tokio::test]
async fn no_twin_is_starlettes_404() {
    let dir = tempfile::tempdir().unwrap();
    let app = app(dir.path()).await;
    for path in ["/nope", "/nope/", "/static/"] {
        let (status, location, body) = get(&app, path).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{path}");
        assert_eq!(location, None, "{path}");
        assert_eq!(body, r#"{"detail":"Not Found"}"#, "{path}");
    }
}

#[tokio::test]
async fn the_tradebook_and_reconcile_routes_answer() {
    let dir = tempfile::tempdir().unwrap();
    let app = app(dir.path()).await;
    let (status, _, body) = get(&app, "/tradebook/data").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.starts_with('{'), "{body}");
    let (status, _, body) = get(&app, "/reconcile/data").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.contains(r#""available":false"#), "{body}");
    let (status, _, body) = get(&app, "/tradebook").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, _, body) = get(&app, "/reconcile").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, _, body) = get(&app, "/").await;
    assert_eq!(status, StatusCode::OK, "{body}");
}
