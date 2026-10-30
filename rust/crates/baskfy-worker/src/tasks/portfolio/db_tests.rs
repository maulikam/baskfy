//! `services/worker/tests/test_portfolio_nav_job.py`, ported — the database half (the pure
//! arithmetic is in `ledger`'s own tests; the wiring tests are `tests/task_registry.rs`'s).
//!
//! **Where they run.** On one connection to a *scratch* database, inside `TEMP` tables shaped
//! like the production ones (the columns the job reads and writes, their numeric scales, and
//! migration 0023's `NULLS NOT DISTINCT` unique index) — never the bed's `baskfy`. Set
//! `BASKFY_RS_SCRATCH_DATABASE_URL` and run `cargo test -p baskfy-worker -- --ignored
//! portfolio`; without it they are reported `ignored`. A URL naming `baskfy` is refused. The
//! task-level grade against Python on a real clone is `task_diff.py --cases
//! tools/parity/task_cases/portfolio.json`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::{Connection, PgConnection, Row};

use super::nav::{NavRow, UserNav, run_portfolio_nav};

const IGNORE: &str =
    "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored";

/// `VALUATION_DATE` and `EARLIER`.
fn valuation() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 14).unwrap()
}
fn earlier() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 13).unwrap()
}

fn dec(s: &str) -> Decimal {
    s.parse().unwrap()
}

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
        "refusing the database {database:?}: these tests write temp tables"
    );
    let mut conn = PgConnection::connect(&url).await.unwrap();
    exec(
        &mut conn,
        "CREATE TEMP TABLE ohlcv_daily (instrument_id bigint NOT NULL, date date NOT NULL, \
         close_raw numeric(18,4) NOT NULL, PRIMARY KEY (instrument_id, date)); \
         CREATE TEMP TABLE portfolio (id bigserial PRIMARY KEY, user_id bigint NOT NULL, \
         name varchar NOT NULL, kind varchar NOT NULL DEFAULT 'CAPITAL', \
         source varchar NOT NULL DEFAULT 'HOLDING_GROUP', started_on date NOT NULL); \
         CREATE TEMP TABLE broker_account (id bigserial PRIMARY KEY, user_id bigint NOT NULL, \
         broker_id varchar NOT NULL DEFAULT 'zerodha', label varchar NOT NULL DEFAULT 'primary'); \
         CREATE TEMP TABLE portfolio_holding (portfolio_id bigint NOT NULL, \
         instrument_id bigint NOT NULL, quantity numeric(20,4), added_on date NOT NULL, \
         broker_account_id bigint NOT NULL, first_bought_on date, \
         history_source varchar NOT NULL DEFAULT 'NONE', \
         PRIMARY KEY (portfolio_id, instrument_id, broker_account_id)); \
         CREATE TEMP TABLE portfolio_cash_flow (id bigserial PRIMARY KEY, \
         broker_account_id bigint NOT NULL, portfolio_id bigint, kind varchar NOT NULL, \
         amount numeric(20,2) NOT NULL, occurred_on date NOT NULL, instrument_id bigint, \
         quantity numeric(20,4)); \
         CREATE TEMP TABLE reconciliation_item (id bigserial PRIMARY KEY, user_id bigint NOT NULL, \
         instrument_id bigint NOT NULL, broker_account_id bigint NOT NULL, \
         state varchar NOT NULL, detected_on date NOT NULL); \
         CREATE TEMP TABLE portfolio_nav_daily (portfolio_id bigint, user_id bigint NOT NULL, \
         date date NOT NULL, market_value numeric(20,2) NOT NULL, cash numeric(20,2) NOT NULL, \
         net_flow numeric(20,2) NOT NULL, pending_reconciliation boolean NOT NULL); \
         CREATE UNIQUE INDEX ON portfolio_nav_daily (user_id, date, portfolio_id) \
         NULLS NOT DISTINCT;",
    )
    .await;
    conn
}

async fn scalar_id(conn: &mut PgConnection, sql: &str) -> i64 {
    sqlx::query_scalar(sql).fetch_one(&mut *conn).await.unwrap()
}

async fn broker(conn: &mut PgConnection, user: i64) -> i64 {
    scalar_id(
        conn,
        &format!("INSERT INTO broker_account (user_id) VALUES ({user}) RETURNING id"),
    )
    .await
}

async fn portfolio(conn: &mut PgConnection, user: i64, name: &str, kind: &str) -> i64 {
    scalar_id(
        conn,
        &format!(
            "INSERT INTO portfolio (user_id, name, kind, started_on) \
             VALUES ({user}, '{name}', '{kind}', '2026-08-13') RETURNING id"
        ),
    )
    .await
}

async fn bar(conn: &mut PgConnection, instrument: i64, on: NaiveDate, close: &str) {
    exec(
        conn,
        &format!("INSERT INTO ohlcv_daily VALUES ({instrument}, '{on}', {close})"),
    )
    .await;
}

async fn holding(
    conn: &mut PgConnection,
    portfolio: i64,
    instrument: i64,
    broker: i64,
    quantity: &str,
) {
    exec(
        conn,
        &format!(
            "INSERT INTO portfolio_holding (portfolio_id, instrument_id, broker_account_id, \
             quantity, added_on) VALUES ({portfolio}, {instrument}, {broker}, {quantity}, \
             '2026-08-13')"
        ),
    )
    .await;
}

async fn flow(
    conn: &mut PgConnection,
    broker: i64,
    kind: &str,
    amount: &str,
    on: NaiveDate,
    portfolio: Option<i64>,
    trade: Option<(i64, &str)>,
) {
    let portfolio = portfolio.map_or("NULL".to_owned(), |p| p.to_string());
    let (instrument, quantity) = trade.map_or(("NULL".to_owned(), "NULL".to_owned()), |(i, q)| {
        (i.to_string(), q.to_owned())
    });
    exec(
        conn,
        &format!(
            "INSERT INTO portfolio_cash_flow (broker_account_id, portfolio_id, kind, amount, \
             occurred_on, instrument_id, quantity) VALUES ({broker}, {portfolio}, '{kind}', \
             {amount}, '{on}', {instrument}, {quantity})"
        ),
    )
    .await;
}

async fn open_item(
    conn: &mut PgConnection,
    user: i64,
    instrument: i64,
    broker: i64,
    on: NaiveDate,
) {
    exec(
        conn,
        &format!(
            "INSERT INTO reconciliation_item (user_id, instrument_id, broker_account_id, state, \
             detected_on) VALUES ({user}, {instrument}, {broker}, 'OPEN', '{on}')"
        ),
    )
    .await;
}

async fn stored(
    conn: &mut PgConnection,
    user: i64,
    on: NaiveDate,
) -> Vec<(Option<i64>, Decimal, Decimal, bool)> {
    let rows = sqlx::query(
        "SELECT portfolio_id, market_value, cash, pending_reconciliation FROM portfolio_nav_daily \
         WHERE user_id = $1 AND date = $2 ORDER BY portfolio_id NULLS FIRST",
    )
    .bind(user)
    .bind(on)
    .fetch_all(&mut *conn)
    .await
    .unwrap();
    rows.iter()
        .map(|r| {
            (
                r.get("portfolio_id"),
                r.get("market_value"),
                r.get("cash"),
                r.get("pending_reconciliation"),
            )
        })
        .collect()
}

fn row_for(user: &UserNav, portfolio: i64) -> &NavRow {
    user.portfolio_rows
        .iter()
        .find(|r| r.portfolio_id == Some(portfolio))
        .unwrap_or_else(|| panic!("no NAV row was produced for portfolio {portfolio}"))
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn consolidated_equals_the_sum_of_its_parts_to_the_paisa() {
    let mut c = scratch().await;
    let b = broker(&mut c, 1).await;
    let momentum = portfolio(&mut c, 1, "Momentum", "CAPITAL").await;
    let longterm = portfolio(&mut c, 1, "Long term", "CAPITAL").await;
    bar(&mut c, 101, valuation(), "10.5050").await;
    bar(&mut c, 102, valuation(), "20.0350").await;
    bar(&mut c, 103, valuation(), "30.1050").await;
    holding(&mut c, momentum, 101, b, "3").await;
    holding(&mut c, momentum, 102, b, "7").await;
    holding(&mut c, longterm, 103, b, "9").await;
    flow(
        &mut c,
        b,
        "EXTERNAL_DEPOSIT",
        "5000.00",
        earlier(),
        None,
        None,
    )
    .await;
    flow(
        &mut c,
        b,
        "ASSIGN",
        "1000.00",
        valuation(),
        Some(momentum),
        None,
    )
    .await;

    let result = run_portfolio_nav(&mut c, valuation()).await.unwrap();
    assert!(result.skipped_reason.is_none());
    assert_eq!(result.users.len(), 1);
    let user = &result.users[0];
    assert_eq!(row_for(user, momentum).market_value, dec("171.77"));
    assert_eq!(row_for(user, longterm).market_value, dec("270.95"));
    let mut parts = user.unallocated_market_value + user.unallocated_cash;
    for row in &user.portfolio_rows {
        parts += row.total_value().unwrap();
    }
    assert_eq!(user.consolidated.total_value().unwrap(), parts);
    assert_eq!(
        user.consolidated.total_value().unwrap().to_string(),
        "5442.72"
    );

    let rows = stored(&mut c, 1, valuation()).await;
    let ids: Vec<Option<i64>> = rows.iter().map(|r| r.0).collect();
    assert_eq!(ids, vec![None, Some(momentum), Some(longterm)]);
    let json = result.as_json().unwrap();
    let text = crate::pyjson::dumps(&json).unwrap();
    assert_eq!(
        text,
        "{\"as_of\": \"2026-08-14\", \"users\": 1, \"rows_written\": 3, \"rows_removed\": 0, \
         \"marks_written\": 3, \"pending_reconciliation\": 0, \"net_worth\": {\"1\": \
         \"5442.72\"}, \"skipped_reason\": null}"
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn net_flow_divides_the_day_for_the_right_pot() {
    let mut c = scratch().await;
    let b = broker(&mut c, 1).await;
    let momentum = portfolio(&mut c, 1, "Momentum", "CAPITAL").await;
    bar(&mut c, 110, valuation(), "100.0000").await;
    holding(&mut c, momentum, 110, b, "5").await;
    flow(
        &mut c,
        b,
        "EXTERNAL_DEPOSIT",
        "9000.00",
        valuation(),
        None,
        None,
    )
    .await;
    flow(
        &mut c,
        b,
        "ASSIGN",
        "2000.00",
        valuation(),
        Some(momentum),
        None,
    )
    .await;
    flow(
        &mut c,
        b,
        "RELEASE",
        "500.00",
        valuation(),
        Some(momentum),
        None,
    )
    .await;
    flow(
        &mut c,
        b,
        "BUY",
        "400.00",
        valuation(),
        Some(momentum),
        Some((110, "4")),
    )
    .await;
    flow(
        &mut c,
        b,
        "ASSIGN",
        "700.00",
        earlier(),
        Some(momentum),
        None,
    )
    .await;

    let result = run_portfolio_nav(&mut c, valuation()).await.unwrap();
    let user = &result.users[0];
    assert_eq!(row_for(user, momentum).net_flow, dec("1500.00"));
    assert_eq!(user.consolidated.net_flow, dec("9000.00"));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_monitoring_view_gets_no_row_and_cash_into_one_is_refused() {
    let mut c = scratch().await;
    let b = broker(&mut c, 1).await;
    let capital = portfolio(&mut c, 1, "Core", "CAPITAL").await;
    let lens = portfolio(&mut c, 1, "Watching", "MONITORING").await;
    bar(&mut c, 120, valuation(), "50.0000").await;
    holding(&mut c, capital, 120, b, "10").await;
    holding(&mut c, lens, 120, b, "10").await;

    let result = run_portfolio_nav(&mut c, valuation()).await.unwrap();
    let user = &result.users[0];
    assert!(
        user.portfolio_rows
            .iter()
            .all(|r| r.portfolio_id != Some(lens))
    );
    assert_eq!(user.consolidated.market_value, dec("500.00"));

    flow(&mut c, b, "ASSIGN", "10.00", valuation(), Some(lens), None).await;
    let err = run_portfolio_nav(&mut c, valuation()).await.unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert!(
        err.to_string()
            .starts_with("'Watching' is a monitoring view and holds no cash")
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn an_open_item_freezes_its_holding_and_a_later_one_does_not_poison_the_date() {
    let mut c = scratch().await;
    let b = broker(&mut c, 1).await;
    let core = portfolio(&mut c, 1, "Core", "CAPITAL").await;
    bar(&mut c, 130, valuation(), "10.0000").await;
    bar(&mut c, 131, valuation(), "20.0000").await;
    holding(&mut c, core, 130, b, "1").await;
    holding(&mut c, core, 131, b, "1").await;
    let later = NaiveDate::from_ymd_opt(2026, 8, 20).unwrap();
    open_item(&mut c, 1, 130, b, later).await;
    let user = run_portfolio_nav(&mut c, valuation())
        .await
        .unwrap()
        .users
        .remove(0);
    assert!(!row_for(&user, core).pending_reconciliation);
    assert_eq!(row_for(&user, core).market_value, dec("30.00"));

    open_item(&mut c, 1, 130, b, valuation()).await;
    let user = run_portfolio_nav(&mut c, valuation())
        .await
        .unwrap()
        .users
        .remove(0);
    assert!(row_for(&user, core).pending_reconciliation);
    assert!(user.consolidated.pending_reconciliation);
    assert_eq!(row_for(&user, core).market_value, dec("20.00"));
    assert_eq!(user.frozen_holdings, 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn rerunning_converges_and_a_lens_loses_its_row() {
    let mut c = scratch().await;
    let b = broker(&mut c, 1).await;
    let core = portfolio(&mut c, 1, "Core", "CAPITAL").await;
    let other = portfolio(&mut c, 1, "Other", "CAPITAL").await;
    bar(&mut c, 140, valuation(), "10.0000").await;
    holding(&mut c, core, 140, b, "3").await;
    run_portfolio_nav(&mut c, valuation()).await.unwrap();
    let first = stored(&mut c, 1, valuation()).await;
    let again = run_portfolio_nav(&mut c, valuation()).await.unwrap();
    assert_eq!(again.marks_written, 0);
    assert_eq!(stored(&mut c, 1, valuation()).await, first);
    assert_eq!(first.len(), 3);

    exec(
        &mut c,
        &format!("UPDATE portfolio SET kind = 'MONITORING' WHERE id = {other}"),
    )
    .await;
    let pruned = run_portfolio_nav(&mut c, valuation()).await.unwrap();
    assert_eq!(pruned.rows_removed, 1);
    let ids: Vec<Option<i64>> = stored(&mut c, 1, valuation())
        .await
        .iter()
        .map(|r| r.0)
        .collect();
    assert_eq!(ids, vec![None, Some(core)]);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn no_bars_no_rows_unpriced_is_flagged_and_illiquid_is_carried() {
    let mut c = scratch().await;
    let b = broker(&mut c, 1).await;
    let core = portfolio(&mut c, 1, "Core", "CAPITAL").await;
    holding(&mut c, core, 150, b, "2").await;
    holding(&mut c, core, 151, b, "2").await;
    bar(&mut c, 150, earlier(), "7.0000").await;

    let skipped = run_portfolio_nav(&mut c, valuation()).await.unwrap();
    assert_eq!(
        skipped.skipped_reason.as_deref(),
        Some("no bars on 2026-08-14; the market printed no close")
    );
    assert!(stored(&mut c, 1, valuation()).await.is_empty());

    bar(&mut c, 999, valuation(), "1.0000").await;
    let user = run_portfolio_nav(&mut c, valuation())
        .await
        .unwrap()
        .users
        .remove(0);
    assert_eq!(row_for(&user, core).market_value, dec("14.00"));
    assert!(row_for(&user, core).pending_reconciliation);
    assert_eq!(user.unpriced_holdings, 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn the_grouping_date_is_stamped_only_on_undated_none_rows_grouped_by_then() {
    let mut c = scratch().await;
    let b = broker(&mut c, 1).await;
    let core = portfolio(&mut c, 1, "Core", "CAPITAL").await;
    bar(&mut c, 160, valuation(), "1.0000").await;
    holding(&mut c, core, 160, b, "1").await;
    holding(&mut c, core, 161, b, "1").await;
    holding(&mut c, core, 162, b, "1").await;
    exec(
        &mut c,
        "UPDATE portfolio_holding SET first_bought_on = '2020-01-01', history_source = 'CAS' \
         WHERE instrument_id = 161; \
         UPDATE portfolio_holding SET added_on = '2026-09-01' WHERE instrument_id = 162;",
    )
    .await;
    let result = run_portfolio_nav(&mut c, valuation()).await.unwrap();
    assert_eq!(result.marks_written, 1);
    let rows = sqlx::query(
        "SELECT instrument_id, first_bought_on, history_source FROM portfolio_holding \
         ORDER BY instrument_id",
    )
    .fetch_all(&mut c)
    .await
    .unwrap();
    let got: Vec<(i64, Option<NaiveDate>, String)> = rows
        .iter()
        .map(|r| (r.get(0), r.get(1), r.get(2)))
        .collect();
    assert_eq!(
        got,
        vec![
            (160, Some(earlier()), "NONE".to_owned()),
            (161, NaiveDate::from_ymd_opt(2020, 1, 1), "CAS".to_owned()),
            (162, None, "NONE".to_owned()),
        ]
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_user_with_nothing_gets_no_row_and_each_user_gets_their_own() {
    let mut c = scratch().await;
    bar(&mut c, 170, valuation(), "2.0000").await;
    let b1 = broker(&mut c, 1).await;
    let p1 = portfolio(&mut c, 1, "One", "CAPITAL").await;
    holding(&mut c, p1, 170, b1, "1").await;
    let b2 = broker(&mut c, 2).await;
    let p2 = portfolio(&mut c, 2, "Two", "CAPITAL").await;
    holding(&mut c, p2, 170, b2, "3").await;
    // User 3 has a broker account and nothing else: no portfolio, no flow — no row.
    broker(&mut c, 3).await;
    let result = run_portfolio_nav(&mut c, valuation()).await.unwrap();
    let worth: Vec<(i64, String)> = result
        .users
        .iter()
        .map(|u| (u.user_id, u.consolidated.total_value().unwrap().to_string()))
        .collect();
    assert_eq!(worth, vec![(1, "2.00".to_owned()), (2, "6.00".to_owned())]);
    assert!(stored(&mut c, 3, valuation()).await.is_empty());
}
