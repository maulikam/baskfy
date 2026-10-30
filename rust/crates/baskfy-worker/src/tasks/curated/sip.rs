//! `tasks/curated_sip.run_curated_sip_reminders` and the calendar math of
//! `baskfy_core.curated_sip` it calls (SC7): raise `SIP_DUE` pending actions for ACTIVE
//! REMINDER plans due on `as_of`. REMINDER only — a plan is a **reminder**, never an order;
//! `AUTO` is refused by [`assert_reminder_mode`] exactly as Python refuses it.

use std::collections::{BTreeSet, HashSet};

use baskfy_db::OrderedJson;
use baskfy_db::row::{date, decimal, jsonb};
use chrono::{Datelike, Duration, NaiveDate};
use sqlx::Row;

use super::math::Dec;
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::tasks::TaskContext;

/// `SIP_DUE`.
pub const SIP_DUE: &str = "SIP_DUE";
/// `SIP_MODE_REMINDER`.
pub const SIP_MODE_REMINDER: &str = "REMINDER";
/// `SIP_STATUS_ACTIVE`.
pub const SIP_STATUS_ACTIVE: &str = "ACTIVE";
/// `_NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i16 = 1;
/// `_CALENDAR_LOOKBACK_DAYS`.
const CALENDAR_LOOKBACK_DAYS: i64 = 14;
/// `_CALENDAR_LOOKAHEAD_DAYS`.
const CALENDAR_LOOKAHEAD_DAYS: i64 = 90;
/// `_SNAP_HORIZON_DAYS`.
const SNAP_HORIZON_DAYS: i64 = 60;

fn value_error(message: impl Into<String>) -> WorkerError {
    WorkerError::python("ValueError", message)
}

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `assert_reminder_mode(mode)`.
///
/// # Errors
/// `ValueError` for `AUTO` or any mode other than `REMINDER`.
pub fn assert_reminder_mode(mode: &str) -> Result<(), WorkerError> {
    if mode == "AUTO" {
        return Err(value_error(format!(
            "SIP write path refuses mode {}; AUTO stays dark until D3 (desk non-negotiable #1)",
            py_repr(mode)
        )));
    }
    if mode != SIP_MODE_REMINDER {
        return Err(value_error(format!(
            "SIP write path accepts 'REMINDER' only; got {}",
            py_repr(mode)
        )));
    }
    Ok(())
}

/// `repr(str)` for the plain strings a mode column can hold.
fn py_repr(text: &str) -> String {
    if text.contains('\'') && !text.contains('"') {
        format!("\"{text}\"")
    } else {
        format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// `next_trading_on_or_after(day, trading_dates)`.
///
/// # Errors
/// `ValueError` for an empty set or no trading day within 60 days.
pub fn next_trading_on_or_after(
    day: NaiveDate,
    trading: &BTreeSet<NaiveDate>,
) -> Result<NaiveDate, WorkerError> {
    if trading.is_empty() {
        return Err(value_error("trading_dates cannot be empty"));
    }
    let mut cursor = day;
    for _ in 0..SNAP_HORIZON_DAYS {
        if trading.contains(&cursor) {
            return Ok(cursor);
        }
        cursor += Duration::days(1);
    }
    Err(value_error(format!(
        "no trading day on or after {} within {SNAP_HORIZON_DAYS} days",
        iso(day)
    )))
}

fn candidate(year: i32, month: u32, day_of_month: u32) -> Result<NaiveDate, WorkerError> {
    let first_next = if month == 12 {
        NaiveDate::from_ymd_opt(year + 1, 1, 1)
    } else {
        NaiveDate::from_ymd_opt(year, month + 1, 1)
    };
    let last = first_next
        .and_then(|d| d.pred_opt())
        .map_or(28, |d| d.day());
    NaiveDate::from_ymd_opt(year, month, day_of_month.min(last))
        .ok_or_else(|| value_error("year is out of range"))
}

/// `next_fire_date(day_of_month=, as_of=, trading_dates=, after=)`.
///
/// # Errors
/// Python's `ValueError`s.
pub fn next_fire_date(
    day_of_month: i64,
    as_of: NaiveDate,
    trading: &BTreeSet<NaiveDate>,
    after: Option<NaiveDate>,
) -> Result<NaiveDate, WorkerError> {
    if !(1..=28).contains(&day_of_month) {
        return Err(value_error(format!(
            "day_of_month must be in 1..28, got {day_of_month}"
        )));
    }
    if trading.is_empty() {
        return Err(value_error("trading_dates cannot be empty"));
    }
    let floor = match after {
        None => as_of,
        Some(a) => as_of.max(a + Duration::days(1)),
    };
    let dom = u32::try_from(day_of_month).unwrap_or(1);
    let (mut year, mut month) = (floor.year(), floor.month());
    for _ in 0..14 {
        let snapped = next_trading_on_or_after(candidate(year, month, dom)?, trading)?;
        if snapped >= floor {
            return Ok(snapped);
        }
        if month == 12 {
            year += 1;
            month = 1;
        } else {
            month += 1;
        }
    }
    Err(value_error(format!(
        "could not resolve next_fire_date for day_of_month={day_of_month} as_of={}",
        iso(as_of)
    )))
}

/// `fire_idempotency_key(plan_id, year, month)`.
///
/// # Errors
/// `ValueError` for a non-positive plan id.
pub fn fire_idempotency_key(plan_id: i64, year: i32, month: u32) -> Result<String, WorkerError> {
    if plan_id < 1 {
        return Err(value_error(format!(
            "plan_id must be positive, got {plan_id}"
        )));
    }
    Ok(format!("{plan_id}:{year:04}-{month:02}"))
}

/// One `cb_sip_plan` row with its investment's user.
#[derive(Debug, Clone)]
pub struct Plan {
    /// `id`.
    pub id: i64,
    /// `investment_id`.
    pub investment_id: i64,
    /// The investment's `user_id`.
    pub user_id: i64,
    /// `amount`.
    pub amount: Dec,
    /// `day_of_month`.
    pub day_of_month: i64,
    /// `mode`.
    pub mode: String,
    /// `status`.
    pub status: String,
    /// `next_fire_date`.
    pub next_fire_date: NaiveDate,
}

/// `evaluate_plan_for_fire(plan, as_of=, trading_dates=, already_fired_keys=)`: the pending
/// action's `(user_id, payload)` and the advanced `next_fire_date`, or `None`.
///
/// # Errors
/// Python's `ValueError`s.
pub fn evaluate_plan_for_fire(
    plan: &Plan,
    as_of: NaiveDate,
    trading: &BTreeSet<NaiveDate>,
    fired: &HashSet<String>,
) -> Result<Option<(OrderedJson, NaiveDate)>, WorkerError> {
    if !(plan.status == SIP_STATUS_ACTIVE && plan.next_fire_date <= as_of) {
        return Ok(None);
    }
    assert_reminder_mode(&plan.mode)?;
    // raise_sip_due
    assert_reminder_mode(&plan.mode)?;
    if plan.amount <= Dec::from(0_i64) {
        return Err(value_error(format!(
            "SIP amount must be positive, got {}",
            plan.amount.to_sci_string()
        )));
    }
    let fire = plan.next_fire_date;
    let key = fire_idempotency_key(plan.id, fire.year(), fire.month())?;
    if fired.contains(&key) {
        return Ok(None);
    }
    let payload = object([
        ("plan_id", int(plan.id)),
        ("investment_id", int(plan.investment_id)),
        ("amount", s(plan.amount.to_string())),
        ("fire_date", s(iso(fire))),
        ("fire_key", s(key)),
        ("mode", s(SIP_MODE_REMINDER)),
    ]);
    let advanced = next_fire_date(plan.day_of_month, fire, trading, Some(fire))?;
    Ok(Some((payload, advanced)))
}

/// `run_curated_sip_reminders(session, as_of)`, in one transaction.
///
/// # Errors
/// A database failure or the calendar's `ValueError` (rolled back).
pub async fn run_sip_reminders(
    ctx: &TaskContext,
    as_of: NaiveDate,
) -> Result<OrderedJson, WorkerError> {
    let mut tx = ctx.db.begin().await?;
    let start = as_of - Duration::days(CALENDAR_LOOKBACK_DAYS);
    let end = as_of + Duration::days(CALENDAR_LOOKAHEAD_DAYS);
    let rows = sqlx::query(
        "SELECT trading_day.date \nFROM trading_day \nWHERE trading_day.exchange_id = \
         $1::SMALLINT AND trading_day.is_trading_day IS true AND trading_day.date >= $2::DATE \
         AND trading_day.date <= $3::DATE",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *tx)
    .await?;
    let mut trading = BTreeSet::new();
    for r in &rows {
        trading.insert(date(r, "date")?);
    }
    if trading.is_empty() {
        tx.commit().await?;
        return Ok(object([
            ("as_of", s(iso(as_of))),
            ("examined", int(0)),
            ("raised", int(0)),
            ("skipped", int(0)),
            ("reason", s("no_trading_dates")),
        ]));
    }

    let mut fired: HashSet<String> = HashSet::new();
    let payloads = sqlx::query(
        "SELECT cb_pending_action.payload \nFROM cb_pending_action \nWHERE \
         cb_pending_action.type = $1::VARCHAR",
    )
    .bind(SIP_DUE)
    .fetch_all(&mut *tx)
    .await?;
    for r in &payloads {
        if let Some(OrderedJson::Str(k)) = jsonb(r, "payload")?.get("fire_key")
            && !k.is_empty()
        {
            fired.insert(k.clone());
        }
    }

    let plan_rows = sqlx::query(
        "SELECT cb_sip_plan.id, cb_sip_plan.investment_id, cb_sip_plan.amount, \
         cb_sip_plan.day_of_month, cb_sip_plan.mode, cb_sip_plan.status, \
         cb_sip_plan.next_fire_date, cb_sip_plan.created_at \nFROM cb_sip_plan \nWHERE \
         cb_sip_plan.status = $1::VARCHAR AND cb_sip_plan.mode = $2::VARCHAR",
    )
    .bind(SIP_STATUS_ACTIVE)
    .bind(SIP_MODE_REMINDER)
    .fetch_all(&mut *tx)
    .await?;

    let mut raised = 0_i64;
    let mut skipped = 0_i64;
    let mut writes: Vec<(i64, String, i64, NaiveDate, bool)> = Vec::new();
    for r in &plan_rows {
        let investment_id: i64 = r.try_get("investment_id")?;
        let user: Option<i64> = sqlx::query_scalar(
            "SELECT cb_investment.user_id \nFROM cb_investment \nWHERE cb_investment.id = \
             $1::BIGINT",
        )
        .bind(investment_id)
        .fetch_optional(&mut *tx)
        .await?;
        let Some(user_id) = user else {
            skipped += 1;
            continue;
        };
        let plan = Plan {
            id: r.try_get("id")?,
            investment_id,
            user_id,
            amount: Dec::from(decimal(r, "amount")?),
            day_of_month: i64::from(r.try_get::<i16, _>("day_of_month")?),
            mode: r.try_get("mode")?,
            status: r.try_get("status")?,
            next_fire_date: date(r, "next_fire_date")?,
        };
        let Some((payload, advanced)) = evaluate_plan_for_fire(&plan, as_of, &trading, &fired)?
        else {
            skipped += 1;
            continue;
        };
        let text = payload
            .to_python_json()
            .map_err(|e| WorkerError::python("TypeError", e.to_string()))?;
        // `session.add(...)` and `plan.next_fire_date = advanced`: nothing reaches the database
        // until the closing `flush()`, so a later plan's ValueError leaves no INSERT behind (and
        // no sequence value consumed) — the rows are written after the loop, in add order.
        writes.push((
            plan.user_id,
            text,
            plan.id,
            advanced,
            advanced != plan.next_fire_date,
        ));
        if let Some(OrderedJson::Str(k)) = payload.get("fire_key") {
            fired.insert(k.clone());
        }
        raised += 1;
    }
    // The ORM flush: the unit of work inserts the new actions, then updates the dirty plans.
    for (user_id, text, _, _, _) in &writes {
        sqlx::query(
            "INSERT INTO cb_pending_action (user_id, type, payload) VALUES ($1::BIGINT, \
             $2::VARCHAR, $3::JSONB)",
        )
        .bind(*user_id)
        .bind(SIP_DUE)
        .bind(text)
        .execute(&mut *tx)
        .await?;
    }
    for (_, _, plan_id, advanced, changed) in &writes {
        if *changed {
            sqlx::query(
                "UPDATE cb_sip_plan SET next_fire_date=$1::DATE WHERE cb_sip_plan.id = $2::BIGINT",
            )
            .bind(*advanced)
            .bind(*plan_id)
            .execute(&mut *tx)
            .await?;
        }
    }
    tx.commit().await?;
    Ok(object([
        ("as_of", s(iso(as_of))),
        (
            "examined",
            int(i64::try_from(plan_rows.len()).unwrap_or(i64::MAX)),
        ),
        ("raised", int(raised)),
        ("skipped", int(skipped)),
    ]))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::*;

    fn day(y: i32, m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, d).unwrap()
    }

    fn plan(next: NaiveDate) -> Plan {
        Plan {
            id: 7,
            investment_id: 3,
            user_id: 42,
            amount: "5000".parse().unwrap(),
            day_of_month: 21,
            mode: SIP_MODE_REMINDER.to_owned(),
            status: SIP_STATUS_ACTIVE.to_owned(),
            next_fire_date: next,
        }
    }

    // test_curated_sip_beat.py::test_run_raises_sip_due_idempotently, on the pure step.
    #[test]
    fn raises_sip_due_once_per_month() {
        let as_of = day(2026, 8, 21);
        let trading: BTreeSet<NaiveDate> = [as_of, day(2026, 9, 21)].into_iter().collect();
        let mut fired = HashSet::new();
        let (payload, advanced) = evaluate_plan_for_fire(&plan(as_of), as_of, &trading, &fired)
            .unwrap()
            .unwrap();
        assert_eq!(
            payload.get("fire_key"),
            Some(&OrderedJson::Str("7:2026-08".to_owned()))
        );
        assert_eq!(
            payload.get("mode"),
            Some(&OrderedJson::Str("REMINDER".to_owned()))
        );
        assert_eq!(
            payload.get("amount"),
            Some(&OrderedJson::Str("5000".to_owned()))
        );
        assert_eq!(advanced, day(2026, 9, 21));
        fired.insert("7:2026-08".to_owned());
        assert!(
            evaluate_plan_for_fire(&plan(as_of), as_of, &trading, &fired)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn not_due_and_auto_refused() {
        let as_of = day(2026, 8, 21);
        let trading: BTreeSet<NaiveDate> = [as_of].into_iter().collect();
        let later = plan(day(2026, 8, 22));
        assert!(
            evaluate_plan_for_fire(&later, as_of, &trading, &HashSet::new())
                .unwrap()
                .is_none()
        );
        let mut auto = plan(as_of);
        auto.mode = "AUTO".to_owned();
        let err = evaluate_plan_for_fire(&auto, as_of, &trading, &HashSet::new()).unwrap_err();
        assert_eq!(
            err.to_string(),
            "SIP write path refuses mode 'AUTO'; AUTO stays dark until D3 (desk non-negotiable #1)"
        );
    }

    #[test]
    fn next_fire_snaps_forward_over_holidays() {
        let trading: BTreeSet<NaiveDate> =
            [day(2026, 9, 22), day(2026, 10, 21)].into_iter().collect();
        assert_eq!(
            next_fire_date(21, day(2026, 9, 1), &trading, None).unwrap(),
            day(2026, 9, 22)
        );
        assert_eq!(
            next_fire_date(21, day(2026, 9, 22), &trading, Some(day(2026, 9, 22))).unwrap(),
            day(2026, 10, 21)
        );
        assert_eq!(
            next_fire_date(29, day(2026, 9, 1), &trading, None)
                .unwrap_err()
                .to_string(),
            "day_of_month must be in 1..28, got 29"
        );
    }
}
