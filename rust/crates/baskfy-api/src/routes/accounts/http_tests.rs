//! The GET-path assertions of the Python tests for the accounts lane's routers, through the
//! whole app on the bed (`cfg(bed)`, DECISIONS-RS R5.4): `test_api_screens.py`,
//! `test_api_admin.py`, `test_api_alerts.py`, `test_api_keys.py`, `test_api_webhooks.py`,
//! `test_curated_tenant_isolation.py`, `test_curated_trending_http.py`, `test_csv_export.py`,
//! `test_af_lane_i_versions_watchlist.py`. Rows the bed does not hold are not invented: every
//! assertion reads what the bed's copy actually contains.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode};
use baskfy_providers::secret::Secret;
use tower::Service;

use crate::{AppState, Settings, build_app};

const SECRET: &str = "baskfy-test-secret-0123456789abc";
const OWNER: &str = "2fe482ed2015";
const PLAIN: &str = "a0a1fd14fa3e";

fn app() -> Router {
    let url = std::env::var("BASKFY_RS_TEST_DATABASE_URL").expect("cfg(bed) implies the variable");
    let settings = Settings {
        database_url: Secret::new(&url),
        environment: "test".to_owned(),
        jwt_secret: Secret::new(SECRET),
        rate_limit_enabled: false,
        cookie_secure: false,
        db_pool_timeout_seconds: 5.0,
        ..Settings::default()
    };
    let db = baskfy_db::Db::connect_lazy(&settings.db_settings()).unwrap();
    build_app(AppState {
        settings: Arc::new(settings),
        db,
        limiter: None,
    })
}

fn bearer(public_id: &str) -> String {
    let now = i64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs(),
    )
    .unwrap();
    format!(
        "Bearer {}",
        crate::auth::encode_token(public_id, SECRET.as_bytes(), now, 600, 0)
    )
}

async fn get(
    app: &Router,
    uri: &str,
    who: Option<&str>,
) -> (StatusCode, Vec<u8>, axum::http::HeaderMap) {
    let mut builder = Request::builder()
        .method("GET")
        .uri(uri)
        .header("host", "testserver");
    if let Some(who) = who {
        builder = builder.header("authorization", bearer(who));
    }
    let response = app
        .clone()
        .call(builder.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let body = to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    (status, body, headers)
}

fn json(body: &[u8]) -> serde_json::Value {
    serde_json::from_slice(body)
        .unwrap_or_else(|e| panic!("{e}: {}", String::from_utf8_lossy(body)))
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn anonymous_sees_only_the_example_screens_and_a_user_sees_their_own_too() {
    let app = app();
    let (status, body, _) = get(&app, "/api/v1/screens", None).await;
    assert_eq!(status, StatusCode::OK);
    let anon = json(&body);
    let rows = anon["data"].as_array().unwrap();
    assert!(!rows.is_empty());
    assert!(
        rows.iter()
            .all(|s| s["is_example"] == true && s["editable"] == false)
    );
    let (_, body, _) = get(&app, "/api/v1/screens", Some(OWNER)).await;
    let owner = json(&body);
    let mine: Vec<_> = owner["data"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|s| s["editable"] == true)
        .collect();
    assert!(!mine.is_empty(), "the owner holds screens on the bed");
    // test_one_user_never_sees_another_users_screens / another_users_screen_is_404_not_403.
    let theirs = mine[0]["public_id"].as_str().unwrap();
    let (status, body, _) = get(&app, &format!("/api/v1/screens/{theirs}"), Some(PLAIN)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(json(&body)["type"], "not-found");
    // test_an_example_screen_is_readable_anonymously.
    let (status, body, _) = get(&app, "/api/v1/screens/exmpl0000001", None).await;
    assert_eq!(status, StatusCode::OK);
    let one = json(&body);
    assert_eq!(
        one["definition"]["missing_data"], "penalize",
        "the full model dump"
    );
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn runs_are_keyset_paginated_and_a_bad_cursor_is_a_404() {
    let app = app();
    let (status, body, _) = get(
        &app,
        "/api/v1/screens/da1dae3992bf/runs?limit=2",
        Some(OWNER),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let page = json(&body);
    let cursor = page["next_cursor"].as_str().unwrap().to_owned();
    let (_, body, _) = get(
        &app,
        &format!("/api/v1/screens/da1dae3992bf/runs?limit=2&cursor={cursor}"),
        Some(OWNER),
    )
    .await;
    let next = json(&body);
    assert_ne!(
        page["data"][0], next["data"][0],
        "the second page does not repeat the first"
    );
    let (status, _, _) = get(
        &app,
        "/api/v1/screens/da1dae3992bf/runs?cursor=abc",
        Some(OWNER),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, _, _) = get(
        &app,
        "/api/v1/screens/da1dae3992bf/runs?limit=0",
        Some(OWNER),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn every_admin_get_is_401_anonymous_and_404_for_non_staff() {
    // test_api_admin.py: test_an_anonymous_caller_gets_401 / test_a_signed_in_non_staff_caller_gets_404.
    let app = app();
    for path in [
        "/api/v1/admin/pipeline/runs",
        "/api/v1/admin/pipeline/runs/1",
        "/api/v1/admin/resync?days=1",
        "/api/v1/admin/data-versions",
        "/api/v1/admin/providers",
        "/api/v1/admin/users?q=a",
        "/api/v1/admin/users/2fe482ed2015",
        "/api/v1/admin/actions",
        "/api/v1/admin/public-api",
    ] {
        let (status, _, headers) = get(&app, path, None).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "{path}");
        assert_eq!(headers.get("www-authenticate").unwrap(), "Bearer");
        let (status, body, _) = get(&app, path, Some(PLAIN)).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{path}");
        assert_eq!(json(&body)["detail"], "Not found.");
    }
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn admin_reads_carry_steps_versions_users_and_the_closed_gate() {
    let app = app();
    // test_a_run_carries_its_steps_and_its_publish_latency.
    let (_, body, _) = get(&app, "/api/v1/admin/pipeline/runs?limit=5", Some(OWNER)).await;
    let runs = json(&body);
    let published = runs["data"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| !r["data_version"].is_null())
        .unwrap();
    assert!(published["publish_latency_seconds"].is_number());
    assert!(!published["steps"].as_array().unwrap().is_empty());
    // test_an_unknown_run_is_a_404.
    let (status, body, _) = get(&app, "/api/v1/admin/pipeline/runs/999999", Some(OWNER)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(json(&body)["detail"], "No pipeline run with id 999999.");
    // test_it_lists_only_published_runs_and_names_the_current_one.
    let (_, body, _) = get(&app, "/api/v1/admin/data-versions", Some(OWNER)).await;
    let versions = json(&body);
    assert_eq!(versions["current"], versions["data"][0]["data_version"]);
    // test_lookup_matches_on_an_email_fragment.
    let (_, body, _) = get(&app, "/api/v1/admin/users?q=2fe482ed2015", Some(OWNER)).await;
    assert_eq!(json(&body)["data"][0]["public_id"], OWNER);
    // The owner's overrides grant the paid bundle (test_an_override_changes_the_effective_entitlements).
    let (_, body, _) = get(&app, "/api/v1/admin/users/2fe482ed2015", Some(OWNER)).await;
    let detail = json(&body);
    assert_eq!(detail["entitlements"]["export_csv"], true);
    assert!(
        detail["overrides"]
            .as_array()
            .unwrap()
            .iter()
            .all(|o| o["active"] == true)
    );
    // The public API stays shut (D9): the review is not signed off.
    let (_, body, _) = get(&app, "/api/v1/admin/public-api", Some(OWNER)).await;
    let gate = json(&body);
    assert_eq!(gate["serving"], false);
    assert_eq!(gate["review_signed_off"], false);
    assert!(
        gate["withheld_fields"]
            .as_array()
            .unwrap()
            .iter()
            .any(|f| f == "close_raw")
    );
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn integration_lists_need_an_account_and_never_carry_a_secret() {
    let app = app();
    for path in [
        "/api/v1/alerts",
        "/api/v1/keys",
        "/api/v1/webhook-endpoints",
    ] {
        let (status, _, _) = get(&app, path, None).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "{path}");
        let (status, body, _) = get(&app, path, Some(OWNER)).await;
        assert_eq!(status, StatusCode::OK, "{path}");
        let text = String::from_utf8_lossy(&body);
        assert!(
            !text.contains("token_hash") && !text.contains("secret\""),
            "{text}"
        );
    }
    // test_one_account_cannot_see_or_touch_another_accounts_key: an unknown id is a 404.
    let (status, body, _) = get(&app, "/api/v1/keys/nope/usage", Some(OWNER)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(json(&body)["detail"], "No API key with id 'nope'.");
    let (status, body, _) = get(&app, "/api/v1/keys/nope/usage?days=0", Some(OWNER)).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        json(&body)["errors"][0]["message"],
        "days must be between 1 and 366"
    );
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn a_foreign_principal_is_refused_by_every_sole_tenant_surface() {
    // test_curated_tenant_isolation.py: scoped_sole_user_id refuses a principal that is not the
    // tenant, with the machine-readable reason, never promoting it.
    let app = app();
    let sole = sqlx_free_sole_user();
    for path in [
        "/api/v1/watchlist/instruments",
        "/api/v1/watchlist/discover-preferences",
        "/api/v1/cb/pending-actions",
        "/api/v1/cb/updates",
        "/api/v1/cb/trending",
        "/api/v1/cb/investments",
        "/api/v1/cb/fees",
    ] {
        let (status, body, _) = get(&app, path, Some(PLAIN)).await;
        let problem = json(&body);
        match sole {
            Some(_) => {
                assert_eq!(status, StatusCode::NOT_FOUND, "{path}");
                assert_eq!(problem["reason"], "not-the-sole-tenant", "{path}");
            }
            None => assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{path}"),
        }
    }
}

/// Whether the process environment names a sole tenant (the test does not set it: the process
/// environment is shared by every test thread).
fn sqlx_free_sole_user() -> Option<String> {
    std::env::var("BASKFY_SOLE_USER_ID")
        .ok()
        .filter(|v| !v.trim().is_empty())
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_csv_export_is_the_reference_shape_and_gated() {
    let app = app();
    // test_the_export_reproduces_the_reference_shape: BOM, the 93-column header, bare newlines,
    // the name quoted.
    let (status, body, headers) = get(&app, "/api/v1/screens/exmpl0000001/csv", Some(OWNER)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get("content-type").unwrap(),
        "text/csv; charset=utf-8"
    );
    let disposition = headers
        .get("content-disposition")
        .unwrap()
        .to_str()
        .unwrap();
    assert!(
        disposition.starts_with("attachment; filename=\"investing-001-"),
        "{disposition}"
    );
    let text = String::from_utf8(body).unwrap();
    assert!(text.starts_with('\u{feff}'));
    assert!(!text.contains('\r'));
    let mut lines = text.lines();
    assert_eq!(
        lines.next().unwrap().trim_start_matches('\u{feff}'),
        super::csv_export::header_line()
    );
    if let Some(row) = lines.next() {
        assert!(row.starts_with('"'), "{row}");
        assert!(row.matches(',').count() >= 92);
    }
    // A plain account is not entitled: 402 with the upgrade URL.
    let (status, body, _) = get(&app, "/api/v1/screens/exmpl0000001/csv", Some(PLAIN)).await;
    assert_eq!(status, StatusCode::PAYMENT_REQUIRED);
    assert_eq!(json(&body)["upgrade_url"], "/pricing");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn versions_need_an_account_and_a_diff_names_two_versions() {
    let app = app();
    let (status, _, _) = get(&app, "/api/v1/explore/trend-stack/versions", None).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, body, _) = get(&app, "/api/v1/explore/trend-stack/versions", Some(PLAIN)).await;
    assert_eq!(status, StatusCode::OK);
    let versions = json(&body);
    assert_eq!(
        versions["count"],
        versions["versions"].as_array().unwrap().len()
    );
    let (status, body, _) = get(
        &app,
        "/api/v1/explore/trend-stack/versions/diff?from=1&to=1",
        Some(PLAIN),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(json(&body)["type"], "bad-request");
}
