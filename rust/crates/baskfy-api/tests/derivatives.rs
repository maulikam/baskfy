//! The derivatives lane's routers (`options`, `fno`, `overlap`, `backtests`): the GET-path
//! assertions of `test_options_readonly.py`, `test_fno_readonly.py`, `test_overlap_readonly.py`,
//! `test_api_options.py`, `test_api_fno.py` and `test_api_backtests.py`.
//!
//! Database-free (a dead loopback database): method dispatch (every undocumented verb is 405 —
//! and, until the pool may write, the documented writes too, which Python keeps serving), every
//! read refuses an anonymous caller, no path names an order, the download link's refusals.
//! On the bed (`cfg(bed)`): the sole-tenant guard, the entitlement gate, the tenant's 404 and a
//! finished run's event stream. The seeded-database tests (`_completed` runs, scan rows written
//! by the test) are writes and stay with Python; their outputs are graded instead by
//! `tools/parity/api_diff.py` over `tools/parity/api_cases/derivatives.json`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use common::{DEAD_DATABASE, app, bed_url, call, settings, valid};

/// Every GET this lane serves, as the web app calls it.
const READS: [&str; 20] = [
    "/api/v1/options/today",
    "/api/v1/options/scan/O2",
    "/api/v1/options/chain",
    "/api/v1/options/positions",
    "/api/v1/options/sessions",
    "/api/v1/options/journal",
    "/api/v1/options/backtest",
    "/api/v1/options/calendar",
    "/api/v1/options/config",
    "/api/v1/fno/overnight",
    "/api/v1/fno/info",
    "/api/v1/fno/config",
    "/api/v1/overlap/tags/export",
    "/api/v1/overlap/reviews/export",
    "/api/v1/overlap/catalyst-scan",
    "/api/v1/backtests",
    "/api/v1/backtests/aaaaaaaaaaaaaaaaaaaaaaaa",
    "/api/v1/backtests/aaaaaaaaaaaaaaaaaaaaaaaa/trades",
    "/api/v1/backtests/aaaaaaaaaaaaaaaaaaaaaaaa/export",
    "/api/v1/backtests/aaaaaaaaaaaaaaaaaaaaaaaa/events",
];

#[tokio::test]
async fn every_read_requires_an_authenticated_principal() {
    // test_options_readonly / test_fno_readonly / test_overlap_readonly
    // `test_every_route_requires_an_authenticated_principal`, asked of the running app.
    let app = app(settings(DEAD_DATABASE), None);
    for path in READS {
        let answer = call(&app, "GET", path, &[]).await;
        let body = answer.assert_problem(401, "unauthenticated");
        assert_eq!(answer.header("www-authenticate"), Some("Bearer"), "{path}");
        assert_eq!(body["instance"], path);
    }
}

#[tokio::test]
async fn every_other_verb_is_405() {
    // `test_every_other_verb_is_405`: method dispatch happens before authentication. The
    // documented writes (`PATCH /options/config`, `PATCH /fno/config`,
    // `POST /overlap/catalyst-scan`, `POST /backtests`, `DELETE /backtests/{id}`) are served
    // since wave 4c (lane writes-sleeves) and are skipped, as Python's test skips them.
    const WRITES: [(&str, &str); 5] = [
        ("PATCH", "/api/v1/options/config"),
        ("PATCH", "/api/v1/fno/config"),
        ("POST", "/api/v1/overlap/catalyst-scan"),
        ("POST", "/api/v1/backtests"),
        ("DELETE", "/api/v1/backtests/aaaaaaaaaaaaaaaaaaaaaaaa"),
    ];
    let app = app(settings(DEAD_DATABASE), None);
    let mut paths: Vec<&str> = READS.to_vec();
    paths.extend([
        "/api/v1/backtests/aaaaaaaaaaaaaaaaaaaaaaaa/download/trades",
        "/api/v1/backtests/aaaaaaaaaaaaaaaaaaaaaaaa/holdings",
    ]);
    for path in paths {
        for verb in ["POST", "PUT", "PATCH", "DELETE"] {
            if WRITES.contains(&(verb, path)) {
                continue;
            }
            let answer = call(&app, verb, path, &[]).await;
            answer.assert_problem(405, "internal-error");
            assert!(answer.header("allow").is_none(), "{verb} {path}");
        }
    }
}

#[test]
fn no_path_mentions_an_order() {
    // `test_no_options_path_mentions_an_order` / `test_no_fno_path_mentions_an_order`, and the
    // whole lane's: no execute, confirm, close, plan, gtt or roll route — law 2.
    for path in READS {
        let lower = path.to_lowercase();
        for word in [
            "/order", "execute", "confirm", "close", "plan", "gtt", "roll",
        ] {
            assert!(!lower.contains(word), "{path} names {word}");
        }
    }
}

#[test]
fn no_router_source_names_the_execution_package_or_a_broker() {
    // `test_no_module_names_the_execution_package_or_a_broker`, over the Rust sources.
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/routes");
    let mut files = vec![
        root.join("options.rs"),
        root.join("fno.rs"),
        root.join("overlap.rs"),
        root.join("backtests.rs"),
    ];
    for entry in std::fs::read_dir(root.join("derivatives")).unwrap() {
        files.push(entry.unwrap().path());
    }
    for file in files {
        let source = std::fs::read_to_string(&file).unwrap();
        for forbidden in [
            "baskfy_execution",
            "OrderGateway",
            "kiteconnect",
            "place_order",
            "place_gtt",
            "kite_client",
            "build_kite_provider",
            "fno_gates",
            "auto_execute",
        ] {
            assert!(
                !source.contains(forbidden),
                "{} names {forbidden}",
                file.display()
            );
        }
    }
}

#[tokio::test]
async fn a_download_link_that_cannot_be_a_signature_is_refused_before_any_read() {
    // `test_a_forged_download_token_is_refused`, `test_an_expired_download_token_is_refused`,
    // `test_a_non_ascii_token_is_refused_rather_than_crashing`: a 404 — the dead database proves
    // the refusal comes before the row is looked up.
    let app = app(settings(DEAD_DATABASE), None);
    for query in [
        "expires=9999999999&token=%C3%A9%C3%A9%C3%A9",
        "expires=9999999999&token=forged",
        "expires=1&token=x",
        "expires=99999999999999999999999999&token=x",
    ] {
        let uri = format!("/api/v1/backtests/aaaaaaaaaaaaaaaaaaaaaaaa/download/trades?{query}");
        let body = call(&app, "GET", &uri, &[])
            .await
            .assert_problem(404, "not-found");
        assert_eq!(
            body["detail"],
            "No backtest artefact with id 'aaaaaaaaaaaaaaaaaaaaaaaa/trades'."
        );
    }
    // FastAPI's validation: the path's pattern, then the two required query parameters.
    let answer = call(
        &app,
        "GET",
        "/api/v1/backtests/aaaaaaaaaaaaaaaaaaaaaaaa/download/parquet",
        &[],
    )
    .await;
    let body = answer.assert_problem(400, "invalid-screen-definition");
    let fields: Vec<&str> = body["errors"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["field"].as_str().unwrap())
        .collect();
    assert_eq!(fields, ["artefact", "expires", "token"]);
    assert_eq!(body["errors"][0]["type"], "string_pattern_mismatch");
}

#[tokio::test]
async fn an_anonymous_caller_is_refused_before_validation() {
    // `AuthenticatedDep` is a dependency, so it runs before FastAPI validates the query; the
    // validation half (a bearer is needed) runs on the bed below.
    let app = app(settings(DEAD_DATABASE), None);
    for uri in [
        "/api/v1/options/calendar?year=1999",
        "/api/v1/options/scan/O1M?date=nope",
        "/api/v1/backtests?limit=0",
        "/api/v1/backtests/x/export?artefact=parquet",
    ] {
        call(&app, "GET", uri, &[])
            .await
            .assert_problem(401, "unauthenticated");
    }
}

// --- on the bed ----------------------------------------------------------------------------

/// The bed's subscriber's one backtest (failed).
const BACKTEST: &str = "e6f0727d3f8dba80097e76d7";

fn sole_tenant_configured() -> Option<String> {
    std::env::var("BASKFY_SOLE_USER_ID")
        .ok()
        .filter(|v| !v.trim().is_empty())
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_sole_tenant_guard_answers_as_python() {
    // `test_a_principal_who_is_not_the_sole_tenant_is_refused`,
    // `test_another_users_rows_are_not_served`, `test_every_route_scopes_to_the_sole_tenant`.
    let app = app(settings(&bed_url()), None);
    let plain = valid(common::PLAIN);
    for path in [
        "/api/v1/options/today",
        "/api/v1/fno/overnight",
        "/api/v1/overlap/catalyst-scan",
    ] {
        let answer = call(&app, "GET", path, &[("authorization", &plain)]).await;
        if sole_tenant_configured().is_some() {
            let body = answer.assert_problem(404, "not-found");
            assert_eq!(body["reason"], "not-the-sole-tenant", "{path}");
        } else {
            // The bed has no seeded e2e account: the operator-shaped 503 naming the variable.
            let body = answer.assert_problem(503, "pipeline-degraded");
            assert!(
                body["detail"]
                    .as_str()
                    .unwrap()
                    .contains("BASKFY_SOLE_USER_ID"),
                "{body}"
            );
        }
    }
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn validation_comes_before_the_tenant_and_the_entitlement() {
    let app = app(settings(&bed_url()), None);
    let plain = valid(common::PLAIN);
    let answer = call(
        &app,
        "GET",
        "/api/v1/options/calendar?year=2019",
        &[("authorization", &plain)],
    )
    .await;
    let body = answer.assert_problem(400, "invalid-screen-definition");
    assert_eq!(body["errors"][0]["field"], "year");
    assert_eq!(body["errors"][0]["type"], "greater_than_equal");
    let answer = call(
        &app,
        "GET",
        "/api/v1/backtests?limit=0",
        &[("authorization", &plain)],
    )
    .await;
    let body = answer.assert_problem(400, "invalid-screen-definition");
    assert_eq!(body["errors"][0]["field"], "limit");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn backtests_are_entitlement_gated_and_yours_only() {
    // `test_backtests_are_entitlement_gated`, `test_somebody_elses_backtest_is_a_404`,
    // `test_every_backtest_route_is_a_404_for_somebody_else`, `test_list_and_detail`.
    let app = app(settings(&bed_url()), None);
    let plain = valid(common::PLAIN);
    let body = call(
        &app,
        "GET",
        "/api/v1/backtests",
        &[("authorization", &plain)],
    )
    .await
    .assert_problem(402, "payment-required");
    assert_eq!(body["feature"], "backtests");
    assert_eq!(body["upgrade_url"], "/pricing");

    let subscriber = valid(common::SUBSCRIBER);
    let list = call(
        &app,
        "GET",
        "/api/v1/backtests",
        &[("authorization", &subscriber)],
    )
    .await;
    assert_eq!(list.status.as_u16(), 200, "{}", list.text());
    let data = list.json()["data"].as_array().unwrap().clone();
    assert!(data.iter().any(|row| row["public_id"] == BACKTEST));

    let detail = call(
        &app,
        "GET",
        &format!("/api/v1/backtests/{BACKTEST}"),
        &[("authorization", &subscriber)],
    )
    .await;
    assert_eq!(detail.status.as_u16(), 200);
    let detail = detail.json();
    assert_eq!(
        detail["disclaimer"],
        "Past backtest results do not predict future results."
    );
    assert_eq!(detail["config"]["selection"]["top_n"], 20);

    let owner = valid(common::OWNER);
    for tail in ["", "/trades", "/holdings", "/export", "/events"] {
        let answer = call(
            &app,
            "GET",
            &format!("/api/v1/backtests/{BACKTEST}{tail}"),
            &[("authorization", &owner)],
        )
        .await;
        let body = answer.assert_problem(404, "not-found");
        assert_eq!(body["detail"], format!("No backtest with id '{BACKTEST}'."));
    }
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn a_failed_runs_artefacts_do_not_exist_and_its_stream_closes() {
    // `test_artefacts_of_a_queued_run_do_not_exist_yet`,
    // `test_the_event_stream_opens_with_the_current_state`.
    let app = app(settings(&bed_url()), None);
    let subscriber = valid(common::SUBSCRIBER);
    for tail in ["trades", "holdings", "export"] {
        let body = call(
            &app,
            "GET",
            &format!("/api/v1/backtests/{BACKTEST}/{tail}"),
            &[("authorization", &subscriber)],
        )
        .await
        .assert_problem(404, "not-found");
        assert_eq!(
            body["detail"],
            format!("Backtest {BACKTEST} is 'failed'; its artefacts do not exist yet.")
        );
    }
    let events = call(
        &app,
        "GET",
        &format!("/api/v1/backtests/{BACKTEST}/events"),
        &[("authorization", &subscriber)],
    )
    .await;
    assert_eq!(events.status.as_u16(), 200);
    assert_eq!(
        events.header("content-type"),
        Some("text/event-stream; charset=utf-8")
    );
    assert_eq!(events.header("cache-control"), Some("no-store"));
    let text = events.text();
    let frames: Vec<&str> = text.split("\n\n").filter(|f| !f.is_empty()).collect();
    assert_eq!(frames.len(), 2, "{text}");
    assert!(frames[0].starts_with("event: progress\ndata: {\"as_of\":null,\"completed\":1,"));
    assert!(frames[1].starts_with("event: end\ndata: "));
    assert!(frames[0].contains("\"percent\":100") && frames[0].contains("\"status\":\"failed\""));
}
