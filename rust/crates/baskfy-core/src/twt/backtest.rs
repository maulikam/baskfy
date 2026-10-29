//! Port of `baskfy_core.twt.backtest` — the study, re-run by the sleeve's own functions
//! (`docs/twt/04` §11).
//!
//! The backtest walks the functions the live sleeve walks — `size_entry`, `stop_fill`,
//! `fill_day_stop`, `ratchet`, `initial_stop` — so it cannot drift from the book by
//! construction; this module adds only the **sequencing**: exits at the open, fills at the open
//! (gate read at the previous close), the fill-day stop, the ratchet and the mark at the close,
//! and the end-of-run liquidation.
//!
//! Money and every price level are Python `Decimal`s, carried here as
//! [`WideDecimal`] (28 significant digits, half-even, Python's exponents — DECISIONS-RS R1.3),
//! because the study's quotients (`return_pct`, `r_multiple`, `years`, `calmar`) print beyond
//! `rust_decimal`'s scale. Prices arrive as `f64` matrices and are read **through their
//! shortest repr** — `Decimal(str(x))`, never `Decimal(x)` (DECISIONS-TW TW2.9).

use std::collections::{BTreeMap, HashMap};

use chrono::{Datelike, NaiveDate};
use polars::chunked_array::cast::CastOptions;
use polars::prelude::{DataFrame, DataType};
use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize};

use crate::decimal::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::py::float_repr as py_float_repr;
use crate::twt::config::{TICK_INR, TwtConfig};
use crate::twt::exits::wide::{self, Position as WPosition, WBar};
use crate::twt::exits::{ExitReason, arith};
use crate::twt::published::decimal_of_float;
use crate::twt::sizing::{SizeCap, SizeRefusal, size_entry};

/// Ports `PAISE`: the smallest money the study resolves; a final equity is quantised to it.
pub const PAISE: &str = "0.01";
/// Ports `SESSIONS_A_YEAR`: sessions in an NSE trading year, for the Sharpe's annualisation.
pub const SESSIONS_A_YEAR: i64 = 252;
/// Ports `DAYS_A_YEAR`: days in an average year including the leap.
pub const DAYS_A_YEAR: &str = "365.25";
/// Ports `PANEL_COLUMNS`: the columns `panel_from_frame` reads off a frame.
pub const PANEL_COLUMNS: [&str; 6] = [
    "open",
    "high",
    "low",
    "close",
    "turnover_inr",
    "turnover_avg_20",
];

/// `Decimal(100)`.
fn hundred() -> WideDecimal {
    WideDecimal::from(100_i64)
}

fn one() -> WideDecimal {
    WideDecimal::from(1_i64)
}

fn index_error(what: &str) -> CoreError {
    CoreError::InvalidInput(format!("IndexError: {what}"))
}

mod nan_matrix {
    //! A matrix of floats encoded the golden way: `null` is NaN.

    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub(super) fn serialize<S: Serializer>(m: &[Vec<f64>], s: S) -> Result<S::Ok, S::Error> {
        let rows: Vec<Vec<Option<f64>>> = m
            .iter()
            .map(|row| {
                row.iter()
                    .map(|x| if x.is_finite() { Some(*x) } else { None })
                    .collect()
            })
            .collect();
        rows.serialize(s)
    }

    pub(super) fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<Vec<f64>>, D::Error> {
        let rows: Vec<Vec<Option<f64>>> = Vec::deserialize(d)?;
        Ok(rows
            .into_iter()
            .map(|row| row.into_iter().map(|x| x.unwrap_or(f64::NAN)).collect())
            .collect())
    }
}

fn null_as_default<'de, D, T>(d: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(d)?.unwrap_or_default())
}

/// Ports `Panel`: the history as matrices, rows = instruments, columns = sessions. A missing bar
/// is NaN and is never forward-filled into a fill price.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Panel {
    /// One symbol per row (`""` when the frame had none).
    pub symbols: Vec<String>,
    /// One instrument per row, ascending.
    pub instrument_ids: Vec<i64>,
    /// One session per column, ascending.
    pub sessions: Vec<NaiveDate>,
    /// Opens.
    #[serde(with = "nan_matrix")]
    pub open: Vec<Vec<f64>>,
    /// Highs.
    #[serde(with = "nan_matrix")]
    pub high: Vec<Vec<f64>>,
    /// Lows.
    #[serde(with = "nan_matrix")]
    pub low: Vec<Vec<f64>>,
    /// Closes.
    #[serde(with = "nan_matrix")]
    pub close: Vec<Vec<f64>>,
    /// The session's own rupee turnover (the rank key).
    #[serde(with = "nan_matrix")]
    pub turnover_inr: Vec<Vec<f64>>,
    /// The 20-session average turnover.
    #[serde(with = "nan_matrix")]
    pub turnover_avg_20: Vec<Vec<f64>>,
    /// The entry signal per cell.
    pub signal: Vec<Vec<bool>>,
}

impl Panel {
    /// Ports `Panel.instruments`.
    #[must_use]
    pub fn instruments(&self) -> usize {
        self.symbols.len()
    }

    /// Ports `Panel.sessions_count`.
    #[must_use]
    pub fn sessions_count(&self) -> usize {
        self.sessions.len()
    }

    /// Ports `Panel.column_of`: the column at or after `session` (`bisect_left`).
    #[must_use]
    pub fn column_of(&self, session: NaiveDate) -> usize {
        self.sessions.partition_point(|s| *s < session)
    }

    fn cell(matrix: &[Vec<f64>], row: usize, col: usize) -> CoreResult<f64> {
        matrix
            .get(row)
            .and_then(|r| r.get(col))
            .copied()
            .ok_or_else(|| index_error("index is out of bounds for the panel"))
    }
}

/// Ports `panel_from_frame`: build a [`Panel`] from an indicated, signal-tagged frame.
/// `signal_column` may be a boolean column (`entry_event`) or `signal_state` (where `SIGNAL` is
/// the truth); a column the frame lacks leaves the mask all-false.
///
/// # Errors
/// A frame without `instrument_id`/`date`, a null in either, or a signal column that is
/// neither boolean nor string.
pub fn panel_from_frame(indicated: &DataFrame, signal_column: &str) -> CoreResult<Panel> {
    let has = |name: &str| {
        indicated
            .get_column_names()
            .iter()
            .any(|c| c.as_str() == name)
    };
    let ids_col = indicated
        .column("instrument_id")
        .map_err(|_| CoreError::MissingColumn("instrument_id".to_owned()))?
        .cast(&DataType::Int64)?;
    let ids: Vec<i64> = ids_col
        .i64()?
        .iter()
        .map(|v| v.ok_or_else(|| CoreError::InvalidInput("a null instrument_id".to_owned())))
        .collect::<CoreResult<_>>()?;
    let date_col = indicated
        .column("date")
        .map_err(|_| CoreError::MissingColumn("date".to_owned()))?
        .cast(&DataType::Date)?;
    let days: Vec<i32> = date_col
        .date()?
        .physical()
        .iter()
        .map(|v| v.ok_or_else(|| CoreError::InvalidInput("a null date".to_owned())))
        .collect::<CoreResult<_>>()?;

    // `indicated.sort(["instrument_id", "date"])`.
    let mut order: Vec<usize> = (0..ids.len()).collect();
    order.sort_by(|&a, &b| ids[a].cmp(&ids[b]).then(days[a].cmp(&days[b])));

    let mut unique_ids = ids.clone();
    unique_ids.sort_unstable();
    unique_ids.dedup();
    let mut unique_days = days.clone();
    unique_days.sort_unstable();
    unique_days.dedup();
    let row_of: HashMap<i64, usize> = unique_ids
        .iter()
        .enumerate()
        .map(|(i, v)| (*v, i))
        .collect();
    let col_of: HashMap<i32, usize> = unique_days
        .iter()
        .enumerate()
        .map(|(i, v)| (*v, i))
        .collect();
    let (n, m) = (unique_ids.len(), unique_days.len());
    let cell_of = |i: usize| -> (usize, usize) {
        (
            row_of.get(&ids[i]).copied().unwrap_or(0),
            col_of.get(&days[i]).copied().unwrap_or(0),
        )
    };

    let matrix = |name: &str| -> CoreResult<Vec<Vec<f64>>> {
        let mut out = vec![vec![f64::NAN; m]; n];
        if has(name) {
            let values: Vec<Option<f64>> = indicated
                .column(name)?
                .cast_with_options(&DataType::Float64, CastOptions::NonStrict)?
                .f64()?
                .iter()
                .collect();
            for &i in &order {
                let (r, c) = cell_of(i);
                out[r][c] = values[i].unwrap_or(f64::NAN);
            }
        }
        Ok(out)
    };

    let mut signal = vec![vec![false; m]; n];
    if has(signal_column) {
        let column = indicated.column(signal_column)?;
        let flags: Vec<bool> = match column.dtype() {
            DataType::Boolean => column.bool()?.iter().map(|v| v.unwrap_or(false)).collect(),
            DataType::String => column.str()?.iter().map(|v| v == Some("SIGNAL")).collect(),
            DataType::Null => vec![false; column.len()],
            other => {
                return Err(CoreError::InvalidInput(format!(
                    "cannot compare a {other} column with the string 'SIGNAL'"
                )));
            }
        };
        for &i in &order {
            let (r, c) = cell_of(i);
            signal[r][c] = flags[i];
        }
    }

    let mut symbols = vec![String::new(); n];
    if has("symbol") {
        let names: Vec<Option<String>> = indicated
            .column("symbol")?
            .cast(&DataType::String)?
            .str()?
            .iter()
            .map(|v| v.map(str::to_owned))
            .collect();
        let mut named = vec![false; n];
        for &i in &order {
            if let Some(name) = &names[i] {
                let (r, _) = cell_of(i);
                if !named[r] {
                    symbols[r].clone_from(name);
                    named[r] = true;
                }
            }
        }
    }

    let sessions = unique_days
        .iter()
        .map(|d| {
            NaiveDate::default()
                .checked_add_signed(chrono::TimeDelta::days(i64::from(*d)))
                .ok_or_else(|| CoreError::InvalidInput(format!("date {d} is out of range")))
        })
        .collect::<CoreResult<Vec<_>>>()?;

    Ok(Panel {
        symbols,
        instrument_ids: unique_ids,
        sessions,
        open: matrix("open")?,
        high: matrix("high")?,
        low: matrix("low")?,
        close: matrix("close")?,
        turnover_inr: matrix("turnover_inr")?,
        turnover_avg_20: matrix("turnover_avg_20")?,
        signal,
    })
}

/// Ports `BacktestParams`: what a run was asked for. The defaults are the sleeve's.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestParams {
    /// Starting capital.
    pub sleeve_inr: Decimal,
    /// The first session the book may trade.
    pub start: Option<NaiveDate>,
    /// The last session.
    pub end: Option<NaiveDate>,
    /// The backtest's own cost, a side, as a **percent**.
    pub cost_pct_per_side: Decimal,
    /// The tick every level is floored and rounded to.
    pub tick: Decimal,
    /// The sleeve's configuration (`null` decodes as the default).
    #[serde(deserialize_with = "null_as_default")]
    pub config: TwtConfig,
    /// A label.
    pub label: String,
}

impl Default for BacktestParams {
    fn default() -> Self {
        let config = TwtConfig::default();
        // `cost_bps_per_side / Decimal(100)`: 25.0 / 100 is 0.25.
        let cost = WideDecimal::from(config.costs.cost_bps_per_side)
            .div(&hundred())
            .ok()
            .and_then(|c| c.to_decimal())
            .unwrap_or_else(|| Decimal::new(25, 2));
        Self {
            sleeve_inr: config.backtest.initial_capital_inr,
            start: None,
            end: None,
            cost_pct_per_side: cost,
            tick: Decimal::from_str_exact(TICK_INR).unwrap_or_else(|_| Decimal::new(5, 2)),
            config,
            label: "full".to_owned(),
        }
    }
}

/// Ports `BacktestTrade`: one round trip.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BacktestTrade {
    /// The symbol.
    pub symbol: String,
    /// The instrument.
    pub instrument_id: i64,
    /// The fill session.
    pub entry_date: NaiveDate,
    /// The exit session.
    pub exit_date: NaiveDate,
    /// The cost-inclusive price the book paid.
    pub entry_price: WideDecimal,
    /// The exchange print the exit filled at, rounded to the tick.
    pub exit_price: WideDecimal,
    /// Shares.
    pub quantity: i64,
    /// Proceeds after the sell-side cost, less the basis.
    pub pnl_inr: WideDecimal,
    /// `pnl / basis x 100`.
    pub return_pct: WideDecimal,
    /// `(pnl / basis) / (risk / entry)`; `None` without positive risk.
    pub r_multiple: Option<WideDecimal>,
    /// Sessions held.
    pub hold_sessions: i64,
    /// Why it left.
    pub reason: ExitReason,
    /// Which budget the quantity was made of.
    pub cap: Option<SizeCap>,
}

/// Ports `YearRow`: one calendar year of the equity curve.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct YearRow {
    /// The year.
    pub year: i32,
    /// The year's return, percent.
    pub return_pct: f64,
    /// Trades that closed in the year.
    pub trades: i64,
    /// Their win rate, percent.
    pub win_rate_pct: f64,
}

/// Ports `BacktestStats`: `04` §12's reported statistics.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestStats {
    /// First session.
    pub start: NaiveDate,
    /// Last session.
    pub end: NaiveDate,
    /// `days / 365.25`.
    pub years: WideDecimal,
    /// The last non-zero equity, to the paise.
    pub final_equity_inr: WideDecimal,
    /// Compound annual growth, percent.
    pub cagr_pct: f64,
    /// The deepest fall from a running peak, percent.
    pub max_drawdown_pct: f64,
    /// The peak of the deepest fall.
    pub drawdown_peak_on: NaiveDate,
    /// Its trough.
    pub drawdown_trough_on: NaiveDate,
    /// `cagr / |drawdown|`.
    pub calmar: Option<WideDecimal>,
    /// Daily Sharpe, annualised (NaN on a flat curve).
    pub sharpe_ratio: f64,
    /// Trades.
    pub trades: i64,
    /// Winners, percent.
    pub win_rate_pct: f64,
    /// Gross win over gross loss.
    pub profit_factor: Option<WideDecimal>,
    /// Mean winning return.
    pub avg_win_pct: f64,
    /// Mean losing return.
    pub avg_loss_pct: f64,
    /// Mean return.
    pub avg_trade_pct: f64,
    /// Mean hold.
    pub avg_hold_sessions: WideDecimal,
    /// Mean open positions per session.
    pub avg_open_positions: WideDecimal,
    /// Mean open positions over the slots, percent.
    pub exposure_pct: f64,
    /// Trades per exit reason.
    pub by_reason: BTreeMap<String, i64>,
}

/// Ports `BacktestResult`: every trade, the curve, and what the book did not do.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestResult {
    /// What the run was asked for.
    pub params: BacktestParams,
    /// Every round trip.
    pub trades: Vec<BacktestTrade>,
    /// The sessions of the window.
    pub sessions: Vec<NaiveDate>,
    /// Equity per session.
    pub equity: Vec<WideDecimal>,
    /// Open positions per session.
    pub open_positions: Vec<i64>,
    /// Signals that did not become positions, by cause.
    #[serde(default)]
    pub skipped: BTreeMap<String, i64>,
    /// Times the clamp produced a trigger below the stop in force.
    #[serde(default)]
    pub clamped_below_stop: i64,
}

impl BacktestResult {
    /// Ports `BacktestResult.stats`.
    ///
    /// # Errors
    /// As [`summarise`].
    pub fn stats(&self) -> CoreResult<Option<BacktestStats>> {
        summarise(self)
    }
}

/// `_Position`: an open position in the walk.
#[derive(Debug, Clone)]
struct Pos {
    row: usize,
    quantity: i64,
    entry: WideDecimal,
    entry_col: usize,
    stop: WideDecimal,
    initial_stop: WideDecimal,
    high_since: WideDecimal,
    cap: Option<SizeCap>,
}

/// `_dec`: `Decimal(str(x))`.
fn dec(x: f64) -> CoreResult<WideDecimal> {
    decimal_of_float(x)
}

/// `_round_to_tick`: half-even to the tick.
fn round_to_tick(value: &WideDecimal, tick: &WideDecimal) -> CoreResult<WideDecimal> {
    let q = value.div(tick).map_err(arith)?;
    Ok(wide::to_integral(&q, Rounding::HalfEven)?.mul(tick))
}

/// `_bar_at`: a non-finite cell is `None` — "the name did not print".
fn bar_at(panel: &Panel, row: usize, col: usize) -> CoreResult<WBar> {
    let level = |matrix: &[Vec<f64>]| -> CoreResult<Option<WideDecimal>> {
        let value = Panel::cell(matrix, row, col)?;
        if value.is_finite() {
            dec(value).map(Some)
        } else {
            Ok(None)
        }
    };
    Ok(WBar {
        session: *panel
            .sessions
            .get(col)
            .ok_or_else(|| index_error("tuple index out of range"))?,
        open: level(&panel.open)?,
        high: level(&panel.high)?,
        low: level(&panel.low)?,
        close: level(&panel.close)?,
    })
}

fn as_open_position(panel: &Panel, position: &Pos) -> CoreResult<WPosition> {
    Ok(WPosition {
        entry_date: *panel
            .sessions
            .get(position.entry_col)
            .ok_or_else(|| index_error("tuple index out of range"))?,
        stop_price: position.stop.clone(),
        high_since: position.high_since.clone(),
    })
}

/// `_rank_of`: the signal session's own rupee turnover; a missing reading ranks last.
fn rank_of(panel: &Panel, row: usize, signal_col: usize) -> CoreResult<WideDecimal> {
    let value = Panel::cell(&panel.turnover_inr, row, signal_col)?;
    if value.is_finite() {
        dec(value)
    } else {
        "-1e30".parse::<WideDecimal>().map_err(arith)
    }
}

/// `_mark_equity`: cash plus every open position at `col`'s close, or at its entry.
fn mark_equity(
    panel: &Panel,
    positions: &[Pos],
    cash: &WideDecimal,
    col: usize,
) -> CoreResult<WideDecimal> {
    let mut total = cash.clone();
    for position in positions {
        let mark = Panel::cell(&panel.close, position.row, col)?;
        let price = if mark.is_finite() {
            dec(mark)?
        } else {
            position.entry.clone()
        };
        total = total.add(&price.mul(&WideDecimal::from(position.quantity)));
    }
    Ok(total)
}

/// `_last_printed_close`: the latest session at or before `col - 1` (not before the entry) on
/// which the name printed, and its close.
fn last_printed_close(
    panel: &Panel,
    position: &Pos,
    col: usize,
) -> CoreResult<(usize, Option<WideDecimal>)> {
    let mut previous = col.saturating_sub(1);
    while previous > position.entry_col
        && !Panel::cell(&panel.close, position.row, previous)?.is_finite()
    {
        previous -= 1;
    }
    let mark = Panel::cell(&panel.close, position.row, previous)?;
    Ok((
        previous,
        if mark.is_finite() {
            Some(dec(mark)?)
        } else {
            None
        },
    ))
}

/// Ports `gate_vector`: `gate == "OPEN"` per session; a session with no row is SHUT.
///
/// # Errors
/// A frame without `date` or `gate`.
pub fn gate_vector(breadth: &DataFrame, sessions: &[NaiveDate]) -> CoreResult<Vec<bool>> {
    let mut lookup: HashMap<Option<i32>, bool> = HashMap::new();
    if breadth.height() > 0 || breadth.width() > 0 {
        let days: Vec<Option<i32>> = breadth
            .column("date")
            .map_err(|_| CoreError::MissingColumn("date".to_owned()))?
            .cast(&DataType::Date)?
            .date()?
            .physical()
            .iter()
            .collect();
        let gates: Vec<bool> = breadth
            .column("gate")
            .map_err(|_| CoreError::MissingColumn("gate".to_owned()))?
            .cast(&DataType::String)?
            .str()?
            .iter()
            .map(|g| g == Some("OPEN"))
            .collect();
        for (day, gate) in days.into_iter().zip(gates) {
            lookup.insert(day, gate);
        }
    }
    let epoch = NaiveDate::default();
    Ok(sessions
        .iter()
        .map(|s| {
            let day = i32::try_from((*s - epoch).num_days()).ok();
            day.is_some_and(|d| lookup.get(&Some(d)).copied().unwrap_or(false))
        })
        .collect())
}

/// The walk's mutable state, so `close_position` can be a method rather than a closure over
/// the whole book.
struct Book<'a> {
    panel: &'a Panel,
    tick: WideDecimal,
    cost: WideDecimal,
    cash: WideDecimal,
    positions: Vec<Pos>,
    trades: Vec<BacktestTrade>,
}

impl Book<'_> {
    /// `close_position`: round the exit to the tick, book the proceeds net of the sell-side
    /// cost, write the trade, drop the position.
    fn close(
        &mut self,
        row: usize,
        col: usize,
        price: &WideDecimal,
        reason: ExitReason,
    ) -> CoreResult<()> {
        let Some(index) = self.positions.iter().position(|p| p.row == row) else {
            return Err(CoreError::InvalidInput(format!(
                "no open position on row {row}"
            )));
        };
        let position = self.positions.remove(index);
        let price = round_to_tick(price, &self.tick)?;
        let quantity = WideDecimal::from(position.quantity);
        let proceeds = price.mul(&quantity).mul(&one().sub(&self.cost));
        self.cash = self.cash.add(&proceeds);
        let basis = position.entry.mul(&quantity);
        let pnl = proceeds.sub(&basis);
        let risk = position.entry.sub(&position.initial_stop);
        let return_pct = if basis.is_zero() {
            WideDecimal::zero()
        } else {
            pnl.div(&basis).map_err(arith)?.mul(&hundred())
        };
        let r_multiple = if risk > WideDecimal::zero() && !basis.is_zero() {
            let per_basis = pnl.div(&basis).map_err(arith)?;
            let per_entry = risk.div(&position.entry).map_err(arith)?;
            Some(per_basis.div(&per_entry).map_err(arith)?)
        } else {
            None
        };
        let panel = self.panel;
        let session = |c: usize| {
            panel
                .sessions
                .get(c)
                .copied()
                .ok_or_else(|| index_error("tuple index out of range"))
        };
        self.trades.push(BacktestTrade {
            symbol: panel.symbols.get(row).cloned().unwrap_or_default(),
            instrument_id: panel.instrument_ids.get(row).copied().unwrap_or(0),
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

fn to_dec(x: &WideDecimal) -> CoreResult<Decimal> {
    x.try_to_decimal().map_err(arith)
}

/// Ports `run_backtest`: walk the history one session at a time and return every trade and the
/// equity curve. `gate_open[c]` says whether the book may open positions at session `c + 1`, as
/// read at `c`'s close.
///
/// # Errors
/// Python's `IndexError` where its indexing would raise (an empty panel, an `end` past the last
/// session, a short `gate_open`), and [`CoreError::Arithmetic`] where `Decimal` would raise.
#[allow(clippy::too_many_lines)]
pub fn run_backtest(
    panel: &Panel,
    gate_open: &[bool],
    params: &BacktestParams,
) -> CoreResult<BacktestResult> {
    let config = &params.config;
    let (sizing, exits_config) = (&config.sizing, &config.exits);
    let cost = WideDecimal::from(params.cost_pct_per_side)
        .div(&hundred())
        .map_err(arith)?;
    let tick = WideDecimal::from(params.tick);
    let min_trade = WideDecimal::from(sizing.min_trade_value_inr);

    let total_sessions = panel.sessions_count();
    let first = params.start.map_or(0, |s| panel.column_of(s));
    let last: i64 = match params.end {
        None => i64::try_from(total_sessions).unwrap_or(i64::MAX) - 1,
        Some(e) => i64::try_from(panel.column_of(e)).unwrap_or(i64::MAX),
    };
    let last_index = usize::try_from(last)
        .ok()
        .filter(|l| *l < total_sessions)
        .ok_or_else(|| index_error("list assignment index out of range"))?;

    let mut book = Book {
        panel,
        tick: tick.clone(),
        cost: cost.clone(),
        cash: WideDecimal::from(params.sleeve_inr),
        positions: Vec::new(),
        trades: Vec::new(),
    };
    let mut equity = vec![WideDecimal::zero(); total_sessions];
    let mut open_count = vec![0_i64; total_sessions];
    let mut clamped_below_stop = 0_i64;
    let mut skipped: BTreeMap<String, i64> = [
        "gate_shut",
        "session_cap",
        "slots_full",
        "no_bar",
        "locked",
        "cash",
        "turnover",
    ]
    .into_iter()
    .map(|k| (k.to_owned(), 0))
    .collect();
    let mut bump = |key: &str, by: i64| {
        if let Some(v) = skipped.get_mut(key) {
            *v += by;
        }
    };

    for col in first..=last_index {
        // --- 1. exits at the open ------------------------------------------------------------
        let rows: Vec<usize> = book.positions.iter().map(|p| p.row).collect();
        for row in rows {
            let Some(position) = book.positions.iter().find(|p| p.row == row).cloned() else {
                continue;
            };
            let bar = bar_at(panel, row, col)?;
            if bar.open.is_none() {
                let (previous, mark) = last_printed_close(panel, &position, col)?;
                let blank = i64::try_from(col - previous).unwrap_or(i64::MAX);
                if blank >= exits_config.no_bar_sessions {
                    let written_off = mark.unwrap_or_else(|| position.stop.clone());
                    book.close(row, col, &written_off, ExitReason::NoBar)?;
                }
                continue;
            }
            if let Some((reason, price)) = wide::stop_fill(&bar, &position.stop) {
                book.close(row, col, &price, reason)?;
            }
        }

        // --- 2. fills at the open, if the gate was OPEN at yesterday's close -----------------
        if col >= 1 {
            let signal_col = col - 1;
            let gate = *gate_open
                .get(signal_col)
                .ok_or_else(|| index_error("index is out of bounds for the gate vector"))?;
            let signalled: Vec<usize> = panel
                .signal
                .iter()
                .enumerate()
                .filter(|(_, r)| r.get(signal_col).copied().unwrap_or(false))
                .map(|(i, _)| i)
                .collect();
            if gate {
                let mut candidates: Vec<(WideDecimal, usize)> = Vec::new();
                for row in signalled {
                    if book.positions.iter().any(|p| p.row == row) {
                        continue;
                    }
                    candidates.push((rank_of(panel, row, signal_col)?, row));
                }
                if !candidates.is_empty() {
                    // `sort(key=(-rank, row))`.
                    candidates.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
                    let marked = mark_equity(panel, &book.positions, &book.cash, signal_col)?;
                    let mut taken = 0_i64;
                    for (_, row) in candidates {
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
                        let bar = bar_at(panel, row, col)?;
                        let Some(fill) = bar.open.clone().filter(|o| *o > WideDecimal::zero())
                        else {
                            bump("no_bar", 1);
                            continue;
                        };
                        if bar.high.as_ref() == Some(&fill) && bar.low == bar.high {
                            bump("locked", 1);
                            continue;
                        }
                        let stop = wide::initial_stop(&fill, exits_config, &tick)?;
                        let turnover_avg = Panel::cell(&panel.turnover_avg_20, row, signal_col)?;
                        let turnover = if turnover_avg.is_finite() {
                            Some(to_dec(&dec(turnover_avg)?)?)
                        } else {
                            None
                        };
                        let sized = size_entry(
                            to_dec(&marked)?,
                            to_dec(&book.cash)?,
                            to_dec(&fill.mul(&one().add(&cost)))?,
                            to_dec(&stop)?,
                            turnover,
                            sizing,
                            Decimal::ONE,
                        )?;
                        if !sized.placed() {
                            let short_of_cash = book.cash < min_trade
                                || sized.refusal != Some(SizeRefusal::BelowMinTradeValue);
                            bump(if short_of_cash { "cash" } else { "turnover" }, 1);
                            continue;
                        }
                        let entry = WideDecimal::from(sized.entry_price);
                        book.cash = book
                            .cash
                            .sub(&entry.mul(&WideDecimal::from(sized.quantity)));
                        book.positions.push(Pos {
                            row,
                            quantity: sized.quantity,
                            entry,
                            entry_col: col,
                            stop: stop.clone(),
                            initial_stop: stop,
                            high_since: fill,
                            cap: sized.cap,
                        });
                        taken += 1;
                    }
                }
            } else {
                bump(
                    "gate_shut",
                    i64::try_from(signalled.len()).unwrap_or(i64::MAX),
                );
            }
        }

        // --- 3. the fill-day stop, then 4. the ratchet and the close -------------------------
        let rows: Vec<usize> = book.positions.iter().map(|p| p.row).collect();
        for row in rows {
            let Some(position) = book.positions.iter().find(|p| p.row == row).cloned() else {
                continue;
            };
            let bar = bar_at(panel, row, col)?;
            if bar.close.is_none() {
                continue;
            }
            let open_position = as_open_position(panel, &position)?;
            if position.entry_col == col
                && let Some(day_zero) = wide::fill_day_stop(&open_position, &bar)
                && let Some(price) = day_zero.price
            {
                book.close(row, col, &price, ExitReason::StopDay0)?;
                continue;
            }
            let Some(moved) = wide::ratchet(&open_position, &bar, exits_config, &tick)? else {
                continue;
            };
            if moved.clamped_below_stop {
                clamped_below_stop += 1;
            }
            if let Some(p) = book.positions.iter_mut().find(|p| p.row == row) {
                p.high_since = moved.high_since;
                p.stop = moved.next_trigger;
            }
        }

        equity[col] = mark_equity(panel, &book.positions, &book.cash, col)?;
        open_count[col] = i64::try_from(book.positions.len()).unwrap_or(i64::MAX);
    }

    let rows: Vec<usize> = book.positions.iter().map(|p| p.row).collect();
    for row in rows {
        let Some(position) = book.positions.iter().find(|p| p.row == row).cloned() else {
            continue;
        };
        let mark = Panel::cell(&panel.close, row, last_index)?;
        let price = if mark.is_finite() {
            dec(mark)?
        } else {
            position.entry.clone()
        };
        book.close(row, last_index, &price, ExitReason::EndOfRun)?;
    }
    equity[last_index] = book.cash.clone();

    let window = |n: usize| first.min(n)..(last_index + 1).max(first.min(n));
    Ok(BacktestResult {
        params: params.clone(),
        trades: book.trades,
        sessions: panel.sessions[window(total_sessions)].to_vec(),
        equity: equity[window(total_sessions)].to_vec(),
        open_positions: open_count[window(total_sessions)].to_vec(),
        skipped,
        clamped_below_stop,
    })
}

/// `float(Decimal)`: correctly rounded, as parsing the decimal's digits is.
fn to_f64(x: &WideDecimal) -> CoreResult<f64> {
    x.to_sci_string()
        .parse::<f64>()
        .map_err(|e| CoreError::Arithmetic(format!("float({x}): {e}")))
}

/// Python's `float ** float`, raising where Python raises (R2.2's `py_pow`).
fn py_pow(base: f64, exponent: f64) -> CoreResult<f64> {
    if base < 0.0 && exponent.is_finite() && exponent.fract() != 0.0 {
        return Err(CoreError::Arithmetic(format!(
            "({}) ** {} is complex: Decimal(str(complex)) is InvalidOperation",
            py_float_repr(base),
            py_float_repr(exponent)
        )));
    }
    if base == 0.0 && exponent < 0.0 {
        return Err(CoreError::Arithmetic(
            "0.0 cannot be raised to a negative power".to_owned(),
        ));
    }
    let value = base.powf(exponent);
    if value.is_infinite() && base.is_finite() && exponent.is_finite() {
        return Err(CoreError::Arithmetic(format!(
            "({}) ** {}: numerical result out of range",
            py_float_repr(base),
            py_float_repr(exponent)
        )));
    }
    Ok(value)
}

/// `_annualised_pct`: `total ** (1 / years) - 1`, as a percent, through a float.
fn annualised_pct(total_ratio: &WideDecimal, years: &WideDecimal) -> CoreResult<WideDecimal> {
    if *years <= WideDecimal::zero() {
        return Ok(WideDecimal::zero());
    }
    let grown = py_pow(to_f64(total_ratio)?, 1.0 / to_f64(years)?)?;
    Ok(dec(grown)?.sub(&one()).mul(&hundred()))
}

mod numpy {
    //! numpy's float64 reductions, in its own summation order (R2.2).

    /// numpy's `PW_BLOCKSIZE`.
    const PW_BLOCKSIZE: usize = 128;

    /// numpy's `pairwise_sum`.
    pub(super) fn pairwise_sum(a: &[f64]) -> f64 {
        let n = a.len();
        if n < 8 {
            let mut res = 0.0;
            for x in a {
                res += x;
            }
            res
        } else if n <= PW_BLOCKSIZE {
            let mut r = [0.0f64; 8];
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

fn count_f64(n: usize) -> f64 {
    u32::try_from(n).map_or(f64::MAX, f64::from)
}

fn wide_count(n: usize) -> WideDecimal {
    WideDecimal::from(n)
}

/// Ports `summarise`: `04` §12's statistics; `None` for a run with fewer than two non-zero
/// equity points or no sessions.
///
/// # Errors
/// `zip(strict=True)`'s `ValueError` on a curve whose length is not the sessions', and
/// [`CoreError::Arithmetic`] where `Decimal` or `**` would raise (zero slots, a negative
/// growth ratio).
#[allow(clippy::too_many_lines)]
pub fn summarise(result: &BacktestResult) -> CoreResult<Option<BacktestStats>> {
    let zero = WideDecimal::zero();
    let curve: Vec<&WideDecimal> = result.equity.iter().filter(|v| **v != zero).collect();
    let (Some(start), Some(end)) = (result.sessions.first(), result.sessions.last()) else {
        return Ok(None);
    };
    if curve.len() < 2 {
        return Ok(None);
    }
    let (Some(first_point), Some(last_point)) = (curve.first(), curve.last()) else {
        return Ok(None);
    };
    let days_a_year: WideDecimal = DAYS_A_YEAR.parse().map_err(arith)?;
    let years = WideDecimal::from((*end - *start).num_days())
        .div(&days_a_year)
        .map_err(arith)?;
    let cagr = annualised_pct(&last_point.div(first_point).map_err(arith)?, &years)?;

    if result.sessions.len() != result.equity.len() {
        return Err(CoreError::InvalidInput(
            "zip() argument 2 is shorter than argument 1".to_owned(),
        ));
    }
    let mut peak = (*first_point).clone();
    let mut drawdown = WideDecimal::zero();
    let (mut trough_at, mut peak_at, mut running_peak_at) = (*start, *start, *start);
    for (session, value) in result.sessions.iter().zip(&result.equity) {
        if *value == zero {
            continue;
        }
        if *value > peak {
            peak = value.clone();
            running_peak_at = *session;
        }
        let fall = value.div(&peak).map_err(arith)?.sub(&one()).mul(&hundred());
        if fall < drawdown {
            drawdown = fall;
            trough_at = *session;
            peak_at = running_peak_at;
        }
    }

    let mut ratios: Vec<f64> = Vec::new();
    for pair in curve.windows(2) {
        if pair[0].is_zero() {
            continue;
        }
        ratios.push(to_f64(&pair[1].div(pair[0]).map_err(arith)?.sub(&one()))?);
    }
    let deviation = if ratios.len() > 1 {
        numpy::std_ddof1(&ratios)
    } else {
        0.0
    };
    let sharpe_ratio = if deviation > 0.0 {
        numpy::mean(&ratios) / deviation * count_f64(252).sqrt()
    } else {
        f64::NAN
    };

    let wins: Vec<&BacktestTrade> = result.trades.iter().filter(|t| t.pnl_inr > zero).collect();
    let losses: Vec<&BacktestTrade> = result.trades.iter().filter(|t| t.pnl_inr <= zero).collect();
    let gross_win = WideDecimal::sum(wins.iter().map(|t| &t.pnl_inr));
    let gross_loss = WideDecimal::sum(losses.iter().map(|t| &t.pnl_inr)).neg();
    let count = result.trades.len();
    let sessions_seen = if result.open_positions.is_empty() {
        1
    } else {
        result.open_positions.len()
    };
    let average_open = WideDecimal::from(result.open_positions.iter().sum::<i64>())
        .div(&wide_count(sessions_seen))
        .map_err(arith)?;
    let slots = WideDecimal::from(result.params.config.sizing.max_slots);
    let mean_return = |trades: &[&BacktestTrade]| -> CoreResult<f64> {
        if trades.is_empty() {
            return Ok(0.0);
        }
        to_f64(
            &WideDecimal::sum(trades.iter().map(|t| &t.return_pct))
                .div(&wide_count(trades.len()))
                .map_err(arith)?,
        )
    };
    let all: Vec<&BacktestTrade> = result.trades.iter().collect();
    let paise: WideDecimal = PAISE.parse().map_err(arith)?;

    Ok(Some(BacktestStats {
        start: *start,
        end: *end,
        final_equity_inr: last_point
            .quantize(paise.exponent(), Rounding::HalfEven)
            .map_err(arith)?,
        cagr_pct: to_f64(&cagr)?,
        max_drawdown_pct: to_f64(&drawdown)?,
        drawdown_peak_on: peak_at,
        drawdown_trough_on: trough_at,
        calmar: if drawdown < zero {
            Some(cagr.div(&drawdown.abs()).map_err(arith)?)
        } else {
            None
        },
        sharpe_ratio,
        trades: i64::try_from(count).unwrap_or(i64::MAX),
        win_rate_pct: if count == 0 {
            0.0
        } else {
            count_f64(wins.len()) / count_f64(count) * 100.0
        },
        profit_factor: if gross_loss > zero {
            Some(gross_win.div(&gross_loss).map_err(arith)?)
        } else {
            None
        },
        avg_win_pct: mean_return(&wins)?,
        avg_loss_pct: mean_return(&losses)?,
        avg_trade_pct: mean_return(&all)?,
        avg_hold_sessions: if count == 0 {
            WideDecimal::zero()
        } else {
            WideDecimal::from(result.trades.iter().map(|t| t.hold_sessions).sum::<i64>())
                .div(&wide_count(count))
                .map_err(arith)?
        },
        exposure_pct: to_f64(&average_open.div(&slots).map_err(arith)?.mul(&hundred()))?,
        avg_open_positions: average_open,
        years,
        by_reason: ExitReason::ALL
            .iter()
            .filter_map(|reason| {
                let n = result.trades.iter().filter(|t| t.reason == *reason).count();
                (n > 0).then(|| (reason.value().to_owned(), i64::try_from(n).unwrap_or(0)))
            })
            .collect(),
    }))
}

/// Ports `yearly`: calendar-year returns off the equity curve (each year opening on the
/// previous year's close), and the trades that closed in each.
///
/// # Errors
/// `zip(strict=True)`'s `ValueError` on mismatched lengths; [`CoreError::Arithmetic`] where
/// `Decimal` would raise.
pub fn yearly(result: &BacktestResult) -> CoreResult<Vec<YearRow>> {
    if result.sessions.len() != result.equity.len() {
        return Err(CoreError::InvalidInput(
            "zip() argument 2 is shorter than argument 1".to_owned(),
        ));
    }
    let zero = WideDecimal::zero();
    let mut by_year: BTreeMap<i32, Vec<&WideDecimal>> = BTreeMap::new();
    for (session, value) in result.sessions.iter().zip(&result.equity) {
        if *value != zero {
            by_year.entry(session.year()).or_default().push(value);
        }
    }
    let mut opening = result.equity.first().cloned().unwrap_or_default();
    let mut out = Vec::new();
    for (year, values) in by_year {
        let Some(closing) = values.last() else {
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
                to_f64(
                    &closing
                        .div(&opening)
                        .map_err(arith)?
                        .sub(&one())
                        .mul(&hundred()),
                )?
            },
            trades: i64::try_from(closed.len()).unwrap_or(i64::MAX),
            win_rate_pct: if closed.is_empty() {
                0.0
            } else {
                count_f64(wins) / count_f64(closed.len()) * 100.0
            },
        });
        opening = (*closing).clone();
    }
    Ok(out)
}
