//! The O2 builder (`options.plan_o2`, OP7): one trigger, one plan a day, priced from the
//! trigger minute's snapshot. No broker call at all — a long option's ceiling is its premium.

use baskfy_core::options::config::{OptionsCeilings, OptionsConfig, Sleeve};
use baskfy_core::options::plan_o2::{
    O2Outcome, O2Plan, decide_o2, finalize_o2, leg_of, wanted_minutes,
};
use baskfy_core::options::session::SessionState;
use baskfy_db::OrderedJson;
use chrono::{DateTime, FixedOffset};
use sqlx::PgConnection;

use super::alerting::{Level, OptionsAlert, OptionsName, RUNBOOK_PLAN};
use super::common::{in_ist, iso};
use super::plan::{
    BuildResult, Decided, PlanReport, PlanRow, costs_json, ds, existing_session, hhmm, labels,
    lapse_expired, merged, minute_payload, money, opt_iso, py_text, reasons_text, skip_alert,
    write_decided,
};
use super::scan::{book_of, core, json_of, load_context, load_market, load_snapshots, mode_value};
use crate::error::WorkerError;
use crate::pyjson::{object, s, strings};
use crate::settings::WorkerSettings;

/// `plan_o2.plan_detail(plan)`.
fn detail(plan: &O2Plan) -> Result<OrderedJson, WorkerError> {
    let exits = &plan.exits;
    Ok(object([
        ("costs", costs_json(&plan.costs)?),
        ("premium_inr", ds(plan.premium_inr)),
        ("gap_through_inr", ds(plan.gap_through_inr)),
        ("direction", s(plan.direction.value())),
        (
            "exits",
            object([
                ("stop_price", ds(exits.stop_price)),
                ("target_price", ds(exits.target_price)),
                ("invalidation_level", ds(exits.invalidation_level)),
                (
                    "time_stop_minutes",
                    OrderedJson::Int(exits.time_stop_minutes),
                ),
                ("time_stop_min_price", ds(exits.time_stop_min_price)),
                ("hard_exit_at", s(super::common::iso_dt(exits.hard_exit_at))),
            ]),
        ),
        ("half_size", OrderedJson::Bool(plan.half_size)),
        ("warnings", strings(plan.warnings.clone())),
        ("gate", json_of(&plan.numbers)?),
        ("as_of_minute", opt_iso(plan.as_of_minute)),
        ("mode", s(mode_value(plan.mode))),
    ]))
}

/// `plan_o2.plan_alert(outcome)`.
fn alert(outcome: &O2Outcome) -> Result<OptionsAlert, WorkerError> {
    let decision = &outcome.decision;
    let day = iso(decision.trade_date);
    let mut labels = labels(decision.trade_date, Sleeve::O2, outcome.state);
    let Some(plan) = &outcome.plan else {
        return skip_alert(
            format!("O2 {day}: no trade — {}.", reasons_text(&outcome.reasons)),
            labels,
            &outcome.reasons,
            &decision.numbers,
            decision.candidate.as_ref(),
        );
    };
    let leg = leg_of(plan).map_err(core)?;
    let exits = &plan.exits;
    let summary = format!(
        "O2 {day} {} plan {}: BUY {} {} @ {} ({} lot(s) of {}, {}) on the {} break of {} at {}; \
         premium ₹{}, stop {} (risk ₹{}), target {} (₹{}), time stop {} min, hard exit {}; \
         gap-through worst case ₹{}; round-trip costs ₹{} (share {}). Expires {}; nothing is \
         sent without a confirm on the desk.",
        mode_value(plan.mode),
        plan.plan_id,
        leg.quantity,
        leg.tradingsymbol,
        ds_text(leg.limit_price),
        plan.lots,
        plan.lot_size,
        plan.sizing_mode.value(),
        plan.direction.value(),
        py_text(plan.numbers.get("trigger_level")),
        py_text(plan.numbers.get("trigger_time")),
        ds_text(plan.premium_inr),
        ds_text(exits.stop_price),
        ds_text(plan.max_loss_inr),
        ds_text(exits.target_price),
        ds_text(plan.profit_target_inr),
        exits.time_stop_minutes,
        hhmm(exits.hard_exit_at),
        ds_text(plan.gap_through_inr),
        ds_text(plan.expected_cost_inr),
        ds_text(plan.cost_share),
        hhmm(plan.expires_at),
    );
    labels.push(("plan_id".to_owned(), plan.plan_id.clone()));
    let leg_json = object([
        ("seq", OrderedJson::Int(leg.seq)),
        ("role", s(leg.role.value())),
        ("tradingsymbol", s(leg.tradingsymbol.clone())),
        ("side", s(leg.side.value())),
        ("quantity", OrderedJson::Int(leg.quantity)),
        ("limit_price", ds(leg.limit_price)),
        ("bid", money(leg.bid)),
        ("ask", money(leg.ask)),
        (
            "delta",
            leg.delta.map_or(OrderedJson::Null, OrderedJson::Float),
        ),
    ]);
    Ok(OptionsAlert {
        name: OptionsName::Plan,
        severity: Level::Warning,
        summary,
        labels,
        detail: merged(
            detail(plan)?,
            vec![("legs", OrderedJson::Array(vec![leg_json]))],
        ),
        runbook: RUNBOOK_PLAN,
    })
}

fn ds_text(d: rust_decimal::Decimal) -> String {
    baskfy_core::options::num::py_str(d)
}

/// `build_o2_plan(session, user_id, now, trading_day=True)`.
async fn build_o2_plan(
    conn: &mut PgConnection,
    user_id: i64,
    now: DateTime<FixedOffset>,
    settings: &WorkerSettings,
) -> Result<BuildResult, WorkerError> {
    let cfg = OptionsConfig::default();
    let ceil = OptionsCeilings::default();
    let day = in_ist(now).date_naive();
    let current = existing_session(conn, user_id, Sleeve::O2, day).await?;
    if let Some(row) = &current
        && row.state != SessionState::Observing.value()
    {
        return Ok(BuildResult {
            report: row.report(),
            alert: None,
        });
    }
    let market = load_market(conn, day, true, &cfg).await?;
    let mode = super::scan::mode_of(settings, Sleeve::O2);
    let context = load_context(conn, user_id, day, settings).await?;
    let wanted = wanted_minutes(&market, &context, &now, &cfg, &ceil).map_err(core)?;
    let loaded = load_snapshots(conn, wanted, now, day).await?;
    let mut book = book_of(&loaded);
    let decision = decide_o2(&market, &context, &mut book, &now, &cfg, &ceil).map_err(core)?;
    let mut report = PlanReport::of(Sleeve::O2, day, decision.state, &decision.reasons);
    if !decision.writes_session() {
        return Ok(BuildResult {
            report,
            alert: None,
        });
    }
    let outcome = finalize_o2(&decision, user_id, mode, &cfg).map_err(core)?;
    let plan_row = match &outcome.plan {
        Some(plan) => Some(PlanRow {
            plan_id: &plan.plan_id,
            sleeve: plan.sleeve,
            structure: plan.structure.value(),
            sizing_mode: plan.sizing_mode.value(),
            issued_at: plan.issued_at,
            entry_window_end: plan.entry_window_end,
            expires_at: plan.expires_at,
            credit_points: None,
            debit_points: Some(plan.debit_points),
            width_points: None,
            lots: plan.lots,
            lot_size: plan.lot_size,
            risk_per_lot_inr: plan.risk_per_lot_inr,
            risk_budget_inr: plan.risk_budget_inr,
            max_loss_inr: plan.max_loss_inr,
            profit_target_inr: plan.profit_target_inr,
            stop_inr: plan.stop_inr,
            expected_cost_inr: plan.expected_cost_inr,
            cost_share: plan.cost_share,
            margin_required_inr: None,
            detail: detail(plan)?,
            legs: &plan.legs,
            mode: plan.mode,
        }),
        None => None,
    };
    let decided = Decided {
        sleeve: Sleeve::O2,
        trade_date: day,
        expiry: decision.expiry,
        numbers: &decision.numbers,
        candidate: decision.candidate.as_ref(),
        as_of_minute: decision.as_of_minute,
        verdict: decision.verdict,
        mode,
        plan_id: outcome.plan.as_ref().map(|p| p.plan_id.clone()),
        reasons: &outcome.reasons,
    };
    if let Err(raced) =
        write_decided(conn, user_id, &decided, current.as_ref(), plan_row.as_ref()).await?
    {
        return Ok(BuildResult {
            report: raced.unwrap_or(report),
            alert: None,
        });
    }
    report.state = outcome.state.value().to_owned();
    report.reasons.clone_from(&outcome.reasons);
    report.plan_id = outcome.plan.as_ref().map(|p| p.plan_id.clone());
    report.created = true;
    report.detail = vec![(
        "snapshots".to_owned(),
        strings(
            loaded
                .iter()
                .filter_map(|(m, _)| m.map(super::common::iso_dt)),
        ),
    )];
    Ok(BuildResult {
        report,
        alert: Some(alert(&outcome)?),
    })
}

/// `plan_o2_minute(session, user_id, now, trading_day=True)`: lapse, then today's O2 session.
pub(crate) async fn plan_o2_minute(
    conn: &mut PgConnection,
    user_id: i64,
    now: DateTime<FixedOffset>,
    settings: &WorkerSettings,
) -> Result<(Vec<(String, OrderedJson)>, Vec<OptionsAlert>), WorkerError> {
    let lapsed = lapse_expired(conn, user_id, now).await?;
    let result = build_o2_plan(conn, user_id, now, settings).await?;
    Ok((
        minute_payload(now, lapsed, &[result.report]),
        result.alert.into_iter().collect(),
    ))
}
