//! C2's `desk_score_daily` write, folded into step 7. Ports `baskfy_worker.tasks.desk_score`
//! (the loaders and the writer) and the pure path it calls, `baskfy_core.desk_score_service.
//! score_day` → `momentum_scan.build` → `score.score` → `project`, none of which had a Rust home.
//!
//! The inputs are read exactly as Python reads them: each scanned NSE instrument's last
//! [`desk_bar_lookback`] bars on or before the as-of date (a date-range read, then a per-instrument
//! `LATERAL ... LIMIT` top-up for names with a hole), numbers as floats with NULL as 0.0
//! (`scan_source._fetch`); the trading days as every `ohlcv_daily` date on or before it; the
//! carried columns from the as-of `factor_daily` row. Rows are upserted on `(instrument_id,
//! date)` and any row of the date the scan no longer produces is deleted (house rule 7).

use std::collections::{BTreeMap, BTreeSet, HashMap};

use baskfy_api::basket_build::scan::{Carried, DeskBars, build_scan};
use baskfy_api::basket_build::score::score;
use baskfy_core::factors::{
    HIGH_1Y_BARS, MA_LENGTHS, MEDIAN_VOL_BARS, VOL_AVG_BARS, subtract_months,
};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row};

use super::core_err;
use super::factors::{NSE_EXCHANGE_ID, mask_value};
use super::numeric::quantize_half_up_repr;
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

/// `desk_score_service.DESK_SCORE_VERSION`.
pub(crate) const DESK_SCORE_VERSION: &str = "desk-score-2026.09.13";
/// `tasks.desk_score.DESK_BAR_MARGIN`.
pub(crate) const DESK_BAR_MARGIN: usize = 20;
/// `tasks.desk_score.STREAM_PARTITION`.
const STREAM_PARTITION: i64 = 50_000;
/// `ranking.ensure_nifty_fno_flag`'s rule: the F&O bit (`nifty-fno`, index 13).
const NIFTY_FNO_INDEX_ID: i64 = 13;

/// `desk_bar_lookback(as_of, trading_days)`: `max(N + 1, 252, ...) + DESK_BAR_MARGIN`, `N` the
/// trading days in `(as_of - 12 months, as_of]` counted by bisection.
///
/// # Errors
/// `subtract_months`' (a date out of range).
pub(crate) fn desk_bar_lookback(
    as_of: NaiveDate,
    trading_days: &[NaiveDate],
) -> Result<usize, WorkerError> {
    let mut ordered = trading_days.to_vec();
    ordered.sort();
    let anniversary = subtract_months(as_of, 12).map_err(core_err)?;
    let bisect_right = |x: NaiveDate| ordered.partition_point(|d| *d <= x);
    let window_12m = bisect_right(as_of).saturating_sub(bisect_right(anniversary));
    let mut longest = (window_12m + 1).max(HIGH_1Y_BARS).max(MEDIAN_VOL_BARS);
    for k in MA_LENGTHS {
        longest = longest.max(k);
    }
    for (_, k) in VOL_AVG_BARS {
        longest = longest.max(k);
    }
    Ok(longest + DESK_BAR_MARGIN)
}

/// `desk_bar_window_start(as_of, trading_days, lookback)`: the `lookback`-th latest trading day
/// on or before `as_of`; `as_of` itself with no calendar.
pub(crate) fn desk_bar_window_start(
    as_of: NaiveDate,
    trading_days: &[NaiveDate],
    lookback: usize,
) -> NaiveDate {
    let mut ordered: Vec<NaiveDate> = trading_days
        .iter()
        .copied()
        .filter(|d| *d <= as_of)
        .collect();
    ordered.sort();
    match ordered.len() {
        0 => as_of,
        n => ordered[n.saturating_sub(lookback)],
    }
}

/// `_desk_float`: a number as float, NULL as 0.0.
fn desk_float(text: Option<String>) -> Result<f64, WorkerError> {
    Ok(super::factors::parse_float(text.as_deref())?.unwrap_or(0.0))
}

/// Runs one bar query through a server-side cursor, `STREAM_PARTITION` rows at a time, into
/// `bars`. The query selects `symbol, instrument_id, date` then the seven numbers as text.
async fn stream_desk_bars(
    conn: &mut PgConnection,
    cursor: &str,
    declare: sqlx::query::Query<'_, sqlx::Postgres, sqlx::postgres::PgArguments>,
    bars: &mut DeskBars,
) -> Result<(), WorkerError> {
    declare.execute(&mut *conn).await?;
    loop {
        let rows = sqlx::query(&format!("FETCH FORWARD {STREAM_PARTITION} FROM {cursor}"))
            .fetch_all(&mut *conn)
            .await?;
        if rows.is_empty() {
            break;
        }
        for row in &rows {
            bars.symbol.push(row.try_get(0)?);
            bars.instrument_id.push(row.try_get(1)?);
            bars.date.push(row.try_get(2)?);
            for (k, column) in bars.numbers.iter_mut().enumerate() {
                column.push(desk_float(row.try_get(k + 3)?)?);
            }
        }
    }
    sqlx::query(&format!("CLOSE {cursor}"))
        .execute(&mut *conn)
        .await?;
    Ok(())
}

/// `load_desk_trading_days`: every `ohlcv_daily` date on or before `as_of`.
async fn load_desk_trading_days(
    conn: &mut PgConnection,
    as_of: NaiveDate,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT ohlcv_daily.date FROM ohlcv_daily WHERE ohlcv_daily.date <= $1 \
         GROUP BY ohlcv_daily.date ORDER BY ohlcv_daily.date",
    )
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|r| r.0).collect())
}

/// `load_desk_bars(session, as_of, trading_days)`.
async fn load_desk_bars(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    trading_days: &[NaiveDate],
) -> Result<DeskBars, WorkerError> {
    let lookback = desk_bar_lookback(as_of, trading_days)?;
    let window_start = desk_bar_window_start(as_of, trading_days, lookback);

    let scanned_rows: Vec<(i64,)> = sqlx::query_as(
        "SELECT factor_daily.instrument_id FROM factor_daily \
         JOIN instrument ON instrument.id = factor_daily.instrument_id \
         WHERE instrument.exchange_id = $1 AND factor_daily.date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut scanned: Vec<i64> = scanned_rows.into_iter().map(|r| r.0).collect();
    scanned.sort_unstable();
    let mut bars = DeskBars::default();
    if scanned.is_empty() {
        return Ok(bars);
    }

    let declare = sqlx::query(
        "DECLARE baskfy_desk_recent NO SCROLL CURSOR FOR \
         SELECT instrument.symbol, ohlcv_daily.instrument_id, ohlcv_daily.date, \
         ohlcv_daily.open::text, ohlcv_daily.high::text, ohlcv_daily.low::text, \
         ohlcv_daily.close::text, ohlcv_daily.volume::text, ohlcv_daily.close_raw::text, \
         ohlcv_daily.volume_raw::text \
         FROM ohlcv_daily JOIN instrument ON instrument.id = ohlcv_daily.instrument_id \
         WHERE instrument.exchange_id = $1 AND ohlcv_daily.date >= $2 AND ohlcv_daily.date <= $3 \
         AND ohlcv_daily.instrument_id = ANY($4) \
         ORDER BY instrument.symbol, ohlcv_daily.date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(window_start)
    .bind(as_of)
    .bind(scanned.clone());
    stream_desk_bars(conn, "baskfy_desk_recent", declare, &mut bars).await?;

    let mut have: HashMap<i64, usize> = HashMap::new();
    for id in &bars.instrument_id {
        *have.entry(*id).or_insert(0) += 1;
    }
    let short: Vec<(i64, i32)> = scanned
        .iter()
        .filter_map(|id| {
            let got = have.get(id).copied().unwrap_or(0);
            (got < lookback).then(|| (*id, i32::try_from(lookback - got).unwrap_or(i32::MAX)))
        })
        .collect();
    if short.is_empty() {
        return Ok(bars);
    }
    let need_ids: Vec<i64> = short.iter().map(|s| s.0).collect();
    let need_counts: Vec<i32> = short.iter().map(|s| s.1).collect();
    let declare = sqlx::query(
        "DECLARE baskfy_desk_older NO SCROLL CURSOR FOR \
         SELECT instrument.symbol, desk_older.instrument_id, desk_older.date, \
         desk_older.open::text, desk_older.high::text, desk_older.low::text, \
         desk_older.close::text, desk_older.volume::text, desk_older.close_raw::text, \
         desk_older.volume_raw::text \
         FROM UNNEST($1::bigint[], $2::integer[]) AS desk_need (instrument_id, need) \
         JOIN LATERAL (SELECT ohlcv_daily.instrument_id AS instrument_id, \
           ohlcv_daily.date AS date, ohlcv_daily.open AS open, ohlcv_daily.high AS high, \
           ohlcv_daily.low AS low, ohlcv_daily.close AS close, ohlcv_daily.volume AS volume, \
           ohlcv_daily.close_raw AS close_raw, ohlcv_daily.volume_raw AS volume_raw \
           FROM ohlcv_daily WHERE ohlcv_daily.instrument_id = desk_need.instrument_id \
           AND ohlcv_daily.date < $3 ORDER BY ohlcv_daily.date DESC \
           LIMIT desk_need.need) AS desk_older ON true \
         JOIN instrument ON instrument.id = desk_older.instrument_id",
    )
    .bind(need_ids)
    .bind(need_counts)
    .bind(window_start);
    stream_desk_bars(conn, "baskfy_desk_older", declare, &mut bars).await?;
    Ok(bars)
}

/// `load_carried(session, as_of)`: 2B.2's carried columns, by symbol.
async fn load_carried(
    conn: &mut PgConnection,
    as_of: NaiveDate,
) -> Result<Vec<Carried>, WorkerError> {
    let rows = sqlx::query(
        "SELECT instrument.symbol, factor_daily.series, factor_daily.marketcap_cr, \
         factor_daily.beta_12m::text, factor_daily.circuits_3m, factor_daily.circuits_12m, \
         factor_daily.universe_mask \
         FROM factor_daily JOIN instrument ON instrument.id = factor_daily.instrument_id \
         WHERE instrument.exchange_id = $1 AND factor_daily.date = $2 ORDER BY instrument.symbol",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let fno = mask_value(NIFTY_FNO_INDEX_ID).unwrap_or(0);
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let marketcap: Option<i64> = row.try_get(2)?;
        let beta: Option<String> = row.try_get(3)?;
        let c3: Option<i16> = row.try_get(4)?;
        let c12: Option<i16> = row.try_get(5)?;
        let mask: i32 = row.try_get(6)?;
        #[allow(clippy::cast_precision_loss)]
        let marketcap = marketcap.map(|m| m as f64);
        out.push(Carried {
            symbol: row.try_get(0)?,
            series: row.try_get(1)?,
            marketcap,
            beta: super::factors::parse_float(beta.as_deref())?,
            circuits_three_months: c3.map(i64::from),
            circuits_one_year: c12.map(i64::from),
            is_nifty_fno: i64::from(i64::from(mask) & fno != 0),
        });
    }
    Ok(out)
}

/// Python's `repr` of a list of strings (`['A', 'B']`).
fn py_str_list(items: &[String]) -> String {
    let quoted: Vec<String> = items.iter().map(|s| format!("'{s}'")).collect();
    format!("[{}]", quoted.join(", "))
}

/// `_instrument_ids(bars, as_of)`: symbol → instrument id from the as-of bars; a symbol naming
/// two instruments is refused.
fn instrument_ids(bars: &DeskBars, as_of: NaiveDate) -> Result<HashMap<String, i64>, WorkerError> {
    let mut pairs: BTreeMap<String, BTreeSet<i64>> = BTreeMap::new();
    for i in 0..bars.len() {
        if bars.date[i] == as_of {
            pairs
                .entry(bars.symbol[i].clone())
                .or_default()
                .insert(bars.instrument_id[i]);
        }
    }
    let shared: Vec<String> = pairs
        .iter()
        .filter(|(_, ids)| ids.len() > 1)
        .map(|(s, _)| s.clone())
        .collect();
    if !shared.is_empty() {
        return Err(WorkerError::python(
            "ValueError",
            format!(
                "symbols naming more than one instrument on {}: {}. The book scores by symbol; \
                 pass one exchange's instruments (NSE cash) so each is unique.",
                as_of.format("%Y-%m-%d"),
                py_str_list(&shared)
            ),
        ));
    }
    Ok(pairs
        .into_iter()
        .filter_map(|(s, ids)| ids.into_iter().next().map(|id| (s, id)))
        .collect())
}

/// One `desk_score_daily` row (`desk_rows`).
struct DeskRow {
    instrument_id: i64,
    score: Option<Decimal>,
    score_rank: Option<i32>,
    components: [Option<Decimal>; 7],
    reject: String,
}

/// `score_day` then `project` then `desk_rows`.
fn score_day(
    bars: &DeskBars,
    as_of: NaiveDate,
    trading_days: &[NaiveDate],
    carried: &[Carried],
) -> Result<Vec<DeskRow>, WorkerError> {
    let ids = instrument_ids(bars, as_of)?;
    let scan = build_scan(bars, as_of, trading_days, carried).map_err(WorkerError::from)?;
    if scan.is_empty() {
        return Ok(Vec::new());
    }
    let unknown: BTreeSet<String> = scan
        .iter()
        .filter(|r| !ids.contains_key(&r.symbol))
        .map(|r| r.symbol.clone())
        .collect();
    if !unknown.is_empty() {
        let unknown: Vec<String> = unknown.into_iter().collect();
        return Err(WorkerError::python(
            "ValueError",
            format!(
                "scored symbols with no instrument_id on {}: {}",
                as_of.format("%Y-%m-%d"),
                py_str_list(&unknown)
            ),
        ));
    }
    let scored = score(&scan);
    let mut rows = Vec::with_capacity(scan.len());
    for (row, s) in scan.iter().zip(scored) {
        let rejected = !s.reject.is_empty();
        let keep = |x: f64, places: u32| -> Result<Option<Decimal>, WorkerError> {
            if rejected {
                Ok(None)
            } else {
                quantize_half_up_repr(x, places)
            }
        };
        let [a, b, c, d, e, f] = s.components;
        rows.push(DeskRow {
            instrument_id: ids.get(&row.symbol).copied().unwrap_or_default(),
            score: keep(s.score, 1)?,
            score_rank: if rejected {
                None
            } else {
                s.rank.map(|r| i32::try_from(r).unwrap_or(i32::MAX))
            },
            components: [
                keep(a, 4)?,
                keep(b, 4)?,
                keep(c, 4)?,
                keep(d, 4)?,
                keep(e, 4)?,
                keep(f, 4)?,
                keep(s.ext_over_20dma, 4)?,
            ],
            reject: s.reject,
        });
    }
    Ok(rows)
}

/// `run_compute_desk_score(session, outcome, on)`; returns the rows written.
pub(crate) async fn run(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let carried = load_carried(conn, on).await?;
    outcome.rows_in = i64::try_from(carried.len()).unwrap_or(i64::MAX);
    if carried.is_empty() {
        outcome.note(
            "reason",
            pyjson::s("no factor_daily rows for this date, so nothing to scan"),
        );
        return Ok(0);
    }
    let trading_days = load_desk_trading_days(conn, on).await?;
    let bars = load_desk_bars(conn, on, &trading_days).await?;
    let rows = score_day(&bars, on, &trading_days, &carried)?;
    drop(bars);

    if !rows.is_empty() {
        let col =
            |k: usize| -> Vec<Option<Decimal>> { rows.iter().map(|r| r.components[k]).collect() };
        sqlx::query(
            "INSERT INTO desk_score_daily (instrument_id, date, score, score_rank, a_trend, \
             b_momentum, c_sharpe, d_consistency, e_liquidity, f_penalty, ext_over_20dma, reject, \
             score_version) \
             SELECT u.instrument_id, $2, u.score, u.score_rank, u.a, u.b, u.c, u.d, u.e, u.f, \
             u.ext, u.reject, $3 FROM UNNEST($1::bigint[], $4::numeric[], $5::integer[], \
             $6::numeric[], $7::numeric[], $8::numeric[], $9::numeric[], $10::numeric[], \
             $11::numeric[], $12::numeric[], $13::text[]) \
             AS u(instrument_id, score, score_rank, a, b, c, d, e, f, ext, reject) \
             ON CONFLICT (instrument_id, date) DO UPDATE SET score = excluded.score, \
             score_rank = excluded.score_rank, a_trend = excluded.a_trend, \
             b_momentum = excluded.b_momentum, c_sharpe = excluded.c_sharpe, \
             d_consistency = excluded.d_consistency, e_liquidity = excluded.e_liquidity, \
             f_penalty = excluded.f_penalty, ext_over_20dma = excluded.ext_over_20dma, \
             reject = excluded.reject, score_version = excluded.score_version",
        )
        .bind(rows.iter().map(|r| r.instrument_id).collect::<Vec<i64>>())
        .bind(on)
        .bind(DESK_SCORE_VERSION)
        .bind(
            rows.iter()
                .map(|r| r.score)
                .collect::<Vec<Option<Decimal>>>(),
        )
        .bind(
            rows.iter()
                .map(|r| r.score_rank)
                .collect::<Vec<Option<i32>>>(),
        )
        .bind(col(0))
        .bind(col(1))
        .bind(col(2))
        .bind(col(3))
        .bind(col(4))
        .bind(col(5))
        .bind(col(6))
        .bind(
            rows.iter()
                .map(|r| r.reject.clone())
                .collect::<Vec<String>>(),
        )
        .execute(&mut *conn)
        .await?;
    }
    let written_ids: Vec<i64> = rows.iter().map(|r| r.instrument_id).collect();
    let stale = sqlx::query(
        "DELETE FROM desk_score_daily WHERE desk_score_daily.date = $1 \
         AND NOT (desk_score_daily.instrument_id = ANY($2))",
    )
    .bind(on)
    .bind(written_ids)
    .execute(&mut *conn)
    .await?;

    let written = i64::try_from(rows.len()).unwrap_or(i64::MAX);
    outcome.rows_out = written;
    outcome.note("date", pyjson::s(on.format("%Y-%m-%d").to_string()));
    outcome.note("score_version", pyjson::s(DESK_SCORE_VERSION));
    let count = |n: usize| OrderedJson::Int(i64::try_from(n).unwrap_or(i64::MAX));
    outcome.note(
        "scored",
        count(rows.iter().filter(|r| r.score.is_some()).count()),
    );
    outcome.note(
        "rejected",
        count(rows.iter().filter(|r| !r.reject.is_empty()).count()),
    );
    outcome.note(
        "stale_rows_deleted",
        OrderedJson::Int(i64::try_from(stale.rows_affected()).unwrap_or(i64::MAX)),
    );
    Ok(written)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use chrono::{Datelike, Days, Weekday};

    use super::*;

    // test_desk_score_bounds.py::TestLookbackArithmetic
    #[test]
    fn the_lookback_covers_the_twelve_month_window_plus_one_bar_and_the_252_bar_windows() {
        let first = NaiveDate::from_ymd_opt(2024, 1, 1).unwrap();
        let weekdays: Vec<NaiveDate> = (0..1000)
            .map(|o| first + Days::new(o))
            .filter(|d| !matches!(d.weekday(), Weekday::Sat | Weekday::Sun))
            .collect();
        let as_of = *weekdays.last().unwrap();
        let anniversary = subtract_months(as_of, 12).unwrap();
        let window_12m = weekdays.partition_point(|d| *d <= as_of)
            - weekdays.partition_point(|d| *d <= anniversary);
        let lookback = desk_bar_lookback(as_of, &weekdays).unwrap();
        assert_eq!(
            lookback,
            (window_12m + 1).max(HIGH_1Y_BARS) + DESK_BAR_MARGIN
        );
        assert!(lookback < 400);

        let start = desk_bar_window_start(as_of, &weekdays, lookback);
        assert_eq!(
            weekdays
                .iter()
                .filter(|d| start <= **d && **d <= as_of)
                .count(),
            lookback
        );
        // A calendar shorter than the lookback starts at its first day; nothing is invented.
        let tail = &weekdays[weekdays.len() - 10..];
        assert_eq!(desk_bar_window_start(as_of, tail, lookback), tail[0]);
        assert_eq!(desk_bar_window_start(as_of, &[], lookback), as_of);
    }

    #[test]
    fn a_symbol_naming_two_instruments_is_refused() {
        let day = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap();
        let bars = DeskBars {
            symbol: vec!["X".into(), "X".into(), "Y".into()],
            instrument_id: vec![1, 2, 3],
            date: vec![day, day, day],
            numbers: Default::default(),
        };
        let err = instrument_ids(&bars, day).err().unwrap();
        assert_eq!(err.python_type(), "ValueError");
        assert!(err.to_string().contains("['X']"));
    }

    /// Differential check against Python's `score.score` + `desk_score_service._stored` over
    /// generated scans (`BASKFY_SCORE_DIFF_JSON` names the dump; see DECISIONS-RS R7.x of the
    /// compute lane). Ignored by default: the dump is generated, not committed.
    #[test]
    #[ignore = "needs BASKFY_SCORE_DIFF_JSON (a dump written by the lane's score_gen.py)"]
    fn score_matches_python_on_generated_scans() {
        use std::str::FromStr;

        use rust_decimal::Decimal;

        let path = std::env::var("BASKFY_SCORE_DIFF_JSON").unwrap();
        let text = std::fs::read_to_string(path).unwrap();
        let cases: serde_json::Value = serde_json::from_str(&text).unwrap();
        let f = |v: &serde_json::Value| v.as_f64().unwrap_or(f64::NAN);
        let mut checked = 0;
        for case in cases.as_array().unwrap() {
            let rows: Vec<baskfy_api::basket_build::score::ScanRow> = case["rows"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| {
                    let w = [
                        "one_month",
                        "three_months",
                        "six_months",
                        "nine_months",
                        "one_year",
                    ];
                    baskfy_api::basket_build::score::ScanRow {
                        symbol: r["symbol"].as_str().unwrap().to_owned(),
                        series: r["series"].as_str().map(str::to_owned),
                        close: f(&r["close"]),
                        marketcap: f(&r["marketcap"]),
                        median_volume_one_year: f(&r["median_volume_one_year"]),
                        rsi_one_month: f(&r["rsi_one_month"]),
                        volatility_one_year: f(&r["volatility_one_year"]),
                        beta: f(&r["beta"]),
                        circuits_three_months: f(&r["circuits_three_months"]),
                        circuits_one_year: f(&r["circuits_one_year"]),
                        positive_days_3m: f(&r["positive_days_percent_three_months"]),
                        positive_days_6m: f(&r["positive_days_percent_six_months"]),
                        ma_20: f(&r["ma_20"]),
                        ma_50: f(&r["ma_50"]),
                        ma_100: f(&r["ma_100"]),
                        ma_200: f(&r["ma_200"]),
                        away_from_high_one_year: f(&r["away_from_high_one_year"]),
                        is_nifty_fno: f(&r["is_nifty_fno"]),
                        returns: w.map(|k| f(&r[format!("absolute_return_{k}").as_str()])),
                        sharpe: w.map(|k| f(&r[format!("sharpe_return_{k}").as_str()])),
                    }
                })
                .collect();
            let out = score(&rows);
            let want = &case["out"];
            let dec = |v: &serde_json::Value| v.as_str().map(|s| Decimal::from_str(s).unwrap());
            for (i, s) in out.iter().enumerate() {
                let rejected = !s.reject.is_empty();
                assert_eq!(
                    s.reject,
                    want["reject"][i].as_str().unwrap(),
                    "reject row {i}"
                );
                let got_score = if rejected {
                    None
                } else {
                    crate::steps::compute::numeric::quantize_half_up_repr(s.score, 1).unwrap()
                };
                assert_eq!(got_score, dec(&want["score"][i]), "score row {i}");
                let got_rank = if rejected { None } else { s.rank };
                assert_eq!(got_rank, want["rank"][i].as_i64(), "rank row {i}");
                let names = [
                    "A_trend",
                    "B_momentum",
                    "C_sharpe",
                    "D_consistency",
                    "E_liquidity",
                    "F_penalty",
                    "ext_over_20dma",
                ];
                for (k, name) in names.iter().enumerate() {
                    let x = if k < 6 {
                        s.components[k]
                    } else {
                        s.ext_over_20dma
                    };
                    let got = if rejected {
                        None
                    } else {
                        crate::steps::compute::numeric::quantize_half_up_repr(x, 4).unwrap()
                    };
                    assert_eq!(got, dec(&want[*name][i]), "{name} row {i}");
                }
                checked += 1;
            }
        }
        assert!(checked > 0);
    }
}
