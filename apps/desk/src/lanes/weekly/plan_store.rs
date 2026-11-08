//! `app/analytics/plan_store.py`: the weekly plan and its orders, written once at `/analyze` and
//! updated in place by `/execute` and by the broker's order book.
//!
//! Planned, submitted and filled are **different facts** and stay different here: a placed
//! order is `SUBMITTED` (never `FILLED`), a dry run is `DRY_RUN` (never submitted), an order the
//! circuit breaker held back is `ABORTED` (never failed). Graded on
//! `fixtures/weekly/plan_store.json` (dumped from the live Python) and by the spec tests ported
//! from `tests/test_plan_store.py` (`tests/weekly_plan.rs`).
//!
//! SQL is the Python's, SQLite dialect with `?`, run through [`Conn`] on either backend.

use std::collections::BTreeSet;

use chrono::{NaiveDate, TimeZone};

use super::pandas::py_round_int;
use crate::lanes::analytics::web::{db_to_py, row_dict};
use crate::lanes::exec_core::journal::dumps_allow_nan;
use crate::storage::{Conn, DbValue, Param, Row, StorageError};
use crate::wire::{PyDict, PyValue, dict};

/// An order that has not been sent.
pub const PENDING: &str = "PENDING";
/// Reached the exchange; nothing yet known about trading.
pub const SUBMITTED: &str = "SUBMITTED";
/// The broker's order book says it traded in full.
pub const FILLED: &str = "FILLED";
/// Traded part of its quantity.
pub const PARTIAL: &str = "PARTIAL";
/// Refused (by the broker, or an error).
pub const FAILED: &str = "FAILED";
/// Refused by a guard.
pub const BLOCKED: &str = "BLOCKED";
/// The gateway's idempotency answer.
pub const DUPLICATE: &str = "DUPLICATE";
/// A dry run (`SIMULATED` in Python, stored as `"DRY_RUN"`).
pub const SIMULATED: &str = "DRY_RUN";
/// A day order that rested and expired unfilled.
pub const LAPSED: &str = "LAPSED";
/// Held back by the circuit breaker; never sent.
pub const ABORTED: &str = "ABORTED";

const OK: [&str; 2] = ["OK", "PLACED"];
const AT_EXCHANGE: [&str; 4] = [SUBMITTED, FILLED, PARTIAL, LAPSED];
const REFUSED: [&str; 2] = [FAILED, BLOCKED];

/// A Python value as the parameter sqlite3/psycopg would bind.
#[must_use]
pub fn param_of(v: &PyValue) -> Param {
    match v {
        PyValue::None => Param::Null,
        PyValue::Bool(b) => Param::Bool(*b),
        PyValue::Int(i) => Param::Int(*i),
        PyValue::Float(f) => Param::Float(*f),
        PyValue::Str(s) => Param::Text(s.clone()),
        other => Param::Text(format!("{other:?}")),
    }
}

/// `float(x)` for a Python number (`None` for anything else).
#[allow(clippy::cast_precision_loss)]
fn as_f64(v: &PyValue) -> Option<f64> {
    match v {
        PyValue::Float(f) => Some(*f),
        PyValue::Int(i) => Some(*i as f64),
        PyValue::Bool(b) => Some(f64::from(u8::from(*b))),
        _ => None,
    }
}

/// `int(x)` for a Python number (truncation, as `int(float)`).
#[allow(clippy::cast_possible_truncation)]
fn as_int(v: &PyValue) -> Option<i64> {
    match v {
        PyValue::Int(i) => Some(*i),
        PyValue::Bool(b) => Some(i64::from(*b)),
        PyValue::Float(f) if f.is_finite() => Some(f.trunc() as i64),
        _ => None,
    }
}

/// `str(value)` for the scalars a plan or a result carries.
#[must_use]
pub fn py_str(v: &PyValue) -> String {
    match v {
        PyValue::None => "None".to_owned(),
        PyValue::Bool(true) => "True".to_owned(),
        PyValue::Bool(false) => "False".to_owned(),
        PyValue::Int(i) => i.to_string(),
        PyValue::Float(f) => baskfy_core::py::float_repr(*f),
        PyValue::Str(s) => s.clone(),
        other => format!("{other:?}"),
    }
}

fn get<'a>(d: &'a PyDict, k: &str) -> &'a PyValue {
    static NONE: PyValue = PyValue::None;
    d.get(k).unwrap_or(&NONE)
}

fn sym_of(v: &PyValue) -> String {
    match v {
        PyValue::Str(s) => s.clone(),
        other => format!("{other:?}"),
    }
}

fn db_int(v: Option<&DbValue>) -> i64 {
    #[allow(clippy::cast_possible_truncation)]
    match v {
        Some(DbValue::Int(i)) => *i,
        Some(DbValue::Float(f)) if f.is_finite() => f.trunc() as i64,
        Some(DbValue::Bool(b)) => i64::from(*b),
        _ => 0,
    }
}

fn db_f64(v: Option<&DbValue>) -> f64 {
    match v {
        Some(DbValue::Text(s)) => s.trim().parse().unwrap_or(0.0),
        Some(v) => v.as_f64().unwrap_or(0.0),
        None => 0.0,
    }
}

fn db_str(row: &Row, k: &str) -> String {
    match row.get(k) {
        Some(DbValue::Text(s)) => s.clone(),
        Some(DbValue::Null) | None => String::new(),
        Some(other) => format!("{other:?}"),
    }
}

fn str_list(items: impl IntoIterator<Item = String>) -> PyValue {
    PyValue::List(items.into_iter().map(PyValue::Str).collect())
}

/// `save_plan(conn, plan, *, evaluation_id=None, note="")`: store a plan and its orders
/// (idempotent by `plan_id`); returns the version id. `now` is `time.time()`, used only when
/// the plan has no `created_at`.
///
/// # Errors
/// The driver's (the transaction rolls back).
pub async fn save_plan(
    conn: &mut Conn,
    plan: &PyDict,
    evaluation_id: Option<&str>,
    note: &str,
    now: f64,
) -> Result<String, StorageError> {
    let pid = match get(plan, "plan_id") {
        PyValue::Str(s) => s.clone(),
        other => py_str(other),
    };
    let all: Vec<PyDict> = match get(plan, "orders") {
        PyValue::List(rows) => rows
            .iter()
            .filter_map(|o| match o {
                PyValue::Map(m) => Some(m.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    };
    let mut weights = PyDict::new();
    let mut constituents: BTreeSet<String> = BTreeSet::new();
    for o in &all {
        let s = sym_of(get(o, "symbol"));
        weights.insert(s.clone(), get(o, "weight").clone());
        constituents.insert(s);
    }
    let created = as_f64(get(plan, "created_at"))
        .filter(|v| *v != 0.0)
        .unwrap_or(now);
    let constituents_json = dumps_allow_nan(&str_list(constituents));
    let weights_json = dumps_allow_nan(&PyValue::Map(weights));
    let rows: Vec<Vec<Param>> = all
        .iter()
        .filter(|o| get(o, "delta").truthy())
        .map(|o| {
            let delta = as_int(get(o, "delta")).unwrap_or(0);
            let price = if get(o, "ref_price").truthy() {
                as_f64(get(o, "ref_price")).unwrap_or(0.0)
            } else {
                0.0
            };
            vec![
                Param::Text(pid.clone()),
                param_of(get(o, "symbol")),
                Param::Text(if delta > 0 { "BUY" } else { "SELL" }.to_owned()),
                Param::Int(delta.abs()),
                Param::Float(price),
                Param::Text(PENDING.to_owned()),
            ]
        })
        .collect();
    let version = vec![
        Param::Text(pid.clone()),
        Param::Float(created),
        Param::Text(constituents_json),
        Param::Text(weights_json),
        Param::Text(note.to_owned()),
        evaluation_id.map_or(Param::Null, |e| Param::Text(e.to_owned())),
    ];
    let pid_param = Param::Text(pid.clone());
    conn.transaction(move |c| {
        Box::pin(async move {
            c.execute(
                "DELETE FROM rebalance_orders WHERE version_id=?",
                std::slice::from_ref(&pid_param),
            )
            .await?;
            c.execute(
                "INSERT INTO rebalance_versions(version_id, created_ts, constituents_json, \
                 weights_json, note, evaluation_id) VALUES(?,?,?,?,?,?) ON CONFLICT(version_id) \
                 DO UPDATE SET created_ts=excluded.created_ts, \
                 constituents_json=excluded.constituents_json, \
                 weights_json=excluded.weights_json, note=excluded.note, \
                 evaluation_id=excluded.evaluation_id",
                &version,
            )
            .await?;
            c.executemany(
                "INSERT INTO rebalance_orders(version_id, symbol, side, planned_qty, \
                 planned_ref_price, filled_qty, avg_fill_price, status) \
                 VALUES(?,?,?,?,?,0,NULL,?)",
                &rows,
            )
            .await?;
            Ok(())
        })
    })
    .await?;
    Ok(pid)
}

async fn orders_of(conn: &mut Conn, plan_id: &str) -> Result<Vec<Row>, StorageError> {
    Ok(conn
        .execute(
            "SELECT * FROM rebalance_orders WHERE version_id=?",
            &[Param::Text(plan_id.to_owned())],
        )
        .await?
        .rows)
}

/// `record_execution(conn, plan_id, results)`: fold `/execute`'s results onto the stored
/// orders. A result for an unplanned symbol is reported in `unknown`, never invented.
///
/// # Errors
/// The driver's.
pub async fn record_execution(
    conn: &mut Conn,
    plan_id: &str,
    results: &[PyDict],
) -> Result<PyDict, StorageError> {
    // `{r["symbol"]: r for r in ...}`: first position, last row.
    let mut rows: Vec<(String, Row)> = Vec::new();
    for r in orders_of(conn, plan_id).await? {
        let s = db_str(&r, "symbol");
        match rows.iter_mut().find(|(k, _)| *k == s) {
            Some(slot) => slot.1 = r,
            None => rows.push((s, r)),
        }
    }
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut unknown: Vec<PyValue> = Vec::new();
    let mut updates: Vec<Vec<Param>> = Vec::new();
    for res in results {
        let sym_v = get(res, "symbol");
        let sym = match sym_v {
            PyValue::Str(s) => s.clone(),
            _ => {
                unknown.push(sym_v.clone());
                continue;
            }
        };
        let Some((_, row)) = rows.iter().find(|(k, _)| *k == sym) else {
            unknown.push(sym_v.clone());
            continue;
        };
        seen.insert(sym.clone());
        let status_v = get(res, "status");
        let raw = if status_v.truthy() {
            py_str(status_v).to_uppercase()
        } else {
            String::new()
        };
        let (status, filled) = if OK.contains(&raw.as_str()) {
            (SUBMITTED, 0)
        } else if raw.starts_with("DRY") {
            (SIMULATED, 0)
        } else if raw == "DUPLICATE" {
            (DUPLICATE, db_int(row.get("filled_qty")))
        } else if raw == "REJECTED" {
            (FAILED, 0)
        } else if raw == "BLOCKED" {
            (BLOCKED, 0)
        } else if raw == "ABORTED" {
            (ABORTED, 0)
        } else {
            (FAILED, 0)
        };
        let price = if get(res, "avg_price").truthy() {
            get(res, "avg_price")
        } else {
            get(res, "price")
        };
        updates.push(vec![
            Param::Text(status.to_owned()),
            Param::Int(filled),
            param_of(price),
            param_of(get(res, "order_id")),
            Param::Text(plan_id.to_owned()),
            Param::Text(sym),
        ]);
    }
    conn.transaction(move |c| {
        Box::pin(async move {
            c.executemany(
                "UPDATE rebalance_orders SET status=?, filled_qty=?, avg_fill_price=?, \
                 order_id=? WHERE version_id=? AND symbol=?",
                &updates,
            )
            .await?;
            Ok(())
        })
    })
    .await?;
    let not_submitted: BTreeSet<String> = rows
        .iter()
        .map(|(k, _)| k.clone())
        .filter(|k| !seen.contains(k))
        .collect();
    Ok(dict([
        (
            "updated",
            PyValue::Int(i64::try_from(seen.len()).unwrap_or(i64::MAX)),
        ),
        ("unknown", PyValue::List(unknown)),
        ("not_submitted", str_list(not_submitted)),
    ]))
}

/// `round(sum(...))` of a Python float sum (an empty sum is the int 0).
fn round_sum(values: impl IntoIterator<Item = f64>) -> PyValue {
    let mut any = false;
    let mut total = 0.0_f64;
    for v in values {
        any = true;
        total += v;
    }
    if !any {
        return PyValue::Int(0);
    }
    PyValue::Int(py_round_int(total).unwrap_or(0))
}

/// `reconciliation(conn, plan_id)`: planned against submitted against filled, in rupees;
/// `{}` for an unknown plan.
///
/// # Errors
/// The driver's.
pub async fn reconciliation(conn: &mut Conn, plan_id: &str) -> Result<PyDict, StorageError> {
    let rows = orders_of(conn, plan_id).await?;
    if rows.is_empty() {
        return Ok(PyDict::new());
    }
    #[allow(clippy::cast_precision_loss)]
    let value = |r: &Row, key: &str| -> f64 {
        db_int(r.get(key)).abs() as f64 * db_f64(r.get("planned_ref_price"))
    };
    let status = |r: &Row| db_str(r, "status");
    let side = |r: &Row| db_str(r, "side");
    let of = |pred: &dyn Fn(&str) -> bool| -> Vec<&Row> {
        rows.iter().filter(|r| pred(&status(r))).collect()
    };
    let sells: Vec<&Row> = rows.iter().filter(|r| side(r) == "SELL").collect();
    let buys: Vec<&Row> = rows.iter().filter(|r| side(r) == "BUY").collect();
    let submitted = of(&|s| AT_EXCHANGE.contains(&s));
    let filled = of(&|s| s == FILLED || s == PARTIAL);
    let failed = of(&|s| REFUSED.contains(&s));
    let aborted = of(&|s| s == ABORTED);
    let simulated = of(&|s| s == SIMULATED);
    let working = of(&|s| s == SUBMITTED);
    let pending = of(&|s| s == PENDING);
    let syms = |v: &[&Row]| str_list(v.iter().map(|r| db_str(r, "symbol")));
    let statuses: BTreeSet<String> = rows.iter().map(status).collect();
    let mut counts = PyDict::new();
    for s in &statuses {
        let n = rows.iter().filter(|r| status(r) == *s).count();
        counts.insert(
            s.clone(),
            PyValue::Int(i64::try_from(n).unwrap_or(i64::MAX)),
        );
    }
    let n = |v: usize| PyValue::Int(i64::try_from(v).unwrap_or(i64::MAX));
    Ok(dict([
        ("plan_id", PyValue::Str(plan_id.to_owned())),
        ("orders", n(rows.len())),
        (
            "planned_sell_value",
            round_sum(sells.iter().map(|r| value(r, "planned_qty"))),
        ),
        (
            "planned_buy_value",
            round_sum(buys.iter().map(|r| value(r, "planned_qty"))),
        ),
        (
            "submitted_value",
            round_sum(submitted.iter().map(|r| value(r, "planned_qty"))),
        ),
        (
            "filled_value",
            round_sum(filled.iter().map(|r| value(r, "filled_qty"))),
        ),
        (
            "failed_value",
            round_sum(failed.iter().map(|r| value(r, "planned_qty"))),
        ),
        (
            "aborted_value",
            round_sum(aborted.iter().map(|r| value(r, "planned_qty"))),
        ),
        (
            "simulated_value",
            round_sum(simulated.iter().map(|r| value(r, "planned_qty"))),
        ),
        ("aborted_symbols", syms(&aborted)),
        ("reached_exchange", n(submitted.len())),
        ("counts", PyValue::Map(counts)),
        ("not_submitted", syms(&pending)),
        ("failed_symbols", syms(&failed)),
        ("awaiting_reconciliation", syms(&working)),
        ("fills_confirmed", PyValue::Bool(working.is_empty())),
    ]))
}

/// `latest(conn, *, evaluation_id=None)`: the most recent stored plan.
///
/// # Errors
/// The driver's.
pub async fn latest(
    conn: &mut Conn,
    evaluation_id: Option<&str>,
) -> Result<Option<PyDict>, StorageError> {
    let mut sql = "SELECT * FROM rebalance_versions".to_owned();
    let mut args = Vec::new();
    if let Some(e) = evaluation_id.filter(|e| !e.is_empty()) {
        sql.push_str(" WHERE evaluation_id=?");
        args.push(Param::Text(e.to_owned()));
    }
    sql.push_str(" ORDER BY created_ts DESC LIMIT 1");
    let cur = conn.execute(&sql, &args).await?;
    Ok(cur.fetchone().map(row_dict))
}

/// `orders_frame(conn, plan_id=None)`'s rows (the DataFrame's records).
///
/// # Errors
/// The driver's.
pub async fn orders_rows(
    conn: &mut Conn,
    plan_id: Option<&str>,
) -> Result<Vec<PyDict>, StorageError> {
    let mut sql = "SELECT * FROM rebalance_orders".to_owned();
    let mut args = Vec::new();
    if let Some(p) = plan_id.filter(|p| !p.is_empty()) {
        sql.push_str(" WHERE version_id=?");
        args.push(Param::Text(p.to_owned()));
    }
    Ok(conn
        .execute(&sql, &args)
        .await?
        .rows
        .iter()
        .map(row_dict)
        .collect())
}

/// `history(conn, limit=20)`.
///
/// # Errors
/// The driver's.
pub async fn history(conn: &mut Conn, limit: i64) -> Result<Vec<PyDict>, StorageError> {
    Ok(conn
        .execute(
            "SELECT v.*, (SELECT COUNT(*) FROM rebalance_orders o             WHERE o.version_id = v.version_id) AS order_count FROM rebalance_versions v ORDER BY v.created_ts DESC LIMIT ?",
            &[Param::Int(limit)],
        )
        .await?
        .rows
        .iter()
        .map(row_dict)
        .collect())
}

/// `datetime.fromtimestamp(ts).date()` in the process's local zone (Python's semantics).
fn local_date(ts: f64) -> Option<NaiveDate> {
    if !ts.is_finite() {
        return None;
    }
    #[allow(clippy::cast_possible_truncation)]
    let secs = ts.floor() as i64;
    chrono::Local
        .timestamp_opt(secs, 0)
        .single()
        .map(|t| t.date_naive())
}

/// `lapse_stale_orders(conn, *, today)`: settle `SUBMITTED` rows whose plan belongs to a
/// **strictly earlier** session (a Zerodha regular order is a DAY order) — `PARTIAL` when
/// something traded, `LAPSED` otherwise. `now` is the `reconciled_at` stamp
/// (`datetime.now().isoformat(timespec="seconds")`).
///
/// # Errors
/// The driver's.
pub async fn lapse_stale_orders(
    conn: &mut Conn,
    today: NaiveDate,
    now: &str,
) -> Result<PyDict, StorageError> {
    let now = now.to_owned();
    let changed = conn
        .transaction(move |c| {
            Box::pin(async move {
                let rows = c
                    .execute(
                        "SELECT o.id, o.symbol, o.filled_qty, o.planned_qty, o.version_id,       v.created_ts FROM rebalance_orders o JOIN rebalance_versions v ON v.version_id = o.version_id WHERE o.status = ?",
                        &[Param::Text(SUBMITTED.to_owned())],
                    )
                    .await?
                    .rows;
                let mut changed: Vec<PyValue> = Vec::new();
                for r in rows {
                    let ts = match r.get("created_ts") {
                        Some(DbValue::Text(s)) => s.trim().parse::<f64>().ok(),
                        Some(v) => v.as_f64(),
                        None => None,
                    };
                    let Some(created) = ts.and_then(local_date) else {
                        continue;
                    };
                    if created >= today {
                        continue;
                    }
                    let filled = db_int(r.get("filled_qty"));
                    let status = if filled != 0 { PARTIAL } else { LAPSED };
                    c.execute(
                        "UPDATE rebalance_orders SET status=?, reconciled_at=? WHERE id=?",
                        &[
                            Param::Text(status.to_owned()),
                            Param::Text(now.clone()),
                            r.get("id").map_or(Param::Null, |v| param_of(&db_to_py(v))),
                        ],
                    )
                    .await?;
                    changed.push(PyValue::Map(dict([
                        ("symbol", r.get("symbol").map_or(PyValue::None, db_to_py)),
                        ("plan", r.get("version_id").map_or(PyValue::None, db_to_py)),
                        ("session", PyValue::Str(created.format("%Y-%m-%d").to_string())),
                        ("was", PyValue::Str(SUBMITTED.to_owned())),
                        ("now", PyValue::Str(status.to_owned())),
                        ("filled", PyValue::Int(filled)),
                        ("planned", PyValue::Int(db_int(r.get("planned_qty")))),
                    ])));
                }
                Ok(changed)
            })
        })
        .await?;
    let n = i64::try_from(changed.len()).unwrap_or(i64::MAX);
    Ok(dict([
        ("lapsed", PyValue::List(changed)),
        ("n", PyValue::Int(n)),
    ]))
}

/// `str(o.get(k) or "")`.
fn str_or_empty(o: &PyDict, k: &str) -> String {
    let v = get(o, k);
    if v.truthy() { py_str(v) } else { String::new() }
}

/// `reconcile_fills(conn, plan_id, broker_orders, *, now)`: settle `SUBMITTED`/`FILLED`/
/// `PARTIAL` rows from the broker's own order book, by `order_id`, else by a unique symbol.
///
/// # Errors
/// The driver's.
pub async fn reconcile_fills(
    conn: &mut Conn,
    plan_id: &str,
    broker_orders: &[PyDict],
    now: &str,
) -> Result<PyDict, StorageError> {
    let mut by_id: Vec<(String, PyDict)> = Vec::new();
    let mut by_sym: Vec<(String, Vec<PyDict>)> = Vec::new();
    for o in broker_orders {
        let oid = str_or_empty(o, "order_id");
        if !oid.is_empty() {
            match by_id.iter_mut().find(|(k, _)| *k == oid) {
                Some(slot) => slot.1 = o.clone(),
                None => by_id.push((oid, o.clone())),
            }
        }
        let sym = str_or_empty(o, "tradingsymbol");
        match by_sym.iter_mut().find(|(k, _)| *k == sym) {
            Some(slot) => slot.1.push(o.clone()),
            None => by_sym.push((sym, vec![o.clone()])),
        }
    }
    let rows = orders_of(conn, plan_id).await?;
    let mut changed: Vec<PyValue> = Vec::new();
    let mut unresolved: Vec<PyValue> = Vec::new();
    let mut updates: Vec<Vec<Param>> = Vec::new();
    for r in &rows {
        let status_was = db_str(r, "status");
        if ![SUBMITTED, FILLED, PARTIAL].contains(&status_was.as_str()) {
            continue;
        }
        let row_oid = match r.get("order_id") {
            Some(DbValue::Null) | None => String::new(),
            Some(v) => py_str(&db_to_py(v)),
        };
        let symbol = db_str(r, "symbol");
        let found = by_id
            .iter()
            .find(|(k, _)| *k == row_oid)
            .map(|(_, o)| o.clone())
            .or_else(|| {
                by_sym
                    .iter()
                    .find(|(k, _)| *k == symbol)
                    .and_then(|(_, c)| {
                        if c.len() == 1 {
                            c.first().cloned()
                        } else {
                            None
                        }
                    })
            });
        let Some(o) = found else {
            unresolved.push(PyValue::Str(symbol));
            continue;
        };
        let state = str_or_empty(&o, "status").to_uppercase();
        let filled = if get(&o, "filled_quantity").truthy() {
            as_int(get(&o, "filled_quantity")).unwrap_or(0)
        } else {
            0
        };
        let planned = db_int(r.get("planned_qty"));
        let status = if state == "COMPLETE" {
            if filled >= planned { FILLED } else { PARTIAL }
        } else if state == "REJECTED" || state == "CANCELLED" {
            if filled == 0 && state == "REJECTED" {
                FAILED
            } else if filled != 0 {
                PARTIAL
            } else {
                LAPSED
            }
        } else {
            SUBMITTED
        };
        let avg = if get(&o, "average_price").truthy() {
            param_of(get(&o, "average_price"))
        } else {
            Param::Null
        };
        updates.push(vec![
            Param::Text(status.to_owned()),
            Param::Int(filled),
            avg,
            Param::Text(now.to_owned()),
            r.get("id").map_or(Param::Null, |v| param_of(&db_to_py(v))),
        ]);
        if status != status_was || filled != db_int(r.get("filled_qty")) {
            changed.push(PyValue::Map(dict([
                ("symbol", PyValue::Str(db_str(r, "symbol"))),
                ("was", PyValue::Str(status_was.clone())),
                ("now", PyValue::Str(status.to_owned())),
                ("filled", PyValue::Int(filled)),
                ("planned", PyValue::Int(planned)),
            ])));
        }
    }
    conn.transaction(move |c| {
        Box::pin(async move {
            c.executemany(
                "UPDATE rebalance_orders SET status=?, filled_qty=?, avg_fill_price=?, \
                 reconciled_at=? WHERE id=?",
                &updates,
            )
            .await?;
            Ok(())
        })
    })
    .await?;
    Ok(dict([
        ("plan_id", PyValue::Str(plan_id.to_owned())),
        ("changed", PyValue::List(changed)),
        ("unresolved", PyValue::List(unresolved)),
        ("reconciled_at", PyValue::Str(now.to_owned())),
    ]))
}

/// `open_plans(conn, limit=5)`: plans with orders still `SUBMITTED`.
///
/// # Errors
/// The driver's.
pub async fn open_plans(conn: &mut Conn, limit: i64) -> Result<Vec<String>, StorageError> {
    Ok(conn
        .execute(
            "SELECT DISTINCT version_id FROM rebalance_orders WHERE status=? ORDER BY id DESC LIMIT ?",
            &[Param::Text(SUBMITTED.to_owned()), Param::Int(limit)],
        )
        .await?
        .rows
        .iter()
        .map(|r| db_str(r, "version_id"))
        .collect())
}
