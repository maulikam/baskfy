//! The two cross-sectional `factor_daily` columns, filled once the day's rows are written.
//! Ports `baskfy_worker.tasks.ranking` (`fill_mom_pctile`, `fill_rank_persist_20`,
//! `run_rank_columns`) and the two pure functions it calls from
//! `baskfy_core.factors_ranking`, which have no Rust home yet: `cross_sectional_pctile` and
//! `rank_persistence`.
//!
//! * `mom_pctile` — percent rank x 100 of `avg_sharpe_12_6_3_1` (the mean of the **stored**
//!   `sharpe_12m/6m/3m/1m`, NULL if any is NULL) among the date's rows with `universe_mask <> 0`:
//!   `100 x (average-tie rank - 1) / (n - 1)`, a population of one is 100, rounded half-up 2 dp.
//! * `rank_persist_20` — the % of the last 20 distinct `factor_daily` dates on or before the
//!   as-of date on which the instrument's stored `mom_pctile` was >= 80; NULL unless all 20 carry
//!   one. Nothing dated after the as-of date is read (house rule 5).
//!
//! Both updates touch only their own column; running them twice leaves identical rows (house
//! rule 7).

use std::collections::{BTreeMap, BTreeSet, HashSet};

use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row};

use super::numeric::{float_to_numeric, round_half_up};
use crate::error::WorkerError;

/// `factors_ranking.RANK_PERSIST_DATES`.
pub(crate) const RANK_PERSIST_DATES: usize = 20;
/// `factors_ranking.RANK_PERSIST_THRESHOLD`.
pub(crate) const RANK_PERSIST_THRESHOLD: f64 = 80.0;
/// `precision.PERCENT_DP`.
const PERCENT_DP: i32 = 2;
/// `mom_pctile` and `rank_persist_20` are `numeric(7,2)`.
const PERCENT_SCALE: u32 = 2;

/// One row `cross_sectional_pctile` ranks: the four stored Sharpe components in
/// `MOM_PCTILE_COMPONENTS` order (`sharpe_12m`, `sharpe_6m`, `sharpe_3m`, `sharpe_1m`).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct PctileInput {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `universe_mask` (`None` is not eligible).
    pub universe_mask: Option<i64>,
    /// The four components, `None` for NULL.
    pub components: [Option<f64>; 4],
}

/// `blends.blend_expr`: the arithmetic mean, NULL if any component is NULL. polars'
/// `sum_horizontal` reduces as a **tree**, so four columns add as `(a + b) + (c + d)` — not left
/// to right. Measured on polars 1.43: 0 of 20,000 random 2-dp rows differ from the pairwise sum,
/// 5,766 from the left fold, and the left fold moved 1,259 stored `mom_pctile` values on the
/// 2026-10-29 replay day (ties split differently).
fn blend(components: &[Option<f64>; 4]) -> Option<f64> {
    let [a, b, c, d] = *components;
    Some(((a? + b?) + (c? + d?)) / 4.0)
}

/// Average ranks (1-based, ties share the mean of their positions) of `values`, `None` stays
/// `None` — polars' `rank(method="average")`. Ties are exact float equality, as polars'.
#[allow(clippy::float_cmp)]
pub(crate) fn average_ranks(values: &[Option<f64>]) -> Vec<Option<f64>> {
    let mut order: Vec<(usize, f64)> = values
        .iter()
        .enumerate()
        .filter_map(|(i, v)| v.map(|x| (i, x)))
        .collect();
    order.sort_by(|a, b| a.1.total_cmp(&b.1));
    let mut out = vec![None; values.len()];
    let mut start = 0;
    while start < order.len() {
        let mut end = start + 1;
        while end < order.len() && order[end].1 == order[start].1 {
            end += 1;
        }
        // positions start+1 ..= end, their mean
        #[allow(clippy::cast_precision_loss)]
        let rank = (start + 1 + end) as f64 / 2.0;
        for item in &order[start..end] {
            out[item.0] = Some(rank);
        }
        start = end;
    }
    out
}

/// `factors_ranking.cross_sectional_pctile` on the rows as written: `(instrument_id,
/// mom_pctile)` in input order.
pub(crate) fn cross_sectional_pctile(rows: &[PctileInput]) -> Vec<(i64, Option<f64>)> {
    let values: Vec<Option<f64>> = rows.iter().map(|r| blend(&r.components)).collect();
    let eligible: Vec<bool> = rows
        .iter()
        .zip(&values)
        .map(|(r, v)| r.universe_mask.is_some_and(|m| m != 0) && v.is_some_and(f64::is_finite))
        .collect();
    let count = eligible.iter().filter(|e| **e).count();
    let masked: Vec<Option<f64>> = values
        .iter()
        .zip(&eligible)
        .map(|(v, e)| if *e { *v } else { None })
        .collect();
    let ranks = average_ranks(&masked);
    #[allow(clippy::cast_precision_loss)]
    let denominator = count.saturating_sub(1).max(1) as f64;
    rows.iter()
        .enumerate()
        .map(|(i, r)| {
            let pct = if !eligible[i] {
                None
            } else if count == 1 {
                Some(100.0)
            } else {
                ranks[i].map(|rank| (rank - 1.0) / denominator * 100.0)
            };
            (r.instrument_id, round_half_up(pct, PERCENT_DP))
        })
        .collect()
}

/// `factors_ranking.rank_persistence`: `(instrument_id, rank_persist_20)` for every row dated
/// `as_of`, in input order.
///
/// # Errors
/// `ValueError` when one `(instrument_id, date)` appears twice, as Python's.
pub(crate) fn rank_persistence(
    history: &[(i64, NaiveDate, Option<f64>)],
    as_of: NaiveDate,
) -> Result<Vec<(i64, Option<f64>)>, WorkerError> {
    let past: Vec<&(i64, NaiveDate, Option<f64>)> =
        history.iter().filter(|r| r.1 <= as_of).collect();
    let mut seen = HashSet::with_capacity(past.len());
    for r in &past {
        if !seen.insert((r.0, r.1)) {
            return Err(WorkerError::python(
                "ValueError",
                "rank_persistence was given the same (instrument_id, date) twice",
            ));
        }
    }
    let today: Vec<i64> = past.iter().filter(|r| r.1 == as_of).map(|r| r.0).collect();
    let dates: BTreeSet<NaiveDate> = past.iter().map(|r| r.1).collect();
    if dates.len() < RANK_PERSIST_DATES || today.is_empty() {
        return Ok(today.into_iter().map(|id| (id, None)).collect());
    }
    let window: BTreeSet<NaiveDate> = dates
        .iter()
        .rev()
        .take(RANK_PERSIST_DATES)
        .copied()
        .collect();
    let mut counts: BTreeMap<i64, (usize, usize)> = BTreeMap::new();
    for r in past.iter().filter(|r| window.contains(&r.1)) {
        let entry = counts.entry(r.0).or_insert((0, 0));
        if let Some(v) = r.2 {
            entry.0 += 1;
            if v >= RANK_PERSIST_THRESHOLD {
                entry.1 += 1;
            }
        }
    }
    Ok(today
        .into_iter()
        .map(|id| {
            let value = match counts.get(&id) {
                Some(&(observed, hits)) if observed >= RANK_PERSIST_DATES => {
                    #[allow(clippy::cast_precision_loss)]
                    let share = hits as f64 / RANK_PERSIST_DATES as f64 * 100.0;
                    round_half_up(Some(share), PERCENT_DP)
                }
                _ => None,
            };
            (id, value)
        })
        .collect())
}

fn parse_opt_f64(text: Option<String>) -> Result<Option<f64>, WorkerError> {
    text.map(|t| {
        t.parse::<f64>().map_err(|e| {
            WorkerError::python(
                "ValueError",
                format!("could not convert {t:?} to float: {e}"),
            )
        })
    })
    .transpose()
}

/// `_update_by_key` over `rows`: `UPDATE factor_daily SET <column> = value WHERE instrument_id
/// = id AND date = on`, every row bound as Python's executemany binds it (`Decimal(float)`,
/// rounded to the column's 2 dp by PostgreSQL). Returns `len(rows)`, as Python does.
async fn execute_updates(
    conn: &mut PgConnection,
    column: &str,
    on: NaiveDate,
    rows: &[(i64, Option<f64>)],
) -> Result<i64, WorkerError> {
    if rows.is_empty() {
        return Ok(0);
    }
    let ids: Vec<i64> = rows.iter().map(|r| r.0).collect();
    let values: Vec<Option<Decimal>> = rows
        .iter()
        .map(|r| match r.1 {
            Some(x) => float_to_numeric(x, PERCENT_SCALE),
            None => Ok(None),
        })
        .collect::<Result<_, _>>()?;
    let sql = format!(
        "UPDATE factor_daily AS f SET {column} = v.value \
         FROM UNNEST($1::bigint[], $2::numeric[]) AS v(instrument_id, value) \
         WHERE f.instrument_id = v.instrument_id AND f.date = $3"
    );
    sqlx::query(&sql)
        .bind(ids)
        .bind(values)
        .bind(on)
        .execute(&mut *conn)
        .await?;
    i64::try_from(rows.len()).map_err(|_| WorkerError::python("OverflowError", "too many rows"))
}

/// `fill_mom_pctile`: write `mom_pctile` for every row dated `on`; returns rows updated.
pub(crate) async fn fill_mom_pctile(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let rows = sqlx::query(
        "SELECT factor_daily.instrument_id, factor_daily.universe_mask, \
         factor_daily.sharpe_12m::text, factor_daily.sharpe_6m::text, \
         factor_daily.sharpe_3m::text, factor_daily.sharpe_1m::text \
         FROM factor_daily WHERE factor_daily.date = $1",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        return Ok(0);
    }
    let mut inputs = Vec::with_capacity(rows.len());
    for row in rows {
        let mask: i32 = row.try_get(1)?;
        inputs.push(PctileInput {
            instrument_id: row.try_get(0)?,
            universe_mask: Some(i64::from(mask)),
            components: [
                parse_opt_f64(row.try_get(2)?)?,
                parse_opt_f64(row.try_get(3)?)?,
                parse_opt_f64(row.try_get(4)?)?,
                parse_opt_f64(row.try_get(5)?)?,
            ],
        });
    }
    let ranked = cross_sectional_pctile(&inputs);
    execute_updates(conn, "mom_pctile", on, &ranked).await
}

/// `persistence_dates`: the last 20 distinct `factor_daily` dates on or before `on`, ascending.
async fn persistence_dates(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT factor_daily.date FROM factor_daily WHERE factor_daily.date <= $1 \
         GROUP BY factor_daily.date ORDER BY factor_daily.date DESC LIMIT $2",
    )
    .bind(on)
    .bind(i64::try_from(RANK_PERSIST_DATES).unwrap_or(20))
    .fetch_all(&mut *conn)
    .await?;
    let mut dates: Vec<NaiveDate> = rows.into_iter().map(|r| r.0).collect();
    dates.sort();
    Ok(dates)
}

/// `fill_rank_persist_20`: write `rank_persist_20` for every row dated `on`, from the stored
/// `mom_pctile` history. Runs after [`fill_mom_pctile`] for `on`.
pub(crate) async fn fill_rank_persist_20(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let dates = persistence_dates(conn, on).await?;
    if dates.is_empty() {
        return Ok(0);
    }
    let rows = sqlx::query(
        "SELECT factor_daily.instrument_id, factor_daily.date, factor_daily.mom_pctile::text \
         FROM factor_daily WHERE factor_daily.date = ANY($1)",
    )
    .bind(&dates)
    .fetch_all(&mut *conn)
    .await?;
    let mut history = Vec::with_capacity(rows.len());
    for row in rows {
        history.push((
            row.try_get::<i64, _>(0)?,
            row.try_get::<NaiveDate, _>(1)?,
            parse_opt_f64(row.try_get(2)?)?,
        ));
    }
    let persistence = rank_persistence(&history, on)?;
    execute_updates(conn, "rank_persist_20", on, &persistence).await
}

/// `run_rank_columns`: both columns for `on`, in dependency order, as
/// `{"mom_pctile_rows", "rank_persist_20_rows"}`.
pub(crate) async fn run_rank_columns(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<(i64, i64), WorkerError> {
    let mom = fill_mom_pctile(conn, on).await?;
    let persist = fill_rank_persist_20(conn, on).await?;
    Ok((mom, persist))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::float_cmp)]
    use super::*;

    fn row(id: i64, mask: i64, sharpe: Option<f64>) -> PctileInput {
        PctileInput {
            instrument_id: id,
            universe_mask: Some(mask),
            components: [sharpe, sharpe, sharpe, sharpe],
        }
    }

    // test_ranking_worker.py::test_mom_pctile_excludes_rows_outside_every_universe
    #[test]
    fn mom_pctile_excludes_rows_outside_every_universe_and_without_a_blend() {
        let rows = vec![
            row(1, 1, Some(0.0)),
            row(2, 1, None),
            row(3, 1, Some(2.0)),
            row(4, 0, Some(3.0)),
        ];
        assert_eq!(
            cross_sectional_pctile(&rows),
            vec![(1, Some(0.0)), (2, None), (3, Some(100.0)), (4, None)]
        );
    }

    #[test]
    fn the_blend_adds_pairwise_as_polars_sum_horizontal() {
        // ((3.73 + 1.88) + (0.82 + -0.14)) / 4: the left fold gives 6.29, polars 6.289999999999999.
        let pairwise = blend(&[Some(3.73), Some(1.88), Some(0.82), Some(-0.14)]).unwrap();
        assert_eq!(pairwise * 4.0, 6.289_999_999_999_999);
        assert_eq!(blend(&[Some(1.0), None, Some(1.0), Some(1.0)]), None);
    }

    #[test]
    fn mom_pctile_is_percent_rank_with_average_ties_and_one_is_100() {
        let rows = vec![
            row(1, 1, Some(1.0)),
            row(2, 1, Some(2.0)),
            row(3, 1, Some(2.0)),
            row(4, 1, Some(5.0)),
            row(5, 1, Some(0.5)),
        ];
        // ranks 2, 3.5, 3.5, 5, 1 over n - 1 = 4
        assert_eq!(
            cross_sectional_pctile(&rows),
            vec![
                (1, Some(25.0)),
                (2, Some(62.5)),
                (3, Some(62.5)),
                (4, Some(100.0)),
                (5, Some(0.0))
            ]
        );
        assert_eq!(
            cross_sectional_pctile(&[row(9, 4, Some(1.0))]),
            vec![(9, Some(100.0))]
        );
    }

    fn days(n: usize) -> Vec<NaiveDate> {
        let start = NaiveDate::from_ymd_opt(2026, 1, 1).unwrap();
        (0..n)
            .map(|i| start + chrono::Days::new(u64::try_from(i).unwrap()))
            .collect()
    }

    // test_ranking_worker.py::test_rank_persist_20_counts_the_stored_mom_pctile_of_the_last_20_dates
    #[test]
    fn rank_persist_20_counts_the_last_20_dates_and_needs_all_of_them() {
        let days = days(21);
        let mut history = Vec::new();
        for (n, day) in days.iter().enumerate() {
            history.push((1, *day, Some(100.0)));
            history.push((2, *day, Some(if n % 2 == 0 { 100.0 } else { 40.0 })));
            history.push((3, *day, Some(0.0)));
        }
        // Nineteen dates is not a window.
        let short = rank_persistence(&history, days[18]).unwrap();
        assert!(short.iter().all(|r| r.1.is_none()));
        let full: BTreeMap<i64, Option<f64>> = rank_persistence(&history, days[19])
            .unwrap()
            .into_iter()
            .collect();
        assert_eq!(full[&1], Some(100.0));
        assert_eq!(full[&2], Some(50.0));
        assert_eq!(full[&3], Some(0.0));
        // The window slides and nothing after the as-of date is read.
        let slid: BTreeMap<i64, Option<f64>> = rank_persistence(&history, days[20])
            .unwrap()
            .into_iter()
            .collect();
        assert_eq!(slid[&2], Some(50.0));
    }

    // test_rank_persist_20_is_null_when_a_date_in_the_window_lacks_mom_pctile
    #[test]
    fn rank_persist_20_is_null_when_a_date_lacks_mom_pctile() {
        let days = days(20);
        let mut history = Vec::new();
        for (n, day) in days.iter().enumerate() {
            history.push((1, *day, Some(90.0)));
            history.push((2, *day, if n == 5 { None } else { Some(90.0) }));
        }
        let out: BTreeMap<i64, Option<f64>> = rank_persistence(&history, days[19])
            .unwrap()
            .into_iter()
            .collect();
        assert_eq!(out[&1], Some(100.0));
        assert_eq!(out[&2], None);
    }

    #[test]
    fn rank_persistence_refuses_a_duplicate_key() {
        let d = days(1)[0];
        assert!(rank_persistence(&[(1, d, None), (1, d, None)], d).is_err());
    }
}
