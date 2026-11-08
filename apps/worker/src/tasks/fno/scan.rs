//! Ports `baskfy_worker.fno.scan`: `fo_scan` for the session after `trade_date`, per user
//! (`docs/fno/06` FO4). **Database only, no network.** The arithmetic is
//! `baskfy_core::fno::scan`'s; this module reads, decides the state (`04` §8) in Python's order
//! and writes. Idempotent per `(user_id, sleeve, trade_date, symbol)` (house rule 7): rows are
//! upserted and a row the re-run no longer produces is deleted.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::fno::calendar::{
    f2_contract_expiry, hard_exit_session, monthly_expiries, next_entry,
};
use baskfy_core::fno::condor::{ENTRY_SEQUENCE, option_type_of, sign_of};
use baskfy_core::fno::config::{
    FnoCeilings, FnoConfig, FoSleeve, ScanState, f1_sleeve_for, f3_sleeve_for,
};
use baskfy_core::fno::ledger::{FoPause, column_pause_applies, evaluate_pauses, pauses_for};
use baskfy_core::fno::scan::{
    CondorProposal, FutureProposal, LegPrint, LegPrints, REGIME_UNDERLYING, allocate_capacity,
    f2_signals, propose_condor, propose_future,
};
use baskfy_core::fno::series::cols;
use baskfy_core::fno::sizing::SizingMode;
use baskfy_core::fno::vol::{OptionPrint, OptionType, atm_iv};
use baskfy_core::options::costs::CostRates;
use baskfy_core::screener::SECTOR_INDEX_SLUGS;
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use polars::prelude::{Column, DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{
    dec_to_f64, dstr, fno_err, iso, opt_dstr, opt_float, opt_int, opt_iso, plan_state, q_dec,
    q_float, scan_state,
};
use super::ingest::STATUS_INGESTED;
use super::ledger::{ENTRIES_SIMULATED, load_lifts, load_trades, monthly_pause_inr};
use super::underlying::{minus, plus, trading_days};
use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::pyjson::{self, object, s, strings};

/// Calendar days of futures history the F2 window reads.
pub(crate) const SCAN_LOOKBACK_DAYS: u64 = 150;
/// Calendar days of exchange calendar ahead of `trade_date`.
pub(crate) const CALENDAR_AHEAD_DAYS: u64 = 120;
/// The sleeve-level row's symbol (FO4.3).
pub(crate) const SLEEVE_ROW: &str = "*";

const IV_RV_NOTE: &str = "IV ÷ RV20 is recorded, not used: F1 has no IV filter (01 §1)";
const SPREAD_NOTE: &str =
    "the live spread (≤ 5 % of mid) and the broker margin are checked at 09:20";

/// A JSON object under construction (a Python `dict`: an existing key keeps its place).
pub(crate) type Detail = Vec<(String, OrderedJson)>;

/// `{**base, key: value}`.
pub(crate) fn put(detail: &mut Detail, key: &str, value: OrderedJson) {
    pyjson::update(detail, key, value);
}

/// `ScanRow`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ScanRow {
    pub(crate) symbol: String,
    pub(crate) state: String,
    pub(crate) reasons: Vec<String>,
    pub(crate) detail: Detail,
    pub(crate) credit: Option<Decimal>,
    pub(crate) max_loss_per_lot: Option<Decimal>,
    pub(crate) cost_share: Option<Decimal>,
    pub(crate) iv: Option<Decimal>,
    pub(crate) rv20: Option<Decimal>,
    pub(crate) iv_rv: Option<Decimal>,
}

impl ScanRow {
    /// `ScanRow(symbol, state, reasons, detail)`: every row carries its reason.
    pub(crate) fn new(
        symbol: &str,
        state: ScanState,
        reasons: Vec<String>,
        detail: Detail,
    ) -> Result<Self, WorkerError> {
        Self::with_state(symbol, scan_state(state).to_owned(), reasons, detail)
    }

    pub(crate) fn with_state(
        symbol: &str,
        state: String,
        reasons: Vec<String>,
        detail: Detail,
    ) -> Result<Self, WorkerError> {
        if reasons.is_empty() || reasons.iter().any(|r| r.trim().is_empty()) {
            return Err(WorkerError::python(
                "ValueError",
                format!("{symbol} {state}: every scan row carries its reason"),
            ));
        }
        Ok(Self {
            symbol: symbol.to_owned(),
            state,
            reasons,
            detail,
            credit: None,
            max_loss_per_lot: None,
            cost_share: None,
            iv: None,
            rv20: None,
            iv_rv: None,
        })
    }
}

/// `_money(value)` for a float: `None`, or half-up to the paisa.
pub(crate) fn money_f(value: Option<f64>) -> Option<Decimal> {
    q_float(value, 2)
}

/// `str(x)` of an optional decimal (`"None"` for `None`, as an f-string prints it).
pub(crate) fn py_opt(d: Option<Decimal>) -> String {
    d.map_or_else(|| "None".to_owned(), dstr)
}

// ---------------------------------------------------------------------------------------------
// Market facts, shared by every user
// ---------------------------------------------------------------------------------------------

/// One futures row of the window.
#[derive(Debug, Clone)]
pub(crate) struct FutRow {
    pub(crate) trade_date: NaiveDate,
    pub(crate) symbol: String,
    pub(crate) expiry: NaiveDate,
    pub(crate) settle: Option<f64>,
    pub(crate) lot_size: Option<i64>,
}

/// One row of `f2_signals`.
#[derive(Debug, Clone)]
pub(crate) struct Signal {
    pub(crate) symbol: String,
    pub(crate) good_sessions: Option<i64>,
    pub(crate) close_inr: Option<f64>,
    pub(crate) prior_high_inr: Option<f64>,
    pub(crate) average_inr: Option<f64>,
    pub(crate) atr_inr: Option<f64>,
    pub(crate) rv20: Option<f64>,
    pub(crate) ca_recent: Option<bool>,
    pub(crate) turnover_20d: Option<f64>,
    pub(crate) turnover_rank: Option<f64>,
    pub(crate) ranked: Option<i64>,
    pub(crate) in_universe: Option<bool>,
    pub(crate) breakout: Option<bool>,
    pub(crate) trend: Option<bool>,
}

fn read_signals(frame: &DataFrame) -> Result<Vec<Signal>, WorkerError> {
    let f = |name: &str| cols::floats(frame, name).map_err(fno_err);
    let i = |name: &str| cols::ints(frame, name).map_err(fno_err);
    let b = |name: &str| cols::bools(frame, name).map_err(fno_err);
    let symbol = cols::strings(frame, "symbol").map_err(fno_err)?;
    let (good, close, prior, avg, atr, rv) = (
        i("good_sessions")?,
        f("close_inr")?,
        f("prior_high_inr")?,
        f("average_inr")?,
        f("atr_inr")?,
        f("rv20")?,
    );
    let (ca, turn, rank, ranked, uni, brk, trend) = (
        b("ca_recent")?,
        f("turnover_20d")?,
        f("turnover_rank")?,
        i("ranked")?,
        b("in_universe")?,
        b("breakout")?,
        b("trend")?,
    );
    Ok((0..frame.height())
        .map(|k| Signal {
            symbol: symbol[k].clone().unwrap_or_default(),
            good_sessions: good[k],
            close_inr: close[k],
            prior_high_inr: prior[k],
            average_inr: avg[k],
            atr_inr: atr[k],
            rv20: rv[k],
            ca_recent: ca[k],
            turnover_20d: turn[k],
            turnover_rank: rank[k],
            ranked: ranked[k],
            in_universe: uni[k],
            breakout: brk[k],
            trend: trend[k],
        })
        .collect())
}

/// `Market`.
#[derive(Debug, Clone)]
pub(crate) struct Market {
    pub(crate) trade_date: NaiveDate,
    pub(crate) next_session: Option<NaiveDate>,
    pub(crate) sessions: Vec<NaiveDate>,
    pub(crate) ingest_status: Option<String>,
    pub(crate) ban: Option<BTreeSet<String>>,
    pub(crate) futures: Vec<FutRow>,
    pub(crate) signals: Vec<Signal>,
    pub(crate) expiries: BTreeMap<String, Vec<NaiveDate>>,
    pub(crate) expiry_lots: BTreeMap<(String, NaiveDate), i64>,
    pub(crate) industries: BTreeMap<String, String>,
}

impl Market {
    /// `ingested`.
    pub(crate) fn ingested(&self) -> bool {
        self.ingest_status.as_deref() == Some(STATUS_INGESTED)
    }

    /// `settle(symbol, expiry)`: the session's settle, half-up to the paisa, and its lot size.
    pub(crate) fn settle(&self, symbol: &str, expiry: NaiveDate) -> Option<(Decimal, Option<i64>)> {
        let row = self.futures.iter().find(|r| {
            r.trade_date == self.trade_date && r.symbol == symbol && r.expiry == expiry
        })?;
        let settle = row.settle?;
        if settle <= 0.0 {
            return None;
        }
        let money = money_f(Some(settle))
            .filter(|d| !d.is_zero())
            .unwrap_or(Decimal::ZERO);
        Some((money, row.lot_size))
    }

    /// `listed_futures(symbol)`.
    pub(crate) fn listed_futures(&self, symbol: &str) -> Vec<NaiveDate> {
        self.futures
            .iter()
            .filter(|r| r.trade_date == self.trade_date && r.symbol == symbol)
            .map(|r| r.expiry)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }

    fn signal(&self, symbol: &str) -> Option<&Signal> {
        self.signals.iter().find(|r| r.symbol == symbol)
    }
}

type FutDbRow = (
    NaiveDate,
    String,
    String,
    NaiveDate,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<i64>,
    Option<Decimal>,
    Option<i32>,
);

async fn futures_window(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<(DataFrame, Vec<FutRow>), WorkerError> {
    let start = minus(trade_date, SCAN_LOOKBACK_DAYS);
    let rows: Vec<FutDbRow> = sqlx::query_as(
        "SELECT trade_date, symbol, instrument, expiry, open, high, low, settle, underlying, \
         open_interest, turnover, lot_size FROM fo_contract_daily \
         WHERE trade_date > $1 AND trade_date <= $2 AND instrument = ANY($3)",
    )
    .bind(start)
    .bind(trade_date)
    .bind(vec!["FUTSTK".to_owned(), "FUTIDX".to_owned()])
    .fetch_all(&mut *conn)
    .await?;
    let fl = |v: Option<Decimal>| v.map(dec_to_f64);
    let floats = |name: &str, f: fn(&FutDbRow) -> Option<Decimal>| -> Column {
        cols::float_column(name, &rows.iter().map(|r| fl(f(r))).collect::<Vec<_>>())
    };
    let n = rows.len();
    let frame = DataFrame::new(
        n,
        vec![
            cols::date_column(
                "trade_date",
                &rows.iter().map(|r| Some(r.0)).collect::<Vec<_>>(),
            )
            .map_err(fno_err)?,
            Series::new(
                "symbol".into(),
                rows.iter().map(|r| r.1.clone()).collect::<Vec<_>>(),
            )
            .into_column(),
            Series::new(
                "instrument".into(),
                rows.iter().map(|r| r.2.clone()).collect::<Vec<_>>(),
            )
            .into_column(),
            cols::date_column(
                "expiry",
                &rows.iter().map(|r| Some(r.3)).collect::<Vec<_>>(),
            )
            .map_err(fno_err)?,
            floats("open", |r| r.4),
            floats("high", |r| r.5),
            floats("low", |r| r.6),
            floats("settle", |r| r.7),
            floats("underlying", |r| r.8),
            cols::int_column(
                "open_interest",
                &rows.iter().map(|r| r.9).collect::<Vec<_>>(),
            ),
            floats("turnover", |r| r.10),
            cols::int_column(
                "lot_size",
                &rows.iter().map(|r| r.11.map(i64::from)).collect::<Vec<_>>(),
            ),
        ],
    )
    .map_err(|e| WorkerError::python("ShapeError", e.to_string()))?;
    let plain = rows
        .iter()
        .map(|r| FutRow {
            trade_date: r.0,
            symbol: r.1.clone(),
            expiry: r.3,
            settle: fl(r.7),
            lot_size: r.11.map(i64::from),
        })
        .collect();
    Ok((frame, plain))
}

type Expiries = (
    BTreeMap<String, Vec<NaiveDate>>,
    BTreeMap<(String, NaiveDate), i64>,
);

async fn expiries(conn: &mut PgConnection) -> Result<Expiries, WorkerError> {
    let rows: Vec<(String, NaiveDate, i32)> =
        sqlx::query_as("SELECT underlying, expiry_date, lot_size FROM op_expiry")
            .fetch_all(&mut *conn)
            .await?;
    let mut listed: BTreeMap<String, Vec<NaiveDate>> = BTreeMap::new();
    let mut lots = BTreeMap::new();
    for (underlying, expiry, lot) in rows {
        listed.entry(underlying.clone()).or_default().push(expiry);
        lots.insert((underlying, expiry), i64::from(lot));
    }
    Ok((listed, lots))
}

/// `_industries`: `symbol -> sector slug`, the narrowest NSE sector index the cash instrument
/// belongs to on the latest membership date ≤ `on` (FO4.5).
async fn industries(
    conn: &mut PgConnection,
    symbols: &[String],
    on: NaiveDate,
) -> Result<BTreeMap<String, String>, WorkerError> {
    let wanted: BTreeSet<&String> = symbols.iter().collect();
    if wanted.is_empty() {
        return Ok(BTreeMap::new());
    }
    let slugs: Vec<String> = SECTOR_INDEX_SLUGS.iter().map(|s| (*s).to_owned()).collect();
    let latest: Option<(Option<NaiveDate>,)> = sqlx::query_as(
        "SELECT max(m.date) FROM index_member_daily m JOIN index_def d ON d.id = m.index_id \
         WHERE m.date <= $1 AND d.slug = ANY($2)",
    )
    .bind(on)
    .bind(&slugs)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(latest) = latest.and_then(|(d,)| d) else {
        return Ok(BTreeMap::new());
    };
    let rows: Vec<(String, String, i64)> = sqlx::query_as(
        "SELECT instrument.symbol, index_def.slug, \
         count(*) OVER (PARTITION BY index_member_daily.index_id) AS count_1 \
         FROM instrument JOIN index_member_daily \
         ON index_member_daily.instrument_id = instrument.id \
         JOIN index_def ON index_def.id = index_member_daily.index_id \
         WHERE instrument.exchange_id = $1 AND index_member_daily.date = $2 \
         AND index_def.slug = ANY($3)",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(latest)
    .bind(&slugs)
    .fetch_all(&mut *conn)
    .await?;
    let mut best: BTreeMap<String, (i64, String)> = BTreeMap::new();
    for (symbol, slug, members) in rows {
        if !wanted.contains(&symbol) {
            continue;
        }
        match best.get(&symbol) {
            Some((n, _)) if members >= *n => {}
            _ => {
                best.insert(symbol, (members, slug));
            }
        }
    }
    Ok(best.into_iter().map(|(s, (_, slug))| (s, slug)).collect())
}

type IngestRow = (Option<String>, Option<NaiveDate>, Option<Vec<String>>);

/// `load_market`.
pub(crate) async fn load_market(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
    config: &FnoConfig,
) -> Result<Market, WorkerError> {
    let sessions = trading_days(
        conn,
        minus(trade_date, SCAN_LOOKBACK_DAYS),
        plus(trade_date, CALENDAR_AHEAD_DAYS),
    )
    .await?;
    let following = sessions.iter().copied().find(|d| *d > trade_date);
    let ingest: Option<IngestRow> = sqlx::query_as(
        "SELECT status, ban_for_session, ban_symbols FROM fo_ingest_day WHERE trade_date = $1",
    )
    .bind(trade_date)
    .fetch_optional(&mut *conn)
    .await?;
    let mut ban = None;
    if let (Some((_, Some(for_session), symbols)), Some(following)) = (&ingest, following)
        && *for_session == following
    {
        ban = Some(symbols.clone().unwrap_or_default().into_iter().collect());
    }
    let (frame, futures) = futures_window(conn, trade_date).await?;
    let signals_frame =
        f2_signals(&frame, trade_date, &config.f2, &config.series).map_err(fno_err)?;
    let signals = read_signals(&signals_frame)?;
    let (listed, lots) = expiries(conn).await?;
    let open_f2: Vec<(String,)> = sqlx::query_as(
        "SELECT symbol FROM fo_position WHERE sleeve = 'F2'::fo_sleeve AND closed_at IS NULL",
    )
    .fetch_all(&mut *conn)
    .await?;
    let mut names: Vec<String> = signals
        .iter()
        .filter(|r| r.in_universe == Some(true))
        .map(|r| r.symbol.clone())
        .collect();
    names.extend(open_f2.into_iter().map(|(s,)| s));
    let industries = industries(conn, &names, trade_date).await?;
    Ok(Market {
        trade_date,
        next_session: following,
        sessions,
        ingest_status: ingest.and_then(|(status, _, _)| status),
        ban,
        futures,
        signals,
        expiries: listed,
        expiry_lots: lots,
        industries,
    })
}

// ---------------------------------------------------------------------------------------------
// Per-user facts
// ---------------------------------------------------------------------------------------------

/// An open `fo_position`, the columns the scan reads.
#[derive(Debug, Clone)]
pub(crate) struct Position {
    pub(crate) id: i64,
    pub(crate) symbol: String,
    pub(crate) entry_plan_id: String,
    pub(crate) hard_exit_date: Option<NaiveDate>,
    pub(crate) stop_price: Option<Decimal>,
    pub(crate) next_roll_date: Option<NaiveDate>,
    pub(crate) legs: OrderedJson,
}

/// `UserBook`.
#[derive(Debug, Clone)]
pub(crate) struct UserBook {
    pub(crate) capital: BTreeMap<String, Decimal>,
    pub(crate) sleeve_pause: BTreeMap<String, String>,
    pub(crate) book_pause: Option<String>,
    pub(crate) open_positions: BTreeMap<String, Vec<Position>>,
    pub(crate) auto_pauses: Vec<FoPause>,
    pub(crate) events: Vec<(NaiveDate, String)>,
}

type PositionRow = (
    i64,
    String,
    String,
    String,
    Option<NaiveDate>,
    Option<Decimal>,
    Option<NaiveDate>,
    Option<serde_json::Value>,
);

/// `load_user`.
pub(crate) async fn load_user(
    conn: &mut PgConnection,
    user_id: i64,
    market: &Market,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> Result<UserBook, WorkerError> {
    let after = market.next_session.unwrap_or(market.trade_date);
    let simulated = ENTRIES_SIMULATED;
    let rows: Vec<(String, Decimal, Option<NaiveDate>, Option<String>)> = sqlx::query_as(
        "SELECT sleeve, capital_inr, paused_until, paused_reason FROM fo_sleeve_config \
         WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    let mut capital = BTreeMap::new();
    let mut sleeve_pause = BTreeMap::new();
    for (sleeve, inr, until, reason) in rows {
        capital.insert(sleeve.clone(), inr);
        if let Some(until_day) = until
            && column_pause_applies(until, reason.as_deref(), simulated, after)
        {
            sleeve_pause.insert(
                sleeve.clone(),
                format!(
                    "sleeve {sleeve} is paused until {} ({})",
                    iso(until_day),
                    reason
                        .as_deref()
                        .filter(|r| !r.is_empty())
                        .unwrap_or("no reason recorded")
                ),
            );
        }
    }
    let book: Option<(Option<NaiveDate>, Option<String>)> =
        sqlx::query_as("SELECT paused_until, paused_reason FROM fo_book_config WHERE user_id = $1")
            .bind(user_id)
            .fetch_optional(&mut *conn)
            .await?;
    let mut book_pause = None;
    if let Some((Some(until), reason)) = &book
        && column_pause_applies(Some(*until), reason.as_deref(), simulated, after)
    {
        book_pause = Some(format!(
            "the FO book is paused until {} ({})",
            iso(*until),
            reason
                .as_deref()
                .filter(|r| !r.is_empty())
                .unwrap_or("no reason recorded")
        ));
    }
    let monthly = monthly_pause_inr(conn, user_id).await?;
    let trades = load_trades(conn, user_id).await?;
    let lifts = load_lifts(conn, user_id).await?;
    let auto_pauses = evaluate_pauses(
        &trades,
        simulated,
        after,
        monthly,
        Some(&lifts),
        config,
        ceilings,
    )
    .map_err(fno_err)?;
    let positions: Vec<PositionRow> = sqlx::query_as(
        "SELECT id, sleeve::text, symbol, entry_plan_id, hard_exit_date, stop_price, \
         next_roll_date, legs FROM fo_position WHERE user_id = $1 AND closed_at IS NULL",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    let mut open_positions: BTreeMap<String, Vec<Position>> = BTreeMap::new();
    for r in positions {
        let legs = match r.7 {
            Some(v) => OrderedJson::parse(&v.to_string())
                .map_err(|e| WorkerError::python("ValueError", e.to_string()))?,
            None => OrderedJson::Null,
        };
        open_positions.entry(r.1).or_default().push(Position {
            id: r.0,
            symbol: r.2,
            entry_plan_id: r.3,
            hard_exit_date: r.4,
            stop_price: r.5,
            next_roll_date: r.6,
            legs,
        });
    }
    let events: Vec<(NaiveDate, String)> = sqlx::query_as(
        "SELECT date, reason FROM op_event_day WHERE user_id = $1 AND date >= $2 ORDER BY date",
    )
    .bind(user_id)
    .bind(market.trade_date)
    .fetch_all(&mut *conn)
    .await?;
    Ok(UserBook {
        capital,
        sleeve_pause,
        book_pause,
        open_positions,
        auto_pauses,
        events,
    })
}

// ---------------------------------------------------------------------------------------------
// F1
// ---------------------------------------------------------------------------------------------

type ChainRow = (
    Decimal,
    String,
    Option<Decimal>,
    Option<Decimal>,
    Option<i64>,
    Option<i64>,
);

async fn f1_chain(
    conn: &mut PgConnection,
    underlying: &str,
    expiry: NaiveDate,
    trade_date: NaiveDate,
) -> Result<(Vec<OptionPrint>, LegPrints), WorkerError> {
    let rows: Vec<ChainRow> = sqlx::query_as(
        "SELECT strike, option_type, close, settle, open_interest, volume \
             FROM fo_contract_daily WHERE trade_date = $1 AND symbol = $2 AND expiry = $3 \
             AND instrument = ANY($4)",
    )
    .bind(trade_date)
    .bind(underlying)
    .bind(expiry)
    .bind(vec!["OPTSTK".to_owned(), "OPTIDX".to_owned()])
    .fetch_all(&mut *conn)
    .await?;
    let mut prints = Vec::new();
    let mut legs: LegPrints = Vec::new();
    for (strike, kind, close, settle, oi, volume) in rows {
        let option_type = OptionType::parse(&kind).map_err(fno_err)?;
        prints.push(OptionPrint {
            expiry,
            strike: dec_to_f64(strike),
            option_type,
            close: close.map(dec_to_f64),
            volume: volume.unwrap_or(0),
        });
        let settle = settle.ok_or_else(|| {
            WorkerError::python(
                "TypeError",
                "conversion from NoneType to Decimal is not supported",
            )
        })?;
        legs.push((
            (strike, option_type),
            LegPrint {
                settle,
                open_interest: oi,
                volume,
            },
        ));
    }
    Ok((prints, legs))
}

async fn listed_strikes(
    conn: &mut PgConnection,
    underlying: &str,
    expiry: NaiveDate,
) -> Result<(Vec<Decimal>, Vec<Decimal>), WorkerError> {
    let rows: Vec<(String, Decimal)> = sqlx::query_as(
        "SELECT option_type, strike FROM op_contract WHERE underlying = $1 AND expiry = $2",
    )
    .bind(underlying)
    .bind(expiry)
    .fetch_all(&mut *conn)
    .await?;
    let (mut calls, mut puts) = (Vec::new(), Vec::new());
    for (kind, strike) in rows {
        match OptionType::parse(&kind).map_err(fno_err)? {
            OptionType::Ce => calls.push(strike),
            OptionType::Pe => puts.push(strike),
        }
    }
    Ok((calls, puts))
}

/// `pauses_for` messages, book first, then the stored sleeve pause, then the ledger's.
pub(crate) fn pauses(book: &UserBook, group: &str, sleeve: FoSleeve) -> Vec<String> {
    let mut found = Vec::new();
    if let Some(p) = &book.book_pause {
        found.push(p.clone());
    }
    if let Some(p) = book.sleeve_pause.get(group) {
        found.push(p.clone());
    }
    found.extend(
        pauses_for(&book.auto_pauses, sleeve)
            .into_iter()
            .map(|p| p.message),
    );
    found
}

/// `OptionType.value`.
pub(crate) const fn option_value(kind: OptionType) -> &'static str {
    match kind {
        OptionType::Ce => "CE",
        OptionType::Pe => "PE",
    }
}

/// `SizingMode.value`.
pub(crate) const fn sizing_mode(mode: SizingMode) -> &'static str {
    match mode {
        SizingMode::Budget => "BUDGET",
        SizingMode::PaperOneLot => "PAPER_ONE_LOT",
    }
}

fn proposal_detail(p: &CondorProposal, expiry: NaiveDate, lot_size: i64) -> Detail {
    let mut legs = Vec::new();
    if let Some(strikes) = &p.strikes {
        for (seq, role) in ENTRY_SEQUENCE.into_iter().enumerate() {
            legs.push(object([
                (
                    "entry_seq",
                    OrderedJson::Int(i64::try_from(seq).unwrap_or(0) + 1),
                ),
                ("role", s(role.value())),
                ("strike", s(dstr(strikes.strike(role)))),
                ("option_type", s(option_value(option_type_of(role)))),
                ("expiry", s(iso(expiry))),
                ("qty_sign", OrderedJson::Int(sign_of(role))),
                ("settle", opt_dstr(p.entry.get(&role).copied())),
            ]));
        }
    }
    let mut out: Detail = vec![
        ("legs".to_owned(), OrderedJson::Array(legs)),
        ("lot_size".to_owned(), OrderedJson::Int(lot_size)),
        ("sd_points".to_owned(), s(py_opt(q_dec(Some(p.sd), 2)))),
        ("credit_points".to_owned(), opt_dstr(p.credit)),
        (
            "max_loss_per_unit".to_owned(),
            opt_dstr(p.max_loss_per_unit),
        ),
        (
            "max_loss_per_lot_inr".to_owned(),
            opt_dstr(p.max_loss_per_lot_inr),
        ),
    ];
    if let Some(sz) = &p.sizing {
        put(
            &mut out,
            "sizing",
            object([
                ("lots", OrderedJson::Int(sz.lots)),
                ("sizing_mode", s(sizing_mode(sz.sizing_mode))),
                (
                    "risk_budget_inr",
                    s(py_opt(q_dec(Some(sz.risk_budget_inr), 2))),
                ),
                ("max_loss_inr", s(py_opt(q_dec(Some(sz.max_loss_inr), 2)))),
                ("message", s(sz.message.clone())),
            ]),
        );
    }
    if let Some(cost) = &p.cost {
        put(
            &mut out,
            "cost",
            object([
                ("round_trip_inr", s(dstr(cost.round_trip_inr))),
                ("credit_inr", s(dstr(cost.credit_inr))),
                ("cost_share_pct", opt_dstr(cost.cost_share_pct)),
            ]),
        );
    }
    out
}

fn merge(base: &Detail, more: Detail) -> Detail {
    let mut out = base.clone();
    for (k, v) in more {
        put(&mut out, &k, v);
    }
    out
}

#[allow(clippy::too_many_lines)] // one return per state of 04 §8, in order, as Python
async fn f1_row(
    conn: &mut PgConnection,
    market: &Market,
    book: &UserBook,
    underlying: &str,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> Result<ScanRow, WorkerError> {
    let sleeve = f1_sleeve_for(underlying).map_err(fno_err)?;
    let s1 = market.next_session;
    let mut base: Detail = vec![
        ("underlying".to_owned(), s(underlying)),
        ("next_session".to_owned(), opt_iso(s1)),
    ];
    if let Some(position) = book
        .open_positions
        .get(sleeve.value())
        .and_then(|v| v.first())
    {
        return ScanRow::new(
            underlying,
            ScanState::OpenPosition,
            vec![format!(
                "an F1 structure on {underlying} is open (plan {}); the next entry waits for \
                 its exit (04 §1)",
                position.entry_plan_id
            )],
            merge(
                &base,
                vec![
                    ("position_id".to_owned(), OrderedJson::Int(position.id)),
                    (
                        "hard_exit_date".to_owned(),
                        opt_iso(position.hard_exit_date),
                    ),
                ],
            ),
        );
    }
    let Some(s1) = s1 else {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "the trading_day calendar has no session after {}",
                iso(market.trade_date)
            )],
            base,
        );
    };
    let listed = market.expiries.get(underlying).cloned().unwrap_or_default();
    if listed.is_empty() {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "the NFO master (op_expiry) lists no expiry for {underlying}"
            )],
            base,
        );
    }
    let n = config.f1.entry_sessions_before;
    let Some((entry, expiry)) = next_entry(s1, &listed, &market.sessions, n).map_err(fno_err)?
    else {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "the trading_day calendar or the NFO master does not reach the entry session \
                 of a monthly after {}",
                iso(s1)
            )],
            merge(
                &base,
                vec![(
                    "monthlies".to_owned(),
                    strings(monthly_expiries(&listed).into_iter().map(iso)),
                )],
            ),
        );
    };
    put(&mut base, "next_entry_date", s(iso(entry)));
    put(&mut base, "expiry", s(iso(expiry)));
    let found = pauses(book, "F1", sleeve);
    if !found.is_empty() {
        return ScanRow::new(underlying, ScanState::Paused, found, base);
    }
    if entry != s1 {
        return ScanRow::new(
            underlying,
            ScanState::NotEntryDay,
            vec![format!(
                "the next entry session is {}, {n} sessions before the {} monthly; {} is not it",
                iso(entry),
                iso(expiry),
                iso(s1)
            )],
            base,
        );
    }
    f1_entry_row(
        conn, market, book, underlying, expiry, s1, base, config, ceilings,
    )
    .await
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)] // the entry session's inputs
async fn f1_entry_row(
    conn: &mut PgConnection,
    market: &Market,
    book: &UserBook,
    underlying: &str,
    expiry: NaiveDate,
    s1: NaiveDate,
    base: Detail,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> Result<ScanRow, WorkerError> {
    if !market.ingested() {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "the bhavcopy for {} is not ingested ({}); nothing is proposed from a guess",
                iso(market.trade_date),
                market
                    .ingest_status
                    .as_deref()
                    .filter(|x| !x.is_empty())
                    .unwrap_or("never tried")
            )],
            base,
        );
    }
    let Some((forward, bhav_lot)) = market.settle(underlying, expiry) else {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "the bhavcopy has no {underlying} {} future settle",
                iso(expiry)
            )],
            base,
        );
    };
    let (prints, legs) = f1_chain(conn, underlying, expiry, market.trade_date).await?;
    let mut settle = BTreeMap::new();
    settle.insert(expiry, dec_to_f64(forward));
    let atm = atm_iv(
        market.trade_date,
        &prints,
        &settle,
        &market.sessions,
        &config.series,
    );
    let iv = atm.filter(|a| a.expiry == expiry).and_then(|a| a.iv());
    let Some(iv) = iv else {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "no ATM IV on the {} monthly: a leg did not trade (04 §4)",
                iso(expiry)
            )],
            merge(&base, vec![("forward".to_owned(), s(dstr(forward)))]),
        );
    };
    let lot_size = market
        .expiry_lots
        .get(&(underlying.to_owned(), expiry))
        .copied()
        .filter(|l| *l != 0)
        .or(bhav_lot);
    let Some(lot_size) = lot_size.filter(|l| *l > 0) else {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "neither the master nor the bhavcopy gives a lot size for {underlying}"
            )],
            base,
        );
    };
    let (mut calls, mut puts) = listed_strikes(conn, underlying, expiry).await?;
    let mut strike_source = "op_contract";
    if calls.is_empty() || puts.is_empty() {
        strike_source = "bhavcopy";
        let pick = |kind: OptionType| -> Vec<Decimal> {
            let set: BTreeSet<Decimal> = legs
                .iter()
                .filter(|((_, t), _)| *t == kind)
                .map(|((k, _), _)| k.normalize())
                .collect();
            // Python sorts a set of Decimals; equal values compare equal whatever their scale,
            // and the first one seen is the one kept.
            set.into_iter()
                .map(|v| {
                    legs.iter()
                        .find(|((k, t), _)| *t == kind && k.normalize() == v)
                        .map_or(v, |((k, _), _)| *k)
                })
                .collect()
        };
        calls = pick(OptionType::Ce);
        puts = pick(OptionType::Pe);
    }
    let days = (expiry - s1).num_days();
    let proposal = propose_condor(
        forward,
        iv,
        days,
        &calls,
        &puts,
        &legs,
        lot_size,
        book.capital.get("F1").copied().unwrap_or(Decimal::ZERO),
        config,
        &CostRates::default(),
        ceilings,
    )
    .map_err(fno_err)?;
    let hard_exit = hard_exit_session(
        &market.sessions,
        expiry,
        config.common.hard_exit_before_expiry,
    )
    .map_err(fno_err)?;
    let last_day = hard_exit.unwrap_or(expiry);
    let events: Vec<&(NaiveDate, String)> = book
        .events
        .iter()
        .filter(|(d, _)| s1 <= *d && *d <= last_day)
        .collect();
    let rv = market.signal(underlying).and_then(|r| r.rv20);
    let iv_rv = rv.filter(|r| *r > 0.0).map(|r| iv / r);
    let mut detail = merge(
        &base,
        vec![
            ("entry_session".to_owned(), s(iso(s1))),
            ("hard_exit_date".to_owned(), opt_iso(hard_exit)),
            ("forward".to_owned(), s(dstr(forward))),
            ("iv".to_owned(), OrderedJson::Float(iv)),
            ("rv20".to_owned(), opt_float(rv)),
            ("iv_rv".to_owned(), opt_float(iv_rv)),
            ("iv_rv_note".to_owned(), s("recorded, not used")),
            ("days_to_expiry".to_owned(), OrderedJson::Int(days)),
            ("strike_source".to_owned(), s(strike_source)),
            (
                "events".to_owned(),
                OrderedJson::Array(
                    events
                        .iter()
                        .map(|(d, r)| object([("date", s(iso(*d))), ("reason", s(r.clone()))]))
                        .collect(),
                ),
            ),
        ],
    );
    detail = merge(&detail, proposal_detail(&proposal, expiry, lot_size));
    if let Some(sz) = &proposal.sizing {
        put(&mut detail, "lots", OrderedJson::Int(sz.lots));
    }
    let mut reasons = proposal.reasons.clone();
    let state = if events.is_empty() {
        proposal
            .state
            .map_or(scan_state(ScanState::Candidate), plan_state)
            .to_owned()
    } else {
        let named: Vec<String> = events
            .iter()
            .map(|(d, r)| format!("{} {r}", iso(*d)))
            .collect();
        reasons.insert(
            0,
            format!(
                "event day inside the hold {} to {}: {}; skipped unless Maulik accepts it on the \
                 plan (01 §1)",
                iso(s1),
                iso(last_day),
                named.join(", ")
            ),
        );
        scan_state(ScanState::SkippedEvent).to_owned()
    };
    reasons.push(IV_RV_NOTE.to_owned());
    reasons.push(SPREAD_NOTE.to_owned());
    let mut row = ScanRow::with_state(underlying, state, reasons, detail)?;
    row.credit = q_dec(proposal.credit, 2);
    row.max_loss_per_lot = proposal.max_loss_per_lot_inr;
    row.cost_share = proposal
        .cost
        .as_ref()
        .and_then(|c| q_dec(c.cost_share_pct, 4));
    row.iv = q_float(Some(iv), 6);
    row.rv20 = q_float(rv, 6);
    row.iv_rv = q_float(iv_rv, 4);
    Ok(row)
}

// ---------------------------------------------------------------------------------------------
// F2
// ---------------------------------------------------------------------------------------------

fn no_signal_reason(row: &Signal, config: &FnoConfig) -> String {
    let mut parts = Vec::new();
    let close = py_opt(money_f(row.close_inr));
    if row.breakout != Some(true) {
        parts.push(format!(
            "close {close} is not above the prior {}-session high {}",
            config.f2.breakout_sessions,
            py_opt(money_f(row.prior_high_inr))
        ));
    }
    if row.trend != Some(true) {
        parts.push(format!(
            "close {close} is not above its {}-session average {}",
            config.f2.trend_sessions,
            py_opt(money_f(row.average_inr))
        ));
    }
    parts.join("; ")
}

fn f2_contract(
    market: &Market,
    symbol: &str,
    config: &FnoConfig,
) -> Option<(NaiveDate, &'static str)> {
    let s1 = market.next_session?;
    let sources = [
        (
            "op_expiry",
            market.expiries.get(symbol).cloned().unwrap_or_default(),
        ),
        ("bhavcopy", market.listed_futures(symbol)),
    ];
    for (source, listed) in sources {
        if listed.is_empty() {
            continue;
        }
        if let Some(expiry) = f2_contract_expiry(
            s1,
            &listed,
            &market.sessions,
            config.f2.near_month_min_sessions,
        ) {
            return Some((expiry, source));
        }
    }
    None
}

fn future_detail(
    p: &FutureProposal,
    expiry: NaiveDate,
    source: &str,
    lot_size: i64,
) -> Result<Detail, WorkerError> {
    let total = p.cost.total().map_err(fno_err)?;
    Ok(vec![
        ("contract_expiry".to_owned(), s(iso(expiry))),
        ("expiry_source".to_owned(), s(source)),
        ("entry_reference".to_owned(), s(dstr(p.entry))),
        ("atr14".to_owned(), s(dstr(p.atr))),
        ("stop".to_owned(), s(dstr(p.stop))),
        ("gtt_trigger".to_owned(), s(dstr(p.gtt_trigger))),
        (
            "risk_per_unit".to_owned(),
            s(py_opt(q_dec(Some(p.risk_per_unit), 2))),
        ),
        ("risk_per_lot_inr".to_owned(), s(dstr(p.risk_per_lot_inr))),
        ("lot_size".to_owned(), OrderedJson::Int(lot_size)),
        ("lots".to_owned(), OrderedJson::Int(p.sizing.lots)),
        (
            "sizing_mode".to_owned(),
            s(sizing_mode(p.sizing.sizing_mode)),
        ),
        (
            "lots_at_ceiling".to_owned(),
            opt_int(p.sizing.lots_at_ceiling),
        ),
        ("round_trip_inr".to_owned(), s(dstr(total))),
        ("cost_share_pct".to_owned(), s(dstr(p.cost_share_pct))),
    ])
}

fn candidate_reasons(
    p: &FutureProposal,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> Vec<String> {
    let mut reasons = vec![format!(
        "close above the prior {}-session high and its {}-session average, NIFTY above its own \
         (04 §10)",
        config.f2.breakout_sessions, config.f2.trend_sessions
    )];
    if let Some(at_ceiling) = p.sizing.lots_at_ceiling {
        reasons.push(p.sizing.message.clone());
        if at_ceiling == 0 {
            reasons.push(format!(
                "live would be REJECTED_SIZE: one lot risks ₹{}, above the ₹{} per-trade ceiling",
                dstr(p.risk_per_lot_inr),
                dstr(ceilings.risk_per_trade_inr_max)
            ));
        }
    }
    reasons
}

#[allow(clippy::too_many_lines)] // the ordered states of 04 §8 for one sleeve, as Python
fn f2_rows(
    market: &Market,
    book: &UserBook,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> Result<Vec<ScanRow>, WorkerError> {
    let s1 = market.next_session;
    let (true, Some(s1)) = (market.ingested(), s1) else {
        let why = if market.ingested() {
            format!(
                "the trading_day calendar has no session after {}",
                iso(market.trade_date)
            )
        } else {
            format!(
                "the bhavcopy for {} is not ingested ({})",
                iso(market.trade_date),
                market
                    .ingest_status
                    .as_deref()
                    .filter(|x| !x.is_empty())
                    .unwrap_or("never tried")
            )
        };
        return Ok(vec![ScanRow::new(
            SLEEVE_ROW,
            ScanState::NoData,
            vec![why],
            Vec::new(),
        )?]);
    };
    if market.signals.is_empty() {
        return Ok(vec![ScanRow::new(
            SLEEVE_ROW,
            ScanState::NoData,
            vec!["no continuous futures series could be derived from the window".to_owned()],
            Vec::new(),
        )?]);
    }
    let mut rows = Vec::new();
    let open_f2: Vec<Position> = book.open_positions.get("F2").cloned().unwrap_or_default();
    let held: BTreeSet<&str> = open_f2.iter().map(|p| p.symbol.as_str()).collect();
    for position in &open_f2 {
        rows.push(ScanRow::new(
            &position.symbol,
            ScanState::OpenPosition,
            vec![format!(
                "an F2 position in {} is open; one per stock (04 §10)",
                position.symbol
            )],
            vec![
                ("position_id".to_owned(), OrderedJson::Int(position.id)),
                ("stop_price".to_owned(), opt_dstr(position.stop_price)),
                (
                    "next_roll_date".to_owned(),
                    opt_iso(position.next_roll_date),
                ),
            ],
        )?);
    }
    let mut regime: Option<bool> = None;
    let mut regime_detail: Detail = Vec::new();
    let mut nifty_close = "None".to_owned();
    let mut nifty_average = "None".to_owned();
    if let Some(n_row) = market.signal(REGIME_UNDERLYING) {
        regime = n_row.trend;
        nifty_close = py_opt(money_f(n_row.close_inr));
        nifty_average = py_opt(money_f(n_row.average_inr));
        regime_detail = vec![
            ("nifty_close".to_owned(), s(nifty_close.clone())),
            ("nifty_average".to_owned(), s(nifty_average.clone())),
            (
                "nifty_up".to_owned(),
                regime.map_or(OrderedJson::Null, OrderedJson::Bool),
            ),
        ];
    }
    let found_pauses = pauses(book, "F2", FoSleeve::F2);
    let capital = book.capital.get("F2").copied().unwrap_or(Decimal::ZERO);
    let ban_note = market.ban.is_none().then(|| {
        format!(
            "the ban list for {} is not stored yet; the 09:20 plan re-checks it",
            iso(s1)
        )
    });
    let mut queued: Vec<(f64, String, ScanRow)> = Vec::new();
    let mut universe: Vec<&Signal> = market
        .signals
        .iter()
        .filter(|r| r.in_universe == Some(true))
        .collect();
    universe.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    for row in universe {
        let symbol = row.symbol.as_str();
        if held.contains(symbol) {
            continue;
        }
        let mut detail: Detail = vec![
            (
                "turnover_20d".to_owned(),
                opt_dstr(money_f(row.turnover_20d)),
            ),
            ("turnover_rank".to_owned(), opt_float(row.turnover_rank)),
            ("universe_size".to_owned(), opt_int(row.ranked)),
            ("close".to_owned(), opt_dstr(money_f(row.close_inr))),
            (
                "breakout_level".to_owned(),
                opt_dstr(money_f(row.prior_high_inr)),
            ),
            ("average".to_owned(), opt_dstr(money_f(row.average_inr))),
            (
                "industry".to_owned(),
                market
                    .industries
                    .get(symbol)
                    .map_or(OrderedJson::Null, |i| s(i.clone())),
            ),
        ];
        for (k, v) in &regime_detail {
            put(&mut detail, k, v.clone());
        }
        let rv = row.rv20;
        if market.ban.as_ref().is_some_and(|b| b.contains(symbol)) {
            rows.push(ScanRow::new(
                symbol,
                ScanState::BlockedBan,
                vec![format!(
                    "{symbol} is in the F&O ban list for {} (02 Track C §9)",
                    iso(s1)
                )],
                detail,
            )?);
            continue;
        }
        if row.ca_recent == Some(true) {
            rows.push(ScanRow::new(
                symbol,
                ScanState::NoSignal,
                vec![format!(
                    "a corporate action (a > 30 % move in the held future) in the last {} \
                     sessions excludes it from signals (04 §4)",
                    config.series.ca_exclusion_sessions
                )],
                detail,
            )?);
            continue;
        }
        let (Some(breakout), Some(trend)) = (row.breakout, row.trend) else {
            rows.push(ScanRow::new(
                symbol,
                ScanState::NoData,
                vec![format!(
                    "{} good sessions in the window; the breakout and {}-session average need \
                     more",
                    row.good_sessions
                        .map_or_else(|| "None".to_owned(), |g| g.to_string()),
                    config.f2.trend_sessions
                )],
                detail,
            )?);
            continue;
        };
        if !(breakout && trend) {
            rows.push(ScanRow::new(
                symbol,
                ScanState::NoSignal,
                vec![no_signal_reason(row, config)],
                detail,
            )?);
            continue;
        }
        let Some(up) = regime else {
            rows.push(ScanRow::new(
                symbol,
                ScanState::NoData,
                vec![format!(
                    "NIFTY's continuous future has no {}-session average in the window, so the \
                     regime cannot be read",
                    config.f2.trend_sessions
                )],
                detail,
            )?);
            continue;
        };
        if !up {
            rows.push(ScanRow::new(
                symbol,
                ScanState::BlockedRegime,
                vec![format!(
                    "NIFTY's future {nifty_close} is below its {}-session average \
                     {nifty_average} (04 §10)",
                    config.f2.trend_sessions
                )],
                detail,
            )?);
            continue;
        }
        if !found_pauses.is_empty() {
            rows.push(ScanRow::new(
                symbol,
                ScanState::Paused,
                found_pauses.clone(),
                detail,
            )?);
            continue;
        }
        let contract = f2_contract(market, symbol, config);
        let future = contract.and_then(|(e, _)| market.settle(symbol, e));
        let atr = money_f(row.atr_inr);
        let (Some((expiry, source)), Some((entry, bhav_lot)), Some(atr)) =
            (contract, future, atr.filter(|a| *a > Decimal::ZERO))
        else {
            let missing = match (contract, future) {
                (None, _) => format!(
                    "no monthly contract more than {} sessions from expiry",
                    config.f2.near_month_min_sessions
                ),
                (Some((e, _)), None) => format!("no {} settle in the bhavcopy", iso(e)),
                _ => "no ATR14 in the window".to_owned(),
            };
            rows.push(ScanRow::new(
                symbol,
                ScanState::NoData,
                vec![missing],
                detail,
            )?);
            continue;
        };
        let lot_size = market
            .expiry_lots
            .get(&(symbol.to_owned(), expiry))
            .copied()
            .filter(|l| *l != 0)
            .or(bhav_lot);
        let Some(lot_size) = lot_size.filter(|l| *l > 0) else {
            rows.push(ScanRow::new(
                symbol,
                ScanState::NoData,
                vec![format!(
                    "neither the master nor the bhavcopy gives a lot size for {symbol}"
                )],
                detail,
            )?);
            continue;
        };
        let proposal = propose_future(
            entry,
            atr,
            rv.unwrap_or(0.0),
            lot_size,
            capital,
            config,
            ceilings,
        )
        .map_err(fno_err)?;
        detail = merge(&detail, future_detail(&proposal, expiry, source, lot_size)?);
        let mut scan_row = ScanRow::new(
            symbol,
            ScanState::Candidate,
            candidate_reasons(&proposal, config, ceilings),
            detail,
        )?;
        scan_row.max_loss_per_lot = Some(proposal.risk_per_lot_inr);
        scan_row.cost_share = q_dec(Some(proposal.cost_share_pct), 4);
        scan_row.rv20 = q_float(rv, 6);
        if let Some(note) = &ban_note {
            scan_row.reasons.push(note.clone());
        }
        if let Some(state) = proposal.sizing.state {
            scan_row.state = plan_state(state).to_owned();
            scan_row.reasons.insert(0, proposal.sizing.message.clone());
            rows.push(scan_row);
            continue;
        }
        queued.push((row.turnover_20d.unwrap_or(0.0), symbol.to_owned(), scan_row));
    }
    queued.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.1.cmp(&b.1))
    });
    let ordered: Vec<(String, Option<String>)> = queued
        .iter()
        .map(|(_, symbol, _)| (symbol.clone(), market.industries.get(symbol).cloned()))
        .collect();
    let open_industries: Vec<Option<String>> = open_f2
        .iter()
        .map(|p| market.industries.get(&p.symbol).cloned())
        .collect();
    let verdicts = allocate_capacity(&ordered, &open_industries, &config.f2);
    for (_, symbol, mut scan_row) in queued {
        let blocked = verdicts
            .iter()
            .find(|(s, _)| *s == symbol)
            .and_then(|(_, v)| v.clone());
        if let Some(blocked) = blocked {
            scan_row.state = scan_state(ScanState::BlockedCapacity).to_owned();
            scan_row.reasons.insert(0, format!("{blocked} (04 §10)"));
        }
        rows.push(scan_row);
    }
    Ok(rows)
}

// ---------------------------------------------------------------------------------------------
// Write
// ---------------------------------------------------------------------------------------------

/// `write_rows`: upsert on `(user_id, sleeve, trade_date, symbol)` and delete what the run no
/// longer produces, so a re-run leaves exactly this set (house rule 7).
pub(crate) async fn write_rows(
    conn: &mut PgConnection,
    user_id: i64,
    sleeve: FoSleeve,
    trade_date: NaiveDate,
    rows: &[ScanRow],
) -> Result<i64, WorkerError> {
    let symbols: Vec<String> = rows.iter().map(|r| r.symbol.clone()).collect();
    if symbols.iter().collect::<BTreeSet<_>>().len() != symbols.len() {
        return Err(WorkerError::python(
            "ValueError",
            format!("{}: a symbol appears twice in one scan", sleeve.value()),
        ));
    }
    if symbols.is_empty() {
        sqlx::query(
            "DELETE FROM fo_scan WHERE user_id = $1 AND sleeve = $2::fo_sleeve AND trade_date = $3",
        )
        .bind(user_id)
        .bind(sleeve.value())
        .bind(trade_date)
        .execute(&mut *conn)
        .await?;
        return Ok(0);
    }
    sqlx::query(
        "DELETE FROM fo_scan WHERE user_id = $1 AND sleeve = $2::fo_sleeve AND trade_date = $3 \
         AND (symbol NOT IN (SELECT unnest($4::varchar[])))",
    )
    .bind(user_id)
    .bind(sleeve.value())
    .bind(trade_date)
    .bind(&symbols)
    .execute(&mut *conn)
    .await?;
    for r in rows {
        let detail = pyjson::dumps(&OrderedJson::Object(r.detail.clone()))
            .map_err(|e| WorkerError::python("ValueError", e))?;
        sqlx::query(
            "INSERT INTO fo_scan (user_id, sleeve, trade_date, symbol, state, reasons, detail, \
             credit, max_loss_per_lot, cost_share, iv, rv20, iv_rv) \
             VALUES ($1, $2::fo_sleeve, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13) \
             ON CONFLICT ON CONSTRAINT uq_fo_scan_user_sleeve_date_symbol DO UPDATE SET \
             state = excluded.state, reasons = excluded.reasons, detail = excluded.detail, \
             credit = excluded.credit, max_loss_per_lot = excluded.max_loss_per_lot, \
             cost_share = excluded.cost_share, iv = excluded.iv, rv20 = excluded.rv20, \
             iv_rv = excluded.iv_rv",
        )
        .bind(user_id)
        .bind(sleeve.value())
        .bind(trade_date)
        .bind(&r.symbol)
        .bind(&r.state)
        .bind(&r.reasons)
        .bind(detail)
        .bind(r.credit)
        .bind(r.max_loss_per_lot)
        .bind(r.cost_share)
        .bind(r.iv)
        .bind(r.rv20)
        .bind(r.iv_rv)
        .execute(&mut *conn)
        .await?;
    }
    Ok(i64::try_from(rows.len()).unwrap_or(i64::MAX))
}

// ---------------------------------------------------------------------------------------------
// The run
// ---------------------------------------------------------------------------------------------

/// `scan_users`: every user with an `fo_sleeve_config` row (FO4.2), ascending.
pub(crate) async fn scan_users(conn: &mut PgConnection) -> Result<Vec<i64>, WorkerError> {
    let rows: Vec<(i64,)> = sqlx::query_as("SELECT DISTINCT user_id FROM fo_sleeve_config")
        .fetch_all(&mut *conn)
        .await?;
    let mut users: Vec<i64> = rows.into_iter().map(|(u,)| u).collect();
    users.sort_unstable();
    Ok(users)
}

/// `run_scan`: `fo_scan` for the session after `trade_date`, for each user. No network.
pub(crate) async fn run_scan(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
    user_ids: Option<Vec<i64>>,
) -> Result<OrderedJson, WorkerError> {
    let config = FnoConfig::default();
    let ceilings = FnoCeilings::default();
    let users = match user_ids {
        Some(u) => u,
        None => scan_users(conn).await?,
    };
    if users.is_empty() {
        return Ok(object([
            ("trade_date", s(iso(trade_date))),
            (
                "skipped",
                s("no user has fo_sleeve_config rows (fno_cli seed)"),
            ),
        ]));
    }
    let market = load_market(conn, trade_date, &config).await?;
    let mut per_user: Detail = Vec::new();
    for user_id in users {
        let book = load_user(conn, user_id, &market, &config, &ceilings).await?;
        let mut written: Detail = Vec::new();
        for underlying in &config.f1.underlyings {
            let row = f1_row(conn, &market, &book, underlying, &config, &ceilings).await?;
            let sleeve = f1_sleeve_for(underlying).map_err(fno_err)?;
            write_rows(
                conn,
                user_id,
                sleeve,
                trade_date,
                std::slice::from_ref(&row),
            )
            .await?;
            put(
                &mut written,
                sleeve.value(),
                object([(row.state.clone(), OrderedJson::Int(1))]),
            );
        }
        for underlying in &config.f3.underlyings {
            let row = super::f3_scan::f3_row(conn, &market, &book, underlying, &config, &ceilings)
                .await?;
            let sleeve = f3_sleeve_for(underlying).map_err(fno_err)?;
            write_rows(
                conn,
                user_id,
                sleeve,
                trade_date,
                std::slice::from_ref(&row),
            )
            .await?;
            put(
                &mut written,
                sleeve.value(),
                object([(row.state.clone(), OrderedJson::Int(1))]),
            );
        }
        let f2 = f2_rows(&market, &book, &config, &ceilings)?;
        write_rows(conn, user_id, FoSleeve::F2, trade_date, &f2).await?;
        let mut counts: Detail = Vec::new();
        for row in &f2 {
            let n = match counts.iter().find(|(k, _)| *k == row.state) {
                Some((_, OrderedJson::Int(n))) => n + 1,
                _ => 1,
            };
            put(&mut counts, &row.state, OrderedJson::Int(n));
        }
        put(
            &mut written,
            FoSleeve::F2.value(),
            OrderedJson::Object(counts),
        );
        put(
            &mut per_user,
            &user_id.to_string(),
            OrderedJson::Object(written),
        );
    }
    Ok(object([
        ("trade_date", s(iso(trade_date))),
        ("next_session", opt_iso(market.next_session)),
        (
            "ingest_status",
            market
                .ingest_status
                .clone()
                .map_or(OrderedJson::Null, OrderedJson::Str),
        ),
        ("users", OrderedJson::Object(per_user)),
    ]))
}
