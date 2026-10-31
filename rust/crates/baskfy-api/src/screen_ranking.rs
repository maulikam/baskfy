//! `baskfy_api.screen_ranking`: the I/O half of ranking explain and portfolio selection.
//! Informational only; nothing here plans, places or routes an order.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::decimal::WideDecimal;
use baskfy_core::ranking_engine::{self, ExplainContext, RankExplanation};
use baskfy_core::ranking_selection::{Candidate, Holding, Returns};
use baskfy_core::screener::{Payload, PyDecimal, ScreenDefinition};
use chrono::NaiveDate;
use polars::prelude::{NamedFrom, Series};
use sqlx::{PgConnection, Row as _};

use crate::screener::{
    NSE_EXCHANGE_ID, RankedFrame, RunError, desk_score_version, rank_definition, uses_desk_score,
};

/// `CORPORATE_ACTION_SESSIONS`.
pub const CORPORATE_ACTION_SESSIONS: i64 = 252;
/// `ADV_VALUE_COLUMN`.
pub const ADV_VALUE_COLUMN: &str = "median_vol_12m";
/// `CLOSE_RAW_COLUMN`.
pub const CLOSE_RAW_COLUMN: &str = "close_raw";

/// Why an explanation could not be given.
#[derive(Debug)]
pub enum ExplainError<C> {
    /// `InstrumentNotInUniverse(symbol)`.
    NotInUniverse(String),
    /// The run's.
    Run(RunError<C>),
}

impl<C> From<RunError<C>> for ExplainError<C> {
    fn from(e: RunError<C>) -> Self {
        Self::Run(e)
    }
}

impl<C> From<sqlx::Error> for ExplainError<C> {
    fn from(e: sqlx::Error) -> Self {
        Self::Run(RunError::Db(e))
    }
}

/// `previous_trading_day(session, as_of)`.
///
/// # Errors
/// The database's.
pub async fn previous_trading_day(
    conn: &mut PgConnection,
    as_of: NaiveDate,
) -> Result<Option<NaiveDate>, sqlx::Error> {
    let (v,): (Option<NaiveDate>,) = sqlx::query_as(
        "SELECT max(trading_day.date) FROM trading_day WHERE trading_day.exchange_id = $1 \
         AND trading_day.is_trading_day IS true AND trading_day.date < $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(as_of)
    .fetch_one(&mut *conn)
    .await?;
    Ok(v)
}

/// `previous_session_rank(session, definition, instrument_id, as_of)`.
///
/// # Errors
/// As [`rank_definition`].
pub async fn previous_session_rank<C>(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    instrument_id: i64,
    as_of: NaiveDate,
) -> Result<(Option<NaiveDate>, Option<i64>), RunError<C>> {
    let Some(previous) = previous_trading_day(conn, as_of).await? else {
        return Ok((None, None));
    };
    let ranked = rank_definition(conn, definition, previous).await?;
    let Some(result) = &ranked.result else {
        return Ok((Some(previous), None));
    };
    let rows = &result.ranked;
    for i in 0..rows.height() {
        if ranked.instrument_at(rows, i) == Some(instrument_id) {
            return Ok((Some(previous), RankedFrame::rank_at(rows, i)));
        }
    }
    Ok((Some(previous), None))
}

/// `recent_corporate_action(session, instrument_id, as_of)`.
///
/// # Errors
/// The database's.
pub async fn recent_corporate_action(
    conn: &mut PgConnection,
    instrument_id: i64,
    as_of: NaiveDate,
) -> Result<bool, sqlx::Error> {
    let (n,): (i64,) = sqlx::query_as(
        "SELECT count(DISTINCT anon_1.adj_factor) FROM (SELECT ohlcv_daily.adj_factor AS \
         adj_factor FROM ohlcv_daily WHERE ohlcv_daily.instrument_id = $1 AND ohlcv_daily.date \
         <= $2 ORDER BY ohlcv_daily.date DESC LIMIT $3) AS anon_1",
    )
    .bind(instrument_id)
    .bind(as_of)
    .bind(CORPORATE_ACTION_SESSIONS)
    .fetch_one(&mut *conn)
    .await?;
    Ok(n > 1)
}

/// `explain_symbol(session, definition, symbol, as_of=, data_version=)`.
///
/// # Errors
/// `InstrumentNotInUniverse`; the run's.
pub async fn explain_symbol<C>(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    symbol: &str,
    as_of: NaiveDate,
    data_version: i64,
) -> Result<RankExplanation, ExplainError<C>> {
    let ranked = rank_definition(conn, definition, as_of).await?;
    let wanted = symbol.trim().to_uppercase();
    let Some(result) = &ranked.result else {
        return Err(ExplainError::NotInUniverse(wanted));
    };
    let scored = &result.scored;
    let position = (0..scored.height()).find(|&i| {
        matches!(
            ranked.value(scored, i, ranking_engine::SYMBOL),
            Payload::Str(s) if s == wanted
        )
    });
    let Some(position) = position else {
        return Err(ExplainError::NotInUniverse(wanted));
    };
    let instrument_id = ranked
        .instrument_at(scored, position)
        .ok_or_else(|| ExplainError::NotInUniverse(wanted.clone()))?;
    let flag = recent_corporate_action(conn, instrument_id, as_of).await?;
    let (previous_as_of, previous_rank) =
        previous_session_rank(conn, definition, instrument_id, as_of).await?;
    let desk_version = if uses_desk_score(definition) {
        desk_score_version(conn, as_of).await?
    } else {
        None
    };
    let mut patched = result.clone();
    let column = Series::new(
        ranking_engine::RECENT_CORPORATE_ACTION.into(),
        vec![flag; scored.height()],
    );
    patched
        .scored
        .with_column(column.into())
        .map_err(|e| RunError::Raised("ValueError", e.to_string()))?;
    let context = ExplainContext {
        result: &patched,
        universe: ranked.universe_slug.clone(),
        as_of,
        data_version: Some(data_version),
        desk_score_version: desk_version,
        clause_details: ranked
            .clause_details
            .iter()
            .cloned()
            .collect::<BTreeMap<_, _>>(),
        previous_as_of,
        previous_rank,
    };
    ranking_engine::explain(position, &context)
        .map_err(|e| ExplainError::Run(RunError::Raised(e.python_type(), e.message().to_owned())))
}

fn number(value: &Payload) -> Option<f64> {
    match value {
        Payload::Int(i) => {
            #[allow(clippy::cast_precision_loss)]
            let f = *i as f64;
            Some(f)
        }
        Payload::Float(f) if !f.is_nan() => Some(*f),
        Payload::Decimal(PyDecimal::Finite(d)) => d.py_str().parse::<f64>().ok(),
        _ => None,
    }
}

fn decimal(value: &Payload) -> Option<WideDecimal> {
    match value {
        Payload::Int(i) => Some(WideDecimal::from(*i)),
        Payload::Float(f) if !f.is_nan() => baskfy_core::py::float_repr(*f).parse().ok(),
        Payload::Decimal(PyDecimal::Finite(d)) => d.py_str().parse().ok(),
        _ => None,
    }
}

/// `candidates_from(ranked)`: C5 candidates from the whole ranked output.
#[must_use]
pub fn candidates_from(ranked: &RankedFrame) -> Vec<Candidate> {
    let Some(result) = &ranked.result else {
        return Vec::new();
    };
    let Some(first) = ranked.spec.terms.first() else {
        return Vec::new();
    };
    let score_column = if ranked.spec.mode == ranking_engine::Mode::Composite {
        ranking_engine::COMPOSITE_SCORE
    } else {
        first.key.as_str()
    };
    let frame = &result.ranked;
    let mut out = Vec::with_capacity(frame.height());
    for i in 0..frame.height() {
        let sector = match ranked.value(frame, i, ranking_engine::SECTOR) {
            Payload::Str(s) => Some(s),
            _ => None,
        };
        let close = decimal(&ranked.value(frame, i, CLOSE_RAW_COLUMN))
            .filter(|c| !(c.is_zero() || c.is_sign_negative()));
        let symbol = match ranked.value(frame, i, ranking_engine::SYMBOL) {
            Payload::Str(s) => s,
            Payload::Null => "None".to_owned(),
            _ => String::new(),
        };
        out.push(Candidate {
            instrument_id: ranked.instrument_at(frame, i).unwrap_or(0),
            symbol,
            quality_rank: RankedFrame::rank_at(frame, i).unwrap_or(0),
            score: number(&ranked.value(frame, i, score_column)),
            sector,
            adv_value_inr: decimal(&ranked.value(frame, i, ADV_VALUE_COLUMN)),
            close_raw: close,
        });
    }
    out
}

/// `BookHoldings`.
#[derive(Debug, Clone, Default)]
pub struct BookHoldings {
    /// `holdings`.
    pub holdings: Vec<Holding>,
    /// `without_quantity`.
    pub without_quantity: Vec<String>,
}

/// `aggregate_holdings(rows)`: one holding per instrument, its slices' quantities summed.
#[must_use]
pub fn aggregate_holdings(rows: &[(i64, String, Option<WideDecimal>)]) -> BookHoldings {
    let mut totals: BTreeMap<i64, WideDecimal> = BTreeMap::new();
    let mut symbols: BTreeMap<i64, String> = BTreeMap::new();
    for (id, symbol, quantity) in rows {
        symbols.insert(*id, symbol.clone());
        let q = quantity.clone().unwrap_or_else(WideDecimal::zero);
        let total = totals.remove(id).unwrap_or_else(WideDecimal::zero).add(&q);
        totals.insert(*id, total);
    }
    let holdings = totals
        .iter()
        .filter(|(_, t)| !(t.is_zero() || t.is_sign_negative()))
        .map(|(id, t)| Holding {
            instrument_id: *id,
            symbol: symbols.get(id).cloned().unwrap_or_default(),
            quantity: t.clone(),
            sector: None,
        })
        .collect();
    let mut without: Vec<String> = totals
        .iter()
        .filter(|(_, t)| t.is_zero() || t.is_sign_negative())
        .map(|(id, _)| symbols.get(id).cloned().unwrap_or_default())
        .collect();
    without.sort();
    BookHoldings {
        holdings,
        without_quantity: without,
    }
}

/// `portfolio_holdings(session, portfolio_id)`.
///
/// # Errors
/// The database's.
pub async fn portfolio_holdings(
    conn: &mut PgConnection,
    portfolio_id: i64,
) -> Result<BookHoldings, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT portfolio_holding.instrument_id, instrument.symbol, \
         portfolio_holding.quantity::text AS quantity FROM portfolio_holding JOIN instrument ON \
         instrument.id = portfolio_holding.instrument_id WHERE portfolio_holding.portfolio_id = $1",
    )
    .bind(portfolio_id)
    .fetch_all(&mut *conn)
    .await?;
    let mut items = Vec::with_capacity(rows.len());
    for r in &rows {
        let q: Option<String> = r.try_get("quantity")?;
        items.push((
            r.try_get::<i64, _>("instrument_id")?,
            r.try_get::<String, _>("symbol")?,
            q.and_then(|t| t.parse::<WideDecimal>().ok()),
        ));
    }
    Ok(aggregate_holdings(&items))
}

/// `with_candidate_sectors(holdings, candidates)`.
#[must_use]
pub fn with_candidate_sectors(holdings: &[Holding], candidates: &[Candidate]) -> Vec<Holding> {
    let sectors: BTreeMap<i64, Option<String>> = candidates
        .iter()
        .map(|c| (c.instrument_id, c.sector.clone()))
        .collect();
    holdings
        .iter()
        .map(|h| Holding {
            sector: sectors.get(&h.instrument_id).cloned().flatten(),
            ..h.clone()
        })
        .collect()
}

/// `daily_returns(session, instrument_ids, as_of, window)`.
///
/// # Errors
/// The database's.
pub async fn daily_returns(
    conn: &mut PgConnection,
    instrument_ids: &BTreeSet<i64>,
    as_of: NaiveDate,
    window: i64,
) -> Result<Returns, sqlx::Error> {
    let ids: Vec<i64> = instrument_ids.iter().copied().collect();
    let mut sessions: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT trading_day.date FROM trading_day WHERE trading_day.exchange_id = $1 AND \
         trading_day.is_trading_day IS true AND trading_day.date <= $2 ORDER BY \
         trading_day.date DESC LIMIT $3",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(as_of)
    .bind(window + 1)
    .fetch_all(&mut *conn)
    .await?;
    sessions.reverse();
    if ids.is_empty() || sessions.len() < 2 {
        return Ok(Returns {
            columns: ids.into_iter().map(|id| (id, Vec::new())).collect(),
        });
    }
    let bars = sqlx::query(
        "SELECT ohlcv_daily.instrument_id, ohlcv_daily.date, ohlcv_daily.close::text AS close \
         FROM ohlcv_daily WHERE ohlcv_daily.instrument_id = ANY($1) AND ohlcv_daily.date >= $2 \
         AND ohlcv_daily.date <= $3",
    )
    .bind(&ids)
    .bind(sessions[0])
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let position: BTreeMap<NaiveDate, usize> =
        sessions.iter().enumerate().map(|(i, d)| (*d, i)).collect();
    let mut closes: BTreeMap<i64, Vec<f64>> = ids
        .iter()
        .map(|id| (*id, vec![f64::NAN; sessions.len()]))
        .collect();
    for b in &bars {
        let id: i64 = b.try_get("instrument_id")?;
        let date: NaiveDate = b.try_get("date")?;
        let close: Option<String> = b.try_get("close")?;
        if let (Some(p), Some(series)) = (position.get(&date), closes.get_mut(&id)) {
            series[*p] = close
                .and_then(|c| c.parse::<f64>().ok())
                .unwrap_or(f64::NAN);
        }
    }
    let columns = closes
        .into_iter()
        .map(|(id, c)| {
            let r: Vec<f64> = c.windows(2).map(|w| w[1] / w[0] - 1.0).collect();
            (id, r)
        })
        .collect();
    Ok(Returns { columns })
}
