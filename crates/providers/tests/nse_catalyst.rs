//! SW11B: the catalyst feed's two NSE reads (STANDING-ANSWERS A3) — `test_nse_catalyst.py`,
//! case for case, over the recorded payloads in `fixtures/nse/` (read in place).
//!
//! A record is a **headline, a stamp and a link** — never the filing's text; a row with no
//! attachment is dropped, a row with no stamp is kept and sorts last; a URL keeps its query
//! string; the calendar yields result meetings only, soonest first; a symbol NSE does not know is
//! absence, and every other failure is the provider's own error.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;
mod nse_common;

use std::sync::Arc;

use baskfy_providers::archive::{RawArchive, archive_key};
use baskfy_providers::errors::ErrorKind;
use baskfy_providers::nse::{
    HEADLINE_MAX_CHARS, KIND_ANNOUNCEMENTS, KIND_EVENT_CALENDAR, ist, parse_announcements,
    parse_results_calendar,
};
use baskfy_providers::records::PyDateTime;
use chrono::{NaiveDate, TimeZone};
use common::arm;
use nse_common::{FakeHttpClient, InertLimiter, archive, build, recorded, settings};

const ARCHIVE_URL: &str = "https://nsearchives.nseindia.com";
const PAGE: &str =
    "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar?symbol=";

fn on() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 2).unwrap()
}

fn routes() -> Vec<(&'static str, Vec<u8>)> {
    vec![
        (
            "corporate-announcements",
            recorded("announcements-INFY.json"),
        ),
        ("event-calendar", recorded("event-calendar-INFY.json")),
    ]
}

fn client() -> Arc<FakeHttpClient> {
    Arc::new(FakeHttpClient::new(&routes()).fallback(b"[]"))
}

fn ist_at(y: i32, mo: u32, d: u32, h: u32, mi: u32, s: u32) -> PyDateTime {
    PyDateTime::Aware(ist().with_ymd_and_hms(y, mo, d, h, mi, s).unwrap())
}

fn infy() -> Vec<baskfy_providers::records::CatalystRecord> {
    parse_announcements(&recorded("announcements-INFY.json"), "INFY", ARCHIVE_URL).unwrap()
}

// --- TestAnnouncements -----------------------------------------------------------------------

#[test]
fn an_announcement_is_a_headline_a_stamp_and_a_link() {
    arm();
    let records = infy();
    let newest = &records[0];
    assert_eq!(newest.symbol, "INFY");
    assert_eq!(newest.published_at, Some(ist_at(2026, 9, 2, 8, 41, 5)));
    assert!(
        newest
            .url
            .starts_with("https://nsearchives.nseindia.com/corporate/INFY_02092026")
    );
    assert!(
        newest
            .headline
            .starts_with("Press Release - Infosys and a European bank")
    );
    // `model_dump()` has exactly these five keys.
    let dumped = serde_json::to_value(newest).unwrap();
    let mut keys: Vec<&String> = dumped.as_object().unwrap().keys().collect();
    keys.sort();
    assert_eq!(
        keys,
        ["headline", "published_at", "source", "symbol", "url"]
    );
}

/// A3: link out, do not reproduce text. The cap is what makes a headline a headline.
#[test]
fn the_headline_is_capped_so_the_filing_text_is_never_carried() {
    arm();
    let records = infy();
    assert!(
        records
            .iter()
            .all(|r| r.headline.chars().count() <= HEADLINE_MAX_CHARS)
    );
    assert!(records[0].headline.ends_with('…'));
}

#[test]
fn an_announcement_without_an_attachment_is_dropped_not_invented() {
    arm();
    let records = infy();
    let all: Vec<&str> = records.iter().map(|r| r.headline.as_str()).collect();
    assert!(!all.join(" ").contains("Takeover"));
    assert_eq!(records.len(), 3);
}

#[test]
fn an_announcement_without_a_timestamp_is_kept_and_sorts_last() {
    arm();
    let records = infy();
    let undated: Vec<bool> = records.iter().map(|r| r.published_at.is_none()).collect();
    assert_eq!(undated, [false, false, true]);
    assert_eq!(
        records[2].headline,
        "Analysts/Institutional Investor Meet/Con. Call Updates"
    );
}

/// The URL is `sw_catalyst`'s uniqueness key; normalising it is how two filings collide.
#[test]
fn a_url_keeps_its_query_string_verbatim() {
    arm();
    assert!(
        infy()[0]
            .url
            .ends_with("INFY_02092026084105_PR.pdf?x=1&y=2")
    );
}

#[test]
fn a_relative_attachment_is_joined_to_the_archive_host() {
    arm();
    let payload = br#"[{"symbol":"X","desc":"Updates","attchmntFile":"/corporate/X.pdf","an_dt":"01-Sep-2026 10:00:00"}]"#;
    let records = parse_announcements(payload, "X", ARCHIVE_URL).unwrap();
    assert_eq!(
        records[0].url,
        "https://nsearchives.nseindia.com/corporate/X.pdf"
    );
}

#[test]
fn an_empty_an_dt_beside_a_filled_sort_date_keeps_the_stamp() {
    arm();
    let payload = br#"[{"symbol":"X","desc":"Updates","attchmntFile":"https://a/x.pdf","an_dt":"","sort_date":"2026-09-01 10:00:00"}]"#;
    let records = parse_announcements(payload, "X", "https://a").unwrap();
    assert_eq!(records[0].published_at, Some(ist_at(2026, 9, 1, 10, 0, 0)));
    assert_eq!(
        records[0].published_at.unwrap().isoformat(),
        "2026-09-01T10:00:00+05:30"
    );
}

#[test]
fn an_empty_list_is_no_announcements_not_an_error() {
    arm();
    assert!(
        parse_announcements(b"[]", "X", "https://a")
            .unwrap()
            .is_empty()
    );
}

#[test]
fn a_body_that_is_not_a_list_is_an_unexpected_payload() {
    arm();
    let err = parse_announcements(br#"{"error":"x"}"#, "X", "https://a").unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    assert_eq!(
        err.message(),
        "X announcements: expected a JSON list, got dict"
    );
    let err = parse_announcements(b"<html>", "X", "https://a").unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
}

/// `{"data": [...]}` is unwrapped, as Python's `_filings_rows` does.
#[test]
fn a_data_envelope_is_unwrapped() {
    arm();
    let payload = br#"{"data":[{"desc":"d","attchmntFile":"f"}]}"#;
    let records = parse_announcements(payload, "INFY", "https://arch").unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].url, "https://arch/f");
    assert_eq!(records[0].symbol, "INFY");
    assert_eq!(records[0].headline, "d");
}

// --- TestAnnouncementsThroughTheProvider -----------------------------------------------------

#[tokio::test]
async fn every_symbol_is_one_archived_request_under_the_limiter() {
    arm();
    let (_dir, archive) = archive();
    let limiter = Arc::new(InertLimiter::default());
    let fake = client();
    let records = build(
        settings(),
        archive.clone(),
        fake.clone(),
        Some(limiter.clone()),
    )
    .announcements(&["INFY".to_owned(), "tcs ".to_owned()], on())
    .await
    .unwrap();
    let base = settings().nse_base_url;
    let requests = fake.requests();
    // The site root once, then one request per symbol; a token for each.
    assert_eq!(requests[0], base);
    let asked: Vec<&String> = requests
        .iter()
        .filter(|u| u.contains("corporate-announcements"))
        .collect();
    assert_eq!(
        asked,
        [
            &format!("{base}/api/corporate-announcements?index=equities&symbol=INFY"),
            &format!("{base}/api/corporate-announcements?index=equities&symbol=TCS"),
        ]
    );
    assert_eq!(limiter.count(), 3);
    assert!(
        archive
            .exists(&archive_key(&format!("{KIND_ANNOUNCEMENTS}/INFY"), on(), "json").unwrap())
            .await
            .unwrap()
    );
    let symbols: std::collections::BTreeSet<&str> =
        records.iter().map(|r| r.symbol.as_str()).collect();
    assert_eq!(symbols.into_iter().collect::<Vec<_>>(), ["INFY"]);
}

#[tokio::test]
async fn a_second_run_the_same_morning_parses_the_archive() {
    arm();
    let (_dir, archive) = archive();
    let fake = client();
    let provider = build(settings(), archive, fake.clone(), None);
    provider
        .announcements(&["INFY".to_owned()], on())
        .await
        .unwrap();
    let before = fake.requests().len();
    provider
        .announcements(&["INFY".to_owned()], on())
        .await
        .unwrap();
    assert_eq!(fake.requests().len(), before);
}

/// A renamed symbol answers 404. No row, no error — the same rule as a missing quote.
#[tokio::test]
async fn a_symbol_nse_does_not_know_is_absence() {
    arm();
    let (_dir, archive) = archive();
    let fake = Arc::new(FakeHttpClient::new(&[]).fallback(b"[]").status(404));
    let records = build(settings(), archive, fake, None)
        .announcements(&["OLDNAME".to_owned()], on())
        .await
        .unwrap();
    assert!(records.is_empty());
}

#[tokio::test]
async fn a_rate_limit_or_an_outage_is_the_provider_error_not_a_swallow() {
    arm();
    for status in [429, 503] {
        let (_dir, archive) = archive();
        let fake = Arc::new(
            FakeHttpClient::new(&[("corporate-announcements", b"[]".to_vec())]).status(status),
        );
        let err = build(settings(), archive, fake, None)
            .announcements(&["INFY".to_owned()], on())
            .await
            .unwrap_err();
        assert!(err.is_provider_error(), "{err}");
    }
}

/// 403 is how NSE's edge refuses a session it does not like; that is not absence.
#[tokio::test]
async fn a_forbidden_answer_is_loud() {
    arm();
    let (_dir, archive) = archive();
    let fake = Arc::new(FakeHttpClient::new(&[]).fallback(b"[]").status(403));
    let err = build(settings(), archive, fake, None)
        .announcements(&["INFY".to_owned()], on())
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
}

// --- TestResultsCalendar ---------------------------------------------------------------------

#[test]
fn only_result_meetings_are_earnings_dates_soonest_first() {
    arm();
    let records =
        parse_results_calendar(&recorded("event-calendar-INFY.json"), "INFY", PAGE).unwrap();
    let got: Vec<(NaiveDate, &str)> = records
        .iter()
        .map(|r| (r.event_date, r.purpose.as_str()))
        .collect();
    assert_eq!(
        got,
        [
            (
                NaiveDate::from_ymd_opt(2026, 4, 16).unwrap(),
                "Financial Results/Dividend"
            ),
            (
                NaiveDate::from_ymd_opt(2026, 10, 15).unwrap(),
                "Financial Results"
            ),
        ]
    );
    assert!(
        records[0]
            .url
            .ends_with("corporate-filings-event-calendar?symbol=INFY")
    );
}

#[tokio::test]
async fn the_results_calendar_goes_through_the_provider_archived_and_limited() {
    arm();
    let (_dir, archive) = archive();
    let limiter = Arc::new(InertLimiter::default());
    let fake = client();
    let records = build(
        settings(),
        archive.clone(),
        fake.clone(),
        Some(limiter.clone()),
    )
    .results_calendar(&["INFY".to_owned()], on())
    .await
    .unwrap();
    let base = settings().nse_base_url;
    let asked: Vec<String> = fake
        .requests()
        .into_iter()
        .filter(|u| u.contains("event-calendar"))
        .collect();
    assert_eq!(
        asked,
        [format!(
            "{base}/api/event-calendar?index=equities&symbol=INFY"
        )]
    );
    assert_eq!(limiter.count(), 2);
    assert!(
        archive
            .exists(&archive_key(&format!("{KIND_EVENT_CALENDAR}/INFY"), on(), "json").unwrap())
            .await
            .unwrap()
    );
    let dates: Vec<NaiveDate> = records.iter().map(|r| r.event_date).collect();
    assert_eq!(
        dates,
        [
            NaiveDate::from_ymd_opt(2026, 4, 16).unwrap(),
            NaiveDate::from_ymd_opt(2026, 10, 15).unwrap()
        ]
    );
    assert_eq!(records[0].url, format!("{PAGE}INFY"));
}

#[test]
fn an_earnings_date_the_calendar_cannot_date_is_dropped() {
    arm();
    let records = parse_results_calendar(
        &recorded("event-calendar-INFY.json"),
        "INFY",
        "https://p?s=",
    )
    .unwrap();
    assert_eq!(records.len(), 2);
}

/// A symbol in the page link is `urllib.parse.quote`d (`M&M` -> `M%26M`).
#[test]
fn the_page_link_quotes_the_symbol() {
    arm();
    let payload = br#"[{"bm_desc":"Quarterly result  ","date":"2026-02-03","symbol":"M&M"}]"#;
    let records = parse_results_calendar(payload, "INFY", "https://p?s=").unwrap();
    assert_eq!(records[0].url, "https://p?s=M%26M");
    assert_eq!(records[0].purpose, "Quarterly result");
}

#[tokio::test]
async fn a_results_calendar_outage_is_the_provider_error() {
    arm();
    let (_dir, archive) = archive();
    let fake = Arc::new(FakeHttpClient::new(&[]).raises("connection reset"));
    let err = build(settings(), archive, fake, None)
        .results_calendar(&["INFY".to_owned()], on())
        .await
        .unwrap_err();
    assert!(err.is_provider_error(), "{err}");
}

/// `datetime.strptime` refuses a leap second: `_strptime`'s `%S` matches 60 and 61, then
/// `datetime(...)` raises "second must be in 0..59", so `_stamp` falls through every format to
/// `None`. Checked against the live Python: `_stamp("01-Sep-2026 18:32:60") is None`, and the
/// same for `61`; `59` parses. A port that clamps 60 to 59 would invent a stamp.
#[test]
fn a_leap_second_is_not_a_stamp() {
    arm();
    use baskfy_providers::nse::{PyValue, stamp};
    for text in [
        "01-Sep-2026 18:32:60",
        "01-Sep-2026 18:32:61",
        "2026-09-01 18:32:60",
    ] {
        assert_eq!(
            stamp(&PyValue::Str(text.to_owned())).unwrap(),
            None,
            "{text}"
        );
    }
    let kept = stamp(&PyValue::Str("01-Sep-2026 18:32:59".to_owned()))
        .unwrap()
        .unwrap();
    assert_eq!(
        kept,
        ist().with_ymd_and_hms(2026, 9, 1, 18, 32, 59).unwrap()
    );
}
