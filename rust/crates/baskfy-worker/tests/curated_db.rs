//! The curated jobs against a throwaway clone of the bed: `test_curated_batch_sync.py`,
//! `test_curated_dividends_beat.py`, `test_curated_rebalance_notify.py`, `test_curated_sip_beat.py`.
//!
//! Run: `BASKFY_RS_CURATED_DB_URL=postgres://…/rst_… cargo test -p baskfy-worker --test curated_db
//! -- --ignored`. Mail goes to the console transport; nothing leaves the box.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_db::{DbSettings, OrderedJson, WriteDb};
use baskfy_providers::secret::Secret;
use baskfy_worker::settings::WorkerSettings;
use baskfy_worker::tasks::context::TaskClock;
use baskfy_worker::tasks::{TaskContext, run_task};
use chrono::{DateTime, Duration, NaiveDate, NaiveDateTime, TimeZone, Utc};
use rust_decimal::Decimal;
use serde_json::{Value, json};

const ENV: &str = "BASKFY_RS_CURATED_DB_URL";
const IGNORE: &str = "needs BASKFY_RS_CURATED_DB_URL (an rst_* clone of the bed): --ignored";

/// One test at a time: the jobs read every curated row on the clone.
static LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

const CLEANUP: &str = "
DELETE FROM cb_pending_action WHERE user_id IN (SELECT id FROM app_user WHERE public_id LIKE 'rs-w1-cur-%');
DELETE FROM cb_sip_plan WHERE investment_id IN (SELECT i.id FROM cb_investment i JOIN app_user u ON u.id = i.user_id WHERE u.public_id LIKE 'rs-w1-cur-%');
DELETE FROM cb_dividend WHERE investment_id IN (SELECT i.id FROM cb_investment i JOIN app_user u ON u.id = i.user_id WHERE u.public_id LIKE 'rs-w1-cur-%');
DELETE FROM cb_investment_holding WHERE investment_id IN (SELECT i.id FROM cb_investment i JOIN app_user u ON u.id = i.user_id WHERE u.public_id LIKE 'rs-w1-cur-%');
DELETE FROM cb_order_batch WHERE user_id IN (SELECT id FROM app_user WHERE public_id LIKE 'rs-w1-cur-%');
DELETE FROM cb_investment WHERE user_id IN (SELECT id FROM app_user WHERE public_id LIKE 'rs-w1-cur-%');
DELETE FROM broker_account WHERE user_id IN (SELECT id FROM app_user WHERE public_id LIKE 'rs-w1-cur-%');
DELETE FROM app_user WHERE public_id LIKE 'rs-w1-cur-%';
DELETE FROM desk.rebalance_orders WHERE version_id LIKE 'rs-w1-%' OR version_id = 'cb-sim-rsw1aaaa';
DELETE FROM desk.rebalance_versions WHERE version_id LIKE 'rs-w1-%' OR version_id = 'cb-sim-rsw1aaaa';
DELETE FROM corporate_action WHERE instrument_id IN (SELECT id FROM instrument WHERE symbol = 'RSW1CURDIV');
DELETE FROM instrument WHERE symbol = 'RSW1CURDIV';
";

fn url() -> String {
    let url = std::env::var(ENV).unwrap_or_else(|_| panic!("{IGNORE}"));
    let name = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        name.starts_with("rst_"),
        "refusing the database {name:?}: only rst_* clones"
    );
    url
}

fn ist(text: &str) -> NaiveDateTime {
    NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M").unwrap()
}

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn ctx(at_ist: &str) -> TaskContext {
    let db = WriteDb::connect_lazy(&DbSettings::with_url(Secret::new(url()))).unwrap();
    TaskContext::new(
        db,
        baskfy_api::Settings::default(),
        WorkerSettings::default(),
        TaskClock::starting_at_ist(ist(at_ist)),
        None,
        None,
    )
}

async fn exec(ctx: &TaskContext, sql: &str) {
    let mut tx = ctx.db.begin().await.unwrap();
    sqlx::raw_sql(sql).execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
}

async fn rows<T>(ctx: &TaskContext, sql: &str, id: i64) -> Vec<T>
where
    T: for<'r> sqlx::FromRow<'r, sqlx::postgres::PgRow> + Send + Unpin,
{
    let mut tx = ctx.db.begin().await.unwrap();
    let out = sqlx::query_as(sql)
        .bind(id)
        .fetch_all(&mut *tx)
        .await
        .unwrap();
    tx.rollback().await.unwrap();
    out
}

async fn run(ctx: &TaskContext, task: &str, kwargs: Vec<(&str, OrderedJson)>) -> Value {
    let keywords = kwargs.into_iter().map(|(k, v)| (k.to_owned(), v)).collect();
    let out = run_task(ctx, task, Vec::new(), keywords).await.unwrap();
    serde_json::to_value(&out).unwrap()
}

/// A test user with a broker account and one ACTIVE investment in basket 7 created at `created`.
async fn investor(ctx: &TaskContext, n: u8, created: &str) -> (i64, i64) {
    exec(
        ctx,
        &format!(
            "INSERT INTO app_user (public_id, email) VALUES ('rs-w1-cur-{n:02}', \
             'rs.w1.cur.{n}@example.test'); \
             INSERT INTO broker_account (user_id, broker_id) SELECT id, 'zerodha' FROM app_user \
             WHERE public_id = 'rs-w1-cur-{n:02}'; \
             INSERT INTO cb_investment (user_id, basket_id, status, version_applied_id, created_at, \
             broker_account_id) SELECT u.id, 7, 'ACTIVE', 7, TIMESTAMPTZ '{created}', b.id \
             FROM app_user u JOIN broker_account b ON b.user_id = u.id \
             WHERE u.public_id = 'rs-w1-cur-{n:02}';"
        ),
    )
    .await;
    let found: Vec<(i64, i64)> = rows(
        ctx,
        "SELECT i.user_id, i.id FROM cb_investment i JOIN app_user u ON u.id = i.user_id \
         WHERE u.public_id = 'rs-w1-cur-' || lpad($1::text, 2, '0')",
        i64::from(n),
    )
    .await;
    found[0]
}

async fn batch(ctx: &TaskContext, investment_id: i64, plan_id: &str, status: &str) -> i64 {
    let mut tx = ctx.db.begin().await.unwrap();
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO cb_order_batch (investment_id, kind, desk_plan_id, status, user_id, \
         broker_account_id) SELECT i.id, 'REBALANCE', $2, $3, i.user_id, i.broker_account_id \
         FROM cb_investment i WHERE i.id = $1 RETURNING id",
    )
    .bind(investment_id)
    .bind(plan_id)
    .bind(status)
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
    id
}

async fn desk_plan(ctx: &TaskContext, version_id: &str, orders: &[(i64, i64)]) {
    let mut sql = format!(
        "INSERT INTO desk.rebalance_versions (version_id, created_ts, constituents_json, \
         weights_json) VALUES ('{version_id}', 1.0, '[]', '{{}}');"
    );
    for (i, (planned, filled)) in orders.iter().enumerate() {
        sql.push_str(&format!(
            "INSERT INTO desk.rebalance_orders (version_id, symbol, side, planned_qty, filled_qty) \
             VALUES ('{version_id}', 'S{i}', 'BUY', {planned}, {filled});"
        ));
    }
    exec(ctx, &sql).await;
}

type BatchRow = (String, Option<DateTime<Utc>>);

async fn batch_state(ctx: &TaskContext, id: i64) -> BatchRow {
    rows::<BatchRow>(
        ctx,
        "SELECT status, executed_at FROM cb_order_batch WHERE id = $1",
        id,
    )
    .await
    .remove(0)
}

fn moment() -> DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 8, 24, 0, 0, 0).unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CURATED_DB_URL"]
async fn settled_marks_executed() {
    let _guard = LOCK.lock().await;
    let ctx = ctx("2026-08-24T05:30");
    exec(&ctx, CLEANUP).await;
    let (_, inv) = investor(&ctx, 1, "2026-01-05 10:00:00+00").await;
    desk_plan(&ctx, "rs-w1-v1", &[(10, 10), (5, 5)]).await;
    let id = batch(&ctx, inv, "rs-w1-v1", "PLANNED").await;

    let out = run(&ctx, "baskfy.cb.sync_batches", vec![]).await;
    assert_eq!(
        out,
        json!({"examined": 1, "executed": 1, "partial": 0, "skipped": 0})
    );
    let (status, executed_at) = batch_state(&ctx, id).await;
    assert_eq!(status, "EXECUTED");
    let at = executed_at.unwrap();
    assert!(
        at >= moment() && at < moment() + Duration::seconds(30),
        "{at}"
    );
    assert_eq!(at.timestamp_subsec_nanos(), 0);

    let again = run(&ctx, "baskfy.cb.sync_batches", vec![]).await;
    assert_eq!(
        again,
        json!({"examined": 0, "executed": 0, "partial": 0, "skipped": 0})
    );
    assert_eq!(batch_state(&ctx, id).await, (status, executed_at));
    exec(&ctx, CLEANUP).await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CURATED_DB_URL"]
async fn partial_fill_marks_partial() {
    let _guard = LOCK.lock().await;
    let ctx = ctx("2026-08-24T05:30");
    exec(&ctx, CLEANUP).await;
    let (_, inv) = investor(&ctx, 1, "2026-01-05 10:00:00+00").await;
    desk_plan(&ctx, "rs-w1-v2", &[(10, 3)]).await;
    desk_plan(&ctx, "rs-w1-v3", &[(10, 0)]).await;
    let part = batch(&ctx, inv, "rs-w1-v2", "PLANNED").await;
    let none = batch(&ctx, inv, "rs-w1-v3", "PLANNED").await;

    let out = run(&ctx, "baskfy.cb.sync_batches", vec![]).await;
    assert_eq!(
        out,
        json!({"examined": 2, "executed": 0, "partial": 1, "skipped": 1})
    );
    assert_eq!(batch_state(&ctx, part).await, ("PARTIAL".to_owned(), None));
    assert_eq!(batch_state(&ctx, none).await, ("PLANNED".to_owned(), None));

    // A PARTIAL batch is examined again and stays PARTIAL until every lot fills.
    let again = run(&ctx, "baskfy.cb.sync_batches", vec![]).await;
    assert_eq!(
        again,
        json!({"examined": 2, "executed": 0, "partial": 1, "skipped": 1})
    );
    exec(
        &ctx,
        "UPDATE desk.rebalance_orders SET filled_qty = planned_qty WHERE version_id = 'rs-w1-v2'",
    )
    .await;
    let settled = run(&ctx, "baskfy.cb.sync_batches", vec![]).await;
    assert_eq!(
        settled,
        json!({"examined": 2, "executed": 1, "partial": 0, "skipped": 1})
    );
    assert_eq!(batch_state(&ctx, part).await.0, "EXECUTED");
    exec(&ctx, CLEANUP).await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CURATED_DB_URL"]
async fn synthetic_cb_sim_stays_planned() {
    let _guard = LOCK.lock().await;
    let ctx = ctx("2026-08-24T05:30");
    exec(&ctx, CLEANUP).await;
    let (_, inv) = investor(&ctx, 1, "2026-01-05 10:00:00+00").await;
    // Even a desk row under the synthetic id never promotes it.
    desk_plan(&ctx, "cb-sim-rsw1aaaa", &[(10, 10)]).await;
    let sim = batch(&ctx, inv, "cb-sim-rsw1aaaa", "PLANNED").await;
    let unknown = batch(&ctx, inv, "rs-w1-unknown", "PLANNED").await;

    let out = run(&ctx, "baskfy.cb.sync_batches", vec![]).await;
    assert_eq!(
        out,
        json!({"examined": 2, "executed": 0, "partial": 0, "skipped": 2})
    );
    assert_eq!(batch_state(&ctx, sim).await, ("PLANNED".to_owned(), None));
    assert_eq!(
        batch_state(&ctx, unknown).await,
        ("PLANNED".to_owned(), None)
    );
    exec(&ctx, CLEANUP).await;
}

type DividendRow = (i64, NaiveDate, Decimal, Decimal, Decimal, String);

#[tokio::test]
#[ignore = "needs BASKFY_RS_CURATED_DB_URL"]
async fn run_derives_and_persists_dividends_idempotently() {
    let _guard = LOCK.lock().await;
    let ctx = ctx("2026-08-21T20:25");
    exec(&ctx, CLEANUP).await;
    let (_, inv) = investor(&ctx, 1, "2024-01-01 00:00:00+00").await;
    exec(
        &ctx,
        "INSERT INTO instrument (exchange_id, symbol, name, instrument_type, is_active) \
         VALUES (1, 'RSW1CURDIV', 'RS W1 curated dividend', 'EQ', true);
         INSERT INTO corporate_action (instrument_id, action_type, ex_date, amount, raw) \
         SELECT id, 'dividend', DATE '2025-06-15', 2.50, '{}'::jsonb FROM instrument WHERE symbol = 'RSW1CURDIV';
         INSERT INTO corporate_action (instrument_id, action_type, ex_date, amount, raw) \
         SELECT id, 'dividend', DATE '2026-09-01', 9.00, '{}'::jsonb FROM instrument WHERE symbol = 'RSW1CURDIV';
         INSERT INTO corporate_action (instrument_id, action_type, ex_date, ratio_from, ratio_to, raw) \
         SELECT id, 'split', DATE '2025-07-01', 1, 2, '{}'::jsonb FROM instrument WHERE symbol = 'RSW1CURDIV';",
    )
    .await;
    {
        let mut tx = ctx.db.begin().await.unwrap();
        sqlx::query(
            "INSERT INTO cb_investment_holding (investment_id, instrument_id, qty, avg_price) \
             SELECT $1, id, 10.0000, 100.00 FROM instrument WHERE symbol = 'RSW1CURDIV'",
        )
        .bind(inv)
        .execute(&mut *tx)
        .await
        .unwrap();
        tx.commit().await.unwrap();
    }

    let kwargs = || vec![("as_of", OrderedJson::Str("2026-08-21".to_owned()))];
    let first = run(&ctx, "baskfy.cb.derive_dividends", kwargs()).await;
    assert_eq!(
        first,
        json!({"as_of": "2026-08-21", "investments": 1, "derived": 1, "inserted": 1, "skipped_existing": 0})
    );
    let written: Vec<DividendRow> = rows(
        &ctx,
        "SELECT d.investment_id, d.ex_date, d.amount_per_share, d.qty_held, d.total, d.source \
         FROM cb_dividend d WHERE d.investment_id = $1",
        inv,
    )
    .await;
    assert_eq!(
        written,
        vec![(
            inv,
            d(2025, 6, 15),
            Decimal::new(250, 2),
            Decimal::new(100_000, 4),
            Decimal::new(2500, 2),
            "CORPORATE_ACTIONS".to_owned(),
        )]
    );

    let second = run(&ctx, "baskfy.cb.derive_dividends", kwargs()).await;
    assert_eq!(
        second,
        json!({"as_of": "2026-08-21", "investments": 1, "derived": 1, "inserted": 0, "skipped_existing": 1})
    );
    let count: Vec<(i64,)> = rows(
        &ctx,
        "SELECT count(*) FROM cb_dividend WHERE investment_id = $1",
        inv,
    )
    .await;
    assert_eq!(count, vec![(1,)]);
    exec(&ctx, CLEANUP).await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CURATED_DB_URL"]
async fn first_send_stamps_notified_at_second_is_noop() {
    let _guard = LOCK.lock().await;
    let ctx = ctx("2026-08-24T15:30");
    exec(&ctx, CLEANUP).await;
    let (user, _) = investor(&ctx, 1, "2026-01-05 10:00:00+00").await;
    let action: i64 = {
        let mut tx = ctx.db.begin().await.unwrap();
        let id = sqlx::query_scalar(
            "INSERT INTO cb_pending_action (user_id, type, payload) VALUES ($1, \
             'REBALANCE_AVAILABLE', '{\"basket_id\": 7, \"version_no\": 3}'::jsonb) RETURNING id",
        )
        .bind(user)
        .fetch_one(&mut *tx)
        .await
        .unwrap();
        tx.commit().await.unwrap();
        id
    };

    let first = run(&ctx, "baskfy.cb.rebalance_notify", vec![]).await;
    assert_eq!(first, json!({"examined": 1, "sent": 1, "skipped": 0}));
    let payload: Vec<(Value,)> = rows(
        &ctx,
        "SELECT payload FROM cb_pending_action WHERE id = $1",
        action,
    )
    .await;
    let payload = &payload[0].0;
    assert_eq!(payload["basket_id"], json!(7));
    assert_eq!(payload["version_no"], json!(3));
    assert_eq!(payload["delivery"], json!("email"));
    let stamp = payload["notified_at"].as_str().unwrap();
    let at = DateTime::parse_from_rfc3339(stamp).unwrap();
    let start = Utc.with_ymd_and_hms(2026, 8, 24, 10, 0, 0).unwrap();
    assert!(at >= start && at < start + Duration::seconds(30), "{stamp}");
    assert!(stamp.ends_with("+00:00"), "{stamp}");

    let second = run(&ctx, "baskfy.cb.rebalance_notify", vec![]).await;
    assert_eq!(second, json!({"examined": 1, "sent": 0, "skipped": 1}));
    let unchanged: Vec<(Value,)> = rows(
        &ctx,
        "SELECT payload FROM cb_pending_action WHERE id = $1",
        action,
    )
    .await;
    assert_eq!(&unchanged[0].0, payload);
    exec(&ctx, CLEANUP).await;
}

type PendingRow = (i64, String, Value);

#[tokio::test]
#[ignore = "needs BASKFY_RS_CURATED_DB_URL"]
async fn run_raises_sip_due_idempotently() {
    let _guard = LOCK.lock().await;
    let ctx = ctx("2026-08-21T09:00");
    exec(&ctx, CLEANUP).await;
    let (user, inv) = investor(&ctx, 1, "2026-01-05 10:00:00+00").await;
    let plan: i64 = {
        let mut tx = ctx.db.begin().await.unwrap();
        let id = sqlx::query_scalar(
            "INSERT INTO cb_sip_plan (investment_id, amount, day_of_month, mode, status, \
             next_fire_date) VALUES ($1, 5000.00, 21, 'REMINDER', 'ACTIVE', DATE '2026-08-21') \
             RETURNING id",
        )
        .bind(inv)
        .fetch_one(&mut *tx)
        .await
        .unwrap();
        tx.commit().await.unwrap();
        id
    };
    let kwargs = || vec![("as_of", OrderedJson::Str("2026-08-21".to_owned()))];

    let first = run(&ctx, "baskfy.cb.sip_reminders", kwargs()).await;
    assert_eq!(
        first,
        json!({"as_of": "2026-08-21", "examined": 1, "raised": 1, "skipped": 0})
    );
    let raised: Vec<PendingRow> = rows(
        &ctx,
        "SELECT user_id, type, payload FROM cb_pending_action WHERE user_id = $1",
        user,
    )
    .await;
    assert_eq!(raised.len(), 1);
    let (owner, kind, payload) = &raised[0];
    assert_eq!((*owner, kind.as_str()), (user, "SIP_DUE"));
    assert_eq!(payload["fire_key"], json!(format!("{plan}:2026-08")));
    assert_eq!(payload["mode"], json!("REMINDER"));
    assert_eq!(payload["plan_id"], json!(plan));
    assert_eq!(payload["amount"], json!("5000.00"));
    let next: Vec<(NaiveDate,)> = rows(
        &ctx,
        "SELECT next_fire_date FROM cb_sip_plan WHERE id = $1",
        plan,
    )
    .await;
    assert_eq!(next, vec![(d(2026, 9, 21),)]);

    // Re-due for the test: the fire key already raised blocks a second reminder.
    exec(
        &ctx,
        &format!("UPDATE cb_sip_plan SET next_fire_date = DATE '2026-08-21' WHERE id = {plan}"),
    )
    .await;
    let second = run(&ctx, "baskfy.cb.sip_reminders", kwargs()).await;
    assert_eq!(second["raised"], json!(0));
    let count: Vec<(i64,)> = rows(
        &ctx,
        "SELECT count(*) FROM cb_pending_action WHERE user_id = $1",
        user,
    )
    .await;
    assert_eq!(count, vec![(1,)]);
    exec(&ctx, CLEANUP).await;
}
