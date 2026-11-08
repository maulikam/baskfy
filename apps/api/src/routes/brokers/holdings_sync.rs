//! `baskfy_api.broker_holdings_sync` (M75): a live broker read written into the broker's own
//! holding group, the desk's swing positions filed first, and filed rows the broker no longer
//! backs reconciled. Database only; the Kite read happened before this module (law 2).

use std::collections::{BTreeMap, BTreeSet, HashMap};

use baskfy_core::decimal::WideDecimal;
use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::problems::Problem;
use crate::routes::portfolios::portfolio::db;
use crate::routes::portfolios::portfolio::symbols::{
    SymbolResolution, numeric_text, replace_holdings, resolve_symbols,
};

/// `SWING_PORTFOLIO_NAME`.
const SWING_PORTFOLIO_NAME: &str = "Swing";

/// One live row, as the sync reads it.
#[derive(Debug, Clone)]
pub(crate) struct SyncRow {
    pub(crate) symbol: String,
    pub(crate) total_quantity: WideDecimal,
    pub(crate) average_price: WideDecimal,
}

/// `BrokerSyncResult`.
#[derive(Debug, Clone)]
pub(crate) struct BrokerSyncResult {
    pub(crate) persisted: bool,
    pub(crate) written: i64,
    pub(crate) unresolved: Vec<String>,
    pub(crate) portfolio_id: Option<i64>,
    pub(crate) reason: String,
}

/// `DisappearanceReport`.
struct DisappearanceReport {
    reconciled: i64,
    questions: i64,
    unjudged: Vec<String>,
}

/// `is_persistable(result)`.
pub(crate) fn is_persistable(source: &str, degraded: bool) -> bool {
    source == "live" && !degraded
}

/// `portfolio_for_broker_account(...)`: `(id, name)` of the broker's pile, created once.
async fn portfolio_for_broker_account(
    conn: &mut PgConnection,
    user_id: i64,
    broker_account_id: i64,
    broker_name: &str,
    as_of: NaiveDate,
) -> Result<(i64, String), Problem> {
    let existing: Vec<(i64, String)> = sqlx::query_as(
        "SELECT id, name FROM portfolio WHERE user_id = $1 AND broker_account_id = $2 \
         AND is_broker_pile IS true",
    )
    .bind(user_id)
    .bind(broker_account_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    match existing.len() {
        0 => {}
        1 => return Ok(existing[0].clone()),
        _ => {
            return Err(crate::routes::portfolios::portfolio::fail(
                "MultipleResultsFound",
            ));
        }
    }
    let name = format!("{broker_name} holdings");
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO portfolio (user_id, broker_account_id, name, kind, source, is_broker_pile, \
         started_on) VALUES ($1, $2, $3, 'CAPITAL', 'HOLDING_GROUP', true, $4) RETURNING id",
    )
    .bind(user_id)
    .bind(broker_account_id)
    .bind(&name)
    .bind(as_of)
    .fetch_one(&mut *conn)
    .await
    .map_err(db::fail)?;
    Ok((id, name))
}

/// `sync_holdings_into_portfolio(session, result, ...)`.
///
/// # Errors
/// The database's.
pub(crate) async fn sync_holdings_into_portfolio(
    conn: &mut PgConnection,
    rows: &[SyncRow],
    user_id: i64,
    broker_account_id: i64,
    broker_name: &str,
    as_of: NaiveDate,
) -> Result<BrokerSyncResult, Problem> {
    let symbols: Vec<String> = rows.iter().map(|r| r.symbol.clone()).collect();
    let resolutions = resolve_symbols(conn, &symbols).await?;
    let mut wanted: Vec<(i64, Option<WideDecimal>, Option<WideDecimal>)> = Vec::new();
    let mut unresolved = Vec::new();
    for row in rows {
        match resolutions
            .get(&row.symbol)
            .and_then(SymbolResolution::instrument_id)
        {
            None => unresolved.push(row.symbol.clone()),
            Some(id) => wanted.push((
                id,
                Some(row.total_quantity.clone()),
                Some(row.average_price.clone()),
            )),
        }
    }
    let (pile_id, pile_name) =
        portfolio_for_broker_account(conn, user_id, broker_account_id, broker_name, as_of).await?;
    let held: HashMap<i64, WideDecimal> = wanted
        .iter()
        .filter_map(|(id, q, _)| q.clone().map(|q| (*id, q)))
        .collect();
    if let Err(e) = file_swing_positions(conn, user_id, broker_account_id, &held, as_of).await {
        crate::logging::error(
            "baskfy_api.broker_holdings_sync",
            &format!("could not file swing positions for broker account {broker_account_id}"),
            &[("error", &e.detail)],
        );
    }
    let unjudgeable = might_have_meant(&symbols, &resolutions);
    let disappeared = reconcile_missing_positions(
        conn,
        rows,
        &held,
        &unjudgeable,
        user_id,
        broker_account_id,
        pile_id,
        as_of,
    )
    .await?;
    let wanted =
        minus_what_is_filed_elsewhere(conn, wanted, user_id, broker_account_id, pile_id).await?;
    let written = replace_holdings(
        conn,
        pile_id,
        user_id,
        Some(broker_account_id),
        &wanted,
        as_of,
    )
    .await?;
    sqlx::query("UPDATE broker_account SET holdings_synced_at = now() WHERE id = $1")
        .bind(broker_account_id)
        .execute(&mut *conn)
        .await
        .map_err(db::fail)?;
    let mut reason = format!("{written} holding(s) written to {pile_name} from a live read");
    if disappeared.reconciled != 0 {
        reason.push_str(&format!(
            "; {} filed holding(s) the broker no longer reports closed",
            disappeared.reconciled
        ));
    }
    if disappeared.questions != 0 {
        reason.push_str(&format!(
            "; {} left for the reconciliation inbox to answer",
            disappeared.questions
        ));
    }
    if !disappeared.unjudged.is_empty() {
        reason.push_str(&format!(
            "; not judged (unresolved symbol): {}",
            disappeared.unjudged.join(", ")
        ));
    }
    Ok(BrokerSyncResult {
        persisted: true,
        written: i64::try_from(written).unwrap_or(i64::MAX),
        unresolved,
        portfolio_id: Some(pile_id),
        reason,
    })
}

/// `_instruments_a_symbol_might_have_meant(resolutions)`, in the resolutions' insertion order.
fn might_have_meant(
    symbols: &[String],
    resolutions: &HashMap<String, SymbolResolution>,
) -> HashMap<i64, String> {
    let mut seen = BTreeSet::new();
    let mut out = HashMap::new();
    for symbol in symbols {
        if !seen.insert(symbol.clone()) {
            continue;
        }
        let Some(r) = resolutions.get(symbol) else {
            continue;
        };
        if r.instrument_id().is_none() {
            for c in &r.candidates {
                out.insert(c.instrument_id, symbol.clone());
            }
        }
    }
    out
}

#[allow(clippy::too_many_arguments)]
async fn reconcile_missing_positions(
    conn: &mut PgConnection,
    rows: &[SyncRow],
    broker_quantities: &HashMap<i64, WideDecimal>,
    unjudgeable: &HashMap<i64, String>,
    user_id: i64,
    broker_account_id: i64,
    pile_portfolio_id: i64,
    as_of: NaiveDate,
) -> Result<DisappearanceReport, Problem> {
    let none = || DisappearanceReport {
        reconciled: 0,
        questions: 0,
        unjudged: Vec::new(),
    };
    if rows.is_empty() {
        return Ok(none());
    }
    let reported: BTreeSet<String> = rows
        .iter()
        .map(|r| r.symbol.trim().to_uppercase())
        .collect();
    let filed = sqlx::query(
        "SELECT ph.instrument_id, ph.portfolio_id, ph.quantity, ph.avg_price, i.symbol \
         FROM portfolio_holding ph JOIN portfolio p ON p.id = ph.portfolio_id \
         JOIN instrument i ON i.id = ph.instrument_id \
         WHERE p.user_id = $1 AND ph.broker_account_id = $2 AND ph.portfolio_kind = 'CAPITAL' \
         AND ph.portfolio_id != $3 AND ph.quantity IS NOT NULL AND ph.quantity > 0",
    )
    .bind(user_id)
    .bind(broker_account_id)
    .bind(pile_portfolio_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    if filed.is_empty() {
        return Ok(none());
    }
    let mut slices: BTreeMap<i64, BTreeMap<i64, WideDecimal>> = BTreeMap::new();
    let mut symbols: HashMap<i64, String> = HashMap::new();
    for row in &filed {
        let instrument_id: i64 = db::get(row, "instrument_id")?;
        symbols.insert(instrument_id, db::get(row, "symbol")?);
        slices
            .entry(instrument_id)
            .or_default()
            .insert(db::get(row, "portfolio_id")?, db::dec(row, "quantity")?);
    }
    let mut report = none();
    for (instrument_id, held_by) in &slices {
        let recorded = WideDecimal::sum(held_by.values());
        let at_broker = match broker_quantities.get(instrument_id) {
            Some(q) => q.clone(),
            None => {
                if let Some(maybe) = unjudgeable.get(instrument_id) {
                    report.unjudged.push(maybe.clone());
                    continue;
                }
                let symbol = symbols.get(instrument_id).cloned().unwrap_or_default();
                if reported.contains(&symbol.trim().to_uppercase()) {
                    report.unjudged.push(symbol);
                    continue;
                }
                WideDecimal::zero()
            }
        };
        let missing = recorded.sub(&at_broker);
        if missing <= WideDecimal::zero() {
            continue;
        }
        let sole = if held_by.len() == 1 {
            held_by.keys().next().copied()
        } else {
            None
        };
        if missing > recorded {
            ask_once(
                conn,
                "QUANTITY_MISMATCH",
                &missing,
                sole,
                user_id,
                *instrument_id,
                broker_account_id,
                as_of,
            )
            .await?;
            report.questions += 1;
            continue;
        }
        if let Some(owner) = sole {
            let remaining = held_by
                .get(&owner)
                .cloned()
                .unwrap_or_default()
                .sub(&missing);
            close_slice(conn, owner, *instrument_id, broker_account_id, &remaining).await?;
            report.reconciled += 1;
            continue;
        }
        ask_once(
            conn,
            "SPLIT_HOLDING",
            &missing,
            None,
            user_id,
            *instrument_id,
            broker_account_id,
            as_of,
        )
        .await?;
        report.questions += 1;
    }
    Ok(report)
}

async fn close_slice(
    conn: &mut PgConnection,
    portfolio_id: i64,
    instrument_id: i64,
    broker_account_id: i64,
    remaining: &WideDecimal,
) -> Result<(), Problem> {
    if *remaining > WideDecimal::zero() {
        sqlx::query(
            "UPDATE portfolio_holding SET quantity = $1::numeric WHERE portfolio_id = $2 \
             AND instrument_id = $3 AND broker_account_id = $4",
        )
        .bind(numeric_text(Some(remaining)))
        .bind(portfolio_id)
        .bind(instrument_id)
        .bind(broker_account_id)
        .execute(&mut *conn)
        .await
        .map_err(db::fail)?;
        return Ok(());
    }
    sqlx::query(
        "DELETE FROM portfolio_holding WHERE portfolio_id = $1 AND instrument_id = $2 \
         AND broker_account_id = $3",
    )
    .bind(portfolio_id)
    .bind(instrument_id)
    .bind(broker_account_id)
    .execute(&mut *conn)
    .await
    .map_err(db::fail)?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn ask_once(
    conn: &mut PgConnection,
    reason: &str,
    quantity: &WideDecimal,
    suggested_portfolio_id: Option<i64>,
    user_id: i64,
    instrument_id: i64,
    broker_account_id: i64,
    as_of: NaiveDate,
) -> Result<(), Problem> {
    let existing: Option<i64> = sqlx::query_scalar(
        "SELECT id FROM reconciliation_item WHERE user_id = $1 AND instrument_id = $2 \
         AND broker_account_id = $3 AND reason = $4 AND state = 'OPEN' LIMIT 1",
    )
    .bind(user_id)
    .bind(instrument_id)
    .bind(broker_account_id)
    .bind(reason)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db::fail)?;
    if let Some(id) = existing {
        sqlx::query(
            "UPDATE reconciliation_item SET quantity = $1::numeric, suggested_portfolio_id = $2 \
             WHERE id = $3",
        )
        .bind(numeric_text(Some(quantity)))
        .bind(suggested_portfolio_id)
        .bind(id)
        .execute(&mut *conn)
        .await
        .map_err(db::fail)?;
        return Ok(());
    }
    sqlx::query(
        "INSERT INTO reconciliation_item (user_id, instrument_id, broker_account_id, quantity, \
         reason, state, suggested_portfolio_id, detected_on) \
         VALUES ($1, $2, $3, $4::numeric, $5, 'OPEN', $6, $7)",
    )
    .bind(user_id)
    .bind(instrument_id)
    .bind(broker_account_id)
    .bind(numeric_text(Some(quantity)))
    .bind(reason)
    .bind(suggested_portfolio_id)
    .bind(as_of)
    .execute(&mut *conn)
    .await
    .map_err(db::fail)?;
    Ok(())
}

/// `SUM(quantity)` per instrument over this account's capital rows, `exclude` left out.
async fn claimed(
    conn: &mut PgConnection,
    user_id: i64,
    broker_account_id: i64,
    exclude: &[i64],
) -> Result<HashMap<i64, WideDecimal>, Problem> {
    let rows = sqlx::query(
        "SELECT ph.instrument_id, sum(ph.quantity) AS total FROM portfolio_holding ph \
         JOIN portfolio p ON p.id = ph.portfolio_id \
         WHERE p.user_id = $1 AND ph.broker_account_id = $2 AND ph.portfolio_kind = 'CAPITAL' \
         AND NOT (ph.portfolio_id = ANY($3)) GROUP BY ph.instrument_id",
    )
    .bind(user_id)
    .bind(broker_account_id)
    .bind(exclude)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut out = HashMap::new();
    for row in &rows {
        if let Some(total) = db::opt_dec(row, "total")? {
            out.insert(db::get(row, "instrument_id")?, total);
        }
    }
    Ok(out)
}

async fn minus_what_is_filed_elsewhere(
    conn: &mut PgConnection,
    wanted: Vec<(i64, Option<WideDecimal>, Option<WideDecimal>)>,
    user_id: i64,
    broker_account_id: i64,
    pile_portfolio_id: i64,
) -> Result<Vec<(i64, Option<WideDecimal>, Option<WideDecimal>)>, Problem> {
    let elsewhere = claimed(conn, user_id, broker_account_id, &[pile_portfolio_id]).await?;
    if elsewhere.is_empty() {
        return Ok(wanted);
    }
    let mut out = Vec::with_capacity(wanted.len());
    for (instrument_id, quantity, avg_price) in wanted {
        match (elsewhere.get(&instrument_id), quantity) {
            (Some(allocated), Some(q)) => {
                let remaining = q.sub(allocated);
                if remaining > WideDecimal::zero() {
                    out.push((instrument_id, Some(remaining), avg_price));
                }
            }
            (_, quantity) => out.push((instrument_id, quantity, avg_price)),
        }
    }
    Ok(out)
}

/// `swing_portfolio(session, user_id=, as_of=)`.
async fn swing_portfolio(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<i64, Problem> {
    let existing: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM portfolio WHERE user_id = $1 AND source = 'MY_STRATEGY' AND name = $2",
    )
    .bind(user_id)
    .bind(SWING_PORTFOLIO_NAME)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    match existing.len() {
        0 => {}
        1 => return Ok(existing[0]),
        _ => {
            return Err(crate::routes::portfolios::portfolio::fail(
                "MultipleResultsFound",
            ));
        }
    }
    sqlx::query_scalar(
        "INSERT INTO portfolio (user_id, name, kind, source, started_on) \
         VALUES ($1, $2, 'CAPITAL', 'MY_STRATEGY', $3) RETURNING id",
    )
    .bind(user_id)
    .bind(SWING_PORTFOLIO_NAME)
    .bind(as_of)
    .fetch_one(&mut *conn)
    .await
    .map_err(db::fail)
}

/// `file_swing_positions(...)`: the rows written.
async fn file_swing_positions(
    conn: &mut PgConnection,
    user_id: i64,
    broker_account_id: i64,
    held: &HashMap<i64, WideDecimal>,
    as_of: NaiveDate,
) -> Result<i64, Problem> {
    let swing_id = swing_portfolio(conn, user_id, as_of).await?;
    let open = sqlx::query(
        "SELECT instrument_id, quantity_open::numeric AS quantity_open FROM sw_position \
         WHERE user_id = $1 AND broker_account_id = $2 AND state IN ('OPEN', 'PARTIAL') \
         AND quantity_open > 0",
    )
    .bind(user_id)
    .bind(broker_account_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut wanted: HashMap<i64, WideDecimal> = HashMap::new();
    for row in &open {
        wanted.insert(
            db::get(row, "instrument_id")?,
            db::dec(row, "quantity_open")?,
        );
    }
    let (pile_id, _) =
        portfolio_for_broker_account(conn, user_id, broker_account_id, "", as_of).await?;
    let claimed = claimed(conn, user_id, broker_account_id, &[swing_id, pile_id]).await?;
    let existing = sqlx::query(
        "SELECT instrument_id, quantity FROM portfolio_holding WHERE portfolio_id = $1 \
         AND broker_account_id = $2",
    )
    .bind(swing_id)
    .bind(broker_account_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut already: HashMap<i64, Option<WideDecimal>> = HashMap::new();
    for row in &existing {
        already.insert(
            db::get(row, "instrument_id")?,
            db::opt_dec(row, "quantity")?,
        );
    }
    let ids: BTreeSet<i64> = wanted.keys().chain(already.keys()).copied().collect();
    let zero = WideDecimal::zero();
    let mut written = 0;
    for instrument_id in ids {
        let free = held
            .get(&instrument_id)
            .unwrap_or(&zero)
            .sub(claimed.get(&instrument_id).unwrap_or(&zero));
        let free = if free > zero { free } else { zero.clone() };
        let want = wanted.get(&instrument_id).cloned().unwrap_or_default();
        let target = if free < want { free } else { want };
        let current = already.get(&instrument_id).cloned().flatten();
        if target <= zero {
            if current.is_some() {
                sqlx::query(
                    "DELETE FROM portfolio_holding WHERE portfolio_id = $1 AND instrument_id = $2 \
                     AND broker_account_id = $3",
                )
                .bind(swing_id)
                .bind(instrument_id)
                .bind(broker_account_id)
                .execute(&mut *conn)
                .await
                .map_err(db::fail)?;
            }
            continue;
        }
        match current {
            None => {
                sqlx::query(
                    "INSERT INTO portfolio_holding (portfolio_id, instrument_id, \
                     broker_account_id, portfolio_kind, quantity, added_on) \
                     VALUES ($1, $2, $3, 'CAPITAL', $4::numeric, $5)",
                )
                .bind(swing_id)
                .bind(instrument_id)
                .bind(broker_account_id)
                .bind(numeric_text(Some(&target)))
                .bind(as_of)
                .execute(&mut *conn)
                .await
                .map_err(db::fail)?;
                written += 1;
            }
            Some(c) if c != target => {
                sqlx::query(
                    "UPDATE portfolio_holding SET quantity = $1::numeric WHERE portfolio_id = $2 \
                     AND instrument_id = $3 AND broker_account_id = $4",
                )
                .bind(numeric_text(Some(&target)))
                .bind(swing_id)
                .bind(instrument_id)
                .bind(broker_account_id)
                .execute(&mut *conn)
                .await
                .map_err(db::fail)?;
                written += 1;
            }
            Some(_) => {}
        }
    }
    Ok(written)
}
