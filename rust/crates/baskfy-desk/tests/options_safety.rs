//! Lane **options**, OP13's side door and the routes' refusals: ports
//! `tests/test_options_safety.py::TestTheSideDoor` (with `OPTIONS_ENABLED=true` the frozen lab's
//! `/options*` routes, its nav entry, the `/ops` strangle controls and the autorun's options loop
//! stay dark) and `tests/test_options_execute.py::TestThePage::
//! test_a_confirm_that_is_not_true_is_400_before_any_read`. The DB-backed halves of both files
//! are `tests/options_confirm_db.rs`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use axum::body::Body;
use axum::http::Request;
use baskfy_desk::app::StateParts;
use baskfy_desk::clock::{FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::analytics::analytics_views::autorun;
use baskfy_desk::lanes::analytics::ops;
use baskfy_desk::lanes::options::gates;
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Db, DbConfig};
use baskfy_desk::{AppState, DeskPaths, build_app, build_state};
use chrono::{NaiveDate, TimeZone};
use tower::ServiceExt as _;

const KITE: &str = r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;
/// A database the bed does not have: every connect is refused, so a route that opens the store
/// answers 500 — the proof a 400 came before any read.
const DEAD: &str = "postgres://baskfy:rs-test-only@127.0.0.1:55432/rsd_nowhere";

fn flag_on() -> Settings {
    let mut s = Settings::defaults().unwrap();
    s.options_enabled = true;
    s
}

async fn state(settings: Settings, backend: &str, dir: &std::path::Path) -> AppState {
    std::os::unix::fs::symlink(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../kite-momentum-rebalancer/app"),
        dir.join("app"),
    )
    .unwrap();
    std::fs::create_dir_all(dir.join("data")).unwrap();
    let kite_dir = dir.join("kite");
    std::fs::create_dir_all(kite_dir.join("instruments")).unwrap();
    std::fs::write(kite_dir.join("kite.json"), KITE).unwrap();
    let kite = Arc::new(ReplayKite::load(&kite_dir, None, "test-api-key").unwrap());
    let db = Db::open(DbConfig {
        backend: backend.to_owned(),
        schema: "desk".to_owned(),
        database_url: DEAD.to_owned(),
        db_path: dir.join("data").join("portfolio.db"),
    })
    .await
    .unwrap();
    build_state(StateParts {
        settings,
        clock: Arc::new(FixedClock::new(
            ist().with_ymd_and_hms(2026, 10, 27, 10, 0, 0).unwrap(),
        )),
        ids: Arc::new(HarnessIds::new("options-safety")),
        db,
        kite,
        paths: DeskPaths::under(dir),
        harness: None,
    })
}

#[test]
fn the_lab_is_frozen_so_the_flag_alone_wakes_nothing() {
    let s = flag_on();
    let repo =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer");
    assert!(!gates::lab_present(&repo));
    assert!(!gates::lab_enabled(&s, &repo));
    assert!(!baskfy_desk::lanes::options::lab_enabled(&s));
}

#[test]
fn ops_offers_no_strangle_control() {
    assert!(!ops::groups().contains(&"Options"));
    assert!(
        !ops::OPERATIONS
            .iter()
            .any(|o| o.name.starts_with("strangle"))
    );
}

#[tokio::test]
async fn the_autorun_never_reaches_the_options_loop() {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("p.db"))
        .await
        .unwrap();
    conn.migrate().await.unwrap();
    let lab = baskfy_desk::lanes::options::lab_enabled(&flag_on());
    let now = NaiveDate::from_ymd_opt(2026, 10, 27)
        .unwrap()
        .and_hms_opt(10, 0, 0)
        .unwrap();
    let items = autorun::needed(&mut conn, now, true, lab).await.unwrap();
    assert!(!items.iter().any(|i| {
        i.get("op")
            .is_some_and(|o| o.py_str().starts_with("strangle"))
    }));
}

async fn send(app: &axum::Router, req: Request<Body>) -> (u16, String) {
    let r = app.clone().oneshot(req).await.unwrap();
    let status = r.status().as_u16();
    let body = axum::body::to_bytes(r.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, String::from_utf8_lossy(&body).into_owned())
}

fn get(path: &str) -> Request<Body> {
    Request::builder().uri(path).body(Body::empty()).unwrap()
}

#[tokio::test]
async fn the_lab_routes_404_and_the_nav_has_no_link() {
    let dir = tempfile::tempdir().unwrap();
    let state = state(flag_on(), "sqlite", dir.path()).await;
    let app = build_app(state.clone());
    assert_eq!(send(&app, get("/options")).await.0, 404);
    assert_eq!(send(&app, get("/options/data")).await.0, 404);
    // A same-origin POST (the desk's CSRF rule answers a POST without an origin 403 before any
    // route is matched; the TestClient's host is on Python's allowed list).
    let post = Request::builder()
        .method("POST")
        .uri("/options/run")
        .header("host", "localhost")
        .header("origin", "http://localhost")
        .body(Body::empty())
        .unwrap();
    let status = send(&app, post).await.0;
    assert!(status == 404 || status == 422, "{status}");
    let (status, ops_page) = send(&app, get("/ops")).await;
    assert_eq!(status, 200, "{ops_page}");
    assert!(!ops_page.contains(r#"href="/options""#));
}

fn form(path: &str, body: &str) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/x-www-form-urlencoded")
        .body(Body::from(body.to_owned()))
        .unwrap()
}

#[tokio::test]
async fn a_confirm_that_is_not_true_is_400_before_any_read() {
    let dir = tempfile::tempdir().unwrap();
    let state = state(Settings::defaults().unwrap(), "postgres", dir.path()).await;
    let app = baskfy_desk::lanes::options::routes().with_state(state);
    for (path, field) in [
        ("/nifty-options/execute", "plan_id"),
        ("/nifty-options/close", "session_id"),
    ] {
        let (status, body) = send(&app, form(path, &format!("{field}=1&confirm=false"))).await;
        assert_eq!(status, 400, "{path}: {body}");
        let json: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(json["code"], "CONFIRM_REQUIRED");
    }
    // The same request with confirm=true reaches the store, which here refuses every connect:
    // the 400 above was decided before any read.
    let (status, _) = send(
        &app,
        form("/nifty-options/execute", "plan_id=1&confirm=true"),
    )
    .await;
    assert_eq!(status, 500);
}
