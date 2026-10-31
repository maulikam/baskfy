#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic, clippy::print_stderr)]
//! The consumer against the parity Redis (db 13 only).

use std::time::Duration;

use baskfy_db::OrderedJson;
use baskfy_worker::pyjson;
use baskfy_worker::runtime::celery::{self, ArgsRepr, Outgoing};
use baskfy_worker::runtime::consumer::{ServeConfig, serve};
use baskfy_worker::settings::WorkerSettings;

const URL: &str = "redis://127.0.0.1:56379/13";

fn fixture(key: &str) -> OrderedJson {
    let all =
        OrderedJson::parse(include_str!("fixtures/celery_messages.json")).unwrap();
    all.get(key).unwrap().clone()
}

async fn meta(conn: &mut redis::aio::MultiplexedConnection, id: &str) -> Option<OrderedJson> {
    let text: Option<String> = redis::cmd("GET")
        .arg(celery::meta_key(id))
        .query_async(conn)
        .await
        .unwrap();
    text.map(|t| OrderedJson::parse(&t).unwrap())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_consumer_runs_rejects_and_revokes_like_celery() {
    let client = redis::Client::open(URL).unwrap();
    let Ok(mut conn) = client.get_multiplexed_async_connection().await else {
        eprintln!("parity Redis at {URL} is not reachable; skipped");
        return;
    };
    let _: () = redis::cmd("FLUSHDB").query_async(&mut conn).await.unwrap();

    let python = pyjson::dumps(&fixture("task_time_limit")).unwrap();
    let probe_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

    let mut unknown = Outgoing::new("baskfy.nope", Vec::new(), ArgsRepr::Tuple, Vec::new(), "default").unwrap();
    unknown.task_id = "u-1".to_owned();
    let mut expired = Outgoing::new(
        "baskfy.swing.timing_probe",
        Vec::new(),
        ArgsRepr::Tuple,
        Vec::new(),
        "default",
    )
    .unwrap();
    expired.task_id = "x-1".to_owned();
    expired.expires = Some(
        chrono::DateTime::parse_from_rfc3339("2026-01-01T00:00:00+05:30").unwrap(),
    );
    let mut bad_args = Outgoing::new(
        "baskfy.swing.timing_probe",
        vec![OrderedJson::Int(1)],
        ArgsRepr::List,
        Vec::new(),
        "default",
    )
    .unwrap();
    bad_args.task_id = "t-1".to_owned();

    let mut pushed: Vec<String> = vec![python];
    for m in [&unknown, &expired, &bad_args] {
        pushed.push(m.encode().unwrap());
    }
    for text in &pushed {
        let _: i64 = redis::cmd("LPUSH")
            .arg("default")
            .arg(text)
            .query_async(&mut conn)
            .await
            .unwrap();
    }

    let worker = WorkerSettings {
        redis_url: URL.to_owned(),
        ..WorkerSettings::default()
    };
    let cfg = ServeConfig::new(
        baskfy_api::Settings::default(),
        worker,
        vec!["default".to_owned()],
        1,
    );
    assert_eq!(cfg.broker_url, URL);
    let (warm_tx, warm_rx) = tokio::sync::watch::channel(false);
    let (_cold_tx, cold_rx) = tokio::sync::watch::channel(false);
    let handle = tokio::spawn(serve(cfg, warm_rx, cold_rx));

    let ids = [probe_id, "u-1", "x-1", "t-1"];
    let mut done = false;
    for _ in 0..100 {
        let mut all = true;
        for id in ids {
            let m = meta(&mut conn, id).await;
            let finished = m
                .and_then(|m| m.get("status").cloned())
                .is_some_and(|s| s != pyjson::s("STARTED"));
            all &= finished;
        }
        if all {
            done = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    warm_tx.send(true).unwrap();
    let served = tokio::time::timeout(Duration::from_secs(10), handle)
        .await
        .unwrap()
        .unwrap();
    assert!(served.is_ok(), "{served:?}");
    assert!(done, "not every message finished");

    let probe = meta(&mut conn, probe_id).await.unwrap();
    assert_eq!(probe.get("status"), Some(&pyjson::s("SUCCESS")));
    assert_eq!(
        probe.get("result").and_then(|r| r.get("skipped")),
        Some(&pyjson::s("BASKFY_SWING_TIMING_PROBE is false"))
    );
    assert_eq!(probe.get("task_id"), Some(&pyjson::s(probe_id)));

    let unknown = meta(&mut conn, "u-1").await.unwrap();
    assert_eq!(unknown.get("status"), Some(&pyjson::s("FAILURE")));
    assert_eq!(
        unknown.get("result").and_then(|r| r.get("exc_type")),
        Some(&pyjson::s("NotRegistered"))
    );

    let revoked = meta(&mut conn, "x-1").await.unwrap();
    assert_eq!(revoked.get("status"), Some(&pyjson::s("REVOKED")));

    let typed = meta(&mut conn, "t-1").await.unwrap();
    assert_eq!(typed.get("status"), Some(&pyjson::s("FAILURE")));
    assert_eq!(
        typed.get("result").and_then(|r| r.get("exc_type")),
        Some(&pyjson::s("TypeError"))
    );

    let ttl: i64 = redis::cmd("TTL")
        .arg(celery::meta_key(probe_id))
        .query_async(&mut conn)
        .await
        .unwrap();
    assert!(ttl > 600_000 && ttl <= 604_800);
    let unacked: i64 = redis::cmd("HLEN").arg(celery::UNACKED).query_async(&mut conn).await.unwrap();
    let index: i64 = redis::cmd("ZCARD").arg(celery::UNACKED_INDEX).query_async(&mut conn).await.unwrap();
    let left: i64 = redis::cmd("LLEN").arg("default").query_async(&mut conn).await.unwrap();
    assert_eq!((unacked, index, left), (0, 0, 0));
    let _: () = redis::cmd("FLUSHDB").query_async(&mut conn).await.unwrap();
}
