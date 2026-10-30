//! R8.TD1 on a bed clone: the Rust twin of
//! `tests/test_twt_desk.py::TestTheSessionLockOnPostgres`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use async_trait::async_trait;
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
};
use baskfy_desk::lanes::twt::common::db_int;
use baskfy_desk::lanes::twt::execute::{ExecError, execute_line};
use baskfy_desk::lanes::twt::store::{PgTwtStore, SESSION_BUSY, SessionBump, StoreError, TwtStore};
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Conn, Db, DbConfig, Param};
use baskfy_desk::wire::PyDict;
use chrono::{DateTime, Duration, FixedOffset, TimeZone};

fn now() -> DateTime<FixedOffset> {
    FixedOffset::east_opt(5 * 3600 + 1800)
        .unwrap()
        .with_ymd_and_hms(2026, 10, 30, 10, 0, 0)
        .unwrap()
}

struct NeverGateway;

#[async_trait]
impl Gateway for NeverGateway {
    async fn place(&self, _o: PlaceOrder) -> Result<PyDict, GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
    async fn place_gtt_stop(&self, _s: GttStop) -> Result<PyDict, GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
    async fn delete_gtt(&self, _r: DeleteGtt) -> Result<PyDict, GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
    async fn modify_gtt_quantity(&self, _r: ModifyGtt) -> Result<PyDict, GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
    async fn cancel_order(&self, _r: CancelOrder) -> Result<PyDict, GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
}

async fn one_int(conn: &mut Conn, sql: &str, params: &[Param]) -> i64 {
    let rows = conn.execute(sql, params).await.unwrap().rows;
    db_int(rows[0].0.first().map(|(_, v)| v))
}

async fn bed() -> Db {
    let url = std::env::var("BASKFY_RS_TEST_DATABASE_URL").unwrap();
    let name = url.rsplit('/').next().unwrap().split('?').next().unwrap();
    assert!(
        name.starts_with("rsd_"),
        "{name}: only an rsd_* clone is ever written"
    );
    Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "public".to_owned(),
        database_url: url,
        db_path: std::path::PathBuf::new(),
    })
    .await
    .unwrap()
}

/// The holder has **updated** the session row inside its transaction: before the `NOT EXISTS`
/// insert, the second confirm's `INSERT … ON CONFLICT` waited on that row instead of a 409.
#[tokio::test]
#[ignore = "needs the bed: BASKFY_RS_TEST_DATABASE_URL naming an rsd_* clone of it, then --ignored"]
async fn a_second_confirm_of_a_held_session_is_busy_and_sends_nothing() {
    let db = bed().await;
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
            Param::Text(format!("twlock-rs-{tag}")),
            Param::Text(format!("twlock-rs-{tag}@x.test")),
        ],
    )
    .await;
    let day = now().date_naive();
    let result = async {
        let iid = one_int(
            &mut setup,
            "SELECT id FROM instrument WHERE symbol ~ '^[A-Z]+$' ORDER BY id LIMIT 1",
            &[],
        )
        .await;
        let plan_id = "00000000-0000-4000-8000-000000777001".to_owned();
        let pk = one_int(
            &mut setup,
            "INSERT INTO tw_plan (plan_id, user_id, session_date, source, built_at, expires_at, \
             plan_hash, gate, sleeve_equity_inr, total_new_exposure_inr) \
             VALUES (CAST(? AS uuid), ?, ?, 'LIVE', ?, ?, 'h', 'OPEN', 1000000, 0) RETURNING id",
            &[
                Param::Text(plan_id.clone()),
                Param::Int(uid),
                Param::Date(day),
                Param::Aware(now() - Duration::minutes(1)),
                Param::Aware(now() + Duration::minutes(29)),
            ],
        )
        .await;
        let line_id = one_int(
            &mut setup,
            "INSERT INTO tw_plan_line (plan_id, user_id, instrument_id, kind, state, quantity, \
             stop_price, value_inr, client_id) \
             VALUES (?, ?, ?, 'BUY_AT_OPEN', 'PROPOSED', 10, 80, 1000, ?) RETURNING id",
            &[
                Param::Int(pk),
                Param::Int(uid),
                Param::Int(iid),
                Param::Text(format!("{plan_id}:X")),
            ],
        )
        .await;
        let mut a = PgTwtStore::new(db.connect().await.unwrap(), uid, "public", Some(1));
        let mut b = PgTwtStore::new(db.connect().await.unwrap(), uid, "public", Some(1));

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

        let busy = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            b.lock_session_for_update(day),
        )
        .await
        .expect("the second lock waited instead of refusing");
        assert!(matches!(busy, Err(StoreError::SessionBusy)), "{busy:?}");
        assert!(!b.session_locked());
        let refused = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            execute_line(
                &mut b,
                &NeverGateway,
                &Settings::defaults().unwrap(),
                &plan_id,
                line_id,
                "true",
                now(),
                None,
            ),
        )
        .await
        .expect("the confirm waited instead of refusing");
        match refused {
            Err(ExecError::Http(h)) => {
                assert_eq!(h.status, 409);
                assert_eq!(h.detail, SESSION_BUSY);
            }
            other => panic!("{other:?}"),
        }
        assert_eq!(
            b.line(line_id).await.unwrap().unwrap().s("state"),
            "PROPOSED"
        );
        assert_eq!(
            b.session(day)
                .await
                .unwrap()
                .map_or(0, |r| r.int("confirms")),
            0,
            "the holder's write is not visible before its outermost exit"
        );

        a.unlock_session().await.unwrap();
        assert!(!a.session_locked());
        assert_eq!(b.session(day).await.unwrap().unwrap().int("confirms"), 1);
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
