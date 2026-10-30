//! Ports `baskfy_core.swing.backtest` — the EOD backtest of the swing method (`04` §11).
//!
//! Pure (law 1): bars, the calendar, the index and the delisting dates in; a result out; no
//! clock, no I/O. Every rule is the function the live book calls — `detect_setups`,
//! `build_entries`, `manage` (baskfy-core, golden-graded) and the nightly's own breadth, index
//! and gate readers (`steps::sleeves::for_backtests`, wave 5, graded by the nightly) — so the
//! backtest cannot disagree with the desk about what the method is.
//!
//! It lives in the worker rather than in `baskfy-core::swing` because it has no goldens yet and
//! leans on the worker's ports of `market.breadth_snapshot`/`ema`/`market_gate`; it is graded end
//! to end, byte for byte, by `task_diff.py` against Python's task (DECISIONS-RS, lane
//! `sleeve_backtests`). Every `Decimal` follows Python's: the money and prices exact, each
//! division in the 28-digit context ([`WideDecimal`]), `quantize` with the rounding Python names
//! (and the context's half-even where it names none), `str()` keeping the exponent.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::swing::market::{ExposureTier, MarketGate, exposure_tier};
use baskfy_core::swing::plan::{SkipReason, SwingAccount, WatchItem, build_entries};
use baskfy_core::swing::setups::{
    CandidateStatus, Setup, SwingConfig, TRADEABLE_SETUPS, detect_setups, with_swing_indicators,
};
use baskfy_core::swing::stops::{Action, ActionKind, ActionReason, DailyBar, OpenPosition, manage};
use baskfy_db::OrderedJson;
use chrono::{Datelike, NaiveDate};
use polars::prelude::{
    BooleanChunked, DataFrame, IntoColumn, NamedFrom, NewChunkedArray, Series, SortMultipleOptions,
};
use rust_decimal::Decimal;

use super::common::{dec_str, iso_date};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::sleeves::for_backtests::{
    BreadthRow, BreadthSnapshot, INDEX_HISTORY_SESSIONS, IndexReading, breadth_snapshot, core_err,
    ema, is_liquid, market_gate, pl_err, value_err,
};

/// `CAVEATS`.
pub(super) const CAVEATS: [&str; 4] = [
    "No intraday data (so no ORH filter — real entries are more selective).",
    "No circuit history before 2020.",
    "Survivorship handled by instrument.delisted_on.",
    "Where upper_circuit is absent no lock is assumed, so a name that was locked may have been \
     entered here.",
];

/// `INDEX_ABSENT_CAVEAT`.
pub(super) const INDEX_ABSENT_CAVEAT: &str =
    "No index series was supplied, so the gate is breadth-only and the index rule was not applied.";

/// `baskfy_core.backtest.PRICE_EXPONENT` / `MONEY_EXPONENT` / `MISSING_BAR_TOLERANCE_DAYS`.
const PRICE_EXP: i64 = -4;
const MONEY_EXP: i64 = -2;
const MISSING_BAR_TOLERANCE_DAYS: i64 = 5;
/// `baskfy_core.windows.HIGH_1Y_BARS`.
const HIGH_1Y_BARS: usize = 252;
/// `_MIN_WINDOW_BARS`.
const MIN_WINDOW_BARS: usize = 2;

fn arith(e: baskfy_core::decimal::DecimalError) -> WorkerError {
    WorkerError::python("InvalidOperation", e.0)
}

fn wide(d: Decimal) -> WideDecimal {
    WideDecimal::from(d)
}

fn narrow(w: &WideDecimal) -> Result<Decimal, WorkerError> {
    w.try_to_decimal().map_err(arith)
}

/// `value.quantize(Decimal("1E{exp}"), rounding=mode)`, the exponent kept.
fn quantize(value: Decimal, exp: i64, mode: Rounding) -> Result<Decimal, WorkerError> {
    narrow(&wide(value).quantize(exp, mode).map_err(arith)?)
}

/// `a / b` in the default context.
fn div(a: &WideDecimal, b: &WideDecimal) -> Result<WideDecimal, WorkerError> {
    a.div(b).map_err(|e| {
        let class = if e.0.contains("DivisionByZero") {
            "DivisionByZero"
        } else {
            "InvalidOperation"
        };
        WorkerError::python(class, e.0)
    })
}

/// `_price(value)`: `Decimal(value)` — the float's exact binary value — quantized half-up to
/// four places.
pub(super) fn price(value: f64) -> Result<Decimal, WorkerError> {
    if !value.is_finite() {
        return Err(WorkerError::python(
            "InvalidOperation",
            "[<class 'decimal.InvalidOperation'>]",
        ));
    }
    // Every double's exact decimal expansion has at most 1074 fractional digits; Rust prints
    // it exactly at any precision, so 1100 places are the exact value, padded with zeros.
    let exact: WideDecimal = format!("{value:.1100}").parse().map_err(arith)?;
    narrow(&exact.quantize(PRICE_EXP, Rounding::HalfUp).map_err(arith)?)
}

/// `_decimal(value)`: `Decimal(repr(float(value)))`.
fn decimal_of(value: f64) -> Result<Decimal, WorkerError> {
    baskfy_core::py::decimal_from_float_str(value).map_err(|e| value_err(e.0))
}

fn money(value: Decimal) -> Result<Decimal, WorkerError> {
    quantize(value, MONEY_EXP, Rounding::HalfUp)
}

fn checked(value: Option<Decimal>, what: &str) -> Result<Decimal, WorkerError> {
    value.ok_or_else(|| WorkerError::python("Overflow", format!("{what} overflows")))
}

// ---------------------------------------------------------------------------------------------
// The journal (`baskfy_core.swing.journal`)
// ---------------------------------------------------------------------------------------------

/// `ClosedTrade`'s inputs.
#[derive(Debug, Clone)]
pub(super) struct ClosedTrade {
    pub symbol: String,
    pub setup: String,
    pub entry_date: NaiveDate,
    pub exit_date: NaiveDate,
    pub entry: Decimal,
    pub initial_stop: Decimal,
    pub exit_avg: Decimal,
    pub quantity: i64,
}

impl ClosedTrade {
    /// `r_multiple`: `((exit_avg - entry) / (entry - initial_stop)).quantize(0.01)` (half-even).
    fn r_multiple(&self) -> Result<Decimal, WorkerError> {
        let one_r = checked(self.entry.checked_sub(self.initial_stop), "one R")?;
        if one_r <= Decimal::ZERO {
            return Err(value_err(format!(
                "{}: initial stop {} not below entry",
                self.symbol,
                dec_str(self.initial_stop)
            )));
        }
        let gain = checked(self.exit_avg.checked_sub(self.entry), "the gain")?;
        let r = div(&wide(gain), &wide(one_r))?;
        narrow(&r.quantize(MONEY_EXP, Rounding::HalfEven).map_err(arith)?)
    }

    /// `pnl_inr`: `((exit_avg - entry) * quantity).quantize(0.01)`.
    fn pnl_inr(&self) -> Result<Decimal, WorkerError> {
        let gain = checked(self.exit_avg.checked_sub(self.entry), "the gain")?;
        let total = wide(gain).mul(&WideDecimal::from(self.quantity));
        narrow(
            &total
                .quantize(MONEY_EXP, Rounding::HalfEven)
                .map_err(arith)?,
        )
    }
}

/// `JournalStats`.
#[derive(Debug, Clone)]
pub(super) struct JournalStats {
    pub trades: i64,
    pub win_rate_pct: Decimal,
    pub avg_win_r: Decimal,
    pub avg_loss_r: Decimal,
    pub expectancy_r: Decimal,
    pub profit_factor: Option<Decimal>,
    pub net_r: Decimal,
    pub largest_win_r: Decimal,
    pub largest_loss_r: Decimal,
    pub current_loss_streak: i64,
}

fn dsum(values: &[Decimal]) -> Result<Decimal, WorkerError> {
    let mut total = Decimal::ZERO;
    for v in values {
        total = checked(total.checked_add(*v), "a sum")?;
    }
    Ok(total)
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `_mean`: the mean to two places (half-even), `Decimal(0)` for none.
fn mean(values: &[Decimal]) -> Result<Decimal, WorkerError> {
    if values.is_empty() {
        return Ok(Decimal::ZERO);
    }
    let m = div(
        &wide(dsum(values)?),
        &WideDecimal::from(len_i64(values.len())),
    )?;
    narrow(&m.quantize(MONEY_EXP, Rounding::HalfEven).map_err(arith)?)
}

/// `max(values, default=Decimal(0))`: the first of the largest.
fn first_max(values: &[Decimal]) -> Decimal {
    let mut best: Option<Decimal> = None;
    for v in values {
        if best.is_none_or(|b| *v > b) {
            best = Some(*v);
        }
    }
    best.unwrap_or(Decimal::ZERO)
}

fn first_min(values: &[Decimal]) -> Decimal {
    let mut best: Option<Decimal> = None;
    for v in values {
        if best.is_none_or(|b| *v < b) {
            best = Some(*v);
        }
    }
    best.unwrap_or(Decimal::ZERO)
}

/// `summarize`: the book's statistics in R; an empty input is a zero row.
pub(super) fn summarize(trades: &[ClosedTrade]) -> Result<JournalStats, WorkerError> {
    let rs = trades
        .iter()
        .map(ClosedTrade::r_multiple)
        .collect::<Result<Vec<_>, _>>()?;
    let wins: Vec<Decimal> = rs.iter().copied().filter(|r| *r > Decimal::ZERO).collect();
    let losses: Vec<Decimal> = rs.iter().copied().filter(|r| *r <= Decimal::ZERO).collect();
    let gross_win = dsum(&wins)?;
    let gross_loss = -dsum(&losses)?;
    let streak = len_i64(rs.iter().rev().take_while(|r| **r < Decimal::ZERO).count());
    let count = len_i64(rs.len());
    let win_rate_pct = if count == 0 {
        Decimal::ZERO
    } else {
        let share = div(
            &WideDecimal::from(len_i64(wins.len())),
            &WideDecimal::from(count),
        )?
        .mul(&WideDecimal::from(100_i64));
        narrow(
            &share
                .quantize(MONEY_EXP, Rounding::HalfEven)
                .map_err(arith)?,
        )?
    };
    let profit_factor = if gross_loss > Decimal::ZERO {
        let pf = div(&wide(gross_win), &wide(gross_loss))?;
        Some(narrow(
            &pf.quantize(MONEY_EXP, Rounding::HalfEven).map_err(arith)?,
        )?)
    } else {
        None
    };
    Ok(JournalStats {
        trades: count,
        win_rate_pct,
        avg_win_r: mean(&wins)?,
        avg_loss_r: mean(&losses)?,
        expectancy_r: mean(&rs)?,
        profit_factor,
        net_r: quantize(dsum(&rs)?, MONEY_EXP, Rounding::HalfEven)?,
        largest_win_r: first_max(&rs),
        largest_loss_r: first_min(&rs),
        current_loss_streak: streak,
    })
}

/// `exit_average`: the share-weighted average of `(quantity, price)` exit fills.
fn exit_average(fills: &[(i64, Decimal)]) -> Result<Decimal, WorkerError> {
    let total: i64 = fills.iter().map(|(q, _)| *q).sum();
    if total <= 0 {
        return Err(value_err("no exit quantity"));
    }
    let mut weighted = WideDecimal::zero();
    for (q, p) in fills {
        weighted = weighted.add(&WideDecimal::from(*q).mul(&wide(*p)));
    }
    let avg = div(&weighted, &WideDecimal::from(total))?;
    narrow(&avg.quantize(MONEY_EXP, Rounding::HalfEven).map_err(arith)?)
}

fn stats_json(stats: &JournalStats) -> OrderedJson {
    object([
        ("trades", int(stats.trades)),
        ("win_rate_pct", s(dec_str(stats.win_rate_pct))),
        ("avg_win_r", s(dec_str(stats.avg_win_r))),
        ("avg_loss_r", s(dec_str(stats.avg_loss_r))),
        ("expectancy_r", s(dec_str(stats.expectancy_r))),
        (
            "profit_factor",
            stats
                .profit_factor
                .map_or(OrderedJson::Null, |p| s(dec_str(p))),
        ),
        ("net_r", s(dec_str(stats.net_r))),
        ("largest_win_r", s(dec_str(stats.largest_win_r))),
        ("largest_loss_r", s(dec_str(stats.largest_loss_r))),
        ("current_loss_streak", int(stats.current_loss_streak)),
    ])
}

// ---------------------------------------------------------------------------------------------
// Params, trades, the result
// ---------------------------------------------------------------------------------------------

/// `BacktestParams`.
#[derive(Debug, Clone)]
pub(super) struct Params {
    pub start: NaiveDate,
    pub end: NaiveDate,
    pub sleeve_inr: Decimal,
    pub cost_pct_per_side: Decimal,
    pub config: SwingConfig,
    pub index_slug: Option<String>,
}

/// `params_json(params)` / `to_json()["params"]`: `_plain(asdict(params))`.
pub(super) fn params_json(params: &Params) -> Result<OrderedJson, WorkerError> {
    // serde writes the struct's fields in declaration order (= the dataclass's), a float as a
    // float (`4.0`), the `Decimal` as its string and a tuple as a list: `_plain(asdict(...))`.
    let text = serde_json::to_string(&params.config).map_err(|e| value_err(e.to_string()))?;
    let config = OrderedJson::parse(&text).map_err(|e| value_err(e.to_string()))?;
    Ok(object([
        ("start", s(iso_date(params.start))),
        ("end", s(iso_date(params.end))),
        ("sleeve_inr", s(dec_str(params.sleeve_inr))),
        ("cost_pct_per_side", s(dec_str(params.cost_pct_per_side))),
        ("config", config),
        (
            "index_slug",
            params
                .index_slug
                .as_ref()
                .map_or(OrderedJson::Null, |t| s(t.clone())),
        ),
    ]))
}

/// `BacktestTrade`.
#[derive(Debug, Clone)]
pub(super) struct Trade {
    pub closed: ClosedTrade,
    pub r_multiple: Decimal,
    pub pnl_inr: Decimal,
    pub close_reason: String,
}

fn trade_json(t: &Trade) -> OrderedJson {
    let c = &t.closed;
    object([
        ("symbol", s(c.symbol.clone())),
        ("setup", s(c.setup.clone())),
        ("entry_date", s(iso_date(c.entry_date))),
        ("exit_date", s(iso_date(c.exit_date))),
        ("entry", s(dec_str(c.entry))),
        ("initial_stop", s(dec_str(c.initial_stop))),
        ("exit_avg", s(dec_str(c.exit_avg))),
        ("quantity", int(c.quantity)),
        ("r_multiple", s(dec_str(t.r_multiple))),
        ("pnl_inr", s(dec_str(t.pnl_inr))),
        ("close_reason", s(t.close_reason.clone())),
    ])
}

/// `DrawdownSummary`.
#[derive(Debug, Clone)]
pub(super) struct Drawdown {
    pub max_pct: Decimal,
    pub peak: Decimal,
    pub trough: Decimal,
    pub trough_date: Option<NaiveDate>,
    pub locked_sessions: i64,
}

/// `GateMode`, in the order `_Run` builds its books.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum GateMode {
    GateOff,
    BreadthOnly,
    Full,
}

impl GateMode {
    pub(super) const fn value(self) -> &'static str {
        match self {
            Self::GateOff => "gate_off",
            Self::BreadthOnly => "breadth_only",
            Self::Full => "full",
        }
    }

    const fn definition(self) -> &'static str {
        match self {
            Self::GateOff => {
                "Gate off: the market gate is GREEN every session; the ladder and the drawdown \
                 lock-out still apply."
            }
            Self::BreadthOnly => "Breadth only: the gate reads breadth and no index.",
            Self::Full => {
                "Full: the gate reads breadth and the index rule (the 10-day average over the \
                 20-day)."
            }
        }
    }
}

/// `GateCell`.
#[derive(Debug, Clone)]
pub(super) struct GateCell {
    pub entered: i64,
    pub net_r: Decimal,
    pub expectancy_r: Decimal,
    pub win_rate_pct: Decimal,
    pub max_drawdown_pct: Option<Decimal>,
}

fn cell_json(c: &GateCell) -> OrderedJson {
    object([
        ("entered", int(c.entered)),
        ("net_r", s(dec_str(c.net_r))),
        ("expectancy_r", s(dec_str(c.expectancy_r))),
        ("win_rate_pct", s(dec_str(c.win_rate_pct))),
        (
            "max_drawdown_pct",
            c.max_drawdown_pct
                .map_or(OrderedJson::Null, |d| s(dec_str(d))),
        ),
    ])
}

/// `GateContribution`.
#[derive(Debug, Clone)]
pub(super) struct Contribution {
    pub overall: GateCell,
    pub by_year: Vec<(i32, GateCell)>,
    pub by_setup: Vec<(String, GateCell)>,
}

/// `GateComparison`.
#[derive(Debug, Clone)]
pub(super) struct Comparison {
    pub index_supplied: bool,
    pub primary: GateMode,
    pub overall: Vec<(GateMode, GateCell)>,
    pub by_year: Vec<(i32, Vec<(GateMode, GateCell)>)>,
    pub by_setup: Vec<(String, Vec<(GateMode, GateCell)>)>,
    pub breadth: Contribution,
    pub index_rule: Option<Contribution>,
}

fn cells_json(cells: &[(GateMode, GateCell)]) -> OrderedJson {
    object(cells.iter().map(|(m, c)| (m.value(), cell_json(c))))
}

fn contribution_json(c: &Contribution) -> OrderedJson {
    object([
        ("overall", cell_json(&c.overall)),
        (
            "by_year",
            object(
                c.by_year
                    .iter()
                    .map(|(y, cell)| (y.to_string(), cell_json(cell))),
            ),
        ),
        (
            "by_setup",
            object(
                c.by_setup
                    .iter()
                    .map(|(k, cell)| (k.clone(), cell_json(cell))),
            ),
        ),
    ])
}

fn comparison_json(c: &Comparison) -> OrderedJson {
    object([
        ("index_supplied", OrderedJson::Bool(c.index_supplied)),
        ("primary", s(c.primary.value())),
        (
            "modes",
            object(
                c.overall
                    .iter()
                    .map(|(m, _)| (m.value(), s(m.definition()))),
            ),
        ),
        ("overall", cells_json(&c.overall)),
        (
            "by_year",
            object(
                c.by_year
                    .iter()
                    .map(|(y, cells)| (y.to_string(), cells_json(cells))),
            ),
        ),
        (
            "by_setup",
            object(
                c.by_setup
                    .iter()
                    .map(|(k, cells)| (k.clone(), cells_json(cells))),
            ),
        ),
        (
            "contribution",
            object([
                ("breadth", contribution_json(&c.breadth)),
                (
                    "index_rule",
                    c.index_rule
                        .as_ref()
                        .map_or(OrderedJson::Null, contribution_json),
                ),
            ]),
        ),
    ])
}

/// The funnel's keys, in `_FUNNEL_KEYS`' order.
const FUNNEL_KEYS: [&str; 20] = [
    "sessions",
    "detected",
    "candidates",
    "entered",
    "skipped_locked",
    "skipped_drawdown",
    "skipped_gate",
    "skipped_held",
    "skipped_no_next_session",
    "skipped_no_bar",
    "skipped_no_trigger",
    "skipped_session_cap",
    "skipped_tier",
    "skipped_size",
    "skipped_exposure",
    "skipped_not_tradeable",
    "closed",
    "closed_no_bar",
    "closed_delisted",
    "closed_end_of_run",
];

/// A `dict.fromkeys(_FUNNEL_KEYS, 0)`, in order.
#[derive(Debug, Clone)]
pub(super) struct Funnel(Vec<(&'static str, i64)>);

impl Funnel {
    fn new() -> Self {
        Self(FUNNEL_KEYS.iter().map(|k| (*k, 0)).collect())
    }

    fn add(&mut self, key: &str, n: i64) -> Result<(), WorkerError> {
        let slot = self
            .0
            .iter_mut()
            .find(|(k, _)| *k == key)
            .ok_or_else(|| WorkerError::python("KeyError", format!("'{key}'")))?;
        slot.1 += n;
        Ok(())
    }

    pub(super) fn json(&self) -> OrderedJson {
        object(self.0.iter().map(|(k, v)| (*k, int(*v))))
    }
}

fn skip_key(reason: SkipReason) -> &'static str {
    match reason {
        SkipReason::DrawdownLockout => "skipped_drawdown",
        SkipReason::GateRed => "skipped_gate",
        SkipReason::AlreadyHeld => "skipped_held",
        SkipReason::LockedUpperCircuit => "skipped_locked",
        SkipReason::SessionCap => "skipped_session_cap",
        SkipReason::TierFull => "skipped_tier",
        SkipReason::SizeRefused => "skipped_size",
        SkipReason::ExposureFull => "skipped_exposure",
        SkipReason::NotTradeableSetup => "skipped_not_tradeable",
    }
}

/// `BacktestResult`.
#[derive(Debug, Clone)]
pub(super) struct BacktestResult {
    pub params: Params,
    pub trades: Vec<Trade>,
    pub stats: JournalStats,
    pub by_setup: Vec<(String, JournalStats)>,
    pub by_year: Vec<(i32, JournalStats)>,
    pub equity_curve: Vec<(NaiveDate, Decimal)>,
    pub funnel: Funnel,
    pub ladder: Vec<(NaiveDate, &'static str, i64)>,
    pub drawdown: Drawdown,
    pub comparison: Comparison,
}

impl BacktestResult {
    fn caveats(&self) -> Vec<&'static str> {
        let mut out = CAVEATS.to_vec();
        if !self.comparison.index_supplied {
            out.push(INDEX_ABSENT_CAVEAT);
        }
        out
    }

    /// `to_json()`.
    pub(super) fn to_json(&self) -> Result<OrderedJson, WorkerError> {
        let d = &self.drawdown;
        Ok(object([
            ("params", params_json(&self.params)?),
            (
                "trades",
                OrderedJson::Array(self.trades.iter().map(trade_json).collect()),
            ),
            ("stats", stats_json(&self.stats)),
            (
                "by_setup",
                object(
                    self.by_setup
                        .iter()
                        .map(|(k, st)| (k.clone(), stats_json(st))),
                ),
            ),
            (
                "by_year",
                object(
                    self.by_year
                        .iter()
                        .map(|(y, st)| (y.to_string(), stats_json(st))),
                ),
            ),
            (
                "equity_curve",
                OrderedJson::Array(
                    self.equity_curve
                        .iter()
                        .map(|(day, eq)| {
                            OrderedJson::Array(vec![s(iso_date(*day)), s(dec_str(*eq))])
                        })
                        .collect(),
                ),
            ),
            ("funnel", self.funnel.json()),
            (
                "ladder",
                OrderedJson::Array(
                    self.ladder
                        .iter()
                        .map(|(day, gate, level)| {
                            OrderedJson::Array(vec![s(iso_date(*day)), s(*gate), int(*level)])
                        })
                        .collect(),
                ),
            ),
            (
                "drawdown",
                object([
                    ("max_pct", s(dec_str(d.max_pct))),
                    ("peak", s(dec_str(d.peak))),
                    ("trough", s(dec_str(d.trough))),
                    (
                        "trough_date",
                        d.trough_date.map_or(OrderedJson::Null, |t| s(iso_date(t))),
                    ),
                    ("locked_sessions", int(d.locked_sessions)),
                ]),
            ),
            ("comparison", comparison_json(&self.comparison)),
            (
                "caveats",
                OrderedJson::Array(self.caveats().into_iter().map(s).collect()),
            ),
        ]))
    }
}

// ---------------------------------------------------------------------------------------------
// The panel and the index
// ---------------------------------------------------------------------------------------------

const PANEL_COLUMNS: [&str; 6] = ["open", "high", "low", "close", "ma_fast", "ma_slow"];

/// `_Panel`: dense `(instrument, session)` arrays of the prices `manage` and the fills read.
struct Panel {
    instruments: Vec<i64>,
    sessions: usize,
    /// `PANEL_COLUMNS` order, each row-major `[instrument][session]`.
    columns: Vec<Vec<f64>>,
}

impl Panel {
    fn build(indicated: &DataFrame, calendar_days: &[i32]) -> Result<Self, WorkerError> {
        let ids = ints(indicated, "instrument_id")?;
        let days = days_of(indicated, "date")?;
        let mut instruments: Vec<i64> = ids.iter().flatten().copied().collect();
        instruments.sort_unstable();
        instruments.dedup();
        let sessions = calendar_days.len();
        let mut columns = Vec::with_capacity(PANEL_COLUMNS.len());
        let values: Vec<Vec<Option<f64>>> = PANEL_COLUMNS
            .iter()
            .map(|name| floats(indicated, name))
            .collect::<Result<_, _>>()?;
        for _ in PANEL_COLUMNS {
            columns.push(vec![f64::NAN; instruments.len() * sessions]);
        }
        for i in 0..indicated.height() {
            let (Some(id), Some(day)) = (ids[i], days[i]) else {
                continue;
            };
            let Ok(col) = calendar_days.binary_search(&day) else {
                continue;
            };
            let Ok(row) = instruments.binary_search(&id) else {
                continue;
            };
            for (k, column) in columns.iter_mut().enumerate() {
                column[row * sessions + col] = values[k][i].unwrap_or(f64::NAN);
            }
        }
        Ok(Self {
            instruments,
            sessions,
            columns,
        })
    }

    fn row_of(&self, instrument_id: i64) -> Result<usize, WorkerError> {
        self.instruments
            .binary_search(&instrument_id)
            .map_err(|_| WorkerError::python("KeyError", instrument_id.to_string()))
    }

    fn raw(&self, k: usize, row: usize, col: usize) -> f64 {
        self.columns[k][row * self.sessions + col]
    }

    fn has_bar(&self, row: usize, col: usize) -> bool {
        !self.raw(3, row, col).is_nan()
    }

    fn price(&self, k: usize, row: usize, col: usize) -> Result<Decimal, WorkerError> {
        price(self.raw(k, row, col))
    }

    fn optional(&self, k: usize, row: usize, col: usize) -> Result<Option<Decimal>, WorkerError> {
        let v = self.raw(k, row, col);
        if v.is_nan() {
            Ok(None)
        } else {
            price(v).map(Some)
        }
    }
}

const OPEN: usize = 0;
const HIGH: usize = 1;
const LOW: usize = 2;
const CLOSE: usize = 3;
const MA_FAST: usize = 4;
const MA_SLOW: usize = 5;

/// `_IndexSeries`: the index closes, sorted, read on or before each session.
struct IndexSeries {
    days: Vec<i32>,
    closes: Vec<f64>,
    fast: usize,
    slow: usize,
}

impl IndexSeries {
    fn build(index: &[(NaiveDate, f64)], config: &SwingConfig) -> Result<Self, WorkerError> {
        let mut rows: Vec<(i32, f64)> = index
            .iter()
            .filter(|(_, c)| !c.is_nan())
            .map(|(d, c)| (day_number(*d), *c))
            .collect();
        rows.sort_by_key(|r| r.0);
        if rows.windows(2).any(|w| w[0].0 == w[1].0) {
            return Err(value_err("index has two closes for one date"));
        }
        Ok(Self {
            days: rows.iter().map(|r| r.0).collect(),
            closes: rows.iter().map(|r| r.1).collect(),
            fast: usize::try_from(config.market.index_ma_fast).unwrap_or(usize::MAX),
            slow: usize::try_from(config.market.index_ma_slow).unwrap_or(usize::MAX),
        })
    }

    fn reading_on(&self, day: i32) -> Result<Option<IndexReading>, WorkerError> {
        let n = self.days.partition_point(|d| *d <= day);
        if n < self.slow {
            return Ok(None);
        }
        let history = usize::try_from(INDEX_HISTORY_SESSIONS).unwrap_or(usize::MAX);
        let window = &self.closes[n.saturating_sub(history)..n];
        let Some(&close) = window.last() else {
            return Ok(None);
        };
        Ok(Some(IndexReading {
            close,
            ma_fast: ema(window, self.fast)?,
            ma_slow: ema(window, self.slow)?,
        }))
    }
}

// ---------------------------------------------------------------------------------------------
// Frame helpers
// ---------------------------------------------------------------------------------------------

fn day_number(d: NaiveDate) -> i32 {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap_or_default();
    i32::try_from((d - epoch).num_days()).unwrap_or(i32::MAX)
}

fn series_of(df: &DataFrame, name: &str) -> Result<Series, WorkerError> {
    Ok(df
        .column(name)
        .map_err(|e| pl_err(&e))?
        .as_materialized_series()
        .clone())
}

fn floats(df: &DataFrame, name: &str) -> Result<Vec<Option<f64>>, WorkerError> {
    let s = series_of(df, name)?
        .cast(&polars::prelude::DataType::Float64)
        .map_err(|e| pl_err(&e))?;
    Ok(s.f64().map_err(|e| pl_err(&e))?.iter().collect())
}

fn ints(df: &DataFrame, name: &str) -> Result<Vec<Option<i64>>, WorkerError> {
    let s = series_of(df, name)?
        .cast(&polars::prelude::DataType::Int64)
        .map_err(|e| pl_err(&e))?;
    Ok(s.i64().map_err(|e| pl_err(&e))?.iter().collect())
}

fn days_of(df: &DataFrame, name: &str) -> Result<Vec<Option<i32>>, WorkerError> {
    let s = series_of(df, name)?;
    Ok(s.date()
        .map_err(|e| pl_err(&e))?
        .physical()
        .iter()
        .collect())
}

fn strings(df: &DataFrame, name: &str) -> Result<Vec<Option<String>>, WorkerError> {
    let s = series_of(df, name)?
        .cast(&polars::prelude::DataType::String)
        .map_err(|e| pl_err(&e))?;
    Ok(s.str()
        .map_err(|e| pl_err(&e))?
        .iter()
        .map(|v| v.map(str::to_owned))
        .collect())
}

fn bools(df: &DataFrame, name: &str) -> Result<Vec<Option<bool>>, WorkerError> {
    let s = series_of(df, name)?
        .cast(&polars::prelude::DataType::Boolean)
        .map_err(|e| pl_err(&e))?;
    Ok(s.bool().map_err(|e| pl_err(&e))?.iter().collect())
}

/// `_indicated`: the indicator frame plus `high_1y` (the rolling 252-bar high per instrument,
/// null until a year exists, filled with the bar's own high), sorted by `(date, instrument_id)`.
fn indicated(bars: &DataFrame, config: &SwingConfig) -> Result<DataFrame, WorkerError> {
    if !bars
        .get_column_names()
        .iter()
        .any(|c| c.as_str() == "symbol")
    {
        return Err(value_err(
            "bars need a symbol column; the journal is kept by symbol",
        ));
    }
    let mut frame = with_swing_indicators(bars, config).map_err(core_err)?;
    let ids = ints(&frame, "instrument_id")?;
    let highs = floats(&frame, "high")?;
    // `rolling_max(window_size=252, min_samples=252).over("instrument_id")`: per group, in the
    // frame's order; a null in the window does not count toward `min_samples`.
    let mut by_group: BTreeMap<Option<i64>, Vec<usize>> = BTreeMap::new();
    for (i, id) in ids.iter().enumerate() {
        by_group.entry(*id).or_default().push(i);
    }
    let mut high_1y: Vec<Option<f64>> = vec![None; frame.height()];
    for rows in by_group.values() {
        for (pos, &i) in rows.iter().enumerate() {
            if pos + 1 < HIGH_1Y_BARS {
                continue;
            }
            let window = &rows[pos + 1 - HIGH_1Y_BARS..=pos];
            let present: Vec<f64> = window.iter().filter_map(|&j| highs[j]).collect();
            if present.len() < HIGH_1Y_BARS {
                continue;
            }
            high_1y[i] = present.iter().copied().fold(None, |m: Option<f64>, x| {
                Some(m.map_or(x, |m| polars_max(m, x)))
            });
        }
    }
    let filled: Vec<Option<f64>> = high_1y
        .into_iter()
        .zip(&highs)
        .map(|(h, own)| h.or(*own))
        .collect();
    frame
        .with_column(Series::new("high_1y".into(), filled).into_column())
        .map_err(|e| pl_err(&e))?;
    frame
        .sort(
            ["date", "instrument_id"],
            SortMultipleOptions::default().with_maintain_order(true),
        )
        .map_err(|e| pl_err(&e))
}

/// polars' `max` of two floats: NaN is the largest.
fn polars_max(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if b > a {
        b
    } else {
        a
    }
}

// ---------------------------------------------------------------------------------------------
// The book
// ---------------------------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct Position {
    instrument_id: i64,
    row: usize,
    setup: String,
    state: OpenPosition,
    entry_col: usize,
    entry_paid: Decimal,
    fills: Vec<(i64, Decimal)>,
    pending: Vec<Action>,
    last_close: Decimal,
    missing: i64,
}

#[derive(Debug, Clone)]
struct Candidate {
    instrument_id: i64,
    symbol: String,
    setup: String,
    score: Decimal,
    trigger: Decimal,
    stop: Decimal,
    adr_pct: Decimal,
    avg_turnover_inr: Option<Decimal>,
    locked: bool,
}

struct Close {
    candidates: Vec<Candidate>,
    detected: i64,
    breadth: BreadthSnapshot,
    reading: Option<IndexReading>,
}

/// `stops.apply`: the position after `actions`.
fn apply(position: &OpenPosition, actions: &[Action]) -> Result<OpenPosition, WorkerError> {
    let mut quantity = position.quantity;
    let mut stop = position.stop;
    let mut partial_done = position.partial_done;
    for action in actions {
        match action.kind {
            ActionKind::SellAll | ActionKind::StoppedOut => quantity = 0,
            ActionKind::SellPartial => {
                quantity = quantity
                    .checked_sub(action.quantity)
                    .ok_or_else(|| value_err("quantity overflows"))?;
                partial_done = true;
            }
            ActionKind::RaiseStop => {
                if let Some(new_stop) = action.new_stop {
                    // `max(stop, new_stop)`: the first when equal.
                    if new_stop > stop {
                        stop = new_stop;
                    }
                }
            }
            ActionKind::Hold => {}
        }
    }
    Ok(OpenPosition {
        stop,
        quantity,
        partial_done,
        ..position.clone()
    })
}

fn reason_value(reason: ActionReason) -> &'static str {
    match reason {
        ActionReason::PartialIntoStrength => "PARTIAL_INTO_STRENGTH",
        ActionReason::BreakevenAfterPartial => "BREAKEVEN_AFTER_PARTIAL",
        ActionReason::BreakevenAtR => "BREAKEVEN_AT_R",
        ActionReason::CloseBelowTrailMa => "CLOSE_BELOW_TRAIL_MA",
        ActionReason::EpFailedRedOnDay => "EP_FAILED_RED_ON_DAY",
        ActionReason::HardStopHit => "HARD_STOP_HIT",
        ActionReason::NothingToDo => "NOTHING_TO_DO",
    }
}

fn gate_value(gate: MarketGate) -> &'static str {
    match gate {
        MarketGate::Green => "GREEN",
        MarketGate::Amber => "AMBER",
        MarketGate::Red => "RED",
    }
}

/// `BacktestCloseReason`.
#[derive(Debug, Clone, Copy)]
enum CloseReason {
    NoBar,
    Delisted,
    EndOfRun,
}

impl CloseReason {
    const fn value(self) -> &'static str {
        match self {
            Self::NoBar => "NO_BAR",
            Self::Delisted => "DELISTED",
            Self::EndOfRun => "END_OF_RUN",
        }
    }

    const fn funnel_key(self) -> &'static str {
        match self {
            Self::NoBar => "closed_no_bar",
            Self::Delisted => "closed_delisted",
            Self::EndOfRun => "closed_end_of_run",
        }
    }
}

struct Book<'a> {
    mode: GateMode,
    params: &'a Params,
    panel: Option<&'a Panel>,
    calendar: &'a [NaiveDate],
    delisted: &'a BTreeMap<i64, NaiveDate>,
    cost: Decimal,
    cash: Decimal,
    positions: BTreeMap<String, Position>,
    closed: Vec<Trade>,
    closed_r: Vec<Decimal>,
    level: i64,
    gate: MarketGate,
    peak: Decimal,
    drawdown_pct: Decimal,
    drawdown_locked: bool,
    max_drawdown: Drawdown,
    locked_sessions: i64,
    drawdowns: Vec<(NaiveDate, Decimal)>,
    tier: ExposureTier,
    pending_candidates: Vec<Candidate>,
    held_at_close: BTreeSet<String>,
    funnel: Funnel,
    equity_curve: Vec<(NaiveDate, Decimal)>,
    ladder: Vec<(NaiveDate, &'static str, i64)>,
}

impl<'a> Book<'a> {
    fn new(
        mode: GateMode,
        params: &'a Params,
        panel: Option<&'a Panel>,
        calendar: &'a [NaiveDate],
        delisted: &'a BTreeMap<i64, NaiveDate>,
    ) -> Result<Self, WorkerError> {
        let cost = narrow(&div(
            &wide(params.cost_pct_per_side),
            &WideDecimal::from(100_i64),
        )?)?;
        let tier = exposure_tier(0, &[], MarketGate::Red, &params.config.market, 0.0, false)
            .map_err(core_err)?;
        Ok(Self {
            mode,
            params,
            panel,
            calendar,
            delisted,
            cost,
            cash: params.sleeve_inr,
            positions: BTreeMap::new(),
            closed: Vec::new(),
            closed_r: Vec::new(),
            level: 0,
            gate: MarketGate::Red,
            peak: params.sleeve_inr,
            drawdown_pct: Decimal::ZERO,
            drawdown_locked: false,
            max_drawdown: Drawdown {
                max_pct: Decimal::ZERO,
                peak: params.sleeve_inr,
                trough: params.sleeve_inr,
                trough_date: None,
                locked_sessions: 0,
            },
            locked_sessions: 0,
            drawdowns: Vec::new(),
            tier,
            pending_candidates: Vec::new(),
            held_at_close: BTreeSet::new(),
            funnel: Funnel::new(),
            equity_curve: Vec::new(),
            ladder: Vec::new(),
        })
    }

    fn panel(&self) -> Result<&'a Panel, WorkerError> {
        self.panel
            .ok_or_else(|| value_err("no bars, so no prices to fill at"))
    }

    fn paid(&self, p: Decimal) -> Result<Decimal, WorkerError> {
        let factor = checked(Decimal::ONE.checked_add(self.cost), "the cost")?;
        quantize(
            checked(p.checked_mul(factor), "a price")?,
            PRICE_EXP,
            Rounding::HalfUp,
        )
    }

    fn received(&self, p: Decimal) -> Result<Decimal, WorkerError> {
        let factor = checked(Decimal::ONE.checked_sub(self.cost), "the cost")?;
        quantize(
            checked(p.checked_mul(factor), "a price")?,
            PRICE_EXP,
            Rounding::HalfUp,
        )
    }

    fn cash_for(&self, p: Decimal, quantity: i64, received: bool) -> Result<Decimal, WorkerError> {
        let effective = if received {
            self.received(p)?
        } else {
            self.paid(p)?
        };
        money(checked(
            effective.checked_mul(Decimal::from(quantity)),
            "money",
        )?)
    }

    fn tier_for(&self, gate: MarketGate) -> Result<ExposureTier, WorkerError> {
        let dd: f64 = dec_str(self.drawdown_pct)
            .parse()
            .map_err(|e: std::num::ParseFloatError| value_err(e.to_string()))?;
        exposure_tier(
            self.level,
            &self.closed_r,
            gate,
            &self.params.config.market,
            dd,
            self.drawdown_locked,
        )
        .map_err(core_err)
    }

    fn has_bar(&self, p: &Position, col: usize) -> Result<bool, WorkerError> {
        Ok(self.panel()?.has_bar(p.row, col))
    }

    fn morning(&mut self, col: usize) -> Result<(), WorkerError> {
        self.funnel.add("sessions", 1)?;
        self.fill_pending(col)?;
        self.enter(col)
    }

    fn evening(&mut self, col: usize, day: NaiveDate, last: bool) -> Result<(), WorkerError> {
        self.manage_all(col, day)?;
        if last {
            let symbols: Vec<String> = self.positions.keys().cloned().collect();
            for symbol in symbols {
                self.liquidate(&symbol, day, CloseReason::EndOfRun)?;
            }
        }
        self.mark(day)
    }

    fn settle(&mut self, day: NaiveDate, close: &Close, last: bool) -> Result<(), WorkerError> {
        self.held_at_close = self.positions.keys().cloned().collect();
        self.funnel.add("detected", close.detected)?;
        self.funnel
            .add("candidates", len_i64(close.candidates.len()))?;
        if last {
            self.funnel
                .add("skipped_no_next_session", len_i64(close.candidates.len()))?;
        } else {
            self.pending_candidates = close.candidates.clone();
        }
        self.gate = match self.mode {
            GateMode::GateOff => MarketGate::Green,
            GateMode::BreadthOnly => market_gate(&close.breadth, None, &self.params.config.market),
            GateMode::Full => market_gate(
                &close.breadth,
                close.reading.as_ref(),
                &self.params.config.market,
            ),
        };
        self.tier = self.tier_for(self.gate)?;
        self.level = self.tier.level;
        self.drawdown_locked = self.tier.drawdown_locked;
        if self.tier.drawdown_locked {
            self.locked_sessions += 1;
        }
        self.ladder.push((day, gate_value(self.gate), self.level));
        Ok(())
    }

    fn fill_pending(&mut self, col: usize) -> Result<(), WorkerError> {
        let symbols: Vec<String> = self.positions.keys().cloned().collect();
        for symbol in symbols {
            let Some(position) = self.positions.get(&symbol) else {
                continue;
            };
            if position.pending.is_empty() || !self.has_bar(position, col)? {
                continue;
            }
            let open = self.panel()?.price(OPEN, position.row, col)?;
            let mut position = position.clone();
            let actions = std::mem::take(&mut position.pending);
            for action in &actions {
                position.fills.push((action.quantity, self.received(open)?));
                let cash = self.cash_for(open, action.quantity, true)?;
                self.cash = checked(self.cash.checked_add(cash), "cash")?;
            }
            position.state = apply(&position.state, &actions)?;
            let done = position.state.quantity == 0;
            let reason = actions.last().map(|a| reason_value(a.reason)).unwrap_or("");
            self.positions.insert(symbol.clone(), position);
            if done {
                self.close(&symbol, self.calendar[col], reason)?;
            }
        }
        Ok(())
    }

    fn enter(&mut self, col: usize) -> Result<(), WorkerError> {
        let pending = std::mem::take(&mut self.pending_candidates);
        let mut candidates: Vec<(Candidate, Decimal)> = Vec::new();
        let mut seen: BTreeSet<String> = BTreeSet::new();
        for candidate in pending {
            if let Some(fill) = self.entry_fill(&candidate, col, &seen)? {
                seen.insert(candidate.symbol.clone());
                candidates.push((candidate, fill));
            }
        }
        if candidates.is_empty() {
            return Ok(());
        }
        let mut open_cost = Decimal::ZERO;
        for p in self.positions.values() {
            let v = checked(
                p.entry_paid.checked_mul(Decimal::from(p.state.quantity)),
                "exposure",
            )?;
            open_cost = checked(open_cost.checked_add(v), "exposure")?;
        }
        let account = SwingAccount {
            equity: self.params.sleeve_inr,
            cash_available: checked(self.params.sleeve_inr.checked_sub(open_cost), "cash")?,
            open_symbols: self.positions.keys().cloned().collect(),
            open_exposure_inr: open_cost,
            open_entry_counts: BTreeMap::new(),
        };
        let watch = candidates
            .iter()
            .map(|(c, fill)| {
                Ok(WatchItem {
                    symbol: c.symbol.clone(),
                    setup: setup_of(&c.setup)?,
                    trigger: *fill,
                    stop_ref: Some(c.stop),
                    adr_pct: c.adr_pct,
                    avg_turnover_inr: c.avg_turnover_inr,
                    score: c.score,
                    locked_upper_circuit: c.locked,
                })
            })
            .collect::<Result<Vec<_>, WorkerError>>()?;
        let entries = build_entries(
            self.calendar[col],
            &watch,
            &account,
            self.gate,
            &self.tier,
            &self.params.config,
            0,
            Decimal::ONE,
        )
        .map_err(core_err)?;
        for skip in &entries.skipped {
            self.funnel.add(skip_key(skip.reason), 1)?;
        }
        let by_symbol: BTreeMap<&str, &(Candidate, Decimal)> = candidates
            .iter()
            .map(|pair| (pair.0.symbol.as_str(), pair))
            .collect();
        for line in &entries.lines {
            let Some(&(candidate, fill)) = by_symbol.get(line.symbol.as_str()) else {
                return Err(WorkerError::python(
                    "KeyError",
                    format!("'{}'", line.symbol),
                ));
            };
            let Some(trail) = line.trail else {
                return Err(value_err(format!(
                    "{}: a BUY line without a trail",
                    line.symbol
                )));
            };
            let paid = self.paid(*fill)?;
            let spent = self.cash_for(*fill, line.quantity, false)?;
            self.cash = checked(self.cash.checked_sub(spent), "cash")?;
            let row = self.panel()?.row_of(candidate.instrument_id)?;
            self.positions.insert(
                line.symbol.clone(),
                Position {
                    instrument_id: candidate.instrument_id,
                    row,
                    setup: candidate.setup.clone(),
                    state: OpenPosition {
                        symbol: line.symbol.clone(),
                        entry_date: self.calendar[col],
                        entry: *fill,
                        initial_stop: candidate.stop,
                        stop: candidate.stop,
                        quantity: line.quantity,
                        partial_done: false,
                        trail,
                        is_ep_gap_day: false,
                    },
                    entry_col: col,
                    entry_paid: paid,
                    fills: Vec::new(),
                    pending: Vec::new(),
                    last_close: *fill,
                    missing: 0,
                },
            );
            self.funnel.add("entered", 1)?;
        }
        Ok(())
    }

    fn entry_fill(
        &mut self,
        c: &Candidate,
        col: usize,
        seen: &BTreeSet<String>,
    ) -> Result<Option<Decimal>, WorkerError> {
        let refusal = if c.locked {
            Some("skipped_locked")
        } else if self.tier.drawdown_locked {
            Some("skipped_drawdown")
        } else if self.gate == MarketGate::Red || !self.tier.new_entries_allowed {
            Some("skipped_gate")
        } else if self.held_at_close.contains(&c.symbol) || seen.contains(&c.symbol) {
            Some("skipped_held")
        } else {
            None
        };
        let (fill, reason) = match refusal {
            Some(r) => (None, Some(r)),
            None => {
                let panel = self.panel()?;
                let row = panel.row_of(c.instrument_id)?;
                if !panel.has_bar(row, col) {
                    (None, Some("skipped_no_bar"))
                } else {
                    let open = panel.price(OPEN, row, col)?;
                    if open >= c.trigger {
                        (Some(open), None)
                    } else if panel.price(HIGH, row, col)? >= c.trigger {
                        (Some(c.trigger), None)
                    } else {
                        (None, Some("skipped_no_trigger"))
                    }
                }
            }
        };
        if let Some(r) = reason {
            self.funnel.add(r, 1)?;
            return Ok(None);
        }
        Ok(fill)
    }

    fn manage_all(&mut self, col: usize, day: NaiveDate) -> Result<(), WorkerError> {
        let symbols: Vec<String> = self.positions.keys().cloned().collect();
        for symbol in symbols {
            let Some(position) = self.positions.get(&symbol) else {
                continue;
            };
            let gone = self.delisted.get(&position.instrument_id).copied();
            if !self.has_bar(position, col)? {
                if gone.is_some() {
                    self.liquidate(&symbol, day, CloseReason::Delisted)?;
                    continue;
                }
                let mut p = position.clone();
                p.missing += 1;
                let dead = p.missing >= MISSING_BAR_TOLERANCE_DAYS;
                self.positions.insert(symbol.clone(), p);
                if dead {
                    self.liquidate(&symbol, day, CloseReason::NoBar)?;
                }
                continue;
            }
            let panel = self.panel()?;
            let mut p = position.clone();
            p.missing = 0;
            let bar = DailyBar {
                date: day,
                open: panel.price(OPEN, p.row, col)?,
                high: panel.price(HIGH, p.row, col)?,
                low: panel.price(LOW, p.row, col)?,
                close: panel.price(CLOSE, p.row, col)?,
                ma10: panel.optional(MA_FAST, p.row, col)?,
                ma20: panel.optional(MA_SLOW, p.row, col)?,
                bars_since_entry: len_i64(col) - len_i64(p.entry_col),
            };
            p.last_close = bar.close;
            let actions = manage(&p.state, &bar, &self.params.config.stops).map_err(core_err)?;
            self.positions.insert(symbol.clone(), p);
            self.apply_actions(&symbol, &bar, &actions)?;
            if let Some(gone) = gone
                && day >= gone
                && self.positions.contains_key(&symbol)
            {
                self.liquidate(&symbol, day, CloseReason::Delisted)?;
            }
        }
        Ok(())
    }

    fn apply_actions(
        &mut self,
        symbol: &str,
        bar: &DailyBar,
        actions: &[Action],
    ) -> Result<(), WorkerError> {
        let Some(mut p) = self.positions.get(symbol).cloned() else {
            return Ok(());
        };
        let stopped: Vec<Action> = actions
            .iter()
            .filter(|a| a.kind == ActionKind::StoppedOut)
            .cloned()
            .collect();
        if !stopped.is_empty() {
            let mut fill = p.state.stop;
            if bar.bars_since_entry > 0 && bar.open < fill {
                fill = bar.open;
            }
            p.fills.push((p.state.quantity, self.received(fill)?));
            let cash = self.cash_for(fill, p.state.quantity, true)?;
            self.cash = checked(self.cash.checked_add(cash), "cash")?;
            p.state = apply(&p.state, &stopped)?;
            self.positions.insert(symbol.to_owned(), p);
            return self.close(symbol, bar.date, reason_value(ActionReason::HardStopHit));
        }
        p.pending = actions
            .iter()
            .filter(|a| matches!(a.kind, ActionKind::SellAll | ActionKind::SellPartial))
            .cloned()
            .collect();
        let raises: Vec<Action> = actions
            .iter()
            .filter(|a| a.kind == ActionKind::RaiseStop)
            .cloned()
            .collect();
        if !raises.is_empty() {
            p.state = apply(&p.state, &raises)?;
        }
        self.positions.insert(symbol.to_owned(), p);
        Ok(())
    }

    fn liquidate(
        &mut self,
        symbol: &str,
        day: NaiveDate,
        reason: CloseReason,
    ) -> Result<(), WorkerError> {
        let Some(mut p) = self.positions.get(symbol).cloned() else {
            return Ok(());
        };
        let quantity = p.state.quantity;
        p.fills.push((quantity, self.received(p.last_close)?));
        let cash = self.cash_for(p.last_close, quantity, true)?;
        self.cash = checked(self.cash.checked_add(cash), "cash")?;
        p.pending = Vec::new();
        p.state = apply(
            &p.state,
            &[Action {
                kind: ActionKind::SellAll,
                reason: ActionReason::NothingToDo,
                quantity,
                new_stop: None,
            }],
        )?;
        self.positions.insert(symbol.to_owned(), p);
        self.close(symbol, day, reason.value())?;
        self.funnel.add(reason.funnel_key(), 1)
    }

    fn close(&mut self, symbol: &str, day: NaiveDate, reason: &str) -> Result<(), WorkerError> {
        let Some(p) = self.positions.get(symbol) else {
            return Ok(());
        };
        let entered: i64 = p.fills.iter().map(|(q, _)| *q).sum();
        let closed = ClosedTrade {
            symbol: p.state.symbol.clone(),
            setup: p.setup.clone(),
            entry_date: p.state.entry_date,
            exit_date: day,
            entry: p.entry_paid,
            initial_stop: p.state.initial_stop,
            exit_avg: exit_average(&p.fills)?,
            quantity: entered,
        };
        let r = closed.r_multiple()?;
        let pnl = closed.pnl_inr()?;
        self.closed.push(Trade {
            closed,
            r_multiple: r,
            pnl_inr: pnl,
            close_reason: reason.to_owned(),
        });
        self.closed_r.push(r);
        self.funnel.add("closed", 1)?;
        let key = p.state.symbol.clone();
        self.positions.remove(&key);
        Ok(())
    }

    fn mark(&mut self, day: NaiveDate) -> Result<(), WorkerError> {
        let mut marked = Decimal::ZERO;
        for p in self.positions.values() {
            let v = checked(
                p.last_close.checked_mul(Decimal::from(p.state.quantity)),
                "a mark",
            )?;
            marked = checked(marked.checked_add(v), "the mark")?;
        }
        let equity = money(checked(self.cash.checked_add(marked), "equity")?)?;
        self.equity_curve.push((day, equity));
        if equity > self.peak {
            self.peak = equity;
        }
        self.drawdown_pct = if equity >= self.peak {
            Decimal::ZERO
        } else {
            let gap = checked(self.peak.checked_sub(equity), "the drawdown")?;
            let pct =
                div(&wide(gap), &wide(self.params.sleeve_inr))?.mul(&WideDecimal::from(100_i64));
            narrow(&pct.quantize(MONEY_EXP, Rounding::HalfUp).map_err(arith)?)?
        };
        self.drawdowns.push((day, self.drawdown_pct));
        if self.drawdown_pct > self.max_drawdown.max_pct {
            self.max_drawdown = Drawdown {
                max_pct: self.drawdown_pct,
                peak: self.peak,
                trough: equity,
                trough_date: Some(day),
                locked_sessions: self.locked_sessions,
            };
        }
        Ok(())
    }

    fn drawdown_summary(&self) -> Drawdown {
        Drawdown {
            locked_sessions: self.locked_sessions,
            ..self.max_drawdown.clone()
        }
    }

    fn max_drawdown_in(&self, year: Option<i32>) -> Decimal {
        let inside: Vec<Decimal> = self
            .drawdowns
            .iter()
            .filter(|(day, _)| year.is_none_or(|y| day.year() == y))
            .map(|(_, dd)| *dd)
            .collect();
        first_max(&inside)
    }
}

fn setup_of(value: &str) -> Result<Setup, WorkerError> {
    match value {
        "FLAG" => Ok(Setup::Flag),
        "EP" => Ok(Setup::Ep),
        "PARABOLIC_SHORT" => Ok(Setup::ParabolicShort),
        other => Err(value_err(format!("'{other}' is not a valid Setup"))),
    }
}

// ---------------------------------------------------------------------------------------------
// The run
// ---------------------------------------------------------------------------------------------

/// `SwingConfig.bars_required`.
fn bars_required(c: &SwingConfig) -> i64 {
    (c.flag.lookback_bars + c.flag.base_max_bars)
        .max(c.ep.prior_bars + c.ep.rvol_bars)
        .max(c.ma_trend)
        + 1
}

struct Run<'a> {
    config: &'a SwingConfig,
    by_date: &'a DataFrame,
    row_days: Vec<i32>,
    instrument_ids: Vec<Option<i64>>,
    calendar_days: &'a [i32],
    index: Option<IndexSeries>,
}

impl Run<'_> {
    /// `_slices`: the detection window (`bars_required` sessions ending today, names with at
    /// least two bars in it) and today's rows.
    fn slices(&self, col: usize) -> Result<(DataFrame, DataFrame), WorkerError> {
        if self.by_date.height() == 0 {
            return Ok((self.by_date.clone(), self.by_date.clone()));
        }
        let back = usize::try_from(bars_required(self.config) - 1).unwrap_or(0);
        let first = col.saturating_sub(back);
        let lo = self
            .row_days
            .partition_point(|d| *d < self.calendar_days[first]);
        let day_lo = self
            .row_days
            .partition_point(|d| *d < self.calendar_days[col]);
        let hi = self
            .row_days
            .partition_point(|d| *d <= self.calendar_days[col]);
        let window = self.by_date.slice(len_i64(lo), hi - lo);
        let mut counts: BTreeMap<Option<i64>, usize> = BTreeMap::new();
        for id in &self.instrument_ids[lo..hi] {
            *counts.entry(*id).or_default() += 1;
        }
        let keep: Vec<bool> = self.instrument_ids[lo..hi]
            .iter()
            .map(|id| counts.get(id).copied().unwrap_or(0) >= MIN_WINDOW_BARS)
            .collect();
        let mask = BooleanChunked::from_slice("keep".into(), &keep);
        let window = window.filter(&mask).map_err(|e| pl_err(&e))?;
        let today = self.by_date.slice(len_i64(day_lo), hi - day_lo);
        Ok((window, today))
    }

    /// `_close`: detection, breadth and the index reading, all at `day`'s close.
    fn close(&self, col: usize, day: NaiveDate) -> Result<Close, WorkerError> {
        let (window, today) = self.slices(col)?;
        let mut detected = 0;
        let mut candidates = Vec::new();
        if window.height() > 0 {
            let found = detect_setups(&window, day, self.config).map_err(core_err)?;
            if found.height() > 0 {
                detected = len_i64(found.height());
                candidates = candidates_of(&found)?;
            }
        }
        let breadth = if today.height() > 0 {
            let adr = floats(&today, "adr_pct")?;
            let turnover = floats(&today, "turnover_avg")?;
            let close = floats(&today, "close")?;
            let ret_20 = floats(&today, "ret_20")?;
            let ma_slow = floats(&today, "ma_slow")?;
            let high_1y = floats(&today, "high_1y")?;
            let rows: Vec<BreadthRow> = (0..today.height())
                .filter(|&i| is_liquid(adr[i], turnover[i], close[i], &self.config.liquidity))
                .map(|i| BreadthRow {
                    ret_20: ret_20[i],
                    close: close[i],
                    ma_slow: ma_slow[i],
                    high_1y: high_1y[i],
                })
                .collect();
            breadth_snapshot(&rows, &self.config.market)
        } else {
            BreadthSnapshot::empty()
        };
        let reading = match &self.index {
            None => None,
            Some(index) => index.reading_on(self.calendar_days[col])?,
        };
        Ok(Close {
            candidates,
            detected,
            breadth,
            reading,
        })
    }
}

/// `_candidates`: the enterable rows (`SETTING_UP` / `GAP_DAY`, a tradeable setup), best score
/// first, then symbol, setup and instrument.
fn candidates_of(found: &DataFrame) -> Result<Vec<Candidate>, WorkerError> {
    let status = strings(found, "status")?;
    let setup = strings(found, "setup")?;
    let ids = ints(found, "instrument_id")?;
    let symbol = strings(found, "symbol")?;
    let score = floats(found, "score")?;
    let trigger = floats(found, "trigger")?;
    let stop = floats(found, "stop_ref")?;
    let adr = floats(found, "adr_pct")?;
    let turnover = floats(found, "turnover_avg")?;
    let locked = bools(found, "locked_upper_circuit")?;
    let enterable = [
        CandidateStatus::SettingUp.as_str(),
        CandidateStatus::GapDay.as_str(),
    ];
    let tradeable: Vec<&str> = TRADEABLE_SETUPS.iter().map(|s| s.as_str()).collect();
    let need = |v: Option<f64>, what: &str| {
        v.ok_or_else(|| {
            WorkerError::python(
                "TypeError",
                format!(
                    "float() argument must be a string or a real number, not 'NoneType' ({what})"
                ),
            )
        })
    };
    let mut rows = Vec::new();
    for i in 0..found.height() {
        let (Some(st), Some(se)) = (status[i].as_deref(), setup[i].as_deref()) else {
            continue;
        };
        if !enterable.contains(&st) || !tradeable.contains(&se) {
            continue;
        }
        rows.push(Candidate {
            instrument_id: ids[i].ok_or_else(|| value_err("a candidate without an instrument"))?,
            symbol: symbol[i].clone().unwrap_or_else(|| "None".to_owned()),
            setup: se.to_owned(),
            score: decimal_of(need(score[i], "score")?)?,
            trigger: price(need(trigger[i], "trigger")?)?,
            stop: price(need(stop[i], "stop_ref")?)?,
            adr_pct: decimal_of(need(adr[i], "adr_pct")?)?,
            avg_turnover_inr: turnover[i].map(decimal_of).transpose()?,
            locked: locked[i].unwrap_or(false),
        });
    }
    rows.sort_by(|a, b| {
        b.score
            .cmp(&a.score)
            .then_with(|| a.symbol.cmp(&b.symbol))
            .then_with(|| a.setup.cmp(&b.setup))
            .then_with(|| a.instrument_id.cmp(&b.instrument_id))
    });
    Ok(rows)
}

fn cell(trades: &[&Trade], max_drawdown_pct: Option<Decimal>) -> Result<GateCell, WorkerError> {
    let closed: Vec<ClosedTrade> = trades.iter().map(|t| t.closed.clone()).collect();
    let stats = summarize(&closed)?;
    Ok(GateCell {
        entered: len_i64(trades.len()),
        net_r: stats.net_r,
        expectancy_r: stats.expectancy_r,
        win_rate_pct: stats.win_rate_pct,
        max_drawdown_pct,
    })
}

fn delta(with_it: &GateCell, without: &GateCell) -> Result<GateCell, WorkerError> {
    let sub = |a: Decimal, b: Decimal| checked(a.checked_sub(b), "a delta");
    Ok(GateCell {
        entered: with_it.entered - without.entered,
        net_r: sub(with_it.net_r, without.net_r)?,
        expectancy_r: sub(with_it.expectancy_r, without.expectancy_r)?,
        win_rate_pct: sub(with_it.win_rate_pct, without.win_rate_pct)?,
        max_drawdown_pct: match (with_it.max_drawdown_pct, without.max_drawdown_pct) {
            (Some(a), Some(b)) => Some(sub(a, b)?),
            _ => None,
        },
    })
}

fn find(cells: &[(GateMode, GateCell)], mode: GateMode) -> Result<&GateCell, WorkerError> {
    cells
        .iter()
        .find(|(m, _)| *m == mode)
        .map(|(_, c)| c)
        .ok_or_else(|| WorkerError::python("KeyError", mode.value().to_owned()))
}

fn contribution(
    overall: &[(GateMode, GateCell)],
    by_year: &[(i32, Vec<(GateMode, GateCell)>)],
    by_setup: &[(String, Vec<(GateMode, GateCell)>)],
    with_it: GateMode,
    without: GateMode,
) -> Result<Contribution, WorkerError> {
    Ok(Contribution {
        overall: delta(find(overall, with_it)?, find(overall, without)?)?,
        by_year: by_year
            .iter()
            .map(|(y, cells)| Ok((*y, delta(find(cells, with_it)?, find(cells, without)?)?)))
            .collect::<Result<_, WorkerError>>()?,
        by_setup: by_setup
            .iter()
            .map(|(k, cells)| {
                Ok((
                    k.clone(),
                    delta(find(cells, with_it)?, find(cells, without)?)?,
                ))
            })
            .collect::<Result<_, WorkerError>>()?,
    })
}

fn tradeable_setups() -> Vec<String> {
    [Setup::Flag, Setup::Ep, Setup::ParabolicShort]
        .iter()
        .filter(|s| TRADEABLE_SETUPS.contains(s))
        .map(|s| s.as_str().to_owned())
        .collect()
}

fn comparison(
    books: &[Book<'_>],
    primary: GateMode,
    params: &Params,
) -> Result<Comparison, WorkerError> {
    let years: Vec<i32> = (params.start.year()..=params.end.year()).collect();
    let setups = tradeable_setups();
    let mut overall = Vec::new();
    for book in books {
        let all: Vec<&Trade> = book.closed.iter().collect();
        overall.push((book.mode, cell(&all, Some(book.max_drawdown_in(None)))?));
    }
    let mut by_year = Vec::new();
    for year in &years {
        let mut cells = Vec::new();
        for book in books {
            let inside: Vec<&Trade> = book
                .closed
                .iter()
                .filter(|t| t.closed.entry_date.year() == *year)
                .collect();
            cells.push((
                book.mode,
                cell(&inside, Some(book.max_drawdown_in(Some(*year))))?,
            ));
        }
        by_year.push((*year, cells));
    }
    let mut by_setup = Vec::new();
    for setup in &setups {
        let mut cells = Vec::new();
        for book in books {
            let of: Vec<&Trade> = book
                .closed
                .iter()
                .filter(|t| &t.closed.setup == setup)
                .collect();
            cells.push((book.mode, cell(&of, None)?));
        }
        by_setup.push((setup.clone(), cells));
    }
    let index_supplied = books.iter().any(|b| b.mode == GateMode::Full);
    let breadth = contribution(
        &overall,
        &by_year,
        &by_setup,
        GateMode::BreadthOnly,
        GateMode::GateOff,
    )?;
    let index_rule = if index_supplied {
        Some(contribution(
            &overall,
            &by_year,
            &by_setup,
            GateMode::Full,
            GateMode::BreadthOnly,
        )?)
    } else {
        None
    };
    Ok(Comparison {
        index_supplied,
        primary,
        overall,
        by_year,
        by_setup,
        breadth,
        index_rule,
    })
}

fn check(
    params: &Params,
    calendar: &[NaiveDate],
    index: Option<&[(NaiveDate, f64)]>,
) -> Result<(), WorkerError> {
    if params.end < params.start {
        return Err(value_err(format!(
            "end {} is before start {}",
            iso_date(params.end),
            iso_date(params.start)
        )));
    }
    if params.sleeve_inr <= Decimal::ZERO {
        return Err(value_err(format!(
            "sleeve_inr must be positive, got {}",
            dec_str(params.sleeve_inr)
        )));
    }
    if params.cost_pct_per_side < Decimal::ZERO {
        return Err(value_err(format!(
            "cost_pct_per_side must not be negative, got {}",
            dec_str(params.cost_pct_per_side)
        )));
    }
    if calendar.windows(2).any(|w| w[1] <= w[0]) {
        return Err(value_err("calendar must be strictly increasing"));
    }
    if index.is_none()
        && let Some(slug) = &params.index_slug
    {
        return Err(value_err(format!(
            "index_slug {} names a series that was not supplied",
            baskfy_providers::py::repr_str(slug)
        )));
    }
    Ok(())
}

/// `run_backtest(bars, params, calendar=, index=, delisted=)`.
pub(super) fn run_backtest(
    bars: &DataFrame,
    params: &Params,
    calendar: &[NaiveDate],
    index: Option<&[(NaiveDate, f64)]>,
    delisted: &BTreeMap<i64, NaiveDate>,
) -> Result<BacktestResult, WorkerError> {
    check(params, calendar, index)?;
    let calendar_days: Vec<i32> = calendar.iter().map(|d| day_number(*d)).collect();
    let session_cols: Vec<usize> = calendar
        .iter()
        .enumerate()
        .filter(|(_, d)| params.start <= **d && **d <= params.end)
        .map(|(i, _)| i)
        .collect();
    let (by_date, panel) = if bars.height() > 0 {
        let by_date = indicated(bars, &params.config)?;
        let panel = if calendar.is_empty() {
            None
        } else {
            Some(Panel::build(&by_date, &calendar_days)?)
        };
        (by_date, panel)
    } else {
        (DataFrame::empty(), None)
    };
    let row_days: Vec<i32> = if by_date.height() > 0 {
        days_of(&by_date, "date")?
            .into_iter()
            .map(|d| d.unwrap_or(i32::MIN))
            .collect()
    } else {
        Vec::new()
    };
    let instrument_ids = if by_date.height() > 0 {
        ints(&by_date, "instrument_id")?
    } else {
        Vec::new()
    };
    let index_series = index
        .map(|rows| IndexSeries::build(rows, &params.config))
        .transpose()?;
    let mut modes = vec![GateMode::GateOff, GateMode::BreadthOnly];
    if index_series.is_some() {
        modes.push(GateMode::Full);
    }
    let primary_mode = if index_series.is_some() {
        GateMode::Full
    } else {
        GateMode::BreadthOnly
    };
    let run = Run {
        config: &params.config,
        by_date: &by_date,
        row_days,
        instrument_ids,
        calendar_days: &calendar_days,
        index: index_series,
    };
    let mut books = modes
        .iter()
        .map(|m| Book::new(*m, params, panel.as_ref(), calendar, delisted))
        .collect::<Result<Vec<_>, _>>()?;
    for (n, &col) in session_cols.iter().enumerate() {
        let last = n + 1 == session_cols.len();
        let day = calendar[col];
        for book in &mut books {
            book.morning(col)?;
            book.evening(col, day, last)?;
        }
        let close = run.close(col, day)?;
        for book in &mut books {
            book.settle(day, &close, last)?;
        }
    }
    let comparison = comparison(&books, primary_mode, params)?;
    let primary = books
        .iter()
        .find(|b| b.mode == primary_mode)
        .ok_or_else(|| WorkerError::python("KeyError", primary_mode.value()))?;
    let closed: Vec<ClosedTrade> = primary.closed.iter().map(|t| t.closed.clone()).collect();
    let by_setup = tradeable_setups()
        .into_iter()
        .map(|setup| {
            let of: Vec<ClosedTrade> = closed
                .iter()
                .filter(|t| t.setup == setup)
                .cloned()
                .collect();
            Ok((setup, summarize(&of)?))
        })
        .collect::<Result<Vec<_>, WorkerError>>()?;
    let by_year = (params.start.year()..=params.end.year())
        .map(|year| {
            let of: Vec<ClosedTrade> = closed
                .iter()
                .filter(|t| t.exit_date.year() == year)
                .cloned()
                .collect();
            Ok((year, summarize(&of)?))
        })
        .collect::<Result<Vec<_>, WorkerError>>()?;
    Ok(BacktestResult {
        params: params.clone(),
        trades: primary.closed.clone(),
        stats: summarize(&closed)?,
        by_setup,
        by_year,
        equity_curve: primary.equity_curve.clone(),
        funnel: primary.funnel.clone(),
        ladder: primary.ladder.clone(),
        drawdown: primary.drawdown_summary(),
        comparison,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn price_is_the_exact_float_half_up() {
        // 1.03125 is exact in binary: half-up gives 1.0313 where `{:.4}` (half-even) gives 1.0312.
        assert_eq!(price(1.031_25).map(dec_str).ok().as_deref(), Some("1.0313"));
        // 0.1 is 0.1000000000000000055…: four places is 0.1000, scale kept.
        assert_eq!(price(0.1).map(dec_str).ok().as_deref(), Some("0.1000"));
        assert_eq!(price(250.0).map(dec_str).ok().as_deref(), Some("250.0000"));
    }

    fn trade(entry: i64, stop: i64, exit: i64) -> ClosedTrade {
        let day = NaiveDate::from_ymd_opt(2026, 1, 1).unwrap_or_default();
        ClosedTrade {
            symbol: "X".into(),
            setup: "FLAG".into(),
            entry_date: day,
            exit_date: day,
            entry: Decimal::from(entry),
            initial_stop: Decimal::from(stop),
            exit_avg: Decimal::from(exit),
            quantity: 3,
        }
    }

    #[test]
    fn the_journal_is_pythons() {
        let stats = summarize(&[trade(100, 90, 130), trade(100, 90, 95), trade(100, 90, 85)]);
        let stats = stats.ok();
        let st = stats.as_ref();
        assert_eq!(
            st.map(|s| dec_str(s.win_rate_pct)).as_deref(),
            Some("33.33")
        );
        assert_eq!(st.map(|s| dec_str(s.net_r)).as_deref(), Some("1.00"));
        assert_eq!(st.map(|s| s.current_loss_streak), Some(2));
        assert_eq!(
            st.and_then(|s| s.profit_factor).map(dec_str).as_deref(),
            Some("1.50")
        );
        let empty = summarize(&[]).ok();
        assert_eq!(empty.map(|s| dec_str(s.win_rate_pct)).as_deref(), Some("0"));
        let bad = trade(100, 100, 120)
            .r_multiple()
            .err()
            .map(|e| e.to_string());
        assert_eq!(bad.as_deref(), Some("X: initial stop 100 not below entry"));
    }

    #[test]
    fn exit_average_weights_by_quantity() {
        let fills = [(1, Decimal::new(1000, 1)), (2, Decimal::new(1100, 1))];
        assert_eq!(
            exit_average(&fills).map(dec_str).ok().as_deref(),
            Some("106.67")
        );
        assert!(exit_average(&[]).is_err());
    }

    /// `params_json(BacktestParams(start=2025-01-01, end=2026-10-29, index_slug="nifty-500"))`
    /// as Python's `json.dumps` writes it (captured from the Python worker).
    const PYTHON_PARAMS: &str = r#"{"start": "2025-01-01", "end": "2026-10-29", "sleeve_inr": "1000000", "cost_pct_per_side": "0.13", "config": {"liquidity": {"adr_bars": 20, "adr_min_pct": 4.0, "turnover_bars": 20, "turnover_min_inr": 50000000.0, "price_min": 20.0}, "flag": {"lookback_bars": 65, "flagpole_min_gain_pct": 30.0, "base_min_bars": 10, "base_max_bars": 60, "base_max_depth_pct": 30.0, "tight_bars": 10, "tight_max_adr_multiple": 3.0, "require_higher_lows": true, "max_extension_adr": 2.0, "ma_tolerance_pct": 2.0, "ma_rising_bars": 5, "dryup_bars": 10, "dryup_max_ratio": 0.85, "pivot_bars": 20, "breakout_min_rvol": 1.5}, "ep": {"min_gap_pct": 10.0, "rvol_bars": 50, "min_rvol": 3.0, "min_close_position": 0.5, "prior_bars": 60, "max_prior_gain_pct": 30.0, "valid_bars": 3}, "parabolic": {"min_gain_5_bars_pct": 50.0, "min_gain_10_bars_pct": 100.0, "min_up_streak": 3, "min_extension_adr": 4.0}, "sizing": {"risk_per_trade_pct": 0.5, "pyramiding": true, "max_entries_per_name": 2, "max_position_pct": 20.0, "max_open_positions": 10, "max_new_entries_per_session": 3, "max_position_vs_turnover": 0.01, "min_trade_value_inr": "10000", "risk_multiplier_first_live": 0.5, "first_live_sessions": 5}, "stops": {"partial_earliest_bar": 3, "partial_latest_bar": 5, "partial_numerator": 1, "partial_denominator": 3, "breakeven_after_r": 1.0, "fast_trail_min_adr_pct": 6.0, "max_stop_distance_pct": 10.0, "max_stop_adr_multiple": 1.0}, "watch": {"auto_watch_min_score": 60.0, "flag_valid_bars": 10, "auto_watch_top_n": 20, "focus_top_n": 5, "manual_valid_bars": 10}, "opening_range": {"windows_minutes": [1, 5, 60], "default_window_minutes": 5, "break_buffer_pct": 0.1, "live_min_gap_pct": 10.0, "live_min_volume_pace": 3.0, "session_open": [9, 15], "monitor_close": [15, 30], "pending_cutoff_at": [10, 45], "entry_limit_buffer_pct": 0.5, "entry_limit_max_adr": 0.25, "entry_order_type": "MARKET", "market_protection_floor_pct": 0.05, "market_protection_max_pct": 3.0, "fill_poll_seconds": 10.0, "fill_poll_interval_seconds": 0.5, "range_reconcile_delay_minutes": 1, "quote_poll_min_seconds": 5.0, "gtt_sweep_at": [15, 15]}, "market": {"strong_move_pct": 25.0, "green_min_pct_up": 5.0, "red_max_pct_up": 2.0, "index_ma_fast": 10, "index_ma_slow": 20, "tiers": [[2, 25.0], [4, 50.0], [6, 75.0], [10, 100.0]], "lookback_trades": 5, "step_down_loss_streak": 3, "max_drawdown_pct": 15.0, "resume_drawdown_pct": 10.0}, "ma_fast": 10, "ma_slow": 20, "ma_trend": 50}, "index_slug": "nifty-500"}"#;

    #[test]
    fn params_are_written_exactly_as_python_writes_them() {
        let d = |y, m, day| NaiveDate::from_ymd_opt(y, m, day).unwrap_or_default();
        let params = Params {
            start: d(2025, 1, 1),
            end: d(2026, 10, 29),
            sleeve_inr: Decimal::from(1_000_000_i64),
            cost_pct_per_side: Decimal::new(13, 2),
            config: SwingConfig::default(),
            index_slug: Some("nifty-500".to_owned()),
        };
        let ours = params_json(&params)
            .ok()
            .and_then(|j| crate::pyjson::dumps(&j).ok());
        assert_eq!(ours.as_deref(), Some(PYTHON_PARAMS));
    }

    #[test]
    fn bars_required_is_pythons_property() {
        assert_eq!(bars_required(&SwingConfig::default()), 126);
    }
}
