//! The replay Kite: reads answer from the fixture as the SDK would parse them; writes are
//! recorded, never sent, and logged one `json.dumps` line each; secrets never reach the log.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_desk::fake_kite::{ReplayKite, read_log};
use baskfy_desk::kite::{
    GttOrderLeg, GttParams, KiteError, KiteExceptionKind, KiteReads, KiteWrites, OrderParams,
};
use baskfy_desk::wire::PyValue;
use baskfy_providers::secret::Secret;

const FIXTURE: &str = r#"{
  "session": {"authed": true},
  "routes": {
    "user.profile": {"data": {"user_id": "REPLAY", "user_name": "Replay Desk"}},
    "portfolio.holdings": {"data": [{"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 10, "t1_quantity": 2, "collateral_quantity": 3, "average_price": 1400.0, "last_price": 1500.5}]},
    "portfolio.positions": {"data": {"net": [], "day": []}},
    "user.margins": {"data": {"equity": {"available": {"live_balance": 250000.0}}}},
    "orders": {"data": [{"order_id": "1", "order_timestamp": "2026-10-30 09:16:01", "status": "COMPLETE"}]},
    "gtt.place": {"error": null},
    "order.margins.basket": {"error": {"type": "InputException", "message": "bad basket", "code": 400}}
  },
  "quotes": {"NSE:INFY": {"instrument_token": 408065, "last_price": 1500.5, "ohlc": {"open": 1490.0, "high": 1510.0, "low": 1480.0, "close": 1495.0}, "volume": 12345}},
  "historical": {"408065": {"day": [["2026-10-28T00:00:00+0530", 1, 2, 0.5, 1.5, 100], ["2026-10-29T00:00:00+0530", 1.5, 2.5, 1, 2, 200], ["2026-10-30T00:00:00+0530", 2, 3, 1.5, 2.5, 300]]}}
}"#;

fn replay() -> (tempfile::TempDir, ReplayKite) {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("kite.json"), FIXTURE).unwrap();
    std::fs::create_dir(dir.path().join("instruments")).unwrap();
    std::fs::write(
        dir.path().join("instruments/NSE.csv"),
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n408065,1594,INFY,INFOSYS,0,,0,0.1,1,EQ,NSE,NSE\n",
    )
    .unwrap();
    let log = dir.path().join("calls.jsonl");
    let k = ReplayKite::load(dir.path(), Some(log), "test-api-key").unwrap();
    (dir, k)
}

#[tokio::test]
async fn reads_come_from_the_fixture_in_the_sdks_shape() {
    let (_d, k) = replay();
    assert!(k.profile().await.is_ok());
    let ltp = k
        .ltp(&["NSE:INFY".into(), "NSE:NOPE".into(), "NSE:INFY".into()])
        .await
        .unwrap();
    assert_eq!(
        baskfy_desk::wire::orjson_dumps(&ltp).unwrap(),
        r#"{"NSE:INFY":{"instrument_token":408065,"last_price":1500.5}}"#
    );
    let candles = k
        .historical_data(408_065, "2026-10-29", "2026-10-30", "day", false, false)
        .await
        .unwrap();
    let PyValue::List(rows) = candles else {
        panic!()
    };
    assert_eq!(rows.len(), 2);
    let PyValue::List(inst) = k.instruments(Some("NSE")).await.unwrap() else {
        panic!()
    };
    assert_eq!(inst.len(), 1);
    let PyValue::List(orders) = k.orders().await.unwrap() else {
        panic!()
    };
    let PyValue::Map(o) = &orders[0] else {
        panic!()
    };
    assert!(
        matches!(o.get("order_timestamp"), Some(PyValue::DateTime(_))),
        "_format_response parsed it"
    );
    let err = k.basket_order_margins(&[], true, None).await.unwrap_err();
    assert!(matches!(
        err,
        KiteError::Kite {
            kind: KiteExceptionKind::Input,
            code: 400,
            ..
        }
    ));
    let missing = k.trades().await.unwrap_err();
    assert!(
        missing
            .to_string()
            .contains("no recording for route 'trades'")
    );
}

#[tokio::test]
async fn writes_are_recorded_not_sent_and_logged_as_python_would() {
    let (dir, k) = replay();
    let order = OrderParams {
        variety: "regular".into(),
        exchange: "NSE".into(),
        tradingsymbol: "INFY".into(),
        transaction_type: "BUY".into(),
        quantity: 5,
        product: "CNC".into(),
        order_type: "LIMIT".into(),
        price: Some(PyValue::Float(1500.5)),
        validity: Some("DAY".into()),
        ..OrderParams::default()
    };
    assert_eq!(k.place_order(&order).await.unwrap(), "REPLAY000001");
    let gtt = GttParams {
        trigger_type: "single".into(),
        tradingsymbol: "INFY".into(),
        exchange: "NSE".into(),
        trigger_values: vec![PyValue::Float(1380.0)],
        last_price: PyValue::Float(1500.5),
        orders: vec![GttOrderLeg {
            transaction_type: "SELL".into(),
            quantity: 5,
            order_type: "LIMIT".into(),
            product: "CNC".into(),
            price: 1373.1,
        }],
    };
    let placed = k.place_gtt(&gtt).await.unwrap();
    assert_eq!(
        baskfy_desk::wire::orjson_dumps(&placed).unwrap(),
        r#"{"trigger_id":900001}"#
    );
    k.delete_gtt(900_001).await.unwrap();
    k.generate_session("req-token", &Secret::new("test-secret".to_owned()))
        .await
        .unwrap();
    assert_eq!(k.write_calls().len(), 3);
    let log = read_log(&dir.path().join("calls.jsonl")).unwrap();
    assert_eq!(log.len(), 4);
    let first = std::fs::read_to_string(dir.path().join("calls.jsonl")).unwrap();
    let line = first.lines().next().unwrap();
    assert_eq!(
        line,
        r#"{"seq": 1, "route": "order.place", "method": "POST", "url_args": {"variety": "regular"}, "params": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 5, "product": "CNC", "order_type": "LIMIT", "price": 1500.5, "validity": "DAY"}, "is_json": false, "query_params": null}"#
    );
    let gtt_line = first.lines().nth(1).unwrap();
    assert!(gtt_line.contains(r#""condition": "{\"exchange\": \"NSE\", \"tradingsymbol\": \"INFY\", \"trigger_values\": [1380.0], \"last_price\": 1500.5}""#), "{gtt_line}");
    let session = first.lines().nth(3).unwrap();
    assert!(
        session.contains(r#""api_key": "<redacted>""#)
            && session.contains(r#""request_token": "<redacted>""#)
    );
    assert!(
        !first.contains("test-secret")
            && !first.contains("test-api-key")
            && !first.contains("req-token")
    );
}

#[tokio::test]
async fn a_logged_out_fixture_answers_token_exception() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("kite.json"),
        r#"{"session": {"authed": false}, "routes": {}}"#,
    )
    .unwrap();
    let k = ReplayKite::load(dir.path(), None, "k").unwrap();
    let err = k.profile().await.unwrap_err();
    assert_eq!(err.python_type(), "TokenException");
}
