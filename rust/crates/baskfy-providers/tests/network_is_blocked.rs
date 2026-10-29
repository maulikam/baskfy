//! Prompt 2 acceptance criterion 1: zero network calls in the test suite — ports
//! `tests/test_network_is_blocked.py` and the guarantee of `network_guard.py`.
//!
//! These prove the block is armed and that nothing can route around it: every HTTP client is
//! built in `net.rs`, every test arms the block, and a refused call names the address.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::net::{IpAddr, Ipv4Addr};
use std::path::{Path, PathBuf};

use baskfy_providers::net::{
    Address, GuardedClient, GuardedResolver, client_builder, guard_url, is_loopback,
    network_blocked,
};
use common::arm;

// --- TestTheBlockIsArmed -------------------------------------------------------------------

#[tokio::test]
async fn connecting_to_a_remote_host_is_refused() {
    arm();
    assert!(network_blocked());
    let client = GuardedClient::from_builder(client_builder()).unwrap();
    let err = client.get("https://www.nseindia.com/").unwrap_err();
    assert_eq!(err.python_type(), "NetworkAccessBlocked");
}

#[tokio::test]
async fn an_ip_literal_is_refused_before_any_socket() {
    arm();
    let client = GuardedClient::from_builder(client_builder()).unwrap();
    let err = client.get("https://13.107.42.14/").unwrap_err();
    assert!(err.to_string().contains("13.107.42.14"), "{err}");
}

#[tokio::test]
async fn a_request_that_bypasses_the_url_check_still_cannot_resolve() {
    // A raw builder-made request on the guarded client's inner resolver: send() fails at DNS.
    arm();
    let client = client_builder().build().unwrap();
    let err = client
        .get("https://api.kite.trade/")
        .send()
        .await
        .unwrap_err();
    let chain = format!("{err:?}");
    assert!(chain.contains("kite.trade"), "{chain}");
}

#[tokio::test]
async fn dns_resolution_is_refused() {
    arm();
    let err = GuardedResolver
        .lookup("nsearchives.nseindia.com")
        .await
        .unwrap_err();
    assert_eq!(err.python_type(), "NetworkAccessBlocked");
}

#[tokio::test]
async fn the_refusal_names_the_address() {
    arm();
    let err = GuardedResolver
        .lookup("www.nseindia.com")
        .await
        .unwrap_err();
    assert!(err.to_string().contains("nseindia"), "{err}");
    assert!(
        err.to_string()
            .starts_with("getaddrinfo tried to reach ('www.nseindia.com', 0).")
    );
}

#[test]
fn a_remote_redis_url_is_refused() {
    arm();
    assert!(guard_url("redis", "redis://cache.example.com:6379/0").is_err());
    assert!(guard_url("redis", "redis://localhost:6380/0").is_ok());
    assert!(guard_url("redis", "redis+unix:///run/redis.sock").is_ok());
}

// --- TestLoopbackRemainsUsable -------------------------------------------------------------

#[test]
fn loopback_ipv4_is_allowed() {
    arm();
    assert!(is_loopback(Address::Host("127.0.0.1", 5433)));
}

#[test]
fn localhost_is_allowed() {
    arm();
    assert!(is_loopback(Address::Host("localhost", 6380)));
}

#[test]
fn unix_socket_paths_are_allowed() {
    arm();
    assert!(is_loopback(Address::Unix("/tmp/postgres.sock")));
}

#[test]
fn a_public_address_is_not_loopback() {
    arm();
    assert!(!is_loopback(Address::Host("13.107.42.14", 443)));
}

#[tokio::test]
async fn permitted_hosts_resolve_to_themselves_without_the_system_resolver() {
    arm();
    let addrs = GuardedResolver.lookup("127.0.0.1").await.unwrap();
    assert_eq!(
        addrs
            .iter()
            .map(std::net::SocketAddr::ip)
            .collect::<Vec<_>>(),
        vec![IpAddr::V4(Ipv4Addr::LOCALHOST)]
    );
    assert_eq!(GuardedResolver.lookup("localhost").await.unwrap().len(), 2);
}

// --- Nothing routes around the block -------------------------------------------------------

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in std::fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().is_some_and(|x| x == "rs") {
            out.push(p);
        }
    }
}

fn code_only(text: &str) -> String {
    text.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn every_client_and_socket_is_built_in_the_guarded_modules() {
    arm();
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = Vec::new();
    rust_files(&root, &mut files);
    // (token, the only file allowed to contain it)
    let rules: [(&str, &str); 9] = [
        ("Client::builder", "net.rs"),
        ("ClientBuilder::new", "net.rs"),
        ("Client::new(", "net.rs"),
        ("reqwest::get(", "net.rs"),
        ("redis::Client::open", "ratelimit.rs"),
        ("TcpStream", "net.rs"),
        ("UdpSocket", "net.rs"),
        ("std::net::TcpListener", "net.rs"),
        ("lookup_host", "net.rs"),
    ];
    let mut hits = Vec::new();
    for f in &files {
        let name = f.file_name().unwrap().to_string_lossy().to_string();
        let code = code_only(&std::fs::read_to_string(f).unwrap());
        for (token, home) in rules {
            if code.contains(token) && name != home {
                hits.push(format!("{}: {token} (only {home} may)", f.display()));
            }
        }
    }
    assert!(
        hits.is_empty(),
        "network escapes the guard:\n{}",
        hits.join("\n")
    );
    // The one Redis connection site guards its URL first.
    let ratelimit = std::fs::read_to_string(root.join("ratelimit.rs")).unwrap();
    let guard = ratelimit.find("guard_url(\"redis\"").unwrap();
    let open = ratelimit.find("redis::Client::open").unwrap();
    assert!(guard < open);
}

#[test]
fn every_test_arms_the_block_first() {
    arm();
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests");
    let mut missing = Vec::new();
    for e in std::fs::read_dir(&dir).unwrap() {
        let p = e.unwrap().path();
        if p.extension().is_none_or(|x| x != "rs") {
            continue;
        }
        let code = code_only(&std::fs::read_to_string(&p).unwrap());
        // Built with concat! so this file's own literals do not count themselves.
        let tests = code.matches(concat!("#[", "test]")).count()
            + code.matches(concat!("#[", "tokio::test")).count();
        let arms = code.matches(concat!("    ", "arm();")).count();
        if arms < tests {
            missing.push(format!(
                "{}: {tests} tests, {arms} arm() calls",
                p.display()
            ));
        }
    }
    assert!(missing.is_empty(), "{}", missing.join("\n"));
}
