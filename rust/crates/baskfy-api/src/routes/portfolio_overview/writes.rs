//! The write routes of `baskfy_api.routers.portfolio_overview`: `POST /portfolio`,
//! `POST /portfolio/{portfolio_id}/holdings` and `POST /portfolio/reconciliation/{item_id}/resolve`.
//!
//! **Not order paths.** Each records bookkeeping — a grouping, which portfolio a quantity of an
//! existing holding counts against, the answer to a reconciliation question. None reaches a
//! broker or names a side (law 2, §9). `_apply_allocation` is ported statement for statement:
//! the quantity moves (the broker pile first, then other slices smallest first), shares are
//! conserved, and the rows it touches are locked first (`FOR UPDATE`).

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use serde::Serialize;
use sqlx::PgConnection;

use super::{
    PortfolioRefOut, ReconciliationItemOut, detail_on, item_out, owned_portfolio, portfolio_ref,
};
use crate::auth::{Principal, require_authenticated};
use crate::body::{Received, receive};
use crate::problems::{Problem, ProblemType, not_found};
use crate::routes::common::today_ist;
use crate::routes::portfolios::portfolio::ledger::{HoldingKey, PortfolioKind};
use crate::routes::portfolios::portfolio::load::{Ledger, Position, load_ledger};
use crate::routes::portfolios::portfolio::model::{self, Bounds, Errors, PyInt};
use crate::routes::portfolios::portfolio::pydec::Dec;
use crate::routes::portfolios::portfolio::request::{pool, read_body};
use crate::routes::portfolios::portfolio::{db, fail, params};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

/// `ALLOCATION_REFUSED` (the 400 `invalid-screen-definition`).
fn refused(detail: String) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, detail)
}

/// `NAME_MAX`.
const NAME_MAX: usize = 120;

/// `format(d, "g")` for a `Decimal` with no precision: `str(d)` with a lower-case exponent.
fn g(d: &Dec) -> String {
    d.to_sci_string().replace('E', "e")
}

fn zero() -> Dec {
    Dec::lit("0")
}

/// One `HoldingKeyIn`.
struct HoldingKeyIn {
    key: HoldingKey,
    quantity: Option<Dec>,
}

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

fn gt0() -> Bounds {
    Bounds {
        gt: Some(0),
        ..Bounds::default()
    }
}

fn to_dec(d: &WideDecimal) -> Dec {
    Dec::parse(&d.to_sci_string()).unwrap_or_else(zero)
}

/// `list[HoldingKeyIn]` at `loc`.
fn holding_keys(
    value: &OrderedJson,
    loc: &str,
    min_length: Option<usize>,
    errors: &mut Errors,
) -> Option<Vec<HoldingKeyIn>> {
    let items = model::list(value, loc, min_length, errors)?;
    let before = errors.0.len();
    let mut out = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        let at = model::at(loc, &i.to_string());
        let Some(entries) = model::object(item, &at, errors) else {
            continue;
        };
        let int_field = |name: &str, errors: &mut Errors| -> Option<PyInt> {
            let loc = model::at(&at, name);
            match model::get(entries, name) {
                None => {
                    errors.push(&loc, "Field required", "missing");
                    None
                }
                Some(v) => model::int(v, &loc, gt0(), errors),
            }
        };
        let instrument = int_field("instrument_id", errors);
        let broker = int_field("broker_account_id", errors);
        let quantity = model::optional_decimal(entries, "quantity", &at, true, errors);
        model::forbid_extra(
            entries,
            &["instrument_id", "broker_account_id", "quantity"],
            &at,
            errors,
        );
        if let (Some(i), Some(b), Ok(q)) = (instrument, broker, quantity) {
            // An id beyond `bigint` names nothing (saturated here; DECISIONS-RS R6.24).
            let saturate =
                |v: PyInt| i64::try_from(v).unwrap_or(if v < 0 { i64::MIN } else { i64::MAX });
            let (instrument_id, broker_account_id) = (saturate(i), saturate(b));
            out.push(HoldingKeyIn {
                key: HoldingKey {
                    instrument_id,
                    broker_account_id,
                },
                quantity: q.as_ref().map(to_dec),
            });
        }
    }
    (errors.0.len() == before).then_some(out)
}

/// `_requested_quantities(lines)`: one request per holding; lines add up, `None` means "all".
fn requested_quantities(lines: &[HoldingKeyIn]) -> Vec<(HoldingKey, Option<Dec>)> {
    let mut requested: Vec<(HoldingKey, Option<Dec>)> = Vec::new();
    for line in lines {
        let slot = requested.iter().position(|(k, _)| *k == line.key);
        match (&line.quantity, slot) {
            (None, Some(i)) => requested[i].1 = None,
            (None, None) => requested.push((line.key, None)),
            (Some(q), None) => requested.push((line.key, Some(q.clone()))),
            (Some(q), Some(i)) => {
                if let Some(existing) = requested[i].1.clone() {
                    requested[i].1 = Some(existing.add(q));
                }
            }
        }
    }
    requested
}

/// `wanted = sorted(requested, key=(instrument_id, broker_account_id))`.
fn sorted_keys(requested: &[(HoldingKey, Option<Dec>)]) -> Vec<HoldingKey> {
    let mut keys: Vec<HoldingKey> = requested.iter().map(|(k, _)| *k).collect();
    keys.sort_by_key(|k| (k.instrument_id, k.broker_account_id));
    keys
}

fn asked(requested: &[(HoldingKey, Option<Dec>)], key: HoldingKey) -> Option<Dec> {
    requested
        .iter()
        .find(|(k, _)| *k == key)
        .and_then(|(_, q)| q.clone())
}

/// `_owned_broker_account`.
async fn owned_broker_account(
    conn: &mut PgConnection,
    id: i64,
    user_id: i64,
) -> Result<(), Problem> {
    let found = sqlx::query_scalar::<_, i64>(
        "SELECT id FROM broker_account WHERE id = $1 AND user_id = $2",
    )
    .bind(id)
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db::fail)?;
    found
        .map(|_| ())
        .ok_or_else(|| not_found("broker account", &id.to_string()))
}

fn position(ledger: &Ledger, key: HoldingKey) -> Result<&Position, Problem> {
    ledger
        .positions
        .iter()
        .find(|p| p.key == key)
        .ok_or_else(|| {
            not_found(
                "holding",
                &format!(
                    "instrument {} at broker account {}",
                    key.instrument_id, key.broker_account_id
                ),
            )
        })
}

fn symbol_of(ledger: &Ledger, instrument_id: i64) -> String {
    ledger
        .instruments
        .get(&instrument_id)
        .map_or_else(|| instrument_id.to_string(), |i| i.symbol.clone())
}

fn slice_of(p: &Position, portfolio_id: i64) -> Option<Dec> {
    p.capital_slices
        .iter()
        .find(|(id, _)| *id == portfolio_id)
        .map(|(_, q)| q.clone())
}

/// `_apply_allocation(session, ledger, allocation, resolved_on=)`: file `wanted` shares into
/// `target`, taking them from where they already are. Shares are conserved.
async fn apply_allocation(
    conn: &mut PgConnection,
    ledger: &Ledger,
    key: HoldingKey,
    target: i64,
    wanted: &Dec,
    resolved_on: NaiveDate,
) -> Result<(), Problem> {
    let Some(existing) = ledger.positions.iter().find(|p| p.key == key) else {
        sqlx::query(
            "INSERT INTO portfolio_holding (portfolio_id, instrument_id, broker_account_id, \
             portfolio_kind, quantity, avg_price, added_on, first_bought_on, history_source) \
             VALUES ($1, $2, $3, 'CAPITAL', $4::numeric, NULL, $5, NULL, 'NONE')",
        )
        .bind(target)
        .bind(key.instrument_id)
        .bind(key.broker_account_id)
        .bind(wanted.to_sci_string())
        .bind(resolved_on)
        .execute(&mut *conn)
        .await
        .map_err(db::fail)?;
        return Ok(());
    };
    if *wanted > existing.holding.quantity {
        return Err(fail(&format!(
            "ValueError: cannot file {} shares: only {} are held",
            wanted.to_sci_string(),
            existing.holding.quantity.to_sci_string()
        )));
    }
    sqlx::query(
        "SELECT portfolio_id FROM portfolio_holding WHERE instrument_id = $1 \
         AND broker_account_id = $2 FOR UPDATE",
    )
    .bind(key.instrument_id)
    .bind(key.broker_account_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut piles = existing.pile_slices.clone();
    piles.sort_by_key(|(id, _)| *id);
    let mut others: Vec<(i64, Dec)> = existing
        .capital_slices
        .iter()
        .filter(|(id, _)| *id != target)
        .cloned()
        .collect();
    others.sort_by(|a, b| {
        a.1.partial_cmp(&b.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.0.cmp(&b.0))
    });
    let mut outstanding = wanted.clone();
    for (donor, donor_quantity) in piles.into_iter().chain(others) {
        if outstanding <= zero() {
            break;
        }
        let taken = if outstanding < donor_quantity {
            outstanding.clone()
        } else {
            donor_quantity.clone()
        };
        outstanding = outstanding.sub(&taken);
        if taken >= donor_quantity {
            sqlx::query(
                "DELETE FROM portfolio_holding WHERE portfolio_id = $1 AND instrument_id = $2 \
                 AND broker_account_id = $3",
            )
            .bind(donor)
            .bind(key.instrument_id)
            .bind(key.broker_account_id)
            .execute(&mut *conn)
            .await
            .map_err(db::fail)?;
        } else {
            sqlx::query(
                "UPDATE portfolio_holding SET quantity = $1::numeric WHERE portfolio_id = $2 \
                 AND instrument_id = $3 AND broker_account_id = $4",
            )
            .bind(donor_quantity.sub(&taken).to_sci_string())
            .bind(donor)
            .bind(key.instrument_id)
            .bind(key.broker_account_id)
            .execute(&mut *conn)
            .await
            .map_err(db::fail)?;
        }
    }
    if let Some(already) = slice_of(existing, target) {
        sqlx::query(
            "UPDATE portfolio_holding SET quantity = $1::numeric WHERE portfolio_id = $2 \
             AND instrument_id = $3 AND broker_account_id = $4",
        )
        .bind(already.add(wanted).to_sci_string())
        .bind(target)
        .bind(key.instrument_id)
        .bind(key.broker_account_id)
        .execute(&mut *conn)
        .await
        .map_err(db::fail)?;
        return Ok(());
    }
    sqlx::query(
        "INSERT INTO portfolio_holding (portfolio_id, instrument_id, broker_account_id, \
         portfolio_kind, quantity, avg_price, added_on, first_bought_on, history_source) \
         VALUES ($1, $2, $3, 'CAPITAL', $4::numeric, $5::numeric, $6, $7, $8)",
    )
    .bind(target)
    .bind(key.instrument_id)
    .bind(key.broker_account_id)
    .bind(wanted.to_sci_string())
    .bind(existing.holding.avg_price.as_ref().map(Dec::to_sci_string))
    .bind(resolved_on)
    .bind(existing.first_bought_on)
    .bind(&existing.history_source)
    .execute(&mut *conn)
    .await
    .map_err(db::fail)?;
    Ok(())
}

/// `_add_to_monitoring_view`: a lens row copying the position's facts.
async fn add_to_monitoring_view(
    conn: &mut PgConnection,
    portfolio_id: i64,
    p: &Position,
    added_on: NaiveDate,
) -> Result<(), Problem> {
    sqlx::query(
        "INSERT INTO portfolio_holding (portfolio_id, instrument_id, broker_account_id, \
         portfolio_kind, quantity, avg_price, added_on, first_bought_on, history_source) \
         VALUES ($1, $2, $3, 'MONITORING', $4::numeric, $5::numeric, $6, $7, $8)",
    )
    .bind(portfolio_id)
    .bind(p.key.instrument_id)
    .bind(p.key.broker_account_id)
    .bind(p.holding.quantity.to_sci_string())
    .bind(p.holding.avg_price.as_ref().map(Dec::to_sci_string))
    .bind(added_on)
    .bind(p.first_bought_on)
    .bind(&p.history_source)
    .execute(&mut *conn)
    .await
    .map_err(db::fail)?;
    Ok(())
}

fn ids_json(ids: &[i64]) -> OrderedJson {
    OrderedJson::Array(ids.iter().map(|i| OrderedJson::Int(*i)).collect())
}

/// `_refuse_over_allocation(ledger, chosen)`.
fn refuse_over_allocation(ledger: &Ledger, chosen: &[(&Position, Dec)]) -> Result<(), Problem> {
    let conflicts: Vec<&(&Position, Dec)> = chosen
        .iter()
        .filter(|(p, wanted)| *wanted > p.unallocated_quantity())
        .collect();
    if conflicts.is_empty() {
        return Ok(());
    }
    let mut named = Vec::new();
    let mut ids = Vec::new();
    let mut rows = Vec::new();
    for (p, wanted) in &conflicts {
        let free = p.unallocated_quantity();
        let place = if p.capital_slices.is_empty() {
            String::new()
        } else {
            let parts: Vec<String> = p
                .capital_slices
                .iter()
                .map(|(pid, q)| {
                    let name = ledger
                        .portfolios
                        .get(pid)
                        .map_or_else(|| pid.to_string(), |lp| lp.name.clone());
                    format!("{} in {}", g(q), baskfy_providers::py::repr_str(&name))
                })
                .collect();
            format!(", already {}", parts.join(" and "))
        };
        named.push(format!(
            "{}: you asked for {} and only {} are free{place}",
            symbol_of(ledger, p.key.instrument_id),
            g(wanted),
            g(&free)
        ));
        ids.push(p.key.instrument_id);
        rows.push(OrderedJson::Object(vec![
            (
                "instrument_id".to_owned(),
                OrderedJson::Int(p.key.instrument_id),
            ),
            (
                "broker_account_id".to_owned(),
                OrderedJson::Int(p.key.broker_account_id),
            ),
            (
                "requested".to_owned(),
                OrderedJson::Str(wanted.to_sci_string()),
            ),
            (
                "available".to_owned(),
                OrderedJson::Str(free.to_sci_string()),
            ),
            (
                "slices".to_owned(),
                OrderedJson::Object(
                    p.capital_slices
                        .iter()
                        .map(|(pid, q)| (pid.to_string(), OrderedJson::Str(q.to_sci_string())))
                        .collect(),
                ),
            ),
        ]));
    }
    Err(refused(format!(
        "A holding cannot be filed into more shares than you own — {}. Lower those quantities, \
         or free the shares from the portfolios they are in.",
        named.join("; ")
    ))
    .with("conflicts", OrderedJson::Array(rows))
    .with("instrument_ids", ids_json(&ids)))
}

/// `PortfolioDetailOut` of the overview, after a write, on the write's transaction.
async fn detail_after(
    conn: &mut PgConnection,
    user_id: i64,
    portfolio_id: i64,
) -> Result<super::PortfolioDetailOut, Problem> {
    let benchmark = owned_portfolio(conn, PyInt::from(portfolio_id), user_id).await?;
    detail_on(conn, user_id, portfolio_id, benchmark).await
}

/// `POST /portfolio`: name a grouping, choose its kind and source, and file holdings into it.
///
/// # Errors
/// 400 for the body or a refused allocation; 401 anonymous; 404 for a foreign benchmark, broker
/// account or holding.
#[allow(clippy::too_many_lines)] // the route's four refusals, in Python's order
pub async fn new_portfolio(
    State(_state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (_, content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let mut errors = Errors::default();
    let mut body = None;
    if let Some(entries) = root(&received, &mut errors) {
        let name =
            model::required_string(entries, "name", "", (Some(1), Some(NAME_MAX)), &mut errors);
        let kind = model::choice(entries, "kind", "", &["CAPITAL", "MONITORING"], &mut errors);
        let source = model::choice(
            entries,
            "source",
            "",
            &["SUBSCRIBED", "MY_SCREEN", "MY_STRATEGY", "HOLDING_GROUP"],
            &mut errors,
        );
        let benchmark = model::optional_int(entries, "benchmark_index_id", "", gt0(), &mut errors);
        let holdings = match model::get(entries, "holdings") {
            None => Some(Vec::new()),
            Some(v) => holding_keys(v, "holdings", None, &mut errors),
        };
        model::forbid_extra(
            entries,
            &["name", "kind", "source", "benchmark_index_id", "holdings"],
            "",
            &mut errors,
        );
        if let (Some(n), Some(k), Some(s), Ok(b), Some(h)) =
            (name, kind, source, benchmark, holdings)
        {
            body = Some((n, k, s, b, h));
        }
    }
    errors.finish()?;
    let Some((name, kind, source, benchmark, holdings)) = body else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    let capital = kind == "CAPITAL";
    let pool = pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let today = today_ist();
            let benchmark = match benchmark {
                None => None,
                Some(raw) => {
                    // `index_def.id` is a smallint: asyncpg refuses a wider value (a 500).
                    let id = i16::try_from(raw)
                        .map_err(|_| fail("DataError: value out of int16 range"))?;
                    let found =
                        sqlx::query_scalar::<_, i16>("SELECT id FROM index_def WHERE id = $1")
                            .bind(id)
                            .fetch_optional(&mut *conn)
                            .await
                            .map_err(db::fail)?;
                    if found.is_none() {
                        return Err(not_found("benchmark index", &raw.to_string()));
                    }
                    Some(id)
                }
            };
            let requested = requested_quantities(&holdings);
            let wanted = sorted_keys(&requested);
            for key in &wanted {
                owned_broker_account(conn, key.broker_account_id, user_id).await?;
            }
            let ledger = load_ledger(conn, user_id).await?;
            let mut chosen: Vec<(&Position, Dec)> = Vec::new();
            for key in &wanted {
                let p = position(&ledger, *key)?;
                let q = asked(&requested, *key).unwrap_or_else(|| p.unallocated_quantity());
                chosen.push((p, q));
            }
            if capital {
                refuse_over_allocation(&ledger, &chosen)?;
                let empty: Vec<&(&Position, Dec)> =
                    chosen.iter().filter(|(_, q)| *q <= zero()).collect();
                if !empty.is_empty() {
                    let names: Vec<String> = empty
                        .iter()
                        .map(|(p, _)| symbol_of(&ledger, p.key.instrument_id))
                        .collect();
                    let ids: Vec<i64> = empty.iter().map(|(p, _)| p.key.instrument_id).collect();
                    return Err(refused(format!(
                        "Every holding in a capital portfolio needs shares in it, and {} has none \
                         free. Free some, or leave it out of the selection.",
                        names.join(", ")
                    ))
                    .with("instrument_ids", ids_json(&ids)));
                }
            }
            let mut accounts: Vec<i64> = chosen
                .iter()
                .map(|(p, _)| p.key.broker_account_id)
                .collect();
            accounts.sort_unstable();
            accounts.dedup();
            let broker_account_id = (accounts.len() == 1).then(|| accounts[0]);
            let block: Result<i64, Problem> = async {
                let portfolio_id = sqlx::query_scalar::<_, i64>(
                    "INSERT INTO portfolio (user_id, name, kind, source, started_on, \
                     benchmark_index_id, broker_account_id) VALUES ($1, $2, $3, $4, $5, $6, $7) \
                     RETURNING id",
                )
                .bind(user_id)
                .bind(&name)
                .bind(kind)
                .bind(source)
                .bind(today)
                .bind(benchmark)
                .bind(broker_account_id)
                .fetch_one(&mut *conn)
                .await
                .map_err(integrity)?;
                for (p, quantity) in &chosen {
                    if capital {
                        // `Allocation(...)` refuses a non-positive quantity (a ValueError).
                        if *quantity <= zero() {
                            return Err(fail(
                                "ValueError: an allocation must be a positive quantity",
                            ));
                        }
                        apply_allocation(conn, &ledger, p.key, portfolio_id, quantity, today)
                            .await?;
                    } else {
                        add_to_monitoring_view(conn, portfolio_id, p, today).await?;
                    }
                }
                Ok(portfolio_id)
            }
            .await;
            let portfolio_id = block?;
            detail_after(conn, user_id, portfolio_id).await
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// The `IntegrityError` window `new_portfolio` answers in words; any other failure is the 500.
fn integrity(e: sqlx::Error) -> Problem {
    let unique = e
        .as_database_error()
        .and_then(|d| d.code())
        .is_some_and(|c| c.starts_with("23"));
    if unique {
        refused(
            "One of these holdings was put into another capital portfolio while this one was \
             being created. A holding belongs to exactly one capital portfolio. Reload your \
             holdings and try again."
                .to_owned(),
        )
    } else {
        db::fail(e)
    }
}

/// The path's `portfolio_id`/`item_id` (`Path(ge=1)`).
fn path_ge1(raw: &str, field: &str, errors: &mut Errors) -> Option<PyInt> {
    model::int(
        &OrderedJson::Str(raw.to_owned()),
        field,
        Bounds {
            ge: Some(1),
            ..Bounds::default()
        },
        errors,
    )
}

/// `POST /portfolio/{portfolio_id}/holdings`: file more shares into an existing portfolio
/// (moving them from where they are, free shares first).
///
/// # Errors
/// 400 for the body, a broker pile as the target, or more shares than are held; 401 anonymous;
/// 404 for a foreign portfolio, account or holding.
pub async fn add_holdings(
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
    let pid = path_ge1(&raw_id, "portfolio_id", &mut errors);
    let mut body = None;
    if let Some(entries) = root(&received, &mut errors) {
        body = match model::get(entries, "holdings") {
            None => {
                errors.push("holdings", "Field required", "missing");
                None
            }
            Some(v) => holding_keys(v, "holdings", Some(1), &mut errors),
        };
        model::forbid_extra(entries, &["holdings"], "", &mut errors);
    }
    errors.finish()?;
    let (Some(pid), Some(holdings)) = (pid, body) else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    let pool = pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            owned_portfolio(conn, pid, user_id).await?;
            let pid = params::bigint(pid)?;
            let requested = requested_quantities(&holdings);
            let wanted = sorted_keys(&requested);
            for key in &wanted {
                owned_broker_account(conn, key.broker_account_id, user_id).await?;
            }
            let ledger = load_ledger(conn, user_id).await?;
            let portfolio = ledger
                .portfolios
                .get(&pid)
                .ok_or_else(|| fail(&format!("KeyError: {pid}")))?
                .clone();
            if ledger.pile_ids.contains(&pid) {
                return Err(refused(format!(
                    "{} is where unsorted shares already live, so there is nothing to add to \
                     it. Remove them from the portfolio they are in instead.",
                    baskfy_providers::py::repr_str(&portfolio.name)
                )));
            }
            let today = today_ist();
            let mut chosen: Vec<(&Position, Dec)> = Vec::new();
            for key in &wanted {
                let p = position(&ledger, *key)?;
                let held_here = slice_of(p, pid).unwrap_or_else(zero);
                let q =
                    asked(&requested, *key).unwrap_or_else(|| p.holding.quantity.sub(&held_here));
                chosen.push((p, q));
            }
            let capital = portfolio.kind == PortfolioKind::Capital;
            if capital {
                let over: Vec<&(&Position, Dec)> = chosen
                    .iter()
                    .filter(|(p, w)| {
                        *w > p
                            .holding
                            .quantity
                            .sub(&slice_of(p, pid).unwrap_or_else(zero))
                    })
                    .collect();
                if !over.is_empty() {
                    let named: Vec<String> = over
                        .iter()
                        .map(|(p, w)| {
                            format!(
                                "{}: asked for {}, {} held in total",
                                symbol_of(&ledger, p.key.instrument_id),
                                g(w),
                                g(&p.holding.quantity)
                            )
                        })
                        .collect();
                    let ids: Vec<i64> = over.iter().map(|(p, _)| p.key.instrument_id).collect();
                    return Err(refused(format!(
                        "You cannot file more shares than you own — {}.",
                        named.join("; ")
                    ))
                    .with("instrument_ids", ids_json(&ids)));
                }
            }
            for (p, quantity) in &chosen {
                if *quantity <= zero() {
                    continue;
                }
                if capital {
                    apply_allocation(conn, &ledger, p.key, pid, quantity, today).await?;
                } else if !p.monitoring_portfolio_ids.contains(&pid) {
                    add_to_monitoring_view(conn, pid, p, today).await?;
                }
            }
            detail_after(conn, user_id, pid).await
        })
    })
    .await?;
    Ok(wire::json(&out))
}

fn state_name(
    state: crate::routes::portfolios::portfolio::recon::ReconciliationState,
) -> &'static str {
    use crate::routes::portfolios::portfolio::recon::ReconciliationState as S;
    match state {
        S::Open => "OPEN",
        S::Resolved => "RESOLVED",
        S::Dismissed => "DISMISSED",
    }
}

/// `ResolveOut`.
#[derive(Debug, Clone, Serialize)]
struct ResolveOut {
    item: ReconciliationItemOut,
    allocated_to: PortfolioRefOut,
    unfroze: bool,
}

/// `POST /portfolio/reconciliation/{item_id}/resolve`: answer one question by naming the
/// capital portfolio the change belongs to.
///
/// # Errors
/// 400 for the body or a refusal of `reconciliation.resolve`; 401 anonymous; 404 for a foreign
/// item or portfolio.
pub async fn resolve_reconciliation_item(
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
    let item_id = path_ge1(&raw_id, "item_id", &mut errors);
    let mut target = None;
    if let Some(entries) = root(&received, &mut errors) {
        // `ResolveBody` ignores extra keys (no `extra="forbid"`).
        target = match model::get(entries, "portfolio_id") {
            None => {
                errors.push("portfolio_id", "Field required", "missing");
                None
            }
            Some(v) => model::int(v, "portfolio_id", gt0(), &mut errors),
        };
    }
    errors.finish()?;
    let (Some(item_id), Some(target)) = (item_id, target) else {
        return Err(crate::problems::internal_error());
    };
    let user_id = principal.require_user()?;
    let pool = pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let conn: &mut PgConnection = tx;
            let ledger = load_ledger(conn, user_id).await?;
            let item = i64::try_from(item_id).ok();
            let Some(entry) = ledger
                .entries
                .iter()
                .find(|e| Some(e.item_id) == item)
                .cloned()
            else {
                return Err(not_found("reconciliation item", &item_id.to_string()));
            };
            owned_portfolio(conn, target, user_id).await?;
            let target = params::bigint(target)?;
            let today = today_ist();
            if !entry.freezes() {
                return Err(refused(format!(
                    "reconciliation item {} is already {}; an answered question is not \
                     re-answerable, because the allocation and the valuations its answer \
                     produced have been written — record a new item instead",
                    entry.item_id,
                    state_name(entry.state)
                )));
            }
            let Some(portfolio) = ledger.portfolios.get(&target).cloned() else {
                return Err(refused(format!(
                    "cannot resolve reconciliation item {} to portfolio {target}, which does not \
                     exist",
                    entry.item_id
                )));
            };
            if portfolio.kind == PortfolioKind::Monitoring {
                return Err(refused(format!(
                    "{} is a monitoring view, and a monitoring view holds no allocation: it \
                     overlaps other portfolios by design and is excluded from every total (spec \
                     section 4.1). Resolving to it would leave the holding in no capital \
                     portfolio and the consolidated total short",
                    baskfy_providers::py::repr_str(&portfolio.name)
                )));
            }
            let quantity = entry.item.quantity.clone();
            if quantity <= zero() {
                return Err(refused(format!(
                    "an allocation must be a positive quantity; got {}. Remove the row instead \
                     of writing a zero one",
                    quantity.to_sci_string()
                )));
            }
            sqlx::query(
                "UPDATE reconciliation_item SET state = 'RESOLVED', resolved_portfolio_id = $1, \
                 resolved_at = $2 WHERE id = $3 AND user_id = $4",
            )
            .bind(target)
            .bind(crate::entitlements::utc_now())
            .bind(entry.item_id)
            .bind(user_id)
            .execute(&mut *conn)
            .await
            .map_err(db::fail)?;
            apply_allocation(conn, &ledger, entry.key(), target, &quantity, today).await?;
            let refreshed = load_ledger(conn, user_id).await?;
            let fresh = refreshed
                .entries
                .iter()
                .find(|e| e.item_id == entry.item_id)
                .ok_or_else(|| fail("StopIteration"))?;
            Ok(ResolveOut {
                item: item_out(&refreshed, fresh),
                allocated_to: portfolio_ref(&portfolio),
                unfroze: true,
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
    fn requests_add_up_and_all_wins() {
        // Ports test_portfolio_overview.py's `_requested_quantities` assertions.
        let key = HoldingKey {
            instrument_id: 1,
            broker_account_id: 2,
        };
        let line = |q: Option<&str>| HoldingKeyIn {
            key,
            quantity: q.map(Dec::lit),
        };
        let summed = requested_quantities(&[line(Some("20")), line(Some("14"))]);
        assert_eq!(
            summed[0].1.as_ref().map(Dec::to_sci_string),
            Some("34".to_owned())
        );
        let all = requested_quantities(&[line(Some("20")), line(None), line(Some("5"))]);
        assert!(all[0].1.is_none());
        assert_eq!(g(&Dec::lit("20.0000")), "20.0000");
        assert_eq!(g(&Dec::lit("1E+2")), "1e+2");
    }
}
