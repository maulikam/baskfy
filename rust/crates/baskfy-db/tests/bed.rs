//! Against the test bed: a **copy** of the production database (DECISIONS-RS R5.4). Run with
//! `BASKFY_RS_TEST_DATABASE_URL=postgres://...` set at build time; without it every test here is
//! reported `ignored` with the reason. Never point it at the live database: these tests only
//! read, and one of them proves the pool cannot write, but the bed is the only place agreed for
//! them.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_db::{Db, DbSettings, OrderedJson, row};
use baskfy_providers::secret::Secret;
use rust_decimal::Decimal;

fn db() -> Db {
    let url = std::env::var("BASKFY_RS_TEST_DATABASE_URL").expect("cfg(bed) implies the variable");
    Db::connect_lazy(&DbSettings::with_url(Secret::new(url))).unwrap()
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_pool_answers() {
    db().ping().await.unwrap();
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn every_connection_is_read_only() {
    let db = db();
    let r = sqlx::query("SHOW default_transaction_read_only")
        .fetch_one(db.pool())
        .await
        .unwrap();
    let on: String = sqlx::Row::get(&r, 0);
    assert_eq!(on, "on");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn a_write_is_refused() {
    let db = db();
    // `WHERE false` touches no row, so even a misconfigured pool would change nothing; the
    // read-only check fires at executor start regardless of the row count.
    let err = sqlx::query("UPDATE app_user SET name = name WHERE false")
        .execute(db.pool())
        .await
        .map_err(baskfy_db::DbError::from)
        .unwrap_err();
    assert_eq!(err.sqlstate(), Some("25006"), "{err}");

    // Inside an explicit transaction too.
    let mut tx = db.begin().await.unwrap();
    let err = sqlx::query("CREATE TABLE rs_must_not_exist (x int)")
        .execute(&mut *tx)
        .await
        .map_err(baskfy_db::DbError::from)
        .unwrap_err();
    assert_eq!(err.sqlstate(), Some("25006"), "{err}");
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn numeric_keeps_its_scale() {
    // asyncpg: Decimal('1.5000'), Decimal('1E-7'), Decimal('0.00'), Decimal('-12.30').
    let r = sqlx::query(
        "SELECT 1.5::numeric(18,4) AS a, 0.0000001::numeric AS b, 0::numeric(10,2) AS c, \
         -12.30::numeric AS d, NULL::numeric AS e",
    )
    .fetch_one(db().pool())
    .await
    .unwrap();
    assert_eq!(row::decimal(&r, "a").unwrap().to_string(), "1.5000");
    assert_eq!(row::decimal(&r, "b").unwrap(), Decimal::new(1, 7));
    assert_eq!(row::decimal(&r, "b").unwrap().scale(), 7);
    assert_eq!(row::decimal(&r, "c").unwrap().to_string(), "0.00");
    assert_eq!(row::decimal(&r, "d").unwrap().to_string(), "-12.30");
    assert_eq!(row::opt_decimal(&r, "e").unwrap(), None);
    assert!(row::decimal(&r, "e").is_err(), "a NULL is not a Decimal");
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn numeric_beyond_28_digits_is_an_error_not_a_rounding() {
    let r = sqlx::query("SELECT 1e40::numeric AS big")
        .fetch_one(db().pool())
        .await
        .unwrap();
    assert!(row::decimal(&r, "big").is_err());
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn timestamptz_is_utc_and_date_is_a_date() {
    let r = sqlx::query(
        "SELECT '2026-08-26 13:52:55.316805+05:30'::timestamptz AS t, \
         '2026-10-29'::date AS d, NULL::timestamptz AS n",
    )
    .fetch_one(db().pool())
    .await
    .unwrap();
    assert_eq!(
        row::timestamptz(&r, "t").unwrap().to_rfc3339(),
        "2026-08-26T08:22:55.316805+00:00"
    );
    assert_eq!(row::date(&r, "d").unwrap().to_string(), "2026-10-29");
    assert_eq!(row::opt_timestamptz(&r, "n").unwrap(), None);
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn jsonb_keeps_the_order_python_reads() {
    // Python: json.dumps(json.loads(<jsonb text>), separators=(",", ":"))
    //   == '{"a":1.5,"zeta":[1,{"y":null,"x":true}]}'
    let r = sqlx::query(
        r#"SELECT '{"zeta":[1,{"y":null,"x":true}],"a":1.50}'::jsonb AS j, NULL::jsonb AS n"#,
    )
    .fetch_one(db().pool())
    .await
    .unwrap();
    let j: OrderedJson = row::jsonb(&r, "j").unwrap();
    assert_eq!(
        j.to_python_json().unwrap(),
        r#"{"a":1.5,"zeta":[1,{"x":true,"y":null}]}"#
    );
    assert_eq!(row::opt_jsonb(&r, "n").unwrap(), None);
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn a_real_table_reads_as_python_reads_it() {
    // The bed's plan rows: `plan.features` is jsonb; Python's json.loads keeps jsonb's order,
    // which puts "gated" before "label" before "includes" (shorter keys first).
    let r = sqlx::query("SELECT code, features FROM plan WHERE code = 'forever'")
        .fetch_one(db().pool())
        .await
        .unwrap();
    let features = row::jsonb(&r, "features").unwrap();
    let OrderedJson::Object(entries) = &features else {
        panic!("features is an object");
    };
    let keys: Vec<&str> = entries.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(&keys[..3], &["gated", "label", "tagline"]);
    assert_eq!(
        features
            .get("entitlements")
            .and_then(|e| e.get("max_screens")),
        Some(&OrderedJson::Int(50))
    );
}

#[tokio::test]
#[cfg_attr(
    not(bed),
    ignore = "BASKFY_RS_TEST_DATABASE_URL is unset at build time: the bed tests are skipped"
)]
async fn the_write_pool_writes_and_an_uncommitted_session_leaves_nothing() {
    use baskfy_db::WriteDb;
    let url = std::env::var("BASKFY_RS_TEST_DATABASE_URL").expect("cfg(bed) implies the variable");
    let write = WriteDb::connect_lazy(&DbSettings::with_url(Secret::new(url))).unwrap();
    let mut tx = write.begin().await.unwrap();
    let r = sqlx::query("SHOW default_transaction_read_only")
        .fetch_one(&mut *tx)
        .await
        .unwrap();
    let on: String = sqlx::Row::get(&r, 0);
    assert_eq!(on, "off");
    // A temporary table, inside a transaction that is dropped: nothing persists on the bed.
    sqlx::query("CREATE TEMP TABLE rs_write_probe (x int) ON COMMIT DROP")
        .execute(&mut *tx)
        .await
        .unwrap();
    sqlx::query("INSERT INTO rs_write_probe VALUES (1)")
        .execute(&mut *tx)
        .await
        .unwrap();
    drop(tx);
    let mut tx = write.begin().await.unwrap();
    let gone = sqlx::query("SELECT to_regclass('pg_temp.rs_write_probe') IS NULL")
        .fetch_one(&mut *tx)
        .await
        .unwrap();
    assert!(
        sqlx::Row::get::<bool, _>(&gone, 0),
        "a dropped session must roll back"
    );
    drop(tx);
    write.close().await;
}
