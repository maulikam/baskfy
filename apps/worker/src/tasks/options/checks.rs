//! The options book's in-process checks (`options.checks`, OP14), each run at the minute it is
//! about; a failed one is one `OPTIONS_CHECK_FAILED`. A check whose switch is off is `skipped`,
//! never a failure. `FLAT_AFTER_HARD_EXIT` runs whatever the flags say. Read-only.

use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveTime, Timelike};
use sqlx::PgConnection;

use super::alerting::{Level, OptionsAlert, OptionsName, RUNBOOK_HEALTH};
use super::collector::load_contracts;
use super::common::{in_ist, ist_at};
use super::model::{SLEEVES, hard_exit_time, hm, sleeve_of, sleeve_value, trades_on};
use crate::error::WorkerError;
use crate::pyjson::{object, s};
use baskfy_core::options::gating::Sleeve;
use baskfy_db::OrderedJson;

const COLLECTOR_MIN_MINUTES: i64 = 360;

/// `checks.CheckResult`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CheckResult {
    pub(crate) name: &'static str,
    pub(crate) ok: bool,
    pub(crate) detail: String,
    pub(crate) skipped: bool,
}

impl CheckResult {
    fn new(name: &'static str, ok: bool, detail: impl Into<String>) -> Self {
        Self {
            name,
            ok,
            detail: detail.into(),
            skipped: false,
        }
    }

    fn skip(name: &'static str, detail: impl Into<String>) -> Self {
        Self {
            name,
            ok: true,
            detail: detail.into(),
            skipped: true,
        }
    }

    /// `as_dict()`.
    pub(crate) fn as_dict(&self) -> OrderedJson {
        object([
            ("name", s(self.name)),
            ("ok", OrderedJson::Bool(self.ok)),
            ("skipped", OrderedJson::Bool(self.skipped)),
            ("detail", s(self.detail.clone())),
        ])
    }
}

/// `checks.Switches`.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Switches {
    pub(crate) scan_enabled: bool,
    pub(crate) collect_enabled: bool,
    pub(crate) monitor_enabled: bool,
}

/// `hard_exit_checks(options)`: each distinct hard exit + 3 minutes, ascending.
fn hard_exit_checks() -> Vec<NaiveTime> {
    let mut times: Vec<NaiveTime> = SLEEVES
        .into_iter()
        .map(|s| hard_exit_time(s) + Duration::minutes(3))
        .collect();
    times.sort();
    times.dedup();
    times
}

/// `due_checks(now, options)`: the checks whose minute `now` is (IST, to the minute).
pub(crate) fn due_checks(now: DateTime<FixedOffset>) -> Vec<&'static str> {
    let clock = in_ist(now).time();
    let minute = clock
        .with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .unwrap_or(clock);
    let mut due = Vec::new();
    if minute == hm(9, 20) {
        due.push("SCAN_STARTED");
    }
    if minute == hm(10, 20) {
        due.push("O1_DECIDED");
    }
    if hard_exit_checks().contains(&minute) {
        due.push("FLAT_AFTER_HARD_EXIT");
    }
    if minute == hm(15, 35) {
        due.push("COLLECTOR_FULL_DAY");
    }
    due
}

async fn scan_started(
    conn: &mut PgConnection,
    now: DateTime<FixedOffset>,
    switches: Switches,
) -> Result<CheckResult, WorkerError> {
    const NAME: &str = "SCAN_STARTED";
    if !switches.scan_enabled {
        return Ok(CheckResult::skip(
            NAME,
            "BASKFY_OPTIONS_SCAN_ENABLED is false",
        ));
    }
    let start = ist_at(in_ist(now).date_naive(), hm(9, 15))?;
    let seen: Vec<String> = sqlx::query_scalar(
        "SELECT DISTINCT op_scan.sleeve::text FROM op_scan WHERE op_scan.ts >= $1",
    )
    .bind(start)
    .fetch_all(&mut *conn)
    .await?;
    let mut missing: Vec<&str> = SLEEVES
        .into_iter()
        .map(sleeve_value)
        .filter(|v| !seen.iter().any(|s| s == v))
        .collect();
    missing.sort_unstable();
    if missing.is_empty() {
        return Ok(CheckResult::new(
            NAME,
            true,
            "every sleeve has a scan row today",
        ));
    }
    Ok(CheckResult::new(
        NAME,
        false,
        format!("no scan row since 09:15 for {}", missing.join(", ")),
    ))
}

async fn o1_decided(
    conn: &mut PgConnection,
    now: DateTime<FixedOffset>,
    switches: Switches,
) -> Result<CheckResult, WorkerError> {
    const NAME: &str = "O1_DECIDED";
    if !switches.monitor_enabled {
        return Ok(CheckResult::skip(
            NAME,
            "BASKFY_OPTIONS_MONITOR_ENABLED is false",
        ));
    }
    let day = in_ist(now).date_naive();
    let contracts = load_contracts(conn).await?;
    let events: Vec<NaiveDate> = sqlx::query_scalar("SELECT op_event_day.date FROM op_event_day")
        .fetch_all(&mut *conn)
        .await?;
    let mut o1_today: Vec<Sleeve> = Vec::new();
    for sleeve in [Sleeve::O1m, Sleeve::O1w] {
        if trades_on(day, sleeve, &contracts, &events, true)? {
            o1_today.push(sleeve);
        }
    }
    if o1_today.is_empty() {
        return Ok(CheckResult::skip(NAME, "not an O1 day"));
    }
    let values: Vec<String> = o1_today
        .iter()
        .map(|s| sleeve_value(*s).to_owned())
        .collect();
    let decided: Vec<String> = sqlx::query_scalar(
        "SELECT DISTINCT op_session.sleeve::text FROM op_session \
         WHERE op_session.trade_date = $1 AND op_session.sleeve::text = ANY($2)",
    )
    .bind(day)
    .bind(&values)
    .fetch_all(&mut *conn)
    .await?;
    if let Some(missing) = values.iter().find(|v| !decided.contains(v)) {
        return Ok(CheckResult::new(
            NAME,
            false,
            format!("no verdict or plan by 10:20 for {missing}"),
        ));
    }
    Ok(CheckResult::new(
        NAME,
        true,
        format!("{} decided", values.join(", ")),
    ))
}

async fn flat_after_hard_exit(
    conn: &mut PgConnection,
    now: DateTime<FixedOffset>,
) -> Result<CheckResult, WorkerError> {
    const NAME: &str = "FLAT_AFTER_HARD_EXIT";
    let rows: Vec<(String, NaiveDate, Option<String>)> = sqlx::query_as(
        "SELECT op_session.sleeve::text, op_session.trade_date, op_session.plan_id \
         FROM op_session JOIN op_position ON op_position.session_id = op_session.id \
         WHERE op_position.closed_at IS NULL",
    )
    .fetch_all(&mut *conn)
    .await?;
    let mut late: Vec<String> = Vec::new();
    for (sleeve, trade_date, plan_id) in rows {
        let exit = ist_at(trade_date, hard_exit_time(sleeve_of(&sleeve)?))? + Duration::minutes(3);
        if now >= exit {
            late.push(format!("{sleeve} {}", plan_id.as_deref().unwrap_or("None")));
        }
    }
    if late.is_empty() {
        return Ok(CheckResult::new(
            NAME,
            true,
            "nothing open past its hard exit",
        ));
    }
    Ok(CheckResult::new(
        NAME,
        false,
        format!("still open: {}", late.join("; ")),
    ))
}

async fn collector_full_day(
    conn: &mut PgConnection,
    now: DateTime<FixedOffset>,
    switches: Switches,
) -> Result<CheckResult, WorkerError> {
    const NAME: &str = "COLLECTOR_FULL_DAY";
    if !switches.collect_enabled {
        return Ok(CheckResult::skip(
            NAME,
            "BASKFY_OPTIONS_COLLECT_ENABLED is false",
        ));
    }
    let day = in_ist(now).date_naive();
    let minutes: i64 = sqlx::query_scalar(
        "SELECT count(DISTINCT op_chain_snapshot.ts) FROM op_chain_snapshot \
         WHERE op_chain_snapshot.ts >= $1 AND op_chain_snapshot.ts < $2",
    )
    .bind(ist_at(day, hm(9, 15))?)
    .bind(ist_at(day, hm(15, 30))?)
    .fetch_one(&mut *conn)
    .await?;
    if minutes < COLLECTOR_MIN_MINUTES {
        return Ok(CheckResult::new(
            NAME,
            false,
            format!("{minutes} chain minutes today, fewer than 360 of 375"),
        ));
    }
    Ok(CheckResult::new(
        NAME,
        true,
        format!("{minutes} chain minutes today"),
    ))
}

/// `run_checks(session, names, now, switches)` (the caller has asked the NSE calendar).
pub(crate) async fn run_checks(
    conn: &mut PgConnection,
    names: &[&'static str],
    now: DateTime<FixedOffset>,
    switches: Switches,
) -> Result<Vec<CheckResult>, WorkerError> {
    let mut out = Vec::with_capacity(names.len());
    for name in names {
        out.push(match *name {
            "SCAN_STARTED" => scan_started(conn, now, switches).await?,
            "O1_DECIDED" => o1_decided(conn, now, switches).await?,
            "FLAT_AFTER_HARD_EXIT" => flat_after_hard_exit(conn, now).await?,
            "COLLECTOR_FULL_DAY" => collector_full_day(conn, now, switches).await?,
            other => {
                return Err(WorkerError::python(
                    "ValueError",
                    format!(
                        "unknown options check {}",
                        baskfy_providers::py::repr_str(other)
                    ),
                ));
            }
        });
    }
    Ok(out)
}

/// `check_alert(result, now)`: one failed check as `OPTIONS_CHECK_FAILED`.
pub(crate) fn check_alert(result: &CheckResult, now: DateTime<FixedOffset>) -> OptionsAlert {
    let critical = result.name == "FLAT_AFTER_HARD_EXIT";
    OptionsAlert {
        name: OptionsName::CheckFailed,
        severity: if critical {
            Level::Critical
        } else {
            Level::Warning
        },
        summary: format!(
            "Options check {} failed at {}: {}",
            result.name,
            in_ist(now).format("%H:%M"),
            result.detail
        ),
        labels: vec![("check".to_owned(), result.name.to_owned())],
        detail: object::<&str>([]),
        runbook: RUNBOOK_HEALTH,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn at(h: u32, m: u32, s: u32) -> DateTime<FixedOffset> {
        ist_at(
            NaiveDate::from_ymd_opt(2026, 10, 29).unwrap(),
            NaiveTime::from_hms_opt(h, m, s).unwrap(),
        )
        .unwrap()
    }

    #[test]
    fn each_check_is_due_at_its_minute_and_only_then() {
        assert_eq!(due_checks(at(9, 20, 40)), ["SCAN_STARTED"]);
        assert_eq!(due_checks(at(10, 20, 0)), ["O1_DECIDED"]);
        assert_eq!(due_checks(at(14, 33, 0)), ["FLAT_AFTER_HARD_EXIT"]);
        assert_eq!(due_checks(at(14, 48, 5)), ["FLAT_AFTER_HARD_EXIT"]);
        assert_eq!(due_checks(at(15, 3, 0)), ["FLAT_AFTER_HARD_EXIT"]);
        assert_eq!(due_checks(at(15, 35, 0)), ["COLLECTOR_FULL_DAY"]);
        assert!(due_checks(at(9, 21, 0)).is_empty());
        assert!(due_checks(at(14, 30, 0)).is_empty());
    }

    #[test]
    fn a_failed_flat_check_is_critical() {
        let failed = CheckResult::new("FLAT_AFTER_HARD_EXIT", false, "still open: O2 p1");
        let alert = check_alert(&failed, at(15, 3, 20));
        assert_eq!(alert.severity, Level::Critical);
        assert_eq!(
            alert.summary,
            "Options check FLAT_AFTER_HARD_EXIT failed at 15:03: still open: O2 p1"
        );
        let other = check_alert(&CheckResult::new("SCAN_STARTED", false, "x"), at(9, 20, 0));
        assert_eq!(other.severity, Level::Warning);
    }
}
