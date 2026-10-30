//! `baskfy.accounts.purge` — ports `baskfy_worker.tasks.purge_accounts.run_purge_accounts` and
//! `celery_tasks.purge_accounts_task` (Prompt 12 §5, docs/11 §Compliance: the seven-day
//! soft-delete window closes).
//!
//! An account whose `account_deletion.purge_after` has passed is **deleted** (everything it owns
//! cascades from `app_user`) when it never paid, and **anonymised in place** when an invoice
//! exists (India's tax law keeps the invoice; the person is removed from the row it points at).
//! The evidence is the returned note: public ids, never an address.
//!
//! One transaction (`run_in_session`): commit on return, roll back on raise. The statements are
//! the ORM's, in its order — the sweep's `SELECT` (no `ORDER BY`, so the clone's heap order, the
//! same on both sides), one `SELECT` of the user per row, the payment count, the flush of
//! `purged_at`, then the delete or the anonymisation — and a deletion row set to `cancelled_at`
//! (a reactivated account) or to `purged_at` (a missing user) is written by the final flush.

use baskfy_db::OrderedJson;
use chrono::{DateTime, Utc};
use sqlx::{PgConnection, Row as _};

use crate::error::WorkerError;
use crate::pyjson::{int, object, strings};
use crate::steps::{StepOutcome, log_warning};

/// `TOMBSTONE_DOMAIN`.
pub const TOMBSTONE_DOMAIN: &str = "deleted.invalid";

/// `_OWNED_TABLES`, in Python's order: deleted explicitly on the anonymise path.
const OWNED_TABLES: [&str; 5] = [
    "screen",
    "portfolio",
    "consent_record",
    "auth_token",
    "refresh_token",
];

/// `PurgeResult`.
#[derive(Debug, Default)]
struct PurgeResult {
    considered: i64,
    purged: i64,
    anonymised: i64,
    public_ids: Vec<String>,
}

/// A deletion row's pending write (the ORM's dirty attribute), flushed at the end.
enum Pending {
    PurgedAt(i64),
    CancelledAt(i64),
}

/// `run_purge_accounts(session, outcome, now=)`: erase every account whose window has closed.
/// Idempotent — a purged row is skipped. Returns the number purged.
///
/// # Errors
/// The database's.
pub async fn run_purge_accounts(
    tx: &mut PgConnection,
    outcome: &mut StepOutcome,
    moment: DateTime<Utc>,
) -> Result<i64, WorkerError> {
    let mut result = PurgeResult::default();
    let due: Vec<i64> = sqlx::query(
        "SELECT account_deletion.user_id, account_deletion.requested_at, \
         account_deletion.purge_after, account_deletion.cancelled_at, \
         account_deletion.purged_at \nFROM account_deletion \nWHERE \
         account_deletion.cancelled_at IS NULL AND account_deletion.purged_at IS NULL AND \
         account_deletion.purge_after <= $1::TIMESTAMP WITH TIME ZONE",
    )
    .bind(moment)
    .fetch_all(&mut *tx)
    .await?
    .iter()
    .map(|r| r.try_get::<i64, _>("user_id"))
    .collect::<Result<_, _>>()?;
    result.considered = i64::try_from(due.len()).unwrap_or(i64::MAX);

    let mut pending: Vec<Pending> = Vec::new();
    for user_id in due {
        let user = sqlx::query(
            "SELECT app_user.id, app_user.public_id, app_user.deleted_at \nFROM app_user \
             \nWHERE app_user.id = $1::BIGINT",
        )
        .bind(user_id)
        .fetch_optional(&mut *tx)
        .await?;
        let Some(user) = user else {
            pending.push(Pending::PurgedAt(user_id));
            continue;
        };
        let deleted_at: Option<DateTime<Utc>> = user.try_get("deleted_at")?;
        if deleted_at.is_none() {
            // Reactivated between the sweep's read and now, or by a path that forgot to cancel.
            log_warning(&format!(
                "skipping purge of a reactivated account (user_id={user_id})"
            ));
            pending.push(Pending::CancelledAt(user_id));
            continue;
        }
        let public_id: String = user.try_get("public_id")?;
        result.public_ids.push(public_id.clone());
        let invoices: i64 = sqlx::query_scalar(
            "SELECT count(*) AS count_1 \nFROM payment \nWHERE payment.user_id = $1::BIGINT",
        )
        .bind(user_id)
        .fetch_one(&mut *tx)
        .await?;

        // `row.purged_at = moment; await session.flush()` — with every earlier pending write.
        pending.push(Pending::PurgedAt(user_id));
        flush(tx, &mut pending, moment).await?;

        if invoices > 0 {
            anonymise(tx, user_id, &public_id).await?;
            result.anonymised += 1;
        } else {
            sqlx::query("DELETE FROM app_user WHERE app_user.id = $1::BIGINT")
                .bind(user_id)
                .execute(&mut *tx)
                .await?;
        }
        result.purged += 1;
    }
    flush(tx, &mut pending, moment).await?;

    outcome.rows_in = result.considered;
    outcome.rows_out = result.purged;
    outcome.note(
        "purged_public_ids",
        if result.public_ids.is_empty() {
            OrderedJson::Null
        } else {
            strings(result.public_ids.iter().cloned())
        },
    );
    outcome.note(
        "anonymised",
        if result.anonymised == 0 {
            OrderedJson::Null
        } else {
            int(result.anonymised)
        },
    );
    Ok(result.purged)
}

/// The ORM's flush of the deletion rows' dirty attributes.
async fn flush(
    tx: &mut PgConnection,
    pending: &mut Vec<Pending>,
    moment: DateTime<Utc>,
) -> Result<(), WorkerError> {
    for write in pending.drain(..) {
        let (column, user_id) = match write {
            Pending::PurgedAt(id) => ("purged_at", id),
            Pending::CancelledAt(id) => ("cancelled_at", id),
        };
        sqlx::query(&format!(
            "UPDATE account_deletion SET {column}=$1::TIMESTAMP WITH TIME ZONE \
             WHERE account_deletion.user_id = $2::BIGINT"
        ))
        .bind(moment)
        .bind(user_id)
        .execute(&mut *tx)
        .await?;
    }
    Ok(())
}

/// `_anonymise(session, user)`: remove the person from a row the invoice trail still points at.
async fn anonymise(
    tx: &mut PgConnection,
    user_id: i64,
    public_id: &str,
) -> Result<(), WorkerError> {
    for table in OWNED_TABLES {
        sqlx::query(&format!(
            "DELETE FROM {table} WHERE {table}.user_id = $1::BIGINT"
        ))
        .bind(user_id)
        .execute(&mut *tx)
        .await?;
    }
    sqlx::query(
        "UPDATE app_user SET email=$1::VARCHAR, password_hash=NULL, name=NULL, \
         email_verified_at=NULL WHERE app_user.id = $2::BIGINT",
    )
    .bind(format!("{public_id}@{TOMBSTONE_DOMAIN}"))
    .bind(user_id)
    .execute(&mut *tx)
    .await?;
    Ok(())
}

/// `purge_accounts_task()`'s payload: `{"purged", "considered", "notes"}` (`outcome.detail or
/// None` — the detail always holds the two notes, so it is never `None`).
#[must_use]
pub fn payload(purged: i64, outcome: &StepOutcome) -> OrderedJson {
    object([
        ("purged", int(purged)),
        ("considered", int(outcome.rows_in)),
        (
            "notes",
            if outcome.detail.is_empty() {
                OrderedJson::Null
            } else {
                outcome.detail_json()
            },
        ),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_payload_carries_both_notes_even_when_nothing_was_due() {
        let mut outcome = StepOutcome::default();
        outcome.note("purged_public_ids", OrderedJson::Null);
        outcome.note("anonymised", OrderedJson::Null);
        let text = crate::pyjson::dumps(&payload(0, &outcome)).unwrap_or_default();
        assert_eq!(
            text,
            r#"{"purged": 0, "considered": 0, "notes": {"purged_public_ids": null, "anonymised": null}}"#
        );
    }
}
