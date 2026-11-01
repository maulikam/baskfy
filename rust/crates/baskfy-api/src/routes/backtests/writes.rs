//! `POST /backtests` and `DELETE /backtests/{public_id}` (lane writes-sleeves) — ports of
//! `routers/backtests.create_backtest` / `delete_backtest` with `BacktestCreate` /
//! `BacktestConfig` validation, `backtests.capacity_check` (and its stale-run reaper), the
//! `Idempotency-Key` reservation, and the Celery hand-off (`baskfy.backtest.run`).
//!
//! **Executor.** With `BASKFY_BACKTEST_EXECUTOR=celery` the run is queued exactly as Python
//! queues it. With `inline` (Python's default) Python runs the simulation inside the API process,
//! which this server cannot do yet (the worker is a later wave), so the request is refused with a
//! 503 **before** anything is written or reserved, rather than answered with a run that would
//! never start (DECISIONS-RS, lane writes-sleeves). A backtest is a simulation; nothing here
//! reaches a broker (law 2).

use std::cmp::Ordering;

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_core::screener::ScreenDefinition;
use baskfy_db::OrderedJson;
use chrono::{DateTime, Utc};
use sqlx::Row;

use super::FEATURE;
use crate::auth::{Principal, require_authenticated};
use crate::body::receive;
use crate::entitlements::entitlements_for;
use crate::idempotency::{self, Reservation};
use crate::problems::{
    Problem, ProblemType, internal_error, not_found, payment_required, pipeline_degraded,
    rate_limited, stale_data_version,
};
use crate::routes::sleeves::common::db;
use crate::routes::sleeves::pyd::{
    Bound, Default as D, Model, PV, PyDec, Ty, iso_date, validate_body,
};
use crate::routes::sleeves::writes::{has_queue, now_micros, pool, read_body, send_task};
use crate::session::{self, WritePool};
use crate::settings::API_PREFIX;
use crate::state::AppState;
use crate::wire;

/// `_why_it_cannot_start`'s reason when the executor is Celery and no broker is configured.
const NO_BROKER_REASON: &str = "this deployment runs backtests on a Celery worker \
     (BASKFY_BACKTEST_EXECUTOR=celery) and no message broker is reachable, so there is nothing to \
     run it.";

/// `BACKTEST_TASK`.
pub const BACKTEST_TASK: &str = "baskfy.backtest.run";
/// `IN_FLIGHT_RETRY_SECONDS`.
const IN_FLIGHT_RETRY_SECONDS: i64 = 2;
/// `STALE_RUNNING_SECONDS`.
const STALE_RUNNING_SECONDS: i64 = 15 * 60;
/// `STALE_QUEUED_SECONDS`.
const STALE_QUEUED_SECONDS: i64 = 60 * 60;
/// `STALE_RUNNING_ERROR`.
const STALE_RUNNING_ERROR: &str = "the worker running this stopped without finishing it (it was \
     probably restarted or ran out of memory). Run it again.";
/// `STALE_QUEUED_ERROR`.
const STALE_QUEUED_ERROR: &str =
    "nothing ever picked this run up, so it never started. Run it again.";

fn spec(
    fields: Vec<(&'static str, Ty, D)>,
    after: Option<crate::routes::sleeves::pyd::After>,
) -> Ty {
    Ty::Model(Box::new(Model {
        fields,
        forbid_extra: true,
        before: None,
        after,
    }))
}

fn field<'a>(fields: &'a [(String, Option<PV>)], name: &str) -> Option<&'a PV> {
    fields
        .iter()
        .find(|(n, _)| n == name)
        .and_then(|(_, v)| v.as_ref())
}

/// `BacktestCreate` (with `BacktestConfig` and its blocks, field for field).
fn backtest_create() -> Model {
    let rebalance = spec(
        vec![
            (
                "frequency",
                Ty::Enum(&["weekly", "fortnightly", "monthly", "quarterly"]),
                D::Unset,
            ),
            (
                "day",
                Ty::Enum(&["last_trading_day", "first_trading_day"]),
                D::Unset,
            ),
        ],
        None,
    );
    let selection = spec(
        vec![
            (
                "top_n",
                Ty::Int(vec![Bound::Ge("1"), Bound::Le("500")]),
                D::Unset,
            ),
            (
                "hold_buffer",
                Ty::Int(vec![Bound::Ge("0"), Bound::Le("500")]),
                D::Unset,
            ),
        ],
        None,
    );
    let limits = spec(
        vec![
            (
                "max_weight",
                Ty::Dec(vec![Bound::Gt("0"), Bound::Le("1")]),
                D::Unset,
            ),
            (
                "min_weight",
                Ty::Dec(vec![Bound::Ge("0"), Bound::Le("1")]),
                D::Unset,
            ),
        ],
        Some(|fields| {
            let get = |n: &str, default: &str| match field(fields, n) {
                Some(PV::Dec(d)) => d.clone(),
                _ => PyDec::parse(default)
                    .unwrap_or_else(|| PyDec::from_decimal(rust_decimal::Decimal::ZERO)),
            };
            let (min, max) = (get("min_weight", "0.01"), get("max_weight", "0.10"));
            if min.cmp_num(&max) == Ordering::Greater {
                Err(format!(
                    "min_weight {} is above max_weight {}",
                    min.py_str(),
                    max.py_str()
                ))
            } else {
                Ok(())
            }
        }),
    );
    let bps = || Ty::Dec(vec![Bound::Ge("0"), Bound::Le("1000")]);
    let costs = spec(
        vec![
            ("brokerage_bps", bps(), D::Unset),
            ("stt_bps", bps(), D::Unset),
            ("slippage_bps", bps(), D::Unset),
            ("impact_model", Ty::Enum(&["fixed"]), D::Unset),
        ],
        None,
    );
    let overlay = spec(
        vec![
            ("enabled", Ty::Bool, D::Unset),
            ("rule", Ty::Enum(&["index_above_200dma"]), D::Unset),
        ],
        None,
    );
    let config = Model {
        fields: vec![
            ("screen_public_id", Ty::str_len(None, None).opt(), D::Unset),
            ("screen_definition", Ty::AnyDict.opt(), D::Unset),
            ("start", Ty::Date, D::Required),
            ("end", Ty::Date, D::Required),
            ("initial_capital", Ty::Dec(vec![Bound::Gt("0")]), D::Unset),
            ("rebalance", rebalance, D::Unset),
            ("selection", selection, D::Unset),
            (
                "weighting",
                Ty::Enum(&["equal", "inverse_volatility", "rank", "marketcap"]),
                D::Unset,
            ),
            ("position_limits", limits, D::Unset),
            ("costs", costs, D::Unset),
            (
                "cash_policy",
                Ty::Enum(&["hold_cash", "benchmark"]),
                D::Unset,
            ),
            ("benchmark", Ty::str_len(None, None), D::Unset),
            ("risk_overlay", overlay, D::Unset),
            (
                "dividends",
                Ty::Enum(&["reinvest", "cash", "ignore"]),
                D::Unset,
            ),
            (
                "risk_free_rate",
                Ty::Dec(vec![Bound::Ge("0"), Bound::Le("1")]),
                D::Unset,
            ),
            (
                "risk_free_curve",
                Ty::Tuple(Box::new(Ty::Pair(
                    Box::new(Ty::Date),
                    Box::new(Ty::Dec(vec![])),
                ))),
                D::Unset,
            ),
        ],
        forbid_extra: true,
        before: None,
        after: Some(
            |fields| match (field(fields, "start"), field(fields, "end")) {
                (Some(PV::Date(s)), Some(PV::Date(e))) if e <= s => Err(format!(
                    "end {} must be after start {}",
                    iso_date(*e),
                    iso_date(*s)
                )),
                _ => Ok(()),
            },
        ),
    };
    Model {
        fields: vec![
            ("config", Ty::Model(Box::new(config)), D::Required),
            ("data_version", Ty::Int(vec![]).opt(), D::Unset),
            ("fragility", Ty::Bool, D::Unset),
        ],
        forbid_extra: true,
        before: None,
        after: None,
    }
}

/// `json.loads(config.model_dump_json())`: every field in declaration order, defaults filled,
/// decimals as `str(Decimal)`, enums as their values.
fn dump_config(config: &PV) -> OrderedJson {
    let s = |v: &str| OrderedJson::Str(v.to_owned());
    let value = |pv: Option<&PV>, default: OrderedJson| -> OrderedJson {
        match pv {
            None => default,
            Some(PV::Null) => OrderedJson::Null,
            Some(PV::Str(x)) => OrderedJson::Str(x.clone()),
            Some(PV::Dec(d)) => OrderedJson::Str(d.py_str()),
            Some(PV::Int(i)) => i.as_i64().map_or_else(
                || OrderedJson::BigInt(i.canonical.clone()),
                OrderedJson::Int,
            ),
            Some(PV::Bool(b)) => OrderedJson::Bool(*b),
            Some(PV::Date(d)) => OrderedJson::Str(iso_date(*d)),
            Some(PV::Any(j)) => j.clone(),
            Some(_) => default,
        }
    };
    let block = |pv: Option<&PV>, defaults: Vec<(&str, OrderedJson)>| -> OrderedJson {
        OrderedJson::Object(
            defaults
                .into_iter()
                .map(|(k, d)| (k.to_owned(), value(pv.and_then(|p| p.field(k)), d)))
                .collect(),
        )
    };
    let f = |k: &str| config.field(k);
    let curve = match f("risk_free_curve") {
        Some(PV::List(pairs)) => OrderedJson::Array(
            pairs
                .iter()
                .map(|p| match p {
                    PV::List(items) => OrderedJson::Array(
                        items
                            .iter()
                            .map(|i| value(Some(i), OrderedJson::Null))
                            .collect(),
                    ),
                    _ => OrderedJson::Null,
                })
                .collect(),
        ),
        _ => OrderedJson::Array(Vec::new()),
    };
    OrderedJson::Object(vec![
        (
            "screen_public_id".into(),
            value(f("screen_public_id"), OrderedJson::Null),
        ),
        (
            "screen_definition".into(),
            value(f("screen_definition"), OrderedJson::Null),
        ),
        ("start".into(), value(f("start"), OrderedJson::Null)),
        ("end".into(), value(f("end"), OrderedJson::Null)),
        (
            "initial_capital".into(),
            value(f("initial_capital"), s("1000000")),
        ),
        (
            "rebalance".into(),
            block(
                f("rebalance"),
                vec![("frequency", s("monthly")), ("day", s("last_trading_day"))],
            ),
        ),
        (
            "selection".into(),
            block(
                f("selection"),
                vec![
                    ("top_n", OrderedJson::Int(20)),
                    ("hold_buffer", OrderedJson::Int(10)),
                ],
            ),
        ),
        ("weighting".into(), value(f("weighting"), s("equal"))),
        (
            "position_limits".into(),
            block(
                f("position_limits"),
                vec![("max_weight", s("0.10")), ("min_weight", s("0.01"))],
            ),
        ),
        (
            "costs".into(),
            block(
                f("costs"),
                vec![
                    ("brokerage_bps", s("3")),
                    ("stt_bps", s("10")),
                    ("slippage_bps", s("15")),
                    ("impact_model", s("fixed")),
                ],
            ),
        ),
        (
            "cash_policy".into(),
            value(f("cash_policy"), s("hold_cash")),
        ),
        ("benchmark".into(), value(f("benchmark"), s("nifty-500"))),
        (
            "risk_overlay".into(),
            block(
                f("risk_overlay"),
                vec![
                    ("enabled", OrderedJson::Bool(false)),
                    ("rule", s("index_above_200dma")),
                ],
            ),
        ),
        ("dividends".into(), value(f("dividends"), s("ignore"))),
        ("risk_free_rate".into(), value(f("risk_free_rate"), s("0"))),
        ("risk_free_curve".into(), curve),
    ])
}

/// `BacktestAcceptedOut` (pydantic's JSON), with the `202`.
fn accepted(public_id: &str, status: &str, created_at: DateTime<Utc>) -> Response {
    let body = OrderedJson::Object(vec![
        ("public_id".into(), OrderedJson::Str(public_id.to_owned())),
        ("status".into(), OrderedJson::Str(status.to_owned())),
        (
            "created_at".into(),
            OrderedJson::Str(wire::py_datetime(created_at)),
        ),
        (
            "events_url".into(),
            OrderedJson::Str(format!("{API_PREFIX}/backtests/{public_id}/events")),
        ),
    ]);
    let mut response = wire::json(&body);
    *response.status_mut() = StatusCode::ACCEPTED;
    response
}

/// `secrets.token_hex(12)`.
fn new_public_id() -> String {
    let b: [u8; 12] = rand::random();
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// What the request decided to write, once every refusal has passed.
struct Plan {
    user_id: i64,
    screen_id: Option<i64>,
    config: OrderedJson,
    per_user: i64,
    global: i64,
}

/// `POST /backtests`: queue a backtest (`202 {public_id, status, created_at, events_url}`).
///
/// # Errors
/// 400 (body; a definition), 401, 402, 404 (screen), 409 (stale data version), 429 (an
/// idempotency key in flight; a concurrency cap), 503 (the in-process executor), 500.
pub async fn create_backtest(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let key_header = request.headers().get(idempotency::HEADER).map(|v| {
        v.as_bytes()
            .iter()
            .map(|&b| char::from(b))
            .collect::<String>()
    });
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let body =
        validate_body(&received, &backtest_create()).map_err(|f| f.into_problem(Vec::new()))?;
    if !entitlements.allows(FEATURE) {
        return Err(payment_required(FEATURE, None));
    }
    let user_id = principal.require_user()?;
    if state.settings.backtest_executor != "celery" {
        return Err(pipeline_degraded(
            "This server runs backtests in-process (BASKFY_BACKTEST_EXECUTOR=inline), which the \
             Rust API cannot do yet; the Python API serves this route here.",
        ));
    }
    let config = body.field("config").cloned().ok_or_else(internal_error)?;
    let fragility = !matches!(body.field("fragility"), Some(PV::Bool(false)));
    let current: Option<i64> = sqlx::query_scalar(
        "SELECT max(data_version)::bigint FROM pipeline_run WHERE data_version IS NOT NULL",
    )
    .fetch_one(state.db.pool())
    .await
    .map_err(db)?;
    let current = current.unwrap_or(0);
    if let Some(PV::Int(sent)) = body.field("data_version") {
        let sent_v = sent.saturated();
        if sent_v != i128::from(current) {
            return Err(stale_data_version(
                i64::try_from(sent_v).unwrap_or(i64::MAX),
                current,
            ));
        }
    }
    let url = state.settings.redis_url.clone();
    let scope = format!("backtest:create:{user_id}");
    let key = key_header.filter(|k| !k.is_empty());
    let reservation = if url.expose().is_empty() {
        Reservation::Reserved
    } else {
        idempotency::reserve(&url, &scope, key.as_deref()).await
    };
    if let Reservation::Replay(public_id) = &reservation {
        let existing = sqlx::query(
            "SELECT public_id, user_id, status, created_at FROM backtest WHERE public_id = $1",
        )
        .bind(public_id)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db)?;
        if let Some(row) = existing {
            let owner: i64 = row.try_get("user_id").map_err(db)?;
            if owner == user_id {
                return Ok(accepted(
                    &row.try_get::<String, _>("public_id").map_err(db)?,
                    &row.try_get::<String, _>("status").map_err(db)?,
                    row.try_get("created_at").map_err(db)?,
                ));
            }
        }
    }
    if reservation == Reservation::InFlight {
        let mut problem = rate_limited(IN_FLIGHT_RETRY_SECONDS, 1);
        problem.detail = "Another request with this Idempotency-Key is still being processed. \
             Retry in a moment and you will get that run rather than a second one."
            .to_owned();
        return Err(problem);
    }
    // `_why_it_cannot_start`: with the Celery executor and no broker (`task_queue` is `None`
    // without a Redis URL), Python still writes the row, then fails it in the same request with
    // the reason, and answers `202 {status: "failed"}` rather than a run that would never start.
    let cannot_start = (!has_queue(&url)).then_some(NO_BROKER_REASON);
    let created = create_run(&state, write, user_id, &config, &entitlements, cannot_start).await;
    let (public_id, created_at) = match created {
        Ok(v) => v,
        Err(problem) => {
            if !url.expose().is_empty() {
                idempotency::release(&url, &scope, key.as_deref()).await;
            }
            return Err(problem);
        }
    };
    if !url.expose().is_empty() {
        idempotency::remember(&url, &scope, key.as_deref(), &public_id).await;
    }
    if let Some(reason) = cannot_start {
        crate::logging::error(
            "baskfy_api.routers.backtests",
            "backtest could not be started",
            &[("public_id", &public_id), ("reason", reason)],
        );
        return Ok(accepted(&public_id, "failed", created_at));
    }
    // `_start_after_commit`: the hand-off runs after the COMMIT (a background task in Python);
    // a broker that refuses leaves the row queued, logged, exactly as `_dispatch` does.
    if let Err(e) = send_task(
        &url,
        BACKTEST_TASK,
        &[
            OrderedJson::Str(public_id.clone()),
            OrderedJson::Bool(fragility),
        ],
    )
    .await
    {
        crate::logging::error(
            "baskfy_api.routers.backtests",
            "backtest could not be dispatched",
            &[("public_id", &public_id), ("error", &e)],
        );
    }
    Ok(accepted(&public_id, "queued", created_at))
}

/// `_resolve_screen`, the universe gate, `capacity_check` and the insert (with `remember`).
async fn create_run(
    state: &AppState,
    write: Option<Extension<WritePool>>,
    user_id: i64,
    config: &PV,
    entitlements: &crate::entitlements::Entitlements,
    cannot_start: Option<&'static str>,
) -> Result<(String, DateTime<Utc>), Problem> {
    let screen_public_id = match config.field("screen_public_id") {
        Some(PV::Str(s)) => Some(s.clone()),
        _ => None,
    };
    let inline = match config.field("screen_definition") {
        Some(PV::Any(j)) => Some(j.clone()),
        _ => None,
    };
    let (screen_id, definition) = match &screen_public_id {
        None => {
            let Some(def) = inline.clone() else {
                return Err(Problem::new(
                    ProblemType::InvalidScreenDefinition,
                    "A backtest needs either 'screen_public_id' or an inline 'screen_definition'.",
                ));
            };
            if let Err(baskfy_core::screener::ScreenError::Validation(e)) =
                ScreenDefinition::model_validate(&crate::routes::screens::payload_of(&def))
            {
                let errors = e
                    .errors
                    .iter()
                    .map(|d| {
                        OrderedJson::Object(vec![
                            ("field".to_owned(), OrderedJson::Str(d.loc.join("."))),
                            ("message".to_owned(), OrderedJson::Str(d.msg.clone())),
                        ])
                    })
                    .collect();
                return Err(Problem::new(
                    ProblemType::InvalidScreenDefinition,
                    "The inline screen definition failed validation.",
                )
                .with("errors", OrderedJson::Array(errors)));
            }
            (None, Some(def))
        }
        Some(pid) => {
            let row =
                sqlx::query("SELECT id, user_id, definition FROM screen WHERE public_id = $1")
                    .bind(pid)
                    .fetch_optional(state.db.pool())
                    .await
                    .map_err(db)?;
            let Some(row) = row else {
                return Err(not_found("screen", pid));
            };
            let owner: Option<i64> = row.try_get("user_id").map_err(db)?;
            if owner.is_some_and(|o| o != user_id) {
                return Err(not_found("screen", pid));
            }
            let def = crate::routes::sleeves::common::ojson(&row, "definition")?;
            (Some(row.try_get::<i64, _>("id").map_err(db)?), def)
        }
    };
    if let Some(index) = definition.as_ref().and_then(|d| d.get("index"))
        && !matches!(index, OrderedJson::Null)
    {
        require_universe(
            entitlements,
            &crate::routes::sleeves::common::py_str_json(index),
        )?;
    }
    let plan = Plan {
        user_id,
        screen_id,
        config: dump_config(config),
        per_user: state.settings.backtest_user_concurrency,
        global: state.settings.backtest_global_concurrency,
    };
    let public_id = new_public_id();
    let now = now_micros();
    let pool = pool(write)?;
    let pid = public_id.clone();
    let created_at = session::run(&pool, move |tx| {
        Box::pin(async move {
            for (status, column, seconds, message) in [
                ("running", "started_at", STALE_RUNNING_SECONDS, STALE_RUNNING_ERROR),
                ("queued", "created_at", STALE_QUEUED_SECONDS, STALE_QUEUED_ERROR),
            ] {
                let cutoff = now - chrono::Duration::seconds(seconds);
                sqlx::query(&format!(
                    "UPDATE backtest SET status = 'failed', error = $1, finished_at = $2 \
                     WHERE status = $3 AND {column} IS NOT NULL AND {column} < $4"
                ))
                .bind(message)
                .bind(now)
                .bind(status)
                .bind(cutoff)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            }
            let mine: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM backtest WHERE user_id = $1 AND status IN ('queued', 'running')",
            )
            .bind(plan.user_id)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            if mine >= plan.per_user {
                let mut p = rate_limited(60, plan.per_user);
                p.detail = format!(
                    "You already have a backtest running. The per-user limit is {}; it should \
                     finish in a few seconds.",
                    plan.per_user
                );
                return Err(p);
            }
            let everyone: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM backtest WHERE status IN ('queued', 'running')",
            )
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            if everyone >= plan.global {
                let mut p = rate_limited(120, plan.global);
                p.detail = format!(
                    "The service is running its maximum of {} backtests. Try again shortly.",
                    plan.global
                );
                return Err(p);
            }
            let created: DateTime<Utc> = sqlx::query_scalar(
                "INSERT INTO backtest (public_id, user_id, screen_id, config, status) \
                 VALUES ($1, $2, $3, $4, 'queued') RETURNING created_at",
            )
            .bind(&pid)
            .bind(plan.user_id)
            .bind(plan.screen_id)
            .bind(sqlx::types::Json(&plan.config))
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            if let Some(reason) = cannot_start {
                // The row fails in this request, in the same transaction (`row.status = "failed"`
                // and a second flush in Python): the same INSERT, then this UPDATE.
                sqlx::query(
                    "UPDATE backtest SET status = 'failed', error = $1, finished_at = $2 \
                     WHERE public_id = $3",
                )
                .bind(reason)
                .bind(now_micros())
                .bind(&pid)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            }
            Ok(created)
        })
    })
    .await?;
    Ok((public_id, created_at))
}

/// `Entitlements.require_universe(code)`.
fn require_universe(e: &crate::entitlements::Entitlements, code: &str) -> Result<(), Problem> {
    let Some(allowed) = &e.universes else {
        return Ok(());
    };
    if allowed.contains(code) {
        return Ok(());
    }
    let mut values: Vec<&str> = allowed.iter().map(String::as_str).collect();
    values.sort_unstable();
    let english = match values.as_slice() {
        [] => "no universe".to_owned(),
        [one] => (*one).to_owned(),
        [init @ .., last] => format!("{} and {last}", init.join(", ")),
    };
    Err(payment_required(
        "universes",
        Some(&format!(
            "Your plan can screen {english} only, not {}.",
            baskfy_providers::py::repr_str(code)
        )),
    ))
}

/// `DELETE /backtests/{public_id}`: the row goes (its artefacts are left for a sweeper).
///
/// # Errors
/// 401, 402, 404 (not yours, or no such run), 500.
pub async fn delete_backtest(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    if !entitlements.allows(FEATURE) {
        return Err(payment_required(FEATURE, None));
    }
    let pool = pool(write)?;
    let caller = principal.user_id;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let row: Option<(i64, i64)> =
                sqlx::query_as("SELECT id, user_id FROM backtest WHERE public_id = $1")
                    .bind(&public_id)
                    .fetch_optional(&mut **tx)
                    .await
                    .map_err(db)?;
            match row {
                Some((id, owner)) if Some(owner) == caller => {
                    sqlx::query("DELETE FROM backtest WHERE id = $1")
                        .bind(id)
                        .execute(&mut **tx)
                        .await
                        .map_err(db)?;
                    Ok(())
                }
                _ => Err(not_found("backtest", &public_id)),
            }
        })
    })
    .await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}
