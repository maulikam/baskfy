//! `baskfy_worker.backtest` — the I/O half of a backtest: the calendar, one screen per rebalance
//! date (as of that date), the candidates' adjusted bars, the benchmark, the reference data, and
//! the adjustment coverage, assembled into `baskfy_core`'s [`BacktestData`].
//!
//! The screen is the publish step's (`steps::gate::execute_screen_with_limit`), so a backtest is
//! the screener executed on a series of past dates, never a second implementation of it (house
//! rule 5: point-in-time membership and factor rows on every as-of).

use std::collections::{BTreeMap, BTreeSet, HashMap};

use baskfy_core::backtest::engine::{
    BacktestConfig, BacktestProgress, BacktestResult, FragilityReport, run_backtest, run_fragility,
};
use baskfy_core::backtest::helpers::{
    AdjustmentCoverage, BacktestData, BacktestError, PricePanel, rebalance_dates, shift_schedule,
};
use baskfy_core::screener::{DecimalValue, Payload, PyDecimal, ScreenDefinition, universe_by_slug};
use chrono::NaiveDate;
use polars::prelude::{DataFrame, DataType, IntoColumn as _, NamedFrom as _, Series};
use sqlx::{PgConnection, Row as _};

use super::risk_free::attach_tbill_curve;
use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::steps::gate::{current_data_version, execute_screen_with_limit};

/// `BACKTEST_SCREEN_COLUMNS`: the weighting inputs docs/10's `marketcap` and
/// `inverse_volatility` schemes read, beyond the identity columns every screen projects.
pub(super) const BACKTEST_SCREEN_COLUMNS: [&str; 2] = ["marketcap_cr", "vol_12m"];

/// `MAX_REBALANCE_DATES`: the most screen runs one backtest may issue.
pub(super) const MAX_REBALANCE_DATES: usize = 1_000;

/// `MAX_BAR_ROWS`: the most bar rows one backtest may load.
pub(super) const MAX_BAR_ROWS: i64 = 3_000_000;

/// `_DATES_IN_MESSAGE`: how many missing dates a refusal names before summarising.
const DATES_IN_MESSAGE: usize = 5;

/// How a load or a run stops: a failure the job records on the row (Python's `BacktestError`,
/// `ValueError` and `ArchiveError` — the classes `run_backtest_job` catches), or anything else,
/// which propagates and rolls the transaction back.
#[derive(Debug)]
pub(super) enum Stop {
    /// Caught: `type(exc).__name__` and `str(exc)`.
    Caught {
        /// The Python class.
        class: String,
        /// `str(exc)`.
        message: String,
    },
    /// Raised past the job.
    Raised(WorkerError),
}

impl Stop {
    pub(super) fn caught(class: impl Into<String>, message: impl Into<String>) -> Self {
        Self::Caught {
            class: class.into(),
            message: message.into(),
        }
    }

    /// `BacktestDataError(message)`.
    pub(super) fn data(message: impl Into<String>) -> Self {
        Self::caught("BacktestDataError", message)
    }
}

/// The Python classes `run_backtest_job` catches (`BacktestError` and its subclasses,
/// `ValueError` and its subclasses — pydantic's `ValidationError`, `ScreenQueryError` — and
/// `ArchiveError`).
pub(super) const CAUGHT_CLASSES: [&str; 8] = [
    "BacktestError",
    "BacktestDataError",
    "BacktestConfigError",
    "LookAheadError",
    "ValidationError",
    "ValueError",
    "ScreenQueryError",
    "ArchiveError",
];

impl From<WorkerError> for Stop {
    fn from(e: WorkerError) -> Self {
        let class = e.python_type();
        if CAUGHT_CLASSES.contains(&class.as_str()) {
            Self::Caught {
                class,
                message: e.to_string(),
            }
        } else {
            Self::Raised(e)
        }
    }
}

impl From<sqlx::Error> for Stop {
    fn from(e: sqlx::Error) -> Self {
        Self::Raised(e.into())
    }
}

impl From<BacktestError> for Stop {
    fn from(e: BacktestError) -> Self {
        match e {
            // `decimal.InvalidOperation` is an `ArithmeticError`, and a polars error is polars'
            // own: neither is caught by the job, so the transaction rolls back and the task raises.
            BacktestError::Decimal(_) | BacktestError::Polars(_) => {
                Self::Raised(WorkerError::python(e.type_name(), e.to_string()))
            }
            other => Self::caught(other.type_name(), other.to_string()),
        }
    }
}

/// A progress event's destination (`ProgressSink`), here a plain closure.
pub(super) type Sink<'a> = &'a mut dyn FnMut(&BacktestProgress);

/// `LoadedBacktest`: a panel ready for the engine and the loader's notes (Python also keeps the
/// schedule, the offset dates and the panel's size, which the job never reads).
pub(super) struct LoadedBacktest {
    pub(super) data: BacktestData,
    pub(super) notes: Vec<String>,
}

/// `BacktestOutcome`: one completed run and the fragility probe, if asked for.
pub(super) struct BacktestOutcome {
    pub(super) loaded: LoadedBacktest,
    pub(super) result: BacktestResult,
    pub(super) fragility: Option<FragilityReport>,
}

fn pl(e: &polars::prelude::PolarsError) -> Stop {
    Stop::Raised(WorkerError::python("PolarsError", e.to_string()))
}

/// `trading_calendar(session, start, end)`: NSE trading days in `[start, end]`, in order.
pub(super) async fn trading_calendar(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trading_day.date FROM trading_day WHERE trading_day.exchange_id = $1 AND \
         trading_day.is_trading_day IS true AND trading_day.date >= $2 AND trading_day.date <= $3 \
         ORDER BY trading_day.date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(d,)| d).collect())
}

/// `_dates(days)`: the first five, and how many more.
fn dates_text(days: &[NaiveDate]) -> String {
    let shown: Vec<String> = days
        .iter()
        .take(DATES_IN_MESSAGE)
        .map(|d| d.format("%Y-%m-%d").to_string())
        .collect();
    let more = if days.len() > DATES_IN_MESSAGE {
        format!(" and {} more", days.len() - DATES_IN_MESSAGE)
    } else {
        String::new()
    };
    format!("{}{more}", shown.join(", "))
}

/// Python's `repr(str)` (the universe slug in the refusal).
fn repr(text: &str) -> String {
    baskfy_providers::py::repr_str(text)
}

/// `_require_screen_coverage`'s refusal, given what is missing (M39, M45).
pub(super) fn coverage_refusal(
    wanted: usize,
    index: &str,
    missing_factors: &[NaiveDate],
    missing_members: &[NaiveDate],
) -> Option<String> {
    if missing_factors.is_empty() && missing_members.is_empty() {
        return None;
    }
    let mut parts: Vec<String> = Vec::new();
    if !missing_factors.is_empty() {
        parts.push(format!(
            "{} have no factor rows ({})",
            missing_factors.len(),
            dates_text(missing_factors)
        ));
    }
    if !missing_members.is_empty() {
        parts.push(format!(
            "{} have no {} index membership ({})",
            missing_members.len(),
            repr(index),
            dates_text(missing_members)
        ));
    }
    Some(format!(
        "of {wanted} rebalance dates, {}. The screen needs both on every rebalance date: factors \
         to rank by, and point-in-time membership to rank within. Without them it selects \
         nothing and the simulated book sits in cash, which is not a result. Run \
         `compute_factors` and `refresh_index_membership` for the missing dates, or choose a \
         window the computed dates cover.",
        parts.join(" and ")
    ))
}

/// `_require_screen_coverage`: refuse a schedule a screen cannot answer on every date.
async fn require_screen_coverage(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    schedule: &[NaiveDate],
) -> Result<(), Stop> {
    if schedule.is_empty() {
        return Ok(());
    }
    let factors: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT DISTINCT factor_daily.date FROM factor_daily WHERE factor_daily.date = ANY($1)",
    )
    .bind(schedule)
    .fetch_all(&mut *conn)
    .await?;
    let covered_factors: BTreeSet<NaiveDate> = factors.into_iter().map(|(d,)| d).collect();
    let mut covered_members: BTreeSet<NaiveDate> = BTreeSet::new();
    if let Ok(universe) = universe_by_slug(&definition.index) {
        let members: Vec<(NaiveDate,)> = sqlx::query_as(
            "SELECT DISTINCT index_member_daily.date FROM index_member_daily WHERE \
             index_member_daily.index_id = $1 AND index_member_daily.date = ANY($2)",
        )
        .bind(universe.index_id)
        .bind(schedule)
        .fetch_all(&mut *conn)
        .await?;
        covered_members = members.into_iter().map(|(d,)| d).collect();
    }
    let missing_factors: Vec<NaiveDate> = schedule
        .iter()
        .copied()
        .filter(|d| !covered_factors.contains(d))
        .collect();
    let missing_members: Vec<NaiveDate> = schedule
        .iter()
        .copied()
        .filter(|d| !covered_members.contains(d))
        .collect();
    match coverage_refusal(
        schedule.len(),
        &definition.index,
        &missing_factors,
        &missing_members,
    ) {
        Some(message) => Err(Stop::data(message)),
        None => Ok(()),
    }
}

/// `float(text)` — correctly rounded, as Python's `float(Decimal)` is.
fn float_of(text: &str) -> Option<f64> {
    match text {
        "NaN" | "-NaN" | "sNaN" | "-sNaN" => Some(f64::NAN),
        "Infinity" => Some(f64::INFINITY),
        "-Infinity" => Some(f64::NEG_INFINITY),
        other => other.parse::<f64>().ok(),
    }
}

/// `_number(value)`: a weighting input as a float, `None` for `NULL`.
pub(super) fn number(value: Option<&Payload>) -> Result<Option<f64>, Stop> {
    let bad = |shown: String| {
        Stop::data(format!(
            "{shown} is not a number a weighting scheme can use"
        ))
    };
    match value {
        None | Some(Payload::Null) => Ok(None),
        Some(Payload::Int(i)) => float_of(&i.to_string())
            .map(Some)
            .ok_or_else(|| bad(i.to_string())),
        Some(Payload::Float(f)) => Ok(Some(*f)),
        Some(Payload::Decimal(PyDecimal::Finite(d))) => {
            let text = decimal_text(d);
            float_of(&text).map(Some).ok_or_else(|| bad(text))
        }
        Some(Payload::Decimal(PyDecimal::NonFinite(s))) => {
            float_of(s).map(Some).ok_or_else(|| bad(s.clone()))
        }
        Some(Payload::Bool(b)) => Ok(Some(if *b { 1.0 } else { 0.0 })),
        Some(Payload::Str(s)) => Err(bad(baskfy_providers::py::repr_str(s))),
        Some(other) => Err(bad(format!("{other:?}"))),
    }
}

fn decimal_text(d: &DecimalValue) -> String {
    d.format_f()
}

/// `str(value)` of a screen cell (`symbol`, `name`): `None` prints as `"None"`.
fn str_of(value: Option<&Payload>) -> String {
    match value {
        None | Some(Payload::Null) => "None".to_owned(),
        Some(Payload::Str(s)) => s.clone(),
        Some(Payload::Int(i)) => i.to_string(),
        Some(Payload::Float(f)) => baskfy_core::py::float_repr(*f),
        Some(Payload::Bool(b)) => if *b { "True" } else { "False" }.to_owned(),
        Some(Payload::Date(d)) => d.format("%Y-%m-%d").to_string(),
        Some(Payload::Decimal(PyDecimal::Finite(d))) => d.format_f(),
        Some(Payload::Decimal(PyDecimal::NonFinite(s))) => s.clone(),
        Some(other) => format!("{other:?}"),
    }
}

/// `_screen_frames`: one screen per date, as of that date, and the union of what they selected.
async fn screen_frames(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    dates: &[NaiveDate],
    data_version: i64,
    limit: i64,
    progress: &mut Option<Sink<'_>>,
) -> Result<(BTreeMap<NaiveDate, DataFrame>, BTreeSet<i64>), Stop> {
    let columns: Vec<String> = BACKTEST_SCREEN_COLUMNS
        .iter()
        .map(|c| (*c).to_owned())
        .collect();
    let mut frames = BTreeMap::new();
    let mut candidates = BTreeSet::new();
    for (index, day) in dates.iter().enumerate() {
        let result =
            execute_screen_with_limit(conn, definition, *day, data_version, &columns, limit)
                .await?;
        let n = result.rows.len();
        let mut ids = Vec::with_capacity(n);
        let mut symbols = Vec::with_capacity(n);
        let mut names = Vec::with_capacity(n);
        let mut ranks = Vec::with_capacity(n);
        let mut caps = Vec::with_capacity(n);
        let mut vols = Vec::with_capacity(n);
        for row in &result.rows {
            let value = |k: &str| row.values.iter().find(|(n, _)| n == k).map(|(_, v)| v);
            candidates.insert(row.instrument_id);
            ids.push(row.instrument_id);
            symbols.push(str_of(value("symbol")));
            names.push(str_of(value("name")));
            ranks.push(row.rank);
            caps.push(number(value("marketcap_cr"))?);
            vols.push(number(value("vol_12m"))?);
        }
        // The engine's look-ahead guard checks `date`: the as-of the screen actually ran for.
        let frame = DataFrame::new(
            n,
            vec![
                Series::new("date".into(), vec![result.as_of; n]).into_column(),
                Series::new("instrument_id".into(), ids).into_column(),
                Series::new("symbol".into(), symbols).into_column(),
                Series::new("name".into(), names).into_column(),
                Series::new("rank".into(), ranks).into_column(),
                Series::new("marketcap_cr".into(), caps).into_column(),
                Series::new("vol_12m".into(), vols).into_column(),
            ],
        )
        .map_err(|e| pl(&e))?;
        let frame = ensure_date(frame, "date")?;
        frames.insert(*day, frame);
        if let Some(sink) = progress.as_mut() {
            sink(&BacktestProgress {
                stage: "screening".to_owned(),
                completed: index + 1,
                total: dates.len(),
                as_of: Some(*day),
            });
        }
    }
    Ok((frames, candidates))
}

/// A frame whose `name` column is cast to `Date` (an empty `Vec<NaiveDate>` builds one already;
/// the cast is a no-op then, and keeps the schema fixed whatever the row count).
fn ensure_date(frame: DataFrame, name: &str) -> Result<DataFrame, Stop> {
    let mut frame = frame;
    let cast = frame
        .column(name)
        .map_err(|e| pl(&e))?
        .cast(&DataType::Date)
        .map_err(|e| pl(&e))?;
    frame.with_column(cast).map_err(|e| pl(&e))?;
    Ok(frame)
}

/// `_require_a_loadable_size`: one indexed COUNT before any bar is read (M45.8).
async fn require_a_loadable_size(
    conn: &mut PgConnection,
    instrument_ids: &[i64],
    start: NaiveDate,
    end: NaiveDate,
) -> Result<(), Stop> {
    if instrument_ids.is_empty() {
        return Ok(());
    }
    let (rows,): (i64,) = sqlx::query_as(
        "SELECT count(*) AS count_1 FROM ohlcv_daily WHERE ohlcv_daily.instrument_id = ANY($1) \
         AND ohlcv_daily.date >= $2 AND ohlcv_daily.date <= $3",
    )
    .bind(instrument_ids)
    .bind(start)
    .bind(end)
    .fetch_one(&mut *conn)
    .await?;
    if rows > MAX_BAR_ROWS {
        return Err(Stop::data(format!(
            "this configuration would load {} daily bars for {} instruments, above the {} a \
             single backtest may hold in memory. Shorten the window, or select fewer names.",
            thousands(rows),
            thousands(i64::try_from(instrument_ids.len()).unwrap_or(i64::MAX)),
            thousands(MAX_BAR_ROWS)
        )));
    }
    Ok(())
}

/// Python's `f"{n:,}"`.
pub(super) fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

/// `_bar_frame`: the candidates' **adjusted** opens and closes, as floats (`float(Decimal)`:
/// the numeric's text, correctly rounded).
async fn bar_frame(
    conn: &mut PgConnection,
    instrument_ids: &[i64],
    start: NaiveDate,
    end: NaiveDate,
) -> Result<DataFrame, Stop> {
    let rows = if instrument_ids.is_empty() {
        Vec::new()
    } else {
        sqlx::query(
            "SELECT ohlcv_daily.date, ohlcv_daily.instrument_id, ohlcv_daily.open::text AS open, \
             ohlcv_daily.close::text AS close FROM ohlcv_daily WHERE ohlcv_daily.instrument_id = \
             ANY($1) AND ohlcv_daily.date >= $2 AND ohlcv_daily.date <= $3 ORDER BY \
             ohlcv_daily.instrument_id, ohlcv_daily.date",
        )
        .bind(instrument_ids)
        .bind(start)
        .bind(end)
        .fetch_all(&mut *conn)
        .await?
    };
    let n = rows.len();
    let mut days = Vec::with_capacity(n);
    let mut ids = Vec::with_capacity(n);
    let mut opens = Vec::with_capacity(n);
    let mut closes = Vec::with_capacity(n);
    let not_a_float = |what: &str, text: &str| {
        Stop::Raised(WorkerError::python(
            "ValueError",
            format!("could not convert {what} {text:?} to float"),
        ))
    };
    for row in &rows {
        days.push(row.try_get::<NaiveDate, _>("date")?);
        ids.push(row.try_get::<i64, _>("instrument_id")?);
        let open: String = row.try_get("open")?;
        let close: String = row.try_get("close")?;
        opens.push(float_of(&open).ok_or_else(|| not_a_float("open", &open))?);
        closes.push(float_of(&close).ok_or_else(|| not_a_float("close", &close))?);
    }
    let frame = DataFrame::new(
        n,
        vec![
            Series::new("date".into(), days).into_column(),
            Series::new("instrument_id".into(), ids).into_column(),
            Series::new("open".into(), opens).into_column(),
            Series::new("close".into(), closes).into_column(),
        ],
    )
    .map_err(|e| pl(&e))?;
    ensure_date(frame, "date")
}

/// `_benchmark_frame`: the benchmark's levels, or a note saying why there are none.
async fn benchmark_frame(
    conn: &mut PgConnection,
    slug: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<(Option<DataFrame>, Option<String>), Stop> {
    let index_id: Option<(i64,)> =
        sqlx::query_as("SELECT index_def.id::bigint FROM index_def WHERE index_def.slug = $1")
            .bind(slug)
            .fetch_optional(&mut *conn)
            .await?;
    let Some((index_id,)) = index_id else {
        return Ok((
            None,
            Some(format!(
                "no index_def row has slug {}, so no benchmark was loaded",
                repr(slug)
            )),
        ));
    };
    let rows: Vec<(NaiveDate, String)> = sqlx::query_as(
        "SELECT index_snapshot_daily.date, index_snapshot_daily.level::text FROM \
         index_snapshot_daily WHERE index_snapshot_daily.index_id = $1 AND \
         index_snapshot_daily.date >= $2 AND index_snapshot_daily.date <= $3 AND \
         index_snapshot_daily.level IS NOT NULL ORDER BY index_snapshot_daily.date",
    )
    .bind(index_id)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        return Ok((
            None,
            Some(format!(
                "{slug} has no index_snapshot_daily levels between {} and {}",
                start.format("%Y-%m-%d"),
                end.format("%Y-%m-%d")
            )),
        ));
    }
    let n = rows.len();
    let mut days = Vec::with_capacity(n);
    let mut levels = Vec::with_capacity(n);
    for (day, level) in rows {
        days.push(day);
        levels.push(float_of(&level).ok_or_else(|| {
            Stop::Raised(WorkerError::python(
                "ValueError",
                format!("could not convert level {level:?} to float"),
            ))
        })?);
    }
    let frame = DataFrame::new(
        n,
        vec![
            Series::new("date".into(), days).into_column(),
            Series::new("level".into(), levels).into_column(),
        ],
    )
    .map_err(|e| pl(&e))?;
    Ok((Some(ensure_date(frame, "date")?), None))
}

/// `(symbols, names, delistings)` by instrument id.
type InstrumentMeta = (
    HashMap<i64, String>,
    HashMap<i64, String>,
    HashMap<i64, NaiveDate>,
);

/// `_instrument_meta`: symbols, names and delisting dates of the candidates.
async fn instrument_meta(
    conn: &mut PgConnection,
    instrument_ids: &[i64],
) -> Result<InstrumentMeta, Stop> {
    let mut symbols = HashMap::new();
    let mut names = HashMap::new();
    let mut delistings = HashMap::new();
    if instrument_ids.is_empty() {
        return Ok((symbols, names, delistings));
    }
    let rows: Vec<(i64, String, Option<String>, Option<NaiveDate>)> = sqlx::query_as(
        "SELECT instrument.id, instrument.symbol, instrument.name, instrument.delisted_on FROM \
         instrument WHERE instrument.id = ANY($1)",
    )
    .bind(instrument_ids)
    .fetch_all(&mut *conn)
    .await?;
    for (id, symbol, name, delisted_on) in rows {
        symbols.insert(id, symbol);
        names.insert(id, name.unwrap_or_else(|| "None".to_owned()));
        if let Some(day) = delisted_on {
            delistings.insert(id, day);
        }
    }
    Ok((symbols, names, delistings))
}

/// `_adjustment_coverage`: the two spliced segments of `ohlcv_daily`, measured.
async fn adjustment_coverage(conn: &mut PgConnection) -> Result<AdjustmentCoverage, Stop> {
    let (actions_from, actions_to): (Option<NaiveDate>, Option<NaiveDate>) = sqlx::query_as(
        "SELECT min(corporate_action.ex_date) AS min_1, max(corporate_action.ex_date) AS max_1 \
         FROM corporate_action",
    )
    .fetch_one(&mut *conn)
    .await?;
    let (self_adjusted_from,): (Option<NaiveDate>,) = sqlx::query_as(
        "SELECT min(ohlcv_daily.date) AS min_1 FROM ohlcv_daily WHERE ohlcv_daily.source != 'kite'",
    )
    .fetch_one(&mut *conn)
    .await?;
    let (deep_from,): (Option<NaiveDate>,) = sqlx::query_as(
        "SELECT min(ohlcv_daily.date) AS min_1 FROM ohlcv_daily WHERE ohlcv_daily.source = 'kite'",
    )
    .fetch_one(&mut *conn)
    .await?;
    let splice_on = if deep_from.is_some() && self_adjusted_from.is_some() {
        self_adjusted_from
    } else {
        None
    };
    Ok(AdjustmentCoverage {
        actions_from,
        actions_to,
        self_adjusted_from,
        splice_on,
        as_of_today: true,
    })
}

/// `load_backtest_data(session, config, definition, with_offsets=..., progress=...)`.
pub(super) async fn load_backtest_data(
    conn: &mut PgConnection,
    config: &BacktestConfig,
    definition: &ScreenDefinition,
    with_offsets: bool,
    progress: &mut Option<Sink<'_>>,
) -> Result<LoadedBacktest, Stop> {
    let calendar = trading_calendar(conn, config.start, config.end).await?;
    if calendar.is_empty() {
        return Err(Stop::data(format!(
            "the NSE trading calendar has no day between {} and {}",
            config.start.format("%Y-%m-%d"),
            config.end.format("%Y-%m-%d")
        )));
    }
    let schedule = rebalance_dates(&calendar, config.start, config.end, &config.rebalance);
    if schedule.is_empty() {
        return Err(Stop::data("the configuration produces no rebalance dates"));
    }
    let offsets: Vec<NaiveDate> = if with_offsets {
        let scheduled: BTreeSet<NaiveDate> = schedule.iter().copied().collect();
        let mut around: BTreeSet<NaiveDate> = shift_schedule(&calendar, &schedule, -1)
            .into_iter()
            .collect();
        around.extend(shift_schedule(&calendar, &schedule, 1));
        around.difference(&scheduled).copied().collect()
    } else {
        Vec::new()
    };
    let wanted: Vec<NaiveDate> = schedule
        .iter()
        .chain(offsets.iter())
        .copied()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if wanted.len() > MAX_REBALANCE_DATES {
        return Err(Stop::data(format!(
            "this configuration needs {} screen runs, above the {MAX_REBALANCE_DATES} a single \
             backtest may issue. Use a longer rebalance interval or a shorter window.",
            wanted.len()
        )));
    }

    require_screen_coverage(conn, definition, &schedule).await?;

    let data_version = current_data_version(conn).await?;
    let limit = config.selection.top_n + config.selection.hold_buffer;
    let (frames, candidates) =
        screen_frames(conn, definition, &wanted, data_version, limit, progress).await?;
    let candidates: Vec<i64> = candidates.into_iter().collect();

    require_a_loadable_size(conn, &candidates, config.start, config.end).await?;
    let bars = bar_frame(conn, &candidates, config.start, config.end).await?;
    if bars.height() == 0 {
        return Err(Stop::data(
            "no adjusted bars exist for any name this screen selected in the requested window; \
             there is nothing to simulate",
        ));
    }
    let (benchmark, benchmark_note) =
        benchmark_frame(conn, &config.benchmark, config.start, config.end).await?;
    let (symbols, names, delistings) = instrument_meta(conn, &candidates).await?;

    let notes: Vec<String> = benchmark_note.into_iter().collect();
    let prices = PricePanel::new(&bars, &calendar, false)?;
    let coverage = adjustment_coverage(conn).await?;
    let data = BacktestData {
        calendar,
        prices,
        screens: frames,
        benchmark,
        delistings,
        symbols,
        names,
        dividends: None,
        data_version: Some(data_version),
        adjustment_coverage: Some(coverage),
    };
    Ok(LoadedBacktest { data, notes })
}

/// `execute_backtest(session, config, definition, fragility=..., progress=...)`: attach the
/// T-bill curve, load, simulate, and — unless asked not to — run docs/10's four perturbations.
pub(super) async fn execute_backtest(
    conn: &mut PgConnection,
    config: &BacktestConfig,
    definition: &ScreenDefinition,
    fragility: bool,
    mut progress: Option<Sink<'_>>,
) -> Result<BacktestOutcome, Stop> {
    let config = attach_tbill_curve(config).map_err(Stop::from)?;
    let loaded = load_backtest_data(conn, &config, definition, fragility, &mut progress).await?;
    let sink: Option<&mut dyn FnMut(&BacktestProgress)> = match progress.as_mut() {
        Some(s) => Some(&mut **s),
        None => None,
    };
    let result = run_backtest(&config, &loaded.data, sink, None)?;
    let mut report = None;
    if fragility {
        if let Some(sink) = progress.as_mut() {
            sink(&BacktestProgress {
                stage: "fragility".to_owned(),
                completed: 0,
                total: 4,
                as_of: None,
            });
        }
        report = Some(run_fragility(
            &config,
            &loaded.data,
            Some(result.clone()),
            None,
        )?);
        if let Some(sink) = progress.as_mut() {
            sink(&BacktestProgress {
                stage: "fragility".to_owned(),
                completed: 4,
                total: 4,
                as_of: None,
            });
        }
    }
    Ok(BacktestOutcome {
        loaded,
        result,
        fragility: report,
    })
}

/// `notes_for(loaded, result)`: the loader's notes, then the run's, first occurrence kept.
pub(super) fn notes_for(loaded: &LoadedBacktest, result: &BacktestResult) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for note in loaded.notes.iter().chain(result.notes.iter()) {
        if !out.contains(note) {
            out.push(note.clone());
        }
    }
    out
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    #[test]
    fn the_refusal_names_both_halves_and_what_to_run() {
        // test_the_refusal_names_the_dates_so_it_can_be_acted_on (Python, against the database).
        let missing: Vec<NaiveDate> = (1..=7).map(|i| d(2024, 1, i)).collect();
        let message = coverage_refusal(9, "nifty-500", &missing, &missing[..2]).unwrap();
        assert_eq!(
            message,
            "of 9 rebalance dates, 7 have no factor rows (2024-01-01, 2024-01-02, 2024-01-03, \
             2024-01-04, 2024-01-05 and 2 more) and 2 have no 'nifty-500' index membership \
             (2024-01-01, 2024-01-02). The screen needs both on every rebalance date: factors to \
             rank by, and point-in-time membership to rank within. Without them it selects \
             nothing and the simulated book sits in cash, which is not a result. Run \
             `compute_factors` and `refresh_index_membership` for the missing dates, or choose a \
             window the computed dates cover."
        );
        assert!(coverage_refusal(3, "nifty-500", &[], &[]).is_none());
    }

    #[test]
    fn a_weighting_input_is_a_float_or_none() {
        assert_eq!(number(None).unwrap(), None);
        assert_eq!(number(Some(&Payload::Null)).unwrap(), None);
        assert_eq!(number(Some(&Payload::Int(7))).unwrap(), Some(7.0));
        assert_eq!(number(Some(&Payload::Float(0.25))).unwrap(), Some(0.25));
        match number(Some(&Payload::Str("x".into()))) {
            Err(Stop::Caught { class, message }) => {
                assert_eq!(class, "BacktestDataError");
                assert_eq!(message, "'x' is not a number a weighting scheme can use");
            }
            _ => panic!("a string must be refused"),
        }
    }

    #[test]
    fn counts_print_with_thousands_separators() {
        assert_eq!(thousands(3_000_000), "3,000,000");
        assert_eq!(thousands(809_807), "809,807");
        assert_eq!(thousands(12), "12");
        assert_eq!(thousands(0), "0");
    }

    #[test]
    fn a_failure_the_job_does_not_catch_is_raised() {
        assert!(matches!(
            Stop::from(BacktestError::Decimal("x".into())),
            Stop::Raised(_)
        ));
        assert!(matches!(
            Stop::from(BacktestError::Data("x".into())),
            Stop::Caught { .. }
        ));
        assert!(matches!(
            Stop::from(WorkerError::python("ScreenQueryError", "x")),
            Stop::Caught { .. }
        ));
        assert!(matches!(
            Stop::from(WorkerError::python("TypeError", "x")),
            Stop::Raised(_)
        ));
    }
}
