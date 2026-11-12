//! STRATEGY-BOOKS on a scratch database (TEMP tables, never `baskfy`): a screen slice, a strategy
//! book and the broker pile attribute each share once; a re-run and a T+1 rollover write nothing.
//! Set `BASKFY_RS_SCRATCH_DATABASE_URL`, run with `--ignored strategy_books`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_api::strategy_books::sync_user;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::{Connection, PgConnection};

const IGNORE: &str =
    "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored";

async fn exec(conn: &mut PgConnection, sql: &str) {
    sqlx::raw_sql(sql).execute(&mut *conn).await.unwrap();
}

async fn scratch() -> PgConnection {
    let url =
        std::env::var("BASKFY_RS_SCRATCH_DATABASE_URL").unwrap_or_else(|_| panic!("{IGNORE}"));
    let database = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        database != "baskfy" && !database.is_empty(),
        "refusing {database:?}"
    );
    let mut conn = PgConnection::connect(&url).await.unwrap();
    exec(
        &mut conn,
        "CREATE TEMP TABLE broker_account (id bigint PRIMARY KEY, user_id bigint NOT NULL, \
         broker_id varchar NOT NULL DEFAULT 'zerodha'); \
         CREATE TEMP TABLE portfolio (id bigserial PRIMARY KEY, user_id bigint NOT NULL, \
         name varchar NOT NULL, kind varchar NOT NULL, source varchar NOT NULL, \
         started_on date NOT NULL, broker_account_id bigint, \
         is_broker_pile boolean NOT NULL DEFAULT false, strategy_key text); \
         CREATE TEMP TABLE portfolio_holding (portfolio_id bigint NOT NULL, \
         instrument_id bigint NOT NULL, broker_account_id bigint NOT NULL, \
         portfolio_kind varchar NOT NULL, quantity numeric(20,4), avg_price numeric(18,4), \
         added_on date NOT NULL, first_bought_on date, \
         history_source varchar NOT NULL DEFAULT 'NONE', \
         PRIMARY KEY (portfolio_id, instrument_id, broker_account_id)); \
         CREATE TEMP TABLE tw_position (id bigserial, user_id bigint, broker_account_id bigint, \
         instrument_id bigint, entry_date date, entry_avg numeric(14,4), quantity_open integer, \
         state varchar, simulated boolean); \
         CREATE TEMP TABLE vb_position (LIKE tw_position); \
         CREATE TEMP TABLE sw_position (LIKE tw_position); \
         CREATE TEMP TABLE sw_plan_line (id bigserial, position_id bigint, note text); \
         INSERT INTO broker_account VALUES (1, 1, 'zerodha'); \
         INSERT INTO portfolio (id, user_id, name, kind, source, started_on, broker_account_id, \
         is_broker_pile) VALUES (3, 1, 'Zerodha holdings', 'CAPITAL', 'HOLDING_GROUP', \
         '2026-09-03', 1, true), (13, 1, 'AVG SHARP 12-1M', 'CAPITAL', 'MY_SCREEN', \
         '2026-11-12', NULL, false); \
         SELECT setval(pg_get_serial_sequence('pg_temp.portfolio', 'id'), 100); \
         INSERT INTO portfolio_holding VALUES \
         (3, 7, 1, 'CAPITAL', 340, 366.4, '2026-11-11', '2026-11-11', 'NONE'), \
         (13, 7, 1, 'CAPITAL', 414, 373.166, '2026-11-12', '2026-11-12', 'NONE'); \
         INSERT INTO tw_position (user_id, broker_account_id, instrument_id, entry_date, \
         entry_avg, quantity_open, state, simulated) VALUES \
         (1, 1, 7, '2026-11-09', 366.4, 340, 'OPEN', false), \
         (1, 1, 8, '2026-11-09', 1, 99, 'OPEN', true), \
         (1, 1, 9, '2026-11-01', 1, 0, 'CLOSED', false); \
         INSERT INTO sw_position (id, user_id, broker_account_id, instrument_id, entry_date, \
         entry_avg, quantity_open, state, simulated) VALUES \
         (2, 1, 1, 20, '2026-11-12', 109.0444, 2752, 'OPEN', false), \
         (5, 1, NULL, 21, '2026-11-12', 50, 10, 'OPEN', false); \
         INSERT INTO sw_plan_line (position_id, note) VALUES \
         (2, 'OPPS product=CNC src=swing setup=FLAG'), (5, 'OPPS src=twt setup=FLAG');",
    )
    .await;
    conn
}

async fn rows(conn: &mut PgConnection) -> Vec<(i64, i64, Decimal, NaiveDate, Option<NaiveDate>)> {
    sqlx::query_as(
        "SELECT portfolio_id, instrument_id, quantity, added_on, first_bought_on \
         FROM portfolio_holding ORDER BY 1, 2",
    )
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

fn day(n: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 11, n).unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn strategy_books_attribute_each_share_once_and_rerun_is_a_no_op() {
    let mut conn = scratch().await;
    let first = sync_user(&mut conn, 1).await.unwrap();
    assert_eq!(first.created, 2, "Three-weeks-tight and Swing");
    assert_eq!(first.from_pile, Decimal::from(340));
    let books: Vec<(i64, String, String)> = sqlx::query_as(
        "SELECT id, strategy_key, name FROM portfolio WHERE strategy_key IS NOT NULL ORDER BY id",
    )
    .fetch_all(&mut conn)
    .await
    .unwrap();
    let id = |k: &str| books.iter().find(|b| b.1 == k).unwrap().0;
    let (twt, swing) = (id("twt"), id("swing"));
    assert_eq!(
        books.iter().find(|b| b.1 == "twt").unwrap().2,
        "Three-weeks-tight"
    );
    // Pile row gone (moved), screen slice untouched, TWT gets its auto buy and the Tight ticket
    // line, Swing the Swing ticket; simulated and closed lines filed nowhere.
    assert_eq!(
        rows(&mut conn).await,
        vec![
            (13, 7, Decimal::from(414), day(12), Some(day(12))),
            (twt, 7, Decimal::from(340), day(9), Some(day(9))),
            (twt, 21, Decimal::from(10), day(12), Some(day(12))),
            (swing, 20, Decimal::from(2752), day(12), Some(day(12))),
        ]
    );
    // Re-run, and day two (the same lines are now broker holdings): nothing moves.
    let before = rows(&mut conn).await;
    let again = sync_user(&mut conn, 1).await.unwrap();
    assert_eq!(
        (again.inserted, again.updated, again.deleted, again.created),
        (0, 0, 0, 0)
    );
    assert_eq!(rows(&mut conn).await, before);
    // Partial exit, then close.
    exec(
        &mut conn,
        "UPDATE tw_position SET quantity_open = 170 WHERE instrument_id = 7",
    )
    .await;
    sync_user(&mut conn, 1).await.unwrap();
    let q: Decimal = sqlx::query_scalar(
        "SELECT quantity FROM portfolio_holding WHERE portfolio_id = $1 AND instrument_id = 7",
    )
    .bind(twt)
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!(q, Decimal::from(170));
    exec(
        &mut conn,
        "UPDATE tw_position SET state = 'CLOSED' WHERE instrument_id = 7",
    )
    .await;
    let closed = sync_user(&mut conn, 1).await.unwrap();
    assert_eq!(closed.deleted, 1);
    let left: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM portfolio_holding WHERE portfolio_id = $1 AND instrument_id = 7",
    )
    .bind(twt)
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!(left, 0);
}
