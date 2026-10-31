//! `tests/test_stops_route.py`, ported over the replay Kite (DRY_RUN on, no flag flipped): the
//! review places nothing, the gates refuse, arming is single use and reports every failure.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic, missing_docs)]

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{Request, Response, StatusCode, header};
use baskfy_desk::app::{AppState, DeskPaths, StateParts};
use baskfy_desk::clock::{Clock, FixedClock};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::analytics::web::unquote_plus;
use baskfy_desk::lanes::exec_core::gtt::to_tick;
use baskfy_desk::lanes::exec_core::kite_client::ClientError;
use baskfy_desk::lanes::weekly::stops::{self, STOP_OK, StopArmer, arm_with, stop_plans};
use baskfy_desk::storage::{Db, DbConfig};
use baskfy_desk::wire::{PyDict, PyValue, dict};
use baskfy_desk::{Settings, build_app, build_state};
use chrono::{FixedOffset, TimeZone};
use serde_json::{Value, json};
use tower::ServiceExt as _;

fn python_desk() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer")
}

fn holding(symbol: &str, qty: i64, last: f64) -> Value {
    json!({"tradingsymbol": symbol, "exchange": "NSE", "quantity": qty, "t1_quantity": 0,
           "collateral_quantity": 0, "average_price": last, "last_price": last})
}

fn aaa() -> Vec<Value> {
    vec![holding("AAA", 100, 1000.0)]
}

fn gtt(symbol: &str, trigger: f64, qty: i64, id: i64) -> Value {
    json!({"id": id, "status": "active",
           "condition": {"tradingsymbol": symbol, "trigger_values": [trigger]},
           "orders": [{"quantity": qty}]})
}

struct Desk {
    _dir: tempfile::TempDir,
    state: AppState,
    app: Router,
    kite: Arc<ReplayKite>,
    clock: Arc<FixedClock>,
}

async fn desk_with(holdings: Vec<Value>, gtts: Vec<Value>, authed: bool) -> Desk {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    std::fs::create_dir_all(root.join("data/outputs")).unwrap();
    let mut settings = Settings::defaults().unwrap();
    assert!(settings.dry_run, "DRY_RUN must default true");
    settings.kite_api_key = "replay-test-key".to_owned();
    settings.db_backend = "sqlite".to_owned();
    settings.db_path = root.join("desk.db").display().to_string();
    settings.token_file = root.join("no-such-token.json").display().to_string();
    let fixture = json!({
        "session": {"authed": authed},
        "routes": {
            "user.profile": {"data": {"user_id": "REPLAY"}},
            "portfolio.holdings": {"data": holdings},
            "portfolio.positions": {"data": {"net": [], "day": []}},
            "gtt": {"data": gtts},
        },
        "quotes": {}
    });
    std::fs::write(root.join("kite.json"), fixture.to_string()).unwrap();
    let kite = Arc::new(ReplayKite::load(root, None, &settings.kite_api_key).unwrap());
    let db = Db::open(DbConfig::from_settings(&settings, None))
        .await
        .unwrap();
    let ist = FixedOffset::east_opt(19800).unwrap();
    let clock = Arc::new(FixedClock::new(
        ist.with_ymd_and_hms(2026, 10, 30, 11, 0, 0).unwrap(),
    ));
    let desk = python_desk();
    let state = build_state(StateParts {
        settings,
        clock: clock.clone(),
        ids: Arc::new(HarnessIds::new("weekly-stops")),
        db,
        kite: kite.clone(),
        paths: DeskPaths {
            root: root.to_path_buf(),
            templates: desk.join("app").join("templates"),
            static_dir: desk.join("app").join("static"),
        },
        harness: None,
    });
    let app = build_app(state.clone());
    Desk {
        _dir: dir,
        state,
        app,
        kite,
        clock,
    }
}

async fn desk() -> Desk {
    desk_with(aaa(), Vec::new(), true).await
}

async fn send(app: &Router, req: Request<Body>) -> (StatusCode, Option<String>, String) {
    let r = app.clone().oneshot(req).await.unwrap();
    read(r).await
}

async fn read(r: Response<Body>) -> (StatusCode, Option<String>, String) {
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

async fn get_stops(d: &Desk) -> (StatusCode, String) {
    let req = Request::builder()
        .uri("/stops")
        .header(header::HOST, "127.0.0.1")
        .body(Body::empty())
        .unwrap();
    let (status, _, body) = send(&d.app, req).await;
    (status, body)
}

async fn post_arm(d: &Desk, plan_id: &str, confirm: &str) -> (StatusCode, Option<String>, String) {
    let req = Request::builder()
        .method("POST")
        .uri("/stops/arm")
        .header(header::HOST, "127.0.0.1")
        .header(header::ORIGIN, "http://127.0.0.1")
        .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
        .body(Body::from(format!("plan_id={plan_id}&confirm={confirm}")))
        .unwrap();
    send(&d.app, req).await
}

async fn make_plan(d: &Desk) -> String {
    let (status, body) = get_stops(d).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let ids = stop_plans(&d.state).ids();
    assert_eq!(ids.len(), 1, "{body}");
    ids[0].clone()
}

fn rows(d: &Desk, pid: &str) -> Vec<PyDict> {
    let plan = stop_plans(&d.state).get(pid).unwrap();
    match plan.body.get("rows") {
        Some(PyValue::List(l)) => l
            .iter()
            .map(|r| match r {
                PyValue::Map(m) => m.clone(),
                o => panic!("{o:?}"),
            })
            .collect(),
        o => panic!("{o:?}"),
    }
}

fn gtt_writes(d: &Desk) -> Vec<String> {
    d.kite
        .write_calls()
        .into_iter()
        .map(|c| c.route)
        .filter(|r| r.starts_with("gtt"))
        .collect()
}

enum Mode {
    Simulate,
    Raise(&'static str, &'static str, &'static str),
    Reject(&'static str),
}

struct Recorder {
    mode: Mode,
    calls: Mutex<Vec<String>>,
    stops: Mutex<Vec<(String, i64, f64)>>,
}

impl Recorder {
    fn new(mode: Mode) -> Self {
        Self {
            mode,
            calls: Mutex::new(Vec::new()),
            stops: Mutex::new(Vec::new()),
        }
    }

    fn stops(&self) -> Vec<(String, i64, f64)> {
        self.stops.lock().unwrap().clone()
    }

    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }
}

#[async_trait]
impl StopArmer for Recorder {
    async fn place_gtt_stop(
        &self,
        symbol: &str,
        qty: i64,
        trigger: f64,
        _last_price: f64,
    ) -> Result<PyDict, ClientError> {
        self.calls.lock().unwrap().push(format!("place:{symbol}"));
        match &self.mode {
            Mode::Raise(sym, kind, msg) if *sym == symbol => Err(ClientError {
                python_type: (*kind).to_owned(),
                message: (*msg).to_owned(),
            }),
            Mode::Reject(msg) => Ok(dict([
                ("symbol", PyValue::Str(symbol.to_owned())),
                ("status", PyValue::Str("GTT_ERROR".to_owned())),
                ("error", PyValue::Str((*msg).to_owned())),
            ])),
            _ => {
                self.stops
                    .lock()
                    .unwrap()
                    .push((symbol.to_owned(), qty, trigger));
                Ok(dict([
                    ("symbol", PyValue::Str(symbol.to_owned())),
                    ("status", PyValue::Str("DRY_RUN_GTT".to_owned())),
                    ("trigger", PyValue::Float(trigger)),
                ]))
            }
        }
    }

    async fn delete_gtt(&self, gtt_id: i64, symbol: &str) -> Result<PyDict, ClientError> {
        self.calls
            .lock()
            .unwrap()
            .push(format!("delete:{symbol}:{gtt_id}"));
        Ok(dict([
            ("symbol", PyValue::Str(symbol.to_owned())),
            ("gtt_id", PyValue::Int(gtt_id)),
            ("status", PyValue::Str("DRY_RUN_GTT_DELETE".to_owned())),
        ]))
    }
}

async fn arm(d: &Desk, pid: &str, armer: &Recorder) -> (StatusCode, Option<String>, String) {
    read(arm_with(&d.state, pid, "true", armer).await).await
}

// =============================================================================================
// reviewing places nothing
// =============================================================================================

#[tokio::test]
async fn the_review_page_arms_nothing() {
    let d = desk().await;
    get_stops(&d).await;
    assert!(gtt_writes(&d).is_empty());
}

#[tokio::test]
async fn the_review_page_issues_a_plan() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    assert_eq!(pid.len(), 12);
    assert_eq!(rows(&d, &pid).len(), 1);
}

#[tokio::test]
async fn an_expired_session_explains_itself_instead_of_erroring() {
    let d = desk_with(aaa(), Vec::new(), false).await;
    let (status, body) = get_stops(&d).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.to_lowercase().contains("session expired"), "{body}");
    assert!(stop_plans(&d.state).ids().is_empty());
}

#[tokio::test]
async fn nothing_to_arm_is_stated_plainly() {
    let d = desk_with(aaa(), vec![gtt("AAA", 900.0, 100, 1)], true).await;
    let (status, body) = get_stops(&d).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("already carries a stop"), "{body}");
}

// =============================================================================================
// the gates
// =============================================================================================

#[tokio::test]
async fn arming_without_confirmation_is_refused() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    let (status, _, body) = post_arm(&d, &pid, "false").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body.contains("Arming stops requires explicit confirmation."));
}

#[tokio::test]
async fn arming_an_unknown_plan_is_refused() {
    let d = desk().await;
    let (status, _, body) = post_arm(&d, "nope", "true").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(body.contains("Unknown or expired plan"), "{body}");
}

#[tokio::test]
async fn a_stale_plan_is_refused_because_triggers_are_priced_live() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    stop_plans(&d.state).set_created_at(&pid, d.clock.time() - 3600.0);
    let (status, _, _) = post_arm(&d, &pid, "true").await;
    assert_eq!(status, StatusCode::GONE);
}

#[tokio::test]
async fn missing_form_fields_are_fastapis_422() {
    let d = desk().await;
    let req = Request::builder()
        .method("POST")
        .uri("/stops/arm")
        .header(header::HOST, "127.0.0.1")
        .header(header::ORIGIN, "http://127.0.0.1")
        .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
        .body(Body::from("plan_id=x"))
        .unwrap();
    let (status, _, body) = send(&d.app, req).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(
        body.contains("confirm") && !body.contains("plan_id"),
        "{body}"
    );
}

#[tokio::test]
async fn a_refused_attempt_places_nothing() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    let rec = Recorder::new(Mode::Simulate);
    let r = read(arm_with(&d.state, &pid, "false", &rec).await).await;
    assert_eq!(r.0, StatusCode::BAD_REQUEST);
    assert!(rec.calls().is_empty());
    assert!(stop_plans(&d.state).get(&pid).is_some());
}

// =============================================================================================
// arming
// =============================================================================================

#[tokio::test]
async fn confirming_places_one_stop_per_row() {
    let d = desk_with(
        vec![holding("AAA", 100, 1000.0), holding("BBB", 40, 500.0)],
        Vec::new(),
        true,
    )
    .await;
    let pid = make_plan(&d).await;
    let planned = rows(&d, &pid);
    let rec = Recorder::new(Mode::Simulate);
    let (status, _, _) = arm(&d, &pid, &rec).await;
    assert_eq!(status, StatusCode::SEE_OTHER);
    let stops = rec.stops();
    assert_eq!(stops.len(), planned.len());
    for (s, r) in stops.iter().zip(&planned) {
        assert_eq!(PyValue::Str(s.0.clone()), r.get("symbol").cloned().unwrap());
        assert_eq!(PyValue::Int(s.1), r.get("qty").cloned().unwrap());
        assert_eq!(PyValue::Float(s.2), r.get("trigger").cloned().unwrap());
    }
}

#[tokio::test]
async fn every_trigger_sits_inside_the_vol_scaled_band() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    for r in rows(&d, &pid) {
        let Some(PyValue::Float(drop)) = r.get("drop_pct") else {
            panic!("{r:?}")
        };
        assert!((8.0..=12.0).contains(drop), "{drop}");
    }
}

#[tokio::test]
async fn the_http_arm_in_dry_run_reaches_no_broker_write() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    let (status, location, _) = post_arm(&d, &pid, "true").await;
    assert_eq!(status, StatusCode::SEE_OTHER);
    assert_eq!(
        unquote_plus(&location.unwrap()),
        "/stops?armed=1 of 1 stops simulated"
    );
    assert!(d.kite.write_calls().is_empty());
    let log = d
        .state
        .paths
        .resolve(&format!("data/outputs/stops_{pid}.json"));
    let logged: Value = serde_json::from_slice(&std::fs::read(log).unwrap()).unwrap();
    assert_eq!(logged["results"][0]["status"], "DRY_RUN_GTT");
    assert_eq!(logged["results"][0]["qty"], 100);
}

#[tokio::test]
async fn a_plan_is_single_use() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    let rec = Recorder::new(Mode::Simulate);
    arm(&d, &pid, &rec).await;
    let first = rec.stops().len();
    let (status, _, _) = arm(&d, &pid, &rec).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(rec.stops().len(), first);
}

#[tokio::test]
async fn one_rejection_does_not_abandon_the_rest() {
    let d = desk_with(
        vec![holding("AAA", 100, 1000.0), holding("BBB", 100, 500.0)],
        Vec::new(),
        true,
    )
    .await;
    let pid = make_plan(&d).await;
    let rec = Recorder::new(Mode::Raise("AAA", "RuntimeError", "exchange rejected"));
    let (_, location, _) = arm(&d, &pid, &rec).await;
    assert_eq!(rec.calls().len(), 2);
    let stops = rec.stops();
    assert_eq!(stops.len(), 1);
    assert_eq!((stops[0].0.as_str(), stops[0].1), ("BBB", 100));
    let msg = unquote_plus(&location.unwrap());
    assert!(
        msg.contains("failed") && msg.contains("AAA (exchange rejected)"),
        "{msg}"
    );
}

#[tokio::test]
async fn an_untouchable_refusal_is_blocked_and_reported() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    let rec = Recorder::new(Mode::Raise(
        "AAA",
        "UntouchableInstrumentError",
        "AAA is untouchable",
    ));
    let (_, location, _) = arm(&d, &pid, &rec).await;
    let msg = unquote_plus(&location.unwrap());
    assert!(msg.contains("0 of 1") && msg.contains("1 failed"), "{msg}");
    let log = d
        .state
        .paths
        .resolve(&format!("data/outputs/stops_{pid}.json"));
    let logged: Value = serde_json::from_slice(&std::fs::read(log).unwrap()).unwrap();
    assert_eq!(logged["results"][0]["status"], "BLOCKED");
}

#[tokio::test]
async fn the_outcome_is_reported_back() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    let (_, location, _) = post_arm(&d, &pid, "true").await;
    assert!(location.unwrap().contains("armed="));
}

#[tokio::test]
async fn dry_run_says_simulated_rather_than_armed() {
    let d = desk().await;
    assert!(d.state.settings_snapshot().dry_run);
    let pid = make_plan(&d).await;
    let (_, location, _) = post_arm(&d, &pid, "true").await;
    assert!(location.unwrap().contains("simulated"));
}

#[tokio::test]
async fn an_over_covered_symbol_is_cancelled_before_it_is_re_armed() {
    let d = desk_with(aaa(), vec![gtt("AAA", 900.0, 250, 77)], true).await;
    let pid = make_plan(&d).await;
    let rec = Recorder::new(Mode::Simulate);
    let (_, location, _) = arm(&d, &pid, &rec).await;
    assert_eq!(rec.calls(), vec!["delete:AAA:77", "place:AAA"]);
    assert_eq!(rec.stops()[0].1, 100);
    let msg = unquote_plus(&location.unwrap());
    assert!(
        msg.starts_with("/stops?armed=1 of 1 wrong triggers cancelled \u{b7} 1 of 1 stops"),
        "{msg}"
    );
}

// =============================================================================================
// both bugs the first live arming run exposed
// =============================================================================================

#[test]
fn the_success_status_matches_what_kite_client_actually_returns() {
    let src = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes/exec_core/kite_client.rs"),
    )
    .unwrap();
    let returned: Vec<&str> = ["GTT_PLACED", "DRY_RUN_GTT"]
        .into_iter()
        .filter(|s| src.contains(&format!("(\"status\", s(\"{s}\"))")))
        .collect();
    assert!(
        !returned.is_empty(),
        "kite_client no longer returns a recognisable GTT status"
    );
    for s in returned {
        assert!(STOP_OK.contains(&s), "{s} would be reported as a failure");
    }
}

#[tokio::test]
async fn a_real_failure_is_still_reported_with_its_reason() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    let rec = Recorder::new(Mode::Reject(
        "Trigger price should be a multiple of tick size 1.00.",
    ));
    let (_, location, _) = arm(&d, &pid, &rec).await;
    let msg = unquote_plus(&location.unwrap());
    assert!(msg.contains("0 of 1"), "{msg}");
    assert!(
        msg.contains("1 failed") && msg.contains("tick size"),
        "{msg}"
    );
}

#[test]
fn prices_snap_to_the_instruments_tick() {
    for (price, tick, expected) in [
        (10528.9, 1.0, 10529.0),
        (1291.13, 0.1, 1291.1),
        (198.567, 0.01, 198.57),
        (447.6, 0.05, 447.60),
    ] {
        assert!(
            (to_tick(price, tick) - expected).abs() < 1e-9,
            "{price} {tick}"
        );
    }
}

#[test]
fn a_snapped_price_is_always_a_whole_number_of_ticks() {
    for tick in [0.01, 0.05, 0.1, 1.0] {
        for price in [10528.9, 1291.13, 198.567, 47.111] {
            let snapped = to_tick(price, tick);
            assert!(((snapped / tick).round() * tick - snapped).abs() < 1e-6);
        }
    }
}

#[test]
fn an_unknown_tick_does_not_crash_the_arming() {
    assert!((to_tick(100.123, 0.0) - 100.12).abs() < 1e-12);
}

// =============================================================================================
// cancels must be visible on the page that confirms them
// =============================================================================================

fn template() -> String {
    std::fs::read_to_string(python_desk().join("app/templates/stops.html")).unwrap()
}

#[test]
fn the_page_shows_the_triggers_it_will_cancel() {
    let src = template();
    assert!(src.contains("plan.cancels"));
    assert!(src.contains("Triggers to cancel"));
    assert!(src.contains("short delivery"));
}

#[test]
fn the_page_no_longer_claims_over_coverage_is_left_alone() {
    let src = template().split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(src.contains("Over-coverage is different, and is cancelled below"));
}

#[test]
fn the_confirm_step_states_the_cancel_count() {
    assert!(template().contains("will be cancelled first"));
}

#[tokio::test]
async fn the_rendered_page_lists_a_cancel_it_will_make() {
    let d = desk_with(aaa(), vec![gtt("AAA", 900.0, 250, 77)], true).await;
    let (_, body) = get_stops(&d).await;
    assert!(
        body.contains("Triggers to cancel") && body.contains("77"),
        "{body}"
    );
    let _ = stops::DELETE_OK;
}

#[tokio::test]
async fn a_cross_site_arm_is_refused_before_any_gate() {
    let d = desk().await;
    let pid = make_plan(&d).await;
    let req = Request::builder()
        .method("POST")
        .uri("/stops/arm")
        .header(header::HOST, "127.0.0.1")
        .header(header::ORIGIN, "https://evil.example")
        .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
        .body(Body::from(format!("plan_id={pid}&confirm=true")))
        .unwrap();
    let (status, _, _) = send(&d.app, req).await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(stop_plans(&d.state).get(&pid).is_some());
}
