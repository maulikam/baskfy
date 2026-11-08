//! `NseProvider::fo_bhavcopy` — the F&O end-of-day file (docs/fno/07 §1, docs/fno/03 §1).
//! `test_nse_fo_bhavcopy.py`, case for case.
//!
//! Both NSE layouts land on one schema, the URL follows the layout switch of 8 Jul 2024, the raw
//! file is archived before it is parsed, and a file stamped with another session is refused.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;
mod nse_common;

use std::str::FromStr;
use std::sync::Arc;

use baskfy_providers::archive::{RawArchive, archive_key};
use baskfy_providers::errors::ErrorKind;
use baskfy_providers::nse::{FO_UDIFF_SINCE, KIND_FO_BHAVCOPY};
use baskfy_providers::records::fo_bhavcopy_schema;
use chrono::NaiveDate;
use common::arm;
use nse_common::{FakeHttpClient, archive, build, settings, zip_one};
use polars::prelude::{AnyValue, DataFrame};
use rust_decimal::Decimal;

const UDIFF_HEADER: &str = "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,\
XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,\
PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,\
TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n";

fn udiff_day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 18).unwrap()
}

fn legacy_day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2024, 6, 28).unwrap()
}

fn udiff(stamp: &str) -> Vec<u8> {
    let rows = format!(
        "{stamp},{stamp},FO,NSE,STF,1,,SBIN,,2026-08-25,2026-08-25,,,SBIN25AUGFUT,\
801,812,799,810.5,810.5,800,808.2,810.35,60000000,1500000,12000,9700000000,9000,F1,750,\
,,,,\n\
{stamp},{stamp},FO,NSE,STO,2,,SBIN,,2026-08-25,2026-08-25,820,CE,SBIN25AUG820CE,\
9.5,11,8,10.05,10,9,808.2,10.05,3000000,-75000,4000,30000000,3000,F1,750,,,,,\n\
{stamp},{stamp},FO,NSE,IDO,3,,NIFTY,,2026-08-25,2026-08-25,24000,PE,NIFTY25AUG24000PE,\
50,60,45,55.25,55,48,24400,55.25,900000,1000,20000,80000000,10000,F1,75,,,,,\n"
    );
    zip_one(
        "BhavCopy_NSE_FO_0_0_0_20260818_F_0000.csv",
        format!("{UDIFF_HEADER}{rows}").as_bytes(),
    )
}

fn legacy() -> Vec<u8> {
    let csv = "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,\
CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,\n\
FUTSTK,SBIN,27-Jun-2024,0,XX,840,845,838,842.3,842.3,9000,56000.5,90000000,-2000,\
28-JUN-2024,\n\
OPTSTK,SBIN,25-Jul-2024,860,CE,20,22,19,21.1,21.1,500,300.25,1200000,1500,\
28-JUN-2024,\n";
    zip_one("fo28JUN2024bhav.csv", csv.as_bytes())
}

fn client(fragment: &str, body: Vec<u8>) -> Arc<FakeHttpClient> {
    Arc::new(FakeHttpClient::new(&[(fragment, body)]))
}

fn cell(frame: &DataFrame, row: usize, column: &str) -> AnyValue<'static> {
    frame
        .column(column)
        .unwrap()
        .get(row)
        .unwrap()
        .into_static()
}

fn dec(frame: &DataFrame, row: usize, column: &str) -> Option<Decimal> {
    match cell(frame, row, column) {
        AnyValue::Decimal(v, _, s) => {
            Some(Decimal::from_i128_with_scale(v, u32::try_from(s).unwrap()))
        }
        AnyValue::Null => None,
        other => panic!("{column}: {other:?}"),
    }
}

fn int(frame: &DataFrame, row: usize, column: &str) -> Option<i64> {
    match cell(frame, row, column) {
        AnyValue::Int64(v) => Some(v),
        AnyValue::Null => None,
        other => panic!("{column}: {other:?}"),
    }
}

fn text(frame: &DataFrame, row: usize, column: &str) -> Option<String> {
    match cell(frame, row, column) {
        AnyValue::Null => None,
        v => Some(v.get_str().unwrap().to_owned()),
    }
}

fn day(frame: &DataFrame, row: usize, column: &str) -> Option<NaiveDate> {
    match cell(frame, row, column) {
        AnyValue::Date(days) => Some(
            NaiveDate::from_ymd_opt(1970, 1, 1).unwrap() + chrono::TimeDelta::days(i64::from(days)),
        ),
        AnyValue::Null => None,
        other => panic!("{column}: {other:?}"),
    }
}

fn d(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

// --- TestUdiffLayout -------------------------------------------------------------------------

#[tokio::test]
async fn every_contract_lands_on_the_schema() {
    arm();
    let (_dir, archive) = archive();
    let frame = build(
        settings(),
        archive,
        client("BhavCopy_NSE_FO", udiff("2026-08-18")),
        None,
    )
    .fo_bhavcopy(udiff_day())
    .await
    .unwrap();
    assert_eq!(*frame.schema().as_ref(), fo_bhavcopy_schema());
    let instruments: Vec<Option<String>> = (0..frame.height())
        .map(|i| text(&frame, i, "instrument"))
        .collect();
    assert_eq!(
        instruments,
        [
            Some("FUTSTK".to_owned()),
            Some("OPTSTK".to_owned()),
            Some("OPTIDX".to_owned())
        ]
    );
}

#[tokio::test]
async fn a_future_carries_no_strike_or_option_type_and_keeps_underlying_and_lot() {
    arm();
    let (_dir, archive) = archive();
    let frame = build(
        settings(),
        archive,
        client("BhavCopy_NSE_FO", udiff("2026-08-18")),
        None,
    )
    .fo_bhavcopy(udiff_day())
    .await
    .unwrap();
    assert_eq!(text(&frame, 0, "option_type"), None);
    assert_eq!(
        day(&frame, 0, "expiry"),
        NaiveDate::from_ymd_opt(2026, 8, 25)
    );
    assert_eq!(dec(&frame, 0, "settle"), Some(d("810.35")));
    assert_eq!(dec(&frame, 0, "underlying"), Some(d("808.2")));
    assert_eq!(int(&frame, 0, "lot_size"), Some(750));
    assert_eq!(int(&frame, 0, "open_interest"), Some(60_000_000));
    assert_eq!(day(&frame, 0, "date"), Some(udiff_day()));
    assert_eq!(dec(&frame, 1, "strike"), Some(d("820")));
    assert_eq!(text(&frame, 1, "option_type").as_deref(), Some("CE"));
    assert_eq!(int(&frame, 1, "oi_change"), Some(-75_000));
    // Python's Decimal('9700000000.00') — the schema's scale, byte for byte.
    assert_eq!(
        dec(&frame, 0, "turnover").unwrap().to_string(),
        "9700000000.00"
    );
}

#[tokio::test]
async fn the_url_is_the_udiff_file_and_it_is_archived_before_parsing() {
    arm();
    let (_dir, archive) = archive();
    let fake = client("BhavCopy_NSE_FO", udiff("2026-08-18"));
    build(settings(), archive.clone(), fake.clone(), None)
        .fo_bhavcopy(udiff_day())
        .await
        .unwrap();
    assert!(
        fake.requests()
            .last()
            .unwrap()
            .ends_with("/content/fo/BhavCopy_NSE_FO_0_0_0_20260818_F_0000.csv.zip")
    );
    assert!(
        archive
            .exists(&archive_key(KIND_FO_BHAVCOPY, udiff_day(), "zip").unwrap())
            .await
            .unwrap()
    );
}

#[tokio::test]
async fn a_file_stamped_with_another_session_is_refused() {
    arm();
    let (_dir, archive) = archive();
    let err = build(
        settings(),
        archive,
        client("BhavCopy_NSE_FO", udiff("2026-08-17")),
        None,
    )
    .fo_bhavcopy(udiff_day())
    .await
    .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    assert!(err.to_string().contains("wrong file"));
    assert_eq!(
        err.message(),
        "F&O bhavcopy for 2026-08-18 is stamped [datetime.date(2026, 8, 17)]; wrong file for \
         this date"
    );
}

// --- TestLegacyLayout ------------------------------------------------------------------------

#[tokio::test]
async fn days_before_the_switch_read_the_historical_derivatives_file() {
    arm();
    assert!(legacy_day() < FO_UDIFF_SINCE);
    let (_dir, archive) = archive();
    let fake = client("fo28JUN2024bhav", legacy());
    build(settings(), archive, fake.clone(), None)
        .fo_bhavcopy(legacy_day())
        .await
        .unwrap();
    assert!(
        fake.requests()
            .last()
            .unwrap()
            .ends_with("/content/historical/DERIVATIVES/2024/JUN/fo28JUN2024bhav.csv.zip")
    );
}

#[tokio::test]
async fn legacy_rows_land_on_the_same_schema_with_absent_fields_null() {
    arm();
    let (_dir, archive) = archive();
    let frame = build(
        settings(),
        archive,
        client("fo28JUN2024bhav", legacy()),
        None,
    )
    .fo_bhavcopy(legacy_day())
    .await
    .unwrap();
    assert_eq!(*frame.schema().as_ref(), fo_bhavcopy_schema());
    assert_eq!(text(&frame, 0, "option_type"), None);
    assert_eq!(
        day(&frame, 0, "expiry"),
        NaiveDate::from_ymd_opt(2024, 6, 27)
    );
    assert_eq!(dec(&frame, 0, "underlying"), None);
    assert_eq!(int(&frame, 0, "lot_size"), None);
    // VAL_INLAKH is in lakh; the schema's turnover is rupees.
    assert_eq!(dec(&frame, 0, "turnover"), Some(d("5600050000.00")));
    assert_eq!(dec(&frame, 1, "strike"), Some(d("860")));
    assert_eq!(text(&frame, 1, "option_type").as_deref(), Some("CE"));
}

#[tokio::test]
async fn an_empty_file_is_refused() {
    arm();
    let (_dir, archive) = archive();
    let body = zip_one(
        "BhavCopy_NSE_FO_0_0_0_20260818_F_0000.csv",
        UDIFF_HEADER.as_bytes(),
    );
    let err = build(settings(), archive, client("BhavCopy_NSE_FO", body), None)
        .fo_bhavcopy(udiff_day())
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    assert!(err.to_string().contains("no readable rows"));
}
