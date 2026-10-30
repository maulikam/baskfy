//! SW11 — three of the swing book's four in-process alert checks (`tasks/swing_ops.py`) and the
//! health facts they read (`baskfy_api.swing_health`). The fourth, `check_detect_fresh` (21:30),
//! is the `swing_eod` lane's.
//!
//! **Law 2.** Every check *reads* the desk's books (`sw_session`, `sw_plan_line`, `sw_position`)
//! and raises an alert; none places, modifies or cancels an order or a GTT.

use baskfy_api::Settings;
use baskfy_db::OrderedJson;
use chrono::{Datelike, NaiveDate, NaiveDateTime};
use sqlx::PgConnection;

use crate::alerts::{Alert, AlertName, Severity, dispatch};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};

/// `NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i16 = 1;
/// `swing_health.BUY_KIND` / `SENT_STATE`.
const BUY_KIND: &str = "BUY_ON_TRIGGER";
const SENT_STATE: &str = "SENT";

/// `swing_ops.RUNBOOK` (`ops.RUNBOOKS[SWING_GTT_MISSING_AT_1515]`).
fn runbook() -> String {
    AlertName::SwingGttMissingAt1515.runbook().to_owned()
}

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `now.astimezone(IST).isoformat(timespec="seconds")` for a naive IST moment.
pub(super) fn ist_seconds(now: NaiveDateTime) -> String {
    format!("{}+05:30", now.format("%Y-%m-%dT%H:%M:%S"))
}

/// `swing_health.is_session_day`: a day the calendar does not carry is a session; a named
/// holiday is not.
pub(super) async fn is_session_day(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<bool, WorkerError> {
    let row: Option<(bool,)> = sqlx::query_as(
        "SELECT trading_day.is_trading_day FROM trading_day \
         WHERE trading_day.exchange_id = $1 AND trading_day.date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.is_none_or(|(known,)| known))
}

/// `swing_ops._not_a_session`: a weekend, or a weekday the NSE calendar names a holiday.
async fn not_a_session(
    conn: &mut PgConnection,
    now: NaiveDateTime,
) -> Result<Option<&'static str>, WorkerError> {
    if now.weekday().num_days_from_monday() >= 5 {
        return Ok(Some("not a weekday"));
    }
    if !is_session_day(conn, now.date()).await? {
        return Ok(Some("an NSE holiday"));
    }
    Ok(None)
}

/// `swing_health.monitor_ran_on`.
async fn monitor_ran_on(conn: &mut PgConnection, day: NaiveDate) -> Result<bool, WorkerError> {
    let (found,): (bool,) = sqlx::query_as(
        "SELECT EXISTS (SELECT * FROM sw_session WHERE sw_session.session_date = $1 \
         AND sw_session.monitor_ran IS true)",
    )
    .bind(day)
    .fetch_one(&mut *conn)
    .await?;
    Ok(found)
}

/// `swing_health.open_buy_orders_on`.
async fn open_buy_orders_on(conn: &mut PgConnection, day: NaiveDate) -> Result<i64, WorkerError> {
    let (count,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM sw_plan_line JOIN sw_plan ON sw_plan.id = sw_plan_line.plan_id \
         WHERE sw_plan.as_of = $1 AND sw_plan_line.kind = $2 AND sw_plan_line.state = $3",
    )
    .bind(day)
    .bind(BUY_KIND)
    .bind(SENT_STATE)
    .fetch_one(&mut *conn)
    .await?;
    Ok(count)
}

/// `swing_health.naked_positions`.
async fn naked_positions(conn: &mut PgConnection) -> Result<i64, WorkerError> {
    let (count,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM sw_position WHERE sw_position.state IN ('OPEN', 'PARTIAL') \
         AND sw_position.quantity_open > 0 AND sw_position.gtt_id IS NULL",
    )
    .fetch_one(&mut *conn)
    .await?;
    Ok(count)
}

fn quiet(day: NaiveDate, reason: &str) -> OrderedJson {
    object([
        ("date", s(iso(day))),
        ("checked", OrderedJson::Bool(false)),
        ("reason", s(reason)),
    ])
}

fn alert(name: AlertName, summary: String, day: NaiveDate, detail: OrderedJson) -> Alert {
    Alert {
        name,
        severity: Severity::Critical,
        summary,
        labels: vec![("trade_date".to_owned(), iso(day))],
        detail,
        runbook: runbook(),
    }
}

/// `check_monitor_started` (09:20): with the flag on, the monitor must have written
/// `monitor_ran` for today.
pub(super) async fn check_monitor_started(
    conn: &mut PgConnection,
    now: NaiveDateTime,
    monitor_enabled: bool,
    settings: &Settings,
) -> Result<OrderedJson, WorkerError> {
    let day = now.date();
    if !monitor_enabled {
        return Ok(quiet(day, "monitor flag is off"));
    }
    if let Some(reason) = not_a_session(conn, now).await? {
        return Ok(quiet(day, reason));
    }
    if monitor_ran_on(conn, day).await? {
        return Ok(object([
            ("date", s(iso(day))),
            ("checked", OrderedJson::Bool(true)),
            ("alert", OrderedJson::Null),
        ]));
    }
    let raised = alert(
        AlertName::SwingMonitorDidNotStart,
        format!(
            "The swing monitor has not started for {} and the flag is on.",
            iso(day)
        ),
        day,
        object([("checked_at", s(ist_seconds(now)))]),
    );
    Ok(object([
        ("date", s(iso(day))),
        ("checked", OrderedJson::Bool(true)),
        ("alert", dispatch(&raised, settings).await),
    ]))
}

/// `check_orders_after_cutoff` (10:50): no BUY line of today may still be `SENT`.
pub(super) async fn check_orders_after_cutoff(
    conn: &mut PgConnection,
    now: NaiveDateTime,
    settings: &Settings,
) -> Result<OrderedJson, WorkerError> {
    let day = now.date();
    if let Some(reason) = not_a_session(conn, now).await? {
        return Ok(quiet(day, reason));
    }
    let open_orders = open_buy_orders_on(conn, day).await?;
    if open_orders == 0 {
        return Ok(object([
            ("date", s(iso(day))),
            ("checked", OrderedJson::Bool(true)),
            ("open_orders", int(0)),
            ("alert", OrderedJson::Null),
        ]));
    }
    let raised = alert(
        AlertName::SwingOrderOpenAfterCutoff,
        format!(
            "{open_orders} swing buy order(s) still open after the 10:45 cutoff on {}.",
            iso(day)
        ),
        day,
        object([("open_orders", int(open_orders))]),
    );
    Ok(object([
        ("date", s(iso(day))),
        ("checked", OrderedJson::Bool(true)),
        ("open_orders", int(open_orders)),
        ("alert", dispatch(&raised, settings).await),
    ]))
}

/// `check_gtt_at_1515` (15:20): after the desk's sweep, no position with shares may be without
/// a GTT (A8).
pub(super) async fn check_gtt_at_1515(
    conn: &mut PgConnection,
    now: NaiveDateTime,
    settings: &Settings,
) -> Result<OrderedJson, WorkerError> {
    let day = now.date();
    if let Some(reason) = not_a_session(conn, now).await? {
        return Ok(quiet(day, reason));
    }
    let naked = naked_positions(conn).await?;
    if naked == 0 {
        return Ok(object([
            ("date", s(iso(day))),
            ("checked", OrderedJson::Bool(true)),
            ("naked", int(0)),
            ("alert", OrderedJson::Null),
        ]));
    }
    let raised = alert(
        AlertName::SwingGttMissingAt1515,
        format!(
            "{naked} swing position(s) have shares open and no GTT after the 15:15 sweep on {} \
             \u{2014} the book goes into the close unprotected.",
            iso(day)
        ),
        day,
        object([("naked_positions", int(naked))]),
    );
    Ok(object([
        ("date", s(iso(day))),
        ("checked", OrderedJson::Bool(true)),
        ("naked", int(naked)),
        ("alert", dispatch(&raised, settings).await),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checked_at_is_ist_to_the_second() {
        let now = NaiveDateTime::parse_from_str("2026-10-30 09:20:07", "%Y-%m-%d %H:%M:%S")
            .unwrap_or_default();
        assert_eq!(ist_seconds(now), "2026-10-30T09:20:07+05:30");
    }

    #[test]
    fn every_swing_alert_shares_the_morning_runbook() {
        assert_eq!(runbook(), "docs/runbooks/06-swing-morning.md");
        assert_eq!(
            AlertName::SwingMonitorDidNotStart.value(),
            "SWING_MONITOR_DID_NOT_START"
        );
    }
}
