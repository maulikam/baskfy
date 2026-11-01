//! `baskfy_worker.seeds.options_event_days`: verified event days and the options config rows.

use baskfy_core::options::config::{OptionsConfig, Sleeve};
use baskfy_db::OrderedJson;
use chrono::{NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use crate::error::WorkerError;

pub(crate) const RBI_MPC_2026_27: &str =
    "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=62422";

/// `EVENT_DAYS`: (date, reason, note); every one sourced from [`RBI_MPC_2026_27`].
pub(crate) const EVENT_DAYS: [((i32, u32, u32), &str, &str); 6] = [
    ((2026, 4, 8), "RBI_POLICY", "MPC decision, Apr 6-8"),
    ((2026, 6, 5), "RBI_POLICY", "MPC decision, Jun 3-5"),
    ((2026, 8, 5), "RBI_POLICY", "MPC decision, Aug 3-5"),
    ((2026, 10, 7), "RBI_POLICY", "MPC decision, Oct 5-7"),
    ((2026, 12, 4), "RBI_POLICY", "MPC decision, Dec 2-4"),
    ((2027, 2, 5), "RBI_POLICY", "MPC decision, Feb 3-5"),
];

/// `_GROUP_SLEEVE`: a representative sleeve per config group.
pub(crate) const GROUP_SLEEVE: [(&str, Sleeve); 4] = [
    ("O1M", Sleeve::O1m),
    ("O1W", Sleeve::O1w),
    ("O2", Sleeve::O2),
    ("O3", Sleeve::O3a),
];

/// `sleeve_config_rows(user_id)`: (sleeve, risk, max lots, hard exit); capital stays 0.
pub(crate) fn sleeve_config_rows(
    config: &OptionsConfig,
) -> Vec<(&'static str, Decimal, i64, NaiveTime)> {
    GROUP_SLEEVE
        .iter()
        .map(|(group, sleeve)| {
            (
                *group,
                config.risk_per_trade_pct(*sleeve),
                config.max_lots(*sleeve),
                config.hard_exit_time(*sleeve),
            )
        })
        .collect()
}

/// `seed_options(session, user_id)`: rows written per table; `ON CONFLICT DO NOTHING` everywhere.
pub(crate) async fn seed_options(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<(String, OrderedJson)>, WorkerError> {
    let mut days = 0u64;
    for ((y, m, d), reason, note) in EVENT_DAYS {
        let date = NaiveDate::from_ymd_opt(y, m, d).unwrap_or_default();
        days += sqlx::query(
            "INSERT INTO op_event_day (user_id, date, reason, source, source_url, note) \
             VALUES ($1, $2, $3, 'SEED', $4, $5) ON CONFLICT (user_id, date) DO NOTHING",
        )
        .bind(user_id)
        .bind(date)
        .bind(reason)
        .bind(RBI_MPC_2026_27)
        .bind(note)
        .execute(&mut *conn)
        .await?
        .rows_affected();
    }
    let book = sqlx::query(
        "INSERT INTO op_book_config (user_id, updated_by) VALUES ($1, 'seed') \
         ON CONFLICT (user_id) DO NOTHING",
    )
    .bind(user_id)
    .execute(&mut *conn)
    .await?
    .rows_affected();
    let mut sleeves = 0u64;
    for (sleeve, risk, lots, exit) in sleeve_config_rows(&OptionsConfig::default()) {
        sleeves += sqlx::query(
            "INSERT INTO op_sleeve_config (user_id, sleeve, risk_per_trade_pct, max_lots, \
             hard_exit_time, updated_by) VALUES ($1, $2, $3, $4, $5, 'seed') \
             ON CONFLICT (user_id, sleeve) DO NOTHING",
        )
        .bind(user_id)
        .bind(sleeve)
        .bind(risk)
        .bind(i16::try_from(lots).unwrap_or(i16::MAX))
        .bind(exit)
        .execute(&mut *conn)
        .await?
        .rows_affected();
    }
    let int = |n: u64| OrderedJson::Int(i64::try_from(n).unwrap_or(i64::MAX));
    Ok(vec![
        ("op_event_day".to_owned(), int(days)),
        ("op_book_config".to_owned(), int(book)),
        ("op_sleeve_config".to_owned(), int(sleeves)),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_rows_are_the_configs_defaults_and_hard_exit_by_three() {
        let rows = sleeve_config_rows(&OptionsConfig::default());
        let names: Vec<&str> = rows.iter().map(|r| r.0).collect();
        assert_eq!(names, ["O1M", "O1W", "O2", "O3"]);
        let three = NaiveTime::from_hms_opt(15, 0, 0).unwrap_or_default();
        for row in &rows {
            assert!(row.3 <= three);
        }
        assert_eq!((rows[0].1, rows[0].2), (Decimal::new(100, 2), 3));
        assert_eq!(rows[2].3, three);
        assert_eq!(
            rows[3].3,
            NaiveTime::from_hms_opt(14, 45, 0).unwrap_or_default()
        );
    }

    #[test]
    fn every_event_day_is_a_dated_rbi_decision() {
        for ((y, m, d), reason, note) in EVENT_DAYS {
            assert!(NaiveDate::from_ymd_opt(y, m, d).is_some());
            assert_eq!(reason, "RBI_POLICY");
            assert!(note.starts_with("MPC decision"));
        }
    }
}
