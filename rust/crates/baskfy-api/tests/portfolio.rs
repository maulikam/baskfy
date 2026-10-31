//! The api-portfolio lane's HTTP tests: the GET-path assertions of `test_api_portfolios.py`,
//! `test_api_portfolios_tree.py`, `test_portfolio_overview.py`, `test_api_trades.py`,
//! `test_brokers.py`, `test_api_billing.py` and `test_track_b_gates.py`, on the bed's real
//! accounts (the Python suites build fixtures; the bed is read-only here, so each assertion is
//! made about the rows the bed has). Byte-equality with Python is `tools/parity/api_diff.py`'s
//! job (`api_cases/portfolio.json`); these hold the contracts the Python tests name.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use common::{DEAD_DATABASE, OWNER, PLAIN, SUBSCRIBER, app, bed_url, call, settings, valid};
use serde_json::Value;

fn bed() -> axum::Router {
    app(settings(&bed_url()), None)
}

fn offline() -> axum::Router {
    app(settings(DEAD_DATABASE), None)
}

async fn get(app: &axum::Router, uri: &str, user: Option<&str>) -> common::Answer {
    match user {
        Some(public_id) => {
            let auth = valid(public_id);
            call(app, "GET", uri, &[("authorization", auth.as_str())]).await
        }
        None => call(app, "GET", uri, &[]).await,
    }
}

// ---------------------------------------------------------------------------------------------
// Without a database
// ---------------------------------------------------------------------------------------------

#[tokio::test]
async fn the_sample_csv_is_a_downloadable_file_for_anyone() {
    // test_api_portfolios.py::TestSampleCsv::test_it_is_served_as_a_downloadable_file
    let a = get(&offline(), "/api/v1/portfolios/sample-csv", None).await;
    assert_eq!(a.status, 200);
    assert_eq!(a.header("content-type"), Some("text/csv; charset=utf-8"));
    assert_eq!(
        a.header("content-disposition"),
        Some("attachment; filename=\"baskfy-portfolio-sample.csv\"")
    );
    assert!(a.text().starts_with("symbol,quantity,avg_price\n"));
}

#[tokio::test]
async fn every_portfolio_route_requires_a_signed_in_account() {
    // test_api_portfolios.py::test_it_requires_authentication and the overview's twin.
    let app = offline();
    for uri in [
        "/api/v1/portfolios",
        "/api/v1/portfolios/3",
        "/api/v1/portfolios/3/holdings",
        "/api/v1/portfolios/3/rebalances",
        "/api/v1/portfolio/overview",
        "/api/v1/portfolio/holdings",
        "/api/v1/portfolio/activity",
        "/api/v1/portfolio/reconciliation",
        "/api/v1/portfolio/suggestions",
        "/api/v1/portfolio/3",
        "/api/v1/portfolio/3/nav",
        "/api/v1/trades",
        "/api/v1/brokers",
        "/api/v1/brokers/zerodha",
        "/api/v1/invoices",
    ] {
        let a = get(&app, uri, None).await;
        a.assert_problem(401, "unauthenticated");
        assert_eq!(a.header("www-authenticate"), Some("Bearer"), "{uri}");
    }
}

#[tokio::test]
async fn track_b_surfaces_stay_dark_while_their_flags_are_off() {
    // test_track_b_gates.py
    let app = offline();
    let flags = get(&app, "/api/v1/cb/track-b/flags", None).await.json();
    assert_eq!(
        flags,
        serde_json::json!({
            "subscriptions_enabled": false,
            "fee_collection_enabled": false,
            "public_signup_enabled": false
        })
    );
    let free = get(
        &app,
        "/api/v1/cb/track-b/free-access?basket_access=FEE",
        None,
    )
    .await
    .json();
    assert_eq!(free["free_access"], true, "every basket is Free Access");
    let paywall = get(&app, "/api/v1/cb/paywall", None).await;
    let body = paywall.assert_problem(404, "not-found");
    assert_eq!(body["detail"], "No route with id 'paywall'.");
}

#[tokio::test]
async fn an_invoice_number_with_a_newline_is_unrouted_as_in_starlette() {
    // `{invoice_number:path}` is `.*`, and a Python regex `.` never matches `\n`: Starlette
    // routes nothing, so the answer is the plain 404 — before the token is even looked at.
    let app = offline();
    for uri in [
        "/api/v1/invoices/a%0Ab/pdf",
        "/api/v1/invoices/%0A/pdf",
        "/api/v1/invoices/DCL/2026-27%0A/000001/pdf",
    ] {
        let a = get(&app, uri, None).await;
        let body = a.assert_problem(404, "not-found");
        assert_eq!(body["detail"], "Not Found", "{uri}");
    }
    // A carriage return is not a newline: that path is routed (and needs a token).
    get(&app, "/api/v1/invoices/a%0D/pdf", None)
        .await
        .assert_problem(401, "unauthenticated");
}

#[tokio::test]
async fn write_routes_answer_only_their_own_verb() {
    // Wave 4c: the lane's write routes are served on their own verb (an anonymous call is the
    // 401, or the Track B stub's 404 while its flag is off); every other verb is Starlette's 405,
    // decided by routing before any dependency runs. `POST /portfolios/{id}/rebalance` is not
    // ported (DECISIONS-RS R6.23): every verb there is a 405 and Python serves the POST.
    let app = offline();
    for (uri, served, anonymous) in [
        ("/api/v1/portfolio", "POST", 401),
        ("/api/v1/portfolio/3/holdings", "POST", 401),
        ("/api/v1/portfolio/reconciliation/1/resolve", "POST", 401),
        ("/api/v1/trades/import", "POST", 401),
        ("/api/v1/trades/sync-today", "POST", 401),
        ("/api/v1/brokers/zerodha/connect", "POST", 401),
        ("/api/v1/brokers/zerodha/sync-holdings", "POST", 401),
        ("/api/v1/cb/paywall/checkout", "POST", 404),
        ("/api/v1/cb/public-signup", "POST", 404),
        ("/api/v1/cb/fees/collect", "POST", 404),
        ("/api/v1/portfolios/3/holdings", "PUT", 401),
        ("/api/v1/portfolios/3/rebalance", "POST", 401),
    ] {
        if served != "PUT" {
            assert_eq!(get(&app, uri, None).await.status, 405, "GET {uri}");
        }
        for method in ["POST", "PUT", "PATCH", "DELETE"] {
            let w = call(&app, method, uri, &[]).await;
            let want = if method == served { anonymous } else { 405 };
            assert_eq!(w.status, want, "{method} {uri}");
        }
    }
    // `/portfolios/import-csv` takes POST; GET, PATCH and DELETE reach `/{portfolio_id}`, as in
    // Starlette (its `int` refuses the segment once the caller is known: here, the 401 first).
    assert_eq!(
        call(&app, "POST", "/api/v1/portfolios/import-csv", &[])
            .await
            .status,
        401
    );
    assert_eq!(
        call(&app, "PUT", "/api/v1/portfolios/import-csv", &[])
            .await
            .status,
        405
    );
    for method in ["PATCH", "DELETE"] {
        let w = call(&app, method, "/api/v1/portfolios/import-csv", &[]).await;
        assert_eq!(w.status, 401, "{method}");
    }
}

#[test]
fn the_routers_name_no_order_path() {
    // test_api_portfolios_tree.py::test_the_router_names_no_order_path, over the Rust sources.
    for (name, source) in [
        ("portfolios.rs", include_str!("../src/routes/portfolios.rs")),
        (
            "portfolio_overview.rs",
            include_str!("../src/routes/portfolio_overview.rs"),
        ),
        ("trades.rs", include_str!("../src/routes/trades.rs")),
        ("brokers.rs", include_str!("../src/routes/brokers.rs")),
        ("billing.rs", include_str!("../src/routes/billing.rs")),
        ("track_b.rs", include_str!("../src/routes/track_b.rs")),
        (
            "brokers/writes.rs",
            include_str!("../src/routes/brokers/writes.rs"),
        ),
        (
            "trades/writes.rs",
            include_str!("../src/routes/trades/writes.rs"),
        ),
        (
            "portfolio_overview/writes.rs",
            include_str!("../src/routes/portfolio_overview/writes.rs"),
        ),
        (
            "portfolio/portfolios_writes.rs",
            include_str!("../src/routes/portfolio/portfolios_writes.rs"),
        ),
        (
            "portfolio/symbols.rs",
            include_str!("../src/routes/portfolio/symbols.rs"),
        ),
    ] {
        for word in [
            "place_order",
            "modify_order",
            "cancel_order",
            "place_gtt",
            "/execute",
        ] {
            assert!(!source.contains(word), "{name} names {word}");
        }
    }
}

// ---------------------------------------------------------------------------------------------
// On the bed
// ---------------------------------------------------------------------------------------------

fn ids_in_forest(nodes: &Value, out: &mut Vec<i64>) {
    for node in nodes.as_array().unwrap() {
        out.push(node["id"].as_i64().unwrap());
        ids_in_forest(&node["children"], out);
    }
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn one_user_never_sees_anothers_portfolio() {
    // test_api_portfolios.py::test_one_user_never_sees_anothers_portfolio and the tree's
    // test_one_users_tree_never_appears_in_anothers_listing / test_another_users_roll_up_is_not_found.
    let app = bed();
    let mine = get(&app, "/api/v1/portfolios", Some(OWNER)).await.json();
    let mut owner_ids = Vec::new();
    ids_in_forest(&mine["data"], &mut owner_ids);
    let theirs = get(&app, "/api/v1/portfolios", Some(SUBSCRIBER))
        .await
        .json();
    let mut subscriber_ids = Vec::new();
    ids_in_forest(&theirs["data"], &mut subscriber_ids);
    assert!(!subscriber_ids.is_empty());
    for id in &subscriber_ids {
        assert!(!owner_ids.contains(id));
        let a = get(&app, &format!("/api/v1/portfolios/{id}"), Some(OWNER)).await;
        let body = a.assert_problem(404, "not-found");
        assert_eq!(body["detail"], format!("No portfolio with id '{id}'."));
        get(
            &app,
            &format!("/api/v1/portfolios/{id}/holdings"),
            Some(OWNER),
        )
        .await
        .assert_problem(404, "not-found");
        get(&app, &format!("/api/v1/portfolio/{id}"), Some(OWNER))
            .await
            .assert_problem(404, "not-found");
    }
    let plain = get(&app, "/api/v1/portfolios", Some(PLAIN)).await.json();
    assert_eq!(plain, serde_json::json!({"data": [], "orphans": []}));
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_forest_holds_every_portfolio_once_with_its_own_count() {
    // test_api_portfolios_tree.py: test_every_portfolio_appears_exactly_once,
    // test_the_nesting_carries_depth_and_the_parent_id, test_holdings_count_stays_this_portfolios_own.
    let app = bed();
    let forest = get(&app, "/api/v1/portfolios", Some(OWNER)).await.json();
    let mut ids = Vec::new();
    ids_in_forest(&forest["data"], &mut ids);
    let mut unique = ids.clone();
    unique.sort_unstable();
    unique.dedup();
    assert_eq!(unique.len(), ids.len());
    for root in forest["data"].as_array().unwrap() {
        assert_eq!(root["depth"], 1);
        assert!(root["parent_id"].is_null());
        let id = root["id"].as_i64().unwrap();
        let detail = get(&app, &format!("/api/v1/portfolios/{id}"), Some(OWNER))
            .await
            .json();
        assert_eq!(
            detail["holdings"].as_array().unwrap().len() as i64,
            root["holdings_count"].as_i64().unwrap()
        );
        assert_eq!(detail["depth"], 1);
    }
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn a_roll_up_total_is_exactly_its_lines_plus_the_unattributed() {
    // test_api_portfolios_tree.py::test_the_total_is_exactly_the_sum_of_the_lines and
    // test_the_subtree_ids_say_what_was_summed.
    let app = bed();
    let forest = get(&app, "/api/v1/portfolios", Some(OWNER)).await.json();
    for root in forest["data"].as_array().unwrap() {
        let id = root["id"].as_i64().unwrap();
        let r = get(
            &app,
            &format!("/api/v1/portfolios/{id}/holdings"),
            Some(OWNER),
        )
        .await
        .json();
        assert_eq!(r["subtree_portfolio_ids"][0], id);
        let mut holdings = r["unattributed"]["holdings"].as_i64().unwrap();
        for line in r["by_broker"].as_array().unwrap() {
            holdings += line["totals"]["holdings"].as_i64().unwrap();
            assert!(
                line["broker_id"].is_string(),
                "the caller's own account is named"
            );
        }
        assert_eq!(holdings, r["total"]["holdings"].as_i64().unwrap());
        assert_eq!(r["rows"].as_array().unwrap().len() as i64, holdings);
    }
}

fn walk_strings(v: &Value, out: &mut Vec<String>) {
    match v {
        Value::String(s) => out.push(s.clone()),
        Value::Array(items) => items.iter().for_each(|i| walk_strings(i, out)),
        Value::Object(map) => map.values().for_each(|i| walk_strings(i, out)),
        _ => {}
    }
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_overview_keeps_its_promises() {
    // test_portfolio_overview.py: totals exclude monitoring views, no monitoring view in the
    // table, every return carries a label and a start date, the consolidated row shows XIRR and
    // TWR as two labelled numbers, the price date and the sync date are separate fields, §9's
    // wording over the rendered payload.
    let app = bed();
    let o = get(&app, "/api/v1/portfolio/overview", Some(OWNER)).await;
    assert_eq!(o.status, 200, "{}", o.text());
    let o = o.json();
    for row in o["portfolios"].as_array().unwrap() {
        assert_eq!(row["kind"], "CAPITAL");
        assert_eq!(row["counts_toward_total"], true);
        assert!(row["headline_return"]["label"].is_string());
        assert!(row["headline_return"]["since"].is_string());
    }
    for row in o["monitoring_views"].as_array().unwrap() {
        assert_eq!(row["kind"], "MONITORING");
        assert_eq!(row["counts_toward_total"], false);
    }
    assert!(o["hero"]["xirr"]["label"].is_string());
    assert!(o["hero"]["twr"]["label"].is_string());
    assert_ne!(o["hero"]["xirr"]["label"], o["hero"]["twr"]["label"]);
    assert!(o.get("prices_as_of").is_some() && o.get("holdings_synced_on").is_some());
    assert_eq!(o["unallocated"]["cta"], "Organize into portfolios");
    let mut strings = Vec::new();
    walk_strings(&o, &mut strings);
    for s in strings {
        let lower = s.to_lowercase();
        for word in ["managed", "advisory", " pms"] {
            assert!(!lower.contains(word), "{s}");
        }
    }
    // The empty picture for an account with nothing.
    let p = get(&app, "/api/v1/portfolio/overview", Some(PLAIN))
        .await
        .json();
    assert_eq!(p["sync_summary"], "No broker connected");
    assert_eq!(p["hero"]["current_value"], "0.00");
    assert!(p["portfolios"].as_array().unwrap().is_empty());
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_holdings_view_never_shows_another_tenants_shares() {
    // test_portfolio_overview.py::test_the_holdings_view_never_shows_another_tenants_shares and
    // test_a_stock_held_at_two_brokers_aggregates_with_the_breakdown_kept.
    let app = bed();
    let mine = get(&app, "/api/v1/portfolio/holdings", Some(OWNER))
        .await
        .json();
    for row in mine["rows"].as_array().unwrap() {
        let lines = row["brokers"].as_array().unwrap();
        assert!(!lines.is_empty());
        for line in lines {
            assert_eq!(line["broker"]["broker_id"], "zerodha");
        }
    }
    let plain = get(&app, "/api/v1/portfolio/holdings", Some(PLAIN))
        .await
        .json();
    assert!(plain["rows"].as_array().unwrap().is_empty());
    assert_eq!(plain["total_value"], "0.00");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn trades_are_the_callers_own_and_validated() {
    // test_api_trades.py's list assertions.
    let app = bed();
    let plain = get(&app, "/api/v1/trades", Some(PLAIN)).await.json();
    assert_eq!(plain["total"], 0);
    assert!(plain["rows"].as_array().unwrap().is_empty());
    let page = get(&app, "/api/v1/trades?limit=3", Some(OWNER))
        .await
        .json();
    let rows = page["rows"].as_array().unwrap();
    assert_eq!(rows.len(), 3);
    for pair in rows.windows(2) {
        assert!(pair[0]["trade_date"].as_str() >= pair[1]["trade_date"].as_str());
    }
    let bad = get(&app, "/api/v1/trades?limit=0&offset=-1", Some(OWNER)).await;
    let body = bad.assert_problem(400, "invalid-screen-definition");
    assert_eq!(body["errors"].as_array().unwrap().len(), 2);
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_broker_grid_and_the_plan_catalogue() {
    // test_brokers.py (catalog, gate, no session on the bed) and test_api_billing.py (public
    // catalogue, the ₹0 tier hidden while its flag is off, the pre-purchase disclaimers).
    let app = bed();
    let grid = get(&app, "/api/v1/brokers", Some(OWNER)).await.json();
    assert_eq!(grid["brokers"].as_array().unwrap().len(), 10);
    assert_eq!(grid["adapters_wired"], 5);
    assert_eq!(grid["gate"]["signed_off"], true);
    assert_eq!(grid["gate"]["connect_configured"], false);
    assert_eq!(grid["brokers"][0]["id"], "zerodha");
    assert_eq!(grid["brokers"][0]["connection_status"], "not_connected");
    get(&app, "/api/v1/brokers/robinhood", Some(OWNER))
        .await
        .assert_problem(404, "not-found");
    let plans = get(&app, "/api/v1/plans", None).await.json();
    assert_eq!(plans["free_tier_enabled"], false);
    assert_eq!(plans["disclaimers"].as_array().unwrap().len(), 5);
    for plan in plans["data"].as_array().unwrap() {
        assert_ne!(plan["code"], "free");
        assert_eq!(plan["currency"], "INR");
        assert!(
            plan["price_inr"].is_string(),
            "money is a string, never a float"
        );
    }
}
