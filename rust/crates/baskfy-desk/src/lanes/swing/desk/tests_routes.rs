//! `tests/test_swing_desk.py`, through the routes: `TestRender`, `TestStatusBar`'s page tests,
//! `TestTriggers`, `TestNoConfirmAll`, `TestExecuteRoute`, `TestExecuteThroughTheRealModule`,
//! `TestTheModuleIsAPage`, `TestNav`, `TestTheRouteReSizesThroughTheRealModule`,
//! `TestPendingRangeOnTheDesk`'s route tests, `TestTheSweepRoutes`, `TestFirstLiveHeaderAndFocus`'s
//! banner, `TestScanNowStatusBar` and `TestScanNowRoute`.
//!
//! **The two fixtures are one here** (see `scenario.rs`): Python's `client` patches
//! `execute_line` with a fake honouring contract C1's HTTP semantics; the Rust desk has nothing
//! to patch, so those tests run the real `execute_line` and assert what the fake asserted —
//! the status codes, the outcome's JSON shape, what the route handed over (the store's user,
//! the clock, the broker's last price, read from the replay Kite's call log) and what landed in
//! the file. Every test ends with the replay Kite having recorded **no** order or GTT call: the
//! twin of `ExplodingKC`, under DRY_RUN.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use axum::http::StatusCode;
use rust_decimal::Decimal;

use super::scenario::{
    Client, LineSpec, Scenario, USER, between, find, forms, from, int_of, iso, ist_at, now, today,
};
use crate::lanes::swing::store::SwingStore;
use crate::storage::Param;

fn d(text: &str) -> Decimal {
    use std::str::FromStr;
    Decimal::from_str(text).unwrap()
}

/// `re.search(r'data-signal-id="%d".*?</tr>' % id, html, re.S).group(0)`.
fn signal_row(html: &str, id: i64) -> String {
    find(&format!(r#"data-signal-id="{id}".*?</tr>"#), html)
}

/// The `quote` routes the replay Kite was asked for, as their `i` keys.
fn quoted(c: &Client) -> Vec<String> {
    c.kite
        .calls()
        .iter()
        .filter(|call| call.route.starts_with("market.quote"))
        .flat_map(|call| {
            match &call.params {
                Some(crate::wire::PyValue::Map(m)) => m.get("i").cloned(),
                _ => None,
            }
            .map(|v| match v {
                crate::wire::PyValue::List(items) => {
                    items.iter().map(crate::wire::PyValue::py_str).collect()
                }
                other => vec![other.py_str()],
            })
            .unwrap_or_default()
        })
        .collect()
}

async fn morning() -> (Scenario, super::scenario::Morning, Client) {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let c = Client::new(&sc).await;
    (sc, ids, c)
}

async fn execute(c: &Client, plan_id: &str, line_id: i64) -> super::scenario::Reply {
    c.post(
        "/swing/execute",
        &[
            ("plan_id", plan_id),
            ("line_id", &line_id.to_string()),
            ("confirm", "true"),
        ],
    )
    .await
}

// =============================================================================================
// TestRender
// =============================================================================================

#[tokio::test]
async fn test_render_is_200_with_the_three_panels() {
    let (_sc, _ids, c) = morning().await;
    let r = c.get("/swing").await;
    assert_eq!(r.status, StatusCode::OK);
    let html = r.text;
    for s in [
        r#"<section id="triggers">"#,
        r#"<section id="plan">"#,
        r#"<section id="book">"#,
    ] {
        assert!(html.contains(s), "{s}");
    }
    let (t, p, b) = (
        html.find(r#"id="triggers""#).unwrap(),
        html.find(r#"id="plan""#).unwrap(),
        html.find(r#"id="book""#).unwrap(),
    );
    assert!(t < p && p < b);
}

#[tokio::test]
async fn test_render_of_an_empty_book_still_says_why() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let c = Client::new(&sc).await;
    let html = c.html("/swing").await;
    for s in [
        "No signals today",
        "No morning plan for 2026-09-02",
        "No open swing positions",
        "Nothing managed yet",
    ] {
        assert!(html.contains(s), "{s}");
    }
}

#[tokio::test]
async fn test_render_when_the_store_cannot_be_read_is_a_banner_not_a_500() {
    // Python makes `open_store` raise "no such table: sw_plan"; here the desk's database is a
    // file without the swing tables, so the store's first read raises the driver's own words.
    let sc = Scenario::new().await;
    let c = Client::new(&sc).await;
    c.settings(|s| s.db_path = sc.dir.path().join("empty.db").display().to_string());
    let state = c.state.clone();
    let db = crate::storage::Db::open(crate::storage::DbConfig::from_settings(
        &state.settings_snapshot(),
        None,
    ))
    .await
    .unwrap();
    let state = crate::app::AppState { db, ..state };
    let app = crate::app::build_app(state);
    let r = tower::ServiceExt::oneshot(
        app,
        axum::http::Request::get("/swing")
            .header(axum::http::header::HOST, "127.0.0.1")
            .body(axum::body::Body::empty())
            .unwrap(),
    )
    .await
    .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let text = String::from_utf8(
        axum::body::to_bytes(r.into_body(), usize::MAX)
            .await
            .unwrap()
            .to_vec(),
    )
    .unwrap();
    assert!(text.contains("unavailable") && text.contains("no such table"));
    assert!(text.contains("DRY_RUN"), "the status bar still renders");
}

#[tokio::test]
async fn test_panels_order_exits_then_waiting_buys_then_skips() {
    let (_sc, _ids, c) = morning().await;
    let html = c.html("/swing").await;
    let plan = between(&html, r#"<section id="plan">"#, r#"<section id="book">"#);
    let sell = plan.find("SWING SELL DELTAHELD").unwrap();
    let raise = plan.find("SWING RAISE GTT DELTAHELD").unwrap();
    let waiting = plan.find("SWING BUY ALPHAFLAG").unwrap();
    let skip = plan.find("LOCKED_UPPER_CIRCUIT").unwrap();
    assert!(sell < raise && raise < waiting && waiting < skip);
    assert!(
        plan.contains("upper circuit at 52.50"),
        "the skip carries its detail"
    );
}

#[tokio::test]
async fn test_panels_book_shows_gtt_ids_naked_and_the_manage_actions() {
    let (_sc, _ids, c) = morning().await;
    let html = c.html("/swing").await;
    let book = from(&html, r#"<section id="book">"#);
    for s in [
        "#G-44",
        "naked",
        "1 without a stop",
        "Re-arm GTT",
        "manage action",
        "SWING SELL DELTAHELD x100 at open",
        "SWING RAISE GTT DELTAHELD to 100.80",
    ] {
        assert!(book.contains(s), "{s}");
    }
}

#[tokio::test]
async fn test_panels_data_route_is_the_same_view_as_json() {
    let (_sc, _ids, c) = morning().await;
    let j = c.get("/swing/data").await.json();
    assert_eq!(j["available"], true);
    for k in [
        "triggers",
        "plans",
        "book",
        "status",
        "fingerprint",
        "poll_ms",
    ] {
        assert!(j.get(k).is_some(), "{k}");
    }
    assert_eq!(j["triggers"].as_array().unwrap().len(), 3);
    assert_eq!(j["plans"]["morning"]["exits"][0]["kind"], "SELL_AT_OPEN");
    // Decimals cross as strings — money is never a float on the wire
    assert_eq!(j["plans"]["morning"]["buys"][0]["trigger"], "100.80");
    assert_eq!(j["poll_ms"], 5000);
}

// =============================================================================================
// TestStatusBar (the page)
// =============================================================================================

#[tokio::test]
async fn test_status_bar_carries_the_flags_the_monitor_the_token_and_the_counters() {
    let (_sc, _ids, c) = morning().await;
    let html = c.html("/swing").await;
    let bar = between(&html, r#"id="swStatus""#, r#"<section id="triggers">"#);
    for s in [
        "DRY_RUN</b>=<code>true</code>",
        "BASKFY_SWING_EXECUTION_ENABLED</b>=<code>false</code>",
        "not enabled",
        "no token",
        "signals 3",
        "confirms 1",
        "fills 1",
        "manage actions 0",
        "SIMULATED",
    ] {
        assert!(bar.contains(s), "{s}");
    }
}

#[tokio::test]
async fn test_status_bar_reads_the_session_row_when_there_is_none() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let c = Client::new(&sc).await;
    let bar = c.html("/swing").await;
    assert!(bar.contains("no <code>sw_session</code> row yet"));
    assert!(bar.contains("signals 0"));
}

// =============================================================================================
// TestTriggers
// =============================================================================================

#[tokio::test]
async fn test_confirm_button_and_countdown_on_a_triggered_signal() {
    let (_sc, ids, c) = morning().await;
    let html = c.html("/swing").await;
    let triggers = between(
        &html,
        r#"<section id="triggers">"#,
        r#"<section id="plan">"#,
    );
    let row = find(
        &format!(
            r#"<tr class="sw-trigger sw-trigger-triggered" data-signal-id="{}".*?</tr>"#,
            ids.trigger
        ),
        triggers,
    );
    assert!(row.contains("5-min ORH 100.50 broken at 100.80"));
    assert!(row.contains(">Confirm</button>"));
    let f = forms(&row, "/swing/execute");
    assert_eq!(f.len(), 1);
    assert!(f[0].contains(&format!(r#"name="line_id" value="{}""#, ids.trigger_line)));
    assert!(f[0].contains(&format!(r#"name="plan_id" value="{}""#, ids.signal_plan.1)));
    assert!(f[0].contains(r#"name="confirm" value="true""#));
    // built 09:31, page at 09:40 → 21:00 left on a 30-minute plan
    assert!(
        regex::Regex::new(r#"class="sw-countdown "[^>]*>21:00<"#)
            .unwrap()
            .is_match(&row)
    );
    assert!(row.contains(&ids.signal_plan.1[..8]));
}

#[tokio::test]
async fn test_confirm_button_is_absent_once_the_plan_has_expired_but_the_row_stays() {
    let (_sc, ids, c) = morning().await;
    c.at(ist_at(2026, 9, 2, 10, 30));
    let html = c.html("/swing").await;
    let row = signal_row(&html, ids.trigger);
    assert!(row.contains("ALPHAFLAG"));
    assert!(row.contains(">0:00<") && row.contains("plan expired"));
    assert!(forms(&row, "/swing/execute").is_empty());
}

#[tokio::test]
async fn test_locked_signal_shows_without_a_button() {
    let (_sc, ids, c) = morning().await;
    let html = c.html("/swing").await;
    let row = signal_row(&html, ids.locked);
    assert!(row.contains("GAMMALOCK") && row.contains("locked at the upper circuit"));
    assert!(!row.contains("Confirm"));
    assert!(forms(&row, "/swing/execute").is_empty());
}

#[tokio::test]
async fn test_locked_and_skipped_triggers_say_why_there_is_no_line() {
    let (_sc, ids, c) = morning().await;
    let html = c.html("/swing").await;
    let row = signal_row(&html, ids.skipped);
    assert!(row.contains("EPSILON"));
    assert!(row.contains("skipped \u{2014} TIER_FULL"));
    assert!(row.contains("2 of 2 positions open"));
    assert!(forms(&row, "/swing/execute").is_empty());
}

#[tokio::test]
#[allow(non_snake_case)]
async fn test_SWING_prefix_on_every_line() {
    let (_sc, _ids, c) = morning().await;
    let html = c.html("/swing").await;
    let joined = forms(&html, "/swing/execute").concat();
    let titles: Vec<String> = regex::Regex::new(r#"title="([^"]+)""#)
        .unwrap()
        .captures_iter(&joined)
        .map(|m| m[1].to_owned())
        .collect();
    assert!(!titles.is_empty() && titles.iter().all(|t| t.starts_with("SWING ")));
    for label in [
        "SWING BUY ALPHAFLAG x1666 @ 100.80",
        "SWING SELL DELTAHELD x100 at open",
        "SWING RAISE GTT DELTAHELD to 100.80",
    ] {
        assert!(html.contains(label), "{label}");
    }
}

#[tokio::test]
async fn test_prefix_and_sizing_line_carry_risk_share_and_cap() {
    let (_sc, _ids, c) = morning().await;
    let html = c.html("/swing").await;
    assert!(html.contains("risk \u{20b9}300.00"));
    assert!(html.contains("1.01% of allocation"), "10,080 of 10,00,000");
    assert!(html.contains("cap 20.00%"));
}

#[tokio::test]
async fn test_triggers_newest_first() {
    let (_sc, _ids, c) = morning().await;
    let j = c.get("/swing/data").await.json();
    let times: Vec<String> = j["triggers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["raised_at"].as_str().unwrap()[11..16].to_owned())
        .collect();
    assert_eq!(times, ["09:35", "09:31", "09:20"]);
}

// =============================================================================================
// TestNoConfirmAll
// =============================================================================================

#[tokio::test]
async fn test_one_form_per_line_and_one_line_id_per_form() {
    let (_sc, ids, c) = morning().await;
    let html = c.html("/swing").await;
    let f = forms(&html, "/swing/execute");
    // the trigger, the SELL, the RAISE, the waiting ALPHAFLAG buy; BETAEP is held → none
    assert_eq!(f.len(), 4);
    let re = regex::Regex::new(r#"name="line_id" value="(\d+)""#).unwrap();
    let mut line_ids = Vec::new();
    for form in &f {
        let found: Vec<i64> = re
            .captures_iter(form)
            .map(|m| m[1].parse().unwrap())
            .collect();
        assert_eq!(found.len(), 1, "{form}");
        assert_eq!(form.matches("<button").count(), 1);
        line_ids.push(found[0]);
    }
    let mut want = vec![ids.trigger_line, ids.sell, ids.raise, ids.waiting];
    want.sort_unstable();
    let mut got = line_ids.clone();
    got.sort_unstable();
    assert_eq!(got, want);
    got.dedup();
    assert_eq!(got.len(), line_ids.len());
}

#[tokio::test]
async fn test_no_confirm_all_control_anywhere() {
    let (_sc, _ids, c) = morning().await;
    let html = c.html("/swing").await;
    let lowered = html.to_lowercase();
    assert!(!lowered.contains("confirm all"));
    assert!(
        !lowered
            .replace("no confirm-all", "")
            .contains("confirm-all")
    );
    assert!(!html.contains(r#"name="line_ids"#) && !html.contains(r#"name="line_id[]"#));
    assert!(!lowered.contains("select all"));
    // the only buttons on the page are one per form (SW15's Scan now is one form too)
    assert_eq!(
        html.matches("<button").count(),
        forms(&html, "/swing/execute").len()
            + forms(&html, "/swing/rearm").len()
            + forms(&html, "/swing/scan").len()
    );
}

#[test]
fn test_no_confirm_all_in_the_template_source_either() {
    let src =
        std::fs::read_to_string(super::scenario::desk_root().join("app/templates/swing.html"))
            .unwrap();
    assert_eq!(
        src.matches(r#"action="/swing/execute""#).count(),
        1,
        "one macro, one form shape"
    );
    assert!(!src.contains(r#"type="checkbox""#));
}

// =============================================================================================
// TestExecuteRoute
// =============================================================================================

#[tokio::test]
async fn test_execute_calls_the_execute_module_and_returns_its_outcome() {
    let (sc, ids, c) = morning().await;
    let r = execute(&c, &ids.signal_plan.1, ids.trigger_line).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    let j = r.json();
    assert_eq!(j["status"], "SIMULATED");
    assert_eq!(j["simulated"], true);
    assert_eq!(j["line_id"], ids.trigger_line);
    let position_id = j["position_id"].as_i64().unwrap();
    assert_eq!(j["gtt"]["status"], "DRY_RUN_GTT");
    // What the route handed over: the sole user's store (the position is that user's), the
    // clock's `now` (the GTT was armed at 09:40 IST), and — SW22, the entry is a MARKET order —
    // the broker's last price for the BUY, read through the Kite wrapper.
    let mut store = sc.store().await;
    let pos = store.position(position_id).await.unwrap().unwrap();
    assert_eq!(pos.gtt_armed_at, Some(now()));
    assert_eq!(quoted(&c), ["NSE:ALPHAFLAG"]);
    // the store the route handed over wrote through to the file
    assert_eq!(
        store.line(ids.trigger_line).await.unwrap().unwrap().state,
        "FILLED"
    );
    assert_eq!(store.session(today()).await.unwrap().unwrap().confirms, 2);
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_execute_of_an_exit_passes_the_brokers_last_price() {
    let (sc, ids, c) = morning().await;
    let r = execute(&c, &ids.morning.1, ids.sell).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    assert_eq!(quoted(&c), ["NSE:DELTAHELD"]);
    // the simulated fill is the quote: 104.00
    let mut store = sc.store().await;
    assert_eq!(
        store.position(ids.held).await.unwrap().unwrap().exit_avg,
        Some(d("104.0000"))
    );
    let r = c
        .post(
            "/swing/rearm",
            &[("position_id", &ids.naked.to_string()), ("confirm", "true")],
        )
        .await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    assert_eq!(quoted(&c), ["NSE:DELTAHELD", "NSE:BETAEP"]);
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_execute_without_a_broker_session_passes_no_price_rather_than_a_guess() {
    let sc = Scenario::new().await;
    let c = Client::new(&sc).await;
    c.settings(|s| s.kite_api_key = String::new()); // Kite() refuses to build
    assert_eq!(super::last_price(&c.state, "DELTAHELD").await, None);
    assert!(c.kite.calls().is_empty());
}

#[tokio::test]
async fn test_execute_last_price_is_a_read_through_the_kite_wrapper() {
    let sc = Scenario::new().await;
    let c = Client::with_quotes(&sc, &[("DELTAHELD", 444, 104.5)]).await;
    assert_eq!(
        super::last_price(&c.state, "DELTAHELD").await,
        Some(d("104.5"))
    );
    assert_eq!(quoted(&c), ["NSE:DELTAHELD"]);
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_execute_without_confirm_is_400_through_the_route() {
    let (sc, ids, c) = morning().await;
    let r = c
        .post(
            "/swing/execute",
            &[
                ("plan_id", &ids.signal_plan.1),
                ("line_id", &ids.trigger_line.to_string()),
                ("confirm", "false"),
            ],
        )
        .await;
    assert_eq!(r.status, StatusCode::BAD_REQUEST);
    let mut store = sc.store().await;
    assert_eq!(
        store.line(ids.trigger_line).await.unwrap().unwrap().state,
        "PROPOSED"
    );
}

#[tokio::test]
async fn test_execute_missing_confirm_field_is_a_validation_error_not_an_order() {
    let (sc, ids, c) = morning().await;
    let r = c
        .post(
            "/swing/execute",
            &[
                ("plan_id", &ids.signal_plan.1),
                ("line_id", &ids.trigger_line.to_string()),
            ],
        )
        .await;
    assert_eq!(r.status, StatusCode::UNPROCESSABLE_ENTITY);
    // `fake_execute == []`: the execute path was never entered — no price read, no write.
    assert!(c.kite.calls().is_empty());
    let mut store = sc.store().await;
    assert_eq!(
        store.line(ids.trigger_line).await.unwrap().unwrap().state,
        "PROPOSED"
    );
    assert_eq!(store.session(today()).await.unwrap().unwrap().confirms, 1);
}

#[tokio::test]
async fn test_execute_of_an_expired_plan_is_410_through_the_route() {
    let (_sc, ids, c) = morning().await;
    c.at(ist_at(2026, 9, 2, 10, 30));
    let r = execute(&c, &ids.signal_plan.1, ids.trigger_line).await;
    assert_eq!(r.status, StatusCode::GONE);
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_execute_of_an_unknown_line_is_404_and_a_confirmed_line_409() {
    let (sc, ids, c) = morning().await;
    let r = execute(&c, &ids.signal_plan.1, 999_999).await;
    assert_eq!(r.status, StatusCode::NOT_FOUND);
    let mut store = sc.store().await;
    store
        .set_line(ids.trigger_line, "FILLED", None, None)
        .await
        .unwrap();
    let r = execute(&c, &ids.signal_plan.1, ids.trigger_line).await;
    assert_eq!(r.status, StatusCode::CONFLICT);
}

/// Python: monkeypatching `app.swing_execute.execute_line` is enough, because the route holds no
/// reference of its own. Rust has no attribute to patch; the same property is that the route
/// owns no execute logic: it calls `execute::execute_line` and writes nothing to the book itself.
#[tokio::test]
async fn test_execute_route_resolves_the_function_at_call_time() {
    let source = include_str!("../desk.rs");
    let start = source.find("async fn swing_execute(").unwrap();
    let end = source.find("async fn swing_scan(").unwrap();
    let route = &source[start..end];
    assert_eq!(route.matches("execute_line(").count(), 1);
    for write in [
        "set_line(",
        "create_position(",
        "update_position(",
        "add_fill(",
        "bump_session(",
    ] {
        assert!(!route.contains(write), "the route writes {write} itself");
    }
    let (_sc, ids, c) = morning().await;
    let r = execute(&c, &ids.signal_plan.1, ids.trigger_line).await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(r.json()["line_id"], ids.trigger_line);
}

#[tokio::test]
#[allow(non_snake_case)]
async fn test_execute_a_guard_refusal_is_BLOCKED_with_the_guards_words_not_a_500() {
    // Python's fake raises UntouchableInstrumentError after marking the line REJECTED; here a
    // real SGB line on a live SIGNAL plan reaches the real guard.
    let mut sc = Scenario::new().await;
    sc.config().await;
    sc.exec(
        "INSERT INTO instrument(id, symbol, name, kite_token) VALUES (6, 'SGBDE31III', 'SGB', 666)",
        &[],
    )
    .await;
    sc.detected(6, "FLAG").await;
    let (pk, plan_id) = sc.plan("SIGNAL", ist_at(2026, 9, 2, 9, 31)).await;
    let line_id = sc
        .line(
            pk,
            &plan_id,
            LineSpec {
                quantity: 10,
                ..LineSpec::new("BUY_ON_TRIGGER", 6, "SGBDE31III")
            },
        )
        .await;
    let c = Client::with_quotes(&sc, &[("SGBDE31III", 666, 100.8)]).await;
    let r = execute(&c, &plan_id, line_id).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    let j = r.json();
    assert_eq!(j["status"], "BLOCKED");
    assert!(
        j["reason"]
            .as_str()
            .unwrap()
            .contains("protected instrument (SGB/G-sec)"),
        "{j}"
    );
    assert_eq!(j["line_id"], line_id);
    assert_eq!(j["simulated"], true);
    let mut store = sc.store().await;
    assert_eq!(
        store.line(line_id).await.unwrap().unwrap().state,
        "REJECTED"
    );
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_execute_rearm_route_goes_the_same_way() {
    let (_sc, ids, c) = morning().await;
    let naked = ids.naked.to_string();
    let r = c
        .post(
            "/swing/rearm",
            &[("position_id", &naked), ("confirm", "true")],
        )
        .await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(r.json()["status"], "SIMULATED");
    assert_eq!(r.json()["position_id"], ids.naked);
    let r = c
        .post(
            "/swing/rearm",
            &[("position_id", &ids.held.to_string()), ("confirm", "true")],
        )
        .await;
    assert_eq!(r.json()["status"], "BLOCKED");
    let r = c
        .post(
            "/swing/rearm",
            &[("position_id", &naked), ("confirm", "no")],
        )
        .await;
    assert_eq!(r.status, StatusCode::BAD_REQUEST);
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_execute_rearm_form_exists_only_for_the_naked_position() {
    let (_sc, ids, c) = morning().await;
    let html = c.html("/swing").await;
    let f = forms(&html, "/swing/rearm");
    assert_eq!(f.len(), 1);
    assert!(f[0].contains(&format!(r#"name="position_id" value="{}""#, ids.naked)));
}

#[tokio::test]
async fn test_execute_route_needs_a_same_origin_post() {
    let (sc, ids, c) = morning().await;
    let r = c
        .post_from(
            "/swing/execute",
            &[
                ("plan_id", &ids.signal_plan.1),
                ("line_id", &ids.trigger_line.to_string()),
                ("confirm", "true"),
            ],
            Some("http://evil.example"),
        )
        .await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    let mut store = sc.store().await;
    assert_eq!(
        store.line(ids.trigger_line).await.unwrap().unwrap().state,
        "PROPOSED"
    );
}

// =============================================================================================
// TestExecuteThroughTheRealModule
// =============================================================================================

#[tokio::test]
async fn test_execute_a_buy_end_to_end_is_simulated_and_the_book_is_written() {
    let (sc, ids, c) = morning().await;
    let r = execute(&c, &ids.signal_plan.1, ids.trigger_line).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    let j = r.json();
    assert_eq!(
        (j["status"].clone(), j["simulated"].clone()),
        ("SIMULATED".into(), true.into())
    );
    assert_eq!(j["reason"], "");
    assert_eq!(j["line_id"], ids.trigger_line);
    let pid = j["position_id"].as_i64().unwrap();
    assert!(pid > 0);
    assert_eq!(j["order"]["status"], "DRY_RUN");
    assert_eq!(j["gtt"]["status"], "DRY_RUN_GTT");
    let mut s = sc.store().await;
    let line = s.line(ids.trigger_line).await.unwrap().unwrap();
    assert_eq!(
        (line.state.as_str(), line.position_id),
        ("FILLED", Some(pid))
    );
    let pos = s.position(pid).await.unwrap().unwrap();
    assert_eq!(
        (pos.symbol.as_str(), pos.quantity_open),
        ("ALPHAFLAG", 1666)
    );
    assert_eq!(pos.entry_avg, Some(d("100.80")));
    assert_eq!(pos.stop, Some(d("97.80")));
    assert!(pos.gtt_id.as_deref().unwrap().starts_with("DRY-"));
    assert!(pos.simulated);
    assert_eq!(pos.gtt_armed_at, Some(now()));
    let fills = s.fills_for(pid).await.unwrap();
    assert_eq!(fills.len(), 1);
    assert_eq!((fills[0].side.as_str(), fills[0].quantity), ("BUY", 1666));
    assert!(fills[0].simulated);
    let session = s.session(today()).await.unwrap().unwrap();
    assert_eq!(
        (session.confirms, session.fills),
        (2, 2),
        "the scenario had 1 and 1"
    );
    // and the page now shows the position, armed, and the line as done
    let html = c.html("/swing").await;
    assert!(from(&html, r#"<section id="book">"#).contains("ALPHAFLAG"));
    let row = signal_row(&html, ids.trigger);
    assert!(row.contains("filled") && forms(&row, "/swing/execute").is_empty());
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_execute_without_confirm_is_400_through_the_real_module() {
    let (sc, ids, c) = morning().await;
    let r = c
        .post(
            "/swing/execute",
            &[
                ("plan_id", &ids.signal_plan.1),
                ("line_id", &ids.trigger_line.to_string()),
                ("confirm", "yes"),
            ],
        )
        .await;
    assert_eq!(r.status, StatusCode::BAD_REQUEST);
    let mut store = sc.store().await;
    assert_eq!(
        store.line(ids.trigger_line).await.unwrap().unwrap().state,
        "PROPOSED"
    );
}

#[tokio::test]
async fn test_execute_expired_is_410_through_the_real_module_and_marks_the_line() {
    let (sc, ids, c) = morning().await;
    c.at(ist_at(2026, 9, 2, 10, 30));
    let r = execute(&c, &ids.signal_plan.1, ids.trigger_line).await;
    assert_eq!(r.status, StatusCode::GONE);
    let mut store = sc.store().await;
    assert_eq!(
        store.line(ids.trigger_line).await.unwrap().unwrap().state,
        "EXPIRED"
    );
}

#[tokio::test]
async fn test_execute_a_sell_partial_through_the_real_module() {
    let (sc, ids, c) = morning().await;
    let r = execute(&c, &ids.morning.1, ids.sell).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    assert_eq!(r.json()["status"], "SIMULATED", "{}", r.text);
    let mut store = sc.store().await;
    let pos = store.position(ids.held).await.unwrap().unwrap();
    assert_eq!((pos.quantity_open, pos.state.as_str()), (200, "PARTIAL"));
    assert!(pos.partial_done);
    assert_eq!(pos.exit_avg, Some(d("104.0000")));
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_execute_rearm_through_the_real_module() {
    let (sc, ids, c) = morning().await;
    let naked = ids.naked.to_string();
    let r = c
        .post(
            "/swing/rearm",
            &[("position_id", &naked), ("confirm", "true")],
        )
        .await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    assert_eq!(r.json()["status"], "SIMULATED");
    let mut store = sc.store().await;
    let pos = store.position(ids.naked).await.unwrap().unwrap();
    assert!(pos.gtt_id.is_some());
    assert_eq!(pos.gtt_trigger, Some(d("204.50")));
    // a second re-arm is BLOCKED: two triggers would sell twice what is held
    let r = c
        .post(
            "/swing/rearm",
            &[("position_id", &naked), ("confirm", "true")],
        )
        .await;
    assert_eq!(r.json()["status"], "BLOCKED");
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_execute_no_order_reached_the_broker_in_this_class() {
    let (_sc, ids, c) = morning().await;
    let r = execute(&c, &ids.morning.1, ids.raise).await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(r.json()["status"], "SIMULATED");
    assert_eq!(c.broker_writes(), 0);
}

// =============================================================================================
// TestTheModuleIsAPage
// =============================================================================================

fn code_only(source: &str) -> String {
    source
        .lines()
        .filter(|l| {
            let t = l.trim_start();
            !t.starts_with("//") && !t.starts_with("#[")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn test_execute_verbs_and_the_broker_are_never_named() {
    let source = code_only(include_str!("../desk.rs"));
    let source = &source[..source.find("mod tests;").unwrap_or(source.len())];
    for forbidden in [
        "kc.place_order",
        "place_gtt_stop(",
        "delete_gtt(",
        "gw.place(",
        "kiteconnect",
        ".place(",
        "place_order",
    ] {
        assert!(!source.contains(forbidden), "{forbidden}");
    }
}

#[test]
fn test_the_gateway_is_built_lazily_never_at_import() {
    let source = code_only(include_str!("../desk.rs"));
    // no process-wide gateway cell
    for cell in ["static ", "OnceLock", "OnceCell", "lazy_static", "LazyLock"] {
        assert!(!source.contains(cell), "{cell}");
    }
    // the sleeve's gateway is built only inside swing_gateway()
    let start = source.find("pub async fn swing_gateway(").unwrap();
    let end = source[start..].find("\n}\n").unwrap() + start;
    assert_eq!(source.matches("sleeve_gateway(").count(), 1);
    assert!(source[start..end].contains("sleeve_gateway("));
}

// =============================================================================================
// TestNav
// =============================================================================================

/// Pages another lane has not ported yet (DECISIONS-RS R8.24): Python serves them; the Rust desk
/// answers 404 until they land, and this test then checks them like the rest.
const NOT_YET_PORTED: [&str; 3] = ["/", "/performance", "/tradebook"];

#[tokio::test]
async fn test_nav_links_to_swing_from_every_desk_page_and_back() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let c = Client::new(&sc).await;
    let routes = [
        "/",
        "/performance",
        "/regime",
        "/regime/backtest",
        "/tradebook",
        "/settings",
        "/swing",
    ];
    let href = regex::Regex::new(r#"href="([^"]+)""#).unwrap();
    for page in routes {
        let r = c.get(page).await;
        if r.status == StatusCode::NOT_FOUND {
            assert!(NOT_YET_PORTED.contains(&page), "{page} is 404");
            continue;
        }
        assert_eq!(r.status, StatusCode::OK, "{page}");
        let links: std::collections::BTreeSet<String> = href
            .captures_iter(&r.text)
            .map(|m| m[1].to_owned())
            .collect();
        assert!(links.contains("/swing"), "{page} does not link to /swing");
        if page == "/swing" {
            let missing: Vec<&str> = routes
                .iter()
                .copied()
                .filter(|x| !links.contains(*x))
                .collect();
            assert!(missing.is_empty(), "/swing does not link to {missing:?}");
        }
    }
}

#[test]
fn test_nav_entry_sits_after_reconcile() {
    let shell =
        std::fs::read_to_string(super::scenario::desk_root().join("app/templates/base.html"))
            .unwrap();
    let reconcile = shell.find("('/reconcile', 'Reconcile')").unwrap();
    let swing = shell.find("('/swing', 'Swing')").unwrap();
    let ops = shell.find("('/ops', 'Operations')").unwrap();
    assert!(reconcile < swing && swing < ops);
}

#[tokio::test]
async fn test_nav_marks_swing_current_on_its_own_page() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let c = Client::new(&sc).await;
    assert!(
        c.html("/swing")
            .await
            .contains(r#"href="/swing" aria-current="page""#)
    );
}

// =============================================================================================
// TestTheRouteReSizesThroughTheRealModule
// =============================================================================================

#[tokio::test]
async fn test_the_route_re_sizes_a_signal_line_to_the_ceiling() {
    let (mut sc, ids, c) = morning().await;
    sc.exec(
        "UPDATE sw_market_daily SET exposure_level = 0, max_open_positions = 2, \
         max_exposure_pct = 25.00",
        &[],
    )
    .await;
    // rung 0 allows two names and two are held: lift the count so the money is what binds
    sc.exec("UPDATE sw_market_daily SET max_open_positions = 3", &[])
        .await;
    let r = execute(&c, &ids.signal_plan.1, ids.trigger_line).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    let j = r.json();
    assert_eq!(j["status"], "SIMULATED", "{j}");
    assert_eq!(j["gtt"]["qty"], 1553, "{j}");
    let mut s = sc.store().await;
    let line = s.line(ids.trigger_line).await.unwrap().unwrap();
    assert_eq!((line.quantity, line.state.as_str()), (1553, "FILLED"));
    assert_eq!(line.position_value, Some(d("156542.40")));
    assert_eq!(line.risk_inr, Some(d("4659.00")), "(100.80 − 97.80) × 1553");
    assert!(
        line.note
            .unwrap()
            .contains("re-sized at confirm 1666 \u{2192} 1553")
    );
    let pos = s
        .position(j["position_id"].as_i64().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!((pos.quantity_entered, pos.quantity_open), (1553, 1553));
    let book: Decimal = s
        .open_positions()
        .await
        .unwrap()
        .iter()
        .map(|p| p.entry_avg.unwrap() * Decimal::from(p.quantity_open))
        .sum();
    assert_eq!(book, d("249932.40"));
    assert!(book <= Decimal::from(250_000));
    let html = c.html("/swing").await;
    assert!(html.contains("x1,553") || html.contains("x1553"));
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
#[allow(non_snake_case)]
async fn test_the_route_answers_EXPOSURE_FULL_as_BLOCKED_and_the_line_is_REJECTED() {
    let (mut sc, ids, c) = morning().await;
    // ₹1 lakh, ₹93,390 held
    sc.exec(
        "UPDATE sw_market_daily SET exposure_level = 0, max_open_positions = 3, \
         max_exposure_pct = 10.00",
        &[],
    )
    .await;
    let r = execute(&c, &ids.signal_plan.1, ids.trigger_line).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    let j = r.json();
    assert_eq!(j["status"], "BLOCKED");
    assert!(
        j["reason"]
            .as_str()
            .unwrap()
            .starts_with("EXPOSURE_FULL: ALPHAFLAG"),
        "{j}"
    );
    assert!(j["order"].is_null() && j["gtt"].is_null() && j["position_id"].is_null());
    let mut s = sc.store().await;
    assert_eq!(
        s.line(ids.trigger_line).await.unwrap().unwrap().state,
        "REJECTED"
    );
    assert_eq!(s.open_positions().await.unwrap().len(), 2);
    let session = s.session(today()).await.unwrap().unwrap();
    assert_eq!((session.confirms, session.fills), (2, 1));
    let html = c.html("/swing").await;
    let row = signal_row(&html, ids.trigger);
    assert!(row.contains("rejected") && forms(&row, "/swing/execute").is_empty());
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
#[allow(non_snake_case)]
async fn test_the_route_answers_TIER_FULL_at_a_full_rung() {
    let (mut sc, ids, c) = morning().await;
    sc.exec(
        "UPDATE sw_market_daily SET exposure_level = 0, max_open_positions = 2, \
         max_exposure_pct = 25.00",
        &[],
    )
    .await;
    let j = execute(&c, &ids.signal_plan.1, ids.trigger_line)
        .await
        .json();
    assert_eq!(j["status"], "BLOCKED");
    let reason = j["reason"].as_str().unwrap();
    assert!(reason.starts_with("TIER_FULL: ALPHAFLAG"), "{j}");
    assert!(reason.contains("rung 0 allows 2 positions"));
}

#[tokio::test]
#[allow(non_snake_case)]
async fn test_the_route_answers_SESSION_CAP_after_three_entries() {
    let (mut sc, ids, c) = morning().await;
    let (pk, plan_id) = ids.signal_plan.clone();
    for (instrument_id, symbol) in [(3, "GAMMALOCK"), (5, "EPSILON")] {
        sc.line(
            pk,
            &plan_id,
            LineSpec {
                quantity: 10,
                state: "FILLED",
                ..LineSpec::new("BUY_ON_TRIGGER", instrument_id, symbol)
            },
        )
        .await;
    }
    sc.line(
        pk,
        &plan_id,
        LineSpec {
            quantity: 10,
            state: "SENT",
            ..LineSpec::new("BUY_ON_TRIGGER", 4, "DELTAHELD")
        },
    )
    .await;
    let j = execute(&c, &plan_id, ids.trigger_line).await.json();
    assert_eq!(j["status"], "BLOCKED");
    assert!(
        j["reason"]
            .as_str()
            .unwrap()
            .starts_with("SESSION_CAP: ALPHAFLAG"),
        "{j}"
    );
    let mut s = sc.store().await;
    assert_eq!(
        s.line(ids.trigger_line).await.unwrap().unwrap().state,
        "REJECTED"
    );
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_a_confirm_that_fits_is_sent_as_planned_and_the_row_is_untouched() {
    let (sc, ids, c) = morning().await; // rung 2: 6 names, 75 % — the 1,666 fit whole
    let j = execute(&c, &ids.signal_plan.1, ids.trigger_line)
        .await
        .json();
    assert_eq!(j["status"], "SIMULATED");
    assert_eq!(j["gtt"]["qty"], 1666);
    let mut s = sc.store().await;
    let line = s.line(ids.trigger_line).await.unwrap().unwrap();
    assert_eq!((line.quantity, line.note), (1666, None));
}

// =============================================================================================
// TestPendingRangeOnTheDesk (the page and the route)
// =============================================================================================

#[tokio::test]
async fn test_a_pending_range_line_renders_with_no_confirm_button() {
    let mut sc = Scenario::new().await;
    let (_, _, line_id) = super::tests_store::pending(&mut sc).await;
    let c = Client::new(&sc).await;
    let html = c.html("/swing").await;
    let row = find(&format!(r#"data-line-id="{line_id}".*?</tr>"#), &html);
    assert!(row.contains("SWING PENDING EPSILON") && row.contains("no stop yet"));
    assert!(forms(&row, "/swing/execute").is_empty());
    assert!(row.contains("pending the opening range") && row.contains("not confirmable"));
}

#[tokio::test]
async fn test_the_route_refuses_a_pending_range_line_with_400_before_the_module() {
    let mut sc = Scenario::new().await;
    let (_, plan_id, line_id) = super::tests_store::pending(&mut sc).await;
    let c = Client::new(&sc).await;
    let r = execute(&c, &plan_id, line_id).await;
    assert_eq!(r.status, StatusCode::BAD_REQUEST);
    assert!(r.text.contains("PENDING_RANGE"));
    // "the execute module was never asked": no price read, no session row bumped.
    assert!(c.kite.calls().is_empty());
    let mut store = sc.store().await;
    assert!(store.session(today()).await.unwrap().is_none());
    assert_eq!(
        store.line(line_id).await.unwrap().unwrap().state,
        "PROPOSED"
    );
}

#[tokio::test]
async fn test_the_real_module_refuses_a_pending_range_line_too() {
    // The route's own refusal answers first (above); the module's is asserted on `execute_line`
    // itself, as Python's real_client reaches it only through the same route.
    let mut sc = Scenario::new().await;
    let (_, plan_id, line_id) = super::tests_store::pending(&mut sc).await;
    let c = Client::new(&sc).await;
    assert_eq!(
        execute(&c, &plan_id, line_id).await.status,
        StatusCode::BAD_REQUEST
    );
    let mut store = sc.store().await;
    let gw = super::swing_gateway(&c.state).await.unwrap();
    let settings = c.state.settings_snapshot();
    let timer = super::TokioTimer;
    let cx = super::ExecContext {
        settings: &settings,
        orders: None,
        timer: &timer,
    };
    let e = super::execute_line(
        &mut store,
        gw.as_ref(),
        &cx,
        &plan_id,
        line_id,
        "true",
        now(),
        None,
    )
    .await
    .unwrap_err();
    match e {
        super::ExecError::Http(h) => assert_eq!(h.status, 400),
        other => panic!("{other:?}"),
    }
    assert_eq!(
        store.line(line_id).await.unwrap().unwrap().state,
        "PROPOSED"
    );
    assert_eq!(c.broker_writes(), 0);
}

// =============================================================================================
// TestTheSweepRoutes
// =============================================================================================

#[tokio::test]
async fn test_the_cutoff_route_frees_slots_and_needs_confirm() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let (pk, plan_id) = sc.plan("MORNING", ist_at(2026, 9, 2, 9, 12)).await;
    let line_id = sc
        .line(
            pk,
            &plan_id,
            LineSpec {
                quantity: 0,
                trigger: Some("112.50"),
                stop: None,
                setup: Some("EP"),
                ..LineSpec::new("PENDING_RANGE", 5, "EPSILON")
            },
        )
        .await;
    let c = Client::new(&sc).await;
    assert_eq!(
        c.post("/swing/cutoff", &[("confirm", "no")]).await.status,
        StatusCode::BAD_REQUEST
    );
    let r = c.post("/swing/cutoff", &[("confirm", "true")]).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    assert_eq!(
        r.json(),
        serde_json::json!({"reconciled": 0, "cancelled": 0, "cancel_failed": 0,
                           "slots_freed": 1, "outcomes": []})
    );
    let mut store = sc.store().await;
    assert_eq!(store.line(line_id).await.unwrap().unwrap().state, "EXPIRED");
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_the_reconcile_route_applies_the_brokers_fill_through_the_handler() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    store
        .set_line(ids.trigger_line, "SENT", Some("ORD-9"), None)
        .await
        .unwrap();
    // `Orders.order_status` → OrderReport("OPEN", 40, 100.90): the replay Kite's order history.
    let c = Client::with_orders(
        &sc,
        &serde_json::json!({"ORD-9": [
            {"order_id": "ORD-9", "status": "OPEN", "filled_quantity": 40, "average_price": 100.9}
        ]}),
    )
    .await;
    assert_eq!(
        c.post("/swing/reconcile", &[("confirm", "no")])
            .await
            .status,
        StatusCode::BAD_REQUEST
    );
    let r = c.post("/swing/reconcile", &[("confirm", "true")]).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    let reconciled = r.json()["reconciled"].as_array().unwrap().clone();
    assert_eq!(reconciled.len(), 1);
    let applied = &reconciled[0];
    // Under the desk's dry-run gates the fill is booked simulated=true; the line stays SENT with
    // the position on it, as a live partial would.
    assert_eq!(applied["status"], "SIMULATED");
    assert_eq!(applied["filled_quantity"], 40);
    let pos = store
        .position(applied["position_id"].as_i64().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(pos.quantity_open, 40);
    assert!(pos.gtt_id.as_deref().unwrap().starts_with("DRY-"));
    let line = store.line(ids.trigger_line).await.unwrap().unwrap();
    assert_eq!(
        (line.state.as_str(), line.position_id),
        ("SENT", Some(pos.id))
    );
    let html = c.html("/swing").await;
    assert!(html.contains("not yet complete") && html.contains("ORD-9"));
    assert!(!forms(&html, "/swing/reconcile").is_empty());
    assert!(!forms(&html, "/swing/cutoff").is_empty());
    assert_eq!(c.broker_writes(), 0);
}

#[tokio::test]
async fn test_the_sweeps_are_covered_by_websec() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let c = Client::new(&sc).await;
    for path in ["/swing/cutoff", "/swing/reconcile"] {
        let r = c
            .post_from(path, &[("confirm", "true")], Some("http://evil.example"))
            .await;
        assert_eq!(r.status, StatusCode::FORBIDDEN, "{path}");
    }
}

// =============================================================================================
// TestFirstLiveHeaderAndFocus (the page)
// =============================================================================================

#[tokio::test]
async fn test_the_page_shows_the_start_small_banner() {
    let mut sc = Scenario::new().await;
    sc.config_with("1000000", "0.500", 5, true, true).await;
    let c = Client::new(&sc).await;
    let html = c.html("/swing").await;
    assert!(html.contains(r#"id="swFirstLive""#));
    assert!(html.contains("first live sessions: 5 left"));
}

// =============================================================================================
// TestScanNowStatusBar
// =============================================================================================

async fn market_scan_row(sc: &mut Scenario, provisional: bool, scanned_at: Option<&str>) {
    let mut detail =
        serde_json::json!({"funnel": {"liquid": 41, "candidates": {"FLAG": 2, "EP": 0}}});
    if let Some(at) = scanned_at {
        detail["scan"] =
            serde_json::json!({"run_id": 1, "scanned_at": at, "provisional": provisional});
    }
    sc.exec(
        "INSERT INTO sw_market_daily(user_id, date, constituent_count, gate, exposure_level, \
         max_open_positions, max_exposure_pct, new_entries_allowed, provisional, detail) \
         VALUES (?, ?, 41, 'GREEN', 2, 6, '75.00', 1, ?, ?)",
        &[
            Param::Int(USER),
            Param::Text(today().format("%Y-%m-%d").to_string()),
            Param::Bool(provisional),
            Param::Text(py_json(&detail)),
        ],
    )
    .await;
}

/// `json.dumps(obj)` (Python's separators) for the fixture rows.
fn py_json(v: &serde_json::Value) -> String {
    let p = crate::wire::PyValue::from_json(v.to_string().as_bytes()).unwrap();
    baskfy_providers::kite::py_json_dumps(&p).unwrap()
}

async fn scan_run_row(
    sc: &mut Scenario,
    status: &str,
    requested_at: chrono::DateTime<chrono::FixedOffset>,
    finished_at: Option<chrono::DateTime<chrono::FixedOffset>>,
    provisional: bool,
    error: Option<&str>,
    funnel: Option<serde_json::Value>,
) -> i64 {
    let mut detail = serde_json::json!({"source": "desk"});
    if let Some(f) = funnel {
        detail["funnel"] = f;
    }
    let rows = sc
        .exec(
            "INSERT INTO sw_scan_run(user_id, requested_at, finished_at, session_date, provisional, \
             status, detail, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            &[
                Param::Int(USER),
                Param::Text(iso(requested_at)),
                finished_at.map_or(Param::Null, |t| Param::Text(iso(t))),
                Param::Text(today().format("%Y-%m-%d").to_string()),
                Param::Bool(provisional),
                Param::Text(status.to_owned()),
                Param::Text(py_json(&detail)),
                error.map_or(Param::Null, |e| Param::Text(e.to_owned())),
            ],
        )
        .await;
    int_of(&rows[0], "id")
}

#[tokio::test]
async fn test_a_provisional_day_says_so_and_from_what() {
    let mut sc = Scenario::new().await;
    sc.config_with("1000000", "0.500", 5, false, true).await;
    market_scan_row(&mut sc, true, Some("2026-09-02T13:42:00+05:30")).await;
    let c = Client::new(&sc).await;
    let html = c.html("/swing").await;
    let bar = between(&html, r#"id="swStatus""#, r#"<section id="triggers">"#);
    assert!(bar.contains("provisional \u{2014} scanned 13:42 IST from live quotes"));
    assert!(bar.contains(r#"id="swScanLabel""#));
}

#[tokio::test]
async fn test_a_nightly_day_says_nightly_and_a_re_scan_says_re_scanned() {
    let mut sc = Scenario::new().await;
    sc.config_with("1000000", "0.500", 5, false, true).await;
    market_scan_row(&mut sc, false, None).await;
    let c = Client::new(&sc).await;
    assert!(c.html("/swing").await.contains("nightly (2026-09-02)"));
    sc.exec("DELETE FROM sw_market_daily", &[]).await;
    market_scan_row(&mut sc, false, Some("2026-09-02T18:02:00+05:30")).await;
    assert!(
        c.html("/swing")
            .await
            .contains("re-scanned 18:02 IST from published bars")
    );
}

#[tokio::test]
async fn test_the_last_run_is_beside_the_button_done_failed_or_in_flight() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let done = now() - chrono::Duration::minutes(30);
    scan_run_row(
        &mut sc,
        "DONE",
        done - chrono::Duration::minutes(1),
        Some(done),
        true,
        None,
        Some(serde_json::json!({"liquid": 41, "candidates": {"FLAG": 2, "EP": 1}})),
    )
    .await;
    let c = Client::new(&sc).await;
    let html = c.html("/swing").await;
    assert!(html.contains("last scan 09:10 IST from live quotes \u{b7} 41 liquid, 3 flagged"));
    assert!(html.contains(">Scan now</button>"));
    assert!(!html.contains("Scanning\u{2026}</button>"));

    scan_run_row(
        &mut sc,
        "FAILED",
        now() - chrono::Duration::minutes(5),
        Some(now() - chrono::Duration::minutes(5)),
        false,
        Some("ScanNotRunnable: no Kite quote source"),
        None,
    )
    .await;
    assert!(
        c.html("/swing")
            .await
            .contains("last scan failed: ScanNotRunnable: no Kite quote source")
    );

    scan_run_row(
        &mut sc,
        "RUNNING",
        now() - chrono::Duration::minutes(2),
        None,
        false,
        None,
        None,
    )
    .await;
    let html = c.html("/swing").await;
    assert!(html.contains("scanning\u{2026}") && html.contains("Scanning\u{2026}</button>"));
    let form = forms(&html, "/swing/scan").remove(0);
    assert!(form.contains("disabled") && form.contains(r#"data-in-flight="true""#));
}

#[tokio::test]
async fn test_the_view_json_carries_the_scan_and_the_fingerprint_moves_with_it() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let c = Client::new(&sc).await;
    let before = c.get("/swing/data").await.json();
    assert_eq!(before["status"]["scan"]["in_flight"], false);
    scan_run_row(
        &mut sc,
        "RUNNING",
        now() - chrono::Duration::minutes(1),
        None,
        false,
        None,
        None,
    )
    .await;
    let during = c.get("/swing/data").await.json();
    assert_eq!(during["status"]["scan"]["in_flight"], true);
    assert_ne!(during["fingerprint"], before["fingerprint"]);
    sc.exec("UPDATE sw_scan_run SET status = 'DONE'", &[]).await;
    let after = c.get("/swing/data").await.json();
    assert_eq!(after["status"]["scan"]["in_flight"], false);
    assert_ne!(after["fingerprint"], during["fingerprint"]);
}

// =============================================================================================
// TestScanNowRoute
// =============================================================================================

#[tokio::test]
async fn test_a_press_writes_one_queued_row_for_the_sweep_and_answers_its_id() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let c = Client::new(&sc).await;
    let r = c.post("/swing/scan", &[]).await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    let body = r.json();
    let rows = sc.exec("SELECT * FROM sw_scan_run", &[]).await;
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    let text = |k: &str| crate::lanes::swing::values::text(row.get(k));
    assert_eq!(
        body,
        serde_json::json!({"run_id": int_of(row, "id"), "status": "QUEUED",
                           "requested_at": iso(now())})
    );
    assert_eq!(int_of(row, "user_id"), USER);
    assert_eq!(text("status"), "QUEUED");
    assert!(
        row.get("task_id")
            .is_none_or(|v| *v == crate::storage::DbValue::Null)
    );
    assert!(text("detail").contains(r#""source": "desk""#));
    assert_eq!(
        crate::lanes::swing::values::stamp(row.get("requested_at")),
        Some(now())
    );
}

#[tokio::test]
async fn test_a_second_press_while_one_is_in_flight_is_a_409() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let c = Client::new(&sc).await;
    let first = c.post("/swing/scan", &[]).await.json()["run_id"]
        .as_i64()
        .unwrap();
    let second = c.post("/swing/scan", &[]).await;
    assert_eq!(second.status, StatusCode::CONFLICT);
    assert!(
        second.json()["detail"]
            .as_str()
            .unwrap()
            .contains(&format!("Scan {first} is queued"))
    );
    let n = sc.exec("SELECT count(*) AS n FROM sw_scan_run", &[]).await;
    assert_eq!(int_of(&n[0], "n"), 1);
}

#[tokio::test]
async fn test_a_press_inside_a_minute_of_the_last_is_a_429_with_retry_after() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    scan_run_row(
        &mut sc,
        "DONE",
        now() - chrono::Duration::seconds(20),
        Some(now() - chrono::Duration::seconds(10)),
        false,
        None,
        None,
    )
    .await;
    let c = Client::new(&sc).await;
    let r = c.post("/swing/scan", &[]).await;
    assert_eq!(r.status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(r.headers["Retry-After"], "40");
    assert!(
        r.json()["detail"]
            .as_str()
            .unwrap()
            .contains("one a minute is the limit")
    );
}

#[tokio::test]
async fn test_a_stale_run_no_longer_blocks_and_an_old_one_no_longer_throttles() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    scan_run_row(
        &mut sc,
        "RUNNING",
        now() - chrono::Duration::minutes(20),
        None,
        false,
        None,
        None,
    )
    .await;
    let c = Client::new(&sc).await;
    assert_eq!(c.post("/swing/scan", &[]).await.status, StatusCode::OK);
}

#[tokio::test]
async fn test_the_thresholds_are_config_not_literals() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    scan_run_row(
        &mut sc,
        "DONE",
        now() - chrono::Duration::seconds(90),
        Some(now() - chrono::Duration::seconds(80)),
        false,
        None,
        None,
    )
    .await;
    let c = Client::new(&sc).await;
    c.settings(|s| s.swing_scan_min_interval_seconds = 120);
    assert_eq!(
        c.post("/swing/scan", &[]).await.status,
        StatusCode::TOO_MANY_REQUESTS
    );
    c.settings(|s| s.swing_scan_min_interval_seconds = 60);
    assert_eq!(c.post("/swing/scan", &[]).await.status, StatusCode::OK);
}

#[tokio::test]
async fn test_the_scan_route_is_covered_by_websec() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let c = Client::new(&sc).await;
    let r = c
        .post_from("/swing/scan", &[], Some("http://evil.example"))
        .await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    assert!(sc.exec("SELECT id FROM sw_scan_run", &[]).await.is_empty());
}

#[test]
fn test_the_scan_route_names_no_order_path() {
    let source = include_str!("../desk.rs");
    let start = source.find("async fn swing_scan(").unwrap();
    let end = source.find("async fn swing_reconcile(").unwrap();
    let src = source[start..end]
        .to_lowercase()
        .replace("swing_gateway", "");
    for word in ["gateway", "execute_line", "place", "gtt", "kc."] {
        assert!(!src.contains(word), "{word}");
    }
    assert!(src.contains("request_scan"));
}
