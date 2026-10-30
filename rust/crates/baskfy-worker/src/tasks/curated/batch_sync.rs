//! `tasks/curated_batch_sync.run_curated_batch_sync` (T8.2): promote PLANNED (and PARTIAL)
//! curated batches from the desk journal. It **reads** `desk.rebalance_orders` fills; it never
//! places, modifies or cancels anything. Synthetic `cb-sim-*` plan ids stay PLANNED.

use baskfy_db::OrderedJson;
use baskfy_db::row::opt_timestamptz;
use chrono::{DateTime, Utc};
use sqlx::Row;

use crate::error::WorkerError;
use crate::pyjson::{int, object};
use crate::steps::log_warning;
use crate::tasks::TaskContext;

/// `_SIM_PREFIX`.
const SIM_PREFIX: &str = "cb-sim-";

/// `DeskOrderFill` (the journal's `bigint` quantities, NULL read as 0).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DeskOrderFill {
    /// `planned_qty`.
    pub planned_qty: i64,
    /// `filled_qty`.
    pub filled_qty: i64,
}

/// `classify_fills(fills)`: `EXECUTED` when every planned lot filled, `PARTIAL` when any fill
/// landed, `None` (stay as is) otherwise.
#[must_use]
pub fn classify_fills(fills: &[DeskOrderFill]) -> Option<&'static str> {
    let planned: Vec<&DeskOrderFill> = fills.iter().filter(|f| f.planned_qty > 0).collect();
    if planned.is_empty() {
        return None;
    }
    if planned.iter().all(|f| f.filled_qty >= f.planned_qty) {
        return Some("EXECUTED");
    }
    if fills.iter().any(|f| f.filled_qty > 0) {
        return Some("PARTIAL");
    }
    None
}

/// `SqlDeskFillReader.fills_for_plan`: `None` for an unknown plan or a journal that cannot be
/// read (logged, as Python logs it; the transaction is then aborted, as Python's is).
async fn fills_for_plan(
    conn: &mut sqlx::PgConnection,
    plan_id: &str,
) -> Option<Vec<DeskOrderFill>> {
    if plan_id.starts_with(SIM_PREFIX) {
        return None;
    }
    let read = async {
        let exists: Option<String> = sqlx::query_scalar(
            "select version_id from \"desk\".rebalance_versions where version_id = $1",
        )
        .bind(plan_id)
        .fetch_optional(&mut *conn)
        .await?;
        if exists.is_none() {
            return Ok::<_, sqlx::Error>(None);
        }
        let rows = sqlx::query(
            "select planned_qty, filled_qty from \"desk\".rebalance_orders where version_id = $1",
        )
        .bind(plan_id)
        .fetch_all(&mut *conn)
        .await?;
        let mut out = Vec::with_capacity(rows.len());
        for r in &rows {
            out.push(DeskOrderFill {
                planned_qty: r.try_get::<Option<i64>, _>("planned_qty")?.unwrap_or(0),
                filled_qty: r.try_get::<Option<i64>, _>("filled_qty")?.unwrap_or(0),
            });
        }
        Ok(Some(out))
    };
    match read.await {
        Ok(found) => found,
        Err(_) => {
            log_warning(&format!("desk journal unavailable for plan {plan_id}"));
            None
        }
    }
}

/// `run_curated_batch_sync(session)`, in one transaction. The UPDATEs go out at the end (the
/// ORM's flush), and only for a row whose value changed.
///
/// # Errors
/// A database failure (rolled back).
pub async fn run_batch_sync(
    ctx: &TaskContext,
    moment: DateTime<Utc>,
) -> Result<OrderedJson, WorkerError> {
    let mut tx = ctx.db.begin().await?;
    let batches = sqlx::query(
        "SELECT cb_order_batch.id, cb_order_batch.user_id, cb_order_batch.broker_account_id, \
         cb_order_batch.investment_id, cb_order_batch.kind, cb_order_batch.requested_amount, \
         cb_order_batch.desk_plan_id, cb_order_batch.status, cb_order_batch.fee_entry_id, \
         cb_order_batch.created_at, cb_order_batch.executed_at \nFROM cb_order_batch \nWHERE \
         cb_order_batch.status IN ($1::VARCHAR, $2::VARCHAR) AND cb_order_batch.desk_plan_id IS \
         NOT NULL",
    )
    .bind("PLANNED")
    .bind("PARTIAL")
    .fetch_all(&mut *tx)
    .await?;
    let (mut executed, mut partial, mut skipped) = (0_i64, 0_i64, 0_i64);
    // (id, new status, new executed_at)
    let mut dirty: Vec<(i64, &'static str, Option<DateTime<Utc>>)> = Vec::new();
    for r in &batches {
        let id: i64 = r.try_get("id")?;
        let status: String = r.try_get("status")?;
        let executed_at = opt_timestamptz(r, "executed_at")?;
        let plan_id: Option<String> = r.try_get("desk_plan_id")?;
        let Some(plan_id) = plan_id.filter(|p| !p.starts_with(SIM_PREFIX)) else {
            skipped += 1;
            continue;
        };
        let Some(fills) = fills_for_plan(&mut tx, &plan_id).await else {
            skipped += 1;
            continue;
        };
        match classify_fills(&fills) {
            None => skipped += 1,
            Some("EXECUTED") => {
                if status != "EXECUTED" || executed_at != Some(moment) {
                    dirty.push((id, "EXECUTED", Some(moment)));
                }
                executed += 1;
            }
            Some(_) => {
                if status != "PARTIAL" {
                    dirty.push((id, "PARTIAL", executed_at));
                }
                partial += 1;
            }
        }
    }
    for (id, status, executed_at) in dirty {
        if status == "EXECUTED" {
            sqlx::query(
                "UPDATE cb_order_batch SET status=$1::VARCHAR, executed_at=$2::TIMESTAMP WITH \
                 TIME ZONE WHERE cb_order_batch.id = $3::BIGINT",
            )
            .bind(status)
            .bind(executed_at)
            .bind(id)
            .execute(&mut *tx)
            .await?;
        } else {
            sqlx::query(
                "UPDATE cb_order_batch SET status=$1::VARCHAR WHERE cb_order_batch.id = $2::BIGINT",
            )
            .bind(status)
            .bind(id)
            .execute(&mut *tx)
            .await?;
        }
    }
    tx.commit().await?;
    Ok(object([
        (
            "examined",
            int(i64::try_from(batches.len()).unwrap_or(i64::MAX)),
        ),
        ("executed", int(executed)),
        ("partial", int(partial)),
        ("skipped", int(skipped)),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fill(planned: i64, filled: i64) -> DeskOrderFill {
        DeskOrderFill {
            planned_qty: planned,
            filled_qty: filled,
        }
    }

    // test_curated_batch_sync.py::test_classify_settled_partial_and_untouched
    #[test]
    fn classify_settled_partial_and_untouched() {
        assert_eq!(
            classify_fills(&[fill(10, 10), fill(5, 5)]),
            Some("EXECUTED")
        );
        assert_eq!(classify_fills(&[fill(10, 4), fill(5, 0)]), Some("PARTIAL"));
        assert_eq!(classify_fills(&[fill(10, 0)]), None);
        assert_eq!(classify_fills(&[]), None);
        // A fill on an unplanned row still makes a planned batch PARTIAL.
        assert_eq!(classify_fills(&[fill(10, 0), fill(0, 3)]), Some("PARTIAL"));
    }
}
