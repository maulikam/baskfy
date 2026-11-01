//! SW3 — the daily swing detection job. Ports `baskfy_worker.tasks.swing` as the nightly
//! reaches it (`run_detect_swing` with no `extra_bars`, `provisional=False`, no
//! `reference_date`, no `scan`, no live index level) and, since wave 6 (lane swing_live), as
//! "Scan now" reaches it too ([`run_detect_swing_scan`] with [`ScanExtras`]: SW15's provisional
//! bars, the `reference_date` the point-in-time reads fall back to, `detail.scan` and the live
//! index level). The nightly's path is the same code with every extra at its default.
//!
//! `baskfy_core::swing` computes the indicators and the three detectors; the breadth snapshot,
//! the gate and the EMA index reading come from [`super::swing_market`] (not yet in the core);
//! this module reads rows, applies the two score bonuses the core may not read (law 1), turns
//! adjusted levels back into exchange prices, and writes `sw_setup_daily` and
//! `sw_market_daily`. **Law 2:** a trigger is a number on a row and the rung is a preview; the
//! evening's `settle_ladder` owns the ladder and nothing here sizes, arms or places anything.
//! `execution_enabled` is accepted for the orchestrator's contract and read by nothing, as in
//! Python since A10 (SW10.5).

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::pl;
use baskfy_core::swing::market::{ExposureTier, MarketConfig, exposure_tier};
use baskfy_core::swing::setups::{
    LiquidityConfig, Setup, SwingConfig, detect_setups, with_swing_indicators,
};
use baskfy_core::swing::sizing::SizingConfig;
use baskfy_db::OrderedJson;
use baskfy_db::row::{decimal as row_decimal, opt_decimal};
use chrono::NaiveDate;
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use super::common::{
    self, Num, core_err, date_column, day_number, lookback_start, pl_err, store_float, value_err,
};
use super::swing_market::{
    self, BreadthRow, BreadthSnapshot, INDEX_HISTORY_SESSIONS, IndexReading, py_round,
};
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::{StepOutcome, StepStatus};

/// `LOOKBACK_SESSIONS`.
const LOOKBACK_SESSIONS: i64 = 200;
/// `CASH_SERIES`.
const CASH_SERIES: [&str; 2] = ["EQ", "BE"];
/// `YOUNG_LISTING_DAYS`.
const YOUNG_LISTING_DAYS: i64 = 730;
/// `YOUNG_LISTING_BONUS`, `HOT_SECTOR_BONUS`, `MAX_SCORE`.
const YOUNG_LISTING_BONUS: f64 = 5.0;
const HOT_SECTOR_BONUS: f64 = 5.0;
const MAX_SCORE: f64 = 100.0;
/// `HOT_SECTOR_COUNT`.
const HOT_SECTOR_COUNT: usize = 3;
/// `MIN_SECTOR_MEMBERS`.
const MIN_SECTOR_MEMBERS: i64 = 5;
/// `CLOSED_TRADE_WINDOW`.
const CLOSED_TRADE_WINDOW: i64 = 10;
/// `SETTLED_BY_EVENING` = `SYSTEM_OWNED_FIELDS["exposure_level"]`.
const SETTLED_BY_EVENING: &str = "swing-eod";
/// The gate's fallback benchmark.
const FALLBACK_INDEX: &str = "nifty-500";
/// `baskfy_core.universes.SECTOR_INDEX_SLUGS`.
const SECTOR_INDEX_SLUGS: [&str; 15] = [
    "nifty-auto",
    "nifty-bank",
    "nifty-chemicals",
    "nifty-consumer-durables",
    "nifty-financial-services",
    "nifty-fmcg",
    "nifty-healthcare-index",
    "nifty-it",
    "nifty-media",
    "nifty-metal",
    "nifty-oil-gas",
    "nifty-pharma",
    "nifty-private-bank",
    "nifty-psu-bank",
    "nifty-realty",
];
/// `Setup` in its declaration order.
const SETUPS: [Setup; 3] = [Setup::Flag, Setup::Ep, Setup::ParabolicShort];

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn dec_err(e: baskfy_core::decimal::DecimalError) -> WorkerError {
    WorkerError::python("InvalidOperation", e.to_string())
}

/// The `sw_config` columns the job reads.
pub(crate) struct ConfigRow {
    pub(crate) adr_min_pct: f64,
    pub(crate) turnover_min_inr: f64,
    pub(crate) price_min: f64,
    pub(crate) risk_per_trade_pct: f64,
    pub(crate) max_position_pct: f64,
    pub(crate) max_open_positions: i64,
    pub(crate) exposure_level: i64,
    pub(crate) sleeve_capital_inr: Decimal,
    pub(crate) sleeve_peak_inr: Option<Decimal>,
    pub(crate) drawdown_locked: bool,
}

pub(crate) async fn config_row(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Option<ConfigRow>, WorkerError> {
    let row = sqlx::query(
        "SELECT adr_min_pct::float8 AS adr_min_pct, turnover_min_inr::float8 AS \
         turnover_min_inr, price_min::float8 AS price_min, risk_per_trade_pct::float8 AS \
         risk_per_trade_pct, max_position_pct::float8 AS max_position_pct, \
         max_open_positions, exposure_level, sleeve_capital_inr, sleeve_peak_inr, \
         drawdown_locked FROM sw_config WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    let max_open: i16 = row.try_get("max_open_positions")?;
    let level: i16 = row.try_get("exposure_level")?;
    Ok(Some(ConfigRow {
        adr_min_pct: row.try_get("adr_min_pct")?,
        turnover_min_inr: row.try_get("turnover_min_inr")?,
        price_min: row.try_get("price_min")?,
        risk_per_trade_pct: row.try_get("risk_per_trade_pct")?,
        max_position_pct: row.try_get("max_position_pct")?,
        max_open_positions: i64::from(max_open),
        exposure_level: i64::from(level),
        sleeve_capital_inr: row_decimal(&row, "sleeve_capital_inr")?,
        sleeve_peak_inr: opt_decimal(&row, "sleeve_peak_inr")?,
        drawdown_locked: row.try_get("drawdown_locked")?,
    }))
}

/// `load_swing_config`: the defaults with the user's liquidity floors and sizing knobs.
pub(crate) fn swing_config(row: Option<&ConfigRow>) -> SwingConfig {
    let mut config = SwingConfig::default();
    if let Some(r) = row {
        config.liquidity = LiquidityConfig {
            adr_min_pct: r.adr_min_pct,
            turnover_min_inr: r.turnover_min_inr,
            price_min: r.price_min,
            ..LiquidityConfig::default()
        };
        config.sizing = SizingConfig {
            risk_per_trade_pct: r.risk_per_trade_pct,
            max_position_pct: r.max_position_pct,
            max_open_positions: r.max_open_positions,
            ..SizingConfig::default()
        };
    }
    config
}

/// One bar of `_bar_query`, numerics as `::float8` (Python's `float(Decimal)`).
type BarRow = (
    i64,
    String,
    NaiveDate,
    f64,
    f64,
    f64,
    f64,
    f64,
    Option<f64>,
    Option<f64>,
    f64,
);

/// `load_swing_bars`: adjusted bars of every live cash-market name, the circuit band moved
/// into the adjusted space.
pub(crate) async fn load_swing_bars(
    conn: &mut PgConnection,
    start: NaiveDate,
    as_of: NaiveDate,
) -> Result<DataFrame, WorkerError> {
    let series: Vec<String> = CASH_SERIES.iter().map(|s| (*s).to_owned()).collect();
    let records: Vec<BarRow> = sqlx::query_as(
        "SELECT o.instrument_id, i.symbol, o.date, o.open::float8, o.high::float8, \
         o.low::float8, o.close::float8, o.volume::float8, o.turnover::float8, \
         o.upper_circuit::float8, o.adj_factor::float8 \
         FROM ohlcv_daily o JOIN instrument i ON i.id = o.instrument_id \
         WHERE o.date >= $1 AND o.date <= $2 AND i.delisted_on IS NULL \
         AND i.instrument_type = 'EQ' AND i.series = ANY($3) \
         ORDER BY o.instrument_id, o.date",
    )
    .bind(start)
    .bind(as_of)
    .bind(&series)
    .fetch_all(&mut *conn)
    .await?;
    let n = records.len();
    let mut cols: [Vec<Option<f64>>; 8] = Default::default();
    let mut ids = Vec::with_capacity(n);
    let mut symbols = Vec::with_capacity(n);
    let mut days = Vec::with_capacity(n);
    for r in records {
        let factor = r.10;
        ids.push(r.0);
        symbols.push(r.1);
        days.push(Some(day_number(r.2)));
        cols[0].push(Some(r.3));
        cols[1].push(Some(r.4));
        cols[2].push(Some(r.5));
        cols[3].push(Some(r.6));
        cols[4].push(Some(r.7));
        cols[5].push(r.8);
        cols[6].push(r.9.map(|c| c * factor));
        cols[7].push(Some(factor));
    }
    let names = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "upper_circuit",
        "adj_factor",
    ];
    let mut columns = vec![
        Series::new("instrument_id".into(), ids).into_column(),
        Series::new("symbol".into(), symbols).into_column(),
        date_column("date", days)?,
    ];
    for (name, values) in names.iter().zip(cols) {
        columns.push(Series::new((*name).into(), values).into_column());
    }
    DataFrame::new(n, columns).map_err(|e| pl_err(&e))
}

/// `load_sector_membership`: `instrument_id -> sector slug` for `on`, the narrower index (by
/// member count that day) winning; the first seen wins a tie, in the window's partition order.
async fn load_sector_membership(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<BTreeMap<i64, String>, WorkerError> {
    let slugs: Vec<String> = SECTOR_INDEX_SLUGS.iter().map(|s| (*s).to_owned()).collect();
    let rows: Vec<(i64, String, i64)> = sqlx::query_as(
        "SELECT index_member_daily.instrument_id, index_def.slug, \
         count(*) OVER (PARTITION BY index_member_daily.index_id) AS anon_1 \
         FROM index_member_daily JOIN index_def ON index_def.id = index_member_daily.index_id \
         WHERE index_member_daily.date = $1 AND index_def.slug = ANY($2)",
    )
    .bind(on)
    .bind(&slugs)
    .fetch_all(&mut *conn)
    .await?;
    let mut best: BTreeMap<i64, (i64, String)> = BTreeMap::new();
    for (id, slug, members) in rows {
        match best.get(&id) {
            Some((count, _)) if members >= *count => {}
            _ => {
                best.insert(id, (members, slug));
            }
        }
    }
    Ok(best.into_iter().map(|(id, (_, slug))| (id, slug)).collect())
}

/// `load_listing_dates`.
async fn load_listing_dates(
    conn: &mut PgConnection,
) -> Result<BTreeMap<i64, NaiveDate>, WorkerError> {
    let rows: Vec<(i64, NaiveDate)> =
        sqlx::query_as("SELECT id, listed_on FROM instrument WHERE listed_on IS NOT NULL")
            .fetch_all(&mut *conn)
            .await?;
    Ok(rows.into_iter().collect())
}

/// The liquid names of the day: rows of the indicated frame, the columns the job reads.
struct Liquid {
    ids: Vec<i64>,
    close: Vec<Option<f64>>,
    ma_slow: Vec<Option<f64>>,
    ret_20: Vec<Option<f64>>,
    high: Vec<Option<f64>>,
}

/// `sector_breadth`: `(slug, % above the slow MA, members)` per sector with at least
/// `MIN_SECTOR_MEMBERS` liquid names, strongest first, then by slug.
fn sector_breadth(liquid: &Liquid, sectors: &BTreeMap<i64, String>) -> Vec<(String, f64, i64)> {
    if liquid.ids.is_empty() || sectors.is_empty() {
        return Vec::new();
    }
    let mut groups: BTreeMap<String, (i64, i64)> = BTreeMap::new();
    for (i, id) in liquid.ids.iter().enumerate() {
        let Some(slug) = sectors.get(id) else {
            continue;
        };
        if liquid.ma_slow[i].is_none() {
            continue;
        }
        let entry = groups.entry(slug.clone()).or_insert((0, 0));
        entry.0 += 1;
        // `(close > ma_slow).sum()`: a null comparison adds nothing.
        if pl::cmp(liquid.close[i], liquid.ma_slow[i], |o| o.is_gt()) == Some(true) {
            entry.1 += 1;
        }
    }
    let mut out: Vec<(String, f64, i64)> = groups
        .into_iter()
        .filter(|(_, (members, _))| *members >= MIN_SECTOR_MEMBERS)
        .map(|(slug, (members, above))| {
            #[allow(clippy::cast_precision_loss)]
            let pct = py_round(above as f64 / members as f64 * 100.0, 2);
            (slug, pct, members)
        })
        .collect();
    out.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    out
}

/// `to_exchange_prices` for one level: divided by the row's factor, `when(adj_factor > 0)
/// .then(adj_factor).otherwise(1.0)` — a null or non-positive factor divides by one.
fn to_exchange_price(value: Option<f64>, factor: Option<f64>) -> Option<f64> {
    let factor = match factor {
        Some(x) if pl::gt(x, 0.0) => x,
        _ => 1.0,
    };
    value.map(|x| x / factor)
}

/// One `sw_setup_daily` row.
struct SetupRow {
    instrument_id: i64,
    setup: String,
    status: String,
    score: Option<Decimal>,
    close: Option<Decimal>,
    trigger: Option<Decimal>,
    stop_ref: Option<Decimal>,
    pivot_high: Option<Decimal>,
    adj_factor: Option<Decimal>,
    adr_pct: Option<Decimal>,
    prior_move_pct: Option<Decimal>,
    base_depth_pct: Option<Decimal>,
    tightness_adr: Option<Decimal>,
    dryup_ratio: Option<Decimal>,
    dist_ma_fast_pct: Option<Decimal>,
    dist_ma_slow_pct: Option<Decimal>,
    rvol: Option<Decimal>,
    gap_pct: Option<Decimal>,
    turnover_avg: Option<i64>,
    base_bars: Option<i64>,
    up_streak: Option<i64>,
    locked_upper_circuit: bool,
    sector_slug: Option<String>,
    listed_within_2y: bool,
}

/// `to_exchange_prices` + `apply_score_adjustments` + `apply_storage_precision` + the payload
/// of `_write_setups`.
fn setup_rows(
    candidates: &DataFrame,
    young: &BTreeMap<i64, bool>,
    sectors: &BTreeMap<i64, String>,
    hot: &BTreeSet<String>,
) -> Result<Vec<SetupRow>, WorkerError> {
    let n = candidates.height();
    let ids = common::ints(candidates, "instrument_id")?;
    let setups = common::strings(candidates, "setup")?;
    let statuses = common::strings(candidates, "status")?;
    let factors = common::floats(candidates, "adj_factor")?;
    let scores = common::floats(candidates, "score")?;
    let locked = common::bools(candidates, "locked_upper_circuit")?;
    let level = |name: &str| -> Result<Vec<Option<f64>>, WorkerError> {
        Ok(common::floats(candidates, name)?
            .into_iter()
            .zip(&factors)
            .map(|(v, f)| to_exchange_price(v, *f))
            .collect())
    };
    let close = level("close")?;
    let trigger = level("trigger")?;
    let stop_ref = level("stop_ref")?;
    let pivot = level("pivot_high")?;
    let adj = common::stored(candidates, "adj_factor", "adj_factor")?;
    let measure = |name: &str| common::stored(candidates, name, name);
    let adr = measure("adr_pct")?;
    let prior = measure("prior_move_pct")?;
    let depth = measure("base_depth_pct")?;
    let tight = measure("tightness_adr")?;
    let dryup = measure("dryup_ratio")?;
    let fast = measure("dist_ma_fast_pct")?;
    let slow = measure("dist_ma_slow_pct")?;
    let rvol = measure("rvol")?;
    let gap = measure("gap_pct")?;
    let turnover = measure("turnover_avg")?;
    let base_bars = measure("base_bars")?;
    let up_streak = measure("up_streak")?;
    let mut rows = Vec::with_capacity(n);
    for i in 0..n {
        let id = ids[i].ok_or_else(|| value_err("a candidate without an instrument"))?;
        let listed = young.get(&id).copied().unwrap_or(false);
        let sector = sectors.get(&id).cloned();
        let bonus_young = if listed { YOUNG_LISTING_BONUS } else { 0.0 };
        let bonus_hot = if sector.as_ref().is_some_and(|s| hot.contains(s)) {
            HOT_SECTOR_BONUS
        } else {
            0.0
        };
        let raw_score = scores[i].map(|s| s + bonus_young + bonus_hot);
        let score = pl::min_horizontal_total([raw_score, Some(MAX_SCORE)]);
        // `int(...)` of a stored integer column, `None` passing through.
        let as_int = |v: Num| -> Result<Option<i64>, WorkerError> { v.int() };
        rows.push(SetupRow {
            instrument_id: id,
            setup: setups[i].clone().unwrap_or_default(),
            status: statuses[i].clone().unwrap_or_default(),
            score: store_float("score", score).decimal()?,
            close: store_float("close", close[i]).decimal()?,
            trigger: store_float("trigger", trigger[i]).decimal()?,
            stop_ref: store_float("stop_ref", stop_ref[i]).decimal()?,
            pivot_high: store_float("pivot_high", pivot[i]).decimal()?,
            adj_factor: adj[i].decimal()?,
            adr_pct: adr[i].decimal()?,
            prior_move_pct: prior[i].decimal()?,
            base_depth_pct: depth[i].decimal()?,
            tightness_adr: tight[i].decimal()?,
            dryup_ratio: dryup[i].decimal()?,
            dist_ma_fast_pct: fast[i].decimal()?,
            dist_ma_slow_pct: slow[i].decimal()?,
            rvol: rvol[i].decimal()?,
            gap_pct: gap[i].decimal()?,
            turnover_avg: as_int(turnover[i])?,
            base_bars: as_int(base_bars[i])?,
            up_streak: as_int(up_streak[i])?,
            locked_upper_circuit: locked[i].unwrap_or(false),
            sector_slug: sector,
            listed_within_2y: listed,
        });
    }
    Ok(rows)
}

fn small(v: Option<i64>) -> Result<Option<i16>, WorkerError> {
    v.map(|x| {
        i16::try_from(x).map_err(|_| {
            WorkerError::python("DataError", format!("value {x} out of range for smallint"))
        })
    })
    .transpose()
}

/// `_upsert_setups`: the rows upserted on `(user_id, date, instrument_id, setup)`, then the
/// day's provisional rows this run did not write deleted.
async fn upsert_setups(
    conn: &mut PgConnection,
    rows: &[SetupRow],
    user_id: i64,
    pipeline_run_id: Option<i64>,
    trade_date: NaiveDate,
    provisional: bool,
) -> Result<i64, WorkerError> {
    for r in rows {
        sqlx::query(
            "INSERT INTO sw_setup_daily (user_id, date, instrument_id, setup, status, score, \
             close, trigger, stop_ref, pivot_high, adj_factor, adr_pct, prior_move_pct, \
             base_depth_pct, tightness_adr, dryup_ratio, dist_ma_fast_pct, dist_ma_slow_pct, \
             rvol, gap_pct, turnover_avg, base_bars, up_streak, locked_upper_circuit, \
             sector_slug, listed_within_2y, pipeline_run_id, provisional) VALUES ($1, $2, $3, \
             $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, \
             $21, $22, $23, $24, $25, $26, $27, $28) \
             ON CONFLICT (user_id, date, instrument_id, setup) DO UPDATE SET \
             status = excluded.status, score = excluded.score, close = excluded.close, \
             trigger = excluded.trigger, stop_ref = excluded.stop_ref, \
             pivot_high = excluded.pivot_high, adj_factor = excluded.adj_factor, \
             adr_pct = excluded.adr_pct, prior_move_pct = excluded.prior_move_pct, \
             base_depth_pct = excluded.base_depth_pct, tightness_adr = excluded.tightness_adr, \
             dryup_ratio = excluded.dryup_ratio, dist_ma_fast_pct = excluded.dist_ma_fast_pct, \
             dist_ma_slow_pct = excluded.dist_ma_slow_pct, rvol = excluded.rvol, \
             gap_pct = excluded.gap_pct, turnover_avg = excluded.turnover_avg, \
             base_bars = excluded.base_bars, up_streak = excluded.up_streak, \
             locked_upper_circuit = excluded.locked_upper_circuit, \
             sector_slug = excluded.sector_slug, listed_within_2y = excluded.listed_within_2y, \
             pipeline_run_id = excluded.pipeline_run_id, provisional = excluded.provisional",
        )
        .bind(user_id)
        .bind(trade_date)
        .bind(r.instrument_id)
        .bind(&r.setup)
        .bind(&r.status)
        .bind(r.score)
        .bind(r.close)
        .bind(r.trigger)
        .bind(r.stop_ref)
        .bind(r.pivot_high)
        .bind(r.adj_factor)
        .bind(r.adr_pct)
        .bind(r.prior_move_pct)
        .bind(r.base_depth_pct)
        .bind(r.tightness_adr)
        .bind(r.dryup_ratio)
        .bind(r.dist_ma_fast_pct)
        .bind(r.dist_ma_slow_pct)
        .bind(r.rvol)
        .bind(r.gap_pct)
        .bind(r.turnover_avg)
        .bind(small(r.base_bars)?)
        .bind(small(r.up_streak)?)
        .bind(r.locked_upper_circuit)
        .bind(&r.sector_slug)
        .bind(r.listed_within_2y)
        .bind(pipeline_run_id)
        .bind(provisional)
        .execute(&mut *conn)
        .await?;
    }
    drop_stale_provisional(conn, rows, user_id, trade_date).await?;
    Ok(len_i64(rows.len()))
}

/// `_drop_stale_provisional`: `trade_date`'s provisional rows whose key this run did not write.
async fn drop_stale_provisional(
    conn: &mut PgConnection,
    rows: &[SetupRow],
    user_id: i64,
    trade_date: NaiveDate,
) -> Result<(), WorkerError> {
    if rows.is_empty() {
        sqlx::query(
            "DELETE FROM sw_setup_daily WHERE user_id = $1 AND date = $2 AND provisional IS true",
        )
        .bind(user_id)
        .bind(trade_date)
        .execute(&mut *conn)
        .await?;
        return Ok(());
    }
    let ids: Vec<i64> = rows.iter().map(|r| r.instrument_id).collect();
    let setups: Vec<String> = rows.iter().map(|r| r.setup.clone()).collect();
    sqlx::query(
        "DELETE FROM sw_setup_daily WHERE user_id = $1 AND date = $2 AND provisional IS true \
         AND (instrument_id, setup) NOT IN (SELECT * FROM unnest($3::bigint[], $4::varchar[]))",
    )
    .bind(user_id)
    .bind(trade_date)
    .bind(&ids)
    .bind(&setups)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// `_drop_provisional_day`: a run with no bar for the day supersedes a scan's provisional rows.
async fn drop_provisional_day(
    conn: &mut PgConnection,
    user_id: i64,
    trade_date: NaiveDate,
) -> Result<(), WorkerError> {
    drop_stale_provisional(conn, &[], user_id, trade_date).await?;
    sqlx::query(
        "DELETE FROM sw_market_daily WHERE user_id = $1 AND date = $2 AND provisional IS true",
    )
    .bind(user_id)
    .bind(trade_date)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// `_index_levels`: the newest `count` non-null levels on or before `on`, oldest first.
async fn index_levels(
    conn: &mut PgConnection,
    on: NaiveDate,
    slug: &str,
    count: i64,
) -> Result<Vec<f64>, WorkerError> {
    let rows: Vec<(Option<f64>,)> = sqlx::query_as(
        "SELECT s.level::float8 FROM index_snapshot_daily s JOIN index_def d \
         ON d.id = s.index_id WHERE d.slug = $1 AND s.date <= $2 AND s.level IS NOT NULL \
         ORDER BY s.date DESC LIMIT $3",
    )
    .bind(slug)
    .bind(on)
    .bind(count)
    .fetch_all(&mut *conn)
    .await?;
    let mut levels: Vec<f64> = rows.into_iter().filter_map(|r| r.0).collect();
    levels.reverse();
    Ok(levels)
}

/// `load_index_reading`: the benchmark's reading, else the fallback's, else `(None, None)`.
async fn load_index_reading(
    conn: &mut PgConnection,
    on: NaiveDate,
    slug: &str,
    config: &MarketConfig,
    live_level: Option<f64>,
) -> Result<(Option<IndexReading>, Option<String>), WorkerError> {
    for candidate in [slug, FALLBACK_INDEX] {
        let mut levels = index_levels(conn, on, candidate, INDEX_HISTORY_SESSIONS).await?;
        // SW30 (lane swing_live): today's level is appended, never substituted, and for the
        // first candidate only — `[*levels, live][-INDEX_HISTORY_SESSIONS:]`.
        if let Some(live) = live_level
            && candidate == slug
        {
            levels.push(live);
            let keep = usize::try_from(INDEX_HISTORY_SESSIONS).unwrap_or(usize::MAX);
            if levels.len() > keep {
                levels.drain(..levels.len() - keep);
            }
        }
        if let Some(reading) = swing_market::index_reading(&levels, config)? {
            return Ok((Some(reading), Some(candidate.to_owned())));
        }
    }
    Ok((None, None))
}

/// `load_closed_trades(on=)`: the R-multiples of the last real closes on or before `on`,
/// oldest first.
async fn load_closed_trades(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
) -> Result<Vec<Decimal>, WorkerError> {
    let rows = sqlx::query(
        "SELECT r_multiple FROM sw_position WHERE user_id = $1 AND state = 'CLOSED' \
         AND simulated IS false AND r_multiple IS NOT NULL AND closed_on IS NOT NULL \
         AND closed_on <= $2 ORDER BY closed_on DESC, id DESC LIMIT $3",
    )
    .bind(user_id)
    .bind(on)
    .bind(CLOSED_TRADE_WINDOW)
    .fetch_all(&mut *conn)
    .await?;
    let mut values = Vec::with_capacity(rows.len());
    for row in &rows {
        if let Some(v) = opt_decimal(row, "r_multiple")? {
            values.push(v);
        }
    }
    values.reverse();
    Ok(values)
}

/// `SleeveDrawdown`, decimals kept as Python holds them (for `str()`).
pub(crate) struct SleeveDrawdown {
    pub(crate) nav: WideDecimal,
    pub(crate) peak: WideDecimal,
    pub(crate) pct: WideDecimal,
    pub(crate) was_locked: bool,
}

/// `sleeve_nav(simulated=False)`: capital + realised + the open book marked at `on`'s close
/// (and a partial's realised half), quantized to 0.01 half-even.
async fn sleeve_nav(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    capital: Decimal,
) -> Result<WideDecimal, WorkerError> {
    let realised = sqlx::query(
        "SELECT sum(pnl_inr) AS total FROM sw_position WHERE user_id = $1 AND state = 'CLOSED' \
         AND simulated IS false AND pnl_inr IS NOT NULL AND closed_on IS NOT NULL \
         AND closed_on <= $2",
    )
    .bind(user_id)
    .bind(on)
    .fetch_one(&mut *conn)
    .await?;
    let realised = opt_decimal(&realised, "total")?;
    let mut nav =
        WideDecimal::from(capital).add(&realised.map_or_else(WideDecimal::zero, WideDecimal::from));
    let open = sqlx::query(
        "SELECT id, instrument_id, entry_avg, quantity_open FROM sw_position WHERE user_id = $1 \
         AND state IN ('OPEN', 'PARTIAL') AND quantity_open > 0 AND simulated IS false \
         AND entry_date <= $2",
    )
    .bind(user_id)
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    for position in &open {
        let id: i64 = position.try_get("id")?;
        let instrument_id: i64 = position.try_get("instrument_id")?;
        let entry = WideDecimal::from(row_decimal(position, "entry_avg")?);
        let quantity_open: i32 = position.try_get("quantity_open")?;
        let mark_row = sqlx::query(
            "SELECT close FROM ohlcv_daily WHERE instrument_id = $1 AND date <= $2 \
             ORDER BY date DESC LIMIT 1",
        )
        .bind(instrument_id)
        .bind(on)
        .fetch_optional(&mut *conn)
        .await?;
        let mark = match mark_row {
            Some(r) => WideDecimal::from(row_decimal(&r, "close")?),
            None => entry.clone(),
        };
        nav = nav.add(
            &mark
                .sub(&entry)
                .mul(&WideDecimal::from(i64::from(quantity_open))),
        );
        let sold = sqlx::query(
            "SELECT price, quantity FROM sw_fill WHERE position_id = $1 AND side = 'SELL'",
        )
        .bind(id)
        .fetch_all(&mut *conn)
        .await?;
        let mut fills = WideDecimal::zero();
        for fill in &sold {
            let price = WideDecimal::from(row_decimal(fill, "price")?);
            let quantity: i32 = fill.try_get("quantity")?;
            fills = fills.add(
                &price
                    .sub(&entry)
                    .mul(&WideDecimal::from(i64::from(quantity))),
            );
        }
        nav = nav.add(&fills);
    }
    nav.quantize(-2, Rounding::HalfEven).map_err(dec_err)
}

/// `sleeve_drawdown`: tonight's NAV against the stored peak, read without writing anything.
pub(crate) async fn sleeve_drawdown(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    row: Option<&ConfigRow>,
) -> Result<SleeveDrawdown, WorkerError> {
    let Some(row) = row else {
        return Ok(SleeveDrawdown {
            nav: WideDecimal::zero(),
            peak: WideDecimal::zero(),
            pct: WideDecimal::zero(),
            was_locked: false,
        });
    };
    let nav = sleeve_nav(conn, user_id, on, row.sleeve_capital_inr).await?;
    let peak = match row.sleeve_peak_inr {
        None => nav.clone(),
        // `max(Decimal(stored), nav)`: the stored peak on a tie.
        Some(stored) => {
            let stored = WideDecimal::from(stored);
            if nav > stored { nav.clone() } else { stored }
        }
    };
    let pct = swing_market::drawdown_pct(&peak, &nav)?;
    let pct = baskfy_core::py::wide_from_float_str(pct)
        .map_err(dec_err)?
        .quantize(-2, Rounding::HalfEven)
        .map_err(dec_err)?;
    Ok(SleeveDrawdown {
        nav,
        peak,
        pct,
        was_locked: row.drawdown_locked,
    })
}

/// The settled market row's columns.
struct Settled {
    tier: ExposureTier,
    drawdown_pct: Option<Decimal>,
    detail: Vec<(String, OrderedJson)>,
}

/// `settled_market_row`: the row for `on` if the evening has settled it.
async fn settled_market_row(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
) -> Result<Option<Settled>, WorkerError> {
    let row = sqlx::query(
        "SELECT exposure_level, max_open_positions, max_exposure_pct::float8 AS \
         max_exposure_pct, new_entries_allowed, drawdown_locked, drawdown_pct, detail \
         FROM sw_market_daily WHERE user_id = $1 AND date = $2",
    )
    .bind(user_id)
    .bind(on)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    let Some(OrderedJson::Object(detail)) = baskfy_db::row::opt_jsonb(&row, "detail")? else {
        return Ok(None);
    };
    let settled = detail.iter().any(|(k, v)| {
        k == "ladder"
            && matches!(v, OrderedJson::Object(_))
            && matches!(v.get("settled_by"), Some(OrderedJson::Str(s)) if s == SETTLED_BY_EVENING)
    });
    if !settled {
        return Ok(None);
    }
    let level: i16 = row.try_get("exposure_level")?;
    let positions: i16 = row.try_get("max_open_positions")?;
    Ok(Some(Settled {
        tier: ExposureTier {
            level: i64::from(level),
            max_open_positions: i64::from(positions),
            max_exposure_pct: row.try_get("max_exposure_pct")?,
            new_entries_allowed: row.try_get("new_entries_allowed")?,
            drawdown_locked: row.try_get("drawdown_locked")?,
        },
        drawdown_pct: opt_decimal(&row, "drawdown_pct")?,
        detail,
    }))
}

/// `load_year_highs`: `factor_daily.high_1y` on `on`.
async fn load_year_highs(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<BTreeMap<i64, f64>, WorkerError> {
    let rows: Vec<(i64, f64)> = sqlx::query_as(
        "SELECT instrument_id, high_1y::float8 FROM factor_daily WHERE date = $1 \
         AND high_1y IS NOT NULL",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().collect())
}

/// A JSON float.
fn jf(x: f64) -> OrderedJson {
    OrderedJson::Float(x)
}

/// `str(Decimal)`.
fn dstr(d: &WideDecimal) -> OrderedJson {
    OrderedJson::Str(d.to_sci_string())
}

/// `write_market_row` (nightly arguments): breadth, the gate and tomorrow's tier.
#[allow(clippy::too_many_arguments)]
async fn write_market_row(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
    user_id: i64,
    config: &SwingConfig,
    cfg_row: Option<&ConfigRow>,
    liquid: &Liquid,
    parabolic_count: i64,
    sectors: &[(String, f64, i64)],
    index_slug: &str,
    funnel: OrderedJson,
    extras: &ScanExtras,
) -> Result<(), WorkerError> {
    let lookup = extras.reference_date.unwrap_or(trade_date);
    let highs = load_year_highs(conn, lookup).await?;
    let breadth = if liquid.ids.is_empty() {
        BreadthSnapshot::empty()
    } else {
        let rows: Vec<BreadthRow> = (0..liquid.ids.len())
            .map(|i| BreadthRow {
                ret_20: liquid.ret_20[i],
                close: liquid.close[i],
                ma_slow: liquid.ma_slow[i],
                high_1y: highs.get(&liquid.ids[i]).copied().or(liquid.high[i]),
            })
            .collect();
        swing_market::breadth_snapshot(&rows, &config.market)
    };
    let (reading, used_slug) = load_index_reading(
        conn,
        lookup,
        index_slug,
        &config.market,
        extras.live_index_level,
    )
    .await?;
    let gate = swing_market::market_gate(&breadth, reading.as_ref(), &config.market);

    let closed = load_closed_trades(conn, user_id, trade_date).await?;
    let current = cfg_row.map_or(0, |r| r.exposure_level);
    let mut drawdown = sleeve_drawdown(conn, user_id, trade_date, cfg_row).await?;
    let pct_f = drawdown
        .pct
        .to_string()
        .parse::<f64>()
        .map_err(|e| value_err(e.to_string()))?;
    let mut tier = exposure_tier(
        current,
        &closed,
        gate,
        &config.market,
        pct_f,
        drawdown.was_locked,
    )
    .map_err(core_err)?;

    let mut detail: Vec<(String, OrderedJson)> = vec![
        (
            "closed_r_multiples".to_owned(),
            pyjson::strings(closed.iter().map(ToString::to_string)),
        ),
        ("closed_trades_read".to_owned(), pyjson::s("real")),
        (
            "drawdown".to_owned(),
            pyjson::object([
                ("nav", dstr(&drawdown.nav)),
                ("peak", dstr(&drawdown.peak)),
                ("pct", dstr(&drawdown.pct)),
                ("was_locked", OrderedJson::Bool(drawdown.was_locked)),
                ("locked", OrderedJson::Bool(tier.drawdown_locked)),
            ]),
        ),
        (
            "sectors".to_owned(),
            OrderedJson::Array(
                sectors
                    .iter()
                    .map(|(slug, pct, members)| {
                        pyjson::object([
                            ("slug", pyjson::s(slug.clone())),
                            ("pct_above_ma_slow", jf(*pct)),
                            ("members", pyjson::int(*members)),
                        ])
                    })
                    .collect(),
            ),
        ),
    ];
    pyjson::update(&mut detail, "funnel", funnel);
    if let Some(scan) = &extras.scan {
        pyjson::update(&mut detail, "scan", scan.clone());
    }
    if let Some(settled) = settled_market_row(conn, user_id, trade_date).await? {
        tier = settled.tier;
        for key in [
            "ladder",
            "closed_r_multiples",
            "closed_trades_read",
            "drawdown",
        ] {
            if let Some((_, v)) = settled.detail.iter().find(|(k, _)| k == key) {
                pyjson::update(&mut detail, key, v.clone());
            }
        }
        // `Decimal(str(settled.drawdown_pct or 0))`.
        drawdown.pct = match settled.drawdown_pct {
            Some(d) if !d.is_zero() => WideDecimal::from(d),
            _ => WideDecimal::zero(),
        };
    }
    let detail_text = OrderedJson::Object(detail)
        .to_python_json()
        .map_err(|e| value_err(e.to_string()))?;
    let round2 = |x: f64| Num::F(py_round(x, 2)).decimal();
    let (close, ma_fast, ma_slow) = match &reading {
        Some(r) => (round2(r.close)?, round2(r.ma_fast)?, round2(r.ma_slow)?),
        None => (None, None, None),
    };
    let pct_dd = drawdown.pct.try_to_decimal().map_err(dec_err)?;
    sqlx::query(
        "INSERT INTO sw_market_daily (user_id, date, constituent_count, pct_up_strong_1m, \
         pct_new_52w_high, pct_above_ma_slow, index_slug, index_close, index_ma_fast, \
         index_ma_slow, gate, exposure_level, max_open_positions, max_exposure_pct, \
         new_entries_allowed, drawdown_pct, drawdown_locked, parabolic_count, detail, \
         provisional) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, \
         $15, $16, $17, $18, CAST($19 AS JSONB), $20) \
         ON CONFLICT (user_id, date) DO UPDATE SET provisional = excluded.provisional, \
         constituent_count = excluded.constituent_count, \
         pct_up_strong_1m = excluded.pct_up_strong_1m, \
         pct_new_52w_high = excluded.pct_new_52w_high, \
         pct_above_ma_slow = excluded.pct_above_ma_slow, index_slug = excluded.index_slug, \
         index_close = excluded.index_close, index_ma_fast = excluded.index_ma_fast, \
         index_ma_slow = excluded.index_ma_slow, gate = excluded.gate, \
         exposure_level = excluded.exposure_level, \
         max_open_positions = excluded.max_open_positions, \
         max_exposure_pct = excluded.max_exposure_pct, \
         new_entries_allowed = excluded.new_entries_allowed, \
         drawdown_pct = excluded.drawdown_pct, drawdown_locked = excluded.drawdown_locked, \
         parabolic_count = excluded.parabolic_count, detail = excluded.detail",
    )
    .bind(user_id)
    .bind(trade_date)
    .bind(i32::try_from(breadth.constituent_count).map_err(|e| value_err(e.to_string()))?)
    .bind(Num::F(breadth.pct_up_strong_1m).decimal()?)
    .bind(Num::F(breadth.pct_new_52w_high).decimal()?)
    .bind(Num::F(breadth.pct_above_ma_slow).decimal()?)
    .bind(used_slug)
    .bind(close)
    .bind(ma_fast)
    .bind(ma_slow)
    .bind(swing_market::gate_value(gate))
    .bind(small(Some(tier.level))?)
    .bind(small(Some(tier.max_open_positions))?)
    .bind(Num::F(tier.max_exposure_pct).decimal()?)
    .bind(tier.new_entries_allowed)
    .bind(pct_dd)
    .bind(tier.drawdown_locked)
    .bind(i32::try_from(parabolic_count).map_err(|e| value_err(e.to_string()))?)
    .bind(detail_text)
    .bind(extras.provisional)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// `run_detect_swing` (the nightly's call): returns the candidate count.
///
/// # Errors
/// Python's, as the task raises them; the orchestrator's wrapper records any as `skipped`.
pub(crate) async fn run_detect_swing(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    trade_date: NaiveDate,
    user_id: i64,
    index_slug: &str,
    pipeline_run_id: i64,
) -> Result<i64, WorkerError> {
    run_detect_swing_for(
        conn,
        outcome,
        trade_date,
        user_id,
        index_slug,
        Some(pipeline_run_id),
    )
    .await
}

/// `run_detect_swing(..., pipeline_run_id=)` with Python's `int | None` run id: the nightly
/// passes its run, `baskfy.swing.detect` and `baskfy.swing.weekend` pass `None` (wave 6, lane
/// `swing_eod`; the same detector, widened rather than forked).
///
/// # Errors
/// Python's, as the task raises them.
pub(crate) async fn run_detect_swing_for(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    trade_date: NaiveDate,
    user_id: i64,
    index_slug: &str,
    pipeline_run_id: Option<i64>,
) -> Result<i64, WorkerError> {
    run_detect_swing_scan(
        conn,
        outcome,
        trade_date,
        user_id,
        index_slug,
        pipeline_run_id,
        &ScanExtras::default(),
    )
    .await
}

/// SW15's inputs to `run_detect_swing` beyond the nightly's (wave 6, lane swing_live):
/// `extra_bars` (one provisional bar per name, in `load_swing_bars`' shape, appended to the
/// published ones), `provisional` (stamped on every row), `reference_date` (the last *published*
/// session, where the point-in-time reads look when `trade_date` has no published row), `scan`
/// (`detail.scan`) and the benchmark's live level. The default is the nightly's call.
#[derive(Debug, Default, Clone)]
pub struct ScanExtras {
    /// `extra_bars`.
    pub extra_bars: Option<DataFrame>,
    /// `provisional`.
    pub provisional: bool,
    /// `reference_date`.
    pub reference_date: Option<NaiveDate>,
    /// `scan`.
    pub scan: Option<OrderedJson>,
    /// `live_index_level`.
    pub live_index_level: Option<f64>,
}

/// `BAR_SCHEMA`'s columns, in order.
pub(crate) const BAR_COLUMNS: [&str; 11] = [
    "instrument_id",
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "upper_circuit",
    "adj_factor",
];

/// `append_bars`: the published bars plus a scan's provisional ones, sorted by
/// `(instrument_id, date)` as the indicators expect.
fn append_bars(bars: DataFrame, extra: Option<&DataFrame>) -> Result<DataFrame, WorkerError> {
    let Some(extra) = extra.filter(|e| e.height() > 0) else {
        return Ok(bars);
    };
    let mut all = bars.select(BAR_COLUMNS).map_err(|e| pl_err(&e))?;
    all.vstack_mut(&extra.select(BAR_COLUMNS).map_err(|e| pl_err(&e))?)
        .map_err(|e| pl_err(&e))?;
    all.sort(
        ["instrument_id", "date"],
        polars::prelude::SortMultipleOptions::default(),
    )
    .map_err(|e| pl_err(&e))
}

/// `run_detect_swing(..., extra_bars=, provisional=, reference_date=, scan=,
/// live_index_level=)` — "Scan now"'s call (SW15; wave 6, lane swing_live), and the nightly's
/// with every extra at its default.
///
/// # Errors
/// Python's, as the task raises them.
pub(crate) async fn run_detect_swing_scan(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    trade_date: NaiveDate,
    user_id: i64,
    index_slug: &str,
    pipeline_run_id: Option<i64>,
    extras: &ScanExtras,
) -> Result<i64, WorkerError> {
    let cfg_row = config_row(conn, user_id).await?;
    let config = swing_config(cfg_row.as_ref());
    let start = lookback_start(conn, trade_date, LOOKBACK_SESSIONS).await?;
    let bars = append_bars(
        load_swing_bars(conn, start, trade_date).await?,
        extras.extra_bars.as_ref(),
    )?;
    let lookup = extras.reference_date.unwrap_or(trade_date);
    let day = trade_date.format("%Y-%m-%d").to_string();
    if bars.height() == 0 {
        outcome.status = StepStatus::Skipped;
        outcome.note("date", pyjson::s(day.clone()));
        outcome.note(
            "skipped_reason",
            pyjson::s("no published bars in the lookback window"),
        );
        outcome.note(
            "window",
            pyjson::strings([start.format("%Y-%m-%d").to_string(), day]),
        );
        drop_provisional_day(conn, user_id, trade_date).await?;
        return Ok(0);
    }
    let indicated = with_swing_indicators(&bars, &config).map_err(core_err)?;
    let today_day = day_number(trade_date);
    let dates = common::dates(&indicated, "date")?;
    let today: Vec<usize> = (0..indicated.height())
        .filter(|&i| dates[i] == Some(today_day))
        .collect();
    if today.is_empty() {
        outcome.status = StepStatus::Skipped;
        outcome.note("date", pyjson::s(day));
        outcome.note(
            "skipped_reason",
            pyjson::s("no instrument has a bar on this date"),
        );
        outcome.note("bars", pyjson::int(len_i64(bars.height())));
        drop_provisional_day(conn, user_id, trade_date).await?;
        return Ok(0);
    }

    let ids = common::ints(&indicated, "instrument_id")?;
    let adr = common::floats(&indicated, "adr_pct")?;
    let turnover = common::floats(&indicated, "turnover_avg")?;
    let close = common::floats(&indicated, "close")?;
    let ma_slow = common::floats(&indicated, "ma_slow")?;
    let ret_20 = common::floats(&indicated, "ret_20")?;
    let high = common::floats(&indicated, "high")?;
    let mut liquid = Liquid {
        ids: Vec::new(),
        close: Vec::new(),
        ma_slow: Vec::new(),
        ret_20: Vec::new(),
        high: Vec::new(),
    };
    for &i in &today {
        if swing_market::is_liquid(adr[i], turnover[i], close[i], &config.liquidity) {
            liquid
                .ids
                .push(ids[i].ok_or_else(|| value_err("a bar without an instrument"))?);
            liquid.close.push(close[i]);
            liquid.ma_slow.push(ma_slow[i]);
            liquid.ret_20.push(ret_20[i]);
            liquid.high.push(high[i]);
        }
    }
    let sectors = load_sector_membership(conn, lookup).await?;
    let strip = sector_breadth(&liquid, &sectors);
    let hot: BTreeSet<String> = strip
        .iter()
        .take(HOT_SECTOR_COUNT)
        .map(|(s, _, _)| s.clone())
        .collect();

    let candidates = detect_setups(&indicated, trade_date, &config).map_err(core_err)?;
    let listing = load_listing_dates(conn).await?;
    let young: BTreeMap<i64, bool> = listing
        .into_iter()
        .map(|(id, listed)| (id, (trade_date - listed).num_days() <= YOUNG_LISTING_DAYS))
        .collect();
    let rows = if candidates.height() == 0 {
        Vec::new()
    } else {
        setup_rows(&candidates, &young, &sectors, &hot)?
    };
    let written = upsert_setups(
        conn,
        &rows,
        user_id,
        pipeline_run_id,
        trade_date,
        extras.provisional,
    )
    .await?;

    let mut per_setup: Vec<(String, OrderedJson)> = Vec::new();
    let mut parabolic = 0;
    for setup in SETUPS {
        let count = len_i64(rows.iter().filter(|r| r.setup == setup.as_str()).count());
        if setup == Setup::ParabolicShort {
            parabolic = count;
        }
        per_setup.push((setup.as_str().to_owned(), pyjson::int(count)));
    }
    let bar_count = len_i64(bars.height());
    let funnel = pyjson::object([
        ("instruments", pyjson::int(len_i64(today.len()))),
        ("bars", pyjson::int(bar_count)),
        ("with_a_bar_today", pyjson::int(len_i64(today.len()))),
        ("liquid", pyjson::int(len_i64(liquid.ids.len()))),
        ("candidates", OrderedJson::Object(per_setup)),
    ]);
    write_market_row(
        conn,
        trade_date,
        user_id,
        &config,
        cfg_row.as_ref(),
        &liquid,
        parabolic,
        &strip,
        index_slug,
        funnel.clone(),
        extras,
    )
    .await?;
    outcome.rows_in = bar_count;
    outcome.rows_out = written;
    outcome.note("date", pyjson::s(day));
    outcome.note("funnel", funnel);
    Ok(written)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::float_cmp)]
    use super::*;

    fn liquid(rows: &[(i64, f64, Option<f64>)]) -> Liquid {
        Liquid {
            ids: rows.iter().map(|r| r.0).collect(),
            close: rows.iter().map(|r| Some(r.1)).collect(),
            ma_slow: rows.iter().map(|r| r.2).collect(),
            ret_20: vec![None; rows.len()],
            high: vec![None; rows.len()],
        }
    }

    #[test]
    fn a_level_is_divided_by_its_own_adjustment_factor_and_a_zero_one_is_one() {
        assert_eq!(to_exchange_price(Some(55.0), Some(0.5)), Some(110.0));
        assert_eq!(to_exchange_price(Some(50.0), Some(0.5)), Some(100.0));
        assert_eq!(to_exchange_price(Some(55.0), Some(0.0)), Some(55.0));
        assert_eq!(to_exchange_price(Some(55.0), None), Some(55.0));
        assert_eq!(to_exchange_price(None, Some(2.0)), None);
    }

    #[test]
    fn a_sector_with_too_few_names_is_not_a_sector_and_strongest_comes_first() {
        let two = liquid(&[(1, 100.0, Some(90.0)), (2, 100.0, Some(110.0))]);
        let sectors = BTreeMap::from([(1, "nifty-it".to_owned()), (2, "nifty-it".to_owned())]);
        assert!(sector_breadth(&two, &sectors).is_empty());
        let rows: Vec<(i64, f64, Option<f64>)> = (1..=10)
            .map(|i| (i, 100.0, Some(if i <= 6 { 90.0 } else { 110.0 })))
            .collect();
        let sectors: BTreeMap<i64, String> = (1..=10)
            .map(|i| {
                (
                    i,
                    if i <= 5 { "nifty-it" } else { "nifty-metal" }.to_owned(),
                )
            })
            .collect();
        assert_eq!(
            sector_breadth(&liquid(&rows), &sectors),
            vec![
                ("nifty-it".to_owned(), 100.0, 5),
                ("nifty-metal".to_owned(), 20.0, 5)
            ]
        );
        assert!(
            sector_breadth(&liquid(&[]), &BTreeMap::from([(1, "nifty-it".to_owned())])).is_empty()
        );
    }

    #[test]
    fn sector_breadth_needs_five_measured_members_and_sorts_strongest_first() {
        let mut sectors = BTreeMap::new();
        for id in 1..=5 {
            sectors.insert(id, "nifty-it".to_owned());
        }
        for id in 6..=11 {
            sectors.insert(id, "nifty-bank".to_owned());
        }
        sectors.insert(12, "nifty-auto".to_owned());
        let mut rows: Vec<(i64, f64, Option<f64>)> = Vec::new();
        // IT: 4 of 5 above.
        for id in 1..=5 {
            rows.push((id, if id == 5 { 9.0 } else { 11.0 }, Some(10.0)));
        }
        // Bank: 6 members, one with no MA (not counted), 5 measured, all above.
        for id in 6..=11 {
            rows.push((id, 11.0, if id == 11 { None } else { Some(10.0) }));
        }
        rows.push((12, 11.0, Some(10.0)));
        let strip = sector_breadth(&liquid(&rows), &sectors);
        assert_eq!(
            strip,
            vec![
                ("nifty-bank".to_owned(), 100.0, 5),
                ("nifty-it".to_owned(), 80.0, 5)
            ]
        );
        assert!(sector_breadth(&liquid(&rows), &BTreeMap::new()).is_empty());
    }

    #[test]
    fn the_score_takes_both_bonuses_and_is_capped() {
        let candidates = DataFrame::new(
            2,
            vec![
                Series::new("instrument_id".into(), vec![1_i64, 2]).into_column(),
                Series::new("setup".into(), vec!["FLAG", "EP"]).into_column(),
                Series::new("status".into(), vec!["SETTING_UP", "GAP_DAY"]).into_column(),
                Series::new("score".into(), vec![Some(93.0), Some(50.0)]).into_column(),
                Series::new("close".into(), vec![Some(200.0), Some(100.0)]).into_column(),
                Series::new("adj_factor".into(), vec![Some(2.0), None]).into_column(),
                Series::new("trigger".into(), vec![Some(210.0), None]).into_column(),
                Series::new("stop_ref".into(), vec![Some(190.0), None]).into_column(),
                Series::new("pivot_high".into(), vec![Some(209.0), None]).into_column(),
                Series::new("locked_upper_circuit".into(), vec![false, false]).into_column(),
            ]
            .into_iter()
            .chain(
                [
                    "adr_pct",
                    "prior_move_pct",
                    "base_depth_pct",
                    "tightness_adr",
                    "dryup_ratio",
                    "dist_ma_fast_pct",
                    "dist_ma_slow_pct",
                    "rvol",
                    "gap_pct",
                    "turnover_avg",
                ]
                .iter()
                .map(|n| Series::new((*n).into(), vec![Some(1.234_5), None]).into_column()),
            )
            .chain(
                ["base_bars", "up_streak"]
                    .iter()
                    .map(|n| Series::new((*n).into(), vec![Some(3_i64), None]).into_column()),
            )
            .collect(),
        )
        .unwrap_or_default();
        let young = BTreeMap::from([(1, true), (2, false)]);
        let sectors = BTreeMap::from([(1, "nifty-it".to_owned())]);
        let hot = BTreeSet::from(["nifty-it".to_owned()]);
        let rows = setup_rows(&candidates, &young, &sectors, &hot).unwrap_or_default();
        assert_eq!(rows.len(), 2);
        let d = |s: &str| s.parse::<Decimal>().ok();
        assert_eq!(rows[0].score, d("100.0"));
        assert_eq!(rows[0].close, d("100.0"));
        assert_eq!(rows[0].trigger, d("105.0"));
        assert_eq!(rows[0].sector_slug.as_deref(), Some("nifty-it"));
        assert!(rows[0].listed_within_2y);
        assert_eq!(rows[0].turnover_avg, Some(1));
        assert_eq!(rows[0].adr_pct, d("1.23"));
        assert_eq!(rows[1].score, d("50.0"));
        assert_eq!(rows[1].close, d("100.0"));
        assert_eq!(rows[1].sector_slug, None);
        assert!(!rows[1].listed_within_2y);
    }
}
