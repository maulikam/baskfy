//! `app/analytics/regime_store.py`: persistence for regime decisions, exposure reconciliation
//! and book snapshots — the **reads**, plus `record_exposure`.
//!
//! Two separate facts, two tables: `regime_evaluations` (what the market said) and
//! `regime_exposure` (what the portfolio actually is). `run_id='canonical'` is reserved for the
//! one committed record per `evaluation_id`.
//!
//! **Not ported (loud):** `save_preview`, `commit_evaluation` and `save_book_snapshot` take the
//! engine's `RegimeState`, which only `regime_run.py` builds; they stay on Python with it
//! (DECISIONS-RS, lane analytics_regime, label R-ar.1). The refusal they carry — `commit_evaluation` raises under `DRY_RUN` — is
//! asserted on the Python side by `tests/test_regime.py` and is not weakened by leaving it there.

use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::{PyDict, PyValue};

use super::web::row_dict;

/// `CANONICAL`.
pub const CANONICAL: &str = "canonical";

async fn one(conn: &mut Conn, sql: &str, args: &[Param]) -> Result<Option<PyDict>, StorageError> {
    let cur = conn.execute(sql, args).await?;
    Ok(cur.fetchone().map(row_dict))
}

async fn all(conn: &mut Conn, sql: &str, args: &[Param]) -> Result<Vec<PyDict>, StorageError> {
    let cur = conn.execute(sql, args).await?;
    Ok(cur.rows.iter().map(row_dict).collect())
}

/// `get_canonical(conn, evaluation_id)`.
///
/// # Errors
/// The driver's.
pub async fn get_canonical(
    conn: &mut Conn,
    evaluation_id: &str,
) -> Result<Option<PyDict>, StorageError> {
    one(
        conn,
        "SELECT * FROM regime_evaluations WHERE evaluation_id=? AND run_id=?",
        &[evaluation_id.into(), CANONICAL.into()],
    )
    .await
}

/// `previews_for(conn, evaluation_id)`.
///
/// # Errors
/// The driver's.
pub async fn previews_for(
    conn: &mut Conn,
    evaluation_id: &str,
) -> Result<Vec<PyDict>, StorageError> {
    all(
        conn,
        "SELECT * FROM regime_evaluations WHERE evaluation_id=? AND run_id<>? ORDER BY created_at",
        &[evaluation_id.into(), CANONICAL.into()],
    )
    .await
}

/// `latest_committed(conn)`.
///
/// # Errors
/// The driver's.
pub async fn latest_committed(conn: &mut Conn) -> Result<Option<PyDict>, StorageError> {
    one(
        conn,
        "SELECT * FROM regime_evaluations WHERE run_id=? ORDER BY scheduled_week_end DESC, id DESC LIMIT 1",
        &[CANONICAL.into()],
    )
    .await
}

/// `latest_evaluation(conn)`: the most recent evaluation of any kind.
///
/// # Errors
/// The driver's.
pub async fn latest_evaluation(conn: &mut Conn) -> Result<Option<PyDict>, StorageError> {
    one(
        conn,
        "SELECT * FROM regime_evaluations ORDER BY scheduled_week_end DESC, id DESC LIMIT 1",
        &[],
    )
    .await
}

fn truthy(v: Option<&PyValue>) -> bool {
    match v {
        None | Some(PyValue::None) => false,
        Some(PyValue::Bool(b)) => *b,
        Some(PyValue::Int(i)) => *i != 0,
        Some(PyValue::Float(f)) => *f != 0.0,
        Some(PyValue::Str(s)) => !s.is_empty(),
        Some(PyValue::List(l)) => !l.is_empty(),
        Some(PyValue::Map(m)) => !m.is_empty(),
        Some(_) => true,
    }
}

/// `"RECOVERY_OVERRIDE_UNCONFIRMED" in json.loads(r["reason_codes_json"])`, raising where Python
/// raises: `json.loads` of NULL or of text that is not JSON, and `in` over a number or a bool.
/// `in` over a JSON string is a substring test and over an object a key test, as in Python.
/// `build` turns the error into `recovery_confirmations = None`, exactly as its `except` does.
fn holds_recovery_code(v: Option<&PyValue>) -> Result<bool, StorageError> {
    const CODE: &str = "RECOVERY_OVERRIDE_UNCONFIRMED";
    let text = match v {
        Some(PyValue::Str(s)) => s.clone(),
        _ => {
            return Err(StorageError::Other(
                "TypeError: the JSON object must be str, bytes or bytearray, not NoneType"
                    .to_owned(),
            ));
        }
    };
    match PyValue::from_json(text.as_bytes()) {
        Ok(PyValue::List(items)) => Ok(items
            .iter()
            .any(|i| matches!(i, PyValue::Str(s) if s == CODE))),
        Ok(PyValue::Str(s)) => Ok(s.contains(CODE)),
        Ok(PyValue::Map(m)) => Ok(m.get(CODE).is_some()),
        Ok(PyValue::None) => Err(StorageError::Other(
            "TypeError: argument of type 'NoneType' is not iterable".to_owned(),
        )),
        Ok(_) => Err(StorageError::Other(
            "TypeError: argument of type is not iterable".to_owned(),
        )),
        Err(e) => Err(StorageError::Other(format!("JSONDecodeError: {e}"))),
    }
}

/// `count_recovery_confirmations(conn)`: consecutive most-recent committed evaluations whose
/// recovery conditions held.
///
/// # Errors
/// The driver's.
pub async fn count_recovery_confirmations(conn: &mut Conn) -> Result<i64, StorageError> {
    let rows = all(
        conn,
        "SELECT override_active, reason_codes_json FROM regime_evaluations WHERE run_id=? ORDER BY scheduled_week_end DESC",
        &[CANONICAL.into()],
    )
    .await?;
    let mut n = 0;
    for r in rows {
        // Python decodes before it tests `override_active`, so a bad row raises either way.
        let has_code = holds_recovery_code(r.get("reason_codes_json"))?;
        if truthy(r.get("override_active")) || has_code {
            n += 1;
        } else {
            break;
        }
    }
    Ok(n)
}

/// `committed_history(conn)`.
///
/// # Errors
/// The driver's.
pub async fn committed_history(conn: &mut Conn) -> Result<Vec<PyDict>, StorageError> {
    all(
        conn,
        "SELECT * FROM regime_evaluations WHERE run_id=? ORDER BY scheduled_week_end",
        &[CANONICAL.into()],
    )
    .await
}

/// `record_exposure(...)`: append (or update in place) one exposure observation.
///
/// # Errors
/// The driver's.
#[allow(clippy::too_many_arguments)]
pub async fn record_exposure(
    conn: &mut Conn,
    evaluation_id: &str,
    actual_equity_pct: f64,
    target_equity_cap_pct: f64,
    pending_buy_pct: f64,
    pending_sell_pct: f64,
    execution_status: &str,
    observed_at: &str,
    note: Option<&str>,
) -> Result<PyDict, StorageError> {
    let gap = crate::lanes::weekly::pandas::py_round(actual_equity_pct - target_equity_cap_pct, 6);
    let args = vec![
        Param::from(evaluation_id),
        Param::from(observed_at),
        Param::from(actual_equity_pct),
        Param::from(target_equity_cap_pct),
        Param::from(gap),
        Param::from(pending_buy_pct),
        Param::from(pending_sell_pct),
        Param::from(execution_status),
        Param::from(note.map(str::to_owned)),
    ];
    conn.transaction(|c| {
        Box::pin(async move {
            c.execute(
                "INSERT INTO regime_exposure(evaluation_id, observed_at, actual_equity_pct, \
                 target_equity_cap_pct, exposure_gap_pct, pending_buy_pct, pending_sell_pct, \
                 execution_status, note) VALUES(?,?,?,?,?,?,?,?,?) \
                 ON CONFLICT(evaluation_id, observed_at) DO UPDATE SET \
                 actual_equity_pct=excluded.actual_equity_pct, \
                 target_equity_cap_pct=excluded.target_equity_cap_pct, \
                 exposure_gap_pct=excluded.exposure_gap_pct, \
                 pending_buy_pct=excluded.pending_buy_pct, \
                 pending_sell_pct=excluded.pending_sell_pct, \
                 execution_status=excluded.execution_status, note=excluded.note",
                &args,
            )
            .await?;
            Ok(())
        })
    })
    .await?;
    Ok(crate::wire::dict([
        ("evaluation_id", PyValue::Str(evaluation_id.to_owned())),
        ("observed_at", PyValue::Str(observed_at.to_owned())),
        ("exposure_gap_pct", PyValue::Float(gap)),
        (
            "execution_status",
            PyValue::Str(execution_status.to_owned()),
        ),
    ]))
}

/// `latest_exposure(conn, evaluation_id=None)`.
///
/// # Errors
/// The driver's.
pub async fn latest_exposure(
    conn: &mut Conn,
    evaluation_id: Option<&str>,
) -> Result<Option<PyDict>, StorageError> {
    match evaluation_id.filter(|e| !e.is_empty()) {
        Some(e) => {
            one(
                conn,
                "SELECT * FROM regime_exposure WHERE evaluation_id=? ORDER BY observed_at DESC, id DESC LIMIT 1",
                &[e.into()],
            )
            .await
        }
        None => {
            one(
                conn,
                "SELECT * FROM regime_exposure ORDER BY observed_at DESC, id DESC LIMIT 1",
                &[],
            )
            .await
        }
    }
}

/// `open_exposure_gap(conn, tolerance_pct)`: the latest observation still above its cap.
///
/// # Errors
/// The driver's.
pub async fn open_exposure_gap(
    conn: &mut Conn,
    tolerance_pct: f64,
) -> Result<Option<PyDict>, StorageError> {
    let row = latest_exposure(conn, None).await?;
    Ok(row.filter(|r| {
        r.get("exposure_gap_pct")
            .and_then(|v| match v {
                PyValue::Float(f) => Some(*f),
                #[allow(clippy::cast_precision_loss)]
                PyValue::Int(i) => Some(*i as f64),
                _ => None,
            })
            .is_some_and(|g| g > tolerance_pct)
    }))
}

/// `exposure_history(conn, evaluation_id)`.
///
/// # Errors
/// The driver's.
pub async fn exposure_history(
    conn: &mut Conn,
    evaluation_id: &str,
) -> Result<Vec<PyDict>, StorageError> {
    all(
        conn,
        "SELECT * FROM regime_exposure WHERE evaluation_id=? ORDER BY observed_at",
        &[evaluation_id.into()],
    )
    .await
}

/// `last_r1_snapshot(conn)`'s row (weights, source, coverage), or `None`.
///
/// # Errors
/// The driver's.
pub async fn last_r1_snapshot(conn: &mut Conn) -> Result<Option<PyDict>, StorageError> {
    one(
        conn,
        "SELECT * FROM regime_book_snapshots WHERE policy_tier='R1' ORDER BY scheduled_week_end DESC LIMIT 1",
        &[],
    )
    .await
}

/// `DbValue` → `bool(value)` (used by readers of the stored ints).
#[must_use]
pub fn db_truthy(v: Option<&DbValue>) -> bool {
    match v {
        None | Some(DbValue::Null) => false,
        Some(DbValue::Int(i)) => *i != 0,
        Some(DbValue::Bool(b)) => *b,
        Some(DbValue::Float(f)) => *f != 0.0,
        Some(DbValue::Text(s) | DbValue::Json(s)) => !s.is_empty(),
        Some(_) => true,
    }
}

#[cfg(test)]
mod tests {
    use super::holds_recovery_code;
    use crate::wire::PyValue;

    fn s(x: &str) -> PyValue {
        PyValue::Str(x.to_owned())
    }

    #[test]
    fn the_recovery_code_test_raises_where_python_raises() {
        // Python: `codes = json.loads(r["reason_codes_json"])` then `"..." in codes`.
        assert!(matches!(
            holds_recovery_code(Some(&s(r#"["RECOVERY_OVERRIDE_UNCONFIRMED"]"#))),
            Ok(true)
        ));
        assert!(matches!(
            holds_recovery_code(Some(&s(r#"["R2_DEFAULT"]"#))),
            Ok(false)
        ));
        assert!(matches!(holds_recovery_code(Some(&s("[]"))), Ok(false)));
        // A JSON string: `in` is a substring test.
        assert!(matches!(
            holds_recovery_code(Some(&s(r#""xRECOVERY_OVERRIDE_UNCONFIRMEDx""#))),
            Ok(true)
        ));
        // NULL, not-JSON, `null` and a number all raise, which `build` reports as None.
        assert!(holds_recovery_code(Some(&PyValue::None)).is_err());
        assert!(holds_recovery_code(None).is_err());
        assert!(holds_recovery_code(Some(&s("not json"))).is_err());
        assert!(holds_recovery_code(Some(&s("null"))).is_err());
        assert!(holds_recovery_code(Some(&s("3"))).is_err());
    }
}
