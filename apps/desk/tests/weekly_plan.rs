//! Lane weekly_plan: the plan store (graded on `fixtures/weekly/plan_store.json`, dumped from
//! the live Python by `tools/parity/desk_weekly_goldens.py`), the spec tests of
//! `tests/test_plan_store.py` (the properties, not the numbers), the confirm gate of the weekly
//! book, and the strategy interface.
//!
//! Not ported from `test_plan_store.py`: the two `metrics.slippage` tests (the metric is the
//! analytics lane's); the rows they read are covered here by `orders_rows`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic, missing_docs)]

use std::path::Path;

use baskfy_desk::lanes::analytics::web::row_dict;
use baskfy_desk::lanes::weekly::plan_store as ps;
use baskfy_desk::plan::{Plan, check_execute};
use baskfy_desk::storage::{Conn, Db, DbValue, Param};
use baskfy_desk::wire::{PyDict, PyValue, dict, orjson_dumps};
use chrono::{NaiveDate, TimeZone};
use serde_json::Value;

async fn conn(dir: &Path) -> Conn {
    let mut c = Db::connect_sqlite_path(&dir.join("p.db")).await.unwrap();
    c.migrate().await.unwrap();
    c
}

fn py(v: &str) -> PyValue {
    PyValue::from_json(v.as_bytes()).unwrap()
}

fn map(v: PyValue) -> PyDict {
    match v {
        PyValue::Map(m) => m,
        other => panic!("not a dict: {other:?}"),
    }
}

fn order(symbol: &str, delta: i64, ref_price: f64, weight: f64) -> PyValue {
    PyValue::Map(dict([
        ("symbol", PyValue::Str(symbol.to_owned())),
        ("delta", PyValue::Int(delta)),
        ("ref_price", PyValue::Float(ref_price)),
        ("weight", PyValue::Float(weight)),
    ]))
}

fn plan(pid: &str) -> PyDict {
    dict([
        ("plan_id", PyValue::Str(pid.to_owned())),
        ("created_at", PyValue::Float(1_700_000_000.0)),
        (
            "orders",
            PyValue::List(vec![
                order("AAA", 10, 100.0, 5.0),
                order("BBB", -5, 200.0, 0.0),
                order("CCC", 0, 300.0, 7.0),
            ]),
        ),
    ])
}

fn res(pairs: &[(&str, &str)]) -> PyDict {
    dict(
        pairs
            .iter()
            .map(|(k, v)| (*k, PyValue::Str((*v).to_owned())))
            .collect::<Vec<_>>(),
    )
}

fn broker(symbol: &str, status: &str, filled: i64, avg: Option<f64>, oid: &str) -> PyDict {
    dict([
        ("tradingsymbol", PyValue::Str(symbol.to_owned())),
        ("status", PyValue::Str(status.to_owned())),
        ("filled_quantity", PyValue::Int(filled)),
        ("average_price", avg.map_or(PyValue::None, PyValue::Float)),
        ("order_id", PyValue::Str(oid.to_owned())),
    ])
}

async fn count(c: &mut Conn, table: &str) -> i64 {
    c.execute(&format!("SELECT COUNT(*) c FROM {table}"), &[])
        .await
        .unwrap()
        .rows[0]
        .get("c")
        .and_then(DbValue::as_i64)
        .unwrap()
}

async fn row(c: &mut Conn, sym: &str) -> PyDict {
    let cur = c
        .execute(
            "SELECT * FROM rebalance_orders WHERE symbol=?",
            &[Param::Text(sym.to_owned())],
        )
        .await
        .unwrap();
    row_dict(&cur.rows[0])
}

fn s(d: &PyDict, k: &str) -> String {
    match d.get(k) {
        Some(PyValue::Str(s)) => s.clone(),
        other => format!("{other:?}"),
    }
}

fn i(d: &PyDict, k: &str) -> i64 {
    match d.get(k) {
        Some(PyValue::Int(i)) => *i,
        other => panic!("{k}: {other:?}"),
    }
}

fn strs(d: &PyDict, k: &str) -> Vec<String> {
    match d.get(k) {
        Some(PyValue::List(l)) => l
            .iter()
            .map(|v| match v {
                PyValue::Str(s) => s.clone(),
                o => format!("{o:?}"),
            })
            .collect(),
        other => panic!("{k}: {other:?}"),
    }
}

const NOW: &str = "2026-10-30T09:16:00";

// =============================================================================================
// the golden: save → record → reconcile, byte for byte against the live Python
// =============================================================================================

#[tokio::test]
async fn plan_store_matches_the_python_golden() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/weekly/plan_store.json");
    let golden: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    let cases = golden["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 12);
    for (n, case) in cases.iter().enumerate() {
        let tmp = tempfile::tempdir().unwrap();
        let mut c = conn(tmp.path()).await;
        let plan = map(py(case["plan"].as_str().unwrap()));
        let versions_want: Value =
            serde_json::from_str(case["versions"].as_str().unwrap()).unwrap();
        let v0 = &versions_want[0];
        let ev = v0["evaluation_id"].as_str().map(ToOwned::to_owned);
        let note = v0["note"].as_str().unwrap();
        let pid = ps::save_plan(&mut c, &plan, ev.as_deref(), note, 1.0)
            .await
            .unwrap();
        assert_eq!(PyValue::Str(pid), plan.get("plan_id").cloned().unwrap());
        let results: Vec<PyDict> = match py(case["results"].as_str().unwrap()) {
            PyValue::List(l) => l.into_iter().map(map).collect(),
            _ => panic!(),
        };
        let pid = s(&plan, "plan_id");
        let rec = ps::record_execution(&mut c, &pid, &results).await.unwrap();
        assert_eq!(
            orjson_dumps(&PyValue::Map(rec)).unwrap(),
            case["record"].as_str().unwrap(),
            "case {n}: record"
        );
        let recon = ps::reconciliation(&mut c, &pid).await.unwrap();
        assert_eq!(
            orjson_dumps(&PyValue::Map(recon)).unwrap(),
            case["reconciliation"].as_str().unwrap(),
            "case {n}: reconciliation"
        );
        let rows = c
            .execute("SELECT * FROM rebalance_orders ORDER BY id", &[])
            .await
            .unwrap()
            .rows;
        let got = PyValue::List(rows.iter().map(|r| PyValue::Map(row_dict(r))).collect());
        assert_eq!(
            orjson_dumps(&got).unwrap(),
            case["orders"].as_str().unwrap(),
            "case {n}: orders"
        );
        let vrows = c
            .execute("SELECT * FROM rebalance_versions", &[])
            .await
            .unwrap()
            .rows;
        let mut got: Value =
            serde_json::from_str(&orjson_dumps(&PyValue::Map(row_dict(&vrows[0]))).unwrap())
                .unwrap();
        let mut want = v0.clone();
        if matches!(plan.get("created_at"), Some(PyValue::None) | None) {
            // `time.time()` at the dump; the clock is a seam here (1.0).
            assert_eq!(got["created_ts"], serde_json::json!(1.0), "case {n}");
            got["created_ts"] = Value::Null;
            want["created_ts"] = Value::Null;
        }
        assert_eq!(got, want, "case {n}: versions");
        assert_eq!(vrows.len(), 1);
    }
}

// =============================================================================================
// saving (test_plan_store.py)
// =============================================================================================

#[tokio::test]
async fn a_plan_and_its_orders_are_stored_and_a_hold_is_not_an_order() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    assert_eq!(count(&mut c, "rebalance_versions").await, 1);
    assert_eq!(count(&mut c, "rebalance_orders").await, 2);
}

#[tokio::test]
async fn sides_are_derived_from_the_delta_and_orders_start_pending() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    assert_eq!(s(&row(&mut c, "AAA").await, "side"), "BUY");
    assert_eq!(s(&row(&mut c, "BBB").await, "side"), "SELL");
    assert_eq!(s(&row(&mut c, "AAA").await, "status"), ps::PENDING);
    assert_eq!(s(&row(&mut c, "BBB").await, "status"), ps::PENDING);
}

#[tokio::test]
async fn resaving_the_same_plan_does_not_duplicate_orders() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    assert_eq!(count(&mut c, "rebalance_orders").await, 2);
    assert_eq!(count(&mut c, "rebalance_versions").await, 1);
}

#[tokio::test]
async fn the_evaluation_link_is_stored_and_its_absence_is_not_a_loss() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), Some("eval-1"), "", 0.0)
        .await
        .unwrap();
    let got = ps::latest(&mut c, Some("eval-1")).await.unwrap().unwrap();
    assert_eq!(s(&got, "version_id"), "p1");
    ps::save_plan(&mut c, &plan("p2"), None, "", 0.0)
        .await
        .unwrap();
    let got = ps::latest(&mut c, Some("eval-1")).await.unwrap().unwrap();
    assert_eq!(s(&got, "version_id"), "p1");
    let tmp2 = tempfile::tempdir().unwrap();
    let mut c2 = conn(tmp2.path()).await;
    ps::save_plan(&mut c2, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    let got = ps::latest(&mut c2, None).await.unwrap().unwrap();
    assert_eq!(got.get("evaluation_id"), Some(&PyValue::None));
}

// =============================================================================================
// execution results
// =============================================================================================

#[tokio::test]
async fn a_placed_order_records_submitted_not_filled() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    ps::record_execution(
        &mut c,
        "p1",
        &[res(&[
            ("symbol", "AAA"),
            ("status", "PLACED"),
            ("order_id", "X1"),
        ])],
    )
    .await
    .unwrap();
    let r = row(&mut c, "AAA").await;
    assert_eq!(s(&r, "status"), ps::SUBMITTED);
    assert_eq!(i(&r, "filled_qty"), 0);
    assert_eq!(s(&r, "order_id"), "X1");
}

#[tokio::test]
async fn reconciliation_settles_fills_partials_working_and_lapses_from_the_order_book() {
    for (status, filled, want, want_filled) in [
        ("COMPLETE", 10, ps::FILLED, 10),
        ("COMPLETE", 4, ps::PARTIAL, 4),
        ("OPEN", 0, ps::SUBMITTED, 0),
        ("CANCELLED", 0, ps::LAPSED, 0),
        ("REJECTED", 0, ps::FAILED, 0),
    ] {
        let tmp = tempfile::tempdir().unwrap();
        let mut c = conn(tmp.path()).await;
        ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
            .await
            .unwrap();
        ps::record_execution(
            &mut c,
            "p1",
            &[res(&[
                ("symbol", "AAA"),
                ("status", "PLACED"),
                ("order_id", "X1"),
            ])],
        )
        .await
        .unwrap();
        ps::reconcile_fills(
            &mut c,
            "p1",
            &[broker("AAA", status, filled, Some(101.0), "X1")],
            NOW,
        )
        .await
        .unwrap();
        let r = row(&mut c, "AAA").await;
        assert_eq!(s(&r, "status"), want, "{status} {filled}");
        assert_eq!(i(&r, "filled_qty"), want_filled);
        assert_eq!(s(&r, "reconciled_at"), NOW);
    }
}

#[tokio::test]
async fn an_order_still_working_is_corrected_back_to_submitted() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    c.execute(
        "UPDATE rebalance_orders SET status='FILLED', filled_qty=10, order_id='X1' WHERE symbol='AAA'",
        &[],
    )
    .await
    .unwrap();
    ps::reconcile_fills(&mut c, "p1", &[broker("AAA", "OPEN", 0, None, "X1")], NOW)
        .await
        .unwrap();
    let r = row(&mut c, "AAA").await;
    assert_eq!(s(&r, "status"), ps::SUBMITTED);
    assert_eq!(i(&r, "filled_qty"), 0);
}

#[tokio::test]
async fn reconciliation_reports_what_it_could_not_match() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    ps::record_execution(
        &mut c,
        "p1",
        &[res(&[
            ("symbol", "AAA"),
            ("status", "PLACED"),
            ("order_id", "X1"),
        ])],
    )
    .await
    .unwrap();
    let out = ps::reconcile_fills(&mut c, "p1", &[], NOW).await.unwrap();
    assert_eq!(strs(&out, "unresolved"), ["AAA"]);
}

#[tokio::test]
async fn a_dry_run_fills_nothing_and_a_block_is_not_a_failure() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    ps::record_execution(
        &mut c,
        "p1",
        &[res(&[("symbol", "AAA"), ("status", "DRY_RUN")])],
    )
    .await
    .unwrap();
    let r = row(&mut c, "AAA").await;
    assert_eq!(s(&r, "status"), "DRY_RUN");
    assert_eq!(i(&r, "filled_qty"), 0);
    ps::record_execution(
        &mut c,
        "p1",
        &[
            res(&[("symbol", "AAA"), ("status", "BLOCKED")]),
            res(&[("symbol", "BBB"), ("status", "REJECTED")]),
        ],
    )
    .await
    .unwrap();
    assert_eq!(s(&row(&mut c, "AAA").await, "status"), ps::BLOCKED);
    assert_eq!(s(&row(&mut c, "BBB").await, "status"), ps::FAILED);
}

#[tokio::test]
async fn never_submitted_stays_pending_and_unplanned_is_reported_not_invented() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    let out = ps::record_execution(&mut c, "p1", &[res(&[("symbol", "AAA"), ("status", "OK")])])
        .await
        .unwrap();
    assert_eq!(strs(&out, "not_submitted"), ["BBB"]);
    let out = ps::record_execution(&mut c, "p1", &[res(&[("symbol", "ZZZ"), ("status", "OK")])])
        .await
        .unwrap();
    assert_eq!(strs(&out, "unknown"), ["ZZZ"]);
    assert_eq!(count(&mut c, "rebalance_orders").await, 2);
}

// =============================================================================================
// reconciliation — the number /regime needs
// =============================================================================================

#[tokio::test]
async fn planned_submitted_and_filled_are_separate_facts() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    ps::record_execution(
        &mut c,
        "p1",
        &[
            res(&[("symbol", "AAA"), ("status", "PLACED"), ("order_id", "X1")]),
            res(&[("symbol", "BBB"), ("status", "REJECTED")]),
        ],
    )
    .await
    .unwrap();
    let mid = ps::reconciliation(&mut c, "p1").await.unwrap();
    assert_eq!(i(&mid, "planned_buy_value"), 1000);
    assert_eq!(i(&mid, "planned_sell_value"), 1000);
    assert_eq!(
        i(&mid, "filled_value"),
        0,
        "nothing is filled until the broker says so"
    );
    assert_eq!(strs(&mid, "awaiting_reconciliation"), ["AAA"]);
    assert_eq!(mid.get("fills_confirmed"), Some(&PyValue::Bool(false)));
    ps::reconcile_fills(
        &mut c,
        "p1",
        &[broker("AAA", "COMPLETE", 10, None, "X1")],
        NOW,
    )
    .await
    .unwrap();
    let r = ps::reconciliation(&mut c, "p1").await.unwrap();
    assert_eq!(i(&r, "filled_value"), 1000);
    assert_eq!(i(&r, "failed_value"), 1000);
    assert_eq!(strs(&r, "failed_symbols"), ["BBB"]);
    assert_eq!(r.get("fills_confirmed"), Some(&PyValue::Bool(true)));
}

#[tokio::test]
async fn a_dry_run_reaches_nothing_and_a_refusal_is_not_submitted() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    ps::record_execution(
        &mut c,
        "p1",
        &[
            res(&[("symbol", "AAA"), ("status", "DRY_RUN")]),
            res(&[("symbol", "BBB"), ("status", "DRY_RUN")]),
        ],
    )
    .await
    .unwrap();
    let r = ps::reconciliation(&mut c, "p1").await.unwrap();
    assert_eq!(i(&r, "submitted_value"), 0);
    assert_eq!(i(&r, "simulated_value"), 2000);
    assert_eq!(i(&r, "filled_value"), 0);
    assert_eq!(i(&r, "reached_exchange"), 0);
    ps::record_execution(
        &mut c,
        "p1",
        &[
            res(&[
                ("symbol", "AAA"),
                ("status", "ERROR"),
                ("error", "IP not allowed"),
            ]),
            res(&[
                ("symbol", "BBB"),
                ("status", "ERROR"),
                ("error", "IP not allowed"),
            ]),
        ],
    )
    .await
    .unwrap();
    let r = ps::reconciliation(&mut c, "p1").await.unwrap();
    assert_eq!(
        i(&r, "submitted_value"),
        0,
        "a refusal was counted as submitted"
    );
    assert_eq!(i(&r, "reached_exchange"), 0);
    assert_eq!(i(&r, "failed_value"), 2000);
}

#[tokio::test]
async fn an_order_the_breaker_held_back_is_not_a_failure() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    ps::record_execution(
        &mut c,
        "p1",
        &[
            res(&[("symbol", "AAA"), ("status", "ERROR"), ("error", "x")]),
            res(&[
                ("symbol", "BBB"),
                ("status", "ABORTED"),
                ("error", "batch stopped"),
            ]),
        ],
    )
    .await
    .unwrap();
    let r = ps::reconciliation(&mut c, "p1").await.unwrap();
    assert_eq!(i(&r, "failed_value"), 1000);
    assert_eq!(i(&r, "aborted_value"), 1000);
    assert_eq!(strs(&r, "aborted_symbols"), ["BBB"]);
    assert_eq!(i(&r, "submitted_value"), 0);
}

#[tokio::test]
async fn placed_and_lapsed_orders_count_as_reaching_the_exchange() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    ps::record_execution(
        &mut c,
        "p1",
        &[res(&[
            ("symbol", "AAA"),
            ("status", "PLACED"),
            ("order_id", "X1"),
        ])],
    )
    .await
    .unwrap();
    let r = ps::reconciliation(&mut c, "p1").await.unwrap();
    assert_eq!(i(&r, "submitted_value"), 1000);
    assert_eq!(i(&r, "reached_exchange"), 1);
    ps::reconcile_fills(
        &mut c,
        "p1",
        &[broker("AAA", "CANCELLED", 0, None, "X1")],
        NOW,
    )
    .await
    .unwrap();
    let r = ps::reconciliation(&mut c, "p1").await.unwrap();
    assert_eq!(
        i(&r, "reached_exchange"),
        1,
        "a lapse rested at the exchange"
    );
}

#[tokio::test]
async fn reconciliation_of_an_unknown_plan_is_empty() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    assert!(ps::reconciliation(&mut c, "nope").await.unwrap().is_empty());
    assert!(
        ps::orders_rows(&mut c, Some("nope"))
            .await
            .unwrap()
            .is_empty()
    );
}

// =============================================================================================
// orders a previous session left working
// =============================================================================================

fn ts(y: i32, m: u32, d: u32, h: u32, mi: u32) -> String {
    let t = chrono::Local
        .with_ymd_and_hms(y, m, d, h, mi, 0)
        .single()
        .unwrap()
        .timestamp();
    #[allow(clippy::cast_precision_loss)]
    baskfy_core::py::float_repr(t as f64)
}

async fn submitted_at(c: &mut Conn, created: &str, filled: i64) {
    ps::save_plan(c, &plan("p1"), None, "", 0.0).await.unwrap();
    ps::record_execution(
        c,
        "p1",
        &[res(&[
            ("symbol", "AAA"),
            ("status", "PLACED"),
            ("order_id", "X1"),
        ])],
    )
    .await
    .unwrap();
    c.execute(
        "UPDATE rebalance_versions SET created_ts=? WHERE version_id='p1'",
        &[Param::Text(created.to_owned())],
    )
    .await
    .unwrap();
    ps::reconcile_fills(c, "p1", &[broker("AAA", "OPEN", filled, None, "X1")], NOW)
        .await
        .unwrap();
}

fn aug(d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, d).unwrap()
}

#[tokio::test]
async fn an_order_left_working_by_an_earlier_session_lapses_once() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    submitted_at(&mut c, &ts(2026, 8, 18, 14, 30), 0).await;
    assert_eq!(s(&row(&mut c, "AAA").await, "status"), ps::SUBMITTED);
    let out = ps::lapse_stale_orders(&mut c, aug(19), NOW).await.unwrap();
    assert_eq!(i(&out, "n"), 1);
    assert_eq!(s(&row(&mut c, "AAA").await, "status"), ps::LAPSED);
    let again = ps::lapse_stale_orders(&mut c, aug(19), NOW).await.unwrap();
    assert_eq!(i(&again, "n"), 0, "the sweep is idempotent");
}

#[tokio::test]
async fn a_partial_fill_left_working_is_partial_not_lapsed() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    submitted_at(&mut c, &ts(2026, 8, 18, 14, 30), 4).await;
    ps::lapse_stale_orders(&mut c, aug(19), NOW).await.unwrap();
    assert_eq!(s(&row(&mut c, "AAA").await, "status"), ps::PARTIAL);
}

#[tokio::test]
async fn todays_working_order_is_left_alone() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    submitted_at(&mut c, &ts(2026, 8, 19, 10, 0), 0).await;
    let out = ps::lapse_stale_orders(&mut c, aug(19), NOW).await.unwrap();
    assert_eq!(i(&out, "n"), 0);
    assert_eq!(s(&row(&mut c, "AAA").await, "status"), ps::SUBMITTED);
}

#[tokio::test]
async fn a_settled_status_is_never_reopened_by_the_sweep() {
    let tmp = tempfile::tempdir().unwrap();
    let mut c = conn(tmp.path()).await;
    ps::save_plan(&mut c, &plan("p1"), None, "", 0.0)
        .await
        .unwrap();
    ps::record_execution(
        &mut c,
        "p1",
        &[res(&[
            ("symbol", "AAA"),
            ("status", "PLACED"),
            ("order_id", "X1"),
        ])],
    )
    .await
    .unwrap();
    c.execute(
        "UPDATE rebalance_versions SET created_ts=? WHERE version_id='p1'",
        &[Param::Text(ts(2026, 8, 18, 14, 30))],
    )
    .await
    .unwrap();
    ps::reconcile_fills(
        &mut c,
        "p1",
        &[broker("AAA", "COMPLETE", 10, None, "X1")],
        NOW,
    )
    .await
    .unwrap();
    ps::lapse_stale_orders(&mut c, aug(19), NOW).await.unwrap();
    assert_eq!(s(&row(&mut c, "AAA").await, "status"), ps::FILLED);
    assert!(ps::open_plans(&mut c, 5).await.unwrap().is_empty());
}

// =============================================================================================
// the weekly book's confirm gate (non-negotiable 1)
// =============================================================================================

#[test]
fn the_weekly_execute_gate_is_confirm_then_plan_then_thirty_minutes() {
    let p = Plan {
        plan_id: "abc".to_owned(),
        created_at: 1000.0,
        body: PyDict::new(),
    };
    for word in ["yes", "True", "1", "", "TRUE", "true "] {
        assert_eq!(
            check_execute(word, Some(&p), 1000.0).unwrap_err().status,
            400
        );
    }
    assert_eq!(check_execute("true", None, 1000.0).unwrap_err().status, 404);
    assert!(check_execute("true", Some(&p), 1000.0 + 1800.0).is_ok());
    assert_eq!(
        check_execute("true", Some(&p), 1000.0 + 1801.0)
            .unwrap_err()
            .status,
        410
    );
}

#[test]
fn the_weekly_book_has_no_auto_execute_flag_and_no_other_order_path() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes/weekly");
    for name in [
        "analyze.rs",
        "plan_store.rs",
        "strategy.rs",
        "basket.rs",
        "score.rs",
    ] {
        let text = std::fs::read_to_string(root.join(name)).unwrap();
        let lower = text.to_lowercase();
        assert!(
            !lower.contains("weekly_auto"),
            "{name} names a weekly auto flag"
        );
        assert!(
            !text.contains("KiteWrites"),
            "{name} reaches the order verbs"
        );
        assert!(
            !text.contains(".place_order("),
            "{name} places around the gateway"
        );
    }
    let analyze = std::fs::read_to_string(root.join("analyze.rs")).unwrap();
    assert_eq!(
        analyze.matches(".gateway.place(").count(),
        1,
        "exactly one order call, inside POST /execute"
    );
    assert!(analyze.contains("client_id: Some(format!(\"{plan_id}:{}\", o.symbol))"));
    assert!(analyze.contains("check_execute(confirm"));
}

#[test]
fn the_planner_uses_the_real_guard() {
    use baskfy_desk::lanes::weekly::basket::tradeable;
    for s in ["SGBDE31III", "sgbdec25", " SGBJUN31 "] {
        assert!(!tradeable(s), "{s}");
    }
    assert!(tradeable("RELIANCE"));
}
