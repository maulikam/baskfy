//! Two swing confirms of one session at once: one is sent, the other is a 409 `SESSION_BUSY`
//! with nothing sent. The Rust twin of `tests/test_swing_desk.py::TestTwoConfirmsAtOnce`.
//! Needs the bed: `BASKFY_RS_TEST_DATABASE_URL` naming an `rsd_*` clone, then `--ignored`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_desk::clock::FixedClock;
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
};
use baskfy_desk::lanes::swing::execute::{ExecContext, ExecError, TokioTimer, execute_line};
use baskfy_desk::lanes::swing::store::{PgSwingStore, SESSION_BUSY};
use baskfy_desk::lanes::twt::common::db_int;
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Conn, Db, DbConfig, DbValue, Param};
use baskfy_desk::wire::{PyDict, PyValue, dict};
use chrono::{DateTime, Duration, FixedOffset, TimeZone};
use rust_decimal::Decimal;

fn now() -> DateTime<FixedOffset> {
    FixedOffset::east_opt(5 * 3600 + 1800)
        .unwrap()
        .with_ymd_and_hms(2026, 10, 30, 10, 0, 0)
        .unwrap()
}

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
        panic!("not this test's path")
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

fn settings() -> Settings {
    let mut s = Settings::defaults().unwrap();
    s.dry_run = true;
    s.swing_execution_enabled = false;
    s.swing_auto_execute = false;
    s
}

async fn store(db: &Db, uid: i64) -> PgSwingStore {
    PgSwingStore {
        conn: db.connect().await.unwrap(),
        user_id: uid,
        schema: "public".to_owned(),
        broker_account_id: None,
        live_level: None,
        settings: settings(),
        clock: Arc::new(FixedClock::new(now())),
    }
}

#[tokio::test]
#[ignore = "needs the bed: BASKFY_RS_TEST_DATABASE_URL naming an rsd_* clone of it, then --ignored"]
async fn test_one_is_sent_and_the_other_is_refused_without_waiting() {
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
            Param::Text(format!("swlock-rs-{tag}")),
            Param::Text(format!("swlock-rs-{tag}@x.test")),
        ],
    )
    .await;
    let today = now().date_naive();
    let result = async {
        let rows = setup
            .execute(
                "SELECT id, symbol FROM instrument WHERE symbol IS NOT NULL \
                 AND symbol NOT LIKE 'SGB%' AND symbol ~ '^[A-Z]+$' ORDER BY id LIMIT 2",
                &[],
            )
            .await
            .unwrap()
            .rows;
        let instruments: Vec<(i64, String)> = rows
            .iter()
            .map(|r| {
                let sym = match r.get("symbol") {
                    Some(DbValue::Text(s)) => s.clone(),
                    other => panic!("{other:?}"),
                };
                (db_int(r.get("id")), sym)
            })
            .collect();
        let plan_id = "00000000-0000-4000-8000-000000910000".to_owned();
        let pk = one_int(
            &mut setup,
            "INSERT INTO sw_plan (plan_id, user_id, as_of, source, built_at, expires_at, \
             plan_hash, gate, exposure_level) \
             VALUES (CAST(? AS uuid), ?, ?, 'MORNING', ?, ?, 'h', 'GREEN', 2) RETURNING id",
            &[
                Param::Text(plan_id.clone()),
                Param::Int(uid),
                Param::Date(today),
                Param::Aware(now() - Duration::minutes(1)),
                Param::Aware(now() + Duration::minutes(29)),
            ],
        )
        .await;
        let mut line_ids = Vec::new();
        for (iid, sym) in &instruments {
            let pos = one_int(
                &mut setup,
                "INSERT INTO sw_position (user_id, instrument_id, setup, entry_date, entry_avg, \
                 quantity_entered, initial_stop, stop, trail, quantity_open) \
                 VALUES (?, ?, 'FLAG', ?, 100, 10, 90, 90, 'MA10', 10) RETURNING id",
                &[
                    Param::Int(uid),
                    Param::Int(*iid),
                    Param::Date(today - Duration::days(5)),
                ],
            )
            .await;
            line_ids.push(
                one_int(
                    &mut setup,
                    "INSERT INTO sw_plan_line (plan_id, user_id, kind, instrument_id, quantity, \
                     client_id, position_id) VALUES (?, ?, 'SELL_AT_OPEN', ?, 10, ?, ?) \
                     RETURNING id",
                    &[
                        Param::Int(pk),
                        Param::Int(uid),
                        Param::Int(*iid),
                        Param::Text(format!("{plan_id}:{sym}:SELL")),
                        Param::Int(pos),
                    ],
                )
                .await,
            );
        }

        let mut a = store(&db, uid).await;
        let mut b = store(&db, uid).await;
        let gw = SlowGateway::default();
        let s = settings();
        let timer = TokioTimer;
        let cx = ExecContext {
            settings: &s,
            orders: None,
            timer: &timer,
        };
        let price = Some(Decimal::from(120));
        let (ra, rb) = tokio::join!(
            execute_line(
                &mut a,
                &gw,
                &cx,
                &plan_id,
                line_ids[0],
                "true",
                now(),
                price
            ),
            execute_line(
                &mut b,
                &gw,
                &cx,
                &plan_id,
                line_ids[1],
                "true",
                now(),
                price
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
        let rows = setup
            .execute(
                "SELECT state FROM sw_plan_line WHERE user_id = ? ORDER BY state",
                &[Param::Int(uid)],
            )
            .await
            .unwrap()
            .rows;
        let states: Vec<String> = rows
            .iter()
            .map(|r| match r.get("state") {
                Some(DbValue::Text(s)) => s.clone(),
                other => panic!("{other:?}"),
            })
            .collect();
        assert_eq!(states, ["FILLED", "PROPOSED"]);
        let confirms = one_int(
            &mut setup,
            "SELECT confirms FROM sw_session WHERE user_id = ? AND session_date = ?",
            &[Param::Int(uid), Param::Date(today)],
        )
        .await;
        assert_eq!(confirms, 1);
    }
    .await;
    setup
        .execute("DELETE FROM app_user WHERE id = ?", &[Param::Int(uid)])
        .await
        .unwrap();
    result
}
