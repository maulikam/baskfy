//! Ports `tests/test_outbound_ip.py` (the IPv4 pin; the "pins before it builds a session"
//! ordering belongs to the Kite-client lane) and the pure half of `tests/test_websec.py`
//! (the allow-lists, the Basic check, the closed defaults). The middleware's HTTP cases need
//! the assembled app and are graded through `desk_diff.py` (`desk_cases/exec_guards.json`).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use base64::Engine as _;

use super::super::net::{force_ipv4, ipv4_only, local_bind, outbound_ip};
use crate::settings::Settings;
use crate::websec::{NO_AUTH, basic_ok, hosts, origins};

#[test]
fn test_pinning_selects_the_ipv4_family_and_twice_changes_nothing() {
    let _first = force_ipv4();
    assert!(ipv4_only());
    assert_eq!(
        local_bind(),
        Some(std::net::IpAddr::V4(std::net::Ipv4Addr::UNSPECIFIED))
    );
    assert!(
        !force_ipv4(),
        "a second pin is a no-op, not a stacked patch"
    );
}

#[test]
fn test_it_defaults_to_on() {
    assert!(Settings::defaults().unwrap().force_ipv4);
}

#[tokio::test]
async fn a_failed_lookup_is_none_not_an_error() {
    assert_eq!(
        outbound_ip(|| async { Err("timed out".to_owned()) }).await,
        None
    );
    assert_eq!(
        outbound_ip(|| async { Ok(" 203.0.113.7\n".to_owned()) })
            .await
            .as_deref(),
        Some("203.0.113.7")
    );
}

#[test]
fn the_names_it_answers_to_default_to_loopback_and_are_configurable() {
    let h = hosts("");
    assert!(h.contains("127.0.0.1") && h.contains("localhost") && h.len() == 2);
    let h = hosts(" Desk.Internal ,, ");
    assert_eq!(h.into_iter().collect::<Vec<_>>(), ["desk.internal"]);
    let o = origins("", 8420);
    for want in [
        "http://localhost",
        "http://localhost:8420",
        "https://127.0.0.1",
        "https://127.0.0.1:8420",
    ] {
        assert!(o.contains(want), "{want}");
    }
    assert!(!o.contains("https://evil.example"));
}

#[test]
fn test_no_password_by_default_and_the_docs_closed() {
    let s = Settings::defaults().unwrap();
    assert_eq!(s.desk_password, "");
    assert!(!s.desk_docs);
    assert_eq!(NO_AUTH, ["/status"]);
}

fn basic(user_pass: &str) -> String {
    format!(
        "Basic {}",
        base64::engine::general_purpose::STANDARD.encode(user_pass)
    )
}

#[test]
fn test_the_right_password_gets_in_and_a_wrong_one_does_not() {
    assert!(basic_ok(Some(&basic("desk:hunter2")), "hunter2"));
    assert!(basic_ok(Some(&basic("anyone:hunter2")), "hunter2"));
    assert!(!basic_ok(Some(&basic("desk:hunter3")), "hunter2"));
    assert!(!basic_ok(Some(&basic("desk:hunter22")), "hunter2"));
}

#[test]
fn test_malformed_credentials_are_rejected_rather_than_crashing() {
    for bad in ["", "Basic", "Basic !!!not-base64!!!", "Bearer hunter2"] {
        assert!(!basic_ok(Some(bad), "hunter2"), "{bad:?}");
    }
    assert!(!basic_ok(None, "hunter2"));
}

// --- review R8 (exec_guards): headers are read as Starlette reads them -----------------------

#[test]
fn a_non_ascii_header_is_read_as_latin1_not_as_absent() {
    let v = axum::http::HeaderValue::from_bytes(b"\xe9vil.example").unwrap();
    assert_eq!(crate::websec::latin1(&v), "\u{e9}vil.example");
}

#[test]
fn the_basic_header_decodes_like_b64decode() {
    // `base64.b64decode` drops characters outside the alphabet (a wrapped header still works).
    let enc = base64::engine::general_purpose::STANDARD.encode("desk:hunter2");
    let (a, b) = enc.split_at(4);
    assert!(basic_ok(Some(&format!("Basic {a}\n{b}")), "hunter2"));
    assert!(basic_ok(Some(&format!("Basic {a} {b}")), "hunter2"));
    // CPython: a stray or excess `=` is skipped; missing padding is "Incorrect padding".
    for (enc, ok) in [
        ("ZGVzazpodW50ZXIy=", true),
        ("ZGVzazpodW50ZXIy==", true),
        ("ZGVz=azpodW50ZXIy", true),
        ("ZGVzazpodW50ZX", false),
        ("ZGVzazpodW50ZXIyZ", false),
    ] {
        assert_eq!(
            basic_ok(Some(&format!("Basic {enc}")), "hunter2"),
            ok,
            "{enc}"
        );
    }
}
