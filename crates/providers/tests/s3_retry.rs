//! DECISIONS-RS R4.14, closed: the hand-signed S3 client retries exactly as the Python archive's
//! `boto3.client("s3", ...)` does — botocore 1.43's **legacy** mode (no `retries` config, no
//! `AWS_RETRY_MODE`): five attempts in all, `random() * 2 ** (attempt - 1)` seconds between
//! them, and only the answers `_retry.json` names for `s3`. Driven against a local stub server
//! on loopback (the network block stays armed) with a recording sleeper and a fixed draw.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

mod common;

use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};

use baskfy_providers::archive::{
    PutObject, S3_MAX_ATTEMPTS, S3ClientLike, SigV4S3Client, s3_error_code, s3_retryable,
};
use baskfy_providers::secret::Secret;
use common::{FixedRandom, ManualClock, RecordingSleeper, arm};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// One scripted reply: a status line with a body, or a dropped connection.
#[derive(Clone, Debug)]
enum Reply {
    Status(u16, &'static str),
    Drop,
}

/// A stub S3 on 127.0.0.1 answering each request with the next scripted reply (the last one
/// repeats), recording each request's method and whether it carried a fresh signature.
struct Stub {
    url: String,
    seen: Arc<Mutex<Vec<String>>>,
}

async fn stub(script: Vec<Reply>) -> Stub {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let replies = Arc::new(Mutex::new(VecDeque::from(script)));
    let seen_in = Arc::clone(&seen);
    tokio::spawn(async move {
        loop {
            let Ok((mut socket, _)) = listener.accept().await else {
                return;
            };
            let mut buf = vec![0u8; 65536];
            let mut got = Vec::new();
            // Read the head and any declared body.
            loop {
                let n = socket.read(&mut buf).await.unwrap_or(0);
                if n == 0 {
                    break;
                }
                got.extend_from_slice(&buf[..n]);
                let text = String::from_utf8_lossy(&got).to_string();
                if let Some(end) = text.find("\r\n\r\n") {
                    let length = text
                        .lines()
                        .find_map(|l| {
                            let lower = l.to_ascii_lowercase();
                            lower
                                .strip_prefix("content-length:")
                                .map(|v| v.trim().parse::<usize>().unwrap_or(0))
                        })
                        .unwrap_or(0);
                    if got.len() >= end + 4 + length {
                        break;
                    }
                }
            }
            let head = String::from_utf8_lossy(&got).to_string();
            let first = head.lines().next().unwrap_or_default().to_owned();
            let signed = head
                .to_ascii_lowercase()
                .contains("authorization: aws4-hmac-sha256");
            seen_in
                .lock()
                .unwrap()
                .push(format!("{first} signed={signed}"));
            let reply = {
                let mut q = replies.lock().unwrap();
                if q.len() > 1 {
                    q.pop_front().unwrap()
                } else {
                    q.front().cloned().unwrap()
                }
            };
            match reply {
                Reply::Drop => drop(socket),
                Reply::Status(code, body) => {
                    let is_head = first.starts_with("HEAD ");
                    let response = format!(
                        "HTTP/1.1 {code} X\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                        body.len(),
                        if is_head { "" } else { body }
                    );
                    let _ = socket.write_all(response.as_bytes()).await;
                    let _ = socket.shutdown().await;
                }
            }
        }
    });
    Stub {
        url: format!("http://{addr}"),
        seen,
    }
}

fn client(url: &str, sleeper: Arc<RecordingSleeper>, draw: f64) -> SigV4S3Client {
    SigV4S3Client::configure(
        url,
        "auto",
        Secret::new("AKIDTEST"),
        Secret::new("test-only"),
    )
    .unwrap()
    .with_seams(ManualClock::at(0.0), sleeper, Arc::new(FixedRandom(draw)))
}

fn xml(code: &'static str) -> &'static str {
    match code {
        "SlowDown" => "<Error><Code>SlowDown</Code></Error>",
        "RequestTimeout" => "<?xml version=\"1.0\"?><Error><Code>RequestTimeout</Code></Error>",
        "Throttling" => "<Error><Code>Throttling</Code></Error>",
        "BadDigest" => "<Error><Code>BadDigest</Code></Error>",
        "InvalidArgument" => "<Error><Code>InvalidArgument</Code></Error>",
        "NoSuchKey" => "<Error><Code>NoSuchKey</Code></Error>",
        _ => "",
    }
}

#[test]
fn the_policy_is_botocores_legacy_s3_policy() {
    arm();
    assert_eq!(S3_MAX_ATTEMPTS, 5);
    for status in [429, 500, 502, 503, 504, 509] {
        assert!(s3_retryable(status, None), "{status}");
    }
    for code in [
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "RequestThrottledException",
        "ProvisionedThroughputExceededException",
        "RequestTimeout",
        "BadDigest",
    ] {
        assert!(s3_retryable(400, Some(code)), "{code}");
        // The code-named policies apply to a 400 only.
        assert!(!s3_retryable(403, Some(code)), "403 {code}");
    }
    for status in [301, 400, 401, 403, 404, 405, 409, 412, 501, 505] {
        assert!(!s3_retryable(status, None), "{status}");
    }
    assert!(!s3_retryable(400, Some("InvalidArgument")));
    // S3's own SlowDown arrives as a 503 and is retried by its status.
    assert!(s3_retryable(503, Some("SlowDown")));
    assert_eq!(
        s3_error_code(xml("NoSuchKey")).as_deref(),
        Some("NoSuchKey")
    );
    assert_eq!(s3_error_code(""), None);
    assert_eq!(s3_error_code("<Code></Code>"), None);
}

#[tokio::test]
async fn a_server_error_is_retried_with_exponential_rand_backoff_then_succeeds() {
    arm();
    let s = stub(vec![
        Reply::Status(500, ""),
        Reply::Status(503, xml("SlowDown")),
        Reply::Status(200, "payload"),
    ])
    .await;
    let sleeper = Arc::new(RecordingSleeper::default());
    let c = client(&s.url, Arc::clone(&sleeper), 0.25);
    let body = c
        .get_object("bucket", "nse/x/2026-10-29.csv")
        .await
        .unwrap();
    assert_eq!(body, b"payload");
    // delay_exponential(base=rand, growth_factor=2): r * 2 ** (attempts - 1), attempts from 1.
    assert_eq!(sleeper.sleeps(), vec![0.25, 0.5]);
    let seen = s.seen.lock().unwrap().clone();
    assert_eq!(seen.len(), 3);
    assert!(
        seen.iter()
            .all(|l| l.starts_with("GET /bucket/nse/x/2026-10-29.csv"))
    );
    assert!(seen.iter().all(|l| l.ends_with("signed=true")));
}

#[tokio::test]
async fn five_attempts_in_all_and_the_last_failure_is_the_one_returned() {
    arm();
    let s = stub(vec![Reply::Status(500, "")]).await;
    let sleeper = Arc::new(RecordingSleeper::default());
    let c = client(&s.url, Arc::clone(&sleeper), 0.5);
    let err = c
        .put_object(PutObject {
            bucket: "b",
            key: "k.csv",
            body: b"abc",
            content_type: "text/csv",
            metadata: BTreeMap::new(),
        })
        .await
        .unwrap_err();
    assert_eq!(err.to_string(), "S3 answered 500 Internal Server Error");
    assert_eq!(s.seen.lock().unwrap().len(), 5);
    assert_eq!(sleeper.sleeps(), vec![0.5, 1.0, 2.0, 4.0]);
}

#[tokio::test]
async fn throttling_and_s3s_own_400_codes_are_retried_a_plain_400_is_not() {
    arm();
    for code in ["Throttling", "RequestTimeout", "BadDigest"] {
        let s = stub(vec![
            Reply::Status(400, xml(code)),
            Reply::Status(200, "ok"),
        ])
        .await;
        let sleeper = Arc::new(RecordingSleeper::default());
        let c = client(&s.url, Arc::clone(&sleeper), 1.0);
        assert_eq!(c.get_object("b", "k").await.unwrap(), b"ok", "{code}");
        assert_eq!(sleeper.sleeps(), vec![1.0], "{code}");
    }
    let s = stub(vec![
        Reply::Status(400, xml("InvalidArgument")),
        Reply::Status(200, "ok"),
    ])
    .await;
    let sleeper = Arc::new(RecordingSleeper::default());
    let c = client(&s.url, Arc::clone(&sleeper), 1.0);
    let err = c.get_object("b", "k").await.unwrap_err();
    assert_eq!(
        err.to_string(),
        "S3 answered 400 Bad Request (InvalidArgument)"
    );
    assert!(sleeper.sleeps().is_empty());
    assert_eq!(s.seen.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn too_many_requests_is_retried_not_found_and_forbidden_are_answers() {
    arm();
    let s = stub(vec![Reply::Status(429, ""), Reply::Status(200, "ok")]).await;
    let sleeper = Arc::new(RecordingSleeper::default());
    let c = client(&s.url, Arc::clone(&sleeper), 0.1);
    assert_eq!(c.get_object("b", "k").await.unwrap(), b"ok");
    assert_eq!(sleeper.sleeps(), vec![0.1]);

    for status in [403u16, 404] {
        let s = stub(vec![Reply::Status(status, xml("NoSuchKey"))]).await;
        let sleeper = Arc::new(RecordingSleeper::default());
        let c = client(&s.url, Arc::clone(&sleeper), 0.1);
        // HEAD: no body, so no code; a 404 is a missing key, answered at once.
        assert!(c.head_object("b", "k").await.is_err());
        assert!(sleeper.sleeps().is_empty(), "{status}");
        assert_eq!(s.seen.lock().unwrap().len(), 1, "{status}");
    }
}

#[tokio::test]
async fn a_dropped_connection_is_retried_as_a_socket_error() {
    arm();
    let s = stub(vec![Reply::Drop, Reply::Drop, Reply::Status(200, "ok")]).await;
    let sleeper = Arc::new(RecordingSleeper::default());
    let c = client(&s.url, Arc::clone(&sleeper), 0.5);
    assert_eq!(c.get_object("b", "k").await.unwrap(), b"ok");
    assert_eq!(sleeper.sleeps(), vec![0.5, 1.0]);
    assert_eq!(s.seen.lock().unwrap().len(), 3);
}

#[tokio::test]
async fn a_refused_connection_is_retried_five_times_then_raised() {
    arm();
    // Bind and drop: nothing listens on the port any more.
    let port = {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        l.local_addr().unwrap().port()
    };
    let sleeper = Arc::new(RecordingSleeper::default());
    let c = client(
        &format!("http://127.0.0.1:{port}"),
        Arc::clone(&sleeper),
        0.5,
    );
    assert!(c.get_object("b", "k").await.is_err());
    assert_eq!(sleeper.sleeps(), vec![0.5, 1.0, 2.0, 4.0]);
}

#[tokio::test]
async fn a_blocked_host_is_refused_before_any_attempt_and_never_retried() {
    arm();
    let sleeper = Arc::new(RecordingSleeper::default());
    let c = client(
        "https://example.r2.cloudflarestorage.com",
        Arc::clone(&sleeper),
        0.5,
    );
    assert!(c.get_object("b", "k").await.is_err());
    assert!(sleeper.sleeps().is_empty());
}
