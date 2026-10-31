//! `KiteHttp` against a local mock: DRY_RUN sends nothing, writes speak the SDK wire.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex};

use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::gateway::{
    CancelOrder, DeleteGtt, GatesFn, GttStop, ModifyGtt, PlaceOrder, ProductGates, TenantIds,
};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::kite::{
    GttOrderLeg, GttParams, KiteExceptionKind, KiteHttp, KiteReads, KiteWrites, OrderParams,
};
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskManager};
use baskfy_desk::wire::PyValue;
use baskfy_providers::kite::KiteError;
use baskfy_providers::secret::Secret;
use chrono::TimeZone;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

#[derive(Debug, Clone)]
struct Seen {
    head: String,
    body: String,
}

type Log = Arc<Mutex<Vec<Seen>>>;

async fn mock(reply: &'static str) -> (String, Log) {
    baskfy_providers::net::block_network();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let log: Log = Arc::default();
    let sink = log.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                return;
            };
            let sink = sink.clone();
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 4096];
                let split = loop {
                    let n = sock.read(&mut chunk).await.unwrap_or(0);
                    if n == 0 {
                        return;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        break i + 4;
                    }
                };
                let head = String::from_utf8_lossy(&buf[..split]).to_string();
                let len = head
                    .lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        k.eq_ignore_ascii_case("content-length")
                            .then(|| v.trim().parse::<usize>().ok())?
                    })
                    .unwrap_or(0);
                while buf.len() < split + len {
                    let n = sock.read(&mut chunk).await.unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                }
                let body = String::from_utf8_lossy(&buf[split..]).to_string();
                sink.lock().unwrap().push(Seen { head, body });
                let out = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}",
                    reply.len()
                );
                let _ = sock.write_all(out.as_bytes()).await;
            });
        }
    });
    (format!("http://{addr}"), log)
}

const OK: &str = r#"{"status":"success","data":{"order_id":"151220000000000","trigger_id":123,"access_token":"fresh","login_time":"2026-10-31 09:00:00"}}"#;

fn tenant() -> TenantIds {
    TenantIds::new(1, 1).unwrap()
}

fn client(root: &str) -> Arc<KiteHttp> {
    let k = KiteHttp::with_root("test-key", root, Some(2.0)).unwrap();
    k.set_access_token(&Secret::new("test-token"));
    Arc::new(k)
}

#[tokio::test]
async fn dry_run_through_the_real_client_sends_no_http_request() {
    let (root, log) = mock(OK).await;
    let kite = client(&root);
    let dir = tempfile::tempdir().unwrap();
    let clock: Arc<dyn Clock> = Arc::new(FixedClock::new(
        ist().with_ymd_and_hms(2026, 10, 30, 10, 0, 0).unwrap(),
    ));
    let gates: GatesFn = Arc::new(|| ProductGates {
        dry_run: true,
        intraday_enabled: false,
        options_enabled: false,
        fno_carry_enabled: false,
    });
    let risk = Arc::new(
        RiskManager::new(RiskConfig::default(), clock.clone(), RiskBacking::Memory)
            .await
            .unwrap(),
    );
    let broker: Arc<dyn KiteWrites> = kite.clone();
    let gw = OrderGateway::new(
        broker,
        risk,
        GatewayOptions {
            gates,
            journal_path: dir.path().join("journal.jsonl"),
            stop_band: None,
            coverage: None,
            clock,
            ids: Arc::new(HarnessIds::new("kite-http-test")),
            limits: None,
        },
    )
    .unwrap();
    let t = Some(tenant());
    let status = |d: &baskfy_desk::wire::PyDict| d.get("status").unwrap().py_str();

    let placed = gw
        .place(&PlaceOrder {
            symbol: "RELIANCE".to_owned(),
            qty: 1,
            side: "BUY".to_owned(),
            price: Some(100.0),
            gross_exposure: Some(100.0),
            tenant: t,
            plan_tenant: t,
            ..PlaceOrder::default()
        })
        .await
        .unwrap();
    assert_eq!(status(&placed), "DRY_RUN");
    let gtt = gw
        .place_gtt_stop(&GttStop {
            symbol: "RELIANCE".to_owned(),
            qty: 10,
            trigger: 89.0,
            last_price: 100.0,
            exchange: "NSE".to_owned(),
            series: None,
            client_id: None,
            tenant: t,
            plan_tenant: t,
            limit_fraction: None,
            fo_plan: None,
        })
        .await
        .unwrap();
    assert_eq!(status(&gtt), "DRY_RUN_GTT");
    let modified = gw
        .modify_gtt_value(
            &ModifyGtt {
                gtt_id: 7,
                symbol: "RELIANCE".to_owned(),
                qty: 5,
                trigger: 90.0,
                last_price: 100.0,
                exchange: "NSE".to_owned(),
                series: None,
                client_id: None,
                tenant: t,
                plan_tenant: t,
                limit_fraction: None,
                fo_plan: None,
                floor_trigger: None,
            },
            &PyValue::Int(7),
        )
        .await
        .unwrap();
    assert!(status(&modified).contains("DRY"), "{modified:?}");
    let deleted = gw
        .delete_gtt_value(
            &DeleteGtt {
                gtt_id: 7,
                symbol: "RELIANCE".to_owned(),
                exchange: "NSE".to_owned(),
                series: None,
                client_id: None,
                tenant: t,
                plan_tenant: t,
            },
            &PyValue::Int(7),
        )
        .await
        .unwrap();
    assert_eq!(status(&deleted), "DRY_RUN_GTT_DELETE");
    let cancelled = gw
        .cancel_order_value(
            &CancelOrder {
                order_id: "151220000000000".to_owned(),
                symbol: "RELIANCE".to_owned(),
                variety: "regular".to_owned(),
                series: None,
                client_id: None,
                tenant: t,
                plan_tenant: t,
            },
            &PyValue::Str("151220000000000".to_owned()),
        )
        .await
        .unwrap();
    assert!(status(&cancelled).contains("DRY"), "{cancelled:?}");
    assert_eq!(log.lock().unwrap().len(), 0, "DRY_RUN reached the HTTP layer");

    kite.profile().await.unwrap();
    assert_eq!(log.lock().unwrap().len(), 1, "the mock counts real requests");
}

#[tokio::test]
async fn the_client_speaks_the_sdk_wire_to_a_mock() {
    let (root, log) = mock(OK).await;
    let kite = client(&root);
    let oid = kite
        .place_order(&OrderParams {
            variety: "regular".to_owned(),
            exchange: "NSE".to_owned(),
            tradingsymbol: "INFY".to_owned(),
            transaction_type: "BUY".to_owned(),
            quantity: 2,
            product: "CNC".to_owned(),
            order_type: "LIMIT".to_owned(),
            price: Some(PyValue::Float(1500.5)),
            validity: Some("DAY".to_owned()),
            ..OrderParams::default()
        })
        .await
        .unwrap();
    assert_eq!(oid, "151220000000000");
    kite.cancel_order("regular", "42", None).await.unwrap();
    kite.place_gtt(&GttParams {
        trigger_type: "single".to_owned(),
        tradingsymbol: "INFY".to_owned(),
        exchange: "NSE".to_owned(),
        trigger_values: vec![PyValue::Float(1400.0)],
        last_price: PyValue::Float(1500.0),
        orders: vec![GttOrderLeg {
            transaction_type: "SELL".to_owned(),
            quantity: 2,
            order_type: "LIMIT".to_owned(),
            product: "CNC".to_owned(),
            price: 1393.0,
        }],
    })
    .await
    .unwrap();
    kite.ltp(&["NSE:INFY".to_owned(), "NSE:TCS".to_owned()])
        .await
        .unwrap();
    let session = kite
        .generate_session("req", &Secret::new("sec"))
        .await
        .unwrap();
    assert!(matches!(
        session.get("login_time").unwrap(),
        PyValue::DateTime(_)
    ));

    let seen = log.lock().unwrap().clone();
    assert_eq!(seen.len(), 5);
    let first = |s: &Seen| s.head.lines().next().unwrap().to_owned();
    assert_eq!(first(&seen[0]), "POST /orders/regular HTTP/1.1");
    assert_eq!(
        seen[0].body,
        "variety=regular&exchange=NSE&tradingsymbol=INFY&transaction_type=BUY&quantity=2&product=CNC&order_type=LIMIT&price=1500.5&validity=DAY"
    );
    let lower = seen[0].head.to_ascii_lowercase();
    assert!(lower.contains("x-kite-version: 3"));
    assert!(lower.contains("authorization: token test-key:"));
    assert!(lower.contains("application/x-www-form-urlencoded"));
    assert_eq!(first(&seen[1]), "DELETE /orders/regular/42 HTTP/1.1");
    assert_eq!(first(&seen[2]), "POST /gtt/triggers HTTP/1.1");
    assert!(seen[2].body.starts_with("condition=%7B%22exchange%22"));
    assert!(seen[2].body.ends_with("&type=single"));
    assert_eq!(
        first(&seen[3]),
        "GET /quote/ltp?i=NSE%3AINFY&i=NSE%3ATCS HTTP/1.1"
    );
    assert_eq!(first(&seen[4]), "POST /session/token HTTP/1.1");
    assert!(seen[4].body.starts_with("api_key=test-key&request_token=req&checksum="));
    assert!(!seen[4].body.contains("sec&"));
}

#[tokio::test]
async fn an_error_envelope_raises_the_named_exception() {
    let (root, _log) = mock(
        r#"{"status":"error","error_type":"TokenException","message":"Incorrect `api_key` or `access_token`."}"#,
    )
    .await;
    let err = client(&root).holdings().await.unwrap_err();
    assert!(matches!(
        err,
        KiteError::Kite {
            kind: KiteExceptionKind::Token,
            code: 200,
            ..
        }
    ));
}
