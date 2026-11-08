//! `NseProvider::fo_ban_list` — the F&O ban list for the next session (docs/fno/02 Track C §9,
//! FO2). `test_nse_fo_ban_list.py`, case for case, over the recorded file read in place.
//!
//! The file names the session it is for and is refused for any other (the SW16 rule), it is
//! archived before it is parsed and keyed by the session it *says* it is for, an archived answer
//! is not re-fetched, NIL is an empty list, and a file that cannot be read in full is refused.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;
mod nse_common;

use std::sync::Arc;

use baskfy_providers::archive::{RawArchive, archive_key};
use baskfy_providers::errors::ErrorKind;
use baskfy_providers::nse::{FO_BAN_LIST_PATH, KIND_FO_BAN_LIST, parse_fo_ban_list};
use chrono::NaiveDate;
use common::arm;
use nse_common::{FakeHttpClient, InertLimiter, archive, build, recorded, settings};

fn session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 25).unwrap()
}

fn fixture() -> Vec<u8> {
    recorded("fo_secban-2026-09-25.csv")
}

fn ban_file(stamp: &str, rows: &[&str]) -> Vec<u8> {
    let mut body = format!("Securities in Ban For Trade Date {stamp}:\n");
    for r in rows {
        body.push_str(r);
        body.push('\n');
    }
    body.into_bytes()
}

fn client(body: Vec<u8>) -> Arc<FakeHttpClient> {
    Arc::new(FakeHttpClient::new(&[("fo_secban", body)]))
}

// --- TestTheRecordedFile ---------------------------------------------------------------------

#[tokio::test]
async fn it_reads_the_four_securities_fo0_saw() {
    arm();
    let (_dir, archive) = archive();
    let banned = build(settings(), archive, client(fixture()), None)
        .fo_ban_list(session())
        .await
        .unwrap();
    assert_eq!(banned, ["KAYNES", "LICHSGFIN", "MANAPPURAM", "SAIL"]);
}

#[tokio::test]
async fn the_url_is_the_ban_file_behind_the_limiter_and_cookie_priming() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(fixture());
    let limiter = Arc::new(InertLimiter::default());
    build(settings(), archive, fake.clone(), Some(limiter.clone()))
        .fo_ban_list(session())
        .await
        .unwrap();
    let requests = fake.requests();
    let s = settings();
    assert_eq!(
        requests.last().unwrap(),
        &format!("{}{FO_BAN_LIST_PATH}", s.nse_archive_url)
    );
    assert!(
        requests
            .last()
            .unwrap()
            .ends_with("/content/fo/fo_secban.csv")
    );
    // One cookie-priming visit to the site root, then the file; each one throttled.
    assert_eq!(requests[0], s.nse_base_url);
    assert_eq!(limiter.count(), requests.len());
    assert_eq!(requests.len(), 2);
}

#[tokio::test]
async fn it_is_archived_under_its_session_before_parsing_and_never_refetched() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(fixture());
    let provider = build(settings(), archive.clone(), fake.clone(), None);
    let first = provider.fo_ban_list(session()).await.unwrap();
    let key = archive_key(KIND_FO_BAN_LIST, session(), "csv").unwrap();
    assert!(archive.exists(&key).await.unwrap());
    assert_eq!(archive.get(&key).await.unwrap(), fixture());
    let asked = fake.requests().len();
    assert_eq!(provider.fo_ban_list(session()).await.unwrap(), first);
    assert_eq!(
        fake.requests().len(),
        asked,
        "an archived session must not ask NSE again"
    );
}

// --- TestTheWrongDateRefusal -----------------------------------------------------------------

#[tokio::test]
async fn todays_list_served_for_tomorrow_is_refused() {
    arm();
    let (_dir, archive) = archive();
    let err = build(
        settings(),
        archive.clone(),
        client(ban_file("24-SEP-2026", &["1,IDEA"])),
        None,
    )
    .fo_ban_list(session())
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    assert!(err.to_string().contains("wrong file"));
    assert_eq!(
        err.message(),
        "F&O ban list for 2026-09-25 is stamped 2026-09-24; wrong file for this session"
    );
    // Archived under the session it names (true), never under the one asked for.
    let named = archive_key(
        KIND_FO_BAN_LIST,
        NaiveDate::from_ymd_opt(2026, 9, 24).unwrap(),
        "csv",
    )
    .unwrap();
    assert!(archive.exists(&named).await.unwrap());
    let asked = archive_key(KIND_FO_BAN_LIST, session(), "csv").unwrap();
    assert!(!archive.exists(&asked).await.unwrap());
}

#[tokio::test]
async fn a_refused_session_can_be_asked_again_once_nse_publishes() {
    arm();
    let (_dir, archive) = archive();
    let early = client(ban_file("24-SEP-2026", &["1,IDEA"]));
    let err = build(settings(), archive.clone(), early, None)
        .fo_ban_list(session())
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    let later = client(fixture());
    assert_eq!(
        build(settings(), archive, later, None)
            .fo_ban_list(session())
            .await
            .unwrap(),
        ["KAYNES", "LICHSGFIN", "MANAPPURAM", "SAIL"]
    );
}

// --- TestTheParser ---------------------------------------------------------------------------

#[test]
fn nil_is_an_empty_list() {
    arm();
    let parsed = parse_fo_ban_list(b"Securities in Ban For Trade Date 25-SEP-2026: NIL\n").unwrap();
    assert_eq!(parsed, (session(), Vec::<String>::new()));
}

#[test]
fn no_rows_is_an_empty_list() {
    arm();
    assert_eq!(
        parse_fo_ban_list(&ban_file("25-SEP-2026", &[])).unwrap(),
        (session(), Vec::<String>::new())
    );
}

#[test]
fn rows_are_sorted_upper_cased_and_deduplicated() {
    arm();
    let payload = ban_file("25-Sep-2026", &["1,sail", "2,M&M", "3,SAIL,"]);
    assert_eq!(
        parse_fo_ban_list(&payload).unwrap(),
        (session(), vec!["M&M".to_owned(), "SAIL".to_owned()])
    );
}

#[test]
fn a_file_without_the_header_is_refused() {
    arm();
    let err = parse_fo_ban_list(b"1,KAYNES\n2,SAIL\n").unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    assert!(err.to_string().contains("header"));
}

/// A missed symbol is a trade in a banned stock, so a partial read is never returned.
#[test]
fn an_unreadable_line_refuses_the_whole_list() {
    arm();
    let err = parse_fo_ban_list(&ban_file(
        "25-SEP-2026",
        &["1,KAYNES", "<html>blocked</html>"],
    ))
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    assert!(err.to_string().contains("unreadable line"));
    assert_eq!(
        err.message(),
        "F&O ban list for 2026-09-25 has an unreadable line '<html>blocked</html>'"
    );
}

#[test]
fn an_empty_body_is_refused() {
    arm();
    let err = parse_fo_ban_list(b"").unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
}

/// `utf-8-sig`, `\r\n`, spaces around the comma and a `NIL` row between commas all read as
/// Python reads them; an impossible date in the header is no header.
#[test]
fn the_parser_reads_what_python_reads() {
    arm();
    let payload =
        b"\xef\xbb\xbfSecurities in Ban For Trade Date 5-Sep-2026\r\n 1 , abc ,\r\n,NIL,\r\n";
    assert_eq!(
        parse_fo_ban_list(payload).unwrap(),
        (
            NaiveDate::from_ymd_opt(2026, 9, 5).unwrap(),
            vec!["ABC".to_owned()]
        )
    );
    let err = parse_fo_ban_list(b"Securities in Ban For Trade Date 31-SEP-2026:\n").unwrap_err();
    assert!(err.to_string().contains("header"));
    let err = parse_fo_ban_list(b"\xff\xfe").unwrap_err();
    assert!(err.message().starts_with("F&O ban list is not UTF-8 text:"));
}
