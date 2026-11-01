//! The write routes of `baskfy_api.routers.portfolios`: `POST /portfolios`,
//! `POST /portfolios/import-csv`, `PATCH /portfolios/{id}`, `DELETE /portfolios/{id}` and
//! `PUT /portfolios/{id}/holdings`. (`POST /{id}/rebalance` is not ported; DECISIONS-RS R6.23.)
//!
//! Order of refusals, as FastAPI's: the body is read (and a JSON or multipart syntax error
//! answered) before any dependency runs; then the 401; then every validation error of the path,
//! the query and the body together; then the handler. Every statement runs in the request's one
//! transaction ([`crate::session::run`]), and the reads after a write run on it too.

use axum::extract::{Extension, Path, RawQuery, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::PgConnection;

use super::graph::{self, GraphError, MAX_DEPTH};
use super::model::{self, Bounds, Errors, PyInt};
use super::multipart::{self, FormValue};
use super::portfolio_csv::{
    self, MAX_IMPORT_ROWS, MatchStatus, ParseFailure, ParsedCsv, ParsedHolding, RowIssue,
    SkipReason, SkippedRow, UnmatchedReason,
};
use super::request::{header_value, pool, read_body};
use super::symbols::{self, SymbolResolution};
use super::{db, num, params};
use crate::auth::{Principal, require_authenticated};
use crate::body::{Received, receive};
use crate::idempotency;
use crate::problems::{Problem, ProblemType, not_found};
use crate::query::QueryParams;
use crate::routes::common::today_ist;
use crate::routes::portfolios::{
    OwnedPortfolio, PortfolioDetailOut, graph_rows, holdings_out, load,
};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

/// `MAX_UPLOAD_BYTES`.
const MAX_UPLOAD_BYTES: usize = 1_048_576;

/// `CandidateOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct CandidateOut {
    instrument_id: i64,
    symbol: String,
    name: String,
    series: Option<String>,
}

/// `ImportRowOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ImportRowOut {
    line: usize,
    raw_symbol: String,
    symbol: String,
    status: MatchStatus,
    reason: Option<UnmatchedReason>,
    instrument_id: Option<i64>,
    name: Option<String>,
    candidates: Vec<CandidateOut>,
    issues: Vec<RowIssue>,
    matched_via_alias: bool,
    #[serde(serialize_with = "num::ser_opt")]
    quantity: Option<WideDecimal>,
    #[serde(serialize_with = "num::ser_opt")]
    avg_price: Option<WideDecimal>,
}

/// `SkippedRowOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct SkippedRowOut {
    line: usize,
    reason: SkipReason,
    raw: String,
}

/// `ImportReportOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ImportReportOut {
    total_lines: usize,
    matched: usize,
    ambiguous: usize,
    unmatched: usize,
    skipped: usize,
    imported: usize,
    rows: Vec<ImportRowOut>,
    skipped_rows: Vec<SkippedRowOut>,
    ignored_columns: Vec<String>,
}

/// `PortfolioWriteDetailOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct PortfolioWriteDetailOut {
    portfolio: PortfolioDetailOut,
    report: ImportReportOut,
}

/// `HoldingIn`.
struct HoldingIn {
    symbol: String,
    quantity: Option<WideDecimal>,
    avg_price: Option<WideDecimal>,
}

fn refuse(e: &GraphError) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, e.to_string())
}

fn too_many(n: usize) -> Problem {
    Problem::new(
        ProblemType::InvalidScreenDefinition,
        format!("A portfolio may hold at most {MAX_IMPORT_ROWS} names; {n} were sent."),
    )
}

/// The parsed JSON body, or the `missing`/`model_attributes_type` root error (recorded).
fn root<'a>(received: &'a Received, errors: &mut Errors) -> Option<&'a [(String, OrderedJson)]> {
    match received {
        Received::Missing | Received::Json(OrderedJson::Null) => {
            errors.push("<root>", "Field required", "missing");
            None
        }
        Received::Raw => {
            errors.push(
                "<root>",
                "Input should be a valid dictionary or object to extract fields from",
                "model_attributes_type",
            );
            None
        }
        Received::Json(v) => model::object(v, "", errors),
    }
}

/// `list[HoldingIn]` at `loc`.
fn holdings_in(value: &OrderedJson, loc: &str, errors: &mut Errors) -> Option<Vec<HoldingIn>> {
    let items = model::list(value, loc, None, errors)?;
    let before = errors.0.len();
    let mut out = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        let at = model::at(loc, &i.to_string());
        let Some(entries) = model::object(item, &at, errors) else {
            continue;
        };
        let symbol = model::required_string(entries, "symbol", &at, (Some(1), Some(30)), errors);
        let quantity = model::optional_decimal(entries, "quantity", &at, false, errors);
        let avg_price = model::optional_decimal(entries, "avg_price", &at, false, errors);
        model::forbid_extra(entries, &["symbol", "quantity", "avg_price"], &at, errors);
        if let (Some(symbol), Ok(quantity), Ok(avg_price)) = (symbol, quantity, avg_price) {
            out.push(HoldingIn {
                symbol,
                quantity,
                avg_price,
            });
        }
    }
    (errors.0.len() == before).then_some(out)
}

/// The path's `portfolio_id: int`.
fn path_id(raw: &str, errors: &mut Errors) -> Option<PyInt> {
    model::int(
        &OrderedJson::Str(raw.to_owned()),
        "portfolio_id",
        Bounds::default(),
        errors,
    )
}

/// `_rows_from_body(holdings)`: the JSON array through the CSV's shape rules.
fn rows_from_body(holdings: &[HoldingIn]) -> ParsedCsv {
    let mut parsed = ParsedCsv::default();
    let mut seen: Vec<String> = Vec::new();
    for (index, holding) in holdings.iter().enumerate() {
        let line_number = index + 1;
        let (token, issues) = portfolio_csv::normalise_symbol(&holding.symbol);
        if token.is_empty() {
            parsed.skipped.push(SkippedRow {
                line_number,
                reason: SkipReason::NoSymbol,
                raw: String::new(),
            });
            continue;
        }
        if seen.contains(&token) {
            parsed.skipped.push(SkippedRow {
                line_number,
                reason: SkipReason::Duplicate,
                raw: token,
            });
            continue;
        }
        seen.push(token.clone());
        parsed.rows.push(ParsedHolding {
            line_number,
            raw_symbol: holding.symbol.clone(),
            shape: portfolio_csv::classify_symbol(&token),
            symbol: token,
            quantity: holding.quantity.clone(),
            avg_price: holding.avg_price.clone(),
            issues,
        });
    }
    parsed
}

/// `_apply(session, portfolio, parsed)`: resolve, replace the holdings, report.
async fn apply(
    conn: &mut PgConnection,
    portfolio: &OwnedPortfolio,
    parsed: &ParsedCsv,
) -> Result<ImportReportOut, Problem> {
    let resolved = symbols::resolve_symbols(conn, &symbols::lookup_symbols(&parsed.rows)).await?;
    let mut rows = Vec::with_capacity(parsed.rows.len());
    let mut matched = Vec::new();
    for row in &parsed.rows {
        let resolution: SymbolResolution = symbols::resolution_for(row, &resolved);
        let first = resolution.candidates.first();
        rows.push(ImportRowOut {
            line: row.line_number,
            raw_symbol: row.raw_symbol.clone(),
            symbol: row.symbol.clone(),
            status: resolution.status,
            reason: resolution.reason,
            instrument_id: resolution.instrument_id(),
            name: match (resolution.status, first) {
                (MatchStatus::Matched, Some(c)) => Some(c.name.clone()),
                _ => None,
            },
            candidates: if resolution.status == MatchStatus::Ambiguous {
                resolution
                    .candidates
                    .iter()
                    .map(|c| CandidateOut {
                        instrument_id: c.instrument_id,
                        symbol: c.symbol.clone(),
                        name: c.name.clone(),
                        series: c.series.clone(),
                    })
                    .collect()
            } else {
                Vec::new()
            },
            issues: row.issues.clone(),
            matched_via_alias: resolution.matched_via_alias,
            quantity: row.quantity.clone(),
            avg_price: row.avg_price.clone(),
        });
        if let Some(id) = resolution.instrument_id() {
            matched.push((id, row.quantity.clone(), row.avg_price.clone()));
        }
    }
    let imported = symbols::replace_holdings(
        conn,
        portfolio.id,
        portfolio.user_id,
        portfolio.broker_account_id,
        &matched,
        today_ist(),
    )
    .await?;
    let count = |s: MatchStatus| rows.iter().filter(|r| r.status == s).count();
    Ok(ImportReportOut {
        total_lines: parsed.total_lines(),
        matched: count(MatchStatus::Matched),
        ambiguous: count(MatchStatus::Ambiguous),
        unmatched: count(MatchStatus::Unmatched),
        skipped: parsed.skipped.len(),
        imported,
        skipped_rows: parsed
            .skipped
            .iter()
            .map(|s| SkippedRowOut {
                line: s.line_number,
                reason: s.reason,
                raw: s.raw.clone(),
            })
            .collect(),
        ignored_columns: parsed.ignored_columns.clone(),
        rows,
    })
}

/// `_portfolio_out(session, portfolio)`.
async fn portfolio_out(
    conn: &mut PgConnection,
    portfolio: &OwnedPortfolio,
) -> Result<PortfolioDetailOut, Problem> {
    let (nodes, _) = graph_rows(conn, portfolio.user_id).await?;
    let depth = graph::depth_of(&nodes, portfolio.id).map_err(|e| refuse(&e))?;
    Ok(PortfolioDetailOut {
        id: portfolio.id,
        name: portfolio.name.clone(),
        created_at: portfolio.created_at,
        holdings: holdings_out(conn, portfolio.id).await?,
        parent_id: portfolio.parent_id,
        broker_account_id: portfolio.broker_account_id,
        depth,
        child_ids: nodes
            .iter()
            .filter(|n| n.parent_id == Some(portfolio.id))
            .map(|n| n.id)
            .collect(),
    })
}

/// `_replayed(...)`: the portfolio a previous request with this key created, if it still exists.
async fn replayed(
    conn: &mut PgConnection,
    state: &AppState,
    user_id: i64,
    scope: &str,
    key: Option<&str>,
) -> Result<Option<PortfolioWriteDetailOut>, Problem> {
    let Some(remembered) = idempotency::replay(&state.settings.redis_url, scope, key).await else {
        return Ok(None);
    };
    // `str.isdigit()`: Unicode digits pass it, and `int()` then reads them.
    if remembered.is_empty() || !remembered.chars().all(char::is_numeric) {
        return Ok(None);
    }
    let Some(id) = crate::routes::common::python_int(&remembered) else {
        return Ok(None);
    };
    let id = params::bigint(id.saturated())?;
    let portfolio = match load(conn, PyInt::from(id), user_id).await {
        Ok(p) => p,
        Err(p) if p.status == 404 => return Ok(None),
        Err(p) => return Err(p),
    };
    let holdings = holdings_out(conn, portfolio.id).await?;
    let n = holdings.len();
    Ok(Some(PortfolioWriteDetailOut {
        portfolio: portfolio_out(conn, &portfolio).await?,
        report: ImportReportOut {
            total_lines: n,
            matched: n,
            ambiguous: 0,
            unmatched: 0,
            skipped: 0,
            imported: n,
            rows: Vec::new(),
            skipped_rows: Vec::new(),
            ignored_columns: Vec::new(),
        },
    }))
}

/// `_broker_account_for`: the caller's own account, or `No broker account with id '9'.`
async fn broker_account_for(
    conn: &mut PgConnection,
    id: Option<PyInt>,
    user_id: i64,
) -> Result<Option<i64>, Problem> {
    let Some(raw) = id else {
        return Ok(None);
    };
    let owned = sqlx::query_scalar::<_, i64>(
        "SELECT id FROM broker_account WHERE id = $1 AND user_id = $2",
    )
    .bind(params::bigint(raw)?)
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db::fail)?;
    owned
        .map(Some)
        .ok_or_else(|| not_found("broker account", &raw.to_string()))
}

/// `INSERT INTO portfolio` for a holding group the user assembled; the new row.
async fn insert_portfolio(
    conn: &mut PgConnection,
    user_id: i64,
    name: &str,
    parent_id: Option<i64>,
    broker_account_id: Option<i64>,
) -> Result<OwnedPortfolio, Problem> {
    let row = sqlx::query(
        "INSERT INTO portfolio (user_id, name, parent_id, broker_account_id, kind, source, \
         started_on) VALUES ($1, $2, $3, $4, 'CAPITAL', 'HOLDING_GROUP', $5) \
         RETURNING id, created_at",
    )
    .bind(user_id)
    .bind(name)
    .bind(parent_id)
    .bind(broker_account_id)
    .bind(today_ist())
    .fetch_one(&mut *conn)
    .await
    .map_err(db::fail)?;
    let created_at: DateTime<Utc> = db::ts(&row, "created_at")?;
    Ok(OwnedPortfolio {
        id: db::get(&row, "id")?,
        user_id,
        name: name.to_owned(),
        created_at,
        parent_id,
        broker_account_id,
    })
}

/// `POST /portfolios`: create a portfolio, optionally under a parent, and file its holdings.
///
/// # Errors
/// 400 for the body or a depth breach; 401 anonymous; 404 for a foreign parent or account.
pub async fn create_portfolio(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (headers, content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let key = header_value(&headers, idempotency::HEADER);
    let mut errors = Errors::default();
    let mut body = None;
    if let Some(entries) = root(&received, &mut errors) {
        let name = model::required_string(entries, "name", "", (Some(1), Some(120)), &mut errors);
        let holdings = match model::get(entries, "holdings") {
            None => Some(Vec::new()),
            Some(v) => holdings_in(v, "holdings", &mut errors),
        };
        let parent_id =
            model::optional_int(entries, "parent_id", "", Bounds::default(), &mut errors);
        let broker = model::optional_int(
            entries,
            "broker_account_id",
            "",
            Bounds::default(),
            &mut errors,
        );
        model::forbid_extra(
            entries,
            &["name", "holdings", "parent_id", "broker_account_id"],
            "",
            &mut errors,
        );
        if let (Some(name), Some(holdings), Ok(parent_id), Ok(broker)) =
            (name, holdings, parent_id, broker)
        {
            body = Some((name, holdings, parent_id, broker));
        }
    }
    errors.finish()?;
    let Some((name, holdings, parent_id, broker)) = body else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    if holdings.len() > MAX_IMPORT_ROWS {
        return Err(too_many(holdings.len()));
    }
    let pool = pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let scope = format!("portfolio:create:{user_id}");
            if let Some(replay) = replayed(conn, &state, user_id, &scope, key.as_deref()).await? {
                return Ok(replay);
            }
            let parent = match parent_id {
                Some(raw) => Some(load(conn, raw, user_id).await?.id),
                None => None,
            };
            let broker_account_id = broker_account_for(conn, broker, user_id).await?;
            if let Some(parent) = parent {
                let (nodes, _) = graph_rows(conn, user_id).await?;
                let depth = graph::depth_of(&nodes, parent).map_err(|e| refuse(&e))? + 1;
                if depth > MAX_DEPTH {
                    return Err(Problem::new(
                        ProblemType::InvalidScreenDefinition,
                        format!(
                            "A portfolio under {parent} would sit at depth {depth}, deeper \
                             than the limit of {MAX_DEPTH}."
                        ),
                    ));
                }
            }
            let portfolio =
                insert_portfolio(conn, user_id, &name, parent, broker_account_id).await?;
            let report = apply(conn, &portfolio, &rows_from_body(&holdings)).await?;
            idempotency::remember(
                &state.settings.redis_url,
                &scope,
                key.as_deref(),
                &portfolio.id.to_string(),
            )
            .await;
            Ok(PortfolioWriteDetailOut {
                portfolio: portfolio_out(conn, &portfolio).await?,
                report,
            })
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// `_name_from(filename)`.
fn name_from(filename: &str) -> String {
    let stem = filename.rsplit('/').next().unwrap_or("");
    let stem: String = if stem.to_lowercase().ends_with(".csv") {
        let n = stem.chars().count();
        stem.chars().take(n.saturating_sub(4)).collect()
    } else {
        stem.to_owned()
    };
    let cut: String = model::py_strip(&stem).chars().take(120).collect();
    if cut.is_empty() {
        "Imported portfolio".to_owned()
    } else {
        cut
    }
}

/// `POST /portfolios/import-csv?name=&portfolio_id=` (multipart `file`).
///
/// # Errors
/// 400 for the form, the query, the file's size, encoding or shape; 401 anonymous; 404 for a
/// foreign `portfolio_id`.
pub async fn import_csv(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    RawQuery(raw): RawQuery,
    request: Request,
) -> Result<Response, Problem> {
    let (headers, content_type, bytes) = read_body(request).await?;
    let file = multipart::form_field(content_type.as_deref(), &bytes, "file")?;
    let principal = require_authenticated(&principal)?.clone();
    let q = QueryParams::parse(raw.as_deref());
    let mut errors = Errors::default();
    let name = q.get("name").map(|n| {
        model::string(
            &OrderedJson::Str(n.to_owned()),
            "name",
            None,
            Some(120),
            &mut errors,
        )
    });
    let portfolio_id = q.get("portfolio_id").map(|p| {
        model::int(
            &OrderedJson::Str(p.to_owned()),
            "portfolio_id",
            Bounds::default(),
            &mut errors,
        )
    });
    let key = header_value(&headers, idempotency::HEADER);
    let upload = match file {
        None => {
            errors.push("file", "Field required", "missing");
            None
        }
        Some(FormValue::Text(_)) => {
            errors.push("file", multipart::NOT_A_FILE, "value_error");
            None
        }
        Some(FormValue::File { filename, data }) => Some((filename, data)),
    };
    errors.finish()?;
    let (Some((filename, data)), Some(name), Some(portfolio_id)) = (
        upload,
        name.map_or(Some(None), |n| n.map(Some)),
        portfolio_id.map_or(Some(None), |p| p.map(Some)),
    ) else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    if data.len() > MAX_UPLOAD_BYTES {
        return Err(Problem::new(
            ProblemType::InvalidScreenDefinition,
            format!(
                "The file is {} bytes; the limit is {MAX_UPLOAD_BYTES}.",
                data.len()
            ),
        ));
    }
    let text = std::str::from_utf8(&data).map_err(|_| {
        Problem::new(
            ProblemType::InvalidScreenDefinition,
            "The file is not UTF-8 text. Export it as CSV rather than as a spreadsheet.",
        )
    })?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let parsed = match portfolio_csv::parse_portfolio_csv(text) {
        Ok(p) => p,
        Err(ParseFailure::Refused(m)) => {
            return Err(Problem::new(ProblemType::InvalidScreenDefinition, m));
        }
        Err(ParseFailure::Csv(e)) => return Err(super::fail(&format!("csv.Error: {}", e.0))),
    };
    let pool = pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let portfolio = if let Some(pid) = portfolio_id {
                load(conn, pid, user_id).await?
            } else {
                let scope = format!("portfolio:import:{user_id}");
                if let Some(replay) =
                    replayed(conn, &state, user_id, &scope, key.as_deref()).await?
                {
                    return Ok(replay);
                }
                let chosen = match name.filter(|n| !n.is_empty()) {
                    Some(n) => n,
                    None => name_from(&filename),
                };
                let portfolio = insert_portfolio(conn, user_id, &chosen, None, None).await?;
                idempotency::remember(
                    &state.settings.redis_url,
                    &scope,
                    key.as_deref(),
                    &portfolio.id.to_string(),
                )
                .await;
                portfolio
            };
            let report = apply(conn, &portfolio, &parsed).await?;
            Ok(PortfolioWriteDetailOut {
                portfolio: portfolio_out(conn, &portfolio).await?,
                report,
            })
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// `PATCH /portfolios/{portfolio_id}`: rename, move or re-attribute — only the fields sent.
///
/// # Errors
/// 400 for the body or a refused move; 401 anonymous; 404 for a foreign portfolio, parent or
/// account.
pub async fn update_portfolio(
    State(_state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let (_, content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let mut errors = Errors::default();
    let id = path_id(&raw_id, &mut errors);
    let mut body = None;
    if let Some(entries) = root(&received, &mut errors) {
        let name = match model::get(entries, "name") {
            None | Some(OrderedJson::Null) => Ok(None),
            Some(v) => model::string(v, "name", Some(1), Some(120), &mut errors)
                .map(Some)
                .ok_or(()),
        };
        let parent = model::optional_int(entries, "parent_id", "", Bounds::default(), &mut errors);
        let broker = model::optional_int(
            entries,
            "broker_account_id",
            "",
            Bounds::default(),
            &mut errors,
        );
        model::forbid_extra(
            entries,
            &["name", "parent_id", "broker_account_id"],
            "",
            &mut errors,
        );
        let sent = |k: &str| model::get(entries, k).is_some();
        if let (Ok(name), Ok(parent), Ok(broker)) = (name, parent, broker) {
            body = Some((
                name,
                sent("parent_id").then_some(parent),
                sent("broker_account_id").then_some(broker),
            ));
        }
    }
    errors.finish()?;
    let (Some(id), Some((name, parent, broker))) = (id, body) else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    let pool = pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let mut portfolio = load(conn, id, user_id).await?;
            if let Some(broker) = broker {
                portfolio.broker_account_id = broker_account_for(conn, broker, user_id).await?;
            }
            if let Some(parent) = parent {
                let parent_id = match parent {
                    Some(raw) => Some(load(conn, raw, user_id).await?.id),
                    None => None,
                };
                let (nodes, _) = graph_rows(conn, user_id).await?;
                graph::check_move(&nodes, portfolio.id, parent_id).map_err(|e| refuse(&e))?;
                portfolio.parent_id = parent_id;
            }
            if let Some(name) = name {
                portfolio.name = name;
            }
            // The ORM writes only the columns whose value changed.
            sqlx::query(
                "UPDATE portfolio SET name = $2, parent_id = $3, broker_account_id = $4 \
                 WHERE id = $1 AND (name IS DISTINCT FROM $2 OR parent_id IS DISTINCT FROM $3 \
                 OR broker_account_id IS DISTINCT FROM $4)",
            )
            .bind(portfolio.id)
            .bind(&portfolio.name)
            .bind(portfolio.parent_id)
            .bind(portfolio.broker_account_id)
            .execute(&mut *conn)
            .await
            .map_err(db::fail)?;
            portfolio_out(conn, &portfolio).await
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `DELETE /portfolios/{portfolio_id}`: 204; children are promoted to roots (`ON DELETE SET NULL`).
///
/// # Errors
/// 400 for a non-integer id; 401 anonymous; 404 when not the caller's.
pub async fn delete_portfolio(
    State(_state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let mut errors = Errors::default();
    let id = path_id(&raw_id, &mut errors);
    errors.finish()?;
    let id = id.unwrap_or_default();
    let user_id = principal.require_user()?;
    let pool = pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let portfolio = load(conn, id, user_id).await?;
            sqlx::query("DELETE FROM portfolio WHERE id = $1")
                .bind(portfolio.id)
                .execute(&mut *conn)
                .await
                .map_err(db::fail)?;
            Ok(())
        })
    })
    .await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

/// `PUT /portfolios/{portfolio_id}/holdings`: replace the holdings (a replacement, not a merge).
///
/// # Errors
/// 400 for the body or more than 500 names; 401 anonymous; 404 when not the caller's.
pub async fn put_holdings(
    State(_state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let (_, content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let mut errors = Errors::default();
    let id = path_id(&raw_id, &mut errors);
    let mut body = None;
    if let Some(entries) = root(&received, &mut errors) {
        let holdings = match model::get(entries, "holdings") {
            None => {
                errors.push("holdings", "Field required", "missing");
                None
            }
            Some(v) => holdings_in(v, "holdings", &mut errors),
        };
        model::forbid_extra(entries, &["holdings"], "", &mut errors);
        body = holdings;
    }
    errors.finish()?;
    let (Some(id), Some(holdings)) = (id, body) else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    let pool = pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let portfolio = load(conn, id, user_id).await?;
            if holdings.len() > MAX_IMPORT_ROWS {
                return Err(too_many(holdings.len()));
            }
            let report = apply(conn, &portfolio, &rows_from_body(&holdings)).await?;
            Ok(PortfolioWriteDetailOut {
                portfolio: portfolio_out(conn, &portfolio).await?,
                report,
            })
        })
    })
    .await?;
    Ok(wire::json(&out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_import_is_named_from_its_file() {
        // Ports test_api_portfolios.py's `_name_from` assertions.
        assert_eq!(name_from("dir/My Book.CSV"), "My Book");
        assert_eq!(name_from(".csv"), "Imported portfolio");
        assert_eq!(name_from(""), "Imported portfolio");
        assert_eq!(name_from("  holdings.csv"), "holdings");
        assert_eq!(name_from(&"x".repeat(200)).chars().count(), 120);
    }

    #[test]
    fn a_json_body_gets_the_csv_shape_rules() {
        let holdings = [" infy ", "INFY", "   ", "tcs.ns"].map(|s| HoldingIn {
            symbol: s.to_owned(),
            quantity: None,
            avg_price: None,
        });
        let parsed = rows_from_body(&holdings);
        assert_eq!(parsed.rows.len(), 2);
        assert_eq!(parsed.rows[0].raw_symbol, " infy ");
        let reasons: Vec<SkipReason> = parsed.skipped.iter().map(|s| s.reason).collect();
        assert_eq!(reasons, vec![SkipReason::Duplicate, SkipReason::NoSymbol]);
        assert_eq!(parsed.total_lines(), 4);
    }
}
