//! The volume-breakout desk over HTTP and over its own source, ported from
//! `tests/test_vbt_rescan_desk.py::TestTheScanRoutesTheContractFixed`,
//! `tests/test_vbt_scan_safety.py` (all four classes), `tests/test_vbt_safety.py`
//! (`TestThereIsNoAutoExecute`, `TestOnlyAConfirmedRequestCanProduceAnOrder`) and
//! `tests/test_vbt_desk.py::TestTheLaw`'s route-shape tests.
//!
//! **The app.** The real router (`build_app`) over a SQLite file built from `test_vbt_desk.py`'s
//! DDL, a fixed clock at `NOW`, the replay Kite, and — for the scan-safety suite — a gateway that
//! **explodes on any use at all** with both switches set the dangerous way (`DRY_RUN=false`,
//! `BASKFY_VBT_EXECUTION_ENABLED=true`), so nothing but the shape of the code stands between a
//! scan and an order.
//!
//! **The source scans** run over the Rust lane (`src/lanes/vbt.rs`, `vbt/`) where Python's run
//! over `app/vbt_desk.py`, and over the same Python and TypeScript trees for the auto-execute
//! flag, so a flag added on either side fails here.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use baskfy_desk::app::{AppState, DeskPaths, StateParts};
use baskfy_desk::clock::FixedClock;
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
    SleeveGatewaySpec,
};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::storage::{Conn, Db, DbConfig, DbValue, Param};
use baskfy_desk::wire::PyDict;
use baskfy_desk::{Settings, build_app, build_state};
use chrono::{DateTime, Duration, FixedOffset, TimeZone};
use tower::ServiceExt as _;

fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).unwrap()
}

/// `NOW`.
fn now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 9, 21, 20, 0).unwrap()
}

fn txt(s: &str) -> Param {
    Param::Text(s.to_owned())
}

fn desk_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer")
}

fn monorepo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

fn lane_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes")
}

/// The DDL of `tests/test_vbt_desk.py`, read from the store suite's copy so there is one.
fn ddl() -> String {
    let text =
        std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/vbt_store.rs"))
            .unwrap();
    let start = text.find("pub const DDL: &str = \"").unwrap() + "pub const DDL: &str = \"".len();
    let end = start + text[start..].find("\";").unwrap();
    text[start..end].to_owned()
}

// =============================================================================================
// The client
// =============================================================================================

/// `ExplodingGateway`: any use at all is a failure — deriving the sleeve's gateway included.
#[derive(Debug)]
struct ExplodingGateway;

#[async_trait::async_trait]
impl Gateway for ExplodingGateway {
    async fn place(&self, _o: PlaceOrder) -> Result<PyDict, GatewayError> {
        panic!("a scan route reached the gateway: .place")
    }
    async fn place_gtt_stop(&self, _s: GttStop) -> Result<PyDict, GatewayError> {
        panic!("a scan route reached the gateway: .place_gtt_stop")
    }
    async fn delete_gtt(&self, _r: DeleteGtt) -> Result<PyDict, GatewayError> {
        panic!("a scan route reached the gateway: .delete_gtt")
    }
    async fn modify_gtt_quantity(&self, _r: ModifyGtt) -> Result<PyDict, GatewayError> {
        panic!("a scan route reached the gateway: .modify_gtt_quantity")
    }
    async fn cancel_order(&self, _r: CancelOrder) -> Result<PyDict, GatewayError> {
        panic!("a scan route reached the gateway: .cancel_order")
    }
    fn derive_sleeve(&self, _spec: SleeveGatewaySpec) -> Option<Arc<dyn Gateway>> {
        panic!("a scan route reached the gateway: .derive_sleeve")
    }
    fn as_any(&self) -> Option<&(dyn std::any::Any + Send + Sync)> {
        panic!("a scan route reached the gateway: .as_any")
    }
}

struct Client {
    app: Router,
    kite: Arc<ReplayKite>,
    db_path: PathBuf,
    _dir: tempfile::TempDir,
}

struct Reply {
    status: StatusCode,
    headers: axum::http::HeaderMap,
    text: String,
}

impl Reply {
    fn json(&self) -> serde_json::Value {
        serde_json::from_str(&self.text).unwrap()
    }
}

impl Client {
    /// `client`: the desk with the scenario's SQLite file. `live` is `live_desk_client`: both
    /// switches set the dangerous way and the landmine for a gateway.
    async fn new(live: bool) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("vbt.db");
        let mut conn = Db::connect_sqlite_path(&db_path).await.unwrap();
        for stmt in ddl().split(';').map(str::trim).filter(|s| !s.is_empty()) {
            conn.execute(stmt, &[]).await.unwrap();
        }
        drop(conn);
        let mut settings = Settings::defaults().unwrap();
        settings.dry_run = !live;
        settings.vbt_execution_enabled = live;
        settings.sole_user_id = 1;
        settings.sole_broker_account_id = 1;
        settings.kite_api_key = "replay-test-key".to_owned();
        settings.db_backend = "sqlite".to_owned();
        settings.db_path = db_path.display().to_string();
        settings.token_file = dir.path().join("no-such-token.json").display().to_string();
        std::fs::write(
            dir.path().join("kite.json"),
            r#"{"session": {"authed": true}, "routes": {}, "quotes": {}}"#,
        )
        .unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, &settings.kite_api_key).unwrap());
        let db = Db::open(DbConfig::from_settings(&settings, None))
            .await
            .unwrap();
        let root = desk_root();
        let paths = DeskPaths {
            root: dir.path().to_path_buf(),
            templates: root.join("app").join("templates"),
            static_dir: root.join("app").join("static"),
        };
        let state = build_state(StateParts {
            settings,
            clock: Arc::new(FixedClock::new(now())),
            ids: Arc::new(HarnessIds::new("vbt-route-tests")),
            db,
            kite: kite.clone(),
            paths,
            harness: None,
        });
        let state = if live {
            AppState {
                gateway: Arc::new(ExplodingGateway),
                ..state
            }
        } else {
            state
        };
        Self {
            app: build_app(state),
            kite,
            db_path,
            _dir: dir,
        }
    }

    async fn conn(&self) -> Conn {
        Db::connect_sqlite_path(&self.db_path).await.unwrap()
    }

    async fn send(&self, request: Request<Body>) -> Reply {
        let r = self.app.clone().oneshot(request).await.unwrap();
        let status = r.status();
        let headers = r.headers().clone();
        let bytes = to_bytes(r.into_body(), usize::MAX).await.unwrap();
        Reply {
            status,
            headers,
            text: String::from_utf8(bytes.to_vec()).unwrap(),
        }
    }

    async fn get(&self, path: &str) -> Reply {
        self.send(
            Request::get(path)
                .header(header::HOST, "127.0.0.1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
    }

    async fn post_from(&self, path: &str, origin: Option<&str>) -> Reply {
        let mut b = Request::post(path)
            .header(header::HOST, "127.0.0.1")
            .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded");
        if let Some(o) = origin {
            b = b.header(header::ORIGIN, o);
        }
        self.send(b.body(Body::empty()).unwrap()).await
    }

    async fn post(&self, path: &str) -> Reply {
        self.post_from(path, Some("http://127.0.0.1")).await
    }

    /// `_rows(store)`.
    async fn scan_rows(&self) -> Vec<baskfy_desk::storage::Row> {
        self.conn()
            .await
            .execute("SELECT * FROM vb_scan_run ORDER BY id", &[])
            .await
            .unwrap()
            .rows
    }

    async fn exec(&self, sql: &str, params: &[Param]) {
        self.conn().await.execute(sql, params).await.unwrap();
    }

    /// `store.request_scan(now=at)` through the real store over the same file.
    async fn request_scan(&self, at: DateTime<FixedOffset>) -> i64 {
        let mut store =
            baskfy_desk::lanes::vbt::store::PgVbtStore::new(self.conn().await, 1, "", Some(1));
        store.request_scan(at, None, None).await.unwrap()
    }

    /// `_nothing_but_the_scan_row(store)`, and the broker saw nothing either.
    async fn nothing_but_the_scan_row(&self) {
        let mut conn = self.conn().await;
        for table in MONEY_TABLES {
            let rows = conn
                .execute(&format!("SELECT COUNT(*) AS n FROM {table}"), &[])
                .await
                .unwrap()
                .rows;
            assert_eq!(
                rows[0].get("n"),
                Some(&DbValue::Int(0)),
                "a scan wrote to {table}"
            );
        }
        assert!(
            self.kite.write_calls().is_empty(),
            "{:?}",
            self.kite.write_calls()
        );
    }
}

fn id_of(row: &baskfy_desk::storage::Row) -> i64 {
    match row.get("id") {
        Some(DbValue::Int(n)) => *n,
        other => panic!("{other:?}"),
    }
}

/// `MONEY_TABLES`: the tables a scan must leave alone.
const MONEY_TABLES: [&str; 5] = [
    "vb_plan",
    "vb_plan_line",
    "vb_order",
    "vb_position",
    "vb_fill",
];

// =============================================================================================
// TestTheScanRoutesTheContractFixed
// =============================================================================================

#[tokio::test]
async fn a_press_is_a_202_with_the_run_to_poll() {
    let c = Client::new(false).await;
    let r = c.post("/vbt/scan").await;
    assert_eq!(r.status, StatusCode::ACCEPTED, "{}", r.text);
    let rows = c.scan_rows().await;
    assert_eq!(rows.len(), 1);
    assert_eq!(
        r.json(),
        serde_json::json!({
            "run_id": id_of(&rows[0]),
            "status": "QUEUED",
            "requested_at": "2026-09-09T21:20:00+05:30",
        })
    );
    assert_eq!(rows[0].get("source").and_then(|v| v.as_str()), Some("desk"));
    assert_eq!(
        rows[0].get("task_id"),
        Some(&DbValue::Null),
        "the sweep publishes it; the desk has no Celery"
    );
}

#[tokio::test]
async fn a_second_press_while_one_is_in_flight_is_a_409_naming_it() {
    let c = Client::new(false).await;
    let first = c.post("/vbt/scan").await.json()["run_id"].as_i64().unwrap();
    let second = c.post("/vbt/scan").await;
    assert_eq!(second.status, StatusCode::CONFLICT);
    assert!(
        second.json()["detail"]
            .as_str()
            .unwrap()
            .contains(&format!("Scan {first} is queued"))
    );
    assert_eq!(
        c.scan_rows().await.len(),
        1,
        "the second press wrote nothing"
    );
}

#[tokio::test]
async fn a_press_inside_a_minute_of_a_finished_one_is_a_429_with_retry_after() {
    let c = Client::new(false).await;
    c.request_scan(now() - Duration::seconds(20)).await;
    c.exec("UPDATE vb_scan_run SET status = 'DONE'", &[]).await;
    let r = c.post("/vbt/scan").await;
    assert_eq!(r.status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(r.headers.get("retry-after").unwrap(), "40");
    assert!(
        r.json()["detail"]
            .as_str()
            .unwrap()
            .contains("one a minute is the limit")
    );
    assert_eq!(c.scan_rows().await.len(), 1);
}

#[tokio::test]
async fn a_dead_worker_does_not_wedge_the_button() {
    let c = Client::new(false).await;
    c.request_scan(
        now() - Duration::seconds(baskfy_desk::lanes::vbt::store::RESCAN_STALE_AFTER_SECONDS + 1),
    )
    .await;
    c.exec("UPDATE vb_scan_run SET status = 'RUNNING'", &[])
        .await;
    assert_eq!(c.post("/vbt/scan").await.status, StatusCode::ACCEPTED);
    assert_eq!(c.scan_rows().await.len(), 2);
}

#[tokio::test]
async fn get_scan_reads_the_run_back_in_the_contract_s_vocabulary() {
    for status in ["QUEUED", "RUNNING", "DONE", "FAILED"] {
        let c = Client::new(false).await;
        let run_id = c.request_scan(now()).await;
        c.exec("UPDATE vb_scan_run SET status = ?", &[txt(status)])
            .await;
        let body = c.get(&format!("/vbt/scan/{run_id}")).await.json();
        assert_eq!(body["run_id"], run_id, "{status}");
        assert_eq!(body["status"], status);
        assert_eq!(body["source"], "desk");
        assert!(
            body["session_date"].is_null(),
            "only the worker decides which session"
        );
        assert!(body["error"].is_null());
    }
}

#[tokio::test]
async fn get_scan_carries_the_funnel_and_the_reason_the_worker_wrote() {
    let c = Client::new(false).await;
    let run_id = c.request_scan(now()).await;
    c.exec(
        "UPDATE vb_scan_run SET status = 'DONE', session_date = ?, detail = ?",
        &[
            txt("2026-09-11"),
            txt(r#"{"signals": 19, "scan_hits": 41}"#),
        ],
    )
    .await;
    let body = c.get(&format!("/vbt/scan/{run_id}")).await.json();
    assert_eq!(body["session_date"], "2026-09-11");
    assert_eq!(
        body["detail"],
        serde_json::json!({"signals": 19, "scan_hits": 41})
    );
}

#[tokio::test]
async fn an_unknown_run_is_a_404_rather_than_a_five_hundred() {
    let c = Client::new(false).await;
    assert_eq!(
        c.get("/vbt/scan/999999").await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn a_run_that_is_not_this_desk_s_user_is_a_404_rather_than_a_peek() {
    let c = Client::new(false).await;
    c.exec(
        "INSERT INTO vb_scan_run (user_id, requested_at, status, source) \
         VALUES (2, ?, 'DONE', 'desk')",
        &[txt("2026-09-09T21:20:00+05:30")],
    )
    .await;
    let theirs = id_of(&c.scan_rows().await[0]);
    assert_eq!(
        c.get(&format!("/vbt/scan/{theirs}")).await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn the_older_rescan_name_still_answers_exactly_as_the_page_expects() {
    let c = Client::new(false).await;
    let accepted = c.post("/vbt/rescan").await;
    assert_eq!(accepted.status, StatusCode::OK);
    assert_eq!(accepted.json()["accepted"], true);
    let in_flight = c.post("/vbt/rescan").await.json();
    assert_eq!(
        in_flight,
        serde_json::json!({
            "accepted": false,
            "reason": "a re-detect is already in flight",
            "run_id": accepted.json()["run_id"],
            "status": "QUEUED",
        })
    );
    c.exec("UPDATE vb_scan_run SET status = 'DONE'", &[]).await;
    let too_soon = c.post("/vbt/rescan").await.json();
    assert_eq!(too_soon["accepted"], false);
    assert_eq!(too_soon["reason"], "too soon");
    assert_eq!(too_soon["retry_after_seconds"], 60);
}

#[tokio::test]
async fn both_names_write_the_scan_row_and_nothing_else() {
    let c = Client::new(false).await;
    assert_eq!(c.post("/vbt/scan").await.status, StatusCode::ACCEPTED);
    c.exec("UPDATE vb_scan_run SET status = 'DONE'", &[]).await;
    assert_eq!(c.post("/vbt/rescan").await.json()["accepted"], false);
    c.nothing_but_the_scan_row().await;
}

#[tokio::test]
async fn the_scan_route_is_covered_by_the_desk_s_origin_check() {
    let c = Client::new(false).await;
    let hostile = c.post_from("/vbt/scan", Some("http://evil.example")).await;
    assert_eq!(hostile.status, StatusCode::FORBIDDEN);
    assert!(c.scan_rows().await.is_empty());
}

// =============================================================================================
// test_vbt_scan_safety.py — TestNoScanRouteReachesABrokerWithTheFlagsLive
// =============================================================================================

#[tokio::test]
async fn the_contract_scan_places_nothing() {
    let c = Client::new(true).await;
    let r = c.post("/vbt/scan").await;
    assert_eq!(r.status, StatusCode::ACCEPTED, "{}", r.text);
    c.nothing_but_the_scan_row().await;
}

#[tokio::test]
async fn the_older_rescan_name_places_nothing() {
    let c = Client::new(true).await;
    let r = c.post("/vbt/rescan").await;
    assert_eq!(r.status, StatusCode::OK, "{}", r.text);
    assert_eq!(r.json()["accepted"], true);
    c.nothing_but_the_scan_row().await;
}

#[tokio::test]
async fn the_poll_places_nothing() {
    let c = Client::new(true).await;
    let run_id = c.request_scan(now() - Duration::minutes(5)).await;
    assert_eq!(
        c.get(&format!("/vbt/scan/{run_id}")).await.status,
        StatusCode::OK
    );
    c.nothing_but_the_scan_row().await;
}

#[tokio::test]
async fn both_refusals_place_nothing_either() {
    let c = Client::new(true).await;
    assert_eq!(c.post("/vbt/scan").await.status, StatusCode::ACCEPTED);
    assert_eq!(c.post("/vbt/scan").await.status, StatusCode::CONFLICT);
    c.exec("UPDATE vb_scan_run SET status = 'DONE'", &[]).await;
    assert_eq!(
        c.post("/vbt/scan").await.status,
        StatusCode::TOO_MANY_REQUESTS
    );
    c.nothing_but_the_scan_row().await;
}

#[tokio::test]
#[should_panic(expected = "reached the gateway")]
async fn the_landmine_is_real() {
    let _ = ExplodingGateway.place(PlaceOrder::default()).await;
}

// =============================================================================================
// The source: TestTheScanSurfaceIsWhatWeThinkItIs, TestNoScanRouteEvenNamesAnOrder, and the
// route-shape halves of TestTheLaw / TestOnlyAConfirmedRequestCanProduceAnOrder
// =============================================================================================

fn page_source() -> String {
    std::fs::read_to_string(lane_dir().join("vbt.rs")).unwrap()
}

fn store_source() -> String {
    std::fs::read_to_string(lane_dir().join("vbt/store.rs")).unwrap()
}

/// `(METHOD, path)` for every `.route(path, verb(handler))` in a `routes()` body.
fn routes_in(source: &str) -> Vec<(String, String, String)> {
    let re = regex::Regex::new(r#"\.route\("([^"]+)",\s*(get|post|put|delete|patch)\((\w+)\)\)"#)
        .unwrap();
    re.captures_iter(source)
        .map(|c| (c[2].to_uppercase(), c[1].to_owned(), c[3].to_owned()))
        .collect()
}

/// The code of one `fn name(` — its body, with `//` comments (Rust's docstrings) blanked.
fn fn_code(source: &str, name: &str) -> String {
    let start = [
        format!("async fn {name}("),
        format!("pub async fn {name}("),
        format!("fn {name}("),
    ]
    .iter()
    .find_map(|p| source.find(p.as_str()))
    .unwrap_or_else(|| panic!("no fn {name}"));
    let open = start + source[start..].find('{').unwrap();
    let mut depth = 0usize;
    let mut end = open;
    for (i, ch) in source[open..].char_indices() {
        match ch {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    end = open + i + 1;
                    break;
                }
            }
            _ => {}
        }
    }
    strip_prose(&source[start..end])
}

/// `_strip_prose`: comments blanked (`//`, `///`, `//!`, and Python's `#` lines when asked).
fn strip_prose(source: &str) -> String {
    source
        .lines()
        .map(|l| l.find("//").map_or(l, |i| &l[..i]))
        .collect::<Vec<_>>()
        .join("\n")
}

/// `EXPECTED_SCAN_ROUTES`.
fn expected_scan_routes() -> Vec<(String, String)> {
    let mut v = vec![
        ("POST".to_owned(), "/vbt/rescan".to_owned()),
        ("POST".to_owned(), "/vbt/scan".to_owned()),
        ("GET".to_owned(), "/vbt/scan/{run_id}".to_owned()),
    ];
    v.sort();
    v
}

/// `ORDER_WORDS`.
const ORDER_WORDS: [&str; 10] = [
    "execute_line",
    "gateway",
    "place",
    "place_order",
    "place_gtt",
    "delete_gtt",
    "kc.",
    "kiteconnect",
    "gtt",
    "ordergateway",
];

#[test]
fn the_desk_mounts_exactly_the_scan_routes_this_file_covers() {
    let mut found: Vec<(String, String)> = routes_in(&page_source())
        .into_iter()
        .filter(|(_, p, _)| p.contains("scan"))
        .map(|(m, p, _)| (m, p))
        .collect();
    found.sort();
    assert_eq!(found, expected_scan_routes());
}

#[test]
fn the_route_scan_would_notice_a_new_one() {
    let planted = r#".route("/vbt/scan/confirm", post(go))"#;
    let found = routes_in(planted);
    assert_eq!(
        found,
        vec![(
            "POST".to_owned(),
            "/vbt/scan/confirm".to_owned(),
            "go".to_owned()
        )]
    );
}

#[test]
fn exactly_one_desk_route_can_still_reach_an_order() {
    let source = strip_prose(&page_source());
    assert_eq!(source.matches("execute::execute_line(").count(), 1);
    let posts: Vec<String> = routes_in(&page_source())
        .into_iter()
        .filter(|(m, _, _)| m == "POST")
        .map(|(_, p, _)| p)
        .collect();
    assert_eq!(posts.iter().filter(|p| *p == "/vbt/execute").count(), 1);
}

#[test]
fn there_is_exactly_one_execute_route_and_it_takes_one_line() {
    let mut posts: Vec<(String, String)> = routes_in(&page_source())
        .into_iter()
        .filter(|(m, _, _)| m == "POST")
        .map(|(_, p, h)| (p, h))
        .collect();
    posts.sort();
    assert_eq!(
        posts.iter().map(|(p, _)| p.as_str()).collect::<Vec<_>>(),
        vec!["/vbt/execute", "/vbt/rescan", "/vbt/scan"]
    );
    let handler = &posts[0].1;
    let code = fn_code(&page_source(), handler);
    for field in [
        r#"form.str("plan_id")"#,
        r#"form.str("line_id")"#,
        r#"form.str("confirm")"#,
    ] {
        assert!(code.contains(field), "{handler} does not read {field}");
    }
    // One plan, one line, one confirm — and no list of anything in the request.
    assert!(!code.contains("Vec<"), "{handler} takes a list");
    assert!(!code.contains("form.list"), "{handler} takes a list");
}

#[test]
fn the_scan_routes_cannot_reach_an_order() {
    for (path, handler) in routes_in(&page_source())
        .into_iter()
        .filter(|(m, p, _)| m == "POST" && p != "/vbt/execute")
        .map(|(_, p, h)| (p, h))
    {
        let code = fn_code(&page_source(), &handler).to_lowercase();
        for forbidden in ["execute_line", "gateway", "place", "kc.", "gtt", "confirm"] {
            assert!(!code.contains(forbidden), "{path} names {forbidden}");
        }
        assert!(
            code.contains("request_scan"),
            "{path} does not write a scan row at all"
        );
    }
}

#[test]
fn the_handler_names_no_order_verb() {
    for handler in ["vbt_scan_now", "vbt_scan_run", "vbt_rescan"] {
        let code = fn_code(&page_source(), handler).to_lowercase();
        for word in ORDER_WORDS {
            assert!(!code.contains(word), "{handler} names {word}");
        }
    }
}

#[test]
fn the_store_method_the_scan_calls_writes_one_table() {
    let code = fn_code(&store_source(), "request_scan");
    assert_eq!(code.matches("INSERT INTO").count(), 1);
    assert!(code.contains("vb_scan_run"));
    for table in MONEY_TABLES {
        assert!(
            !code.contains(&format!("\"{table}\"")),
            "request_scan names {table}"
        );
    }
}

#[test]
fn the_poll_reads_and_does_not_write() {
    let code = fn_code(&store_source(), "scan_run").to_uppercase();
    for verb in ["INSERT", "UPDATE", "DELETE"] {
        assert!(!code.contains(verb), "scan_run performs an {verb}");
    }
}

#[test]
fn there_is_no_auto_execute_flag_anywhere_near_the_scan() {
    assert!(!page_source().to_uppercase().contains("AUTO_EXECUTE"));
}

#[test]
fn the_only_execute_entry_point_takes_a_confirm() {
    let source = std::fs::read_to_string(lane_dir().join("vbt/execute.rs")).unwrap();
    let start = source.find("pub async fn execute_line(").unwrap();
    let sig = &source[start..start + source[start..].find(')').unwrap()];
    assert!(sig.contains("confirm: &str"));
    assert!(sig.contains("plan_id: &str"));
    // And it is the only public way in: the locked body is private.
    assert!(source.contains("\nasync fn execute_line_locked("));
}

#[test]
fn no_get_on_the_desk_page_can_execute() {
    for (method, path, _) in routes_in(&page_source()) {
        if method == "GET" {
            assert!(!path.contains("execute"), "{path}");
        }
    }
}

// =============================================================================================
// test_vbt_safety.py — TestThereIsNoAutoExecute
// =============================================================================================

/// Every Python, TypeScript and Rust source in either tree that could name a flag; test trees
/// excluded, as in Python (a prohibition must not trip the check that states it).
fn sources() -> Vec<PathBuf> {
    let roots = [
        desk_root().join("app"),
        monorepo().join("decile-blueprint/services"),
        monorepo().join("decile-blueprint/packages/core/src"),
        monorepo().join("decile-blueprint/apps/web/src"),
        monorepo().join("rust/crates"),
    ];
    let mut out = Vec::new();
    let mut stack: Vec<PathBuf> = roots.into_iter().filter(|r| r.is_dir()).collect();
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            let name = path.file_name().unwrap().to_string_lossy().to_string();
            if path.is_dir() {
                if [
                    "node_modules",
                    "__pycache__",
                    "__tests__",
                    "tests",
                    "target",
                    "fixtures",
                ]
                .contains(&name.as_str())
                {
                    continue;
                }
                stack.push(path);
            } else if ["py", "ts", "tsx", "rs"]
                .iter()
                .any(|e| path.extension().is_some_and(|x| x == *e))
            {
                out.push(path);
            }
        }
    }
    out
}

/// `_strip_prose`: triple-quoted strings, then `#` and `//` comments, line count preserved.
fn strip_all_prose(source: &str) -> String {
    let docs = regex::Regex::new(r#"(?s)(""".*?"""|'''.*?''')"#).unwrap();
    let without = docs.replace_all(source, |c: &regex::Captures<'_>| {
        "\n".repeat(c[0].matches('\n').count())
    });
    let comment = regex::Regex::new(r"(?m)(#|//).*$").unwrap();
    comment.replace_all(&without, "").into_owned()
}

fn flag_pattern() -> regex::Regex {
    regex::Regex::new(r"VBT[_A-Z]*AUTO[_A-Z]*EXECUTE|vbt[_a-z]*auto[_a-z]*execute").unwrap()
}

#[test]
fn no_source_defines_or_reads_a_vbt_auto_execute_flag() {
    let pattern = flag_pattern();
    let files = sources();
    assert!(
        files.len() > 100,
        "the scan found almost nothing, so it would prove nothing"
    );
    let mut offenders = Vec::new();
    for path in files {
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        let code = strip_all_prose(&text);
        for (i, line) in code.lines().enumerate() {
            if pattern.is_match(line) {
                offenders.push(format!("{}:{}", path.display(), i + 1));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "an auto-execute flag appeared in {offenders:?}"
    );
}

#[test]
fn the_scan_would_catch_a_real_one() {
    let planted = strip_all_prose("vbt_auto_execute: bool = True  # a comment\n");
    assert!(flag_pattern().is_match(&planted));
    let planted_rs = strip_all_prose("pub vbt_auto_execute: bool, // a comment\n");
    assert!(flag_pattern().is_match(&planted_rs));
    assert_eq!(
        strip_all_prose("# vbt_auto_execute is forbidden\n").trim(),
        ""
    );
    assert_eq!(
        strip_all_prose("// vbt_auto_execute is forbidden\n").trim(),
        ""
    );
}

#[test]
fn the_desk_config_says_so_where_somebody_would_look_for_it() {
    let source = std::fs::read_to_string(desk_root().join("app/config.py")).unwrap();
    assert!(source.contains("THERE IS NO BASKFY_VBT_AUTO_EXECUTE"));
    // And the Rust lane says it where a reader of the port would look.
    assert!(page_source().contains("**No auto-execute flag,"));
}

#[test]
fn no_scheduler_entry_reaches_an_execute_path() {
    let beat = std::fs::read_to_string(
        monorepo().join("decile-blueprint/services/worker/src/baskfy_worker/celery_app.py"),
    )
    .unwrap();
    let re = regex::Regex::new(r#""(baskfy\.vbt\.[a-z_]+)""#).unwrap();
    let entries: Vec<String> = re.captures_iter(&beat).map(|c| c[1].to_owned()).collect();
    assert!(
        !entries.is_empty(),
        "no vbt tasks are scheduled at all, so this would prove nothing"
    );
    for name in entries {
        assert!(
            !name.contains("execute"),
            "{name} is scheduled and names an execute path"
        );
        assert!(
            !name.contains("confirm"),
            "{name} is scheduled and names a confirm"
        );
    }
}

#[tokio::test]
async fn the_execute_route_from_a_hostile_origin_is_refused_before_anything_is_read() {
    let c = Client::new(false).await;
    let r = c
        .post_from("/vbt/execute", Some("http://evil.example"))
        .await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    let _ = ist();
}
