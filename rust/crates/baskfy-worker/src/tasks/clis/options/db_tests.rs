//! `test_options_schema.py`'s seed tests and the CLI's gates, on an `rst_*` clone of the bed.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use chrono::{DateTime, NaiveTime};
use rust_decimal::Decimal;
use sqlx::{Connection as _, PgConnection};

use super::seed::{EVENT_DAYS, seed_options};
use super::{collect_refusal, plan_gate};
use crate::tasks::clis::cli::context;

const ENV: &str = "BASKFY_RS_CLIS_DB_URL";

pub(crate) async fn clone() -> PgConnection {
    let url = std::env::var(ENV).unwrap_or_else(|_| panic!("needs {ENV} (an rst_* clone)"));
    let name = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        name.starts_with("rst_"),
        "refusing {name:?}: only rst_* clones"
    );
    PgConnection::connect(&url).await.unwrap()
}

pub(crate) async fn fresh_user(conn: &mut PgConnection, tag: &str) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO app_user (public_id, email, name) VALUES ($1, $2, 'CLI') RETURNING id",
    )
    .bind(format!("clis{tag}"))
    .bind(format!("clis-{tag}@example.com"))
    .fetch_one(&mut *conn)
    .await
    .unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn seeding_twice_never_resets_a_chosen_number() {
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let user = fresh_user(&mut tx, "opseed").await;
    let first = seed_options(&mut tx, user).await.unwrap();
    let counts: Vec<i64> = first
        .iter()
        .map(|(_, v)| match v {
            baskfy_db::OrderedJson::Int(n) => *n,
            _ => -1,
        })
        .collect();
    assert_eq!(counts, [i64::try_from(EVENT_DAYS.len()).unwrap(), 1, 4]);
    sqlx::query("UPDATE op_sleeve_config SET max_lots = 1 WHERE user_id = $1 AND sleeve = 'O2'")
        .bind(user)
        .execute(&mut *tx)
        .await
        .unwrap();
    let second = seed_options(&mut tx, user).await.unwrap();
    assert!(
        second
            .iter()
            .all(|(_, v)| *v == baskfy_db::OrderedJson::Int(0))
    );
    let lots: i16 = sqlx::query_scalar(
        "SELECT max_lots FROM op_sleeve_config WHERE user_id = $1 AND sleeve = 'O2'",
    )
    .bind(user)
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    assert_eq!(lots, 1);
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn the_seed_writes_the_documents_defaults_and_zero_capital() {
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let user = fresh_user(&mut tx, "opdefaults").await;
    seed_options(&mut tx, user).await.unwrap();
    let rows: Vec<(String, Decimal, Decimal, i16, NaiveTime, bool)> = sqlx::query_as(
        "SELECT sleeve, sleeve_capital_inr, risk_per_trade_pct, max_lots, hard_exit_time, \
         paper_enabled FROM op_sleeve_config WHERE user_id = $1 ORDER BY sleeve",
    )
    .bind(user)
    .fetch_all(&mut *tx)
    .await
    .unwrap();
    let names: Vec<&str> = rows.iter().map(|r| r.0.as_str()).collect();
    assert_eq!(names, ["O1M", "O1W", "O2", "O3"]);
    for row in &rows {
        assert_eq!(row.1, Decimal::ZERO, "PACK.6: no agent writes a capital");
        assert!(row.4 <= NaiveTime::from_hms_opt(15, 0, 0).unwrap());
        assert!(row.5);
    }
    assert_eq!((rows[0].2, rows[0].3), (Decimal::new(100, 2), 3));
    assert_eq!(rows[3].4, NaiveTime::from_hms_opt(14, 45, 0).unwrap());
    let book: (String, Decimal) =
        sqlx::query_as("SELECT underlying, account_inr FROM op_book_config WHERE user_id = $1")
            .bind(user)
            .fetch_one(&mut *tx)
            .await
            .unwrap();
    assert_eq!(book, ("NIFTY".to_owned(), Decimal::ZERO));
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn collect_once_refuses_outside_the_session_before_any_kite_call() {
    require_clone_env();
    let ctx = context().unwrap();
    let mut tx = ctx.db.begin().await.unwrap();
    let night = DateTime::parse_from_rfc3339("2026-09-24T20:00:00+05:30").unwrap();
    let why = collect_refusal(&ctx, &mut tx, night).await.unwrap();
    assert_eq!(why, Some("outside 09:15-15:30 IST"));
    let sunday = DateTime::parse_from_rfc3339("2026-09-27T10:00:00+05:30").unwrap();
    let why = collect_refusal(&ctx, &mut tx, sunday).await.unwrap();
    assert_eq!(why, Some("not an NSE trading day"));
    tx.rollback().await.unwrap();
    ctx.db.close().await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn a_plan_on_a_holiday_is_skipped_before_the_builder() {
    require_clone_env();
    let ctx = context().unwrap();
    let sunday = DateTime::parse_from_rfc3339("2026-09-27T10:00:00+05:30").unwrap();
    let out = plan_gate(&ctx, sunday).await.unwrap();
    let expected = if crate::tasks::context::sole_user_id().is_none() {
        "no BASKFY_SOLE_USER_ID configured"
    } else {
        "not an NSE trading day"
    };
    assert_eq!(
        out,
        Some(baskfy_db::OrderedJson::Object(vec![(
            "skipped".to_owned(),
            baskfy_db::OrderedJson::Str(expected.to_owned())
        )]))
    );
    ctx.db.close().await;
}

fn require_clone_env() {
    let url = std::env::var(ENV).unwrap_or_else(|_| panic!("needs {ENV}"));
    assert!(url.rsplit('/').next().unwrap_or("").starts_with("rst_"));
    assert_eq!(
        std::env::var("BASKFY_DATABASE_URL").ok().as_deref(),
        Some(url.as_str()),
        "run with BASKFY_DATABASE_URL set to the clone too"
    );
}
