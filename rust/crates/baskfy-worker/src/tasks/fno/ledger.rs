//! Ports the readers of `baskfy_worker.fno.ledger` that the worker's tasks use: the journal
//! (`load_trades`), the F1 lifts (`load_lifts`), each open position's latest mark,
//! and the ledger and `04` §7 pauses per mode (never pooled, DECISIONS-FO FO10.1). Read-only.
//!
//! **Not ported here** (DECISIONS-RS R8.fno): `read_checklist` and `lift_f1_pause` — the API's
//! FO routes are their only callers; no task reaches them.

use std::collections::BTreeMap;

use baskfy_core::fno::config::{FnoCeilings, FnoConfig, FoSleeve};
use baskfy_core::fno::ledger::{
    ClosedTrade, FoPause, Ledger, OpenMark, build_ledger, evaluate_pauses,
};
use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::fno_err;
use crate::error::WorkerError;

/// `ENTRIES_SIMULATED`: every FO entry is paper while FO7.1 stands.
pub(crate) const ENTRIES_SIMULATED: bool = true;

/// `FoSleeve(value)`.
pub(crate) fn sleeve_of(value: &str) -> Result<FoSleeve, WorkerError> {
    FoSleeve::ALL
        .into_iter()
        .find(|s| s.value() == value)
        .ok_or_else(|| {
            WorkerError::python(
                "ValueError",
                format!(
                    "{} is not a valid FoSleeve",
                    baskfy_providers::py::repr_str(value)
                ),
            )
        })
}

type TradeRow = (
    i64,
    String,
    String,
    bool,
    NaiveDate,
    Option<DateTime<Utc>>,
    Decimal,
    Decimal,
    String,
    i16,
);

/// `load_trades`: every `fo_journal` row of the user, both modes, each with its `simulated`.
pub(crate) async fn load_trades(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<ClosedTrade>, WorkerError> {
    let rows: Vec<TradeRow> = sqlx::query_as(
        "SELECT j.position_id, j.sleeve::text, j.symbol, j.simulated, j.closed_on, p.closed_at, \
         j.net_pnl_inr, j.r_multiple, j.closed_reason, j.rolls FROM fo_journal j \
         JOIN fo_position p ON p.id = j.position_id WHERE j.user_id = $1 \
         ORDER BY j.closed_on, j.position_id",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.into_iter()
        .map(|r| {
            Ok(ClosedTrade {
                position_id: r.0,
                sleeve: sleeve_of(&r.1)?,
                symbol: r.2,
                simulated: r.3,
                closed_on: r.4,
                closed_at: r.5.map(|t| t.naive_utc()),
                net_pnl_inr: r.6,
                r_multiple: r.7,
                closed_reason: r.8,
                rolls: i64::from(r.9),
            })
        })
        .collect()
}

/// `load_lifts`: the latest lift of each F1 underlying's pause (`fo_config_audit`, FO10.2), as
/// UTC wall clock.
pub(crate) async fn load_lifts(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<BTreeMap<FoSleeve, NaiveDateTime>, WorkerError> {
    let rows: Vec<(String, Option<DateTime<Utc>>)> = sqlx::query_as(
        "SELECT key, max(changed_at) FROM fo_config_audit WHERE user_id = $1 \
         AND key = ANY($2) GROUP BY key",
    )
    .bind(user_id)
    .bind(vec!["lift:F1N".to_owned(), "lift:F1B".to_owned()])
    .fetch_all(&mut *conn)
    .await?;
    let mut out = BTreeMap::new();
    for (key, at) in rows {
        let sleeve = if key == "lift:F1N" {
            FoSleeve::F1N
        } else {
            FoSleeve::F1B
        };
        if let Some(at) = at {
            out.insert(sleeve, at.naive_utc());
        }
    }
    Ok(out)
}

type MarkRow = (
    i64,
    String,
    String,
    bool,
    Option<Decimal>,
    Option<NaiveDate>,
    Option<Decimal>,
);

/// `load_open_marks`: each open position's latest `fo_mark` (one not yet marked carries ₹0).
pub(crate) async fn load_open_marks(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<OpenMark>, WorkerError> {
    let rows: Vec<MarkRow> = sqlx::query_as(
        "SELECT p.id, p.sleeve::text, p.symbol, p.simulated, p.max_loss_inr, m.trade_date, \
         m.pnl_inr FROM fo_position p \
         LEFT OUTER JOIN (SELECT position_id, max(trade_date) AS day FROM fo_mark \
         WHERE user_id = $1 GROUP BY position_id) latest ON latest.position_id = p.id \
         LEFT OUTER JOIN fo_mark m ON m.position_id = p.id AND m.trade_date = latest.day \
         WHERE p.user_id = $1 AND p.closed_at IS NULL ORDER BY p.id",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.into_iter()
        .map(|r| {
            Ok(OpenMark {
                position_id: r.0,
                sleeve: sleeve_of(&r.1)?,
                symbol: r.2,
                simulated: r.3,
                trade_date: r.5,
                pnl_inr: r.6.unwrap_or(Decimal::ZERO),
                r_inr: r.4,
            })
        })
        .collect()
}

/// `fo_book_config.monthly_pause_inr`, or `Decimal(0)` with no row.
pub(crate) async fn monthly_pause_inr(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Decimal, WorkerError> {
    let row: Option<(Decimal,)> =
        sqlx::query_as("SELECT monthly_pause_inr FROM fo_book_config WHERE user_id = $1")
            .bind(user_id)
            .fetch_optional(&mut *conn)
            .await?;
    Ok(row.map_or(Decimal::ZERO, |(v,)| v))
}

/// `read_ledger`: per sleeve and underlying and per book, per `simulated`.
pub(crate) async fn read_ledger(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<Ledger, WorkerError> {
    let trades = load_trades(conn, user_id).await?;
    let marks = load_open_marks(conn, user_id).await?;
    build_ledger(&trades, &marks, as_of).map_err(fno_err)
}

/// `read_pauses`: `04` §7 for one mode on `as_of`.
pub(crate) async fn read_pauses(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
    simulated: bool,
) -> Result<Vec<FoPause>, WorkerError> {
    let monthly = monthly_pause_inr(conn, user_id).await?;
    let trades = load_trades(conn, user_id).await?;
    let lifts = load_lifts(conn, user_id).await?;
    evaluate_pauses(
        &trades,
        simulated,
        as_of,
        monthly,
        Some(&lifts),
        &FnoConfig::default(),
        &FnoCeilings::default(),
    )
    .map_err(fno_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sleeves_parse_by_value_and_refuse_anything_else() {
        assert_eq!(sleeve_of("F3B").ok(), Some(FoSleeve::F3B));
        let err = sleeve_of("F9").err().map(|e| e.to_string());
        assert_eq!(err.as_deref(), Some("'F9' is not a valid FoSleeve"));
    }
}
