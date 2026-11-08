//! Step 9 — `data_quality_gate`. Ports `baskfy_worker.tasks.quality`.
//!
//! docs/09 §"Data-quality gate (hard blocker)": "Fail the run — and do **not** bump
//! `data_version` — if any assertion trips." Eight named checks, each returning a verdict of
//! `passed`, `failed` or `skipped` (an input it needs does not exist yet — reported, never
//! counted as a pass and never as a failure). The orchestrator turns a report with a failure
//! into the chain's `HardFailure` after this step's row is written.
//!
//! Every statement is the SQL SQLAlchemy compiles for Python's query (same text, same bind
//! order), so the one result whose order the database chooses — assertion 2's offender list,
//! which has no `ORDER BY` — comes back in the same order. Every number in the step row is
//! produced as Python produces it: `Decimal` moves in the default context
//! ([`WideDecimal`]), `statistics.median`/`fmean`/`pstdev` bit for bit ([`super::pystats`]),
//! `float(Decimal)` through the decimal string, and `f"{x:.Nf}"` half-to-even.

use std::collections::HashMap;
use std::str::FromStr as _;

use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use sqlx::{PgConnection, Row as _};

use super::pystats::{Median, fmean, format_fixed, format_percent0, median_ints, pstdev};
use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::pyjson::{self, object};
use crate::settings::WorkerSettings;
use crate::steps::StepOutcome;

/// `BASELINE_TRADING_DAYS`: docs/09 assertion 1's window.
pub(crate) const BASELINE_TRADING_DAYS: i64 = 10;
/// `RET_MEDIAN_HISTORY_DAYS`: docs/09 assertion 8's window.
pub(crate) const RET_MEDIAN_HISTORY_DAYS: i64 = 60;
/// `MIN_MEDIANS_FOR_SIGMA`.
pub(crate) const MIN_MEDIANS_FOR_SIGMA: usize = 2;

/// `NOMINAL_SIZES`: assertion 5's constituent counts, read from each universe's own name.
pub(crate) const NOMINAL_SIZES: [(&str, i64); 10] = [
    ("nifty-50", 50),
    ("nifty-next-50", 50),
    ("nifty-100", 100),
    ("nifty-200", 200),
    ("nifty-500", 500),
    ("nifty-large-mid-250", 250),
    ("nifty-midcap-150", 150),
    ("nifty-smallcap-250", 250),
    ("nifty-microcap-250", 250),
    ("nifty-mid-small-400", 400),
];

/// `NO_MEMBERSHIP_SOURCE`: the two universes the gate reports empty but never fails a night over
/// (`etf`, `nifty-fno`; quality.py carries the 27 Aug 2026 reasoning).
pub(crate) const NO_MEMBERSHIP_SOURCE: [&str; 2] = ["etf", "nifty-fno"];

/// `baskfy_core.universes.UNIVERSES` as `(index_id, slug)`, in id order (a private copy, as
/// R2.3 allows; `baskfy_core::screener` keeps its own).
pub(crate) const UNIVERSES: [(i64, &str); 15] = [
    (1, "nifty-50"),
    (2, "nifty-next-50"),
    (3, "nifty-100"),
    (4, "nifty-200"),
    (5, "nifty-500"),
    (6, "nifty-total-market"),
    (7, "nifty-large-mid-250"),
    (8, "nifty-midcap-150"),
    (9, "nifty-smallcap-250"),
    (10, "nifty-microcap-250"),
    (11, "nifty-mid-small-400"),
    (12, "nifty-allcap"),
    (13, "nifty-fno"),
    (14, "etf"),
    (15, "nse-sme-emerge"),
];

/// `sorted(SME_SERIES)`: the NSE Emerge series.
pub(crate) const SME_SERIES: [&str; 3] = ["SM", "ST", "SZ"];

/// `CheckStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CheckStatus {
    Passed,
    Failed,
    Skipped,
}

impl CheckStatus {
    pub(crate) fn value(self) -> &'static str {
        match self {
            Self::Passed => "passed",
            Self::Failed => "failed",
            Self::Skipped => "skipped",
        }
    }
}

/// `CheckResult`: one assertion's verdict, with what it observed.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct CheckResult {
    pub(crate) name: &'static str,
    pub(crate) assertion: i64,
    pub(crate) status: CheckStatus,
    pub(crate) message: String,
    pub(crate) observed: OrderedJson,
}

impl CheckResult {
    fn new(
        name: &'static str,
        assertion: i64,
        status: CheckStatus,
        message: impl Into<String>,
        observed: OrderedJson,
    ) -> Self {
        Self {
            name,
            assertion,
            status,
            message: message.into(),
            observed,
        }
    }

    /// `blocks_publish`.
    pub(crate) fn blocks_publish(&self) -> bool {
        self.status == CheckStatus::Failed
    }
}

/// `GateContext`'s settings half: the five thresholds `WorkerSettings` carries.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct Thresholds {
    pub(crate) min_bar_ratio: f64,
    pub(crate) max_unexplained_return: f64,
    pub(crate) membership_tolerance: f64,
    pub(crate) ret_median_sigma: f64,
}

impl Thresholds {
    pub(crate) fn from_settings(settings: &WorkerSettings) -> Self {
        Self {
            min_bar_ratio: settings.gate_min_bar_ratio,
            max_unexplained_return: settings.gate_max_unexplained_return,
            membership_tolerance: settings.gate_membership_tolerance,
            ret_median_sigma: settings.gate_ret_median_sigma,
        }
    }
}

fn int(v: i64) -> OrderedJson {
    OrderedJson::Int(v)
}

fn float(v: f64) -> OrderedJson {
    OrderedJson::Float(v)
}

fn empty() -> OrderedJson {
    OrderedJson::Object(Vec::new())
}

fn median_json(m: Median) -> OrderedJson {
    match m {
        Median::Int(i) => int(i),
        Median::Float(f) => float(f),
    }
}

/// `float(Decimal)`: Python converts through the decimal string, correctly rounded.
fn decimal_to_f64(text: &str) -> Result<f64, WorkerError> {
    text.parse::<f64>()
        .map_err(|e| WorkerError::python("ValueError", format!("{text}: {e}")))
}

fn decimal_error(e: &baskfy_core::decimal::DecimalError) -> WorkerError {
    WorkerError::python("InvalidOperation", e.0.clone())
}

// --- helpers (`_bar_count`, `previous_trading_days`, ...) -------------------------------------

async fn bar_count(conn: &mut PgConnection, on: NaiveDate) -> Result<i64, WorkerError> {
    let (count,): (i64,) = sqlx::query_as(
        "SELECT count(*) AS count_1 \nFROM ohlcv_daily \nWHERE ohlcv_daily.date = $1::DATE",
    )
    .bind(on)
    .fetch_one(&mut *conn)
    .await?;
    Ok(count)
}

/// `calendar.previous_trading_days(session, before, count)`: most recent first.
pub(crate) async fn previous_trading_days(
    conn: &mut PgConnection,
    before: NaiveDate,
    count: i64,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trading_day.date \nFROM trading_day \nWHERE trading_day.exchange_id = \
         $1::SMALLINT AND trading_day.is_trading_day IS true AND trading_day.date < $2::DATE \
         ORDER BY trading_day.date DESC \n LIMIT $3::INTEGER",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(before)
    .bind(i32::try_from(count).unwrap_or(i32::MAX))
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(d,)| d).collect())
}

async fn has_action_on(
    conn: &mut PgConnection,
    instrument_id: i64,
    on: NaiveDate,
) -> Result<bool, WorkerError> {
    let (found,): (i64,) = sqlx::query_as(
        "SELECT count(*) AS count_1 \nFROM corporate_action \nWHERE \
         corporate_action.instrument_id = $1::BIGINT AND corporate_action.ex_date = $2::DATE",
    )
    .bind(instrument_id)
    .bind(on)
    .fetch_one(&mut *conn)
    .await?;
    Ok(found > 0)
}

/// `_hit_circuit`: a close at or beyond the exchange's own band explains a move (docs/09 2).
async fn hit_circuit(
    conn: &mut PgConnection,
    instrument_id: i64,
    on: NaiveDate,
) -> Result<bool, WorkerError> {
    let rows = sqlx::query(
        "SELECT ohlcv_daily.close_raw, ohlcv_daily.upper_circuit, ohlcv_daily.lower_circuit \n\
         FROM ohlcv_daily \nWHERE ohlcv_daily.instrument_id = $1::BIGINT AND ohlcv_daily.date = \
         $2::DATE",
    )
    .bind(instrument_id)
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    // `.one_or_none()`.
    if rows.len() > 1 {
        return Err(multiple_rows());
    }
    let Some(row) = rows.first() else {
        return Ok(false);
    };
    let close = baskfy_db::row::opt_decimal(row, "close_raw")?;
    let upper = baskfy_db::row::opt_decimal(row, "upper_circuit")?;
    let lower = baskfy_db::row::opt_decimal(row, "lower_circuit")?;
    let Some(close) = close else {
        return Ok(false);
    };
    Ok(upper.is_some_and(|u| close >= u) || lower.is_some_and(|l| close <= l))
}

fn multiple_rows() -> WorkerError {
    WorkerError::python(
        "MultipleResultsFound",
        "Multiple rows were found when one or none was required",
    )
}

async fn ret_median(conn: &mut PgConnection, on: NaiveDate) -> Result<Option<f64>, WorkerError> {
    let (value,): (Option<f64>,) = sqlx::query_as(
        "SELECT percentile_cont($1::FLOAT) WITHIN GROUP (ORDER BY factor_daily.ret_12m) AS anon_1 \n\
         FROM factor_daily \nWHERE factor_daily.date = $2::DATE AND factor_daily.ret_12m IS NOT \
         NULL",
    )
    .bind(0.5_f64)
    .bind(on)
    .fetch_one(&mut *conn)
    .await?;
    // SQLAlchemy types `percentile_cont(...).within_group(ret_12m)` as the column's own
    // `Numeric(14, 2)`, so its result processor turns asyncpg's float into
    // `Decimal("%.2f" % value)` before `float()` reads it back: Postgres's -18.009999999999998
    // reaches the gate as -18.01. Reproduced exactly (found by nightly_diff, R-gate.1).
    value
        .map(|v| decimal_to_f64(&format_fixed(v, RET_12M_SCALE)))
        .transpose()
}

/// `factor_daily.ret_12m`'s declared scale (`NUMERIC(14, 2)`).
const RET_12M_SCALE: usize = 2;

// --- assertion 1 ------------------------------------------------------------------------------

/// docs/09 1: `count(bars for as_of) >= 0.9 x median(count over last 10 trading days)`.
pub(crate) async fn check_bar_count_against_baseline(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
    t: Thresholds,
) -> Result<CheckResult, WorkerError> {
    let today = bar_count(conn, trade_date).await?;
    let baseline_days = previous_trading_days(conn, trade_date, BASELINE_TRADING_DAYS).await?;
    let mut counts = Vec::with_capacity(baseline_days.len());
    for day in baseline_days {
        counts.push(bar_count(conn, day).await?);
    }
    Ok(bar_count_verdict(today, &counts, t.min_bar_ratio))
}

/// Assertion 1's verdict from the counts (the pure half, tested on its own).
pub(crate) fn bar_count_verdict(today: i64, counts: &[i64], ratio: f64) -> CheckResult {
    let populated: Vec<i64> = counts.iter().copied().filter(|c| *c > 0).collect();
    let name = "bar_count_against_baseline";
    let Some(median) = median_ints(&populated) else {
        return CheckResult::new(
            name,
            1,
            CheckStatus::Skipped,
            "no prior trading day has bars, so there is no baseline to compare against",
            object([("today", int(today))]),
        );
    };
    let threshold = ratio * median.as_f64();
    // `today >= threshold`: an int against a float, exactly (bar counts are far below 2**53).
    #[allow(clippy::cast_precision_loss)]
    let status = if today as f64 >= threshold {
        CheckStatus::Passed
    } else {
        CheckStatus::Failed
    };
    CheckResult::new(
        name,
        1,
        status,
        format!(
            "{today} bars against a {BASELINE_TRADING_DAYS}-day median of {} (threshold {})",
            format_fixed(median.as_f64(), 0),
            format_fixed(threshold, 0)
        ),
        object([
            ("today", int(today)),
            ("median", median_json(median)),
            ("threshold", float(threshold)),
        ]),
    )
}

// --- assertion 2 ------------------------------------------------------------------------------

/// docs/09 2: no `|r_t| > 0.5` unless a corporate action or a legitimate circuit explains it.
/// Over the adjusted `close`, the series every factor reads.
pub(crate) async fn check_no_unexplained_jumps(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
    t: Thresholds,
) -> Result<CheckResult, WorkerError> {
    let name = "no_unexplained_jumps";
    let Some(previous) = previous_trading_days(conn, trade_date, 1)
        .await?
        .into_iter()
        .next()
    else {
        return Ok(CheckResult::new(
            name,
            2,
            CheckStatus::Skipped,
            "no prior trading day to compare with",
            empty(),
        ));
    };

    // `Decimal(str(settings.gate_max_unexplained_return))`.
    let limit_text = baskfy_core::py::float_repr(t.max_unexplained_return);
    let limit = WideDecimal::from_str(&limit_text)
        .map_err(|e| WorkerError::python("InvalidOperation", e.0))?;
    let rows = sqlx::query(
        "SELECT ohlcv_daily.instrument_id, ohlcv_daily.close, (SELECT prior.close \nFROM \
         ohlcv_daily AS prior \nWHERE prior.instrument_id = ohlcv_daily.instrument_id AND \
         prior.date = $1::DATE) AS anon_1 \nFROM ohlcv_daily \nWHERE ohlcv_daily.date = $2::DATE \
         AND ohlcv_daily.close IS NOT NULL",
    )
    .bind(previous)
    .bind(trade_date)
    .fetch_all(&mut *conn)
    .await?;

    let mut offenders: Vec<OrderedJson> = Vec::new();
    let mut checked: i64 = 0;
    for row in &rows {
        let instrument_id: i64 = row.try_get("instrument_id")?;
        let close_today = baskfy_db::row::opt_decimal(row, "close")?;
        let close_prior = baskfy_db::row::opt_decimal(row, "anon_1")?;
        let (Some(today), Some(prior)) = (close_today, close_prior) else {
            continue;
        };
        if prior.is_zero() {
            continue;
        }
        checked += 1;
        let today = WideDecimal::from(today);
        let prior = WideDecimal::from(prior);
        let moved = today
            .sub(&prior)
            .div(&prior)
            .map_err(|e| decimal_error(&e))?;
        if moved.abs() <= limit {
            continue;
        }
        if has_action_on(conn, instrument_id, trade_date).await? {
            continue;
        }
        if hit_circuit(conn, instrument_id, trade_date).await? {
            continue;
        }
        offenders.push(object([
            ("instrument_id", int(instrument_id)),
            ("move", float(decimal_to_f64(&moved.to_sci_string())?)),
        ]));
    }

    let status = if offenders.is_empty() {
        CheckStatus::Passed
    } else {
        CheckStatus::Failed
    };
    // `float(limit)` is the setting itself: `Decimal(repr(x))` converts back to `x` exactly.
    let message = format!(
        "{} of {checked} instruments moved more than {} with no corporate action or circuit to \
         explain it",
        offenders.len(),
        format_percent0(decimal_to_f64(&limit.to_sci_string())?)
    );
    offenders.truncate(20);
    Ok(CheckResult::new(
        name,
        2,
        status,
        message,
        object([
            ("checked", int(checked)),
            ("offenders", OrderedJson::Array(offenders)),
        ]),
    ))
}

// --- assertion 3 ------------------------------------------------------------------------------

/// docs/09 3: no duplicate `(instrument_id, date)` anywhere — a check that the primary key is
/// still there.
pub(crate) async fn check_no_duplicate_bars(
    conn: &mut PgConnection,
) -> Result<CheckResult, WorkerError> {
    let (duplicates,): (i64,) = sqlx::query_as(
        "SELECT count(*) AS count_1 \nFROM (SELECT ohlcv_daily.instrument_id AS instrument_id, \
         ohlcv_daily.date AS date \nFROM ohlcv_daily GROUP BY ohlcv_daily.instrument_id, \
         ohlcv_daily.date \nHAVING count(*) > $1::INTEGER) AS anon_1",
    )
    .bind(1_i32)
    .fetch_one(&mut *conn)
    .await?;
    let status = if duplicates == 0 {
        CheckStatus::Passed
    } else {
        CheckStatus::Failed
    };
    Ok(CheckResult::new(
        "no_duplicate_bars",
        3,
        status,
        format!("{duplicates} duplicate (instrument_id, date) pairs in ohlcv_daily"),
        object([("duplicates", int(duplicates))]),
    ))
}

// --- assertion 4 ------------------------------------------------------------------------------

/// docs/09 4: `factor_daily` row count == `ohlcv_daily` row count for the date, ± 0.
pub(crate) async fn check_factor_rows_match_bars(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<CheckResult, WorkerError> {
    let bars = bar_count(conn, trade_date).await?;
    let (factors,): (i64,) = sqlx::query_as(
        "SELECT count(*) AS count_1 \nFROM factor_daily \nWHERE factor_daily.date = $1::DATE",
    )
    .bind(trade_date)
    .fetch_one(&mut *conn)
    .await?;
    Ok(factor_rows_verdict(factors, bars))
}

/// Assertion 4's verdict.
pub(crate) fn factor_rows_verdict(factors: i64, bars: i64) -> CheckResult {
    let status = if factors == bars {
        CheckStatus::Passed
    } else {
        CheckStatus::Failed
    };
    CheckResult::new(
        "factor_rows_match_bars",
        4,
        status,
        format!("{factors} factor rows against {bars} bars"),
        object([("factor_rows", int(factors)), ("bars", int(bars))]),
    )
}

// --- assertion 5 ------------------------------------------------------------------------------

/// docs/09 5: every selectable universe has a membership set within 5% of its nominal size;
/// the ones with no fixed size are checked for non-emptiness instead.
pub(crate) async fn check_universe_sizes(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
    t: Thresholds,
) -> Result<CheckResult, WorkerError> {
    let rows: Vec<(i16, i64)> = sqlx::query_as(
        "SELECT index_member_daily.index_id, count(*) AS count_1 \nFROM index_member_daily \n\
         WHERE index_member_daily.date = $1::DATE GROUP BY index_member_daily.index_id",
    )
    .bind(trade_date)
    .fetch_all(&mut *conn)
    .await?;
    let counts: HashMap<i64, i64> = rows.into_iter().map(|(i, c)| (i64::from(i), c)).collect();
    let (emerge_instruments,): (i64,) = sqlx::query_as(
        "SELECT count(*) AS count_1 \nFROM instrument \nWHERE instrument.series IN \
         ($1::VARCHAR, $2::VARCHAR, $3::VARCHAR)",
    )
    .bind(SME_SERIES[0])
    .bind(SME_SERIES[1])
    .bind(SME_SERIES[2])
    .fetch_one(&mut *conn)
    .await?;
    Ok(universe_sizes_verdict(
        &counts,
        emerge_instruments,
        t.membership_tolerance,
    ))
}

/// Assertion 5's verdict from the per-universe counts.
pub(crate) fn universe_sizes_verdict(
    counts: &HashMap<i64, i64>,
    emerge_instruments: i64,
    tolerance: f64,
) -> CheckResult {
    let mut problems: Vec<String> = Vec::new();
    let mut unsourced: Vec<&str> = Vec::new();
    let mut observed: Vec<(String, OrderedJson)> = Vec::new();
    for (index_id, slug) in UNIVERSES {
        let actual = counts.get(&index_id).copied().unwrap_or(0);
        pyjson::update(&mut observed, slug, int(actual));
        let nominal = NOMINAL_SIZES
            .iter()
            .find(|(s, _)| *s == slug)
            .map(|(_, n)| *n);
        let Some(nominal) = nominal else {
            if actual == 0 {
                // An Emerge universe on a box that never read the Emerge register, or one of
                // the two that nothing has to populate: reported, never fatal.
                let sourceless = slug == "nse-sme-emerge" && emerge_instruments == 0;
                if sourceless || NO_MEMBERSHIP_SOURCE.contains(&slug) {
                    unsourced.push(slug);
                } else {
                    problems.push(format!("{slug} has no members"));
                }
            }
            continue;
        };
        // `abs(actual - nominal) > nominal * tolerance`: an int against a float.
        #[allow(clippy::cast_precision_loss)]
        let over = (actual - nominal).unsigned_abs() as f64 > nominal as f64 * tolerance;
        if over {
            problems.push(format!(
                "{slug}: {actual} members against a nominal {nominal}"
            ));
        }
    }
    unsourced.sort_unstable();
    let status = if problems.is_empty() {
        CheckStatus::Passed
    } else {
        CheckStatus::Failed
    };
    let message = if !problems.is_empty() {
        problems.join("; ")
    } else if !unsourced.is_empty() {
        format!(
            "every sourced universe is within tolerance; no membership source wired for: {}",
            unsourced.join(", ")
        )
    } else {
        "every universe is within tolerance".to_owned()
    };
    CheckResult::new(
        "universe_sizes",
        5,
        status,
        message,
        object([
            ("counts", OrderedJson::Object(observed)),
            ("tolerance", float(tolerance)),
            (
                "unsourced",
                if unsourced.is_empty() {
                    OrderedJson::Null
                } else {
                    pyjson::strings(unsourced)
                },
            ),
        ]),
    )
}

// --- assertion 6 ------------------------------------------------------------------------------

/// docs/09 6: our NIFTY 50 level against the published snapshot. Only the half the data supports
/// is checked (quality.py says why): that a snapshot exists, and whether weights exist at all.
pub(crate) async fn check_index_level_agreement(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<CheckResult, WorkerError> {
    let name = "index_level_agreement";
    let rows = sqlx::query(
        "SELECT index_snapshot_daily.level \nFROM index_snapshot_daily JOIN index_def ON \
         index_def.id = index_snapshot_daily.index_id \nWHERE index_def.slug = $1::VARCHAR AND \
         index_snapshot_daily.date = $2::DATE",
    )
    .bind("nifty-50")
    .bind(trade_date)
    .fetch_all(&mut *conn)
    .await?;
    if rows.len() > 1 {
        return Err(multiple_rows());
    }
    let level = match rows.first() {
        Some(row) => baskfy_db::row::opt_decimal(row, "level")?,
        None => None,
    };
    let Some(level) = level else {
        return Ok(CheckResult::new(
            name,
            6,
            CheckStatus::Skipped,
            "no published NIFTY 50 snapshot for this date, so there is nothing to reconcile \
             against",
            empty(),
        ));
    };
    let published = decimal_to_f64(&level.to_string())?;
    let (weighted,): (i64,) = sqlx::query_as(
        "SELECT count(*) AS count_1 \nFROM index_member_daily \nWHERE index_member_daily.date = \
         $1::DATE AND index_member_daily.weight IS NOT NULL",
    )
    .bind(trade_date)
    .fetch_one(&mut *conn)
    .await?;
    Ok(index_level_verdict(published, weighted))
}

/// Assertion 6's verdict once a snapshot exists.
pub(crate) fn index_level_verdict(published: f64, weighted: i64) -> CheckResult {
    let name = "index_level_agreement";
    if weighted == 0 {
        return CheckResult::new(
            name,
            6,
            CheckStatus::Skipped,
            "index_member_daily.weight is unpopulated, so an index level cannot be reconstructed \
             from constituents; reconciliation needs free-float weights and the index divisor",
            object([("published_level", float(published))]),
        );
    }
    CheckResult::new(
        name,
        6,
        CheckStatus::Passed,
        format!(
            "published NIFTY 50 level {} with {weighted} weighted constituents",
            format_fixed(published, 2)
        ),
        object([
            ("published_level", float(published)),
            ("weighted_constituents", int(weighted)),
        ]),
    )
}

// --- assertion 7 ------------------------------------------------------------------------------

/// docs/09 7: zero NULLs in `close`, `close_raw`, `volume` for the date.
pub(crate) async fn check_no_null_prices(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<CheckResult, WorkerError> {
    let (nulls,): (i64,) = sqlx::query_as(
        "SELECT count(*) AS count_1 \nFROM ohlcv_daily \nWHERE ohlcv_daily.date = $1::DATE AND \
         (ohlcv_daily.close IS NULL OR ohlcv_daily.close_raw IS NULL OR ohlcv_daily.volume IS \
         NULL)",
    )
    .bind(trade_date)
    .fetch_one(&mut *conn)
    .await?;
    let status = if nulls == 0 {
        CheckStatus::Passed
    } else {
        CheckStatus::Failed
    };
    Ok(CheckResult::new(
        "no_null_prices",
        7,
        status,
        format!("{nulls} rows with a NULL close, close_raw or volume"),
        object([("nulls", int(nulls))]),
    ))
}

// --- assertion 8 ------------------------------------------------------------------------------

/// docs/09 8: the `ret_12m` median within ±3σ of its own 60-day history ("mass
/// mis-adjustment").
pub(crate) async fn check_return_distribution(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
    t: Thresholds,
) -> Result<CheckResult, WorkerError> {
    let today = ret_median(conn, trade_date).await?;
    let Some(today) = today else {
        return Ok(return_distribution_verdict(None, &[], t.ret_median_sigma));
    };
    let history_days = previous_trading_days(conn, trade_date, RET_MEDIAN_HISTORY_DAYS).await?;
    let mut medians = Vec::with_capacity(history_days.len());
    for day in history_days {
        if let Some(m) = ret_median(conn, day).await? {
            medians.push(m);
        }
    }
    Ok(return_distribution_verdict(
        Some(today),
        &medians,
        t.ret_median_sigma,
    ))
}

/// Assertion 8's verdict from today's median and the history's (NULLs already dropped).
pub(crate) fn return_distribution_verdict(
    today: Option<f64>,
    medians: &[f64],
    sigmas: f64,
) -> CheckResult {
    let name = "return_distribution";
    let Some(today) = today else {
        return CheckResult::new(
            name,
            8,
            CheckStatus::Skipped,
            "ret_12m is unpopulated for this date; the factor engine has not run",
            empty(),
        );
    };
    if medians.len() < MIN_MEDIANS_FOR_SIGMA {
        return CheckResult::new(
            name,
            8,
            CheckStatus::Skipped,
            format!(
                "only {} historical medians available; need at least {MIN_MEDIANS_FOR_SIGMA} \
                 for a sigma",
                medians.len()
            ),
            object([("today", float(today))]),
        );
    }
    let (Some(mean), Some(sigma)) = (fmean(medians), pstdev(medians)) else {
        // Unreachable: two or more finite medians always have both.
        return CheckResult::new(
            name,
            8,
            CheckStatus::Skipped,
            "the historical median series has zero variance; a sigma band is undefined",
            object([("today", float(today))]),
        );
    };
    if sigma == 0.0 {
        return CheckResult::new(
            name,
            8,
            CheckStatus::Skipped,
            "the historical median series has zero variance; a sigma band is undefined",
            object([("today", float(today)), ("mean", float(mean))]),
        );
    }
    let band = sigmas * sigma;
    let deviation = (today - mean).abs();
    let status = if deviation <= band {
        CheckStatus::Passed
    } else {
        CheckStatus::Failed
    };
    CheckResult::new(
        name,
        8,
        status,
        format!(
            "ret_12m median {} against a {}-day mean {} +/- {}",
            format_fixed(today, 2),
            medians.len(),
            format_fixed(mean, 2),
            format_fixed(band, 2)
        ),
        object([
            ("today", float(today)),
            ("mean", float(mean)),
            ("sigma", float(sigma)),
            ("band", float(band)),
        ]),
    )
}

// --- the gate ---------------------------------------------------------------------------------

/// `CHECKS`: docs/09's eight assertions, in the order the document lists them (the order
/// [`run_data_quality_gate`] runs them in).
#[cfg(test)]
pub(crate) const CHECK_NAMES: [&str; 8] = [
    "bar_count_against_baseline",
    "no_unexplained_jumps",
    "no_duplicate_bars",
    "factor_rows_match_bars",
    "universe_sizes",
    "index_level_agreement",
    "no_null_prices",
    "return_distribution",
];

/// `run_data_quality_gate`: every assertion, in order; never raises on a failed assertion.
pub(crate) async fn run_data_quality_gate(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    trade_date: NaiveDate,
    t: Thresholds,
) -> Result<Vec<CheckResult>, WorkerError> {
    let results = vec![
        check_bar_count_against_baseline(conn, trade_date, t).await?,
        check_no_unexplained_jumps(conn, trade_date, t).await?,
        check_no_duplicate_bars(conn).await?,
        check_factor_rows_match_bars(conn, trade_date).await?,
        check_universe_sizes(conn, trade_date, t).await?,
        check_index_level_agreement(conn, trade_date).await?,
        check_no_null_prices(conn, trade_date).await?,
        check_return_distribution(conn, trade_date, t).await?,
    ];
    record_report(outcome, trade_date, &results);
    Ok(results)
}

/// `outcome.rows_in/rows_out` and `outcome.note(**report.to_payload())`.
pub(crate) fn record_report(
    outcome: &mut StepOutcome,
    trade_date: NaiveDate,
    results: &[CheckResult],
) {
    outcome.rows_in = i64::try_from(results.len()).unwrap_or(i64::MAX);
    outcome.rows_out = i64::try_from(
        results
            .iter()
            .filter(|r| r.status == CheckStatus::Passed)
            .count(),
    )
    .unwrap_or(i64::MAX);
    let passed = !results.iter().any(CheckResult::blocks_publish);
    let checks = results
        .iter()
        .map(|r| {
            object([
                ("assertion", int(r.assertion)),
                ("name", pyjson::s(r.name)),
                ("status", pyjson::s(r.status.value())),
                ("message", pyjson::s(r.message.clone())),
                ("observed", r.observed.clone()),
            ])
        })
        .collect();
    outcome.note(
        "trade_date",
        pyjson::s(trade_date.format("%Y-%m-%d").to_string()),
    );
    outcome.note("passed", OrderedJson::Bool(passed));
    outcome.note("checks", OrderedJson::Array(checks));
}
