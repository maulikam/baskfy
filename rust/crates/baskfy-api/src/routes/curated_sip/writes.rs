//! `POST /cb/investments/{investment_id}/sip` (wave 4c, lane writes-screens): an ACTIVE
//! **REMINDER** plan, with `baskfy_core.curated_sip`'s calendar rules ported here privately.
//! `AUTO` is refused before anything is read (desk non-negotiable #1: no auto-debit, no order
//! path — law 2).

use std::collections::BTreeSet;

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use baskfy_db::OrderedJson;
use chrono::{Datelike, NaiveDate, TimeDelta};

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, internal_error};
use crate::routes::screens::accounts::investments::load_investment_for_user;
use crate::routes::screens::body_model::{self, Errors, Model, Ty, opt, req};
use crate::routes::screens::write_kit::{self, db, numeric_text};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

/// `SIP_MODE_REMINDER`.
const SIP_MODE_REMINDER: &str = "REMINDER";
/// `_NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i64 = 1;
/// `_CALENDAR_LOOKAHEAD_DAYS`.
const CALENDAR_LOOKAHEAD_DAYS: i64 = 90;
/// `_SNAP_HORIZON_DAYS`.
const SNAP_HORIZON_DAYS: i64 = 60;

/// `SipBody` (a plain `BaseModel`: extra keys ignored).
fn sip_body() -> Model {
    Model {
        fields: vec![
            req(
                "amount",
                Ty::Dec {
                    gt: Some("0"),
                    ge: None,
                    le: None,
                },
            ),
            req(
                "day_of_month",
                Ty::Int {
                    gt: None,
                    ge: Some(1),
                    le: Some(28),
                },
            ),
            opt("mode", Ty::Str(None, None)),
        ],
        forbid_extra: false,
    }
}

fn invalid(detail: impl Into<String>) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, detail)
}

/// `assert_reminder_mode(mode)`.
fn assert_reminder_mode(mode: &str) -> Result<(), Problem> {
    let r = baskfy_providers::py::repr_str(mode);
    if mode == "AUTO" {
        return Err(invalid(format!(
            "SIP write path refuses mode {r}; AUTO stays dark until D3 (desk non-negotiable #1)"
        )));
    }
    if mode != SIP_MODE_REMINDER {
        return Err(invalid(format!(
            "SIP write path accepts 'REMINDER' only; got {r}"
        )));
    }
    Ok(())
}

/// `_candidate(year, month, day_of_month)`: the nominal day, clamped to the month's last.
fn candidate(year: i32, month: u32, day: u32) -> Option<NaiveDate> {
    let next = if month == 12 {
        NaiveDate::from_ymd_opt(year + 1, 1, 1)
    } else {
        NaiveDate::from_ymd_opt(year, month + 1, 1)
    }?;
    let last = next.pred_opt()?.day();
    NaiveDate::from_ymd_opt(year, month, day.min(last))
}

/// `next_trading_on_or_after(day, trading_dates)`.
fn next_trading_on_or_after(
    day: NaiveDate,
    dates: &BTreeSet<NaiveDate>,
) -> Result<NaiveDate, String> {
    let mut cursor = day;
    for _ in 0..SNAP_HORIZON_DAYS {
        if dates.contains(&cursor) {
            return Ok(cursor);
        }
        cursor = cursor
            .checked_add_signed(TimeDelta::days(1))
            .ok_or_else(|| "date overflow".to_owned())?;
    }
    Err(format!(
        "no trading day on or after {} within {SNAP_HORIZON_DAYS} days",
        wire::py_date(day)
    ))
}

/// `next_fire_date(day_of_month=, as_of=, trading_dates=)` (no `after`).
fn next_fire_date(
    day: u32,
    as_of: NaiveDate,
    dates: &BTreeSet<NaiveDate>,
) -> Result<NaiveDate, String> {
    let (mut year, mut month) = (as_of.year(), as_of.month());
    for _ in 0..14 {
        let c = candidate(year, month, day).ok_or_else(|| "date overflow".to_owned())?;
        let snapped = next_trading_on_or_after(c, dates)?;
        if snapped >= as_of {
            return Ok(snapped);
        }
        if month == 12 {
            year += 1;
            month = 1;
        } else {
            month += 1;
        }
    }
    Err(format!(
        "could not resolve next_fire_date for day_of_month={day} as_of={}",
        wire::py_date(as_of)
    ))
}

/// `POST /cb/investments/{investment_id}/sip` (`create_sip_plan`): 201.
///
/// # Errors
/// 400 (path, body, a mode other than REMINDER, an unschedulable day), 401, the sole-tenant
/// refusals, 404, 409 (an ACTIVE plan exists), 422 (no trading days loaded), 500.
pub async fn create_sip_plan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let id = body_model::path_int(&mut e, "investment_id", &raw_id);
    let body = body_model::body(&mut e, &received, &sip_body(), true);
    e.finish()?;
    let (id, body) = (
        id.ok_or_else(internal_error)?,
        body.ok_or_else(internal_error)?,
    );
    let mode = body
        .get("mode")
        .str()
        .unwrap_or(SIP_MODE_REMINDER)
        .to_owned();
    assert_reminder_mode(&mode)?;
    let amount = body
        .get("amount")
        .dec()
        .cloned()
        .ok_or_else(internal_error)?;
    let day = body
        .get("day_of_month")
        .int()
        .and_then(|d| u32::try_from(d).ok())
        .ok_or_else(internal_error)?;
    let inv = load_investment_for_user(&state, principal.user_id, i64::try_from(id).ok()).await?;
    let today = crate::routes::common::today_ist();
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let end = today
                .checked_add_signed(TimeDelta::days(CALENDAR_LOOKAHEAD_DAYS))
                .ok_or_else(internal_error)?;
            let dates: Vec<NaiveDate> = sqlx::query_scalar(
                "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
                 AND date >= $2 AND date <= $3",
            )
            .bind(NSE_EXCHANGE_ID)
            .bind(today)
            .bind(end)
            .fetch_all(&mut **tx)
            .await
            .map_err(db)?;
            let dates: BTreeSet<NaiveDate> = dates.into_iter().collect();
            let existing: Option<i64> = sqlx::query_scalar(
                "SELECT id FROM cb_sip_plan WHERE investment_id = $1 AND status = 'ACTIVE'",
            )
            .bind(inv.id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            if let Some(existing) = existing {
                return Err(Problem::new(
                    ProblemType::StaleDataVersion,
                    "an ACTIVE SIP reminder already exists for this investment",
                )
                .with("sent_data_version", OrderedJson::Int(existing))
                .with("current_data_version", OrderedJson::Int(existing)));
            }
            if dates.is_empty() {
                return Err(Problem::new(
                    ProblemType::NoTradingDay,
                    "no trading days loaded to schedule the next SIP reminder",
                ));
            }
            let fire = next_fire_date(day, today, &dates).map_err(invalid)?;
            let plan_id: i64 = sqlx::query_scalar(
                "INSERT INTO cb_sip_plan (investment_id, amount, day_of_month, mode, status, \
                 next_fire_date) VALUES ($1, ($2::text)::numeric, $3, 'REMINDER', 'ACTIVE', $4) \
                 RETURNING id",
            )
            .bind(inv.id)
            .bind(numeric_text(&amount))
            .bind(i16::try_from(day).map_err(|_| internal_error())?)
            .bind(fire)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            // `_out(plan)` after the flush: the amount as posted, not as the column rounds it.
            Ok(SipCreatedOut {
                id: plan_id,
                mode: SIP_MODE_REMINDER,
                status: "ACTIVE",
                amount: amount.to_sci_string(),
                day_of_month: i64::from(day),
                next_fire_date: wire::py_date(fire),
            })
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// `SipOut` as the create route renders it: the ORM object after the flush, so `amount` is the
/// posted `Decimal` (its `str()`), not the stored, rounded value `GET` reads back.
#[derive(Debug, Clone, serde::Serialize)]
struct SipCreatedOut {
    id: i64,
    mode: &'static str,
    status: &'static str,
    amount: String,
    day_of_month: i64,
    next_fire_date: String,
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    // `packages/core/tests/test_curated_sip.py`: the nominal day snaps forward to a session and
    // never lands before `as_of`; a short month clamps; AUTO is refused.
    #[test]
    fn the_next_fire_snaps_forward_and_rolls_a_month() {
        let dates: BTreeSet<NaiveDate> = [
            d(2026, 11, 2),
            d(2026, 11, 16),
            d(2026, 12, 1),
            d(2027, 2, 26),
        ]
        .into_iter()
        .collect();
        assert_eq!(
            next_fire_date(1, d(2026, 10, 30), &dates).unwrap(),
            d(2026, 11, 2)
        );
        assert_eq!(
            next_fire_date(15, d(2026, 11, 3), &dates).unwrap(),
            d(2026, 11, 16)
        );
        // Python: `ValueError: no trading day on or after 2027-02-28 within 60 days`.
        assert_eq!(
            next_fire_date(28, d(2027, 2, 1), &dates).unwrap_err(),
            "no trading day on or after 2027-02-28 within 60 days"
        );
        assert_eq!(
            next_fire_date(20, d(2027, 2, 1), &dates).unwrap(),
            d(2027, 2, 26)
        );
        assert_eq!(candidate(2027, 2, 28).unwrap(), d(2027, 2, 28));
        assert!(assert_reminder_mode("AUTO").is_err());
        assert!(assert_reminder_mode("reminder").is_err());
        assert!(assert_reminder_mode("REMINDER").is_ok());
    }
}
