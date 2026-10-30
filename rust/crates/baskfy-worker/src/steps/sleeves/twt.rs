//! TW4 — the nightly three-weeks-tight detection job. Ports `baskfy_worker.tasks.twt` as the
//! nightly reaches it (`run_detect_twt` with no `extra_bars`, `provisional=False`).
//!
//! The arithmetic is `baskfy_core::twt` (calendar, indicators, weekly closes, the month-3 low,
//! the state, the entry event, the breadth); this module reads rows, hands them over, and writes
//! `tw_state_daily`, `tw_signal_daily` and `tw_breadth_daily` — upserts, never a delete first
//! (`tw_order.signal_date` is a foreign key into the signal rows) — and then does the book's
//! evening arithmetic: Qullamaggie's management (LV9), which writes **only** the queue columns
//! and the trail on `tw_position`. **Law 2:** `stop_price`, `gtt_trigger` and `quantity_open`
//! are never written; nothing is armed, cancelled or placed.

use std::collections::BTreeMap;

use baskfy_core::decimal::WideDecimal;
use baskfy_core::twt::breadth::{BreadthReading, breadth_above_dma};
use baskfy_core::twt::calendar::drop_thin_sessions;
use baskfy_core::twt::config::{Gate, SignalState, TICK_INR, TwtConfig};
use baskfy_core::twt::exits::initial_stop;
use baskfy_core::twt::signals::{detect_signals, weekly_column, with_twt_columns};
use baskfy_db::OrderedJson;
use baskfy_db::row::{date as row_date, decimal as row_decimal, opt_decimal};
use chrono::NaiveDate;
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use super::common::{
    self, Num, UniverseRule, core_err, day_number, load_raw_bars, lookback_start, pl_err,
    store_float, value_err,
};
use super::precision;
use super::qulla::{self, MA_TRAIL, OhlcBar, PARTIAL};
use super::vbt::{decimal_to_f64, to_i32};
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::{StepOutcome, StepStatus};

/// `TURNOVER_FILTER`: the one filter that can reject an entry event.
const TURNOVER_FILTER: &str = "TURNOVER";
/// `QULLA_LOOKBACK_SESSIONS`.
const QULLA_LOOKBACK_SESSIONS: i64 = 45;

/// `TwtFunnel`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct TwtFunnel {
    instruments: i64,
    bars: i64,
    with_a_bar: i64,
    with_a_dma: i64,
    above_the_dma: i64,
    in_state: i64,
    entries: i64,
    signals: i64,
    dropped_thin_sessions: i64,
}

impl TwtFunnel {
    fn as_detail(&self) -> Vec<(String, OrderedJson)> {
        [
            ("instruments", self.instruments),
            ("bars", self.bars),
            ("with_a_bar", self.with_a_bar),
            ("with_a_dma", self.with_a_dma),
            ("above_the_dma", self.above_the_dma),
            ("in_state", self.in_state),
            ("entries", self.entries),
            ("signals", self.signals),
            ("dropped_thin_sessions", self.dropped_thin_sessions),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), pyjson::int(v)))
        .collect()
    }
}

/// `QullaReport`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
struct QullaReport {
    positions: i64,
    partials: i64,
    exits: i64,
    raises: i64,
    stop_hits: i64,
    without_a_bar: i64,
}

impl QullaReport {
    fn as_detail(&self) -> Vec<(String, OrderedJson)> {
        [
            ("managed_positions", self.positions),
            ("partials_queued", self.partials),
            ("exits_queued", self.exits),
            ("breakeven_raises", self.raises),
            ("stop_hits_seen", self.stop_hits),
            ("positions_without_a_bar", self.without_a_bar),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), pyjson::int(v)))
        .collect()
    }
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `load_twt_config`: the defaults with the user's two detector settings (`stop_pct`,
/// `trail_pct`) as `Decimal(str(...))`.
async fn load_twt_config(conn: &mut PgConnection, user_id: i64) -> Result<TwtConfig, WorkerError> {
    let row = sqlx::query("SELECT stop_pct, trail_pct FROM tw_config WHERE user_id = $1")
        .bind(user_id)
        .fetch_optional(&mut *conn)
        .await?;
    let mut config = TwtConfig::default();
    if let Some(row) = row {
        config.exits.stop_pct = row_decimal(&row, "stop_pct")?;
        config.exits.trail_pct = row_decimal(&row, "trail_pct")?;
    }
    Ok(config)
}

/// `with_storage_columns`: `sessions_in_state` (sessions since the state was last false, this
/// one included) and `bars_in_window` (non-null closes in the trailing `dma_bars` rows), per
/// instrument over the frame sorted by `(instrument_id, date)`.
fn with_storage_columns(
    detected: &DataFrame,
    config: &TwtConfig,
) -> Result<DataFrame, WorkerError> {
    let ids = common::ints(detected, "instrument_id")?;
    let days = common::dates(detected, "date")?;
    let mut order: Vec<usize> = (0..detected.height()).collect();
    // polars' sort: nulls first in each key.
    order.sort_by(|&a, &b| ids[a].cmp(&ids[b]).then_with(|| days[a].cmp(&days[b])));
    let idx = polars::prelude::IdxCa::from_vec(
        "".into(),
        order
            .iter()
            .map(|&i| {
                polars::prelude::IdxSize::try_from(i).unwrap_or(polars::prelude::IdxSize::MAX)
            })
            .collect(),
    );
    let mut frame = detected.take(&idx).map_err(|e| pl_err(&e))?;
    let ids = common::ints(&frame, "instrument_id")?;
    let tight = common::bools(&frame, "tight_state")?;
    let close = common::floats(&frame, "close")?;
    let window = usize::try_from(config.breadth.dma_bars).unwrap_or(0);
    let n = frame.height();
    let mut in_state = vec![0_i64; n];
    let mut in_window = vec![0_i64; n];
    let mut start = 0;
    while start < n {
        let mut end = start + 1;
        while end < n && ids[end] == ids[start] {
            end += 1;
        }
        let mut last_false: i64 = -1;
        let mut running = 0_i64;
        for (k, i) in (start..end).enumerate() {
            let position = len_i64(k);
            // `when(~tight_state).then(position)`: a null state is not a false one.
            if tight[i] == Some(false) {
                last_false = position;
            }
            in_state[i] = position - last_false;
            running += i64::from(close[i].is_some());
            if k >= window && window > 0 {
                running -= i64::from(close[start + k - window].is_some());
            }
            in_window[i] = running;
        }
        start = end;
    }
    frame
        .with_column(Series::new("sessions_in_state".into(), in_state).into_column())
        .map_err(|e| pl_err(&e))?;
    frame
        .with_column(Series::new("bars_in_window".into(), in_window).into_column())
        .map_err(|e| pl_err(&e))?;
    Ok(frame)
}

/// One `tw_state_daily` row.
struct StateRow {
    instrument_id: i64,
    open: Option<Decimal>,
    high: Option<Decimal>,
    low: Option<Decimal>,
    close: Option<Decimal>,
    close_raw: Option<Decimal>,
    adj_factor: Option<Decimal>,
    week_close_0: Option<Decimal>,
    week_close_1: Option<Decimal>,
    week_close_2: Option<Decimal>,
    week_range_pct: Option<Decimal>,
    month_low_3: Option<Decimal>,
    month_low_ratio: Option<Decimal>,
    vol_sma_50: Option<i64>,
    volume: Option<i64>,
    turnover_inr: Option<i64>,
    turnover_avg_20: Option<i64>,
    sma_dma: Option<Decimal>,
    sessions_in_state: Option<i64>,
    bars_in_window: Option<i64>,
    locked_upper_circuit: bool,
}

/// `state_rows` + `upsert_states`' payload: the names holding the state on `as_of`, adjusted
/// series and comparands, storage precision applied.
fn state_rows(detected: &DataFrame, as_of: NaiveDate) -> Result<Vec<StateRow>, WorkerError> {
    let day = day_number(as_of);
    let days = common::dates(detected, "date")?;
    let tight = common::bools(detected, "tight_state")?;
    let held: Vec<usize> = (0..detected.height())
        .filter(|&i| days[i] == Some(day) && tight[i] == Some(true))
        .collect();
    if held.is_empty() {
        return Ok(Vec::new());
    }
    let take = |name: &str, dest: &str| -> Result<Vec<Num>, WorkerError> {
        let all = common::stored(detected, name, dest)?;
        Ok(held.iter().map(|&i| all[i]).collect())
    };
    let ids = common::ints(detected, "instrument_id")?;
    let close_f = common::floats(detected, "close")?;
    let low_back = common::floats(detected, "month_low_back")?;
    let high_f = common::floats(detected, "high")?;
    let circuit = common::floats(detected, "upper_circuit")?;

    let open = take("open", "open")?;
    let high = take("high", "high")?;
    let low = take("low", "low")?;
    let close = take("close", "close")?;
    let close_raw = take("close_raw", "close_raw")?;
    let adj = take("adj_factor", "adj_factor")?;
    let w0 = take(&weekly_column(0), "week_close_0")?;
    let w1 = take(&weekly_column(1), "week_close_1")?;
    let w2 = take(&weekly_column(2), "week_close_2")?;
    let range = take("tight_range_pct", "week_range_pct")?;
    let month_low = take("month_low_back", "month_low_3")?;
    let vol_sma = take("vol_sma", "vol_sma_50")?;
    let volume = take("volume", "volume")?;
    let turnover = take("turnover_inr", "turnover_inr")?;
    let turnover_avg = take("turnover_avg_20", "turnover_avg_20")?;
    let sma = take("sma_dma", "sma_dma")?;
    let in_state = take("sessions_in_state", "sessions_in_state")?;
    let in_window = take("bars_in_window", "bars_in_window")?;

    let mut rows = Vec::with_capacity(held.len());
    for (k, &i) in held.iter().enumerate() {
        // `close / month_low_back`, before precision (polars float division; null in, null out).
        let ratio = match (close_f[i], low_back[i]) {
            (Some(c), Some(m)) => Some(c / m),
            _ => None,
        };
        let ratio = store_float("month_low_ratio", ratio);
        // `circuit > 0 & high >= circuit`, null read as false.
        let locked = match (circuit[i], high_f[i]) {
            (Some(c), Some(h)) => baskfy_core::pl::gt(c, 0.0) && baskfy_core::pl::ge(h, c),
            _ => false,
        };
        rows.push(StateRow {
            instrument_id: ids[i].ok_or_else(|| value_err("a state row without an instrument"))?,
            open: open[k].decimal()?,
            high: high[k].decimal()?,
            low: low[k].decimal()?,
            close: close[k].decimal()?,
            close_raw: close_raw[k].decimal()?,
            adj_factor: adj[k].decimal()?,
            week_close_0: w0[k].decimal()?,
            week_close_1: w1[k].decimal()?,
            week_close_2: w2[k].decimal()?,
            week_range_pct: range[k].decimal()?,
            month_low_3: month_low[k].decimal()?,
            month_low_ratio: ratio.decimal()?,
            vol_sma_50: vol_sma[k].int()?,
            volume: volume[k].int()?,
            turnover_inr: turnover[k].int()?,
            turnover_avg_20: turnover_avg[k].int()?,
            sma_dma: sma[k].decimal()?,
            sessions_in_state: in_state[k].int()?,
            bars_in_window: in_window[k].int()?,
            locked_upper_circuit: locked,
        });
    }
    Ok(rows)
}

/// One `tw_signal_daily` row.
struct SignalRow {
    instrument_id: i64,
    state: String,
    failed_filters: Vec<String>,
    entry_reference_close: Option<Decimal>,
    stop_preview: Option<Decimal>,
    sessions_out_before: Option<i64>,
    rank_key: i64,
    turnover_avg_20: Option<i64>,
}

/// `signal_rows`: the session's entry events, `SIGNAL` and `SCAN_ONLY`, with the reference
/// close and the stop preview in exchange prices.
fn signal_rows(
    detected: &DataFrame,
    as_of: NaiveDate,
    config: &TwtConfig,
) -> Result<Vec<SignalRow>, WorkerError> {
    let events = detect_signals(detected, Some(as_of), config, None).map_err(core_err)?;
    let n = events.height();
    if n == 0 {
        return Ok(Vec::new());
    }
    let tick: Decimal = TICK_INR
        .parse()
        .map_err(|_| value_err("TICK_INR does not parse"))?;
    let ids = common::ints(&events, "instrument_id")?;
    let factors = common::floats(&events, "adj_factor")?;
    let closes = common::floats(&events, "close")?;
    let out_before = common::raw(&events, "sessions_out_before")?;
    let listed = common::raw(&events, "sessions_listed")?;
    let states = common::strings(&events, "signal_state")?;
    let rank = common::stored(&events, "rank_key", "rank_key")?;
    let turnover_avg = common::stored(&events, "turnover_avg_20", "turnover_avg_20")?;
    let mut rows = Vec::with_capacity(n);
    for i in 0..n {
        let factor = match factors[i] {
            Some(f) if f != 0.0 => f,
            _ => 1.0,
        };
        let close = closes[i]
            .ok_or_else(|| WorkerError::python("TypeError", "float() argument must be a number"))?;
        let reference = close / factor;
        let stop = initial_stop(
            Num::F(reference).decimal()?.unwrap_or_default(),
            &config.exits,
            tick,
        )
        .map_err(core_err)?;
        // `int(out_before if out_before is not None else (sessions_listed or 1) - 1)`.
        let sessions_out = match out_before[i] {
            Num::Null => {
                let listed = match listed[i] {
                    Num::Null => 1,
                    other => {
                        let v = other.int()?.unwrap_or(1);
                        if v == 0 { 1 } else { v }
                    }
                };
                listed - 1
            }
            other => other.int()?.unwrap_or(0),
        };
        let state = states[i].clone().unwrap_or_else(|| "None".to_owned());
        let failed = if state == SignalState::Signal.value() {
            Vec::new()
        } else {
            vec![TURNOVER_FILTER.to_owned()]
        };
        rows.push(SignalRow {
            instrument_id: ids[i].ok_or_else(|| value_err("a signal row without an instrument"))?,
            state,
            failed_filters: failed,
            entry_reference_close: store_float("entry_reference_close", Some(reference))
                .decimal()?,
            stop_preview: store_float("stop_preview", Some(decimal_to_f64(stop)?)).decimal()?,
            sessions_out_before: Some(sessions_out),
            rank_key: rank[i].int()?.unwrap_or(0),
            turnover_avg_20: turnover_avg[i].int()?,
        });
    }
    Ok(rows)
}

/// `_drop_provisional_stragglers`: a live scan's rows the real bar did not reproduce, gone —
/// except a signal row an order references.
async fn drop_provisional_stragglers(
    conn: &mut PgConnection,
    table: &str,
    keep: &[i64],
    user_id: i64,
    trade_date: NaiveDate,
) -> Result<(), WorkerError> {
    let mut sql = format!(
        "DELETE FROM {table} WHERE user_id = $1 AND date = $2 AND provisional IS true \
         AND instrument_id <> ALL($3)"
    );
    if table == "tw_signal_daily" {
        sql.push_str(
            " AND instrument_id NOT IN (SELECT tw_order.instrument_id FROM tw_order \
             WHERE tw_order.user_id = $1 AND tw_order.signal_date = $2)",
        );
    }
    sqlx::query(&sql)
        .bind(user_id)
        .bind(trade_date)
        .bind(keep)
        .execute(&mut *conn)
        .await?;
    Ok(())
}

async fn upsert_states(
    conn: &mut PgConnection,
    rows: &[StateRow],
    user_id: i64,
    trade_date: NaiveDate,
    pipeline_run_id: i64,
) -> Result<i64, WorkerError> {
    let keep: Vec<i64> = rows.iter().map(|r| r.instrument_id).collect();
    drop_provisional_stragglers(conn, "tw_state_daily", &keep, user_id, trade_date).await?;
    for r in rows {
        sqlx::query(
            "INSERT INTO tw_state_daily (user_id, date, instrument_id, provisional, open, high, \
             low, close, close_raw, adj_factor, week_close_0, week_close_1, week_close_2, \
             week_range_pct, month_low_3, month_low_ratio, vol_sma_50, volume, turnover_inr, \
             turnover_avg_20, sma_dma, sessions_in_state, bars_in_window, locked_upper_circuit, \
             pipeline_run_id) VALUES ($1, $2, $3, false, $4, $5, $6, $7, $8, $9, $10, $11, $12, \
             $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24) \
             ON CONFLICT (user_id, date, instrument_id) DO UPDATE SET \
             provisional = excluded.provisional, open = excluded.open, high = excluded.high, \
             low = excluded.low, close = excluded.close, close_raw = excluded.close_raw, \
             adj_factor = excluded.adj_factor, week_close_0 = excluded.week_close_0, \
             week_close_1 = excluded.week_close_1, week_close_2 = excluded.week_close_2, \
             week_range_pct = excluded.week_range_pct, month_low_3 = excluded.month_low_3, \
             month_low_ratio = excluded.month_low_ratio, vol_sma_50 = excluded.vol_sma_50, \
             volume = excluded.volume, turnover_inr = excluded.turnover_inr, \
             turnover_avg_20 = excluded.turnover_avg_20, sma_dma = excluded.sma_dma, \
             sessions_in_state = excluded.sessions_in_state, \
             bars_in_window = excluded.bars_in_window, \
             locked_upper_circuit = excluded.locked_upper_circuit, \
             pipeline_run_id = excluded.pipeline_run_id",
        )
        .bind(user_id)
        .bind(trade_date)
        .bind(r.instrument_id)
        .bind(r.open)
        .bind(r.high)
        .bind(r.low)
        .bind(r.close)
        .bind(r.close_raw)
        .bind(r.adj_factor)
        .bind(r.week_close_0)
        .bind(r.week_close_1)
        .bind(r.week_close_2)
        .bind(r.week_range_pct)
        .bind(r.month_low_3)
        .bind(r.month_low_ratio)
        .bind(r.vol_sma_50)
        .bind(r.volume)
        .bind(r.turnover_inr)
        .bind(r.turnover_avg_20)
        .bind(r.sma_dma)
        .bind(small(r.sessions_in_state)?)
        .bind(small(r.bars_in_window)?)
        .bind(r.locked_upper_circuit)
        .bind(pipeline_run_id)
        .execute(&mut *conn)
        .await?;
    }
    Ok(len_i64(rows.len()))
}

/// A `smallint` bind; Python would fail at the database on an out-of-range value.
fn small(v: Option<i64>) -> Result<Option<i16>, WorkerError> {
    v.map(|x| {
        i16::try_from(x).map_err(|_| {
            WorkerError::python("DataError", format!("value {x} out of range for smallint"))
        })
    })
    .transpose()
}

async fn upsert_signals(
    conn: &mut PgConnection,
    rows: &[SignalRow],
    user_id: i64,
    trade_date: NaiveDate,
    pipeline_run_id: i64,
) -> Result<i64, WorkerError> {
    let keep: Vec<i64> = rows.iter().map(|r| r.instrument_id).collect();
    drop_provisional_stragglers(conn, "tw_signal_daily", &keep, user_id, trade_date).await?;
    for r in rows {
        sqlx::query(
            "INSERT INTO tw_signal_daily (user_id, date, instrument_id, provisional, state, \
             failed_filters, entry_reference_close, stop_preview, sessions_out_before, \
             rank_key, turnover_avg_20, pipeline_run_id) VALUES ($1, $2, $3, false, $4, $5, \
             $6, $7, $8, $9, $10, $11) ON CONFLICT (user_id, date, instrument_id) DO UPDATE SET \
             provisional = excluded.provisional, state = excluded.state, \
             failed_filters = excluded.failed_filters, \
             entry_reference_close = excluded.entry_reference_close, \
             stop_preview = excluded.stop_preview, \
             sessions_out_before = excluded.sessions_out_before, \
             rank_key = excluded.rank_key, turnover_avg_20 = excluded.turnover_avg_20, \
             pipeline_run_id = excluded.pipeline_run_id",
        )
        .bind(user_id)
        .bind(trade_date)
        .bind(r.instrument_id)
        .bind(&r.state)
        .bind(&r.failed_filters)
        .bind(r.entry_reference_close)
        .bind(r.stop_preview)
        .bind(small(r.sessions_out_before)?)
        .bind(r.rank_key)
        .bind(r.turnover_avg_20)
        .bind(pipeline_run_id)
        .execute(&mut *conn)
        .await?;
    }
    Ok(len_i64(rows.len()))
}

/// `write_breadth_row`: a thin session's percentage is NULL; an unmeasured one `0.0000`.
#[allow(clippy::too_many_arguments)]
async fn write_breadth_row(
    conn: &mut PgConnection,
    user_id: i64,
    trade_date: NaiveDate,
    reading: Option<&BreadthReading>,
    funnel: &TwtFunnel,
    thin_session: bool,
    config: &TwtConfig,
    pipeline_run_id: i64,
) -> Result<(), WorkerError> {
    let detail = OrderedJson::Object(funnel.as_detail())
        .to_python_json()
        .map_err(|e| value_err(e.to_string()))?;
    let pct = if thin_session {
        None
    } else {
        Some(reading.map_or(Decimal::new(0, 4), |r| r.pct_above_dma))
    };
    let gate = reading.map_or(Gate::Shut.value(), |r| r.gate.value());
    let dma_bars = i16::try_from(config.breadth.dma_bars).map_err(|e| value_err(e.to_string()))?;
    sqlx::query(
        "INSERT INTO tw_breadth_daily (user_id, date, universe_count, measured_count, \
         above_count, pct_above_dma, gate, dma_bars, thin_session, provisional, detail, \
         pipeline_run_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, false, \
         CAST($10 AS JSONB), $11) ON CONFLICT (user_id, date) DO UPDATE SET \
         universe_count = excluded.universe_count, measured_count = excluded.measured_count, \
         above_count = excluded.above_count, pct_above_dma = excluded.pct_above_dma, \
         gate = excluded.gate, dma_bars = excluded.dma_bars, \
         thin_session = excluded.thin_session, provisional = excluded.provisional, \
         detail = excluded.detail, pipeline_run_id = excluded.pipeline_run_id",
    )
    .bind(user_id)
    .bind(trade_date)
    .bind(to_i32(funnel.with_a_bar)?)
    .bind(to_i32(reading.map_or(0, |r| r.measured_count))?)
    .bind(to_i32(reading.map_or(0, |r| r.above_count))?)
    .bind(pct)
    .bind(gate)
    .bind(dma_bars)
    .bind(thin_session)
    .bind(detail)
    .bind(pipeline_run_id)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// `_exchange`: `value / factor if factor else value`, in the decimal context.
fn exchange(value: &WideDecimal, factor: &WideDecimal) -> Result<WideDecimal, WorkerError> {
    if factor.is_zero() {
        return Ok(value.clone());
    }
    value
        .div(factor)
        .map_err(|e| WorkerError::python("InvalidOperation", e.to_string()))
}

/// `_exchange_series`: each held name's bars in exchange prices, oldest first.
async fn exchange_series(
    conn: &mut PgConnection,
    ids: &[i64],
    start: NaiveDate,
    end: NaiveDate,
) -> Result<BTreeMap<i64, Vec<OhlcBar>>, WorkerError> {
    let mut out: BTreeMap<i64, Vec<OhlcBar>> = BTreeMap::new();
    if ids.is_empty() {
        return Ok(out);
    }
    let rows = sqlx::query(
        "SELECT instrument_id, date, open, high, low, close, close_raw, adj_factor \
         FROM ohlcv_daily WHERE instrument_id = ANY($1) AND date >= $2 AND date <= $3 \
         ORDER BY instrument_id, date",
    )
    .bind(ids)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    for row in rows {
        let id: i64 = row.try_get("instrument_id")?;
        let factor = WideDecimal::from(row_decimal(&row, "adj_factor")?);
        let open = WideDecimal::from(row_decimal(&row, "open")?);
        let high = WideDecimal::from(row_decimal(&row, "high")?);
        let low = WideDecimal::from(row_decimal(&row, "low")?);
        let close = match opt_decimal(&row, "close_raw")? {
            Some(raw) => WideDecimal::from(raw),
            None => exchange(&WideDecimal::from(row_decimal(&row, "close")?), &factor)?,
        };
        out.entry(id).or_default().push(OhlcBar {
            date: row_date(&row, "date")?,
            open: exchange(&open, &factor)?,
            high: exchange(&high, &factor)?,
            low: exchange(&low, &factor)?,
            close,
        });
    }
    Ok(out)
}

/// `sessions_between`: trading days in `(start, end]`.
async fn sessions_between(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<i64, WorkerError> {
    let (count,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date > $2 AND date <= $3",
    )
    .bind(common::NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_one(&mut *conn)
    .await?;
    Ok(count)
}

/// The queue columns of one `tw_position`, as loaded and as tonight leaves them.
#[derive(Debug, Clone, PartialEq)]
struct Queue {
    trail: Option<String>,
    partial_queued_for: Option<NaiveDate>,
    partial_quantity: Option<i32>,
    exit_queued_for: Option<NaiveDate>,
    exit_reason_queued: Option<String>,
    next_trigger: Option<Decimal>,
    next_trigger_for: Option<NaiveDate>,
}

/// `run_twt_manage_qulla`: Qullamaggie's rule over every `OPEN` position after the close.
/// Writes the trail (once) and tonight's queue; the ORM's flush issues an `UPDATE` (with
/// `updated_at = now()`) only for a row whose values changed, and so does this.
async fn run_twt_manage_qulla(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
    user_id: i64,
    config: &TwtConfig,
) -> Result<QullaReport, WorkerError> {
    let positions = sqlx::query(
        "SELECT p.id, p.instrument_id, p.entry_date, p.entry_avg, p.initial_stop, \
         p.stop_price, p.gtt_trigger, p.quantity_open, p.partial_done, p.trail, \
         p.partial_queued_for, p.partial_quantity, p.exit_queued_for, p.exit_reason_queued, \
         p.next_trigger, p.next_trigger_for, i.symbol \
         FROM tw_position p JOIN instrument i ON i.id = p.instrument_id \
         WHERE p.user_id = $1 AND p.state = 'OPEN' AND p.quantity_open > 0 ORDER BY i.symbol",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    if positions.is_empty() {
        return Ok(QullaReport::default());
    }
    let start = lookback_start(conn, trade_date, QULLA_LOOKBACK_SESSIONS).await?;
    let ids: Vec<i64> = positions
        .iter()
        .map(|r| r.try_get::<i64, _>("instrument_id"))
        .collect::<Result<_, _>>()?;
    let series = exchange_series(conn, &ids, start, trade_date).await?;
    let mut report = QullaReport {
        positions: len_i64(positions.len()),
        ..QullaReport::default()
    };
    let places = u32::try_from(precision::places_of("next_trigger").unwrap_or(2)).unwrap_or(2);
    for row in &positions {
        let id: i64 = row.try_get("id")?;
        let instrument_id: i64 = row.try_get("instrument_id")?;
        let entry_date = row_date(row, "entry_date")?;
        let stop_price = row_decimal(row, "stop_price")?;
        let resting = match opt_decimal(row, "gtt_trigger")? {
            None => stop_price,
            // Python's `max(a, b)` keeps `a` on a tie.
            Some(g) => {
                if g > stop_price {
                    g
                } else {
                    stop_price
                }
            }
        };
        let trail: Option<String> = row.try_get("trail")?;
        let before = Queue {
            trail: trail.clone(),
            partial_queued_for: row.try_get("partial_queued_for")?,
            partial_quantity: row.try_get("partial_quantity")?,
            exit_queued_for: row.try_get("exit_queued_for")?,
            exit_reason_queued: row.try_get("exit_reason_queued")?,
            next_trigger: opt_decimal(row, "next_trigger")?,
            next_trigger_for: row.try_get("next_trigger_for")?,
        };
        let bars_since_entry = sessions_between(conn, entry_date, trade_date).await?;
        let quantity: i32 = row.try_get("quantity_open")?;
        let position = qulla::Position {
            symbol: row.try_get("symbol")?,
            entry_date,
            entry: row_decimal(row, "entry_avg")?,
            initial_stop: row_decimal(row, "initial_stop")?,
            stop: resting,
            quantity: i64::from(quantity),
            partial_done: row.try_get("partial_done")?,
            trail: trail.as_deref().map(qulla::trail_from).transpose()?,
        };
        let empty = Vec::new();
        let bars = series.get(&instrument_id).unwrap_or(&empty);
        let Some(decision) = qulla::decide(
            &position,
            bars,
            trade_date,
            bars_since_entry,
            &config.exits.qulla,
        )?
        else {
            report.without_a_bar += 1;
            continue;
        };
        let mut after = Queue {
            trail: Some(trail.unwrap_or_else(|| qulla::trail_value(decision.trail).to_owned())),
            partial_queued_for: None,
            partial_quantity: None,
            exit_queued_for: None,
            exit_reason_queued: None,
            next_trigger: None,
            next_trigger_for: Some(trade_date),
        };
        if decision.stop_hit {
            report.stop_hits += 1;
        }
        if decision.sell_reason == PARTIAL && decision.sell_quantity > 0 {
            after.partial_queued_for = Some(trade_date);
            after.partial_quantity = Some(i32::try_from(decision.sell_quantity).map_err(|_| {
                WorkerError::python("DataError", "partial_quantity out of range for integer")
            })?);
            report.partials += 1;
        } else if decision.sell_reason == MA_TRAIL && decision.sell_quantity > 0 {
            after.exit_queued_for = Some(trade_date);
            after.exit_reason_queued = Some(MA_TRAIL.to_owned());
            report.exits += 1;
        }
        if let Some(level) = decision.raise_to
            && level > resting
        {
            after.next_trigger = precision::quantise(Some(level), places);
            report.raises += 1;
        }
        if after != before {
            sqlx::query(
                "UPDATE tw_position SET trail = $1, partial_queued_for = $2, \
                 partial_quantity = $3, exit_queued_for = $4, exit_reason_queued = $5, \
                 next_trigger = $6, next_trigger_for = $7, updated_at = now() WHERE id = $8",
            )
            .bind(&after.trail)
            .bind(after.partial_queued_for)
            .bind(after.partial_quantity)
            .bind(after.exit_queued_for)
            .bind(&after.exit_reason_queued)
            .bind(after.next_trigger)
            .bind(after.next_trigger_for)
            .bind(id)
            .execute(&mut *conn)
            .await?;
        }
    }
    Ok(report)
}

/// `run_detect_twt` (the nightly's call): returns the full `SIGNAL` count.
///
/// # Errors
/// Python's, as the task raises them; the orchestrator's wrapper records any as `skipped`.
pub(crate) async fn run_detect_twt(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    trade_date: NaiveDate,
    user_id: i64,
    pipeline_run_id: i64,
) -> Result<i64, WorkerError> {
    let config = load_twt_config(conn, user_id).await?;
    let universe = common::load_universe(
        conn,
        &UniverseRule {
            instrument_type: &config.data.instrument_type,
            series_allowed: &config.data.series_allowed,
            keep_null_series: config.data.keep_null_series,
            etf_universe_slug: &config.data.etf_universe_slug,
            etf_name_pattern: &config.data.etf_name_pattern,
            etf_symbol_pattern: &config.data.etf_symbol_pattern,
        },
    )
    .await?;
    let lookback = TwtConfig::default().data.bars_required;
    let start = lookback_start(conn, trade_date, lookback).await?;
    let bars = load_raw_bars(conn, start, trade_date, &universe, true).await?;
    let day = trade_date.format("%Y-%m-%d");
    if bars.height() == 0 {
        outcome.status = StepStatus::Skipped;
        outcome.note(
            "skipped_reason",
            pyjson::s(format!("no bars for {day} in the TWT universe")),
        );
        return Ok(0);
    }
    let (kept, calendar) = drop_thin_sessions(&bars, &config).map_err(core_err)?;
    let thin = calendar.dropped.contains(&trade_date);
    let instruments = len_i64(universe.len());
    let bar_count = len_i64(bars.height());
    if !thin && !calendar.sessions.contains(&trade_date) {
        outcome.status = StepStatus::Skipped;
        outcome.note(
            "skipped_reason",
            pyjson::s(format!("{day} is not a session the bars know about")),
        );
        outcome.note("instruments", pyjson::int(instruments));
        outcome.note("bars", pyjson::int(bar_count));
        return Ok(0);
    }
    let dropped = len_i64(calendar.dropped.len());
    if thin {
        let counted = calendar.counts.get(&trade_date).copied().unwrap_or(0);
        let funnel = TwtFunnel {
            instruments,
            bars: bar_count,
            with_a_bar: counted,
            with_a_dma: 0,
            above_the_dma: 0,
            in_state: 0,
            entries: 0,
            signals: 0,
            dropped_thin_sessions: dropped,
        };
        write_breadth_row(
            conn,
            user_id,
            trade_date,
            None,
            &funnel,
            true,
            &config,
            pipeline_run_id,
        )
        .await?;
        outcome.status = StepStatus::Skipped;
        outcome.note(
            "skipped_reason",
            pyjson::s(format!(
                "a thin session — only {counted} names printed, so 04 §2.1 removed it from \
                 the rolling calendar and the sleeve neither traded nor ratcheted on it"
            )),
        );
        for (k, v) in funnel.as_detail() {
            outcome.note(&k, v);
        }
        return Ok(0);
    }

    let detected = with_storage_columns(
        &with_twt_columns(&kept, &calendar, &config).map_err(core_err)?,
        &config,
    )?;
    let reading = breadth_above_dma(&detected, Some(trade_date), &config).map_err(core_err)?;
    let states = state_rows(&detected, trade_date)?;
    let signals = signal_rows(&detected, trade_date, &config)?;
    let states_written = upsert_states(conn, &states, user_id, trade_date, pipeline_run_id).await?;
    let entries_written =
        upsert_signals(conn, &signals, user_id, trade_date, pipeline_run_id).await?;
    let full = len_i64(
        signals
            .iter()
            .filter(|s| s.state == SignalState::Signal.value())
            .count(),
    );
    let funnel = TwtFunnel {
        instruments,
        bars: bar_count,
        with_a_bar: reading.universe_count,
        with_a_dma: reading.measured_count,
        above_the_dma: reading.above_count,
        in_state: states_written,
        entries: entries_written,
        signals: full,
        dropped_thin_sessions: dropped,
    };
    write_breadth_row(
        conn,
        user_id,
        trade_date,
        Some(&reading),
        &funnel,
        false,
        &config,
        pipeline_run_id,
    )
    .await?;
    outcome.rows_in = bar_count;
    outcome.rows_out = states_written + entries_written;
    if !config.exits.qulla_exits {
        // `run_twt_ratchet`: unreachable from the nightly — `qulla_exits` is a code default
        // (True) that no setting can change (`load_twt_config` reads only the two stops).
        // DECISIONS-RS records why it is not ported.
        return Err(WorkerError::NotPorted {
            what: "run_twt_ratchet",
        });
    }
    let managed = run_twt_manage_qulla(conn, trade_date, user_id, &config).await?;
    outcome.note("gate", pyjson::s(reading.gate.value()));
    for (k, v) in funnel.as_detail() {
        outcome.note(&k, v);
    }
    for (k, v) in managed.as_detail() {
        outcome.note(&k, v);
    }
    if states_written == 0 && managed.positions == 0 {
        outcome.status = StepStatus::Skipped;
    }
    Ok(full)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(
        ids: Vec<i64>,
        days: Vec<i32>,
        tight: Vec<Option<bool>>,
        close: Vec<Option<f64>>,
    ) -> DataFrame {
        let n = ids.len();
        DataFrame::new(
            n,
            vec![
                Series::new("instrument_id".into(), ids).into_column(),
                common::date_column("date", days.into_iter().map(Some).collect()).unwrap_or_else(
                    |_| Series::new("date".into(), Vec::<i32>::new()).into_column(),
                ),
                Series::new("tight_state".into(), tight).into_column(),
                Series::new("close".into(), close).into_column(),
            ],
        )
        .unwrap_or_default()
    }

    #[test]
    fn sessions_in_state_counts_from_the_last_false_and_ignores_a_null() {
        // Unsorted on purpose: the helper sorts by (instrument_id, date).
        let df = frame(
            vec![2, 1, 1, 1, 1, 2],
            vec![2, 1, 2, 3, 4, 1],
            vec![
                Some(true),
                Some(true),
                Some(false),
                None,
                Some(true),
                Some(false),
            ],
            vec![Some(1.0), Some(1.0), None, Some(1.0), Some(1.0), Some(1.0)],
        );
        let mut config = TwtConfig::default();
        config.breadth.dma_bars = 2;
        let out = with_storage_columns(&df, &config).unwrap_or_default();
        let state = common::ints(&out, "sessions_in_state").unwrap_or_default();
        let window = common::ints(&out, "bars_in_window").unwrap_or_default();
        // instrument 1: positions 0..3, last false at 1 → [1, 0, 1, 2]; instrument 2: [0, 1].
        assert_eq!(
            state,
            [Some(1), Some(0), Some(1), Some(2), Some(0), Some(1)]
        );
        // A trailing window of two rows of non-null closes, min_samples 1.
        assert_eq!(
            window,
            [Some(1), Some(1), Some(1), Some(2), Some(1), Some(2)]
        );
    }

    #[test]
    fn the_funnel_and_the_report_keep_pythons_keys() {
        let r = QullaReport::default();
        let keys: Vec<String> = r.as_detail().into_iter().map(|(k, _)| k).collect();
        assert_eq!(
            keys,
            [
                "managed_positions",
                "partials_queued",
                "exits_queued",
                "breakeven_raises",
                "stop_hits_seen",
                "positions_without_a_bar"
            ]
        );
    }
}
