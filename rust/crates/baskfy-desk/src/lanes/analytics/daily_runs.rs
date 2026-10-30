//! `app/analytics/daily_runs.py`: the outcome of each daily collection, so a broken streak is
//! visible without reading a log.

use chrono::NaiveDate;

use crate::storage::{Conn, Param, StorageError};
use crate::wire::{PyDict, PyValue, dict};

use super::web::row_dict;

/// A step's status.
pub const OK: &str = "ok";
/// A skipped step.
pub const SKIP: &str = "skipped";
/// A failed step.
pub const FAIL: &str = "FAILED";

/// Outcomes, worst first.
pub const AUTH: &str = "auth";
/// Nothing collected.
pub const FAILED: &str = "failed";
/// Some steps failed.
pub const PARTIAL: &str = "partial";
/// Everything collected.
pub const FINE: &str = "ok";

/// `classify(steps)`: `(name, status, detail)` triples → an outcome.
#[must_use]
pub fn classify(steps: &[(String, String, String)]) -> &'static str {
    let ran: Vec<_> = steps.iter().filter(|s| s.1 != SKIP).collect();
    if ran.is_empty() {
        return FAILED;
    }
    let bad = ran.iter().filter(|s| s.1 == FAIL).count();
    if bad == 0 {
        FINE
    } else if bad == ran.len() {
        FAILED
    } else {
        PARTIAL
    }
}

/// `record(conn, steps=..., trigger=..., session_date=..., outcome=None, duration_s=None)`.
///
/// # Errors
/// Storage.
pub async fn record(
    conn: &mut Conn,
    steps: &[(String, String, String)],
    trigger: &str,
    session_date: NaiveDate,
    outcome: Option<&str>,
    duration_s: Option<f64>,
    now_iso: &str,
) -> Result<i64, StorageError> {
    let outcome = outcome.unwrap_or_else(|| classify(steps)).to_owned();
    let failed = i64::try_from(steps.iter().filter(|s| s.1 == FAIL).count()).unwrap_or(0);
    let steps_json = crate::wire::py_json_dumps(&PyValue::List(
        steps
            .iter()
            .map(|(a, b, c)| {
                PyValue::List(vec![
                    PyValue::Str(a.clone()),
                    PyValue::Str(b.clone()),
                    PyValue::Str(c.clone()),
                ])
            })
            .collect(),
    ))
    .map_err(|e| StorageError::Other(e.to_string()))?;
    let params = vec![
        Param::Text(now_iso.to_owned()),
        Param::Text(session_date.format("%Y-%m-%d").to_string()),
        Param::Text(trigger.to_owned()),
        Param::Text(outcome),
        Param::Int(failed),
        Param::Text(steps_json),
        duration_s.map_or(Param::Null, Param::Float),
    ];
    conn.transaction(move |c| {
        Box::pin(async move {
            let cur = c
                .execute(
                    "INSERT INTO daily_runs(ran_at, session_date, trigger, outcome, \
                     failed_steps, steps_json, duration_s) VALUES(?,?,?,?,?,?,?) RETURNING id",
                    &params,
                )
                .await?;
            Ok(cur
                .fetchone()
                .and_then(|r| r.at(0))
                .and_then(crate::storage::DbValue::as_i64)
                .unwrap_or(0))
        })
    })
    .await
}

/// `history(conn, limit)`.
///
/// # Errors
/// Storage.
pub async fn history(conn: &mut Conn, limit: i64) -> Result<Vec<PyDict>, StorageError> {
    let cur = conn
        .execute(
            "SELECT * FROM daily_runs ORDER BY id DESC LIMIT ?",
            &[Param::Int(limit)],
        )
        .await?;
    Ok(cur.rows.iter().map(row_dict).collect())
}

fn text<'a>(row: &'a PyDict, key: &str) -> &'a str {
    match row.get(key) {
        Some(PyValue::Str(s)) => s,
        _ => "",
    }
}

fn steps_of(row: &PyDict) -> PyValue {
    PyValue::from_json(text(row, "steps_json").as_bytes()).unwrap_or(PyValue::None)
}

/// `status(conn, today=...)`: what the interface says about collection health.
///
/// # Errors
/// Storage.
pub async fn status(conn: &mut Conn, today: NaiveDate) -> Result<PyDict, StorageError> {
    let rows = history(conn, 60).await?;
    let Some(last) = rows.first() else {
        return Ok(dict([
            ("ever_ran", PyValue::Bool(false)),
            ("last", PyValue::None),
            ("outcome", PyValue::None),
            ("stale_days", PyValue::None),
            ("consecutive_failures", PyValue::Int(0)),
            ("healthy", PyValue::Bool(false)),
            (
                "message",
                PyValue::Str("The daily collection has never run.".to_owned()),
            ),
        ]));
    };
    let streak = rows
        .iter()
        .take_while(|r| matches!(text(r, "outcome"), AUTH | FAILED))
        .count();
    let landed = rows
        .iter()
        .find(|r| matches!(text(r, "outcome"), FINE | PARTIAL));
    let stale_days = landed.and_then(|r| {
        let ran = text(r, "ran_at");
        let day = ran.get(..10).unwrap_or(ran);
        NaiveDate::parse_from_str(day, "%Y-%m-%d")
            .ok()
            .map(|d| (today - d).num_days())
    });
    let outcome = text(last, "outcome");
    let msg = match outcome {
        AUTH => "The last run could not log in to Kite. Tokens expire daily with no refresh, so the job cannot recover on its own.".to_owned(),
        FAILED => "The last run collected nothing.".to_owned(),
        PARTIAL => {
            let names: Vec<String> = match steps_of(last) {
                PyValue::List(items) => items
                    .iter()
                    .filter_map(|s| match s {
                        PyValue::List(parts) => match (parts.first(), parts.get(1)) {
                            (Some(n), Some(PyValue::Str(st))) if st == FAIL => Some(n.py_str()),
                            _ => None,
                        },
                        _ => None,
                    })
                    .collect(),
                _ => Vec::new(),
            };
            format!("The last run failed on: {}.", names.join(", "))
        }
        _ => "The last run collected everything.".to_owned(),
    };
    let healthy = matches!(outcome, FINE | PARTIAL) && stale_days.unwrap_or(0) <= 1;
    Ok(dict([
        ("ever_ran", PyValue::Bool(true)),
        ("last", PyValue::Map(last.clone())),
        (
            "outcome",
            last.get("outcome").cloned().unwrap_or(PyValue::None),
        ),
        (
            "ran_at",
            last.get("ran_at").cloned().unwrap_or(PyValue::None),
        ),
        (
            "trigger",
            last.get("trigger").cloned().unwrap_or(PyValue::None),
        ),
        ("steps", steps_of(last)),
        ("stale_days", stale_days.map_or(PyValue::None, PyValue::Int)),
        (
            "consecutive_failures",
            PyValue::Int(i64::try_from(streak).unwrap_or(0)),
        ),
        ("healthy", PyValue::Bool(healthy)),
        ("message", PyValue::Str(msg)),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(n: &str, st: &str) -> (String, String, String) {
        (n.to_owned(), st.to_owned(), String::new())
    }

    #[test]
    fn classify_outcomes() {
        assert_eq!(classify(&[]), FAILED);
        assert_eq!(classify(&[s("a", SKIP)]), FAILED);
        assert_eq!(classify(&[s("a", OK), s("b", SKIP)]), FINE);
        assert_eq!(classify(&[s("a", OK), s("b", FAIL)]), PARTIAL);
        assert_eq!(
            classify(&[s("a", FAIL), s("b", FAIL), s("c", SKIP)]),
            FAILED
        );
    }
}
