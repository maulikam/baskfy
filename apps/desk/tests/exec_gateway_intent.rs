//! Durable intents: a send that may have reached Kite is never repeated, across a crash, a
//! timeout or a second desk process. The store is real SQL on SQLite; the broker is the replay.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::{GatesFn, PlaceOrder, ProductGates, TenantIds};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::kite::KiteWrites;
use baskfy_desk::lanes::exec_core::intent::{
    self, DbIntentStore, IntentStore, NewIntent, Reservation,
};
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskManager};
use baskfy_desk::storage::{Db, DbConfig};
use baskfy_desk::wire::{PyDict, PyValue, dict};
use chrono::TimeZone;

const OK: &str = r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;
const TIMEOUT: &str = r#"{"session": {"authed": true}, "quotes": {}, "historical": {}, "routes":
    {"order.place": {"error": {"type": "NetworkException", "message": "timed out", "code": 502}}}}"#;
const REFUSED: &str = r#"{"session": {"authed": true}, "quotes": {}, "historical": {}, "routes":
    {"order.place": {"error": {"type": "InputException", "message": "bad qty", "code": 400}}}}"#;
const CID: &str = "plan-1:RELIANCE";

fn clock() -> Arc<dyn Clock> {
    Arc::new(FixedClock::new(
        ist().with_ymd_and_hms(2026, 10, 30, 10, 0, 0).unwrap(),
    ))
}

/// Reserves, then "dies" before the outcome reaches the store.
struct DiesBeforeSettle(DbIntentStore);

#[async_trait::async_trait]
impl IntentStore for DiesBeforeSettle {
    async fn reserve(&self, i: &NewIntent<'_>) -> Result<Reservation, String> {
        self.0.reserve(i).await
    }
    async fn settle(
        &self,
        _: &str,
        _: &str,
        _: Option<&str>,
        _: Option<&str>,
    ) -> Result<(), String> {
        Ok(())
    }
}

struct Bed {
    dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
    db: Db,
}

impl Bed {
    async fn new(fixture: &str) -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("kite.json"), fixture).unwrap();
        std::fs::create_dir(dir.path().join("instruments")).unwrap();
        let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
        let db = Db::open(DbConfig {
            backend: "sqlite".to_owned(),
            schema: "desk".to_owned(),
            database_url: String::new(),
            db_path: dir.path().join("desk.db"),
        })
        .await
        .unwrap();
        Self { dir, kite, db }
    }

    fn store(&self) -> DbIntentStore {
        DbIntentStore::new(self.db.clone(), clock())
    }

    async fn bare(&self, dry_run: bool) -> OrderGateway {
        let gates: GatesFn = Arc::new(move || ProductGates {
            dry_run,
            intraday_enabled: false,
            options_enabled: false,
            fno_carry_enabled: false,
        });
        let kite: Arc<dyn KiteWrites> = self.kite.clone();
        let risk = Arc::new(
            RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
                .await
                .unwrap(),
        );
        OrderGateway::new(
            kite,
            risk,
            GatewayOptions {
                gates,
                journal_path: self.dir.path().join("journal.jsonl"),
                stop_band: None,
                coverage: None,
                clock: clock(),
                ids: Arc::new(HarnessIds::new("intent-test")),
                limits: None,
            },
        )
        .unwrap()
    }

    async fn gateway(&self) -> OrderGateway {
        self.bare(false).await.with_intents(Arc::new(self.store()))
    }

    fn lose_journal(&self) {
        std::fs::remove_file(self.dir.path().join("journal.jsonl")).unwrap();
    }

    fn posts(&self) -> usize {
        self.kite.write_calls().len()
    }
}

fn buy() -> PlaceOrder {
    let tenant = Some(TenantIds::new(1, 1).unwrap());
    PlaceOrder {
        symbol: "RELIANCE".to_owned(),
        qty: 1,
        side: "BUY".to_owned(),
        price: Some(100.0),
        gross_exposure: Some(100.0),
        client_id: Some(CID.to_owned()),
        tenant,
        plan_tenant: tenant,
        ..PlaceOrder::default()
    }
}

fn status(d: &PyDict) -> String {
    d.get("status").unwrap().py_str()
}

fn book_with(tag: &str, order_id: &str) -> Vec<PyValue> {
    vec![PyValue::Map(dict([
        ("tag", PyValue::Str(tag.to_owned())),
        ("order_id", PyValue::Str(order_id.to_owned())),
    ]))]
}

#[tokio::test]
async fn without_the_store_a_crash_after_the_post_sends_twice() {
    let bed = Bed::new(OK).await;
    assert_eq!(
        status(&bed.bare(false).await.place(&buy()).await.unwrap()),
        "PLACED"
    );
    bed.lose_journal();
    assert_eq!(
        status(&bed.bare(false).await.place(&buy()).await.unwrap()),
        "PLACED"
    );
    assert_eq!(bed.posts(), 2, "this is the hazard the store closes");
}

#[tokio::test]
async fn a_crash_after_the_post_never_sends_twice_and_settles_by_tag() {
    let bed = Bed::new(OK).await;
    let dying = bed
        .bare(false)
        .await
        .with_intents(Arc::new(DiesBeforeSettle(bed.store())));
    assert_eq!(status(&dying.place(&buy()).await.unwrap()), "PLACED");
    bed.lose_journal();

    let after = bed.gateway().await.place(&buy()).await.unwrap();
    assert_eq!(status(&after), "ERROR");
    assert_eq!(after.get("reached_exchange"), Some(&PyValue::None));
    assert_eq!(bed.posts(), 1);

    let tag = intent::kite_tag(CID);
    let now = clock().time() + 31.0;
    let settled = intent::resolve_from_order_book(&bed.db, now, &book_with(&tag, "K1")).await;
    assert_eq!(settled.unwrap(), (1, 0));
    let again = bed.gateway().await.place(&buy()).await.unwrap();
    assert_eq!(status(&again), "DUPLICATE");
    assert_eq!(again.get("order_id"), Some(&PyValue::Str("K1".to_owned())));
    assert_eq!(bed.posts(), 1);
}

#[tokio::test]
async fn a_timeout_stays_unknown_until_the_operator_rules() {
    let bed = Bed::new(TIMEOUT).await;
    assert_eq!(
        status(&bed.gateway().await.place(&buy()).await.unwrap()),
        "ERROR"
    );
    assert_eq!(
        status(&bed.gateway().await.place(&buy()).await.unwrap()),
        "ERROR"
    );
    assert_eq!(bed.posts(), 1, "the second attempt never reached Kite");

    let now = clock().time() + 600.0;
    for _ in 0..3 {
        let polled = intent::resolve_from_order_book(&bed.db, now, &[]).await;
        assert_eq!(polled.unwrap(), (0, 1), "an empty book proves nothing");
    }
    assert!(
        intent::resolve_by_operator(&bed.db, now, CID, intent::NOT_SENT, None)
            .await
            .unwrap()
    );
    bed.gateway().await.place(&buy()).await.unwrap();
    assert_eq!(bed.posts(), 2, "only the operator's ruling frees the id");
}

#[tokio::test]
async fn a_definitive_refusal_frees_the_id_for_a_corrected_retry() {
    let bed = Bed::new(REFUSED).await;
    assert_eq!(
        status(&bed.gateway().await.place(&buy()).await.unwrap()),
        "REJECTED"
    );
    assert_eq!(
        status(&bed.gateway().await.place(&buy()).await.unwrap()),
        "REJECTED"
    );
    assert_eq!(bed.posts(), 2);
}

#[tokio::test]
async fn two_processes_send_one_order() {
    let bed = Bed::new(OK).await;
    let (first, second) = (bed.gateway().await, bed.gateway().await);
    let order = buy();
    let (a, b) = tokio::join!(first.place(&order), second.place(&order));
    let placed = [a.unwrap(), b.unwrap()]
        .iter()
        .filter(|d| status(d) == "PLACED")
        .count();
    assert_eq!(placed, 1);
    assert_eq!(bed.posts(), 1);
}

#[tokio::test]
async fn a_pause_refuses_before_the_broker_and_survives_a_restart() {
    let bed = Bed::new(OK).await;
    intent::set_paused(&bed.db, clock().time(), true, "deploy")
        .await
        .unwrap();
    let refused = bed.gateway().await.place(&buy()).await.unwrap();
    assert_eq!(status(&refused), "BLOCKED");
    assert_eq!(bed.posts(), 0);
    intent::set_paused(&bed.db, clock().time(), false, "")
        .await
        .unwrap();
    assert_eq!(
        status(&bed.gateway().await.place(&buy()).await.unwrap()),
        "PLACED"
    );
}

#[tokio::test]
async fn the_order_carries_its_tag_and_a_dry_run_never_reaches_kite() {
    let bed = Bed::new(OK).await;
    let dry = bed.bare(true).await.with_intents(Arc::new(bed.store()));
    assert_eq!(status(&dry.place(&buy()).await.unwrap()), "DRY_RUN");
    assert_eq!(bed.posts(), 0);

    let live = Bed::new(OK).await;
    live.gateway().await.place(&buy()).await.unwrap();
    let tag = intent::kite_tag(CID);
    assert_eq!(tag.len(), 20);
    assert!(format!("{:?}", live.kite.write_calls()[0]).contains(&tag));
}
