//! The production NSE client on the wire, against a loopback server (the network block allows
//! loopback only). The expected bytes are what Python's `build_http_client` (httpx 0.28, no
//! brotli/zstandard installed) sent to the same kind of server, captured on 29 Oct 2026:
//!
//! ```text
//! GET / HTTP/1.1\r\nHost: 127.0.0.1:PORT\r\nAccept-Encoding: gzip, deflate\r\n
//! User-Agent: Mozilla/5.0 (...) Chrome/124.0 Safari/537.36\r\n
//! Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n
//! Accept-Language: en-GB,en;q=0.9\r\nConnection: keep-alive\r\n\r\n
//! ```
//!
//! and, after the root set two cookies, the next request carried `Cookie: nsit=abc; bm_sv=xyz`
//! last. NSE's cookie/header discipline is exactly this, so it is asserted byte for byte.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::sync::{Arc, Mutex};

use baskfy_providers::archive::LocalRawArchive;
use baskfy_providers::nse::{NseProvider, NseRuntime, build_http_client};
use baskfy_providers::retry::RetryPolicy;
use baskfy_providers::settings::ProviderSettings;
use common::arm;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const UA: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like \
                  Gecko) Chrome/124.0 Safari/537.36";

fn gzip_body() -> Vec<u8> {
    let hex = "1f8b08000000000000ff0b8ef475f2f7e10a76f2f4e30200fefea4c50c000000";
    (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect()
}

/// A tiny HTTP/1.1 server: records every raw request head, answers by path, keeps connections
/// alive. `/` sets two cookies; `/gz` answers gzip-encoded; `/missing` is a 404.
async fn serve() -> (u16, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut socket, _)) = listener.accept().await else {
                return;
            };
            let log = log.clone();
            tokio::spawn(async move {
                let mut buffer = Vec::new();
                loop {
                    let mut chunk = [0_u8; 4096];
                    let Ok(n) = socket.read(&mut chunk).await else {
                        return;
                    };
                    if n == 0 {
                        return;
                    }
                    buffer.extend_from_slice(&chunk[..n]);
                    while let Some(end) = buffer.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head: Vec<u8> = buffer.drain(..end + 4).collect();
                        let head = String::from_utf8(head).unwrap();
                        let path = head.split(' ').nth(1).unwrap_or("").to_owned();
                        log.lock().unwrap().push(head);
                        let (status, extra, body): (&str, String, Vec<u8>) = match path.as_str() {
                            "/" => (
                                "200 OK",
                                "Set-Cookie: nsit=abc; Path=/\r\nSet-Cookie: bm_sv=xyz; Path=/; HttpOnly\r\n"
                                    .to_owned(),
                                b"ok".to_vec(),
                            ),
                            "/deep/set" => (
                                "200 OK",
                                "Set-Cookie: deep=1; Path=/deep\r\nSet-Cookie: nsit=new; Path=/\r\n"
                                    .to_owned(),
                                b"ok".to_vec(),
                            ),
                            "/gz" => (
                                "200 OK",
                                "Content-Encoding: gzip\r\n".to_owned(),
                                gzip_body(),
                            ),
                            "/missing" => ("404 Not Found", String::new(), b"no".to_vec()),
                            _ => (
                                "200 OK",
                                String::new(),
                                b"SYMBOL,NAME OF COMPANY\nSBIN,STATE BANK OF INDIA\n".to_vec(),
                            ),
                        };
                        let response = format!(
                            "HTTP/1.1 {status}\r\n{extra}Content-Length: {}\r\n\r\n",
                            body.len()
                        );
                        if socket.write_all(response.as_bytes()).await.is_err()
                            || socket.write_all(&body).await.is_err()
                        {
                            return;
                        }
                    }
                }
            });
        }
    });
    (port, seen)
}

fn expected(path: &str, port: u16, cookie: bool) -> String {
    let mut s = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept-Encoding: gzip, deflate\r\n\
         User-Agent: {UA}\r\n\
         Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n\
         Accept-Language: en-GB,en;q=0.9\r\nConnection: keep-alive\r\n"
    );
    if cookie {
        s.push_str("Cookie: nsit=abc; bm_sv=xyz\r\n");
    }
    s.push_str("\r\n");
    s
}

fn local(port: u16) -> ProviderSettings {
    ProviderSettings {
        nse_base_url: format!("http://127.0.0.1:{port}"),
        nse_archive_url: format!("http://127.0.0.1:{port}"),
        ..ProviderSettings::default()
    }
}

/// The request httpx makes, byte for byte: header order, title case, values, and the jar's
/// cookies on the next request; a space in the query is percent-encoded as httpx does.
#[tokio::test]
async fn the_client_sends_httpxs_request_byte_for_byte() {
    arm();
    let (port, seen) = serve().await;
    let client = build_http_client(&local(port)).unwrap();
    let root = client
        .get(&format!("http://127.0.0.1:{port}"))
        .await
        .unwrap();
    assert_eq!(
        (root.status_code, root.content.as_slice()),
        (200, &b"ok"[..])
    );
    let file = client
        .get(&format!(
            "http://127.0.0.1:{port}/content/equities/EQUITY_L.csv?x=a b&y=%41"
        ))
        .await
        .unwrap();
    assert_eq!(file.status_code, 200);
    let heads = seen.lock().unwrap().clone();
    assert_eq!(heads[0], expected("/", port, false));
    assert_eq!(
        heads[1],
        expected("/content/equities/EQUITY_L.csv?x=a%20b&y=%41", port, true)
    );
}

/// `http.cookiejar`'s order, as httpx sent it on 29 Oct 2026: the longest cookie path first, then
/// first-set order — a reset cookie (`nsit=new`) keeps its place.
#[tokio::test]
async fn cookies_go_out_in_cookiejars_order() {
    arm();
    let (port, seen) = serve().await;
    let client = build_http_client(&local(port)).unwrap();
    for path in ["/", "/deep/set", "/deep/file", "/other"] {
        client
            .get(&format!("http://127.0.0.1:{port}{path}"))
            .await
            .unwrap();
    }
    let cookies: Vec<Option<String>> = seen
        .lock()
        .unwrap()
        .iter()
        .map(|head| {
            head.split("\r\n")
                .find(|l| l.starts_with("Cookie: "))
                .map(str::to_owned)
        })
        .collect();
    assert_eq!(
        cookies,
        [
            None,
            Some("Cookie: nsit=abc; bm_sv=xyz".to_owned()),
            Some("Cookie: deep=1; nsit=new; bm_sv=xyz".to_owned()),
            Some("Cookie: nsit=new; bm_sv=xyz".to_owned()),
        ]
    );
}

/// httpx's `.content` is the decoded body; so is this client's.
#[tokio::test]
async fn a_gzip_body_is_decoded_and_a_4xx_is_returned_as_a_status() {
    arm();
    let (port, _) = serve().await;
    let client = build_http_client(&local(port)).unwrap();
    let gz = client
        .get(&format!("http://127.0.0.1:{port}/gz"))
        .await
        .unwrap();
    assert_eq!(gz.content, b"SYMBOL\nSBIN\n");
    let missing = client
        .get(&format!("http://127.0.0.1:{port}/missing"))
        .await
        .unwrap();
    assert_eq!(missing.status_code, 404);
}

/// The provider end to end on the real client: the root is primed once, the file follows with
/// the session's cookies, and the parse reads the archived bytes.
#[tokio::test]
async fn the_provider_primes_the_session_then_fetches_with_its_cookies() {
    arm();
    let (port, seen) = serve().await;
    let settings = local(port);
    let dir = tempfile::tempdir().unwrap();
    let provider = NseProvider::new(
        settings.clone(),
        Some(Arc::new(LocalRawArchive::new(dir.path()))),
        NseRuntime {
            client: Some(build_http_client(&settings).unwrap()),
            rate_limiter: Some(Arc::new(Unlimited)),
            retry_policy: Some(RetryPolicy::new(1, 0.01, 30.0).unwrap()),
            retry_hooks: None,
            clock: None,
        },
    )
    .unwrap();
    let listings = provider.listings().await.unwrap();
    assert_eq!(listings.len(), 1);
    assert_eq!(listings[0].name, "STATE BANK OF INDIA");
    let heads = seen.lock().unwrap().clone();
    assert_eq!(heads.len(), 2);
    assert_eq!(heads[0], expected("/", port, false));
    assert_eq!(
        heads[1],
        expected("/content/equities/EQUITY_L.csv", port, true)
    );
}

/// A non-loopback NSE host is refused by the network block before any socket opens, and the
/// refusal reaches the caller as the transport failure Python's `_get` wraps.
#[tokio::test]
async fn the_real_nse_host_is_unreachable_from_the_suite() {
    arm();
    let client = build_http_client(&ProviderSettings::default()).unwrap();
    let err = client.get("https://www.nseindia.com").await.unwrap_err();
    assert!(err.to_string().contains("tried to reach"), "{err}");
}

struct Unlimited;

#[async_trait::async_trait]
impl baskfy_providers::ratelimit::RateLimiter for Unlimited {
    async fn acquire(&self, _tokens: f64) -> Result<f64, baskfy_providers::ProviderError> {
        Ok(0.0)
    }
}
