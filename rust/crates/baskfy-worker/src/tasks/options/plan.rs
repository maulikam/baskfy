//! The plan builders' shared database side (`options.plan`, OP6) and the O1 builder:
//! `op_session` + `op_plan` + `op_leg`, idempotent per date, the margin calculator asked only
//! for a `PLANNED` decision and only with a Kite session. No order path (law 2).

use baskfy_core::decimal::Rounding;
use baskfy_core::options::config::{Mode, OptionsCeilings, OptionsConfig, Sleeve};
use baskfy_core::options::costs::ChargeBreakdown;
use baskfy_core::options::num::{at_ist, from_float, py_str, quantize};
use baskfy_core::options::plan::{
    Baskets, Finalize, MarginLeg, MarginQuote, O1Outcome, O1Plan, PlanLeg, PlanState, Verdict,
    condor_config_for, decide_o1, finalize_o1,
};
use baskfy_core::options::scan::{Numbers, Value};
use baskfy_core::options::session::{Session, SessionState, transition};
use baskfy_core::options::structures::{Candidate, to_json};
use baskfy_db::OrderedJson;
use baskfy_providers::kite::KiteProvider;
use baskfy_providers::records::{
    MarginLegFields, MarginLegRecord, MarginOrderType, MarginProduct, MarginVariety,
    TransactionType,
};
use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::alerting::{Level, OptionsAlert, OptionsName, RUNBOOK_PLAN};
use super::common::{in_ist, iso, iso_dt};
use super::scan::{core, json_of, jsonb, load_context, load_market, load_snapshot, mode_value};
use crate::error::WorkerError;
use crate::pyjson::{object, opt_s, s, strings};
use crate::settings::WorkerSettings;

/// Sessions whose plan holds margin (`04` §7.4).
const HOLDING_MARGIN: [&str; 2] = ["CONFIRMED", "OPEN"];

/// `PlanReport`: one build attempt for one sleeve.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct PlanReport {
    pub(crate) sleeve: String,
    pub(crate) trade_date: String,
    pub(crate) state: String,
    pub(crate) reasons: Vec<String>,
    pub(crate) plan_id: Option<String>,
    pub(crate) created: bool,
    pub(crate) detail: Vec<(String, OrderedJson)>,
}

impl PlanReport {
    /// A fresh report of a decision.
    pub(crate) fn of(sleeve: Sleeve, day: NaiveDate, state: PlanState, reasons: &[String]) -> Self {
        Self {
            sleeve: sleeve.value().to_owned(),
            trade_date: iso(day),
            state: state.value().to_owned(),
            reasons: reasons.to_vec(),
            plan_id: None,
            created: false,
            detail: Vec::new(),
        }
    }

    /// `PlanReport.as_dict()`.
    pub(crate) fn as_dict(&self) -> OrderedJson {
        object([
            ("sleeve", s(self.sleeve.clone())),
            ("trade_date", s(self.trade_date.clone())),
            ("state", s(self.state.clone())),
            ("reasons", strings(self.reasons.clone())),
            ("plan_id", opt_s(self.plan_id.clone())),
            ("created", OrderedJson::Bool(self.created)),
            ("detail", OrderedJson::Object(self.detail.clone())),
        ])
    }
}

/// One build's report, and the alert when this call wrote the session.
pub(crate) struct BuildResult {
    pub(crate) report: PlanReport,
    pub(crate) alert: Option<OptionsAlert>,
}

// --- small conversions ----------------------------------------------------------------------

/// `str(Decimal)` as JSON.
pub(crate) fn ds(d: Decimal) -> OrderedJson {
    s(py_str(d))
}

/// `_money(value)`: `str` or `None`.
pub(crate) fn money(d: Option<Decimal>) -> OrderedJson {
    d.map_or(OrderedJson::Null, ds)
}

/// An aware moment's `isoformat()` or `None`.
pub(crate) fn opt_iso(at: Option<DateTime<FixedOffset>>) -> OrderedJson {
    at.map_or(OrderedJson::Null, |t| s(iso_dt(t)))
}

/// `f"{moment.astimezone(IST):%H:%M}"`.
pub(crate) fn hhmm(at: DateTime<FixedOffset>) -> String {
    in_ist(at).format("%H:%M").to_string()
}

/// `str(value)` of a `numbers` entry inside an f-string.
pub(crate) fn py_text(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_owned(),
        Some(Value::Bool(b)) => if *b { "True" } else { "False" }.to_owned(),
        Some(Value::Int(i)) => i.to_string(),
        Some(Value::Str(t)) => t.clone(),
        Some(Value::List(_)) => "[...]".to_owned(),
    }
}

fn greek(value: Option<f64>) -> Result<Option<Decimal>, WorkerError> {
    value
        .map(|v| from_float(v).and_then(|d| quantize(d, 6, Rounding::HalfUp)))
        .transpose()
        .map_err(core)
}

const fn verdict_value(v: Verdict) -> &'static str {
    match v {
        Verdict::Trade => "TRADE",
        Verdict::Skip => "SKIP",
    }
}

/// `costs_json(costs)`: each component to the paisa, and the total.
pub(crate) fn costs_json(costs: &ChargeBreakdown) -> Result<OrderedJson, WorkerError> {
    Ok(object([
        ("orders", OrderedJson::Int(costs.orders)),
        ("brokerage", ds(costs.brokerage)),
        ("stt", ds(costs.stt)),
        ("exchange_txn", ds(costs.exchange_txn)),
        ("sebi", ds(costs.sebi)),
        ("ipft", ds(costs.ipft)),
        ("stamp", ds(costs.stamp)),
        ("gst", ds(costs.gst)),
        ("total", ds(costs.total().map_err(core)?)),
    ]))
}

/// `to_json(candidate)` as JSON, or `None`.
pub(crate) fn candidate_json(c: Option<&Candidate>) -> Result<OrderedJson, WorkerError> {
    match c {
        Some(c) => json_of(&to_json(c).map_err(core)?),
        None => Ok(OrderedJson::Null),
    }
}

/// The no-trade `OPTIONS_PLAN` alert every builder raises.
pub(crate) fn skip_alert(
    summary: String,
    labels: Vec<(String, String)>,
    reasons: &[String],
    numbers: &Numbers,
    candidate: Option<&Candidate>,
) -> Result<OptionsAlert, WorkerError> {
    Ok(OptionsAlert {
        name: OptionsName::Plan,
        severity: Level::Warning,
        summary,
        labels,
        detail: object([
            ("reasons", strings(reasons.to_vec())),
            ("gate", json_of(numbers)?),
            ("candidate", candidate_json(candidate)?),
        ]),
        runbook: RUNBOOK_PLAN,
    })
}

/// `", ".join(reasons) or "no reason recorded"`.
pub(crate) fn reasons_text(reasons: &[String]) -> String {
    if reasons.is_empty() {
        "no reason recorded".to_owned()
    } else {
        reasons.join(", ")
    }
}

/// The alert's labels.
pub(crate) fn labels(day: NaiveDate, sleeve: Sleeve, state: PlanState) -> Vec<(String, String)> {
    vec![
        ("trade_date".to_owned(), iso(day)),
        ("sleeve".to_owned(), sleeve.value().to_owned()),
        ("state".to_owned(), state.value().to_owned()),
    ]
}

/// Merges `extra` into a JSON object (`a | b`).
pub(crate) fn merged(base: OrderedJson, extra: Vec<(&str, OrderedJson)>) -> OrderedJson {
    let OrderedJson::Object(mut entries) = base else {
        return base;
    };
    for (k, v) in extra {
        crate::pyjson::update(&mut entries, k, v);
    }
    OrderedJson::Object(entries)
}

// --- the margin calculator ------------------------------------------------------------------

/// A fixed calculator: the hedged total for a basket, or the error text it raised.
pub type MarginFn = dyn Fn(&[MarginLeg]) -> Result<Option<Decimal>, String> + Send + Sync;

/// The broker's margin calculator for one basket (`plan.MarginReader`).
pub enum MarginReader {
    /// `kite_margin_reader(general)`: Kite's `/margins/basket`, `consider_positions=False`.
    Kite(Box<KiteProvider>),
    /// A calculator given in-process (the tests' fake broker).
    Fixed(std::sync::Arc<MarginFn>),
}

impl MarginReader {
    async fn read(&self, legs: &[MarginLeg]) -> Result<Option<Decimal>, String> {
        let provider = match self {
            Self::Fixed(f) => return f(legs),
            Self::Kite(provider) => provider,
        };
        let mut records = Vec::with_capacity(legs.len());
        for lg in legs {
            let record = MarginLegRecord::new(MarginLegFields {
                exchange: "NFO".to_owned(),
                tradingsymbol: lg.tradingsymbol.clone(),
                transaction_type: match lg.side {
                    baskfy_core::options::config::Side::Buy => TransactionType::Buy,
                    baskfy_core::options::config::Side::Sell => TransactionType::Sell,
                },
                quantity: lg.quantity,
                product: MarginProduct::default(),
                order_type: MarginOrderType::default(),
                variety: MarginVariety::default(),
                price: Decimal::ZERO,
            })
            .map_err(|e| format!("ValidationError: {e}"))?;
            records.push(record);
        }
        let answer = provider
            .basket_order_margins(&records, false)
            .await
            .map_err(|e| format!("{}: {e}", e.python_type()))?;
        Ok(answer.final_total)
    }
}

/// `ask_margin(reader, decision)`: the hedged basket and the transient prefix, or `None` with the
/// reason the calculator did not answer.
pub(crate) async fn ask_margin(
    reader: Option<&MarginReader>,
    baskets: Baskets,
) -> (Option<MarginQuote>, String) {
    let Some(reader) = reader else {
        return (
            None,
            "no Kite session: the margin calculator was not asked".to_owned(),
        );
    };
    let (hedged_basket, transient_basket) = baskets;
    let hedged = match reader.read(&hedged_basket).await {
        Ok(v) => v,
        Err(e) => return (None, format!("margin calculator failed: {e}")),
    };
    let transient = if transient_basket.is_empty() {
        None
    } else {
        match reader.read(&transient_basket).await {
            Ok(v) => v,
            Err(e) => return (None, format!("margin calculator failed: {e}")),
        }
    };
    match hedged {
        None => (None, "the margin calculator returned no total".to_owned()),
        Some(hedged_inr) => (
            Some(MarginQuote {
                hedged_inr,
                transient_inr: transient,
            }),
            "asked".to_owned(),
        ),
    }
}

// --- reads ----------------------------------------------------------------------------------

/// Today's `op_session` row as the builders read it.
#[derive(Debug, Clone)]
pub(crate) struct SessionRow {
    pub(crate) id: i64,
    sleeve: String,
    trade_date: NaiveDate,
    pub(crate) state: String,
    skip_reasons: Vec<String>,
    plan_id: Option<String>,
}

impl SessionRow {
    /// `_report_of(row)`.
    pub(crate) fn report(&self) -> PlanReport {
        PlanReport {
            sleeve: self.sleeve.clone(),
            trade_date: iso(self.trade_date),
            state: self.state.clone(),
            reasons: self.skip_reasons.clone(),
            plan_id: self.plan_id.clone(),
            created: false,
            detail: Vec::new(),
        }
    }
}

type SessionTuple = (i64, String, NaiveDate, String, Vec<String>, Option<String>);

/// `existing_session(session, user_id, sleeve, day)`.
pub(crate) async fn existing_session(
    conn: &mut PgConnection,
    user_id: i64,
    sleeve: Sleeve,
    day: NaiveDate,
) -> Result<Option<SessionRow>, WorkerError> {
    let row: Option<SessionTuple> = sqlx::query_as(
        "SELECT id, sleeve::text, trade_date, state, skip_reasons::text[], plan_id \
             FROM op_session WHERE user_id = $1 AND sleeve = $2::op_sleeve AND trade_date = $3",
    )
    .bind(user_id)
    .bind(sleeve.value())
    .bind(day)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(
        |(id, sleeve, trade_date, state, skip_reasons, plan_id)| SessionRow {
            id,
            sleeve,
            trade_date,
            state,
            skip_reasons,
            plan_id,
        },
    ))
}

/// `margin_in_use(session, user_id, day)`: the hedged figure of today's confirmed or open plans.
pub(crate) async fn margin_in_use(
    conn: &mut PgConnection,
    user_id: i64,
    day: NaiveDate,
) -> Result<Decimal, WorkerError> {
    let total: Option<Decimal> = sqlx::query_scalar(
        "SELECT coalesce(sum(op_plan.margin_required_inr), 0) FROM op_plan \
         JOIN op_session ON op_session.id = op_plan.session_id \
         WHERE op_plan.user_id = $1 AND op_session.trade_date = $2 \
         AND op_session.state = ANY($3) AND op_plan.kind = 'ENTRY'",
    )
    .bind(user_id)
    .bind(day)
    .bind(&HOLDING_MARGIN[..])
    .fetch_one(&mut *conn)
    .await?;
    Ok(total.unwrap_or(Decimal::ZERO))
}

/// `margin_pool(session, user_id)`.
pub(crate) async fn margin_pool(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Decimal, WorkerError> {
    let pool: Option<Decimal> = sqlx::query_scalar(
        "SELECT op_book_config.margin_pool_inr FROM op_book_config WHERE op_book_config.user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(pool.unwrap_or(Decimal::ZERO))
}

// --- writes ---------------------------------------------------------------------------------

/// `session_values(...)`'s columns for a decided day.
pub(crate) struct SessionValues {
    expiry: Option<NaiveDate>,
    mode: Mode,
    state: SessionState,
    numbers: OrderedJson,
    verdict: Option<Verdict>,
    skip_reasons: Vec<String>,
    plan_id: Option<String>,
}

/// What a decided day carries into `op_session`.
pub(crate) struct Decided<'a> {
    pub(crate) sleeve: Sleeve,
    pub(crate) trade_date: NaiveDate,
    pub(crate) expiry: Option<NaiveDate>,
    pub(crate) numbers: &'a Numbers,
    pub(crate) candidate: Option<&'a Candidate>,
    pub(crate) as_of_minute: Option<DateTime<FixedOffset>>,
    pub(crate) verdict: Option<Verdict>,
    pub(crate) mode: Mode,
    pub(crate) plan_id: Option<String>,
    pub(crate) reasons: &'a [String],
}

/// `session_values(...)`: `PLANNED` with a plan, else `SKIPPED` with its reasons — the core
/// machine asked whether `OBSERVING` may go there.
pub(crate) fn session_values(d: &Decided<'_>) -> Result<SessionValues, WorkerError> {
    let mut detail = match json_of(d.numbers)? {
        OrderedJson::Object(entries) => entries,
        _ => Vec::new(),
    };
    if let Some(c) = d.candidate {
        crate::pyjson::update(&mut detail, "candidate", candidate_json(Some(c))?);
    }
    if let Some(at) = d.as_of_minute {
        crate::pyjson::update(&mut detail, "as_of_minute", s(iso_dt(at)));
    }
    let planned = d.plan_id.is_some();
    let state = if planned {
        SessionState::Planned
    } else {
        SessionState::Skipped
    };
    let reason = (!planned).then(|| d.reasons.join(","));
    transition(
        &Session {
            sleeve: d.sleeve,
            trade_date: d.trade_date,
            state: SessionState::Observing,
            reason: None,
        },
        state,
        reason.as_deref(),
    )
    .map_err(|e| WorkerError::python("IllegalTransition", e.to_string()))?;
    Ok(SessionValues {
        expiry: d.expiry,
        mode: d.mode,
        state,
        numbers: OrderedJson::Object(detail),
        verdict: d.verdict,
        skip_reasons: if planned {
            Vec::new()
        } else {
            d.reasons.to_vec()
        },
        plan_id: d.plan_id.clone(),
    })
}

/// `write_session_row(...)`: move the `OBSERVING` row on, or insert today's; `None` if another
/// builder wrote it first.
pub(crate) async fn write_session_row(
    conn: &mut PgConnection,
    user_id: i64,
    sleeve: Sleeve,
    trade_date: NaiveDate,
    values: &SessionValues,
    current: Option<&SessionRow>,
) -> Result<Option<i64>, WorkerError> {
    let numbers = jsonb(&values.numbers)?;
    let verdict = values.verdict.map(verdict_value);
    if let Some(current) = current {
        sqlx::query(
            "UPDATE op_session SET expiry_used = $1, mode = $2, state = $3, numbers = $4::jsonb, \
             verdict = $5, skip_reasons = $6, plan_id = $7 WHERE op_session.id = $8",
        )
        .bind(values.expiry)
        .bind(mode_value(values.mode))
        .bind(values.state.value())
        .bind(numbers)
        .bind(verdict)
        .bind(&values.skip_reasons)
        .bind(&values.plan_id)
        .bind(current.id)
        .execute(&mut *conn)
        .await?;
        return Ok(Some(current.id));
    }
    let id: Option<i64> = sqlx::query_scalar(
        "INSERT INTO op_session (user_id, sleeve, trade_date, expiry_used, mode, state, numbers, \
         verdict, skip_reasons, plan_id) \
         VALUES ($1, $2::op_sleeve, $3, $4, $5, $6, $7::jsonb, $8, $9, $10) \
         ON CONFLICT ON CONSTRAINT uq_op_session_user_sleeve_date DO NOTHING RETURNING op_session.id",
    )
    .bind(user_id)
    .bind(sleeve.value())
    .bind(trade_date)
    .bind(values.expiry)
    .bind(mode_value(values.mode))
    .bind(values.state.value())
    .bind(numbers)
    .bind(verdict)
    .bind(&values.skip_reasons)
    .bind(&values.plan_id)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(id)
}

/// One `op_plan` row's columns.
pub(crate) struct PlanRow<'a> {
    pub(crate) plan_id: &'a str,
    pub(crate) sleeve: Sleeve,
    pub(crate) structure: &'static str,
    pub(crate) sizing_mode: &'static str,
    pub(crate) issued_at: DateTime<FixedOffset>,
    pub(crate) entry_window_end: DateTime<FixedOffset>,
    pub(crate) expires_at: DateTime<FixedOffset>,
    pub(crate) credit_points: Option<Decimal>,
    pub(crate) debit_points: Option<Decimal>,
    pub(crate) width_points: Option<Decimal>,
    pub(crate) lots: i64,
    pub(crate) lot_size: i64,
    pub(crate) risk_per_lot_inr: Decimal,
    pub(crate) risk_budget_inr: Decimal,
    pub(crate) max_loss_inr: Decimal,
    pub(crate) profit_target_inr: Decimal,
    pub(crate) stop_inr: Decimal,
    pub(crate) expected_cost_inr: Decimal,
    pub(crate) cost_share: Decimal,
    pub(crate) margin_required_inr: Option<Decimal>,
    pub(crate) detail: OrderedJson,
    pub(crate) legs: &'a [PlanLeg],
    pub(crate) mode: Mode,
}

fn depth_json(leg: &PlanLeg) -> OrderedJson {
    let side = |levels: &[baskfy_core::options::chain::Level]| {
        OrderedJson::Array(
            levels
                .iter()
                .map(|lv| {
                    object([
                        ("price", ds(lv.price)),
                        ("quantity", OrderedJson::Int(lv.quantity)),
                    ])
                })
                .collect(),
        )
    };
    object([("buy", side(&leg.bids)), ("sell", side(&leg.asks))])
}

/// `_write_plan`: the `ISSUED` entry plan and its `PENDING` legs (`simulated` on paper).
pub(crate) async fn write_plan(
    conn: &mut PgConnection,
    user_id: i64,
    session_pk: i64,
    row: &PlanRow<'_>,
) -> Result<(), WorkerError> {
    let pk: i64 = sqlx::query_scalar(
        "INSERT INTO op_plan (user_id, plan_id, session_id, sleeve, structure, kind, sizing_mode, \
         issued_at, entry_window_end, expires_at, credit_points, debit_points, width_points, \
         lots, lot_size, risk_per_lot_inr, risk_budget_inr, max_loss_inr, profit_target_inr, \
         stop_inr, expected_cost_inr, cost_share, margin_required_inr, status, detail) \
         VALUES ($1, $2, $3, $4::op_sleeve, $5, 'ENTRY', $6, $7, $8, $9, $10, $11, $12, \
         $13::smallint, $14::smallint, $15, $16, $17, $18, $19, $20, $21, $22, 'ISSUED', \
         $23::jsonb) RETURNING op_plan.id",
    )
    .bind(user_id)
    .bind(row.plan_id)
    .bind(session_pk)
    .bind(row.sleeve.value())
    .bind(row.structure)
    .bind(row.sizing_mode)
    .bind(row.issued_at)
    .bind(row.entry_window_end)
    .bind(row.expires_at)
    .bind(row.credit_points)
    .bind(row.debit_points)
    .bind(row.width_points)
    .bind(row.lots)
    .bind(row.lot_size)
    .bind(row.risk_per_lot_inr)
    .bind(row.risk_budget_inr)
    .bind(row.max_loss_inr)
    .bind(row.profit_target_inr)
    .bind(row.stop_inr)
    .bind(row.expected_cost_inr)
    .bind(row.cost_share)
    .bind(row.margin_required_inr)
    .bind(jsonb(&row.detail)?)
    .fetch_one(&mut *conn)
    .await?;
    let simulated = row.mode == Mode::Paper;
    for leg in row.legs {
        sqlx::query(
            "INSERT INTO op_leg (user_id, plan_id, seq, role, tradingsymbol, instrument_token, \
             strike, option_type, side, quantity, limit_price, iv_at_plan, delta_at_plan, bid, \
             ask, depth_json, status, filled_qty, simulated) \
             VALUES ($1, $2, $3::smallint, $4, $5, $6, $7, $8, $9, $10::integer, $11, $12, $13, \
             $14, $15, $16::jsonb, 'PENDING', 0, $17)",
        )
        .bind(user_id)
        .bind(pk)
        .bind(leg.seq)
        .bind(leg.role.value())
        .bind(&leg.tradingsymbol)
        .bind(leg.instrument_token)
        .bind(leg.strike)
        .bind(leg.option_type.value())
        .bind(leg.side.value())
        .bind(leg.quantity)
        .bind(leg.limit_price)
        .bind(greek(leg.iv)?)
        .bind(greek(leg.delta)?)
        .bind(leg.bid)
        .bind(leg.ask)
        .bind(jsonb(&depth_json(leg))?)
        .bind(simulated)
        .execute(&mut *conn)
        .await?;
    }
    Ok(())
}

/// `lapse_expired(session, user_id, now)`: every issued entry plan past `expires_at` and its
/// `PLANNED` session to `LAPSED`; the plan ids.
pub(crate) async fn lapse_expired(
    conn: &mut PgConnection,
    user_id: i64,
    now: DateTime<FixedOffset>,
) -> Result<Vec<String>, WorkerError> {
    let rows: Vec<(i64, String, i64)> = sqlx::query_as(
        "SELECT op_plan.id, op_plan.plan_id, op_plan.session_id FROM op_plan \
         WHERE op_plan.user_id = $1 AND op_plan.status = 'ISSUED' AND op_plan.kind = 'ENTRY' \
         AND op_plan.expires_at <= $2",
    )
    .bind(user_id)
    .bind(now)
    .fetch_all(&mut *conn)
    .await?;
    let mut lapsed = Vec::with_capacity(rows.len());
    for (pk, plan_id, session_pk) in rows {
        sqlx::query("UPDATE op_plan SET status = 'LAPSED' WHERE op_plan.id = $1")
            .bind(pk)
            .execute(&mut *conn)
            .await?;
        sqlx::query(
            "UPDATE op_session SET state = 'LAPSED' \
             WHERE op_session.id = $1 AND op_session.state = 'PLANNED'",
        )
        .bind(session_pk)
        .execute(&mut *conn)
        .await?;
        lapsed.push(plan_id);
    }
    Ok(lapsed)
}

/// Writes the decided session (and the plan); the raced row's report when another builder won.
pub(crate) async fn write_decided(
    conn: &mut PgConnection,
    user_id: i64,
    decided: &Decided<'_>,
    current: Option<&SessionRow>,
    plan: Option<&PlanRow<'_>>,
) -> Result<Result<(), Option<PlanReport>>, WorkerError> {
    let values = session_values(decided)?;
    let Some(session_pk) = write_session_row(
        conn,
        user_id,
        decided.sleeve,
        decided.trade_date,
        &values,
        current,
    )
    .await?
    else {
        let raced = existing_session(conn, user_id, decided.sleeve, decided.trade_date).await?;
        return Ok(Err(raced.map(|r| r.report())));
    };
    if let Some(plan) = plan {
        write_plan(conn, user_id, session_pk, plan).await?;
    }
    Ok(Ok(()))
}

/// The task's payload: `{"at", "lapsed", "sleeves"}` (the caller adds `"alerts"`).
pub(crate) fn minute_payload(
    now: DateTime<FixedOffset>,
    lapsed: Vec<String>,
    reports: &[PlanReport],
) -> Vec<(String, OrderedJson)> {
    vec![
        ("at".to_owned(), s(iso_dt(now))),
        ("lapsed".to_owned(), strings(lapsed)),
        (
            "sleeves".to_owned(),
            OrderedJson::Array(reports.iter().map(PlanReport::as_dict).collect()),
        ),
    ]
}

// --- O1 -------------------------------------------------------------------------------------

fn o1_label(sleeve: Sleeve) -> &'static str {
    if sleeve == Sleeve::O1m {
        "O1-M"
    } else {
        "O1-W"
    }
}

/// `plan.plan_detail(plan)` for O1.
fn o1_detail(plan: &O1Plan) -> Result<OrderedJson, WorkerError> {
    Ok(object([
        ("costs", costs_json(&plan.costs)?),
        ("credit_inr", ds(plan.credit_inr)),
        (
            "margin",
            object([
                ("hedged_inr", money(plan.margin_required_inr)),
                ("transient_inr", money(plan.margin_transient_inr)),
                (
                    "code",
                    plan.margin
                        .as_ref()
                        .map_or(OrderedJson::Null, |m| s(m.code.value())),
                ),
                (
                    "message",
                    plan.margin
                        .as_ref()
                        .map_or(OrderedJson::Null, |m| s(m.message.clone())),
                ),
            ]),
        ),
        ("half_size", OrderedJson::Bool(plan.half_size)),
        ("warnings", strings(plan.warnings.clone())),
        ("gate", json_of(&plan.numbers)?),
        ("as_of_minute", opt_iso(plan.as_of_minute)),
        ("mode", s(mode_value(plan.mode))),
    ]))
}

/// `_leg_json(leg)` (O1, O3).
pub(crate) fn leg_json(leg: &PlanLeg) -> OrderedJson {
    object([
        ("seq", OrderedJson::Int(leg.seq)),
        ("role", s(leg.role.value())),
        ("tradingsymbol", s(leg.tradingsymbol.clone())),
        ("side", s(leg.side.value())),
        ("quantity", OrderedJson::Int(leg.quantity)),
        ("limit_price", ds(leg.limit_price)),
        ("bid", money(leg.bid)),
        ("ask", money(leg.ask)),
    ])
}

/// `plan.plan_alert(outcome)`.
fn o1_alert(outcome: &O1Outcome) -> Result<OptionsAlert, WorkerError> {
    let decision = &outcome.decision;
    let day = iso(decision.trade_date);
    let mut labels = labels(decision.trade_date, decision.sleeve, outcome.state);
    let Some(plan) = &outcome.plan else {
        return skip_alert(
            format!(
                "{} {day}: no trade — {}.",
                o1_label(decision.sleeve),
                reasons_text(&outcome.reasons)
            ),
            labels,
            &outcome.reasons,
            &decision.numbers,
            decision.candidate.as_ref(),
        );
    };
    let legs: Vec<String> = plan
        .legs
        .iter()
        .map(|lg| {
            format!(
                "{}. {} {} {} @ {}",
                lg.seq,
                lg.side.value(),
                lg.quantity,
                lg.tradingsymbol,
                py_str(lg.limit_price)
            )
        })
        .collect();
    let margin = plan.margin_required_inr.map_or_else(
        || "margin not known".to_owned(),
        |m| format!("margin ₹{}", py_str(m)),
    );
    let warnings = if plan.warnings.is_empty() {
        String::new()
    } else {
        format!(" Warnings: {}.", plan.warnings.join(", "))
    };
    let summary = format!(
        "{} {day} {} plan {}: iron condor on the {} expiry, credit {} pts (₹{}) for {} lot(s) \
         of {} ({}); max loss ₹{}, round-trip costs ₹{} (share {}), {margin}. Legs in send \
         order: {}. Expires {}; nothing is sent without a confirm on the desk.{warnings}",
        o1_label(plan.sleeve),
        mode_value(plan.mode),
        plan.plan_id,
        iso(plan.expiry),
        py_str(plan.credit_points),
        py_str(plan.credit_inr),
        plan.lots,
        plan.lot_size,
        plan.sizing_mode.value(),
        py_str(plan.max_loss_inr),
        py_str(plan.expected_cost_inr),
        py_str(plan.cost_share),
        legs.join("; "),
        hhmm(plan.expires_at),
    );
    labels.push(("plan_id".to_owned(), plan.plan_id.clone()));
    Ok(OptionsAlert {
        name: OptionsName::Plan,
        severity: Level::Warning,
        summary,
        labels,
        detail: merged(
            o1_detail(plan)?,
            vec![(
                "legs",
                OrderedJson::Array(plan.legs.iter().map(leg_json).collect()),
            )],
        ),
        runbook: RUNBOOK_PLAN,
    })
}

/// `build_o1_plan(session, user_id, sleeve, now, trading_day=..., margin=...)`.
async fn build_o1_plan(
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
    let plan_time = condor_config_for(sleeve, &cfg).map_err(core)?.plan_time;
    let snapshot = load_snapshot(conn, Some(at_ist(day, plan_time)), now, day).await?;
    let decision = decide_o1(
        sleeve,
        &market,
        &context,
        snapshot.as_ref(),
        &now,
        &cfg,
        &ceil,
    )
    .map_err(core)?;
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
    let outcome = finalize_o1(&decision, quote.as_ref(), &inputs, &cfg).map_err(core)?;
    let plan_row = match &outcome.plan {
        Some(plan) => Some(PlanRow {
            plan_id: &plan.plan_id,
            sleeve: plan.sleeve,
            structure: plan.structure.value(),
            sizing_mode: plan.sizing_mode.value(),
            issued_at: plan.issued_at,
            entry_window_end: plan.entry_window_end,
            expires_at: plan.expires_at,
            credit_points: Some(plan.credit_points),
            debit_points: None,
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
            detail: o1_detail(plan)?,
            legs: &plan.legs,
            mode: plan.mode,
        }),
        None => None,
    };
    let decided = Decided {
        sleeve: decision.sleeve,
        trade_date: decision.trade_date,
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
    report.detail = vec![("margin".to_owned(), s(asked))];
    Ok(BuildResult {
        report,
        alert: Some(o1_alert(&outcome)?),
    })
}

/// `plan_o1_minute(session, user_id, now, trading_day=True, margin=...)`: lapse, then O1-M and
/// O1-W.
pub(crate) async fn plan_o1_minute(
    conn: &mut PgConnection,
    user_id: i64,
    now: DateTime<FixedOffset>,
    margin: Option<&MarginReader>,
    settings: &WorkerSettings,
) -> Result<(Vec<(String, OrderedJson)>, Vec<OptionsAlert>), WorkerError> {
    let lapsed = lapse_expired(conn, user_id, now).await?;
    let mut reports = Vec::new();
    let mut alerts = Vec::new();
    for sleeve in [Sleeve::O1m, Sleeve::O1w] {
        let result = build_o1_plan(conn, user_id, sleeve, now, margin, settings).await?;
        reports.push(result.report);
        alerts.extend(result.alert);
    }
    Ok((minute_payload(now, lapsed, &reports), alerts))
}
