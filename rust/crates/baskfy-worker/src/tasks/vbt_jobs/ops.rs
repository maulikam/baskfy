//! The four checks behind the volume-breakout sleeve's alerts (`docs/vbt/05` §4). Ports
//! `baskfy_worker.tasks.vbt_ops`:
//!
//! | Beat | Alert | Condition |
//! |---|---|---|
//! | 21:30 | `VBT_DETECT_STALE` | no `vb_breadth_daily` row in the last four days |
//! | 21:40 | `VBT_ORDER_PAST_EXPIRY` | a working limit still live after its third session |
//! | 21:40 | `VBT_POSITION_NAKED` | shares open with no resting GTT |
//! | 21:40 | `VBT_POSITION_NO_BAR` | a held name that has not printed for more than seven days |
//!
//! **A check only tells**: it reads and, when something is wrong, dispatches one alert (the log
//! line always, the mail when `BASKFY_OPS_ALERT_EMAIL` is set). It writes no row and cannot fix
//! anything — the cancel and the GTT are order-shaped and belong to the desk's confirm. Silent on
//! a weekend: there is no evening on a Saturday.
//!
//! The alerts go through the `twt_jobs` lane's shared sleeve-alert dispatch (runbook 8).

use std::collections::BTreeMap;

use baskfy_api::Settings;
use baskfy_db::OrderedJson;
use chrono::{DateTime, Datelike, Duration, NaiveDate, Utc, Weekday};
use sqlx::{PgConnection, Row as _};

use super::evening::{IST_MINUTES, iso};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::tasks::twt_jobs::alerting::{SleeveAlert, VBT_RUNBOOK, dispatch};

/// `_QUIET_DAYS`: calendar days, not sessions.
const QUIET_DAYS: i64 = 7;

/// `VBT_DETECT_STALE`'s window: a breadth row at most four days old is fresh.
const FRESH_DAYS: i64 = 4;

/// What a check needs besides the database.
pub(crate) struct CheckInputs<'a> {
    pub(crate) now: DateTime<Utc>,
    pub(crate) user_id: i64,
    pub(crate) settings: &'a Settings,
}

impl CheckInputs<'_> {
    /// `now.astimezone(IST).date()`.
    fn day(&self) -> NaiveDate {
        (self.now + Duration::minutes(IST_MINUTES)).date_naive()
    }
}

/// `_weekday(now)`: Monday to Friday in IST.
fn weekday(day: NaiveDate) -> bool {
    !matches!(day.weekday(), Weekday::Sat | Weekday::Sun)
}

fn not_a_weekday(day: NaiveDate) -> OrderedJson {
    object([
        ("date", s(iso(day))),
        ("checked", OrderedJson::Bool(false)),
        ("reason", s("not a weekday")),
    ])
}

fn trade_date_label(day: NaiveDate) -> Vec<(String, String)> {
    vec![("trade_date".to_owned(), iso(day))]
}

fn opt_iso(d: Option<NaiveDate>) -> OrderedJson {
    d.map_or(OrderedJson::Null, |d| s(iso(d)))
}

/// `check_detect_fresh` (21:30): the sleeve must have a breadth row by the end of the evening.
pub(crate) async fn check_detect_fresh(
    conn: &mut PgConnection,
    inputs: &CheckInputs<'_>,
) -> Result<OrderedJson, WorkerError> {
    let day = inputs.day();
    if !weekday(day) {
        return Ok(not_a_weekday(day));
    }
    let (latest,): (Option<NaiveDate>,) =
        sqlx::query_as("SELECT max(date) FROM vb_breadth_daily WHERE user_id = $1")
            .bind(inputs.user_id)
            .fetch_one(&mut *conn)
            .await?;
    if let Some(latest) = latest
        && latest >= day - Duration::days(FRESH_DAYS)
    {
        return Ok(object([
            ("date", s(iso(day))),
            ("checked", OrderedJson::Bool(true)),
            ("latest", s(iso(latest))),
            ("alert", OrderedJson::Null),
        ]));
    }
    let alert = SleeveAlert {
        name: "VBT_DETECT_STALE",
        severity: "warning",
        summary: format!(
            "The volume-breakout detector has written nothing since {} (checked {}).",
            latest.map_or_else(|| "never".to_owned(), iso),
            iso(day)
        ),
        labels: trade_date_label(day),
        detail: object([("latest", opt_iso(latest))]),
        runbook: VBT_RUNBOOK,
    };
    Ok(object([
        ("date", s(iso(day))),
        ("checked", OrderedJson::Bool(true)),
        ("alert", dispatch(&alert, inputs.settings, inputs.now).await),
    ]))
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `check_orders_past_expiry` (21:40): a limit that has outlived its window is still an order.
pub(crate) async fn check_orders_past_expiry(
    conn: &mut PgConnection,
    inputs: &CheckInputs<'_>,
) -> Result<OrderedJson, WorkerError> {
    let day = inputs.day();
    if !weekday(day) {
        return Ok(not_a_weekday(day));
    }
    let rows = sqlx::query(
        "SELECT id, instrument_id, signal_date, expires_after_session, state FROM vb_order \
         WHERE user_id = $1 AND state = ANY($2) AND expires_after_session IS NOT NULL \
         AND expires_after_session < $3",
    )
    .bind(inputs.user_id)
    .bind(&["PROPOSED", "CONFIRMED", "SENT", "PARTIAL"][..])
    .bind(day)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        return Ok(object([
            ("date", s(iso(day))),
            ("checked", OrderedJson::Bool(true)),
            ("past_expiry", int(0)),
            ("alert", OrderedJson::Null),
        ]));
    }
    let mut orders = Vec::new();
    for row in rows.iter().take(10) {
        let signal: NaiveDate = row.try_get("signal_date")?;
        let expires: Option<NaiveDate> = row.try_get("expires_after_session")?;
        let state: String = row.try_get("state")?;
        orders.push(object([
            ("id", int(row.try_get("id")?)),
            ("instrument_id", int(row.try_get("instrument_id")?)),
            ("signal_date", s(iso(signal))),
            ("expires_after_session", opt_iso(expires)),
            ("state", s(state)),
        ]));
    }
    let n = len_i64(rows.len());
    let alert = SleeveAlert {
        name: "VBT_ORDER_PAST_EXPIRY",
        severity: "critical",
        summary: format!(
            "{n} volume-breakout limit order(s) are still working past their third session on {}.",
            iso(day)
        ),
        labels: trade_date_label(day),
        detail: object([("orders", OrderedJson::Array(orders))]),
        runbook: VBT_RUNBOOK,
    };
    Ok(object([
        ("date", s(iso(day))),
        ("checked", OrderedJson::Bool(true)),
        ("past_expiry", int(n)),
        ("alert", dispatch(&alert, inputs.settings, inputs.now).await),
    ]))
}

/// `check_naked_positions` (21:40): shares open and no resting GTT.
pub(crate) async fn check_naked_positions(
    conn: &mut PgConnection,
    inputs: &CheckInputs<'_>,
) -> Result<OrderedJson, WorkerError> {
    let day = inputs.day();
    if !weekday(day) {
        return Ok(not_a_weekday(day));
    }
    let rows: Vec<(i64, i64, i64)> = sqlx::query_as(
        "SELECT id, instrument_id, quantity_open::bigint FROM vb_position \
         WHERE user_id = $1 AND state = 'OPEN' AND quantity_open > 0 AND gtt_id IS NULL",
    )
    .bind(inputs.user_id)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        return Ok(object([
            ("date", s(iso(day))),
            ("checked", OrderedJson::Bool(true)),
            ("naked", int(0)),
            ("alert", OrderedJson::Null),
        ]));
    }
    let n = len_i64(rows.len());
    let positions: Vec<OrderedJson> = rows
        .iter()
        .take(10)
        .map(|(id, instrument_id, qty)| {
            object([
                ("id", int(*id)),
                ("instrument_id", int(*instrument_id)),
                ("qty", int(*qty)),
            ])
        })
        .collect();
    let alert = SleeveAlert {
        name: "VBT_POSITION_NAKED",
        severity: "critical",
        summary: format!(
            "{n} volume-breakout position(s) have shares open and no GTT stop on {}.",
            iso(day)
        ),
        labels: trade_date_label(day),
        detail: object([("positions", OrderedJson::Array(positions))]),
        runbook: VBT_RUNBOOK,
    };
    Ok(object([
        ("date", s(iso(day))),
        ("checked", OrderedJson::Bool(true)),
        ("naked", int(n)),
        ("alert", dispatch(&alert, inputs.settings, inputs.now).await),
    ]))
}

/// `check_positions_without_bars` (21:40): a held name that has stopped printing — more than
/// seven calendar days since its last bar, or none at all.
pub(crate) async fn check_positions_without_bars(
    conn: &mut PgConnection,
    inputs: &CheckInputs<'_>,
) -> Result<OrderedJson, WorkerError> {
    let day = inputs.day();
    if !weekday(day) {
        return Ok(not_a_weekday(day));
    }
    let held: Vec<(i64, i64)> = sqlx::query_as(
        "SELECT id, instrument_id FROM vb_position \
         WHERE user_id = $1 AND state = 'OPEN' AND quantity_open > 0",
    )
    .bind(inputs.user_id)
    .fetch_all(&mut *conn)
    .await?;
    let quiet = || {
        object([
            ("date", s(iso(day))),
            ("checked", OrderedJson::Bool(true)),
            ("silent", int(0)),
            ("alert", OrderedJson::Null),
        ])
    };
    if held.is_empty() {
        return Ok(quiet());
    }
    let ids: Vec<i64> = held.iter().map(|h| h.1).collect();
    let rows: Vec<(i64, Option<NaiveDate>)> = sqlx::query_as(
        "SELECT instrument_id, max(date) FROM ohlcv_daily WHERE instrument_id = ANY($1) \
         GROUP BY instrument_id",
    )
    .bind(&ids)
    .fetch_all(&mut *conn)
    .await?;
    let last_bars: BTreeMap<i64, Option<NaiveDate>> = rows.into_iter().collect();
    let silent: Vec<&(i64, i64)> = held
        .iter()
        .filter(
            |(_, instrument_id)| match last_bars.get(instrument_id).copied().flatten() {
                None => true,
                Some(last) => (day - last).num_days() > QUIET_DAYS,
            },
        )
        .collect();
    if silent.is_empty() {
        return Ok(quiet());
    }
    let n = len_i64(silent.len());
    let positions: Vec<OrderedJson> = silent
        .iter()
        .take(10)
        .map(|(id, instrument_id)| {
            // `last_bars[id].isoformat() if id in last_bars else None`.
            let last = match last_bars.get(instrument_id) {
                Some(value) => opt_iso(*value),
                None => OrderedJson::Null,
            };
            object([
                ("id", int(*id)),
                ("instrument_id", int(*instrument_id)),
                ("last_bar", last),
            ])
        })
        .collect();
    let alert = SleeveAlert {
        name: "VBT_POSITION_NO_BAR",
        severity: "warning",
        summary: format!(
            "{n} volume-breakout position(s) have not printed a bar in more than {QUIET_DAYS} \
             days as of {}.",
            iso(day)
        ),
        labels: trade_date_label(day),
        detail: object([("positions", OrderedJson::Array(positions))]),
        runbook: VBT_RUNBOOK,
    };
    Ok(object([
        ("date", s(iso(day))),
        ("checked", OrderedJson::Bool(true)),
        ("silent", int(n)),
        ("alert", dispatch(&alert, inputs.settings, inputs.now).await),
    ]))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]
    use super::*;

    #[test]
    fn the_four_names_share_runbook_eight() {
        // test_vbt_ops.py::test_every_vbt_alert_names_the_same_runbook_and_it_exists.
        assert_eq!(VBT_RUNBOOK, "docs/runbooks/08-vbt-evening.md");
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        assert!(
            root.join("decile-blueprint").join(VBT_RUNBOOK).exists()
                || root.join(VBT_RUNBOOK).exists(),
            "runbook 8 is missing"
        );
    }

    #[test]
    fn a_weekend_is_not_a_weekday_in_ist() {
        // test_vbt_ops.py::test_it_stays_quiet_on_a_saturday: 2026-10-31 is a Saturday.
        let sat = NaiveDate::from_ymd_opt(2026, 10, 31).unwrap_or_default();
        assert!(!weekday(sat));
        assert!(weekday(sat - Duration::days(1)));
        // 19:00 UTC on Friday is 00:30 Saturday in IST.
        let inputs = CheckInputs {
            now: NaiveDate::from_ymd_opt(2026, 10, 30)
                .and_then(|d| d.and_hms_opt(19, 0, 0))
                .unwrap_or_default()
                .and_utc(),
            user_id: 1,
            settings: &Settings::default(),
        };
        assert_eq!(inputs.day(), sat);
        let OrderedJson::Object(entries) = not_a_weekday(sat) else {
            panic!("an object");
        };
        let keys: Vec<&str> = entries.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(keys, ["date", "checked", "reason"]);
    }
}
