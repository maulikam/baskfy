//! Ports `baskfy_worker.fno.nightly`: one night of the FO data layer — the bhavcopy, then the
//! ban list NSE published for the session after it, then `fo_underlying_daily` and the scan
//! (in a savepoint: a scan that raises is reported, and the night's ingest is kept).

use baskfy_db::OrderedJson;
use chrono::{NaiveDate, NaiveDateTime};
use sqlx::PgConnection;

use super::common::iso;
use super::ingest::{
    FoBhavcopyReader, STATUS_INGESTED, ingest_day, is_final_attempt, store_ban_list,
};
use super::scan::run_scan;
use super::underlying::derive_for_night;
use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::ops::is_trading_day;
use crate::pyjson::{self, object, s};
use crate::steps::log_error;

/// `next_session`: the next NSE session after `after`, from `trading_day`.
pub(crate) async fn next_session(
    conn: &mut PgConnection,
    after: NaiveDate,
) -> Result<Option<NaiveDate>, WorkerError> {
    let row: Option<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date > $2 ORDER BY date LIMIT 1",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(after)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|(d,)| d))
}

/// `run_night`: ingest `trade_date` and store the next session's ban list. Never raises for a
/// provider refusal; `now_wall` is the task's moment as written (its zone dropped, as
/// `is_final_attempt` reads it).
pub(crate) async fn run_night<R: FoBhavcopyReader>(
    conn: &mut PgConnection,
    reader: &R,
    trade_date: NaiveDate,
    now_wall: NaiveDateTime,
) -> Result<OrderedJson, WorkerError> {
    if !is_trading_day(conn, trade_date).await? {
        return Ok(object([
            ("trade_date", s(iso(trade_date))),
            ("skipped", s("not an NSE trading day")),
        ]));
    }
    let ingest = ingest_day(
        conn,
        reader,
        trade_date,
        is_final_attempt(trade_date, now_wall),
    )
    .await?;
    let mut out = vec![
        ("trade_date".to_owned(), s(iso(trade_date))),
        ("ingest".to_owned(), ingest.as_dict()),
    ];
    let ban = match next_session(conn, trade_date).await? {
        None => object([(
            "skipped",
            s("the trading_day calendar has no session after this one"),
        )]),
        Some(following) => store_ban_list(conn, reader, trade_date, following)
            .await?
            .as_dict(),
    };
    pyjson::update(&mut out, "ban_list", ban);
    let ingested = ingest.status == STATUS_INGESTED;
    let underlying = if ingested {
        OrderedJson::Int(derive_for_night(conn, trade_date).await?)
    } else {
        OrderedJson::Null
    };
    pyjson::update(&mut out, "underlying_rows", underlying);
    if ingested {
        let scan = scan_in_savepoint(conn, trade_date).await?;
        pyjson::update(&mut out, "scan", scan);
    }
    Ok(OrderedJson::Object(out))
}

/// `scan_in_savepoint`: `run_scan` inside a savepoint; its failure is logged and returned as
/// `{"error": "<Class>: <message>"}`, never swallowed, and the ingest is kept.
pub(crate) async fn scan_in_savepoint(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<OrderedJson, WorkerError> {
    sqlx::query("SAVEPOINT fno_scan")
        .execute(&mut *conn)
        .await?;
    match run_scan(conn, trade_date, None).await {
        Ok(out) => {
            sqlx::query("RELEASE SAVEPOINT fno_scan")
                .execute(&mut *conn)
                .await?;
            Ok(out)
        }
        Err(e) => {
            sqlx::query("ROLLBACK TO SAVEPOINT fno_scan")
                .execute(&mut *conn)
                .await?;
            log_error(&format!(
                "fo_scan for {} failed; the ingest is kept: {e}",
                iso(trade_date)
            ));
            Ok(object([("error", s(format!("{}: {e}", e.python_type())))]))
        }
    }
}
