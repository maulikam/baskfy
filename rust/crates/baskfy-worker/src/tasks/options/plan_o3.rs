//! The O3 builder (`options.plan_o3`, OP8): O3-B decides before O3-A reads its session; the
//! margin calculator is asked about the hedged two-leg basket only with a Kite session.

use baskfy_core::options::config::{OptionsCeilings, OptionsConfig, Sleeve};
use baskfy_core::options::num::py_str;
use baskfy_core::options::plan::{Finalize, PlanState};
use baskfy_core::options::plan_o3::{
    O3Outcome, O3Plan, contract_label, decide_o3, finalize_o3, wanted_minutes,
};
use baskfy_core::options::session::SessionState;
use baskfy_db::OrderedJson;
use chrono::{DateTime, FixedOffset};
use sqlx::PgConnection;

use super::alerting::{Level, OptionsAlert, OptionsName, RUNBOOK_PLAN};
use super::common::{in_ist, iso, iso_dt};
use super::plan::{
    BuildResult, Decided, MarginReader, PlanReport, PlanRow, ask_margin, costs_json, ds,
    existing_session, hhmm, labels, lapse_expired, leg_json, margin_in_use, margin_pool, merged,
    minute_payload, opt_iso, reasons_text, skip_alert, write_decided,
};
use super::scan::{book_of, core, json_of, load_context, load_market, load_snapshots, mode_value};
use crate::error::WorkerError;
use crate::pyjson::{object, s, strings};
use crate::settings::WorkerSettings;

/// `plan_o3.ORDER`: O3-B decides before O3-A reads its session.
const ORDER: [Sleeve; 2] = [Sleeve::O3b, Sleeve::O3a];

/// `plan_o3.plan_detail(plan)`.
fn detail(plan: &O3Plan) -> Result<OrderedJson, WorkerError> {
    let exits = &plan.exits;
    Ok(object([
        ("costs", costs_json(&plan.costs)?),
        ("debit_inr", ds(plan.debit_inr)),
        ("direction", s(plan.direction.value())),
        (
            "exits",
            object([
                ("target_value", ds(exits.target_value)),
                ("stop_value", ds(exits.stop_value)),
                ("invalidation", s(exits.invalidation.clone())),
                ("invalidation_level", ds(exits.invalidation_level)),
                ("hard_exit_at", s(iso_dt(exits.hard_exit_at))),
            ]),
        ),
        (
            "margin",
            plan.margin.as_ref().map_or(OrderedJson::Null, |m| {
                object([
                    ("code", s(m.code.value())),
                    ("message", s(m.message.clone())),
                ])
            }),
        ),
        ("half_size", OrderedJson::Bool(plan.half_size)),
        ("warnings", strings(plan.warnings.clone())),
        ("gate", json_of(&plan.numbers)?),
        ("as_of_minute", opt_iso(plan.as_of_minute)),
        ("mode", s(mode_value(plan.mode))),
    ]))
}

/// `plan_o3.plan_alert(outcome)`.
fn alert(outcome: &O3Outcome) -> Result<OptionsAlert, WorkerError> {
    let decision = &outcome.decision;
    let sleeve = decision.sleeve.value();
    let day = iso(decision.trade_date);
    let mut labels = labels(decision.trade_date, decision.sleeve, outcome.state);
    let Some(plan) = &outcome.plan else {
        return skip_alert(
            format!(
                "{sleeve} {day}: no trade — {}.",
                reasons_text(&outcome.reasons)
            ),
            labels,
            &outcome.reasons,
            &decision.numbers,
            decision.candidate.as_ref(),
        );
    };
    let [long_leg, short_leg] = plan.legs.as_slice() else {
        return Err(WorkerError::python(
            "IndexError",
            "an O3 plan has exactly two legs",
        ));
    };
    let exits = &plan.exits;
    let margin = plan
        .margin_required_inr
        .map_or_else(|| "unknown".to_owned(), |m| format!("₹{}", py_str(m)));
    let summary = format!(
        "{sleeve} {day} {} plan {}: {} debit spread {} — BUY {} first, then SELL {}; {} lot(s) \
         of {} ({}); debit {} of a {}-point width = max loss ₹{}; target value {} (₹{}), stop \
         value {} (₹{}), invalidation at {}, hard exit {}; round-trip costs ₹{} (share {}); \
         margin {margin}. Expires {}; nothing is sent without a confirm on the desk.",
        mode_value(plan.mode),
        plan.plan_id,
        plan.direction.value(),
        contract_label(plan).map_err(core)?,
        long_leg.tradingsymbol,
        short_leg.tradingsymbol,
        plan.lots,
        plan.lot_size,
        plan.sizing_mode.value(),
        py_str(plan.debit_points),
        py_str(plan.width_points),
        py_str(plan.max_loss_inr),
        py_str(exits.target_value),
        py_str(plan.profit_target_inr),
        py_str(exits.stop_value),
        py_str(plan.stop_inr),
        py_str(exits.invalidation_level),
        hhmm(exits.hard_exit_at),
        py_str(plan.expected_cost_inr),
        py_str(plan.cost_share),
        hhmm(plan.expires_at),
    );
    labels.push(("plan_id".to_owned(), plan.plan_id.clone()));
    Ok(OptionsAlert {
        name: OptionsName::Plan,
        severity: Level::Warning,
        summary,
        labels,
        detail: merged(
            detail(plan)?,
            vec![(
                "legs",
                OrderedJson::Array(plan.legs.iter().map(leg_json).collect()),
            )],
        ),
        runbook: RUNBOOK_PLAN,
    })
}

/// `build_o3_plan(session, user_id, sleeve, now, trading_day=True, margin=...)`.
async fn build_o3_plan(
    conn: &mut PgConnection,
    user_id: i64,
    sleeve: Sleeve,
    now: DateTime<FixedOffset>,
    margin: Option<&MarginReader>,
    settings: &WorkerSettings,
) -> Result<BuildResult, WorkerError> {
    let cfg = OptionsConfig::default();
    let ceil = OptionsCeilings::default();
    let day = in_ist(now).date_naive();
    let current = existing_session(conn, user_id, sleeve, day).await?;
    if let Some(row) = &current
        && row.state != SessionState::Observing.value()
    {
        return Ok(BuildResult {
            report: row.report(),
            alert: None,
        });
    }
    let market = load_market(conn, day, true, &cfg).await?;
    let mode = super::scan::mode_of(settings, sleeve);
    let context = load_context(conn, user_id, day, settings).await?;
    let wanted = wanted_minutes(sleeve, &market, &context, &now, &cfg, &ceil).map_err(core)?;
    let loaded = load_snapshots(conn, wanted, now, day).await?;
    let mut book = book_of(&loaded);
    let decision =
        decide_o3(sleeve, &market, &context, &mut book, &now, &cfg, &ceil).map_err(core)?;
    let mut report = PlanReport::of(sleeve, day, decision.state, &decision.reasons);
    if !decision.writes_session() {
        return Ok(BuildResult {
            report,
            alert: None,
        });
    }
    let (quote, asked) = if decision.state == PlanState::Planned {
        ask_margin(margin, decision.margin_baskets()).await
    } else {
        (None, "not asked".to_owned())
    };
    let inputs = Finalize {
        user_id,
        mode,
        margin_pool_inr: margin_pool(conn, user_id).await?,
        margin_in_use_inr: margin_in_use(conn, user_id, day).await?,
    };
    let outcome = finalize_o3(&decision, quote.as_ref(), &inputs, &cfg).map_err(core)?;
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
            width_points: Some(plan.width_points),
            lots: plan.lots,
            lot_size: plan.lot_size,
            risk_per_lot_inr: plan.risk_per_lot_inr,
            risk_budget_inr: plan.risk_budget_inr,
            max_loss_inr: plan.max_loss_inr,
            profit_target_inr: plan.profit_target_inr,
            stop_inr: plan.stop_inr,
            expected_cost_inr: plan.expected_cost_inr,
            cost_share: plan.cost_share,
            margin_required_inr: plan.margin_required_inr,
            detail: detail(plan)?,
            legs: &plan.legs,
            mode: plan.mode,
        }),
        None => None,
    };
    let last = &outcome.decision;
    let decided = Decided {
        sleeve,
        trade_date: day,
        expiry: last.expiry,
        numbers: &last.numbers,
        candidate: last.candidate.as_ref(),
        as_of_minute: last.as_of_minute,
        verdict: last.verdict,
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
    report.detail = vec![
        ("margin".to_owned(), s(asked)),
        (
            "snapshots".to_owned(),
            strings(loaded.iter().filter_map(|(m, _)| m.map(iso_dt))),
        ),
    ];
    Ok(BuildResult {
        report,
        alert: Some(alert(&outcome)?),
    })
}

/// `plan_o3_minute(session, user_id, now, trading_day=True, margin=...)`: lapse, then O3-B,
/// then O3-A.
pub(crate) async fn plan_o3_minute(
    conn: &mut PgConnection,
    user_id: i64,
    now: DateTime<FixedOffset>,
    margin: Option<&MarginReader>,
    settings: &WorkerSettings,
) -> Result<(Vec<(String, OrderedJson)>, Vec<OptionsAlert>), WorkerError> {
    let lapsed = lapse_expired(conn, user_id, now).await?;
    let mut reports = Vec::new();
    let mut alerts = Vec::new();
    for sleeve in ORDER {
        let result = build_o3_plan(conn, user_id, sleeve, now, margin, settings).await?;
        reports.push(result.report);
        alerts.extend(result.alert);
    }
    Ok((minute_payload(now, lapsed, &reports), alerts))
}
