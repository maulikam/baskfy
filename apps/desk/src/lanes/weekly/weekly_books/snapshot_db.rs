//! The snapshot half of `app/analytics/db.py` that `snapshot.run()` and its CLI need:
//! cashflows (`record_cashflow`, `import_cashflows_csv`, `cashflows_by_date`), the EOD row
//! (`upsert_snapshot`, `rechain_index`, `save_snapshot`, `get_snapshot`, `snapshot_series`) and
//! the unrecorded-flow aid (`suggest_unrecorded_cashflows`).
//!
//! **Kept here, in this sub-lane, until lane `analytics` ports `db.py` whole** (the same arrangement
//! as `tradebook`'s private `corporate_actions` reads, DECISIONS-RS R8.WB3). The SQL is Python's,
//! verbatim; the tables are the desk schema's (`storage::migrations` version 1).
//!
//! Money here is `REAL`, as Python's table declares it: the NAV series is a float series in the
//! desk, so it is one here (house rule 9 governs the screener's `numeric` columns, not this file).

use std::path::Path;

use crate::storage::{Conn, DbValue, Param, Row, StorageError};
use crate::wire::{PyDict, PyValue, dict};

/// `config.INDEX_BASE`: the portfolio index on the inception date.
pub const INDEX_BASE: f64 = 100.0;

/// What a cashflow import or a snapshot write can fail on.
#[derive(Debug, thiserror::Error)]
pub enum DbError {
    /// The database.
    #[error(transparent)]
    Storage(#[from] StorageError),
    /// What Python raises uncaught (`"ValueError: ..."`, `"FileNotFoundError: ..."`).
    #[error("{0}")]
    Python(String),
}

fn f(row: &Row, key: &str) -> f64 {
    row.get(key).and_then(DbValue::as_f64).unwrap_or(0.0)
}

fn text(row: &Row, key: &str) -> String {
    match row.get(key) {
        Some(DbValue::Text(s)) => s.clone(),
        Some(other) => super::db_to_py(other).py_str(),
        None => String::new(),
    }
}

/// `float(text)` (whitespace stripped; `_` between digits; `inf`/`nan` in any case).
///
/// # Errors
/// `ValueError: could not convert string to float: '<text>'`.
pub fn py_float(raw: &str) -> Result<f64, String> {
    let refuse = || {
        format!(
            "ValueError: could not convert string to float: {}",
            PyValue::Str(raw.to_owned()).py_repr()
        )
    };
    let t = raw.trim();
    let bytes = t.as_bytes();
    for (i, b) in bytes.iter().enumerate() {
        if *b == b'_' {
            let ok = i > 0
                && bytes[i - 1].is_ascii_digit()
                && bytes.get(i + 1).is_some_and(u8::is_ascii_digit);
            if !ok {
                return Err(refuse());
            }
        }
    }
    let clean: String = t.chars().filter(|c| *c != '_').collect();
    let lower = clean.to_ascii_lowercase();
    let body = lower.trim_start_matches(['+', '-']);
    if matches!(body, "inf" | "infinity" | "nan") {
        let neg = lower.starts_with('-');
        return Ok(match (body, neg) {
            ("nan", _) => f64::NAN,
            (_, true) => f64::NEG_INFINITY,
            _ => f64::INFINITY,
        });
    }
    if clean.is_empty() || !clean.bytes().any(|b| b.is_ascii_digit()) {
        return Err(refuse());
    }
    clean.parse::<f64>().map_err(|_| refuse())
}

/// `record_cashflow(conn, date, amount, kind)`: `True` when a new row was inserted. Sign
/// convention: invest NEGATIVE, withdraw POSITIVE.
///
/// # Errors
/// The driver's.
pub async fn record_cashflow(
    conn: &mut Conn,
    date: &str,
    amount: f64,
    kind: &str,
) -> Result<bool, StorageError> {
    let cur = conn
        .execute(
            "INSERT INTO cashflows(date, amount, type) VALUES(?,?,?) \
             ON CONFLICT(date, amount, type) DO NOTHING",
            &[
                Param::Text(date.to_owned()),
                Param::Float(amount),
                Param::Text(kind.to_owned()),
            ],
        )
        .await?;
    Ok(cur.rowcount > 0)
}

/// `csv.DictReader` rows over `text` (UTF-8, BOM stripped): the first record names the columns,
/// a later duplicate name wins, a short row reads missing as `None` (`""` after `or ""`), extra
/// fields go under `None` (dropped by the caller's `if k`).
fn dict_rows(text: &str) -> Vec<Vec<(String, String)>> {
    let mut records = super::tradebook::csv_records(text).into_iter();
    let Some(header) = records.find(|r| !r.is_empty()) else {
        return Vec::new();
    };
    records
        .filter(|r| !r.is_empty())
        .map(|r| {
            let mut row: Vec<(String, String)> = Vec::new();
            for (i, name) in header.iter().enumerate() {
                let v = r.get(i).cloned().unwrap_or_default();
                if let Some(slot) = row.iter_mut().find(|(k, _)| k == name) {
                    slot.1 = v;
                } else {
                    row.push((name.clone(), v));
                }
            }
            row
        })
        .collect()
}

/// `import_cashflows_csv(conn, path)`: `date,amount,type` (signed) or `date,debit,credit[,type]`
/// (ledger style: debit is money in). Re-importing the same file is a no-op.
///
/// # Errors
/// The file (Python's `FileNotFoundError`/`UnicodeDecodeError`), an amount `float()` refuses
/// (`ValueError`), or the driver's.
pub async fn import_cashflows_csv(conn: &mut Conn, path: &Path) -> Result<PyDict, DbError> {
    let bytes = std::fs::read(path).map_err(|e| {
        DbError::Python(format!(
            "FileNotFoundError: [Errno 2] No such file or directory: {} ({e})",
            PyValue::Str(path.display().to_string()).py_repr()
        ))
    })?;
    let text = String::from_utf8(bytes)
        .map_err(|e| DbError::Python(format!("UnicodeDecodeError: {e}")))?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    let (mut inserted, mut skipped) = (0_i64, 0_i64);
    for row in dict_rows(text) {
        // `{k.strip().lower(): (v or "").strip() for k, v in row.items() if k}`
        let mut keys: Vec<(String, String)> = Vec::new();
        for (k, v) in row {
            if k.is_empty() {
                continue;
            }
            let key = k.trim().to_lowercase();
            let val = v.trim().to_owned();
            if let Some(slot) = keys.iter_mut().find(|(kk, _)| *kk == key) {
                slot.1 = val;
            } else {
                keys.push((key, val));
            }
        }
        let get = |k: &str| keys.iter().find(|(kk, _)| kk == k).map(|(_, v)| v.as_str());
        let date = get("date")
            .filter(|d| !d.is_empty())
            .or_else(|| get("posting_date"))
            .unwrap_or("");
        if date.is_empty() {
            continue;
        }
        let amount = match get("amount").filter(|a| !a.is_empty()) {
            Some(a) => py_float(&a.replace(',', "")).map_err(DbError::Python)?,
            None => {
                let leg = |k: &str| -> Result<f64, DbError> {
                    let raw = get(k)
                        .filter(|v| !v.is_empty())
                        .unwrap_or("0")
                        .replace(',', "");
                    if raw.is_empty() {
                        Ok(0.0)
                    } else {
                        py_float(&raw).map_err(DbError::Python)
                    }
                };
                let debit = leg("debit")?;
                let credit = leg("credit")?;
                // Money INTO the account is an investment: negative in XIRR terms.
                -(debit - credit)
            }
        };
        let kind = match get("type").filter(|t| !t.is_empty()) {
            Some(t) => t.to_owned(),
            None if amount < 0.0 => "invest".to_owned(),
            None => "withdraw".to_owned(),
        };
        if record_cashflow(conn, date, amount, &kind).await? {
            inserted += 1;
        } else {
            skipped += 1;
        }
    }
    Ok(dict([
        ("inserted", PyValue::Int(inserted)),
        ("skipped_existing", PyValue::Int(skipped)),
    ]))
}

/// `cashflows_by_date(conn)`: date → net flow INTO the portfolio (sign-flipped).
///
/// # Errors
/// The driver's.
pub async fn cashflows_by_date(conn: &mut Conn) -> Result<Vec<(String, f64)>, StorageError> {
    let cur = conn
        .execute(
            "SELECT date, SUM(amount) AS amt FROM cashflows GROUP BY date",
            &[],
        )
        .await?;
    Ok(cur
        .rows
        .iter()
        .map(|r| (text(r, "date"), -f(r, "amt")))
        .collect())
}

/// One EOD snapshot as `upsert_snapshot` takes it.
#[derive(Debug, Clone, PartialEq)]
pub struct SnapRow {
    /// `date`.
    pub date: String,
    /// `nav`.
    pub nav: f64,
    /// `invested`.
    pub invested: f64,
    /// `cash`.
    pub cash: f64,
    /// `holdings_json` (already text).
    pub holdings_json: String,
}

/// `upsert_snapshot(conn, snap, force=)`: `inserted` | `updated` | `unchanged`. Insert-if-absent
/// unless `force`.
///
/// # Errors
/// The driver's.
pub async fn upsert_snapshot(
    conn: &mut Conn,
    snap: &SnapRow,
    force: bool,
) -> Result<&'static str, StorageError> {
    let existing = conn
        .execute(
            "SELECT date FROM snapshots WHERE date=?",
            &[Param::Text(snap.date.clone())],
        )
        .await?;
    let exists = existing.fetchone().is_some();
    if exists && !force {
        return Ok("unchanged");
    }
    if exists {
        conn.execute(
            "UPDATE snapshots SET nav=?, invested=?, cash=?, holdings_json=? WHERE date=?",
            &[
                Param::Float(snap.nav),
                Param::Float(snap.invested),
                Param::Float(snap.cash),
                Param::Text(snap.holdings_json.clone()),
                Param::Text(snap.date.clone()),
            ],
        )
        .await?;
        return Ok("updated");
    }
    conn.execute(
        "INSERT INTO snapshots(date, nav, invested, cash, holdings_json) VALUES(?,?,?,?,?)",
        &[
            Param::Text(snap.date.clone()),
            Param::Float(snap.nav),
            Param::Float(snap.invested),
            Param::Float(snap.cash),
            Param::Text(snap.holdings_json.clone()),
        ],
    )
    .await?;
    Ok("inserted")
}

/// `rechain_index(conn, base=)`: the whole series' `index_value`, base at inception, daily
/// time-weighted with external flows neutralised (`r_t = (nav_t - flow_t) / nav_{t-1} - 1`); a
/// day with no capital at risk yesterday carries the index forward. Returns the rows updated.
///
/// # Errors
/// The driver's.
pub async fn rechain_index(conn: &mut Conn, base: Option<f64>) -> Result<i64, StorageError> {
    let b = base.unwrap_or(INDEX_BASE);
    let flows = cashflows_by_date(conn).await?;
    let rows = conn
        .execute("SELECT date, nav FROM snapshots ORDER BY date", &[])
        .await?
        .rows;
    let mut updates: Vec<Vec<Param>> = Vec::new();
    let mut idx: Option<f64> = None;
    let mut prev_nav: Option<f64> = None;
    for r in &rows {
        let (date, nav) = (text(r, "date"), f(r, "nav"));
        let next = match (idx, prev_nav) {
            (None, _) => b,
            (Some(i), Some(p)) if p > 0.0 => {
                let flow = flows
                    .iter()
                    .rev()
                    .find(|(d, _)| *d == date)
                    .map_or(0.0, |(_, v)| *v);
                i * (1.0 + ((nav - flow) / p - 1.0))
            }
            (Some(i), _) => i,
        };
        idx = Some(next);
        updates.push(vec![Param::Float(next), Param::Text(date)]);
        prev_nav = Some(nav);
    }
    conn.executemany("UPDATE snapshots SET index_value=? WHERE date=?", &updates)
        .await?;
    Ok(i64::try_from(updates.len()).unwrap_or(i64::MAX))
}

/// `save_snapshot(conn, snap, force=)`: upsert + rechain in one transaction.
///
/// # Errors
/// The driver's (the transaction rolled back).
pub async fn save_snapshot(
    conn: &mut Conn,
    snap: &SnapRow,
    force: bool,
) -> Result<&'static str, StorageError> {
    let snap = snap.clone();
    conn.transaction(move |c| {
        Box::pin(async move {
            let outcome = upsert_snapshot(c, &snap, force).await?;
            rechain_index(c, None).await?;
            Ok(outcome)
        })
    })
    .await
}

/// `get_snapshot(conn, date)`: the row, or `None`.
///
/// # Errors
/// The driver's.
pub async fn get_snapshot(conn: &mut Conn, date: &str) -> Result<Option<Row>, StorageError> {
    let cur = conn
        .execute(
            "SELECT * FROM snapshots WHERE date=?",
            &[Param::Text(date.to_owned())],
        )
        .await?;
    Ok(cur.rows.into_iter().next())
}

/// `snapshot_series(conn)`: `(date, nav, invested, cash, index_value)` by date.
///
/// # Errors
/// The driver's.
pub async fn snapshot_series(conn: &mut Conn) -> Result<Vec<Row>, StorageError> {
    Ok(conn
        .execute(
            "SELECT date, nav, invested, cash, index_value FROM snapshots ORDER BY date",
            &[],
        )
        .await?
        .rows)
}

/// One `suggest_unrecorded_cashflows` hit.
#[derive(Debug, Clone, PartialEq)]
pub struct FlowHint {
    /// `date`.
    pub date: String,
    /// `unexplained` (2 dp).
    pub unexplained: f64,
    /// `cash_delta` (2 dp).
    pub cash_delta: f64,
    /// `invested_delta` (2 dp).
    pub invested_delta: f64,
}

/// `suggest_unrecorded_cashflows(conn, tol=0.02)`: a detection aid only — days where cash moved
/// sharply while the invested leg barely did, and no flow is recorded. Never inserts.
///
/// # Errors
/// The driver's.
pub async fn suggest_unrecorded_cashflows(
    conn: &mut Conn,
    tol: f64,
) -> Result<Vec<FlowHint>, StorageError> {
    let rows = snapshot_series(conn).await?;
    let known = cashflows_by_date(conn).await?;
    let mut out = Vec::new();
    for pair in rows.windows(2) {
        let (prev, cur) = (&pair[0], &pair[1]);
        let d_cash = f(cur, "cash") - f(prev, "cash");
        let d_inv = f(cur, "invested") - f(prev, "invested");
        let nav = f(prev, "nav");
        let base = if nav == 0.0 { 1.0 } else { nav };
        let unexplained = d_cash + d_inv;
        let date = text(cur, "date");
        if (unexplained.abs() / base) > tol && !known.iter().any(|(d, _)| *d == date) {
            out.push(FlowHint {
                date,
                unexplained: super::round_to(unexplained, 2),
                cash_delta: super::round_to(d_cash, 2),
                invested_delta: super::round_to(d_inv, 2),
            });
        }
    }
    Ok(out)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::float_cmp)]
mod tests {
    //! `tests/test_db_snapshots.py`'s snapshot and cashflow cases that `snapshot.run()` relies on
    //! (the migration/WAL cases are `storage`'s, the TWR maths `metrics`').
    use super::*;

    async fn conn() -> (tempfile::TempDir, Conn) {
        let dir = tempfile::tempdir().unwrap();
        let mut c = crate::storage::Db::connect_sqlite_path(&dir.path().join("p.db"))
            .await
            .unwrap();
        c.migrate().await.unwrap();
        (dir, c)
    }

    fn snap(date: &str, nav: f64) -> SnapRow {
        SnapRow {
            date: date.into(),
            nav,
            invested: nav * 0.8,
            cash: nav * 0.2,
            holdings_json: "{}".into(),
        }
    }

    async fn index(c: &mut Conn) -> Vec<f64> {
        snapshot_series(c)
            .await
            .unwrap()
            .iter()
            .map(|r| f(r, "index_value"))
            .collect()
    }

    #[test]
    fn float_reads_as_python_does() {
        assert_eq!(py_float(" 1_000.5 "), Ok(1000.5));
        assert!(py_float("NaN").unwrap().is_nan());
        assert_eq!(py_float("-inf"), Ok(f64::NEG_INFINITY));
        assert_eq!(
            py_float("1,000").unwrap_err(),
            "ValueError: could not convert string to float: '1,000'"
        );
        assert!(py_float("_1").is_err());
    }

    #[tokio::test]
    async fn inception_index_is_exactly_100_and_a_rerun_changes_nothing() {
        let (_d, mut c) = conn().await;
        assert_eq!(
            save_snapshot(&mut c, &snap("2026-08-10", 1e6), false)
                .await
                .unwrap(),
            "inserted"
        );
        assert_eq!(index(&mut c).await, [100.0]);
        let again = SnapRow {
            nav: 2e6,
            ..snap("2026-08-10", 1e6)
        };
        assert_eq!(
            save_snapshot(&mut c, &again, false).await.unwrap(),
            "unchanged"
        );
        let row = get_snapshot(&mut c, "2026-08-10").await.unwrap().unwrap();
        assert_eq!(f(&row, "nav"), 1e6);
        assert_eq!(
            save_snapshot(&mut c, &again, true).await.unwrap(),
            "updated"
        );
        let row = get_snapshot(&mut c, "2026-08-10").await.unwrap().unwrap();
        assert_eq!(f(&row, "nav"), 2e6);
    }

    #[tokio::test]
    async fn a_cashflow_does_not_register_as_performance_and_a_backdated_row_rechains() {
        let (_d, mut c) = conn().await;
        save_snapshot(&mut c, &snap("2026-08-10", 1_000_000.0), false)
            .await
            .unwrap();
        // ₹5 lakh invested on the 11th (invest NEGATIVE): the NAV rises by exactly the flow.
        assert!(
            record_cashflow(&mut c, "2026-08-11", -500_000.0, "invest")
                .await
                .unwrap()
        );
        assert!(
            !record_cashflow(&mut c, "2026-08-11", -500_000.0, "invest")
                .await
                .unwrap()
        );
        save_snapshot(&mut c, &snap("2026-08-11", 1_500_000.0), false)
            .await
            .unwrap();
        assert_eq!(index(&mut c).await, [100.0, 100.0]);
        // A withdrawal is not a loss.
        record_cashflow(&mut c, "2026-08-12", 300_000.0, "withdraw")
            .await
            .unwrap();
        save_snapshot(&mut c, &snap("2026-08-12", 1_200_000.0), false)
            .await
            .unwrap();
        assert_eq!(index(&mut c).await, [100.0, 100.0, 100.0]);
        // Backdated: the inception moves and the whole series rechains.
        save_snapshot(&mut c, &snap("2026-08-09", 500_000.0), false)
            .await
            .unwrap();
        assert_eq!(index(&mut c).await, [100.0, 200.0, 200.0, 200.0]);
    }

    #[tokio::test]
    async fn a_zero_nav_day_carries_the_index_forward() {
        let (_d, mut c) = conn().await;
        for (d, nav) in [
            ("2026-08-10", 100.0),
            ("2026-08-11", 0.0),
            ("2026-08-12", 50.0),
        ] {
            save_snapshot(&mut c, &snap(d, nav), false).await.unwrap();
        }
        assert_eq!(index(&mut c).await, [100.0, 0.0, 0.0]);
    }

    #[tokio::test]
    async fn csv_import_reads_signed_and_ledger_shapes_and_is_idempotent() {
        let (dir, mut c) = conn().await;
        let signed = dir.path().join("signed.csv");
        std::fs::write(
            &signed,
            "\u{feff}Date,Amount,Type\n2026-08-01,\"-1,000\",invest\n2026-08-02,250,\n,5,\n",
        )
        .unwrap();
        let got = import_cashflows_csv(&mut c, &signed).await.unwrap();
        assert_eq!(got.get("inserted"), Some(&PyValue::Int(2)));
        let again = import_cashflows_csv(&mut c, &signed).await.unwrap();
        assert_eq!(again.get("skipped_existing"), Some(&PyValue::Int(2)));
        let ledger = dir.path().join("ledger.csv");
        std::fs::write(
            &ledger,
            "posting_date,debit,credit\n2026-08-03,1000,\n2026-08-04,,400\n",
        )
        .unwrap();
        import_cashflows_csv(&mut c, &ledger).await.unwrap();
        let rows = c
            .execute(
                "SELECT date, amount, type FROM cashflows ORDER BY date",
                &[],
            )
            .await
            .unwrap()
            .rows;
        let got: Vec<(String, f64, String)> = rows
            .iter()
            .map(|r| (text(r, "date"), f(r, "amount"), text(r, "type")))
            .collect();
        assert_eq!(
            got,
            [
                ("2026-08-01".into(), -1000.0, "invest".into()),
                ("2026-08-02".into(), 250.0, "withdraw".into()),
                ("2026-08-03".into(), -1000.0, "invest".into()),
                ("2026-08-04".into(), 400.0, "withdraw".into()),
            ]
        );
    }

    #[tokio::test]
    async fn the_unrecorded_cashflow_hint_names_the_day_and_never_inserts() {
        let (_d, mut c) = conn().await;
        for (d, inv, cash) in [("2026-08-10", 800.0, 200.0), ("2026-08-11", 800.0, 700.0)] {
            let s = SnapRow {
                date: d.into(),
                nav: inv + cash,
                invested: inv,
                cash,
                holdings_json: "{}".into(),
            };
            save_snapshot(&mut c, &s, false).await.unwrap();
        }
        let hits = suggest_unrecorded_cashflows(&mut c, 0.02).await.unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(
            (hits[0].date.as_str(), hits[0].unexplained),
            ("2026-08-11", 500.0)
        );
        assert!(cashflows_by_date(&mut c).await.unwrap().is_empty());
    }
}
