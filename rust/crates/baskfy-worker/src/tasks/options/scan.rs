//! The once-a-minute scan (`options.scan`, OP4) and the loaders the plan builders share: every
//! input read from the database (no Kite call), `baskfy_core::options::scan` decides, one
//! `op_scan` row per sleeve upserted on `(user_id, sleeve, ts)`.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Instant;

use baskfy_core::error::CoreError;
use baskfy_core::options::bars::Bar;
use baskfy_core::options::calendar::Contract;
use baskfy_core::options::chain::{Level, OptionQuote};
use baskfy_core::options::config::{Mode, OptionType, OptionsCeilings, OptionsConfig, Sleeve};
use baskfy_core::options::gating::{OptionsFlags, options_gates};
use baskfy_core::options::risk::is_paused;
use baskfy_core::options::scan::{
    DayContext, MarketDay, MinuteKey, ScanResult, SleeveContext, Snapshot, SnapshotBook, scan_all,
    scan_minute, wanted_minutes,
};
use baskfy_core::options::session::SessionState;
use baskfy_core::options::structures::SleeveBook;
use baskfy_db::OrderedJson;
use chrono::{DateTime, FixedOffset, NaiveDate, Utc};
use rust_decimal::Decimal;
use serde::Serialize;
use sqlx::PgConnection;

use super::common::{count, in_ist, iso_dt, seconds_json};
use super::index_bars::{NIFTY_50, session_bounds};
use super::model::group_value;
use crate::error::WorkerError;
use crate::pyjson::{object, s, strings};
use crate::settings::WorkerSettings;

/// `index_bars.INDIA_VIX`.
const INDIA_VIX: &str = "INDIA VIX";
/// `scan.NIFTY_50_SLUG`.
const NIFTY_50_SLUG: &str = "nifty-50";
/// `scan.INDIA_VIX_SLUG`.
const INDIA_VIX_SLUG: &str = "india-vix";
/// `scan.DAILY_HISTORY`.
const DAILY_HISTORY: i64 = 120;

/// A core refusal as the Python exception it raises.
pub(crate) fn core(e: CoreError) -> WorkerError {
    match e {
        CoreError::InvalidInput(m) => WorkerError::python("ValueError", m),
        other => WorkerError::python("ArithmeticError", other.to_string()),
    }
}

/// A `Serialize` value as Python's `dict`/`list` (keys in serialisation order).
pub(crate) fn json_of<T: Serialize>(value: &T) -> Result<OrderedJson, WorkerError> {
    let text = serde_json::to_string(value)
        .map_err(|e| WorkerError::python("TypeError", e.to_string()))?;
    OrderedJson::parse(&text).map_err(|e| WorkerError::python("TypeError", e.to_string()))
}

/// `json.dumps` of a value bound for a `jsonb` column.
pub(crate) fn jsonb(value: &OrderedJson) -> Result<String, WorkerError> {
    value
        .to_python_json()
        .map_err(|e| WorkerError::python("TypeError", e.to_string()))
}

/// `Mode.value`.
pub(crate) const fn mode_value(mode: Mode) -> &'static str {
    match mode {
        Mode::Paper => "PAPER",
        Mode::Live => "LIVE",
    }
}

fn desk_true(value: &str) -> bool {
    value.trim().eq_ignore_ascii_case("true")
}

/// `options_gates(sleeve).mode` with this process's switches (`options.options_flags`).
pub(crate) fn mode_of(settings: &WorkerSettings, sleeve: Sleeve) -> Mode {
    let flags = OptionsFlags {
        dry_run: !settings
            .options_desk_dry_run
            .trim()
            .eq_ignore_ascii_case("false"),
        options_enabled: desk_true(&settings.options_desk_options_enabled),
        intraday_enabled: desk_true(&settings.options_desk_intraday_enabled),
        o1m_execution_enabled: settings.options_o1m_execution_enabled,
        o1w_execution_enabled: settings.options_o1w_execution_enabled,
        o2_execution_enabled: settings.options_o2_execution_enabled,
        o3_execution_enabled: settings.options_o3_execution_enabled,
    };
    options_gates(sleeve, &flags).mode
}

/// `Sleeve(value)`.
pub(crate) fn sleeve_of(value: &str) -> Result<Sleeve, WorkerError> {
    Sleeve::ALL
        .into_iter()
        .find(|s| s.value() == value)
        .ok_or_else(|| {
            WorkerError::python(
                "ValueError",
                format!(
                    "{} is not a valid Sleeve",
                    baskfy_providers::py::repr_str(value)
                ),
            )
        })
}

fn option_type_of(value: &str) -> Result<OptionType, WorkerError> {
    match value {
        "CE" => Ok(OptionType::Ce),
        "PE" => Ok(OptionType::Pe),
        other => Err(WorkerError::python(
            "ValueError",
            format!(
                "{} is not a valid OptionType",
                baskfy_providers::py::repr_str(other)
            ),
        )),
    }
}

fn session_state_of(value: &str) -> Result<SessionState, WorkerError> {
    [
        SessionState::Observing,
        SessionState::Skipped,
        SessionState::Planned,
        SessionState::Lapsed,
        SessionState::Confirmed,
        SessionState::Open,
        SessionState::Closed,
    ]
    .into_iter()
    .find(|s| s.value() == value)
    .ok_or_else(|| {
        WorkerError::python(
            "ValueError",
            format!(
                "{} is not a valid SessionState",
                baskfy_providers::py::repr_str(value)
            ),
        )
    })
}

/// A database `timestamptz` as asyncpg returns it: aware UTC.
fn utc(at: DateTime<Utc>) -> DateTime<FixedOffset> {
    at.fixed_offset()
}

// --- reads ----------------------------------------------------------------------------------

type ContractRow = (
    i64,
    String,
    String,
    NaiveDate,
    Decimal,
    String,
    i32,
    Decimal,
);

/// `master.load_contracts(session, underlying)` as the core's calendar contracts.
async fn load_contracts(
    conn: &mut PgConnection,
    underlying: &str,
) -> Result<Vec<Contract>, WorkerError> {
    let rows: Vec<ContractRow> = sqlx::query_as(
        "SELECT op_contract.instrument_token, op_contract.tradingsymbol, \
             op_contract.underlying, op_contract.expiry, op_contract.strike, \
             op_contract.option_type, op_contract.lot_size, op_contract.tick_size \
             FROM op_contract WHERE op_contract.underlying = $1 AND op_contract.expired IS false",
    )
    .bind(underlying)
    .fetch_all(&mut *conn)
    .await?;
    rows.into_iter()
        .map(
            |(token, symbol, underlying, expiry, strike, kind, lot, tick)| {
                Ok(Contract {
                    instrument_token: token,
                    tradingsymbol: symbol,
                    underlying,
                    expiry,
                    strike,
                    option_type: option_type_of(&kind)?,
                    lot_size: i64::from(lot),
                    tick_size: tick,
                })
            },
        )
        .collect()
}

/// `load_bars(session, symbol, day)`: the stored minutes of `day`'s session, oldest first.
async fn load_bars(
    conn: &mut PgConnection,
    symbol: &str,
    day: NaiveDate,
) -> Result<Vec<Bar>, WorkerError> {
    let (start, end) = session_bounds(day)?;
    let rows: Vec<(DateTime<Utc>, Decimal, Decimal, Decimal, Decimal)> = sqlx::query_as(
        "SELECT op_index_minute.ts, op_index_minute.open, op_index_minute.high, \
         op_index_minute.low, op_index_minute.close FROM op_index_minute \
         JOIN instrument ON instrument.id = op_index_minute.instrument_id \
         WHERE instrument.symbol = $1 AND op_index_minute.ts >= $2 AND op_index_minute.ts < $3 \
         ORDER BY op_index_minute.ts",
    )
    .bind(symbol)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(ts, open, high, low, close)| Bar {
            ts: utc(ts),
            open,
            high,
            low,
            close,
        })
        .collect())
}

/// `daily_levels(session, slug, before, limit)`: oldest first, the previous session last.
async fn daily_levels(
    conn: &mut PgConnection,
    slug: &str,
    before: NaiveDate,
    limit: i64,
) -> Result<Vec<Decimal>, WorkerError> {
    let rows: Vec<(Option<Decimal>,)> = sqlx::query_as(
        "SELECT index_snapshot_daily.level FROM index_snapshot_daily \
         JOIN index_def ON index_def.id = index_snapshot_daily.index_id \
         WHERE index_def.slug = $1 AND index_snapshot_daily.date < $2 \
         AND index_snapshot_daily.level IS NOT NULL \
         ORDER BY index_snapshot_daily.date DESC LIMIT $3",
    )
    .bind(slug)
    .bind(before)
    .bind(limit)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().rev().filter_map(|(l,)| l).collect())
}

/// `vix_previous_close(session, day)`: the daily level, else the last stored minute before
/// today's open.
async fn vix_previous_close(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<Option<Decimal>, WorkerError> {
    if let Some(last) = daily_levels(conn, INDIA_VIX_SLUG, day, 1).await?.pop() {
        return Ok(Some(last));
    }
    let (start, _) = session_bounds(day)?;
    let row: Option<(Decimal,)> = sqlx::query_as(
        "SELECT op_index_minute.close FROM op_index_minute \
         JOIN instrument ON instrument.id = op_index_minute.instrument_id \
         WHERE instrument.symbol = $1 AND op_index_minute.ts < $2 \
         ORDER BY op_index_minute.ts DESC LIMIT 1",
    )
    .bind(INDIA_VIX)
    .bind(start)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|(c,)| c))
}

fn json_decimal(value: &OrderedJson) -> Option<Decimal> {
    match value {
        OrderedJson::Str(t) => t.parse().ok().or_else(|| Decimal::from_scientific(t).ok()),
        OrderedJson::Int(i) => Some(Decimal::from(*i)),
        OrderedJson::Float(f) => baskfy_core::options::num::from_float(*f).ok(),
        _ => None,
    }
}

/// `scan._levels(raw)`: the well-formed `{"price", "quantity"}` items of a depth side.
fn levels(raw: Option<&OrderedJson>) -> Vec<Level> {
    let Some(OrderedJson::Array(items)) = raw else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|item| {
            let price = item.get("price")?;
            let quantity = match item.get("quantity")? {
                OrderedJson::Int(q) => *q,
                OrderedJson::Bool(b) => i64::from(*b),
                _ => return None,
            };
            if !matches!(
                price,
                OrderedJson::Str(_) | OrderedJson::Int(_) | OrderedJson::Float(_)
            ) {
                return None;
            }
            Some(Level {
                price: json_decimal(price)?,
                quantity,
            })
        })
        .collect()
}

type SnapshotRow = (
    i64,
    NaiveDate,
    Decimal,
    String,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<String>,
    Option<i64>,
    Option<i64>,
);

/// `load_snapshot(session, at, now, day)`: the first stored minute at or after `at` (not after
/// `now`'s minute), or the latest of today for `at = None`.
pub(crate) async fn load_snapshot(
    conn: &mut PgConnection,
    at: MinuteKey,
    now: DateTime<FixedOffset>,
    day: NaiveDate,
) -> Result<Option<Snapshot>, WorkerError> {
    let (start, _) = session_bounds(day)?;
    let upper = scan_minute(&now);
    let pick = if at.is_some() { "min" } else { "max" };
    let minute: Option<DateTime<Utc>> = sqlx::query_scalar(&format!(
        "SELECT {pick}(op_chain_snapshot.ts) FROM op_chain_snapshot \
         WHERE op_chain_snapshot.ts >= $1 AND op_chain_snapshot.ts <= $2"
    ))
    .bind(at.unwrap_or(start))
    .bind(upper)
    .fetch_one(&mut *conn)
    .await?;
    let Some(minute) = minute else {
        return Ok(None);
    };
    let rows: Vec<SnapshotRow> = sqlx::query_as(
        "SELECT instrument_token, expiry, strike, option_type, spot, bid, ask, last, \
         depth_json::text, oi, volume FROM op_chain_snapshot WHERE op_chain_snapshot.ts = $1",
    )
    .bind(minute)
    .fetch_all(&mut *conn)
    .await?;
    let ts = utc(minute);
    let spot = rows.iter().find_map(|r| r.4);
    let mut quotes = Vec::with_capacity(rows.len());
    for (token, expiry, strike, kind, _, bid, ask, last, depth, oi, volume) in rows {
        let depth = match depth {
            Some(text) => OrderedJson::parse(&text)
                .map_err(|e| WorkerError::python("ValueError", e.to_string()))?,
            None => OrderedJson::Object(Vec::new()),
        };
        quotes.push(OptionQuote {
            instrument_token: token,
            expiry,
            strike,
            option_type: option_type_of(&kind)?,
            bid,
            ask,
            bids: levels(depth.get("buy")),
            asks: levels(depth.get("sell")),
            oi: oi.unwrap_or(0),
            ts,
            last,
            volume: volume.unwrap_or(0),
        });
    }
    Ok(Some(Snapshot { ts, spot, quotes }))
}

/// `load_market(session, day, trading_day, config)`.
pub(crate) async fn load_market(
    conn: &mut PgConnection,
    day: NaiveDate,
    trading_day: bool,
    config: &OptionsConfig,
) -> Result<MarketDay, WorkerError> {
    Ok(MarketDay {
        trade_date: day,
        trading_day,
        contracts: load_contracts(conn, &config.calendar.underlying).await?,
        bars: load_bars(conn, NIFTY_50, day).await?,
        daily_closes: daily_levels(conn, NIFTY_50_SLUG, day, DAILY_HISTORY).await?,
        vix_prev_close: vix_previous_close(conn, day).await?,
    })
}

type SleeveConfigRow = (String, Decimal, Decimal, i16, Option<NaiveDate>);

/// `load_context(session, user_id, day, mode_of)`: event days, books, pauses, today's sessions
/// and the real journal counts.
pub(crate) async fn load_context(
    conn: &mut PgConnection,
    user_id: i64,
    day: NaiveDate,
    settings: &WorkerSettings,
) -> Result<DayContext, WorkerError> {
    let mut events: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT op_event_day.date FROM op_event_day WHERE op_event_day.user_id = $1",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    events.sort_unstable();
    events.dedup();
    let book: Option<(Option<NaiveDate>,)> = sqlx::query_as(
        "SELECT op_book_config.paused_until FROM op_book_config WHERE op_book_config.user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await?;
    let book_paused = is_paused(book.and_then(|(p,)| p), day);
    let configs: Vec<SleeveConfigRow> = sqlx::query_as(
        "SELECT sleeve, sleeve_capital_inr, risk_per_trade_pct, max_lots, paused_until \
         FROM op_sleeve_config WHERE op_sleeve_config.user_id = $1",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    let configs: BTreeMap<String, SleeveConfigRow> =
        configs.into_iter().map(|r| (r.0.clone(), r)).collect();
    let sessions: Vec<(String, String, Option<String>)> = sqlx::query_as(
        "SELECT sleeve::text, state, slot_holder FROM op_session \
         WHERE op_session.user_id = $1 AND op_session.trade_date = $2",
    )
    .bind(user_id)
    .bind(day)
    .fetch_all(&mut *conn)
    .await?;
    let counted: Vec<(String, i64)> = sqlx::query_as(
        "SELECT op_journal.sleeve::text, count(*) FROM op_journal \
         WHERE op_journal.user_id = $1 AND op_journal.simulated IS false \
         GROUP BY op_journal.sleeve",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    let mut real: BTreeMap<&'static str, i64> = BTreeMap::new();
    for (code, n) in counted {
        *real.entry(group_value(sleeve_of(&code)?)).or_default() += n;
    }
    let mut by_sleeve: BTreeMap<String, (String, Option<String>)> = BTreeMap::new();
    let mut holder = None;
    for (sleeve, state, slot) in sessions {
        if holder.is_none()
            && let Some(slot) = &slot
        {
            holder = Some(sleeve_of(slot)?);
        }
        by_sleeve.insert(sleeve, (state, slot));
    }
    let mut sleeves = BTreeMap::new();
    for sleeve in Sleeve::ALL {
        let group = group_value(sleeve);
        let row = configs.get(group);
        let real_rows = real.get(group).copied().unwrap_or(0);
        let mode = mode_of(settings, sleeve);
        let book = match row {
            None => SleeveBook {
                mode,
                real_journal_rows: real_rows,
                ..SleeveBook::default()
            },
            Some((_, capital, pct, lots, _)) => SleeveBook {
                mode,
                sleeve_capital_inr: *capital,
                risk_per_trade_pct: Some(*pct),
                max_lots: Some(i64::from(*lots)),
                real_journal_rows: real_rows,
            },
        };
        let session_state = match by_sleeve.get(sleeve.value()) {
            Some((state, _)) => Some(session_state_of(state)?),
            None => None,
        };
        sleeves.insert(
            sleeve,
            SleeveContext {
                book,
                paused: book_paused || is_paused(row.and_then(|r| r.4), day),
                session_state,
            },
        );
    }
    Ok(DayContext {
        event_days: events,
        slot_holder: holder,
        sleeves,
    })
}

/// Loads every minute `wanted` names, in the order given.
pub(crate) async fn load_snapshots(
    conn: &mut PgConnection,
    wanted: impl IntoIterator<Item = MinuteKey>,
    now: DateTime<FixedOffset>,
    day: NaiveDate,
) -> Result<Vec<(MinuteKey, Option<Snapshot>)>, WorkerError> {
    let mut out = Vec::new();
    for minute in wanted {
        out.push((minute, load_snapshot(conn, minute, now, day).await?));
    }
    Ok(out)
}

/// A book over the loaded minutes.
pub(crate) fn book_of(loaded: &[(MinuteKey, Option<Snapshot>)]) -> SnapshotBook {
    SnapshotBook::new(loaded.iter().cloned().collect())
}

// --- writes ---------------------------------------------------------------------------------

/// `write_rows`: one upsert per row on `(user_id, sleeve, ts)`.
async fn write_row(
    conn: &mut PgConnection,
    result: &ScanResult,
    user_id: i64,
) -> Result<(), WorkerError> {
    let candidates = json_of(&result.candidates_json().map_err(core)?)?;
    sqlx::query(
        "INSERT INTO op_scan (user_id, sleeve, trade_date, ts, state, reasons, numbers, \
         candidates, as_of_minute, stale) \
         VALUES ($1, $2::op_sleeve, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10) \
         ON CONFLICT ON CONSTRAINT uq_op_scan_user_sleeve_ts DO UPDATE SET \
         trade_date = excluded.trade_date, state = excluded.state, reasons = excluded.reasons, \
         numbers = excluded.numbers, candidates = excluded.candidates, \
         as_of_minute = excluded.as_of_minute, stale = excluded.stale",
    )
    .bind(user_id)
    .bind(result.sleeve.value())
    .bind(result.trade_date)
    .bind(result.ts)
    .bind(result.state.value())
    .bind(&result.reasons)
    .bind(jsonb(&json_of(&result.numbers)?)?)
    .bind(jsonb(&candidates)?)
    .bind(result.as_of_minute)
    .bind(result.stale)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

fn stamp(snapshot: Option<&Snapshot>) -> String {
    snapshot.map_or_else(|| "none".to_owned(), |s| iso_dt(s.ts))
}

/// `scan_minute_for(session, user_id, now, trading_day=...)` → `ScanReport.as_dict()`.
pub(crate) async fn scan_minute_for(
    conn: &mut PgConnection,
    user_id: i64,
    now: DateTime<FixedOffset>,
    trading_day: bool,
    settings: &WorkerSettings,
) -> Result<OrderedJson, WorkerError> {
    let started = Instant::now();
    let cfg = OptionsConfig::default();
    let ceil = OptionsCeilings::default();
    let day = in_ist(now).date_naive();
    let market = load_market(conn, day, trading_day, &cfg).await?;
    let context = load_context(conn, user_id, day, settings).await?;
    let wanted: BTreeSet<MinuteKey> =
        wanted_minutes(&market, &context, &now, &cfg, &ceil).map_err(core)?;
    let mut ordered: Vec<MinuteKey> = wanted.iter().copied().filter(Option::is_some).collect();
    if wanted.contains(&None) {
        ordered.push(None);
    }
    let loaded = load_snapshots(conn, ordered, now, day).await?;
    let snapshots: Vec<String> = loaded
        .iter()
        .map(|(minute, snap)| {
            let asked = minute.map_or_else(|| "latest".to_owned(), iso_dt);
            format!("{asked} -> {}", stamp(snap.as_ref()))
        })
        .collect();
    let mut book = book_of(&loaded);
    let results = scan_all(&market, &context, &now, &mut book, &cfg, &ceil).map_err(core)?;
    for result in &results {
        write_row(conn, result, user_id).await?;
    }
    let states = object(
        results
            .iter()
            .map(|r| (r.sleeve.value().to_owned(), s(r.state.value()))),
    );
    Ok(object([
        ("ts", s(iso_dt(scan_minute(&now)))),
        ("user_id", OrderedJson::Int(user_id)),
        ("written", count(results.len())),
        ("states", states),
        ("snapshots", strings(snapshots)),
        ("skipped", OrderedJson::Null),
        ("seconds", seconds_json(started.elapsed().as_secs_f64())),
    ]))
}
