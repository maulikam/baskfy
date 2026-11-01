//! Port of `baskfy_core.vbt.backtest` (`decile-blueprint/packages/core/src/baskfy_core/vbt/backtest.py`)
//! — the study, re-run by the book's own rules (`docs/vbt/04` §11).
//!
//! The session, in order: (1) exits at the open — a queued EMA exit at the open, else a gap
//! through the stop at the open, else the low through the stop at the stop; a name blank for
//! `no_bar_tolerance_sessions` is written off at its last close; (2) yesterday's signals become
//! working orders and stale ones expire, gate or no gate; (3) fills, only if the gate was OPEN at
//! the previous close, ranked by signal-day turnover, under the caps; (4) the fresh entry's own
//! low can stop it out; (5) at the close, mark equity and queue tomorrow's EMA exit; (6) at the
//! end, liquidate what is open at the last close, and count those trades.
//!
//! Money is `Decimal` (house rule 9): every rupee is a [`WideDecimal`] — Python's `Decimal` in its
//! default context — from the moment it is money, so each product and quotient rounds to 28
//! significant digits exactly where Python's does, and unquantised results (`r_multiple`,
//! `return_pct`) print Python's digits, scale 29 included (DECISIONS-RS R1.3). A bar price
//! enters as `Decimal(float)`, its exact binary value. Statistics are floats where Python's
//! are, with numpy's pairwise summation for `np.mean`/`np.std`.
//!
//! Goldens: `backtest.panel_from_frame`, `backtest.gate_vector`, `backtest.run_backtest`,
//! `backtest.summarise`, `backtest.yearly`, `backtest.BacktestResult.fill_rate_pct`.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::str::FromStr;

use chrono::{Datelike, NaiveDate};
use polars::prelude::{DataFrame, DataType};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::decimal::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::vbt::calendar::frames;
use crate::vbt::config::{Gate, VbtConfig};
use crate::vbt::exits::{ExitReason, wide};
use crate::vbt::sizing::SizeCap;

/// Ports `SESSIONS_A_YEAR`: sessions in an NSE trading year, for the Sharpe's annualisation.
pub const SESSIONS_A_YEAR: i64 = 252;

/// Ports `DAYS_A_YEAR = Decimal("365.25")`.
pub const DAYS_A_YEAR: &str = "365.25";

/// Ports `PAISE = Decimal("0.01")`.
pub const PAISE: &str = "0.01";

/// Ports `PANEL_COLUMNS`: the columns [`panel_from_frame`] reads off an indicated frame.
pub const PANEL_COLUMNS: [&str; 7] = [
    "open",
    "high",
    "low",
    "close",
    "ema_exit",
    "turnover_avg",
    "turnover_inr",
];

// =============================================================================================
// Panel
// =============================================================================================

fn de_matrix<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<Vec<f64>>, D::Error> {
    let rows: Vec<Vec<Option<f64>>> = Vec::deserialize(d)?;
    Ok(rows
        .into_iter()
        .map(|row| row.into_iter().map(|v| v.unwrap_or(f64::NAN)).collect())
        .collect())
}

fn ser_matrix<S: Serializer>(m: &[Vec<f64>], s: S) -> Result<S::Ok, S::Error> {
    let rows: Vec<Vec<Option<f64>>> = m
        .iter()
        .map(|row| {
            row.iter()
                .map(|v| if v.is_finite() { Some(*v) } else { None })
                .collect()
        })
        .collect();
    rows.serialize(s)
}

/// Ports `baskfy_core.vbt.backtest.Panel` — the history as matrices: rows are instruments,
/// columns are sessions. A missing bar is `NaN` (encoded `null`) and is **never** forward-filled
/// into a fill price.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Panel {
    /// One symbol per row (`""` when the frame had none).
    pub symbols: Vec<String>,
    /// One instrument id per row, ascending.
    pub instrument_ids: Vec<i64>,
    /// The sessions, ascending.
    pub sessions: Vec<NaiveDate>,
    /// The signal matrix.
    pub signal: Vec<Vec<bool>>,
    /// Opens.
    #[serde(serialize_with = "ser_matrix", deserialize_with = "de_matrix")]
    pub open: Vec<Vec<f64>>,
    /// Highs.
    #[serde(serialize_with = "ser_matrix", deserialize_with = "de_matrix")]
    pub high: Vec<Vec<f64>>,
    /// Lows.
    #[serde(serialize_with = "ser_matrix", deserialize_with = "de_matrix")]
    pub low: Vec<Vec<f64>>,
    /// Closes.
    #[serde(serialize_with = "ser_matrix", deserialize_with = "de_matrix")]
    pub close: Vec<Vec<f64>>,
    /// The exit EMA.
    #[serde(serialize_with = "ser_matrix", deserialize_with = "de_matrix")]
    pub ema_exit: Vec<Vec<f64>>,
    /// The 20-day average rupee turnover.
    #[serde(serialize_with = "ser_matrix", deserialize_with = "de_matrix")]
    pub turnover_avg: Vec<Vec<f64>>,
    /// The session's rupee turnover.
    #[serde(serialize_with = "ser_matrix", deserialize_with = "de_matrix")]
    pub turnover_inr: Vec<Vec<f64>>,
}

impl Panel {
    /// Ports the `instruments` property.
    #[must_use]
    pub fn instruments(&self) -> usize {
        self.symbols.len()
    }

    /// Ports the `sessions_count` property.
    #[must_use]
    pub fn sessions_count(&self) -> usize {
        self.sessions.len()
    }

    /// Ports `Panel.column_of`: the column at or after `session` (`bisect_left`).
    #[must_use]
    pub fn column_of(&self, session: NaiveDate) -> usize {
        self.sessions.partition_point(|d| *d < session)
    }
}

/// `matrix[row][col]` with Python's IndexError as an error.
fn at(m: &[Vec<f64>], row: usize, col: usize, what: &str) -> CoreResult<f64> {
    m.get(row).and_then(|r| r.get(col)).copied().ok_or_else(|| {
        CoreError::InvalidInput(format!("IndexError: {what}[{row}, {col}] is out of bounds"))
    })
}

/// Ports `baskfy_core.vbt.backtest.panel_from_frame`: a [`Panel`] from an indicated,
/// signal-tagged frame. `signal_column` is a `state` column (`SIGNAL` / `SCAN_ONLY` / null) or a
/// boolean column; symbols are each instrument's first non-null in `(instrument_id, date)` order.
///
/// # Errors
/// A missing `instrument_id`/`date` column, a null id or date, a `date` that is not a polars
/// `Date`, or a signal column that is neither `Boolean` nor `String`.
pub fn panel_from_frame(indicated: &DataFrame, signal_column: &str) -> CoreResult<Panel> {
    let ids_col = indicated
        .column("instrument_id")?
        .as_materialized_series()
        .cast(&DataType::Int64)?;
    let raw_ids: Vec<Option<i64>> = ids_col.i64()?.iter().collect();
    let raw_dates = frames::dates(indicated, "date")?;
    let n = raw_ids.len();
    let mut keyed: Vec<(i64, i32, usize)> = Vec::with_capacity(n);
    for r in 0..n {
        let id = raw_ids[r].ok_or_else(|| {
            CoreError::InvalidInput(
                "int() argument must not be None: a null instrument_id".to_owned(),
            )
        })?;
        let day = raw_dates[r]
            .ok_or_else(|| CoreError::InvalidInput("a null date cannot be a session".to_owned()))?;
        keyed.push((id, day, r));
    }
    // `frame.sort(["instrument_id", "date"])`; equal keys keep their frame order.
    keyed.sort_unstable();
    let ids: Vec<i64> = keyed
        .iter()
        .map(|k| k.0)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let days: Vec<i32> = keyed
        .iter()
        .map(|k| k.1)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let row_of: HashMap<i64, usize> = ids.iter().enumerate().map(|(i, v)| (*v, i)).collect();
    let col_of: HashMap<i32, usize> = days.iter().enumerate().map(|(i, v)| (*v, i)).collect();
    let place = |k: &(i64, i32, usize)| -> CoreResult<(usize, usize)> {
        match (row_of.get(&k.0), col_of.get(&k.1)) {
            (Some(r), Some(c)) => Ok((*r, *c)),
            _ => Err(CoreError::InvalidInput("panel key lost".to_owned())),
        }
    };
    let shape = (ids.len(), days.len());

    let matrix = |name: &str| -> CoreResult<Vec<Vec<f64>>> {
        let mut out = vec![vec![f64::NAN; shape.1]; shape.0];
        if frames::has(indicated, name) {
            let values = frames::floats(indicated, name)?;
            for k in &keyed {
                let (r, c) = place(k)?;
                out[r][c] = values[k.2].unwrap_or(f64::NAN);
            }
        }
        Ok(out)
    };

    let mut signal = vec![vec![false; shape.1]; shape.0];
    if frames::has(indicated, signal_column) {
        let series = indicated.column(signal_column)?.as_materialized_series();
        let flags: Vec<bool> = match series.dtype() {
            DataType::Boolean => series.bool()?.iter().map(|v| v.unwrap_or(false)).collect(),
            DataType::String => series.str()?.iter().map(|v| v == Some("SIGNAL")).collect(),
            DataType::Null => vec![false; series.len()],
            other => {
                return Err(CoreError::InvalidInput(format!(
                    "cannot compare a {other} signal column with 'SIGNAL'"
                )));
            }
        };
        for k in &keyed {
            let (r, c) = place(k)?;
            signal[r][c] = flags[k.2];
        }
    }

    let mut symbols = vec![String::new(); ids.len()];
    if frames::has(indicated, "symbol") {
        let sym = indicated
            .column("symbol")?
            .as_materialized_series()
            .cast(&DataType::String)?;
        let sym: Vec<Option<String>> = sym.str()?.iter().map(|v| v.map(str::to_owned)).collect();
        let mut named = vec![false; ids.len()];
        for k in &keyed {
            let (r, _) = place(k)?;
            if named[r] {
                continue;
            }
            if let Some(s) = &sym[k.2] {
                symbols[r].clone_from(s);
                named[r] = true;
            }
        }
    }

    let sessions = days
        .iter()
        .map(|d| frames::from_day_number(*d))
        .collect::<CoreResult<Vec<_>>>()?;
    Ok(Panel {
        symbols,
        instrument_ids: ids,
        sessions,
        signal,
        open: matrix("open")?,
        high: matrix("high")?,
        low: matrix("low")?,
        close: matrix("close")?,
        ema_exit: matrix("ema_exit")?,
        turnover_avg: matrix("turnover_avg")?,
        turnover_inr: matrix("turnover_inr")?,
    })
}

/// Ports `baskfy_core.vbt.backtest.gate_vector`: `gate == "OPEN"` per session, aligned to the
/// panel's columns. A session with no row is SHUT; a repeated date keeps its last row.
///
/// # Errors
/// A missing `date`/`gate` column or a `date` that is not a polars `Date`.
pub fn gate_vector(breadth: &DataFrame, sessions: &[NaiveDate]) -> CoreResult<Vec<bool>> {
    let days = frames::dates(breadth, "date")?;
    let gate = breadth
        .column("gate")?
        .as_materialized_series()
        .cast(&DataType::String)?;
    let gates: Vec<Option<String>> = gate.str()?.iter().map(|v| v.map(str::to_owned)).collect();
    let mut lookup: HashMap<Option<i32>, bool> = HashMap::new();
    for (day, g) in days.into_iter().zip(gates) {
        lookup.insert(day, g.as_deref() == Some(Gate::Open.value()));
    }
    sessions
        .iter()
        .map(|s| {
            Ok(lookup
                .get(&Some(frames::day_number(*s)?))
                .copied()
                .unwrap_or(false))
        })
        .collect()
}

// =============================================================================================
// Params, trades, results
// =============================================================================================

/// Ports `baskfy_core.vbt.backtest.BacktestParams` — what a run was asked for.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestParams {
    /// The sleeve's starting capital.
    pub sleeve_inr: WideDecimal,
    /// The first session the book may trade (everything before is warm-up).
    pub start: Option<NaiveDate>,
    /// The last session.
    pub end: Option<NaiveDate>,
    /// The backtest's own cost, a side, in percent.
    pub cost_pct_per_side: WideDecimal,
    /// The tick prices are floored and rounded to (the study's paise).
    pub tick: WideDecimal,
    /// The strategy's configuration.
    pub config: VbtConfig,
    /// A label.
    pub label: String,
}

impl Default for BacktestParams {
    fn default() -> Self {
        Self {
            sleeve_inr: WideDecimal::from(1_000_000_i64),
            start: None,
            end: None,
            cost_pct_per_side: WideDecimal::from_str("0.25").unwrap_or_default(),
            tick: WideDecimal::from_str(PAISE).unwrap_or_default(),
            config: VbtConfig::default(),
            label: "full".to_owned(),
        }
    }
}

/// Ports `baskfy_core.vbt.backtest.BacktestTrade` — one round trip.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestTrade {
    /// Symbol.
    pub symbol: String,
    /// Instrument.
    pub instrument_id: i64,
    /// Fill session.
    pub entry_date: NaiveDate,
    /// Exit session.
    pub exit_date: NaiveDate,
    /// Cost-adjusted entry.
    pub entry_price: WideDecimal,
    /// Exit price, rounded to the tick.
    pub exit_price: WideDecimal,
    /// Shares.
    pub quantity: i64,
    /// Rupee P&L net of both sides' cost.
    pub pnl_inr: WideDecimal,
    /// P&L over the basis, in percent (unquantised).
    pub return_pct: WideDecimal,
    /// The return over the initial risk (unquantised); `None` without positive risk.
    pub r_multiple: Option<WideDecimal>,
    /// Sessions held.
    pub hold_sessions: i64,
    /// Why it left.
    pub reason: ExitReason,
    /// The budget that bound the size.
    #[serde(default)]
    pub cap: Option<SizeCap>,
}

/// Ports `baskfy_core.vbt.backtest.YearRow` — one calendar year of the curve. Its `to_json` is
/// this record's own encoding.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct YearRow {
    /// The year.
    pub year: i32,
    /// The year's return, percent.
    pub return_pct: f64,
    /// Trades that closed in it.
    pub trades: i64,
    /// Their win rate, percent.
    pub win_rate_pct: f64,
}

/// Ports `baskfy_core.vbt.backtest.BacktestStats` — `04` §11's reported statistics.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestStats {
    /// First session.
    pub start: NaiveDate,
    /// Last session.
    pub end: NaiveDate,
    /// `(end - start).days / 365.25`, as a float.
    pub years: f64,
    /// The last non-zero equity, to the paisa.
    pub final_equity_inr: WideDecimal,
    /// CAGR, percent.
    pub cagr_pct: f64,
    /// The deepest fall from a running peak, percent (≤ 0).
    pub max_drawdown_pct: f64,
    /// The peak before the deepest fall.
    pub drawdown_peak_on: NaiveDate,
    /// The deepest point.
    pub drawdown_trough_on: NaiveDate,
    /// CAGR over |max drawdown|; `None` without a drawdown.
    pub calmar: Option<f64>,
    /// Daily Sharpe, annualised (`NaN`, encoded `null`, at zero deviation).
    pub sharpe: f64,
    /// Round trips.
    pub trades: i64,
    /// Winners, percent.
    pub win_rate_pct: f64,
    /// Gross win over gross loss; `None` without a loss.
    pub profit_factor: Option<f64>,
    /// Average winner's return, percent.
    pub avg_win_pct: f64,
    /// Average loser's return, percent.
    pub avg_loss_pct: f64,
    /// Average trade's return, percent.
    pub avg_trade_pct: f64,
    /// Average sessions held.
    pub avg_hold_sessions: f64,
    /// Average open positions per session.
    pub avg_open_positions: f64,
    /// Average open positions over the slots, percent.
    pub exposure_pct: f64,
    /// Trades per exit reason (only reasons that occurred).
    pub by_reason: BTreeMap<String, i64>,
}

/// The JSONB shape `BacktestStats.to_json` returns: the stats with `final_equity_inr` as
/// `str(Decimal)`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BacktestStatsJson {
    /// First session.
    pub start: NaiveDate,
    /// Last session.
    pub end: NaiveDate,
    /// Years.
    pub years: f64,
    /// `str(final_equity_inr)`.
    pub final_equity_inr: String,
    /// CAGR, percent.
    pub cagr_pct: f64,
    /// Max drawdown, percent.
    pub max_drawdown_pct: f64,
    /// Peak date.
    pub drawdown_peak_on: NaiveDate,
    /// Trough date.
    pub drawdown_trough_on: NaiveDate,
    /// Calmar.
    pub calmar: Option<f64>,
    /// Sharpe.
    pub sharpe: f64,
    /// Trades.
    pub trades: i64,
    /// Win rate.
    pub win_rate_pct: f64,
    /// Profit factor.
    pub profit_factor: Option<f64>,
    /// Average win.
    pub avg_win_pct: f64,
    /// Average loss.
    pub avg_loss_pct: f64,
    /// Average trade.
    pub avg_trade_pct: f64,
    /// Average hold.
    pub avg_hold_sessions: f64,
    /// Average open positions.
    pub avg_open_positions: f64,
    /// Exposure.
    pub exposure_pct: f64,
    /// Trades per reason.
    pub by_reason: BTreeMap<String, i64>,
}

impl BacktestStats {
    /// Ports `BacktestStats.to_json`: money as `str(Decimal)`, dates ISO.
    #[must_use]
    pub fn to_json(&self) -> BacktestStatsJson {
        BacktestStatsJson {
            start: self.start,
            end: self.end,
            years: self.years,
            final_equity_inr: self.final_equity_inr.to_sci_string(),
            cagr_pct: self.cagr_pct,
            max_drawdown_pct: self.max_drawdown_pct,
            drawdown_peak_on: self.drawdown_peak_on,
            drawdown_trough_on: self.drawdown_trough_on,
            calmar: self.calmar,
            sharpe: self.sharpe,
            trades: self.trades,
            win_rate_pct: self.win_rate_pct,
            profit_factor: self.profit_factor,
            avg_win_pct: self.avg_win_pct,
            avg_loss_pct: self.avg_loss_pct,
            avg_trade_pct: self.avg_trade_pct,
            avg_hold_sessions: self.avg_hold_sessions,
            avg_open_positions: self.avg_open_positions,
            exposure_pct: self.exposure_pct,
            by_reason: self.by_reason.clone(),
        }
    }
}

/// Ports `baskfy_core.vbt.backtest.BacktestResult`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestResult {
    /// What was asked.
    pub params: BacktestParams,
    /// Every round trip, in close order.
    pub trades: Vec<BacktestTrade>,
    /// The sessions walked.
    pub sessions: Vec<NaiveDate>,
    /// Equity at each session's close.
    pub equity: Vec<WideDecimal>,
    /// Open positions at each session's close.
    pub open_positions: Vec<i64>,
    /// Skip counters.
    #[serde(default)]
    pub skipped: BTreeMap<String, i64>,
    /// Distinct `(name, signal session)` orders that reached the fill test.
    #[serde(default)]
    pub orders_offered: i64,
}

/// Python's `round(x, 1)` on a float (correctly rounded, half-even on the exact binary value).
fn py_round1(x: f64) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.1}").parse::<f64>().unwrap_or(x)
}

fn usize_f64(n: usize) -> f64 {
    u32::try_from(n).map_or(f64::MAX, f64::from)
}

/// Python's `float(int)` for an int a double holds exactly (|n| <= 2^53).
#[allow(clippy::cast_precision_loss)] // bounded to the exact range just above
fn i64_f64(n: i64) -> CoreResult<f64> {
    if n.unsigned_abs() > (1_u64 << 53) {
        return Err(CoreError::InvalidInput(format!(
            "{n} is too large for an exact float"
        )));
    }
    Ok(n as f64)
}

impl BacktestResult {
    /// Ports `BacktestResult.stats` (= [`summarise`]).
    ///
    /// # Errors
    /// As [`summarise`].
    pub fn stats(&self) -> CoreResult<Option<BacktestStats>> {
        summarise(self)
    }

    /// Ports `BacktestResult.fill_rate_pct`: filled over offered, one decimal; `None` before
    /// anything was offered.
    ///
    /// # Errors
    /// Counts too large for an exact float.
    pub fn fill_rate_pct(&self) -> CoreResult<Option<f64>> {
        if self.orders_offered == 0 {
            return Ok(None);
        }
        let trades =
            i64::try_from(self.trades.len()).map_err(|e| CoreError::InvalidInput(e.to_string()))?;
        let numerator = i64_f64(trades.saturating_mul(100))?;
        Ok(Some(py_round1(numerator / i64_f64(self.orders_offered)?)))
    }
}

// =============================================================================================
// The engine
// =============================================================================================

#[derive(Debug, Clone)]
struct Position {
    row: usize,
    quantity: i64,
    entry: WideDecimal,
    entry_col: usize,
    stop: WideDecimal,
    initial_stop: WideDecimal,
    cap: Option<SizeCap>,
    queued_exit: Option<ExitReason>,
}

#[derive(Debug, Clone)]
struct Working {
    row: usize,
    signal_col: usize,
    limit: WideDecimal,
}

/// `_dec`: a price as a Decimal, exactly (`Decimal(float)`).
fn dec(value: f64) -> CoreResult<WideDecimal> {
    wide::from_f64_exact(value)
}

/// `_round_to_tick`: half-even, matching Python's own `round`.
fn round_to_tick(value: &WideDecimal, tick: &WideDecimal) -> CoreResult<WideDecimal> {
    Ok(wide::to_integral(&wide::div(value, tick)?, Rounding::HalfEven)?.mul(tick))
}

/// `_fill_threshold`: the limit itself with no through-requirement (never `limit x 1`, which
/// would round a fifty-digit float price to 28 digits and miss an exact touch).
fn fill_threshold(limit: &WideDecimal, fill_through: &WideDecimal) -> CoreResult<WideDecimal> {
    if fill_through.is_zero() {
        return Ok(limit.clone());
    }
    Ok(limit.mul(&wide::int(1).sub(&wide::div(fill_through, &wide::int(100))?)))
}

/// `_rank_of`: the signal-day rupee turnover; a non-finite one ranks last.
fn rank_of(panel: &Panel, row: usize, signal_col: usize) -> CoreResult<f64> {
    let value = at(&panel.turnover_inr, row, signal_col, "turnover_inr")?;
    Ok(if value.is_finite() {
        value
    } else {
        f64::NEG_INFINITY
    })
}

/// `_mark_equity`: cash plus every open position at `col`'s close, or at its entry.
fn mark_equity(
    panel: &Panel,
    positions: &[Position],
    cash: &WideDecimal,
    col: usize,
) -> CoreResult<WideDecimal> {
    let mut total = cash.clone();
    for position in positions {
        let mark = at(&panel.close, position.row, col, "close")?;
        let price = if mark.is_finite() {
            dec(mark)?
        } else {
            position.entry.clone()
        };
        total = total.add(&price.mul(&wide::int(position.quantity)));
    }
    Ok(total)
}

/// `_budget`: `04` §5.2's budgets, smallest wins (the first on a tie), and which one it was.
fn budget(
    equity: &WideDecimal,
    cash: &WideDecimal,
    turnover_avg: f64,
    slot_divisor: &WideDecimal,
    position_fraction: &WideDecimal,
    turnover_fraction: &WideDecimal,
) -> CoreResult<(WideDecimal, SizeCap)> {
    let mut budgets = vec![
        (wide::div(equity, slot_divisor)?, SizeCap::Slot),
        (equity.mul(position_fraction), SizeCap::PositionPct),
        (cash.clone(), SizeCap::Cash),
    ];
    if turnover_avg.is_finite() {
        budgets.push((dec(turnover_avg)?.mul(turnover_fraction), SizeCap::Turnover));
    }
    let mut best = 0;
    for i in 1..budgets.len() {
        if budgets[i].0 < budgets[best].0 {
            best = i;
        }
    }
    let (value, cap) = budgets.swap_remove(best);
    Ok((value, cap))
}

/// `a == b == c` on floats (False whenever one is NaN).
#[allow(clippy::float_cmp)] // Python compares the bar's three prints exactly
fn all_equal(a: f64, b: f64, c: f64) -> bool {
    a == b && b == c
}

/// Python's `seq[start:stop]` for `0 <= start`, any `stop`.
fn py_slice<T: Clone>(items: &[T], start: usize, stop: i64) -> Vec<T> {
    let len = i64::try_from(items.len()).unwrap_or(i64::MAX);
    let stop = if stop < 0 {
        (len + stop).max(0)
    } else {
        stop.min(len)
    };
    let stop = usize::try_from(stop).unwrap_or(0);
    if start >= stop {
        return Vec::new();
    }
    items[start..stop].to_vec()
}

struct Book<'a> {
    panel: &'a Panel,
    tick: WideDecimal,
    one_minus_cost: WideDecimal,
    cash: WideDecimal,
    positions: Vec<Position>,
    trades: Vec<BacktestTrade>,
}

impl Book<'_> {
    fn has(&self, row: usize) -> bool {
        self.positions.iter().any(|p| p.row == row)
    }

    fn get(&self, row: usize) -> Option<&Position> {
        self.positions.iter().find(|p| p.row == row)
    }

    fn rows(&self) -> Vec<usize> {
        self.positions.iter().map(|p| p.row).collect()
    }

    /// `close_position`.
    fn close(
        &mut self,
        row: usize,
        col: usize,
        price: &WideDecimal,
        reason: ExitReason,
    ) -> CoreResult<()> {
        let Some(index) = self.positions.iter().position(|p| p.row == row) else {
            return Err(CoreError::InvalidInput(format!("KeyError: {row}")));
        };
        let position = self.positions.remove(index);
        let price = round_to_tick(price, &self.tick)?;
        let quantity = wide::int(position.quantity);
        let proceeds = price.mul(&quantity).mul(&self.one_minus_cost);
        self.cash = self.cash.add(&proceeds);
        let basis = position.entry.mul(&quantity);
        let pnl = proceeds.sub(&basis);
        let risk = position.entry.sub(&position.initial_stop);
        let hundred = wide::int(100);
        let return_pct = if basis.is_zero() {
            WideDecimal::zero()
        } else {
            wide::div(&pnl, &basis)?.mul(&hundred)
        };
        let r_multiple = if risk > WideDecimal::zero() && !basis.is_zero() {
            Some(wide::div(
                &wide::div(&pnl, &basis)?,
                &wide::div(&risk, &position.entry)?,
            )?)
        } else {
            None
        };
        let panel = self.panel;
        let symbol = panel
            .symbols
            .get(position.row)
            .cloned()
            .ok_or_else(|| CoreError::InvalidInput("IndexError: symbols".to_owned()))?;
        let instrument_id = panel
            .instrument_ids
            .get(position.row)
            .copied()
            .ok_or_else(|| CoreError::InvalidInput("IndexError: instrument_ids".to_owned()))?;
        let session = |c: usize| {
            panel
                .sessions
                .get(c)
                .copied()
                .ok_or_else(|| CoreError::InvalidInput("IndexError: sessions".to_owned()))
        };
        self.trades.push(BacktestTrade {
            symbol,
            instrument_id,
            entry_date: session(position.entry_col)?,
            exit_date: session(col)?,
            entry_price: position.entry.clone(),
            exit_price: price,
            quantity: position.quantity,
            pnl_inr: pnl,
            return_pct,
            r_multiple,
            hold_sessions: i64::try_from(col).unwrap_or(0)
                - i64::try_from(position.entry_col).unwrap_or(0),
            reason,
            cap: position.cap,
        });
        Ok(())
    }
}

/// Ports `baskfy_core.vbt.backtest.run_backtest`: walk the history one session at a time and
/// return every trade and the equity curve.
///
/// `gate_open` is one boolean per session: may the book open new positions at the **next**
/// session, as read at this session's close.
///
/// # Errors
/// What Python raises: an index past the panel or the gate vector (`IndexError`), a zero
/// divisor (`DivisionByZero`), a non-finite config float the port refuses, and a `quantize`
/// beyond 28 digits.
#[allow(clippy::too_many_lines)] // the sequencing is the module; splitting it would hide the order
pub fn run_backtest(
    panel: &Panel,
    gate_open: &[bool],
    params: &BacktestParams,
) -> CoreResult<BacktestResult> {
    let config = &params.config;
    let (sizing, exits_config, entry_config) = (&config.sizing, &config.exits, &config.entry);
    let hundred = wide::int(100);
    let one = wide::int(1);
    let cost = wide::div(&params.cost_pct_per_side, &hundred)?;
    let tick = params.tick.clone();
    let stop_fraction = one.sub(&wide::div(
        &wide::from_py_float_str(exits_config.stop_pct)?,
        &hundred,
    )?);
    let fill_through = wide::from_py_float_str(entry_config.fill_through_pct)?;
    let slot_divisor = wide::int(sizing.max_slots);
    let position_fraction =
        wide::div(&wide::from_py_float_str(sizing.max_position_pct)?, &hundred)?;
    let turnover_fraction = wide::from_py_float_str(sizing.max_position_vs_turnover)?;
    let min_trade = WideDecimal::from(sizing.min_trade_value_inr);

    let total_sessions = panel.sessions_count();
    let total_i =
        i64::try_from(total_sessions).map_err(|e| CoreError::InvalidInput(e.to_string()))?;
    let first = params.start.map_or(0, |d| panel.column_of(d));
    let last: i64 = match params.end {
        None => total_i - 1,
        Some(d) => {
            i64::try_from(panel.column_of(d)).map_err(|e| CoreError::InvalidInput(e.to_string()))?
        }
    };

    let mut book = Book {
        panel,
        tick: tick.clone(),
        one_minus_cost: one.sub(&cost),
        cash: params.sleeve_inr.clone(),
        positions: Vec::new(),
        trades: Vec::new(),
    };
    // Insertion-ordered, as Python's dict is.
    let mut working: Vec<Working> = Vec::new();
    let mut offered: BTreeSet<(usize, usize)> = BTreeSet::new();
    let mut equity: Vec<WideDecimal> = vec![WideDecimal::zero(); total_sessions];
    let mut open_count: Vec<i64> = vec![0; total_sessions];
    let mut skipped: BTreeMap<String, i64> = [
        "expired",
        "session_cap",
        "slots_full",
        "cash",
        "turnover",
        "gate_shut",
        "no_bar",
        "locked",
    ]
    .into_iter()
    .map(|k| (k.to_owned(), 0))
    .collect();
    let mut bump = |key: &str, by: i64| {
        if let Some(v) = skipped.get_mut(key) {
            *v += by;
        }
    };
    let index_err = |what: &str, i: i64| {
        CoreError::InvalidInput(format!("IndexError: {what} index {i} is out of range"))
    };

    let first_i = i64::try_from(first).map_err(|e| CoreError::InvalidInput(e.to_string()))?;
    let mut col_i = first_i;
    while col_i <= last {
        let col = usize::try_from(col_i).map_err(|_| index_err("session", col_i))?;

        // --- 1. exits at the open ------------------------------------------------------------
        for row in book.rows() {
            let Some(position) = book.get(row).cloned() else {
                continue;
            };
            let open_price = at(&panel.open, row, col, "open")?;
            if !open_price.is_finite() {
                let mut previous = col_i - 1;
                while previous > i64::try_from(position.entry_col).unwrap_or(0) {
                    let p = usize::try_from(previous).map_err(|_| index_err("close", previous))?;
                    if at(&panel.close, row, p, "close")?.is_finite() {
                        break;
                    }
                    previous -= 1;
                }
                if col_i - previous >= exits_config.no_bar_tolerance_sessions {
                    let p = usize::try_from(previous).map_err(|_| index_err("close", previous))?;
                    let mark = at(&panel.close, row, p, "close")?;
                    let price = if mark.is_finite() {
                        dec(mark)?
                    } else {
                        position.stop.clone()
                    };
                    book.close(row, col, &price, ExitReason::NoBar)?;
                }
                continue;
            }
            if let Some(reason) = position.queued_exit {
                book.close(row, col, &dec(open_price)?, reason)?;
                continue;
            }
            let open_dec = dec(open_price)?;
            if open_dec <= position.stop {
                book.close(row, col, &open_dec, ExitReason::StopGap)?;
                continue;
            }
            let low = at(&panel.low, row, col, "low")?;
            if low.is_finite() && dec(low)? <= position.stop {
                book.close(row, col, &position.stop, ExitReason::StopHit)?;
            }
        }

        // --- 2. working orders: yesterday's signals in, stale ones out ------------------------
        if col >= 1 {
            for (key, flags) in panel.signal.iter().enumerate() {
                let flagged = flags
                    .get(col - 1)
                    .copied()
                    .ok_or_else(|| index_err("signal", col_i - 1))?;
                if !flagged {
                    continue;
                }
                if !book.has(key) && !working.iter().any(|w| w.row == key) {
                    let close_yesterday = at(&panel.close, key, col - 1, "close")?;
                    if close_yesterday.is_finite() {
                        working.push(Working {
                            row: key,
                            signal_col: col - 1,
                            limit: dec(close_yesterday)?,
                        });
                    }
                }
            }
            let valid = entry_config.valid_sessions;
            let before = working.len();
            working
                .retain(|order| i64::try_from(col - order.signal_col).unwrap_or(i64::MAX) <= valid);
            bump(
                "expired",
                i64::try_from(before - working.len()).unwrap_or(0),
            );
        }

        // --- 3. fills, if the gate was open at yesterday's close ------------------------------
        if col >= 1 {
            let gate = gate_open
                .get(col - 1)
                .copied()
                .ok_or_else(|| index_err("gate_open", col_i - 1))?;
            if gate {
                let mut candidates: Vec<(usize, f64)> = Vec::new();
                for order in &working {
                    if !book.has(order.row) {
                        candidates.push((order.row, rank_of(panel, order.row, order.signal_col)?));
                    }
                }
                if !candidates.is_empty() {
                    // `sort(key=-rank)`: stable, highest turnover first.
                    candidates.sort_by(|a, b| {
                        (-a.1)
                            .partial_cmp(&(-b.1))
                            .unwrap_or(std::cmp::Ordering::Equal)
                    });
                    let marked = mark_equity(panel, &book.positions, &book.cash, col - 1)?;
                    let mut taken: i64 = 0;
                    for (row, _) in candidates {
                        if taken >= sizing.max_new_entries_per_session {
                            bump("session_cap", 1);
                            break;
                        }
                        if i64::try_from(book.positions.len()).unwrap_or(i64::MAX)
                            >= sizing.max_slots
                        {
                            bump("slots_full", 1);
                            break;
                        }
                        let open_price = at(&panel.open, row, col, "open")?;
                        let high = at(&panel.high, row, col, "high")?;
                        let low = at(&panel.low, row, col, "low")?;
                        if !open_price.is_finite() || open_price <= 0.0 {
                            bump("no_bar", 1);
                            continue;
                        }
                        if all_equal(open_price, high, low) {
                            bump("locked", 1);
                            continue;
                        }
                        let Some(slot) = working.iter().position(|w| w.row == row) else {
                            return Err(CoreError::InvalidInput(format!("KeyError: {row}")));
                        };
                        let order = working[slot].clone();
                        offered.insert((row, order.signal_col));
                        if !low.is_finite()
                            || dec(low)? > fill_threshold(&order.limit, &fill_through)?
                        {
                            continue;
                        }
                        let fill = wide::py_min(&dec(open_price)?, &order.limit);
                        working.remove(slot);

                        let stop = wide::tick_floor(&fill.mul(&stop_fraction), &tick)?;
                        let entry_price = fill.mul(&one.add(&cost));
                        let turnover = at(&panel.turnover_avg, row, col - 1, "turnover_avg")?;
                        let (budget_value, cap) = budget(
                            &marked,
                            &book.cash,
                            turnover,
                            &slot_divisor,
                            &position_fraction,
                            &turnover_fraction,
                        )?;
                        let whole = wide::to_integral(
                            &wide::div(&budget_value, &entry_price)?,
                            Rounding::Down,
                        )?;
                        let quantity = wide::to_dec(&whole)?
                            .trunc()
                            .to_string()
                            .parse::<i64>()
                            .map_err(|e| CoreError::Arithmetic(e.to_string()))?;
                        let cost_of = wide::int(quantity).mul(&entry_price);
                        if cost_of < min_trade {
                            if book.cash < min_trade {
                                bump("cash", 1);
                            } else {
                                bump("turnover", 1);
                            }
                            continue;
                        }
                        book.cash = book.cash.sub(&cost_of);
                        book.positions.push(Position {
                            row,
                            quantity,
                            entry: entry_price,
                            entry_col: col,
                            stop: stop.clone(),
                            initial_stop: stop,
                            cap: Some(cap),
                            queued_exit: None,
                        });
                        taken += 1;
                    }
                }
            } else {
                let mut count: i64 = 0;
                for flags in &panel.signal {
                    if flags
                        .get(col - 1)
                        .copied()
                        .ok_or_else(|| index_err("signal", col_i - 1))?
                    {
                        count += 1;
                    }
                }
                bump("gate_shut", count);
            }
        }

        // --- 4/5. the fresh entry's own stop, then the close ----------------------------------
        for row in book.rows() {
            let Some(position) = book.get(row).cloned() else {
                continue;
            };
            let close = at(&panel.close, row, col, "close")?;
            if !close.is_finite() {
                continue;
            }
            if position.entry_col == col {
                let low = at(&panel.low, row, col, "low")?;
                if low.is_finite() && dec(low)? <= position.stop {
                    let open_price = dec(at(&panel.open, row, col, "open")?)?;
                    let price = if open_price > position.stop {
                        position.stop.clone()
                    } else {
                        open_price
                    };
                    book.close(row, col, &price, ExitReason::StopHit)?;
                    continue;
                }
            }
            let ema = at(&panel.ema_exit, row, col, "ema_exit")?;
            if ema.is_finite()
                && dec(close)? < dec(ema)?
                && let Some(p) = book.positions.iter_mut().find(|p| p.row == row)
            {
                p.queued_exit = Some(ExitReason::EmaExit);
            }
        }

        let marked = mark_equity(panel, &book.positions, &book.cash, col)?;
        let slot = equity
            .get_mut(col)
            .ok_or_else(|| index_err("equity", col_i))?;
        *slot = marked;
        let count = open_count
            .get_mut(col)
            .ok_or_else(|| index_err("open_count", col_i))?;
        *count = i64::try_from(book.positions.len()).unwrap_or(i64::MAX);
        col_i += 1;
    }

    // Python's `equity[last]` / `panel.close[row, last]` index from the end when negative.
    let resolve = |i: i64, len: usize, what: &str| -> CoreResult<usize> {
        let len_i = i64::try_from(len).unwrap_or(i64::MAX);
        let j = if i < 0 { len_i + i } else { i };
        if j < 0 || j >= len_i {
            return Err(index_err(what, i));
        }
        usize::try_from(j).map_err(|_| index_err(what, i))
    };
    for row in book.rows() {
        let Some(position) = book.get(row).cloned() else {
            continue;
        };
        let last_col = resolve(last, total_sessions, "close")?;
        let mark = at(&panel.close, row, last_col, "close")?;
        let price = if mark.is_finite() {
            dec(mark)?
        } else {
            position.entry.clone()
        };
        book.close(row, last_col, &price, ExitReason::EndOfRun)?;
    }
    let last_slot = resolve(last, equity.len(), "equity")?;
    equity[last_slot] = book.cash.clone();

    let orders_offered =
        i64::try_from(offered.len()).map_err(|e| CoreError::InvalidInput(e.to_string()))?;
    Ok(BacktestResult {
        params: params.clone(),
        trades: book.trades,
        sessions: py_slice(&panel.sessions, first, last + 1),
        equity: py_slice(&equity, first, last + 1),
        open_positions: py_slice(&open_count, first, last + 1),
        skipped,
        orders_offered,
    })
}

// =============================================================================================
// Statistics
// =============================================================================================

/// Float kernels in numpy's order of operations (DECISIONS-RS R2.2).
mod numeric {
    const PW_BLOCKSIZE: usize = 128;

    /// numpy's `pairwise_sum` over a contiguous float64 array.
    pub(super) fn pairwise_sum(a: &[f64]) -> f64 {
        let n = a.len();
        if n < 8 {
            let mut res = 0.0;
            for x in a {
                res += x;
            }
            res
        } else if n <= PW_BLOCKSIZE {
            let mut r = [0.0_f64; 8];
            r.copy_from_slice(&a[..8]);
            let mut i = 8;
            while i < n - (n % 8) {
                for (j, acc) in r.iter_mut().enumerate() {
                    *acc += a[i + j];
                }
                i += 8;
            }
            let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
            while i < n {
                res += a[i];
                i += 1;
            }
            res
        } else {
            let mut n2 = n / 2;
            n2 -= n2 % 8;
            pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
        }
    }

    fn len_f64(n: usize) -> f64 {
        u32::try_from(n).map_or(f64::MAX, f64::from)
    }

    /// `np.mean`.
    pub(super) fn mean(a: &[f64]) -> f64 {
        pairwise_sum(a) / len_f64(a.len())
    }

    /// `np.std(ddof=1)`.
    pub(super) fn std_ddof1(a: &[f64]) -> f64 {
        let m = mean(a);
        let squares: Vec<f64> = a
            .iter()
            .map(|x| {
                let d = x - m;
                d * d
            })
            .collect();
        (pairwise_sum(&squares) / len_f64(a.len().saturating_sub(1))).sqrt()
    }
}

/// Python's `float ** float` for the CAGR: a negative base with a fractional exponent is a
/// complex number in Python, which `Decimal(str(...))` then refuses; an overflow raises.
fn py_pow(base: f64, exponent: f64) -> CoreResult<f64> {
    if base < 0.0 && exponent.fract() != 0.0 && exponent.is_finite() {
        return Err(CoreError::InvalidInput(format!(
            "Decimal(str(complex)): {base} ** {exponent} is complex"
        )));
    }
    if base == 0.0 && exponent < 0.0 {
        return Err(CoreError::Arithmetic(
            "ZeroDivisionError: 0.0 cannot be raised to a negative power".to_owned(),
        ));
    }
    let out = base.powf(exponent);
    if out.is_infinite() && base.is_finite() && exponent.is_finite() {
        return Err(CoreError::Arithmetic(
            "OverflowError: (34, 'Result too large')".to_owned(),
        ));
    }
    Ok(out)
}

/// `sum(values, Decimal(0))`, left to right.
fn dsum<'a>(values: impl IntoIterator<Item = &'a WideDecimal>) -> WideDecimal {
    let mut total = WideDecimal::zero();
    for v in values {
        total = total.add(v);
    }
    total
}

fn zip_strict_check(a: usize, b: usize) -> CoreResult<()> {
    match a.cmp(&b) {
        std::cmp::Ordering::Equal => Ok(()),
        std::cmp::Ordering::Less => Err(CoreError::InvalidInput(
            "zip() argument 2 is longer than argument 1".to_owned(),
        )),
        std::cmp::Ordering::Greater => Err(CoreError::InvalidInput(
            "zip() argument 2 is shorter than argument 1".to_owned(),
        )),
    }
}

/// Ports `baskfy_core.vbt.backtest.summarise`: `04` §11's statistics; `None` for a curve with
/// fewer than two non-zero points or no sessions.
///
/// # Errors
/// Python's raises: a `zip(strict=True)` length mismatch, a complex or overflowing CAGR power,
/// a zero divisor, a `quantize` beyond 28 digits.
#[allow(clippy::too_many_lines)] // one record, field by field, as Python builds it
pub fn summarise(result: &BacktestResult) -> CoreResult<Option<BacktestStats>> {
    let zero = WideDecimal::zero();
    let one = wide::int(1);
    let hundred = wide::int(100);
    let curve: Vec<&WideDecimal> = result.equity.iter().filter(|v| !v.is_zero()).collect();
    if curve.len() < 2 || result.sessions.is_empty() {
        return Ok(None);
    }
    let (Some(&start), Some(&end)) = (result.sessions.first(), result.sessions.last()) else {
        return Ok(None);
    };
    let (Some(&first_point), Some(&last_point)) = (curve.first(), curve.last()) else {
        return Ok(None);
    };
    let days = (end - start).num_days();
    let years = wide::div(
        &wide::int(days),
        &WideDecimal::from_str(DAYS_A_YEAR).map_err(wide::arith)?,
    )?;
    let total = wide::div(last_point, first_point)?;
    let cagr = if years > zero {
        let grown = py_pow(wide::to_f64(&total)?, 1.0 / wide::to_f64(&years)?)?;
        wide::from_py_float_str(grown)?.sub(&one).mul(&hundred)
    } else {
        zero.clone()
    };

    zip_strict_check(result.sessions.len(), result.equity.len())?;
    let mut peak = first_point.clone();
    let mut drawdown = zero.clone();
    let (mut trough_at, mut peak_at, mut running_peak_at) = (start, start, start);
    for (session, value) in result.sessions.iter().zip(&result.equity) {
        if value.is_zero() {
            continue;
        }
        if *value > peak {
            peak = value.clone();
            running_peak_at = *session;
        }
        let fall = wide::div(value, &peak)?.sub(&one).mul(&hundred);
        if fall < drawdown {
            drawdown = fall;
            trough_at = *session;
            peak_at = running_peak_at;
        }
    }

    let mut returns: Vec<f64> = Vec::new();
    for pair in curve.windows(2) {
        let (earlier, later) = (pair[0], pair[1]);
        if !earlier.is_zero() {
            returns.push(wide::to_f64(&wide::div(later, earlier)?.sub(&one))?);
        }
    }
    let deviation = if returns.len() > 1 {
        numeric::std_ddof1(&returns)
    } else {
        0.0
    };
    let sessions_a_year = i64_f64(SESSIONS_A_YEAR)?;
    let sharpe = if deviation > 0.0 {
        numeric::mean(&returns) / deviation * sessions_a_year.sqrt()
    } else {
        f64::NAN
    };

    let wins: Vec<&BacktestTrade> = result.trades.iter().filter(|t| t.pnl_inr > zero).collect();
    let losses: Vec<&BacktestTrade> = result.trades.iter().filter(|t| t.pnl_inr <= zero).collect();
    let gross_win = dsum(wins.iter().map(|t| &t.pnl_inr));
    let gross_loss = dsum(losses.iter().map(|t| &t.pnl_inr)).neg();
    let count = result.trades.len();
    let sessions_seen = if result.open_positions.is_empty() {
        1
    } else {
        result.open_positions.len()
    };
    let open_sum: i64 = result.open_positions.iter().sum();
    let average_open = i64_f64(open_sum)? / usize_f64(sessions_seen);
    let avg_of = |items: &[&BacktestTrade]| -> CoreResult<f64> {
        if items.is_empty() {
            return Ok(0.0);
        }
        let n = wide::int(
            i64::try_from(items.len()).map_err(|e| CoreError::InvalidInput(e.to_string()))?,
        );
        wide::to_f64(&wide::div(&dsum(items.iter().map(|t| &t.return_pct)), &n)?)
    };
    let all: Vec<&BacktestTrade> = result.trades.iter().collect();
    let hold_total: i64 = result.trades.iter().map(|t| t.hold_sessions).sum();
    let mut by_reason: BTreeMap<String, i64> = BTreeMap::new();
    for reason in ExitReason::ALL {
        let n = result.trades.iter().filter(|t| t.reason == reason).count();
        if n > 0 {
            by_reason.insert(
                reason.value().to_owned(),
                i64::try_from(n).map_err(|e| CoreError::InvalidInput(e.to_string()))?,
            );
        }
    }
    let max_slots = i64_f64(result.params.config.sizing.max_slots)?;
    Ok(Some(BacktestStats {
        start,
        end,
        years: wide::to_f64(&years)?,
        final_equity_inr: last_point
            .quantize(-2, Rounding::HalfEven)
            .map_err(wide::arith)?,
        cagr_pct: wide::to_f64(&cagr)?,
        max_drawdown_pct: wide::to_f64(&drawdown)?,
        drawdown_peak_on: peak_at,
        drawdown_trough_on: trough_at,
        calmar: if drawdown < zero {
            Some(wide::to_f64(&wide::div(&cagr, &drawdown.abs())?)?)
        } else {
            None
        },
        sharpe,
        trades: i64::try_from(count).map_err(|e| CoreError::InvalidInput(e.to_string()))?,
        win_rate_pct: if count == 0 {
            0.0
        } else {
            usize_f64(wins.len()) / usize_f64(count) * 100.0
        },
        profit_factor: if gross_loss > zero {
            Some(wide::to_f64(&wide::div(&gross_win, &gross_loss)?)?)
        } else {
            None
        },
        avg_win_pct: avg_of(&wins)?,
        avg_loss_pct: avg_of(&losses)?,
        avg_trade_pct: avg_of(&all)?,
        avg_hold_sessions: if count == 0 {
            0.0
        } else {
            i64_f64(hold_total)? / usize_f64(count)
        },
        avg_open_positions: average_open,
        exposure_pct: average_open / max_slots * 100.0,
        by_reason,
    }))
}

/// Ports `baskfy_core.vbt.backtest.yearly`: calendar-year returns off the equity curve, and the
/// trades that closed in each.
///
/// # Errors
/// KNOWN BUG (R2.9, `yearly.case_003`): a result with no equity raises Python's `IndexError`
/// (`tuple index out of range`) where `summarise` returns `None` — reproduced as
/// [`CoreError::InvalidInput`] carrying that message. Also a `zip(strict=True)` mismatch and a
/// zero divisor.
pub fn yearly(result: &BacktestResult) -> CoreResult<Vec<YearRow>> {
    zip_strict_check(result.sessions.len(), result.equity.len())?;
    let zero = WideDecimal::zero();
    let mut by_year: BTreeMap<i32, Vec<&WideDecimal>> = BTreeMap::new();
    for (session, value) in result.sessions.iter().zip(&result.equity) {
        if !value.is_zero() {
            by_year.entry(session.year()).or_default().push(value);
        }
    }
    // KNOWN BUG (R2.9): unguarded `result.equity[0]`.
    let Some(mut opening) = result.equity.first().cloned() else {
        return Err(CoreError::InvalidInput(
            "tuple index out of range".to_owned(),
        ));
    };
    let one = wide::int(1);
    let hundred = wide::int(100);
    let mut out = Vec::new();
    for (year, values) in by_year {
        let Some(&closing) = values.last() else {
            continue;
        };
        let closed: Vec<&BacktestTrade> = result
            .trades
            .iter()
            .filter(|t| t.exit_date.year() == year)
            .collect();
        let wins = closed.iter().filter(|t| t.pnl_inr > zero).count();
        out.push(YearRow {
            year,
            return_pct: if opening.is_zero() {
                0.0
            } else {
                wide::to_f64(&wide::div(closing, &opening)?.sub(&one).mul(&hundred))?
            },
            trades: i64::try_from(closed.len())
                .map_err(|e| CoreError::InvalidInput(e.to_string()))?,
            win_rate_pct: if closed.is_empty() {
                0.0
            } else {
                usize_f64(wins) / usize_f64(closed.len()) * 100.0
            },
        });
        opening = closing.clone();
    }
    Ok(out)
}
