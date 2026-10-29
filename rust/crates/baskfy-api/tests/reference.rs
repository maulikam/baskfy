//! The reference-data lane's routers (`meta`, `market_data`, `instruments`, `search`, `explore`,
//! `kite`, `desk`, `public`): the GET-path assertions of `test_api_meta.py`,
//! `test_api_meta_ranking.py`, `test_meta_live_marks.py`, `test_api_market_data.py`,
//! `test_api_instruments.py`, `test_instrument_appearances.py`, `test_api_search.py`,
//! `test_explore_http.py`, `test_explore_catalog.py`, `test_explore_weight_pct.py`,
//! `test_explore_no_orders.py`, `test_kite_basket.py`, `test_desk_readonly.py`,
//! `test_baskets_readonly.py` and `test_public_api_flag.py`.
//!
//! Database-free (a dead loopback database): the reference data served from code, every
//! refusal FastAPI decides before a query (401 for an anonymous caller on an authenticated route,
//! 400 for a bad parameter), the public API's 404, and the read-only promise over the source.
//! On the bed (`cfg(bed)`): the orderings, shapes and visibility rules over real rows. Python's
//! seeded-database tests (rows the test writes) stay with Python; their outputs are graded by
//! `tools/parity/api_diff.py` over `tools/parity/api_cases/reference.json`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use common::{DEAD_DATABASE, OWNER, PLAIN, app, bed_url, call, settings, valid};

fn fresh() -> axum::Router {
    app(settings(DEAD_DATABASE), None)
}

// ---------------------------------------------------------------------------------------------
// Reference data straight from code
// ---------------------------------------------------------------------------------------------

#[tokio::test]
async fn universes_are_the_fifteen_in_ui_order_with_twelve_breadth_series() {
    let a = call(&fresh(), "GET", "/api/v1/meta/universes", &[]).await;
    assert_eq!(a.status, 200);
    let body = a.json();
    let rows = body.as_array().unwrap();
    assert_eq!(rows.len(), 15);
    let orders: Vec<i64> = rows
        .iter()
        .map(|r| r["sort_order"].as_i64().unwrap())
        .collect();
    let mut sorted = orders.clone();
    sorted.sort_unstable();
    assert_eq!(orders, sorted, "UI order");
    assert_eq!(
        rows.iter().filter(|r| r["market_health"] == true).count(),
        12
    );
    // `nifty-fno` (ui 12) sorts before `nifty-allcap` (ui 13) although its id is larger.
    assert_eq!(rows[11]["slug"], "nifty-fno");
    assert_eq!(rows[12]["slug"], "nifty-allcap");
    assert_eq!(
        a.text()
            .split("\"index_id\"")
            .nth(1)
            .map(|s| s.starts_with(":1,")),
        Some(true)
    );
}

#[tokio::test]
async fn ranking_presets_carry_status_and_a_patch_that_names_every_ranking_field() {
    let body = call(&fresh(), "GET", "/api/v1/meta/ranking-presets", &[])
        .await
        .json();
    let rows = body.as_array().unwrap();
    let keys: Vec<&str> = rows.iter().map(|r| r["key"].as_str().unwrap()).collect();
    assert_eq!(
        keys,
        [
            "desk_quality",
            "path_quality",
            "trend_structure",
            "participation",
            "leadership",
            "nse_momentum",
            "desk_sequential"
        ]
    );
    for row in rows {
        assert_eq!(row["sort_by"], row["patch"]["sort_by"]);
        assert!(["ready", "research"].contains(&row["status"].as_str().unwrap()));
        assert_eq!(row["patch"]["factor_two"]["enabled"], false);
        assert_eq!(row["patch"]["factor_three"]["enabled"], false);
    }
    assert_eq!(rows[0]["status"], "ready");
    assert_eq!(rows[5]["patch"]["index"], "nifty-200");
    assert_eq!(
        rows[5]["label"],
        "NIFTY200 Momentum 30 score (NSE methodology)"
    );
    // `target_min=0.0` is a float on the wire, as pydantic writes it.
    let text = call(&fresh(), "GET", "/api/v1/meta/ranking-presets", &[])
        .await
        .text();
    assert!(
        text.contains(r#""target_min":0.0,"target_max":3.0"#),
        "{text}"
    );
}

// ---------------------------------------------------------------------------------------------
// Refusals decided before any query
// ---------------------------------------------------------------------------------------------

#[tokio::test]
async fn authenticated_reads_refuse_an_anonymous_caller() {
    let app = fresh();
    for path in [
        "/api/v1/meta/live-marks?symbols=RELIANCE",
        "/api/v1/instruments/RELIANCE/appearances",
        "/api/v1/explore",
        "/api/v1/explore?sort=nonsense",
        "/api/v1/explore/managers",
        "/api/v1/explore/managers/maulik",
        "/api/v1/explore/collections",
        "/api/v1/explore/collections/start-here",
        "/api/v1/explore/momentum-scan",
        "/api/v1/explore/momentum-scan/constituents",
        "/api/v1/explore/momentum-scan/performance",
        "/api/v1/explore/momentum-scan/kite?amount=1000",
        "/api/v1/watchlist",
        "/api/v1/baskets/plan/kite",
        "/api/v1/desk/performance",
        "/api/v1/desk/holdings",
        "/api/v1/desk/tradebook?limit=0",
        "/api/v1/desk/regime",
        "/api/v1/desk/reconcile",
    ] {
        let a = call(&app, "GET", path, &[]).await;
        a.assert_problem(401, "unauthenticated");
        assert_eq!(a.header("www-authenticate"), Some("Bearer"), "{path}");
    }
}

#[tokio::test]
async fn query_bounds_are_declared_and_every_failure_is_reported() {
    let app = fresh();
    let errors = |body: &serde_json::Value| -> Vec<(String, String)> {
        body["errors"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| {
                (
                    e["field"].as_str().unwrap().to_owned(),
                    e["type"].as_str().unwrap().to_owned(),
                )
            })
            .collect()
    };
    let a = call(&app, "GET", "/api/v1/instruments?search=&limit=abc", &[]).await;
    let body = a.assert_problem(400, "invalid-screen-definition");
    assert_eq!(
        errors(&body),
        [
            ("search".to_owned(), "string_too_short".to_owned()),
            ("limit".to_owned(), "int_parsing".to_owned())
        ]
    );
    let a = call(&app, "GET", "/api/v1/instruments?search=x&limit=51", &[]).await;
    let body = a.assert_problem(400, "invalid-screen-definition");
    assert_eq!(
        body["errors"][0]["message"],
        "Input should be less than or equal to 50"
    );
    let a = call(&app, "GET", "/api/v1/search?limit=21", &[]).await;
    let body = a.assert_problem(400, "invalid-screen-definition");
    assert_eq!(
        errors(&body),
        [
            ("q".to_owned(), "missing".to_owned()),
            ("limit".to_owned(), "less_than_equal".to_owned())
        ]
    );
    let a = call(
        &app,
        "GET",
        "/api/v1/listings?limit=0&series=ABCDEFGHI&from=x",
        &[],
    )
    .await;
    let body = a.assert_problem(400, "invalid-screen-definition");
    assert_eq!(
        errors(&body),
        [
            ("limit".to_owned(), "greater_than_equal".to_owned()),
            ("series".to_owned(), "string_too_long".to_owned()),
            ("from".to_owned(), "date_from_datetime_parsing".to_owned())
        ]
    );
}

#[tokio::test]
async fn a_universe_without_breadth_and_an_unknown_field_are_refused_by_name() {
    let app = fresh();
    let a = call(&app, "GET", "/api/v1/market-health?universe=nifty-fno", &[]).await;
    let body = a.assert_problem(400, "invalid-screen-definition");
    assert_eq!(
        body["detail"],
        "NIFTY FNO has no breadth series; docs/01 §6 covers twelve universes."
    );
    assert_eq!(body["errors"][0]["field"], "universe");
    assert!(
        body["errors"][0]["message"]
            .as_str()
            .unwrap()
            .starts_with("choose one of ['nifty-100', ")
    );
    let a = call(&app, "GET", "/api/v1/market-health/history?universe=x", &[]).await;
    let body = a.assert_problem(400, "invalid-screen-definition");
    assert_eq!(body["detail"], "'x' is not a universe.");
    let a = call(
        &app,
        "GET",
        "/api/v1/instruments/NOSUCH/history?field=open",
        &[],
    )
    .await;
    let body = a.assert_problem(400, "invalid-screen-definition");
    assert_eq!(
        body["detail"],
        "'open' is not a series this endpoint serves."
    );
}

#[tokio::test]
async fn the_public_api_is_shut_and_answers_a_plain_404() {
    let app = fresh();
    for path in [
        "/api/public/v1/terms",
        "/api/public/v1/status",
        "/api/public/v1/breadth",
        "/api/public/v1/openapi.json",
        "/api/public/v1/instruments/RELIANCE/factors",
    ] {
        call(&app, "GET", path, &[])
            .await
            .assert_problem(404, "not-found");
    }
}

// ---------------------------------------------------------------------------------------------
// Read-only, as a property of the source (test_desk_readonly.py, test_baskets_readonly.py,
// test_explore_no_orders.py)
// ---------------------------------------------------------------------------------------------

#[test]
fn the_lanes_routers_register_no_write_and_name_no_order() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/routes");
    for file in [
        "meta.rs",
        "market_data.rs",
        "instruments.rs",
        "search.rs",
        "explore.rs",
        "kite.rs",
        "desk.rs",
        "public.rs",
    ] {
        let source = std::fs::read_to_string(root.join(file)).unwrap();
        for verb in ["post(", "put(", "patch(", "delete("] {
            assert!(
                !source.contains(&format!("routing::{verb}"))
                    && !source.contains(&format!(" {verb}")),
                "{file} registers a {verb}"
            );
        }
        for word in [
            "place_order",
            "modify_order",
            "cancel_order",
            "OrderGateway",
        ] {
            assert!(!source.contains(word), "{file} names {word}");
        }
    }
    let routes = std::fs::read_to_string(root.parent().unwrap().join("routes.rs")).unwrap();
    assert!(
        !routes.contains("merge(public::router())"),
        "the public API is never mounted (D9)"
    );
}

#[tokio::test]
async fn desk_and_kite_answer_405_to_every_write_verb() {
    let app = fresh();
    for path in [
        "/api/v1/desk/performance",
        "/api/v1/desk/holdings",
        "/api/v1/desk/tradebook",
        "/api/v1/desk/regime",
        "/api/v1/desk/reconcile",
        "/api/v1/baskets/plan/kite",
        "/api/v1/explore/momentum-scan/kite",
    ] {
        for method in ["POST", "PUT", "PATCH", "DELETE"] {
            let a = call(&app, method, path, &[("Authorization", &valid(OWNER))]).await;
            assert_eq!(a.status.as_u16(), 405, "{method} {path}");
        }
    }
}

// ---------------------------------------------------------------------------------------------
// On the bed
// ---------------------------------------------------------------------------------------------

fn bed() -> axum::Router {
    app(settings(&bed_url()), None)
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn status_says_what_is_published_and_whether_it_is_degraded() {
    let a = call(&bed(), "GET", "/api/v1/meta/status", &[]).await;
    assert_eq!(a.status, 200);
    let body = a.json();
    let keys: Vec<&String> = body.as_object().unwrap().keys().collect();
    assert_eq!(keys.len(), 9);
    assert_eq!(body["data_start_date"], "2024-11-01");
    assert_eq!(body["live_quotes"], false, "no Kite session on the bed");
    let run = &body["last_pipeline_run"];
    assert_eq!(
        body["degraded"],
        run["status"] == "failed" || run["status"] == "aborted"
    );
    assert!(body["data_version"].as_i64().unwrap() >= 1);
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn live_marks_off_session_keep_the_close_and_count_distinct_names() {
    let a = call(
        &bed(),
        "GET",
        "/api/v1/meta/live-marks?symbols=reliance,%20RELIANCE%20,,tcs",
        &[("Authorization", &valid(OWNER))],
    )
    .await;
    assert_eq!(a.status, 200);
    let body = a.json();
    assert_eq!(body["live"], false);
    assert_eq!(body["requested"], 2);
    assert_eq!(body["covered"], 0);
    assert_eq!(body["quotes"], serde_json::json!({}));
    assert_eq!(body["stale_after_seconds"], 120);
    assert!(["market_closed", "no_session"].contains(&body["reason"].as_str().unwrap()));
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_dashboard_is_sorted_by_change_with_thirty_day_sparklines_and_snapshot_headers() {
    let a = call(&bed(), "GET", "/api/v1/indices/dashboard", &[]).await;
    assert_eq!(a.status, 200);
    assert!(a.header("x-baskfy-as-of").is_some());
    assert!(a.header("etag").unwrap().starts_with("W/\"v"));
    let body = a.json();
    let rows = body["data"].as_array().unwrap();
    assert!(rows.len() > 100);
    let mut previous = f64::INFINITY;
    let mut seen_null = false;
    for row in rows {
        assert!(row["sparkline"].as_array().unwrap().len() <= 30);
        match row["change_pct"].as_f64() {
            Some(c) => {
                assert!(!seen_null, "NULLS LAST");
                assert!(c <= previous);
                previous = c;
            }
            None => seen_null = true,
        }
    }
    // Decimals are written at their stored precision, unquoted.
    assert!(a.text().contains(".0000") || a.text().contains("00,"));
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn market_health_has_four_gauges_and_the_history_respects_its_range() {
    let app = bed();
    let body = call(&app, "GET", "/api/v1/market-health", &[]).await.json();
    let labels: Vec<&str> = body["gauges"]
        .as_array()
        .unwrap()
        .iter()
        .map(|g| g["label"].as_str().unwrap())
        .collect();
    assert_eq!(
        labels,
        [
            "Above 200 DMA",
            "Above 50 DMA",
            "Within 10% of ATH",
            "1Y Return > 0%"
        ]
    );
    assert_eq!(body["universe"]["slug"], "nifty-500");
    let body = call(
        &app,
        "GET",
        "/api/v1/market-health/history?universe=nifty-50&from=2026-06-01&to=2026-07-01",
        &[],
    )
    .await
    .json();
    assert_eq!(body["from"], "2026-06-01");
    for point in body["points"].as_array().unwrap() {
        let d = point["date"].as_str().unwrap();
        assert!(("2026-06-01"..="2026-07-01").contains(&d));
    }
    let a = call(
        &app,
        "GET",
        "/api/v1/market-health/history?from=2026-10-20&to=2026-10-10",
        &[],
    )
    .await;
    a.assert_problem(400, "invalid-screen-definition");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn listings_paginate_by_keyset_without_skipping_or_repeating() {
    let app = bed();
    let first = call(&app, "GET", "/api/v1/listings?limit=50", &[])
        .await
        .json();
    let cursor = first["next_cursor"].as_str().unwrap().to_owned();
    let second = call(
        &app,
        "GET",
        &format!("/api/v1/listings?limit=50&cursor={cursor}"),
        &[],
    )
    .await
    .json();
    let both = call(&app, "GET", "/api/v1/listings?limit=100", &[])
        .await
        .json();
    let symbols = |v: &serde_json::Value| -> Vec<String> {
        v["data"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["symbol"].as_str().unwrap().to_owned())
            .collect()
    };
    let mut paged = symbols(&first);
    paged.extend(symbols(&second));
    assert_eq!(paged, symbols(&both));
    assert_eq!(first["total"], both["total"], "total ignores the cursor");
    call(&app, "GET", "/api/v1/listings?cursor=garbage!!", &[])
        .await
        .assert_problem(400, "invalid-screen-definition");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_typeahead_ranks_an_exact_symbol_first_and_the_factsheet_mirrors_the_teardown() {
    let app = bed();
    let body = call(&app, "GET", "/api/v1/instruments?search=tcs&limit=5", &[])
        .await
        .json();
    assert_eq!(body["data"][0]["symbol"], "TCS");
    let a = call(&app, "GET", "/api/v1/instruments/reliance", &[]).await;
    assert_eq!(a.status, 200);
    let body = a.json();
    let keys: Vec<&String> = body.as_object().unwrap().keys().collect();
    assert_eq!(keys.len(), 19);
    assert_eq!(body["symbol"], "RELIANCE");
    assert_eq!(body["header"]["exchange"], "NSE");
    assert_eq!(body["returns"].as_array().unwrap().len(), 7);
    assert_eq!(
        body["market_quality"]["circuits"].as_array().unwrap().len(),
        5
    );
    call(&app, "GET", "/api/v1/instruments/NOSUCHSTOCK", &[])
        .await
        .assert_problem(404, "not-found");
    let body = call(
        &app,
        "GET",
        "/api/v1/instruments/KPIGREEN/corporate-actions",
        &[],
    )
    .await
    .json();
    let dates: Vec<&str> = body["data"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["ex_date"].as_str().unwrap())
        .collect();
    let mut sorted = dates.clone();
    sorted.sort_unstable_by(|a, b| b.cmp(a));
    assert_eq!(dates, sorted, "newest first");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn search_never_shows_another_users_screen_or_a_basket_to_an_anonymous_caller() {
    let app = bed();
    let body = call(&app, "GET", "/api/v1/search?q=momentum", &[])
        .await
        .json();
    let kinds: Vec<&str> = body["data"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| h["kind"].as_str().unwrap())
        .collect();
    assert!(!kinds.contains(&"basket"));
    let a = call(
        &app,
        "GET",
        "/api/v1/search?q=untitled",
        &[("Authorization", &valid(PLAIN))],
    )
    .await;
    assert_eq!(a.header("cache-control"), Some("no-store"));
    assert!(
        a.json()["data"]
            .as_array()
            .unwrap()
            .iter()
            .all(|h| h["kind"] != "screen"),
        "the subscriber's screen is not the plain account's hit"
    );
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_catalogue_shows_only_published_baskets_and_its_weights_round_once() {
    let app = bed();
    let auth = [("Authorization", valid(OWNER))];
    let headers: Vec<(&str, &str)> = auth.iter().map(|(k, v)| (*k, v.as_str())).collect();
    let body = call(&app, "GET", "/api/v1/explore", &headers).await.json();
    for item in body["items"].as_array().unwrap() {
        assert_eq!(item["visibility"], "PUBLISHED");
        assert_eq!(item["metrics"]["dividends_included"], false);
    }
    let categories: Vec<&str> = body["categories"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_str().unwrap())
        .collect();
    let mut sorted = categories.clone();
    sorted.sort_unstable();
    assert_eq!(categories, sorted);
    let body = call(
        &app,
        "GET",
        "/api/v1/explore/momentum-scan/constituents",
        &headers,
    )
    .await
    .json();
    for c in body["constituents"].as_array().unwrap() {
        let weight: f64 = c["weight"].as_str().unwrap().parse().unwrap();
        let pct = c["weight_pct"].as_str().unwrap();
        assert_eq!(pct.split('.').nth(1).map(str::len), Some(2), "{pct}");
        assert!((weight * 100.0 - pct.parse::<f64>().unwrap()).abs() < 0.006);
    }
    call(&app, "GET", "/api/v1/explore/nope", &headers)
        .await
        .assert_problem(404, "not-found");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_watchlist_refuses_without_a_sole_tenant_or_to_another_account() {
    let a = call(
        &bed(),
        "GET",
        "/api/v1/watchlist",
        &[("Authorization", &valid(PLAIN))],
    )
    .await;
    if std::env::var("BASKFY_SOLE_USER_ID").is_ok_and(|v| !v.trim().is_empty()) {
        let body = a.assert_problem(404, "not-found");
        assert_eq!(body["reason"], "not-the-sole-tenant");
    } else {
        // The bed has no seeded e2e account: "not configured" is an operator-shaped 503.
        let body = a.assert_problem(503, "pipeline-degraded");
        assert!(
            body["detail"]
                .as_str()
                .unwrap()
                .contains("BASKFY_SOLE_USER_ID")
        );
    }
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_kite_hand_off_is_cnc_market_in_batches_of_ten() {
    let app = bed();
    let auth = valid(OWNER);
    let body = call(
        &app,
        "GET",
        "/api/v1/explore/momentum-scan/kite?amount=1000000",
        &[("Authorization", &auth)],
    )
    .await
    .json();
    assert_eq!(body["url"], "https://kite.zerodha.com/connect/basket");
    assert_eq!(body["configured"], false, "no publisher key on the bed");
    for batch in body["batches"].as_array().unwrap() {
        assert!(batch.as_array().unwrap().len() <= 10);
    }
    for item in body["items"].as_array().unwrap() {
        assert_eq!(item["product"], "CNC");
        assert_eq!(item["order_type"], "MARKET");
        assert_eq!(item["transaction_type"], "BUY");
        assert!(item["quantity"].as_i64().unwrap() > 0);
    }
    call(
        &app,
        "GET",
        "/api/v1/explore/momentum-scan/kite?amount=0",
        &[("Authorization", &auth)],
    )
    .await
    .assert_problem(400, "invalid-screen-definition");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_desk_reads_its_own_schema_and_totals_agree_with_the_rows() {
    let app = bed();
    let auth = valid(OWNER);
    let h = [("Authorization", auth.as_str())];
    let body = call(&app, "GET", "/api/v1/desk/tradebook?limit=5", &h)
        .await
        .json();
    assert!(body["rows"].as_array().unwrap().len() <= 5);
    assert_eq!(
        body["closed_count"].as_i64().unwrap(),
        body["total"].as_i64().unwrap() - body["open_count"].as_i64().unwrap()
    );
    let body = call(&app, "GET", "/api/v1/desk/reconcile", &h).await.json();
    let rows = body["rows"].as_array().unwrap();
    assert_eq!(body["planned_count"].as_u64().unwrap() as usize, rows.len());
    assert_eq!(
        body["planned_count"],
        body["complete_count"].as_i64().unwrap()
            + body["partial_count"].as_i64().unwrap()
            + body["unfilled_count"].as_i64().unwrap()
    );
    assert!(body["created_at"].as_str().unwrap().ends_with("+00:00"));
    for path in [
        "/api/v1/desk/performance",
        "/api/v1/desk/holdings",
        "/api/v1/desk/regime",
    ] {
        assert_eq!(call(&app, "GET", path, &h).await.status, 200, "{path}");
    }
}
