//! The plan-preview handlers (`POST /cb/plans/{invest,apply,exit}`) and what the customize route
//! shares with them: the bodies' dict fields, the market-hours gate, and the preview's shape.
//! A preview is a plan, never an order (law 2).

use std::collections::{BTreeMap, BTreeSet};

use axum::extract::Request;
use axum::response::Response;
use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use chrono::{DateTime, FixedOffset, NaiveDate, TimeDelta};
use num_bigint::BigInt;
use serde::{Serialize, Serializer};

use super::core::{self, Plan};
use crate::body::Received;
use crate::problems::{Problem, ProblemType, internal_error, pipeline_degraded};
use crate::routes::common::lax_int;
use crate::routes::screens::body_model::{self, Errors, Model, Ty, V, opt, req};
use crate::routes::screens::write_kit::{self, db};
use crate::state::AppState;
use crate::wire;

/// `NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i64 = 1;
/// `_CALENDAR_LOOKBACK_DAYS` / `_CALENDAR_LOOKAHEAD_DAYS`.
const LOOKBACK_DAYS: i64 = 7;
const LOOKAHEAD_DAYS: i64 = 30;

/// `dict[str, Decimal]`.
pub(crate) fn dec_dict() -> Ty {
    Ty::Dict(Box::new(Ty::Dec {
        gt: None,
        ge: None,
        le: None,
    }))
}

/// `dict[str, int]`.
pub(crate) fn int_dict() -> Ty {
    Ty::Dict(Box::new(Ty::Int {
        gt: None,
        ge: None,
        le: None,
    }))
}

/// `Decimal = Field(gt=0)`.
pub(crate) fn positive_amount() -> Ty {
    Ty::Dec {
        gt: Some("0"),
        ge: None,
        le: None,
    }
}

/// A validated `dict[str, Decimal]` (a repeated key cannot occur: the JSON reader keeps the last).
pub(crate) fn decimals(v: &V) -> BTreeMap<String, WideDecimal> {
    v.entries()
        .iter()
        .filter_map(|(k, x)| x.dec().map(|d| (k.clone(), d.clone())))
        .collect()
}

/// The raw JSON a body carried for one top-level field (a repeated key already kept its last
/// value when the body was read).
pub(crate) fn raw_field<'a>(received: &'a Received, name: &str) -> Option<&'a OrderedJson> {
    match received {
        Received::Json(OrderedJson::Object(entries)) => entries
            .iter()
            .rev()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v),
        _ => None,
    }
}

/// The exact `int` pydantic's lax mode builds from one JSON value already validated as an `int`
/// (`V::Int` saturates beyond `i128`; Python's int does not — R7.15, R8.ar1).
fn exact_int(raw: &OrderedJson) -> Option<BigInt> {
    match raw {
        OrderedJson::Bool(b) => Some(BigInt::from(u8::from(*b))),
        OrderedJson::Int(i) => Some(BigInt::from(*i)),
        OrderedJson::UInt(u) => Some(BigInt::from(*u)),
        OrderedJson::BigInt(text) => text.parse().ok(),
        // An integral finite float (validation refused the rest): `int(f)`, exactly.
        OrderedJson::Float(f) => format!("{f:.0}").parse().ok(),
        OrderedJson::Str(s) => lax_int(s).and_then(|p| p.canonical.parse().ok()),
        _ => None,
    }
}

/// A validated `dict[str, int]`, every value Python's exact `int` (read back from `raw`, the
/// field's JSON object; the validated `i128` when `raw` has no such key).
pub(crate) fn ints(v: &V, raw: Option<&OrderedJson>) -> BTreeMap<String, BigInt> {
    let raw_entries: &[(String, OrderedJson)] = match raw {
        Some(OrderedJson::Object(entries)) => entries,
        _ => &[],
    };
    v.entries()
        .iter()
        .filter_map(|(k, x)| {
            let exact = raw_entries
                .iter()
                .rev()
                .find(|(rk, _)| rk == k)
                .and_then(|(_, rv)| exact_int(rv));
            exact
                .or_else(|| x.int().map(BigInt::from))
                .map(|n| (k.clone(), n))
        })
        .collect()
}

/// A Python `int` on the wire: a JSON integer literal of any size.
#[derive(Debug, Clone)]
pub(crate) struct PyIntOut(pub(crate) BigInt);

impl Serialize for PyIntOut {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let raw = serde_json::value::RawValue::from_string(self.0.to_string())
            .map_err(serde::ser::Error::custom)?;
        raw.serialize(serializer)
    }
}

/// `PlanLegOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct LegOut {
    symbol: String,
    side: &'static str,
    quantity: PyIntOut,
    ref_price: String,
}

/// The legs as `PlanLegOut` renders them.
pub(crate) fn legs_out(plan: &Plan) -> Vec<LegOut> {
    plan.legs
        .iter()
        .map(|l| LegOut {
            symbol: l.symbol.clone(),
            side: l.side,
            quantity: PyIntOut(l.quantity.clone()),
            ref_price: l.ref_price.to_sci_string(),
        })
        .collect()
}

/// `f"cb-sim-{uuid.uuid4()}"`.
pub(crate) fn sim_plan_id() -> String {
    let mut b: [u8; 16] = rand::random();
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let hex: String = b.iter().map(|x| format!("{x:02x}")).collect();
    format!(
        "cb-sim-{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

/// `_now()`: `datetime.now(tz=IST)`, microsecond precision.
pub(crate) fn now_ist() -> DateTime<FixedOffset> {
    write_kit::now_utc().with_timezone(&crate::routes::common::ist())
}

/// `ClosedMarketOut`.
#[derive(Debug, Clone, Serialize)]
struct ClosedMarketOut {
    market_open: bool,
    next_open_ist: String,
}

/// `_hours_or_closed(session)`: `None` while the session is open, else the closed-market body
/// (`200`); an unanswerable calendar is the 503.
///
/// # Errors
/// 503 `pipeline-degraded` (no next open in the loaded calendar); 500.
pub(crate) async fn hours_or_closed(state: &AppState) -> Result<Option<Response>, Problem> {
    let now = now_ist();
    let around = now.date_naive();
    let start = around
        .checked_sub_signed(TimeDelta::days(LOOKBACK_DAYS))
        .ok_or_else(internal_error)?;
    let end = around
        .checked_add_signed(TimeDelta::days(LOOKAHEAD_DAYS))
        .ok_or_else(internal_error)?;
    let rows: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date >= $2 AND date <= $3",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let dates: BTreeSet<NaiveDate> = rows.into_iter().collect();
    if core::is_nse_session_open(now, &dates) {
        return Ok(None);
    }
    let next = core::next_session_open(now, &dates).map_err(|m| pipeline_degraded(&m))?;
    Ok(Some(wire::json(&ClosedMarketOut {
        market_open: false,
        next_open_ist: wire::py_datetime_fixed(next),
    })))
}

/// `_domain_error(exc)`: a `ValueError` from the plan arithmetic is the 400 with its message.
pub(crate) fn domain_error(message: String) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, message)
}

/// `PlanPreviewOut`.
#[derive(Debug, Clone, Serialize)]
struct PlanPreviewOut {
    market_open: bool,
    kind: &'static str,
    status: &'static str,
    desk_plan_id: String,
    legs: Vec<LegOut>,
    requested_amount: String,
    expires_at_hint: String,
}

fn preview(plan: &Plan) -> Response {
    wire::json(&PlanPreviewOut {
        market_open: true,
        kind: plan.kind,
        status: "PLANNED",
        desk_plan_id: sim_plan_id(),
        legs: legs_out(plan),
        requested_amount: plan.requested_amount.to_sci_string(),
        expires_at_hint: wire::py_datetime_fixed(plan.expires_at_hint),
    })
}

/// Reads and validates a body (no dependencies run before it: the routes take no principal).
async fn validated(request: Request, model: &Model) -> Result<V, Problem> {
    validated_with_raw(request, model).await.map(|(v, _)| v)
}

/// [`validated`], keeping the body as received (the exact ints are read back from it).
async fn validated_with_raw(request: Request, model: &Model) -> Result<(V, Received), Problem> {
    let received = write_kit::read(request).await?;
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, model, true);
    e.finish()?;
    Ok((body.ok_or_else(internal_error)?, received))
}

/// `POST /cb/plans/invest` (`plan_invest`).
///
/// # Errors
/// 400 (body, the plan arithmetic's refusals), 503 (no next open), 500.
pub async fn plan_invest(
    axum::extract::State(state): axum::extract::State<AppState>,
    request: Request,
) -> Result<Response, Problem> {
    let model = Model {
        fields: vec![
            req("target_weights", dec_dict()),
            req("prices", dec_dict()),
            req("amount", positive_amount()),
        ],
        forbid_extra: false,
    };
    let body = validated(request, &model).await?;
    if let Some(closed) = hours_or_closed(&state).await? {
        return Ok(closed);
    }
    let amount = body
        .get("amount")
        .dec()
        .cloned()
        .ok_or_else(internal_error)?;
    let plan = core::build_invest_plan(
        &decimals(body.get("target_weights")),
        &decimals(body.get("prices")),
        &amount,
        now_ist(),
    )
    .map_err(domain_error)?;
    Ok(preview(&plan))
}

/// `POST /cb/plans/apply` (`plan_apply`).
///
/// # Errors
/// As [`plan_invest`].
pub async fn plan_apply(
    axum::extract::State(state): axum::extract::State<AppState>,
    request: Request,
) -> Result<Response, Problem> {
    let model = Model {
        fields: vec![
            req("holdings", int_dict()),
            req("target_weights", dec_dict()),
            req("prices", dec_dict()),
            req("amount", positive_amount()),
        ],
        forbid_extra: false,
    };
    let (body, received) = validated_with_raw(request, &model).await?;
    if let Some(closed) = hours_or_closed(&state).await? {
        return Ok(closed);
    }
    let amount = body
        .get("amount")
        .dec()
        .cloned()
        .ok_or_else(internal_error)?;
    let plan = core::build_apply_plan(
        &ints(body.get("holdings"), raw_field(&received, "holdings")),
        &decimals(body.get("target_weights")),
        &decimals(body.get("prices")),
        &amount,
        now_ist(),
    )
    .map_err(domain_error)?;
    Ok(preview(&plan))
}

/// `POST /cb/plans/exit` (`plan_exit`).
///
/// # Errors
/// As [`plan_invest`].
pub async fn plan_exit(
    axum::extract::State(state): axum::extract::State<AppState>,
    request: Request,
) -> Result<Response, Problem> {
    let model = Model {
        fields: vec![
            req("holdings", int_dict()),
            req("prices", dec_dict()),
            opt(
                "requested_amount",
                Ty::Opt(Box::new(Ty::Dec {
                    gt: None,
                    ge: None,
                    le: None,
                })),
            ),
        ],
        forbid_extra: false,
    };
    let (body, received) = validated_with_raw(request, &model).await?;
    if let Some(closed) = hours_or_closed(&state).await? {
        return Ok(closed);
    }
    let plan = core::build_exit_plan(
        &ints(body.get("holdings"), raw_field(&received, "holdings")),
        &decimals(body.get("prices")),
        now_ist(),
        body.get("requested_amount").dec().cloned(),
    )
    .map_err(domain_error)?;
    Ok(preview(&plan))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simulated_ids_are_uuid4_shaped() {
        let id = sim_plan_id();
        assert_eq!(id.len(), "cb-sim-".len() + 36);
        assert_eq!(&id[7 + 14..7 + 15], "4");
        assert!(matches!(&id[7 + 19..7 + 20], "8" | "9" | "a" | "b"));
    }
}
