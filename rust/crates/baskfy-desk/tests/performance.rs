//! `performance_view.build` against Python's own output on the same stored book
//! (`performance_golden.json`), the flags that keep short history honest, and the two routes.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp,
    clippy::cast_precision_loss,
    missing_docs
)]

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode};
use baskfy_desk::app::{DeskPaths, StateParts};
use baskfy_desk::clock::FixedClock;
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::analytics::analytics_views::benchmark::{self as b, PRI, TRI};
use baskfy_desk::lanes::analytics::analytics_views::performance_view as pv;
use baskfy_desk::lanes::weekly::weekly_books::snapshot_db as sdb;
use baskfy_desk::storage::{Conn, Db, DbConfig, Param};
use baskfy_desk::wire::PyValue;
use baskfy_desk::{Settings, build_app, build_state};
use chrono::{Duration, FixedOffset, NaiveDate, TimeZone};
use serde_json::Value;
use tower::ServiceExt as _;

fn d(s: &str) -> NaiveDate {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
}

fn iso(x: NaiveDate) -> String {
    x.format("%Y-%m-%d").to_string()
}

fn epoch(s: &str) -> f64 {
    d(s).and_hms_opt(0, 0, 0).unwrap().and_utc().timestamp() as f64
}

/// The book `perf_golden.py` stores through Python's own `db`/`benchmark` modules.
async fn seed(conn: &mut Conn) {
    let start = d("2026-03-02");
    let mut nav = 1_000_000.0;
    sdb::record_cashflow(conn, &iso(start), -1_000_000.0, "invest")
        .await
        .unwrap();
    let hj = r#"{"positions": [{"symbol": "AAA"}, {"symbol": "SGBX", "excluded": true}, {"symbol": "BBB"}]}"#;
    for i in 0..120_i64 {
        let day = start + Duration::days(i);
        if i > 0 {
            let r = ((i * 37) % 11 - 5) as f64 / 1000.0;
            nav *= 1.0 + r;
        }
        if i == 40 {
            nav += 100_000.0;
            sdb::record_cashflow(conn, &iso(day), -100_000.0, "invest")
                .await
                .unwrap();
        }
        let row = sdb::SnapRow {
            date: iso(day),
            nav,
            invested: nav * 0.9,
            cash: nav * 0.1,
            holdings_json: hj.to_owned(),
        };
        sdb::save_snapshot(conn, &row, false).await.unwrap();
    }
    let mut lvl = 20_000.0;
    let mut rows = Vec::new();
    for i in 0..140_i64 {
        let day = start + Duration::days(i - 10);
        lvl *= 1.0 + ((i * 13) % 7 - 3) as f64 / 1000.0;
        rows.push((iso(day), lvl));
    }
    b::upsert_benchmark(conn, "NIFTY 500", &rows, TRI)
        .await
        .unwrap();
    let mom: Vec<(String, f64)> = rows[20..]
        .iter()
        .map(|(t, v)| (t.clone(), v * 1.5))
        .collect();
    b::upsert_benchmark(conn, "NIFTY200MOMENTM30", &mom, PRI)
        .await
        .unwrap();
    let stale = [(iso(start - Duration::days(5)), 1.0), (iso(start), 2.0)];
    b::upsert_benchmark(conn, "NIFTY200MOMENTM50", &stale, TRI)
        .await
        .unwrap();
    for (sym, e, x, pnl, score, reason, costs) in [
        (
            "AAA",
            "2026-03-02",
            Some("2026-04-01"),
            5000.0,
            81.0,
            Some("rank_drop"),
            100.0,
        ),
        (
            "BBB",
            "2026-03-02",
            Some("2026-03-20"),
            -2500.0,
            70.0,
            Some("stop_loss"),
            80.0,
        ),
        (
            "AAA",
            "2026-03-10",
            Some("2026-05-01"),
            1200.0,
            77.0,
            Some("rank_drop"),
            50.0,
        ),
        (
            "CCC",
            "2026-03-15",
            Some("2026-04-15"),
            -800.0,
            60.0,
            None,
            40.0,
        ),
        ("DDD", "2026-04-01", None, 0.0, 66.0, None, 10.0),
    ] {
        conn.execute(
            "INSERT INTO trades(symbol, entry_ts, exit_ts, qty, entry_price, exit_price, entry_score, exit_reason, pnl, costs) VALUES(?,?,?,?,?,?,?,?,?,?)",
            &[
                Param::from(sym),
                Param::Float(epoch(e)),
                x.map_or(Param::Null, |x| Param::Float(epoch(x))),
                Param::Int(10),
                Param::Float(100.0),
                Param::Float(110.0),
                Param::Float(score),
                reason.map_or(Param::Null, Param::from),
                Param::Float(pnl),
                Param::Float(costs),
            ],
        )
        .await
        .unwrap();
    }
}

fn same(path: &str, got: &Value, want: &Value) {
    match (got, want) {
        (Value::Number(a), Value::Number(b)) => {
            let (a, b) = (a.as_f64().unwrap(), b.as_f64().unwrap());
            assert!(
                (a - b).abs() <= 1e-9 * b.abs().max(1e-6),
                "{path}: {a} != {b}"
            );
        }
        (Value::Object(a), Value::Object(b)) => {
            assert_eq!(
                a.keys().collect::<Vec<_>>(),
                b.keys().collect::<Vec<_>>(),
                "{path}"
            );
            for (k, v) in b {
                same(&format!("{path}.{k}"), &a[k], v);
            }
        }
        (Value::Array(a), Value::Array(b)) => {
            assert_eq!(a.len(), b.len(), "{path}");
            for (i, (x, y)) in a.iter().zip(b).enumerate() {
                same(&format!("{path}[{i}]"), x, y);
            }
        }
        (a, b) => assert_eq!(a, b, "{path}"),
    }
}

async fn sqlite(dir: &Path) -> Conn {
    let mut c = Db::connect_sqlite_path(&dir.join("p.db")).await.unwrap();
    c.migrate().await.unwrap();
    c
}

#[tokio::test]
async fn no_snapshots_says_how_to_record_one() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = sqlite(dir.path()).await;
    let view = pv::build(&mut c).await.unwrap();
    assert_eq!(
        pv::starlette_json(&PyValue::Map(view)).unwrap(),
        r#"{"available":false,"message":"No EOD snapshots recorded yet.","how_to":"python -m app.analytics.snapshot"}"#
    );
}

#[tokio::test]
async fn the_view_matches_python_on_the_same_book() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = sqlite(dir.path()).await;
    seed(&mut c).await;
    let view = pv::build(&mut c).await.unwrap();
    let got: Value =
        serde_json::from_str(&pv::starlette_json(&PyValue::Map(view)).unwrap()).unwrap();
    let want: Value = serde_json::from_str(include_str!("performance_golden.json")).unwrap();
    same("p", &got, &want);
}

#[tokio::test]
async fn short_history_is_flagged_not_presented_as_a_result() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = sqlite(dir.path()).await;
    for (i, nav) in [100.0, 101.0, 99.0].iter().enumerate() {
        let row = sdb::SnapRow {
            date: iso(d("2026-01-05") + Duration::days(i as i64)),
            nav: *nav,
            invested: *nav,
            cash: 0.0,
            holdings_json: String::new(),
        };
        sdb::save_snapshot(&mut c, &row, false).await.unwrap();
    }
    let view = pv::build(&mut c).await.unwrap();
    let v: Value = serde_json::from_str(&pv::starlette_json(&PyValue::Map(view)).unwrap()).unwrap();
    for m in v["metrics"].as_array().unwrap() {
        assert_eq!(
            m["flag"]["meaningful"],
            Value::Bool(false),
            "{}",
            m["label"]
        );
    }
    assert_eq!(v["metrics"][2]["value"], Value::Null);
    assert_eq!(v["metrics"][2]["note"], "No cashflows recorded.");
    assert_eq!(v["trades"], Value::Null);
    assert_eq!(v["relative"], serde_json::json!({}));
    assert_eq!(v["positions"], 0);
    assert_eq!(v["has_cashflows"], false);
}

#[test]
fn polyline_spans_the_box_and_a_flat_line_does_not_divide_by_zero() {
    assert_eq!(
        pv::polyline(&[1.0, 2.0, 3.0], 100.0, 10.0, None, None),
        "0.00,10.00 50.00,5.00 100.00,0.00"
    );
    assert_eq!(
        pv::polyline(&[5.0, 5.0], 10.0, 4.0, None, None),
        "0.00,4.00 10.00,4.00"
    );
    assert_eq!(pv::polyline(&[5.0], 10.0, 4.0, None, None), "");
}

#[test]
fn nan_is_refused_as_starlette_refuses_it() {
    let bad = PyValue::List(vec![PyValue::Float(f64::NAN)]);
    assert!(
        pv::starlette_json(&bad)
            .unwrap_err()
            .starts_with("Out of range float values are not JSON compliant")
    );
    let ok = PyValue::List(vec![PyValue::Float(1e16), PyValue::Str("é\"\n".into())]);
    assert_eq!(pv::starlette_json(&ok).unwrap(), "[1e+16,\"é\\\"\\n\"]");
}

// =============================================================================================
// the routes
// =============================================================================================

fn desk_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer")
}

async fn app(dir: &Path, kite_json: &str) -> axum::Router {
    let db_path = dir.join("p.db");
    let mut settings = Settings::defaults().unwrap();
    settings.dry_run = true;
    settings.kite_api_key = "replay-test-key".to_owned();
    settings.db_backend = "sqlite".to_owned();
    settings.db_path = db_path.display().to_string();
    settings.token_file = dir.join("no-such-token.json").display().to_string();
    std::fs::write(dir.join("kite.json"), kite_json).unwrap();
    let kite = Arc::new(ReplayKite::load(dir, None, &settings.kite_api_key).unwrap());
    let db = Db::open(DbConfig::from_settings(&settings, None))
        .await
        .unwrap();
    let root = desk_root();
    let ist = FixedOffset::east_opt(19_800).unwrap();
    let state = build_state(StateParts {
        settings,
        clock: Arc::new(FixedClock::new(
            ist.with_ymd_and_hms(2026, 6, 30, 16, 0, 0).unwrap(),
        )),
        ids: Arc::new(HarnessIds::new("performance-tests")),
        db,
        kite,
        paths: DeskPaths {
            root: dir.to_path_buf(),
            templates: root.join("app").join("templates"),
            static_dir: root.join("app").join("static"),
        },
        harness: None,
    });
    build_app(state)
}

async fn get(app: &axum::Router, path: &str) -> (StatusCode, String) {
    let resp = app
        .clone()
        .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    (status, String::from_utf8(body.to_vec()).unwrap())
}

const LOGGED_OUT: &str = r#"{"session": {"authed": false}, "routes": {}, "quotes": {}}"#;

const LOGGED_IN: &str = r#"{
  "session": {"authed": true},
  "routes": {
    "user.profile": {"data": {"user_id": "REPLAY"}},
    "portfolio.holdings": {"data": [
      {"tradingsymbol": "AAA", "exchange": "NSE", "isin": "INE000A01", "quantity": 10, "t1_quantity": 0,
       "collateral_quantity": 2, "average_price": 100.0, "last_price": 120.0, "day_change": 1.5,
       "day_change_percentage": 1.27},
      {"tradingsymbol": "BBB", "exchange": "NSE", "isin": "INE000B01", "quantity": 5, "t1_quantity": 0,
       "collateral_quantity": 0, "average_price": 200.0, "last_price": 190.0, "day_change": -2.0,
       "day_change_percentage": -1.04}
    ]},
    "portfolio.positions": {"data": {"net": [], "day": []}},
    "user.margins": {"data": {"equity": {"available": {"live_balance": 5000.0}}}},
    "gtt": {"data": []}
  },
  "quotes": {}
}"#;

#[tokio::test]
async fn the_data_route_is_the_view_model_and_the_page_renders_logged_out() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = sqlite(dir.path()).await;
    seed(&mut c).await;
    drop(c);
    let app = app(dir.path(), LOGGED_OUT).await;
    let (status, body) = get(&app, "/performance/data").await;
    assert_eq!(status, StatusCode::OK);
    let got: Value = serde_json::from_str(&body).unwrap();
    same(
        "p",
        &got,
        &serde_json::from_str(include_str!("performance_golden.json")).unwrap(),
    );
    let (status, page) = get(&app, "/performance").await;
    assert_eq!(status, StatusCode::OK, "{page}");
    assert!(page.contains("Not available: not logged in."), "{page}");
    assert!(page.contains("120 sessions"));
}

#[tokio::test]
async fn the_page_shows_the_holdings_block_over_kite_holdings() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = sqlite(dir.path()).await;
    seed(&mut c).await;
    drop(c);
    let app = app(dir.path(), LOGGED_IN).await;
    let (status, page) = get(&app, "/performance").await;
    assert_eq!(status, StatusCode::OK, "{page}");
    assert!(page.contains("data-sym=\"AAA\""), "{page}");
    assert!(page.contains("data-sym=\"BBB\""));
    assert!(
        page.contains("2 pledged"),
        "pledged shares are shown as information"
    );
    assert!(page.contains("missing"), "an unprotected holding is named");
}

#[tokio::test]
async fn head_is_405_get_only() {
    let dir = tempfile::tempdir().unwrap();
    let app = app(dir.path(), LOGGED_OUT).await;
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("HEAD")
                .uri("/performance/data")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::METHOD_NOT_ALLOWED);
    let (status, body) = get(&app, "/performance/data").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.starts_with(r#"{"available":false"#));
}
