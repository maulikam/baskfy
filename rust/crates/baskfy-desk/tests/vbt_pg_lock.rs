//! R8.VB1 on the deployment's database: two confirms at once cannot pass the three-entries-a-
//! session cap. The Rust twin of `tests/test_vbt_desk.py::TestTwoConfirmsCannotPassTheCap`.
//!
//! Two entries are already taken today; a MORNING plan's line (which locks **yesterday's**
//! `vb_session` row) and a LIVE plan's line (which locks **today's**) are confirmed at the same
//! moment on two connections. Before R8.VB1 each read two taken and each sent a third — four
//! entries. Now one is sent and the other is a 409 `SESSION_BUSY` with nothing sent.
//!
//! Needs the bed: `BASKFY_RS_TEST_DATABASE_URL` naming an `rsd_*` clone, then `--ignored`. It
//! writes one throwaway `app_user` (and its rows, by cascade) and deletes it on the way out.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Mutex;

use async_trait::async_trait;
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
};
use baskfy_desk::lanes::twt::common::db_int;
use baskfy_desk::lanes::vbt::execute::{ExecError, execute_line};
use baskfy_desk::lanes::vbt::store::{PgVbtStore, SESSION_BUSY, StoreError, VbtStore};
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Conn, Db, DbConfig, DbValue, Param};
use baskfy_desk::wire::{PyDict, PyValue, dict};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};

fn now() -> DateTime<FixedOffset> {
    FixedOffset::east_opt(5 * 3600 + 1800)
        .unwrap()
        .with_ymd_and_hms(2026, 10, 30, 10, 0, 0)
        .unwrap()
}

/// A gateway whose send takes long enough for the other confirm to arrive mid-flight.
#[derive(Default)]
struct SlowGateway {
    places: Mutex<Vec<String>>,
}

#[async_trait]
impl Gateway for SlowGateway {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        self.places.lock().unwrap().push(order.symbol.clone());
        tokio::time::sleep(std::time::Duration::from_millis(400)).await;
        Ok(dict([
            ("status", PyValue::Str("DRY_RUN".into())),
            ("order_id", PyValue::Str(format!("dry-{}", order.symbol))),
        ]))
    }
    async fn place_gtt_stop(&self, _s: GttStop) -> Result<PyDict, GatewayError> {
        Ok(dict([
            ("status", PyValue::Str("DRY_RUN_GTT".into())),
            ("trigger_id", PyValue::Str("g".into())),
        ]))
    }
    async fn delete_gtt(&self, _r: DeleteGtt) -> Result<PyDict, GatewayError> {
        panic!("not this test's path")
    }
    async fn modify_gtt_quantity(&self, _r: ModifyGtt) -> Result<PyDict, GatewayError> {
        panic!("not this test's path")
    }
    async fn cancel_order(&self, _r: CancelOrder) -> Result<PyDict, GatewayError> {
        panic!("not this test's path")
    }
}

async fn one_int(conn: &mut Conn, sql: &str, params: &[Param]) -> i64 {
    let rows = conn.execute(sql, params).await.unwrap().rows;
    db_int(rows[0].0.first().map(|(_, v)| v))
}

#[tokio::test]
#[ignore = "needs the bed: BASKFY_RS_TEST_DATABASE_URL naming an rsd_* clone of it, then --ignored"]
async fn two_tabs_at_once_take_one_entry_between_them() {
    let url = std::env::var("BASKFY_RS_TEST_DATABASE_URL").unwrap();
    let name = url.rsplit('/').next().unwrap().split('?').next().unwrap();
    assert!(
        name.starts_with("rsd_"),
        "{name}: only an rsd_* clone is ever written"
    );
    let db = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "public".to_owned(),
        database_url: url.clone(),
        db_path: std::path::PathBuf::new(),
    })
    .await
    .unwrap();
    let mut setup = db.connect().await.unwrap();
    let tag = format!(
        "{:x}",
        now().timestamp_nanos_opt().unwrap_or(0) ^ i64::from(std::process::id())
    );
    let uid = one_int(
        &mut setup,
        "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
        &[
            Param::Text(format!("vbcap-rs-{tag}")),
            Param::Text(format!("vbcap-rs-{tag}@x.test")),
        ],
    )
    .await;
    let today = now().date_naive();
    let yesterday = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap();
    let result = async {
        let rows = setup
            .execute(
                "SELECT id, symbol FROM instrument WHERE symbol IS NOT NULL \
                 AND symbol NOT LIKE 'SGB%' AND symbol ~ '^[A-Z]+$' ORDER BY id LIMIT 4",
                &[],
            )
            .await
            .unwrap()
            .rows;
        let instruments: Vec<(i64, String)> = rows
            .iter()
            .map(|r| {
                let id = db_int(r.get("id"));
                let sym = match r.get("symbol") {
                    Some(DbValue::Text(s)) => s.clone(),
                    other => panic!("{other:?}"),
                };
                (id, sym)
            })
            .collect();
        for (iid, _) in &instruments[..2] {
            setup
                .execute(
                    "INSERT INTO vb_order (user_id, instrument_id, signal_date, limit_price, \
                     stop_price, quantity, state, simulated, created_at) \
                     VALUES (?, ?, ?, 100, 90, 1, 'SENT', true, ?)",
                    &[
                        Param::Int(uid),
                        Param::Int(*iid),
                        Param::Date(yesterday),
                        Param::Aware(now()),
                    ],
                )
                .await
                .unwrap();
        }
        let mut lines = Vec::new();
        for (i, ((iid, sym), (source, day, kind))) in instruments[2..]
            .iter()
            .zip([
                ("MORNING", yesterday, "PLACE_LIMIT"),
                ("LIVE", today, "BUY_AT_MARKET"),
            ])
            .enumerate()
        {
            let plan_id = format!("00000000-0000-4000-8000-{:012}", 900_000 + i);
            let pk = one_int(
                &mut setup,
                "INSERT INTO vb_plan (plan_id, user_id, session_date, source, built_at, \
                 expires_at, plan_hash, gate, sleeve_equity_inr) \
                 VALUES (CAST(? AS uuid), ?, ?, ?, ?, ?, 'h', 'OPEN', 1000000) RETURNING id",
                &[
                    Param::Text(plan_id.clone()),
                    Param::Int(uid),
                    Param::Date(day),
                    Param::Text(source.to_owned()),
                    Param::Aware(now() - Duration::minutes(1)),
                    Param::Aware(now() + Duration::minutes(29)),
                ],
            )
            .await;
            let line_id = one_int(
                &mut setup,
                "INSERT INTO vb_plan_line (plan_id, user_id, instrument_id, kind, state, \
                 quantity, limit_price, stop_price, value_inr, client_id) \
                 VALUES (?, ?, ?, ?, 'PROPOSED', 10, 100, 88, 1000, ?) RETURNING id",
                &[
                    Param::Int(pk),
                    Param::Int(uid),
                    Param::Int(*iid),
                    Param::Text(kind.to_owned()),
                    Param::Text(format!("{plan_id}:{sym}:{kind}")),
                ],
            )
            .await;
            lines.push((plan_id, line_id));
        }

        let mut a = PgVbtStore::new(db.connect().await.unwrap(), uid, "public", None);
        let mut b = PgVbtStore::new(db.connect().await.unwrap(), uid, "public", None);
        let gw = SlowGateway::default();
        let settings = Settings::defaults().unwrap();
        let (ra, rb) = tokio::join!(
            execute_line(
                &mut a,
                &gw,
                &settings,
                &lines[0].0,
                lines[0].1,
                "true",
                now(),
                None
            ),
            execute_line(
                &mut b,
                &gw,
                &settings,
                &lines[1].0,
                lines[1].1,
                "true",
                now(),
                None
            ),
        );
        let outcomes = [ra, rb];
        let sent: Vec<_> = outcomes.iter().filter_map(|o| o.as_ref().ok()).collect();
        let busy: Vec<_> = outcomes
            .iter()
            .filter_map(|o| match o {
                Err(ExecError::Http(h)) => Some(h),
                _ => None,
            })
            .collect();
        assert_eq!(sent.len(), 1, "{outcomes:?}");
        assert_eq!(sent[0].status, "SIMULATED", "{outcomes:?}");
        assert_eq!(busy.len(), 1, "{outcomes:?}");
        assert_eq!(busy[0].status, 409);
        assert_eq!(busy[0].detail, SESSION_BUSY);
        assert_eq!(
            gw.places.lock().unwrap().len(),
            1,
            "the refused confirm sent nothing"
        );
        assert!(!a.session_locked() && !b.session_locked());
        let mut check = PgVbtStore::new(db.connect().await.unwrap(), uid, "public", None);
        assert_eq!(
            check.entries_taken(today).await.unwrap(),
            3,
            "the cap is three; it was four before R8.VB1"
        );
        // A store-level refusal is the same error the confirm maps to 409.
        a.lock_session_for_update(yesterday).await.unwrap();
        assert!(
            matches!(
                b.lock_session_for_update(today).await,
                Err(StoreError::SessionBusy)
            ),
            "the per-user lock spans the two days' rows"
        );
        a.unlock_session().await.unwrap();
        b.lock_session_for_update(today).await.unwrap();
        b.unlock_session().await.unwrap();
    }
    .await;
    setup
        .execute("DELETE FROM app_user WHERE id = ?", &[Param::Int(uid)])
        .await
        .unwrap();
    result
}

/// Nested blocks open one transaction; a row the holder **updated** is a 409 at once, not a
/// wait on the session insert; a row held by another transaction without the per-user lock is
/// the `55P03` refusal, rolled back.
#[tokio::test]
#[ignore = "needs the bed: BASKFY_RS_TEST_DATABASE_URL naming an rsd_* clone of it, then --ignored"]
async fn nested_blocks_commit_once_and_a_held_row_never_waits() {
    use baskfy_desk::lanes::vbt::store::SessionBump;

    let url = std::env::var("BASKFY_RS_TEST_DATABASE_URL").unwrap();
    let name = url.rsplit('/').next().unwrap().split('?').next().unwrap();
    assert!(
        name.starts_with("rsd_"),
        "{name}: only an rsd_* clone is ever written"
    );
    let db = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "public".to_owned(),
        database_url: url.clone(),
        db_path: std::path::PathBuf::new(),
    })
    .await
    .unwrap();
    let mut setup = db.connect().await.unwrap();
    let tag = format!(
        "{:x}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
            ^ u128::from(std::process::id())
    );
    let uid = one_int(
        &mut setup,
        "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
        &[
            Param::Text(format!("vbnest-rs-{tag}")),
            Param::Text(format!("vbnest-rs-{tag}@x.test")),
        ],
    )
    .await;
    let day = now().date_naive();
    let wait = std::time::Duration::from_secs(5);
    let result = async {
        let mut a = PgVbtStore::new(db.connect().await.unwrap(), uid, "public", None);
        let mut b = PgVbtStore::new(db.connect().await.unwrap(), uid, "public", None);
        a.lock_session_for_update(day).await.unwrap();
        a.lock_session_for_update(day).await.unwrap();
        a.bump_session(
            day,
            SessionBump {
                mode: "DRY_RUN".to_owned(),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await
        .unwrap();
        a.unlock_session().await.unwrap();
        assert!(a.session_locked(), "the inner exit does not commit");
        let busy = tokio::time::timeout(wait, b.lock_session_for_update(day))
            .await
            .expect("the second lock waited instead of refusing");
        assert!(matches!(busy, Err(StoreError::SessionBusy)), "{busy:?}");
        assert!(!b.session_locked());
        assert_eq!(
            b.session(day)
                .await
                .unwrap()
                .map_or(0, |r| r.int("confirms")),
            0
        );
        a.unlock_session().await.unwrap();
        assert!(!a.session_locked());
        assert_eq!(b.session(day).await.unwrap().unwrap().int("confirms"), 1);

        let mut holder = db.connect().await.unwrap();
        holder.execute("BEGIN", &[]).await.unwrap();
        holder
            .execute(
                "SELECT 1 FROM vb_session WHERE user_id = ? AND session_date = ? FOR UPDATE",
                &[Param::Int(uid), Param::Date(day)],
            )
            .await
            .unwrap();
        let row_busy = tokio::time::timeout(wait, b.lock_session_for_update(day))
            .await
            .expect("a held row waited instead of refusing");
        assert!(
            matches!(row_busy, Err(StoreError::SessionBusy)),
            "{row_busy:?}"
        );
        assert!(!b.session_locked());
        assert_eq!(
            b.entries_taken(day).await.unwrap(),
            0,
            "b is out of the aborted transaction"
        );
        holder.execute("ROLLBACK", &[]).await.unwrap();
        b.lock_session_for_update(day).await.unwrap();
        b.unlock_session().await.unwrap();
    }
    .await;
    setup
        .execute("DELETE FROM app_user WHERE id = ?", &[Param::Int(uid)])
        .await
        .unwrap();
    result
}
