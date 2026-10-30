//! Ports `baskfy_worker.fno.alerts`' events (`docs/fno/06` FO12): a plan issued, an exit done,
//! a hard exit tomorrow (from 18:00), a `LATE_EXIT` — read from what the desk wrote and raised
//! once each (a Redis `SET NX` marker per event; Redis unavailable delivers anyway, fail open).
//! **Read-only**: writes no `fo_` row and reaches no broker.

use baskfy_api::Settings;
use baskfy_db::OrderedJson;
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, Utc};
use rust_decimal::Decimal;
use sqlx::postgres::PgRow;
use sqlx::{PgConnection, Row};

use super::alerting::{FnoAlert, FnoName, Level, dispatch};
use super::common::{dstr, fromisoformat_datetime, iso, iso_dt, opt_dstr};
use super::nightly::next_session;
use crate::error::WorkerError;
use crate::pyjson::{object, s};
use crate::steps::log_warning;

/// `MARKER_PREFIX`.
pub(crate) const MARKER_PREFIX: &str = "fno:alert:";
/// `MARKER_TTL_SECONDS`.
pub(crate) const MARKER_TTL_SECONDS: u64 = 3 * 24 * 60 * 60;
/// `LATE_EXIT`.
const LATE_EXIT: &str = "LATE_EXIT";

/// `HARD_EXIT_NOTICE_FROM`: 18:00 IST.
fn notice_from() -> NaiveTime {
    NaiveTime::from_hms_opt(18, 0, 0).unwrap_or(NaiveTime::MIN)
}

/// `Pending`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Pending {
    pub(crate) marker: String,
    pub(crate) alert: FnoAlert,
}

fn mode(simulated: bool) -> &'static str {
    if simulated { "paper" } else { "LIVE" }
}

/// `_day(raw)` for text: an aware stamp's IST date, a naive one's own; `None` when unreadable.
fn day_of_text(raw: &str) -> Option<NaiveDate> {
    if raw.is_empty() {
        return None;
    }
    let (naive, tz) = fromisoformat_datetime(raw).ok()?;
    match tz {
        None => Some(naive.date()),
        Some(tz) => naive.and_local_timezone(tz).single().map(|a| {
            a.with_timezone(&baskfy_providers::tokens::ist())
                .date_naive()
        }),
    }
}

/// One `fo_plan` row, the columns the alerts read.
#[derive(Debug, Clone)]
pub(crate) struct PlanRow {
    pub(crate) plan_id: String,
    pub(crate) sleeve: String,
    pub(crate) symbol: String,
    pub(crate) structure: String,
    pub(crate) trade_date: NaiveDate,
    pub(crate) lots: i16,
    pub(crate) lot_size: i32,
    pub(crate) credit_points: Option<Decimal>,
    pub(crate) debit_points: Option<Decimal>,
    pub(crate) max_loss_inr: Option<Decimal>,
    pub(crate) margin_required_inr: Option<Decimal>,
    pub(crate) hard_exit_date: Option<NaiveDate>,
    pub(crate) expires_at: DateTime<Utc>,
    pub(crate) status: String,
    pub(crate) detail: Option<serde_json::Value>,
}

/// `plan_alert`.
pub(crate) fn plan_alert(plan: &PlanRow) -> FnoAlert {
    let price = match (plan.credit_points, plan.debit_points) {
        (Some(c), _) => format!("credit {} pts", dstr(c)),
        (None, Some(d)) => format!("entry {} pts", dstr(d)),
        (None, None) => "price on the card".to_owned(),
    };
    let ist = baskfy_providers::tokens::ist();
    let expires = plan
        .expires_at
        .with_timezone(&ist)
        .format("%H:%M")
        .to_string();
    let mut parts = vec![
        format!(
            "{} {} {} plan {} issued",
            plan.sleeve, plan.symbol, plan.structure, plan.plan_id
        ),
        format!("{} lot(s) of {}", plan.lots, plan.lot_size),
        price,
    ];
    if let Some(m) = plan.max_loss_inr {
        parts.push(format!("max loss ₹{}", dstr(m)));
    }
    if let Some(h) = plan.hard_exit_date {
        parts.push(format!("hard exit {}", iso(h)));
    }
    parts.push(format!(
        "confirm on the desk's F&O tab by {expires} IST or it lapses"
    ));
    FnoAlert {
        name: FnoName::Plan,
        severity: Level::Warning,
        summary: format!("{}.", parts.join("; ")),
        labels: vec![
            ("sleeve".to_owned(), plan.sleeve.clone()),
            ("symbol".to_owned(), plan.symbol.clone()),
            ("trade_date".to_owned(), iso(plan.trade_date)),
        ],
        detail: object([
            ("plan_id", s(plan.plan_id.clone())),
            ("structure", s(plan.structure.clone())),
            ("lots", OrderedJson::Int(i64::from(plan.lots))),
            ("lot_size", OrderedJson::Int(i64::from(plan.lot_size))),
            ("credit_points", opt_dstr(plan.credit_points)),
            ("debit_points", opt_dstr(plan.debit_points)),
            ("max_loss_inr", opt_dstr(plan.max_loss_inr)),
            ("margin_required_inr", opt_dstr(plan.margin_required_inr)),
            ("expires_at", s(iso_dt(utc_fixed(plan.expires_at)))),
            ("status", s(plan.status.clone())),
        ]),
    }
}

/// An aware UTC moment as Python's `datetime` from the database prints it (`+00:00`).
fn utc_fixed(at: DateTime<Utc>) -> DateTime<FixedOffset> {
    at.fixed_offset()
}

/// One `fo_journal` row, the columns the exit alert reads.
#[derive(Debug, Clone)]
pub(crate) struct JournalRow {
    pub(crate) position_id: i64,
    pub(crate) sleeve: String,
    pub(crate) symbol: String,
    pub(crate) structure: String,
    pub(crate) closed_on: NaiveDate,
    pub(crate) net_pnl_inr: Decimal,
    pub(crate) costs_inr: Decimal,
    pub(crate) r_multiple: Decimal,
    pub(crate) closed_reason: String,
    pub(crate) sessions_held: i32,
    pub(crate) rolls: i16,
    pub(crate) simulated: bool,
}

/// `exit_alert`.
pub(crate) fn exit_alert(row: &JournalRow) -> FnoAlert {
    FnoAlert {
        name: FnoName::Exit,
        severity: Level::Warning,
        summary: format!(
            "{} {} {} {} closed ({}) on {}: net ₹{} after ₹{} costs, {}R, {} session(s), {} \
             roll(s).",
            row.sleeve,
            mode(row.simulated),
            row.symbol,
            row.structure,
            row.closed_reason,
            iso(row.closed_on),
            dstr(row.net_pnl_inr),
            dstr(row.costs_inr),
            dstr(row.r_multiple),
            row.sessions_held,
            row.rolls
        ),
        labels: vec![
            ("sleeve".to_owned(), row.sleeve.clone()),
            ("symbol".to_owned(), row.symbol.clone()),
            ("closed_on".to_owned(), iso(row.closed_on)),
        ],
        detail: object([
            ("position_id", OrderedJson::Int(row.position_id)),
            ("closed_reason", s(row.closed_reason.clone())),
            ("simulated", OrderedJson::Bool(row.simulated)),
            ("net_pnl_inr", s(dstr(row.net_pnl_inr))),
            ("r_multiple", s(dstr(row.r_multiple))),
        ]),
    }
}

/// One open `fo_position`, the columns the hard-exit alert reads.
#[derive(Debug, Clone)]
pub(crate) struct OpenRow {
    pub(crate) id: i64,
    pub(crate) sleeve: String,
    pub(crate) symbol: String,
    pub(crate) structure: String,
    pub(crate) entry_plan_id: String,
    pub(crate) hard_exit_date: Option<NaiveDate>,
    pub(crate) simulated: bool,
    pub(crate) legs: Option<serde_json::Value>,
}

/// `hard_exit_tomorrow_alert`.
pub(crate) fn hard_exit_tomorrow_alert(
    position: &OpenRow,
    exit_on: NaiveDate,
    which: &str,
) -> FnoAlert {
    FnoAlert {
        name: FnoName::HardExitTomorrow,
        severity: Level::Warning,
        summary: format!(
            "{} {} {} {} (position {}) is still open and its {which} is tomorrow, {}: the \
             desk's monitor closes it under the entry's confirm; have the Kite login done before \
             09:15.",
            position.sleeve,
            mode(position.simulated),
            position.symbol,
            position.structure,
            position.id,
            iso(exit_on)
        ),
        labels: vec![
            ("sleeve".to_owned(), position.sleeve.clone()),
            ("symbol".to_owned(), position.symbol.clone()),
            ("exit_on".to_owned(), iso(exit_on)),
        ],
        detail: object([
            ("position_id", OrderedJson::Int(position.id)),
            ("which", s(which)),
            ("entry_plan_id", s(position.entry_plan_id.clone())),
        ]),
    }
}

/// `late_exit_alert`.
pub(crate) fn late_exit_alert(plan: &PlanRow, message: &str, at: &str) -> FnoAlert {
    FnoAlert {
        name: FnoName::LateExit,
        severity: Level::Critical,
        summary: format!(
            "{} {} {} (entry {}) is past its hard exit — LATE_EXIT: {message}. A paper-checklist \
             violation; check the desk's F&O tab that the exit filled.",
            plan.sleeve, plan.symbol, plan.structure, plan.plan_id
        ),
        labels: vec![
            ("sleeve".to_owned(), plan.sleeve.clone()),
            ("symbol".to_owned(), plan.symbol.clone()),
            ("trade_date".to_owned(), iso(plan.trade_date)),
        ],
        detail: object([
            ("plan_id", s(plan.plan_id.clone())),
            ("message", s(message)),
            ("at", s(at)),
        ]),
    }
}

fn hard_exit_of(position: &OpenRow) -> Option<(NaiveDate, &'static str)> {
    if let Some(day) = position.hard_exit_date {
        return Some((day, "hard exit"));
    }
    let carry = position.legs.as_ref()?.get("carry")?;
    let raw = carry.get("time_exit_date")?.as_str()?;
    day_of_text(raw).map(|d| (d, "time exit"))
}

/// `str(value or "")` for a JSON member.
fn text_or_empty(value: Option<&serde_json::Value>) -> String {
    match value {
        None | Some(serde_json::Value::Null | serde_json::Value::Bool(false)) => String::new(),
        Some(serde_json::Value::String(t)) => t.clone(),
        Some(serde_json::Value::Bool(true)) => "True".to_owned(),
        Some(other) => other.to_string(),
    }
}

impl PlanRow {
    fn read(r: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            plan_id: r.try_get("plan_id")?,
            sleeve: r.try_get("sleeve")?,
            symbol: r.try_get("symbol")?,
            structure: r.try_get("structure")?,
            trade_date: r.try_get("trade_date")?,
            lots: r.try_get("lots")?,
            lot_size: r.try_get("lot_size")?,
            credit_points: r.try_get("credit_points")?,
            debit_points: r.try_get("debit_points")?,
            max_loss_inr: r.try_get("max_loss_inr")?,
            margin_required_inr: r.try_get("margin_required_inr")?,
            hard_exit_date: r.try_get("hard_exit_date")?,
            expires_at: r.try_get("expires_at")?,
            status: r.try_get("status")?,
            detail: r.try_get("detail")?,
        })
    }
}

impl JournalRow {
    fn read(r: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            position_id: r.try_get("position_id")?,
            sleeve: r.try_get("sleeve")?,
            symbol: r.try_get("symbol")?,
            structure: r.try_get("structure")?,
            closed_on: r.try_get("closed_on")?,
            net_pnl_inr: r.try_get("net_pnl_inr")?,
            costs_inr: r.try_get("costs_inr")?,
            r_multiple: r.try_get("r_multiple")?,
            closed_reason: r.try_get("closed_reason")?,
            sessions_held: r.try_get("sessions_held")?,
            rolls: r.try_get("rolls")?,
            simulated: r.try_get("simulated")?,
        })
    }
}

impl OpenRow {
    fn read(r: &PgRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            id: r.try_get("id")?,
            sleeve: r.try_get("sleeve")?,
            symbol: r.try_get("symbol")?,
            structure: r.try_get("structure")?,
            entry_plan_id: r.try_get("entry_plan_id")?,
            hard_exit_date: r.try_get("hard_exit_date")?,
            simulated: r.try_get("simulated")?,
            legs: r.try_get("legs")?,
        })
    }
}

fn read_all<T>(
    rows: &[PgRow],
    f: fn(&PgRow) -> Result<T, sqlx::Error>,
) -> Result<Vec<T>, WorkerError> {
    rows.iter()
        .map(|r| f(r).map_err(WorkerError::from))
        .collect()
}

const PLAN_COLUMNS: &str = "plan_id, sleeve::text AS sleeve, symbol, structure, trade_date, \
     lots, lot_size, credit_points, debit_points, max_loss_inr, margin_required_inr, \
     hard_exit_date, expires_at, status, detail";

/// `pending_events`: every FO event of `now`'s IST day for `user_id`, each with its marker.
pub(crate) async fn pending_events(
    conn: &mut PgConnection,
    user_id: i64,
    now: DateTime<FixedOffset>,
) -> Result<Vec<Pending>, WorkerError> {
    let ist = baskfy_providers::tokens::ist();
    let local = now.with_timezone(&ist);
    let today = local.date_naive();
    let start = today
        .and_time(NaiveTime::MIN)
        .and_local_timezone(ist)
        .single()
        .ok_or_else(|| WorkerError::python("ValueError", "IST day"))?;
    let end = start + chrono::Duration::days(1);
    let mut out = Vec::new();

    let plans: Vec<PgRow> = sqlx::query(&format!(
        "SELECT {PLAN_COLUMNS} FROM fo_plan WHERE user_id = $1 AND kind = 'ENTRY' \
         AND issued_at >= $2 AND issued_at < $3 ORDER BY issued_at, id"
    ))
    .bind(user_id)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    let plans = read_all(&plans, PlanRow::read)?;
    for plan in &plans {
        if !plan.status.starts_with("REJECTED") {
            out.push(Pending {
                marker: format!("{MARKER_PREFIX}plan:{user_id}:{}", plan.plan_id),
                alert: plan_alert(plan),
            });
        }
    }

    let journal: Vec<PgRow> = sqlx::query(
        "SELECT position_id, sleeve::text AS sleeve, symbol, structure, closed_on, net_pnl_inr, \
         costs_inr, r_multiple, closed_reason, sessions_held, rolls, simulated FROM fo_journal \
         WHERE user_id = $1 AND closed_on = $2 ORDER BY position_id",
    )
    .bind(user_id)
    .bind(today)
    .fetch_all(&mut *conn)
    .await?;
    let journal = read_all(&journal, JournalRow::read)?;
    for row in &journal {
        out.push(Pending {
            marker: format!("{MARKER_PREFIX}exit:{user_id}:{}", row.position_id),
            alert: exit_alert(row),
        });
    }

    let flagged: Vec<PgRow> = sqlx::query(&format!(
        "SELECT {PLAN_COLUMNS} FROM fo_plan WHERE user_id = $1 AND kind = 'ENTRY' \
         AND (detail -> 'violations') IS NOT NULL ORDER BY id"
    ))
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    let flagged = read_all(&flagged, PlanRow::read)?;
    for plan in &flagged {
        let Some(serde_json::Value::Array(items)) =
            plan.detail.as_ref().and_then(|d| d.get("violations"))
        else {
            continue;
        };
        for item in items {
            if !item.is_object() || item.get("code").and_then(|c| c.as_str()) != Some(LATE_EXIT) {
                continue;
            }
            let at = text_or_empty(item.get("at"));
            if day_of_text(&at) != Some(today) {
                continue;
            }
            let message = text_or_empty(item.get("message"));
            out.push(Pending {
                marker: format!("{MARKER_PREFIX}late:{user_id}:{}:{at}", plan.plan_id),
                alert: late_exit_alert(plan, &message, &at),
            });
        }
    }

    if local.time() >= notice_from()
        && let Some(tomorrow) = next_session(conn, today).await?
    {
        let open: Vec<PgRow> = sqlx::query(
            "SELECT id, sleeve::text AS sleeve, symbol, structure, entry_plan_id, \
             hard_exit_date, simulated, legs FROM fo_position WHERE user_id = $1 \
             AND closed_at IS NULL ORDER BY id",
        )
        .bind(user_id)
        .fetch_all(&mut *conn)
        .await?;
        let open = read_all(&open, OpenRow::read)?;
        for position in &open {
            if let Some((day, which)) = hard_exit_of(position)
                && day == tomorrow
            {
                out.push(Pending {
                    marker: format!(
                        "{MARKER_PREFIX}hard:{user_id}:{}:{}",
                        position.id,
                        iso(tomorrow)
                    ),
                    alert: hard_exit_tomorrow_alert(position, tomorrow, which),
                });
            }
        }
    }
    Ok(out)
}

/// `claim`: `SET NX` the marker — true when this run is the first to see the event, or when
/// Redis cannot say (fail open).
pub(crate) async fn claim(cache: Option<&redis::Client>, marker: &str) -> bool {
    let Some(client) = cache else {
        return true;
    };
    let answer: Result<Option<String>, String> = async {
        let mut conn = client
            .get_multiplexed_async_connection()
            .await
            .map_err(|e| e.to_string())?;
        redis::cmd("SET")
            .arg(marker)
            .arg("1")
            .arg("NX")
            .arg("EX")
            .arg(MARKER_TTL_SECONDS)
            .query_async(&mut conn)
            .await
            .map_err(|e| e.to_string())
    }
    .await;
    match answer {
        Ok(claimed) => claimed.is_some(),
        Err(e) => {
            log_warning(&format!(
                "fno alerts: Redis unavailable; delivering without de-duplication: {e}"
            ));
            true
        }
    }
}

/// `run_fno_alerts`: raise each of today's unseen FO events once, per tenant.
pub(crate) async fn run_fno_alerts(
    conn: &mut PgConnection,
    user_ids: &[i64],
    cache: Option<&redis::Client>,
    now: DateTime<FixedOffset>,
    settings: &Settings,
    raised_at: impl Fn() -> DateTime<Utc>,
) -> Result<OrderedJson, WorkerError> {
    let mut sent = Vec::new();
    let mut seen = 0_i64;
    for user_id in user_ids {
        for event in pending_events(conn, *user_id, now).await? {
            seen += 1;
            if !claim(cache, &event.marker).await {
                continue;
            }
            let payload = dispatch(&event.alert, settings, raised_at()).await;
            sent.push(object([
                ("marker", s(event.marker.clone())),
                ("alert", s(event.alert.name.value())),
                (
                    "delivered_to",
                    payload
                        .get("delivered_to")
                        .cloned()
                        .unwrap_or(OrderedJson::Null),
                ),
            ]));
        }
    }
    Ok(object([
        ("at", s(iso_dt(now))),
        ("events", OrderedJson::Int(seen)),
        ("sent", OrderedJson::Array(sent)),
    ]))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn days_read_aware_stamps_in_ist() {
        assert_eq!(
            day_of_text("2026-10-29T20:00:00+00:00"),
            NaiveDate::from_ymd_opt(2026, 10, 30)
        );
        assert_eq!(
            day_of_text("2026-10-29T20:00:00"),
            NaiveDate::from_ymd_opt(2026, 10, 29)
        );
        assert_eq!(day_of_text("nope"), None);
        assert_eq!(day_of_text(""), None);
    }

    #[test]
    fn the_plan_alert_names_the_lapse_in_ist() {
        let plan = PlanRow {
            plan_id: "p1".to_owned(),
            sleeve: "F1N".to_owned(),
            symbol: "NIFTY".to_owned(),
            structure: "IRON_CONDOR".to_owned(),
            trade_date: NaiveDate::from_ymd_opt(2026, 10, 29).unwrap(),
            lots: 1,
            lot_size: 75,
            credit_points: Some("85.50".parse().unwrap()),
            debit_points: None,
            max_loss_inr: Some("12000.00".parse().unwrap()),
            margin_required_inr: None,
            hard_exit_date: None,
            expires_at: "2026-10-29T04:20:00Z".parse().unwrap(),
            status: "ISSUED".to_owned(),
            detail: None,
        };
        let alert = plan_alert(&plan);
        assert_eq!(
            alert.summary,
            "F1N NIFTY IRON_CONDOR plan p1 issued; 1 lot(s) of 75; credit 85.50 pts; max loss \
             ₹12000.00; confirm on the desk's F&O tab by 09:50 IST or it lapses."
        );
        assert_eq!(
            alert.detail.get("expires_at"),
            Some(&s("2026-10-29T04:20:00+00:00"))
        );
    }
    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    fn plan() -> PlanRow {
        PlanRow {
            plan_id: "F1N-20310725-1".to_owned(),
            sleeve: "F1N".to_owned(),
            symbol: "NIFTY".to_owned(),
            structure: "IRON_CONDOR".to_owned(),
            trade_date: d(2031, 7, 25),
            lots: 2,
            lot_size: 75,
            credit_points: Some("112.40".parse().unwrap()),
            debit_points: None,
            max_loss_inr: Some("24500.00".parse().unwrap()),
            margin_required_inr: None,
            hard_exit_date: Some(d(2031, 8, 21)),
            expires_at: "2031-07-25T04:20:00Z".parse().unwrap(),
            status: "ISSUED".to_owned(),
            detail: None,
        }
    }

    fn journal(simulated: bool) -> JournalRow {
        JournalRow {
            position_id: 11,
            sleeve: "F2".to_owned(),
            symbol: "ZQA".to_owned(),
            structure: "FUTURE".to_owned(),
            closed_on: d(2031, 7, 25),
            net_pnl_inr: "3019.50".parse().unwrap(),
            costs_inr: "180.50".parse().unwrap(),
            r_multiple: "0.30".parse().unwrap(),
            closed_reason: "STOP".to_owned(),
            sessions_held: 1,
            rolls: 0,
            simulated,
        }
    }

    fn position(hard_exit: Option<NaiveDate>, legs: Option<serde_json::Value>) -> OpenRow {
        OpenRow {
            id: 21,
            sleeve: "F1B".to_owned(),
            symbol: "BANKNIFTY".to_owned(),
            structure: "IRON_CONDOR".to_owned(),
            entry_plan_id: "F1B-20310701-1".to_owned(),
            hard_exit_date: hard_exit,
            simulated: true,
            legs,
        }
    }

    /// `test_a_plan_alert_carries_what_the_confirm_needs`.
    #[test]
    fn a_plan_alert_carries_what_the_confirm_needs() {
        let alert = plan_alert(&plan());
        assert_eq!(
            (alert.name, alert.severity),
            (FnoName::Plan, Level::Warning)
        );
        for text in [
            "F1N NIFTY IRON_CONDOR plan F1N-20310725-1 issued",
            "2 lot(s) of 75",
            "credit 112.40 pts",
            "max loss ₹24500.00",
            "hard exit 2031-08-21",
            "by 09:50 IST or it lapses",
        ] {
            assert!(alert.summary.contains(text), "{text}");
        }
        let mut debit = plan();
        debit.credit_points = None;
        debit.debit_points = Some("40.00".parse().unwrap());
        assert!(plan_alert(&debit).summary.contains("entry 40.00 pts"));
        debit.debit_points = None;
        assert!(plan_alert(&debit).summary.contains("price on the card"));
    }

    /// `test_an_exit_alert_names_its_mode_and_is_in_rupees_and_r`.
    #[test]
    fn an_exit_alert_names_its_mode_and_is_in_rupees_and_r() {
        let summary = exit_alert(&journal(true)).summary;
        for text in [
            "F2 paper ZQA FUTURE closed (STOP) on 2031-07-25",
            "net ₹3019.50",
            "₹180.50 costs",
            "0.30R",
        ] {
            assert!(summary.contains(text), "{text}");
        }
        assert!(exit_alert(&journal(false)).summary.contains("F2 LIVE"));
    }

    /// `test_hard_exit_tomorrow_and_late_exit`.
    #[test]
    fn hard_exit_tomorrow_and_late_exit() {
        let notice = hard_exit_tomorrow_alert(
            &position(Some(d(2031, 7, 28)), None),
            d(2031, 7, 28),
            "hard exit",
        );
        assert!(
            notice
                .summary
                .contains("F1B paper BANKNIFTY IRON_CONDOR (position 21)")
        );
        assert!(notice.summary.contains("hard exit is tomorrow, 2031-07-28"));
        let late = late_exit_alert(
            &plan(),
            "open past the 15:00 hard exit",
            "2031-07-25T15:05:00+05:30",
        );
        assert_eq!(
            (late.name, late.severity),
            (FnoName::LateExit, Level::Critical)
        );
        assert!(
            late.summary
                .contains("LATE_EXIT: open past the 15:00 hard exit")
        );
    }

    /// The F2 carry's `time_exit_date` stands in for a missing hard exit.
    #[test]
    fn a_carry_time_exit_is_read_when_no_hard_exit_is_set() {
        let carry = serde_json::json!({"legs": [], "carry": {"time_exit_date": "2031-07-28"}});
        assert_eq!(
            hard_exit_of(&position(None, Some(carry))),
            Some((d(2031, 7, 28), "time exit"))
        );
        assert_eq!(
            hard_exit_of(&position(Some(d(2031, 8, 21)), None)),
            Some((d(2031, 8, 21), "hard exit"))
        );
        assert_eq!(
            hard_exit_of(&position(None, Some(serde_json::json!({"legs": []})))),
            None
        );
    }

    /// `claim` fails open: no cache, or a cache that cannot answer, delivers.
    #[tokio::test]
    async fn claim_fails_open() {
        assert!(claim(None, "fno:alert:x").await);
        let down = redis::Client::open("redis://127.0.0.1:1/0").unwrap();
        assert!(claim(Some(&down), "fno:alert:x").await);
    }
}
