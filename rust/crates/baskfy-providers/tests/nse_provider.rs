//! `NseProvider` (Prompt 2 deliverable 3) — `test_nse.py`, `test_af_dividend_sum.py` and the
//! unwired-NSE case of `test_network_is_blocked.py`, case for case.
//!
//! Every test drives a fake HTTP client through `NseRuntime`, so nothing reaches the network
//! while the cookie priming, the archive-before-parse ordering and every parser are exercised
//! against realistic file shapes.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;
mod nse_common;

use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use baskfy_providers::archive::{RawArchive, archive_key};
use baskfy_providers::errors::{ErrorKind, ProviderError};
use baskfy_providers::nse::{
    KIND_BHAVCOPY, KIND_CORPORATE_ACTIONS, KIND_CORPORATE_ACTIONS_WINDOWED, NSE_DEFAULT_PAGE_ROWS,
    NseProvider, NseRuntime, parse_corporate_action_purpose, parse_corporate_action_purposes,
    parse_equity_quote,
};
use baskfy_providers::ports::{Capability, HealthReporting, reference_capabilities};
use baskfy_providers::records::{CorporateActionType, bhavcopy_schema};
use baskfy_providers::settings::ProviderSettings;
use chrono::NaiveDate;
use common::arm;
use nse_common::{FakeHttpClient, InertLimiter, archive, build, settings, zip_one};
use polars::prelude::{AnyValue, DataFrame};
use rust_decimal::Decimal;

fn on() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 18).unwrap()
}

fn d(text: &str) -> Decimal {
    Decimal::from_str(text).unwrap()
}

fn day(y: i32, m: u32, dd: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, dd).unwrap()
}

fn client(routes: &[(&str, Vec<u8>)]) -> Arc<FakeHttpClient> {
    Arc::new(FakeHttpClient::new(routes))
}

fn bare(settings: ProviderSettings) -> NseProvider {
    NseProvider::new(settings, None, NseRuntime::default()).unwrap()
}

fn assert_kind(err: &ProviderError, kind: ErrorKind, needle: &str) {
    assert_eq!(err.kind(), kind, "{err}");
    assert!(err.to_string().contains(needle), "{err} lacks {needle:?}");
}

/// The cell at (`symbol` row, `column`) as a Decimal (the frame's Decimal(p, s) cell).
fn cell_decimal(frame: &DataFrame, symbol: &str, column: &str) -> Option<Decimal> {
    let symbols = frame.column("symbol").unwrap();
    let index = (0..frame.height())
        .find(|i| symbols.get(*i).unwrap().get_str() == Some(symbol))
        .unwrap();
    match frame.column(column).unwrap().get(index).unwrap() {
        AnyValue::Decimal(v, _, s) => {
            Some(Decimal::from_i128_with_scale(v, u32::try_from(s).unwrap()))
        }
        AnyValue::Null => None,
        other => panic!("{column} is {other:?}"),
    }
}

fn cell_str(frame: &DataFrame, row: usize, column: &str) -> Option<String> {
    match frame.column(column).unwrap().get(row).unwrap() {
        AnyValue::Null => None,
        v => Some(v.get_str().unwrap().to_owned()),
    }
}

// --- TestThrottleIsMandatory -----------------------------------------------------------------

/// AF 3.11 — an unthrottled NSE client must refuse, not silently fire.
#[tokio::test]
async fn throttle_raises_without_a_limiter() {
    arm();
    let err = bare(settings()).throttle().await.unwrap_err();
    assert_kind(&err, ErrorKind::ProviderUnavailable, "rate limiter");
}

// --- TestCapabilities ------------------------------------------------------------------------

/// docs/09's table: NSE provides ReferenceProvider. Bars come from Kite.
#[test]
fn it_offers_only_reference_data() {
    arm();
    let provider = bare(settings());
    assert_eq!(provider.capabilities(), reference_capabilities());
    assert!(!provider.capabilities().contains(&Capability::DailyBars));
}

// --- TestHealth ------------------------------------------------------------------------------

#[test]
fn without_an_archive_it_is_unavailable() {
    arm();
    let health = bare(settings()).check();
    assert!(!health.available);
    assert!(health.detail.contains("archive"));
}

#[test]
fn fully_wired_is_available() {
    arm();
    let (_dir, archive) = archive();
    let health = build(settings(), archive, client(&[]), None).check();
    assert!(health.available);
    assert!(health.detail.starts_with("archiving to local directory "));
}

// --- TestBrowserSession ----------------------------------------------------------------------

/// docs/09: "public files need a browser-like session (cookie priming)".
#[tokio::test]
async fn the_site_root_is_visited_before_the_archive_file() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[("EQUITY_L.csv", listings_csv())]);
    build(settings(), archive, fake.clone(), None)
        .listings()
        .await
        .unwrap();
    let requests = fake.requests();
    assert_eq!(requests[0], settings().nse_base_url);
    assert!(requests[1].contains("EQUITY_L.csv"));
}

/// Re-priming on every file is itself a traffic pattern that gets throttled.
#[tokio::test]
async fn cookies_are_primed_once_per_provider_not_once_per_request() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[
        ("EQUITY_L.csv", listings_csv()),
        ("ind_close_all", index_snapshot_csv()),
    ]);
    let provider = build(settings(), archive, fake.clone(), None);
    provider.listings().await.unwrap();
    provider.index_snapshots(on()).await.unwrap();
    let base = settings().nse_base_url;
    assert_eq!(fake.requests().iter().filter(|u| **u == base).count(), 1);
}

/// docs/09: NSE's public files are rate-sensitive.
#[tokio::test]
async fn every_request_takes_a_rate_limit_token() {
    arm();
    let (_dir, archive) = archive();
    let limiter = Arc::new(InertLimiter::default());
    let fake = client(&[("EQUITY_L.csv", listings_csv())]);
    build(settings(), archive, fake, Some(limiter.clone()))
        .listings()
        .await
        .unwrap();
    // One for the cookie prime, one for the file.
    assert_eq!(limiter.count(), 2);
}

/// AF 3.11: a 401/403 clears the primed flag, re-primes once and retries the file once.
#[tokio::test]
async fn a_forbidden_answer_re_primes_the_cookie_jar_once() {
    arm();
    let (_dir, archive) = archive();
    let limiter = Arc::new(InertLimiter::default());
    let fake = Arc::new(FakeHttpClient::new(&[("EQUITY_L.csv", listings_csv())]).status(403));
    let err = build(settings(), archive, fake.clone(), Some(limiter.clone()))
        .listings()
        .await
        .unwrap_err();
    assert_kind(&err, ErrorKind::UnexpectedPayload, "returned 403");
    let base = settings().nse_base_url;
    let requests = fake.requests();
    assert_eq!(requests.len(), 4);
    assert_eq!(requests[0], base);
    assert_eq!(requests[2], base);
    assert_eq!(limiter.count(), 4);
}

// --- TestArchiveFirst ------------------------------------------------------------------------

#[tokio::test]
async fn the_raw_file_is_archived_before_parsing() {
    arm();
    let (_dir, archive) = archive();
    build(
        settings(),
        archive.clone(),
        client(&[("BhavCopy", bhavcopy_zip())]),
        None,
    )
    .bhavcopy(on())
    .await
    .unwrap();
    assert!(
        archive
            .exists(&archive_key(KIND_BHAVCOPY, on(), "zip").unwrap())
            .await
            .unwrap()
    );
}

/// docs/09: "Never re-fetch to re-parse".
#[tokio::test]
async fn a_second_call_parses_the_archive_without_refetching() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[("BhavCopy", bhavcopy_zip())]);
    let provider = build(settings(), archive, fake.clone(), None);
    provider.bhavcopy(on()).await.unwrap();
    let before = fake.requests().len();
    provider.bhavcopy(on()).await.unwrap();
    assert_eq!(fake.requests().len(), before);
}

#[tokio::test]
async fn without_an_archive_it_refuses_rather_than_parsing_in_memory() {
    arm();
    let provider = NseProvider::new(
        settings(),
        None,
        NseRuntime {
            client: Some(client(&[])),
            ..NseRuntime::default()
        },
    )
    .unwrap();
    let err = provider.bhavcopy(on()).await.unwrap_err();
    assert_kind(&err, ErrorKind::ProviderUnavailable, "archive");
}

// --- TestBhavcopy ----------------------------------------------------------------------------

#[tokio::test]
async fn it_conforms_to_the_schema() {
    arm();
    let (_dir, archive) = archive();
    let frame = build(
        settings(),
        archive,
        client(&[("BhavCopy", bhavcopy_zip())]),
        None,
    )
    .bhavcopy(on())
    .await
    .unwrap();
    assert_eq!(*frame.schema().as_ref(), bhavcopy_schema());
}

/// docs/09 §"Provider ports": "bhavcopy(on) ... incl. circuit bands, series".
#[tokio::test]
async fn series_and_circuit_bands_are_carried() {
    arm();
    let (_dir, archive) = archive();
    let frame = build(
        settings(),
        archive,
        client(&[("BhavCopy", bhavcopy_zip())]),
        None,
    )
    .bhavcopy(on())
    .await
    .unwrap();
    assert_eq!(cell_str(&frame, 0, "series").as_deref(), Some("EQ"));
    assert_eq!(
        cell_decimal(&frame, "SBIN", "upper_circuit"),
        Some(d("880.0000"))
    );
    assert_eq!(
        cell_decimal(&frame, "SBIN", "lower_circuit"),
        Some(d("720.0000"))
    );
    // Byte-exact scale, as Python's Decimal('880.0000') prints.
    assert_eq!(
        cell_decimal(&frame, "SBIN", "upper_circuit")
            .unwrap()
            .to_string(),
        "880.0000"
    );
    assert_eq!(
        cell_decimal(&frame, "CUPID", "upper_circuit")
            .unwrap()
            .to_string(),
        "340.8360"
    );
}

/// docs/13 §2 finding 5: `volume` in the export is traded value, not a share count.
#[tokio::test]
async fn turnover_is_read_as_rupees() {
    arm();
    let (_dir, archive) = archive();
    let frame = build(
        settings(),
        archive,
        client(&[("BhavCopy", bhavcopy_zip())]),
        None,
    )
    .bhavcopy(on())
    .await
    .unwrap();
    let turnover = cell_decimal(&frame, "SBIN", "turnover").unwrap();
    assert_eq!(turnover, d("8000000.00"));
    assert_eq!(turnover.to_string(), "8000000.00");
}

/// NSE renamed every bhavcopy column; a 15-year backfill spans both spellings.
#[tokio::test]
async fn the_legacy_column_vocabulary_is_accepted() {
    arm();
    let (_dir, archive) = archive();
    let legacy = b"SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TOTALTRADES\n\
                   SBIN,EQ,790,805,788,800,795,10000,8000000,1200\n"
        .to_vec();
    let frame = build(settings(), archive, client(&[("BhavCopy", legacy)]), None)
        .bhavcopy(on())
        .await
        .unwrap();
    assert_eq!(frame.height(), 1);
    assert_eq!(cell_str(&frame, 0, "symbol").as_deref(), Some("SBIN"));
    assert_eq!(cell_decimal(&frame, "SBIN", "close"), Some(d("800.0000")));
    assert_eq!(cell_decimal(&frame, "SBIN", "upper_circuit"), None);
}

/// A day with no rows is a publication failure; the parser must not pretend it read a valid file.
#[tokio::test]
async fn an_empty_file_is_an_error_not_an_empty_frame() {
    arm();
    let (_dir, archive) = archive();
    let header_only = b"TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric\n".to_vec();
    let err = build(
        settings(),
        archive,
        client(&[("BhavCopy", header_only)]),
        None,
    )
    .bhavcopy(on())
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
}

// --- TestListings ----------------------------------------------------------------------------

#[tokio::test]
async fn listing_rows_are_mapped() {
    arm();
    let (_dir, archive) = archive();
    let records = build(
        settings(),
        archive,
        client(&[("EQUITY_L", listings_csv())]),
        None,
    )
    .listings()
    .await
    .unwrap();
    let symbols: Vec<&str> = records.iter().map(|r| r.symbol.as_str()).collect();
    assert_eq!(symbols, ["SBIN", "CUPID"]);
    assert_eq!(records[0].listed_on, Some(day(1997, 3, 1)));
    assert_eq!(records[0].face_value, Some(d("1")));
    assert_eq!(records[0].name, "STATE BANK OF INDIA");
    assert_eq!(records[0].market_lot, Some(1));
    assert_eq!(records[0].isin.as_deref(), Some("INE062A01020"));
}

/// NSE's EQUITY_L.csv really does ship columns named " SERIES" with a leading space.
#[tokio::test]
async fn the_leading_space_column_names_nse_publishes_are_handled() {
    arm();
    let (_dir, archive) = archive();
    let records = build(
        settings(),
        archive,
        client(&[("EQUITY_L", listings_csv())]),
        None,
    )
    .listings()
    .await
    .unwrap();
    assert_eq!(records[0].series.as_deref(), Some("EQ"));
}

/// The Emerge register has no MARKET_LOT: None, never a guessed 1.
#[tokio::test]
async fn the_sme_register_reads_underscore_headers_and_leaves_the_lot_null() {
    arm();
    let (_dir, archive) = archive();
    let csv =
        b"SYMBOL,NAME_OF_COMPANY,SERIES,DATE_OF_LISTING,PAID_UP_VALUE,ISIN_NUMBER,FACE_VALUE\n\
                ABC,ABC LIMITED,SM,05-Jan-2024,10,INE000A01011,10\n"
            .to_vec();
    let fake = client(&[("SME_EQUITY_L", csv)]);
    let records = build(settings(), archive, fake.clone(), None)
        .sme_listings()
        .await
        .unwrap();
    assert!(fake.requests()[1].ends_with("/emerge/corporates/content/SME_EQUITY_L.csv"));
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].series.as_deref(), Some("SM"));
    assert_eq!(records[0].market_lot, None);
    assert_eq!(records[0].listed_on, Some(day(2024, 1, 5)));
}

#[tokio::test]
async fn etf_symbols_are_sorted_upper_cased_and_deduplicated() {
    arm();
    let (_dir, archive) = archive();
    let csv = b"Symbol,Underlying\nniftybees,NIFTY\nGOLDBEES,GOLD\nNIFTYBEES,NIFTY\n".to_vec();
    let symbols = build(settings(), archive, client(&[("eq_etfseclist", csv)]), None)
        .etf_symbols()
        .await
        .unwrap();
    assert_eq!(symbols, ["GOLDBEES", "NIFTYBEES"]);
}

// --- TestIndexSnapshots ----------------------------------------------------------------------

#[tokio::test]
async fn levels_and_fundamentals_are_read() {
    arm();
    let (_dir, archive) = archive();
    let snapshots = build(
        settings(),
        archive,
        client(&[("ind_close_all", index_snapshot_csv())]),
        None,
    )
    .index_snapshots(on())
    .await
    .unwrap();
    let nifty = snapshots
        .iter()
        .find(|s| s.index_slug == "nifty-50")
        .unwrap();
    assert_eq!(nifty.level, Some(d("24500.35")));
    assert_eq!(nifty.pe, Some(d("22.4")));
}

/// docs/01 §7: India VIX and inverse indices render PE/PB/DivYield as `-`. Zero is a lie.
#[tokio::test]
async fn a_derived_index_with_no_fundamentals_yields_none_not_zero() {
    arm();
    let (_dir, archive) = archive();
    let snapshots = build(
        settings(),
        archive,
        client(&[("ind_close_all", index_snapshot_csv())]),
        None,
    )
    .index_snapshots(on())
    .await
    .unwrap();
    let vix = snapshots
        .iter()
        .find(|s| s.index_slug == "INDIA VIX")
        .unwrap();
    assert_eq!(vix.pe, None);
    assert_eq!(vix.pb, None);
}

/// SW16: the first row of `nse/index-snapshot/2026-08-27.csv` as archived on the box — mixed-case
/// name, `-.48` with no leading zero — parses to the level the box holds for that day.
#[tokio::test]
async fn the_archived_file_from_the_box_parses_to_the_published_close() {
    arm();
    let (_dir, archive) = archive();
    let snapshots = build(
        settings(),
        archive,
        client(&[("ind_close_all_27082026", archived_index_row_from_the_box())]),
        None,
    )
    .index_snapshots(day(2026, 8, 27))
    .await
    .unwrap();
    let nifty = snapshots
        .iter()
        .find(|s| s.index_slug == "nifty-50")
        .unwrap();
    assert_eq!(nifty.level, Some(d("24090.85")));
    assert_eq!(nifty.change_abs, Some(d("-116.9")));
    assert_eq!(nifty.change_pct, Some(d("-0.48")));
    assert_eq!(nifty.pe, Some(d("20.37")));
    // Byte-exact: Python's Decimal(str(-0.48)) is "-0.48".
    assert_eq!(nifty.change_pct.unwrap().to_string(), "-0.48");
}

/// SW16: the file fetched for a date must say that date in its own `Index Date`.
#[tokio::test]
async fn a_file_dated_for_another_session_is_refused() {
    arm();
    let (_dir, archive) = archive();
    let err = build(
        settings(),
        archive,
        client(&[("ind_close_all", index_snapshot_csv())]),
        None,
    )
    .index_snapshots(day(2026, 8, 19))
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    let text = err.to_string();
    let a = text.find("2026-08-19").unwrap();
    assert!(text[a..].contains("2026-08-18"), "{text}");
}

// --- TestIndexConstituents -------------------------------------------------------------------

#[tokio::test]
async fn constituent_symbols_are_returned() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[(
        "ind_nifty50list",
        b"Company Name,Industry,Symbol\nSBI,Bank,SBIN\n".to_vec(),
    )]);
    let symbols = build(settings(), archive, fake.clone(), None)
        .index_constituents("nifty-50", on())
        .await
        .unwrap();
    assert_eq!(symbols, ["SBIN"]);
    assert_eq!(
        fake.requests()[1],
        "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
    );
}

/// The four constituent files the rule cannot derive are named, not guessed (M8).
#[tokio::test]
async fn an_overridden_constituent_file_name_is_used() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[("ind_", b"Symbol\nKAYNES\n".to_vec())]);
    build(settings(), archive, fake.clone(), None)
        .index_constituents("nifty-mid-small-400", on())
        .await
        .unwrap();
    assert!(fake.requests()[1].ends_with("/content/indices/ind_niftymidsmallcap400list.csv"));
}

/// docs/06 §"Step 2": nifty-allcap and etf are defined by rule, not by an NSE file.
#[tokio::test]
async fn a_derived_universe_is_refused_with_an_explanation() {
    arm();
    let (_dir, archive) = archive();
    let err = build(settings(), archive, client(&[]), None)
        .index_constituents("nifty-allcap", on())
        .await
        .unwrap_err();
    assert_kind(&err, ErrorKind::UnexpectedPayload, "derived by rule");
}

#[tokio::test]
async fn an_unknown_universe_is_refused() {
    arm();
    let (_dir, archive) = archive();
    let err = build(settings(), archive, client(&[]), None)
        .index_constituents("nifty-4000", on())
        .await
        .unwrap_err();
    assert_kind(&err, ErrorKind::UnexpectedPayload, "unknown universe");
}

#[tokio::test]
async fn a_file_without_a_symbol_column_is_an_error() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[(
        "ind_nifty50list",
        b"Company Name,Industry\nSBI,Bank\n".to_vec(),
    )]);
    let err = build(settings(), archive, fake, None)
        .index_constituents("nifty-50", on())
        .await
        .unwrap_err();
    assert_kind(&err, ErrorKind::UnexpectedPayload, "expected columns");
    assert_eq!(
        err.message(),
        "nifty-50 constituents: none of the expected columns ['Symbol', 'SYMBOL', 'symbol'] are \
         present; got ['Company Name', 'Industry']"
    );
}

// --- TestHttpFailures ------------------------------------------------------------------------

#[tokio::test]
async fn a_5xx_is_transient() {
    arm();
    let (_dir, archive) = archive();
    let fake = Arc::new(FakeHttpClient::new(&[("EQUITY_L", b"x".to_vec())]).status(503));
    let err = build(settings(), archive, fake, None)
        .listings()
        .await
        .unwrap_err();
    let text = err.to_string();
    assert!(text.contains("503") || text.contains("gave up"), "{text}");
    assert_eq!(err.kind(), ErrorKind::RetryBudgetExhausted);
    assert_eq!(
        err.last_error().unwrap().kind(),
        ErrorKind::UpstreamUnavailable
    );
}

#[tokio::test]
async fn a_connection_error_is_translated() {
    arm();
    let (_dir, archive) = archive();
    let fake = Arc::new(FakeHttpClient::new(&[]).raises("connection reset"));
    let err = build(settings(), archive, fake, None)
        .listings()
        .await
        .unwrap_err();
    assert!(
        err.to_string().contains("gave up") || err.kind() == ErrorKind::UpstreamUnavailable,
        "{err}"
    );
    let inner = err.last_error().unwrap_or(&err);
    assert_eq!(inner.kind(), ErrorKind::UpstreamUnavailable);
    assert!(inner.message().contains("failed: connection reset"));
}

#[tokio::test]
async fn a_rate_limited_answer_is_transient() {
    arm();
    let (_dir, archive) = archive();
    let fake = Arc::new(FakeHttpClient::new(&[]).status(429));
    let err = build(settings(), archive, fake, None)
        .listings()
        .await
        .unwrap_err();
    let inner = err.last_error().unwrap();
    assert_eq!(inner.kind(), ErrorKind::UpstreamUnavailable);
    assert!(inner.message().starts_with("NSE rate-limited GET "));
}

// --- TestCorporateActionPurposes -------------------------------------------------------------

#[test]
fn a_face_value_split_is_read_as_a_split() {
    arm();
    let parsed = parse_corporate_action_purpose("FACE VALUE SPLIT FROM RS.10/- TO RE.1/-").unwrap();
    assert_eq!(
        parsed,
        Some((
            CorporateActionType::Split,
            Some(d("10")),
            Some(d("1")),
            None
        ))
    );
}

/// docs/04: bonus 4:1 -> ratio_from=4, ratio_to=1.
#[test]
fn a_bonus_ratio_keeps_the_documented_orientation() {
    arm();
    assert_eq!(
        parse_corporate_action_purpose("BONUS 4:1").unwrap(),
        Some((CorporateActionType::Bonus, Some(d("4")), Some(d("1")), None))
    );
}

#[test]
fn a_cash_dividend_carries_an_amount() {
    arm();
    let parsed = parse_corporate_action_purpose("DIVIDEND - RS.12.50 PER SHARE").unwrap();
    assert_eq!(
        parsed,
        Some((CorporateActionType::Dividend, None, None, Some(d("12.50"))))
    );
    assert_eq!(parsed.unwrap().3.unwrap().to_string(), "12.50");
}

#[test]
fn rights_are_recognised_even_without_a_ratio() {
    arm();
    assert_eq!(
        parse_corporate_action_purpose("RIGHTS ISSUE").unwrap(),
        Some((CorporateActionType::Rights, None, None, None))
    );
}

#[test]
fn a_demerger_is_recognised() {
    arm();
    assert_eq!(
        parse_corporate_action_purpose("SCHEME OF DEMERGER").unwrap(),
        Some((CorporateActionType::Demerger, None, None, None))
    );
}

/// AF 3.2 — a combined purpose must not drop the bonus leg.
#[test]
fn split_and_bonus_emit_both_legs() {
    arm();
    let legs =
        parse_corporate_action_purposes("FACE VALUE SPLIT FROM RS.10/- TO RE.1/- AND BONUS 1:1")
            .unwrap();
    let kinds: Vec<CorporateActionType> = legs.iter().map(|l| l.0).collect();
    assert_eq!(
        kinds,
        [CorporateActionType::Split, CorporateActionType::Bonus]
    );
}

/// AF 3.3 — every RS amount in the purpose contributes (`test_af_dividend_sum.py` too).
#[test]
fn dividend_legs_are_summed() {
    arm();
    let parsed = parse_corporate_action_purpose("DIVIDEND - RS.2.50 + RS.1.00 PER SHARE").unwrap();
    assert_eq!(
        parsed,
        Some((CorporateActionType::Dividend, None, None, Some(d("3.50"))))
    );
    assert_eq!(parsed.unwrap().3.unwrap().to_string(), "3.50");
}

/// A wrong action puts a wrong adjustment factor on every price before its ex-date.
#[test]
fn an_unmodelled_purpose_is_dropped_rather_than_guessed() {
    arm();
    for purpose in [
        "ANNUAL GENERAL MEETING",
        "CHANGE IN NAME",
        "BOARD MEETING INTIMATION",
        "",
    ] {
        assert_eq!(
            parse_corporate_action_purpose(purpose).unwrap(),
            None,
            "{purpose}"
        );
    }
}

/// Lower-case input is upper-cased first, and `&` separates legs as `AND` does.
#[test]
fn purposes_are_case_insensitive_and_split_on_ampersand() {
    arm();
    let legs = parse_corporate_action_purposes("Bonus 1:1 & Dividend - Rs 2 Per Share").unwrap();
    assert_eq!(
        legs,
        [
            (CorporateActionType::Bonus, Some(d("1")), Some(d("1")), None),
            (CorporateActionType::Dividend, None, None, Some(d("2"))),
        ]
    );
}

// --- TestEquityFundamentals ------------------------------------------------------------------

#[test]
fn issued_size_times_last_price_is_marketcap_in_crore() {
    arm();
    let record = parse_equity_quote(&get_symbol_data_json(), on(), "INFY")
        .unwrap()
        .unwrap();
    assert_eq!(record.symbol, "INFY");
    assert_eq!(record.shares_outstanding, Some(4_058_056_597));
    // 4_058_056_597 * 1144 / 1e7, half-up.
    assert_eq!(record.marketcap_cr, Some(464_242));
    assert_eq!(record.pe, Some(d("15.12")));
    assert_eq!(record.last_price.unwrap().to_string(), "1144");
}

/// A pre-migration file must never become unreadable — that is why we archive bytes.
#[test]
fn the_retired_payload_still_parses_out_of_the_archive() {
    arm();
    let record = parse_equity_quote(&quote_equity_json(), on(), "INFY")
        .unwrap()
        .unwrap();
    assert_eq!(record.shares_outstanding, Some(4_148_506_959));
    assert_eq!(record.marketcap_cr, Some(767_578));
    assert_eq!(record.pe, Some(d("24.5")));
    assert_eq!(record.pb, Some(d("7.1")));
}

/// An absent number must look absent.
#[test]
fn pb_and_div_yield_are_null_because_the_live_payload_omits_them() {
    arm();
    let record = parse_equity_quote(&get_symbol_data_json(), on(), "INFY")
        .unwrap()
        .unwrap();
    assert_eq!(record.pb, None);
    assert_eq!(record.div_yield, None);
}

/// NSE answers a wrong series with 200 and an empty list, not an error.
#[test]
fn a_wrong_series_is_not_a_row() {
    arm();
    assert!(
        parse_equity_quote(br#"{"equityResponse":[]}"#, on(), "INFY")
            .unwrap()
            .is_none()
    );
}

/// The literal shape NSE returns for a name it does not quote (DSFCL, 2026-08-18): every
/// section is JSON null, not absent.
#[test]
fn an_entry_whose_sections_are_all_null_is_not_a_row() {
    arm();
    let payload = br#"{"equityResponse":[{"orderBook":null,"metaData":null,"tradeInfo":null,"priceInfo":null,"secInfo":null,"lastUpdateTime":null}]}"#;
    assert!(
        parse_equity_quote(payload, on(), "DSFCL")
            .unwrap()
            .is_none()
    );
}

#[test]
fn nse_total_marketcap_is_used_when_shares_are_missing() {
    arm();
    let payload = br#"{"equityResponse":[{"metaData":{"symbol":"X"},"tradeInfo":{"totalMarketCap":4642416746968},"secInfo":{"pdSymbolPe":"15.12"}}]}"#;
    let record = parse_equity_quote(payload, on(), "X").unwrap().unwrap();
    assert_eq!(record.shares_outstanding, None);
    assert_eq!(record.marketcap_cr, Some(464_242));
}

/// `Decimal(0)` is falsy: a zero last price falls through to the next source, as Python's `or`.
#[test]
fn a_zero_last_price_falls_through_to_the_close() {
    arm();
    let payload = br#"{"equityResponse":[{"metaData":{"symbol":"X","closePrice":100},"tradeInfo":{"issuedSize":10000000,"lastPrice":0}}]}"#;
    let record = parse_equity_quote(payload, on(), "X").unwrap().unwrap();
    assert_eq!(record.last_price, Some(d("100")));
    assert_eq!(record.marketcap_cr, Some(100));
}

#[test]
fn a_quote_that_is_not_json_is_an_unexpected_payload() {
    arm();
    let err = parse_equity_quote(b"<html>", on(), "X").unwrap_err();
    assert_kind(
        &err,
        ErrorKind::UnexpectedPayload,
        "equity quote for X is not JSON",
    );
    let err = parse_equity_quote(b"[1]", on(), "X").unwrap_err();
    assert_kind(&err, ErrorKind::UnexpectedPayload, "is not an object");
}

#[tokio::test]
async fn a_missing_quote_is_skipped_not_zero() {
    arm();
    let (_dir, archive) = archive();
    let fake = Arc::new(FakeHttpClient::new(&[]).status(404));
    let records = build(settings(), archive, fake, None)
        .equity_fundamentals(on(), &["NOSUCH".to_owned()], None)
        .await
        .unwrap();
    assert!(records.is_empty());
}

fn hint(symbol: &str, series: &str) -> HashMap<String, String> {
    HashMap::from([(symbol.to_owned(), series.to_owned())])
}

#[tokio::test]
async fn quotes_are_archived_per_symbol_and_series() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[("getSymbolData", get_symbol_data_json())]);
    let records = build(settings(), archive.clone(), fake.clone(), None)
        .equity_fundamentals(on(), &["INFY".to_owned()], Some(&hint("INFY", "EQ")))
        .await
        .unwrap();
    assert_eq!(records.len(), 1);
    assert!(
        archive
            .exists(&archive_key("equity-fundamentals/INFY-EQ", on(), "json").unwrap())
            .await
            .unwrap()
    );
    assert_eq!(
        fake.requests()[1],
        "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData\
         &marketType=N&series=EQ&symbol=INFY"
    );
}

/// The hint exists to avoid a lookup round trip; if it does not, it is pointless.
#[tokio::test]
async fn a_hinted_series_costs_exactly_one_request() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[("getSymbolData", get_symbol_data_json())]);
    build(settings(), archive, fake.clone(), None)
        .equity_fundamentals(on(), &["INFY".to_owned()], Some(&hint("INFY", "EQ")))
        .await
        .unwrap();
    let quote_calls: Vec<String> = fake
        .requests()
        .into_iter()
        .filter(|u| u.contains("GetQuoteApi"))
        .collect();
    assert_eq!(quote_calls.len(), 1);
    assert!(!quote_calls[0].contains("getMetaData"));
}

/// A wrong hint must cost a round trip, never a NULL row.
#[tokio::test]
async fn a_stale_hint_falls_back_to_the_series_nse_reports() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[
        ("series=EQ", br#"{"equityResponse":[]}"#.to_vec()),
        (
            "getMetaData",
            br#"{"symbol":"X","activeSeries":["BE"]}"#.to_vec(),
        ),
        ("series=BE", get_symbol_data_json()),
    ]);
    let records = build(settings(), archive, fake.clone(), None)
        .equity_fundamentals(on(), &["X".to_owned()], Some(&hint("X", "EQ")))
        .await
        .unwrap();
    assert_eq!(records.len(), 1);
    assert!(fake.requests().iter().any(|u| u.contains("getMetaData")));
}

#[tokio::test]
async fn no_hint_asks_nse_which_series_it_quotes() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[
        (
            "getMetaData",
            br#"{"symbol":"X","activeSeries":["BE"]}"#.to_vec(),
        ),
        ("series=BE", get_symbol_data_json()),
    ]);
    let records = build(settings(), archive, fake, None)
        .equity_fundamentals(on(), &["X".to_owned()], None)
        .await
        .unwrap();
    assert_eq!(records.len(), 1);
}

/// `EQ` wins when NSE lists it among several active series; otherwise the first listed.
#[tokio::test]
async fn eq_is_preferred_among_the_active_series() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[
        ("getMetaData", br#"{"activeSeries":["be"," eq "]}"#.to_vec()),
        ("series=EQ", get_symbol_data_json()),
    ]);
    let records = build(settings(), archive, fake.clone(), None)
        .equity_fundamentals(on(), &["x".to_owned()], None)
        .await
        .unwrap();
    assert_eq!(records.len(), 1);
    assert!(
        fake.requests()
            .iter()
            .any(|u| u.ends_with("series=EQ&symbol=X"))
    );
}

// --- TestFundamentalsConcurrency -------------------------------------------------------------

fn symbols(count: usize) -> Vec<String> {
    (0..count).map(|i| format!("SYM{i:03}")).collect()
}

fn hints(symbols: &[String]) -> HashMap<String, String> {
    symbols
        .iter()
        .map(|s| (s.clone(), "EQ".to_owned()))
        .collect()
}

fn with_concurrency(n: i64) -> ProviderSettings {
    let mut s = settings();
    s.nse_fetch_concurrency = n;
    s
}

fn quoted(fake: &FakeHttpClient) -> Vec<String> {
    fake.requests()
        .iter()
        .filter(|u| u.contains("getSymbolData"))
        .map(|u| u.split("symbol=").nth(1).unwrap().to_owned())
        .collect()
}

#[tokio::test]
async fn every_symbol_is_fetched_and_the_callers_order_is_kept() {
    arm();
    let (_dir, archive) = archive();
    let syms = symbols(12);
    let fake = client(&[("getSymbolData", get_symbol_data_json())]);
    let records = build(with_concurrency(4), archive, fake.clone(), None)
        .equity_fundamentals(on(), &syms, Some(&hints(&syms)))
        .await
        .unwrap();
    assert_eq!(records.len(), syms.len());
    let mut asked = quoted(&fake);
    asked.sort();
    assert_eq!(asked, syms);
}

/// Concurrency overlaps the waiting; it must not buy extra requests per second.
#[tokio::test]
async fn the_limiter_still_sees_one_acquire_per_request() {
    arm();
    let (_dir, archive) = archive();
    let syms = symbols(9);
    let fake = client(&[("getSymbolData", get_symbol_data_json())]);
    let limiter = Arc::new(InertLimiter::default());
    build(
        with_concurrency(3),
        archive,
        fake.clone(),
        Some(limiter.clone()),
    )
    .equity_fundamentals(on(), &syms, Some(&hints(&syms)))
    .await
    .unwrap();
    assert_eq!(limiter.count(), fake.requests().len());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrency_never_exceeds_the_setting() {
    arm();
    let (_dir, archive) = archive();
    let syms = symbols(16);
    let fake = Arc::new(
        FakeHttpClient::new(&[("getSymbolData", get_symbol_data_json())])
            .delay(Duration::from_millis(10)),
    );
    build(with_concurrency(4), archive, fake.clone(), None)
        .equity_fundamentals(on(), &syms, Some(&hints(&syms)))
        .await
        .unwrap();
    let peak = fake.peak.load(std::sync::atomic::Ordering::SeqCst);
    assert!(1 < peak && peak <= 4, "peak {peak}");
}

#[tokio::test]
async fn one_worker_keeps_the_old_sequential_path() {
    arm();
    let (_dir, archive) = archive();
    let syms = symbols(3);
    let fake = client(&[("getSymbolData", get_symbol_data_json())]);
    let records = build(with_concurrency(1), archive, fake.clone(), None)
        .equity_fundamentals(on(), &syms, Some(&hints(&syms)))
        .await
        .unwrap();
    assert_eq!(records.len(), 3);
    assert_eq!(quoted(&fake), syms);
}

/// Results keep the caller's order (house rule 7) even when workers finish out of order.
#[tokio::test]
async fn concurrent_results_come_back_in_the_callers_order() {
    arm();
    let (_dir, archive) = archive();
    let syms = vec!["AAA".to_owned(), "BBB".to_owned(), "CCC".to_owned()];
    let body = |s: &str| {
        format!(r#"{{"equityResponse":[{{"metaData":{{"symbol":"{s}"}},"secInfo":{{"pdSymbolPe":"1"}}}}]}}"#)
            .into_bytes()
    };
    let fake = client(&[
        ("symbol=AAA", body("AAA")),
        ("symbol=BBB", body("BBB")),
        ("symbol=CCC", body("CCC")),
    ]);
    let records = build(with_concurrency(3), archive, fake, None)
        .equity_fundamentals(on(), &syms, Some(&hints(&syms)))
        .await
        .unwrap();
    let got: Vec<&str> = records.iter().map(|r| r.symbol.as_str()).collect();
    assert_eq!(got, ["AAA", "BBB", "CCC"]);
}

/// A failure stops the run instead of fanning out over every remaining symbol. Python's
/// `pool.map` is read in input order and the first failed result it reaches cancels every
/// future not yet started, so a dead NSE costs a handful of requests, not one retry budget per
/// symbol of a ~3,300-name universe. The error raised is the first in input order.
#[tokio::test]
async fn a_failure_stops_the_workers_from_starting_more_symbols() {
    arm();
    let (_dir, archive) = archive();
    let syms = symbols(40);
    let fake = Arc::new(FakeHttpClient::new(&[]).raises("connection reset"));
    let error = build(with_concurrency(3), archive, fake.clone(), None)
        .equity_fundamentals(on(), &syms, Some(&hints(&syms)))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), ErrorKind::RetryBudgetExhausted, "{error}");
    let made = fake.requests().len();
    assert!(
        (1..=6).contains(&made),
        "{made} requests after the first failure"
    );
}

// --- TestCorporateActionWindow ---------------------------------------------------------------

fn ca_rows(count: usize) -> Vec<u8> {
    let rows: Vec<serde_json::Value> = (0..count)
        .map(|i| {
            serde_json::json!({
                "symbol": format!("SYM{i}"),
                "exDate": "11-Sep-2026",
                "subject": "Dividend - Rs 1 Per Share",
            })
        })
        .collect();
    serde_json::to_vec(&rows).unwrap()
}

fn sep4() -> NaiveDate {
    day(2026, 9, 4)
}

fn asked_ca(fake: &FakeHttpClient) -> String {
    fake.requests()
        .into_iter()
        .find(|u| u.contains("corporateActions"))
        .unwrap()
}

#[tokio::test]
async fn the_request_names_a_date_window() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[("corporates-corporateActions", ca_rows(3))]);
    build(settings(), archive, fake.clone(), None)
        .corporate_actions(sep4())
        .await
        .unwrap();
    let asked = asked_ca(&fake);
    assert!(asked.contains("from_date=04-09-2026"));
    assert!(asked.contains("to_date="));
    assert_eq!(
        asked,
        "https://www.nseindia.com/api/corporates-corporateActions?index=equities\
         &from_date=04-09-2026&to_date=02-01-2027"
    );
}

#[tokio::test]
async fn the_far_edge_follows_the_window_setting() {
    arm();
    let (_dir, archive) = archive();
    let mut s = settings();
    s.nse_corporate_action_window_days = 30;
    let fake = client(&[("corporates-corporateActions", ca_rows(3))]);
    build(s, archive, fake.clone(), None)
        .corporate_actions(sep4())
        .await
        .unwrap();
    assert!(asked_ca(&fake).contains("to_date=04-10-2026"));
}

/// NSE announces an ex-date ahead of it; storing early is safe (§21.9).
#[tokio::test]
async fn the_window_reaches_past_today_to_catch_announced_ex_dates() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[("corporates-corporateActions", ca_rows(3))]);
    build(settings(), archive, fake.clone(), None)
        .corporate_actions(sep4())
        .await
        .unwrap();
    let asked = asked_ca(&fake);
    let until = &asked.split("to_date=").nth(1).unwrap()[..10];
    let until = NaiveDate::parse_from_str(until, "%d-%m-%Y").unwrap();
    assert!(until > sep4());
}

/// Exactly 20 rows over a 120-day window is NSE ignoring the range, not a quiet quarter.
#[tokio::test]
async fn a_default_page_is_refused_rather_than_parsed() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[(
        "corporates-corporateActions",
        ca_rows(NSE_DEFAULT_PAGE_ROWS),
    )]);
    let err = build(settings(), archive, fake, None)
        .corporate_actions(sep4())
        .await
        .unwrap_err();
    assert_kind(&err, ErrorKind::UnexpectedPayload, "ignores the date range");
}

/// A genuinely quiet few days may hold 20 actions; the refusal cannot cry wolf.
#[tokio::test]
async fn a_short_window_is_allowed_to_return_the_page_size() {
    arm();
    let (_dir, archive) = archive();
    let mut s = settings();
    s.nse_corporate_action_window_days = 5;
    let fake = client(&[(
        "corporates-corporateActions",
        ca_rows(NSE_DEFAULT_PAGE_ROWS),
    )]);
    let actions = build(s, archive, fake, None)
        .corporate_actions(sep4())
        .await
        .unwrap();
    assert_eq!(actions.len(), NSE_DEFAULT_PAGE_ROWS);
}

#[tokio::test]
async fn one_row_fewer_than_a_page_is_not_refused() {
    arm();
    let (_dir, archive) = archive();
    let fake = client(&[(
        "corporates-corporateActions",
        ca_rows(NSE_DEFAULT_PAGE_ROWS - 1),
    )]);
    let actions = build(settings(), archive, fake, None)
        .corporate_actions(sep4())
        .await
        .unwrap();
    assert_eq!(actions.len(), NSE_DEFAULT_PAGE_ROWS - 1);
}

/// `fetch_and_archive` never re-fetches an existing key, so a truncated payload already on disk
/// under the old kind must not be able to answer the new question.
#[tokio::test]
async fn the_windowed_payload_is_archived_under_its_own_kind() {
    arm();
    let (_dir, archive) = archive();
    archive
        .put(
            &archive_key(KIND_CORPORATE_ACTIONS, sep4(), "json").unwrap(),
            &ca_rows(NSE_DEFAULT_PAGE_ROWS),
            "application/json",
        )
        .await
        .unwrap();
    let fake = client(&[("corporates-corporateActions", ca_rows(3))]);
    let actions = build(settings(), archive.clone(), fake, None)
        .corporate_actions(sep4())
        .await
        .unwrap();
    assert_eq!(
        actions.len(),
        3,
        "the stale un-windowed payload was re-parsed"
    );
    assert!(
        archive
            .exists(&archive_key(KIND_CORPORATE_ACTIONS_WINDOWED, sep4(), "json").unwrap())
            .await
            .unwrap()
    );
}

/// NSE's own row for PGIL, verbatim — the action that was missed.
#[tokio::test]
async fn pgil_the_action_that_was_missed_parses_and_survives_the_window() {
    arm();
    let (_dir, archive) = archive();
    let body = serde_json::to_vec(&serde_json::json!([{
        "symbol": "PGIL",
        "exDate": "11-Sep-2026",
        "subject": "Bonus 1:1",
        "faceVal": "5",
    }]))
    .unwrap();
    let fake = client(&[("corporates-corporateActions", body)]);
    let actions = build(settings(), archive, fake, None)
        .corporate_actions(sep4())
        .await
        .unwrap();
    assert_eq!(actions.len(), 1);
    assert_eq!(actions[0].symbol, "PGIL");
    assert_eq!(actions[0].action_type, CorporateActionType::Bonus);
    assert_eq!(
        (actions[0].ratio_from, actions[0].ratio_to),
        (Some(Decimal::from(1)), Some(Decimal::from(1)))
    );
    assert_eq!(actions[0].ex_date, day(2026, 9, 11));
    // `raw=dict(row)`: the whole row, as NSE sent it.
    assert_eq!(
        serde_json::Value::Object(actions[0].raw.clone()),
        serde_json::json!({"symbol": "PGIL", "exDate": "11-Sep-2026", "subject": "Bonus 1:1", "faceVal": "5"})
    );
}

/// An ex-date before `since`, a missing symbol or an undatable row is skipped.
#[tokio::test]
async fn rows_outside_the_window_or_without_identity_are_skipped() {
    arm();
    let (_dir, archive) = archive();
    let body = serde_json::to_vec(&serde_json::json!([
        {"symbol": "OLD", "exDate": "01-Sep-2026", "subject": "Bonus 1:1"},
        {"symbol": "", "exDate": "11-Sep-2026", "subject": "Bonus 1:1"},
        {"symbol": "NODATE", "exDate": "soon", "subject": "Bonus 1:1"},
        {"symbol": "AGM", "exDate": "11-Sep-2026", "subject": "Annual General Meeting"},
        {"symbol": "KEEP", "exDate": "2026-09-12", "subject": "Face Value Split From Rs 10/- To Rs 2/-"},
    ]))
    .unwrap();
    let fake = client(&[("corporates-corporateActions", body)]);
    let actions = build(settings(), archive, fake, None)
        .corporate_actions(sep4())
        .await
        .unwrap();
    assert_eq!(actions.len(), 1);
    assert_eq!(actions[0].symbol, "KEEP");
    assert_eq!(actions[0].action_type, CorporateActionType::Split);
    assert_eq!(
        (actions[0].ratio_from, actions[0].ratio_to),
        (Some(d("10")), Some(d("2")))
    );
}

// --- test_network_is_blocked.py: the unwired NSE provider ------------------------------------

/// docs/09 requires archive-before-parse; with no client wired there is nothing to dial.
#[tokio::test]
async fn an_unwired_nse_provider_never_dials_out() {
    arm();
    let err = bare(settings()).listings().await.unwrap_err();
    assert!(err.is_unavailable(), "{err}");
}

// --- payloads (test_nse.py's helpers, byte for byte) -----------------------------------------

fn listings_csv() -> Vec<u8> {
    b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n\
      SBIN,STATE BANK OF INDIA,EQ,01-MAR-1997,1,1,INE062A01020,1\n\
      CUPID,CUPID LIMITED,EQ,11-OCT-2017,1,1,INE509G01020,1\n"
        .to_vec()
}

fn index_snapshot_csv() -> Vec<u8> {
    b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n\
      NIFTY 50,18-08-2026,24400,24550,24380,24500.35,100.35,0.41,100,5000,22.4,3.9,1.2\n\
      INDIA VIX,18-08-2026,12,13,11,12.5,0.5,4.0,0,0,-,-,-\n"
        .to_vec()
}

fn archived_index_row_from_the_box() -> Vec<u8> {
    b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n\
      Nifty 50,27-08-2026,24277.6,24297.45,24090.85,24090.85,-116.9,-.48,323419647,24757.82,20.37,2.92,1.17\n"
        .to_vec()
}

fn bhavcopy_zip() -> Vec<u8> {
    let csv = b"TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,UpprBookgPric,LwrBookgPric\n\
                SBIN,EQ,790,805,788,800,795,10000,8000000,1200,880,720\n\
                CUPID,EQ,273,285.7,257.21,284.03,280,5000,1420150,400,340.836,227.224\n";
    zip_one("BhavCopy_NSE_CM_0_0_0_20260818_F_0000.csv", csv)
}

fn get_symbol_data_json() -> Vec<u8> {
    br#"{"equityResponse":[{"orderBook":{"lastPrice":1144},"metaData":{"symbol":"INFY","series":"EQ","closePrice":1144},"tradeInfo":{"issuedSize":4058056597,"lastPrice":1144,"faceValue":5,"totalMarketCap":4642416746968},"priceInfo":{"yearHigh":1728,"yearLow":982.4},"secInfo":{"secStatus":"Listed","pdSectorPe":"14.69","pdSymbolPe":"15.12"}}],"lastUpdateTime":"25-Aug-2026 16:00:00"}"#
        .to_vec()
}

fn quote_equity_json() -> Vec<u8> {
    br#"{"info":{"symbol":"INFY"},"securityInfo":{"issuedSize":4148506959},"priceInfo":{"lastPrice":1850.25,"pE":24.5,"pB":7.1},"metadata":{}}"#
        .to_vec()
}
