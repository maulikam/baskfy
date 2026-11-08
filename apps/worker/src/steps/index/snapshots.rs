//! Step 6 — `refresh_index_snapshots`. Ports `baskfy_worker.tasks.snapshots`.
//!
//! "~145 indices with level, absolute change, % change, PE, PB, div yield into
//! index_snapshot_daily. Handle indices that publish no fundamentals (store NULL)."
//!
//! `index_def` is populated from what NSE publishes: an index in the file we have no row for is
//! registered on the spot as a dashboard-only index (`is_universe = false`) with an id from
//! `FIRST_NON_UNIVERSE_INDEX_ID` upward, so the 15 selectable universes keep their pinned ids
//! (they carry `factor_daily` mask bits).
//!
//! **The sanity guard (SW16).** A level that moves more than [`MAX_DAY_ON_DAY_CHANGE`] against
//! the last stored level for the same index within [`GUARD_LOOKBACK_DAYS`] is refused and named;
//! the sane rows of the same file are still written; a first-ever row is accepted. The arithmetic
//! is Python's `Decimal` in its default context ([`WideDecimal`]), so the boundary and the
//! message's `{move * 100:+.1f}` round exactly where Python's do.

use std::collections::HashMap;

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_db::OrderedJson;
use baskfy_providers::records::IndexSnapshot;
use chrono::{Days, NaiveDate};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::universes::{FIRST_NON_UNIVERSE_INDEX_ID, py_strip, slugify_index};
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

/// `MAX_INDEX_ID`: smallint tops out here.
pub(crate) const MAX_INDEX_ID: i64 = 32767;

/// `MAX_DAY_ON_DAY_CHANGE = Decimal("0.40")`.
pub(crate) const MAX_DAY_ON_DAY_CHANGE: &str = "0.40";

/// `GUARD_LOOKBACK_DAYS`: a long weekend plus a holiday run.
pub(crate) const GUARD_LOOKBACK_DAYS: u64 = 14;

/// `SnapshotResult`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct SnapshotResult {
    pub(crate) rows_written: i64,
    pub(crate) registered: Vec<String>,
    pub(crate) unnamed: i64,
    /// SW16: rows the guard refused, each named with both levels and both dates.
    pub(crate) refused: Vec<String>,
}

/// One row bound for `index_snapshot_daily` (Python's `values` dict).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct SnapshotRow {
    pub(crate) index_id: i64,
    pub(crate) level: Option<Decimal>,
    pub(crate) change_abs: Option<Decimal>,
    pub(crate) change_pct: Option<Decimal>,
    pub(crate) pe: Option<Decimal>,
    pub(crate) pb: Option<Decimal>,
    pub(crate) div_yield: Option<Decimal>,
}

/// `outcome.note(date=..., newly_registered=... or None, unnamed_rows=... or None,
/// refused=... or None)`.
pub(crate) fn note_result(outcome: &mut StepOutcome, on: NaiveDate, result: &SnapshotResult) {
    outcome.note("date", pyjson::s(iso(on)));
    outcome.note(
        "newly_registered",
        if result.registered.is_empty() {
            OrderedJson::Null
        } else {
            pyjson::strings(result.registered.iter().cloned())
        },
    );
    outcome.note(
        "unnamed_rows",
        if result.unnamed == 0 {
            OrderedJson::Null
        } else {
            pyjson::int(result.unnamed)
        },
    );
    outcome.note(
        "refused",
        if result.refused.is_empty() {
            OrderedJson::Null
        } else {
            pyjson::strings(result.refused.iter().cloned())
        },
    );
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// `store_snapshots(session, on, snapshots)`: upsert one row per published index, registering
/// any we have not seen before.
pub(crate) async fn store_snapshots(
    conn: &mut PgConnection,
    on: NaiveDate,
    snapshots: &[IndexSnapshot],
) -> Result<SnapshotResult, WorkerError> {
    let mut result = SnapshotResult::default();
    if snapshots.is_empty() {
        return Ok(result);
    }

    let mut known: HashMap<String, i64> =
        sqlx::query_as::<_, (String, i16)>("SELECT index_def.slug, index_def.id FROM index_def")
            .fetch_all(&mut *conn)
            .await?
            .into_iter()
            .map(|(slug, id)| (slug, i64::from(id)))
            .collect();
    let mut next_id = next_dashboard_id(conn).await?;

    let mut values: Vec<SnapshotRow> = Vec::new();
    let mut slugs: Vec<String> = Vec::new();
    for snapshot in snapshots {
        let published = &snapshot.index_slug;
        let slug = if known.contains_key(published.as_str()) {
            published.clone()
        } else {
            slugify_index(published)
        };
        if slug.is_empty() {
            result.unnamed += 1;
            continue;
        }
        let index_id = if let Some(id) = known.get(&slug) {
            *id
        } else {
            let id = next_id;
            next_id += 1;
            register_dashboard_index(conn, id, &slug, published).await?;
            known.insert(slug.clone(), id);
            result.registered.push(slug.clone());
            id
        };
        slugs.push(slug);
        values.push(SnapshotRow {
            index_id,
            level: snapshot.level,
            change_abs: snapshot.change_abs,
            change_pct: snapshot.change_pct,
            // NULL where the index publishes no fundamentals (docs/01 §7).
            pe: snapshot.pe,
            pb: snapshot.pb,
            div_yield: snapshot.div_yield,
        });
    }

    if values.is_empty() {
        return Ok(result);
    }

    let ids: Vec<i64> = values.iter().map(|v| v.index_id).collect();
    let previous = previous_levels(conn, &ids, on).await?;
    let (kept, refused) = refuse_implausible_levels(on, values, &slugs, &previous)?;
    result.refused = refused;
    if kept.is_empty() {
        return Ok(result);
    }

    upsert_snapshots(conn, on, &kept).await?;
    result.rows_written = i64::try_from(kept.len()).unwrap_or(i64::MAX);
    Ok(result)
}

async fn upsert_snapshots(
    conn: &mut PgConnection,
    on: NaiveDate,
    rows: &[SnapshotRow],
) -> Result<(), WorkerError> {
    let index_ids = rows
        .iter()
        .map(|r| smallint(r.index_id))
        .collect::<Result<Vec<i16>, WorkerError>>()?;
    let col = |f: fn(&SnapshotRow) -> Option<Decimal>| rows.iter().map(f).collect::<Vec<_>>();
    sqlx::query(
        "INSERT INTO index_snapshot_daily \
         (index_id, date, level, change_abs, change_pct, pe, pb, div_yield) \
         SELECT * FROM UNNEST($1::smallint[], $2::date[], $3::numeric[], $4::numeric[], \
         $5::numeric[], $6::numeric[], $7::numeric[], $8::numeric[]) \
         ON CONFLICT (index_id, date) DO UPDATE SET level = excluded.level, \
         change_abs = excluded.change_abs, change_pct = excluded.change_pct, pe = excluded.pe, \
         pb = excluded.pb, div_yield = excluded.div_yield",
    )
    .bind(index_ids)
    .bind(vec![on; rows.len()])
    .bind(col(|r| r.level))
    .bind(col(|r| r.change_abs))
    .bind(col(|r| r.change_pct))
    .bind(col(|r| r.pe))
    .bind(col(|r| r.pb))
    .bind(col(|r| r.div_yield))
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// An id bound for a `smallint` column. asyncpg refuses an out-of-range int before sending it;
/// so does this (the id allocator already refuses past [`MAX_INDEX_ID`] at its first id).
fn smallint(id: i64) -> Result<i16, WorkerError> {
    i16::try_from(id).map_err(|_| {
        WorkerError::python(
            "DBAPIError",
            format!("invalid input for query argument: {id} (value out of int16 range)"),
        )
    })
}

/// `_refuse_implausible_levels`: drop, and name, every row whose level is more than 40 % away
/// from the last stored one. Pure over the anchors `_previous_levels` read.
pub(crate) fn refuse_implausible_levels(
    on: NaiveDate,
    values: Vec<SnapshotRow>,
    slugs: &[String],
    previous: &HashMap<i64, (NaiveDate, WideDecimal)>,
) -> Result<(Vec<SnapshotRow>, Vec<String>), WorkerError> {
    let bound: WideDecimal = MAX_DAY_ON_DAY_CHANGE.parse().map_err(decimal_error)?;
    let one = WideDecimal::from(1_i64);
    let hundred = WideDecimal::from(100_i64);
    let zero = WideDecimal::zero();
    let mut kept = Vec::with_capacity(values.len());
    let mut refused = Vec::new();
    for (slug, value) in slugs.iter().zip(values) {
        let (Some(level), Some((anchor_date, anchor_level))) =
            (value.level, previous.get(&value.index_id))
        else {
            kept.push(value);
            continue;
        };
        if *anchor_level <= zero {
            kept.push(value);
            continue;
        }
        let level = WideDecimal::from(level);
        let movement = level.div(anchor_level).map_err(decimal_error)?.sub(&one);
        if movement.abs() > bound {
            let pct = movement
                .mul(&hundred)
                .quantize(-1, Rounding::HalfEven)
                .map_err(decimal_error)?;
            let signed = if pct.is_sign_negative() {
                pct.to_string()
            } else {
                format!("+{pct}")
            };
            refused.push(format!(
                "{slug} {}: level {} is {signed}% against {} on {}; refused, not written",
                iso(on),
                level.to_sci_string(),
                anchor_level.to_sci_string(),
                iso(*anchor_date),
            ));
            continue;
        }
        kept.push(value);
    }
    Ok((kept, refused))
}

fn decimal_error(e: baskfy_core::decimal::DecimalError) -> WorkerError {
    WorkerError::python("InvalidOperation", e.to_string())
}

/// `_previous_levels`: the last stored `(date, level)` per index strictly before `on`, within
/// the lookback. Read as text so the anchor keeps the stored scale (`24398.7000`).
async fn previous_levels(
    conn: &mut PgConnection,
    index_ids: &[i64],
    on: NaiveDate,
) -> Result<HashMap<i64, (NaiveDate, WideDecimal)>, WorkerError> {
    if index_ids.is_empty() {
        return Ok(HashMap::new());
    }
    let ids = index_ids
        .iter()
        .map(|id| smallint(*id))
        .collect::<Result<Vec<i16>, WorkerError>>()?;
    let since = on
        .checked_sub_days(Days::new(GUARD_LOOKBACK_DAYS))
        .unwrap_or(NaiveDate::MIN);
    let rows: Vec<(i16, NaiveDate, String)> = sqlx::query_as(
        "SELECT index_snapshot_daily.index_id, index_snapshot_daily.date, \
         index_snapshot_daily.level::text FROM index_snapshot_daily \
         WHERE index_snapshot_daily.index_id = ANY($1::smallint[]) \
         AND index_snapshot_daily.date < $2 AND index_snapshot_daily.date >= $3 \
         AND index_snapshot_daily.level IS NOT NULL \
         ORDER BY index_snapshot_daily.index_id, index_snapshot_daily.date",
    )
    .bind(ids)
    .bind(on)
    .bind(since)
    .fetch_all(&mut *conn)
    .await?;
    let mut latest = HashMap::new();
    for (index_id, day, level) in rows {
        // Ordered by date ascending, so the last write per index is the latest row.
        let level: WideDecimal = level.parse().map_err(decimal_error)?;
        latest.insert(i64::from(index_id), (day, level));
    }
    Ok(latest)
}

/// `_next_dashboard_id`: the next free id at or above `FIRST_NON_UNIVERSE_INDEX_ID`.
async fn next_dashboard_id(conn: &mut PgConnection) -> Result<i64, WorkerError> {
    let (highest,): (Option<i16>,) =
        sqlx::query_as("SELECT max(index_def.id) AS max_1 FROM index_def WHERE index_def.id >= $1")
            .bind(i16::try_from(FIRST_NON_UNIVERSE_INDEX_ID).unwrap_or(i16::MAX))
            .fetch_one(&mut *conn)
            .await?;
    let candidate = highest.map_or(FIRST_NON_UNIVERSE_INDEX_ID, |h| i64::from(h) + 1);
    if candidate > MAX_INDEX_ID {
        return Err(WorkerError::python(
            "ValueError",
            format!(
                "no index_def id available below {MAX_INDEX_ID}; index_def has outgrown smallint"
            ),
        ));
    }
    Ok(candidate)
}

/// `_register_dashboard_index`: not selectable in the screener (`is_universe = false`), sorted
/// by its id; `ON CONFLICT (id) DO NOTHING`.
async fn register_dashboard_index(
    conn: &mut PgConnection,
    index_id: i64,
    slug: &str,
    name: &str,
) -> Result<(), WorkerError> {
    let id = smallint(index_id)?;
    sqlx::query(
        "INSERT INTO index_def (id, slug, name, is_universe, sort_order) \
         VALUES ($1, $2, $3, $4, $5) ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(slug)
    .bind(py_strip(name))
    .bind(false)
    .bind(id)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    fn row(index_id: i64, level: &str) -> SnapshotRow {
        SnapshotRow {
            index_id,
            level: Some(level.parse().unwrap()),
            change_abs: None,
            change_pct: None,
            pe: None,
            pb: None,
            div_yield: None,
        }
    }

    const REAL_DAY: (i32, u32, u32) = (2026, 7, 7);
    const FIXTURE_DAY: (i32, u32, u32) = (2026, 7, 8);

    fn anchors() -> HashMap<i64, (NaiveDate, WideDecimal)> {
        let real = d(REAL_DAY.0, REAL_DAY.1, REAL_DAY.2);
        // What `_previous_levels` reads back for the seeded real day: the stored scale.
        HashMap::from([
            (1, (real, "24398.7000".parse().unwrap())),
            (5, (real, "23368.8500".parse().unwrap())),
        ])
    }

    fn slugs(names: &[&str]) -> Vec<String> {
        names.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn a_move_over_forty_percent_is_refused_and_named() {
        // test_snapshots.TestSanityGuard.test_a_move_over_forty_percent_is_refused_and_named.
        let on = d(FIXTURE_DAY.0, FIXTURE_DAY.1, FIXTURE_DAY.2);
        let (kept, refused) = refuse_implausible_levels(
            on,
            vec![row(1, "1004.6026")],
            &slugs(&["nifty-50"]),
            &anchors(),
        )
        .unwrap();
        assert!(kept.is_empty());
        assert_eq!(refused.len(), 1);
        assert!(
            refused[0].starts_with("nifty-50 2026-07-08: level 1004.6026 is -95.9%"),
            "{}",
            refused[0]
        );
        assert_eq!(
            refused[0],
            "nifty-50 2026-07-08: level 1004.6026 is -95.9% against 24398.7000 on 2026-07-07; \
             refused, not written"
        );
    }

    #[test]
    fn the_sane_rows_of_the_same_file_are_still_written() {
        let on = d(FIXTURE_DAY.0, FIXTURE_DAY.1, FIXTURE_DAY.2);
        let (kept, refused) = refuse_implausible_levels(
            on,
            vec![row(1, "1004.6026"), row(5, "23500.10")],
            &slugs(&["nifty-50", "nifty-500"]),
            &anchors(),
        )
        .unwrap();
        assert_eq!(kept, vec![row(5, "23500.10")]);
        let first_words: Vec<&str> = refused
            .iter()
            .map(|r| r.split(' ').next().unwrap())
            .collect();
        assert_eq!(first_words, ["nifty-50"]);
    }

    #[test]
    fn a_move_at_the_threshold_is_accepted() {
        // 24398.70 * (1 - 0.40), quantized to 0.0001: exactly -40 %, inside the bound.
        let on = d(FIXTURE_DAY.0, FIXTURE_DAY.1, FIXTURE_DAY.2);
        let (kept, refused) = refuse_implausible_levels(
            on,
            vec![row(1, "14639.2200")],
            &slugs(&["nifty-50"]),
            &anchors(),
        )
        .unwrap();
        assert!(refused.is_empty());
        assert_eq!(kept.len(), 1);
        // One step past it is refused.
        let (_, refused) = refuse_implausible_levels(
            on,
            vec![row(1, "14639.2199")],
            &slugs(&["nifty-50"]),
            &anchors(),
        )
        .unwrap();
        assert_eq!(refused.len(), 1);
    }

    #[test]
    fn a_first_ever_row_and_a_null_level_are_accepted() {
        let on = d(FIXTURE_DAY.0, FIXTURE_DAY.1, FIXTURE_DAY.2);
        let mut no_level = row(1, "1");
        no_level.level = None;
        let (kept, refused) = refuse_implausible_levels(
            on,
            vec![row(42, "1004.6026"), no_level.clone()],
            &slugs(&["india-vix", "nifty-50"]),
            &anchors(),
        )
        .unwrap();
        assert!(refused.is_empty());
        assert_eq!(kept, vec![row(42, "1004.6026"), no_level]);
    }

    #[test]
    fn a_non_positive_anchor_guards_nothing() {
        let on = d(FIXTURE_DAY.0, FIXTURE_DAY.1, FIXTURE_DAY.2);
        let previous = HashMap::from([(1, (d(2026, 7, 7), "0.0000".parse().unwrap()))]);
        let (kept, refused) =
            refuse_implausible_levels(on, vec![row(1, "5")], &slugs(&["nifty-50"]), &previous)
                .unwrap();
        assert!(refused.is_empty());
        assert_eq!(kept.len(), 1);
    }

    #[test]
    fn the_percentage_rounds_half_even_with_a_sign() {
        // Python: f"{Decimal('41.25'):+.1f}" == "+41.2"; a rise prints its plus sign.
        let on = d(2026, 7, 8);
        let previous = HashMap::from([(1, (d(2026, 7, 7), "100".parse().unwrap()))]);
        let (_, refused) =
            refuse_implausible_levels(on, vec![row(1, "141.25")], &slugs(&["x"]), &previous)
                .unwrap();
        assert_eq!(
            refused,
            [
                "x 2026-07-08: level 141.25 is +41.2% against 100 on 2026-07-07; refused, not written"
            ]
        );
        let (_, refused) =
            refuse_implausible_levels(on, vec![row(1, "141.35")], &slugs(&["x"]), &previous)
                .unwrap();
        assert!(refused[0].contains("is +41.4%"), "{}", refused[0]);
    }

    #[test]
    fn the_note_carries_nones_for_empty_facts() {
        let mut outcome = StepOutcome::default();
        note_result(&mut outcome, d(2026, 10, 29), &SnapshotResult::default());
        assert_eq!(
            pyjson::dumps(&outcome.detail_json()).unwrap(),
            r#"{"date": "2026-10-29", "newly_registered": null, "unnamed_rows": null, "refused": null}"#
        );
        let mut outcome = StepOutcome::default();
        note_result(
            &mut outcome,
            d(2026, 10, 29),
            &SnapshotResult {
                rows_written: 1,
                registered: vec!["india-vix".into()],
                unnamed: 2,
                refused: vec!["r".into()],
            },
        );
        assert_eq!(
            pyjson::dumps(&outcome.detail_json()).unwrap(),
            r#"{"date": "2026-10-29", "newly_registered": ["india-vix"], "unnamed_rows": 2, "refused": ["r"]}"#
        );
    }
}
