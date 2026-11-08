//! The drift scan and fix handlers, and `baskfy_core.curated_drift`'s `detect_drift` /
//! `rebase_holdings_after_drift` (via `fix_drift`), ported for the two routes.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use axum::extract::{Extension, Path, Request, State};
use axum::response::Response;
use baskfy_core::decimal::WideDecimal;
use baskfy_db::{OrderedJson, row};
use serde::Serialize;
use sqlx::Row;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, internal_error};
use crate::routes::common::py_space;
use crate::routes::screens::accounts::investments::{json_equals_int, load_investment_for_user};
use crate::routes::screens::body_model::{self, Errors, Model, Ty, opt, req};
use crate::routes::screens::write_kit::{self, db, numeric_text};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;
use crate::wire;

/// `DRIFT_ACTION_TYPE`.
const DRIFT: &str = "DRIFT";

/// `DriftScanBody` (a plain `BaseModel`).
fn scan_body() -> Model {
    Model {
        fields: vec![opt(
            "broker_holdings",
            Ty::Opt(Box::new(Ty::List(
                Box::new(Ty::Model(Model {
                    fields: vec![
                        req("symbol", Ty::Str(Some(1), Some(32))),
                        req(
                            "qty",
                            Ty::Dec {
                                gt: None,
                                ge: Some("0"),
                                le: None,
                            },
                        ),
                    ],
                    forbid_extra: false,
                })),
                None,
            ))),
        )],
        forbid_extra: false,
    }
}

/// A `ValueError`/`KeyError` raised inside the pure rules: unhandled in the route, the 500.
fn unhandled(message: &str) -> Problem {
    crate::logging::error("baskfy_api.app", "unhandled error", &[("error", message)]);
    internal_error()
}

fn zero() -> WideDecimal {
    WideDecimal::zero()
}

/// `DriftDelta`.
struct Delta {
    instrument_id: i64,
    ledger_qty: WideDecimal,
    broker_qty: WideDecimal,
    shortfall: WideDecimal,
    excess: WideDecimal,
}

/// `detect_drift(ledger, broker)`: the deltas (sorted by instrument), and whether any is short.
fn detect_drift(
    ledger: &[(i64, WideDecimal, WideDecimal)],
    broker: &[(i64, WideDecimal)],
) -> Result<(Vec<Delta>, bool), Problem> {
    let mut ledger_map: BTreeMap<i64, WideDecimal> = BTreeMap::new();
    for (id, qty, avg) in ledger {
        if qty.is_sign_negative() && !qty.is_zero() || avg.is_sign_negative() && !avg.is_zero() {
            return Err(unhandled("ledger qty/avg_price cannot be negative"));
        }
        if ledger_map.insert(*id, qty.clone()).is_some() {
            return Err(unhandled(&format!("duplicate ledger instrument_id {id}")));
        }
    }
    let mut broker_map: BTreeMap<i64, WideDecimal> = BTreeMap::new();
    for (id, qty) in broker {
        if qty.is_sign_negative() && !qty.is_zero() {
            return Err(unhandled("broker qty cannot be negative"));
        }
        if broker_map.insert(*id, qty.clone()).is_some() {
            return Err(unhandled(&format!("duplicate broker instrument_id {id}")));
        }
    }
    let ids: BTreeSet<i64> = ledger_map
        .keys()
        .chain(broker_map.keys())
        .copied()
        .collect();
    let mut deltas = Vec::new();
    let mut has_shortfall = false;
    for id in ids {
        let ledger_qty = ledger_map.get(&id).cloned().unwrap_or_else(zero);
        let broker_qty = broker_map.get(&id).cloned().unwrap_or_else(zero);
        let mut shortfall = ledger_qty.sub(&broker_qty);
        let mut excess = broker_qty.sub(&ledger_qty);
        if shortfall < zero() {
            shortfall = zero();
        }
        if excess < zero() {
            excess = zero();
        }
        if shortfall > zero() {
            has_shortfall = true;
        }
        if shortfall > zero() || excess > zero() {
            deltas.push(Delta {
                instrument_id: id,
                ledger_qty,
                broker_qty,
                shortfall,
                excess,
            });
        }
    }
    Ok((deltas, has_shortfall))
}

/// `rebase_holdings_after_drift(intended, broker, avg_prices)` reduced to what the route reads:
/// the new intended quantities (instruments the broker holds a non-zero quantity of, sorted) and
/// the number of synthetic exits (instruments short at the broker).
fn rebase(
    intended: &BTreeMap<i64, WideDecimal>,
    broker: &BTreeMap<i64, WideDecimal>,
) -> Result<(Vec<(i64, WideDecimal)>, usize), Problem> {
    // `fix_drift` re-runs `detect_drift` over the rebuilt rows; its refusals are reachable only
    // through negative quantities, which the body and the ledger columns refuse already.
    let ledger: Vec<(i64, WideDecimal, WideDecimal)> = intended
        .iter()
        .map(|(id, q)| (*id, q.clone(), zero()))
        .chain(
            broker
                .keys()
                .filter(|id| !intended.contains_key(id))
                .map(|id| (*id, zero(), zero())),
        )
        .collect();
    let broker_rows: Vec<(i64, WideDecimal)> =
        broker.iter().map(|(k, v)| (*k, v.clone())).collect();
    detect_drift(&ledger, &broker_rows)?;
    let ids: BTreeSet<i64> = intended.keys().chain(broker.keys()).copied().collect();
    let mut holdings = Vec::new();
    let mut exits = 0;
    for id in ids {
        let broker_qty = broker.get(&id).cloned().unwrap_or_else(zero);
        let ledger_qty = intended.get(&id).cloned().unwrap_or_else(zero);
        if ledger_qty.sub(&broker_qty) > zero() {
            exits += 1;
        }
        if broker_qty.is_zero() {
            continue;
        }
        holdings.push((id, broker_qty));
    }
    Ok((holdings, exits))
}

/// `_resolve_broker(session, body)`: the declared lots resolved to active instruments (exact,
/// upper-cased symbol), in posted order, plus their symbols.
async fn resolve_broker(
    tx: &mut Tx,
    lots: &[(String, WideDecimal)],
) -> Result<(Vec<(i64, WideDecimal)>, HashMap<i64, String>), Problem> {
    if lots.is_empty() {
        return Err(Problem::new(
            ProblemType::InvalidScreenDefinition,
            "declare broker_holdings or sync a desk holdings snapshot first",
        ));
    }
    let symbols: Vec<String> = lots
        .iter()
        .map(|(s, _)| s.trim_matches(py_space).to_uppercase())
        .collect();
    let rows = sqlx::query(
        "SELECT id, symbol FROM instrument WHERE symbol = ANY($1) AND is_active IS true",
    )
    .bind(&symbols)
    .fetch_all(&mut **tx)
    .await
    .map_err(db)?;
    let mut by_symbol: HashMap<String, (i64, String)> = HashMap::new();
    for r in &rows {
        let s: String = r.try_get("symbol").map_err(db)?;
        by_symbol.insert(s.to_uppercase(), (r.try_get("id").map_err(db)?, s));
    }
    let missing: Vec<&str> = symbols
        .iter()
        .filter(|s| !by_symbol.contains_key(*s))
        .map(String::as_str)
        .collect();
    if !missing.is_empty() {
        return Err(Problem::new(
            ProblemType::NotFound,
            format!("unknown symbols: {}", missing.join(", ")),
        ));
    }
    let mut holdings = Vec::with_capacity(lots.len());
    let mut id_to_symbol = HashMap::new();
    for ((_, qty), symbol) in lots.iter().zip(&symbols) {
        let (id, stored) = by_symbol.get(symbol).cloned().ok_or_else(internal_error)?;
        holdings.push((id, qty.clone()));
        id_to_symbol.insert(id, stored);
    }
    Ok((holdings, id_to_symbol))
}

/// One ledger row: `(holding id, instrument_id, qty, avg_price)`.
type LedgerRow = (i64, i64, WideDecimal, WideDecimal);

/// `_ledger(session, investment_id)`: the holdings (in the table's order) and their symbols.
async fn ledger(
    tx: &mut Tx,
    investment_id: i64,
) -> Result<(Vec<LedgerRow>, HashMap<i64, String>), Problem> {
    let rows = sqlx::query(
        "SELECT id, instrument_id, qty, avg_price FROM cb_investment_holding \
         WHERE investment_id = $1",
    )
    .bind(investment_id)
    .fetch_all(&mut **tx)
    .await
    .map_err(db)?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let dec = |c: &str| {
            row::decimal(r, c)
                .map(WideDecimal::from)
                .map_err(|e| crate::db_failure(&e))
        };
        out.push((
            r.try_get("id").map_err(db)?,
            r.try_get("instrument_id").map_err(db)?,
            dec("qty")?,
            dec("avg_price")?,
        ));
    }
    let mut symbols = HashMap::new();
    if !out.is_empty() {
        let ids: Vec<i64> = out.iter().map(|r| r.1).collect();
        for r in sqlx::query("SELECT id, symbol FROM instrument WHERE id = ANY($1)")
            .bind(&ids)
            .fetch_all(&mut **tx)
            .await
            .map_err(db)?
        {
            symbols.insert(
                r.try_get::<i64, _>("id").map_err(db)?,
                r.try_get::<String, _>("symbol").map_err(db)?,
            );
        }
    }
    Ok((out, symbols))
}

/// The declared broker lots: `(symbol, qty)` in posted order.
type Lots = Vec<(String, WideDecimal)>;

/// The validated path id and the declared lots; the 400 for either.
fn inputs(raw_id: &str, received: &crate::body::Received) -> Result<(Option<i64>, Lots), Problem> {
    let mut e = Errors::new();
    let id = body_model::path_int(&mut e, "investment_id", raw_id);
    let body = body_model::body(&mut e, received, &scan_body(), true);
    e.finish()?;
    let (id, body) = (
        id.ok_or_else(internal_error)?,
        body.ok_or_else(internal_error)?,
    );
    let lots = body
        .get("broker_holdings")
        .items()
        .iter()
        .map(|h| {
            (
                h.get("symbol").str().unwrap_or_default().to_owned(),
                h.get("qty").dec().cloned().unwrap_or_default(),
            )
        })
        .collect();
    Ok((i64::try_from(id).ok(), lots))
}

fn symbol_for(symbols: &HashMap<i64, String>, id: i64) -> String {
    symbols.get(&id).cloned().unwrap_or_else(|| id.to_string())
}

/// `DriftDeltaOut`.
#[derive(Debug, Serialize)]
struct DeltaOut {
    symbol: String,
    ledger_qty: String,
    broker_qty: String,
    shortfall: String,
    excess: String,
}

/// `DriftScanOut` (field order: `action_type`, `deltas`, `pending_action_id`).
#[derive(Debug, Serialize)]
struct ScanOut {
    action_type: Option<&'static str>,
    deltas: Vec<DeltaOut>,
    pending_action_id: Option<i64>,
}

/// The open `DRIFT` actions of a user: `(id, payload)`.
async fn open_drift_actions(tx: &mut Tx, user_id: i64) -> Result<Vec<(i64, OrderedJson)>, Problem> {
    let rows = sqlx::query(
        "SELECT id, payload FROM cb_pending_action WHERE user_id = $1 AND type = 'DRIFT' \
         AND dismissed_at IS NULL AND resolved_at IS NULL",
    )
    .bind(user_id)
    .fetch_all(&mut **tx)
    .await
    .map_err(db)?;
    rows.iter()
        .map(|r| {
            Ok((
                r.try_get("id").map_err(db)?,
                row::jsonb(r, "payload").map_err(|e| crate::db_failure(&e))?,
            ))
        })
        .collect()
}

/// `payload.get("investment_id")` when the payload is an object.
fn payload_investment(payload: &OrderedJson) -> Option<&OrderedJson> {
    match payload {
        OrderedJson::Object(entries) => entries
            .iter()
            .find(|(k, _)| k == "investment_id")
            .map(|(_, v)| v),
        _ => None,
    }
}

/// `POST /cb/investments/{investment_id}/drift/scan` (`scan_investment_drift`).
///
/// # Errors
/// 400 (path, body, nothing declared), 401, the sole-tenant refusals, 404 (investment, unknown
/// symbols), 500 (a symbol declared twice: `detect_drift`'s unhandled `ValueError`).
pub async fn scan_investment_drift(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let (id, lots) = inputs(&raw_id, &received)?;
    let inv = load_investment_for_user(&state, principal.user_id, id).await?;
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let (broker, broker_symbols) = resolve_broker(tx, &lots).await?;
            let (rows, mut symbols) = ledger(tx, inv.id).await?;
            symbols.extend(broker_symbols);
            let ledger_rows: Vec<(i64, WideDecimal, WideDecimal)> = rows
                .iter()
                .map(|r| (r.1, r.2.clone(), r.3.clone()))
                .collect();
            let (deltas, has_shortfall) = detect_drift(&ledger_rows, &broker)?;
            let mut pending_id = None;
            if has_shortfall {
                let open = open_drift_actions(tx, inv.user_id).await?;
                let existing = open
                    .iter()
                    .find(|(_, p)| json_equals_int(payload_investment(p), inv.id))
                    .map(|(id, _)| *id);
                let payload = OrderedJson::Object(vec![
                    ("investment_id".to_owned(), OrderedJson::Int(inv.id)),
                    (
                        "deltas".to_owned(),
                        OrderedJson::Array(
                            deltas
                                .iter()
                                .map(|d| {
                                    OrderedJson::Object(vec![
                                        (
                                            "instrument_id".to_owned(),
                                            OrderedJson::Int(d.instrument_id),
                                        ),
                                        (
                                            "shortfall".to_owned(),
                                            OrderedJson::Str(d.shortfall.to_sci_string()),
                                        ),
                                        (
                                            "excess".to_owned(),
                                            OrderedJson::Str(d.excess.to_sci_string()),
                                        ),
                                    ])
                                })
                                .collect(),
                        ),
                    ),
                ]);
                let text = payload.to_python_json().map_err(|_| internal_error())?;
                pending_id = Some(match existing {
                    None => sqlx::query_scalar(
                        "INSERT INTO cb_pending_action (user_id, type, payload) \
                         VALUES ($1, 'DRIFT', ($2::text)::jsonb) RETURNING id",
                    )
                    .bind(inv.user_id)
                    .bind(&text)
                    .fetch_one(&mut **tx)
                    .await
                    .map_err(db)?,
                    Some(id) => {
                        sqlx::query(
                            "UPDATE cb_pending_action SET payload = ($1::text)::jsonb \
                             WHERE id = $2 AND payload IS DISTINCT FROM ($1::text)::jsonb",
                        )
                        .bind(&text)
                        .bind(id)
                        .execute(&mut **tx)
                        .await
                        .map_err(db)?;
                        id
                    }
                });
            }
            Ok(ScanOut {
                action_type: has_shortfall.then_some(DRIFT),
                deltas: deltas
                    .iter()
                    .map(|d| DeltaOut {
                        symbol: symbol_for(&symbols, d.instrument_id),
                        ledger_qty: d.ledger_qty.to_sci_string(),
                        broker_qty: d.broker_qty.to_sci_string(),
                        shortfall: d.shortfall.to_sci_string(),
                        excess: d.excess.to_sci_string(),
                    })
                    .collect(),
                pending_action_id: pending_id,
            })
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `BrokerHoldingIn` as `DriftFixOut.holdings` renders it.
#[derive(Debug, Serialize)]
struct HoldingOut {
    symbol: String,
    qty: String,
}

/// `DriftFixOut`.
#[derive(Debug, Serialize)]
struct FixOut {
    holdings: Vec<HoldingOut>,
    cleared_action: bool,
    synthetic_exit_count: usize,
}

/// `POST /cb/investments/{investment_id}/drift/fix` (`fix_investment_drift`).
///
/// # Errors
/// 400 (path, body, nothing declared), 401, the sole-tenant refusals, 404 (investment, unknown
/// symbols), 500.
pub async fn fix_investment_drift(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let (id, lots) = inputs(&raw_id, &received)?;
    let inv = load_investment_for_user(&state, principal.user_id, id).await?;
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let (broker, broker_symbols) = resolve_broker(tx, &lots).await?;
            let (rows, mut symbols) = ledger(tx, inv.id).await?;
            symbols.extend(broker_symbols);
            // `{row.instrument_id: row.qty for row in ledger}` and the broker map: a later row
            // wins a repeated instrument.
            let mut intended: BTreeMap<i64, WideDecimal> = BTreeMap::new();
            let mut avg: HashMap<i64, WideDecimal> = HashMap::new();
            for r in &rows {
                intended.insert(r.1, r.2.clone());
                avg.insert(r.1, r.3.clone());
            }
            let broker_map: BTreeMap<i64, WideDecimal> = broker.into_iter().collect();
            let (new_intended, exits) = rebase(&intended, &broker_map)?;
            let by_instrument: HashMap<i64, (i64, WideDecimal)> =
                rows.iter().map(|r| (r.1, (r.0, r.2.clone()))).collect();
            // The ORM's flush order for one table (`persistence.save_obj`): the UPDATEs of
            // persistent rows (in primary-key order), then the INSERTs of new rows as multi-row
            // statements (insertmanyvalues, 1,000 a page), then the DELETEs. A quantity a column
            // cannot hold therefore fails before any new row draws an identity value.
            let mut updates: Vec<(i64, String)> = Vec::new();
            let mut inserts: Vec<(i64, String, String)> = Vec::new();
            for (instrument_id, qty) in &new_intended {
                match by_instrument.get(instrument_id) {
                    None => {
                        let price = avg.get(instrument_id).cloned().unwrap_or_else(zero);
                        inserts.push((*instrument_id, numeric_text(qty), numeric_text(&price)));
                    }
                    // The ORM writes only a net change, and `onupdate` stamps `updated_at`.
                    Some((holding_id, current)) if current != qty => {
                        updates.push((*holding_id, numeric_text(qty)));
                    }
                    Some(_) => {}
                }
            }
            updates.sort_by_key(|(holding_id, _)| *holding_id);
            for (holding_id, qty) in &updates {
                sqlx::query(
                    "UPDATE cb_investment_holding SET qty = ($1::text)::numeric, \
                     updated_at = now() WHERE id = $2",
                )
                .bind(qty)
                .bind(holding_id)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            }
            for page in inserts.chunks(write_kit::INSERT_PAGE_ROWS) {
                let values = write_kit::values_rows(page.len(), 3, |p| {
                    format!(
                        "($1, ${}, (${}::text)::numeric, (${}::text)::numeric)",
                        p[0], p[1], p[2]
                    )
                });
                let sql = format!(
                    "INSERT INTO cb_investment_holding (investment_id, instrument_id, qty, \
                     avg_price) VALUES {values}"
                );
                let mut q = sqlx::query(&sql).bind(inv.id);
                for (instrument_id, qty, price) in page {
                    q = q.bind(*instrument_id).bind(qty).bind(price);
                }
                q.execute(&mut **tx).await.map_err(db)?;
            }
            let kept: BTreeSet<i64> = new_intended.iter().map(|(id, _)| *id).collect();
            let doomed: Vec<i64> = rows
                .iter()
                .filter(|r| !kept.contains(&r.1))
                .map(|r| r.0)
                .collect();
            for holding_id in doomed {
                sqlx::query("DELETE FROM cb_investment_holding WHERE id = $1")
                    .bind(holding_id)
                    .execute(&mut **tx)
                    .await
                    .map_err(db)?;
            }
            let now = write_kit::now_utc();
            let mut cleared = false;
            for (action_id, payload) in open_drift_actions(tx, inv.user_id).await? {
                let target = payload_investment(&payload);
                if matches!(target, None | Some(OrderedJson::Null))
                    || json_equals_int(target, inv.id)
                {
                    sqlx::query("UPDATE cb_pending_action SET resolved_at = $1 WHERE id = $2")
                        .bind(now)
                        .bind(action_id)
                        .execute(&mut **tx)
                        .await
                        .map_err(db)?;
                    cleared = true;
                }
            }
            Ok(FixOut {
                holdings: new_intended
                    .iter()
                    .map(|(id, qty)| HoldingOut {
                        symbol: symbol_for(&symbols, *id),
                        qty: qty.to_sci_string(),
                    })
                    .collect(),
                cleared_action: cleared,
                synthetic_exit_count: exits,
            })
        })
    })
    .await?;
    Ok(wire::json(&out))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(s: &str) -> WideDecimal {
        s.parse().unwrap()
    }

    // `packages/core/tests/test_curated_drift.py`: a shortfall is DRIFT; excess alone is not;
    // the rebase adopts the broker's quantities and archives each shortfall as one exit.
    #[test]
    fn shortfalls_raise_drift_and_the_rebase_adopts_the_broker() {
        let ledger = vec![(1, d("10.0000"), d("100.00")), (2, d("5.0000"), d("50.00"))];
        let broker = vec![(1, d("8")), (3, d("2"))];
        let (deltas, short) = detect_drift(&ledger, &broker).unwrap();
        assert!(short);
        let printed: Vec<(i64, String, String)> = deltas
            .iter()
            .map(|x| {
                (
                    x.instrument_id,
                    x.shortfall.to_sci_string(),
                    x.excess.to_sci_string(),
                )
            })
            .collect();
        assert_eq!(
            printed,
            vec![
                (1, "2.0000".to_owned(), "0".to_owned()),
                (2, "5.0000".to_owned(), "0".to_owned()),
                (3, "0".to_owned(), "2".to_owned()),
            ]
        );
        let intended: BTreeMap<i64, WideDecimal> = [(1, d("10.0000")), (2, d("5.0000"))].into();
        let broker_map: BTreeMap<i64, WideDecimal> = [(1, d("8")), (3, d("2"))].into();
        let (holdings, exits) = rebase(&intended, &broker_map).unwrap();
        assert_eq!(exits, 2);
        let h: Vec<(i64, String)> = holdings
            .iter()
            .map(|(i, q)| (*i, q.to_sci_string()))
            .collect();
        assert_eq!(h, vec![(1, "8".to_owned()), (3, "2".to_owned())]);
        assert!(detect_drift(&[], &[(1, d("1")), (1, d("2"))]).is_err());
        let (_, short) = detect_drift(&[(1, d("1"), d("1"))], &[(1, d("3"))]).unwrap();
        assert!(!short);
    }
}
