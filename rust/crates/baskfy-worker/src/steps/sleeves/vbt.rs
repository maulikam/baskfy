//! VB4 — the nightly volume-breakout detection job. Ports `baskfy_worker.tasks.vbt` as the
//! nightly reaches it (`run_detect_vbt` with no `extra_bars`, `provisional=False`).
//!
//! The arithmetic is `baskfy_core::vbt` (the calendar, the indicators, the scan, the six
//! filters, the breadth); this module reads rows, hands them over as a frame and writes what
//! comes back — one `vb_signal_daily` row per scan hit and one `vb_breadth_daily` row per
//! session, both upserts (house rule 7). **Law 2:** a `limit_price` and a `stop_price` are
//! numbers on a row; nothing here sends them anywhere.

use baskfy_core::vbt::breadth::{BreadthReading, breadth_above_dma};
use baskfy_core::vbt::calendar::drop_thin_sessions;
use baskfy_core::vbt::config::{Gate, SignalState, TICK_INR, VbtConfig};
use baskfy_core::vbt::exits::{initial_stop, tick_floor};
use baskfy_core::vbt::indicators::with_vbt_indicators;
use baskfy_core::vbt::signals::detect_signals;
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use polars::prelude::DataFrame;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{
    self, Num, UniverseRule, core_err, load_raw_bars, lookback_start, store_float,
};
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::{StepOutcome, StepStatus};

/// `LOOKBACK_SESSIONS`: `bars_required` (201) with room for a few missed sessions.
pub(crate) const LOOKBACK_SESSIONS: i64 = 260;

/// `VbtFunnel`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct VbtFunnel {
    pub instruments: i64,
    pub bars: i64,
    pub with_a_bar: i64,
    pub with_an_average: i64,
    pub above_the_average: i64,
    pub scan_hits: i64,
    pub signals: i64,
    pub thin_sessions_dropped: i64,
}

impl VbtFunnel {
    /// `as_detail()`, in its key order.
    pub(crate) fn as_detail(&self) -> Vec<(String, OrderedJson)> {
        vec![
            ("instruments".to_owned(), pyjson::int(self.instruments)),
            ("bars".to_owned(), pyjson::int(self.bars)),
            ("with_a_bar".to_owned(), pyjson::int(self.with_a_bar)),
            (
                "with_an_average".to_owned(),
                pyjson::int(self.with_an_average),
            ),
            (
                "above_the_average".to_owned(),
                pyjson::int(self.above_the_average),
            ),
            ("scan_hits".to_owned(), pyjson::int(self.scan_hits)),
            ("signals".to_owned(), pyjson::int(self.signals)),
            (
                "thin_sessions_dropped".to_owned(),
                pyjson::int(self.thin_sessions_dropped),
            ),
        ]
    }
}

/// `load_vbt_config`: the defaults with the user's `vb_config` row applied (no row: defaults).
pub(crate) async fn load_vbt_config(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<VbtConfig, WorkerError> {
    let row: Option<(i16, f64, f64)> = sqlx::query_as(
        "SELECT max_open_positions, max_position_pct::float8, stop_pct::float8 \
         FROM vb_config WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await?;
    let mut config = VbtConfig::default();
    if let Some((max_open, max_position_pct, stop_pct)) = row {
        let defaults = VbtConfig::default();
        config.sizing.max_slots = i64::from(max_open).min(defaults.sizing.max_slots);
        config.sizing.max_position_pct = max_position_pct;
        config.exits.stop_pct = stop_pct;
    }
    Ok(config)
}

/// One `vb_signal_daily` row, as `upsert_signals` builds its payload.
struct SignalRow {
    instrument_id: i64,
    state: String,
    failed_filters: Vec<String>,
    open: Option<Decimal>,
    high: Option<Decimal>,
    low: Option<Decimal>,
    close: Option<Decimal>,
    close_raw: Option<Decimal>,
    adj_factor: Option<Decimal>,
    limit_price: Option<Decimal>,
    stop_price: Option<Decimal>,
    change_pct: Option<Decimal>,
    rvol: Option<Decimal>,
    close_position: Option<Decimal>,
    ret_20_pct: Option<Decimal>,
    volume: Option<i64>,
    vol_sma_50: Option<i64>,
    turnover_inr: Option<i64>,
    turnover_avg_20: Option<i64>,
    sma_200: Option<Decimal>,
    ema_21: Option<Decimal>,
    high_20_prior: Option<Decimal>,
    rank_key: i64,
    locked_upper_circuit: bool,
}

/// `_as_exchange_price`: `value / factor if factor else value`.
fn as_exchange_price(value: Option<f64>, factor: f64) -> Option<f64> {
    value.map(|v| if factor == 0.0 { v } else { v / factor })
}

/// `signal_rows` + `upsert_signals`' payload: the scan hits with their levels as exchange
/// prices, storage precision applied, each value as Python's `_decimal`/`_int` reads it.
fn signal_rows(detected: &DataFrame, config: &VbtConfig) -> Result<Vec<SignalRow>, WorkerError> {
    let n = detected.height();
    if n == 0 {
        return Ok(Vec::new());
    }
    let tick: Decimal = TICK_INR
        .parse()
        .map_err(|_| common::value_err("TICK_INR does not parse"))?;
    let ids = common::ints(detected, "instrument_id")?;
    let factors = common::floats(detected, "adj_factor")?;
    let close_raw_f = common::floats(detected, "close_raw")?;
    let sma = common::floats(detected, "sma_dma")?;
    let ema = common::floats(detected, "ema_exit")?;
    let high_prior = common::floats(detected, "high_prior")?;
    let states = common::strings(detected, "state")?;
    let failed = common::string_lists(detected, "failed_filters")?;
    let locked = common::bools(detected, "locked_upper_circuit")?;
    // The columns copied from the detected row keep its dtype; `apply_storage_precision` then
    // rounds each by the name it is written under.
    let open = common::stored(detected, "open", "open")?;
    let high = common::stored(detected, "high", "high")?;
    let low = common::stored(detected, "low", "low")?;
    let close = common::stored(detected, "close", "close")?;
    let close_raw = common::stored(detected, "close_raw", "close_raw")?;
    let adj = common::stored(detected, "adj_factor", "adj_factor")?;
    let change = common::stored(detected, "change_pct", "change_pct")?;
    let rvol = common::stored(detected, "rvol", "rvol")?;
    let position = common::stored(detected, "close_position", "close_position")?;
    let ret = common::stored(detected, "ret_lookback_pct", "ret_20_pct")?;
    let volume = common::stored(detected, "volume", "volume")?;
    let vol_sma = common::stored(detected, "vol_sma", "vol_sma_50")?;
    let turnover = common::stored(detected, "turnover_inr", "turnover_inr")?;
    let turnover_avg = common::stored(detected, "turnover_avg", "turnover_avg_20")?;
    let rank = common::stored(detected, "rank_key", "rank_key")?;

    let mut rows = Vec::with_capacity(n);
    for i in 0..n {
        // `float(row["adj_factor"] or 1.0)`.
        let factor = match factors[i] {
            Some(f) if f != 0.0 => f,
            _ => 1.0,
        };
        let raw_close = close_raw_f[i]
            .ok_or_else(|| WorkerError::python("TypeError", "float() argument must be a number"))?;
        let limit =
            tick_floor(Num::F(raw_close).decimal()?.unwrap_or_default(), tick).map_err(core_err)?;
        let stop = initial_stop(limit, &config.exits, tick).map_err(core_err)?;
        let limit_f = decimal_to_f64(limit)?;
        let stop_f = decimal_to_f64(stop)?;
        let id = ids[i].ok_or_else(|| common::value_err("a signal row without an instrument"))?;
        rows.push(SignalRow {
            instrument_id: id,
            state: states[i].clone().unwrap_or_else(|| "None".to_owned()),
            failed_filters: failed.get(i).cloned().unwrap_or_default(),
            open: open[i].decimal()?,
            high: high[i].decimal()?,
            low: low[i].decimal()?,
            close: close[i].decimal()?,
            close_raw: close_raw[i].decimal()?,
            adj_factor: adj[i].decimal()?,
            limit_price: store_float("limit_price", Some(limit_f)).decimal()?,
            stop_price: store_float("stop_price", Some(stop_f)).decimal()?,
            change_pct: change[i].decimal()?,
            rvol: rvol[i].decimal()?,
            close_position: position[i].decimal()?,
            ret_20_pct: ret[i].decimal()?,
            volume: volume[i].int()?,
            vol_sma_50: vol_sma[i].int()?,
            turnover_inr: turnover[i].int()?,
            turnover_avg_20: turnover_avg[i].int()?,
            sma_200: store_float("sma_200", as_exchange_price(sma[i], factor)).decimal()?,
            ema_21: store_float("ema_21", as_exchange_price(ema[i], factor)).decimal()?,
            high_20_prior: store_float("high_20_prior", as_exchange_price(high_prior[i], factor))
                .decimal()?,
            // `_int(row["rank_key"]) or 0`.
            rank_key: rank[i].int()?.unwrap_or(0),
            locked_upper_circuit: locked[i].unwrap_or(false),
        });
    }
    Ok(rows)
}

/// `float(Decimal)`: the double nearest the decimal.
pub(crate) fn decimal_to_f64(d: Decimal) -> Result<f64, WorkerError> {
    d.to_string()
        .parse::<f64>()
        .map_err(|e| common::value_err(e.to_string()))
}

/// `upsert_signals`: the day's provisional stragglers swept (a real run), then one upsert per
/// row on `(user_id, date, instrument_id)`.
async fn upsert_signals(
    conn: &mut PgConnection,
    rows: &[SignalRow],
    user_id: i64,
    trade_date: NaiveDate,
    pipeline_run_id: i64,
) -> Result<i64, WorkerError> {
    let keep: Vec<i64> = rows.iter().map(|r| r.instrument_id).collect();
    if keep.is_empty() {
        sqlx::query(
            "DELETE FROM vb_signal_daily WHERE user_id = $1 AND date = $2 \
             AND provisional IS true",
        )
        .bind(user_id)
        .bind(trade_date)
        .execute(&mut *conn)
        .await?;
    } else {
        sqlx::query(
            "DELETE FROM vb_signal_daily WHERE user_id = $1 AND date = $2 \
             AND provisional IS true AND instrument_id <> ALL($3)",
        )
        .bind(user_id)
        .bind(trade_date)
        .bind(&keep)
        .execute(&mut *conn)
        .await?;
    }
    for r in rows {
        sqlx::query(
            "INSERT INTO vb_signal_daily (user_id, date, instrument_id, state, failed_filters, \
             open, high, low, close, close_raw, adj_factor, limit_price, stop_price, change_pct, \
             rvol, close_position, ret_20_pct, volume, vol_sma_50, turnover_inr, \
             turnover_avg_20, sma_200, ema_21, high_20_prior, rank_key, locked_upper_circuit, \
             bars_in_window, pipeline_run_id, provisional) VALUES ($1, $2, $3, $4, \
             $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, \
             $18, $19, $20, $21, $22, $23, $24, $25, $26, NULL, $27, false) \
             ON CONFLICT (user_id, date, instrument_id) DO UPDATE SET \
             state = excluded.state, failed_filters = excluded.failed_filters, \
             open = excluded.open, high = excluded.high, low = excluded.low, \
             close = excluded.close, close_raw = excluded.close_raw, \
             adj_factor = excluded.adj_factor, limit_price = excluded.limit_price, \
             stop_price = excluded.stop_price, change_pct = excluded.change_pct, \
             rvol = excluded.rvol, close_position = excluded.close_position, \
             ret_20_pct = excluded.ret_20_pct, volume = excluded.volume, \
             vol_sma_50 = excluded.vol_sma_50, turnover_inr = excluded.turnover_inr, \
             turnover_avg_20 = excluded.turnover_avg_20, sma_200 = excluded.sma_200, \
             ema_21 = excluded.ema_21, high_20_prior = excluded.high_20_prior, \
             rank_key = excluded.rank_key, \
             locked_upper_circuit = excluded.locked_upper_circuit, \
             bars_in_window = excluded.bars_in_window, \
             pipeline_run_id = excluded.pipeline_run_id, provisional = excluded.provisional",
        )
        .bind(user_id)
        .bind(trade_date)
        .bind(r.instrument_id)
        .bind(&r.state)
        .bind(&r.failed_filters)
        .bind(r.open)
        .bind(r.high)
        .bind(r.low)
        .bind(r.close)
        .bind(r.close_raw)
        .bind(r.adj_factor)
        .bind(r.limit_price)
        .bind(r.stop_price)
        .bind(r.change_pct)
        .bind(r.rvol)
        .bind(r.close_position)
        .bind(r.ret_20_pct)
        .bind(r.volume)
        .bind(r.vol_sma_50)
        .bind(r.turnover_inr)
        .bind(r.turnover_avg_20)
        .bind(r.sma_200)
        .bind(r.ema_21)
        .bind(r.high_20_prior)
        .bind(r.rank_key)
        .bind(r.locked_upper_circuit)
        .bind(pipeline_run_id)
        .execute(&mut *conn)
        .await?;
    }
    Ok(i64::try_from(rows.len()).unwrap_or(i64::MAX))
}

#[allow(clippy::too_many_arguments)]
/// `write_breadth_row`: one row per session, a thin one included (shut gate, zero counts).
async fn write_breadth_row(
    conn: &mut PgConnection,
    user_id: i64,
    trade_date: NaiveDate,
    reading: Option<&BreadthReading>,
    funnel: &VbtFunnel,
    thin_session: bool,
    config: &VbtConfig,
    pipeline_run_id: i64,
) -> Result<(), WorkerError> {
    let detail = OrderedJson::Object(funnel.as_detail())
        .to_python_json()
        .map_err(|e| common::value_err(e.to_string()))?;
    let (measured, above, pct, gate) = match reading {
        None => (0, 0, Decimal::new(0, 4), Gate::Shut.value()),
        Some(r) => (
            r.measured_count,
            r.above_count,
            r.pct_above_dma,
            r.gate.value(),
        ),
    };
    let dma_bars =
        i16::try_from(config.breadth.dma_bars).map_err(|e| common::value_err(e.to_string()))?;
    sqlx::query(
        "INSERT INTO vb_breadth_daily (user_id, date, universe_count, measured_count, \
         above_count, pct_above_dma, gate, dma_bars, thin_session, detail, pipeline_run_id, \
         provisional) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CAST($10 AS JSONB), $11, \
         false) ON CONFLICT (user_id, date) DO UPDATE SET \
         universe_count = excluded.universe_count, measured_count = excluded.measured_count, \
         above_count = excluded.above_count, pct_above_dma = excluded.pct_above_dma, \
         gate = excluded.gate, dma_bars = excluded.dma_bars, \
         thin_session = excluded.thin_session, detail = excluded.detail, \
         pipeline_run_id = excluded.pipeline_run_id, provisional = excluded.provisional",
    )
    .bind(user_id)
    .bind(trade_date)
    .bind(to_i32(funnel.with_a_bar)?)
    .bind(to_i32(measured)?)
    .bind(to_i32(above)?)
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

pub(crate) fn to_i32(v: i64) -> Result<i32, WorkerError> {
    i32::try_from(v).map_err(|e| common::value_err(e.to_string()))
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `run_detect_vbt` (the nightly's call): returns the `SIGNAL` count.
///
/// # Errors
/// Python's, as the task raises them; the orchestrator's wrapper records any as `skipped`.
pub(crate) async fn run_detect_vbt(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    trade_date: NaiveDate,
    user_id: i64,
    pipeline_run_id: i64,
) -> Result<i64, WorkerError> {
    let config = load_vbt_config(conn, user_id).await?;
    let universe = common::load_universe(
        conn,
        &UniverseRule {
            instrument_type: &config.data.instrument_type,
            series_allowed: &config.data.series_allowed,
            // VBT-1's loader has no null-series arm (`_universe_query`).
            keep_null_series: false,
            etf_universe_slug: &config.data.etf_universe_slug,
            etf_name_pattern: &config.data.etf_name_pattern,
            etf_symbol_pattern: &config.data.etf_symbol_pattern,
        },
    )
    .await?;
    let start = lookback_start(conn, trade_date, LOOKBACK_SESSIONS).await?;
    let bars = load_raw_bars(conn, start, trade_date, &universe, false).await?;
    if bars.height() == 0 {
        outcome.status = StepStatus::Skipped;
        outcome.note(
            "skipped_reason",
            pyjson::s(format!(
                "no bars for {} in the VBT universe",
                trade_date.format("%Y-%m-%d")
            )),
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
            pyjson::s(format!(
                "{} is not a session the bars know about",
                trade_date.format("%Y-%m-%d")
            )),
        );
        outcome.note("instruments", pyjson::int(instruments));
        outcome.note("bars", pyjson::int(bar_count));
        return Ok(0);
    }
    let dropped = len_i64(calendar.dropped.len());
    let counted = calendar.counts.get(&trade_date).copied().unwrap_or(0);

    if thin {
        let funnel = VbtFunnel {
            instruments,
            bars: bar_count,
            with_a_bar: counted,
            with_an_average: 0,
            above_the_average: 0,
            scan_hits: 0,
            signals: 0,
            thin_sessions_dropped: dropped,
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
                 the rolling calendar and the sleeve did not trade it"
            )),
        );
        for (k, v) in funnel.as_detail() {
            outcome.note(&k, v);
        }
        return Ok(0);
    }

    let indicated = with_vbt_indicators(&kept, &calendar, &config).map_err(core_err)?;
    let reading = breadth_above_dma(&indicated, Some(trade_date), &config).map_err(core_err)?;
    let detected = detect_signals(&indicated, Some(trade_date), &config).map_err(core_err)?;
    let rows = signal_rows(&detected, &config)?;
    let written = upsert_signals(conn, &rows, user_id, trade_date, pipeline_run_id).await?;
    let signals = len_i64(
        rows.iter()
            .filter(|r| r.state == SignalState::Signal.value())
            .count(),
    );
    let funnel = VbtFunnel {
        instruments,
        bars: bar_count,
        with_a_bar: reading.universe_count,
        with_an_average: reading.measured_count,
        above_the_average: reading.above_count,
        scan_hits: written,
        signals,
        thin_sessions_dropped: dropped,
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
    outcome.rows_out = written;
    outcome.note("gate", pyjson::s(reading.gate.value()));
    outcome.note("provisional", OrderedJson::Bool(false));
    for (k, v) in funnel.as_detail() {
        outcome.note(&k, v);
    }
    if written == 0 {
        outcome.status = StepStatus::Skipped;
    }
    Ok(signals)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exchange_price_divides_by_a_nonzero_factor_only() {
        assert_eq!(as_exchange_price(Some(10.0), 2.0), Some(5.0));
        assert_eq!(as_exchange_price(Some(10.0), 0.0), Some(10.0));
        assert_eq!(as_exchange_price(None, 2.0), None);
    }

    #[test]
    fn the_funnel_detail_is_pythons_key_order() {
        let f = VbtFunnel {
            instruments: 1,
            bars: 2,
            with_a_bar: 3,
            with_an_average: 4,
            above_the_average: 5,
            scan_hits: 6,
            signals: 7,
            thin_sessions_dropped: 8,
        };
        let keys: Vec<String> = f.as_detail().into_iter().map(|(k, _)| k).collect();
        assert_eq!(
            keys,
            [
                "instruments",
                "bars",
                "with_a_bar",
                "with_an_average",
                "above_the_average",
                "scan_hits",
                "signals",
                "thin_sessions_dropped"
            ]
        );
    }
}
