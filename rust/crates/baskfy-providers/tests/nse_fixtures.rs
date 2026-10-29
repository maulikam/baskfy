//! `FixtureProvider` and `FixtureHoldingsProvider` — `test_fixtures.py`'s cases that do not need
//! the Parquet files, and the fixture/composite half of `test_holdings_port.py`, case for case.
//!
//! The Parquet fixtures cannot be read until polars' `parquet` feature is declared in this
//! crate's manifest (DECISIONS-RS R4.12), so every rule above the file read is exercised on
//! in-memory frames with the fixture files' exact dtypes (`Decimal(38, 4)` prices, `Date`,
//! `Int64`, an all-null `isin`). The data-shape cases (forty instruments, CUPID's actions, the
//! reference export) are below, ignored with that reason, so un-ignoring them is the gate.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;

use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::composite::Registered;
use baskfy_providers::errors::ErrorKind;
use baskfy_providers::factory::register_fixtures;
use baskfy_providers::fixtures::{
    FILES, FixtureHoldingsProvider, FixtureProvider, default_fixture_dir,
};
use baskfy_providers::ports::{
    BarsProvider, Capability, HealthReporting, HoldingsProvider, ReferenceProvider,
    bars_capabilities, holdings_capabilities, reference_capabilities,
};
use baskfy_providers::records::{
    BrokerAccountFields, BrokerAccountRef, BrokerHoldingFields, BrokerHoldingRecord,
    CorporateActionType, InstrumentType, bhavcopy_schema, daily_bars_schema,
};
use chrono::NaiveDate;
use common::arm;
use polars::prelude::{
    AnyValue, Column, DataFrame, DataType, Int128Chunked, IntoColumn, IntoSeries, NamedFrom,
    NewChunkedArray, PlSmallStr, Series,
};
use rust_decimal::Decimal;

fn day(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).unwrap()
}

fn strs(name: &str, values: &[&str]) -> Column {
    Series::new(PlSmallStr::from(name), values).into_column()
}

fn opt_strs(name: &str, values: &[Option<&str>]) -> Column {
    Series::new(PlSmallStr::from(name), values).into_column()
}

fn dates(name: &str, values: &[Option<NaiveDate>]) -> Column {
    Series::new(PlSmallStr::from(name), values)
        .cast(&DataType::Date)
        .unwrap()
        .into_column()
}

fn ints(name: &str, values: &[Option<i64>]) -> Column {
    Series::new(PlSmallStr::from(name), values).into_column()
}

fn nulls(name: &str, n: usize) -> Column {
    Series::full_null(PlSmallStr::from(name), n, &DataType::Null).into_column()
}

fn decs(name: &str, values: &[Option<&str>], scale: u32) -> Column {
    let mantissas = values.iter().map(|v| {
        v.map(|t| {
            let mut d = Decimal::from_str(t).unwrap();
            d.rescale(scale);
            d.mantissa()
        })
    });
    Int128Chunked::from_iter_options(PlSmallStr::from(name), mantissas)
        .into_decimal(38, scale as usize)
        .unwrap()
        .into_series()
        .into_column()
}

fn frame(columns: Vec<Column>) -> DataFrame {
    let height = columns.first().map_or(0, Column::len);
    DataFrame::new(height, columns).unwrap()
}

/// Frames with the fixture files' dtypes.
fn frames() -> HashMap<String, DataFrame> {
    let d1 = day(2026, 8, 17);
    let d2 = day(2026, 8, 18);
    let d0 = day(2026, 8, 14);
    let mut out = HashMap::new();
    out.insert(
        "instruments".to_owned(),
        frame(vec![
            strs("symbol", &["CUPID", "ABB", "SBIN"]),
            strs("name", &["CUPID LIMITED", "ABB INDIA", "SBI"]),
            strs("instrument_type", &["EQ", "EQ", "EQ"]),
            strs("series", &["EQ", "EQ", " "]),
            nulls("isin", 3),
            opt_strs("exchange", &[Some("NSE"), Some(""), None]),
            ints("kite_token", &[Some(900_000), Some(900_001), None]),
            ints("lot_size", &[Some(1), None, Some(1)]),
            dates("listed_on", &[Some(day(2023, 8, 18)), None, None]),
        ]),
    );
    out.insert(
        "daily_bars".to_owned(),
        frame(vec![
            strs("symbol", &["CUPID", "CUPID", "ABB", "CUPID"]),
            dates("date", &[Some(d2), Some(d0), Some(d2), Some(d1)]),
            decs("open", &[Some("10"), Some("8"), Some("7500"), Some("9")], 4),
            decs(
                "high",
                &[Some("11"), Some("9"), Some("7600"), Some("10")],
                4,
            ),
            decs(
                "low",
                &[Some("9.5"), Some("7.5"), Some("7400"), Some("8.5")],
                4,
            ),
            decs(
                "close",
                &[Some("10.5"), Some("8.5"), Some("7546"), Some("9.5")],
                4,
            ),
            ints("volume", &[Some(100), Some(80), Some(150_095), Some(90)]),
            strs("source", &["nse", "nse", "nse", "nse"]),
        ]),
    );
    out.insert(
        "corporate_actions".to_owned(),
        frame(vec![
            strs("symbol", &["CUPID", "CUPID", "ABB", "CUPID"]),
            strs("action_type", &["bonus", "split", "dividend", "bonus"]),
            dates(
                "ex_date",
                &[
                    Some(day(2026, 3, 9)),
                    Some(day(2024, 4, 15)),
                    Some(day(2025, 5, 1)),
                    Some(day(2024, 4, 15)),
                ],
            ),
            decs("ratio_from", &[Some("4"), Some("10"), None, Some("1")], 0),
            decs("ratio_to", &[Some("1"), Some("1"), None, Some("1")], 0),
            decs("amount", &[None, None, Some("5.50"), None], 2),
            opt_strs(
                "purpose",
                &[
                    Some("BONUS 4:1"),
                    Some("FACE VALUE SPLIT"),
                    None,
                    Some("BONUS 1:1"),
                ],
            ),
            strs(
                "provenance",
                &["docs/01 §9", "docs/01 §9", "synthetic", "docs/01 §9"],
            ),
        ]),
    );
    out.insert(
        "index_members".to_owned(),
        frame(vec![
            strs(
                "index_slug",
                &["nifty-500", "nifty-500", "nifty-500", "nifty-50"],
            ),
            dates("date", &[Some(d2), Some(d2), Some(d1), Some(d2)]),
            strs("symbol", &["SBIN", "ABB", "CUPID", "SBIN"]),
        ]),
    );
    out.insert(
        "index_snapshots".to_owned(),
        frame(vec![
            strs("index_slug", &["nifty-50", "etf", "nifty-50"]),
            dates("date", &[Some(d2), Some(d2), Some(d1)]),
            decs(
                "level",
                &[Some("24500.3500"), Some("2780.5112"), Some("1")],
                4,
            ),
            decs("change_abs", &[Some("1"), Some("-6.9888"), None], 4),
            decs("change_pct", &[Some("0.41"), Some("-0.2507"), None], 4),
            decs("pe", &[Some("22.4"), None, None], 4),
            decs("pb", &[Some("3.9"), None, None], 4),
            decs("div_yield", &[Some("1.2"), None, None], 4),
        ]),
    );
    out.insert(
        "listings".to_owned(),
        frame(vec![
            strs("symbol", &["CUPID"]),
            strs("name", &["CUPID LIMITED"]),
            strs("series", &["EQ"]),
            nulls("isin", 1),
            dates("listed_on", &[Some(day(2023, 8, 18))]),
            decs("face_value", &[Some("1")], 4),
            decs("paid_up_value", &[Some("1")], 4),
            ints("market_lot", &[Some(1)]),
        ]),
    );
    out.insert(
        "bhavcopy".to_owned(),
        frame(vec![
            strs("symbol", &["SBIN", "ABB", "CUPID"]),
            strs("series", &["EQ", "EQ", "EQ"]),
            dates("date", &[Some(d2), Some(d2), Some(d1)]),
            decs("open", &[Some("790"), Some("7527.7136"), Some("9")], 4),
            decs("high", &[Some("805"), Some("7562.7133"), Some("10")], 4),
            decs("low", &[Some("788"), Some("7465.5123"), Some("8")], 4),
            decs("close", &[Some("800"), Some("7546"), Some("9")], 4),
            decs(
                "prev_close",
                &[Some("795"), Some("7568.6341"), Some("9")],
                4,
            ),
            ints("volume", &[Some(10_000), Some(150_095), Some(1)]),
            decs(
                "turnover",
                &[Some("8000000"), Some("1132616870"), Some("1")],
                2,
            ),
            ints("trades", &[Some(1200), Some(600), Some(1)]),
            decs(
                "upper_circuit",
                &[Some("880"), Some("9055.2"), Some("1")],
                4,
            ),
            decs(
                "lower_circuit",
                &[Some("720"), Some("6036.8"), Some("1")],
                4,
            ),
        ]),
    );
    out
}

fn provider() -> FixtureProvider {
    FixtureProvider::from_frames("/nonexistent/fixtures", frames())
}

fn dec_cell(frame: &DataFrame, row: usize, column: &str) -> Decimal {
    match frame.column(column).unwrap().get(row).unwrap() {
        AnyValue::Decimal(v, _, s) => Decimal::from_i128_with_scale(v, u32::try_from(s).unwrap()),
        other => panic!("{other:?}"),
    }
}

// --- TestShape / TestSchemas / TestBarIntegrity (on in-memory frames) -------------------------

/// docs/03's `local` environment stubs the whole provider layer with this one object.
#[test]
fn it_serves_both_ports() {
    arm();
    let expected: std::collections::BTreeSet<Capability> = bars_capabilities()
        .into_iter()
        .chain(reference_capabilities())
        .collect();
    assert_eq!(provider().capabilities(), expected);
}

#[tokio::test]
async fn instruments_are_mapped_as_python_maps_them() {
    arm();
    let records = provider().list_instruments().await.unwrap();
    assert_eq!(records.len(), 3);
    assert_eq!(records[0].symbol, "CUPID");
    assert_eq!(records[0].instrument_type, InstrumentType::Eq);
    assert_eq!(records[0].kite_token, Some(900_000));
    assert_eq!(records[0].listed_on, Some(day(2023, 8, 18)));
    assert_eq!(records[0].isin, None);
    assert_eq!(records[0].exchange, "NSE");
    // `str(row.get("exchange") or "NSE")`: an empty or null exchange is NSE.
    assert_eq!(records[1].exchange, "NSE");
    assert_eq!(records[2].exchange, "NSE");
    // `_text`: a blank series is None.
    assert_eq!(records[2].series, None);
    assert_eq!(records[1].lot_size, None);
}

/// The whole point of a fixed schema: fixture and Kite bars are swappable.
#[tokio::test]
async fn bars_conform_to_the_shared_schema_sorted_by_date() {
    arm();
    let frame = provider()
        .daily_bars_for_symbol("CUPID", day(2000, 1, 1), day(2026, 8, 18))
        .await
        .unwrap();
    assert_eq!(*frame.schema().as_ref(), daily_bars_schema());
    assert_eq!(frame.height(), 3);
    let closes: Vec<Decimal> = (0..3).map(|i| dec_cell(&frame, i, "close")).collect();
    assert_eq!(
        closes,
        [
            Decimal::from_str("8.5000").unwrap(),
            Decimal::from_str("9.5000").unwrap(),
            Decimal::from_str("10.5000").unwrap()
        ]
    );
}

/// Addressed exactly as Kite would be: by token, inclusive window.
#[tokio::test]
async fn bars_are_served_by_token_inside_the_window() {
    arm();
    let frame = provider()
        .daily_bars(900_000, day(2026, 8, 17), day(2026, 8, 17))
        .await
        .unwrap();
    assert_eq!(frame.height(), 1);
    assert_eq!(
        dec_cell(&frame, 0, "close"),
        Decimal::from_str("9.5").unwrap()
    );
}

#[tokio::test]
async fn an_empty_window_yields_an_empty_frame_with_the_schema() {
    arm();
    let frame = provider()
        .daily_bars_for_symbol("CUPID", day(1990, 1, 1), day(1990, 12, 31))
        .await
        .unwrap();
    assert_eq!(frame.height(), 0);
    assert_eq!(*frame.schema().as_ref(), daily_bars_schema());
}

#[tokio::test]
async fn bhavcopy_conforms_to_the_shared_schema_sorted_by_symbol() {
    arm();
    let frame = provider().bhavcopy(day(2026, 8, 18)).await.unwrap();
    assert_eq!(*frame.schema().as_ref(), bhavcopy_schema());
    assert_eq!(frame.height(), 2);
    assert_eq!(
        frame.column("symbol").unwrap().get(0).unwrap().get_str(),
        Some("ABB")
    );
    assert_eq!(frame.column("series").unwrap().null_count(), 0);
    assert_eq!(frame.column("upper_circuit").unwrap().null_count(), 0);
    let empty = provider().bhavcopy(day(1990, 1, 1)).await.unwrap();
    assert_eq!(empty.height(), 0);
    assert_eq!(*empty.schema().as_ref(), bhavcopy_schema());
}

// --- TestCorporateActions --------------------------------------------------------------------

#[tokio::test]
async fn actions_are_filtered_by_since_and_sorted_by_symbol_then_ex_date() {
    arm();
    let actions = provider().corporate_actions(day(2000, 1, 1)).await.unwrap();
    let got: Vec<(&str, NaiveDate)> = actions
        .iter()
        .map(|a| (a.symbol.as_str(), a.ex_date))
        .collect();
    assert_eq!(
        got,
        [
            ("ABB", day(2025, 5, 1)),
            ("CUPID", day(2024, 4, 15)),
            ("CUPID", day(2024, 4, 15)),
            ("CUPID", day(2026, 3, 9)),
        ]
    );
    let recent = provider().corporate_actions(day(2026, 1, 1)).await.unwrap();
    assert!(!recent.is_empty());
    assert!(recent.iter().all(|a| a.ex_date >= day(2026, 1, 1)));
}

/// docs/04: bonus 4:1 -> ratio_from=4, ratio_to=1; a dividend carries its amount.
#[tokio::test]
async fn the_bonus_ratio_follows_the_documented_orientation() {
    arm();
    let actions = provider().corporate_actions(day(2000, 1, 1)).await.unwrap();
    let bonus = actions
        .iter()
        .find(|a| a.symbol == "CUPID" && a.ex_date == day(2026, 3, 9))
        .unwrap();
    assert_eq!(bonus.action_type, CorporateActionType::Bonus);
    assert_eq!(
        (bonus.ratio_from, bonus.ratio_to),
        (Some(Decimal::from(4)), Some(Decimal::from(1)))
    );
    let dividend = actions
        .iter()
        .find(|a| a.action_type == CorporateActionType::Dividend)
        .unwrap();
    assert_eq!(dividend.amount.unwrap().to_string(), "5.50");
}

/// Which rows are real and which are synthetic must be readable from the data itself; a null
/// purpose is `""`, as `str(row.get("purpose") or "")`.
#[tokio::test]
async fn provenance_travels_with_each_action() {
    arm();
    let actions = provider().corporate_actions(day(2000, 1, 1)).await.unwrap();
    for action in &actions {
        assert!(!action.raw["provenance"].as_str().unwrap().is_empty());
        assert_eq!(action.raw.len(), 2);
    }
    assert_eq!(actions[0].raw["purpose"], "");
}

// --- the reference reads ---------------------------------------------------------------------

#[tokio::test]
async fn index_constituents_are_sorted_for_one_slug_and_day() {
    arm();
    assert_eq!(
        provider()
            .index_constituents("nifty-500", day(2026, 8, 18))
            .await
            .unwrap(),
        ["ABB", "SBIN"]
    );
    assert!(
        provider()
            .index_constituents("nifty-500", day(2026, 1, 1))
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn index_snapshots_keep_the_frames_decimals_and_nulls() {
    arm();
    let snapshots = provider().index_snapshots(day(2026, 8, 18)).await.unwrap();
    assert_eq!(snapshots.len(), 2);
    let etf = snapshots.iter().find(|s| s.index_slug == "etf").unwrap();
    assert_eq!(etf.level.unwrap().to_string(), "2780.5112");
    assert_eq!(etf.pe, None);
}

#[tokio::test]
async fn listings_sme_and_fundamentals() {
    arm();
    let p = provider();
    let listings = p.listings().await.unwrap();
    assert_eq!(listings.len(), 1);
    assert_eq!(listings[0].face_value.unwrap().to_string(), "1.0000");
    assert_eq!(listings[0].market_lot, Some(1));
    assert_eq!(listings[0].isin, None);
    // Fixtures carry no issued-capital series: NULL is the honest local-dev answer.
    assert!(
        p.equity_fundamentals(day(2026, 8, 18), &["CUPID".to_owned()], None)
            .await
            .unwrap()
            .is_empty()
    );
    use baskfy_providers::composite::SupplementaryReads;
    assert!(p.sme_listings().await.unwrap().is_empty());
}

#[test]
fn health_counts_instruments_and_trading_days() {
    arm();
    let health = provider().check();
    assert!(health.available);
    assert_eq!(
        health.detail,
        "3 instruments over 3 trading days from /nonexistent/fixtures"
    );
}

// --- TestFailureModes ------------------------------------------------------------------------

#[test]
fn a_missing_fixture_directory_is_reported_not_crashed() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let health = FixtureProvider::new(dir.path().join("nope")).check();
    assert!(!health.available);
    assert!(health.detail.contains("fixture_builder"));
    assert!(health.detail.starts_with(
        "missing fixture files ['instruments', 'daily_bars', 'corporate_actions', \
         'index_members', 'index_snapshots', 'listings', 'bhavcopy'] in "
    ));
}

#[tokio::test]
async fn reading_from_an_empty_directory_raises_provider_unavailable() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    let err = FixtureProvider::new(dir.path())
        .list_instruments()
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ProviderUnavailable);
    assert!(err.to_string().contains("fixture_builder"));
}

#[tokio::test]
async fn an_unknown_token_is_refused() {
    arm();
    let err = provider()
        .daily_bars(1, day(2000, 1, 1), day(2026, 8, 18))
        .await
        .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UnexpectedPayload);
    assert!(err.to_string().contains("kite_token"));
    let err = provider()
        .daily_bars_for_symbol("NOPE", day(2000, 1, 1), day(2026, 8, 18))
        .await
        .unwrap_err();
    assert_eq!(err.message(), "no fixture instrument named 'NOPE'");
}

#[tokio::test]
async fn an_inverted_window_is_rejected() {
    arm();
    let err = provider()
        .daily_bars_for_symbol("CUPID", day(2026, 8, 18), day(2000, 1, 1))
        .await
        .unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert!(err.to_string().contains("precedes"));
    assert_eq!(err.message(), "end 2000-01-01 precedes start 2026-08-18");
}

/// The deviation, said out loud: a Parquet file that exists is reported unreadable, never
/// silently empty.
#[tokio::test]
async fn a_parquet_file_is_refused_until_the_feature_is_declared() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    for (_, file) in FILES {
        std::fs::write(dir.path().join(file), b"PAR1").unwrap();
    }
    let p = FixtureProvider::new(dir.path());
    let err = p.list_instruments().await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ProviderUnavailable);
    assert!(err.to_string().contains("parquet"));
    assert!(!p.check().available);
}

#[test]
fn the_default_fixture_directory_is_the_python_one() {
    arm();
    let dir = default_fixture_dir().unwrap();
    assert!(dir.ends_with("decile-blueprint/tests/fixtures/providers"));
    // Anyone reading these numbers must be able to see what is real and what is not.
    let text = std::fs::read_to_string(dir.join("PROVENANCE.md")).unwrap();
    assert!(text.contains("Synthetic"));
    assert!(text.contains("not market data"));
}

// --- test_fixtures.py's data-shape cases: they need the Parquet files ------------------------

#[tokio::test]
#[ignore = "reads the Parquet fixtures: needs polars' `parquet` feature (DECISIONS-RS R4.12)"]
async fn about_forty_instruments_each_with_bars() {
    arm();
    let p = FixtureProvider::new(default_fixture_dir().unwrap());
    let instruments = p.list_instruments().await.unwrap();
    assert_eq!(instruments.len(), 40);
    for record in instruments {
        let frame = p
            .daily_bars(
                record.kite_token.unwrap(),
                day(2000, 1, 1),
                day(2026, 8, 18),
            )
            .await
            .unwrap();
        assert!(frame.height() > 0, "{} has no bars", record.symbol);
    }
}

#[tokio::test]
#[ignore = "reads the Parquet fixtures: needs polars' `parquet` feature (DECISIONS-RS R4.12)"]
async fn cupids_documented_actions_are_present() {
    arm();
    let p = FixtureProvider::new(default_fixture_dir().unwrap());
    let actions = p.corporate_actions(day(2000, 1, 1)).await.unwrap();
    let cupid: std::collections::BTreeSet<(CorporateActionType, i32)> = actions
        .iter()
        .filter(|a| a.symbol == "CUPID")
        .map(|a| (a.action_type, chrono::Datelike::year(&a.ex_date)))
        .collect();
    assert!(cupid.contains(&(CorporateActionType::Bonus, 2026)));
    assert!(cupid.contains(&(CorporateActionType::Split, 2024)));
    assert!(cupid.contains(&(CorporateActionType::Bonus, 2024)));
    assert!(
        actions
            .iter()
            .any(|a| a.action_type == CorporateActionType::Dividend)
    );
}

// test_fixtures.py's remaining file-backed cases, written now so un-ignoring them is the whole
// gate. TestAnchoredToRealData (four cases) needs `reference_export_io.reference_rows()`, which
// waits on core's `reference_export`, and TestDeterminism needs `fixture_builder`; neither is
// ported, so those five have no Rust counterpart yet (STATUS lists them).

const FIXTURE_AS_OF: (i32, u32, u32) = (2026, 8, 18);
const FIXTURE_YEARS: i64 = 3;

fn as_of() -> NaiveDate {
    day(FIXTURE_AS_OF.0, FIXTURE_AS_OF.1, FIXTURE_AS_OF.2)
}

async fn cupid_bars() -> DataFrame {
    FixtureProvider::new(default_fixture_dir().unwrap())
        .daily_bars_for_symbol("CUPID", day(2000, 1, 1), as_of())
        .await
        .unwrap()
}

fn date_cells(frame: &DataFrame) -> Vec<NaiveDate> {
    let column = frame.column("date").unwrap();
    (0..frame.height())
        .map(|i| match column.get(i).unwrap() {
            AnyValue::Date(days) => NaiveDate::from_num_days_from_ce_opt(days + 719_163).unwrap(),
            other => panic!("not a date: {other:?}"),
        })
        .collect()
}

fn decimal_cells(frame: &DataFrame, name: &str) -> Vec<Decimal> {
    let column = frame.column(name).unwrap();
    (0..frame.height())
        .map(|i| Decimal::from_str(&column.get(i).unwrap().to_string()).unwrap())
        .collect()
}

#[tokio::test]
#[ignore = "reads the Parquet fixtures: needs polars' `parquet` feature (DECISIONS-RS R4.12)"]
async fn three_years_of_history() {
    arm();
    let dates = date_cells(&cupid_bars().await);
    let span = *dates.iter().max().unwrap() - *dates.iter().min().unwrap();
    assert!(span.num_days() >= FIXTURE_YEARS * 365 - 5);
}

#[tokio::test]
#[ignore = "reads the Parquet fixtures: needs polars' `parquet` feature (DECISIONS-RS R4.12)"]
async fn there_is_a_split_a_bonus_and_a_dividend_with_an_amount() {
    arm();
    let actions = FixtureProvider::new(default_fixture_dir().unwrap())
        .corporate_actions(day(2000, 1, 1))
        .await
        .unwrap();
    assert!(
        actions
            .iter()
            .any(|a| a.action_type == CorporateActionType::Split)
    );
    assert!(
        actions
            .iter()
            .any(|a| a.action_type == CorporateActionType::Bonus)
    );
    let dividend = actions
        .iter()
        .find(|a| a.action_type == CorporateActionType::Dividend)
        .unwrap();
    assert!(dividend.amount.is_some());
}

#[tokio::test]
#[ignore = "reads the Parquet fixtures: needs polars' `parquet` feature (DECISIONS-RS R4.12)"]
async fn the_since_filter_is_applied_to_the_files() {
    arm();
    let recent = FixtureProvider::new(default_fixture_dir().unwrap())
        .corporate_actions(day(2026, 1, 1))
        .await
        .unwrap();
    assert!(!recent.is_empty());
    assert!(recent.iter().all(|a| a.ex_date >= day(2026, 1, 1)));
}

#[tokio::test]
#[ignore = "reads the Parquet fixtures: needs polars' `parquet` feature (DECISIONS-RS R4.12)"]
async fn the_files_conform_and_carry_series_and_circuit_bands() {
    arm();
    let p = FixtureProvider::new(default_fixture_dir().unwrap());
    assert_eq!(cupid_bars().await.schema().as_ref(), &daily_bars_schema());
    let frame = p.bhavcopy(as_of()).await.unwrap();
    assert_eq!(frame.schema().as_ref(), &bhavcopy_schema());
    for column in ["series", "upper_circuit", "lower_circuit"] {
        assert_eq!(frame.column(column).unwrap().null_count(), 0, "{column}");
    }
}

#[tokio::test]
#[ignore = "reads the Parquet fixtures: needs polars' `parquet` feature (DECISIONS-RS R4.12)"]
async fn bars_are_internally_consistent_and_positive() {
    arm();
    let frame = cupid_bars().await;
    let (open, high) = (decimal_cells(&frame, "open"), decimal_cells(&frame, "high"));
    let (low, close) = (decimal_cells(&frame, "low"), decimal_cells(&frame, "close"));
    for i in 0..frame.height() {
        assert!(low[i] <= high[i], "row {i}");
        assert!(low[i] <= close[i] && close[i] <= high[i], "row {i}");
        assert!(low[i] <= open[i] && open[i] <= high[i], "row {i}");
        assert!(low[i] > Decimal::ZERO, "row {i}");
    }
}

#[tokio::test]
#[ignore = "reads the Parquet fixtures: needs polars' `parquet` feature (DECISIONS-RS R4.12)"]
async fn dates_are_unique_ordered_and_never_a_weekend() {
    arm();
    let dates = date_cells(&cupid_bars().await);
    let mut sorted = dates.clone();
    sorted.sort();
    assert_eq!(dates, sorted);
    sorted.dedup();
    assert_eq!(sorted.len(), dates.len());
    assert!(
        dates
            .iter()
            .all(|d| chrono::Datelike::weekday(d).number_from_monday() <= 5)
    );
}

// --- test_holdings_port.py: the fixture adapter and the composite's routing -----------------

fn account(id: i64) -> BrokerAccountRef {
    BrokerAccountRef::new(BrokerAccountFields {
        broker_account_id: id,
        broker_id: "zerodha".to_owned(),
        kite_user_id: None,
    })
    .unwrap()
}

fn holding(symbol: &str, quantity: i64) -> BrokerHoldingRecord {
    let mut f = BrokerHoldingFields::new(symbol);
    f.quantity = Decimal::from(quantity);
    BrokerHoldingRecord::new(f).unwrap()
}

#[test]
fn the_fixture_holdings_adapter_satisfies_the_port_and_the_parquet_one_does_not() {
    arm();
    let fixture = FixtureHoldingsProvider::default();
    assert_eq!(fixture.capabilities(), holdings_capabilities());
    let registered = register_fixtures(provider());
    assert!(registered.holdings().is_err());
    assert!(
        !provider()
            .capabilities()
            .iter()
            .any(|c| holdings_capabilities().contains(c))
    );
}

/// Two accounts are two answers — the property a per-tenant test needs to exist.
#[tokio::test]
async fn it_answers_per_broker_account() {
    arm();
    let p = FixtureHoldingsProvider::new(
        HashMap::from([
            (1, vec![holding("SBIN", 10)]),
            (2, vec![holding("INFY", 4)]),
        ]),
        HashMap::new(),
    );
    let one: Vec<String> = p
        .broker_holdings(&account(1))
        .await
        .unwrap()
        .iter()
        .map(|r| r.symbol.clone())
        .collect();
    let two: Vec<String> = p
        .broker_holdings(&account(2))
        .await
        .unwrap()
        .iter()
        .map(|r| r.symbol.clone())
        .collect();
    assert_eq!(
        (one, two),
        (vec!["SBIN".to_owned()], vec!["INFY".to_owned()])
    );
}

/// An empty list means "sold everything". A typo must not be able to impersonate it.
#[tokio::test]
async fn an_empty_account_must_be_said_not_inferred() {
    arm();
    let p = FixtureHoldingsProvider::new(HashMap::from([(1, Vec::new())]), HashMap::new());
    assert!(p.broker_holdings(&account(1)).await.unwrap().is_empty());
    let err = p.broker_holdings(&account(2)).await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ProviderUnavailable);
    assert!(err.to_string().contains("no answer for broker account"));
}

#[tokio::test]
async fn cash_is_none_when_none_was_configured_and_decimal_when_it_was() {
    arm();
    let p = FixtureHoldingsProvider::new(HashMap::from([(1, Vec::new())]), HashMap::new());
    assert_eq!(p.broker_cash(&account(1)).await.unwrap(), None);
    let p = FixtureHoldingsProvider::new(
        HashMap::from([(1, Vec::new())]),
        HashMap::from([(1, Decimal::from_str("2500.75").unwrap())]),
    );
    assert_eq!(
        p.broker_cash(&account(1)).await.unwrap(),
        Some(Decimal::from_str("2500.75").unwrap())
    );
    assert!(p.broker_cash(&account(9)).await.is_err());
}

/// `providers doctor` must never let this be mistaken for a broker.
#[test]
fn it_reports_itself_as_fabricated() {
    arm();
    let report =
        FixtureHoldingsProvider::new(HashMap::from([(1, vec![holding("X", 1)])]), HashMap::new())
            .check();
    assert!(report.available);
    assert!(report.detail.contains("not anyone's real holdings"));
    assert_eq!(
        report.detail,
        "1 fabricated positions across 1 broker accounts; these are not anyone's real holdings"
    );
}

#[tokio::test]
async fn it_routes_a_holdings_call_to_the_adapter_that_offers_it() {
    arm();
    let fixture = Arc::new(FixtureHoldingsProvider::new(
        HashMap::from([(1, vec![holding("SBIN", 3)])]),
        HashMap::from([(1, Decimal::from(100))]),
    ));
    let composite = CompositeProvider::with_defaults(vec![
        Registered::new(fixture.clone()).with_holdings(fixture),
    ])
    .unwrap();
    let rows = composite.broker_holdings(&account(1)).await.unwrap();
    assert_eq!(rows[0].symbol, "SBIN");
    assert_eq!(
        composite.broker_cash(&account(1)).await.unwrap(),
        Some(Decimal::from(100))
    );
}

/// The whole policy, in one assertion: bars and reference but no broker session raises.
#[tokio::test]
async fn with_no_holdings_adapter_it_fails_loudly() {
    arm();
    let composite = CompositeProvider::with_defaults(vec![register_fixtures(provider())]).unwrap();
    let err = composite.broker_holdings(&account(1)).await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CapabilityNotAvailable);
    assert!(err.to_string().contains("broker_holdings"));
}

/// Claiming a capability without implementing the port is a wiring bug, not a fallback.
#[tokio::test]
async fn a_provider_that_only_claims_the_capability_is_refused() {
    arm();
    let liar = Arc::new(FixtureHoldingsProvider::default());
    let composite = CompositeProvider::with_defaults(vec![Registered::new(liar)]).unwrap();
    let err = composite.broker_holdings(&account(1)).await.unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CapabilityNotAvailable);
    assert!(
        err.to_string()
            .contains("does not implement HoldingsProvider")
    );
}
