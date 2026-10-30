//! `services/worker/tests/test_purge_accounts.py`, ported (wave 6, lane api_routes).
//!
//! **Where they run.** As the wave-5 lanes' database tests (DECISIONS-RS R7.9): one connection to
//! a *scratch* database, inside `TEMP` tables shaped like the production ones (the keys, the
//! `ON DELETE CASCADE`s the purge relies on) — never the bed's `baskfy`. Set
//! `BASKFY_RS_SCRATCH_DATABASE_URL` and run `cargo test -p baskfy-worker -- --ignored
//! api_routes::purge_db_tests`; without it they are reported `ignored`. The parity against Python
//! on real clones is `task_diff.py --cases tools/parity/task_cases/api_routes.json`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use chrono::{DateTime, Duration, TimeZone, Utc};
use sqlx::{Connection as _, PgConnection};

use super::purge::{TOMBSTONE_DOMAIN, run_purge_accounts};
use crate::steps::StepOutcome;

const IGNORE: &str =
    "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored";

/// `NOW = dt.datetime(2026, 8, 21, 3, 0, tzinfo=dt.UTC)`.
fn now() -> DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 8, 21, 3, 0, 0).unwrap()
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
    for ddl in [
        "CREATE TEMP TABLE app_user (id bigserial PRIMARY KEY, public_id varchar NOT NULL UNIQUE, \
         email varchar NOT NULL UNIQUE, password_hash varchar, name varchar, \
         email_verified_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), \
         deleted_at timestamptz)",
        "CREATE TEMP TABLE account_deletion (user_id bigint PRIMARY KEY REFERENCES app_user(id) \
         ON DELETE CASCADE, requested_at timestamptz NOT NULL DEFAULT now(), \
         purge_after timestamptz NOT NULL, cancelled_at timestamptz, purged_at timestamptz)",
        "CREATE TEMP TABLE payment (id bigserial PRIMARY KEY, user_id bigint NOT NULL \
         REFERENCES app_user(id), amount_inr numeric(12,2) NOT NULL, status varchar NOT NULL, \
         invoice_number varchar)",
        "CREATE TEMP TABLE screen (id bigserial PRIMARY KEY, user_id bigint REFERENCES \
         app_user(id) ON DELETE CASCADE)",
        "CREATE TEMP TABLE portfolio (id bigserial PRIMARY KEY, user_id bigint NOT NULL \
         REFERENCES app_user(id) ON DELETE CASCADE)",
        "CREATE TEMP TABLE consent_record (id bigserial PRIMARY KEY, user_id bigint NOT NULL \
         REFERENCES app_user(id) ON DELETE CASCADE)",
        "CREATE TEMP TABLE auth_token (id bigserial PRIMARY KEY, user_id bigint REFERENCES \
         app_user(id) ON DELETE CASCADE)",
        "CREATE TEMP TABLE refresh_token (id bigserial PRIMARY KEY, user_id bigint NOT NULL \
         REFERENCES app_user(id) ON DELETE CASCADE)",
    ] {
        sqlx::query(ddl).execute(&mut conn).await.unwrap();
    }
    conn
}

/// `_account(session, email, deleted=, purge_after=)`.
async fn account(
    conn: &mut PgConnection,
    email: &str,
    deleted: bool,
    purge_after: Option<DateTime<Utc>>,
) -> i64 {
    let local = email.split('@').next().unwrap_or("");
    let public_id: String = format!("{local:0<12}").chars().take(12).collect();
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO app_user (public_id, email, name, password_hash, deleted_at) \
         VALUES ($1, $2, 'Someone', 'hash', $3) RETURNING id",
    )
    .bind(&public_id)
    .bind(email)
    .bind(deleted.then(|| now() - Duration::days(8)))
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    if let Some(after) = purge_after {
        sqlx::query("INSERT INTO account_deletion (user_id, purge_after) VALUES ($1, $2)")
            .bind(id)
            .bind(after)
            .execute(&mut *conn)
            .await
            .unwrap();
    }
    id
}

async fn exists(conn: &mut PgConnection, id: i64) -> bool {
    sqlx::query_scalar::<_, i64>("SELECT id FROM app_user WHERE id = $1")
        .bind(id)
        .fetch_optional(&mut *conn)
        .await
        .unwrap()
        .is_some()
}

async fn purge(conn: &mut PgConnection, outcome: &mut StepOutcome) -> i64 {
    let mut tx = conn.begin().await.unwrap();
    let n = run_purge_accounts(&mut tx, outcome, now()).await.unwrap();
    tx.commit().await.unwrap();
    n
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn it_erases_an_account_past_its_window() {
    let mut conn = scratch().await;
    let id = account(
        &mut conn,
        "gone@example.com",
        true,
        Some(now() - Duration::days(1)),
    )
    .await;
    assert_eq!(purge(&mut conn, &mut StepOutcome::default()).await, 1);
    assert!(!exists(&mut conn, id).await);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn it_leaves_an_account_still_inside_its_window() {
    let mut conn = scratch().await;
    let id = account(
        &mut conn,
        "waiting@example.com",
        true,
        Some(now() + Duration::days(3)),
    )
    .await;
    assert_eq!(purge(&mut conn, &mut StepOutcome::default()).await, 0);
    assert!(exists(&mut conn, id).await);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn it_leaves_an_account_that_was_reactivated() {
    let mut conn = scratch().await;
    let id = account(
        &mut conn,
        "back@example.com",
        false,
        Some(now() - Duration::days(1)),
    )
    .await;
    assert_eq!(purge(&mut conn, &mut StepOutcome::default()).await, 0);
    assert!(exists(&mut conn, id).await);
    let cancelled: Option<DateTime<Utc>> =
        sqlx::query_scalar("SELECT cancelled_at FROM account_deletion WHERE user_id = $1")
            .bind(id)
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(cancelled, Some(now()), "the reactivation is recorded");
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn it_keeps_the_invoice_trail_but_unlinks_it() {
    let mut conn = scratch().await;
    let id = account(
        &mut conn,
        "paid@example.com",
        true,
        Some(now() - Duration::days(1)),
    )
    .await;
    sqlx::query(
        "INSERT INTO payment (user_id, amount_inr, status, invoice_number) \
         VALUES ($1, 500.00, 'captured', 'DEC-0001')",
    )
    .bind(id)
    .execute(&mut conn)
    .await
    .unwrap();
    sqlx::query("INSERT INTO consent_record (user_id) VALUES ($1)")
        .bind(id)
        .execute(&mut conn)
        .await
        .unwrap();
    let mut outcome = StepOutcome::default();
    assert_eq!(purge(&mut conn, &mut outcome).await, 1);
    let owner: i64 =
        sqlx::query_scalar("SELECT user_id FROM payment WHERE invoice_number = 'DEC-0001'")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(owner, id, "the invoice trail survives intact");
    let (email, name, hash): (String, Option<String>, Option<String>) =
        sqlx::query_as("SELECT email, name, password_hash FROM app_user WHERE id = $1")
            .bind(id)
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert!(email.ends_with(&format!("@{TOMBSTONE_DOMAIN}")));
    assert!(!email.contains("paid@example.com"));
    assert_eq!((name, hash), (None, None));
    let consents: i64 = sqlx::query_scalar("SELECT count(*) FROM consent_record")
        .fetch_one(&mut conn)
        .await
        .unwrap();
    assert_eq!(consents, 0, "every owned row but the invoice is deleted");
    assert_eq!(
        outcome.detail_json().get("anonymised"),
        Some(&baskfy_db::OrderedJson::Int(1))
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn it_is_idempotent() {
    let mut conn = scratch().await;
    account(
        &mut conn,
        "twice@example.com",
        true,
        Some(now() - Duration::days(1)),
    )
    .await;
    assert_eq!(purge(&mut conn, &mut StepOutcome::default()).await, 1);
    assert_eq!(purge(&mut conn, &mut StepOutcome::default()).await, 0);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_evidence_row_goes_with_the_account() {
    let mut conn = scratch().await;
    let id = account(
        &mut conn,
        "audit@example.com",
        true,
        Some(now() - Duration::days(1)),
    )
    .await;
    purge(&mut conn, &mut StepOutcome::default()).await;
    let left: Option<i64> =
        sqlx::query_scalar("SELECT user_id FROM account_deletion WHERE user_id = $1")
            .bind(id)
            .fetch_optional(&mut conn)
            .await
            .unwrap();
    assert!(
        left.is_none(),
        "the FK cascades: the evidence is the step's note"
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn it_names_public_ids_and_never_addresses() {
    let mut conn = scratch().await;
    account(
        &mut conn,
        "private@example.com",
        true,
        Some(now() - Duration::days(1)),
    )
    .await;
    let mut outcome = StepOutcome::default();
    purge(&mut conn, &mut outcome).await;
    let rendered = crate::pyjson::dumps(&outcome.detail_json()).unwrap();
    assert!(!rendered.contains("private@example.com"));
    assert!(rendered.contains("private00000"));
}
