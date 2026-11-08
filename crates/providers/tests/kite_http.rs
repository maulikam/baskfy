//! `KiteConnect` on the wire, against a loopback server (the network block allows loopback;
//! nothing here can reach Kite). Every expected request line, query, header value and body
//! below was captured from the live Python — `kiteconnect` 5.2.1 over `requests` 2.34.2,
//! `KiteConnect(api_key="test-key", timeout=30)` with access token `tok`, through a capturing
//! `requests` adapter (29 Oct 2026) — and every expected parse is what that SDK returned for the
//! same body. Header names are compared case-insensitively (HTTP's rule; hyper writes them
//! lower-case where `requests` title-cases them).
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

mod common;
mod kite_common;

use std::sync::Arc;

use baskfy_providers::errors::ErrorKind;
use baskfy_providers::kite::{
    HistoricalBound, KiteClient, KiteConnect, KiteError, KiteExceptionKind, KiteMap, KiteProvider,
    KiteRuntime, KiteValue,
};
use baskfy_providers::records::PyDateTime;
use baskfy_providers::secret::Secret;
use common::arm;
use kite_common::{
    Canned, Captured, CountingLimiter, MockKite, aware, configured_settings, date, dict, naive,
    ref_, stored_token,
};

/// The headers Python sends on every GET, in its order.
const PYTHON_HEADERS: [(&str, &str); 6] = [
    ("User-Agent", "Kiteconnect-python/5.2.1"),
    ("Accept-Encoding", "gzip, deflate"),
    ("Accept", "*/*"),
    ("Connection", "keep-alive"),
    ("X-Kite-Version", "3"),
    ("Authorization", "token test-key:tok"),
];

async fn client(responses: Vec<Canned>) -> (MockKite, KiteConnect) {
    let server = MockKite::start(responses).await;
    let client = KiteConnect::with_root("test-key", &server.root(), Some(30.0)).unwrap();
    client.set_access_token(&Secret::new("tok"));
    (server, client)
}

fn assert_python_headers(request: &Captured, extra: &[(&str, &str)]) {
    let mut expected: Vec<(String, String)> = PYTHON_HEADERS
        .iter()
        .chain(extra.iter())
        .map(|(k, v)| (k.to_lowercase(), (*v).to_owned()))
        .collect();
    let mut got: Vec<(String, String)> = request
        .headers
        .iter()
        .filter(|(k, _)| !k.eq_ignore_ascii_case("host"))
        .map(|(k, v)| (k.to_lowercase(), v.clone()))
        .collect();
    // The order Python sends them in, too.
    let python_order: Vec<String> = expected.iter().map(|(k, _)| k.clone()).collect();
    let got_order: Vec<String> = got.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(got_order, python_order, "header order");
    expected.sort();
    got.sort();
    assert_eq!(got, expected);
    assert_eq!(request.version, "HTTP/1.1");
}

fn only_request(server: &MockKite) -> Captured {
    let requests = server.requests();
    assert_eq!(requests.len(), 1, "{requests:?}");
    requests.into_iter().next().unwrap()
}

// --- routes, queries, headers --------------------------------------------------------------

const DUMP: &str = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,\
tick_size,lot_size,instrument_type,segment,exchange\n408065,1594,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,\
NSE\n12345,48,NIFTY26OCT25000CE,NIFTY,101.5,2026-10-27,25000,0.05,65,CE,NFO-OPT,NFO\n";

#[tokio::test]
async fn instruments_for_an_exchange() {
    arm();
    let (server, client) = client(vec![Canned::with(200, Some("text/csv"), DUMP.as_bytes())]).await;
    let rows = client.instruments(Some("NSE")).await.unwrap();
    let request = only_request(&server);
    assert_eq!(request.method, "GET");
    assert_eq!(request.target, "/instruments/NSE");
    assert!(request.body.is_empty());
    assert_python_headers(&request, &[]);
    // [{'instrument_token': 408065, 'exchange_token': '1594', 'tradingsymbol': 'INFY',
    //   'name': 'INFOSYS', 'last_price': 0.0, 'expiry': '', 'strike': 0.0, 'tick_size': 0.05,
    //   'lot_size': 1, 'instrument_type': 'EQ', 'segment': 'NSE', 'exchange': 'NSE'},
    //  {..., 'last_price': 101.5, 'expiry': datetime.date(2026, 10, 27), 'strike': 25000.0, ...}]
    let expected = KiteValue::List(vec![
        dict(vec![
            ("instrument_token", 408_065_i64.into()),
            ("exchange_token", "1594".into()),
            ("tradingsymbol", "INFY".into()),
            ("name", "INFOSYS".into()),
            ("last_price", 0.0.into()),
            ("expiry", "".into()),
            ("strike", 0.0.into()),
            ("tick_size", 0.05.into()),
            ("lot_size", 1_i64.into()),
            ("instrument_type", "EQ".into()),
            ("segment", "NSE".into()),
            ("exchange", "NSE".into()),
        ]),
        dict(vec![
            ("instrument_token", 12_345_i64.into()),
            ("exchange_token", "48".into()),
            ("tradingsymbol", "NIFTY26OCT25000CE".into()),
            ("name", "NIFTY".into()),
            ("last_price", 101.5.into()),
            ("expiry", date(2026, 10, 27).into()),
            ("strike", 25_000.0.into()),
            ("tick_size", 0.05.into()),
            ("lot_size", 65_i64.into()),
            ("instrument_type", "CE".into()),
            ("segment", "NFO-OPT".into()),
            ("exchange", "NFO".into()),
        ]),
    ]);
    assert_eq!(rows, expected);
    assert_eq!(
        rows.py_repr(),
        "[{'instrument_token': 408065, 'exchange_token': '1594', 'tradingsymbol': 'INFY', \
         'name': 'INFOSYS', 'last_price': 0.0, 'expiry': '', 'strike': 0.0, 'tick_size': 0.05, \
         'lot_size': 1, 'instrument_type': 'EQ', 'segment': 'NSE', 'exchange': 'NSE'}, \
         {'instrument_token': 12345, 'exchange_token': '48', 'tradingsymbol': \
         'NIFTY26OCT25000CE', 'name': 'NIFTY', 'last_price': 101.5, 'expiry': \
         datetime.date(2026, 10, 27), 'strike': 25000.0, 'tick_size': 0.05, 'lot_size': 65, \
         'instrument_type': 'CE', 'segment': 'NFO-OPT', 'exchange': 'NFO'}]"
    );
}

#[tokio::test]
async fn instruments_for_every_exchange() {
    arm();
    let header_only = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,\
                       strike,tick_size,lot_size,instrument_type,segment,exchange\n";
    let (server, client) = client(vec![Canned::with(
        200,
        Some("text/csv"),
        header_only.as_bytes(),
    )])
    .await;
    assert_eq!(
        client.instruments(None).await.unwrap(),
        KiteValue::List(Vec::new())
    );
    assert_eq!(only_request(&server).target, "/instruments");
}

#[tokio::test]
async fn historical_by_day() {
    arm();
    let body = r#"{"status":"success","data":{"candles":[["2026-01-01T00:00:00+0530",1500.5,1510,1490.25,1505.75,123456],["2026-01-02T00:00:00+0530",1505,1520,1500,1515,654321,77]]}}"#;
    let (server, client) = client(vec![Canned::json(body)]).await;
    let candles = client
        .historical_data(
            408_065,
            HistoricalBound::Date(date(2026, 1, 1)),
            HistoricalBound::Date(date(2026, 3, 1)),
            "day",
        )
        .await
        .unwrap();
    let request = only_request(&server);
    assert_eq!(
        request.target,
        "/instruments/historical/408065/day?from=2026-01-01&to=2026-03-01&interval=day&continuous=0&oi=0"
    );
    assert_python_headers(&request, &[]);
    let ist = 19_800;
    assert_eq!(
        candles,
        KiteValue::List(vec![
            dict(vec![
                ("date", aware(naive(2026, 1, 1, 0, 0, 0), ist).into()),
                ("open", 1500.5.into()),
                ("high", 1510_i64.into()),
                ("low", 1490.25.into()),
                ("close", 1505.75.into()),
                ("volume", 123_456_i64.into()),
            ]),
            dict(vec![
                ("date", aware(naive(2026, 1, 2, 0, 0, 0), ist).into()),
                ("open", 1505_i64.into()),
                ("high", 1520_i64.into()),
                ("low", 1500_i64.into()),
                ("close", 1515_i64.into()),
                ("volume", 654_321_i64.into()),
                ("oi", 77_i64.into()),
            ]),
        ])
    );
    assert_eq!(
        candles.py_repr(),
        "[{'date': datetime.datetime(2026, 1, 1, 0, 0, tzinfo=tzoffset(None, 19800)), 'open': \
         1500.5, 'high': 1510, 'low': 1490.25, 'close': 1505.75, 'volume': 123456}, {'date': \
         datetime.datetime(2026, 1, 2, 0, 0, tzinfo=tzoffset(None, 19800)), 'open': 1505, \
         'high': 1520, 'low': 1500, 'close': 1515, 'volume': 654321, 'oi': 77}]"
    );
}

#[tokio::test]
async fn historical_by_minute() {
    arm();
    let body = r#"{"status":"success","data":{"candles":[["2026-09-22T09:15:00+0530",25000,25010,24990,25005,0]]}}"#;
    let (server, client) = client(vec![Canned::json(body)]).await;
    client
        .historical_data(
            256_265,
            HistoricalBound::DateTime(naive(2026, 9, 22, 9, 15, 0)),
            HistoricalBound::DateTime(naive(2026, 9, 22, 23, 59, 59)),
            "minute",
        )
        .await
        .unwrap();
    assert_eq!(
        only_request(&server).target,
        "/instruments/historical/256265/minute?from=2026-09-22+09%3A15%3A00&to=2026-09-22+23%3A59%3A59&interval=minute&continuous=0&oi=0"
    );
}

#[tokio::test]
async fn the_ledger_routes() {
    arm();
    let (server, client) = client(vec![
        Canned::json(
            r#"{"status":"success","data":[{"tradingsymbol":"SBIN","quantity":60,"average_price":512.25}]}"#,
        ),
        Canned::json(r#"{"status":"success","data":{"net":12345.67}}"#),
        Canned::json(r#"{"status":"success","data":{"equity":{"net":1.5}}}"#),
        Canned::json(r#"{"status":"success","data":{"user_id":"AB1234"}}"#),
    ])
    .await;
    let holdings = client.holdings().await.unwrap();
    assert_eq!(
        holdings.py_repr(),
        "[{'tradingsymbol': 'SBIN', 'quantity': 60, 'average_price': 512.25}]"
    );
    assert_eq!(
        client.margins(Some("equity")).await.unwrap().py_repr(),
        "{'net': 12345.67}"
    );
    assert_eq!(
        client.margins(None).await.unwrap().py_repr(),
        "{'equity': {'net': 1.5}}"
    );
    assert_eq!(
        client.profile().await.unwrap().py_repr(),
        "{'user_id': 'AB1234'}"
    );
    let targets: Vec<String> = server.requests().into_iter().map(|r| r.target).collect();
    assert_eq!(
        targets,
        vec![
            "/portfolio/holdings",
            "/user/margins/equity",
            "/user/margins",
            "/user/profile"
        ]
    );
    for request in server.requests() {
        assert_eq!(request.method, "GET");
        assert_python_headers(&request, &[]);
    }
}

#[tokio::test]
async fn trades_parse_nineteen_character_stamps() {
    arm();
    let body = r#"{"status":"success","data":[{"trade_id":"T1","fill_timestamp":"2026-09-11 10:05:07","order_timestamp":"2026-09-11T10:05:07","exchange_timestamp":null}]}"#;
    let (server, client) = client(vec![Canned::json(body)]).await;
    let trades = client.trades().await.unwrap();
    assert_eq!(only_request(&server).target, "/trades");
    assert_eq!(
        trades.py_repr(),
        "[{'trade_id': 'T1', 'fill_timestamp': datetime.datetime(2026, 9, 11, 10, 5, 7), \
         'order_timestamp': datetime.datetime(2026, 9, 11, 10, 5, 7), 'exchange_timestamp': None}]"
    );
}

#[tokio::test]
async fn quote_encodes_keys_like_requests_and_keeps_kites_order() {
    arm();
    let body = r#"{"status":"success","data":{"NSE:NIFTY 50":{"last_price":25012.35,"timestamp":"2026-09-22 10:00:05"},"NSE:INFY":{"last_price":1500.1,"last_trade_time":"2026-09-22 10:00:01","ohlc":{"close":1490}}}}"#;
    let (server, client) = client(vec![Canned::json(body)]).await;
    let keys: Vec<String> = ["NSE:INFY", "NSE:NIFTY 50", "NFO:M&M26OCTFUT", "BSE:A~B*C"]
        .into_iter()
        .map(str::to_owned)
        .collect();
    let quotes = client.quote(&keys).await.unwrap();
    assert_eq!(
        only_request(&server).target,
        "/quote?i=NSE%3AINFY&i=NSE%3ANIFTY+50&i=NFO%3AM%26M26OCTFUT&i=BSE%3AA~B%2AC"
    );
    assert_eq!(
        quotes.py_repr(),
        "{'NSE:NIFTY 50': {'last_price': 25012.35, 'timestamp': datetime.datetime(2026, 9, 22, \
         10, 0, 5)}, 'NSE:INFY': {'last_price': 1500.1, 'last_trade_time': \
         datetime.datetime(2026, 9, 22, 10, 0, 1), 'ohlc': {'close': 1490}}}"
    );
}

/// `{key: ... for key in data}` over a payload that is not a dict, as the live SDK answers it
/// (29 Oct 2026): `[]` and `""` are `{}`; `["NSE:X"]`, `"ab"`, `null` and `5` are TypeErrors.
#[tokio::test]
async fn a_quote_payload_that_is_not_a_dict_fails_as_the_sdk_does() {
    arm();
    let cases: [(&str, Option<&str>); 6] = [
        ("[]", None),
        (r#""""#, None),
        (
            r#"["NSE:X"]"#,
            Some("list indices must be integers or slices, not str"),
        ),
        (
            r#""ab""#,
            Some("string indices must be integers, not 'str'"),
        ),
        ("null", Some("'NoneType' object is not iterable")),
        ("5", Some("'int' object is not iterable")),
    ];
    for (data, error) in cases {
        let body = format!(r#"{{"status":"success","data":{data}}}"#);
        let (_server, client) = client(vec![Canned::json(&body)]).await;
        let got = client.quote(&["NSE:X".to_owned()]).await;
        match (got, error) {
            (Ok(value), None) => assert_eq!(value, KiteValue::Map(KiteMap::new()), "{data}"),
            (Err(e), Some(message)) => {
                assert_eq!(e.python_type(), "TypeError", "{data}");
                assert_eq!(e.to_string(), message, "{data}");
            }
            (got, _) => panic!("{data}: {got:?}"),
        }
    }
}

#[tokio::test]
async fn ltp_and_ohlc_routes() {
    arm();
    let (server, client) = client(vec![
        Canned::json(
            r#"{"status":"success","data":{"NSE:INFY":{"instrument_token":408065,"last_price":1500.1}}}"#,
        ),
        Canned::json(r#"{"status":"success","data":{}}"#),
    ])
    .await;
    let keys = vec!["NSE:INFY".to_owned()];
    assert_eq!(
        client.ltp(&keys).await.unwrap().py_repr(),
        "{'NSE:INFY': {'instrument_token': 408065, 'last_price': 1500.1}}"
    );
    assert_eq!(client.ohlc(&keys).await.unwrap().py_repr(), "{}");
    let targets: Vec<String> = server.requests().into_iter().map(|r| r.target).collect();
    assert_eq!(
        targets,
        vec!["/quote/ltp?i=NSE%3AINFY", "/quote/ohlc?i=NSE%3AINFY"]
    );
}

#[tokio::test]
async fn the_basket_margin_calculation_posts_pythons_json() {
    arm();
    let body = r#"{"status":"success","data":{"initial":{"total":180000.5},"final":{"total":42000.25},"orders":[]}}"#;
    let (server, client) = client(vec![Canned::json(body)]).await;
    let leg = |symbol: &str, side: &str, order_type: &str, price: f64| {
        dict(vec![
            ("exchange", "NFO".into()),
            ("tradingsymbol", symbol.into()),
            ("transaction_type", side.into()),
            ("variety", "regular".into()),
            ("product", "MIS".into()),
            ("order_type", order_type.into()),
            ("quantity", 65_i64.into()),
            ("price", price.into()),
        ])
    };
    let params = vec![
        leg("W", "BUY", "MARKET", 0.0),
        leg("S\u{e9}", "SELL", "LIMIT", 101.5),
    ];
    let answer = client
        .basket_order_margins(&params, false, None)
        .await
        .unwrap();
    let request = only_request(&server);
    assert_eq!(request.method, "POST");
    assert_eq!(request.target, "/margins/basket?consider_positions=False");
    let python_body = "[{\"exchange\": \"NFO\", \"tradingsymbol\": \"W\", \"transaction_type\": \"BUY\", \"variety\": \"regular\", \"product\": \"MIS\", \"order_type\": \"MARKET\", \"quantity\": 65, \"price\": 0.0}, {\"exchange\": \"NFO\", \"tradingsymbol\": \"S\\u00e9\", \"transaction_type\": \"SELL\", \"variety\": \"regular\", \"product\": \"MIS\", \"order_type\": \"LIMIT\", \"quantity\": 65, \"price\": 101.5}]";
    assert_eq!(
        String::from_utf8(request.body.clone()).unwrap(),
        python_body
    );
    assert_eq!(request.body.len(), 336);
    assert_python_headers(
        &request,
        &[
            ("Content-Type", "application/json"),
            ("Content-Length", "336"),
        ],
    );
    assert_eq!(
        answer.py_repr(),
        "{'initial': {'total': 180000.5}, 'final': {'total': 42000.25}, 'orders': []}"
    );
}

#[tokio::test]
async fn no_authorization_header_before_a_token_is_set() {
    arm();
    let server = MockKite::start(vec![Canned::json(r#"{"status":"success","data":[]}"#)]).await;
    let client = KiteConnect::with_root("test-key", &server.root(), Some(30.0)).unwrap();
    client.holdings().await.unwrap();
    assert_eq!(only_request(&server).header("authorization"), None);
}

// --- response validation: the SDK's errors -------------------------------------------------

async fn holdings_error(canned: Canned) -> KiteError {
    let (_server, client) = client(vec![canned]).await;
    client.holdings().await.unwrap_err()
}

#[tokio::test]
async fn a_kite_error_body_raises_its_named_class_with_the_status() {
    arm();
    let err = holdings_error(Canned::with(
        403,
        Some("application/json"),
        br#"{"status":"error","message":"Incorrect `api_key` or `access_token`.","error_type":"TokenException"}"#,
    ))
    .await;
    let KiteError::Kite {
        kind,
        message,
        code,
    } = &err
    else {
        panic!("{err:?}");
    };
    assert_eq!(*kind, KiteExceptionKind::Token);
    assert_eq!(message, "Incorrect `api_key` or `access_token`.");
    assert_eq!(*code, 403);
}

#[tokio::test]
async fn an_unknown_error_type_is_a_general_exception() {
    arm();
    let err = holdings_error(Canned::json(
        r#"{"status":"error","message":"boom","error_type":"WhateverException"}"#,
    ))
    .await;
    assert_eq!(err.python_type(), "GeneralException");
    assert_eq!(err.to_string(), "boom");
}

#[tokio::test]
async fn an_error_without_a_type_is_pythons_type_error() {
    arm();
    let err = holdings_error(Canned::json(r#"{"status":"error","message":"boom"}"#)).await;
    assert_eq!(err.python_type(), "TypeError");
    assert_eq!(
        err.to_string(),
        "attribute name must be string, not 'NoneType'"
    );
}

#[tokio::test]
async fn unparseable_json_is_a_data_exception() {
    arm();
    let err = holdings_error(Canned::with(200, Some("application/json"), b"<html>nope")).await;
    assert_eq!(err.python_type(), "DataException");
    assert_eq!(
        err.to_string(),
        "Couldn't parse the JSON response received from the server: b'<html>nope'"
    );
}

#[tokio::test]
async fn an_unknown_content_type_is_a_data_exception() {
    arm();
    let err = holdings_error(Canned::with(200, Some("text/html"), b"<html>502</html>")).await;
    assert_eq!(err.python_type(), "DataException");
    assert_eq!(
        err.to_string(),
        "Unknown Content-Type (text/html) with response: (b'<html>502</html>')"
    );
}

#[tokio::test]
async fn no_content_type_is_a_key_error() {
    arm();
    let err = holdings_error(Canned::with(200, None, b"{}")).await;
    assert_eq!(err.python_type(), "KeyError");
    assert_eq!(err.to_string(), "'content-type'");
}

#[tokio::test]
async fn a_success_without_data_is_a_key_error() {
    arm();
    let err = holdings_error(Canned::json(r#"{"status":"success"}"#)).await;
    assert_eq!(err.python_type(), "KeyError");
    assert_eq!(err.to_string(), "'data'");
}

#[tokio::test]
async fn a_list_body_is_an_attribute_error() {
    arm();
    let err = holdings_error(Canned::json("[1,2]")).await;
    assert_eq!(err.python_type(), "AttributeError");
    assert_eq!(err.to_string(), "'list' object has no attribute 'get'");
}

// --- the provider over the wire ------------------------------------------------------------

fn http_provider(server: &MockKite, env: &kite_common::Env, timeout: f64) -> KiteProvider {
    let root = server.root();
    KiteProvider::new(
        env.settings.clone(),
        KiteRuntime {
            rate_limiter: Some(Arc::new(CountingLimiter::default())),
            client_factory: Some(Arc::new(move |api_key: &str| {
                let client: Arc<dyn KiteClient> =
                    Arc::new(KiteConnect::with_root(api_key, &root, Some(timeout))?);
                Ok(client)
            })),
            token_store: Some(stored_token(&env.settings)),
            ..KiteRuntime::default()
        },
    )
    .unwrap()
}

#[tokio::test]
async fn the_provider_reads_holdings_over_http_with_its_token() {
    arm();
    let env = configured_settings();
    let server = MockKite::start(vec![Canned::json(
        r#"{"status":"success","data":[{"tradingsymbol":"SBIN","exchange":"NSE","quantity":60,"t1_quantity":10,"collateral_quantity":30,"average_price":512.25,"last_price":540.1}]}"#,
    )])
    .await;
    let provider = http_provider(&server, &env, 30.0);
    let rows = provider.broker_holdings(&ref_()).await.unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].total_quantity().to_string(), "100");
    assert_eq!(rows[0].average_price.unwrap().to_string(), "512.25");
    let request = only_request(&server);
    assert_eq!(request.target, "/portfolio/holdings");
    assert_eq!(
        request.header("authorization"),
        Some("token test-key:fresh-token")
    );
}

#[tokio::test]
async fn a_token_rejected_on_the_wire_is_loud_and_not_retried() {
    arm();
    let env = configured_settings();
    let server = MockKite::start(vec![Canned::with(
        403,
        Some("application/json"),
        br#"{"status":"error","message":"Token is invalid or has expired.","error_type":"TokenException"}"#,
    )])
    .await;
    let provider = http_provider(&server, &env, 30.0);
    let err = provider.broker_holdings(&ref_()).await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::AccessTokenExpired);
    assert_eq!(server.requests().len(), 1);
}

#[tokio::test]
async fn a_read_timeout_passes_through_untranslated_and_unretried_known_bug() {
    arm();
    // KNOWN BUG (DECISIONS-RS R4.10), reproduced: `requests.exceptions.ReadTimeout` is not the
    // builtin `TimeoutError` the translation table names, so Python neither translates nor
    // retries it. One request, and `ReadTimeout` surfaces as itself.
    let env = configured_settings();
    let server = MockKite::start(vec![Canned::hang()]).await;
    let provider = http_provider(&server, &env, 0.3);
    let err = provider.broker_holdings(&ref_()).await.unwrap_err();
    assert_eq!(err.python_type(), "ReadTimeout");
    assert!(!err.is_transient());
    assert_eq!(server.requests().len(), 1);
}

#[tokio::test]
async fn daily_bars_over_http_carry_kites_stamps_into_the_frame() {
    arm();
    let env = configured_settings();
    let body = r#"{"status":"success","data":{"candles":[["2026-01-01T00:00:00+0530",1500.5,1510,1490.25,1505.75,123456],["2026-01-02T00:00:00+0530",1505.123456,1520,1500,1515.00005,654321]]}}"#;
    let server = MockKite::start(vec![Canned::json(body)]).await;
    let provider = http_provider(&server, &env, 30.0);
    let frame = provider
        .daily_bars(408_065, date(2026, 1, 1), date(2026, 1, 2))
        .await
        .unwrap();
    assert_eq!(frame.height(), 2);
    let open = frame.column("open").unwrap();
    let close = frame.column("close").unwrap();
    let show = |c: &polars::prelude::Column, i: usize| match c.get(i).unwrap() {
        polars::prelude::AnyValue::Decimal(m, _, s) => {
            rust_decimal::Decimal::from_i128_with_scale(m, u32::try_from(s).unwrap()).to_string()
        }
        other => panic!("{other:?}"),
    };
    // polars' strict cast onto Decimal(18, 4) rounds half to even (1515.00005 -> 1515.0000).
    assert_eq!(show(open, 0), "1500.5000");
    assert_eq!(show(open, 1), "1505.1235");
    assert_eq!(show(close, 1), "1515.0000");
    assert_eq!(
        only_request(&server).target,
        "/instruments/historical/408065/day?from=2026-01-01&to=2026-01-02&interval=day&continuous=0&oi=0"
    );
}

#[test]
fn values_print_as_python_prints_them() {
    arm();
    let stamp = KiteValue::DateTime(PyDateTime::Naive(naive(2026, 9, 22, 10, 0, 5)));
    assert_eq!(stamp.py_str(), "2026-09-22 10:00:05");
    let mut map = KiteMap::new();
    map.insert("a", KiteValue::None);
    map.insert("b", true.into());
    map.insert("a", 1e16.into());
    assert_eq!(KiteValue::Map(map).py_repr(), "{'a': 1e+16, 'b': True}");
}
