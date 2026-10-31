//! The screener-running routes of `baskfy_api.routers.screens`: `POST /screens/preview`,
//! `/explain`, `/selection` and `POST /screens/{public_id}/run` (R8.ar6). Informational only.

use std::collections::BTreeSet;

use axum::extract::{Extension, Path, Request, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use baskfy_core::decimal::WideDecimal;
use baskfy_core::ranking_selection::{self, Constraints, SelectionResult};
use baskfy_core::screener::{DEFAULT_RESULT_COLUMNS, Payload, ScreenDefinition, resolve_columns};
use baskfy_db::OrderedJson;
use sqlx::PgConnection;

use super::body_model::{self, Errors, Model, Ty, V, opt, req};
use super::write_kit::{self, db};
use super::{require_feature, require_universe, stored_definition, string_list};
use crate::auth::{Principal, require_authenticated};
use crate::entitlements::{Entitlements, entitlements_for};
use crate::problems::{Problem, bad_request, internal_error, invalid_screen_definition, not_found};
use crate::screen_ranking::{self, ExplainError};
use crate::screener::{
    self, ApiCache, AsOfResolution, NoCacheError, SCREEN_CACHE_FALLBACK_TTL_SECONDS,
    ScreenRunResult,
};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

/// `seed_data.DEFAULT_COLUMNS`.
const DEFAULT_COLUMNS: [&str; 3] = ["ret_12m", "vol_12m", "close_raw"];
/// `MAX_CORRELATION_WINDOW`.
const MAX_CORRELATION_WINDOW: i128 = 756;

fn opt_date() -> Ty {
    Ty::Opt(Box::new(Ty::Date))
}

fn opt_version() -> Ty {
    Ty::Opt(Box::new(Ty::Int {
        gt: None,
        ge: Some(0),
        le: None,
    }))
}

fn preview_request() -> Model {
    Model {
        fields: vec![
            req("definition", Ty::Definition),
            opt("as_of", opt_date()),
            opt(
                "columns",
                Ty::Opt(Box::new(Ty::List(Box::new(Ty::Str(None, None)), None))),
            ),
            opt("data_version", opt_version()),
        ],
        forbid_extra: true,
    }
}

fn run_request() -> Model {
    Model {
        fields: vec![
            opt("as_of", opt_date()),
            opt("override_definition", Ty::Opt(Box::new(Ty::Definition))),
            opt("data_version", opt_version()),
        ],
        forbid_extra: true,
    }
}

fn explain_request() -> Model {
    Model {
        fields: vec![
            req("definition", Ty::Definition),
            req("symbol", Ty::Str(Some(1), Some(30))),
            opt("as_of", opt_date()),
            opt("data_version", opt_version()),
        ],
        forbid_extra: true,
    }
}

fn int_ge(ge: i128) -> Ty {
    Ty::Int {
        gt: None,
        ge: Some(ge),
        le: None,
    }
}

fn constraints_in() -> Model {
    Model {
        fields: vec![
            opt("max_names", int_ge(1)),
            opt("entry_rank", int_ge(1)),
            opt("retention_rank", int_ge(1)),
            opt("max_per_sector", Ty::Opt(Box::new(int_ge(1)))),
            opt(
                "capital_inr",
                Ty::Opt(Box::new(Ty::Dec {
                    gt: Some("0"),
                    ge: None,
                    le: None,
                })),
            ),
            opt(
                "max_adv_participation_pct",
                Ty::Opt(Box::new(Ty::Dec {
                    gt: Some("0"),
                    ge: None,
                    le: None,
                })),
            ),
            opt("turnover_budget_names", Ty::Opt(Box::new(int_ge(0)))),
            opt(
                "max_correlation",
                Ty::Opt(Box::new(Ty::Float {
                    ge: Some((-1.0, "-1")),
                    le: Some((1.0, "1")),
                })),
            ),
            opt(
                "correlation_window",
                Ty::Int {
                    gt: None,
                    ge: Some(60),
                    le: Some(MAX_CORRELATION_WINDOW),
                },
            ),
        ],
        forbid_extra: true,
    }
}

fn selection_request() -> Model {
    Model {
        fields: vec![
            req("definition", Ty::Definition),
            opt("as_of", opt_date()),
            opt("data_version", opt_version()),
            opt("portfolio_id", Ty::Opt(Box::new(int_ge(1)))),
            opt(
                "holdings",
                Ty::Opt(Box::new(Ty::List(
                    Box::new(Ty::Model(Model {
                        fields: vec![
                            req("symbol", Ty::Str(Some(1), Some(30))),
                            req(
                                "quantity",
                                Ty::Dec {
                                    gt: Some("0"),
                                    ge: None,
                                    le: None,
                                },
                            ),
                        ],
                        forbid_extra: true,
                    })),
                    None,
                ))),
            ),
            opt("constraints", Ty::Model(constraints_in())),
        ],
        forbid_extra: true,
    }
}

fn definition_of(v: &V) -> Option<&ScreenDefinition> {
    match v {
        V::Definition(d) => Some(d),
        _ => None,
    }
}

fn int_of(v: &V) -> Option<i64> {
    v.int().and_then(|i| i64::try_from(i).ok())
}

/// `_validated_columns(requested)`.
fn validated_columns(items: &[V]) -> Result<Vec<String>, Problem> {
    let names: Vec<String> = items
        .iter()
        .filter_map(|v| v.str().map(str::to_owned))
        .collect();
    resolve_columns(&names).map_err(|e| query_error(&e.to_string()))?;
    Ok(names)
}

fn query_error(message: &str) -> Problem {
    invalid_screen_definition(vec![vec![
        (
            "field".to_owned(),
            OrderedJson::Str("definition".to_owned()),
        ),
        ("message".to_owned(), OrderedJson::Str(message.to_owned())),
    ]])
}

/// `_check_custom_columns`.
fn check_custom_columns(columns: &[String], e: &Entitlements) -> Result<(), Problem> {
    if columns.iter().any(|c| {
        !DEFAULT_RESULT_COLUMNS.contains(&c.as_str()) && !DEFAULT_COLUMNS.contains(&c.as_str())
    }) {
        require_feature(e, "custom_columns")?;
    }
    Ok(())
}

/// `_check_historical`.
fn check_historical(r: &AsOfResolution, e: &Entitlements) -> Result<(), Problem> {
    if r.requested.is_some() && r.as_of != r.latest_published {
        require_feature(e, "historical_ranks")?;
    }
    Ok(())
}

/// `_check_data_version`.
async fn check_data_version(conn: &mut PgConnection, sent: Option<i64>) -> Result<i64, Problem> {
    let current = screener::current_data_version(conn).await.map_err(db)?;
    if let Some(s) = sent
        && s != current
    {
        return Err(crate::problems::stale_data_version(s, current));
    }
    Ok(current)
}

/// `_json(outcome)`.
fn run_response(outcome: &ScreenRunResult) -> Response {
    let mut response = (StatusCode::OK, outcome.payload.clone()).into_response();
    let headers = response.headers_mut();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(wire::JSON));
    let pairs = [
        ("x-baskfy-as-of", wire::py_date(outcome.resolution.as_of)),
        ("x-baskfy-data-version", outcome.data_version.to_string()),
        (
            "x-baskfy-cache",
            if outcome.cache_hit { "hit" } else { "miss" }.to_owned(),
        ),
    ];
    for (name, value) in pairs {
        if let Ok(v) = HeaderValue::from_str(&value) {
            headers.insert(name, v);
        }
    }
    response
}

async fn acquire(state: &AppState) -> Result<sqlx::pool::PoolConnection<sqlx::Postgres>, Problem> {
    state.db.pool().acquire().await.map_err(db)
}

fn run_problem<C: std::fmt::Display>(e: &screener::RunError<C>) -> Problem {
    screener::problem(e)
}

/// `POST /screens/preview` (`preview_screen`): run an unsaved definition; nothing persisted.
///
/// # Errors
/// 400, 402, 409, 422, 503, 500.
pub async fn preview_screen(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &preview_request(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let definition = definition_of(body.get("definition")).ok_or_else(internal_error)?;
    let mut conn = acquire(&state).await?;
    check_data_version(&mut conn, int_of(body.get("data_version"))).await?;
    let columns = match body.get("columns") {
        V::List(items) => validated_columns(items)?,
        _ => Vec::new(),
    };
    check_custom_columns(&columns, &entitlements)?;
    require_universe(&entitlements, &definition.index)?;
    let requested = body.get("as_of").date().or(definition.historical_date);
    let resolution = screener::resolve_as_of::<NoCacheError>(&mut conn, requested)
        .await
        .map_err(|e| run_problem(&e))?;
    check_historical(&resolution, &entitlements)?;
    let mut cache = ApiCache::new(&state.settings.redis_url);
    let outcome = screener::run_screen(
        &mut conn,
        definition,
        resolution.requested,
        &columns,
        Some(&mut cache),
        SCREEN_CACHE_FALLBACK_TTL_SECONDS,
    )
    .await
    .map_err(|e| run_problem(&e))?;
    Ok(run_response(&outcome))
}

/// `_ranked_definition`: explain and selection need `ranking_terms`.
fn ranked_definition(definition: &ScreenDefinition) -> Result<(), Problem> {
    if definition.ranking_terms.is_empty() {
        return Err(invalid_screen_definition(vec![vec![
            (
                "field".to_owned(),
                OrderedJson::Str("definition.ranking_terms".to_owned()),
            ),
            (
                "message".to_owned(),
                OrderedJson::Str(
                    "explain and selection need a definition with ranking_terms".to_owned(),
                ),
            ),
        ]]));
    }
    Ok(())
}

/// `POST /screens/explain` (`explain_screen_row`).
///
/// # Errors
/// 400, 401, 402, 404, 409, 422, 503, 500.
pub async fn explain_screen_row(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &explain_request(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    principal.require_user()?;
    let definition = definition_of(body.get("definition")).ok_or_else(internal_error)?;
    let mut conn = acquire(&state).await?;
    let data_version = check_data_version(&mut conn, int_of(body.get("data_version"))).await?;
    ranked_definition(definition)?;
    require_universe(&entitlements, &definition.index)?;
    let requested = body.get("as_of").date().or(definition.historical_date);
    let resolution = screener::resolve_as_of::<NoCacheError>(&mut conn, requested)
        .await
        .map_err(|e| run_problem(&e))?;
    check_historical(&resolution, &entitlements)?;
    let symbol = body.get("symbol").str().unwrap_or_default();
    let explanation = screen_ranking::explain_symbol::<NoCacheError>(
        &mut conn,
        definition,
        symbol,
        resolution.as_of,
        data_version,
    )
    .await
    .map_err(|e| match e {
        ExplainError::NotInUniverse(s) => not_found("instrument in this screen's universe", &s),
        ExplainError::Run(r) => run_problem(&r),
    })?;
    let text = serde_json::to_string(&explanation).map_err(|_| internal_error())?;
    let OrderedJson::Object(fields) = OrderedJson::parse(&text).map_err(|_| internal_error())?
    else {
        return Err(internal_error());
    };
    let mut out = vec![
        (
            "as_of".to_owned(),
            OrderedJson::Str(wire::py_date(resolution.as_of)),
        ),
        ("data_version".to_owned(), OrderedJson::Int(data_version)),
    ];
    out.extend(fields);
    Ok(wire::json(&OrderedJson::Object(out)))
}

fn dec_str(d: Option<&WideDecimal>) -> OrderedJson {
    d.map_or(OrderedJson::Null, |d| OrderedJson::Str(d.to_sci_string()))
}

fn opt_i(v: Option<i64>) -> OrderedJson {
    v.map_or(OrderedJson::Null, OrderedJson::Int)
}

fn opt_f(v: Option<f64>) -> OrderedJson {
    v.map_or(OrderedJson::Null, OrderedJson::Float)
}

fn opt_s(v: Option<&str>) -> OrderedJson {
    v.map_or(OrderedJson::Null, |s| OrderedJson::Str(s.to_owned()))
}

fn obj(entries: Vec<(&str, OrderedJson)>) -> OrderedJson {
    OrderedJson::Object(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

fn payload_json(p: &Payload) -> OrderedJson {
    match p {
        Payload::Null | Payload::Opaque(_) => OrderedJson::Null,
        Payload::Bool(b) => OrderedJson::Bool(*b),
        Payload::Int(i) => OrderedJson::Int(*i),
        Payload::Float(f) => OrderedJson::Float(*f),
        Payload::Str(s) => OrderedJson::Str(s.clone()),
        Payload::Decimal(d) => OrderedJson::Str(match d {
            baskfy_core::screener::PyDecimal::Finite(v) => v.py_str(),
            baskfy_core::screener::PyDecimal::NonFinite(s) => s.clone(),
        }),
        Payload::Date(d) => OrderedJson::Str(wire::py_date(*d)),
        Payload::List(items) => OrderedJson::Array(items.iter().map(payload_json).collect()),
        Payload::Map(entries) => OrderedJson::Object(
            entries
                .iter()
                .map(|(k, v)| (k.clone(), payload_json(v)))
                .collect(),
        ),
    }
}

fn selection_json(s: &SelectionResult) -> Vec<(String, OrderedJson)> {
    let c = &s.constraints;
    let rows = s
        .rows
        .iter()
        .map(|r| {
            obj(vec![
                ("instrument_id", OrderedJson::Int(r.instrument_id)),
                ("symbol", OrderedJson::Str(r.symbol.clone())),
                ("action", OrderedJson::Str(r.action.value().to_owned())),
                (
                    "reasons",
                    OrderedJson::Array(
                        r.reasons
                            .iter()
                            .map(|x| OrderedJson::Str(x.value().to_owned()))
                            .collect(),
                    ),
                ),
                (
                    "flags",
                    OrderedJson::Array(
                        r.flags
                            .iter()
                            .map(|x| OrderedJson::Str(x.value().to_owned()))
                            .collect(),
                    ),
                ),
                ("explanation", OrderedJson::Str(r.explanation.clone())),
                ("quality_rank", opt_i(r.quality_rank)),
                ("score", opt_f(r.score)),
                ("sector", opt_s(r.sector.as_deref())),
                ("current_quantity", dec_str(r.current_quantity.as_ref())),
                ("proposed_value_inr", dec_str(r.proposed_value_inr.as_ref())),
                ("proposed_qty", opt_i(r.proposed_qty)),
                (
                    "adv_participation_pct",
                    dec_str(r.adv_participation_pct.as_ref()),
                ),
                ("max_correlation", opt_f(r.max_correlation)),
                ("correlation_peer", opt_s(r.correlation_peer.as_deref())),
            ])
        })
        .collect();
    vec![
        (
            "constraints".to_owned(),
            obj(vec![
                ("max_names", OrderedJson::Int(c.max_names)),
                ("entry_rank", OrderedJson::Int(c.entry_rank)),
                ("retention_rank", OrderedJson::Int(c.retention_rank)),
                ("max_per_sector", opt_i(c.max_per_sector)),
                ("capital_inr", dec_str(c.capital_inr.as_ref())),
                (
                    "max_adv_participation_pct",
                    dec_str(c.max_adv_participation_pct.as_ref()),
                ),
                ("turnover_budget_names", opt_i(c.turnover_budget_names)),
                ("max_correlation", opt_f(c.max_correlation)),
                ("correlation_window", OrderedJson::Int(c.correlation_window)),
            ]),
        ),
        (
            "summary".to_owned(),
            obj(vec![
                ("kept", OrderedJson::Int(s.kept_count)),
                ("entries", OrderedJson::Int(s.entry_count)),
                ("exits", OrderedJson::Int(s.exit_count)),
                ("skips", OrderedJson::Int(s.skip_count)),
                ("turnover_used", OrderedJson::Int(s.turnover_used)),
                ("turnover_budget", opt_i(c.turnover_budget_names)),
                ("unfilled_slots", OrderedJson::Int(s.unfilled_slots)),
                (
                    "sector_counts",
                    OrderedJson::Object(
                        s.sector_counts
                            .iter()
                            .map(|(k, n)| (k.clone(), OrderedJson::Int(*n)))
                            .collect(),
                    ),
                ),
            ]),
        ),
        (
            "notes".to_owned(),
            OrderedJson::Array(
                s.notes
                    .iter()
                    .map(|n| {
                        obj(vec![
                            ("code", OrderedJson::Str(n.code.value().to_owned())),
                            ("message", OrderedJson::Str(n.message.clone())),
                        ])
                    })
                    .collect(),
            ),
        ),
        ("rows".to_owned(), OrderedJson::Array(rows)),
    ]
}

fn constraints_from(v: &V) -> Constraints {
    let int_or = |name: &str, default: i64| match v.get(name) {
        V::Absent => default,
        other => int_of(other).unwrap_or(default),
    };
    let opt_int = |name: &str| int_of(v.get(name));
    let dec = |name: &str, default: Option<WideDecimal>| match v.get(name) {
        V::Absent => default,
        other => other.dec().cloned(),
    };
    Constraints {
        max_names: int_or("max_names", 15),
        entry_rank: int_or("entry_rank", 15),
        retention_rank: int_or("retention_rank", 60),
        max_per_sector: opt_int("max_per_sector"),
        capital_inr: dec("capital_inr", None),
        max_adv_participation_pct: dec("max_adv_participation_pct", "1.0".parse().ok()),
        turnover_budget_names: opt_int("turnover_budget_names"),
        max_correlation: v.get("max_correlation").float(),
        correlation_window: int_or("correlation_window", 126),
    }
}

/// `POST /screens/selection` (`select_over_screen`): informational only, no plan id.
///
/// # Errors
/// 400, 401, 402, 404, 409, 422, 503, 500.
pub async fn select_over_screen(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &selection_request(), true);
    if let Some(b) = &body {
        let c = b.get("constraints");
        let entry = int_of(c.get("entry_rank")).unwrap_or(15);
        let retention = int_of(c.get("retention_rank")).unwrap_or(60);
        if !matches!(c, V::Absent) && retention < entry {
            e.push(
                &["constraints".to_owned()],
                "Value error, retention_rank must be >= entry_rank",
                "value_error",
            );
        } else if matches!(b.get("portfolio_id"), V::Absent | V::None)
            == matches!(b.get("holdings"), V::Absent | V::None)
        {
            e.push(
                &[],
                "Value error, send exactly one of portfolio_id and holdings",
                "value_error",
            );
        }
    }
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let user_id = principal.require_user()?;
    let definition = definition_of(body.get("definition")).ok_or_else(internal_error)?;
    let mut conn = acquire(&state).await?;
    let data_version = check_data_version(&mut conn, int_of(body.get("data_version"))).await?;
    ranked_definition(definition)?;
    require_universe(&entitlements, &definition.index)?;
    let requested = body.get("as_of").date().or(definition.historical_date);
    let resolution = screener::resolve_as_of::<NoCacheError>(&mut conn, requested)
        .await
        .map_err(|e| run_problem(&e))?;
    check_historical(&resolution, &entitlements)?;

    let portfolio_id = int_of(body.get("portfolio_id"));
    let book = match portfolio_id {
        Some(pid) => {
            let found: Option<i64> =
                sqlx::query_scalar("SELECT id FROM portfolio WHERE id = $1 AND user_id = $2")
                    .bind(pid)
                    .bind(user_id)
                    .fetch_optional(&mut *conn)
                    .await
                    .map_err(db)?;
            let Some(id) = found else {
                return Err(not_found("portfolio", &pid.to_string()));
            };
            screen_ranking::portfolio_holdings(&mut conn, id)
                .await
                .map_err(db)?
        }
        None => inline_book(&mut conn, body.get("holdings").items()).await?,
    };
    let as_of = resolution.as_of;
    let ranked = screener::rank_definition::<NoCacheError>(&mut conn, definition, as_of)
        .await
        .map_err(|e| run_problem(&e))?;
    let candidates = screen_ranking::candidates_from(&ranked);
    let holdings = screen_ranking::with_candidate_sectors(&book.holdings, &candidates);
    let constraints = constraints_from(body.get("constraints"));
    constraints.validate().map_err(|m| {
        crate::logging::error("baskfy_api.app", "unhandled error", &[("error", &m)]);
        internal_error()
    })?;
    let returns = if constraints.max_correlation.is_some() {
        let mut peers: BTreeSet<i64> = holdings.iter().map(|h| h.instrument_id).collect();
        peers.extend(
            candidates
                .iter()
                .filter(|c| c.quality_rank <= constraints.entry_rank)
                .map(|c| c.instrument_id),
        );
        Some(
            screen_ranking::daily_returns(&mut conn, &peers, as_of, constraints.correlation_window)
                .await
                .map_err(db)?,
        )
    } else {
        None
    };
    let selection =
        ranking_selection::select_portfolio(&candidates, &holdings, &constraints, returns.as_ref())
            .map_err(|m| bad_request(&format!("selection refused its inputs: {m}"), None))?;
    let provenance =
        screener::screen_provenance::<NoCacheError>(&mut conn, definition, as_of, data_version)
            .await
            .map_err(|e| run_problem(&e))?;
    let mut out = vec![
        (
            "version".to_owned(),
            OrderedJson::Str(selection.version.to_owned()),
        ),
        ("informational_only".to_owned(), OrderedJson::Bool(true)),
        ("as_of".to_owned(), OrderedJson::Str(wire::py_date(as_of))),
        ("data_version".to_owned(), OrderedJson::Int(data_version)),
        (
            "provenance".to_owned(),
            payload_json(&Payload::Map(provenance)),
        ),
        ("portfolio_id".to_owned(), opt_i(portfolio_id)),
        (
            "holdings_without_quantity".to_owned(),
            OrderedJson::Array(
                book.without_quantity
                    .iter()
                    .cloned()
                    .map(OrderedJson::Str)
                    .collect(),
            ),
        ),
    ];
    out.extend(selection_json(&selection));
    Ok(wire::json(&OrderedJson::Object(out)))
}

/// `_inline_book`: inline holdings resolved by symbol; an unmatched one is a 400.
async fn inline_book(
    conn: &mut PgConnection,
    items: &[V],
) -> Result<screen_ranking::BookHoldings, Problem> {
    let symbols: Vec<String> = items
        .iter()
        .map(|i| {
            i.get("symbol")
                .str()
                .unwrap_or_default()
                .trim_matches(crate::routes::common::py_space)
                .to_uppercase()
        })
        .collect();
    let resolved =
        crate::routes::portfolios::portfolio::symbols::resolve_symbols(conn, &symbols).await?;
    let mut unmatched: Vec<&String> = resolved
        .iter()
        .filter(|(_, m)| m.instrument_id().is_none())
        .map(|(s, _)| s)
        .collect();
    unmatched.sort();
    if !unmatched.is_empty() {
        let errors = unmatched
            .iter()
            .map(|s| {
                vec![
                    ("field".to_owned(), OrderedJson::Str("holdings".to_owned())),
                    (
                        "message".to_owned(),
                        OrderedJson::Str(format!(
                            "{} is not a listed symbol",
                            baskfy_providers::py::repr_str(s)
                        )),
                    ),
                ]
            })
            .collect();
        return Err(bad_request(
            "Some holdings do not resolve to exactly one listed instrument.",
            Some(errors),
        ));
    }
    let mut rows = Vec::with_capacity(items.len());
    for (item, symbol) in items.iter().zip(&symbols) {
        let Some(m) = resolved.get(symbol) else {
            continue;
        };
        let (Some(id), Some(c)) = (m.instrument_id(), m.candidates.first()) else {
            continue;
        };
        rows.push((id, c.symbol.clone(), item.get("quantity").dec().cloned()));
    }
    Ok(screen_ranking::aggregate_holdings(&rows))
}

/// `POST /screens/{public_id}/run` (`run_saved_screen`): run, record the `screen_run` row.
///
/// # Errors
/// 400, 402, 404, 409, 422, 503, 500.
pub async fn run_saved_screen(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &run_request(), false);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let pool = write_kit::pool(write)?;
    let st = state.clone();
    let outcome = session::run(&pool, move |tx| {
        Box::pin(async move {
            let screen = super::load_visible(&st, &public_id, &principal).await?;
            check_data_version(tx, int_of(body.get("data_version"))).await?;
            let stored;
            let definition = match body.get("override_definition") {
                V::Definition(d) => d.as_ref(),
                _ => {
                    stored = stored_definition(&screen.definition)?;
                    &stored
                }
            };
            let columns = string_list(&screen.columns);
            check_custom_columns(&columns, &entitlements)?;
            require_universe(&entitlements, &definition.index)?;
            let requested = body.get("as_of").date().or(definition.historical_date);
            let resolution = screener::resolve_as_of::<NoCacheError>(tx, requested)
                .await
                .map_err(|e| run_problem(&e))?;
            check_historical(&resolution, &entitlements)?;
            let mut cache = ApiCache::new(&st.settings.redis_url);
            let outcome = screener::run_screen(
                tx,
                definition,
                requested,
                &columns,
                Some(&mut cache),
                SCREEN_CACHE_FALLBACK_TTL_SECONDS,
            )
            .await
            .map_err(|e| run_problem(&e))?;
            if let Some(result) = &outcome.result {
                screener::record_run::<NoCacheError>(
                    tx,
                    screen.id,
                    result,
                    &definition.definition_hash(),
                )
                .await
                .map_err(|e| run_problem(&e))?;
            }
            Ok(outcome)
        })
    })
    .await?;
    Ok(run_response(&outcome))
}

/// `GET`/`PATCH`/`DELETE` on a static run path fall to `/screens/{public_id}`, as in Starlette.
pub(crate) mod as_id {
    use super::*;

    macro_rules! static_id {
        ($get:ident, $patch:ident, $delete:ident, $id:literal) => {
            /// `GET /screens/{public_id}` with this literal id.
            ///
            /// # Errors
            /// As the GET.
            pub async fn $get(
                state: State<AppState>,
                principal: Extension<Principal>,
            ) -> Result<Response, Problem> {
                super::super::get_screen(state, principal, Path($id.to_owned())).await
            }

            /// `PATCH /screens/{public_id}` with this literal id.
            ///
            /// # Errors
            /// As the PATCH.
            pub async fn $patch(
                principal: Extension<Principal>,
                write: Option<Extension<WritePool>>,
                request: Request,
            ) -> Result<Response, Problem> {
                super::super::writes::update_screen(principal, write, Path($id.to_owned()), request)
                    .await
            }

            /// `DELETE /screens/{public_id}` with this literal id.
            ///
            /// # Errors
            /// As the DELETE.
            pub async fn $delete(
                principal: Extension<Principal>,
                write: Option<Extension<WritePool>>,
            ) -> Result<Response, Problem> {
                super::super::writes::delete_screen(principal, write, Path($id.to_owned())).await
            }
        };
    }

    static_id!(preview_get, preview_patch, preview_delete, "preview");
    static_id!(explain_get, explain_patch, explain_delete, "explain");
    static_id!(
        selection_get,
        selection_patch,
        selection_delete,
        "selection"
    );
}
