//! Ports `baskfy_api.portfolios` (symbol resolution, `replace_holdings`, `resolve_broker_account`)
//! and `baskfy_api.broker_accounts.ensure_default_broker_account` — the database half of the
//! portfolio imports. Every function runs on the connection it is handed: a write route hands it
//! the request's transaction, so later reads see these writes, as Python's one session does.

use std::collections::{HashMap, HashSet};

use baskfy_core::decimal::WideDecimal;
use chrono::NaiveDate;
use sqlx::PgConnection;

use super::db;
use super::portfolio_csv::{MatchStatus, ParsedHolding, SymbolShape, UnmatchedReason};
use crate::problems::Problem;
use crate::routes::meta::NSE_EXCHANGE_ID;

/// `baskfy_core.tenancy.DEFAULT_BROKER_ID`.
pub(crate) const DEFAULT_BROKER_ID: &str = "zerodha";

/// `GSEC_SERIES_SUFFIXES`.
const GSEC_SERIES_SUFFIXES: [&str; 2] = ["-GB", "-GS"];

/// `Candidate`.
#[derive(Debug, Clone)]
pub(crate) struct Candidate {
    pub(crate) instrument_id: i64,
    pub(crate) symbol: String,
    pub(crate) name: String,
    pub(crate) series: Option<String>,
}

/// `SymbolResolution`.
#[derive(Debug, Clone)]
pub(crate) struct SymbolResolution {
    pub(crate) status: MatchStatus,
    pub(crate) candidates: Vec<Candidate>,
    pub(crate) reason: Option<UnmatchedReason>,
    pub(crate) matched_via_alias: bool,
}

impl SymbolResolution {
    fn unmatched(reason: UnmatchedReason) -> Self {
        Self {
            status: MatchStatus::Unmatched,
            candidates: Vec::new(),
            reason: Some(reason),
            matched_via_alias: false,
        }
    }

    /// `instrument_id`: the candidate's, for a match only.
    pub(crate) fn instrument_id(&self) -> Option<i64> {
        if self.status == MatchStatus::Matched {
            self.candidates.first().map(|c| c.instrument_id)
        } else {
            None
        }
    }
}

fn candidate(row: &sqlx::postgres::PgRow) -> Result<Candidate, Problem> {
    Ok(Candidate {
        instrument_id: db::get(row, "id")?,
        symbol: db::get(row, "symbol")?,
        name: db::get(row, "name")?,
        series: db::get(row, "series")?,
    })
}

async fn by_symbol(
    conn: &mut PgConnection,
    symbols: &[String],
    active_only: bool,
) -> Result<HashMap<String, Vec<Candidate>>, Problem> {
    let rows = sqlx::query(
        "SELECT id, symbol, name, series, delisted_on, is_active FROM instrument \
         WHERE exchange_id = $1 AND symbol = ANY($2) AND ($3 = false OR is_active IS true)",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(symbols)
    .bind(active_only)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut out: HashMap<String, Vec<Candidate>> = HashMap::new();
    for row in &rows {
        let c = candidate(row)?;
        out.entry(c.symbol.clone()).or_default().push(c);
    }
    Ok(out)
}

/// `resolve_symbols(session, symbols)`: four tiers, most specific first.
pub(crate) async fn resolve_symbols(
    conn: &mut PgConnection,
    symbols: &[String],
) -> Result<HashMap<String, SymbolResolution>, Problem> {
    let mut wanted: Vec<String> = Vec::new();
    for s in symbols {
        if !wanted.contains(s) {
            wanted.push(s.clone());
        }
    }
    if wanted.is_empty() {
        return Ok(HashMap::new());
    }
    let direct = by_symbol(conn, &wanted, false).await?;
    let active = by_symbol(conn, &wanted, true).await?;
    let unresolved: Vec<String> = wanted
        .iter()
        .filter(|s| !direct.contains_key(*s))
        .cloned()
        .collect();
    let mut aliased: HashMap<String, Vec<Candidate>> = HashMap::new();
    if !unresolved.is_empty() {
        let rows = sqlx::query(
            "SELECT a.old_symbol, i.id, i.symbol, i.name, i.series, i.delisted_on \
             FROM symbol_alias a JOIN instrument i ON i.id = a.instrument_id \
             WHERE a.old_symbol = ANY($1)",
        )
        .bind(&unresolved)
        .fetch_all(&mut *conn)
        .await
        .map_err(db::fail)?;
        for row in &rows {
            let old: String = db::get(row, "old_symbol")?;
            aliased.entry(old).or_default().push(candidate(row)?);
        }
    }
    let still_unknown: Vec<&String> = unresolved
        .iter()
        .filter(|s| !aliased.contains_key(*s))
        .collect();
    let mut suffixed: HashMap<String, Vec<Candidate>> = HashMap::new();
    if !still_unknown.is_empty() {
        let mut forms: Vec<(String, String)> = Vec::new();
        for s in &still_unknown {
            for suffix in GSEC_SERIES_SUFFIXES {
                forms.push((format!("{s}{suffix}"), (*s).clone()));
            }
        }
        let names: Vec<String> = forms.iter().map(|(f, _)| f.clone()).collect();
        let found = by_symbol(conn, &names, false).await?;
        for (form, bare) in &forms {
            if let Some(cands) = found.get(form) {
                suffixed
                    .entry(bare.clone())
                    .or_default()
                    .extend(cands.iter().cloned());
            }
        }
    }
    let empty = Vec::new();
    Ok(wanted
        .iter()
        .map(|s| {
            let tiers = [
                (active.get(s).unwrap_or(&empty), false),
                (direct.get(s).unwrap_or(&empty), false),
                (aliased.get(s).unwrap_or(&empty), true),
                (suffixed.get(s).unwrap_or(&empty), false),
            ];
            (s.clone(), resolve_one(&tiers))
        })
        .collect())
}

/// `_resolve_one`.
fn resolve_one(tiers: &[(&Vec<Candidate>, bool)]) -> SymbolResolution {
    for (tier, via_alias) in tiers {
        if tier.len() == 1 {
            return SymbolResolution {
                status: MatchStatus::Matched,
                candidates: vec![tier[0].clone()],
                reason: None,
                matched_via_alias: *via_alias,
            };
        }
        if tier.len() > 1 {
            let mut sorted = (*tier).clone();
            sorted.sort_by(|a, b| {
                (a.series.as_deref().unwrap_or(""), a.instrument_id)
                    .cmp(&(b.series.as_deref().unwrap_or(""), b.instrument_id))
            });
            return SymbolResolution {
                status: MatchStatus::Ambiguous,
                candidates: sorted,
                reason: None,
                matched_via_alias: *via_alias,
            };
        }
    }
    SymbolResolution::unmatched(UnmatchedReason::UnknownSymbol)
}

/// `resolution_for(row, resolved)`.
pub(crate) fn resolution_for(
    row: &ParsedHolding,
    resolved: &HashMap<String, SymbolResolution>,
) -> SymbolResolution {
    match row.shape {
        SymbolShape::BseCode => SymbolResolution::unmatched(UnmatchedReason::BseCode),
        SymbolShape::Invalid => SymbolResolution::unmatched(UnmatchedReason::Invalid),
        SymbolShape::Symbol => resolved
            .get(&row.symbol)
            .cloned()
            .unwrap_or_else(|| SymbolResolution::unmatched(UnmatchedReason::UnknownSymbol)),
    }
}

/// `lookup_symbols(rows)`.
pub(crate) fn lookup_symbols(rows: &[ParsedHolding]) -> Vec<String> {
    rows.iter()
        .filter(|r| r.shape == SymbolShape::Symbol)
        .map(|r| r.symbol.clone())
        .collect()
}

/// `ensure_default_broker_account(session, user_id, broker_id=)`.
pub(crate) async fn ensure_default_broker_account(
    conn: &mut PgConnection,
    user_id: i64,
    broker_id: &str,
) -> Result<i64, Problem> {
    let existing = sqlx::query_scalar::<_, i64>(
        "SELECT id FROM broker_account WHERE user_id = $1 AND broker_id = $2",
    )
    .bind(user_id)
    .bind(broker_id)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db::fail)?;
    if let Some(id) = existing {
        return Ok(id);
    }
    sqlx::query_scalar::<_, i64>(
        "INSERT INTO broker_account (user_id, broker_id, label) VALUES ($1, $2, 'primary') \
         RETURNING id",
    )
    .bind(user_id)
    .bind(broker_id)
    .fetch_one(&mut *conn)
    .await
    .map_err(db::fail)
}

/// A `Decimal | None` bound as Postgres reads its text (any precision; `numeric(p,s)` rounds).
pub(crate) fn numeric_text(value: Option<&WideDecimal>) -> Option<String> {
    value.map(WideDecimal::to_sci_string)
}

/// `replace_holdings(session, portfolio, holdings, added_on=)`: the row count asked for.
pub(crate) async fn replace_holdings(
    conn: &mut PgConnection,
    portfolio_id: i64,
    user_id: i64,
    declared_broker_account_id: Option<i64>,
    holdings: &[(i64, Option<WideDecimal>, Option<WideDecimal>)],
    added_on: NaiveDate,
) -> Result<usize, Problem> {
    let broker_account_id = match declared_broker_account_id {
        Some(id) => id,
        None => ensure_default_broker_account(conn, user_id, DEFAULT_BROKER_ID).await?,
    };
    let existing = sqlx::query(
        "SELECT instrument_id, broker_account_id, added_on FROM portfolio_holding \
         WHERE portfolio_id = $1",
    )
    .bind(portfolio_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut first_seen: HashMap<i64, NaiveDate> = HashMap::new();
    let mut already_here: HashSet<i64> = HashSet::new();
    for row in &existing {
        let instrument_id: i64 = db::get(row, "instrument_id")?;
        let added: NaiveDate = db::date(row, "added_on")?;
        let seen = first_seen.entry(instrument_id).or_insert(added);
        if added < *seen {
            *seen = added;
        }
        if db::get::<i64>(row, "broker_account_id")? == broker_account_id {
            already_here.insert(instrument_id);
        }
    }
    let keep: Vec<i64> = holdings.iter().map(|(id, _, _)| *id).collect();
    sqlx::query(
        "DELETE FROM portfolio_holding WHERE portfolio_id = $1 \
         AND (NOT (instrument_id = ANY($2)) OR broker_account_id != $3)",
    )
    .bind(portfolio_id)
    .bind(&keep)
    .bind(broker_account_id)
    .execute(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut inserts = Vec::new();
    for (instrument_id, quantity, avg_price) in holdings {
        let on = first_seen.get(instrument_id).copied().unwrap_or(added_on);
        if already_here.contains(instrument_id) {
            sqlx::query(
                "UPDATE portfolio_holding SET quantity = $1::numeric, avg_price = $2::numeric, \
                 added_on = $3 WHERE portfolio_id = $4 AND instrument_id = $5 \
                 AND broker_account_id = $6",
            )
            .bind(numeric_text(quantity.as_ref()))
            .bind(numeric_text(avg_price.as_ref()))
            .bind(on)
            .bind(portfolio_id)
            .bind(instrument_id)
            .bind(broker_account_id)
            .execute(&mut *conn)
            .await
            .map_err(db::fail)?;
        } else {
            inserts.push((*instrument_id, quantity.clone(), avg_price.clone(), on));
        }
    }
    // The ORM flushes the added rows after the statements it ran directly.
    for (instrument_id, quantity, avg_price, on) in inserts {
        sqlx::query(
            "INSERT INTO portfolio_holding (portfolio_id, instrument_id, broker_account_id, \
             quantity, avg_price, added_on) VALUES ($1, $2, $3, $4::numeric, $5::numeric, $6)",
        )
        .bind(portfolio_id)
        .bind(instrument_id)
        .bind(broker_account_id)
        .bind(numeric_text(quantity.as_ref()))
        .bind(numeric_text(avg_price.as_ref()))
        .bind(on)
        .execute(&mut *conn)
        .await
        .map_err(db::fail)?;
    }
    Ok(holdings.len())
}
