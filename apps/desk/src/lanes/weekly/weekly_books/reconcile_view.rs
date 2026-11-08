//! `app/analytics/reconcile_view.py` — plan against broker: orders, stops and positions, side by
//! side, with the **broker as the authority**. Read-only: it compares, it does not repair (the
//! source carries no write verb and no `UPDATE`/`INSERT` — pinned by a test below).
//!
//! The stops section is `analytics/protection.py`'s `review(holdings, gtts)`, which lane
//! `analytics` owns; [`build`] takes that review's result as `stops` (a dict with `healthy` and
//! `findings`) instead of importing it.

use super::{db_to_py, pyi, pys};
use crate::storage::{Conn, StorageError};
use crate::wire::{PyDict, PyValue, dict};

/// Agreement.
pub const OK: &str = "ok";
/// The local record lags the broker; nothing is broken.
pub const DRIFT: &str = "drift";
/// The local record and the broker disagree.
pub const WRONG: &str = "wrong";

/// `plan_store.PENDING`.
const PENDING: &str = "PENDING";
/// `plan_store.SUBMITTED`.
const SUBMITTED: &str = "SUBMITTED";

fn state_of(row: &PyDict) -> String {
    pys(row.get("state"))
}

fn sev(rows: &[PyDict]) -> &'static str {
    if rows.iter().any(|r| state_of(r) == WRONG) {
        return WRONG;
    }
    if rows.iter().any(|r| state_of(r) == DRIFT) {
        DRIFT
    } else {
        OK
    }
}

fn section(rows: Vec<PyDict>) -> PyDict {
    let state = if rows.is_empty() { OK } else { sev(&rows) };
    dict([
        (
            "rows",
            PyValue::List(rows.into_iter().map(PyValue::Map).collect()),
        ),
        ("state", PyValue::Str(state.to_owned())),
    ])
}

/// `orders_view(conn, plan_id, broker_orders)`: what the plan believes each order did, against
/// the order book. A `PENDING` row is never symbol-matched to another plan's order.
///
/// # Errors
/// The driver's.
pub async fn orders_view(
    conn: &mut Conn,
    plan_id: &str,
    broker_orders: &[PyDict],
) -> Result<PyDict, StorageError> {
    let mut by_id: Vec<(String, &PyDict)> = Vec::new();
    let mut by_sym: Vec<(String, Vec<&PyDict>)> = Vec::new();
    for o in broker_orders {
        let oid = pys(o.get("order_id"));
        if !oid.is_empty() {
            let key = o.get("order_id").map_or_else(String::new, PyValue::py_str);
            if let Some(slot) = by_id.iter_mut().find(|(k, _)| *k == key) {
                slot.1 = o;
            } else {
                by_id.push((key, o));
            }
        }
        let sym = pys(o.get("tradingsymbol"));
        if let Some(slot) = by_sym.iter_mut().find(|(k, _)| *k == sym) {
            slot.1.push(o);
        } else {
            by_sym.push((sym, vec![o]));
        }
    }
    let cur = conn
        .execute(
            "SELECT * FROM rebalance_orders WHERE version_id=?",
            &[plan_id.into()],
        )
        .await?;
    let mut rows = Vec::new();
    for raw in &cur.rows {
        let r: PyDict = dict(raw.0.iter().map(|(k, v)| (k.clone(), db_to_py(v))));
        let status = pys(r.get("status"));
        let symbol = pys(r.get("symbol"));
        let order_key = pys(r.get("order_id"));
        let mut o = by_id.iter().find(|(k, _)| *k == order_key).map(|(_, o)| *o);
        if o.is_none() && status != PENDING {
            o = by_sym
                .iter()
                .find(|(k, _)| *k == symbol)
                .and_then(|(_, c)| if c.len() == 1 { Some(c[0]) } else { None });
        }
        let broker_status = o.map_or_else(String::new, |o| pys(o.get("status")));
        let broker_filled = o.map_or(0, |o| pyi(o.get("filled_quantity")));
        let local_filled = pyi(r.get("filled_qty"));
        let (state, why) = if status == PENDING && o.is_none() {
            (OK, "never submitted".to_owned())
        } else if o.is_none() {
            (
                WRONG,
                "recorded as sent, but no such order at the broker".to_owned(),
            )
        } else if local_filled != broker_filled {
            (
                WRONG,
                format!("local says {local_filled} filled, broker says {broker_filled}"),
            )
        } else if status == SUBMITTED
            && matches!(
                broker_status.as_str(),
                "COMPLETE" | "REJECTED" | "CANCELLED"
            )
        {
            (
                DRIFT,
                format!("settled at the broker ({broker_status}); not reconciled yet"),
            )
        } else {
            (
                OK,
                if broker_status.is_empty() {
                    status.clone()
                } else {
                    broker_status.clone()
                },
            )
        };
        rows.push(dict([
            ("symbol", r.get("symbol").cloned().unwrap_or(PyValue::None)),
            ("side", r.get("side").cloned().unwrap_or(PyValue::None)),
            (
                "planned",
                r.get("planned_qty").cloned().unwrap_or(PyValue::None),
            ),
            (
                "local_status",
                r.get("status").cloned().unwrap_or(PyValue::None),
            ),
            ("local_filled", PyValue::Int(local_filled)),
            ("broker_status", PyValue::Str(broker_status)),
            ("broker_filled", PyValue::Int(broker_filled)),
            ("state", PyValue::Str(state.to_owned())),
            ("why", PyValue::Str(why)),
        ]));
    }
    rows.sort_by(|a, b| {
        let key = |r: &PyDict| {
            let s = state_of(r);
            (s != WRONG, s != DRIFT, pys(r.get("symbol")))
        };
        key(a).cmp(&key(b))
    });
    Ok(section(rows))
}

/// `positions_view(plan, holdings)`: where the book landed against where the plan aimed. An exact
/// match is not reported; a holding outside the plan is drift, not an error.
#[must_use]
pub fn positions_view(plan: Option<&PyDict>, holdings: &[PyDict]) -> PyDict {
    let mut held: Vec<(String, i64)> = Vec::new();
    for h in holdings {
        let sym = h.get("symbol").map_or_else(String::new, PyValue::py_str);
        let q = pyi(h.get("quantity"));
        if let Some(slot) = held.iter_mut().find(|(s, _)| *s == sym) {
            slot.1 = q;
        } else {
            held.push((sym, q));
        }
    }
    let mut rows = Vec::new();
    let orders = match plan.and_then(|p| p.get("orders")) {
        Some(PyValue::List(l)) => l.clone(),
        _ => Vec::new(),
    };
    for o in &orders {
        let PyValue::Map(o) = o else { continue };
        let want = pyi(o.get("qty_final"));
        let sym = o.get("symbol").map_or_else(String::new, PyValue::py_str);
        let have = match held.iter().position(|(s, _)| *s == sym) {
            Some(i) => held.remove(i).1,
            None => 0,
        };
        if want == have {
            continue;
        }
        rows.push(dict([
            ("symbol", PyValue::Str(sym)),
            ("planned_final", PyValue::Int(want)),
            ("held", PyValue::Int(have)),
            ("diff", PyValue::Int(have - want)),
            ("state", PyValue::Str(WRONG.to_owned())),
            (
                "why",
                PyValue::Str(
                    if have < want {
                        "target not reached"
                    } else {
                        "more than planned"
                    }
                    .to_owned(),
                ),
            ),
        ]));
    }
    for (sym, q) in held {
        rows.push(dict([
            ("symbol", PyValue::Str(sym)),
            ("planned_final", PyValue::None),
            ("held", PyValue::Int(q)),
            ("diff", PyValue::Int(q)),
            ("state", PyValue::Str(DRIFT.to_owned())),
            ("why", PyValue::Str("held but not in this plan".to_owned())),
        ]));
    }
    rows.sort_by_key(|r| std::cmp::Reverse(pyi(r.get("diff")).abs()));
    section(rows)
}

fn count_state(section: &PyDict, state: &str) -> i64 {
    match section.get("rows") {
        Some(PyValue::List(rows)) => i64::try_from(
            rows.iter()
                .filter(|r| matches!(r, PyValue::Map(m) if state_of(m) == state))
                .count(),
        )
        .unwrap_or(0),
        _ => 0,
    }
}

/// `build(conn, plan_id=, plan=, broker_orders=, gtts=, holdings=)` with the protection review
/// passed in as `stops` (see the module docs): three sections, the worst sets the headline.
///
/// # Errors
/// The driver's.
pub async fn build(
    conn: &mut Conn,
    plan_id: Option<&str>,
    plan: Option<&PyDict>,
    broker_orders: &[PyDict],
    stops: &PyDict,
    holdings: &[PyDict],
) -> Result<PyDict, StorageError> {
    let orders = match plan_id {
        Some(pid) if !pid.is_empty() => orders_view(conn, pid, broker_orders).await?,
        _ => section(Vec::new()),
    };
    let positions = positions_view(plan, holdings);
    let healthy = stops.get("healthy").is_some_and(PyValue::truthy);
    let sections = [
        ("orders", pys(orders.get("state"))),
        ("stops", if healthy { OK } else { WRONG }.to_owned()),
        ("positions", pys(positions.get("state"))),
    ];
    let worst = if sections.iter().any(|(_, s)| s == WRONG) {
        WRONG
    } else if sections.iter().any(|(_, s)| s == DRIFT) {
        DRIFT
    } else {
        OK
    };
    let findings = match stops.get("findings") {
        Some(PyValue::List(l)) => i64::try_from(l.len()).unwrap_or(0),
        _ => 0,
    };
    let counts = dict([
        ("orders_wrong", PyValue::Int(count_state(&orders, WRONG))),
        ("orders_drift", PyValue::Int(count_state(&orders, DRIFT))),
        (
            "positions_off",
            PyValue::Int(count_state(&positions, WRONG)),
        ),
        ("stop_findings", PyValue::Int(findings)),
    ]);
    Ok(dict([
        (
            "plan_id",
            plan_id.map_or(PyValue::None, |p| PyValue::Str(p.to_owned())),
        ),
        ("state", PyValue::Str(worst.to_owned())),
        (
            "sections",
            PyValue::Map(dict(
                sections.into_iter().map(|(k, v)| (k, PyValue::Str(v))),
            )),
        ),
        ("orders", PyValue::Map(orders)),
        ("positions", PyValue::Map(positions)),
        ("stops", PyValue::Map(stops.clone())),
        ("counts", PyValue::Map(counts)),
    ]))
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
mod tests {
    //! `tests/test_reconcile_view.py`, ported. `plan_store.save_plan` / `record_execution` /
    //! `reconcile_fills` are the weekly plan store's; the rows they leave are seeded here with the
    //! same columns, and the protection review is passed in as its result.
    use super::*;
    use crate::storage::Db;

    async fn conn(dir: &tempfile::TempDir) -> Conn {
        let mut c = Db::connect_sqlite_path(&dir.path().join("p.db"))
            .await
            .unwrap();
        c.migrate().await.unwrap();
        c
    }

    /// `PS.save_plan(conn, plan())`: the version row and one PENDING order row.
    async fn save_plan(c: &mut Conn) {
        let cols = c
            .execute("PRAGMA table_info(rebalance_versions)", &[])
            .await
            .unwrap();
        let names: Vec<String> = cols
            .rows
            .iter()
            .map(|r| r.get("name").unwrap().as_str().unwrap().to_owned())
            .collect();
        assert!(names.contains(&"version_id".to_owned()));
        let notnull: Vec<String> = cols
            .rows
            .iter()
            .filter(|r| {
                r.get("notnull") == Some(&crate::storage::DbValue::Int(1))
                    && r.get("dflt_value")
                        .is_some_and(crate::storage::DbValue::is_null)
            })
            .map(|r| r.get("name").unwrap().as_str().unwrap().to_owned())
            .filter(|n| n != "version_id")
            .collect();
        let mut sql_cols = vec!["version_id".to_owned()];
        let mut params: Vec<crate::storage::Param> = vec!["p1".into()];
        for n in notnull {
            sql_cols.push(n);
            params.push("x".into());
        }
        let sql = format!(
            "INSERT INTO rebalance_versions({}) VALUES({})",
            sql_cols.join(","),
            vec!["?"; sql_cols.len()].join(",")
        );
        c.execute(&sql, &params).await.unwrap();
        c.execute(
            "INSERT INTO rebalance_orders(version_id, symbol, side, planned_qty, planned_ref_price) VALUES('p1','AAA','BUY',10,100.0)",
            &[],
        ).await.unwrap();
    }

    fn plan() -> PyDict {
        dict([
            ("plan_id", PyValue::Str("p1".into())),
            (
                "orders",
                PyValue::List(vec![PyValue::Map(dict([
                    ("symbol", PyValue::Str("AAA".into())),
                    ("side", PyValue::Str("BUY".into())),
                    ("delta", PyValue::Int(10)),
                    ("qty_final", PyValue::Int(10)),
                    ("ref_price", PyValue::Float(100.0)),
                ]))]),
            ),
        ])
    }

    fn bo(sym: &str, status: &str, filled: i64, oid: &str) -> PyDict {
        dict([
            ("tradingsymbol", PyValue::Str(sym.into())),
            ("status", PyValue::Str(status.into())),
            ("filled_quantity", PyValue::Int(filled)),
            ("order_id", PyValue::Str(oid.into())),
            ("average_price", PyValue::Float(100.0)),
        ])
    }

    fn first_row(v: &PyDict) -> PyDict {
        let PyValue::List(rows) = v.get("rows").unwrap() else {
            panic!()
        };
        let PyValue::Map(m) = &rows[0] else { panic!() };
        m.clone()
    }

    fn holding(sym: &str, q: i64) -> PyDict {
        dict([
            ("symbol", PyValue::Str(sym.into())),
            ("quantity", PyValue::Int(q)),
        ])
    }

    #[tokio::test]
    async fn a_local_fill_the_broker_does_not_confirm_is_wrong() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        save_plan(&mut c).await;
        c.execute("UPDATE rebalance_orders SET status='FILLED', filled_qty=10, order_id='X1' WHERE symbol='AAA'", &[]).await.unwrap();
        let v = orders_view(&mut c, "p1", &[bo("AAA", "OPEN", 0, "X1")])
            .await
            .unwrap();
        let row = first_row(&v);
        assert_eq!(pys(row.get("state")), WRONG);
        assert!(pys(row.get("why")).contains("local says 10 filled, broker says 0"));
    }

    #[tokio::test]
    async fn an_order_the_broker_has_never_heard_of_is_wrong() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        save_plan(&mut c).await;
        c.execute(
            "UPDATE rebalance_orders SET status='SUBMITTED', order_id='X9'",
            &[],
        )
        .await
        .unwrap();
        let row = first_row(&orders_view(&mut c, "p1", &[]).await.unwrap());
        assert_eq!(pys(row.get("state")), WRONG);
        assert!(pys(row.get("why")).contains("no such order at the broker"));
    }

    #[tokio::test]
    async fn a_settled_order_not_yet_reconciled_is_drift_not_wrong() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        save_plan(&mut c).await;
        c.execute("UPDATE rebalance_orders SET status='SUBMITTED', filled_qty=10, order_id='X1' WHERE symbol='AAA'", &[]).await.unwrap();
        let row = first_row(
            &orders_view(&mut c, "p1", &[bo("AAA", "COMPLETE", 10, "X1")])
                .await
                .unwrap(),
        );
        assert_eq!(pys(row.get("state")), DRIFT);
    }

    #[tokio::test]
    async fn agreement_reads_ok() {
        // record_execution + reconcile_fills leave: FILLED, filled 10, order X1.
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        save_plan(&mut c).await;
        c.execute("UPDATE rebalance_orders SET status='FILLED', filled_qty=10, order_id='X1' WHERE symbol='AAA'", &[]).await.unwrap();
        let v = orders_view(&mut c, "p1", &[bo("AAA", "COMPLETE", 10, "X1")])
            .await
            .unwrap();
        assert_eq!(pys(v.get("state")), OK);
    }

    #[tokio::test]
    async fn an_unsubmitted_order_is_not_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        save_plan(&mut c).await;
        assert_eq!(
            pys(first_row(&orders_view(&mut c, "p1", &[]).await.unwrap()).get("state")),
            OK
        );
    }

    #[test]
    fn a_position_short_of_its_target_is_reported() {
        let row = first_row(&positions_view(Some(&plan()), &[holding("AAA", 3)]));
        assert_eq!(pyi(row.get("diff")), -7);
        assert_eq!(pys(row.get("state")), WRONG);
    }

    #[test]
    fn a_holding_outside_the_plan_is_drift_not_an_error() {
        let v = positions_view(
            Some(&plan()),
            &[holding("AAA", 10), holding("SGBDE31III", 392)],
        );
        let PyValue::List(rows) = v.get("rows").unwrap() else {
            panic!()
        };
        assert_eq!(rows.len(), 1);
        let PyValue::Map(r) = &rows[0] else { panic!() };
        assert_eq!(pys(r.get("symbol")), "SGBDE31III");
        assert_eq!(pys(r.get("state")), DRIFT);
    }

    #[test]
    fn a_book_matching_its_plan_reports_nothing() {
        let v = positions_view(Some(&plan()), &[holding("AAA", 10)]);
        assert_eq!(v.get("rows"), Some(&PyValue::List(Vec::new())));
    }

    fn stops(healthy: bool, findings: usize) -> PyDict {
        dict([
            ("healthy", PyValue::Bool(healthy)),
            ("findings", PyValue::List(vec![PyValue::None; findings])),
        ])
    }

    #[tokio::test]
    async fn the_worst_section_sets_the_headline() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        save_plan(&mut c).await;
        c.execute("UPDATE rebalance_orders SET status='FILLED', filled_qty=10, order_id='X1' WHERE symbol='AAA'", &[]).await.unwrap();
        let v = build(
            &mut c,
            Some("p1"),
            Some(&plan()),
            &[bo("AAA", "OPEN", 0, "X1")],
            &stops(true, 0),
            &[holding("AAA", 10)],
        )
        .await
        .unwrap();
        assert_eq!(pys(v.get("state")), WRONG);
        let PyValue::Map(s) = v.get("sections").unwrap() else {
            panic!()
        };
        assert_eq!(pys(s.get("orders")), WRONG);
    }

    #[tokio::test]
    async fn an_unprotected_book_makes_the_stops_section_wrong() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        let v = build(
            &mut c,
            None,
            None,
            &[],
            &stops(false, 1),
            &[holding("AAA", 10)],
        )
        .await
        .unwrap();
        let PyValue::Map(s) = v.get("sections").unwrap() else {
            panic!()
        };
        assert_eq!(pys(s.get("stops")), WRONG);
    }

    #[test]
    fn the_view_only_reads() {
        let src = include_str!("reconcile_view.rs");
        let body = src.split("#[cfg(test)]").next().unwrap();
        for token in [
            "place_order",
            "place_gtt",
            "delete_gtt",
            "UPDATE ",
            "INSERT ",
            concat!("Kite", "Writes"),
        ] {
            assert!(!body.contains(token), "{token}");
        }
    }

    #[tokio::test]
    async fn a_pending_row_is_not_matched_to_another_plans_order() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = conn(&dir).await;
        save_plan(&mut c).await;
        let row = first_row(
            &orders_view(&mut c, "p1", &[bo("AAA", "COMPLETE", 10, "OTHER")])
                .await
                .unwrap(),
        );
        assert_eq!(pys(row.get("state")), OK);
        assert_eq!(pys(row.get("why")), "never submitted");
    }
}
