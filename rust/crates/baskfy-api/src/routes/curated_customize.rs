//! Customize plan preview — investor weight edits on an investment (leaf 4.7).
//!
//! Ports `baskfy_api.routers.curated_customize` (`APIRouter(prefix="/cb/investments",
//! tags=["curated-customize"])`) at wave 4c (lane writes-screens):
//!
//! * `POST /cb/investments/{investment_id}/customize` — a CUSTOMIZE-kind plan **preview** while the
//!   cash session is open (synthetic `cb-sim-…` id), the closed-market payload outside it. The
//!   path id is only checked to be >= 1 (Python reads no ledger and takes no principal here).
//!
//! Preview only — **no route may place, modify or cancel an order** (law 2).

use axum::Router;
use axum::extract::{Path, Request, State};
use axum::response::Response;
use axum::routing::post;
use serde::Serialize;

use super::curated_plans::{core, writes as plans};
use crate::problems::{Problem, ProblemType, internal_error};
use crate::routes::screens::body_model::{self, Errors, Model, req};
use crate::routes::screens::write_kit;
use crate::state::AppState;
use crate::wire;

/// `CustomizePlanOut`.
#[derive(Debug, Clone, Serialize)]
struct CustomizePlanOut {
    market_open: bool,
    kind: &'static str,
    status: &'static str,
    desk_plan_id: String,
    investment_id: plans::PyIntOut,
    legs: Vec<plans::LegOut>,
    requested_amount: String,
    expires_at_hint: String,
}

/// `POST /cb/investments/{investment_id}/customize` (`customize_investment`).
///
/// # Errors
/// 400 (path, body, an id below 1, the plan arithmetic's refusals), 503 (no next open), 500.
pub async fn customize_investment(
    State(state): State<AppState>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let model = Model {
        fields: vec![
            req("holdings", plans::int_dict()),
            req("target_weights", plans::dec_dict()),
            req("prices", plans::dec_dict()),
            req("amount", plans::positive_amount()),
        ],
        forbid_extra: false,
    };
    let mut e = Errors::new();
    let id = body_model::path_int(&mut e, "investment_id", &raw_id);
    let body = body_model::body(&mut e, &received, &model, true);
    e.finish()?;
    let (investment_id, body) = (
        id.ok_or_else(internal_error)?,
        body.ok_or_else(internal_error)?,
    );
    if investment_id < 1 {
        return Err(Problem::new(
            ProblemType::InvalidScreenDefinition,
            "investment_id must be >= 1",
        ));
    }
    if let Some(closed) = plans::hours_or_closed(&state).await? {
        return Ok(closed);
    }
    let amount = body
        .get("amount")
        .dec()
        .cloned()
        .ok_or_else(internal_error)?;
    let plan = core::build_customize_plan(
        &plans::ints(
            body.get("holdings"),
            plans::raw_field(&received, "holdings"),
        ),
        &plans::decimals(body.get("target_weights")),
        &plans::decimals(body.get("prices")),
        &amount,
        plans::now_ist(),
    )
    .map_err(plans::domain_error)?;
    Ok(wire::json(&CustomizePlanOut {
        market_open: true,
        kind: "CUSTOMIZE",
        status: "PLANNED",
        desk_plan_id: plans::sim_plan_id(),
        // Echoed as Python's exact `int` (the check above reads the saturated value: beyond
        // `i128` only the sign matters to `< 1`).
        investment_id: plans::PyIntOut(
            crate::routes::common::lax_int(&raw_id)
                .and_then(|p| p.canonical.parse().ok())
                .unwrap_or_else(|| investment_id.into()),
        ),
        legs: plans::legs_out(&plan),
        requested_amount: plan.requested_amount.to_sci_string(),
        expires_at_hint: wire::py_datetime_fixed(plan.expires_at_hint),
    }))
}

/// The routes of `baskfy_api.routers.curated_customize`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new().route(
        "/cb/investments/{investment_id}/customize",
        post(customize_investment),
    )
}
