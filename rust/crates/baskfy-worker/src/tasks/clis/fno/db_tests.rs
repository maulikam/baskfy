//! `test_fno_schema.py`'s seed tests, on an `rst_*` clone of the bed, rolled back.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_db::OrderedJson;
use rust_decimal::Decimal;
use sqlx::Connection as _;

use super::checkpoint;
use super::seed::seed_fno;
use crate::tasks::clis::options::db_tests::{clone, fresh_user};

type AuditRow = (String, Option<String>, Option<String>, Option<String>);

fn ints(written: &[(String, OrderedJson)]) -> Vec<(String, i64)> {
    written
        .iter()
        .map(|(k, v)| {
            let OrderedJson::Int(n) = v else {
                panic!("{k} is not an int");
            };
            (k.clone(), *n)
        })
        .collect()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn seeding_twice_never_resets_a_chosen_number_nor_audits_it() {
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let user = fresh_user(&mut tx, "foseed").await;
    let first = ints(&seed_fno(&mut tx, user).await.unwrap());
    assert_eq!(
        first,
        [
            ("fo_book_config".to_owned(), 1),
            ("fo_sleeve_config".to_owned(), 3),
            ("fo_config_audit".to_owned(), 3)
        ]
    );
    sqlx::query(
        "UPDATE fo_sleeve_config SET capital_inr = 500000.00 WHERE user_id = $1 AND sleeve = 'F1'",
    )
    .bind(user)
    .execute(&mut *tx)
    .await
    .unwrap();
    let second = ints(&seed_fno(&mut tx, user).await.unwrap());
    assert!(second.iter().all(|(_, n)| *n == 0));
    let capital: Decimal = sqlx::query_scalar(
        "SELECT capital_inr FROM fo_sleeve_config WHERE user_id = $1 AND sleeve = 'F1'",
    )
    .bind(user)
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    assert_eq!(capital.to_string(), "500000.00");
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn the_seed_writes_mauliks_capital_and_the_documents_defaults() {
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let user = fresh_user(&mut tx, "fodefaults").await;
    seed_fno(&mut tx, user).await.unwrap();
    let rows: Vec<(String, String, Decimal, i16, i16, bool)> = sqlx::query_as(
        "SELECT sleeve, capital_inr::text, risk_per_trade_pct, max_lots, max_open_positions, \
         paper_enabled FROM fo_sleeve_config WHERE user_id = $1 ORDER BY sleeve",
    )
    .bind(user)
    .fetch_all(&mut *tx)
    .await
    .unwrap();
    let names: Vec<&str> = rows.iter().map(|r| r.0.as_str()).collect();
    assert_eq!(names, ["F1", "F2", "F3"]);
    assert_eq!(rows[0].1, "2500000.00");
    assert_eq!(rows[1].1, "0.00");
    assert_eq!(rows[2].1, "0.00");
    for row in &rows {
        assert_eq!(row.2, Decimal::new(100, 2));
        assert_eq!(row.3, 2);
        assert!(row.5);
    }
    assert_eq!((rows[0].4, rows[1].4), (2, 5));
    let pause: Decimal =
        sqlx::query_scalar("SELECT monthly_pause_inr FROM fo_book_config WHERE user_id = $1")
            .bind(user)
            .fetch_one(&mut *tx)
            .await
            .unwrap();
    assert_eq!(pause, Decimal::ZERO);
    let audits: Vec<AuditRow> = sqlx::query_as(
        "SELECT scope, old_value, new_value, note FROM fo_config_audit WHERE user_id = $1 \
         ORDER BY scope",
    )
    .bind(user)
    .fetch_all(&mut *tx)
    .await
    .unwrap();
    let scopes: Vec<&str> = audits.iter().map(|a| a.0.as_str()).collect();
    assert_eq!(scopes, ["F1", "F2", "F3"]);
    assert_eq!(audits[0].1, None);
    assert_eq!(audits[0].2.as_deref(), Some("2500000.00"));
    assert!(audits[0].3.as_deref().unwrap_or("").contains("M.2"));
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn a_checkpoint_makes_the_work_so_far_durable() {
    let mut conn = clone().await;
    sqlx::query("BEGIN").execute(&mut conn).await.unwrap();
    let user = fresh_user(&mut conn, "fockpt").await;
    checkpoint(&mut conn).await.unwrap();
    sqlx::query("ROLLBACK").execute(&mut conn).await.unwrap();
    let kept: i64 = sqlx::query_scalar("SELECT count(*) FROM app_user WHERE id = $1")
        .bind(user)
        .fetch_one(&mut conn)
        .await
        .unwrap();
    assert_eq!(kept, 1);
    sqlx::query("DELETE FROM app_user WHERE id = $1")
        .bind(user)
        .execute(&mut conn)
        .await
        .unwrap();
}
