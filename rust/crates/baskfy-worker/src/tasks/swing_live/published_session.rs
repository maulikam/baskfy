//! `tasks/published_session.py` — one answer to "what is the last published session", for every
//! sleeve that asks (`gates/sleeve-read-contract.md` C3).
//!
//! The trade date of the most recently **published** run (`data_version IS NOT NULL`), ordered
//! by `data_version` (the publish order), not by `trade_date` — a day the quality gate refused
//! is not published, and a late re-publish of an older day does not make it "the latest". Public
//! so the other sleeves' lanes (VBT's re-scan, TWT's scan) read the same rule rather than a copy.

use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::error::WorkerError;

/// `last_published_session(session, on_or_before=None)`.
///
/// # Errors
/// The database's.
pub async fn last_published_session(
    conn: &mut PgConnection,
    on_or_before: Option<NaiveDate>,
) -> Result<Option<NaiveDate>, WorkerError> {
    let row: Option<(NaiveDate,)> = match on_or_before {
        None => {
            sqlx::query_as(
                "SELECT pipeline_run.trade_date FROM pipeline_run \
                 WHERE pipeline_run.data_version IS NOT NULL \
                 ORDER BY pipeline_run.data_version DESC LIMIT 1",
            )
            .fetch_optional(&mut *conn)
            .await?
        }
        Some(bound) => {
            sqlx::query_as(
                "SELECT pipeline_run.trade_date FROM pipeline_run \
                 WHERE pipeline_run.data_version IS NOT NULL AND pipeline_run.trade_date <= $1 \
                 ORDER BY pipeline_run.data_version DESC LIMIT 1",
            )
            .bind(bound)
            .fetch_optional(&mut *conn)
            .await?
        }
    };
    Ok(row.map(|(d,)| d))
}
