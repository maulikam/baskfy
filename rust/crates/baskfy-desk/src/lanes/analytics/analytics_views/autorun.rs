//! `app/analytics/autorun.py` — what the day still needs collected, and why.
//!
//! PURE in the Python's sense: it reads the database and a clock and returns reasons; it
//! runs nothing. The options-lab half (`strangle_collect`, `strangle_session`) needs the
//! strangle subsystem, which is **frozen** (`frozen/strangle/`, M6) and therefore absent:
//! Python's `lab_enabled()` is `OPTIONS_ENABLED and lab_present()`, and with the lab frozen it
//! is false, so the Python never offers those items either. [`needed`] takes the lab's
//! presence as an argument and, when asked for it, logs Python's warning and adds nothing —
//! the frozen tree is never ported (root CLAUDE.md, safety rails).

use chrono::{NaiveDateTime, NaiveTime};

use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::{PyDict, PyValue, dict};

/// `MARKET_CLOSE`.
#[must_use]
pub fn market_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// `DECIDED`: events that mean the options day has been decided (kept for the record; the
/// frozen lab is the only reader).
pub const DECIDED: [&str; 5] = [
    "session_closed",
    "entry_window_closed",
    "skipped",
    "no_entry",
    "entry_cost_veto",
];

fn step_text(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Null => "None".to_owned(),
        serde_json::Value::Bool(true) => "True".to_owned(),
        serde_json::Value::Bool(false) => "False".to_owned(),
        other => other.to_string(),
    }
}

/// `_daily_ok_today(conn, today, after_close)`: a COMPLETE run today, not merely a
/// successful one.
///
/// # Errors
/// The driver's.
pub async fn daily_ok_today(
    conn: &mut Conn,
    today: chrono::NaiveDate,
    after_close: bool,
) -> Result<bool, StorageError> {
    let cur = conn
        .execute(
            "SELECT steps_json FROM daily_runs WHERE session_date=? AND outcome='ok'",
            &[Param::Text(today.format("%Y-%m-%d").to_string())],
        )
        .await?;
    for row in &cur.rows {
        let raw = match row.get("steps_json") {
            Some(DbValue::Text(s)) if !s.is_empty() => s.clone(),
            Some(DbValue::Json(s)) => s.clone(),
            _ => "[]".to_owned(),
        };
        // json.loads(...) raising ValueError → skip the row.
        let Ok(serde_json::Value::Array(steps)) = serde_json::from_str::<serde_json::Value>(&raw)
        else {
            continue;
        };
        let as_list = |s: &serde_json::Value| s.as_array().cloned().unwrap_or_default();
        if steps.iter().any(|s| {
            let s = as_list(s);
            s.len() > 2 && step_text(&s[2]).contains("not selected by --only")
        }) {
            continue;
        }
        if after_close {
            let snap = steps.iter().map(as_list).find(|s| {
                s.first()
                    .is_some_and(|n| n.as_str() == Some("EOD snapshot"))
            });
            let landed = snap
                .as_ref()
                .and_then(|s| s.get(1))
                .is_some_and(|o| o.as_str() == Some("ok"));
            if !landed {
                continue;
            }
        }
        return Ok(true);
    }
    Ok(false)
}

/// `needed(conn, now=..., is_trading_day=...)`: the outstanding collection for today, in
/// order, each with its reason. `lab_enabled` is Python's `options_lab.lab_enabled()`.
///
/// # Errors
/// The driver's.
pub async fn needed(
    conn: &mut Conn,
    now: NaiveDateTime,
    is_trading_day: bool,
    lab_enabled: bool,
) -> Result<Vec<PyDict>, StorageError> {
    if !is_trading_day {
        return Ok(vec![]);
    }
    let today = now.date();
    let mut out = Vec::new();
    let after_close = now.time() >= market_close();
    if !daily_ok_today(conn, today, after_close).await? {
        let why = if after_close {
            "the EOD snapshot for today has still not landed; kc.margins() has no history, so a missed session cannot be backfilled"
        } else {
            "no complete run recorded today; index history, benchmarks and today's fills are same-day reads that cannot be backfilled"
        };
        let note = if now.time() < market_close() {
            "the EOD snapshot will be skipped before 15:30 and is left to the 18:30 job"
        } else {
            ""
        };
        out.push(dict([
            ("op", PyValue::Str("daily".to_owned())),
            ("label", PyValue::Str("Daily collection".to_owned())),
            ("why", PyValue::Str(why.to_owned())),
            ("note", PyValue::Str(note.to_owned())),
        ]));
    }
    if lab_enabled {
        // The strangle subsystem is frozen: Python's `ImportError` branch (logging.warning).
        warn_frozen_lab();
    }
    Ok(out)
}

/// Python's `logging.warning` for the frozen lab, on stderr.
#[allow(clippy::print_stderr)]
fn warn_frozen_lab() {
    eprintln!(
        "WARNING: OPTIONS_ENABLED is set, but the strangle subsystem is frozen (frozen/strangle/). Today's options collection is not offered; the equity collection above is unaffected."
    );
}

/// `summary(items)`.
#[must_use]
pub fn summary(items: &[PyDict]) -> String {
    if items.is_empty() {
        return "Nothing outstanding — today is already collected.".to_owned();
    }
    let labels: Vec<String> = items
        .iter()
        .map(|i| i.get("label").map(PyValue::py_str).unwrap_or_default())
        .collect();
    format!("{} outstanding: {}", items.len(), labels.join(", "))
}

#[cfg(test)]
mod tests;
